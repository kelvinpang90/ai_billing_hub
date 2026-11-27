"""Retention purge of wallet reconciliation findings and runs (AIH-TASK-268).

设计闸门 #862 v2 §7。覆盖「核对：最近一次 `COMPLETED`」「核对：正常路径」
「核对：只开运行一项」「核对：没有任何 `COMPLETED`」，以及这两项的「边界值」
「count 与 off」。注册表与静态扫描在 tests/backend/test_retention_purge.py。

「正常路径」在清理前后各调用一次 app/services/alerts.py 的
`wallet_negative_balance`（核对告警并在这一维里），断言两次结果相同，不打桩。

行为用例在 SQLite 与 MySQL 上各跑一遍（`db` 夹具）。SQLite 打开外键检查，
与 MySQL 一样拒绝删掉还有发现指向的运行。MySQL 那一半需要
`BILLING_TEST_DATABASE_URL` 指向一个**可以被清空的**库，没设时 skip ——
**skip 不是 pass**，CI 设了它。MySQL 上每个用例前后先清空
`wallet_reconciliation_findings` 再清空 `wallet_reconciliation_runs`
（没有触发器；发现以 RESTRICT 指向运行，普通 DELETE 即可），并删掉本文件邮箱域的
租户；审计表只追加、拒绝 DELETE，所以只看本用例开始之后写入的审计（`id` 大于起点）。

时间一律注入：`NOW` 是固定的整秒时刻，截止时刻 `CUTOFF = NOW − 731 天`。
uuid 用固定的占位值。
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import pytest
from alembic.config import Config
from sqlalchemy import Engine, create_engine, delete, event, func, select
from sqlalchemy.orm import Session, sessionmaker

from alembic import command
from app.core.database import create_session_factory
from app.models.auth import AuditAction, AuditLog
from app.models.base import Base
from app.models.tenancy import Tenant
from app.models.wallet_reconciliation import (
    ReconciliationRunStatus,
    WalletReconciliationFinding,
    WalletReconciliationRun,
)
from app.services import alerts
from app.services.retention_purge import (
    ENTITY_TYPE,
    ITEMS,
    WALLET_RECONCILIATION_RETENTION,
    PurgeResult,
    run_purge,
)

DOMAIN = "@retention-reconciliation.example.com"
FINDINGS = "wallet_reconciliation_findings"
RUNS = "wallet_reconciliation_runs"

COMPLETED = ReconciliationRunStatus.COMPLETED
ABANDONED = ReconciliationRunStatus.ABANDONED
RUNNING = ReconciliationRunStatus.RUNNING

NOW = dt.datetime(2026, 11, 25, 3, 0, 0)
CUTOFF = NOW - dt.timedelta(days=731)
LONG_AGO = CUTOFF - dt.timedelta(days=5)
SECOND = dt.timedelta(seconds=1)
HOUR = dt.timedelta(hours=1)
DAY = dt.timedelta(days=1)

DELETE = {FINDINGS: "delete", RUNS: "delete"}
COUNT = {FINDINGS: "count", RUNS: "count"}
NO_CAP = 50_000


# --- 库 -------------------------------------------------------------------------------


@dataclass
class Db:
    engine: Engine
    factory: sessionmaker[Session]
    # 本用例开始之前审计表里最大的 id：MySQL 上的审计不能删，只看它之后的。
    audit_start: int


def _mysql_url() -> str:
    # 环境变量在夹具里读：xdist 换库名（conftest 的 pytest_configure）之后才有最终值。
    url = os.environ.get("BILLING_TEST_DATABASE_URL", "")
    if not url:
        pytest.skip("BILLING_TEST_DATABASE_URL is not set; the MySQL half needs a real MySQL")
    return url


def clean(engine: Engine) -> None:
    """MySQL only: the findings, then the runs they point at, then this file's tenants."""
    with engine.begin() as connection:
        connection.execute(delete(WalletReconciliationFinding))
        connection.execute(delete(WalletReconciliationRun))
        connection.execute(delete(Tenant).where(Tenant.email.like(f"%{DOMAIN}")))


def _audit_start(factory: sessionmaker[Session]) -> int:
    with factory() as session:
        return int(session.execute(select(func.coalesce(func.max(AuditLog.id), 0))).scalar_one())


def _sqlite_foreign_keys(dbapi_connection, connection_record) -> None:
    # SQLite 缺省不查外键；打开后删掉还有发现指向的运行与 MySQL 一样报错。
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


@pytest.fixture(params=["sqlite", "mysql"])
def db(request, monkeypatch) -> Iterator[Db]:
    if request.param == "sqlite":
        engine = create_engine("sqlite+pysqlite:///:memory:")
        event.listen(engine, "connect", _sqlite_foreign_keys)
        Base.metadata.create_all(engine)
        factory = create_session_factory(engine)
        try:
            yield Db(engine=engine, factory=factory, audit_start=0)
        finally:
            engine.dispose()
        return

    url = _mysql_url()
    # 建表走 alembic：审计表的只追加触发器只有迁移会建。
    from app.core.config import get_settings

    monkeypatch.setenv("BILLING_DATABASE_URL", url)
    get_settings.cache_clear()
    command.upgrade(Config("alembic.ini"), "head")
    get_settings.cache_clear()
    engine = create_engine(url, pool_size=4, max_overflow=4)
    clean(engine)
    factory = create_session_factory(engine)
    try:
        yield Db(engine=engine, factory=factory, audit_start=_audit_start(factory))
    finally:
        clean(engine)
        engine.dispose()


# --- 造数 -----------------------------------------------------------------------------

_numbers = iter(range(1, 1_000_000))


def make_tenant(db: Db, number: int) -> int:
    with db.factory() as session:
        tenant = Tenant(
            public_id=str(uuid.UUID(int=number)),
            company_name=f"Fictional Reconciliation {number} Sdn Bhd",
            email=f"tenant{number}{DOMAIN}",
            created_at=LONG_AGO,
            updated_at=LONG_AGO,
        )
        session.add(tenant)
        session.commit()
        return int(tenant.id)


def add_run(
    db: Db,
    status: ReconciliationRunStatus,
    *,
    finished_at: dt.datetime | None,
    started_at: dt.datetime | None = None,
    inconsistent: int = 0,
) -> int:
    """Insert one run; return its `id`. `RUNNING` has no `finished_at` and holds the slot."""
    number = next(_numbers)
    started = started_at or (finished_at - HOUR if finished_at is not None else NOW - HOUR)
    with db.factory() as session:
        run = WalletReconciliationRun(
            public_id=str(uuid.UUID(int=(1 << 64) + number)),
            run_date=started.date(),
            status=status,
            started_at=started,
            finished_at=finished_at,
            cursor_tenant_id=3,
            wallets_checked=3,
            wallets_inconsistent=inconsistent,
            lease_owner=None,
            lease_until=None,
            running_slot=1 if status is RUNNING else None,
        )
        session.add(run)
        session.commit()
        return int(run.id)


def add_finding(db: Db, run_id: int, tenant_id: int) -> int:
    """Insert one finding of `run_id`; return its `id`. The purge never reads `checked_at`."""
    with db.factory() as session:
        finding = WalletReconciliationFinding(
            run_id=run_id,
            tenant_id=tenant_id,
            problem_codes="BALANCE_MISMATCH",
            balance=Decimal("12.5"),
            wallet_version=4,
            ledger_rows=7,
            checked_at=NOW,
        )
        session.add(finding)
        session.commit()
        return int(finding.id)


# --- 读回 -----------------------------------------------------------------------------


def run_ids(db: Db) -> set[int]:
    with db.factory() as session:
        return {int(i) for i in session.execute(select(WalletReconciliationRun.id)).scalars()}


def finding_ids(db: Db) -> set[int]:
    with db.factory() as session:
        return {int(i) for i in session.execute(select(WalletReconciliationFinding.id)).scalars()}


def purge_audits(db: Db) -> list[AuditLog]:
    with db.factory() as session:
        rows = list(
            session.execute(
                select(AuditLog)
                .where(
                    AuditLog.id > db.audit_start,
                    AuditLog.action == AuditAction.DATA_RETENTION_PURGE,
                )
                .order_by(AuditLog.id)
            ).scalars()
        )
        for row in rows:
            session.expunge(row)
        return rows


def reconciliation_alert(db: Db) -> alerts.AlertResult:
    with db.factory() as session:
        return alerts.wallet_negative_balance(
            session, alerts.AlertContext(now=NOW, currencies=("USD",))
        )


def rows_of(result: PurgeResult) -> dict[str, int]:
    return {outcome.item: outcome.rows for outcome in result.items}


def ours(result: PurgeResult) -> dict[str, int]:
    rows = rows_of(result)
    return {FINDINGS: rows[FINDINGS], RUNS: rows[RUNS]}


def purge(
    db: Db, modes: dict[str, Any] | None = None, *, max_rows: int = NO_CAP, now: dt.datetime = NOW
) -> PurgeResult:
    return run_purge(
        db.factory,
        modes=DELETE if modes is None else modes,
        max_rows_per_item=max_rows,
        now=now,
    )


# --- 最近一次 COMPLETED -------------------------------------------------------------------


def test_the_only_completed_run_stays_however_old(db: Db) -> None:
    """设计 §7「核对：最近一次 `COMPLETED`」：只有一条 `COMPLETED` 且结束满 731 天，
    它与它的发现都不删（§1「我定的」第 4 条）。"""
    tenant = make_tenant(db, 1)
    latest = add_run(db, COMPLETED, finished_at=LONG_AGO)
    finding = add_finding(db, latest, tenant)

    counted = purge(db, COUNT)
    result = purge(db)

    assert result.failed_item is None
    assert run_ids(db) == {latest}
    assert finding_ids(db) == {finding}
    assert ours(counted) == ours(result) == {FINDINGS: 0, RUNS: 0}
    assert purge_audits(db) == []


def test_runs_newer_than_the_latest_completed_stay_however_old(db: Db) -> None:
    """设计 §7「核对：最近一次 `COMPLETED`」另一种：最近一次 `COMPLETED` 之后
    有结束满 731 天的 `ABANDONED`（有发现、没发现）——它们与最近 `COMPLETED`
    都不删；比它旧的 `ABANDONED` 照删。"""
    tenant = make_tenant(db, 1)
    older = add_run(db, ABANDONED, finished_at=LONG_AGO - DAY)
    older_finding = add_finding(db, older, tenant)
    latest = add_run(db, COMPLETED, finished_at=LONG_AGO)
    newer = add_run(db, ABANDONED, finished_at=LONG_AGO + HOUR)
    newer_finding = add_finding(db, newer, tenant)
    newer_bare = add_run(db, ABANDONED, finished_at=LONG_AGO + 2 * HOUR)

    result = purge(db)

    assert result.failed_item is None
    assert run_ids(db) == {latest, newer, newer_bare}
    assert finding_ids(db) == {newer_finding}
    assert older_finding not in finding_ids(db)
    assert ours(result) == {FINDINGS: 1, RUNS: 1}


# --- 正常路径 -----------------------------------------------------------------------------


def test_findings_go_first_then_their_runs_and_the_alert_is_unchanged(db: Db) -> None:
    """设计 §7「核对：正常路径」：结束满 731 天的 `COMPLETED` / `ABANDONED`
    （其中两条有发现）与它们的发现都删，发现先删、运行后删（审计顺序）；
    `RUNNING`、不满 731 天的与最近一次 `COMPLETED`（连同它的发现）留着；
    核对告警的结果清理前后相同。"""
    first = make_tenant(db, 1)
    second = make_tenant(db, 2)
    old_completed = add_run(db, COMPLETED, finished_at=LONG_AGO, inconsistent=2)
    old_abandoned = add_run(db, ABANDONED, finished_at=LONG_AGO + DAY, inconsistent=1)
    gone_findings = {
        add_finding(db, old_completed, first),
        add_finding(db, old_completed, second),
        add_finding(db, old_abandoned, first),
    }
    gone_runs = {
        old_completed,
        old_abandoned,
        add_run(db, COMPLETED, finished_at=CUTOFF - SECOND),
        add_run(db, ABANDONED, finished_at=CUTOFF - DAY),
    }
    recent = add_run(db, COMPLETED, finished_at=CUTOFF + SECOND)
    yesterday = add_run(db, ABANDONED, finished_at=NOW - DAY)
    latest = add_run(db, COMPLETED, finished_at=NOW - HOUR, inconsistent=1)
    latest_finding = add_finding(db, latest, second)
    running = add_run(db, RUNNING, finished_at=None, started_at=NOW - 10 * SECOND)
    before = reconciliation_alert(db)

    result = purge(db)

    assert result.failed_item is None
    assert run_ids(db) == {recent, yesterday, latest, running}
    assert not gone_runs & run_ids(db)
    assert finding_ids(db) == {latest_finding}
    assert not gone_findings & finding_ids(db)
    assert rows_of(result) == {
        "refresh_tokens": 0,
        "password_reset_tokens": 0,
        "recovery_codes": 0,
        "domain_outbox": 0,
        "notifications": 0,
        FINDINGS: len(gone_findings),
        RUNS: len(gone_runs),
    }
    assert [(o.item, o.mode, o.cutoff) for o in result.items if o.item in (FINDINGS, RUNS)] == [
        (FINDINGS, "delete", CUTOFF),
        (RUNS, "delete", CUTOFF),
    ]
    audits = purge_audits(db)
    assert [json.loads(audit.after_state or "null") for audit in audits] == [
        {"item": FINDINGS, "cutoff": "2024-11-24T03:00:00", "batch": 1, "deleted": 3},
        {"item": RUNS, "cutoff": "2024-11-24T03:00:00", "batch": 1, "deleted": 4},
    ]
    for audit in audits:
        assert audit.actor_user_id is None
        assert audit.actor_role == "SYSTEM"
        assert audit.entity_type == ENTITY_TYPE
        assert audit.entity_id == "2026-11-25"
        assert audit.created_at == NOW
    assert reconciliation_alert(db) == before


# --- 只开运行一项 ---------------------------------------------------------------------------


def test_with_only_the_runs_item_on_runs_with_findings_stay(db: Db) -> None:
    """设计 §7「核对：只开运行一项」、§3「核对外键」：发现一项 `off`、运行一项
    `delete`，有发现的运行不删、不报错（不撞 RESTRICT 外键）；没有发现的照删。
    之后打开发现一项，下一轮两者都删。"""
    tenant = make_tenant(db, 1)
    with_finding = add_run(db, ABANDONED, finished_at=LONG_AGO)
    finding = add_finding(db, with_finding, tenant)
    bare = add_run(db, COMPLETED, finished_at=LONG_AGO + HOUR)
    latest = add_run(db, COMPLETED, finished_at=NOW - HOUR)

    result = purge(db, {FINDINGS: "off", RUNS: "delete"})

    assert result.failed_item is None, result
    assert result.error_type is None
    assert run_ids(db) == {with_finding, latest}
    assert bare not in run_ids(db)
    assert finding_ids(db) == {finding}
    assert ours(result) == {FINDINGS: 0, RUNS: 1}

    both = purge(db)

    assert both.failed_item is None, both
    assert run_ids(db) == {latest}
    assert finding_ids(db) == set()
    assert ours(both) == {FINDINGS: 1, RUNS: 1}


# --- 没有任何 COMPLETED ----------------------------------------------------------------------


def test_without_any_completed_run_nothing_goes(db: Db) -> None:
    """设计 §7「核对：没有任何 `COMPLETED`」：全是结束很久的 `ABANDONED`
    （有发现、没发现）时 `MAX(id)` 为 NULL，一行都不删。"""
    tenant = make_tenant(db, 1)
    runs = {
        add_run(db, ABANDONED, finished_at=LONG_AGO - DAY),
        add_run(db, ABANDONED, finished_at=LONG_AGO),
    }
    with_finding = add_run(db, ABANDONED, finished_at=LONG_AGO + HOUR)
    runs.add(with_finding)
    finding = add_finding(db, with_finding, tenant)

    counted = purge(db, COUNT)
    result = purge(db)

    assert result.failed_item is None
    assert run_ids(db) == runs
    assert finding_ids(db) == {finding}
    assert ours(counted) == ours(result) == {FINDINGS: 0, RUNS: 0}
    assert purge_audits(db) == []


# --- 边界值 ---------------------------------------------------------------------------


def test_a_run_finished_exactly_at_the_cutoff_stays_with_its_findings(db: Db) -> None:
    """设计 §7「边界值」：严格早于截止时刻。运行的 `finished_at` 恰好等于截止时刻的
    （`COMPLETED`、`ABANDONED`，有发现、没发现）连同发现都不删；早一秒的删；
    两项的 `count` 与 `delete` 一致。"""
    tenant = make_tenant(db, 1)
    at_with_finding = add_run(db, ABANDONED, finished_at=CUTOFF)
    at_finding = add_finding(db, at_with_finding, tenant)
    at_bare = add_run(db, COMPLETED, finished_at=CUTOFF)
    before_with_finding = add_run(db, COMPLETED, finished_at=CUTOFF - SECOND)
    add_finding(db, before_with_finding, tenant)
    add_run(db, ABANDONED, finished_at=CUTOFF - SECOND)
    latest = add_run(db, COMPLETED, finished_at=NOW - HOUR)

    counted_findings = purge(db, {FINDINGS: "count"})
    # 运行一项只删没有发现的运行：在发现删完之后数，
    # 才与同一轮里两项都 `delete` 的结果可比。
    findings_then_runs = purge(db, {FINDINGS: "delete", RUNS: "count"})

    assert rows_of(counted_findings)[FINDINGS] == rows_of(findings_then_runs)[FINDINGS] == 1
    assert finding_ids(db) == {at_finding}

    result = purge(db)

    assert run_ids(db) == {at_with_finding, at_bare, latest}
    assert finding_ids(db) == {at_finding}
    assert rows_of(findings_then_runs)[RUNS] == rows_of(result)[RUNS] == 2
    assert rows_of(result)[FINDINGS] == 0


# --- count 与 off -----------------------------------------------------------------------


def test_count_mode_deletes_nothing_and_reports_what_delete_would(db: Db) -> None:
    """`count` 只数不删、不写审计。运行一项数的是此刻没有发现的可删运行：
    两项都 `count` 时有发现的运行不计；发现一项 `delete` 在前、运行一项 `count`
    在后时，计入刚删完发现的运行，与随后两项都 `delete` 删掉的行数一致。"""
    tenant = make_tenant(db, 1)
    with_findings = add_run(db, COMPLETED, finished_at=LONG_AGO)
    add_finding(db, with_findings, tenant)
    add_finding(db, with_findings, make_tenant(db, 2))
    add_run(db, ABANDONED, finished_at=LONG_AGO + HOUR)
    latest = add_run(db, COMPLETED, finished_at=NOW - HOUR)
    runs_before, findings_before = run_ids(db), finding_ids(db)

    counted = purge(db, COUNT)

    assert (run_ids(db), finding_ids(db)) == (runs_before, findings_before)
    assert purge_audits(db) == []
    assert ours(counted) == {FINDINGS: 2, RUNS: 1}
    assert [(o.item, o.mode) for o in counted.items if o.item in (FINDINGS, RUNS)] == [
        (FINDINGS, "count"),
        (RUNS, "count"),
    ]
    assert counted.deleted == 0

    findings_deleted = purge(db, {FINDINGS: "delete", RUNS: "count"})

    assert ours(findings_deleted) == {FINDINGS: 2, RUNS: 2}
    assert finding_ids(db) == set()
    assert run_ids(db) == runs_before

    deleted = purge(db)

    assert ours(deleted) == {FINDINGS: 0, RUNS: rows_of(findings_deleted)[RUNS]}
    assert run_ids(db) == {latest}


def test_off_mode_runs_no_sql_on_the_reconciliation_tables(db: Db) -> None:
    """`off` 不执行这两张表的 SQL：两项 `off`、其余各项 `count` 时没有一句提到它们；
    全部 `off` 时一句 SQL 都没有。（运行一项的条件读发现表，所以只关发现一项时，
    运行一项仍会读到发现表。）"""
    tenant = make_tenant(db, 1)
    old = add_run(db, ABANDONED, finished_at=LONG_AGO)
    add_finding(db, old, tenant)
    add_run(db, COMPLETED, finished_at=NOW - HOUR)
    runs_before, findings_before = run_ids(db), finding_ids(db)
    statements: list[str] = []
    tables = re.compile(rf"\b({FINDINGS}|{RUNS})\b")

    def capture(conn, cursor, statement, parameters, context, executemany) -> None:
        statements.append(statement)

    event.listen(db.engine, "before_cursor_execute", capture)
    try:
        others = purge(db, {item.name: "count" for item in ITEMS if item.name not in DELETE})
        assert statements, "其余各项 count：确有 SQL 发出"
        mentioned = [s for s in statements if tables.search(s.replace("`", "").lower())]
        statements.clear()
        purge(db, {FINDINGS: "off", RUNS: "off"})
    finally:
        event.remove(db.engine, "before_cursor_execute", capture)

    assert mentioned == []
    assert statements == []
    assert [(o.item, o.mode, o.rows) for o in others.items if o.item in (FINDINGS, RUNS)] == [
        (FINDINGS, "off", 0),
        (RUNS, "off", 0),
    ]
    assert (run_ids(db), finding_ids(db)) == (runs_before, findings_before)


def test_the_retention_is_731_days() -> None:
    """设计 §1「我定的」第 2 条：任意连续 24 个日历月最长 731 天；两项共用一个常量。"""

    assert WALLET_RECONCILIATION_RETENTION == dt.timedelta(days=731)
    assert NOW - WALLET_RECONCILIATION_RETENTION == CUTOFF == dt.datetime(2024, 11, 24, 3, 0, 0)
    assert {item.retention for item in ITEMS if item.name in (FINDINGS, RUNS)} == {
        WALLET_RECONCILIATION_RETENTION
    }

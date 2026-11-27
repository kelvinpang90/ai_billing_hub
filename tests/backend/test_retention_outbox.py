"""Retention purge of settled domain outbox rows (设计闸门 #862 v2 §7; AIH-TASK-266).

覆盖设计 §7 的「领域 outbox 正常路径」「领域 outbox 死信告警」「恢复扫描与通知幂等」三行，以及本项的
「边界值」「count 与 off」「批与上限」「幂等重放」。「锁范围（MySQL）」的 `domain_outbox` 场景在
tests/backend/test_retention_purge_mysql.py；注册表与静态扫描在
tests/backend/test_retention_purge.py。

死信告警调用 app/services/alerts.py 的 `outbox_backlog`，恢复扫描调用 app/tasks/outbox.py 的
`recover` 与 `deliver`，都不打桩它们的查询：只把任务接到本用例的库、注入时钟、把 `delay` 换成一个
记下 id 的列表（broker 不在），并让任何 `build_adapter` 调用失败（通知类事件不发信）。

行为用例在 SQLite 与 MySQL 上各跑一遍（`db` 夹具）。MySQL 那一半需要 `BILLING_TEST_DATABASE_URL`
指向一个**可以被清空的**库，没设时 skip —— **skip 不是 pass**，CI 设了它。MySQL 上每个用例前后清空
`domain_outbox`（没有触发器、没有入向外键，普通 DELETE 即可），
并删掉本文件邮箱域的租户与它们的通知；
审计表只追加、拒绝 DELETE，所以只看本用例开始之后写入的审计（`id` 大于起点）。

时间一律注入：`NOW` 是固定的整秒时刻，截止时刻 `CUTOFF = NOW − 90 天`。uuid 用全零占位值。
"""

from __future__ import annotations

import datetime as dt
import json
import os
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import pytest
from alembic.config import Config
from sqlalchemy import Engine, create_engine, delete, event, func, insert, select
from sqlalchemy.orm import Session, sessionmaker

from alembic import command
from app.core.config import Settings
from app.core.database import create_session_factory
from app.models.auth import AuditAction, AuditLog, DomainOutbox, OutboxStatus
from app.models.base import Base
from app.models.notifications import Notification, NotificationType
from app.models.tenancy import Tenant
from app.repositories.wallet import AGGREGATE_TENANT, EVENT_LOW_BALANCE
from app.services import alerts
from app.services.account_status import EVENT_ACCOUNT_STATUS_CHANGED
from app.services.internal_billing import EVENT_BILLING_MODE_CHANGED
from app.services.password_reset import EVENT_PASSWORD_RESET
from app.services.project_integration import EVENT_INTEGRATION_STATUS_CHANGED
from app.services.retention_purge import (
    BATCH_SIZE,
    DOMAIN_OUTBOX_RETENTION,
    ENTITY_TYPE,
    PurgeResult,
    run_purge,
)
from app.tasks import outbox as outbox_task

DOMAIN = "@retention-outbox.example.com"
ITEM = "domain_outbox"

NOW = dt.datetime(2026, 11, 25, 3, 0, 0)
CUTOFF = NOW - dt.timedelta(days=90)
LONG_AGO = CUTOFF - dt.timedelta(days=5)
SECOND = dt.timedelta(seconds=1)

DELETE = {ITEM: "delete"}
COUNT = {ITEM: "count"}
NO_CAP = 50_000
ZERO_UUID = "00000000-0000-0000-0000-000000000000"

# 设计闸门 #862 v2 §1「已定」K10：三类没有处理器、维持永久 `PENDING` 的事件。
UNHANDLED = (
    EVENT_ACCOUNT_STATUS_CHANGED,
    EVENT_BILLING_MODE_CHANGED,
    EVENT_INTEGRATION_STATUS_CHANGED,
)
# 写入方 `post_transaction` 的低余额 payload（app/repositories/wallet.py）。
LOW = {"balance": "45.00000000", "threshold": "50.00000000", "wallet_transaction": ZERO_UUID}


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
    """MySQL only: `domain_outbox` entirely, then this file's tenants and their notifications."""
    with engine.begin() as connection:
        ours = select(Tenant.id).where(Tenant.email.like(f"%{DOMAIN}"))
        connection.execute(delete(Notification).where(Notification.tenant_id.in_(ours)))
        connection.execute(delete(DomainOutbox))
        connection.execute(delete(Tenant).where(Tenant.email.like(f"%{DOMAIN}")))


def _audit_start(factory: sessionmaker[Session]) -> int:
    with factory() as session:
        return int(session.execute(select(func.coalesce(func.max(AuditLog.id), 0))).scalar_one())


@pytest.fixture(params=["sqlite", "mysql"])
def db(request, monkeypatch) -> Iterator[Db]:
    if request.param == "sqlite":
        engine = create_engine("sqlite+pysqlite:///:memory:")
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


def wire(monkeypatch: pytest.MonkeyPatch, db: Db) -> list[int]:
    """Point the outbox task at this database with the clock at `NOW`; the broker is gone.

    返回 `delay` 收到的 id 列表。通知类事件不发信：任何 `build_adapter` 调用让用例失败。
    """

    def no_adapter(_channel: object, _settings: Settings) -> object:
        raise AssertionError("notification events must not build a notification adapter")

    queued: list[int] = []
    settings = Settings(frontend_base_url="", outbox_retry_base_seconds=60)
    monkeypatch.setattr(outbox_task, "_session_factory", lambda: db.factory)
    monkeypatch.setattr(outbox_task, "get_settings", lambda: settings)
    monkeypatch.setattr(outbox_task, "utc_now", lambda: NOW)
    monkeypatch.setattr(outbox_task, "build_adapter", no_adapter)
    monkeypatch.setattr(outbox_task.deliver, "delay", queued.append)
    return queued


# --- 造数 -----------------------------------------------------------------------------


def add_outbox(
    db: Db,
    *,
    status: OutboxStatus,
    processed_at: dt.datetime | None,
    event_type: str = EVENT_PASSWORD_RESET,
    created_at: dt.datetime | None = None,
    next_retry_at: dt.datetime | None = None,
    attempt_count: int = 1,
    aggregate_type: str = "users",
    aggregate_id: str = "0",
    payload: dict[str, Any] | None = None,
) -> int:
    created = created_at or processed_at or NOW - dt.timedelta(hours=1)
    with db.factory() as session:
        row = DomainOutbox(
            event_type=event_type,
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            payload_json=None if payload is None else json.dumps(payload, sort_keys=True),
            status=status,
            attempt_count=attempt_count,
            next_retry_at=next_retry_at or created,
            created_at=created,
            processed_at=processed_at,
        )
        session.add(row)
        session.commit()
        return int(row.id)


def add_many_settled(db: Db, count: int, *, processed_at: dt.datetime) -> None:
    with db.factory() as session:
        session.execute(
            insert(DomainOutbox),
            [
                {
                    "event_type": EVENT_PASSWORD_RESET,
                    "aggregate_type": "users",
                    "aggregate_id": "0",
                    "payload_json": None,
                    "status": OutboxStatus.SENT,
                    "attempt_count": 1,
                    "next_retry_at": processed_at,
                    "created_at": processed_at,
                    "processed_at": processed_at,
                }
                for _ in range(count)
            ],
        )
        session.commit()


def make_tenant(db: Db, number: int) -> tuple[int, str]:
    public_id = str(uuid.UUID(int=number))
    with db.factory() as session:
        tenant = Tenant(
            public_id=public_id,
            company_name=f"Fictional Outbox {number} Sdn Bhd",
            email=f"tenant{number}{DOMAIN}",
            created_at=LONG_AGO,
            updated_at=LONG_AGO,
        )
        session.add(tenant)
        session.commit()
        return int(tenant.id), public_id


def add_notification(db: Db, tenant_id: int, *, source_outbox_id: int, number: int) -> None:
    with db.factory() as session:
        session.add(
            Notification(
                public_id=str(uuid.UUID(int=number)),
                tenant_id=tenant_id,
                notification_type=NotificationType.LOW_BALANCE,
                source_outbox_id=source_outbox_id,
                balance=Decimal("45.00000000"),
                threshold=Decimal("50.00000000"),
                occurred_at=LONG_AGO,
                created_at=LONG_AGO,
            )
        )
        session.commit()


# --- 读回 -----------------------------------------------------------------------------


def outbox_ids(db: Db) -> set[int]:
    with db.factory() as session:
        return set(session.execute(select(DomainOutbox.id)).scalars())


def notification_sources(db: Db) -> list[int]:
    with db.factory() as session:
        return list(
            session.execute(
                select(Notification.source_outbox_id).order_by(Notification.id)
            ).scalars()
        )


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


def batches(db: Db) -> list[tuple[int, int]]:
    """(batch, deleted) of this item's audits, in write order."""
    found = [json.loads(row.after_state or "null") for row in purge_audits(db)]
    return [(state["batch"], state["deleted"]) for state in found if state["item"] == ITEM]


def rows_of(result: PurgeResult) -> dict[str, int]:
    return {outcome.item: outcome.rows for outcome in result.items}


def purge(
    db: Db, modes: dict[str, Any] | None = None, *, max_rows: int = NO_CAP, now: dt.datetime = NOW
) -> PurgeResult:
    return run_purge(
        db.factory,
        modes=DELETE if modes is None else modes,
        max_rows_per_item=max_rows,
        now=now,
    )


def outbox_alert(db: Db) -> alerts.AlertResult:
    with db.factory() as session:
        return alerts.outbox_backlog(session, alerts.AlertContext(now=NOW, currencies=()))


# --- 正常路径 -----------------------------------------------------------------------------


def test_only_sent_or_failed_rows_settled_ninety_days_ago_go(db: Db) -> None:
    """设计 §7「领域 outbox 正常路径」、INV-13：`PENDING`（有处理器、三类无处理器、`created_at`
    与 `next_retry_at` 很早的、带着很早的 `processed_at` 的）、`processed_at` 为空的 `SENT`、
    结算不满 90 天的都留着。"""
    gone = {
        add_outbox(db, status=OutboxStatus.SENT, processed_at=CUTOFF - SECOND),
        add_outbox(db, status=OutboxStatus.FAILED, processed_at=LONG_AGO),
        add_outbox(
            db,
            status=OutboxStatus.SENT,
            processed_at=LONG_AGO,
            event_type=EVENT_LOW_BALANCE,
            aggregate_type=AGGREGATE_TENANT,
            aggregate_id=ZERO_UUID,
            payload=LOW,
        ),
    }
    very_early = CUTOFF - dt.timedelta(days=365)
    kept = {
        add_outbox(db, status=OutboxStatus.SENT, processed_at=CUTOFF + dt.timedelta(days=1)),
        add_outbox(db, status=OutboxStatus.FAILED, processed_at=NOW - dt.timedelta(days=1)),
        add_outbox(db, status=OutboxStatus.SENT, processed_at=None, created_at=very_early),
        add_outbox(
            db,
            status=OutboxStatus.PENDING,
            processed_at=None,
            created_at=very_early,
            next_retry_at=very_early,
            attempt_count=2,
        ),
        # 正常不存在（结算才写 `processed_at`）：钉住条件里的 `status` 过滤本身，去掉它这一行就被删
        # （设计 §7 变异「去掉 status 过滤」）。
        add_outbox(
            db,
            status=OutboxStatus.PENDING,
            processed_at=LONG_AGO,
            created_at=very_early,
            next_retry_at=very_early,
        ),
        *(
            add_outbox(
                db,
                status=OutboxStatus.PENDING,
                processed_at=None,
                event_type=event_type,
                aggregate_type=AGGREGATE_TENANT,
                aggregate_id=ZERO_UUID,
                created_at=very_early,
                attempt_count=0,
            )
            for event_type in UNHANDLED
        ),
    }
    assert not set(UNHANDLED) & set(outbox_task.handled_event_types())

    result = purge(db)

    assert outbox_ids(db) == kept
    assert result.failed_item is None
    assert rows_of(result) == {
        "refresh_tokens": 0,
        "password_reset_tokens": 0,
        "recovery_codes": 0,
        ITEM: len(gone),
        "notifications": 0,
        "wallet_reconciliation_findings": 0,
        "wallet_reconciliation_runs": 0,
    }
    [outcome] = [o for o in result.items if o.item == ITEM]
    assert (outcome.mode, outcome.cutoff) == ("delete", CUTOFF)
    [audit] = purge_audits(db)
    assert audit.actor_user_id is None
    assert audit.actor_role == "SYSTEM"
    assert audit.entity_type == ENTITY_TYPE
    assert audit.entity_id == "2026-11-25"
    assert audit.created_at == NOW
    # 恰好这四个键，与 R1 相同（设计 §1「可观察的完成标准」第 3 条）。
    assert json.loads(audit.after_state or "null") == {
        "item": ITEM,
        "cutoff": "2026-08-27T03:00:00",
        "batch": 1,
        "deleted": len(gone),
    }


# --- 死信告警 -----------------------------------------------------------------------------


def test_a_dead_letter_inside_the_seven_day_window_stays_and_still_alerts(db: Db) -> None:
    """设计 §7「领域 outbox 死信告警」：7 天内的 `FAILED` 不删，死信告警的结果清理前后相同。"""
    recent = add_outbox(db, status=OutboxStatus.FAILED, processed_at=NOW - dt.timedelta(days=2))
    old = add_outbox(db, status=OutboxStatus.FAILED, processed_at=LONG_AGO)
    before = outbox_alert(db)

    result = purge(db)

    assert outbox_ids(db) == {recent}
    assert old not in outbox_ids(db)
    assert rows_of(result)[ITEM] == 1
    after = outbox_alert(db)
    assert after == before
    assert after.level == alerts.AlertLevel.P2
    assert "1 dead letter in 7 days" in after.summary


# --- 恢复扫描与通知幂等 -----------------------------------------------------------------------


def test_recovery_after_the_purge_dispatches_pending_and_makes_no_new_notification(
    db: Db, monkeypatch: pytest.MonkeyPatch
) -> None:
    """设计 §7「恢复扫描与通知幂等」、INV-14：删掉一条满 90 天的通知类 `SENT` 行后跑一次恢复扫描。

    另一条 `PENDING` 的通知类行照常派发并产生它自己的通知；被删那一行不会再派生通知。
    """
    queued = wire(monkeypatch, db)
    tenant_id, public_id = make_tenant(db, 1)
    settled = add_outbox(
        db,
        status=OutboxStatus.SENT,
        processed_at=LONG_AGO,
        event_type=EVENT_LOW_BALANCE,
        aggregate_type=AGGREGATE_TENANT,
        aggregate_id=public_id,
        payload=LOW,
    )
    add_notification(db, tenant_id, source_outbox_id=settled, number=101)
    pending = add_outbox(
        db,
        status=OutboxStatus.PENDING,
        processed_at=None,
        event_type=EVENT_LOW_BALANCE,
        aggregate_type=AGGREGATE_TENANT,
        aggregate_id=public_id,
        payload=LOW,
        attempt_count=0,
    )

    assert rows_of(purge(db))[ITEM] == 1
    assert outbox_ids(db) == {pending}
    assert notification_sources(db) == [settled], "通知表没有外键，通知留着"

    assert outbox_task.recover() == 1
    assert queued == [pending]
    assert [outbox_task.deliver(outbox_id) for outbox_id in queued] == ["notified"]
    assert outbox_task.deliver(settled) == "missing"

    assert notification_sources(db) == [settled, pending]
    with db.factory() as session:
        row = session.get(DomainOutbox, pending)
        assert row is not None
        assert (row.status, row.processed_at) == (OutboxStatus.SENT, NOW)


# --- 边界值 ---------------------------------------------------------------------------


def test_a_row_settled_exactly_at_the_cutoff_stays(db: Db) -> None:
    """严格早于截止时刻：`processed_at` 恰好等于的不删，早一秒的删；`count` 与 `delete` 一致。"""
    at = {
        add_outbox(db, status=OutboxStatus.SENT, processed_at=CUTOFF),
        add_outbox(db, status=OutboxStatus.FAILED, processed_at=CUTOFF),
    }
    add_outbox(db, status=OutboxStatus.SENT, processed_at=CUTOFF - SECOND)
    add_outbox(db, status=OutboxStatus.FAILED, processed_at=CUTOFF - SECOND)

    counted = purge(db, COUNT)
    result = purge(db)

    assert outbox_ids(db) == at
    assert rows_of(counted)[ITEM] == rows_of(result)[ITEM] == 2


# --- count 与 off -----------------------------------------------------------------------


def test_count_mode_deletes_nothing_and_reports_what_delete_would(db: Db) -> None:
    add_outbox(db, status=OutboxStatus.SENT, processed_at=LONG_AGO)
    add_outbox(db, status=OutboxStatus.FAILED, processed_at=LONG_AGO)
    pending = add_outbox(
        db, status=OutboxStatus.PENDING, processed_at=None, created_at=LONG_AGO, attempt_count=0
    )
    before = outbox_ids(db)

    counted = purge(db, COUNT)

    assert outbox_ids(db) == before
    assert purge_audits(db) == []
    assert rows_of(counted)[ITEM] == 2
    assert [(o.item, o.mode) for o in counted.items if o.item == ITEM] == [(ITEM, "count")]
    assert counted.deleted == 0

    deleted = purge(db)

    assert rows_of(deleted)[ITEM] == rows_of(counted)[ITEM]
    assert outbox_ids(db) == {pending}


def test_off_mode_runs_no_sql_on_the_outbox(db: Db) -> None:
    """`off` 不执行这张表的 SQL：本项 `off`、R1 三项 `delete` 时没有一句提到 `domain_outbox`；
    全部 `off` 时一句 SQL 都没有。"""
    add_outbox(db, status=OutboxStatus.SENT, processed_at=LONG_AGO)
    before = outbox_ids(db)
    statements: list[str] = []

    def capture(conn, cursor, statement, parameters, context, executemany) -> None:
        statements.append(statement)

    event.listen(db.engine, "before_cursor_execute", capture)
    try:
        r1_only = purge(
            db,
            {
                "refresh_tokens": "delete",
                "password_reset_tokens": "delete",
                "recovery_codes": "delete",
                ITEM: "off",
            },
        )
        mentioned = [s for s in statements if "domain_outbox" in s.replace("`", "").lower()]
        statements.clear()
        purge(db, {ITEM: "off"})
    finally:
        event.remove(db.engine, "before_cursor_execute", capture)

    assert mentioned == []
    assert statements == []
    assert [(o.item, o.mode, o.rows) for o in r1_only.items if o.item == ITEM] == [(ITEM, "off", 0)]
    assert outbox_ids(db) == before


# --- 批与上限 -------------------------------------------------------------------------


def test_batches_stop_at_the_cap_and_the_next_round_finishes(db: Db) -> None:
    """2 500 行可删、上限 2 000：两批各 1 000、两条审计后停；下一轮删余下 500。"""
    add_many_settled(db, 2_500, processed_at=LONG_AGO)
    kept = add_outbox(db, status=OutboxStatus.SENT, processed_at=NOW - dt.timedelta(days=1))

    first = purge(db, max_rows=2_000)

    assert rows_of(first)[ITEM] == 2_000
    assert len(outbox_ids(db)) == 501
    assert batches(db) == [(1, BATCH_SIZE), (2, BATCH_SIZE)]

    second = purge(db, max_rows=2_000)

    assert rows_of(second)[ITEM] == 500
    assert outbox_ids(db) == {kept}
    assert batches(db) == [(1, 1_000), (2, 1_000), (1, 500)]


# --- 幂等重放 -------------------------------------------------------------------------


def test_running_twice_with_the_same_now_deletes_nothing_the_second_time(db: Db) -> None:
    add_outbox(db, status=OutboxStatus.SENT, processed_at=LONG_AGO)
    add_outbox(db, status=OutboxStatus.FAILED, processed_at=LONG_AGO)
    kept = add_outbox(db, status=OutboxStatus.SENT, processed_at=NOW - dt.timedelta(days=1))
    purge(db)
    audits = len(purge_audits(db))
    assert audits == 1

    again = purge(db)

    assert rows_of(again)[ITEM] == 0
    assert len(purge_audits(db)) == audits
    assert outbox_ids(db) == {kept}


def test_the_retention_is_ninety_days() -> None:
    assert DOMAIN_OUTBOX_RETENTION == dt.timedelta(days=90)
    assert NOW - DOMAIN_OUTBOX_RETENTION == CUTOFF

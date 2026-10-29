"""Wallet reconciliation steps 3–5 and `step` (AIH-TASK-132, design gate #474 v13 §2, §7).

§7 里属于第 3 到第 5 步与 `step` 的行在这里（第 0 到第 2 步的步骤函数在
test_wallet_reconciliation_start.py）：

- 两库：正常路径（钱包与账本行不变）；运行期间提交的低 id 租户；凌晨空表的收尾不挡住当天的
  完成部分；读到运行之后失败的 `step` 部分；钱包不存在；完成时刻；未来日期的运行的推进部分；
  收尾失败则本次不开始；游标不符时释放租约；租约过期后接手的提交部分；长读取续租；
  异常时释放租约；释放失败只记 warning 的 `step` 部分；事务中途失败；推进中读钱包失败；
  事务不跨扫描；一次 step 一个今天；Redis/Celery 丢失；
- 只在 SQLite（unit）：预算用注入的 `monotonic`；
- 只在 MySQL：并发推进（领不到租约的一方返回 `busy`、不扫描）；单块超时；金额精度（记录）。

SQLite 用 test_usage_billing 的工厂（`create_all` 建表，加一条代替 0006 触发器推进钱包的测试
触发器）；MySQL 那一半需要 `BILLING_TEST_DATABASE_URL`，没设时 skip ——
**不要把 skipped 读成 passed**，CI 里不许 skip。MySQL 库是共用的：别的测试文件留下的租户也会被
核对，计数断言加上开局时它们的数目（`Db.checked`、`Db.bad`），findings 只看这里建的租户。

⚠️ SQL 文本只用来挑出语句注入故障或计数，不写死方言字面量；只有 MySQL 才有的断言放在
MySQL 用例里。示例 uuid 全零（只有末尾计数）。
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import itertools
import logging
import re
import threading
from collections.abc import Callable, Iterator
from decimal import Decimal
from typing import Any, cast

import pytest
from sqlalchemy import delete, event, select, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker
from test_usage_billing import T0, TEST_EMAIL_DOMAIN, _mysql_factory, _sqlite_factory
from test_wallet_reconciliation_start import (
    ABANDONED,
    COMPLETED,
    LOGGER,
    RELEASE_FAILED,
    RUNNING,
    TODAY,
    TOMORROW,
    UPDATE_RUNS,
    YESTERDAY,
    Run,
    _wipe,
    engine_of,
    extras,
    fail_on,
    myt,
    put_run,
    runs,
    sample_uuid,
    the_run,
)
from test_wallet_streaming import post_in, truncate_ledger

from app.core.celery_app import create_celery_app
from app.core.config import Settings
from app.core.database import create_session_factory
from app.models.tenancy import Tenant
from app.models.wallet import Wallet, WalletTransaction
from app.models.wallet_reconciliation import WalletReconciliationFinding
from app.repositories import wallet as wallet_repository
from app.repositories.wallet import (
    BALANCE_NOT_LEDGER_SUM,
    LEDGER_MISSING,
    WALLET_MISSING,
    check_wallet_snapshot,
    create_wallet,
    verify_wallet,
)
from app.services import wallet_reconciliation
from app.services.wallet_reconciliation import (
    LEASE_SECONDS,
    TENANT_BATCH,
    retire_stale_runs,
    start_run,
    step,
)
from app.tasks import wallet_reconciliation as reconciliation_task

Finding = WalletReconciliationFinding

NOW = myt(TODAY, 3, 5)
FAILED = "wallet_reconciliation_failed"

UPDATE_PROGRESS = re.compile(
    r"^\s*UPDATE\s+\W?wallet_reconciliation_runs\b.*\bcursor_tenant_id\b", re.IGNORECASE | re.DOTALL
)
# 释放语句是运行表上唯一既不带 status 也不带 cursor_tenant_id 的 UPDATE。
UPDATE_RELEASE = re.compile(
    r"^\s*UPDATE\s+\W?wallet_reconciliation_runs\b(?!.*\bstatus\b)(?!.*\bcursor_tenant_id\b)",
    re.IGNORECASE | re.DOTALL,
)
UPDATE_RENEW = re.compile(
    r"^\s*UPDATE\s+\W?wallet_reconciliation_runs\s+SET\s+\W?lease_until\W?\s*=",
    re.IGNORECASE,
)
INSERT_FINDINGS = re.compile(
    r"^\s*INSERT\s+INTO\s+\W?wallet_reconciliation_findings\b", re.IGNORECASE
)
# 第 2 步读运行的那条：只有它选 public_id。
SELECT_CLAIM = re.compile(
    r"^\s*SELECT\b.*\bpublic_id\b.*\bFROM\s+\W?wallet_reconciliation_runs\b",
    re.IGNORECASE | re.DOTALL,
)
SELECT_CURSOR = re.compile(r"^\s*SELECT\b.*\bcursor_tenant_id\b", re.IGNORECASE | re.DOTALL)
LEDGER_SELECT = re.compile(r"^\s*SELECT\b.*\bFROM\s+\W?wallet_transactions\b", re.I | re.DOTALL)
MONEY_WRITE = re.compile(
    r"^\s*(?:INSERT\s+INTO|UPDATE|DELETE\s+FROM)\s+\W?(?:wallets|wallet_transactions)\b",
    re.IGNORECASE,
)

# 租户 uuid 前四组全零，末尾计数从别的测试文件用不到的区段开始，免得在共用的 MySQL 库里撞号。
_NUMBERS = itertools.count(132_000_000_001)


# --- fixtures --------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class Db:
    """A session factory and the tenants other test files left in a shared database.

    `foreign`：租户 id → 开局时它的钱包是否不一致。SQLite 上为空。
    """

    factory: sessionmaker[Session]
    foreign: dict[int, bool]

    def checked(self, ours: int) -> int:
        return ours + len(self.foreign)

    def bad(self, ours: int) -> int:
        return ours + sum(self.foreign.values())


def _foreign(factory: sessionmaker[Session]) -> dict[int, bool]:
    with factory() as session:
        tenant_ids = session.scalars(select(Tenant.id).order_by(Tenant.id)).all()
        found = {
            int(tenant_id): bool(check_wallet_snapshot(session, tenant_id).problem_codes)
            for tenant_id in tenant_ids
        }
        session.rollback()
    return found


def _sqlite_db() -> Iterator[Db]:
    for factory in _sqlite_factory():
        yield Db(factory, {})


def _mysql_db(monkeypatch: pytest.MonkeyPatch) -> Iterator[Db]:
    """test_usage_billing 的 MySQL 工厂；两张核对表前后清空（findings 引用租户，先于租户删）。"""
    generator = _mysql_factory(monkeypatch)
    factory = next(generator)
    _wipe(factory)
    try:
        yield Db(factory, _foreign(factory))
    finally:
        _wipe(factory)
        generator.close()


@pytest.fixture(params=["sqlite", "mysql"])
def both(request, monkeypatch) -> Iterator[Db]:
    """The same cases on SQLite and on a real MySQL."""
    if request.param == "mysql":
        yield from _mysql_db(monkeypatch)
        return
    yield from _sqlite_db()


@pytest.fixture
def sqlite_db() -> Iterator[Db]:
    yield from _sqlite_db()


@pytest.fixture
def mysql_db(monkeypatch) -> Iterator[Db]:
    yield from _mysql_db(monkeypatch)


# --- helpers ---------------------------------------------------------------------------


class Clock:
    """Wall clock for `step`: `now`, then `now + tick` on each later call."""

    def __init__(self, moment: dt.datetime, tick: dt.timedelta = dt.timedelta(0)) -> None:
        self.now = moment
        self.tick = tick
        self.readings: list[dt.datetime] = []

    def __call__(self) -> dt.datetime:
        current = self.now
        self.readings.append(current)
        self.now = current + self.tick
        return current


class Monotonic:
    """Budget clock for `step`: stands still unless a test moves it."""

    def __init__(self, value: float = 1000.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


class Checks:
    """Wraps the service's `check_wallet_snapshot`: records calls, runs hooks, can shrink chunks.

    `before(tenant_id)` 在快照会话发出第一条语句之前运行；`after(tenant_id)` 在判定之后运行；
    `per_chunk()` 在每块读完、服务的续租检查之前运行。
    """

    def __init__(self, monkeypatch: pytest.MonkeyPatch, *, chunk_rows: int | None = None) -> None:
        self.calls: list[tuple[str, int]] = []
        self.on_chunk_given: list[bool] = []
        self.before: Callable[[int], None] | None = None
        self.after: Callable[[int], None] | None = None
        self.per_chunk: Callable[[], None] | None = None
        real = wallet_repository.check_wallet_snapshot

        def wrapped(session: Session, tenant_id: int, **kwargs: Any):
            self.calls.append((threading.current_thread().name, tenant_id))
            self.on_chunk_given.append(kwargs.get("on_chunk") is not None)
            if self.before is not None:
                self.before(tenant_id)
            if chunk_rows is not None:
                kwargs["chunk_rows"] = chunk_rows
            service_on_chunk = kwargs.get("on_chunk")
            if self.per_chunk is not None and service_on_chunk is not None:
                per_chunk = self.per_chunk

                def on_chunk() -> None:
                    per_chunk()
                    service_on_chunk()

                kwargs["on_chunk"] = on_chunk
            result = real(session, tenant_id, **kwargs)
            if self.after is not None:
                self.after(tenant_id)
            return result

        monkeypatch.setattr(wallet_reconciliation, "check_wallet_snapshot", wrapped)

    @property
    def seen(self) -> list[int]:
        return [tenant_id for _, tenant_id in self.calls]


def make_tenant(
    factory: sessionmaker[Session], *, tenant_id: int | None = None, wallet: bool = True
) -> int:
    number = next(_NUMBERS)
    with factory() as session:
        tenant = Tenant(
            public_id=f"00000000-0000-0000-0000-{number:012d}",
            company_name="Fictional Reconciliation Sdn Bhd",
            email=f"reconciliation-{number}{TEST_EMAIL_DOMAIN}",
            created_at=T0,
            updated_at=T0,
        )
        if tenant_id is not None:
            tenant.id = tenant_id
        session.add(tenant)
        session.flush()
        if wallet:
            create_wallet(session, tenant_id=tenant.id, now=T0)
        session.commit()
        return int(tenant.id)


def fund(factory: sessionmaker[Session], tenant_id: int, *amounts: int | str) -> None:
    """Top-ups through the real repository, each in its own transaction."""
    for amount in amounts:
        with factory() as session:
            post_in(session, tenant_id, Decimal(amount))
            session.commit()


def max_tenant_id(factory: sessionmaker[Session]) -> int:
    with factory() as session:
        return int(session.scalar(select(Tenant.id).order_by(Tenant.id.desc()).limit(1)) or 0)


def only_run(factory: sessionmaker[Session]) -> Run:
    (run,) = runs(factory)
    return run


def findings(
    factory: sessionmaker[Session], run_id: int, tenants: list[int] | None = None
) -> list[Finding]:
    statement = select(Finding).where(Finding.run_id == run_id).order_by(Finding.tenant_id)
    if tenants is not None:
        statement = statement.where(Finding.tenant_id.in_(tenants))
    with factory() as session:
        return list(session.scalars(statement))


def all_findings(factory: sessionmaker[Session]) -> list[Finding]:
    with factory() as session:
        return list(session.scalars(select(Finding).order_by(Finding.id)))


def money_rows(factory: sessionmaker[Session]) -> tuple[list[tuple], list[tuple]]:
    """Every wallet and ledger row, column by column."""
    with factory() as session:
        wallets = session.execute(select(*Wallet.__table__.columns).order_by(Wallet.id)).all()
        ledger = session.execute(
            select(*WalletTransaction.__table__.columns).order_by(WalletTransaction.id)
        ).all()
    return [tuple(row) for row in wallets], [tuple(row) for row in ledger]


def run_step(
    db: Db,
    *,
    now: dt.datetime = NOW,
    clock: Callable[[], dt.datetime] | None = None,
    monotonic: Callable[[], float] | None = None,
    factory: sessionmaker[Session] | None = None,
) -> str:
    return step(
        factory if factory is not None else db.factory,
        clock=clock if clock is not None else Clock(now),
        monotonic=monotonic if monotonic is not None else Monotonic(),
    )


def records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [record for record in caplog.records if record.name == LOGGER]


def assert_one_failure(
    caplog: pytest.LogCaptureFixture, stage: str, run_id: str | None, error_type: str
) -> None:
    logged = records(caplog)
    assert [(record.levelno, record.getMessage()) for record in logged] == [(logging.ERROR, FAILED)]
    assert extras(logged[0]) == {"stage": stage, "run_id": run_id, "error_type": error_type}


def assert_unleased(run: Run) -> None:
    assert (run.lease_owner, run.lease_until) == (None, None)


class WalletReadFailure(Exception):
    pass


# --- 定时任务与 beat --------------------------------------------------------------------


def test_the_beat_entry_runs_the_step_every_300_seconds() -> None:
    app = create_celery_app(Settings(redis_url="memory://"))
    entry = app.conf.beat_schedule["wallet-reconciliation-step"]
    assert entry == {
        "task": "app.tasks.wallet_reconciliation.step",
        "schedule": 300,
        "options": {"expires": 270},
    }
    assert "app.tasks.wallet_reconciliation" in app.conf.include
    assert "app.tasks.wallet_reconciliation.step" in app.tasks


# --- 正常路径 --------------------------------------------------------------------------


def test_a_run_completes_and_records_the_inconsistent_wallets(both, caplog) -> None:
    factory = both.factory
    mysql = engine_of(factory).dialect.name == "mysql"
    if mysql:
        # MySQL 的触发器挡住直接改钱包：只做一致与 TRUNCATE 账本两种。
        emptied = make_tenant(factory)
        fund(factory, emptied, 1, 2)
        truncate_ledger(factory)
        consistent = make_tenant(factory)
        fund(factory, consistent, 1, 2)
        ours, bad = [emptied, consistent], {emptied: LEDGER_MISSING}
    else:
        consistent = make_tenant(factory)
        fund(factory, consistent, 1, 2)
        emptied = make_tenant(factory)
        fund(factory, emptied, 1, 2)
        edited = make_tenant(factory)
        fund(factory, edited, 1, 2)
        with factory() as session:
            session.execute(delete(WalletTransaction).where(WalletTransaction.tenant_id == emptied))
            session.execute(
                update(Wallet).where(Wallet.tenant_id == edited).values(balance=Wallet.balance + 1)
            )
            session.commit()
        ours = [consistent, emptied, edited]
        bad = {emptied: LEDGER_MISSING, edited: BALANCE_NOT_LEDGER_SUM}
    before = money_rows(factory)
    statements: list[str] = []

    def record(_conn, _cursor, statement, *_args) -> None:
        statements.append(statement)

    engine = engine_of(factory)
    event.listen(engine, "before_cursor_execute", record)
    try:
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            assert run_step(both) == "completed"
    finally:
        event.remove(engine, "before_cursor_execute", record)

    run = only_run(factory)
    assert run.status is COMPLETED
    assert run.finished_at == NOW
    assert run.running_slot is None
    assert_unleased(run)
    assert run.cursor_tenant_id == max_tenant_id(factory)
    assert run.wallets_checked == both.checked(len(ours))
    assert run.wallets_inconsistent == both.bad(len(bad))

    found = findings(factory, run.id, ours)
    assert [finding.tenant_id for finding in found] == sorted(bad)
    with factory() as session:
        for finding in found:
            assert bad[finding.tenant_id] in finding.problem_codes.split(",")
            # 按 ledger_problems 的输出顺序以英文逗号连接。
            assert finding.problem_codes == ",".join(verify_wallet(session, finding.tenant_id))
            wallet = session.execute(
                select(Wallet.balance, Wallet.version).where(Wallet.tenant_id == finding.tenant_id)
            ).one()
            assert (finding.balance, finding.wallet_version) == (wallet.balance, wallet.version)
            assert finding.checked_at == NOW
    assert {finding.tenant_id: finding.ledger_rows for finding in found} == (
        {emptied: 0} if mysql else {emptied: 0, edited: 2}
    )

    # 只读钱包与账本（INV-4、INV-5）：行原样，没有任何写它们的语句。
    assert money_rows(factory) == before
    assert [statement for statement in statements if MONEY_WRITE.search(statement)] == []
    assert records(caplog) == []


def test_a_missing_wallet_is_recorded_without_balance_or_version(both) -> None:
    tenant_id = make_tenant(both.factory, wallet=False)

    assert run_step(both) == "completed"

    run = only_run(both.factory)
    (finding,) = findings(both.factory, run.id, [tenant_id])
    assert finding.problem_codes == WALLET_MISSING
    assert (finding.balance, finding.wallet_version, finding.ledger_rows) == (None, None, 0)


def test_idle_and_busy_are_returned_as_they_are(both, monkeypatch) -> None:
    checks = Checks(monkeypatch)
    make_tenant(both.factory)
    put_run(both.factory, YESTERDAY, COMPLETED)
    # 表非空、早于 03:00：不开始，没有可推进的运行。
    assert run_step(both, now=myt(TODAY, 2, 0)) == "idle"

    run_id = put_run(
        both.factory,
        TODAY,
        RUNNING,
        lease_owner=sample_uuid(),
        lease_until=NOW + dt.timedelta(seconds=100),
    )
    assert run_step(both) == "busy"
    assert checks.calls == []
    assert the_run(both.factory, run_id).wallets_checked == 0


# --- 覆盖范围 --------------------------------------------------------------------------


def test_a_low_id_tenant_committed_during_a_run_is_covered_by_the_next_run(
    both, monkeypatch
) -> None:
    factory = both.factory
    base = max_tenant_id(factory) + 100
    first, second, third = (make_tenant(factory, tenant_id=base + n) for n in (1, 3, 5))
    late = base + 2
    checks = Checks(monkeypatch)

    def commit_a_late_tenant(tenant_id: int) -> None:
        # 游标已过第 2 个租户：模拟较小 id 的建户事务晚提交。
        if tenant_id == third:
            make_tenant(factory, tenant_id=late)

    checks.before = commit_a_late_tenant
    assert run_step(both) == "completed"
    run = only_run(factory)
    assert run.wallets_checked == both.checked(3)
    assert late not in checks.seen
    assert [tenant_id for tenant_id in checks.seen if tenant_id > base] == [first, second, third]

    checks.before = None
    next_day = myt(TOMORROW, 3, 5)
    assert run_step(both, now=next_day) == "completed"
    newer = runs(factory)[-1]
    assert newer.id > run.id
    assert newer.run_date == TOMORROW
    assert newer.wallets_checked == both.checked(4)
    assert late in checks.seen


# --- 完成时刻与日期 --------------------------------------------------------------------


def test_the_finish_time_is_not_before_any_finding(both) -> None:
    ours = [make_tenant(both.factory, wallet=False) for _ in range(2)]
    clock = Clock(NOW, tick=dt.timedelta(seconds=1))

    assert run_step(both, clock=clock) == "completed"

    run = only_run(both.factory)
    found = findings(both.factory, run.id, ours)
    assert len(found) == 2
    assert found[0].checked_at < found[1].checked_at
    assert run.finished_at is not None
    assert run.finished_at >= max(finding.checked_at for finding in found)
    # 提交前重新读的时刻：本次 step 最后一次读墙上时钟。
    assert run.finished_at == clock.readings[-1]


@pytest.mark.parametrize("existing", [False, True], ids=["empty-table", "today-running"])
def test_one_step_has_one_today(both, existing) -> None:
    # 第一次读到马来西亚 23:59:59，之后读到次日 00:00:01：第 0、1、2 步都按第一次的日期判断。
    first = myt(TODAY, 23, 59, 59)
    later = myt(TOMORROW, 0, 0, 1)
    readings = iter([first])

    def clock() -> dt.datetime:
        return next(readings, later)

    make_tenant(both.factory)
    run_id = put_run(both.factory, TODAY, RUNNING) if existing else None

    assert run_step(both, clock=clock) == "completed"

    run = only_run(both.factory)
    if run_id is not None:
        assert run.id == run_id
    else:
        assert run.started_at == first
    assert run.run_date == TODAY
    assert (run.status, run.finished_at) == (COMPLETED, later)


def test_a_future_run_is_retired_and_today_completes(both) -> None:
    factory = both.factory
    make_tenant(factory)
    completed_id = put_run(
        factory, TOMORROW, COMPLETED, finished_at=myt(TOMORROW, 4, 0), wallets_checked=3
    )
    future_id = put_run(factory, TOMORROW + dt.timedelta(days=1), RUNNING)

    assert run_step(both) == "completed"

    future = the_run(factory, future_id)
    assert (future.status, future.finished_at, future.running_slot) == (ABANDONED, NOW, None)
    tomorrow = the_run(factory, completed_id)
    assert (tomorrow.status, tomorrow.finished_at, tomorrow.wallets_checked) == (
        COMPLETED,
        myt(TOMORROW, 4, 0),
        3,
    )
    today = runs(factory)[-1]
    assert (today.run_date, today.status) == (TODAY, COMPLETED)
    assert today.wallets_checked == both.checked(1)


def test_an_early_morning_retire_does_not_block_the_days_completion(both) -> None:
    factory = both.factory
    make_tenant(factory)
    now_a = myt(TODAY, 23, 59, 59)
    now_b = myt(TOMORROW, 0, 0, 1)
    assert start_run(factory, now=now_b) == "started"
    (b_run,) = runs(factory)
    assert retire_stale_runs(factory, now=now_a) == 1

    assert run_step(both, now=myt(TOMORROW, 3, 5)) == "completed"

    old, new = runs(factory)
    assert (old.id, old.status) == (b_run.id, ABANDONED)
    assert new.id > b_run.id
    assert (new.run_date, new.status) == (TOMORROW, COMPLETED)
    assert new.wallets_checked == both.checked(1)


# --- 第 0、1、2 步失败 -----------------------------------------------------------------


def test_a_retire_failure_ends_the_step_before_any_start(both, caplog) -> None:
    factory = both.factory
    make_tenant(factory)
    yesterday_id = put_run(factory, YESTERDAY, RUNNING)

    with caplog.at_level(logging.WARNING, logger=LOGGER), fail_on(factory, UPDATE_RUNS):
        assert run_step(both) == "error"
    assert_one_failure(caplog, "retire", None, "InjectedFailure")
    (old,) = runs(factory)
    assert (old.id, old.status, old.finished_at) == (yesterday_id, RUNNING, None)

    later = NOW + dt.timedelta(minutes=5)
    assert run_step(both, now=later) == "completed"
    old, today = runs(factory)
    assert (old.status, old.finished_at) == (ABANDONED, later)
    assert (today.run_date, today.status) == (TODAY, COMPLETED)


def test_a_start_failure_is_logged_as_start(both, caplog) -> None:
    make_tenant(both.factory)
    insert_runs = re.compile(r"^\s*INSERT\s+INTO\s+\W?wallet_reconciliation_runs\b", re.I)
    with caplog.at_level(logging.WARNING, logger=LOGGER), fail_on(both.factory, insert_runs):
        assert run_step(both) == "error"
    assert_one_failure(caplog, "start", None, "InjectedFailure")
    assert runs(both.factory) == []


def test_a_failure_after_the_run_is_read_logs_its_public_id(both, caplog) -> None:
    make_tenant(both.factory)
    run_id = put_run(both.factory, TODAY, RUNNING)
    public_id = the_run(both.factory, run_id).public_id

    with caplog.at_level(logging.WARNING, logger=LOGGER), fail_on(both.factory, SELECT_CURSOR):
        assert run_step(both) == "error"

    # error_type 是 ClaimError 的 __cause__ 的类名。
    assert_one_failure(caplog, "advance", public_id, "InjectedFailure")
    run = the_run(both.factory, run_id)
    assert_unleased(run)
    assert (run.cursor_tenant_id, run.wallets_checked) == (0, 0)
    assert run_step(both) == "completed"


def test_a_failure_before_the_run_is_read_has_no_run_id(both, caplog) -> None:
    run_id = put_run(both.factory, TODAY, RUNNING)
    with caplog.at_level(logging.WARNING, logger=LOGGER), fail_on(both.factory, SELECT_CLAIM):
        assert run_step(both) == "error"
    assert_one_failure(caplog, "advance", None, "InjectedFailure")
    assert_unleased(the_run(both.factory, run_id))


# --- 第 3 步：扫描、预算、读钱包失败 ---------------------------------------------------------


def test_the_budget_uses_the_injected_monotonic_clock(sqlite_db, monkeypatch) -> None:
    factory = sqlite_db.factory
    first, second, third = (make_tenant(factory) for _ in range(3))
    monotonic = Monotonic(0.0)
    checks = Checks(monkeypatch)

    def fifteen_seconds(_tenant_id: int) -> None:
        monotonic.value += 15

    checks.after = fifteen_seconds

    # 预算 20 秒：第 2 个租户之后（30 秒）超时。
    assert run_step(sqlite_db, monotonic=monotonic) == "progressed"
    run = only_run(factory)
    assert checks.seen == [first, second]
    assert (run.status, run.cursor_tenant_id, run.wallets_checked) == (RUNNING, second, 2)
    assert_unleased(run)

    assert run_step(sqlite_db, monotonic=monotonic) == "completed"
    assert checks.seen == [first, second, third]
    run = only_run(factory)
    assert (run.status, run.cursor_tenant_id, run.wallets_checked) == (COMPLETED, third, 3)


def test_a_wallet_read_failure_keeps_the_cursor_and_restarts_from_it(
    both, monkeypatch, caplog
) -> None:
    factory = both.factory
    ours = [make_tenant(factory, wallet=False) for _ in range(3)]
    run_id = put_run(factory, TODAY, RUNNING)
    public_id = the_run(factory, run_id).public_id
    checks = Checks(monkeypatch)
    calls = itertools.count(1)

    def fail_the_second(_tenant_id: int) -> None:
        if next(calls) == 2:
            raise WalletReadFailure

    checks.before = fail_the_second
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        assert run_step(both) == "error"

    assert_one_failure(caplog, "advance", public_id, "WalletReadFailure")
    run = the_run(factory, run_id)
    assert (run.status, run.cursor_tenant_id, run.wallets_checked, run.wallets_inconsistent) == (
        RUNNING,
        0,
        0,
        0,
    )
    assert_unleased(run)
    assert all_findings(factory) == []
    first_seen = checks.seen[0]

    checks.before = None
    checks.calls.clear()
    assert run_step(both) == "completed"
    # 从第 1 个租户重新开始。
    assert checks.seen[0] == first_seen
    run = the_run(factory, run_id)
    assert run.wallets_checked == both.checked(3)
    assert [finding.tenant_id for finding in findings(factory, run_id, ours)] == ours


def test_no_session_stays_open_across_the_scan(both, monkeypatch) -> None:
    factory = both.factory
    for _ in range(3):
        make_tenant(factory)

    class Tracking:
        def __init__(self) -> None:
            self.open = 0
            self.peak = 0
            self.opened = 0

        def __call__(self) -> Session:
            session = factory()
            self.open += 1
            self.opened += 1
            self.peak = max(self.peak, self.open)
            close = session.close
            closed = [False]

            def tracked_close() -> None:
                if not closed[0]:
                    closed[0] = True
                    self.open -= 1
                close()

            session.close = tracked_close  # type: ignore[method-assign]
            return session

    tracking = Tracking()
    open_at_check: list[int] = []
    checks = Checks(monkeypatch)
    checks.before = lambda _tenant_id: open_at_check.append(tracking.open)

    assert run_step(both, factory=cast(sessionmaker[Session], tracking)) == "completed"

    total = both.checked(3)
    # 每个快照读开始时，打开的只有它自己的会话；任何时刻至多一个会话。
    assert open_at_check == [1] * total
    assert tracking.peak == 1
    assert tracking.open == 0
    # 收尾 1、开始 2（读与插入）、领租约 3（读运行、领取、读游标）、每批租户 id 各 1
    # （最后一批取空）、每个钱包 1、提交 1。
    batches = -(-total // TENANT_BATCH) + 1
    assert tracking.opened == 1 + 2 + 3 + batches + total + 1


# --- 续租与租约 ------------------------------------------------------------------------


def test_a_long_read_renews_the_lease_and_keeps_others_out(both, monkeypatch) -> None:
    factory = both.factory
    tenant_id = make_tenant(factory)
    fund(factory, tenant_id, *range(1, 31))
    clock = Clock(NOW)
    monotonic = Monotonic(0.0)
    checks = Checks(monkeypatch, chunk_rows=3)
    chunks = itertools.count(1)
    seen_by_other: dict[str, Any] = {}

    def thirty_five_seconds_per_chunk() -> None:
        number = next(chunks)
        monotonic.value = 35.0 * number
        clock.now = NOW + dt.timedelta(seconds=35 * number)
        if number == 9:
            # 315 秒：原来的租约（NOW + 290）已过期，续过的还没有。
            moment = clock.now
            with factory() as session:
                seen_by_other["lease_until"] = session.scalar(select(Run.lease_until))
            seen_by_other["result"] = step(factory, clock=Clock(moment), monotonic=Monotonic())

    checks.per_chunk = thirty_five_seconds_per_chunk
    renewals: list[str] = []

    def record(_conn, _cursor, statement, *_args) -> None:
        if UPDATE_RENEW.search(statement):
            renewals.append(statement)

    engine = engine_of(factory)
    event.listen(engine, "before_cursor_execute", record)
    try:
        result = run_step(both, clock=clock, monotonic=monotonic)
    finally:
        event.remove(engine, "before_cursor_execute", record)

    assert all(checks.on_chunk_given)
    # 每 70 秒（超过 60 秒）续一次：70、140、210、280、350 秒。
    assert len(renewals) == 5
    assert seen_by_other == {
        "lease_until": NOW + dt.timedelta(seconds=280 + LEASE_SECONDS),
        "result": "busy",
    }
    # 读完后照常提交（预算早已用完：停在这个租户）。
    assert result == "progressed"
    run = only_run(factory)
    assert run.cursor_tenant_id == tenant_id
    assert run.wallets_checked == both.checked(1)
    assert_unleased(run)
    assert run_step(both, now=clock.now) == "completed"


@pytest.mark.parametrize("then", ["renew", "commit"])
def test_an_expired_lease_taken_over_supersedes_the_first_holder(both, monkeypatch, then) -> None:
    factory = both.factory
    ours = [make_tenant(factory, wallet=False) for _ in range(2)]
    monotonic_a = Monotonic(0.0)
    results: dict[str, str] = {}
    checks = Checks(monkeypatch)

    def b_takes_over(_tenant_id: int) -> None:
        if "B" in results:
            return
        results["B"] = "running"
        # A 暂停、不续租；时钟拨过 lease_until，B 领到租约、跑完。
        later = NOW + dt.timedelta(seconds=LEASE_SECONDS + 1)
        results["B"] = step(factory, clock=Clock(later), monotonic=Monotonic())
        if then == "renew":
            # A 继续：距领租超过 60 秒，核对完这个钱包就续租。
            monotonic_a.value = 61.0

    checks.before = b_takes_over
    renewals: list[str] = []

    def record(_conn, _cursor, statement, *_args) -> None:
        if UPDATE_RENEW.search(statement):
            renewals.append(statement)

    engine = engine_of(factory)
    event.listen(engine, "before_cursor_execute", record)
    try:
        results["A"] = run_step(both, monotonic=monotonic_a)
    finally:
        event.remove(engine, "before_cursor_execute", record)

    assert results == {"A": "superseded", "B": "completed"}
    assert len(renewals) == (1 if then == "renew" else 0)
    run = only_run(factory)
    assert run.status is COMPLETED
    assert run.wallets_checked == both.checked(2)
    assert run.wallets_inconsistent == both.bad(2)
    # 只有 B 的 findings：每个租户一条。
    assert [finding.tenant_id for finding in findings(factory, run.id, ours)] == ours
    assert len(all_findings(factory)) == both.bad(2)


def test_a_cursor_mismatch_supersedes_and_releases_the_lease(both, monkeypatch) -> None:
    factory = both.factory
    make_tenant(factory, wallet=False)
    run_id = put_run(factory, TODAY, RUNNING)
    checks = Checks(monkeypatch)

    def move_the_cursor(_tenant_id: int) -> None:
        if checks.calls[1:]:
            return
        with factory() as session:
            session.execute(update(Run).where(Run.id == run_id).values(cursor_tenant_id=1))
            session.commit()

    checks.before = move_the_cursor
    assert run_step(both) == "superseded"

    run = the_run(factory, run_id)
    assert_unleased(run)
    assert (run.status, run.cursor_tenant_id, run.wallets_checked) == (RUNNING, 1, 0)
    assert all_findings(factory) == []
    # 下一次 step 立即领到租约。
    assert run_step(both) == "completed"


# --- 第 4、5 步：提交失败、释放 --------------------------------------------------------------


def test_a_commit_failure_releases_the_lease(both, caplog) -> None:
    make_tenant(both.factory)
    run_id = put_run(both.factory, TODAY, RUNNING)
    public_id = the_run(both.factory, run_id).public_id

    with caplog.at_level(logging.WARNING, logger=LOGGER), fail_on(both.factory, UPDATE_PROGRESS):
        assert run_step(both) == "error"

    assert_one_failure(caplog, "commit", public_id, "InjectedFailure")
    run = the_run(both.factory, run_id)
    assert_unleased(run)
    assert (run.cursor_tenant_id, run.wallets_checked) == (0, 0)
    assert run_step(both) == "completed"


def test_a_failed_release_only_logs_a_warning(both, caplog) -> None:
    make_tenant(both.factory)
    run_id = put_run(both.factory, TODAY, RUNNING)
    public_id = the_run(both.factory, run_id).public_id

    with (
        caplog.at_level(logging.WARNING, logger=LOGGER),
        fail_on(both.factory, UPDATE_PROGRESS),
        fail_on(both.factory, UPDATE_RELEASE),
    ):
        assert run_step(both) == "error"

    logged = records(caplog)
    assert [(record.levelno, record.getMessage()) for record in logged] == [
        (logging.ERROR, FAILED),
        (logging.WARNING, RELEASE_FAILED),
    ]
    assert extras(logged[0]) == {
        "stage": "commit",
        "run_id": public_id,
        "error_type": "InjectedFailure",
    }
    assert extras(logged[1]) == {"run_id": public_id, "error_type": "InjectedFailure"}
    run = the_run(both.factory, run_id)
    assert run.lease_until == NOW + dt.timedelta(seconds=LEASE_SECONDS)

    # 租约保留到过期：到期那一秒仍领不到，之后被下一次 step 领走。
    assert run_step(both, now=NOW + dt.timedelta(seconds=LEASE_SECONDS)) == "busy"
    assert run_step(both, now=NOW + dt.timedelta(seconds=LEASE_SECONDS + 1)) == "completed"


def test_a_findings_insert_failure_rolls_the_progress_back(both, caplog) -> None:
    factory = both.factory
    make_tenant(factory, wallet=False)
    run_id = put_run(factory, TODAY, RUNNING)
    public_id = the_run(factory, run_id).public_id

    with caplog.at_level(logging.WARNING, logger=LOGGER), fail_on(factory, INSERT_FINDINGS):
        assert run_step(both) == "error"

    # 进度 CAS 与 findings 同一事务（INV-13）：一起回滚。
    assert_one_failure(caplog, "commit", public_id, "InjectedFailure")
    run = the_run(factory, run_id)
    assert (run.status, run.cursor_tenant_id, run.wallets_checked, run.wallets_inconsistent) == (
        RUNNING,
        0,
        0,
        0,
    )
    assert run.running_slot == 1
    assert_unleased(run)
    assert all_findings(factory) == []


# --- Redis/Celery 丢失 ------------------------------------------------------------------


def test_steps_without_the_queue_continue_from_the_stored_cursor(both, monkeypatch) -> None:
    engine = engine_of(both.factory)
    ours = [make_tenant(both.factory) for _ in range(3)]
    monotonic = Monotonic(0.0)
    checks = Checks(monkeypatch)

    def past_the_budget(_tenant_id: int) -> None:
        monotonic.value += 25

    checks.after = past_the_budget
    results = []
    for _ in range(both.checked(3) + 1):
        # 每次换一个新的会话工厂：进程重启、队列清空之后，进度只在库里。
        results.append(run_step(both, factory=create_session_factory(engine), monotonic=monotonic))
        if results[-1] == "completed":
            break

    assert results[-1] == "completed"
    assert set(results[:-1]) <= {"progressed"}
    run = only_run(both.factory)
    assert run.wallets_checked == both.checked(3)
    assert len(checks.seen) == len(set(checks.seen)) == both.checked(3)
    assert set(ours) <= set(checks.seen)


def test_the_celery_task_runs_one_step(both, monkeypatch) -> None:
    make_tenant(both.factory)
    monkeypatch.setattr(reconciliation_task, "_session_factory", lambda: both.factory)

    assert reconciliation_task.step() == "completed"

    run = only_run(both.factory)
    assert (run.status, run.wallets_checked) == (COMPLETED, both.checked(1))


# --- 只在 MySQL ---------------------------------------------------------------------------


def test_a_second_worker_is_busy_while_the_first_scans(mysql_db, monkeypatch) -> None:
    factory = mysql_db.factory
    ours = [make_tenant(factory, wallet=False) for _ in range(3)]
    checks = Checks(monkeypatch)
    other: dict[str, Any] = {}

    def b_steps(tenant_id: int) -> None:
        if tenant_id != ours[0] or other:
            return
        other["result"] = "running"

        def run_b() -> None:
            other["result"] = step(factory, clock=Clock(NOW), monotonic=Monotonic())

        thread = threading.Thread(target=run_b, name="B")
        thread.start()
        thread.join(timeout=30)
        assert not thread.is_alive()

    checks.before = b_steps
    assert run_step(mysql_db) == "completed"

    assert other == {"result": "busy"}
    assert [name for name, _ in checks.calls if name == "B"] == []
    run = only_run(factory)
    assert run.wallets_checked == mysql_db.checked(3)
    found = findings(factory, run.id, ours)
    assert [finding.tenant_id for finding in found] == ours
    assert len(all_findings(factory)) == mysql_db.bad(3)


def test_a_chunk_timeout_is_an_advance_error_and_the_wallet_is_retried(
    mysql_db, monkeypatch, caplog
) -> None:
    factory = mysql_db.factory
    tenant_id = make_tenant(factory)
    fund(factory, tenant_id, *range(1, 11))
    run_id = put_run(factory, TODAY, RUNNING)
    public_id = the_run(factory, run_id).public_id
    checks = Checks(monkeypatch, chunk_rows=3)
    engine = engine_of(factory)
    chunks: list[str] = []
    timeout = OperationalError(
        "SELECT wallet_transactions",
        {},
        Exception(3024, "Query execution was interrupted, maximum statement execution time"),
    )

    def time_out_the_second_chunk(_conn, _cursor, statement, *_args) -> None:
        # 只数这个钱包的块（共用库里别的租户的钱包也会被读）。
        if LEDGER_SELECT.search(statement) and checks.seen[-1:] == [tenant_id]:
            chunks.append(statement)
            if len(chunks) == 2:
                raise timeout

    event.listen(engine, "before_cursor_execute", time_out_the_second_chunk)
    try:
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            assert run_step(mysql_db) == "error"
    finally:
        event.remove(engine, "before_cursor_execute", time_out_the_second_chunk)

    assert_one_failure(caplog, "advance", public_id, "OperationalError")
    assert len(chunks) == 2
    run = the_run(factory, run_id)
    assert (run.cursor_tenant_id, run.wallets_checked) == (0, 0)
    assert_unleased(run)

    before = checks.seen.count(tenant_id)
    assert before == 1
    statements: list[str] = []

    def record(_conn, _cursor, statement, *_args) -> None:
        if LEDGER_SELECT.search(statement):
            statements.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        assert run_step(mysql_db) == "completed"
    finally:
        event.remove(engine, "before_cursor_execute", record)
    # 下一次从同一个钱包重来。
    assert checks.seen.count(tenant_id) == before + 1
    # 每块账本 SELECT 恰好带一次提示，在最外层。
    assert statements
    for statement in statements:
        assert statement.count("MAX_EXECUTION_TIME") == 1, statement
        assert re.match(r"\s*SELECT\s+/\*\+\s*MAX_EXECUTION_TIME\(60000\)\s*\*/", statement)
        assert len(re.findall(r"\bSELECT\b", statement, re.IGNORECASE)) == 1, statement
    run = the_run(factory, run_id)
    assert (run.status, run.wallets_checked) == (COMPLETED, mysql_db.checked(1))


def test_the_largest_balance_is_recorded_exactly(mysql_db) -> None:
    factory = mysql_db.factory
    exact = Decimal("999999999999.99999999")
    # 用例有区分力：这个值经 float 往返会变。
    assert Decimal(float(exact)) != exact
    tenant_id = make_tenant(factory)
    fund(factory, tenant_id, "999999999999.99999998", "0.00000001")
    truncate_ledger(factory)

    assert run_step(mysql_db) == "completed"

    run = only_run(factory)
    (finding,) = findings(factory, run.id, [tenant_id])
    assert LEDGER_MISSING in finding.problem_codes.split(",")
    assert isinstance(finding.balance, Decimal)
    assert finding.balance == exact
    assert format(finding.balance, "f") == "999999999999.99999999"
    assert (finding.wallet_version, finding.ledger_rows) == (2, 0)

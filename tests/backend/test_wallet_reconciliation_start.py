"""Wallet reconciliation steps 0–2 (AIH-TASK-131, design gate #474 v13 §2「核对运行」, §7).

这一层没有 `step`：用例直接调 `retire_stale_runs`、`start_run`、`claim_run`，
按设计的顺序（收尾 → 开始 → 领租约）组合。§7 里属于第 0 到第 2 步的行在这里：

- 两库：开始规则；首次运行在凌晨（开始部分）；隔天；前一天的运行在零点后收尾；
  未来日期的运行（收尾部分）；凌晨空表的收尾不挡住当天（验到新运行开始）；
  非 `running_slot` 冲突的插入失败；领租约后读游标；读到运行之后失败（`claim_run` 部分）；
  租约过期后接手（领取部分）；释放失败只记 warning；
- 只在 MySQL：唯一冲突后锁读、锁等待超时、并发开始（20 次）、跨午夜开始竞态
  （两个线程与屏障）。

SQLite 用 test_usage_billing 的工厂（`create_all` 建表）；MySQL 那一半需要
`BILLING_TEST_DATABASE_URL`，没设时 skip ——
**不要把 skipped 读成 passed**，CI 里不许 skip。

⚠️ SQL 文本只用来挑出语句注入故障或排序，不写死方言字面量；
只有 MySQL 才有的查询放在 MySQL 用例里。
"""

from __future__ import annotations

import contextlib
import datetime as dt
import itertools
import logging
import re
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from typing import Any, cast

import pytest
from sqlalchemy import Engine, create_engine, delete, event, func, insert, select, text, update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session, sessionmaker
from test_usage_billing import TEST_DATABASE_URL, _mysql_factory, _sqlite_factory

from app.core.database import create_session_factory
from app.core.fx_source import KUALA_LUMPUR_OFFSET
from app.models.wallet_reconciliation import (
    ReconciliationRunStatus,
    WalletReconciliationFinding,
    WalletReconciliationRun,
)
from app.services import wallet_reconciliation
from app.services.wallet_reconciliation import (
    LEASE_RENEW_AFTER_SECONDS,
    LEASE_SECONDS,
    STALE_AFTER,
    START_HOUR_MYT,
    STEP_BUDGET_SECONDS,
    STEP_SECONDS,
    TENANT_BATCH,
    ClaimedRun,
    ClaimError,
    claim_run,
    release_lease,
    retire_stale_runs,
    start_run,
)

Run = WalletReconciliationRun
RUNNING = ReconciliationRunStatus.RUNNING
COMPLETED = ReconciliationRunStatus.COMPLETED
ABANDONED = ReconciliationRunStatus.ABANDONED

TODAY = dt.date(2026, 10, 29)
YESTERDAY = TODAY - dt.timedelta(days=1)
TOMORROW = TODAY + dt.timedelta(days=1)
ZERO_UUID = "00000000-0000-0000-0000-000000000000"

LOGGER = "app.services.wallet_reconciliation"
RELEASE_FAILED = "wallet_reconciliation_lease_release_failed"
_STANDARD_RECORD_KEYS = set(logging.makeLogRecord({}).__dict__) | {"message", "asctime"}

INSERT_RUNS = re.compile(r"^\s*INSERT\s+INTO\s+\W?wallet_reconciliation_runs\b", re.IGNORECASE)
UPDATE_RUNS = re.compile(r"^\s*UPDATE\s+\W?wallet_reconciliation_runs\b", re.IGNORECASE)
SELECT_RUNS = re.compile(r"^\s*SELECT\b.*\bwallet_reconciliation_runs\b", re.IGNORECASE | re.DOTALL)
SELECT_CURSOR = re.compile(r"^\s*SELECT\b.*\bcursor_tenant_id\b", re.IGNORECASE | re.DOTALL)

_NUMBERS = itertools.count(1)


def myt(day: dt.date, hour: int, minute: int = 0, second: int = 0) -> dt.datetime:
    """Naive UTC for a Kuala Lumpur wall-clock time."""
    return dt.datetime.combine(day, dt.time(hour, minute, second)) - KUALA_LUMPUR_OFFSET


def sample_uuid() -> str:
    """Mostly-zero placeholder uuids (secret-scan); all zeros is kept for one case."""
    return f"00000000-0000-0000-0000-{next(_NUMBERS):012d}"


# --- fixtures --------------------------------------------------------------------------


def _wipe(factory: sessionmaker[Session]) -> None:
    with factory() as session:
        session.execute(delete(WalletReconciliationFinding))
        session.execute(delete(Run))
        session.commit()


def _mysql(monkeypatch: pytest.MonkeyPatch) -> Iterator[sessionmaker[Session]]:
    """test_usage_billing 的 MySQL 工厂，另外在前后清空两张核对表（库是共用的）。"""
    generator = _mysql_factory(monkeypatch)
    factory = next(generator)
    _wipe(factory)
    try:
        yield factory
    finally:
        _wipe(factory)
        generator.close()


@pytest.fixture(params=["sqlite", "mysql"])
def both(request, monkeypatch) -> Iterator[sessionmaker[Session]]:
    """The same cases on SQLite and on a real MySQL."""
    if request.param == "mysql":
        yield from _mysql(monkeypatch)
        return
    yield from _sqlite_factory()


@pytest.fixture
def mysql_factory(monkeypatch) -> Iterator[sessionmaker[Session]]:
    yield from _mysql(monkeypatch)


def engine_of(factory: sessionmaker[Session]) -> Engine:
    engine = factory.kw["bind"]
    assert isinstance(engine, Engine)
    return engine


# --- helpers ---------------------------------------------------------------------------


def run_values(run_date: dt.date, status: ReconciliationRunStatus, **overrides: Any) -> dict:
    started = myt(run_date, 3, 5)
    values: dict[str, Any] = {
        "public_id": sample_uuid(),
        "run_date": run_date,
        "status": status,
        "started_at": started,
        "finished_at": None if status is RUNNING else started + dt.timedelta(hours=1),
        "cursor_tenant_id": 0,
        "wallets_checked": 0,
        "wallets_inconsistent": 0,
        "lease_owner": None,
        "lease_until": None,
        "running_slot": 1 if status is RUNNING else None,
    }
    values.update(overrides)
    return values


def put_run(
    factory: sessionmaker[Session], run_date: dt.date, status: ReconciliationRunStatus, **overrides
) -> int:
    with factory() as session:
        run_id = session.execute(
            insert(Run).values(**run_values(run_date, status, **overrides))
        ).inserted_primary_key[0]
        session.commit()
    return int(run_id)


def runs(factory: sessionmaker[Session]) -> list[Run]:
    with factory() as session:
        return list(session.scalars(select(Run).order_by(Run.id)))


def the_run(factory: sessionmaker[Session], run_id: int) -> Run:
    with factory() as session:
        return session.scalars(select(Run).where(Run.id == run_id)).one()


def running(factory: sessionmaker[Session]) -> list[Run]:
    return [run for run in runs(factory) if run.status is RUNNING]


def steps(factory: sessionmaker[Session], now: dt.datetime) -> tuple[int, str, Any]:
    """Steps 0–2 in the design's order, with one `now` (what AIH-TASK-132's `step` will do)."""
    retired = retire_stale_runs(factory, now=now)
    started = start_run(factory, now=now)
    return retired, started, claim_run(factory, now=now)


def complete(factory: sessionmaker[Session], run_id: int, finished_at: dt.datetime) -> None:
    """Stand-in for step 4 reaching the end (AIH-TASK-132)."""
    with factory() as session:
        session.execute(
            update(Run)
            .where(Run.id == run_id)
            .values(
                status=COMPLETED,
                finished_at=finished_at,
                running_slot=None,
                lease_owner=None,
                lease_until=None,
            )
        )
        session.commit()


class InjectedFailure(Exception):
    pass


@contextlib.contextmanager
def fail_on(
    factory: sessionmaker[Session], pattern: re.Pattern[str], *, skip: int = 0
) -> Iterator[InjectedFailure]:
    """Raise from the driver call of every matching statement after the first `skip`."""
    engine = engine_of(factory)
    failure = InjectedFailure("injected")
    matched = [0]

    def before(_conn, _cursor, statement, _parameters, _context, _executemany) -> None:
        if pattern.search(statement):
            matched[0] += 1
            if matched[0] > skip:
                raise failure

    event.listen(engine, "before_cursor_execute", before)
    try:
        yield failure
    finally:
        event.remove(engine, "before_cursor_execute", before)


class BeforeSession:
    """A session factory that runs `hook` just before handing out the `nth` session."""

    def __init__(self, factory: sessionmaker[Session], nth: int, hook: Callable[[], None]):
        self._factory = factory
        self._nth = nth
        self._hook = hook
        self._calls = 0

    def __call__(self) -> Session:
        self._calls += 1
        if self._calls == self._nth:
            self._hook()
        return self._factory()

    def as_factory(self) -> sessionmaker[Session]:
        return cast(sessionmaker[Session], self)


def extras(record: logging.LogRecord) -> dict[str, Any]:
    return {k: v for k, v in record.__dict__.items() if k not in _STANDARD_RECORD_KEYS}


# --- constants -------------------------------------------------------------------------


def test_the_constants_are_the_designs() -> None:
    assert STEP_SECONDS == 300
    assert STEP_BUDGET_SECONDS == 20
    assert TENANT_BATCH == 50
    assert START_HOUR_MYT == 3
    assert STALE_AFTER == dt.timedelta(hours=30)
    assert LEASE_SECONDS == 290
    assert LEASE_RENEW_AFTER_SECONDS == 60


# --- 开始规则 --------------------------------------------------------------------------


@pytest.mark.parametrize("hour", [0, 2, 3, 23])
def test_an_empty_table_starts_at_any_time(both, hour) -> None:
    now = myt(TODAY, hour, 30)
    assert start_run(both, now=now) == "started"
    (run,) = runs(both)
    assert run.status is RUNNING
    assert run.run_date == TODAY
    assert run.started_at == now
    assert run.finished_at is None
    assert (run.cursor_tenant_id, run.wallets_checked, run.wallets_inconsistent) == (0, 0, 0)
    assert run.running_slot == 1
    assert run.lease_owner is None and run.lease_until is None
    assert uuid.UUID(run.public_id).version == 4


def test_a_non_empty_table_waits_for_three_in_the_morning(both) -> None:
    put_run(both, YESTERDAY, COMPLETED)
    assert start_run(both, now=myt(TODAY, 2, 59, 59)) == "not_due"
    assert len(runs(both)) == 1
    assert start_run(both, now=myt(TODAY, 3, 0)) == "started"
    assert [run.run_date for run in running(both)] == [TODAY]


def test_a_completed_run_today_stops_another_start(both) -> None:
    put_run(both, TODAY, COMPLETED)
    assert start_run(both, now=myt(TODAY, 10, 0)) == "not_due"
    assert len(runs(both)) == 1


def test_an_abandoned_run_today_does_not_stop_another_start(both) -> None:
    put_run(both, TODAY, ABANDONED)
    assert start_run(both, now=myt(TODAY, 10, 0)) == "started"
    assert [run.status for run in runs(both)] == [ABANDONED, RUNNING]


@pytest.mark.parametrize("run_date", [YESTERDAY, TODAY, TOMORROW])
@pytest.mark.parametrize("hour", [1, 10])
def test_any_running_run_stops_a_start(both, run_date, hour) -> None:
    # 不论日期：哪怕今天已有 COMPLETED 或时间未到，存在 RUNNING 就是 already_running。
    put_run(both, TODAY, COMPLETED)
    put_run(both, run_date, RUNNING)
    assert start_run(both, now=myt(TODAY, hour, 0)) == "already_running"
    assert len(runs(both)) == 2


def test_a_running_row_inserted_after_the_check_is_found_by_the_locked_read(both) -> None:
    # 插入前的检查没看到；插入时 running_slot 冲突（IntegrityError），
    # 回滚后锁读读到它。
    other: list[int] = []
    factory = BeforeSession(both, 2, lambda: other.append(put_run(both, TODAY, RUNNING)))
    assert start_run(factory.as_factory(), now=myt(TODAY, 3, 5)) == "already_running"
    assert [run.id for run in runs(both)] == other


# --- 首次运行在凌晨（开始部分） ----------------------------------------------------------


def test_a_first_run_after_midnight_and_the_next_start_after_three(both) -> None:
    first_now = myt(TODAY, 0, 0, 30)
    retired, started, claimed = steps(both, first_now)
    assert (retired, started) == (0, "started")
    assert isinstance(claimed, ClaimedRun)
    complete(both, claimed.run_id, first_now + dt.timedelta(minutes=5))

    assert steps(both, myt(TOMORROW, 2, 59)) == (0, "not_due", "idle")
    retired, started, claimed = steps(both, myt(TOMORROW, 3, 5))
    assert (retired, started) == (0, "started")
    assert isinstance(claimed, ClaimedRun)
    assert the_run(both, claimed.run_id).run_date == TOMORROW


# --- 隔天、零点后收尾、未来日期 ----------------------------------------------------------


def test_the_next_day_abandons_yesterday_and_starts_today_from_zero(both) -> None:
    yesterday_id = put_run(
        both,
        YESTERDAY,
        RUNNING,
        cursor_tenant_id=40,
        wallets_checked=40,
        lease_owner=sample_uuid(),
        lease_until=myt(YESTERDAY, 23, 0),
    )
    now = myt(TODAY, 3, 5)
    retired, started, claimed = steps(both, now)
    assert (retired, started) == (1, "started")

    old = the_run(both, yesterday_id)
    assert old.status is ABANDONED
    assert old.finished_at == now
    assert (old.running_slot, old.lease_owner, old.lease_until) == (None, None, None)
    # 收尾只改状态，不动进度。
    assert (old.cursor_tenant_id, old.wallets_checked) == (40, 40)

    assert isinstance(claimed, ClaimedRun)
    assert claimed.run_id != yesterday_id
    assert claimed.old_cursor == 0
    assert the_run(both, claimed.run_id).run_date == TODAY


def test_yesterdays_run_is_abandoned_after_midnight_and_nothing_starts_before_three(both) -> None:
    yesterday_id = put_run(both, YESTERDAY, RUNNING)
    now = myt(TODAY, 0, 5)
    assert steps(both, now) == (1, "not_due", "idle")
    old = the_run(both, yesterday_id)
    assert (old.status, old.finished_at, old.running_slot) == (ABANDONED, now, None)
    assert len(runs(both)) == 1


def test_a_future_running_run_is_abandoned_and_today_starts(both) -> None:
    completed_id = put_run(
        both, TOMORROW, COMPLETED, finished_at=myt(TOMORROW, 4, 0), wallets_checked=3
    )
    future_id = put_run(both, TOMORROW + dt.timedelta(days=1), RUNNING)
    now = myt(TODAY, 3, 5)
    retired, started, claimed = steps(both, now)
    assert (retired, started) == (1, "started")

    future = the_run(both, future_id)
    assert (future.status, future.finished_at, future.running_slot) == (ABANDONED, now, None)
    tomorrow = the_run(both, completed_id)
    assert (tomorrow.status, tomorrow.finished_at) == (COMPLETED, myt(TOMORROW, 4, 0))
    assert isinstance(claimed, ClaimedRun)
    assert the_run(both, claimed.run_id).run_date == TODAY


def test_a_running_run_of_today_is_not_retired(both) -> None:
    today_id = put_run(both, TODAY, RUNNING)
    assert retire_stale_runs(both, now=myt(TODAY, 23, 59, 59)) == 0
    assert the_run(both, today_id).status is RUNNING


def test_a_future_running_run_is_not_claimed(both) -> None:
    put_run(both, TOMORROW, RUNNING)
    assert claim_run(both, now=myt(TODAY, 3, 5)) == "idle"


def test_an_early_morning_retire_does_not_block_the_day(both) -> None:
    # 表为空；B（次日 00:00:01）开始次日的运行，A（23:59:59）的第 0 步把它收尾；
    # 03:00 后照常开始。
    now_a = myt(TODAY, 23, 59, 59)
    now_b = myt(TOMORROW, 0, 0, 1)
    assert retire_stale_runs(both, now=now_b) == 0
    assert start_run(both, now=now_b) == "started"
    (b_run,) = runs(both)
    assert b_run.run_date == TOMORROW

    assert retire_stale_runs(both, now=now_a) == 1
    assert the_run(both, b_run.id).status is ABANDONED

    later = myt(TOMORROW, 3, 5)
    assert retire_stale_runs(both, now=later) == 0
    assert start_run(both, now=later) == "started"
    (new_run,) = running(both)
    assert new_run.run_date == TOMORROW
    assert new_run.id > b_run.id


def test_a_retire_failure_rolls_back_and_raises_unchanged(both) -> None:
    yesterday_id = put_run(both, YESTERDAY, RUNNING)
    with fail_on(both, UPDATE_RUNS) as failure, pytest.raises(InjectedFailure) as raised:
        retire_stale_runs(both, now=myt(TODAY, 3, 5))
    assert raised.value is failure
    old = the_run(both, yesterday_id)
    assert (old.status, old.finished_at, old.running_slot) == (RUNNING, None, 1)


# --- 非 running_slot 冲突的插入失败 --------------------------------------------------------


def test_an_insert_failure_that_is_not_the_running_slot_is_raised(both, monkeypatch) -> None:
    # 别的约束失败（这里是 public_id 唯一冲突），表里没有任何 RUNNING：
    # 锁读读不到，原样抛出。
    put_run(both, YESTERDAY, ABANDONED, public_id=ZERO_UUID)
    monkeypatch.setattr(wallet_reconciliation.uuid, "uuid4", lambda: uuid.UUID(ZERO_UUID))
    with pytest.raises(IntegrityError):
        start_run(both, now=myt(TODAY, 3, 5))
    assert [run.status for run in runs(both)] == [ABANDONED]


def test_an_insert_failure_of_another_kind_is_raised(both) -> None:
    with fail_on(both, INSERT_RUNS) as failure, pytest.raises(InjectedFailure) as raised:
        start_run(both, now=myt(TODAY, 3, 5))
    assert raised.value is failure
    assert runs(both) == []


# --- 领租约 ----------------------------------------------------------------------------


def test_claiming_an_empty_table_is_idle(both) -> None:
    assert claim_run(both, now=myt(TODAY, 3, 5)) == "idle"


def test_claiming_takes_a_fresh_lease_and_reads_the_cursor(both) -> None:
    run_id = put_run(both, TODAY, RUNNING, cursor_tenant_id=12)
    now = myt(TODAY, 3, 5)
    claimed = claim_run(both, now=now)
    assert isinstance(claimed, ClaimedRun)
    assert claimed._fields == ("run_id", "public_id", "token", "old_cursor")
    run = the_run(both, run_id)
    assert claimed == (run_id, run.public_id, run.lease_owner, 12)
    assert uuid.UUID(claimed.token).version == 4
    assert run.lease_until == now + dt.timedelta(seconds=LEASE_SECONDS)


def test_the_cursor_is_read_after_the_lease(both) -> None:
    # B 在 A 提交之前已读到运行，A 提交一段（游标前进、租约释放）之后 B 才领租约。
    run_id = put_run(both, TODAY, RUNNING)
    now = myt(TODAY, 3, 5)
    a = claim_run(both, now=now)
    assert isinstance(a, ClaimedRun)

    def a_commits_a_segment() -> None:
        # 第 4 步的进度 CAS（AIH-TASK-132）的替身：带租约与游标，提交即释放租约。
        with both() as session:
            result = session.execute(
                update(Run)
                .where(
                    Run.id == a.run_id,
                    Run.status == RUNNING,
                    Run.cursor_tenant_id == a.old_cursor,
                    Run.lease_owner == a.token,
                )
                .values(cursor_tenant_id=7, wallets_checked=7, lease_owner=None, lease_until=None)
            )
            assert result.rowcount == 1
            session.commit()

    b = claim_run(BeforeSession(both, 2, a_commits_a_segment).as_factory(), now=now)
    assert isinstance(b, ClaimedRun)
    assert (b.run_id, b.old_cursor) == (run_id, 7)
    assert b.token != a.token
    assert the_run(both, run_id).lease_owner == b.token


def test_an_expired_lease_is_taken_over(both) -> None:
    run_id = put_run(both, TODAY, RUNNING)
    t = myt(TODAY, 3, 5)
    a = claim_run(both, now=t)
    assert isinstance(a, ClaimedRun)
    assert claim_run(both, now=t + dt.timedelta(seconds=60)) == "busy"
    # lease_until < now 才过期：恰好到期的那一秒还领不到。
    assert claim_run(both, now=t + dt.timedelta(seconds=LEASE_SECONDS)) == "busy"
    assert the_run(both, run_id).lease_owner == a.token

    later = t + dt.timedelta(seconds=LEASE_SECONDS + 1)
    b = claim_run(both, now=later)
    assert isinstance(b, ClaimedRun)
    assert b.token != a.token
    run = the_run(both, run_id)
    assert run.lease_owner == b.token
    assert run.lease_until == later + dt.timedelta(seconds=LEASE_SECONDS)


def test_a_cursor_read_failure_releases_the_lease_and_raises_claim_error(both) -> None:
    run_id = put_run(both, TODAY, RUNNING)
    public_id = the_run(both, run_id).public_id
    with fail_on(both, SELECT_CURSOR) as failure, pytest.raises(ClaimError) as raised:
        claim_run(both, now=myt(TODAY, 3, 5))
    assert raised.value.public_id == public_id
    assert raised.value.__cause__ is failure
    run = the_run(both, run_id)
    assert (run.lease_owner, run.lease_until) == (None, None)
    # 已释放：同一时刻的下一次领取立即领到。
    assert isinstance(claim_run(both, now=myt(TODAY, 3, 5)), ClaimedRun)


def test_a_lease_update_failure_raises_claim_error_without_a_lease(both) -> None:
    run_id = put_run(both, TODAY, RUNNING)
    public_id = the_run(both, run_id).public_id
    with fail_on(both, UPDATE_RUNS) as failure, pytest.raises(ClaimError) as raised:
        claim_run(both, now=myt(TODAY, 3, 5))
    assert raised.value.public_id == public_id
    assert raised.value.__cause__ is failure
    run = the_run(both, run_id)
    assert (run.lease_owner, run.lease_until) == (None, None)


def test_a_failure_before_the_run_is_read_is_raised_unchanged(both) -> None:
    put_run(both, TODAY, RUNNING)
    with fail_on(both, SELECT_RUNS) as failure, pytest.raises(InjectedFailure) as raised:
        claim_run(both, now=myt(TODAY, 3, 5))
    assert raised.value is failure


# --- 释放 ------------------------------------------------------------------------------


def test_release_clears_only_its_own_lease(both) -> None:
    run_id = put_run(both, TODAY, RUNNING)
    claimed = claim_run(both, now=myt(TODAY, 3, 5))
    assert isinstance(claimed, ClaimedRun)

    release_lease(both, run_id=run_id, public_id=claimed.public_id, token=sample_uuid())
    assert the_run(both, run_id).lease_owner == claimed.token

    release_lease(both, run_id=run_id, public_id=claimed.public_id, token=claimed.token)
    run = the_run(both, run_id)
    assert (run.lease_owner, run.lease_until) == (None, None)


def test_a_release_failure_only_logs_a_warning(both, caplog) -> None:
    run_id = put_run(both, TODAY, RUNNING)
    claimed = claim_run(both, now=myt(TODAY, 3, 5))
    assert isinstance(claimed, ClaimedRun)
    with caplog.at_level(logging.WARNING, logger=LOGGER), fail_on(both, UPDATE_RUNS):
        release_lease(both, run_id=run_id, public_id=claimed.public_id, token=claimed.token)

    records = [record for record in caplog.records if record.name == LOGGER]
    assert [(record.levelno, record.getMessage()) for record in records] == [
        (logging.WARNING, RELEASE_FAILED)
    ]
    assert extras(records[0]) == {"run_id": claimed.public_id, "error_type": "InjectedFailure"}
    # 租约保留到过期。
    assert the_run(both, run_id).lease_owner == claimed.token


def test_a_failed_release_inside_claim_still_raises_claim_error(both, caplog) -> None:
    run_id = put_run(both, TODAY, RUNNING)
    public_id = the_run(both, run_id).public_id
    with (
        caplog.at_level(logging.WARNING, logger=LOGGER),
        fail_on(both, SELECT_CURSOR) as failure,
        fail_on(both, UPDATE_RUNS, skip=1),
        pytest.raises(ClaimError) as raised,
    ):
        claim_run(both, now=myt(TODAY, 3, 5))
    assert raised.value.public_id == public_id
    assert raised.value.__cause__ is failure
    records = [record for record in caplog.records if record.name == LOGGER]
    assert [record.getMessage() for record in records] == [RELEASE_FAILED]
    assert extras(records[0]) == {"run_id": public_id, "error_type": "InjectedFailure"}


# --- MySQL：锁读、锁等待超时、并发 ----------------------------------------------------------

# ⚠️ 不用 information_schema.INNODB_TRX：它是不满 0.1 秒不刷新的缓存，而且不分库（见
# test_session_revocation_concurrency.py）。这里只数本库运行表上的锁等待。
_LOCK_WAITS = text(
    "SELECT COUNT(*) FROM performance_schema.data_lock_waits AS w"
    " JOIN performance_schema.data_locks AS l"
    " ON l.ENGINE = w.ENGINE AND l.ENGINE_LOCK_ID = w.REQUESTING_ENGINE_LOCK_ID"
    " WHERE l.OBJECT_SCHEMA = DATABASE() AND l.OBJECT_NAME = :table_name"
).bindparams(table_name=Run.__tablename__)
_ER_DUP_ENTRY = 1062
_ER_LOCK_WAIT_TIMEOUT = 1205


def _wait_for_a_lock_wait(factory: sessionmaker[Session]) -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        with factory() as session:
            if session.scalar(_LOCK_WAITS):
                return
        time.sleep(0.05)
    raise AssertionError("no transaction ever waited for the lock")


@contextlib.contextmanager
def driver_errors(engine: Engine) -> Iterator[list[Any]]:
    """The MySQL error numbers the driver raised on `engine`."""
    seen: list[Any] = []

    def on_error(context) -> None:
        arguments = getattr(context.original_exception, "args", ())
        seen.append(arguments[0] if arguments else None)

    event.listen(engine, "handle_error", on_error)
    try:
        yield seen
    finally:
        event.remove(engine, "handle_error", on_error)


@contextlib.contextmanager
def running_watch(factory: sessionmaker[Session]) -> Iterator[list[int]]:
    """Count the RUNNING rows over and over while the block runs."""
    seen: list[int] = []
    stop = threading.Event()

    def watch() -> None:
        while not stop.is_set():
            with factory() as session:
                seen.append(
                    session.scalar(
                        select(func.count()).select_from(Run).where(Run.status == RUNNING)
                    )
                    or 0
                )

    watcher = threading.Thread(target=watch, name="watch")
    watcher.start()
    try:
        yield seen
    finally:
        stop.set()
        watcher.join()
        with factory() as session:
            seen.append(
                session.scalar(select(func.count()).select_from(Run).where(Run.status == RUNNING))
                or 0
            )


def race(calls: dict[str, Callable[[], Any]]) -> tuple[dict[str, Any], dict[str, Exception]]:
    """Run the calls in named threads that leave a barrier together."""
    barrier = threading.Barrier(len(calls))
    results: dict[str, Any] = {}
    errors: dict[str, Exception] = {}

    def run(name: str, call: Callable[[], Any]) -> None:
        try:
            barrier.wait(timeout=10)
            results[name] = call()
        except Exception as error:  # 汇报给主线程断言
            errors[name] = error

    threads = [
        threading.Thread(target=run, args=(name, call), name=name) for name, call in calls.items()
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert not any(thread.is_alive() for thread in threads)
    return results, errors


def test_a_unique_conflict_is_followed_by_a_locked_read(mysql_factory) -> None:
    # A 插入 running_slot = 1 后不提交；B 的插入等 A 的锁；
    # A 提交后 B 唯一冲突、回滚、锁读读到 A。
    now = myt(TODAY, 3, 5)
    holder = mysql_factory()
    try:
        holder.execute(insert(Run).values(**run_values(TODAY, RUNNING, started_at=now)))
        results: dict[str, Any] = {}
        errors: dict[str, Exception] = {}

        def b_starts() -> None:
            try:
                results["b"] = start_run(mysql_factory, now=now)
            except Exception as error:
                errors["b"] = error

        with driver_errors(engine_of(mysql_factory)) as seen:
            b = threading.Thread(target=b_starts, name="B")
            b.start()
            _wait_for_a_lock_wait(mysql_factory)
            holder.commit()
            b.join(timeout=30)
        assert not b.is_alive()
        assert errors == {}
        assert results == {"b": "already_running"}
        assert _ER_DUP_ENTRY in seen
    finally:
        holder.rollback()
        holder.close()
    assert len(runs(mysql_factory)) == 1


def test_a_lock_wait_timeout_is_raised_and_the_next_start_sees_the_run(mysql_factory) -> None:
    impatient = create_engine(TEST_DATABASE_URL)

    @event.listens_for(impatient, "connect")
    def _short_lock_wait(dbapi_connection, _record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("SET SESSION innodb_lock_wait_timeout = 1")
        cursor.close()

    b_factory = create_session_factory(impatient)
    now = myt(TODAY, 3, 5)
    holder = mysql_factory()
    try:
        holder.execute(insert(Run).values(**run_values(TODAY, RUNNING, started_at=now)))
        with driver_errors(impatient) as seen, pytest.raises(OperationalError) as raised:
            start_run(b_factory, now=now)
        # 插入超时、回滚，锁读同样超时：抛出，不当成已有 RUNNING。
        assert raised.value.orig.args[0] == _ER_LOCK_WAIT_TIMEOUT
        assert seen == [_ER_LOCK_WAIT_TIMEOUT, _ER_LOCK_WAIT_TIMEOUT]
        assert runs(mysql_factory) == []
        holder.commit()
        assert start_run(b_factory, now=now) == "already_running"
    finally:
        holder.rollback()
        holder.close()
        impatient.dispose()
    assert len(runs(mysql_factory)) == 1


def test_concurrent_starts_leave_exactly_one_running_run(mysql_factory) -> None:
    now = myt(TODAY, 3, 5)
    for _ in range(20):
        _wipe(mysql_factory)
        yesterday_id = put_run(mysql_factory, YESTERDAY, RUNNING)
        with running_watch(mysql_factory) as seen:
            results, errors = race(
                {"A": lambda: steps(mysql_factory, now), "B": lambda: steps(mysql_factory, now)}
            )
        # 其中一方可能遇到 1213 死锁或唯一冲突，都被吸收为 already_running，不抛出。
        assert errors == {}
        assert max(seen) <= 1
        assert sum(result[0] for result in results.values()) == 1
        assert sorted(result[1] for result in results.values()) == ["already_running", "started"]
        claims = [result[2] for result in results.values()]
        assert sum(isinstance(claim, ClaimedRun) for claim in claims) == 1
        assert [claim for claim in claims if not isinstance(claim, ClaimedRun)] == ["busy"]

        assert the_run(mysql_factory, yesterday_id).status is ABANDONED
        (today,) = running(mysql_factory)
        assert today.run_date == TODAY


@contextlib.contextmanager
def inserts_in_order(engine: Engine, first: str, second: str) -> Iterator[None]:
    """Both threads pass the start check, then `first` inserts before `second` does."""
    both_checked = threading.Barrier(2)
    first_inserted = threading.Event()

    def before(_conn, _cursor, statement, _parameters, _context, _executemany) -> None:
        name = threading.current_thread().name
        if name in (first, second) and INSERT_RUNS.search(statement):
            both_checked.wait(timeout=10)
            if name == second:
                assert first_inserted.wait(timeout=10)

    def after(_conn, _cursor, statement, _parameters, _context, _executemany) -> None:
        if threading.current_thread().name == first and INSERT_RUNS.search(statement):
            first_inserted.set()

    event.listen(engine, "before_cursor_execute", before)
    event.listen(engine, "after_cursor_execute", after)
    try:
        yield
    finally:
        event.remove(engine, "before_cursor_execute", before)
        event.remove(engine, "after_cursor_execute", after)


@pytest.mark.parametrize("first", ["A", "B"])
def test_a_start_race_across_midnight_leaves_one_running_run(mysql_factory, first) -> None:
    # 表为空；A 读到马来西亚 23:59:59，B 读到次日 00:00:01。
    now_a = myt(TODAY, 23, 59, 59)
    now_b = myt(TOMORROW, 0, 0, 1)
    second = "B" if first == "A" else "A"
    with (
        running_watch(mysql_factory) as seen,
        inserts_in_order(engine_of(mysql_factory), first, second),
    ):
        results, errors = race(
            {"A": lambda: steps(mysql_factory, now_a), "B": lambda: steps(mysql_factory, now_b)}
        )
    assert errors == {}
    assert max(seen) <= 1
    assert results[first][:2] == (0, "started")
    assert results[second][:2] == (0, "already_running")
    claims = [results["A"][2], results["B"][2]]
    assert sum(isinstance(claim, ClaimedRun) for claim in claims) == 1

    (run,) = runs(mysql_factory)
    assert run.status is RUNNING
    assert run.run_date == (TODAY if first == "A" else TOMORROW)
    if first == "B":
        # A 的今天早于这条运行的日期：A 读不到它。
        assert results["A"][2] == "idle"
        return

    # A 先插入：下一个周期（马来西亚 00:05）收尾 A 那条，表非空且早于 03:00，不开始。
    assert steps(mysql_factory, myt(TOMORROW, 0, 5)) == (1, "not_due", "idle")
    old = the_run(mysql_factory, run.id)
    assert (old.status, old.running_slot, old.lease_owner) == (ABANDONED, None, None)
    retired, started, claimed = steps(mysql_factory, myt(TOMORROW, 3, 5))
    assert (retired, started) == (0, "started")
    assert isinstance(claimed, ClaimedRun)
    assert claimed.run_id > run.id
    assert the_run(mysql_factory, claimed.run_id).run_date == TOMORROW

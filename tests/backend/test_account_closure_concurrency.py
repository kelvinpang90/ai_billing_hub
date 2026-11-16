"""Account closure under concurrency, on a real MySQL only (design gate #707 v4 §7, AIH-TASK-212).

§7 的三行并发：

- **关户 vs 计费**（v2 起的写法）：关户 A 经注入的钩子停在 `closure_blockers` 之后、写入之前
  （此时 A 的四类计数都是 0、快照已建立）；这之后才插入一条该租户的 `RECEIVED` 用量事件，由 B
  认领并计费：② 读到 `DISABLED`（A 未提交），⑧ 等钱包锁；放行 A 提交后 B 得到
  `FAILED_FINAL` / `ACCOUNT_CLOSED`，没有账本行，余额仍 0；
- **关户 vs 调账**，两种先后：调账先，关户锁内读到非零余额 → 409；
  关户先，调账 409 `ACCOUNT_CLOSED`；
- **关户 vs 启用**：`DISABLED → ENABLED` 与关户，两种先后各一次、再同时起跑一次：
  恰好一个成功、另一个 409，不出现死锁。

「对方确实在等行锁」一律查 `performance_schema.data_lock_waits`
（按 `DATABASE()` 与表名过滤，写法同 test_session_revocation_concurrency.py 的
`_WAITING_ON_TABLE`），不查 INNODB_TRX。每个参与方用自己的引擎（池里只有一条连接，
所以它的 `CONNECTION_ID()` 是确定的）。业务时间一律注入；轮询等锁的短暂停顿只用于
观察数据库，不参与业务时间。

需要 `BILLING_TEST_DATABASE_URL` 指向一个**可以被清空的**库，没设时 skip ——
**不要把 skipped 读成 passed**，CI 设了它并把 skipped 判成失败。
世界沿用 test_usage_billing.py 的 `World`。
"""

from __future__ import annotations

import datetime as dt
import os
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import Connection, Engine, create_engine, event, func, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker
from test_usage_billing import MAX_ATTEMPTS, Tenancy, _mysql_factory
from test_usage_billing import World as BillingWorld

from app.core.database import create_session_factory
from app.models.auth import AuditAction, AuditLog
from app.models.tenancy import AccountStatus, Tenant
from app.models.usage import UsageEvent, UsageEventStatus
from app.models.wallet import TransactionType, Wallet, WalletTransaction
from app.services import account_closure, usage_billing
from app.services.account_closure import (
    AccountClosureBalanceNotZero,
    ClosureBlockers,
    close_account,
)
from app.services.account_status import AccountStatusTransitionInvalid, change_account_status
from app.services.auth import RequestContext
from app.services.project_integration import AccountClosed
from app.services.usage_billing import Outcome
from app.services.wallet_adjustments import post_adjustment

CONTEXT = RequestContext(ip_address="203.0.113.22", user_agent="account-closure-concurrency")
REASON = "Closure ticket CL-0002: refunded and settled"
CLOSE_AT = dt.datetime(2026, 10, 6, 9, 0, 0)

NOTHING = ClosureBlockers(
    usage_events=0, ai_usage=0, payments=0, payment_events=0, payment_events_truncated=False
)

# 暂停点最多停这么久；到点还没人放行，被停住的那一方报错退出。
GATE_TIMEOUT_SECONDS = 15.0
# 「对方在等行锁」的轮询：间隔 50 毫秒、最多 10 秒。
LOCK_POLL_INTERVAL_SECONDS = 0.05
LOCK_POLL_LIMIT_SECONDS = 10.0
# 本文件的连接都把 innodb_lock_wait_timeout 调到这个值：真卡在锁上时以 1205 报错收场。
LOCK_WAIT_TIMEOUT_SECONDS = 20
# 等一个参与方结束：比锁等待超时长，锁等待超时会先以错误的形式出现。
JOIN_TIMEOUT_SECONDS = 40.0

_WAITING_ON_TABLE = text(
    "SELECT COUNT(*) FROM performance_schema.data_lock_waits AS w"
    " JOIN performance_schema.threads AS t ON t.THREAD_ID = w.REQUESTING_THREAD_ID"
    " JOIN performance_schema.data_locks AS l"
    " ON l.ENGINE = w.ENGINE AND l.ENGINE_LOCK_ID = w.REQUESTING_ENGINE_LOCK_ID"
    " WHERE t.PROCESSLIST_ID = :connection_id"
    " AND l.OBJECT_SCHEMA = DATABASE() AND l.OBJECT_NAME = :table_name"
)

# 只用于超时诊断。
_LOCK_REPORT = (
    ("performance_schema.data_lock_waits", "SELECT * FROM performance_schema.data_lock_waits"),
    (
        "performance_schema.data_locks",
        "SELECT THREAD_ID, OBJECT_SCHEMA, OBJECT_NAME, INDEX_NAME, LOCK_TYPE, LOCK_MODE,"
        " LOCK_STATUS, LOCK_DATA FROM performance_schema.data_locks",
    ),
)


# --- 线程与暂停点 -----------------------------------------------------------------


class GateTimeout(AssertionError):
    """Raised in the paused thread when nobody released its gate in time."""


class Gate:
    """One pause point: the first arrival waits here until `release()`."""

    def __init__(self, step: str) -> None:
        self.step = step
        self.reached = threading.Event()
        self.released = threading.Event()
        self._fired = False
        self._lock = threading.Lock()

    def claim(self) -> bool:
        with self._lock:
            if self._fired:
                return False
            self._fired = True
            return True

    def hold(self) -> None:
        self.reached.set()
        if not self.released.wait(GATE_TIMEOUT_SECONDS):
            raise GateTimeout(f"nobody released the gate {self.step!r}")

    def wait_reached(self) -> None:
        if not self.reached.wait(GATE_TIMEOUT_SECONDS):
            pytest.fail(f"never reached the gate {self.step!r} within {GATE_TIMEOUT_SECONDS}s")

    def release(self) -> None:
        self.released.set()


class Actor:
    """One side of a race, on its own thread."""

    def __init__(self, name: str, work: Callable[[], Any]) -> None:
        self.name = name
        self.result: Any = None
        self.error: BaseException | None = None
        self._thread = threading.Thread(target=self._run, args=(work,), name=name, daemon=True)
        self._thread.start()

    def _run(self, work: Callable[[], Any]) -> None:
        try:
            self.result = work()
        except BaseException as exc:
            self.error = exc

    def join(self, timeout: float = JOIN_TIMEOUT_SECONDS) -> Actor:
        self._thread.join(timeout)
        if self._thread.is_alive():
            pytest.fail(f"{self.name!r} did not finish within {timeout}s")
        return self

    def settle(self, timeout: float) -> None:
        self._thread.join(timeout)

    def value(self) -> Any:
        self.join()
        if self.error is not None:
            raise AssertionError(f"{self.name!r} failed: {self.error!r}") from self.error
        return self.result

    def failure(self) -> BaseException:
        self.join()
        assert self.error is not None, f"{self.name!r} should have failed, got {self.result!r}"
        return self.error


@dataclass
class Side:
    """One engine with a one-connection pool: a gate on it only stops that side."""

    name: str
    engine: Engine
    factory: sessionmaker[Session]
    connection_ids: list[int] = field(default_factory=list)
    errors: list[BaseException] = field(default_factory=list)


def _lock_report(connection: Connection) -> str:
    lines = []
    for title, sql in _LOCK_REPORT:
        try:
            rows = connection.execute(text(sql)).all()
        except Exception as exc:  # 诊断本身出错不能盖住它要解释的那个失败
            lines.append(f"{title}: unavailable ({exc!r})")
            continue
        lines.append(f"{title}: {[tuple(row) for row in rows]}")
    return "\n".join(lines)


class Race:
    """The sides, their gates and threads, and an observer that watches the lock waits."""

    def __init__(self, url: str) -> None:
        self.url = url
        self.observer = create_engine(url, isolation_level="AUTOCOMMIT", pool_size=1)
        self._sides: list[Side] = []
        self._gates: list[Gate] = []
        self._actors: list[Actor] = []

    def side(self, name: str) -> Side:
        # 池里只有一条连接：关户的事务 0、事务 1 与读详情、计费的处理事务都走它，
        # 所以「它在不在等锁」只要看这一个 CONNECTION_ID()。
        engine = create_engine(self.url, pool_size=1, max_overflow=0)
        side = Side(name=name, engine=engine, factory=create_session_factory(engine))

        def prepare(dbapi_connection: Any, _record: Any) -> None:
            cursor = dbapi_connection.cursor()
            try:
                cursor.execute("SELECT CONNECTION_ID()")
                side.connection_ids.append(int(cursor.fetchone()[0]))
                cursor.execute(
                    f"SET SESSION innodb_lock_wait_timeout = {LOCK_WAIT_TIMEOUT_SECONDS}"
                )
            finally:
                cursor.close()
            dbapi_connection.rollback()

        def note_error(exception_context: Any) -> None:
            side.errors.append(exception_context.original_exception)

        event.listen(engine, "connect", prepare)
        event.listen(engine, "handle_error", note_error)
        self._sides.append(side)
        return side

    def actor(self, name: str, work: Callable[[], Any]) -> Actor:
        actor = Actor(name, work)
        self._actors.append(actor)
        return actor

    def gate(self, step: str) -> Gate:
        gate = Gate(step)
        self._gates.append(gate)
        return gate

    def pause_before_commit(self, side: Side, step: str) -> Gate:
        """The side's first commit stops after its flush: written, locks held, not committed."""
        gate = self.gate(step)

        def listener(session: Session) -> None:
            if gate.claim():
                session.flush()
                gate.hold()

        event.listen(side.factory, "before_commit", listener)
        return gate

    def pause_after_closure_check(
        self, monkeypatch: pytest.MonkeyPatch, step: str
    ) -> tuple[Gate, list[ClosureBlockers]]:
        """The injected hook: the closure stops after `closure_blockers`, before writing.

        此时钱包与租户两把排他锁都在关户手里、一致性读快照已建立；返回的列表记下它数到的。
        """
        gate = self.gate(step)
        seen: list[ClosureBlockers] = []
        real = account_closure.closure_blockers

        def paused(session: Session, *, tenant_id: int, now: dt.datetime) -> ClosureBlockers:
            found = real(session, tenant_id=tenant_id, now=now)
            seen.append(found)
            if gate.claim():
                gate.hold()
            return found

        monkeypatch.setattr(account_closure, "closure_blockers", paused)
        return gate, seen

    def wait_until_waiting(self, side: Side, table_name: str) -> None:
        """Poll until the side's own connection is waiting for a row lock on `table_name`."""
        deadline = time.monotonic() + LOCK_POLL_LIMIT_SECONDS
        connection_id = None
        with self.observer.connect() as connection:
            while True:
                connection_id = side.connection_ids[-1] if side.connection_ids else None
                if connection_id is not None:
                    waiting = connection.execute(
                        _WAITING_ON_TABLE,
                        {"connection_id": connection_id, "table_name": table_name},
                    ).scalar_one()
                    if waiting:
                        return
                if time.monotonic() >= deadline:
                    break
                time.sleep(LOCK_POLL_INTERVAL_SECONDS)
            report = _lock_report(connection)
        pytest.fail(
            f"{side.name} (connection {connection_id}) was not seen waiting for a"
            f" {table_name} row lock within {LOCK_POLL_LIMIT_SECONDS}s\n{report}"
        )

    def assert_no_database_errors(self, *actors: Actor) -> None:
        """No OperationalError (deadlock, lock wait timeout) on any side or out of any thread."""
        for side in self._sides:
            assert side.errors == [], f"database errors on {side.name!r}: {side.errors!r}"
        for actor in actors:
            assert not isinstance(actor.error, OperationalError), (
                f"{actor.name!r} hit {actor.error!r}"
            )

    def close(self) -> None:
        for gate in self._gates:
            gate.release()
        for actor in self._actors:
            actor.settle(JOIN_TIMEOUT_SECONDS)
        for side in self._sides:
            side.engine.dispose()
        self.observer.dispose()


# --- 夹具 -----------------------------------------------------------------------


@pytest.fixture
def mysql_factory(monkeypatch) -> Iterator[sessionmaker[Session]]:
    yield from _mysql_factory(monkeypatch)


@pytest.fixture
def world(mysql_factory) -> BillingWorld:
    return BillingWorld(mysql_factory)


@pytest.fixture
def race(world) -> Iterator[Race]:
    # 地址在夹具里读（xdist 换库名之后）；没设时 `_mysql_factory` 已经 skip。
    url = os.environ["BILLING_TEST_DATABASE_URL"]
    race = Race(url)
    try:
        yield race
    finally:
        # 先放行所有暂停点、等线程结束，清场才不会卡在它们持有的锁上。
        race.close()


# --- 帮手 -----------------------------------------------------------------------


def disabled(world: BillingWorld) -> Tenancy:
    tenant = world.tenant()
    world.set_account_status(tenant, AccountStatus.DISABLED)
    return tenant


def close(factory: sessionmaker[Session], world: BillingWorld, tenant: Tenancy):
    return close_account(
        factory,
        actor=world.admin,
        customer_id=tenant.public_id,
        reason=REASON,
        context=CONTEXT,
        now=CLOSE_AT,
    )


def adjust(factory: sessionmaker[Session], world: BillingWorld, tenant: Tenancy, amount: str):
    return post_adjustment(
        factory,
        actor=world.admin,
        customer_id=tenant.public_id,
        transaction_type=TransactionType.ADJUSTMENT_CREDIT,
        amount=Decimal(amount),
        reason="Fictional settlement adjustment",
        idempotency_key=str(uuid.uuid4()),
        context=CONTEXT,
        now=CLOSE_AT,
    )


def enable(factory: sessionmaker[Session], world: BillingWorld, tenant: Tenancy):
    return change_account_status(
        factory,
        actor=world.admin,
        customer_id=tenant.public_id,
        account_status=AccountStatus.ENABLED,
        reason="Fictional re-enable",
        context=CONTEXT,
        now=CLOSE_AT,
    )


@dataclass(frozen=True)
class State:
    account_status: AccountStatus
    status_version: int
    balance: Decimal
    wallet_version: int
    ledger_rows: int
    status_audits: int


def state(world: BillingWorld, tenant: Tenancy) -> State:
    with world.factory() as session:
        row = session.get(Tenant, tenant.id)
        wallet = session.execute(select(Wallet).where(Wallet.tenant_id == tenant.id)).scalar_one()
        assert row is not None
        ledger = session.execute(
            select(func.count())
            .select_from(WalletTransaction)
            .where(WalletTransaction.tenant_id == tenant.id)
        ).scalar_one()
        audits = session.execute(
            select(func.count())
            .select_from(AuditLog)
            .where(
                AuditLog.action == AuditAction.TENANT_ACCOUNT_STATUS_CHANGED,
                AuditLog.entity_id == tenant.public_id,
            )
        ).scalar_one()
        return State(
            account_status=row.account_status,
            status_version=row.status_version,
            balance=wallet.balance,
            wallet_version=wallet.version,
            ledger_rows=int(ledger),
            status_audits=int(audits),
        )


def recording_post_transaction(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Wrap ⑧'s `post_transaction`: one entry per call, the exception type it raised or "ok"."""
    original = usage_billing.post_transaction
    calls: list[str] = []

    def recorded(*args: Any, **kwargs: Any) -> Any:
        try:
            result = original(*args, **kwargs)
        except Exception as error:
            calls.append(type(error).__name__)
            raise
        calls.append("ok")
        return result

    monkeypatch.setattr(usage_billing, "post_transaction", recorded)
    return calls


# --- 关户 vs 计费 ---------------------------------------------------------------


def test_an_event_billed_while_the_closure_commits_is_failed_final(
    world, race, monkeypatch
) -> None:
    """§7「并发：关户 vs 计费」：B 的 ② 读到 DISABLED，⑧ 等关户的钱包锁，锁内读到 CLOSED。"""
    world.publish_markup_usd()
    tenant = disabled(world)
    before = state(world, tenant)
    closer = race.side("closer")
    biller = race.side("biller")
    gate, seen = race.pause_after_closure_check(monkeypatch, "closure checked")
    calls = recording_post_transaction(monkeypatch)

    closing = race.actor("close", lambda: close(closer.factory, world, tenant))
    gate.wait_reached()
    # A 已检查过、四类都是 0；事件在这之后才落库，A 的快照看不到它。
    assert seen == [NOTHING]
    event_id = world.event(tenant)
    [claim] = world.claim()
    billing = race.actor(
        "bill",
        lambda: usage_billing.process_event(
            biller.factory, claim, max_attempts=MAX_ATTEMPTS, clock=world.clock
        ),
    )
    race.wait_until_waiting(biller, "wallets")
    gate.release()

    assert closing.value().account_status == "CLOSED"
    assert billing.value() is Outcome.FAILED_FINAL
    # ⑧ 确实调用了记账（② 读到的是 DISABLED），并在租户锁内被拒。
    assert calls == ["TenantClosed"]
    with world.factory() as session:
        row = session.get(UsageEvent, event_id)
        assert row is not None
        assert (row.status, row.error_code, row.wallet_transaction_id) == (
            UsageEventStatus.FAILED_FINAL,
            "ACCOUNT_CLOSED",
            None,
        )
    after = state(world, tenant)
    assert (after.account_status, after.status_version) == (AccountStatus.CLOSED, 1)
    assert (after.balance, after.wallet_version, after.ledger_rows) == (
        Decimal(0),
        before.wallet_version,
        0,
    )
    race.assert_no_database_errors(closing, billing)


# --- 关户 vs 调账 ---------------------------------------------------------------


def test_an_adjustment_committed_first_makes_the_closure_see_the_balance(world, race) -> None:
    """§7「并发：关户 vs 调账」，调账先：关户等钱包锁，锁内读到非零余额 → 409。"""
    tenant = disabled(world)
    adjuster = race.side("adjuster")
    closer = race.side("closer")
    gate = race.pause_before_commit(adjuster, "adjustment written")

    adjusting = race.actor("adjust", lambda: adjust(adjuster.factory, world, tenant, "20"))
    gate.wait_reached()
    closing = race.actor("close", lambda: close(closer.factory, world, tenant))
    race.wait_until_waiting(closer, "wallets")
    gate.release()

    adjusting.value()
    error = closing.failure()
    assert isinstance(error, AccountClosureBalanceNotZero)
    assert (error.code, error.http_status) == ("ACCOUNT_CLOSURE_BALANCE_NOT_ZERO", 409)
    after = state(world, tenant)
    # 版本 +1 来自调账跨零的计费状态跃迁（SUSPENDED → ACTIVE），不是关户。
    assert (after.account_status, after.status_version) == (AccountStatus.DISABLED, 1)
    assert (after.balance, after.ledger_rows, after.status_audits) == (Decimal(20), 1, 0)
    race.assert_no_database_errors(adjusting, closing)


def test_an_adjustment_after_the_closure_is_refused(world, race, monkeypatch) -> None:
    """§7「并发：关户 vs 调账」，关户先：调账等钱包锁，记账锁内读到 CLOSED。

    调账得到 409 `ACCOUNT_CLOSED`，什么都不写。
    """
    tenant = disabled(world)
    before = state(world, tenant)
    closer = race.side("closer")
    adjuster = race.side("adjuster")
    gate, seen = race.pause_after_closure_check(monkeypatch, "closure checked")

    closing = race.actor("close", lambda: close(closer.factory, world, tenant))
    gate.wait_reached()
    assert seen == [NOTHING]
    adjusting = race.actor("adjust", lambda: adjust(adjuster.factory, world, tenant, "20"))
    race.wait_until_waiting(adjuster, "wallets")
    gate.release()

    assert closing.value().account_status == "CLOSED"
    error = adjusting.failure()
    assert isinstance(error, AccountClosed)
    assert (error.code, error.http_status) == ("ACCOUNT_CLOSED", 409)
    after = state(world, tenant)
    assert (after.account_status, after.status_version, after.status_audits) == (
        AccountStatus.CLOSED,
        1,
        1,
    )
    assert (after.balance, after.wallet_version, after.ledger_rows) == (
        Decimal(0),
        before.wallet_version,
        0,
    )
    race.assert_no_database_errors(closing, adjusting)


# --- 关户 vs 启用 ---------------------------------------------------------------


def test_enabling_first_makes_the_closure_refuse(world, race) -> None:
    """§7「并发：关户 vs 启用」，启用先：关户拿到钱包锁后等租户锁，锁内读到 ENABLED → 409。"""
    tenant = disabled(world)
    enabler = race.side("enabler")
    closer = race.side("closer")
    gate = race.pause_before_commit(enabler, "enable written")

    enabling = race.actor("enable", lambda: enable(enabler.factory, world, tenant))
    gate.wait_reached()
    closing = race.actor("close", lambda: close(closer.factory, world, tenant))
    race.wait_until_waiting(closer, "tenants")
    gate.release()

    assert enabling.value().account_status == "ENABLED"
    error = closing.failure()
    assert isinstance(error, AccountStatusTransitionInvalid)
    assert (error.code, error.http_status) == ("ACCOUNT_STATUS_TRANSITION_INVALID", 409)
    after = state(world, tenant)
    assert (after.account_status, after.status_version, after.status_audits) == (
        AccountStatus.ENABLED,
        1,
        1,
    )
    race.assert_no_database_errors(enabling, closing)


def test_closing_first_makes_the_enable_refuse(world, race, monkeypatch) -> None:
    """§7「并发：关户 vs 启用」，关户先：启用等租户锁，锁内读到 CLOSED → 409。

    跃迁表里 `CLOSED` 没有任何一格。
    """
    tenant = disabled(world)
    closer = race.side("closer")
    enabler = race.side("enabler")
    gate, _seen = race.pause_after_closure_check(monkeypatch, "closure checked")

    closing = race.actor("close", lambda: close(closer.factory, world, tenant))
    gate.wait_reached()
    enabling = race.actor("enable", lambda: enable(enabler.factory, world, tenant))
    race.wait_until_waiting(enabler, "tenants")
    gate.release()

    assert closing.value().account_status == "CLOSED"
    error = enabling.failure()
    assert isinstance(error, AccountStatusTransitionInvalid)
    after = state(world, tenant)
    assert (after.account_status, after.status_version, after.status_audits) == (
        AccountStatus.CLOSED,
        1,
        1,
    )
    race.assert_no_database_errors(closing, enabling)


def test_enabling_and_closing_at_once_end_with_exactly_one_winner(world, race) -> None:
    """§7「并发：关户 vs 启用」，同时起跑：恰好一个成功、另一个 409，不出现死锁。"""
    tenant = disabled(world)
    closer = race.side("closer")
    enabler = race.side("enabler")
    start = threading.Barrier(2)

    def started(work: Callable[[], Any]) -> Callable[[], Any]:
        def run() -> Any:
            start.wait(timeout=GATE_TIMEOUT_SECONDS)
            return work()

        return run

    closing = race.actor("close", started(lambda: close(closer.factory, world, tenant)))
    enabling = race.actor("enable", started(lambda: enable(enabler.factory, world, tenant)))
    closing.join()
    enabling.join()

    race.assert_no_database_errors(closing, enabling)
    outcomes = {closing.name: closing.error, enabling.name: enabling.error}
    winners = [name for name, error in outcomes.items() if error is None]
    assert len(winners) == 1, outcomes
    [loser_error] = [error for error in outcomes.values() if error is not None]
    assert isinstance(loser_error, AccountStatusTransitionInvalid), loser_error
    after = state(world, tenant)
    expected = AccountStatus.CLOSED if winners == ["close"] else AccountStatus.ENABLED
    assert (after.account_status, after.status_version, after.status_audits) == (expected, 1, 1)

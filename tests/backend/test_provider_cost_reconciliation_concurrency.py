"""Concurrent confirmations of provider cost reconciliations (design gate #755 v1 §4, §7).

只在真 MySQL 上跑：唯一约束的仲裁（`ux_provider_cost_reconciliations_root`、
`uq_provider_cost_reconciliations_supersedes`）要真的行锁等待才看得出来。

编排（不改产品代码）：

- 事务 1 跑真实的 `confirm`，被 monkeypatch 的 `_after_audit_before_commit` 挡在
  审计之后、提交之前：它的记录与审计都已写进库、只差提交（提交后是一行一条审计）；
- 事务 2 用自己的引擎跑同一个月的 `confirm`，它的插入在唯一索引上等事务 1 的未提交键；
- 「事务 2 确实在等锁」查 `performance_schema.data_lock_waits`：经 `threads` 的
  `PROCESSLIST_ID` 对上事务 2 那条连接的 `CONNECTION_ID()`，经 `data_locks` 确认锁在
  `provider_cost_reconciliations` 上（写法照 test_session_revocation_concurrency.py）；
  ⚠️ 不用 `information_schema.INNODB_TRX`（一份不满 0.1 秒不刷新的缓存）；
- 确认之后放行事务 1，事务 2 得到重复键 → 回滚 → 409。

每一步等待都有超时，超时即失败并说出停在哪一步。
MySQL 那部分需要 `BILLING_TEST_DATABASE_URL`，没设时 skip —— **不要把 skipped 读成 passed**。
"""

from __future__ import annotations

import datetime as dt
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import pytest
from provider_cost_world import CONTEXT, PROVIDER_CODE, World
from sqlalchemy import Engine, create_engine, event, select, text
from sqlalchemy.orm import Session, sessionmaker

from app.core.database import create_session_factory
from app.core.errors import AppError
from app.models.auth import AuditAction, AuditLog
from app.models.provider_cost_reconciliations import ProviderCostReconciliation
from app.services import provider_cost_reconciliation as service

NOW = dt.datetime(2027, 1, 15, 0, 0, 0)
DECEMBER = "2026-12"

# 暂停点最多停这么久；到点还没人放行，被停住的那一方报错退出。
GATE_TIMEOUT_SECONDS = 15.0
# 「事务 2 在等行锁」的轮询：间隔 50 毫秒、最多 10 秒。
LOCK_POLL_INTERVAL_SECONDS = 0.05
LOCK_POLL_LIMIT_SECONDS = 10.0
# 事务 2 的连接把 innodb_lock_wait_timeout 调到这个值：真卡在锁上时以 1205 报错收场。
LOCK_WAIT_TIMEOUT_SECONDS = 20
# 等一个参与方结束：比锁等待超时长。
JOIN_TIMEOUT_SECONDS = 40.0

_CONNECTION_ID = "connection_id"
_TABLE = "provider_cost_reconciliations"

_WAITING_ON_TABLE = text(
    "SELECT COUNT(*) FROM performance_schema.data_lock_waits AS w"
    " JOIN performance_schema.threads AS t ON t.THREAD_ID = w.REQUESTING_THREAD_ID"
    " JOIN performance_schema.data_locks AS l"
    " ON l.ENGINE = w.ENGINE AND l.ENGINE_LOCK_ID = w.REQUESTING_ENGINE_LOCK_ID"
    " WHERE t.PROCESSLIST_ID = :connection_id"
    " AND l.OBJECT_SCHEMA = DATABASE() AND l.OBJECT_NAME = :table_name"
)


@pytest.fixture(autouse=True)
def first_month(monkeypatch) -> None:
    monkeypatch.setattr(service, "FIRST_RECONCILIATION_MONTH", DECEMBER)


# --- 编排 ---------------------------------------------------------------------------------


@dataclass
class Gate:
    """Holds the first caller of the hook until released; any later caller passes through."""

    held: threading.Event = field(default_factory=threading.Event)
    release: threading.Event = field(default_factory=threading.Event)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _claimed: bool = False

    def claim(self) -> bool:
        with self._lock:
            if self._claimed:
                return False
            self._claimed = True
            return True

    def hook(self, session: Session) -> None:
        if not self.claim():
            return
        self.held.set()
        if not self.release.wait(GATE_TIMEOUT_SECONDS):
            raise RuntimeError("transaction 1 was never released")


@dataclass
class Actor:
    """One confirmation running on its own thread."""

    name: str
    run: Callable[[], service.ReconciliationView]
    value: service.ReconciliationView | None = None
    error: BaseException | None = None
    done: threading.Event = field(default_factory=threading.Event)
    thread: threading.Thread | None = None

    def start(self) -> None:
        def target() -> None:
            try:
                self.value = self.run()
            except BaseException as error:
                # 由用例断言报出。
                self.error = error
            finally:
                self.done.set()

        self.thread = threading.Thread(target=target, name=self.name, daemon=True)
        self.thread.start()

    def join(self) -> None:
        assert self.thread is not None
        self.thread.join(JOIN_TIMEOUT_SECONDS)
        assert not self.thread.is_alive(), f"{self.name} did not finish"


@dataclass
class Waiter:
    """The second transaction's own engine; it records its connection's `CONNECTION_ID()`."""

    engine: Engine
    factory: sessionmaker[Session]
    connection_ids: list[int] = field(default_factory=list)


def _prepare_connection(dbapi_connection: Any, connection_record: Any) -> None:
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("SELECT CONNECTION_ID()")
        connection_record.info[_CONNECTION_ID] = int(cursor.fetchone()[0])
        cursor.execute(f"SET SESSION innodb_lock_wait_timeout = {LOCK_WAIT_TIMEOUT_SECONDS}")
    finally:
        cursor.close()
    dbapi_connection.rollback()


@pytest.fixture
def waiter(provider_cost_mysql_world: World) -> Iterator[Waiter]:
    url = provider_cost_mysql_world.factory.kw["bind"].url
    engine = create_engine(url, pool_size=1, max_overflow=0)
    result = Waiter(engine=engine, factory=create_session_factory(engine))
    event.listen(engine, "connect", _prepare_connection)

    def note_checkout(dbapi_connection: Any, connection_record: Any, proxy: Any) -> None:
        result.connection_ids.append(connection_record.info[_CONNECTION_ID])

    event.listen(engine, "checkout", note_checkout)
    try:
        yield result
    finally:
        engine.dispose()


def _hold_next_confirmation(monkeypatch: pytest.MonkeyPatch) -> Gate:
    """From now on, the first `confirm` to reach the hook stops there until released."""
    gate = Gate()
    monkeypatch.setattr(service, "_after_audit_before_commit", gate.hook)
    return gate


def _confirm(
    factory: sessionmaker[Session],
    world: World,
    *,
    supersedes_id: str | None = None,
    reason: str | None = None,
    source_total: str = "11.70",
) -> service.ReconciliationView:
    return service.confirm(
        factory,
        actor=world.admin,
        context=CONTEXT,
        provider_code=PROVIDER_CODE,
        billing_month=DECEMBER,
        source_currency="USD",
        source_total=Decimal(source_total),
        source_document_reference="test-statement-2026-12",
        supersedes_id=supersedes_id,
        reason=reason,
        expected_event_count=2,
        expected_estimated_source_cost=Decimal("10.50"),
        now=NOW,
    )


def _wait_until_waiting(world: World, waiter: Waiter, second: Actor) -> None:
    """Poll until the second transaction's connection waits for a lock on the table."""
    observer = world.factory.kw["bind"]
    polls = int(LOCK_POLL_LIMIT_SECONDS / LOCK_POLL_INTERVAL_SECONDS)
    connection_id = None
    with observer.connect() as connection:
        for _ in range(polls):
            connection_id = waiter.connection_ids[-1] if waiter.connection_ids else None
            if connection_id is not None:
                waiting = connection.execute(
                    _WAITING_ON_TABLE, {"connection_id": connection_id, "table_name": _TABLE}
                ).scalar_one()
                if waiting:
                    return
            # 事务 2 没等锁就结束了：在这里报，而不是等满 10 秒。
            if second.done.wait(LOCK_POLL_INTERVAL_SECONDS):
                pytest.fail(f"{second.name} finished without waiting: {second.error!r}")
    pytest.fail(
        f"{second.name} (connection {connection_id}) was not seen waiting for a lock on"
        f" {_TABLE} within {LOCK_POLL_LIMIT_SECONDS}s"
    )


def _race(world: World, waiter: Waiter, gate: Gate, first: Actor, second: Actor) -> None:
    """Hold `first` after its audit, start `second`, see it wait, then let `first` commit.

    ⚠️ 不论哪一步失败都放行 `first`（finally），线程不挂在暂停点上。
    """
    first.start()
    try:
        assert gate.held.wait(GATE_TIMEOUT_SECONDS), (
            f"{first.name} never reached the hook: {first.error!r}"
        )
        second.start()
        _wait_until_waiting(world, waiter, second)
    finally:
        gate.release.set()
        first.join()
        if second.thread is not None:
            second.join()


def _rows(world: World) -> list[ProviderCostReconciliation]:
    with world.factory() as session:
        statement = select(ProviderCostReconciliation).order_by(ProviderCostReconciliation.id)
        return list(session.execute(statement).scalars())


def _audits(world: World) -> list[AuditLog]:
    with world.factory() as session:
        statement = (
            select(AuditLog)
            .where(AuditLog.action == AuditAction.PROVIDER_COST_RECONCILIATION_CREATE)
            .order_by(AuditLog.id)
        )
        return list(session.execute(statement).scalars())


def _refused(actor: Actor, code: str) -> None:
    assert actor.value is None
    assert isinstance(actor.error, AppError), f"{actor.name} raised {actor.error!r}"
    assert (actor.error.http_status, actor.error.code) == (409, code)


# --- 用例 ---------------------------------------------------------------------------------


def test_two_roots_of_the_same_month_one_wins(
    provider_cost_mysql_world: World, waiter: Waiter, monkeypatch
) -> None:
    world = provider_cost_mysql_world
    gate = _hold_next_confirmation(monkeypatch)
    first = Actor("transaction 1", lambda: _confirm(world.factory, world))
    second = Actor("transaction 2", lambda: _confirm(waiter.factory, world, source_total="12.00"))

    _race(world, waiter, gate, first, second)

    assert first.error is None, f"transaction 1 raised {first.error!r}"
    assert first.value is not None
    _refused(second, "RECONCILIATION_EXISTS")
    [row] = _rows(world)
    assert row.public_id == first.value.reconciliation_id
    assert str(row.source_total) == "11.70000000"
    [audit] = _audits(world)
    assert audit.entity_id == first.value.reconciliation_id


def test_two_corrections_of_the_same_row_one_wins(
    provider_cost_mysql_world: World, waiter: Waiter, monkeypatch
) -> None:
    world = provider_cost_mysql_world
    # a 经真实的 confirm 写入（一行、一条审计），之后才装暂停点。
    a = _confirm(world.factory, world)
    assert len(_audits(world)) == 1
    gate = _hold_next_confirmation(monkeypatch)
    first = Actor(
        "transaction 1",
        lambda: _confirm(
            world.factory,
            world,
            supersedes_id=a.reconciliation_id,
            reason="First correction",
            source_total="12.00",
        ),
    )
    second = Actor(
        "transaction 2",
        lambda: _confirm(
            waiter.factory,
            world,
            supersedes_id=a.reconciliation_id,
            reason="Second correction",
            source_total="13.00",
        ),
    )

    _race(world, waiter, gate, first, second)

    assert first.error is None, f"transaction 1 raised {first.error!r}"
    assert first.value is not None
    _refused(second, "RECONCILIATION_NOT_CURRENT")
    rows = _rows(world)
    assert [row.public_id for row in rows] == [a.reconciliation_id, first.value.reconciliation_id]
    assert rows[1].supersedes_id == rows[0].id
    assert rows[1].reason == "First correction"
    audits = _audits(world)
    assert [audit.entity_id for audit in audits] == [
        a.reconciliation_id,
        first.value.reconciliation_id,
    ]

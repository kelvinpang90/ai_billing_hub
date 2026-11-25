"""Rebill confirm under concurrency on a real MySQL (design gate #830 v12 §2, §4, §7; AIH-TASK-262).

设计 §2「读视图与锁的约定」、§4「并发」与 §7 的「并发（MySQL）」两行、「不同客户并发（MySQL）」、
「零计费并发（MySQL）」两个变体、「读视图（MySQL）」，另加同一幂等键的两种并发。非并发各行在
`test_usage_rebill_service.py`（AIH-TASK-255）。

⚠️ 只能对着真 MySQL 跑：SQLite 方言下 `FOR UPDATE` 被静默忽略，没有行锁，也没有隔离级别可设，
这里的每一种交错在内存库上都不存在。需要 `BILLING_TEST_DATABASE_URL`，CI 设了；本地没设时
整个文件 skip —— **skip 不是 pass**。整个文件只在 MySQL 上跑，断言里的 `FOR UPDATE` /
`FOR SHARE` 与 `@@transaction_isolation` 都是 MySQL 的字面量，不涉及 CLAUDE.md 说的「SQLite 与
MySQL 两边都要断言」那种情况。

编排（不改业务代码）：

- 让确认在指定位置停住：用 monkeypatch 包住 app/repositories/usage_rebills.py 的公开函数
  （`get_by_idempotency_key` 是第 3 步、钱包锁之后；`select_events` 是第 4 步选事件），在里面
  等一个 `threading.Event`；只有第一个调用方停；
- 「两边都在插入之前停住」：各自引擎上的 `before_cursor_execute` 匹配 `INSERT INTO usage_rebills`，
  在同一个 `threading.Barrier` 上会合；
- 每个参与方用自己的引擎：它的锁等待、错误与连接号都只记在它自己身上；
- 「对方确实在等行锁」查 `performance_schema.data_lock_waits` 联 `performance_schema.data_locks`、
  按 `DATABASE()` 与表名过滤，经 `performance_schema.threads` 对上等待方那条连接的
  `CONNECTION_ID()`（写法照 test_session_revocation_concurrency.py 的 `_WAITING_ON_TABLE`）；
  不用 `information_schema.INNODB_TRX`（缓存不准，只在超时诊断里报）；
- 每一步等待都有超时，超时即失败并说出停在哪一步；用例结束（含失败）时放开所有暂停点、
  join 每个线程、释放各引擎的连接（未提交的事务随之回滚）。

设计 §7 末尾的五个变异与守住它们的用例：

1. 删掉确认时的指纹比对 → `test_two_confirms_of_one_preview_serialise_and_the_second_is_stale`、
   `test_a_zero_charge_event_billed_while_a_rebill_holds_the_wallet[before_selection]`；
2. `eff(e)` 总用 `billable_cost` → test_usage_rebill_service.py 的
   `test_two_corrections_keep_the_ledger_equal_to_the_effective_charge`；
3. 删掉 `READ COMMITTED` 并在锁钱包之前加一次普通读 →
   `test_two_confirms_of_one_preview_serialise_and_the_second_is_stale`（B 的读视图建在 A 提交
   之前）；读视图用例同样变红；
4. 删掉 `READ COMMITTED` 并把幂等查重（或最近明细）改为 `FOR SHARE` →
   `test_two_customers_confirm_concurrently_without_a_deadlock`（1213）；读视图用例同样变红；
5. 幂等判定挪到指纹比对之后 → test_usage_rebill_service.py 的
   `test_a_lost_response_is_replayed_even_after_the_state_moved_on`；本文件的
   `test_one_key_twice_for_one_customer_is_one_201_and_one_replay` 同样变红。

金额：默认数量按 `V_BASE`、`R_GLOBAL` 计费 4.5，更正规则 `R_LOW`（×1.5）是 3.375。客户 `A` 的
项目 `A1` 上，计费时选中的规则是 `R_FIXED`：一个缓存读 token 计 0.000000004，舍成 0
（计费额为 0 的预付事件），默认数量计费额大于 0。uuid 是全零占位；时间一律注入。
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import re
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from decimal import Decimal
from types import ModuleType
from typing import Any, Final

import pytest
from provider_cost_world import CONTEXT, mysql_url, zero_uuid
from rebill_world import A1, B1, R_LOW, A, B, EventRef, RebillWorld, build_world, mysql_factory
from sqlalchemy import Engine, create_engine, event, func, inspect, select, text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session, sessionmaker

from app.core.database import create_session_factory
from app.core.errors import AppError
from app.models.auth import AuditAction, AuditLog
from app.models.usage import UsageEvent, UsageEventStatus
from app.models.usage_rebills import UsageRebill, UsageRebillLine
from app.models.wallet import ReferenceType, TransactionType, Wallet, WalletTransaction
from app.repositories import usage_rebills as rebill_rows
from app.repositories.wallet import post_transaction
from app.services.usage_billing import Outcome, claim_events, process_event
from app.services.usage_rebill import (
    RebillRequest,
    RebillResult,
    confirm_rebill,
    preview_rebill,
)

START: Final = dt.datetime(2026, 11, 10, 0, 0, 0)
HOUR: Final = dt.timedelta(hours=1)
FUNDED_AT: Final = dt.datetime(2026, 10, 2, 0, 0, 0)
# 确认的时钟；计费的时钟在它之后（账本行的 created_at 与序号同向）。
NOW: Final = dt.datetime(2026, 11, 25, 3, 0, 0)
BILLED_AT: Final = dt.datetime(2026, 11, 25, 4, 0, 0)
REASON: Final = "Fictional concurrent correction"
# 按 `R_FIXED` 计费额为 0、按 `R_LOW` 更正后大于 0 的数量。
ONE_CACHE_READ: Final = (0, 0, 0, 1)
LEASE_SECONDS: Final = 300
MAX_ATTEMPTS: Final = 5

# 暂停点最多停这么久；到点还没人放行，被停住的那一方报错退出。
GATE_TIMEOUT_SECONDS: Final = 15.0
# 「对方在等行锁」的轮询：间隔 50 毫秒、最多 10 秒。
LOCK_POLL_INTERVAL_SECONDS: Final = 0.05
LOCK_POLL_LIMIT_SECONDS: Final = 10.0
# 本文件的连接都把 innodb_lock_wait_timeout 调到这个值：真卡在锁上时以 1205 报错收场。
# 计费自己的事务会再设成 10 秒（app/services/usage_billing.py）。
LOCK_WAIT_TIMEOUT_SECONDS: Final = 20
# 等一个参与方结束：比锁等待超时长，锁等待超时会先以错误的形式出现。
JOIN_TIMEOUT_SECONDS: Final = 40.0

_CONNECTION_ID: Final = "connection_id"
_ISOLATION: Final = "SELECT @@transaction_isolation"

_WAITING_ON_TABLE: Final = text(
    "SELECT COUNT(*) FROM performance_schema.data_lock_waits AS w"
    " JOIN performance_schema.threads AS t ON t.THREAD_ID = w.REQUESTING_THREAD_ID"
    " JOIN performance_schema.data_locks AS l"
    " ON l.ENGINE = w.ENGINE AND l.ENGINE_LOCK_ID = w.REQUESTING_ENGINE_LOCK_ID"
    " WHERE t.PROCESSLIST_ID = :connection_id"
    " AND l.OBJECT_SCHEMA = DATABASE() AND l.OBJECT_NAME = :table_name"
)

# 只用于超时诊断。INNODB_TRX 那份缓存不准，但报出来仍有参考价值。
_LOCK_REPORT: Final = (
    ("performance_schema.data_lock_waits", "SELECT * FROM performance_schema.data_lock_waits"),
    (
        "performance_schema.data_locks",
        "SELECT THREAD_ID, OBJECT_SCHEMA, OBJECT_NAME, INDEX_NAME, LOCK_TYPE, LOCK_MODE,"
        " LOCK_STATUS, LOCK_DATA FROM performance_schema.data_locks",
    ),
    (
        "information_schema.INNODB_TRX",
        "SELECT trx_id, trx_state, trx_mysql_thread_id, trx_query"
        " FROM information_schema.INNODB_TRX",
    ),
)


# --- statements ---------------------------------------------------------------------


def _sql(statement: str) -> str:
    return " ".join(statement.replace("`", "").lower().split())


_LOCKING: Final = re.compile(r"\bfor (update|share)\b|\block in share mode\b")
_SHARING: Final = re.compile(r"\bfor share\b|\block in share mode\b")
_FROM: Final = re.compile(r" from (\w+)")


def locks_wallet(statement: str) -> bool:
    """`SELECT … FROM wallets … FOR UPDATE`: the confirm's first SQL and `post_transaction`'s."""
    sql = _sql(statement)
    return sql.startswith("select ") and " from wallets " in sql and sql.endswith(" for update")


def inserts_rebill(statement: str) -> bool:
    return _sql(statement).startswith("insert into usage_rebills ")


def is_locking(sql: str) -> bool:
    return _LOCKING.search(sql) is not None


def locked_table(sql: str) -> str:
    found = _FROM.search(sql)
    assert found is not None, sql
    return found.group(1)


# --- threads, gates and engines -------------------------------------------------------


class GateTimeout(AssertionError):
    """Raised in the paused thread when nobody released its gate in time."""


class Gate:
    """One pause point: the first caller waits here until `release()`."""

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
class Db:
    """One engine. Each side of a race gets its own, so its waits and errors are its own."""

    name: str
    engine: Engine
    factory: sessionmaker[Session]
    # `CONNECTION_ID()` of each connection that sent `SELECT … FROM wallets … FOR UPDATE`.
    lockers: list[int | None] = field(default_factory=list)
    errors: list[BaseException] = field(default_factory=list)


def _prepare_connection(dbapi_connection: Any, connection_record: Any) -> None:
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("SELECT CONNECTION_ID()")
        connection_record.info[_CONNECTION_ID] = int(cursor.fetchone()[0])
        cursor.execute(f"SET SESSION innodb_lock_wait_timeout = {LOCK_WAIT_TIMEOUT_SECONDS}")
    finally:
        cursor.close()
    dbapi_connection.rollback()


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
    """The rebill world plus engines per side, pause points, threads and lock-wait polling."""

    def __init__(self, world: RebillWorld, url: str) -> None:
        self.w = world
        self._url = url
        self._dbs: list[Db] = []
        self._gates: list[Gate] = []
        self._barriers: list[threading.Barrier] = []
        self._actors: list[Actor] = []
        self.observer = create_engine(url, isolation_level="AUTOCOMMIT", pool_size=1)

    def db(self, name: str, *, pool_size: int = 4, max_overflow: int = 4) -> Db:
        engine = create_engine(self._url, pool_size=pool_size, max_overflow=max_overflow)
        db = Db(name=name, engine=engine, factory=create_session_factory(engine))
        event.listen(engine, "connect", _prepare_connection)

        def note_locker(conn, cursor, statement, parameters, context, executemany) -> None:
            if locks_wallet(statement):
                db.lockers.append(conn.info.get(_CONNECTION_ID))

        def note_error(exception_context) -> None:
            db.errors.append(
                exception_context.sqlalchemy_exception or exception_context.original_exception
            )

        event.listen(engine, "before_cursor_execute", note_locker)
        event.listen(engine, "handle_error", note_error)
        self._dbs.append(db)
        return db

    def actor(self, name: str, work: Callable[[], Any]) -> Actor:
        actor = Actor(name, work)
        self._actors.append(actor)
        return actor

    def barrier(self, parties: int) -> threading.Barrier:
        barrier = threading.Barrier(parties, timeout=GATE_TIMEOUT_SECONDS)
        self._barriers.append(barrier)
        return barrier

    def pause_in(
        self,
        monkeypatch: pytest.MonkeyPatch,
        module: ModuleType,
        name: str,
        step: str,
        *,
        after: bool = False,
    ) -> Gate:
        """Wrap `module.name`: its first caller stops before (or `after`) the real call."""
        gate = Gate(step)
        self._gates.append(gate)
        original = getattr(module, name)

        def paused(*args: Any, **kwargs: Any) -> Any:
            if not after and gate.claim():
                gate.hold()
            result = original(*args, **kwargs)
            if after and gate.claim():
                gate.hold()
            return result

        monkeypatch.setattr(module, name, paused)
        return gate

    def meet_before(
        self, db: Db, barrier: threading.Barrier, matches: Callable[[str], bool]
    ) -> None:
        """`db`'s first statement that `matches` waits on `barrier` before it is sent."""
        fired = threading.Event()

        def listener(conn, cursor, statement, parameters, context, executemany) -> None:
            if matches(statement) and not fired.is_set():
                fired.set()
                barrier.wait()

        event.listen(db.engine, "before_cursor_execute", listener)

    def wait_until_waiting(self, db: Db, who: str, table: str) -> None:
        """Poll until `who`'s own wallet-locking connection waits for a row lock on `table`."""
        deadline = time.monotonic() + LOCK_POLL_LIMIT_SECONDS
        connection_id = None
        with self.observer.connect() as connection:
            while True:
                connection_id = db.lockers[-1] if db.lockers else None
                if connection_id is not None:
                    waiting = connection.execute(
                        _WAITING_ON_TABLE,
                        {"connection_id": connection_id, "table_name": table},
                    ).scalar_one()
                    if waiting:
                        return
                if time.monotonic() >= deadline:
                    break
                time.sleep(LOCK_POLL_INTERVAL_SECONDS)
            report = _lock_report(connection)
        pytest.fail(
            f"{who} (connection {connection_id}) was not seen waiting for a {table} row lock"
            f" within {LOCK_POLL_LIMIT_SECONDS}s\n{report}"
        )

    def assert_no_database_errors(self, *actors: Actor) -> None:
        """No database error on any engine, and no OperationalError out of any thread."""
        for db in self._dbs:
            assert db.errors == [], f"database errors on {db.name!r}: {db.errors!r}"
        self.assert_no_lock_errors(*actors)

    def assert_no_lock_errors(self, *actors: Actor) -> None:
        """No lock wait timeout and no deadlock (OperationalError) anywhere."""
        for db in self._dbs:
            locked = [error for error in db.errors if isinstance(error, OperationalError)]
            assert locked == [], f"lock errors on {db.name!r}: {locked!r}"
        for actor in actors:
            assert not isinstance(actor.error, OperationalError), (
                f"{actor.name!r} hit {actor.error!r}"
            )

    def close(self) -> None:
        # 先放开所有暂停点，再 join：被停住的线程走完（或报错）后各自回滚、归还连接。
        for gate in self._gates:
            gate.release()
        for barrier in self._barriers:
            barrier.abort()
        for actor in self._actors:
            actor.settle(JOIN_TIMEOUT_SECONDS)
        for db in self._dbs:
            db.engine.dispose()
        self.observer.dispose()


@pytest.fixture
def race(monkeypatch: pytest.MonkeyPatch) -> Iterator[Race]:
    """The rebill world on a real MySQL (skips without `BILLING_TEST_DATABASE_URL`)."""
    databases = mysql_factory(monkeypatch)
    factory = next(databases)
    created: Race | None = None
    try:
        created = Race(build_world(factory), mysql_url())
        yield created
    finally:
        if created is not None:
            created.close()
        # 跑完 mysql_factory 的 finally：清场、释放主引擎。
        databases.close()


# --- helpers --------------------------------------------------------------------------


def _criteria() -> dict[str, Any]:
    return {"occurred_from": "2026-11-10T00:00:00Z", "occurred_to": "2026-11-20T00:00:00Z"}


def _request(
    w: RebillWorld, customer: str = A, *, events: list[EventRef] | None = None
) -> RebillRequest:
    """Correct with `R_LOW`: the given events, or the customer's events in the criteria range."""
    selection = (
        {"usage_event_ids": [item.public_id for item in events]}
        if events is not None
        else _criteria()
    )
    return RebillRequest(
        customer_id=w.customers[customer].public_id,
        selection=selection,
        corrective_pricing_rule_id=w.public_rule(R_LOW),
    )


def _clock() -> dt.datetime:
    return NOW


def _billing_clock() -> dt.datetime:
    return BILLED_AT


def _confirm(
    factory: sessionmaker[Session], w: RebillWorld, request: RebillRequest, token: str, *, key: int
) -> RebillResult:
    return confirm_rebill(
        factory,
        actor=w.admin,
        request=request,
        preview_token=token,
        reason=REASON,
        idempotency_key=zero_uuid(key),
        context=CONTEXT,
        clock=_clock,
    )


def _bill(factory: sessionmaker[Session]) -> list[Outcome]:
    """Claim and process every due event, as the billing worker does."""
    claims = claim_events(factory, lease_seconds=LEASE_SECONDS, clock=_billing_clock)
    return [
        process_event(factory, claim, max_attempts=MAX_ATTEMPTS, clock=_billing_clock)
        for claim in claims
    ]


def _fund(w: RebillWorld, customer: str, amount: str) -> None:
    with w.factory() as session:
        post_transaction(
            session,
            tenant_id=w.customers[customer].tenant_id,
            transaction_type=TransactionType.ADJUSTMENT_CREDIT,
            amount=Decimal(amount),
            reference_type=ReferenceType.ADMIN_ADJUSTMENT,
            reference_id=zero_uuid(w.number()),
            now=FUNDED_AT,
            created_by=w.admin_id,
            description="Fictional funding",
        )
        session.commit()


def _row(w: RebillWorld, model: Any, row_id: int) -> dict[str, Any]:
    with w.factory() as session:
        row = session.get(model, row_id)
        assert row is not None
        return {attr.key: getattr(row, attr.key) for attr in inspect(model).column_attrs}


def _count(w: RebillWorld, model: Any, *conditions: Any) -> int:
    with w.factory() as session:
        return int(
            session.execute(select(func.count()).select_from(model).where(*conditions)).scalar_one()
        )


def _balance(w: RebillWorld, customer: str) -> Decimal:
    with w.factory() as session:
        return session.execute(
            select(Wallet.balance).where(Wallet.tenant_id == w.customers[customer].tenant_id)
        ).scalar_one()


def _rebill_ledger(w: RebillWorld, customer: str | None = None) -> list[WalletTransaction]:
    statement = select(WalletTransaction).where(
        WalletTransaction.reference_type == ReferenceType.REBILL
    )
    if customer is not None:
        statement = statement.where(WalletTransaction.tenant_id == w.customers[customer].tenant_id)
    with w.factory() as session:
        return list(session.execute(statement.order_by(WalletTransaction.id)).scalars())


def _footprint(w: RebillWorld) -> tuple[int, ...]:
    """Every table a confirm writes: nothing may change when it is refused."""
    return (
        _count(w, WalletTransaction),
        _count(w, UsageRebill),
        _count(w, UsageRebillLine),
        _count(w, AuditLog, AuditLog.action == AuditAction.REBILL),
    )


def _code(error: BaseException) -> tuple[str, int]:
    assert isinstance(error, AppError), f"expected an AppError, got {error!r}"
    return error.code, error.http_status


def _isolation(engine: Engine) -> str:
    with engine.connect() as connection:
        return str(connection.exec_driver_sql(_ISOLATION).scalar_one())


# --- 同一客户并发（§7「并发（MySQL）」第一行） ---------------------------------------------


def test_two_confirms_of_one_preview_serialise_and_the_second_is_stale(
    race: Race, monkeypatch: pytest.MonkeyPatch
) -> None:
    """同一客户、同一事件、同一预览、不同幂等键：A 拿到钱包锁后停住；B 确实在等钱包锁；A 提交。

    B 拿到锁之后在 `READ COMMITTED` 下读到 A 的明细，最近明细 id 变了：409
    `REBILL_PREVIEW_STALE`。只有一行账本、一组明细。
    """
    w = race.w
    _fund(w, A, "100")
    target = w.add_event(project=A1, occurred_at=START)
    request = _request(w, events=[target])
    token = preview_rebill(w.factory, request=request).preview_token

    first_db, second_db = race.db("first confirm"), race.db("second confirm")
    gate = race.pause_in(
        monkeypatch,
        rebill_rows,
        "get_by_idempotency_key",
        "first confirm: holding the wallet lock",
    )
    first = race.actor(
        "first confirm", lambda: _confirm(first_db.factory, w, request, token, key=1)
    )
    gate.wait_reached()
    assert first_db.lockers, "第一个确认停住之前必须已锁钱包"
    second = race.actor(
        "second confirm", lambda: _confirm(second_db.factory, w, request, token, key=2)
    )
    race.wait_until_waiting(second_db, "second confirm", "wallets")
    gate.release()
    result = first.value()
    error = second.failure()

    assert result.replayed is False
    assert _code(error) == ("REBILL_PREVIEW_STALE", 409)
    [ledger] = _rebill_ledger(w)
    assert ledger.reference_id == result.id
    assert ledger.amount == Decimal("1.125")
    assert _count(w, UsageRebill) == 1
    assert _count(w, UsageRebillLine) == 1
    assert _count(w, AuditLog, AuditLog.action == AuditAction.REBILL) == 1
    race.assert_no_database_errors(first, second)


# --- Rebill 与计费（§7「并发（MySQL）」第二行） ---------------------------------------------


def test_billing_another_event_waits_for_a_rebill_holding_the_wallet_and_then_succeeds(
    race: Race, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rebill 持钱包锁时，同一客户另一个事件的计费在钱包锁上等待；Rebill 提交后计费成功。

    最终余额 = 初始 − 计费 + Rebill 份额；计费的账本行排在 Rebill 之后。
    """
    w = race.w
    _fund(w, A, "100")
    target = w.add_event(project=A1, occurred_at=START)
    pending = w.add_event(project=A1, occurred_at=START + HOUR, status=UsageEventStatus.RECEIVED)
    initial = _balance(w, A)
    request = _request(w, events=[target])
    token = preview_rebill(w.factory, request=request).preview_token

    confirm_db, billing_db = race.db("confirm"), race.db("billing")
    gate = race.pause_in(
        monkeypatch, rebill_rows, "get_by_idempotency_key", "confirm: holding the wallet lock"
    )
    confirming = race.actor(
        "confirm", lambda: _confirm(confirm_db.factory, w, request, token, key=1)
    )
    gate.wait_reached()
    billing = race.actor("billing", lambda: _bill(billing_db.factory))
    race.wait_until_waiting(billing_db, "billing", "wallets")
    gate.release()
    result = confirming.value()
    assert billing.value() == [Outcome.PROCESSED]

    billed = _row(w, UsageEvent, pending.id)
    assert billed["status"] == UsageEventStatus.PROCESSED
    charge = billed["billable_cost"]
    assert charge > 0
    usage = _row(w, WalletTransaction, billed["wallet_transaction_id"])
    assert usage["amount"] == -charge
    [ledger] = _rebill_ledger(w, A)
    assert ledger.reference_id == result.id
    assert usage["wallet_sequence"] > ledger.wallet_sequence, "计费在 Rebill 提交之后才入账"
    assert _balance(w, A) == initial - charge + result.ledger_amount
    race.assert_no_database_errors(confirming, billing)


# --- 同一幂等键并发（§4「同一幂等键并发」、§2 第 8 步） ---------------------------------------


def test_one_key_twice_for_one_customer_is_one_201_and_one_replay(
    race: Race, monkeypatch: pytest.MonkeyPatch
) -> None:
    """同一客户两个线程同键同参：第二个在钱包锁上等待，拿到锁后第 3 步查到第一个 → 重放。"""
    w = race.w
    _fund(w, A, "100")
    target = w.add_event(project=A1, occurred_at=START)
    request = _request(w, events=[target])
    token = preview_rebill(w.factory, request=request).preview_token

    first_db, second_db = race.db("first confirm"), race.db("second confirm")
    gate = race.pause_in(
        monkeypatch,
        rebill_rows,
        "get_by_idempotency_key",
        "first confirm: holding the wallet lock",
    )
    first = race.actor(
        "first confirm", lambda: _confirm(first_db.factory, w, request, token, key=1)
    )
    gate.wait_reached()
    second = race.actor(
        "second confirm", lambda: _confirm(second_db.factory, w, request, token, key=1)
    )
    race.wait_until_waiting(second_db, "second confirm", "wallets")
    gate.release()
    created = first.value()
    replayed = second.value()

    assert created.replayed is False
    assert replayed == dataclasses.replace(created, replayed=True)
    [ledger] = _rebill_ledger(w)
    assert ledger.reference_id == created.id
    assert _count(w, UsageRebill) == 1
    assert _count(w, UsageRebillLine) == 1
    race.assert_no_database_errors(first, second)


def test_one_key_for_two_customers_concurrently_is_one_201_and_one_conflict(
    race: Race, monkeypatch: pytest.MonkeyPatch
) -> None:
    """两个客户同键：没有共同的锁，两边第 3 步都查不到（在屏障上会合之后才往下走）。

    后插入的一方撞 `idempotency_key` 唯一约束，回滚后开新事务重查（第 8 步），请求指纹不同：
    409 `REBILL_CONFLICT`，它的客户什么都没写。
    """
    w = race.w
    _fund(w, A, "100")
    _fund(w, B, "100")
    event_a = w.add_event(project=A1, occurred_at=START)
    event_b = w.add_event(project=B1, occurred_at=START)
    requests = {A: _request(w, A, events=[event_a]), B: _request(w, B, events=[event_b])}
    tokens = {
        name: preview_rebill(w.factory, request=request).preview_token
        for name, request in requests.items()
    }
    balances = {name: _balance(w, name) for name in requests}

    barrier = race.barrier(2)
    original = rebill_rows.get_by_idempotency_key
    calls: list[tuple[str, bool]] = []
    guard = threading.Lock()

    def looked_up(session: Session, idempotency_key: str) -> UsageRebill | None:
        found = original(session, idempotency_key)
        with guard:
            calls.append((idempotency_key, found is not None))
            step_three = len(calls) <= 2
        if step_three:
            # 两边的第 3 步都做完了才放行：谁也没看到对方。
            barrier.wait()
        return found

    monkeypatch.setattr(rebill_rows, "get_by_idempotency_key", looked_up)
    dbs = {name: race.db(f"confirm {name}") for name in requests}
    actors = {
        name: race.actor(
            f"confirm {name}",
            lambda name=name: _confirm(dbs[name].factory, w, requests[name], tokens[name], key=1),
        )
        for name in requests
    }
    for actor in actors.values():
        actor.join()

    winners = [name for name, actor in actors.items() if actor.error is None]
    losers = [name for name, actor in actors.items() if actor.error is not None]
    assert len(winners) == 1 and len(losers) == 1, {
        name: actor.error for name, actor in actors.items()
    }
    [winner], [loser] = winners, losers
    created = actors[winner].value()
    assert created.replayed is False
    assert created.customer_id == w.customers[winner].public_id
    assert _code(actors[loser].failure()) == ("REBILL_CONFLICT", 409)
    # 两次第 3 步都没查到；第三次是失败一方第 8 步的重查，查到了胜出一方的那一行。
    assert calls == [(zero_uuid(1), False), (zero_uuid(1), False), (zero_uuid(1), True)]
    [duplicate] = [error for error in dbs[loser].errors if isinstance(error, IntegrityError)]
    assert "idempotency_key" in str(duplicate.orig)
    assert dbs[winner].errors == []

    [ledger] = _rebill_ledger(w)
    assert ledger.reference_id == created.id
    assert ledger.tenant_id == w.customers[winner].tenant_id
    assert _rebill_ledger(w, loser) == []
    assert _balance(w, loser) == balances[loser]
    assert _count(w, UsageRebill) == 1
    assert _count(w, UsageRebillLine) == 1
    race.assert_no_lock_errors(*actors.values())


# --- 不同客户并发（§7「不同客户并发（MySQL）」） ---------------------------------------------


def test_two_customers_confirm_concurrently_without_a_deadlock(race: Race) -> None:
    """两张新表为空；两个客户各自预览后同时确认，两边都在插入 `usage_rebills` 之前停住再放行。

    `READ COMMITTED` 下查不到行的读不加间隙锁：两个都 201，没有 1213；各自一行账本、一组明细。
    """
    w = race.w
    assert _count(w, UsageRebill) == 0
    assert _count(w, UsageRebillLine) == 0
    _fund(w, A, "100")
    _fund(w, B, "100")
    targets = {
        A: w.add_event(project=A1, occurred_at=START),
        B: w.add_event(project=B1, occurred_at=START),
    }
    requests = {name: _request(w, name, events=[target]) for name, target in targets.items()}
    tokens = {
        name: preview_rebill(w.factory, request=request).preview_token
        for name, request in requests.items()
    }

    barrier = race.barrier(2)
    dbs = {name: race.db(f"confirm {name}") for name in requests}
    for db in dbs.values():
        race.meet_before(db, barrier, inserts_rebill)
    actors = {
        name: race.actor(
            f"confirm {name}",
            lambda name=name, index=index: _confirm(
                dbs[name].factory, w, requests[name], tokens[name], key=index
            ),
        )
        for index, name in enumerate(requests, start=1)
    }
    results = {name: actor.value() for name, actor in actors.items()}

    race.assert_no_database_errors(*actors.values())
    for name, result in results.items():
        assert result.replayed is False
        assert result.customer_id == w.customers[name].public_id
        assert [line.usage_event_id for line in result.lines] == [targets[name].public_id]
        [ledger] = _rebill_ledger(w, name)
        assert ledger.reference_id == result.id
        assert ledger.amount == Decimal("1.125")
        assert _count(w, UsageRebill, UsageRebill.tenant_id == w.customers[name].tenant_id) == 1
    assert _count(w, UsageRebillLine) == 2


# --- 零计费并发（§7「零计费并发（MySQL）」两个变体） -------------------------------------------


@pytest.mark.parametrize("pause", ["before_selection", "after_selection"])
def test_a_zero_charge_event_billed_while_a_rebill_holds_the_wallet(
    race: Race, monkeypatch: pytest.MonkeyPatch, pause: str
) -> None:
    """事件 a 已计费（> 0），事件 b 预付、计费额为 0、`RECEIVED`，都在条件范围内；预览只含 a。

    Rebill 拿到钱包锁后停在第 4 步选事件之前（①）或之后、写账本之前（②）；计费处理 b 并提交
    （计费额为 0 不拿钱包锁，不等 Rebill）；Rebill 继续。① 409 `REBILL_PREVIEW_STALE`，什么都
    不写；② 成功，明细只有 a。两种情况 b 的快照与状态都不受影响；再预览同一条件，b 出现在明细里。
    """
    w = race.w
    _fund(w, A, "100")
    charged = w.add_event(project=A1, occurred_at=START)
    zero = w.add_event(
        project=A1,
        occurred_at=START + HOUR,
        status=UsageEventStatus.RECEIVED,
        tokens=ONE_CACHE_READ,
    )
    request = _request(w)
    preview = preview_rebill(w.factory, request=request)
    assert [line.usage_event_id for line in preview.lines] == [charged.public_id]
    assert preview.unchanged_count == 0

    confirm_db, billing_db = race.db("confirm"), race.db("billing")
    before_selection = pause == "before_selection"
    gate = race.pause_in(
        monkeypatch,
        rebill_rows,
        "select_events",
        f"confirm: wallet locked, {pause.replace('_', ' ')}",
        after=not before_selection,
    )
    confirming = race.actor(
        "confirm", lambda: _confirm(confirm_db.factory, w, request, preview.preview_token, key=1)
    )
    gate.wait_reached()
    assert confirm_db.lockers, "停住之前必须已锁钱包"
    # 计费在 Rebill 持锁期间完成并提交：计费额为 0 不经 post_transaction、不等钱包锁。
    assert race.actor("billing", lambda: _bill(billing_db.factory)).value() == [Outcome.PROCESSED]
    billed = _row(w, UsageEvent, zero.id)
    assert billed["status"] == UsageEventStatus.PROCESSED
    assert billed["billable_cost"] == 0
    assert billed["wallet_transaction_id"] is None
    footprint = _footprint(w)
    gate.release()

    if before_selection:
        assert _code(confirming.failure()) == ("REBILL_PREVIEW_STALE", 409)
        assert _footprint(w) == footprint
        expected = [charged.public_id, zero.public_id]
    else:
        result = confirming.value()
        assert [line.usage_event_id for line in result.lines] == [charged.public_id]
        [ledger] = _rebill_ledger(w, A)
        assert ledger.reference_id == result.id
        assert ledger.amount == Decimal("1.125")
        # a 已按 R_LOW 更正：再预览时它没有差额。
        expected = [zero.public_id]

    assert _row(w, UsageEvent, zero.id) == billed
    again = preview_rebill(w.factory, request=request)
    assert [line.usage_event_id for line in again.lines] == expected
    [line] = [item for item in again.lines if item.usage_event_id == zero.public_id]
    assert line.previous_charge == 0
    assert line.corrected_charge > 0
    race.assert_no_database_errors(confirming)


# --- 读视图（§7「读视图（MySQL）」） -----------------------------------------------------------


def test_the_confirm_reads_committed_locks_the_wallet_first_and_never_shares(
    race: Race, monkeypatch: pytest.MonkeyPatch
) -> None:
    """捕获确认事务在该连接上执行的语句，并在事务内读 `@@transaction_isolation`。

    隔离级别是 `READ-COMMITTED`；第一条语句是钱包的 `FOR UPDATE`；除 `post_transaction` 内部对
    钱包与租户的 `FOR UPDATE`（既有行为，设计 v9 豁免）外没有任何加锁读、没有 `FOR SHARE`；
    确认结束后同一连接池取出的连接回到引擎默认的隔离级别。
    """
    w = race.w
    _fund(w, A, "100")
    target = w.add_event(project=A1, occurred_at=START)
    request = _request(w, events=[target])
    token = preview_rebill(w.factory, request=request).preview_token

    # 一个连接的池：确认之后取出的就是确认用过的那条连接。
    db = race.db("confirm", pool_size=1, max_overflow=0)
    default = _isolation(db.engine)
    assert default != "READ-COMMITTED", "引擎默认值若已是 READ COMMITTED，这里验证不了还原"

    transactions: list[list[str]] = []

    def began(conn: Connection) -> None:
        transactions.append([])

    def captured(conn, cursor, statement, parameters, context, executemany) -> None:
        if transactions:
            transactions[-1].append(_sql(statement))

    seen: list[str] = []
    original = rebill_rows.get_by_idempotency_key

    def probed(session: Session, idempotency_key: str) -> UsageRebill | None:
        # 第 3 步在确认事务里：此刻本事务的隔离级别。
        seen.append(str(session.execute(text(_ISOLATION)).scalar_one()))
        return original(session, idempotency_key)

    monkeypatch.setattr(rebill_rows, "get_by_idempotency_key", probed)
    event.listen(db.engine, "begin", began)
    event.listen(db.engine, "before_cursor_execute", captured)
    try:
        result = _confirm(db.factory, w, request, token, key=1)
    finally:
        event.remove(db.engine, "before_cursor_execute", captured)
        event.remove(db.engine, "begin", began)

    assert result.replayed is False
    assert seen == ["READ-COMMITTED"]
    probe = _sql(_ISOLATION)
    [confirming] = [statements for statements in transactions if probe in statements]
    statements = [sql for sql in confirming if sql != probe]
    assert locks_wallet(statements[0]), statements[0]
    locking = [index for index, sql in enumerate(statements) if is_locking(sql)]
    assert locking[0] == 0
    # 其余的加锁读只有 post_transaction 的钱包锁与租户锁。
    assert [locked_table(statements[index]) for index in locking] == [
        "wallets",
        "wallets",
        "tenants",
    ], [statements[index] for index in locking]
    assert [sql for sql in statements if _SHARING.search(sql)] == []
    assert _isolation(db.engine) == default
    race.assert_no_database_errors()

"""Retention purge against real MySQL row locks (设计闸门 #833 v7 §7; AIH-TASK-263).

设计 §7 的「并发：两个实例」（这一层只验证 `run_purge`：两次并发的 `run_purge` 结果正确；命名锁由
AIH-TASK-264 实现与测试）、「并发：与业务」「锁范围（MySQL）」「隔离级别（MySQL）」，以及设计 v5
审查那条不阻断更正里的两条业务路径（app/services/retention_purge.py 模块说明）：重置成功时作废同一
用户的兄弟令牌、出示已死家族里用过的令牌时吊销整个家族——各与一个停在提交前的清理批次并发一次。

⚠️ 只能对着真 MySQL 跑：SQLite 没有行锁，这里的每一种交错在内存库上都不存在。需要
`BILLING_TEST_DATABASE_URL`，CI 设了；本地没设时整个文件 skip —— **skip 不是 pass**。

编排（不改产品代码，写法照 tests/backend/test_session_revocation_concurrency.py）：

- 暂停点挂在 SQLAlchemy 事件上（`before_cursor_execute` / `after_cursor_execute` 匹配到目标语句，
  或会话工厂的 `before_commit`），在 `threading.Event` 上等；每个参与方用自己的引擎，暂停点只装在
  它的引擎上
- 「有人在等行锁」查 `performance_schema.data_lock_waits` 联 `performance_schema.data_locks`，按
  `DATABASE()` 与表名过滤（50 毫秒一次，最多 10 秒）；不用 `information_schema.INNODB_TRX`
- 每一步等待都有超时；业务连接的 `innodb_lock_wait_timeout` 调到 20 秒，真卡在锁上时以 1205 收场
- 清理的 `now` 取用例开始时的真实时刻（整秒）：业务路径（登录、刷新、重置）用真实时钟，令牌的
  到期与闲置判定要和它对得上

匹配语句用的是 MySQL 编译出来的 SQL 文本；整个文件只在 MySQL 上跑。审计表只追加、拒绝 DELETE，
只看本用例开始之后写入的审计。uuid 用全零占位值（死家族用全 f，见「锁范围」）。
"""

from __future__ import annotations

import datetime as dt
import json
import os
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

import pytest
from alembic.config import Config
from sqlalchemy import Engine, create_engine, delete, event, func, insert, select, text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from alembic import command
from app.core.config import Settings
from app.core.database import create_session_factory
from app.core.passwords import hash_password, verify_password
from app.core.tokens import hash_refresh_token
from app.models.auth import (
    AuditAction,
    AuditLog,
    DomainOutbox,
    PasswordResetToken,
    RecoveryCode,
    RefreshToken,
    TokenPurpose,
    User,
    UserRole,
    UserStatus,
)
from app.models.tenancy import Tenant
from app.services.auth import RequestContext, TokenReused, authenticate, refresh_session, utc_now
from app.services.password_reset import request_reset, reset_password
from app.services.retention_purge import ITEMS, PurgeResult, run_purge

TEST_DATABASE_URL = os.environ.get("BILLING_TEST_DATABASE_URL", "")

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="BILLING_TEST_DATABASE_URL is not set; these interleavings need real MySQL row locks",
)

DOMAIN = "@retention-purge-mysql.example.com"
PASSWORD = "a-perfectly-fine-passphrase"
NEW_PASSWORD = "another-perfectly-fine-passphrase"
CONTEXT = RequestContext(ip_address="10.0.0.1", user_agent="pytest")
ALL_DELETE = {item.name: "delete" for item in ITEMS}
NO_CAP = 50_000

GATE_TIMEOUT_SECONDS = 15.0
LOCK_POLL_INTERVAL_SECONDS = 0.05
LOCK_POLL_LIMIT_SECONDS = 10.0
LOCK_WAIT_TIMEOUT_SECONDS = 20
JOIN_TIMEOUT_SECONDS = 40.0
# 设计 §7「锁范围」：清理批次停在提交前时，每个业务操作都在这么久之内完成。
BUSINESS_LIMIT_SECONDS = 5.0

_CONNECTION_ID = "connection_id"

_WAITING_ON_TABLE = text(
    "SELECT COUNT(*) FROM performance_schema.data_lock_waits AS w"
    " JOIN performance_schema.data_locks AS l"
    " ON l.ENGINE = w.ENGINE AND l.ENGINE_LOCK_ID = w.REQUESTING_ENGINE_LOCK_ID"
    " WHERE l.OBJECT_SCHEMA = DATABASE() AND l.OBJECT_NAME = :table_name"
)

_LOCK_REPORT = (
    ("performance_schema.data_lock_waits", "SELECT * FROM performance_schema.data_lock_waits"),
    (
        "performance_schema.data_locks",
        "SELECT THREAD_ID, OBJECT_SCHEMA, OBJECT_NAME, INDEX_NAME, LOCK_TYPE, LOCK_MODE,"
        " LOCK_STATUS, LOCK_DATA FROM performance_schema.data_locks",
    ),
)


def _sql(statement: str) -> str:
    return " ".join(statement.replace("`", "").lower().split())


def deletes_from(table: str) -> Callable[[str], bool]:
    return lambda statement: _sql(statement).startswith(f"delete from {table} ")


def selects_reset_ids(statement: str) -> bool:
    """`select_ids` of `password_reset_tokens`: the first statement of each of its batches."""
    return _sql(statement).startswith(
        "select password_reset_tokens.id from password_reset_tokens where"
    )


# --- threads and gates ------------------------------------------------------------


class GateTimeout(AssertionError):
    """Raised in the paused thread when nobody released its gate in time."""


class Gate:
    """One pause point: the matching statement waits here until `release()`."""

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

    def running(self) -> bool:
        return self._thread.is_alive()

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


@dataclass
class Db:
    """One engine. Each side of a race gets its own, so a gate only stops that side."""

    name: str
    engine: Engine
    factory: sessionmaker[Session]
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


def clean(engine: Engine) -> None:
    """The three token tables entirely, then this file's users and their outbox rows."""
    with engine.begin() as connection:
        connection.execute(delete(RefreshToken))
        connection.execute(delete(PasswordResetToken))
        connection.execute(delete(RecoveryCode))
        ours = [
            str(user_id)
            for user_id in connection.execute(
                select(User.id).where(User.email.like(f"%{DOMAIN}"))
            ).scalars()
        ]
        if ours:
            connection.execute(
                delete(DomainOutbox).where(
                    DomainOutbox.aggregate_type == "users", DomainOutbox.aggregate_id.in_(ours)
                )
            )
        connection.execute(delete(User).where(User.email.like(f"%{DOMAIN}")))
        connection.execute(delete(Tenant).where(Tenant.email.like(f"%{DOMAIN}")))


class World:
    """Engines, gates, actors, seeding and reading back."""

    def __init__(self, url: str, settings: Settings) -> None:
        self.url = url
        self.settings = settings
        self._dbs: list[Db] = []
        self._gates: list[Gate] = []
        self._actors: list[Actor] = []
        self.observer = create_engine(url, isolation_level="AUTOCOMMIT", pool_size=1)
        self.main = self.db("main")
        # 整秒：DATETIME 列不存小数秒；业务路径用真实时钟，这里与它对齐。
        self.now = utc_now().replace(microsecond=0)
        self.old = self.now - dt.timedelta(days=31)
        with self.main.factory() as session:
            self.audit_start = int(
                session.execute(select(func.coalesce(func.max(AuditLog.id), 0))).scalar_one()
            )

    # -- plumbing --

    def db(self, name: str, *, pool_size: int = 4, max_overflow: int = 4) -> Db:
        engine = create_engine(self.url, pool_size=pool_size, max_overflow=max_overflow)
        db = Db(name=name, engine=engine, factory=create_session_factory(engine))
        event.listen(engine, "connect", _prepare_connection)

        def note_error(exception_context) -> None:
            db.errors.append(exception_context.original_exception)

        event.listen(engine, "handle_error", note_error)
        self._dbs.append(db)
        return db

    def actor(self, name: str, work: Callable[[], Any]) -> Actor:
        actor = Actor(name, work)
        self._actors.append(actor)
        return actor

    def _gate(self, step: str) -> Gate:
        gate = Gate(step)
        self._gates.append(gate)
        return gate

    def pause_before(
        self, db: Db, step: str, matches: Callable[[str], bool], *, nth: int = 1
    ) -> Gate:
        gate = self._gate(step)
        seen = [0]

        def listener(conn, cursor, statement, parameters, context, executemany) -> None:
            if matches(statement):
                seen[0] += 1
                if seen[0] == nth and gate.claim():
                    gate.hold()

        event.listen(db.engine, "before_cursor_execute", listener)
        return gate

    def pause_after(self, db: Db, step: str, matches: Callable[[str], bool]) -> Gate:
        gate = self._gate(step)

        def listener(conn, cursor, statement, parameters, context, executemany) -> None:
            if matches(statement) and gate.claim():
                gate.hold()

        event.listen(db.engine, "after_cursor_execute", listener)
        return gate

    def pause_before_commit(self, db: Db, step: str) -> Gate:
        gate = self._gate(step)

        def listener(session: Session) -> None:
            if gate.claim():
                # 先 flush：停下时这一批的删除与审计都已发到库里，只差提交。
                session.flush()
                gate.hold()

        event.listen(db.factory, "before_commit", listener)
        return gate

    def waits_on(self, connection: Connection, table: str) -> int:
        return int(connection.execute(_WAITING_ON_TABLE, {"table_name": table}).scalar_one())

    def wait_until_waiting_on(self, table: str, who: str) -> None:
        deadline = time.monotonic() + LOCK_POLL_LIMIT_SECONDS
        with self.observer.connect() as connection:
            while True:
                if self.waits_on(connection, table):
                    return
                if time.monotonic() >= deadline:
                    break
                time.sleep(LOCK_POLL_INTERVAL_SECONDS)
            report = _lock_report(connection)
        pytest.fail(
            f"{who} was not seen waiting for a {table} row lock"
            f" within {LOCK_POLL_LIMIT_SECONDS}s\n{report}"
        )

    def unblocked(self, name: str, work: Callable[[], Any]) -> Any:
        """Run `work`; it must finish within 5 s and nobody may wait on the token tables."""
        started = time.monotonic()
        actor = self.actor(name, work)
        seen: list[str] = []
        with self.observer.connect() as connection:
            while actor.running() and time.monotonic() - started < BUSINESS_LIMIT_SECONDS:
                for table in ("refresh_tokens", "password_reset_tokens"):
                    if self.waits_on(connection, table):
                        seen.append(table)
                time.sleep(LOCK_POLL_INTERVAL_SECONDS)
            report = _lock_report(connection) if seen or actor.running() else ""
        assert seen == [], f"{name}: lock waits on {sorted(set(seen))}\n{report}"
        actor.join(max(0.0, BUSINESS_LIMIT_SECONDS - (time.monotonic() - started)))
        return actor.value()

    def assert_no_database_errors(self, *actors: Actor) -> None:
        for db in self._dbs:
            assert db.errors == [], f"database errors on {db.name!r}: {db.errors!r}"
        for actor in actors:
            assert not isinstance(actor.error, OperationalError), f"{actor.name!r}: {actor.error!r}"

    def close(self) -> None:
        for gate in self._gates:
            gate.release()
        for actor in self._actors:
            actor.settle(5)
        for db in self._dbs:
            db.engine.dispose()
        self.observer.dispose()

    # -- the purge --

    def purge(self, db: Db, modes: dict[str, Any] | None = None) -> PurgeResult:
        return run_purge(
            db.factory,
            modes=ALL_DELETE if modes is None else modes,
            max_rows_per_item=NO_CAP,
            now=self.now,
        )

    def purge_completes(self, result: PurgeResult) -> None:
        """Either this round finished, or it stopped (设计 §5) and the next round finishes."""
        if result.failed_item is not None:
            again = self.purge(self.main)
            assert again.failed_item is None, again

    # -- seeding --

    def make_user(self, name: str) -> int:
        with self.main.factory() as session:
            user = User(
                email=f"{name}{DOMAIN}",
                password_hash=hash_password(PASSWORD),
                role=UserRole.CUSTOMER,
                status=UserStatus.ACTIVE,
                created_at=self.old,
                updated_at=self.old,
            )
            session.add(user)
            session.commit()
            return int(user.id)

    def add_refresh(
        self,
        user_id: int,
        family: str,
        *,
        live: bool,
        used: bool = False,
        raw: str | None = None,
    ) -> int:
        issued = self.now - dt.timedelta(minutes=1) if live else self.old - dt.timedelta(hours=12)
        expires = self.now + dt.timedelta(hours=1) if live else self.old
        with self.main.factory() as session:
            row = RefreshToken(
                user_id=user_id,
                family_id=family,
                token_hash=hash_refresh_token(raw or uuid.uuid4().hex),
                issued_at=issued,
                expires_at=expires,
                used_at=issued if used else None,
                password_version=0,
            )
            session.add(row)
            session.commit()
            return int(row.id)

    def add_expired_resets(self, user_id: int, count: int) -> None:
        with self.main.factory() as session:
            session.execute(
                insert(PasswordResetToken),
                [
                    {
                        "user_id": user_id,
                        "token_hash": hash_refresh_token(uuid.uuid4().hex),
                        "expires_at": self.old,
                        "created_at": self.old - dt.timedelta(minutes=30),
                        "purpose": TokenPurpose.PASSWORD_RESET,
                    }
                    for _ in range(count)
                ],
            )
            session.commit()

    def reset_token(self, name: str) -> str:
        outbox_id = request_reset(
            self.main.factory, self.settings, email=f"{name}{DOMAIN}", context=CONTEXT
        )
        assert outbox_id is not None
        with self.main.factory() as session:
            row = session.get(DomainOutbox, outbox_id)
            assert row is not None and row.payload_json is not None
            return str(json.loads(row.payload_json)["token"])

    # -- reading back --

    def ids(self, model: type[Any], **where: Any) -> set[int]:
        with self.main.factory() as session:
            statement = select(model.id).filter_by(**where)
            return set(session.execute(statement).scalars())

    def audits(self, action: AuditAction) -> list[AuditLog]:
        with self.main.factory() as session:
            rows = list(
                session.execute(
                    select(AuditLog)
                    .where(AuditLog.id > self.audit_start, AuditLog.action == action)
                    .order_by(AuditLog.id)
                ).scalars()
            )
            for row in rows:
                session.expunge(row)
            return rows

    def purged(self) -> dict[str, int]:
        """item → the sum of `deleted` over this test's purge audits; none of them may be 0."""
        totals: dict[str, int] = {}
        for row in self.audits(AuditAction.DATA_RETENTION_PURGE):
            state = json.loads(row.after_state or "null")
            assert state["deleted"] > 0, state
            totals[state["item"]] = totals.get(state["item"], 0) + state["deleted"]
        return totals


@pytest.fixture
def world(tmp_path, monkeypatch) -> Iterator[World]:
    from app.core.config import get_settings

    url = os.environ["BILLING_TEST_DATABASE_URL"]
    monkeypatch.setenv("BILLING_DATABASE_URL", url)
    get_settings.cache_clear()
    command.upgrade(Config("alembic.ini"), "head")
    get_settings.cache_clear()

    jwt_key = tmp_path / "jwt.key"
    jwt_key.write_text("a-test-signing-key-that-is-long-enough", encoding="utf-8")
    settings = Settings(database_url="", redis_url="", jwt_secret_file=str(jwt_key))

    created = World(url, settings)
    clean(created.main.engine)
    try:
        yield created
    finally:
        created.close()
        cleaner = create_engine(url)
        try:
            clean(cleaner)
        finally:
            cleaner.dispose()


# --- 并发：两个实例（这一层只有 run_purge） ---------------------------------------------


def test_two_purges_at_once_delete_every_row_once_and_audit_it_once(world: World) -> None:
    """第一次停在第一批提交之前；第二次选到同一批主键、在它们的行锁上等；放行。

    两次都正常结束：每一行恰好删一次，审计之和等于删除数，没有 0 行的审计。不靠命名锁
    （AIH-TASK-264）也不会删错或重复计数，只是第二次白等一次。
    """
    user = world.make_user("two-instances")
    world.add_expired_resets(user, 1_500)
    modes = {"password_reset_tokens": "delete"}
    first_db, second_db = world.db("first purge"), world.db("second purge")
    gate = world.pause_before_commit(first_db, "first purge: batch 1 before commit")

    first = world.actor("first purge", lambda: world.purge(first_db, modes))
    gate.wait_reached()
    second = world.actor("second purge", lambda: world.purge(second_db, modes))
    world.wait_until_waiting_on("password_reset_tokens", "second purge")
    gate.release()
    one, two = first.value(), second.value()

    assert one.failed_item is None and two.failed_item is None, (one, two)
    assert one.deleted + two.deleted == 1_500
    assert world.ids(PasswordResetToken) == set()
    assert world.purged() == {"password_reset_tokens": 1_500}
    world.assert_no_database_errors(first, second)


# --- 并发：与业务 ---------------------------------------------------------------------


def test_login_refresh_and_forgot_password_succeed_while_a_purge_pauses_between_batches(
    world: World,
) -> None:
    """清理删完一批后暂停；登录、刷新、忘记密码都成功；清理继续并只删到期终态行。"""
    user = world.make_user("business")
    other = world.make_user("business-other")
    live_raw = "a-live-refresh-token"
    live = world.add_refresh(user, uuid.UUID(int=1).hex, live=True, raw=live_raw)
    world.add_refresh(user, uuid.UUID(int=2).hex, live=False, used=True)
    world.add_refresh(user, uuid.UUID(int=2).hex, live=False)
    world.add_expired_resets(other, 1_500)

    purge_db, business_db = world.db("purge"), world.db("business")
    gate = world.pause_before(
        purge_db, "purge: reset batch 1 committed, batch 2 not started", selects_reset_ids, nth=2
    )
    purging = world.actor("purge", lambda: world.purge(purge_db))
    gate.wait_reached()

    login = world.unblocked(
        "login",
        lambda: authenticate(
            business_db.factory,
            world.settings,
            email=f"business{DOMAIN}",
            password=PASSWORD,
            context=CONTEXT,
        ),
    )
    rotated = world.unblocked(
        "refresh",
        lambda: refresh_session(
            business_db.factory, world.settings, raw_token=live_raw, context=CONTEXT
        ),
    )
    queued = world.unblocked(
        "forgot password",
        lambda: request_reset(
            business_db.factory, world.settings, email=f"business{DOMAIN}", context=CONTEXT
        ),
    )
    gate.release()
    result = purging.value()

    assert login.issued is not None
    assert rotated.refresh_token
    assert queued is not None
    assert result.failed_item is None, result
    kept = world.ids(RefreshToken)
    assert live in kept
    assert kept == world.ids(RefreshToken, user_id=user), "死家族全删，新家族与轮换出的行都在"
    assert len(kept) == 3
    assert world.ids(PasswordResetToken) == world.ids(PasswordResetToken, user_id=user)
    assert len(world.ids(PasswordResetToken)) == 1, "只剩刚签发的那一条"
    assert world.purged() == {"refresh_tokens": 2, "password_reset_tokens": 1_500}
    world.assert_no_database_errors(purging)


# --- 锁范围 ---------------------------------------------------------------------------

# 死家族的 family_id 取全 f：它是这个用户在 `(user_id, family_id)` 索引上最大的一项，新登录的随机
# 家族几乎一定落在它前面的间隙里。REPEATABLE READ 下 DELETE 的派生表对它加 next-key 锁，登录的插入
# 就会等；READ COMMITTED 下不加间隙锁，不等。
DEAD_FAMILY = "f" * 32


@pytest.mark.parametrize("table", ["refresh_tokens", "password_reset_tokens"])
def test_a_purge_batch_paused_before_commit_blocks_no_login_refresh_or_reset(
    world: World, table: str
) -> None:
    """清理批次停在 `DELETE` 之后、提交之前；登录、刷新活家族、忘记密码都在 5 秒内成功，期间没有
    人等这两张表的行锁；清理随后提交。可删家族与活家族同一用户与另一用户各一。
    """
    first = world.make_user("scope-a")
    second = world.make_user("scope-b")
    raws = {}
    for user, name in ((first, "a"), (second, "b")):
        raws[name] = f"a-live-refresh-token-{name}"
        world.add_refresh(user, uuid.UUID(int=1).hex, live=True, raw=raws[name])
        world.add_refresh(user, DEAD_FAMILY, live=False, used=True)
        world.add_refresh(user, DEAD_FAMILY, live=False)
        world.add_expired_resets(user, 1)

    purge_db, business_db = world.db("purge"), world.db("business")
    gate = world.pause_after(purge_db, f"purge: DELETE FROM {table} sent", deletes_from(table))
    purging = world.actor("purge", lambda: world.purge(purge_db))
    gate.wait_reached()

    login = world.unblocked(
        "login",
        lambda: authenticate(
            business_db.factory,
            world.settings,
            email=f"scope-a{DOMAIN}",
            password=PASSWORD,
            context=CONTEXT,
        ),
    )
    rotated = world.unblocked(
        "refresh",
        lambda: refresh_session(
            business_db.factory, world.settings, raw_token=raws["a"], context=CONTEXT
        ),
    )
    queued = world.unblocked(
        "forgot password",
        lambda: request_reset(
            business_db.factory, world.settings, email=f"scope-a{DOMAIN}", context=CONTEXT
        ),
    )
    gate.release()
    result = purging.value()

    assert login.issued is not None
    assert rotated.refresh_token
    assert queued is not None
    assert result.failed_item is None, result
    assert world.purged() == {"refresh_tokens": 4, "password_reset_tokens": 2}
    with world.main.factory() as session:
        families = set(session.execute(select(RefreshToken.family_id)).scalars())
    assert DEAD_FAMILY not in families
    assert len(world.ids(RefreshToken, user_id=first)) == 3, "活家族两行（含轮换）+ 新登录一行"
    assert len(world.ids(RefreshToken, user_id=second)) == 1
    assert len(world.ids(PasswordResetToken)) == 1, "只剩刚签发的那一条"
    world.assert_no_database_errors(purging)


# --- 隔离级别 -------------------------------------------------------------------------


def test_every_delete_batch_runs_read_committed_and_the_connection_is_restored(
    world: World,
) -> None:
    user = world.make_user("isolation")
    world.add_refresh(user, DEAD_FAMILY, live=False)
    world.add_expired_resets(user, 1)
    with world.main.factory() as session:
        session.add(
            RecoveryCode(
                user_id=user,
                code_hash="not-a-real-hash",
                used_at=world.old,
                created_at=world.old,
            )
        )
        session.commit()

    # 一条连接：事后读回的就是清理用过的那一条。
    purge_db = world.db("purge", pool_size=1, max_overflow=0)
    with purge_db.engine.connect() as connection:
        default = connection.exec_driver_sql("SELECT @@transaction_isolation").scalar_one()
        connection_id = connection.exec_driver_sql("SELECT CONNECTION_ID()").scalar_one()
    seen: list[tuple[str, str, int]] = []
    statements: list[str] = []

    def capture(conn, cursor, statement, parameters, context, executemany) -> None:
        sql = _sql(statement)
        if not sql.startswith("delete from "):
            return
        statements.append(sql)
        probe = conn.connection.cursor()
        try:
            probe.execute("SELECT @@transaction_isolation, CONNECTION_ID()")
            level, current = probe.fetchone()
        finally:
            probe.close()
        seen.append((sql.split()[2], level, int(current)))

    event.listen(purge_db.engine, "before_cursor_execute", capture)
    result = world.purge(purge_db)
    event.remove(purge_db.engine, "before_cursor_execute", capture)

    assert result.failed_item is None, result
    assert seen == [
        ("refresh_tokens", "READ-COMMITTED", connection_id),
        ("password_reset_tokens", "READ-COMMITTED", connection_id),
        ("recovery_codes", "READ-COMMITTED", connection_id),
    ]
    # 刷新令牌的 DELETE：派生表带本批候选家族的 IN 条件，只经索引探查这些家族。
    [refresh_delete] = [sql for sql in statements if sql.startswith("delete from refresh_tokens")]
    derived = refresh_delete.split("not in (select live.user_id, live.family_id from (", 1)[1]
    derived = derived.split(") as live", 1)[0]
    assert derived.startswith("select refresh_tokens.user_id")
    assert (
        " from refresh_tokens where (refresh_tokens.user_id, refresh_tokens.family_id) in ("
        in derived
    )
    assert "refresh_tokens.expires_at >= " in derived
    assert "group by" not in refresh_delete

    with purge_db.engine.connect() as connection:
        assert connection.exec_driver_sql("SELECT CONNECTION_ID()").scalar_one() == connection_id
        restored = connection.exec_driver_sql("SELECT @@transaction_isolation").scalar_one()
    assert restored == default


# --- 设计 v5 审查的不阻断更正：两条会写到待删行的业务路径 ----------------------------------


def test_a_reset_voiding_an_expired_sibling_waits_for_the_purge_batch_and_succeeds(
    world: World,
) -> None:
    """重置成功时把同一用户所有未用的兄弟令牌置为已用（不带过期条件），会写到清理批次刚删、
    尚未提交的那一行：它等清理提交，然后照常成功。
    """
    user = world.make_user("sibling")
    world.add_expired_resets(user, 1)
    expired = world.ids(PasswordResetToken)
    raw = world.reset_token("sibling")
    fresh = world.ids(PasswordResetToken) - expired

    purge_db, reset_db = world.db("purge"), world.db("reset")
    gate = world.pause_before_commit(purge_db, "purge: reset batch before commit")
    purging = world.actor(
        "purge", lambda: world.purge(purge_db, {"password_reset_tokens": "delete"})
    )
    gate.wait_reached()
    resetting = world.actor(
        "reset_password",
        lambda: reset_password(
            reset_db.factory,
            world.settings,
            token=raw,
            new_password=NEW_PASSWORD,
            context=CONTEXT,
        ),
    )
    world.wait_until_waiting_on("password_reset_tokens", "reset_password")
    gate.release()
    result = purging.value()
    resetting.value()

    world.purge_completes(result)
    world.assert_no_database_errors(resetting)
    with world.main.factory() as session:
        account = session.get(User, user)
        assert account is not None
        assert verify_password(account.password_hash, NEW_PASSWORD)
        assert account.password_version == 1
    assert world.ids(PasswordResetToken) == fresh
    assert world.purged() == {"password_reset_tokens": 1}
    assert len(world.audits(AuditAction.PASSWORD_RESET)) == 1


def test_replaying_a_token_of_a_dead_family_waits_for_the_purge_batch_and_is_refused_cleanly(
    world: World,
) -> None:
    """出示已死家族里用过的令牌：吊销整个家族的 UPDATE 会写到清理批次刚删、尚未提交的那些行。

    它等清理提交，然后照常走重放处置（写 `TOKEN_REUSED`、401），不是数据库错误。
    """
    user = world.make_user("dead-replay")
    raw = "a-used-token-of-a-dead-family"
    world.add_refresh(user, DEAD_FAMILY, live=False, used=True, raw=raw)
    world.add_refresh(user, DEAD_FAMILY, live=False)

    purge_db, refresh_db = world.db("purge"), world.db("refresh")
    gate = world.pause_before_commit(purge_db, "purge: refresh batch before commit")
    purging = world.actor("purge", lambda: world.purge(purge_db, {"refresh_tokens": "delete"}))
    gate.wait_reached()
    refreshing = world.actor(
        "refresh",
        lambda: refresh_session(refresh_db.factory, world.settings, raw_token=raw, context=CONTEXT),
    )
    world.wait_until_waiting_on("refresh_tokens", "refresh")
    gate.release()
    result = purging.value()
    refreshing.join()

    world.purge_completes(result)
    world.assert_no_database_errors(refreshing)
    assert isinstance(refreshing.error, TokenReused), refreshing.error
    assert [(row.entity_type, row.entity_id) for row in world.audits(AuditAction.TOKEN_REUSED)] == [
        ("refresh_tokens", DEAD_FAMILY)
    ]
    assert world.ids(RefreshToken) == set()
    assert world.purged() == {"refresh_tokens": 2}

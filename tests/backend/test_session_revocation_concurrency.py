"""A password reset racing a login, a refresh, a second factor or a 2FA write (#369 v6 §7).

设计闸门 #369 v6 §7 的**并发**各行（AIH-TASK-088）；非并发各行在
`test_session_revocation.py`。

⚠️ 只能对着真 MySQL 跑：SQLite 方言下 `FOR UPDATE` 被静默忽略，没有行锁，
这里的每一种交错在内存库上都不存在。需要 `BILLING_TEST_DATABASE_URL`，
CI 设了；本地没设时整个文件 skip —— **skip 不是 pass**。

编排（不改产品代码）：

- 暂停点挂在 SQLAlchemy 事件上：`before_cursor_execute` / `after_cursor_execute`
  匹配到目标语句，或 Session 的 `before_commit`，就在 `threading.Event` 上等
- 每个参与方用自己的引擎，暂停点只装在它的引擎上，别人的同一条语句不会被拦
- 「对方确实在等行锁」查 `performance_schema.data_lock_waits`：经 `threads` 的
  `PROCESSLIST_ID` 对上等待方那条连接的 `CONNECTION_ID()`，经 `data_locks`
  确认锁在 `users` 上（50 毫秒一次，最多 10 秒）
- ⚠️ 不用 `information_schema.INNODB_TRX` 判断：它是一份不满 0.1 秒不刷新的
  缓存，50 毫秒轮询会一直读到旧快照（run d56365f0 因此失败）；只在超时诊断里报
- 每一步等待都有超时，超时即失败并说出停在哪一步

匹配语句用的是 MySQL 编译出来的 SQL 文本；整个文件只在 MySQL 上跑，
不涉及 CLAUDE.md 说的「SQLite 与 MySQL 两边都要断言」那种情况。
"""

from __future__ import annotations

import base64
import datetime as dt
import json
import logging
import os
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

import pyotp
import pytest
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, delete, event, select, text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from alembic import command
from app.core.config import Settings
from app.core.database import create_session_factory
from app.core.errors import AppError
from app.core.passwords import hash_password, verify_password
from app.core.tokens import (
    TOKEN_TYPE_ACCESS,
    TOKEN_TYPE_PENDING_2FA,
    decode_token,
    hash_refresh_token,
)
from app.main import create_app
from app.models.auth import (
    AuditAction,
    AuditLog,
    DomainOutbox,
    RecoveryCode,
    RefreshToken,
    TwoFactorSetting,
    User,
    UserRole,
    UserStatus,
)
from app.services.auth import (
    REFRESH_COOKIE_NAME,
    STAGE_ENROL_2FA,
    STAGE_TOTP_REQUIRED,
    IssuedSession,
    RequestContext,
    authenticate,
    complete_second_factor,
    logout,
    token_password_version,
    utc_now,
)
from app.services.password_reset import request_reset, reset_password
from app.services.two_factor import (
    confirm_enrolment,
    regenerate_recovery_codes,
    start_enrolment,
)

TEST_DATABASE_URL = os.environ.get("BILLING_TEST_DATABASE_URL", "")

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="BILLING_TEST_DATABASE_URL is not set; these interleavings need real MySQL row locks",
)

PASSWORD = "a-perfectly-fine-passphrase"
NEW_PASSWORD = "another-perfectly-fine-passphrase"
DOMAIN = "@session-revocation-concurrency.example.com"
ADMIN_EMAIL = f"admin{DOMAIN}"
CUSTOMER_EMAIL = f"customer{DOMAIN}"
EMAILS = (ADMIN_EMAIL, CUSTOMER_EMAIL)
CONTEXT = RequestContext(ip_address="10.0.0.1", user_agent="pytest")

# 任何一个已登录接口都经过 `require_current_user`；挑一个只读的。
LOGGED_IN = "/api/v1/admin/audit-logs"
REUSE_WARNING = "Refresh token reuse detected"

# 暂停点最多停这么久；到点还没人放行，被停住的那一方报错退出。
GATE_TIMEOUT_SECONDS = 15.0
# 「对方在等行锁」的轮询（契约：间隔 50 毫秒、最多 10 秒）。
LOCK_POLL_INTERVAL_SECONDS = 0.05
LOCK_POLL_LIMIT_SECONDS = 10.0
# 本文件的连接都把 innodb_lock_wait_timeout 调到这个值：真卡在锁上时
# 以 1205 报错收场，而不是等默认的 50 秒。
LOCK_WAIT_TIMEOUT_SECONDS = 20
# 等一个参与方结束：比锁等待超时长，锁等待超时会先以错误的形式出现。
JOIN_TIMEOUT_SECONDS = 40.0

_ER_LOCK_DEADLOCK = 1213
_CONNECTION_ID = "connection_id"

_WAITING_ON_TABLE = text(
    "SELECT COUNT(*) FROM performance_schema.data_lock_waits AS w"
    " JOIN performance_schema.threads AS t ON t.THREAD_ID = w.REQUESTING_THREAD_ID"
    " JOIN performance_schema.data_locks AS l"
    " ON l.ENGINE = w.ENGINE AND l.ENGINE_LOCK_ID = w.REQUESTING_ENGINE_LOCK_ID"
    " WHERE t.PROCESSLIST_ID = :connection_id"
    " AND l.OBJECT_SCHEMA = DATABASE() AND l.OBJECT_NAME = :table_name"
)

# 只用于超时诊断。INNODB_TRX 那份缓存不准，但报出来仍有参考价值。
_LOCK_REPORT = (
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


# --- statements the gates wait on -------------------------------------------------


def _sql(statement: str) -> str:
    return " ".join(statement.replace("`", "").lower().split())


def locks_user(statement: str) -> bool:
    """`SELECT … FROM users … FOR UPDATE` — every path's first lock (§2「统一加锁顺序」)."""
    sql = _sql(statement)
    return sql.startswith("select ") and " from users " in sql and sql.endswith(" for update")


def marks_refresh_token_used(statement: str) -> bool:
    """The conditional UPDATE that consumes a refresh token."""
    return _sql(statement).startswith("update refresh_tokens set used_at")


def inserts_refresh_token(statement: str) -> bool:
    return _sql(statement).startswith("insert into refresh_tokens")


def reads_two_factor_setting(statement: str) -> bool:
    """In `authenticate` this read comes after `verify_password` and before any token."""
    sql = _sql(statement)
    return sql.startswith("select ") and " from two_factor_settings " in sql


# --- threads and gates ------------------------------------------------------------


class GateTimeout(AssertionError):
    """Raised in the paused thread when nobody released its gate in time."""


class Gate:
    """One pause point: the first matching statement waits here until `release()`."""

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
    """One engine. Each side of a race gets its own, so a gate only stops that side."""

    name: str
    engine: Engine
    factory: sessionmaker[Session]
    # `CONNECTION_ID()` of each connection that sent `SELECT … FROM users … FOR UPDATE`.
    lockers: list[int | None] = field(default_factory=list)
    errors: list[BaseException] = field(default_factory=list)


@dataclass
class Account:
    secret: str
    codes: list[str]


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


class World:
    """Engines, an app per engine, the services, and what the database looks like."""

    def __init__(self, url: str, settings: Settings) -> None:
        self.url = url
        self.settings = settings
        self._dbs: list[Db] = []
        self._gates: list[Gate] = []
        self._actors: list[Actor] = []
        self.observer = create_engine(url, isolation_level="AUTOCOMMIT", pool_size=1)
        self.main = self.db("main")
        self.reset_db = self.db("reset")
        self.client = self.client_on(self.main)
        # 重置与登出显式传入时刻，行上的 `revoked_at` 因此认得出是谁写的。
        # 取整到秒：DATETIME 列不存小数秒。
        moment = utc_now().replace(microsecond=0)
        self.reset_at = moment - dt.timedelta(minutes=1)
        self.logout_at = moment - dt.timedelta(minutes=2)

    # -- plumbing --

    def db(self, name: str) -> Db:
        engine = create_engine(self.url, pool_size=4, max_overflow=4)
        db = Db(name=name, engine=engine, factory=create_session_factory(engine))
        event.listen(engine, "connect", _prepare_connection)

        def note_locker(conn, cursor, statement, parameters, context, executemany) -> None:
            if locks_user(statement):
                db.lockers.append(conn.info.get(_CONNECTION_ID))

        def note_error(exception_context) -> None:
            db.errors.append(exception_context.original_exception)

        event.listen(engine, "before_cursor_execute", note_locker)
        event.listen(engine, "handle_error", note_error)
        self._dbs.append(db)
        return db

    def client_on(self, db: Db) -> TestClient:
        app = create_app(self.settings)
        app.state.engine = db.engine
        app.state.session_factory = db.factory
        # 死锁兜底那条要看 500，而不是让异常冒到用例里。
        return TestClient(app, raise_server_exceptions=False)

    def actor(self, name: str, work: Callable[[], Any]) -> Actor:
        actor = Actor(name, work)
        self._actors.append(actor)
        return actor

    def _gate(self, step: str) -> Gate:
        gate = Gate(step)
        self._gates.append(gate)
        return gate

    def pause_before(self, db: Db, step: str, matches: Callable[[str], bool]) -> Gate:
        gate = self._gate(step)

        def listener(conn, cursor, statement, parameters, context, executemany) -> None:
            if matches(statement) and gate.claim():
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
                # commit 的第一步本来就是 flush。先做掉它，停下时写入已经发到库里，
                # 只差提交 —— 这才是「写入方在提交之前暂停」。
                session.flush()
                gate.hold()

        event.listen(db.factory, "before_commit", listener)
        return gate

    def wait_until_waiting_for_users(self, db: Db, who: str) -> None:
        """Poll until `who`'s own connection is waiting for a row lock on `users`."""
        deadline = time.monotonic() + LOCK_POLL_LIMIT_SECONDS
        connection_id = None
        with self.observer.connect() as connection:
            while True:
                connection_id = db.lockers[-1] if db.lockers else None
                if connection_id is not None:
                    waiting = connection.execute(
                        _WAITING_ON_TABLE,
                        {"connection_id": connection_id, "table_name": "users"},
                    ).scalar_one()
                    if waiting:
                        return
                if time.monotonic() >= deadline:
                    break
                time.sleep(LOCK_POLL_INTERVAL_SECONDS)
            report = _lock_report(connection)
        pytest.fail(
            f"{who} (connection {connection_id}) was not seen waiting for a users row lock"
            f" within {LOCK_POLL_LIMIT_SECONDS}s\n{report}"
        )

    def assert_no_database_errors(self, *actors: Actor) -> None:
        """No OperationalError anywhere: not on any engine, not out of any thread."""
        for db in self._dbs:
            assert db.errors == [], f"database errors on {db.name!r}: {db.errors!r}"
        for actor in actors:
            assert not isinstance(actor.error, OperationalError), (
                f"{actor.name!r} hit {actor.error!r}"
            )

    def close(self) -> None:
        for gate in self._gates:
            gate.release()
        for actor in self._actors:
            actor.settle(5)
        for db in self._dbs:
            db.engine.dispose()
        self.observer.dispose()

    # -- setting up --

    def make_user(self, email: str, role: UserRole) -> int:
        now = utc_now()
        with self.main.factory() as session:
            user = User(
                email=email,
                password_hash=hash_password(PASSWORD),
                role=role,
                status=UserStatus.ACTIVE,
                created_at=now,
                updated_at=now,
            )
            session.add(user)
            session.commit()
            return int(user.id)

    def enrol(self, user_id: int) -> Account:
        enrolment = start_enrolment(
            self.main.factory, self.settings, user_id=user_id, password_version=0
        )
        codes = confirm_enrolment(
            self.main.factory,
            self.settings,
            user_id=user_id,
            password_version=0,
            code=pyotp.TOTP(enrolment.secret).now(),
            context=CONTEXT,
        )
        return Account(secret=enrolment.secret, codes=list(codes))

    def pending(self, email: str, stage: str = STAGE_TOTP_REQUIRED) -> str:
        outcome = authenticate(
            self.main.factory, self.settings, email=email, password=PASSWORD, context=CONTEXT
        )
        assert outcome.stage == stage
        assert outcome.pending_token is not None
        return outcome.pending_token

    def sign_in(self, email: str, account: Account) -> IssuedSession:
        """Both steps, with a recovery code (a TOTP code only works once per 30 seconds)."""
        issued, _ = complete_second_factor(
            self.main.factory,
            self.settings,
            pending_token=self.pending(email),
            code=account.codes.pop(),
            context=CONTEXT,
        )
        return issued

    def reset_token(self, email: str) -> str:
        outbox_id = request_reset(self.main.factory, self.settings, email=email, context=CONTEXT)
        assert outbox_id is not None
        with self.main.factory() as session:
            row = session.get(DomainOutbox, outbox_id)
            assert row is not None and row.payload_json is not None
            return str(json.loads(row.payload_json)["token"])

    def reset(self, token: str) -> None:
        """`reset_password` itself, on its own engine (so its lock wait can be seen)."""
        reset_password(
            self.reset_db.factory,
            self.settings,
            token=token,
            new_password=NEW_PASSWORD,
            context=CONTEXT,
            now=self.reset_at,
        )

    # -- the two orders for a write of authentication material --

    def reset_commits_first(self, who: str, token: str, write: Callable[[sessionmaker], Any]):
        """The writer stops before locking the user; the reset commits; the writer goes on."""
        writer_db = self.db(who)
        gate = self.pause_before(writer_db, f"{who}: before locking the user", locks_user)
        writer = self.actor(who, lambda: write(writer_db.factory))
        gate.wait_reached()
        self.actor("reset_password", lambda: self.reset(token)).value()
        gate.release()
        return writer.join()

    def write_commits_first(self, who: str, token: str, write: Callable[[sessionmaker], Any]):
        """The writer stops before committing; the reset starts and waits; the writer commits."""
        writer_db = self.db(who)
        gate = self.pause_before_commit(writer_db, f"{who}: before commit")
        writer = self.actor(who, lambda: write(writer_db.factory))
        gate.wait_reached()
        resetting = self.actor("reset_password", lambda: self.reset(token))
        self.wait_until_waiting_for_users(self.reset_db, "reset_password")
        gate.release()
        writer.join()
        resetting.value()
        return writer

    # -- reading back --

    def pwv(self, token: str, kind: str) -> int:
        return token_password_version(decode_token(self.settings, token, expected_type=kind))

    def user(self, user_id: int) -> User:
        with self.main.factory() as session:
            user = session.get(User, user_id)
            assert user is not None
            session.expunge(user)
            return user

    def refresh_rows(self, user_id: int) -> list[RefreshToken]:
        with self.main.factory() as session:
            rows = list(
                session.execute(
                    select(RefreshToken)
                    .where(RefreshToken.user_id == user_id)
                    .order_by(RefreshToken.id)
                ).scalars()
            )
            for row in rows:
                session.expunge(row)
            return rows

    def refresh_row(self, raw: str) -> RefreshToken:
        with self.main.factory() as session:
            row = session.execute(
                select(RefreshToken).where(RefreshToken.token_hash == hash_refresh_token(raw))
            ).scalar_one()
            session.expunge(row)
            return row

    def audits(self, action: AuditAction) -> list[AuditLog]:
        with self.main.factory() as session:
            rows = list(
                session.execute(
                    select(AuditLog).where(AuditLog.action == action).order_by(AuditLog.id)
                ).scalars()
            )
            for row in rows:
                session.expunge(row)
            return rows

    def two_factor(self, user_id: int) -> TwoFactorSetting | None:
        with self.main.factory() as session:
            row = session.get(TwoFactorSetting, user_id)
            if row is not None:
                session.expunge(row)
            return row

    def live_code_ids(self, user_id: int) -> list[int]:
        with self.main.factory() as session:
            return list(
                session.execute(
                    select(RecoveryCode.id)
                    .where(
                        RecoveryCode.user_id == user_id,
                        RecoveryCode.used_at.is_(None),
                        RecoveryCode.revoked_at.is_(None),
                    )
                    .order_by(RecoveryCode.id)
                ).scalars()
            )

    def used_code_ids(self, user_id: int) -> list[int]:
        with self.main.factory() as session:
            return list(
                session.execute(
                    select(RecoveryCode.id).where(
                        RecoveryCode.user_id == user_id, RecoveryCode.used_at.is_not(None)
                    )
                ).scalars()
            )

    def all_code_ids(self, user_id: int) -> list[int]:
        with self.main.factory() as session:
            return list(
                session.execute(
                    select(RecoveryCode.id).where(RecoveryCode.user_id == user_id)
                ).scalars()
            )


@pytest.fixture
def world(tmp_path, monkeypatch) -> Iterator[World]:
    # ⚠️ 建表走 alembic，不用 `create_all`（原因见 test_password_reset_concurrency.py）。
    from app.core.config import get_settings

    url = os.environ["BILLING_TEST_DATABASE_URL"]
    monkeypatch.setenv("BILLING_DATABASE_URL", url)
    get_settings.cache_clear()
    command.upgrade(Config("alembic.ini"), "head")
    get_settings.cache_clear()

    jwt_key = tmp_path / "jwt.key"
    jwt_key.write_text("a-test-signing-key-that-is-long-enough", encoding="utf-8")
    master = tmp_path / "master.key"
    master.write_text(f"1:{base64.b64encode(os.urandom(32)).decode()}\n", encoding="utf-8")
    settings = Settings(
        # 空着：应用不自己建引擎，每个应用绑到用例给的那个引擎上。
        database_url="",
        redis_url="",
        jwt_secret_file=str(jwt_key),
        master_key_file=str(master),
        session_cookie_secure=False,
        auth_rate_limit_per_minute=600,
        auth_rate_limit_burst=200,
    )

    created = World(url, settings)
    # ⚠️ 每个用例自己清场。审计表用 TRUNCATE：它有只追加的触发器，DELETE 会被拒。
    # 用户行一删，令牌、2FA、恢复码、重置令牌随外键级联删除。
    with created.observer.connect() as connection:
        connection.execute(text("TRUNCATE TABLE audit_logs"))
    with created.main.factory() as session:
        ids = [
            str(user_id)
            for user_id in session.execute(select(User.id).where(User.email.in_(EMAILS))).scalars()
        ]
        if ids:
            session.execute(
                delete(DomainOutbox).where(
                    DomainOutbox.aggregate_type == "users", DomainOutbox.aggregate_id.in_(ids)
                )
            )
        session.execute(delete(User).where(User.email.in_(EMAILS)))
        session.commit()
    try:
        yield created
    finally:
        created.close()


@pytest.fixture
def reuse_warnings() -> Iterator[list[logging.LogRecord]]:
    """Collect the theft alert T0.9 hangs off (`app/services/auth.py`)."""
    records: list[logging.LogRecord] = []

    class _Collect(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            if record.getMessage() == REUSE_WARNING:
                records.append(record)

    handler = _Collect(level=logging.WARNING)
    logger = logging.getLogger("app.services.auth")
    logger.addHandler(handler)
    try:
        yield records
    finally:
        logger.removeHandler(handler)


# --- HTTP helpers -----------------------------------------------------------------


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def error_code(response) -> str:  # noqa: ANN001 - httpx.Response
    return response.json()["error"]["code"]


def refresh_via(client: TestClient, raw: str):  # noqa: ANN201 - httpx.Response
    # 显式带 Cookie 头：每个参与方的客户端不共用 cookie jar，也不依赖它。
    return client.post("/api/v1/auth/refresh", headers={"Cookie": f"{REFRESH_COOKIE_NAME}={raw}"})


def second_step(client: TestClient, pending: str, code: str):  # noqa: ANN201 - httpx.Response
    return client.post("/api/v1/auth/login/totp", json={"pending_token": pending, "code": code})


def assert_refused(response, status: int, code: str) -> None:  # noqa: ANN001
    assert response.status_code == status, response.text
    assert error_code(response) == code, response.text


def assert_token_invalid(error: BaseException | None) -> None:
    """What the API turns into 401 TOKEN_INVALID（设计写的 INVALID_TOKEN，勘误）."""
    assert isinstance(error, AppError), f"expected TOKEN_INVALID, got {error!r}"
    assert error.code == "TOKEN_INVALID"
    assert error.http_status == 401


def wrong_totp(secret: str) -> str:
    """Six digits no accepted time step matches."""
    totp = pyotp.TOTP(secret)
    now = time.time()
    accepted = {totp.at(now + offset * totp.interval) for offset in range(-2, 3)}
    return next(code for n in range(1_000_000) if (code := f"{n:06d}") not in accepted)


# --- 登录与改密码并发 ---------------------------------------------------------------


def test_a_login_past_the_password_check_when_a_reset_commits_hands_out_dead_tokens(
    world: World, reuse_warnings: list[logging.LogRecord]
) -> None:
    """登录在 `verify_password` 之后、签发之前停住；重置提交；放行。

    登录照常返回令牌，令牌在每个使用点都 401；刷新令牌行上的版本是旧值。
    （未启用 2FA 的 CUSTOMER：这一步就签出访问令牌和刷新令牌行。）
    """
    user_id = world.make_user(CUSTOMER_EMAIL, UserRole.CUSTOMER)
    token = world.reset_token(CUSTOMER_EMAIL)
    login_db = world.db("login")
    gate = world.pause_before(
        login_db, "login: password checked, nothing issued yet", reads_two_factor_setting
    )
    client = world.client_on(login_db)
    logging_in = world.actor(
        "login",
        lambda: client.post(
            "/api/v1/auth/login", json={"email": CUSTOMER_EMAIL, "password": PASSWORD}
        ),
    )
    gate.wait_reached()
    world.actor("reset_password", lambda: world.reset(token)).value()
    gate.release()
    response = logging_in.value()

    assert response.status_code == 200, response.text
    access = response.json()["data"]["access_token"]
    refresh = response.cookies.get(REFRESH_COOKIE_NAME)
    assert access and refresh
    assert world.pwv(access, TOKEN_TYPE_ACCESS) == 0

    user = world.user(user_id)
    assert user.password_version == 1
    assert verify_password(user.password_hash, NEW_PASSWORD)
    [row] = world.refresh_rows(user_id)
    assert row.token_hash == hash_refresh_token(refresh)
    assert row.password_version == 0, "刷新令牌行上是登录时读到的旧版本"
    # 它在重置提交之后才插入，重置吊销不到它 —— 拦住它的是版本号。
    assert row.revoked_at is None

    assert_refused(world.client.get(LOGGED_IN, headers=bearer(access)), 401, "TOKEN_INVALID")
    assert_refused(refresh_via(world.client, refresh), 401, "TOKEN_REUSED")
    after = world.refresh_row(refresh)
    assert after.used_at is None and after.revoked_at is None
    assert len(world.refresh_rows(user_id)) == 1, "不签出新行"
    assert world.audits(AuditAction.TOKEN_REUSED) == []
    assert reuse_warnings == []

    # 对照：新密码换来的令牌过得了 `require_current_user`（CUSTOMER 进管理端是 403）。
    fresh = world.client.post(
        "/api/v1/auth/login", json={"email": CUSTOMER_EMAIL, "password": NEW_PASSWORD}
    )
    assert fresh.status_code == 200, fresh.text
    fresh_access = fresh.json()["data"]["access_token"]
    assert world.pwv(fresh_access, TOKEN_TYPE_ACCESS) == 1
    assert_refused(world.client.get(LOGGED_IN, headers=bearer(fresh_access)), 403, "ADMIN_REQUIRED")
    world.assert_no_database_errors(logging_in)


def test_a_pending_token_from_a_login_racing_a_reset_is_dead(world: World) -> None:
    """同一交错，已启用 2FA 的 ADMIN：签出的 pending 令牌带旧版本，`/login/totp` 401。"""
    user_id = world.make_user(ADMIN_EMAIL, UserRole.ADMIN)
    account = world.enrol(user_id)
    codes_before = world.live_code_ids(user_id)
    token = world.reset_token(ADMIN_EMAIL)
    login_db = world.db("login")
    gate = world.pause_before(
        login_db, "login: password checked, nothing issued yet", reads_two_factor_setting
    )
    client = world.client_on(login_db)
    logging_in = world.actor(
        "login",
        lambda: client.post(
            "/api/v1/auth/login", json={"email": ADMIN_EMAIL, "password": PASSWORD}
        ),
    )
    gate.wait_reached()
    world.actor("reset_password", lambda: world.reset(token)).value()
    gate.release()
    response = logging_in.value()

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["stage"] == STAGE_TOTP_REQUIRED
    assert world.pwv(data["pending_token"], TOKEN_TYPE_PENDING_2FA) == 0
    assert world.user(user_id).password_version == 1

    for code in (account.codes[0], pyotp.TOTP(account.secret).now()):
        assert_refused(
            second_step(world.client, data["pending_token"], code), 401, "INVALID_CREDENTIALS"
        )
    assert world.live_code_ids(user_id) == codes_before
    setting = world.two_factor(user_id)
    assert setting is not None and setting.last_used_counter is None
    assert world.refresh_rows(user_id) == []
    world.assert_no_database_errors(logging_in)


# --- 刷新与改密码并发 ---------------------------------------------------------------


def test_a_refresh_paused_before_the_user_lock_loses_to_a_reset_without_a_replay_alarm(
    world: World, reuse_warnings: list[logging.LogRecord]
) -> None:
    """① 刷新读出令牌行之后、锁用户行之前停住；重置提交后放行。

    401 TOKEN_REUSED；没有 TOKEN_REUSED 审计、没有 warning；行只被重置吊销。
    """
    user_id = world.make_user(ADMIN_EMAIL, UserRole.ADMIN)
    account = world.enrol(user_id)
    device_a = world.sign_in(ADMIN_EMAIL, account)
    world.sign_in(ADMIN_EMAIL, account)  # 设备 B：另一条链
    token = world.reset_token(ADMIN_EMAIL)

    refresh_db = world.db("refresh")
    gate = world.pause_before(
        refresh_db, "refresh: token row read, user not locked yet", locks_user
    )
    client = world.client_on(refresh_db)
    refreshing = world.actor("refresh", lambda: refresh_via(client, device_a.refresh_token))
    gate.wait_reached()
    world.actor("reset_password", lambda: world.reset(token)).value()
    gate.release()
    response = refreshing.value()

    assert_refused(response, 401, "TOKEN_REUSED")
    assert world.user(user_id).password_version == 1
    rows = world.refresh_rows(user_id)
    assert len(rows) == 2, "不签出新行"
    # 两条链的每一行都只带着重置写下的吊销时刻：刷新这边没有再吊销什么。
    assert [row.revoked_at for row in rows] == [world.reset_at, world.reset_at]
    assert all(row.used_at is None for row in rows)
    assert world.audits(AuditAction.TOKEN_REUSED) == []
    assert reuse_warnings == []
    world.assert_no_database_errors(refreshing)


def test_a_refresh_that_commits_before_a_reset_hands_out_tokens_the_reset_kills(
    world: World, reuse_warnings: list[logging.LogRecord]
) -> None:
    """② 同一暂停点，先放行刷新、刷新提交后再重置：刷新照常，重置后新令牌都 401。"""
    user_id = world.make_user(ADMIN_EMAIL, UserRole.ADMIN)
    account = world.enrol(user_id)
    session = world.sign_in(ADMIN_EMAIL, account)
    token = world.reset_token(ADMIN_EMAIL)

    refresh_db = world.db("refresh")
    gate = world.pause_before(
        refresh_db, "refresh: token row read, user not locked yet", locks_user
    )
    client = world.client_on(refresh_db)
    refreshing = world.actor("refresh", lambda: refresh_via(client, session.refresh_token))
    gate.wait_reached()
    gate.release()
    response = refreshing.value()
    assert response.status_code == 200, response.text
    access = response.json()["data"]["access_token"]
    rotated = response.cookies.get(REFRESH_COOKIE_NAME)
    assert access and rotated
    assert world.pwv(access, TOKEN_TYPE_ACCESS) == 0

    world.actor("reset_password", lambda: world.reset(token)).value()

    assert world.user(user_id).password_version == 1
    old, new = world.refresh_row(session.refresh_token), world.refresh_row(rotated)
    assert old.used_at is not None
    assert new.password_version == 0
    assert new.used_at is None and new.revoked_at == world.reset_at
    assert_refused(world.client.get(LOGGED_IN, headers=bearer(access)), 401, "TOKEN_INVALID")
    assert_refused(refresh_via(world.client, rotated), 401, "TOKEN_REUSED")
    assert len(world.refresh_rows(user_id)) == 2
    assert world.audits(AuditAction.TOKEN_REUSED) == []
    assert reuse_warnings == []
    world.assert_no_database_errors(refreshing)


# --- 刷新持锁时改密码（原死锁交错） -------------------------------------------------


def test_a_reset_waits_for_a_refresh_holding_the_user_lock_and_no_deadlock_occurs(
    world: World, reuse_warnings: list[logging.LogRecord]
) -> None:
    """刷新已锁用户行、已条件 UPDATE 旧行、尚未插入新行时停住；重置开始（等用户锁）；放行。

    原先刷新先锁令牌行、插入新行时才碰用户行，与重置成环。现在刷新先提交，
    重置随后拿到锁并提交；新的访问令牌与刷新令牌都 401。
    """
    user_id = world.make_user(ADMIN_EMAIL, UserRole.ADMIN)
    account = world.enrol(user_id)
    session = world.sign_in(ADMIN_EMAIL, account)
    token = world.reset_token(ADMIN_EMAIL)

    refresh_db = world.db("refresh")
    gate = world.pause_after(
        refresh_db, "refresh: user locked, old row marked used", marks_refresh_token_used
    )
    client = world.client_on(refresh_db)
    refreshing = world.actor("refresh", lambda: refresh_via(client, session.refresh_token))
    gate.wait_reached()
    resetting = world.actor("reset_password", lambda: world.reset(token))
    world.wait_until_waiting_for_users(world.reset_db, "reset_password")
    gate.release()
    response = refreshing.value()
    resetting.value()

    assert response.status_code == 200, response.text
    access = response.json()["data"]["access_token"]
    rotated = response.cookies.get(REFRESH_COOKIE_NAME)
    assert access and rotated
    world.assert_no_database_errors(refreshing, resetting)

    assert world.user(user_id).password_version == 1
    old, new = world.refresh_row(session.refresh_token), world.refresh_row(rotated)
    assert old.used_at is not None
    assert new.password_version == 0
    assert new.revoked_at == world.reset_at, "重置在刷新之后提交，吊销了新行"
    assert_refused(world.client.get(LOGGED_IN, headers=bearer(access)), 401, "TOKEN_INVALID")
    assert_refused(refresh_via(world.client, rotated), 401, "TOKEN_REUSED")
    assert world.audits(AuditAction.TOKEN_REUSED) == []
    assert reuse_warnings == []


# --- 死锁兜底 -----------------------------------------------------------------------


class _MySqlDeadlock(Exception):
    """Stands in for the driver's error: same `args` shape, `(1213, message)`."""


def test_a_deadlock_while_refreshing_rolls_everything_back_and_the_token_still_works(
    world: World, reuse_warnings: list[logging.LogRecord]
) -> None:
    """刷新在插入新行时抛 1213：500 带 request_id；整体回滚；同一刷新令牌重试成功。"""
    user_id = world.make_user(ADMIN_EMAIL, UserRole.ADMIN)
    account = world.enrol(user_id)
    session = world.sign_in(ADMIN_EMAIL, account)
    rows_before = len(world.refresh_rows(user_id))

    refresh_db = world.db("refresh")
    injected: list[str] = []

    def deadlock_on_insert(conn, cursor, statement, parameters, context, executemany) -> None:
        if inserts_refresh_token(statement) and not injected:
            injected.append(statement)
            raise OperationalError(
                statement,
                parameters,
                _MySqlDeadlock(
                    _ER_LOCK_DEADLOCK,
                    "Deadlock found when trying to get lock; try restarting transaction",
                ),
            )

    event.listen(refresh_db.engine, "before_cursor_execute", deadlock_on_insert)
    client = world.client_on(refresh_db)

    response = refresh_via(client, session.refresh_token)
    assert injected, "插入新行那一步没有被走到"
    assert response.status_code == 500, response.text
    body = response.json()
    assert body["error"]["code"] == "INTERNAL_ERROR"
    assert body["request_id"]
    assert response.headers.get("X-Request-ID") == body["request_id"]

    row = world.refresh_row(session.refresh_token)
    assert row.used_at is None, "回滚连同标记已用"
    assert row.revoked_at is None
    assert len(world.refresh_rows(user_id)) == rows_before, "没有新行"
    assert world.audits(AuditAction.TOKEN_REUSED) == []
    assert reuse_warnings == []

    retried = refresh_via(client, session.refresh_token)
    assert retried.status_code == 200, retried.text
    rotated = retried.cookies.get(REFRESH_COOKIE_NAME)
    assert rotated
    assert world.refresh_row(session.refresh_token).used_at is not None
    assert world.refresh_row(rotated).password_version == 0
    assert len(world.refresh_rows(user_id)) == rows_before + 1
    assert world.audits(AuditAction.TOKEN_REUSED) == []
    assert reuse_warnings == []


# --- 刷新与登出并发 -----------------------------------------------------------------


def test_a_refresh_racing_a_logout_is_refused_without_a_replay_alarm(
    world: World, reuse_warnings: list[logging.LogRecord]
) -> None:
    """刷新锁了用户行、条件 UPDATE 之前停住；另一个连接登出同一家族；放行。

    401 TOKEN_REUSED，没有重放审计（修正既有误报）。登出不碰用户行，
    所以它在刷新持锁期间就能完成。
    """
    user_id = world.make_user(ADMIN_EMAIL, UserRole.ADMIN)
    account = world.enrol(user_id)
    device_a = world.sign_in(ADMIN_EMAIL, account)
    device_b = world.sign_in(ADMIN_EMAIL, account)

    refresh_db = world.db("refresh")
    gate = world.pause_before(
        refresh_db, "refresh: user locked, old row not marked yet", marks_refresh_token_used
    )
    client = world.client_on(refresh_db)
    refreshing = world.actor("refresh", lambda: refresh_via(client, device_a.refresh_token))
    gate.wait_reached()
    logging_out = world.actor(
        "logout",
        lambda: logout(
            world.main.factory,
            raw_token=device_a.refresh_token,
            context=CONTEXT,
            now=world.logout_at,
        ),
    )
    logging_out.value()
    gate.release()
    response = refreshing.value()

    assert_refused(response, 401, "TOKEN_REUSED")
    presented = world.refresh_row(device_a.refresh_token)
    assert presented.used_at is None
    assert presented.revoked_at == world.logout_at, "只被登出吊销"
    other = world.refresh_row(device_b.refresh_token)
    assert other.used_at is None and other.revoked_at is None, "另一条链不受影响"
    assert len(world.refresh_rows(user_id)) == 2, "不签出新行"
    assert len(world.audits(AuditAction.LOGOUT)) == 1
    assert world.audits(AuditAction.TOKEN_REUSED) == []
    assert reuse_warnings == []
    world.assert_no_database_errors(refreshing, logging_out)


# --- 真正的重放不受影响（两个并发刷新抢同一行） -------------------------------------


def test_two_refreshes_racing_for_one_row_is_still_a_replay(
    world: World, reuse_warnings: list[logging.LogRecord]
) -> None:
    """与现在一致：一个成功，另一个走 `_treat_as_replay`（整条链吊销、写审计、记 warning）。

    第一个持有用户锁时停住；第二个已经不加锁读到「未用」的快照，卡在用户锁上；
    放行第一个。第二个拿到锁后条件更新落空，加锁重读看到已用。
    """
    user_id = world.make_user(ADMIN_EMAIL, UserRole.ADMIN)
    account = world.enrol(user_id)
    session = world.sign_in(ADMIN_EMAIL, account)

    first_db, second_db = world.db("first refresh"), world.db("second refresh")
    gate = world.pause_after(first_db, "first refresh: holding the user lock", locks_user)
    first_client, second_client = world.client_on(first_db), world.client_on(second_db)
    first = world.actor("first refresh", lambda: refresh_via(first_client, session.refresh_token))
    gate.wait_reached()
    second = world.actor(
        "second refresh", lambda: refresh_via(second_client, session.refresh_token)
    )
    world.wait_until_waiting_for_users(second_db, "second refresh")
    gate.release()
    winner, loser = first.value(), second.value()

    assert winner.status_code == 200, winner.text
    assert_refused(loser, 401, "TOKEN_REUSED")
    family = world.refresh_row(session.refresh_token).family_id
    in_family = [row for row in world.refresh_rows(user_id) if row.family_id == family]
    assert len(in_family) == 2
    assert all(row.revoked_at is not None for row in in_family), "整条链吊销"
    assert [row.reason for row in world.audits(AuditAction.TOKEN_REUSED)] == ["RACE_LOST"]
    assert len(reuse_warnings) == 1
    world.assert_no_database_errors(first, second)


# --- 认证材料写入与改密码并发 -------------------------------------------------------


def _start(world: World, user_id: int) -> Callable[[sessionmaker], Any]:
    return lambda factory: start_enrolment(
        factory, world.settings, user_id=user_id, password_version=0
    )


def test_start_enrolment_after_a_reset_commits_is_refused(world: World) -> None:
    """改密码先提交：401 TOKEN_INVALID，不写任何 2FA 行。"""
    user_id = world.make_user(ADMIN_EMAIL, UserRole.ADMIN)
    stale = world.pending(ADMIN_EMAIL, STAGE_ENROL_2FA)
    assert world.pwv(stale, TOKEN_TYPE_PENDING_2FA) == 0
    token = world.reset_token(ADMIN_EMAIL)

    writer = world.reset_commits_first("start_enrolment", token, _start(world, user_id))

    assert_token_invalid(writer.error)
    assert world.two_factor(user_id) is None
    assert world.user(user_id).password_version == 1
    world.assert_no_database_errors(writer)


def test_start_enrolment_that_commits_before_a_reset_stands_and_the_old_session_dies(
    world: World,
) -> None:
    """写入先提交：注册成立（PENDING）；之后原来的 pending 令牌在两个注册接口都 401。"""
    user_id = world.make_user(ADMIN_EMAIL, UserRole.ADMIN)
    stale = world.pending(ADMIN_EMAIL, STAGE_ENROL_2FA)
    token = world.reset_token(ADMIN_EMAIL)

    writer = world.write_commits_first("start_enrolment", token, _start(world, user_id))

    enrolment = writer.value()
    setting = world.two_factor(user_id)
    assert setting is not None
    assert setting.secret_version == enrolment.secret_version == 1
    assert setting.confirmed_at is None
    assert world.user(user_id).password_version == 1
    world.assert_no_database_errors(writer)

    assert_refused(
        world.client.post("/api/v1/auth/2fa/enrol", json={"pending_token": stale}),
        401,
        "TOKEN_INVALID",
    )
    assert_refused(
        world.client.post(
            "/api/v1/auth/2fa/confirm",
            json={"pending_token": stale, "code": pyotp.TOTP(enrolment.secret).now()},
        ),
        401,
        "TOKEN_INVALID",
    )
    after = world.two_factor(user_id)
    assert after is not None
    assert after.secret_version == 1 and after.confirmed_at is None
    assert world.all_code_ids(user_id) == []


def _confirm(world: World, user_id: int, secret: str) -> Callable[[sessionmaker], Any]:
    return lambda factory: confirm_enrolment(
        factory,
        world.settings,
        user_id=user_id,
        password_version=0,
        code=pyotp.TOTP(secret).now(),
        context=CONTEXT,
    )


def test_confirm_enrolment_after_a_reset_commits_is_refused(world: World) -> None:
    """改密码先提交：401 TOKEN_INVALID；2FA 不启用、不发恢复码、不写启用审计。"""
    user_id = world.make_user(ADMIN_EMAIL, UserRole.ADMIN)
    enrolment = start_enrolment(
        world.main.factory, world.settings, user_id=user_id, password_version=0
    )
    token = world.reset_token(ADMIN_EMAIL)

    writer = world.reset_commits_first(
        "confirm_enrolment", token, _confirm(world, user_id, enrolment.secret)
    )

    assert_token_invalid(writer.error)
    setting = world.two_factor(user_id)
    assert setting is not None and setting.confirmed_at is None
    assert world.all_code_ids(user_id) == []
    assert world.audits(AuditAction.TWO_FACTOR_ENABLED) == []
    assert world.user(user_id).password_version == 1
    world.assert_no_database_errors(writer)


def test_confirm_enrolment_that_commits_before_a_reset_stands_and_the_old_session_dies(
    world: World,
) -> None:
    """写入先提交：2FA 启用、十个恢复码；之后原来的 pending 令牌 `/login/totp` 401。"""
    user_id = world.make_user(ADMIN_EMAIL, UserRole.ADMIN)
    stale = world.pending(ADMIN_EMAIL, STAGE_ENROL_2FA)
    enrolment = start_enrolment(
        world.main.factory, world.settings, user_id=user_id, password_version=0
    )
    token = world.reset_token(ADMIN_EMAIL)

    writer = world.write_commits_first(
        "confirm_enrolment", token, _confirm(world, user_id, enrolment.secret)
    )

    codes = writer.value()
    assert len(codes) == 10
    setting = world.two_factor(user_id)
    assert setting is not None and setting.confirmed_at is not None
    live = world.live_code_ids(user_id)
    assert len(live) == 10
    assert len(world.audits(AuditAction.TWO_FACTOR_ENABLED)) == 1
    assert world.user(user_id).password_version == 1
    world.assert_no_database_errors(writer)

    assert_refused(second_step(world.client, stale, codes[0]), 401, "INVALID_CREDENTIALS")
    assert world.live_code_ids(user_id) == live, "恢复码不被消耗"
    assert world.refresh_rows(user_id) == []


def _regenerate(world: World, user_id: int) -> Callable[[sessionmaker], Any]:
    return lambda factory: regenerate_recovery_codes(
        factory, user_id=user_id, password_version=0, context=CONTEXT
    )


def test_regenerating_recovery_codes_after_a_reset_commits_is_refused(world: World) -> None:
    """改密码先提交：401 TOKEN_INVALID，恢复码不替换、不写审计。"""
    user_id = world.make_user(ADMIN_EMAIL, UserRole.ADMIN)
    account = world.enrol(user_id)
    world.sign_in(ADMIN_EMAIL, account)
    codes_before = world.live_code_ids(user_id)
    token = world.reset_token(ADMIN_EMAIL)

    writer = world.reset_commits_first(
        "regenerate_recovery_codes", token, _regenerate(world, user_id)
    )

    assert_token_invalid(writer.error)
    assert world.live_code_ids(user_id) == codes_before
    assert world.audits(AuditAction.RECOVERY_CODES_REGENERATED) == []
    assert world.user(user_id).password_version == 1
    world.assert_no_database_errors(writer)


def test_regenerating_recovery_codes_before_a_reset_stands_and_the_old_session_dies(
    world: World,
) -> None:
    """写入先提交：旧码全部作废、换上十个新码；之后原来的访问令牌 401。"""
    user_id = world.make_user(ADMIN_EMAIL, UserRole.ADMIN)
    account = world.enrol(user_id)
    session = world.sign_in(ADMIN_EMAIL, account)
    codes_before = world.live_code_ids(user_id)
    token = world.reset_token(ADMIN_EMAIL)

    writer = world.write_commits_first(
        "regenerate_recovery_codes", token, _regenerate(world, user_id)
    )

    assert len(writer.value()) == 10
    live = world.live_code_ids(user_id)
    assert len(live) == 10
    assert set(live).isdisjoint(codes_before), "旧码全部作废"
    assert len(world.audits(AuditAction.RECOVERY_CODES_REGENERATED)) == 1
    assert world.user(user_id).password_version == 1
    world.assert_no_database_errors(writer)

    assert_refused(
        world.client.get(LOGGED_IN, headers=bearer(session.access_token)), 401, "TOKEN_INVALID"
    )
    assert_refused(
        world.client.post(
            "/api/v1/auth/2fa/recovery-codes",
            json={"password": PASSWORD},
            headers=bearer(session.access_token),
        ),
        401,
        "TOKEN_INVALID",
    )
    assert world.live_code_ids(user_id) == live


def test_regenerating_after_the_api_authenticated_but_before_the_service_locks_is_refused(
    world: World,
) -> None:
    """`require_current_user` 已返回、密码已复核，进服务事务之前停住；重置提交后放行。

    能拦住它的只剩服务事务里那次锁内比较：401 TOKEN_INVALID，恢复码不替换。
    """
    user_id = world.make_user(ADMIN_EMAIL, UserRole.ADMIN)
    account = world.enrol(user_id)
    session = world.sign_in(ADMIN_EMAIL, account)
    codes_before = world.live_code_ids(user_id)
    token = world.reset_token(ADMIN_EMAIL)

    api_db = world.db("recovery-codes API")
    # `require_current_user` 读用户是不加锁的 SELECT，匹配不上；匹配上的第一条
    # `FOR UPDATE` 就是服务事务的第一条语句。走到这里说明鉴权与复核密码都已通过。
    gate = world.pause_before(
        api_db, "/2fa/recovery-codes: authenticated, service not started", locks_user
    )
    client = world.client_on(api_db)
    calling = world.actor(
        "/2fa/recovery-codes",
        lambda: client.post(
            "/api/v1/auth/2fa/recovery-codes",
            json={"password": PASSWORD},
            headers=bearer(session.access_token),
        ),
    )
    gate.wait_reached()
    world.actor("reset_password", lambda: world.reset(token)).value()
    gate.release()
    response = calling.value()

    assert_refused(response, 401, "TOKEN_INVALID")
    assert world.live_code_ids(user_id) == codes_before
    assert world.audits(AuditAction.RECOVERY_CODES_REGENERATED) == []
    assert world.user(user_id).password_version == 1
    world.assert_no_database_errors(calling)


# --- 第二因子消耗与改密码并发 -------------------------------------------------------


@pytest.mark.parametrize("factor", ["totp", "recovery_code"])
def test_a_second_step_paused_before_its_transaction_is_refused_after_a_reset(
    world: World, factor: str
) -> None:
    """① pending 令牌解码之后、进入事务之前停住；重置提交后放行。

    401 INVALID_CREDENTIALS；TOTP 计数器不变、恢复码不被消耗、不发会话。
    """
    user_id = world.make_user(ADMIN_EMAIL, UserRole.ADMIN)
    account = world.enrol(user_id)
    pending = world.pending(ADMIN_EMAIL)
    codes_before = world.live_code_ids(user_id)
    token = world.reset_token(ADMIN_EMAIL)
    code = pyotp.TOTP(account.secret).now() if factor == "totp" else account.codes[0]

    totp_db = world.db("/login/totp")
    # 事务里的第一条语句就是锁用户行：停在它之前 = 解码之后、进入事务之前。
    gate = world.pause_before(totp_db, "/login/totp: decoded, no transaction yet", locks_user)
    client = world.client_on(totp_db)
    stepping = world.actor("/login/totp", lambda: second_step(client, pending, code))
    gate.wait_reached()
    world.actor("reset_password", lambda: world.reset(token)).value()
    gate.release()
    response = stepping.value()

    assert_refused(response, 401, "INVALID_CREDENTIALS")
    setting = world.two_factor(user_id)
    assert setting is not None and setting.last_used_counter is None, "TOTP 计数器不能动"
    assert world.live_code_ids(user_id) == codes_before, "恢复码不被消耗"
    assert world.used_code_ids(user_id) == []
    assert world.refresh_rows(user_id) == [], "不发会话"
    assert world.audits(AuditAction.LOGIN) == []
    assert world.user(user_id).password_version == 1
    world.assert_no_database_errors(stepping)


@pytest.mark.parametrize("factor", ["totp", "recovery_code"])
def test_a_reset_waits_for_a_second_step_holding_the_user_lock(
    world: World, factor: str, reuse_warnings: list[logging.LogRecord]
) -> None:
    """② 重置在 `/login/totp` 持有用户行锁期间开始（重置等待）。

    只可能是 `/login/totp` 先提交（勘误；另一种先后由 ① 覆盖）：照常登录、
    消耗一次，随后重置完成，该会话 401。
    """
    user_id = world.make_user(ADMIN_EMAIL, UserRole.ADMIN)
    account = world.enrol(user_id)
    pending = world.pending(ADMIN_EMAIL)
    codes_before = world.live_code_ids(user_id)
    token = world.reset_token(ADMIN_EMAIL)
    code = pyotp.TOTP(account.secret).now() if factor == "totp" else account.codes[0]

    totp_db = world.db("/login/totp")
    gate = world.pause_after(totp_db, "/login/totp: holding the user lock", locks_user)
    client = world.client_on(totp_db)
    stepping = world.actor("/login/totp", lambda: second_step(client, pending, code))
    gate.wait_reached()
    resetting = world.actor("reset_password", lambda: world.reset(token))
    world.wait_until_waiting_for_users(world.reset_db, "reset_password")
    gate.release()
    response = stepping.value()
    resetting.value()

    assert response.status_code == 200, response.text
    access = response.json()["data"]["access_token"]
    refresh = response.cookies.get(REFRESH_COOKIE_NAME)
    assert access and refresh
    assert world.pwv(access, TOKEN_TYPE_ACCESS) == 0
    world.assert_no_database_errors(stepping, resetting)

    setting = world.two_factor(user_id)
    assert setting is not None
    if factor == "totp":
        assert setting.last_used_counter is not None, "TOTP 计数器前移"
        assert world.live_code_ids(user_id) == codes_before
    else:
        assert setting.last_used_counter is None
        assert len(world.used_code_ids(user_id)) == 1, "恰好消耗一个恢复码"
        assert len(world.live_code_ids(user_id)) == len(codes_before) - 1
    [row] = world.refresh_rows(user_id)
    assert row.password_version == 0
    assert row.revoked_at == world.reset_at, "重置随后提交，吊销了这条会话"
    assert len(world.audits(AuditAction.LOGIN)) == 1
    assert world.user(user_id).password_version == 1

    assert_refused(world.client.get(LOGGED_IN, headers=bearer(access)), 401, "TOKEN_INVALID")
    assert_refused(refresh_via(world.client, refresh), 401, "TOKEN_REUSED")
    assert world.audits(AuditAction.TOKEN_REUSED) == []
    assert reuse_warnings == []


# --- 第二因子失败与锁 ---------------------------------------------------------------


def test_a_wrong_second_factor_releases_the_user_lock_before_counting_the_failure(
    world: World,
) -> None:
    """加锁之后输错 TOTP：401 INVALID_TOTP，失败计数 +1，不出现锁等待超时。

    失败计数在独立事务里写同一行；持锁时去写会等本事务自己的锁，
    在这里就是 `innodb_lock_wait_timeout` 之后的 1205 与 500。
    """
    user_id = world.make_user(ADMIN_EMAIL, UserRole.ADMIN)
    account = world.enrol(user_id)
    pending = world.pending(ADMIN_EMAIL)
    codes_before = world.live_code_ids(user_id)
    assert world.user(user_id).failed_login_count == 0

    totp_db = world.db("/login/totp")
    client = world.client_on(totp_db)
    started = time.monotonic()
    stepping = world.actor(
        "/login/totp", lambda: second_step(client, pending, wrong_totp(account.secret))
    )
    response = stepping.value()
    elapsed = time.monotonic() - started

    assert_refused(response, 401, "INVALID_TOTP")
    assert totp_db.lockers, "第二步必须先锁用户行"
    assert elapsed < LOCK_WAIT_TIMEOUT_SECONDS
    world.assert_no_database_errors(stepping)
    assert world.user(user_id).failed_login_count == 1
    assert [row.reason for row in world.audits(AuditAction.LOGIN_FAILED)] == ["BAD_SECOND_FACTOR"]
    setting = world.two_factor(user_id)
    assert setting is not None and setting.last_used_counter is None
    assert world.live_code_ids(user_id) == codes_before
    assert world.refresh_rows(user_id) == []

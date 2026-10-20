"""Changing the password, racing login, refresh, the second factor and 2FA writes.

设计闸门 #369 v6 §7 的**并发**各行（AIH-TASK-088）；非并发各行在
`test_session_revocation.py`（AIH-TASK-087）。

设计 §7 的行与用例：

- 登录与改密码并发：
  `test_a_login_that_checked_the_old_password_before_a_reset_issues_dead_tokens`
- 第二因子消耗与改密码并发 ①：
  `test_a_second_factor_paused_before_its_transaction_is_refused_after_a_reset`
- 第二因子消耗与改密码并发 ②：
  `test_a_reset_waits_for_a_second_factor_that_holds_the_user_row`
- 第二因子失败与锁：
  `test_a_wrong_second_factor_releases_the_user_row_before_counting_the_failure`
- 刷新与改密码并发 ①：
  `test_a_reset_that_commits_while_a_refresh_waits_before_the_user_lock`
- 刷新与改密码并发 ②：
  `test_a_refresh_released_before_a_reset_issues_tokens_the_reset_ends`
- 刷新持锁时改密码（原死锁交错）：
  `test_a_reset_waits_for_a_refresh_that_holds_the_user_row`
- 死锁兜底：
  `test_a_deadlock_while_inserting_the_new_row_rolls_the_refresh_back`
- 刷新与登出并发：
  `test_a_logout_while_a_refresh_holds_the_user_row_is_not_a_replay`
- 真正的重放不受影响（并发那一半）：
  `test_two_refreshes_racing_for_one_row_still_count_as_a_replay`
- 认证材料写入与改密码并发：
  `test_a_reset_that_commits_first_stops_the_write`、
  `test_a_write_that_commits_first_stands_and_the_reset_ends_its_session`、
  `test_regenerating_codes_after_authentication_but_before_the_service_is_refused`

勘误（任务契约，优先于设计正文）：设计写的 401 `INVALID_TOKEN` 是现有错误码
`TOKEN_INVALID`；「第二因子消耗与改密码并发」② 只可能是 `/login/totp` 先提交 ——
它从事务开头就持有用户行锁，重置只能等它 —— 所以只跑这一种，另一种先后由 ① 覆盖。

**暂停点不碰产品代码**：引擎的 `before_cursor_execute` / `after_cursor_execute`、
`commit` / `rollback` 与会话工厂的 `before_commit` 上各挂**一个**常驻监听器，在任何
线程开始之前挂上，之后不再增删（SQLAlchemy 的监听器存在 deque 里，别的线程正在派发时
增删会抛 `deque mutated during iteration`，变成一个并非被测代码造成的 500）。暂停点与
注入的死锁只是登记在一张加锁的表里，监听器每次查表。

**每个暂停点绑定到它针对的那一方**：每个后台线程有一个归属标识，服务调用在线程里把它
绑成日志上下文的 `request_id`，HTTP 请求把它放进 `X-Request-ID` 头、由中间件绑上；
同步路由在线程池里执行，上下文随之带过去，所以监听器用 `current_request_id()` 就知道
这条语句是谁发的。暂停点只会停住它绑定的那一方，别人的同名语句不受影响。「提交之前」
的暂停点还要求那一方当前事务里已经锁了用户行（`SELECT … FROM users … FOR UPDATE`
执行成功之后、`commit` / `rollback` 之前），否则不算。

「对方确实在等行锁」不靠睡眠猜：先从另一个连接 `FOR UPDATE NOWAIT` 证明用户行此刻确实
被暂停的一方持有，再轮询 `information_schema.INNODB_TRX` 里**等待方自己那条 MySQL 连接**
（连接借出时记下的 `CONNECTION_ID()`）的事务，直到它是 `LOCK WAIT`、且它发出的最后一条
语句是锁用户行（MySQL 专有，这个文件只在 MySQL 上跑）。超时报出全部 `INNODB_TRX` 行、
等待方连接的 `PROCESSLIST` 状态（能看出是不是在等元数据锁）与它发出的最后几条语句。
每一处等待都有超时，超时即失败并写明停在哪一步；被等的一方若已经结束（返回或抛错），
立即失败并带上它的结果，而不是空等到超时。每个用例收尾时先放开所有暂停点、把后台线程
全部等完（有上限），再清掉本用例的行。

**用例之间互不干扰**：xdist 的每个 worker 用自己的库（`tests/backend/conftest.py` 的
`pytest_configure` 把 `BILLING_TEST_DATABASE_URL` 换成 `<库名>_<worker id>`），同一个
worker 里的用例顺序执行。此外每个用例用自己独有的邮箱，清理只删本用例那个用户的行，
审计的断言也只看本用例用户名下的行 —— 审计表只追加、DELETE 会被触发器拒，留下的行不碍事，
所以这里不 TRUNCATE。

刷新各行与角色无关，会话一律取自 `/login/totp`（两步登录）；只有「登录与改密码并发」
一行要 `/login` 本身签发会话，所以用未启用 2FA 的 CUSTOMER。签发的刷新令牌一律从那次
响应自己的 `Set-Cookie` 里取，不读共用的 cookie jar。

⚠️ 行锁、`FOR UPDATE`、锁等待在 SQLite 上都不存在，所以这里只对着真 MySQL 跑：需要
`BILLING_TEST_DATABASE_URL`，CI 的 MySQL 分片设了它；本地没设时整个文件 skip ——
**skip 不是通过**。
"""

from __future__ import annotations

import base64
import itertools
import json
import logging
import os
import re
import threading
import time
import traceback
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from http.cookies import SimpleCookie

import pyotp
import pytest
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, delete, event, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

from alembic import command
from app.core.config import Settings
from app.core.database import create_session_factory
from app.core.errors import AppError
from app.core.logging import bind_log_context, clear_log_context, current_request_id
from app.core.middleware import REQUEST_ID_HEADER
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
    PasswordResetToken,
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
    logout,
    refresh_session,
    utc_now,
)
from app.services.password_reset import request_reset, reset_password
from app.services.two_factor import regenerate_recovery_codes

SKIP_REASON = "BILLING_TEST_DATABASE_URL is not set; row locks and lock waits need a real MySQL"

pytestmark = pytest.mark.skipif(not os.environ.get("BILLING_TEST_DATABASE_URL"), reason=SKIP_REASON)

PASSWORD = "a-perfectly-fine-passphrase"
NEW_PASSWORD = "another-perfectly-fine-passphrase"
CONTEXT = RequestContext(ip_address="10.0.0.1", user_agent="pytest")

LOGIN = "/api/v1/auth/login"
LOGIN_TOTP = "/api/v1/auth/login/totp"
REFRESH = "/api/v1/auth/refresh"
ENROL = "/api/v1/auth/2fa/enrol"
CONFIRM = "/api/v1/auth/2fa/confirm"
RECOVERY_CODES = "/api/v1/auth/2fa/recovery-codes"
# 任何一个已登录接口都经过 `require_current_user`；挑一个只读的。ADMIN 的有效令牌在
# 这里拿 200，失效的令牌在鉴权那一步就是 401。
LOGGED_IN = "/api/v1/admin/audit-logs"

REUSE_WARNING = "Refresh token reuse detected"
UNHANDLED = "Unhandled exception"

# 走到暂停点、被暂停的一方等放行、后台线程结束：各自的上限。结束的上限更宽，
# 是因为确认 2FA、重新生成恢复码要算十次 Argon2。
REACH_SECONDS = 10
RELEASE_SECONDS = 30
FINISH_SECONDS = 30
# 轮询 INNODB_TRX：间隔 50 毫秒、最多 10 秒（任务契约）。
LOCK_WAIT_POLL_SECONDS = 10
LOCK_WAIT_POLL_INTERVAL = 0.05

# MySQL 的 ER_LOCK_DEADLOCK。
ER_LOCK_DEADLOCK = 1213

_FLAGS = re.IGNORECASE | re.DOTALL
LOCK_USER = re.compile(r"^\s*SELECT\b.*\bFROM\s+`?users`?\b.*\bFOR\s+UPDATE\b", _FLAGS)
READ_USER = re.compile(r"^\s*SELECT\b(?!.*\bFOR\s+UPDATE\b).*\bFROM\s+`?users`?\b", _FLAGS)
READ_TWO_FACTOR = re.compile(r"^\s*SELECT\b.*\bFROM\s+`?two_factor_settings`?\b", _FLAGS)
MARK_REFRESH_USED = re.compile(r"^\s*UPDATE\s+`?refresh_tokens`?\s+SET\s+used_at\s*=", _FLAGS)
INSERT_REFRESH = re.compile(r"^\s*INSERT\s+INTO\s+`?refresh_tokens`?", _FLAGS)
COUNT_FAILURE = re.compile(r"^\s*UPDATE\s+`?users`?\s+SET\s+failed_login_count\s*=", _FLAGS)

# 连接借出时记下它在 MySQL 里的线程号（`connection_record.info` 的键）。
MYSQL_ID = "session_revocation_mysql_id"
# 等待方自己那条连接的事务；超时时报出的全部事务与该连接的状态（MySQL 专有）。
TRX_OF = text(
    "SELECT trx_state, trx_query FROM information_schema.INNODB_TRX"
    " WHERE trx_mysql_thread_id = :thread_id"
)
ALL_TRX = text(
    "SELECT trx_mysql_thread_id, trx_state, trx_query FROM information_schema.INNODB_TRX"
)
PROCESS_OF = text("SELECT STATE, INFO FROM information_schema.PROCESSLIST WHERE ID = :thread_id")
# 诊断信息里每条语句、每个用户的语句各保留多少。
STATEMENT_CHARS = 200
TRAIL_TAIL = 8

_EMAILS = itertools.count(1)


def unique_email() -> str:
    worker = os.environ.get("PYTEST_XDIST_WORKER", "main")
    return f"session-revocation-{worker}-{os.getpid()}-{next(_EMAILS)}@example.com"


def short(statement: str | None) -> str:
    return " ".join((statement or "").split())[:STATEMENT_CHARS]


# --- barriers ----------------------------------------------------------------------------


class Pause:
    """One stop on the way to (or back from) MySQL for one side; it fires once.

    `phase` 是 `before` / `after`（语句执行前 / 后，匹配 `pattern`）或 `commit`
    （会话提交之前，要求那一方当前事务已经锁了用户行）。
    """

    def __init__(self, name: str, *, phase: str, pattern: re.Pattern[str] | None) -> None:
        self.name = name
        self.phase = phase
        self.pattern = pattern
        # 以下由 Tracer 在它的锁里读写。
        self.owner: str | None = None
        self.armed = False
        # 触发之前这一方发出的语句：用来证明暂停之前确实已经走过了哪一步。
        self.before: list[str] = []
        self._reached = threading.Event()
        self._released = threading.Event()

    def hold(self) -> None:
        """Called on the paused thread: stay here until the test lets go."""
        self._reached.set()
        if not self._released.wait(RELEASE_SECONDS):
            raise TimeoutError(f"{self.name}：停在暂停点上 {RELEASE_SECONDS} 秒没有被放行")

    def wait_reached(self, side: Background) -> None:
        """Wait for `side` to stop here; fail at once if it ends without getting here."""
        assert self.owner == side.owner, f"{self.name} 绑定的不是 {side.name}"
        deadline = time.monotonic() + REACH_SECONDS
        while not self._reached.wait(LOCK_WAIT_POLL_INTERVAL):
            if side.finished:
                pytest.fail(f"{self.name}：没有走到暂停点就结束了 —— {side.describe()}")
            if time.monotonic() >= deadline:
                pytest.fail(f"{self.name}：{REACH_SECONDS} 秒内没有走到暂停点 —— {side.describe()}")

    def release(self) -> None:
        self._released.set()


class Deadlock:
    """One side's first statement matching `pattern` fails as a MySQL deadlock would."""

    def __init__(self, pattern: re.Pattern[str]) -> None:
        self.pattern = pattern
        self.owner: str | None = None
        self.armed = False
        self.fired: list[str] = []


@dataclass
class Trail:
    """What one side has sent to MySQL so far, and on which connection."""

    statements: list[str] = field(default_factory=list)
    connection_id: int | None = None
    # 当前事务里锁用户行的语句已经执行成功、事务尚未结束。
    holds_user: bool = False


class Tracer:
    """The one set of listeners for a test: registered before any thread, never removed.

    引擎与会话工厂都是本用例自己的，用例结束时随之丢弃，所以监听器不需要摘 ——
    也就不会在别的线程派发事件时改动监听器列表。
    """

    def __init__(self, engine: Engine) -> None:
        self._guard = threading.Lock()
        self._trails: dict[str, Trail] = {}
        self._pauses: list[Pause] = []
        self._deadlocks: list[Deadlock] = []
        self._dbapi_error = engine.dialect.loaded_dbapi.OperationalError
        event.listen(engine, "checkout", self._on_checkout)
        event.listen(engine, "before_cursor_execute", self._before_execute)
        event.listen(engine, "after_cursor_execute", self._after_execute)
        event.listen(engine, "commit", self._on_end)
        event.listen(engine, "rollback", self._on_end)
        event.listen(engine, "checkin", self._on_checkin)

    def watch_sessions(self, factory: sessionmaker[Session]) -> None:
        event.listen(factory, "before_commit", self._before_commit)

    # -- arming --

    def arm(self, trap: Pause | Deadlock, owner: str) -> None:
        with self._guard:
            assert trap.owner is None, "一个暂停点只绑定一方"
            trap.owner = owner
            trap.armed = True
            if isinstance(trap, Pause):
                self._pauses.append(trap)
            else:
                self._deadlocks.append(trap)

    def release_all(self) -> None:
        with self._guard:
            pauses = list(self._pauses)
            for trap in [*self._pauses, *self._deadlocks]:
                trap.armed = False
        for pause in pauses:
            pause.release()

    # -- what each side did --

    def connection_of(self, owner: str) -> int | None:
        with self._guard:
            trail = self._trails.get(owner)
            return trail.connection_id if trail else None

    def last_statement(self, owner: str) -> str | None:
        with self._guard:
            trail = self._trails.get(owner)
            return trail.statements[-1] if trail and trail.statements else None

    def describe(self, owner: str) -> str:
        with self._guard:
            trail = self._trails.get(owner)
            if trail is None:
                return "还没有向 MySQL 发过语句"
            tail = [short(statement) for statement in trail.statements[-TRAIL_TAIL:]]
            return (
                f"MySQL 连接 {trail.connection_id}，持有用户行锁：{trail.holds_user}，"
                f"最后发出的语句：{tail}"
            )

    # -- listeners --

    def _on_checkout(self, dbapi_connection, record, proxy) -> None:  # noqa: ANN001
        if MYSQL_ID in record.info:
            return
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("SELECT CONNECTION_ID()")
            record.info[MYSQL_ID] = int(cursor.fetchone()[0])
        finally:
            cursor.close()

    def _trail(self, owner: str) -> Trail:
        return self._trails.setdefault(owner, Trail())

    def _take(self, owner: str, phase: str, statement: str | None) -> Pause | None:
        """Disarm and return the pause of `owner` this point hits, if any (under the guard)."""
        trail = self._trail(owner)
        for pause in self._pauses:
            if not pause.armed or pause.owner != owner or pause.phase != phase:
                continue
            if pause.pattern is not None and not pause.pattern.search(statement or ""):
                continue
            if phase == "commit" and not trail.holds_user:
                continue
            pause.armed = False
            sent = trail.statements if phase == "commit" else trail.statements[:-1]
            pause.before = list(sent)
            return pause
        return None

    def _before_execute(self, conn, cursor, statement, parameters, context, executemany) -> None:  # noqa: ANN001
        owner = current_request_id()
        if owner is None:
            return
        failing: Deadlock | None = None
        with self._guard:
            trail = self._trail(owner)
            trail.statements.append(statement)
            trail.connection_id = conn.info.get(MYSQL_ID)
            for deadlock in self._deadlocks:
                if (
                    deadlock.armed
                    and deadlock.owner == owner
                    and deadlock.pattern.search(statement)
                ):
                    deadlock.armed = False
                    deadlock.fired.append(statement)
                    failing = deadlock
                    break
            pause = None if failing else self._take(owner, "before", statement)
        if failing is not None:
            raise OperationalError(
                statement,
                parameters,
                self._dbapi_error(
                    ER_LOCK_DEADLOCK,
                    "Deadlock found when trying to get lock; try restarting transaction",
                ),
            )
        if pause is not None:
            pause.hold()

    def _after_execute(self, conn, cursor, statement, parameters, context, executemany) -> None:  # noqa: ANN001
        owner = current_request_id()
        if owner is None:
            return
        with self._guard:
            if LOCK_USER.search(statement):
                self._trail(owner).holds_user = True
            pause = self._take(owner, "after", statement)
        if pause is not None:
            pause.hold()

    def _on_end(self, conn) -> None:  # noqa: ANN001 - Connection
        owner = current_request_id()
        if owner is None:
            return
        with self._guard:
            self._trail(owner).holds_user = False

    def _on_checkin(self, dbapi_connection, record) -> None:  # noqa: ANN001
        # 连接还回池里，事务无论如何都结束了。
        self._on_end(None)

    def _before_commit(self, session) -> None:  # noqa: ANN001 - Session
        owner = current_request_id()
        if owner is None:
            return
        with self._guard:
            pause = self._take(owner, "commit", None)
        if pause is not None:
            pause.hold()


# 后台线程的归属标识：`World.post` 据此给请求带上 `X-Request-ID`。
_current = threading.local()


class Background:
    """Run one side of the race on its own thread (and its own connection)."""

    def __init__(
        self,
        name: str,
        owner: str,
        target: Callable[[], object],
        peers: list[Background],
        tracer: Tracer,
    ) -> None:
        self.name = name
        self.owner = owner
        self._tracer = tracer
        self._outcome: object = None
        self._trace = ""
        self._done = threading.Event()
        # 同一个用例里的其他后台线程：超时时一并报出它们在做什么。
        self._peers = peers
        # daemon：编排出错时卡住的线程不拖住整个 pytest 进程（收尾仍会等它、等不到即失败）。
        self._thread = threading.Thread(target=self._run, args=(target,), name=name, daemon=True)
        self._thread.start()

    def _run(self, target: Callable[[], object]) -> None:
        # 服务调用直接在这个线程里发语句：把归属绑成日志上下文的 request_id，监听器
        # 用 `current_request_id()` 认人。HTTP 请求由 `World.post` 带上同一个值。
        _current.owner = self.owner
        clear_log_context()
        bind_log_context(request_id=self.owner)
        try:
            self._outcome = target()
        except BaseException as error:  # noqa: BLE001 - 结果由用例断言
            self._outcome = error
            self._trace = "".join(traceback.format_exception(error))
        finally:
            self._done.set()

    @property
    def finished(self) -> bool:
        return self._done.is_set()

    def join(self, seconds: float) -> bool:
        return self._done.wait(seconds)

    def describe(self) -> str:
        sent = self._tracer.describe(self.owner)
        if not self.finished:
            return f"{self.name}：仍在运行（{sent}）"
        if isinstance(self._outcome, BaseException):
            return f"{self.name}：抛出 {self._outcome!r}（{sent}）\n{self._trace}"
        text_body = getattr(self._outcome, "text", None)
        if text_body is not None:
            return f"{self.name}：返回 {self._outcome!r} {text_body}（{sent}）"
        return f"{self.name}：返回 {self._outcome!r}（{sent}）"

    def outcome(self) -> object:
        """Its return value, or the exception it raised."""
        if not self._done.wait(FINISH_SECONDS):
            others = "；".join(peer.describe() for peer in self._peers if peer is not self)
            pytest.fail(
                f"{self.name}：{FINISH_SECONDS} 秒内没有结束 —— {self.describe()}"
                f"（其他线程：{others or '无'}）"
            )
        return self._outcome


# --- fixtures ----------------------------------------------------------------------------


@pytest.fixture
def database_url() -> str:
    # 在 fixture 里读：conftest 的 `pytest_configure` 给每个 xdist worker 换了库名。
    url = os.environ.get("BILLING_TEST_DATABASE_URL", "")
    if not url:
        pytest.skip(SKIP_REASON)
    return url


@pytest.fixture
def engine(database_url: str, monkeypatch) -> Iterator[Engine]:
    # ⚠️ 建表走 alembic，不用 `Base.metadata.create_all`：后者建的表不在 `alembic_version`
    # 的记账里，会让 test_migrations.py 的降级再升级撞上「表已存在」。
    from app.core.config import get_settings

    monkeypatch.setenv("BILLING_DATABASE_URL", database_url)
    get_settings.cache_clear()
    command.upgrade(Config("alembic.ini"), "head")
    get_settings.cache_clear()

    created = create_engine(database_url, pool_size=8, max_overflow=8)
    try:
        yield created
    finally:
        created.dispose()


@pytest.fixture
def tracer(engine: Engine) -> Tracer:
    return Tracer(engine)


@pytest.fixture
def probe(database_url: str) -> Iterator[Engine]:
    """A separate engine for looking at locks: no listeners on it."""
    created = create_engine(database_url, poolclass=NullPool)
    try:
        yield created
    finally:
        created.dispose()


@pytest.fixture
def client(engine: Engine, tracer: Tracer, tmp_path) -> TestClient:
    jwt_key = tmp_path / "jwt.key"
    jwt_key.write_text("a-test-signing-key-that-is-long-enough", encoding="utf-8")
    master = tmp_path / "master.key"
    master.write_text(f"1:{base64.b64encode(os.urandom(32)).decode()}\n", encoding="utf-8")

    app = create_app(
        Settings(
            # 显式置空：上面的 fixture 设了 BILLING_DATABASE_URL，不置空的话应用会自己
            # 再建一个引擎；这里要它用下面这个挂了监听器的引擎。
            database_url="",
            jwt_secret_file=str(jwt_key),
            master_key_file=str(master),
            session_cookie_secure=False,
            auth_rate_limit_per_minute=600,
            auth_rate_limit_burst=200,
        )
    )
    app.state.engine = engine
    app.state.session_factory = create_session_factory(engine)
    tracer.watch_sessions(app.state.session_factory)
    # 死锁兜底那条要看 500，而不是让异常冒到用例里。
    return TestClient(app, raise_server_exceptions=False)


class _Collect(logging.Handler):
    def __init__(self, message: str, into: list[logging.LogRecord]) -> None:
        super().__init__(level=logging.WARNING)
        self.message = message
        self.into = into

    def emit(self, record: logging.LogRecord) -> None:
        if record.getMessage() == self.message:
            self.into.append(record)


@dataclass
class Logs:
    # T0.9 的盗用告警挂在这条 warning 上。
    reuse_warnings: list[logging.LogRecord]
    # 全局处理器记下的未预期异常（500）：死锁会以这种形式冒出来。
    unhandled: list[logging.LogRecord]


@pytest.fixture
def logs(client: TestClient) -> Iterator[Logs]:
    # 依赖 client：`create_app` 会重装根日志处理器，要在它之后挂。
    collected = Logs(reuse_warnings=[], unhandled=[])
    handlers = {
        "app.services.auth": _Collect(REUSE_WARNING, collected.reuse_warnings),
        "app.core.errors": _Collect(UNHANDLED, collected.unhandled),
    }
    for name, handler in handlers.items():
        logging.getLogger(name).addHandler(handler)
    try:
        yield collected
    finally:
        for name, handler in handlers.items():
            logging.getLogger(name).removeHandler(handler)


def _world(
    engine: Engine, probe: Engine, client: TestClient, logs: Logs, tracer: Tracer, role: UserRole
) -> Iterator[World]:
    world = World(engine, probe, client, logs, tracer, role=role)
    try:
        yield world
    finally:
        # 线程都结束了才清：它手上的行锁与写入不能落到清理之后。
        try:
            world.finish_backgrounds()
        finally:
            world.clean()


@pytest.fixture
def admin(
    engine: Engine, probe: Engine, client: TestClient, logs: Logs, tracer: Tracer
) -> Iterator[World]:
    yield from _world(engine, probe, client, logs, tracer, UserRole.ADMIN)


@pytest.fixture
def customer(
    engine: Engine, probe: Engine, client: TestClient, logs: Logs, tracer: Tracer
) -> Iterator[World]:
    """A CUSTOMER without 2FA: login hands out a session in one step."""
    yield from _world(engine, probe, client, logs, tracer, UserRole.CUSTOMER)


# --- the world ---------------------------------------------------------------------------


@dataclass
class Account:
    secret: str
    codes: list[str]


@dataclass(frozen=True)
class SignedIn:
    access_token: str
    refresh_token: str


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def issued_refresh_cookie(response) -> str:  # noqa: ANN001 - httpx.Response
    """The refresh token this very response set, from its own `Set-Cookie` headers."""
    headers = response.headers.get_list("set-cookie")
    jar: SimpleCookie = SimpleCookie()
    for header in headers:
        jar.load(header)
    morsel = jar.get(REFRESH_COOKIE_NAME)
    assert morsel is not None and morsel.value, f"响应没有发刷新 cookie：{headers}"
    return morsel.value


def assert_error(response, http_status: int, code: str) -> None:  # noqa: ANN001 - httpx.Response
    assert not isinstance(response, BaseException), (
        f"期望 {http_status} {code}，实际抛出 {response!r}"
    )
    assert response.status_code == http_status, response.text
    assert response.json()["error"]["code"] == code, response.text


def assert_app_error(outcome: object, http_status: int, code: str) -> None:
    """A service-level refusal, as the error handler would render it."""
    assert isinstance(outcome, AppError), f"期望 {http_status} {code}，实际 {outcome!r}"
    assert (outcome.http_status, outcome.code) == (http_status, code)


class World:
    def __init__(
        self,
        engine: Engine,
        probe: Engine,
        client: TestClient,
        logs: Logs,
        tracer: Tracer,
        *,
        role: UserRole,
    ) -> None:
        self.engine = engine
        self.probe = probe
        self.client = client
        self.logs = logs
        self.tracer = tracer
        self.settings: Settings = client.app.state.settings
        self.factory = client.app.state.session_factory
        self.email = unique_email()
        # 本用例开过的后台线程：收尾时全部等完。
        self._backgrounds: list[Background] = []
        self.clean()
        now = utc_now()
        with self.factory() as session:
            user = User(
                email=self.email,
                password_hash=hash_password(PASSWORD),
                role=role,
                status=UserStatus.ACTIVE,
                created_at=now,
                updated_at=now,
            )
            session.add(user)
            session.commit()
            self.user_id = int(user.id)

    def clean(self) -> None:
        """Delete this case's user and everything hanging off it; nobody else's rows."""
        with self.engine.begin() as connection:
            ours = select(User.id).where(User.email == self.email)
            ids = [str(user_id) for user_id in connection.execute(ours).scalars()]
            for model in (RecoveryCode, TwoFactorSetting, RefreshToken, PasswordResetToken):
                connection.execute(delete(model).where(model.user_id.in_(ours)))
            connection.execute(
                delete(DomainOutbox).where(
                    DomainOutbox.aggregate_type == "users", DomainOutbox.aggregate_id.in_(ids)
                )
            )
            connection.execute(delete(User).where(User.email == self.email))

    # -- barriers --

    def pause(self, pattern: re.Pattern[str], name: str, *, after: bool = False) -> Pause:
        """A stop at the first statement matching `pattern`: before it runs, or after it.

        交给 `background(..., trap=...)` 才生效，而且只停那一方。
        """
        return Pause(name, phase="after" if after else "before", pattern=pattern)

    def pause_before_commit(self, name: str) -> Pause:
        """A stop just before committing a transaction that locked the users row."""
        return Pause(name, phase="commit", pattern=None)

    def background(
        self, name: str, target: Callable[[], object], *, trap: Pause | Deadlock | None = None
    ) -> Background:
        owner = f"bg-{len(self._backgrounds) + 1}"
        if trap is not None:
            # 先登记再起线程：线程的第一条语句就可能是暂停点。
            self.tracer.arm(trap, owner)
        started = Background(name, owner, target, self._backgrounds, self.tracer)
        self._backgrounds.append(started)
        return started

    def assert_user_row_held(self, step: str, holder: Background) -> None:
        """Somebody holds the users row: `FOR UPDATE NOWAIT` from another connection fails."""
        with self.probe.connect() as connection:
            try:
                connection.execute(
                    select(User.id).where(User.id == self.user_id).with_for_update(nowait=True)
                )
            except OperationalError:
                held = True
            else:
                held = False
            finally:
                connection.rollback()
        if not held:
            pytest.fail(f"{step}：用户行没有被持有 —— 暂停的一方：{holder.describe()}")

    def assert_user_row_unlocked(self, step: str) -> None:
        """Nobody holds the users row: `FOR UPDATE NOWAIT` from another connection gets it."""
        with self.probe.connect() as connection:
            try:
                connection.execute(
                    select(User.id).where(User.id == self.user_id).with_for_update(nowait=True)
                )
            except OperationalError as error:
                pytest.fail(f"{step}：用户行仍被持有：{error.orig!r}")
            finally:
                connection.rollback()

    def wait_for_lock_wait(self, step: str, waiter: Background) -> None:
        """Poll INNODB_TRX until `waiter`'s own connection waits for the users row lock.

        `waiter` 已经结束就说明它没有在等锁，立即失败。
        """
        deadline = time.monotonic() + LOCK_WAIT_POLL_SECONDS
        with self.probe.connect() as connection:
            while True:
                thread_id = self.tracer.connection_of(waiter.owner)
                last = self.tracer.last_statement(waiter.owner)
                state = None
                if thread_id is not None:
                    state = connection.execute(TRX_OF, {"thread_id": thread_id}).first()
                    connection.rollback()
                if (
                    state is not None
                    and state.trx_state == "LOCK WAIT"
                    and last is not None
                    and LOCK_USER.search(last)
                ):
                    return
                if waiter.finished:
                    pytest.fail(f"{step}：等锁的一方已经结束，没有在等锁 —— {waiter.describe()}")
                if time.monotonic() >= deadline:
                    pytest.fail(
                        f"{step}：{LOCK_WAIT_POLL_SECONDS} 秒内没有看到 MySQL 连接 {thread_id}"
                        f" 在等 users 行锁（它的事务：{state and tuple(state)}）\n"
                        f"{self._lock_report(connection, thread_id)}\n{waiter.describe()}"
                    )
                time.sleep(LOCK_WAIT_POLL_INTERVAL)

    def _lock_report(self, connection, thread_id: int | None) -> str:  # noqa: ANN001
        transactions = [
            (row.trx_mysql_thread_id, row.trx_state, short(row.trx_query))
            for row in connection.execute(ALL_TRX)
        ]
        process = None
        if thread_id is not None:
            found = connection.execute(PROCESS_OF, {"thread_id": thread_id}).first()
            process = (found.STATE, short(found.INFO)) if found else None
        connection.rollback()
        return (
            f"INNODB_TRX 全部行：{transactions}\n等待方连接的 PROCESSLIST（STATE, INFO）：{process}"
        )

    def finish_backgrounds(self) -> None:
        """Teardown: let every pause go, then wait (bounded) for every thread this case started.

        ⚠️ 用例中途失败时后台线程可能还停在暂停点上、或者还在跑；不等它结束就清库，
        它手上的行锁与写入会落到清理之后。
        """
        self.tracer.release_all()
        deadline = time.monotonic() + FINISH_SECONDS
        stuck = [
            started
            for started in self._backgrounds
            if not started.join(max(deadline - time.monotonic(), 0))
        ]
        if stuck:
            pytest.fail(
                f"收尾：{FINISH_SECONDS} 秒内这些后台线程没有结束："
                + "；".join(started.describe() for started in stuck)
            )

    # -- HTTP --

    def _tagged(self, headers: dict[str, str] | None) -> dict[str, str]:
        """In a background thread, carry its owner as the request id (see `Tracer`)."""
        tagged = dict(headers or {})
        owner = getattr(_current, "owner", None)
        if owner is not None:
            tagged[REQUEST_ID_HEADER] = owner
        return tagged

    def post(self, path: str, *, json: dict | None = None, headers: dict[str, str] | None = None):  # noqa: ANN201
        return self.client.post(path, json=json, headers=self._tagged(headers))

    def get(self, path: str, *, headers: dict[str, str] | None = None):  # noqa: ANN201
        return self.client.get(path, headers=self._tagged(headers))

    def login(self, password: str = PASSWORD) -> dict:
        response = self.post(LOGIN, json={"email": self.email, "password": password})
        assert response.status_code == 200, response.text
        return response.json()["data"]

    def enrol(self) -> Account:
        first = self.login()
        assert first["stage"] == STAGE_ENROL_2FA
        enrolment = self.post(ENROL, json={"pending_token": first["pending_token"]})
        assert enrolment.status_code == 200, enrolment.text
        secret = enrolment.json()["data"]["secret"]
        confirmed = self.post(
            CONFIRM,
            json={"pending_token": first["pending_token"], "code": pyotp.TOTP(secret).now()},
        )
        assert confirmed.status_code == 200, confirmed.text
        return Account(secret=secret, codes=list(confirmed.json()["data"]["recovery_codes"]))

    def pending_second_factor(self) -> str:
        first = self.login()
        assert first["stage"] == STAGE_TOTP_REQUIRED
        return first["pending_token"]

    def sign_in_admin(self, account: Account) -> SignedIn:
        """Both steps, with a recovery code (a TOTP code works only once per window)."""
        second = self.post(
            LOGIN_TOTP,
            json={"pending_token": self.pending_second_factor(), "code": account.codes.pop()},
        )
        assert second.status_code == 200, second.text
        return SignedIn(
            access_token=second.json()["data"]["access_token"],
            refresh_token=issued_refresh_cookie(second),
        )

    def sign_in(self) -> SignedIn:
        """A fresh ADMIN session for the refresh rows: enrol, then both steps."""
        return self.sign_in_admin(self.enrol())

    def pwv(self, token: str, expected_type: str) -> int:
        return decode_token(self.settings, token, expected_type=expected_type)["pwv"]

    # -- services --

    def reset_link(self) -> str:
        """Forgot → the token out of the outbox row, the way the email carries it."""
        request_reset(self.factory, self.settings, email=self.email, context=CONTEXT)
        with self.factory() as session:
            row = session.execute(
                select(DomainOutbox)
                .where(DomainOutbox.aggregate_id == str(self.user_id))
                .order_by(DomainOutbox.id.desc())
                .limit(1)
            ).scalar_one()
            return str(json.loads(row.payload_json)["token"])

    def reset(self, link: str) -> None:
        reset_password(
            self.factory, self.settings, token=link, new_password=NEW_PASSWORD, context=CONTEXT
        )

    def refresh(self, raw: str) -> IssuedSession:
        return refresh_session(self.factory, self.settings, raw_token=raw, context=CONTEXT)

    # -- state --

    def user(self) -> User:
        with self.factory() as session:
            user = session.get(User, self.user_id)
            assert user is not None
            session.expunge(user)
            return user

    def two_factor(self) -> TwoFactorSetting | None:
        with self.factory() as session:
            row = session.get(TwoFactorSetting, self.user_id)
            if row is not None:
                session.expunge(row)
            return row

    def refresh_rows(self) -> list[RefreshToken]:
        with self.factory() as session:
            rows = list(
                session.execute(
                    select(RefreshToken)
                    .where(RefreshToken.user_id == self.user_id)
                    .order_by(RefreshToken.id)
                ).scalars()
            )
            session.expunge_all()
            return rows

    def refresh_row(self, raw: str) -> RefreshToken:
        rows = self.refresh_rows()
        matches = [row for row in rows if row.token_hash == hash_refresh_token(raw)]
        assert len(matches) == 1, (
            f"这个刷新令牌应当恰好对应一行；用户 {self.user_id} 名下全部刷新令牌行"
            "（id, family_id, password_version, used_at, revoked_at）："
            + repr(
                [
                    (row.id, row.family_id, row.password_version, row.used_at, row.revoked_at)
                    for row in rows
                ]
            )
        )
        return matches[0]

    def live_code_ids(self) -> list[int]:
        with self.factory() as session:
            return list(
                session.execute(
                    select(RecoveryCode.id)
                    .where(
                        RecoveryCode.user_id == self.user_id,
                        RecoveryCode.used_at.is_(None),
                        RecoveryCode.revoked_at.is_(None),
                    )
                    .order_by(RecoveryCode.id)
                ).scalars()
            )

    def audits(self, action: AuditAction) -> list[AuditLog]:
        """This case's user's audit rows of one kind (the table is append-only and shared)."""
        with self.factory() as session:
            rows = list(
                session.execute(
                    select(AuditLog)
                    .where(AuditLog.action == action, AuditLog.actor_user_id == self.user_id)
                    .order_by(AuditLog.id)
                ).scalars()
            )
            session.expunge_all()
            return rows

    # -- shared final-state assertions --

    def assert_no_database_error(self, *outcomes: object) -> None:
        """No side died of a database error: no OperationalError, no 500, nothing unhandled."""
        for outcome in outcomes:
            assert not isinstance(outcome, OperationalError), f"数据库错误：{outcome!r}"
            status = getattr(outcome, "status_code", None)
            assert status != 500, getattr(outcome, "text", "")
        assert self.logs.unhandled == [], [record.exc_info for record in self.logs.unhandled]

    def assert_no_replay_alarm(self) -> None:
        assert self.audits(AuditAction.TOKEN_REUSED) == []
        assert self.logs.reuse_warnings == []

    def assert_session_is_dead(self, session: SignedIn) -> None:
        """Both halves of a session issued under the old password: 401 everywhere."""
        response = self.get(LOGGED_IN, headers=bearer(session.access_token))
        assert_error(response, 401, "TOKEN_INVALID")
        try:
            refreshed: object = self.refresh(session.refresh_token)
        except AppError as error:
            refreshed = error
        assert_app_error(refreshed, 401, "TOKEN_REUSED")


def second_factor_code(account: Account, factor: str) -> str:
    if factor == "totp":
        return pyotp.TOTP(account.secret).now()
    return account.codes.pop()


# --- 登录与改密码并发 ------------------------------------------------------------------


def test_a_login_that_checked_the_old_password_before_a_reset_issues_dead_tokens(
    customer: World,
) -> None:
    """登录在 `verify_password` 之后、签发之前停住，重置提交，再放行登录。

    登录照常返回令牌，但令牌带的是验证密码时读到的旧版本号：在任何使用点都 401，
    刷新令牌行上的版本是旧值。暂停点是 `authenticate` 在验证密码之后读 2FA 设置的那一条
    语句，它之后就是签发。
    """
    w = customer
    link = w.reset_link()
    logins_before = len(w.audits(AuditAction.LOGIN))

    pause = w.pause(READ_TWO_FACTOR, "登录：verify_password 之后、签发之前")
    login = w.background(
        "登录",
        lambda: w.post(LOGIN, json={"email": w.email, "password": PASSWORD}),
        trap=pause,
    )
    pause.wait_reached(login)
    reset = w.background("重置", lambda: w.reset(link)).outcome()
    assert reset is None, f"重置应当成功：{reset!r}"
    pause.release()
    response = login.outcome()

    w.assert_no_database_error(reset, response)
    assert response.status_code == 200, response.text
    issued = SignedIn(
        access_token=response.json()["data"]["access_token"],
        refresh_token=issued_refresh_cookie(response),
    )
    assert w.pwv(issued.access_token, TOKEN_TYPE_ACCESS) == 0
    # 登录返回了令牌，它的刷新令牌行与登录审计就必须已经落库。
    row = w.refresh_row(issued.refresh_token)
    assert (row.password_version, row.revoked_at) == (0, None)
    assert len(w.audits(AuditAction.LOGIN)) == logins_before + 1
    user = w.user()
    assert user.password_version == 1
    # 登录后半段写用户行（`last_login_at`）不能把新密码盖回去。
    assert verify_password(user.password_hash, NEW_PASSWORD)

    # 任何使用点都 401。
    w.assert_session_is_dead(issued)
    response = w.post(
        RECOVERY_CODES, json={"password": NEW_PASSWORD}, headers=bearer(issued.access_token)
    )
    assert_error(response, 401, "TOKEN_INVALID")
    row = w.refresh_row(issued.refresh_token)
    assert (row.used_at, row.revoked_at) == (None, None)
    w.assert_no_replay_alarm()


# --- 第二因子消耗与改密码并发 --------------------------------------------------------------


@pytest.mark.parametrize("factor", ["totp", "recovery_code"])
def test_a_second_factor_paused_before_its_transaction_is_refused_after_a_reset(
    admin: World, factor: str
) -> None:
    """① pending 令牌解码之后、进入事务之前停住，重置提交后放行。

    401 `INVALID_CREDENTIALS`；TOTP 计数器不变、恢复码不被消耗、不发会话。暂停点是
    `complete_second_factor` 事务里的第一条语句（锁用户行），它执行之前事务还没开始。
    """
    w = admin
    account = w.enrol()
    pending = w.pending_second_factor()
    code = second_factor_code(account, factor)
    link = w.reset_link()
    codes_before = w.live_code_ids()
    rows_before = len(w.refresh_rows())
    logins_before = len(w.audits(AuditAction.LOGIN))

    pause = w.pause(LOCK_USER, "/login/totp：pending 令牌解码之后、进入事务之前")
    second = w.background(
        "/login/totp",
        lambda: w.post(LOGIN_TOTP, json={"pending_token": pending, "code": code}),
        trap=pause,
    )
    pause.wait_reached(second)
    reset = w.background("重置", lambda: w.reset(link)).outcome()
    assert reset is None, f"重置应当成功：{reset!r}"
    pause.release()
    response = second.outcome()

    w.assert_no_database_error(reset, response)
    assert_error(response, 401, "INVALID_CREDENTIALS")
    setting = w.two_factor()
    assert setting is not None
    assert setting.last_used_counter is None, "TOTP 计数器不能动"
    assert w.live_code_ids() == codes_before, "恢复码不能被消耗"
    assert len(w.refresh_rows()) == rows_before, "不能签出会话"
    assert len(w.audits(AuditAction.LOGIN)) == logins_before
    assert w.user().password_version == 1


@pytest.mark.parametrize("factor", ["totp", "recovery_code"])
def test_a_reset_waits_for_a_second_factor_that_holds_the_user_row(
    admin: World, factor: str
) -> None:
    """② 重置在 `/login/totp` 持有用户行锁期间开始（重置等待）。

    只有 `/login/totp` 先提交这一种先后（任务契约的勘误）：照常登录、消耗一次，随后
    重置完成，该会话 401。
    """
    w = admin
    account = w.enrol()
    pending = w.pending_second_factor()
    code = second_factor_code(account, factor)
    link = w.reset_link()
    codes_before = w.live_code_ids()
    logins_before = len(w.audits(AuditAction.LOGIN))

    pause = w.pause(LOCK_USER, "/login/totp：已锁用户行", after=True)
    second = w.background(
        "/login/totp",
        lambda: w.post(LOGIN_TOTP, json={"pending_token": pending, "code": code}),
        trap=pause,
    )
    pause.wait_reached(second)
    w.assert_user_row_held("/login/totp 停在锁用户行之后", second)
    reset = w.background("重置", lambda: w.reset(link))
    w.wait_for_lock_wait("重置在等 /login/totp 持有的用户行锁", reset)
    assert not reset.finished, "重置应当在等锁"
    pause.release()
    response = second.outcome()
    reset_outcome = reset.outcome()

    w.assert_no_database_error(response, reset_outcome)
    assert reset_outcome is None, f"重置应当成功：{reset_outcome!r}"
    assert response.status_code == 200, response.text
    issued = SignedIn(
        access_token=response.json()["data"]["access_token"],
        refresh_token=issued_refresh_cookie(response),
    )
    assert len(w.audits(AuditAction.LOGIN)) == logins_before + 1

    # 消耗了一次，且只一次。
    setting = w.two_factor()
    assert setting is not None
    if factor == "totp":
        assert setting.last_used_counter is not None
        assert w.live_code_ids() == codes_before
    else:
        assert setting.last_used_counter is None
        live = w.live_code_ids()
        assert len(live) == len(codes_before) - 1
        assert set(live) < set(codes_before)

    assert w.user().password_version == 1
    assert w.pwv(issued.access_token, TOKEN_TYPE_ACCESS) == 0
    row = w.refresh_row(issued.refresh_token)
    assert row.password_version == 0
    assert row.revoked_at is not None, "重置吊销了它"
    w.assert_session_is_dead(issued)
    assert w.refresh_row(issued.refresh_token).used_at is None
    w.assert_no_replay_alarm()


# --- 第二因子失败与锁 ----------------------------------------------------------------------


def test_a_wrong_second_factor_releases_the_user_row_before_counting_the_failure(
    admin: World,
) -> None:
    """加锁之后输错 TOTP：401 `INVALID_TOTP`，失败计数 +1，不出现锁等待超时。

    失败计数在独立事务里写同一行；它开始写的那一刻，`/login/totp` 的事务必须已经提交、
    放开了用户行（否则它会等自己的锁，直到锁等待超时成 500）。暂停在写失败计数之前，
    从另一个连接 `FOR UPDATE NOWAIT` 证明此刻没有人持有用户行。
    """
    w = admin
    account = w.enrol()
    pending = w.pending_second_factor()
    totp = pyotp.TOTP(account.secret)
    wrong = next(
        candidate
        for candidate in ("000000", "111111", "222222", "333333")
        if not totp.verify(candidate, valid_window=1)
    )
    assert w.user().failed_login_count == 0

    pause = w.pause(COUNT_FAILURE, "失败计数：写失败计数之前")
    second = w.background(
        "/login/totp",
        lambda: w.post(LOGIN_TOTP, json={"pending_token": pending, "code": wrong}),
        trap=pause,
    )
    pause.wait_reached(second)
    # 它确实先锁过用户行（`complete_second_factor` 的第一条语句）。
    assert any(LOCK_USER.search(statement) for statement in pause.before), pause.before
    w.assert_user_row_unlocked("失败计数开始写之前")
    pause.release()
    response = second.outcome()

    w.assert_no_database_error(response)
    assert_error(response, 401, "INVALID_TOTP")
    assert w.user().failed_login_count == 1
    failures = w.audits(AuditAction.LOGIN_FAILED)
    assert [row.reason for row in failures] == ["BAD_SECOND_FACTOR"]
    setting = w.two_factor()
    assert setting is not None
    assert setting.last_used_counter is None
    assert w.refresh_rows() == [], "不发会话"


# --- 刷新与改密码并发 ----------------------------------------------------------------------


def test_a_reset_that_commits_while_a_refresh_waits_before_the_user_lock(admin: World) -> None:
    """刷新与改密码并发 ①。

    `refresh_session` 在不加锁读出令牌行之后、锁用户行之前停住；重置提交后放行刷新。
    401 `TOKEN_REUSED`，**没有** `TOKEN_REUSED` 审计，同一家族的其他行不被额外吊销。
    """
    w = admin
    first = w.sign_in()
    # 先轮换一次，让家族里有一条「其他行」（已用过的旧行）。
    rotated = w.refresh(first.refresh_token)
    current = rotated.refresh_token
    link = w.reset_link()

    pause = w.pause(LOCK_USER, "刷新：读出令牌行之后、锁用户行之前")
    refresh = w.background("刷新", lambda: w.refresh(current), trap=pause)
    pause.wait_reached(refresh)
    reset = w.background("重置", lambda: w.reset(link)).outcome()
    assert reset is None, f"重置应当成功：{reset!r}"
    after_reset = {row.id: (row.used_at, row.revoked_at) for row in w.refresh_rows()}
    pause.release()
    outcome = refresh.outcome()

    w.assert_no_database_error(reset, outcome)
    assert_app_error(outcome, 401, "TOKEN_REUSED")
    # 没有新行；家族里每一行（含这一行）都停在重置之后的样子：没有被标记已用，
    # 也没有被整链吊销再写一遍吊销时刻。
    assert len(after_reset) == 2
    assert {row.id: (row.used_at, row.revoked_at) for row in w.refresh_rows()} == after_reset
    assert w.refresh_row(current).used_at is None
    w.assert_no_replay_alarm()


def test_a_refresh_released_before_a_reset_issues_tokens_the_reset_ends(admin: World) -> None:
    """刷新与改密码并发 ②。

    同一个暂停点，先放行刷新、刷新提交后再重置：刷新照常返回新令牌，重置提交后新的
    访问令牌与刷新令牌都 401。
    """
    w = admin
    first = w.sign_in()
    rotated = w.refresh(first.refresh_token)
    current = rotated.refresh_token
    link = w.reset_link()

    pause = w.pause(LOCK_USER, "刷新：读出令牌行之后、锁用户行之前")
    refresh = w.background("刷新", lambda: w.refresh(current), trap=pause)
    pause.wait_reached(refresh)
    pause.release()
    issued = refresh.outcome()

    assert isinstance(issued, IssuedSession), f"刷新应当成功：{issued!r}"
    fresh = SignedIn(access_token=issued.access_token, refresh_token=issued.refresh_token)
    # 重置之前这一对令牌是有效的。
    response = w.get(LOGGED_IN, headers=bearer(fresh.access_token))
    assert response.status_code == 200, response.text

    reset = w.background("重置", lambda: w.reset(link)).outcome()
    assert reset is None, f"重置应当成功：{reset!r}"
    w.assert_no_database_error(issued, reset)
    assert w.pwv(fresh.access_token, TOKEN_TYPE_ACCESS) == 0
    assert w.refresh_row(fresh.refresh_token).password_version == 0
    w.assert_session_is_dead(fresh)
    assert w.refresh_row(fresh.refresh_token).used_at is None
    w.assert_no_replay_alarm()


def test_a_reset_waits_for_a_refresh_that_holds_the_user_row(admin: World) -> None:
    """刷新持锁时改密码（原死锁交错）。

    刷新已锁用户行并条件 UPDATE 了旧令牌行、尚未插入新行时停住；重置开始并等用户行锁；
    放行刷新。原先的加锁顺序（先令牌行、插入新行时才碰用户行）在这里会死锁；现在不出现
    死锁错误：刷新先提交并返回新令牌，重置随后拿到锁并提交，新的一对令牌都 401。
    """
    w = admin
    session = w.sign_in()
    link = w.reset_link()

    pause = w.pause(MARK_REFRESH_USED, "刷新：已锁用户行、已条件更新旧行、未插入新行", after=True)
    refresh = w.background("刷新", lambda: w.refresh(session.refresh_token), trap=pause)
    pause.wait_reached(refresh)
    w.assert_user_row_held("刷新停在条件更新之后", refresh)
    reset = w.background("重置", lambda: w.reset(link))
    w.wait_for_lock_wait("重置在等刷新持有的用户行锁", reset)
    assert not reset.finished, "重置应当在等锁"
    pause.release()
    issued = refresh.outcome()
    reset_outcome = reset.outcome()

    w.assert_no_database_error(issued, reset_outcome)
    assert isinstance(issued, IssuedSession), f"刷新应当成功：{issued!r}"
    assert reset_outcome is None, f"重置应当成功：{reset_outcome!r}"
    assert w.user().password_version == 1

    old = w.refresh_row(session.refresh_token)
    assert old.used_at is not None
    new = w.refresh_row(issued.refresh_token)
    assert new.password_version == 0
    assert new.revoked_at is not None, "重置在刷新之后提交，吊销了新行"
    assert w.pwv(issued.access_token, TOKEN_TYPE_ACCESS) == 0
    w.assert_session_is_dead(SignedIn(issued.access_token, issued.refresh_token))
    w.assert_no_replay_alarm()


# --- 死锁兜底 ----------------------------------------------------------------------------


def test_a_deadlock_while_inserting_the_new_row_rolls_the_refresh_back(admin: World) -> None:
    """刷新事务插入新行时抛 MySQL 1213 对应的 `OperationalError`。

    500 带 `request_id`；旧令牌未被标记已用、没有新行、没有重放审计；同一个刷新令牌
    重试成功。
    """
    w = admin
    session = w.sign_in()
    rows_before = [(row.id, row.used_at, row.revoked_at) for row in w.refresh_rows()]
    # 刷新走 cookie：jar 里此刻就是这次登录发的那一个。
    assert w.client.cookies.get(REFRESH_COOKIE_NAME) == session.refresh_token

    deadlock = Deadlock(INSERT_REFRESH)
    response = w.background("刷新", lambda: w.post(REFRESH), trap=deadlock).outcome()

    assert len(deadlock.fired) == 1, "注入点没有触发"
    assert_error(response, 500, "INTERNAL_ERROR")
    request_id = response.json()["request_id"]
    assert request_id
    assert response.headers.get(REQUEST_ID_HEADER) == request_id
    [record] = w.logs.unhandled
    assert record.exc_info is not None
    error = record.exc_info[1]
    assert isinstance(error, OperationalError)
    assert error.orig.args[0] == ER_LOCK_DEADLOCK

    assert [(row.id, row.used_at, row.revoked_at) for row in w.refresh_rows()] == rows_before
    w.assert_no_replay_alarm()

    # 500 不发 cookie，客户端手上还是同一个刷新令牌。
    assert w.client.cookies.get(REFRESH_COOKIE_NAME) == session.refresh_token
    retried = w.post(REFRESH)
    assert retried.status_code == 200, retried.text
    assert w.refresh_row(session.refresh_token).used_at is not None
    assert len(w.refresh_rows()) == len(rows_before) + 1
    w.assert_no_replay_alarm()


# --- 刷新与登出并发 ----------------------------------------------------------------------


def test_a_logout_while_a_refresh_holds_the_user_row_is_not_a_replay(admin: World) -> None:
    """刷新在锁用户行之后、条件 UPDATE 之前停住，另一个连接登出同一家族后放行刷新。

    401 `TOKEN_REUSED`，没有重放审计（修正既有误报）。
    """
    w = admin
    session = w.sign_in()

    pause = w.pause(MARK_REFRESH_USED, "刷新：已锁用户行、条件更新之前")
    refresh = w.background("刷新", lambda: w.refresh(session.refresh_token), trap=pause)
    pause.wait_reached(refresh)
    assert any(LOCK_USER.search(statement) for statement in pause.before), pause.before
    logged_out = w.background(
        "登出", lambda: logout(w.factory, raw_token=session.refresh_token, context=CONTEXT)
    ).outcome()
    assert logged_out is None, f"登出应当成功：{logged_out!r}"
    pause.release()
    outcome = refresh.outcome()

    w.assert_no_database_error(logged_out, outcome)
    assert_app_error(outcome, 401, "TOKEN_REUSED")
    row = w.refresh_row(session.refresh_token)
    assert row.used_at is None
    assert row.revoked_at is not None
    assert len(w.refresh_rows()) == 1, "没有新行"
    assert len(w.audits(AuditAction.LOGOUT)) == 1
    w.assert_no_replay_alarm()


# --- 真正的重放不受影响（两个并发刷新抢同一行） --------------------------------------------


def test_two_refreshes_racing_for_one_row_still_count_as_a_replay(admin: World) -> None:
    """两个并发刷新抢同一行：与现在一致，一个成功，另一个走 `_treat_as_replay`。

    第一个持有用户行锁时停住；第二个此时读到的令牌行快照还没被用过，然后等用户行锁。
    放行第一个，它提交；第二个拿到锁后条件更新落空、加锁重读看到 `used_at` 已置，
    按重放处理：整条链吊销、写 `TOKEN_REUSED` 审计、记 warning。
    """
    w = admin
    session = w.sign_in()

    pause = w.pause(LOCK_USER, "第一个刷新：已锁用户行", after=True)
    first = w.background("第一个刷新", lambda: w.refresh(session.refresh_token), trap=pause)
    pause.wait_reached(first)
    w.assert_user_row_held("第一个刷新停在锁用户行之后", first)
    second = w.background("第二个刷新", lambda: w.refresh(session.refresh_token))
    w.wait_for_lock_wait("第二个刷新在等用户行锁", second)
    assert not second.finished, "第二个刷新应当在等锁"
    pause.release()
    winner = first.outcome()
    loser = second.outcome()

    w.assert_no_database_error(winner, loser)
    assert isinstance(winner, IssuedSession), f"第一个刷新应当成功：{winner!r}"
    assert_app_error(loser, 401, "TOKEN_REUSED")

    family = w.refresh_row(session.refresh_token).family_id
    in_family = [row for row in w.refresh_rows() if row.family_id == family]
    assert len(in_family) == 2
    assert all(row.revoked_at is not None for row in in_family), "整条链吊销，包括赢家刚拿到的新行"
    audits = w.audits(AuditAction.TOKEN_REUSED)
    assert [(row.entity_id, row.reason) for row in audits] == [(family, "RACE_LOST")]
    assert len(w.logs.reuse_warnings) == 1


# --- 认证材料写入与改密码并发 --------------------------------------------------------------


@dataclass
class Writer:
    """One of the three operations that write authentication material.

    见设计 §2「在途请求的语义」。
    """

    write: Callable[[], object]
    # 改密码先提交：401，不写、不替换。
    refused: Callable[[object], None]
    # 写入先提交：写入成立。
    written: Callable[[object], None]
    # 写入先提交之后：旧会话 401。
    old_session_refused: Callable[[object], None]


def start_enrolment_writer(w: World) -> Writer:
    """`/2fa/enrol` with an `ENROL_2FA` pending token."""
    first = w.login()
    assert first["stage"] == STAGE_ENROL_2FA
    pending = first["pending_token"]
    assert w.pwv(pending, TOKEN_TYPE_PENDING_2FA) == 0

    def write() -> object:
        return w.post(ENROL, json={"pending_token": pending})

    def refused(outcome: object) -> None:
        assert_error(outcome, 401, "TOKEN_INVALID")
        assert "secret" not in outcome.text
        assert w.two_factor() is None, "不写任何 2FA 行"

    def written(outcome: object) -> None:
        assert outcome.status_code == 200, outcome.text
        setting = w.two_factor()
        assert setting is not None
        assert (setting.secret_version, setting.confirmed_at) == (1, None)

    def old_session_refused(outcome: object) -> None:
        secret = outcome.json()["data"]["secret"]
        response = w.post(
            CONFIRM, json={"pending_token": pending, "code": pyotp.TOTP(secret).now()}
        )
        assert_error(response, 401, "TOKEN_INVALID")
        setting = w.two_factor()
        assert setting is not None
        assert setting.confirmed_at is None
        assert w.live_code_ids() == []

    return Writer(write, refused, written, old_session_refused)


def confirm_enrolment_writer(w: World) -> Writer:
    """`/2fa/confirm` with the pending token the enrolment was started with."""
    first = w.login()
    assert first["stage"] == STAGE_ENROL_2FA
    pending = first["pending_token"]
    enrolment = w.post(ENROL, json={"pending_token": pending})
    assert enrolment.status_code == 200, enrolment.text
    secret = enrolment.json()["data"]["secret"]

    def write() -> object:
        return w.post(CONFIRM, json={"pending_token": pending, "code": pyotp.TOTP(secret).now()})

    def refused(outcome: object) -> None:
        assert_error(outcome, 401, "TOKEN_INVALID")
        setting = w.two_factor()
        assert setting is not None
        assert setting.confirmed_at is None, "2FA 不启用"
        assert w.live_code_ids() == [], "不发恢复码"
        assert w.audits(AuditAction.TWO_FACTOR_ENABLED) == []

    def written(outcome: object) -> None:
        assert outcome.status_code == 200, outcome.text
        assert len(outcome.json()["data"]["recovery_codes"]) == 10
        setting = w.two_factor()
        assert setting is not None
        assert setting.confirmed_at is not None
        assert len(w.live_code_ids()) == 10
        assert len(w.audits(AuditAction.TWO_FACTOR_ENABLED)) == 1

    def old_session_refused(outcome: object) -> None:
        codes = outcome.json()["data"]["recovery_codes"]
        response = w.post(LOGIN_TOTP, json={"pending_token": pending, "code": codes[0]})
        assert_error(response, 401, "INVALID_CREDENTIALS")
        assert len(w.live_code_ids()) == 10, "恢复码不被消耗"

    return Writer(write, refused, written, old_session_refused)


def regenerate_recovery_codes_writer(w: World) -> Writer:
    """`regenerate_recovery_codes` with the version of a session signed in before the reset.

    经接口、鉴权之后才停住的那一次另有一条用例。
    """
    account = w.enrol()
    session = w.sign_in_admin(account)
    version = w.pwv(session.access_token, TOKEN_TYPE_ACCESS)
    assert version == 0
    before = w.live_code_ids()
    assert before

    def write() -> object:
        return regenerate_recovery_codes(
            w.factory, user_id=w.user_id, password_version=version, context=CONTEXT
        )

    def refused(outcome: object) -> None:
        assert_app_error(outcome, 401, "TOKEN_INVALID")
        assert w.live_code_ids() == before, "不替换恢复码"
        assert w.audits(AuditAction.RECOVERY_CODES_REGENERATED) == []

    def written(outcome: object) -> None:
        assert isinstance(outcome, list), f"重新生成应当成功：{outcome!r}"
        assert len(outcome) == 10
        live = w.live_code_ids()
        assert len(live) == 10
        assert not set(live) & set(before), "旧码全部作废"
        assert len(w.audits(AuditAction.RECOVERY_CODES_REGENERATED)) == 1

    def old_session_refused(outcome: object) -> None:
        live = w.live_code_ids()
        response = w.get(LOGGED_IN, headers=bearer(session.access_token))
        assert_error(response, 401, "TOKEN_INVALID")
        response = w.post(
            RECOVERY_CODES, json={"password": NEW_PASSWORD}, headers=bearer(session.access_token)
        )
        assert_error(response, 401, "TOKEN_INVALID")
        assert w.live_code_ids() == live

    return Writer(write, refused, written, old_session_refused)


WRITERS: dict[str, Callable[[World], Writer]] = {
    "start_enrolment": start_enrolment_writer,
    "confirm_enrolment": confirm_enrolment_writer,
    "regenerate_recovery_codes": regenerate_recovery_codes_writer,
}


@pytest.mark.parametrize("name", list(WRITERS))
def test_a_reset_that_commits_first_stops_the_write(admin: World, name: str) -> None:
    """改密码先提交：写入方在锁用户行的语句之前停住，重置提交后放行 → 401，不写。"""
    w = admin
    writer = WRITERS[name](w)
    link = w.reset_link()

    pause = w.pause(LOCK_USER, f"{name}：锁用户行之前")
    write = w.background(name, writer.write, trap=pause)
    pause.wait_reached(write)
    reset = w.background("重置", lambda: w.reset(link)).outcome()
    assert reset is None, f"重置应当成功：{reset!r}"
    pause.release()
    outcome = write.outcome()

    w.assert_no_database_error(reset, outcome)
    writer.refused(outcome)
    assert w.user().password_version == 1


@pytest.mark.parametrize("name", list(WRITERS))
def test_a_write_that_commits_first_stands_and_the_reset_ends_its_session(
    admin: World, name: str
) -> None:
    """写入先提交：写入方在提交之前停住，此时开始重置并确认它在等锁，再放行写入。

    写入成立；重置随后提交，之后旧会话 401。暂停点是写入方锁了用户行的那个事务的
    提交，不是这个请求路径上任何更早的提交。
    """
    w = admin
    writer = WRITERS[name](w)
    link = w.reset_link()

    pause = w.pause_before_commit(f"{name}：已锁用户行、提交之前")
    write = w.background(name, writer.write, trap=pause)
    pause.wait_reached(write)
    assert any(LOCK_USER.search(statement) for statement in pause.before), pause.before
    w.assert_user_row_held(f"{name} 停在提交之前", write)
    reset = w.background("重置", lambda: w.reset(link))
    w.wait_for_lock_wait(f"重置在等 {name} 持有的用户行锁", reset)
    assert not reset.finished, "重置应当在等锁"
    pause.release()
    outcome = write.outcome()
    reset_outcome = reset.outcome()

    w.assert_no_database_error(outcome, reset_outcome)
    assert reset_outcome is None, f"重置应当成功：{reset_outcome!r}"
    writer.written(outcome)
    assert w.user().password_version == 1
    writer.old_session_refused(outcome)


def test_regenerating_codes_after_authentication_but_before_the_service_is_refused(
    admin: World,
) -> None:
    """`/2fa/recovery-codes`：`require_current_user` 已经返回、进入服务事务之前停住。

    重置提交后放行。

    鉴权早已放行，能拦住它的只有服务在自己的事务里锁用户行后的那次比较：401
    `TOKEN_INVALID`，不替换恢复码。
    """
    w = admin
    account = w.enrol()
    session = w.sign_in_admin(account)
    before = w.live_code_ids()
    link = w.reset_link()

    pause = w.pause(LOCK_USER, "/2fa/recovery-codes：鉴权之后、进入服务事务之前")
    regenerate = w.background(
        "/2fa/recovery-codes",
        lambda: w.post(
            RECOVERY_CODES, json={"password": PASSWORD}, headers=bearer(session.access_token)
        ),
        trap=pause,
    )
    pause.wait_reached(regenerate)
    # 鉴权确实已经过了：它那次不加锁的读用户发生在暂停之前。
    assert any(READ_USER.search(statement) for statement in pause.before), pause.before
    reset = w.background("重置", lambda: w.reset(link)).outcome()
    assert reset is None, f"重置应当成功：{reset!r}"
    pause.release()
    response = regenerate.outcome()

    w.assert_no_database_error(reset, response)
    assert_error(response, 401, "TOKEN_INVALID")
    assert w.live_code_ids() == before, "不替换恢复码"
    assert w.audits(AuditAction.RECOVERY_CODES_REGENERATED) == []
    assert w.user().password_version == 1

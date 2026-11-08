"""SQLite outbox core (design gate #571 v24 §2「存储（SQLite）」「事务边界」「投递」, §4).

本模块（AIH-TASK-165）：建表与两种打开方式、版本保护、单进程锁、连接登记与作废、
文件替换检测、写事件与去重、领取与租约栅栏、落回执、清理、投递相关的只读计数。
死信运维、重放、`ops_log`、`incidents`、`status_state` 与 `reconcile_state` 的读写
属于 AIH-TASK-166；这里只建这四张表。不发 HTTP、不做内存暂存、不起线程。

- 每个线程各用自己的连接；全部连接以 `check_same_thread=False` 建立并登记。每次使用
  连接时持登记表的共享锁，`invalidate()` 取排他锁、关闭全部已登记连接、代号加一。
  重新打开（含新建库）只在 `reopen()` 里、关掉全部旧连接之后进行，各线程不会自行
  重连或建文件：线程级连接一律以 `mode=rw` 打开，路径上没有文件时不会被建出来。
- 每次使用连接前比对路径当前的文件身份 `(st_dev, st_ino)`：变了或路径不存在就抛
  `StoreReplacedError`，由门面（AIH-TASK-168）`invalidate()` 后转入降级。
- 本模块不自己调 `invalidate()`：持着共享锁的调用里再取排他锁会自锁。
- `ops_log` 只增不改：本模块没有更新或删除它的代码（写入也在 AIH-TASK-166）。
"""

from __future__ import annotations

import errno
import os
import re
import sqlite3
import sys
import threading
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Final, NamedTuple

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl

SCHEMA_VERSION: Final = 1
BUSY_TIMEOUT_MS: Final = 5000
# 批量请求体 `{"events":[` + 各条 payload 用 `,` 连接 + `]}`（§2「投递」）。
_BATCH_PREFIX: Final = b'{"events":['
_BATCH_SUFFIX: Final = b"]}"
AUTH_FAILED_CODE: Final = "INTEGRATION_AUTH_FAILED"

_VERSION_TEXT: Final = re.compile(r"[1-9][0-9]*")
_TABLES: Final = frozenset(
    {"meta", "usage_outbox", "status_state", "reconcile_state", "ops_log", "incidents"}
)

# 首次打开时在**一个** BEGIN IMMEDIATE 事务里逐条执行（不用 executescript：它会先提交）。
_SCHEMA_STATEMENTS: tuple[str, ...] = (
    # 主键列另加 NOT NULL：SQLite 的 TEXT PRIMARY KEY 本身放过 NULL。
    "CREATE TABLE meta (key TEXT NOT NULL PRIMARY KEY, value TEXT NOT NULL)",
    """
    CREATE TABLE usage_outbox (
        event_id TEXT NOT NULL PRIMARY KEY,
        payload BLOB NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('PENDING', 'SENDING', 'DELIVERED', 'DEAD')),
        attempts INTEGER NOT NULL DEFAULT 0,
        next_attempt_at REAL NOT NULL,
        lease_until REAL,
        last_error_code TEXT,
        created_at REAL NOT NULL,
        delivered_at REAL,
        dead_at REAL,
        dead_acked_at REAL
    )
    """,
    "CREATE INDEX usage_outbox_status_next_attempt ON usage_outbox (status, next_attempt_at)",
    """
    CREATE TABLE status_state (
        tenant_id TEXT NOT NULL,
        project_id TEXT NOT NULL,
        billing_mode TEXT NOT NULL,
        billing_status TEXT NOT NULL,
        account_status TEXT NOT NULL,
        project_integration_status TEXT NOT NULL,
        effective_status TEXT NOT NULL CHECK (effective_status IN ('ALLOW_AI', 'BLOCK_AI')),
        status_version INTEGER NOT NULL,
        reason_code TEXT,
        effective_at TEXT NOT NULL,
        applied_at REAL NOT NULL,
        PRIMARY KEY (tenant_id, project_id)
    )
    """,
    """
    CREATE TABLE reconcile_state (
        tenant_id TEXT NOT NULL,
        project_id TEXT NOT NULL,
        last_ok_at REAL,
        last_error_at REAL,
        PRIMARY KEY (tenant_id, project_id)
    )
    """,
    """
    CREATE TABLE ops_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        action TEXT NOT NULL CHECK (
            action IN (
                'REQUEUE_DEAD', 'ACK_DEAD', 'REPLAY_DELIVERED', 'ACK_INCIDENT', 'RESET_STATUS'
            )
        ),
        target_id TEXT NOT NULL,
        reason TEXT NOT NULL,
        at REAL NOT NULL
    )
    """,
    """
    CREATE TABLE incidents (
        incident_id TEXT NOT NULL PRIMARY KEY,
        kind TEXT NOT NULL CHECK (kind IN ('DROPPED', 'CONFLICT_DROPPED', 'OVERFLOW_DROPPED')),
        event_id TEXT,
        dropped_count INTEGER NOT NULL,
        first_occurred_at REAL NOT NULL,
        last_occurred_at REAL NOT NULL,
        recorded_at REAL NOT NULL,
        acked_at REAL,
        ack_reason TEXT
    )
    """,
)


# --- 异常 -----------------------------------------------------------------------------


class StoreError(Exception):
    """Base class of every store-level failure defined here."""


class StoreMissingError(StoreError):
    """Normal open: the directory does not exist. Open-existing: the file is missing or empty."""


class StoreForeignError(StoreError):
    """Not an outbox of this client: not SQLite, other tables without `meta`, or missing tables."""

    def __init__(self, message: str, *, sqlite_errorname: str | None = None) -> None:
        super().__init__(message)
        # 由 sqlite3 错误引起时保留原错误名（例如 SQLITE_NOTADB），门面据此判断坏文件。
        self.sqlite_errorname = sqlite_errorname


class StoreVersionError(StoreError):
    """`schema_version` is not a positive integer or is newer than this code."""


class StoreLockedError(StoreError):
    """Another process holds the single-process lock on `<path>.lock`."""


class StoreResidueError(StoreError):
    """The main file is gone but `-wal` / `-shm` are still there; nothing is created."""

    def __init__(self, files: list[str]) -> None:
        super().__init__("outbox residue files present: " + ", ".join(files))
        self.files = files


class StoreReplacedError(StoreError):
    """The file at the path was moved away or replaced since it was opened."""


class StoreUnavailableError(StoreError):
    """The store is closed: after `invalidate()` / `close()` and before a successful `reopen()`."""


class StoreIntegrityFault(StoreError):
    """INSERT raised IntegrityError but no row with that event_id exists (NOT NULL, CHECK…).

    不是 `sqlite3.IntegrityError` 的子类：门面按存储故障处理（进暂存、不抛给接入方）。
    """


class EventConflictError(Exception):
    """Same event_id already stored with different payload bytes; the stored row is unchanged."""

    def __init__(self, event_id: str) -> None:
        super().__init__(f"event_id {event_id} already exists with different content")
        self.event_id = event_id


# --- 返回值与入参 ---------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ClaimedEvent:
    event_id: str
    payload: bytes
    # 加 1 之后的值。
    attempts: int
    # 上一次失败的码；领取不改它。
    last_error_code: str | None
    # 领取时写下的租约，落回执与续租的栅栏。
    lease_until: float


class Fence(NamedTuple):
    event_id: str
    lease_until: float


class RetryRow(NamedTuple):
    event_id: str
    lease_until: float
    next_attempt_at: float
    error_code: str


@dataclass(frozen=True, slots=True)
class DeliveryCounts:
    pending: int
    sending: int
    delivered: int
    dead: int
    dead_unacked: int
    # PENDING 与 SENDING（含租约已过期的）行里最早的 created_at；没有这类行为 None。
    oldest_unsent_created_at: float | None
    # 存在 last_error_code 为 INTEGRATION_AUTH_FAILED 的 PENDING 或 SENDING 行。
    auth_failing: bool


# --- 单进程锁与登记表的读写锁 ---------------------------------------------------------


class _ProcessLock:
    """Exclusive, non-blocking OS lock on `<path>.lock`; the file is never written or deleted."""

    def __init__(self, path: str) -> None:
        self._path = path
        self._fd: int | None = None

    @property
    def held(self) -> bool:
        return self._fd is not None

    def acquire(self) -> None:
        fd = os.open(self._path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            if sys.platform == "win32":
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            if exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK, errno.EACCES, errno.EDEADLK):
                message = f"outbox is in use by another process: {self._path}"
                raise StoreLockedError(message) from exc
            raise
        self._fd = fd

    def release(self) -> None:
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            if sys.platform == "win32":
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        finally:
            # POSIX 上关闭描述符即释放 flock。
            os.close(fd)


class _SharedExclusiveLock:
    """Readers share; one writer excludes all. Waiting writers block new readers. Not reentrant."""

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._readers = 0
        self._writer = False
        self._writers_waiting = 0

    @contextmanager
    def shared(self) -> Iterator[None]:
        with self._cond:
            while self._writer or self._writers_waiting:
                self._cond.wait()
            self._readers += 1
        try:
            yield
        finally:
            with self._cond:
                self._readers -= 1
                if self._readers == 0:
                    self._cond.notify_all()

    @contextmanager
    def exclusive(self) -> Iterator[None]:
        with self._cond:
            self._writers_waiting += 1
            try:
                while self._writer or self._readers:
                    self._cond.wait()
            finally:
                self._writers_waiting -= 1
            self._writer = True
        try:
            yield
        finally:
            with self._cond:
                self._writer = False
                self._cond.notify_all()


def _rollback_quietly(conn: sqlite3.Connection) -> None:
    try:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
    except sqlite3.Error:
        pass


def _close_quietly(conn: sqlite3.Connection) -> None:
    try:
        conn.close()
    except sqlite3.Error:
        pass


# --- Store ----------------------------------------------------------------------------


class Store:
    """The outbox file. `Store(path, *, clock)` opens or creates; `open_existing` never creates."""

    def __init__(self, path: str | os.PathLike[str], *, clock: Callable[[], float]) -> None:
        self._setup(path, clock, existing=False)

    @classmethod
    def open_existing(cls, path: str | os.PathLike[str], *, clock: Callable[[], float]) -> Store:
        """Open without creating tables or changing the journal mode (command line, AIH-TASK-170).

        文件不存在、零字节时直接抛 `StoreMissingError`，不建任何文件（包括 `<path>.lock`）；
        之后才取单进程锁。不是 SQLite、没有 `meta` 表、`schema_version` 比代码新都抛错。
        """
        store = cls.__new__(cls)
        store._setup(path, clock, existing=True)
        return store

    def _setup(
        self, path: str | os.PathLike[str], clock: Callable[[], float], *, existing: bool
    ) -> None:
        self._path = os.path.abspath(os.fspath(path))
        self._uri = Path(self._path).as_uri()
        self._clock = clock
        self._existing = existing
        self._lock = _ProcessLock(self._path + ".lock")
        self._rw = _SharedExclusiveLock()
        self._registry_mutex = threading.Lock()
        self._connections: dict[int, sqlite3.Connection] = {}
        self._local = threading.local()
        self._generation = 0
        self._identity: tuple[int, int] | None = None
        self._open = False
        self._precheck()
        self._lock.acquire()
        try:
            with self._rw.exclusive():
                self._open_locked()
        except BaseException:
            self._lock.release()
            raise

    # --- 生命周期 ---

    @property
    def path(self) -> str:
        return self._path

    @property
    def is_open(self) -> bool:
        return self._open

    def invalidate(self) -> None:
        """Close every registered connection; the store stays closed until `reopen()`.

        等正在使用连接的调用结束（排他锁）后执行；单进程锁仍然持有。不能在持着本 Store
        共享锁的调用里调用。
        """
        with self._rw.exclusive():
            self._close_connections()

    def reopen(self) -> None:
        """Close old connections, then open (or create, only if path, -wal, -shm are all absent).

        失败照常抛错，Store 保持关闭。`close()` 之后调用会重新取单进程锁，失败时放掉。
        """
        acquired = False
        if not self._lock.held:
            self._precheck()
            self._lock.acquire()
            acquired = True
        try:
            with self._rw.exclusive():
                self._close_connections()
                self._open_locked()
        except BaseException:
            if acquired:
                self._lock.release()
            raise

    def close(self) -> None:
        """Close every registered connection and release the single-process lock. Idempotent."""
        with self._rw.exclusive():
            self._close_connections()
            self._lock.release()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def _close_connections(self) -> None:
        with self._registry_mutex:
            connections = list(self._connections.values())
            self._connections.clear()
        for conn in connections:
            _close_quietly(conn)
        self._generation += 1
        self._open = False

    # --- 打开与建表 ---

    def _precheck(self) -> None:
        if self._existing:
            try:
                st = os.stat(self._path)
            except FileNotFoundError:
                raise StoreMissingError(f"outbox file does not exist: {self._path}") from None
            if not os.path.isfile(self._path) or st.st_size == 0:
                raise StoreMissingError(f"outbox file is empty or not a file: {self._path}")
        elif not os.path.isdir(os.path.dirname(self._path)):
            # 不替接入方建目录，免得写到容器的临时层。
            raise StoreMissingError(f"outbox directory does not exist: {self._path}")

    def _open_locked(self) -> None:
        self._precheck()
        conn = self._open_existing_file() if self._existing else self._open_or_create()
        identity = self._identity
        assert identity is not None
        with self._registry_mutex:
            self._connections[id(conn)] = conn
        # 打开时用的连接留作当前线程的连接：库一直有连接，`-wal` 不会在两次使用之间被删掉。
        self._local.conn = conn
        self._local.generation = self._generation
        self._local.identity = identity
        self._open = True

    def _open_or_create(self) -> sqlite3.Connection:
        if not os.path.lexists(self._path):
            residue = [p for p in (self._path + "-wal", self._path + "-shm") if os.path.lexists(p)]
            if residue:
                # 新库会与旧 WAL 配错对：不新建、不碰这些文件。
                raise StoreResidueError(residue)
            conn = self._connect("rwc")
            try:
                self._configure(conn, wal=True)
                self._identity = self._path_identity()
                self._create_schema(conn)
            except BaseException:
                _close_quietly(conn)
                raise
            return conn
        conn = self._connect_checked()
        try:
            populated = self._inspect(conn, allow_empty=True)
            # 校验通过之后才改日志模式：外来库与版本更新的库不被改动。
            self._configure(conn, wal=True)
            if not populated:
                self._create_schema(conn)
        except BaseException:
            _close_quietly(conn)
            raise
        return conn

    def _open_existing_file(self) -> sqlite3.Connection:
        conn = self._connect_checked()
        try:
            self._inspect(conn, allow_empty=False)
            self._configure(conn, wal=False)
        except BaseException:
            _close_quietly(conn)
            raise
        return conn

    def _connect(self, mode: str) -> sqlite3.Connection:
        conn = sqlite3.connect(
            f"{self._uri}?mode={mode}",
            uri=True,
            timeout=BUSY_TIMEOUT_MS / 1000,
            isolation_level=None,
            check_same_thread=False,
        )
        try:
            conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        except BaseException:
            _close_quietly(conn)
            raise
        return conn

    def _connect_checked(self) -> sqlite3.Connection:
        """Connect to the existing file and record its identity; it must not change meanwhile."""
        before = self._path_identity()
        conn = self._connect("rw")
        try:
            if self._path_identity() != before:
                raise StoreReplacedError(f"outbox file changed while opening: {self._path}")
        except BaseException:
            _close_quietly(conn)
            raise
        self._identity = before
        return conn

    def _path_identity(self) -> tuple[int, int]:
        try:
            st = os.stat(self._path)
        except FileNotFoundError:
            raise StoreReplacedError(f"outbox file is gone: {self._path}") from None
        return st.st_dev, st.st_ino

    def _configure(self, conn: sqlite3.Connection, *, wal: bool) -> None:
        if wal:
            conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = FULL")
        conn.execute("PRAGMA foreign_keys = ON")

    def _inspect(self, conn: sqlite3.Connection, *, allow_empty: bool) -> bool:
        """Return True for a complete outbox, False for an empty SQLite file; raise otherwise."""
        try:
            objects = conn.execute("SELECT type, name FROM sqlite_master").fetchall()
        except sqlite3.DatabaseError as exc:
            name = getattr(exc, "sqlite_errorname", None)
            if name == "SQLITE_NOTADB":
                raise StoreForeignError(
                    f"not an SQLite database: {self._path}", sqlite_errorname=name
                ) from exc
            raise
        if not objects:
            if allow_empty:
                return False
            raise StoreForeignError(f"no meta table: {self._path}")
        tables = {name for kind, name in objects if kind == "table"}
        if "meta" not in tables:
            raise StoreForeignError(f"no meta table: {self._path}")
        row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
        if row is None:
            raise StoreForeignError(f"meta has no schema_version: {self._path}")
        text = str(row[0])
        if _VERSION_TEXT.fullmatch(text) is None:
            raise StoreVersionError(f"schema_version is not a positive integer: {self._path}")
        if int(text) > SCHEMA_VERSION:
            # 防止旧版客户端写坏新格式：不改任何表。
            raise StoreVersionError(
                f"schema_version {text} is newer than {SCHEMA_VERSION}: {self._path}"
            )
        missing = sorted(_TABLES - tables)
        if missing:
            raise StoreForeignError(f"missing tables {', '.join(missing)}: {self._path}")
        return True

    def _create_schema(self, conn: sqlite3.Connection) -> None:
        conn.execute("BEGIN IMMEDIATE")
        try:
            if conn.execute("SELECT count(*) FROM sqlite_master").fetchone()[0]:
                raise StoreForeignError(f"database is not empty: {self._path}")
            for statement in _SCHEMA_STATEMENTS:
                conn.execute(statement)
            conn.execute(
                "INSERT INTO meta (key, value) VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),)
            )
            conn.execute("COMMIT")
        except BaseException:
            # 中途失败整体回滚，不留半建的库。
            _rollback_quietly(conn)
            raise

    # --- 线程级连接 ---

    def _connection(self) -> sqlite3.Connection:
        """The calling thread's connection; call only while holding the shared lock."""
        if not self._open:
            raise StoreUnavailableError(f"outbox store is closed: {self._path}")
        local = self._local
        conn: sqlite3.Connection | None = getattr(local, "conn", None)
        if conn is None or local.generation != self._generation:
            local.conn = None
            conn = self._new_connection()
            local.conn = conn
            local.generation = self._generation
            local.identity = self._identity
        if self._path_identity() != local.identity:
            raise StoreReplacedError(f"outbox file was replaced: {self._path}")
        return conn

    def _new_connection(self) -> sqlite3.Connection:
        # 路径上必须仍是 open / reopen 校验过的那个文件，不能绕过表结构、外来库与残留检查。
        if self._path_identity() != self._identity:
            raise StoreReplacedError(f"outbox file was replaced: {self._path}")
        conn = self._connect("rw")
        try:
            if self._path_identity() != self._identity:
                raise StoreReplacedError(f"outbox file was replaced: {self._path}")
            self._configure(conn, wal=not self._existing)
        except BaseException:
            _close_quietly(conn)
            raise
        with self._registry_mutex:
            self._connections[id(conn)] = conn
        return conn

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        """One BEGIN IMMEDIATE transaction; any exception rolls the whole of it back."""
        with self._rw.shared():
            conn = self._connection()
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                conn.execute("COMMIT")
            except BaseException:
                _rollback_quietly(conn)
                raise

    @contextmanager
    def _read(self) -> Iterator[sqlite3.Connection]:
        """One deferred read transaction, so several queries see the same snapshot."""
        with self._rw.shared():
            conn = self._connection()
            conn.execute("BEGIN")
            try:
                yield conn
            finally:
                _rollback_quietly(conn)

    # --- 写事件 ---

    def insert_event(self, event_id: str, payload: bytes) -> str:
        """Insert a PENDING row, or return `event_id` if the same bytes are already stored.

        同 `event_id` 不同字节抛 `EventConflictError`、不改原行。INSERT 撞主键时按同一规则
        比对，不当作存储故障；库里没有这一行的 `IntegrityError`（NOT NULL、CHECK 等）包装成
        `StoreIntegrityFault`。其他 `sqlite3.Error` 原样抛出。
        """
        now = float(self._clock())
        with self._write() as conn:
            existing = self._find_existing_payload(conn, event_id)
            if existing is not None:
                return self._same_or_conflict(event_id, existing, payload)
            try:
                conn.execute(
                    "INSERT INTO usage_outbox"
                    " (event_id, payload, status, attempts, next_attempt_at, created_at)"
                    " VALUES (?, ?, 'PENDING', 0, ?, ?)",
                    (event_id, payload, now, now),
                )
            except sqlite3.IntegrityError as exc:
                row = conn.execute(
                    "SELECT payload FROM usage_outbox WHERE event_id = ?", (event_id,)
                ).fetchone()
                if row is None:
                    raise StoreIntegrityFault(
                        f"outbox row for {event_id} violates a constraint"
                    ) from exc
                return self._same_or_conflict(event_id, bytes(row[0]), payload)
        return event_id

    def _find_existing_payload(self, conn: sqlite3.Connection, event_id: str) -> bytes | None:
        row = conn.execute(
            "SELECT payload FROM usage_outbox WHERE event_id = ?", (event_id,)
        ).fetchone()
        return None if row is None else bytes(row[0])

    @staticmethod
    def _same_or_conflict(event_id: str, existing: bytes, payload: bytes) -> str:
        if existing != payload:
            raise EventConflictError(event_id)
        return event_id

    # --- 领取与续租 ---

    def claim(self, *, max_events: int, max_bytes: int, lease_seconds: float) -> list[ClaimedEvent]:
        """Claim due PENDING and lease-expired SENDING rows in `(created_at, event_id)` order.

        累加到 `max_events` 条，或下一条会让批量请求体超过 `max_bytes` 为止；第一条单独超过
        时仍领这一条。置 SENDING、写租约、`attempts + 1`，不改 `last_error_code`。
        """
        if max_events < 1 or max_bytes < 1 or lease_seconds <= 0:
            raise ValueError("max_events, max_bytes and lease_seconds must be positive")
        now = float(self._clock())
        lease_until = now + lease_seconds
        chosen: list[ClaimedEvent] = []
        with self._write() as conn:
            cursor = conn.execute(
                "SELECT event_id, payload, attempts, last_error_code FROM usage_outbox"
                " WHERE (status = 'PENDING' AND next_attempt_at <= ?)"
                " OR (status = 'SENDING' AND lease_until < ?)"
                " ORDER BY created_at, event_id",
                (now, now),
            )
            try:
                size = len(_BATCH_PREFIX) + len(_BATCH_SUFFIX)
                for event_id, payload, attempts, last_error_code in cursor:
                    if len(chosen) >= max_events:
                        break
                    added = len(payload) + (1 if chosen else 0)
                    if chosen and size + added > max_bytes:
                        break
                    size += added
                    chosen.append(
                        ClaimedEvent(
                            event_id=event_id,
                            payload=bytes(payload),
                            attempts=attempts + 1,
                            last_error_code=last_error_code,
                            lease_until=lease_until,
                        )
                    )
            finally:
                cursor.close()
            for event in chosen:
                conn.execute(
                    "UPDATE usage_outbox SET status = 'SENDING', lease_until = ?,"
                    " attempts = attempts + 1 WHERE event_id = ?",
                    (lease_until, event.event_id),
                )
        return chosen

    def renew_lease(self, event_ids: Iterable[str], old_lease: float, new_lease: float) -> int:
        """Extend the lease of rows still fenced by `old_lease`; returns the rows affected."""
        affected = 0
        with self._write() as conn:
            for event_id in event_ids:
                affected += conn.execute(
                    "UPDATE usage_outbox SET lease_until = ?"
                    " WHERE event_id = ? AND status = 'SENDING' AND lease_until = ?",
                    (new_lease, event_id, old_lease),
                ).rowcount
        return affected

    # --- 落回执 ---
    # 每行的栅栏：status = SENDING AND lease_until = 领取时写下的值。栅栏失效的行不改、
    # 不在返回的集合里；事务中途抛错则这一组都不改、停在 SENDING。

    def mark_delivered(self, rows: Iterable[Fence]) -> set[str]:
        now = float(self._clock())
        updated: set[str] = set()
        with self._write() as conn:
            for event_id, lease_until in rows:
                cursor = conn.execute(
                    "UPDATE usage_outbox SET status = 'DELIVERED', delivered_at = ?,"
                    " lease_until = NULL, last_error_code = NULL"
                    " WHERE event_id = ? AND status = 'SENDING' AND lease_until = ?",
                    (now, event_id, lease_until),
                )
                if cursor.rowcount:
                    updated.add(event_id)
        return updated

    def mark_retry(self, rows: Iterable[RetryRow]) -> set[str]:
        updated: set[str] = set()
        with self._write() as conn:
            for event_id, lease_until, next_attempt_at, error_code in rows:
                cursor = conn.execute(
                    "UPDATE usage_outbox SET status = 'PENDING', next_attempt_at = ?,"
                    " lease_until = NULL, last_error_code = ?"
                    " WHERE event_id = ? AND status = 'SENDING' AND lease_until = ?",
                    (next_attempt_at, error_code, event_id, lease_until),
                )
                if cursor.rowcount:
                    updated.add(event_id)
        return updated

    def mark_dead(self, rows: Iterable[Fence], error_code: str) -> set[str]:
        # 同一组共用一个错误码；再次死信的行重新算作未确认。
        now = float(self._clock())
        updated: set[str] = set()
        with self._write() as conn:
            for event_id, lease_until in rows:
                cursor = conn.execute(
                    "UPDATE usage_outbox SET status = 'DEAD', dead_at = ?, dead_acked_at = NULL,"
                    " lease_until = NULL, last_error_code = ?"
                    " WHERE event_id = ? AND status = 'SENDING' AND lease_until = ?",
                    (now, error_code, event_id, lease_until),
                )
                if cursor.rowcount:
                    updated.add(event_id)
        return updated

    # --- 清理 ---

    def purge_delivered(self, older_than: float, limit: int = 1000) -> int:
        """Delete up to `limit` DELIVERED rows with `delivered_at < older_than`; DEAD rows stay.

        `older_than` 由调用方按 `delivered_retention_seconds` 算。
        """
        if limit < 1:
            raise ValueError("limit must be positive")
        with self._write() as conn:
            return conn.execute(
                "DELETE FROM usage_outbox WHERE event_id IN ("
                " SELECT event_id FROM usage_outbox"
                " WHERE status = 'DELIVERED' AND delivered_at < ?"
                " ORDER BY delivered_at LIMIT ?)",
                (older_than, limit),
            ).rowcount

    # --- meta：last_delivery_ok_at ---

    def record_delivery_ok(self) -> float:
        now = float(self._clock())
        with self._write() as conn:
            conn.execute(
                "INSERT INTO meta (key, value) VALUES ('last_delivery_ok_at', ?)"
                " ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                (repr(now),),
            )
        return now

    def read_last_delivery_ok_at(self) -> float | None:
        with self._read() as conn:
            row = conn.execute(
                "SELECT value FROM meta WHERE key = 'last_delivery_ok_at'"
            ).fetchone()
        return None if row is None else float(row[0])

    # --- 投递相关的只读计数 ---

    def delivery_counts(self) -> DeliveryCounts:
        with self._read() as conn:
            by_status = dict(
                conn.execute("SELECT status, count(*) FROM usage_outbox GROUP BY status").fetchall()
            )
            dead_unacked = conn.execute(
                "SELECT count(*) FROM usage_outbox WHERE status = 'DEAD' AND dead_acked_at IS NULL"
            ).fetchone()[0]
            oldest = conn.execute(
                "SELECT min(created_at) FROM usage_outbox WHERE status IN ('PENDING', 'SENDING')"
            ).fetchone()[0]
            auth_failing = conn.execute(
                "SELECT EXISTS (SELECT 1 FROM usage_outbox"
                " WHERE status IN ('PENDING', 'SENDING') AND last_error_code = ?)",
                (AUTH_FAILED_CODE,),
            ).fetchone()[0]
        return DeliveryCounts(
            pending=by_status.get("PENDING", 0),
            sending=by_status.get("SENDING", 0),
            delivered=by_status.get("DELIVERED", 0),
            dead=by_status.get("DEAD", 0),
            dead_unacked=dead_unacked,
            oldest_unsent_created_at=oldest,
            auth_failing=bool(auth_failing),
        )


__all__ = [
    "AUTH_FAILED_CODE",
    "SCHEMA_VERSION",
    "ClaimedEvent",
    "DeliveryCounts",
    "EventConflictError",
    "Fence",
    "RetryRow",
    "Store",
    "StoreError",
    "StoreForeignError",
    "StoreIntegrityFault",
    "StoreLockedError",
    "StoreMissingError",
    "StoreReplacedError",
    "StoreResidueError",
    "StoreUnavailableError",
    "StoreVersionError",
]

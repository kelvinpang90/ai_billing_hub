"""SQLite outbox core (design gate #571 v24 §2「存储」「投递」, §4, §7 rows marked 165).

载荷是测试里直接构造的字节，不经 events.py。SQLite 用 tmp_path 下的文件，时间用注入的
时钟；并发用例用两个线程各自的连接。在打开状态下移动或替换文件的用例依赖 POSIX 语义，
在 win32 上跳过（生产与 CI 都是 Linux）。
"""

from __future__ import annotations

import multiprocessing
import os
import queue
import sqlite3
import sys
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from acuven_billing_client import (
    EventConflictError,
    Fence,
    RetryRow,
    Store,
    StoreError,
    StoreForeignError,
    StoreIntegrityFault,
    StoreLockedError,
    StoreMissingError,
    StoreReplacedError,
    StoreResidueError,
    StoreUnavailableError,
    StoreVersionError,
)
from acuven_billing_client import store as store_module

T0 = 1_790_000_000.0
DAY = 86_400.0
LEASE = 60.0
TABLES = {"meta", "usage_outbox", "status_state", "reconcile_state", "ops_log", "incidents"}

posix_only = pytest.mark.skipif(
    sys.platform == "win32", reason="moving an open file needs POSIX semantics"
)


class FakeClock:
    def __init__(self, now: float = T0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def payload(event_id: str, variant: str = "a") -> bytes:
    return f'{{"event_id":"{event_id}","v":"{variant}"}}'.encode()


class Worker:
    """A long-lived thread, so its store connection survives between calls."""

    def __init__(self) -> None:
        self._tasks: queue.Queue[Any] = queue.Queue()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while True:
            item = self._tasks.get()
            if item is None:
                return
            fn, box = item
            try:
                box.put((True, fn()))
            except BaseException as exc:
                box.put((False, exc))

    def submit(self, fn: Callable[[], Any]) -> queue.Queue[Any]:
        box: queue.Queue[Any] = queue.Queue()
        self._tasks.put((fn, box))
        return box

    def call(self, fn: Callable[[], Any]) -> Any:
        ok, value = self.submit(fn).get(timeout=30)
        if not ok:
            raise value
        return value

    def stop(self) -> None:
        self._tasks.put(None)
        self._thread.join(timeout=30)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "outbox.db"


@pytest.fixture
def opened() -> Iterator[list[Store]]:
    stores: list[Store] = []
    yield stores
    for store in stores:
        store.close()


@pytest.fixture
def store(db_path: Path, clock: FakeClock, opened: list[Store]) -> Store:
    result = Store(db_path, clock=clock)
    opened.append(result)
    return result


@pytest.fixture
def workers() -> Iterator[Callable[[], Worker]]:
    started: list[Worker] = []

    def make() -> Worker:
        worker = Worker()
        started.append(worker)
        return worker

    yield make
    for worker in started:
        worker.stop()


def raw(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(path, isolation_level=None)


def read_rows(path: Path) -> dict[str, dict[str, Any]]:
    conn = raw(path)
    conn.row_factory = sqlite3.Row
    try:
        return {r["event_id"]: dict(r) for r in conn.execute("SELECT * FROM usage_outbox")}
    finally:
        conn.close()


def schema_snapshot(path: Path) -> tuple[list[tuple[Any, ...]], list[tuple[Any, ...]]]:
    conn = raw(path)
    try:
        master = conn.execute("SELECT type, name, sql FROM sqlite_master ORDER BY name").fetchall()
        meta = conn.execute("SELECT key, value FROM meta ORDER BY key").fetchall()
    finally:
        conn.close()
    return master, meta


def journal_mode(path: Path) -> str:
    conn = raw(path)
    try:
        return conn.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        conn.close()


def integrity(path: Path) -> str:
    conn = raw(path)
    try:
        return conn.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        conn.close()


def make_foreign_db(path: Path) -> None:
    conn = raw(path)
    try:
        conn.execute("CREATE TABLE notes (body TEXT)")
        conn.execute("INSERT INTO notes VALUES ('keep me')")
    finally:
        conn.close()


def make_outbox(path: Path, *event_ids: str) -> None:
    with Store(path, clock=FakeClock()) as created:
        for event_id in event_ids:
            created.insert_event(event_id, payload(event_id))


def claim_all(store: Store, max_events: int = 100) -> list[Any]:
    return store.claim(max_events=max_events, max_bytes=1_000_000, lease_seconds=LEASE)


def _zero_clock() -> float:
    return 0.0


def _child_open(path: str, existing: bool) -> None:
    opener = Store.open_existing if existing else Store
    try:
        child = opener(path, clock=_zero_clock)
    except StoreLockedError:
        sys.exit(3)
    child.close()
    sys.exit(0)


def run_child(path: Path, *, existing: bool) -> int | None:
    process = multiprocessing.get_context("spawn").Process(
        target=_child_open, args=(str(path), existing)
    )
    process.start()
    process.join(timeout=120)
    return process.exitcode


# --- 建表、连接设置与列 -----------------------------------------------------------


def test_first_open_creates_six_tables_index_and_version(store: Store, db_path: Path) -> None:
    master, meta = schema_snapshot(db_path)
    tables = {name for kind, name, _ in master if kind == "table"}
    assert TABLES <= tables
    assert meta == [("schema_version", "1")]
    conn = raw(db_path)
    try:
        index = conn.execute("PRAGMA index_list(usage_outbox)").fetchall()
        named = [row[1] for row in index if row[1] == "usage_outbox_status_next_attempt"]
        assert named
        columns = [r[2] for r in conn.execute(f"PRAGMA index_info({named[0]})")]
        assert columns == ["status", "next_attempt_at"]
    finally:
        conn.close()
    assert journal_mode(db_path) == "wal"


def test_connection_pragmas(store: Store) -> None:
    with store._rw.shared():
        conn = store._connection()
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == 2
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


EXPECTED_COLUMNS = {
    "meta": [("key", "TEXT", 1, 1), ("value", "TEXT", 1, 0)],
    "usage_outbox": [
        ("event_id", "TEXT", 1, 1),
        ("payload", "BLOB", 1, 0),
        ("status", "TEXT", 1, 0),
        ("attempts", "INTEGER", 1, 0),
        ("next_attempt_at", "REAL", 1, 0),
        ("lease_until", "REAL", 0, 0),
        ("last_error_code", "TEXT", 0, 0),
        ("created_at", "REAL", 1, 0),
        ("delivered_at", "REAL", 0, 0),
        ("dead_at", "REAL", 0, 0),
        ("dead_acked_at", "REAL", 0, 0),
    ],
    "status_state": [
        ("tenant_id", "TEXT", 1, 1),
        ("project_id", "TEXT", 1, 2),
        ("billing_mode", "TEXT", 1, 0),
        ("billing_status", "TEXT", 1, 0),
        ("account_status", "TEXT", 1, 0),
        ("project_integration_status", "TEXT", 1, 0),
        ("effective_status", "TEXT", 1, 0),
        ("status_version", "INTEGER", 1, 0),
        ("reason_code", "TEXT", 0, 0),
        ("effective_at", "TEXT", 1, 0),
        ("applied_at", "REAL", 1, 0),
    ],
    "reconcile_state": [
        ("tenant_id", "TEXT", 1, 1),
        ("project_id", "TEXT", 1, 2),
        ("last_ok_at", "REAL", 0, 0),
        ("last_error_at", "REAL", 0, 0),
    ],
    "ops_log": [
        ("id", "INTEGER", 0, 1),
        ("action", "TEXT", 1, 0),
        ("target_id", "TEXT", 1, 0),
        ("reason", "TEXT", 1, 0),
        ("at", "REAL", 1, 0),
    ],
    "incidents": [
        ("incident_id", "TEXT", 1, 1),
        ("kind", "TEXT", 1, 0),
        ("event_id", "TEXT", 0, 0),
        ("dropped_count", "INTEGER", 1, 0),
        ("first_occurred_at", "REAL", 1, 0),
        ("last_occurred_at", "REAL", 1, 0),
        ("recorded_at", "REAL", 1, 0),
        ("acked_at", "REAL", 0, 0),
        ("ack_reason", "TEXT", 0, 0),
    ],
}


def test_columns_types_nullability_and_keys(store: Store, db_path: Path) -> None:
    conn = raw(db_path)
    try:
        for table, expected in EXPECTED_COLUMNS.items():
            info = conn.execute(f"PRAGMA table_info({table})").fetchall()
            assert [(r[1], r[2], r[3], r[5]) for r in info] == expected, table
    finally:
        conn.close()


@pytest.mark.parametrize(
    "statement",
    [
        "INSERT INTO usage_outbox (event_id, payload, status, next_attempt_at, created_at)"
        " VALUES ('X', x'00', 'BOGUS', 0, 0)",
        "INSERT INTO ops_log (action, target_id, reason, at) VALUES ('DELETE', 't', 'r', 0)",
        "INSERT INTO incidents (incident_id, kind, dropped_count, first_occurred_at,"
        " last_occurred_at, recorded_at) VALUES ('i', 'BOGUS', 1, 0, 0, 0)",
        "INSERT INTO status_state VALUES ('t', 'p', 'm', 's', 'a', 'i', 'MAYBE', 1, NULL, 'e', 0)",
    ],
)
def test_check_constraints(store: Store, db_path: Path, statement: str) -> None:
    conn = raw(db_path)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(statement)
    finally:
        conn.close()


def test_ops_log_accepts_every_action(store: Store, db_path: Path) -> None:
    conn = raw(db_path)
    try:
        for action in ("REQUEUE_DEAD", "ACK_DEAD", "REPLAY_DELIVERED", "ACK_INCIDENT"):
            conn.execute(
                "INSERT INTO ops_log (action, target_id, reason, at) VALUES (?, 'x', 'r', 0)",
                (action,),
            )
        conn.execute(
            "INSERT INTO ops_log (action, target_id, reason, at)"
            " VALUES ('RESET_STATUS', 't/p', 'r', 0)"
        )
    finally:
        conn.close()


def test_module_never_updates_or_deletes_ops_log() -> None:
    source = Path(store_module.__file__).read_text(encoding="utf-8").upper()
    assert "UPDATE OPS_LOG" not in source
    assert "DELETE FROM OPS_LOG" not in source


def test_schema_failure_rolls_back_everything(
    db_path: Path, clock: FakeClock, opened: list[Store], monkeypatch: pytest.MonkeyPatch
) -> None:
    broken = (*store_module._SCHEMA_STATEMENTS, "CREATE TABLE broken (")
    monkeypatch.setattr(store_module, "_SCHEMA_STATEMENTS", broken)
    with pytest.raises(sqlite3.OperationalError):
        Store(db_path, clock=clock)
    assert schema_snapshot_tables(db_path) == []
    monkeypatch.undo()
    # 锁已放掉，空库照常建表。
    reopened = Store(db_path, clock=clock)
    opened.append(reopened)
    assert TABLES <= {row[1] for row in schema_snapshot(db_path)[0]}


def test_empty_sqlite_file_and_zero_byte_file_get_tables(
    tmp_path: Path, clock: FakeClock, opened: list[Store]
) -> None:
    empty = tmp_path / "empty.db"
    conn = raw(empty)
    conn.execute("CREATE TABLE t (x)")
    conn.execute("DROP TABLE t")
    conn.close()
    zero = tmp_path / "zero.db"
    zero.write_bytes(b"")
    for path in (empty, zero):
        opened.append(Store(path, clock=clock))
        assert schema_snapshot(path)[1] == [("schema_version", "1")]


# --- 正常打开的报错路径 -------------------------------------------------------------


def test_missing_directory(tmp_path: Path, clock: FakeClock) -> None:
    path = tmp_path / "nope" / "outbox.db"
    with pytest.raises(StoreMissingError):
        Store(path, clock=clock)
    assert not path.parent.exists()


def test_foreign_database_is_left_alone(db_path: Path, clock: FakeClock) -> None:
    make_foreign_db(db_path)
    before = db_path.read_bytes()
    with pytest.raises(StoreForeignError) as info:
        Store(db_path, clock=clock)
    assert info.value.sqlite_errorname is None
    assert db_path.read_bytes() == before
    assert journal_mode(db_path) == "delete"
    assert {row[1] for row in schema_snapshot_tables(db_path)} == {"notes"}


def schema_snapshot_tables(path: Path) -> list[tuple[Any, ...]]:
    conn = raw(path)
    try:
        return conn.execute("SELECT type, name FROM sqlite_master").fetchall()
    finally:
        conn.close()


@pytest.mark.parametrize(
    "statements",
    [
        # meta 里没有 schema_version 而已有别的表
        ["CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)", "CREATE TABLE x (y)"],
        # 只有 meta 而缺表
        [
            "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
            "INSERT INTO meta VALUES ('schema_version', '1')",
        ],
    ],
)
def test_meta_without_version_or_missing_tables_is_foreign(
    db_path: Path, clock: FakeClock, statements: list[str]
) -> None:
    conn = raw(db_path)
    for statement in statements:
        conn.execute(statement)
    conn.close()
    before = db_path.read_bytes()
    for opener in (Store, Store.open_existing):
        with pytest.raises(StoreForeignError):
            opener(db_path, clock=clock)
    assert db_path.read_bytes() == before


def test_not_sqlite_is_foreign_with_errorname(db_path: Path, clock: FakeClock) -> None:
    db_path.write_bytes(b"this is not an sqlite database\n" * 64)
    before = db_path.read_bytes()
    for opener in (Store, Store.open_existing):
        with pytest.raises(StoreForeignError) as info:
            opener(db_path, clock=clock)
        assert info.value.sqlite_errorname == "SQLITE_NOTADB"
    assert db_path.read_bytes() == before


@pytest.mark.parametrize("version", ["2", "0", "abc", "-1"])
def test_version_protection(db_path: Path, clock: FakeClock, version: str) -> None:
    make_outbox(db_path, "E01")
    conn = raw(db_path)
    conn.execute("UPDATE meta SET value = ? WHERE key = 'schema_version'", (version,))
    conn.close()
    before = schema_snapshot(db_path)
    rows_before = read_rows(db_path)
    for opener in (Store, Store.open_existing):
        with pytest.raises(StoreVersionError):
            opener(db_path, clock=clock)
    assert schema_snapshot(db_path) == before
    assert read_rows(db_path) == rows_before


def test_residue_without_main_file(db_path: Path, clock: FakeClock, opened: list[Store]) -> None:
    wal = Path(f"{db_path}-wal")
    wal.write_bytes(b"old wal")
    with pytest.raises(StoreResidueError) as info:
        Store(db_path, clock=clock)
    assert info.value.files == [str(wal)]
    assert str(wal) in str(info.value)
    assert not db_path.exists()
    assert wal.read_bytes() == b"old wal"
    shm = Path(f"{db_path}-shm")
    shm.write_bytes(b"old shm")
    with pytest.raises(StoreResidueError) as info:
        Store(db_path, clock=clock)
    assert info.value.files == [str(wal), str(shm)]
    wal.unlink()
    shm.unlink()
    # 构造失败时锁已放掉。
    opened.append(Store(db_path, clock=clock))


def test_exception_hierarchy() -> None:
    for exc in (
        StoreMissingError,
        StoreForeignError,
        StoreVersionError,
        StoreLockedError,
        StoreResidueError,
        StoreReplacedError,
        StoreUnavailableError,
        StoreIntegrityFault,
    ):
        assert issubclass(exc, StoreError)
    assert not issubclass(StoreIntegrityFault, sqlite3.IntegrityError)
    assert not issubclass(EventConflictError, StoreError)


# --- 不建表的打开方式 --------------------------------------------------------------


def test_open_existing_missing_file_creates_nothing(tmp_path: Path, clock: FakeClock) -> None:
    path = tmp_path / "absent.db"
    with pytest.raises(StoreMissingError):
        Store.open_existing(path, clock=clock)
    assert sorted(tmp_path.iterdir()) == []


def test_open_existing_zero_byte_file_creates_nothing(db_path: Path, clock: FakeClock) -> None:
    db_path.write_bytes(b"")
    with pytest.raises(StoreMissingError):
        Store.open_existing(db_path, clock=clock)
    assert sorted(p.name for p in db_path.parent.iterdir()) == [db_path.name]
    assert db_path.read_bytes() == b""


def test_open_existing_foreign_database(db_path: Path, clock: FakeClock) -> None:
    make_foreign_db(db_path)
    before = db_path.read_bytes()
    with pytest.raises(StoreForeignError):
        Store.open_existing(db_path, clock=clock)
    assert db_path.read_bytes() == before
    assert journal_mode(db_path) == "delete"


def test_open_existing_empty_sqlite_is_foreign(db_path: Path, clock: FakeClock) -> None:
    conn = raw(db_path)
    conn.execute("CREATE TABLE t (x)")
    conn.execute("DROP TABLE t")
    conn.close()
    with pytest.raises(StoreForeignError):
        Store.open_existing(db_path, clock=clock)
    assert schema_snapshot_tables(db_path) == []


def test_open_existing_reads_and_keeps_journal_mode(db_path: Path, clock: FakeClock) -> None:
    make_outbox(db_path, "E01", "E02")
    conn = raw(db_path)
    assert conn.execute("PRAGMA journal_mode = DELETE").fetchone()[0] == "delete"
    conn.close()
    with Store.open_existing(db_path, clock=clock) as existing:
        assert existing.delivery_counts().pending == 2
    assert journal_mode(db_path) == "delete"


# --- 单进程锁 ---------------------------------------------------------------------


def test_second_store_in_same_process_is_locked(
    store: Store, db_path: Path, clock: FakeClock
) -> None:
    with pytest.raises(StoreLockedError):
        Store(db_path, clock=clock)
    with pytest.raises(StoreLockedError):
        Store.open_existing(db_path, clock=clock)
    assert Path(f"{db_path}.lock").read_bytes() == b""


def test_single_process_lock_across_processes(db_path: Path, clock: FakeClock) -> None:
    parent = Store(db_path, clock=clock)
    try:
        parent.insert_event("E01", payload("E01"))
        assert run_child(db_path, existing=False) == 3
        assert run_child(db_path, existing=True) == 3
    finally:
        parent.close()
    parent.close()  # 可重复调用
    assert run_child(db_path, existing=False) == 0
    assert run_child(db_path, existing=True) == 0
    assert Path(f"{db_path}.lock").exists()


def test_close_then_reopen_reacquires_lock(store: Store, db_path: Path, clock: FakeClock) -> None:
    store.insert_event("E01", payload("E01"))
    store.close()
    with pytest.raises(StoreUnavailableError):
        store.delivery_counts()
    other = Store(db_path, clock=clock)
    other.close()
    store.reopen()
    assert store.delivery_counts().pending == 1
    with pytest.raises(StoreLockedError):
        Store(db_path, clock=clock)


# --- 写入 ------------------------------------------------------------------------


def test_insert_is_durable(db_path: Path, clock: FakeClock) -> None:
    with Store(db_path, clock=clock) as first:
        assert first.insert_event("E01", payload("E01")) == "E01"
    with Store(db_path, clock=clock) as second:
        assert second.delivery_counts().pending == 1
    row = read_rows(db_path)["E01"]
    assert row["status"] == "PENDING"
    assert row["attempts"] == 0
    assert row["next_attempt_at"] == T0
    assert row["created_at"] == T0
    assert row["payload"] == payload("E01")
    assert row["last_error_code"] is None


def test_local_dedupe_and_conflict(store: Store, db_path: Path, clock: FakeClock) -> None:
    assert store.insert_event("E01", payload("E01")) == "E01"
    clock.now += 5
    assert store.insert_event("E01", payload("E01")) == "E01"
    before = read_rows(db_path)
    with pytest.raises(EventConflictError) as info:
        store.insert_event("E01", payload("E01", "b"))
    assert info.value.event_id == "E01"
    assert read_rows(db_path) == before
    assert len(before) == 1


def _concurrent_insert(
    store: Store, workers: Callable[[], Worker], first: bytes, second: bytes
) -> list[tuple[bool, Any]]:
    barrier = threading.Barrier(2)

    def insert(data: bytes) -> Callable[[], str]:
        def run() -> str:
            barrier.wait(timeout=30)
            return store.insert_event("E01", data)

        return run

    boxes = [workers().submit(insert(first)), workers().submit(insert(second))]
    return [box.get(timeout=30) for box in boxes]


def test_concurrent_insert_same_event_id_same_payload(
    store: Store, db_path: Path, workers: Callable[[], Worker]
) -> None:
    results = _concurrent_insert(store, workers, payload("E01"), payload("E01"))
    assert results == [(True, "E01"), (True, "E01")]
    assert list(read_rows(db_path)) == ["E01"]


def test_concurrent_insert_same_event_id_different_payload(
    store: Store, db_path: Path, workers: Callable[[], Worker]
) -> None:
    results = _concurrent_insert(store, workers, payload("E01", "a"), payload("E01", "b"))
    succeeded = [value for ok, value in results if ok]
    failed = [value for ok, value in results if not ok]
    assert succeeded == ["E01"]
    assert len(failed) == 1 and isinstance(failed[0], EventConflictError)
    rows = read_rows(db_path)
    assert list(rows) == ["E01"]
    winner = results.index((True, "E01"))
    assert rows["E01"]["payload"] == payload("E01", "ab"[winner])


def test_primary_key_collision_is_not_a_storage_fault(
    store: Store, db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store.insert_event("E01", payload("E01"))
    monkeypatch.setattr(Store, "_find_existing_payload", lambda self, conn, event_id: None)
    assert store.insert_event("E01", payload("E01")) == "E01"
    before = read_rows(db_path)
    with pytest.raises(EventConflictError):
        store.insert_event("E01", payload("E01", "b"))
    assert read_rows(db_path) == before


def test_constraint_violation_without_row_is_integrity_fault(store: Store, db_path: Path) -> None:
    with pytest.raises(StoreIntegrityFault) as info:
        store.insert_event("E01", None)  # type: ignore[arg-type]
    assert isinstance(info.value.__cause__, sqlite3.IntegrityError)
    assert read_rows(db_path) == {}


def test_other_sqlite_errors_propagate_unchanged(store: Store, db_path: Path) -> None:
    conn = raw(db_path)
    conn.execute("DROP TABLE usage_outbox")
    conn.close()
    with pytest.raises(sqlite3.OperationalError):
        store.insert_event("E01", payload("E01"))


# --- 领取 ------------------------------------------------------------------------


def test_claim_order_by_created_at_then_event_id(store: Store, clock: FakeClock) -> None:
    clock.now = T0 - 10
    store.insert_event("Z00", payload("Z00"))
    clock.now = T0
    for event_id in ("C", "A", "B"):
        store.insert_event(event_id, payload(event_id))
    assert [e.event_id for e in claim_all(store)] == ["Z00", "A", "B", "C"]


def test_claim_returns_lease_and_incremented_attempts(
    store: Store, db_path: Path, clock: FakeClock
) -> None:
    store.insert_event("E01", payload("E01"))
    (event,) = claim_all(store)
    assert event.event_id == "E01"
    assert event.payload == payload("E01")
    assert event.attempts == 1
    assert event.last_error_code is None
    assert event.lease_until == T0 + LEASE
    row = read_rows(db_path)["E01"]
    assert (row["status"], row["attempts"], row["lease_until"]) == ("SENDING", 1, T0 + LEASE)


def test_claim_count_and_byte_limits(store: Store) -> None:
    ids = ["E01", "E02", "E03"]
    for event_id in ids:
        store.insert_event(event_id, b"x" * 10)
    overhead = len(b'{"events":[') + len(b"]}")
    two = overhead + 10 + 1 + 10
    assert [e.event_id for e in store.claim(max_events=2, max_bytes=10**6, lease_seconds=1)] == [
        "E01",
        "E02",
    ]
    store.mark_delivered([Fence("E01", T0 + 1), Fence("E02", T0 + 1)])
    store.insert_event("E04", b"x" * 10)
    claimed = store.claim(max_events=10, max_bytes=two - 1, lease_seconds=1)
    assert [e.event_id for e in claimed] == ["E03"]
    store.mark_delivered([Fence("E03", T0 + 1)])
    store.insert_event("E05", b"x" * 10)
    claimed = store.claim(max_events=10, max_bytes=two, lease_seconds=1)
    assert [e.event_id for e in claimed] == ["E04", "E05"]


def test_first_oversized_event_is_still_claimed_alone(store: Store) -> None:
    store.insert_event("E01", b"x" * 500)
    store.insert_event("E02", b"x" * 5)
    claimed = store.claim(max_events=10, max_bytes=100, lease_seconds=LEASE)
    assert [e.event_id for e in claimed] == ["E01"]


def test_claim_rejects_non_positive_limits(store: Store) -> None:
    with pytest.raises(ValueError):
        store.claim(max_events=0, max_bytes=10, lease_seconds=LEASE)


def test_claim_concurrency(store: Store, db_path: Path, workers: Callable[[], Worker]) -> None:
    ids = [f"E{i:02d}" for i in range(20)]
    for event_id in ids:
        store.insert_event(event_id, payload(event_id))
    barrier = threading.Barrier(2)

    def claim() -> list[str]:
        barrier.wait(timeout=30)
        return [e.event_id for e in claim_all(store)]

    boxes = [workers().submit(claim), workers().submit(claim)]
    results = [box.get(timeout=30) for box in boxes]
    assert all(ok for ok, _ in results)
    first, second = (set(value) for _, value in results)
    assert not first & second
    assert first | second == set(ids)
    assert {row["attempts"] for row in read_rows(db_path).values()} == {1}


def test_lease_expiry_reclaim_and_stale_receipts(
    store: Store, db_path: Path, clock: FakeClock
) -> None:
    store.insert_event("E01", payload("E01"))
    (first,) = claim_all(store)
    clock.now = first.lease_until
    assert claim_all(store) == []  # 租约恰好到点不算过期
    clock.now = first.lease_until + 1
    (second,) = claim_all(store)
    assert second.attempts == 2
    assert second.lease_until == clock.now + LEASE
    old = Fence("E01", first.lease_until)
    assert store.mark_delivered([old]) == set()
    assert store.mark_retry([RetryRow("E01", first.lease_until, T0, "X")]) == set()
    assert store.mark_dead([old], "VALIDATION_ERROR") == set()
    assert store.renew_lease(["E01"], first.lease_until, clock.now + 999) == 0
    row = read_rows(db_path)["E01"]
    assert (row["status"], row["lease_until"], row["attempts"]) == (
        "SENDING",
        second.lease_until,
        2,
    )
    assert store.mark_delivered([Fence("E01", second.lease_until)]) == {"E01"}


def test_renew_lease_fence(store: Store, db_path: Path, clock: FakeClock) -> None:
    for event_id in ("E01", "E02", "E03"):
        store.insert_event(event_id, payload(event_id))
    claimed = claim_all(store)
    lease = claimed[0].lease_until
    store.mark_delivered([Fence("E01", lease)])
    new_lease = clock.now + 30 + LEASE
    assert store.renew_lease(["E01", "E02", "E03"], lease, new_lease) == 2
    rows = read_rows(db_path)
    assert rows["E02"]["lease_until"] == new_lease
    assert rows["E03"]["lease_until"] == new_lease
    assert rows["E01"]["status"] == "DELIVERED"
    assert store.mark_delivered([Fence("E02", lease)]) == set()
    assert store.mark_delivered([Fence("E02", new_lease)]) == {"E02"}


# --- 错误码与 §4 状态机中投递相关的行 ------------------------------------------------


def test_error_code_kept_by_claim_and_cleared_by_delivery(
    store: Store, db_path: Path, clock: FakeClock
) -> None:
    store.insert_event("E01", payload("E01"))
    (event,) = claim_all(store)
    assert store.mark_retry([RetryRow("E01", event.lease_until, clock.now, "BILLING_UNAVAILABLE")])
    (again,) = claim_all(store)
    assert again.last_error_code == "BILLING_UNAVAILABLE"
    assert read_rows(db_path)["E01"]["last_error_code"] == "BILLING_UNAVAILABLE"
    assert store.mark_delivered([Fence("E01", again.lease_until)]) == {"E01"}
    row = read_rows(db_path)["E01"]
    assert row["last_error_code"] is None
    assert row["status"] == "DELIVERED"


def test_pending_is_claimed_only_when_due(store: Store, clock: FakeClock) -> None:
    store.insert_event("E01", payload("E01"))
    (event,) = claim_all(store)
    store.mark_retry([RetryRow("E01", event.lease_until, clock.now + 100, "NETWORK_ERROR")])
    clock.now += 99
    assert claim_all(store) == []
    clock.now += 1
    assert [e.event_id for e in claim_all(store)] == ["E01"]


def test_receipt_transitions_from_sending(store: Store, db_path: Path, clock: FakeClock) -> None:
    for event_id in ("E01", "E02", "E03"):
        store.insert_event(event_id, payload(event_id))
    lease = claim_all(store)[0].lease_until
    clock.now += 7
    assert store.mark_delivered([Fence("E01", lease)]) == {"E01"}
    assert store.mark_retry([RetryRow("E02", lease, clock.now + 42, "NETWORK_ERROR")]) == {"E02"}
    assert store.mark_dead([Fence("E03", lease)], "VALIDATION_ERROR") == {"E03"}
    rows = read_rows(db_path)
    assert rows["E01"]["status"] == "DELIVERED"
    assert rows["E01"]["delivered_at"] == clock.now
    assert rows["E02"]["status"] == "PENDING"
    assert rows["E02"]["next_attempt_at"] == clock.now + 42
    assert rows["E02"]["last_error_code"] == "NETWORK_ERROR"
    assert rows["E03"]["status"] == "DEAD"
    assert rows["E03"]["dead_at"] == clock.now
    assert rows["E03"]["last_error_code"] == "VALIDATION_ERROR"
    assert rows["E03"]["dead_acked_at"] is None
    for row in rows.values():
        assert row["attempts"] == 1
        assert row["payload"] == payload(row["event_id"])
    # DELIVERED 与 DEAD 不会被领取，即使时间走得很远。
    clock.now += 10 * DAY
    assert [e.event_id for e in claim_all(store)] == ["E02"]


def test_receipts_on_non_sending_rows_change_nothing(store: Store, db_path: Path) -> None:
    store.insert_event("E01", payload("E01"))
    before = read_rows(db_path)
    next_attempt = before["E01"]["next_attempt_at"]
    assert store.mark_delivered([Fence("E01", next_attempt)]) == set()
    assert store.mark_retry([RetryRow("E01", next_attempt, T0 + 5, "X")]) == set()
    assert store.mark_dead([Fence("E01", next_attempt)], "X") == set()
    assert store.mark_delivered([Fence("MISSING", T0)]) == set()
    assert read_rows(db_path) == before


def test_dead_again_counts_as_unacked(store: Store, db_path: Path, clock: FakeClock) -> None:
    store.insert_event("E01", payload("E01"))
    (event,) = claim_all(store)
    store.mark_dead([Fence("E01", event.lease_until)], "VALIDATION_ERROR")
    # 模拟确认后又被放回 PENDING（属于 AIH-TASK-166 的路径）。
    conn = raw(db_path)
    conn.execute(
        "UPDATE usage_outbox SET status = 'PENDING', dead_acked_at = 1, next_attempt_at = ?"
        " WHERE event_id = 'E01'",
        (clock.now,),
    )
    conn.close()
    (again,) = claim_all(store)
    assert store.mark_dead([Fence("E01", again.lease_until)], "VALIDATION_ERROR") == {"E01"}
    assert read_rows(db_path)["E01"]["dead_acked_at"] is None
    assert store.delivery_counts().dead_unacked == 1


def test_mixed_batch_reports_only_fenced_rows(store: Store, clock: FakeClock) -> None:
    for event_id in ("E01", "E02"):
        store.insert_event(event_id, payload(event_id))
    lease = claim_all(store)[0].lease_until
    result = store.mark_delivered([Fence("E01", lease), Fence("E02", lease + 1)])
    assert result == {"E01"}


def test_receipt_transaction_failure_leaves_rows_sending(
    store: Store, db_path: Path, clock: FakeClock
) -> None:
    for event_id in ("E01", "E02"):
        store.insert_event(event_id, payload(event_id))
    lease = claim_all(store)[0].lease_until
    conn = raw(db_path)
    conn.execute(
        "CREATE TRIGGER fail_e02 BEFORE UPDATE ON usage_outbox"
        " WHEN NEW.event_id = 'E02' AND NEW.status != 'SENDING'"
        " BEGIN SELECT RAISE(ABORT, 'injected'); END"
    )
    conn.close()
    fences = [Fence("E01", lease), Fence("E02", lease)]
    with pytest.raises(sqlite3.Error):
        store.mark_delivered(fences)
    with pytest.raises(sqlite3.Error):
        store.mark_dead(fences, "X")

    def interrupted() -> Iterator[RetryRow]:
        yield RetryRow("E01", lease, T0, "X")
        raise RuntimeError("receipt handling failed")

    with pytest.raises(RuntimeError):
        store.mark_retry(interrupted())
    rows = read_rows(db_path)
    for row in rows.values():
        assert (row["status"], row["lease_until"], row["last_error_code"]) == (
            "SENDING",
            lease,
            None,
        )
    # 租约到期后被重领。
    clock.now = lease + 1
    assert {e.event_id: e.attempts for e in claim_all(store)} == {"E01": 2, "E02": 2}


# --- 清理与 meta ------------------------------------------------------------------


def _delivered_8_and_6_days_ago_plus_dead(store: Store, clock: FakeClock) -> None:
    for event_id in ("OLD", "NEW", "DEAD"):
        store.insert_event(event_id, payload(event_id))
    lease = claim_all(store)[0].lease_until
    store.mark_delivered([Fence("OLD", lease)])
    store.mark_dead([Fence("DEAD", lease)], "VALIDATION_ERROR")
    clock.now = T0 + 2 * DAY
    store.mark_delivered([Fence("NEW", lease)])
    clock.now = T0 + 8 * DAY


def test_purge_default_retention(store: Store, db_path: Path, clock: FakeClock) -> None:
    _delivered_8_and_6_days_ago_plus_dead(store, clock)
    assert store.purge_delivered(clock.now - 7 * DAY) == 1
    assert sorted(read_rows(db_path)) == ["DEAD", "NEW"]


def test_purge_two_day_retention(store: Store, db_path: Path, clock: FakeClock) -> None:
    _delivered_8_and_6_days_ago_plus_dead(store, clock)
    assert store.purge_delivered(clock.now - 2 * DAY) == 2
    assert sorted(read_rows(db_path)) == ["DEAD"]


def test_purge_limit(store: Store, db_path: Path, clock: FakeClock) -> None:
    _delivered_8_and_6_days_ago_plus_dead(store, clock)
    assert store.purge_delivered(clock.now, limit=1) == 1
    assert sorted(read_rows(db_path)) == ["DEAD", "NEW"]


def test_last_delivery_ok_at_in_meta(store: Store, db_path: Path, clock: FakeClock) -> None:
    assert store.read_last_delivery_ok_at() is None
    assert store.record_delivery_ok() == T0
    clock.now = T0 + 12.5
    store.record_delivery_ok()
    assert store.read_last_delivery_ok_at() == T0 + 12.5
    assert dict(schema_snapshot(db_path)[1])["last_delivery_ok_at"] == repr(T0 + 12.5)


def test_delivery_counts(store: Store, db_path: Path, clock: FakeClock) -> None:
    empty = store.delivery_counts()
    assert (empty.pending, empty.oldest_unsent_created_at, empty.auth_failing) == (0, None, False)
    clock.now = T0 - 50
    store.insert_event("SEND", payload("SEND"))
    clock.now = T0 - 40
    store.insert_event("DEL", payload("DEL"))
    store.insert_event("DEAD1", payload("DEAD1"))
    store.insert_event("DEAD2", payload("DEAD2"))
    lease = claim_all(store)[0].lease_until
    store.mark_delivered([Fence("DEL", lease)])
    store.mark_dead([Fence("DEAD1", lease)], "VALIDATION_ERROR")
    store.mark_dead([Fence("DEAD2", lease)], "INTEGRATION_AUTH_FAILED")
    clock.now = T0
    store.insert_event("PEND", payload("PEND"))
    conn = raw(db_path)
    conn.execute("UPDATE usage_outbox SET dead_acked_at = 1 WHERE event_id = 'DEAD1'")
    conn.close()
    counts = store.delivery_counts()
    assert (counts.pending, counts.sending, counts.delivered, counts.dead) == (1, 1, 1, 2)
    assert counts.dead_unacked == 1
    # SENDING 的租约过期也照算。
    assert counts.oldest_unsent_created_at == T0 - 50
    # 只有 DEAD 行带凭据错误码时不算。
    assert counts.auth_failing is False
    clock.now = lease + 1
    (sending,) = claim_all(store)[:1]
    store.mark_retry(
        [RetryRow(sending.event_id, sending.lease_until, T0, "INTEGRATION_AUTH_FAILED")]
    )
    assert store.delivery_counts().auth_failing is True


# --- 连接登记、作废与文件替换 -------------------------------------------------------


def test_invalidate_closes_everything_until_reopen(
    store: Store, db_path: Path, workers: Callable[[], Worker]
) -> None:
    first, second = workers(), workers()
    first.call(lambda: store.insert_event("E01", payload("E01")))
    second.call(lambda: store.insert_event("E02", payload("E02")))
    old = list(store._connections.values())
    assert len(old) == 3
    store.invalidate()
    assert store._connections == {}
    assert not store.is_open
    for conn in old:
        with pytest.raises(sqlite3.ProgrammingError):
            conn.execute("SELECT 1")
    with pytest.raises(StoreUnavailableError):
        first.call(lambda: store.insert_event("E03", payload("E03")))
    with pytest.raises(StoreUnavailableError):
        store.delivery_counts()
    store.reopen()
    first.call(lambda: store.insert_event("E03", payload("E03")))
    second.call(lambda: store.insert_event("E04", payload("E04")))
    assert sorted(read_rows(db_path)) == ["E01", "E02", "E03", "E04"]
    assert os.path.exists(f"{db_path}-wal")
    assert integrity(db_path) == "ok"


@posix_only
def test_idle_threads_do_not_write_old_file(
    store: Store, db_path: Path, tmp_path: Path, clock: FakeClock, workers: Callable[[], Worker]
) -> None:
    first, second = workers(), workers()
    first.call(lambda: store.insert_event("E01", payload("E01")))
    second.call(lambda: store.insert_event("E02", payload("E02")))
    quarantine = tmp_path / "quarantine.db"
    for suffix in ("", "-wal", "-shm"):
        os.rename(f"{db_path}{suffix}", f"{quarantine}{suffix}")
    with pytest.raises(StoreReplacedError):
        first.call(lambda: store.insert_event("E03", payload("E03")))
    store.invalidate()
    assert not os.path.exists(f"{db_path}-wal")
    store.reopen()
    assert os.path.exists(f"{db_path}-wal")
    first.call(lambda: store.insert_event("E03", payload("E03")))
    second.call(lambda: store.insert_event("E04", payload("E04")))
    assert os.path.exists(f"{db_path}-wal")
    assert integrity(db_path) == "ok"
    assert sorted(read_rows(db_path)) == ["E03", "E04"]
    with Store.open_existing(quarantine, clock=clock) as old:
        assert old.delivery_counts().pending == 2


@posix_only
def test_replaced_or_removed_file_raises(
    store: Store,
    db_path: Path,
    tmp_path: Path,
    clock: FakeClock,
    workers: Callable[[], Worker],
) -> None:
    store.insert_event("E01", payload("E01"))
    other = tmp_path / "other.db"
    make_outbox(other)
    os.replace(other, db_path)
    with pytest.raises(StoreReplacedError):
        store.insert_event("E02", payload("E02"))
    # 新线程也不能在换过的文件上建连接。
    with pytest.raises(StoreReplacedError):
        workers().call(lambda: store.insert_event("E02", payload("E02")))
    os.unlink(db_path)
    with pytest.raises(StoreReplacedError):
        store.delivery_counts()
    store.invalidate()
    assert not db_path.exists()


@posix_only
def test_only_main_file_moved(
    store: Store, db_path: Path, tmp_path: Path, clock: FakeClock
) -> None:
    for event_id in ("E01", "E02", "E03"):
        store.insert_event(event_id, payload(event_id))
    wal = Path(f"{db_path}-wal")
    assert wal.stat().st_size > 0
    quarantine = tmp_path / "quarantine.db"
    os.rename(db_path, quarantine)
    with pytest.raises(StoreReplacedError):
        store.insert_event("E04", payload("E04"))
    store.invalidate()
    assert wal.exists()
    with pytest.raises(StoreResidueError) as info:
        store.reopen()
    assert str(wal) in info.value.files
    assert all(os.path.exists(name) for name in info.value.files)
    assert not db_path.exists()
    for suffix in ("-wal", "-shm"):
        if os.path.exists(f"{db_path}{suffix}"):
            os.rename(f"{db_path}{suffix}", f"{quarantine}{suffix}")
    store.reopen()
    assert store.delivery_counts().pending == 0
    with Store.open_existing(quarantine, clock=clock) as old:
        assert old.delivery_counts().pending == 3
    assert integrity(quarantine) == "ok"


@posix_only
def test_all_three_files_moved(
    store: Store, db_path: Path, tmp_path: Path, clock: FakeClock
) -> None:
    for event_id in ("E01", "E02", "E03"):
        store.insert_event(event_id, payload(event_id))
    quarantine = tmp_path / "quarantine.db"
    for suffix in ("", "-wal", "-shm"):
        os.rename(f"{db_path}{suffix}", f"{quarantine}{suffix}")
    with pytest.raises(StoreReplacedError):
        store.insert_event("E04", payload("E04"))
    store.invalidate()
    store.reopen()
    store.insert_event("E04", payload("E04"))
    assert sorted(read_rows(db_path)) == ["E04"]
    with Store.open_existing(quarantine, clock=clock) as old:
        assert old.delivery_counts().pending == 3
    assert integrity(quarantine) == "ok"

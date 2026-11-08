"""BillingClient delivery thread, deliver_once, restart and file handling (design gate #571 v24).

覆盖 §7 标 168 的投递与运行各行：后台线程不死、stop 在超时内返回、重启后继续送、
降级后只靠 deliver_once() 恢复、空闲线程不写旧文件、单进程锁、只移走主库、三个文件一起
移走，以及 deliver_once 与后台线程同时回灌时一个冲突只计一次。

SQLite 用 tmp_path 下的文件，HTTP 用注入的假 Transport，时间用注入的时钟；后台线程把
投递间隔配成 0.01 秒，用 threading.Event 同步，不 sleep。在打开状态下移动文件的用例依赖
POSIX 语义，在 win32 上跳过（生产与 CI 都是 Linux）。
"""

from __future__ import annotations

import json
import logging
import multiprocessing
import os
import queue
import random
import sqlite3
import sys
import threading
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest
from acuven_billing_client import (
    BillingClient,
    BillingClientConfig,
    DeliverySummary,
    EventConflictError,
    Response,
    Store,
    TransportError,
    build_llm_event,
    new_ulid,
)
from acuven_billing_client.client import THREAD_NAME
from acuven_billing_client.delivery import BATCH_PATH

T0 = 1_790_000_000.0
LOGGER = "acuven_billing_client"
BASE_URL = "https://billing.example.com"
SECRET = "sk_" + "0" * 64
OCCURRED = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
FAST = 0.01

posix_only = pytest.mark.skipif(
    sys.platform == "win32", reason="moving an open file needs POSIX semantics"
)


# --- 替身 -------------------------------------------------------------------------------


class FakeClock:
    def __init__(self, now: float = T0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


Handler = Callable[[str, bytes], Response]


class FakeTransport:
    def __init__(self, handler: Handler) -> None:
        self.handler = handler
        self.sent: list[bytes] = []

    def send(self, method: str, url: str, headers: Any, body: bytes, timeout: float) -> Response:
        self.sent.append(body)
        return self.handler(url, body)


def accept_all(url: str, body: bytes) -> Response:
    document = json.loads(body)
    if urlsplit(url).path.endswith(BATCH_PATH):
        results = [
            {
                "index": index,
                "event_id": event["event_id"],
                "status": "accepted",
                "processing_status": "RECEIVED",
                "error_code": None,
                "retryable": False,
            }
            for index, event in enumerate(document["events"])
        ]
        return Response(200, json.dumps({"data": {"results": results}}).encode())
    data = {"event_id": document["event_id"], "status": "accepted"}
    return Response(202, json.dumps({"data": data}).encode())


def network_down(url: str, body: bytes) -> Response:
    raise TransportError("network down")


def sqlite_error(name: str, cls: type[sqlite3.Error] = sqlite3.OperationalError) -> sqlite3.Error:
    error = cls(f"simulated {name}")
    error.sqlite_errorname = name  # type: ignore[attr-defined]
    return error


class Fault:
    """Make one `Store` method raise while active."""

    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        method: str,
        make_error: Callable[[], BaseException],
        *,
        when: Callable[[], bool] | None = None,
    ) -> None:
        self.active = True
        original = getattr(Store, method)
        fault = self

        def replacement(store: Store, *args: Any, **kwargs: Any) -> Any:
            if fault.active and (when is None or when()):
                raise make_error()
            return original(store, *args, **kwargs)

        monkeypatch.setattr(Store, method, replacement)


class ContendedLock:
    """A lock that reports when a second caller has to wait for it."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.contended = threading.Event()

    def __enter__(self) -> ContendedLock:
        if not self._lock.acquire(blocking=False):
            self.contended.set()
            self._lock.acquire()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self._lock.release()


class Worker:
    """A long-lived named thread, so its store connection survives between calls."""

    def __init__(self, name: str) -> None:
        self._tasks: queue.Queue[Any] = queue.Queue()
        self._thread = threading.Thread(target=self._run, name=name, daemon=True)
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

    def call(self, fn: Callable[[], Any]) -> Any:
        box: queue.Queue[Any] = queue.Queue()
        self._tasks.put((fn, box))
        ok, value = box.get(timeout=30)
        if not ok:
            raise value
        return value

    def stop(self) -> None:
        self._tasks.put(None)
        self._thread.join(timeout=30)


class ListHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


# --- 事件与库 ---------------------------------------------------------------------------


def eid(n: int) -> str:
    return f"01J{n:023d}"


def llm_fields() -> dict[str, Any]:
    return {
        "provider": "openai",
        "model": "gpt-4o",
        "usage_type": "llm.chat",
        "input_tokens": 10,
        "output_tokens": 5,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "request_id": "req-1",
    }


def llm(client: BillingClient) -> str:
    return client.record_llm_usage(**llm_fields())


def own(client: BillingClient, n: int, variant: int = 0) -> str:
    return client.record_llm_usage(
        **llm_fields(), event_id=eid(n), occurred_at=OCCURRED + timedelta(seconds=variant)
    )


def own_payload(n: int, variant: int = 0) -> bytes:
    _, payload = build_llm_event(
        **llm_fields(),
        now=OCCURRED,
        new_id=new_ulid,
        event_id=eid(n),
        occurred_at=OCCURRED + timedelta(seconds=variant),
    )
    return payload


def raw(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    return conn


def rows(path: Path) -> dict[str, dict[str, Any]]:
    conn = raw(path)
    try:
        return {r["event_id"]: dict(r) for r in conn.execute("SELECT * FROM usage_outbox")}
    finally:
        conn.close()


def incident_rows(path: Path) -> list[dict[str, Any]]:
    conn = raw(path)
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM incidents")]
    finally:
        conn.close()


def integrity(path: Path) -> str:
    conn = raw(path)
    try:
        return conn.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        conn.close()


def insert_raw(path: Path, event_id: str, payload: bytes) -> None:
    conn = raw(path)
    try:
        conn.execute(
            "INSERT INTO usage_outbox"
            " (event_id, payload, status, attempts, next_attempt_at, created_at)"
            " VALUES (?, ?, 'PENDING', 0, ?, ?)",
            (event_id, payload, T0, T0),
        )
    finally:
        conn.close()


def side(path: Path, suffix: str) -> Path:
    return Path(str(path) + suffix)


def move_set(source: Path, target: Path) -> None:
    for suffix in ("", "-wal", "-shm"):
        if os.path.lexists(side(source, suffix)):
            os.replace(side(source, suffix), side(target, suffix))


def make_config(path: Path, **overrides: Any) -> BillingClientConfig:
    values: dict[str, Any] = {
        "base_url": BASE_URL,
        "api_key": "ak_test",
        "key_version": 3,
        "secret": SECRET,
        "tenant_id": "ten_a",
        "project_id": "prj_a",
        "outbox_path": str(path),
    }
    values.update(overrides)
    return BillingClientConfig(**values)


def delivery_threads() -> int:
    return sum(1 for thread in threading.enumerate() if thread.name == THREAD_NAME)


# --- 夹具 -------------------------------------------------------------------------------


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "outbox.db"


@pytest.fixture
def make_client(clock: FakeClock) -> Iterator[Callable[..., BillingClient]]:
    made: list[BillingClient] = []

    def make(path: Path, *, transport: Any = None, **overrides: Any) -> BillingClient:
        client = BillingClient(
            make_config(path, **overrides),
            transport=transport if transport is not None else FakeTransport(network_down),
            clock=clock,
            rng=random.Random(0),
        )
        made.append(client)
        return client

    yield make
    for client in made:
        client.close(timeout=2)


@pytest.fixture
def workers() -> Iterator[Callable[[str], Worker]]:
    started: list[Worker] = []

    def make(name: str) -> Worker:
        worker = Worker(name)
        started.append(worker)
        return worker

    yield make
    for worker in started:
        worker.stop()


@pytest.fixture
def logs(caplog: pytest.LogCaptureFixture) -> pytest.LogCaptureFixture:
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    return caplog


# --- 后台线程 ---------------------------------------------------------------------------


def test_background_thread_survives_a_failing_round(
    make_client: Callable[..., BillingClient],
    db_path: Path,
    clock: FakeClock,
    logs: pytest.LogCaptureFixture,
) -> None:
    calls: list[str] = []
    delivered = threading.Event()

    def handler(url: str, body: bytes) -> Response:
        calls.append(url)
        if len(calls) == 1:
            # 让这批的租约在下一轮之前到期，下一轮重领。
            clock.now += 120
            raise RuntimeError("transport bug")
        delivered.set()
        return accept_all(url, body)

    client = make_client(db_path, transport=FakeTransport(handler), delivery_interval_seconds=FAST)
    event = llm(client)
    client.start()

    assert delivered.wait(2)
    assert client.stop(timeout=2) is True

    assert rows(db_path)[event]["status"] == "DELIVERED"
    assert "delivery round failed: RuntimeError" in logs.text
    assert delivery_threads() == 0


def test_stop_times_out_then_restart(
    make_client: Callable[..., BillingClient], db_path: Path, logs: pytest.LogCaptureFixture
) -> None:
    entered, release = threading.Event(), threading.Event()

    def handler(url: str, body: bytes) -> Response:
        entered.set()
        release.wait(5)
        return accept_all(url, body)

    client = make_client(db_path, transport=FakeTransport(handler), delivery_interval_seconds=FAST)
    llm(client)
    client.start()
    assert entered.wait(2)

    assert client.stop(timeout=0.05) is False
    assert "did not stop" in logs.text
    with pytest.raises(RuntimeError):
        client.start()

    release.set()
    assert client.stop(timeout=2) is True

    # stop 成功之后可以再 start；新线程的第一轮立即执行。
    entered.clear()
    llm(client)
    client.start()
    assert entered.wait(2)
    assert client.stop(timeout=2) is True
    assert delivery_threads() == 0


def test_start_twice_runs_one_thread(
    make_client: Callable[..., BillingClient], db_path: Path
) -> None:
    client = make_client(db_path, delivery_interval_seconds=FAST)
    before = delivery_threads()

    client.start()
    client.start()

    assert delivery_threads() == before + 1
    assert client.stop(timeout=2) is True
    assert delivery_threads() == before


def test_stop_without_start_returns_true(
    make_client: Callable[..., BillingClient], db_path: Path
) -> None:
    client = make_client(db_path)
    assert client.stop(timeout=0.05) is True


# --- deliver_once 与重启 ----------------------------------------------------------------


def test_restart_continues_delivery(
    make_client: Callable[..., BillingClient], db_path: Path
) -> None:
    first = make_client(db_path, transport=FakeTransport(network_down))
    event = llm(first)
    first.close()

    second = make_client(db_path, transport=FakeTransport(accept_all))
    summary = second.deliver_once()

    assert summary.delivered == 1
    assert rows(db_path)[event]["status"] == "DELIVERED"


def test_deliver_once_alone_recovers_from_degraded(
    make_client: Callable[..., BillingClient], tmp_path: Path
) -> None:
    path = tmp_path / "later" / "outbox.db"
    client = make_client(path, transport=FakeTransport(accept_all))
    assert not client.store_available
    event = llm(client)
    assert client.deliver_once() == DeliverySummary.zero()

    path.parent.mkdir()
    summary = client.deliver_once()

    # 一次 deliver_once：先重新打开、再回灌、再投递。
    assert client.store_available
    assert summary.delivered == 1
    assert rows(path)[event]["status"] == "DELIVERED"
    assert client.held_in_memory == 0


@pytest.mark.parametrize(
    ("name", "degrades"),
    [("SQLITE_IOERR", True), ("SQLITE_BUSY", False), ("SQLITE_LOCKED_SHAREDCACHE", False)],
)
def test_store_error_mid_round_returns_zero(
    name: str,
    degrades: bool,
    make_client: Callable[..., BillingClient],
    db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = make_client(db_path, transport=FakeTransport(accept_all))
    event = llm(client)
    Fault(monkeypatch, "mark_delivered", lambda: sqlite_error(name))

    assert client.deliver_once() == DeliverySummary.zero()

    assert client.store_available is (not degrades)
    # 已领取的行停在 SENDING，以库为准。
    assert rows(db_path)[event]["status"] == "SENDING"


# --- 关闭 -------------------------------------------------------------------------------


def test_closed_client_rejects_calls_and_releases_the_lock(
    make_client: Callable[..., BillingClient], db_path: Path, clock: FakeClock
) -> None:
    client = make_client(db_path)
    client.close()

    start = datetime.fromtimestamp(T0 - 1, UTC)
    end = datetime.fromtimestamp(T0, UTC)
    calls: list[Callable[[], object]] = [
        lambda: llm(client),
        lambda: client.record_quantity_usage(
            provider="openai",
            model="whisper-1",
            usage_type="audio.transcription",
            quantity="1",
            unit="minute",
            request_id="req-2",
        ),
        client.start,
        client.deliver_once,
        lambda: client.requeue_dead(eid(1), reason="r"),
        lambda: client.ack_dead(eid(1), reason="r"),
        lambda: client.list_dead(),
        lambda: client.replay_delivered(delivered_from=start, delivered_until=end, reason="r"),
        lambda: client.list_incidents(),
        lambda: client.ack_incident("inc_x", reason="r"),
    ]
    for call in calls:
        with pytest.raises(RuntimeError):
            call()
    assert not client.store_available

    # 单进程锁已释放。
    Store(db_path, clock=clock).close()


def test_leftover_thread_after_close_does_not_reopen(
    make_client: Callable[..., BillingClient], db_path: Path, clock: FakeClock
) -> None:
    entered, release = threading.Event(), threading.Event()

    def handler(url: str, body: bytes) -> Response:
        entered.set()
        release.wait(5)
        return accept_all(url, body)

    client = make_client(db_path, transport=FakeTransport(handler), delivery_interval_seconds=FAST)
    event = llm(client)
    client.start()
    assert entered.wait(2)
    thread = client._thread
    assert thread is not None

    assert client.close(timeout=0.05) is False
    release.set()
    thread.join(2)
    assert not thread.is_alive()

    # 残留的线程落回执时撞上已关闭的库：不重开、不取回单进程锁。
    assert not client.store_available
    Store(db_path, clock=clock).close()
    assert rows(db_path)[event]["status"] == "SENDING"


# --- 同时回灌 ---------------------------------------------------------------------------


def test_concurrent_refill_counts_one_conflict_once(
    make_client: Callable[..., BillingClient], db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = make_client(
        db_path, transport=FakeTransport(accept_all), delivery_interval_seconds=FAST
    )
    busy = Fault(monkeypatch, "insert_event", lambda: sqlite_error("SQLITE_BUSY"))
    own(client, 1, 1)
    assert client.held_in_memory == 1
    busy.active = False
    insert_raw(db_path, eid(1), own_payload(1, 0))

    entered, release = threading.Event(), threading.Event()
    original = Store.insert_event

    def blocking(store: Store, event_id: str, payload: bytes) -> str:
        entered.set()
        release.wait(5)
        return original(store, event_id, payload)

    monkeypatch.setattr(Store, "insert_event", blocking)
    lock = ContendedLock()
    client._maintenance_lock = lock  # type: ignore[assignment]

    client.start()
    assert entered.wait(2)
    outcome: dict[str, Any] = {}
    other = threading.Thread(
        target=lambda: outcome.setdefault("summary", client.deliver_once()), daemon=True
    )
    other.start()
    # deliver_once 在等后台线程放开回灌的锁。
    assert lock.contended.wait(2)
    release.set()
    other.join(2)
    assert not other.is_alive()
    assert client.stop(timeout=2) is True

    assert "summary" in outcome
    assert client.conflicts_dropped == 1
    assert client.held_in_memory == 0
    assert client.incidents_pending == 0
    assert [(r["kind"], r["event_id"]) for r in incident_rows(db_path)] == [
        ("CONFLICT_DROPPED", eid(1))
    ]
    with pytest.raises(EventConflictError):
        own(client, 1, 1)


# --- 空闲线程不写旧文件 -----------------------------------------------------------------


def test_idle_threads_do_not_write_the_old_file(
    make_client: Callable[..., BillingClient],
    db_path: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    workers: Callable[[str], Worker],
) -> None:
    client = make_client(db_path)
    first, second, third = workers("idle-1"), workers("idle-2"), workers("trigger")
    old_events = {first.call(lambda: llm(client)), second.call(lambda: llm(client))}
    Fault(
        monkeypatch,
        "insert_event",
        lambda: sqlite_error("SQLITE_CORRUPT", sqlite3.DatabaseError),
        when=lambda: threading.current_thread().name == "trigger",
    )

    held = third.call(lambda: llm(client))

    assert not client.store_available
    assert client.held_in_memory == 1
    # 旧连接全部关闭，-wal 已随最后一个连接检查点并删除。
    assert not side(db_path, "-wal").exists()

    quarantine = tmp_path / "quarantine"
    quarantine.mkdir()
    old_path = quarantine / "outbox.db"
    move_set(db_path, old_path)
    client.deliver_once()

    assert client.store_available
    assert side(db_path, "-wal").exists()
    new_events = {first.call(lambda: llm(client)), second.call(lambda: llm(client))}

    assert set(rows(old_path)) == old_events
    assert set(rows(db_path)) == new_events | {held}
    assert integrity(db_path) == "ok"
    assert side(db_path, "-wal").exists()


# --- 单进程锁 ---------------------------------------------------------------------------


def _child_client(path: str, ready: Any, release: Any, results: Any) -> None:
    handler = ListHandler()
    package_logger = logging.getLogger(LOGGER)
    package_logger.addHandler(handler)
    package_logger.setLevel(logging.INFO)
    client = BillingClient(
        make_config(Path(path)),
        transport=FakeTransport(accept_all),
        clock=FakeClock(),
        rng=random.Random(0),
    )
    first_available = client.store_available
    logged_lock = any(
        "StoreLockedError" in message and "locked by another process" in message
        for message in handler.messages
    )
    llm(client)
    held = client.held_in_memory
    ready.set()
    release.wait(60)
    summary = client.deliver_once()
    results.put((first_available, logged_lock, held, client.store_available, summary.delivered))
    client.close(timeout=2)


def test_single_process_lock(db_path: Path, clock: FakeClock) -> None:
    holder = Store(db_path, clock=clock)
    context = multiprocessing.get_context("spawn")
    ready, release, results = context.Event(), context.Event(), context.Queue()
    process = context.Process(target=_child_client, args=(str(db_path), ready, release, results))
    process.start()
    try:
        assert ready.wait(60)
        holder.close()
        release.set()
        outcome = results.get(timeout=60)
    finally:
        release.set()
        holder.close()
        process.join(timeout=60)

    assert process.exitcode == 0
    first_available, logged_lock, held, available, delivered = outcome
    assert first_available is False
    assert logged_lock is True
    assert held == 1
    assert available is True
    assert delivered == 1


# --- 运行中移走文件（POSIX） ------------------------------------------------------------


@posix_only
def test_moving_only_the_main_file(
    make_client: Callable[..., BillingClient],
    db_path: Path,
    tmp_path: Path,
    logs: pytest.LogCaptureFixture,
) -> None:
    client = make_client(db_path)
    committed = {llm(client), llm(client)}
    assert side(db_path, "-wal").exists()
    quarantine = tmp_path / "quarantine"
    quarantine.mkdir()
    old_path = quarantine / "outbox.db"

    os.replace(db_path, old_path)
    held = llm(client)

    # 检测到替换、转入降级；原路径的 -wal 仍在，不新建库，ERROR 日志点名残留文件。
    assert not client.store_available
    assert client.held_in_memory == 1
    assert side(db_path, "-wal").exists()
    assert not db_path.exists()
    errors = "\n".join(r.getMessage() for r in logs.records if r.levelno >= logging.ERROR)
    assert "StoreResidueError" in errors
    assert str(side(db_path, "-wal")) in errors

    for suffix in ("-wal", "-shm"):
        if side(db_path, suffix).exists():
            os.replace(side(db_path, suffix), side(old_path, suffix))
    client.deliver_once()

    assert client.store_available
    assert set(rows(db_path)) == {held}
    assert set(rows(old_path)) == committed
    assert integrity(old_path) == "ok"


@posix_only
def test_moving_all_three_files(
    make_client: Callable[..., BillingClient], db_path: Path, tmp_path: Path
) -> None:
    client = make_client(db_path)
    committed = {llm(client), llm(client)}
    assert side(db_path, "-wal").exists()
    quarantine = tmp_path / "quarantine"
    quarantine.mkdir()
    old_path = quarantine / "outbox.db"

    move_set(db_path, old_path)
    held = llm(client)

    # 下一次使用转入降级并立即新建库。
    assert client.store_available
    assert db_path.exists()
    assert client.held_in_memory == 1
    client.deliver_once()

    assert client.held_in_memory == 0
    assert set(rows(db_path)) == {held}
    assert set(rows(old_path)) == committed
    assert integrity(old_path) == "ok"

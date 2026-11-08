"""BillingClient recording, hold and refill, incidents, degraded mode (design gate #571 v24).

覆盖 §7 标 168 的记账与存储故障各行：平台不可用、存储故障与暂存满、回灌时冲突、事故记录、
溢出事故、回写失败并回、构造时存储不可用、运行中转入降级、部分损坏的文件不来回切换、
运行中隔离损坏文件，以及处置函数的降级行为。

SQLite 用 tmp_path 下的文件，HTTP 用注入的假 Transport，时间用注入的时钟。存储故障用
monkeypatch 把 Store 的方法换成抛 sqlite3 错误的替身（错误名照 SQLite 的 `sqlite_errorname`），
不靠真的写满磁盘。§7「事故记录」「溢出事故」两行里对 `level` 与 `incidents_unacked` 的断言，
按本任务契约改用 Store 的未确认事故数与 `incidents_pending`（`level` 由 AIH-TASK-170 覆盖）。
"""

from __future__ import annotations

import json
import logging
import os
import random
import sqlite3
import threading
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest
from acuven_billing_client import (
    BillingClient,
    BillingClientConfig,
    DeliverySummary,
    EventConflictError,
    IncidentItem,
    OverflowCount,
    Response,
    Store,
    StoreIntegrityFault,
    StoreReplacedError,
    StoreUnavailableError,
    TransportError,
    build_llm_event,
    new_ulid,
)
from acuven_billing_client import client as client_module
from acuven_billing_client import store as store_module
from acuven_billing_client.client import _merge_pending
from acuven_billing_client.delivery import BATCH_PATH

T0 = 1_790_000_000.0
LOGGER = "acuven_billing_client"
BASE_URL = "https://billing.example.com"
SECRET = "sk_" + "0" * 64
OCCURRED = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
# 放在 request_id 里，载荷含它；日志里不得出现。
MARKER = "marker-must-not-be-logged"


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
    """An sqlite3 error carrying `sqlite_errorname`, as raised by the sqlite3 module."""
    error = cls(f"simulated {name}")
    error.sqlite_errorname = name  # type: ignore[attr-defined]
    return error


class Fault:
    """Make one `Store` method raise while active (the store stand-in of §7)."""

    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        method: str,
        make_error: Callable[[], BaseException],
        *,
        when: Callable[..., bool] | None = None,
        times: int | None = None,
    ) -> None:
        self.active = True
        self.calls = 0
        original = getattr(Store, method)
        fault = self

        def replacement(store: Store, *args: Any, **kwargs: Any) -> Any:
            if fault.active and (when is None or when(*args, **kwargs)):
                fault.calls += 1
                if times is not None and fault.calls >= times:
                    fault.active = False
                raise make_error()
            return original(store, *args, **kwargs)

        monkeypatch.setattr(Store, method, replacement)


def count_calls(monkeypatch: pytest.MonkeyPatch, method: str) -> list[int]:
    calls: list[int] = []
    original = getattr(Store, method)

    def counting(store: Store, *args: Any, **kwargs: Any) -> Any:
        calls.append(1)
        return original(store, *args, **kwargs)

    monkeypatch.setattr(Store, method, counting)
    return calls


# --- 事件与库 ---------------------------------------------------------------------------


def eid(n: int) -> str:
    """An integrator-supplied ULID."""
    return f"01J{n:023d}"


def llm_fields(request_id: str = MARKER) -> dict[str, Any]:
    return {
        "provider": "openai",
        "model": "gpt-4o",
        "usage_type": "llm.chat",
        "input_tokens": 10,
        "output_tokens": 5,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "request_id": request_id,
    }


def llm(client: BillingClient) -> str:
    return client.record_llm_usage(**llm_fields())


def own(client: BillingClient, n: int, variant: int = 0) -> str:
    """Own event_id; a different `variant` gives different payload bytes."""
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
        return [
            dict(r)
            for r in conn.execute("SELECT * FROM incidents ORDER BY first_occurred_at, kind")
        ]
    finally:
        conn.close()


def ops_actions(path: Path) -> list[str]:
    conn = raw(path)
    try:
        return [r["action"] for r in conn.execute("SELECT action FROM ops_log ORDER BY id")]
    finally:
        conn.close()


def insert_raw(path: Path, event_id: str, payload: bytes) -> None:
    """Another tool writing the same file (§2「存储」主键冲突)."""
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


def move_set(source: Path, target: Path) -> None:
    """Move the main file with its `-wal` / `-shm`, keeping the paired names."""
    for suffix in ("", "-wal", "-shm"):
        if os.path.lexists(str(source) + suffix):
            os.replace(str(source) + suffix, str(target) + suffix)


def make_valid_outbox(path: Path) -> None:
    Store(path, clock=FakeClock()).close()


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


def unacked_incidents(client: BillingClient) -> int:
    store = client._store
    assert store is not None
    return store.count_unacked_incidents()


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
def logs(caplog: pytest.LogCaptureFixture) -> pytest.LogCaptureFixture:
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    return caplog


def error_text(caplog: pytest.LogCaptureFixture) -> str:
    return "\n".join(r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR)


# --- 记账 -------------------------------------------------------------------------------


def test_platform_down_recording_still_succeeds(
    make_client: Callable[..., BillingClient], db_path: Path
) -> None:
    client = make_client(db_path, transport=FakeTransport(network_down))
    first, second = llm(client), llm(client)
    assert first != second

    summary = client.deliver_once()

    assert summary.claimed == 2
    assert summary.retried == 2
    stored = rows(db_path)
    assert set(stored) == {first, second}
    for row in stored.values():
        assert row["status"] == "PENDING"
        assert row["last_error_code"] == "NETWORK_ERROR"
    assert client.store_available
    assert client.held_in_memory == 0
    # 平台不可用之后照常记账。
    third = llm(client)
    assert third in rows(db_path)


def test_own_event_id_requires_occurred_at_and_writes_nothing(
    make_client: Callable[..., BillingClient], db_path: Path
) -> None:
    client = make_client(db_path)
    with pytest.raises(ValueError):
        client.record_llm_usage(**llm_fields(), event_id=eid(1))
    with pytest.raises(ValueError):
        client.record_quantity_usage(
            provider="openai",
            model="whisper-1",
            usage_type="audio.transcription",
            quantity=Decimal("1.5"),
            unit="minute",
            request_id="req-1",
            event_id=eid(2),
        )
    assert rows(db_path) == {}
    assert client.held_in_memory == 0


def test_same_arguments_twice_return_the_same_id(
    make_client: Callable[..., BillingClient], db_path: Path
) -> None:
    client = make_client(db_path)
    assert own(client, 1) == own(client, 1) == eid(1)
    quantity_args: dict[str, Any] = {
        "provider": "openai",
        "model": "whisper-1",
        "usage_type": "audio.transcription",
        "quantity": "37.420",
        "unit": "minute",
        "request_id": "req-2",
        "event_id": eid(2),
        "occurred_at": OCCURRED,
    }
    assert client.record_quantity_usage(**quantity_args) == eid(2)
    assert client.record_quantity_usage(**quantity_args) == eid(2)
    assert set(rows(db_path)) == {eid(1), eid(2)}
    assert client.held_in_memory == 0


def test_validation_and_conflict_errors_reach_the_caller(
    make_client: Callable[..., BillingClient], db_path: Path
) -> None:
    client = make_client(db_path)
    with pytest.raises(ValueError):
        client.record_llm_usage(**{**llm_fields(), "provider": "Not Valid"})
    with pytest.raises(TypeError):
        client.record_llm_usage(**llm_fields(), prompt="hello")  # type: ignore[call-arg]
    own(client, 1, 0)
    with pytest.raises(EventConflictError):
        own(client, 1, 1)
    assert rows(db_path)[eid(1)]["payload"] == own_payload(1, 0)
    assert client.held_in_memory == 0


# --- 存储故障与暂存 ---------------------------------------------------------------------


def test_store_failure_holds_in_memory_then_refills(
    make_client: Callable[..., BillingClient],
    db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    logs: pytest.LogCaptureFixture,
) -> None:
    client = make_client(db_path)
    fault = Fault(monkeypatch, "insert_event", lambda: sqlite_error("SQLITE_FULL"))

    event = llm(client)

    assert not client.store_available
    assert client.held_in_memory == 1
    assert rows(db_path) == {}
    assert event in error_text(logs)
    assert MARKER not in logs.text

    fault.active = False
    client.deliver_once()

    assert client.store_available
    assert client.held_in_memory == 0
    assert set(rows(db_path)) == {event}


def test_full_hold_drops_and_counts(
    make_client: Callable[..., BillingClient],
    db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    logs: pytest.LogCaptureFixture,
) -> None:
    client = make_client(db_path, held_max_events=1)
    busy = Fault(monkeypatch, "insert_event", lambda: sqlite_error("SQLITE_BUSY"))

    kept = llm(client)
    dropped = llm(client)

    assert client.held_in_memory == 1
    assert client.dropped_events == 1
    assert client.incidents_pending == 1
    assert dropped in error_text(logs)
    assert MARKER not in logs.text

    busy.active = False
    client.deliver_once()

    assert set(rows(db_path)) == {kept}
    recorded = incident_rows(db_path)
    assert [(r["kind"], r["event_id"], r["dropped_count"]) for r in recorded] == [
        ("DROPPED", dropped, 1)
    ]
    assert client.held_in_memory == 0
    assert client.incidents_pending == 0
    assert client.dropped_events == 1


def test_hold_is_checked_before_the_store(
    make_client: Callable[..., BillingClient], db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = make_client(db_path)
    busy = Fault(monkeypatch, "insert_event", lambda: sqlite_error("SQLITE_LOCKED"))

    assert own(client, 1, 0) == eid(1)
    assert busy.calls == 1
    # 同内容：直接返回，不碰库、不重复放入。
    assert own(client, 1, 0) == eid(1)
    assert busy.calls == 1
    with pytest.raises(EventConflictError):
        own(client, 1, 1)
    assert busy.calls == 1
    assert client.held_in_memory == 1


@pytest.mark.parametrize("same_content", [True, False], ids=["same", "different"])
def test_refill_conflict(
    same_content: bool,
    make_client: Callable[..., BillingClient],
    db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    logs: pytest.LogCaptureFixture,
) -> None:
    client = make_client(db_path)
    busy = Fault(monkeypatch, "insert_event", lambda: sqlite_error("SQLITE_BUSY"))
    own(client, 1, 0)
    assert client.held_in_memory == 1
    stored_payload = own_payload(1, 0 if same_content else 1)
    insert_raw(db_path, eid(1), stored_payload)

    busy.active = False
    client.deliver_once()

    assert client.held_in_memory == 0
    assert rows(db_path)[eid(1)]["payload"] == stored_payload
    if same_content:
        assert client.conflicts_dropped == 0
        assert incident_rows(db_path) == []
    else:
        assert client.conflicts_dropped == 1
        assert [(r["kind"], r["event_id"]) for r in incident_rows(db_path)] == [
            ("CONFLICT_DROPPED", eid(1))
        ]
        assert eid(1) in error_text(logs)
        assert MARKER not in logs.text
    assert client.incidents_pending == 0


def test_refill_drops_only_the_event_with_an_integrity_fault(
    make_client: Callable[..., BillingClient], db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = make_client(db_path)
    busy = Fault(monkeypatch, "insert_event", lambda: sqlite_error("SQLITE_BUSY"))
    own(client, 1)
    own(client, 2)
    busy.active = False
    Fault(
        monkeypatch,
        "insert_event",
        lambda: StoreIntegrityFault("constraint"),
        when=lambda event_id, payload: event_id == eid(1),
    )

    client.deliver_once()

    assert client.store_available
    assert client.held_in_memory == 0
    assert client.dropped_events == 1
    assert set(rows(db_path)) == {eid(2)}
    assert [(r["kind"], r["event_id"]) for r in incident_rows(db_path)] == [("DROPPED", eid(1))]


def test_record_integrity_fault_holds_without_degrading(
    make_client: Callable[..., BillingClient], db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = make_client(db_path)
    Fault(monkeypatch, "insert_event", lambda: StoreIntegrityFault("constraint"))
    store = client._store
    assert store is not None

    llm(client)

    assert client.held_in_memory == 1
    assert client.store_available
    assert store.is_open


def test_refill_stops_on_busy_but_still_delivers(
    make_client: Callable[..., BillingClient], db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = make_client(db_path, transport=FakeTransport(accept_all))
    stored = llm(client)
    busy = Fault(monkeypatch, "insert_event", lambda: sqlite_error("SQLITE_BUSY"))
    own(client, 1)
    own(client, 2)
    assert busy.calls == 2

    summary = client.deliver_once()

    # 回灌撞到 BUSY 只试一条就停；投递照常。
    assert busy.calls == 3
    assert client.held_in_memory == 2
    assert client.store_available
    assert summary.delivered == 1
    assert rows(db_path)[stored]["status"] == "DELIVERED"


# --- 事故 -------------------------------------------------------------------------------


def spy_pending_at_write(
    monkeypatch: pytest.MonkeyPatch, client_box: list[BillingClient]
) -> list[int]:
    """Record `incidents_pending` at the moment `record_incidents` is called."""
    seen: list[int] = []
    original = Store.record_incidents

    def spy(store: Store, items: Any, *, overflow: Any) -> int:
        seen.append(client_box[0].incidents_pending)
        return original(store, items, overflow=overflow)

    monkeypatch.setattr(Store, "record_incidents", spy)
    return seen


def test_incident_record_survives_restart(
    make_client: Callable[..., BillingClient], db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = make_client(db_path, held_max_events=1)
    own(client, 1, 0)
    fault = Fault(monkeypatch, "_connection", lambda: sqlite_error("SQLITE_IOERR"))
    own(client, 1, 1)
    assert not client.store_available
    dropped = [llm(client), llm(client)]
    assert client.incidents_pending == 2
    assert client.dropped_events == 2

    fault.active = False
    seen = spy_pending_at_write(monkeypatch, [client])
    client.deliver_once()

    assert seen == [3]
    assert client.incidents_pending == 0
    assert client.conflicts_dropped == 1
    recorded = incident_rows(db_path)
    assert sorted((r["kind"], r["event_id"]) for r in recorded) == sorted(
        [("DROPPED", dropped[0]), ("DROPPED", dropped[1]), ("CONFLICT_DROPPED", eid(1))]
    )
    assert all(r["dropped_count"] == 1 and r["acked_at"] is None for r in recorded)
    assert "payload" not in recorded[0]
    # 库里原行不变。
    assert rows(db_path)[eid(1)]["payload"] == own_payload(1, 0)

    client.close()
    restarted = make_client(db_path)
    assert unacked_incidents(restarted) == 3
    assert restarted.incidents_pending == 0
    incident_id = restarted.list_incidents()[0].incident_id
    assert restarted.ack_incident(incident_id, reason="reconciled by hand")
    assert unacked_incidents(restarted) == 2


def test_overflow_incident(
    make_client: Callable[..., BillingClient],
    db_path: Path,
    clock: FakeClock,
    monkeypatch: pytest.MonkeyPatch,
    logs: pytest.LogCaptureFixture,
) -> None:
    client = make_client(db_path, held_max_events=1, pending_incidents_max=2)
    fault = Fault(monkeypatch, "_connection", lambda: sqlite_error("SQLITE_IOERR"))
    held = llm(client)
    dropped: list[str] = []
    for n in range(5):
        clock.now = T0 + 10 + n
        dropped.append(llm(client))
    assert client.incidents_pending == 5
    assert client.dropped_events == 5
    assert "overflow count is now 3" in error_text(logs)

    fault.active = False
    seen = spy_pending_at_write(monkeypatch, [client])
    client.deliver_once()

    assert seen == [5]
    assert client.incidents_pending == 0
    assert held in rows(db_path)
    recorded = incident_rows(db_path)
    summary = [
        (
            r["kind"],
            r["event_id"],
            r["dropped_count"],
            r["first_occurred_at"],
            r["last_occurred_at"],
        )
        for r in recorded
    ]
    assert summary == [
        ("DROPPED", dropped[0], 1, T0 + 10, T0 + 10),
        ("DROPPED", dropped[1], 1, T0 + 11, T0 + 11),
        ("OVERFLOW_DROPPED", None, 3, T0 + 12, T0 + 14),
    ]

    for row in recorded:
        assert client.ack_incident(row["incident_id"], reason="reconciled by hand")
    assert unacked_incidents(client) == 0
    assert ops_actions(db_path) == ["ACK_INCIDENT"] * 3


def test_merge_after_failed_write() -> None:
    # 上限 3；快照 2 个 id 与溢出 2 条（t1、t2），写库失败期间又丢弃 2 条（t3、t4）。
    t1, t2, t3, t4 = 1.0, 2.0, 3.0, 4.0
    snapshot = [IncidentItem(eid(1), "DROPPED", 0.5), IncidentItem(eid(2), "DROPPED", 0.6)]
    later = [IncidentItem(eid(3), "DROPPED", t3), IncidentItem(eid(4), "DROPPED", t4)]

    items, overflow = _merge_pending(snapshot, OverflowCount(2, t1, t2), later, None, 3)

    assert items == [*snapshot, later[0]]
    assert overflow == OverflowCount(3, t1, t4)

    # 两段溢出：条数相加，首次取较早、最近取较晚。
    items, overflow = _merge_pending(
        [], OverflowCount(2, 5.0, 6.0), [], OverflowCount(4, 3.0, 4.0), 3
    )
    assert items == []
    assert overflow == OverflowCount(6, 3.0, 6.0)


def test_failed_incident_write_is_merged_back(
    make_client: Callable[..., BillingClient],
    db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    logs: pytest.LogCaptureFixture,
) -> None:
    client = make_client(db_path, held_max_events=1, pending_incidents_max=3)
    busy = Fault(monkeypatch, "insert_event", lambda: sqlite_error("SQLITE_BUSY"))
    llm(client)
    dropped = [llm(client) for _ in range(5)]
    assert client.incidents_pending == 5
    busy.active = False
    write = Fault(monkeypatch, "record_incidents", lambda: sqlite_error("SQLITE_BUSY"))

    client.deliver_once()

    assert write.calls == 1
    assert client.store_available
    assert client.incidents_pending == 5
    assert incident_rows(db_path) == []
    assert "writing pending incidents failed" in error_text(logs)

    write.active = False
    client.deliver_once()

    assert client.incidents_pending == 0
    recorded = incident_rows(db_path)
    assert sorted(r["event_id"] for r in recorded if r["kind"] == "DROPPED") == sorted(dropped[:3])
    assert [r["dropped_count"] for r in recorded if r["kind"] == "OVERFLOW_DROPPED"] == [2]


def test_incidents_added_during_the_write_are_kept(
    make_client: Callable[..., BillingClient], db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = make_client(db_path, held_max_events=1)
    busy = Fault(monkeypatch, "insert_event", lambda: sqlite_error("SQLITE_BUSY"))
    llm(client)
    first_drop = llm(client)
    assert client.incidents_pending == 1
    busy.active = False

    original = Store.record_incidents
    during: dict[str, Any] = {}

    def writing(store: Store, items: Any, *, overflow: Any) -> int:
        if not during:
            busy.active = True
            during["held"] = llm(client)
            during["dropped"] = llm(client)
            # 正在写的快照 1 条加写库期间新增的 1 条。
            during["pending"] = client.incidents_pending
        return original(store, items, overflow=overflow)

    monkeypatch.setattr(Store, "record_incidents", writing)
    client.deliver_once()

    assert during["pending"] == 2
    assert client.incidents_pending == 1
    assert [r["event_id"] for r in incident_rows(db_path)] == [first_drop]

    busy.active = False
    client.deliver_once()

    assert client.incidents_pending == 0
    assert client.held_in_memory == 0
    assert sorted(r["event_id"] for r in incident_rows(db_path)) == sorted(
        [first_drop, during["dropped"]]
    )
    assert during["held"] in rows(db_path)


# --- 构造时存储不可用 -------------------------------------------------------------------


@pytest.mark.parametrize(
    "case", ["missing_directory", "random_bytes", "newer_schema", "open_error"]
)
def test_construction_with_store_unavailable(
    case: str,
    tmp_path: Path,
    make_client: Callable[..., BillingClient],
    monkeypatch: pytest.MonkeyPatch,
    logs: pytest.LogCaptureFixture,
) -> None:
    path = tmp_path / "outbox.db"

    def replace_with_valid() -> None:
        spare = tmp_path / "spare"
        spare.mkdir()
        make_valid_outbox(spare / "outbox.db")
        os.replace(spare / "outbox.db", path)

    fix: Callable[[], object]
    if case == "missing_directory":
        path = tmp_path / "absent" / "outbox.db"
        fix = path.parent.mkdir
    elif case == "random_bytes":
        path.write_bytes(random.Random(7).randbytes(8192))
        fix = replace_with_valid
    elif case == "newer_schema":
        make_valid_outbox(path)
        conn = raw(path)
        try:
            conn.execute("UPDATE meta SET value = '2' WHERE key = 'schema_version'")
        finally:
            conn.close()
        fix = replace_with_valid
    else:
        # 打开时抛 sqlite3.OperationalError 的替身；清空集合即去掉替身。
        failing = {"open"}
        real_store = client_module.Store

        def stand_in(store_path: Any, *, clock: Any) -> Store:
            if failing:
                raise sqlite_error("SQLITE_IOERR")
            return real_store(store_path, clock=clock)

        monkeypatch.setattr(client_module, "Store", stand_in)
        fix = failing.clear

    client = make_client(path)

    assert not client.store_available
    assert "running degraded" in error_text(logs)
    event = llm(client)
    assert client.held_in_memory == 1
    with pytest.raises(StoreUnavailableError):
        client.requeue_dead(eid(1), reason="retry after fix")

    fix()
    client.deliver_once()

    assert client.store_available
    assert client.held_in_memory == 0
    assert set(rows(path)) == {event}


def test_construction_with_residue_files_stays_degraded(
    make_client: Callable[..., BillingClient], db_path: Path, logs: pytest.LogCaptureFixture
) -> None:
    wal = Path(str(db_path) + "-wal")
    wal.write_bytes(b"")

    client = make_client(db_path)

    assert not client.store_available
    assert not db_path.exists()
    assert "StoreResidueError" in error_text(logs)
    assert "outbox.db-wal" in error_text(logs)
    client.deliver_once()
    assert not client.store_available

    wal.unlink()
    client.deliver_once()
    assert client.store_available


def test_corrupt_file_at_construction_is_not_reopened(
    make_client: Callable[..., BillingClient], db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path.write_bytes(random.Random(11).randbytes(4096))
    opened: list[int] = []
    real_store = client_module.Store

    def counting(store_path: Any, *, clock: Any) -> Store:
        opened.append(1)
        return real_store(store_path, clock=clock)

    monkeypatch.setattr(client_module, "Store", counting)
    client = make_client(db_path)
    assert opened == [1]

    for _ in range(3):
        assert client.deliver_once() == DeliverySummary.zero()
    assert opened == [1]
    assert not client.store_available


# --- 运行中转入降级 ---------------------------------------------------------------------


@pytest.mark.parametrize("name", ["SQLITE_CORRUPT", "SQLITE_CORRUPT_INDEX", "SQLITE_NOTADB"])
def test_runtime_corruption_degrades_and_closes_connections(
    name: str,
    make_client: Callable[..., BillingClient],
    db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = make_client(db_path)
    first, second = llm(client), llm(client)
    store = client._store
    assert store is not None
    fault = Fault(monkeypatch, "_connection", lambda: sqlite_error(name, sqlite3.DatabaseError))

    third = llm(client)

    assert not client.store_available
    assert not store.is_open
    assert store._connections == {}
    assert client.held_in_memory == 1
    assert set(rows(db_path)) == {first, second}
    assert third not in rows(db_path)

    # 坏文件：路径上还是这个文件，就不重开。
    fault.active = False
    client.deliver_once()
    assert not client.store_available
    assert client.held_in_memory == 1


@pytest.mark.parametrize("name", ["SQLITE_IOERR", "SQLITE_IOERR_WRITE"])
def test_runtime_io_error_degrades_then_recovers(
    name: str,
    make_client: Callable[..., BillingClient],
    db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = make_client(db_path)
    first, second = llm(client), llm(client)
    store = client._store
    assert store is not None
    fault = Fault(monkeypatch, "_connection", lambda: sqlite_error(name))

    third = llm(client)

    assert not client.store_available
    assert not store.is_open
    assert client.held_in_memory == 1

    fault.active = False
    client.deliver_once()

    assert client.store_available
    assert client.held_in_memory == 0
    assert set(rows(db_path)) == {first, second, third}


@pytest.mark.parametrize("name", ["SQLITE_BUSY", "SQLITE_LOCKED", "SQLITE_BUSY_SNAPSHOT"])
def test_busy_or_locked_does_not_degrade(
    name: str,
    make_client: Callable[..., BillingClient],
    db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = make_client(db_path)
    first, second = llm(client), llm(client)
    store = client._store
    assert store is not None
    fault = Fault(monkeypatch, "insert_event", lambda: sqlite_error(name))

    third = llm(client)

    assert client.store_available
    assert store.is_open
    assert client.held_in_memory == 1

    fault.active = False
    client.deliver_once()

    assert client.held_in_memory == 0
    assert set(rows(db_path)) == {first, second, third}


def test_replaced_file_degrades_and_reopens_at_once(
    make_client: Callable[..., BillingClient], db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = make_client(db_path)
    first = llm(client)
    Fault(monkeypatch, "_connection", lambda: StoreReplacedError("replaced"), times=1)

    second = llm(client)

    assert client.store_available
    assert client.held_in_memory == 1
    client.deliver_once()
    assert client.held_in_memory == 0
    assert set(rows(db_path)) == {first, second}


def test_late_error_from_an_old_connection_only_warns(
    make_client: Callable[..., BillingClient],
    db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    logs: pytest.LogCaptureFixture,
) -> None:
    original = Store.insert_event
    entered, gate = threading.Event(), threading.Event()
    main_error: dict[str, Callable[[], BaseException] | None] = {"make": None}

    def insert(store: Store, event_id: str, payload: bytes) -> str:
        if threading.current_thread().name == "late-writer":
            entered.set()
            gate.wait(5)
            raise sqlite_error("SQLITE_CORRUPT", sqlite3.DatabaseError)
        make = main_error["make"]
        if make is not None:
            raise make()
        return original(store, event_id, payload)

    monkeypatch.setattr(Store, "insert_event", insert)
    client = make_client(db_path)
    late: dict[str, str] = {}
    writer = threading.Thread(
        target=lambda: late.setdefault("id", llm(client)), name="late-writer", daemon=True
    )
    writer.start()
    assert entered.wait(2)

    # 当前代号上的错误转入降级，随后重新打开（代号加一）。
    main_error["make"] = lambda: sqlite_error("SQLITE_IOERR")
    degraded = llm(client)
    assert not client.store_available
    main_error["make"] = None
    client.deliver_once()
    assert client.store_available
    store = client._store
    assert store is not None

    gate.set()
    writer.join(2)
    assert not writer.is_alive()

    # 旧代号迟到的 SQLITE_CORRUPT：只记 WARNING，不作废新库、不标记坏文件。
    assert client.store_available
    assert store.is_open
    assert "late outbox error from an old connection" in logs.text
    assert client.held_in_memory == 1
    client.deliver_once()
    assert client.held_in_memory == 0
    assert {degraded, late["id"]} <= set(rows(db_path))

    main_error["make"] = lambda: sqlite_error("SQLITE_IOERR")
    llm(client)
    assert not client.store_available
    main_error["make"] = None
    client.deliver_once()
    assert client.store_available


# --- 部分损坏的文件不来回切换、运行中隔离 -----------------------------------------------


def test_partially_corrupt_file_is_not_reopened_until_moved(
    make_client: Callable[..., BillingClient],
    db_path: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = make_client(db_path)
    first = llm(client)
    # 能打开、但写入时报 SQLITE_CORRUPT 的文件。
    fault = Fault(
        monkeypatch, "insert_event", lambda: sqlite_error("SQLITE_CORRUPT", sqlite3.DatabaseError)
    )
    reopened = count_calls(monkeypatch, "reopen")
    second = llm(client)
    assert not client.store_available

    for _ in range(3):
        assert client.deliver_once() == DeliverySummary.zero()
    assert reopened == []
    assert not client.store_available
    assert client.held_in_memory == 1
    assert set(rows(db_path)) == {first}

    quarantine = tmp_path / "quarantine"
    quarantine.mkdir()
    move_set(db_path, quarantine / "outbox.db")
    fault.active = False
    client.deliver_once()

    assert reopened == [1]
    assert client.store_available
    assert client.held_in_memory == 0
    assert set(rows(db_path)) == {second}
    assert set(rows(quarantine / "outbox.db")) == {first}


def test_disk_full_reopens_the_same_file(
    make_client: Callable[..., BillingClient], db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = make_client(db_path)
    first = llm(client)
    fault = Fault(monkeypatch, "insert_event", lambda: sqlite_error("SQLITE_FULL"))
    reopened = count_calls(monkeypatch, "reopen")
    second = llm(client)
    assert not client.store_available

    fault.active = False
    client.deliver_once()

    assert reopened == [1]
    assert client.store_available
    assert set(rows(db_path)) == {first, second}


def test_quarantine_corrupt_file_while_running(
    make_client: Callable[..., BillingClient], db_path: Path, tmp_path: Path
) -> None:
    db_path.write_bytes(random.Random(3).randbytes(8192))
    client = make_client(db_path, held_max_events=2)
    assert not client.store_available
    kept = [llm(client), llm(client)]
    dropped = llm(client)
    assert client.held_in_memory == 2
    assert client.incidents_pending == 1

    client.deliver_once()
    assert not client.store_available

    quarantine = tmp_path / "quarantine"
    quarantine.mkdir()
    move_set(db_path, quarantine / "outbox.db")
    client.deliver_once()

    assert client.store_available
    assert set(rows(db_path)) == set(kept)
    assert [(r["kind"], r["event_id"]) for r in incident_rows(db_path)] == [("DROPPED", dropped)]
    assert client.held_in_memory == 0
    assert client.incidents_pending == 0


# --- 死信运维、重放与事故确认 -----------------------------------------------------------


def test_disposals_raise_store_unavailable_when_degraded(
    make_client: Callable[..., BillingClient], tmp_path: Path
) -> None:
    client = make_client(tmp_path / "absent" / "outbox.db")
    start = datetime.fromtimestamp(T0 - 10, UTC)
    end = datetime.fromtimestamp(T0, UTC)
    calls: list[Callable[[], object]] = [
        lambda: client.requeue_dead(eid(1), reason="r"),
        lambda: client.ack_dead(eid(1), reason="r"),
        lambda: client.list_dead(),
        lambda: client.replay_delivered(delivered_from=start, delivered_until=end, reason="r"),
        lambda: client.list_incidents(),
        lambda: client.ack_incident("inc_x", reason="r"),
    ]
    for call in calls:
        with pytest.raises(StoreUnavailableError):
            call()


def test_disposal_busy_is_raised_as_is(
    make_client: Callable[..., BillingClient], db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = make_client(db_path)
    Fault(monkeypatch, "requeue_dead", lambda: sqlite_error("SQLITE_BUSY"))

    with pytest.raises(sqlite3.OperationalError):
        client.requeue_dead(eid(1), reason="retry")

    assert client.store_available


def test_disposal_store_failure_degrades_first(
    make_client: Callable[..., BillingClient], db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = make_client(db_path)
    Fault(monkeypatch, "list_incidents", lambda: sqlite_error("SQLITE_IOERR"))

    with pytest.raises(StoreUnavailableError) as info:
        client.list_incidents()

    assert isinstance(info.value.__cause__, sqlite3.OperationalError)
    assert not client.store_available


def test_disposals_forward_to_the_store(
    make_client: Callable[..., BillingClient], db_path: Path
) -> None:
    client = make_client(db_path, transport=FakeTransport(accept_all))
    event = llm(client)
    assert client.deliver_once().delivered == 1

    naive_start, naive_end = datetime(2026, 1, 1), datetime(2026, 1, 2)
    with pytest.raises(ValueError):
        client.replay_delivered(
            delivered_from=naive_start, delivered_until=naive_end, reason="restore"
        )
    start = datetime.fromtimestamp(T0 - 1, UTC)
    end = datetime.fromtimestamp(T0 + 1, UTC)
    with pytest.raises(ValueError):
        client.replay_delivered(delivered_from=start, delivered_until=end, reason="   ")
    assert (
        client.replay_delivered(delivered_from=start, delivered_until=end, reason="center restored")
        == 1
    )
    assert rows(db_path)[event]["status"] == "PENDING"
    assert ops_actions(db_path) == ["REPLAY_DELIVERED"]

    assert client.requeue_dead(event, reason="not dead") is False
    assert client.ack_dead(event, reason="not dead") is False
    assert client.list_dead() == []
    assert client.list_incidents() == []
    assert client.ack_incident("inc_missing", reason="nothing") is False
    with pytest.raises(ValueError):
        client.requeue_dead(event, reason="")


def test_store_unavailable_error_is_the_store_one() -> None:
    assert client_module.StoreUnavailableError is store_module.StoreUnavailableError
    assert "StoreUnavailableError" in client_module.__all__


def test_invalid_config_is_the_only_constructor_error() -> None:
    with pytest.raises(TypeError):
        BillingClient({"outbox_path": "x"})  # type: ignore[arg-type]

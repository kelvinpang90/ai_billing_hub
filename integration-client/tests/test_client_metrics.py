"""BillingClient.health() (design gate #571 v24 §2「健康指标」).

覆盖 §7 标 170 的「健康指标」「确认后告警消失」两行，以及 AIH-TASK-168、169 移交的断言：
「事故记录」「溢出事故」两行里 `health().level` 的部分，「webhook 不冒充对账」一行里
`status_reconcile_stale` 的部分。

SQLite 用 tmp_path 下的文件，HTTP 用注入的假 Transport，时间用注入的时钟；不 sleep。
行的状态有时用测试自己的 sqlite3 连接直接改（客户端代码只经 Store 读写）。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import random
import sqlite3
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest
from acuven_billing_client import (
    HEALTH_KEYS,
    BillingClient,
    BillingClientConfig,
    Response,
    Store,
    TransportError,
)
from acuven_billing_client.delivery import BATCH_PATH
from acuven_billing_client.metrics import (
    OLDEST_PENDING_P2_SECONDS,
    RECONCILE_STALE_SECONDS,
    STORE_KEYS,
)

T0 = 1_790_000_000.0
BASE_URL = "https://billing.example.com"
SECRET = "sk_" + "0" * 64
WEBHOOK_SECRET = "whsec_" + "2" * 40
TENANT = "ten_a"
PROJECT = "prj_a"
HOOK_PATH = "/hooks/acuven/status"
OCCURRED = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
COUNTERS = ("held_in_memory", "incidents_pending", "dropped_events", "conflicts_dropped")
EXPECTED_KEYS = [
    "store_available",
    "pending",
    "sending",
    "dead_unacked",
    "incidents_unacked",
    "oldest_pending_age_seconds",
    "held_in_memory",
    "incidents_pending",
    "dropped_events",
    "conflicts_dropped",
    "last_delivery_ok_at",
    "last_status_reconcile_ok_at",
    "status_reconcile_stale",
    "auth_failing",
    "level",
]


# --- 替身 -------------------------------------------------------------------------------


class FakeClock:
    def __init__(self, now: float = T0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def status_data(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "tenant_id": TENANT,
        "project_id": PROJECT,
        "billing_mode": "PREPAID",
        "billing_status": "ACTIVE",
        "account_status": "ACTIVE",
        "project_integration_status": "ENABLED",
        "effective_status": "ALLOW_AI",
        "status_version": 1,
        "reason_code": None,
        "effective_at": "2026-09-01T12:00:00Z",
    }
    data.update(overrides)
    return data


STATUS_OK = Response(200, json.dumps({"data": status_data()}).encode())
UNAVAILABLE = Response(503, b'{"error":{"code":"BILLING_UNAVAILABLE"},"retryable":true}')
AUTH_FAILED = Response(401, b'{"error":{"code":"INTEGRATION_AUTH_FAILED"},"retryable":false}')


def batch_results(body: bytes, **result: Any) -> Response:
    document = json.loads(body)
    results = [
        {"index": index, "event_id": event["event_id"], **result}
        for index, event in enumerate(document["events"])
    ]
    return Response(200, json.dumps({"data": {"results": results}}).encode())


def accept_all(url: str, body: bytes) -> Response:
    assert urlsplit(url).path.endswith(BATCH_PATH)
    return batch_results(
        body, status="accepted", processing_status="RECEIVED", error_code=None, retryable=False
    )


def reject_all(url: str, body: bytes) -> Response:
    return batch_results(body, status="rejected", error_code="VALIDATION_ERROR", retryable=False)


def network_down(url: str, body: bytes) -> Response:
    raise TransportError("network down")


def auth_failed(url: str, body: bytes) -> Response:
    return AUTH_FAILED


class FakeTransport:
    """GET effective-status gets `status_reply`; delivery POSTs go to `post`."""

    def __init__(self) -> None:
        self.status_reply = UNAVAILABLE
        self.post: Callable[[str, bytes], Response] = network_down

    def send(self, method: str, url: str, headers: Any, body: bytes, timeout: float) -> Response:
        if method.upper() == "GET":
            return self.status_reply
        return self.post(url, body)


def sqlite_error(name: str, cls: type[sqlite3.Error] = sqlite3.OperationalError) -> sqlite3.Error:
    error = cls(f"simulated {name}")
    error.sqlite_errorname = name  # type: ignore[attr-defined]
    return error


class Fault:
    """Make one `Store` method raise while active."""

    def __init__(
        self, monkeypatch: pytest.MonkeyPatch, method: str, make_error: Callable[[], BaseException]
    ) -> None:
        self.active = True
        original = getattr(Store, method)
        fault = self

        def replacement(store: Store, *args: Any, **kwargs: Any) -> Any:
            if fault.active:
                raise make_error()
            return original(store, *args, **kwargs)

        monkeypatch.setattr(Store, method, replacement)


# --- 事件、webhook 与库 -----------------------------------------------------------------


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


def signed_webhook(client: BillingClient, clock: FakeClock, **overrides: Any) -> bool:
    event_id = "sevt_prj_a_7"
    body = json.dumps({**status_data(**overrides), "event_id": event_id}).encode()
    ts = str(int(clock.now))
    canonical = "\n".join(
        (
            "POST",
            HOOK_PATH,
            ts,
            event_id,
            hashlib.sha256(body).hexdigest(),
        )
    )
    signature = hmac.new(WEBHOOK_SECRET.encode(), canonical.encode(), hashlib.sha256).hexdigest()
    headers = {
        "X-Acuven-Timestamp": ts,
        "X-Acuven-Signature": signature,
        "X-Acuven-Event-ID": event_id,
        "X-Acuven-Key-Version": "1",
    }
    return client.handle_status_webhook(
        method="POST", raw_path=HOOK_PATH, query_string=b"", headers=headers, body=body
    )


def execute(path: Path, sql: str, params: tuple[Any, ...] = ()) -> None:
    conn = sqlite3.connect(path, isolation_level=None)
    try:
        conn.execute(sql, params)
    finally:
        conn.close()


def insert_raw(path: Path, event_id: str, payload: bytes) -> None:
    execute(
        path,
        "INSERT INTO usage_outbox"
        " (event_id, payload, status, attempts, next_attempt_at, created_at)"
        " VALUES (?, ?, 'PENDING', 0, ?, ?)",
        (event_id, payload, T0, T0),
    )


def make_config(path: Path, **overrides: Any) -> BillingClientConfig:
    values: dict[str, Any] = {
        "base_url": BASE_URL,
        "api_key": "ak_test",
        "key_version": 3,
        "secret": SECRET,
        "tenant_id": TENANT,
        "project_id": PROJECT,
        "outbox_path": str(path),
        "webhook_secrets": {1: WEBHOOK_SECRET},
    }
    values.update(overrides)
    return BillingClientConfig(**values)


def store_keys(report: dict[str, Any]) -> dict[str, Any]:
    return {key: report[key] for key in STORE_KEYS}


# --- 夹具 -------------------------------------------------------------------------------


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "outbox.db"


@pytest.fixture
def transport() -> FakeTransport:
    return FakeTransport()


@pytest.fixture
def make_client(
    clock: FakeClock, transport: FakeTransport
) -> Iterator[Callable[..., BillingClient]]:
    made: list[BillingClient] = []

    def make(path: Path, **overrides: Any) -> BillingClient:
        client = BillingClient(
            make_config(path, **overrides),
            transport=transport,
            clock=clock,
            rng=random.Random(0),
        )
        made.append(client)
        return client

    yield make
    for client in made:
        client.close(timeout=2)


def reconcile_ok(client: BillingClient, transport: FakeTransport) -> None:
    transport.status_reply = STATUS_OK
    assert client.reconcile_status_once() is True
    transport.status_reply = UNAVAILABLE


# --- 键集合与基本值 ---------------------------------------------------------------------


def test_keys_are_fixed_and_json_serialisable(
    make_client: Callable[..., BillingClient], db_path: Path
) -> None:
    client = make_client(db_path)

    report = client.health()

    assert list(report) == EXPECTED_KEYS
    assert list(HEALTH_KEYS) == EXPECTED_KEYS
    json.dumps(report)
    assert report == {
        "store_available": True,
        "pending": 0,
        "sending": 0,
        "dead_unacked": 0,
        "incidents_unacked": 0,
        "oldest_pending_age_seconds": None,
        "held_in_memory": 0,
        "incidents_pending": 0,
        "dropped_events": 0,
        "conflicts_dropped": 0,
        "last_delivery_ok_at": None,
        "last_status_reconcile_ok_at": None,
        # 从未对账成功。
        "status_reconcile_stale": True,
        "auth_failing": False,
        "level": "P2",
    }


def test_counts(
    make_client: Callable[..., BillingClient],
    db_path: Path,
    clock: FakeClock,
    transport: FakeTransport,
) -> None:
    client = make_client(db_path)
    ids = [llm(client) for _ in range(6)]
    execute(
        db_path,
        "UPDATE usage_outbox SET status = 'SENDING', lease_until = ? WHERE event_id = ?",
        (T0 + 60, ids[1]),
    )
    execute(
        db_path,
        "UPDATE usage_outbox SET status = 'DEAD', dead_at = ? WHERE event_id IN (?, ?)",
        (T0, ids[2], ids[3]),
    )
    execute(db_path, "UPDATE usage_outbox SET dead_acked_at = ? WHERE event_id = ?", (T0, ids[3]))
    execute(
        db_path,
        "UPDATE usage_outbox SET status = 'DELIVERED', delivered_at = ? WHERE event_id = ?",
        (T0, ids[4]),
    )
    for n, acked in ((1, None), (2, T0), (3, None)):
        execute(
            db_path,
            "INSERT INTO incidents (incident_id, kind, event_id, dropped_count,"
            " first_occurred_at, last_occurred_at, recorded_at, acked_at)"
            " VALUES (?, 'DROPPED', ?, 1, ?, ?, ?, ?)",
            (f"inc_{n}", eid(n), T0, T0, T0, acked),
        )
    clock.now = T0 + 10
    reconcile_ok(client, transport)

    report = client.health()

    assert report["pending"] == 2
    assert report["sending"] == 1
    assert report["dead_unacked"] == 1
    assert report["incidents_unacked"] == 2
    assert report["oldest_pending_age_seconds"] == 10.0
    assert report["last_status_reconcile_ok_at"] == T0 + 10
    assert report["status_reconcile_stale"] is False
    assert report["level"] == "P1"


def test_last_delivery_ok_at(
    make_client: Callable[..., BillingClient],
    db_path: Path,
    clock: FakeClock,
    transport: FakeTransport,
) -> None:
    client = make_client(db_path)
    llm(client)
    transport.post = accept_all
    clock.now = T0 + 5
    assert client.deliver_once().delivered == 1
    reconcile_ok(client, transport)

    report = client.health()

    assert report["last_delivery_ok_at"] == T0 + 5
    assert report["pending"] == 0
    assert report["oldest_pending_age_seconds"] is None
    assert report["level"] == "OK"


# --- 阈值边界 ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("age", "level"),
    [(OLDEST_PENDING_P2_SECONDS, "OK"), (OLDEST_PENDING_P2_SECONDS + 1, "P2")],
)
def test_oldest_pending_threshold(
    age: int,
    level: str,
    make_client: Callable[..., BillingClient],
    db_path: Path,
    clock: FakeClock,
    transport: FakeTransport,
) -> None:
    client = make_client(db_path)
    llm(client)
    clock.now = T0 + age
    reconcile_ok(client, transport)

    report = client.health()

    assert report["oldest_pending_age_seconds"] == float(age)
    assert report["status_reconcile_stale"] is False
    assert report["level"] == level


def test_oldest_counts_sending_rows_with_expired_lease(
    make_client: Callable[..., BillingClient],
    db_path: Path,
    clock: FakeClock,
    transport: FakeTransport,
) -> None:
    client = make_client(db_path)
    clock.now = T0 - 1000
    old = llm(client)
    clock.now = T0
    llm(client)
    execute(
        db_path,
        "UPDATE usage_outbox SET status = 'SENDING', lease_until = ? WHERE event_id = ?",
        (T0 - 500, old),
    )
    reconcile_ok(client, transport)

    report = client.health()

    assert report["sending"] == 1
    assert report["oldest_pending_age_seconds"] == 1000.0
    assert report["level"] == "P2"


def test_oldest_age_is_never_negative(
    make_client: Callable[..., BillingClient], db_path: Path, clock: FakeClock
) -> None:
    client = make_client(db_path)
    llm(client)
    # 注入的时钟早于 created_at。
    clock.now = T0 - 100

    assert client.health()["oldest_pending_age_seconds"] == 0.0


@pytest.mark.parametrize(
    ("elapsed", "stale", "level"),
    [(RECONCILE_STALE_SECONDS, False, "OK"), (RECONCILE_STALE_SECONDS + 1, True, "P2")],
)
def test_reconcile_stale_threshold(
    elapsed: int,
    stale: bool,
    level: str,
    make_client: Callable[..., BillingClient],
    db_path: Path,
    clock: FakeClock,
    transport: FakeTransport,
) -> None:
    client = make_client(db_path)
    reconcile_ok(client, transport)
    clock.now = T0 + elapsed
    # 期间对账失败不改 last_ok_at。
    assert client.reconcile_status_once() is False

    report = client.health()

    assert report["last_status_reconcile_ok_at"] == T0
    assert report["status_reconcile_stale"] is stale
    assert report["level"] == level


# --- P1 条件 ----------------------------------------------------------------------------


def test_dead_unacked_is_p1_until_acked(
    make_client: Callable[..., BillingClient],
    db_path: Path,
    transport: FakeTransport,
) -> None:
    client = make_client(db_path)
    event = llm(client)
    transport.post = reject_all
    client.deliver_once()
    reconcile_ok(client, transport)

    report = client.health()
    assert report["dead_unacked"] == 1
    assert report["level"] == "P1"

    assert client.ack_dead(event, reason="checked with the platform") is True
    report = client.health()
    assert report["dead_unacked"] == 0
    assert report["level"] == "OK"


def test_auth_failing_survives_restart(
    make_client: Callable[..., BillingClient],
    db_path: Path,
    transport: FakeTransport,
) -> None:
    first = make_client(db_path)
    llm(first)
    transport.post = auth_failed
    first.deliver_once()
    reconcile_ok(first, transport)
    report = first.health()
    assert report["auth_failing"] is True
    assert report["level"] == "P1"
    first.close()

    # 模拟重启：auth_failing 从库里算，新客户端同样看到。
    second = make_client(db_path)
    report = second.health()
    assert report["auth_failing"] is True
    assert report["level"] == "P1"


def test_auth_failing_only_looks_at_auth_failed(
    make_client: Callable[..., BillingClient],
    db_path: Path,
    transport: FakeTransport,
) -> None:
    client = make_client(db_path)
    llm(client)
    transport.post = lambda url, body: UNAVAILABLE
    client.deliver_once()
    reconcile_ok(client, transport)

    report = client.health()

    assert report["pending"] == 1
    assert report["auth_failing"] is False
    assert report["level"] == "OK"


def test_held_in_memory_is_p1_until_refilled(
    make_client: Callable[..., BillingClient],
    db_path: Path,
    transport: FakeTransport,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = make_client(db_path)
    reconcile_ok(client, transport)
    busy = Fault(monkeypatch, "insert_event", lambda: sqlite_error("SQLITE_BUSY"))
    llm(client)

    # 库可读，只因暂存里有 1 条而 P1。
    report = client.health()
    assert report["store_available"] is True
    assert report["held_in_memory"] == 1
    assert report["pending"] == 0
    assert report["level"] == "P1"

    busy.active = False
    client.deliver_once()
    report = client.health()
    assert report["held_in_memory"] == 0
    assert report["pending"] == 1
    assert report["level"] == "OK"


# --- 存储不可用与读库失败 ---------------------------------------------------------------


def test_store_unavailable(make_client: Callable[..., BillingClient], tmp_path: Path) -> None:
    client = make_client(tmp_path / "missing" / "outbox.db", held_max_events=1)
    llm(client)
    llm(client)

    report = client.health()

    assert list(report) == EXPECTED_KEYS
    assert report["store_available"] is False
    assert store_keys(report) == dict.fromkeys(STORE_KEYS)
    assert report["held_in_memory"] == 1
    assert report["incidents_pending"] == 1
    assert report["dropped_events"] == 1
    assert report["conflicts_dropped"] == 0
    assert report["level"] == "P1"


@pytest.mark.parametrize(
    "make_error",
    [lambda: sqlite_error("SQLITE_BUSY"), lambda: RuntimeError("unexpected")],
    ids=["busy", "runtime-error"],
)
def test_read_failure_without_degrading(
    make_error: Callable[[], BaseException],
    make_client: Callable[..., BillingClient],
    db_path: Path,
    transport: FakeTransport,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = make_client(db_path)
    reconcile_ok(client, transport)
    busy = Fault(monkeypatch, "insert_event", lambda: sqlite_error("SQLITE_BUSY"))
    llm(client)
    busy.active = False
    Fault(monkeypatch, "delivery_counts", make_error)

    report = client.health()

    assert list(report) == EXPECTED_KEYS
    assert report["store_available"] is True
    assert store_keys(report) == dict.fromkeys(STORE_KEYS)
    assert report["held_in_memory"] == 1
    assert report["incidents_pending"] == 0
    assert report["level"] == "P1"


def test_read_failure_that_degrades_reports_store_unavailable(
    make_client: Callable[..., BillingClient],
    db_path: Path,
    transport: FakeTransport,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = make_client(db_path)
    reconcile_ok(client, transport)
    Fault(monkeypatch, "read_reconcile", lambda: sqlite_error("SQLITE_IOERR"))

    report = client.health()

    # 返回转入降级之后的 store_available。
    assert report["store_available"] is False
    assert client.store_available is False
    assert store_keys(report) == dict.fromkeys(STORE_KEYS)
    assert report["level"] == "P1"


def test_health_after_close_does_not_raise(
    make_client: Callable[..., BillingClient], db_path: Path
) -> None:
    client = make_client(db_path)
    client.close()

    report = client.health()

    assert report["store_available"] is False
    assert store_keys(report) == dict.fromkeys(STORE_KEYS)
    assert report["level"] == "P1"


# --- 事故（AIH-TASK-168 移交）与确认后告警消失 -------------------------------------------


def test_incident_record_levels(
    make_client: Callable[..., BillingClient],
    db_path: Path,
    transport: FakeTransport,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = make_client(db_path, held_max_events=1)
    reconcile_ok(client, transport)
    busy = Fault(monkeypatch, "insert_event", lambda: sqlite_error("SQLITE_BUSY"))
    own(client, 1, 1)
    own(client, 2)
    own(client, 3)
    insert_raw(db_path, eid(1), b'{"different":true}')

    report = client.health()
    assert report["held_in_memory"] == 1
    assert report["incidents_pending"] == 2
    assert report["level"] == "P1"

    busy.active = False
    client.deliver_once()
    report = client.health()
    assert report["held_in_memory"] == 0
    assert report["incidents_pending"] == 0
    assert report["incidents_unacked"] == 3
    assert report["dropped_events"] == 2
    assert report["conflicts_dropped"] == 1
    assert report["level"] == "P1"
    client.close()

    # 模拟重启：未确认事故在库里，仍然 P1。
    restarted = make_client(db_path, held_max_events=1)
    report = restarted.health()
    assert report["incidents_unacked"] == 3
    assert report["dropped_events"] == 0
    assert report["level"] == "P1"

    incidents = restarted.list_incidents()
    for done, incident in enumerate(incidents, start=1):
        assert restarted.ack_incident(incident.incident_id, reason="reconciled by hand") is True
        report = restarted.health()
        assert report["incidents_unacked"] == 3 - done
        assert report["level"] == ("P1" if done < 3 else "OK")


def test_overflow_incident_levels(
    make_client: Callable[..., BillingClient],
    db_path: Path,
    clock: FakeClock,
    transport: FakeTransport,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = make_client(db_path, held_max_events=1, pending_incidents_max=2)
    busy = Fault(monkeypatch, "insert_event", lambda: sqlite_error("SQLITE_BUSY"))
    llm(client)
    for _ in range(5):
        llm(client)

    report = client.health()
    assert report["incidents_pending"] == 5
    assert report["level"] == "P1"

    busy.active = False
    client.deliver_once()
    report = client.health()
    assert report["incidents_pending"] == 0
    assert report["incidents_unacked"] == 3
    assert report["level"] == "P1"

    clock.now = T0 + 1
    reconcile_ok(client, transport)
    for incident in client.list_incidents():
        assert client.ack_incident(incident.incident_id, reason="overflow reconciled") is True
    report = client.health()
    assert report["incidents_unacked"] == 0
    # 累计数仍在，但不参与 level。
    assert report["dropped_events"] == 5
    assert report["level"] == "OK"


def test_alert_clears_after_ack_in_the_same_process(
    make_client: Callable[..., BillingClient],
    db_path: Path,
    transport: FakeTransport,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = make_client(db_path, held_max_events=1)
    busy = Fault(monkeypatch, "insert_event", lambda: sqlite_error("SQLITE_BUSY"))
    own(client, 1, 1)
    own(client, 2)
    own(client, 3)
    insert_raw(db_path, eid(1), b'{"different":true}')
    busy.active = False
    client.deliver_once()
    reconcile_ok(client, transport)
    assert client.health()["level"] == "P1"

    for incident in client.list_incidents():
        client.ack_incident(incident.incident_id, reason="reconciled by hand")

    report = client.health()
    assert report["incidents_unacked"] == 0
    assert report["dropped_events"] == 2
    assert report["conflicts_dropped"] == 1
    assert report["level"] == "OK"


# --- webhook 不冒充对账（AIH-TASK-169 移交） -------------------------------------------


def test_webhook_does_not_count_as_reconcile(
    make_client: Callable[..., BillingClient],
    db_path: Path,
    clock: FakeClock,
) -> None:
    client = make_client(db_path)
    assert client.reconcile_status_once() is False

    assert signed_webhook(client, clock) is True

    report = client.health()
    assert client.current_status() is not None
    assert report["last_status_reconcile_ok_at"] is None
    assert report["status_reconcile_stale"] is True
    assert report["level"] == "P2"


def test_webhook_does_not_refresh_a_stale_reconcile(
    make_client: Callable[..., BillingClient],
    db_path: Path,
    clock: FakeClock,
    transport: FakeTransport,
) -> None:
    client = make_client(db_path)
    reconcile_ok(client, transport)
    clock.now = T0 + RECONCILE_STALE_SECONDS + 1
    assert client.reconcile_status_once() is False

    assert signed_webhook(client, clock, status_version=2) is True

    report = client.health()
    assert report["last_status_reconcile_ok_at"] == T0
    assert report["status_reconcile_stale"] is True
    assert report["level"] == "P2"

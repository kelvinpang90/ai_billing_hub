"""BillingClient effective status: reconcile, cadence, should_block_ai (design gate #571 v24).

覆盖 §7 标 169 的对账各行：对账应用与版本 3 → 5 → 4、先失败后成功、状态应答字段（缺字段、
类型错、取值非法与多一个未知字段）、换项目不沿用旧状态、租户或项目不符、对账失败放行两种、
状态表读失败放行、对账不被投递拖住、对账节奏七步，以及降级、存储错误与 `reset_status`。
webhook 的各行在 test_client_webhook.py。

SQLite 用 tmp_path 下的文件，HTTP 用注入的假 Transport，时间用注入的时钟；对账线程每次最多
等待的秒数改成 0.01，用 threading.Event / Condition 同步，不 sleep。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import random
import sqlite3
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest
from acuven_billing_client import (
    BillingClient,
    BillingClientConfig,
    Response,
    Store,
    StoreReplacedError,
    TransportError,
)
from acuven_billing_client import client as client_module
from acuven_billing_client.status import EFFECTIVE_STATUS_PATH, MAX_STATUS_VERSION

T0 = 1_790_000_000.0
LOGGER = "acuven_billing_client"
BASE_URL = "https://billing.example.com"
SECRET = "sk_" + "0" * 64
TENANT = "ten_a"
PROJECT = "prj_a"
FAST = 0.01
STATUS_KEYS = {
    "tenant_id",
    "project_id",
    "billing_mode",
    "billing_status",
    "account_status",
    "project_integration_status",
    "effective_status",
    "status_version",
    "reason_code",
    "effective_at",
}


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


def ok(**overrides: Any) -> Response:
    return Response(200, json.dumps({"data": status_data(**overrides)}).encode())


UNAVAILABLE = Response(503, b'{"error":{"code":"BILLING_UNAVAILABLE"},"retryable":true}')

Reply = Response | BaseException | Callable[[], Response]


class StatusTransport:
    """GETs of effective-status take the next scripted reply (default 503); POSTs get `post`."""

    def __init__(self, *replies: Reply, post: Callable[[], Response] | None = None) -> None:
        self.replies: list[Reply] = list(replies)
        self.post = post
        self.lock = threading.Lock()
        self.gets: list[tuple[str, dict[str, str], bytes, float]] = []
        self.posts = 0
        self.in_flight = 0
        self.max_in_flight = 0
        self.got = threading.Event()

    def send(self, method: str, url: str, headers: Any, body: bytes, timeout: float) -> Response:
        if method != "GET":
            with self.lock:
                self.posts += 1
            return self.post() if self.post is not None else UNAVAILABLE
        assert urlsplit(url).path.endswith(EFFECTIVE_STATUS_PATH)
        with self.lock:
            self.gets.append((url, dict(headers), body, timeout))
            reply = self.replies.pop(0) if self.replies else UNAVAILABLE
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
        self.got.set()
        try:
            if isinstance(reply, BaseException):
                raise reply
            if callable(reply):
                return reply()
            return reply
        finally:
            with self.lock:
                self.in_flight -= 1

    @property
    def get_count(self) -> int:
        with self.lock:
            return len(self.gets)


def sqlite_error(name: str, cls: type[sqlite3.Error] = sqlite3.OperationalError) -> sqlite3.Error:
    error = cls(f"simulated {name}")
    error.sqlite_errorname = name  # type: ignore[attr-defined]
    return error


class Fault:
    """Make one `Store` method raise while active; counts calls."""

    def __init__(
        self, monkeypatch: pytest.MonkeyPatch, method: str, make_error: Callable[[], BaseException]
    ) -> None:
        self.active = True
        self.calls = 0
        original = getattr(Store, method)
        fault = self

        def replacement(store: Store, *args: Any, **kwargs: Any) -> Any:
            fault.calls += 1
            if fault.active:
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


class TickWatch:
    """Record, after every reconcile-thread wake-up, how many GETs had been sent."""

    def __init__(self, client: BillingClient, transport: StatusTransport) -> None:
        self.cond = threading.Condition()
        self.ended: list[int] = []
        original = client._reconcile_tick

        def tick() -> float:
            try:
                return original()
            finally:
                with self.cond:
                    self.ended.append(transport.get_count)
                    self.cond.notify_all()

        client._reconcile_tick = tick  # type: ignore[method-assign]

    def wait(self, predicate: Callable[[list[int]], bool]) -> None:
        with self.cond:
            assert self.cond.wait_for(lambda: predicate(self.ended), timeout=5)

    def count(self) -> int:
        with self.cond:
            return len(self.ended)


# --- 库 ---------------------------------------------------------------------------------


def raw(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    return conn


def status_rows(path: Path) -> list[dict[str, Any]]:
    conn = raw(path)
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM status_state")]
    finally:
        conn.close()


def reconcile_row(path: Path) -> dict[str, Any] | None:
    conn = raw(path)
    try:
        row = conn.execute(
            "SELECT * FROM reconcile_state WHERE tenant_id = ? AND project_id = ?",
            (TENANT, PROJECT),
        ).fetchone()
        return None if row is None else dict(row)
    finally:
        conn.close()


def ops_rows(path: Path) -> list[dict[str, Any]]:
    conn = raw(path)
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM ops_log")]
    finally:
        conn.close()


def make_config(path: Path, **overrides: Any) -> BillingClientConfig:
    values: dict[str, Any] = {
        "base_url": BASE_URL,
        "api_key": "ak_test",
        "key_version": 3,
        "secret": SECRET,
        "tenant_id": TENANT,
        "project_id": PROJECT,
        "outbox_path": str(path),
    }
    values.update(overrides)
    return BillingClientConfig(**values)


def llm(client: BillingClient) -> str:
    return client.record_llm_usage(
        provider="openai",
        model="gpt-4o",
        usage_type="llm.chat",
        input_tokens=10,
        output_tokens=5,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
        request_id="req-1",
    )


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

    def make(path: Path, transport: Any, **overrides: Any) -> BillingClient:
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


@pytest.fixture
def logs(caplog: pytest.LogCaptureFixture) -> pytest.LogCaptureFixture:
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    return caplog


@pytest.fixture
def fast_wakeups(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(client_module, "RECONCILE_WAIT_MAX_SECONDS", FAST)


def warnings(logs: pytest.LogCaptureFixture) -> str:
    return "\n".join(r.getMessage() for r in logs.records if r.levelno == logging.WARNING)


# --- 一次对账 ---------------------------------------------------------------------------


def test_reconcile_signs_an_empty_get(
    make_client: Callable[..., BillingClient], db_path: Path
) -> None:
    transport = StatusTransport(ok())
    client = make_client(db_path, transport, base_url=BASE_URL + "/prefix/")

    assert client.reconcile_status_once() is True

    [(url, headers, body, timeout)] = transport.gets
    path = "/prefix" + EFFECTIVE_STATUS_PATH
    assert url == "https://billing.example.com" + path
    assert body == b""
    assert timeout == 10
    assert headers["X-Acuven-Timestamp"] == str(int(T0))
    assert "Content-Type" not in headers
    canonical = "\n".join(
        (
            "GET",
            path,
            headers["X-Acuven-Timestamp"],
            headers["X-Acuven-Request-Id"],
            hashlib.sha256(b"").hexdigest(),
        )
    )
    expected = hmac.new(SECRET.encode(), canonical.encode(), hashlib.sha256).hexdigest()
    assert headers["X-Acuven-Signature"] == expected


def test_reconcile_applies_by_version(
    make_client: Callable[..., BillingClient], db_path: Path, clock: FakeClock
) -> None:
    transport = StatusTransport(
        ok(status_version=3),
        ok(status_version=5, effective_status="BLOCK_AI", reason_code="WALLET_EXHAUSTED"),
        ok(status_version=4),
    )
    client = make_client(db_path, transport)

    assert client.reconcile_status_once() is True
    assert client.current_status()["status_version"] == 3

    clock.now = T0 + 10
    assert client.reconcile_status_once() is True
    assert client.should_block_ai() is True

    # 版本 4 不改状态，但对账照样算成功、更新 last_ok_at。
    clock.now = T0 + 20
    assert client.reconcile_status_once() is True
    status = client.current_status()
    assert status is not None
    assert status["status_version"] == 5
    assert status["effective_status"] == "BLOCK_AI"
    assert status["applied_at"] == T0 + 10
    assert client.should_block_ai() is True
    assert reconcile_row(db_path) == {
        "tenant_id": TENANT,
        "project_id": PROJECT,
        "last_ok_at": T0 + 20,
        "last_error_at": None,
    }


def test_first_failure_then_success(
    make_client: Callable[..., BillingClient], db_path: Path, clock: FakeClock
) -> None:
    transport = StatusTransport(UNAVAILABLE, ok(status_version=7))
    client = make_client(db_path, transport)

    assert client.reconcile_status_once() is False
    assert status_rows(db_path) == []
    assert reconcile_row(db_path) == {
        "tenant_id": TENANT,
        "project_id": PROJECT,
        "last_ok_at": None,
        "last_error_at": T0,
    }

    clock.now = T0 + 30
    assert client.reconcile_status_once() is True
    [row] = status_rows(db_path)
    assert row["status_version"] == 7
    assert reconcile_row(db_path)["last_ok_at"] == T0 + 30
    assert reconcile_row(db_path)["last_error_at"] == T0


def _without(name: str) -> dict[str, Any]:
    data = status_data()
    del data[name]
    return data


BAD_RESPONSES: list[tuple[str, Response]] = [
    *[
        (f"missing {name}", Response(200, json.dumps({"data": _without(name)}).encode()))
        for name in sorted(STATUS_KEYS)
    ],
    *[
        (f"{name} not a string", ok(**{name: 1}))
        for name in (
            "tenant_id",
            "project_id",
            "billing_mode",
            "billing_status",
            "account_status",
            "project_integration_status",
            "effective_at",
        )
    ],
    *[(f"{name} null", ok(**{name: None})) for name in ("billing_mode", "effective_at")],
    ("reason_code a number", ok(reason_code=5)),
    ("status_version a bool", ok(status_version=True)),
    ("status_version negative", ok(status_version=-1)),
    ("status_version a float", ok(status_version=3.0)),
    ("status_version a string", ok(status_version="3")),
    ("status_version over 2^63 - 1", ok(status_version=MAX_STATUS_VERSION + 1)),
    ("effective_status unknown", ok(effective_status="MAYBE_AI")),
    ("effective_status lowercase", ok(effective_status="block_ai")),
    ("other tenant", ok(tenant_id="ten_b")),
    ("other project", ok(project_id="prj_b")),
    ("data not an object", Response(200, b'{"data":[1,2]}')),
    ("no data", Response(200, b'{"result":{}}')),
    ("body not an object", Response(200, b"[]")),
    ("body not JSON", Response(200, b"<html>")),
    ("body not UTF-8", Response(200, b"\xff\xfe")),
    ("HTTP 401", Response(401, b'{"error":{"code":"INTEGRATION_AUTH_FAILED"}}')),
    ("HTTP 202", Response(202, json.dumps({"data": status_data()}).encode())),
]


@pytest.mark.parametrize(
    "response", [r for _, r in BAD_RESPONSES], ids=[name for name, _ in BAD_RESPONSES]
)
def test_invalid_response_is_a_failure(
    response: Response,
    make_client: Callable[..., BillingClient],
    db_path: Path,
    logs: pytest.LogCaptureFixture,
) -> None:
    client = make_client(db_path, StatusTransport(response))

    assert client.reconcile_status_once() is False

    assert status_rows(db_path) == []
    assert reconcile_row(db_path)["last_error_at"] == T0
    assert reconcile_row(db_path)["last_ok_at"] is None
    assert "status reconcile" in warnings(logs)
    assert client.should_block_ai() is False


def test_network_error_and_transport_bug_are_failures(
    make_client: Callable[..., BillingClient], db_path: Path
) -> None:
    transport = StatusTransport(TransportError("down"), RuntimeError("bug"), ok())
    client = make_client(db_path, transport)

    assert client.reconcile_status_once() is False
    assert client.reconcile_status_once() is False
    assert client.reconcile_status_once() is True


def test_unknown_field_is_applied_but_not_stored(
    make_client: Callable[..., BillingClient], db_path: Path
) -> None:
    body = {"data": {**status_data(status_version=MAX_STATUS_VERSION), "cost_cents": 1234}}
    client = make_client(db_path, StatusTransport(Response(200, json.dumps(body).encode())))

    assert client.reconcile_status_once() is True

    status = client.current_status()
    assert status is not None
    assert set(status) == STATUS_KEYS | {"applied_at"}
    assert status["status_version"] == MAX_STATUS_VERSION
    [row] = status_rows(db_path)
    assert set(row) == STATUS_KEYS | {"applied_at"}
    assert "1234" not in json.dumps(row)


def test_other_values_of_open_string_fields_are_accepted(
    make_client: Callable[..., BillingClient], db_path: Path
) -> None:
    transport = StatusTransport(
        ok(billing_mode="SOMETHING_NEW", account_status="", reason_code="NEW_REASON")
    )
    client = make_client(db_path, transport)

    assert client.reconcile_status_once() is True
    assert client.current_status()["billing_mode"] == "SOMETHING_NEW"


# --- 换项目、放行与读失败 ---------------------------------------------------------------


def test_new_project_does_not_inherit_old_status(
    make_client: Callable[..., BillingClient], db_path: Path, clock: FakeClock
) -> None:
    with Store(db_path, clock=clock) as store:
        store.apply_status(
            "ten_old",
            "prj_old",
            status_data(
                tenant_id="ten_old",
                project_id="prj_old",
                effective_status="BLOCK_AI",
                status_version=99,
            ),
            now=T0 - 100,
        )
    client = make_client(db_path, StatusTransport(ok(status_version=1)))
    assert client.should_block_ai() is False
    assert client.current_status() is None

    assert client.reconcile_status_once() is True

    assert client.should_block_ai() is False
    rows = {(r["tenant_id"], r["project_id"]): r for r in status_rows(db_path)}
    assert rows[(TENANT, PROJECT)]["status_version"] == 1
    assert rows[("ten_old", "prj_old")]["status_version"] == 99
    assert rows[("ten_old", "prj_old")]["effective_status"] == "BLOCK_AI"
    assert rows[("ten_old", "prj_old")]["applied_at"] == T0 - 100


def test_reconcile_failure_allows_ai_when_never_succeeded(
    make_client: Callable[..., BillingClient], db_path: Path, logs: pytest.LogCaptureFixture
) -> None:
    client = make_client(db_path, StatusTransport(UNAVAILABLE, UNAVAILABLE))

    assert client.reconcile_status_once() is False
    assert client.reconcile_status_once() is False

    assert client.should_block_ai() is False
    assert client.current_status() is None
    assert "no effective status" in warnings(logs)


def test_reconcile_failure_keeps_an_applied_block(
    make_client: Callable[..., BillingClient], db_path: Path, clock: FakeClock
) -> None:
    transport = StatusTransport(
        ok(effective_status="BLOCK_AI", status_version=2), UNAVAILABLE, TransportError("down")
    )
    client = make_client(db_path, transport)

    assert client.reconcile_status_once() is True
    clock.now = T0 + 30
    assert client.reconcile_status_once() is False
    clock.now = T0 + 270
    assert client.reconcile_status_once() is False

    assert client.should_block_ai() is True
    assert reconcile_row(db_path)["last_ok_at"] == T0


@pytest.mark.parametrize(("name", "degrades"), [("SQLITE_IOERR", True), ("SQLITE_BUSY", False)])
def test_status_read_failure_allows_ai(
    name: str,
    degrades: bool,
    make_client: Callable[..., BillingClient],
    db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    logs: pytest.LogCaptureFixture,
) -> None:
    client = make_client(db_path, StatusTransport(ok(effective_status="BLOCK_AI")))
    assert client.reconcile_status_once() is True
    assert client.should_block_ai() is True
    Fault(monkeypatch, "read_status", lambda: sqlite_error(name))

    assert client.should_block_ai() is False
    assert "reading the effective status failed: OperationalError" in warnings(logs)
    assert client.store_available is (not degrades)
    assert client.current_status() is None


def test_degraded_store_is_not_read(
    make_client: Callable[..., BillingClient], db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = make_client(db_path, StatusTransport(ok(effective_status="BLOCK_AI")))
    assert client.reconcile_status_once() is True
    fault = Fault(monkeypatch, "read_status", lambda: sqlite_error("SQLITE_CORRUPT"))
    assert client.should_block_ai() is False
    assert not client.store_available
    calls = fault.calls

    assert client.should_block_ai() is False
    assert client.current_status() is None
    assert fault.calls == calls


def test_current_status_shape(
    make_client: Callable[..., BillingClient], db_path: Path, clock: FakeClock
) -> None:
    clock.now = T0 + 0.5
    client = make_client(
        db_path, StatusTransport(ok(effective_status="BLOCK_AI", status_version=9))
    )
    assert client.reconcile_status_once() is True

    assert client.current_status() == {
        **status_data(effective_status="BLOCK_AI", status_version=9),
        "applied_at": T0 + 0.5,
    }


# --- 降级与存储错误 ---------------------------------------------------------------------


def test_reconcile_while_degraded_reopens_first(
    make_client: Callable[..., BillingClient], tmp_path: Path
) -> None:
    path = tmp_path / "later" / "outbox.db"
    transport = StatusTransport(ok(status_version=4))
    client = make_client(path, transport)
    assert not client.store_available

    assert client.reconcile_status_once() is False
    assert transport.get_count == 0
    assert client.next_reconcile_at == T0 + 30

    path.parent.mkdir()
    assert client.reconcile_status_once() is True
    assert client.store_available
    assert transport.get_count == 1
    assert status_rows(path)[0]["status_version"] == 4


@pytest.mark.parametrize(
    ("method", "error", "available"),
    [
        ("apply_status", lambda: sqlite_error("SQLITE_BUSY"), True),
        ("apply_status", lambda: sqlite_error("SQLITE_IOERR"), False),
        ("apply_status", lambda: StoreReplacedError("replaced"), True),
        ("record_reconcile_ok", lambda: sqlite_error("SQLITE_LOCKED"), True),
        ("record_reconcile_ok", lambda: sqlite_error("SQLITE_FULL"), False),
    ],
    ids=["apply-busy", "apply-ioerr", "apply-replaced", "ok-locked", "ok-full"],
)
def test_store_error_during_reconcile_is_a_failure(
    method: str,
    error: Callable[[], BaseException],
    available: bool,
    make_client: Callable[..., BillingClient],
    db_path: Path,
    clock: FakeClock,
    monkeypatch: pytest.MonkeyPatch,
    logs: pytest.LogCaptureFixture,
) -> None:
    transport = StatusTransport(ok(status_version=2), ok(status_version=3))
    client = make_client(db_path, transport)
    fault = Fault(monkeypatch, method, error)

    assert client.reconcile_status_once() is False

    assert "status reconcile failed on the store" in warnings(logs)
    # 被替换（同一个文件）时立即重新打开；BUSY / LOCKED 不转降级。
    assert client.store_available is available
    assert client.next_reconcile_at == T0 + 30
    if available:
        assert reconcile_row(db_path)["last_error_at"] == T0
        assert reconcile_row(db_path)["last_ok_at"] is None

    fault.active = False
    clock.now = T0 + 30
    assert client.reconcile_status_once() is True
    assert client.store_available
    assert client.current_status()["status_version"] in (2, 3)
    assert reconcile_row(db_path)["last_ok_at"] == T0 + 30


def test_recording_the_failure_may_fail_too(
    make_client: Callable[..., BillingClient], db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = make_client(db_path, StatusTransport(UNAVAILABLE))
    Fault(monkeypatch, "record_reconcile_error", lambda: sqlite_error("SQLITE_IOERR"))

    assert client.reconcile_status_once() is False
    assert not client.store_available


def test_reset_status(
    make_client: Callable[..., BillingClient], db_path: Path, clock: FakeClock
) -> None:
    transport = StatusTransport(
        ok(effective_status="BLOCK_AI", status_version=50), ok(status_version=3)
    )
    client = make_client(db_path, transport)
    assert client.reconcile_status_once() is True
    assert client.should_block_ai() is True

    with pytest.raises(ValueError):
        client.reset_status(reason="   ")
    assert client.should_block_ai() is True

    assert client.reset_status(reason="center restored from backup") is True
    assert client.should_block_ai() is False
    assert client.reset_status(reason="again") is False
    [op] = ops_rows(db_path)
    assert (op["action"], op["target_id"]) == ("RESET_STATUS", f"{TENANT}/{PROJECT}")

    assert client.reconcile_status_once() is True
    assert client.current_status()["status_version"] == 3


# --- 节奏 -------------------------------------------------------------------------------


def test_next_reconcile_at_is_none_before_start_and_manual_calls(
    make_client: Callable[..., BillingClient], db_path: Path
) -> None:
    client = make_client(db_path, StatusTransport(ok()))
    assert client.next_reconcile_at is None
    assert client.should_block_ai() is False
    assert client.next_reconcile_at is None


@pytest.mark.usefixtures("fast_wakeups")
def test_reconcile_cadence_seven_steps(
    make_client: Callable[..., BillingClient], db_path: Path, clock: FakeClock
) -> None:
    transport = StatusTransport(
        ok(status_version=1),
        UNAVAILABLE,
        UNAVAILABLE,
        UNAVAILABLE,
        ok(status_version=2),
        UNAVAILABLE,
    )
    client = make_client(db_path, transport)
    watch = TickWatch(client, transport)

    client.start()
    # 第一次：start 后立即（时钟没动）。
    delays = [240, 30, 240, 240, 240, 30]
    for n, delay in enumerate(delays, start=1):
        watch.wait(lambda ended, n=n: n in ended)
        assert transport.get_count == n
        assert client.next_reconcile_at == clock.now + delay
        if n == len(delays):
            break
        # 没到时刻：醒来几次都不对账。
        seen = watch.count()
        watch.wait(lambda ended, seen=seen: len(ended) >= seen + 3)
        assert transport.get_count == n
        clock.now = client.next_reconcile_at

    assert client.stop(timeout=2) is True
    assert transport.gets[0][1]["X-Acuven-Timestamp"] == str(int(T0))
    assert client.current_status()["status_version"] == 2


@pytest.mark.usefixtures("fast_wakeups")
def test_manual_calls_share_the_cadence_and_start_resets_it(
    make_client: Callable[..., BillingClient], db_path: Path, clock: FakeClock
) -> None:
    transport = StatusTransport(UNAVAILABLE, UNAVAILABLE, UNAVAILABLE, UNAVAILABLE)
    client = make_client(db_path, transport)

    # start 之前的手动调用：本段连续失败的第一次补查 30 秒，之后 240 秒。
    assert client.reconcile_status_once() is False
    assert client.next_reconcile_at == T0 + 30
    clock.now = T0 + 30
    assert client.reconcile_status_once() is False
    assert client.next_reconcile_at == T0 + 270

    # start 重置为立即对账，并重置补查标记：这次失败又是 30 秒。
    clock.now = T0 + 100
    watch = TickWatch(client, transport)
    client.start()
    watch.wait(lambda ended: 3 in ended)
    assert client.next_reconcile_at == T0 + 130

    # 线程运行中的手动调用照样推进同一份节奏。
    assert client.reconcile_status_once() is False
    assert client.next_reconcile_at == T0 + 340
    assert client.stop(timeout=2) is True


def test_manual_call_waits_for_the_thread_round(
    make_client: Callable[..., BillingClient], db_path: Path
) -> None:
    entered, release = threading.Event(), threading.Event()

    def blocked() -> Response:
        entered.set()
        release.wait(5)
        return ok(status_version=1)

    transport = StatusTransport(blocked, ok(status_version=2))
    client = make_client(db_path, transport)
    lock = ContendedLock()
    client._reconcile_lock = lock  # type: ignore[assignment]

    client.start()
    assert entered.wait(2)
    outcome: dict[str, bool] = {}
    other = threading.Thread(
        target=lambda: outcome.setdefault("ok", client.reconcile_status_once()), daemon=True
    )
    other.start()
    assert lock.contended.wait(2)
    assert transport.get_count == 1

    release.set()
    other.join(2)
    assert not other.is_alive()
    assert client.stop(timeout=2) is True

    assert outcome == {"ok": True}
    assert transport.get_count == 2
    assert transport.max_in_flight == 1
    assert client.current_status()["status_version"] == 2


@pytest.mark.usefixtures("fast_wakeups")
def test_reconcile_is_not_held_up_by_delivery(
    make_client: Callable[..., BillingClient], db_path: Path, clock: FakeClock
) -> None:
    posting, release = threading.Event(), threading.Event()

    def stuck_post() -> Response:
        posting.set()
        release.wait(5)
        return UNAVAILABLE

    transport = StatusTransport(ok(status_version=1), ok(status_version=2), post=stuck_post)
    client = make_client(db_path, transport)
    llm(client)
    watch = TickWatch(client, transport)

    client.start()
    assert posting.wait(2)
    watch.wait(lambda ended: 1 in ended)

    clock.now = client.next_reconcile_at
    watch.wait(lambda ended: 2 in ended)
    # 投递一轮仍卡着。
    assert not release.is_set()
    assert transport.posts == 1
    assert client.current_status()["status_version"] == 2

    release.set()
    assert client.stop(timeout=2) is True


@pytest.mark.usefixtures("fast_wakeups")
def test_thread_skips_while_the_store_is_unavailable(
    make_client: Callable[..., BillingClient], tmp_path: Path
) -> None:
    path = tmp_path / "later" / "outbox.db"
    transport = StatusTransport(ok(status_version=6))
    client = make_client(path, transport, delivery_interval_seconds=3600)
    watch = TickWatch(client, transport)

    client.start()
    watch.wait(lambda ended: len(ended) >= 3)
    # 跳过的轮不算失败：不发请求、时刻保持为 start 时（已到期）。
    assert transport.get_count == 0
    assert client.next_reconcile_at == T0

    path.parent.mkdir()
    client.deliver_once()
    assert client.store_available
    watch.wait(lambda ended: 1 in ended)

    assert client.stop(timeout=2) is True
    assert client.next_reconcile_at == T0 + 240
    assert reconcile_row(path)["last_ok_at"] == T0
    assert client.current_status()["status_version"] == 6


@pytest.mark.usefixtures("fast_wakeups")
def test_reconcile_thread_survives_an_exception(
    make_client: Callable[..., BillingClient], db_path: Path, logs: pytest.LogCaptureFixture
) -> None:
    transport = StatusTransport(ok())
    client = make_client(db_path, transport)
    original = client._reconcile_tick
    calls = 0

    def flaky() -> float:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("tick bug")
        return original()

    client._reconcile_tick = flaky  # type: ignore[method-assign]

    client.start()
    assert transport.got.wait(2)
    assert client.stop(timeout=2) is True

    assert "status reconcile round failed: RuntimeError" in logs.text
    assert transport.get_count == 1


def test_delivery_does_not_reconcile(
    make_client: Callable[..., BillingClient], db_path: Path
) -> None:
    transport = StatusTransport(ok())
    client = make_client(db_path, transport)
    llm(client)

    client.deliver_once()

    assert transport.posts == 1
    assert transport.get_count == 0
    assert client.next_reconcile_at is None


def test_stop_waits_for_both_threads(
    make_client: Callable[..., BillingClient], db_path: Path
) -> None:
    entered, release = threading.Event(), threading.Event()

    def blocked() -> Response:
        entered.set()
        release.wait(5)
        return ok()

    client = make_client(db_path, StatusTransport(blocked))
    client.start()
    assert entered.wait(2)

    # 对账线程卡在请求里：stop 超时返回假。
    assert client.stop(timeout=0.05) is False
    with pytest.raises(RuntimeError):
        client.start()

    reconcile_thread = client._reconcile_thread
    assert reconcile_thread is not None
    assert reconcile_thread.name == client_module.RECONCILE_THREAD_NAME
    release.set()
    assert client.stop(timeout=2) is True
    assert not reconcile_thread.is_alive()
    assert client._thread is None
    assert client._reconcile_thread is None

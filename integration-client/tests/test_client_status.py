"""BillingClient effective-status reconcile and cadence (design gate #571 v24 §2「有效状态」).

覆盖 §7 标 169 的对账各行：对账应用与版本 3 → 5 → 4、先失败后成功、状态应答字段（含多一个
未知字段照常应用）、换项目不沿用旧状态、租户或项目不符、对账失败放行两种、状态表读失败放行、
对账不被投递拖住、对账节奏七步；另有降级时的对账与放行、读写库失败算一次失败、`reset_status`。

SQLite 用 tmp_path 下的文件，HTTP 用注入的假 Transport，时间用注入的时钟；后台线程用
threading.Event / queue 同步，把对账线程每次最多等待的秒数配成 0.01，不 sleep。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import queue
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
    TransportError,
)
from acuven_billing_client import client as client_module
from acuven_billing_client.client import RECONCILE_THREAD_NAME
from acuven_billing_client.delivery import BATCH_PATH
from acuven_billing_client.status import EFFECTIVE_STATUS_PATH

T0 = 1_790_000_000.0
LOGGER = "acuven_billing_client"
BASE_URL = "https://billing.example.com/prefix/"
SECRET = "sk_" + "0" * 64
TENANT = "ten_a"
PROJECT = "prj_a"
FAST = 0.01
EMPTY_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


# --- 替身 -------------------------------------------------------------------------------


class FakeClock:
    def __init__(self, now: float = T0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class Request:
    def __init__(self, method: str, url: str, headers: Any, body: bytes, timeout: float) -> None:
        self.method = method
        self.url = url
        self.headers = dict(headers)
        self.body = body
        self.timeout = timeout


Handler = Callable[[Request], Response]


class FakeTransport:
    """GET goes to `status`; POST goes to `delivery` (network down unless replaced)."""

    def __init__(self, status: Handler, delivery: Handler | None = None) -> None:
        self.status = status
        self.delivery = delivery if delivery is not None else network_down
        self.requests: list[Request] = []
        self.status_calls: queue.Queue[float] = queue.Queue()
        self.clock: FakeClock | None = None

    def send(self, method: str, url: str, headers: Any, body: bytes, timeout: float) -> Response:
        request = Request(method, url, headers, body, timeout)
        self.requests.append(request)
        if method == "GET":
            try:
                return self.status(request)
            finally:
                self.status_calls.put(self.clock.now if self.clock is not None else 0.0)
        return self.delivery(request)

    def gets(self) -> list[Request]:
        return [r for r in self.requests if r.method == "GET"]


def network_down(request: Request) -> Response:
    raise TransportError("network down")


def status_data(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "tenant_id": TENANT,
        "project_id": PROJECT,
        "billing_mode": "PREPAID",
        "billing_status": "ACTIVE",
        "account_status": "ENABLED",
        "project_integration_status": "ENABLED",
        "effective_status": "ALLOW_AI",
        "status_version": 1,
        "reason_code": None,
        "effective_at": "2026-10-09T10:10:00",
    }
    data.update(overrides)
    return data


def block(version: int, **overrides: Any) -> dict[str, Any]:
    return status_data(
        effective_status="BLOCK_AI",
        billing_status="SUSPENDED",
        reason_code="BALANCE_NOT_POSITIVE",
        status_version=version,
        **overrides,
    )


def allow(version: int, **overrides: Any) -> dict[str, Any]:
    return status_data(status_version=version, **overrides)


def ok(data: Any) -> Response:
    return Response(200, json.dumps({"success": True, "data": data, "error": None}).encode())


class Answers:
    """Status handler answering from a list; each item is a data dict or a Response."""

    def __init__(self, *answers: Any) -> None:
        self.answers = list(answers)

    def add(self, *answers: Any) -> None:
        self.answers.extend(answers)

    def __call__(self, request: Request) -> Response:
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        if isinstance(answer, Response):
            return answer
        return ok(answer)


def failing(request: Request) -> Response:
    return Response(503, b'{"error":{"code":"BILLING_UNAVAILABLE"},"retryable":true}')


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
        self.calls = 0
        original = getattr(Store, method)
        fault = self

        def replacement(store: Store, *args: Any, **kwargs: Any) -> Any:
            if fault.active:
                fault.calls += 1
                raise make_error()
            return original(store, *args, **kwargs)

        monkeypatch.setattr(Store, method, replacement)


# --- 库 ---------------------------------------------------------------------------------


def query(path: Path, sql: str, *args: Any) -> list[dict[str, Any]]:
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql, args)]
    finally:
        conn.close()


def status_rows(path: Path) -> list[dict[str, Any]]:
    return query(path, "SELECT * FROM status_state ORDER BY tenant_id, project_id")


def reconcile_row(path: Path) -> dict[str, Any] | None:
    found = query(
        path,
        "SELECT last_ok_at, last_error_at FROM reconcile_state"
        " WHERE tenant_id = ? AND project_id = ?",
        TENANT,
        PROJECT,
    )
    return found[0] if found else None


def insert_status(path: Path, tenant: str, project: str, effective: str, version: int) -> None:
    conn = sqlite3.connect(path, isolation_level=None)
    try:
        conn.execute(
            "INSERT INTO status_state (tenant_id, project_id, billing_mode, billing_status,"
            " account_status, project_integration_status, effective_status, status_version,"
            " reason_code, effective_at, applied_at) VALUES (?, ?, 'PREPAID', 'SUSPENDED',"
            " 'ENABLED', 'ENABLED', ?, ?, NULL, '2026-10-01T00:00:00', ?)",
            (tenant, project, effective, version, T0 - 100),
        )
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


def threads_named(name: str) -> int:
    return sum(1 for thread in threading.enumerate() if thread.name == name)


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

    def make(path: Path, transport: FakeTransport, **overrides: Any) -> BillingClient:
        transport.clock = clock
        client = BillingClient(
            make_config(path, **overrides), transport=transport, clock=clock, rng=random.Random(0)
        )
        made.append(client)
        return client

    yield make
    for client in made:
        client.close(timeout=2)


@pytest.fixture
def fast_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(client_module, "RECONCILE_WAIT_MAX_SECONDS", FAST)


@pytest.fixture
def logs(caplog: pytest.LogCaptureFixture) -> pytest.LogCaptureFixture:
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    return caplog


def warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]


def wait_round(client: BillingClient) -> None:
    """Wait until the reconcile round in progress (if any) has finished, schedule included."""
    with client._reconcile_lock:
        pass


# --- 请求 -------------------------------------------------------------------------------


def test_reconcile_signs_a_get_with_an_empty_body(
    make_client: Callable[..., BillingClient], db_path: Path
) -> None:
    transport = FakeTransport(Answers(allow(1)))
    client = make_client(db_path, transport, http_timeout_seconds=7)

    assert client.reconcile_status_once() is True

    (request,) = transport.gets()
    assert request.url == "https://billing.example.com/prefix" + EFFECTIVE_STATUS_PATH
    assert request.body == b""
    assert request.timeout == 7
    assert request.headers["X-Acuven-Api-Key"] == "ak_test"
    assert request.headers["X-Acuven-Key-Version"] == "3"
    assert request.headers["X-Acuven-Timestamp"] == str(int(T0))
    assert "Content-Type" not in request.headers
    # 签名覆盖 GET、含前缀的路径、时间戳、请求 id 与空请求体的 SHA-256。
    canonical = "\n".join(
        (
            "GET",
            "/prefix" + EFFECTIVE_STATUS_PATH,
            str(int(T0)),
            request.headers["X-Acuven-Request-Id"],
            EMPTY_SHA256,
        )
    )
    expected = hmac.new(SECRET.encode(), canonical.encode(), hashlib.sha256).hexdigest()
    assert request.headers["X-Acuven-Signature"] == expected


# --- 对账应用与版本 ---------------------------------------------------------------------


def test_reconcile_versions_3_5_4(
    make_client: Callable[..., BillingClient], db_path: Path, clock: FakeClock
) -> None:
    transport = FakeTransport(Answers(allow(3), block(5), allow(4)))
    client = make_client(db_path, transport)

    assert client.reconcile_status_once() is True
    assert client.should_block_ai() is False
    assert reconcile_row(db_path) == {"last_ok_at": T0, "last_error_at": None}

    clock.now = T0 + 240
    assert client.reconcile_status_once() is True
    assert client.should_block_ai() is True
    assert status_rows(db_path)[0]["status_version"] == 5
    assert status_rows(db_path)[0]["applied_at"] == T0 + 240

    clock.now = T0 + 480
    # 版本 4 被忽略，但对账仍算成功、照样写 last_ok_at。
    assert client.reconcile_status_once() is True
    (row,) = status_rows(db_path)
    assert row["status_version"] == 5
    assert row["effective_status"] == "BLOCK_AI"
    assert row["applied_at"] == T0 + 240
    assert reconcile_row(db_path) == {"last_ok_at": T0 + 480, "last_error_at": None}
    assert client.should_block_ai() is True


def test_first_failure_then_success(
    make_client: Callable[..., BillingClient], db_path: Path, clock: FakeClock
) -> None:
    transport = FakeTransport(Answers(Response(503, b"{}"), block(2)))
    client = make_client(db_path, transport)

    assert client.reconcile_status_once() is False
    # 失败只写 last_error_at；status_state 没有行。
    assert reconcile_row(db_path) == {"last_ok_at": None, "last_error_at": T0}
    assert status_rows(db_path) == []
    assert client.current_status() is None
    assert client.should_block_ai() is False

    clock.now = T0 + 30
    assert client.reconcile_status_once() is True
    assert reconcile_row(db_path) == {"last_ok_at": T0 + 30, "last_error_at": T0}
    assert client.should_block_ai() is True


def test_current_status_returns_the_ten_fields_and_applied_at(
    make_client: Callable[..., BillingClient], db_path: Path
) -> None:
    client = make_client(db_path, FakeTransport(Answers(block(7))))
    assert client.current_status() is None

    assert client.reconcile_status_once() is True

    assert client.current_status() == {**block(7), "applied_at": T0}


# --- 状态应答字段 -----------------------------------------------------------------------


def _without(name: str) -> dict[str, Any]:
    data = status_data()
    del data[name]
    return data


BAD_ANSWERS: list[tuple[str, Any]] = [
    *[(f"missing {name}", _without(name)) for name in status_data()],
    *[
        (f"{name} not a string", status_data(**{name: 1}))
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
    *[(f"{name} null", status_data(**{name: None})) for name in ("billing_mode", "effective_at")],
    ("reason_code a number", status_data(reason_code=5)),
    ("status_version a string", status_data(status_version="5")),
    ("status_version a float", status_data(status_version=5.0)),
    ("status_version a bool", status_data(status_version=True)),
    ("status_version negative", status_data(status_version=-1)),
    ("status_version above 2^63 - 1", status_data(status_version=2**63)),
    ("status_version far above", status_data(status_version=10**30)),
    ("effective_status unknown", status_data(effective_status="MAYBE")),
    ("effective_status lowercase", status_data(effective_status="block_ai")),
    ("effective_status null", status_data(effective_status=None)),
    ("tenant mismatch", status_data(tenant_id="ten_other")),
    ("project mismatch", status_data(project_id="prj_other")),
    ("data a list", [status_data()]),
    ("data null", None),
    ("HTTP 500", Response(500, json.dumps({"data": block(9)}).encode())),
    ("HTTP 202", Response(202, json.dumps({"data": block(9)}).encode())),
    ("not JSON", Response(200, b"<html>")),
    ("no data key", Response(200, json.dumps(block(9)).encode())),
    ("top-level array", Response(200, b"[]")),
    ("network error", TransportError("down")),
    ("transport bug", RuntimeError("bug")),
]


@pytest.mark.parametrize("answer", [a for _, a in BAD_ANSWERS], ids=[n for n, _ in BAD_ANSWERS])
def test_invalid_answers_are_failures(
    answer: Any,
    make_client: Callable[..., BillingClient],
    db_path: Path,
    logs: pytest.LogCaptureFixture,
) -> None:
    client = make_client(db_path, FakeTransport(Answers(answer)))

    assert client.reconcile_status_once() is False

    assert status_rows(db_path) == []
    assert reconcile_row(db_path) == {"last_ok_at": None, "last_error_at": T0}
    assert any("reconcile failed" in message for message in warnings(logs))
    assert client.next_reconcile_at == T0 + 30
    assert client.store_available


def test_invalid_answer_keeps_the_applied_status(
    make_client: Callable[..., BillingClient], db_path: Path, clock: FakeClock
) -> None:
    client = make_client(db_path, FakeTransport(Answers(block(4), allow(9, project_id="prj_x"))))
    assert client.reconcile_status_once() is True

    clock.now = T0 + 240
    assert client.reconcile_status_once() is False

    (row,) = status_rows(db_path)
    assert (row["status_version"], row["effective_status"]) == (4, "BLOCK_AI")
    assert client.should_block_ai() is True


def test_unknown_field_is_ignored_and_not_stored(
    make_client: Callable[..., BillingClient], db_path: Path
) -> None:
    client = make_client(db_path, FakeTransport(Answers(block(3, new_field="x", unit_cost=1))))

    assert client.reconcile_status_once() is True

    (row,) = status_rows(db_path)
    assert set(row) == {*status_data(), "applied_at"}
    assert client.current_status() == {**block(3), "applied_at": T0}
    assert client.should_block_ai() is True


def test_unknown_values_of_other_string_fields_are_accepted(
    make_client: Callable[..., BillingClient], db_path: Path
) -> None:
    data = allow(2, billing_mode="SOMETHING_NEW", account_status="", reason_code="NEW_REASON")
    client = make_client(db_path, FakeTransport(Answers(data)))

    assert client.reconcile_status_once() is True
    assert client.current_status() == {**data, "applied_at": T0}


def test_largest_status_version_is_accepted(
    make_client: Callable[..., BillingClient], db_path: Path
) -> None:
    client = make_client(db_path, FakeTransport(Answers(block(2**63 - 1))))

    assert client.reconcile_status_once() is True
    assert status_rows(db_path)[0]["status_version"] == 2**63 - 1


# --- 换项目、租户或项目不符 -------------------------------------------------------------


def test_new_project_does_not_inherit_old_status(
    make_client: Callable[..., BillingClient], db_path: Path, clock: FakeClock
) -> None:
    Store(db_path, clock=clock).close()
    insert_status(db_path, "ten_old", "prj_old", "BLOCK_AI", 99)
    client = make_client(db_path, FakeTransport(Answers(allow(1))))
    # 旧那一对的行不被读到。
    assert client.should_block_ai() is False
    assert client.current_status() is None

    assert client.reconcile_status_once() is True

    assert client.should_block_ai() is False
    rows = {(r["tenant_id"], r["project_id"]): r for r in status_rows(db_path)}
    assert rows[(TENANT, PROJECT)]["status_version"] == 1
    assert rows[(TENANT, PROJECT)]["effective_status"] == "ALLOW_AI"
    old = rows[("ten_old", "prj_old")]
    assert (old["status_version"], old["effective_status"], old["applied_at"]) == (
        99,
        "BLOCK_AI",
        T0 - 100,
    )


@pytest.mark.parametrize("field", ["tenant_id", "project_id"])
def test_tenant_or_project_mismatch_is_not_applied(
    field: str, make_client: Callable[..., BillingClient], db_path: Path
) -> None:
    client = make_client(db_path, FakeTransport(Answers(block(5, **{field: "other"}))))

    assert client.reconcile_status_once() is False

    assert status_rows(db_path) == []
    assert client.should_block_ai() is False


# --- 对账失败放行 -----------------------------------------------------------------------


def test_never_reconciled_allows_ai(
    make_client: Callable[..., BillingClient], db_path: Path, clock: FakeClock
) -> None:
    client = make_client(db_path, FakeTransport(failing))
    for step in range(3):
        clock.now = T0 + 240 * step
        assert client.reconcile_status_once() is False
        assert client.should_block_ai() is False


def test_block_survives_later_failures(
    make_client: Callable[..., BillingClient], db_path: Path, clock: FakeClock
) -> None:
    answers = Answers(block(5))
    client = make_client(db_path, FakeTransport(answers))
    assert client.reconcile_status_once() is True

    answers.add(Response(503, b""), TransportError("down"), allow(5), allow(1))
    for step in range(1, 5):
        clock.now = T0 + 240 * step
        client.reconcile_status_once()
        assert client.should_block_ai() is True


# --- 状态表读失败放行 -------------------------------------------------------------------


@pytest.mark.parametrize(("name", "degrades"), [("SQLITE_IOERR", True), ("SQLITE_BUSY", False)])
def test_status_read_failure_allows_ai(
    name: str,
    degrades: bool,
    make_client: Callable[..., BillingClient],
    db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    logs: pytest.LogCaptureFixture,
) -> None:
    client = make_client(db_path, FakeTransport(Answers(block(5))))
    assert client.reconcile_status_once() is True
    assert client.should_block_ai() is True
    Fault(monkeypatch, "read_status", lambda: sqlite_error(name))

    assert client.should_block_ai() is False
    assert any("allowing AI" in message for message in warnings(logs))
    assert client.current_status() is None
    assert client.store_available is (not degrades)


def test_no_row_logs_a_warning(
    make_client: Callable[..., BillingClient], db_path: Path, logs: pytest.LogCaptureFixture
) -> None:
    client = make_client(db_path, FakeTransport(failing))

    assert client.should_block_ai() is False
    assert any("no effective status" in message for message in warnings(logs))


# --- 降级 -------------------------------------------------------------------------------


def test_degraded_should_block_ai_does_not_touch_the_store(
    make_client: Callable[..., BillingClient],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = make_client(tmp_path / "absent" / "outbox.db", FakeTransport(Answers(block(5))))
    assert not client.store_available
    reads = Fault(monkeypatch, "read_status", lambda: AssertionError("store read"))

    assert client.should_block_ai() is False
    assert client.current_status() is None
    assert reads.calls == 0


def test_degraded_reconcile_reopens_first(
    make_client: Callable[..., BillingClient], tmp_path: Path, clock: FakeClock
) -> None:
    path = tmp_path / "later" / "outbox.db"
    transport = FakeTransport(Answers(block(5)))
    client = make_client(path, transport)
    assert client.next_reconcile_at is None

    # 仍打不开：算一次失败，不发请求。
    assert client.reconcile_status_once() is False
    assert transport.gets() == []
    assert client.next_reconcile_at == T0 + 30

    path.parent.mkdir()
    clock.now = T0 + 30
    assert client.reconcile_status_once() is True
    assert client.store_available
    assert client.should_block_ai() is True
    assert client.next_reconcile_at == T0 + 270


@pytest.mark.parametrize(
    ("method", "name", "degrades"),
    [
        ("apply_status", "SQLITE_IOERR", True),
        ("apply_status", "SQLITE_BUSY", False),
        ("record_reconcile_ok", "SQLITE_FULL", True),
    ],
)
def test_store_error_during_reconcile_is_a_failure(
    method: str,
    name: str,
    degrades: bool,
    make_client: Callable[..., BillingClient],
    db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    logs: pytest.LogCaptureFixture,
) -> None:
    client = make_client(db_path, FakeTransport(Answers(block(5))))
    Fault(monkeypatch, method, lambda: sqlite_error(name))

    assert client.reconcile_status_once() is False

    # 先尽力写 last_error_at，再转入降级。
    assert reconcile_row(db_path) == {"last_ok_at": None, "last_error_at": T0}
    assert client.store_available is (not degrades)
    assert client.next_reconcile_at == T0 + 30
    assert any("store error" in message for message in warnings(logs))
    if method == "apply_status":
        assert status_rows(db_path) == []


def test_record_error_failure_is_swallowed(
    make_client: Callable[..., BillingClient], db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = make_client(db_path, FakeTransport(failing))
    Fault(monkeypatch, "record_reconcile_error", lambda: sqlite_error("SQLITE_IOERR"))

    assert client.reconcile_status_once() is False
    assert not client.store_available


# --- 状态重置 ---------------------------------------------------------------------------


def test_reset_status_lets_the_next_reconcile_apply_a_lower_version(
    make_client: Callable[..., BillingClient], db_path: Path, clock: FakeClock
) -> None:
    client = make_client(db_path, FakeTransport(Answers(block(50), allow(3), allow(3))))
    assert client.reconcile_status_once() is True
    clock.now = T0 + 240
    assert client.reconcile_status_once() is True
    assert client.should_block_ai() is True

    with pytest.raises(ValueError):
        client.reset_status(reason="  ")
    assert client.reset_status(reason="center restored from backup") is True
    assert client.should_block_ai() is False
    ops = query(db_path, "SELECT action, target_id, reason FROM ops_log")
    assert ops == [
        {
            "action": "RESET_STATUS",
            "target_id": f"{TENANT}/{PROJECT}",
            "reason": "center restored from backup",
        }
    ]
    # reconcile_state 不动。
    assert reconcile_row(db_path) == {"last_ok_at": T0 + 240, "last_error_at": None}

    clock.now = T0 + 480
    assert client.reconcile_status_once() is True
    assert status_rows(db_path)[0]["status_version"] == 3
    assert client.reset_status(reason="again") is True
    assert client.reset_status(reason="nothing left") is False


# --- 节奏 -------------------------------------------------------------------------------


def test_manual_cadence(
    make_client: Callable[..., BillingClient], db_path: Path, clock: FakeClock
) -> None:
    answers = Answers()
    client = make_client(db_path, FakeTransport(answers))
    assert client.next_reconcile_at is None

    expected: list[tuple[Any, float]] = [
        (allow(1), 240),
        (Response(503, b""), 30),
        (Response(503, b""), 240),
        (Response(503, b""), 240),
        (allow(2), 240),
        (Response(503, b""), 30),
        (Response(503, b""), 240),
    ]
    for answer, delay in expected:
        answers.add(answer)
        client.reconcile_status_once()
        assert client.next_reconcile_at == clock.now + delay
        clock.now = client.next_reconcile_at


def test_background_cadence_seven_steps(
    make_client: Callable[..., BillingClient], db_path: Path, clock: FakeClock, fast_wait: None
) -> None:
    fail = Response(503, b"")
    answers = Answers(allow(1), fail, fail, fail, allow(2), fail)
    transport = FakeTransport(answers)
    client = make_client(db_path, transport, delivery_interval_seconds=FAST)

    client.start()
    # 第一次：start() 后立即；之后每一步的间隔按上一次的结果。
    assert transport.status_calls.get(timeout=5) == T0
    called_at = T0
    delays = (240, 30, 240, 240, 240, 30)
    for index, delay in enumerate(delays):
        wait_round(client)
        assert client.next_reconcile_at == called_at + delay
        assert transport.status_calls.empty()
        if index == len(delays) - 1:
            break
        clock.now = called_at + delay
        called_at = transport.status_calls.get(timeout=5)
        assert called_at == clock.now
    assert client.stop(timeout=5) is True
    assert len(transport.gets()) == 6
    assert threads_named(RECONCILE_THREAD_NAME) == 0


def test_manual_and_background_share_the_schedule(
    make_client: Callable[..., BillingClient], db_path: Path, clock: FakeClock, fast_wait: None
) -> None:
    fail = Response(503, b"")
    answers = Answers(fail, fail, allow(1))
    transport = FakeTransport(answers)
    client = make_client(db_path, transport, delivery_interval_seconds=FAST)

    # start() 之前的手动对账：失败、用掉这一段的补查。
    assert client.reconcile_status_once() is False
    assert client.next_reconcile_at == T0 + 30

    # start() 重置为立即对账，并重置「已补查过」的标记：再失败仍然 30 秒补查。
    client.start()
    transport.status_calls.get(timeout=5)
    wait_round(client)
    assert client.next_reconcile_at == T0 + 30

    clock.now = T0 + 10
    # 手动调用不论节奏，立即做一轮；成功后后台线程按它算出的时刻继续。
    assert client.reconcile_status_once() is True
    assert client.next_reconcile_at == T0 + 250
    assert client.stop(timeout=5) is True


def test_manual_call_waits_for_the_running_round(
    make_client: Callable[..., BillingClient], db_path: Path, clock: FakeClock, fast_wait: None
) -> None:
    entered, release = threading.Event(), threading.Event()
    calls: list[str] = []

    def handler(request: Request) -> Response:
        calls.append("enter")
        if len(calls) == 1:
            entered.set()
            release.wait(5)
        calls.append("leave")
        return ok(allow(len(calls)))

    transport = FakeTransport(handler)
    client = make_client(db_path, transport, delivery_interval_seconds=FAST)
    client.start()
    assert entered.wait(5)

    outcome: dict[str, bool] = {}
    manual = threading.Thread(
        target=lambda: outcome.setdefault("ok", client.reconcile_status_once())
    )
    manual.start()
    manual.join(0.05)
    # 手动调用在等后台线程这一轮做完。
    assert manual.is_alive()
    release.set()
    manual.join(5)

    assert outcome == {"ok": True}
    assert calls == ["enter", "leave", "enter", "leave"]
    assert client.stop(timeout=5) is True


def test_thread_skips_due_rounds_while_degraded(
    make_client: Callable[..., BillingClient],
    tmp_path: Path,
    clock: FakeClock,
    fast_wait: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "later" / "outbox.db"
    transport = FakeTransport(Answers(block(5)))
    client = make_client(path, transport, delivery_interval_seconds=FAST)
    ticks = threading.Semaphore(0)
    original = BillingClient._reconcile_tick

    def counting(self: BillingClient) -> None:
        original(self)
        ticks.release()

    monkeypatch.setattr(BillingClient, "_reconcile_tick", counting)

    client.start()
    for _ in range(3):
        assert ticks.acquire(timeout=5)
    # 跳过的轮不算失败、不发请求，next_reconcile_at 保持原值（已到期）。
    assert transport.gets() == []
    assert client.next_reconcile_at == T0
    assert not client.store_available

    # 存储恢复（投递线程重新打开）后的下一次唤醒立即对账。
    path.parent.mkdir()
    assert transport.status_calls.get(timeout=5) == T0
    wait_round(client)
    assert client.store_available
    assert client.should_block_ai() is True
    assert client.next_reconcile_at == T0 + 240
    assert reconcile_row(path) == {"last_ok_at": T0, "last_error_at": None}
    assert client.stop(timeout=5) is True


def test_reconcile_thread_survives_an_exception(
    make_client: Callable[..., BillingClient],
    db_path: Path,
    clock: FakeClock,
    fast_wait: None,
    monkeypatch: pytest.MonkeyPatch,
    logs: pytest.LogCaptureFixture,
) -> None:
    transport = FakeTransport(Answers(block(5)))
    client = make_client(db_path, transport, delivery_interval_seconds=FAST)
    raised = threading.Event()
    original = BillingClient._reconcile_with

    def flaky(self: BillingClient, epoch: int, store: Store) -> bool:
        if not raised.is_set():
            raised.set()
            raise RuntimeError("bug")
        return original(self, epoch, store)

    monkeypatch.setattr(BillingClient, "_reconcile_with", flaky)

    client.start()
    # 异常不让线程退出；节奏没变（仍到期），下一次唤醒照常对账。
    transport.status_calls.get(timeout=5)
    wait_round(client)
    assert raised.is_set()
    assert "status reconcile round failed: RuntimeError" in logs.text
    assert client.should_block_ai() is True
    assert client.stop(timeout=5) is True


# --- 对账不被投递拖住 -------------------------------------------------------------------


def test_reconcile_is_not_held_up_by_delivery(
    make_client: Callable[..., BillingClient], db_path: Path, clock: FakeClock, fast_wait: None
) -> None:
    posting, release = threading.Event(), threading.Event()

    def stuck_delivery(request: Request) -> Response:
        assert urlsplit(request.url).path.endswith(BATCH_PATH)
        posting.set()
        release.wait(10)
        raise TransportError("released")

    transport = FakeTransport(Answers(allow(1), block(2)), stuck_delivery)
    client = make_client(db_path, transport, delivery_interval_seconds=FAST)
    client.record_llm_usage(
        provider="openai",
        model="gpt-4o",
        usage_type="llm.chat",
        input_tokens=10,
        output_tokens=5,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
        request_id="req-1",
    )

    try:
        client.start()
        assert posting.wait(5)
        assert transport.status_calls.get(timeout=5) == T0
        wait_round(client)

        # 投递仍卡着；到了下一次对账时刻，对账线程照常发出请求。
        clock.now = T0 + 240
        assert transport.status_calls.get(timeout=5) == T0 + 240
        wait_round(client)
        assert not release.is_set()
        assert client.should_block_ai() is True
    finally:
        release.set()
    assert client.stop(timeout=5) is True

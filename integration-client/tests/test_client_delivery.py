"""One delivery round (design gate #571 v24 §2「投递」, §3, §5, §7 rows marked 167).

SQLite 用 tmp_path 下的文件；HTTP 用注入的假 Transport（按请求路径与事件分派回执）；
时间用注入的时钟，Store 与投递共用同一个；退避的随机数用固定比例的替身。载荷是测试里
直接构造的字节，不经 events.py。
"""

from __future__ import annotations

import itertools
import json
import logging
import random
import sqlite3
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest
from acuven_billing_client import (
    BillingClientConfig,
    DeliverySummary,
    Response,
    Store,
    TransportError,
    run_delivery_round,
)
from acuven_billing_client.delivery import (
    BATCH_PATH,
    DEAD_LETTER_CODES,
    SINGLE_PATH,
    backoff_seconds,
    batch_body,
    next_attempt_at,
)
from acuven_billing_client.signing import canonical_request, sign

T0 = 1_790_000_000.0
DAY = 86_400.0
BASE_URL = "https://billing.example.com"
SECRET = "sk_" + "0" * 64
AUTH = "INTEGRATION_AUTH_FAILED"
MARKER = "payload-marker-must-not-be-logged"


# --- 替身 -------------------------------------------------------------------------------


class FakeClock:
    def __init__(self, now: float = T0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class FixedRng:
    """`uniform(a, b)` returns `a + (b - a) * fraction`; records every call."""

    def __init__(self, fraction: float = 0.0) -> None:
        self.fraction = fraction
        self.calls: list[tuple[float, float]] = []

    def uniform(self, a: float, b: float) -> float:
        self.calls.append((a, b))
        return a + (b - a) * self.fraction


@dataclass
class Sent:
    method: str
    url: str
    headers: dict[str, str]
    body: bytes
    timeout: float

    @property
    def path(self) -> str:
        return urlsplit(self.url).path

    @property
    def is_batch(self) -> bool:
        return self.path.endswith(BATCH_PATH)

    def event_ids(self) -> list[str]:
        document = json.loads(self.body)
        if self.is_batch:
            return [event["event_id"] for event in document["events"]]
        return [document["event_id"]]


Handler = Callable[[Sent], Response | Exception]


class FakeTransport:
    def __init__(self, handler: Handler) -> None:
        self.handler = handler
        self.sent: list[Sent] = []

    def send(self, method: str, url: str, headers: Any, body: bytes, timeout: float) -> Response:
        request = Sent(method, url, dict(headers), body, timeout)
        self.sent.append(request)
        result = self.handler(request)
        if isinstance(result, Exception):
            raise result
        return result

    @property
    def singles(self) -> list[Sent]:
        return [request for request in self.sent if not request.is_batch]


def route(batch: Handler, single: Handler | None = None) -> Handler:
    def handle(request: Sent) -> Response | Exception:
        if request.is_batch:
            return batch(request)
        assert single is not None, "unexpected single-event request"
        return single(request)

    return handle


# --- 回执 -------------------------------------------------------------------------------


def json_response(status: int, document: Any) -> Response:
    return Response(status, json.dumps(document).encode())


def error(status: int, code: Any, retryable: Any) -> Response:
    return json_response(status, {"error": {"code": code, "message": "m"}, "retryable": retryable})


def accepted_item(status: str = "accepted") -> dict[str, Any]:
    return {
        "status": status,
        "processing_status": "RECEIVED",
        "error_code": None,
        "retryable": False,
    }


def rejected_item(code: str, retryable: bool) -> dict[str, Any]:
    return {
        "status": "rejected",
        "processing_status": None,
        "error_code": code,
        "retryable": retryable,
    }


def batch_receipt(request: Sent, items: dict[str, dict[str, Any]] | None = None) -> Response:
    """200 with one result per sent event; events not in `items` are accepted."""
    items = items or {}
    results = [
        {"index": index, "event_id": event_id, **items.get(event_id, accepted_item())}
        for index, event_id in enumerate(request.event_ids())
    ]
    return json_response(200, {"data": {"results": results}, "request_id": "r"})


def receipt(event_id: str, status: str = "accepted", http: int = 202) -> Response:
    return json_response(
        http,
        {"data": {"event_id": event_id, "status": status, "processing_status": "RECEIVED"}},
    )


def accept_all(request: Sent) -> Response:
    if request.is_batch:
        return batch_receipt(request)
    return receipt(request.event_ids()[0])


# --- 夹具与工具 -------------------------------------------------------------------------


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "outbox.db"


@pytest.fixture
def store(db_path: Path, clock: FakeClock) -> Iterator[Store]:
    opened = Store(db_path, clock=clock)
    yield opened
    opened.close()


def make_config(db_path: Path, **overrides: Any) -> BillingClientConfig:
    values: dict[str, Any] = {
        "base_url": BASE_URL,
        "api_key": "ak_test",
        "key_version": 3,
        "secret": SECRET,
        "tenant_id": "ten_a",
        "project_id": "prj_a",
        "outbox_path": str(db_path),
    }
    values.update(overrides)
    return BillingClientConfig(**values)


@pytest.fixture
def config(db_path: Path) -> BillingClientConfig:
    return make_config(db_path)


def event_id(n: int) -> str:
    return f"evt{n:04d}"


def payload_for(eid: str, size: int | None = None) -> bytes:
    head = f'{{"event_id":"{eid}","marker":"{MARKER}","pad":"'.encode()
    tail = b'"}'
    pad = 0 if size is None else size - len(head) - len(tail)
    return head + b"x" * pad + tail


def add_events(store: Store, count: int, *, size: int | None = None) -> list[str]:
    ids = [event_id(n) for n in range(count)]
    for eid in ids:
        store.insert_event(eid, payload_for(eid, size))
    return ids


def raw(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    return conn


def rows(path: Path) -> dict[str, dict[str, Any]]:
    conn = raw(path)
    try:
        return {
            row["event_id"]: dict(row)
            for row in conn.execute("SELECT * FROM usage_outbox ORDER BY event_id")
        }
    finally:
        conn.close()


def execute(path: Path, sql: str, params: tuple[Any, ...] = ()) -> None:
    conn = raw(path)
    try:
        conn.execute(sql, params)
    finally:
        conn.close()


def request_ids() -> Callable[[], str]:
    counter = itertools.count(1)
    return lambda: f"REQ{next(counter):06d}"


def run(
    store: Any,
    transport: FakeTransport,
    config: BillingClientConfig,
    clock: FakeClock,
    rng: Any = None,
    new_request_id: Callable[[], str] | None = None,
) -> DeliverySummary:
    return run_delivery_round(
        store,
        transport,
        config,
        clock=clock,
        rng=rng if rng is not None else FixedRng(),
        new_request_id=new_request_id or request_ids(),
    )


def assert_signed(request: Sent, secret: str, path: str) -> None:
    headers = request.headers
    expected = sign(
        secret,
        canonical_request(
            "POST",
            path,
            headers["X-Acuven-Timestamp"],
            headers["X-Acuven-Request-Id"],
            request.body,
        ),
    )
    assert headers["X-Acuven-Signature"] == expected


# --- 基本形态 ---------------------------------------------------------------------------


def test_summary_zero():
    assert DeliverySummary.zero() == DeliverySummary(
        claimed=0, delivered=0, retried=0, dead=0, fenced=0
    )


def test_empty_round_sends_nothing(store, config, clock, db_path):
    transport = FakeTransport(accept_all)

    summary = run(store, transport, config, clock)

    assert summary == DeliverySummary.zero()
    assert transport.sent == []
    assert store.read_last_delivery_ok_at() is None


def test_batch_request_is_signed_post_with_raw_payloads(store, config, clock):
    ids = add_events(store, 3)
    transport = FakeTransport(accept_all)

    run(store, transport, config, clock)

    [request] = transport.sent
    assert request.method == "POST"
    assert request.url == BASE_URL + BATCH_PATH
    assert request.timeout == config.http_timeout_seconds
    assert request.body == b'{"events":[' + b",".join(payload_for(e) for e in ids) + b"]}"
    assert request.body == batch_body([payload_for(e) for e in ids])
    assert request.headers["X-Acuven-Api-Key"] == "ak_test"
    assert request.headers["X-Acuven-Key-Version"] == "3"
    assert request.headers["Content-Type"] == "application/json"
    assert request.headers["X-Acuven-Timestamp"] == str(int(T0))
    assert_signed(request, SECRET, BATCH_PATH)


# --- 回执判定：批量 200 ------------------------------------------------------------------


def test_batch_results_mixed(store, config, clock, db_path):
    ids = add_events(store, 6)
    items = {
        ids[0]: accepted_item("accepted"),
        ids[1]: accepted_item("already_received"),
        ids[2]: accepted_item("already_processed"),
        ids[3]: rejected_item("UNKNOWN_USAGE_TYPE", True),
        ids[4]: rejected_item("VALIDATION_ERROR", False),
        ids[5]: rejected_item("SERVICE_UNAVAILABLE", True),
    }
    transport = FakeTransport(lambda request: batch_receipt(request, items))

    summary = run(store, transport, config, clock)

    assert summary == DeliverySummary(claimed=6, delivered=3, retried=2, dead=1, fenced=0)
    state = rows(db_path)
    for eid in ids[:3]:
        assert state[eid]["status"] == "DELIVERED"
        assert state[eid]["delivered_at"] == T0
        assert state[eid]["last_error_code"] is None
    assert state[ids[3]]["status"] == "PENDING"
    assert state[ids[3]]["last_error_code"] == "UNKNOWN_USAGE_TYPE"
    # attempts = 1：2 ** 1 × 0.5。
    assert state[ids[3]]["next_attempt_at"] == T0 + 1
    assert state[ids[4]]["status"] == "DEAD"
    assert state[ids[4]]["last_error_code"] == "VALIDATION_ERROR"
    assert state[ids[4]]["dead_at"] == T0
    assert state[ids[5]]["status"] == "PENDING"
    assert state[ids[5]]["last_error_code"] == "SERVICE_UNAVAILABLE"
    assert store.read_last_delivery_ok_at() == T0
    assert len(transport.sent) == 1


@pytest.mark.parametrize("code", sorted(DEAD_LETTER_CODES))
def test_batch_item_dead_only_for_whitelisted_codes(store, config, clock, db_path, code):
    [eid] = add_events(store, 1)
    transport = FakeTransport(
        lambda request: batch_receipt(request, {eid: rejected_item(code, False)})
    )

    summary = run(store, transport, config, clock)

    assert summary.dead == 1
    assert rows(db_path)[eid]["status"] == "DEAD"
    assert rows(db_path)[eid]["last_error_code"] == code


def test_batch_item_unknown_non_retryable_code_stays_pending(store, config, clock, db_path):
    [eid] = add_events(store, 1)
    items = {eid: rejected_item("SOMETHING_NEW", False)}
    transport = FakeTransport(lambda request: batch_receipt(request, items))

    summary = run(store, transport, config, clock)

    assert summary == DeliverySummary(claimed=1, delivered=0, retried=1, dead=0, fenced=0)
    assert rows(db_path)[eid]["status"] == "PENDING"
    assert rows(db_path)[eid]["last_error_code"] == "SOMETHING_NEW"


def test_batch_items_incomplete_only_affect_those_items(store, config, clock, db_path):
    ids = add_events(store, 8)
    no_retryable = rejected_item("VALIDATION_ERROR", False)
    del no_retryable["retryable"]
    no_code = rejected_item("VALIDATION_ERROR", False)
    del no_code["error_code"]
    no_status = accepted_item()
    del no_status["status"]
    string_retryable = rejected_item("VALIDATION_ERROR", False) | {"retryable": "false"}
    items = {
        ids[1]: accepted_item("weird"),
        ids[2]: no_retryable,
        ids[3]: no_code,
        ids[4]: no_status,
        ids[5]: string_retryable,
        ids[6]: accepted_item() | {"status": ["accepted"]},
        ids[7]: accepted_item("already_processed"),
    }
    transport = FakeTransport(lambda request: batch_receipt(request, items))

    summary = run(store, transport, config, clock)

    assert summary == DeliverySummary(claimed=8, delivered=2, retried=6, dead=0, fenced=0)
    state = rows(db_path)
    assert state[ids[0]]["status"] == "DELIVERED"
    assert state[ids[7]]["status"] == "DELIVERED"
    for eid in ids[1:7]:
        assert state[eid]["status"] == "PENDING"
        assert state[eid]["last_error_code"] == "INVALID_BILLING_RECEIPT"


def _results(request: Sent) -> list[dict[str, Any]]:
    return json.loads(batch_receipt(request).body)["data"]["results"]


def _with_results(results: list[Any]) -> Response:
    return json_response(200, {"data": {"results": results}})


def _swap_index(request: Sent) -> Response:
    results = _results(request)
    results[0]["index"], results[1]["index"] = 1, 0
    return _with_results(results)


def _bool_index(request: Sent) -> Response:
    results = _results(request)
    results[1]["index"] = True
    return _with_results(results)


def _missing_index(request: Sent) -> Response:
    results = _results(request)
    del results[2]["index"]
    return _with_results(results)


def _wrong_event_id(request: Sent) -> Response:
    results = _results(request)
    results[1]["event_id"] = "evt9999"
    return _with_results(results)


def _short(request: Sent) -> Response:
    return _with_results(_results(request)[:-1])


def _item_not_object(request: Sent) -> Response:
    results: list[Any] = _results(request)
    results[0] = "accepted"
    return _with_results(results)


@pytest.mark.parametrize(
    "respond",
    [
        lambda request: Response(200, b"not json"),
        lambda request: Response(200, b""),
        lambda request: json_response(200, []),
        lambda request: json_response(200, {"data": {}}),
        lambda request: json_response(200, {"data": {"results": "x"}}),
        lambda request: json_response(200, {"results": _results(request)}),
        lambda request: Response(200, b"[" * 50_000),
        _short,
        _swap_index,
        _bool_index,
        _missing_index,
        _wrong_event_id,
        _item_not_object,
    ],
)
def test_batch_200_with_bad_structure_retries_whole_batch(store, config, clock, db_path, respond):
    ids = add_events(store, 3)
    transport = FakeTransport(respond)

    summary = run(store, transport, config, clock)

    assert summary == DeliverySummary(claimed=3, delivered=0, retried=3, dead=0, fenced=0)
    for eid in ids:
        assert rows(db_path)[eid]["status"] == "PENDING"
        assert rows(db_path)[eid]["last_error_code"] == "INVALID_BILLING_RECEIPT"


# --- 回执判定：整批失败 -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("result", "code"),
    [
        (error(503, "SERVICE_UNAVAILABLE", True), "SERVICE_UNAVAILABLE"),
        (error(429, "RATE_LIMITED", True), "RATE_LIMITED"),
        (error(413, "PAYLOAD_TOO_LARGE", True), "PAYLOAD_TOO_LARGE"),
        (Response(502, b"<html>bad gateway</html>"), "BILLING_UNAVAILABLE"),
        (Response(500, b'{"error":{"code":"X"}}'), "BILLING_UNAVAILABLE"),
        (error(404, "NOT_FOUND", False), "NOT_FOUND"),
        (error(405, "METHOD_NOT_ALLOWED", False), "METHOD_NOT_ALLOWED"),
        (error(403, "CREDENTIAL_SCOPE_MISMATCH", False), "CREDENTIAL_SCOPE_MISMATCH"),
        (error(422, "SOMETHING_NEW", False), "SOMETHING_NEW"),
        (error(400, "VALIDATION_ERROR", False), "VALIDATION_ERROR"),
        (Response(400, b"bad request"), "INVALID_BILLING_RESPONSE"),
        (Response(404, b'{"error":{"code":5},"retryable":false}'), "INVALID_BILLING_RESPONSE"),
        (Response(404, b'{"error":"NOT_FOUND","retryable":false}'), "INVALID_BILLING_RESPONSE"),
        (
            Response(404, b'{"error":{"code":"NOT_FOUND"},"retryable":"false"}'),
            "INVALID_BILLING_RESPONSE",
        ),
        (Response(202, b'{"data":{}}'), "INVALID_BILLING_RESPONSE"),
        (TransportError("timed out"), "NETWORK_ERROR"),
    ],
)
def test_whole_batch_failure_retries_every_row(store, config, clock, db_path, result, code):
    ids = add_events(store, 3)
    transport = FakeTransport(lambda request: result)

    summary = run(store, transport, config, clock)

    assert summary == DeliverySummary(claimed=3, delivered=0, retried=3, dead=0, fenced=0)
    # 不改单发、不进死信。
    assert len(transport.sent) == 1
    for eid in ids:
        row = rows(db_path)[eid]
        assert row["status"] == "PENDING"
        assert row["last_error_code"] == code
        assert row["attempts"] == 1
        assert row["next_attempt_at"] == T0 + 1
        assert row["lease_until"] is None
    assert store.read_last_delivery_ok_at() is None


# --- 凭据错误 ---------------------------------------------------------------------------


def test_batch_401_stays_pending_with_fixed_60_seconds(store, config, clock, db_path):
    ids = add_events(store, 2)
    rng = FixedRng()
    transport = FakeTransport(lambda request: error(401, AUTH, False))

    summary = run(store, transport, config, clock, rng)

    assert summary == DeliverySummary(claimed=2, delivered=0, retried=2, dead=0, fenced=0)
    for eid in ids:
        assert rows(db_path)[eid]["status"] == "PENDING"
        assert rows(db_path)[eid]["last_error_code"] == AUTH
        assert rows(db_path)[eid]["next_attempt_at"] == T0 + 60
    assert rng.calls == []
    assert store.delivery_counts().auth_failing is True


def test_single_401_in_fallback_stays_pending(store, config, clock, db_path):
    ids = add_events(store, 3)
    transport = FakeTransport(
        route(
            lambda request: error(422, "BATCH_TOO_LARGE", False),
            lambda request: error(401, AUTH, False),
        )
    )

    summary = run(store, transport, config, clock)

    assert summary == DeliverySummary(claimed=3, delivered=0, retried=3, dead=0, fenced=0)
    assert len(transport.singles) == 1
    for eid in ids:
        assert rows(db_path)[eid]["status"] == "PENDING"
        assert rows(db_path)[eid]["last_error_code"] == AUTH
        assert rows(db_path)[eid]["next_attempt_at"] == T0 + 60
    assert store.delivery_counts().auth_failing is True


def test_auth_failure_retries_every_60_seconds_until_credentials_are_fixed(store, clock, db_path):
    good_secret = "sk_" + "2" * 64
    [eid] = add_events(store, 1)

    def server(request: Sent) -> Response:
        headers = request.headers
        expected = sign(
            good_secret,
            canonical_request(
                "POST",
                request.path,
                headers["X-Acuven-Timestamp"],
                headers["X-Acuven-Request-Id"],
                request.body,
            ),
        )
        if headers["X-Acuven-Signature"] != expected:
            return error(401, AUTH, False)
        return batch_receipt(request)

    transport = FakeTransport(server)
    rng = FixedRng(1.0)
    wrong = make_config(db_path, secret="sk_" + "1" * 64)
    for attempt in range(1, 6):
        summary = run(store, transport, wrong, clock, rng)
        row = rows(db_path)[eid]
        assert summary.retried == 1
        assert row["attempts"] == attempt
        assert row["status"] == "PENDING"
        assert row["last_error_code"] == AUTH
        # 第 5 次也是 now + 60，不按指数退避。
        assert row["next_attempt_at"] == clock.now + 60
        clock.now += 60
    assert rng.calls == []
    assert store.delivery_counts().auth_failing is True

    fixed = make_config(db_path, secret=good_secret)
    summary = run(store, transport, fixed, clock, rng)

    assert summary.delivered == 1
    row = rows(db_path)[eid]
    assert row["status"] == "DELIVERED"
    assert row["last_error_code"] is None
    assert row["attempts"] == 6
    assert store.delivery_counts().auth_failing is False


# --- 批层不可重试改单发 -----------------------------------------------------------------


@pytest.mark.parametrize(
    "batch_error",
    [
        error(413, "PAYLOAD_TOO_LARGE", False),
        error(422, "VALIDATION_ERROR", False),
        error(422, "BATCH_TOO_LARGE", False),
    ],
)
def test_batch_level_rejection_falls_back_to_single_sends(
    store, config, clock, db_path, batch_error
):
    ids = add_events(store, 5)
    singles = {
        ids[0]: receipt(ids[0], "accepted", 202),
        ids[1]: receipt(ids[1], "already_received", 200),
        ids[2]: receipt(ids[2], "already_processed", 200),
        ids[3]: error(422, "VALIDATION_ERROR", False),
        # 不要求状态码与 status 的对应关系。
        ids[4]: receipt(ids[4], "accepted", 200),
    }
    transport = FakeTransport(
        route(lambda request: batch_error, lambda request: singles[request.event_ids()[0]])
    )

    summary = run(store, transport, config, clock)

    assert summary == DeliverySummary(claimed=5, delivered=4, retried=0, dead=1, fenced=0)
    assert [request.event_ids() for request in transport.singles] == [[eid] for eid in ids]
    for request, eid in zip(transport.singles, ids, strict=True):
        assert request.url == BASE_URL + SINGLE_PATH
        assert request.body == payload_for(eid)
        assert_signed(request, SECRET, SINGLE_PATH)
    request_id_values = [request.headers["X-Acuven-Request-Id"] for request in transport.sent]
    assert len(set(request_id_values)) == len(request_id_values)
    state = rows(db_path)
    assert [state[eid]["status"] for eid in ids] == [
        "DELIVERED",
        "DELIVERED",
        "DELIVERED",
        "DEAD",
        "DELIVERED",
    ]
    assert state[ids[3]]["last_error_code"] == "VALIDATION_ERROR"


def test_fallback_unknown_usage_type_only_backs_off_that_event(store, config, clock, db_path):
    ids = add_events(store, 3)

    def single(request: Sent) -> Response:
        eid = request.event_ids()[0]
        if eid == ids[1]:
            return error(409, "UNKNOWN_USAGE_TYPE", True)
        return receipt(eid)

    transport = FakeTransport(route(lambda request: error(422, "BATCH_TOO_LARGE", False), single))

    summary = run(store, transport, config, clock)

    assert summary == DeliverySummary(claimed=3, delivered=2, retried=1, dead=0, fenced=0)
    assert len(transport.singles) == 3
    state = rows(db_path)
    assert state[ids[0]]["status"] == "DELIVERED"
    assert state[ids[1]]["status"] == "PENDING"
    assert state[ids[1]]["last_error_code"] == "UNKNOWN_USAGE_TYPE"
    assert state[ids[1]]["next_attempt_at"] == T0 + 1
    assert state[ids[2]]["status"] == "DELIVERED"


@pytest.mark.parametrize(
    "bad_receipt",
    [
        lambda eid: receipt("evt9999"),
        lambda eid: receipt(eid, "rejected", 202),
        lambda eid: receipt(eid, "weird", 200),
        lambda eid: Response(202, b"not json"),
        lambda eid: json_response(200, {"data": "x"}),
        lambda eid: json_response(202, {"data": {"event_id": eid, "status": ["accepted"]}}),
    ],
)
def test_fallback_invalid_single_receipt_only_affects_that_event(
    store, config, clock, db_path, bad_receipt
):
    ids = add_events(store, 3)

    def single(request: Sent) -> Response:
        eid = request.event_ids()[0]
        return bad_receipt(eid) if eid == ids[1] else receipt(eid)

    transport = FakeTransport(route(lambda request: error(422, "BATCH_TOO_LARGE", False), single))

    summary = run(store, transport, config, clock)

    assert summary == DeliverySummary(claimed=3, delivered=2, retried=1, dead=0, fenced=0)
    assert len(transport.singles) == 3
    state = rows(db_path)
    assert state[ids[1]]["status"] == "PENDING"
    assert state[ids[1]]["last_error_code"] == "INVALID_BILLING_RECEIPT"
    assert state[ids[2]]["status"] == "DELIVERED"


@pytest.mark.parametrize(
    ("result", "code"),
    [
        (TransportError("connection reset"), "NETWORK_ERROR"),
        (error(503, "SERVICE_UNAVAILABLE", True), "SERVICE_UNAVAILABLE"),
        (error(401, AUTH, False), AUTH),
        (error(405, "METHOD_NOT_ALLOWED", False), "METHOD_NOT_ALLOWED"),
        (error(404, "SOMETHING_NEW", False), "SOMETHING_NEW"),
        (error(409, "UNKNOWN_USAGE_TYPE", False), "UNKNOWN_USAGE_TYPE"),
        (Response(502, b""), "BILLING_UNAVAILABLE"),
        (Response(400, b"bad request"), "INVALID_BILLING_RESPONSE"),
    ],
)
def test_fallback_stops_on_result_unrelated_to_the_event(
    store, config, clock, db_path, result, code
):
    ids = add_events(store, 4)

    def single(request: Sent) -> Response | Exception:
        eid = request.event_ids()[0]
        return result if eid == ids[1] else receipt(eid)

    transport = FakeTransport(route(lambda request: error(422, "BATCH_TOO_LARGE", False), single))

    summary = run(store, transport, config, clock)

    assert summary == DeliverySummary(claimed=4, delivered=1, retried=3, dead=0, fenced=0)
    # 第 2 条之后不再发。
    assert [request.event_ids() for request in transport.singles] == [[ids[0]], [ids[1]]]
    state = rows(db_path)
    assert state[ids[0]]["status"] == "DELIVERED"
    expected_next = T0 + 60 if code == AUTH else T0 + 1
    for eid in ids[1:]:
        assert state[eid]["status"] == "PENDING"
        assert state[eid]["last_error_code"] == code
        assert state[eid]["next_attempt_at"] == expected_next
        assert state[eid]["lease_until"] is None


def test_fallback_whitelisted_codes_go_dead_and_continue(store, config, clock, db_path):
    ids = add_events(store, len(DEAD_LETTER_CODES) + 1)
    codes = dict(zip(ids, sorted(DEAD_LETTER_CODES), strict=False))

    def single(request: Sent) -> Response:
        eid = request.event_ids()[0]
        if eid in codes:
            return error(422, codes[eid], False)
        return receipt(eid)

    transport = FakeTransport(route(lambda request: error(413, "PAYLOAD_TOO_LARGE", False), single))

    summary = run(store, transport, config, clock)

    assert summary.dead == len(DEAD_LETTER_CODES)
    assert summary.delivered == 1
    state = rows(db_path)
    for eid, code in codes.items():
        assert state[eid]["status"] == "DEAD"
        assert state[eid]["last_error_code"] == code
    assert state[ids[-1]]["status"] == "DELIVERED"


# --- 范围不符按信封；日志 ---------------------------------------------------------------


def test_scope_mismatch_goes_dead_by_envelope(store, config, clock, db_path, caplog):
    caplog.set_level(logging.DEBUG, logger="acuven_billing_client")
    ids = add_events(store, 2)
    items = {ids[0]: rejected_item("CREDENTIAL_SCOPE_MISMATCH", False)}
    transport = FakeTransport(lambda request: batch_receipt(request, items))

    summary = run(store, transport, config, clock)

    assert summary == DeliverySummary(claimed=2, delivered=1, retried=0, dead=1, fenced=0)
    assert rows(db_path)[ids[0]]["status"] == "DEAD"
    assert rows(db_path)[ids[0]]["last_error_code"] == "CREDENTIAL_SCOPE_MISMATCH"
    assert store.delivery_counts().auth_failing is False
    errors = [record for record in caplog.records if record.levelno == logging.ERROR]
    assert len(errors) == 1
    assert errors[0].name == "acuven_billing_client"
    assert ids[0] in errors[0].getMessage()
    assert "CREDENTIAL_SCOPE_MISMATCH" in errors[0].getMessage()
    signature = transport.sent[0].headers["X-Acuven-Signature"]
    for forbidden in (SECRET, signature, MARKER, "ak_test"):
        assert forbidden not in caplog.text


def test_single_403_scope_mismatch_goes_dead_and_continues(store, config, clock, db_path, caplog):
    caplog.set_level(logging.DEBUG, logger="acuven_billing_client")
    ids = add_events(store, 3)

    def single(request: Sent) -> Response:
        eid = request.event_ids()[0]
        if eid == ids[0]:
            return error(403, "CREDENTIAL_SCOPE_MISMATCH", False)
        return receipt(eid)

    transport = FakeTransport(route(lambda request: error(422, "VALIDATION_ERROR", False), single))

    summary = run(store, transport, config, clock)

    assert summary == DeliverySummary(claimed=3, delivered=2, retried=0, dead=1, fenced=0)
    assert rows(db_path)[ids[0]]["status"] == "DEAD"
    assert store.delivery_counts().auth_failing is False
    expected = f"event_id={ids[0]} error_code=CREDENTIAL_SCOPE_MISMATCH"
    assert [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR] == [
        "usage event moved to dead letter: " + expected
    ]
    for request in transport.sent:
        assert request.headers["X-Acuven-Signature"] not in caplog.text
    assert MARKER not in caplog.text
    assert SECRET not in caplog.text


# --- 地址拼接 ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("base_url", "prefix"),
    [
        ("https://billing.example.com/", ""),
        ("https://billing.example.com", ""),
        ("https://billing.example.com/prefix", "/prefix"),
        ("https://billing.example.com/prefix/", "/prefix"),
        ("http://10.0.0.5:8080/a/b/", "/a/b"),
    ],
)
def test_url_and_signed_path_keep_prefix_without_double_slash(
    store, clock, db_path, base_url, prefix
):
    config = make_config(db_path, base_url=base_url)
    add_events(store, 2)
    transport = FakeTransport(
        route(lambda request: error(422, "BATCH_TOO_LARGE", False), accept_all)
    )

    run(store, transport, config, clock)

    origin = base_url.rstrip("/").removesuffix(prefix)
    batch, *singles = transport.sent
    assert batch.url == origin + prefix + BATCH_PATH
    assert_signed(batch, SECRET, prefix + BATCH_PATH)
    assert len(singles) == 2
    for request in singles:
        assert request.url == origin + prefix + SINGLE_PATH
        assert_signed(request, SECRET, prefix + SINGLE_PATH)
    for request in transport.sent:
        assert "//" not in request.path


# --- 重试载荷逐字节相同 -----------------------------------------------------------------


def test_retry_sends_identical_payload_bytes_with_new_request_id_and_timestamp(
    store, config, clock, db_path
):
    ids = add_events(store, 2)
    clock.now = T0 + 0.9
    responses = iter([error(503, "SERVICE_UNAVAILABLE", True)])
    transport = FakeTransport(lambda request: next(responses, None) or batch_receipt(request))
    # 两轮共用一个 Request-Id 生成器，像真实客户端那样；每轮新建的话两轮都从 REQ000001 开始。
    new_request_id = request_ids()

    first = run(store, transport, config, clock, new_request_id=new_request_id)
    clock.now = T0 + 3600.4
    second = run(store, transport, config, clock, new_request_id=new_request_id)

    assert first.retried == 2
    assert second.delivered == 2
    one, two = transport.sent
    assert one.body == two.body == batch_body([payload_for(eid) for eid in ids])
    assert one.headers["X-Acuven-Request-Id"] != two.headers["X-Acuven-Request-Id"]
    assert one.headers["X-Acuven-Timestamp"] == str(int(T0))
    assert two.headers["X-Acuven-Timestamp"] == str(int(T0 + 3600))
    assert one.headers["X-Acuven-Signature"] != two.headers["X-Acuven-Signature"]
    for request in (one, two):
        assert_signed(request, SECRET, BATCH_PATH)
    stored = rows(db_path)
    assert [stored[eid]["payload"] for eid in ids] == [payload_for(eid) for eid in ids]


# --- 组批上限 ---------------------------------------------------------------------------


def test_batches_split_by_event_count(store, config, clock):
    add_events(store, 150)
    transport = FakeTransport(accept_all)

    first = run(store, transport, config, clock)
    second = run(store, transport, config, clock)
    third = run(store, transport, config, clock)

    assert first.claimed == first.delivered == 100
    assert second.claimed == second.delivered == 50
    assert third == DeliverySummary.zero()
    assert [len(request.event_ids()) for request in transport.sent] == [100, 50]


def test_batches_split_by_body_bytes(store, config, clock):
    size = 15 * 1024
    ids = add_events(store, 100, size=size)
    assert len(payload_for(ids[0], size)) == size
    transport = FakeTransport(accept_all)

    summaries = [run(store, transport, config, clock) for _ in range(3)]

    counts = [len(request.event_ids()) for request in transport.sent]
    assert sum(counts) == 100
    assert counts == [65, 35]
    assert [summary.delivered for summary in summaries] == [65, 35, 0]
    for request in transport.sent:
        assert len(request.body) <= config.batch_max_bytes
    # 第一批再加一条就会超过上限。
    assert len(transport.sent[0].body) + size + 1 > config.batch_max_bytes


# --- 单发回退续租与被重领后停止 ---------------------------------------------------------


def test_fallback_renews_leases_and_stops_when_rows_are_reclaimed(store, config, clock, db_path):
    ids = add_events(store, 4)
    leases_seen: list[dict[str, float]] = []
    reclaimed: list[str] = []

    def leases(event_ids: list[str]) -> dict[str, float]:
        state = rows(db_path)
        return {eid: state[eid]["lease_until"] for eid in event_ids}

    def batch(request: Sent) -> Response:
        clock.now += 10
        return error(422, "BATCH_TOO_LARGE", False)

    def single(request: Sent) -> Response:
        eid = request.event_ids()[0]
        position = ids.index(eid)
        leases_seen.append(leases(ids[position:]))
        # 每条单发耗时 30 秒。
        clock.now += 30
        if position == 1:
            # 第 3、4 行的租约改到过去，再由另一个线程领取：它们被别人重领。
            execute(
                db_path,
                "UPDATE usage_outbox SET lease_until = ? WHERE event_id IN (?, ?)",
                (clock.now - 1, ids[2], ids[3]),
            )

            def claim() -> None:
                claimed = store.claim(max_events=10, max_bytes=1_000_000, lease_seconds=60)
                reclaimed.extend(event.event_id for event in claimed)

            thread = threading.Thread(target=claim)
            thread.start()
            thread.join(10)
        return receipt(eid)

    transport = FakeTransport(route(batch, single))

    summary = run(store, transport, config, clock)

    assert reclaimed == [ids[2], ids[3]]
    # 每条发送前对剩下还没落状态的行统一续租到 now + 60。
    assert leases_seen == [
        {eid: T0 + 10 + 60 for eid in ids},
        {eid: T0 + 40 + 60 for eid in ids[1:]},
    ]
    assert [request.event_ids() for request in transport.singles] == [[ids[0]], [ids[1]]]
    assert summary == DeliverySummary(claimed=4, delivered=2, retried=0, dead=0, fenced=2)
    state = rows(db_path)
    assert state[ids[0]]["status"] == "DELIVERED"
    assert state[ids[1]]["status"] == "DELIVERED"
    # 剩下的行以重领者为准：本进程没有改它们。
    for eid in ids[2:]:
        assert state[eid]["status"] == "SENDING"
        assert state[eid]["lease_until"] == T0 + 70 + 60
        assert state[eid]["attempts"] == 2
        assert state[eid]["last_error_code"] is None


def test_fenced_rows_are_counted_when_marking(store, config, clock, db_path):
    ids = add_events(store, 2)

    def batch(request: Sent) -> Response:
        # 发送期间第 2 行被别人重领（租约值变了）。
        execute(
            db_path,
            "UPDATE usage_outbox SET lease_until = ? WHERE event_id = ?",
            (T0 + 999, ids[1]),
        )
        return batch_receipt(request)

    transport = FakeTransport(batch)

    summary = run(store, transport, config, clock)

    assert summary == DeliverySummary(claimed=2, delivered=1, retried=0, dead=0, fenced=1)
    assert rows(db_path)[ids[0]]["status"] == "DELIVERED"
    assert rows(db_path)[ids[1]]["status"] == "SENDING"
    assert rows(db_path)[ids[1]]["lease_until"] == T0 + 999


# --- 落回执或续租失败：本轮中止 -----------------------------------------------------------


class FailingStore:
    """Delegates to a real Store; the named methods raise `sqlite3.OperationalError`."""

    def __init__(self, inner: Store, fail: set[str]) -> None:
        self.inner = inner
        self.fail = fail
        self.calls: list[str] = []

    def __getattr__(self, name: str) -> Any:
        target = getattr(self.inner, name)
        if not callable(target):
            return target

        def call(*args: Any, **kwargs: Any) -> Any:
            self.calls.append(name)
            if name in self.fail:
                raise sqlite3.OperationalError("disk I/O error")
            return target(*args, **kwargs)

        return call


def test_mark_failure_aborts_round(store, config, clock, db_path):
    ids = add_events(store, 2)
    failing = FailingStore(store, {"mark_delivered"})
    transport = FakeTransport(accept_all)

    with pytest.raises(sqlite3.OperationalError):
        run(failing, transport, config, clock)

    assert "purge_delivered" not in failing.calls
    assert "record_delivery_ok" not in failing.calls
    for eid in ids:
        assert rows(db_path)[eid]["status"] == "SENDING"
    # 租约到期后被重领。
    clock.now += 61
    assert [
        event.event_id
        for event in store.claim(max_events=10, max_bytes=1_000_000, lease_seconds=60)
    ] == ids


def test_renew_failure_aborts_fallback(store, config, clock, db_path):
    ids = add_events(store, 3)
    failing = FailingStore(store, {"renew_lease"})
    transport = FakeTransport(
        route(lambda request: error(422, "BATCH_TOO_LARGE", False), accept_all)
    )

    with pytest.raises(sqlite3.OperationalError):
        run(failing, transport, config, clock)

    assert transport.singles == []
    assert "purge_delivered" not in failing.calls
    for eid in ids:
        assert rows(db_path)[eid]["status"] == "SENDING"


def test_mark_failure_during_fallback_stops_further_sends(store, config, clock, db_path):
    ids = add_events(store, 3)
    failing = FailingStore(store, {"mark_dead"})

    def single(request: Sent) -> Response:
        eid = request.event_ids()[0]
        if eid == ids[1]:
            return error(422, "VALIDATION_ERROR", False)
        return receipt(eid)

    transport = FakeTransport(route(lambda request: error(422, "BATCH_TOO_LARGE", False), single))

    with pytest.raises(sqlite3.OperationalError):
        run(failing, transport, config, clock)

    assert [request.event_ids() for request in transport.singles] == [[ids[0]], [ids[1]]]
    assert "purge_delivered" not in failing.calls
    assert "record_delivery_ok" not in failing.calls
    state = rows(db_path)
    assert state[ids[0]]["status"] == "DELIVERED"
    assert state[ids[1]]["status"] == "SENDING"
    assert state[ids[2]]["status"] == "SENDING"
    assert store.read_last_delivery_ok_at() is None


# --- 清理 -------------------------------------------------------------------------------


def _insert_delivered(db_path: Path, eid: str, delivered_at: float) -> None:
    execute(
        db_path,
        "INSERT INTO usage_outbox (event_id, payload, status, attempts, next_attempt_at,"
        " created_at, delivered_at) VALUES (?, ?, 'DELIVERED', 1, ?, ?, ?)",
        (eid, payload_for(eid), delivered_at, delivered_at, delivered_at),
    )


@pytest.mark.parametrize(
    ("retention_days", "kept"),
    [
        (2, {"age01", "dead"}),
        (30, {"age01", "age03", "age29", "dead"}),
        (7, {"age01", "age03", "dead"}),
    ],
)
def test_round_purges_by_configured_retention(store, clock, db_path, retention_days, kept):
    config = make_config(db_path, delivered_retention_seconds=retention_days * DAY)
    for days in (1, 3, 29, 31):
        _insert_delivered(db_path, f"age{days:02d}", T0 - days * DAY)
    execute(
        db_path,
        "INSERT INTO usage_outbox (event_id, payload, status, attempts, next_attempt_at,"
        " created_at, dead_at) VALUES ('dead', x'7b7d', 'DEAD', 1, ?, ?, ?)",
        (T0 - 90 * DAY, T0 - 90 * DAY, T0 - 90 * DAY),
    )
    transport = FakeTransport(accept_all)

    summary = run(store, transport, config, clock)

    assert summary == DeliverySummary.zero()
    assert transport.sent == []
    assert set(rows(db_path)) == kept


def test_purge_removes_at_most_1000_rows_per_round(store, config, clock, db_path):
    conn = raw(db_path)
    try:
        conn.execute("BEGIN")
        for n in range(1001):
            old = T0 - 30 * DAY
            conn.execute(
                "INSERT INTO usage_outbox (event_id, payload, status, attempts, next_attempt_at,"
                " created_at, delivered_at) VALUES (?, x'7b7d', 'DELIVERED', 1, ?, ?, ?)",
                (f"old{n:05d}", old, old, old + n),
            )
        conn.execute("COMMIT")
    finally:
        conn.close()

    run(store, FakeTransport(accept_all), config, clock)

    assert list(rows(db_path)) == ["old01000"]


# --- 退避 -------------------------------------------------------------------------------


@pytest.mark.parametrize("attempts", range(1, 14))
def test_backoff_interval(attempts):
    cap = min(3600, 2 ** min(attempts, 12))

    assert backoff_seconds(attempts, FixedRng(0.0)) == 0.5 * cap
    assert backoff_seconds(attempts, FixedRng(1.0)) == 1.0 * cap
    rng = random.Random(attempts)
    for _ in range(200):
        assert 0.5 * cap <= backoff_seconds(attempts, rng) <= 1.0 * cap


def test_backoff_caps_at_one_hour():
    assert backoff_seconds(12, FixedRng(1.0)) == 3600
    assert backoff_seconds(13, FixedRng(1.0)) == 3600
    assert backoff_seconds(40, FixedRng(1.0)) == 3600


def test_next_attempt_at_fixed_for_auth_failure():
    rng = FixedRng(1.0)

    assert next_attempt_at(T0, 9, AUTH, rng) == T0 + 60
    assert rng.calls == []
    assert next_attempt_at(T0, 9, "NETWORK_ERROR", rng) == T0 + 512
    assert rng.calls == [(0.5, 1.0)]


def test_retry_uses_claimed_attempts_for_backoff(store, config, clock, db_path):
    [eid] = add_events(store, 1)
    execute(db_path, "UPDATE usage_outbox SET attempts = 6 WHERE event_id = ?", (eid,))
    transport = FakeTransport(lambda request: TransportError("down"))

    run(store, transport, config, clock, FixedRng(0.5))

    row = rows(db_path)[eid]
    assert row["attempts"] == 7
    # 2 ** 7 × uniform(0.5, 1.0)，uniform 取 0.75。
    assert row["next_attempt_at"] == T0 + 128 * 0.75


def test_retryable_failures_have_no_give_up_limit(store, config, clock, db_path):
    [eid] = add_events(store, 1)
    transport = FakeTransport(lambda request: error(503, "SERVICE_UNAVAILABLE", True))

    for _ in range(20):
        run(store, transport, config, clock, FixedRng(1.0))
        clock.now += 3600

    row = rows(db_path)[eid]
    assert row["status"] == "PENDING"
    assert row["attempts"] == 20

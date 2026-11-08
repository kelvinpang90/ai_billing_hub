"""BillingClient.handle_status_webhook (design gate #571 v24 §2「状态 webhook」).

覆盖 §7 标 169 的 webhook 各行：验签全部情形（signed_path 与查询串排序、正确与错误签名、
时间窗外 301 秒与恰好 300 秒、未知版本、缺请求头、体里 event_id 与头不符、新旧两个版本）、
webhook 不冒充对账、webhook 写库失败、webhook 的 BLOCK_AI 在对账失败时生效、换项目不沿用
旧状态、租户或项目不符，以及请求头与路径的各种传法。

签名在测试里按 `docs/api.md`「状态 webhook」的规范串直接用 hmac 计算，不经客户端的签名函数。
SQLite 用 tmp_path 下的文件，HTTP 用注入的假 Transport，时间用注入的时钟。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import random
import sqlite3
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from acuven_billing_client import (
    BillingClient,
    BillingClientConfig,
    Response,
    StatusStoreError,
    Store,
    WebhookVerificationError,
)
from acuven_billing_client.status import MAX_STATUS_VERSION

T0 = 1_790_000_000.0
BASE_URL = "https://billing.example.com"
SECRET = "sk_" + "0" * 64
OLD_SECRET = "whsec_" + "1" * 40
NEW_SECRET = "whsec_" + "2" * 40
TENANT = "ten_a"
PROJECT = "prj_a"
EVENT_ID = "sevt_prj_a_7"
HOOK_PATH = "/hooks/acuven/status"
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


class StatusTransport:
    """Every request gets `reply` (default 503); counts requests."""

    def __init__(self, reply: Response | None = None) -> None:
        self.reply = reply if reply is not None else Response(503, b"")
        self.requests = 0

    def send(self, method: str, url: str, headers: Any, body: bytes, timeout: float) -> Response:
        self.requests += 1
        return self.reply


def sqlite_error(name: str) -> sqlite3.Error:
    error = sqlite3.OperationalError(f"simulated {name}")
    error.sqlite_errorname = name  # type: ignore[attr-defined]
    return error


def fail_store_method(
    monkeypatch: pytest.MonkeyPatch, method: str, make_error: Callable[[], BaseException]
) -> None:
    def replacement(store: Store, *args: Any, **kwargs: Any) -> Any:
        raise make_error()

    monkeypatch.setattr(Store, method, replacement)


# --- 请求 -------------------------------------------------------------------------------


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


def webhook_body(event_id: str = EVENT_ID, **overrides: Any) -> bytes:
    return json.dumps({**status_data(**overrides), "event_id": event_id}).encode()


def compute_signature(
    secret: str, path_and_query: str, timestamp: str, event_id: str, body: bytes
) -> str:
    # path_and_query 已按规则规范化（查询串排序）。
    canonical = "\n".join(
        ("POST", path_and_query, timestamp, event_id, hashlib.sha256(body).hexdigest())
    )
    return hmac.new(secret.encode(), canonical.encode(), hashlib.sha256).hexdigest()


def signed_headers(
    body: bytes,
    *,
    secret: str = NEW_SECRET,
    version: int | str = 2,
    timestamp: str | None = None,
    event_id: str = EVENT_ID,
    path: str = HOOK_PATH,
) -> dict[str, str]:
    ts = str(int(T0)) if timestamp is None else timestamp
    return {
        "X-Acuven-Timestamp": ts,
        "X-Acuven-Signature": compute_signature(secret, path, ts, event_id, body),
        "X-Acuven-Event-ID": event_id,
        "X-Acuven-Key-Version": str(version),
        "Content-Type": "application/json",
    }


def send(
    client: BillingClient,
    body: bytes,
    headers: Any,
    *,
    method: str | bytes = "POST",
    raw_path: str | bytes = HOOK_PATH,
    query_string: str | bytes = b"",
    signed_path: str | None = None,
) -> bool:
    return client.handle_status_webhook(
        method=method,
        raw_path=raw_path,
        query_string=query_string,
        headers=headers,
        body=body,
        signed_path=signed_path,
    )


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


def reconcile_rows(path: Path) -> list[dict[str, Any]]:
    conn = raw(path)
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM reconcile_state")]
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
        "webhook_secrets": {1: OLD_SECRET, 2: NEW_SECRET},
    }
    values.update(overrides)
    return BillingClientConfig(**values)


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
            transport=transport if transport is not None else StatusTransport(),
            clock=clock,
            rng=random.Random(0),
        )
        made.append(client)
        return client

    yield make
    for client in made:
        client.close(timeout=2)


@pytest.fixture
def client(make_client: Callable[..., BillingClient], db_path: Path) -> BillingClient:
    return make_client(db_path)


# --- 通过与版本 -------------------------------------------------------------------------


def test_valid_webhook_is_applied(client: BillingClient, db_path: Path) -> None:
    body = webhook_body(effective_status="BLOCK_AI", status_version=4, reason_code="X")

    assert send(client, body, signed_headers(body)) is True

    assert client.should_block_ai() is True
    status = client.current_status()
    assert status == {
        **status_data(effective_status="BLOCK_AI", status_version=4, reason_code="X"),
        "applied_at": T0,
    }
    [row] = status_rows(db_path)
    assert set(row) == STATUS_KEYS | {"applied_at"}


def test_same_or_older_version_is_not_an_error(client: BillingClient, clock: FakeClock) -> None:
    newer = webhook_body(effective_status="BLOCK_AI", status_version=5)
    assert send(client, newer, signed_headers(newer)) is True

    clock.now = T0 + 10
    same = webhook_body(status_version=5)
    older = webhook_body(status_version=4)
    assert send(client, same, signed_headers(same, timestamp=str(int(T0 + 10)))) is False
    assert send(client, older, signed_headers(older, timestamp=str(int(T0 + 10)))) is False

    status = client.current_status()
    assert status is not None
    assert status["status_version"] == 5
    assert status["effective_status"] == "BLOCK_AI"
    assert status["applied_at"] == T0


def test_old_and_new_key_versions_are_both_accepted(client: BillingClient) -> None:
    first = webhook_body(status_version=1)
    second = webhook_body(status_version=2)

    assert send(client, first, signed_headers(first, secret=OLD_SECRET, version=1)) is True
    assert send(client, second, signed_headers(second, secret=NEW_SECRET, version=2)) is True
    assert client.current_status()["status_version"] == 2


def test_unknown_field_is_ignored(client: BillingClient, db_path: Path) -> None:
    body = json.dumps(
        {**status_data(status_version=MAX_STATUS_VERSION), "event_id": EVENT_ID, "margin": 9}
    ).encode()

    assert send(client, body, signed_headers(body)) is True

    assert set(client.current_status()) == STATUS_KEYS | {"applied_at"}
    [row] = status_rows(db_path)
    assert set(row) == STATUS_KEYS | {"applied_at"}


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
    client = make_client(db_path)
    body = webhook_body(status_version=1)

    # 版本只和配置那一对比较：1 不因旧那一对的 99 被忽略。
    assert send(client, body, signed_headers(body)) is True

    assert client.should_block_ai() is False
    rows = {(r["tenant_id"], r["project_id"]): r for r in status_rows(db_path)}
    assert rows[(TENANT, PROJECT)]["status_version"] == 1
    assert rows[("ten_old", "prj_old")]["status_version"] == 99
    assert rows[("ten_old", "prj_old")]["effective_status"] == "BLOCK_AI"


# --- 与对账的关系 -----------------------------------------------------------------------


def test_webhook_block_applies_while_reconcile_fails(
    make_client: Callable[..., BillingClient], db_path: Path, clock: FakeClock
) -> None:
    client = make_client(db_path, transport=StatusTransport(Response(503, b"")))
    assert client.reconcile_status_once() is False
    assert client.should_block_ai() is False

    clock.now = T0 + 5
    body = webhook_body(effective_status="BLOCK_AI", status_version=8)
    assert send(client, body, signed_headers(body, timestamp=str(int(T0 + 5)))) is True

    # webhook 不冒充对账：reconcile_state 不变。
    assert client.should_block_ai() is True
    assert reconcile_rows(db_path) == [
        {"tenant_id": TENANT, "project_id": PROJECT, "last_ok_at": None, "last_error_at": T0}
    ]

    assert client.reset_status(reason="center restored") is True
    assert client.should_block_ai() is False


def test_webhook_does_not_touch_the_last_reconcile_time(
    make_client: Callable[..., BillingClient], db_path: Path, clock: FakeClock
) -> None:
    ok = Response(200, json.dumps({"data": status_data(status_version=1)}).encode())
    client = make_client(db_path, transport=StatusTransport(ok))
    assert client.reconcile_status_once() is True
    next_at = client.next_reconcile_at

    clock.now = T0 + 60
    body = webhook_body(status_version=2)
    assert send(client, body, signed_headers(body, timestamp=str(int(T0 + 60)))) is True

    assert reconcile_rows(db_path)[0]["last_ok_at"] == T0
    assert client.next_reconcile_at == next_at


# --- 写库失败与降级 ---------------------------------------------------------------------


@pytest.mark.parametrize(("name", "available"), [("SQLITE_IOERR", False), ("SQLITE_BUSY", True)])
def test_store_failure_raises_status_store_error(
    name: str,
    available: bool,
    client: BillingClient,
    db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fail_store_method(monkeypatch, "apply_status", lambda: sqlite_error(name))
    body = webhook_body(effective_status="BLOCK_AI")

    with pytest.raises(StatusStoreError) as caught:
        send(client, body, signed_headers(body))

    assert not isinstance(caught.value, WebhookVerificationError)
    assert client.store_available is available
    monkeypatch.undo()
    assert status_rows(db_path) == []


def test_degraded_store(make_client: Callable[..., BillingClient], tmp_path: Path) -> None:
    client = make_client(tmp_path / "missing" / "outbox.db")
    assert not client.store_available
    body = webhook_body()

    with pytest.raises(StatusStoreError):
        send(client, body, signed_headers(body))
    # 验签不过仍是 WebhookVerificationError。
    with pytest.raises(WebhookVerificationError):
        send(client, body, signed_headers(body, secret="whsec_wrong"))


# --- 验签 -------------------------------------------------------------------------------


def test_wrong_signature(client: BillingClient, db_path: Path) -> None:
    body = webhook_body()
    headers = signed_headers(body)
    headers["X-Acuven-Signature"] = "0" * 64

    with pytest.raises(WebhookVerificationError):
        send(client, body, headers)
    assert status_rows(db_path) == []


def test_wrong_secret_for_the_version(client: BillingClient) -> None:
    body = webhook_body()
    with pytest.raises(WebhookVerificationError):
        send(client, body, signed_headers(body, secret=OLD_SECRET, version=2))


def test_unknown_key_version(client: BillingClient) -> None:
    body = webhook_body()
    with pytest.raises(WebhookVerificationError):
        send(client, body, signed_headers(body, secret=NEW_SECRET, version=3))


def test_tampered_body(client: BillingClient) -> None:
    body = webhook_body(status_version=1)
    headers = signed_headers(body)
    with pytest.raises(WebhookVerificationError):
        send(client, webhook_body(status_version=2), headers)


def test_no_webhook_secrets_configured(
    make_client: Callable[..., BillingClient], db_path: Path
) -> None:
    client = make_client(db_path, webhook_secrets={})
    body = webhook_body()
    with pytest.raises(WebhookVerificationError):
        send(client, body, signed_headers(body))
    assert status_rows(db_path) == []


@pytest.mark.parametrize(
    ("offset", "accepted"),
    [(-301, False), (-300, True), (0, True), (300, True), (301, False)],
)
def test_time_window(offset: int, accepted: bool, client: BillingClient, clock: FakeClock) -> None:
    # 本机时钟带小数：按 int(clock()) 与整秒的时间戳比较。
    clock.now = T0 + 0.9
    body = webhook_body()
    headers = signed_headers(body, timestamp=str(int(T0) + offset))

    if accepted:
        assert send(client, body, headers) is True
    else:
        with pytest.raises(WebhookVerificationError):
            send(client, body, headers)


@pytest.mark.parametrize(
    "header",
    ["X-Acuven-Timestamp", "X-Acuven-Signature", "X-Acuven-Event-ID", "X-Acuven-Key-Version"],
)
def test_missing_header(header: str, client: BillingClient) -> None:
    body = webhook_body()
    headers = signed_headers(body)
    del headers[header]
    with pytest.raises(WebhookVerificationError):
        send(client, body, headers)


MALFORMED = [
    ("X-Acuven-Timestamp", "+1790000000"),
    ("X-Acuven-Timestamp", "01790000000"),
    ("X-Acuven-Timestamp", "1790000000.0"),
    ("X-Acuven-Timestamp", " 1790000000"),
    ("X-Acuven-Timestamp", ""),
    ("X-Acuven-Key-Version", "02"),
    ("X-Acuven-Key-Version", "0"),
    ("X-Acuven-Key-Version", "-2"),
    ("X-Acuven-Key-Version", "2.0"),
    ("X-Acuven-Key-Version", "٢"),
    ("X-Acuven-Event-ID", "sevt prj"),
    ("X-Acuven-Event-ID", "sevt/prj"),
    ("X-Acuven-Event-ID", ""),
    ("X-Acuven-Event-ID", "e" * 201),
]


@pytest.mark.parametrize(("header", "value"), MALFORMED)
def test_malformed_header(header: str, value: str, client: BillingClient) -> None:
    body = webhook_body()
    if header == "X-Acuven-Event-ID":
        # 签名按这个值计算：只有格式会让它不过。
        headers = signed_headers(webhook_body(event_id=value), event_id=value)
        body = webhook_body(event_id=value)
    elif header == "X-Acuven-Timestamp":
        headers = signed_headers(body, timestamp=value)
    else:
        headers = signed_headers(body)
        headers[header] = value
    with pytest.raises(WebhookVerificationError):
        send(client, body, headers)


def test_malformed_signature(client: BillingClient) -> None:
    body = webhook_body()
    headers = signed_headers(body)
    headers["X-Acuven-Signature"] = headers["X-Acuven-Signature"].upper()
    with pytest.raises(WebhookVerificationError):
        send(client, body, headers)


def test_event_id_format_limits(client: BillingClient) -> None:
    longest = "sevt_" + "a" * 189 + ".:-_09"
    assert len(longest) == 200
    body = webhook_body(event_id=longest)
    assert send(client, body, signed_headers(body, event_id=longest)) is True


def test_body_event_id_must_match_the_header(client: BillingClient, db_path: Path) -> None:
    body = webhook_body(event_id="sevt_prj_a_8")
    with pytest.raises(WebhookVerificationError):
        send(client, body, signed_headers(body, event_id=EVENT_ID))
    assert status_rows(db_path) == []


def _without(name: str) -> bytes:
    document = {**status_data(), "event_id": EVENT_ID}
    del document[name]
    return json.dumps(document).encode()


BAD_BODIES: list[tuple[str, bytes]] = [
    *[(f"missing {name}", _without(name)) for name in sorted(STATUS_KEYS | {"event_id"})],
    ("event_id a number", json.dumps({**status_data(), "event_id": 7}).encode()),
    ("billing_mode a number", webhook_body(billing_mode=1)),
    ("effective_at null", webhook_body(effective_at=None)),
    ("reason_code a list", webhook_body(reason_code=["X"])),
    ("effective_status unknown", webhook_body(effective_status="PAUSE_AI")),
    ("status_version a bool", webhook_body(status_version=False)),
    ("status_version negative", webhook_body(status_version=-5)),
    ("status_version a float", webhook_body(status_version=1.5)),
    ("status_version over 2^63 - 1", webhook_body(status_version=MAX_STATUS_VERSION + 1)),
    ("other tenant", webhook_body(tenant_id="ten_b")),
    ("other project", webhook_body(project_id="prj_b")),
    ("not JSON", b"status=ALLOW_AI"),
    ("an array", b"[]"),
    ("empty", b""),
]


@pytest.mark.parametrize("body", [b for _, b in BAD_BODIES], ids=[n for n, _ in BAD_BODIES])
def test_invalid_body(body: bytes, client: BillingClient, db_path: Path) -> None:
    with pytest.raises(WebhookVerificationError):
        send(client, body, signed_headers(body))
    assert status_rows(db_path) == []


# --- 路径、查询串与请求头的传法 ---------------------------------------------------------


def test_query_string_is_sorted(client: BillingClient) -> None:
    body = webhook_body()
    headers = signed_headers(body, path=HOOK_PATH + "?a=1&b=2")

    assert send(client, body, headers, query_string=b"b=2&a=1") is True


def test_raw_path_and_query_as_str(client: BillingClient) -> None:
    body = webhook_body()
    headers = signed_headers(body, path=HOOK_PATH + "?k=v")

    assert send(client, body, headers, raw_path=HOOK_PATH, query_string="k=v") is True


def test_raw_path_bytes_are_latin1(client: BillingClient) -> None:
    body = webhook_body()
    # 每个字节按 latin-1 解成一个字符：b"\xe9" 成为 "é"。
    headers = signed_headers(body, path="/hooks/\xe9t\xe9?q=\xe9")

    assert send(client, body, headers, raw_path=b"/hooks/\xe9t\xe9", query_string=b"q=\xe9") is True


def test_signed_path_replaces_the_raw_path(client: BillingClient) -> None:
    body = webhook_body()
    # 平台配置的 URL 是 /prefix/hooks/acuven/status?z=1&a=2，代理剥掉了 /prefix。
    headers = signed_headers(body, path="/prefix" + HOOK_PATH + "?a=2&z=1")

    with pytest.raises(WebhookVerificationError):
        send(client, body, headers, raw_path=HOOK_PATH, query_string=b"z=1&a=2")
    assert (
        send(
            client,
            body,
            headers,
            raw_path=HOOK_PATH,
            query_string=b"z=1&a=2",
            signed_path="/prefix" + HOOK_PATH + "?z=1&a=2",
        )
        is True
    )


def test_header_names_are_case_insensitive(client: BillingClient) -> None:
    body = webhook_body()
    headers = {name.lower(): value for name, value in signed_headers(body).items()}
    assert send(client, body, headers) is True

    newer = webhook_body(status_version=2)
    shouting = {name.upper(): value for name, value in signed_headers(newer).items()}
    assert send(client, newer, shouting) is True


def test_headers_as_byte_pairs(client: BillingClient) -> None:
    body = webhook_body()
    pairs = [
        (name.lower().encode("latin-1"), value.encode("latin-1"))
        for name, value in signed_headers(body).items()
    ]
    pairs.append((b"x-forwarded-for", b"10.0.0.1"))
    pairs.append((b"x-forwarded-for", b"10.0.0.2"))

    assert send(client, body, pairs, method=b"POST") is True


def test_repeated_signing_header_is_malformed(client: BillingClient) -> None:
    body = webhook_body()
    pairs = list(signed_headers(body).items())
    pairs.append(("x-acuven-event-id", EVENT_ID))

    with pytest.raises(WebhookVerificationError):
        send(client, body, pairs)

    # 映射里大小写不同的同名请求头也算重复。
    headers: dict[str, str] = signed_headers(body)
    headers["x-acuven-timestamp"] = headers["X-Acuven-Timestamp"]
    with pytest.raises(WebhookVerificationError):
        send(client, body, headers)


@pytest.mark.parametrize(
    "headers",
    [None, "X-Acuven-Timestamp: 1", [("only-a-name",)], [(1, "x")]],
    ids=["none", "a string", "not pairs", "name not text"],
)
def test_unusable_headers(headers: Any, client: BillingClient) -> None:
    body = webhook_body()
    with pytest.raises(WebhookVerificationError):
        send(client, body, headers)


def test_lowercase_method_is_signed_uppercase(client: BillingClient) -> None:
    body = webhook_body()
    assert send(client, body, signed_headers(body), method="post") is True


def test_webhook_does_not_call_the_platform(
    make_client: Callable[..., BillingClient], db_path: Path
) -> None:
    transport = StatusTransport()
    client = make_client(db_path, transport=transport)
    body = webhook_body()

    assert send(client, body, signed_headers(body)) is True
    assert transport.requests == 0

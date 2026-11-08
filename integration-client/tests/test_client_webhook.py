"""BillingClient status webhook helper (design gate #571 v24 §2「状态 webhook」).

覆盖 §7 标 169 的 webhook 各行：webhook 验签全部情形（含 `signed_path` 与新旧两个版本）、
webhook 的 BLOCK_AI 在对账失败时生效、webhook 不冒充对账（只断言 `reconcile_state.last_ok_at`
不变；`status_reconcile_stale` 在 AIH-TASK-170）、webhook 写库失败、换项目不沿用旧状态、
租户或项目不符；另有请求头与路径的几种传法、降级时的行为。

签名在测试里按 `docs/api.md`「状态 webhook 签名」独立计算（hmac + hashlib），不调用被测的
签名函数。SQLite 用 tmp_path 下的文件，HTTP 用注入的假 Transport，时间用注入的时钟。
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

T0 = 1_790_000_000.0
TS = int(T0)
BASE_URL = "https://billing.example.com"
SECRET = "sk_" + "0" * 64
OLD_SECRET = "whsec_old_" + "1" * 32
NEW_SECRET = "whsec_new_" + "2" * 32
SECRETS = {1: OLD_SECRET, 2: NEW_SECRET}
TENANT = "ten_a"
PROJECT = "prj_a"
PATH = "/hooks/acuven"


# --- 替身 -------------------------------------------------------------------------------


class FakeClock:
    def __init__(self, now: float = T0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class FakeTransport:
    """Every reconcile fails (503); counts the requests."""

    def __init__(self, answer: Callable[[], Response] | None = None) -> None:
        self.answer = answer
        self.calls = 0

    def send(self, method: str, url: str, headers: Any, body: bytes, timeout: float) -> Response:
        self.calls += 1
        if self.answer is not None:
            return self.answer()
        return Response(503, b'{"error":{"code":"BILLING_UNAVAILABLE"},"retryable":true}')


def sqlite_error(name: str) -> sqlite3.Error:
    error = sqlite3.OperationalError(f"simulated {name}")
    error.sqlite_errorname = name  # type: ignore[attr-defined]
    return error


def fault(
    monkeypatch: pytest.MonkeyPatch, method: str, make_error: Callable[[], Exception]
) -> None:
    def replacement(store: Store, *args: Any, **kwargs: Any) -> Any:
        raise make_error()

    monkeypatch.setattr(Store, method, replacement)


# --- 请求 -------------------------------------------------------------------------------


def payload(version: int, effective: str = "BLOCK_AI", **overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "event_id": f"sevt_{PROJECT}_{version}",
        "tenant_id": TENANT,
        "project_id": PROJECT,
        "account_status": "ENABLED",
        "billing_status": "SUSPENDED" if effective == "BLOCK_AI" else "ACTIVE",
        "billing_mode": "PREPAID",
        "project_integration_status": "ENABLED",
        "effective_status": effective,
        "reason_code": "BALANCE_NOT_POSITIVE" if effective == "BLOCK_AI" else None,
        "status_version": version,
        "effective_at": "2026-10-09T10:10:00",
    }
    data.update(overrides)
    return data


def encode(document: Any) -> bytes:
    # 与服务端相同：按键排序、无多余空白。
    return json.dumps(document, sort_keys=True, separators=(",", ":")).encode()


def signature(
    secret: str,
    canonical_path: str,
    timestamp: str,
    event_id: str,
    body: bytes,
    method: str = "POST",
) -> str:
    canonical = "\n".join(
        (method, canonical_path, timestamp, event_id, hashlib.sha256(body).hexdigest())
    )
    return hmac.new(secret.encode(), canonical.encode(), hashlib.sha256).hexdigest()


def request(
    document: Any = None,
    *,
    body: bytes | None = None,
    version: int = 2,
    secret: str | None = None,
    canonical_path: str = PATH,
    raw_path: str | bytes = PATH,
    query_string: str | bytes = "",
    timestamp: str = str(TS),
    event_id: str | None = None,
    signed_path: str | None = None,
) -> dict[str, Any]:
    """Keyword arguments for `handle_status_webhook`, signed like the platform signs them.

    `event_id` 缺省取请求体里的 `event_id`。
    """
    if body is None:
        document = document if document is not None else payload(5)
        body = encode(document)
    if event_id is None:
        event_id = str(document["event_id"])
    key = secret if secret is not None else SECRETS[version]
    headers = {
        "X-Acuven-Timestamp": timestamp,
        "X-Acuven-Signature": signature(key, canonical_path, timestamp, event_id, body),
        "X-Acuven-Event-ID": event_id,
        "X-Acuven-Key-Version": str(version),
        "Content-Type": "application/json",
    }
    return {
        "method": "POST",
        "raw_path": raw_path,
        "query_string": query_string,
        "headers": headers,
        "body": body,
        "signed_path": signed_path,
    }


# --- 库 ---------------------------------------------------------------------------------


def query(path: Path, sql: str, *args: Any) -> list[dict[str, Any]]:
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql, args)]
    finally:
        conn.close()


def status_rows(path: Path) -> dict[tuple[str, str], dict[str, Any]]:
    return {(r["tenant_id"], r["project_id"]): r for r in query(path, "SELECT * FROM status_state")}


def reconcile_rows(path: Path) -> list[dict[str, Any]]:
    return query(path, "SELECT * FROM reconcile_state")


def make_config(path: Path, **overrides: Any) -> BillingClientConfig:
    values: dict[str, Any] = {
        "base_url": BASE_URL,
        "api_key": "ak_test",
        "key_version": 3,
        "secret": SECRET,
        "tenant_id": TENANT,
        "project_id": PROJECT,
        "outbox_path": str(path),
        "webhook_secrets": SECRETS,
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

    def make(path: Path, transport: FakeTransport | None = None, **overrides: Any) -> BillingClient:
        client = BillingClient(
            make_config(path, **overrides),
            transport=transport if transport is not None else FakeTransport(),
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


# --- 验签通过 ---------------------------------------------------------------------------


@pytest.mark.parametrize("version", [1, 2])
def test_both_configured_versions_verify(
    version: int, client: BillingClient, db_path: Path
) -> None:
    assert client.handle_status_webhook(**request(payload(5), version=version)) is True

    row = status_rows(db_path)[(TENANT, PROJECT)]
    assert (row["status_version"], row["effective_status"]) == (5, "BLOCK_AI")
    assert row["applied_at"] == T0
    assert client.should_block_ai() is True
    assert client.current_status() == {
        key: value for key, value in payload(5).items() if key != "event_id"
    } | {"applied_at": T0}


def test_query_string_is_sorted_for_the_signature(client: BillingClient) -> None:
    kwargs = request(
        payload(5),
        # 按 UTF-8 字节序：值 "10" 排在 "2" 前面。
        canonical_path=PATH + "?a=1&b=10&b=2",
        query_string=b"b=2&a=1&b=10",
    )
    assert client.handle_status_webhook(**kwargs) is True


def test_signed_path_replaces_the_raw_path(client: BillingClient, db_path: Path) -> None:
    # 反向代理剥掉了 /public 前缀；平台签的是配置里的完整路径与查询串。
    platform = "/public/hooks/acuven?token=x&a=1"
    canonical = "/public/hooks/acuven?a=1&token=x"
    without = request(payload(5), canonical_path=canonical, query_string="")
    with pytest.raises(WebhookVerificationError):
        client.handle_status_webhook(**without)
    assert status_rows(db_path) == {}

    with_signed = request(payload(5), canonical_path=canonical, signed_path=platform)
    assert client.handle_status_webhook(**with_signed) is True


def test_signed_path_wins_over_raw_query(client: BillingClient) -> None:
    kwargs = request(
        payload(5),
        canonical_path="/public/hook",
        raw_path="/hook",
        query_string="ignored=1",
        signed_path="/public/hook",
    )
    assert client.handle_status_webhook(**kwargs) is True


@pytest.mark.parametrize("offset", [300, -300, 300.9])
def test_exactly_300_seconds_is_accepted(
    offset: float, client: BillingClient, clock: FakeClock
) -> None:
    # 时间窗用 int(clock()) 与请求头比较，都是整秒。
    clock.now = T0 + offset
    assert client.handle_status_webhook(**request(payload(5))) is True


def test_bytes_and_pair_sequences_are_accepted(client: BillingClient) -> None:
    kwargs = request(
        payload(5), canonical_path=PATH + "?a=%C3%A9&b=2", query_string=b"b=2&a=%C3%A9"
    )
    kwargs["raw_path"] = PATH.encode("latin-1")
    kwargs["headers"] = [
        (name.lower().encode("latin-1"), value.encode("latin-1"))
        for name, value in kwargs["headers"].items()
    ]
    assert client.handle_status_webhook(**kwargs) is True


def test_header_names_are_case_insensitive(client: BillingClient) -> None:
    kwargs = request(payload(5))
    kwargs["headers"] = [
        (name.upper() if index % 2 else name.lower(), value)
        for index, (name, value) in enumerate(kwargs["headers"].items())
    ]
    assert client.handle_status_webhook(**kwargs) is True


def test_event_id_with_underscores_dots_and_colons(client: BillingClient) -> None:
    document = payload(5, event_id="sevt_prj.a:b-c_5")
    assert client.handle_status_webhook(**request(document)) is True


def test_unknown_body_field_is_ignored_and_not_stored(client: BillingClient, db_path: Path) -> None:
    assert client.handle_status_webhook(**request(payload(5, unit_cost="1.00"))) is True

    row = status_rows(db_path)[(TENANT, PROJECT)]
    assert "unit_cost" not in row
    assert "event_id" not in row


# --- 版本规则 ---------------------------------------------------------------------------


def test_older_and_same_versions_return_false(client: BillingClient, db_path: Path) -> None:
    assert client.handle_status_webhook(**request(payload(5))) is True
    # 同版本即使内容不同也不覆盖；旧版本忽略。都不是错误。
    assert client.handle_status_webhook(**request(payload(5, "ALLOW_AI"))) is False
    assert client.handle_status_webhook(**request(payload(4, "ALLOW_AI"))) is False

    row = status_rows(db_path)[(TENANT, PROJECT)]
    assert (row["status_version"], row["effective_status"]) == (5, "BLOCK_AI")
    assert client.handle_status_webhook(**request(payload(6, "ALLOW_AI"))) is True
    assert client.should_block_ai() is False


# --- 验签失败 ---------------------------------------------------------------------------


def _drop(name: str) -> Callable[[dict[str, Any]], None]:
    def change(kwargs: dict[str, Any]) -> None:
        del kwargs["headers"][name]

    return change


def _set(name: str, value: str) -> Callable[[dict[str, Any]], None]:
    def change(kwargs: dict[str, Any]) -> None:
        kwargs["headers"][name] = value

    return change


def _duplicate(name: str) -> Callable[[dict[str, Any]], None]:
    def change(kwargs: dict[str, Any]) -> None:
        pairs = list(kwargs["headers"].items())
        value = kwargs["headers"][name]
        kwargs["headers"] = [*pairs, (name.lower(), value)]

    return change


def _flip_signature(kwargs: dict[str, Any]) -> None:
    value = kwargs["headers"]["X-Acuven-Signature"]
    kwargs["headers"]["X-Acuven-Signature"] = ("1" if value[0] == "0" else "0") + value[1:]


def _tamper_body(kwargs: dict[str, Any]) -> None:
    kwargs["body"] = kwargs["body"].replace(b"BLOCK_AI", b"ALLOW_AI")


HEADER_FAILURES: list[tuple[str, Callable[[dict[str, Any]], None]]] = [
    *[
        (f"missing {name}", _drop(name))
        for name in (
            "X-Acuven-Timestamp",
            "X-Acuven-Signature",
            "X-Acuven-Event-ID",
            "X-Acuven-Key-Version",
        )
    ],
    *[
        (f"duplicate {name}", _duplicate(name))
        for name in (
            "X-Acuven-Timestamp",
            "X-Acuven-Signature",
            "X-Acuven-Event-ID",
            "X-Acuven-Key-Version",
        )
    ],
    ("wrong signature", _flip_signature),
    ("body changed after signing", _tamper_body),
    (
        "uppercase signature",
        lambda k: k["headers"].update(
            {"X-Acuven-Signature": k["headers"]["X-Acuven-Signature"].upper()}
        ),
    ),
    ("short signature", _set("X-Acuven-Signature", "ab" * 31)),
    ("timestamp with sign", _set("X-Acuven-Timestamp", f"+{TS}")),
    ("timestamp padded", _set("X-Acuven-Timestamp", f"0{TS}")),
    ("timestamp fraction", _set("X-Acuven-Timestamp", f"{TS}.0")),
    ("timestamp empty", _set("X-Acuven-Timestamp", "")),
    ("key version zero", _set("X-Acuven-Key-Version", "0")),
    ("key version padded", _set("X-Acuven-Key-Version", "02")),
    ("key version negative", _set("X-Acuven-Key-Version", "-2")),
    ("key version text", _set("X-Acuven-Key-Version", "v2")),
    ("event id with space", _set("X-Acuven-Event-ID", "sevt x")),
    ("event id with slash", _set("X-Acuven-Event-ID", "sevt/x")),
    ("event id too long", _set("X-Acuven-Event-ID", "e" * 201)),
    ("event id empty", _set("X-Acuven-Event-ID", "")),
    ("method changed", lambda k: k.update({"method": "PUT"})),
    ("path changed", lambda k: k.update({"raw_path": PATH + "/x"})),
    ("query added", lambda k: k.update({"query_string": "a=1"})),
]


@pytest.mark.parametrize(
    "change", [c for _, c in HEADER_FAILURES], ids=[n for n, _ in HEADER_FAILURES]
)
def test_request_failures(
    change: Callable[[dict[str, Any]], None], client: BillingClient, db_path: Path
) -> None:
    kwargs = request(payload(5))
    change(kwargs)

    with pytest.raises(WebhookVerificationError):
        client.handle_status_webhook(**kwargs)
    assert status_rows(db_path) == {}


@pytest.mark.parametrize("offset", [301, -301, 10_000])
def test_outside_the_window_fails(
    offset: int, client: BillingClient, clock: FakeClock, db_path: Path
) -> None:
    clock.now = T0 + offset
    with pytest.raises(WebhookVerificationError):
        client.handle_status_webhook(**request(payload(5)))
    assert status_rows(db_path) == {}


def test_unknown_key_version_fails(client: BillingClient, db_path: Path) -> None:
    # 用的是某个密钥签的，但版本 3 不在配置里。
    with pytest.raises(WebhookVerificationError):
        client.handle_status_webhook(**request(payload(5), version=3, secret=NEW_SECRET))
    # 版本与密钥对不上。
    with pytest.raises(WebhookVerificationError):
        client.handle_status_webhook(**request(payload(5), version=1, secret=NEW_SECRET))
    assert status_rows(db_path) == {}


def test_only_the_new_version_after_rotation(
    make_client: Callable[..., BillingClient], db_path: Path
) -> None:
    client = make_client(db_path, webhook_secrets={2: NEW_SECRET})
    with pytest.raises(WebhookVerificationError):
        client.handle_status_webhook(**request(payload(5), version=1))
    assert client.handle_status_webhook(**request(payload(5), version=2)) is True


def test_no_webhook_secrets_rejects_everything(
    make_client: Callable[..., BillingClient], db_path: Path
) -> None:
    client = make_client(db_path, webhook_secrets={})
    with pytest.raises(WebhookVerificationError):
        client.handle_status_webhook(**request(payload(5)))
    assert status_rows(db_path) == {}


def _without(name: str) -> dict[str, Any]:
    document = payload(5)
    del document[name]
    return document


BODY_FAILURES: list[tuple[str, Any]] = [
    *[(f"missing {name}", _without(name)) for name in payload(5)],
    ("not an object", [payload(5)]),
    ("tenant_id a number", payload(5, tenant_id=7)),
    ("billing_mode null", payload(5, billing_mode=None)),
    ("effective_at a number", payload(5, effective_at=1)),
    ("event_id a number", payload(5, event_id=5)),
    ("reason_code a list", payload(5, reason_code=["x"])),
    ("effective_status unknown", payload(5, effective_status="PAUSE_AI")),
    ("status_version bool", payload(5, status_version=True)),
    ("status_version string", payload(5, status_version="5")),
    ("status_version float", payload(5, status_version=5.0)),
    ("status_version negative", payload(5, status_version=-1)),
    ("status_version above 2^63 - 1", payload(5, status_version=2**63)),
    ("status_version huge", payload(5, status_version=10**40)),
    ("tenant mismatch", payload(5, tenant_id="ten_other")),
    ("project mismatch", payload(5, project_id="prj_other")),
]


@pytest.mark.parametrize(
    "document", [d for _, d in BODY_FAILURES], ids=[n for n, _ in BODY_FAILURES]
)
def test_body_failures(document: Any, client: BillingClient, db_path: Path) -> None:
    # 签名正确，只有请求体不合规。
    kwargs = request(body=encode(document), event_id="sevt_prj_a_5")

    with pytest.raises(WebhookVerificationError):
        client.handle_status_webhook(**kwargs)
    assert status_rows(db_path) == {}


@pytest.mark.parametrize("body", [b"", b"not json", b"\xff\xfe", b"[" * 100_000])
def test_unparseable_bodies_fail(body: bytes, client: BillingClient) -> None:
    with pytest.raises(WebhookVerificationError):
        client.handle_status_webhook(**request(body=body, event_id="sevt_prj_a_5"))


def test_body_event_id_must_match_the_header(client: BillingClient, db_path: Path) -> None:
    kwargs = request(payload(5), event_id="sevt_prj_a_6")
    with pytest.raises(WebhookVerificationError):
        client.handle_status_webhook(**kwargs)
    assert status_rows(db_path) == {}


@pytest.mark.parametrize("field", ["tenant_id", "project_id"])
def test_tenant_or_project_mismatch_is_rejected(
    field: str, client: BillingClient, db_path: Path
) -> None:
    with pytest.raises(WebhookVerificationError):
        client.handle_status_webhook(**request(payload(5, **{field: "someone_else"})))
    assert status_rows(db_path) == {}
    assert client.should_block_ai() is False


# --- 与对账的关系 -----------------------------------------------------------------------


def test_webhook_block_applies_while_reconcile_fails(
    client: BillingClient, clock: FakeClock, db_path: Path
) -> None:
    assert client.reconcile_status_once() is False
    assert client.should_block_ai() is False

    clock.now = T0 + 30
    assert client.handle_status_webhook(**request(payload(5), timestamp=str(TS + 30))) is True
    assert client.should_block_ai() is True

    clock.now = T0 + 270
    assert client.reconcile_status_once() is False
    assert client.should_block_ai() is True

    assert client.reset_status(reason="center restored") is True
    assert client.should_block_ai() is False


def test_webhook_does_not_pose_as_a_reconcile(
    make_client: Callable[..., BillingClient], clock: FakeClock, db_path: Path
) -> None:
    answers = iter(
        [
            Response(
                200,
                json.dumps(
                    {"data": {k: v for k, v in payload(3, "ALLOW_AI").items() if k != "event_id"}}
                ).encode(),
            )
        ]
    )
    transport = FakeTransport(lambda: next(answers, Response(503, b"")))
    client = make_client(db_path, transport)
    assert client.reconcile_status_once() is True
    clock.now = T0 + 240
    assert client.reconcile_status_once() is False
    before = reconcile_rows(db_path)
    assert before == [
        {"tenant_id": TENANT, "project_id": PROJECT, "last_ok_at": T0, "last_error_at": T0 + 240}
    ]

    clock.now = T0 + 250
    assert client.handle_status_webhook(**request(payload(5), timestamp=str(TS + 250))) is True

    # 状态被应用；对账时刻不变，节奏也不变。
    assert status_rows(db_path)[(TENANT, PROJECT)]["status_version"] == 5
    assert reconcile_rows(db_path) == before
    assert client.next_reconcile_at == T0 + 270
    assert transport.calls == 2


def test_webhook_on_a_fresh_outbox_writes_no_reconcile_row(
    client: BillingClient, db_path: Path
) -> None:
    assert client.handle_status_webhook(**request(payload(5))) is True
    assert reconcile_rows(db_path) == []
    assert client.next_reconcile_at is None


def test_webhook_compares_versions_of_the_configured_pair_only(
    make_client: Callable[..., BillingClient], clock: FakeClock, db_path: Path
) -> None:
    Store(db_path, clock=clock).close()
    conn = sqlite3.connect(db_path, isolation_level=None)
    try:
        conn.execute(
            "INSERT INTO status_state VALUES ('ten_old', 'prj_old', 'PREPAID', 'SUSPENDED',"
            " 'ENABLED', 'ENABLED', 'BLOCK_AI', 99, 'BALANCE_NOT_POSITIVE',"
            " '2026-10-01T00:00:00', ?)",
            (T0 - 100,),
        )
    finally:
        conn.close()
    client = make_client(db_path)
    assert client.should_block_ai() is False

    assert client.handle_status_webhook(**request(payload(1, "ALLOW_AI"))) is True

    rows = status_rows(db_path)
    assert rows[(TENANT, PROJECT)]["status_version"] == 1
    assert rows[("ten_old", "prj_old")]["status_version"] == 99
    assert rows[("ten_old", "prj_old")]["effective_status"] == "BLOCK_AI"
    assert client.should_block_ai() is False


# --- 写库失败与降级 ---------------------------------------------------------------------


@pytest.mark.parametrize(("name", "degrades"), [("SQLITE_IOERR", True), ("SQLITE_BUSY", False)])
def test_store_failure_raises_status_store_error(
    name: str,
    degrades: bool,
    client: BillingClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fault(monkeypatch, "apply_status", lambda: sqlite_error(name))

    with pytest.raises(StatusStoreError) as caught:
        client.handle_status_webhook(**request(payload(5)))
    assert not isinstance(caught.value, WebhookVerificationError)
    assert client.store_available is (not degrades)


def test_degraded_client_verifies_then_raises_status_store_error(
    make_client: Callable[..., BillingClient], tmp_path: Path
) -> None:
    client = make_client(tmp_path / "absent" / "outbox.db")
    assert not client.store_available

    # 验签不过仍是 WebhookVerificationError；验签通过才是 StatusStoreError。
    bad = request(payload(5))
    _flip_signature(bad)
    with pytest.raises(WebhookVerificationError):
        client.handle_status_webhook(**bad)
    with pytest.raises(StatusStoreError):
        client.handle_status_webhook(**request(payload(5)))
    assert client.should_block_ai() is False

"""Request signing and the webhook signature check (design gate #571 v24 §2「签名」, §7).

对照向量照抄 `tests/backend/test_integration_auth.py` 的全零占位输入与期望签名
（不 import 那个文件，也不 import 服务端）。规则的唯一出处是 `docs/api.md`「集成请求签名」。

⚠️ 示例 secret 与 api_key 一律是全零占位值：仓库是公开的。
"""

from __future__ import annotations

import hashlib
import hmac
from pathlib import Path

import pytest
from acuven_billing_client import (
    BillingClientConfig,
    canonical_request,
    request_headers,
    sign,
    verify_webhook_signature,
)

SECRET = "sk_" + "0" * 64
WEBHOOK_SECRET = "whs_" + "0" * 64
WEBHOOK_SECRET_NEXT = "whs_" + "1" * 64
TIMESTAMP = "1790325000"
REQUEST_ID = "00000000-0000-4000-8000-000000000000"
EMPTY_BODY_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
REFERENCE_PATH = "/api/v1/usage-events?source=chatbot&batch=7"
REFERENCE_CANONICAL = (
    "POST\n"
    "/api/v1/usage-events?batch=7&source=chatbot\n"
    "1790325000\n"
    "00000000-0000-4000-8000-000000000000\n"
    "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
)
REFERENCE_SIGNATURE = "903f7fc026621f3e2ff5eb51ef329293c8627b6fbb5053e36c72c8d8ce445199"

CONFIG = BillingClientConfig(
    base_url="https://billing.example.test",
    api_key="ak_" + "0" * 32,
    key_version=2,
    secret=SECRET,
    tenant_id="00000000-0000-4000-8000-000000000001",
    project_id="00000000-0000-4000-8000-000000000002",
    outbox_path="/var/lib/app/outbox.db",
)


def path_line(path_and_query: str) -> str:
    return canonical_request("GET", path_and_query, TIMESTAMP, REQUEST_ID, b"").split("\n")[1]


# --- 对照向量 ---------------------------------------------------------------------


def test_reference_canonical_request() -> None:
    assert canonical_request("post", REFERENCE_PATH, TIMESTAMP, REQUEST_ID, b"") == (
        REFERENCE_CANONICAL
    )


def test_reference_signature() -> None:
    assert sign(SECRET, REFERENCE_CANONICAL) == REFERENCE_SIGNATURE
    assert (
        hmac.new(SECRET.encode(), REFERENCE_CANONICAL.encode(), hashlib.sha256).hexdigest()
        == REFERENCE_SIGNATURE
    )


def test_reference_request_headers() -> None:
    headers = request_headers(
        CONFIG, "POST", REFERENCE_PATH, b"", timestamp=1_790_325_000, request_id=REQUEST_ID
    )
    assert headers == {
        "X-Acuven-Api-Key": "ak_" + "0" * 32,
        "X-Acuven-Key-Version": "2",
        "X-Acuven-Timestamp": "1790325000",
        "X-Acuven-Request-Id": REQUEST_ID,
        "X-Acuven-Signature": REFERENCE_SIGNATURE,
    }


# --- 规范串 -----------------------------------------------------------------------


def test_canonical_request_has_five_lines_without_trailing_newline() -> None:
    canonical = canonical_request("post", "/x", TIMESTAMP, REQUEST_ID, b"{}")
    assert canonical.split("\n") == [
        "POST",
        "/x",
        TIMESTAMP,
        REQUEST_ID,
        hashlib.sha256(b"{}").hexdigest(),
    ]
    assert not canonical.endswith("\n")


def test_method_is_uppercased() -> None:
    assert canonical_request("get", "/x", TIMESTAMP, REQUEST_ID, b"").startswith("GET\n")
    assert canonical_request("Post", "/x", TIMESTAMP, REQUEST_ID, b"").startswith("POST\n")


def test_empty_body_is_hashed() -> None:
    assert canonical_request("GET", "/x", TIMESTAMP, REQUEST_ID, b"").endswith(EMPTY_BODY_SHA256)


def test_timestamp_and_request_id_are_verbatim() -> None:
    lines = canonical_request("GET", "/x", "0001790325000", "req id", b"").split("\n")
    assert lines[2] == "0001790325000"
    assert lines[3] == "req id"


@pytest.mark.parametrize(
    ("path_and_query", "expected"),
    [
        # docs/api.md 的例子
        (REFERENCE_PATH, "/api/v1/usage-events?batch=7&source=chatbot"),
        # 没有查询串；只有 `?`
        ("/api/v1/usage-events", "/api/v1/usage-events"),
        ("/api/v1/usage-events?", "/api/v1/usage-events"),
        # 路径原样：不折叠 `.` / `..`、不去尾部斜杠、不解码
        ("/a/./b/../c/", "/a/./b/../c/"),
        ("/a%2Fb/%7E?z=1", "/a%2Fb/%7E?z=1"),
        # 重复键：按值排
        ("/x?b=2&b=1&a=3", "/x?a=3&b=1&b=2"),
        # 没有 `=` 的段：值为空串；键值都相同时按整段排
        ("/x?b&a=1&a", "/x?a&a=1&b"),
        ("/x?a=&a", "/x?a&a="),
        # 在第一个 `=` 处切开
        ("/x?a=b=c&a=b", "/x?a=b&a=b=c"),
        # 空值不丢
        ("/x?b=&a=", "/x?a=&b="),
        # 百分号编码原样保留，按编码后的字节排
        ("/x?x=%20&a=%41&%61=1", "/x?%61=1&a=%41&x=%20"),
        # 字节序：大写字母排在小写之前
        ("/x?b=1&B=1&a=1", "/x?B=1&a=1&b=1"),
        ("/x?k=b&k=B", "/x?k=B&k=b"),
        # 非 ASCII 按 UTF-8 字节
        ("/x?é=1&z=1&a=1", "/x?a=1&z=1&é=1"),
        # 空段原样参与排序
        ("/x?b=1&&a=1", "/x?&a=1&b=1"),
        # 只有第一个 `?` 分隔路径与查询串
        ("/x?b=?&a=1", "/x?a=1&b=?"),
    ],
)
def test_query_normalization(path_and_query: str, expected: str) -> None:
    assert path_line(path_and_query) == expected


def test_sign_is_lowercase_hex_hmac_sha256() -> None:
    signature = sign(SECRET, "anything")
    assert signature == hmac.new(SECRET.encode(), b"anything", hashlib.sha256).hexdigest()
    assert len(signature) == 64
    assert signature == signature.lower()


# --- 请求头 -----------------------------------------------------------------------


def test_request_headers_with_body() -> None:
    body = b'{"events":[]}'
    headers = request_headers(
        CONFIG,
        "POST",
        "/api/v1/integration/usage-events/batch",
        body,
        timestamp=1_790_325_000,
        request_id="01JBZX6Q0000000000000000AB",
    )
    assert headers["Content-Type"] == "application/json"
    assert set(headers) == {
        "X-Acuven-Api-Key",
        "X-Acuven-Key-Version",
        "X-Acuven-Timestamp",
        "X-Acuven-Request-Id",
        "X-Acuven-Signature",
        "Content-Type",
    }
    assert headers["X-Acuven-Signature"] == sign(
        SECRET,
        canonical_request(
            "POST",
            "/api/v1/integration/usage-events/batch",
            "1790325000",
            "01JBZX6Q0000000000000000AB",
            body,
        ),
    )


def test_request_headers_without_body_have_no_content_type() -> None:
    headers = request_headers(
        CONFIG,
        "GET",
        "/api/v1/integration/effective-status",
        b"",
        timestamp=1,
        request_id=REQUEST_ID,
    )
    assert "Content-Type" not in headers
    assert headers["X-Acuven-Timestamp"] == "1"


def test_request_headers_do_not_contain_the_secret() -> None:
    headers = request_headers(CONFIG, "GET", "/x", b"", timestamp=1, request_id=REQUEST_ID)
    assert SECRET not in "".join(headers.values())


@pytest.mark.parametrize("timestamp", [True, 1.5, "1790325000", -1, None])
def test_request_headers_reject_non_integer_timestamps(timestamp: object) -> None:
    with pytest.raises(ValueError):
        request_headers(CONFIG, "GET", "/x", b"", timestamp=timestamp, request_id=REQUEST_ID)


def test_signing_module_does_not_read_the_clock_or_make_ids() -> None:
    source = (
        Path(__file__).resolve().parents[1] / "acuven_billing_client" / "signing.py"
    ).read_text(encoding="utf-8")
    for forbidden in ("import time", "datetime", "import secrets", "new_ulid", "uuid"):
        assert forbidden not in source


# --- webhook 验签 -----------------------------------------------------------------

WEBHOOK = {
    "secrets": {1: WEBHOOK_SECRET},
    "key_version": 1,
    "method": "POST",
    "path_and_query": "/hooks/billing?b=2&a=1",
    "timestamp": TIMESTAMP,
    "event_id": "01JBZX6Q0000000000000000AB",
    "body": b'{"event_id":"01JBZX6Q0000000000000000AB"}',
}


def webhook_signature(secret: str = WEBHOOK_SECRET, **changes: object) -> str:
    request = {**WEBHOOK, **changes}
    canonical = "\n".join(
        (
            str(request["method"]).upper(),
            "/hooks/billing?a=1&b=2",
            str(request["timestamp"]),
            str(request["event_id"]),
            hashlib.sha256(request["body"]).hexdigest(),  # type: ignore[arg-type]
        )
    )
    return hmac.new(secret.encode(), canonical.encode(), hashlib.sha256).hexdigest()


def verify(signature: object, **changes: object) -> bool:
    return verify_webhook_signature(**{**WEBHOOK, **changes, "signature": signature})


def test_webhook_signature_verifies() -> None:
    assert verify(webhook_signature()) is True


def test_webhook_uses_event_id_as_the_fourth_line() -> None:
    signature = sign(
        WEBHOOK_SECRET,
        canonical_request(
            "POST",
            "/hooks/billing?b=2&a=1",
            TIMESTAMP,
            "01JBZX6Q0000000000000000AB",
            WEBHOOK["body"],  # type: ignore[arg-type]
        ),
    )
    assert signature == webhook_signature()
    assert verify(signature) is True


def test_webhook_timestamp_is_used_verbatim() -> None:
    signature = webhook_signature(timestamp="01790325000")
    assert verify(signature, timestamp="01790325000") is True
    assert verify(signature) is False


def test_webhook_rotation_accepts_both_versions() -> None:
    secrets = {1: WEBHOOK_SECRET, 2: WEBHOOK_SECRET_NEXT}
    assert verify(webhook_signature(), secrets=secrets, key_version=1) is True
    assert verify(webhook_signature(WEBHOOK_SECRET_NEXT), secrets=secrets, key_version=2) is True
    assert verify(webhook_signature(), secrets=secrets, key_version=2) is False


@pytest.mark.parametrize(
    "changes",
    [
        {"method": "PUT"},
        {"path_and_query": "/hooks/billing?a=1"},
        {"timestamp": "1790325001"},
        {"event_id": "01JBZX6Q0000000000000000AC"},
        {"body": b"{}"},
    ],
)
def test_webhook_tampering_fails(changes: dict[str, object]) -> None:
    assert verify(webhook_signature(), **changes) is False


@pytest.mark.parametrize("key_version", [2, 0, -1, True, "1", 1.0, None])
def test_webhook_unknown_or_invalid_version_fails(key_version: object) -> None:
    # True == 1 且哈希相同：不先挡掉 bool 就会取到版本 1 的密钥。
    assert verify(webhook_signature(), key_version=key_version) is False


def test_webhook_empty_secrets_fail() -> None:
    assert verify(webhook_signature(), secrets={}) is False


@pytest.mark.parametrize(
    "transform",
    [
        str.upper,
        lambda value: value[:-1],
        lambda value: value + "0",
        lambda value: " " + value[1:],
        lambda value: value[:-1] + "g",
        lambda value: value[:-1] + "é",
        lambda value: value.encode(),
        lambda value: None,
    ],
)
def test_webhook_malformed_signature_fails(transform) -> None:
    assert verify(transform(webhook_signature())) is False


@pytest.mark.parametrize(
    "changes",
    [
        {"secrets": None},
        {"secrets": [WEBHOOK_SECRET]},
        {"secrets": {1: None}},
        {"secrets": {1: b"whs_"}},
        {"secrets": {1: "\ud800"}},
        {"method": None},
        {"path_and_query": None},
        {"timestamp": 1790325000},
        {"timestamp": None},
        {"event_id": None},
        {"body": "not bytes"},
        {"body": None},
    ],
)
def test_webhook_never_raises(changes: dict[str, object]) -> None:
    assert verify(webhook_signature(), **changes) is False

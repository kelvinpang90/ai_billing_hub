"""Request signing library (spec §37; design gate #118 v1 §2「校验库」, §7).

纯函数，不连库。`find_verifiable_credential` 要查库，在 test_integration_access_service.py。

⚠️ 示例 secret 与 api_key 一律是全零占位值（设计 §2「格式」）：仓库是公开的。
"""

from __future__ import annotations

import calendar
import datetime as dt
import hashlib
import hmac
import inspect
import secrets

import pytest

from app.services import integration_auth
from app.services.integration_auth import (
    BAD_SIGNATURE,
    MALFORMED_TIMESTAMP,
    MAX_SKEW_SECONDS,
    TIMESTAMP_OUT_OF_WINDOW,
    SignatureRejected,
    canonical_request,
    credential_aad,
    sign,
    verify_signature,
)

SECRET = "sk_" + "0" * 64
NOW = dt.datetime(2026, 9, 25, 8, 30, 0)
# 2026-09-25T08:30:00Z 的 Unix 纪元秒。
TIMESTAMP = "1790325000"
REQUEST_ID = "00000000-0000-4000-8000-000000000000"
# SHA256 of the empty string.
EMPTY_BODY_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"

REQUEST = {
    "method": "POST",
    "path_and_query": "/api/v1/usage-events?source=chatbot&batch=7",
    "timestamp": TIMESTAMP,
    "request_id": REQUEST_ID,
    "body": b'{"event_id":"00000000-0000-4000-8000-000000000000"}',
}


def signed(**changes: object) -> str:
    request = {**REQUEST, **changes}
    return sign(SECRET, canonical_request(**request))  # type: ignore[arg-type]


def verify(signature: str, *, now: dt.datetime = NOW, **changes: object) -> None:
    request = {**REQUEST, **changes}
    verify_signature(SECRET, signature=signature, now=now, **request)  # type: ignore[arg-type]


def rejection(signature: str, **changes: object) -> str:
    with pytest.raises(SignatureRejected) as raised:
        verify(signature, **changes)
    return raised.value.code


# --- 对照向量（以后 Billing Client 的实现拿它比对） --------------------------------


def test_the_fixed_timestamp_is_the_epoch_second_of_now() -> None:
    assert calendar.timegm(NOW.timetuple()) == int(TIMESTAMP)


def test_the_reference_canonical_request() -> None:
    """§37 的五行，`\\n` 连接；查询串已排序，请求体是 SHA256 的小写十六进制。"""
    canonical = canonical_request(
        "post", "/api/v1/usage-events?source=chatbot&batch=7", TIMESTAMP, REQUEST_ID, b""
    )

    assert canonical == (
        "POST\n"
        "/api/v1/usage-events?batch=7&source=chatbot\n"
        "1790325000\n"
        "00000000-0000-4000-8000-000000000000\n"
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    )


def test_the_reference_signature_is_plain_hmac_sha256_of_the_canonical_string() -> None:
    """期望值是写死的字面值（用 openssl 独立算出，不经过 `sign`）。

    密钥是整个 `sk_…` 字符串的 UTF-8 字节，输出小写十六进制。
    """
    canonical = (
        "POST\n"
        "/api/v1/usage-events?batch=7&source=chatbot\n"
        "1790325000\n"
        "00000000-0000-4000-8000-000000000000\n"
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    )
    expected = "903f7fc026621f3e2ff5eb51ef329293c8627b6fbb5053e36c72c8d8ce445199"

    assert hmac.new(SECRET.encode(), canonical.encode(), hashlib.sha256).hexdigest() == expected
    assert signed(body=b"") == expected
    verify(expected, body=b"")


def test_a_correct_signature_verifies() -> None:
    verify(signed())


# --- 篡改 -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "changes",
    [
        {"method": "PUT"},
        {"path_and_query": "/api/v1/usage-events/x?source=chatbot&batch=7"},
        {"path_and_query": "/api/v1/usage-events?source=chatbot&batch=8"},
        {"path_and_query": "/api/v1/usage-events?source=chatbot"},
        {"body": b'{"event_id":"00000000-0000-4000-8000-000000000001"}'},
        {"request_id": "00000000-0000-4000-8000-000000000001"},
        # 仍在时间窗内的另一个时间戳：签名对不上。
        {"timestamp": str(int(TIMESTAMP) + 1)},
    ],
    ids=["method", "path", "query-value", "query-dropped", "body-byte", "request-id", "timestamp"],
)
def test_any_tampering_is_a_bad_signature(changes: dict) -> None:
    signature = signed()

    assert rejection(signature, **changes) == BAD_SIGNATURE


def test_one_changed_signature_character_is_a_bad_signature() -> None:
    signature = signed()
    flipped = signature[:-1] + ("0" if signature[-1] != "0" else "1")

    assert rejection(flipped) == BAD_SIGNATURE


def test_another_secret_is_a_bad_signature() -> None:
    other = sign("sk_" + secrets.token_hex(32), canonical_request(**REQUEST))  # type: ignore[arg-type]

    assert rejection(other) == BAD_SIGNATURE


def test_a_timestamp_far_outside_the_window_is_out_of_window_not_bad() -> None:
    late = str(int(TIMESTAMP) + 3600)

    assert rejection(signed(timestamp=late), timestamp=late) == TIMESTAMP_OUT_OF_WINDOW


# --- 规范化 ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("/p?b=2&a=1", "/p?a=1&b=2"),
        # 重复键按值排序。
        ("/p?a=2&a=1", "/p?a=1&a=2"),
        # 空值与没有等号的键都保留，不丢。
        ("/p?b&a=", "/p?a=&b"),
        # 没有查询串时不带 `?`。
        ("/p?", "/p"),
    ],
    ids=["order", "duplicate-keys", "empty-values", "bare-question-mark"],
)
def test_requests_that_normalise_alike_sign_alike(first: str, second: str) -> None:
    left = canonical_request("GET", first, TIMESTAMP, REQUEST_ID, b"")
    right = canonical_request("GET", second, TIMESTAMP, REQUEST_ID, b"")

    assert left == right
    assert sign(SECRET, left) == sign(SECRET, right)


@pytest.mark.parametrize(
    ("raw", "normalised"),
    [
        ("/p?b=2&a=1", "/p?a=1&b=2"),
        ("/p?a=2&a=1", "/p?a=1&a=2"),
        ("/p?b&a=", "/p?a=&b"),
        # 字节序：大写字母排在小写之前。
        ("/p?b=1&B=1", "/p?B=1&b=1"),
        # 百分号编码原样保留，不解码、不改大小写。
        ("/p?q=a%2fb&p=%20", "/p?p=%20&q=a%2fb"),
        # 路径原样：不解码、不折叠 `..`、不去尾斜杠。
        ("/a/../b%2F/", "/a/../b%2F/"),
        ("/p?", "/p"),
        ("/p", "/p"),
    ],
)
def test_the_normalised_path_and_query(raw: str, normalised: str) -> None:
    lines = canonical_request("GET", raw, TIMESTAMP, REQUEST_ID, b"").split("\n")

    assert lines[1] == normalised


def test_requests_that_differ_after_normalising_sign_differently() -> None:
    left = canonical_request("GET", "/p?a=1", TIMESTAMP, REQUEST_ID, b"")
    right = canonical_request("GET", "/p/?a=1", TIMESTAMP, REQUEST_ID, b"")

    assert sign(SECRET, left) != sign(SECRET, right)


def test_the_empty_body_is_hashed_too() -> None:
    lines = canonical_request("get", "/p", TIMESTAMP, REQUEST_ID, b"").split("\n")

    assert lines == ["GET", "/p", TIMESTAMP, REQUEST_ID, EMPTY_BODY_SHA256]
    body = b'{"a":1}'
    with_body = canonical_request("POST", "/p", TIMESTAMP, REQUEST_ID, body).split("\n")
    assert with_body[4] == hashlib.sha256(body).hexdigest()


# --- 时间窗 ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("offset", "outcome"),
    [
        (0, None),
        (MAX_SKEW_SECONDS, None),
        (-MAX_SKEW_SECONDS, None),
        (MAX_SKEW_SECONDS + 1, TIMESTAMP_OUT_OF_WINDOW),
        (-(MAX_SKEW_SECONDS + 1), TIMESTAMP_OUT_OF_WINDOW),
    ],
    ids=["exact", "300", "-300", "301", "-301"],
)
def test_the_time_window_is_plus_minus_five_minutes(offset: int, outcome: str | None) -> None:
    timestamp = str(int(TIMESTAMP) + offset)
    signature = signed(timestamp=timestamp)

    if outcome is None:
        verify(signature, timestamp=timestamp)
    else:
        assert rejection(signature, timestamp=timestamp) == outcome


@pytest.mark.parametrize(
    "timestamp",
    [
        "1.5",
        "+100",
        "-100",
        "abc",
        "",
        " 1790325000",
        "01790325000",
        "１７９０３２５０００",
        "1e9",
    ],
    ids=[
        "decimal",
        "plus",
        "minus",
        "letters",
        "empty",
        "space",
        "leading-zero",
        "fullwidth",
        "exponent",
    ],
)
def test_a_malformed_timestamp_is_refused_before_anything_else(timestamp: str) -> None:
    signature = signed(timestamp=timestamp)

    assert rejection(signature, timestamp=timestamp) == MALFORMED_TIMESTAMP


# --- 签名格式与常量时间比较 -------------------------------------------------------


@pytest.mark.parametrize(
    "mangle",
    [
        str.upper,
        lambda value: value[:-1],
        lambda value: value + "0",
        lambda value: "z" + value[1:],
    ],
    ids=["uppercase", "short", "long", "not-hex"],
)
def test_a_malformed_signature_is_a_bad_signature(mangle) -> None:
    signature = mangle(signed())

    assert rejection(signature) == BAD_SIGNATURE


def test_signatures_are_compared_in_constant_time() -> None:
    source = inspect.getsource(integration_auth.verify_signature)

    assert "hmac.compare_digest(" in source
    assert "== signature" not in source
    assert "signature ==" not in source


def test_a_rejection_carries_only_its_code() -> None:
    """异常的 str / repr 里只有问题码：没有 secret、签名或请求内容。"""
    signature = signed()
    flipped = signature[:-1] + ("0" if signature[-1] != "0" else "1")
    with pytest.raises(SignatureRejected) as raised:
        verify(flipped)

    for text in (str(raised.value), repr(raised.value)):
        assert SECRET not in text
        assert flipped not in text
        assert BAD_SIGNATURE in text


def test_the_library_does_not_log() -> None:
    source = inspect.getsource(integration_auth)

    assert "logging" not in source
    assert "logger" not in source


# --- AAD ------------------------------------------------------------------------


def test_the_associated_data_names_the_table_the_key_and_the_version() -> None:
    api_key = "ak_" + "0" * 32

    assert credential_aad(api_key, 1) == b"integration_credentials|" + api_key.encode() + b"|1"
    assert credential_aad(api_key, 1) != credential_aad(api_key, 2)
    assert credential_aad(api_key, 1) != credential_aad("ak_" + secrets.token_hex(16), 1)

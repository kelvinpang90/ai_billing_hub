"""Status webhook verification (design gate #571 v24 §2「状态 webhook」, §6).

AIH-TASK-169：五步里的前四步（取请求头、时间窗、验签、载荷校验）；第五步按版本应用在门面
`BillingClient.handle_status_webhook`。任何一步不过都抛 `WebhookVerificationError`
（接入方回 401）；验签通过而写库失败时门面抛 `StatusStoreError`（接入方回 503）。

- 请求头名字不区分大小写；`headers` 可以是映射或键值对序列，名字与值可以是 `str` 或
  `bytes`（按 latin-1 逐字节解码）。四个签名相关请求头任何一个出现多次都算格式错误。
- `raw_path` 与 `query_string` 同样接受 `str` 或 `bytes`；`signed_path` 给出时代替它们参与
  规范串第二行，仍按查询串规则排序。
- 时间窗用 `int(clock())` 与请求头时间戳比较（服务端同为整秒），恰好 300 秒接受。
- 载荷与对账共用 `status.validate_status`，另要求 `event_id` 等于请求头的 Event-ID。

⚠️ 异常消息与日志不带密钥、签名、请求头原值与请求体（§6「日志」）。
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from typing import Any, Final

from .config import BillingClientConfig
from .signing import verify_webhook_signature
from .status import StatusValidationError, validate_status

MAX_SKEW_SECONDS: Final = 300
# 与请求签名相同：十进制、不带小数与正负号、不补零。
_TIMESTAMP: Final = re.compile(r"0|[1-9][0-9]{0,18}")
_KEY_VERSION: Final = re.compile(r"[1-9][0-9]{0,18}")
_SIGNATURE: Final = re.compile(r"[0-9a-f]{64}")
# 服务端发的是 sevt_ 加项目对外 id 加版本号。
_EVENT_ID: Final = re.compile(r"[A-Za-z0-9_.:-]{1,200}")

_TIMESTAMP_HEADER: Final = "x-acuven-timestamp"
_SIGNATURE_HEADER: Final = "x-acuven-signature"
_EVENT_ID_HEADER: Final = "x-acuven-event-id"
_KEY_VERSION_HEADER: Final = "x-acuven-key-version"
_SIGNING_HEADERS: Final = frozenset(
    {_TIMESTAMP_HEADER, _SIGNATURE_HEADER, _EVENT_ID_HEADER, _KEY_VERSION_HEADER}
)


class WebhookVerificationError(Exception):
    """The request is not a valid status webhook for this client; answer 401."""


class StatusStoreError(Exception):
    """The webhook verified but the status could not be stored; answer 503."""


def _text(value: object, what: str) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, bytes | bytearray | memoryview):
        return bytes(value).decode("latin-1")
    raise WebhookVerificationError(f"{what} must be str or bytes")


def _header_items(headers: object) -> Iterable[Any]:
    if isinstance(headers, Mapping):
        return headers.items()
    if isinstance(headers, str | bytes | bytearray | memoryview) or not isinstance(
        headers, Iterable
    ):
        raise WebhookVerificationError("headers must be a mapping or a sequence of pairs")
    return headers


def _signing_headers(headers: object) -> dict[str, str]:
    """The four signing headers by lowercase name; a repeated one is malformed."""
    found: dict[str, str] = {}
    for item in _header_items(headers):
        try:
            name, value = item
        except (TypeError, ValueError):
            raise WebhookVerificationError("headers must be name and value pairs") from None
        key = _text(name, "header name").lower()
        if key not in _SIGNING_HEADERS:
            continue
        if key in found:
            raise WebhookVerificationError(f"header {key} appears more than once")
        found[key] = _text(value, "header value")
    missing = sorted(_SIGNING_HEADERS - found.keys())
    if missing:
        raise WebhookVerificationError("missing headers: " + ", ".join(missing))
    return found


def _path_and_query(raw_path: object, query_string: object, signed_path: object) -> str:
    if signed_path is not None:
        return _text(signed_path, "signed_path")
    path = _text(raw_path, "raw_path")
    query = "" if query_string is None else _text(query_string, "query_string")
    return path + "?" + query if query else path


def verify_status_webhook(
    config: BillingClientConfig,
    *,
    now: int,
    method: str | bytes,
    raw_path: str | bytes,
    query_string: str | bytes,
    headers: object,
    body: bytes,
    signed_path: str | None = None,
) -> dict[str, Any]:
    """Steps 1–4; returns the 10 status fields to apply. Raises `WebhookVerificationError`.

    `now` 是 `int(clock())`。
    """
    if not config.webhook_secrets:
        raise WebhookVerificationError("no webhook secrets are configured")

    # 1. 请求头。
    found = _signing_headers(headers)
    timestamp = found[_TIMESTAMP_HEADER]
    signature = found[_SIGNATURE_HEADER]
    event_id = found[_EVENT_ID_HEADER]
    key_version = found[_KEY_VERSION_HEADER]
    if _TIMESTAMP.fullmatch(timestamp) is None:
        raise WebhookVerificationError("X-Acuven-Timestamp is malformed")
    if _SIGNATURE.fullmatch(signature) is None:
        raise WebhookVerificationError("X-Acuven-Signature is malformed")
    if _EVENT_ID.fullmatch(event_id) is None:
        raise WebhookVerificationError("X-Acuven-Event-ID is malformed")
    if _KEY_VERSION.fullmatch(key_version) is None:
        raise WebhookVerificationError("X-Acuven-Key-Version is malformed")

    # 2. 时间窗。
    if abs(int(now) - int(timestamp)) > MAX_SKEW_SECONDS:
        raise WebhookVerificationError("X-Acuven-Timestamp is outside the 300 second window")

    # 3. 验签。
    if not isinstance(body, bytes | bytearray | memoryview):
        raise WebhookVerificationError("body must be bytes")
    raw_body = bytes(body)
    if not verify_webhook_signature(
        secrets=config.webhook_secrets,
        key_version=int(key_version),
        method=_text(method, "method"),
        path_and_query=_path_and_query(raw_path, query_string, signed_path),
        timestamp=timestamp,
        event_id=event_id,
        body=raw_body,
        signature=signature,
    ):
        raise WebhookVerificationError("signature does not verify")

    # 4. 载荷。
    try:
        document = json.loads(raw_body)
    except (ValueError, RecursionError):
        raise WebhookVerificationError("body is not JSON") from None
    if not isinstance(document, dict):
        raise WebhookVerificationError("body is not an object")
    if "event_id" not in document:
        raise WebhookVerificationError("status fields missing: event_id")
    if not isinstance(document["event_id"], str):
        raise WebhookVerificationError("event_id must be a string")
    try:
        fields = validate_status(document, config)
    except StatusValidationError as exc:
        raise WebhookVerificationError(str(exc)) from None
    if document["event_id"] != event_id:
        raise WebhookVerificationError("event_id does not match X-Acuven-Event-ID")
    return fields


__all__ = [
    "MAX_SKEW_SECONDS",
    "StatusStoreError",
    "WebhookVerificationError",
    "verify_status_webhook",
]

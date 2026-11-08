"""Status webhook verification (design gate #571 v24 §2「状态 webhook」, §5, §6).

`verify_status_webhook` 做设计的前四步：取四个签名相关请求头（大小写不敏感）→ 时间窗
→ 按 `X-Acuven-Key-Version` 取 webhook 密钥验签（规范串第四行是 Event-ID）→ 解析请求体并
按与对账相同的规则校验（另加 `event_id`，须等于请求头）。任何一步不过抛
`WebhookVerificationError`（接入方回 401）。第五步按版本应用在门面（`client.py`），
验签通过而写库失败时抛 `StatusStoreError`（接入方回 503）。

- `raw_path`、`query_string` 接受 str 或 bytes（bytes 按 latin-1 逐字节解码）；`headers`
  接受映射或键值对序列，名字与值可以是 str 或 bytes（同样按 latin-1）。同名的签名相关请求头
  出现多次算格式错误。
- 时间戳与 `X-Acuven-Key-Version` 用与请求签名相同的格式（十进制、不带正负号、不补零）；
  时间窗用 `int(now)` 比较，相差恰好 300 秒接受。
- `signed_path` 给出时代替 `raw_path` 与 `query_string` 参与规范串第二行（仍按查询串规则排序）。

⚠️ 异常消息只点名请求头或字段，不带请求头的值、签名、密钥与请求体（§6「日志」）。
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from typing import Any, Final

from .config import BillingClientConfig
from .signing import verify_webhook_signature
from .status import StatusValidationError, parse_status

MAX_SKEW_SECONDS: Final = 300

TIMESTAMP_HEADER: Final = "x-acuven-timestamp"
SIGNATURE_HEADER: Final = "x-acuven-signature"
EVENT_ID_HEADER: Final = "x-acuven-event-id"
KEY_VERSION_HEADER: Final = "x-acuven-key-version"
_SIGNED_HEADERS: Final = (TIMESTAMP_HEADER, SIGNATURE_HEADER, EVENT_ID_HEADER, KEY_VERSION_HEADER)

# ⚠️ 用 [0-9] 而不是 \d：\d 还认全角与其他文字的数字。
_TIMESTAMP: Final = re.compile(r"0|[1-9][0-9]{0,17}")
_KEY_VERSION: Final = re.compile(r"[1-9][0-9]{0,18}")
_SIGNATURE: Final = re.compile(r"[0-9a-f]{64}")
# 服务端发的是 `sevt_<项目 public_id>_<status_version>`。
_EVENT_ID: Final = re.compile(r"[A-Za-z0-9_.:-]{1,200}")


class WebhookVerificationError(Exception):
    """The webhook request did not verify; reply non-2xx (401). Nothing was applied."""


class StatusStoreError(Exception):
    """The webhook verified but the status could not be stored; reply 503 so it is resent."""


def _text(value: object, what: str) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, bytes | bytearray | memoryview):
        return bytes(value).decode("latin-1")
    raise TypeError(f"{what} must be str or bytes")


def _signed_headers(headers: Mapping[Any, Any] | Iterable[Any]) -> dict[str, str]:
    """The four signing headers by lowercase name; a repeated one is a format error."""
    items = headers.items() if isinstance(headers, Mapping) else headers
    found: dict[str, str] = {}
    for item in items:
        try:
            name, value = item
        except (TypeError, ValueError):
            raise TypeError(
                "headers must be a mapping or a sequence of (name, value) pairs"
            ) from None
        key = _text(name, "header name").lower()
        if key not in _SIGNED_HEADERS:
            continue
        if key in found:
            raise WebhookVerificationError(f"header {key} appears more than once")
        found[key] = _text(value, "header value")
    for key in _SIGNED_HEADERS:
        if key not in found:
            raise WebhookVerificationError(f"header {key} is missing")
    return found


def _path_and_query(raw_path: object, query_string: object, signed_path: object) -> str:
    if signed_path is not None:
        return _text(signed_path, "signed_path")
    path = _text(raw_path, "raw_path")
    query = _text(query_string, "query_string") if query_string is not None else ""
    return path + "?" + query if query else path


def verify_status_webhook(
    config: BillingClientConfig,
    *,
    now: float,
    method: str,
    raw_path: str | bytes,
    query_string: str | bytes,
    headers: Mapping[Any, Any] | Iterable[Any],
    body: bytes,
    signed_path: str | None = None,
) -> dict[str, Any]:
    """Steps 1–4. Returns the 10 status fields plus `event_id`; raises on any failure."""
    if not isinstance(method, str):
        raise TypeError("method must be str")
    if not isinstance(body, bytes | bytearray | memoryview):
        raise TypeError("body must be bytes")
    body = bytes(body)
    path_and_query = _path_and_query(raw_path, query_string, signed_path)
    if not config.webhook_secrets:
        raise WebhookVerificationError("no webhook secrets are configured")

    # 1. 请求头。
    found = _signed_headers(headers)
    timestamp = found[TIMESTAMP_HEADER]
    signature = found[SIGNATURE_HEADER]
    event_id = found[EVENT_ID_HEADER]
    key_version = found[KEY_VERSION_HEADER]
    if _TIMESTAMP.fullmatch(timestamp) is None:
        raise WebhookVerificationError(f"header {TIMESTAMP_HEADER} is malformed")
    if _SIGNATURE.fullmatch(signature) is None:
        raise WebhookVerificationError(f"header {SIGNATURE_HEADER} is malformed")
    if _EVENT_ID.fullmatch(event_id) is None:
        raise WebhookVerificationError(f"header {EVENT_ID_HEADER} is malformed")
    if _KEY_VERSION.fullmatch(key_version) is None:
        raise WebhookVerificationError(f"header {KEY_VERSION_HEADER} is malformed")

    # 2. 时间窗：与服务端同为整秒，恰好 300 秒接受。
    if abs(int(now) - int(timestamp)) > MAX_SKEW_SECONDS:
        raise WebhookVerificationError("timestamp is outside the 300 second window")

    # 3. 验签：版本不在配置里也只是验签失败。
    if not verify_webhook_signature(
        secrets=config.webhook_secrets,
        key_version=int(key_version),
        method=method,
        path_and_query=path_and_query,
        timestamp=timestamp,
        event_id=event_id,
        body=body,
        signature=signature,
    ):
        raise WebhookVerificationError("signature does not verify")

    # 4. 请求体：与对账同一套校验，另要 event_id 等于请求头。
    try:
        document = json.loads(body)
    except (ValueError, RecursionError):
        raise WebhookVerificationError("body is not JSON") from None
    try:
        fields = parse_status(document, config, extra_fields=("event_id",))
    except StatusValidationError as exc:
        raise WebhookVerificationError(str(exc)) from None
    if fields["event_id"] != event_id:
        raise WebhookVerificationError("event_id does not match the header")
    return fields


__all__ = [
    "MAX_SKEW_SECONDS",
    "StatusStoreError",
    "WebhookVerificationError",
    "verify_status_webhook",
]

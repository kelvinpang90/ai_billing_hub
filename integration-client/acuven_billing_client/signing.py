"""Request signing and the webhook signature check (design gate #571 v24 §2「签名」).

规范串逐条照 `docs/api.md`「集成请求签名」（唯一出处）：五行用 `\\n` 连接、
末尾无换行；大写方法；路径原样；查询串按键、值、整段的 UTF-8 字节序排序；
没有查询串（含只有一个 `?`）时不带 `?`；空请求体也算 SHA-256。

⚠️ 不能复用 `app/ai_api/client_rules.py` 的规范串：它不排序查询串、不大写方法。

本模块不读时钟、不生成 id：时间戳与请求 id 由调用方传入。
"""

from __future__ import annotations

import hashlib
import hmac
import re
from collections.abc import Mapping
from typing import Final

from .config import BillingClientConfig

_SIGNATURE: Final = re.compile(r"[0-9a-f]{64}")


def _normalized_path_and_query(path_and_query: str) -> str:
    path, _, query = path_and_query.partition("?")
    if not query:
        return path

    def order(segment: str) -> tuple[bytes, bytes, bytes]:
        key, _, value = segment.partition("=")
        # 最后一项让「a」与「a=」这类键值都相同的段也有确定的先后。
        return key.encode("utf-8"), value.encode("utf-8"), segment.encode("utf-8")

    return path + "?" + "&".join(sorted(query.split("&"), key=order))


def canonical_request(
    method: str, path_and_query: str, timestamp: str, request_id: str, body: bytes
) -> str:
    """The five lines joined by `\\n`. `timestamp` and `request_id` are the header values."""
    return "\n".join(
        (
            method.upper(),
            _normalized_path_and_query(path_and_query),
            timestamp,
            request_id,
            hashlib.sha256(body).hexdigest(),
        )
    )


def sign(secret: str, canonical: str) -> str:
    """HMAC-SHA256 of the canonical string, lowercase hex. Key: the whole secret string."""
    return hmac.new(secret.encode("utf-8"), canonical.encode("utf-8"), hashlib.sha256).hexdigest()


def request_headers(
    config: BillingClientConfig,
    method: str,
    path_and_query: str,
    body: bytes,
    *,
    timestamp: int,
    request_id: str,
) -> dict[str, str]:
    """The five signing headers, plus `Content-Type` when there is a body."""
    if isinstance(timestamp, bool) or not isinstance(timestamp, int) or timestamp < 0:
        raise ValueError("timestamp must be a non-negative integer of Unix seconds")
    # 十进制、不带小数与正负号、不补零。
    timestamp_header = str(timestamp)
    signature = sign(
        config.secret,
        canonical_request(method, path_and_query, timestamp_header, request_id, body),
    )
    headers = {
        "X-Acuven-Api-Key": config.api_key,
        "X-Acuven-Key-Version": str(config.key_version),
        "X-Acuven-Timestamp": timestamp_header,
        "X-Acuven-Request-Id": request_id,
        "X-Acuven-Signature": signature,
    }
    if body:
        headers["Content-Type"] = "application/json"
    return headers


def verify_webhook_signature(
    *,
    secrets: Mapping[int, str],
    key_version: int,
    method: str,
    path_and_query: str,
    timestamp: str,
    event_id: str,
    body: bytes,
    signature: str,
) -> bool:
    """Look up the secret by version, compute, compare in constant time. Never raises.

    规范串的第四行是 `X-Acuven-Event-ID`；`timestamp` 是请求头原值，原样进规范串。
    请求头解析、时间窗与载荷校验不在这里（`webhook.py`）。
    """
    try:
        if isinstance(key_version, bool) or not isinstance(key_version, int):
            return False
        if not isinstance(signature, str) or _SIGNATURE.fullmatch(signature) is None:
            return False
        if key_version not in secrets:
            return False
        secret = secrets[key_version]
        if not isinstance(secret, str):
            return False
        expected = sign(
            secret, canonical_request(method, path_and_query, timestamp, event_id, body)
        )
        return hmac.compare_digest(expected.encode("ascii"), signature.encode("ascii"))
    except Exception:
        # 任何输入（类型不对、编码不了的字符等）都只算验签失败。
        return False


__all__ = ["canonical_request", "request_headers", "sign", "verify_webhook_signature"]

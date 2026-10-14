"""Pure rules for AI usage-event delivery."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from datetime import UTC, datetime
from typing import Final

INGEST_PATH: Final = "/api/v1/integration/usage-events"
MODEL_PATTERN: Final = r"^[A-Za-z0-9][A-Za-z0-9._:/@-]{0,127}$"
MAX_TOKENS: Final = 10**12
_ALPHABET: Final = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_RECEIPT_STATUSES = {202: {"accepted"}, 200: {"already_received", "already_processed"}}


def canonical_request(method: str, path: str, timestamp: str, attempt_id: str, body: bytes) -> str:
    return "\n".join((method, path, timestamp, attempt_id, hashlib.sha256(body).hexdigest()))


def sign(secret: str, canonical: str) -> str:
    return hmac.new(secret.encode("utf-8"), canonical.encode("utf-8"), hashlib.sha256).hexdigest()


def receipt_acknowledges(status_code: int, document, event_id: str) -> bool:
    try:
        receipt = document["data"]
        return (
            receipt["event_id"] == event_id and receipt["status"] in _RECEIPT_STATUSES[status_code]
        )
    except (KeyError, TypeError):
        return False


def error_outcome(document) -> tuple[str, bool]:
    try:
        code = document.get("error", {}).get("code", "BILLING_ERROR")
        retryable = document.get("retryable")
    except (AttributeError, TypeError):
        return "INVALID_BILLING_RESPONSE", False
    if not isinstance(code, str):
        code = "BILLING_ERROR"
    return code[:64], retryable is True


def new_ulid() -> str:
    value = ((time.time_ns() // 1_000_000) << 80) | secrets.randbits(80)
    return "".join(_ALPHABET[(value >> shift) & 31] for shift in range(125, -1, -5))


def signed_headers(
    api_key: str,
    key_version: int,
    secret: str,
    body: bytes,
    *,
    timestamp: str,
    attempt_id: str,
    method: str = "POST",
    path: str = INGEST_PATH,
) -> dict[str, str]:
    signature = sign(secret, canonical_request(method, path, timestamp, attempt_id, body))
    return {
        "X-Acuven-Api-Key": api_key,
        "X-Acuven-Key-Version": str(key_version),
        "X-Acuven-Timestamp": timestamp,
        "X-Acuven-Request-Id": attempt_id,
        "X-Acuven-Signature": signature,
        "Content-Type": "application/json",
    }


def usage_payload(
    *,
    tenant_id: str,
    project_id: str,
    feature: str,
    request_id: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_creation_input_tokens: int,
    cache_read_input_tokens: int,
    occurred_at: datetime | None = None,
    event_id: str | None = None,
) -> dict:
    moment = occurred_at or datetime.now(UTC)
    return {
        "schema_version": "1.0",
        "event_id": event_id or new_ulid(),
        "request_id": request_id,
        "feature": feature,
        "tenant_id": tenant_id,
        "project_id": project_id,
        "provider": "anthropic",
        "model": model,
        "usage_type": "LLM_TOKEN",
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_creation_input_tokens": cache_creation_input_tokens,
        "cache_read_input_tokens": cache_read_input_tokens,
        "occurred_at": moment.astimezone(UTC)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z"),
    }

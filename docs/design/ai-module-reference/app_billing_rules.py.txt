"""Pure rules of ai_billing_hub's integration contract.

Standard library only, so the OpenClaw Worker sandbox (no network, no ssl) can test them.
"""

from __future__ import annotations

import hashlib
import hmac

_RECEIPT_STATUSES = {202: {"accepted"}, 200: {"already_received", "already_processed"}}


def canonical_request(method: str, path: str, timestamp: str, attempt_id: str, body: bytes) -> str:
    return "\n".join((method, path, timestamp, attempt_id, hashlib.sha256(body).hexdigest()))


def sign(secret: str, canonical: str) -> str:
    return hmac.new(secret.encode("utf-8"), canonical.encode("utf-8"), hashlib.sha256).hexdigest()


def receipt_acknowledges(status_code: int, document, event_id: str) -> bool:
    """Only a receipt naming this exact event, with a status valid for the HTTP code, is a delivery."""
    try:
        receipt = document["data"]
        return receipt["event_id"] == event_id and receipt["status"] in _RECEIPT_STATUSES[status_code]
    except (KeyError, TypeError):
        return False


def error_outcome(document) -> tuple[str, bool]:
    """Return (error code, retryable). Only the server's explicit retryable flag authorizes a retry."""
    try:
        code = document.get("error", {}).get("code", "BILLING_ERROR")
        retryable = document.get("retryable")
    except (AttributeError, TypeError):
        return "INVALID_BILLING_RESPONSE", False
    if not isinstance(code, str):
        code = "BILLING_ERROR"
    return code[:64], retryable is True


def status_allows_ai(data, tenant_id: str, project_id: str) -> bool:
    """Raise ValueError unless the status is verifiably this tenant's and project's."""
    try:
        valid = (
            data["tenant_id"] == tenant_id
            and data["project_id"] == project_id
            and data["effective_status"] in {"ALLOW_AI", "BLOCK_AI"}
            # bool is a subclass of int; a JSON true/false is not a version.
            and type(data["status_version"]) is int
        )
    except (KeyError, TypeError) as error:
        raise ValueError("unverifiable billing status") from error
    if not valid:
        raise ValueError("unverifiable billing status")
    return data["effective_status"] == "ALLOW_AI"

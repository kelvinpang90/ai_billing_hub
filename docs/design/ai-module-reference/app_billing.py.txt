"""ai_billing_hub's signed single-event ingestion contract."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlsplit

import httpx

from app.billing_rules import canonical_request, error_outcome, receipt_acknowledges, sign, status_allows_ai
from app.config import ClientBinding
from app.ids import new_ulid
from app.provider import Generation

INGEST_PATH = "/api/v1/integration/usage-events"
STATUS_PATH = "/api/v1/integration/effective-status"


class BillingStatusUnavailable(Exception):
    pass


def usage_payload(binding: ClientBinding, result: Generation, *, request_id: str) -> dict:
    return {
        "schema_version": "1.0",
        "event_id": new_ulid(),
        "request_id": request_id,
        "tenant_id": binding.tenant_id,
        "project_id": binding.billing_project_id,
        "provider": "anthropic",
        "model": result.model,
        "usage_type": "LLM_TOKEN",
        "input_tokens": result.input_tokens,
        "output_tokens": result.output_tokens,
        "cache_creation_input_tokens": result.cache_creation_input_tokens,
        "cache_read_input_tokens": result.cache_read_input_tokens,
        "occurred_at": datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z"),
    }


def signed_headers(
    binding: ClientBinding,
    body: bytes,
    *,
    timestamp: str,
    attempt_id: str,
    method: str = "POST",
    path: str = INGEST_PATH,
) -> dict[str, str]:
    signature = sign(binding.billing_secret, canonical_request(method, path, timestamp, attempt_id, body))
    return {
        "X-Acuven-Api-Key": binding.billing_api_key,
        "X-Acuven-Key-Version": str(binding.billing_key_version),
        "X-Acuven-Timestamp": timestamp,
        "X-Acuven-Request-Id": attempt_id,
        "X-Acuven-Signature": signature,
        "Content-Type": "application/json",
    }


@dataclass(frozen=True)
class DeliveryResult:
    delivered: bool
    retryable: bool
    error_code: str | None = None


class BillingClient:
    def __init__(self, base_url: str, *, transport: httpx.BaseTransport | None = None) -> None:
        parsed = urlsplit(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.path.rstrip("/") or parsed.query or parsed.fragment:
            raise ValueError("billing base URL must be an origin without a path")
        if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError("billing API requires HTTPS outside local development")
        self.base_url = base_url.rstrip("/")
        self.transport = transport

    def allows_ai(self, binding: ClientBinding) -> bool:
        """Read live status; raise BillingStatusUnavailable so the caller can fail open per invariant 1."""
        headers = signed_headers(
            binding,
            b"",
            timestamp=str(int(datetime.now(UTC).timestamp())),
            attempt_id=new_ulid(),
            method="GET",
            path=STATUS_PATH,
        )
        try:
            with httpx.Client(timeout=5, transport=self.transport) as client:
                response = client.get(self.base_url + STATUS_PATH, headers=headers)
            if response.status_code != 200:
                raise BillingStatusUnavailable
            return status_allows_ai(response.json()["data"], binding.tenant_id, binding.billing_project_id)
        except (httpx.RequestError, ValueError, TypeError, KeyError) as error:
            raise BillingStatusUnavailable from error

    def send(self, binding: ClientBinding, payload_json: str) -> DeliveryResult:
        body = payload_json.encode("utf-8")
        try:
            event_id = json.loads(payload_json)["event_id"]
            if not isinstance(event_id, str):
                raise ValueError("invalid event_id")
        except (ValueError, KeyError, TypeError):
            return DeliveryResult(False, False, "INVALID_LOCAL_EVENT")
        headers = signed_headers(
            binding,
            body,
            timestamp=str(int(datetime.now(UTC).timestamp())),
            attempt_id=new_ulid(),  # Fresh on every retry; event_id and body never change.
        )
        try:
            with httpx.Client(timeout=10, transport=self.transport) as client:
                response = client.post(self.base_url + INGEST_PATH, headers=headers, content=body)
        except httpx.RequestError:
            return DeliveryResult(False, True, "NETWORK_ERROR")
        try:
            document = response.json()
        except ValueError:
            document = None
        if response.status_code in {200, 202}:
            if receipt_acknowledges(response.status_code, document, event_id):
                return DeliveryResult(True, False)
            return DeliveryResult(False, True, "INVALID_BILLING_RECEIPT")
        code, retryable = error_outcome(document)
        return DeliveryResult(False, retryable, code)


def payload_json(payload: dict) -> str:
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False)

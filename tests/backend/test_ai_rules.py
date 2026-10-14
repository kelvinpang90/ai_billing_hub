from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import hmac
import json

import httpx2
import pytest
from pydantic import ValidationError

from app.ai_api.client_rules import (
    MAX_TOKENS,
    MODEL_PATTERN,
    canonical_request,
    error_outcome,
    receipt_acknowledges,
    sign,
    signed_headers,
    usage_payload,
)
from app.ai_api.messages import MessageRequest, request_body_is_within_limit
from app.ai_api.provider import (
    AnthropicProvider,
    ProviderRejectedRequest,
    ProviderUnavailable,
    read_anthropic_key,
)
from app.schemas.usage_ingest import MAX_TOKENS as INGEST_MAX_TOKENS
from app.schemas.usage_ingest import MODEL_PATTERN as INGEST_MODEL_PATTERN
from app.services.integration_auth import verify_signature


def request(**overrides: object) -> MessageRequest:
    body: dict[str, object] = {
        "feature": "crm.contact_summary",
        "model": "claude-sonnet-4-6",
        "messages": [{"role": "user", "content": "hello"}],
        "max_tokens": 4000,
    }
    body.update(overrides)
    return MessageRequest.model_validate(body)


@pytest.mark.parametrize(
    "body",
    [
        {},
        {
            "feature": "CRM.summary",
            "model": "m",
            "messages": [{"role": "user", "content": "x"}],
            "max_tokens": 1,
        },
        {"feature": "crm.summary", "model": "m", "messages": [], "max_tokens": 1},
        {
            "feature": "crm.summary",
            "model": "m",
            "messages": [{"role": "assistant", "content": "x"}],
            "max_tokens": 1,
        },
        {
            "feature": "crm.summary",
            "model": "m",
            "messages": [{"role": "user", "content": "x"}],
            "max_tokens": True,
        },
        {
            "feature": "crm.summary",
            "model": "m",
            "messages": [{"role": "user", "content": "x"}],
            "max_tokens": 1,
            "tools": [],
        },
    ],
)
def test_message_request_rejects_invalid_shapes(body: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        MessageRequest.model_validate(body)


def test_max_tokens_and_raw_body_boundaries() -> None:
    request().validate_max_tokens(4000)
    with pytest.raises(ValueError):
        request().validate_max_tokens(3999)
    assert request_body_is_within_limit(b"x" * 65536, 65536)
    assert not request_body_is_within_limit(b"x" * 65537, 65536)


def test_billing_rules_and_cross_signature() -> None:
    body = b'{"event_id":"01J00000000000000000000000"}'
    canonical = canonical_request(
        "POST", "/api/v1/integration/usage-events", "1700000000", "attempt-1", body
    )
    assert canonical == "\n".join(
        (
            "POST",
            "/api/v1/integration/usage-events",
            "1700000000",
            "attempt-1",
            hashlib.sha256(body).hexdigest(),
        )
    )
    assert (
        sign("secret", canonical)
        == hmac.new(b"secret", canonical.encode(), hashlib.sha256).hexdigest()
    )
    headers = signed_headers(
        "ak_test", 1, "secret", body, timestamp="1700000000", attempt_id="attempt-1"
    )
    verify_signature(
        "secret",
        method="POST",
        path_and_query="/api/v1/integration/usage-events",
        timestamp=headers["X-Acuven-Timestamp"],
        request_id=headers["X-Acuven-Request-Id"],
        body=body,
        signature=headers["X-Acuven-Signature"],
        now=dt.datetime.fromtimestamp(1700000000, tz=dt.UTC),
    )


def test_receipts_errors_and_usage_payload() -> None:
    event_id = "01J00000000000000000000000"
    assert receipt_acknowledges(
        202, {"data": {"event_id": event_id, "status": "accepted"}}, event_id
    )
    assert not receipt_acknowledges(
        202, {"data": {"event_id": "other", "status": "accepted"}}, event_id
    )
    assert error_outcome({"error": {"code": "TEMP"}, "retryable": True}) == ("TEMP", True)
    assert error_outcome(None) == ("INVALID_BILLING_RESPONSE", False)
    payload = usage_payload(
        tenant_id="tenant-public",
        project_id="project-public",
        feature="crm.contact_summary",
        request_id=event_id,
        model="claude-sonnet-4-6",
        input_tokens=1,
        output_tokens=2,
        cache_creation_input_tokens=3,
        cache_read_input_tokens=4,
        event_id=event_id,
        occurred_at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
    )
    assert payload["feature"] == "crm.contact_summary"
    assert payload["tenant_id"] == "tenant-public"
    assert payload["project_id"] == "project-public"


def run(coroutine):
    return asyncio.run(coroutine)


def provider_with(response):
    return AnthropicProvider("test-key", transport=httpx2.MockTransport(response))


def test_provider_forwards_only_the_allowed_subset_and_joins_text() -> None:
    seen = []

    def respond(request):
        seen.append(json.loads(request.content))
        return httpx2.Response(
            200,
            json={
                "model": "claude-sonnet-4-6",
                "content": [
                    {"type": "text", "text": "one"},
                    {"type": "tool_use", "text": "skip"},
                    {"type": "text", "text": "two"},
                ],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 1, "output_tokens": 2},
            },
        )

    result = run(
        provider_with(respond).generate(
            request(system="caller system", output_config={"effort": "low"}),
            timeout=1,
            wall_clock_budget=1,
        )
    )
    assert result.text == "one\ntwo"
    assert seen == [
        {
            "model": "claude-sonnet-4-6",
            "max_tokens": 4000,
            "system": "caller system",
            "messages": [{"role": "user", "content": "hello"}],
            "output_config": {"effort": "low"},
        }
    ]


@pytest.mark.parametrize("status", [400, 404, 413, 422])
def test_provider_classifies_rejected_requests(status: int) -> None:
    def respond(_request):
        return httpx2.Response(
            status, json={"type": "error", "error": {"type": "invalid_request_error"}}
        )

    with pytest.raises(ProviderRejectedRequest, match="invalid_request_error"):
        run(provider_with(respond).generate(request(), timeout=1, wall_clock_budget=1))


@pytest.mark.parametrize(
    "response",
    [
        lambda _request: httpx2.Response(429),
        lambda _request: httpx2.Response(401),
        lambda _request: httpx2.Response(403),
        lambda _request: httpx2.Response(500),
        lambda _request: httpx2.Response(302),
        lambda _request: httpx2.Response(418),
        lambda _request: httpx2.Response(200, content=b"not json"),
        lambda _request: httpx2.Response(
            200, json={"usage": {"input_tokens": 1, "output_tokens": 1}}
        ),
        lambda _request: httpx2.Response(200, json={"model": "claude-sonnet-4-6", "content": []}),
    ],
)
def test_provider_classifies_unavailable_responses(response) -> None:
    with pytest.raises(ProviderUnavailable):
        run(provider_with(response).generate(request(), timeout=1, wall_clock_budget=1))


def test_provider_classifies_network_errors_as_unavailable() -> None:
    def fail(request):
        raise httpx2.ConnectError("unavailable", request=request)

    with pytest.raises(ProviderUnavailable):
        run(provider_with(fail).generate(request(), timeout=1, wall_clock_budget=1))


def test_provider_keeps_empty_text_and_stop_reasons() -> None:
    def respond(_request):
        return httpx2.Response(
            200,
            json={
                "model": "claude-sonnet-4-6",
                "content": "not-a-list",
                "stop_reason": "refusal",
                "usage": {
                    "input_tokens": 1,
                    "output_tokens": 2,
                    "cache_creation_input_tokens": None,
                },
            },
        )

    result = run(provider_with(respond).generate(request(), timeout=1, wall_clock_budget=1))
    assert result.text == ""
    assert result.stop_reason == "refusal"
    assert result.cache_creation_input_tokens == 0


@pytest.mark.parametrize(
    "usage",
    [
        {"input_tokens": MAX_TOKENS + 1, "output_tokens": 1},
        {"input_tokens": -1, "output_tokens": 1},
        {"input_tokens": True, "output_tokens": 1},
        {"input_tokens": 1.5, "output_tokens": 1},
        {"input_tokens": 1, "output_tokens": 1, "cache_read_input_tokens": "1"},
    ],
)
def test_provider_rejects_unreadable_usage(usage: dict[str, object]) -> None:
    def respond(_request):
        return httpx2.Response(
            200, json={"model": "claude-sonnet-4-6", "content": [], "usage": usage}
        )

    with pytest.raises(ProviderUnavailable):
        run(provider_with(respond).generate(request(), timeout=1, wall_clock_budget=1))


def test_provider_wall_clock_budget_cuts_off_slow_transport() -> None:
    async def slow(_request):
        await asyncio.sleep(0.05)
        return httpx2.Response(200, json={})

    with pytest.raises(ProviderUnavailable):
        run(provider_with(slow).generate(request(), timeout=1, wall_clock_budget=0.001))


def test_provider_constants_match_ingestion_contract() -> None:
    assert MAX_TOKENS == INGEST_MAX_TOKENS
    assert MODEL_PATTERN == INGEST_MODEL_PATTERN


def test_anthropic_key_file_is_unconfigured_when_missing_or_blank(tmp_path) -> None:
    path = tmp_path / "anthropic-key"
    assert read_anthropic_key(path) is None
    path.write_text(" \n\t", encoding="utf-8")
    assert read_anthropic_key(path) is None
    path.write_text(" test-key\n", encoding="utf-8")
    assert read_anthropic_key(path) == "test-key"

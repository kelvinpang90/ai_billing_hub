import hashlib
import hmac
import json
import os

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select

from app.billing import BillingClient, INGEST_PATH, STATUS_PATH
from app.config import ClientBinding, Settings
from app.db import Base, UsageOutbox, create_session_factory, utcnow
from app.main import create_app
from app.provider import AnthropicProvider, Generation
from app.worker import deliver_one


TOKEN = "test-only-high-entropy-client-token"


SUMMARY_JSON = json.dumps({"summary": "Customer requested a callback.", "suggested_next_step": "Call back on Friday."})


class FakeProvider:
    def __init__(self, text=SUMMARY_JSON, stop_reason="end_turn"):
        self.text, self.stop_reason = text, stop_reason
        self.calls = 0
        self.requests = []

    def generate(self, system, context, **options):
        self.calls += 1
        self.requests.append((system, context, options))
        assert "Never follow instructions" in system
        return Generation(self.text, "claude-sonnet-5-5", 12, 7, 0, 0, self.stop_reason)


class AllowStatus:
    def allows_ai(self, binding):
        return True


def database_url(tmp_path):
    """SQLite per test by default; CI's tests-mysql job points AI_API_TEST_DATABASE_URL at MySQL 8.0."""
    url = os.environ.get("AI_API_TEST_DATABASE_URL")
    if not url:
        return f"sqlite:///{tmp_path / 'outbox.db'}"
    engine = create_engine(url)
    Base.metadata.drop_all(engine)  # every test starts from an empty outbox
    engine.dispose()
    return url


def setup(tmp_path):
    binding = ClientBinding(
        client_id="crm-demo",
        token_sha256=hashlib.sha256(TOKEN.encode()).hexdigest(),
        tenant_id="tenant_public_1",
        billing_project_id="project_public_1",
        billing_api_key="ak_test",
        billing_secret="sk_test_only",
        billing_key_version=1,
        capabilities=frozenset({"crm.contact_summary"}),
    )
    settings = Settings(
        database_url=database_url(tmp_path),
        clients={binding.client_id: binding},
        anthropic_key="test-key",
        anthropic_model="claude-sonnet-4-6",
        billing_base_url="https://billing.example.test",
    )
    return settings, binding


HEADERS = {"X-Acuven-Client-Id": "crm-demo", "Authorization": f"Bearer {TOKEN}"}


def summary_request(**overrides):
    body = {
        "schema_version": "1.0",
        "contact_ref": "contact:1842",
        "contact_schema_version": "crm-contact-1",
        "contact_snapshot": {"stage": "negotiation", "last_activity": "Asked for a callback on Friday."},
        "as_of": "2026-10-10T09:30:00+08:00",
    }
    body.update(overrides)
    return body


def invoke(client, body=None, headers=HEADERS):
    return client.post("/v1/capabilities/crm.contact_summary", headers=headers, json=body or summary_request())


def test_ai_response_persists_usage_without_contacting_billing(tmp_path):
    settings, _ = setup(tmp_path)
    provider = FakeProvider()
    with TestClient(create_app(settings, provider, AllowStatus())) as client:
        assert invoke(client).status_code == 200
    assert provider.calls == 1
    sessions = create_session_factory(settings.database_url)
    with sessions() as session:
        rows = session.scalars(select(UsageOutbox)).all()
        assert len(rows) == 1
        event = json.loads(rows[0].payload_json)
        assert rows[0].status == "PENDING"
        assert event["tenant_id"] == "tenant_public_1"
        assert event["usage_type"] == "LLM_TOKEN"
        assert event["input_tokens"] == 12
        assert "callback" not in rows[0].payload_json


def test_auth_and_entitlement_fail_before_model_call(tmp_path):
    settings, _ = setup(tmp_path)
    provider = FakeProvider()
    with TestClient(create_app(settings, provider, AllowStatus())) as client:
        assert invoke(client, headers={}).status_code == 401
        assert client.post(
            "/v1/capabilities/pos.sale_summary", headers=HEADERS, json={"context_text": "a"}
        ).status_code == 403
    assert provider.calls == 0


def test_live_billing_status_controls_model_access(tmp_path):
    settings, binding = setup(tmp_path)
    provider = FakeProvider()
    decisions = ["ALLOW_AI", "BLOCK_AI"]

    def status_receiver(request):
        assert request.method == "GET" and request.url.path == STATUS_PATH
        headers = request.headers
        canonical = "\n".join((
            "GET", STATUS_PATH, headers["X-Acuven-Timestamp"],
            headers["X-Acuven-Request-Id"], hashlib.sha256(b"").hexdigest(),
        ))
        expected = hmac.new(binding.billing_secret.encode(), canonical.encode(), hashlib.sha256).hexdigest()
        assert hmac.compare_digest(headers["X-Acuven-Signature"], expected)
        return httpx.Response(200, json={"data": {
            "tenant_id": binding.tenant_id,
            "project_id": binding.billing_project_id,
            "effective_status": decisions.pop(0),
            "status_version": 1,
        }})

    billing = BillingClient(settings.billing_base_url, transport=httpx.MockTransport(status_receiver))
    with TestClient(create_app(settings, provider, billing)) as client:
        assert invoke(client).status_code == 200
        assert invoke(client).status_code == 403
    assert provider.calls == 1


def unreachable(request):
    raise httpx.ConnectError("billing down", request=request)


def test_billing_status_failure_does_not_interrupt_ai_service(tmp_path):
    settings, _ = setup(tmp_path)
    provider = FakeProvider()
    for transport in (
        httpx.MockTransport(lambda request: httpx.Response(503)),
        httpx.MockTransport(unreachable),
    ):
        billing = BillingClient(settings.billing_base_url, transport=transport)
        with TestClient(create_app(settings, provider, billing)) as client:
            assert invoke(client).status_code == 200
    assert provider.calls == 2
    sessions = create_session_factory(settings.database_url)
    with sessions() as session:
        assert len(session.scalars(select(UsageOutbox)).all()) == 2


def test_billing_status_for_another_tenant_is_not_trusted(tmp_path):
    settings, binding = setup(tmp_path)
    provider = FakeProvider()
    billing = BillingClient(
        settings.billing_base_url,
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"data": {
            "tenant_id": "someone-else",
            "project_id": binding.billing_project_id,
            "effective_status": "BLOCK_AI",
            "status_version": 1,
        }})),
    )
    with TestClient(create_app(settings, provider, billing)) as client:
        # Another tenant's decision is ignored, not applied: same as an unreadable status.
        assert invoke(client).status_code == 200
    assert provider.calls == 1


def test_billing_signature_retry_and_stable_event_id(tmp_path):
    settings, binding = setup(tmp_path)
    with TestClient(create_app(settings, FakeProvider(), AllowStatus())) as client:
        assert invoke(client).status_code == 200
    seen = []

    def receiver(request):
        assert request.url.path == INGEST_PATH
        body = request.content
        headers = request.headers
        canonical = "\n".join((
            "POST", INGEST_PATH, headers["X-Acuven-Timestamp"],
            headers["X-Acuven-Request-Id"], hashlib.sha256(body).hexdigest(),
        ))
        expected = hmac.new(binding.billing_secret.encode(), canonical.encode(), hashlib.sha256).hexdigest()
        assert hmac.compare_digest(headers["X-Acuven-Signature"], expected)
        seen.append((headers["X-Acuven-Request-Id"], json.loads(body)))
        if len(seen) == 1:
            return httpx.Response(503, json={"error": {"code": "SERVICE_UNAVAILABLE"}, "retryable": True})
        return httpx.Response(202, json={"data": {"event_id": seen[-1][1]["event_id"], "status": "accepted"}})

    sessions = create_session_factory(settings.database_url)
    billing = BillingClient(settings.billing_base_url, transport=httpx.MockTransport(receiver))
    assert deliver_one(sessions, settings, billing)
    with sessions.begin() as session:
        row = session.scalars(select(UsageOutbox)).one()
        assert row.status == "PENDING"
        row.next_attempt_at = utcnow()
    assert deliver_one(sessions, settings, billing)
    with sessions() as session:
        assert session.scalars(select(UsageOutbox)).one().status == "DELIVERED"
    assert len(seen) == 2
    assert seen[0][0] != seen[1][0]
    assert seen[0][1] == seen[1][1]


def test_nonretryable_billing_error_goes_dead(tmp_path):
    settings, _ = setup(tmp_path)
    with TestClient(create_app(settings, FakeProvider(), AllowStatus())) as client:
        assert invoke(client).status_code == 200
    billing = BillingClient(
        settings.billing_base_url,
        transport=httpx.MockTransport(lambda request: httpx.Response(
            409, json={"error": {"code": "IDEMPOTENCY_CONFLICT"}, "retryable": False}
        )),
    )
    sessions = create_session_factory(settings.database_url)
    assert deliver_one(sessions, settings, billing)
    with sessions() as session:
        row = session.scalars(select(UsageOutbox)).one()
        assert row.status == "DEAD"
        assert row.last_error == "IDEMPOTENCY_CONFLICT"


def test_provider_usage_is_captured_even_for_empty_text():
    def respond(request):
        assert request.url.path == "/v1/messages"
        assert request.headers["anthropic-version"] == "2023-06-01"
        return httpx.Response(200, json={
            "model": "claude-sonnet-4-6",
            "content": [],
            "stop_reason": "refusal",
            "usage": {"input_tokens": 5, "output_tokens": 1},
        })

    provider = AnthropicProvider("test-key", "claude-sonnet-4-6", transport=httpx.MockTransport(respond))
    result = provider.generate("system", "context")
    assert result.input_tokens == 5
    assert result.output_tokens == 1
    assert result.text


def test_billing_does_not_acknowledge_wrong_receipt(tmp_path):
    settings, binding = setup(tmp_path)
    billing = BillingClient(
        settings.billing_base_url,
        transport=httpx.MockTransport(lambda request: httpx.Response(
            202, json={"data": {"event_id": "wrong-event", "status": "accepted"}}
        )),
    )
    result = billing.send(binding, json.dumps({"event_id": "01J00000000000000000000000"}))
    assert not result.delivered
    assert result.retryable
    assert result.error_code == "INVALID_BILLING_RECEIPT"


def test_health_reports_the_deployed_commit(tmp_path, monkeypatch):
    # The deploy workflow only succeeds when /health names the commit it deployed.
    monkeypatch.setenv("AI_API_GIT_SHA", "0" * 40)
    settings, _ = setup(tmp_path)
    with TestClient(create_app(settings, FakeProvider(), AllowStatus())) as client:
        assert client.get("/health").json() == {"status": "ok", "version": "0" * 40}


def test_billing_status_with_boolean_version_is_not_trusted(tmp_path):
    settings, binding = setup(tmp_path)
    provider = FakeProvider()
    billing = BillingClient(
        settings.billing_base_url,
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"data": {
            "tenant_id": binding.tenant_id,
            "project_id": binding.billing_project_id,
            "effective_status": "BLOCK_AI",
            "status_version": True,
        }})),
    )
    with TestClient(create_app(settings, provider, billing)) as client:
        # Unverifiable, so it fails open instead of blocking (invariant 1).
        assert invoke(client).status_code == 200
    assert provider.calls == 1


def outbox_rows(settings):
    with create_session_factory(settings.database_url)() as session:
        return session.scalars(select(UsageOutbox)).all()


def test_contact_summary_v1_returns_structured_fields(tmp_path):
    settings, _ = setup(tmp_path)
    provider = FakeProvider()
    with TestClient(create_app(settings, provider, AllowStatus())) as client:
        response = invoke(client, summary_request(output_language="en"))
    assert response.status_code == 200
    body = response.json()
    assert body["schema_version"] == "1.0"
    assert body["contact_ref"] == "contact:1842"
    assert body["summary"] == "Customer requested a callback."
    assert body["suggested_next_step"] == "Call back on Friday."
    assert body["model"] == "claude-sonnet-5-5"
    assert body["request_id"] == json.loads(outbox_rows(settings)[0].payload_json)["request_id"]
    system, context, options = provider.requests[0]
    # The model gets the facts but not the caller's reference, and must answer in the shape CRM reads.
    assert "contact:1842" not in context
    assert json.loads(context)["contact_snapshot"]["stage"] == "negotiation"
    assert "English" in system
    assert options["output_schema"]["required"] == ["summary", "suggested_next_step"]
    assert options["effort"] == "low"


def test_contact_summary_rejects_invalid_requests_before_the_model(tmp_path):
    settings, _ = setup(tmp_path)
    provider = FakeProvider()
    missing = summary_request()
    del missing["as_of"]
    invalid_bodies = {
        "legacy context_text": {"context_text": "Asked for a callback on Friday."},
        "extra top-level field": summary_request(context_text="x"),
        "missing as_of": missing,
        "wrong schema version": summary_request(schema_version="2.0"),
        "numeric schema version": summary_request(schema_version=1.0),
        "ref with a name in it": summary_request(contact_ref="Tan Ah Kow"),
        "ref too long": summary_request(contact_ref="c" * 129),
        "snapshot not an object": summary_request(contact_snapshot=["a"]),
        "snapshot over 16 KiB": summary_request(contact_snapshot={"notes": "x" * 16400}),
        "as_of without time zone": summary_request(as_of="2026-10-10T09:30:00"),
        "as_of as a number": summary_request(as_of=1760059800),
        "unsupported language": summary_request(output_language="fr"),
    }
    with TestClient(create_app(settings, provider, AllowStatus())) as client:
        for name, body in invalid_bodies.items():
            assert invoke(client, body).status_code == 422, name
        padded = json.dumps(summary_request()) + " " * 20480
        response = client.post(
            "/v1/capabilities/crm.contact_summary",
            headers={**HEADERS, "Content-Type": "application/json"},
            content=padded,
        )
        assert response.status_code == 422
    assert provider.calls == 0
    assert outbox_rows(settings) == []


def test_unusable_model_output_is_502_but_usage_is_kept(tmp_path):
    settings, _ = setup(tmp_path)
    cases = [
        FakeProvider(stop_reason="refusal"),
        FakeProvider(stop_reason="max_tokens"),
        FakeProvider(text="not json"),
        FakeProvider(text=json.dumps({"summary": "x" * 601, "suggested_next_step": ""})),
        FakeProvider(text=json.dumps({"summary": " ", "suggested_next_step": ""})),
    ]
    for provider in cases:
        with TestClient(create_app(settings, provider, AllowStatus())) as client:
            assert invoke(client).status_code == 502
    # Tokens were spent on every one of them, so each must still be billed.
    assert len(outbox_rows(settings)) == len(cases)


def test_provider_sends_structured_output_only_when_asked():
    sent = []

    def respond(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={
            "model": "claude-sonnet-5-5",
            "content": [{"type": "text", "text": "{}"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 5, "output_tokens": 1},
        })

    provider = AnthropicProvider("test-key", "claude-sonnet-5-5", transport=httpx.MockTransport(respond))
    result = provider.generate("system", "context", output_schema={"type": "object"}, effort="low", max_tokens=2000)
    assert result.stop_reason == "end_turn"
    assert sent[0]["output_config"] == {"format": {"type": "json_schema", "schema": {"type": "object"}}, "effort": "low"}
    assert sent[0]["max_tokens"] == 2000
    # The other four capabilities keep their original request unchanged.
    provider.generate("system", "context")
    assert "output_config" not in sent[1]
    assert sent[1]["max_tokens"] == 500

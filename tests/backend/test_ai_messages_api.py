"""Contract tests for the public AI messages request path."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.ai_api.access import AccessDecision, AuthenticationFailed, ModelNotAllowed, ServiceBlocked
from app.ai_api.held import HeldBuffer
from app.ai_api.provider import (
    Generation,
    ProviderRejectedRequest,
    ProviderUnavailable,
)
from app.ai_api.routes import AiRuntime, router
from app.core.errors import register_error_handlers
from app.core.middleware import RequestContextMiddleware


class ImmediateExecutor:
    async def run(self, function, *args, **kwargs):
        return function(*args, **kwargs)


class RaisingExecutor:
    def __init__(self, error: Exception) -> None:
        self.error = error

    async def run(self, function, *args, **kwargs):
        raise self.error


def settings() -> SimpleNamespace:
    return SimpleNamespace(
        ai_request_deadline_seconds=1.0,
        ai_provider_timeout_seconds=1.0,
        ai_db_pool_timeout_seconds=0.1,
        ai_db_connect_timeout_seconds=0.1,
        ai_db_rw_timeout_seconds=0.1,
        ai_max_request_bytes=65536,
        ai_max_output_tokens=4000,
    )


def decision() -> AccessDecision:
    return AccessDecision(1, 2, "tenant-public", "project-public", "claude-target")


def generation(*, text: str = "answer", stop_reason: str | None = "end_turn") -> Generation:
    return Generation(text, "claude-provider", 10, 4, 0, 0, stop_reason)


def body(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "feature": "crm.contact_summary",
        "model": "claude-alias",
        "messages": [{"role": "user", "content": "prompt-sentinel"}],
        "max_tokens": 10,
    }
    value.update(overrides)
    return value


@pytest.fixture
def app(monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    application = FastAPI()
    application.add_middleware(RequestContextMiddleware)
    register_error_handlers(application)
    application.include_router(router)
    application.state.settings = settings()
    application.state.ai_runtime = AiRuntime(
        engine=None,  # type: ignore[arg-type]
        session_factory=None,  # type: ignore[arg-type]
        executor=ImmediateExecutor(),  # type: ignore[arg-type]
        held_buffer=HeldBuffer(10),
        key_file="unused",
        _key="configured-key",
    )
    monkeypatch.setattr("app.ai_api.routes.resolve_access", lambda *args, **kwargs: decision())
    return application


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


def headers() -> dict[str, str]:
    return {"X-Acuven-Client-Id": "aic_client", "Authorization": "Bearer token"}


def install_success(
    monkeypatch: pytest.MonkeyPatch, captured: dict[str, object] | None = None
) -> None:
    class Provider:
        def __init__(self, key: str) -> None:
            assert key == "configured-key"

        async def generate(self, request, **kwargs):
            if captured is not None:
                captured["model"] = request.model
            return generation()

    async def persisted(*args, **kwargs):
        return "written"

    monkeypatch.setattr("app.ai_api.routes.AnthropicProvider", Provider)
    monkeypatch.setattr("app.ai_api.routes.persist_usage", persisted)


def test_normal_path_resolves_alias_and_returns_provider_response(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}
    install_success(monkeypatch, captured)

    response = client.post("/v1/messages", headers=headers(), json=body())

    assert response.status_code == 200
    assert captured["model"] == "claude-target"
    assert response.json() == {
        "request_id": response.json()["request_id"],
        "model": "claude-provider",
        "text": "answer",
        "stop_reason": "end_turn",
        "usage": {
            "input_tokens": 10,
            "output_tokens": 4,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        },
    }


@pytest.mark.parametrize(
    "payload",
    [
        body(feature="BAD"),
        body(messages=[]),
        body(messages=[{"role": "assistant", "content": "x"}]),
        body(max_tokens=True),
        body(tools=[]),
    ],
)
def test_validation_rejects_before_provider(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, payload: dict[str, object]
) -> None:
    called = False

    class Provider:
        def __init__(self, key: str) -> None:
            pass

        async def generate(self, request, **kwargs):
            nonlocal called
            called = True
            return generation()

    monkeypatch.setattr("app.ai_api.routes.AnthropicProvider", Provider)

    response = client.post("/v1/messages", headers=headers(), json=payload)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
    assert called is False


@pytest.mark.parametrize(
    ("error", "status", "code"),
    [
        (AuthenticationFailed(), 401, "AI_AUTH_FAILED"),
        (ServiceBlocked(), 403, "AI_SERVICE_BLOCKED"),
        (ModelNotAllowed(), 422, "MODEL_NOT_ALLOWED"),
        (RuntimeError(), 503, "AI_STORE_UNAVAILABLE"),
    ],
)
def test_access_failures_are_mapped_before_provider(
    app: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
    status: int,
    code: str,
) -> None:
    app.state.ai_runtime.executor = RaisingExecutor(error)
    install_success(monkeypatch)

    response = TestClient(app, raise_server_exceptions=False).post(
        "/v1/messages", headers=headers(), json=body()
    )

    assert response.status_code == status
    assert response.json()["error"]["code"] == code


@pytest.mark.parametrize(
    ("error", "status", "code"),
    [
        (ProviderUnavailable(), 502, "AI_PROVIDER_UNAVAILABLE"),
        (ProviderRejectedRequest("invalid_request_error"), 422, "PROVIDER_REJECTED_REQUEST"),
    ],
)
def test_provider_errors_are_mapped(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, error: Exception, status: int, code: str
) -> None:
    class Provider:
        def __init__(self, key: str) -> None:
            pass

        async def generate(self, request, **kwargs):
            raise error

    monkeypatch.setattr("app.ai_api.routes.AnthropicProvider", Provider)

    response = client.post("/v1/messages", headers=headers(), json=body())

    assert response.status_code == status
    assert response.json()["error"]["code"] == code


@pytest.mark.parametrize("stop_reason", ["max_tokens", "refusal"])
def test_provider_refusal_and_truncation_remain_successful(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, stop_reason: str
) -> None:
    install_success(monkeypatch)

    class Provider:
        def __init__(self, key: str) -> None:
            pass

        async def generate(self, request, **kwargs):
            return generation(text="", stop_reason=stop_reason)

    monkeypatch.setattr("app.ai_api.routes.AnthropicProvider", Provider)
    response = client.post("/v1/messages", headers=headers(), json=body())

    assert response.status_code == 200
    assert response.json()["text"] == ""
    assert response.json()["stop_reason"] == stop_reason


def test_key_is_checked_after_access(app: FastAPI) -> None:
    app.state.ai_runtime._key = None

    response = TestClient(app, raise_server_exceptions=False).post(
        "/v1/messages", headers=headers(), json=body()
    )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "AI_PROVIDER_NOT_CONFIGURED"


def test_missing_runtime_short_circuits_before_body_validation(app: FastAPI) -> None:
    app.state.ai_runtime = None

    response = TestClient(app, raise_server_exceptions=False).post(
        "/v1/messages", content=b"not json"
    )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "AI_STORE_UNAVAILABLE"


def test_unconfirmed_usage_returns_503_and_keeps_content_out_of_held_item(
    app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_success(monkeypatch)
    limits: list[float] = []

    async def not_persisted(*args, **kwargs):
        limits.append(kwargs["timeout_seconds"])
        held = args[3]
        assert "prompt-sentinel" not in str(held.payload)
        assert "answer" not in str(held.payload)
        return "unknown"

    monkeypatch.setattr("app.ai_api.routes.persist_usage", not_persisted)
    response = TestClient(app, raise_server_exceptions=False).post(
        "/v1/messages", headers=headers(), json=body(system="system-sentinel")
    )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "AI_USAGE_NOT_PERSISTED"
    assert limits == pytest.approx([0.7])


def test_auth_timeout_and_provider_timeout_have_distinct_errors(
    app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def blocked_run(function, *args, **kwargs):
        await asyncio.sleep(1)

    app.state.ai_runtime.executor.run = blocked_run
    app.state.settings.ai_db_pool_timeout_seconds = 0.001
    response = TestClient(app, raise_server_exceptions=False).post(
        "/v1/messages", headers=headers(), json=body()
    )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "AI_STORE_UNAVAILABLE"

    app.state.ai_runtime.executor = ImmediateExecutor()
    app.state.settings.ai_db_pool_timeout_seconds = 0.1

    class SlowProvider:
        def __init__(self, key: str) -> None:
            pass

        async def generate(self, request, **kwargs):
            await asyncio.sleep(1)
            return generation()

    monkeypatch.setattr("app.ai_api.routes.AnthropicProvider", SlowProvider)
    app.state.settings.ai_provider_timeout_seconds = 0.001
    response = TestClient(app, raise_server_exceptions=False).post(
        "/v1/messages", headers=headers(), json=body()
    )
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "AI_PROVIDER_UNAVAILABLE"

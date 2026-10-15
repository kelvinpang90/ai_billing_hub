"""Contract tests for the public AI messages request path."""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import inspect
import json
import logging
import os
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import httpx2
import pytest
from alembic.config import Config
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, delete, event, select, update
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from alembic import command
from app.ai_api.access import (
    AccessDecision,
    AuthenticationFailed,
    ModelNotAllowed,
    ServiceBlocked,
    resolve_access,
)
from app.ai_api.database import AiDatabaseExecutor, create_ai_session_factory
from app.ai_api.held import HeldBuffer
from app.ai_api.models import AiClientKey, AiClientKeyStatus
from app.ai_api.outbox import AiUsageOutbox, AiUsageOutboxStatus, write_pending
from app.ai_api.provider import (
    AnthropicProvider,
    Generation,
    ProviderRejectedRequest,
    ProviderUnavailable,
)
from app.ai_api.routes import AiRuntime, router
from app.core.config import Settings, get_settings
from app.core.errors import register_error_handlers
from app.core.middleware import RequestContextMiddleware
from app.main import create_app
from app.models.ai_catalog import AiModel, AiModelAlias, AiProvider, CatalogStatus
from app.models.auth import AuditLog
from app.models.base import Base
from app.models.tenancy import (
    AccountStatus,
    BillingMode,
    BillingStatus,
    IntegrationStatus,
    Project,
    Tenant,
)


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
        {key: value for key, value in body().items() if key != "feature"},
        body(feature="BAD"),
        body(feature="Crm.contact_summary"),
        body(feature="nodot"),
        body(messages=[]),
        body(messages=[{"role": "assistant", "content": "x"}]),
        body(max_tokens=True),
        body(max_tokens=4001),
        body(tools=[]),
        body(stream=True),
        body(temperature=0.5),
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


def test_oversized_body_is_rejected_before_provider(client: TestClient) -> None:
    payload = json.dumps(body(system="x" * 65537)).encode()

    response = client.post(
        "/v1/messages",
        headers={**headers(), "Content-Type": "application/json"},
        content=payload,
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "REQUEST_TOO_LARGE"


# --- 集成：真实的鉴权、状态、模型目录与 outbox 写入，供应商在 transport 层造假（设计 §7） ---

TEST_DATABASE_URL = os.environ.get("BILLING_TEST_DATABASE_URL", "")
MODEL = "claude-model-l"
SENTINEL = "sentinel-aih-063"
TENANT_A = ("aic_tenant_a", "token-tenant-a")
TENANT_B = ("aic_tenant_b", "token-tenant-b")
REVOKED = ("aic_tenant_a_revoked", "token-tenant-a-revoked")


@dataclass
class Party:
    tenant_id: int
    project_id: int
    tenant_public_id: str
    project_public_id: str


def key_headers(key: tuple[str, str]) -> dict[str, str]:
    return {"X-Acuven-Client-Id": key[0], "Authorization": f"Bearer {key[1]}"}


def provider_body(**overrides: object) -> dict[str, Any]:
    value: dict[str, Any] = {
        "model": MODEL,
        "content": [{"type": "text", "text": "answer"}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 10, "output_tokens": 4},
    }
    value.update(overrides)
    return value


def reply(status: int = 200, **overrides: object) -> httpx2.Response:
    return httpx2.Response(status, json=provider_body(**overrides))


@dataclass
class World:
    app: FastAPI
    client: TestClient
    factory: sessionmaker[Session]
    runtime: AiRuntime
    a: Party
    b: Party
    provider_bodies: list[dict[str, Any]] = field(default_factory=list)
    respond: Callable[[httpx2.Request], Any] = field(default=lambda request: reply())

    def post(self, key: tuple[str, str] = TENANT_A, **overrides: object):
        return self.client.post(
            "/v1/messages", headers=key_headers(key), json=body(**{"model": MODEL, **overrides})
        )

    def rows(self) -> list[AiUsageOutbox]:
        with self.factory() as session:
            return list(session.scalars(select(AiUsageOutbox).order_by(AiUsageOutbox.id)))


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC).replace(tzinfo=None)


def _clean(engine: Engine, parties: list[Party]) -> None:
    with engine.begin() as connection:
        connection.execute(delete(AiUsageOutbox))
        connection.execute(delete(AiClientKey))
        connection.execute(delete(AiModelAlias))
        connection.execute(delete(AiModel))
        connection.execute(delete(AiProvider))
        for party in parties:
            connection.execute(delete(Project).where(Project.id == party.project_id))
            connection.execute(delete(Tenant).where(Tenant.id == party.tenant_id))


def _party(session: Session, name: str, now: dt.datetime) -> Party:
    tenant = Tenant(
        public_id=str(uuid.uuid4()),
        company_name=f"AI messages {name}",
        email=f"ai-messages-{name}@example.com",
        billing_status=BillingStatus.ACTIVE,
        created_at=now,
        updated_at=now,
    )
    session.add(tenant)
    session.flush()
    project = Project(
        public_id=str(uuid.uuid4()),
        tenant_id=tenant.id,
        name=f"AI messages {name}",
        created_at=now,
        updated_at=now,
    )
    session.add(project)
    session.flush()
    return Party(tenant.id, project.id, tenant.public_id, project.public_id)


def _key(
    session: Session, party: Party, key: tuple[str, str], now: dt.datetime, *, revoked: bool = False
) -> None:
    session.add(
        AiClientKey(
            tenant_id=party.tenant_id,
            project_id=party.project_id,
            client_id=key[0],
            token_sha256=hashlib.sha256(key[1].encode()).hexdigest(),
            status=AiClientKeyStatus.REVOKED if revoked else AiClientKeyStatus.ACTIVE,
            created_at=now,
            revoked_at=now if revoked else None,
        )
    )


def _seed(factory: sessionmaker[Session]) -> tuple[Party, Party]:
    now = _now()
    with factory.begin() as session:
        provider = AiProvider(
            public_id=str(uuid.uuid4()),
            code="anthropic",
            display_name="Anthropic",
            status=CatalogStatus.ACTIVE,
            created_at=now,
            updated_at=now,
        )
        session.add(provider)
        session.flush()
        models = {}
        for code, status in (
            (MODEL, CatalogStatus.ACTIVE),
            ("claude-retired", CatalogStatus.RETIRED),
        ):
            model = AiModel(
                public_id=str(uuid.uuid4()),
                provider_id=provider.id,
                code=code,
                display_name=code,
                status=status,
                created_at=now,
                updated_at=now,
            )
            session.add(model)
            session.flush()
            models[code] = model
        # claude-x 此刻生效；claude-old 一小时前已截止。
        for alias, effective_to in (
            ("claude-x", None),
            ("claude-old", now - dt.timedelta(hours=1)),
        ):
            session.add(
                AiModelAlias(
                    public_id=str(uuid.uuid4()),
                    provider_id=provider.id,
                    model_id=models[MODEL].id,
                    alias=alias,
                    effective_from=None,
                    effective_to=effective_to,
                    created_at=now - dt.timedelta(days=1),
                    closed_at=effective_to,
                )
            )
        a = _party(session, "a", now)
        b = _party(session, "b", now)
        _key(session, a, TENANT_A, now)
        _key(session, a, REVOKED, now, revoked=True)
        _key(session, b, TENANT_B, now)
    return a, b


def integration_settings(tmp_path, **overrides: object) -> Settings:
    key_file = tmp_path / "anthropic.key"
    key_file.write_text("provider-test-key\n", encoding="utf-8")
    values: dict[str, Any] = {
        "database_url": "",
        "jwt_secret_file": "",
        "ai_anthropic_key_file": str(key_file),
        "ai_provider_timeout_seconds": 2.0,
        "ai_request_deadline_seconds": 8.0,
        "ai_db_pool_timeout_seconds": 0.5,
        "ai_db_connect_timeout_seconds": 0.5,
        "ai_db_rw_timeout_seconds": 1.0,
        "ai_db_threads": 2,
        "ai_db_queue_max": 2,
    }
    values.update(overrides)
    return Settings(**values)


def _engine(kind: str, monkeypatch: pytest.MonkeyPatch) -> Engine:
    if kind == "sqlite":
        engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            poolclass=StaticPool,
            connect_args={"check_same_thread": False},
        )
        Base.metadata.create_all(engine)
        return engine
    if not TEST_DATABASE_URL:
        pytest.skip("BILLING_TEST_DATABASE_URL is not set; the MySQL half needs a real MySQL")
    monkeypatch.setenv("BILLING_DATABASE_URL", TEST_DATABASE_URL)
    get_settings.cache_clear()
    command.upgrade(Config("alembic.ini"), "head")
    get_settings.cache_clear()
    engine = create_engine(TEST_DATABASE_URL, pool_size=4, max_overflow=2)
    _clean(engine, [])
    return engine


def _build_world(kind: str, tmp_path, monkeypatch, **overrides: object) -> Iterator[World]:
    settings = integration_settings(tmp_path, **overrides)
    engine = _engine(kind, monkeypatch)
    factory = create_ai_session_factory(engine)
    a, b = _seed(factory)
    application = create_app(settings)
    runtime = AiRuntime(
        engine=engine,
        session_factory=factory,
        executor=AiDatabaseExecutor(
            threads=settings.ai_db_threads, queue_max=settings.ai_db_queue_max
        ),
        held_buffer=HeldBuffer(10),
        key_file=settings.ai_anthropic_key_file,
    )
    application.state.engine = engine
    application.state.session_factory = factory
    application.state.ai_runtime = runtime
    client = TestClient(application, raise_server_exceptions=False)
    world = World(application, client, factory, runtime, a, b)

    async def transport(request: httpx2.Request) -> httpx2.Response:
        world.provider_bodies.append(json.loads(request.content))
        result = world.respond(request)
        return await result if inspect.isawaitable(result) else result

    monkeypatch.setattr(
        "app.ai_api.routes.AnthropicProvider",
        lambda key: AnthropicProvider(key, transport=httpx2.MockTransport(transport)),
    )
    try:
        yield world
    finally:
        runtime.executor.shutdown()
        _clean(engine, [a, b])
        engine.dispose()


@pytest.fixture(params=["sqlite", "mysql"])
def world(request, tmp_path, monkeypatch) -> Iterator[World]:
    yield from _build_world(request.param, tmp_path, monkeypatch)


@pytest.fixture
def fast_world(tmp_path, monkeypatch) -> Iterator[World]:
    """Small wall clocks for the time-limit cases: read 0.4 s, write 1.0 s, provider 1.0 s."""
    yield from _build_world(
        "sqlite",
        tmp_path,
        monkeypatch,
        ai_provider_timeout_seconds=1.0,
        ai_request_deadline_seconds=3.0,
        ai_db_pool_timeout_seconds=0.1,
        ai_db_connect_timeout_seconds=0.1,
        ai_db_rw_timeout_seconds=0.2,
    )


def test_normal_path_forwards_the_subset_and_writes_one_pending_row(world: World) -> None:
    response = world.post(output_config={"effort": "low"}, system="caller system")

    assert response.status_code == 200
    data = response.json()
    assert set(data) == {"request_id", "model", "text", "stop_reason", "usage"}
    assert (data["model"], data["text"], data["stop_reason"]) == (MODEL, "answer", "end_turn")
    assert data["usage"] == {
        "input_tokens": 10,
        "output_tokens": 4,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
    }
    assert world.provider_bodies == [
        {
            "model": MODEL,
            "max_tokens": 10,
            "system": "caller system",
            "messages": [{"role": "user", "content": "prompt-sentinel"}],
            "output_config": {"effort": "low"},
        }
    ]
    rows = world.rows()
    assert [(row.status, row.feature, row.tenant_id, row.project_id) for row in rows] == [
        (AiUsageOutboxStatus.PENDING, "crm.contact_summary", world.a.tenant_id, world.a.project_id)
    ]
    payload = json.loads(rows[0].payload_json)
    assert payload["request_id"] == data["request_id"] == rows[0].request_id
    assert payload["event_id"] != data["request_id"]
    assert (payload["tenant_id"], payload["project_id"]) == (
        world.a.tenant_public_id,
        world.a.project_public_id,
    )
    assert (payload["feature"], payload["model"]) == ("crm.contact_summary", MODEL)


@pytest.mark.parametrize(
    ("model", "status", "forwarded"),
    [
        (MODEL, 200, MODEL),
        ("claude-retired", 422, None),
        ("claude-unknown", 422, None),
        ("claude-x", 200, MODEL),
        ("claude-old", 422, None),
    ],
)
def test_model_guard(world: World, model: str, status: int, forwarded: str | None) -> None:
    response = world.post(model=model)

    assert response.status_code == status
    if forwarded is None:
        assert response.json()["error"]["code"] == "MODEL_NOT_ALLOWED"
        assert world.provider_bodies == []
        assert world.rows() == []
    else:
        assert [sent["model"] for sent in world.provider_bodies] == [forwarded]
        assert len(world.rows()) == 1


def test_a_retired_provider_allows_no_model(world: World) -> None:
    with world.factory.begin() as session:
        session.execute(update(AiProvider).values(status=CatalogStatus.RETIRED))

    for model in (MODEL, "claude-x"):
        response = world.post(model=model)
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "MODEL_NOT_ALLOWED"
    assert world.provider_bodies == []
    assert world.rows() == []


async def _hang(request: httpx2.Request) -> httpx2.Response:
    await asyncio.sleep(30)
    return reply()


def _rejected(request: httpx2.Request) -> httpx2.Response:
    return httpx2.Response(
        400,
        json={"type": "error", "error": {"type": "invalid_request_error", "message": SENTINEL}},
    )


@pytest.mark.parametrize(
    ("respond", "status", "code"),
    [
        (_rejected, 422, "PROVIDER_REJECTED_REQUEST"),
        (lambda request: httpx2.Response(429), 502, "AI_PROVIDER_UNAVAILABLE"),
        (lambda request: httpx2.Response(500), 502, "AI_PROVIDER_UNAVAILABLE"),
        (_hang, 502, "AI_PROVIDER_UNAVAILABLE"),
        (
            lambda request: httpx2.Response(200, json={"model": MODEL, "content": []}),
            502,
            "AI_PROVIDER_UNAVAILABLE",
        ),
    ],
    ids=["400", "429", "500", "timeout", "no_usage"],
)
def test_provider_errors_are_mapped_without_outbox_rows(
    world: World, respond, status: int, code: str
) -> None:
    world.app.state.settings.ai_provider_timeout_seconds = 0.3
    world.respond = respond

    response = world.post()

    assert response.status_code == status
    error = response.json()["error"]
    assert error["code"] == code
    assert SENTINEL not in response.text
    if code == "PROVIDER_REJECTED_REQUEST":
        assert "invalid_request_error" in error["message"]
    assert world.rows() == []


@pytest.mark.parametrize(
    "overrides",
    [
        {"usage": {"input_tokens": 10**12 + 1, "output_tokens": 4}},
        {"usage": {"input_tokens": -1, "output_tokens": 4}},
        {"usage": {"input_tokens": True, "output_tokens": 4}},
        {"usage": {"input_tokens": 1.5, "output_tokens": 4}},
        {"usage": {"input_tokens": 10, "output_tokens": 4, "cache_read_input_tokens": "1"}},
        {"model": "claude model with spaces"},
    ],
    ids=["over_max", "negative", "boolean", "fraction", "cache_string", "bad_model"],
)
def test_unusable_usage_or_model_is_502_without_rows(
    world: World, overrides: dict[str, object]
) -> None:
    world.respond = lambda request: reply(**overrides)

    response = world.post()

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "AI_PROVIDER_UNAVAILABLE"
    assert world.rows() == []


@pytest.mark.parametrize(
    "usage",
    [
        {"input_tokens": 10, "output_tokens": 4},
        {
            "input_tokens": 10,
            "output_tokens": 4,
            "cache_creation_input_tokens": None,
            "cache_read_input_tokens": None,
        },
    ],
    ids=["missing", "null"],
)
def test_missing_cache_fields_count_as_zero(world: World, usage: dict[str, object]) -> None:
    world.respond = lambda request: reply(usage=usage)

    response = world.post()

    assert response.status_code == 200
    payload = json.loads(world.rows()[0].payload_json)
    assert (payload["cache_creation_input_tokens"], payload["cache_read_input_tokens"]) == (0, 0)


@pytest.mark.parametrize("stop_reason", ["max_tokens", "refusal"])
def test_refusal_and_truncation_are_200_and_recorded(world: World, stop_reason: str) -> None:
    world.respond = lambda request: reply(content=[], stop_reason=stop_reason)

    response = world.post()

    assert response.status_code == 200
    assert (response.json()["text"], response.json()["stop_reason"]) == ("", stop_reason)
    assert [row.status for row in world.rows()] == [AiUsageOutboxStatus.PENDING]


@pytest.mark.parametrize(
    ("tenant_values", "project_values", "status"),
    [
        ({"account_status": AccountStatus.DISABLED}, {}, 403),
        ({}, {"integration_status": IntegrationStatus.DISABLED}, 403),
        ({"billing_status": BillingStatus.SUSPENDED}, {}, 403),
        (
            {
                "billing_status": BillingStatus.SUSPENDED,
                "billing_mode": BillingMode.INTERNAL_METERED_ONLY,
            },
            {},
            200,
        ),
    ],
    ids=["account_disabled", "project_disabled", "prepaid_balance_zero", "internal_balance_zero"],
)
def test_status_blocks_before_provider(
    world: World,
    tenant_values: dict[str, object],
    project_values: dict[str, object],
    status: int,
) -> None:
    with world.factory.begin() as session:
        if tenant_values:
            session.execute(
                update(Tenant).where(Tenant.id == world.a.tenant_id).values(**tenant_values)
            )
        if project_values:
            session.execute(
                update(Project).where(Project.id == world.a.project_id).values(**project_values)
            )

    response = world.post()

    assert response.status_code == status
    if status == 403:
        assert response.json()["error"]["code"] == "AI_SERVICE_BLOCKED"
        assert world.provider_bodies == []
        assert world.rows() == []
    else:
        assert len(world.rows()) == 1


@pytest.mark.parametrize(
    "request_headers",
    [
        {},
        {"Authorization": f"Bearer {TENANT_A[1]}"},
        {"X-Acuven-Client-Id": TENANT_A[0]},
        key_headers((TENANT_A[0], "token-wrong")),
        key_headers((TENANT_A[0], TENANT_B[1])),
        key_headers(REVOKED),
        key_headers(("aic_unknown", TENANT_A[1])),
        {"X-Acuven-Client-Id": TENANT_A[0], "Authorization": TENANT_A[1]},
    ],
    ids=[
        "no_headers",
        "no_client_id",
        "no_token",
        "wrong_token",
        "other_tenants_token",
        "revoked",
        "unknown_client",
        "not_bearer",
    ],
)
def test_authentication_failures_are_401_before_provider(
    world: World, request_headers: dict[str, str]
) -> None:
    response = world.client.post("/v1/messages", headers=request_headers, json=body())

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "AI_AUTH_FAILED"
    assert world.provider_bodies == []
    assert world.rows() == []


def test_a_key_only_writes_rows_for_its_own_tenant(world: World) -> None:
    assert world.post(TENANT_A).status_code == 200
    assert world.post(TENANT_B).status_code == 200

    rows = world.rows()
    assert [(row.tenant_id, row.project_id) for row in rows] == [
        (world.a.tenant_id, world.a.project_id),
        (world.b.tenant_id, world.b.project_id),
    ]
    tenants = [json.loads(row.payload_json)["tenant_id"] for row in rows]
    assert tenants == [world.a.tenant_public_id, world.b.tenant_public_id]


def test_content_never_lands(world: World, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    content = {
        "system": f"system-{SENTINEL}",
        "messages": [{"role": "user", "content": f"prompt-{SENTINEL}"}],
    }
    responses = []

    world.respond = lambda request: reply(content=[{"type": "text", "text": f"output-{SENTINEL}"}])
    responses.append(world.post(**content))
    responses.append(world.post(tools=[SENTINEL], **content))
    world.respond = lambda request: httpx2.Response(500, text=f"upstream-{SENTINEL}")
    responses.append(world.post(**content))
    world.respond = _rejected
    responses.append(world.post(**content))

    assert [response.status_code for response in responses] == [200, 422, 502, 422]
    assert f"output-{SENTINEL}" in responses[0].text
    assert all(SENTINEL not in response.text for response in responses[1:])
    assert all(SENTINEL not in json.dumps(dict(response.headers)) for response in responses)
    rows = world.rows()
    assert len(rows) == 1
    assert SENTINEL not in rows[0].payload_json
    with world.factory() as session:
        assert session.scalars(select(AuditLog)).all() == []
    assert SENTINEL not in caplog.text


def test_request_path_writes_only_the_outbox(world: World) -> None:
    statements: list[str] = []

    def record(conn, cursor, statement, parameters, context, executemany) -> None:
        statements.append(statement)

    event.listen(world.runtime.engine, "before_cursor_execute", record)
    try:
        response = world.post()
    finally:
        event.remove(world.runtime.engine, "before_cursor_execute", record)

    assert response.status_code == 200
    writes = [
        [word.upper().strip('`"') for word in statement.split()[:3]]
        for statement in statements
        if statement.split()[0].upper() in {"INSERT", "UPDATE", "DELETE", "REPLACE"}
    ]
    assert writes == [["INSERT", "INTO", "AI_USAGE_OUTBOX"]]


# --- 时限与取消（设计 §7「时限与取消」，缩小的时限） ---


class Blocker:
    """Make a sync database call hang on an event, counting how many run at once."""

    def __init__(self, real: Callable[..., Any]) -> None:
        self.real = real
        self.release = threading.Event()
        self.lock = threading.Lock()
        self.running = 0
        self.peak = 0

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        if not self.release.is_set():
            with self.lock:
                self.running += 1
                self.peak = max(self.peak, self.running)
            try:
                self.release.wait(30)
            finally:
                with self.lock:
                    self.running -= 1
        return self.real(*args, **kwargs)


def _drain(world: World) -> None:
    deadline = time.monotonic() + 5
    while world.runtime.executor._active and time.monotonic() < deadline:
        time.sleep(0.01)
    assert world.runtime.executor._active == 0


def test_hung_auth_read_returns_503_within_its_limit(
    fast_world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    blocker = Blocker(resolve_access)
    monkeypatch.setattr("app.ai_api.routes.resolve_access", blocker)
    try:
        started = time.monotonic()
        response = fast_world.post()
        elapsed = time.monotonic() - started
    finally:
        blocker.release.set()

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "AI_STORE_UNAVAILABLE"
    assert 0.4 <= elapsed < 0.4 + 1.0
    assert fast_world.provider_bodies == []
    _drain(fast_world)
    assert fast_world.rows() == []


def test_hung_provider_and_slow_trickle_are_cut_at_the_budget(fast_world: World) -> None:
    async def trickle(request: httpx2.Request) -> httpx2.Response:
        # 每一段都远小于 httpx 的读超时，累计却超过预算。
        async def chunks():
            while True:
                await asyncio.sleep(0.05)
                yield b" "

        return httpx2.Response(200, content=chunks())

    for respond in (_hang, trickle):
        fast_world.respond = respond
        started = time.monotonic()
        response = fast_world.post()
        elapsed = time.monotonic() - started

        assert response.status_code == 502
        assert response.json()["error"]["code"] == "AI_PROVIDER_UNAVAILABLE"
        assert 1.0 <= elapsed < 1.0 + 1.0
    assert fast_world.rows() == []


def test_hung_outbox_write_holds_usage_within_its_limit(
    fast_world: World, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    blocker = Blocker(write_pending)
    monkeypatch.setattr("app.ai_api.outbox.write_pending", blocker)
    try:
        started = time.monotonic()
        response = fast_world.post()
        elapsed = time.monotonic() - started

        assert response.status_code == 503
        assert response.json()["error"]["code"] == "AI_USAGE_NOT_PERSISTED"
        assert 1.0 <= elapsed < 1.0 + 1.0
        # 超时那一刻 CRITICAL 与暂存已经存在，不等卡住的线程回来。
        critical = [r for r in caplog.records if r.getMessage() == "AI usage not persisted"]
        assert [r.levelno for r in critical] == [logging.CRITICAL]
        assert critical[0].commit_outcome == "unknown"
        assert len(fast_world.runtime.held_buffer) == 1
    finally:
        blocker.release.set()
    _drain(fast_world)


def test_a_stuck_database_caps_threads_rejects_overflow_and_recovers(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 读时限 1 秒：被接纳的请求等满 1 秒才 503，超额的请求应远早于此返回。
    built = _build_world(
        "sqlite",
        tmp_path,
        monkeypatch,
        ai_request_deadline_seconds=5.0,
        ai_db_pool_timeout_seconds=0.3,
        ai_db_connect_timeout_seconds=0.3,
        ai_db_rw_timeout_seconds=0.4,
    )
    world = next(built)
    executor = world.runtime.executor
    limit = 2 + 2
    blocker = Blocker(resolve_access)
    monkeypatch.setattr("app.ai_api.routes.resolve_access", blocker)
    samples: list[int] = []

    async def wave(client: httpx2.AsyncClient, count: int) -> list[tuple[int, str, float]]:
        async def one() -> tuple[int, str, float]:
            started = time.monotonic()
            response = await client.post(
                "/v1/messages", headers=key_headers(TENANT_A), json=body(model=MODEL)
            )
            elapsed = time.monotonic() - started
            return response.status_code, response.json()["error"]["code"], elapsed

        return list(await asyncio.gather(*(one() for _ in range(count))))

    async def sample(stop: asyncio.Event) -> None:
        while not stop.is_set():
            samples.append(executor._active)
            await asyncio.sleep(0.005)

    async def scenario() -> tuple[list, list, float, int]:
        transport = httpx2.ASGITransport(app=world.app)
        async with httpx2.AsyncClient(transport=transport, base_url="http://test") as client:
            stop = asyncio.Event()
            sampler = asyncio.create_task(sample(stop))
            first = await wave(client, 12)
            health_started = time.monotonic()
            health = await client.get("/healthz")
            health_elapsed = time.monotonic() - health_started
            second = await wave(client, 12)
            stop.set()
            await sampler
            return first, second, health_elapsed, health.status_code

    try:
        first, second, health_elapsed, health_status = asyncio.run(scenario())

        for results in (first, second):
            assert {(status, code) for status, code, _ in results} == {
                (503, "AI_STORE_UNAVAILABLE")
            }
            assert all(elapsed < 5.0 for _, _, elapsed in results)
            # 超额的请求立刻 503（不排队），被接纳的等满读时限。
            assert sum(elapsed < 0.5 for _, _, elapsed in results) >= 12 - limit
        assert max(samples) <= limit
        assert blocker.peak <= 2
        assert blocker.running == 2
        assert (health_status, health_elapsed < 0.5) == (200, True)
        assert world.provider_bodies == []

        blocker.release.set()
        _drain(world)
        assert blocker.running == 0
        assert world.post().status_code == 200
        assert len(world.rows()) == 1
    finally:
        blocker.release.set()
        for _ in built:
            pass


# --- 应用生命周期（app/main.py） ---


def test_without_a_database_the_runtime_is_empty_and_calls_get_503(tmp_path) -> None:
    with TestClient(create_app(integration_settings(tmp_path))) as client:
        assert client.app.state.ai_runtime is None
        response = client.post(
            "/v1/messages", headers=key_headers(TENANT_A), json=body(model=MODEL)
        )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "AI_STORE_UNAVAILABLE"


def test_lifespan_builds_the_runtime_and_survives_a_slow_final_flush(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "ai.sqlite3"
    settings = integration_settings(tmp_path, database_url=f"sqlite:///{database.as_posix()}")
    flushes: list[float] = []

    async def slow_final_flush(*args: Any, timeout_seconds: float) -> None:
        flushes.append(timeout_seconds)
        raise TimeoutError

    monkeypatch.setattr("app.main.final_held_attempt", slow_final_flush)
    application = create_app(settings)

    with TestClient(application):
        runtime = application.state.ai_runtime
        assert isinstance(runtime, AiRuntime)
        assert runtime.executor._limit == settings.ai_db_threads + settings.ai_db_queue_max

    # 停机前最后一次落库按 5 秒上限放弃，lifespan 不把 TimeoutError 抛出去。
    assert flushes == [min(5, 0.5 + 0.5 + 1.0)]

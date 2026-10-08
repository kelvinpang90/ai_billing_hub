"""`POST /api/v1/integration/usage-events` on SQLite (design gate #176 v8 §7, AIH-TASK-029).

Rows of design §7 covered here, through the HTTP endpoint with real signatures:

- **正常路径** — an LLM_TOKEN event, an AUDIO_SECOND decimal and an IMAGE_GENERATION integer:
  202 and one `RECEIVED` row with the right columns; a `RETIRED` meter type; an unknown model;
- **202 先落库** — the commit raises: 500, never 202, no row;
- **合法重复 / 已处理的重复 / 轮换期间重复** — new signature and Request-Id; another spelling
  of `quantity` and `occurred_at`; a `PROCESSED` row; the next `key_version` of the same api_key;
- **冲突** — a different fingerprint, another api_key, both; the original row column for column
  unchanged; one conflict row and one audit, also after three retries;
- **签名与凭据**, **越权**, **重放（REQ-AUTH-001 证据）**, **Redis 不可用**,
  **吊销即时生效**;
- **字段校验**, **`event_id` 格式**, **未知 usage_type**, **时间**, **请求体上限**,
  **归属不符**;
- **`retryable` 全覆盖** — every code of the error table, FastAPI's 422, an unexpected 500;
- **`last_used_at` 节流**.

The unit cases (nonce expiry, decryption cache, fingerprint), the mid-transaction rollback on
SQLite and MySQL, the concurrency cases and the database constraints are in
test_usage_ingest_service.py.

Placeholders only (public repository): uuids, api keys and secrets are zeros with a counter;
usage numbers are fictional; there are no prices or amounts here at all.
"""

from __future__ import annotations

import base64
import datetime as dt
import itertools
import json
import logging
from collections.abc import Iterator
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import redis
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel
from sqlalchemy import create_engine, select, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.api import integration_usage
from app.core.config import Settings
from app.core.crypto import encrypt_secret, load_keyring
from app.core.database import create_session_factory
from app.core.nonce import NonceStore
from app.main import create_app
from app.models.ai_catalog import (
    SEED_METER_TYPES,
    TOKEN_FIELDS,
    CatalogStatus,
    PayloadShape,
    QuantityKind,
    UsageMeterType,
)
from app.models.auth import AuditAction, AuditLog
from app.models.base import Base
from app.models.integration import CredentialStatus, IntegrationCredential
from app.models.tenancy import AccountStatus, BillingMode, Project, Tenant
from app.models.usage import (
    ConflictMismatch,
    UsageEvent,
    UsageEventConflict,
    UsageEventStatus,
)
from app.repositories import usage_events
from app.services import usage_ingest
from app.services.integration_auth import canonical_request, credential_aad, sign

PATH = "/api/v1/integration/usage-events"
STATUS_PATH = "/api/v1/integration/effective-status"
ENVELOPE_FIELDS = {"success", "data", "error", "request_id"}
ERROR_FIELDS = ENVELOPE_FIELDS | {"retryable"}

NOW = dt.datetime(2026, 10, 5, 8, 0, 0)
SECOND = dt.timedelta(seconds=1)
# A moment before NOW, in the canonical spelling and an equivalent one (+08:00).
OCCURRED = "2026-10-05T07:59:30.250000Z"
OCCURRED_KL = "2026-10-05T15:59:30.25+08:00"

MISSING = "<missing>"
_REQUEST_NUMBERS = itertools.count(1)


def uuid7(number: int) -> str:
    """A UUIDv7 in its canonical spelling, zeros apart from the version, variant and counter."""
    return f"00000000-0000-7000-8000-{number:012d}"


def public_id(number: int) -> str:
    return f"00000000-0000-0000-0000-{number:012d}"


def api_key(number: int) -> str:
    return f"ak_{number:032d}"


def secret(number: int) -> str:
    return f"sk_{number:064d}"


def epoch(moment: dt.datetime) -> int:
    return int(moment.replace(tzinfo=dt.UTC).timestamp())


# --- fixtures -------------------------------------------------------------------------


class Clock:
    """The endpoint's `utc_now`, frozen; tests move it."""

    def __init__(self, moment: dt.datetime) -> None:
        self.now = moment

    def __call__(self) -> dt.datetime:
        return self.now


class FakeRedis:
    """redis-py's `SET … NX PXAT`, in memory. Keeps every call for the assertions."""

    def __init__(self) -> None:
        self.keys: dict[str, int] = {}
        self.calls: list[tuple[str, int]] = []

    def set(self, name: str, value: int, *, nx: bool, pxat: int) -> bool | None:
        self.calls.append((name, pxat))
        if nx and name in self.keys:
            return None
        self.keys[name] = pxat
        return True


class DownRedis:
    """A Redis that does not answer."""

    def set(self, name: str, value: int, *, nx: bool, pxat: int) -> bool | None:
        raise redis.exceptions.ConnectionError("connection refused")


@pytest.fixture
def clock() -> Clock:
    return Clock(NOW)


@pytest.fixture
def fake_redis() -> FakeRedis:
    return FakeRedis()


@pytest.fixture
def app(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    clock: Clock,
    fake_redis: FakeRedis,
) -> Iterator[FastAPI]:
    """Built in the setup phase: `create_app` reinstalls the root log handlers."""
    master = tmp_path / "master.key"
    master.write_text(f"1:{base64.b64encode(bytes(32)).decode()}\n", encoding="utf-8")
    settings = Settings(database_url="", master_key_file=str(master), redis_url="")
    application = create_app(settings)
    # StaticPool: the endpoint runs in a thread pool; the in-memory database is one connection.
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    application.state.engine = engine
    application.state.session_factory = create_session_factory(engine)
    application.state.nonce_store = NonceStore(fake_redis)
    monkeypatch.setattr(integration_usage, "utc_now", clock)
    seed_meter_types(application.state.session_factory)
    yield application
    engine.dispose()


@pytest.fixture
def factory(app: FastAPI) -> sessionmaker[Session]:
    return app.state.session_factory


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


@dataclass(frozen=True)
class Credential:
    """One signing version as the integrated backend holds it, plus its owner's public ids."""

    api_key: str
    key_version: int
    secret: str
    tenant_public_id: str
    project_public_id: str
    tenant_id: int
    project_id: int


def seed_meter_types(factory: sessionmaker[Session]) -> None:
    """What migration 0012 seeds, for the SQLite tables `create_all` builds empty."""
    with factory() as session:
        for number, seed in enumerate(SEED_METER_TYPES, start=900):
            session.add(
                UsageMeterType(
                    public_id=public_id(number),
                    code=seed.code,
                    display_name=seed.display_name,
                    payload_shape=seed.payload_shape,
                    unit=seed.unit,
                    quantity_kind=seed.quantity_kind,
                    created_at=NOW,
                    updated_at=NOW,
                )
            )
        session.commit()


def make_project(factory: sessionmaker[Session], number: int) -> tuple[int, int]:
    """A tenant and its project with placeholder public ids `number` and `number + 100`."""
    with factory() as session:
        tenant = Tenant(
            public_id=public_id(number),
            company_name=f"Fictional {number} Sdn Bhd",
            email=f"ops-{number}@usage-ingest-test.example.com",
            created_at=NOW,
            updated_at=NOW,
        )
        session.add(tenant)
        session.flush()
        project = Project(
            public_id=public_id(number + 100),
            tenant_id=tenant.id,
            name="Chatbot",
            created_at=NOW,
            updated_at=NOW,
        )
        session.add(project)
        session.commit()
        return tenant.id, project.id


def add_version(
    app: FastAPI,
    *,
    tenant_id: int,
    project_id: int,
    key: str,
    key_version: int,
    plaintext: str,
    status: CredentialStatus = CredentialStatus.ACTIVE,
    valid_until: dt.datetime | None = None,
) -> None:
    """One credential row, encrypted with the row's AAD like the admin service does."""
    keyring = load_keyring(app.state.settings)
    aad = credential_aad(key, key_version)
    encrypted, master_version = encrypt_secret(keyring, plaintext, associated_data=aad)
    with app.state.session_factory() as session:
        session.add(
            IntegrationCredential(
                tenant_id=tenant_id,
                project_id=project_id,
                public_api_key=key,
                key_version=key_version,
                encrypted_secret=encrypted,
                encryption_key_version=master_version,
                status=status,
                valid_from=NOW - dt.timedelta(days=1),
                valid_until=valid_until,
                created_at=NOW - dt.timedelta(days=1),
                revoked_at=NOW if status is CredentialStatus.REVOKED else None,
            )
        )
        session.commit()


def make_credential(app: FastAPI, number: int, **row: Any) -> Credential:
    """A new tenant and project, and version 1 of api key `number`."""
    tenant_id, project_id = make_project(app.state.session_factory, number)
    add_version(
        app,
        tenant_id=tenant_id,
        project_id=project_id,
        key=api_key(number),
        key_version=1,
        plaintext=secret(number),
        **row,
    )
    return Credential(
        api_key=api_key(number),
        key_version=1,
        secret=secret(number),
        tenant_public_id=public_id(number),
        project_public_id=public_id(number + 100),
        tenant_id=tenant_id,
        project_id=project_id,
    )


@pytest.fixture
def credential(app: FastAPI) -> Credential:
    return make_credential(app, 1)


# --- requests -------------------------------------------------------------------------


def llm_event(number: int = 1, **changes: object) -> dict[str, object]:
    event: dict[str, object] = {
        "schema_version": "1.0",
        "event_id": uuid7(number),
        "request_id": f"call-{number}",
        "conversation_id": "conv-1",
        "provider": "anthropic",
        "model": "claude-fictional-1",
        "usage_type": "LLM_TOKEN",
        "input_tokens": 1200,
        "output_tokens": 340,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 50,
        "occurred_at": OCCURRED,
    }
    return _changed(event, changes)


def quantity_event(
    number: int = 1,
    usage_type: str = "AUDIO_SECOND",
    **changes: object,
) -> dict[str, object]:
    defaults = {"AUDIO_SECOND": ("12.5", "SECOND"), "IMAGE_GENERATION": ("2", "IMAGE")}
    quantity, unit = defaults.get(usage_type, ("1", "UNIT"))
    event: dict[str, object] = {
        "schema_version": "1.0",
        "event_id": uuid7(number),
        "request_id": f"call-{number}",
        "provider": "openai",
        "model": "whisper-fictional",
        "usage_type": usage_type,
        "quantity": quantity,
        "unit": unit,
        "occurred_at": OCCURRED,
    }
    return _changed(event, changes)


def _changed(event: dict[str, object], changes: dict[str, object]) -> dict[str, object]:
    for key, value in changes.items():
        if value == MISSING:
            event.pop(key, None)
        else:
            event[key] = value
    return event


def signed_headers(
    credential: Credential,
    body: bytes,
    *,
    timestamp: int,
    request_id: str | None = None,
    key_version: int | None = None,
    signing_secret: str | None = None,
) -> dict[str, str]:
    request_id = request_id or f"try-{next(_REQUEST_NUMBERS)}"
    canonical = canonical_request("POST", PATH, str(timestamp), request_id, body)
    return {
        "X-Acuven-Api-Key": credential.api_key,
        "X-Acuven-Key-Version": str(key_version or credential.key_version),
        "X-Acuven-Timestamp": str(timestamp),
        "X-Acuven-Request-Id": request_id,
        "X-Acuven-Signature": sign(signing_secret or credential.secret, canonical),
        "Content-Type": "application/json",
    }


def status_headers(credential: Credential, clock: Clock) -> dict[str, str]:
    timestamp = str(epoch(clock.now))
    attempt_id = f"status-{next(_REQUEST_NUMBERS)}"
    canonical = canonical_request("GET", STATUS_PATH, timestamp, attempt_id, b"")
    return {
        "X-Acuven-Api-Key": credential.api_key,
        "X-Acuven-Key-Version": str(credential.key_version),
        "X-Acuven-Timestamp": timestamp,
        "X-Acuven-Request-Id": attempt_id,
        "X-Acuven-Signature": sign(credential.secret, canonical),
    }


def encode(event: object) -> bytes:
    return json.dumps(event).encode("utf-8")


def post(
    client: TestClient,
    credential: Credential,
    event: object,
    *,
    clock: Clock | None = None,
    **signing: Any,
):
    """Sign and send one event: a new attempt, so a new Request-Id and the clock's time."""
    body = event if isinstance(event, bytes) else encode(event)
    timestamp = signing.pop("timestamp", epoch(clock.now if clock else NOW))
    headers = signed_headers(credential, body, timestamp=timestamp, **signing)
    return client.post(PATH, content=body, headers=headers)


def test_effective_status_uses_mode_but_never_bypasses_admin_disable(
    client, app, credential, clock
) -> None:
    def read() -> dict:
        response = client.get(STATUS_PATH, headers=status_headers(credential, clock))
        assert response.status_code == 200, response.text
        return response.json()["data"]

    initial = read()
    assert initial == {
        "tenant_id": credential.tenant_public_id,
        "project_id": credential.project_public_id,
        "billing_mode": "PREPAID",
        "billing_status": "SUSPENDED",
        "account_status": "ENABLED",
        "effective_status": "BLOCK_AI",
        "status_version": 0,
    }
    with app.state.session_factory() as session:
        tenant = session.get(Tenant, credential.tenant_id)
        tenant.billing_mode = BillingMode.INTERNAL_METERED_ONLY
        tenant.status_version += 1
        session.commit()
    internal = read()
    assert internal["billing_status"] == "SUSPENDED"
    assert internal["effective_status"] == "ALLOW_AI"
    assert internal["status_version"] == 1

    with app.state.session_factory() as session:
        tenant = session.get(Tenant, credential.tenant_id)
        tenant.account_status = AccountStatus.DISABLED
        tenant.status_version += 1
        session.commit()
    assert read()["effective_status"] == "BLOCK_AI"


def test_effective_status_rejects_unsigned_request(client) -> None:
    assert client.get(STATUS_PATH).status_code == 401


def assert_accepted(response, event_id: str) -> None:
    assert response.status_code == 202, response.text
    body = response.json()
    assert set(body) == ENVELOPE_FIELDS
    assert body["success"] is True
    assert body["error"] is None
    assert body["data"] == {
        "event_id": event_id,
        "status": "accepted",
        "processing_status": "RECEIVED",
    }


def assert_error(response, status: int, code: str, retryable: bool) -> None:
    assert response.status_code == status, response.text
    body = response.json()
    assert set(body) == ERROR_FIELDS
    assert body["success"] is False
    assert body["data"] is None
    assert body["error"]["code"] == code
    assert body["retryable"] is retryable


def events(factory: sessionmaker[Session]) -> list[UsageEvent]:
    with factory() as session:
        return list(session.execute(select(UsageEvent).order_by(UsageEvent.id)).scalars())


def conflicts(factory: sessionmaker[Session]) -> list[UsageEventConflict]:
    with factory() as session:
        statement = select(UsageEventConflict).order_by(UsageEventConflict.id)
        return list(session.execute(statement).scalars())


def audits(factory: sessionmaker[Session], action: AuditAction) -> list[AuditLog]:
    with factory() as session:
        statement = select(AuditLog).where(AuditLog.action == action).order_by(AuditLog.id)
        return list(session.execute(statement).scalars())


def snapshot(row: UsageEvent) -> dict[str, object]:
    return {column.key: getattr(row, column.key) for column in UsageEvent.__table__.columns}


# --- 正常路径 --------------------------------------------------------------------------


def test_an_llm_token_event_is_received_and_stored(client, factory, credential) -> None:
    response = post(client, credential, llm_event(1))

    assert_accepted(response, uuid7(1))
    [row] = events(factory)
    with factory() as session:
        llm = usage_events.find_meter_type(session, "LLM_TOKEN")
        credential_row = session.execute(select(IntegrationCredential)).scalar_one()
    assert llm is not None
    assert row.status is UsageEventStatus.RECEIVED
    assert (row.event_id, row.schema_version, row.request_id) == (uuid7(1), "1.0", "call-1")
    assert row.conversation_id == "conv-1"
    assert (row.tenant_id, row.project_id) == (credential.tenant_id, credential.project_id)
    assert row.integration_credential_id == credential_row.id
    assert (row.provider_code_raw, row.model_code_raw) == ("anthropic", "claude-fictional-1")
    # Resolved by T-H later; empty at ingestion.
    assert (row.provider_id, row.model_id) == (None, None)
    assert row.usage_meter_type_id == llm.id
    assert row.payload_shape is PayloadShape.LLM_TOKEN_FIELDS
    assert (row.unit, row.quantity_kind) == ("TOKEN", QuantityKind.INTEGER)
    tokens = (
        row.input_tokens,
        row.output_tokens,
        row.cache_creation_input_tokens,
        row.cache_read_input_tokens,
    )
    assert tokens == (1200, 340, 0, 50)
    assert row.quantity is None
    assert row.occurred_at == dt.datetime(2026, 10, 5, 7, 59, 30, 250000)
    assert (row.received_at, row.created_at, row.processed_at) == (NOW, NOW, None)
    assert (row.error_code, row.error_message) == (None, None)
    assert len(row.payload_fingerprint) == 64
    assert len(row.public_id) == 36


@pytest.mark.parametrize(
    ("usage_type", "quantity", "unit", "kind"),
    [
        ("AUDIO_SECOND", "12.5", "SECOND", QuantityKind.DECIMAL),
        ("IMAGE_GENERATION", "2", "IMAGE", QuantityKind.INTEGER),
    ],
)
def test_quantity_events_are_received_and_stored(
    client,
    factory,
    credential,
    usage_type: str,
    quantity: str,
    unit: str,
    kind: QuantityKind,
) -> None:
    event = quantity_event(2, usage_type, quantity=quantity, unit=unit)

    assert_accepted(post(client, credential, event), uuid7(2))

    [row] = events(factory)
    assert row.payload_shape is PayloadShape.QUANTITY
    assert (row.quantity, row.unit, row.quantity_kind) == (Decimal(quantity), unit, kind)
    assert (row.input_tokens, row.output_tokens) == (None, None)
    assert (row.cache_creation_input_tokens, row.cache_read_input_tokens) == (None, None)
    # Absent from the payload: stored as NULL.
    assert row.conversation_id is None


def test_a_retired_meter_type_is_accepted(client, factory, credential) -> None:
    with factory() as session:
        session.execute(
            update(UsageMeterType)
            .where(UsageMeterType.code == "AUDIO_SECOND")
            .values(status=CatalogStatus.RETIRED)
        )
        session.commit()

    assert_accepted(post(client, credential, quantity_event(3)), uuid7(3))


def test_a_well_formed_model_missing_from_the_catalog_is_accepted(client, factory, credential):
    event = llm_event(4, provider="no-such-provider", model="no/such:model@v1")

    assert_accepted(post(client, credential, event), uuid7(4))
    [row] = events(factory)
    assert row.provider_code_raw == "no-such-provider"
    assert row.model_code_raw == "no/such:model@v1"
    assert (row.provider_id, row.model_id) == (None, None)


def test_a_failed_commit_never_answers_202(client, factory, credential, monkeypatch) -> None:
    """REQ-INGEST-001: 202 only after the row is committed. The commit raises: 500, no row."""

    def refuse(self: Session) -> None:
        raise RuntimeError("commit failed")

    monkeypatch.setattr(Session, "commit", refuse)
    response = post(client, credential, llm_event(5))
    monkeypatch.undo()

    assert_error(response, 500, "INTERNAL_ERROR", True)
    assert events(factory) == []


# --- 合法重复与冲突 --------------------------------------------------------------------


def test_a_retry_with_a_new_signature_is_a_duplicate(client, factory, credential, clock) -> None:
    event = llm_event(6)
    assert_accepted(post(client, credential, event), uuid7(6))
    clock.now = NOW + 7 * SECOND

    # A new attempt: new Request-Id, new timestamp, new signature, same event.
    response = post(client, credential, event, clock=clock)

    assert response.status_code == 200, response.text
    assert response.json()["data"] == {
        "event_id": uuid7(6),
        "status": "already_received",
        "processing_status": "RECEIVED",
    }
    assert len(events(factory)) == 1
    assert conflicts(factory) == []


def test_equivalent_spellings_are_the_same_event(client, factory, credential) -> None:
    """`"37.42"` and `"37.420"`; `…Z` and the same moment at `+08:00`: one fingerprint."""
    first = quantity_event(7, quantity="37.42", occurred_at=OCCURRED)
    second = quantity_event(7, quantity="37.420", occurred_at=OCCURRED_KL)
    assert_accepted(post(client, credential, first), uuid7(7))

    response = post(client, credential, second)

    assert response.status_code == 200, response.text
    assert response.json()["data"]["status"] == "already_received"
    [row] = events(factory)
    assert row.quantity == Decimal("37.42")
    assert conflicts(factory) == []


def test_a_processed_event_answers_already_processed(client, factory, credential) -> None:
    event = llm_event(8)
    assert_accepted(post(client, credential, event), uuid7(8))
    with factory() as session:
        session.execute(update(UsageEvent).values(status=UsageEventStatus.PROCESSED))
        session.commit()

    response = post(client, credential, event)

    assert response.status_code == 200, response.text
    assert response.json()["data"] == {
        "event_id": uuid7(8),
        "status": "already_processed",
        "processing_status": "PROCESSED",
    }


def test_the_next_key_version_is_a_duplicate(app, client, factory, credential) -> None:
    """Design §2: ownership compares the api_key, not the row; a retry while rotating is fine."""
    event = llm_event(9)
    assert_accepted(post(client, credential, event), uuid7(9))
    add_version(
        app,
        tenant_id=credential.tenant_id,
        project_id=credential.project_id,
        key=credential.api_key,
        key_version=2,
        plaintext=secret(2),
    )

    response = post(client, credential, event, key_version=2, signing_secret=secret(2))

    assert response.status_code == 200, response.text
    assert response.json()["data"]["status"] == "already_received"
    assert len(events(factory)) == 1
    assert conflicts(factory) == []


def test_a_different_fingerprint_is_a_conflict_that_changes_nothing(
    client,
    factory,
    credential,
) -> None:
    assert_accepted(post(client, credential, llm_event(10)), uuid7(10))
    [original] = events(factory)
    before = snapshot(original)

    response = post(client, credential, llm_event(10, output_tokens=341))

    assert_error(response, 409, "IDEMPOTENCY_CONFLICT", False)
    [after] = events(factory)
    assert snapshot(after) == before
    [conflict] = conflicts(factory)
    assert conflict.mismatch is ConflictMismatch.FINGERPRINT
    assert (conflict.usage_event_id, conflict.event_id) == (original.id, uuid7(10))
    assert conflict.api_key == credential.api_key
    assert conflict.payload_fingerprint != original.payload_fingerprint
    assert conflict.received_at == NOW
    [audit] = audits(factory, AuditAction.USAGE_EVENT_IDEMPOTENCY_CONFLICT)
    assert (audit.entity_type, audit.entity_id) == ("usage_event", original.public_id)
    assert audit.actor_user_id is None
    assert json.loads(audit.after_state or "") == {
        "event_id": uuid7(10),
        "api_key": credential.api_key,
        "key_version": 1,
        "mismatch": "FINGERPRINT",
    }


def test_another_api_key_is_an_ownership_conflict_that_reveals_nothing(
    app,
    client,
    factory,
    credential,
) -> None:
    event = llm_event(11)
    assert_accepted(post(client, credential, event), uuid7(11))
    [original] = events(factory)
    before = snapshot(original)
    stranger = make_credential(app, 2)

    same_payload = post(client, stranger, event)
    other_payload = post(client, stranger, llm_event(11, input_tokens=1))

    assert_error(same_payload, 409, "IDEMPOTENCY_CONFLICT", False)
    assert_error(other_payload, 409, "IDEMPOTENCY_CONFLICT", False)
    for response in (same_payload, other_payload):
        for secret_of_the_owner in (original.public_id, credential.tenant_public_id, "call-11"):
            assert secret_of_the_owner not in response.text
    assert [row.mismatch for row in conflicts(factory)] == [
        ConflictMismatch.OWNERSHIP,
        ConflictMismatch.BOTH,
    ]
    assert {row.api_key for row in conflicts(factory)} == {stranger.api_key}
    [after] = events(factory)
    assert snapshot(after) == before


def test_a_repeated_conflict_is_recorded_once(client, factory, credential) -> None:
    assert_accepted(post(client, credential, llm_event(12)), uuid7(12))

    for _ in range(3):
        response = post(client, credential, llm_event(12, cache_read_input_tokens=51))
        assert_error(response, 409, "IDEMPOTENCY_CONFLICT", False)

    assert len(conflicts(factory)) == 1
    assert len(audits(factory, AuditAction.USAGE_EVENT_IDEMPOTENCY_CONFLICT)) == 1


# --- 签名与凭据 ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "header",
    [
        "X-Acuven-Api-Key",
        "X-Acuven-Key-Version",
        "X-Acuven-Timestamp",
        "X-Acuven-Request-Id",
        "X-Acuven-Signature",
    ],
)
def test_a_missing_header_is_401(client, factory, credential, header: str) -> None:
    body = encode(llm_event(13))
    headers = signed_headers(credential, body, timestamp=epoch(NOW))
    del headers[header]

    response = client.post(PATH, content=body, headers=headers)

    assert_error(response, 401, "INTEGRATION_AUTH_FAILED", False)
    assert events(factory) == []


@pytest.mark.parametrize(
    ("header", "value"),
    [
        ("X-Acuven-Request-Id", "two words"),
        ("X-Acuven-Request-Id", "x" * 129),
        ("X-Acuven-Key-Version", "01"),
        ("X-Acuven-Key-Version", "0"),
        ("X-Acuven-Key-Version", "one"),
        ("X-Acuven-Api-Key", "ak-with-a-dash"),
        ("X-Acuven-Timestamp", "1790000000.5"),
    ],
)
def test_a_malformed_header_is_401(client, factory, credential, header: str, value: str) -> None:
    body = encode(llm_event(14))
    headers = signed_headers(credential, body, timestamp=epoch(NOW))
    headers[header] = value

    response = client.post(PATH, content=body, headers=headers)

    assert_error(response, 401, "INTEGRATION_AUTH_FAILED", False)
    assert events(factory) == []


def test_a_wrong_signature_is_401(client, factory, credential) -> None:
    response = post(client, credential, llm_event(15), signing_secret=secret(99))

    assert_error(response, 401, "INTEGRATION_AUTH_FAILED", False)
    assert events(factory) == []


def test_a_signature_over_other_bytes_is_401(client, factory, credential) -> None:
    """The signature covers the raw body: re-serialising the same JSON breaks it."""
    event = llm_event(16)
    headers = signed_headers(credential, encode(event), timestamp=epoch(NOW))
    respaced = json.dumps(event, indent=2).encode("utf-8")

    response = client.post(PATH, content=respaced, headers=headers)

    assert_error(response, 401, "INTEGRATION_AUTH_FAILED", False)
    assert events(factory) == []


def test_an_unknown_key_version_is_401(client, factory, credential) -> None:
    response = post(client, credential, llm_event(17), key_version=9)

    assert_error(response, 401, "INTEGRATION_AUTH_FAILED", False)
    assert events(factory) == []


@pytest.mark.parametrize(
    "row",
    [
        {"status": CredentialStatus.REVOKED},
        {"valid_until": NOW - SECOND},
        {"valid_until": NOW},
    ],
    ids=["revoked", "expired", "ends-now"],
)
def test_an_unusable_credential_is_401(app, client, factory, row: dict[str, object]) -> None:
    credential = make_credential(app, 3, **row)

    response = post(client, credential, llm_event(18))

    assert_error(response, 401, "INTEGRATION_AUTH_FAILED", False)
    assert events(factory) == []


@pytest.mark.parametrize("skew", [-301, 301])
def test_a_timestamp_out_of_the_window_is_401(client, factory, credential, skew: int) -> None:
    response = post(client, credential, llm_event(19), timestamp=epoch(NOW) + skew)

    assert_error(response, 401, "INTEGRATION_AUTH_FAILED", False)
    assert events(factory) == []


@pytest.mark.parametrize("skew", [-300, 300])
def test_a_timestamp_exactly_300_seconds_away_passes(client, credential, skew: int) -> None:
    response = post(client, credential, llm_event(20), timestamp=epoch(NOW) + skew)

    assert_accepted(response, uuid7(20))


def test_an_admin_session_or_no_signature_is_401(client, factory) -> None:
    """Only the integration signature counts; a Bearer token is not even read."""
    body = encode(llm_event(21))
    plain = {"Content-Type": "application/json"}
    bearer = {**plain, "Authorization": "Bearer not-a-real-admin-token"}

    for headers in (plain, bearer):
        response = client.post(PATH, content=body, headers=headers)
        assert_error(response, 401, "INTEGRATION_AUTH_FAILED", False)
    assert events(factory) == []


def test_revocation_takes_effect_on_the_next_request(client, factory, credential) -> None:
    """The decrypted secret is cached, the credential row is not: revoking is immediate."""
    assert_accepted(post(client, credential, llm_event(22)), uuid7(22))
    with factory() as session:
        session.execute(
            update(IntegrationCredential).values(
                status=CredentialStatus.REVOKED,
                revoked_at=NOW,
            )
        )
        session.commit()

    response = post(client, credential, llm_event(23))

    assert_error(response, 401, "INTEGRATION_AUTH_FAILED", False)
    assert len(events(factory)) == 1


# --- 重放与 nonce（REQ-AUTH-001） -------------------------------------------------------


def test_a_replayed_request_is_401_and_stores_nothing_twice(
    client,
    factory,
    credential,
    fake_redis: FakeRedis,
) -> None:
    """REQ-AUTH-001 replay evidence: the same request — same Request-Id, timestamp, signature."""
    body = encode(llm_event(24))
    headers = signed_headers(credential, body, timestamp=epoch(NOW), request_id="replay-1")

    first = client.post(PATH, content=body, headers=headers)
    second = client.post(PATH, content=body, headers=headers)

    assert_accepted(first, uuid7(24))
    assert_error(second, 401, "INTEGRATION_AUTH_FAILED", False)
    assert len(events(factory)) == 1
    key = f"nonce:{credential.api_key}:replay-1"
    assert fake_redis.calls == [(key, (epoch(NOW) + 300) * 1000)] * 2


def test_the_nonce_is_written_only_after_the_signature_verifies(client, credential, fake_redis):
    """Design §2: an unverified request never reaches Redis (it cannot burn a Request-Id)."""
    post(client, credential, llm_event(25), signing_secret=secret(99))
    post(client, credential, llm_event(25), timestamp=epoch(NOW) - 301)

    assert fake_redis.calls == []


def test_redis_down_lets_the_event_through_with_one_warning(
    app,
    client,
    factory,
    credential,
    caplog: pytest.LogCaptureFixture,
) -> None:
    app.state.nonce_store = NonceStore(DownRedis())

    with caplog.at_level(logging.WARNING, logger="app.services.usage_ingest"):
        response = post(client, credential, llm_event(26))

    assert_accepted(response, uuid7(26))
    assert len(events(factory)) == 1
    warnings = [r for r in caplog.records if r.getMessage() == "nonce_store_unavailable"]
    assert len(warnings) == 1
    assert warnings[0].levelno == logging.WARNING
    assert credential.secret not in caplog.text


# --- event_id 与字段校验 ---------------------------------------------------------------

# 大小写用例必须真有字母，否则换大小写什么也没变、用例悄悄退化成合法值。
_UUID7_UPPERCASE = "00000000-0000-7000-A000-000000000000"
_ULID_LOWERCASE = "0" * 25 + "a"
assert _UUID7_UPPERCASE != _UUID7_UPPERCASE.lower()
assert _ULID_LOWERCASE != _ULID_LOWERCASE.upper()


@pytest.mark.parametrize(
    ("event_id", "accepted"),
    [
        pytest.param(uuid7(27), True, id="uuid7"),
        pytest.param("0" * 25 + "1", True, id="ulid"),
        pytest.param(_UUID7_UPPERCASE, False, id="uuid7-uppercase"),
        pytest.param("0" * 27, False, id="ulid-too-long"),
        pytest.param(_ULID_LOWERCASE, False, id="ulid-lowercase"),
        pytest.param("00000000-0000-4000-8000-000000000027", False, id="uuid4"),
        pytest.param("00000000-0000-7000-c000-000000000027", False, id="uuid7-bad-variant"),
        pytest.param("8" + "0" * 25, False, id="ulid-overflow"),
        pytest.param("1", False, id="one"),
        pytest.param("test", False, id="test"),
        pytest.param("x" * 65, False, id="65-characters"),
    ],
)
def test_event_id_is_a_canonical_uuid7_or_ulid(
    client,
    factory,
    credential,
    event_id: str,
    accepted: bool,
) -> None:
    response = post(client, credential, llm_event(27, event_id=event_id))

    if accepted:
        assert_accepted(response, event_id)
        assert [row.event_id for row in events(factory)] == [event_id]
    else:
        assert_error(response, 422, "VALIDATION_ERROR", False)
        assert events(factory) == []


# (id, kind, changes, accepted). `kind` is "llm" or a QUANTITY meter type.
FIELD_CASES = [
    ("request-id-128", "llm", {"request_id": "r" * 128}, True),
    ("request-id-129", "llm", {"request_id": "r" * 129}, False),
    ("request-id-space", "llm", {"request_id": "a b"}, False),
    ("request-id-missing", "llm", {"request_id": MISSING}, False),
    ("conversation-null", "llm", {"conversation_id": None}, True),
    ("conversation-slash", "llm", {"conversation_id": "a/b"}, False),
    ("provider-64", "llm", {"provider": "p" * 64}, True),
    ("provider-65", "llm", {"provider": "p" * 65}, False),
    ("provider-uppercase", "llm", {"provider": "Anthropic"}, False),
    ("model-128", "llm", {"model": "m" * 128}, True),
    ("model-129", "llm", {"model": "m" * 129}, False),
    ("model-leading-dash", "llm", {"model": "-model"}, False),
    ("tokens-zero", "llm", {"input_tokens": 0}, True),
    ("tokens-max", "llm", {"input_tokens": 10**12}, True),
    ("tokens-over-max", "llm", {"input_tokens": 10**12 + 1}, False),
    ("tokens-negative", "llm", {"output_tokens": -1}, False),
    ("tokens-float", "llm", {"output_tokens": 1.0}, False),
    ("tokens-string", "llm", {"output_tokens": "5"}, False),
    ("tokens-boolean", "llm", {"output_tokens": True}, False),
    ("tokens-null", "llm", {"cache_read_input_tokens": None}, False),
    ("tokens-missing", "llm", {"cache_creation_input_tokens": MISSING}, False),
    ("llm-with-quantity", "llm", {"quantity": "1"}, False),
    ("llm-with-unit", "llm", {"unit": "TOKEN"}, False),
    ("quantity-with-tokens", "AUDIO_SECOND", {"input_tokens": 1}, False),
    ("quantity-with-null-tokens", "AUDIO_SECOND", {"input_tokens": None}, False),
    ("quantity-8-places", "AUDIO_SECOND", {"quantity": "1.12345678"}, True),
    ("quantity-9-places", "AUDIO_SECOND", {"quantity": "1.123456789"}, False),
    ("quantity-12-digits", "AUDIO_SECOND", {"quantity": "999999999999"}, True),
    ("quantity-13-digits", "AUDIO_SECOND", {"quantity": "1000000000000"}, False),
    ("quantity-zero", "AUDIO_SECOND", {"quantity": "0"}, True),
    ("quantity-negative", "AUDIO_SECOND", {"quantity": "-1"}, False),
    ("quantity-number", "AUDIO_SECOND", {"quantity": 12.5}, False),
    ("quantity-exponent", "AUDIO_SECOND", {"quantity": "1e3"}, False),
    ("quantity-missing", "AUDIO_SECOND", {"quantity": MISSING}, False),
    ("integer-kind-whole", "IMAGE_GENERATION", {"quantity": "3"}, True),
    ("integer-kind-point-zero", "IMAGE_GENERATION", {"quantity": "3.0"}, False),
    ("unit-other", "AUDIO_SECOND", {"unit": "MINUTE"}, False),
    ("unit-lowercase", "AUDIO_SECOND", {"unit": "second"}, False),
    ("unit-missing", "AUDIO_SECOND", {"unit": MISSING}, False),
    ("usage-type-number", "llm", {"usage_type": 1}, False),
    ("event-id-missing", "llm", {"event_id": MISSING}, False),
    ("schema-version-number", "llm", {"schema_version": 1.0}, False),
    ("tenant-id-65", "llm", {"tenant_id": "t" * 65}, False),
    ("unknown-field", "llm", {"note": "hello"}, False),
    ("customer-cost", "llm", {"customer_cost": "0.00000001"}, False),
    ("prompt", "llm", {"prompt": "fictional prompt text"}, False),
]


@pytest.mark.parametrize(
    ("kind", "changes", "accepted"),
    [pytest.param(kind, changes, ok, id=name) for name, kind, changes, ok in FIELD_CASES],
)
def test_field_rules(
    client,
    factory,
    credential,
    kind: str,
    changes: dict[str, object],
    accepted: bool,
) -> None:
    event = llm_event(28, **changes) if kind == "llm" else quantity_event(28, kind, **changes)

    response = post(client, credential, event)

    if accepted:
        assert_accepted(response, uuid7(28))
        assert len(events(factory)) == 1
    else:
        assert_error(response, 422, "VALIDATION_ERROR", False)
        assert events(factory) == []
        # The message names fields; it never echoes a value.
        for value in changes.values():
            if isinstance(value, str) and len(value) > 3:
                assert value not in response.json()["error"]["message"]


@pytest.mark.parametrize(
    "body",
    [b"", b"not json", b"[]", b'"text"', b'{"input_tokens": NaN}', b"\xff\xfe"],
    ids=["empty", "not-json", "array", "string", "nan", "not-utf8"],
)
def test_a_body_that_is_not_a_json_object_is_422(client, factory, credential, body: bytes):
    response = post(client, credential, body)

    assert_error(response, 422, "VALIDATION_ERROR", False)
    assert events(factory) == []


def test_an_unsupported_schema_version_is_422(client, factory, credential) -> None:
    response = post(client, credential, llm_event(29, schema_version="2.0"))

    assert_error(response, 422, "UNSUPPORTED_SCHEMA_VERSION", False)
    assert events(factory) == []


@pytest.mark.parametrize(
    "fields",
    [
        {"input_tokens": 1, "output_tokens": 1},
        {"quantity": "1", "unit": "LINE"},
        {"quantity": "1.5", "input_tokens": 1},
        {},
    ],
    ids=["token-fields", "quantity-fields", "mixed-groups", "no-fields"],
)
def test_an_unknown_usage_type_is_409_and_retryable(
    client,
    factory,
    credential,
    fields: dict[str, object],
) -> None:
    """Checked before the field group (design §2 order): never a VALIDATION_ERROR first."""
    event = llm_event(30, usage_type="TRANSLATED_LINE", **dict.fromkeys(TOKEN_FIELDS, MISSING))
    event.update(fields)

    response = post(client, credential, event)

    assert_error(response, 409, "UNKNOWN_USAGE_TYPE", True)
    assert events(factory) == []


def test_an_unknown_usage_type_succeeds_once_the_admin_creates_it(
    client,
    factory,
    credential,
) -> None:
    event = quantity_event(31, "TRANSLATED_LINE", quantity="4", unit="LINE")
    assert_error(post(client, credential, event), 409, "UNKNOWN_USAGE_TYPE", True)
    with factory() as session:
        session.add(
            UsageMeterType(
                public_id=public_id(999),
                code="TRANSLATED_LINE",
                display_name="Translated lines",
                payload_shape=PayloadShape.QUANTITY,
                unit="LINE",
                quantity_kind=QuantityKind.INTEGER,
                created_at=NOW,
                updated_at=NOW,
            )
        )
        session.commit()

    # The outbox retries the same event (a new attempt, a new Request-Id).
    assert_accepted(post(client, credential, event), uuid7(31))


# --- 时间 ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("occurred_at", "status", "code"),
    [
        pytest.param("2026-10-05T08:05:01Z", 422, "OCCURRED_AT_IN_FUTURE", id="301s-ahead"),
        pytest.param("2026-10-05T16:05:01+08:00", 422, "OCCURRED_AT_IN_FUTURE", id="kl-301s"),
        pytest.param("2026-10-05T08:05:00Z", 202, None, id="300s-ahead"),
        pytest.param("2019-01-01T00:00:00Z", 202, None, id="long-ago"),
        pytest.param("2026-10-05T07:00:00", 422, "VALIDATION_ERROR", id="no-time-zone"),
        pytest.param("2026-10-05 07:00:00Z", 422, "VALIDATION_ERROR", id="space"),
        pytest.param("2026-10-05T07:00:00.1234567Z", 422, "VALIDATION_ERROR", id="7-digits"),
        pytest.param("2026-02-30T07:00:00Z", 422, "VALIDATION_ERROR", id="no-such-day"),
    ],
)
def test_occurred_at(
    client,
    factory,
    credential,
    occurred_at: str,
    status: int,
    code: str | None,
) -> None:
    response = post(client, credential, llm_event(32, occurred_at=occurred_at))

    if code is None:
        assert_accepted(response, uuid7(32))
    else:
        assert_error(response, status, code, False)
        assert events(factory) == []


# --- 请求体上限 ------------------------------------------------------------------------


def test_a_body_over_16_kib_is_413_before_anything_else(client, factory) -> None:
    """Step ① comes first: no signature is looked at (none is even sent)."""
    body = b" " * (16 * 1024 + 1)

    response = client.post(PATH, content=body, headers={"Content-Type": "application/json"})

    assert_error(response, 413, "PAYLOAD_TOO_LARGE", False)
    assert response.json()["error"]["message"] == "request body too large"
    assert events(factory) == []


def test_a_body_of_exactly_16_kib_is_read(client, credential) -> None:
    compact = encode(llm_event(33))
    padded = compact + b" " * (16 * 1024 - len(compact))
    assert len(padded) == 16 * 1024

    assert_accepted(post(client, credential, padded), uuid7(33))


# --- 归属比对 --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "given",
    [
        {"tenant_id": public_id(77)},
        {"project_id": public_id(77)},
        # The tenant's own public id is not its project's.
        {"project_id": public_id(1)},
        {"tenant_id": public_id(1), "project_id": public_id(77)},
    ],
    ids=["tenant", "project", "tenant-id-as-project", "one-of-two"],
)
def test_a_payload_scope_that_is_not_the_credentials_is_403_and_audited(
    client,
    factory,
    credential,
    given: dict[str, str],
) -> None:
    response = post(client, credential, llm_event(34, **given))

    assert_error(response, 403, "CREDENTIAL_SCOPE_MISMATCH", False)
    assert events(factory) == []
    [audit] = audits(factory, AuditAction.USAGE_EVENT_SCOPE_MISMATCH)
    assert (audit.entity_type, audit.entity_id) == ("integration_credential", credential.api_key)
    assert audit.actor_user_id is None
    assert audit.user_agent == "testclient"
    # Exactly the design's fields: nothing else of the payload.
    assert json.loads(audit.after_state or "") == {
        "key_version": 1,
        "event_id": uuid7(34),
        "payload_tenant_id": given.get("tenant_id"),
        "payload_project_id": given.get("project_id"),
        "tenant_public_id": credential.tenant_public_id,
        "project_public_id": credential.project_public_id,
    }
    assert "call-34" not in (audit.after_state or "")


def test_a_payload_scope_that_matches_is_accepted(client, factory, credential) -> None:
    event = llm_event(
        35,
        tenant_id=credential.tenant_public_id,
        project_id=credential.project_public_id,
    )

    assert_accepted(post(client, credential, event), uuid7(35))
    assert audits(factory, AuditAction.USAGE_EVENT_SCOPE_MISMATCH) == []


# --- retryable 全覆盖 ------------------------------------------------------------------


class _Probe(BaseModel):
    number: int


def test_fastapis_own_validation_error_carries_retryable(app, client) -> None:
    """Any route under the integration prefix: FastAPI's 422 gets the flag too."""

    @app.post("/api/v1/integration/test-probe")
    def probe(payload: _Probe) -> dict[str, int]:
        return {"number": payload.number}

    response = client.post("/api/v1/integration/test-probe", json={"number": "not a number"})

    assert_error(response, 422, "VALIDATION_ERROR", False)


def test_framework_errors_under_the_prefix_carry_retryable(client) -> None:
    assert_error(client.get(PATH), 405, "HTTP_ERROR", False)
    assert_error(client.post("/api/v1/integration/nothing-here"), 404, "HTTP_ERROR", False)


def test_an_unexpected_error_is_500_and_retryable(client, credential, monkeypatch) -> None:
    def explode(*args: object, **kwargs: object) -> None:
        raise RuntimeError("unexpected")

    monkeypatch.setattr(usage_ingest, "ingest_one", explode)

    response = post(client, credential, llm_event(36))

    assert_error(response, 500, "INTERNAL_ERROR", True)
    assert "unexpected" not in response.text


def test_no_database_is_503_and_retryable(app, client, credential) -> None:
    app.state.session_factory = None

    response = post(client, credential, llm_event(37))

    assert_error(response, 503, "SERVICE_UNAVAILABLE", True)


def test_a_database_outage_is_503_and_retryable(client, factory, credential, monkeypatch):
    def unavailable(session: Session, code: str) -> None:
        raise OperationalError("SELECT", {}, Exception("server has gone away"))

    monkeypatch.setattr(usage_events, "find_meter_type", unavailable)

    response = post(client, credential, llm_event(38))

    assert_error(response, 503, "SERVICE_UNAVAILABLE", True)
    assert events(factory) == []


def test_responses_outside_the_prefix_keep_the_plain_envelope(client) -> None:
    """The flag is an additive extension for the integration endpoints only."""
    unauthenticated = client.get("/api/v1/admin/customers")
    missing = client.get("/api/v1/no-such-path")

    for response in (unauthenticated, missing):
        assert response.status_code in {401, 404}
        assert set(response.json()) == ENVELOPE_FIELDS


def test_success_responses_carry_no_retryable(client, credential) -> None:
    response = post(client, credential, llm_event(39))

    assert_accepted(response, uuid7(39))
    assert "retryable" not in response.json()


# --- last_used_at ----------------------------------------------------------------------


def last_used(factory: sessionmaker[Session]) -> dt.datetime | None:
    with factory() as session:
        statement = select(IntegrationCredential.last_used_at)
        return session.execute(statement).scalar_one()


def test_last_used_at_is_written_at_most_once_a_minute(client, factory, credential, clock):
    assert last_used(factory) is None

    assert_accepted(post(client, credential, llm_event(40), clock=clock), uuid7(40))
    assert last_used(factory) == NOW

    clock.now = NOW + 30 * SECOND
    assert_accepted(post(client, credential, llm_event(41), clock=clock), uuid7(41))
    assert last_used(factory) == NOW

    clock.now = NOW + 61 * SECOND
    assert_accepted(post(client, credential, llm_event(42), clock=clock), uuid7(42))
    assert last_used(factory) == NOW + 61 * SECOND


def test_a_duplicate_also_counts_as_a_use_but_a_rejection_does_not(
    client,
    factory,
    credential,
    clock,
) -> None:
    rejected = post(client, credential, llm_event(43), signing_secret=secret(99))
    assert rejected.status_code == 401
    assert last_used(factory) is None

    assert_accepted(post(client, credential, llm_event(43)), uuid7(43))
    clock.now = NOW + 61 * SECOND
    assert post(client, credential, llm_event(43), clock=clock).status_code == 200

    assert last_used(factory) == NOW + 61 * SECOND

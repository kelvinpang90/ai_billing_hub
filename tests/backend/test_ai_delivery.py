"""Pure delivery rules pinned by AI module design gate #281 v23."""

from __future__ import annotations

import datetime as dt
import json
import os
import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from types import SimpleNamespace

import pytest
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, delete, update
from sqlalchemy.orm import Session, sessionmaker

from alembic import command
from app.ai_api import client_rules, delivery
from app.ai_api.database import create_ai_session_factory
from app.ai_api.delivery import _outcome, _retry_delay
from app.ai_api.outbox import AiUsageOutbox, AiUsageOutboxStatus
from app.core.config import Settings, get_settings
from app.core.crypto import DecryptionFailed, encrypt_secret, load_keyring
from app.main import create_app
from app.models.integration import CredentialStatus, IntegrationCredential
from app.models.tenancy import Project, Tenant
from app.models.usage import UsageEvent

TEST_DATABASE_URL = os.environ.get("BILLING_TEST_DATABASE_URL", "")
_DEFAULT_CREDENTIAL = SimpleNamespace(
    public_api_key="ak_delivery_stub", key_version=1, encrypted_secret="stub"
)


@dataclass(frozen=True)
class World:
    factory: sessionmaker[Session]
    tenant_id: int
    project_id: int
    tenant_public_id: str
    project_public_id: str


@pytest.fixture
def mysql_world(monkeypatch) -> Iterator[World]:
    if not TEST_DATABASE_URL:
        pytest.skip("BILLING_TEST_DATABASE_URL is not set; these cases need real MySQL")
    monkeypatch.setenv("BILLING_DATABASE_URL", TEST_DATABASE_URL)
    get_settings.cache_clear()
    command.upgrade(Config("alembic.ini"), "head")
    get_settings.cache_clear()
    engine = create_engine(TEST_DATABASE_URL, pool_size=6, max_overflow=2)
    factory = create_ai_session_factory(engine)
    now = dt.datetime.now(dt.UTC).replace(tzinfo=None)
    with factory.begin() as session:
        tenant_public_id = str(uuid.uuid4())
        project_public_id = str(uuid.uuid4())
        tenant = Tenant(
            public_id=tenant_public_id,
            company_name="AI delivery",
            email="delivery@example.com",
            created_at=now,
            updated_at=now,
        )
        session.add(tenant)
        session.flush()
        project = Project(
            public_id=project_public_id,
            tenant_id=tenant.id,
            name="AI delivery",
            created_at=now,
            updated_at=now,
        )
        session.add(project)
        session.flush()
        world = World(factory, tenant.id, project.id, tenant_public_id, project_public_id)
    try:
        yield world
    finally:
        with engine.begin() as connection:
            connection.execute(
                delete(AiUsageOutbox).where(AiUsageOutbox.tenant_id == world.tenant_id)
            )
            connection.execute(delete(UsageEvent).where(UsageEvent.tenant_id == world.tenant_id))
            connection.execute(
                delete(IntegrationCredential).where(
                    IntegrationCredential.tenant_id == world.tenant_id
                )
            )
            connection.execute(delete(Project).where(Project.id == world.project_id))
            connection.execute(delete(Tenant).where(Tenant.id == world.tenant_id))
        engine.dispose()


def add_row(world: World, number: int, *, status=AiUsageOutboxStatus.PENDING) -> int:
    now = dt.datetime.now(dt.UTC).replace(tzinfo=None)
    event_id = f"01J000000000000000000000{number:02d}"
    payload = {
        "schema_version": "1.0",
        "event_id": event_id,
        "request_id": "01J00000000000000000000000",
        "feature": "crm.contact_summary",
        "tenant_id": world.tenant_public_id,
        "project_id": world.project_public_id,
        "provider": "anthropic",
        "model": "claude-model",
        "usage_type": "LLM_TOKEN",
        "input_tokens": 1,
        "output_tokens": 2,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "occurred_at": "2026-10-14T00:00:00.000000Z",
    }
    with world.factory.begin() as session:
        row = AiUsageOutbox(
            event_id=event_id,
            request_id=payload["request_id"],
            claim_id=None,
            tenant_id=world.tenant_id,
            project_id=world.project_id,
            feature=payload["feature"],
            payload_json=json.dumps(payload, separators=(",", ":")),
            status=status,
            attempts=0,
            next_attempt_at=now,
            last_error=None,
            first_attempted_at=None,
            created_at=now,
            delivered_at=None,
            resolved_at=None,
            review_required_at=None,
            reviewed_at=None,
            dead_acked_at=None,
        )
        session.add(row)
        session.flush()
        return row.id


def read(world: World, row_id: int) -> AiUsageOutbox:
    with world.factory() as session:
        return session.get(AiUsageOutbox, row_id)


def settings(**changes: object) -> Settings:
    values = {
        "ai_ingest_base_url": "http://api:8000",
        "ai_delivery_batch": 20,
        "ai_delivery_run_budget_seconds": 45,
        "ai_delivery_lease_seconds": 60,
    }
    values.update(changes)
    return Settings(**values)


def wire_delivery(monkeypatch, credential=_DEFAULT_CREDENTIAL) -> None:
    monkeypatch.setattr(delivery, "_resolve", lambda _url: ("127.0.0.1", "api", 8000))
    monkeypatch.setattr(delivery, "_credential", lambda *_args: credential)
    monkeypatch.setattr(delivery, "load_keyring", lambda _settings: None)
    monkeypatch.setattr(delivery, "decrypt_secret", lambda *_args, **_kwargs: "secret")


def receipt(row: AiUsageOutbox, status: int = 202, value: str = "accepted") -> tuple[int, bytes]:
    return status, json.dumps({"data": {"event_id": row.event_id, "status": value}}).encode()


def test_a_mismatched_success_receipt_is_retryable() -> None:
    delivered, retryable, error = _outcome(
        202,
        b'{"data":{"event_id":"other","status":"accepted"}}',
        "event",
    )

    assert (delivered, retryable, error) == (False, True, "INVALID_BILLING_RECEIPT")


def test_non_envelope_5xx_retries_but_other_non_envelopes_die() -> None:
    assert _outcome(503, b"not json", "event") == (False, True, "BILLING_UNAVAILABLE")
    assert _outcome(503, b'{"error":{"code":"IGNORED"}}', "event") == (
        False,
        True,
        "BILLING_UNAVAILABLE",
    )
    assert _outcome(422, b"not json", "event") == (False, False, "INVALID_BILLING_RESPONSE")


def test_backoff_includes_the_configured_jitter_bounds() -> None:
    assert _retry_delay(1, lambda _low, _high: 0.5) == 1
    assert _retry_delay(12, lambda _low, _high: 1.0) == 3600
    assert _retry_delay(20, lambda _low, _high: 0.5) == 1800


def test_delivery_claims_and_settles_with_an_injected_sender(mysql_world, monkeypatch) -> None:
    row_id, attempts = add_row(mysql_world, 1), []
    wire_delivery(monkeypatch)

    def send(row, *_args):
        attempts.append(json.loads(row.payload_json))
        return receipt(row)

    assert delivery.deliver_due(mysql_world.factory, settings(), send=send) == 1
    row = read(mysql_world, row_id)
    assert row.status is AiUsageOutboxStatus.DELIVERED
    assert row.claim_id is None and row.attempts == 1 and row.first_attempted_at is not None
    assert attempts[0]["event_id"] == row.event_id


def test_idempotent_redelivery_keeps_event_body_and_uses_a_new_attempt(
    mysql_world, monkeypatch
) -> None:
    row_id, seen = add_row(mysql_world, 2), []
    wire_delivery(monkeypatch)

    def send(row, *_args):
        seen.append(row.payload_json)
        return receipt(
            row,
            202 if len(seen) == 1 else 200,
            "accepted" if len(seen) == 1 else "already_processed",
        )

    assert delivery.deliver_due(mysql_world.factory, settings(), send=send) == 1
    with mysql_world.factory.begin() as session:
        session.execute(
            update(AiUsageOutbox)
            .where(AiUsageOutbox.id == row_id)
            .values(
                status=AiUsageOutboxStatus.PENDING,
                next_attempt_at=dt.datetime.now(dt.UTC),
                delivered_at=None,
            )
        )
    assert delivery.deliver_due(mysql_world.factory, settings(), send=send) == 1
    assert seen[0] == seen[1]
    assert read(mysql_world, row_id).attempts == 2


def test_old_claim_cannot_overwrite_a_newer_claim(mysql_world) -> None:
    row_id = add_row(mysql_world, 3)
    first = delivery.claim_one(mysql_world.factory, settings())
    assert first is not None
    with mysql_world.factory.begin() as session:
        session.execute(
            update(AiUsageOutbox)
            .where(AiUsageOutbox.id == row_id)
            .values(next_attempt_at=dt.datetime.now(dt.UTC) - dt.timedelta(seconds=1))
        )
    second = delivery.claim_one(mysql_world.factory, settings())
    assert second is not None and second.claim_id != first.claim_id
    assert not delivery.settle(
        mysql_world.factory, first, delivered=True, retryable=False, error=None
    )
    assert read(mysql_world, row_id).claim_id == second.claim_id


def test_sweep_recovers_a_pending_row_without_a_celery_message(mysql_world, monkeypatch) -> None:
    row_id = add_row(mysql_world, 4)
    wire_delivery(monkeypatch)
    assert (
        delivery.deliver_due(mysql_world.factory, settings(), send=lambda row, *_: receipt(row))
        == 1
    )
    assert read(mysql_world, row_id).status is AiUsageOutboxStatus.DELIVERED


def test_held_rows_are_never_claimed(mysql_world, monkeypatch) -> None:
    row_id = add_row(mysql_world, 5, status=AiUsageOutboxStatus.HELD)
    wire_delivery(monkeypatch)
    assert (
        delivery.deliver_due(mysql_world.factory, settings(), send=lambda row, *_: receipt(row))
        == 0
    )
    assert read(mysql_world, row_id).status is AiUsageOutboxStatus.HELD


def test_budget_exhaustion_does_not_claim_a_new_row(mysql_world, monkeypatch) -> None:
    row_id = add_row(mysql_world, 6)
    wire_delivery(monkeypatch)
    clock = iter((0.0, 1.0))
    monkeypatch.setattr(delivery, "monotonic", lambda: next(clock))
    assert (
        delivery.deliver_due(mysql_world.factory, settings(ai_delivery_run_budget_seconds=0.5)) == 0
    )
    assert read(mysql_world, row_id).attempts == 0


@pytest.mark.parametrize("missing", [True, False])
def test_credential_errors_are_retryable(mysql_world, monkeypatch, missing: bool) -> None:
    row_id = add_row(mysql_world, 10 + int(missing))
    wire_delivery(monkeypatch, None if missing else _DEFAULT_CREDENTIAL)
    if not missing:
        monkeypatch.setattr(
            delivery,
            "decrypt_secret",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(DecryptionFailed()),
        )
    assert delivery.deliver_due(mysql_world.factory, settings()) == 1
    row = read(mysql_world, row_id)
    assert row.status is AiUsageOutboxStatus.PENDING
    assert row.last_error == (
        "SIGNING_CREDENTIAL_MISSING" if missing else "SIGNING_CREDENTIAL_UNREADABLE"
    )


def test_delivery_lock_allows_only_one_concurrent_run_and_is_released(mysql_world) -> None:
    with delivery._delivery_lock(mysql_world.factory) as acquired:
        assert acquired

        def try_lock() -> bool:
            with delivery._delivery_lock(mysql_world.factory) as concurrent:
                return concurrent

        with ThreadPoolExecutor(max_workers=1) as pool:
            assert pool.submit(try_lock).result() is False
    with delivery._delivery_lock(mysql_world.factory) as acquired:
        assert acquired


def test_concurrent_claiming_has_one_winner(mysql_world) -> None:
    add_row(mysql_world, 20)
    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(
            pool.map(lambda _unused: delivery.claim_one(mysql_world.factory, settings()), range(2))
        )
    assert sum(claim is not None for claim in claims) == 1


def test_delivery_selects_the_latest_verifiable_signing_credential(mysql_world) -> None:
    now = dt.datetime.now(dt.UTC).replace(tzinfo=None)
    with mysql_world.factory.begin() as session:
        session.add_all(
            [
                IntegrationCredential(
                    tenant_id=mysql_world.tenant_id,
                    project_id=mysql_world.project_id,
                    public_api_key="ak_delivery_old",
                    key_version=1,
                    encrypted_secret="old",
                    encryption_key_version=1,
                    status=CredentialStatus.ACTIVE,
                    valid_from=now - dt.timedelta(days=2),
                    valid_until=None,
                    last_used_at=None,
                    created_at=now,
                    revoked_at=None,
                ),
                IntegrationCredential(
                    tenant_id=mysql_world.tenant_id,
                    project_id=mysql_world.project_id,
                    public_api_key="ak_delivery_new",
                    key_version=1,
                    encrypted_secret="new",
                    encryption_key_version=1,
                    status=CredentialStatus.ACTIVE,
                    valid_from=now - dt.timedelta(days=1),
                    valid_until=None,
                    last_used_at=None,
                    created_at=now,
                    revoked_at=None,
                ),
            ]
        )
    with mysql_world.factory() as session:
        assert (
            delivery._credential(session, mysql_world.project_id, now).public_api_key
            == "ak_delivery_new"
        )


def test_delivery_sender_reaches_the_real_ingestion_endpoint(
    mysql_world, monkeypatch, tmp_path
) -> None:
    key_file = tmp_path / "master.key"
    key_file.write_text("1:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=\n", encoding="utf-8")
    endpoint_settings = settings(master_key_file=str(key_file))
    keyring = load_keyring(endpoint_settings)
    encrypted, version = encrypt_secret(
        keyring, "sk_delivery_test", associated_data=b"integration_credentials|ak_delivery_test|1"
    )
    now = dt.datetime.now(dt.UTC).replace(tzinfo=None)
    with mysql_world.factory.begin() as session:
        session.add(
            IntegrationCredential(
                tenant_id=mysql_world.tenant_id,
                project_id=mysql_world.project_id,
                public_api_key="ak_delivery_test",
                key_version=1,
                encrypted_secret=encrypted,
                encryption_key_version=version,
                status=CredentialStatus.ACTIVE,
                valid_from=now - dt.timedelta(seconds=1),
                valid_until=None,
                last_used_at=None,
                created_at=now,
                revoked_at=None,
            )
        )
    row_id = add_row(mysql_world, 24)
    application = create_app(endpoint_settings)
    application.state.session_factory = mysql_world.factory
    application.state.engine = mysql_world.factory.kw["bind"]
    monkeypatch.setattr(delivery, "_resolve", lambda _url: ("127.0.0.1", "api", 8000))
    with TestClient(application, raise_server_exceptions=False) as client:

        def send(row, credential, secret, _endpoint):
            body = row.payload_json.encode()
            headers = client_rules.signed_headers(
                credential.public_api_key,
                credential.key_version,
                secret,
                body,
                timestamp=str(int(dt.datetime.now(dt.UTC).timestamp())),
                attempt_id=client_rules.new_ulid(),
            )
            response = client.post(client_rules.INGEST_PATH, content=body, headers=headers)
            return response.status_code, response.content

        assert delivery.deliver_due(mysql_world.factory, endpoint_settings, send=send) == 1
    assert read(mysql_world, row_id).status is AiUsageOutboxStatus.DELIVERED


def test_non_envelope_response_becomes_dead(mysql_world, monkeypatch) -> None:
    row_id = add_row(mysql_world, 21)
    wire_delivery(monkeypatch)
    assert (
        delivery.deliver_due(
            mysql_world.factory, settings(), send=lambda *_args: (422, b"not json")
        )
        == 1
    )
    row = read(mysql_world, row_id)
    assert (row.status, row.last_error, row.claim_id) == (
        AiUsageOutboxStatus.DEAD,
        "INVALID_BILLING_RESPONSE",
        None,
    )


def test_unavailable_ingestion_leaves_the_row_pending_after_one_attempt(
    mysql_world, monkeypatch
) -> None:
    row_id = add_row(mysql_world, 22)
    wire_delivery(monkeypatch)
    assert (
        delivery.deliver_due(
            mysql_world.factory, settings(), send=lambda *_args: (503, b"not json")
        )
        == 1
    )
    row = read(mysql_world, row_id)
    assert (row.status, row.attempts, row.last_error) == (
        AiUsageOutboxStatus.PENDING,
        1,
        "BILLING_UNAVAILABLE",
    )


def test_slow_receipt_timeout_is_settled_as_a_retryable_network_error(
    mysql_world, monkeypatch
) -> None:
    row_id = add_row(mysql_world, 25)
    wire_delivery(monkeypatch)

    def timeout(*_args):
        raise TimeoutError("delivery wall-clock limit exceeded")

    assert delivery.deliver_due(mysql_world.factory, settings(), send=timeout) == 1
    row = read(mysql_world, row_id)
    assert (row.status, row.last_error) == (AiUsageOutboxStatus.PENDING, "NETWORK_ERROR")


def test_resolution_failure_leaves_rows_pending_and_a_later_sweep_recovers(
    mysql_world, monkeypatch
) -> None:
    row_id = add_row(mysql_world, 23)
    monkeypatch.setattr(delivery, "_resolve", lambda _url: None)
    assert delivery.deliver_due(mysql_world.factory, settings()) == 0
    assert read(mysql_world, row_id).status is AiUsageOutboxStatus.PENDING
    wire_delivery(monkeypatch)
    assert (
        delivery.deliver_due(mysql_world.factory, settings(), send=lambda row, *_: receipt(row))
        == 1
    )
    assert read(mysql_world, row_id).status is AiUsageOutboxStatus.DELIVERED


def test_wall_clock_expiry_is_a_network_error_before_receipt_parsing(monkeypatch) -> None:
    class Response:
        status = 200

        def read(self, _limit):
            return b'{"data":{"event_id":"event","status":"accepted"}}'

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    class Opener:
        def open(self, *_args, **_kwargs):
            return Response()

    class Timer:
        def __init__(self, _seconds, callback):
            self.callback = callback

        def start(self):
            self.callback()

        def cancel(self):
            pass

    monkeypatch.setattr(delivery.urllib.request, "build_opener", lambda *_args: Opener())
    monkeypatch.setattr(delivery.threading, "Timer", Timer)
    with pytest.raises(TimeoutError):
        delivery.urllib_transport(delivery.urllib.request.Request("http://127.0.0.1"), 1)

"""Usage delivery (AI module design gate #281 v23, §2 投递 and the §7 rows of task 065)."""

from __future__ import annotations

import datetime as dt
import functools
import http.server
import json
import os
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from types import SimpleNamespace

import pytest
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, delete, select, update
from sqlalchemy.orm import Session, sessionmaker
from test_usage_billing import EVENT_AT, NOW, OPENING, _mysql_factory
from test_usage_billing import World as BillingWorld

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
from app.services.integration_auth import credential_aad
from app.services.usage_billing import Outcome

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


def add_row(
    world: World, number: int, *, status=AiUsageOutboxStatus.PENDING, **payload_changes: object
) -> int:
    now = dt.datetime.now(dt.UTC).replace(tzinfo=None)
    event_id = f"01J000000000000000000000{number:02d}"
    payload: dict[str, object] = {
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
    payload.update(payload_changes)
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


def wire_credential(monkeypatch, credential=_DEFAULT_CREDENTIAL) -> None:
    monkeypatch.setattr(delivery, "_credential", lambda *_args: credential)
    monkeypatch.setattr(delivery, "load_keyring", lambda _settings: None)
    monkeypatch.setattr(delivery, "decrypt_secret", lambda *_args, **_kwargs: "secret")


def wire_delivery(monkeypatch, credential=_DEFAULT_CREDENTIAL, port: int = 8000) -> None:
    monkeypatch.setattr(delivery, "_resolve", lambda _url: ("127.0.0.1", "api", port))
    wire_credential(monkeypatch, credential)


def receipt(row: AiUsageOutbox, status: int = 202, value: str = "accepted") -> tuple[int, bytes]:
    return status, json.dumps({"data": {"event_id": row.event_id, "status": value}}).encode()


def make_due(factory: sessionmaker[Session], row_id: int, **values: object) -> None:
    """Move the row's next attempt (or an expired lease) into the past."""
    past = dt.datetime.now(dt.UTC).replace(tzinfo=None) - dt.timedelta(seconds=1)
    with factory.begin() as session:
        session.execute(
            update(AiUsageOutbox)
            .where(AiUsageOutbox.id == row_id)
            .values(next_attempt_at=past, **values)
        )


def lock_is_free(factory: sessionmaker[Session]) -> bool:
    with delivery._delivery_lock(factory) as acquired:
        return acquired


def add_credential(
    world: World,
    api_key: str,
    key_version: int,
    valid_from: dt.datetime,
    *,
    status: CredentialStatus = CredentialStatus.ACTIVE,
    valid_until: dt.datetime | None = None,
) -> None:
    with world.factory.begin() as session:
        session.add(
            IntegrationCredential(
                tenant_id=world.tenant_id,
                project_id=world.project_id,
                public_api_key=api_key,
                key_version=key_version,
                encrypted_secret="not-a-real-ciphertext",
                encryption_key_version=1,
                status=status,
                valid_from=valid_from,
                valid_until=valid_until,
                last_used_at=None,
                created_at=valid_from,
                revoked_at=valid_from if status is CredentialStatus.REVOKED else None,
            )
        )


def selected_key(world: World) -> tuple[str, int]:
    now = dt.datetime.now(dt.UTC).replace(tzinfo=None)
    with world.factory() as session:
        chosen = delivery._credential(session, world.project_id, now)
        assert chosen is not None
        return chosen.public_api_key, chosen.key_version


def master_key_settings(tmp_path) -> Settings:
    key_file = tmp_path / "master.key"
    key_file.write_text("1:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=\n", encoding="utf-8")
    return settings(master_key_file=str(key_file))


def ingest_app(endpoint_settings: Settings, factory: sessionmaker[Session]):
    application = create_app(endpoint_settings)
    application.state.session_factory = factory
    application.state.engine = factory.kw["bind"]
    return application


def client_sender(client: TestClient, sent: list[dict[str, object]]):
    """A sender that signs like `_request` but posts through the in-process ingestion app."""

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
        sent.append(
            {
                "body": body,
                "attempt_id": headers["X-Acuven-Request-Id"],
                "status": response.status_code,
                "receipt": response.json().get("data", {}).get("status"),
            }
        )
        return response.status_code, response.content

    return send


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


@pytest.fixture
def billing_world(monkeypatch) -> Iterator[BillingWorld]:
    """test_usage_billing's world (prices, rate, rule, funded PREPAID tenants) on real MySQL."""
    factories = _mysql_factory(monkeypatch)
    world = BillingWorld(next(factories))
    try:
        yield world
    finally:
        with world.factory.begin() as session:
            session.execute(delete(AiUsageOutbox))
        next(factories, None)


def test_idempotent_replay_reaches_billing_once_with_one_body_and_new_attempt_ids(
    billing_world, monkeypatch, tmp_path
) -> None:
    """§7 幂等重放: a lease expires after a send, the row is sent again, then requeued."""
    world = billing_world
    world.publish_markup_usd()
    tenant = world.tenant()
    world.fund(tenant, OPENING)
    endpoint_settings = master_key_settings(tmp_path)
    with world.factory.begin() as session:
        credential = session.get(IntegrationCredential, tenant.credential_id)
        assert credential is not None
        credential.encrypted_secret, credential.encryption_key_version = encrypt_secret(
            load_keyring(endpoint_settings),
            "sk_delivery_test",
            associated_data=credential_aad(credential.public_api_key, credential.key_version),
        )
        signer = SimpleNamespace(
            public_api_key=credential.public_api_key, key_version=credential.key_version
        )
        project_public_id = session.scalar(
            select(Project.public_id).where(Project.id == tenant.project_id)
        )
    factory = create_ai_session_factory(world.factory.kw["bind"])
    outbox = World(factory, tenant.id, tenant.project_id, tenant.public_id, project_public_id)
    row_id = add_row(
        outbox,
        30,
        model="claude-x",
        input_tokens=1000,
        output_tokens=2000,
        occurred_at=EVENT_AT.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
    )
    monkeypatch.setattr(delivery, "_resolve", lambda _url: ("127.0.0.1", "api", 8000))
    sent: list[dict[str, object]] = []
    with TestClient(
        ingest_app(endpoint_settings, factory), raise_server_exceptions=False
    ) as client:
        send = client_sender(client, sent)
        stale = delivery.claim_one(factory, endpoint_settings)
        assert stale is not None
        send(stale, signer, "sk_delivery_test", None)
        make_due(factory, row_id)
        assert delivery.deliver_due(factory, endpoint_settings, send=send) == 1
        assert not delivery.settle(factory, stale, delivered=True, retryable=False, error=None)
        assert world.bill(NOW).outcomes == {Outcome.PROCESSED: 1}
        make_due(factory, row_id, status=AiUsageOutboxStatus.PENDING, delivered_at=None)
        assert delivery.deliver_due(factory, endpoint_settings, send=send) == 1

    row = read(outbox, row_id)
    assert (row.status, row.attempts, row.claim_id) == (AiUsageOutboxStatus.DELIVERED, 3, None)
    assert [(item["status"], item["receipt"]) for item in sent] == [
        (202, "accepted"),
        (200, "already_received"),
        (200, "already_processed"),
    ]
    assert len({item["body"] for item in sent}) == 1
    assert len({item["attempt_id"] for item in sent}) == 3
    assert world.count(UsageEvent, UsageEvent.tenant_id == tenant.id) == 1
    assert len(world.usage_rows(tenant)) == 1


def test_old_claim_cannot_overwrite_a_newer_claim(mysql_world) -> None:
    row_id = add_row(mysql_world, 3)
    first = delivery.claim_one(mysql_world.factory, settings())
    assert first is not None
    make_due(mysql_world.factory, row_id)
    second = delivery.claim_one(mysql_world.factory, settings())
    assert second is not None and second.claim_id != first.claim_id
    assert delivery.settle(mysql_world.factory, second, delivered=True, retryable=False, error=None)
    assert not delivery.settle(
        mysql_world.factory, first, delivered=False, retryable=True, error="NETWORK_ERROR"
    )
    row = read(mysql_world, row_id)
    assert (row.status, row.claim_id, row.last_error) == (
        AiUsageOutboxStatus.DELIVERED,
        None,
        None,
    )


def test_sweep_recovers_a_pending_row_without_a_celery_message(mysql_world, monkeypatch) -> None:
    row_id = add_row(mysql_world, 4)
    wire_delivery(monkeypatch)
    assert (
        delivery.deliver_due(mysql_world.factory, settings(), send=lambda row, *_: receipt(row))
        == 1
    )
    assert read(mysql_world, row_id).status is AiUsageOutboxStatus.DELIVERED


def test_a_claim_left_unsettled_is_delivered_after_its_lease_expires(
    mysql_world, monkeypatch
) -> None:
    """§7 Redis/Celery 丢失: the worker that claimed the row died before settling it."""
    row_id = add_row(mysql_world, 7)
    wire_delivery(monkeypatch)
    assert delivery.claim_one(mysql_world.factory, settings()) is not None

    def send(row, *_args):
        return receipt(row)

    assert delivery.deliver_due(mysql_world.factory, settings(), send=send) == 0
    assert read(mysql_world, row_id).status is AiUsageOutboxStatus.SENDING
    make_due(mysql_world.factory, row_id)
    assert delivery.deliver_due(mysql_world.factory, settings(), send=send) == 1
    row = read(mysql_world, row_id)
    assert (row.status, row.attempts, row.claim_id) == (AiUsageOutboxStatus.DELIVERED, 2, None)


@pytest.mark.parametrize("status", [AiUsageOutboxStatus.HELD, AiUsageOutboxStatus.DISCARDED])
def test_held_and_discarded_rows_are_never_claimed(mysql_world, monkeypatch, status) -> None:
    row_id = add_row(mysql_world, 5, status=status)
    wire_delivery(monkeypatch)
    assert (
        delivery.deliver_due(mysql_world.factory, settings(), send=lambda row, *_: receipt(row))
        == 0
    )
    row = read(mysql_world, row_id)
    assert (row.status, row.attempts) == (status, 0)


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


def test_a_missing_master_key_is_a_retryable_unreadable_credential(
    mysql_world, monkeypatch
) -> None:
    now = dt.datetime.now(dt.UTC).replace(tzinfo=None)
    add_credential(mysql_world, "ak_delivery_nokey", 1, now - dt.timedelta(seconds=1))
    row_id = add_row(mysql_world, 12)
    monkeypatch.setattr(delivery, "_resolve", lambda _url: ("127.0.0.1", "api", 8000))
    assert delivery.deliver_due(mysql_world.factory, settings(master_key_file="")) == 1
    row = read(mysql_world, row_id)
    assert (row.status, row.attempts, row.last_error) == (
        AiUsageOutboxStatus.PENDING,
        1,
        "SIGNING_CREDENTIAL_UNREADABLE",
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


def test_concurrent_claiming_claims_every_due_row_exactly_once(mysql_world) -> None:
    row_ids = {add_row(mysql_world, number) for number in range(40, 46)}
    start = threading.Barrier(4)

    def claim_all(_unused) -> list[int]:
        start.wait()
        won = []
        for _attempt in range(len(row_ids)):
            claim = delivery.claim_one(mysql_world.factory, settings())
            if claim is not None:
                won.append(claim.id)
        return won

    with ThreadPoolExecutor(max_workers=4) as pool:
        claimed = [row_id for won in pool.map(claim_all, range(4)) for row_id in won]
    while (late := delivery.claim_one(mysql_world.factory, settings())) is not None:
        claimed.append(late.id)
    assert sorted(claimed) == sorted(row_ids)


def test_selection_ignores_key_version_and_breaks_valid_from_ties_by_id(mysql_world) -> None:
    now = dt.datetime.now(dt.UTC).replace(tzinfo=None)
    tie = now - dt.timedelta(days=1)
    add_credential(mysql_world, "ak_delivery_old", 9, now - dt.timedelta(days=2))
    add_credential(mysql_world, "ak_delivery_first", 1, tie)
    add_credential(mysql_world, "ak_delivery_second", 2, tie)
    assert selected_key(mysql_world) == ("ak_delivery_second", 2)


def test_selection_during_rotation_takes_the_newest_verifiable_row(mysql_world) -> None:
    now = dt.datetime.now(dt.UTC).replace(tzinfo=None)
    hour = dt.timedelta(hours=1)
    second = dt.timedelta(seconds=1)
    add_credential(mysql_world, "ak_delivery_rotated", 1, now - 2 * hour, valid_until=now + hour)
    add_credential(mysql_world, "ak_delivery_rotated", 2, now - dt.timedelta(minutes=1))
    add_credential(mysql_world, "ak_delivery_rotated", 3, now + hour)
    add_credential(
        mysql_world, "ak_delivery_revoked", 1, now - second, status=CredentialStatus.REVOKED
    )
    add_credential(
        mysql_world, "ak_delivery_expired", 1, now - 2 * second, valid_until=now - second
    )
    assert selected_key(mysql_world) == ("ak_delivery_rotated", 2)


def test_delivery_sender_reaches_the_real_ingestion_endpoint(
    mysql_world, monkeypatch, tmp_path
) -> None:
    endpoint_settings = master_key_settings(tmp_path)
    encrypted, version = encrypt_secret(
        load_keyring(endpoint_settings),
        "sk_delivery_test",
        associated_data=credential_aad("ak_delivery_test", 1),
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
    monkeypatch.setattr(delivery, "_resolve", lambda _url: ("127.0.0.1", "api", 8000))
    application = ingest_app(endpoint_settings, mysql_world.factory)
    with TestClient(application, raise_server_exceptions=False) as client:
        send = client_sender(client, [])
        assert delivery.deliver_due(mysql_world.factory, endpoint_settings, send=send) == 1
    assert read(mysql_world, row_id).status is AiUsageOutboxStatus.DELIVERED


@pytest.mark.parametrize(
    ("status", "settled", "error"),
    [
        (502, AiUsageOutboxStatus.PENDING, "BILLING_UNAVAILABLE"),
        (400, AiUsageOutboxStatus.DEAD, "INVALID_BILLING_RESPONSE"),
    ],
)
def test_non_envelope_responses_retry_on_5xx_and_die_otherwise(
    mysql_world, monkeypatch, status: int, settled: AiUsageOutboxStatus, error: str
) -> None:
    row_id = add_row(mysql_world, 21)
    wire_delivery(monkeypatch)
    assert (
        delivery.deliver_due(
            mysql_world.factory, settings(), send=lambda *_args: (status, b"not json")
        )
        == 1
    )
    row = read(mysql_world, row_id)
    assert (row.status, row.last_error, row.claim_id) == (settled, error, None)


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


class _SlowReceipt(http.server.BaseHTTPRequestHandler):
    """Headers at once, then one byte of the body every 0.2 s (well under the socket timeout)."""

    def do_POST(self) -> None:
        self.rfile.read(int(self.headers["Content-Length"]))
        self.send_response(202)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", "100")
        self.end_headers()
        try:
            for _byte in range(100):
                self.wfile.write(b" ")
                self.wfile.flush()
                time.sleep(0.2)
        except OSError:
            pass

    def log_message(self, *_args) -> None:
        pass


def test_a_slow_receipt_is_cut_at_the_send_limit_and_the_next_run_reclaims_the_row(
    mysql_world, monkeypatch
) -> None:
    """§7 慢速回执, through the real `_request` with the send limit shrunk to 1 s."""
    row_id = add_row(mysql_world, 25)
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _SlowReceipt)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    wire_delivery(monkeypatch, port=server.server_address[1])
    monkeypatch.setattr(
        delivery, "urllib_transport", functools.partial(delivery.urllib_transport, wall_limit=1.0)
    )
    try:
        started = time.monotonic()
        assert delivery.deliver_due(mysql_world.factory, settings()) == 1
        assert time.monotonic() - started < 5
    finally:
        server.shutdown()
        server.server_close()
    row = read(mysql_world, row_id)
    assert (row.status, row.last_error, row.claim_id) == (
        AiUsageOutboxStatus.PENDING,
        "NETWORK_ERROR",
        None,
    )
    assert lock_is_free(mysql_world.factory)
    make_due(mysql_world.factory, row_id)
    assert (
        delivery.deliver_due(mysql_world.factory, settings(), send=lambda row, *_: receipt(row))
        == 1
    )
    row = read(mysql_world, row_id)
    assert (row.status, row.attempts) == (AiUsageOutboxStatus.DELIVERED, 2)


def test_a_stuck_resolver_is_killed_claims_nothing_and_the_next_run_recovers(
    mysql_world, monkeypatch
) -> None:
    """§7 解析卡住与恢复: the real `_resolve`, its child first swapped for one that never ends."""
    row_ids = [add_row(mysql_world, 26), add_row(mysql_world, 27)]
    wire_credential(monkeypatch)
    children: list[subprocess.Popen] = []

    class RecordedPopen(subprocess.Popen):
        def __init__(self, *args, **kwargs) -> None:
            super().__init__(*args, **kwargs)
            children.append(self)

    def stuck(_argv, **kwargs):
        return subprocess.run([sys.executable, "-c", "import time; time.sleep(60)"], **kwargs)

    monkeypatch.setattr(subprocess, "Popen", RecordedPopen)
    monkeypatch.setattr(
        delivery,
        "subprocess",
        SimpleNamespace(run=stuck, SubprocessError=subprocess.SubprocessError),
    )
    started = time.monotonic()
    assert delivery.deliver_due(mysql_world.factory, settings()) == 0
    assert 2 <= time.monotonic() - started < 10
    assert len(children) == 1 and children[0].returncode is not None
    for row_id in row_ids:
        row = read(mysql_world, row_id)
        assert (row.status, row.attempts, row.first_attempted_at) == (
            AiUsageOutboxStatus.PENDING,
            0,
            None,
        )
    assert lock_is_free(mysql_world.factory)

    monkeypatch.setattr(delivery, "subprocess", subprocess)
    recovered = settings(ai_ingest_base_url="http://localhost:8000")
    assert (
        delivery.deliver_due(mysql_world.factory, recovered, send=lambda row, *_: receipt(row)) == 2
    )
    assert {read(mysql_world, row_id).status for row_id in row_ids} == {
        AiUsageOutboxStatus.DELIVERED
    }
    assert (delivery.deliver_due.time_limit, delivery.deliver_due.soft_time_limit) == (59, 57)
    assert delivery.deliver_due.time_limit < settings().ai_delivery_lease_seconds


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

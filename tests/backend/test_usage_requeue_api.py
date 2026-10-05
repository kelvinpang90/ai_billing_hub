"""Requeueing usage events, end to end on SQLite (design gate #181 v2 §2「重新入队」, §7).

The design §7 row **重新入队** (API half): single and bulk; an event that is PROCESSED or
RECEIVED is 409 alone and skipped in bulk; an event the ledger already has (a LEDGER_CONFLICT) is
refused the same way; each requeue is one transaction with its `USAGE_EVENT_REQUEUE` audit
(`entity_type` usage_event, `entity_id` the event's public_id, the reason, the previous status,
error code and attempts); the requeued event is billed by the next sweep; bulk filters, its limit
and its counts; request validation (422) and unknown ids (404); the response carries counts and
public ids only; "the first statement is `require_admin`" (AST).

The database half — the trigger refusing a direct UPDATE of a billed event back to RECEIVED — is in
test_usage_billing_concurrency.py; anonymous 401 / CUSTOMER 403 without writes is in
test_admin_customers_api.py. Amounts are fictional exact binary fractions (see
test_usage_billing.py); uuids are zero placeholders.
"""

from __future__ import annotations

import ast
import base64
import datetime as dt
import itertools
import json
import os
import pathlib
import uuid
from collections.abc import Iterator
from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select, update
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.config import Settings
from app.core.database import create_session_factory
from app.core.tokens import issue_access_token
from app.main import create_app
from app.models.ai_catalog import SEED_METER_TYPES, UsageMeterComponent, UsageMeterType
from app.models.auth import AuditAction, AuditLog, User, UserRole, UserStatus
from app.models.base import Base
from app.models.integration import CredentialStatus, IntegrationCredential
from app.models.pricing_rules import PricingScope, PricingStrategy
from app.models.tenancy import Project, Tenant
from app.models.usage import UsageEvent, UsageEventStatus
from app.models.wallet import ReferenceType, TransactionType
from app.repositories import usage_events as event_repository
from app.repositories import wallet as wallet_repository
from app.schemas.provider_prices import ComponentInput
from app.services import (
    ai_catalog,
    customers,
    pricing_rules,
    provider_prices,
    usage_billing,
    usage_requeue,
)
from app.services.auth import RequestContext, utc_now

SINGLE = "/api/v1/admin/usage-events/{usage_event_id}/requeue"
BULK = "/api/v1/admin/usage-events/requeue"
ROUTER_FILE = pathlib.Path("app/api/admin_usage_events.py")
CONTEXT = RequestContext(ip_address="203.0.113.9", user_agent="usage-requeue-test")
TEST_EMAIL_DOMAIN = "@usage-requeue-test.example.com"
ZERO_ID = "00000000-0000-4000-8000-000000000000"

T0 = dt.datetime(2026, 9, 29, 8, 30, 0)
EVENT_AT = dt.datetime(2026, 9, 29, 9, 30, 0)
NOW = dt.datetime(2026, 9, 29, 10, 30, 0)
# Fictional MYR prices per audio second (exact binary fractions).
AUDIO_COST = "0.03125"
REASON = "Price for the model published after the events arrived"

_NUMBERS = itertools.count(1)

# SQLite stand-in for migration 0006's AFTER INSERT trigger (as in test_usage_billing.py).
_SQLITE_LEDGER_TRIGGER = """
CREATE TRIGGER test_wallet_follows_ledger
AFTER INSERT ON wallet_transactions
FOR EACH ROW
BEGIN
    UPDATE wallets
       SET balance = NEW.balance_after, version = NEW.wallet_sequence,
           updated_at = NEW.created_at
     WHERE id = NEW.wallet_id;
END
"""


class Clock:
    def __init__(self, moment: dt.datetime) -> None:
        self.now = moment

    def __call__(self) -> dt.datetime:
        return self.now


def settings_for(tmp_path) -> Settings:
    key = tmp_path / "jwt.key"
    key.write_text("a-test-signing-key-that-is-long-enough", encoding="utf-8")
    master = tmp_path / "master.key"
    master.write_text(f"1:{base64.b64encode(os.urandom(32)).decode()}\n", encoding="utf-8")
    return Settings(jwt_secret_file=str(key), master_key_file=str(master), database_url="")


def seed_meter_types(factory: sessionmaker[Session]) -> None:
    """What migration 0012 seeds, for the SQLite tables `create_all` builds empty."""
    with factory() as session:
        for number, seed in enumerate(SEED_METER_TYPES, start=1):
            kind = UsageMeterType(
                public_id=f"00000000-0000-4000-8000-{number:012d}",
                code=seed.code,
                display_name=seed.display_name,
                payload_shape=seed.payload_shape,
                unit=seed.unit,
                quantity_kind=seed.quantity_kind,
                created_at=T0,
                updated_at=T0,
            )
            session.add(kind)
            session.flush()
            for part in seed.components:
                session.add(
                    UsageMeterComponent(
                        meter_type_id=kind.id,
                        payload_shape=seed.payload_shape,
                        component_code=part.component_code,
                        quantity_field=part.quantity_field,
                        created_at=T0,
                    )
                )
        session.commit()


@pytest.fixture
def app(tmp_path) -> Iterator[FastAPI]:
    application = create_app(settings_for(tmp_path))
    # StaticPool: TestClient runs sync endpoints in a thread pool; one shared in-memory database.
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.exec_driver_sql(_SQLITE_LEDGER_TRIGGER)
    application.state.engine = engine
    application.state.session_factory = create_session_factory(engine)
    seed_meter_types(application.state.session_factory)
    yield application
    engine.dispose()


@pytest.fixture
def client(app) -> TestClient:
    return TestClient(app)


class World:
    """An admin, provider `openai` with model `whisper-x`, tenants; AUDIO_SECOND events."""

    def __init__(self, factory: sessionmaker[Session]) -> None:
        self.factory = factory
        self.clock = Clock(T0)
        with factory() as session:
            admin = User(
                email=f"admin-{next(_NUMBERS)}{TEST_EMAIL_DOMAIN}",
                password_hash="not-a-real-hash",
                role=UserRole.ADMIN,
                status=UserStatus.ACTIVE,
                created_at=T0,
                updated_at=T0,
            )
            session.add(admin)
            session.commit()
            self.admin = admin
        self.provider = ai_catalog.create_provider(
            factory,
            actor=self.admin,
            code="openai",
            display_name="Fictional provider",
            context=CONTEXT,
            clock=self.clock,
        )
        self.model = ai_catalog.create_model(
            factory,
            actor=self.admin,
            provider_id=self.provider.id,
            code="whisper-x",
            display_name="Fictional model",
            context=CONTEXT,
            clock=self.clock,
        )

    def tenant(self) -> tuple[str, int, int, int]:
        """(customer public_id, tenant id, project id, credential id), the wallet funded."""
        number = next(_NUMBERS)
        detail = customers.create_customer(
            self.factory,
            actor=self.admin,
            company_name=f"Fictional {number} Sdn Bhd",
            email=f"tenant-{number}{TEST_EMAIL_DOMAIN}",
            context=CONTEXT,
            now=T0,
        )
        project = customers.create_project(
            self.factory,
            actor=self.admin,
            customer_id=detail.id,
            name="Chatbot",
            context=CONTEXT,
            now=T0,
        )
        with self.factory() as session:
            tenant_id = session.execute(
                select(Tenant.id).where(Tenant.public_id == detail.id)
            ).scalar_one()
            project_id = session.execute(
                select(Project.id).where(Project.public_id == project.id)
            ).scalar_one()
            credential = IntegrationCredential(
                tenant_id=tenant_id,
                project_id=project_id,
                public_api_key=f"ak_{number:032d}",
                key_version=1,
                encrypted_secret="not-a-real-ciphertext",
                encryption_key_version=1,
                status=CredentialStatus.ACTIVE,
                valid_from=T0,
                created_at=T0,
            )
            session.add(credential)
            session.flush()
            wallet_repository.post_transaction(
                session,
                tenant_id=tenant_id,
                transaction_type=TransactionType.ADJUSTMENT_CREDIT,
                amount=Decimal("10"),
                reference_type=ReferenceType.ADMIN_ADJUSTMENT,
                reference_id=f"fund-{number}",
                now=T0,
                created_by=self.admin.id,
                description="Fictional top-up for a requeue test",
                actor_role="ADMIN",
            )
            session.commit()
            return detail.id, tenant_id, project_id, credential.id

    def publish(self) -> None:
        """An MYR price for audio seconds and a global MARKUP of 2."""
        component = ComponentInput.model_validate(
            {"component_code": "AUDIO_SECOND", "unit_quantity": "1", "rate_amount": AUDIO_COST}
        )
        draft = provider_prices.create_draft(
            self.factory,
            actor=self.admin,
            provider_id=self.provider.id,
            model_id=self.model.id,
            source_currency="MYR",
            source_reference="Fictional price page",
            components=[component],
            context=CONTEXT,
            clock=self.clock,
        )
        provider_prices.publish(
            self.factory,
            actor=self.admin,
            price_version_id=draft.id,
            effective_from=None,
            context=CONTEXT,
            clock=self.clock,
        )
        rule = pricing_rules.create_draft(
            self.factory,
            actor=self.admin,
            priority_scope=PricingScope.GLOBAL,
            customer_id=None,
            provider_id=None,
            model_id=None,
            strategy=PricingStrategy.MARKUP,
            markup_multiplier=Decimal("2"),
            components=None,
            context=CONTEXT,
            clock=self.clock,
        )
        pricing_rules.publish(
            self.factory,
            actor=self.admin,
            rule_id=rule.id,
            effective_from=None,
            context=CONTEXT,
            clock=self.clock,
        )

    def event(
        self,
        tenant: tuple[str, int, int, int],
        *,
        model: str = "whisper-x",
        occurred_at: dt.datetime = EVENT_AT,
    ) -> int:
        number = next(_NUMBERS)
        _public_id, tenant_id, project_id, credential_id = tenant
        with self.factory() as session:
            meter = event_repository.find_meter_type(session, "AUDIO_SECOND")
            assert meter is not None
            row = event_repository.insert_event(
                session,
                event_id=f"00000000-0000-7000-8000-{number:012d}",
                schema_version="1.0",
                tenant_id=tenant_id,
                project_id=project_id,
                integration_credential_id=credential_id,
                request_id=f"call-{number}",
                conversation_id=None,
                provider_code_raw="openai",
                model_code_raw=model,
                usage_meter_type_id=meter.id,
                payload_shape=meter.payload_shape,
                quantity_kind=meter.quantity_kind,
                unit=meter.unit,
                tokens=None,
                quantity=Decimal("2"),
                payload_fingerprint="0" * 64,
                occurred_at=occurred_at,
                now=T0,
            )
            session.commit()
            return row.id

    def bill(self, at: dt.datetime = NOW) -> usage_billing.SweepResult:
        return usage_billing.bill_pending_events(
            self.factory, lease_seconds=120, max_attempts=10, clock=lambda: at
        )

    def row(self, event_id: int) -> UsageEvent:
        with self.factory() as session:
            found = session.get(UsageEvent, event_id)
            assert found is not None
            session.expunge(found)
            return found

    def audits(self) -> list[AuditLog]:
        statement = (
            select(AuditLog)
            .where(AuditLog.action == AuditAction.USAGE_EVENT_REQUEUE)
            .order_by(AuditLog.id)
        )
        with self.factory() as session:
            found = list(session.execute(statement).scalars())
            session.expunge_all()
            return found

    def statuses(self) -> dict[int, tuple[UsageEventStatus, str | None, int]]:
        statement = select(
            UsageEvent.id, UsageEvent.status, UsageEvent.error_code, UsageEvent.attempt_count
        )
        with self.factory() as session:
            return {row[0]: (row[1], row[2], row[3]) for row in session.execute(statement)}


@pytest.fixture
def world(app) -> World:
    return World(app.state.session_factory)


@pytest.fixture
def admin(app, world) -> dict[str, str]:
    token = issue_access_token(
        app.state.settings,
        user_id=world.admin.id,
        role="ADMIN",
        session_id=uuid.uuid4().hex,
        now=utc_now(),
    )
    return {"Authorization": f"Bearer {token}"}


def error_code(response) -> str | None:
    return (response.json().get("error") or {}).get("code")


def requeue_one(client: TestClient, headers, world: World, event_id: int, **body):
    public_id = world.row(event_id).public_id
    return client.post(
        SINGLE.format(usage_event_id=public_id), json={"reason": REASON, **body}, headers=headers
    )


# --- single ------------------------------------------------------------------------------


def test_a_pricing_error_event_is_requeued_with_its_audit_and_billed_next(
    client, admin, world
) -> None:
    tenant = world.tenant()
    event_id = world.event(tenant)
    world.bill()
    before = world.row(event_id)
    assert (before.status, before.error_code, before.attempt_count) == (
        UsageEventStatus.PRICING_ERROR,
        "NO_PROVIDER_PRICE",
        1,
    )

    response = requeue_one(client, admin, world, event_id)

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["data"] == {"id": before.public_id, "status": "RECEIVED"}
    after = world.row(event_id)
    assert (after.status, after.error_code, after.error_message) == (
        UsageEventStatus.RECEIVED,
        None,
        None,
    )
    assert (after.attempt_count, after.next_attempt_at) == (0, None)
    [audit] = world.audits()
    assert (audit.entity_type, audit.entity_id, audit.reason) == (
        "usage_event",
        before.public_id,
        REASON,
    )
    assert audit.actor_user_id == world.admin.id
    assert json.loads(audit.before_state or "{}") == {
        "status": "PRICING_ERROR",
        "error_code": "NO_PROVIDER_PRICE",
        "attempt_count": 1,
    }
    assert json.loads(audit.after_state or "{}") == {"status": "RECEIVED"}

    # The administrator fixed the price; the next sweep bills it.
    world.publish()
    assert world.bill().outcomes == {usage_billing.Outcome.PROCESSED: 1}
    assert world.row(event_id).status is UsageEventStatus.PROCESSED


@pytest.mark.parametrize("state", ["processed", "received"])
def test_an_event_that_is_not_in_an_error_state_is_409(client, admin, world, state: str) -> None:
    tenant = world.tenant()
    if state == "processed":
        world.publish()
    event_id = world.event(tenant)
    if state == "processed":
        world.bill()
    before = world.statuses()

    response = requeue_one(client, admin, world, event_id)

    assert (response.status_code, error_code(response)) == (409, "USAGE_EVENT_NOT_REQUEUABLE")
    assert world.statuses() == before
    assert world.audits() == []


def test_an_event_with_a_ledger_row_is_never_requeued(client, admin, world) -> None:
    """A LEDGER_CONFLICT (FAILED_FINAL with a ledger row) had a financial effect: 409."""
    tenant = world.tenant()
    world.publish()
    event_id = world.event(tenant)
    with world.factory() as session:
        wallet_repository.post_transaction(
            session,
            tenant_id=tenant[1],
            transaction_type=TransactionType.AI_USAGE,
            amount=Decimal("-0.0625"),
            reference_type=ReferenceType.USAGE_EVENT,
            reference_id=world.row(event_id).event_id,
            now=T0,
        )
        session.commit()
    world.bill()
    assert world.row(event_id).error_code == "LEDGER_CONFLICT"

    response = requeue_one(client, admin, world, event_id)

    assert (response.status_code, error_code(response)) == (409, "USAGE_EVENT_NOT_REQUEUABLE")
    assert world.row(event_id).status is UsageEventStatus.FAILED_FINAL
    assert world.audits() == []


def test_an_unknown_event_is_404(client, admin, world) -> None:
    response = client.post(
        SINGLE.format(usage_event_id=ZERO_ID), json={"reason": REASON}, headers=admin
    )

    assert (response.status_code, error_code(response)) == (404, "USAGE_EVENT_NOT_FOUND")


@pytest.mark.parametrize(
    "body",
    [{}, {"reason": "   "}, {"reason": "x" * 256}, {"reason": REASON, "status": "RECEIVED"}],
    ids=["no-reason", "blank-reason", "long-reason", "extra-field"],
)
def test_a_single_requeue_needs_exactly_a_reason(client, admin, world, body) -> None:
    tenant = world.tenant()
    event_id = world.event(tenant)
    world.bill()
    public_id = world.row(event_id).public_id

    response = client.post(SINGLE.format(usage_event_id=public_id), json=body, headers=admin)

    assert (response.status_code, error_code(response)) == (422, "VALIDATION_ERROR")
    assert world.row(event_id).status is UsageEventStatus.PRICING_ERROR


# --- bulk --------------------------------------------------------------------------------


def _bulk(client: TestClient, headers, **body):
    return client.post(BULK, json={"reason": REASON, **body}, headers=headers)


def test_bulk_requeues_the_matching_events_one_audit_each(client, admin, world) -> None:
    a, b = world.tenant(), world.tenant()
    pricing = [world.event(a), world.event(a), world.event(b)]
    unknown = world.event(a, model="no-such-model")
    world.bill()

    response = _bulk(client, admin, status="PRICING_ERROR", customer_id=a[0])

    assert response.status_code == 200
    data = response.json()["data"]
    expected = [world.row(event_id).public_id for event_id in pricing[:2]]
    assert data == {"requeued": 2, "skipped": 0, "ids": expected}
    statuses = world.statuses()
    assert [statuses[event_id][0] for event_id in pricing] == [
        UsageEventStatus.RECEIVED,
        UsageEventStatus.RECEIVED,
        UsageEventStatus.PRICING_ERROR,
    ]
    assert statuses[unknown][0] is UsageEventStatus.MODEL_UNKNOWN
    assert [audit.entity_id for audit in world.audits()] == expected
    # No amount, cost or internal id in the response.
    assert set(data) == {"requeued", "skipped", "ids"}


def test_bulk_filters_by_code_model_and_period(client, admin, world) -> None:
    tenant = world.tenant()
    before_the_period = EVENT_AT - dt.timedelta(hours=1)
    early = world.event(tenant, model="no-such-model", occurred_at=before_the_period)
    inside = world.event(tenant, model="no-such-model")
    other_model = world.event(tenant, model="other-model")
    world.bill()

    response = _bulk(
        client,
        admin,
        status="MODEL_UNKNOWN",
        provider="openai",
        model="no-such-model",
        occurred_from="2026-09-29T09:00:00Z",
        occurred_to="2026-09-29T10:00:00Z",
    )

    assert response.json()["data"]["ids"] == [world.row(inside).public_id]
    statuses = world.statuses()
    assert statuses[early][0] is UsageEventStatus.MODEL_UNKNOWN
    assert statuses[other_model][0] is UsageEventStatus.MODEL_UNKNOWN
    # An error code that no MODEL_UNKNOWN event has selects nothing.
    nothing = _bulk(client, admin, status="MODEL_UNKNOWN", error_code="NO_PRICING_RULE")
    assert nothing.json()["data"] == {"requeued": 0, "skipped": 0, "ids": []}


def test_bulk_skips_events_that_cannot_be_requeued_and_counts_them(client, admin, world) -> None:
    """Selected as FAILED_FINAL, but one has a ledger row: skipped, counted, not audited."""
    tenant = world.tenant()
    world.publish()
    conflicted, final = world.event(tenant), world.event(tenant)
    with world.factory() as session:
        wallet_repository.post_transaction(
            session,
            tenant_id=tenant[1],
            transaction_type=TransactionType.AI_USAGE,
            amount=Decimal("-0.0625"),
            reference_type=ReferenceType.USAGE_EVENT,
            reference_id=world.row(conflicted).event_id,
            now=T0,
        )
        statement = update(UsageEvent).where(UsageEvent.id == final)
        session.execute(
            statement.values(status=UsageEventStatus.FAILED_FINAL, error_code="ACCOUNT_CLOSED")
        )
        session.commit()
    world.bill()

    response = _bulk(client, admin, status="FAILED_FINAL")

    assert response.json()["data"] == {
        "requeued": 1,
        "skipped": 1,
        "ids": [world.row(final).public_id],
    }
    assert world.row(conflicted).status is UsageEventStatus.FAILED_FINAL
    assert [audit.entity_id for audit in world.audits()] == [world.row(final).public_id]


def test_bulk_takes_at_most_the_limit_oldest_first(client, admin, world, monkeypatch) -> None:
    monkeypatch.setattr(usage_requeue, "MAX_BULK_REQUEUE", 2)
    tenant = world.tenant()
    events = [world.event(tenant) for _ in range(3)]
    world.bill()

    response = _bulk(client, admin, status="PRICING_ERROR")

    expected = [world.row(event_id).public_id for event_id in events[:2]]
    assert response.json()["data"] == {"requeued": 2, "skipped": 0, "ids": expected}
    assert world.row(events[2]).status is UsageEventStatus.PRICING_ERROR
    assert usage_requeue.MAX_BULK_REQUEUE == 2


def test_the_bulk_limit_is_1000() -> None:
    assert usage_requeue.MAX_BULK_REQUEUE == 1000


@pytest.mark.parametrize(
    "body",
    [
        {"status": "PROCESSED"},
        {"status": "RECEIVED"},
        {},
        {"status": "PRICING_ERROR", "reason": ""},
        {"status": "PRICING_ERROR", "occurred_from": "2026-09-29T09:00:00"},
        {
            "status": "PRICING_ERROR",
            "occurred_from": "2026-09-29T10:00:00Z",
            "occurred_to": "2026-09-29T10:00:00Z",
        },
        {"status": "PRICING_ERROR", "tenant_id": ZERO_ID},
    ],
    ids=[
        "processed",
        "received",
        "no-status",
        "blank-reason",
        "no-time-zone",
        "empty-period",
        "unknown-field",
    ],
)
def test_bulk_validation(client, admin, world, body) -> None:
    payload = {"reason": REASON, **body}

    response = client.post(BULK, json=payload, headers=admin)

    assert (response.status_code, error_code(response)) == (422, "VALIDATION_ERROR")


def test_bulk_for_an_unknown_customer_is_404(client, admin, world) -> None:
    response = _bulk(client, admin, status="PRICING_ERROR", customer_id=ZERO_ID)

    assert (response.status_code, error_code(response)) == (404, "CUSTOMER_NOT_FOUND")


def test_nothing_is_counted_twice_across_two_bulk_calls(client, admin, world) -> None:
    tenant = world.tenant()
    world.event(tenant)
    world.bill()

    first = _bulk(client, admin, status="PRICING_ERROR").json()["data"]
    second = _bulk(client, admin, status="PRICING_ERROR").json()["data"]

    assert (first["requeued"], second["requeued"]) == (1, 0)
    with world.factory() as session:
        count = select(func.count()).select_from(AuditLog)
        audits = session.execute(count.where(AuditLog.action == AuditAction.USAGE_EVENT_REQUEUE))
        assert audits.scalar_one() == 1


# --- the handlers ------------------------------------------------------------------------


def _handlers() -> list[ast.FunctionDef]:
    tree = ast.parse(ROUTER_FILE.read_text(encoding="utf-8"))
    return [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and any(
            isinstance(decorator, ast.Call)
            and isinstance(decorator.func, ast.Attribute)
            and isinstance(decorator.func.value, ast.Name)
            and decorator.func.value.id == "router"
            for decorator in node.decorator_list
        )
    ]


def test_every_handler_starts_with_require_admin() -> None:
    handlers = _handlers()
    # Two endpoints; none would mean the AST scan found nothing.
    assert len(handlers) == 2

    for handler in handlers:
        body = handler.body
        first = body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            body = body[1:]
        statement = body[0]
        value = statement.value if isinstance(statement, ast.Assign | ast.Expr) else None
        assert isinstance(value, ast.Call), handler.name
        assert isinstance(value.func, ast.Name) and value.func.id == "require_admin"
        assert [ast.unparse(arg) for arg in value.args] == ["request"]


def test_the_paths_say_requeue_not_reprocess(app) -> None:
    paths = {route.path for route in app.routes if "usage-events" in getattr(route, "path", "")}

    assert paths == {SINGLE, BULK}
    assert not any("reprocess" in path for path in paths)

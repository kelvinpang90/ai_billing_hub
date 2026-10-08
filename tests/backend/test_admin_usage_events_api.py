"""The admin usage event query, end to end on SQLite (spec §61 / §89, AIH-TASK-034).

- **列表** — every filter alone (customer, project, conversation, request, provider, model,
  period, status, error code) and in combination; an unknown customer or project is an empty page;
  customer isolation (filtering on A never returns B, a project of A with customer B is empty);
  pagination and newest-first order; the period is half-open and converted from any zone to UTC;
  bad times and an empty period are 422 after authentication; the exact item fields;
- **详情** — the MYR snapshot (both FX fields empty), the USD snapshot with its FX version and
  rate, the margin with `basis = estimated`, the ledger row's `public_id` and amount, a 0 MYR event
  without a ledger row, an error event without a snapshot, the claim fields of a PROCESSING event,
  the conflicts recorded for its `event_id`, 404 for an unknown id, the exact detail fields;
- **越权** — anonymous 401, CUSTOMER 403 (every admin route: test_admin_customers_api.py).

No internal id, credential secret or signature appears in any response. Amounts are fictional
exact binary fractions (see test_usage_billing.py); uuids are zero placeholders.
"""

from __future__ import annotations

import base64
import datetime as dt
import itertools
import os
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, update
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.config import Settings
from app.core.database import create_session_factory
from app.core.tokens import issue_access_token
from app.main import create_app
from app.models.ai_catalog import SEED_METER_TYPES, UsageMeterComponent, UsageMeterType
from app.models.auth import User, UserRole, UserStatus
from app.models.base import Base
from app.models.integration import CredentialStatus, IntegrationCredential
from app.models.pricing_rules import PricingScope, PricingStrategy
from app.models.tenancy import BillingMode, Project, Tenant
from app.models.usage import ConflictMismatch, UsageEvent, UsageEventStatus
from app.models.wallet import ReferenceType, TransactionType, WalletTransaction
from app.repositories import usage_events as event_repository
from app.repositories import wallet as wallet_repository
from app.schemas.provider_prices import ComponentInput
from app.services import (
    ai_catalog,
    customers,
    fx_rates,
    pricing_rules,
    provider_prices,
    usage_billing,
)
from app.services.auth import RequestContext, utc_now

LIST = "/api/v1/admin/usage-events"
DETAIL = LIST + "/{usage_event_id}"
CONTEXT = RequestContext(ip_address="203.0.113.9", user_agent="usage-query-test")
TEST_EMAIL_DOMAIN = "@usage-query-test.example.com"
ZERO_ID = "00000000-0000-4000-8000-000000000000"
CIPHERTEXT = "not-a-real-ciphertext"

T0 = dt.datetime(2026, 9, 29, 8, 30, 0)
EVENT_AT = dt.datetime(2026, 9, 29, 9, 30, 0)
NOW = dt.datetime(2026, 9, 29, 10, 30, 0)
# Fictional prices per audio second (exact binary fractions); MARKUP 2 on top.
AUDIO_COST = "0.03125"
FX_RATE = "4.5"

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

SUMMARY_FIELDS = {
    "id",
    "event_id",
    "customer_id",
    "customer_company_name",
    "project_id",
    "request_id",
    "conversation_id",
    "provider",
    "model",
    "usage_type",
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
    "quantity",
    "unit",
    "status",
    "error_code",
    "occurred_at",
    "received_at",
    "processed_at",
    "billable_cost",
    "estimated_provider_cost_myr",
    "billing_mode_snapshot",
    "reference_customer_price",
}
DETAIL_FIELDS = SUMMARY_FIELDS | {
    "schema_version",
    "payload_shape",
    "quantity_kind",
    "payload_fingerprint",
    "error_message",
    "created_at",
    "provider_ref_id",
    "model_ref_id",
    "provider_price_version_id",
    "pricing_rule_id",
    "fx_rate_version_id",
    "fx_rate_applied",
    "provider_source_currency",
    "provider_source_cost",
    "gross_margin",
    "gross_margin_basis",
    "wallet_transaction",
    "attempt_count",
    "next_attempt_at",
    "claim_token",
    "claimed_at",
    "lease_expires_at",
    "conflicts",
}
SNAPSHOT_FIELDS = (
    "provider_price_version_id",
    "pricing_rule_id",
    "fx_rate_version_id",
    "fx_rate_applied",
    "provider_source_currency",
    "provider_source_cost",
    "estimated_provider_cost_myr",
    "billing_mode_snapshot",
    "reference_customer_price",
    "billable_cost",
    "gross_margin",
    "gross_margin_basis",
    "wallet_transaction",
    "processed_at",
)


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


@dataclass(frozen=True)
class Tenancy:
    """A customer, one of its projects and that project's credential."""

    public_id: str
    id: int
    project_public_id: str
    project_id: int
    credential_id: int
    api_key: str


class World:
    """An admin, provider `openai` with model `whisper-x`; tenants with funded wallets."""

    def __init__(self, factory: sessionmaker[Session], settings: Settings) -> None:
        self.factory = factory
        self.settings = settings
        self.clock = Clock(T0)
        self.admin = self.user(UserRole.ADMIN)
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

    def user(self, role: UserRole) -> User:
        with self.factory() as session:
            user = User(
                email=f"user-{next(_NUMBERS)}{TEST_EMAIL_DOMAIN}",
                password_hash="not-a-real-hash",
                role=role,
                status=UserStatus.ACTIVE,
                created_at=T0,
                updated_at=T0,
            )
            session.add(user)
            session.commit()
            session.expunge(user)
            return user

    def headers(self, user: User, role: str) -> dict[str, str]:
        token = issue_access_token(
            self.settings,
            user_id=user.id,
            role=role,
            session_id=uuid.uuid4().hex,
            now=utc_now(),
        )
        return {"Authorization": f"Bearer {token}"}

    def tenant(self) -> Tenancy:
        number = next(_NUMBERS)
        detail = customers.create_customer(
            self.factory,
            actor=self.admin,
            company_name=f"Fictional {number} Sdn Bhd",
            email=f"tenant-{number}{TEST_EMAIL_DOMAIN}",
            context=CONTEXT,
            now=T0,
        )
        with self.factory() as session:
            tenant_id = session.execute(
                select(Tenant.id).where(Tenant.public_id == detail.id)
            ).scalar_one()
            wallet_repository.post_transaction(
                session,
                tenant_id=tenant_id,
                transaction_type=TransactionType.ADJUSTMENT_CREDIT,
                amount=Decimal("10"),
                reference_type=ReferenceType.ADMIN_ADJUSTMENT,
                reference_id=f"fund-{number}",
                now=T0,
                created_by=self.admin.id,
                description="Fictional top-up for a usage query test",
                actor_role="ADMIN",
            )
            session.commit()
        return self.project(detail.id, tenant_id)

    def project(self, customer_public_id: str, tenant_id: int) -> Tenancy:
        """Another project (with its own credential) of an existing customer."""
        number = next(_NUMBERS)
        project = customers.create_project(
            self.factory,
            actor=self.admin,
            customer_id=customer_public_id,
            name=f"Chatbot {number}",
            context=CONTEXT,
            now=T0,
        )
        with self.factory() as session:
            project_id = session.execute(
                select(Project.id).where(Project.public_id == project.id)
            ).scalar_one()
            api_key = f"ak_{number:032d}"
            credential = IntegrationCredential(
                tenant_id=tenant_id,
                project_id=project_id,
                public_api_key=api_key,
                key_version=1,
                encrypted_secret=CIPHERTEXT,
                encryption_key_version=1,
                status=CredentialStatus.ACTIVE,
                valid_from=T0,
                created_at=T0,
            )
            session.add(credential)
            session.commit()
            return Tenancy(
                customer_public_id, tenant_id, project.id, project_id, credential.id, api_key
            )

    def publish(self, currency: str = "MYR") -> dict[str, str]:
        """A price for audio seconds in `currency` (and its FX rate), a global MARKUP of 2."""
        published = {}
        if currency != "MYR":
            draft = fx_rates.create_draft(
                self.factory,
                actor=self.admin,
                base_currency=currency,
                rate=Decimal(FX_RATE),
                observed_at=T0,
                source_reference="Fictional quote",
                context=CONTEXT,
                clock=self.clock,
            )
            published["fx"] = fx_rates.publish(
                self.factory,
                actor=self.admin,
                fx_rate_id=draft.id,
                effective_from=None,
                context=CONTEXT,
                clock=self.clock,
            ).id
        component = ComponentInput.model_validate(
            {"component_code": "AUDIO_SECOND", "unit_quantity": "1", "rate_amount": AUDIO_COST}
        )
        price = provider_prices.create_draft(
            self.factory,
            actor=self.admin,
            provider_id=self.provider.id,
            model_id=self.model.id,
            source_currency=currency,
            source_reference="Fictional price page",
            components=[component],
            context=CONTEXT,
            clock=self.clock,
        )
        published["price"] = provider_prices.publish(
            self.factory,
            actor=self.admin,
            price_version_id=price.id,
            effective_from=None,
            context=CONTEXT,
            clock=self.clock,
        ).id
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
        published["rule"] = pricing_rules.publish(
            self.factory,
            actor=self.admin,
            rule_id=rule.id,
            effective_from=None,
            context=CONTEXT,
            clock=self.clock,
        ).id
        return published

    def event(
        self,
        tenancy: Tenancy,
        *,
        provider: str = "openai",
        model: str = "whisper-x",
        conversation_id: str | None = None,
        request_id: str | None = None,
        occurred_at: dt.datetime = EVENT_AT,
        seconds: str = "2",
        tokens: dict[str, int] | None = None,
    ) -> int:
        """One RECEIVED event: AUDIO_SECOND, or LLM_TOKEN when `tokens` is given."""
        number = next(_NUMBERS)
        meter_code = "AUDIO_SECOND" if tokens is None else "LLM_TOKEN"
        with self.factory() as session:
            meter = event_repository.find_meter_type(session, meter_code)
            assert meter is not None
            row = event_repository.insert_event(
                session,
                event_id=f"00000000-0000-7000-8000-{number:012d}",
                schema_version="1.0",
                tenant_id=tenancy.id,
                project_id=tenancy.project_id,
                integration_credential_id=tenancy.credential_id,
                request_id=request_id or f"call-{number}",
                conversation_id=conversation_id,
                provider_code_raw=provider,
                model_code_raw=model,
                usage_meter_type_id=meter.id,
                payload_shape=meter.payload_shape,
                quantity_kind=meter.quantity_kind,
                unit=meter.unit,
                tokens=tokens,
                quantity=Decimal(seconds) if tokens is None else None,
                payload_fingerprint="0" * 64,
                occurred_at=occurred_at,
                now=T0,
            )
            session.commit()
            return row.id

    def set_status(self, event_id: int, status: UsageEventStatus, error_code: str) -> None:
        with self.factory() as session:
            statement = update(UsageEvent).where(UsageEvent.id == event_id)
            session.execute(statement.values(status=status, error_code=error_code))
            session.commit()

    def bill(self) -> usage_billing.SweepResult:
        return usage_billing.bill_pending_events(
            self.factory, lease_seconds=120, max_attempts=10, clock=lambda: NOW
        )

    def row(self, event_id: int) -> UsageEvent:
        with self.factory() as session:
            found = session.get(UsageEvent, event_id)
            assert found is not None
            session.expunge(found)
            return found

    def public_id(self, event_id: int) -> str:
        return self.row(event_id).public_id

    def ledger_row(self, event_id: int) -> WalletTransaction:
        statement = select(WalletTransaction).where(
            WalletTransaction.reference_type == ReferenceType.USAGE_EVENT,
            WalletTransaction.reference_id == self.row(event_id).event_id,
        )
        with self.factory() as session:
            found = session.execute(statement).scalar_one()
            session.expunge(found)
            return found


@pytest.fixture
def world(app) -> World:
    return World(app.state.session_factory, app.state.settings)


@pytest.fixture
def admin(world) -> dict[str, str]:
    return world.headers(world.admin, "ADMIN")


def error_code(response) -> str | None:
    return (response.json().get("error") or {}).get("code")


def listed(client: TestClient, headers, **params) -> list[str]:
    response = client.get(LIST, params=params, headers=headers)
    assert response.status_code == 200, response.text
    return [item["id"] for item in response.json()["data"]["items"]]


def detail(client: TestClient, headers, world: World, event_id: int) -> dict:
    response = client.get(DETAIL.format(usage_event_id=world.public_id(event_id)), headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["data"]


# --- the list: filters -------------------------------------------------------------------


class Corpus:
    """Customers A (two projects) and B; one event per filter dimension, all RECEIVED but two.

    `shared` (customer A) and `b` (customer B) use the same `request_id`: requests are not unique
    across customers, so filtering by request alone returns both.
    """

    def __init__(self, world: World) -> None:
        self.world = world
        self.a = world.tenant()
        self.a_second = world.project(self.a.public_id, self.a.id)
        self.b = world.tenant()
        later = EVENT_AT + dt.timedelta(hours=1)
        self.events = {
            "shared": world.event(self.a, request_id="shared-call"),
            "conversation": world.event(self.a, conversation_id="conv-1"),
            "provider": world.event(self.a, provider="anthropic"),
            "model": world.event(self.a, model="whisper-y"),
            "later": world.event(self.a, occurred_at=later),
            "pricing": world.event(self.a),
            "closed": world.event(self.a),
            "second_project": world.event(self.a_second),
            "b": world.event(self.b, request_id="shared-call"),
        }
        world.set_status(self.events["pricing"], UsageEventStatus.PRICING_ERROR, "NO_PRICING_RULE")
        world.set_status(self.events["closed"], UsageEventStatus.FAILED_FINAL, "ACCOUNT_CLOSED")

    def ids(self, *names: str) -> list[str]:
        """The events' public ids, newest first (by row id)."""
        rows = sorted((self.events[name] for name in names), reverse=True)
        return [self.world.public_id(row) for row in rows]


@pytest.fixture
def corpus(world) -> Corpus:
    return Corpus(world)


ALL = (
    "shared",
    "conversation",
    "provider",
    "model",
    "later",
    "pricing",
    "closed",
    "second_project",
    "b",
)
OF_A = tuple(name for name in ALL if name != "b")

SINGLE_FILTERS: list[tuple[str, Callable[[Corpus], dict[str, str]], tuple[str, ...]]] = [
    ("none", lambda c: {}, ALL),
    ("customer-a", lambda c: {"customer_id": c.a.public_id}, OF_A),
    ("customer-b", lambda c: {"customer_id": c.b.public_id}, ("b",)),
    ("project", lambda c: {"project_id": c.a_second.project_public_id}, ("second_project",)),
    ("conversation", lambda c: {"conversation_id": "conv-1"}, ("conversation",)),
    ("request", lambda c: {"request_id": "shared-call"}, ("shared", "b")),
    ("provider", lambda c: {"provider": "anthropic"}, ("provider",)),
    ("model", lambda c: {"model": "whisper-y"}, ("model",)),
    ("occurred-from", lambda c: {"occurred_from": "2026-09-29T10:00:00Z"}, ("later",)),
    (
        "occurred-to",
        lambda c: {"occurred_to": "2026-09-29T10:00:00Z"},
        tuple(name for name in ALL if name != "later"),
    ),
    ("status", lambda c: {"status": "PRICING_ERROR"}, ("pricing",)),
    ("error-code", lambda c: {"error_code": "ACCOUNT_CLOSED"}, ("closed",)),
]


@pytest.mark.parametrize(
    ("params", "expected"),
    [(params, expected) for _, params, expected in SINGLE_FILTERS],
    ids=[name for name, _, _ in SINGLE_FILTERS],
)
def test_each_filter_alone(client, admin, corpus, params, expected) -> None:
    assert listed(client, admin, **params(corpus)) == corpus.ids(*expected)


COMBINED_FILTERS: list[tuple[str, Callable[[Corpus], dict[str, str]], tuple[str, ...]]] = [
    (
        "request-of-customer-a",
        lambda c: {"request_id": "shared-call", "customer_id": c.a.public_id},
        ("shared",),
    ),
    (
        "request-of-customer-b",
        lambda c: {"request_id": "shared-call", "customer_id": c.b.public_id},
        ("b",),
    ),
    (
        "customer-and-status",
        lambda c: {"customer_id": c.a.public_id, "status": "PRICING_ERROR"},
        ("pricing",),
    ),
    (
        "other-customer-and-status",
        lambda c: {"customer_id": c.b.public_id, "status": "PRICING_ERROR"},
        (),
    ),
    (
        "status-and-error-code",
        lambda c: {"status": "FAILED_FINAL", "error_code": "ACCOUNT_CLOSED"},
        ("closed",),
    ),
    (
        "status-and-another-status-error-code",
        lambda c: {"status": "PRICING_ERROR", "error_code": "ACCOUNT_CLOSED"},
        (),
    ),
    (
        "customer-and-project",
        lambda c: {"customer_id": c.a.public_id, "project_id": c.a_second.project_public_id},
        ("second_project",),
    ),
    (
        "project-of-a-with-customer-b",
        lambda c: {"customer_id": c.b.public_id, "project_id": c.a.project_public_id},
        (),
    ),
    (
        "provider-model-period-customer",
        lambda c: {
            "provider": "openai",
            "model": "whisper-x",
            "occurred_from": "2026-09-29T09:00:00Z",
            "occurred_to": "2026-09-29T10:00:00Z",
            "customer_id": c.a.public_id,
        },
        ("shared", "conversation", "pricing", "closed", "second_project"),
    ),
    (
        "conversation-and-status",
        lambda c: {"conversation_id": "conv-1", "status": "RECEIVED"},
        ("conversation",),
    ),
    (
        "unknown-customer",
        lambda c: {"customer_id": ZERO_ID},
        (),
    ),
    (
        "unknown-project",
        lambda c: {"project_id": ZERO_ID},
        (),
    ),
]


@pytest.mark.parametrize(
    ("params", "expected"),
    [(params, expected) for _, params, expected in COMBINED_FILTERS],
    ids=[name for name, _, _ in COMBINED_FILTERS],
)
def test_filters_combine_with_and(client, admin, corpus, params, expected) -> None:
    response = client.get(LIST, params=params(corpus), headers=admin)

    data = response.json()["data"]
    assert [item["id"] for item in data["items"]] == corpus.ids(*expected)
    assert data["total"] == len(expected)


def test_filtering_on_a_customer_never_returns_another_customers_events(
    client, admin, corpus
) -> None:
    response = client.get(LIST, params={"customer_id": corpus.a.public_id}, headers=admin)

    items = response.json()["data"]["items"]
    assert {item["customer_id"] for item in items} == {corpus.a.public_id}
    assert corpus.world.public_id(corpus.events["b"]) not in {item["id"] for item in items}
    assert {item["project_id"] for item in items} == {
        corpus.a.project_public_id,
        corpus.a_second.project_public_id,
    }


# --- the list: pages, order, period ------------------------------------------------------


def test_pages_are_newest_first_with_the_total(client, admin, world) -> None:
    tenant = world.tenant()
    events = [world.event(tenant) for _ in range(5)]
    newest_first = [world.public_id(event_id) for event_id in reversed(events)]

    pages = [
        client.get(LIST, params={"page": page, "page_size": 2}, headers=admin).json()["data"]
        for page in (1, 2, 3, 4)
    ]

    assert [[item["id"] for item in page["items"]] for page in pages] == [
        newest_first[0:2],
        newest_first[2:4],
        newest_first[4:5],
        [],
    ]
    assert {(page["page"], page["page_size"], page["total"]) for page in pages} == {
        (1, 2, 5),
        (2, 2, 5),
        (3, 2, 5),
        (4, 2, 5),
    }


def test_the_default_page_is_20(client, admin, world) -> None:
    tenant = world.tenant()
    for _ in range(21):
        world.event(tenant)

    data = client.get(LIST, headers=admin).json()["data"]

    assert (len(data["items"]), data["page"], data["page_size"], data["total"]) == (20, 1, 20, 21)


@pytest.mark.parametrize(
    ("occurred_from", "occurred_to"),
    [
        ("2026-09-29T09:00:00Z", "2026-09-29T10:00:00Z"),
        # The same instants in Kuala Lumpur and New York time.
        ("2026-09-29T17:00:00+08:00", "2026-09-29T18:00:00+08:00"),
        ("2026-09-29T05:00:00-04:00", "2026-09-29T06:00:00-04:00"),
    ],
    ids=["utc", "plus-eight", "minus-four"],
)
def test_the_period_is_half_open_in_utc(
    client, admin, world, occurred_from: str, occurred_to: str
) -> None:
    tenant = world.tenant()
    before = world.event(tenant, occurred_at=dt.datetime(2026, 9, 29, 8, 59, 59, 999999))
    start = world.event(tenant, occurred_at=dt.datetime(2026, 9, 29, 9, 0, 0))
    last = world.event(tenant, occurred_at=dt.datetime(2026, 9, 29, 9, 59, 59, 999999))
    end = world.event(tenant, occurred_at=dt.datetime(2026, 9, 29, 10, 0, 0))

    found = listed(client, admin, occurred_from=occurred_from, occurred_to=occurred_to)

    assert found == [world.public_id(last), world.public_id(start)]
    assert world.public_id(before) not in found and world.public_id(end) not in found


@pytest.mark.parametrize(
    "params",
    [
        {"occurred_from": "2026-09-29T09:00:00"},
        {"occurred_to": "2026-09-29"},
        {"occurred_from": "2026-09-29T09:00:00.5Z"},
        {"occurred_from": "yesterday"},
        {"occurred_from": "2026-02-30T09:00:00Z"},
        {"occurred_from": "2026-09-29T10:00:00Z", "occurred_to": "2026-09-29T10:00:00Z"},
        {"occurred_from": "2026-09-29T18:00:01+08:00", "occurred_to": "2026-09-29T10:00:00Z"},
        {"status": "DONE"},
        {"page": 0},
        {"page_size": 101},
    ],
    ids=[
        "no-time-zone",
        "a-date",
        "fraction-of-a-second",
        "not-a-time",
        "not-on-the-calendar",
        "empty-period",
        "reversed-period-across-zones",
        "unknown-status",
        "page-zero",
        "page-too-large",
    ],
)
def test_query_validation(client, admin, world, params) -> None:
    response = client.get(LIST, params=params, headers=admin)

    assert (response.status_code, error_code(response)) == (422, "VALIDATION_ERROR")
    # 只列字段名，不回显值。
    for value in params.values():
        if isinstance(value, str):
            assert value not in response.json()["error"]["message"]


def test_the_period_is_checked_after_authentication(client, world) -> None:
    response = client.get(LIST, params={"occurred_from": "yesterday"})

    assert (response.status_code, error_code(response)) == (401, "TOKEN_INVALID")


# --- the list: fields --------------------------------------------------------------------


def test_list_items_carry_the_documented_fields_only(client, admin, world) -> None:
    world.publish()
    tenant = world.tenant()
    audio = world.event(tenant, conversation_id="conv-7")
    tokens = world.event(
        tenant,
        model="no-such-model",
        tokens={
            "input_tokens": 10,
            "output_tokens": 20,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 5,
        },
    )
    world.bill()

    response = client.get(LIST, headers=admin)

    [token_item, audio_item] = response.json()["data"]["items"]
    assert set(audio_item) == SUMMARY_FIELDS
    assert set(token_item) == SUMMARY_FIELDS
    audio_row, token_row = world.row(audio), world.row(tokens)
    with world.factory() as session:
        company = session.execute(
            select(Tenant.company_name).where(Tenant.id == tenant.id)
        ).scalar_one()
    assert audio_item == {
        "id": audio_row.public_id,
        "event_id": audio_row.event_id,
        "customer_id": tenant.public_id,
        "customer_company_name": company,
        "project_id": tenant.project_public_id,
        "request_id": audio_row.request_id,
        "conversation_id": "conv-7",
        "provider": "openai",
        "model": "whisper-x",
        "usage_type": "AUDIO_SECOND",
        "input_tokens": None,
        "output_tokens": None,
        "cache_creation_input_tokens": None,
        "cache_read_input_tokens": None,
        "quantity": "2.00000000",
        "unit": audio_row.unit,
        "status": "PROCESSED",
        "error_code": None,
        "occurred_at": "2026-09-29T09:30:00",
        "received_at": "2026-09-29T08:30:00",
        "processed_at": "2026-09-29T10:30:00",
        # 2 s × 0.03125 = 0.0625 MYR; MARKUP 2.
        "billable_cost": "0.12500000",
        "estimated_provider_cost_myr": "0.06250000",
        "billing_mode_snapshot": "PREPAID",
        "reference_customer_price": None,
    }
    assert token_item["id"] == token_row.public_id
    assert (token_item["usage_type"], token_item["status"]) == ("LLM_TOKEN", "MODEL_UNKNOWN")
    assert (token_item["input_tokens"], token_item["output_tokens"]) == (10, 20)
    assert (token_item["cache_creation_input_tokens"], token_item["cache_read_input_tokens"]) == (
        0,
        5,
    )
    assert (token_item["quantity"], token_item["billable_cost"]) == (None, None)
    assert token_item["estimated_provider_cost_myr"] is None


# --- the detail --------------------------------------------------------------------------


def test_an_myr_event_has_its_snapshot_margin_and_ledger_row_without_fx(
    client, admin, world
) -> None:
    published = world.publish()
    tenant = world.tenant()
    event_id = world.event(tenant)
    world.bill()
    ledger = world.ledger_row(event_id)

    data = detail(client, admin, world, event_id)

    assert set(data) == DETAIL_FIELDS
    assert data["status"] == "PROCESSED"
    assert data["provider_ref_id"] == world.provider.id
    assert data["model_ref_id"] == world.model.id
    assert data["provider_price_version_id"] == published["price"]
    assert data["pricing_rule_id"] == published["rule"]
    assert (data["fx_rate_version_id"], data["fx_rate_applied"]) == (None, None)
    assert data["provider_source_currency"] == "MYR"
    assert data["provider_source_cost"] == "0.06250000"
    assert data["estimated_provider_cost_myr"] == "0.06250000"
    assert data["billable_cost"] == "0.12500000"
    assert (data["gross_margin"], data["gross_margin_basis"]) == ("0.06250000", "estimated")
    assert data["wallet_transaction"] == {"id": ledger.public_id, "amount": "-0.12500000"}
    assert data["attempt_count"] == 1
    assert (data["claim_token"], data["claimed_at"], data["lease_expires_at"]) == (None,) * 3
    assert data["next_attempt_at"] is None
    assert data["conflicts"] == []
    row = world.row(event_id)
    assert (data["schema_version"], data["payload_fingerprint"]) == ("1.0", "0" * 64)
    assert (data["payload_shape"], data["quantity_kind"]) == (
        row.payload_shape.value,
        row.quantity_kind.value,
    )
    assert data["created_at"] == "2026-09-29T08:30:00"


def test_a_foreign_currency_event_has_its_fx_version_and_rate(client, admin, world) -> None:
    published = world.publish("USD")
    tenant = world.tenant()
    event_id = world.event(tenant)
    world.bill()

    data = detail(client, admin, world, event_id)

    assert data["fx_rate_version_id"] == published["fx"]
    assert data["fx_rate_applied"] == FX_RATE
    assert data["provider_source_currency"] == "USD"
    # 2 s × 0.03125 = 0.0625 USD; × 4.5 = 0.28125 MYR; MARKUP 2 = 0.5625.
    assert data["provider_source_cost"] == "0.06250000"
    assert data["estimated_provider_cost_myr"] == "0.28125000"
    assert data["billable_cost"] == "0.56250000"
    assert (data["gross_margin"], data["gross_margin_basis"]) == ("0.28125000", "estimated")
    assert data["wallet_transaction"]["amount"] == "-0.56250000"


def test_a_zero_event_has_no_ledger_row(client, admin, world) -> None:
    world.publish()
    tenant = world.tenant()
    event_id = world.event(tenant, seconds="0")
    world.bill()

    data = detail(client, admin, world, event_id)

    assert data["status"] == "PROCESSED"
    assert (data["billable_cost"], data["estimated_provider_cost_myr"]) == (
        "0.00000000",
        "0.00000000",
    )
    assert (data["gross_margin"], data["gross_margin_basis"]) == ("0.00000000", "estimated")
    assert data["wallet_transaction"] is None


def test_internal_event_displays_reference_price_without_revenue_or_margin(
    client, admin, world
) -> None:
    world.publish()
    created = customers.create_customer(
        world.factory,
        actor=world.admin,
        company_name="Internal example",
        email="internal@usage-query-test.example.com",
        context=CONTEXT,
        now=T0,
    )
    with world.factory() as session:
        tenant = session.execute(select(Tenant).where(Tenant.public_id == created.id)).scalar_one()
        tenant.billing_mode = BillingMode.INTERNAL_METERED_ONLY
        tenant_id = tenant.id
        session.commit()
    tenancy = world.project(created.id, tenant_id)
    event_id = world.event(tenancy)
    world.bill()

    detail_event = detail(client, admin, world, event_id)
    assert detail_event["billing_mode_snapshot"] == "INTERNAL_METERED_ONLY"
    assert detail_event["estimated_provider_cost_myr"] == "0.06250000"
    assert detail_event["reference_customer_price"] == "0.12500000"
    assert detail_event["billable_cost"] == "0.00000000"
    assert detail_event["wallet_transaction"] is None
    assert (detail_event["gross_margin"], detail_event["gross_margin_basis"]) == (None, None)
    item = client.get(LIST, headers=admin).json()["data"]["items"][0]
    assert item["reference_customer_price"] == detail_event["reference_customer_price"]


def test_an_error_event_has_no_snapshot(client, admin, world) -> None:
    tenant = world.tenant()
    event_id = world.event(tenant)
    world.bill()

    data = detail(client, admin, world, event_id)

    assert (data["status"], data["error_code"]) == ("PRICING_ERROR", "NO_PROVIDER_PRICE")
    assert {field: data[field] for field in SNAPSHOT_FIELDS} == dict.fromkeys(SNAPSHOT_FIELDS)
    # The model was resolved: the catalog reference is there for the administrator to see.
    assert (data["provider_ref_id"], data["model_ref_id"]) == (world.provider.id, world.model.id)
    assert data["attempt_count"] == 1


def test_a_processing_event_shows_its_claim(client, admin, world) -> None:
    tenant = world.tenant()
    event_id = world.event(tenant)
    [claim] = usage_billing.claim_events(world.factory, lease_seconds=120, clock=lambda: NOW)

    data = detail(client, admin, world, event_id)

    row = world.row(event_id)
    assert data["status"] == "PROCESSING"
    assert data["claim_token"] == row.claim_token
    assert data["claimed_at"] == "2026-09-29T10:30:00"
    assert data["lease_expires_at"] == "2026-09-29T10:32:00"
    assert data["attempt_count"] == 1
    assert claim.id == event_id


def test_the_conflicts_of_the_event_id_are_listed(client, admin, world) -> None:
    owner, other = world.tenant(), world.tenant()
    event_id = world.event(owner)
    unrelated = world.event(owner)
    row = world.row(event_id)
    with world.factory() as session:
        for number, (tenancy, mismatch, offset) in enumerate(
            [(other, ConflictMismatch.OWNERSHIP, 0), (owner, ConflictMismatch.FINGERPRINT, 5)],
            start=1,
        ):
            event_repository.insert_conflict(
                session,
                usage_event_id=event_id,
                event_id=row.event_id,
                integration_credential_id=tenancy.credential_id,
                api_key=tenancy.api_key,
                payload_fingerprint=str(number) * 64,
                mismatch=mismatch,
                now=T0 + dt.timedelta(seconds=offset),
            )
        session.commit()

    data = detail(client, admin, world, event_id)

    assert data["conflicts"] == [
        {
            "api_key": other.api_key,
            "mismatch": "OWNERSHIP",
            "received_at": "2026-09-29T08:30:00",
        },
        {
            "api_key": owner.api_key,
            "mismatch": "FINGERPRINT",
            "received_at": "2026-09-29T08:30:05",
        },
    ]
    assert detail(client, admin, world, unrelated)["conflicts"] == []


def test_an_unknown_event_is_404(client, admin, world) -> None:
    response = client.get(DETAIL.format(usage_event_id=ZERO_ID), headers=admin)

    assert (response.status_code, error_code(response)) == (404, "USAGE_EVENT_NOT_FOUND")


def test_no_internal_id_or_secret_appears(client, admin, world) -> None:
    world.publish()
    tenant = world.tenant()
    event_id = world.event(tenant)
    world.bill()
    other = world.tenant()
    row = world.row(event_id)
    with world.factory() as session:
        event_repository.insert_conflict(
            session,
            usage_event_id=event_id,
            event_id=row.event_id,
            integration_credential_id=other.credential_id,
            api_key=other.api_key,
            payload_fingerprint="1" * 64,
            mismatch=ConflictMismatch.OWNERSHIP,
            now=T0,
        )
        session.commit()

    texts = [
        client.get(LIST, headers=admin).text,
        client.get(DETAIL.format(usage_event_id=row.public_id), headers=admin).text,
    ]

    for text in texts:
        assert CIPHERTEXT not in text
        for name in (
            "tenant_id",
            "integration_credential_id",
            "usage_meter_type_id",
            "wallet_transaction_id",
            "usage_event_id",
            "encrypted_secret",
            "secret",
        ):
            assert f'"{name}"' not in text


# --- authorization -----------------------------------------------------------------------


@pytest.mark.parametrize("caller", ["anonymous", "customer"])
@pytest.mark.parametrize("path", ["list", "detail"])
def test_only_an_admin_may_query(client, world, caller: str, path: str) -> None:
    tenant = world.tenant()
    event_id = world.event(tenant)
    url = LIST if path == "list" else DETAIL.format(usage_event_id=world.public_id(event_id))
    headers = {}
    if caller == "customer":
        headers = world.headers(world.user(UserRole.CUSTOMER), "CUSTOMER")

    response = client.get(url, params={"customer_id": tenant.public_id}, headers=headers)

    expected = (401, "TOKEN_INVALID") if caller == "anonymous" else (403, "ADMIN_REQUIRED")
    assert (response.status_code, error_code(response)) == expected
    assert response.json()["data"] is None

"""`POST /api/v1/admin/pricing-preview` end to end on SQLite (design gate #179 v1 §7).

The HTTP contract: a priced MARKUP and FIXED_RATE preview with every component and version;
each error state is a 200 with the part that was resolved; a future `occurred_at` hits the
reserved version; an MYR source shows no FX version; aliases and tenant isolation; invalid
requests (422) and unknown customers or meter types (404); an out-of-range result; nothing is
written (row counts of every table, no audit); the preview equals `resolve_charge_inputs` +
`price_event` with `locking=True`; and "the first statement is `require_admin`" (AST).

Anonymous 401 / CUSTOMER 403 without writes is in test_admin_customers_api.py (it enumerates
every admin route). Prices, rates and multipliers are fictional; the placeholder uuid is all
zeros.
"""

from __future__ import annotations

import ast
import base64
import datetime as dt
import os
import pathlib
import uuid
from collections.abc import Iterator, Mapping
from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.config import Settings
from app.core.database import create_session_factory
from app.core.tokens import issue_access_token
from app.main import create_app
from app.models.ai_catalog import SEED_METER_TYPES, UsageMeterComponent, UsageMeterType
from app.models.auth import User, UserRole, UserStatus
from app.models.base import Base
from app.models.pricing_rules import PricingScope, PricingStrategy
from app.models.tenancy import Tenant
from app.schemas.pricing_rules import RuleComponentInput
from app.schemas.provider_prices import ComponentInput
from app.services import ai_catalog, fx_rates, pricing_rules, provider_prices
from app.services.auth import RequestContext, utc_now
from app.services.pricing_engine import price_resolved, resolve_charge_inputs

PREVIEW = "/api/v1/admin/pricing-preview"
ROUTER_FILE = pathlib.Path("app/api/admin_pricing_preview.py")
CONTEXT = RequestContext(ip_address="203.0.113.9", user_agent="pricing-preview-test")
TEST_EMAIL_DOMAIN = "@pricing-preview-test.example.com"
# All-zero placeholder (secret scan).
ZERO_ID = "00000000-0000-4000-8000-000000000000"

T0 = dt.datetime(2026, 9, 29, 8, 30, 0)
EVENT_AT = "2026-09-29T09:30:00Z"

# Response field whitelist (design §2). "Exactly equal": one more key (an internal id) is red.
PREVIEW_FIELDS = {
    "status",
    "error_code",
    "model",
    "provider_price_version",
    "fx_rate_version",
    "pricing_rule",
    "components",
    "provider_source_cost_unrounded",
    "provider_source_cost",
    "estimated_provider_cost_myr_unrounded",
    "estimated_provider_cost_myr",
    "billable_cost_unrounded",
    "billable_cost",
    "tax_inclusive",
}
AMOUNT_FIELDS = (
    "provider_source_cost_unrounded",
    "provider_source_cost",
    "estimated_provider_cost_myr_unrounded",
    "estimated_provider_cost_myr",
    "billable_cost_unrounded",
    "billable_cost",
)
ENVELOPE_FIELDS = {"success", "data", "error", "request_id"}

TOKENS = {
    "input_tokens": 1000,
    "output_tokens": 2000,
    "cache_creation_input_tokens": 400,
    "cache_read_input_tokens": 3000,
}
# Fictional USD provider prices and MYR tax-inclusive customer prices, per 1 000 000 tokens.
USD_RATES = {
    "LLM_INPUT_TOKEN": "1.5",
    "LLM_OUTPUT_TOKEN": "7.5",
    "LLM_CACHE_WRITE_TOKEN": "1.875",
    "LLM_CACHE_READ_TOKEN": "0.15",
}
FIXED_MYR_RATES = {
    "LLM_INPUT_TOKEN": "2",
    "LLM_OUTPUT_TOKEN": "10",
    "LLM_CACHE_WRITE_TOKEN": "2.5",
    "LLM_CACHE_READ_TOKEN": "0.2",
}
FX_RATE = "4.4444444444"
PER_MILLION = "1000000"


# --- fixtures and helpers ------------------------------------------------------------


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
        for seed in SEED_METER_TYPES:
            kind = UsageMeterType(
                public_id=str(uuid.uuid4()),
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
    application.state.engine = engine
    application.state.session_factory = create_session_factory(engine)
    seed_meter_types(application.state.session_factory)
    yield application
    engine.dispose()


@pytest.fixture
def client(app) -> TestClient:
    return TestClient(app)


class World:
    """One admin, provider `anthropic` with model `claude-x`, two tenants; services only."""

    def __init__(self, factory: sessionmaker[Session]) -> None:
        self.factory = factory
        self.clock = Clock(T0)
        now = utc_now()
        with factory() as session:
            user = User(
                email=f"{uuid.uuid4().hex}{TEST_EMAIL_DOMAIN}",
                password_hash="not-a-real-hash",
                role=UserRole.ADMIN,
                status=UserStatus.ACTIVE,
                created_at=now,
                updated_at=now,
            )
            session.add(user)
            session.commit()
            self.admin = user
        self.provider = ai_catalog.create_provider(
            factory,
            actor=self.admin,
            code="anthropic",
            display_name="Fictional provider",
            context=CONTEXT,
            clock=self.clock,
        )
        self.model = ai_catalog.create_model(
            factory,
            actor=self.admin,
            provider_id=self.provider.id,
            code="claude-x",
            display_name="Fictional model",
            context=CONTEXT,
            clock=self.clock,
        )
        self.tenants = {name: self._tenant(name) for name in ("tenant-a", "tenant-b")}

    def _tenant(self, name: str) -> str:
        public_id = str(uuid.uuid4())
        with self.factory() as session:
            session.add(
                Tenant(
                    public_id=public_id,
                    company_name=f"Fictional {name} Sdn Bhd",
                    email=f"{name}{TEST_EMAIL_DOMAIN}",
                    created_at=T0,
                    updated_at=T0,
                )
            )
            session.commit()
        return public_id

    def price(
        self,
        rates: Mapping[str, str] = USD_RATES,
        *,
        currency: str = "USD",
        unit: str = PER_MILLION,
        effective_from: dt.datetime | None = None,
    ) -> str:
        components = [
            ComponentInput.model_validate(
                {"component_code": code, "unit_quantity": unit, "rate_amount": rate}
            )
            for code, rate in rates.items()
        ]
        draft = provider_prices.create_draft(
            self.factory,
            actor=self.admin,
            provider_id=self.provider.id,
            model_id=self.model.id,
            source_currency=currency,
            source_reference="Fictional price page",
            components=components,
            context=CONTEXT,
            clock=self.clock,
        )
        published = provider_prices.publish(
            self.factory,
            actor=self.admin,
            price_version_id=draft.id,
            effective_from=effective_from,
            context=CONTEXT,
            clock=self.clock,
        )
        return published.id

    def fx(self, currency: str = "USD", rate: str = FX_RATE) -> str:
        draft = fx_rates.create_draft(
            self.factory,
            actor=self.admin,
            base_currency=currency,
            rate=Decimal(rate),
            observed_at=T0,
            source_reference="Fictional quote",
            context=CONTEXT,
            clock=self.clock,
        )
        published = fx_rates.publish(
            self.factory,
            actor=self.admin,
            fx_rate_id=draft.id,
            effective_from=None,
            context=CONTEXT,
            clock=self.clock,
        )
        return published.id

    def rule(
        self,
        scope: PricingScope = PricingScope.GLOBAL,
        *,
        tenant: str = "tenant-a",
        multiplier: str = "2",
        rates: Mapping[str, str] | None = None,
        unit: str = PER_MILLION,
    ) -> str:
        """MARKUP with `multiplier` unless `rates` is given (then FIXED_RATE)."""
        components = None
        if rates is not None:
            components = [
                RuleComponentInput.model_validate(
                    {"component_code": code, "unit_quantity": unit, "rate_amount": rate}
                )
                for code, rate in rates.items()
            ]
        draft = pricing_rules.create_draft(
            self.factory,
            actor=self.admin,
            priority_scope=scope,
            customer_id=self.tenants[tenant] if scope.has_customer else None,
            provider_id=self.provider.id if scope.has_provider else None,
            model_id=self.model.id if scope.has_model else None,
            strategy=PricingStrategy.MARKUP if rates is None else PricingStrategy.FIXED_RATE,
            markup_multiplier=Decimal(multiplier) if rates is None else None,
            components=components,
            context=CONTEXT,
            clock=self.clock,
        )
        published = pricing_rules.publish(
            self.factory,
            actor=self.admin,
            rule_id=draft.id,
            effective_from=None,
            context=CONTEXT,
            clock=self.clock,
        )
        return published.id

    def alias(self, alias: str) -> None:
        ai_catalog.map_alias(
            self.factory,
            actor=self.admin,
            provider_id=self.provider.id,
            alias=alias,
            model_id=self.model.id,
            context=CONTEXT,
            clock=self.clock,
        )


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


def request_body(
    world: World, *, tenant: str = "tenant-a", drop: tuple[str, ...] = (), **fields: object
) -> dict[str, object]:
    """The LLM_TOKEN example at EVENT_AT; `fields` replace, `drop` removes."""
    body: dict[str, object] = {
        "customer_id": world.tenants[tenant],
        "provider": "anthropic",
        "model": "claude-x",
        "usage_type": "LLM_TOKEN",
        **TOKENS,
        "occurred_at": EVENT_AT,
    }
    body.update(fields)
    for name in drop:
        body.pop(name)
    return body


def preview(client: TestClient, admin: dict[str, str], body: dict[str, object]) -> dict:
    response = client.post(PREVIEW, json=body, headers=admin)
    assert response.status_code == 200, response.text
    envelope = response.json()
    assert set(envelope) == ENVELOPE_FIELDS
    assert envelope["success"] is True
    data = envelope["data"]
    assert set(data) == PREVIEW_FIELDS
    assert data["tax_inclusive"] is True
    return data


def error_code(response) -> str:
    body = response.json()
    assert set(body) == ENVELOPE_FIELDS
    assert body["success"] is False
    assert body["data"] is None
    return body["error"]["code"]


def unpriced(data: dict) -> None:
    """An error state: no components and no amounts."""
    assert data["components"] == []
    for name in AMOUNT_FIELDS:
        assert data[name] is None, name


def table_counts(application: FastAPI) -> dict[str, int]:
    with application.state.session_factory() as session:
        return {
            table.name: session.execute(select(func.count()).select_from(table)).scalar_one()
            for table in Base.metadata.sorted_tables
        }


# --- priced -------------------------------------------------------------------------


def test_a_markup_preview_lists_every_component_and_version(client, admin, world) -> None:
    price_id = world.price()
    fx_id = world.fx()
    rule_id = world.rule()

    data = preview(client, admin, request_body(world))

    assert (data["status"], data["error_code"]) == ("PRICED", None)
    assert data["model"] == {"provider": "anthropic", "model": "claude-x", "matched_via": "code"}
    assert data["provider_price_version"] == {
        "id": price_id,
        "source_currency": "USD",
        "effective_from": None,
        "effective_to": None,
    }
    assert data["fx_rate_version"] == {
        "id": fx_id,
        "rate": FX_RATE,
        "observed_at": "2026-09-29T08:30:00",
    }
    assert data["pricing_rule"] == {
        "id": rule_id,
        "priority_scope": "GLOBAL",
        "strategy": "MARKUP",
        "markup_multiplier": "2.00000000",
    }
    components = {item["component_code"]: item for item in data["components"]}
    assert [item["component_code"] for item in data["components"]] == sorted(components)
    assert {code: item["quantity"] for code, item in components.items()} == {
        "LLM_CACHE_READ_TOKEN": "3000",
        "LLM_CACHE_WRITE_TOKEN": "400",
        "LLM_INPUT_TOKEN": "1000",
        "LLM_OUTPUT_TOKEN": "2000",
    }
    provider_costs = {
        code: Decimal(item["provider_cost_unrounded"]) for code, item in components.items()
    }
    assert provider_costs == {
        "LLM_CACHE_READ_TOKEN": Decimal("0.00045"),
        "LLM_CACHE_WRITE_TOKEN": Decimal("0.00075"),
        "LLM_INPUT_TOKEN": Decimal("0.0015"),
        "LLM_OUTPUT_TOKEN": Decimal("0.015"),
    }
    assert all(item["customer_price_unrounded"] is None for item in components.values())
    # Unrounded values are decimal strings; stored ones have exactly 8 places (design §2).
    assert Decimal(data["provider_source_cost_unrounded"]) == Decimal("0.0177")
    assert Decimal(data["estimated_provider_cost_myr_unrounded"]) == Decimal("0.07866666666588")
    assert Decimal(data["billable_cost_unrounded"]) == Decimal("0.15733333333176")
    assert data["provider_source_cost"] == "0.01770000"
    assert data["estimated_provider_cost_myr"] == "0.07866667"
    assert data["billable_cost"] == "0.15733333"


def test_a_customer_fixed_rate_preview(client, admin, world) -> None:
    world.price()
    world.fx()
    world.rule()
    rule_id = world.rule(PricingScope.CUSTOMER, rates=FIXED_MYR_RATES)

    data = preview(client, admin, request_body(world))

    assert data["status"] == "PRICED"
    assert data["pricing_rule"] == {
        "id": rule_id,
        "priority_scope": "CUSTOMER",
        "strategy": "FIXED_RATE",
        "markup_multiplier": None,
    }
    customer = {
        item["component_code"]: Decimal(item["customer_price_unrounded"])
        for item in data["components"]
    }
    assert customer == {
        "LLM_CACHE_READ_TOKEN": Decimal("0.0006"),
        "LLM_CACHE_WRITE_TOKEN": Decimal("0.001"),
        "LLM_INPUT_TOKEN": Decimal("0.002"),
        "LLM_OUTPUT_TOKEN": Decimal("0.02"),
    }
    assert data["billable_cost"] == "0.02360000"
    # §14's cost snapshot is there all the same.
    assert data["estimated_provider_cost_myr"] == "0.07866667"


def test_an_myr_source_has_no_fx_version(client, admin, world) -> None:
    world.price(currency="MYR")
    world.rule()

    data = preview(client, admin, request_body(world))

    assert data["status"] == "PRICED"
    assert data["fx_rate_version"] is None
    assert data["estimated_provider_cost_myr"] == data["provider_source_cost"] == "0.01770000"
    assert data["billable_cost"] == "0.03540000"


def test_a_quantity_meter_type(client, admin, world) -> None:
    """AUDIO_SECOND: a decimal `quantity` and its unit."""
    world.price({"AUDIO_SECOND": "0.006"}, unit="1")
    world.fx()
    world.rule()
    body = request_body(
        world,
        drop=tuple(TOKENS),
        usage_type="AUDIO_SECOND",
        quantity="12.5",
        unit="SECOND",
    )

    data = preview(client, admin, body)

    assert data["status"] == "PRICED"
    pairs = [(item["component_code"], item["quantity"]) for item in data["components"]]
    assert pairs == [("AUDIO_SECOND", "12.5")]
    assert data["provider_source_cost"] == "0.07500000"
    assert data["estimated_provider_cost_myr"] == "0.33333333"
    assert data["billable_cost"] == "0.66666667"


def test_a_future_occurred_at_hits_the_reserved_version(client, admin, world) -> None:
    """§59: the explicit `occurred_at` may be in the future; a reservation is previewable."""
    current = world.price()
    reserved_from = T0 + dt.timedelta(days=2)
    reserved = world.price(dict.fromkeys(USD_RATES, "3"), effective_from=reserved_from)
    world.fx()
    world.rule()

    now = preview(client, admin, request_body(world))
    later = preview(client, admin, request_body(world, occurred_at="2026-10-02T00:00:00+08:00"))

    assert now["provider_price_version"]["id"] == current
    assert now["provider_price_version"]["effective_to"] == "2026-10-01T08:30:00"
    assert later["provider_price_version"] == {
        "id": reserved,
        "source_currency": "USD",
        "effective_from": "2026-10-01T08:30:00",
        "effective_to": None,
    }
    assert later["billable_cost"] != now["billable_cost"]


def test_an_alias_gives_the_same_result(client, admin, world) -> None:
    world.price()
    world.fx()
    world.rule()
    world.alias("claude-latest")

    by_alias = preview(client, admin, request_body(world, model="claude-latest"))
    by_code = preview(client, admin, request_body(world))

    assert by_alias["model"] == {
        "provider": "anthropic",
        "model": "claude-x",
        "matched_via": "alias",
    }
    assert {key: value for key, value in by_alias.items() if key != "model"} == {
        key: value for key, value in by_code.items() if key != "model"
    }


def test_one_customers_rule_never_prices_another(client, admin, world) -> None:
    """INV-8: tenant A has a customer-level rule; tenant B gets the global default."""
    world.price()
    world.fx()
    global_rule = world.rule(multiplier="2")
    rule_a = world.rule(PricingScope.CUSTOMER, tenant="tenant-a", multiplier="3")

    a = preview(client, admin, request_body(world, tenant="tenant-a"))
    b = preview(client, admin, request_body(world, tenant="tenant-b"))

    assert (a["pricing_rule"]["id"], b["pricing_rule"]["id"]) == (rule_a, global_rule)
    assert (a["billable_cost"], b["billable_cost"]) == ("0.23600000", "0.15733333")


# --- error states: 200 with what was resolved ------------------------------------------


def test_model_unknown(client, admin, world) -> None:
    world.price()
    world.fx()
    world.rule()

    data = preview(client, admin, request_body(world, model="no-such-model"))

    assert (data["status"], data["error_code"]) == ("MODEL_UNKNOWN", None)
    for name in ("model", "provider_price_version", "fx_rate_version", "pricing_rule"):
        assert data[name] is None, name
    unpriced(data)


def test_no_provider_price(client, admin, world) -> None:
    world.rule()

    data = preview(client, admin, request_body(world))

    assert (data["status"], data["error_code"]) == ("PRICING_ERROR", "NO_PROVIDER_PRICE")
    assert data["model"]["model"] == "claude-x"
    assert data["provider_price_version"] is None
    unpriced(data)


def test_a_price_without_the_meter_type(client, admin, world) -> None:
    price_id = world.price({"AUDIO_SECOND": "0.006"}, unit="1")
    world.fx()
    world.rule()

    data = preview(client, admin, request_body(world))

    assert (data["status"], data["error_code"]) == ("PRICING_ERROR", "MISSING_PROVIDER_COMPONENT")
    assert data["provider_price_version"]["id"] == price_id
    assert data["fx_rate_version"] is None and data["pricing_rule"] is None
    unpriced(data)


def test_fx_rate_error_shows_the_model_and_the_price(client, admin, world) -> None:
    price_id = world.price()
    world.rule()

    data = preview(client, admin, request_body(world))

    assert (data["status"], data["error_code"]) == ("FX_RATE_ERROR", None)
    assert data["model"] is not None
    assert data["provider_price_version"]["id"] == price_id
    assert data["fx_rate_version"] is None and data["pricing_rule"] is None
    unpriced(data)


def test_no_pricing_rule(client, admin, world) -> None:
    world.price()
    fx_id = world.fx()

    data = preview(client, admin, request_body(world))

    assert (data["status"], data["error_code"]) == ("PRICING_ERROR", "NO_PRICING_RULE")
    assert data["fx_rate_version"]["id"] == fx_id
    assert data["pricing_rule"] is None
    unpriced(data)


def test_a_fixed_rate_rule_without_the_meter_type_does_not_fall_through(
    client, admin, world
) -> None:
    world.price()
    world.fx()
    world.rule()
    rule_id = world.rule(PricingScope.CUSTOMER, rates={"AUDIO_SECOND": "0.05"}, unit="1")

    data = preview(client, admin, request_body(world))

    assert (data["status"], data["error_code"]) == ("PRICING_ERROR", "MISSING_RULE_COMPONENT")
    assert data["pricing_rule"]["id"] == rule_id
    unpriced(data)


# --- invalid requests -------------------------------------------------------------------


def test_invalid_bodies_are_422(client, admin, world) -> None:
    world.price()
    world.fx()
    world.rule()
    audio = {"drop": tuple(TOKENS), "usage_type": "AUDIO_SECOND", "unit": "SECOND"}
    image = {"drop": tuple(TOKENS), "usage_type": "IMAGE_GENERATION", "unit": "IMAGE"}
    cases: dict[str, dict[str, object]] = {
        "no occurred_at": {"drop": ("occurred_at",)},
        "occurred_at without a zone": {"occurred_at": "2026-09-29T09:30:00"},
        "occurred_at not on the calendar": {"occurred_at": "2026-02-30T09:30:00Z"},
        "an unknown field": {"billable_cost": "1"},
        "a token as a string": {"input_tokens": "1000"},
        "a token as a boolean": {"input_tokens": True},
        "a negative token": {"input_tokens": -1},
        "a missing token": {"drop": ("output_tokens",)},
        "quantity on a token type": {"quantity": "1", "unit": "TOKEN"},
        # `drop` runs after the overrides: keep `input_tokens` by dropping only the other three.
        "tokens on a quantity type": {**audio, "drop": tuple(TOKENS)[1:], "quantity": "1"},
        "a quantity as a number": {**audio, "quantity": 12.5},
        "a missing quantity": {**audio},
        "the wrong unit": {**audio, "quantity": "1", "unit": "MINUTE"},
        "a fraction of an integer type": {**image, "quantity": "3.5"},
        "a bad provider string": {"provider": "Anthropic Inc"},
    }
    for name, case in cases.items():
        fields = dict(case)
        drop = fields.pop("drop", ())
        body = request_body(world, drop=drop, **fields)  # type: ignore[arg-type]

        response = client.post(PREVIEW, json=body, headers=admin)

        assert (response.status_code, error_code(response)) == (422, "VALIDATION_ERROR"), name


def test_an_unknown_customer_or_meter_type_is_404(client, admin, world) -> None:
    unknown_customer = request_body(world, customer_id=ZERO_ID)
    response = client.post(PREVIEW, json=unknown_customer, headers=admin)
    assert (response.status_code, error_code(response)) == (404, "CUSTOMER_NOT_FOUND")

    unknown_type = request_body(world, usage_type="NO_SUCH_TYPE")
    response = client.post(PREVIEW, json=unknown_type, headers=admin)
    assert (response.status_code, error_code(response)) == (404, "USAGE_METER_TYPE_NOT_FOUND")


def test_an_out_of_range_result_is_422(client, admin, world) -> None:
    """ChargeOverflow: billing would mark the event FAILED_FINAL / AMOUNT_OUT_OF_RANGE."""
    world.price(dict.fromkeys(USD_RATES, "999999999999"), unit="0.00000001")
    world.fx()
    world.rule()
    body = request_body(world, **dict.fromkeys(TOKENS, 10**12))

    response = client.post(PREVIEW, json=body, headers=admin)

    assert (response.status_code, error_code(response)) == (422, "AMOUNT_OUT_OF_RANGE")


# --- read-only, same code, authorisation ------------------------------------------------


def test_the_preview_writes_nothing(client, admin, app, world) -> None:
    world.price()
    world.fx()
    world.rule()
    before = table_counts(app)

    audio = request_body(
        world, drop=tuple(TOKENS), usage_type="AUDIO_SECOND", quantity="1", unit="SECOND"
    )

    assert preview(client, admin, request_body(world))["status"] == "PRICED"
    assert preview(client, admin, request_body(world, model="no-such-model"))["model"] is None
    assert preview(client, admin, audio)["error_code"] == "MISSING_PROVIDER_COMPONENT"

    # Every table, the audit log included: nothing written.
    assert table_counts(app) == before


def test_the_preview_is_the_billing_code_path(client, admin, app, world) -> None:
    """Design §2: the same resolution and calculation, only `locking` differs."""
    world.price()
    world.fx()
    world.rule()
    data = preview(client, admin, request_body(world))

    factory = app.state.session_factory
    tenant = select(Tenant.id).where(Tenant.public_id == world.tenants["tenant-a"])
    meter_type = select(UsageMeterType.id).where(UsageMeterType.code == "LLM_TOKEN")
    with factory() as session:
        tenant_id = session.execute(tenant).scalar_one()
        meter_type_id = session.execute(meter_type).scalar_one()
        resolution = resolve_charge_inputs(
            session,
            tenant_id=tenant_id,
            provider_code="anthropic",
            model_code="claude-x",
            meter_type_id=meter_type_id,
            occurred_at=T0 + dt.timedelta(hours=1),
            locking=True,
        )
        session.commit()
    billed = price_resolved(resolution, TOKENS)

    for name in AMOUNT_FIELDS:
        assert data[name] == format(getattr(billed, name), "f"), name
    assert [item["provider_cost_unrounded"] for item in data["components"]] == [
        format(line.provider_cost_unrounded, "f") for line in billed.components
    ]


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


def test_the_handler_starts_with_require_admin() -> None:
    """Design §2「试算接口」: the first statement (after the docstring) is `require_admin`."""
    handlers = _handlers()
    # One endpoint; none means the AST scan found nothing.
    assert len(handlers) == 1

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


def test_non_admins_are_refused(client, app, world) -> None:
    """INV-7: costs and multipliers stay on the admin side."""
    world.price()
    world.fx()
    world.rule()
    body = request_body(world)
    with app.state.session_factory() as session:
        customer = User(
            email=f"{uuid.uuid4().hex}{TEST_EMAIL_DOMAIN}",
            password_hash="not-a-real-hash",
            role=UserRole.CUSTOMER,
            status=UserStatus.ACTIVE,
            created_at=utc_now(),
            updated_at=utc_now(),
        )
        session.add(customer)
        session.commit()
        customer_id = customer.id
    token = issue_access_token(
        app.state.settings,
        user_id=customer_id,
        role="CUSTOMER",
        session_id=uuid.uuid4().hex,
        now=utc_now(),
    )

    anonymous = client.post(PREVIEW, json=body)
    as_customer = client.post(PREVIEW, json=body, headers={"Authorization": f"Bearer {token}"})

    assert (anonymous.status_code, error_code(anonymous)) == (401, "TOKEN_INVALID")
    assert (as_customer.status_code, error_code(as_customer)) == (403, "ADMIN_REQUIRED")

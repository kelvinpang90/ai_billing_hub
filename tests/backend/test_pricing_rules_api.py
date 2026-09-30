"""Pricing rule endpoints end to end on SQLite (design gate #178 v3 §7).

The HTTP contract: the normal path on all five levels with MARKUP and FIXED_RATE and the audit of
every step; scope fields that must match `priority_scope`; mixed strategies; precision (strings
only, at most 8 decimals, positive); 404s; completeness and retired catalog items; the effective
time's format and no backdating; the 409s of the state machine; closed request bodies; listing
filters and order; no internal ids in responses; "tax-inclusive" in the field descriptions; and
"the first statement of every handler is `require_admin`" (AST).

Period semantics under a frozen clock, triggers and half-way failures are in
test_pricing_rules_service.py; resolution, the property test and concurrency in
test_pricing_rules_resolve.py. Anonymous 401 / CUSTOMER 403 without writes is in
test_admin_customers_api.py (it enumerates every admin route).

Prices and multipliers are fictional; the placeholder uuid is all zeros.
"""

from __future__ import annotations

import ast
import base64
import json
import os
import pathlib
import uuid
from collections.abc import Iterator

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
from app.models.ai_catalog import (
    SEED_METER_TYPES,
    AiModel,
    CatalogStatus,
    UsageMeterComponent,
    UsageMeterType,
)
from app.models.auth import AuditAction, AuditLog, User, UserRole, UserStatus
from app.models.base import Base
from app.models.pricing_rules import PricingRule, PricingRuleComponent
from app.services.auth import utc_now

RULES = "/api/v1/admin/pricing-rules"
CUSTOMERS = "/api/v1/admin/customers"
PROVIDERS = "/api/v1/admin/ai-providers"
ROUTER_FILE = pathlib.Path("app/api/admin_pricing_rules.py")

# Response field whitelist (design §2). "Exactly equal": one more key (an internal id) is red.
RULE_FIELDS = {
    "id",
    "priority_scope",
    "customer_id",
    "provider_id",
    "provider_code",
    "model_id",
    "model_code",
    "strategy",
    "markup_multiplier",
    "status",
    "effective_from",
    "effective_to",
    "components",
    "created_by_email",
    "approved_by_email",
    "created_at",
    "updated_at",
    "approved_at",
}
COMPONENT_FIELDS = {
    "component_code",
    "meter_type_code",
    "unit",
    "unit_quantity",
    "rate_amount",
    "currency",
    "created_at",
}
PAGE_FIELDS = {"items", "page", "page_size", "total"}
ENVELOPE_FIELDS = {"success", "data", "error", "request_id"}

# All-zero placeholder (secret scan).
ZERO_ID = "00000000-0000-4000-8000-000000000000"

# Fictional MYR tax-inclusive prices: per 1 000 000 tokens.
FICTIONAL_TOKEN_RATES = {
    "LLM_INPUT_TOKEN": "1.11111111",
    "LLM_OUTPUT_TOKEN": "2.22222222",
    "LLM_CACHE_WRITE_TOKEN": "3.33333333",
    "LLM_CACHE_READ_TOKEN": "4.44444444",
}
FICTIONAL_MULTIPLIER = "1.23456789"

SCOPES = (
    "CUSTOMER_PROVIDER_MODEL",
    "CUSTOMER_PROVIDER",
    "CUSTOMER",
    "GLOBAL_PROVIDER_MODEL",
    "GLOBAL",
)
# Which scope fields each level takes (design §2「接口」).
SCOPE_FIELDS = {
    "CUSTOMER_PROVIDER_MODEL": ("customer_id", "provider_id", "model_id"),
    "CUSTOMER_PROVIDER": ("customer_id", "provider_id"),
    "CUSTOMER": ("customer_id",),
    "GLOBAL_PROVIDER_MODEL": ("provider_id", "model_id"),
    "GLOBAL": (),
}


# --- fixtures and helpers ------------------------------------------------------------


def settings_for(tmp_path) -> Settings:
    key = tmp_path / "jwt.key"
    key.write_text("a-test-signing-key-that-is-long-enough", encoding="utf-8")
    master = tmp_path / "master.key"
    master.write_text(f"1:{base64.b64encode(os.urandom(32)).decode()}\n", encoding="utf-8")
    return Settings(jwt_secret_file=str(key), master_key_file=str(master), database_url="")


def seed_meter_types(factory: sessionmaker[Session]) -> None:
    """What migration 0012 seeds, for the SQLite tables `create_all` builds empty."""
    now = utc_now().replace(microsecond=0)
    with factory() as session:
        for seed in SEED_METER_TYPES:
            kind = UsageMeterType(
                public_id=str(uuid.uuid4()),
                code=seed.code,
                display_name=seed.display_name,
                payload_shape=seed.payload_shape,
                unit=seed.unit,
                quantity_kind=seed.quantity_kind,
                created_at=now,
                updated_at=now,
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
                        created_at=now,
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


@pytest.fixture
def admin_user(app) -> tuple[int, str]:
    """(internal id, email) of a committed ADMIN."""
    now = utc_now()
    email = f"{uuid.uuid4().hex}@example.com"
    with app.state.session_factory() as session:
        user = User(
            email=email,
            password_hash="not-a-real-hash",
            role=UserRole.ADMIN,
            status=UserStatus.ACTIVE,
            created_at=now,
            updated_at=now,
        )
        session.add(user)
        session.commit()
        return int(user.id), email


@pytest.fixture
def admin(app, admin_user) -> dict[str, str]:
    token = issue_access_token(
        app.state.settings,
        user_id=admin_user[0],
        role="ADMIN",
        session_id=uuid.uuid4().hex,
        now=utc_now(),
    )
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def scope_ids(client, admin) -> dict[str, str]:
    """A customer, a provider with a model, and another provider with its model (public ids)."""
    body = {"company_name": "Fictional Sdn Bhd", "email": "ops@example.com"}
    customer = client.post(CUSTOMERS, json=body, headers=admin)
    assert customer.status_code == 201, customer.text
    provider = new_provider(client, admin, "anthropic")
    other = new_provider(client, admin, "openai")
    return {
        "customer_id": customer.json()["data"]["id"],
        "provider_id": provider["id"],
        "model_id": new_model(client, admin, provider["id"], "claude-x")["id"],
        "other_provider": other["id"],
        "other_model": new_model(client, admin, other["id"], "gpt-x")["id"],
    }


def factory_of(application: FastAPI) -> sessionmaker[Session]:
    return application.state.session_factory


def error_code(response) -> str:
    body = response.json()
    assert set(body) == ENVELOPE_FIELDS
    assert body["success"] is False
    assert body["data"] is None
    return body["error"]["code"]


def snapshot(application: FastAPI) -> dict[str, object]:
    """Every rule row a write may change, as committed, plus the audit count."""
    rules = select(
        PricingRule.public_id,
        PricingRule.status,
        PricingRule.strategy,
        PricingRule.markup_multiplier,
        PricingRule.effective_from,
        PricingRule.effective_to,
        PricingRule.updated_at,
    ).order_by(PricingRule.id)
    components = select(
        PricingRuleComponent.pricing_rule_id,
        PricingRuleComponent.usage_meter_component_id,
        PricingRuleComponent.unit_quantity,
        PricingRuleComponent.rate_amount,
    ).order_by(PricingRuleComponent.id)
    with factory_of(application)() as session:
        return {
            "rules": [tuple(row) for row in session.execute(rules)],
            "components": [tuple(row) for row in session.execute(components)],
            "audits": session.execute(select(func.count()).select_from(AuditLog)).scalar_one(),
        }


def audits(application: FastAPI, action: AuditAction) -> list[AuditLog]:
    statement = select(AuditLog).where(AuditLog.action == action).order_by(AuditLog.id)
    with factory_of(application)() as session:
        return list(session.execute(statement).scalars())


def states(audit: AuditLog) -> tuple[object, object]:
    before = json.loads(audit.before_state) if audit.before_state else None
    after = json.loads(audit.after_state) if audit.after_state else None
    return before, after


def new_provider(client: TestClient, headers: dict[str, str], code: str) -> dict:
    response = client.post(PROVIDERS, json={"code": code, "display_name": code}, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()["data"]


def new_model(client: TestClient, headers: dict[str, str], provider_id: str, code: str) -> dict:
    response = client.post(
        f"{PROVIDERS}/{provider_id}/models",
        json={"code": code, "display_name": code},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]


def token_components(**overrides: str) -> list[dict[str, object]]:
    rates = {**FICTIONAL_TOKEN_RATES, **overrides}
    return [
        {"component_code": code, "unit_quantity": "1000000", "rate_amount": rate}
        for code, rate in rates.items()
    ]


def rule_body(
    scope_ids: dict[str, str], scope: str = "GLOBAL", strategy: str = "MARKUP", **fields: object
) -> dict[str, object]:
    body: dict[str, object] = {"priority_scope": scope, "strategy": strategy}
    for name in SCOPE_FIELDS[scope]:
        body[name] = scope_ids[name]
    if strategy == "MARKUP":
        body["markup_multiplier"] = FICTIONAL_MULTIPLIER
    else:
        body["components"] = token_components()
    return {**body, **fields}


def new_rule(client: TestClient, headers: dict[str, str], body: dict[str, object]) -> dict:
    response = client.post(RULES, json=body, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()["data"]


def publish(client: TestClient, headers: dict[str, str], rule_id: str, **body: object):
    return client.post(f"{RULES}/{rule_id}/publish", json=body, headers=headers)


# --- the normal path --------------------------------------------------------------


@pytest.mark.parametrize("strategy", ["MARKUP", "FIXED_RATE"])
@pytest.mark.parametrize("scope", SCOPES)
def test_a_rule_on_every_level_is_drafted_and_published(
    client, app, admin, admin_user, scope_ids, scope: str, strategy: str
) -> None:
    """§7 "normal path": every level, both strategies; status, period and audits right."""
    created = new_rule(client, admin, rule_body(scope_ids, scope, strategy))

    assert set(created) == RULE_FIELDS
    assert (created["priority_scope"], created["strategy"], created["status"]) == (
        scope,
        strategy,
        "DRAFT",
    )
    fields = SCOPE_FIELDS[scope]
    for name in ("customer_id", "provider_id", "model_id"):
        expected = scope_ids[name] if name in fields else None
        assert created[name] == expected, name
    assert created["provider_code"] == ("anthropic" if "provider_id" in fields else None)
    assert created["model_code"] == ("claude-x" if "model_id" in fields else None)
    if strategy == "MARKUP":
        assert (created["markup_multiplier"], created["components"]) == ("1.23456789", [])
    else:
        assert created["markup_multiplier"] is None
        assert [set(item) for item in created["components"]] == [COMPONENT_FIELDS] * 4
        assert {item["currency"] for item in created["components"]} == {"MYR"}
        codes = [item["component_code"] for item in created["components"]]
        assert codes == sorted(FICTIONAL_TOKEN_RATES)
    assert (created["effective_from"], created["effective_to"]) == (None, None)
    assert created["created_by_email"] == admin_user[1]

    response = publish(client, admin, created["id"])

    assert response.status_code == 200, response.text
    published = response.json()["data"]
    assert (published["status"], published["effective_to"]) == ("PUBLISHED", None)
    # Only the first global default is "from all time" (design §2).
    assert (published["effective_from"] is None) == (scope == "GLOBAL")
    assert published["approved_by_email"] == admin_user[1]
    [create_audit] = audits(app, AuditAction.PRICING_CREATE)
    [publish_audit] = audits(app, AuditAction.PRICING_PUBLISH)
    for audit in (create_audit, publish_audit):
        assert (audit.entity_type, audit.entity_id) == ("pricing_rule", created["id"])
    _, created_state = states(create_audit)
    _, published_state = states(publish_audit)
    assert created_state["priority_scope"] == scope
    assert published_state["status"] == "PUBLISHED"
    assert published_state["strategy"] == strategy


def _integers(value: object) -> list[int]:
    """Every JSON integer inside `value`: internal ids would show up as integers."""
    if isinstance(value, bool):
        return []
    if isinstance(value, int):
        return [value]
    if isinstance(value, dict):
        return [found for item in value.values() for found in _integers(item)]
    if isinstance(value, list):
        return [found for item in value for found in _integers(item)]
    return []


def test_responses_and_audits_carry_no_internal_ids(client, app, admin, scope_ids) -> None:
    rule = new_rule(client, admin, rule_body(scope_ids, "CUSTOMER_PROVIDER_MODEL", "FIXED_RATE"))
    publish(client, admin, rule["id"])

    detail = client.get(f"{RULES}/{rule['id']}", headers=admin).json()["data"]

    assert set(detail) == RULE_FIELDS
    assert _integers(detail) == []
    written = audits(app, AuditAction.PRICING_CREATE) + audits(app, AuditAction.PRICING_PUBLISH)
    assert len(written) == 2
    for audit in written:
        for state in states(audit):
            assert _integers(state) == []
            for key in ("tenant_id", "created_by", "approved_by", "open_slot", "scope_key"):
                assert key not in (state or {})


def test_the_field_descriptions_say_tax_inclusive(app) -> None:
    """Design §2「含税」: the OpenAPI descriptions of the price fields say "tax-inclusive"."""
    schemas = app.openapi()["components"]["schemas"]
    described = [
        schemas["RuleComponentInput"]["properties"]["rate_amount"],
        schemas["PricingRuleComponentView"]["properties"]["rate_amount"],
        schemas["CreatePricingRuleRequest"]["properties"]["markup_multiplier"],
        schemas["PricingRuleView"]["properties"]["markup_multiplier"],
    ]
    for field in described:
        assert "tax-inclusive" in field["description"]


# --- request shape ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("scope", "drop", "add"),
    [
        ("GLOBAL", (), ("customer_id",)),
        ("GLOBAL", (), ("provider_id",)),
        ("GLOBAL_PROVIDER_MODEL", ("model_id",), ()),
        ("GLOBAL_PROVIDER_MODEL", (), ("customer_id",)),
        ("CUSTOMER", ("customer_id",), ()),
        ("CUSTOMER", (), ("model_id",)),
        ("CUSTOMER_PROVIDER", (), ("model_id",)),
        ("CUSTOMER_PROVIDER_MODEL", ("provider_id",), ()),
    ],
)
def test_scope_fields_must_match(client, app, admin, scope_ids, scope, drop, add) -> None:
    """Design §2: more or fewer scope fields than the level takes → 422, nothing written."""
    body = rule_body(scope_ids, scope)
    for name in drop:
        del body[name]
    for name in add:
        body[name] = scope_ids[name]
    before = snapshot(app)

    response = client.post(RULES, json=body, headers=admin)

    assert (response.status_code, error_code(response)) == (422, "VALIDATION_ERROR")
    assert snapshot(app) == before


@pytest.mark.parametrize(
    ("strategy", "fields", "removed"),
    [
        ("MARKUP", {"components": token_components()}, ()),
        ("MARKUP", {}, ("markup_multiplier",)),
        ("FIXED_RATE", {"markup_multiplier": FICTIONAL_MULTIPLIER}, ()),
        ("FIXED_RATE", {}, ("components",)),
        ("MARKUP", {"strategy": "PERCENT"}, ()),
        ("MARKUP", {"priority_scope": "GLOBAL_PROVIDER"}, ()),
        ("MARKUP", {"currency": "MYR"}, ()),
        ("MARKUP", {"status": "PUBLISHED"}, ()),
    ],
    ids=[
        "markup-with-components",
        "markup-without-multiplier",
        "fixed-rate-with-multiplier",
        "fixed-rate-without-components",
        "unknown-strategy",
        "unknown-scope",
        "extra-currency",
        "extra-status",
    ],
)
def test_mixed_or_unknown_shapes_are_refused(
    client, app, admin, scope_ids, strategy, fields, removed
) -> None:
    """§7 "mixed strategies" through the API: 422, nothing written."""
    body = {**rule_body(scope_ids, "GLOBAL", strategy), **fields}
    for name in removed:
        del body[name]
    before = snapshot(app)

    response = client.post(RULES, json=body, headers=admin)

    assert (response.status_code, error_code(response)) == (422, "VALIDATION_ERROR")
    assert snapshot(app) == before


@pytest.mark.parametrize(
    ("value", "accepted"),
    [
        ("0.00000001", True),
        ("123456789012.12345678", True),
        ("1.123456789", False),
        ("0", False),
        ("0.00000000", False),
        ("-1", False),
        ("+1", False),
        ("1e3", False),
        (" 1", False),
        ("1234567890123", False),
        (1.5, False),
        (2, False),
    ],
)
@pytest.mark.parametrize("field", ["markup_multiplier", "rate_amount", "unit_quantity"])
def test_amounts_are_exact_positive_strings(client, app, admin, scope_ids, field, value, accepted):
    """§7 "precision": 8 decimals pass, 9 do not; 0, negatives and JSON numbers are 422."""
    if field == "markup_multiplier":
        body = rule_body(scope_ids, "GLOBAL", "MARKUP", markup_multiplier=value)
    else:
        parts = token_components()
        parts[0][field] = value
        body = rule_body(scope_ids, "GLOBAL", "FIXED_RATE", components=parts)
    before = snapshot(app)

    response = client.post(RULES, json=body, headers=admin)

    if accepted:
        assert response.status_code == 201, response.text
        data = response.json()["data"]
        stored = data[field] if field == "markup_multiplier" else data["components"]
        assert stored
    else:
        assert (response.status_code, error_code(response)) == (422, "VALIDATION_ERROR")
        assert snapshot(app) == before


def test_duplicate_components_and_empty_patches_are_refused(client, app, admin, scope_ids):
    twice = token_components() + token_components()[:1]
    rule = new_rule(client, admin, rule_body(scope_ids, "GLOBAL", "FIXED_RATE"))
    url = f"{RULES}/{rule['id']}"
    duplicated = rule_body(scope_ids, "GLOBAL", "FIXED_RATE", components=twice)
    before = snapshot(app)

    responses = [
        client.post(RULES, json=duplicated, headers=admin),
        client.patch(url, json={}, headers=admin),
        client.patch(url, json={"strategy": None}, headers=admin),
        client.patch(url, json={"components": twice}, headers=admin),
        client.patch(url, json={"customer_id": scope_ids["customer_id"]}, headers=admin),
        client.patch(url, json={"priority_scope": "CUSTOMER"}, headers=admin),
        client.patch(url, json={"markup_multiplier": FICTIONAL_MULTIPLIER}, headers=admin),
    ]

    for response in responses:
        assert (response.status_code, error_code(response)) == (422, "VALIDATION_ERROR")
    assert snapshot(app) == before


# --- 404s and the catalog -----------------------------------------------------------


def test_unknown_references_are_404(client, app, admin, scope_ids) -> None:
    cases = [
        ("CUSTOMER", {"customer_id": ZERO_ID}, "CUSTOMER_NOT_FOUND"),
        ("GLOBAL_PROVIDER_MODEL", {"provider_id": ZERO_ID}, "AI_PROVIDER_NOT_FOUND"),
        ("GLOBAL_PROVIDER_MODEL", {"model_id": ZERO_ID}, "AI_MODEL_NOT_FOUND"),
        # The model exists but belongs to another provider: the same 404.
        ("GLOBAL_PROVIDER_MODEL", {"model_id": scope_ids["other_model"]}, "AI_MODEL_NOT_FOUND"),
    ]
    before = snapshot(app)
    for scope, fields, code in cases:
        response = client.post(RULES, json=rule_body(scope_ids, scope, **fields), headers=admin)
        assert (response.status_code, error_code(response)) == (404, code), fields
    unknown = token_components() + [
        {"component_code": "NOT_A_COMPONENT", "unit_quantity": "1", "rate_amount": "1"}
    ]
    body = rule_body(scope_ids, "GLOBAL", "FIXED_RATE", components=unknown)
    response = client.post(RULES, json=body, headers=admin)
    missing_component = (404, "USAGE_METER_COMPONENT_NOT_FOUND")
    assert (response.status_code, error_code(response)) == missing_component
    calls = [
        ("GET", "", None),
        ("PATCH", "", {"strategy": "MARKUP"}),
        ("POST", "/publish", {}),
        ("POST", "/retire", {"reason": "x"}),
        ("POST", "/discard", {}),
    ]
    for method, suffix, payload in calls:
        response = client.request(method, f"{RULES}/{ZERO_ID}{suffix}", json=payload, headers=admin)
        assert (response.status_code, error_code(response)) == (404, "PRICING_RULE_NOT_FOUND")
    assert snapshot(app) == before


def test_publishing_checks_completeness_and_the_catalog(client, app, admin, scope_ids) -> None:
    """§7 "FIXED_RATE completeness" and "retired catalog items" through the API: 409s."""
    empty = new_rule(client, admin, rule_body(scope_ids, "GLOBAL", "FIXED_RATE", components=[]))
    three = token_components()[:3]
    short = new_rule(client, admin, rule_body(scope_ids, "GLOBAL", "FIXED_RATE", components=three))
    on_model = new_rule(client, admin, rule_body(scope_ids, "GLOBAL_PROVIDER_MODEL"))
    with factory_of(app)() as session:
        session.execute(
            update(AiModel)
            .where(AiModel.public_id == scope_ids["model_id"])
            .values(status=CatalogStatus.RETIRED)
        )
        session.commit()
    before = snapshot(app)

    responses = [publish(client, admin, rule["id"]) for rule in (empty, short, on_model)]

    assert [(item.status_code, error_code(item)) for item in responses] == [
        (409, "PRICING_RULE_INCOMPLETE"),
        (409, "PRICING_RULE_INCOMPLETE"),
        (409, "CATALOG_ITEM_RETIRED"),
    ]
    assert "LLM_CACHE_READ_TOKEN" in responses[1].json()["error"]["message"]
    assert snapshot(app) == before


# --- the effective time and the state machine ------------------------------------------


@pytest.mark.parametrize(
    ("effective_from", "status", "code"),
    [
        ("2000-01-01T00:00:00Z", 422, "EFFECTIVE_FROM_IN_PAST"),
        ("2999-01-01T00:00:00.5Z", 422, "VALIDATION_ERROR"),
        ("2999-01-01T00:00:00", 422, "VALIDATION_ERROR"),
        ("tomorrow", 422, "VALIDATION_ERROR"),
    ],
    ids=["past", "fraction", "no-zone", "garbage"],
)
def test_the_effective_time_is_a_future_whole_second_with_a_zone(
    client, app, admin, scope_ids, effective_from, status, code
) -> None:
    rule = new_rule(client, admin, rule_body(scope_ids))
    before = snapshot(app)

    response = publish(client, admin, rule["id"], effective_from=effective_from)

    assert (response.status_code, error_code(response)) == (status, code)
    assert snapshot(app) == before


def test_a_reservation_in_another_zone_is_stored_in_utc(client, admin, scope_ids) -> None:
    rule = new_rule(client, admin, rule_body(scope_ids, "CUSTOMER"))

    response = publish(client, admin, rule["id"], effective_from="2999-01-01T08:00:00+08:00")

    assert response.status_code == 200, response.text
    assert response.json()["data"]["effective_from"] == "2999-01-01T00:00:00"


def test_the_state_machine_through_the_api(client, app, admin, scope_ids) -> None:
    """Republishing is 200 without a write; edit and discard only drafts; retire needs a reason;
    retired and discarded rules are final; a superseded rule is not retirable."""
    first = new_rule(client, admin, rule_body(scope_ids, "CUSTOMER"))
    published = publish(client, admin, first["id"]).json()["data"]
    before = snapshot(app)
    again = publish(client, admin, first["id"], effective_from="2999-01-01T00:00:00Z")
    assert again.status_code == 200
    assert again.json()["data"] == published
    assert snapshot(app) == before

    patch = {"markup_multiplier": "9.99999999"}
    edited = client.patch(f"{RULES}/{first['id']}", json=patch, headers=admin)
    assert (edited.status_code, error_code(edited)) == (409, "PRICING_RULE_NOT_DRAFT")
    discard = client.post(f"{RULES}/{first['id']}/discard", json={}, headers=admin)
    assert (discard.status_code, error_code(discard)) == (409, "PRICING_RULE_NOT_DRAFT")
    no_reason = client.post(f"{RULES}/{first['id']}/retire", json={}, headers=admin)
    assert (no_reason.status_code, error_code(no_reason)) == (422, "VALIDATION_ERROR")

    reserved = new_rule(client, admin, rule_body(scope_ids, "CUSTOMER"))
    publish(client, admin, reserved["id"], effective_from="2999-01-01T00:00:00Z")
    superseded = client.post(f"{RULES}/{first['id']}/retire", json={"reason": "x"}, headers=admin)
    assert (superseded.status_code, error_code(superseded)) == (409, "PRICING_RULE_NOT_RETIRABLE")

    withdrawn = client.post(
        f"{RULES}/{reserved['id']}/retire", json={"reason": "Fictional"}, headers=admin
    )
    assert withdrawn.status_code == 200, withdrawn.text
    assert withdrawn.json()["data"]["status"] == "RETIRED"
    draft = new_rule(client, admin, rule_body(scope_ids, "CUSTOMER"))
    discarded = client.post(f"{RULES}/{draft['id']}/discard", json={}, headers=admin)
    assert discarded.json()["data"]["status"] == "DISCARDED"
    for rule_id in (reserved["id"], draft["id"]):
        responses = [
            publish(client, admin, rule_id),
            client.patch(f"{RULES}/{rule_id}", json=patch, headers=admin),
            client.post(f"{RULES}/{rule_id}/retire", json={"reason": "x"}, headers=admin),
        ]
        refused = {(item.status_code, error_code(item)) for item in responses}
        assert refused == {(409, "PRICING_RULE_FINAL")}
    [retire_audit] = audits(app, AuditAction.PRICING_RETIRE)
    assert retire_audit.reason == "Fictional"
    [discard_audit] = audits(app, AuditAction.PRICING_DISCARD)
    assert states(discard_audit) == ({"status": "DRAFT"}, {"status": "DISCARDED"})


def test_a_draft_edit_is_audited_and_replaces_the_components(client, app, admin, scope_ids):
    rule = new_rule(client, admin, rule_body(scope_ids, "GLOBAL", "FIXED_RATE"))
    audio = [{"component_code": "AUDIO_SECOND", "unit_quantity": "1", "rate_amount": "0.05555555"}]

    response = client.patch(f"{RULES}/{rule['id']}", json={"components": audio}, headers=admin)

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert [item["component_code"] for item in data["components"]] == ["AUDIO_SECOND"]
    [audit] = audits(app, AuditAction.PRICING_UPDATE)
    before, after = states(audit)
    assert len(before["components"]) == 4
    stored = {"component_code": "AUDIO_SECOND", "unit_quantity": "1.00000000"}
    assert after == {"components": [{**stored, "rate_amount": "0.05555555"}]}


# --- listing -----------------------------------------------------------------------


def test_listing_filters_and_orders_by_scope_then_time(client, admin, scope_ids) -> None:
    """§59 "price history": in §16 order, then `effective_from` (NULL first), with filters."""
    made = {}
    for scope in reversed(SCOPES):
        rule = new_rule(client, admin, rule_body(scope_ids, scope))
        made[scope] = publish(client, admin, rule["id"]).json()["data"]
    later = new_rule(client, admin, rule_body(scope_ids, "GLOBAL"))
    reserved = publish(client, admin, later["id"], effective_from="2999-01-01T00:00:00Z")
    assert reserved.status_code == 200

    listing = client.get(RULES, headers=admin).json()["data"]

    assert set(listing) == PAGE_FIELDS
    assert [item["id"] for item in listing["items"]] == [
        *(made[scope]["id"] for scope in SCOPES),
        later["id"],
    ]
    filters = {
        "priority_scope=GLOBAL": [made["GLOBAL"]["id"], later["id"]],
        f"customer_id={scope_ids['customer_id']}": [made[scope]["id"] for scope in SCOPES[:3]],
        f"model_id={scope_ids['model_id']}": [
            made["CUSTOMER_PROVIDER_MODEL"]["id"],
            made["GLOBAL_PROVIDER_MODEL"]["id"],
        ],
        "status=PUBLISHED&priority_scope=GLOBAL": [made["GLOBAL"]["id"], later["id"]],
        f"provider_id={scope_ids['other_provider']}": [],
    }
    for query, expected in filters.items():
        page = client.get(f"{RULES}?{query}", headers=admin).json()["data"]
        assert [item["id"] for item in page["items"]] == expected, query
    for query, code in (
        (f"customer_id={ZERO_ID}", "CUSTOMER_NOT_FOUND"),
        (f"provider_id={ZERO_ID}", "AI_PROVIDER_NOT_FOUND"),
        (f"model_id={ZERO_ID}", "AI_MODEL_NOT_FOUND"),
        (
            f"provider_id={scope_ids['other_provider']}&model_id={scope_ids['model_id']}",
            "AI_MODEL_NOT_FOUND",
        ),
        ("status=LIVE", "VALIDATION_ERROR"),
        ("priority_scope=EVERYONE", "VALIDATION_ERROR"),
        ("page_size=101", "VALIDATION_ERROR"),
    ):
        assert error_code(client.get(f"{RULES}?{query}", headers=admin)) == code, query


# --- authorisation --------------------------------------------------------------------


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


def _calls_require_admin(statement: ast.stmt) -> bool:
    value = statement.value if isinstance(statement, ast.Assign | ast.Expr) else None
    return (
        isinstance(value, ast.Call)
        and isinstance(value.func, ast.Name)
        and value.func.id == "require_admin"
        and [ast.unparse(arg) for arg in value.args] == ["request"]
    )


def test_every_handler_starts_with_require_admin() -> None:
    """Design §2「接口」: the first statement (after the docstring) is `require_admin(request)`."""
    handlers = _handlers()
    # Seven endpoints; fewer means the AST scan found nothing.
    assert len(handlers) == 7

    for handler in handlers:
        body = handler.body
        first = body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            body = body[1:]
        assert body, handler.name
        assert _calls_require_admin(body[0]), handler.name

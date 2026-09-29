"""Pricing rules over time, through the services (design gate #178 v3 §7).

Five groups:

- **periods** (SQLite, frozen clock): only the first GLOBAL rule may be backdated, every other
  first rule starts at `t`; truncation at `t`; reservations and withdrawing them; no backdating;
  retiring the current rule and a superseded one; completeness; retired catalog items; draft
  edits that switch the strategy; the recheck itself, a failed recheck, and "the clock is read
  only after the rule lock";
- **a write failing half way** (audit or commit raises): the `factory` fixture runs each case
  once on SQLite and once on a real MySQL;
- **the publish side of the snapshot trap** (real MySQL, two connections);
- **triggers** (real MySQL): edits of published rules and components, deletes, `RETIRED` back
  to `PUBLISHED`, a draft straight to `RETIRED`, the one-row lock table;
- **constraints and database fallbacks** (real MySQL): scope and NULL combinations, mixed
  strategies, completeness on the publish transition, direct inserts, the open-slot unique
  index, overlapping periods.

Resolution, the property test and the concurrency cases are in test_pricing_rules_resolve.py;
the HTTP contract in test_pricing_rules_api.py. Prices and multipliers are fictional; the
placeholder uuid is all zeros.

The MySQL half needs `BILLING_TEST_DATABASE_URL` pointing at a database that may be wiped; it
skips without it — **a skip is not a pass**, CI sets it and treats a skip as a failure.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import uuid
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import pytest
from alembic.config import Config
from sqlalchemy import Engine, create_engine, delete, func, select, text, update
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from alembic import command
from app.core.database import create_session_factory
from app.models.ai_catalog import (
    SEED_METER_TYPES,
    AiModel,
    AiModelAlias,
    AiProvider,
    CatalogStatus,
    UsageMeterComponent,
    UsageMeterType,
)
from app.models.auth import AuditAction, AuditLog, User, UserRole, UserStatus
from app.models.base import Base
from app.models.pricing_rules import (
    PricingRule,
    PricingRuleComponent,
    PricingRuleStatus,
    PricingScope,
    PricingStrategy,
)
from app.models.tenancy import Tenant
from app.repositories import pricing_rules as rule_repository
from app.repositories.pricing_rules import resolve_pricing_rule
from app.schemas.ai_catalog import ModelView
from app.schemas.pricing_rules import PricingRuleView, RuleComponentInput
from app.services import ai_catalog, pricing_rules
from app.services.auth import RequestContext, utc_now
from app.services.pricing_rules import (
    EffectiveFromConflict,
    EffectiveFromInPast,
    PricingPeriodsBroken,
    PricingRuleFinal,
    PricingRuleIncomplete,
    PricingRuleNotDraft,
    PricingRuleNotRetirable,
    PricingRuleShapeInvalid,
    verify_periods,
)
from app.services.provider_prices import CatalogItemRetired

TEST_DATABASE_URL = os.environ.get("BILLING_TEST_DATABASE_URL", "")
TEST_EMAIL_DOMAIN = "@pricing-rules-test.example.com"
CONTEXT = RequestContext(ip_address="203.0.113.9", user_agent="pricing-rules-test")

T0 = dt.datetime(2026, 9, 29, 8, 30, 0)
LONG_AGO = dt.datetime(2000, 1, 1)
MICRO = dt.timedelta(microseconds=1)
SECOND = dt.timedelta(seconds=1)
DAY = dt.timedelta(days=1)

# Obviously fictional MYR tax-inclusive prices (public repository): per 1 000 000 tokens.
FICTIONAL_TOKEN_RATES = {
    "LLM_INPUT_TOKEN": "1.11111111",
    "LLM_OUTPUT_TOKEN": "2.22222222",
    "LLM_CACHE_WRITE_TOKEN": "3.33333333",
    "LLM_CACHE_READ_TOKEN": "4.44444444",
}
FICTIONAL_MULTIPLIER = "1.23456789"
OTHER_MULTIPLIER = "2.34567891"

# MySQL error numbers and the trigger messages.
_ER_DUP_ENTRY = 1062
_ER_ROW_IS_REFERENCED = 1451
_ER_NO_REFERENCED_ROW = 1452
_ER_SIGNAL_EXCEPTION = 1644
_ER_CHECK_CONSTRAINT_VIOLATED = 3819
_IMMUTABLE = "a published pricing rule is immutable"
_RETIRE_ONLY = "a published pricing rule can only be retired"
_DISCARDED_FINAL = "a discarded pricing rule is final"
_DRAFT_TRANSITION = "a draft pricing rule is published or discarded"
_DRAFT_ONLY = "pricing rule components change only on a draft"
_NEVER_DELETED = "pricing rules are never deleted"
_STARTS_AS_DRAFT = "a pricing rule starts as a draft"
_NO_COMPONENTS = "a fixed rate pricing rule needs at least one component"
_MISSING_COMPONENT = "a fixed rate pricing rule misses a meter type component"
_OVERLAP = "pricing rule periods overlap"
_ONE_LOCK_ROW = "pricing_rule_locks holds exactly one row"

GLOBAL = PricingScope.GLOBAL
GLOBAL_MODEL = PricingScope.GLOBAL_PROVIDER_MODEL
CUSTOMER = PricingScope.CUSTOMER
CUSTOMER_PROVIDER = PricingScope.CUSTOMER_PROVIDER
CUSTOMER_MODEL = PricingScope.CUSTOMER_PROVIDER_MODEL


# --- helpers ----------------------------------------------------------------------


class Clock:
    """A frozen clock. The services call it once they hold the pricing rule lock."""

    def __init__(self, moment: dt.datetime) -> None:
        self.now = moment

    def __call__(self) -> dt.datetime:
        return self.now

    def advance(self, **delta: float) -> None:
        self.now += dt.timedelta(**delta)


def component(code: str, rate: str, unit_quantity: str = "1000000") -> RuleComponentInput:
    return RuleComponentInput.model_validate(
        {"component_code": code, "unit_quantity": unit_quantity, "rate_amount": rate}
    )


def token_components() -> list[RuleComponentInput]:
    """All four LLM_TOKEN components: a complete set."""
    return [component(code, rate) for code, rate in FICTIONAL_TOKEN_RATES.items()]


def make_admin(factory: sessionmaker[Session]) -> User:
    """A committed ADMIN, detached with its columns loaded — what `require_admin` returns."""
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
        return user


def make_tenant(factory: sessionmaker[Session], name: str) -> str:
    """A tenant written directly (no wallet needed here); returns its public id."""
    public_id = str(uuid.uuid4())
    with factory() as session:
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


class Rules:
    """One admin, one provider with its models and some tenants, driven through the services."""

    def __init__(
        self,
        factory: sessionmaker[Session],
        clock: Callable[[], dt.datetime],
        *,
        provider_code: str = "anthropic",
        models: tuple[str, ...] = ("model-m",),
        tenants: tuple[str, ...] = ("tenant-a",),
    ) -> None:
        self.factory = factory
        self.clock = clock
        self.admin = make_admin(factory)
        self.code = provider_code
        self.provider = ai_catalog.create_provider(
            factory,
            actor=self.admin,
            code=provider_code,
            display_name=provider_code,
            context=CONTEXT,
            clock=clock,
        )
        self.models: dict[str, ModelView] = {}
        for code in models:
            self.models[code] = ai_catalog.create_model(
                factory,
                actor=self.admin,
                provider_id=self.provider.id,
                code=code,
                display_name=code,
                context=CONTEXT,
                clock=clock,
            )
        self.tenants = {name: make_tenant(factory, f"{provider_code}-{name}") for name in tenants}

    def scope_fields(
        self, scope: PricingScope, tenant: str = "tenant-a", model: str = "model-m"
    ) -> dict[str, str | None]:
        return {
            "customer_id": self.tenants[tenant] if scope.has_customer else None,
            "provider_id": self.provider.id if scope.has_provider else None,
            "model_id": self.models[model].id if scope.has_model else None,
        }

    def draft(
        self,
        scope: PricingScope = GLOBAL,
        *,
        tenant: str = "tenant-a",
        model: str = "model-m",
        strategy: PricingStrategy = PricingStrategy.MARKUP,
        multiplier: str | None = FICTIONAL_MULTIPLIER,
        components: list[RuleComponentInput] | None = None,
    ) -> PricingRuleView:
        if strategy is PricingStrategy.FIXED_RATE:
            multiplier = None
            components = token_components() if components is None else components
        return pricing_rules.create_draft(
            self.factory,
            actor=self.admin,
            priority_scope=scope,
            **self.scope_fields(scope, tenant, model),
            strategy=strategy,
            markup_multiplier=None if multiplier is None else Decimal(multiplier),
            components=components,
            context=CONTEXT,
            clock=self.clock,
        )

    def edit(
        self,
        rule_id: str,
        *,
        strategy: PricingStrategy | None = None,
        multiplier: str | None = None,
        components: list[RuleComponentInput] | None = None,
    ) -> PricingRuleView:
        return pricing_rules.update_draft(
            self.factory,
            actor=self.admin,
            rule_id=rule_id,
            strategy=strategy,
            markup_multiplier=None if multiplier is None else Decimal(multiplier),
            components=components,
            context=CONTEXT,
            clock=self.clock,
        )

    def publish(self, rule_id: str, effective_from: dt.datetime | None = None) -> PricingRuleView:
        return pricing_rules.publish(
            self.factory,
            actor=self.admin,
            rule_id=rule_id,
            effective_from=effective_from,
            context=CONTEXT,
            clock=self.clock,
        )

    def publish_new(
        self,
        scope: PricingScope = GLOBAL,
        effective_from: dt.datetime | None = None,
        **options: object,
    ) -> PricingRuleView:
        return self.publish(self.draft(scope, **options).id, effective_from)

    def retire(self, rule_id: str) -> PricingRuleView:
        return pricing_rules.retire(
            self.factory,
            actor=self.admin,
            rule_id=rule_id,
            reason="Fictional retirement",
            context=CONTEXT,
            clock=self.clock,
        )

    def discard(self, rule_id: str) -> PricingRuleView:
        return pricing_rules.discard_draft(
            self.factory, actor=self.admin, rule_id=rule_id, context=CONTEXT, clock=self.clock
        )

    def rule(self, rule_id: str) -> PricingRuleView:
        return pricing_rules.get_pricing_rule(self.factory, rule_id)

    def ids(self, tenant: str = "tenant-a", model: str = "model-m") -> tuple[int, int, int]:
        """(tenant id, provider id, model id), all internal."""
        return internal_ids(self.factory, self.tenants[tenant], self.models[model].id)

    def resolve(
        self, occurred_at: dt.datetime, *, tenant: str = "tenant-a", model: str = "model-m"
    ) -> str | None:
        """The public id of the rule in effect, resolved in a transaction of its own."""
        with self.factory() as session:
            found = resolve_pricing_rule(session, *self.ids(tenant, model), occurred_at)
            session.commit()
            return None if found is None else found.public_id


def internal_ids(
    factory: sessionmaker[Session], tenant_public_id: str, model_public_id: str
) -> tuple[int, int, int]:
    with factory() as session:
        tenant_id = session.execute(
            select(Tenant.id).where(Tenant.public_id == tenant_public_id)
        ).scalar_one()
        provider_id, model_id = session.execute(
            select(AiModel.provider_id, AiModel.id).where(AiModel.public_id == model_public_id)
        ).one()
        return int(tenant_id), int(provider_id), int(model_id)


def snapshot(factory: sessionmaker[Session]) -> dict[str, object]:
    """Every rule row a write may touch, as committed, plus the audit count."""
    statements = {
        "rules": select(
            PricingRule.public_id,
            PricingRule.status,
            PricingRule.strategy,
            PricingRule.markup_multiplier,
            PricingRule.effective_from,
            PricingRule.effective_to,
            PricingRule.approved_by,
            PricingRule.approved_at,
            PricingRule.updated_at,
        ).order_by(PricingRule.id),
        "components": select(
            PricingRuleComponent.pricing_rule_id,
            PricingRuleComponent.usage_meter_component_id,
            PricingRuleComponent.unit_quantity,
            PricingRuleComponent.rate_amount,
        ).order_by(PricingRuleComponent.id),
    }
    stored: dict[str, object] = {}
    with factory() as session:
        for name, statement in statements.items():
            stored[name] = [tuple(row) for row in session.execute(statement)]
        count = select(func.count()).select_from(AuditLog)
        stored["audits"] = session.execute(count).scalar_one()
    return stored


def audit_states(factory: sessionmaker[Session], action: AuditAction) -> list[tuple]:
    """(before, after, reason) of every audit row with the action, oldest first."""
    statement = select(AuditLog).where(AuditLog.action == action).order_by(AuditLog.id)
    with factory() as session:
        return [
            (
                json.loads(row.before_state) if row.before_state else None,
                json.loads(row.after_state) if row.after_state else None,
                row.reason,
            )
            for row in session.execute(statement).scalars()
        ]


# --- fixtures ---------------------------------------------------------------------


@pytest.fixture
def sqlite_factory() -> Iterator[sessionmaker[Session]]:
    yield from _sqlite_factory()


@pytest.fixture
def mysql_factory(monkeypatch) -> Iterator[sessionmaker[Session]]:
    yield from _mysql_factory(monkeypatch)


@pytest.fixture(params=["sqlite", "mysql"])
def factory(request, monkeypatch) -> Iterator[sessionmaker[Session]]:
    if request.param == "mysql":
        yield from _mysql_factory(monkeypatch)
        return
    yield from _sqlite_factory()


def _sqlite_factory() -> Iterator[sessionmaker[Session]]:
    # `create_all` also writes the one row of `pricing_rule_locks` (app/models/pricing_rules.py).
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    seed_meter_types(factory)
    yield factory
    engine.dispose()


def _mysql_factory(monkeypatch) -> Iterator[sessionmaker[Session]]:
    if not TEST_DATABASE_URL:
        pytest.skip("BILLING_TEST_DATABASE_URL is not set; the MySQL half needs a real MySQL")
    # Tables through alembic: triggers, generated columns, unique indexes, composite foreign
    # keys, checks and the lock row are the ones migration 0014 builds; meter types are 0012's.
    from app.core.config import get_settings

    monkeypatch.setenv("BILLING_DATABASE_URL", TEST_DATABASE_URL)
    get_settings.cache_clear()
    command.upgrade(Config("alembic.ini"), "head")
    get_settings.cache_clear()

    engine = create_engine(TEST_DATABASE_URL, pool_size=6, max_overflow=2)
    clean(engine)
    try:
        yield create_session_factory(engine)
    finally:
        clean(engine)
        engine.dispose()


def clean(engine: Engine) -> None:
    """⚠️ The database is shared: wipe before and after every case. Meter types stay.

    The rule triggers refuse DELETE, so TRUNCATE (DDL, no triggers) with foreign key checks off.
    `pricing_rule_locks` is never touched: its one row belongs to the migration. The audit table
    is append-only (0010) and truncated the same way.
    """
    with engine.begin() as connection:
        connection.exec_driver_sql("SET FOREIGN_KEY_CHECKS = 0")
        try:
            connection.exec_driver_sql("TRUNCATE TABLE pricing_rule_components")
            connection.exec_driver_sql("TRUNCATE TABLE pricing_rules")
        finally:
            connection.exec_driver_sql("SET FOREIGN_KEY_CHECKS = 1")
    with engine.begin() as connection:
        connection.exec_driver_sql("TRUNCATE TABLE audit_logs")
    with engine.begin() as connection:
        connection.execute(delete(AiModelAlias))
        connection.execute(delete(AiModel))
        connection.execute(delete(AiProvider))
        connection.execute(delete(Tenant).where(Tenant.email.like(f"%{TEST_EMAIL_DOMAIN}")))
        connection.execute(delete(User).where(User.email.like(f"%{TEST_EMAIL_DOMAIN}")))


# --- periods (design §2「发布：区间怎么定」, §7) ------------------------------------------


def test_the_first_global_rule_without_a_time_applies_to_all_time(sqlite_factory) -> None:
    """§7 "backdating the first global default": `effective_from` is NULL."""
    rules = Rules(sqlite_factory, Clock(T0))
    assert rules.resolve(LONG_AGO) is None

    first = rules.publish_new(GLOBAL)

    assert (first.status, first.effective_from, first.effective_to) == ("PUBLISHED", None, None)
    assert (first.approved_at, first.approved_by_email) == (T0, rules.admin.email)
    for moment in (LONG_AGO, T0, T0 + DAY):
        assert rules.resolve(moment) == first.id


@pytest.mark.parametrize(
    "scope", [CUSTOMER_MODEL, CUSTOMER_PROVIDER, CUSTOMER, GLOBAL_MODEL], ids=lambda s: s.value
)
def test_the_first_rule_of_any_other_scope_starts_at_t(sqlite_factory, scope) -> None:
    """§7 "no backdating": a first non-global rule without a time starts at `t`, not NULL."""
    clock = Clock(T0 + dt.timedelta(microseconds=250_000))
    rules = Rules(sqlite_factory, clock)

    first = rules.publish_new(scope)

    t = T0 + SECOND
    assert (first.effective_from, first.effective_to) == (t, None)
    assert rules.resolve(t - MICRO) is None
    assert rules.resolve(t) == first.id


def test_a_backdated_global_default_does_not_change_what_a_customer_rule_priced(
    sqlite_factory,
) -> None:
    """§7: the global default published later from all time; moments the customer rule already
    covered still resolve to it, and only the uncovered past becomes priced."""
    clock = Clock(T0)
    rules = Rules(sqlite_factory, clock)
    customer = rules.publish_new(CUSTOMER)
    clock.advance(seconds=30)
    covered = [T0 + SECOND, T0 + 20 * SECOND]
    before = [rules.resolve(moment) for moment in covered]

    default = rules.publish_new(GLOBAL)

    assert default.effective_from is None
    assert before == [customer.id, customer.id]
    assert [rules.resolve(moment) for moment in covered] == before
    assert rules.resolve(LONG_AGO) == default.id
    assert rules.resolve(T0) == default.id


def test_a_second_global_default_is_not_backdated(sqlite_factory) -> None:
    """Only the very first global default may start at NULL; after a withdrawn reservation the
    scope has had a published rule, so the next one starts at `t` (design §2, literally)."""
    clock = Clock(T0)
    rules = Rules(sqlite_factory, clock)
    reserved = rules.publish_new(GLOBAL, T0 + DAY)
    rules.retire(reserved.id)

    again = rules.publish_new(GLOBAL)

    assert again.effective_from == T0 + SECOND


@pytest.mark.parametrize("offset", [0, 1, 400_000, 999_999], ids=["whole", "1us", "0.4s", "max"])
def test_publishing_truncates_the_open_rule_at_the_next_second(sqlite_factory, offset) -> None:
    clock = Clock(T0)
    rules = Rules(sqlite_factory, clock)
    first = rules.publish_new(GLOBAL_MODEL)
    clock.now = T0 + dt.timedelta(seconds=10, microseconds=offset)

    second = rules.publish_new(GLOBAL_MODEL, strategy=PricingStrategy.FIXED_RATE)

    t = T0 + 11 * SECOND
    assert second.effective_from == t
    assert rules.rule(first.id).effective_to == t
    assert rules.resolve(clock.now) == first.id
    assert rules.resolve(t - MICRO) == first.id
    assert rules.resolve(t) == second.id
    [_, (before, after, _)] = audit_states(sqlite_factory, AuditAction.PRICING_PUBLISH)
    assert before == {
        "status": "DRAFT",
        "truncated_rule": {"id": first.id, "effective_to": None},
    }
    assert after["truncated_rule"] == {"id": first.id, "effective_to": t.isoformat()}
    assert after["effective_from"] == t.isoformat()
    assert after["strategy"] == "FIXED_RATE"
    assert after["markup_multiplier"] is None
    assert after["priority_scope"] == "GLOBAL_PROVIDER_MODEL"
    assert (after["provider_code"], after["model_code"]) == ("anthropic", "model-m")
    assert after["customer_id"] is None
    assert [item["component_code"] for item in after["components"]] == sorted(FICTIONAL_TOKEN_RATES)


def test_a_reservation_takes_over_at_its_time_and_withdrawing_it_restores(sqlite_factory) -> None:
    """§7 "reservations": the previous rule is truncated at F; withdrawing the reservation before
    F leaves it an empty period that never matches, and the previous rule is open again."""
    clock = Clock(T0)
    rules = Rules(sqlite_factory, clock)
    first = rules.publish_new(CUSTOMER)
    reserved_at = T0 + DAY
    second = rules.publish_new(CUSTOMER, reserved_at, multiplier=OTHER_MULTIPLIER)

    assert rules.rule(first.id).effective_to == reserved_at
    assert rules.resolve(reserved_at - MICRO) == first.id
    assert rules.resolve(reserved_at) == second.id

    clock.advance(seconds=10)
    withdrawn = rules.retire(second.id)

    assert (withdrawn.status, withdrawn.effective_from, withdrawn.effective_to) == (
        "RETIRED",
        reserved_at,
        reserved_at,
    )
    assert rules.rule(first.id).effective_to is None
    for moment in (T0 + SECOND, reserved_at, reserved_at + DAY):
        assert rules.resolve(moment) == first.id
    [(before, after, reason)] = audit_states(sqlite_factory, AuditAction.PRICING_RETIRE)
    assert before["restored_rule"] == {"id": first.id, "effective_to": reserved_at.isoformat()}
    assert after["restored_rule"] == {"id": first.id, "effective_to": None}
    assert after["effective_to"] == reserved_at.isoformat()
    assert reason == "Fictional retirement"


def test_backdating_and_conflicting_times_are_refused(sqlite_factory) -> None:
    """§7: F < `t` is 422; F not after the latest start is 409. Nothing is written."""
    clock = Clock(T0 + dt.timedelta(microseconds=500_000))
    rules = Rules(sqlite_factory, clock)
    draft = rules.draft(GLOBAL)
    before = snapshot(sqlite_factory)

    for past in (T0, T0 - DAY, LONG_AGO):
        with pytest.raises(EffectiveFromInPast):
            rules.publish(draft.id, past)
    assert snapshot(sqlite_factory) == before

    reserved_at = T0 + DAY
    rules.publish(draft.id, reserved_at)
    later = rules.draft(GLOBAL)
    before = snapshot(sqlite_factory)
    for requested in (reserved_at, reserved_at - SECOND, T0 + SECOND, None):
        with pytest.raises(EffectiveFromConflict):
            rules.publish(later.id, requested)
    assert snapshot(sqlite_factory) == before

    assert rules.publish(later.id, reserved_at + SECOND).effective_from == reserved_at + SECOND
    # Exactly `t` is not backdating.
    assert rules.publish_new(CUSTOMER, T0 + SECOND).effective_from == T0 + SECOND


def test_retiring_the_current_rule_falls_through_to_the_next_level(sqlite_factory) -> None:
    """§7 "falling through after a retirement": before `t` the customer rule, from `t` the
    global default."""
    clock = Clock(T0)
    rules = Rules(sqlite_factory, clock)
    default = rules.publish_new(GLOBAL)
    clock.advance(seconds=5)
    customer = rules.publish_new(CUSTOMER)
    clock.now = T0 + dt.timedelta(seconds=20, microseconds=300_000)

    retired = rules.retire(customer.id)

    t = T0 + 21 * SECOND
    assert (retired.status, retired.effective_to) == ("RETIRED", t)
    assert rules.resolve(T0 + 6 * SECOND) == customer.id
    assert rules.resolve(t - MICRO) == customer.id
    assert rules.resolve(t) == default.id
    assert rules.resolve(T0 + SECOND) == default.id


def test_retiring_a_superseded_rule_and_final_states_are_refused(sqlite_factory) -> None:
    """§7 "retiring a historical rule": 409; drafts are not retirable; retired and discarded
    rules are final. Nothing is written."""
    clock = Clock(T0)
    rules = Rules(sqlite_factory, clock)
    first = rules.publish_new(GLOBAL)
    clock.advance(seconds=5)
    second = rules.publish_new(GLOBAL)
    draft = rules.draft(GLOBAL)
    discarded = rules.discard(rules.draft(GLOBAL).id)
    clock.advance(seconds=5)
    retired = rules.retire(second.id)
    before = snapshot(sqlite_factory)

    for rule_id in (first.id, draft.id):
        with pytest.raises(PricingRuleNotRetirable):
            rules.retire(rule_id)
    for rule_id in (retired.id, discarded.id):
        with pytest.raises(PricingRuleFinal):
            rules.retire(rule_id)
        with pytest.raises(PricingRuleFinal):
            rules.publish(rule_id)
        with pytest.raises(PricingRuleFinal):
            rules.edit(rule_id, multiplier=OTHER_MULTIPLIER)
    with pytest.raises(PricingRuleNotDraft):
        rules.edit(first.id, multiplier=OTHER_MULTIPLIER)
    with pytest.raises(PricingRuleNotDraft):
        rules.discard(first.id)

    assert snapshot(sqlite_factory) == before


def test_publishing_after_a_retirement_leaves_the_gap_to_lower_levels(sqlite_factory) -> None:
    clock = Clock(T0)
    rules = Rules(sqlite_factory, clock)
    first = rules.publish_new(CUSTOMER)
    clock.advance(seconds=10)
    ended = rules.retire(first.id).effective_to
    assert ended == T0 + 11 * SECOND
    clock.advance(seconds=90)

    second = rules.publish_new(CUSTOMER)

    assert second.effective_from == T0 + 101 * SECOND
    assert rules.resolve(ended - MICRO) == first.id
    assert rules.resolve(T0 + 100 * SECOND) is None
    assert rules.resolve(T0 + 101 * SECOND) == second.id


def test_republishing_is_idempotent(sqlite_factory) -> None:
    rules = Rules(sqlite_factory, Clock(T0))
    published = rules.publish_new(GLOBAL)
    before = snapshot(sqlite_factory)

    again = rules.publish(published.id, T0 + DAY)

    assert again == published
    assert snapshot(sqlite_factory) == before


def test_every_scope_has_its_own_timeline(sqlite_factory) -> None:
    clock = Clock(T0)
    rules = Rules(sqlite_factory, clock, tenants=("tenant-a", "tenant-b"))
    mine = rules.publish_new(CUSTOMER, tenant="tenant-a")
    clock.advance(seconds=5)

    theirs = rules.publish_new(CUSTOMER, tenant="tenant-b")

    assert rules.rule(mine.id).effective_to is None
    assert theirs.effective_from == T0 + 6 * SECOND
    assert rules.resolve(T0 + DAY, tenant="tenant-a") == mine.id
    assert rules.resolve(T0 + DAY, tenant="tenant-b") == theirs.id


# --- completeness, catalog status and draft edits -----------------------------------


def test_an_incomplete_fixed_rate_rule_is_not_published(sqlite_factory) -> None:
    """§7 "FIXED_RATE completeness": no components; LLM_TOKEN with three of four. 409, no write."""
    rules = Rules(sqlite_factory, Clock(T0))
    fixed = PricingStrategy.FIXED_RATE
    empty = rules.draft(GLOBAL, strategy=fixed, components=[])
    short = rules.draft(GLOBAL, strategy=fixed, components=token_components()[:3])
    before = snapshot(sqlite_factory)

    with pytest.raises(PricingRuleIncomplete) as raised:
        rules.publish(empty.id)
    assert raised.value.missing == ()
    with pytest.raises(PricingRuleIncomplete) as raised:
        rules.publish(short.id)
    assert raised.value.missing == ("LLM_CACHE_READ_TOKEN",)

    assert snapshot(sqlite_factory) == before
    # A meter type that is not priced at all is not required; a single-component type is whole.
    audio = rules.draft(GLOBAL, strategy=fixed, components=[component("AUDIO_SECOND", "0.1")])
    assert rules.publish(audio.id).status == "PUBLISHED"


@pytest.mark.parametrize("item", ["provider", "model", "meter_type"])
def test_retired_catalog_items_take_no_new_rules(sqlite_factory, item: str) -> None:
    """§7 "retired catalog items": 409 on create and on publish. Nothing is written."""
    rules = Rules(sqlite_factory, Clock(T0))
    draft = rules.draft(GLOBAL_MODEL, strategy=PricingStrategy.FIXED_RATE)
    statements = {
        "provider": update(AiProvider),
        "model": update(AiModel),
        "meter_type": update(UsageMeterType).where(UsageMeterType.code == "LLM_TOKEN"),
    }
    with sqlite_factory() as session:
        session.execute(statements[item].values(status=CatalogStatus.RETIRED))
        session.commit()
    before = snapshot(sqlite_factory)

    with pytest.raises(CatalogItemRetired):
        rules.publish(draft.id)
    with pytest.raises(CatalogItemRetired):
        rules.draft(GLOBAL_MODEL, strategy=PricingStrategy.FIXED_RATE)

    assert snapshot(sqlite_factory) == before


def test_a_disabled_customer_may_still_get_a_rule(sqlite_factory) -> None:
    """Design §2: the tenant only has to exist; its account status does not matter."""
    rules = Rules(sqlite_factory, Clock(T0))
    with sqlite_factory() as session:
        session.execute(update(Tenant).values(account_status="DISABLED"))
        session.commit()

    assert rules.publish_new(CUSTOMER).status == "PUBLISHED"


def test_switching_a_draft_between_strategies(sqlite_factory) -> None:
    """FIXED_RATE → MARKUP drops the components (composite key) and sets the multiplier;
    MARKUP → FIXED_RATE clears the multiplier; each change is one PRICING_UPDATE."""
    rules = Rules(sqlite_factory, Clock(T0))
    draft = rules.draft(GLOBAL, strategy=PricingStrategy.FIXED_RATE)

    markup = rules.edit(draft.id, strategy=PricingStrategy.MARKUP, multiplier=OTHER_MULTIPLIER)

    assert (markup.strategy, markup.markup_multiplier, markup.components) == (
        "MARKUP",
        "2.34567891",
        [],
    )
    fixed = rules.edit(draft.id, strategy=PricingStrategy.FIXED_RATE, components=token_components())
    assert (fixed.strategy, fixed.markup_multiplier) == ("FIXED_RATE", None)
    assert len(fixed.components) == 4
    [(first_before, first_after, _), (second_before, second_after, _)] = audit_states(
        sqlite_factory, AuditAction.PRICING_UPDATE
    )
    assert first_before["strategy"] == "FIXED_RATE"
    assert first_after == {
        "strategy": "MARKUP",
        "markup_multiplier": "2.34567891",
        "components": [],
    }
    assert len(first_before["components"]) == 4
    assert second_before == {
        "strategy": "MARKUP",
        "markup_multiplier": "2.34567891",
        "components": [],
    }
    assert second_after["markup_multiplier"] is None


@pytest.mark.parametrize(
    ("start", "change"),
    [
        ("FIXED_RATE", {"multiplier": OTHER_MULTIPLIER}),
        ("FIXED_RATE", {"strategy": PricingStrategy.MARKUP}),
        ("MARKUP", {"components": []}),
        ("MARKUP", {"strategy": PricingStrategy.FIXED_RATE, "multiplier": OTHER_MULTIPLIER}),
    ],
    ids=["multiplier-on-fixed", "markup-without-multiplier", "components-on-markup", "mixed"],
)
def test_edits_that_would_mix_the_strategies_are_refused(sqlite_factory, start, change) -> None:
    rules = Rules(sqlite_factory, Clock(T0))
    draft = rules.draft(GLOBAL, strategy=PricingStrategy(start))
    before = snapshot(sqlite_factory)

    with pytest.raises(PricingRuleShapeInvalid):
        rules.edit(draft.id, **change)

    assert snapshot(sqlite_factory) == before


def test_an_edit_that_changes_nothing_writes_nothing(sqlite_factory) -> None:
    clock = Clock(T0)
    rules = Rules(sqlite_factory, clock)
    fixed = rules.draft(GLOBAL, strategy=PricingStrategy.FIXED_RATE)
    markup = rules.draft(GLOBAL)
    clock.advance(seconds=5)
    before = snapshot(sqlite_factory)

    rules.edit(fixed.id, components=list(reversed(token_components())))
    rules.edit(markup.id, multiplier="1.2345678900")
    rules.edit(markup.id, strategy=PricingStrategy.MARKUP)

    assert snapshot(sqlite_factory) == before


def test_the_service_checks_the_scope_fields_as_well(sqlite_factory) -> None:
    """The service does not rely on the request schema for the scope and strategy shape."""
    factory = sqlite_factory
    rules = Rules(factory, Clock(T0))
    before = snapshot(factory)
    wrong = [
        {"priority_scope": GLOBAL, "customer_id": rules.tenants["tenant-a"]},
        {"priority_scope": CUSTOMER},
        {"priority_scope": GLOBAL_MODEL, "provider_id": rules.provider.id},
    ]
    for scope in wrong:
        fields = {"customer_id": None, "provider_id": None, "model_id": None, **scope}
        with pytest.raises(PricingRuleShapeInvalid):
            pricing_rules.create_draft(
                factory,
                actor=rules.admin,
                **fields,  # type: ignore[arg-type]
                strategy=PricingStrategy.MARKUP,
                markup_multiplier=Decimal(FICTIONAL_MULTIPLIER),
                components=None,
                context=CONTEXT,
            )
    with pytest.raises(PricingRuleShapeInvalid):
        pricing_rules.create_draft(
            factory,
            actor=rules.admin,
            priority_scope=GLOBAL,
            customer_id=None,
            provider_id=None,
            model_id=None,
            strategy=PricingStrategy.FIXED_RATE,
            markup_multiplier=Decimal(FICTIONAL_MULTIPLIER),
            components=token_components(),
            context=CONTEXT,
        )
    assert snapshot(factory) == before


# --- the recheck and the lock ----------------------------------------------------


def _rule(
    row_id: int, start: dt.datetime | None, end: dt.datetime | None, status: str
) -> PricingRule:
    return PricingRule(
        id=row_id, status=PricingRuleStatus(status), effective_from=start, effective_to=end
    )


T1 = T0 + SECOND
T2 = T0 + 2 * SECOND


@pytest.mark.parametrize(
    ("rows", "broken"),
    [
        ([], False),
        ([(None, None, "PUBLISHED")], False),
        ([(None, T0, "PUBLISHED"), (T0, None, "PUBLISHED")], False),
        ([(None, T0, "RETIRED"), (T1, None, "PUBLISHED")], False),
        ([(None, T0, "PUBLISHED"), (T0, T0, "RETIRED"), (T0, None, "PUBLISHED")], False),
        ([(T0, None, "PUBLISHED")], False),
        ([(None, None, "PUBLISHED"), (T1, T1, "RETIRED")], False),
        ([(None, None, "PUBLISHED"), (T1, T1, "PUBLISHED")], True),
        ([(None, T1, "PUBLISHED"), (T0, None, "PUBLISHED")], True),
        ([(None, None, "PUBLISHED"), (T0, None, "PUBLISHED")], True),
        ([(None, T0, "PUBLISHED"), (None, None, "PUBLISHED")], True),
        ([(T0, T2, "PUBLISHED"), (T1, None, "PUBLISHED")], True),
    ],
    ids=[
        "empty",
        "one-open",
        "chained",
        "retired-gap",
        "empty-in-between",
        "first-with-a-start",
        "restored-before-a-withdrawn",
        "empty-but-not-retired",
        "overlap",
        "two-open",
        "two-from-the-beginning",
        "partial-overlap",
    ],
)
def test_verify_periods(rows: list, broken: bool) -> None:
    """Design §2: only the first may start at NULL; each end ≤ the next start."""
    found = [_rule(index + 1, *row) for index, row in enumerate(rows)]
    found.reverse()

    if broken:
        with pytest.raises(PricingPeriodsBroken):
            verify_periods(found)
    else:
        verify_periods(found)


def _always_broken(_rows: object) -> None:
    raise PricingPeriodsBroken("injected")


def test_a_failed_recheck_rolls_the_publish_back(sqlite_factory, monkeypatch) -> None:
    """The previous rule is not truncated either."""
    clock = Clock(T0)
    rules = Rules(sqlite_factory, clock)
    rules.publish_new(GLOBAL)
    draft = rules.draft(GLOBAL)
    before = snapshot(sqlite_factory)
    monkeypatch.setattr(pricing_rules, "verify_periods", _always_broken)
    clock.advance(seconds=5)

    with pytest.raises(PricingPeriodsBroken):
        rules.publish(draft.id)

    assert snapshot(sqlite_factory) == before


def test_a_failed_recheck_rolls_the_withdrawal_back(sqlite_factory, monkeypatch) -> None:
    rules = Rules(sqlite_factory, Clock(T0))
    rules.publish_new(GLOBAL)
    reserved = rules.publish_new(GLOBAL, T0 + DAY)
    before = snapshot(sqlite_factory)
    monkeypatch.setattr(pricing_rules, "verify_periods", _always_broken)

    with pytest.raises(PricingPeriodsBroken):
        rules.retire(reserved.id)

    assert snapshot(sqlite_factory) == before


def test_publish_and_retire_read_the_clock_only_after_the_rule_lock(
    sqlite_factory, monkeypatch
) -> None:
    """Design §2: the lock on `pricing_rule_locks` first, then `t`."""
    calls: list[str] = []
    real_lock = rule_repository.lock_pricing_rules

    def lock_spy(session: Session):
        calls.append("lock")
        return real_lock(session)

    def clock() -> dt.datetime:
        calls.append("clock")
        return T0

    rules = Rules(sqlite_factory, clock)
    draft = rules.draft(GLOBAL)
    monkeypatch.setattr(rule_repository, "lock_pricing_rules", lock_spy)

    calls.clear()
    published = rules.publish(draft.id)
    assert calls == ["lock", "clock"]

    calls.clear()
    rules.retire(published.id)
    assert calls == ["lock", "clock"]


def test_the_open_slot_index_sees_the_global_scope_as_one_scope(sqlite_factory) -> None:
    """§7 "unique constraint": `scope_key` makes the NULL columns of the global scope one key,
    so two untruncated published global rules cannot both exist (SQLite has no triggers, so this
    is the index alone; the MySQL half is below)."""
    rules = Rules(sqlite_factory, Clock(T0))
    rules.publish_new(GLOBAL)
    other = rules.draft(GLOBAL)
    with sqlite_factory() as session:
        with pytest.raises(IntegrityError):
            session.execute(
                update(PricingRule)
                .where(PricingRule.public_id == other.id)
                .values(
                    status=PricingRuleStatus.PUBLISHED,
                    effective_from=T0 + DAY,
                    approved_by=rules.admin.id,
                    approved_at=T0,
                )
            )
        session.rollback()


# --- a write failing half way: SQLite and MySQL once each (INV-13) ----------------------


def break_the_audit(monkeypatch) -> None:
    """The audit row fails at flush: `created_at` is NULL (NOT NULL → IntegrityError)."""
    real = pricing_rules.record_audit

    def broken(session: Session, **options: object) -> None:
        real(session, **{**options, "now": None})  # type: ignore[arg-type]

    monkeypatch.setattr(pricing_rules, "record_audit", broken)


def break_the_commit(monkeypatch) -> None:
    def failing_commit(self: Session) -> None:
        raise RuntimeError("injected commit failure")

    monkeypatch.setattr(Session, "commit", failing_commit)


class Scene:
    """A timeline with every kind of row the writes below act on.

    GLOBAL: an open published rule and a draft; CUSTOMER: only a reservation (withdrawing it
    restores nothing); CUSTOMER_PROVIDER: only a draft (publishing it is the scope's first);
    GLOBAL_PROVIDER_MODEL: a published rule and the reservation that truncated it.
    """

    def __init__(self, factory: sessionmaker[Session]) -> None:
        self.clock = Clock(T0)
        self.rules = Rules(factory, self.clock)
        self.current = self.rules.publish_new(GLOBAL)
        self.reserved = self.rules.publish_new(CUSTOMER, T0 + DAY)
        self.rules.publish_new(GLOBAL_MODEL, strategy=PricingStrategy.FIXED_RATE)
        self.truncating = self.rules.publish_new(GLOBAL_MODEL, T0 + DAY)
        self.draft = self.rules.draft(GLOBAL, strategy=PricingStrategy.FIXED_RATE)
        self.lone_draft = self.rules.draft(CUSTOMER_PROVIDER)
        self.clock.advance(seconds=5)


ACTIONS: dict[str, Callable[[Scene], object]] = {
    "create": lambda scene: scene.rules.draft(GLOBAL, strategy=PricingStrategy.FIXED_RATE),
    "update": lambda scene: scene.rules.edit(
        scene.draft.id, strategy=PricingStrategy.MARKUP, multiplier=OTHER_MULTIPLIER
    ),
    "discard": lambda scene: scene.rules.discard(scene.draft.id),
    "publish-truncating": lambda scene: scene.rules.publish(scene.draft.id),
    "publish-first": lambda scene: scene.rules.publish(scene.lone_draft.id),
    "retire-current": lambda scene: scene.rules.retire(scene.current.id),
    "withdraw-reservation": lambda scene: scene.rules.retire(scene.reserved.id),
    "withdraw-restoring": lambda scene: scene.rules.retire(scene.truncating.id),
}


@pytest.mark.parametrize("failure", ["audit", "commit"])
@pytest.mark.parametrize("action", sorted(ACTIONS))
def test_a_failed_write_leaves_nothing(factory, monkeypatch, action: str, failure: str) -> None:
    """§7 "a transaction failing half way": rules, components, the previous rule and the audit
    all roll back. `publish-truncating` × `audit` is "the audit fails after the truncation"."""
    scene = Scene(factory)
    before = snapshot(factory)
    if failure == "audit":
        break_the_audit(monkeypatch)
    else:
        break_the_commit(monkeypatch)

    with pytest.raises(Exception):  # noqa: B017 - either injection raises its own error
        ACTIONS[action](scene)

    assert snapshot(factory) == before


# --- the publish side of the snapshot trap (real MySQL) ----------------------------------


def test_a_publish_sees_a_model_retired_while_it_waited(mysql_factory) -> None:
    """Another transaction holds the rule lock and retires the model; the publish waits for the
    lock and then reads the retired model — 409, nothing written. A plain read before the lock
    would have kept the REPEATABLE READ snapshot of "active"."""
    rules = Rules(mysql_factory, utc_now)
    draft = rules.draft(GLOBAL_MODEL)
    _, _, model_id = rules.ids()
    before = snapshot(mysql_factory)
    retiring = mysql_factory()
    try:
        retiring.execute(text("SELECT id FROM pricing_rule_locks WHERE id = 1 FOR UPDATE")).one()
        retiring.execute(
            update(AiModel).where(AiModel.id == model_id).values(status=CatalogStatus.RETIRED)
        )
        with ThreadPoolExecutor(max_workers=1) as pool:
            publishing = pool.submit(rules.publish, draft.id)
            with pytest.raises(TimeoutError):
                publishing.result(timeout=2)
            retiring.commit()
            with pytest.raises(CatalogItemRetired):
                publishing.result(timeout=30)
    finally:
        retiring.close()

    assert snapshot(mysql_factory) == before


# --- triggers (real MySQL) ----------------------------------------------------------


def _refused(factory: sessionmaker[Session], statement, **params: object) -> tuple[int, str]:
    """Run in a transaction that is rolled back; return the MySQL error number and message."""
    with factory() as session:
        with pytest.raises(DBAPIError) as raised:
            session.execute(statement, params)
        session.rollback()
    return _error(raised)


def _accepted(factory: sessionmaker[Session], statement, **params: object) -> None:
    """Run and roll back: the database let it through."""
    with factory() as session:
        session.execute(statement, params)
        session.rollback()


def _error(raised: pytest.ExceptionInfo[DBAPIError]) -> tuple[int, str]:
    errno, message = raised.value.orig.args[:2]
    return int(errno), str(message)


def _internal_rule_id(factory: sessionmaker[Session], public_id: str) -> int:
    statement = select(PricingRule.id).where(PricingRule.public_id == public_id)
    with factory() as session:
        return int(session.execute(statement).scalar_one())


def _update_rule(assignment: str):
    return text(f"UPDATE pricing_rules SET {assignment} WHERE id = :id")


_UPDATE_COMPONENTS = text(
    "UPDATE pricing_rule_components SET rate_amount = 9.99999999 WHERE pricing_rule_id = :id"
)
_DELETE_RULE = text("DELETE FROM pricing_rules WHERE id = :id")
_DELETE_COMPONENTS = text("DELETE FROM pricing_rule_components WHERE pricing_rule_id = :id")
_INSERT_COMPONENT = text(
    "INSERT INTO pricing_rule_components (pricing_rule_id, strategy, usage_meter_component_id,"
    " unit_quantity, rate_amount, currency, created_at)"
    " SELECT :id, :strategy, id, 1, 1, :currency, :now FROM usage_meter_components"
    " WHERE component_code = :code"
)


def _insert_component(session: Session, rule_id: int, code: str, **overrides: object) -> None:
    params = {"id": rule_id, "strategy": "FIXED_RATE", "currency": "MYR", "now": T0, "code": code}
    session.execute(_INSERT_COMPONENT, {**params, **overrides})


@pytest.mark.parametrize(
    "assignment",
    [
        "markup_multiplier = 9.99999999",
        "priority_scope = 'GLOBAL', provider_id = NULL, model_id = NULL",
        "effective_from = '2026-09-29 08:30:00'",
        "approved_at = '2026-09-29 09:00:00'",
    ],
    ids=["multiplier", "scope", "start", "approved-at"],
)
def test_a_published_rule_refuses_edits(mysql_factory, assignment: str) -> None:
    """§7 "triggers": the multiplier, the scope and the start of a published rule are frozen."""
    rules = Rules(mysql_factory, utc_now)
    published = rules.publish_new(GLOBAL_MODEL)
    rule_id = _internal_rule_id(mysql_factory, published.id)
    before = snapshot(mysql_factory)

    refused = _refused(mysql_factory, _update_rule(assignment), id=rule_id)

    assert refused == (_ER_SIGNAL_EXCEPTION, _IMMUTABLE)
    assert snapshot(mysql_factory) == before


def test_published_components_are_frozen_and_rules_never_deleted(mysql_factory) -> None:
    rules = Rules(mysql_factory, utc_now)
    published = rules.publish_new(GLOBAL, strategy=PricingStrategy.FIXED_RATE)
    draft = rules.draft(GLOBAL, strategy=PricingStrategy.FIXED_RATE)
    published_id = _internal_rule_id(mysql_factory, published.id)
    draft_id = _internal_rule_id(mysql_factory, draft.id)
    before = snapshot(mysql_factory)
    add = {"strategy": "FIXED_RATE", "currency": "MYR", "now": T0, "code": "EMBEDDING_TOKEN"}

    frozen = [
        (_UPDATE_COMPONENTS, {"id": published_id}),
        (_DELETE_COMPONENTS, {"id": published_id}),
        (_INSERT_COMPONENT, {"id": published_id, **add}),
    ]
    for statement, params in frozen:
        assert _refused(mysql_factory, statement, **params) == (_ER_SIGNAL_EXCEPTION, _DRAFT_ONLY)
    for rule_id in (published_id, draft_id):
        refused = _refused(mysql_factory, _DELETE_RULE, id=rule_id)
        assert refused == (_ER_SIGNAL_EXCEPTION, _NEVER_DELETED)
    assert snapshot(mysql_factory) == before

    # A draft's components change freely (editing a draft replaces them).
    _accepted(mysql_factory, _UPDATE_COMPONENTS, id=draft_id)
    _accepted(mysql_factory, _DELETE_COMPONENTS, id=draft_id)
    _accepted(mysql_factory, _INSERT_COMPONENT, id=draft_id, **add)


def test_only_truncation_and_retirement_pass_on_a_published_rule(mysql_factory) -> None:
    clock = Clock(utc_now())
    rules = Rules(mysql_factory, clock)
    published = rules.publish_new(GLOBAL)
    discarded = rules.discard(rules.draft(GLOBAL).id)
    published_id = _internal_rule_id(mysql_factory, published.id)
    discarded_id = _internal_rule_id(mysql_factory, discarded.id)
    end = "effective_to = '2999-01-01 00:00:00'"

    _accepted(mysql_factory, _update_rule(end), id=published_id)
    _accepted(mysql_factory, _update_rule(f"status = 'RETIRED', {end}"), id=published_id)

    clock.advance(seconds=5)
    assert rules.retire(published.id).status == "RETIRED"
    back = _update_rule("status = 'PUBLISHED'")
    assert _refused(mysql_factory, back, id=published_id) == (_ER_SIGNAL_EXCEPTION, _RETIRE_ONLY)
    edit = _update_rule("markup_multiplier = 9.99999999")
    assert _refused(mysql_factory, edit, id=discarded_id) == (
        _ER_SIGNAL_EXCEPTION,
        _DISCARDED_FINAL,
    )


def test_the_lock_table_holds_exactly_one_row(mysql_factory) -> None:
    """§7 "triggers": deleting the lock row or inserting another one is refused."""
    delete_row = text("DELETE FROM pricing_rule_locks WHERE id = 1")
    insert_row = text("INSERT INTO pricing_rule_locks (id) VALUES (2)")

    assert _refused(mysql_factory, delete_row) == (_ER_SIGNAL_EXCEPTION, _ONE_LOCK_ROW)
    assert _refused(mysql_factory, insert_row) == (_ER_SIGNAL_EXCEPTION, _ONE_LOCK_ROW)
    with mysql_factory() as session:
        assert session.execute(text("SELECT id FROM pricing_rule_locks")).scalars().all() == [1]


# --- constraints and database fallbacks (real MySQL) ---------------------------------------


_INSERT_RULE = text(
    "INSERT INTO pricing_rules (public_id, priority_scope, tenant_id, provider_id, model_id,"
    " strategy, markup_multiplier, status, effective_from, effective_to, created_by, approved_by,"
    " created_at, updated_at, approved_at) VALUES (:public_id, :scope, :tenant_id, :provider_id,"
    " :model_id, :strategy, :multiplier, :status, NULL, NULL, :user, :approver, :now, :now,"
    " :approved_at)"
)
_PUBLISH_DIRECTLY = text(
    "UPDATE pricing_rules SET status = :status, effective_from = :start, effective_to = :end,"
    " approved_by = :user, approved_at = :now WHERE id = :id"
)


def _insert_rule(
    session: Session,
    rules: Rules,
    *,
    scope: str = "GLOBAL",
    columns: tuple[bool, bool, bool] | None = None,
    strategy: str = "MARKUP",
    multiplier: str | None = FICTIONAL_MULTIPLIER,
    status: str = "DRAFT",
):
    """A rule row written with plain SQL. `columns`: which of (tenant, provider, model) are set;
    by default the ones the scope needs."""
    tenant_id, provider_id, model_id = rules.ids()
    if columns is None:
        level = PricingScope(scope)
        columns = (level.has_customer, level.has_provider, level.has_model)
    approved = status != "DRAFT"
    return session.execute(
        _INSERT_RULE,
        {
            "public_id": str(uuid.uuid4()),
            "scope": scope,
            "tenant_id": tenant_id if columns[0] else None,
            "provider_id": provider_id if columns[1] else None,
            "model_id": model_id if columns[2] else None,
            "strategy": strategy,
            "multiplier": multiplier,
            "status": status,
            "user": rules.admin.id,
            "approver": rules.admin.id if approved else None,
            "now": T0,
            "approved_at": T0 if approved else None,
        },
    )


def _direct_draft(
    session: Session,
    rules: Rules,
    codes: tuple[str, ...] | None = None,
    *,
    scope: str = "GLOBAL",
) -> int:
    """A draft written with plain SQL (the insert trigger lets drafts in). `codes` given: a
    FIXED_RATE draft with those components; otherwise a MARKUP draft."""
    if codes is None:
        return int(_insert_rule(session, rules, scope=scope).lastrowid)
    fixed = _insert_rule(session, rules, scope=scope, strategy="FIXED_RATE", multiplier=None)
    rule_id = int(fixed.lastrowid)
    for code in codes:
        _insert_component(session, rule_id, code)
    return rule_id


def _publish_directly(
    session: Session,
    rules: Rules,
    rule_id: int,
    period: tuple[dt.datetime | None, dt.datetime | None],
    status: str = "PUBLISHED",
) -> None:
    start, end = period
    parameters = {
        "status": status,
        "start": start,
        "end": end,
        "user": rules.admin.id,
        "now": T0,
        "id": rule_id,
    }
    session.execute(_PUBLISH_DIRECTLY, parameters)


@pytest.mark.parametrize(
    ("scope", "columns"),
    [
        ("GLOBAL_PROVIDER_MODEL", (False, True, False)),
        ("CUSTOMER_PROVIDER_MODEL", (True, False, True)),
        ("GLOBAL", (False, False, True)),
        ("CUSTOMER", (True, True, False)),
        ("GLOBAL", (True, False, False)),
        ("GLOBAL_PROVIDER", (False, True, False)),
    ],
    ids=[
        "global-provider-without-model",
        "customer-model-without-provider",
        "only-a-model",
        "customer-with-provider",
        "global-with-tenant",
        "unknown-scope",
    ],
)
def test_the_checks_refuse_scope_and_null_combinations_outside_section_16(
    mysql_factory, scope: str, columns: tuple[bool, bool, bool]
) -> None:
    """§7 "scope and NULL combinations": CHECK refuses what §16 does not list."""
    rules = Rules(mysql_factory, utc_now)
    with mysql_factory() as session:
        with pytest.raises(DBAPIError) as raised:
            _insert_rule(session, rules, scope=scope, columns=columns)
        session.rollback()

    assert _error(raised)[0] == _ER_CHECK_CONSTRAINT_VIOLATED


@pytest.mark.parametrize(
    ("strategy", "multiplier"),
    [("FIXED_RATE", FICTIONAL_MULTIPLIER), ("MARKUP", None), ("MARKUP", "0"), ("MARKUP", "-1")],
    ids=["fixed-rate-with-multiplier", "markup-without", "markup-zero", "markup-negative"],
)
def test_the_checks_refuse_a_mixed_strategy(mysql_factory, strategy, multiplier) -> None:
    """§7 "mixed strategies": the multiplier belongs to MARKUP only, and is positive."""
    rules = Rules(mysql_factory, utc_now)
    with mysql_factory() as session:
        with pytest.raises(DBAPIError) as raised:
            _insert_rule(session, rules, strategy=strategy, multiplier=multiplier)
        session.rollback()

    assert _error(raised)[0] == _ER_CHECK_CONSTRAINT_VIOLATED


def test_a_markup_rule_cannot_carry_components(mysql_factory) -> None:
    """§7 "mixed strategies": a component on a MARKUP rule is refused by the composite foreign
    key (or by the component's own CHECK when it claims MARKUP); a FIXED_RATE draft that still
    has components cannot be switched to MARKUP (RESTRICT on update)."""
    rules = Rules(mysql_factory, utc_now)
    with mysql_factory() as session:
        markup = _direct_draft(session, rules)
        for strategy in ("FIXED_RATE", "MARKUP"):
            with pytest.raises(DBAPIError) as raised:
                with session.begin_nested():
                    _insert_component(session, markup, "EMBEDDING_TOKEN", strategy=strategy)
            assert _error(raised)[0] in (_ER_NO_REFERENCED_ROW, _ER_CHECK_CONSTRAINT_VIOLATED)
        with pytest.raises(DBAPIError) as raised:
            with session.begin_nested():
                _insert_component(session, markup, "EMBEDDING_TOKEN", currency="USD")
        assert _error(raised)[0] in (_ER_NO_REFERENCED_ROW, _ER_CHECK_CONSTRAINT_VIOLATED)

        fixed = _direct_draft(session, rules, ("EMBEDDING_TOKEN",))
        with pytest.raises(DBAPIError) as raised:
            with session.begin_nested():
                session.execute(
                    _update_rule("strategy = 'MARKUP', markup_multiplier = 2"), {"id": fixed}
                )
        assert _error(raised)[0] == _ER_ROW_IS_REFERENCED
        session.rollback()


def test_the_currency_check_refuses_anything_but_myr(mysql_factory) -> None:
    rules = Rules(mysql_factory, utc_now)
    with mysql_factory() as session:
        fixed = _direct_draft(session, rules, ())
        with pytest.raises(DBAPIError) as raised:
            _insert_component(session, fixed, "EMBEDDING_TOKEN", currency="USD")
        session.rollback()

    assert _error(raised)[0] == _ER_CHECK_CONSTRAINT_VIOLATED


def test_the_database_checks_completeness_on_the_publish_transition(mysql_factory) -> None:
    """§7 "completeness enforced by the database": zero components and LLM_TOKEN missing one are
    refused on a direct status update, and so is inserting a published rule; a complete FIXED_RATE
    rule and a MARKUP rule go through (the trigger does not refuse the good ones)."""
    rules = Rules(mysql_factory, utc_now)
    llm_three = ("LLM_INPUT_TOKEN", "LLM_OUTPUT_TOKEN", "LLM_CACHE_WRITE_TOKEN")
    with mysql_factory() as session:
        empty = _direct_draft(session, rules, (), scope="GLOBAL")
        short = _direct_draft(session, rules, llm_three, scope="CUSTOMER")
        for rule_id, message in ((empty, _NO_COMPONENTS), (short, _MISSING_COMPONENT)):
            with pytest.raises(DBAPIError) as raised:
                with session.begin_nested():
                    _publish_directly(session, rules, rule_id, (None, None))
            assert _error(raised) == (_ER_SIGNAL_EXCEPTION, message)

        with pytest.raises(DBAPIError) as raised:
            with session.begin_nested():
                _insert_rule(session, rules, status="PUBLISHED")
        assert _error(raised) == (_ER_SIGNAL_EXCEPTION, _STARTS_AS_DRAFT)

        complete = _direct_draft(
            session, rules, (*llm_three, "LLM_CACHE_READ_TOKEN"), scope="CUSTOMER_PROVIDER"
        )
        _publish_directly(session, rules, complete, (None, None))
        markup = _direct_draft(session, rules, scope="GLOBAL_PROVIDER_MODEL")
        _publish_directly(session, rules, markup, (None, None))
        session.rollback()


def test_the_database_refuses_a_draft_going_straight_to_retired(mysql_factory) -> None:
    """Design §4: a draft only becomes a draft, published or discarded — a direct DRAFT → RETIRED
    would skip the completeness check on the publish transition."""
    rules = Rules(mysql_factory, utc_now)
    with mysql_factory() as session:
        for codes in ((), tuple(FICTIONAL_TOKEN_RATES)):
            draft = _direct_draft(session, rules, codes)
            with pytest.raises(DBAPIError) as raised:
                with session.begin_nested():
                    _publish_directly(session, rules, draft, (None, T0), status="RETIRED")
            assert _error(raised) == (_ER_SIGNAL_EXCEPTION, _DRAFT_TRANSITION)
        session.rollback()


def test_two_open_global_rules_are_refused(mysql_factory) -> None:
    """§7 "unique constraint", global scope included: the second untruncated published rule of
    the same scope is refused (the overlap trigger fires before the unique index; either is a
    refusal)."""
    rules = Rules(mysql_factory, utc_now)
    with mysql_factory() as session:
        first = _direct_draft(session, rules)
        _publish_directly(session, rules, first, (None, None))
        second = _direct_draft(session, rules)
        with pytest.raises(DBAPIError) as raised:
            with session.begin_nested():
                _publish_directly(session, rules, second, (T0 + DAY, None))
        assert _error(raised)[0] in (_ER_SIGNAL_EXCEPTION, _ER_DUP_ENTRY)
        session.rollback()


@pytest.mark.parametrize("scope", ["GLOBAL", "CUSTOMER_PROVIDER_MODEL"])
@pytest.mark.parametrize(
    ("start", "end", "accepted"),
    [
        (T0 + 5 * SECOND, T0 + 15 * SECOND, False),
        (T0 + 2 * SECOND, T0 + 3 * SECOND, False),
        (T0, T0 + SECOND, False),
        (None, T0 + SECOND, False),
        (T0 - DAY, None, False),
        (T0 + 10 * SECOND, T0 + 20 * SECOND, True),
        (None, T0, True),
        (T0 + 10 * SECOND, None, True),
    ],
    ids=[
        "partial",
        "contained",
        "same-start",
        "from-the-beginning",
        "open-over-it",
        "end-to-end-after",
        "end-to-end-before",
        "open-after",
    ],
)
def test_the_database_refuses_overlapping_periods(
    mysql_factory, scope: str, start, end, accepted: bool
) -> None:
    """§7 "no overlapping periods": a direct write that intersects a **truncated** rule of the
    same scope is refused; end-to-end periods pass. The global scope checks that the trigger
    compares NULL scope columns as one scope."""
    rules = Rules(mysql_factory, utc_now)
    with mysql_factory() as session:
        existing = _direct_draft(session, rules, scope=scope)
        _publish_directly(session, rules, existing, (T0, T0 + 10 * SECOND))
        candidate = _direct_draft(session, rules, scope=scope)
        if accepted:
            _publish_directly(session, rules, candidate, (start, end))
        else:
            with pytest.raises(DBAPIError) as raised:
                with session.begin_nested():
                    _publish_directly(session, rules, candidate, (start, end))
            assert _error(raised) == (_ER_SIGNAL_EXCEPTION, _OVERLAP)
        session.rollback()


def test_empty_periods_and_other_scopes_do_not_overlap(mysql_factory) -> None:
    """An empty period (a withdrawn reservation) takes no part; other scopes, and the same level
    for another tenant, are unrelated."""
    rules = Rules(mysql_factory, utc_now, tenants=("tenant-a", "tenant-b"))
    tenant_b, _, _ = rules.ids("tenant-b")
    with mysql_factory() as session:
        empty = _direct_draft(session, rules)
        at = T0 + 5 * SECOND
        _publish_directly(session, rules, empty, (at, None))
        session.execute(
            _update_rule("status = 'RETIRED', effective_to = effective_from"), {"id": empty}
        )
        existing = _direct_draft(session, rules)
        _publish_directly(session, rules, existing, (T0, T0 + 10 * SECOND))
        for scope in ("GLOBAL_PROVIDER_MODEL", "CUSTOMER"):
            other = _direct_draft(session, rules, scope=scope)
            _publish_directly(session, rules, other, (T0, None))
        other_tenant = _direct_draft(session, rules, scope="CUSTOMER")
        move = {"tenant": tenant_b, "id": other_tenant}
        session.execute(_update_rule("tenant_id = :tenant"), move)
        _publish_directly(session, rules, other_tenant, (T0, None))
        session.rollback()


def test_the_service_paths_pass_the_triggers(mysql_factory) -> None:
    """Publishing, truncating, retiring, publishing after a retirement, reserving and withdrawing
    (with and without a rule to restore) all pass the real triggers."""
    clock = Clock(utc_now())
    rules = Rules(mysql_factory, clock)
    first = rules.publish_new(GLOBAL)
    clock.advance(seconds=3)
    second = rules.publish_new(GLOBAL, strategy=PricingStrategy.FIXED_RATE)
    clock.advance(seconds=3)
    rules.retire(second.id)
    clock.advance(seconds=3)
    third = rules.publish_new(GLOBAL)
    fourth = rules.publish_new(GLOBAL, clock.now.replace(microsecond=0) + DAY)
    lone = rules.publish_new(CUSTOMER, clock.now.replace(microsecond=0) + DAY)
    restored = rules.publish_new(GLOBAL_MODEL)
    truncating = rules.publish_new(GLOBAL_MODEL, clock.now.replace(microsecond=0) + DAY)
    clock.advance(seconds=3)
    rules.retire(lone.id)
    rules.retire(truncating.id)

    for scope in (GLOBAL, CUSTOMER, GLOBAL_MODEL):
        with mysql_factory() as session:
            rule = session.execute(
                select(PricingRule).where(PricingRule.priority_scope == scope).limit(1)
            ).scalar_one()
            verify_periods(rule_repository.period_rules(session, rule_repository.scope_of(rule)))
            session.rollback()
    assert rules.rule(first.id).effective_to == rules.rule(second.id).effective_from
    assert rules.rule(third.id).effective_to == rules.rule(fourth.id).effective_from
    withdrawn = rules.rule(lone.id)
    assert withdrawn.effective_from == withdrawn.effective_to
    assert rules.rule(restored.id).effective_to is None


@pytest.mark.parametrize(
    ("status", "start", "end", "approved"),
    [
        ("DRAFT", T0, None, False),
        ("DRAFT", None, None, True),
        ("DISCARDED", None, None, True),
        ("PUBLISHED", None, None, False),
    ],
    ids=["draft-with-start", "draft-with-approver", "discarded-with-approver", "no-approver"],
)
def test_the_checks_tie_the_period_and_the_approver_to_the_status(
    mysql_factory, status: str, start, end, approved: bool
) -> None:
    rules = Rules(mysql_factory, utc_now)
    with mysql_factory() as session:
        draft = _direct_draft(session, rules)
        session.commit()
    statement = text(
        "UPDATE pricing_rules SET status = :status, effective_from = :start,"
        " effective_to = :end, approved_by = :approver, approved_at = :approved_at WHERE id = :id"
    )
    params = {
        "status": status,
        "start": start,
        "end": end,
        "approver": rules.admin.id if approved else None,
        "approved_at": T0 if approved else None,
        "id": draft,
    }

    errno, _ = _refused(mysql_factory, statement, **params)

    assert errno == _ER_CHECK_CONSTRAINT_VIOLATED

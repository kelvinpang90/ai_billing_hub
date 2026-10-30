"""`resolve_pricing_rule` and its serialisation with publishing (design gate #178 v4 §7).

Three groups:

- **resolution** (SQLite, frozen clock): the five levels in §16 order, falling through as each
  level is retired; a hit stops the search even when the FIXED_RATE rule lacks the event's
  component; tenants never see each other's rules; drafts never match; components come back with
  their quantity field; every read is a locking read; the eight levels of design gate #507 v4
  (the three project levels first; without project rules nothing changes; one project's rules
  never price another project's or another tenant's events; no project, no project levels); one
  statement reads `pricing_rules` whichever level hits, and two overlapping rules on the chosen
  level raise while those on a lower level do not (design gate #528 v1);
- **time stability** (SQLite, property test): random publish / reserve / retire / withdraw
  sequences over the five levels — once a moment resolved to rule R it resolves to R forever;
- **serialised with publishing** (real MySQL, several connections): billing waits for an
  uncommitted publish and the other way round; two billing runs share the lock without blocking;
  the snapshot-read trap; the eight levels falling through.

The MySQL half needs `BILLING_TEST_DATABASE_URL` pointing at a database that may be wiped; it
skips without it — **a skip is not a pass**, CI sets it and treats a skip as a failure. Prices
and multipliers are fictional.
"""

from __future__ import annotations

import datetime as dt
import inspect
import os
import random
import re
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import pytest
from alembic.config import Config
from sqlalchemy import Engine, create_engine, delete, event, func, select, update
from sqlalchemy.dialects import mysql
from sqlalchemy.exc import MultipleResultsFound
from sqlalchemy.orm import Session, sessionmaker

from alembic import command
from app.core.database import create_session_factory
from app.models.ai_catalog import (
    SEED_METER_TYPES,
    AiModel,
    AiModelAlias,
    AiProvider,
    UsageMeterComponent,
    UsageMeterType,
)
from app.models.auth import User, UserRole, UserStatus
from app.models.base import Base
from app.models.pricing_rules import (
    SCOPE_ORDER,
    PricingRule,
    PricingRuleLock,
    PricingRuleStatus,
    PricingScope,
    PricingStrategy,
)
from app.models.tenancy import Project, Tenant
from app.repositories import pricing_rules as rule_repository
from app.repositories.ai_catalog import resolve_model
from app.repositories.pricing_rules import (
    ResolvedPricingRule,
    lock_pricing_rules_shared,
    resolve_pricing_rule,
)
from app.schemas.ai_catalog import ModelView
from app.schemas.pricing_rules import PricingRuleView, RuleComponentInput
from app.services import ai_catalog, pricing_rules
from app.services.auth import RequestContext, utc_now
from app.services.pricing_rules import (
    EffectiveFromConflict,
    EffectiveFromInPast,
    PricingRuleFinal,
    PricingRuleNotRetirable,
)

TEST_DATABASE_URL = os.environ.get("BILLING_TEST_DATABASE_URL", "")
TEST_EMAIL_DOMAIN = "@pricing-rules-resolve-test.example.com"
CONTEXT = RequestContext(ip_address="203.0.113.9", user_agent="pricing-rules-test")

T0 = dt.datetime(2026, 9, 29, 8, 30, 0)
LONG_AGO = dt.datetime(2000, 1, 1)
SECOND = dt.timedelta(seconds=1)
DAY = dt.timedelta(days=1)

# Fictional MYR tax-inclusive prices: per 1 000 000 tokens, and per audio second.
FICTIONAL_TOKEN_RATES = {
    "LLM_INPUT_TOKEN": "1.11111111",
    "LLM_OUTPUT_TOKEN": "2.22222222",
    "LLM_CACHE_WRITE_TOKEN": "3.33333333",
    "LLM_CACHE_READ_TOKEN": "4.44444444",
}
FICTIONAL_AUDIO_RATE = "0.05555555"
FICTIONAL_MULTIPLIER = "1.23456789"

GLOBAL = PricingScope.GLOBAL
CUSTOMER = PricingScope.CUSTOMER
PROJECT = PricingScope.PROJECT
PROJECT_LEVELS = tuple(level for level in SCOPE_ORDER if level.has_project)
# The five levels without a project (§16 before design gate #507 v4): the cases that predate the
# project levels go level by level over these; the eight-level cases are further below.
BUILDABLE_LEVELS = tuple(level for level in SCOPE_ORDER if not level.has_project)


# --- helpers ----------------------------------------------------------------------


class Clock:
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
    return [component(code, rate) for code, rate in FICTIONAL_TOKEN_RATES.items()]


def make_admin(factory: sessionmaker[Session]) -> User:
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


def make_project(factory: sessionmaker[Session], tenant_public_id: str, name: str) -> str:
    """A project of the tenant, written directly; returns its public id."""
    public_id = str(uuid.uuid4())
    with factory() as session:
        tenant_id = session.execute(
            select(Tenant.id).where(Tenant.public_id == tenant_public_id)
        ).scalar_one()
        session.add(
            Project(
                public_id=public_id,
                tenant_id=tenant_id,
                name=f"Fictional {name}",
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
    """One admin, one provider with one model and some tenants, driven through the services."""

    def __init__(
        self,
        factory: sessionmaker[Session],
        clock: Callable[[], dt.datetime],
        *,
        provider_code: str = "anthropic",
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
        self.model: ModelView = ai_catalog.create_model(
            factory,
            actor=self.admin,
            provider_id=self.provider.id,
            code="model-m",
            display_name="model-m",
            context=CONTEXT,
            clock=clock,
        )
        self.tenants = {name: make_tenant(factory, f"{provider_code}-{name}") for name in tenants}
        # Project name → its public id; each project belongs to one of `tenants` (`add_project`).
        self.projects: dict[str, str] = {}

    def add_project(self, name: str, tenant: str = "tenant-a") -> str:
        self.projects[name] = make_project(self.factory, self.tenants[tenant], name)
        return self.projects[name]

    def project_id(self, name: str) -> int:
        """The project's internal id."""
        statement = select(Project.id).where(Project.public_id == self.projects[name])
        with self.factory() as session:
            return int(session.execute(statement).scalar_one())

    def draft(
        self,
        scope: PricingScope = GLOBAL,
        *,
        tenant: str = "tenant-a",
        project: str | None = None,
        components: list[RuleComponentInput] | None = None,
    ) -> PricingRuleView:
        """MARKUP unless `components` is given (then FIXED_RATE). A project level needs
        `project`."""
        fixed = components is not None
        return pricing_rules.create_draft(
            self.factory,
            actor=self.admin,
            priority_scope=scope,
            customer_id=self.tenants[tenant] if scope.has_customer else None,
            project_id=self.projects[project] if scope.has_project and project else None,
            provider_id=self.provider.id if scope.has_provider else None,
            model_id=self.model.id if scope.has_model else None,
            strategy=PricingStrategy.FIXED_RATE if fixed else PricingStrategy.MARKUP,
            markup_multiplier=None if fixed else Decimal(FICTIONAL_MULTIPLIER),
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
        *,
        tenant: str = "tenant-a",
        project: str | None = None,
        components: list[RuleComponentInput] | None = None,
    ) -> PricingRuleView:
        draft = self.draft(scope, tenant=tenant, project=project, components=components)
        return self.publish(draft.id, effective_from)

    def retire(self, rule_id: str) -> PricingRuleView:
        return pricing_rules.retire(
            self.factory,
            actor=self.admin,
            rule_id=rule_id,
            reason="Fictional retirement",
            context=CONTEXT,
            clock=self.clock,
        )

    def tenant_id(self, tenant: str = "tenant-a") -> int:
        statement = select(Tenant.id).where(Tenant.public_id == self.tenants[tenant])
        with self.factory() as session:
            return int(session.execute(statement).scalar_one())

    def ids(self, tenant: str = "tenant-a") -> tuple[int, int, int]:
        """(tenant id, provider id, model id), all internal."""
        statement = select(AiModel.provider_id, AiModel.id).where(
            AiModel.public_id == self.model.id
        )
        with self.factory() as session:
            provider_id, model_id = session.execute(statement).one()
        return self.tenant_id(tenant), int(provider_id), int(model_id)

    def resolve(
        self, occurred_at: dt.datetime, tenant: str = "tenant-a", project: str | None = None
    ) -> str | None:
        """What one billing run sees (T-H order), in a transaction of its own. `project`: the
        event's project by name; `None` resolves as a preview without a project."""
        project_id = self.project_id(project) if project is not None else None
        return billed(
            self.factory, self.tenant_id(tenant), self.code, occurred_at, project_id=project_id
        )


def billed(
    factory: sessionmaker[Session],
    tenant_id: int,
    provider_code: str,
    occurred_at: dt.datetime,
    *,
    project_id: int | None = None,
) -> str | None:
    """`resolve_model`, the shared rule lock, `resolve_pricing_rule`, then commit (T-H)."""
    with factory() as session:
        found = _rule_of(session, tenant_id, provider_code, occurred_at, project_id=project_id)
        session.commit()
        return None if found is None else found.public_id


def _rule_of(
    session: Session,
    tenant_id: int,
    provider_code: str,
    occurred_at: dt.datetime,
    *,
    project_id: int | None = None,
) -> ResolvedPricingRule | None:
    model = resolve_model(session, provider_code, "model-m", occurred_at)
    assert model is not None
    lock_pricing_rules_shared(session)
    return resolve_pricing_rule(
        session, tenant_id, project_id, model.provider_id, model.id, occurred_at
    )


# --- fixtures ---------------------------------------------------------------------


@pytest.fixture
def sqlite_factory() -> Iterator[sessionmaker[Session]]:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    seed_meter_types(factory)
    yield factory
    engine.dispose()


@pytest.fixture
def mysql_factory(monkeypatch) -> Iterator[sessionmaker[Session]]:
    if not TEST_DATABASE_URL:
        pytest.skip("BILLING_TEST_DATABASE_URL is not set; the MySQL half needs a real MySQL")
    from app.core.config import get_settings

    monkeypatch.setenv("BILLING_DATABASE_URL", TEST_DATABASE_URL)
    get_settings.cache_clear()
    command.upgrade(Config("alembic.ini"), "head")
    get_settings.cache_clear()

    engine = create_engine(TEST_DATABASE_URL, pool_size=6, max_overflow=2)
    _clean(engine)
    try:
        yield create_session_factory(engine)
    finally:
        _clean(engine)
        engine.dispose()


def _clean(engine: Engine) -> None:
    """⚠️ The database is shared: wipe before and after (as test_pricing_rules_service.py)."""
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
        mine = select(Tenant.id).where(Tenant.email.like(f"%{TEST_EMAIL_DOMAIN}"))
        connection.execute(delete(Project).where(Project.tenant_id.in_(mine)))
        connection.execute(delete(Tenant).where(Tenant.email.like(f"%{TEST_EMAIL_DOMAIN}")))
        connection.execute(delete(User).where(User.email.like(f"%{TEST_EMAIL_DOMAIN}")))


# --- resolution -------------------------------------------------------------------


def _one_rule_per_level(rules: Rules) -> dict[PricingScope, str]:
    """A published MARKUP rule on each of the five non-project levels for tenant-a; the global
    one first, so that it applies to all time."""
    published = {GLOBAL: rules.publish_new(GLOBAL).id}
    for level in BUILDABLE_LEVELS[:-1]:
        published[level] = rules.publish_new(level).id
    return published


def test_the_levels_fall_through_in_section_16_order(sqlite_factory) -> None:
    """§7 "five levels falling through": retire the levels from the highest down; the same event
    resolves to each next level in turn, and to None when all are gone."""
    clock = Clock(T0)
    rules = Rules(sqlite_factory, clock)
    published = _one_rule_per_level(rules)
    clock.advance(seconds=10)
    # One and the same event, later than every boundary the retirements below create.
    occurred_at = T0 + 10 * DAY

    seen = []
    for level in BUILDABLE_LEVELS:
        seen.append(rules.resolve(occurred_at))
        rules.retire(published[level])
        clock.advance(seconds=5)
    seen.append(rules.resolve(occurred_at))

    assert seen == [published[level] for level in BUILDABLE_LEVELS] + [None]


def test_a_hit_stops_even_without_the_events_component(sqlite_factory) -> None:
    """§7 "a hit stops": a customer FIXED_RATE rule that only prices AUDIO_SECOND still wins for
    an LLM_TOKEN event; it does not fall through to the global default (T-G then reports
    PRICING_ERROR)."""
    clock = Clock(T0)
    rules = Rules(sqlite_factory, clock)
    rules.publish_new(GLOBAL, components=token_components())
    audio = [component("AUDIO_SECOND", FICTIONAL_AUDIO_RATE, "1")]
    customer = rules.publish_new(CUSTOMER, components=audio)
    tenant_id, provider_id, model_id = rules.ids()

    with sqlite_factory() as session:
        found = resolve_pricing_rule(session, tenant_id, None, provider_id, model_id, T0 + DAY)

    assert found is not None
    assert found.public_id == customer.id
    assert found.priority_scope is CUSTOMER
    assert [item.component_code for item in found.components] == ["AUDIO_SECOND"]
    assert "input_tokens" not in {item.quantity_field for item in found.components}


def test_one_tenants_rule_never_prices_another_tenants_event(sqlite_factory) -> None:
    """§7 "tenant isolation" (INV-8)."""
    rules = Rules(sqlite_factory, Clock(T0), tenants=("tenant-a", "tenant-b"))
    mine = rules.publish_new(CUSTOMER, tenant="tenant-a")

    assert rules.resolve(T0 + DAY, "tenant-a") == mine.id
    assert rules.resolve(T0 + DAY, "tenant-b") is None
    default = rules.publish_new(GLOBAL)
    assert rules.resolve(T0 + DAY, "tenant-b") == default.id
    assert rules.resolve(T0 + DAY, "tenant-a") == mine.id


# --- the eight levels (design gate #507 v4 §2「解析」, §7) ----------------------------------


def _one_rule_per_eight_levels(rules: Rules, project: str) -> dict[PricingScope, str]:
    """A published MARKUP rule on each of the eight levels for tenant-a and `project`; the global
    one first, so that it applies to all time."""
    published = {GLOBAL: rules.publish_new(GLOBAL).id}
    for level in SCOPE_ORDER[:-1]:
        published[level] = rules.publish_new(level, project=project).id
    return published


def _falls_through_eight_levels(
    factory: sessionmaker[Session],
    rules: Rules,
    occurred_at: dt.datetime,
    published: dict[PricingScope, str],
    advance: Callable[[], None],
) -> None:
    """Retire the highest level each time; the event resolves to each next level, then None."""
    seen = []
    for level in SCOPE_ORDER:
        seen.append(rules.resolve(occurred_at, project="p1"))
        rules.retire(published[level])
        advance()
    seen.append(rules.resolve(occurred_at, project="p1"))

    assert seen == [published[level] for level in SCOPE_ORDER] + [None]
    by_level = {
        rule.public_id: PricingScope(rule.priority_scope)
        for rule in _rules(factory, published.values())
    }
    assert [by_level[rule_id] for rule_id in seen[:-1]] == list(SCOPE_ORDER)


def test_the_eight_levels_fall_through_in_order(sqlite_factory) -> None:
    """§7 "eight levels": one rule on each level for the event; retiring the highest level each
    time, the event resolves to the next one, in exactly the eight-level order."""
    clock = Clock(T0)
    rules = Rules(sqlite_factory, clock)
    rules.add_project("p1")
    published = _one_rule_per_eight_levels(rules, "p1")
    clock.advance(seconds=10)

    _falls_through_eight_levels(
        sqlite_factory, rules, T0 + 10 * DAY, published, lambda: clock.advance(seconds=5)
    )


def test_the_eight_levels_fall_through_in_order_on_mysql(mysql_factory) -> None:
    """Design gate #528 v1 §7: the same flow on MySQL, where the OR of the levels' `IS NULL`
    conditions and `FOR SHARE` really run. Every retirement ends at its `t`, well before the
    event a day ahead."""
    rules = Rules(mysql_factory, utc_now)
    rules.add_project("p1")
    published = _one_rule_per_eight_levels(rules, "p1")

    _falls_through_eight_levels(mysql_factory, rules, utc_now() + DAY, published, lambda: None)


def test_without_project_rules_the_project_changes_nothing(sqlite_factory) -> None:
    """§7 "no project rules, no change": with only the five older levels, an event of any project
    (or no project at all) resolves exactly as before, at every step of the fall-through."""
    clock = Clock(T0)
    rules = Rules(sqlite_factory, clock)
    for name in ("p1", "p2"):
        rules.add_project(name)
    published = _one_rule_per_level(rules)
    clock.advance(seconds=10)
    occurred_at = T0 + 10 * DAY

    for level in BUILDABLE_LEVELS:
        expected = rules.resolve(occurred_at)
        assert expected == published[level]
        assert rules.resolve(occurred_at, project="p1") == expected
        assert rules.resolve(occurred_at, project="p2") == expected
        rules.retire(published[level])
        clock.advance(seconds=5)
    for project in (None, "p1", "p2"):
        assert rules.resolve(occurred_at, project=project) is None


def test_one_projects_rule_never_prices_another_projects_event(sqlite_factory) -> None:
    """§7 "project isolation": P1's rules on all three project levels; the same customer's P2
    event resolves to the customer rule, never to P1's."""
    rules = Rules(sqlite_factory, Clock(T0))
    for name in ("p1", "p2"):
        rules.add_project(name)
    customer = rules.publish_new(CUSTOMER)
    mine = {level: rules.publish_new(level, project="p1").id for level in PROJECT_LEVELS}

    assert rules.resolve(T0 + DAY, project="p1") == mine[PricingScope.PROJECT_PROVIDER_MODEL]
    assert rules.resolve(T0 + DAY, project="p2") == customer.id
    assert rules.resolve(T0 + DAY) == customer.id


def test_a_project_rule_never_prices_another_tenants_event(sqlite_factory) -> None:
    """§7 "across tenants" (INV-8): tenant A's project rules never price tenant B's events — not
    for B's own project, and not even when B's event claimed A's project id (SQLite enforces no
    foreign keys, so only the tenant comparison of the project-level query stops it)."""
    rules = Rules(sqlite_factory, Clock(T0), tenants=("tenant-a", "tenant-b"))
    rules.add_project("p1", tenant="tenant-a")
    rules.add_project("q1", tenant="tenant-b")
    for level in PROJECT_LEVELS:
        rules.publish_new(level, tenant="tenant-a", project="p1")
    tenant_b, provider_id, model_id = rules.ids("tenant-b")
    project_a = rules.project_id("p1")

    assert rules.resolve(T0 + DAY, "tenant-b", "q1") is None
    with sqlite_factory() as session:
        forged = resolve_pricing_rule(session, tenant_b, project_a, provider_id, model_id, T0 + DAY)
    assert forged is None
    assert rules.resolve(T0 + DAY, "tenant-a", "p1") is not None
    default = rules.publish_new(GLOBAL)
    assert rules.resolve(T0 + DAY, "tenant-b", "q1") == default.id


def _rule_queries(
    factory: sessionmaker[Session], project_id: int | None
) -> tuple[ResolvedPricingRule | None, int]:
    """Resolve once for tenant-a; return the result and how many queries read `pricing_rules`."""
    engine = factory.kw["bind"]
    statements: list[str] = []

    def record(_conn, _cursor, statement, _parameters, _context, _executemany) -> None:
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        with factory() as session:
            tenant_id = session.execute(select(Tenant.id)).scalar_one()
            provider_id, model_id = session.execute(select(AiModel.provider_id, AiModel.id)).one()
            statements.clear()
            found = resolve_pricing_rule(
                session, tenant_id, project_id, provider_id, model_id, T0 + DAY
            )
    finally:
        event.remove(engine, "before_cursor_execute", record)
    reads = [item for item in statements if re.search(r"\bFROM pricing_rules\b", item)]
    return found, len(reads)


def test_without_a_project_the_project_levels_are_not_queried(sqlite_factory) -> None:
    """Design §2「解析」: `project_id=None` (a preview without a project) skips the three project
    levels — their rules are invisible. Design gate #528 v1 §7: whichever level hits, or none,
    one statement reads `pricing_rules` (it was 5, 1 and 8 when each level had its own)."""
    rules = Rules(sqlite_factory, Clock(T0))
    rules.add_project("p1")
    for level in PROJECT_LEVELS:
        rules.publish_new(level, project="p1")

    found, queried = _rule_queries(sqlite_factory, None)
    assert (found, queried) == (None, 1)
    found, queried = _rule_queries(sqlite_factory, rules.project_id("p1"))
    assert found is not None
    assert found.priority_scope is PricingScope.PROJECT_PROVIDER_MODEL
    assert queried == 1
    rules.add_project("p2")
    found, queried = _rule_queries(sqlite_factory, rules.project_id("p2"))
    assert (found, queried) == (None, 1)


def _corrupt_overlap(rules: Rules, level: PricingScope) -> tuple[str, str]:
    """Two drafts of `level` forced to PUBLISHED with overlapping closed periods, bypassing the
    services: [T0, T0 + 3 days) and [T0 + 1 day, T0 + 4 days). SQLite has no period trigger, and
    a row with `effective_to` takes no `open_slot`, so nothing stops it."""
    periods = ((T0, T0 + 3 * DAY), (T0 + DAY, T0 + 4 * DAY))
    forced = [rules.draft(level).id for _ in periods]
    with rules.factory() as session:
        for public_id, (start, end) in zip(forced, periods, strict=True):
            session.execute(
                update(PricingRule)
                .where(PricingRule.public_id == public_id)
                .values(
                    status=PricingRuleStatus.PUBLISHED,
                    effective_from=start,
                    effective_to=end,
                    approved_by=rules.admin.id,
                    approved_at=T0,
                )
            )
        session.commit()
    return forced[0], forced[1]


def test_two_overlapping_rules_on_the_chosen_level_raise(sqlite_factory) -> None:
    """Design gate #528 v1 §7 "data corruption": the chosen level has two rules in effect at the
    moment → `MultipleResultsFound`, as when each level had its own query; outside the overlap the
    level has one rule and resolves normally."""
    rules = Rules(sqlite_factory, Clock(T0))
    first, second = _corrupt_overlap(rules, CUSTOMER)

    with pytest.raises(MultipleResultsFound):
        rules.resolve(T0 + 2 * DAY)
    assert rules.resolve(T0 + DAY / 2) == first
    assert rules.resolve(T0 + 3 * DAY + DAY / 2) == second


def test_overlapping_rules_below_the_chosen_level_do_not_raise(sqlite_factory) -> None:
    """Design gate #528 v1 §7 "data corruption", the other group: a normal rule on the first level
    and two overlapping rules on a lower one → the first level's rule, no error (the lower level
    was never queried before). Without a project the lower level is the chosen one and raises."""
    rules = Rules(sqlite_factory, Clock(T0))
    rules.add_project("p1")
    top = rules.publish_new(PricingScope.PROJECT_PROVIDER_MODEL, project="p1")
    _corrupt_overlap(rules, CUSTOMER)

    assert rules.resolve(T0 + 2 * DAY, project="p1") == top.id
    with pytest.raises(MultipleResultsFound):
        rules.resolve(T0 + 2 * DAY)


def test_drafts_discarded_drafts_and_withdrawn_reservations_never_match(sqlite_factory) -> None:
    rules = Rules(sqlite_factory, Clock(T0))
    rules.draft(GLOBAL)
    pricing_rules.discard_draft(
        sqlite_factory, actor=rules.admin, rule_id=rules.draft(CUSTOMER).id, context=CONTEXT
    )
    reserved = rules.publish_new(GLOBAL, T0 + DAY)
    rules.retire(reserved.id)

    for moment in (LONG_AGO, T0, T0 + DAY, T0 + 2 * DAY):
        assert rules.resolve(moment) is None


def test_the_rule_comes_with_its_components_or_its_multiplier(sqlite_factory) -> None:
    """T-G contract: FIXED_RATE components carry `component_code`, `quantity_field`, both exact
    amounts and MYR; a MARKUP rule carries its multiplier and no components."""
    clock = Clock(T0)
    rules = Rules(sqlite_factory, clock)
    fixed = rules.publish_new(GLOBAL, components=token_components())
    markup = rules.publish_new(CUSTOMER)
    tenant_id, provider_id, model_id = rules.ids()

    with sqlite_factory() as session:
        old = resolve_pricing_rule(session, tenant_id, None, provider_id, model_id, T0)
        new = resolve_pricing_rule(session, tenant_id, None, provider_id, model_id, T0 + DAY)

    assert old is not None and new is not None
    assert (old.public_id, old.strategy, old.markup_multiplier) == (
        fixed.id,
        PricingStrategy.FIXED_RATE,
        None,
    )
    million = Decimal("1000000")
    priced = [
        (item.component_code, item.quantity_field, item.unit_quantity, item.rate_amount)
        for item in old.components
    ]
    assert priced == [
        ("LLM_CACHE_READ_TOKEN", "cache_read_input_tokens", million, Decimal("4.44444444")),
        ("LLM_CACHE_WRITE_TOKEN", "cache_creation_input_tokens", million, Decimal("3.33333333")),
        ("LLM_INPUT_TOKEN", "input_tokens", million, Decimal("1.11111111")),
        ("LLM_OUTPUT_TOKEN", "output_tokens", million, Decimal("2.22222222")),
    ]
    assert {item.currency for item in old.components} == {"MYR"}
    assert (new.public_id, new.strategy, new.components) == (
        markup.id,
        PricingStrategy.MARKUP,
        (),
    )
    assert new.markup_multiplier == Decimal(FICTIONAL_MULTIPLIER)
    assert isinstance(new.markup_multiplier, Decimal)
    assert (new.tenant_id, new.provider_id, new.model_id) == (tenant_id, None, None)
    by_public_id = select(PricingRule.id).where(PricingRule.public_id == markup.id)
    with sqlite_factory() as session:
        assert new.rule_id == session.execute(by_public_id).scalar_one()


def test_resolution_takes_share_locks() -> None:
    """Design §2: every read is a locking read (`FOR SHARE`) that refreshes loaded objects."""
    shared = rule_repository._shared(select(PricingRule))
    compiled = str(shared.compile(dialect=mysql.dialect()))
    assert "LOCK IN SHARE MODE" in compiled or "FOR SHARE" in compiled
    assert "with_for_update(read=True)" in inspect.getsource(rule_repository._shared)
    # The one statement over the levels, the components once: both through `_shared`.
    assert "_shared(" in inspect.getsource(resolve_pricing_rule)
    assert "_shared(" in inspect.getsource(rule_repository._resolved)
    assert "_shared(" in inspect.getsource(lock_pricing_rules_shared)
    assert ".with_for_update()" in inspect.getsource(rule_repository.lock_pricing_rules)
    assert ".with_for_update()" in inspect.getsource(rule_repository.period_rules)
    # The eight levels (design gate #507 v4), highest first: the project levels before §16's five.
    assert [level.value for level in SCOPE_ORDER] == [
        "PROJECT_PROVIDER_MODEL",
        "PROJECT_PROVIDER",
        "PROJECT",
        "CUSTOMER_PROVIDER_MODEL",
        "CUSTOMER_PROVIDER",
        "CUSTOMER",
        "GLOBAL_PROVIDER_MODEL",
        "GLOBAL",
    ]


def test_the_shared_lock_reads_the_one_lock_row(sqlite_factory) -> None:
    with sqlite_factory() as session:
        assert lock_pricing_rules_shared(session).id == 1
        assert session.execute(select(func.count()).select_from(PricingRuleLock)).scalar_one() == 1


# --- time stability (property test, design §7) -------------------------------------------


def test_a_resolved_moment_never_changes_its_rule(sqlite_factory) -> None:
    """§7 "time stability": random publish / reserve / retire / withdraw steps over all five
    levels (the clock moves by random amounts, including not at all and less than a second).
    After each step every fixed moment not later than now is resolved; once a moment resolved to
    rule R it must resolve to R at every later step (a moment without a rule may gain one)."""
    rng = random.Random(20260929)
    clock = Clock(T0 - 3 * SECOND)
    rules = Rules(sqlite_factory, clock)
    tenant_id, provider_id, model_id = rules.ids()
    probes = [T0 + dt.timedelta(milliseconds=500 * k) for k in range(-6, 240)]
    steps = [0, 1, 250_000, 999_999, 1_000_000, 1_500_000, 7_000_000]
    published: dict[PricingScope, list[str]] = {level: [] for level in BUILDABLE_LEVELS}
    seen: dict[dt.datetime, str] = {}
    changes = 0

    for step in range(200):
        clock.advance(microseconds=rng.choice(steps))
        level = rng.choice(BUILDABLE_LEVELS)
        operation = rng.choice(["publish", "publish", "reserve", "retire"])
        try:
            if operation == "publish":
                published[level].append(rules.publish_new(level).id)
            elif operation == "reserve":
                ahead = clock.now.replace(microsecond=0) + rng.randint(1, 20) * SECOND
                published[level].append(rules.publish_new(level, ahead).id)
            elif published[level]:
                rules.retire(published[level][-1])
            changes += 1
        except (
            EffectiveFromConflict,
            EffectiveFromInPast,
            PricingRuleFinal,
            PricingRuleNotRetirable,
        ):
            pass
        with sqlite_factory() as session:
            for probe in probes:
                if probe > clock.now:
                    break
                found = resolve_pricing_rule(session, tenant_id, None, provider_id, model_id, probe)
                if probe in seen:
                    assert found is not None, (step, level, operation, probe)
                    assert found.public_id == seen[probe], (step, level, operation, probe)
                elif found is not None:
                    seen[probe] = found.public_id

    # Enough really happened, and moments really resolved — otherwise the asserts idled.
    assert changes >= 60
    assert len(seen) >= 60
    assert len(set(seen.values())) >= 5
    levels = {PricingScope(rule.priority_scope) for rule in _rules(sqlite_factory, seen.values())}
    assert len(levels) >= 3


def _rules(factory: sessionmaker[Session], public_ids) -> list[PricingRule]:
    statement = select(PricingRule).where(PricingRule.public_id.in_(set(public_ids)))
    with factory() as session:
        return list(session.execute(statement).scalars())


# --- serialised with publishing (real MySQL) -------------------------------------------


def _pause_inside_the_publish(monkeypatch) -> tuple[threading.Event, threading.Event]:
    """Make the next rule write stop just before its commit, holding its locks."""
    written, release = threading.Event(), threading.Event()
    real = pricing_rules.record_audit

    def pausing(session: Session, **options: object) -> None:
        real(session, **options)  # type: ignore[arg-type]
        session.flush()
        written.set()
        assert release.wait(timeout=60)

    monkeypatch.setattr(pricing_rules, "record_audit", pausing)
    return written, release


def _started(rule: PricingRuleView) -> PricingRuleView:
    """Wait (wall clock) until `rule` is in effect, and return it.

    Only a `GLOBAL` scope's first rule may start "always"; any other rule starts at `t`, the
    whole second after its publish (design §2). Until then it resolves nothing, and publishing the
    next rule without a time is a 409: `t` would not be later than the tail's start.
    """
    assert rule.effective_from is not None
    deadline = time.monotonic() + 5
    while utc_now() < rule.effective_from:
        assert time.monotonic() < deadline
        time.sleep(0.05)
    return rule


def test_billing_waits_for_an_uncommitted_publish(mysql_factory, monkeypatch) -> None:
    """§7 "publishing and billing concurrently" ①: while a publish has written but not committed,
    a billing run (shared rule lock, `occurred_at ≥ t`) blocks until the commit and then selects
    the new rule."""
    rules = Rules(mysql_factory, utc_now)
    old = _started(rules.publish_new(CUSTOMER))
    draft = rules.draft(CUSTOMER)
    tenant_id = rules.tenant_id()
    written, release = _pause_inside_the_publish(monkeypatch)
    occurred_at = utc_now() + DAY

    with ThreadPoolExecutor(max_workers=2) as pool:
        publishing = pool.submit(rules.publish, draft.id)
        assert written.wait(timeout=30)
        billing = pool.submit(billed, mysql_factory, tenant_id, rules.code, occurred_at)
        with pytest.raises(TimeoutError):
            billing.result(timeout=2)
        release.set()
        new = publishing.result(timeout=30)
        assert billing.result(timeout=30) == new.id

    assert new.id != old.id
    assert new.effective_from is not None
    assert new.effective_from <= occurred_at
    assert rules.resolve(occurred_at) == new.id


def test_a_publish_waits_for_an_uncommitted_billing_run(mysql_factory) -> None:
    """§7 ②: a billing run holds the shared rule lock; the publish blocks until it commits, its
    `t` is later than the event, and resolving the moment again agrees with the billed one."""
    rules = Rules(mysql_factory, utc_now)
    old = _started(rules.publish_new(CUSTOMER))
    draft = rules.draft(CUSTOMER)
    tenant_id = rules.tenant_id()
    billing = mysql_factory()
    try:
        occurred_at = utc_now()
        found = _rule_of(billing, tenant_id, rules.code, occurred_at)
        assert found is not None
        snapshot_id = found.public_id
        with ThreadPoolExecutor(max_workers=1) as pool:
            publishing = pool.submit(rules.publish, draft.id)
            with pytest.raises(TimeoutError):
                publishing.result(timeout=2)
            before_commit = utc_now()
            billing.commit()
            new = publishing.result(timeout=30)
    finally:
        billing.close()

    t = new.effective_from
    assert snapshot_id == old.id
    assert t is not None
    assert t > occurred_at
    assert t > before_commit
    assert rules.resolve(occurred_at) == snapshot_id
    assert rules.resolve(t) == new.id


def test_two_billing_runs_share_the_rule_lock(mysql_factory) -> None:
    """§7 "shared locks do not block each other": two billing transactions hold the shared rule
    lock at once; the second does not wait for the first to commit."""
    rules = Rules(mysql_factory, utc_now)
    published = rules.publish_new(GLOBAL)
    tenant_id = rules.tenant_id()
    first = mysql_factory()
    try:
        assert _rule_of(first, tenant_id, rules.code, utc_now()) is not None
        with ThreadPoolExecutor(max_workers=1) as pool:
            second = pool.submit(billed, mysql_factory, tenant_id, rules.code, utc_now())
            assert second.result(timeout=10) == published.id
        first.commit()
    finally:
        first.close()


def test_rule_resolution_reads_past_the_transaction_snapshot(mysql_factory) -> None:
    """§7 "snapshot-read trap": the billing transaction first does a plain read (which fixes its
    REPEATABLE READ snapshot); another connection publishes and commits; resolution still finds
    the new rule — locking reads see the latest commit, not the snapshot."""
    rules = Rules(mysql_factory, utc_now)
    _started(rules.publish_new(CUSTOMER))
    draft = rules.draft(CUSTOMER)
    tenant_id = rules.tenant_id()
    occurred_at = utc_now() + DAY
    is_published = PricingRule.status == PricingRuleStatus.PUBLISHED
    counted = select(func.count()).select_from(PricingRule).where(is_published)
    billing = mysql_factory()
    try:
        billing.execute(select(func.count()).select_from(User)).scalar_one()
        assert billing.execute(counted).scalar_one() == 1

        new = rules.publish(draft.id)

        # The trap is real: a plain read in the same transaction still sees one published rule.
        assert billing.execute(counted).scalar_one() == 1
        found = _rule_of(billing, tenant_id, rules.code, occurred_at)
        assert found is not None
        assert found.public_id == new.id
        billing.commit()
    finally:
        billing.close()

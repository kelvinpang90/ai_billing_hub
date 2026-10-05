"""The pricing engine (design gate #179 v1 §7): `price_event`, `resolve_charge_inputs`, reprice.

Two groups:

- **unit** (no database): MARKUP and FIXED_RATE against hand-computed values; cache components;
  non-token meter types; an MYR source; rounding exactly once; ROUND_HALF_UP on a 5 in the ninth
  place; the unrounded chain; independence from the process's decimal context; determinism; a
  zero billed amount; overflow; and a static scan of the module's source;
- **integration** (SQLite, services under a frozen clock): the error order and its detail codes,
  no falling through a FIXED_RATE rule that lacks the event's components, aliases, tenants never
  seeing each other's rules, the lock order of `locking=True` / `locking=False`, and
  `reprice_from_snapshot` reproducing the result after newer versions are published.

The preview endpoint is in test_pricing_preview_api.py. Prices, rates and multipliers are
fictional; no uuid literal other than the all-zero placeholder appears.
"""

from __future__ import annotations

import ast
import datetime as dt
import inspect
import pathlib
import uuid
from collections.abc import Callable, Iterator, Mapping
from decimal import ROUND_DOWN, ROUND_HALF_EVEN, ROUND_HALF_UP, Decimal, Inexact, localcontext

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.core.database import create_session_factory
from app.models.ai_catalog import SEED_METER_TYPES, UsageMeterComponent, UsageMeterType
from app.models.auth import User, UserRole, UserStatus
from app.models.base import Base, quantize_money
from app.models.pricing_rules import PricingScope, PricingStrategy
from app.models.tenancy import Tenant
from app.repositories import ai_catalog as catalog_repository
from app.repositories import fx_rates as fx_repository
from app.repositories import pricing_rules as rule_repository
from app.repositories import provider_prices as price_repository
from app.schemas.pricing_rules import RuleComponentInput
from app.schemas.provider_prices import ComponentInput
from app.services import ai_catalog, fx_rates, pricing_rules, provider_prices
from app.services.auth import RequestContext, utc_now
from app.services.pricing_engine import (
    PRICING_CONTEXT,
    ChargeOverflow,
    ChargeResolution,
    ChargeResult,
    ChargeSnapshot,
    ChargeStatus,
    MatchedVia,
    PricingErrorCode,
    RuleTerms,
    UnitPrice,
    price_event,
    price_resolved,
    reprice_from_snapshot,
    resolve_charge_inputs,
)

ENGINE_FILE = pathlib.Path("app/services/pricing_engine.py")
CONTEXT = RequestContext(ip_address="203.0.113.9", user_agent="pricing-engine-test")
TEST_EMAIL_DOMAIN = "@pricing-engine-test.example.com"

T0 = dt.datetime(2026, 9, 29, 8, 30, 0)
EVENT_AT = T0 + dt.timedelta(hours=1)

# The four LLM_TOKEN components and the fields they take their quantity from (025).
FIELDS = {
    "LLM_CACHE_READ_TOKEN": "cache_read_input_tokens",
    "LLM_CACHE_WRITE_TOKEN": "cache_creation_input_tokens",
    "LLM_INPUT_TOKEN": "input_tokens",
    "LLM_OUTPUT_TOKEN": "output_tokens",
}
TOKENS = {
    "input_tokens": 1000,
    "output_tokens": 2000,
    "cache_creation_input_tokens": 400,
    "cache_read_input_tokens": 3000,
}
# Fictional USD provider prices per 1 000 000 tokens.
USD_RATES = {
    "LLM_INPUT_TOKEN": "1.5",
    "LLM_OUTPUT_TOKEN": "7.5",
    "LLM_CACHE_WRITE_TOKEN": "1.875",
    "LLM_CACHE_READ_TOKEN": "0.15",
}
# Fictional MYR tax-inclusive customer prices per 1 000 000 tokens (FIXED_RATE).
FIXED_MYR_RATES = {
    "LLM_INPUT_TOKEN": "2",
    "LLM_OUTPUT_TOKEN": "10",
    "LLM_CACHE_WRITE_TOKEN": "2.5",
    "LLM_CACHE_READ_TOKEN": "0.2",
}
FX_RATE = "4.4444444444"
PER_MILLION = "1000000"

# Hand-computed for TOKENS × USD_RATES (design §7「MARKUP」):
#   pc = 1000×1.5/1e6 + 2000×7.5/1e6 + 400×1.875/1e6 + 3000×0.15/1e6
#      = 0.0015 + 0.015 + 0.00075 + 0.00045 = 0.0177
#   EC = 0.0177 × 4.4444444444 = 0.07866666666588  → 0.07866667
#   B  = EC × 2 = 0.15733333333176                 → 0.15733333
#   (the stored EC × 2 would be 0.15733334: the unrounded chain matters)
PROVIDER_COSTS = {
    "LLM_CACHE_READ_TOKEN": "0.00045",
    "LLM_CACHE_WRITE_TOKEN": "0.00075",
    "LLM_INPUT_TOKEN": "0.0015",
    "LLM_OUTPUT_TOKEN": "0.015",
}
SOURCE_COST = "0.0177"
ESTIMATED_MYR = "0.07866666666588"
MARKUP_BILLABLE = "0.15733333333176"
# FIXED_RATE: 1000×2/1e6 + 2000×10/1e6 + 400×2.5/1e6 + 3000×0.2/1e6 = 0.0236.
CUSTOMER_PRICES = {
    "LLM_CACHE_READ_TOKEN": "0.0006",
    "LLM_CACHE_WRITE_TOKEN": "0.001",
    "LLM_INPUT_TOKEN": "0.002",
    "LLM_OUTPUT_TOKEN": "0.02",
}
FIXED_BILLABLE = "0.0236"


def text(value: Decimal) -> str:
    """Plain decimal text (`str` would print 1E-8)."""
    return format(value, "f")


def unit_prices(rates: Mapping[str, str], unit: str = PER_MILLION) -> tuple[UnitPrice, ...]:
    return tuple(
        UnitPrice(code, FIELDS[code], Decimal(unit), Decimal(rate)) for code, rate in rates.items()
    )


def markup(multiplier: str = "2") -> RuleTerms:
    return RuleTerms(PricingStrategy.MARKUP, Decimal(multiplier), ())


def fixed(rates: Mapping[str, str] = FIXED_MYR_RATES) -> RuleTerms:
    return RuleTerms(PricingStrategy.FIXED_RATE, None, unit_prices(rates))


def priced(**overrides: object) -> ChargeResult:
    """`price_event` on the MARKUP example, with some inputs replaced."""
    inputs: dict[str, object] = {
        "quantities": TOKENS,
        "source_currency": "USD",
        "provider_components": unit_prices(USD_RATES),
        "fx_rate": Decimal(FX_RATE),
        "rule": markup(),
    }
    inputs.update(overrides)
    return price_event(**inputs)  # type: ignore[arg-type]


def costs(result: ChargeResult) -> dict[str, Decimal]:
    return {line.component_code: line.provider_cost_unrounded for line in result.components}


# --- unit: the algorithm --------------------------------------------------------------


def test_markup_matches_the_hand_computed_values() -> None:
    """§113 MARKUP: four LLM components, a USD price, an FX rate, multiplier 2."""
    result = priced()

    assert [line.component_code for line in result.components] == sorted(FIELDS)
    assert costs(result) == {code: Decimal(cost) for code, cost in PROVIDER_COSTS.items()}
    assert [line.quantity for line in result.components] == [
        Decimal(TOKENS[FIELDS[code]]) for code in sorted(FIELDS)
    ]
    assert all(line.customer_price_unrounded is None for line in result.components)
    assert result.provider_source_cost_unrounded == Decimal(SOURCE_COST)
    assert result.estimated_provider_cost_myr_unrounded == Decimal(ESTIMATED_MYR)
    assert result.billable_cost_unrounded == Decimal(MARKUP_BILLABLE)
    assert text(result.provider_source_cost) == "0.01770000"
    assert text(result.estimated_provider_cost_myr) == "0.07866667"
    assert text(result.billable_cost) == "0.15733333"
    assert result.provider_source_currency == "USD"
    assert result.fx_rate_applied == Decimal(FX_RATE)


def test_fixed_rate_bills_only_from_the_rule_but_still_snapshots_the_cost() -> None:
    """§113 FIXED_RATE: the billed amount ignores provider prices and the FX rate."""
    result = priced(rule=fixed())

    customer = {line.component_code: line.customer_price_unrounded for line in result.components}
    assert customer == {code: Decimal(price) for code, price in CUSTOMER_PRICES.items()}
    assert result.billable_cost_unrounded == Decimal(FIXED_BILLABLE)
    assert text(result.billable_cost) == "0.02360000"
    # §14's cost snapshot is computed all the same.
    assert text(result.provider_source_cost) == "0.01770000"
    assert text(result.estimated_provider_cost_myr) == "0.07866667"

    cheaper = {code: "0.01" for code in USD_RATES}
    other = priced(
        rule=fixed(), provider_components=unit_prices(cheaper), fx_rate=Decimal("3.1234567891")
    )
    assert other.billable_cost == result.billable_cost
    assert other.estimated_provider_cost_myr != result.estimated_provider_cost_myr


def test_cache_components_are_priced_at_their_own_rates() -> None:
    """§113 cache write and cache read, both non-zero, the others zero."""
    cache_only = {**TOKENS, "input_tokens": 0, "output_tokens": 0}
    result = priced(quantities=cache_only)

    assert costs(result) == {
        "LLM_CACHE_READ_TOKEN": Decimal("0.00045"),
        "LLM_CACHE_WRITE_TOKEN": Decimal("0.00075"),
        "LLM_INPUT_TOKEN": Decimal(0),
        "LLM_OUTPUT_TOKEN": Decimal(0),
    }
    # 0.0012 × 4.4444444444 = 0.00533333333328; × 2 = 0.01066666666656.
    assert result.provider_source_cost_unrounded == Decimal("0.0012")
    assert text(result.estimated_provider_cost_myr) == "0.00533333"
    assert text(result.billable_cost) == "0.01066667"

    swapped = dict(USD_RATES)
    swapped["LLM_CACHE_READ_TOKEN"] = USD_RATES["LLM_CACHE_WRITE_TOKEN"]
    swapped["LLM_CACHE_WRITE_TOKEN"] = USD_RATES["LLM_CACHE_READ_TOKEN"]
    other = priced(quantities=cache_only, provider_components=unit_prices(swapped))
    assert costs(other)["LLM_CACHE_READ_TOKEN"] == Decimal("0.005625")
    assert costs(other)["LLM_CACHE_WRITE_TOKEN"] == Decimal("0.00006")


def test_decimal_audio_seconds() -> None:
    """§113 non-token: 12.5 audio seconds at a fictional USD 0.006 per second."""
    audio = (UnitPrice("AUDIO_SECOND", "quantity", Decimal("1"), Decimal("0.006")),)
    result = priced(quantities={"quantity": Decimal("12.5")}, provider_components=audio)

    assert costs(result) == {"AUDIO_SECOND": Decimal("0.075")}
    # 0.075 × 4.4444444444 = 0.33333333333; × 2 = 0.66666666666.
    assert text(result.provider_source_cost) == "0.07500000"
    assert text(result.estimated_provider_cost_myr) == "0.33333333"
    assert text(result.billable_cost) == "0.66666667"


def test_integer_image_generations_on_a_fixed_rate() -> None:
    """§113 non-token: 3 images, an MYR provider price, a FIXED_RATE customer price."""
    images = (UnitPrice("IMAGE_GENERATION", "quantity", Decimal("1"), Decimal("0.04")),)
    rule = RuleTerms(
        PricingStrategy.FIXED_RATE,
        None,
        (UnitPrice("IMAGE_GENERATION", "quantity", Decimal("1"), Decimal("0.5")),),
    )
    result = priced(
        quantities={"quantity": 3},
        source_currency="MYR",
        provider_components=images,
        fx_rate=None,
        rule=rule,
    )

    assert text(result.provider_source_cost) == "0.12000000"
    assert text(result.estimated_provider_cost_myr) == "0.12000000"
    assert text(result.billable_cost) == "1.50000000"


def test_an_myr_source_needs_no_fx_rate() -> None:
    """EC = PC; no FX rate is applied or stored."""
    result = priced(source_currency="MYR", fx_rate=None)

    assert result.fx_rate_applied is None
    assert result.provider_source_currency == "MYR"
    assert result.estimated_provider_cost_myr_unrounded == result.provider_source_cost_unrounded
    assert result.estimated_provider_cost_myr == result.provider_source_cost
    # 0.0177 × 2.
    assert text(result.billable_cost) == "0.03540000"


def test_the_fx_rate_goes_with_the_source_currency() -> None:
    with pytest.raises(ValueError):
        priced(source_currency="MYR", fx_rate=Decimal(FX_RATE))
    with pytest.raises(ValueError):
        priced(source_currency="USD", fx_rate=None)


def test_components_are_summed_before_the_one_rounding() -> None:
    """§80: two components of 0.000000004 each. Rounding each first would give 0."""
    tiny = (
        UnitPrice("A", "a", Decimal("10"), Decimal("0.00000001")),
        UnitPrice("B", "b", Decimal("10"), Decimal("0.00000001")),
    )
    result = priced(
        quantities={"a": 4, "b": 4},
        source_currency="MYR",
        provider_components=tiny,
        fx_rate=None,
        rule=markup("1"),
    )

    each_rounded = sum(quantize_money(cost) for cost in costs(result).values())
    assert each_rounded == 0
    assert result.provider_source_cost_unrounded == Decimal("0.000000008")
    assert text(result.provider_source_cost) == "0.00000001"
    assert text(result.billable_cost) == "0.00000001"


def test_a_five_in_the_ninth_place_rounds_up() -> None:
    """ROUND_HALF_UP: 0.000000025 → 0.00000003 (half-even would give 0.00000002)."""
    exact = Decimal("0.000000025")
    assert exact.quantize(Decimal("1E-8"), rounding=ROUND_HALF_EVEN) == Decimal("0.00000002")
    one = (UnitPrice("A", "a", Decimal("10"), Decimal("0.00000001")),)

    result = priced(
        quantities={"a": 25},
        source_currency="MYR",
        provider_components=one,
        fx_rate=None,
        rule=markup("1"),
    )

    assert result.provider_source_cost_unrounded == exact
    assert text(result.provider_source_cost) == "0.00000003"
    assert text(result.billable_cost) == "0.00000003"


def test_the_billed_amount_comes_from_the_unrounded_estimated_cost() -> None:
    """§15: quantize(EC × 2), not quantize(EC) × 2 — the two differ here."""
    result = priced()

    from_stored = result.estimated_provider_cost_myr * 2
    assert text(from_stored) == "0.15733334"
    assert text(result.billable_cost) == "0.15733333"
    assert result.billable_cost == quantize_money(result.estimated_provider_cost_myr_unrounded * 2)


def test_the_process_decimal_context_does_not_matter() -> None:
    """INV-10: prec = 5, ROUND_DOWN and an Inexact trap outside give the same digits."""
    baseline = priced()
    fixed_baseline = priced(rule=fixed())

    with localcontext() as context:
        context.prec = 5
        context.rounding = ROUND_DOWN
        context.traps[Inexact] = True
        changed = priced()
        fixed_changed = priced(rule=fixed())

    assert changed == baseline
    assert fixed_changed == fixed_baseline
    assert repr(changed) == repr(baseline)


def test_the_pricing_context() -> None:
    assert PRICING_CONTEXT.prec == 50
    assert PRICING_CONTEXT.rounding == ROUND_HALF_UP
    assert not any(PRICING_CONTEXT.traps.values())


def test_the_same_inputs_give_bit_identical_results() -> None:
    """ADR-0005: a hundred runs, one result."""
    first = priced()

    for _ in range(100):
        again = priced()
        assert again == first
        assert repr(again) == repr(first)


def test_a_zero_billed_amount() -> None:
    """All tokens zero, or too small for 8 places: billable_cost = 0 (T-H writes no ledger)."""
    zero = priced(quantities=dict.fromkeys(TOKENS, 0))
    assert zero.billable_cost == 0
    assert text(zero.billable_cost) == "0.00000000"
    assert text(zero.provider_source_cost) == "0.00000000"

    tiny_rates = dict.fromkeys(USD_RATES, "0.00000001")
    tiny = priced(
        quantities={**dict.fromkeys(TOKENS, 0), "input_tokens": 1},
        provider_components=unit_prices(tiny_rates),
    )
    assert tiny.billable_cost_unrounded > 0
    assert text(tiny.billable_cost) == "0.00000000"


def test_amounts_beyond_decimal_20_8_raise_charge_overflow() -> None:
    top = dict.fromkeys(USD_RATES, "999999999999.99999999")
    with pytest.raises(ChargeOverflow) as raised:
        priced(
            quantities=dict.fromkeys(TOKENS, 10**12),
            provider_components=unit_prices(top, unit="0.00000001"),
        )
    # Only the error code, never an amount (design §6).
    assert str(raised.value) == "AMOUNT_OUT_OF_RANGE"


def test_overflow_is_checked_on_each_stored_amount() -> None:
    """The largest DECIMAL(20,8) fits; a multiplier that pushes only B past it does not."""
    largest = (UnitPrice("A", "a", Decimal("1"), Decimal("999999999999.99999999")),)
    inputs = {
        "quantities": {"a": 1},
        "source_currency": "MYR",
        "provider_components": largest,
        "fx_rate": None,
    }

    fits = priced(**inputs, rule=markup("1"))
    assert text(fits.billable_cost) == "999999999999.99999999"
    with pytest.raises(ChargeOverflow):
        priced(**inputs, rule=markup("1.00000001"))


def test_incomplete_inputs_are_a_caller_error() -> None:
    """Missing components are PRICING_ERROR in the resolver; here they never price as 0."""
    with pytest.raises(ValueError):
        priced(quantities={"input_tokens": 1})
    with pytest.raises(ValueError):
        priced(rule=fixed({"LLM_INPUT_TOKEN": "2"}))
    with pytest.raises(ValueError):
        priced(rule=RuleTerms(PricingStrategy.MARKUP, None, ()))


# --- unit: static scan ----------------------------------------------------------------


def test_the_module_never_uses_float_or_half_even_and_rounds_only_via_quantize_money() -> None:
    source = ENGINE_FILE.read_text(encoding="utf-8")

    assert "float(" not in source
    assert "ROUND_HALF_EVEN" not in source
    assert ".quantize(" not in source
    assert "quantize_money(" in source
    assert "localcontext(PRICING_CONTEXT)" in source
    names = {node.id for node in ast.walk(ast.parse(source)) if isinstance(node, ast.Name)}
    assert "float" not in names


# --- integration helpers ----------------------------------------------------------------


class Clock:
    def __init__(self, moment: dt.datetime) -> None:
        self.now = moment

    def __call__(self) -> dt.datetime:
        return self.now


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


class World:
    """One admin, provider `anthropic` with model `claude-x`, two tenants; services only."""

    def __init__(self, factory: sessionmaker[Session], clock: Clock) -> None:
        self.factory = factory
        self.clock = clock
        self.admin = make_admin(factory)
        self.provider = ai_catalog.create_provider(
            factory,
            actor=self.admin,
            code="anthropic",
            display_name="Fictional provider",
            context=CONTEXT,
            clock=clock,
        )
        self.model = ai_catalog.create_model(
            factory,
            actor=self.admin,
            provider_id=self.provider.id,
            code="claude-x",
            display_name="Fictional model",
            context=CONTEXT,
            clock=clock,
        )
        self.tenants = {name: make_tenant(factory, name) for name in ("tenant-a", "tenant-b")}

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

    def publish_all(self) -> None:
        """The MARKUP example: the USD price, the USD rate and a global MARKUP of 2."""
        self.price()
        self.fx()
        self.rule()

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

    def tenant_id(self, tenant: str) -> int:
        statement = select(Tenant.id).where(Tenant.public_id == self.tenants[tenant])
        with self.factory() as session:
            return int(session.execute(statement).scalar_one())

    def meter_type_id(self, code: str) -> int:
        statement = select(UsageMeterType.id).where(UsageMeterType.code == code)
        with self.factory() as session:
            return int(session.execute(statement).scalar_one())

    def resolve(
        self,
        *,
        provider: str = "anthropic",
        model: str = "claude-x",
        meter: str = "LLM_TOKEN",
        tenant: str = "tenant-a",
        occurred_at: dt.datetime = EVENT_AT,
        locking: bool = True,
    ) -> ChargeResolution:
        """One billing-side resolution in a transaction of its own."""
        tenant_id, meter_type_id = self.tenant_id(tenant), self.meter_type_id(meter)
        with self.factory() as session:
            resolution = resolve_charge_inputs(
                session,
                tenant_id=tenant_id,
                provider_code=provider,
                model_code=model,
                meter_type_id=meter_type_id,
                occurred_at=occurred_at,
                locking=locking,
            )
            session.commit()
        return resolution


@pytest.fixture
def factory() -> Iterator[sessionmaker[Session]]:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = create_session_factory(engine)
    seed_meter_types(session_factory)
    yield session_factory
    engine.dispose()


@pytest.fixture
def world(factory) -> World:
    return World(factory, Clock(T0))


def failed(
    resolution: ChargeResolution, status: ChargeStatus, code: PricingErrorCode | None
) -> None:
    assert (resolution.status, resolution.error_code) == (status, code)
    assert resolution.provider_components == ()
    assert resolution.rule_terms is None
    with pytest.raises(ValueError):
        price_resolved(resolution, TOKENS)


# --- integration: resolution ------------------------------------------------------------


def test_a_complete_resolution_prices_the_event(world) -> None:
    price_id = world.price()
    fx_id = world.fx()
    rule_id = world.rule()

    resolution = world.resolve()

    assert (resolution.status, resolution.error_code) == (ChargeStatus.PRICED, None)
    assert resolution.model is not None
    assert resolution.model.model_code == "claude-x"
    assert resolution.model.matched_via is MatchedVia.CODE
    assert resolution.provider_price is not None and resolution.fx_rate is not None
    assert resolution.pricing_rule is not None
    assert resolution.provider_price.public_id == price_id
    assert resolution.fx_rate.public_id == fx_id
    assert resolution.pricing_rule.public_id == rule_id
    assert [item.component_code for item in resolution.meter_components] == sorted(FIELDS)
    result = price_resolved(resolution, TOKENS)
    assert text(result.provider_source_cost) == "0.01770000"
    assert text(result.estimated_provider_cost_myr) == "0.07866667"
    assert text(result.billable_cost) == "0.15733333"
    assert result.billable_cost_unrounded == Decimal(MARKUP_BILLABLE)


def test_an_unknown_model_or_provider(world) -> None:
    world.publish_all()

    for provider, model in (("anthropic", "no-such-model"), ("no-such-provider", "claude-x")):
        resolution = world.resolve(provider=provider, model=model)
        failed(resolution, ChargeStatus.MODEL_UNKNOWN, None)
        assert resolution.model is None and resolution.provider_price is None


def test_no_provider_price_comes_before_a_missing_fx_rate(world) -> None:
    """Design §7: missing both the price and the FX rate reports the PRICING_ERROR first."""
    world.rule()

    resolution = world.resolve()

    failed(resolution, ChargeStatus.PRICING_ERROR, PricingErrorCode.NO_PROVIDER_PRICE)
    assert resolution.model is not None and resolution.provider_price is None


def test_a_price_without_the_meter_type(world) -> None:
    price_id = world.price({"AUDIO_SECOND": "0.006"}, unit="1")
    world.fx()
    world.rule()

    resolution = world.resolve()

    failed(resolution, ChargeStatus.PRICING_ERROR, PricingErrorCode.MISSING_PROVIDER_COMPONENT)
    assert resolution.provider_price is not None
    assert resolution.provider_price.public_id == price_id
    assert resolution.fx_rate is None and resolution.pricing_rule is None


def test_a_missing_fx_rate_comes_before_a_missing_rule(world) -> None:
    world.price(currency="EUR")
    world.fx("USD")

    resolution = world.resolve()

    failed(resolution, ChargeStatus.FX_RATE_ERROR, None)
    assert resolution.provider_price is not None and resolution.fx_rate is None
    assert resolution.pricing_rule is None


def test_no_pricing_rule(world) -> None:
    world.price()
    world.fx()

    resolution = world.resolve()

    failed(resolution, ChargeStatus.PRICING_ERROR, PricingErrorCode.NO_PRICING_RULE)
    assert resolution.fx_rate is not None and resolution.pricing_rule is None


def test_a_fixed_rate_rule_without_the_meter_type_does_not_fall_through(world) -> None:
    """Design §7「不下落」: the customer's FIXED_RATE rule wins and lacks LLM_TOKEN."""
    world.price()
    world.fx()
    world.rule()
    customer_rule = world.rule(PricingScope.CUSTOMER, rates={"AUDIO_SECOND": "0.05"}, unit="1")

    resolution = world.resolve()

    failed(resolution, ChargeStatus.PRICING_ERROR, PricingErrorCode.MISSING_RULE_COMPONENT)
    assert resolution.pricing_rule is not None
    assert resolution.pricing_rule.public_id == customer_rule
    # The other tenant has no customer rule: the global default prices it.
    assert world.resolve(tenant="tenant-b").status is ChargeStatus.PRICED


def test_an_myr_price_needs_no_fx_rate(world) -> None:
    world.price(currency="MYR")
    world.rule()

    resolution = world.resolve()

    assert resolution.status is ChargeStatus.PRICED
    assert resolution.fx_rate is None
    result = price_resolved(resolution, TOKENS)
    assert result.fx_rate_applied is None
    assert result.estimated_provider_cost_myr == result.provider_source_cost


def test_an_alias_resolves_to_the_same_result(world) -> None:
    world.publish_all()
    world.alias("claude-latest")

    by_alias = world.resolve(model="claude-latest")
    by_code = world.resolve()

    assert by_alias.model is not None
    assert by_alias.model.model_code == "claude-x"
    assert by_alias.model.matched_via is MatchedVia.ALIAS
    assert price_resolved(by_alias, TOKENS) == price_resolved(by_code, TOKENS)


def test_a_customer_rule_never_prices_another_tenant(world) -> None:
    """INV-8: tenant A's own rule; tenant B falls to the global default."""
    world.price()
    world.fx()
    global_rule = world.rule(multiplier="2")
    rule_a = world.rule(PricingScope.CUSTOMER, tenant="tenant-a", multiplier="3")

    a, b = world.resolve(tenant="tenant-a"), world.resolve(tenant="tenant-b")

    assert a.pricing_rule is not None and b.pricing_rule is not None
    assert (a.pricing_rule.public_id, b.pricing_rule.public_id) == (rule_a, global_rule)
    # 0.07866666666588 × 3 = 0.23599999999764; × 2 = 0.15733333333176.
    assert text(price_resolved(a, TOKENS).billable_cost) == "0.23600000"
    assert text(price_resolved(b, TOKENS).billable_cost) == "0.15733333"


def _recorder(
    name: str, original: Callable[..., object], calls: list[str]
) -> Callable[..., object]:
    def recorded(*args: object, **kwargs: object) -> object:
        calls.append(name)
        return original(*args, **kwargs)

    return recorded


def test_the_lock_order(world, monkeypatch) -> None:
    """locking=True: provider (S) → fx_rate_locks (S) → pricing_rule_locks (S), then the reads."""
    world.publish_all()
    calls: list[str] = []
    for module, name in (
        (catalog_repository, "resolve_model"),
        (fx_repository, "lock_fx_rates_shared"),
        (rule_repository, "lock_pricing_rules_shared"),
        (price_repository, "resolve_provider_price"),
        (fx_repository, "resolve_fx_rate"),
        (rule_repository, "resolve_pricing_rule"),
    ):
        monkeypatch.setattr(module, name, _recorder(name, getattr(module, name), calls))

    assert world.resolve(locking=True).status is ChargeStatus.PRICED
    assert calls == [
        "resolve_model",
        "lock_fx_rates_shared",
        "lock_pricing_rules_shared",
        "resolve_provider_price",
        "resolve_fx_rate",
        "resolve_pricing_rule",
    ]

    calls.clear()
    assert world.resolve(locking=False).status is ChargeStatus.PRICED
    assert calls == [
        "resolve_model",
        "resolve_provider_price",
        "resolve_fx_rate",
        "resolve_pricing_rule",
    ]


def test_the_price_fx_and_rule_reads_are_locking_reads() -> None:
    """The contracts of 025 / 026 / 027 / 028: every resolver reads in share mode."""
    for resolver in (
        catalog_repository.resolve_model,
        price_repository.resolve_provider_price,
        fx_repository.resolve_fx_rate,
        rule_repository.resolve_pricing_rule,
        fx_repository.lock_fx_rates_shared,
        rule_repository.lock_pricing_rules_shared,
    ):
        assert "_shared(" in inspect.getsource(resolver), resolver.__name__


def test_reprice_from_snapshot_reproduces_the_result(world) -> None:
    """ADR-0005: the snapshot's versions give the same MYR, even after newer ones are published."""
    world.publish_all()
    resolution = world.resolve()
    first = price_resolved(resolution, TOKENS)
    assert resolution.provider_price is not None and resolution.fx_rate is not None
    assert resolution.pricing_rule is not None
    snapshot = ChargeSnapshot(
        usage_meter_type_id=world.meter_type_id("LLM_TOKEN"),
        provider_price_version_id=resolution.provider_price.version_id,
        fx_rate_version_id=resolution.fx_rate.version_id,
        pricing_rule_id=resolution.pricing_rule.rule_id,
    )

    # Newer versions from T0 + 1 day on: a later moment prices differently, the snapshot does not.
    world.clock.now = T0 + dt.timedelta(days=1)
    world.price(dict.fromkeys(USD_RATES, "9"))
    world.rule(multiplier="5")
    later = world.resolve(occurred_at=T0 + dt.timedelta(days=2))
    assert price_resolved(later, TOKENS).billable_cost != first.billable_cost

    with world.factory() as session:
        again = reprice_from_snapshot(session, snapshot, TOKENS)
    assert again == first
    assert repr(again) == repr(first)
    # The event's own moment still resolves to the snapshot's versions.
    assert price_resolved(world.resolve(), TOKENS) == first

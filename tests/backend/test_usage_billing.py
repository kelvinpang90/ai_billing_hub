"""The billing worker below Celery (design gate #181 v2 §7, AIH-TASK-032).

Rows of design §7 covered here (the concurrent ones, the database constraints and triggers are in
test_usage_billing_concurrency.py; the requeue endpoints in test_usage_requeue_api.py; the alert
dimensions in test_usage_alerts.py):

- **正常路径** — LLM_TOKEN on MARKUP from a USD price: PROCESSED, the snapshot equals T-G's result,
  one `AI_USAGE` ledger row of −billable, the wallet debited;
- **FIXED_RATE、MYR 原币、非 token** — one each; the two FX columns empty for MYR;
- **0 元事件** — PROCESSED, no ledger row, `wallet_transaction_id` empty;
- **错误状态** — unknown model / provider, no price, a price without the meter type, no rule, a
  rule without the meter type, no FX rate: the status and detail code, nothing billed; an overflow
  is FAILED_FINAL `AMOUNT_OUT_OF_RANGE`;
- **重复处理** — a processed event is never claimed again (the trigger half is MySQL's);
- **账本冲突与重放** — a ledger row with another amount: FAILED_FINAL `LEDGER_CONFLICT`, the row
  untouched; with the same amount: PROCESSED, linked, nothing new;
- **认领**, **防护令牌**, **卡住回收**, **回收上限**, **未来的 `occurred_at`**, ⑤ (defensive), and
  the order «three shared locks, then the clock»;
- **跨零停机** (SQLite and MySQL) — 0.0625 − 0.125: SUSPENDED with the ledger row, `status_version`
  + 1, the audit and the outbox row; exactly 0 is SUSPENDED; debits while suspended move nothing;
- **`DISABLED` / `CLOSED` 租户**;
- **退避与上限** — 2^n capped at 300 seconds; FAILED_FINAL at the 10th attempt;
- **事务中途失败** (SQLite and MySQL) — the event write after `post_transaction` raises: ledger,
  wallet, transition, audit and outbox all roll back; the attempt is recorded apart;
- **队列丢失** — the functions are called without Celery; a backlog with expired PROCESSING rows
  after a Beat outage is billed in full;
- **快照可重算** (ADR-0005), **余额核对** (`verify_wallet`), **两个租户** (INV-8);
- **项目级规则** (design gate #507 v4 §7) — billing selects the rule by the event's project (SQLite
  and MySQL); a moment already chosen is never repriced by a later project rule (MySQL: E1, E2,
  E3);
- **名额：参数与 SQLite**, **任务传名额数** (design gate #522 v4 §7) — `parallelism=0` is a
  `ValueError` before anything is claimed; SQLite has no sweep slots and bills as usual; the task
  passes `USAGE_BILLING_SWEEP_PARALLELISM`; the slot statements bind `slot` and nothing else.

⚠️ SQLite has none of migration 0006's wallet triggers, so a ledger row would not move the wallet
and a second debit would reuse the sequence. The SQLite factory installs one test-only trigger
that does what 0006's AFTER INSERT does (balance and version follow the new row); the real
triggers are exercised by the MySQL half. SQLite also keeps DECIMAL as a binary float and checks
`balance_after = balance_before + amount` on it, so every fictional price, rate and amount here is
an exact binary fraction (0.0625, 4.5, …): the sums are exact on both databases. The MySQL half
needs `BILLING_TEST_DATABASE_URL`; it skips without it — **a skip is not a pass**, CI sets it.
uuids are zero placeholders.
"""

from __future__ import annotations

import datetime as dt
import itertools
import os
from collections.abc import Iterator, Mapping
from decimal import Decimal

import pytest
from alembic.config import Config
from sqlalchemy import Engine, create_engine, delete, func, select, update
from sqlalchemy.exc import OperationalError
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
from app.models.auth import AuditAction, AuditLog, DomainOutbox, User, UserRole, UserStatus
from app.models.base import Base
from app.models.integration import CredentialStatus, IntegrationCredential, ProjectStatusDelivery
from app.models.pricing_rules import PricingRule, PricingScope, PricingStrategy
from app.models.tenancy import AccountStatus, BillingMode, BillingStatus, Project, Tenant
from app.models.usage import UsageEvent, UsageEventStatus
from app.models.wallet import ReferenceType, TransactionType, Wallet, WalletTransaction
from app.repositories import usage_events as event_repository
from app.repositories import wallet as wallet_repository
from app.schemas.pricing_rules import RuleComponentInput
from app.schemas.provider_prices import ComponentInput
from app.services import (
    ai_catalog,
    customers,
    fx_rates,
    pricing_rules,
    provider_prices,
    usage_billing,
)
from app.services.auth import RequestContext
from app.services.pricing_engine import (
    ChargeResolution,
    ChargeResult,
    ChargeSnapshot,
    ChargeStatus,
    price_resolved,
    reprice_from_snapshot,
    resolve_charge_inputs,
)
from app.services.usage_billing import Claim, Outcome, SweepResult

TEST_DATABASE_URL = os.environ.get("BILLING_TEST_DATABASE_URL", "")
TEST_EMAIL_DOMAIN = "@usage-billing-test.example.com"
CONTEXT = RequestContext(ip_address="203.0.113.9", user_agent="usage-billing-test")

T0 = dt.datetime(2026, 9, 29, 8, 30, 0)
EVENT_AT = dt.datetime(2026, 9, 29, 9, 30, 0, 250000)
NOW = dt.datetime(2026, 9, 29, 10, 30, 0, 500000)
LEASE_SECONDS = 120
MAX_ATTEMPTS = 10
SECOND = dt.timedelta(seconds=1)

TOKENS = {
    "input_tokens": 1000,
    "output_tokens": 2000,
    "cache_creation_input_tokens": 500,
    "cache_read_input_tokens": 3000,
}
ZERO_TOKENS = dict.fromkeys(TOKENS, 0)
# Fictional USD provider prices per 1 000 tokens (exact binary fractions, see the docstring).
USD_RATES = {
    "LLM_INPUT_TOKEN": "0.0625",
    "LLM_OUTPUT_TOKEN": "0.25",
    "LLM_CACHE_WRITE_TOKEN": "0.125",
    "LLM_CACHE_READ_TOKEN": "0.0078125",
}
# Fictional MYR tax-inclusive customer prices per 1 000 tokens (FIXED_RATE).
FIXED_MYR_RATES = {
    "LLM_INPUT_TOKEN": "0.125",
    "LLM_OUTPUT_TOKEN": "0.5",
    "LLM_CACHE_WRITE_TOKEN": "0.25",
    "LLM_CACHE_READ_TOKEN": "0.015625",
}
FX_RATE = "4.5"
PER_THOUSAND = "1000"
# By hand for TOKENS:
#   source  = 1 × 0.0625 + 2 × 0.25 + 0.5 × 0.125 + 3 × 0.0078125 = 0.6484375 USD
#   EC      = 0.6484375 × 4.5                                     = 2.91796875 MYR
#   MARKUP  = EC × 2                                              = 5.8359375
#   FIXED   = 1 × 0.125 + 2 × 0.5 + 0.5 × 0.25 + 3 × 0.015625     = 1.296875
SOURCE_COST = Decimal("0.64843750")
ESTIMATED_MYR = Decimal("2.91796875")
MARKUP_BILLABLE = Decimal("5.83593750")
FIXED_BILLABLE = Decimal("1.29687500")
OPENING = Decimal("100")
# Audio: MYR 0.03125 a second from the provider; customers pay MYR 0.0625 a second (FIXED_RATE).
AUDIO_COST = "0.03125"
AUDIO_PRICE = "0.0625"

_NUMBERS = itertools.count(1)


class Clock:
    def __init__(self, moment: dt.datetime) -> None:
        self.now = moment

    def __call__(self) -> dt.datetime:
        return self.now


# --- the world ------------------------------------------------------------------------


class Tenancy:
    """A customer (tenant and wallet), its project and one integration credential."""

    def __init__(
        self,
        public_id: str,
        tenant_id: int,
        project_id: int,
        credential_id: int,
        project_public_id: str = "",
    ):
        self.public_id = public_id
        self.id = tenant_id
        self.project_id = project_id
        self.credential_id = credential_id
        self.project_public_id = project_public_id


class World:
    """One admin, provider `anthropic` with model `claude-x`; services only, frozen clock."""

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

    # -- tenants and money --

    def tenant(self, name: str = "a") -> Tenancy:
        number = next(_NUMBERS)
        detail = customers.create_customer(
            self.factory,
            actor=self.admin,
            company_name=f"Fictional {name} Sdn Bhd",
            email=f"tenant-{number}{TEST_EMAIL_DOMAIN}",
            context=CONTEXT,
            now=T0,
        )
        return self.project(detail.id, "Chatbot")

    def project(self, customer_id: str, name: str) -> Tenancy:
        """A new project of the customer with its own credential: events through it carry the
        project (§11). The tenant and its wallet are the customer's."""
        number = next(_NUMBERS)
        project = customers.create_project(
            self.factory,
            actor=self.admin,
            customer_id=customer_id,
            name=name,
            context=CONTEXT,
            now=T0,
        )
        with self.factory() as session:
            tenant_id = session.execute(
                select(Tenant.id).where(Tenant.public_id == customer_id)
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
            session.commit()
            return Tenancy(customer_id, tenant_id, project_id, credential.id, project.id)

    def fund(self, tenant: Tenancy, amount: str | Decimal) -> None:
        with self.factory() as session:
            wallet_repository.post_transaction(
                session,
                tenant_id=tenant.id,
                transaction_type=TransactionType.ADJUSTMENT_CREDIT,
                amount=Decimal(amount),
                reference_type=ReferenceType.ADMIN_ADJUSTMENT,
                reference_id=f"fund-{next(_NUMBERS)}",
                now=T0,
                created_by=self.admin.id,
                description="Fictional top-up for a billing test",
                actor_role="ADMIN",
            )
            session.commit()

    def set_account_status(self, tenant: Tenancy, status: AccountStatus) -> None:
        with self.factory() as session:
            session.execute(
                update(Tenant).where(Tenant.id == tenant.id).values(account_status=status)
            )
            session.commit()

    # -- prices, rules, rates --

    def price(
        self,
        rates: Mapping[str, str] = USD_RATES,
        *,
        currency: str = "USD",
        unit: str = PER_THOUSAND,
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
        return provider_prices.publish(
            self.factory,
            actor=self.admin,
            price_version_id=draft.id,
            effective_from=None,
            context=CONTEXT,
            clock=self.clock,
        ).id

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
        return fx_rates.publish(
            self.factory,
            actor=self.admin,
            fx_rate_id=draft.id,
            effective_from=None,
            context=CONTEXT,
            clock=self.clock,
        ).id

    def rule(
        self,
        *,
        multiplier: str = "2",
        rates: Mapping[str, str] | None = None,
        unit: str = PER_THOUSAND,
        scope: PricingScope = PricingScope.GLOBAL,
        tenant: Tenancy | None = None,
    ) -> str:
        """A global default unless `scope` says otherwise (`tenant` then gives the customer and,
        for a project level, its project): MARKUP with `multiplier` unless `rates` is given
        (FIXED_RATE)."""
        components = None
        if rates is not None:
            components = [
                RuleComponentInput.model_validate(
                    {"component_code": code, "unit_quantity": unit, "rate_amount": rate}
                )
                for code, rate in rates.items()
            ]
        if scope.has_customer:
            assert tenant is not None
        draft = pricing_rules.create_draft(
            self.factory,
            actor=self.admin,
            priority_scope=scope,
            customer_id=tenant.public_id if tenant is not None and scope.has_customer else None,
            project_id=(
                tenant.project_public_id if tenant is not None and scope.has_project else None
            ),
            provider_id=self.provider.id if scope.has_provider else None,
            model_id=self.model.id if scope.has_model else None,
            strategy=PricingStrategy.MARKUP if rates is None else PricingStrategy.FIXED_RATE,
            markup_multiplier=Decimal(multiplier) if rates is None else None,
            components=components,
            context=CONTEXT,
            clock=self.clock,
        )
        return pricing_rules.publish(
            self.factory,
            actor=self.admin,
            rule_id=draft.id,
            effective_from=None,
            context=CONTEXT,
            clock=self.clock,
        ).id

    def publish_markup_usd(self) -> None:
        """The USD price, the USD rate and a global MARKUP of 2."""
        self.price()
        self.fx()
        self.rule()

    def publish_audio_fixed_myr(self) -> None:
        """An MYR audio price and a FIXED_RATE rule: `seconds` × 0.0625 is the billed amount."""
        self.price({"AUDIO_SECOND": AUDIO_COST}, currency="MYR", unit="1")
        self.rule(rates={"AUDIO_SECOND": AUDIO_PRICE}, unit="1")

    # -- events --

    def event(
        self,
        tenant: Tenancy,
        *,
        tokens: Mapping[str, int] | None = None,
        seconds: str | None = None,
        provider: str = "anthropic",
        model: str = "claude-x",
        feature: str | None = None,
        occurred_at: dt.datetime = EVENT_AT,
    ) -> int:
        """One RECEIVED event; LLM_TOKEN unless `seconds` is given (then AUDIO_SECOND)."""
        number = next(_NUMBERS)
        meter_code = "LLM_TOKEN" if seconds is None else "AUDIO_SECOND"
        token_fields = None
        if seconds is None:
            token_fields = dict(tokens if tokens is not None else TOKENS)
        with self.factory() as session:
            meter = event_repository.find_meter_type(session, meter_code)
            assert meter is not None
            row = event_repository.insert_event(
                session,
                event_id=f"00000000-0000-7000-8000-{number:012d}",
                schema_version="1.0",
                tenant_id=tenant.id,
                project_id=tenant.project_id,
                integration_credential_id=tenant.credential_id,
                request_id=f"call-{number}",
                conversation_id=None,
                provider_code_raw=provider,
                model_code_raw=model,
                usage_meter_type_id=meter.id,
                payload_shape=meter.payload_shape,
                quantity_kind=meter.quantity_kind,
                unit=meter.unit,
                tokens=token_fields,
                quantity=Decimal(seconds) if seconds is not None else None,
                payload_fingerprint="0" * 64,
                occurred_at=occurred_at,
                now=T0,
                feature=feature,
            )
            session.commit()
            return row.id

    # -- running the worker --

    def bill(self, at: dt.datetime = NOW, *, max_attempts: int = MAX_ATTEMPTS) -> SweepResult:
        self.clock.now = at
        return usage_billing.bill_pending_events(
            self.factory,
            lease_seconds=LEASE_SECONDS,
            max_attempts=max_attempts,
            clock=self.clock,
        )

    def claim(self, at: dt.datetime = NOW) -> list[Claim]:
        self.clock.now = at
        return usage_billing.claim_events(
            self.factory, lease_seconds=LEASE_SECONDS, clock=self.clock
        )

    def recover(self, at: dt.datetime, *, max_attempts: int = MAX_ATTEMPTS) -> int:
        self.clock.now = at
        return usage_billing.recover_stale_processing(
            self.factory, max_attempts=max_attempts, clock=self.clock
        )

    # -- reading back --

    def row(self, event_id: int) -> UsageEvent:
        with self.factory() as session:
            found = session.get(UsageEvent, event_id)
            assert found is not None
            session.expunge(found)
            return found

    def usage_rows(self, tenant: Tenancy) -> list[WalletTransaction]:
        statement = (
            select(WalletTransaction)
            .where(
                WalletTransaction.transaction_type == TransactionType.AI_USAGE,
                WalletTransaction.tenant_id == tenant.id,
            )
            .order_by(WalletTransaction.id)
        )
        with self.factory() as session:
            rows = list(session.execute(statement).scalars())
            session.expunge_all()
            return rows

    def balance(self, tenant: Tenancy) -> Decimal:
        with self.factory() as session:
            statement = select(Wallet.balance).where(Wallet.tenant_id == tenant.id)
            return session.execute(statement).scalar_one()

    def tenant_row(self, tenant: Tenancy) -> Tenant:
        with self.factory() as session:
            found = session.get(Tenant, tenant.id)
            assert found is not None
            session.expunge(found)
            return found

    def count(self, model: type, *conditions: object) -> int:
        statement = select(func.count()).select_from(model).where(*conditions)
        with self.factory() as session:
            return int(session.execute(statement).scalar_one())

    def verify(self, tenant: Tenancy) -> list[str]:
        with self.factory() as session:
            return wallet_repository.verify_wallet(session, tenant.id)

    def rule_row_id(self, public_id: str) -> int:
        with self.factory() as session:
            statement = select(PricingRule.id).where(PricingRule.public_id == public_id)
            return int(session.execute(statement).scalar_one())

    def provider_row_id(self) -> int:
        with self.factory() as session:
            statement = select(AiProvider.id).where(AiProvider.public_id == self.provider.id)
            return int(session.execute(statement).scalar_one())

    def model_row_id(self) -> int:
        with self.factory() as session:
            statement = select(AiModel.id).where(AiModel.public_id == self.model.id)
            return int(session.execute(statement).scalar_one())

    def expected(self, tenant: Tenancy, event_id: int) -> tuple[ChargeResolution, ChargeResult]:
        """What T-G gives for the event, resolved and priced in a transaction of its own."""
        event = self.row(event_id)
        with self.factory() as session:
            resolution = resolve_charge_inputs(
                session,
                tenant_id=tenant.id,
                project_id=event.project_id,
                provider_code=event.provider_code_raw,
                model_code=event.model_code_raw,
                meter_type_id=event.usage_meter_type_id,
                occurred_at=event.occurred_at,
                locking=False,
            )
            session.rollback()
        assert resolution.status is ChargeStatus.PRICED
        return resolution, price_resolved(resolution, quantities_of(event))


def quantities_of(event: UsageEvent) -> dict[str, int | Decimal]:
    if event.quantity is not None:
        return {"quantity": event.quantity}
    return {name: getattr(event, name) for name in TOKENS}


def assert_unclaimed(row: UsageEvent) -> None:
    assert (row.claim_token, row.claimed_at, row.lease_expires_at) == (None, None, None)


# --- factories -----------------------------------------------------------------------

# SQLite stand-in for migration 0006's AFTER INSERT trigger (see the module docstring).
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


def _sqlite_factory() -> Iterator[sessionmaker[Session]]:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.exec_driver_sql(_SQLITE_LEDGER_TRIGGER)
    factory = create_session_factory(engine)
    seed_meter_types(factory)
    yield factory
    engine.dispose()


def clean(engine: Engine) -> None:
    """⚠️ The database is shared: wipe this file's rows before and after every case.

    Usage events, the ledger, prices, rules, FX versions and the audit refuse DELETE (triggers), so
    TRUNCATE (DDL, no triggers) with foreign key checks off; the lock rows of 0014 / 0015 stay.
    Catalog rows have no delete path: removed directly, as test_pricing_rules_resolve.py does.
    """
    truncated = (
        "usage_event_conflicts",
        "usage_events",
        "wallet_transactions",
        "provider_price_components",
        "provider_price_versions",
        "pricing_rule_components",
        "pricing_rules",
        "fx_fetch_attempts",
        "fx_rate_versions",
        "domain_outbox",
        "audit_logs",
    )
    with engine.begin() as connection:
        connection.exec_driver_sql("SET FOREIGN_KEY_CHECKS = 0")
        try:
            for table in truncated:
                connection.exec_driver_sql(f"TRUNCATE TABLE {table}")
        finally:
            connection.exec_driver_sql("SET FOREIGN_KEY_CHECKS = 1")
    # Design gate #507 v4 §7「MySQL 用例的清理」: no project-level rule is left behind (it would
    # make any later downgrade through 0030 refuse).
    with engine.connect() as connection:
        project_rules = select(func.count()).select_from(PricingRule)
        left = connection.execute(project_rules.where(PricingRule.project_id.is_not(None)))
        assert left.scalar_one() == 0
    with engine.begin() as connection:
        ours = select(Tenant.id).where(Tenant.email.like(f"%{TEST_EMAIL_DOMAIN}"))
        connection.execute(
            delete(IntegrationCredential).where(IntegrationCredential.tenant_id.in_(ours))
        )
        connection.execute(
            delete(ProjectStatusDelivery).where(ProjectStatusDelivery.tenant_id.in_(ours))
        )
        connection.execute(delete(Project).where(Project.tenant_id.in_(ours)))
        connection.execute(delete(Wallet).where(Wallet.tenant_id.in_(ours)))
        connection.execute(delete(Tenant).where(Tenant.email.like(f"%{TEST_EMAIL_DOMAIN}")))
        connection.execute(delete(AiModelAlias))
        connection.execute(delete(AiModel))
        connection.execute(delete(AiProvider))
        connection.execute(delete(User).where(User.email.like(f"%{TEST_EMAIL_DOMAIN}")))


def _mysql_factory(monkeypatch: pytest.MonkeyPatch) -> Iterator[sessionmaker[Session]]:
    if not TEST_DATABASE_URL:
        pytest.skip("BILLING_TEST_DATABASE_URL is not set; the MySQL half needs a real MySQL")
    # Tables through alembic: the wallet, usage event and version triggers are migrations'.
    from app.core.config import get_settings

    monkeypatch.setenv("BILLING_DATABASE_URL", TEST_DATABASE_URL)
    get_settings.cache_clear()
    command.upgrade(Config("alembic.ini"), "head")
    get_settings.cache_clear()
    engine = create_engine(TEST_DATABASE_URL)
    clean(engine)
    try:
        yield create_session_factory(engine)
    finally:
        clean(engine)
        engine.dispose()


@pytest.fixture
def sqlite_factory() -> Iterator[sessionmaker[Session]]:
    yield from _sqlite_factory()


@pytest.fixture(params=["sqlite", "mysql"])
def factory(request, monkeypatch) -> Iterator[sessionmaker[Session]]:
    if request.param == "mysql":
        yield from _mysql_factory(monkeypatch)
        return
    yield from _sqlite_factory()


@pytest.fixture
def mysql_factory(monkeypatch) -> Iterator[sessionmaker[Session]]:
    yield from _mysql_factory(monkeypatch)


@pytest.fixture
def world(sqlite_factory) -> World:
    return World(sqlite_factory)


@pytest.fixture
def mysql_world(mysql_factory) -> World:
    """The world on a real MySQL only."""
    return World(mysql_factory)


@pytest.fixture
def both(factory) -> World:
    """The world on SQLite and on a real MySQL."""
    return World(factory)


# --- unit: backoff and error codes ---------------------------------------------------


def test_the_backoff_doubles_and_stops_at_300_seconds() -> None:
    doubled = [usage_billing.backoff_seconds(n) for n in range(1, 11)]

    assert doubled == [2, 4, 8, 16, 32, 64, 128, 256, 300, 300]
    assert usage_billing.backoff_seconds(10_000) == 300


def test_failure_codes_name_lock_timeouts_and_deadlocks_only() -> None:
    def operational(number: int) -> OperationalError:
        return OperationalError("UPDATE usage_events", {}, Exception(number, "fictional"))

    assert usage_billing.failure_code(operational(1205)) == "LOCK_WAIT_TIMEOUT"
    assert usage_billing.failure_code(operational(1213)) == "DEADLOCK"
    assert usage_billing.failure_code(operational(2013)) == "UNEXPECTED_ERROR"
    assert usage_billing.failure_code(RuntimeError("boom")) == "UNEXPECTED_ERROR"


# --- the normal path (§114) ------------------------------------------------------------


def test_an_llm_token_event_on_markup_in_usd_is_billed_once(world) -> None:
    world.publish_markup_usd()
    tenant = world.tenant()
    world.fund(tenant, OPENING)
    event_id = world.event(tenant)

    result = world.bill()

    assert (result.claimed, result.outcomes) == (1, {Outcome.PROCESSED: 1})
    row = world.row(event_id)
    resolution, charge = world.expected(tenant, event_id)
    assert resolution.model is not None and resolution.provider_price is not None
    assert resolution.fx_rate is not None and resolution.pricing_rule is not None
    assert row.status is UsageEventStatus.PROCESSED
    assert (row.provider_id, row.model_id) == (
        resolution.model.provider_id,
        resolution.model.model_id,
    )
    assert row.provider_price_version_id == resolution.provider_price.version_id
    assert row.pricing_rule_id == resolution.pricing_rule.rule_id
    assert row.fx_rate_version_id == resolution.fx_rate.version_id
    assert row.provider_source_currency == "USD"
    assert row.provider_source_cost == charge.provider_source_cost == SOURCE_COST
    assert row.fx_rate_applied == charge.fx_rate_applied == Decimal(FX_RATE)
    assert row.estimated_provider_cost_myr == charge.estimated_provider_cost_myr == ESTIMATED_MYR
    assert row.billable_cost == charge.billable_cost == MARKUP_BILLABLE
    assert row.processed_at == NOW.replace(microsecond=0)
    assert (row.attempt_count, row.error_code, row.next_attempt_at) == (1, None, None)
    assert_unclaimed(row)

    [ledger] = world.usage_rows(tenant)
    assert ledger.id == row.wallet_transaction_id
    assert ledger.amount == -MARKUP_BILLABLE
    assert (ledger.reference_type, ledger.reference_id) == (
        ReferenceType.USAGE_EVENT,
        row.event_id,
    )
    assert world.balance(tenant) == OPENING - MARKUP_BILLABLE
    assert row.billing_mode_snapshot == BillingMode.PREPAID.value
    assert row.reference_customer_price is None


def test_feature_does_not_change_the_priced_or_debited_amount(world) -> None:
    world.publish_markup_usd()
    tenant = world.tenant()
    world.fund(tenant, OPENING)
    without_feature = world.event(tenant)
    with_feature = world.event(tenant, feature="crm.contact_summary")

    result = world.bill()

    assert (result.claimed, result.outcomes) == (2, {Outcome.PROCESSED: 2})
    plain, labelled = world.row(without_feature), world.row(with_feature)
    assert (plain.feature, labelled.feature) == (None, "crm.contact_summary")
    assert plain.provider_source_cost == labelled.provider_source_cost
    assert plain.estimated_provider_cost_myr == labelled.estimated_provider_cost_myr
    assert plain.billable_cost == labelled.billable_cost
    assert world.balance(tenant) == OPENING - plain.billable_cost - labelled.billable_cost


def test_internal_metered_tenant_preserves_both_prices_without_wallet_effect(both) -> None:
    world = both
    world.publish_markup_usd()
    tenant = world.tenant()
    with world.factory.begin() as session:
        session.get(Tenant, tenant.id).billing_mode = BillingMode.INTERNAL_METERED_ONLY
    event_id = world.event(tenant)

    result = world.bill()

    assert (result.claimed, result.outcomes) == (1, {Outcome.PROCESSED: 1})
    row = world.row(event_id)
    assert row.billing_mode_snapshot == BillingMode.INTERNAL_METERED_ONLY.value
    assert row.estimated_provider_cost_myr == ESTIMATED_MYR
    assert row.reference_customer_price == MARKUP_BILLABLE
    assert row.billable_cost == 0
    assert row.wallet_transaction_id is None
    assert world.usage_rows(tenant) == []
    assert world.balance(tenant) == 0
    assert world.tenant_row(tenant).billing_status is BillingStatus.SUSPENDED
    # A duplicate sweep cannot recalculate or create a wallet charge.
    assert world.bill(NOW + dt.timedelta(minutes=1)).claimed == 0


def test_a_processed_event_is_never_claimed_again(world) -> None:
    """§114 Duplicate: a second sweep finds nothing; still one ledger row, one debit."""
    world.publish_markup_usd()
    tenant = world.tenant()
    world.fund(tenant, OPENING)
    world.event(tenant)
    world.bill()

    again = world.bill(NOW + dt.timedelta(hours=1))

    assert again.claimed == 0
    assert len(world.usage_rows(tenant)) == 1
    assert world.balance(tenant) == OPENING - MARKUP_BILLABLE


def test_fixed_rate_bills_from_the_rule_and_snapshots_the_cost(world) -> None:
    world.price()
    world.fx()
    world.rule(rates=FIXED_MYR_RATES)
    tenant = world.tenant()
    world.fund(tenant, OPENING)
    event_id = world.event(tenant)

    world.bill()

    row = world.row(event_id)
    _resolution, charge = world.expected(tenant, event_id)
    assert row.status is UsageEventStatus.PROCESSED
    assert row.billable_cost == charge.billable_cost == FIXED_BILLABLE
    # The cost is snapshotted although the bill does not depend on it (§15).
    assert row.estimated_provider_cost_myr == ESTIMATED_MYR
    assert [line.amount for line in world.usage_rows(tenant)] == [-FIXED_BILLABLE]


def test_an_myr_price_leaves_both_fx_columns_empty(world) -> None:
    world.price(currency="MYR")
    world.rule(multiplier="1.5")
    tenant = world.tenant()
    world.fund(tenant, OPENING)
    event_id = world.event(tenant)

    world.bill()

    row = world.row(event_id)
    assert row.status is UsageEventStatus.PROCESSED
    assert row.provider_source_currency == "MYR"
    assert (row.fx_rate_version_id, row.fx_rate_applied) == (None, None)
    assert row.estimated_provider_cost_myr == row.provider_source_cost == SOURCE_COST
    # 0.6484375 MYR × 1.5.
    assert row.billable_cost == Decimal("0.97265625")


def test_a_non_token_event_is_billed_by_its_quantity(world) -> None:
    world.price({"AUDIO_SECOND": AUDIO_COST}, currency="MYR", unit="1")
    world.rule()
    tenant = world.tenant()
    world.fund(tenant, OPENING)
    event_id = world.event(tenant, seconds="37.5")

    world.bill()

    row = world.row(event_id)
    # 37.5 × 0.03125 = 1.171875 MYR; × 2 = 2.34375.
    assert row.provider_source_cost == Decimal("1.171875")
    assert row.billable_cost == Decimal("2.34375")
    assert world.balance(tenant) == OPENING - Decimal("2.34375")


def test_a_zero_cost_event_is_processed_without_a_ledger_row(world) -> None:
    world.publish_markup_usd()
    tenant = world.tenant()
    world.fund(tenant, OPENING)
    event_id = world.event(tenant, tokens=ZERO_TOKENS)

    world.bill()

    row = world.row(event_id)
    assert row.status is UsageEventStatus.PROCESSED
    assert row.billable_cost == Decimal(0)
    assert row.wallet_transaction_id is None
    assert row.pricing_rule_id is not None and row.fx_rate_version_id is not None
    assert world.usage_rows(tenant) == []
    assert world.balance(tenant) == OPENING


# --- error states (§113, §114) ---------------------------------------------------------


def _assert_unbilled(world: World, tenant: Tenancy, row: UsageEvent) -> None:
    assert row.wallet_transaction_id is None and row.billable_cost is None
    assert row.processed_at is None and row.next_attempt_at is None
    assert_unclaimed(row)
    assert world.usage_rows(tenant) == []
    assert world.balance(tenant) == OPENING


@pytest.mark.parametrize(
    ("provider", "model"),
    [("anthropic", "no-such-model"), ("no-such-provider", "claude-x")],
    ids=["unknown-model", "unknown-provider"],
)
def test_an_unknown_model_is_model_unknown(world, provider: str, model: str) -> None:
    world.publish_markup_usd()
    tenant = world.tenant()
    world.fund(tenant, OPENING)
    event_id = world.event(tenant, provider=provider, model=model)

    result = world.bill()

    assert result.outcomes == {Outcome.MODEL_UNKNOWN: 1}
    row = world.row(event_id)
    assert (row.status, row.error_code) == (UsageEventStatus.MODEL_UNKNOWN, None)
    assert (row.provider_id, row.model_id) == (None, None)
    _assert_unbilled(world, tenant, row)


@pytest.mark.parametrize(
    ("setup", "status", "code"),
    [
        ("no-price", UsageEventStatus.PRICING_ERROR, "NO_PROVIDER_PRICE"),
        ("price-without-meter", UsageEventStatus.PRICING_ERROR, "MISSING_PROVIDER_COMPONENT"),
        ("no-rule", UsageEventStatus.PRICING_ERROR, "NO_PRICING_RULE"),
        ("rule-without-meter", UsageEventStatus.PRICING_ERROR, "MISSING_RULE_COMPONENT"),
        ("no-fx-rate", UsageEventStatus.FX_RATE_ERROR, None),
    ],
)
def test_missing_prices_rules_and_rates_stop_in_an_error_state(
    world, setup: str, status: UsageEventStatus, code: str | None
) -> None:
    if setup == "no-price":
        world.fx()
        world.rule()
    elif setup == "price-without-meter":
        world.price({"AUDIO_SECOND": AUDIO_COST}, unit="1")
        world.fx()
        world.rule()
    elif setup == "no-rule":
        world.price()
        world.fx()
    elif setup == "rule-without-meter":
        world.price()
        world.fx()
        world.rule(rates={"AUDIO_SECOND": AUDIO_PRICE}, unit="1")
    else:
        world.price(currency="EUR")
        world.fx("USD")
        world.rule()
    tenant = world.tenant()
    world.fund(tenant, OPENING)
    event_id = world.event(tenant)

    result = world.bill()

    assert result.outcomes == {Outcome(status.value): 1}
    row = world.row(event_id)
    assert (row.status, row.error_code) == (status, code)
    # The model was resolved: its catalog references are written for the admin to see.
    assert (row.provider_id, row.model_id) == (world.provider_row_id(), world.model_row_id())
    _assert_unbilled(world, tenant, row)


def test_an_amount_beyond_decimal_20_8_is_failed_final(world) -> None:
    world.price({"AUDIO_SECOND": "999999999999"}, currency="MYR", unit="1")
    world.rule()
    tenant = world.tenant()
    world.fund(tenant, OPENING)
    event_id = world.event(tenant, seconds="999999999999")

    world.bill()

    row = world.row(event_id)
    assert (row.status, row.error_code) == (UsageEventStatus.FAILED_FINAL, "AMOUNT_OUT_OF_RANGE")
    _assert_unbilled(world, tenant, row)


# --- the ledger as the last line (state table) -----------------------------------------


def _post_usage(world: World, tenant: Tenancy, event_id: int, amount: Decimal) -> int:
    """A ledger row for the event posted outside the worker (only a defect would do this)."""
    reference = world.row(event_id).event_id
    with world.factory() as session:
        posted = wallet_repository.post_transaction(
            session,
            tenant_id=tenant.id,
            transaction_type=TransactionType.AI_USAGE,
            amount=amount,
            reference_type=ReferenceType.USAGE_EVENT,
            reference_id=reference,
            now=T0,
        )
        session.commit()
        return posted.transaction.id


def test_a_ledger_row_with_another_amount_is_a_ledger_conflict(world, caplog) -> None:
    world.publish_markup_usd()
    tenant = world.tenant()
    world.fund(tenant, OPENING)
    event_id = world.event(tenant)
    original = _post_usage(world, tenant, event_id, Decimal("-0.0625"))

    with caplog.at_level("WARNING"):
        world.bill()

    row = world.row(event_id)
    assert (row.status, row.error_code) == (UsageEventStatus.FAILED_FINAL, "LEDGER_CONFLICT")
    assert row.wallet_transaction_id is None and row.billable_cost is None
    # The original row is untouched and nothing else was posted.
    assert [(line.id, line.amount) for line in world.usage_rows(tenant)] == [
        (original, Decimal("-0.0625"))
    ]
    assert world.balance(tenant) == OPENING - Decimal("0.0625")
    errors = [r for r in caplog.records if r.name == "app.services.usage_billing"]
    assert [r.levelname for r in errors] == ["ERROR"]
    # Ids and the code, never an amount.
    assert errors[0].__dict__["error_code"] == "LEDGER_CONFLICT"
    assert "0.0625" not in errors[0].getMessage()


def test_a_ledger_row_with_the_same_amount_is_linked_not_posted_again(world, caplog) -> None:
    world.publish_markup_usd()
    tenant = world.tenant()
    world.fund(tenant, OPENING)
    event_id = world.event(tenant)
    existing = _post_usage(world, tenant, event_id, -MARKUP_BILLABLE)

    with caplog.at_level("WARNING"):
        world.bill()

    row = world.row(event_id)
    assert row.status is UsageEventStatus.PROCESSED
    assert row.wallet_transaction_id == existing
    assert len(world.usage_rows(tenant)) == 1
    assert world.balance(tenant) == OPENING - MARKUP_BILLABLE
    warnings = [r for r in caplog.records if r.name == "app.services.usage_billing"]
    assert [r.levelname for r in warnings] == ["WARNING"]
    assert warnings[0].__dict__["event_id"] == row.event_id


# --- claiming, fencing and stale recovery (§82, §83, §110) -----------------------------


def test_a_claim_commits_processing_with_a_token_and_a_lease(world) -> None:
    world.publish_markup_usd()
    tenant = world.tenant()
    event_id = world.event(tenant)

    [claim] = world.claim()

    row = world.row(event_id)
    assert claim.id == event_id
    assert row.status is UsageEventStatus.PROCESSING
    assert row.claim_token == claim.token and len(claim.token) == 36
    assert row.claimed_at == NOW.replace(microsecond=0)
    assert row.lease_expires_at == row.claimed_at + dt.timedelta(seconds=LEASE_SECONDS)
    assert row.attempt_count == 1
    # Claimed rows are not claimable again.
    assert world.claim() == []


def test_a_stale_token_gives_the_event_up_without_a_trace(world) -> None:
    """Fencing: claimed, then (as if recovered and re-claimed) the token changed."""
    world.publish_markup_usd()
    tenant = world.tenant()
    world.fund(tenant, OPENING)
    event_id = world.event(tenant)
    [claim] = world.claim()
    other = "00000000-0000-4000-8000-000000000001"
    with world.factory() as session:
        statement = update(UsageEvent).where(UsageEvent.id == event_id)
        session.execute(statement.values(claim_token=other))
        session.commit()
    before = world.row(event_id)

    outcome = usage_billing.process_event(
        world.factory, claim, max_attempts=MAX_ATTEMPTS, clock=world.clock
    )

    assert outcome is Outcome.ABANDONED
    after = world.row(event_id)
    assert (after.status, after.claim_token, after.attempt_count) == (
        before.status,
        other,
        before.attempt_count,
    )
    assert world.usage_rows(tenant) == []
    # A failure recorded under the stale token is not written either.
    written = usage_billing.record_failure(
        world.factory,
        claim,
        code="UNEXPECTED_ERROR",
        error_type="RuntimeError",
        max_attempts=MAX_ATTEMPTS,
        clock=world.clock,
    )
    assert written is None
    assert world.row(event_id).status is UsageEventStatus.PROCESSING


def test_a_stale_processing_event_is_recovered_and_billed_once(world, caplog) -> None:
    world.publish_markup_usd()
    tenant = world.tenant()
    world.fund(tenant, OPENING)
    event_id = world.event(tenant)
    world.claim()

    # Not yet past the lease: nothing to recover.
    assert world.recover(NOW + dt.timedelta(seconds=LEASE_SECONDS) - SECOND) == 0
    later = NOW + dt.timedelta(seconds=LEASE_SECONDS + 1)
    with caplog.at_level("WARNING"):
        assert world.recover(later) == 1

    row = world.row(event_id)
    assert (row.status, row.error_code) == (UsageEventStatus.FAILED_RETRYABLE, "STALE_PROCESSING")
    assert row.next_attempt_at == later.replace(microsecond=0)
    assert row.attempt_count == 1
    assert_unclaimed(row)
    [record] = [r for r in caplog.records if r.name == "app.services.usage_billing"]
    assert record.__dict__["event_id"] == row.event_id
    assert record.__dict__["attempt_count"] == 1
    assert record.__dict__["claimed_at"] == NOW.replace(microsecond=0).isoformat()

    result = world.bill(later)

    assert result.outcomes == {Outcome.PROCESSED: 1}
    row = world.row(event_id)
    assert (row.status, row.attempt_count, row.error_code) == (
        UsageEventStatus.PROCESSED,
        2,
        None,
    )
    assert len(world.usage_rows(tenant)) == 1


def test_recovery_stops_at_the_attempt_limit(world) -> None:
    world.publish_markup_usd()
    tenant = world.tenant()
    event_id = world.event(tenant)
    moment = NOW
    for attempt in range(1, 4):
        assert len(world.claim(moment)) == 1
        moment += dt.timedelta(seconds=LEASE_SECONDS + 1)
        assert world.recover(moment, max_attempts=3) == 1
        expected = "STALE_PROCESSING" if attempt < 3 else "STALE_PROCESSING_EXHAUSTED"
        assert world.row(event_id).error_code == expected

    row = world.row(event_id)
    assert (row.status, row.attempt_count) == (UsageEventStatus.FAILED_FINAL, 3)
    assert world.claim(moment + dt.timedelta(hours=1)) == []


def test_a_future_event_is_not_claimed_until_it_happens(world) -> None:
    world.publish_markup_usd()
    tenant = world.tenant()
    world.fund(tenant, OPENING)
    event_id = world.event(tenant, occurred_at=NOW + dt.timedelta(seconds=60))

    assert world.bill().claimed == 0
    row = world.row(event_id)
    assert (row.status, row.attempt_count) == (UsageEventStatus.RECEIVED, 0)

    assert world.bill(NOW + dt.timedelta(seconds=60)).outcomes == {Outcome.PROCESSED: 1}
    assert world.row(event_id).attempt_count == 1


def test_an_event_not_yet_due_under_the_locks_goes_back_to_received(world) -> None:
    """⑤ (defensive): `now_r` taken under the three shared locks is before `occurred_at`."""
    world.publish_markup_usd()
    tenant = world.tenant()
    event_id = world.event(tenant)
    [claim] = world.claim()

    outcome = usage_billing.process_event(
        world.factory, claim, max_attempts=MAX_ATTEMPTS, clock=lambda: EVENT_AT - SECOND
    )

    assert outcome is Outcome.NOT_DUE
    row = world.row(event_id)
    assert (row.status, row.attempt_count) == (UsageEventStatus.RECEIVED, 0)
    assert_unclaimed(row)


def test_the_occurred_at_check_comes_after_the_three_shared_locks(world, monkeypatch) -> None:
    """The lock order of design §2 and the contract of 025 / T-B / T-C / T-D: provider (S) →
    fx_rate_locks (S) → pricing_rule_locks (S), only then the clock; the ledger comes last."""
    world.publish_markup_usd()
    tenant = world.tenant()
    world.fund(tenant, OPENING)
    world.event(tenant)
    [claim] = world.claim()
    calls: list[str] = []

    def recorded(name: str, original):
        def call(*args, **kwargs):
            calls.append(name)
            return original(*args, **kwargs)

        return call

    for module, name in (
        (usage_billing.catalog, "resolve_model"),
        (usage_billing.rates, "lock_fx_rates_shared"),
        (usage_billing.rules, "lock_pricing_rules_shared"),
        (usage_billing, "post_transaction"),
    ):
        monkeypatch.setattr(module, name, recorded(name, getattr(module, name)))

    def clock() -> dt.datetime:
        calls.append("now_r")
        return NOW

    outcome = usage_billing.process_event(
        world.factory, claim, max_attempts=MAX_ATTEMPTS, clock=clock
    )

    assert outcome is Outcome.PROCESSED
    assert calls[:4] == [
        "resolve_model",
        "lock_fx_rates_shared",
        "lock_pricing_rules_shared",
        "now_r",
    ]
    assert calls.count("now_r") == 1
    assert calls[-1] == "post_transaction"


# --- backoff and the limit -------------------------------------------------------------


def test_unexpected_errors_back_off_and_end_failed_final(world, monkeypatch) -> None:
    world.publish_markup_usd()
    tenant = world.tenant()
    world.fund(tenant, OPENING)
    event_id = world.event(tenant)

    def refuse(*args, **kwargs):
        raise RuntimeError("fictional failure")

    monkeypatch.setattr(usage_billing, "post_transaction", refuse)
    moment = NOW
    for attempt in range(1, MAX_ATTEMPTS + 1):
        result = world.bill(moment)
        assert result.outcomes == {Outcome.FAILED: 1}, attempt
        row = world.row(event_id)
        assert row.attempt_count == attempt
        assert (row.error_code, row.error_message) == ("UNEXPECTED_ERROR", "RuntimeError")
        assert_unclaimed(row)
        if attempt < MAX_ATTEMPTS:
            assert row.status is UsageEventStatus.FAILED_RETRYABLE
            wait = dt.timedelta(seconds=min(2**attempt, 300))
            assert row.next_attempt_at == moment.replace(microsecond=0) + wait
            # Not before the backoff is over.
            assert world.bill(row.next_attempt_at - SECOND).claimed == 0
            moment = row.next_attempt_at
        else:
            assert row.status is UsageEventStatus.FAILED_FINAL
            assert row.next_attempt_at is None
    assert world.usage_rows(tenant) == []
    assert world.bill(moment + dt.timedelta(hours=1)).claimed == 0


# --- accounts (AIH-TASK-020's contract) -------------------------------------------------


def test_a_disabled_tenant_is_billed_as_usual(world) -> None:
    world.publish_markup_usd()
    tenant = world.tenant()
    world.fund(tenant, OPENING)
    world.set_account_status(tenant, AccountStatus.DISABLED)
    event_id = world.event(tenant)

    world.bill()

    assert world.row(event_id).status is UsageEventStatus.PROCESSED
    assert len(world.usage_rows(tenant)) == 1


def test_a_closed_tenant_is_failed_final_without_a_ledger_row(world) -> None:
    world.publish_markup_usd()
    tenant = world.tenant()
    world.fund(tenant, OPENING)
    world.set_account_status(tenant, AccountStatus.CLOSED)
    event_id = world.event(tenant)

    world.bill()

    row = world.row(event_id)
    assert (row.status, row.error_code) == (UsageEventStatus.FAILED_FINAL, "ACCOUNT_CLOSED")
    _assert_unbilled(world, tenant, row)


# --- crossing zero (§7, ADR-0010), SQLite and MySQL ------------------------------------


def _transitions(world: World, tenant: Tenancy) -> tuple[int, int]:
    audits = world.count(
        AuditLog,
        AuditLog.action == AuditAction.TENANT_BILLING_STATUS_CHANGED,
        AuditLog.entity_id == tenant.public_id,
    )
    outbox = world.count(
        DomainOutbox,
        DomainOutbox.event_type == wallet_repository.EVENT_BILLING_STATUS_CHANGED,
        DomainOutbox.aggregate_id == tenant.public_id,
    )
    return audits, outbox


def test_a_debit_across_zero_suspends_in_the_same_transaction(both) -> None:
    world = both
    world.publish_audio_fixed_myr()
    tenant = world.tenant()
    world.fund(tenant, "0.0625")
    before = world.tenant_row(tenant)
    assert before.billing_status is BillingStatus.ACTIVE
    # The top-up's own transition (SUSPENDED → ACTIVE).
    assert _transitions(world, tenant) == (1, 1)
    # 2 seconds × MYR 0.0625 = 0.125.
    event_id = world.event(tenant, seconds="2")

    world.bill()

    after = world.tenant_row(tenant)
    assert world.row(event_id).billable_cost == Decimal("0.125")
    assert world.balance(tenant) == Decimal("-0.0625")
    assert after.billing_status is BillingStatus.SUSPENDED
    assert after.status_version == before.status_version + 1
    assert _transitions(world, tenant) == (2, 2)
    assert world.verify(tenant) == []


def test_exactly_zero_is_suspended_and_suspended_debits_move_nothing_else(both) -> None:
    world = both
    world.publish_audio_fixed_myr()
    tenant = world.tenant()
    world.fund(tenant, "0.0625")
    world.event(tenant, seconds="1")

    world.bill()

    suspended = world.tenant_row(tenant)
    assert world.balance(tenant) == Decimal(0)
    assert suspended.billing_status is BillingStatus.SUSPENDED
    assert _transitions(world, tenant) == (2, 2)

    world.event(tenant, seconds="1")
    world.bill(NOW + dt.timedelta(minutes=1))

    still = world.tenant_row(tenant)
    assert world.balance(tenant) == Decimal("-0.0625")
    assert (still.billing_status, still.status_version) == (
        BillingStatus.SUSPENDED,
        suspended.status_version,
    )
    assert _transitions(world, tenant) == (2, 2)
    assert len(world.usage_rows(tenant)) == 2
    assert world.verify(tenant) == []


# --- a failure in the middle of the transaction (INV-13), SQLite and MySQL -------------


def test_a_failure_after_the_debit_rolls_everything_back(both, monkeypatch) -> None:
    world = both
    world.publish_audio_fixed_myr()
    tenant = world.tenant()
    world.fund(tenant, "0.0625")
    event_id = world.event(tenant, seconds="2")
    before_tenant = world.tenant_row(tenant)
    audits, outbox = world.count(AuditLog), world.count(DomainOutbox)
    calls: list[str] = []

    def refuse(*args, **kwargs):
        # After `post_transaction` has flushed the ledger row, the transition and its rows.
        calls.append("write_snapshot")
        raise RuntimeError("the event write failed")

    monkeypatch.setattr(usage_billing, "_write_snapshot", refuse)

    result = world.bill()

    assert calls == ["write_snapshot"]
    assert result.outcomes == {Outcome.FAILED: 1}
    assert world.usage_rows(tenant) == []
    assert world.balance(tenant) == Decimal("0.0625")
    after_tenant = world.tenant_row(tenant)
    assert (after_tenant.billing_status, after_tenant.status_version) == (
        before_tenant.billing_status,
        before_tenant.status_version,
    )
    assert (world.count(AuditLog), world.count(DomainOutbox)) == (audits, outbox)
    row = world.row(event_id)
    assert (row.status, row.error_code) == (UsageEventStatus.FAILED_RETRYABLE, "UNEXPECTED_ERROR")
    assert row.wallet_transaction_id is None and row.billable_cost is None
    assert world.verify(tenant) == []

    monkeypatch.undo()
    assert row.next_attempt_at is not None
    world.bill(row.next_attempt_at)
    assert world.row(event_id).status is UsageEventStatus.PROCESSED
    assert world.balance(tenant) == Decimal("-0.0625")


# --- queue loss and a Beat outage (§114, REQ-INGEST-002) --------------------------------


def test_a_backlog_after_a_beat_outage_is_billed_in_full(world) -> None:
    """No Celery at all: the database is the to-do list. Three events were claimed by a worker
    that died; four more arrived during the outage. Recovery and one sweep bill all seven."""
    world.publish_markup_usd()
    tenant = world.tenant()
    world.fund(tenant, OPENING)
    for _ in range(3):
        world.event(tenant)
    assert len(world.claim()) == 3
    for _ in range(4):
        world.event(tenant)

    back = NOW + dt.timedelta(hours=1)
    assert world.recover(back) == 3
    result = world.bill(back)

    assert result.claimed == 7
    assert result.outcomes == {Outcome.PROCESSED: 7}
    assert world.count(UsageEvent, UsageEvent.status != UsageEventStatus.PROCESSED) == 0
    assert len(world.usage_rows(tenant)) == 7
    assert world.balance(tenant) == OPENING - 7 * MARKUP_BILLABLE


def test_a_sweep_stops_claiming_when_its_budget_is_spent(world) -> None:
    world.publish_markup_usd()
    tenant = world.tenant()
    world.fund(tenant, OPENING)
    for _ in range(usage_billing.CLAIM_BATCH + 5):
        world.event(tenant)
    ticks = iter([0.0, 0.0, 9.0])
    world.clock.now = NOW

    result = usage_billing.bill_pending_events(
        world.factory,
        lease_seconds=LEASE_SECONDS,
        max_attempts=MAX_ATTEMPTS,
        clock=world.clock,
        ticker=lambda: next(ticks),
    )

    # One batch of 20, all processed; the budget is checked before the next claim.
    assert result.claimed == usage_billing.CLAIM_BATCH
    assert result.outcomes == {Outcome.PROCESSED: usage_billing.CLAIM_BATCH}
    assert world.count(UsageEvent, UsageEvent.status == UsageEventStatus.RECEIVED) == 5


# --- sweep slots (design gate #522 v4 §7; the MySQL half is in the concurrency file) -------


def test_a_parallelism_below_one_is_refused_before_anything_is_claimed(world) -> None:
    world.publish_markup_usd()
    tenant = world.tenant()
    world.fund(tenant, OPENING)
    event_id = world.event(tenant)
    world.clock.now = NOW

    with pytest.raises(ValueError):
        usage_billing.bill_pending_events(
            world.factory,
            lease_seconds=LEASE_SECONDS,
            max_attempts=MAX_ATTEMPTS,
            clock=world.clock,
            parallelism=0,
        )

    row = world.row(event_id)
    assert (row.status, row.attempt_count) == (UsageEventStatus.RECEIVED, 0)


def test_sqlite_has_no_sweep_slots_and_bills_as_usual(world) -> None:
    world.publish_markup_usd()
    tenant = world.tenant()
    world.fund(tenant, OPENING)
    for _ in range(3):
        world.event(tenant)
    world.clock.now = NOW

    result = usage_billing.bill_pending_events(
        world.factory,
        lease_seconds=LEASE_SECONDS,
        max_attempts=MAX_ATTEMPTS,
        clock=world.clock,
        parallelism=2,
    )

    assert result.claimed == 3
    assert result.outcomes == {Outcome.PROCESSED: 3}
    assert len(world.usage_rows(tenant)) == 3


@pytest.mark.parametrize("statement", ["_GET_LOCK", "_HOLDS_LOCK", "_RELEASE_LOCK"])
def test_the_slot_statements_bind_only_the_slot(statement: str) -> None:
    # The colons inside the quoted name must not be read as bind parameters by text().
    compiled = getattr(usage_billing, statement).compile()

    assert set(compiled.params) == {"slot"}
    assert "':usage-billing-sweep:'" in str(compiled)


def test_the_task_passes_the_sweep_parallelism(monkeypatch) -> None:
    from app.core.celery_app import USAGE_BILLING_SWEEP_PARALLELISM
    from app.tasks import usage_billing as tasks

    received: list[dict[str, object]] = []

    def fake(factory: object, **kwargs: object) -> SweepResult:
        received.append(kwargs)
        return SweepResult(claimed=0)

    monkeypatch.setattr(tasks, "_session_factory", lambda: None)
    monkeypatch.setattr(usage_billing, "bill_pending_events", fake)

    assert tasks.bill_pending_events() == 0
    assert [call["parallelism"] for call in received] == [USAGE_BILLING_SWEEP_PARALLELISM]


# --- snapshot, reconciliation, tenants ---------------------------------------------------


def test_the_snapshot_reprices_to_the_same_amounts(world) -> None:
    """ADR-0005: after newer versions are published, the snapshot still gives the stored values."""
    world.publish_markup_usd()
    tenant = world.tenant()
    world.fund(tenant, OPENING)
    event_id = world.event(tenant)
    world.bill()
    world.clock.now = T0 + dt.timedelta(days=1)
    world.price(dict.fromkeys(USD_RATES, "0.5"))
    world.rule(multiplier="4")

    row = world.row(event_id)
    assert row.provider_price_version_id is not None and row.pricing_rule_id is not None
    snapshot = ChargeSnapshot(
        usage_meter_type_id=row.usage_meter_type_id,
        provider_price_version_id=row.provider_price_version_id,
        fx_rate_version_id=row.fx_rate_version_id,
        pricing_rule_id=row.pricing_rule_id,
    )
    with world.factory() as session:
        again = reprice_from_snapshot(session, snapshot, quantities_of(row))

    assert again.provider_source_cost == row.provider_source_cost
    assert again.estimated_provider_cost_myr == row.estimated_provider_cost_myr
    assert again.billable_cost == row.billable_cost
    assert again.fx_rate_applied == row.fx_rate_applied


def test_a_batch_reconciles_with_the_ledger_and_each_tenant_pays_its_own(world) -> None:
    """`verify_wallet` passes; Σ AI_USAGE = closing − opening; INV-8 across two tenants."""
    world.publish_markup_usd()
    a, b = world.tenant("a"), world.tenant("b")
    world.fund(a, OPENING)
    world.fund(b, "30")
    for _ in range(3):
        world.event(a)
    world.event(b, tokens=ZERO_TOKENS)
    world.event(b)

    world.bill()

    for tenant, opening, debits in ((a, OPENING, 3), (b, Decimal("30"), 1)):
        rows = world.usage_rows(tenant)
        assert len(rows) == debits
        assert {row.tenant_id for row in rows} == {tenant.id}
        assert sum((row.amount for row in rows), Decimal(0)) == world.balance(tenant) - opening
        assert world.verify(tenant) == []


# --- project rules (design gate #507 v4 §2「解析」, §7) --------------------------------------

# EC × 3 (the customer rule) and EC × 4 (the project rule), exact: see the hand computation above.
CUSTOMER_BILLABLE = Decimal("8.75390625")
PROJECT_BILLABLE = Decimal("11.67187500")


def test_billing_picks_the_project_rule_of_the_events_project(both) -> None:
    """The event's own project (derived from its credential, §11) selects the project rule; the
    same customer's other project stays on the customer rule. One wallet pays for both."""
    world = both
    world.publish_markup_usd()
    chatbot = world.tenant()
    api = world.project(chatbot.public_id, "API")
    world.fund(chatbot, OPENING)
    customer_rule = world.rule(scope=PricingScope.CUSTOMER, tenant=chatbot, multiplier="3")
    project_rule = world.rule(scope=PricingScope.PROJECT, tenant=api, multiplier="4")
    on_api = world.event(api)
    on_chatbot = world.event(chatbot)

    result = world.bill()

    assert result.outcomes == {Outcome.PROCESSED: 2}
    api_row, chatbot_row = world.row(on_api), world.row(on_chatbot)
    assert (api_row.project_id, chatbot_row.project_id) == (api.project_id, chatbot.project_id)
    assert api_row.pricing_rule_id == world.rule_row_id(project_rule)
    assert chatbot_row.pricing_rule_id == world.rule_row_id(customer_rule)
    assert (api_row.billable_cost, chatbot_row.billable_cost) == (
        PROJECT_BILLABLE,
        CUSTOMER_BILLABLE,
    )
    for tenancy, row in ((api, api_row), (chatbot, chatbot_row)):
        _, expected = world.expected(tenancy, row.id)
        assert row.billable_cost == expected.billable_cost
    rows = world.usage_rows(chatbot)
    assert sorted(row.amount for row in rows) == sorted([-PROJECT_BILLABLE, -CUSTOMER_BILLABLE])
    assert world.balance(chatbot) == OPENING - PROJECT_BILLABLE - CUSTOMER_BILLABLE
    assert world.verify(chatbot) == []


def test_a_project_rule_never_reprices_a_moment_already_chosen(mysql_world) -> None:
    """Design gate #507 v4 §7 "已选定的时刻不改选" (real MySQL): E1 is billed on the customer rule;
    then the project's PROJECT rule is published from `t`. E2 (`occurred_at < t`, ingested before
    the publish, billed after it) stays on the customer rule; E3 (`occurred_at ≥ t`) takes the
    project rule. E1's snapshot and ledger row do not change and reprice to the same amount."""
    world = mysql_world
    world.publish_markup_usd()
    tenant = world.tenant()
    world.fund(tenant, OPENING)
    customer_rule = world.rule(scope=PricingScope.CUSTOMER, tenant=tenant, multiplier="3")
    e1 = world.event(tenant)
    # Later than the first bill below (not yet due then), earlier than the publish's `t`.
    e2 = world.event(tenant, occurred_at=NOW + 10 * SECOND)
    world.bill(NOW)
    first = world.row(e1)
    [ledger_before] = world.usage_rows(tenant)
    assert first.status is UsageEventStatus.PROCESSED
    assert first.pricing_rule_id == world.rule_row_id(customer_rule)
    assert world.row(e2).status is UsageEventStatus.RECEIVED

    world.clock.now = NOW + 20 * SECOND
    project_rule = world.rule(scope=PricingScope.PROJECT, tenant=tenant, multiplier="4")
    with world.factory() as session:
        t = session.execute(
            select(PricingRule.effective_from).where(PricingRule.public_id == project_rule)
        ).scalar_one()
    assert t is not None
    assert first.occurred_at < t
    assert NOW + 10 * SECOND < t
    e3 = world.event(tenant, occurred_at=t + 5 * SECOND)
    world.bill(NOW + 60 * SECOND)

    again = world.row(e1)
    assert (again.pricing_rule_id, again.billable_cost) == (
        first.pricing_rule_id,
        first.billable_cost,
    )
    assert again.wallet_transaction_id == first.wallet_transaction_id
    ledger_after = world.usage_rows(tenant)
    assert [(row.id, row.amount) for row in ledger_after][0] == (
        ledger_before.id,
        ledger_before.amount,
    )
    assert first.provider_price_version_id is not None and first.pricing_rule_id is not None
    snapshot = ChargeSnapshot(
        usage_meter_type_id=first.usage_meter_type_id,
        provider_price_version_id=first.provider_price_version_id,
        fx_rate_version_id=first.fx_rate_version_id,
        pricing_rule_id=first.pricing_rule_id,
    )
    with world.factory() as session:
        repriced = reprice_from_snapshot(session, snapshot, quantities_of(first))
    assert repriced.billable_cost == first.billable_cost == CUSTOMER_BILLABLE

    second, third = world.row(e2), world.row(e3)
    assert (second.status, third.status) == (UsageEventStatus.PROCESSED, UsageEventStatus.PROCESSED)
    assert second.pricing_rule_id == world.rule_row_id(customer_rule)
    assert second.billable_cost == CUSTOMER_BILLABLE
    assert third.pricing_rule_id == world.rule_row_id(project_rule)
    assert third.billable_cost == PROJECT_BILLABLE
    assert len(ledger_after) == 3
    assert world.verify(tenant) == []

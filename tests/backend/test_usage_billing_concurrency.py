"""The billing worker against a real MySQL (design gate #181 v2 §7, AIH-TASK-032).

Rows of design §7 covered here:

- **认领** — two connections claim 40 due events at once: each exactly once, the claim fields
  complete, `attempt_count` + 1 (`FOR UPDATE SKIP LOCKED`);
- **回收不抢活的事件** — a processing transaction holds the event row past its lease; recovery
  skips it; the holder commits PROCESSED;
- **并发与崩溃** — four workers bill 200 events of one tenant; one of them dies between ⑧ and ⑨
  (its connection is dropped): that event stays PROCESSING, is recovered after its lease and
  billed once; events and ledger rows correspond one to one, `verify_wallet` passes and
  opening − Σ billable = closing;
- **锁顺序与发布并发** — while billing holds its three shared locks, publishing a price, a rule or
  an FX rate, or mapping an alias, waits for it to commit; the bill uses the tables as they were;
  the same with a project rule (design gate #507 v4), whose later events then take it;
- **锁超时** — a publisher holding `pricing_rule_locks` for more than 10 seconds: the billing
  transaction gives up after the session-level 10 seconds (not the default 50), FAILED_RETRYABLE
  `LOCK_WAIT_TIMEOUT` with its backoff;
- **认领字段 CHECK**, **快照齐全 CHECK**, **未处理 ⇒ 无账本链接**, **成本非负**, **已处理不可改**,
  **事件与账本对应**, **重新入队时账本无行**, and **重复处理** (a processed event forced back to
  RECEIVED) — direct SQL, refused by migration 0018's CHECKs and triggers.

Rows of design gate #522 v4 §7 (AIH-TASK-141, sweep slots — MySQL named locks):

- **并发：两个扫描并行、余额跨零** — two sweeps with `parallelism=2` bill 120 events of a tenant
  left with 100.5 events' worth: one ledger row each, the balance follows, SUSPENDED with
  `status_version` + 1 exactly once; **幂等重放** at its end (two more sweeps change nothing);
- **名额：抢不到就不认领**, **跨周期时第三个扫描立即返回**, **名额连接中途断开**,
  **释放失败就丢弃连接**, **扫描抛异常也释放**, **`GET_LOCK` 返回 NULL 或出错**,
  **确认时返回 0、NULL 或出错** — every case ends with both slots free.

All of it needs `BILLING_TEST_DATABASE_URL`; it skips without it — **a skip is not a pass**, CI
sets it. The world is built the way test_usage_billing.py builds it (services, frozen clock);
prices, rates and amounts are fictional exact binary fractions; uuids are zero placeholders.
"""

from __future__ import annotations

import datetime as dt
import itertools
import logging
import os
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from decimal import Decimal

import pytest
from alembic.config import Config
from sqlalchemy import Engine, create_engine, delete, func, select, text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import DBAPIError, OperationalError
from sqlalchemy.orm import Session, object_session, sessionmaker

from alembic import command
from app.core.database import create_session_factory
from app.models.ai_catalog import AiModel, AiModelAlias, AiProvider
from app.models.auth import User, UserRole, UserStatus
from app.models.integration import CredentialStatus, IntegrationCredential, ProjectStatusDelivery
from app.models.pricing_rules import PricingRule, PricingScope, PricingStrategy
from app.models.tenancy import BillingStatus, Project, Tenant
from app.models.usage import UsageEvent, UsageEventStatus
from app.models.wallet import ReferenceType, TransactionType, Wallet, WalletTransaction
from app.repositories import fx_rates as fx_repository
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
from app.services.auth import RequestContext
from app.services.usage_billing import Claim, Outcome

TEST_DATABASE_URL = os.environ.get("BILLING_TEST_DATABASE_URL", "")
TEST_EMAIL_DOMAIN = "@usage-billing-concurrency-test.example.com"
CONTEXT = RequestContext(ip_address="203.0.113.9", user_agent="usage-billing-concurrency-test")

T0 = dt.datetime(2026, 9, 29, 8, 30, 0)
EVENT_AT = dt.datetime(2026, 9, 29, 9, 30, 0, 250000)
NOW = dt.datetime(2026, 9, 29, 10, 30, 0, 500000)
LEASE_SECONDS = 120
MAX_ATTEMPTS = 10
PAST_LEASE = NOW + dt.timedelta(seconds=LEASE_SECONDS + 1)

TOKENS = {
    "input_tokens": 1000,
    "output_tokens": 2000,
    "cache_creation_input_tokens": 500,
    "cache_read_input_tokens": 3000,
}
# Fictional USD prices per 1 000 tokens and a fictional rate: see test_usage_billing.py.
USD_RATES = {
    "LLM_INPUT_TOKEN": "0.0625",
    "LLM_OUTPUT_TOKEN": "0.25",
    "LLM_CACHE_WRITE_TOKEN": "0.125",
    "LLM_CACHE_READ_TOKEN": "0.0078125",
}
FX_RATE = "4.5"
PER_THOUSAND = "1000"
MARKUP_BILLABLE = Decimal("5.83593750")
OPENING = Decimal("10000")

# MySQL error numbers.
_ER_SIGNAL_EXCEPTION = 1644
_ER_CHECK_CONSTRAINT_VIOLATED = 3819

_NUMBERS = itertools.count(1)

# Sweep slots (design gate #522 v4 §2): the production parallelism, and the same lock names the
# implementation builds (the database name keeps them apart from other databases on the server).
SLOTS = 2
_SLOT_NAME = "CONCAT(DATABASE(), ':usage-billing-sweep:', :slot)"
_GET_SLOT = text(f"SELECT GET_LOCK({_SLOT_NAME}, 0)")
_RELEASE_SLOT = text(f"SELECT RELEASE_LOCK({_SLOT_NAME})")
_SLOT_IS_FREE = text(f"SELECT IS_FREE_LOCK({_SLOT_NAME})")
_SLOT_HOLDER = text(f"SELECT IS_USED_LOCK({_SLOT_NAME})")
# Sessions waiting for a row lock on a table of this database (as _WAITING_ON_TABLE in
# test_session_revocation_concurrency.py, counted over every waiting thread).
_WAITERS_ON_TABLE = text(
    "SELECT COUNT(DISTINCT w.REQUESTING_THREAD_ID) FROM performance_schema.data_lock_waits AS w"
    " JOIN performance_schema.data_locks AS l"
    " ON l.ENGINE = w.ENGINE AND l.ENGINE_LOCK_ID = w.REQUESTING_ENGINE_LOCK_ID"
    " WHERE l.OBJECT_SCHEMA = DATABASE() AND l.OBJECT_NAME = :table_name"
)
_SLOT_LOGGER = "app.services.usage_billing"


class Clock:
    def __init__(self, moment: dt.datetime) -> None:
        self.now = moment

    def __call__(self) -> dt.datetime:
        return self.now


class Crash(BaseException):
    """A worker process dying: not an `Exception`, so nothing in the worker catches it."""


# --- the world (as in test_usage_billing.py) -------------------------------------------


class World:
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
        self.tenant_id, self.project_id, self.credential_id = self._tenant()
        self.fund(OPENING)

    def _tenant(self) -> tuple[int, int, int]:
        number = next(_NUMBERS)
        detail = customers.create_customer(
            self.factory,
            actor=self.admin,
            company_name="Fictional Hot Tenant Sdn Bhd",
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
        # Public ids, for customer and project rules.
        self.customer_public_id, self.project_public_id = detail.id, project.id
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
            session.commit()
            return tenant_id, project_id, credential.id

    def fund(self, amount: Decimal) -> None:
        with self.factory() as session:
            wallet_repository.post_transaction(
                session,
                tenant_id=self.tenant_id,
                transaction_type=TransactionType.ADJUSTMENT_CREDIT,
                amount=amount,
                reference_type=ReferenceType.ADMIN_ADJUSTMENT,
                reference_id=f"fund-{next(_NUMBERS)}",
                now=T0,
                created_by=self.admin.id,
                description="Fictional top-up for a billing test",
                actor_role="ADMIN",
            )
            session.commit()

    def price_draft(self, rates: Mapping[str, str] = USD_RATES) -> str:
        components = [
            ComponentInput.model_validate(
                {"component_code": code, "unit_quantity": PER_THOUSAND, "rate_amount": rate}
            )
            for code, rate in rates.items()
        ]
        return provider_prices.create_draft(
            self.factory,
            actor=self.admin,
            provider_id=self.provider.id,
            model_id=self.model.id,
            source_currency="USD",
            source_reference="Fictional price page",
            components=components,
            context=CONTEXT,
            clock=self.clock,
        ).id

    def publish_price(self, draft: str) -> str:
        return provider_prices.publish(
            self.factory,
            actor=self.admin,
            price_version_id=draft,
            effective_from=None,
            context=CONTEXT,
            clock=self.clock,
        ).id

    def fx_draft(self, rate: str = FX_RATE) -> str:
        return fx_rates.create_draft(
            self.factory,
            actor=self.admin,
            base_currency="USD",
            rate=Decimal(rate),
            observed_at=T0,
            source_reference="Fictional quote",
            context=CONTEXT,
            clock=self.clock,
        ).id

    def publish_fx(self, draft: str) -> str:
        return fx_rates.publish(
            self.factory,
            actor=self.admin,
            fx_rate_id=draft,
            effective_from=None,
            context=CONTEXT,
            clock=self.clock,
        ).id

    def rule_draft(self, multiplier: str = "2", scope: PricingScope = PricingScope.GLOBAL) -> str:
        """A MARKUP draft; a customer or project level is the world's tenant and project."""
        return pricing_rules.create_draft(
            self.factory,
            actor=self.admin,
            priority_scope=scope,
            customer_id=self.customer_public_id if scope.has_customer else None,
            project_id=self.project_public_id if scope.has_project else None,
            provider_id=self.provider.id if scope.has_provider else None,
            model_id=self.model.id if scope.has_model else None,
            strategy=PricingStrategy.MARKUP,
            markup_multiplier=Decimal(multiplier),
            components=None,
            context=CONTEXT,
            clock=self.clock,
        ).id

    def publish_rule(self, draft: str) -> str:
        return pricing_rules.publish(
            self.factory,
            actor=self.admin,
            rule_id=draft,
            effective_from=None,
            context=CONTEXT,
            clock=self.clock,
        ).id

    def map_alias(self, alias: str) -> None:
        ai_catalog.map_alias(
            self.factory,
            actor=self.admin,
            provider_id=self.provider.id,
            alias=alias,
            model_id=self.model.id,
            context=CONTEXT,
            clock=self.clock,
        )

    def publish_markup_usd(self) -> None:
        self.publish_price(self.price_draft())
        self.publish_fx(self.fx_draft())
        self.publish_rule(self.rule_draft())

    def event(self, occurred_at: dt.datetime = EVENT_AT) -> int:
        number = next(_NUMBERS)
        with self.factory() as session:
            meter = event_repository.find_meter_type(session, "LLM_TOKEN")
            assert meter is not None
            row = event_repository.insert_event(
                session,
                event_id=f"00000000-0000-7000-8000-{number:012d}",
                schema_version="1.0",
                tenant_id=self.tenant_id,
                project_id=self.project_id,
                integration_credential_id=self.credential_id,
                request_id=f"call-{number}",
                conversation_id=None,
                provider_code_raw="anthropic",
                model_code_raw="claude-x",
                usage_meter_type_id=meter.id,
                payload_shape=meter.payload_shape,
                quantity_kind=meter.quantity_kind,
                unit=meter.unit,
                tokens=dict(TOKENS),
                quantity=None,
                payload_fingerprint="0" * 64,
                occurred_at=occurred_at,
                now=T0,
            )
            session.commit()
            return row.id

    def events(self, count: int) -> list[int]:
        return [self.event() for _ in range(count)]

    def bill(self, at: dt.datetime, *, parallelism: int | None = None) -> usage_billing.SweepResult:
        return usage_billing.bill_pending_events(
            self.factory,
            lease_seconds=LEASE_SECONDS,
            max_attempts=MAX_ATTEMPTS,
            clock=lambda: at,
            parallelism=parallelism,
        )

    def claim(self, at: dt.datetime = NOW) -> list[Claim]:
        return usage_billing.claim_events(
            self.factory, lease_seconds=LEASE_SECONDS, clock=lambda: at
        )

    def recover(self, at: dt.datetime) -> int:
        return usage_billing.recover_stale_processing(
            self.factory, max_attempts=MAX_ATTEMPTS, clock=lambda: at
        )

    def row(self, event_id: int) -> UsageEvent:
        with self.factory() as session:
            found = session.get(UsageEvent, event_id)
            assert found is not None
            session.expunge(found)
            return found

    def rows(self) -> list[UsageEvent]:
        with self.factory() as session:
            found = list(session.execute(select(UsageEvent).order_by(UsageEvent.id)).scalars())
            session.expunge_all()
            return found

    def usage_rows(self) -> list[WalletTransaction]:
        statement = select(WalletTransaction).where(
            WalletTransaction.transaction_type == TransactionType.AI_USAGE,
            WalletTransaction.tenant_id == self.tenant_id,
        )
        with self.factory() as session:
            found = list(session.execute(statement).scalars())
            session.expunge_all()
            return found

    def balance(self) -> Decimal:
        with self.factory() as session:
            statement = select(Wallet.balance).where(Wallet.tenant_id == self.tenant_id)
            return session.execute(statement).scalar_one()

    def debit(self, amount: Decimal) -> None:
        """An admin debit, the way `fund` credits (`amount` positive, posted negative)."""
        with self.factory() as session:
            wallet_repository.post_transaction(
                session,
                tenant_id=self.tenant_id,
                transaction_type=TransactionType.ADJUSTMENT_DEBIT,
                amount=-amount,
                reference_type=ReferenceType.ADMIN_ADJUSTMENT,
                reference_id=f"debit-{next(_NUMBERS)}",
                now=T0,
                created_by=self.admin.id,
                description="Fictional debit for a billing test",
                actor_role="ADMIN",
            )
            session.commit()

    def billing_status(self) -> tuple[BillingStatus, int]:
        with self.factory() as session:
            statement = select(Tenant.billing_status, Tenant.status_version).where(
                Tenant.id == self.tenant_id
            )
            status, version = session.execute(statement).one()
            return status, version

    def bill_until_done(self, at: dt.datetime) -> None:
        """Anything backed off (a deadlock, a lock timeout) is due an hour later: bill it."""
        while self.bill(at, parallelism=SLOTS).claimed:
            pass


# --- fixtures --------------------------------------------------------------------------


def clean(engine: Engine) -> None:
    """⚠️ The database is shared: wipe this file's rows before and after every case (the same
    way test_usage_billing.py does; the lock rows of 0014 / 0015 stay)."""
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
    # Design gate #507 v4 §7「MySQL 用例的清理」: no project-level rule is left behind.
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


@pytest.fixture
def engine(monkeypatch) -> Iterator[Engine]:
    if not TEST_DATABASE_URL:
        pytest.skip("BILLING_TEST_DATABASE_URL is not set; these cases need a real MySQL")
    from app.core.config import get_settings

    monkeypatch.setenv("BILLING_DATABASE_URL", TEST_DATABASE_URL)
    get_settings.cache_clear()
    command.upgrade(Config("alembic.ini"), "head")
    get_settings.cache_clear()
    created = create_engine(TEST_DATABASE_URL, pool_size=10, max_overflow=6)
    clean(created)
    try:
        yield created
    finally:
        clean(created)
        created.dispose()


@pytest.fixture
def world(engine) -> World:
    return World(create_session_factory(engine))


def run_in_threads(*targets: Callable[[], object]) -> list[object]:
    """Start every target at once; each result is its return value or the exception it raised."""
    barrier = threading.Barrier(len(targets))
    results: list[object] = [None] * len(targets)

    def run(index: int, target: Callable[[], object]) -> None:
        barrier.wait()
        try:
            results[index] = target()
        except BaseException as error:
            results[index] = error

    threads = [
        threading.Thread(target=run, args=(index, target)) for index, target in enumerate(targets)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=120)
    return results


# --- claiming --------------------------------------------------------------------------


def test_two_connections_claim_forty_events_each_exactly_once(world) -> None:
    world.publish_markup_usd()
    events = world.events(40)

    def claim_all() -> list[Claim]:
        claimed: list[Claim] = []
        while batch := world.claim():
            claimed.extend(batch)
        return claimed

    first, second = run_in_threads(claim_all, claim_all)

    assert isinstance(first, list) and isinstance(second, list)
    ids = [claim.id for claim in [*first, *second]]
    assert sorted(ids) == sorted(events)
    assert len(set(ids)) == 40
    tokens = {claim.id: claim.token for claim in [*first, *second]}
    for row in world.rows():
        assert row.status is UsageEventStatus.PROCESSING
        assert row.claim_token == tokens[row.id]
        assert row.claimed_at == NOW.replace(microsecond=0)
        assert row.lease_expires_at == row.claimed_at + dt.timedelta(seconds=LEASE_SECONDS)
        assert row.attempt_count == 1


# --- recovery never takes an event that is being processed ------------------------------


def test_recovery_skips_an_event_whose_row_is_still_locked(world, monkeypatch) -> None:
    world.publish_markup_usd()
    event_id = world.event()
    [claim] = world.claim()
    held, release = threading.Event(), threading.Event()
    original = fx_repository.lock_fx_rates_shared

    def slow(session: Session):
        # The holder has the event row (①) and the provider row (③); it waits here.
        held.set()
        release.wait(timeout=30)
        return original(session)

    monkeypatch.setattr(fx_repository, "lock_fx_rates_shared", slow)
    outcome: list[Outcome] = []
    holder = threading.Thread(
        target=lambda: outcome.append(
            usage_billing.process_event(
                world.factory, claim, max_attempts=MAX_ATTEMPTS, clock=lambda: PAST_LEASE
            )
        )
    )
    holder.start()
    try:
        assert held.wait(timeout=30)
        # Past its lease, but its row is locked: SKIP LOCKED passes it by.
        assert world.recover(PAST_LEASE) == 0
        assert world.row(event_id).status is UsageEventStatus.PROCESSING
    finally:
        release.set()
        holder.join(timeout=60)

    assert outcome == [Outcome.PROCESSED]
    row = world.row(event_id)
    assert (row.status, row.attempt_count) == (UsageEventStatus.PROCESSED, 1)
    assert len(world.usage_rows()) == 1
    assert world.recover(PAST_LEASE + dt.timedelta(hours=1)) == 0


# --- many workers, one tenant, one crash (§117) ------------------------------------------


def test_four_workers_bill_two_hundred_events_and_survive_a_crash(world, monkeypatch) -> None:
    world.publish_markup_usd()
    events = world.events(200)
    crash_at = events[57]
    crashed: list[int] = []
    original = usage_billing._write_snapshot

    def dying(event: UsageEvent, *args, **kwargs):
        if event.id == crash_at and not crashed:
            crashed.append(event.id)
            # After ⑧, before ⑨: the connection goes away; MySQL rolls the transaction back.
            session = object_session(event)
            assert session is not None
            session.connection().invalidate()
            raise Crash
        return original(event, *args, **kwargs)

    monkeypatch.setattr(usage_billing, "_write_snapshot", dying)

    results = run_in_threads(*(lambda: world.bill(NOW) for _ in range(4)))

    assert crashed == [crash_at]
    # Three workers finished their sweeps; the fourth died with the crash.
    died = [result for result in results if not isinstance(result, usage_billing.SweepResult)]
    assert len(died) == 1 and isinstance(died[0], BaseException)
    assert world.row(crash_at).status is UsageEventStatus.PROCESSING
    # Before the lease is over nothing is recovered; after it, the dead worker's claims are.
    assert world.recover(NOW + dt.timedelta(seconds=LEASE_SECONDS - 1)) == 0
    recovered = world.recover(PAST_LEASE)
    assert recovered >= 1
    # Anything backed off (a deadlock, a lock timeout) is due an hour later as well.
    later = NOW + dt.timedelta(hours=1)
    while world.bill(later).claimed:
        pass

    rows = world.rows()
    assert {row.status for row in rows} == {UsageEventStatus.PROCESSED}
    ledger = world.usage_rows()
    assert len(ledger) == 200
    by_reference = {line.reference_id: line for line in ledger}
    assert len(by_reference) == 200
    for row in rows:
        line = by_reference[row.event_id]
        assert row.wallet_transaction_id == line.id
        assert line.amount == -row.billable_cost
    # Claimed by the worker that died, recovered, claimed again: one ledger row all the same.
    assert world.row(crash_at).attempt_count >= 2
    billed = sum((row.billable_cost for row in rows), Decimal(0))
    assert billed == 200 * MARKUP_BILLABLE
    assert world.balance() == OPENING - billed
    with world.factory() as session:
        assert wallet_repository.verify_wallet(session, world.tenant_id) == []


# --- sweep slots (design gate #522 v4 §7) --------------------------------------------------


def _slot_is_free(engine: Engine, slot: int) -> bool:
    with engine.connect() as connection:
        return connection.execute(_SLOT_IS_FREE, {"slot": slot}).scalar() == 1


def _assert_slots_free(engine: Engine) -> None:
    assert [_slot_is_free(engine, slot) for slot in range(1, SLOTS + 1)] == [True] * SLOTS


def _hold_slot(engine: Engine, slot: int) -> Connection:
    """An outside connection holding `slot`; the caller releases it (or invalidates it)."""
    connection = engine.connect()
    assert connection.execute(_GET_SLOT, {"slot": slot}).scalar() == 1
    return connection


def _let_go(connection: Connection) -> None:
    """Drop an outside connection: MySQL releases whatever named lock it still holds."""
    connection.invalidate()
    connection.close()


def _slot_warnings(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [
        record
        for record in caplog.records
        if record.name == _SLOT_LOGGER
        and record.levelno == logging.WARNING
        and "sweep slot" in record.getMessage()
    ]


def _assert_billed_once(world: World, events: list[int]) -> None:
    """Every event PROCESSED, one AI_USAGE row each (by reference), `verify_wallet` passes."""
    rows = world.rows()
    assert sorted(row.id for row in rows) == sorted(events)
    assert {row.status for row in rows} == {UsageEventStatus.PROCESSED}
    ledger = world.usage_rows()
    by_reference = {line.reference_id: line for line in ledger}
    assert len(ledger) == len(by_reference) == len(events)
    for row in rows:
        line = by_reference[row.event_id]
        assert row.wallet_transaction_id == line.id
        assert line.amount == -row.billable_cost
    with world.factory() as session:
        assert wallet_repository.verify_wallet(session, world.tenant_id) == []


def _count(world: World, status: UsageEventStatus) -> int:
    return sum(1 for row in world.rows() if row.status is status)


def _sweep(world: World, at: dt.datetime = NOW) -> usage_billing.SweepResult:
    return world.bill(at, parallelism=SLOTS)


def test_two_sweeps_bill_in_parallel_and_the_balance_crosses_zero_once(world, engine) -> None:
    """并发：两个扫描并行、余额跨零; 幂等重放 at the end."""
    world.publish_markup_usd()
    # 100.5 events' worth left: the 101st debit takes the balance from positive to ≤ 0.
    world.debit(OPENING - (100 * MARKUP_BILLABLE + MARKUP_BILLABLE / 2))
    opening = world.balance()
    assert opening == 100 * MARKUP_BILLABLE + MARKUP_BILLABLE / 2
    status_before, version_before = world.billing_status()
    assert status_before is BillingStatus.ACTIVE
    events = world.events(120)

    first, second = run_in_threads(
        lambda: world.bill(NOW, parallelism=SLOTS), lambda: world.bill(NOW, parallelism=SLOTS)
    )

    assert isinstance(first, usage_billing.SweepResult), first
    assert isinstance(second, usage_billing.SweepResult), second
    assert first.claimed >= 1 and second.claimed >= 1
    later = NOW + dt.timedelta(hours=1)
    world.bill_until_done(later)

    _assert_billed_once(world, events)
    billed = sum((row.billable_cost for row in world.rows()), Decimal(0))
    assert billed == 120 * MARKUP_BILLABLE
    assert world.balance() == opening - billed
    # Only one debit saw the balance go from positive to ≤ 0.
    assert world.billing_status() == (BillingStatus.SUSPENDED, version_before + 1)
    _assert_slots_free(engine)

    # 幂等重放: two more sweeps at once find nothing to do and change nothing.
    ledger, balance, status = len(world.usage_rows()), world.balance(), world.billing_status()
    replayed = run_in_threads(lambda: _sweep(world, later), lambda: _sweep(world, later))
    assert [getattr(result, "claimed", result) for result in replayed] == [0, 0]
    assert (len(world.usage_rows()), world.balance(), world.billing_status()) == (
        ledger,
        balance,
        status,
    )


def test_a_sweep_that_finds_every_slot_taken_claims_nothing(world, engine) -> None:
    world.publish_markup_usd()
    events = world.events(10)
    first = _hold_slot(engine, 1)
    second = _hold_slot(engine, 2)
    try:
        assert _sweep(world).claimed == 0
        rows = world.rows()
        assert [(row.status, row.attempt_count) for row in rows] == [
            (UsageEventStatus.RECEIVED, 0)
        ] * 10

        assert second.execute(_RELEASE_SLOT, {"slot": 2}).scalar() == 1
        result = _sweep(world)
        assert result.claimed == 10
        assert result.outcomes == {Outcome.PROCESSED: 10}
        # The sweep gave back the slot it took; the outside one is still held.
        assert _slot_is_free(engine, 2)
        assert not _slot_is_free(engine, 1)

        assert first.execute(_RELEASE_SLOT, {"slot": 1}).scalar() == 1
        _assert_slots_free(engine)
    finally:
        _let_go(first)
        _let_go(second)
    _assert_billed_once(world, events)


def test_a_third_sweep_returns_at_once_while_two_overrun_their_period(world, engine) -> None:
    world.publish_markup_usd()
    events = world.events(40)
    results: list[object] = [None, None]

    def run(index: int) -> None:
        try:
            results[index] = _sweep(world)
        except BaseException as error:
            results[index] = error

    threads = [threading.Thread(target=run, args=(index,)) for index in range(2)]
    with engine.connect() as holder:
        transaction = holder.begin()
        holder.execute(
            text("SELECT id FROM wallets WHERE tenant_id = :tenant FOR UPDATE"),
            {"tenant": world.tenant_id},
        )
        try:
            for thread in threads:
                thread.start()
            deadline = time.monotonic() + 30
            with engine.connect() as watcher:
                while True:
                    waiting = watcher.execute(
                        _WAITERS_ON_TABLE, {"table_name": "wallets"}
                    ).scalar_one()
                    if waiting >= 2:
                        break
                    assert time.monotonic() < deadline, "two sweeps never waited on the wallet"
                    time.sleep(0.05)
            started = time.monotonic()
            third = _sweep(world)
            elapsed = time.monotonic() - started
        finally:
            transaction.rollback()
            for thread in threads:
                thread.join(timeout=120)

    assert third.claimed == 0
    assert elapsed < 1
    assert all(isinstance(result, usage_billing.SweepResult) for result in results), results
    world.bill_until_done(NOW + dt.timedelta(hours=1))
    _assert_billed_once(world, events)
    _assert_slots_free(engine)


def test_a_sweep_whose_slot_connection_is_killed_stops(world, engine, monkeypatch) -> None:
    world.publish_markup_usd()
    events = world.events(30)
    original = usage_billing.process_event
    processed: list[int] = []

    def killing(*args, **kwargs):
        outcome = original(*args, **kwargs)
        processed.append(1)
        if len(processed) == 3:
            # The slot connection alone goes away; the processing connections are untouched.
            with engine.connect() as outside:
                holder = outside.execute(_SLOT_HOLDER, {"slot": 1}).scalar()
                assert holder is not None
                outside.execute(text(f"KILL {int(holder)}"))
        return outcome

    monkeypatch.setattr(usage_billing, "process_event", killing)

    result = _sweep(world)

    assert result.claimed == usage_billing.CLAIM_BATCH
    assert result.outcomes == {Outcome.PROCESSED: 3}
    assert _count(world, UsageEventStatus.PROCESSED) == 3
    assert _count(world, UsageEventStatus.PROCESSING) == usage_billing.CLAIM_BATCH - 3
    # MySQL released the killed connection's slot: anyone can take it now.
    taker = _hold_slot(engine, 1)
    try:
        assert taker.execute(_RELEASE_SLOT, {"slot": 1}).scalar() == 1
    finally:
        _let_go(taker)

    monkeypatch.setattr(usage_billing, "process_event", original)
    assert world.recover(PAST_LEASE) == usage_billing.CLAIM_BATCH - 3
    world.bill_until_done(NOW + dt.timedelta(hours=1))
    _assert_billed_once(world, events)
    _assert_slots_free(engine)


def test_a_failed_release_discards_the_slot_connection(world, engine, monkeypatch, caplog) -> None:
    world.publish_markup_usd()
    events = world.events(5)
    original = usage_billing._release_lock

    def refuse(connection: Connection, slot: int) -> int | None:
        raise OperationalError("SELECT RELEASE_LOCK(...)", {}, Exception("fictional failure"))

    monkeypatch.setattr(usage_billing, "_release_lock", refuse)

    result = _sweep(world)

    assert result.outcomes == {Outcome.PROCESSED: 5}
    assert len(_slot_warnings(caplog)) == 1
    # Invalidated: MySQL drops the lock with the connection.
    deadline = time.monotonic() + 1
    while not _slot_is_free(engine, 1):
        assert time.monotonic() < deadline, "the slot stayed taken after the release failed"
        time.sleep(0.05)
    # No connection in the pool still holds a slot: the next sweep takes one and bills.
    monkeypatch.setattr(usage_billing, "_release_lock", original)
    events.append(world.event())
    assert _sweep(world).outcomes == {Outcome.PROCESSED: 1}
    _assert_billed_once(world, events)
    _assert_slots_free(engine)


def test_a_sweep_that_raises_still_gives_its_slot_back(world, engine, monkeypatch) -> None:
    def broken(*args, **kwargs):
        raise RuntimeError("fictional claim failure")

    monkeypatch.setattr(usage_billing, "claim_events", broken)

    with pytest.raises(RuntimeError, match="fictional claim failure"):
        _sweep(world)

    _assert_slots_free(engine)


@pytest.mark.parametrize("failure", ["null", "error"])
def test_a_get_lock_null_or_error_skips_the_sweep(
    world, engine, monkeypatch, caplog, failure: str
) -> None:
    world.publish_markup_usd()
    world.events(10)
    calls: list[int] = []

    def failing(connection: Connection, slot: int) -> int | None:
        calls.append(slot)
        if failure == "null":
            return None
        raise OperationalError("SELECT GET_LOCK(...)", {}, Exception("fictional failure"))

    monkeypatch.setattr(usage_billing, "_get_lock", failing)

    result = _sweep(world)

    assert result.claimed == 0
    # Slot 2 is not tried after slot 1 failed.
    assert calls == [1]
    assert [(row.status, row.attempt_count) for row in world.rows()] == [
        (UsageEventStatus.RECEIVED, 0)
    ] * 10
    assert len(_slot_warnings(caplog)) == 1
    _assert_slots_free(engine)


@pytest.mark.parametrize("failure", ["zero", "null", "error"])
def test_a_slot_lost_before_an_event_stops_the_sweep(
    world, engine, monkeypatch, caplog, failure: str
) -> None:
    world.publish_markup_usd()
    events = world.events(10)
    original = usage_billing._holds_lock
    calls: list[int] = []

    def losing(connection: Connection, slot: int) -> int | None:
        calls.append(slot)
        # Before the claim, before the 1st and before the 2nd event: the real answer.
        if len(calls) <= 3:
            return original(connection, slot)
        if failure == "zero":
            return 0
        if failure == "null":
            return None
        raise OperationalError("SELECT IS_USED_LOCK(...)", {}, Exception("fictional failure"))

    monkeypatch.setattr(usage_billing, "_holds_lock", losing)

    result = _sweep(world)

    assert result.claimed == 10
    assert result.outcomes == {Outcome.PROCESSED: 2}
    assert _count(world, UsageEventStatus.PROCESSED) == 2
    assert _count(world, UsageEventStatus.PROCESSING) == 8
    assert len(_slot_warnings(caplog)) == 1
    # The named lock really held was released in `finally` all the same.
    _assert_slots_free(engine)

    monkeypatch.setattr(usage_billing, "_holds_lock", original)
    assert world.recover(PAST_LEASE) == 8
    world.bill_until_done(NOW + dt.timedelta(hours=1))
    _assert_billed_once(world, events)
    _assert_slots_free(engine)


# --- the lock order against publishers ---------------------------------------------------


@pytest.mark.parametrize("publisher", ["price", "rule", "fx", "alias"])
def test_a_publisher_waits_for_billing_and_billing_uses_the_old_tables(
    world, monkeypatch, publisher: str
) -> None:
    world.publish_markup_usd()
    event_id = world.event()
    [claim] = world.claim()
    # Drafts first: they do not take the publish locks.
    drafts = {
        "price": world.price_draft(dict.fromkeys(USD_RATES, "0.5")),
        "rule": world.rule_draft("4"),
        "fx": world.fx_draft("8"),
    }

    def publish() -> object:
        if publisher == "price":
            return world.publish_price(drafts["price"])
        if publisher == "rule":
            return world.publish_rule(drafts["rule"])
        if publisher == "fx":
            return world.publish_fx(drafts["fx"])
        world.map_alias("claude-latest")
        return "mapped"

    held, release = threading.Event(), threading.Event()
    original = usage_billing.price_resolved

    def paused(*args, **kwargs):
        # ⑦: the three shared locks are held from ③–⑥ until the commit.
        held.set()
        release.wait(timeout=30)
        return original(*args, **kwargs)

    monkeypatch.setattr(usage_billing, "price_resolved", paused)
    world.clock.now = NOW
    outcome: list[Outcome] = []
    billing = threading.Thread(
        target=lambda: outcome.append(
            usage_billing.process_event(
                world.factory, claim, max_attempts=MAX_ATTEMPTS, clock=lambda: NOW
            )
        )
    )
    published: list[object] = []
    publishing = threading.Thread(target=lambda: published.append(publish()))
    billing.start()
    try:
        assert held.wait(timeout=30)
        publishing.start()
        time.sleep(1.5)
        # Blocked on the exclusive lock while billing holds the shared one.
        assert publishing.is_alive()
        assert published == []
    finally:
        release.set()
        billing.join(timeout=60)
        publishing.join(timeout=60)

    assert outcome == [Outcome.PROCESSED]
    assert len(published) == 1
    # Priced with the tables as they were before the publish.
    assert world.row(event_id).billable_cost == MARKUP_BILLABLE


def _rule_row(world: World, public_id: str) -> PricingRule:
    with world.factory() as session:
        found = session.execute(
            select(PricingRule).where(PricingRule.public_id == public_id)
        ).scalar_one()
        session.expunge(found)
        return found


def test_a_project_rule_publish_waits_for_billing_and_later_events_take_it(
    world, monkeypatch
) -> None:
    """Design gate #507 v4 §7 "并发：发布与计费", the project-level case of the lock test above:
    while billing holds the shared rule lock, publishing a PROJECT rule of the event's project
    waits for its commit; the event keeps the rule selected before the publish (the global one);
    an event of the same project at or after `t` takes the project rule."""
    world.publish_markup_usd()
    event_id = world.event()
    [claim] = world.claim()
    draft = world.rule_draft("4", scope=PricingScope.PROJECT)
    held, release = threading.Event(), threading.Event()
    original = usage_billing.price_resolved

    def paused(*args, **kwargs):
        held.set()
        release.wait(timeout=30)
        return original(*args, **kwargs)

    monkeypatch.setattr(usage_billing, "price_resolved", paused)
    world.clock.now = NOW
    outcome: list[Outcome] = []
    billing = threading.Thread(
        target=lambda: outcome.append(
            usage_billing.process_event(
                world.factory, claim, max_attempts=MAX_ATTEMPTS, clock=lambda: NOW
            )
        )
    )
    published: list[str] = []
    publishing = threading.Thread(target=lambda: published.append(world.publish_rule(draft)))
    billing.start()
    try:
        assert held.wait(timeout=30)
        publishing.start()
        time.sleep(1.5)
        assert publishing.is_alive()
        assert published == []
    finally:
        release.set()
        billing.join(timeout=60)
        publishing.join(timeout=60)
    monkeypatch.setattr(usage_billing, "price_resolved", original)

    assert outcome == [Outcome.PROCESSED]
    [project_rule_id] = published
    project_rule = _rule_row(world, project_rule_id)
    assert PricingScope(project_rule.priority_scope) is PricingScope.PROJECT
    assert project_rule.project_id == world.project_id
    first = world.row(event_id)
    assert first.pricing_rule_id != project_rule.id
    assert first.billable_cost == MARKUP_BILLABLE
    t = project_rule.effective_from
    assert t is not None
    assert first.occurred_at < t

    later = world.event(occurred_at=t + dt.timedelta(seconds=1))
    assert world.bill(t + dt.timedelta(seconds=10)).outcomes == {Outcome.PROCESSED: 1}
    second = world.row(later)
    assert second.pricing_rule_id == project_rule.id
    # EC × 4 = 2.91796875 × 4.
    assert second.billable_cost == Decimal("11.67187500")
    assert world.row(event_id).pricing_rule_id == first.pricing_rule_id


def test_a_publisher_holding_its_lock_past_10_seconds_times_billing_out(world, engine) -> None:
    world.publish_markup_usd()
    event_id = world.event()
    with engine.connect() as publisher:
        transaction = publisher.begin()
        publisher.execute(text("SELECT id FROM pricing_rule_locks WHERE id = 1 FOR UPDATE"))
        started = time.monotonic()
        try:
            result = world.bill(NOW)
        finally:
            elapsed = time.monotonic() - started
            transaction.rollback()

    assert result.outcomes == {Outcome.FAILED: 1}
    # The session-level 10 seconds, not InnoDB's default 50.
    assert 9 <= elapsed < 40
    row = world.row(event_id)
    assert (row.status, row.error_code) == (UsageEventStatus.FAILED_RETRYABLE, "LOCK_WAIT_TIMEOUT")
    assert row.next_attempt_at == NOW.replace(microsecond=0) + dt.timedelta(seconds=2)
    assert world.usage_rows() == []

    assert world.bill(NOW + dt.timedelta(seconds=2)).outcomes == {Outcome.PROCESSED: 1}


# --- migration 0018's CHECKs and triggers (direct SQL) ------------------------------------


def _error(connection: Connection, statement: str, **values: object) -> tuple[int, str]:
    """Run `statement` inside a savepoint; the MySQL error number and message it raised."""
    savepoint = connection.begin_nested()
    with pytest.raises(DBAPIError) as raised:
        connection.execute(text(statement), values)
    savepoint.rollback()
    errno, message = raised.value.orig.args[:2]
    return int(errno), str(message)


def _ok(connection: Connection, statement: str, **values: object) -> None:
    connection.execute(text(statement), values)


@pytest.fixture
def billed(world) -> tuple[World, int, int]:
    """One PROCESSED (billed) event and one RECEIVED event, committed."""
    world.publish_markup_usd()
    processed = world.event()
    world.bill(NOW)
    received = world.event()
    return world, processed, received


@pytest.fixture
def connection(engine) -> Iterator[Connection]:
    with engine.connect() as opened:
        transaction = opened.begin()
        try:
            yield opened
        finally:
            transaction.rollback()


_SET_SNAPSHOT = (
    "UPDATE usage_events SET status = 'PROCESSED', provider_id = :provider,"
    " model_id = :model, provider_price_version_id = :price, pricing_rule_id = :rule,"
    " provider_source_currency = :currency, provider_source_cost = 0,"
    " estimated_provider_cost_myr = 0, billable_cost = :billable,"
    " fx_rate_version_id = :fx, fx_rate_applied = :rate, wallet_transaction_id = :ledger,"
    " billing_mode_snapshot = 'PREPAID', processed_at = :now WHERE id = :id"
)


def _snapshot(row: UsageEvent, **changes: object) -> dict[str, object]:
    values: dict[str, object] = {
        "provider": row.provider_id,
        "model": row.model_id,
        "price": row.provider_price_version_id,
        "rule": row.pricing_rule_id,
        "currency": "USD",
        "billable": Decimal(0),
        "fx": row.fx_rate_version_id,
        "rate": row.fx_rate_applied,
        "ledger": None,
        "now": NOW.replace(microsecond=0),
    }
    values.update(changes)
    return values


_CLAIM = (
    "UPDATE usage_events SET status = 'PROCESSING', claim_token = :token,"
    " claimed_at = :claimed, lease_expires_at = :lease WHERE id = :id"
)


def test_the_claim_fields_check(billed, connection) -> None:
    _world, _processed, received = billed
    token = "00000000-0000-4000-8000-000000000001"
    claimed, lease = NOW.replace(microsecond=0), PAST_LEASE.replace(microsecond=0)
    without_token = "UPDATE usage_events SET status = 'PROCESSING' WHERE id = :id"
    token_only = "UPDATE usage_events SET claim_token = :token WHERE id = :id"

    refused = [
        # PROCESSING without a token.
        _error(connection, without_token, id=received),
        # A token without PROCESSING.
        _error(connection, token_only, token=token, id=received),
        # The lease ends before the claim.
        _error(connection, _CLAIM, token=token, claimed=lease, lease=claimed, id=received),
        # A token but no lease.
        _error(connection, _CLAIM, token=token, claimed=claimed, lease=None, id=received),
    ]

    assert [errno for errno, _ in refused] == [_ER_CHECK_CONSTRAINT_VIOLATED] * 4
    assert all("ck_usage_events_claim_fields" in message for _, message in refused)
    # Complete and ordered: accepted (and rolled back with the transaction).
    _ok(connection, _CLAIM, token=token, claimed=claimed, lease=lease, id=received)


def test_the_snapshot_check_with_the_myr_rule(billed, connection) -> None:
    world, processed, received = billed
    row = world.row(processed)

    missing_rule = _error(connection, _SET_SNAPSHOT, **_snapshot(row, rule=None), id=received)
    usd_without_fx = _error(
        connection, _SET_SNAPSHOT, **_snapshot(row, fx=None, rate=None), id=received
    )
    myr_with_fx = _error(connection, _SET_SNAPSHOT, **_snapshot(row, currency="MYR"), id=received)
    myr_half = _error(
        connection, _SET_SNAPSHOT, **_snapshot(row, currency="MYR", fx=None), id=received
    )

    for errno, message in (missing_rule, usd_without_fx, myr_with_fx, myr_half):
        assert errno == _ER_CHECK_CONSTRAINT_VIOLATED
        assert "ck_usage_events_processed_snapshot" in message
    # Complete: accepted. (Billable 0: no ledger row needed.)
    myr = _snapshot(row, currency="MYR", fx=None, rate=None)
    _ok(connection, _SET_SNAPSHOT, **myr, id=received)


def test_no_ledger_link_before_processing_and_no_negative_costs(billed, connection) -> None:
    world, _processed, received = billed
    # The top-up's ledger row: linked to no event, so only the CHECK can refuse it.
    with world.factory() as session:
        statement = select(WalletTransaction.id).where(
            WalletTransaction.tenant_id == world.tenant_id,
            WalletTransaction.transaction_type == TransactionType.ADJUSTMENT_CREDIT,
        )
        ledger = session.execute(statement).scalar_one()

    linked = _error(
        connection,
        "UPDATE usage_events SET wallet_transaction_id = :ledger WHERE id = :id",
        ledger=ledger,
        id=received,
    )
    assert linked[0] == _ER_CHECK_CONSTRAINT_VIOLATED
    assert "ck_usage_events_unprocessed_ledger" in linked[1]
    for column, check in (
        ("billable_cost", "ck_usage_events_billable_cost"),
        ("provider_source_cost", "ck_usage_events_provider_source_cost"),
        ("estimated_provider_cost_myr", "ck_usage_events_estimated_cost"),
    ):
        errno, message = _error(
            connection, f"UPDATE usage_events SET {column} = -0.0625 WHERE id = :id", id=received
        )
        assert (errno, check in message) == (_ER_CHECK_CONSTRAINT_VIOLATED, True), column


@pytest.mark.parametrize(
    "assignment",
    ["error_code = 'EDITED'", "billable_cost = 0", "status = 'RECEIVED'", "attempt_count = 0"],
)
def test_a_processed_event_refuses_every_update(billed, connection, assignment: str) -> None:
    """INV-6 / INV-11; also §114 Duplicate: forcing it back to RECEIVED is refused."""
    _world, processed, _received = billed

    errno, message = _error(
        connection, f"UPDATE usage_events SET {assignment} WHERE id = :id", id=processed
    )

    assert (errno, message) == (_ER_SIGNAL_EXCEPTION, "a processed usage event is immutable")


def test_a_billed_event_must_link_its_own_ledger_row(billed, connection) -> None:
    world, processed, received = billed
    row = world.row(processed)
    # A ledger row for the RECEIVED event (only a defect would post it), committed before the
    # connection under test reads anything.
    reference = world.row(received).event_id
    with world.factory() as session:
        own = wallet_repository.post_transaction(
            session,
            tenant_id=world.tenant_id,
            transaction_type=TransactionType.AI_USAGE,
            amount=Decimal("-0.0625"),
            reference_type=ReferenceType.USAGE_EVENT,
            reference_id=reference,
            now=T0,
        ).transaction.id
        session.commit()

    # Another event's ledger row, with the right amount.
    foreign = _error(
        connection,
        _SET_SNAPSHOT,
        **_snapshot(row, billable=MARKUP_BILLABLE, ledger=row.wallet_transaction_id),
        id=received,
    )
    # No ledger row at all.
    missing = _error(
        connection, _SET_SNAPSHOT, **_snapshot(row, billable=MARKUP_BILLABLE), id=received
    )
    # Its own ledger row, with another amount.
    wrong_amount = _error(
        connection,
        _SET_SNAPSHOT,
        **_snapshot(row, billable=MARKUP_BILLABLE, ledger=own),
        id=received,
    )

    message = "a billed usage event must link its own ledger row"
    assert foreign == missing == wrong_amount == (_ER_SIGNAL_EXCEPTION, message)
    _ok(
        connection,
        _SET_SNAPSHOT,
        **_snapshot(row, billable=Decimal("0.0625"), ledger=own),
        id=received,
    )


def test_requeue_is_refused_when_the_ledger_has_the_event(world, connection) -> None:
    """A LEDGER_CONFLICT event (FAILED_FINAL with a ledger row) cannot go back to RECEIVED;
    a FAILED_FINAL event without one can."""
    world.publish_markup_usd()
    conflicted, clean_failure = world.event(), world.event()
    with world.factory() as session:
        wallet_repository.post_transaction(
            session,
            tenant_id=world.tenant_id,
            transaction_type=TransactionType.AI_USAGE,
            amount=Decimal("-0.0625"),
            reference_type=ReferenceType.USAGE_EVENT,
            reference_id=world.row(conflicted).event_id,
            now=T0,
        )
        session.commit()
        final = (
            "UPDATE usage_events SET status = 'FAILED_FINAL',"
            " error_code = 'AMOUNT_OUT_OF_RANGE' WHERE id = :id"
        )
        session.execute(text(final), {"id": clean_failure})
        session.commit()
    world.bill(NOW)
    assert world.row(conflicted).error_code == "LEDGER_CONFLICT"

    requeue = "UPDATE usage_events SET status = 'RECEIVED', error_code = NULL WHERE id = :id"
    errno, message = _error(connection, requeue, id=conflicted)

    assert (errno, message) == (
        _ER_SIGNAL_EXCEPTION,
        "a usage event with a ledger row cannot be requeued",
    )
    _ok(connection, requeue, id=clean_failure)
    received = select(func.count()).where(UsageEvent.status == UsageEventStatus.RECEIVED)
    assert connection.execute(received).scalar_one() == 1

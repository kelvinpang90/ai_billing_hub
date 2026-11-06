"""Usage, snapshot, self-checks 2 and 3, generating a statement (AIH-TASK-156, gate #565 v6).

Rows of design §7 marked 156 covered here:

- **正常路径** and **快照键集合** — the whole snapshot equals hand-computed values
  field by field (key sets compared exactly, nested ones too), and the row's columns;
- **链式连续** — three months in a row, each opening the previous closing; with the
  `created_at` before `occ` rows across both month edges;
- **生成后才提交** — a row committed after December was generated goes to January;
- **零收费事件** — processed one second before and at the cutoff;
- **用量统计** — two projects, three models of two providers, with and without
  `conversation_id`, an event whose tokens are NULL; groups add up to the totals and
  sort by code point, not by collation;
- **自检** — a processed event inserted with a `billable_cost` other than its ledger
  row's amount (check 2; inserted that way, as the MySQL trigger refuses changing a
  processed event), and check 3;
- **幂等**, **上期缺失**, **合格性**, **两个租户**, **只读**, statement number format;
- **税务说明** — the version in effect at the cutoff, versions changed between `end`
  and the cutoff and after it, no version at the cutoff, a `treatment_json` failing its
  check on read, then a covering version published;
- **金额精度** and **并发** — MySQL only.

Ledger rows go through `post_transaction` (test_statement_ledger.py's `Books`); events
are inserted directly, already `PROCESSED`. Tax policy versions are written the way
test_tax_policy_db.py does: inserted as drafts, then published, truncated or retired.
The MySQL half needs `BILLING_TEST_DATABASE_URL`; it skips without it — **a skip is not
a pass**, CI sets it. On MySQL, `monthly_statements` and `tax_policy_versions` are
truncated with foreign key checks off before and after every case (their triggers
refuse DELETE). SQL text is never asserted; the idempotency case only looks for the
ledger and event table names in what was sent.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import logging
import re
import threading
import time
import uuid
from collections.abc import Iterator, Mapping
from decimal import Decimal
from typing import Any

import pytest
from alembic.config import Config
from sqlalchemy import Engine, create_engine, event, func, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker
from test_statement_ledger import COSTS, OPENING, USAGE, Books
from test_usage_billing import _NUMBERS as BILLING_NUMBERS
from test_usage_billing import (
    CONTEXT,
    T0,
    TEST_DATABASE_URL,
    TEST_EMAIL_DOMAIN,
    TOKENS,
    Tenancy,
    _sqlite_factory,
    clean,
)

from alembic import command
from app.core.database import create_session_factory
from app.models.ai_catalog import AiModel, AiProvider
from app.models.statements import MonthlyStatement, StatementStatus
from app.models.tax_policy import (
    TaxJurisdiction,
    TaxPolicyStatus,
    TaxPolicyType,
    TaxPolicyVersion,
)
from app.models.tenancy import AccountStatus, BillingMode, Tenant
from app.models.usage import UsageEvent, UsageEventStatus
from app.models.wallet import TransactionType, Wallet, WalletTransaction
from app.repositories import usage_events as usage_repository
from app.services import ai_catalog, customers, statements
from app.services.statements import (
    GENERATION_DELAY,
    GenerateResult,
    PreviousStatementMissing,
    StatementIntegrityError,
    StatementNotDue,
    StatementNotEligible,
    StatementPeriod,
    StatementTaxPolicyUnavailable,
    generate_statement,
    statement_period,
)

SECOND = dt.timedelta(seconds=1)
HOUR = dt.timedelta(hours=1)
DAY = dt.timedelta(days=1)

NOVEMBER = statement_period("2026-11")
DECEMBER = statement_period("2026-12")
JANUARY = statement_period("2027-01")
FEBRUARY = statement_period("2027-02")

# The first tax policy starts long before any period here (Kuala Lumpur 2026-10-17 00:00).
TAX_FROM = dt.datetime(2026, 10, 16, 16)
NOTICE = {
    "zh": "价格为最终价，已包含所有费用，不另收任何税费",
    "en": "Prices are final and include all charges. No tax is charged separately.",
}
NOTICE_B = {"zh": "虚构的第二版税务说明", "en": "Fictional second tax notice."}
NOTICE_C = {"zh": "虚构的第三版税务说明", "en": "Fictional third tax notice."}

STATEMENT_NUMBER = re.compile(r"ST-[0-9]{6}-[0-9A-HJKMNP-TV-Z]{8}")

SNAPSHOT_KEYS = {
    "schema_version",
    "period",
    "currency",
    "company_name",
    "tax_notice",
    "opening_balance",
    "total_topup",
    "total_ai_usage",
    "total_adjustments",
    "prior_period",
    "closing_balance",
    "request_count",
    "conversation_count",
    "input_tokens",
    "output_tokens",
    "total_customer_charges",
    "by_provider",
    "by_model",
    "by_project",
}
GROUP_COUNT_KEYS = {"request_count", "input_tokens", "output_tokens", "charges"}

_RESERVED_LOG_ATTRS = frozenset(
    set(vars(logging.LogRecord("", 0, "", 0, "", None, None))) | {"message", "asctime", "taskName"}
)

# Design §7「并发」: the second session really waits on the unique key (as
# test_session_revocation_concurrency.py checks it: performance_schema, not INNODB_TRX).
_WAITING_ON_TABLE = text(
    "SELECT COUNT(*) FROM performance_schema.data_lock_waits AS w"
    " JOIN performance_schema.threads AS t ON t.THREAD_ID = w.REQUESTING_THREAD_ID"
    " JOIN performance_schema.data_locks AS l"
    " ON l.ENGINE = w.ENGINE AND l.ENGINE_LOCK_ID = w.REQUESTING_ENGINE_LOCK_ID"
    " WHERE t.PROCESSLIST_ID = :connection_id"
    " AND l.OBJECT_SCHEMA = DATABASE() AND l.OBJECT_NAME = :table_name"
)
_WAIT_LIMIT_SECONDS = 10.0
_POLL_SECONDS = 0.05


def due(period: StatementPeriod) -> dt.datetime:
    return period.cutoff + GENERATION_DELAY


def money(value: str | Decimal) -> str:
    return format(Decimal(value).quantize(Decimal("0.00000001")), "f")


def counts(requests: int, inputs: int, outputs: int, charges: str) -> dict[str, Any]:
    return {
        "request_count": requests,
        "input_tokens": inputs,
        "output_tokens": outputs,
        "charges": money(charges),
    }


def tokens(inputs: int, outputs: int) -> dict[str, int]:
    return {
        "input_tokens": inputs,
        "output_tokens": outputs,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
    }


# --- the world ------------------------------------------------------------------------


class Statements(Books):
    """test_statement_ledger's books, plus events of any shape, tax policies and generation."""

    def __init__(self, factory: sessionmaker[Session], *, taxed: bool = True) -> None:
        super().__init__(factory)
        self._catalog: dict[tuple[str, str], tuple[int, int]] = {}
        self.tax_version = self.publish_tax(TAX_FROM) if taxed else None

    # -- the catalog --

    def catalog(self, provider: str = "anthropic", model: str = "claude-x") -> tuple[int, int]:
        """`(provider_id, model_id)`; models other than the world's are created once."""
        if (provider, model) == ("anthropic", "claude-x"):
            return self.world.provider_row_id(), self.world.model_row_id()
        if (provider, model) not in self._catalog:
            world = self.world
            with self.factory() as session:
                existing = session.execute(
                    select(AiProvider.public_id).where(AiProvider.code == provider)
                ).scalar_one_or_none()
            if existing is None:
                existing = ai_catalog.create_provider(
                    self.factory,
                    actor=world.admin,
                    code=provider,
                    display_name="Fictional provider",
                    context=CONTEXT,
                    clock=world.clock,
                ).id
            created = ai_catalog.create_model(
                self.factory,
                actor=world.admin,
                provider_id=existing,
                code=model,
                display_name="Fictional model",
                context=CONTEXT,
                clock=world.clock,
            )
            with self.factory() as session:
                provider_id, model_id = session.execute(
                    select(AiModel.provider_id, AiModel.id).where(AiModel.public_id == created.id)
                ).one()
            self._catalog[(provider, model)] = (int(provider_id), int(model_id))
        return self._catalog[(provider, model)]

    # -- events --

    def _event(
        self,
        session: Session,
        tenant: Tenancy,
        *,
        event_id: str,
        ledger_id: int | None,
        billable: Decimal,
        processed_at: dt.datetime,
        occurred_at: dt.datetime,
        conversation: str | None,
        project: Tenancy | None,
        catalog: tuple[int, int] | None,
        token_fields: Mapping[str, int] | None,
        seconds: str | None,
    ) -> UsageEvent:
        number = next(BILLING_NUMBERS)
        meter = usage_repository.find_meter_type(
            session, "LLM_TOKEN" if seconds is None else "AUDIO_SECOND"
        )
        assert meter is not None
        provider_id, model_id = catalog if catalog is not None else self.catalog()
        on = project if project is not None else tenant
        # Audio carries no token columns at all (NULL, counted as 0).
        fields: dict[str, int | None] = dict.fromkeys(TOKENS)
        if seconds is None:
            fields.update(token_fields if token_fields is not None else TOKENS)
        row = UsageEvent(
            public_id=str(uuid.uuid4()),
            event_id=event_id,
            schema_version="1.0",
            tenant_id=tenant.id,
            project_id=on.project_id,
            integration_credential_id=on.credential_id,
            request_id=f"call-{number}",
            conversation_id=conversation,
            provider_code_raw="fictional",
            model_code_raw="fictional",
            usage_meter_type_id=meter.id,
            payload_shape=meter.payload_shape,
            quantity_kind=meter.quantity_kind,
            unit=meter.unit,
            **fields,
            quantity=None if seconds is None else Decimal(seconds),
            payload_fingerprint="0" * 64,
            status=UsageEventStatus.PROCESSED,
            occurred_at=occurred_at,
            received_at=T0,
            created_at=T0,
            **{**self.snapshot(), "provider_id": provider_id, "model_id": model_id},
            **COSTS,
            billable_cost=billable,
            billing_mode_snapshot=BillingMode.PREPAID.value,
            reference_customer_price=None,
            processed_at=processed_at,
            wallet_transaction_id=ledger_id,
        )
        session.add(row)
        return row

    def charge(
        self,
        tenant: Tenancy,
        created_at: dt.datetime,
        occurred_at: dt.datetime,
        amount: str = USAGE,
        *,
        billable: str | None = None,
        conversation: str | None = None,
        project: Tenancy | None = None,
        catalog: tuple[int, int] | None = None,
        token_fields: Mapping[str, int] | None = None,
        seconds: str | None = None,
    ) -> int:
        """An `AI_USAGE` ledger row and its processed event (`billable_cost` = −amount unless
        `billable` says otherwise). Returns the ledger row id."""
        ledger_id = self.post(
            tenant, TransactionType.AI_USAGE, amount, created_at, occurred_at, event=False
        )
        with self.factory() as session:
            reference = session.execute(
                select(WalletTransaction.reference_id).where(WalletTransaction.id == ledger_id)
            ).scalar_one()
            self._event(
                session,
                tenant,
                event_id=reference,
                ledger_id=ledger_id,
                billable=-Decimal(amount) if billable is None else Decimal(billable),
                processed_at=created_at,
                occurred_at=occurred_at,
                conversation=conversation,
                project=project,
                catalog=catalog,
                token_fields=token_fields,
                seconds=seconds,
            )
            session.commit()
        return ledger_id

    def free(
        self,
        tenant: Tenancy,
        processed_at: dt.datetime,
        occurred_at: dt.datetime,
        *,
        conversation: str | None = None,
        project: Tenancy | None = None,
        catalog: tuple[int, int] | None = None,
        token_fields: Mapping[str, int] | None = None,
    ) -> None:
        """A processed event with `billable_cost = 0` and no ledger row."""
        number = next(BILLING_NUMBERS)
        with self.factory() as session:
            self._event(
                session,
                tenant,
                event_id=f"00000000-0000-7000-8000-{number:012d}",
                ledger_id=None,
                billable=Decimal(0),
                processed_at=processed_at,
                occurred_at=occurred_at,
                conversation=conversation,
                project=project,
                catalog=catalog,
                token_fields=token_fields,
                seconds=None,
            )
            session.commit()

    # -- tax policies (the writes of test_tax_policy_db.py, through the ORM) --

    def publish_tax(
        self,
        start: dt.datetime,
        notice: Mapping[str, str] = NOTICE,
        *,
        treatment: dict[str, Any] | None = None,
    ) -> int:
        """A MY / SST version inserted as a draft, then published from `start` without an end."""
        admin = self.world.admin.id
        with self.factory() as session:
            row = TaxPolicyVersion(
                public_id=str(uuid.uuid4()),
                jurisdiction=TaxJurisdiction.MY,
                policy_type=TaxPolicyType.SST,
                rate=None,
                treatment_json=treatment
                if treatment is not None
                else {
                    "schema_version": 1,
                    "sst_registered": False,
                    "tax_charged": False,
                    "document_notice": dict(notice),
                },
                status=TaxPolicyStatus.DRAFT,
                effective_from=None,
                effective_to=None,
                source_reference="Fictional policy for a statement test",
                created_by=admin,
                approved_by=None,
                approved_at=None,
                created_at=T0,
                updated_at=T0,
            )
            session.add(row)
            session.flush()
            row.status = TaxPolicyStatus.PUBLISHED
            row.effective_from = start
            row.approved_by = admin
            row.approved_at = T0
            session.commit()
            return int(row.id)

    def end_tax(self, version: int, end: dt.datetime) -> None:
        """Truncate a published version at `end`."""
        with self.factory() as session:
            session.execute(
                update(TaxPolicyVersion)
                .where(TaxPolicyVersion.id == version)
                .values(effective_to=end, updated_at=T0)
            )
            session.commit()

    def retire_tax(self, version: int) -> None:
        """Retire a published version with an empty period: it never matches again."""
        with self.factory() as session:
            session.execute(
                update(TaxPolicyVersion)
                .where(TaxPolicyVersion.id == version)
                .values(
                    status=TaxPolicyStatus.RETIRED,
                    effective_to=TaxPolicyVersion.effective_from,
                    updated_at=T0,
                )
            )
            session.commit()

    # -- generating and reading back --

    def generate(
        self,
        tenant: Tenancy,
        period: StatementPeriod,
        *,
        now: dt.datetime | None = None,
        generated_by: int | None = None,
    ) -> GenerateResult:
        """`generate_statement` in a transaction of its own, committed."""
        with self.factory() as session:
            result = generate_statement(
                session,
                tenant.id,
                period,
                now=due(period) if now is None else now,
                generated_by=generated_by,
            )
            session.commit()
            return result

    def statements(self, tenant: Tenancy | None = None) -> list[MonthlyStatement]:
        query = select(MonthlyStatement).order_by(MonthlyStatement.id)
        if tenant is not None:
            query = query.where(MonthlyStatement.tenant_id == tenant.id)
        with self.factory() as session:
            return list(session.execute(query).scalars())

    def customer_at(self, name: str, now: dt.datetime) -> Tenancy:
        """A customer (and its wallet) created at `now`; no project, no money."""
        detail = customers.create_customer(
            self.factory,
            actor=self.world.admin,
            company_name=f"Fictional {name} Sdn Bhd",
            email=f"tenant-{name}-{next(BILLING_NUMBERS)}{TEST_EMAIL_DOMAIN}",
            context=CONTEXT,
            now=now,
        )
        with self.factory() as session:
            tenant_id = session.execute(
                select(Tenant.id).where(Tenant.public_id == detail.id)
            ).scalar_one()
        return Tenancy(detail.id, int(tenant_id), 0, 0)


# --- fixtures ---------------------------------------------------------------------------


def _wipe(engine: Engine) -> None:
    """Statements and tax policy versions refuse DELETE (triggers) and reference the tenants and
    users `clean` deletes: TRUNCATE them first, with foreign key checks off."""
    with engine.begin() as connection:
        connection.exec_driver_sql("SET FOREIGN_KEY_CHECKS = 0")
        try:
            connection.exec_driver_sql("TRUNCATE TABLE monthly_statements")
            connection.exec_driver_sql("TRUNCATE TABLE tax_policy_versions")
        finally:
            connection.exec_driver_sql("SET FOREIGN_KEY_CHECKS = 1")
    clean(engine)


def _mysql_factory(monkeypatch: pytest.MonkeyPatch) -> Iterator[sessionmaker[Session]]:
    if not TEST_DATABASE_URL:
        pytest.skip("BILLING_TEST_DATABASE_URL is not set; the MySQL half needs a real MySQL")
    from app.core.config import get_settings

    monkeypatch.setenv("BILLING_DATABASE_URL", TEST_DATABASE_URL)
    get_settings.cache_clear()
    command.upgrade(Config("alembic.ini"), "head")
    get_settings.cache_clear()
    engine = create_engine(TEST_DATABASE_URL)
    _wipe(engine)
    try:
        yield create_session_factory(engine)
    finally:
        _wipe(engine)
        engine.dispose()


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
def books(factory) -> Statements:
    """SQLite and a real MySQL, with a tax policy in effect from before every period."""
    return Statements(factory)


@pytest.fixture
def untaxed(factory) -> Statements:
    """SQLite and a real MySQL, without any tax policy."""
    return Statements(factory, taxed=False)


@pytest.fixture
def mysql_books(mysql_factory) -> Statements:
    return Statements(mysql_factory)


def _error_records(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == statements.__name__]


def _extras(record: logging.LogRecord) -> dict[str, object]:
    return {k: v for k, v in vars(record).items() if k not in _RESERVED_LOG_ATTRS}


# --- the normal path and the snapshot key set (§7「正常路径」「快照键集合」) ---------------


def test_the_snapshot_equals_the_hand_computed_statement_key_by_key(books) -> None:
    tenant = books.tenant()
    books.post(tenant, TransactionType.TOPUP, "50", DECEMBER.start + DAY)
    books.charge(
        tenant,
        DECEMBER.start + 2 * DAY,
        DECEMBER.start + 2 * DAY - HOUR,
        "-0.25",
        conversation="conv-1",
    )
    books.charge(tenant, DECEMBER.start + 3 * DAY, DECEMBER.start + 3 * DAY - HOUR, "-0.5")
    books.post(tenant, TransactionType.ADJUSTMENT_DEBIT, "-2", DECEMBER.start + 4 * DAY)
    books.post(tenant, TransactionType.BONUS, "1", DECEMBER.start + 5 * DAY)
    # November usage billed after November's cutoff: a prior-period adjustment.
    books.charge(tenant, DECEMBER.start + 6 * DAY, DECEMBER.start - 5 * DAY, "-0.125")
    books.post(tenant, TransactionType.REBILL_DEBIT, "-0.0625", DECEMBER.start + 7 * DAY)
    books.free(tenant, DECEMBER.start + 8 * DAY, DECEMBER.start + 8 * DAY, conversation="conv-1")
    # January: not this statement.
    books.post(tenant, TransactionType.TOPUP, "8", JANUARY.start + DAY)
    high, _ = books.wallet(tenant)
    now = due(DECEMBER) + 5 * HOUR

    result = books.generate(tenant, DECEMBER, now=now)

    statement = result.statement
    assert result.created is True
    snapshot = statement.snapshot_json
    usage_line = counts(3, 3000, 6000, "0.75")
    expected = {
        "schema_version": 1,
        "period": "2026-12",
        "currency": "MYR",
        "company_name": "Fictional a Sdn Bhd",
        "tax_notice": NOTICE,
        "opening_balance": money(OPENING),
        "total_topup": money("50"),
        "total_ai_usage": money("-0.75"),
        "total_adjustments": money("-1"),
        "prior_period": {
            "usage": [{"usage_period": "2026-11", "request_count": 1, "amount": money("-0.125")}],
            "rebill": money("-0.0625"),
            "total": money("-0.1875"),
        },
        "closing_balance": money(OPENING + Decimal("48.0625")),
        "request_count": 3,
        "conversation_count": 1,
        "input_tokens": 3000,
        "output_tokens": 6000,
        "total_customer_charges": money("0.75"),
        "by_provider": [{"provider_code": "anthropic", **usage_line}],
        "by_model": [{"provider_code": "anthropic", "model_code": "claude-x", **usage_line}],
        "by_project": [
            {
                "project_public_id": tenant.project_public_id,
                "project_name": "Chatbot",
                **usage_line,
            }
        ],
    }
    assert set(snapshot) == SNAPSHOT_KEYS
    assert set(snapshot["tax_notice"]) == {"zh", "en"}
    assert set(snapshot["prior_period"]) == {"usage", "rebill", "total"}
    assert set(snapshot["prior_period"]["usage"][0]) == {"usage_period", "request_count", "amount"}
    assert set(snapshot["by_provider"][0]) == {"provider_code"} | GROUP_COUNT_KEYS
    assert set(snapshot["by_model"][0]) == {"provider_code", "model_code"} | GROUP_COUNT_KEYS
    assert (
        set(snapshot["by_project"][0]) == {"project_public_id", "project_name"} | GROUP_COUNT_KEYS
    )
    for key in SNAPSHOT_KEYS:
        assert snapshot[key] == expected[key], key
    assert snapshot == expected

    # The row (the one in the database, read back).
    [stored] = books.statements(tenant)
    assert stored.id == statement.id
    assert stored.snapshot_json == expected
    assert stored.status is StatementStatus.FINALIZED
    assert stored.finalized_at == stored.created_at == now
    assert stored.generated_by is None
    assert stored.ledger_high_sequence == high
    assert (stored.period_start, stored.period_end, stored.cutoff_at) == (
        DECEMBER.start,
        DECEMBER.end,
        DECEMBER.cutoff,
    )
    assert stored.tax_policy_version_id == books.tax_version
    assert STATEMENT_NUMBER.fullmatch(stored.statement_number)
    assert stored.statement_number.startswith("ST-202612-")
    uuid.UUID(stored.public_id)


def test_an_admin_generation_records_the_admin(books) -> None:
    tenant = books.tenant()

    result = books.generate(tenant, DECEMBER, generated_by=books.world.admin.id)

    assert result.created is True
    assert books.statements(tenant)[0].generated_by == books.world.admin.id


# --- chains and late rows (§7「链式连续」「生成后才提交」「`created_at` 早于 `occ` 跨月」) ---


def test_three_months_in_a_row_each_open_with_the_last_closing(books) -> None:
    tenant = books.tenant()
    books.post(tenant, TransactionType.TOPUP, "10", DECEMBER.start + DAY)
    books.charge(tenant, DECEMBER.start + 2 * DAY, DECEMBER.start + 2 * DAY, "-0.25")
    # Booked 90 s before January began, occurred 120 s into January.
    books.charge(tenant, JANUARY.start - 90 * SECOND, JANUARY.start + 120 * SECOND, "-0.0625")
    # December usage billed after December's cutoff.
    books.charge(tenant, DECEMBER.cutoff + HOUR, DECEMBER.end - HOUR, "-0.125")
    books.charge(tenant, JANUARY.start + 3 * DAY, JANUARY.start + 3 * DAY, "-0.5")
    # Booked 90 s before February began, occurred 120 s into February.
    books.charge(tenant, JANUARY.end - 90 * SECOND, JANUARY.end + 120 * SECOND, "-0.0625")
    books.post(tenant, TransactionType.ADJUSTMENT_CREDIT, "1", FEBRUARY.start + DAY)
    high, balance = books.wallet(tenant)

    december = books.generate(tenant, DECEMBER).statement.snapshot_json
    january = books.generate(tenant, JANUARY).statement.snapshot_json
    february = books.generate(tenant, FEBRUARY).statement.snapshot_json

    assert december["opening_balance"] == money(OPENING)
    assert january["opening_balance"] == december["closing_balance"]
    assert february["opening_balance"] == january["closing_balance"]
    assert february["closing_balance"] == money(balance)
    assert december["closing_balance"] == money(OPENING + Decimal("9.75"))
    assert (december["request_count"], december["total_ai_usage"]) == (1, money("-0.25"))
    assert (january["request_count"], january["total_ai_usage"]) == (2, money("-0.5625"))
    assert january["prior_period"]["usage"] == [
        {"usage_period": "2026-12", "request_count": 1, "amount": money("-0.125")}
    ]
    assert (february["request_count"], february["total_ai_usage"]) == (1, money("-0.0625"))
    assert february["total_adjustments"] == money("1")
    assert [s.ledger_high_sequence for s in books.statements(tenant)] == [high, high, high]


def test_a_row_committed_after_generation_goes_to_the_next_statement(books) -> None:
    tenant = books.tenant()
    books.charge(tenant, DECEMBER.start + DAY, DECEMBER.start + DAY, "-0.125")
    december = books.generate(tenant, DECEMBER).statement
    # Booked before December's cutoff, committed only after December was generated.
    books.charge(tenant, DECEMBER.cutoff - HOUR, DECEMBER.end - HOUR, "-0.25")

    january = books.generate(tenant, JANUARY).statement.snapshot_json

    [stored, _] = books.statements(tenant)
    assert stored.snapshot_json == december.snapshot_json
    assert stored.ledger_high_sequence == december.ledger_high_sequence
    assert december.snapshot_json["request_count"] == 1
    assert january["opening_balance"] == december.snapshot_json["closing_balance"]
    assert january["prior_period"]["usage"] == [
        {"usage_period": "2026-12", "request_count": 1, "amount": money("-0.25")}
    ]
    assert (january["request_count"], january["total_customer_charges"]) == (0, money(0))
    assert january["closing_balance"] == money(books.wallet(tenant)[1])


# --- zero-cost events (§7「零收费事件」) --------------------------------------------------


def test_a_zero_cost_event_counts_only_when_processed_before_the_cutoff(books) -> None:
    tenant = books.tenant()
    books.charge(tenant, DECEMBER.start + DAY, DECEMBER.start + DAY, "-0.25")
    books.free(tenant, DECEMBER.cutoff - SECOND, DECEMBER.end - HOUR, token_fields=tokens(10, 20))
    books.free(tenant, DECEMBER.cutoff, DECEMBER.end - HOUR, token_fields=tokens(40, 80))

    snapshot = books.generate(tenant, DECEMBER).statement.snapshot_json

    assert snapshot["request_count"] == 2
    assert (snapshot["input_tokens"], snapshot["output_tokens"]) == (1010, 2020)
    assert snapshot["total_customer_charges"] == money("0.25")
    assert snapshot["total_ai_usage"] == money("-0.25")
    assert snapshot["closing_balance"] == money(OPENING - Decimal("0.25"))


# --- usage statistics (§7「用量统计」) ----------------------------------------------------


def test_usage_is_counted_by_provider_model_and_project_in_code_point_order(books) -> None:
    tenant = books.tenant()
    chatbot = tenant
    # Code point order puts "Chatbot" first; a case-insensitive collation would not.
    analytics = books.world.project(tenant.public_id, "analytics")
    claude_x = books.catalog()
    claude_y = books.catalog("anthropic", "claude-y")
    gpt_x = books.catalog("openai", "gpt-x")
    at = DECEMBER.start + DAY
    books.charge(
        tenant,
        at,
        at - HOUR,
        "-0.25",
        conversation="c-1",
        project=chatbot,
        catalog=claude_x,
        token_fields=tokens(100, 200),
    )
    books.charge(
        tenant,
        at + HOUR,
        at,
        "-0.5",
        conversation="c-1",
        project=chatbot,
        catalog=gpt_x,
        token_fields=tokens(300, 400),
    )
    books.charge(
        tenant,
        at + 2 * HOUR,
        at,
        "-1",
        project=analytics,
        catalog=claude_y,
        token_fields=tokens(1000, 2000),
    )
    books.free(
        tenant,
        at + 3 * HOUR,
        at,
        conversation="c-2",
        project=analytics,
        catalog=claude_x,
        token_fields=tokens(10, 20),
    )
    # Audio: no token columns (NULL counts as 0); "C-1" is another conversation than "c-1".
    books.charge(
        tenant,
        at + 4 * HOUR,
        at,
        "-0.0625",
        conversation="C-1",
        project=analytics,
        catalog=gpt_x,
        seconds="5",
    )

    snapshot = books.generate(tenant, DECEMBER).statement.snapshot_json

    assert snapshot["request_count"] == 5
    assert snapshot["conversation_count"] == 3
    assert (snapshot["input_tokens"], snapshot["output_tokens"]) == (1410, 2620)
    assert snapshot["total_customer_charges"] == money("1.8125")
    assert snapshot["by_provider"] == [
        {"provider_code": "anthropic", **counts(3, 1110, 2220, "1.25")},
        {"provider_code": "openai", **counts(2, 300, 400, "0.5625")},
    ]
    assert snapshot["by_model"] == [
        {"provider_code": "anthropic", "model_code": "claude-x", **counts(2, 110, 220, "0.25")},
        {"provider_code": "anthropic", "model_code": "claude-y", **counts(1, 1000, 2000, "1")},
        {"provider_code": "openai", "model_code": "gpt-x", **counts(2, 300, 400, "0.5625")},
    ]
    assert snapshot["by_project"] == [
        {
            "project_public_id": chatbot.project_public_id,
            "project_name": "Chatbot",
            **counts(2, 400, 600, "0.75"),
        },
        {
            "project_public_id": analytics.project_public_id,
            "project_name": "analytics",
            **counts(3, 1010, 2020, "1.0625"),
        },
    ]
    for group in ("by_provider", "by_model", "by_project"):
        lines = snapshot[group]
        assert sum(line["request_count"] for line in lines) == snapshot["request_count"]
        assert sum(line["input_tokens"] for line in lines) == snapshot["input_tokens"]
        assert sum(line["output_tokens"] for line in lines) == snapshot["output_tokens"]
        assert (
            money(sum(Decimal(line["charges"]) for line in lines))
            == (snapshot["total_customer_charges"])
        )


# --- self-checks 2 and 3 (§7「自检」) -----------------------------------------------------


def test_charges_that_differ_from_the_ledger_fail_check_2_and_write_nothing(books, caplog) -> None:
    tenant = books.tenant()
    books.charge(tenant, DECEMBER.start + DAY, DECEMBER.start + DAY, "-0.25")
    # Inserted already PROCESSED with the wrong `billable_cost` (MySQL refuses changing it later).
    books.charge(
        tenant, DECEMBER.start + 2 * DAY, DECEMBER.start + 2 * DAY, "-0.25", billable="0.5"
    )

    with caplog.at_level(logging.ERROR, logger=statements.__name__):
        with pytest.raises(StatementIntegrityError) as raised:
            books.generate(tenant, DECEMBER)

    assert raised.value.check == "2"
    assert "0.5" not in str(raised.value)
    [record] = _error_records(caplog)
    assert record.levelno == logging.ERROR
    assert record.getMessage() == "Statement integrity check failed"
    assert _extras(record) == {"customer_id": tenant.public_id, "period": "2026-12", "check": "2"}
    assert books.statements() == []


def test_a_charged_event_count_that_differs_from_the_ledger_fails_check_3(
    books, caplog, monkeypatch
) -> None:
    tenant = books.tenant()
    books.charge(tenant, DECEMBER.start + DAY, DECEMBER.start + DAY, "-0.25")
    real = statements.usage_section

    def one_more(*args: Any, **kwargs: Any) -> statements.UsageSection:
        found = real(*args, **kwargs)
        return dataclasses.replace(found, charged_event_count=found.charged_event_count + 1)

    # Patched rather than built from data: a usage ledger row without its event would
    # trip self-check 1 (`missing_event`) before self-check 3 is reached.
    monkeypatch.setattr(statements, "usage_section", one_more)

    with caplog.at_level(logging.ERROR, logger=statements.__name__):
        with pytest.raises(StatementIntegrityError) as raised:
            books.generate(tenant, DECEMBER)

    assert raised.value.check == "3"
    [record] = _error_records(caplog)
    assert _extras(record) == {"customer_id": tenant.public_id, "period": "2026-12", "check": "3"}
    assert books.statements() == []


# --- idempotency, previous period, eligibility (§7「幂等」「上期缺失」「合格性」) ---------------


def test_generating_twice_returns_the_row_without_reading_the_ledger(books) -> None:
    tenant = books.tenant()
    books.charge(tenant, DECEMBER.start + DAY, DECEMBER.start + DAY, "-0.25")
    first = books.generate(tenant, DECEMBER).statement
    books.charge(tenant, DECEMBER.start + 2 * DAY, DECEMBER.start + 2 * DAY, "-0.25")

    sent: list[str] = []

    def record(conn, cursor, statement, parameters, context, executemany) -> None:
        sent.append(statement.lower())

    engine = books.factory.kw["bind"]
    event.listen(engine, "before_cursor_execute", record)
    try:
        second = books.generate(tenant, DECEMBER, now=due(DECEMBER) + 30 * DAY)
    finally:
        event.remove(engine, "before_cursor_execute", record)

    assert second.created is False
    assert second.statement.id == first.id
    assert second.statement.snapshot_json == first.snapshot_json
    assert not [s for s in sent if "wallet_transactions" in s or "usage_events" in s]
    [stored] = books.statements()
    assert stored.snapshot_json == first.snapshot_json


def test_a_missing_previous_statement_is_refused(books) -> None:
    tenant = books.tenant()

    with pytest.raises(PreviousStatementMissing):
        books.generate(tenant, JANUARY)

    assert books.statements() == []
    books.generate(tenant, DECEMBER)
    assert books.generate(tenant, JANUARY).created is True


def test_an_internal_metered_tenant_gets_no_statement(books) -> None:
    tenant = books.tenant()
    with books.factory() as session:
        session.execute(
            update(Tenant)
            .where(Tenant.id == tenant.id)
            .values(billing_mode=BillingMode.INTERNAL_METERED_ONLY)
        )
        session.commit()

    with pytest.raises(StatementNotEligible):
        books.generate(tenant, DECEMBER)

    assert books.statements() == []


def test_a_wallet_created_after_the_period_ended_gets_no_statement_for_it(books) -> None:
    tenant = books.customer_at("late", DECEMBER.end + DAY)

    with pytest.raises(StatementNotEligible):
        books.generate(tenant, DECEMBER)
    # December is not eligible, so January needs no December statement.
    january = books.generate(tenant, JANUARY).statement.snapshot_json

    assert january["opening_balance"] == january["closing_balance"] == money(0)
    assert january["company_name"] == "Fictional late Sdn Bhd"
    assert january["by_provider"] == january["by_model"] == january["by_project"] == []
    assert len(books.statements()) == 1


def test_a_disabled_account_still_gets_its_statement(books) -> None:
    tenant = books.tenant()
    books.world.set_account_status(tenant, AccountStatus.DISABLED)

    assert books.generate(tenant, DECEMBER).created is True


def test_a_period_before_the_first_or_not_yet_due_is_not_generated(books) -> None:
    tenant = books.tenant()

    with pytest.raises(StatementNotDue):
        books.generate(tenant, NOVEMBER, now=due(FEBRUARY))
    with pytest.raises(StatementNotDue):
        books.generate(tenant, DECEMBER, now=due(DECEMBER) - SECOND)
    assert books.statements() == []

    assert books.generate(tenant, DECEMBER, now=due(DECEMBER)).created is True


# --- two tenants, read-only, the number (§7「两个租户」「只读」) ------------------------------


def test_two_tenants_each_get_only_their_own_rows_and_events(books) -> None:
    ours, theirs = books.tenant("a"), books.tenant("b", opening=Decimal("500"))
    for tenant, amount in ((ours, "-0.25"), (theirs, "-0.5")):
        books.post(tenant, TransactionType.TOPUP, "4", DECEMBER.start + DAY)
        books.charge(tenant, DECEMBER.start + 2 * DAY, DECEMBER.start + 2 * DAY, amount)
        books.free(tenant, DECEMBER.start + 3 * DAY, DECEMBER.start + 3 * DAY)

    our = books.generate(ours, DECEMBER).statement.snapshot_json
    their = books.generate(theirs, DECEMBER).statement.snapshot_json

    assert (our["company_name"], their["company_name"]) == (
        "Fictional a Sdn Bhd",
        "Fictional b Sdn Bhd",
    )
    assert our["opening_balance"] == money(OPENING)
    assert their["opening_balance"] == money("500")
    assert (our["total_ai_usage"], their["total_ai_usage"]) == (money("-0.25"), money("-0.5"))
    assert our["closing_balance"] == money(OPENING + Decimal("3.75"))
    assert their["closing_balance"] == money(Decimal("503.5"))
    assert our["request_count"] == their["request_count"] == 2
    assert our["by_project"][0]["project_public_id"] == ours.project_public_id
    assert their["by_project"][0]["project_public_id"] == theirs.project_public_id
    assert len(books.statements(ours)) == len(books.statements(theirs)) == 1


def test_generation_leaves_wallets_and_the_ledger_untouched(books) -> None:
    tenant = books.tenant()
    books.post(tenant, TransactionType.TOPUP, "4", DECEMBER.start + DAY)
    books.charge(tenant, DECEMBER.start + 2 * DAY, DECEMBER.start + 2 * DAY)
    books.charge(tenant, DECEMBER.cutoff + HOUR, DECEMBER.end - HOUR)

    def tables() -> tuple[list[tuple[object, ...]], list[tuple[object, ...]]]:
        with books.factory() as session:
            wallets = [
                tuple(row) for row in session.execute(select(Wallet.__table__).order_by(Wallet.id))
            ]
            ledger = [
                tuple(row)
                for row in session.execute(
                    select(WalletTransaction.__table__).order_by(WalletTransaction.id)
                )
            ]
            return wallets, ledger

    before = tables()
    books.generate(tenant, DECEMBER)
    books.generate(tenant, JANUARY)

    assert tables() == before


def test_statement_numbers_carry_the_period_month_and_eight_crockford_characters(books) -> None:
    tenants = [books.tenant(name) for name in ("a", "b", "c")]

    numbers = [
        books.generate(tenant, DECEMBER, now=due(FEBRUARY)).statement.statement_number
        for tenant in tenants
    ]

    for number in numbers:
        assert STATEMENT_NUMBER.fullmatch(number), number
        # The period's month, not the month of `now` (February 2027).
        assert number.startswith("ST-202612-"), number
    assert len(set(numbers)) == 3


# --- the tax notice (§7「税务说明」) ------------------------------------------------------


def test_the_version_in_effect_at_the_cutoff_is_frozen_into_the_statement(untaxed) -> None:
    books = untaxed
    tenant = books.tenant()
    first = books.publish_tax(TAX_FROM, NOTICE)
    # Changed between December's end and its cutoff: December takes this one.
    books.end_tax(first, DECEMBER.end + HOUR)
    second = books.publish_tax(DECEMBER.end + HOUR, NOTICE_B)
    # Changed again after the cutoff but before December is generated: the version in
    # effect at `now` differs from the one at the cutoff, and December still takes the latter.
    after_cutoff = DECEMBER.cutoff + SECOND
    assert after_cutoff < due(DECEMBER)
    books.end_tax(second, after_cutoff)
    third = books.publish_tax(after_cutoff, NOTICE_C)

    december = books.generate(tenant, DECEMBER, now=due(DECEMBER)).statement

    assert december.snapshot_json["tax_notice"] == NOTICE_B
    assert december.tax_policy_version_id == second

    # January's cutoff falls in the new version: January takes it, December stays.
    january = books.generate(tenant, JANUARY).statement

    stored = books.statements(tenant)[0]
    assert stored.snapshot_json == december.snapshot_json
    assert stored.tax_policy_version_id == second
    assert january.snapshot_json["tax_notice"] == NOTICE_C
    assert january.tax_policy_version_id == third


def _assert_tax_unavailable(books: Statements, tenant: Tenancy, caplog) -> None:
    with caplog.at_level(logging.ERROR, logger=statements.__name__):
        with pytest.raises(StatementTaxPolicyUnavailable):
            books.generate(tenant, DECEMBER)
    [record] = _error_records(caplog)
    assert record.levelno == logging.ERROR
    assert record.getMessage() == "Statement tax policy unavailable"
    assert _extras(record) == {"customer_id": tenant.public_id, "period": "2026-12"}
    assert books.statements() == []


def test_no_version_at_the_cutoff_refuses_until_one_is_published(untaxed, caplog) -> None:
    books = untaxed
    tenant = books.tenant()
    # In effect until December's end only: nothing at the cutoff.
    first = books.publish_tax(TAX_FROM, NOTICE)
    books.end_tax(first, DECEMBER.end)

    _assert_tax_unavailable(books, tenant, caplog)

    covering = books.publish_tax(DECEMBER.end, NOTICE_B)
    december = books.generate(tenant, DECEMBER).statement
    assert december.tax_policy_version_id == covering
    assert december.snapshot_json["tax_notice"] == NOTICE_B


def test_a_treatment_failing_its_check_refuses_until_a_good_one_is_published(
    untaxed, caplog
) -> None:
    books = untaxed
    tenant = books.tenant()
    broken = {
        "schema_version": 1,
        "sst_registered": False,
        "tax_charged": False,
        # Trailing whitespace: `parse_treatment` refuses it on read.
        "document_notice": {"zh": NOTICE["zh"], "en": NOTICE["en"] + " "},
    }
    bad = books.publish_tax(TAX_FROM, treatment=broken)

    _assert_tax_unavailable(books, tenant, caplog)

    books.retire_tax(bad)
    good = books.publish_tax(TAX_FROM, NOTICE)
    december = books.generate(tenant, DECEMBER).statement
    assert december.tax_policy_version_id == good
    assert december.snapshot_json["tax_notice"] == NOTICE


# --- MySQL only: precision and concurrency (§7「金额精度」「并发」) -------------------------


def test_eight_decimal_amounts_are_written_exactly_on_mysql(mysql_books) -> None:
    books = mysql_books
    tenant = books.tenant(opening=Decimal("100.12345678"))
    books.post(tenant, TransactionType.TOPUP, "0.00000001", DECEMBER.start + DAY)
    books.charge(tenant, DECEMBER.start + 2 * DAY, DECEMBER.start + 2 * DAY, "-0.33333333")
    books.charge(tenant, DECEMBER.start + 3 * DAY, DECEMBER.start + 3 * DAY, "-1.98765432")
    books.charge(tenant, DECEMBER.start + 4 * DAY, DECEMBER.start - DAY, "-0.00000007")
    books.post(tenant, TransactionType.SYSTEM_CORRECTION, "-0.00000003", DECEMBER.start + 5 * DAY)
    books.post(tenant, TransactionType.REBILL_DEBIT, "-0.11111111", DECEMBER.start + 6 * DAY)

    snapshot = books.generate(tenant, DECEMBER).statement.snapshot_json

    assert snapshot["opening_balance"] == "100.12345678"
    assert snapshot["total_topup"] == "0.00000001"
    assert snapshot["total_ai_usage"] == "-2.32098765"
    assert snapshot["total_adjustments"] == "-0.00000003"
    assert snapshot["prior_period"] == {
        "usage": [{"usage_period": "2026-11", "request_count": 1, "amount": "-0.00000007"}],
        "rebill": "-0.11111111",
        "total": "-0.11111118",
    }
    assert snapshot["closing_balance"] == "97.69135793" == money(books.wallet(tenant)[1])
    assert snapshot["total_customer_charges"] == "2.32098765"
    assert snapshot["by_model"][0]["charges"] == "2.32098765"


def test_two_sessions_generating_the_same_statement_leave_one_row(mysql_books) -> None:
    books = mysql_books
    tenant = books.tenant()
    books.charge(tenant, DECEMBER.start + DAY, DECEMBER.start + DAY, "-0.25")
    connection_ids: list[int] = []
    outcome: list[BaseException | None] = []
    started = threading.Event()

    def second() -> None:
        with books.factory() as session:
            connection_ids.append(int(session.execute(text("SELECT CONNECTION_ID()")).scalar_one()))
            started.set()
            try:
                generate_statement(session, tenant.id, DECEMBER, now=due(DECEMBER))
            except Exception as error:
                # Handed to the main thread.
                outcome.append(error)
                session.rollback()
            else:
                outcome.append(None)

    first = books.factory()
    worker = threading.Thread(target=second, daemon=True)
    try:
        assert generate_statement(first, tenant.id, DECEMBER, now=due(DECEMBER)).created
        worker.start()
        assert started.wait(_WAIT_LIMIT_SECONDS), "the second session never started"
        deadline = time.monotonic() + _WAIT_LIMIT_SECONDS
        while True:
            with books.factory() as probe:
                waiting = probe.execute(
                    _WAITING_ON_TABLE,
                    {"connection_id": connection_ids[0], "table_name": "monthly_statements"},
                ).scalar_one()
            if waiting:
                break
            assert time.monotonic() < deadline, "the second INSERT never waited"
            time.sleep(_POLL_SECONDS)
        first.commit()
    finally:
        first.close()
        worker.join(_WAIT_LIMIT_SECONDS * 3)

    assert not worker.is_alive()
    [error] = outcome
    assert isinstance(error, IntegrityError)
    with books.factory() as session:
        rows = session.execute(
            select(func.count())
            .select_from(MonthlyStatement)
            .where(MonthlyStatement.tenant_id == tenant.id)
        ).scalar_one()
    assert rows == 1

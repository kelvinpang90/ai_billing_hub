"""Statement periods, ledger attribution and the ledger section (AIH-TASK-155, gate #565 v6 §7).

Rows of design §7 marked 155 covered here:

- **账期边界** — 2026-12, 2027-02 against 2028-02, label ranges,
  due one second either side of `cutoff + 1h`, `due_periods` empty and over several periods;
- **归属谓词** — a table of `AI_USAGE` and other rows around the month edges,
  including rows booked before their event occurred:
  `attributed_through` in SQL equals `attributed_month` row by row;
- **`created_at` 早于 `occ` 跨月** — `end(M) − 90s` booked, `occ = end(M) + 120s`,
  and the mirror at `start(M)`: three consecutive periods, self-check passes,
  each lands in the right period;
- **扫描完整性** — a seeded batch over three months: `later_rows`, `S(M)`,
  closing and first-period opening equal a full scan judged row by row
  with `attributed_month` (ids and amounts);
- **正常路径**, **宽限日**, **晚到** (also two months back),
  **生成后才提交**, **首期**, **两个租户**;
- **自检** — an `AI_USAGE` row without an event in each ledger-side range,
  and an opening that does not add up: `StatementIntegrityError`,
  one error log carrying no amount;
- **只读** — `wallets` and `wallet_transactions` are unchanged by the calls;
- **金额精度** — 8-decimal amounts summed exactly
  (MySQL only: SQLite keeps DECIMAL as a float).

Ledger rows go through `post_transaction`; each `AI_USAGE` row's processed event is inserted
directly with `wallet_transaction_id` filled in (the world of test_usage_billing.py, whose SQLite
factory installs the test trigger that moves the wallet). On SQLite every amount is an exact binary
fraction. The MySQL half needs `BILLING_TEST_DATABASE_URL`; it skips without it — **a skip is not a
pass**, CI sets it. SQL text is never asserted.
"""

from __future__ import annotations

import datetime as dt
import logging
import random
import uuid
from collections.abc import Iterator
from decimal import Decimal
from typing import NamedTuple

import pytest
from sqlalchemy import not_, select
from sqlalchemy.orm import Session, sessionmaker
from test_usage_billing import _NUMBERS as BILLING_NUMBERS
from test_usage_billing import (
    T0,
    TOKENS,
    Tenancy,
    World,
    _mysql_factory,
    _sqlite_factory,
)

from app.models.fx_rates import FxRateVersion
from app.models.pricing_rules import PricingRule
from app.models.provider_prices import ProviderPriceVersion
from app.models.tenancy import BillingMode
from app.models.usage import UsageEvent, UsageEventStatus
from app.models.wallet import (
    ADJUSTMENT_REFERENCE_TYPES,
    REFERENCE_TYPE_FOR,
    ReferenceType,
    TransactionType,
    Wallet,
    WalletTransaction,
)
from app.repositories import usage_events as usage_repository
from app.repositories import wallet as wallet_repository
from app.services import statements
from app.services.statements import (
    FIRST_STATEMENT_PERIOD,
    GENERATION_DELAY,
    LedgerSection,
    PriorUsage,
    StatementIntegrityError,
    StatementPeriod,
    attributed_month,
    attributed_through,
    due_periods,
    is_due,
    later_ids,
    ledger_section,
    period_containing,
    previous_period,
    statement_ids,
    statement_period,
)

SECOND = dt.timedelta(seconds=1)
HOUR = dt.timedelta(hours=1)
DAY = dt.timedelta(days=1)

DECEMBER = statement_period("2026-12")
JANUARY = statement_period("2027-01")
FEBRUARY = statement_period("2027-02")

# Plenty, so no debit here ever crosses zero (that would suspend the tenant).
OPENING = Decimal("1000")
USAGE = "-0.0625"

# Snapshot cost columns: exact binary fractions (SQLite keeps NUMERIC as a float).
COSTS = {
    "provider_source_cost": Decimal("0.03125"),
    "fx_rate_applied": Decimal("4.5"),
    "estimated_provider_cost_myr": Decimal("0.140625"),
}

_RESERVED_LOG_ATTRS = frozenset(
    set(vars(logging.LogRecord("", 0, "", 0, "", None, None))) | {"message", "asctime", "taskName"}
)


# --- the world ------------------------------------------------------------------------


class Line(NamedTuple):
    """One ledger row as a full scan reads it, with its event's `occurred_at` (or None)."""

    id: int
    sequence: int
    kind: TransactionType
    amount: Decimal
    balance_after: Decimal
    created_at: dt.datetime
    occurred_at: dt.datetime | None

    @property
    def month(self) -> str:
        return attributed_month(self.kind, self.created_at, self.occurred_at)


class Books:
    """test_usage_billing's world, plus ledger rows of any type and processed events."""

    def __init__(self, factory: sessionmaker[Session]) -> None:
        self.factory = factory
        self.world = World(factory)
        self._snapshot: dict[str, object] | None = None

    def snapshot(self) -> dict[str, object]:
        """One published USD price, rate and rule: the references of every processed event."""
        if self._snapshot is None:
            price = self.world.price()
            rate = self.world.fx()
            rule = self.world.rule()
            provider_id = self.world.provider_row_id()
            model_id = self.world.model_row_id()
            with self.factory() as session:
                self._snapshot = {
                    "provider_id": provider_id,
                    "model_id": model_id,
                    "provider_price_version_id": session.execute(
                        select(ProviderPriceVersion.id).where(
                            ProviderPriceVersion.public_id == price
                        )
                    ).scalar_one(),
                    "pricing_rule_id": session.execute(
                        select(PricingRule.id).where(PricingRule.public_id == rule)
                    ).scalar_one(),
                    "fx_rate_version_id": session.execute(
                        select(FxRateVersion.id).where(FxRateVersion.public_id == rate)
                    ).scalar_one(),
                    "provider_source_currency": "USD",
                }
        return self._snapshot

    def tenant(self, name: str = "a", opening: Decimal = OPENING) -> Tenancy:
        """A customer whose wallet got `opening` at T0 (2026-09), long before any period here."""
        tenant = self.world.tenant(name)
        if opening:
            self.world.fund(tenant, opening)
        return tenant

    def post(
        self,
        tenant: Tenancy,
        kind: TransactionType,
        amount: str | Decimal,
        created_at: dt.datetime,
        occurred_at: dt.datetime | None = None,
        *,
        event: bool = True,
    ) -> int:
        """One ledger row through `post_transaction`; for `AI_USAGE` also its processed event
        (unless `event=False`: the data defect of design §2). Returns the ledger row id."""
        snapshot = self.snapshot() if kind is TransactionType.AI_USAGE else {}
        number = next(BILLING_NUMBERS)
        value = Decimal(amount)
        source = REFERENCE_TYPE_FOR[kind]
        with self.factory() as session:
            if kind is TransactionType.AI_USAGE:
                assert occurred_at is not None
                reference_id = f"00000000-0000-7000-8000-{number:012d}"
            else:
                reference_id = f"statement-test-{number}"
            posted = wallet_repository.post_transaction(
                session,
                tenant_id=tenant.id,
                transaction_type=kind,
                amount=value,
                reference_type=source,
                reference_id=reference_id,
                now=created_at,
                created_by=self.world.admin.id
                if source is ReferenceType.ADMIN_ADJUSTMENT
                else None,
                description=(
                    "Fictional statement test row" if source in ADJUSTMENT_REFERENCE_TYPES else None
                ),
                actor_role="ADMIN" if source is ReferenceType.ADMIN_ADJUSTMENT else None,
            )
            ledger_id = posted.transaction.id
            if kind is TransactionType.AI_USAGE and event:
                assert occurred_at is not None
                meter = usage_repository.find_meter_type(session, "LLM_TOKEN")
                assert meter is not None
                session.add(
                    UsageEvent(
                        public_id=str(uuid.uuid4()),
                        event_id=reference_id,
                        schema_version="1.0",
                        tenant_id=tenant.id,
                        project_id=tenant.project_id,
                        integration_credential_id=tenant.credential_id,
                        request_id=f"call-{number}",
                        conversation_id=None,
                        provider_code_raw="anthropic",
                        model_code_raw="claude-x",
                        usage_meter_type_id=meter.id,
                        payload_shape=meter.payload_shape,
                        quantity_kind=meter.quantity_kind,
                        unit=meter.unit,
                        **TOKENS,
                        quantity=None,
                        payload_fingerprint="0" * 64,
                        status=UsageEventStatus.PROCESSED,
                        occurred_at=occurred_at,
                        received_at=T0,
                        created_at=T0,
                        **snapshot,
                        **COSTS,
                        billable_cost=-value,
                        billing_mode_snapshot=BillingMode.PREPAID.value,
                        reference_customer_price=None,
                        processed_at=created_at,
                        wallet_transaction_id=ledger_id,
                    )
                )
            session.commit()
            return ledger_id

    def usage(
        self,
        tenant: Tenancy,
        created_at: dt.datetime,
        occurred_at: dt.datetime,
        amount: str = USAGE,
    ) -> int:
        return self.post(tenant, TransactionType.AI_USAGE, amount, created_at, occurred_at)

    # -- reading back --

    def wallet(self, tenant: Tenancy) -> tuple[int, Decimal]:
        """`(version, balance)` in one read: the high-water mark and `B`."""
        with self.factory() as session:
            version, balance = session.execute(
                select(Wallet.version, Wallet.balance).where(Wallet.tenant_id == tenant.id)
            ).one()
            return int(version), Decimal(balance)

    def lines(self, tenant: Tenancy) -> list[Line]:
        """Every ledger row of the tenant by sequence: the full scan the references judge."""
        statement = (
            select(
                WalletTransaction.id,
                WalletTransaction.wallet_sequence,
                WalletTransaction.transaction_type,
                WalletTransaction.amount,
                WalletTransaction.balance_after,
                WalletTransaction.created_at,
                UsageEvent.occurred_at,
            )
            .outerjoin(UsageEvent, UsageEvent.wallet_transaction_id == WalletTransaction.id)
            .where(WalletTransaction.tenant_id == tenant.id)
            .order_by(WalletTransaction.wallet_sequence)
        )
        with self.factory() as session:
            return [
                Line(int(i), int(seq), TransactionType(kind), Decimal(amount), Decimal(after), c, o)
                for i, seq, kind, amount, after, c, o in session.execute(statement)
            ]

    def section(
        self,
        tenant: Tenancy,
        period: StatementPeriod,
        *,
        high_sequence: int,
        balance: Decimal,
        previous: tuple[int, Decimal] | None,
    ) -> LedgerSection:
        with self.factory() as session:
            return ledger_section(
                session,
                tenant.id,
                period,
                tenant_public_id=tenant.public_id,
                high_sequence=high_sequence,
                balance=balance,
                previous=previous,
            )

    def current(
        self, tenant: Tenancy, period: StatementPeriod, previous: tuple[int, Decimal] | None
    ) -> tuple[int, LedgerSection]:
        """The section at the wallet's current high-water mark, and that mark."""
        high, balance = self.wallet(tenant)
        section = self.section(
            tenant, period, high_sequence=high, balance=balance, previous=previous
        )
        return high, section

    def statement_ids(
        self, tenant: Tenancy, period: StatementPeriod, high: int, previous_high: int
    ) -> set[int]:
        with self.factory() as session:
            return statement_ids(
                session,
                tenant.id,
                period,
                high_sequence=high,
                previous_high_sequence=previous_high,
            )

    def later_ids(self, tenant: Tenancy, period: StatementPeriod, high: int) -> set[int]:
        with self.factory() as session:
            return later_ids(session, tenant.id, period, high_sequence=high)


# --- the references: full scan, row by row with `attributed_month` ---------------------


def ref_through(lines: list[Line], period: StatementPeriod, high: int) -> set[int]:
    """`A(K; h)`."""
    return {line.id for line in lines if line.sequence <= high and line.month <= period.label}


def ref_later(lines: list[Line], period: StatementPeriod, high: int) -> set[int]:
    """`L(K; h)`."""
    return {line.id for line in lines if line.sequence <= high and line.month > period.label}


def ref_statement(
    lines: list[Line], period: StatementPeriod, high: int, previous_high: int
) -> set[int]:
    """`S(M) = A(M; hw) \\ A(M-1; hw_prev)`."""
    return ref_through(lines, period, high) - ref_through(
        lines, previous_period(period), previous_high
    )


def amount_of(lines: list[Line], ids: set[int]) -> Decimal:
    return sum((line.amount for line in lines if line.id in ids), Decimal(0))


def balance_at(lines: list[Line], high: int) -> Decimal:
    """`B` for high-water mark `h`: the `balance_after` of row `h` (0 for an empty wallet)."""
    return Decimal(0) if high == 0 else lines[high - 1].balance_after


def expected_section(
    lines: list[Line],
    period: StatementPeriod,
    high: int,
    previous: tuple[int, Decimal] | None,
) -> LedgerSection:
    """Design §2「发生额分类」 applied to the reference `S(M)`."""
    previous_high = high if previous is None else previous[0]
    chosen = ref_statement(lines, period, high, previous_high)
    topup = ai_usage = adjustments = rebill = Decimal(0)
    current_rows = 0
    prior: dict[str, tuple[int, Decimal]] = {}
    for line in lines:
        if line.id not in chosen:
            continue
        if line.kind is TransactionType.TOPUP:
            topup += line.amount
        elif line.kind is TransactionType.AI_USAGE:
            assert line.occurred_at is not None and line.occurred_at < period.end
            if line.occurred_at >= period.start:
                ai_usage += line.amount
                current_rows += 1
            else:
                month = period_containing(line.occurred_at).label
                count, amount = prior.get(month, (0, Decimal(0)))
                prior[month] = (count + 1, amount + line.amount)
        elif line.kind in (TransactionType.REBILL_CREDIT, TransactionType.REBILL_DEBIT):
            rebill += line.amount
        else:
            adjustments += line.amount
    prior_usage = tuple(PriorUsage(m, c, a) for m, (c, a) in sorted(prior.items()))
    if previous is None:
        opening = amount_of(lines, ref_through(lines, previous_period(period), high))
    else:
        opening = previous[1]
    return LedgerSection(
        opening_balance=opening,
        closing_balance=amount_of(lines, ref_through(lines, period, high)),
        total_topup=topup,
        total_ai_usage=ai_usage,
        total_adjustments=adjustments,
        prior_usage=prior_usage,
        prior_rebill=rebill,
        prior_total=sum((item.amount for item in prior_usage), Decimal(0)) + rebill,
        current_usage_rows=current_rows,
    )


# --- fixtures ---------------------------------------------------------------------------


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
def books(factory) -> Books:
    """SQLite and a real MySQL."""
    return Books(factory)


@pytest.fixture
def mysql_books(mysql_factory) -> Books:
    """A real MySQL only."""
    return Books(mysql_factory)


# --- unit: periods and time -------------------------------------------------------------


def test_the_first_period_and_the_generation_delay_are_fixed() -> None:
    assert FIRST_STATEMENT_PERIOD == "2026-12"
    assert GENERATION_DELAY == HOUR


def test_december_2026_starts_ends_and_cuts_off_at_malaysian_midnight() -> None:
    assert DECEMBER == StatementPeriod(
        label="2026-12",
        start=dt.datetime(2026, 11, 30, 16, 0),
        end=dt.datetime(2026, 12, 31, 16, 0),
        cutoff=dt.datetime(2027, 1, 1, 16, 0),
    )
    assert JANUARY.start == DECEMBER.end


def test_february_ends_on_the_28th_or_in_a_leap_year_the_29th() -> None:
    assert statement_period("2027-02") == StatementPeriod(
        label="2027-02",
        start=dt.datetime(2027, 1, 31, 16, 0),
        end=dt.datetime(2027, 2, 28, 16, 0),
        cutoff=dt.datetime(2027, 3, 1, 16, 0),
    )
    assert statement_period("2028-02") == StatementPeriod(
        label="2028-02",
        start=dt.datetime(2028, 1, 31, 16, 0),
        end=dt.datetime(2028, 2, 29, 16, 0),
        cutoff=dt.datetime(2028, 3, 1, 16, 0),
    )


def test_the_year_2000_and_2999_are_the_edges() -> None:
    assert statement_period("2000-01").start == dt.datetime(1999, 12, 31, 16, 0)
    assert statement_period("2999-12").cutoff == dt.datetime(3000, 1, 1, 16, 0)


@pytest.mark.parametrize(
    "label",
    [
        "0001-01",
        "9999-12",
        "1999-12",
        "3000-01",
        "2026-00",
        "2026-13",
        "2026-1",
        "202612",
        "2026-12 ",
        " 2026-12",
        "2026/12",
        "",
    ],
)
def test_a_label_outside_yyyy_mm_2000_to_2999_is_a_value_error(label: str) -> None:
    with pytest.raises(ValueError):
        statement_period(label)


def test_the_previous_period_crosses_the_year_and_stops_at_2000_01() -> None:
    assert previous_period(JANUARY) == DECEMBER
    assert previous_period(statement_period("2027-03")) == FEBRUARY
    with pytest.raises(ValueError):
        previous_period(statement_period("2000-01"))


def test_the_period_containing_a_moment_is_its_malaysian_month() -> None:
    assert period_containing(DECEMBER.start) == DECEMBER
    assert period_containing(DECEMBER.start - SECOND).label == "2026-11"
    assert period_containing(DECEMBER.end - SECOND) == DECEMBER
    assert period_containing(DECEMBER.end) == JANUARY
    assert period_containing(dt.datetime(1999, 12, 31, 16, 0)).label == "2000-01"
    assert period_containing(dt.datetime(2999, 12, 31, 15, 59, 59)).label == "2999-12"


@pytest.mark.parametrize(
    "moment",
    [
        dt.datetime(1999, 12, 31, 15, 59, 59),
        dt.datetime(2999, 12, 31, 16, 0),
        dt.datetime.min,
        dt.datetime.max,
    ],
)
def test_a_moment_outside_2000_to_2999_is_a_value_error(moment: dt.datetime) -> None:
    with pytest.raises(ValueError):
        period_containing(moment)


def test_a_period_is_due_exactly_one_hour_after_its_cutoff() -> None:
    due_at = DECEMBER.cutoff + HOUR

    assert due_at == dt.datetime(2027, 1, 1, 17, 0)
    assert not is_due(DECEMBER, due_at - SECOND)
    assert is_due(DECEMBER, due_at)
    assert is_due(DECEMBER, due_at + SECOND)


def test_due_periods_run_from_the_first_period_to_the_latest_due_one() -> None:
    february_due = FEBRUARY.cutoff + HOUR

    assert due_periods(DECEMBER.cutoff + HOUR - SECOND) == []
    assert due_periods(dt.datetime(2026, 11, 6)) == []
    assert due_periods(DECEMBER.cutoff + HOUR) == [DECEMBER]
    assert due_periods(february_due - SECOND) == [DECEMBER, JANUARY]
    assert due_periods(february_due) == [DECEMBER, JANUARY, FEBRUARY]
    assert [p.label for p in due_periods(february_due, first="2027-01")] == ["2027-01", "2027-02"]


def test_attributed_month_refuses_a_missing_or_a_stray_occurred_at() -> None:
    moment = JANUARY.start + DAY
    with pytest.raises(ValueError):
        attributed_month(TransactionType.AI_USAGE, moment, None)
    with pytest.raises(ValueError):
        attributed_month("AI_USAGE", moment, None)
    with pytest.raises(ValueError):
        attributed_month(TransactionType.TOPUP, moment, moment)
    assert attributed_month("TOPUP", moment, None) == "2027-01"
    assert attributed_month("AI_USAGE", moment, moment) == "2027-01"


# --- the attribution predicate (§7「归属谓词」) -------------------------------------------

# (kind, amount, created_at, occurred_at, t(r)) around January 2027.
ATTRIBUTION_CASES = [
    # AI_USAGE that occurred in December.
    (TransactionType.AI_USAGE, USAGE, DECEMBER.end - 2 * HOUR, DECEMBER.end - 3 * HOUR, "2026-12"),
    (TransactionType.AI_USAGE, USAGE, JANUARY.start + 10 * HOUR, JANUARY.start - SECOND, "2026-12"),
    (TransactionType.AI_USAGE, USAGE, DECEMBER.cutoff - SECOND, DECEMBER.end - HOUR, "2026-12"),
    (TransactionType.AI_USAGE, USAGE, DECEMBER.cutoff, DECEMBER.end - HOUR, "2027-01"),
    (TransactionType.AI_USAGE, USAGE, DECEMBER.cutoff + HOUR, DECEMBER.end - HOUR, "2027-01"),
    # Two months back, booked in January.
    (
        TransactionType.AI_USAGE,
        USAGE,
        JANUARY.start + 5 * DAY,
        DECEMBER.start - 10 * DAY,
        "2027-01",
    ),
    # January.
    (
        TransactionType.AI_USAGE,
        USAGE,
        JANUARY.start + 3 * DAY + HOUR,
        JANUARY.start + 3 * DAY,
        "2027-01",
    ),
    (TransactionType.AI_USAGE, USAGE, JANUARY.end + 5 * HOUR, JANUARY.end - SECOND, "2027-01"),
    (TransactionType.AI_USAGE, USAGE, JANUARY.cutoff - SECOND, JANUARY.end - HOUR, "2027-01"),
    (TransactionType.AI_USAGE, USAGE, JANUARY.cutoff + SECOND, JANUARY.end - HOUR, "2027-02"),
    # February 1st.
    (TransactionType.AI_USAGE, USAGE, JANUARY.end + 2 * HOUR, JANUARY.end + HOUR, "2027-02"),
    # Booked before the event occurred (`created_at < occ`).
    (
        TransactionType.AI_USAGE,
        USAGE,
        JANUARY.end - 90 * SECOND,
        JANUARY.end + 120 * SECOND,
        "2027-02",
    ),
    (
        TransactionType.AI_USAGE,
        USAGE,
        JANUARY.start - 90 * SECOND,
        JANUARY.start + 120 * SECOND,
        "2027-01",
    ),
    (
        TransactionType.AI_USAGE,
        USAGE,
        JANUARY.start + 10 * DAY,
        JANUARY.start + 10 * DAY + 200 * SECOND,
        "2027-01",
    ),
    # Everything else: the month of `created_at`, one second either side of the edges.
    (TransactionType.TOPUP, "1", JANUARY.end - SECOND, None, "2027-01"),
    (TransactionType.TOPUP, "1", JANUARY.end, None, "2027-02"),
    (TransactionType.ADJUSTMENT_DEBIT, "-0.5", JANUARY.end + SECOND, None, "2027-02"),
    (TransactionType.SYSTEM_CORRECTION, "0.5", JANUARY.start - SECOND, None, "2026-12"),
    (TransactionType.REBILL_CREDIT, "0.25", JANUARY.start, None, "2027-01"),
    (TransactionType.BONUS, "0.125", JANUARY.cutoff - SECOND, None, "2027-02"),
]


def test_the_sql_predicate_equals_the_reference_function_row_by_row(books) -> None:
    tenant = books.tenant()
    expected: dict[int, str] = {}
    for kind, amount, created_at, occurred_at, month in ATTRIBUTION_CASES:
        expected[books.post(tenant, kind, amount, created_at, occurred_at)] = month
    lines = {line.id: line for line in books.lines(tenant) if line.id in expected}

    assert {line_id: line.month for line_id, line in lines.items()} == expected
    for label in ("2026-11", "2026-12", "2027-01", "2027-02", "2027-03"):
        period = statement_period(label)
        base = (
            select(WalletTransaction.id)
            .select_from(WalletTransaction)
            .outerjoin(UsageEvent, UsageEvent.wallet_transaction_id == WalletTransaction.id)
            .where(
                WalletTransaction.tenant_id == tenant.id,
                WalletTransaction.id.in_(list(expected)),
            )
        )
        with books.factory() as session:
            through = set(session.execute(base.where(attributed_through(period))).scalars())
            beyond = set(session.execute(base.where(not_(attributed_through(period)))).scalars())
        assert through == {i for i, month in expected.items() if month <= label}, label
        assert beyond == {i for i, month in expected.items() if month > label}, label


# --- `created_at` before `occ` across a month edge (§7) ---------------------------------


def test_usage_booked_before_it_occurred_lands_in_the_month_it_occurred(books) -> None:
    tenant = books.tenant()
    # Review #565 v1: booked 90 s before the end of January, occurred 120 s into February.
    across_end = books.usage(tenant, JANUARY.end - 90 * SECOND, JANUARY.end + 120 * SECOND)
    # The mirror: booked 90 s before January began, occurred 120 s into January.
    across_start = books.usage(tenant, JANUARY.start - 90 * SECOND, JANUARY.start + 120 * SECOND)

    high, december = books.current(tenant, DECEMBER, None)
    _, january = books.current(tenant, JANUARY, (high, december.closing_balance))
    _, february = books.current(tenant, FEBRUARY, (high, january.closing_balance))

    assert across_start not in books.statement_ids(tenant, DECEMBER, high, high)
    assert books.statement_ids(tenant, JANUARY, high, high) == {across_start}
    assert books.statement_ids(tenant, FEBRUARY, high, high) == {across_end}
    assert (december.total_ai_usage, december.current_usage_rows) == (0, 0)
    assert (january.total_ai_usage, january.current_usage_rows) == (Decimal(USAGE), 1)
    assert (february.total_ai_usage, february.current_usage_rows) == (Decimal(USAGE), 1)
    assert january.prior_usage == february.prior_usage == ()
    assert december.opening_balance == december.closing_balance == OPENING
    assert january.opening_balance == december.closing_balance
    assert february.opening_balance == january.closing_balance
    assert february.closing_balance == books.wallet(tenant)[1] == OPENING + 2 * Decimal(USAGE)


# --- scan completeness (§7「扫描完整性」) -------------------------------------------------


def _seed_batch(books: Books, tenant: Tenancy) -> None:
    """Every type; usage booked 0 s to 1 day before or 0 s to 40 days after it occurred; three
    months; inserted in an order unrelated to `created_at`."""
    rng = random.Random(155)
    first, last = DECEMBER.start, FEBRUARY.end
    span = int((last - first).total_seconds())
    rows: list[tuple[TransactionType, str, dt.datetime, dt.datetime | None]] = [
        # The edges, written out.
        (TransactionType.AI_USAGE, USAGE, JANUARY.start, JANUARY.start),
        (TransactionType.AI_USAGE, USAGE, JANUARY.start - DAY, JANUARY.start),
        (TransactionType.AI_USAGE, USAGE, DECEMBER.end - SECOND + 40 * DAY, DECEMBER.end - SECOND),
        (TransactionType.AI_USAGE, USAGE, DECEMBER.cutoff, DECEMBER.end - SECOND),
        (TransactionType.AI_USAGE, USAGE, DECEMBER.cutoff - SECOND, DECEMBER.end - SECOND),
        (TransactionType.AI_USAGE, USAGE, JANUARY.end - SECOND, JANUARY.end + DAY - SECOND),
        (TransactionType.TOPUP, "2", JANUARY.end, None),
        (TransactionType.TOPUP, "2", JANUARY.end - SECOND, None),
    ]
    signed = {
        TransactionType.TOPUP: 1,
        TransactionType.ADJUSTMENT_CREDIT: 1,
        TransactionType.BONUS: 1,
        TransactionType.REBILL_CREDIT: 1,
        TransactionType.ADJUSTMENT_DEBIT: -1,
        TransactionType.REFUND_ADJUSTMENT: -1,
        TransactionType.REBILL_DEBIT: -1,
        TransactionType.SYSTEM_CORRECTION: 0,
    }
    magnitudes = ["0.0625", "0.125", "0.25", "0.5", "1", "2"]
    for _ in range(24):
        occurred = first + rng.randrange(span) * SECOND
        if rng.random() < 0.3:
            created = occurred - rng.randrange(0, 86_400 + 1) * SECOND
        else:
            created = occurred + rng.randrange(0, 40 * 86_400 + 1) * SECOND
        rows.append((TransactionType.AI_USAGE, "-" + rng.choice(magnitudes), created, occurred))
    for kind, sign in signed.items():
        for _ in range(3):
            created = first - DAY + rng.randrange(span + 6 * 86_400) * SECOND
            magnitude = rng.choice(magnitudes)
            negative = sign < 0 or (sign == 0 and rng.random() < 0.5)
            rows.append((kind, ("-" if negative else "") + magnitude, created, None))
    rng.shuffle(rows)
    for kind, amount, created, occurred in rows:
        books.post(tenant, kind, amount, created, occurred)


def test_the_scans_equal_a_full_scan_judged_row_by_row(books) -> None:
    tenant = books.tenant()
    _seed_batch(books, tenant)
    lines = books.lines(tenant)
    top = len(lines)
    marks = [0, 1, top // 3, 2 * top // 3, top]
    assert books.wallet(tenant) == (top, balance_at(lines, top))

    # `L(K; h)` for every month and mark.
    for period in (DECEMBER, JANUARY, FEBRUARY):
        for high in marks:
            assert books.later_ids(tenant, period, high) == ref_later(lines, period, high)

    # `S(M)`, closing and opening for pairs `hw_prev <= hw`.
    pairs = [(top, top), (top, top // 3), (2 * top // 3, top // 3), (top // 3, 1), (top, 0)]
    for period in (DECEMBER, JANUARY, FEBRUARY):
        before = previous_period(period)
        for high, previous_high in pairs:
            chosen = ref_statement(lines, period, high, previous_high)
            assert books.statement_ids(tenant, period, high, previous_high) == chosen
            previous = (
                previous_high,
                amount_of(lines, ref_through(lines, before, previous_high)),
            )
            section = books.section(
                tenant,
                period,
                high_sequence=high,
                balance=balance_at(lines, high),
                previous=previous,
            )
            assert section == expected_section(lines, period, high, previous)
            assert section.closing_balance - section.opening_balance == amount_of(lines, chosen)
        # The first period: no previous statement.
        for high in marks:
            first = books.section(
                tenant, period, high_sequence=high, balance=balance_at(lines, high), previous=None
            )
            assert first == expected_section(lines, period, high, None)
            assert first.opening_balance == amount_of(lines, ref_through(lines, before, high))
            assert first.closing_balance == amount_of(lines, ref_through(lines, period, high))
    # The batch really has late rows and rows booked in a month before their event's month.
    usage = [line for line in lines if line.occurred_at is not None]
    assert any(line.month != period_containing(line.occurred_at).label for line in usage)
    assert any(line.month > period_containing(line.created_at).label for line in usage)


# --- the paths of §7 --------------------------------------------------------------------


def test_the_normal_path_sums_each_kind_and_adds_up(books) -> None:
    tenant = books.tenant()
    books.post(tenant, TransactionType.TOPUP, "50", JANUARY.start + DAY)
    books.usage(tenant, JANUARY.start + 2 * DAY, JANUARY.start + 2 * DAY - HOUR, "-0.25")
    books.usage(tenant, JANUARY.start + 3 * DAY, JANUARY.start + 3 * DAY - HOUR, "-0.5")
    books.post(tenant, TransactionType.ADJUSTMENT_DEBIT, "-2", JANUARY.start + 4 * DAY)
    books.post(tenant, TransactionType.BONUS, "1", JANUARY.start + 5 * DAY)
    # February: not this statement.
    books.post(tenant, TransactionType.TOPUP, "8", JANUARY.end + DAY)

    high, section = books.current(tenant, JANUARY, None)

    assert section == LedgerSection(
        opening_balance=OPENING,
        closing_balance=OPENING + Decimal("48.25"),
        total_topup=Decimal("50"),
        total_ai_usage=Decimal("-0.75"),
        total_adjustments=Decimal("-1"),
        prior_usage=(),
        prior_rebill=Decimal(0),
        prior_total=Decimal(0),
        current_usage_rows=2,
    )
    assert section == expected_section(books.lines(tenant), JANUARY, high, None)


def test_usage_billed_on_the_grace_day_belongs_to_the_month_it_occurred(books) -> None:
    tenant = books.tenant()
    # 31 December 23:59 in Malaysia, billed 1 January 10:00.
    grace = books.usage(tenant, JANUARY.start + 10 * HOUR, JANUARY.start - 60 * SECOND)

    high, december = books.current(tenant, DECEMBER, None)
    _, january = books.current(tenant, JANUARY, (high, december.closing_balance))

    assert books.statement_ids(tenant, DECEMBER, high, high) == {grace}
    assert (december.total_ai_usage, december.current_usage_rows) == (Decimal(USAGE), 1)
    assert books.statement_ids(tenant, JANUARY, high, high) == set()
    assert (january.total_ai_usage, january.prior_usage, january.prior_total) == (0, (), 0)
    assert january.opening_balance == january.closing_balance == december.closing_balance


def test_late_usage_is_a_prior_period_adjustment_under_its_own_month(books) -> None:
    tenant = books.tenant()
    books.usage(tenant, DECEMBER.start + 10 * DAY, DECEMBER.start + 10 * DAY - HOUR, "-0.125")
    december_high, december = books.current(tenant, DECEMBER, None)

    # December usage billed after the cutoff; November usage billed in January.
    late = books.usage(tenant, DECEMBER.cutoff + HOUR, DECEMBER.end - 2 * HOUR, "-0.25")
    older = books.usage(tenant, JANUARY.start + 4 * DAY, DECEMBER.start - 5 * DAY, "-0.5")
    current = books.usage(tenant, JANUARY.start + 6 * DAY, JANUARY.start + 6 * DAY, "-1")

    # December's statement as generated does not change.
    assert (
        books.section(
            tenant,
            DECEMBER,
            high_sequence=december_high,
            balance=balance_at(books.lines(tenant), december_high),
            previous=None,
        )
        == december
    )
    previous = (december_high, december.closing_balance)
    high, january = books.current(tenant, JANUARY, previous)

    assert books.statement_ids(tenant, JANUARY, high, december_high) == {late, older, current}
    assert january.prior_usage == (
        PriorUsage("2026-11", 1, Decimal("-0.5")),
        PriorUsage("2026-12", 1, Decimal("-0.25")),
    )
    assert january.prior_total == Decimal("-0.75")
    assert (january.total_ai_usage, january.current_usage_rows) == (Decimal("-1"), 1)
    assert january.opening_balance == december.closing_balance
    assert january.closing_balance == books.wallet(tenant)[1]
    assert january == expected_section(books.lines(tenant), JANUARY, high, previous)


def test_a_row_committed_after_generation_goes_to_the_next_statement(books) -> None:
    tenant = books.tenant()
    books.usage(tenant, DECEMBER.start + DAY, DECEMBER.start + DAY, "-0.125")
    december_high, december = books.current(tenant, DECEMBER, None)

    # Booked before December's cutoff, committed only after December was generated: its
    # sequence is above the high-water mark that statement used.
    straggler = books.usage(tenant, DECEMBER.cutoff - HOUR, DECEMBER.end - HOUR, "-0.25")
    lines = books.lines(tenant)
    assert straggler not in books.statement_ids(tenant, DECEMBER, december_high, december_high)
    assert (
        books.section(
            tenant,
            DECEMBER,
            high_sequence=december_high,
            balance=balance_at(lines, december_high),
            previous=None,
        )
        == december
    )
    assert december.closing_balance == balance_at(lines, december_high)

    high, january = books.current(tenant, JANUARY, (december_high, december.closing_balance))

    assert high == december_high + 1
    assert books.statement_ids(tenant, JANUARY, high, december_high) == {straggler}
    assert january.prior_usage == (PriorUsage("2026-12", 1, Decimal("-0.25")),)
    assert january.opening_balance == december.closing_balance
    assert january.closing_balance == books.wallet(tenant)[1]


def test_the_first_period_opens_with_everything_attributed_before_it(books) -> None:
    tenant = books.tenant()
    books.post(tenant, TransactionType.TOPUP, "20", DECEMBER.start - 10 * DAY)
    books.usage(tenant, DECEMBER.start - 5 * DAY, DECEMBER.start - 5 * DAY - HOUR, "-0.5")
    # November usage billed on 1 December (Malaysia), still November's.
    books.usage(tenant, DECEMBER.start + 5 * HOUR, DECEMBER.start - HOUR, "-0.25")
    books.usage(tenant, DECEMBER.start + DAY, DECEMBER.start + DAY, "-1")

    high, section = books.current(tenant, DECEMBER, None)

    assert section.opening_balance == OPENING + Decimal("20") - Decimal("0.75")
    assert section.prior_usage == ()
    assert section.closing_balance == section.opening_balance - 1
    assert section == expected_section(books.lines(tenant), DECEMBER, high, None)


def test_two_tenants_never_mix(books) -> None:
    ours, theirs = books.tenant("a"), books.tenant("b", opening=Decimal("500"))
    for tenant, amount in ((ours, "-0.25"), (theirs, "-0.5")):
        books.post(tenant, TransactionType.TOPUP, "4", JANUARY.start + DAY)
        books.usage(tenant, JANUARY.start + 2 * DAY, JANUARY.start + 2 * DAY, amount)
        books.usage(tenant, JANUARY.end - 90 * SECOND, JANUARY.end + 120 * SECOND, amount)
        books.post(tenant, TransactionType.ADJUSTMENT_CREDIT, "1", JANUARY.end + DAY)
    their_ids = {line.id for line in books.lines(theirs)}

    high, section = books.current(ours, JANUARY, None)

    assert section == expected_section(books.lines(ours), JANUARY, high, None)
    assert section.opening_balance == OPENING
    assert section.total_ai_usage == Decimal("-0.25")
    assert not books.statement_ids(ours, JANUARY, high, high) & their_ids
    assert not books.later_ids(ours, JANUARY, high) & their_ids
    assert not books.later_ids(ours, DECEMBER, high) & their_ids


# --- self-checks (§7「自检」) -------------------------------------------------------------


def _assert_one_error_log(caplog, tenant: Tenancy, period: StatementPeriod, check: str) -> None:
    [record] = [r for r in caplog.records if r.name == statements.__name__]
    assert record.levelno == logging.ERROR
    assert record.getMessage() == "Statement integrity check failed"
    extras = {k: v for k, v in vars(record).items() if k not in _RESERVED_LOG_ATTRS}
    assert extras == {"customer_id": tenant.public_id, "period": period.label, "check": check}


@pytest.mark.parametrize(
    ("booked", "previous_mark"),
    [
        # Segment one: above last period's high-water mark.
        (JANUARY.start + DAY, "below"),
        # Segment two: `C1(M-1)` up to last period's mark.
        (JANUARY.start + DAY, "at"),
        # The first period's opening scan, `C1(M-1)` up to this mark.
        (JANUARY.start + DAY, "none"),
        # The closing scan, `C1(M)`.
        (JANUARY.end + DAY, "at"),
    ],
)
def test_an_ai_usage_row_without_its_event_fails_the_statement(
    books, caplog, booked: dt.datetime, previous_mark: str
) -> None:
    tenant = books.tenant()
    books.usage(tenant, JANUARY.start + HOUR, JANUARY.start + HOUR)
    books.post(tenant, TransactionType.AI_USAGE, "-0.125", booked, booked, event=False)
    high, balance = books.wallet(tenant)
    previous = {"below": (high - 1, OPENING), "at": (high, OPENING), "none": None}[previous_mark]

    with caplog.at_level(logging.ERROR, logger=statements.__name__):
        with pytest.raises(StatementIntegrityError) as raised:
            books.section(tenant, JANUARY, high_sequence=high, balance=balance, previous=previous)

    assert raised.value.check == "missing_event"
    _assert_one_error_log(caplog, tenant, JANUARY, "missing_event")


def test_an_opening_that_does_not_add_up_fails_check_1(books, caplog) -> None:
    tenant = books.tenant()
    books.usage(tenant, JANUARY.start + DAY, JANUARY.start + DAY)
    december_high, december = books.current(tenant, DECEMBER, None)
    books.usage(tenant, JANUARY.start + 2 * DAY, JANUARY.start + 2 * DAY)
    high, balance = books.wallet(tenant)
    wrong = december.closing_balance + Decimal("0.5")

    with caplog.at_level(logging.ERROR, logger=statements.__name__):
        with pytest.raises(StatementIntegrityError) as raised:
            books.section(
                tenant,
                JANUARY,
                high_sequence=high,
                balance=balance,
                previous=(december_high, wrong),
            )

    assert raised.value.check == "1"
    _assert_one_error_log(caplog, tenant, JANUARY, "1")
    assert "0.5" not in str(raised.value)


# --- read-only (§7「只读」) ---------------------------------------------------------------


def test_the_ledger_section_reads_and_never_writes(books) -> None:
    tenant = books.tenant()
    books.post(tenant, TransactionType.TOPUP, "4", JANUARY.start + DAY)
    books.usage(tenant, JANUARY.cutoff + HOUR, JANUARY.end - HOUR)
    books.usage(tenant, JANUARY.end - 90 * SECOND, JANUARY.end + 120 * SECOND)

    def tables() -> tuple[list[tuple[object, ...]], list[tuple[object, ...]]]:
        with books.factory() as session:
            wallets = [tuple(row) for row in session.execute(select(Wallet.__table__))]
            ledger = [
                tuple(row)
                for row in session.execute(
                    select(WalletTransaction.__table__).order_by(WalletTransaction.id)
                )
            ]
            return wallets, ledger

    before = tables()
    high, balance = books.wallet(tenant)
    with books.factory() as session:
        for previous in (None, (high, OPENING), (1, OPENING)):
            ledger_section(
                session,
                tenant.id,
                JANUARY,
                tenant_public_id=tenant.public_id,
                high_sequence=high,
                balance=balance,
                previous=previous,
            )
        later_ids(session, tenant.id, JANUARY, high_sequence=high)
        statement_ids(session, tenant.id, JANUARY, high_sequence=high, previous_high_sequence=1)
        assert not (session.new or session.dirty or session.deleted)
        session.rollback()

    assert tables() == before


# --- precision (§7「金额精度」, MySQL only) ------------------------------------------------


def test_eight_decimal_amounts_sum_exactly_on_mysql(mysql_books) -> None:
    books = mysql_books
    tenant = books.tenant(opening=Decimal("100.12345678"))
    books.post(tenant, TransactionType.TOPUP, "0.00000001", JANUARY.start + DAY)
    books.usage(tenant, JANUARY.start + 2 * DAY, JANUARY.start + 2 * DAY, "-0.33333333")
    books.usage(tenant, JANUARY.start + 3 * DAY, JANUARY.start + 3 * DAY, "-1.98765432")
    books.usage(tenant, JANUARY.start + 4 * DAY, DECEMBER.start + DAY, "-0.00000007")
    books.post(tenant, TransactionType.SYSTEM_CORRECTION, "-0.00000003", JANUARY.start + 5 * DAY)
    books.post(tenant, TransactionType.REBILL_DEBIT, "-0.11111111", JANUARY.start + 6 * DAY)

    high, section = books.current(tenant, JANUARY, None)

    assert section.opening_balance == Decimal("100.12345678")
    assert section.total_topup == Decimal("0.00000001")
    assert section.total_ai_usage == Decimal("-2.32098765")
    assert section.total_adjustments == Decimal("-0.00000003")
    assert section.prior_usage == (PriorUsage("2026-12", 1, Decimal("-0.00000007")),)
    assert section.prior_rebill == Decimal("-0.11111111")
    assert section.prior_total == Decimal("-0.11111118")
    assert section.closing_balance == Decimal("97.69135793") == books.wallet(tenant)[1]
    assert section == expected_section(books.lines(tenant), JANUARY, high, None)

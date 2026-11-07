"""The seven billing alert dimensions (design gate #181 v2 §2「告警维度」, §7, AIH-TASK-032).

The design §7 row **告警维度** (unit): each of `usage_pricing_error`, `usage_fx_rate_error`,
`usage_model_unknown`, `usage_failed`, `usage_processing_backlog`, `wallet_negative_balance` and
`outbox_backlog` at OK / P2 / P1 as the design's table says, and the summaries hold only counts,
the earliest moments and at most three detail codes — no tenant, no amount. The registry order
and the empty-database lines are pinned in test_internal_alerts.py.

`usage_processing_backlog` also carries the missing statements and the late usage of gate #565 v6
§2「告警」 (AIH-TASK-158), the §7 rows **告警：缺失**, **告警：晚到** and **告警：原有部分**.

SQLite only: the dimensions are plain reads (design v3 of #183). Rows are inserted directly with
placeholder references (SQLite does not enforce the foreign keys); the CHECKs of migrations 0017
and 0018 hold on every row. uuids are zero placeholders; the one balance is fictional.
"""

from __future__ import annotations

import datetime as dt
import itertools
from collections.abc import Iterator
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.database import create_session_factory
from app.models.ai_catalog import PayloadShape, QuantityKind
from app.models.auth import DomainOutbox, OutboxStatus
from app.models.base import Base
from app.models.statements import MonthlyStatement, StatementStatus
from app.models.tenancy import BillingMode, Tenant
from app.models.usage import UsageEvent, UsageEventStatus
from app.models.wallet import ReferenceType, TransactionType, Wallet, WalletTransaction
from app.models.wallet_reconciliation import ReconciliationRunStatus, WalletReconciliationRun
from app.repositories.wallet import EVENT_BILLING_STATUS_CHANGED
from app.services import alerts
from app.services.account_status import EVENT_ACCOUNT_STATUS_CHANGED
from app.services.alerts import AlertContext, AlertLevel, AlertResult
from app.services.password_reset import EVENT_PASSWORD_RESET
from app.services.statements import (
    StatementPeriod,
    attributed_month,
    period_containing,
    statement_period,
)

NOW = dt.datetime(2026, 10, 1, 4, 30, 0)
MINUTE = dt.timedelta(minutes=1)
TENANT_PUBLIC_ID = "00000000-0000-4000-8000-000000000123"

OK = AlertLevel.OK
P1 = AlertLevel.P1
P2 = AlertLevel.P2

_NUMBERS = itertools.count(1)


@pytest.fixture
def factory() -> Iterator[sessionmaker[Session]]:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    yield create_session_factory(engine)
    engine.dispose()


def add_event(
    factory: sessionmaker[Session],
    status: UsageEventStatus,
    *,
    error_code: str | None = None,
    attempts: int = 0,
    received_at: dt.datetime = NOW - 2 * MINUTE,
    occurred_at: dt.datetime | None = None,
    next_attempt_at: dt.datetime | None = None,
    claimed_at: dt.datetime | None = None,
    lease_expires_at: dt.datetime | None = None,
    tenant_id: int = 1,
    wallet_transaction_id: int | None = None,
    billable_cost: Decimal | None = None,
    billing_mode_snapshot: str | None = None,
    processed_at: dt.datetime | None = None,
) -> None:
    """One event row; placeholder references (no foreign keys on SQLite)."""
    number = next(_NUMBERS)
    processing = status is UsageEventStatus.PROCESSING
    with factory() as session:
        session.add(
            UsageEvent(
                public_id=f"00000000-0000-4000-8000-{number:012d}",
                event_id=f"00000000-0000-7000-8000-{number:012d}",
                schema_version="1.0",
                tenant_id=tenant_id,
                wallet_transaction_id=wallet_transaction_id,
                billable_cost=billable_cost,
                billing_mode_snapshot=billing_mode_snapshot,
                processed_at=processed_at,
                project_id=1,
                integration_credential_id=1,
                request_id=f"call-{number}",
                provider_code_raw="anthropic",
                model_code_raw="claude-x",
                usage_meter_type_id=1,
                payload_shape=PayloadShape.LLM_TOKEN_FIELDS,
                quantity_kind=QuantityKind.INTEGER,
                input_tokens=1,
                output_tokens=1,
                cache_creation_input_tokens=0,
                cache_read_input_tokens=0,
                unit="TOKEN",
                payload_fingerprint="0" * 64,
                status=status,
                error_code=error_code,
                attempt_count=attempts,
                next_attempt_at=next_attempt_at,
                claim_token="00000000-0000-4000-8000-000000000001" if processing else None,
                claimed_at=claimed_at,
                lease_expires_at=lease_expires_at,
                occurred_at=occurred_at or received_at - MINUTE,
                received_at=received_at,
                created_at=received_at,
            )
        )
        session.commit()


def check(factory: sessionmaker[Session], dimension, now: dt.datetime = NOW) -> AlertResult:
    with factory() as session:
        return dimension(session, AlertContext(now=now, currencies=("USD",)))


def stamp(moment: dt.datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


# --- the three error-state dimensions ------------------------------------------------------


@pytest.mark.parametrize(
    ("dimension", "status"),
    [
        (alerts.usage_pricing_error, UsageEventStatus.PRICING_ERROR),
        (alerts.usage_fx_rate_error, UsageEventStatus.FX_RATE_ERROR),
        (alerts.usage_model_unknown, UsageEventStatus.MODEL_UNKNOWN),
    ],
    ids=["pricing", "fx", "model"],
)
def test_an_error_state_dimension_is_ok_until_one_event_is_in_it(
    factory, dimension, status: UsageEventStatus
) -> None:
    # Events in every other state do not count.
    for other in (UsageEventStatus.RECEIVED, UsageEventStatus.FAILED_FINAL):
        add_event(factory, other)
    assert check(factory, dimension) == AlertResult(OK, f"no {status.value} events")

    add_event(factory, status, received_at=NOW - 30 * MINUTE)

    assert check(factory, dimension) == AlertResult(
        P2, f"1 event {status.value}, earliest received {stamp(NOW - 30 * MINUTE)}"
    )


def test_pricing_errors_list_at_most_three_codes_in_order(factory) -> None:
    codes = [
        "NO_PROVIDER_PRICE",
        "NO_PRICING_RULE",
        "MISSING_RULE_COMPONENT",
        "MISSING_PROVIDER_COMPONENT",
        "NO_PRICING_RULE",
    ]
    for minutes, code in enumerate(codes, start=1):
        add_event(
            factory,
            UsageEventStatus.PRICING_ERROR,
            error_code=code,
            received_at=NOW - minutes * MINUTE,
        )

    result = check(factory, alerts.usage_pricing_error)

    assert result == AlertResult(
        P2,
        f"5 events PRICING_ERROR, earliest received {stamp(NOW - 5 * MINUTE)}, codes "
        "MISSING_PROVIDER_COMPONENT, MISSING_RULE_COMPONENT, NO_PRICING_RULE",
    )


# --- usage_failed --------------------------------------------------------------------------


def test_usage_failed_counts_final_events_and_long_retries(factory) -> None:
    add_event(factory, UsageEventStatus.FAILED_RETRYABLE, error_code="DEADLOCK", attempts=2)
    assert check(factory, alerts.usage_failed) == AlertResult(OK, "no failed events")

    add_event(
        factory,
        UsageEventStatus.FAILED_RETRYABLE,
        error_code="LOCK_WAIT_TIMEOUT",
        attempts=3,
        received_at=NOW - 20 * MINUTE,
    )
    assert check(factory, alerts.usage_failed) == AlertResult(
        P2,
        "0 FAILED_FINAL, 1 FAILED_RETRYABLE with 3+ attempts, earliest received "
        f"{stamp(NOW - 20 * MINUTE)}, codes LOCK_WAIT_TIMEOUT",
    )

    add_event(
        factory,
        UsageEventStatus.FAILED_FINAL,
        error_code="STALE_PROCESSING_EXHAUSTED",
        received_at=NOW - 40 * MINUTE,
    )
    assert check(factory, alerts.usage_failed) == AlertResult(
        P2,
        "1 FAILED_FINAL, 1 FAILED_RETRYABLE with 3+ attempts, earliest received "
        f"{stamp(NOW - 40 * MINUTE)}, codes LOCK_WAIT_TIMEOUT, STALE_PROCESSING_EXHAUSTED",
    )


@pytest.mark.parametrize("code", ["LEDGER_CONFLICT", "AMOUNT_OUT_OF_RANGE"])
def test_a_ledger_conflict_or_an_overflow_is_p1(factory, code: str) -> None:
    add_event(factory, UsageEventStatus.FAILED_FINAL, error_code="ACCOUNT_CLOSED")
    assert check(factory, alerts.usage_failed).level is P2

    add_event(factory, UsageEventStatus.FAILED_FINAL, error_code=code)

    result = check(factory, alerts.usage_failed)
    assert result.level is P1
    assert code in result.summary


# --- usage_processing_backlog ----------------------------------------------------------------

# The two parts AIH-TASK-158 appends, on a database with no tenant at NOW (2026-10, before the
# first statement period): the three backlog parts before them are unchanged.
QUIET = "; 0 statements missing; late usage 24h 0/0 rows (0.00%), 0.00% of amount"


@pytest.mark.parametrize(
    ("waiting", "level"),
    [(4, OK), (5, OK), (6, P2), (30, P2), (31, P1)],
)
def test_the_backlog_level_follows_the_oldest_due_event(factory, waiting: int, level) -> None:
    since = NOW - waiting * MINUTE
    add_event(factory, UsageEventStatus.RECEIVED, received_at=since)
    add_event(factory, UsageEventStatus.RECEIVED, received_at=NOW - MINUTE)

    assert check(factory, alerts.usage_processing_backlog) == AlertResult(
        level, f"2 due, oldest since {stamp(since)}; 0 PROCESSING; 0 lease expired{QUIET}"
    )


def test_an_event_from_the_future_waits_from_when_it_happened(factory) -> None:
    # Received 10 minutes ago with an `occurred_at` 300 seconds ahead (ingestion's skew), so
    # claimable for 5 minutes only; and one that has not happened yet: not due at all.
    add_event(
        factory,
        UsageEventStatus.RECEIVED,
        received_at=NOW - 10 * MINUTE,
        occurred_at=NOW - 5 * MINUTE,
    )
    add_event(factory, UsageEventStatus.RECEIVED, received_at=NOW, occurred_at=NOW + MINUTE)

    assert check(factory, alerts.usage_processing_backlog) == AlertResult(
        OK, f"1 due, oldest since {stamp(NOW - 5 * MINUTE)}; 0 PROCESSING; 0 lease expired{QUIET}"
    )


def test_a_retry_counts_from_its_due_time_and_not_before(factory) -> None:
    add_event(
        factory,
        UsageEventStatus.FAILED_RETRYABLE,
        attempts=1,
        received_at=NOW - 60 * MINUTE,
        next_attempt_at=NOW + MINUTE,
    )
    assert check(factory, alerts.usage_processing_backlog).summary.startswith("0 due;")

    add_event(
        factory,
        UsageEventStatus.FAILED_RETRYABLE,
        attempts=1,
        received_at=NOW - 60 * MINUTE,
        next_attempt_at=NOW - 6 * MINUTE,
    )

    result = check(factory, alerts.usage_processing_backlog)
    assert result == AlertResult(
        P2, f"1 due, oldest since {stamp(NOW - 6 * MINUTE)}; 0 PROCESSING; 0 lease expired{QUIET}"
    )


@pytest.mark.parametrize(
    ("expired_for", "level", "expired"),
    [(None, OK, 0), (1, P2, 1), (10, P2, 1), (11, P1, 1)],
    ids=["within-lease", "expired-1-minute", "expired-10-minutes", "expired-11-minutes"],
)
def test_processing_past_its_lease_means_recovery_has_stopped(
    factory, expired_for: int | None, level, expired: int
) -> None:
    lease_end = NOW + MINUTE if expired_for is None else NOW - expired_for * MINUTE
    claimed = lease_end - 2 * MINUTE
    add_event(
        factory,
        UsageEventStatus.PROCESSING,
        attempts=1,
        claimed_at=claimed,
        lease_expires_at=lease_end,
    )

    assert check(factory, alerts.usage_processing_backlog) == AlertResult(
        level,
        f"0 due; 1 PROCESSING, earliest claimed {stamp(claimed)}; {expired} lease expired{QUIET}",
    )


# --- usage_processing_backlog: missing statements (gate #565 v6 §2「告警」, AIH-TASK-158) ------

DECEMBER = statement_period("2026-12")
JANUARY = statement_period("2027-01")
SECOND = dt.timedelta(seconds=1)
HOUR = dt.timedelta(hours=1)
LONG_AGO = dt.datetime(2026, 9, 1)
NO_BACKLOG_PARTS = "0 due; 0 PROCESSING; 0 lease expired"
NO_LATE_USAGE = "late usage 24h 0/0 rows (0.00%), 0.00% of amount"


def add_tenant(
    factory: sessionmaker[Session],
    *,
    mode: BillingMode = BillingMode.PREPAID,
    wallet_created_at: dt.datetime | None = LONG_AGO,
) -> Tenant:
    """A tenant and (unless `wallet_created_at` is None) its empty wallet."""
    number = next(_NUMBERS)
    with factory() as session:
        tenant = Tenant(
            public_id=f"00000000-0000-4000-8000-{number:012d}",
            company_name=f"Statement Alert Company {number}",
            email="billing@example.invalid",
            billing_mode=mode,
            created_at=LONG_AGO,
            updated_at=LONG_AGO,
        )
        session.add(tenant)
        session.flush()
        if wallet_created_at is not None:
            session.add(
                Wallet(
                    tenant_id=tenant.id,
                    currency="MYR",
                    balance=Decimal(0),
                    version=0,
                    created_at=wallet_created_at,
                    updated_at=wallet_created_at,
                )
            )
        session.commit()
        session.refresh(tenant)
        session.expunge(tenant)
        return tenant


def add_statement(factory: sessionmaker[Session], tenant: Tenant, period: StatementPeriod) -> None:
    """A finalized statement row; placeholder tax policy reference (no foreign keys on SQLite)."""
    number = next(_NUMBERS)
    with factory() as session:
        session.add(
            MonthlyStatement(
                public_id=f"00000000-0000-4000-8000-{number:012d}",
                tenant_id=tenant.id,
                statement_number=f"ST-TEST-{number}",
                period_start=period.start,
                period_end=period.end,
                cutoff_at=period.cutoff,
                status=StatementStatus.FINALIZED,
                ledger_high_sequence=0,
                snapshot_json={},
                tax_policy_version_id=1,
                finalized_at=period.cutoff + HOUR,
                created_at=period.cutoff + HOUR,
            )
        )
        session.commit()


def backlog(factory: sessionmaker[Session], now: dt.datetime) -> AlertResult:
    return check(factory, alerts.usage_processing_backlog, now)


def missing_part(result: AlertResult) -> str:
    """The fourth part of the summary: the missing statements."""
    return result.summary.split("; ")[3]


@pytest.mark.parametrize(
    ("offset", "level", "missing"),
    [
        (6 * HOUR - SECOND, OK, "0 statements missing"),
        (6 * HOUR, P2, "1 statements missing, earliest period 2026-12"),
        (30 * HOUR - SECOND, P2, "1 statements missing, earliest period 2026-12"),
        (30 * HOUR, OK, "0 statements missing"),
    ],
    ids=["before-6h", "at-6h", "before-30h", "at-30h"],
)
def test_a_missing_statement_is_p2_only_from_6_to_30_hours_after_the_cutoff(
    factory, offset: dt.timedelta, level, missing: str
) -> None:
    add_tenant(factory)

    result = backlog(factory, DECEMBER.cutoff + offset)

    assert result == AlertResult(level, f"{NO_BACKLOG_PARTS}; {missing}; {NO_LATE_USAGE}")


def test_only_the_period_in_its_window_is_counted(factory) -> None:
    """Both 2026-12 and 2027-01 are missing; only 2027-01 is inside its window."""
    tenant = add_tenant(factory)

    before = backlog(factory, JANUARY.cutoff + 6 * HOUR - SECOND)
    assert before.level is OK
    assert missing_part(before) == "0 statements missing"

    after = backlog(factory, JANUARY.cutoff + 6 * HOUR)
    assert after == AlertResult(
        P2,
        f"{NO_BACKLOG_PARTS}; 1 statements missing, earliest period 2027-01; {NO_LATE_USAGE}",
    )
    assert tenant.public_id not in after.summary
    assert tenant.company_name not in after.summary


def test_tenants_that_get_no_statement_are_not_missing(factory) -> None:
    add_tenant(factory, mode=BillingMode.INTERNAL_METERED_ONLY)
    add_tenant(factory, wallet_created_at=DECEMBER.end)
    add_tenant(factory, wallet_created_at=None)
    add_statement(factory, add_tenant(factory), DECEMBER)
    now = DECEMBER.cutoff + 6 * HOUR

    assert backlog(factory, now) == AlertResult(
        OK, f"{NO_BACKLOG_PARTS}; 0 statements missing; {NO_LATE_USAGE}"
    )

    add_tenant(factory, wallet_created_at=DECEMBER.end - SECOND)

    assert missing_part(backlog(factory, now)) == "1 statements missing, earliest period 2026-12"


def test_a_period_before_the_first_one_is_never_missing(factory) -> None:
    add_tenant(factory)
    november = statement_period("2026-11")

    result = backlog(factory, november.cutoff + 6 * HOUR)

    assert result.level is OK
    assert missing_part(result) == "0 statements missing"


def test_the_worst_part_sets_the_level(factory) -> None:
    """A missing statement (P2) does not hide a stuck backlog (P1); the backlog prefix stays."""
    add_tenant(factory)
    now = DECEMBER.cutoff + 6 * HOUR
    add_event(factory, UsageEventStatus.RECEIVED, received_at=now - 31 * MINUTE)

    result = backlog(factory, now)

    assert result == AlertResult(
        P1,
        f"1 due, oldest since {stamp(now - 31 * MINUTE)}; 0 PROCESSING; 0 lease expired; "
        f"1 statements missing, earliest period 2026-12; {NO_LATE_USAGE}",
    )


# --- usage_processing_backlog: late usage (gate #565 v6 §2「告警」, AIH-TASK-158) ---------------

# Mid-January in Malaysia: the 24 hours lie inside 2027-01 and no statement window is open.
MID_JANUARY = dt.datetime(2027, 1, 20, 4, 0, 0)


def add_usage(
    factory: sessionmaker[Session],
    tenant: Tenant,
    *,
    created_at: dt.datetime,
    occurred_at: dt.datetime,
    amount: str = "-0.0625",
    event: bool = True,
) -> None:
    """One `AI_USAGE` ledger row booked at `created_at` and (unless `event=False`) its
    processed event. Placeholder wallet id (no foreign keys on SQLite)."""
    number = next(_NUMBERS)
    value = Decimal(amount)
    with factory() as session:
        row = WalletTransaction(
            public_id=f"00000000-0000-4000-8000-{number:012d}",
            wallet_id=tenant.id,
            tenant_id=tenant.id,
            wallet_sequence=number,
            transaction_type=TransactionType.AI_USAGE,
            amount=value,
            balance_before=Decimal(0),
            balance_after=value,
            reference_type=ReferenceType.USAGE_EVENT,
            reference_id=f"00000000-0000-7000-8000-{number:012d}",
            created_at=created_at,
        )
        session.add(row)
        session.flush()
        row_id = row.id
        session.commit()
    if event:
        add_event(
            factory,
            UsageEventStatus.PROCESSED,
            received_at=occurred_at,
            occurred_at=occurred_at,
            tenant_id=tenant.id,
            wallet_transaction_id=row_id,
            billable_cost=-value,
            billing_mode_snapshot=BillingMode.PREPAID.value,
            processed_at=created_at,
        )


def late_part(result: AlertResult) -> str:
    """The fifth part of the summary: the late usage."""
    return result.summary.split("; ")[4]


def is_late(created_at: dt.datetime, occurred_at: dt.datetime) -> bool:
    """`t(r) ≠ U` straight from the reference function of AIH-TASK-155."""
    month = attributed_month(TransactionType.AI_USAGE, created_at, occurred_at)
    return month != period_containing(occurred_at).label


def test_one_late_row_in_100_is_p2(factory) -> None:
    tenant = add_tenant(factory)
    for minutes in range(1, 100):
        booked = MID_JANUARY - minutes * MINUTE
        add_usage(factory, tenant, created_at=booked, occurred_at=booked - MINUTE)
    # December usage booked on 20 January: past December's cutoff, so late.
    late_booked, late_occurred = MID_JANUARY - HOUR, dt.datetime(2026, 12, 20)
    assert is_late(late_booked, late_occurred)
    add_usage(factory, tenant, created_at=late_booked, occurred_at=late_occurred)
    # Not counted: booked just outside the window at either end, a tenant metered
    # internally, and a ledger row no event joins.
    window_start = MID_JANUARY - alerts.LATE_USAGE_WINDOW
    add_usage(factory, tenant, created_at=window_start - SECOND, occurred_at=late_occurred)
    add_usage(factory, tenant, created_at=MID_JANUARY, occurred_at=late_occurred)
    internal = add_tenant(factory, mode=BillingMode.INTERNAL_METERED_ONLY)
    add_usage(factory, internal, created_at=late_booked, occurred_at=late_occurred)
    add_usage(factory, tenant, created_at=late_booked, occurred_at=late_occurred, event=False)

    result = backlog(factory, MID_JANUARY)

    assert result == AlertResult(
        P2,
        f"{NO_BACKLOG_PARTS}; 0 statements missing; "
        "late usage 24h 1/100 rows (1.00%), 1.00% of amount",
    )
    assert tenant.public_id not in result.summary
    assert tenant.company_name not in result.summary
    assert "0.0625" not in result.summary


def test_no_late_row_is_ok(factory) -> None:
    tenant = add_tenant(factory)
    for minutes in range(1, 101):
        booked = MID_JANUARY - minutes * MINUTE
        add_usage(factory, tenant, created_at=booked, occurred_at=booked - MINUTE)

    result = backlog(factory, MID_JANUARY)

    assert result.level is OK
    assert late_part(result) == "late usage 24h 0/100 rows (0.00%), 0.00% of amount"


def test_the_level_follows_the_exact_share_not_the_rounded_one(factory) -> None:
    """1 of 101 rows (0.99%) and 0.995% of the amount, shown as 1.00%: still OK. Every amount
    is a multiple of 1/256, exact on SQLite too."""
    tenant = add_tenant(factory)
    for minutes in range(1, 100):
        booked = MID_JANUARY - minutes * MINUTE
        add_usage(factory, tenant, created_at=booked, occurred_at=booked, amount="-0.78125")
    booked = MID_JANUARY - 200 * MINUTE
    add_usage(factory, tenant, created_at=booked, occurred_at=booked, amount="-0.00390625")
    add_usage(
        factory,
        tenant,
        created_at=MID_JANUARY - HOUR,
        occurred_at=dt.datetime(2026, 12, 20),
        amount="-0.77734375",
    )

    result = backlog(factory, MID_JANUARY)

    assert result.level is OK
    assert late_part(result) == "late usage 24h 1/101 rows (0.99%), 1.00% of amount"


def test_a_window_across_a_month_judges_each_part_by_its_month(factory) -> None:
    """At 10:00 on 1 January in Malaysia the window is 10:00 on 31 December to now: the December
    part by 2026-12's constants, the January part by 2027-01's. December usage booked on the
    grace day is not late; November usage booked on 31 December is."""
    tenant = add_tenant(factory)
    now = JANUARY.start + 10 * HOUR
    rows = [
        # The December part.
        (JANUARY.start - 13 * HOUR, JANUARY.start - 2 * DAY),
        (JANUARY.start - 12 * HOUR, dt.datetime(2026, 11, 20)),
        # The January part: the grace day of 2026-12, from its first second.
        (JANUARY.start, JANUARY.start - SECOND),
        (now - HOUR, JANUARY.start - HOUR),
    ]
    for booked, occurred in rows:
        add_usage(factory, tenant, created_at=booked, occurred_at=occurred)
    assert [is_late(booked, occurred) for booked, occurred in rows] == [
        False,
        True,
        False,
        False,
    ]

    result = backlog(factory, now)

    assert result.level is P2
    assert late_part(result) == "late usage 24h 1/4 rows (25.00%), 25.00% of amount"


# --- wallet_negative_balance -------------------------------------------------------------------


def test_negative_wallets_are_counted_without_tenant_or_amount(factory) -> None:
    """负余额仍 P2；核对部分在 tests/backend/test_wallet_reconciliation_alerts.py。
    一条一致、未过期的完成运行，让核对部分本身是 OK（设计闸门 #474 v13 §2「告警」）。"""

    def wallet(tenant_id: int, balance: str) -> Wallet:
        return Wallet(
            tenant_id=tenant_id,
            currency="MYR",
            balance=Decimal(balance),
            version=1,
            created_at=NOW,
            updated_at=NOW,
        )

    with factory() as session:
        session.add_all([wallet(1, "0"), wallet(2, "0.0625")])
        session.add(
            WalletReconciliationRun(
                public_id="00000000-0000-4000-8000-000000000001",
                run_date=dt.date(2026, 10, 1),
                status=ReconciliationRunStatus.COMPLETED,
                started_at=NOW - 2 * MINUTE,
                finished_at=NOW - MINUTE,
                cursor_tenant_id=4,
                wallets_checked=4,
                wallets_inconsistent=0,
            )
        )
        session.commit()
    reconciled = "reconciliation 2026-10-01 checked 4 inconsistent 0"
    assert check(factory, alerts.wallet_negative_balance) == AlertResult(
        OK, f"no wallet below zero; {reconciled}"
    )

    with factory() as session:
        session.add_all([wallet(3, "-0.0625"), wallet(4, "-12.5")])
        session.commit()

    result = check(factory, alerts.wallet_negative_balance)
    assert result == AlertResult(P2, f"2 wallets below zero; {reconciled}")
    assert "0.0625" not in result.summary and "12.5" not in result.summary


# --- outbox_backlog ------------------------------------------------------------------------


def add_outbox(
    factory: sessionmaker[Session],
    event_type: str,
    created_at: dt.datetime,
    status: OutboxStatus = OutboxStatus.PENDING,
    processed_at: dt.datetime | None = None,
) -> None:
    with factory() as session:
        session.add(
            DomainOutbox(
                event_type=event_type,
                aggregate_type="tenant",
                aggregate_id=TENANT_PUBLIC_ID,
                payload_json=None,
                status=status,
                attempt_count=0,
                next_retry_at=created_at,
                created_at=created_at,
                processed_at=processed_at,
            )
        )
        session.commit()


NO_BACKLOG = "no handled outbox row pending over 10 minutes"
NO_DEAD_LETTER = "no dead letter in 7 days"
DAY = dt.timedelta(days=1)


def test_only_handled_outbox_rows_pending_over_10_minutes_count(factory) -> None:
    # Still no handler: waits by design. Sent: done. Pending for exactly 10 minutes: not yet.
    add_outbox(factory, EVENT_ACCOUNT_STATUS_CHANGED, NOW - 60 * MINUTE)
    add_outbox(factory, EVENT_PASSWORD_RESET, NOW - 60 * MINUTE, OutboxStatus.SENT)
    add_outbox(factory, EVENT_PASSWORD_RESET, NOW - 10 * MINUTE)
    assert check(factory, alerts.outbox_backlog) == AlertResult(
        OK, f"{NO_BACKLOG}; {NO_DEAD_LETTER}"
    )

    add_outbox(factory, EVENT_PASSWORD_RESET, NOW - 11 * MINUTE)

    result = check(factory, alerts.outbox_backlog)
    assert result == AlertResult(
        P2,
        f"1 row pending over 10 minutes, oldest created {stamp(NOW - 11 * MINUTE)}; "
        f"{NO_DEAD_LETTER}",
    )
    assert TENANT_PUBLIC_ID not in result.summary


def test_a_billing_status_changed_row_pending_over_10_minutes_counts(factory) -> None:
    """设计闸门 #471 v7 §7「recover 与告警名单」：
    通知投递接上之后，这类事件有处理器，计入。"""
    add_outbox(factory, EVENT_BILLING_STATUS_CHANGED, NOW - 60 * MINUTE)

    result = check(factory, alerts.outbox_backlog)
    assert result == AlertResult(
        P2,
        f"1 row pending over 10 minutes, oldest created {stamp(NOW - 60 * MINUTE)}; "
        f"{NO_DEAD_LETTER}",
    )
    assert TENANT_PUBLIC_ID not in result.summary


def add_dead_letter(
    factory: sessionmaker[Session], event_type: str, processed_at: dt.datetime | None
) -> None:
    add_outbox(factory, event_type, NOW - 8 * DAY, OutboxStatus.FAILED, processed_at)


def test_a_handled_dead_letter_within_7_days_is_p2(factory) -> None:
    """AIH-TASK-149：有处理器的行在最近 7 天内结算为 FAILED 即 P2。"""
    add_dead_letter(factory, EVENT_PASSWORD_RESET, NOW - 2 * DAY)

    result = check(factory, alerts.outbox_backlog)
    assert result == AlertResult(
        P2, f"{NO_BACKLOG}; 1 dead letter in 7 days, latest {stamp(NOW - 2 * DAY)}"
    )
    assert TENANT_PUBLIC_ID not in result.summary


def test_two_dead_letters_report_the_latest(factory) -> None:
    add_dead_letter(factory, EVENT_BILLING_STATUS_CHANGED, NOW - 30 * MINUTE)
    add_dead_letter(factory, EVENT_PASSWORD_RESET, NOW - 3 * DAY)

    result = check(factory, alerts.outbox_backlog)
    assert result == AlertResult(
        P2, f"{NO_BACKLOG}; 2 dead letters in 7 days, latest {stamp(NOW - 30 * MINUTE)}"
    )
    assert TENANT_PUBLIC_ID not in result.summary


@pytest.mark.parametrize(
    ("event_type", "status", "processed_at"),
    [
        # Exactly 7 days ago: outside (strictly later only).
        (EVENT_PASSWORD_RESET, OutboxStatus.FAILED, NOW - alerts.OUTBOX_DEAD_LETTER_WINDOW),
        # Earlier: outside.
        (EVENT_PASSWORD_RESET, OutboxStatus.FAILED, NOW - 8 * DAY),
        # Still no handler: not counted.
        (EVENT_ACCOUNT_STATUS_CHANGED, OutboxStatus.FAILED, NOW - DAY),
        # No processed_at: not counted.
        (EVENT_PASSWORD_RESET, OutboxStatus.FAILED, None),
        # Sent: done.
        (EVENT_PASSWORD_RESET, OutboxStatus.SENT, NOW - DAY),
    ],
)
def test_rows_that_are_not_recent_handled_dead_letters_do_not_count(
    factory, event_type: str, status: OutboxStatus, processed_at: dt.datetime | None
) -> None:
    add_outbox(factory, event_type, NOW - 8 * DAY, status, processed_at)

    result = check(factory, alerts.outbox_backlog)
    assert result == AlertResult(OK, f"{NO_BACKLOG}; {NO_DEAD_LETTER}")


def test_backlog_and_dead_letters_together_are_p2(factory) -> None:
    add_outbox(factory, EVENT_PASSWORD_RESET, NOW - 20 * MINUTE)
    add_outbox(factory, EVENT_BILLING_STATUS_CHANGED, NOW - 15 * MINUTE)
    add_dead_letter(factory, EVENT_BILLING_STATUS_CHANGED, NOW - DAY)

    result = check(factory, alerts.outbox_backlog)
    assert result == AlertResult(
        P2,
        f"2 rows pending over 10 minutes, oldest created {stamp(NOW - 20 * MINUTE)}; "
        f"1 dead letter in 7 days, latest {stamp(NOW - DAY)}",
    )
    assert TENANT_PUBLIC_ID not in result.summary


# --- what no summary may carry -------------------------------------------------------------


def test_no_summary_names_a_tenant_or_an_amount(factory) -> None:
    for status, code in (
        (UsageEventStatus.PRICING_ERROR, "NO_PRICING_RULE"),
        (UsageEventStatus.FX_RATE_ERROR, None),
        (UsageEventStatus.MODEL_UNKNOWN, None),
        (UsageEventStatus.FAILED_FINAL, "LEDGER_CONFLICT"),
        (UsageEventStatus.RECEIVED, None),
    ):
        add_event(factory, status, error_code=code, received_at=NOW - 45 * MINUTE)
    with factory() as session:
        session.add(
            Wallet(
                tenant_id=7,
                currency="MYR",
                balance=Decimal("-3.25"),
                version=1,
                created_at=NOW,
                updated_at=NOW,
            )
        )
        session.commit()

    body = alerts.alert_lines(factory, ("USD",), clock=lambda: NOW)

    for line in body.splitlines()[3:]:
        assert TENANT_PUBLIC_ID not in line
        assert "3.25" not in line
        assert "tenant" not in line.lower()
    assert "usage_failed P1 " in body
    assert "usage_processing_backlog P1 " in body
    assert "wallet_negative_balance P2 1 wallet below zero" in body

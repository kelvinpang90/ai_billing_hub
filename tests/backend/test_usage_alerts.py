"""The seven billing alert dimensions (design gate #181 v2 §2「告警维度」, §7, AIH-TASK-032).

The design §7 row **告警维度** (unit): each of `usage_pricing_error`, `usage_fx_rate_error`,
`usage_model_unknown`, `usage_failed`, `usage_processing_backlog`, `wallet_negative_balance` and
`outbox_backlog` at OK / P2 / P1 as the design's table says, and the summaries hold only counts,
the earliest moments and at most three detail codes — no tenant, no amount. The registry order
and the empty-database lines are pinned in test_internal_alerts.py.

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
from app.models.usage import UsageEvent, UsageEventStatus
from app.models.wallet import Wallet
from app.repositories.wallet import EVENT_BILLING_STATUS_CHANGED
from app.services import alerts
from app.services.alerts import AlertContext, AlertLevel, AlertResult
from app.services.password_reset import EVENT_PASSWORD_RESET

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
                tenant_id=1,
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


@pytest.mark.parametrize(
    ("waiting", "level"),
    [(4, OK), (5, OK), (6, P2), (30, P2), (31, P1)],
)
def test_the_backlog_level_follows_the_oldest_due_event(factory, waiting: int, level) -> None:
    since = NOW - waiting * MINUTE
    add_event(factory, UsageEventStatus.RECEIVED, received_at=since)
    add_event(factory, UsageEventStatus.RECEIVED, received_at=NOW - MINUTE)

    assert check(factory, alerts.usage_processing_backlog) == AlertResult(
        level, f"2 due, oldest since {stamp(since)}; 0 PROCESSING; 0 lease expired"
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
        OK, f"1 due, oldest since {stamp(NOW - 5 * MINUTE)}; 0 PROCESSING; 0 lease expired"
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
        P2, f"1 due, oldest since {stamp(NOW - 6 * MINUTE)}; 0 PROCESSING; 0 lease expired"
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
        level, f"0 due; 1 PROCESSING, earliest claimed {stamp(claimed)}; {expired} lease expired"
    )


# --- wallet_negative_balance -------------------------------------------------------------------


def test_negative_wallets_are_counted_without_tenant_or_amount(factory) -> None:
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
        session.commit()
    assert check(factory, alerts.wallet_negative_balance) == AlertResult(OK, "no wallet below zero")

    with factory() as session:
        session.add_all([wallet(3, "-0.0625"), wallet(4, "-12.5")])
        session.commit()

    result = check(factory, alerts.wallet_negative_balance)
    assert result == AlertResult(P2, "2 wallets below zero")
    assert "0.0625" not in result.summary and "12.5" not in result.summary


# --- outbox_backlog ------------------------------------------------------------------------


def add_outbox(
    factory: sessionmaker[Session],
    event_type: str,
    created_at: dt.datetime,
    status: OutboxStatus = OutboxStatus.PENDING,
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
            )
        )
        session.commit()


def test_only_handled_outbox_rows_pending_over_10_minutes_count(factory) -> None:
    # No handler yet: waits by design. Sent: done. Pending for exactly 10 minutes: not yet.
    add_outbox(factory, EVENT_BILLING_STATUS_CHANGED, NOW - 60 * MINUTE)
    add_outbox(factory, EVENT_PASSWORD_RESET, NOW - 60 * MINUTE, OutboxStatus.SENT)
    add_outbox(factory, EVENT_PASSWORD_RESET, NOW - 10 * MINUTE)
    assert check(factory, alerts.outbox_backlog) == AlertResult(
        OK, "no handled outbox row pending over 10 minutes"
    )

    add_outbox(factory, EVENT_PASSWORD_RESET, NOW - 11 * MINUTE)

    result = check(factory, alerts.outbox_backlog)
    assert result == AlertResult(
        P2, f"1 row pending over 10 minutes, oldest created {stamp(NOW - 11 * MINUTE)}"
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

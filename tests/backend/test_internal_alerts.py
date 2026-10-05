"""`GET /internal/alerts` and its two FX dimensions (design gate #183 v3 §7, AIH-TASK-042).

Rows of design §7 covered here:

- **`fx_fetch` 维度** — only FAILED in the last 72 hours; one `NO_NEW_QUOTE`; one
  `NO_QUOTE_FOR_DATE`; no attempts at all: P2 / OK / OK / OK. Also the 72-hour edge and the
  one-line summary over several currencies;
- **`fx_stale` 维度** — the rate in effect was quoted 5 / 6 calendar days ago (a frozen Kuala
  Lumpur date across the September → October boundary, and the UTC 16:00 change of the Kuala
  Lumpur date); no rate in effect; only a future reservation: OK / P2 / P2 / P2. A manual
  version counts from the Kuala Lumpur date of `observed_at`;
- **告警接口访问控制** — no token, a wrong token, not Bearer: 401; token file not configured:
  404 without a log line; configured but missing / a directory / empty / 31 characters: 404 with
  one ERROR line that does not contain the path; the right token: 200 plain text; new file
  content takes effect without a restart. 401 and 404 run no database statement at all.

Plus the `usage_event_conflicts` dimension of design gate #176 v8 §2「告警」(AIH-TASK-029): the
conflict rows of the last 24 hours (the edge included) and the earliest of them; > 0 is P2.

SQLite only: the dimensions are plain reads with no locking (design v3), and the endpoint holds
no state the database could disagree on. Rates are obviously fictional; tokens are obvious test
values; no uuid literal is needed here (placeholders would be all zeros).
"""

from __future__ import annotations

import datetime as dt
import itertools
import logging
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.config import Settings
from app.core.database import create_session_factory
from app.core.fx_source import FxQuote, bnm_reference, divide_exactly, kuala_lumpur_date
from app.core.logging import JsonFormatter
from app.main import create_app
from app.models.auth import User, UserRole, UserStatus
from app.models.base import Base
from app.models.fx_rates import FxFetchOutcome, FxRateVersion
from app.models.usage import ConflictMismatch, UsageEventConflict
from app.services import alerts, fx_rates
from app.services.alerts import AlertContext, AlertLevel, AlertResult
from app.services.auth import RequestContext
from app.tasks.fx_fetch import add_attempt, insert_bnm_draft

TEST_EMAIL_DOMAIN = "@internal-alerts-test.example.com"
CONTEXT = RequestContext(ip_address="203.0.113.9", user_agent="internal-alerts-test")

# Obviously fictional (public repository).
FICTIONAL_RATE = "1.111111"
REFERENCE = "fictional rate for an alerts test"
TEST_TOKEN = "test-monitor-token-not-a-real-secret-0001"
OTHER_TOKEN = "test-monitor-token-not-a-real-secret-0002"

# 12:30 in Kuala Lumpur on 2026-10-01: the first Beat run of that day.
NOW = dt.datetime(2026, 10, 1, 4, 30, 0)
# Versions are published before every moment the dimensions are evaluated at.
PUBLISHED_AT = dt.datetime(2026, 9, 27, 2, 0, 0)
HOUR = dt.timedelta(hours=1)
SECOND = dt.timedelta(seconds=1)
DAY = dt.timedelta(days=1)

OK = AlertLevel.OK
P2 = AlertLevel.P2
# Besides NEW_DRAFT, the two successes design §7 names.
SUCCESSES = (FxFetchOutcome.NO_NEW_QUOTE, FxFetchOutcome.NO_QUOTE_FOR_DATE)

_EMAIL_NUMBERS = itertools.count(1)


# --- fixtures ---------------------------------------------------------------------


@pytest.fixture
def engine() -> Iterator[Engine]:
    # StaticPool: TestClient runs sync endpoints in a thread pool; the in-memory database must
    # be one connection shared by all of them. `create_all` also writes the FX lock row.
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def factory(engine: Engine) -> sessionmaker[Session]:
    return create_session_factory(engine)


@pytest.fixture
def statements(engine: Engine) -> list[str]:
    """Every SQL statement run on the engine from now on."""
    seen: list[str] = []

    def record(conn, cursor, statement, parameters, context, executemany) -> None:
        seen.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    return seen


@pytest.fixture
def app(engine: Engine, factory: sessionmaker[Session]) -> FastAPI:
    """Built in the setup phase: `create_app` reinstalls the root log handlers, and building it
    during the test would drop the handler `caplog` adds for the call phase."""
    settings = Settings(database_url="", fx_currencies="USD", monitor_token_file="")
    application = create_app(settings)
    application.state.engine = engine
    application.state.session_factory = factory
    return application


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app)


# --- helpers ----------------------------------------------------------------------


def make_admin(factory: sessionmaker[Session]) -> User:
    with factory() as session:
        user = User(
            email=f"admin-{next(_EMAIL_NUMBERS)}{TEST_EMAIL_DOMAIN}",
            password_hash="not-a-real-hash",
            role=UserRole.ADMIN,
            status=UserStatus.ACTIVE,
            created_at=PUBLISHED_AT,
            updated_at=PUBLISHED_AT,
        )
        session.add(user)
        session.commit()
        return user


def attempt(
    factory: sessionmaker[Session],
    outcome: FxFetchOutcome,
    at: dt.datetime,
    currency: str = "USD",
) -> None:
    requested = kuala_lumpur_date(at)
    with factory() as session:
        add_attempt(
            session,
            currency=currency,
            requested_date=requested,
            outcome=outcome,
            now=at,
            quote_date=requested if outcome is FxFetchOutcome.NO_NEW_QUOTE else None,
            error_code="TIMEOUT" if outcome is FxFetchOutcome.FAILED else None,
        )
        session.commit()


def publish_bnm(
    factory: sessionmaker[Session],
    quote_date: dt.date,
    *,
    currency: str = "USD",
    effective_from: dt.datetime | None = None,
    published_at: dt.datetime = PUBLISHED_AT,
) -> None:
    """A BNM draft for `quote_date`, published by an admin at `published_at`."""
    rate = Decimal(FICTIONAL_RATE)
    quote = FxQuote(
        quote_date=quote_date,
        middle_rate=rate,
        unit=1,
        rate=divide_exactly(rate, 1),
        reference=bnm_reference(currency, quote_date, 1),
    )
    with factory() as session:
        version_id = insert_bnm_draft(session, currency, quote, published_at)
        draft = session.get(FxRateVersion, version_id)
        assert draft is not None
        public_id = draft.public_id
        session.commit()
    fx_rates.publish(
        factory,
        actor=make_admin(factory),
        fx_rate_id=public_id,
        effective_from=effective_from,
        context=CONTEXT,
        clock=lambda: published_at,
    )


def publish_manual(factory: sessionmaker[Session], observed_at: dt.datetime) -> None:
    admin = make_admin(factory)
    draft = fx_rates.create_draft(
        factory,
        actor=admin,
        base_currency="USD",
        rate=Decimal(FICTIONAL_RATE),
        observed_at=observed_at,
        source_reference=REFERENCE,
        context=CONTEXT,
        clock=lambda: PUBLISHED_AT,
    )
    fx_rates.publish(
        factory,
        actor=admin,
        fx_rate_id=draft.id,
        effective_from=None,
        context=CONTEXT,
        clock=lambda: PUBLISHED_AT,
    )


def fx_fetch(
    factory: sessionmaker[Session], now: dt.datetime = NOW, currencies: tuple[str, ...] = ("USD",)
) -> AlertResult:
    with factory() as session:
        return alerts.fx_fetch(session, AlertContext(now=now, currencies=currencies))


def fx_stale(
    factory: sessionmaker[Session], now: dt.datetime = NOW, currencies: tuple[str, ...] = ("USD",)
) -> AlertResult:
    with factory() as session:
        return alerts.fx_stale(session, AlertContext(now=now, currencies=currencies))


def record_conflict(factory: sessionmaker[Session], received_at: dt.datetime, number: int) -> None:
    """One conflict row. SQLite does not enforce the foreign keys here: placeholder ids."""
    with factory() as session:
        session.add(
            UsageEventConflict(
                usage_event_id=1,
                event_id=f"00000000-0000-7000-8000-{number:012d}",
                integration_credential_id=1,
                api_key="ak_" + "0" * 32,
                payload_fingerprint="0" * 64,
                mismatch=ConflictMismatch.FINGERPRINT,
                received_at=received_at,
            )
        )
        session.commit()


def usage_event_conflicts(factory: sessionmaker[Session], now: dt.datetime = NOW) -> AlertResult:
    with factory() as session:
        return alerts.usage_event_conflicts(session, AlertContext(now=now, currencies=("USD",)))


# The endpoint's body on an empty database at NOW, one line per registered dimension.
EMPTY_DATABASE_LINES = (
    "fx_fetch OK USD no fetch in 72 hours\n"
    "fx_stale P2 USD no rate in effect\n"
    "usage_event_conflicts OK no conflicts in 24 hours\n"
    "usage_pricing_error OK no PRICING_ERROR events\n"
    "usage_fx_rate_error OK no FX_RATE_ERROR events\n"
    "usage_model_unknown OK no MODEL_UNKNOWN events\n"
    "usage_failed OK no failed events\n"
    "usage_processing_backlog OK 0 due; 0 PROCESSING; 0 lease expired\n"
    "wallet_negative_balance OK no wallet below zero\n"
    "outbox_backlog OK no handled outbox row pending over 10 minutes\n"
)


# --- the registry and the line format ---------------------------------------------


def test_the_registry_is_ordered_and_one_line_per_dimension(
    factory: sessionmaker[Session],
) -> None:
    names = [dimension.name for dimension in alerts.DIMENSIONS]
    assert names == [
        "fx_fetch",
        "fx_stale",
        "usage_event_conflicts",
        "usage_pricing_error",
        "usage_fx_rate_error",
        "usage_model_unknown",
        "usage_failed",
        "usage_processing_backlog",
        "wallet_negative_balance",
        "outbox_backlog",
    ]

    body = alerts.alert_lines(factory, ("USD",), clock=lambda: NOW)

    assert body == EMPTY_DATABASE_LINES


# --- fx_fetch -----------------------------------------------------------------------


def test_fx_fetch_is_p2_when_the_last_72_hours_only_failed(
    factory: sessionmaker[Session],
) -> None:
    for hours in (1, 25, 49):
        attempt(factory, FxFetchOutcome.FAILED, NOW - hours * HOUR)

    assert fx_fetch(factory) == AlertResult(P2, "USD no successful fetch in 72 hours (3 failed)")


@pytest.mark.parametrize("success", SUCCESSES)
def test_fx_fetch_is_ok_with_one_success_among_failures(
    factory: sessionmaker[Session], success: FxFetchOutcome
) -> None:
    attempt(factory, FxFetchOutcome.FAILED, NOW - 1 * HOUR)
    # 2026-09-29 03:30 UTC = 11:30 in Kuala Lumpur on 2026-09-29.
    attempt(factory, success, NOW - 49 * HOUR)
    attempt(factory, FxFetchOutcome.FAILED, NOW - 25 * HOUR)

    assert fx_fetch(factory) == AlertResult(OK, "USD last success 2026-09-29")


def test_fx_fetch_is_ok_without_any_attempt(factory: sessionmaker[Session]) -> None:
    # Never ran, or Beat stopped: `fx_stale` reports it (design §2).
    assert fx_fetch(factory) == AlertResult(OK, "USD no fetch in 72 hours")


def test_fx_fetch_looks_back_exactly_72_hours(factory: sessionmaker[Session]) -> None:
    attempt(factory, FxFetchOutcome.FAILED, NOW - 2 * HOUR)
    attempt(factory, FxFetchOutcome.NO_QUOTE_FOR_DATE, NOW - 72 * HOUR - SECOND)

    assert fx_fetch(factory).level is P2
    # A success exactly 72 hours ago is still inside the window.
    assert fx_fetch(factory, now=NOW - SECOND).level is OK
    # Failures older than the window alone are not an alert either.
    assert fx_fetch(factory, now=NOW + 72 * HOUR).level is OK


def test_fx_fetch_summarises_every_currency_in_one_line(
    factory: sessionmaker[Session],
) -> None:
    attempt(factory, FxFetchOutcome.NO_QUOTE_FOR_DATE, NOW - HOUR, currency="EUR")
    attempt(factory, FxFetchOutcome.NO_NEW_QUOTE, NOW - HOUR, currency="USD")
    both = ("EUR", "USD")

    # All OK: every currency, in the configured order.
    assert fx_fetch(factory, currencies=both) == AlertResult(
        OK, "EUR last success 2026-10-01; USD last success 2026-10-01"
    )

    later = NOW + 72 * HOUR
    attempt(factory, FxFetchOutcome.FAILED, later - HOUR, currency="EUR")
    attempt(factory, FxFetchOutcome.NO_NEW_QUOTE, later - HOUR, currency="USD")

    # Not OK: the worst level, and only the currencies that are not OK.
    assert fx_fetch(factory, now=later, currencies=both) == AlertResult(
        P2, "EUR no successful fetch in 72 hours (1 failed)"
    )


# --- fx_stale -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("quote_date", "expected"),
    [
        (dt.date(2026, 9, 26), AlertResult(OK, "USD quoted 2026-09-26 (5 calendar days)")),
        (dt.date(2026, 9, 25), AlertResult(P2, "USD quoted 2026-09-25 (6 calendar days)")),
    ],
)
def test_fx_stale_counts_calendar_days_across_a_month_end(
    factory: sessionmaker[Session], quote_date: dt.date, expected: AlertResult
) -> None:
    publish_bnm(factory, quote_date)

    # Kuala Lumpur's today is 2026-10-01.
    assert fx_stale(factory) == expected


def test_fx_stale_counts_from_the_kuala_lumpur_date(factory: sessionmaker[Session]) -> None:
    publish_bnm(factory, dt.date(2026, 9, 25))

    # 15:59:59 UTC is still 2026-09-30 in Kuala Lumpur: 5 days.
    before = dt.datetime(2026, 9, 30, 15, 59, 59)
    assert fx_stale(factory, now=before).level is OK
    # 16:00:00 UTC on the same UTC day is 2026-10-01 in Kuala Lumpur: 6 days.
    assert fx_stale(factory, now=before + SECOND).level is P2


def test_fx_stale_dates_a_manual_version_by_its_observed_kuala_lumpur_date(
    factory: sessionmaker[Session],
) -> None:
    # 17:00 UTC on 2026-09-25 is 01:00 on 2026-09-26 in Kuala Lumpur.
    publish_manual(factory, observed_at=dt.datetime(2026, 9, 25, 17, 0, 0))

    assert fx_stale(factory) == AlertResult(OK, "USD quoted 2026-09-26 (5 calendar days)")


def test_fx_stale_is_p2_without_a_rate_in_effect(factory: sessionmaker[Session]) -> None:
    assert fx_stale(factory) == AlertResult(P2, "USD no rate in effect")


def test_fx_stale_is_p2_with_only_a_future_reservation(
    factory: sessionmaker[Session],
) -> None:
    starts = NOW + DAY
    publish_bnm(factory, dt.date(2026, 10, 1), effective_from=starts, published_at=NOW - HOUR)

    assert fx_stale(factory) == AlertResult(P2, "USD no rate in effect")
    # The reservation itself is there: from its start it is the rate in effect.
    assert fx_stale(factory, now=starts) == AlertResult(
        OK, "USD quoted 2026-10-01 (1 calendar day)"
    )


def test_fx_stale_summarises_every_currency_in_one_line(
    factory: sessionmaker[Session],
) -> None:
    publish_bnm(factory, dt.date(2026, 9, 25), currency="USD")

    assert fx_stale(factory, currencies=("USD", "EUR")) == AlertResult(
        P2, "USD quoted 2026-09-25 (6 calendar days); EUR no rate in effect"
    )


# --- usage_event_conflicts ----------------------------------------------------------


def test_usage_event_conflicts_is_ok_without_a_conflict(factory: sessionmaker[Session]) -> None:
    assert usage_event_conflicts(factory) == AlertResult(OK, "no conflicts in 24 hours")


def test_usage_event_conflicts_counts_the_last_24_hours(factory: sessionmaker[Session]) -> None:
    # Exactly 24 hours ago is inside the window; one second earlier is not.
    record_conflict(factory, NOW - 24 * HOUR, 1)
    record_conflict(factory, NOW - HOUR, 2)
    record_conflict(factory, NOW - 24 * HOUR - SECOND, 3)

    assert usage_event_conflicts(factory) == AlertResult(
        P2, "2 conflicts in 24 hours, earliest 2026-09-30T04:30:00Z"
    )
    assert usage_event_conflicts(factory, now=NOW + 23 * HOUR) == AlertResult(
        P2, "1 conflict in 24 hours, earliest 2026-10-01T03:30:00Z"
    )
    # Older conflicts alone are not an alert: the rows stay, the window moves on.
    assert usage_event_conflicts(factory, now=NOW + 24 * HOUR).level is OK


# --- access control -----------------------------------------------------------------


def use_token_file(app: FastAPI, path: Path | str) -> None:
    """What `BILLING_MONITOR_TOKEN_FILE` would say; the endpoint reads the app's settings."""
    settings: Settings = app.state.settings
    app.state.settings = settings.model_copy(update={"monitor_token_file": str(path)})


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def assert_plain(response, status_code: int, body: str) -> None:
    assert response.status_code == status_code
    assert response.headers["content-type"].startswith("text/plain")
    # Not the §107 envelope.
    assert response.text == body


def error_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [record for record in caplog.records if record.levelno >= logging.WARNING]


@pytest.fixture
def token_file(tmp_path: Path, app: FastAPI) -> Path:
    path = tmp_path / "monitor.token"
    # Surrounding whitespace is stripped (a trailing newline from `openssl … > file`).
    path.write_text(f"  {TEST_TOKEN}\n", encoding="utf-8")
    use_token_file(app, path)
    return path


@pytest.mark.parametrize(
    "headers",
    [
        {},
        bearer(OTHER_TOKEN),
        {"Authorization": f"Basic {TEST_TOKEN}"},
        {"Authorization": "Bearer"},
    ],
    ids=["no-token", "wrong-token", "not-bearer", "empty-bearer"],
)
def test_a_missing_or_wrong_token_is_401_without_a_reason(
    client: TestClient,
    token_file: Path,
    statements: list[str],
    headers: dict[str, str],
) -> None:
    response = client.get("/internal/alerts", headers=headers)

    assert_plain(response, 401, "unauthorized\n")
    assert response.headers["www-authenticate"] == "Bearer"
    # The token is judged before any database query.
    assert statements == []


def test_an_unconfigured_token_file_is_404_without_a_log_line(
    client: TestClient,
    statements: list[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.INFO):
        response = client.get("/internal/alerts", headers=bearer(TEST_TOKEN))

    assert_plain(response, 404, "not found\n")
    assert error_records(caplog) == []
    assert statements == []


@pytest.mark.parametrize("state", ["missing", "directory", "empty", "31-characters"])
def test_an_unusable_token_file_is_404_with_an_error_line_without_the_path(
    tmp_path: Path,
    app: FastAPI,
    client: TestClient,
    statements: list[str],
    caplog: pytest.LogCaptureFixture,
    state: str,
) -> None:
    path = tmp_path / "monitor.token"
    if state == "directory":
        # What compose mounts when the `file:` secret is missing.
        path.mkdir()
    elif state == "empty":
        path.write_text("", encoding="utf-8")
    elif state == "31-characters":
        path.write_text(TEST_TOKEN[:31], encoding="utf-8")
    use_token_file(app, path)

    with caplog.at_level(logging.INFO):
        response = client.get("/internal/alerts", headers=bearer(TEST_TOKEN[:31]))

    assert_plain(response, 404, "not found\n")
    assert statements == []
    records = error_records(caplog)
    assert len(records) == 1
    assert records[0].name == "app.api.internal_alerts"
    assert records[0].levelno == logging.ERROR
    for value in vars(records[0]).values():
        assert str(tmp_path) not in str(value)
    written = JsonFormatter().format(records[0])
    assert "monitor.token" not in written
    assert TEST_TOKEN[:31] not in written


def test_the_right_token_gets_the_alert_lines(
    client: TestClient,
    token_file: Path,
    statements: list[str],
) -> None:
    response = client.get("/internal/alerts", headers=bearer(TEST_TOKEN))

    assert_plain(response, 200, EMPTY_DATABASE_LINES)
    # Plain reads: never the FX lock, never a write.
    assert statements
    assert not [sql for sql in statements if "fx_rate_locks" in sql]
    assert all(sql.lstrip().upper().startswith("SELECT") for sql in statements)


def test_the_alert_lines_carry_no_rate(
    client: TestClient, token_file: Path, factory: sessionmaker[Session]
) -> None:
    publish_bnm(factory, dt.date(2026, 9, 25))

    response = client.get("/internal/alerts", headers=bearer(TEST_TOKEN))

    assert response.status_code == 200
    assert FICTIONAL_RATE not in response.text
    assert "@" not in response.text


def test_a_new_token_takes_effect_without_a_restart(client: TestClient, token_file: Path) -> None:
    assert client.get("/internal/alerts", headers=bearer(TEST_TOKEN)).status_code == 200

    token_file.write_text(OTHER_TOKEN, encoding="utf-8")

    assert client.get("/internal/alerts", headers=bearer(TEST_TOKEN)).status_code == 401
    assert client.get("/internal/alerts", headers=bearer(OTHER_TOKEN)).status_code == 200

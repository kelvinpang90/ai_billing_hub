"""The BNM fetch task: drafts and fetch attempts (design gate #183 v3 §7, AIH-TASK-040).

Rows of design §7 covered here:

- **拉取幂等** — the same quote date fetched three times gives one draft and two
  `NO_NEW_QUOTE`; two concurrent fetches (real MySQL, two connections) give one draft, and the
  loser records `NO_NEW_QUOTE`;
- **无记录** — BNM's「No records found.」gives `NO_QUOTE_FOR_DATE` and no draft (the
  「`fx_fetch` 仍 OK」half of that row belongs to the alerts task, AIH-TASK-042);
- **BNM 更正同日价格** — a different rate on the same quote date leaves the draft as it was and
  records `QUOTE_CHANGED`; a different literal that restores to the same rate does not;
- **不自动发布** — a successful fetch only adds a draft: `resolve_fx_rate` still returns the
  version published before;
- **事务中途失败** (the fetch part) — writing the attempt raises after the draft was flushed:
  neither remains, the other currencies are still fetched and written, and the task ends failed.

Every case runs once on SQLite and once on a real MySQL (`factory` fixture) except the
concurrent fetch, which needs two connections on MySQL, and the Celery wiring (SQLite). Rates
are obviously fictional, except the two bodies built from the USD recording of design §10
(marked where they are built); no uuid literal is needed here (placeholders would be all
zeros).

The MySQL half needs `BILLING_TEST_DATABASE_URL` pointing at a database that may be wiped; it
skips without it — **a skip is not a pass**, CI sets it and treats a skip as a failure. As in
every other MySQL test module here, the fixture brings that throwaway test database to alembic
head before use; no other database is touched.
"""

from __future__ import annotations

import datetime as dt
import email.message
import io
import itertools
import logging
import os
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import pytest
from alembic.config import Config
from sqlalchemy import Engine, create_engine, delete, func, select
from sqlalchemy.orm import Session, sessionmaker

from alembic import command
from app.core.config import Settings
from app.core.database import create_session_factory
from app.core.fx_source import (
    BnmFxSource,
    FxQuote,
    FxSourceError,
    bnm_reference,
    divide_exactly,
    kuala_lumpur_date,
)
from app.models.auth import AuditLog, User, UserRole, UserStatus
from app.models.base import Base
from app.models.fx_rates import (
    FxFetchAttempt,
    FxFetchOutcome,
    FxRateSourceType,
    FxRateStatus,
    FxRateVersion,
)
from app.repositories.fx_rates import lock_fx_rates_shared, resolve_fx_rate
from app.services import fx_rates
from app.services.auth import RequestContext, utc_now
from app.tasks import fx_fetch
from app.tasks.fx_fetch import QUOTE_CHANGED, FxFetchIncomplete, fetch_all, fetch_currency

TEST_DATABASE_URL = os.environ.get("BILLING_TEST_DATABASE_URL", "")
TEST_EMAIL_DOMAIN = "@fx-fetch-test.example.com"
CONTEXT = RequestContext(ip_address="203.0.113.9", user_agent="fx-fetch-test")

# Obviously fictional rates (public repository), with BNM's at most 6 places.
FICTIONAL_RATE = "1.111111"
OTHER_RATE = "2.222222"

# 12:30:00.4 in Kuala Lumpur on 2026-10-01: the first Beat run of that day.
NOW = dt.datetime(2026, 10, 1, 4, 30, 0, 400_000)
TODAY = dt.date(2026, 10, 1)

NEW_DRAFT = FxFetchOutcome.NEW_DRAFT
NO_NEW_QUOTE = FxFetchOutcome.NO_NEW_QUOTE
NO_QUOTE_FOR_DATE = FxFetchOutcome.NO_QUOTE_FOR_DATE
FAILED = FxFetchOutcome.FAILED

_EMAIL_NUMBERS = itertools.count(1)


# --- helpers ----------------------------------------------------------------------


class Clock:
    def __init__(self, moment: dt.datetime = NOW) -> None:
        self.now = moment

    def __call__(self) -> dt.datetime:
        return self.now


def fictional_quote(
    rate: str = FICTIONAL_RATE,
    *,
    unit: int = 1,
    quote_date: dt.date = TODAY,
    currency: str = "USD",
) -> FxQuote:
    middle_rate = Decimal(rate)
    return FxQuote(
        quote_date=quote_date,
        middle_rate=middle_rate,
        unit=unit,
        rate=divide_exactly(middle_rate, unit),
        reference=bnm_reference(currency, quote_date, unit),
    )


class ScriptedSource:
    """Answers each currency's fetches in turn: a quote, `None` (no records) or an error."""

    def __init__(self, answers: dict[str, list[FxQuote | None | FxSourceError]]) -> None:
        self.answers = {currency: list(items) for currency, items in answers.items()}
        self.calls: list[tuple[str, dt.date]] = []

    def fetch(self, currency: str, quote_date: dt.date) -> FxQuote | None:
        self.calls.append((currency, quote_date))
        answer = self.answers[currency].pop(0)
        if isinstance(answer, FxSourceError):
            raise answer
        return answer


def http_source(*bodies: tuple[int, str]) -> BnmFxSource:
    """The real adapter over a fake transport that answers the given (status, body) in turn."""
    pending = list(bodies)

    class Response:
        def __init__(self, status: int, body: bytes) -> None:
            self.status = status
            self.body = body

        def read(self) -> bytes:
            return self.body

        def close(self) -> None:
            return None

    def transport(request: urllib.request.Request, timeout: float) -> Response:
        status, body = pending.pop(0)
        if status != 200:
            headers = email.message.Message()
            raise urllib.error.HTTPError(
                request.full_url, status, "fake", headers, io.BytesIO(body.encode())
            )
        return Response(status, body.encode())

    return BnmFxSource("https://bnm.example.com", 10, transport=transport)


def fetch_one(
    factory: sessionmaker[Session], source, currency: str = "USD", clock: Clock | None = None
) -> FxFetchOutcome:
    clock = clock or Clock()
    return fetch_currency(factory, source, currency, kuala_lumpur_date(clock()), clock=clock)


def versions(factory: sessionmaker[Session], currency: str = "USD") -> list[FxRateVersion]:
    statement = (
        select(FxRateVersion)
        .where(FxRateVersion.base_currency == currency)
        .order_by(FxRateVersion.id)
    )
    with factory() as session:
        return list(session.execute(statement).scalars())


def attempts(factory: sessionmaker[Session], currency: str = "USD") -> list[FxFetchAttempt]:
    statement = (
        select(FxFetchAttempt)
        .where(FxFetchAttempt.base_currency == currency)
        .order_by(FxFetchAttempt.id)
    )
    with factory() as session:
        return list(session.execute(statement).scalars())


def audit_count(factory: sessionmaker[Session]) -> int:
    with factory() as session:
        return session.execute(select(func.count()).select_from(AuditLog)).scalar_one()


def make_admin(factory: sessionmaker[Session]) -> User:
    with factory() as session:
        user = User(
            email=f"admin-{next(_EMAIL_NUMBERS)}{TEST_EMAIL_DOMAIN}",
            password_hash="not-a-real-hash",
            role=UserRole.ADMIN,
            status=UserStatus.ACTIVE,
            created_at=NOW,
            updated_at=NOW,
        )
        session.add(user)
        session.commit()
        return user


def resolve(factory: sessionmaker[Session], moment: dt.datetime) -> str | None:
    """The public id of the USD version in effect, resolved the way billing will (T-H)."""
    with factory() as session:
        lock_fx_rates_shared(session)
        found = resolve_fx_rate(session, "USD", moment)
        session.commit()
    return None if found is None else found.public_id


# --- fixtures ---------------------------------------------------------------------


@pytest.fixture(params=["sqlite", "mysql"])
def factory(request, monkeypatch) -> Iterator[sessionmaker[Session]]:
    if request.param == "mysql":
        yield from _mysql_factory(monkeypatch)
        return
    yield from _sqlite_factory()


@pytest.fixture
def mysql_factory(monkeypatch) -> Iterator[sessionmaker[Session]]:
    yield from _mysql_factory(monkeypatch)


def _sqlite_factory() -> Iterator[sessionmaker[Session]]:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    yield create_session_factory(engine)
    engine.dispose()


def _mysql_factory(monkeypatch) -> Iterator[sessionmaker[Session]]:
    if not TEST_DATABASE_URL:
        pytest.skip("BILLING_TEST_DATABASE_URL is not set; the MySQL half needs a real MySQL")
    # Tables through alembic: the unique constraints, checks and triggers of migration 0015.
    from app.core.config import get_settings

    monkeypatch.setenv("BILLING_DATABASE_URL", TEST_DATABASE_URL)
    get_settings.cache_clear()
    command.upgrade(Config("alembic.ini"), "head")
    get_settings.cache_clear()

    engine = create_engine(TEST_DATABASE_URL, pool_size=4, max_overflow=2)
    clean(engine)
    try:
        yield create_session_factory(engine)
    finally:
        clean(engine)
        engine.dispose()


def clean(engine: Engine) -> None:
    """⚠️ The database is shared: wipe before and after every case.

    Versions refuse DELETE and attempts are append-only, so TRUNCATE (DDL, no triggers) with
    foreign key checks off; `fx_rate_locks` keeps its one row. The audit table is append-only too.
    """
    with engine.begin() as connection:
        connection.exec_driver_sql("SET FOREIGN_KEY_CHECKS = 0")
        try:
            connection.exec_driver_sql("TRUNCATE TABLE fx_fetch_attempts")
            connection.exec_driver_sql("TRUNCATE TABLE fx_rate_versions")
        finally:
            connection.exec_driver_sql("SET FOREIGN_KEY_CHECKS = 1")
    with engine.begin() as connection:
        connection.exec_driver_sql("TRUNCATE TABLE audit_logs")
    with engine.begin() as connection:
        connection.execute(delete(User).where(User.email.like(f"%{TEST_EMAIL_DOMAIN}")))


# --- the four outcomes ------------------------------------------------------------


def test_a_quote_becomes_one_bnm_draft_and_a_new_draft_attempt(factory) -> None:
    """NEW_DRAFT: the draft carries the BNM reference, the quote date and 12:00 Kuala Lumpur."""
    source = ScriptedSource({"USD": [fictional_quote()]})

    assert fetch_one(factory, source) == NEW_DRAFT

    assert source.calls == [("USD", TODAY)]
    (draft,) = versions(factory)
    assert draft.status == FxRateStatus.DRAFT
    assert draft.source == FxRateSourceType.BNM
    assert draft.quote_currency == "MYR"
    assert draft.rate == Decimal(FICTIONAL_RATE)
    reference = "bnm:exchange-rate:USD:2026-10-01:session=1200:middle_rate:unit=1"
    assert draft.source_reference == reference
    assert draft.source_quote_date == TODAY
    # 报价日 12:00 吉隆坡 = 当日 04:00 UTC（固定 +8）。
    assert draft.observed_at == dt.datetime(2026, 10, 1, 4, 0, 0)
    assert (draft.effective_from, draft.effective_to) == (None, None)
    assert (draft.created_by, draft.approved_by, draft.approved_at) == (None, None, None)
    assert draft.created_at == draft.updated_at == dt.datetime(2026, 10, 1, 4, 30, 0)

    (attempt,) = attempts(factory)
    assert attempt.outcome == NEW_DRAFT
    assert attempt.source == FxRateSourceType.BNM
    assert attempt.requested_date == TODAY
    assert attempt.quote_date == TODAY
    assert attempt.error_code is None
    assert attempt.fx_rate_version_id == draft.id
    assert attempt.attempted_at == dt.datetime(2026, 10, 1, 4, 30, 0)


def test_a_bnm_draft_writes_no_audit(factory) -> None:
    """设计 §2 审计：拉取记录就是 BNM 草稿的来源证明，操作者是系统。"""
    fetch_one(factory, ScriptedSource({"USD": [fictional_quote()]}))

    assert len(versions(factory)) == 1
    assert audit_count(factory) == 0


def test_a_failed_fetch_records_its_code_and_no_draft(factory) -> None:
    source = ScriptedSource({"USD": [FxSourceError("TIMEOUT")]})

    assert fetch_one(factory, source) == FAILED

    assert versions(factory) == []
    (attempt,) = attempts(factory)
    assert (attempt.outcome, attempt.error_code) == (FAILED, "TIMEOUT")
    assert (attempt.quote_date, attempt.fx_rate_version_id) == (None, None)
    assert attempt.requested_date == TODAY


# --- 无记录 -------------------------------------------------------------------------


def test_no_records_is_no_quote_for_date_and_no_draft(factory) -> None:
    """BNM 答「无记录」（周末、公众假期、中午场尚未公布）：成功，但没有草稿。"""
    source = http_source((404, '{"message":"No records found.","code":404}'))

    assert fetch_one(factory, source) == NO_QUOTE_FOR_DATE

    assert versions(factory) == []
    (attempt,) = attempts(factory)
    assert attempt.outcome == NO_QUOTE_FOR_DATE
    assert attempt.requested_date == TODAY
    assert attempt.quote_date is None
    assert attempt.error_code is None
    assert attempt.fx_rate_version_id is None


# --- 拉取幂等 ---------------------------------------------------------------------


def test_three_fetches_of_one_quote_date_make_one_draft(factory) -> None:
    """Beat runs three times a day; the quote date's unique constraint keeps one draft."""
    source = ScriptedSource({"USD": [fictional_quote(), fictional_quote(), fictional_quote()]})
    clock = Clock()

    outcomes = []
    for moment in (NOW, NOW.replace(hour=6), NOW.replace(hour=9)):
        clock.now = moment
        outcomes.append(fetch_one(factory, source, clock=clock))

    assert outcomes == [NEW_DRAFT, NO_NEW_QUOTE, NO_NEW_QUOTE]
    (draft,) = versions(factory)
    recorded = attempts(factory)
    assert [attempt.outcome for attempt in recorded] == outcomes
    assert [attempt.error_code for attempt in recorded] == [None, None, None]
    assert [attempt.fx_rate_version_id for attempt in recorded] == [draft.id, None, None]
    assert [attempt.quote_date for attempt in recorded] == [TODAY, TODAY, TODAY]


def test_a_quote_date_with_any_bnm_version_is_no_new_quote(factory) -> None:
    """Even after the draft was discarded: the quote date already has its BNM version."""
    fetch_one(factory, ScriptedSource({"USD": [fictional_quote()]}))
    (draft,) = versions(factory)
    fx_rates.discard_draft(
        factory, actor=make_admin(factory), fx_rate_id=draft.public_id, context=CONTEXT
    )

    assert fetch_one(factory, ScriptedSource({"USD": [fictional_quote()]})) == NO_NEW_QUOTE
    assert [version.status for version in versions(factory)] == [FxRateStatus.DISCARDED]


def test_the_loser_of_a_race_records_no_new_quote(factory, monkeypatch) -> None:
    """The insert hits the quote date's unique constraint; a new transaction reads the winner.

    Simulated on both databases: the first existence check is made to miss the version that is
    already there, as it would for a fetch that read before the other one committed.
    """
    fetch_one(factory, ScriptedSource({"USD": [fictional_quote()]}))
    real = fx_fetch.existing_bnm_version
    calls = itertools.count()

    def stale_first(session: Session, currency: str, quote_date: dt.date):
        return None if next(calls) == 0 else real(session, currency, quote_date)

    monkeypatch.setattr(fx_fetch, "existing_bnm_version", stale_first)

    assert fetch_one(factory, ScriptedSource({"USD": [fictional_quote()]})) == NO_NEW_QUOTE
    assert len(versions(factory)) == 1
    assert [attempt.outcome for attempt in attempts(factory)] == [NEW_DRAFT, NO_NEW_QUOTE]


def test_two_concurrent_fetches_make_one_draft(mysql_factory, monkeypatch) -> None:
    """设计 §4：手工重触发与定时同时跑，唯一约束决出一条草稿，输家记 `NO_NEW_QUOTE`.

    Both fetches pass their existence check before either inserts (a barrier holds them there).
    One INSERT goes first; the other **blocks** on the quote date's unique key until the first
    commits, then fails with a duplicate key, rolls its transaction back and, in a new
    transaction, reads the committed winner and records `NO_NEW_QUOTE`. After both: one draft,
    two attempts.
    """
    real = fx_fetch.existing_bnm_version
    barrier = threading.Barrier(2, timeout=30)
    first_call = threading.local()

    def check_then_wait(session: Session, currency: str, quote_date: dt.date):
        found = real(session, currency, quote_date)
        if not getattr(first_call, "done", False):
            first_call.done = True
            barrier.wait()
        return found

    monkeypatch.setattr(fx_fetch, "existing_bnm_version", check_then_wait)
    quote = fictional_quote()

    class SameQuote:
        def fetch(self, currency: str, quote_date: dt.date) -> FxQuote:
            return quote

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(fetch_one, mysql_factory, SameQuote()) for _ in range(2)]
        outcomes = sorted(future.result(timeout=60) for future in futures)

    assert outcomes == sorted([NEW_DRAFT, NO_NEW_QUOTE])
    (draft,) = versions(mysql_factory)
    recorded = attempts(mysql_factory)
    assert sorted(attempt.outcome for attempt in recorded) == outcomes
    assert {attempt.fx_rate_version_id for attempt in recorded} == {draft.id, None}
    assert [attempt.error_code for attempt in recorded] == [None, None]


# --- BNM 更正同日价格 -------------------------------------------------------------------


def test_a_different_rate_for_the_same_quote_date_is_quote_changed(factory) -> None:
    """设计 §4「相同 ID、不同载荷」：已有版本不变，记 `NO_NEW_QUOTE` + `QUOTE_CHANGED`."""
    source = ScriptedSource({"USD": [fictional_quote(), fictional_quote(OTHER_RATE)]})

    assert fetch_one(factory, source) == NEW_DRAFT
    assert fetch_one(factory, source) == NO_NEW_QUOTE

    (draft,) = versions(factory)
    assert draft.rate == Decimal(FICTIONAL_RATE)
    second = attempts(factory)[1]
    assert (second.outcome, second.error_code) == (NO_NEW_QUOTE, QUOTE_CHANGED)
    assert (second.quote_date, second.fx_rate_version_id) == (TODAY, None)


def test_the_rates_are_compared_after_dividing_by_the_unit(factory) -> None:
    """The same rate quoted per 1 and per 100 units is not a correction."""
    per_hundred = fictional_quote("111.1111", unit=100)
    assert per_hundred.rate == Decimal(FICTIONAL_RATE)
    source = ScriptedSource({"USD": [fictional_quote(), per_hundred]})

    fetch_one(factory, source)
    assert fetch_one(factory, source) == NO_NEW_QUOTE

    assert attempts(factory)[1].error_code is None


def test_a_different_literal_that_restores_to_the_same_rate_is_not_quote_changed(
    factory,
) -> None:
    """The comparison is on the restored `Decimal` value, not on BNM's binary64 literal."""
    # 2026-09-29 12:30 Kuala Lumpur, so the requested date matches the recording.
    clock = Clock(dt.datetime(2026, 9, 29, 4, 30, 0))
    recorded = '{"data":{"currency_code":"USD","unit":1,"rate":{"date":"2026-09-29","buying_rate":4.0789999999999997,"selling_rate":4.0869999999999997,"middle_rate":4.0830000000000002}},"meta":{"quote":"rm","session":"1200","last_updated":"2026-09-30 11:56:16","total_result":1}}'  # noqa: E501
    # 派生自 2026-09-29 USD 录制：`middle_rate` 写成最短形式 4.083，还原后与录制相同。
    derived = recorded.replace('"middle_rate":4.0830000000000002', '"middle_rate":4.083')
    assert derived != recorded
    source = http_source((200, recorded), (200, derived))

    assert fetch_one(factory, source, clock=clock) == NEW_DRAFT
    assert fetch_one(factory, source, clock=clock) == NO_NEW_QUOTE

    (draft,) = versions(factory)
    assert draft.rate == Decimal("4.083")
    assert [attempt.error_code for attempt in attempts(factory)] == [None, None]


# --- 不自动发布 ----------------------------------------------------------------------


def test_a_fetched_draft_is_never_published(factory) -> None:
    """§17.1、ADR-0005 §3：拉取成功只有草稿，`resolve_fx_rate` 仍取之前发布的版本。"""
    admin = make_admin(factory)
    published_at = Clock(dt.datetime(2026, 10, 1, 3, 0, 0))
    manual = fx_rates.create_draft(
        factory,
        actor=admin,
        base_currency="USD",
        rate=Decimal(OTHER_RATE),
        observed_at=dt.datetime(2026, 9, 30, 4, 0, 0),
        source_reference="fictional rate for a fetch test",
        context=CONTEXT,
        clock=published_at,
    )
    fx_rates.publish(
        factory,
        actor=admin,
        fx_rate_id=manual.id,
        effective_from=None,
        context=CONTEXT,
        clock=published_at,
    )
    audits_before = audit_count(factory)

    assert fetch_one(factory, ScriptedSource({"USD": [fictional_quote()]})) == NEW_DRAFT

    bnm = [version for version in versions(factory) if version.source == FxRateSourceType.BNM]
    (draft,) = bnm
    assert draft.status == FxRateStatus.DRAFT
    assert (draft.effective_from, draft.effective_to, draft.approved_at) == (None, None, None)
    for moment in (NOW, dt.datetime(2026, 10, 2, 12, 0, 0)):
        assert resolve(factory, moment) == manual.id
    assert audit_count(factory) == audits_before


# --- each currency on its own; a write failing half way -------------------------------


def test_each_currency_is_fetched_and_written_on_its_own(factory) -> None:
    """One currency failing to fetch does not stop the others; all use Kuala Lumpur's today."""
    source = ScriptedSource(
        {
            "USD": [FxSourceError("HTTP_500")],
            "EUR": [fictional_quote(currency="EUR")],
            "JPY": [None],
        }
    )
    # 16:00 UTC is already the next day in Kuala Lumpur.
    clock = Clock(dt.datetime(2026, 9, 30, 16, 0, 0))

    outcomes = fetch_all(factory, source, ("USD", "EUR", "JPY"), clock=clock)

    assert outcomes == {"USD": "FAILED", "EUR": "NEW_DRAFT", "JPY": "NO_QUOTE_FOR_DATE"}
    assert source.calls == [("USD", TODAY), ("EUR", TODAY), ("JPY", TODAY)]
    assert [version.base_currency for version in versions(factory, "EUR")] == ["EUR"]
    for currency in ("USD", "EUR", "JPY"):
        (attempt,) = attempts(factory, currency)
        assert attempt.requested_date == TODAY


def test_an_attempt_that_cannot_be_written_rolls_its_draft_back(
    factory, monkeypatch, caplog
) -> None:
    """INV-13：草稿与拉取记录同一事务。写拉取记录抛错 → 草稿也不在。

    The failing currency is only logged; the next currency is still fetched and written; once
    every currency was handled the task ends failed (`FxFetchIncomplete`).
    """
    real = fx_fetch.add_attempt

    def failing_for_usd(session: Session, *, currency: str, **kwargs) -> None:
        if currency == "USD":
            session.flush()
            raise RuntimeError("the attempt could not be written")
        real(session, currency=currency, **kwargs)

    monkeypatch.setattr(fx_fetch, "add_attempt", failing_for_usd)
    source = ScriptedSource({"USD": [fictional_quote()], "EUR": [fictional_quote(currency="EUR")]})

    with caplog.at_level(logging.ERROR, logger=fx_fetch.__name__):
        with pytest.raises(FxFetchIncomplete):
            fetch_all(factory, source, ("USD", "EUR"), clock=Clock())

    assert versions(factory, "USD") == []
    assert attempts(factory, "USD") == []
    assert len(versions(factory, "EUR")) == 1
    assert [attempt.outcome for attempt in attempts(factory, "EUR")] == [NEW_DRAFT]
    assert source.calls == [("USD", TODAY), ("EUR", TODAY)]
    assert any(record.levelno == logging.ERROR for record in caplog.records)

    # The next run fetches USD again and writes it.
    monkeypatch.setattr(fx_fetch, "add_attempt", real)
    assert fetch_one(factory, ScriptedSource({"USD": [fictional_quote()]})) == NEW_DRAFT


# --- the Celery task ------------------------------------------------------------------


def test_the_task_fetches_the_configured_currencies_for_kuala_lumpur_today(
    monkeypatch,
) -> None:
    """`fetch()` is what Beat (and a manual run) calls: settings, factory and source wired."""
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = create_session_factory(engine)
    source = ScriptedSource({"USD": [None], "EUR": [None]})
    monkeypatch.setattr(fx_fetch, "_session_factory", lambda: session_factory)
    monkeypatch.setattr(fx_fetch, "get_settings", lambda: Settings(fx_currencies="USD,EUR"))
    monkeypatch.setattr(fx_fetch, "build_fx_source", lambda _settings: source)

    before = kuala_lumpur_date(utc_now())
    outcomes = fx_fetch.fetch()
    after = kuala_lumpur_date(utc_now())

    assert outcomes == {"USD": "NO_QUOTE_FOR_DATE", "EUR": "NO_QUOTE_FOR_DATE"}
    assert [currency for currency, _ in source.calls] == ["USD", "EUR"]
    assert all(quote_date in (before, after) for _, quote_date in source.calls)
    engine.dispose()

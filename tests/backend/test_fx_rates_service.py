"""FX rate versions over time, through the services (design gate #183 v3 §2, §7).

Groups:

- **the examples E1–E16** of design §2, one case each (the function name carries the number):
  the "before" is built through the services with a frozen clock, the "operation" is run, and
  every row of the currency pair is compared with the "after" (status and period), plus the
  result (success or error code). E9 ① and E13 also resolve moments around the gap and the
  reservation start. Where an example has alternatives from the same "before" (E9 ① / ②, the
  four rows of E16) each alternative runs on its own currency: the pairs are independent;
- **drafts**: manual create / edit / discard, the BNM draft that cannot be edited, the state
  table's 409s, input validation, and the audit content;
- **the recheck** (design §2「事务内复查」, unit) and **the lock** (first statement, `t` after it);
- **a write failing half way** (design §7「事务中途失败」): the publish audit failing after the
  truncation, the withdrawal audit failing after the previous version was restored, and the
  other writes; nothing remains.

Every case except the recheck units and the SQLite-only injections runs once on SQLite and once
on a real MySQL (`factory` fixture). The concurrency and property cases are in
test_fx_rates_resolve.py (AIH-TASK-044). Rates are fictional; placeholder uuids are all zeros.

The MySQL half needs `BILLING_TEST_DATABASE_URL` pointing at a database that may be wiped; it
skips without it — **a skip is not a pass**, CI sets it and treats a skip as a failure.
"""

from __future__ import annotations

import datetime as dt
import itertools
import json
import os
import re
import uuid
from collections.abc import Callable, Iterator
from decimal import Decimal

import pytest
from alembic.config import Config
from sqlalchemy import Engine, create_engine, delete, event, func, select
from sqlalchemy.orm import Session, sessionmaker

from alembic import command
from app.core.database import create_session_factory
from app.core.errors import AppError
from app.models.auth import AuditAction, AuditLog, User, UserRole, UserStatus
from app.models.base import Base
from app.models.fx_rates import FxRateSourceType, FxRateStatus, FxRateVersion
from app.repositories import fx_rates as fx_repository
from app.repositories.fx_rates import resolve_fx_rate
from app.services import fx_rates
from app.services.auth import RequestContext
from app.services.fx_rates import FxPeriodsBroken, FxRateVersionView, verify_periods

TEST_DATABASE_URL = os.environ.get("BILLING_TEST_DATABASE_URL", "")
TEST_EMAIL_DOMAIN = "@fx-rates-service-test.example.com"
CONTEXT = RequestContext(ip_address="203.0.113.9", user_agent="fx-rates-service-test")

# Obviously fictional rates (public repository): 1 unit of the base currency in MYR.
FICTIONAL_RATE = "1.1111111111"
OTHER_RATE = "2.2222222222"
REFERENCE = "fictional rate for a service test"
OBSERVED_AT = dt.datetime(2026, 9, 30, 4, 0, 0)
MISSING_ID = "00000000-0000-0000-0000-000000000000"

# Design §2: the examples' dates are all 2026-10-01 UTC; "次日 04:30" is 2026-10-02 04:30:00.
NEXT_0430 = dt.datetime(2026, 10, 2, 4, 30, 0)
OCT3_0430 = dt.datetime(2026, 10, 3, 4, 30, 0)
SECOND = dt.timedelta(seconds=1)

PUBLISHED = "PUBLISHED"
RETIRED = "RETIRED"
DRAFT = "DRAFT"
DISCARDED = "DISCARDED"

_EMAIL_NUMBERS = itertools.count(1)
# Connection housekeeping a driver may run; not statements of the service.
_HOUSEKEEPING = re.compile(r"^\s*(SET|SHOW|SELECT @@)", re.IGNORECASE)


def at(hour: int, minute: int = 0, second: int = 0, micro: int = 0) -> dt.datetime:
    """A moment on 2026-10-01 UTC."""
    return dt.datetime(2026, 10, 1, hour, minute, second, micro)


# --- helpers ----------------------------------------------------------------------


class Clock:
    """A frozen clock. Publish and retire call it once they hold the FX lock."""

    def __init__(self, moment: dt.datetime) -> None:
        self.now = moment

    def __call__(self) -> dt.datetime:
        return self.now

    def set(self, moment: dt.datetime) -> None:
        self.now = moment


def make_admin(factory: sessionmaker[Session]) -> User:
    """A committed ADMIN, detached with its columns loaded — what `require_admin` returns."""
    with factory() as session:
        user = User(
            email=f"admin-{next(_EMAIL_NUMBERS)}{TEST_EMAIL_DOMAIN}",
            password_hash="not-a-real-hash",
            role=UserRole.ADMIN,
            status=UserStatus.ACTIVE,
            created_at=OBSERVED_AT,
            updated_at=OBSERVED_AT,
        )
        session.add(user)
        session.commit()
        return user


class Fx:
    """One admin driving the FX services with a frozen clock."""

    def __init__(self, factory: sessionmaker[Session], clock: Callable[[], dt.datetime]) -> None:
        self.factory = factory
        self.clock = clock
        self.admin = make_admin(factory)

    def set(self, moment: dt.datetime) -> None:
        assert isinstance(self.clock, Clock)
        self.clock.set(moment)

    def create(self, base: str = "USD", rate: str = FICTIONAL_RATE) -> FxRateVersionView:
        return fx_rates.create_draft(
            self.factory,
            actor=self.admin,
            base_currency=base,
            rate=Decimal(rate),
            observed_at=OBSERVED_AT,
            source_reference=REFERENCE,
            context=CONTEXT,
            clock=self.clock,
        )

    def draft(self, base: str = "USD", rate: str = FICTIONAL_RATE) -> str:
        return self.create(base, rate).id

    def edit(
        self,
        fx_rate_id: str,
        *,
        rate: str | None = None,
        observed_at: dt.datetime | None = None,
        source_reference: str | None = None,
    ) -> FxRateVersionView:
        return fx_rates.update_draft(
            self.factory,
            actor=self.admin,
            fx_rate_id=fx_rate_id,
            rate=None if rate is None else Decimal(rate),
            observed_at=observed_at,
            source_reference=source_reference,
            context=CONTEXT,
            clock=self.clock,
        )

    def discard(self, fx_rate_id: str) -> FxRateVersionView:
        return fx_rates.discard_draft(
            self.factory, actor=self.admin, fx_rate_id=fx_rate_id, context=CONTEXT, clock=self.clock
        )

    def publish(
        self, fx_rate_id: str, effective_from: dt.datetime | None = None
    ) -> FxRateVersionView:
        return fx_rates.publish(
            self.factory,
            actor=self.admin,
            fx_rate_id=fx_rate_id,
            effective_from=effective_from,
            context=CONTEXT,
            clock=self.clock,
        )

    def publish_new(self, base: str = "USD", effective_from: dt.datetime | None = None) -> str:
        return self.publish(self.draft(base), effective_from).id

    def retire(self, fx_rate_id: str, reason: str = "Fictional retirement") -> FxRateVersionView:
        return fx_rates.retire(
            self.factory,
            actor=self.admin,
            fx_rate_id=fx_rate_id,
            reason=reason,
            context=CONTEXT,
            clock=self.clock,
        )

    def resolve(self, moment: dt.datetime, base: str = "USD") -> str | None:
        """The public id of the version in effect, resolved the way billing will (T-H)."""
        with self.factory() as session:
            fx_repository.lock_fx_rates_shared(session)
            found = resolve_fx_rate(session, base, moment)
            session.commit()
        return None if found is None else found.public_id


def rows(
    factory: sessionmaker[Session], base: str = "USD"
) -> dict[str, tuple[str, dt.datetime | None, dt.datetime | None]]:
    """Every version of the pair as committed: public id → (status, from, to)."""
    statement = (
        select(
            FxRateVersion.public_id,
            FxRateVersion.status,
            FxRateVersion.effective_from,
            FxRateVersion.effective_to,
        )
        .where(FxRateVersion.base_currency == base)
        .order_by(FxRateVersion.id)
    )
    with factory() as session:
        return {
            public_id: (FxRateStatus(status).value, start, end)
            for public_id, status, start, end in session.execute(statement)
        }


def snapshot(factory: sessionmaker[Session]) -> dict[str, object]:
    """Every version column a write may touch, as committed, plus the audit count."""
    statement = select(
        FxRateVersion.public_id,
        FxRateVersion.status,
        FxRateVersion.rate,
        FxRateVersion.observed_at,
        FxRateVersion.source_reference,
        FxRateVersion.effective_from,
        FxRateVersion.effective_to,
        FxRateVersion.approved_by,
        FxRateVersion.approved_at,
        FxRateVersion.updated_at,
    ).order_by(FxRateVersion.id)
    with factory() as session:
        versions = [tuple(row) for row in session.execute(statement)]
        audits = session.execute(select(func.count()).select_from(AuditLog)).scalar_one()
    return {"versions": versions, "audits": audits}


def audit_states(factory: sessionmaker[Session], action: AuditAction) -> list[tuple]:
    """(entity_type, entity_id, before, after, reason) of every audit row with the action."""
    statement = select(AuditLog).where(AuditLog.action == action).order_by(AuditLog.id)
    with factory() as session:
        return [
            (
                row.entity_type,
                row.entity_id,
                json.loads(row.before_state) if row.before_state else None,
                json.loads(row.after_state) if row.after_state else None,
                row.reason,
            )
            for row in session.execute(statement).scalars()
        ]


def refused(code: str, call: Callable[[], object]) -> None:
    """`call` raises the domain error with the design's error code."""
    with pytest.raises(AppError) as raised:
        call()
    assert raised.value.code == code


def insert_bnm_draft(factory: sessionmaker[Session], base: str = "USD") -> str:
    """A draft as the BNM fetch (AIH-TASK-040) will write it: no creator, a quote date."""
    quote_date = dt.date(2026, 9, 30)
    public_id = str(uuid.uuid4())
    with factory() as session:
        session.add(
            FxRateVersion(
                public_id=public_id,
                base_currency=base,
                quote_currency="MYR",
                rate=Decimal(FICTIONAL_RATE),
                source=FxRateSourceType.BNM,
                source_reference=(
                    f"bnm:exchange-rate:{base}:{quote_date}:session=1200:middle_rate:unit=1"
                ),
                source_quote_date=quote_date,
                observed_at=OBSERVED_AT,
                status=FxRateStatus.DRAFT,
                created_by=None,
                created_at=OBSERVED_AT,
                updated_at=OBSERVED_AT,
            )
        )
        session.commit()
    return public_id


# --- the "before" of the examples ---------------------------------------------------------


def after_e4(fx: Fx, base: str = "USD") -> tuple[str, str]:
    """E4: A `[NULL, 10:05:01)`, B `[10:05:01, NULL)` (B published at 10:05:00.2)."""
    fx.set(at(9))
    a = fx.publish_new(base)
    fx.set(at(10, 5, 0, 200_000))
    b = fx.publish_new(base)
    return a, b


def after_e5(fx: Fx, base: str = "USD") -> tuple[str, str]:
    """E5: A `[NULL, 次日 04:30)`, B `[次日 04:30, NULL)`."""
    fx.set(at(9))
    a = fx.publish_new(base)
    fx.set(at(10))
    b = fx.publish_new(base, NEXT_0430)
    return a, b


def after_e9_before(fx: Fx, base: str = "USD") -> tuple[str, str]:
    """E9's before (= E11 after): A `[NULL, 10:05:01)`, B `RETIRED [10:05:01, 12:00:01)`."""
    a, b = after_e4(fx, base)
    fx.set(at(12, 0, 0, 300_000))
    fx.retire(b)
    return a, b


# --- fixtures ---------------------------------------------------------------------


@pytest.fixture
def sqlite_factory() -> Iterator[sessionmaker[Session]]:
    yield from _sqlite_factory()


@pytest.fixture(params=["sqlite", "mysql"])
def factory(request, monkeypatch) -> Iterator[sessionmaker[Session]]:
    if request.param == "mysql":
        yield from _mysql_factory(monkeypatch)
        return
    yield from _sqlite_factory()


def _sqlite_factory() -> Iterator[sessionmaker[Session]]:
    # `create_all` also writes the one row of `fx_rate_locks` (app/models/fx_rates.py).
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    yield create_session_factory(engine)
    engine.dispose()


def _mysql_factory(monkeypatch) -> Iterator[sessionmaker[Session]]:
    if not TEST_DATABASE_URL:
        pytest.skip("BILLING_TEST_DATABASE_URL is not set; the MySQL half needs a real MySQL")
    # Tables through alembic: triggers, the generated column, unique indexes, checks and the
    # lock row are the ones migration 0015 builds.
    from app.core.config import get_settings

    monkeypatch.setenv("BILLING_DATABASE_URL", TEST_DATABASE_URL)
    get_settings.cache_clear()
    command.upgrade(Config("alembic.ini"), "head")
    get_settings.cache_clear()

    engine = create_engine(TEST_DATABASE_URL, pool_size=6, max_overflow=2)
    clean(engine)
    try:
        yield create_session_factory(engine)
    finally:
        clean(engine)
        engine.dispose()


def clean(engine: Engine) -> None:
    """⚠️ The database is shared: wipe before and after every case.

    The version triggers refuse DELETE and the fetch attempts are append-only, so TRUNCATE (DDL,
    no triggers) with foreign key checks off. `fx_rate_locks` is never touched: its one row
    belongs to the migration. The audit table is append-only (0010) and truncated the same way.
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


# --- publishing: E1–E10 (design §2「发布：区间怎么定」) -------------------------------------


def test_e1_p1_publish_without_a_time_on_an_empty_set(factory) -> None:
    fx = Fx(factory, Clock(at(9)))
    a = fx.draft()
    fx.set(at(10, 0, 0, 400_000))

    published = fx.publish(a)

    assert rows(factory) == {a: (PUBLISHED, None, None)}
    assert (published.status, published.effective_from, published.effective_to) == (
        PUBLISHED,
        None,
        None,
    )
    assert (published.approved_at, published.approved_by_email) == (at(10), fx.admin.email)
    assert fx.resolve(dt.datetime(2000, 1, 1)) == a


def test_e2_p1_publish_with_a_time_on_an_empty_set(factory) -> None:
    fx = Fx(factory, Clock(at(10)))
    a = fx.draft()

    fx.publish(a, NEXT_0430)

    assert rows(factory) == {a: (PUBLISHED, NEXT_0430, None)}
    assert fx.resolve(at(10)) is None
    assert fx.resolve(NEXT_0430 - SECOND) is None
    assert fx.resolve(NEXT_0430) == a


def test_e3_p1_only_an_empty_period_row_does_not_count(factory) -> None:
    fx = Fx(factory, Clock(at(9)))
    x = fx.publish_new("USD", NEXT_0430)
    fx.set(at(10))
    fx.retire(x)
    a = fx.draft()
    assert rows(factory) == {x: (RETIRED, NEXT_0430, NEXT_0430), a: (DRAFT, None, None)}

    fx.publish(a)

    assert rows(factory) == {x: (RETIRED, NEXT_0430, NEXT_0430), a: (PUBLISHED, None, None)}


def test_e4_p2_publish_without_a_time_truncates_the_started_tail(factory) -> None:
    fx = Fx(factory, Clock(at(9)))

    a, b = after_e4(fx)

    assert rows(factory) == {
        a: (PUBLISHED, None, at(10, 5, 1)),
        b: (PUBLISHED, at(10, 5, 1), None),
    }
    [*_, (entity_type, entity_id, before, after, reason)] = audit_states(
        factory, AuditAction.FX_RATE_PUBLISH
    )
    assert (entity_type, entity_id, reason) == ("fx_rate_version", b, None)
    assert before == {"status": DRAFT, "truncated_version": {"id": a, "effective_to": None}}
    assert after == {
        "status": PUBLISHED,
        "effective_from": "2026-10-01T10:05:01",
        "effective_to": None,
        "approved_by_email": fx.admin.email,
        "approved_at": "2026-10-01T10:05:00",
        "truncated_version": {"id": a, "effective_to": "2026-10-01T10:05:01"},
    }


def test_e5_p2_reservation_truncates_the_tail_at_f(factory) -> None:
    fx = Fx(factory, Clock(at(9)))

    a, b = after_e5(fx)

    assert rows(factory) == {a: (PUBLISHED, None, NEXT_0430), b: (PUBLISHED, NEXT_0430, None)}
    assert fx.resolve(NEXT_0430 - SECOND) == a
    assert fx.resolve(NEXT_0430) == b


def test_e6_p3_second_publish_in_the_same_second_conflicts(factory) -> None:
    fx = Fx(factory, Clock(at(9)))
    a, b = after_e4(fx)
    fx.set(at(10, 5, 0, 700_000))
    c = fx.draft()
    before = snapshot(factory)

    refused("EFFECTIVE_FROM_CONFLICT", lambda: fx.publish(c))

    assert snapshot(factory) == before
    assert rows(factory) == {
        a: (PUBLISHED, None, at(10, 5, 1)),
        b: (PUBLISHED, at(10, 5, 1), None),
        c: (DRAFT, None, None),
    }


def test_e7_p2_publish_again_in_the_next_second(factory) -> None:
    fx = Fx(factory, Clock(at(9)))
    a, b = after_e4(fx)
    fx.set(at(10, 5, 1, 100_000))

    c = fx.publish_new()

    assert rows(factory) == {
        a: (PUBLISHED, None, at(10, 5, 1)),
        b: (PUBLISHED, at(10, 5, 1), at(10, 5, 2)),
        c: (PUBLISHED, at(10, 5, 2), None),
    }


def test_e8_p3_a_reservation_exists(factory) -> None:
    fx = Fx(factory, Clock(at(9)))
    a, b = after_e5(fx)
    fx.set(at(11))
    c = fx.draft()
    before = snapshot(factory)

    # ① no time ② F = the reservation's start: 409, nothing written.
    refused("EFFECTIVE_FROM_CONFLICT", lambda: fx.publish(c))
    refused("EFFECTIVE_FROM_CONFLICT", lambda: fx.publish(c, NEXT_0430))
    assert snapshot(factory) == before

    # ③ F after the reservation's start.
    fx.publish(c, OCT3_0430)

    assert rows(factory) == {
        a: (PUBLISHED, None, NEXT_0430),
        b: (PUBLISHED, NEXT_0430, OCT3_0430),
        c: (PUBLISHED, OCT3_0430, None),
    }


def test_e9_p4_publish_after_a_retirement(factory) -> None:
    """① (USD) fills the gap from E; ② (EUR) leaves `[E, F)` without a rate."""
    fx = Fx(factory, Clock(at(9)))
    a, b = after_e9_before(fx, "USD")
    a2, b2 = after_e9_before(fx, "EUR")
    fx.set(at(15, 0, 0, 500_000))
    c = fx.draft("USD")
    c2 = fx.draft("EUR")
    assert fx.resolve(at(12, 0, 0)) == b
    assert fx.resolve(at(12, 0, 1)) is None
    assert fx.resolve(at(14, 59, 59)) is None

    fx.publish(c)
    fx.publish(c2, NEXT_0430)

    assert rows(factory, "USD") == {
        a: (PUBLISHED, None, at(10, 5, 1)),
        b: (RETIRED, at(10, 5, 1), at(12, 0, 1)),
        c: (PUBLISHED, at(12, 0, 1), None),
    }
    # The gap 12:00:01–15:00:01 had no rate before; now it has C. Earlier moments are unchanged.
    assert fx.resolve(at(10)) == a
    assert fx.resolve(at(12, 0, 0)) == b
    assert fx.resolve(at(12, 0, 1)) == c
    assert fx.resolve(at(14, 59, 59)) == c
    assert fx.resolve(at(15, 0, 1)) == c

    assert rows(factory, "EUR") == {
        a2: (PUBLISHED, None, at(10, 5, 1)),
        b2: (RETIRED, at(10, 5, 1), at(12, 0, 1)),
        c2: (PUBLISHED, NEXT_0430, None),
    }
    assert fx.resolve(at(12, 0, 1), "EUR") is None
    assert fx.resolve(NEXT_0430 - SECOND, "EUR") is None
    assert fx.resolve(NEXT_0430, "EUR") == c2


def test_e10_a_time_before_t_is_in_the_past(factory) -> None:
    fx = Fx(factory, Clock(at(9)))
    a = fx.publish_new()
    fx.set(at(11))
    b = fx.draft()
    before = snapshot(factory)

    refused("EFFECTIVE_FROM_IN_PAST", lambda: fx.publish(b, at(9)))

    assert snapshot(factory) == before
    assert rows(factory) == {a: (PUBLISHED, None, None), b: (DRAFT, None, None)}


# --- retiring: E11–E16 (design §2「退役」) -----------------------------------------------


def test_e11_r1_retire_the_started_tail(factory) -> None:
    fx = Fx(factory, Clock(at(9)))
    a, b = after_e4(fx)
    fx.set(at(12, 0, 0, 300_000))

    retired = fx.retire(b, "Fictional wrong rate")

    assert rows(factory) == {
        a: (PUBLISHED, None, at(10, 5, 1)),
        b: (RETIRED, at(10, 5, 1), at(12, 0, 1)),
    }
    assert (retired.status, retired.effective_to) == (RETIRED, at(12, 0, 1))
    assert fx.resolve(at(12, 0, 0)) == b
    assert fx.resolve(at(12, 0, 1)) is None
    [(entity_type, entity_id, before, after, reason)] = audit_states(
        factory, AuditAction.FX_RATE_RETIRE
    )
    assert (entity_type, entity_id, reason) == ("fx_rate_version", b, "Fictional wrong rate")
    assert before == {
        "status": PUBLISHED,
        "effective_from": "2026-10-01T10:05:01",
        "effective_to": None,
    }
    assert after == {
        "status": RETIRED,
        "effective_from": "2026-10-01T10:05:01",
        "effective_to": "2026-10-01T12:00:01",
        "reason": "Fictional wrong rate",
    }


def test_e12_r1_retire_in_the_second_it_was_published(factory) -> None:
    fx = Fx(factory, Clock(at(9)))
    a, b = after_e4(fx)
    fx.set(at(10, 5, 0, 600_000))

    fx.retire(b)

    assert rows(factory) == {
        a: (PUBLISHED, None, at(10, 5, 1)),
        b: (RETIRED, at(10, 5, 1), at(10, 5, 1)),
    }
    assert fx.resolve(at(10, 5, 1)) is None
    # L is now A (end 10:05:01): the next publish without a time is P4 from 10:05:01.
    fx.set(at(10, 6))
    c = fx.publish_new()
    assert rows(factory)[c] == (PUBLISHED, at(10, 5, 1), None)


def test_e13_r2_withdraw_a_reservation_that_truncated_p(factory) -> None:
    fx = Fx(factory, Clock(at(9)))
    a, b = after_e5(fx)
    fx.set(at(11))
    assert fx.resolve(NEXT_0430) == b

    fx.retire(b)

    assert rows(factory) == {a: (PUBLISHED, None, None), b: (RETIRED, NEXT_0430, NEXT_0430)}
    assert fx.resolve(NEXT_0430) == a
    [(_, _, before, after, _)] = audit_states(factory, AuditAction.FX_RATE_RETIRE)
    assert before == {
        "status": PUBLISHED,
        "effective_from": "2026-10-02T04:30:00",
        "effective_to": None,
    }
    assert after == {
        "status": RETIRED,
        "effective_from": "2026-10-02T04:30:00",
        "effective_to": "2026-10-02T04:30:00",
        "reason": "Fictional retirement",
        "restored_version": {"id": a},
    }

    # B is not on the timeline: L = A → P2.
    fx.set(at(11, 10, 0, 200_000))
    c = fx.publish_new()

    assert rows(factory) == {
        a: (PUBLISHED, None, at(11, 10, 1)),
        b: (RETIRED, NEXT_0430, NEXT_0430),
        c: (PUBLISHED, at(11, 10, 1), None),
    }
    assert fx.resolve(at(11, 10, 0)) == a
    assert fx.resolve(at(11, 10, 1)) == c
    assert fx.resolve(NEXT_0430) == c


def test_e14_r2_p_ended_by_a_retirement_is_not_restored(factory) -> None:
    fx = Fx(factory, Clock(at(9)))
    a, b = after_e9_before(fx)
    fx.set(at(15, 0, 0, 500_000))
    c = fx.publish_new("USD", NEXT_0430)
    fx.set(at(16))

    fx.retire(c)

    assert rows(factory) == {
        a: (PUBLISHED, None, at(10, 5, 1)),
        b: (RETIRED, at(10, 5, 1), at(12, 0, 1)),
        c: (RETIRED, NEXT_0430, NEXT_0430),
    }
    [*_, (_, _, _, after, _)] = audit_states(factory, AuditAction.FX_RATE_RETIRE)
    assert "restored_version" not in after
    # L = B (end 12:00:01): the next publish without a time is P4 from 12:00:01.
    fx.set(at(16, 30))
    d = fx.publish_new()
    assert rows(factory)[d] == (PUBLISHED, at(12, 0, 1), None)


def test_e15_r2_withdraw_with_no_version_before(factory) -> None:
    fx = Fx(factory, Clock(at(10)))
    a = fx.publish_new("USD", NEXT_0430)
    fx.set(at(11))

    fx.retire(a)

    assert rows(factory) == {a: (RETIRED, NEXT_0430, NEXT_0430)}
    [(_, _, _, after, _)] = audit_states(factory, AuditAction.FX_RATE_RETIRE)
    assert "restored_version" not in after
    # The timeline is empty: the next publish without a time is P1, "all along".
    fx.set(at(11, 30))
    b = fx.publish_new()
    assert rows(factory)[b] == (PUBLISHED, None, None)


def test_e16_r3_r4_r5_cannot_be_retired(factory) -> None:
    """R3 twice (USD: A after E4; EUR: A after E12), R4 (a GBP draft), R5 (JPY: B after E11)."""
    fx = Fx(factory, Clock(at(9)))
    truncated, _ = after_e4(fx, "USD")
    ended_by_empty, eur_b = after_e4(fx, "EUR")
    fx.set(at(10, 5, 0, 600_000))
    fx.retire(eur_b)
    draft = fx.draft("GBP")
    _, retired = after_e4(fx, "JPY")
    fx.set(at(12, 0, 0, 300_000))
    fx.retire(retired)
    fx.set(at(13))
    before = snapshot(factory)

    refused("FX_RATE_NOT_RETIRABLE", lambda: fx.retire(truncated))
    refused("FX_RATE_NOT_RETIRABLE", lambda: fx.retire(ended_by_empty))
    refused("FX_RATE_NOT_RETIRABLE", lambda: fx.retire(draft))
    refused("FX_RATE_FINAL", lambda: fx.retire(retired))

    assert snapshot(factory) == before


# --- the rest of the state table (design §2「状态」) ---------------------------------------


def test_publishing_a_published_version_writes_nothing_and_ignores_the_time(factory) -> None:
    fx = Fx(factory, Clock(at(9)))
    a = fx.publish_new()
    fx.set(at(11))
    before = snapshot(factory)

    again = fx.publish(a, at(9))

    assert snapshot(factory) == before
    assert (again.status, again.effective_from, again.effective_to) == (PUBLISHED, None, None)


def test_final_and_published_versions_refuse_draft_operations(factory) -> None:
    fx = Fx(factory, Clock(at(9)))
    published = fx.publish_new()
    discarded = fx.draft()
    fx.discard(discarded)
    reserved = fx.publish_new("EUR", NEXT_0430)
    fx.set(at(10))
    fx.retire(reserved)
    before = snapshot(factory)

    refused("FX_RATE_NOT_DRAFT", lambda: fx.edit(published, rate=OTHER_RATE))
    refused("FX_RATE_NOT_DRAFT", lambda: fx.discard(published))
    for final in (discarded, reserved):
        refused("FX_RATE_FINAL", lambda final=final: fx.edit(final, rate=OTHER_RATE))
        refused("FX_RATE_FINAL", lambda final=final: fx.discard(final))
        refused("FX_RATE_FINAL", lambda final=final: fx.publish(final))
    for operation in (fx.discard, fx.publish, fx.retire, lambda i: fx.edit(i, rate=OTHER_RATE)):
        refused("FX_RATE_NOT_FOUND", lambda operation=operation: operation(MISSING_ID))

    assert snapshot(factory) == before


def test_a_bnm_draft_cannot_be_edited_but_can_be_discarded(factory) -> None:
    fx = Fx(factory, Clock(at(9)))
    bnm = insert_bnm_draft(factory)
    before = snapshot(factory)

    refused("FX_RATE_NOT_EDITABLE", lambda: fx.edit(bnm, rate=OTHER_RATE))
    assert snapshot(factory) == before

    discarded = fx.discard(bnm)
    assert (discarded.status, discarded.source, discarded.created_by_email) == (
        DISCARDED,
        "BNM",
        None,
    )


# --- drafts and their audit (design §2「审计内容」) ---------------------------------------


def test_create_edit_and_discard_a_manual_draft(factory) -> None:
    fx = Fx(factory, Clock(at(9, 0, 0, 900_000)))

    created = fx.create("SGD", "1.1000000000")

    assert (created.status, created.source, created.base_currency) == (DRAFT, "MANUAL", "SGD")
    assert (created.quote_currency, created.rate, created.observed_at) == (
        "MYR",
        Decimal("1.1"),
        OBSERVED_AT,
    )
    assert (created.created_by_email, created.created_at) == (fx.admin.email, at(9))
    assert audit_states(factory, AuditAction.FX_RATE_CREATE) == [
        (
            "fx_rate_version",
            created.id,
            None,
            {
                "base_currency": "SGD",
                "rate": "1.1",
                "observed_at": "2026-09-30T04:00:00",
                "source_reference": REFERENCE,
                "status": DRAFT,
            },
            None,
        )
    ]

    # No real change (the same rate written differently): nothing written, no audit.
    fx.set(at(9, 30))
    before = snapshot(factory)
    fx.edit(created.id, rate="1.10", source_reference=REFERENCE)
    assert snapshot(factory) == before

    edited = fx.edit(created.id, rate=OTHER_RATE, source_reference=REFERENCE)
    assert (edited.rate, edited.updated_at, edited.source_reference) == (
        Decimal(OTHER_RATE),
        at(9, 30),
        REFERENCE,
    )
    assert audit_states(factory, AuditAction.FX_RATE_UPDATE) == [
        ("fx_rate_version", created.id, {"rate": "1.1"}, {"rate": OTHER_RATE}, None)
    ]

    discarded = fx.discard(created.id)
    assert discarded.status == DISCARDED
    assert audit_states(factory, AuditAction.FX_RATE_DISCARD) == [
        ("fx_rate_version", created.id, {"status": DRAFT}, {"status": DISCARDED}, None)
    ]


@pytest.mark.parametrize(
    "fields",
    [
        {"base_currency": "usd"},
        {"base_currency": "US"},
        {"base_currency": "MYR"},
        {"rate": Decimal("0")},
        {"rate": Decimal("-1.1111111111")},
        {"rate": Decimal("1.11111111111")},
        {"rate": Decimal("100000000000000")},
        {"rate": Decimal("NaN")},
        {"observed_at": OBSERVED_AT.replace(microsecond=1)},
        {"observed_at": OBSERVED_AT.replace(tzinfo=dt.UTC)},
        {"source_reference": "  "},
        {"source_reference": "x" * 256},
    ],
    ids=[
        "lower-case",
        "two-letters",
        "myr",
        "zero",
        "negative",
        "eleven-places",
        "fifteen-integer-digits",
        "nan",
        "fraction-of-a-second",
        "with-a-time-zone",
        "blank-reference",
        "long-reference",
    ],
)
def test_a_manual_draft_outside_the_format_is_refused(factory, fields: dict) -> None:
    """Design §2「金额精度」: at most 10 decimal places, never rounded; > 0; whole seconds."""
    fx = Fx(factory, Clock(at(9)))
    values: dict[str, object] = {
        "base_currency": "USD",
        "rate": Decimal(FICTIONAL_RATE),
        "observed_at": OBSERVED_AT,
        "source_reference": REFERENCE,
        **fields,
    }
    before = snapshot(factory)

    refused(
        "VALIDATION_ERROR",
        lambda: fx_rates.create_draft(
            factory, actor=fx.admin, context=CONTEXT, clock=fx.clock, **values
        ),
    )

    assert snapshot(factory) == before


def test_ten_decimal_places_are_kept_exactly(factory) -> None:
    fx = Fx(factory, Clock(at(9)))

    created = fx.create("JPY", "0.0259360001")

    assert created.rate == Decimal("0.0259360001")


def test_a_publish_time_outside_the_format_is_refused_whatever_the_status(factory) -> None:
    """Design §2「接口」v3: request validation comes before the state checks."""
    fx = Fx(factory, Clock(at(9)))
    published = fx.publish_new()
    draft = fx.draft()
    before = snapshot(factory)

    not_whole_second = NEXT_0430.replace(microsecond=5)
    for fx_rate_id in (published, draft):
        refused(
            "VALIDATION_ERROR",
            lambda fx_rate_id=fx_rate_id: fx.publish(fx_rate_id, not_whole_second),
        )

    assert snapshot(factory) == before


# --- the recheck and the lock (design §2「事务内复查」「锁在前」) -----------------------------


def _version(
    row_id: int, start: dt.datetime | None, end: dt.datetime | None, status: str
) -> FxRateVersion:
    return FxRateVersion(
        id=row_id, status=FxRateStatus(status), effective_from=start, effective_to=end
    )


T1 = at(10)
T2 = at(11)


@pytest.mark.parametrize(
    ("versions", "broken"),
    [
        ([], False),
        ([(None, None, PUBLISHED)], False),
        ([(None, T1, PUBLISHED), (T1, None, PUBLISHED)], False),
        ([(None, T1, PUBLISHED), (T1, T2, RETIRED), (T2, None, PUBLISHED)], False),
        ([(None, T1, PUBLISHED), (T1, T1, RETIRED)], False),
        ([(None, None, PUBLISHED), (NEXT_0430, NEXT_0430, RETIRED)], False),
        ([(None, None, PUBLISHED), (NEXT_0430, NEXT_0430, PUBLISHED)], True),
        ([(None, T2, PUBLISHED), (T1, None, PUBLISHED)], True),
        ([(T1, NEXT_0430, RETIRED), (T2, NEXT_0430, RETIRED)], True),
        ([(None, None, PUBLISHED), (T1, T2, RETIRED)], True),
        ([(None, None, PUBLISHED), (T1, None, PUBLISHED)], True),
        ([(None, T1, PUBLISHED), (None, None, PUBLISHED)], True),
    ],
    ids=[
        "none",
        "one-open",
        "chained",
        "retired-in-between",
        "e12-empty-at-the-end",
        "e13-withdrawn-reservation",
        "empty-but-not-retired",
        "overlap",
        "two-retired-intersect",
        "open-not-last",
        "two-open",
        "two-from-the-beginning",
    ],
)
def test_verify_periods(versions: list, broken: bool) -> None:
    """Design §2: ① empty only on RETIRED; ② only the first starts at NULL, an open one is last,
    each end ≤ the next start; ③ at most one open."""
    found = [_version(index + 1, *row) for index, row in enumerate(versions)]
    found.reverse()

    if broken:
        with pytest.raises(FxPeriodsBroken):
            verify_periods(found)
    else:
        verify_periods(found)


def _always_broken(_rows: object) -> None:
    raise FxPeriodsBroken("injected")


def test_a_failed_recheck_rolls_the_publish_back(sqlite_factory, monkeypatch) -> None:
    """The previous version is not truncated either: 500, not a user error."""
    fx = Fx(sqlite_factory, Clock(at(9)))
    fx.publish_new()
    draft = fx.draft()
    fx.set(at(10))
    before = snapshot(sqlite_factory)
    monkeypatch.setattr(fx_rates, "verify_periods", _always_broken)

    with pytest.raises(FxPeriodsBroken):
        fx.publish(draft)

    assert snapshot(sqlite_factory) == before


def test_a_failed_recheck_rolls_the_withdrawal_back(sqlite_factory, monkeypatch) -> None:
    fx = Fx(sqlite_factory, Clock(at(9)))
    _, reserved = after_e5(fx)
    fx.set(at(11))
    before = snapshot(sqlite_factory)
    monkeypatch.setattr(fx_rates, "verify_periods", _always_broken)

    with pytest.raises(FxPeriodsBroken):
        fx.retire(reserved)

    assert snapshot(sqlite_factory) == before


def test_publish_and_retire_take_the_fx_lock_first_and_read_the_clock_after_it(factory) -> None:
    """Design §2「锁在前」: the first statement of the transaction is the lock, then `t`."""
    events: list[str] = []

    def clock() -> dt.datetime:
        events.append("clock")
        return at(10)

    fx = Fx(factory, clock)
    draft = fx.draft()
    engine = factory.kw["bind"]
    mysql = engine.dialect.name == "mysql"

    def capture(_connection, _cursor, statement: str, *_args: object) -> None:
        if not _HOUSEKEEPING.match(statement):
            events.append(statement)

    event.listen(engine, "before_cursor_execute", capture)
    try:
        for operation in (lambda: fx.publish(draft), lambda: fx.retire(draft)):
            events.clear()
            operation()
            first, second = events[0], events[1]
            assert "fx_rate_locks" in first
            assert "FOR UPDATE" in first or not mysql
            assert second == "clock"
            assert events.count("clock") == 1
    finally:
        event.remove(engine, "before_cursor_execute", capture)


# --- a write failing half way: SQLite and MySQL once each (INV-13) ----------------------


def break_the_audit(monkeypatch) -> None:
    """The audit row fails at flush: `created_at` is NULL (NOT NULL → IntegrityError)."""
    real = fx_rates.record_audit

    def broken(session: Session, **options: object) -> None:
        real(session, **{**options, "now": None})  # type: ignore[arg-type]

    monkeypatch.setattr(fx_rates, "record_audit", broken)


class Scene:
    """USD: an open published version and a draft (publishing it truncates the open one).
    EUR: a published version and the reservation that truncated it (withdrawing restores it)."""

    def __init__(self, factory: sessionmaker[Session]) -> None:
        self.fx = Fx(factory, Clock(at(9)))
        self.current = self.fx.publish_new("USD")
        self.fx.publish_new("EUR")
        self.fx.set(at(10))
        self.reserved = self.fx.publish_new("EUR", NEXT_0430)
        self.draft = self.fx.draft("USD")
        self.fx.set(at(11))


ACTIONS: dict[str, Callable[[Scene], object]] = {
    "create": lambda scene: scene.fx.draft("USD"),
    "update": lambda scene: scene.fx.edit(scene.draft, rate=OTHER_RATE),
    "discard": lambda scene: scene.fx.discard(scene.draft),
    "publish-truncating": lambda scene: scene.fx.publish(scene.draft),
    "retire-current": lambda scene: scene.fx.retire(scene.current),
    "withdraw-restoring": lambda scene: scene.fx.retire(scene.reserved),
}


@pytest.mark.parametrize("action", sorted(ACTIONS))
def test_a_failed_audit_leaves_nothing(factory, monkeypatch, action: str) -> None:
    """Design §7「事务中途失败」: `publish-truncating` is the publish audit failing after the
    truncation; `withdraw-restoring` is the audit failing after P was restored. The version,
    the previous version and the audit all roll back."""
    scene = Scene(factory)
    before = snapshot(factory)
    break_the_audit(monkeypatch)

    with pytest.raises(Exception):  # noqa: B017 - the injected audit raises its own error
        ACTIONS[action](scene)

    assert snapshot(factory) == before

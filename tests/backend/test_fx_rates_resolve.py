"""`resolve_fx_rate` serialised with publishing, and time stability (design gate #183 v3 §7).

AIH-TASK-044 is design §11 F2b: tests only, for the rules of AIH-TASK-039. The cases map one to one
onto four rows of design §7:

- **时间稳定性（性质用例）** — `test_a_resolved_moment_never_changes_its_version`, on SQLite and
  on a real MySQL: random sequences of publish / reserve / retire / withdraw a reservation / fill
  the gap / same-second repeats, with a frozen clock. After every step, the checks of design §12
  「送审前的模型核对」: CHECK 1–5 hold on every row; the timeline does not intersect; at most one
  version is untruncated; the in-transaction recheck (`verify_periods`) passes; every moment not
  later than now that once resolved to V still resolves to V; and the only failures are the 409 /
  422 of design §2 — each exactly where the P1–P4 / R1–R5 tables put it, writing nothing. P4's
  `E ≤ t` is asserted on the way. The seed is fixed: a failure replays the same sequence;
- **发布与计费并发** — `test_billing_waits_for_an_uncommitted_publish` (publish first) and
  `test_a_publish_waits_for_an_uncommitted_billing_run` (billing first);
- **快照读陷阱（计费侧）** — `test_billing_reads_past_its_transaction_snapshot` (the publish
  commits before billing asks for the lock) and
  `test_a_publish_waits_for_a_billing_run_that_read_its_snapshot` (billing holds the lock first);
- **快照读陷阱（发布侧）** — `test_a_waiting_publish_computes_from_the_version_it_waited_for`,
  in both orders (`b-holds-the-lock`: P2 truncates the version just published, or P3's 409;
  `c-holds-the-lock`: the other connection first, then P2 / P2 with F against it).

The serialisation cases use two connections (plus a third that only looks at what is committed)
on a real MySQL; for each of the two orders of every row, a docstring states who blocks until
whose commit (or that nothing blocks), which version is resolved, and that resolving again after
the commit agrees.

MySQL needs `BILLING_TEST_DATABASE_URL` pointing at a database that may be wiped; without it those
cases skip — **a skip is not a pass**, CI sets it and treats a skip as a failure. As in every
other MySQL test module here, the fixture brings that throwaway test database to alembic head
before use; no other database is touched. Rates are
fictional; no uuid literal is needed here (placeholders would be all zeros).
"""

from __future__ import annotations

import datetime as dt
import itertools
import json
import os
import random
import threading
import time
from collections import Counter
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from decimal import Decimal
from functools import partial

import pytest
from alembic.config import Config
from sqlalchemy import Engine, create_engine, delete, event, func, select
from sqlalchemy.orm import Session, sessionmaker

from alembic import command
from app.core.database import create_session_factory
from app.core.errors import AppError
from app.models.auth import AuditAction, AuditLog, User, UserRole, UserStatus
from app.models.base import Base
from app.models.fx_rates import PERIOD_STATUSES, FxRateSourceType, FxRateStatus, FxRateVersion
from app.repositories import fx_rates as fx_repository
from app.repositories.fx_rates import ResolvedFxRate, resolve_fx_rate
from app.services import fx_rates
from app.services.auth import RequestContext, utc_now
from app.services.fx_rates import EffectiveFromConflict, FxRateVersionView, verify_periods

TEST_DATABASE_URL = os.environ.get("BILLING_TEST_DATABASE_URL", "")
TEST_EMAIL_DOMAIN = "@fx-rates-resolve-test.example.com"
CONTEXT = RequestContext(ip_address="203.0.113.9", user_agent="fx-rates-resolve-test")

# Obviously fictional rates (public repository): 1 unit of the base currency in MYR.
FICTIONAL_RATE = "1.1111111111"
REFERENCE = "fictional rate for a resolve test"
OBSERVED_AT = dt.datetime(2026, 9, 30, 4, 0, 0)

T0 = dt.datetime(2026, 10, 1, 9, 0, 0)
LONG_AGO = dt.datetime(2000, 1, 1)
MICRO = dt.timedelta(microseconds=1)
SECOND = dt.timedelta(seconds=1)
DAY = dt.timedelta(days=1)

PUBLISHED = "PUBLISHED"
RETIRED = "RETIRED"
DRAFT = "DRAFT"
DISCARDED = "DISCARDED"

_EMAIL_NUMBERS = itertools.count(1)


# --- helpers ----------------------------------------------------------------------


class Clock:
    """A frozen clock. Publish and retire call it once they hold the FX lock."""

    def __init__(self, moment: dt.datetime) -> None:
        self.now = moment

    def __call__(self) -> dt.datetime:
        return self.now

    def advance(self, microseconds: int) -> None:
        self.now += dt.timedelta(microseconds=microseconds)


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
    """One admin driving the FX services."""

    def __init__(self, factory: sessionmaker[Session], clock: Callable[[], dt.datetime]) -> None:
        self.factory = factory
        self.clock = clock
        self.admin = make_admin(factory)

    def draft(self, base: str = "USD") -> str:
        return fx_rates.create_draft(
            self.factory,
            actor=self.admin,
            base_currency=base,
            rate=Decimal(FICTIONAL_RATE),
            observed_at=OBSERVED_AT,
            source_reference=REFERENCE,
            context=CONTEXT,
            clock=self.clock,
        ).id

    def publish(
        self,
        fx_rate_id: str,
        effective_from: dt.datetime | None = None,
        *,
        factory: sessionmaker[Session] | None = None,
    ) -> FxRateVersionView:
        return fx_rates.publish(
            factory or self.factory,
            actor=self.admin,
            fx_rate_id=fx_rate_id,
            effective_from=effective_from,
            context=CONTEXT,
            clock=self.clock,
        )

    def publish_new(self, base: str = "USD", effective_from: dt.datetime | None = None) -> str:
        return self.publish(self.draft(base), effective_from).id

    def retire(self, fx_rate_id: str) -> FxRateVersionView:
        return fx_rates.retire(
            self.factory,
            actor=self.admin,
            fx_rate_id=fx_rate_id,
            reason="Fictional retirement",
            context=CONTEXT,
            clock=self.clock,
        )

    def resolve(self, moment: dt.datetime, base: str = "USD") -> str | None:
        """One billing transaction of its own (T-H): shared FX lock, resolve, commit."""
        with self.factory() as session:
            fx_repository.lock_fx_rates_shared(session)
            found = resolve_fx_rate(session, base, moment)
            session.commit()
        return public_id_of(found)


def public_id_of(found: ResolvedFxRate | None) -> str | None:
    return None if found is None else found.public_id


def rows(
    factory: sessionmaker[Session], base: str = "USD"
) -> dict[str, tuple[str, dt.datetime | None, dt.datetime | None]]:
    """Every version of the pair as committed (a plain read on a fresh connection)."""
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


def recheck(factory: sessionmaker[Session], base: str = "USD") -> None:
    """Design §2「事务内复查」on what is committed: raises `FxPeriodsBroken` if broken."""
    with factory() as session:
        verify_periods(fx_repository.period_versions(session, base))
        session.commit()


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
    # `create_all` also writes the one row of `fx_rate_locks` (app/models/fx_rates.py).
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    yield create_session_factory(engine)
    engine.dispose()


def _mysql_factory(monkeypatch) -> Iterator[sessionmaker[Session]]:
    if not TEST_DATABASE_URL:
        pytest.skip("BILLING_TEST_DATABASE_URL is not set; the MySQL half needs a real MySQL")
    # Tables through alembic: triggers, the generated column, unique indexes, checks and the
    # lock row are the ones migration 0015 builds (as in test_fx_rates_service.py).
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

    Same as test_fx_rates_service.py: the version triggers refuse DELETE and the fetch attempts
    and the audit are append-only, so TRUNCATE (DDL, no triggers) with foreign key checks off.
    `fx_rate_locks` is never touched: its one row belongs to the migration.
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


# --- time stability: the model (design §2 P1–P4, R1–R5) ---------------------------------

# public id → (status, effective_from, effective_to), for every version of one pair.
type Period = tuple[str, dt.datetime | None, dt.datetime | None]
type Model = dict[str, Period]

# Design §2: the only refusals a publish or a retire may give. Anything else (500) fails.
ALLOWED_REFUSALS = frozenset(
    {
        "EFFECTIVE_FROM_CONFLICT",
        "EFFECTIVE_FROM_IN_PAST",
        "FX_RATE_NOT_RETIRABLE",
        "FX_RATE_FINAL",
    }
)


@dataclass(frozen=True)
class Expected:
    """What design §2 says one operation does: which table row, its refusal, the rows after."""

    label: str
    code: str | None
    after: Model


def boundary(now: dt.datetime) -> dt.datetime:
    """`t`: `now` cut to the second plus one second, a whole second too (design §2 术语)."""
    return now.replace(microsecond=0) + SECOND


def _start_key(start: dt.datetime | None) -> tuple[bool, dt.datetime]:
    return (start is not None, start or dt.datetime.min)


def _is_empty(period: Period) -> bool:
    return period[1] is not None and period[1] == period[2]


def model_timeline(model: Model) -> list[tuple[str, Period]]:
    """The timeline: PUBLISHED / RETIRED with a non-empty period, earliest start first."""
    on = [
        (public_id, period)
        for public_id, period in model.items()
        if period[0] in (PUBLISHED, RETIRED) and not _is_empty(period)
    ]
    return sorted(on, key=lambda item: _start_key(item[1][1]))


def model_resolve(model: Model, moment: dt.datetime) -> str | None:
    for public_id, (_, start, end) in model_timeline(model):
        if (start is None or start <= moment) and (end is None or moment < end):
            return public_id
    return None


def expect_publish(
    model: Model, public_id: str, requested: dt.datetime | None, now: dt.datetime
) -> Expected:
    """Design §2「状态」and「发布：区间怎么定」P1–P4, read literally."""
    status = model[public_id][0]
    if status == PUBLISHED:
        return Expected("republish", None, model)
    if status in (RETIRED, DISCARDED):
        return Expected("R5-final", "FX_RATE_FINAL", model)
    t = boundary(now)
    if requested is not None and requested < t:
        return Expected("F-in-the-past", "EFFECTIVE_FROM_IN_PAST", model)
    after = dict(model)
    timeline = model_timeline(model)
    if not timeline:
        after[public_id] = (PUBLISHED, requested, None)
        return Expected("P1" if requested is None else "P1-F", None, after)
    tail_id, (tail_status, tail_start, tail_end) = timeline[-1]
    if tail_end is None:
        # 「L 未截断」的 L 必然是 PUBLISHED (CHECK 4).
        assert tail_status == PUBLISHED
        if tail_start is None or tail_start < t:
            start = t if requested is None else requested
            label = "P2" if requested is None else "P2-F"
        elif requested is None or requested <= tail_start:
            return Expected("P3-409", "EFFECTIVE_FROM_CONFLICT", model)
        else:
            start, label = requested, "P3-F"
        after[tail_id] = (tail_status, tail_start, start)
    else:
        # Design §2: 「P4 里 E ≤ `t` 恒成立」.
        assert tail_end <= t, ("P4 with E later than t", tail_end, t)
        start = tail_end if requested is None else requested
        label = "P4" if requested is None else "P4-F"
    after[public_id] = (PUBLISHED, start, None)
    return Expected(label, None, after)


def expect_retire(model: Model, public_id: str, now: dt.datetime) -> Expected:
    """Design §2「退役」R1–R5, read literally."""
    status, start, end = model[public_id]
    if status in (RETIRED, DISCARDED):
        return Expected("R5-final", "FX_RATE_FINAL", model)
    if status == DRAFT:
        return Expected("R4-draft", "FX_RATE_NOT_RETIRABLE", model)
    if end is not None:
        return Expected("R3-truncated", "FX_RATE_NOT_RETIRABLE", model)
    t = boundary(now)
    after = dict(model)
    if start is None or start <= t:
        after[public_id] = (RETIRED, start, t)
        return Expected("R1-empty" if start == t else "R1", None, after)
    after[public_id] = (RETIRED, start, start)
    key = _start_key(start)
    earlier = [item for item in model_timeline(model) if _start_key(item[1][1]) < key]
    if earlier:
        previous_id, (previous_status, previous_start, previous_end) = earlier[-1]
        if previous_status == PUBLISHED and previous_end == start:
            after[previous_id] = (PUBLISHED, previous_start, None)
            return Expected("R2-restore", None, after)
    return Expected("R2", None, after)


# Every table row the sequences must have hit, or the asserts idled.
REQUIRED_LABELS = frozenset(
    {
        "P1",
        "P1-F",
        "P2",
        "P2-F",
        "P3-F",
        "P3-409",
        "P4",
        "P4-F",
        "F-in-the-past",
        "republish",
        "R1",
        "R1-empty",
        "R2",
        "R2-restore",
        "R3-truncated",
        "R4-draft",
        "R5-final",
    }
)

# The labels of a draft that became PUBLISHED.
PUBLISHING_LABELS = frozenset({"P1", "P1-F", "P2", "P2-F", "P3-F", "P4", "P4-F"})

SEED = 20261001
STEPS = 50
# The clock moves by these (microseconds): often not at all or within the second (same-second
# repeats, `t` equal to a start just set), sometimes across seconds and further.
ADVANCES = (0, 0, 0, 1, 250_000, 999_999, 1_000_000, 1_500_000, 4_000_000, 20_000_000)
OPERATIONS = (
    *("publish",) * 4,
    *("reserve",) * 3,
    *("retire_latest",) * 3,
    *("retire_any",) * 2,
    "republish",
    "retire_draft",
)


def currency(index: int) -> str:
    """A fictional base currency per sequence (`QAA`, `QAB`, …): the pairs are independent."""
    return "Q" + chr(ord("A") + index // 26) + chr(ord("A") + index % 26)


def check_row(row: FxRateVersion) -> None:
    """Design §2 CHECK 1–5 on one row."""
    status = FxRateStatus(row.status)
    start, end = row.effective_from, row.effective_to
    approval = (row.approved_by, row.approved_at)
    if status in (FxRateStatus.DRAFT, FxRateStatus.DISCARDED):
        assert (start, end, *approval) == (None, None, None, None), row.public_id
    else:
        assert None not in approval, row.public_id
    ordered = start is None or end is None or start < end
    empty_retired = status is FxRateStatus.RETIRED and start == end
    assert ordered or empty_retired, row.public_id
    assert status is not FxRateStatus.RETIRED or end is not None, row.public_id
    bnm = FxRateSourceType(row.source) is FxRateSourceType.BNM
    assert not bnm or row.source_quote_date is not None, row.public_id


def period_of(row: FxRateVersion) -> Period:
    return (FxRateStatus(row.status).value, row.effective_from, row.effective_to)


def _intersect(first: Period, second: Period) -> bool:
    low, high = dt.datetime.min, dt.datetime.max
    return (first[1] or low) < (second[2] or high) and (second[1] or low) < (first[2] or high)


class Sequence:
    """One random sequence on one currency pair, with the model it is checked against."""

    def __init__(self, factory: sessionmaker[Session], base: str, rng: random.Random) -> None:
        self.factory = factory
        self.base = base
        self.rng = rng
        self.clock = Clock(T0 + dt.timedelta(milliseconds=400))
        self.fx = Fx(factory, self.clock)
        self.model: Model = {}
        self.published: list[str] = []
        self.labels: Counter[str] = Counter()
        self.seen: dict[dt.datetime, str] = {}
        # A coarse grid alternating whole and half seconds; every boundary and the moment
        # just before it, and every `now`, join as the sequence goes.
        self.probes: set[dt.datetime] = {LONG_AGO}
        self.probes.update(T0 + dt.timedelta(milliseconds=3500 * k) for k in range(-1, 60))

    # --- operations

    def _new_draft(self) -> str:
        public_id = self.fx.draft(self.base)
        self.model[public_id] = (DRAFT, None, None)
        return public_id

    def _a_draft(self) -> str:
        """A draft left behind by a refused publish now and then, else a new one."""
        left = [key for key, period in self.model.items() if period[0] == DRAFT]
        if left and self.rng.random() < 0.3:
            return self.rng.choice(left)
        return self._new_draft()

    def _a_version(self) -> str | None:
        on = [key for key, period in self.model.items() if period[0] in (PUBLISHED, RETIRED)]
        return self.rng.choice(on) if on else None

    def step(self, forced: str | None = None) -> None:
        self.clock.advance(self.rng.choice(ADVANCES))
        operation = forced or self.rng.choice(OPERATIONS)
        whole = self.clock.now.replace(microsecond=0)
        version = self._a_version()

        republish = operation == "republish" and version is not None
        if republish or operation in ("publish", "reserve", "reserve_ahead"):
            target = version if republish and version else self._a_draft()
            requested = None
            if operation == "reserve":
                # k ≤ 0 is before `t` (422); k = 1 is `t` itself.
                requested = whole + self.rng.randint(-2, 12) * SECOND
            elif operation == "reserve_ahead":
                requested = whole + self.rng.randint(1, 12) * SECOND
            elif republish:
                requested = whole + self.rng.randint(-3, 5) * SECOND
            expected = expect_publish(self.model, target, requested, self.clock.now)
            self._run(expected, lambda: self.fx.publish(target, requested), operation)
            if expected.label in PUBLISHING_LABELS:
                self.published.append(target)
            return

        if operation == "retire_latest" and self.published:
            # The newest: in effect → retire (R1); not started yet → withdraw (R2).
            target = self.published[-1]
        elif operation == "retire_any" and version is not None:
            target = version
        else:
            target = self._a_draft()
        expected = expect_retire(self.model, target, self.clock.now)
        self._run(expected, lambda: self.fx.retire(target), operation)

    def _run(self, expected: Expected, call: Callable[[], object], operation: str) -> None:
        """Run one operation; anything but the expected result or refusal fails (never 500)."""
        context = (self.base, operation, expected.label, self.clock.now)
        code: str | None = None
        try:
            call()
        except AppError as refused:
            code = refused.code
        assert code == expected.code, context
        assert code is None or code in ALLOWED_REFUSALS, context
        self.model = expected.after
        self.labels[expected.label] += 1
        self.check(context)

    # --- the checks after every step (design §12)

    def committed(self) -> Model:
        return dict(rows(self.factory, self.base))

    def check(self, context: tuple) -> None:
        now = self.clock.now
        statement = select(FxRateVersion).where(FxRateVersion.base_currency == self.base)
        with self.factory() as session:
            # A billing transaction's shape (T-H): the shared FX lock, then locking reads.
            fx_repository.lock_fx_rates_shared(session)
            versions = list(session.execute(statement).scalars())

            # Refusals wrote nothing; successes wrote exactly what the tables say.
            actual = {row.public_id: period_of(row) for row in versions}
            assert actual == self.model, context
            for row in versions:
                check_row(row)
            periods = [row for row in versions if FxRateStatus(row.status) in PERIOD_STATUSES]
            verify_periods(periods)
            timeline = model_timeline(self.model)
            for (first_id, first), (second_id, second) in itertools.combinations(timeline, 2):
                assert not _intersect(first, second), (context, first_id, second_id)
            untruncated = [row for row in periods if row.effective_to is None]
            assert len(untruncated) <= 1, context

            for row in periods:
                for edge in (row.effective_from, row.effective_to):
                    if edge is not None:
                        self.probes.update((edge, edge - MICRO))
            self.probes.add(now)
            # Billing only resolves moments not later than now (T-H contract).
            for probe in sorted(self.probes):
                if probe > now:
                    break
                found = public_id_of(resolve_fx_rate(session, self.base, probe))
                assert found == model_resolve(self.model, probe), (context, probe)
                if probe in self.seen:
                    assert found == self.seen[probe], (context, probe)
                elif found is not None:
                    self.seen[probe] = found
            session.commit()


def test_a_resolved_moment_never_changes_its_version(factory) -> None:
    """Design §7「时间稳定性（性质用例）」, asserted as design §12「送审前的模型核对」.

    Fixed seed. Each sequence has its own currency pair and frozen clock; even sequences open
    with a publish without a time (P1), odd ones with a reservation (P1 with F). At least
    `least` sequences run, then more only until every row of the P1–P4 / R1–R5 tables has been
    hit, at most `most`: one run stays well inside a minute on CI.
    """
    mysql = factory.kw["bind"].dialect.name == "mysql"
    least, most = (2, 6) if mysql else (6, 14)
    rng = random.Random(SEED)
    labels: Counter[str] = Counter()
    sequences: list[Sequence] = []

    for index in range(most):
        if index >= least and REQUIRED_LABELS.issubset(labels):
            break
        sequence = Sequence(factory, currency(index), random.Random(rng.getrandbits(64)))
        sequence.step("publish" if index % 2 == 0 else "reserve_ahead")
        for _ in range(STEPS - 1):
            sequence.step()
        labels.update(sequence.labels)
        sequences.append(sequence)

    assert REQUIRED_LABELS.issubset(labels), sorted(REQUIRED_LABELS.difference(labels))
    # The pairs are independent: what one sequence did never touched another's rows.
    for sequence in sequences:
        assert sequence.committed() == sequence.model, sequence.base
    # Moments really resolved, to many versions — otherwise the asserts idled.
    assert sum(len(sequence.seen) for sequence in sequences) >= 100
    resolved_to = {version for sequence in sequences for version in sequence.seen.values()}
    assert len(resolved_to) >= 2 * len(sequences)


# --- serialised with billing (real MySQL, two connections) ----------------------------


class PauseFirstAudit:
    """Stop the next audit write just before its commit — the FX lock and row locks held.

    Only the first call pauses; later writes run through. Installed after the setup's drafts
    and publishes, so the first call is the publish under test.
    """

    def __init__(self, monkeypatch) -> None:
        self.written = threading.Event()
        self.release = threading.Event()
        self._taken = threading.Lock()
        self._paused = False
        real = fx_rates.record_audit

        def pausing(session: Session, **options: object) -> None:
            real(session, **options)  # type: ignore[arg-type]
            with self._taken:
                first, self._paused = not self._paused, True
            if first:
                session.flush()
                self.written.set()
                assert self.release.wait(timeout=60)

        monkeypatch.setattr(fx_rates, "record_audit", pausing)


@dataclass(frozen=True)
class BillingRun:
    """One billing transaction: `now` taken after the shared lock, and what it resolved."""

    now: dt.datetime
    earlier: str | None
    current: str | None


def bill(factory: sessionmaker[Session], earlier: dt.datetime, base: str = "USD") -> BillingRun:
    """Billing per the T-H contract: shared FX lock first, then only moments ≤ now, then commit."""
    with factory() as session:
        fx_repository.lock_fx_rates_shared(session)
        now = utc_now()
        assert earlier <= now
        run = BillingRun(
            now=now,
            earlier=public_id_of(resolve_fx_rate(session, base, earlier)),
            current=public_id_of(resolve_fx_rate(session, base, now)),
        )
        session.commit()
    return run


def wait_until(moment: dt.datetime) -> None:
    """Wall clock past `moment` (a `t` is at most a second ahead)."""
    deadline = time.monotonic() + 5
    while utc_now() < moment:
        assert time.monotonic() < deadline
        time.sleep(0.05)


def test_billing_waits_for_an_uncommitted_publish(mysql_factory, monkeypatch) -> None:
    """Design §7「发布与计费并发」, publish first.

    A `[NULL, NULL)` is in effect. Publishing B has written (A truncated at `t`, B `[t, NULL)`)
    but not committed. Expected:
    - **blocking**: the billing run blocks on the shared FX lock until the publish commits; in the
      meantime nothing of B is visible to anyone;
    - **version**: after the commit the billing run's `now` is past `t` (it waited ≥ 2 s), so
      `now` resolves to B, and a moment before the publish still resolves to A;
    - **again after the commit**: both moments resolve to the same versions.
    """
    fx = Fx(mysql_factory, utc_now)
    a = fx.publish_new()
    b = fx.draft()
    earlier = utc_now()
    pause = PauseFirstAudit(monkeypatch)

    with ThreadPoolExecutor(max_workers=2) as pool:
        try:
            publishing = pool.submit(fx.publish, b)
            assert pause.written.wait(timeout=30)
            billing = pool.submit(bill, mysql_factory, earlier)
            with pytest.raises(TimeoutError):
                billing.result(timeout=2)
            assert rows(mysql_factory) == {a: (PUBLISHED, None, None), b: (DRAFT, None, None)}
        finally:
            pause.release.set()
        new = publishing.result(timeout=30)
        run = billing.result(timeout=30)

    t = new.effective_from
    assert t is not None
    assert earlier < t <= run.now
    assert (run.earlier, run.current) == (a, b)
    assert rows(mysql_factory) == {a: (PUBLISHED, None, t), b: (PUBLISHED, t, None)}
    assert (fx.resolve(earlier), fx.resolve(run.now)) == (a, b)


def test_a_publish_waits_for_an_uncommitted_billing_run(mysql_factory) -> None:
    """Design §7「发布与计费并发」, billing first.

    A `[NULL, NULL)` is in effect. A billing transaction takes the shared FX lock and resolves
    its `now` to A, and does not commit yet. Expected:
    - **blocking**: publishing B blocks on the FX lock until the billing run commits; nothing is
      written meanwhile, and the billing run resolving again inside its transaction still gets A;
    - **version**: B's `t` is later than the billing commit (it is taken after the lock), so the
      billed moment stays A's;
    - **again after the commit**: the billed moment still resolves to A, `t` to B.
    """
    fx = Fx(mysql_factory, utc_now)
    a = fx.publish_new()
    b = fx.draft()
    billing = mysql_factory()
    try:
        fx_repository.lock_fx_rates_shared(billing)
        occurred_at = utc_now()
        assert public_id_of(resolve_fx_rate(billing, "USD", occurred_at)) == a
        with ThreadPoolExecutor(max_workers=1) as pool:
            publishing = pool.submit(fx.publish, b)
            with pytest.raises(TimeoutError):
                publishing.result(timeout=2)
            assert rows(mysql_factory) == {a: (PUBLISHED, None, None), b: (DRAFT, None, None)}
            assert public_id_of(resolve_fx_rate(billing, "USD", occurred_at)) == a
            before_commit = utc_now()
            billing.commit()
            new = publishing.result(timeout=30)
    finally:
        billing.close()

    t = new.effective_from
    assert t is not None
    assert occurred_at <= before_commit < t
    assert rows(mysql_factory) == {a: (PUBLISHED, None, t), b: (PUBLISHED, t, None)}
    assert (fx.resolve(occurred_at), fx.resolve(t)) == (a, b)


def test_billing_reads_past_its_transaction_snapshot(mysql_factory) -> None:
    """Design §7「快照读陷阱（计费侧）」.

    One order: the publish commits before billing asks for the lock (the other order is
    `test_a_publish_waits_for_a_billing_run_that_read_its_snapshot`).

    The billing transaction first does plain reads — REPEATABLE READ fixes its snapshot there,
    with A `[NULL, NULL)` the only published version. Another connection publishes B and
    commits (A truncated at `t`). The billing transaction then takes the shared FX lock (it does
    not block: the publish has committed) and resolves. Expected:
    - **blocking**: none — the publish has committed before the shared lock is asked for;
    - **the trap is real**: plain reads in the same transaction still show the snapshot (one
      published version, A untruncated);
    - **version**: resolving its `now` (past `t`) gives B, and the moment before the publish
      gives A with the end `t` — the latest commit, not the snapshot;
    - **again after the commit**: the same two moments resolve the same.
    """
    fx = Fx(mysql_factory, utc_now)
    a = fx.publish_new()
    b = fx.draft()
    is_published = FxRateVersion.status == FxRateStatus.PUBLISHED
    counted = select(func.count()).select_from(FxRateVersion).where(is_published)
    a_end = select(FxRateVersion.effective_to).where(FxRateVersion.public_id == a)
    billing = mysql_factory()
    try:
        earlier = utc_now()
        assert billing.execute(counted).scalar_one() == 1
        assert billing.execute(a_end).scalar_one() is None

        t = fx.publish(b).effective_from
        assert t is not None
        wait_until(t)

        fx_repository.lock_fx_rates_shared(billing)
        now = utc_now()
        assert billing.execute(counted).scalar_one() == 1
        assert billing.execute(a_end).scalar_one() is None
        current = resolve_fx_rate(billing, "USD", now)
        before = resolve_fx_rate(billing, "USD", earlier)
        billing.commit()
    finally:
        billing.close()

    assert current is not None and before is not None
    assert (current.public_id, current.effective_from, current.effective_to) == (b, t, None)
    assert (before.public_id, before.effective_from, before.effective_to) == (a, None, t)
    assert (fx.resolve(earlier), fx.resolve(now)) == (a, b)


def test_a_publish_waits_for_a_billing_run_that_read_its_snapshot(mysql_factory) -> None:
    """Design §7「快照读陷阱（计费侧）」, the other order: billing takes the lock first.

    The billing transaction does plain reads first (snapshot: A `[NULL, NULL)` the only
    published version), then takes the shared FX lock and resolves; only then does another
    connection publish B. Expected:
    - **blocking**: the publish blocks on the FX lock until the billing run commits; nothing of B
      is committed meanwhile;
    - **version**: the billing run resolves its `now` to A `[NULL, NULL)` — the latest commit,
      which here is also its snapshot: plain and locking reads agree, before and while the
      publish waits; B's `t` is later than the billing commit (taken after the lock), so the
      billed moment stays A's;
    - **again after the commit**: the billed moment still resolves to A (now ending at `t`), and
      `t` to B.
    """
    fx = Fx(mysql_factory, utc_now)
    a = fx.publish_new()
    b = fx.draft()
    is_published = FxRateVersion.status == FxRateStatus.PUBLISHED
    counted = select(func.count()).select_from(FxRateVersion).where(is_published)
    a_end = select(FxRateVersion.effective_to).where(FxRateVersion.public_id == a)
    billing = mysql_factory()
    try:
        earlier = utc_now()
        assert billing.execute(counted).scalar_one() == 1
        assert billing.execute(a_end).scalar_one() is None

        fx_repository.lock_fx_rates_shared(billing)
        now = utc_now()
        current = resolve_fx_rate(billing, "USD", now)
        assert current is not None
        assert (current.public_id, current.effective_from, current.effective_to) == (a, None, None)
        assert public_id_of(resolve_fx_rate(billing, "USD", earlier)) == a

        with ThreadPoolExecutor(max_workers=1) as pool:
            publishing = pool.submit(fx.publish, b)
            with pytest.raises(TimeoutError):
                publishing.result(timeout=2)
            assert rows(mysql_factory) == {a: (PUBLISHED, None, None), b: (DRAFT, None, None)}
            assert billing.execute(counted).scalar_one() == 1
            assert billing.execute(a_end).scalar_one() is None
            assert public_id_of(resolve_fx_rate(billing, "USD", now)) == a
            before_commit = utc_now()
            billing.commit()
            new = publishing.result(timeout=30)
    finally:
        billing.close()

    t = new.effective_from
    assert t is not None
    assert earlier <= now <= before_commit < t
    assert rows(mysql_factory) == {a: (PUBLISHED, None, t), b: (PUBLISHED, t, None)}
    assert (fx.resolve(earlier), fx.resolve(now), fx.resolve(t)) == (a, a, b)


def snapshot_first(factory: sessionmaker[Session]) -> sessionmaker[Session]:
    """Sessions that fix their REPEATABLE READ snapshot with a plain read before anything else.

    The service's first statement is the FX lock (design §2「锁在前」rule 1); this takes the
    snapshot before it anyway, so the case also shows that every read after the lock is a locking
    read (rule 2) and never the stale snapshot.
    """
    early = create_session_factory(factory.kw["bind"])

    def plain_read(_session: Session, _transaction: object, connection) -> None:
        connection.exec_driver_sql("SELECT COUNT(*) FROM fx_rate_versions").scalar_one()

    event.listen(early, "after_begin", plain_read)
    return early


def publish_audit(factory: sessionmaker[Session], public_id: str) -> tuple[dict, dict]:
    statement = select(AuditLog).where(
        AuditLog.action == AuditAction.FX_RATE_PUBLISH, AuditLog.entity_id == public_id
    )
    with factory() as session:
        row = session.execute(statement).scalar_one()
        return json.loads(row.before_state), json.loads(row.after_state)


@pytest.mark.parametrize("snapshot_before_the_lock", [False, True], ids=["as-is", "snapshot"])
@pytest.mark.parametrize("b_reserves", [False, True], ids=["b-now", "b-reserves"])
@pytest.mark.parametrize("b_first", [True, False], ids=["b-holds-the-lock", "c-holds-the-lock"])
def test_a_waiting_publish_computes_from_the_version_it_waited_for(
    mysql_factory, monkeypatch, b_first: bool, b_reserves: bool, snapshot_before_the_lock: bool
) -> None:
    """Design §7「快照读陷阱（发布侧）」, both orders.

    A `[NULL, NULL)` is in effect. Two connections publish: B — without a time (`b-now`), or as
    a reservation a day ahead (`b-reserves`) — and C without a time. The first holds the FX lock
    with its writes uncommitted while the second asks for it; in `snapshot` the second's
    transaction has already fixed a snapshot in which the first's version is a draft and A is
    untruncated.

    `b-holds-the-lock` (B first, C waits). Expected:
    - **blocking**: C's publish blocks on the FX lock until B's commits; nothing is visible
      meanwhile;
    - **version**: C is placed against the new L = B, never against A — `b-now`: P2 truncates B
      at C's `t` (≥ 2 s after B's), C `[t, NULL)`, and C's audit names B as the truncated
      version; `b-reserves`: L = B has not started, so 409 `EFFECTIVE_FROM_CONFLICT` and C stays
      a draft.

    `c-holds-the-lock` (C first, B waits). Expected:
    - **blocking**: B's publish blocks on the FX lock until C's commits; nothing is visible
      meanwhile;
    - **version**: B is placed against the new L = C (started, untruncated), never against A —
      `b-now`: P2 truncates C at B's `t` (≥ 2 s after C's); `b-reserves`: P2 with F truncates C
      at the reservation; B `[its start, NULL)` either way, and B's audit names C as the
      truncated version.

    Either order, **again after the commit**: the committed periods pass design §2「事务内复查」,
    and resolving agrees with the rows — a moment before both publishes is still A's.
    """
    fx = Fx(mysql_factory, utc_now)
    a = fx.publish_new()
    b = fx.draft()
    c = fx.draft()
    reservation = (utc_now() + DAY).replace(microsecond=0) if b_reserves else None
    waiting = snapshot_first(mysql_factory) if snapshot_before_the_lock else mysql_factory
    earlier = utc_now()
    pause = PauseFirstAudit(monkeypatch)
    if b_first:
        first_id, second_id = b, c
        first_call = partial(fx.publish, b, reservation)
        second_call = partial(fx.publish, c, None, factory=waiting)
    else:
        first_id, second_id = c, b
        first_call = partial(fx.publish, c)
        second_call = partial(fx.publish, b, reservation, factory=waiting)
    refused = b_first and b_reserves

    with ThreadPoolExecutor(max_workers=2) as pool:
        try:
            first = pool.submit(first_call)
            assert pause.written.wait(timeout=30)
            second = pool.submit(second_call)
            with pytest.raises(TimeoutError):
                second.result(timeout=2)
            assert rows(mysql_factory) == {
                a: (PUBLISHED, None, None),
                b: (DRAFT, None, None),
                c: (DRAFT, None, None),
            }
        finally:
            pause.release.set()
        published_first = first.result(timeout=30)
        if refused:
            with pytest.raises(EffectiveFromConflict):
                second.result(timeout=30)
        else:
            published_second = second.result(timeout=30)

    first_start = published_first.effective_from
    assert first_start is not None
    if refused:
        assert first_start == reservation
        assert rows(mysql_factory) == {
            a: (PUBLISHED, None, first_start),
            b: (PUBLISHED, first_start, None),
            c: (DRAFT, None, None),
        }
    else:
        second_start = published_second.effective_from
        assert second_start is not None
        assert first_start + SECOND < second_start
        if b_reserves:
            assert second_start == reservation
        assert rows(mysql_factory) == {
            a: (PUBLISHED, None, first_start),
            first_id: (PUBLISHED, first_start, second_start),
            second_id: (PUBLISHED, second_start, None),
        }
        before, after = publish_audit(mysql_factory, second_id)
        assert before["truncated_version"] == {"id": first_id, "effective_to": None}
        assert after["truncated_version"] == {
            "id": first_id,
            "effective_to": second_start.isoformat(),
        }
        assert (fx.resolve(first_start), fx.resolve(second_start - MICRO)) == (first_id, first_id)
        if not b_reserves:
            assert fx.resolve(second_start) == second_id
    recheck(mysql_factory)
    assert fx.resolve(earlier) == a

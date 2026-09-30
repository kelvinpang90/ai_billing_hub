"""The FX rate tables on a real MySQL: checks, unique constraints and triggers (design gate
#183 v3, AIH-TASK-028 = F1 of design §11).

Five rows of design §7, each refusal paired with a case that proves the database does not
refuse more than it should:

- **triggers**: the one-row lock table; versions enter only as drafts; a draft never goes
  straight to `RETIRED`; a published version only truncates and retires; retired and discarded
  versions are final; versions are never deleted; fetch attempts are append-only;
- **open_slot and the unique constraints**: the §7 sequence (v3 order) and one BNM version per
  quote date;
- **database fallbacks**: publishing "from all time" after a timeline exists, publishing with
  an end, retiring without an end, and publishing "from all time" on an empty timeline (passes);
  the service's write orders pass;
- **no overlapping periods**: intersecting, end-to-end, empty periods, an empty period inside
  another version's period, other currencies, and the trigger taking `fx_rate_locks`;
- **migration**: upgrade / downgrade add and drop the three tables; the lock table holds
  exactly one row.

There is no service layer yet (F2 is AIH-TASK-039), so the timelines are built with plain SQL
in the service's write order (design §2: truncate the previous version before publishing;
withdraw a reservation before restoring the previous version), following the examples of
design §2 (all on 2026-10-01, UTC). Each case runs in one transaction that is rolled back.
Rates are obviously fictional; the placeholder uuids are zeros with a counter at the end.

Needs `BILLING_TEST_DATABASE_URL` pointing at a database that may be wiped; skips without it —
**a skip is not a pass**, CI sets it.
"""

from __future__ import annotations

import datetime as dt
import itertools
import os
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from alembic.config import Config
from sqlalchemy import Engine, create_engine, insert, inspect, text
from sqlalchemy.engine import Connection, Row
from sqlalchemy.exc import DBAPIError

from alembic import command
from app.models.auth import User, UserRole

TEST_DATABASE_URL = os.environ.get("BILLING_TEST_DATABASE_URL", "")

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="BILLING_TEST_DATABASE_URL is not set; the FX rate triggers need a real MySQL",
)

TEST_EMAIL = "fx-rates-db@fx-rates-test.example.com"
# The second connection of the lock case: `users.email` is unique, and an uncommitted insert of
# the same address in the first connection would make the second one wait.
OTHER_TEST_EMAIL = "fx-rates-db-other@fx-rates-test.example.com"

# Obviously fictional rates (public repository): MYR per one unit of the base currency.
FICTIONAL_RATE = "1.1111111111"
OTHER_RATE = "9.9999999999"


def at(hour: int, minute: int = 0, second: int = 0) -> dt.datetime:
    """A moment on 2026-10-01 UTC, the day of the design §2 examples."""
    return dt.datetime(2026, 10, 1, hour, minute, second)


WRITTEN_AT = at(9)
NEXT_DAY_0430 = dt.datetime(2026, 10, 2, 4, 30)
OCT_3_0430 = dt.datetime(2026, 10, 3, 4, 30)
QUOTE_DATE = dt.date(2026, 9, 30)
SECOND = dt.timedelta(seconds=1)
DAY = dt.timedelta(days=1)

# MySQL error numbers and the trigger messages (migration 0015).
_ER_LOCK_WAIT_TIMEOUT = 1205
_ER_DUP_ENTRY = 1062
_ER_SIGNAL_EXCEPTION = 1644
_ER_CHECK_CONSTRAINT_VIOLATED = 3819
_ONE_LOCK_ROW = "fx_rate_locks holds exactly one row"
_STARTS_AS_DRAFT = "an fx rate version starts as a draft"
_FINAL = "a retired or discarded fx rate version is final"
_DRAFT_TRANSITION = "a draft fx rate version is published or discarded"
_PUBLISHED_WITH_END = "an fx rate version is published without an end"
_IMMUTABLE = "a published fx rate version is immutable"
_RETIRE_ONLY = "a published fx rate version can only be retired"
_OVERLAP = "fx rate periods overlap"
_NEVER_DELETED = "fx rate versions are never deleted"
_APPEND_ONLY = "fx fetch attempts are append-only"

_TABLES = {"fx_rate_locks", "fx_rate_versions", "fx_fetch_attempts"}
_TRIGGERS = {
    "trg_fx_rate_locks_before_insert",
    "trg_fx_rate_locks_before_delete",
    "trg_fx_rate_versions_before_insert",
    "trg_fx_rate_versions_before_update",
    "trg_fx_rate_versions_before_delete",
    "trg_fx_fetch_attempts_before_update",
    "trg_fx_fetch_attempts_before_delete",
}
# Migration 0016 (AIH-TASK-045): the lock tables' BEFORE UPDATE. Downgrading to 0014 drops them
# too — the fx one with its table, the pricing one by 0016's downgrade.
_TRIGGERS_0016 = {"trg_pricing_rule_locks_before_update", "trg_fx_rate_locks_before_update"}

_INSERT_VERSION = text(
    "INSERT INTO fx_rate_versions (public_id, base_currency, quote_currency, rate, source,"
    " source_reference, source_quote_date, observed_at, status, effective_from, effective_to,"
    " created_by, approved_by, approved_at, created_at, updated_at) VALUES (:public_id, :base,"
    " :quote, :rate, :source, :reference, :quote_date, :observed_at, :status, :start, :end,"
    " :creator, :approver, :approved_at, :now, :now)"
)
_PUBLISH = text(
    "UPDATE fx_rate_versions SET status = 'PUBLISHED', effective_from = :start,"
    " effective_to = :end, approved_by = :approver, approved_at = :now, updated_at = :now"
    " WHERE id = :id"
)
_SET_END = text("UPDATE fx_rate_versions SET effective_to = :end, updated_at = :now WHERE id = :id")
_RETIRE = text(
    "UPDATE fx_rate_versions SET status = 'RETIRED', effective_to = :end, updated_at = :now"
    " WHERE id = :id"
)
# Design §2 R2: a withdrawn reservation becomes an empty RETIRED period `[F, F)`.
_WITHDRAW = text(
    "UPDATE fx_rate_versions SET status = 'RETIRED', effective_to = effective_from,"
    " updated_at = :now WHERE id = :id"
)
_DISCARD = text(
    "UPDATE fx_rate_versions SET status = 'DISCARDED', updated_at = :now WHERE id = :id"
)
_ROW = text(
    "SELECT status, effective_from, effective_to, open_slot FROM fx_rate_versions WHERE id = :id"
)
_ALL_ROWS = text(
    "SELECT id, status, rate, effective_from, effective_to, open_slot FROM fx_rate_versions"
    " ORDER BY id"
)
_DELETE_VERSION = text("DELETE FROM fx_rate_versions WHERE id = :id")
_INSERT_ATTEMPT = text(
    "INSERT INTO fx_fetch_attempts (base_currency, source, requested_date, outcome, quote_date,"
    " error_code, fx_rate_version_id, attempted_at) VALUES (:base, :source, :requested_date,"
    " :outcome, :quote_date, :error_code, :version_id, :attempted_at)"
)
_LOCK_IDS = text("SELECT id FROM fx_rate_locks")
_TRIGGER_NAMES = text(
    "SELECT TRIGGER_NAME FROM information_schema.TRIGGERS WHERE TRIGGER_SCHEMA = DATABASE()"
)


def _update(assignment: str):
    return text(f"UPDATE fx_rate_versions SET {assignment} WHERE id = :id")


def _error(raised: pytest.ExceptionInfo[DBAPIError]) -> tuple[int, str]:
    errno, message = raised.value.orig.args[:2]
    return int(errno), str(message)


class Db:
    """One connection inside a transaction the fixture rolls back; rows are written with plain
    SQL, the way a direct write that bypasses the service would."""

    def __init__(self, connection: Connection, email: str = TEST_EMAIL) -> None:
        self.connection = connection
        self._numbers = itertools.count(1)
        created = connection.execute(
            insert(User).values(
                email=email,
                password_hash="not-a-real-hash",
                role=UserRole.ADMIN,
                created_at=WRITTEN_AT,
                updated_at=WRITTEN_AT,
            )
        )
        self.admin = int(created.inserted_primary_key[0])

    # --- writing ---------------------------------------------------------------------

    def execute(self, statement, **params: object) -> Any:
        return self.connection.execute(statement, params)

    def version_params(self, **overrides: object) -> dict[str, object]:
        """A manual USD draft, with `overrides` on top."""
        params: dict[str, object] = {
            "public_id": f"00000000-0000-0000-0000-{next(self._numbers):012d}",
            "base": "USD",
            "quote": "MYR",
            "rate": FICTIONAL_RATE,
            "source": "MANUAL",
            "reference": "fictional rate for a database test",
            "quote_date": None,
            "observed_at": WRITTEN_AT,
            "status": "DRAFT",
            "start": None,
            "end": None,
            "creator": self.admin,
            "approver": None,
            "approved_at": None,
            "now": WRITTEN_AT,
        }
        params.update(overrides)
        return params

    def draft(self, **overrides: object) -> int:
        result = self.connection.execute(_INSERT_VERSION, self.version_params(**overrides))
        return int(result.lastrowid)

    def bnm_draft(self, quote_date: dt.date = QUOTE_DATE, base: str = "USD") -> int:
        """A draft as the BNM fetch writes it: no creator, a quote date."""
        reference = f"bnm:exchange-rate:{base}:{quote_date}:session=1200:middle_rate:unit=1"
        return self.draft(
            base=base, source="BNM", quote_date=quote_date, creator=None, reference=reference
        )

    def publish(self, version: int, start: dt.datetime | None, end: dt.datetime | None = None):
        params = {"start": start, "end": end, "approver": self.admin, "now": WRITTEN_AT}
        self.execute(_PUBLISH, id=version, **params)

    def set_end(self, version: int, end: dt.datetime | None) -> None:
        """Truncate (design §2 P2–P4) or, for R2, restore the previous version."""
        self.execute(_SET_END, id=version, end=end, now=WRITTEN_AT)

    def retire(self, version: int, end: dt.datetime) -> None:
        self.execute(_RETIRE, id=version, end=end, now=WRITTEN_AT)

    def withdraw(self, version: int) -> None:
        self.execute(_WITHDRAW, id=version, now=WRITTEN_AT)

    def discard(self, version: int) -> None:
        self.execute(_DISCARD, id=version, now=WRITTEN_AT)

    def attempt(self, **overrides: object) -> None:
        params: dict[str, object] = {
            "base": "USD",
            "source": "BNM",
            "requested_date": QUOTE_DATE,
            "outcome": "NO_QUOTE_FOR_DATE",
            "quote_date": None,
            "error_code": None,
            "version_id": None,
            "attempted_at": WRITTEN_AT,
        }
        params.update(overrides)
        self.execute(_INSERT_ATTEMPT, **params)

    # --- refusals --------------------------------------------------------------------

    def refuses(self, write: Callable[[], object]) -> tuple[int, str]:
        """Run `write` in a savepoint; return the MySQL error number and message."""
        with pytest.raises(DBAPIError) as raised:
            with self.connection.begin_nested():
                write()
        return _error(raised)

    def refused(self, statement, **params: object) -> tuple[int, str]:
        return self.refuses(lambda: self.execute(statement, **params))

    # --- reading ---------------------------------------------------------------------

    def row(self, version: int) -> Row:
        return self.execute(_ROW, id=version).one()

    def period(self, version: int) -> tuple[str, dt.datetime | None, dt.datetime | None]:
        status, start, end, _ = self.row(version)
        return status, start, end

    def open_slot(self, version: int) -> int | None:
        return self.row(version).open_slot

    def snapshot(self) -> list[tuple[object, ...]]:
        return [tuple(row) for row in self.execute(_ALL_ROWS)]


# --- fixtures ---------------------------------------------------------------------


@pytest.fixture
def alembic_config(monkeypatch) -> Iterator[Config]:
    """Tables, checks, generated column and triggers are the ones migration 0015 builds."""
    from app.core.config import get_settings

    monkeypatch.setenv("BILLING_DATABASE_URL", TEST_DATABASE_URL)
    get_settings.cache_clear()
    config = Config("alembic.ini")
    command.upgrade(config, "head")
    yield config
    get_settings.cache_clear()


@pytest.fixture
def engine(alembic_config: Config) -> Iterator[Engine]:
    engine = create_engine(TEST_DATABASE_URL)
    try:
        yield engine
    finally:
        engine.dispose()


def _rolled_back(engine: Engine, email: str = TEST_EMAIL) -> Iterator[Db]:
    """⚠️ The database is shared: every row a case writes is rolled back."""
    with engine.connect() as connection:
        transaction = connection.begin()
        try:
            yield Db(connection, email)
        finally:
            transaction.rollback()


@pytest.fixture
def db(engine: Engine) -> Iterator[Db]:
    yield from _rolled_back(engine)


# The design §2 timelines, written in the service's order.


def e4_timeline(db: Db) -> tuple[int, int]:
    """E1 then E4: A `[NULL, 10:05:01)`, B `[10:05:01, NULL)`."""
    a = db.draft()
    db.publish(a, None)
    b = db.draft()
    db.set_end(a, at(10, 5, 1))
    db.publish(b, at(10, 5, 1))
    return a, b


def e9_timeline(db: Db) -> tuple[int, int]:
    """Before E9: A `[NULL, 10:05:01)`, B `RETIRED [10:05:01, 12:00:01)` (E11)."""
    a, b = e4_timeline(db)
    db.retire(b, at(12, 0, 1))
    return a, b


# --- triggers (design §7 "触发器") --------------------------------------------------


def test_the_lock_table_holds_exactly_one_row(db: Db) -> None:
    """Deleting the lock row or inserting a second one is refused; the row is there to lock,
    exclusively for publishing and shared for billing."""
    delete_row = text("DELETE FROM fx_rate_locks WHERE id = 1")
    insert_row = text("INSERT INTO fx_rate_locks (id) VALUES (2)")

    assert db.refused(delete_row) == (_ER_SIGNAL_EXCEPTION, _ONE_LOCK_ROW)
    assert db.refused(insert_row) == (_ER_SIGNAL_EXCEPTION, _ONE_LOCK_ROW)
    assert db.execute(_LOCK_IDS).scalars().all() == [1]
    locked = text("SELECT id FROM fx_rate_locks WHERE id = 1 FOR UPDATE")
    assert db.execute(locked).scalars().all() == [1]
    shared = text("SELECT id FROM fx_rate_locks WHERE id = 1 FOR SHARE")
    assert db.execute(shared).scalars().all() == [1]


@pytest.mark.parametrize("status", ["PUBLISHED", "RETIRED", "DISCARDED"])
def test_a_version_enters_only_as_a_draft(db: Db, status: str) -> None:
    """A direct insert of anything but a draft is refused, so the checks on the publish
    transition cannot be skipped; drafts, manual and BNM, go in."""
    approved = status != "DISCARDED"
    before = db.snapshot()
    params = db.version_params(
        status=status,
        start=at(10) if approved else None,
        end=at(11) if status == "RETIRED" else None,
        approver=db.admin if approved else None,
        approved_at=WRITTEN_AT if approved else None,
    )

    assert db.refused(_INSERT_VERSION, **params) == (_ER_SIGNAL_EXCEPTION, _STARTS_AS_DRAFT)
    assert db.snapshot() == before

    manual = db.draft()
    bnm = db.bnm_draft()
    assert db.period(manual) == db.period(bnm) == ("DRAFT", None, None)


@pytest.mark.parametrize(
    "assignment",
    [
        f"rate = {OTHER_RATE}",
        "base_currency = 'EUR'",
        "source_reference = 'edited after publishing'",
        "observed_at = '2026-09-30 04:00:00'",
        "effective_from = '2026-10-01 09:00:00'",
        "approved_at = '2026-10-01 09:30:00'",
        "created_at = '2026-10-01 09:30:00'",
    ],
    ids=["rate", "currency", "reference", "observed-at", "start", "approved-at", "created-at"],
)
def test_a_published_version_refuses_edits(db: Db, assignment: str) -> None:
    """INV-6: only `effective_to`, `PUBLISHED → RETIRED` and `updated_at` change after
    publishing."""
    version = db.draft()
    db.publish(version, at(10))
    before = db.snapshot()

    refused = db.refused(_update(assignment), id=version)

    assert refused == (_ER_SIGNAL_EXCEPTION, _IMMUTABLE)
    assert db.snapshot() == before


def test_a_published_version_truncates_and_retires(db: Db) -> None:
    """The counterpart: truncating, touching `updated_at`, restoring the end and retiring with
    an end all pass."""
    version = db.draft()
    db.publish(version, at(10))

    db.set_end(version, at(11))
    db.execute(_update("updated_at = '2026-10-01 11:00:00'"), id=version)
    db.set_end(version, None)
    db.retire(version, at(12))

    assert db.period(version) == ("RETIRED", at(10), at(12))


@pytest.mark.parametrize("target", ["DRAFT", "DISCARDED"])
def test_a_published_version_can_only_be_retired(db: Db, target: str) -> None:
    version = db.draft()
    db.publish(version, at(10))

    refused = db.refused(_update(f"status = '{target}'"), id=version)

    assert refused == (_ER_SIGNAL_EXCEPTION, _RETIRE_ONLY)
    db.retire(version, at(11))
    assert db.period(version) == ("RETIRED", at(10), at(11))


def test_a_draft_never_goes_straight_to_retired(db: Db) -> None:
    """v2: a direct DRAFT → RETIRED would bring in a version that never passed the publish
    transition, and a RETIRED version still gives rates inside its period. A draft may be
    edited, discarded or published."""
    draft = db.draft()
    retire = text(
        "UPDATE fx_rate_versions SET status = 'RETIRED', effective_from = :start,"
        " effective_to = :end, approved_by = :approver, approved_at = :now WHERE id = :id"
    )
    params = {"start": at(10), "end": at(11), "approver": db.admin, "now": WRITTEN_AT}

    assert db.refused(retire, id=draft, **params) == (_ER_SIGNAL_EXCEPTION, _DRAFT_TRANSITION)
    assert db.period(draft) == ("DRAFT", None, None)

    db.execute(_update(f"rate = {OTHER_RATE}, source_reference = 'edited draft'"), id=draft)
    db.discard(draft)
    published = db.draft()
    db.publish(published, None)
    assert db.period(draft) == ("DISCARDED", None, None)
    assert db.period(published) == ("PUBLISHED", None, None)


@pytest.mark.parametrize(
    "assignment",
    [
        "effective_to = '2026-10-01 13:00:00'",
        "effective_to = '2026-10-01 11:00:00'",
        "effective_to = NULL",
        "status = 'PUBLISHED'",
        f"rate = {OTHER_RATE}",
        "updated_at = '2026-10-01 13:00:00'",
    ],
    ids=["later-end", "earlier-end", "no-end", "back-to-published", "rate", "updated-at"],
)
def test_a_retired_version_is_final(db: Db, assignment: str) -> None:
    """v2: no change to a retired version at all — not even its end (clearing it would let the
    retired rate cover the gap after it; shortening it would take rates from moments that
    already got one)."""
    version = db.draft()
    db.publish(version, at(10))
    db.retire(version, at(12))
    before = db.snapshot()

    assert db.refused(_update(assignment), id=version) == (_ER_SIGNAL_EXCEPTION, _FINAL)
    assert db.snapshot() == before


@pytest.mark.parametrize(
    "assignment",
    ["status = 'DRAFT'", f"rate = {OTHER_RATE}", "updated_at = '2026-10-01 13:00:00'"],
    ids=["back-to-draft", "rate", "updated-at"],
)
def test_a_discarded_version_is_final(db: Db, assignment: str) -> None:
    version = db.draft()
    db.discard(version)
    before = db.snapshot()

    assert db.refused(_update(assignment), id=version) == (_ER_SIGNAL_EXCEPTION, _FINAL)
    assert db.snapshot() == before


def test_the_same_edits_pass_on_a_draft_and_a_published_version(db: Db) -> None:
    """The counterpart of the two cases above: what is final for RETIRED / DISCARDED is an
    ordinary edit of a draft, and the end of a published version still moves."""
    draft = db.draft()
    published = db.draft()
    db.publish(published, at(10))

    db.execute(_update(f"rate = {OTHER_RATE}"), id=draft)
    db.execute(_update("updated_at = '2026-10-01 13:00:00'"), id=draft)
    db.set_end(published, at(13))
    db.set_end(published, at(11))

    assert db.period(published) == ("PUBLISHED", at(10), at(11))


def test_versions_are_never_deleted(db: Db) -> None:
    """A version in any state is kept (INV-6; a discarded draft stays as a trace). Discarding
    is the way to drop a draft, and it passes."""
    draft = db.draft()
    discarded = db.draft()
    db.discard(discarded)
    published, retired = e9_timeline(db)
    before = db.snapshot()

    for version in (draft, discarded, published, retired):
        refused = db.refused(_DELETE_VERSION, id=version)
        assert refused == (_ER_SIGNAL_EXCEPTION, _NEVER_DELETED), version
    assert db.snapshot() == before

    db.discard(draft)
    assert db.period(draft) == ("DISCARDED", None, None)


def test_fetch_attempts_are_append_only(db: Db) -> None:
    """Appending passes; changing or deleting an attempt is refused."""
    version = db.bnm_draft()
    db.attempt(outcome="NEW_DRAFT", quote_date=QUOTE_DATE, version_id=version)
    db.attempt(outcome="FAILED", error_code="TIMEOUT")
    count = text("SELECT COUNT(*) FROM fx_fetch_attempts")
    assert db.execute(count).scalar_one() == 2

    change = text("UPDATE fx_fetch_attempts SET error_code = 'NETWORK' WHERE outcome = 'FAILED'")
    remove = text("DELETE FROM fx_fetch_attempts")
    assert db.refused(change) == (_ER_SIGNAL_EXCEPTION, _APPEND_ONLY)
    assert db.refused(remove) == (_ER_SIGNAL_EXCEPTION, _APPEND_ONLY)
    assert db.execute(count).scalar_one() == 2


# --- checks on the columns (design §2 CHECK 1–5, fx_fetch_attempts v3) ---------------------


@pytest.mark.parametrize(
    "overrides",
    [
        {"base": "MYR"},
        {"quote": "USD"},
        {"rate": "0"},
        {"rate": "-1.1111111111"},
        {"source": "ECB"},
        {"source": "BNM", "quote_date": None},
    ],
    ids=[
        "base-myr",
        "quote-not-myr",
        "rate-zero",
        "rate-negative",
        "unknown-source",
        "bnm-without-quote-date",
    ],
)
def test_the_checks_refuse_a_bad_draft(db: Db, overrides: dict[str, object]) -> None:
    refused = db.refused(_INSERT_VERSION, **db.version_params(**overrides))

    assert refused[0] == _ER_CHECK_CONSTRAINT_VIOLATED
    # The counterpart: a manual draft without a quote date, a BNM draft with one, a manual one
    # with a quote date, and a lower-case code (the format is the application's, design v3).
    db.draft()
    db.bnm_draft()
    db.draft(quote_date=QUOTE_DATE)
    db.draft(base="usd")


@pytest.mark.parametrize(
    ("statement", "params"),
    [
        (_update("effective_from = :at"), {"at": at(10)}),
        (_update("effective_to = :at"), {"at": at(10)}),
        (_update("approved_by = :user, approved_at = :at"), {"at": at(10)}),
        (
            _update("status = 'DISCARDED', approved_by = :user, approved_at = :at"),
            {"at": at(10)},
        ),
        (
            _update("status = 'PUBLISHED', effective_from = :at, approved_at = :at"),
            {"at": at(10)},
        ),
        (
            _update("status = 'PUBLISHED', effective_from = :at, approved_by = :user"),
            {"at": at(10)},
        ),
    ],
    ids=[
        "draft-with-start",
        "draft-with-end",
        "draft-with-approver",
        "discarded-with-approver",
        "published-without-approver",
        "published-without-approval-time",
    ],
)
def test_the_checks_tie_the_period_and_the_approver_to_the_status(
    db: Db, statement, params: dict[str, object]
) -> None:
    """CHECK 1 and 2: drafts and discarded drafts have no period and no approver; published
    versions have both an approver and an approval time."""
    draft = db.draft()

    refused = db.refused(statement, id=draft, user=db.admin, **params)

    assert refused[0] == _ER_CHECK_CONSTRAINT_VIOLATED
    db.publish(draft, at(10))


@pytest.mark.parametrize("end", [at(10), at(9, 59, 59)], ids=["empty-on-published", "inverted"])
def test_an_empty_period_only_on_a_retired_version(db: Db, end: dt.datetime) -> None:
    """CHECK 3: a period never runs backwards, and the one empty period allowed is a RETIRED
    one (a withdrawn reservation, design §2 R1 / R2)."""
    version = db.draft()
    db.publish(version, at(10))

    assert db.refuses(lambda: db.set_end(version, end))[0] == _ER_CHECK_CONSTRAINT_VIOLATED
    db.withdraw(version)
    assert db.period(version) == ("RETIRED", at(10), at(10))


@pytest.mark.parametrize(
    ("overrides", "accepted"),
    [
        ({"outcome": "NEW_DRAFT", "quote_date": QUOTE_DATE, "version": True}, True),
        ({"outcome": "NEW_DRAFT", "quote_date": QUOTE_DATE}, False),
        ({"outcome": "NEW_DRAFT", "version": True}, False),
        (
            {"outcome": "NEW_DRAFT", "quote_date": QUOTE_DATE, "version": True, "error": "X"},
            False,
        ),
        ({"outcome": "NO_NEW_QUOTE", "quote_date": QUOTE_DATE}, True),
        ({"outcome": "NO_NEW_QUOTE", "quote_date": QUOTE_DATE, "error": "QUOTE_CHANGED"}, True),
        ({"outcome": "NO_NEW_QUOTE", "quote_date": QUOTE_DATE, "error": "TIMEOUT"}, False),
        ({"outcome": "NO_NEW_QUOTE", "quote_date": QUOTE_DATE, "version": True}, False),
        ({"outcome": "NO_NEW_QUOTE"}, False),
        ({"outcome": "NO_QUOTE_FOR_DATE"}, True),
        ({"outcome": "NO_QUOTE_FOR_DATE", "quote_date": QUOTE_DATE}, False),
        ({"outcome": "NO_QUOTE_FOR_DATE", "error": "HTTP_404"}, False),
        ({"outcome": "FAILED", "error": "UNIT_NOT_EXACT"}, True),
        ({"outcome": "FAILED"}, False),
        ({"outcome": "FAILED", "error": "BAD_PAYLOAD", "quote_date": QUOTE_DATE}, False),
        ({"outcome": "FAILED", "error": "BAD_PAYLOAD", "version": True}, False),
        ({"outcome": "SKIPPED"}, False),
        ({"outcome": "NO_QUOTE_FOR_DATE", "source": "MANUAL"}, False),
    ],
    ids=[
        "new-draft",
        "new-draft-without-version",
        "new-draft-without-date",
        "new-draft-with-error",
        "no-new-quote",
        "no-new-quote-changed",
        "no-new-quote-other-error",
        "no-new-quote-with-version",
        "no-new-quote-without-date",
        "no-quote-for-date",
        "no-quote-for-date-with-date",
        "no-quote-for-date-with-error",
        "failed",
        "failed-without-error",
        "failed-with-date",
        "failed-with-version",
        "unknown-outcome",
        "manual-source",
    ],
)
def test_the_columns_of_a_fetch_attempt_follow_its_outcome(
    db: Db, overrides: dict[str, object], accepted: bool
) -> None:
    """v3: each outcome carries exactly its columns; the source is BNM."""
    version = db.bnm_draft()
    params: dict[str, object] = {
        "outcome": overrides["outcome"],
        "quote_date": overrides.get("quote_date"),
        "error_code": overrides.get("error"),
        "version_id": version if overrides.get("version") else None,
        "source": overrides.get("source", "BNM"),
    }

    if accepted:
        db.attempt(**params)
    else:
        refused = db.refuses(lambda: db.attempt(**params))
        assert refused[0] == _ER_CHECK_CONSTRAINT_VIOLATED


# --- open_slot and the unique constraints (design §7 "open_slot 与唯一约束", v3 order) ---------


def test_open_slot_and_the_unique_constraints(db: Db) -> None:
    """① A published, B published truncating A; then a BNM and a manual draft, one discarded:
    all pass, and only B has `open_slot = 1` — the truncated A, the drafts and the discarded one
    are NULL (not 0, or the second of them would hit the unique index). ② While B is still open,
    the remaining draft written directly as a later open PUBLISHED version is refused (two open
    periods always intersect: the overlap trigger fires before the unique index, so only the
    refusal is asserted). ③ After B is retired another draft goes in: RETIRED, a truncated
    PUBLISHED, DISCARDED and DRAFT rows live side by side."""
    a, b = e4_timeline(db)
    bnm = db.bnm_draft()
    manual = db.draft()
    db.discard(manual)

    assert [db.open_slot(v) for v in (a, b, bnm, manual)] == [None, 1, None, None]

    before = db.snapshot()
    refused = db.refuses(lambda: db.publish(bnm, at(11)))
    assert refused[0] in (_ER_SIGNAL_EXCEPTION, _ER_DUP_ENTRY)
    assert db.snapshot() == before

    db.retire(b, at(12, 0, 1))
    later = db.draft()

    rows = {v: db.row(v) for v in (a, b, bnm, manual, later)}
    assert {v: row.status for v, row in rows.items()} == {
        a: "PUBLISHED",
        b: "RETIRED",
        bnm: "DRAFT",
        manual: "DISCARDED",
        later: "DRAFT",
    }
    assert {row.open_slot for row in rows.values()} == {None}


def test_one_bnm_version_per_quote_date(db: Db) -> None:
    """A second BNM version for the same currency and quote date is refused — also after the
    first one is discarded (the fetch then records NO_NEW_QUOTE). Another date and another
    currency are not held back. The constraint covers the source as well: a manual version
    with the same quote date as the BNM one is allowed, but two manual versions with the same
    non-NULL quote date collide too — only a NULL quote date (what the admin API writes for
    manual entries) escapes it."""
    first = db.bnm_draft()

    assert db.refuses(db.bnm_draft)[0] == _ER_DUP_ENTRY
    db.discard(first)
    assert db.refuses(db.bnm_draft)[0] == _ER_DUP_ENTRY

    db.bnm_draft(QUOTE_DATE + DAY)
    db.bnm_draft(base="EUR")
    db.draft()
    db.draft()
    db.draft(quote_date=QUOTE_DATE)

    assert db.refuses(lambda: db.draft(quote_date=QUOTE_DATE))[0] == _ER_DUP_ENTRY
    db.draft(quote_date=QUOTE_DATE + DAY)
    db.draft()


# --- database fallbacks (design §7 "数据库兜底") ---------------------------------------------


@pytest.mark.parametrize("timeline", ["e4", "e9"])
def test_no_version_from_all_time_after_a_timeline(db: Db, timeline: str) -> None:
    """A draft written directly as PUBLISHED `[NULL, NULL)` while the currency already has a
    timeline is refused (a period with no start and no end meets every non-empty one). The
    counterpart publishes after the end of the timeline: E7 after E4 (truncating B first), P4
    after E9 (filling the gap from E)."""
    if timeline == "e4":
        _, last = e4_timeline(db)
    else:
        _, last = e9_timeline(db)
    draft = db.draft()

    assert db.refuses(lambda: db.publish(draft, None)) == (_ER_SIGNAL_EXCEPTION, _OVERLAP)

    if timeline == "e4":
        db.set_end(last, at(10, 5, 2))
        db.publish(draft, at(10, 5, 2))
        assert db.period(draft) == ("PUBLISHED", at(10, 5, 2), None)
    else:
        db.publish(draft, at(12, 0, 1))
        assert db.period(draft) == ("PUBLISHED", at(12, 0, 1), None)


@pytest.mark.parametrize(
    ("start", "end"),
    [
        (at(12, 0, 1), at(15, 0, 1)),
        (at(13), at(14)),
        (None, at(9)),
    ],
    ids=["filling-the-gap", "inside-the-gap", "before-everything"],
)
def test_a_version_is_published_without_an_end(db: Db, start, end) -> None:
    """v2: the publish transition leaves the end empty — a direct write cannot slot a version
    into the gap a retirement left, nor before the first version. The counterpart is E9 ①:
    from the end of the gap, without an end."""
    e9_timeline(db)
    draft = db.draft()

    refused = db.refuses(lambda: db.publish(draft, start, end))

    assert refused == (_ER_SIGNAL_EXCEPTION, _PUBLISHED_WITH_END)
    db.publish(draft, at(12, 0, 1))
    assert db.period(draft) == ("PUBLISHED", at(12, 0, 1), None)


def test_a_retired_version_always_has_an_end(db: Db) -> None:
    """CHECK 4: retiring directly without writing the end is refused; with an end it passes."""
    version = db.draft()
    db.publish(version, at(10))

    refused = db.refused(_update("status = 'RETIRED'"), id=version)

    assert refused[0] == _ER_CHECK_CONSTRAINT_VIOLATED
    assert db.period(version) == ("PUBLISHED", at(10), None)
    db.retire(version, at(11))
    assert db.period(version) == ("RETIRED", at(10), at(11))


def test_an_empty_timeline_takes_a_version_from_all_time(db: Db) -> None:
    """E3: the only other row is an empty RETIRED period (a withdrawn reservation), which is not
    on the timeline — a draft published `[NULL, NULL)` passes (the fallbacks do not refuse
    more than they should)."""
    withdrawn = db.draft()
    db.publish(withdrawn, NEXT_DAY_0430)
    db.withdraw(withdrawn)
    draft = db.draft()

    db.publish(draft, None)

    assert db.period(withdrawn) == ("RETIRED", NEXT_DAY_0430, NEXT_DAY_0430)
    assert db.period(draft) == ("PUBLISHED", None, None)
    assert db.open_slot(draft) == 1


def test_the_service_write_orders_pass(db: Db) -> None:
    """Design §2's P1–P4 and R1 / R2 in the service's write order, one currency, one after the
    other: every step passes the triggers, and the timeline ends with exactly one open
    version."""
    # E1 (P1), E4 (P2), E7 (P2, the next second).
    a = db.draft()
    db.publish(a, None)
    b = db.draft()
    db.set_end(a, at(10, 5, 1))
    db.publish(b, at(10, 5, 1))
    c = db.draft()
    db.set_end(b, at(10, 5, 2))
    db.publish(c, at(10, 5, 2))
    # E11 (R1): C retired at t.
    db.retire(c, at(12, 0, 1))
    # E9 ① (P4): D fills the gap from the end of C.
    d = db.draft()
    db.publish(d, at(12, 0, 1))
    # E5 (P2 with F): E reserved for the next day; E8 ③ (P3 with F after E's start).
    e = db.draft()
    db.set_end(d, NEXT_DAY_0430)
    db.publish(e, NEXT_DAY_0430)
    f = db.draft()
    db.set_end(e, OCT_3_0430)
    db.publish(f, OCT_3_0430)
    # R2 twice: withdraw F, restore E; withdraw E, restore D.
    db.withdraw(f)
    db.set_end(e, None)
    db.withdraw(e)
    db.set_end(d, None)
    # E12 (R1, start = t): G published and retired in the same second, empty, nothing restored;
    # then P4 from D's end.
    g = db.draft()
    db.set_end(d, at(15, 0, 1))
    db.publish(g, at(15, 0, 1))
    db.retire(g, at(15, 0, 1))
    h = db.draft()
    db.publish(h, at(15, 0, 1))

    assert [db.period(v) for v in (a, b, c, d, e, f, g, h)] == [
        ("PUBLISHED", None, at(10, 5, 1)),
        ("PUBLISHED", at(10, 5, 1), at(10, 5, 2)),
        ("RETIRED", at(10, 5, 2), at(12, 0, 1)),
        ("PUBLISHED", at(12, 0, 1), at(15, 0, 1)),
        ("RETIRED", NEXT_DAY_0430, NEXT_DAY_0430),
        ("RETIRED", OCT_3_0430, OCT_3_0430),
        ("RETIRED", at(15, 0, 1), at(15, 0, 1)),
        ("PUBLISHED", at(15, 0, 1), None),
    ]
    assert [db.open_slot(v) for v in (a, b, c, d, e, f, g, h)] == [None] * 7 + [1]


def test_withdrawing_a_reservation_after_a_retirement_keeps_the_retired_end(db: Db) -> None:
    """E14: after E9 ② (C reserved from the next day, after B's retirement), withdrawing C
    passes and B stays as it was; the next publish without a time fills the gap from B's end
    (P4)."""
    a, b = e9_timeline(db)
    c = db.draft()
    db.publish(c, NEXT_DAY_0430)

    db.withdraw(c)
    d = db.draft()
    db.publish(d, at(12, 0, 1))

    assert db.period(a) == ("PUBLISHED", None, at(10, 5, 1))
    assert db.period(b) == ("RETIRED", at(10, 5, 1), at(12, 0, 1))
    assert db.period(c) == ("RETIRED", NEXT_DAY_0430, NEXT_DAY_0430)
    assert db.period(d) == ("PUBLISHED", at(12, 0, 1), None)


# --- no overlapping periods (design §7 "区间不重叠") -----------------------------------------


@pytest.mark.parametrize("ended_by", ["truncation", "retirement"])
@pytest.mark.parametrize(
    ("start", "accepted"),
    [
        (at(10, 0, 5), False),
        (at(10), False),
        (at(10) - DAY, False),
        (None, False),
        (at(10, 0, 10), True),
        (at(10, 0, 20), True),
    ],
    ids=["inside", "same-start", "before", "from-all-time", "end-to-end", "after-a-gap"],
)
def test_a_period_that_meets_another_is_refused(
    db: Db, ended_by: str, start: dt.datetime | None, accepted: bool
) -> None:
    """A direct publish that intersects the non-empty period of a PUBLISHED or RETIRED version
    of the same currency is refused; end-to-end and after a gap pass."""
    existing = db.draft()
    db.publish(existing, at(10))
    if ended_by == "truncation":
        db.set_end(existing, at(10, 0, 10))
    else:
        db.retire(existing, at(10, 0, 10))
    candidate = db.draft()

    if accepted:
        db.publish(candidate, start)
        assert db.period(candidate) == ("PUBLISHED", start, None)
    else:
        refused = db.refuses(lambda: db.publish(candidate, start))
        assert refused == (_ER_SIGNAL_EXCEPTION, _OVERLAP)


def test_two_truncated_versions_that_intersect_are_refused(db: Db) -> None:
    """X `[10:00:00, 10:00:10)` and Y `[10:00:10, 10:00:15)` meet end to end; moving X's end
    into Y (or clearing it) is refused, moving it back is not."""
    x = db.draft()
    db.publish(x, at(10))
    db.set_end(x, at(10, 0, 10))
    y = db.draft()
    db.publish(y, at(10, 0, 10))
    db.set_end(y, at(10, 0, 15))

    for end in (at(10, 0, 12), at(10, 0, 20), None):
        refused = db.refuses(lambda end=end: db.set_end(x, end))
        assert refused == (_ER_SIGNAL_EXCEPTION, _OVERLAP), end
    assert db.period(x) == ("PUBLISHED", at(10), at(10, 0, 10))

    db.set_end(x, at(10, 0, 5))
    db.set_end(x, at(10, 0, 10))
    assert db.period(y) == ("PUBLISHED", at(10, 0, 10), at(10, 0, 15))


def test_an_empty_period_takes_no_part(db: Db) -> None:
    """E13: B reserved from the next day truncates A; withdrawing B leaves the empty
    `[F, F)` inside A's restored period — both pass, the empty period is not compared. The next
    publish (P2) truncates A, not B."""
    a = db.draft()
    db.publish(a, None)
    b = db.draft()
    db.set_end(a, NEXT_DAY_0430)
    db.publish(b, NEXT_DAY_0430)

    db.withdraw(b)
    db.set_end(a, None)
    c = db.draft()
    db.set_end(a, at(11, 10, 1))
    db.publish(c, at(11, 10, 1))

    assert db.period(a) == ("PUBLISHED", None, at(11, 10, 1))
    assert db.period(b) == ("RETIRED", NEXT_DAY_0430, NEXT_DAY_0430)
    assert db.period(c) == ("PUBLISHED", at(11, 10, 1), None)


def test_other_currencies_do_not_overlap(db: Db) -> None:
    """The check is per currency pair: USD and EUR both from all time pass, a second USD one
    does not."""
    usd = db.draft()
    eur = db.draft(base="EUR")
    db.publish(usd, None)
    db.publish(eur, None)

    second = db.draft()
    assert db.refuses(lambda: db.publish(second, None)) == (_ER_SIGNAL_EXCEPTION, _OVERLAP)
    assert db.open_slot(usd) == db.open_slot(eur) == 1


def test_the_overlap_check_takes_the_fx_rate_lock(engine: Engine) -> None:
    """Design §2: the trigger locks `fx_rate_locks` before it looks, so a direct write is
    serialised with the service's publishing. While another transaction holds the lock, a
    direct publish waits (and times out here); editing a draft does not touch the lock."""
    holders = _rolled_back(engine)
    writers = _rolled_back(engine, OTHER_TEST_EMAIL)
    holder = next(holders)
    writer = next(writers)
    try:
        locked = text("SELECT id FROM fx_rate_locks WHERE id = 1 FOR UPDATE")
        assert holder.execute(locked).scalars().all() == [1]
        writer.execute(text("SET SESSION innodb_lock_wait_timeout = 1"))
        draft = writer.draft()

        writer.execute(_update(f"rate = {OTHER_RATE}"), id=draft)
        refused = writer.refuses(lambda: writer.publish(draft, None))

        assert refused[0] == _ER_LOCK_WAIT_TIMEOUT
        assert writer.period(draft) == ("DRAFT", None, None)
    finally:
        writers.close()
        holders.close()


# --- migration (design §7 "迁移") ----------------------------------------------------------


def _tables_triggers_and_locks() -> tuple[set[str], set[str], list[int] | None]:
    """A fresh engine each time: nothing pooled holds a metadata lock across the DDL."""
    engine = create_engine(TEST_DATABASE_URL)
    try:
        tables = set(inspect(engine).get_table_names())
        with engine.connect() as connection:
            triggers = set(connection.execute(_TRIGGER_NAMES).scalars())
            locks = None
            if "fx_rate_locks" in tables:
                locks = list(connection.execute(_LOCK_IDS).scalars())
        return tables, triggers, locks
    finally:
        engine.dispose()


def test_upgrade_and_downgrade(alembic_config: Config) -> None:
    """Downgrading to 0014 drops the three tables and their triggers, and 0016's two lock
    table triggers, and nothing else; upgrading again builds them with exactly one lock row."""
    try:
        tables, triggers, locks = _tables_triggers_and_locks()
        assert _TABLES <= tables
        assert _TRIGGERS | _TRIGGERS_0016 <= triggers
        assert locks == [1]

        command.downgrade(alembic_config, "0014_pricing_rules")
        dropped = _TRIGGERS | _TRIGGERS_0016
        assert _tables_triggers_and_locks() == (tables - _TABLES, triggers - dropped, None)

        command.upgrade(alembic_config, "head")
        assert _tables_triggers_and_locks() == (tables, triggers, [1])
    finally:
        command.upgrade(alembic_config, "head")

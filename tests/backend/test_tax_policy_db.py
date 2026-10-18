"""The tax policy tables on a real MySQL: triggers, checks, the open_slot unique index, the
overlap check, the lock and the foreign keys (design gate #332 v3, AIH-TASK-080 = 080 of design
§11).

The rows of design §7 marked `test_tax_policy_db.py`, each refusal paired with a case that proves
the database does not refuse more than it should:

- **triggers**: versions enter only as drafts; a draft is only published, without an end; the
  tax flags agree with each other and with the rate on the publish transition (rule 3b); a
  published version only truncates and retires; a retired version is final; versions are never
  deleted; the one-row lock table;
- **checks**: every named check refuses (errno 3819, the constraint name in the message) and
  the boundary values pass; `status = 'DISCARDED'` is refused by the triggers first (1644, v3);
- **open_slot and the unique index**, **no overlapping periods**, **the trigger takes the lock**
  (two connections) and **foreign keys**.

There is no service layer yet (078), so rows are written with plain SQL, the way a direct write
that bypasses the service would. Each case runs in one transaction that is rolled back, and
`tax_policy_versions` and `audit_logs` are truncated before and after every case (design §7);
`tax_policy_locks` is never touched — its one row belongs to the migration.

Needs `BILLING_TEST_DATABASE_URL` pointing at a database that may be wiped; skips without it —
**a skip is not a pass**, CI sets it.
"""

from __future__ import annotations

import datetime as dt
import itertools
import json
import os
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from alembic.config import Config
from sqlalchemy import Engine, create_engine, insert, text
from sqlalchemy.engine import Connection, Row
from sqlalchemy.exc import DBAPIError

from alembic import command
from app.models.auth import User, UserRole

TEST_DATABASE_URL = os.environ.get("BILLING_TEST_DATABASE_URL", "")

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="BILLING_TEST_DATABASE_URL is not set; the tax policy triggers need a real MySQL",
)

# `users.email` is unique: each user of a case gets its own address (design §6: test domain).
TEST_EMAIL_DOMAIN = "@tax-policy-test.example.com"
ADMIN_EMAIL = f"tax-policy-db{TEST_EMAIL_DOMAIN}"
OTHER_EMAIL = f"tax-policy-db-other{TEST_EMAIL_DOMAIN}"
SPARE_EMAIL = f"tax-policy-db-spare{TEST_EMAIL_DOMAIN}"

# Design §2: Kuala Lumpur 2026-10-17 00:00 = 2026-10-16T16:00:00 UTC; later periods a day apart.
EFFECTIVE_FROM = dt.datetime(2026, 10, 16, 16)
DAY = dt.timedelta(days=1)
SECOND = dt.timedelta(seconds=1)
D2 = EFFECTIVE_FROM + DAY
D3 = D2 + DAY
WRITTEN_AT = dt.datetime(2026, 10, 18, 3, 4, 5)

# The first policy's treatment (design §2「`treatment_json` 的形状」).
NOTICE_ZH = "价格为最终价，已包含所有费用，不另收任何税费"
NOTICE_EN = "Prices are final and include all charges. No tax is charged separately."
INITIAL_TREATMENT: dict[str, Any] = {
    "schema_version": 1,
    "sst_registered": False,
    "tax_charged": False,
    "document_notice": {"zh": NOTICE_ZH, "en": NOTICE_EN},
}
_MISSING = object()

# MySQL error numbers and the trigger messages (migration 0022).
_ER_LOCK_WAIT_TIMEOUT = 1205
_ER_DUP_ENTRY = 1062
_ER_ROW_IS_REFERENCED_2 = 1451
_ER_SIGNAL_EXCEPTION = 1644
_ER_CHECK_CONSTRAINT_VIOLATED = 3819
_ONE_LOCK_ROW = "tax_policy_locks holds exactly one row"
_STARTS_AS_DRAFT = "a tax policy version starts as a draft"
_FINAL = "a retired tax policy version is final"
_DRAFT_TRANSITION = "a draft tax policy version can only be published"
_PUBLISHED_WITH_END = "a tax policy version is published without an end"
_CONTRADICTS = "a tax policy treatment contradicts itself or its rate"
_IMMUTABLE = "a published tax policy version is immutable"
_RETIRE_ONLY = "a published tax policy version can only be retired"
_OVERLAP = "tax policy periods overlap"
_NEVER_DELETED = "tax policy versions are never deleted"

_INSERT_VERSION = text(
    "INSERT INTO tax_policy_versions (public_id, jurisdiction, policy_type, rate,"
    " treatment_json, status, effective_from, effective_to, source_reference, created_by,"
    " approved_by, approved_at, created_at, updated_at) VALUES (:public_id, :jurisdiction,"
    " :policy_type, :rate, :treatment, :status, :start, :end, :reference, :creator, :approver,"
    " :approved_at, :now, :now)"
)
_PUBLISH = text(
    "UPDATE tax_policy_versions SET status = 'PUBLISHED', effective_from = :start,"
    " effective_to = :end, approved_by = :approver, approved_at = :approved_at,"
    " updated_at = :now WHERE id = :id"
)
_SET_END = text(
    "UPDATE tax_policy_versions SET effective_to = :end, updated_at = :now WHERE id = :id"
)
_RETIRE = text(
    "UPDATE tax_policy_versions SET status = 'RETIRED', effective_to = :end, updated_at = :now"
    " WHERE id = :id"
)
_ROW = text(
    "SELECT status, effective_from, effective_to, open_slot FROM tax_policy_versions WHERE id = :id"
)
_ALL_ROWS = text(
    "SELECT id, public_id, jurisdiction, policy_type, rate, treatment_json, status,"
    " effective_from, effective_to, open_slot, source_reference, created_by, approved_by,"
    " approved_at, created_at, updated_at FROM tax_policy_versions ORDER BY id"
)
_DELETE_VERSION = text("DELETE FROM tax_policy_versions WHERE id = :id")
_DELETE_USER = text("DELETE FROM users WHERE id = :id")
_LOCK_IDS = text("SELECT id FROM tax_policy_locks ORDER BY id")
_LOCK_FOR_UPDATE = text("SELECT id FROM tax_policy_locks WHERE id = 1 FOR UPDATE")


def _update(assignment: str):
    return text(f"UPDATE tax_policy_versions SET {assignment} WHERE id = :id")


def treatment(notice: dict[str, str] | None = None, **flags: object) -> str:
    """The first policy's treatment as JSON text, with `flags` on top (`_MISSING` drops a key)."""
    value = dict(INITIAL_TREATMENT)
    if notice is not None:
        value["document_notice"] = notice
    for key, flag in flags.items():
        if flag is _MISSING:
            del value[key]
        else:
            value[key] = flag
    return json.dumps(value)


def _error(raised: pytest.ExceptionInfo[DBAPIError]) -> tuple[int, str]:
    errno, message = raised.value.orig.args[:2]
    return int(errno), str(message)


class Db:
    """One connection inside a transaction the fixture rolls back."""

    def __init__(self, connection: Connection) -> None:
        self.connection = connection
        self._numbers = itertools.count(1)
        self.admin = self.user(ADMIN_EMAIL)
        self.other = self.user(OTHER_EMAIL)

    # --- writing ---------------------------------------------------------------------

    def user(self, email: str) -> int:
        created = self.connection.execute(
            insert(User).values(
                email=email,
                password_hash="not-a-real-hash",
                role=UserRole.ADMIN,
                created_at=WRITTEN_AT,
                updated_at=WRITTEN_AT,
            )
        )
        return int(created.inserted_primary_key[0])

    def execute(self, statement, **params: object) -> Any:
        return self.connection.execute(statement, params)

    def version_params(self, **overrides: object) -> dict[str, object]:
        """A MY / SST draft with the first policy's treatment, with `overrides` on top."""
        params: dict[str, object] = {
            "public_id": f"00000000-0000-0000-0000-{next(self._numbers):012d}",
            "jurisdiction": "MY",
            "policy_type": "SST",
            "rate": None,
            "treatment": treatment(),
            "status": "DRAFT",
            "start": None,
            "end": None,
            "reference": "fictional policy for a database test",
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

    def publish(
        self,
        version: int,
        start: dt.datetime | None = EFFECTIVE_FROM,
        end: dt.datetime | None = None,
        *,
        approver: int | None = None,
        approved_at: dt.datetime | None = WRITTEN_AT,
    ) -> None:
        params = {
            "start": start,
            "end": end,
            "approver": self.admin if approver is None else approver,
            "approved_at": approved_at,
            "now": WRITTEN_AT,
        }
        self.execute(_PUBLISH, id=version, **params)

    def set_end(self, version: int, end: dt.datetime | None) -> None:
        self.execute(_SET_END, id=version, end=end, now=WRITTEN_AT)

    def retire(self, version: int, end: dt.datetime | None) -> None:
        self.execute(_RETIRE, id=version, end=end, now=WRITTEN_AT)

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


def violates(refused: tuple[int, str], constraint: str) -> bool:
    """A CHECK refusal naming `constraint` (MySQL: "Check constraint '<name>' is violated.")."""
    errno, message = refused
    return errno == _ER_CHECK_CONSTRAINT_VIOLATED and f"'{constraint}'" in message


# --- fixtures ---------------------------------------------------------------------


def clean(engine: Engine) -> None:
    """⚠️ The database is shared: wipe before and after every case (design §7).

    The version triggers refuse DELETE and the audit table is append-only (0010), so TRUNCATE
    (DDL, no triggers) with foreign key checks off. `tax_policy_locks` is never touched: its one
    row belongs to the migration, and a version row left behind would make 0022's downgrade
    refuse in `test_migrations.py`.
    """
    with engine.begin() as connection:
        connection.exec_driver_sql("SET FOREIGN_KEY_CHECKS = 0")
        try:
            connection.exec_driver_sql("TRUNCATE TABLE tax_policy_versions")
            connection.exec_driver_sql("TRUNCATE TABLE audit_logs")
        finally:
            connection.exec_driver_sql("SET FOREIGN_KEY_CHECKS = 1")


@pytest.fixture
def engine(monkeypatch) -> Iterator[Engine]:
    """Tables, checks, generated column and triggers are the ones migration 0022 builds."""
    from app.core.config import get_settings

    monkeypatch.setenv("BILLING_DATABASE_URL", TEST_DATABASE_URL)
    get_settings.cache_clear()
    command.upgrade(Config("alembic.ini"), "head")
    get_settings.cache_clear()

    engine = create_engine(TEST_DATABASE_URL)
    clean(engine)
    try:
        yield engine
    finally:
        clean(engine)
        engine.dispose()


def _rolled_back(engine: Engine) -> Iterator[Db]:
    """⚠️ The database is shared: every row a case writes, users included, is rolled back."""
    with engine.connect() as connection:
        transaction = connection.begin()
        try:
            yield Db(connection)
        finally:
            transaction.rollback()


@pytest.fixture
def db(engine: Engine) -> Iterator[Db]:
    yield from _rolled_back(engine)


# --- triggers: insert (design §7「触发器：插入」) ----------------------------------------------


@pytest.mark.parametrize("status", ["PUBLISHED", "RETIRED"])
def test_a_version_enters_only_as_a_draft(db: Db, status: str) -> None:
    """A direct insert of anything but a draft is refused (with columns that satisfy every
    check, so the trigger is what refuses), so the checks on the publish transition cannot be
    skipped; a draft goes in."""
    before = db.snapshot()
    params = db.version_params(
        status=status,
        start=EFFECTIVE_FROM,
        end=D2 if status == "RETIRED" else None,
        approver=db.admin,
        approved_at=WRITTEN_AT,
    )

    assert db.refused(_INSERT_VERSION, **params) == (_ER_SIGNAL_EXCEPTION, _STARTS_AS_DRAFT)
    assert db.snapshot() == before

    draft = db.draft()
    assert db.period(draft) == ("DRAFT", None, None)


# --- triggers: drafts (design §7「触发器：草稿」) ----------------------------------------------


def test_a_draft_never_goes_straight_to_retired(db: Db) -> None:
    draft = db.draft()
    retire = text(
        "UPDATE tax_policy_versions SET status = 'RETIRED', effective_from = :start,"
        " effective_to = :end, approved_by = :approver, approved_at = :now WHERE id = :id"
    )
    params = {"start": EFFECTIVE_FROM, "end": D2, "approver": db.admin, "now": WRITTEN_AT}

    assert db.refused(retire, id=draft, **params) == (_ER_SIGNAL_EXCEPTION, _DRAFT_TRANSITION)
    assert db.period(draft) == ("DRAFT", None, None)


def test_a_draft_is_published_without_an_end(db: Db) -> None:
    """Rule 3: a newly published version is always the open end of the timeline. The
    counterpart: a start, no end and an approver pass."""
    draft = db.draft()

    refused = db.refuses(lambda: db.publish(draft, EFFECTIVE_FROM, D2))

    assert refused == (_ER_SIGNAL_EXCEPTION, _PUBLISHED_WITH_END)
    assert db.period(draft) == ("DRAFT", None, None)
    db.publish(draft, EFFECTIVE_FROM)
    assert db.period(draft) == ("PUBLISHED", EFFECTIVE_FROM, None)


def test_a_draft_may_be_edited(db: Db) -> None:
    draft = db.draft()

    edit = _update("source_reference = :value, updated_at = :at")
    db.execute(edit, id=draft, value="edited draft", at=WRITTEN_AT + SECOND)

    reference = text("SELECT source_reference FROM tax_policy_versions WHERE id = :id")
    assert db.execute(reference, id=draft).scalar_one() == "edited draft"
    assert db.period(draft) == ("DRAFT", None, None)


# --- triggers: tax consistency on publishing (design §7「触发器：发布时税务一致性」, v2) ----


@pytest.mark.parametrize(
    ("rate", "flags"),
    [
        (None, {"sst_registered": True, "tax_charged": True}),
        ("0.080000", {"sst_registered": False, "tax_charged": False}),
        ("0.080000", {"sst_registered": False, "tax_charged": True}),
        (None, {"tax_charged": "false"}),
        (None, {"sst_registered": _MISSING}),
    ],
    ids=[
        "charged-without-rate",
        "rate-without-charging",
        "unregistered-charging",
        "charged-as-string",
        "registered-missing",
    ],
)
def test_a_contradictory_treatment_is_not_published(db: Db, rate, flags) -> None:
    """Rule 3b: both flags are JSON booleans, an unregistered policy charges no tax, and no tax
    charged ⇔ no rate. The draft itself goes in (the shape is the application's)."""
    draft = db.draft(rate=rate, treatment=treatment(**flags))

    refused = db.refuses(lambda: db.publish(draft))

    assert refused == (_ER_SIGNAL_EXCEPTION, _CONTRADICTS)
    assert db.period(draft) == ("DRAFT", None, None)


def test_a_consistent_treatment_is_published(db: Db) -> None:
    """The counterpart: `rate IS NULL` with both flags `false` passes — `JSON_TYPE` gives
    `BOOLEAN` and `JSON_UNQUOTE` gives `true` / `false` (design §10 assumption 4)."""
    draft = db.draft()

    db.publish(draft)

    assert db.period(draft) == ("PUBLISHED", EFFECTIVE_FROM, None)


# --- triggers: a published version is immutable (design §7「触发器：已发布不可变」) ------------

_OTHER_USER = "<the other user>"
_EDITED_EN = NOTICE_EN[:-1] + "!"


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("rate", "0.080000"),
        ("treatment_json", treatment(notice={"zh": NOTICE_ZH, "en": _EDITED_EN})),
        ("effective_from", EFFECTIVE_FROM - DAY),
        ("source_reference", "edited after publishing"),
        ("jurisdiction", "SG"),
        ("policy_type", "GST"),
        ("approved_by", _OTHER_USER),
        ("approved_at", WRITTEN_AT + SECOND),
        ("created_by", _OTHER_USER),
        ("created_at", WRITTEN_AT - DAY),
        ("public_id", "00000000-0000-0000-0000-999999999999"),
    ],
    ids=[
        "rate",
        "treatment",
        "start",
        "reference",
        "jurisdiction",
        "policy-type",
        "approved-by",
        "approved-at",
        "created-by",
        "created-at",
        "public-id",
    ],
)
def test_a_published_version_refuses_edits(db: Db, column: str, value: object) -> None:
    """INV-6: only `effective_to`, `PUBLISHED → RETIRED` and `updated_at` change after
    publishing. Values that would fail a check or a foreign key are still refused by the trigger,
    which runs first."""
    version = db.draft()
    db.publish(version)
    before = db.snapshot()
    if value == _OTHER_USER:
        value = db.other

    refused = db.refused(_update(f"{column} = :value"), id=version, value=value)

    assert refused == (_ER_SIGNAL_EXCEPTION, _IMMUTABLE)
    assert db.snapshot() == before


def test_the_same_treatment_in_another_key_order_is_not_an_edit(db: Db) -> None:
    """`<=>` on the JSON column compares JSON values, not text (design §10 assumption 1): the
    same object written with its keys in another order passes."""
    version = db.draft()
    db.publish(version)
    reordered = {
        "document_notice": {"en": NOTICE_EN, "zh": NOTICE_ZH},
        "tax_charged": False,
        "sst_registered": False,
        "schema_version": 1,
    }

    db.execute(_update("treatment_json = :value"), id=version, value=json.dumps(reordered))

    stored = text("SELECT treatment_json FROM tax_policy_versions WHERE id = :id")
    assert json.loads(db.execute(stored, id=version).scalar_one()) == INITIAL_TREATMENT
    assert db.period(version) == ("PUBLISHED", EFFECTIVE_FROM, None)


def test_a_published_version_never_goes_back_to_draft(db: Db) -> None:
    version = db.draft()
    db.publish(version)

    refused = db.refused(_update("status = 'DRAFT'"), id=version)

    assert refused == (_ER_SIGNAL_EXCEPTION, _RETIRE_ONLY)
    assert db.period(version) == ("PUBLISHED", EFFECTIVE_FROM, None)


# --- triggers: what a published version may change (design §7「触发器：已发布允许的改动」) ---


def test_a_published_version_truncates_and_retires(db: Db) -> None:
    """Touching `updated_at`, truncating and retiring with an end pass; retiring without an end
    is refused by `ck_tax_policy_versions_retired_end`."""
    version = db.draft()
    db.publish(version)

    db.execute(_update("updated_at = :value"), id=version, value=WRITTEN_AT + SECOND)
    refused = db.refuses(lambda: db.retire(version, None))
    assert violates(refused, "ck_tax_policy_versions_retired_end"), refused
    assert db.period(version) == ("PUBLISHED", EFFECTIVE_FROM, None)

    db.set_end(version, D3)
    assert db.period(version) == ("PUBLISHED", EFFECTIVE_FROM, D3)
    db.retire(version, D3)
    assert db.period(version) == ("RETIRED", EFFECTIVE_FROM, D3)


# --- triggers: final states and deletion (design §7「触发器：终态与删除」) -------------------


@pytest.mark.parametrize(
    ("column", "value"),
    [("effective_to", D3), ("updated_at", WRITTEN_AT + SECOND)],
    ids=["end", "updated-at"],
)
def test_a_retired_version_is_final(db: Db, column: str, value: object) -> None:
    version = db.draft()
    db.publish(version)
    db.retire(version, D2)
    before = db.snapshot()

    refused = db.refused(_update(f"{column} = :value"), id=version, value=value)

    assert refused == (_ER_SIGNAL_EXCEPTION, _FINAL)
    assert db.snapshot() == before


def test_versions_are_never_deleted(db: Db) -> None:
    """A version in any state is kept: it is referenced by every snapshot (INV-6)."""
    draft = db.draft()
    published = db.draft()
    db.publish(published)
    before = db.snapshot()

    for version in (draft, published):
        refused = db.refused(_DELETE_VERSION, id=version)
        assert refused == (_ER_SIGNAL_EXCEPTION, _NEVER_DELETED), version
    assert db.snapshot() == before


# --- the lock table (design §7「锁表」) -----------------------------------------------------


def test_the_lock_table_holds_exactly_one_row(db: Db) -> None:
    """After the migration the lock table is `{1}`; a second row, moving the row and deleting
    it are refused. Locking it is not a write."""
    assert db.execute(_LOCK_IDS).scalars().all() == [1]

    insert_row = text("INSERT INTO tax_policy_locks (id) VALUES (2)")
    move_row = text("UPDATE tax_policy_locks SET id = 2 WHERE id = 1")
    delete_row = text("DELETE FROM tax_policy_locks WHERE id = 1")
    for statement in (insert_row, move_row, delete_row):
        assert db.refused(statement) == (_ER_SIGNAL_EXCEPTION, _ONE_LOCK_ROW)

    assert db.execute(_LOCK_IDS).scalars().all() == [1]
    assert db.execute(_LOCK_FOR_UPDATE).scalars().all() == [1]


# --- checks (design §7「CHECK」) ------------------------------------------------------------


@pytest.mark.parametrize(
    ("overrides", "constraint"),
    [
        ({"rate": "1.000000"}, "ck_tax_policy_versions_rate"),
        ({"rate": "-0.000001"}, "ck_tax_policy_versions_rate"),
        ({"rate": "8"}, "ck_tax_policy_versions_rate"),
        ({"rate": "0"}, None),
        ({"rate": "0.999999"}, None),
        ({"rate": None}, None),
        ({"jurisdiction": "SG"}, "ck_tax_policy_versions_jurisdiction"),
        ({"policy_type": "GST"}, "ck_tax_policy_versions_policy_type"),
        ({"approver": "admin"}, "ck_tax_policy_versions_unpublished"),
        ({"start": EFFECTIVE_FROM}, "ck_tax_policy_versions_unpublished"),
    ],
    ids=[
        "rate-one",
        "rate-negative",
        "rate-as-percent",
        "rate-zero",
        "rate-just-below-one",
        "rate-null",
        "jurisdiction",
        "policy-type",
        "draft-with-approver",
        "draft-with-start",
    ],
)
def test_the_checks_on_a_draft(db: Db, overrides: dict[str, object], constraint) -> None:
    """INV-10: `rate` is a fraction in `[0, 1)` — "8" for 8% lands on the named check, not on
    the column width (design §2). A draft has no period and no approver."""
    if overrides.get("approver") == "admin":
        overrides = {**overrides, "approver": db.admin}
    params = db.version_params(**overrides)

    if constraint is None:
        db.execute(_INSERT_VERSION, **params)
    else:
        refused = db.refused(_INSERT_VERSION, **params)
        assert violates(refused, constraint), refused


@pytest.mark.parametrize(
    ("start", "approved_at", "constraint"),
    [
        (EFFECTIVE_FROM, None, "ck_tax_policy_versions_approved"),
        (None, WRITTEN_AT, "ck_tax_policy_versions_published_start"),
    ],
    ids=["without-approval-time", "without-start"],
)
def test_the_checks_on_the_publish_transition(db: Db, start, approved_at, constraint) -> None:
    """A published version has an approver, an approval time and a concrete start (a tax policy
    has no "from all time", design §9). The counterpart publishes the same draft properly."""
    draft = db.draft()

    refused = db.refuses(lambda: db.publish(draft, start, approved_at=approved_at))

    assert violates(refused, constraint), refused
    assert db.period(draft) == ("DRAFT", None, None)
    db.publish(draft)


@pytest.mark.parametrize(
    "end", [EFFECTIVE_FROM, EFFECTIVE_FROM - SECOND], ids=["empty", "inverted"]
)
def test_a_published_period_never_runs_backwards(db: Db, end: dt.datetime) -> None:
    """`ck_tax_policy_versions_period`: an empty period only on a RETIRED version, and never an
    inverted one."""
    version = db.draft()
    db.publish(version)

    refused = db.refuses(lambda: db.set_end(version, end))

    assert violates(refused, "ck_tax_policy_versions_period"), refused
    assert db.period(version) == ("PUBLISHED", EFFECTIVE_FROM, None)


def test_discarded_is_refused_by_the_triggers(db: Db) -> None:
    """v3: there is no `DISCARDED`. The BEFORE INSERT trigger and rule 2 of BEFORE UPDATE refuse
    it before `ck_tax_policy_versions_status` is evaluated, so on MySQL that check is not
    reachable through DML (its text is compared with the model in `test_migrations.py`)."""
    params = db.version_params(status="DISCARDED")
    assert db.refused(_INSERT_VERSION, **params) == (_ER_SIGNAL_EXCEPTION, _STARTS_AS_DRAFT)

    draft = db.draft()
    refused = db.refused(_update("status = 'DISCARDED'"), id=draft)
    assert refused == (_ER_SIGNAL_EXCEPTION, _DRAFT_TRANSITION)
    assert db.period(draft) == ("DRAFT", None, None)


# --- open_slot and the unique index (design §7「open_slot 与唯一索引」) ----------------------


def test_open_slot_and_the_unique_index(db: Db) -> None:
    """With A published and open, publishing draft B `[later, NULL)` is refused (the overlap
    trigger fires before the unique index, so only the refusal is asserted). A draft and a
    truncated version have `open_slot` NULL, the open one 1."""
    a = db.draft()
    db.publish(a, EFFECTIVE_FROM)
    b = db.draft()
    assert (db.open_slot(a), db.open_slot(b)) == (1, None)

    before = db.snapshot()
    refused = db.refuses(lambda: db.publish(b, D2))
    assert refused[0] in (_ER_SIGNAL_EXCEPTION, _ER_DUP_ENTRY), refused
    assert db.snapshot() == before

    db.set_end(a, D2)
    db.publish(b, D2)
    assert (db.open_slot(a), db.open_slot(b)) == (None, 1)


# --- no overlapping periods (design §7「区间不重叠」) -----------------------------------------


@pytest.mark.parametrize(
    ("end_of_a", "accepted"),
    [(None, False), (D2, True), (D3, False)],
    ids=["not-truncated", "end-to-end", "intersecting"],
)
def test_a_period_that_meets_another_is_refused(
    db: Db, end_of_a: dt.datetime | None, accepted: bool
) -> None:
    """A `[d1, end)` is published; B is published directly from d2 > d1. Only when A ends at d2
    (end to end) does it pass."""
    a = db.draft()
    db.publish(a, EFFECTIVE_FROM)
    if end_of_a is not None:
        db.set_end(a, end_of_a)
    b = db.draft()

    if accepted:
        db.publish(b, D2)
        assert db.period(b) == ("PUBLISHED", D2, None)
    else:
        refused = db.refuses(lambda: db.publish(b, D2))
        assert refused == (_ER_SIGNAL_EXCEPTION, _OVERLAP)
        assert db.period(b) == ("DRAFT", None, None)
    assert db.period(a) == ("PUBLISHED", EFFECTIVE_FROM, end_of_a)


# --- the trigger takes the lock (design §7「触发器拿锁」) ------------------------------------


def test_the_overlap_check_takes_the_tax_policy_lock(engine: Engine) -> None:
    """The trigger locks `tax_policy_locks` before it looks, so a direct write is serialised with
    the service's publishing. While another transaction holds the lock, a direct publish waits
    (and times out here); editing a draft does not touch the lock. Once the holder commits, the
    same publish passes."""
    writers = _rolled_back(engine)
    writer = next(writers)
    try:
        with engine.connect() as holder:
            holder.begin()
            assert holder.execute(_LOCK_FOR_UPDATE).scalars().all() == [1]
            writer.execute(text("SET SESSION innodb_lock_wait_timeout = 1"))
            draft = writer.draft()

            writer.execute(_update("source_reference = :value"), id=draft, value="edited")
            refused = writer.refuses(lambda: writer.publish(draft))

            assert refused[0] == _ER_LOCK_WAIT_TIMEOUT, refused
            assert writer.period(draft) == ("DRAFT", None, None)
            holder.commit()

        writer.publish(draft)
        assert writer.period(draft) == ("PUBLISHED", EFFECTIVE_FROM, None)
    finally:
        writers.close()


# --- foreign keys (design §7「外键」) -------------------------------------------------------


@pytest.mark.parametrize(
    ("referenced", "constraint"),
    [
        ("creator", "fk_tax_policy_versions_created_by"),
        ("approver", "fk_tax_policy_versions_approved_by"),
    ],
    ids=["created-by", "approved-by"],
)
def test_a_referenced_user_is_not_deleted(db: Db, referenced: str, constraint: str) -> None:
    """`created_by` / `approved_by` are RESTRICT: the user behind a version stays (design §6). A
    user no version refers to is deleted."""
    version = db.draft(creator=db.admin)
    db.publish(version, approver=db.other)
    user = db.admin if referenced == "creator" else db.other

    errno, message = db.refused(_DELETE_USER, id=user)

    assert errno == _ER_ROW_IS_REFERENCED_2
    assert constraint in message
    spare = db.user(SPARE_EMAIL)
    db.execute(_DELETE_USER, id=spare)

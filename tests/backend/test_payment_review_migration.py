"""0037_payment_review（AIH-TASK-179，设计闸门 #585 v3）.

Design §2 "数据库", §3 INV-3 / INV-13, §4 (the "已结案" row), §7 (the "迁移与 CHECK" row, task
179), §8 (the migration item).

The first cases need no database: the revision chain; the four columns, the index, the foreign
key and the two checks in the migration equal the model's and the design's, under the same
names and in the same order (after 0036's); `resolution` is a non-native enum of exactly
`CREDITED` / `CLOSED`; the one `ALTER TABLE` the upgrade sends is the design's; every branch of
the upgrade (no new column yet / any of them already there — refused naming it, no DDL /
`--sql` / not MySQL) and of the downgrade (`--sql` refused / a resolved row refused, no DDL /
otherwise the two checks, the foreign key, the index, the four columns, each only while it is
there), with `alembic.op` and the inspector stood in. `create_all` on SQLite (what the unit tests
run) builds the table whose `ck_payments_resolution_fields` refuses every illegal combination and
takes the two legal ones, and refuses moving a resolved row away from its status.

On a real MySQL (`BILLING_TEST_DATABASE_URL`; skipped when unset, never skipped in CI): the
0036 → 0037 → 0036 → 0037 round trip (one `ALTER TABLE` up; checks, key, index, columns down;
every other table, key and check and the wallet and ledger rows unchanged), a new column already
there (each one, and all four — refused, no DDL, version stays 0036), the downgrade with a
resolved row (refused, no DDL, the row kept) and with unresolved rows only (done, rows kept), the
`--sql` upgrade and downgrade, and the checks on the same combinations. The Alembic chain does
not run on SQLite (0006 onwards ask for `@@log_bin`), so every Alembic scenario is MySQL's, as in
test_payments_migration.py. Captured statements are compared after normalising (backticks, case,
spaces). No gateway is called: this task has no service.
"""

from __future__ import annotations

import importlib.util
import io
import os
import pathlib
import re
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from types import ModuleType, SimpleNamespace

import pytest
import sqlalchemy
from alembic.config import Config
from sqlalchemy import (
    CheckConstraint,
    ForeignKeyConstraint,
    UniqueConstraint,
    create_engine,
    event,
    inspect,
    text,
)
from sqlalchemy.dialects import mysql
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError

from alembic import command
from app.models.base import Base
from app.models.payments import Payment, PaymentResolution

TEST_DATABASE_URL = os.environ.get("BILLING_TEST_DATABASE_URL", "")

needs_mysql = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="BILLING_TEST_DATABASE_URL is not set; migration tests need a real MySQL",
)

_REVISION = "0037_payment_review"
_DOWN_REVISION = "0036_payments"
_MIGRATION = pathlib.Path("alembic/versions/20261107_0037_payment_review.py")
_MIGRATION_0036 = pathlib.Path("alembic/versions/20261106_0036_payments.py")
_PAYMENTS = "payments"
_EVENTS = "payment_gateway_events"

# Design §2 "数据库": the four columns, in order, all nullable.
_DESIGN_COLUMNS = {
    "resolution": ("VARCHAR(16)", True),
    "resolved_at": ("DATETIME", True),
    "resolved_by_user_id": ("BIGINT", True),
    "resolution_reason": ("VARCHAR(255)", True),
}
_DESIGN_INDEXES = {
    "ix_payments_resolved_by_user_id": (("resolved_by_user_id",), False),
}
_DESIGN_FOREIGN_KEYS = {
    "fk_payments_resolved_by_user_id": (("resolved_by_user_id",), "users", ("id",), "RESTRICT"),
}
_RESOLUTIONS = ("CREDITED", "CLOSED")
# The design's expression, word for word (whitespace aside).
_DESIGN_CHECKS = {
    "ck_payments_resolution": "resolution IS NULL OR resolution IN ('CREDITED', 'CLOSED')",
    "ck_payments_resolution_fields": (
        "(resolution IS NULL AND resolved_at IS NULL AND resolved_by_user_id IS NULL"
        " AND resolution_reason IS NULL)"
        " OR (resolution IS NOT NULL AND resolved_at IS NOT NULL"
        " AND resolved_by_user_id IS NOT NULL"
        "     AND resolution_reason IS NOT NULL AND review_required_at IS NOT NULL"
        "     AND ((resolution = 'CREDITED' AND status = 'PAID')"
        " OR (resolution = 'CLOSED' AND status = 'FAILED')))"
    ),
}
_OURS = {*_DESIGN_COLUMNS, *_DESIGN_INDEXES, *_DESIGN_FOREIGN_KEYS, *_DESIGN_CHECKS}

# The design's one statement, clause by clause.
_ALTER_CLAUSES = [
    "ADD COLUMN resolution VARCHAR(16) NULL",
    "ADD COLUMN resolved_at DATETIME NULL",
    "ADD COLUMN resolved_by_user_id BIGINT NULL",
    "ADD COLUMN resolution_reason VARCHAR(255) NULL",
    "ADD INDEX ix_payments_resolved_by_user_id (resolved_by_user_id)",
    "ADD CONSTRAINT fk_payments_resolved_by_user_id FOREIGN KEY (resolved_by_user_id)"
    " REFERENCES users (id) ON DELETE RESTRICT",
    *(f"ADD CONSTRAINT {name} CHECK ({check})" for name, check in _DESIGN_CHECKS.items()),
]
_DESIGN_ALTER = f"ALTER TABLE {_PAYMENTS} {', '.join(_ALTER_CLAUSES)}"

# Design §8: checks, then the key, then its index, then the columns.
_DESIGN_DOWNGRADE = [
    "ALTER TABLE PAYMENTS DROP CHECK CK_PAYMENTS_RESOLUTION_FIELDS",
    "ALTER TABLE PAYMENTS DROP CHECK CK_PAYMENTS_RESOLUTION",
    "ALTER TABLE PAYMENTS DROP FOREIGN KEY FK_PAYMENTS_RESOLVED_BY_USER_ID",
    "DROP INDEX IX_PAYMENTS_RESOLVED_BY_USER_ID ON PAYMENTS",
    "ALTER TABLE PAYMENTS DROP COLUMN RESOLUTION_REASON",
    "ALTER TABLE PAYMENTS DROP COLUMN RESOLVED_BY_USER_ID",
    "ALTER TABLE PAYMENTS DROP COLUMN RESOLVED_AT",
    "ALTER TABLE PAYMENTS DROP COLUMN RESOLUTION",
]

# MySQL reports a violated CHECK as 3819 (ER_CHECK_CONSTRAINT_VIOLATED).
_ER_CHECK_CONSTRAINT_VIOLATED = 3819

_NOW = "2026-11-07 08:00:00"
_EXPIRES = "2026-11-07 09:00:00"
_LATER = "2026-11-07 10:00:00"

_ALEMBIC_VERSION = text("SELECT version_num FROM alembic_version")
_CHECKS_QUERY = text(
    "SELECT TABLE_NAME, CONSTRAINT_NAME FROM information_schema.TABLE_CONSTRAINTS"
    " WHERE CONSTRAINT_SCHEMA = DATABASE() AND CONSTRAINT_TYPE = 'CHECK'"
)
_DELETE_RULES_QUERY = text(
    "SELECT CONSTRAINT_NAME, DELETE_RULE FROM information_schema.REFERENTIAL_CONSTRAINTS"
    " WHERE CONSTRAINT_SCHEMA = DATABASE() AND TABLE_NAME = :table"
)
_WALLET_ROWS = text("SELECT * FROM wallets ORDER BY id")
_LEDGER_ROWS = text("SELECT * FROM wallet_transactions ORDER BY id")
_COUNT = text(f"SELECT COUNT(*) FROM {_PAYMENTS}")


def _payment_params(overrides: dict[str, object]) -> dict[str, object]:
    """A legal PENDING payment of RM100.00, not resolved, with `overrides` on top."""
    params: dict[str, object] = {
        "public_id": str(uuid.uuid4()),
        "tenant_id": 1,
        "gateway": "STRIPE",
        "expected_amount": "100.00",
        "expected_currency": "MYR",
        "wallet_credit_amount": "100.00",
        "tax_policy_version_id": 1,
        "tax_amount": None,
        "confirmed_amount": None,
        "confirmed_currency": None,
        "gateway_payment_id": None,
        "gateway_payment_intent_id": None,
        "gateway_fee": None,
        "status": "PENDING",
        "status_reason": None,
        "review_required_at": None,
        "expires_at": _EXPIRES,
        "paid_at": None,
        "last_reconciled_at": None,
        "created_at": _NOW,
        "updated_at": _NOW,
        "resolution": None,
        "resolved_at": None,
        "resolved_by_user_id": None,
        "resolution_reason": None,
    }
    params.update(overrides)
    return params


# Raw SQL: the values go past the enums, straight to the database's checks.
_INSERT_COLUMNS = list(_payment_params({}))
_INSERT_PAYMENT = text(
    f"INSERT INTO {_PAYMENTS} ({', '.join(_INSERT_COLUMNS)})"
    f" VALUES ({', '.join(f':{name}' for name in _INSERT_COLUMNS)})"
)


def _insert(connection: sqlalchemy.Connection, overrides: dict[str, object]) -> int:
    result = connection.execute(_INSERT_PAYMENT, _payment_params(overrides))
    return int(result.lastrowid)


# A payment that went to review (amount mismatch), and the four resolution fields.
_REVIEW = {
    "status_reason": "AMOUNT_MISMATCH",
    "review_required_at": _NOW,
    "confirmed_amount": "99.99",
    "confirmed_currency": "MYR",
}
_FIELDS = {
    "resolved_at": _LATER,
    "resolved_by_user_id": 1,
    "resolution_reason": "checked in the Stripe Dashboard",
}
_CREDITED = {**_REVIEW, **_FIELDS, "resolution": "CREDITED", "status": "PAID", "paid_at": _LATER}
_CLOSED = {**_REVIEW, **_FIELDS, "resolution": "CLOSED", "status": "FAILED"}

_FIELDS_CHECK = {"ck_payments_resolution_fields"}

# Illegal rows: overrides on a legal row → the checks that may name the refusal.
_ILLEGAL_ROWS: dict[str, tuple[dict[str, object], set[str]]] = {
    # The four columns half empty.
    "resolution alone": ({**_REVIEW, "status": "FAILED", "resolution": "CLOSED"}, _FIELDS_CHECK),
    "resolved_at alone": ({"resolved_at": _LATER}, _FIELDS_CHECK),
    "resolved_by_user_id alone": ({"resolved_by_user_id": 1}, _FIELDS_CHECK),
    "resolution_reason alone": ({"resolution_reason": "no"}, _FIELDS_CHECK),
    "CLOSED without resolved_at": ({**_CLOSED, "resolved_at": None}, _FIELDS_CHECK),
    "CLOSED without resolved_by_user_id": (
        {**_CLOSED, "resolved_by_user_id": None},
        _FIELDS_CHECK,
    ),
    "CREDITED without resolution_reason": (
        {**_CREDITED, "resolution_reason": None},
        _FIELDS_CHECK,
    ),
    # Resolution and status do not match.
    "CREDITED with FAILED": ({**_CREDITED, "status": "FAILED"}, _FIELDS_CHECK),
    "CLOSED with PAID": ({**_CLOSED, "status": "PAID", "paid_at": _LATER}, _FIELDS_CHECK),
    "CLOSED with REVIEW_REQUIRED": ({**_CLOSED, "status": "REVIEW_REQUIRED"}, _FIELDS_CHECK),
    "CREDITED with PENDING": ({**_CREDITED, "status": "PENDING"}, _FIELDS_CHECK),
    # Never went to review.
    "CLOSED without review_required_at": (
        {**_CLOSED, "review_required_at": None},
        _FIELDS_CHECK,
    ),
    "CREDITED without review_required_at": (
        {**_CREDITED, "review_required_at": None},
        _FIELDS_CHECK,
    ),
    # Breaks both checks; either may be the one named.
    "an unknown resolution": (
        {**_CLOSED, "resolution": "REFUNDED"},
        {"ck_payments_resolution", "ck_payments_resolution_fields"},
    ),
}

# The two legal resolved rows, next to unresolved rows of every status.
_LEGAL_ROWS: dict[str, dict[str, object]] = {
    "CREDITED with PAID": _CREDITED,
    "CLOSED with FAILED": _CLOSED,
    "PENDING, not resolved": {},
    "REVIEW_REQUIRED, not resolved": {**_REVIEW, "status": "REVIEW_REQUIRED"},
    "PAID, not resolved": {
        "status": "PAID",
        "paid_at": _NOW,
        "confirmed_amount": "100.00",
        "confirmed_currency": "MYR",
    },
    "FAILED, not resolved": {"status": "FAILED", "status_reason": "GATEWAY_FAILED"},
    "EXPIRED, not resolved": {"status": "EXPIRED", "status_reason": "NO_GATEWAY_SESSION"},
    "CANCELLED, not resolved": {"status": "CANCELLED"},
}

# Design §2 / §4: a resolved row is final. (legal row, UPDATE on it.)
_FINAL_UPDATES: dict[str, tuple[dict[str, object], str]] = {
    "CLOSED to PAID": (
        _CLOSED,
        f"UPDATE {_PAYMENTS} SET status = 'PAID', paid_at = '{_LATER}' WHERE id = :id",
    ),
    "CLOSED to REVIEW_REQUIRED": (
        _CLOSED,
        f"UPDATE {_PAYMENTS} SET status = 'REVIEW_REQUIRED' WHERE id = :id",
    ),
    "CREDITED to FAILED": (_CREDITED, f"UPDATE {_PAYMENTS} SET status = 'FAILED' WHERE id = :id"),
    "CLOSED losing its reason": (
        _CLOSED,
        f"UPDATE {_PAYMENTS} SET resolution_reason = NULL WHERE id = :id",
    ),
}


def _load(path: pathlib.Path = _MIGRATION, name: str = "migration_0037") -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _normalised(statement: str) -> str:
    return re.sub(r"\s+", " ", statement.replace("`", "").replace('"', "")).strip().upper()


def _compact(statement: str) -> str:
    """Without any whitespace: for comparing DDL written over several lines."""
    return re.sub(r"\s+", "", statement.replace("`", "")).upper()


def _type_text(column_type: object) -> str:
    """Type and width only, as the migration writes them."""
    return str(column_type).replace(", ", ",").split(" ")[0].upper()


def _model_type_text(column: sqlalchemy.Column) -> str:
    # The generic `Numeric` compiles to NUMERIC on MySQL, which stores (and reflects) DECIMAL.
    compiled = column.type.compile(dialect=mysql.dialect())
    return _type_text(compiled).replace("NUMERIC", "DECIMAL")


def _model_shape() -> dict[str, object]:
    """`Payment`'s columns, indexes, foreign keys and check names (head's shape)."""
    table = Payment.__table__
    indexes: dict[str, tuple[tuple[str, ...], bool]] = {}
    checks: set[str] = set()
    for constraint in table.constraints:
        if isinstance(constraint, UniqueConstraint):
            indexes[str(constraint.name)] = (tuple(c.name for c in constraint.columns), True)
        elif isinstance(constraint, CheckConstraint):
            checks.add(str(constraint.name))
    for index in table.indexes:
        indexes[str(index.name)] = (tuple(c.name for c in index.columns), bool(index.unique))
    foreign_keys = {
        str(key.name): (
            tuple(key.column_keys),
            key.referred_table.name,
            tuple(element.column.name for element in key.elements),
            str(key.ondelete).upper(),
        )
        for key in table.foreign_key_constraints
    }
    return {
        "columns": {c.name: (_model_type_text(c), bool(c.nullable)) for c in table.columns},
        "indexes": indexes,
        "foreign_keys": foreign_keys,
        "checks": checks,
    }


def _head_design() -> dict[str, object]:
    """`payments` after 0037: 0036's frozen shape, then this design's."""
    at_0036 = _load(_MIGRATION_0036, "migration_0036")
    return {
        "columns": {**at_0036._PAYMENT_COLUMNS, **_DESIGN_COLUMNS},
        "indexes": {**at_0036._PAYMENT_INDEXES, **_DESIGN_INDEXES},
        "foreign_keys": {**at_0036._PAYMENT_FOREIGN_KEYS, **_DESIGN_FOREIGN_KEYS},
        "checks": {*at_0036._PAYMENT_CHECKS, *_DESIGN_CHECKS},
    }


# ---------------------------------------------------------------------------
# No database
# ---------------------------------------------------------------------------


def test_0037_follows_0036() -> None:
    migration = _load()

    assert migration.revision == _REVISION
    assert migration.down_revision == _DOWN_REVISION
    # `alembic_version.version_num` is VARCHAR(32).
    assert len(migration.revision) <= 32


def test_0037_shape_is_the_design_and_the_model() -> None:
    """The migration's frozen literals, the model's declarations and design §2 agree: the four
    columns (type, width, nullable, in this order, after 0036's), the index, the foreign key
    RESTRICT to users, the checks by name; the model's whole table is 0036's plus these."""
    migration = _load()

    assert migration._COLUMNS == _DESIGN_COLUMNS
    assert list(migration._COLUMNS) == list(_DESIGN_COLUMNS)
    assert migration._INDEXES == _DESIGN_INDEXES
    assert migration._FOREIGN_KEYS == _DESIGN_FOREIGN_KEYS
    assert list(migration._CHECKS) == list(_DESIGN_CHECKS)
    assert _model_shape() == _head_design()

    at_0036 = _load(_MIGRATION_0036, "migration_0036")
    assert [c.name for c in Payment.__table__.columns] == [
        *at_0036._PAYMENT_COLUMNS,
        *_DESIGN_COLUMNS,
    ]


def test_0037_table_args_end_with_ours_in_the_migrations_order() -> None:
    """`__table_args__` declares 0037's index, foreign key and checks last, under the migration's
    names and in its order."""
    migration = _load()
    kinds = UniqueConstraint | sqlalchemy.Index | ForeignKeyConstraint | CheckConstraint
    declared = [str(arg.name) for arg in Payment.__table_args__ if isinstance(arg, kinds)]
    ours = [*migration._INDEXES, *migration._FOREIGN_KEYS, *migration._CHECKS]

    assert declared[-len(ours) :] == ours
    assert not set(declared[: -len(ours)]) & _OURS


def test_0037_checks_are_the_designs_and_the_models() -> None:
    """Same conditions in the migration, on the model and in the design (whitespace aside)."""
    migration = _load()
    model = {
        str(constraint.name): _normalised(str(constraint.sqltext))
        for constraint in Payment.__table__.constraints
        if isinstance(constraint, CheckConstraint) and constraint.name in _DESIGN_CHECKS
    }

    assert {name: _normalised(c) for name, c in migration._CHECKS.items()} == model
    assert {name: _normalised(c) for name, c in _DESIGN_CHECKS.items()} == model


def test_0037_resolution_is_a_non_native_enum_and_the_check_allows_exactly_it() -> None:
    assert [m.value for m in PaymentResolution] == list(_RESOLUTIONS)
    kind = Payment.__table__.c.resolution.type
    assert isinstance(kind, sqlalchemy.Enum)
    assert kind.native_enum is False
    assert kind.enum_class is PaymentResolution
    assert kind.length == 16

    migration = _load()
    allowed = ", ".join(f"'{m.value}'" for m in PaymentResolution)
    assert _normalised(migration._CHECKS["ck_payments_resolution"]) == _normalised(
        f"resolution IS NULL OR resolution IN ({allowed})"
    )


def test_0037_upgrade_is_the_design_one_alter_table() -> None:
    migration = _load()

    assert _compact(migration._ALTER_UPGRADE) == _compact(_DESIGN_ALTER)
    assert migration._ALTER_UPGRADE.count(";") == 0
    assert _normalised(migration._ALTER_UPGRADE).count("ALTER TABLE") == 1


class _Result:
    def __init__(self, rows: list[tuple[object, ...]]) -> None:
        self._rows = rows

    def first(self) -> tuple[object, ...] | None:
        return self._rows[0] if self._rows else None


class _FakeBind:
    """Answers "any resolved row?"."""

    def __init__(self, resolved: bool) -> None:
        self._resolved = resolved
        self.queries: list[str] = []

    def execute(self, statement: object) -> _Result:
        sql = str(statement)
        self.queries.append(sql)
        assert sql == f"SELECT 1 FROM {_PAYMENTS} WHERE resolution IS NOT NULL LIMIT 1", sql
        return _Result([(1,)] if self._resolved else [])


class _FakeInspector:
    """`payments` with 0036's columns and those of ours named in `present`."""

    def __init__(self, present: set[str]) -> None:
        self._present = present
        self.asked: list[str] = []

    def _named(self, method: str, table: str, names: dict) -> list[dict[str, str]]:
        assert table == _PAYMENTS, table
        self.asked.append(method)
        return [{"name": name} for name in names if name in self._present]

    def get_columns(self, table: str) -> list[dict[str, str]]:
        theirs = [{"name": "id"}, {"name": "status"}, {"name": "review_required_at"}]
        return theirs + self._named("get_columns", table, _DESIGN_COLUMNS)

    def get_check_constraints(self, table: str) -> list[dict[str, str]]:
        theirs = [{"name": "ck_payments_status"}]
        return theirs + self._named("get_check_constraints", table, _DESIGN_CHECKS)

    def get_foreign_keys(self, table: str) -> list[dict[str, str]]:
        theirs = [{"name": "fk_payments_tenant_id"}]
        return theirs + self._named("get_foreign_keys", table, _DESIGN_FOREIGN_KEYS)

    def get_indexes(self, table: str) -> list[dict[str, str]]:
        theirs = [{"name": "ix_payments_tenant_created"}]
        return theirs + self._named("get_indexes", table, _DESIGN_INDEXES)


class _Sa:
    """`sqlalchemy` with `inspect` stood in; everything else is the real module."""

    def __init__(self, inspector: _FakeInspector) -> None:
        self._inspector = inspector

    def inspect(self, _bind: object) -> _FakeInspector:
        return self._inspector

    def __getattr__(self, name: str) -> object:
        return getattr(sqlalchemy, name)


class _FakeOp:
    """Stands in for `alembic.op`: a dialect, online or `--sql`, and the calls it got."""

    def __init__(self, bind: _FakeBind, dialect: str, as_sql: bool) -> None:
        self.calls: list[tuple[str, tuple[object, ...]]] = []
        self._bind = bind
        self._context = SimpleNamespace(as_sql=as_sql, dialect=SimpleNamespace(name=dialect))

    def get_context(self) -> SimpleNamespace:
        return self._context

    def get_bind(self) -> _FakeBind:
        return self._bind

    def execute(self, statement: str) -> None:
        self.calls.append(("execute", (statement,)))

    def drop_constraint(self, name: str, table: str, type_: str) -> None:
        self.calls.append(("drop_constraint", (name, table, type_)))

    def drop_index(self, name: str, table_name: str) -> None:
        self.calls.append(("drop_index", (name, table_name)))

    def drop_column(self, table: str, name: str) -> None:
        self.calls.append(("drop_column", (table, name)))


def _with(
    monkeypatch,
    present: set[str],
    *,
    dialect: str = "mysql",
    as_sql: bool = False,
    resolved: bool = False,
) -> tuple[ModuleType, _FakeOp, _FakeInspector, _FakeBind]:
    migration = _load()
    bind = _FakeBind(resolved)
    fake = _FakeOp(bind, dialect=dialect, as_sql=as_sql)
    inspector = _FakeInspector(present)
    monkeypatch.setattr(migration, "op", fake)
    monkeypatch.setattr(migration, "sa", _Sa(inspector))
    return migration, fake, inspector, bind


def test_0037_upgrade_sends_one_alter_table(monkeypatch) -> None:
    migration, fake, inspector, bind = _with(monkeypatch, set())
    migration.upgrade()

    assert fake.calls == [("execute", (migration._ALTER_UPGRADE,))]
    assert inspector.asked == ["get_columns"]
    assert bind.queries == []


_PRESENT_CASES = {
    **{name: {name} for name in _DESIGN_COLUMNS},
    "all four": set(_DESIGN_COLUMNS),
}


@pytest.mark.parametrize("case", list(_PRESENT_CASES))
def test_0037_upgrade_refuses_a_column_already_there_before_any_ddl(monkeypatch, case: str) -> None:
    """Design §8: any new column already there — refuse, no DDL; the message names the columns
    that are there and not the others."""
    present = _PRESENT_CASES[case]
    migration, fake, _, bind = _with(monkeypatch, present)

    with pytest.raises(RuntimeError) as raised:
        migration.upgrade()

    assert fake.calls == []
    assert bind.queries == []
    message = str(raised.value)
    for name in _DESIGN_COLUMNS:
        assert (f"column {_PAYMENTS}.{name} already exists" in message) == (name in present), name
    assert "No DDL was issued" in message


def test_0037_offline_upgrade_emits_the_alter_without_reading(monkeypatch) -> None:
    migration, fake, inspector, bind = _with(monkeypatch, set(_DESIGN_COLUMNS), as_sql=True)
    migration.upgrade()

    assert inspector.asked == []
    assert bind.queries == []
    assert fake.calls == [("execute", (migration._ALTER_UPGRADE,))]


@pytest.mark.parametrize("as_sql", [False, True])
@pytest.mark.parametrize("step", ["upgrade", "downgrade"])
def test_0037_refuses_other_dialects_before_reading_or_ddl(
    monkeypatch, step: str, as_sql: bool
) -> None:
    migration, fake, inspector, bind = _with(monkeypatch, set(), dialect="sqlite", as_sql=as_sql)

    with pytest.raises(RuntimeError) as raised:
        getattr(migration, step)()

    assert fake.calls == []
    assert inspector.asked == []
    assert bind.queries == []
    assert "only runs on MySQL" in str(raised.value)


def test_0037_offline_downgrade_refuses_before_reading_or_ddl(monkeypatch) -> None:
    """Design §8: offline it cannot tell whether a payment is resolved — refuse, no DDL."""
    migration, fake, inspector, bind = _with(monkeypatch, _OURS, as_sql=True)

    with pytest.raises(RuntimeError) as raised:
        migration.downgrade()

    assert fake.calls == []
    assert inspector.asked == []
    assert bind.queries == []
    message = str(raised.value)
    assert _PAYMENTS in message
    assert "online" in message


def test_0037_downgrade_refuses_a_resolved_row_before_any_ddl(monkeypatch) -> None:
    migration, fake, _, bind = _with(monkeypatch, _OURS, resolved=True)

    with pytest.raises(RuntimeError) as raised:
        migration.downgrade()

    assert fake.calls == []
    assert bind.queries == [migration._RESOLVED_ROWS]
    message = str(raised.value)
    assert "holds resolved payments" in message
    assert "No DDL was issued and no row was deleted" in message


_DROPS = [
    ("execute", (f"ALTER TABLE {_PAYMENTS} DROP CHECK ck_payments_resolution_fields",)),
    ("execute", (f"ALTER TABLE {_PAYMENTS} DROP CHECK ck_payments_resolution",)),
    ("drop_constraint", ("fk_payments_resolved_by_user_id", _PAYMENTS, "foreignkey")),
    ("drop_index", ("ix_payments_resolved_by_user_id", _PAYMENTS)),
    ("drop_column", (_PAYMENTS, "resolution_reason")),
    ("drop_column", (_PAYMENTS, "resolved_by_user_id")),
    ("drop_column", (_PAYMENTS, "resolved_at")),
    ("drop_column", (_PAYMENTS, "resolution")),
]


def test_0037_downgrade_drops_checks_key_index_then_columns(monkeypatch) -> None:
    """Design §8: no resolved row — the two checks, the foreign key, then its index (MySQL 1553),
    then the four columns."""
    migration, fake, _, bind = _with(monkeypatch, _OURS)
    migration.downgrade()

    # Checked before the first DDL.
    assert bind.queries == [migration._RESOLVED_ROWS]
    assert fake.calls == _DROPS


def test_0037_downgrade_rerun_drops_only_what_is_left(monkeypatch) -> None:
    """A downgrade stopped after the key: the rerun drops the index and the columns still
    there, nothing else."""
    left = {"ix_payments_resolved_by_user_id", "resolved_at", "resolution"}
    migration, fake, _, bind = _with(monkeypatch, left)
    migration.downgrade()

    assert bind.queries == [migration._RESOLVED_ROWS]
    assert fake.calls == [
        ("drop_index", ("ix_payments_resolved_by_user_id", _PAYMENTS)),
        ("drop_column", (_PAYMENTS, "resolved_at")),
        ("drop_column", (_PAYMENTS, "resolution")),
    ]


def test_0037_downgrade_with_nothing_left_does_nothing(monkeypatch) -> None:
    migration, fake, _, bind = _with(monkeypatch, set())
    migration.downgrade()

    assert bind.queries == []
    assert fake.calls == []


# ---------------------------------------------------------------------------
# SQLite
# ---------------------------------------------------------------------------


@contextmanager
def _sqlite() -> Iterator[Engine]:
    """The unit tests build SQLite with `create_all` from the models, not with the migrations.
    SQLite does not enforce foreign keys by default: the parent tables are not needed."""
    engine = create_engine("sqlite+pysqlite:///:memory:")
    try:
        Base.metadata.create_all(engine, tables=[Payment.__table__])
        yield engine
    finally:
        engine.dispose()


def test_0037_sqlite_table_gets_the_columns_index_and_foreign_key() -> None:
    with _sqlite() as engine:
        inspector = inspect(engine)
        columns = {str(c["name"]): bool(c["nullable"]) for c in inspector.get_columns(_PAYMENTS)}
        indexes = {
            str(i["name"]): (tuple(i["column_names"]), bool(i["unique"]))
            for i in inspector.get_indexes(_PAYMENTS)
        }
        keys = {
            str(k["name"]): (
                tuple(k["constrained_columns"]),
                str(k["referred_table"]),
                tuple(k["referred_columns"]),
                str(k["options"].get("ondelete")).upper(),
            )
            for k in inspector.get_foreign_keys(_PAYMENTS)
        }

    assert {name: columns[name] for name in _DESIGN_COLUMNS} == {
        name: nullable for name, (_, nullable) in _DESIGN_COLUMNS.items()
    }
    assert {name: indexes[name] for name in _DESIGN_INDEXES} == _DESIGN_INDEXES
    assert {name: keys[name] for name in _DESIGN_FOREIGN_KEYS} == _DESIGN_FOREIGN_KEYS


def test_0037_sqlite_takes_the_legal_rows() -> None:
    with _sqlite() as engine:
        with engine.begin() as connection:
            for overrides in _LEGAL_ROWS.values():
                _insert(connection, overrides)
            count = int(connection.execute(_COUNT).scalar_one())

    assert count == len(_LEGAL_ROWS)


@pytest.mark.parametrize("row", list(_ILLEGAL_ROWS))
def test_0037_sqlite_checks_refuse_the_illegal_rows(row: str) -> None:
    overrides, _ = _ILLEGAL_ROWS[row]
    with _sqlite() as engine:
        with pytest.raises(DBAPIError) as raised:
            with engine.begin() as connection:
                _insert(connection, overrides)
        assert "CHECK constraint failed" in str(raised.value), row

        with engine.connect() as connection:
            assert int(connection.execute(_COUNT).scalar_one()) == 0


@pytest.mark.parametrize("case", list(_FINAL_UPDATES))
def test_0037_sqlite_a_resolved_row_is_final(case: str) -> None:
    legal, update = _FINAL_UPDATES[case]
    with _sqlite() as engine:
        with engine.begin() as connection:
            payment_id = _insert(connection, legal)
            before = connection.execute(
                text(f"SELECT * FROM {_PAYMENTS} WHERE id = :id"), {"id": payment_id}
            ).one()
        with pytest.raises(DBAPIError) as raised:
            with engine.begin() as connection:
                connection.execute(text(update), {"id": payment_id})
        assert "CHECK constraint failed" in str(raised.value), case

        with engine.connect() as connection:
            after = connection.execute(
                text(f"SELECT * FROM {_PAYMENTS} WHERE id = :id"), {"id": payment_id}
            ).one()
        assert tuple(after) == tuple(before)


# ---------------------------------------------------------------------------
# Real MySQL
# ---------------------------------------------------------------------------


@pytest.fixture
def alembic_config(monkeypatch) -> Iterator[Config]:
    # env.py 从 app.core.config 读连接串，而 get_settings() 带 lru_cache，所以要清掉缓存再指过去。
    from app.core.config import get_settings

    monkeypatch.setenv("BILLING_DATABASE_URL", TEST_DATABASE_URL)
    get_settings.cache_clear()
    yield Config("alembic.ini")
    get_settings.cache_clear()


@pytest.fixture
def engine() -> Iterator[Engine]:
    engine = create_engine(TEST_DATABASE_URL)
    try:
        yield engine
    finally:
        engine.dispose()


@contextmanager
def _ddl_statements() -> Iterator[list[str]]:
    """DDL that any engine (Alembic's own included) sends while the block runs."""
    statements: list[str] = []

    def record(_conn, _cursor, statement, _params, _context, _many) -> None:
        statements.append(statement)

    event.listen(Engine, "before_cursor_execute", record)
    try:
        yield statements
    finally:
        event.remove(Engine, "before_cursor_execute", record)
        statements[:] = [
            s for s in statements if _normalised(s).split(" ")[0] in {"ALTER", "CREATE", "DROP"}
        ]


def _version(engine: Engine) -> str:
    with engine.connect() as connection:
        return str(connection.execute(_ALEMBIC_VERSION).scalar_one())


def _execute(engine: Engine, statement: str, params: dict[str, object] | None = None) -> None:
    with engine.begin() as connection:
        connection.execute(text(statement), params or {})


def _our_columns(engine: Engine) -> set[str]:
    return {str(c["name"]) for c in inspect(engine).get_columns(_PAYMENTS)} & set(_DESIGN_COLUMNS)


def _table_shape(engine: Engine) -> dict[str, object]:
    """`payments` as reflected, in the migration's terms; delete rules from information_schema;
    prefix lengths kept so they would show."""
    inspector = inspect(engine)
    with engine.connect() as connection:
        rules = dict(connection.execute(_DELETE_RULES_QUERY, {"table": _PAYMENTS}).all())
    indexes: dict[str, object] = {}
    for index in inspector.get_indexes(_PAYMENTS):
        prefix = (index.get("dialect_options") or {}).get("mysql_length")
        shape = (tuple(index["column_names"]), bool(index["unique"]))
        indexes[str(index["name"])] = shape if not prefix else (*shape, repr(prefix))
    return {
        "columns": {
            str(c["name"]): (_type_text(c["type"]), bool(c["nullable"]))
            for c in inspector.get_columns(_PAYMENTS)
        },
        "indexes": indexes,
        "foreign_keys": {
            str(key["name"]): (
                tuple(key["constrained_columns"]),
                str(key["referred_table"]),
                tuple(key["referred_columns"]),
                rules.get(key["name"]),
            )
            for key in inspector.get_foreign_keys(_PAYMENTS)
        },
        "checks": {str(check["name"]) for check in inspector.get_check_constraints(_PAYMENTS)},
    }


def _assert_design_shape(engine: Engine) -> None:
    assert _table_shape(engine) == _model_shape() == _head_design()
    columns = [str(c["name"]) for c in inspect(engine).get_columns(_PAYMENTS)]
    assert columns == [c.name for c in Payment.__table__.columns]


def _schema_snapshot(engine: Engine) -> dict[str, object]:
    """Every table's columns, indexes (with prefix lengths), unique constraints and foreign
    keys, with every check in the schema."""
    inspector = inspect(engine)
    tables: dict[str, dict[str, object]] = {}
    for table in sorted(inspector.get_table_names()):
        tables[table] = {
            "columns": [
                (c["name"], str(c["type"]), c["nullable"]) for c in inspector.get_columns(table)
            ],
            "indexes": sorted(
                (
                    str(i["name"]),
                    tuple(i["column_names"]),
                    bool(i["unique"]),
                    repr((i.get("dialect_options") or {}).get("mysql_length")),
                )
                for i in inspector.get_indexes(table)
            ),
            "unique": sorted(
                (str(u["name"]), tuple(u["column_names"]))
                for u in inspector.get_unique_constraints(table)
            ),
            "foreign_keys": sorted(
                (str(fk["name"]), tuple(fk["constrained_columns"]), fk["referred_table"])
                for fk in inspector.get_foreign_keys(table)
            ),
        }
    with engine.connect() as connection:
        checks = {tuple(row) for row in connection.execute(_CHECKS_QUERY).all()}
    return {"tables": tables, "checks": checks}


def _without_ours(snapshot: dict[str, object]) -> dict[str, object]:
    """The snapshot with 0037's columns, index, key and checks taken out of `payments`."""
    tables = snapshot["tables"]
    assert isinstance(tables, dict)
    checks = snapshot["checks"]
    assert isinstance(checks, set)
    payments = {
        part: [item for item in items if str(item[0]) not in _OURS]
        for part, items in tables[_PAYMENTS].items()
    }
    return {
        "tables": {**tables, _PAYMENTS: payments},
        "checks": {check for check in checks if check[1] not in _OURS},
    }


def _ledger_and_wallets(engine: Engine) -> tuple[list[tuple[object, ...]], ...]:
    with engine.connect() as connection:
        wallets = [tuple(row) for row in connection.execute(_WALLET_ROWS).all()]
        ledger = [tuple(row) for row in connection.execute(_LEDGER_ROWS).all()]
    return wallets, ledger


def _payment_rows(engine: Engine) -> list[tuple[object, ...]]:
    """The rows by id, in 0036's columns (there at both versions)."""
    columns = [name for name in _INSERT_COLUMNS if name not in _DESIGN_COLUMNS]
    with engine.connect() as connection:
        return [
            tuple(row)
            for row in connection.execute(
                text(f"SELECT id, {', '.join(columns)} FROM {_PAYMENTS} ORDER BY id")
            ).all()
        ]


def _insert_committed(engine: Engine, rows: list[dict[str, object]]) -> None:
    """Payments written at 0037 with foreign key checks off (no tenant, tax policy version or
    user is needed for this)."""
    with engine.begin() as connection:
        connection.exec_driver_sql("SET FOREIGN_KEY_CHECKS = 0")
        try:
            for overrides in rows:
                _insert(connection, overrides)
        finally:
            connection.exec_driver_sql("SET FOREIGN_KEY_CHECKS = 1")


def _at_0037(alembic_config: Config, engine: Engine) -> None:
    """Head, then down to 0037: later revisions (0038 onwards) are not under observation."""
    command.upgrade(alembic_config, "head")
    command.downgrade(alembic_config, _REVISION)
    assert _version(engine) == _REVISION


def _at_0036(alembic_config: Config, engine: Engine) -> None:
    _at_0037(alembic_config, engine)
    command.downgrade(alembic_config, _DOWN_REVISION)
    assert _version(engine) == _DOWN_REVISION
    assert _our_columns(engine) == set()


def _back_to_head(alembic_config: Config, engine: Engine) -> None:
    """Empties `payments` (events first: they refer to it), drops hand-made columns left at
    0036, and upgrades to head again."""
    _execute(engine, f"DELETE FROM {_EVENTS}")
    _execute(engine, f"DELETE FROM {_PAYMENTS}")
    if _version(engine) == _DOWN_REVISION:
        for name in _our_columns(engine):
            _execute(engine, f"ALTER TABLE {_PAYMENTS} DROP COLUMN {name}")
    command.upgrade(alembic_config, "head")


@needs_mysql
def test_0037_round_trip(alembic_config: Config, engine: Engine) -> None:
    """Design §7 "迁移与 CHECK" / §8: 0036 → 0037 → 0036 → 0037. The upgrade sends one
    `ALTER TABLE`, the design's; the shape is the model's and the design's; the downgrade drops
    the checks, the key, the index, the columns, in that order; every other table, key and check
    and the wallet and ledger rows are the same throughout."""
    _at_0036(alembic_config, engine)
    try:
        at_0036 = _schema_snapshot(engine)
        rows = _ledger_and_wallets(engine)

        with _ddl_statements() as statements:
            command.upgrade(alembic_config, _REVISION)
        assert [_compact(s) for s in statements] == [_compact(_DESIGN_ALTER)]
        assert _version(engine) == _REVISION
        _assert_design_shape(engine)
        at_0037 = _schema_snapshot(engine)
        assert _without_ours(at_0037) == at_0036
        assert _ledger_and_wallets(engine) == rows

        with _ddl_statements() as statements:
            command.downgrade(alembic_config, _DOWN_REVISION)
        assert [_normalised(s) for s in statements] == _DESIGN_DOWNGRADE
        assert _version(engine) == _DOWN_REVISION
        assert _our_columns(engine) == set()
        assert _schema_snapshot(engine) == at_0036
        assert _ledger_and_wallets(engine) == rows

        command.upgrade(alembic_config, _REVISION)
        assert _schema_snapshot(engine) == at_0037
        _assert_design_shape(engine)
        assert _ledger_and_wallets(engine) == rows
    finally:
        _back_to_head(alembic_config, engine)


@needs_mysql
@pytest.mark.parametrize("case", list(_PRESENT_CASES))
def test_0037_upgrade_with_a_column_already_there_fails_before_any_ddl(
    alembic_config: Config, engine: Engine, case: str
) -> None:
    """Design §8: version 0036 and a new column already there (each one, or all four) — the
    upgrade fails naming it, issues no DDL, the version stays 0036 and the schema is as it
    was."""
    _at_0036(alembic_config, engine)
    present = _PRESENT_CASES[case]
    try:
        for name in _DESIGN_COLUMNS:
            if name in present:
                kind, _ = _DESIGN_COLUMNS[name]
                _execute(engine, f"ALTER TABLE {_PAYMENTS} ADD COLUMN {name} {kind} NULL")
        before = _schema_snapshot(engine)

        with _ddl_statements() as statements:
            with pytest.raises(RuntimeError) as raised:
                command.upgrade(alembic_config, _REVISION)

        assert statements == []
        message = str(raised.value)
        for name in _DESIGN_COLUMNS:
            assert (f"column {_PAYMENTS}.{name} already exists" in message) == (name in present), (
                name
            )
        assert "No DDL was issued" in message
        assert _version(engine) == _DOWN_REVISION
        assert _schema_snapshot(engine) == before
    finally:
        _back_to_head(alembic_config, engine)


@needs_mysql
@pytest.mark.parametrize("resolved", ["CREDITED with PAID", "CLOSED with FAILED"])
def test_0037_downgrade_with_a_resolved_row_refuses_before_any_ddl(
    alembic_config: Config, engine: Engine, resolved: str
) -> None:
    """Design §8: a resolved payment — the downgrade fails, no DDL, the rows are as they were,
    `alembic_version` stays 0037."""
    _at_0037(alembic_config, engine)
    try:
        _insert_committed(engine, [_LEGAL_ROWS[resolved], _LEGAL_ROWS["PENDING, not resolved"]])
        before = _schema_snapshot(engine)
        with engine.connect() as connection:
            rows = [
                tuple(row)
                for row in connection.execute(text(f"SELECT * FROM {_PAYMENTS} ORDER BY id"))
            ]

        with _ddl_statements() as statements:
            with pytest.raises(RuntimeError) as raised:
                command.downgrade(alembic_config, _DOWN_REVISION)

        assert statements == []
        message = str(raised.value)
        assert "holds resolved payments" in message
        assert "No DDL was issued and no row was deleted" in message
        assert _version(engine) == _REVISION
        assert _schema_snapshot(engine) == before
        with engine.connect() as connection:
            assert [
                tuple(row)
                for row in connection.execute(text(f"SELECT * FROM {_PAYMENTS} ORDER BY id"))
            ] == rows
    finally:
        _back_to_head(alembic_config, engine)


@needs_mysql
def test_0037_downgrade_with_unresolved_rows_only_succeeds(
    alembic_config: Config, engine: Engine
) -> None:
    """Rows with no resolution do not stop the downgrade: the columns go, the rows stay with
    their 0036 columns as they were."""
    _at_0037(alembic_config, engine)
    try:
        _insert_committed(
            engine,
            [
                _LEGAL_ROWS["PENDING, not resolved"],
                _LEGAL_ROWS["REVIEW_REQUIRED, not resolved"],
                _LEGAL_ROWS["PAID, not resolved"],
            ],
        )
        rows = _payment_rows(engine)

        command.downgrade(alembic_config, _DOWN_REVISION)

        assert _version(engine) == _DOWN_REVISION
        assert _our_columns(engine) == set()
        assert _payment_rows(engine) == rows
        assert len(rows) == 3
    finally:
        _back_to_head(alembic_config, engine)


@needs_mysql
def test_0037_offline_upgrade_and_downgrade(alembic_config: Config) -> None:
    """Design §8: `--sql` upgrade emits the one `ALTER TABLE` without reading the database; the
    `--sql` downgrade fails saying it must run online, and its output has no DROP."""
    upgrade_buffer = io.StringIO()
    command.upgrade(
        Config("alembic.ini", output_buffer=upgrade_buffer),
        f"{_DOWN_REVISION}:{_REVISION}",
        sql=True,
    )
    emitted = _compact(upgrade_buffer.getvalue())
    assert _compact(_DESIGN_ALTER) in emitted

    downgrade_buffer = io.StringIO()
    with pytest.raises(RuntimeError) as raised:
        command.downgrade(
            Config("alembic.ini", output_buffer=downgrade_buffer),
            f"{_REVISION}:{_DOWN_REVISION}",
            sql=True,
        )

    message = str(raised.value)
    assert _PAYMENTS in message
    assert "online" in message
    assert "DROP" not in _normalised(downgrade_buffer.getvalue())


@contextmanager
def _mysql_rolled_back(engine: Engine) -> Iterator[sqlalchemy.Connection]:
    """A connection with foreign key checks off (no tenant, tax policy version or user is
    needed), whose writes are all rolled back."""
    with engine.connect() as connection:
        transaction = connection.begin()
        try:
            connection.exec_driver_sql("SET FOREIGN_KEY_CHECKS = 0")
            yield connection
        finally:
            transaction.rollback()
            connection.exec_driver_sql("SET FOREIGN_KEY_CHECKS = 1")


@needs_mysql
def test_0037_mysql_takes_the_legal_rows(alembic_config: Config, engine: Engine) -> None:
    command.upgrade(alembic_config, "head")
    with _mysql_rolled_back(engine) as connection:
        for overrides in _LEGAL_ROWS.values():
            _insert(connection, overrides)
        count = int(connection.execute(_COUNT).scalar_one())

    assert count == len(_LEGAL_ROWS)


@needs_mysql
@pytest.mark.parametrize("row", list(_ILLEGAL_ROWS))
def test_0037_mysql_checks_refuse_the_illegal_rows(
    alembic_config: Config, engine: Engine, row: str
) -> None:
    """Each illegal combination on MySQL is refused (3819, the check named). Everything is
    rolled back."""
    command.upgrade(alembic_config, "head")
    overrides, checks = _ILLEGAL_ROWS[row]
    with _mysql_rolled_back(engine) as connection:
        with pytest.raises(DBAPIError) as raised:
            with connection.begin_nested():
                _insert(connection, overrides)
        assert int(raised.value.orig.args[0]) == _ER_CHECK_CONSTRAINT_VIOLATED, row
        assert any(check in str(raised.value.orig.args[1]) for check in checks), row
        assert int(connection.execute(_COUNT).scalar_one()) == 0


@needs_mysql
@pytest.mark.parametrize("case", list(_FINAL_UPDATES))
def test_0037_mysql_a_resolved_row_is_final(
    alembic_config: Config, engine: Engine, case: str
) -> None:
    """Design §2 / §4: moving a resolved row away from its status, or emptying one of its
    resolution fields, is refused (3819, `ck_payments_resolution_fields`); the row is unchanged.
    Everything is rolled back."""
    command.upgrade(alembic_config, "head")
    legal, update = _FINAL_UPDATES[case]
    select = text(f"SELECT * FROM {_PAYMENTS} WHERE id = :id")
    with _mysql_rolled_back(engine) as connection:
        payment_id = _insert(connection, legal)
        before = tuple(connection.execute(select, {"id": payment_id}).one())
        with pytest.raises(DBAPIError) as raised:
            with connection.begin_nested():
                connection.execute(text(update), {"id": payment_id})
        assert int(raised.value.orig.args[0]) == _ER_CHECK_CONSTRAINT_VIOLATED, case
        assert "ck_payments_resolution_fields" in str(raised.value.orig.args[1]), case
        assert tuple(connection.execute(select, {"id": payment_id}).one()) == before

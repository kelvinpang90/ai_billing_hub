"""0039_payment_receipts（AIH-TASK-196，设计闸门 #589 v2）.

Design §2 "数据库" / "编号", §3 INV-3 / INV-6 / INV-10, §6 "数据保留",
§7 (the "迁移、CHECK 与触发器" row, task 196), §8 (the migration item).

The first cases need no database: the revision chain; the columns, primary keys, indexes, foreign
keys and checks of both tables in the migration equal the models' and the design's, under the same
names and in the same order; `series_year` is not auto-incremented; the two `CREATE TABLE`, the
`INSERT` of the 74 series rows and the two `CREATE TRIGGER` the MySQL upgrade sends; every branch
of the upgrade (nothing yet / everything built / a trigger missing / a trigger that differs / each
wrong shape of either table / `series_year` AUTO_INCREMENT / only the series rows missing (no
prerequisite check) / wrong series rows / receipts there while the series table is missing, empty
or behind their highest sequence / the prerequisite check / `--sql`) and of the downgrade (empty /
with a row / `--sql` / one trigger left / the receipts table already gone / nothing there), with
`alembic.op` and the inspector stood in. `create_all` on SQLite (what the unit tests run) builds
both tables; their checks and unique constraints refuse every illegal row.

On a real MySQL (`BILLING_TEST_DATABASE_URL`; skipped when unset, never skipped in CI): design
§7's migration row — the 0038 → 0039 → 0038 → 0039 round trip (every other table, key, check and
trigger and the wallet and ledger rows unchanged), the rerun with everything built, wrong shapes
(each a fresh hand-made table), one trigger missing, only the series rows missing (one `INSERT`,
no prerequisite check, no DDL), wrong series rows, receipts there while the series table is missing
or empty or a year's `last_sequence` is below that year's MAX(sequence) (refused, no DDL, no row),
the downgrade with a row, the downgrade rerun after it stopped between the two `DROP TABLE`, the
`--sql` upgrade and downgrade — the checks and unique constraints on the same illegal rows, and the
triggers: every UPDATE and the DELETE of a receipt fail with SQLSTATE 45000. The Alembic chain does
not run on SQLite (0006 onwards ask for `@@log_bin`), so every migration scenario is MySQL's, as in
test_monthly_statements_migration.py. Captured statements are compared after normalising
(backticks, case, spaces). Receipts the tests write are removed with TRUNCATE (the triggers refuse
DELETE; they are never dropped to clean up). No gateway is called and no clock is read: this task
has no service.
"""

from __future__ import annotations

import copy
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
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.schema import CreateTable

from alembic import command
from app.models.base import Base
from app.models.receipts import (
    RECEIPT_AMOUNT_CHECK,
    RECEIPT_CURRENCY_CHECK,
    RECEIPT_NUMBER_LENGTH,
    RECEIPT_SEQUENCE_CHECK,
    RECEIPT_SERIES_YEAR_CHECK,
    SERIES_SEQUENCE_CHECK,
    SERIES_YEAR_CHECK,
    PaymentReceipt,
    ReceiptNumberSeries,
)

TEST_DATABASE_URL = os.environ.get("BILLING_TEST_DATABASE_URL", "")

needs_mysql = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="BILLING_TEST_DATABASE_URL is not set; migration tests need a real MySQL",
)

_REVISION = "0039_payment_receipts"
_DOWN_REVISION = "0038_payment_settings"
_MIGRATION = pathlib.Path("alembic/versions/20261109_0039_payment_receipts.py")
_SERIES = "receipt_number_series"
_RECEIPTS = "payment_receipts"
_TABLES = (_SERIES, _RECEIPTS)
_MODELS = {_SERIES: ReceiptNumberSeries, _RECEIPTS: PaymentReceipt}
_UPDATE_TRIGGER = "trg_payment_receipts_before_update"
_DELETE_TRIGGER = "trg_payment_receipts_before_delete"

# Design §2 "数据库": 2026 to 2099, one row a year.
_YEARS = list(range(2026, 2100))
_SEED_ROWS = [(year, 0) for year in _YEARS]

# Design §2 "数据库": the columns, in order.
_DESIGN_COLUMNS = {
    _SERIES: {
        "series_year": ("SMALLINT", False),
        "last_sequence": ("INTEGER", False),
    },
    _RECEIPTS: {
        "id": ("BIGINT", False),
        "public_id": ("CHAR(36)", False),
        "payment_id": ("BIGINT", False),
        "tenant_id": ("BIGINT", False),
        "receipt_number": ("VARCHAR(32)", False),
        "series_year": ("SMALLINT", False),
        "sequence": ("INTEGER", False),
        "amount": ("DECIMAL(20,8)", False),
        "currency": ("CHAR(3)", False),
        "tax_policy_version_id": ("BIGINT", False),
        "snapshot_json": ("JSON", False),
        "issued_at": ("DATETIME", False),
    },
}
_DESIGN_PRIMARY_KEYS = {_SERIES: ("series_year",), _RECEIPTS: ("id",)}
_PAYMENT_UNIQUE = "uq_payment_receipts_payment_id"
_SERIES_SEQUENCE_UNIQUE = "uq_payment_receipts_series_sequence"
_TENANT_ISSUED_INDEX = "ix_payment_receipts_tenant_issued"
_TAX_POLICY_INDEX = "ix_payment_receipts_tax_policy_version_id"
_DESIGN_INDEXES: dict[str, dict[str, tuple[tuple[str, ...], bool]]] = {
    _SERIES: {},
    _RECEIPTS: {
        "uq_payment_receipts_public_id": (("public_id",), True),
        _PAYMENT_UNIQUE: (("payment_id",), True),
        "uq_payment_receipts_receipt_number": (("receipt_number",), True),
        _SERIES_SEQUENCE_UNIQUE: (("series_year", "sequence"), True),
        _TENANT_ISSUED_INDEX: (("tenant_id", "issued_at", "id"), False),
        _TAX_POLICY_INDEX: (("tax_policy_version_id",), False),
    },
}
_FK_PAYMENT = "fk_payment_receipts_payment_id"
_FK_TENANT = "fk_payment_receipts_tenant_id"
_FK_TAX_POLICY = "fk_payment_receipts_tax_policy_version_id"
_DESIGN_FOREIGN_KEYS: dict[str, dict[str, tuple[tuple[str, ...], str, tuple[str, ...], str]]] = {
    _SERIES: {},
    _RECEIPTS: {
        _FK_PAYMENT: (("payment_id",), "payments", ("id",), "RESTRICT"),
        _FK_TENANT: (("tenant_id",), "tenants", ("id",), "RESTRICT"),
        _FK_TAX_POLICY: (("tax_policy_version_id",), "tax_policy_versions", ("id",), "RESTRICT"),
    },
}
_DESIGN_CHECK_TEXTS = {
    _SERIES: {
        "ck_receipt_number_series_year": "series_year BETWEEN 2026 AND 2099",
        "ck_receipt_number_series_sequence": "last_sequence BETWEEN 0 AND 999999",
    },
    _RECEIPTS: {
        "ck_payment_receipts_series_year": "series_year BETWEEN 2026 AND 2099",
        "ck_payment_receipts_sequence": "sequence BETWEEN 1 AND 999999",
        "ck_payment_receipts_amount": "amount > 0 AND amount = ROUND(amount, 2)",
        "ck_payment_receipts_currency": "currency = 'MYR'",
    },
}
# The task contract's trigger body: unconditional.
_IMMUTABLE = "payment_receipts rows are immutable"
_DESIGN_TRIGGER_BODY = f"BEGIN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = '{_IMMUTABLE}'; END"
_DESIGN_TRIGGERS = {
    _UPDATE_TRIGGER: (_RECEIPTS, "BEFORE", "UPDATE"),
    _DELETE_TRIGGER: (_RECEIPTS, "BEFORE", "DELETE"),
}

# The design's MySQL tables, clause by clause; the wrong shapes below are edits of them.
_SERIES_CLAUSES = [
    "series_year SMALLINT NOT NULL",
    "last_sequence INT NOT NULL DEFAULT 0",
    "PRIMARY KEY (series_year)",
    "CONSTRAINT ck_receipt_number_series_year CHECK (series_year BETWEEN 2026 AND 2099)",
    "CONSTRAINT ck_receipt_number_series_sequence CHECK (last_sequence BETWEEN 0 AND 999999)",
]
_RECEIPT_CLAUSES = [
    "id BIGINT NOT NULL AUTO_INCREMENT",
    "public_id CHAR(36) NOT NULL",
    "payment_id BIGINT NOT NULL",
    "tenant_id BIGINT NOT NULL",
    "receipt_number VARCHAR(32) NOT NULL",
    "series_year SMALLINT NOT NULL",
    "sequence INT NOT NULL",
    "amount DECIMAL(20, 8) NOT NULL",
    "currency CHAR(3) NOT NULL",
    "tax_policy_version_id BIGINT NOT NULL",
    "snapshot_json JSON NOT NULL",
    "issued_at DATETIME NOT NULL",
    "PRIMARY KEY (id)",
    "CONSTRAINT uq_payment_receipts_public_id UNIQUE (public_id)",
    f"CONSTRAINT {_PAYMENT_UNIQUE} UNIQUE (payment_id)",
    "CONSTRAINT uq_payment_receipts_receipt_number UNIQUE (receipt_number)",
    f"CONSTRAINT {_SERIES_SEQUENCE_UNIQUE} UNIQUE (series_year, sequence)",
    f"INDEX {_TENANT_ISSUED_INDEX} (tenant_id, issued_at, id)",
    f"INDEX {_TAX_POLICY_INDEX} (tax_policy_version_id)",
    f"CONSTRAINT {_FK_PAYMENT} FOREIGN KEY (payment_id) REFERENCES payments (id)"
    " ON DELETE RESTRICT",
    f"CONSTRAINT {_FK_TENANT} FOREIGN KEY (tenant_id) REFERENCES tenants (id) ON DELETE RESTRICT",
    f"CONSTRAINT {_FK_TAX_POLICY} FOREIGN KEY (tax_policy_version_id)"
    " REFERENCES tax_policy_versions (id) ON DELETE RESTRICT",
    f"CONSTRAINT ck_payment_receipts_series_year CHECK ({RECEIPT_SERIES_YEAR_CHECK})",
    f"CONSTRAINT ck_payment_receipts_sequence CHECK ({RECEIPT_SEQUENCE_CHECK})",
    f"CONSTRAINT ck_payment_receipts_amount CHECK ({RECEIPT_AMOUNT_CHECK})",
    f"CONSTRAINT ck_payment_receipts_currency CHECK ({RECEIPT_CURRENCY_CHECK})",
]
_DESIGN_INSERT = f"INSERT INTO {_SERIES} (series_year, last_sequence) VALUES " + ", ".join(
    f"({year}, 0)" for year in _YEARS
)

# MySQL reports a violated CHECK as 3819 (ER_CHECK_CONSTRAINT_VIOLATED), a duplicate key as 1062
# (ER_DUP_ENTRY), a SIGNAL with SQLSTATE '45000' as 1644 (ER_SIGNAL_EXCEPTION).
_ER_CHECK_CONSTRAINT_VIOLATED = 3819
_ER_DUP_ENTRY = 1062
_ER_SIGNAL_EXCEPTION = 1644

_ISSUED_AT = "2026-12-03 06:05:09"
_PUBLIC_ID = "6f1c2a52-6a6e-4b8e-9a43-0f1d2c3b4a59"

_ALEMBIC_VERSION = text("SELECT version_num FROM alembic_version")
_SET_VERSION = "UPDATE alembic_version SET version_num = :version"
_ALL_TRIGGERS_QUERY = text(
    "SELECT TRIGGER_NAME, EVENT_OBJECT_TABLE, ACTION_TIMING, EVENT_MANIPULATION, ACTION_STATEMENT"
    " FROM information_schema.TRIGGERS WHERE TRIGGER_SCHEMA = DATABASE()"
)
_CHECKS_QUERY = text(
    "SELECT TABLE_NAME, CONSTRAINT_NAME FROM information_schema.TABLE_CONSTRAINTS"
    " WHERE CONSTRAINT_SCHEMA = DATABASE() AND CONSTRAINT_TYPE = 'CHECK'"
)
_DELETE_RULES_QUERY = text(
    "SELECT CONSTRAINT_NAME, DELETE_RULE FROM information_schema.REFERENTIAL_CONSTRAINTS"
    " WHERE CONSTRAINT_SCHEMA = DATABASE() AND TABLE_NAME = :table"
)
_AUTO_INCREMENT_QUERY = text(
    "SELECT COLUMN_NAME FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = DATABASE()"
    " AND TABLE_NAME = :table AND EXTRA LIKE '%auto_increment%'"
)
_WALLET_ROWS = text("SELECT * FROM wallets ORDER BY id")
_LEDGER_ROWS = text("SELECT * FROM wallet_transactions ORDER BY id")
_SERIES_ROWS = text(f"SELECT series_year, last_sequence FROM {_SERIES} ORDER BY series_year")
_RECEIPT_COUNT = text(f"SELECT COUNT(*) FROM {_RECEIPTS}")
_SERIES_COUNT = text(f"SELECT COUNT(*) FROM {_SERIES}")

# Raw SQL: the values go straight to the database's checks.
_INSERT_RECEIPT = text(
    f"INSERT INTO {_RECEIPTS} (public_id, payment_id, tenant_id, receipt_number, series_year,"
    " sequence, amount, currency, tax_policy_version_id, snapshot_json, issued_at) VALUES"
    " (:public_id, :payment_id, :tenant_id, :receipt_number, :series_year, :sequence, :amount,"
    " :currency, :tax_policy_version_id, :snapshot, :issued_at)"
)
_INSERT_SERIES_ROW = text(
    f"INSERT INTO {_SERIES} (series_year, last_sequence) VALUES (:series_year, :last_sequence)"
)
_RECEIPT_ID_OF = text(f"SELECT id FROM {_RECEIPTS} WHERE public_id = :public_id")


def _receipt_params(overrides: dict[str, object], n: int = 1) -> dict[str, object]:
    """A legal receipt (payment `n`, 2026 sequence `n`), with `overrides` on top; the receipt
    number follows the year and sequence unless overridden."""
    params: dict[str, object] = {
        "public_id": str(uuid.uuid4()),
        "payment_id": n,
        "tenant_id": 1,
        "series_year": 2026,
        "sequence": n,
        "amount": "100.00",
        "currency": "MYR",
        "tax_policy_version_id": 1,
        "snapshot": '{"schema_version": 1}',
        "issued_at": _ISSUED_AT,
    }
    params.update(overrides)
    params.setdefault(
        "receipt_number",
        f"ACV-AIR-{int(str(params['series_year'])):04d}-{int(str(params['sequence'])):06d}",
    )
    return params


def _insert_receipt(
    connection: sqlalchemy.Connection, overrides: dict[str, object], n: int = 1
) -> int:
    params = _receipt_params(overrides, n)
    connection.execute(_INSERT_RECEIPT, params)
    return int(connection.execute(_RECEIPT_ID_OF, {"public_id": params["public_id"]}).scalar_one())


def _receipt_check(name: str) -> str:
    return f"ck_payment_receipts_{name}"


# Design §7: illegal receipts (each alone in the table) → the check that refuses it.
_ILLEGAL_RECEIPTS: dict[str, tuple[dict[str, object], str]] = {
    "sequence 0": ({"sequence": 0}, _receipt_check("sequence")),
    "sequence 1000000": ({"sequence": 1000000}, _receipt_check("sequence")),
    "series_year 2025": ({"series_year": 2025}, _receipt_check("series_year")),
    "series_year 2100": ({"series_year": 2100}, _receipt_check("series_year")),
    "amount 0": ({"amount": "0"}, _receipt_check("amount")),
    "amount negative": ({"amount": "-1.00"}, _receipt_check("amount")),
    "amount three decimals": ({"amount": "100.005"}, _receipt_check("amount")),
    "currency USD": ({"currency": "USD"}, _receipt_check("currency")),
}
# Legal receipts next to them: the edges the checks let through (each alone in the table).
_LEGAL_RECEIPTS: dict[str, dict[str, object]] = {
    "the design's": {},
    "sequence 999999": {"sequence": 999999},
    "series_year 2099": {"series_year": 2099},
    "amount 0.01": {"amount": "0.01"},
    "amount 999999.99": {"amount": "999999.99"},
}
# Design §7: a second receipt next to `_FIRST_RECEIPT` → (the unique constraint, its columns).
_FIRST_RECEIPT: dict[str, object] = {"public_id": _PUBLIC_ID}
_DUPLICATES: dict[str, tuple[dict[str, object], str, str]] = {
    "the same payment twice": ({"payment_id": 1}, _PAYMENT_UNIQUE, "payment_id"),
    "the same year and sequence": (
        {"sequence": 1, "receipt_number": "ACV-AIR-2026-900001"},
        _SERIES_SEQUENCE_UNIQUE,
        "series_year",
    ),
    "the same receipt number": (
        {"receipt_number": "ACV-AIR-2026-000001"},
        "uq_payment_receipts_receipt_number",
        "receipt_number",
    ),
    "the same public id": ({"public_id": _PUBLIC_ID}, "uq_payment_receipts_public_id", "public_id"),
}
# Illegal series rows → the check that refuses it; legal ones next to them.
_ILLEGAL_SERIES: dict[str, tuple[dict[str, int], str]] = {
    "series_year 2025": (
        {"series_year": 2025, "last_sequence": 0},
        "ck_receipt_number_series_year",
    ),
    "series_year 2100": (
        {"series_year": 2100, "last_sequence": 0},
        "ck_receipt_number_series_year",
    ),
    "last_sequence -1": (
        {"series_year": 2050, "last_sequence": -1},
        "ck_receipt_number_series_sequence",
    ),
    "last_sequence 1000000": (
        {"series_year": 2050, "last_sequence": 1000000},
        "ck_receipt_number_series_sequence",
    ),
}
_LEGAL_SERIES: dict[str, dict[str, int]] = {
    "2026 at 0": {"series_year": 2026, "last_sequence": 0},
    "2099 at 999999": {"series_year": 2099, "last_sequence": 999999},
}


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("migration_0039", _MIGRATION)
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
    """The migration's own normalisation: type and width only."""
    return str(column_type).replace(", ", ",").split(" ")[0].upper()


def _model_type_text(column: sqlalchemy.Column) -> str:
    # The generic `Numeric` compiles to NUMERIC on MySQL, which stores (and reflects) DECIMAL.
    compiled = column.type.compile(dialect=mysql.dialect())
    return _type_text(compiled).replace("NUMERIC", "DECIMAL")


def _model_shape(table_name: str) -> dict[str, object]:
    """A model's columns, primary key, indexes, foreign keys and check names."""
    table = _MODELS[table_name].__table__
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
        "primary_key": tuple(c.name for c in table.primary_key.columns),
        "indexes": indexes,
        "foreign_keys": foreign_keys,
        "checks": checks,
    }


def _design_shape(table: str) -> dict[str, object]:
    return {
        "columns": _DESIGN_COLUMNS[table],
        "primary_key": _DESIGN_PRIMARY_KEYS[table],
        "indexes": _DESIGN_INDEXES[table],
        "foreign_keys": _DESIGN_FOREIGN_KEYS[table],
        "checks": set(_DESIGN_CHECK_TEXTS[table]),
    }


# ---------------------------------------------------------------------------
# No database
# ---------------------------------------------------------------------------


def test_0039_follows_0038() -> None:
    migration = _load()

    assert migration.revision == _REVISION
    assert migration.down_revision == _DOWN_REVISION
    # `alembic_version.version_num` is VARCHAR(32).
    assert len(migration.revision) <= 32


@pytest.mark.parametrize("table", _TABLES)
def test_0039_shape_is_the_design_and_the_model(table: str) -> None:
    """The migration's frozen literals, the model's declarations and design §2 agree: columns
    (type, width, nullability, in this order), the primary key, the indexes in their column
    order, the foreign keys RESTRICT, the checks by name."""
    migration = _load()

    assert _MODELS[table].__tablename__ == table
    assert migration._SHAPES[table] == _design_shape(table) == _model_shape(table)
    assert list(migration._SHAPES[table]["columns"]) == list(_DESIGN_COLUMNS[table])
    assert [c.name for c in _MODELS[table].__table__.columns] == list(_DESIGN_COLUMNS[table])
    assert list(migration._TABLES) == [_SERIES, _RECEIPTS]


def test_0039_series_year_is_not_auto_incremented() -> None:
    """Design §2: `autoincrement=False` on the model, no AUTO_INCREMENT in the MySQL DDL of the
    migration or of the model."""
    migration = _load()
    model_ddl = str(CreateTable(ReceiptNumberSeries.__table__).compile(dialect=mysql.dialect()))

    assert ReceiptNumberSeries.__table__.c.series_year.autoincrement is False
    assert "AUTO_INCREMENT" not in model_ddl.upper()
    assert "AUTO_INCREMENT" not in migration._CREATE_SERIES_MYSQL.upper()
    assert str(ReceiptNumberSeries.__table__.c.last_sequence.server_default.arg) == "0"


def test_0039_receipt_number_length_is_32() -> None:
    assert RECEIPT_NUMBER_LENGTH == 32
    assert PaymentReceipt.__table__.c.receipt_number.type.length == RECEIPT_NUMBER_LENGTH
    # `ACV-AIR-YYYY-NNNNNN` fits.
    assert len("ACV-AIR-2026-000001") <= RECEIPT_NUMBER_LENGTH


def test_0039_amount_is_decimal_20_8() -> None:
    """INV-10: the receipt amount is DECIMAL(20,8), read as Decimal."""
    column = PaymentReceipt.__table__.c.amount
    assert isinstance(column.type, sqlalchemy.Numeric)
    assert (column.type.precision, column.type.scale) == (20, 8)
    assert column.type.asdecimal is True


@pytest.mark.parametrize("table", _TABLES)
def test_0039_table_args_order_is_the_migrations(table: str) -> None:
    """`__table_args__` declares the unique constraints, indexes, foreign keys and checks under
    the migration's names and in its order."""
    migration = _load()
    shape = migration._SHAPES[table]
    checks = migration._SERIES_CHECKS if table == _SERIES else migration._RECEIPT_CHECKS
    kinds = UniqueConstraint | sqlalchemy.Index | ForeignKeyConstraint | CheckConstraint
    declared = [str(arg.name) for arg in _MODELS[table].__table_args__ if isinstance(arg, kinds)]

    assert declared == [*shape["indexes"], *shape["foreign_keys"], *checks]


@pytest.mark.parametrize("table", _TABLES)
def test_0039_checks_are_the_designs_and_the_models(table: str) -> None:
    """Same conditions in the migration, on the model and in the design (whitespace aside)."""
    migration = _load()
    checks = migration._SERIES_CHECKS if table == _SERIES else migration._RECEIPT_CHECKS
    model = {
        str(constraint.name): _normalised(str(constraint.sqltext))
        for constraint in _MODELS[table].__table__.constraints
        if isinstance(constraint, CheckConstraint)
    }

    assert {name: _normalised(c) for name, c in checks.items()} == model
    assert {name: _normalised(c) for name, c in _DESIGN_CHECK_TEXTS[table].items()} == model
    assert list(checks) == list(_DESIGN_CHECK_TEXTS[table])


def test_0039_model_check_constants() -> None:
    assert _normalised(SERIES_YEAR_CHECK) == _normalised(RECEIPT_SERIES_YEAR_CHECK)
    assert _normalised(SERIES_SEQUENCE_CHECK) == _normalised("last_sequence BETWEEN 0 AND 999999")


@pytest.mark.parametrize("table", _TABLES)
def test_0039_models_tell_not_to_bypass_issue_receipt(table: str) -> None:
    doc = _MODELS[table].__doc__ or ""
    assert "issue_receipt" in doc
    if table == _RECEIPTS:
        assert "UPDATE" in doc and "DELETE" in doc
        assert "触发器" in doc


def test_0039_mysql_create_tables_are_the_design_ones() -> None:
    """The two `CREATE TABLE` the MySQL upgrade sends are the design's clauses, checks
    included; each is one statement."""
    migration = _load()

    assert _compact(migration._CREATE_SERIES_MYSQL) == _compact(
        f"CREATE TABLE {_SERIES} ({', '.join(_SERIES_CLAUSES)})"
    )
    assert _compact(migration._CREATE_RECEIPTS_MYSQL) == _compact(
        f"CREATE TABLE {_RECEIPTS} ({', '.join(_RECEIPT_CLAUSES)})"
    )
    for statement in (migration._CREATE_SERIES_MYSQL, migration._CREATE_RECEIPTS_MYSQL):
        assert statement.count(";") == 0
        assert _normalised(statement).count("CREATE TABLE") == 1


def test_0039_series_insert_is_the_74_design_rows() -> None:
    """Design §2: one INSERT, a row for each year 2026–2099 with `last_sequence = 0`."""
    migration = _load()

    assert len(_YEARS) == 74
    assert list(migration._SERIES_YEARS) == _YEARS
    assert _compact(migration._INSERT_SERIES) == _compact(_DESIGN_INSERT)
    assert migration._INSERT_SERIES.count(";") == 0
    assert migration._INSERT_SERIES.count("(") == 1 + len(_YEARS)


def test_0039_triggers_are_the_design_ones() -> None:
    """Both triggers: BEFORE UPDATE / BEFORE DELETE on payment_receipts, the unconditional body
    of the task contract (BEGIN … END around it)."""
    migration = _load()

    assert _normalised(migration._TRIGGER_BODY) == _normalised(_DESIGN_TRIGGER_BODY)
    assert migration._TRIGGER_BODY == migration._TRIGGER_BODY.strip()
    assert " IF " not in f" {_normalised(migration._TRIGGER_BODY)} "
    assert list(migration._TRIGGERS) == [_UPDATE_TRIGGER, _DELETE_TRIGGER]
    for name, (table, timing, kind) in _DESIGN_TRIGGERS.items():
        assert _compact(migration._TRIGGERS[name]) == _compact(
            f"CREATE TRIGGER {name} {timing} {kind} ON {table} FOR EACH ROW {_DESIGN_TRIGGER_BODY}"
        )


def test_0039_prerequisite_text_is_0035s() -> None:
    """The prerequisite's query, check and wording follow 0035 (the trigger noun aside)."""
    migration = _load()
    spec = importlib.util.spec_from_file_location(
        "migration_0035", pathlib.Path("alembic/versions/20261104_0035_monthly_statements.py")
    )
    assert spec is not None and spec.loader is not None
    earlier = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(earlier)

    assert migration._PREREQUISITE_QUERY == earlier._PREREQUISITE_QUERY
    assert migration._PREREQUISITE_ERROR == earlier._PREREQUISITE_ERROR.replace(
        "monthly statement triggers", "payment receipt triggers"
    )


class _Result:
    def __init__(self, rows: list[tuple[object, ...]]) -> None:
        self._rows = rows

    def all(self) -> list[tuple[object, ...]]:
        return list(self._rows)

    def first(self) -> tuple[object, ...] | None:
        return self._rows[0] if self._rows else None

    def one(self) -> tuple[object, ...]:
        assert len(self._rows) == 1
        return self._rows[0]


_HAS_RECEIPTS_SQL = f"SELECT 1 FROM {_RECEIPTS} LIMIT 1"
_SERIES_ROWS_SQL = f"SELECT series_year, last_sequence FROM {_SERIES}"
_MAX_SEQUENCES_SQL = f"SELECT series_year, MAX(sequence) FROM {_RECEIPTS} GROUP BY series_year"


class _FakeBind:
    """Answers what the migration asks: delete rules, AUTO_INCREMENT columns, the triggers, the
    prerequisite variables, "any receipt?", the series rows and the highest sequences."""

    def __init__(
        self,
        delete_rules: dict[str, dict[str, str]],
        triggers: dict[str, tuple[str, str, str, str]],
        *,
        series_rows: list[tuple[int, int]],
        receipts: list[tuple[int, int]],
        auto_increment: tuple[str, ...] = (),
        log_bin: int = 1,
        trusted: int = 1,
    ) -> None:
        self._delete_rules = delete_rules
        self._triggers = triggers
        self._series_rows = series_rows
        self._receipts = receipts
        self._auto_increment = auto_increment
        self._variables = (log_bin, trusted)
        self.queries: list[str] = []

    def execute(self, statement: object) -> _Result:
        sql = str(statement)
        self.queries.append(sql)
        if "REFERENTIAL_CONSTRAINTS" in sql:
            match = re.search(r"TABLE_NAME = '(\w+)'", sql)
            assert match is not None, sql
            return _Result(list(self._delete_rules.get(match.group(1), {}).items()))
        if "information_schema.COLUMNS" in sql:
            assert f"TABLE_NAME = '{_SERIES}'" in sql, sql
            return _Result([(name,) for name in self._auto_increment])
        if "information_schema.TRIGGERS" in sql:
            return _Result([(name, *row) for name, row in self._triggers.items()])
        if "@@log_bin" in sql:
            return _Result([self._variables])
        if sql == _HAS_RECEIPTS_SQL:
            return _Result([(1,)] if self._receipts else [])
        if sql == _SERIES_ROWS_SQL:
            return _Result(list(self._series_rows))
        if sql == _MAX_SEQUENCES_SQL:
            tops: dict[int, int] = {}
            for year, sequence in self._receipts:
                tops[year] = max(tops.get(year, 0), sequence)
            return _Result(list(tops.items()))
        raise AssertionError(sql)


class _FakeInspector:
    """`Inspector` over the reflected tables that exist."""

    def __init__(self, tables: dict[str, dict]) -> None:
        self._tables = tables
        self.asked: list[tuple[str, str]] = []

    def _get(self, key: str, name: str):
        assert name in self._tables, name
        self.asked.append((name, key))
        return copy.deepcopy(self._tables[name][key])

    def has_table(self, name: str) -> bool:
        assert name in _TABLES, name
        self.asked.append((name, "has_table"))
        return name in self._tables

    def get_columns(self, name: str) -> list[dict]:
        return self._get("columns", name)

    def get_pk_constraint(self, name: str) -> dict:
        return self._get("primary_key", name)

    def get_indexes(self, name: str) -> list[dict]:
        return self._get("indexes", name)

    def get_unique_constraints(self, name: str) -> list[dict]:
        return self._get("unique_constraints", name)

    def get_foreign_keys(self, name: str) -> list[dict]:
        return self._get("foreign_keys", name)

    def get_check_constraints(self, name: str) -> list[dict]:
        return self._get("check_constraints", name)


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

    def __init__(self, bind: _FakeBind, dialect: str = "mysql", as_sql: bool = False) -> None:
        self.calls: list[tuple[str, tuple[object, ...]]] = []
        self._bind = bind
        self._context = SimpleNamespace(as_sql=as_sql, dialect=SimpleNamespace(name=dialect))

    def get_context(self) -> SimpleNamespace:
        return self._context

    def get_bind(self) -> _FakeBind:
        return self._bind

    def execute(self, statement: str) -> None:
        self.calls.append(("execute", (statement,)))

    def create_table(self, name: str, *elements: object) -> None:
        self.calls.append(("create_table", (name,)))

    def create_index(self, name: str, table: str, columns: list[str], unique: bool = False) -> None:
        self.calls.append(("create_index", (name, table, list(columns), unique)))

    def drop_table(self, name: str) -> None:
        self.calls.append(("drop_table", (name,)))


_MYSQL_TYPES = {
    "BIGINT": mysql.BIGINT(),
    "SMALLINT": mysql.SMALLINT(),
    "INTEGER": mysql.INTEGER(),
    "CHAR(3)": mysql.CHAR(length=3),
    "CHAR(36)": mysql.CHAR(length=36),
    "VARCHAR(32)": mysql.VARCHAR(length=32),
    "DECIMAL(20,8)": mysql.DECIMAL(precision=20, scale=8),
    "DATETIME": mysql.DATETIME(),
    "JSON": mysql.JSON(),
}


def _design_table(table: str) -> dict:
    """A table the way MySQL's inspector reflects it: unique constraints are also indexes."""
    return {
        "columns": [
            {
                "name": name,
                "type": _MYSQL_TYPES[kind],
                "nullable": nullable,
                "autoincrement": table == _RECEIPTS and name == "id",
            }
            for name, (kind, nullable) in _DESIGN_COLUMNS[table].items()
        ],
        "primary_key": {"constrained_columns": list(_DESIGN_PRIMARY_KEYS[table]), "name": None},
        "indexes": [
            {"name": name, "column_names": list(columns), "unique": unique}
            for name, (columns, unique) in _DESIGN_INDEXES[table].items()
        ],
        "unique_constraints": [
            {"name": name, "column_names": list(columns)}
            for name, (columns, unique) in _DESIGN_INDEXES[table].items()
            if unique
        ],
        "foreign_keys": [
            {
                "name": name,
                "constrained_columns": list(columns),
                "referred_schema": None,
                "referred_table": referred,
                "referred_columns": list(referred_columns),
                "options": {"ondelete": rule},
            }
            for name, (columns, referred, referred_columns, rule) in _DESIGN_FOREIGN_KEYS[
                table
            ].items()
        ],
        "check_constraints": [
            {"name": name, "sqltext": "…"} for name in _DESIGN_CHECK_TEXTS[table]
        ],
        "delete_rules": {name: key[3] for name, key in _DESIGN_FOREIGN_KEYS[table].items()},
    }


def _design_tables() -> dict[str, dict]:
    return {table: _design_table(table) for table in _TABLES}


def _design_triggers(migration: ModuleType) -> dict[str, tuple[str, str, str, str]]:
    """As information_schema.TRIGGERS keeps them: the body with MySQL's own blanks around it."""
    return {
        name: (table, timing, kind, f"\n{migration._TRIGGER_BODY}\n")
        for name, (table, timing, kind) in _DESIGN_TRIGGERS.items()
    }


def _with(
    monkeypatch,
    tables: dict[str, dict],
    triggers: dict[str, tuple[str, str, str, str]] | None = None,
    *,
    series_rows: list[tuple[int, int]] | None = None,
    receipts: list[tuple[int, int]] | None = None,
    auto_increment: tuple[str, ...] = (),
    dialect: str = "mysql",
    as_sql: bool = False,
    trusted: int = 1,
) -> tuple[ModuleType, _FakeOp, _FakeInspector, _FakeBind]:
    """`series_rows` defaults to the 74 design rows when the series table is there."""
    migration = _load()
    rules = {name: dict(shape["delete_rules"]) for name, shape in tables.items()}
    if series_rows is None:
        series_rows = list(_SEED_ROWS) if _SERIES in tables else []
    bind = _FakeBind(
        rules,
        triggers or {},
        series_rows=series_rows,
        receipts=list(receipts or []),
        auto_increment=auto_increment,
        trusted=trusted,
    )
    fake = _FakeOp(bind, dialect=dialect, as_sql=as_sql)
    inspector = _FakeInspector(tables)
    monkeypatch.setattr(migration, "op", fake)
    monkeypatch.setattr(migration, "sa", _Sa(inspector))
    return migration, fake, inspector, bind


def _executed(fake: _FakeOp) -> list[str]:
    """Each call as `create table <name>`, `insert into <name>`, `create trigger <name>`,
    `drop trigger <name>`, or `<kind> <name>` for the other `op` calls."""
    heads = []
    for kind, args in fake.calls:
        if kind == "execute":
            heads.append(" ".join(_normalised(str(args[0])).split(" ")[:3]).lower())
        else:
            heads.append(f"{kind} {args[0]}")
    return heads


def _prerequisite_asked(bind: _FakeBind) -> bool:
    return any("@@log_bin" in query for query in bind.queries)


_FULL_UPGRADE = [
    f"create table {_SERIES}",
    f"insert into {_SERIES}",
    f"create table {_RECEIPTS}",
    f"create trigger {_UPDATE_TRIGGER}",
    f"create trigger {_DELETE_TRIGGER}",
]


def test_0039_upgrade_builds_everything_in_the_design_order(monkeypatch) -> None:
    migration, fake, _, bind = _with(monkeypatch, {})
    migration.upgrade()

    assert _executed(fake) == _FULL_UPGRADE
    assert [args[0] for _, args in fake.calls] == [
        migration._CREATE_SERIES_MYSQL,
        migration._INSERT_SERIES,
        migration._CREATE_RECEIPTS_MYSQL,
        migration._TRIGGERS[_UPDATE_TRIGGER],
        migration._TRIGGERS[_DELETE_TRIGGER],
    ]
    # The prerequisite check comes before the first DDL.
    assert _prerequisite_asked(bind)
    # No receipts table, no series table: no row is read.
    assert _HAS_RECEIPTS_SQL not in bind.queries
    assert _SERIES_ROWS_SQL not in bind.queries


def test_0039_upgrade_refuses_before_any_write_without_the_trigger_privilege(monkeypatch) -> None:
    """binlog on and log_bin_trust_function_creators off: the account could not create the
    triggers (ERROR 1419) — refuse before CREATE TABLE."""
    migration, fake, _, _ = _with(monkeypatch, {}, trusted=0)

    with pytest.raises(RuntimeError, match="log_bin_trust_function_creators"):
        migration.upgrade()

    assert fake.calls == []


def test_0039_upgrade_with_everything_built_issues_nothing(monkeypatch) -> None:
    migration = _load()
    migration, fake, inspector, bind = _with(
        monkeypatch, _design_tables(), _design_triggers(migration)
    )
    migration.upgrade()

    assert fake.calls == []
    for table in _TABLES:
        for key in ("columns", "primary_key", "indexes", "foreign_keys", "check_constraints"):
            assert (table, key) in inspector.asked
    assert any("information_schema.COLUMNS" in query for query in bind.queries)
    assert _SERIES_ROWS_SQL in bind.queries
    # Nothing to build: the prerequisite is not asked either.
    assert not _prerequisite_asked(bind)


def test_0039_upgrade_on_sqlite_with_everything_built_issues_nothing(monkeypatch) -> None:
    migration, fake, _, bind = _with(monkeypatch, _design_tables(), dialect="sqlite")
    migration.upgrade()

    assert fake.calls == []
    assert bind.queries == [_HAS_RECEIPTS_SQL, _SERIES_ROWS_SQL]


@pytest.mark.parametrize("missing", [_UPDATE_TRIGGER, _DELETE_TRIGGER, "both"])
def test_0039_upgrade_builds_only_the_missing_trigger(monkeypatch, missing: str) -> None:
    """Both tables built, a trigger not (the interrupted upgrade) — only that one."""
    migration = _load()
    triggers = _design_triggers(migration)
    gone = list(triggers) if missing == "both" else [missing]
    for name in gone:
        del triggers[name]
    migration, fake, _, bind = _with(monkeypatch, _design_tables(), triggers)
    migration.upgrade()

    assert _executed(fake) == [f"create trigger {name}" for name in gone]
    assert [args[0] for _, args in fake.calls] == [migration._TRIGGERS[name] for name in gone]
    assert _prerequisite_asked(bind)


def test_0039_upgrade_builds_only_the_receipts_table_and_triggers(monkeypatch) -> None:
    """The series table with its 74 rows, the receipts table not (its CREATE TABLE failed)."""
    migration, fake, _, bind = _with(monkeypatch, {_SERIES: _design_table(_SERIES)})
    migration.upgrade()

    assert _executed(fake) == _FULL_UPGRADE[2:]
    assert _prerequisite_asked(bind)


def test_0039_upgrade_inserts_the_missing_series_rows_then_builds_the_rest(monkeypatch) -> None:
    """Design §2: the series table built and empty (the INSERT not committed), the receipts
    table not — the prerequisite before any write, then the rows, the table and the triggers."""
    migration, fake, _, bind = _with(monkeypatch, {_SERIES: _design_table(_SERIES)}, series_rows=[])
    migration.upgrade()

    assert _executed(fake) == _FULL_UPGRADE[1:]
    assert fake.calls[0][1][0] == migration._INSERT_SERIES
    assert _prerequisite_asked(bind)


def test_0039_upgrade_with_only_the_series_rows_missing_skips_the_prerequisite(
    monkeypatch,
) -> None:
    """Design §2: both tables and both triggers there, no receipt, the series empty — one
    INSERT, no DDL and no prerequisite check."""
    migration = _load()
    migration, fake, _, bind = _with(
        monkeypatch, _design_tables(), _design_triggers(migration), series_rows=[]
    )
    migration.upgrade()

    assert fake.calls == [("execute", (migration._INSERT_SERIES,))]
    assert not _prerequisite_asked(bind)


def test_0039_upgrade_creates_the_series_beside_an_empty_receipts_table(monkeypatch) -> None:
    """The series table gone and `payment_receipts` empty: build and fill the series only."""
    migration = _load()
    migration, fake, _, bind = _with(
        monkeypatch, {_RECEIPTS: _design_table(_RECEIPTS)}, _design_triggers(migration)
    )
    migration.upgrade()

    assert _executed(fake) == _FULL_UPGRADE[:2]
    assert _prerequisite_asked(bind)


def _trigger_changed(name: str, position: int, value: str):
    def mutate(triggers: dict[str, tuple[str, str, str, str]]) -> None:
        row = list(triggers[name])
        row[position] = value
        triggers[name] = (row[0], row[1], row[2], row[3])

    return mutate


# Each trigger that differs: the refusal names it.
_WRONG_TRIGGERS = {
    "update body conditional": (
        _UPDATE_TRIGGER,
        _trigger_changed(
            _UPDATE_TRIGGER,
            3,
            "BEGIN IF OLD.amount <> NEW.amount THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT ="
            f" '{_IMMUTABLE}'; END IF; END",
        ),
    ),
    "update trigger AFTER": (_UPDATE_TRIGGER, _trigger_changed(_UPDATE_TRIGGER, 1, "AFTER")),
    "delete trigger on UPDATE": (_DELETE_TRIGGER, _trigger_changed(_DELETE_TRIGGER, 2, "UPDATE")),
    "delete trigger on another table": (
        _DELETE_TRIGGER,
        _trigger_changed(_DELETE_TRIGGER, 0, "payments"),
    ),
}


@pytest.mark.parametrize("case", list(_WRONG_TRIGGERS))
@pytest.mark.parametrize("other_present", [True, False], ids=["other present", "other missing"])
def test_0039_upgrade_refuses_a_trigger_that_differs_before_any_write(
    monkeypatch, case: str, other_present: bool
) -> None:
    migration = _load()
    name, mutate = _WRONG_TRIGGERS[case]
    triggers = _design_triggers(migration)
    mutate(triggers)
    if not other_present:
        del triggers[_DELETE_TRIGGER if name == _UPDATE_TRIGGER else _UPDATE_TRIGGER]
    migration, fake, _, bind = _with(monkeypatch, _design_tables(), triggers, series_rows=[])

    with pytest.raises(RuntimeError) as raised:
        migration.upgrade()

    assert fake.calls == []
    assert not _prerequisite_asked(bind)
    message = str(raised.value)
    assert f"trigger {name} exists but is not the one 0039 builds" in message
    assert "No DDL was issued" in message


def test_0039_upgrade_refuses_a_trigger_of_ours_on_another_table_without_the_tables(
    monkeypatch,
) -> None:
    migration = _load()
    triggers = {_UPDATE_TRIGGER: ("payments", "BEFORE", "UPDATE", migration._TRIGGER_BODY)}
    migration, fake, _, _ = _with(monkeypatch, {}, triggers)

    with pytest.raises(RuntimeError, match=_UPDATE_TRIGGER):
        migration.upgrade()

    assert fake.calls == []


def _drop_index(name: str):
    def mutate(table: dict) -> None:
        table["indexes"] = [i for i in table["indexes"] if i["name"] != name]
        table["unique_constraints"] = [u for u in table["unique_constraints"] if u["name"] != name]

    return mutate


def _drop_tax_policy_index(table: dict) -> None:
    # What MySQL leaves without it: an index of its own for the foreign key.
    _drop_index(_TAX_POLICY_INDEX)(table)
    table["indexes"].append(
        {"name": _FK_TAX_POLICY, "column_names": ["tax_policy_version_id"], "unique": False}
    )


def _reorder_tenant_index(table: dict) -> None:
    for index in table["indexes"]:
        if index["name"] == _TENANT_ISSUED_INDEX:
            index["column_names"] = ["tenant_id", "id", "issued_at"]


def _unique_as_plain(table: dict) -> None:
    for index in table["indexes"]:
        if index["name"] == _SERIES_SEQUENCE_UNIQUE:
            index["unique"] = False
    table["unique_constraints"] = [
        u for u in table["unique_constraints"] if u["name"] != _SERIES_SEQUENCE_UNIQUE
    ]


def _prefix_length(table: dict) -> None:
    for index in table["indexes"]:
        if index["name"] == "uq_payment_receipts_receipt_number":
            index["dialect_options"] = {"mysql_length": {"receipt_number": 8}}


def _cascade(name: str):
    def mutate(table: dict) -> None:
        table["delete_rules"][name] = "CASCADE"
        for key in table["foreign_keys"]:
            if key["name"] == name:
                key["options"] = {"ondelete": "CASCADE"}

    return mutate


def _no_foreign_key(name: str):
    def mutate(table: dict) -> None:
        table["foreign_keys"] = [k for k in table["foreign_keys"] if k["name"] != name]
        del table["delete_rules"][name]

    return mutate


def _drop_check(name: str):
    def mutate(table: dict) -> None:
        table["check_constraints"] = [c for c in table["check_constraints"] if c["name"] != name]

    return mutate


def _extra_column(table: dict) -> None:
    table["columns"].append({"name": "pdf_path", "type": mysql.VARCHAR(255), "nullable": True})


def _column(name: str, **changes: object):
    def mutate(table: dict) -> None:
        for column in table["columns"]:
            if column["name"] == name:
                column.update(changes)

    return mutate


def _primary_key(*columns: str):
    def mutate(table: dict) -> None:
        table["primary_key"] = {"constrained_columns": list(columns), "name": None}

    return mutate


_WRONG_SHAPES: dict[str, tuple[str, object]] = {
    "receipts without the tax policy version index": (_RECEIPTS, _drop_tax_policy_index),
    "receipts without the payment unique": (_RECEIPTS, _drop_index(_PAYMENT_UNIQUE)),
    "receipts year and sequence unique a plain index": (_RECEIPTS, _unique_as_plain),
    "receipts tenant index in another order": (_RECEIPTS, _reorder_tenant_index),
    "receipts a prefix length": (_RECEIPTS, _prefix_length),
    "receipts payment foreign key not RESTRICT": (_RECEIPTS, _cascade(_FK_PAYMENT)),
    "receipts without the tenant foreign key": (_RECEIPTS, _no_foreign_key(_FK_TENANT)),
    "receipts without the amount check": (_RECEIPTS, _drop_check("ck_payment_receipts_amount")),
    "receipts with an extra column": (_RECEIPTS, _extra_column),
    "receipts sequence nullable": (_RECEIPTS, _column("sequence", nullable=True)),
    "receipts amount DECIMAL(20,2)": (
        _RECEIPTS,
        _column("amount", type=mysql.DECIMAL(precision=20, scale=2)),
    ),
    "receipts series_year an INTEGER": (_RECEIPTS, _column("series_year", type=mysql.INTEGER())),
    "receipts primary key on public_id": (_RECEIPTS, _primary_key("public_id")),
    "series last_sequence a BIGINT": (_SERIES, _column("last_sequence", type=mysql.BIGINT())),
    "series last_sequence nullable": (_SERIES, _column("last_sequence", nullable=True)),
    "series without the year check": (_SERIES, _drop_check("ck_receipt_number_series_year")),
    "series with an extra column": (_SERIES, _extra_column),
    "series without a primary key": (_SERIES, _primary_key()),
}


@pytest.mark.parametrize("shape", list(_WRONG_SHAPES))
@pytest.mark.parametrize("dialect", ["mysql", "sqlite"])
def test_0039_upgrade_refuses_a_wrong_shape_before_any_write(
    monkeypatch, shape: str, dialect: str
) -> None:
    """Design §2: a table there, any item missing, extra or different — nothing is built or
    written (not even a missing trigger or the series rows); the message names the table."""
    table_name, mutate = _WRONG_SHAPES[shape]
    tables = _design_tables()
    mutate(tables[table_name])
    migration, fake, _, bind = _with(monkeypatch, tables, {}, series_rows=[], dialect=dialect)

    with pytest.raises(RuntimeError) as raised:
        migration.upgrade()

    assert fake.calls == []
    assert not _prerequisite_asked(bind)
    message = str(raised.value)
    assert f"table {table_name} exists but differs" in message
    assert "No DDL was issued and no row was written" in message
    assert f"DROP TABLE {table_name}" in message
    assert "data incident" in message


@pytest.mark.parametrize("dialect", ["mysql", "sqlite"])
def test_0039_upgrade_refuses_series_year_auto_increment(monkeypatch, dialect: str) -> None:
    """Design §2: the shape check sees `series_year` AUTO_INCREMENT (information_schema on
    MySQL, the inspector elsewhere) although everything else is the design's."""
    tables = _design_tables()
    if dialect == "sqlite":
        _column("series_year", autoincrement=True)(tables[_SERIES])
    migration, fake, _, bind = _with(
        monkeypatch,
        tables,
        {},
        auto_increment=("series_year",) if dialect == "mysql" else (),
        dialect=dialect,
    )

    with pytest.raises(RuntimeError) as raised:
        migration.upgrade()

    assert fake.calls == []
    assert not _prerequisite_asked(bind)
    message = str(raised.value)
    assert f"table {_SERIES} exists but differs" in message
    assert "series_year is AUTO_INCREMENT" in message


def test_0039_upgrade_does_not_compare_server_defaults(monkeypatch) -> None:
    """Design §2: the shape check does not look at server defaults."""
    tables = _design_tables()
    _column("last_sequence", default="7")(tables[_SERIES])
    migration = _load()
    migration, fake, _, _ = _with(monkeypatch, tables, _design_triggers(migration))
    migration.upgrade()

    assert fake.calls == []


def _seed_without(year: int) -> list[tuple[int, int]]:
    return [row for row in _SEED_ROWS if row[0] != year]


def _seed_with(year: int, last: int) -> list[tuple[int, int]]:
    return [(y, last if y == year else value) for y, value in _SEED_ROWS]


# Design §2: series rows that are neither none nor the 74 design rows.
_WRONG_SERIES_ROWS: dict[str, list[tuple[int, int]]] = {
    "73 rows (no 2099)": _seed_without(2099),
    "75 rows (2100 too)": [*_SEED_ROWS, (2100, 0)],
    "2025 instead of 2099": [(2025, 0), *_seed_without(2099)],
    "2026 twice instead of 2099": [(2026, 0), *_seed_without(2099)],
    "last_sequence -1": _seed_with(2050, -1),
    "last_sequence 1000000": _seed_with(2050, 1000000),
    "one row only": [(2026, 0)],
}


@pytest.mark.parametrize("case", list(_WRONG_SERIES_ROWS))
@pytest.mark.parametrize("with_receipts", [False, True], ids=["no receipt", "receipts"])
def test_0039_upgrade_refuses_wrong_series_rows_before_any_write(
    monkeypatch, case: str, with_receipts: bool
) -> None:
    migration = _load()
    triggers = _design_triggers(migration)
    del triggers[_DELETE_TRIGGER]
    migration, fake, _, bind = _with(
        monkeypatch,
        _design_tables(),
        triggers,
        series_rows=_WRONG_SERIES_ROWS[case],
        receipts=[(2026, 1)] if with_receipts else [],
    )

    with pytest.raises(RuntimeError) as raised:
        migration.upgrade()

    # Not even the missing trigger.
    assert fake.calls == []
    assert not _prerequisite_asked(bind)
    message = str(raised.value)
    assert f"table {_SERIES} does not hold exactly one row for each year 2026–2099" in message
    assert "No DDL was issued and no row was written" in message
    assert "data incident" in message


@pytest.mark.parametrize("series", ["absent", "empty"])
def test_0039_upgrade_refuses_receipts_without_series_rows(monkeypatch, series: str) -> None:
    """Design §2: `payment_receipts` holds rows while the series table does not exist or has no
    row — refuse: no table built, no row inserted (that would number from 0 again)."""
    migration = _load()
    tables = _design_tables()
    if series == "absent":
        del tables[_SERIES]
    migration, fake, _, bind = _with(
        monkeypatch, tables, _design_triggers(migration), series_rows=[], receipts=[(2026, 1)]
    )

    with pytest.raises(RuntimeError) as raised:
        migration.upgrade()

    assert fake.calls == []
    assert not _prerequisite_asked(bind)
    message = str(raised.value)
    state = "does not exist" if series == "absent" else "has no row"
    assert f"table {_SERIES} {state} while {_RECEIPTS} holds receipts" in message
    assert "No DDL was issued and no row was written" in message


def test_0039_upgrade_refuses_a_year_behind_its_highest_sequence(monkeypatch) -> None:
    """Design §2: 74 rows, but 2026's `last_sequence` is below MAX(sequence) of its receipts —
    refuse naming the year; 2027 (equal) is not named."""
    migration = _load()
    rows = [(y, {2026: 2, 2027: 1}.get(y, 0)) for y, _ in _SEED_ROWS]
    migration, fake, _, bind = _with(
        monkeypatch,
        _design_tables(),
        _design_triggers(migration),
        series_rows=rows,
        receipts=[(2026, 1), (2026, 3), (2027, 1)],
    )

    with pytest.raises(RuntimeError) as raised:
        migration.upgrade()

    assert fake.calls == []
    assert not _prerequisite_asked(bind)
    message = str(raised.value)
    assert (
        f"last_sequence in table {_SERIES} is below MAX(sequence) in {_RECEIPTS} for 2026"
        in message
    )
    assert "2027" not in message.split("for 2026")[1].split(".")[0]


@pytest.mark.parametrize("last", [3, 7], ids=["equal", "above"])
def test_0039_upgrade_accepts_counters_at_or_above_the_receipts(monkeypatch, last: int) -> None:
    migration = _load()
    migration, fake, _, bind = _with(
        monkeypatch,
        _design_tables(),
        _design_triggers(migration),
        series_rows=_seed_with(2026, last),
        receipts=[(2026, 1), (2026, 3)],
    )
    migration.upgrade()

    assert fake.calls == []
    assert _MAX_SEQUENCES_SQL in bind.queries


def test_0039_upgrade_on_sqlite_creates_both_tables_without_triggers(monkeypatch) -> None:
    migration, fake, _, bind = _with(monkeypatch, {}, dialect="sqlite")
    migration.upgrade()

    assert fake.calls == [
        ("create_table", (_SERIES,)),
        ("execute", (migration._INSERT_SERIES,)),
        ("create_table", (_RECEIPTS,)),
        (
            "create_index",
            (_TENANT_ISSUED_INDEX, _RECEIPTS, ["tenant_id", "issued_at", "id"], False),
        ),
        ("create_index", (_TAX_POLICY_INDEX, _RECEIPTS, ["tax_policy_version_id"], False)),
    ]
    assert bind.queries == []


def test_0039_offline_upgrade_emits_everything_without_reading(monkeypatch) -> None:
    migration = _load()
    migration, fake, inspector, bind = _with(
        monkeypatch, _design_tables(), _design_triggers(migration), as_sql=True
    )
    migration.upgrade()

    assert inspector.asked == []
    assert bind.queries == []
    assert _executed(fake) == _FULL_UPGRADE


def test_0039_offline_downgrade_refuses_before_reading_or_ddl(monkeypatch) -> None:
    """Design §8: offline the emptiness cannot be checked — refuse, no DROP."""
    migration = _load()
    migration, fake, inspector, bind = _with(
        monkeypatch, _design_tables(), _design_triggers(migration), as_sql=True
    )

    with pytest.raises(RuntimeError) as raised:
        migration.downgrade()

    assert fake.calls == []
    assert inspector.asked == []
    assert bind.queries == []
    message = str(raised.value)
    assert _RECEIPTS in message
    assert "online" in message


def test_0039_downgrade_drops_both_triggers_then_both_empty_tables(monkeypatch) -> None:
    migration = _load()
    migration, fake, _, bind = _with(monkeypatch, _design_tables(), _design_triggers(migration))
    migration.downgrade()

    assert _HAS_RECEIPTS_SQL in bind.queries
    assert fake.calls == [
        ("execute", (f"DROP TRIGGER {_UPDATE_TRIGGER}",)),
        ("execute", (f"DROP TRIGGER {_DELETE_TRIGGER}",)),
        ("drop_table", (_RECEIPTS,)),
        ("drop_table", (_SERIES,)),
    ]


def test_0039_downgrade_drops_only_the_triggers_that_exist(monkeypatch) -> None:
    migration = _load()
    triggers = _design_triggers(migration)
    del triggers[_UPDATE_TRIGGER]
    migration, fake, _, _ = _with(monkeypatch, _design_tables(), triggers)
    migration.downgrade()

    assert fake.calls == [
        ("execute", (f"DROP TRIGGER {_DELETE_TRIGGER}",)),
        ("drop_table", (_RECEIPTS,)),
        ("drop_table", (_SERIES,)),
    ]


def test_0039_downgrade_on_sqlite_drops_both_empty_tables(monkeypatch) -> None:
    migration, fake, _, bind = _with(monkeypatch, _design_tables(), dialect="sqlite")
    migration.downgrade()

    assert bind.queries == [_HAS_RECEIPTS_SQL]
    assert fake.calls == [("drop_table", (_RECEIPTS,)), ("drop_table", (_SERIES,))]


@pytest.mark.parametrize("dialect", ["mysql", "sqlite"])
def test_0039_downgrade_refuses_with_a_receipt_before_any_ddl(monkeypatch, dialect: str) -> None:
    """Design §8: a receipt — no DDL, no row deleted (not the series table either); the message
    names the table, that it is not empty, and to roll back only the application image."""
    migration = _load()
    migration, fake, _, _ = _with(
        monkeypatch,
        _design_tables(),
        _design_triggers(migration),
        receipts=[(2026, 1)],
        dialect=dialect,
    )

    with pytest.raises(RuntimeError) as raised:
        migration.downgrade()

    assert fake.calls == []
    message = str(raised.value)
    assert f"table {_RECEIPTS} is not empty" in message
    assert "No DDL was issued and no row was deleted" in message
    assert "roll back only the application image" in message


@pytest.mark.parametrize("dialect", ["mysql", "sqlite"])
def test_0039_downgrade_rerun_drops_the_series_left_behind(monkeypatch, dialect: str) -> None:
    """Design §8: a downgrade stopped between the two DROP TABLE — `payment_receipts` gone,
    the series table there: the rerun drops the series table (not 0035's "do nothing")."""
    migration, fake, _, bind = _with(
        monkeypatch, {_SERIES: _design_table(_SERIES)}, dialect=dialect
    )
    migration.downgrade()

    assert bind.queries == []
    assert fake.calls == [("drop_table", (_SERIES,))]


@pytest.mark.parametrize("dialect", ["mysql", "sqlite"])
def test_0039_downgrade_without_either_table_does_nothing(monkeypatch, dialect: str) -> None:
    migration, fake, inspector, bind = _with(monkeypatch, {}, dialect=dialect)
    migration.downgrade()

    assert sorted(inspector.asked) == sorted((table, "has_table") for table in _TABLES)
    assert bind.queries == []
    assert fake.calls == []


# ---------------------------------------------------------------------------
# SQLite (the models' metadata)
# ---------------------------------------------------------------------------


@contextmanager
def _sqlite() -> Iterator[Engine]:
    """The unit tests build SQLite with `create_all` from the models, not with the migrations
    (and with no series row). SQLite does not enforce foreign keys by default: the parent tables
    are not needed."""
    engine = create_engine("sqlite+pysqlite:///:memory:")
    try:
        Base.metadata.create_all(
            engine, tables=[ReceiptNumberSeries.__table__, PaymentReceipt.__table__]
        )
        yield engine
    finally:
        engine.dispose()


def test_0039_sqlite_tables_get_the_indexes_and_the_foreign_keys() -> None:
    with _sqlite() as engine:
        inspector = inspect(engine)
        for table in _TABLES:
            indexes = {
                str(index["name"]): (tuple(index["column_names"]), bool(index["unique"]))
                for index in inspector.get_indexes(table)
            }
            for unique in inspector.get_unique_constraints(table):
                indexes[str(unique["name"])] = (tuple(unique["column_names"]), True)
            keys = {
                str(key["name"]): (
                    tuple(key["constrained_columns"]),
                    str(key["referred_table"]),
                    tuple(key["referred_columns"]),
                    str(key["options"].get("ondelete")).upper(),
                )
                for key in inspector.get_foreign_keys(table)
            }
            checks = {str(c["name"]) for c in inspector.get_check_constraints(table)}
            primary_key = tuple(inspector.get_pk_constraint(table)["constrained_columns"])

            assert indexes == _DESIGN_INDEXES[table], table
            assert keys == _DESIGN_FOREIGN_KEYS[table], table
            assert checks == set(_DESIGN_CHECK_TEXTS[table]), table
            assert primary_key == _DESIGN_PRIMARY_KEYS[table], table
        with engine.connect() as connection:
            assert int(connection.execute(_SERIES_COUNT).scalar_one()) == 0


@pytest.mark.parametrize("row", list(_LEGAL_RECEIPTS))
def test_0039_sqlite_takes_the_legal_receipts(row: str) -> None:
    with _sqlite() as engine:
        with engine.begin() as connection:
            _insert_receipt(connection, _LEGAL_RECEIPTS[row])
            assert int(connection.execute(_RECEIPT_COUNT).scalar_one()) == 1


@pytest.mark.parametrize("row", list(_ILLEGAL_RECEIPTS))
def test_0039_sqlite_checks_refuse_the_illegal_receipts(row: str) -> None:
    overrides, _ = _ILLEGAL_RECEIPTS[row]
    with _sqlite() as engine:
        with pytest.raises(DBAPIError) as raised:
            with engine.begin() as connection:
                _insert_receipt(connection, overrides)
        assert "CHECK constraint failed" in str(raised.value), row

        with engine.connect() as connection:
            assert int(connection.execute(_RECEIPT_COUNT).scalar_one()) == 0


@pytest.mark.parametrize("case", list(_DUPLICATES))
def test_0039_sqlite_unique_constraints_refuse_a_second_receipt(case: str) -> None:
    overrides, _, column = _DUPLICATES[case]
    with _sqlite() as engine:
        with engine.begin() as connection:
            _insert_receipt(connection, _FIRST_RECEIPT, 1)
        with pytest.raises(IntegrityError) as raised:
            with engine.begin() as connection:
                _insert_receipt(connection, overrides, 2)
        message = str(raised.value)
        assert "UNIQUE constraint failed" in message, case
        assert f"{_RECEIPTS}.{column}" in message, case

        with engine.connect() as connection:
            assert int(connection.execute(_RECEIPT_COUNT).scalar_one()) == 1


def test_0039_sqlite_series_takes_the_legal_rows_and_the_default() -> None:
    with _sqlite() as engine:
        with engine.begin() as connection:
            for row in _LEGAL_SERIES.values():
                connection.execute(_INSERT_SERIES_ROW, row)
            # `last_sequence` defaults to 0.
            connection.execute(text(f"INSERT INTO {_SERIES} (series_year) VALUES (2030)"))
            rows = [tuple(r) for r in connection.execute(_SERIES_ROWS)]

    assert rows == [(2026, 0), (2030, 0), (2099, 999999)]


@pytest.mark.parametrize("row", list(_ILLEGAL_SERIES))
def test_0039_sqlite_series_checks_refuse_the_illegal_rows(row: str) -> None:
    values, _ = _ILLEGAL_SERIES[row]
    with _sqlite() as engine:
        with pytest.raises(DBAPIError) as raised:
            with engine.begin() as connection:
                connection.execute(_INSERT_SERIES_ROW, values)
        assert "CHECK constraint failed" in str(raised.value), row

        with engine.connect() as connection:
            assert int(connection.execute(_SERIES_COUNT).scalar_one()) == 0


def test_0039_sqlite_series_refuses_a_year_twice() -> None:
    with _sqlite() as engine:
        with engine.begin() as connection:
            connection.execute(_INSERT_SERIES_ROW, {"series_year": 2026, "last_sequence": 0})
        with pytest.raises(IntegrityError):
            with engine.begin() as connection:
                connection.execute(_INSERT_SERIES_ROW, {"series_year": 2026, "last_sequence": 1})


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


_WRITES = {"ALTER", "CREATE", "DELETE", "DROP", "INSERT", "RENAME", "TRUNCATE", "UPDATE"}


@contextmanager
def _statements() -> Iterator[list[str]]:
    """Every statement that any engine (Alembic's own included) sends while the block runs."""
    statements: list[str] = []

    def record(_conn, _cursor, statement, _params, _context, _many) -> None:
        statements.append(statement)

    event.listen(Engine, "before_cursor_execute", record)
    try:
        yield statements
    finally:
        event.remove(Engine, "before_cursor_execute", record)


def _writes(statements: list[str]) -> list[str]:
    """DDL and row writes, `alembic_version` aside."""
    return [
        s
        for s in statements
        if _normalised(s).split(" ")[0] in _WRITES and "ALEMBIC_VERSION" not in _normalised(s)
    ]


def _heads(statements: list[str]) -> list[str]:
    """`CREATE TABLE RECEIPT_NUMBER_SERIES`, `INSERT INTO …`, `CREATE TRIGGER TRG_…`, …"""
    return [" ".join(_normalised(s).split(" ")[:3]) for s in _writes(statements)]


def _asked_prerequisite(statements: list[str]) -> bool:
    return any("@@LOG_BIN" in _normalised(s) for s in statements)


def _version(engine: Engine) -> str:
    with engine.connect() as connection:
        return str(connection.execute(_ALEMBIC_VERSION).scalar_one())


def _execute(engine: Engine, statement: str, params: dict[str, object] | None = None) -> None:
    with engine.begin() as connection:
        connection.execute(text(statement), params or {})


def _present(engine: Engine, table: str) -> bool:
    return inspect(engine).has_table(table)


def _truncate(engine: Engine) -> None:
    """The triggers refuse DELETE of a receipt: TRUNCATE (DDL, no triggers) with foreign key
    checks off. The triggers are never dropped for this."""
    with engine.begin() as connection:
        connection.exec_driver_sql("SET FOREIGN_KEY_CHECKS = 0")
        try:
            connection.exec_driver_sql(f"TRUNCATE TABLE {_RECEIPTS}")
        finally:
            connection.exec_driver_sql("SET FOREIGN_KEY_CHECKS = 1")


def _series_rows(engine: Engine) -> list[tuple[int, int]]:
    with engine.connect() as connection:
        return [(int(y), int(s)) for y, s in connection.execute(_SERIES_ROWS)]


def _auto_increment(engine: Engine, table: str) -> set[str]:
    with engine.connect() as connection:
        return {
            str(n) for n in connection.execute(_AUTO_INCREMENT_QUERY, {"table": table}).scalars()
        }


def _table_shape(engine: Engine, table: str) -> dict[str, object]:
    """A table as reflected, in the migration's terms; delete rules from information_schema;
    prefix lengths kept so they would show."""
    inspector = inspect(engine)
    with engine.connect() as connection:
        rules = dict(connection.execute(_DELETE_RULES_QUERY, {"table": table}).all())
    indexes: dict[str, object] = {}
    for index in inspector.get_indexes(table):
        prefix = (index.get("dialect_options") or {}).get("mysql_length")
        shape = (tuple(index["column_names"]), bool(index["unique"]))
        indexes[str(index["name"])] = shape if not prefix else (*shape, repr(prefix))
    return {
        "columns": {
            str(c["name"]): (_type_text(c["type"]), bool(c["nullable"]))
            for c in inspector.get_columns(table)
        },
        "primary_key": tuple(inspector.get_pk_constraint(table)["constrained_columns"]),
        "indexes": indexes,
        "foreign_keys": {
            str(key["name"]): (
                tuple(key["constrained_columns"]),
                str(key["referred_table"]),
                tuple(key["referred_columns"]),
                rules.get(key["name"]),
            )
            for key in inspector.get_foreign_keys(table)
        },
        "checks": {str(check["name"]) for check in inspector.get_check_constraints(table)},
    }


def _our_triggers(engine: Engine) -> dict[str, tuple[str, str, str, str]]:
    """Triggers by our two names (wherever they are): (table, timing, event, stripped body)."""
    with engine.connect() as connection:
        rows = connection.execute(_ALL_TRIGGERS_QUERY).all()
    return {
        str(name): (str(table), str(timing), str(kind), str(body).strip())
        for name, table, timing, kind, body in rows
        if name in _DESIGN_TRIGGERS
    }


def _assert_design_shape(engine: Engine) -> None:
    """Both tables reflected = the models' = the design's; `series_year` not AUTO_INCREMENT;
    both triggers on payment_receipts with the design's timing, event and body."""
    migration = _load()
    for table in _TABLES:
        assert _table_shape(engine, table) == _model_shape(table) == _design_shape(table), table
    assert _auto_increment(engine, _SERIES) == set()
    assert _our_triggers(engine) == {
        name: (*row, migration._TRIGGER_BODY) for name, row in _DESIGN_TRIGGERS.items()
    }


def _ours(engine: Engine) -> dict[str, object]:
    """Both tables' shapes and rows (those that exist) and our triggers."""
    state: dict[str, object] = {"triggers": _our_triggers(engine)}
    for table in _TABLES:
        if _present(engine, table):
            with engine.connect() as connection:
                rows = [tuple(r) for r in connection.execute(text(f"SELECT * FROM {table}"))]
            state[table] = (_table_shape(engine, table), sorted(rows, key=repr))
    return state


def _schema_snapshot(engine: Engine) -> dict[str, object]:
    """Every table's columns, indexes (with prefix lengths), unique constraints and foreign keys,
    with every trigger and check in the schema."""
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
        triggers = {tuple(row) for row in connection.execute(_ALL_TRIGGERS_QUERY).all()}
        checks = {tuple(row) for row in connection.execute(_CHECKS_QUERY).all()}
    return {"tables": tables, "triggers": triggers, "checks": checks}


def _without_ours(snapshot: dict[str, object]) -> dict[str, object]:
    tables = snapshot["tables"]
    assert isinstance(tables, dict)
    triggers = snapshot["triggers"]
    assert isinstance(triggers, set)
    checks = snapshot["checks"]
    assert isinstance(checks, set)
    return {
        "tables": {name: shape for name, shape in tables.items() if name not in _TABLES},
        "triggers": {trigger for trigger in triggers if trigger[1] != _RECEIPTS},
        "checks": {check for check in checks if check[0] not in _TABLES},
    }


def _ledger_and_wallets(engine: Engine) -> tuple[list[tuple[object, ...]], ...]:
    with engine.connect() as connection:
        wallets = [tuple(row) for row in connection.execute(_WALLET_ROWS).all()]
        ledger = [tuple(row) for row in connection.execute(_LEDGER_ROWS).all()]
    return wallets, ledger


def _at_0038(alembic_config: Config, engine: Engine) -> None:
    command.upgrade(alembic_config, "head")
    command.downgrade(alembic_config, _DOWN_REVISION)
    assert _version(engine) == _DOWN_REVISION
    for table in _TABLES:
        assert not _present(engine, table), table
    assert _our_triggers(engine) == {}


def _at_0039(alembic_config: Config, engine: Engine) -> None:
    command.upgrade(alembic_config, "head")
    assert _version(engine) == _REVISION
    assert _series_rows(engine) == _SEED_ROWS


def _back_to_head(alembic_config: Config, engine: Engine) -> None:
    """Empties `payment_receipts`, puts `alembic_version` back to 0038, drops both tables (and
    with them the triggers, hand-made or not) and upgrades to head again: the database ends at
    head with the 74 design rows and no receipt."""
    if _present(engine, _RECEIPTS):
        _truncate(engine)
    if _version(engine) == _REVISION:
        _execute(engine, _SET_VERSION, {"version": _DOWN_REVISION})
    if _version(engine) == _DOWN_REVISION:
        for table in (_RECEIPTS, _SERIES):
            if _present(engine, table):
                _execute(engine, f"DROP TABLE {table}")
    command.upgrade(alembic_config, "head")
    assert _series_rows(engine) == _SEED_ROWS


def _insert_committed(engine: Engine, rows: list[dict[str, object]]) -> list[int]:
    """Receipts written with foreign key checks off (no payment, tenant or tax policy version is
    needed for this); returns their ids."""
    ids = []
    with engine.begin() as connection:
        connection.exec_driver_sql("SET FOREIGN_KEY_CHECKS = 0")
        try:
            for n, overrides in enumerate(rows, start=1):
                ids.append(_insert_receipt(connection, overrides, n))
        finally:
            connection.exec_driver_sql("SET FOREIGN_KEY_CHECKS = 1")
    return ids


@needs_mysql
def test_0039_round_trip(alembic_config: Config, engine: Engine) -> None:
    """Design §7 "迁移": 0038 → 0039 → 0038 → 0039. The upgrade sends the series `CREATE TABLE`,
    the one `INSERT` of 74 rows, the receipts `CREATE TABLE` and the two `CREATE TRIGGER`; the
    shapes are the models' and the triggers the design's; the downgrade drops both triggers,
    then the receipts table, then the series table; every other table, key, check and trigger
    and the wallet and ledger rows are the same throughout."""
    _at_0038(alembic_config, engine)
    try:
        at_0038 = _schema_snapshot(engine)
        rows = _ledger_and_wallets(engine)

        with _statements() as statements:
            command.upgrade(alembic_config, _REVISION)
        assert _heads(statements) == [
            f"CREATE TABLE {_SERIES.upper()}",
            f"INSERT INTO {_SERIES.upper()}",
            f"CREATE TABLE {_RECEIPTS.upper()}",
            f"CREATE TRIGGER {_UPDATE_TRIGGER.upper()}",
            f"CREATE TRIGGER {_DELETE_TRIGGER.upper()}",
        ]
        assert _asked_prerequisite(statements)
        assert _version(engine) == _REVISION
        _assert_design_shape(engine)
        assert _series_rows(engine) == _SEED_ROWS
        with engine.connect() as connection:
            assert int(connection.execute(_RECEIPT_COUNT).scalar_one()) == 0
        at_0039 = _schema_snapshot(engine)
        assert _without_ours(at_0039) == at_0038
        assert _ledger_and_wallets(engine) == rows

        with _statements() as statements:
            command.downgrade(alembic_config, _DOWN_REVISION)
        assert [_normalised(s) for s in _writes(statements)] == [
            f"DROP TRIGGER {_UPDATE_TRIGGER.upper()}",
            f"DROP TRIGGER {_DELETE_TRIGGER.upper()}",
            f"DROP TABLE {_RECEIPTS.upper()}",
            f"DROP TABLE {_SERIES.upper()}",
        ]
        assert _version(engine) == _DOWN_REVISION
        assert _schema_snapshot(engine) == at_0038
        assert _ledger_and_wallets(engine) == rows

        command.upgrade(alembic_config, _REVISION)
        assert _schema_snapshot(engine) == at_0039
        _assert_design_shape(engine)
        assert _series_rows(engine) == _SEED_ROWS
        assert _ledger_and_wallets(engine) == rows
    finally:
        _back_to_head(alembic_config, engine)


@needs_mysql
def test_0039_rerun_with_everything_built_only_moves_the_version(
    alembic_config: Config, engine: Engine
) -> None:
    """Upgraded, then `alembic_version` put back to 0038 (as if writing the version failed
    after everything was built) — no DDL, no row written, no prerequisite check; the version
    moves to 0039 and the schema and the series rows are as after the first upgrade."""
    _at_0039(alembic_config, engine)
    try:
        after_first = _schema_snapshot(engine)
        _execute(engine, _SET_VERSION, {"version": _DOWN_REVISION})

        with _statements() as statements:
            command.upgrade(alembic_config, _REVISION)

        assert _writes(statements) == []
        assert not _asked_prerequisite(statements)
        assert _version(engine) == _REVISION
        assert _schema_snapshot(engine) == after_first
        assert _series_rows(engine) == _SEED_ROWS
        _assert_design_shape(engine)
    finally:
        _back_to_head(alembic_config, engine)


def _create(table: str, clauses: list[str]) -> str:
    return f"CREATE TABLE {table} ({', '.join(clauses)})"


def _without(clauses: list[str], prefix: str) -> list[str]:
    kept = [clause for clause in clauses if not clause.startswith(prefix)]
    assert len(kept) == len(clauses) - 1, prefix
    return kept


def _replaced(clauses: list[str], old: str, new: str) -> list[str]:
    assert sum(old in clause for clause in clauses) == 1, old
    return [clause.replace(old, new) for clause in clauses]


# Design §7 "迁移" (形状一致 / 不一致): each a fresh hand-made table at 0038.
_WRONG_SHAPE_DDL: dict[str, tuple[str, str]] = {
    # MySQL refuses a CHECK on an AUTO_INCREMENT column (3818): without the year check.
    "series_year AUTO_INCREMENT": (
        _SERIES,
        _create(
            _SERIES,
            _replaced(
                _without(_SERIES_CLAUSES, "CONSTRAINT ck_receipt_number_series_year "),
                "series_year SMALLINT NOT NULL",
                "series_year SMALLINT NOT NULL AUTO_INCREMENT",
            ),
        ),
    ),
    "series last_sequence a BIGINT": (
        _SERIES,
        _create(
            _SERIES,
            _replaced(
                _SERIES_CLAUSES,
                "last_sequence INT NOT NULL DEFAULT 0",
                "last_sequence BIGINT NOT NULL DEFAULT 0",
            ),
        ),
    ),
    "series without the sequence check": (
        _SERIES,
        _create(
            _SERIES, _without(_SERIES_CLAUSES, "CONSTRAINT ck_receipt_number_series_sequence ")
        ),
    ),
    # MySQL then builds an index of its own, named after the foreign key.
    "receipts without the tax policy version index": (
        _RECEIPTS,
        _create(_RECEIPTS, _without(_RECEIPT_CLAUSES, f"INDEX {_TAX_POLICY_INDEX} ")),
    ),
    "receipts payment foreign key not RESTRICT": (
        _RECEIPTS,
        _create(
            _RECEIPTS,
            _replaced(
                _RECEIPT_CLAUSES,
                "REFERENCES payments (id) ON DELETE RESTRICT",
                "REFERENCES payments (id) ON DELETE CASCADE",
            ),
        ),
    ),
    "receipts year and sequence unique a plain index": (
        _RECEIPTS,
        _create(
            _RECEIPTS,
            _replaced(
                _RECEIPT_CLAUSES,
                f"CONSTRAINT {_SERIES_SEQUENCE_UNIQUE} UNIQUE (series_year, sequence)",
                f"INDEX {_SERIES_SEQUENCE_UNIQUE} (series_year, sequence)",
            ),
        ),
    ),
    "receipts without the amount check": (
        _RECEIPTS,
        _create(_RECEIPTS, _without(_RECEIPT_CLAUSES, "CONSTRAINT ck_payment_receipts_amount ")),
    ),
    "receipts amount with two decimals": (
        _RECEIPTS,
        _create(
            _RECEIPTS,
            _replaced(
                _RECEIPT_CLAUSES, "amount DECIMAL(20, 8) NOT NULL", "amount DECIMAL(20, 2) NOT NULL"
            ),
        ),
    ),
}


@needs_mysql
@pytest.mark.parametrize("shape", list(_WRONG_SHAPE_DDL))
def test_0039_rerun_with_a_wrong_shape_fails_before_any_write(
    alembic_config: Config, engine: Engine, shape: str
) -> None:
    """Version 0038 and a fresh hand-made table — the upgrade fails naming the table, issues no
    DDL and writes no row (no trigger, no series row either), the version stays 0038 and the
    table is as it was."""
    table, ddl = _WRONG_SHAPE_DDL[shape]
    _at_0038(alembic_config, engine)
    try:
        _execute(engine, ddl)
        hand_made = _ours(engine)

        with _statements() as statements:
            with pytest.raises(RuntimeError) as raised:
                command.upgrade(alembic_config, _REVISION)

        assert _writes(statements) == []
        assert not _asked_prerequisite(statements)
        message = str(raised.value)
        assert f"table {table} exists but differs" in message
        assert "No DDL was issued and no row was written" in message
        assert "data incident" in message
        if shape == "series_year AUTO_INCREMENT":
            assert "series_year is AUTO_INCREMENT" in message
        assert _version(engine) == _DOWN_REVISION
        assert _ours(engine) == hand_made
    finally:
        _back_to_head(alembic_config, engine)


@needs_mysql
@pytest.mark.parametrize("missing", [_UPDATE_TRIGGER, _DELETE_TRIGGER])
def test_0039_upgrade_builds_only_the_missing_trigger_on_mysql(
    alembic_config: Config, engine: Engine, missing: str
) -> None:
    """Version 0038, everything built and one trigger dropped — the upgrade sends only that one
    `CREATE TRIGGER` (after the prerequisite check); the version is 0039; all is the design's."""
    migration = _load()
    _at_0039(alembic_config, engine)
    try:
        _execute(engine, f"DROP TRIGGER {missing}")
        _execute(engine, _SET_VERSION, {"version": _DOWN_REVISION})

        with _statements() as statements:
            command.upgrade(alembic_config, _REVISION)

        assert [_compact(s) for s in _writes(statements)] == [
            _compact(migration._TRIGGERS[missing])
        ]
        assert _asked_prerequisite(statements)
        assert _version(engine) == _REVISION
        _assert_design_shape(engine)
        assert _series_rows(engine) == _SEED_ROWS
    finally:
        _back_to_head(alembic_config, engine)


@needs_mysql
def test_0039_upgrade_with_only_the_series_rows_missing_on_mysql(
    alembic_config: Config, engine: Engine
) -> None:
    """Design §2 / §7 "只缺系列行（不跑预检）": version 0038, everything built, the series
    emptied and no receipt — the upgrade sends the one `INSERT` and nothing else (no DDL, no
    prerequisite check); the 74 rows are back."""
    _at_0039(alembic_config, engine)
    try:
        _execute(engine, f"DELETE FROM {_SERIES}")
        _execute(engine, _SET_VERSION, {"version": _DOWN_REVISION})
        before = _schema_snapshot(engine)

        with _statements() as statements:
            command.upgrade(alembic_config, _REVISION)

        assert [_compact(s) for s in _writes(statements)] == [_compact(_DESIGN_INSERT)]
        assert not _asked_prerequisite(statements)
        assert _version(engine) == _REVISION
        assert _series_rows(engine) == _SEED_ROWS
        assert _schema_snapshot(engine) == before
    finally:
        _back_to_head(alembic_config, engine)


@needs_mysql
def test_0039_upgrade_after_the_series_insert_was_lost_on_mysql(
    alembic_config: Config, engine: Engine
) -> None:
    """The failure window "系列表建成、INSERT 提交之前中断": version 0038, an empty series table
    of the design's shape and no receipts table — the rerun inserts the rows and builds the
    rest."""
    _at_0038(alembic_config, engine)
    try:
        _execute(engine, _create(_SERIES, _SERIES_CLAUSES))

        with _statements() as statements:
            command.upgrade(alembic_config, _REVISION)

        assert _heads(statements) == [
            f"INSERT INTO {_SERIES.upper()}",
            f"CREATE TABLE {_RECEIPTS.upper()}",
            f"CREATE TRIGGER {_UPDATE_TRIGGER.upper()}",
            f"CREATE TRIGGER {_DELETE_TRIGGER.upper()}",
        ]
        assert _asked_prerequisite(statements)
        assert _version(engine) == _REVISION
        _assert_design_shape(engine)
        assert _series_rows(engine) == _SEED_ROWS
    finally:
        _back_to_head(alembic_config, engine)


@needs_mysql
def test_0039_upgrade_refuses_wrong_series_rows_on_mysql(
    alembic_config: Config, engine: Engine
) -> None:
    """Version 0038, everything built, the 2099 row gone (73 rows) — refused, no DDL, no row;
    the rows stay as they were."""
    _at_0039(alembic_config, engine)
    try:
        _execute(engine, f"DELETE FROM {_SERIES} WHERE series_year = 2099")
        _execute(engine, _SET_VERSION, {"version": _DOWN_REVISION})
        before = _ours(engine)

        with _statements() as statements:
            with pytest.raises(RuntimeError) as raised:
                command.upgrade(alembic_config, _REVISION)

        assert _writes(statements) == []
        assert f"table {_SERIES} does not hold exactly one row" in str(raised.value)
        assert _version(engine) == _DOWN_REVISION
        assert _ours(engine) == before
    finally:
        _back_to_head(alembic_config, engine)


@needs_mysql
@pytest.mark.parametrize("series", ["absent", "empty"])
def test_0039_rerun_with_receipts_and_no_series_rows_fails_on_mysql(
    alembic_config: Config, engine: Engine, series: str
) -> None:
    """Design §2 / §7: a receipt there, the series table dropped or emptied, version 0038 — the
    upgrade fails, builds no table, inserts no row; the version stays 0038 and everything is as
    it was."""
    _at_0039(alembic_config, engine)
    try:
        _insert_committed(engine, [{}])
        if series == "absent":
            _execute(engine, f"DROP TABLE {_SERIES}")
        else:
            _execute(engine, f"DELETE FROM {_SERIES}")
        _execute(engine, _SET_VERSION, {"version": _DOWN_REVISION})
        before = _ours(engine)
        before_schema = _schema_snapshot(engine)

        with _statements() as statements:
            with pytest.raises(RuntimeError) as raised:
                command.upgrade(alembic_config, _REVISION)

        assert _writes(statements) == []
        assert not _asked_prerequisite(statements)
        message = str(raised.value)
        state = "does not exist" if series == "absent" else "has no row"
        assert f"table {_SERIES} {state} while {_RECEIPTS} holds receipts" in message
        assert "No DDL was issued and no row was written" in message
        assert _version(engine) == _DOWN_REVISION
        assert _ours(engine) == before
        assert _schema_snapshot(engine) == before_schema
    finally:
        _back_to_head(alembic_config, engine)


@needs_mysql
def test_0039_rerun_with_a_year_behind_its_receipts_fails_on_mysql(
    alembic_config: Config, engine: Engine
) -> None:
    """Design §2 / §7: receipts 2026-000001..000003 there and 2026's `last_sequence` 2, version
    0038 — the upgrade fails naming 2026, no DDL, no row; with `last_sequence` 3 the rerun only
    moves the version."""
    _at_0039(alembic_config, engine)
    try:
        _insert_committed(engine, [{}, {}, {}])
        _execute(engine, f"UPDATE {_SERIES} SET last_sequence = 2 WHERE series_year = 2026")
        _execute(engine, _SET_VERSION, {"version": _DOWN_REVISION})
        before = _ours(engine)

        with _statements() as statements:
            with pytest.raises(RuntimeError) as raised:
                command.upgrade(alembic_config, _REVISION)

        assert _writes(statements) == []
        message = str(raised.value)
        assert f"is below MAX(sequence) in {_RECEIPTS} for 2026" in message
        assert "No DDL was issued and no row was written" in message
        assert _version(engine) == _DOWN_REVISION
        assert _ours(engine) == before

        _execute(engine, f"UPDATE {_SERIES} SET last_sequence = 3 WHERE series_year = 2026")
        with _statements() as statements:
            command.upgrade(alembic_config, _REVISION)
        assert _writes(statements) == []
        assert _version(engine) == _REVISION
    finally:
        _back_to_head(alembic_config, engine)


@needs_mysql
def test_0039_downgrade_with_a_receipt_refuses_before_any_ddl(
    alembic_config: Config, engine: Engine
) -> None:
    """Design §8: a receipt — the downgrade fails naming the table, no DDL, both tables, their
    rows and the triggers are as they were, `alembic_version` stays 0039."""
    _at_0039(alembic_config, engine)
    try:
        _insert_committed(engine, [{}])
        before_schema = _schema_snapshot(engine)
        before = _ours(engine)

        with _statements() as statements:
            with pytest.raises(RuntimeError) as raised:
                command.downgrade(alembic_config, _DOWN_REVISION)

        assert _writes(statements) == []
        message = str(raised.value)
        assert f"table {_RECEIPTS} is not empty" in message
        assert "roll back only the application image" in message
        assert _version(engine) == _REVISION
        assert _schema_snapshot(engine) == before_schema
        assert _ours(engine) == before
    finally:
        _back_to_head(alembic_config, engine)


@needs_mysql
def test_0039_downgrade_rerun_after_stopping_between_the_drops(
    alembic_config: Config, engine: Engine
) -> None:
    """Design §8: `payment_receipts` already dropped (with its triggers), the series table still
    there, version 0039 — the rerun sends the one `DROP TABLE receipt_number_series`; both
    tables are gone and the version is 0038."""
    _at_0039(alembic_config, engine)
    try:
        _execute(engine, f"DROP TABLE {_RECEIPTS}")

        with _statements() as statements:
            command.downgrade(alembic_config, _DOWN_REVISION)

        assert [_normalised(s) for s in _writes(statements)] == [f"DROP TABLE {_SERIES.upper()}"]
        assert _version(engine) == _DOWN_REVISION
        for table in _TABLES:
            assert not _present(engine, table), table
        assert _our_triggers(engine) == {}
    finally:
        _back_to_head(alembic_config, engine)


@needs_mysql
def test_0039_downgrade_with_both_tables_gone_issues_no_ddl(
    alembic_config: Config, engine: Engine
) -> None:
    _at_0039(alembic_config, engine)
    try:
        _execute(engine, f"DROP TABLE {_RECEIPTS}")
        _execute(engine, f"DROP TABLE {_SERIES}")

        with _statements() as statements:
            command.downgrade(alembic_config, _DOWN_REVISION)

        assert _writes(statements) == []
        assert _version(engine) == _DOWN_REVISION
    finally:
        _back_to_head(alembic_config, engine)


@needs_mysql
def test_0039_offline_upgrade_and_downgrade(alembic_config: Config) -> None:
    """Design §8: `--sql` upgrade emits both tables, the series `INSERT` and both triggers
    without reading the database; the `--sql` downgrade fails saying it must run online, and
    its output has no DROP."""
    migration = _load()
    upgrade_buffer = io.StringIO()
    command.upgrade(
        Config("alembic.ini", output_buffer=upgrade_buffer),
        f"{_DOWN_REVISION}:{_REVISION}",
        sql=True,
    )
    emitted = _compact(upgrade_buffer.getvalue())
    expected = [
        _compact(migration._CREATE_SERIES_MYSQL),
        _compact(_DESIGN_INSERT),
        _compact(migration._CREATE_RECEIPTS_MYSQL),
        _compact(migration._TRIGGERS[_UPDATE_TRIGGER]),
        _compact(migration._TRIGGERS[_DELETE_TRIGGER]),
    ]
    positions = [emitted.index(statement) for statement in expected]
    assert positions == sorted(positions)
    assert "@@LOG_BIN" not in emitted

    downgrade_buffer = io.StringIO()
    with pytest.raises(RuntimeError) as raised:
        command.downgrade(
            Config("alembic.ini", output_buffer=downgrade_buffer),
            f"{_REVISION}:{_DOWN_REVISION}",
            sql=True,
        )

    message = str(raised.value)
    assert _RECEIPTS in message
    assert "online" in message
    assert "DROP" not in _normalised(downgrade_buffer.getvalue())


@contextmanager
def _rolled_back(engine: Engine) -> Iterator[sqlalchemy.Connection]:
    """A connection whose writes are all rolled back, foreign key checks off (no payment, tenant
    or tax policy version is needed for this)."""
    with engine.connect() as connection:
        transaction = connection.begin()
        connection.exec_driver_sql("SET FOREIGN_KEY_CHECKS = 0")
        try:
            yield connection
        finally:
            transaction.rollback()
            connection.exec_driver_sql("SET FOREIGN_KEY_CHECKS = 1")


@needs_mysql
@pytest.mark.parametrize("row", list(_LEGAL_RECEIPTS))
def test_0039_mysql_takes_the_legal_receipts(
    alembic_config: Config, engine: Engine, row: str
) -> None:
    _at_0039(alembic_config, engine)
    with _rolled_back(engine) as connection:
        _insert_receipt(connection, _LEGAL_RECEIPTS[row])
        assert int(connection.execute(_RECEIPT_COUNT).scalar_one()) == 1


@needs_mysql
@pytest.mark.parametrize("row", list(_ILLEGAL_RECEIPTS))
def test_0039_mysql_checks_refuse_the_illegal_receipts(
    alembic_config: Config, engine: Engine, row: str
) -> None:
    """Each illegal receipt on MySQL is refused (3819, the check named). Everything is rolled
    back."""
    _at_0039(alembic_config, engine)
    overrides, check = _ILLEGAL_RECEIPTS[row]
    with _rolled_back(engine) as connection:
        with pytest.raises(DBAPIError) as raised:
            with connection.begin_nested():
                _insert_receipt(connection, overrides)
        assert int(raised.value.orig.args[0]) == _ER_CHECK_CONSTRAINT_VIOLATED, row
        assert check in str(raised.value.orig.args[1]), row
        assert int(connection.execute(_RECEIPT_COUNT).scalar_one()) == 0


@needs_mysql
@pytest.mark.parametrize("case", list(_DUPLICATES))
def test_0039_mysql_unique_constraints_refuse_a_second_receipt(
    alembic_config: Config, engine: Engine, case: str
) -> None:
    """Design §7: the same payment twice, the same year and sequence (and the same number or
    public id) — 1062 naming the unique constraint; the first receipt stays alone."""
    _at_0039(alembic_config, engine)
    overrides, unique, _ = _DUPLICATES[case]
    with _rolled_back(engine) as connection:
        _insert_receipt(connection, _FIRST_RECEIPT, 1)
        with pytest.raises(IntegrityError) as raised:
            with connection.begin_nested():
                _insert_receipt(connection, overrides, 2)
        assert int(raised.value.orig.args[0]) == _ER_DUP_ENTRY, case
        assert unique in str(raised.value.orig.args[1]), case
        assert int(connection.execute(_RECEIPT_COUNT).scalar_one()) == 1


@needs_mysql
@pytest.mark.parametrize("row", list(_ILLEGAL_SERIES))
def test_0039_mysql_series_checks_refuse_the_illegal_rows(
    alembic_config: Config, engine: Engine, row: str
) -> None:
    """Each illegal series row is refused (3819, the check named); for the counter cases the
    year's row is deleted first, inside the rolled-back transaction."""
    _at_0039(alembic_config, engine)
    values, check = _ILLEGAL_SERIES[row]
    with _rolled_back(engine) as connection:
        connection.execute(
            text(f"DELETE FROM {_SERIES} WHERE series_year = :year"),
            {"year": values["series_year"]},
        )
        with pytest.raises(DBAPIError) as raised:
            with connection.begin_nested():
                connection.execute(_INSERT_SERIES_ROW, values)
        assert int(raised.value.orig.args[0]) == _ER_CHECK_CONSTRAINT_VIOLATED, row
        assert check in str(raised.value.orig.args[1]), row


@needs_mysql
def test_0039_mysql_series_counter_can_move(alembic_config: Config, engine: Engine) -> None:
    """The series table has no trigger: `issue_receipt` (AIH-TASK-197) moves the counter up to
    999999; beyond it the check refuses. Everything is rolled back."""
    _at_0039(alembic_config, engine)
    with _rolled_back(engine) as connection:
        connection.execute(
            text(f"UPDATE {_SERIES} SET last_sequence = 999999 WHERE series_year = 2026")
        )
        with pytest.raises(DBAPIError) as raised:
            with connection.begin_nested():
                connection.execute(
                    text(
                        f"UPDATE {_SERIES} SET last_sequence = last_sequence + 1"
                        " WHERE series_year = 2026"
                    )
                )
        assert int(raised.value.orig.args[0]) == _ER_CHECK_CONSTRAINT_VIOLATED
        assert "ck_receipt_number_series_sequence" in str(raised.value.orig.args[1])


# Design §2 / §6: every UPDATE, whatever it changes (nothing included), and the DELETE.
_UPDATES = {
    "amount": f"UPDATE {_RECEIPTS} SET amount = 200.00 WHERE id = :id",
    "snapshot_json": (
        f"UPDATE {_RECEIPTS} SET snapshot_json = JSON_OBJECT('schema_version', 2) WHERE id = :id"
    ),
    "receipt_number": (
        f"UPDATE {_RECEIPTS} SET receipt_number = 'ACV-AIR-2026-999999' WHERE id = :id"
    ),
    "issued_at": f"UPDATE {_RECEIPTS} SET issued_at = '2027-01-01 00:00:00' WHERE id = :id",
    "tax_policy_version_id": f"UPDATE {_RECEIPTS} SET tax_policy_version_id = 2 WHERE id = :id",
    "nothing changed": f"UPDATE {_RECEIPTS} SET currency = currency WHERE id = :id",
}
_DELETE = f"DELETE FROM {_RECEIPTS} WHERE id = :id"


def _assert_immutable(engine: Engine, statement: str, receipt_id: int) -> None:
    with pytest.raises(DBAPIError) as raised:
        _execute(engine, statement, {"id": receipt_id})
    # 1644 is ER_SIGNAL_EXCEPTION: an unhandled SIGNAL SQLSTATE '45000'.
    assert int(raised.value.orig.args[0]) == _ER_SIGNAL_EXCEPTION, statement
    assert _IMMUTABLE in str(raised.value.orig.args[1]), statement


@needs_mysql
def test_0039_triggers_refuse_update_and_delete_of_a_receipt(
    alembic_config: Config, engine: Engine
) -> None:
    """Design §2 / §6 "数据保留": every UPDATE of a receipt and its DELETE fail with SQLSTATE
    45000; the receipts are unchanged."""
    _at_0039(alembic_config, engine)
    try:
        receipt_ids = _insert_committed(engine, [{}, {}])
        before = _ours(engine)

        for receipt_id in receipt_ids:
            for update in _UPDATES.values():
                _assert_immutable(engine, update, receipt_id)
            _assert_immutable(engine, _DELETE, receipt_id)

        assert _ours(engine) == before
    finally:
        _truncate(engine)

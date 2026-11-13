"""0038_payment_settings（AIH-TASK-188，设计闸门 #587 v3）.

Design §2 "数据库", §3 INV-6 / INV-10, §4 ("数据库负责的"), §7 (the "迁移与 CHECK" row, task
188), §8 (the migration item).

The first cases need no database: the revision chain; the table's columns, index, foreign key and
checks in the migration equal the model's and the design's, under the same names and in the same
order; the seed row is the design's (amounts as fixed-point text, `preset_amounts` a JSON array of
fixed-point strings); the one `CREATE TABLE` and the one `INSERT` the upgrade sends are the
design's; every branch of the upgrade (no table yet — create and seed / table already there —
refused, no DDL / `--sql` / not MySQL) and of the downgrade (`--sql` refused / no table — skipped
/ otherwise the table dropped), with `alembic.op` and the inspector stood in. `create_all` on
SQLite (what the unit tests run) builds the table whose checks take the seed row and the legal
edge values and refuse a second row and every illegal value.

On a real MySQL (`BILLING_TEST_DATABASE_URL`; skipped when unset, never skipped in CI): the
0037 → 0038 → 0037 → 0038 round trip (one `CREATE TABLE` and one `INSERT` up, with the seed row
the design's and stamped at migration time; one `DROP TABLE` down; every other table, key and
check and the wallet and ledger rows unchanged), the table already there (refused, no DDL, no
row, version stays 0037), the downgrade with the table gone (skipped, no DDL), the `--sql`
upgrade and downgrade, and the checks on the same values. The Alembic chain does not run on
SQLite (0006 onwards ask for `@@log_bin`), so every Alembic scenario is MySQL's, as in
test_payment_review_migration.py. Captured statements are compared after normalising (backticks,
case, spaces). No gateway is called and no clock is read: this task has no service, and the seed
row's time is MySQL's own `UTC_TIMESTAMP()`, compared with the database clock around the upgrade.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import io
import json
import os
import pathlib
import re
from collections.abc import Iterator
from contextlib import contextmanager
from decimal import Decimal
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

from alembic import command
from app.models.base import Base
from app.models.payment_settings import PaymentSettings

TEST_DATABASE_URL = os.environ.get("BILLING_TEST_DATABASE_URL", "")

needs_mysql = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="BILLING_TEST_DATABASE_URL is not set; migration tests need a real MySQL",
)

_REVISION = "0038_payment_settings"
_DOWN_REVISION = "0037_payment_review"
_MIGRATION = pathlib.Path("alembic/versions/20261108_0038_payment_settings.py")
_TABLE = "payment_settings"

# Design §2 "数据库": the columns, in order.
_DESIGN_COLUMNS = {
    "id": ("INTEGER", False),
    "min_amount": ("DECIMAL(20,8)", False),
    "max_amount": ("DECIMAL(20,8)", True),
    "recovery_buffer": ("DECIMAL(20,8)", False),
    "preset_amounts": ("JSON", False),
    "reuse_window_seconds": ("INTEGER", False),
    "pending_timeout_seconds": ("INTEGER", False),
    "version": ("INTEGER", False),
    "updated_at": ("DATETIME", False),
    "updated_by_user_id": ("BIGINT", True),
}
_DESIGN_INDEXES = {
    "ix_payment_settings_updated_by_user_id": (("updated_by_user_id",), False),
}
_DESIGN_FOREIGN_KEYS = {
    "fk_payment_settings_updated_by_user_id": (
        ("updated_by_user_id",),
        "users",
        ("id",),
        "RESTRICT",
    ),
}
# The design's expressions (whitespace aside); `ck_payment_settings_<column>`.
_DESIGN_CHECKS = {
    "ck_payment_settings_singleton": "id = 1",
    "ck_payment_settings_min_amount": (
        "min_amount >= 2 AND min_amount <= 999999.99 AND min_amount = ROUND(min_amount, 2)"
    ),
    "ck_payment_settings_max_amount": (
        "max_amount IS NULL OR (max_amount >= min_amount AND max_amount <= 999999.99"
        "  AND max_amount = ROUND(max_amount, 2))"
    ),
    "ck_payment_settings_recovery_buffer": (
        "recovery_buffer >= 0.01 AND recovery_buffer <= 999999.99"
        "  AND recovery_buffer = ROUND(recovery_buffer, 2)"
    ),
    "ck_payment_settings_reuse_window_seconds": (
        "reuse_window_seconds >= 0 AND reuse_window_seconds <= 300"
    ),
    "ck_payment_settings_pending_timeout_seconds": (
        "pending_timeout_seconds >= 2400 AND pending_timeout_seconds <= 86400"
    ),
    "ck_payment_settings_version": "version >= 1",
}

# Design §2: the seed row (`updated_at` is the migration's time).
_DESIGN_PRESET_AMOUNTS = ["50.00", "100.00", "300.00", "500.00", "1000.00"]
_DESIGN_SEED = {
    "id": 1,
    "min_amount": Decimal("10.00"),
    "max_amount": None,
    "recovery_buffer": Decimal("10.00"),
    "preset_amounts": _DESIGN_PRESET_AMOUNTS,
    "reuse_window_seconds": 60,
    "pending_timeout_seconds": 3600,
    "version": 1,
    "updated_by_user_id": None,
}

# The design's two statements.
_DESIGN_CREATE = (
    f"CREATE TABLE {_TABLE} ("
    " id INTEGER NOT NULL,"
    " min_amount DECIMAL(20, 8) NOT NULL,"
    " max_amount DECIMAL(20, 8) NULL,"
    " recovery_buffer DECIMAL(20, 8) NOT NULL,"
    " preset_amounts JSON NOT NULL,"
    " reuse_window_seconds INTEGER NOT NULL,"
    " pending_timeout_seconds INTEGER NOT NULL,"
    " version INTEGER NOT NULL,"
    " updated_at DATETIME NOT NULL,"
    " updated_by_user_id BIGINT NULL,"
    " PRIMARY KEY (id),"
    " INDEX ix_payment_settings_updated_by_user_id (updated_by_user_id),"
    " CONSTRAINT fk_payment_settings_updated_by_user_id FOREIGN KEY (updated_by_user_id)"
    " REFERENCES users (id) ON DELETE RESTRICT, "
    + ", ".join(f"CONSTRAINT {name} CHECK ({check})" for name, check in _DESIGN_CHECKS.items())
    + ")"
)
_DESIGN_INSERT = (
    f"INSERT INTO {_TABLE} (id, min_amount, max_amount, recovery_buffer, preset_amounts,"
    " reuse_window_seconds, pending_timeout_seconds, version, updated_at, updated_by_user_id)"
    " VALUES (1, 10.00, NULL, 10.00,"
    ' \'["50.00", "100.00", "300.00", "500.00", "1000.00"]\','
    " 60, 3600, 1, UTC_TIMESTAMP(), NULL)"
)

# MySQL reports a violated CHECK as 3819 (ER_CHECK_CONSTRAINT_VIOLATED), a duplicate key as 1062.
_ER_CHECK_CONSTRAINT_VIOLATED = 3819
_ER_DUP_ENTRY = 1062

_UPDATED_AT = "2026-11-08 00:00:00"

_ALEMBIC_VERSION = text("SELECT version_num FROM alembic_version")
_DATABASE_NOW = text("SELECT UTC_TIMESTAMP()")
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
_COUNT = text(f"SELECT COUNT(*) FROM {_TABLE}")
_SELECT_ROWS = text(f"SELECT * FROM {_TABLE} ORDER BY id")


def _row_params(overrides: dict[str, object]) -> dict[str, object]:
    """The design's seed row, as raw parameters (amounts as text, the presets as a JSON
    string), with `overrides` on top."""
    params: dict[str, object] = {
        "id": 1,
        "min_amount": "10.00",
        "max_amount": None,
        "recovery_buffer": "10.00",
        "preset_amounts": json.dumps(_DESIGN_PRESET_AMOUNTS),
        "reuse_window_seconds": 60,
        "pending_timeout_seconds": 3600,
        "version": 1,
        "updated_at": _UPDATED_AT,
        "updated_by_user_id": None,
    }
    params.update(overrides)
    return params


# Raw SQL: the values go straight to the database's checks.
_INSERT_COLUMNS = list(_row_params({}))
_INSERT_ROW = text(
    f"INSERT INTO {_TABLE} ({', '.join(_INSERT_COLUMNS)})"
    f" VALUES ({', '.join(f':{name}' for name in _INSERT_COLUMNS)})"
)


def _insert(connection: sqlalchemy.Connection, overrides: dict[str, object]) -> None:
    connection.execute(_INSERT_ROW, _row_params(overrides))


def _check(column: str) -> set[str]:
    return {f"ck_payment_settings_{column}"}


# Illegal rows (each alone in the table): overrides on the seed row → the check that refuses it.
_ILLEGAL_ROWS: dict[str, tuple[dict[str, object], set[str]]] = {
    "id 2": ({"id": 2}, _check("singleton")),
    "id 0": ({"id": 0}, _check("singleton")),
    "min_amount 1.99": ({"min_amount": "1.99"}, _check("min_amount")),
    "min_amount 0": ({"min_amount": "0"}, _check("min_amount")),
    "min_amount 1000000.00": ({"min_amount": "1000000.00"}, _check("min_amount")),
    "min_amount three decimals": ({"min_amount": "10.001"}, _check("min_amount")),
    "max_amount below min_amount": ({"max_amount": "9.99"}, _check("max_amount")),
    "max_amount 1000000.00": ({"max_amount": "1000000.00"}, _check("max_amount")),
    "max_amount three decimals": ({"max_amount": "100.005"}, _check("max_amount")),
    "recovery_buffer 0": ({"recovery_buffer": "0"}, _check("recovery_buffer")),
    "recovery_buffer 0.00": ({"recovery_buffer": "0.00"}, _check("recovery_buffer")),
    "recovery_buffer negative": ({"recovery_buffer": "-0.01"}, _check("recovery_buffer")),
    "recovery_buffer 1000000.00": ({"recovery_buffer": "1000000.00"}, _check("recovery_buffer")),
    "recovery_buffer three decimals": ({"recovery_buffer": "0.015"}, _check("recovery_buffer")),
    "reuse_window_seconds -1": ({"reuse_window_seconds": -1}, _check("reuse_window_seconds")),
    "reuse_window_seconds 301": ({"reuse_window_seconds": 301}, _check("reuse_window_seconds")),
    "pending_timeout_seconds 2399": (
        {"pending_timeout_seconds": 2399},
        _check("pending_timeout_seconds"),
    ),
    "pending_timeout_seconds 86401": (
        {"pending_timeout_seconds": 86401},
        _check("pending_timeout_seconds"),
    ),
    "version 0": ({"version": 0}, _check("version")),
}

# Legal rows (each alone in the table): the seed and every edge the checks allow.
_LEGAL_ROWS: dict[str, dict[str, object]] = {
    "the seed": {},
    "min_amount 2.00": {"min_amount": "2.00"},
    "min_amount 999999.99": {"min_amount": "999999.99"},
    "max_amount equal to min_amount": {"max_amount": "10.00"},
    "max_amount 999999.99": {"max_amount": "999999.99"},
    "recovery_buffer 0.01": {"recovery_buffer": "0.01"},
    "recovery_buffer 999999.99": {"recovery_buffer": "999999.99"},
    "reuse_window_seconds 0": {"reuse_window_seconds": 0},
    "reuse_window_seconds 300": {"reuse_window_seconds": 300},
    "pending_timeout_seconds 2400": {"pending_timeout_seconds": 2400},
    "pending_timeout_seconds 86400": {"pending_timeout_seconds": 86400},
    "a later version": {"version": 42},
}


def _load(path: pathlib.Path = _MIGRATION, name: str = "migration_0038") -> ModuleType:
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
    """`PaymentSettings`' columns, indexes, foreign keys and check names."""
    table = PaymentSettings.__table__
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


_DESIGN_SHAPE = {
    "columns": _DESIGN_COLUMNS,
    "indexes": _DESIGN_INDEXES,
    "foreign_keys": _DESIGN_FOREIGN_KEYS,
    "checks": set(_DESIGN_CHECKS),
}


# ---------------------------------------------------------------------------
# No database
# ---------------------------------------------------------------------------


def test_0038_follows_0037() -> None:
    migration = _load()

    assert migration.revision == _REVISION
    assert migration.down_revision == _DOWN_REVISION
    # `alembic_version.version_num` is VARCHAR(32).
    assert len(migration.revision) <= 32


def test_0038_shape_is_the_design_and_the_model() -> None:
    """The migration's frozen literals, the model's declarations and design §2 agree: the
    columns (type, width, nullable, in this order), the index, the foreign key RESTRICT to
    users, the checks by name and in order; the primary key is `id` alone and not
    auto-incremented."""
    migration = _load()

    assert migration._COLUMNS == _DESIGN_COLUMNS
    assert list(migration._COLUMNS) == list(_DESIGN_COLUMNS)
    assert migration._INDEXES == _DESIGN_INDEXES
    assert migration._FOREIGN_KEYS == _DESIGN_FOREIGN_KEYS
    assert list(migration._CHECKS) == list(_DESIGN_CHECKS)
    assert _model_shape() == _DESIGN_SHAPE
    assert [c.name for c in PaymentSettings.__table__.columns] == list(_DESIGN_COLUMNS)
    assert [c.name for c in PaymentSettings.__table__.primary_key.columns] == ["id"]
    assert PaymentSettings.__table__.c.id.autoincrement is False


def test_0038_table_args_are_in_the_migrations_order() -> None:
    """`__table_args__` declares the index, the foreign key and the checks under the
    migration's names and in its order."""
    migration = _load()
    kinds = UniqueConstraint | sqlalchemy.Index | ForeignKeyConstraint | CheckConstraint
    declared = [str(arg.name) for arg in PaymentSettings.__table_args__ if isinstance(arg, kinds)]

    assert declared == [*migration._INDEXES, *migration._FOREIGN_KEYS, *migration._CHECKS]


def test_0038_checks_are_the_designs_and_the_models() -> None:
    """Same conditions in the migration, on the model and in the design (whitespace aside)."""
    migration = _load()
    model = {
        str(constraint.name): _normalised(str(constraint.sqltext))
        for constraint in PaymentSettings.__table__.constraints
        if isinstance(constraint, CheckConstraint)
    }

    assert {name: _normalised(c) for name, c in migration._CHECKS.items()} == model
    assert {name: _normalised(c) for name, c in _DESIGN_CHECKS.items()} == model


def test_0038_seed_is_the_designs() -> None:
    """Design §2: the seed's values one by one; `preset_amounts` is a JSON array of
    fixed-point strings, not of JSON numbers (INV-10)."""
    migration = _load()
    seed = migration._SEED

    assert list(seed) == list(_DESIGN_COLUMNS)
    assert seed["updated_at"] == "UTC_TIMESTAMP()"
    literal = seed["preset_amounts"]
    assert literal.startswith("'") and literal.endswith("'")
    presets = json.loads(literal[1:-1])
    assert presets == _DESIGN_PRESET_AMOUNTS
    assert all(isinstance(amount, str) for amount in presets)
    assert list(migration._SEED_PRESET_AMOUNTS) == _DESIGN_PRESET_AMOUNTS
    for name, value in _DESIGN_SEED.items():
        if name == "preset_amounts":
            continue
        expected = "NULL" if value is None else str(value)
        assert seed[name] == expected, name


def test_0038_upgrade_is_the_design_create_table_and_insert() -> None:
    migration = _load()

    assert _compact(migration._CREATE_TABLE) == _compact(_DESIGN_CREATE)
    assert migration._CREATE_TABLE.count(";") == 0
    assert _normalised(migration._CREATE_TABLE).count("CREATE TABLE") == 1
    assert _compact(migration._INSERT_SEED) == _compact(_DESIGN_INSERT)
    assert migration._INSERT_SEED.count(";") == 0


class _FakeBind:
    """No query is expected: the migration only asks the inspector."""

    def __init__(self) -> None:
        self.queries: list[str] = []

    def execute(self, statement: object) -> None:
        self.queries.append(str(statement))
        raise AssertionError(f"unexpected query: {statement}")


class _FakeInspector:
    """Answers whether `payment_settings` exists."""

    def __init__(self, present: bool) -> None:
        self._present = present
        self.asked: list[str] = []

    def has_table(self, table: str) -> bool:
        assert table == _TABLE, table
        self.asked.append("has_table")
        return self._present


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

    def drop_table(self, table: str) -> None:
        self.calls.append(("drop_table", (table,)))


def _with(
    monkeypatch, present: bool, *, dialect: str = "mysql", as_sql: bool = False
) -> tuple[ModuleType, _FakeOp, _FakeInspector, _FakeBind]:
    migration = _load()
    bind = _FakeBind()
    fake = _FakeOp(bind, dialect=dialect, as_sql=as_sql)
    inspector = _FakeInspector(present)
    monkeypatch.setattr(migration, "op", fake)
    monkeypatch.setattr(migration, "sa", _Sa(inspector))
    return migration, fake, inspector, bind


def test_0038_upgrade_creates_the_table_then_the_seed_row(monkeypatch) -> None:
    migration, fake, inspector, bind = _with(monkeypatch, present=False)
    migration.upgrade()

    assert fake.calls == [
        ("execute", (migration._CREATE_TABLE,)),
        ("execute", (migration._INSERT_SEED,)),
    ]
    assert inspector.asked == ["has_table"]
    assert bind.queries == []


def test_0038_upgrade_refuses_the_table_already_there_before_any_ddl(monkeypatch) -> None:
    """Design §8: the table already there — refuse, no DDL, no row."""
    migration, fake, inspector, bind = _with(monkeypatch, present=True)

    with pytest.raises(RuntimeError) as raised:
        migration.upgrade()

    assert fake.calls == []
    assert inspector.asked == ["has_table"]
    assert bind.queries == []
    message = str(raised.value)
    assert f"table {_TABLE} already exists" in message
    assert "No DDL was issued" in message


def test_0038_offline_upgrade_emits_both_statements_without_reading(monkeypatch) -> None:
    migration, fake, inspector, bind = _with(monkeypatch, present=True, as_sql=True)
    migration.upgrade()

    assert inspector.asked == []
    assert bind.queries == []
    assert fake.calls == [
        ("execute", (migration._CREATE_TABLE,)),
        ("execute", (migration._INSERT_SEED,)),
    ]


@pytest.mark.parametrize("as_sql", [False, True])
@pytest.mark.parametrize("step", ["upgrade", "downgrade"])
def test_0038_refuses_other_dialects_before_reading_or_ddl(
    monkeypatch, step: str, as_sql: bool
) -> None:
    migration, fake, inspector, bind = _with(
        monkeypatch, present=step == "downgrade", dialect="sqlite", as_sql=as_sql
    )

    with pytest.raises(RuntimeError) as raised:
        getattr(migration, step)()

    assert fake.calls == []
    assert inspector.asked == []
    assert bind.queries == []
    assert "only runs on MySQL" in str(raised.value)


def test_0038_offline_downgrade_refuses_before_reading_or_ddl(monkeypatch) -> None:
    """Design §8: offline the downgrade refuses, with no DDL."""
    migration, fake, inspector, bind = _with(monkeypatch, present=True, as_sql=True)

    with pytest.raises(RuntimeError) as raised:
        migration.downgrade()

    assert fake.calls == []
    assert inspector.asked == []
    assert bind.queries == []
    message = str(raised.value)
    assert _TABLE in message
    assert "online" in message


def test_0038_downgrade_without_the_table_does_nothing(monkeypatch) -> None:
    migration, fake, inspector, bind = _with(monkeypatch, present=False)
    migration.downgrade()

    assert inspector.asked == ["has_table"]
    assert bind.queries == []
    assert fake.calls == []


def test_0038_downgrade_drops_the_table(monkeypatch) -> None:
    """Design §8: the table there — dropped (its history is in the audit log), without
    checking for rows."""
    migration, fake, inspector, bind = _with(monkeypatch, present=True)
    migration.downgrade()

    assert inspector.asked == ["has_table"]
    assert bind.queries == []
    assert fake.calls == [("drop_table", (_TABLE,))]


# ---------------------------------------------------------------------------
# SQLite
# ---------------------------------------------------------------------------


@contextmanager
def _sqlite() -> Iterator[Engine]:
    """The unit tests build SQLite with `create_all` from the models, not with the migrations
    (and with no seed row). SQLite does not enforce foreign keys by default: `users` is not
    needed."""
    engine = create_engine("sqlite+pysqlite:///:memory:")
    try:
        Base.metadata.create_all(engine, tables=[PaymentSettings.__table__])
        yield engine
    finally:
        engine.dispose()


def test_0038_sqlite_table_gets_the_columns_index_foreign_key_and_checks() -> None:
    with _sqlite() as engine:
        inspector = inspect(engine)
        columns = {str(c["name"]): bool(c["nullable"]) for c in inspector.get_columns(_TABLE)}
        indexes = {
            str(i["name"]): (tuple(i["column_names"]), bool(i["unique"]))
            for i in inspector.get_indexes(_TABLE)
        }
        keys = {
            str(k["name"]): (
                tuple(k["constrained_columns"]),
                str(k["referred_table"]),
                tuple(k["referred_columns"]),
                str(k["options"].get("ondelete")).upper(),
            )
            for k in inspector.get_foreign_keys(_TABLE)
        }
        checks = {str(c["name"]) for c in inspector.get_check_constraints(_TABLE)}
        with engine.connect() as connection:
            count = int(connection.execute(_COUNT).scalar_one())

    assert columns == {name: nullable for name, (_, nullable) in _DESIGN_COLUMNS.items()}
    assert indexes == _DESIGN_INDEXES
    assert keys == _DESIGN_FOREIGN_KEYS
    assert checks == set(_DESIGN_CHECKS)
    assert count == 0


@pytest.mark.parametrize("row", list(_LEGAL_ROWS))
def test_0038_sqlite_takes_the_legal_rows(row: str) -> None:
    with _sqlite() as engine:
        with engine.begin() as connection:
            _insert(connection, _LEGAL_ROWS[row])
            count = int(connection.execute(_COUNT).scalar_one())

    assert count == 1


@pytest.mark.parametrize("row", list(_ILLEGAL_ROWS))
def test_0038_sqlite_checks_refuse_the_illegal_rows(row: str) -> None:
    overrides, _ = _ILLEGAL_ROWS[row]
    with _sqlite() as engine:
        with pytest.raises(DBAPIError) as raised:
            with engine.begin() as connection:
                _insert(connection, overrides)
        assert "CHECK constraint failed" in str(raised.value), row

        with engine.connect() as connection:
            assert int(connection.execute(_COUNT).scalar_one()) == 0


def test_0038_sqlite_refuses_a_second_row() -> None:
    """Design §4: exactly one row — with the seed there, `id = 2` breaks the singleton check
    and `id = 1` again breaks the primary key."""
    with _sqlite() as engine:
        with engine.begin() as connection:
            _insert(connection, {})
        with pytest.raises(DBAPIError) as raised:
            with engine.begin() as connection:
                _insert(connection, {"id": 2})
        assert "CHECK constraint failed" in str(raised.value)
        with pytest.raises(IntegrityError):
            with engine.begin() as connection:
                _insert(connection, {})

        with engine.connect() as connection:
            assert int(connection.execute(_COUNT).scalar_one()) == 1


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


_WRITES = {"ALTER", "CREATE", "DROP", "INSERT", "RENAME", "TRUNCATE"}


@contextmanager
def _writes() -> Iterator[list[str]]:
    """DDL, and inserts outside `alembic_version`, that any engine (Alembic's own included)
    sends while the block runs."""
    statements: list[str] = []

    def record(_conn, _cursor, statement, _params, _context, _many) -> None:
        statements.append(statement)

    event.listen(Engine, "before_cursor_execute", record)
    try:
        yield statements
    finally:
        event.remove(Engine, "before_cursor_execute", record)
        statements[:] = [
            s
            for s in statements
            if _normalised(s).split(" ")[0] in _WRITES and "ALEMBIC_VERSION" not in _normalised(s)
        ]


def _version(engine: Engine) -> str:
    with engine.connect() as connection:
        return str(connection.execute(_ALEMBIC_VERSION).scalar_one())


def _database_now(engine: Engine) -> dt.datetime:
    with engine.connect() as connection:
        return connection.execute(_DATABASE_NOW).scalar_one()


def _execute(engine: Engine, statement: str) -> None:
    with engine.begin() as connection:
        connection.execute(text(statement))


def _has_table(engine: Engine) -> bool:
    return inspect(engine).has_table(_TABLE)


def _table_shape(engine: Engine) -> dict[str, object]:
    """`payment_settings` as reflected, in the migration's terms; delete rules from
    information_schema; prefix lengths kept so they would show."""
    inspector = inspect(engine)
    with engine.connect() as connection:
        rules = dict(connection.execute(_DELETE_RULES_QUERY, {"table": _TABLE}).all())
    indexes: dict[str, object] = {}
    for index in inspector.get_indexes(_TABLE):
        prefix = (index.get("dialect_options") or {}).get("mysql_length")
        shape = (tuple(index["column_names"]), bool(index["unique"]))
        indexes[str(index["name"])] = shape if not prefix else (*shape, repr(prefix))
    return {
        "columns": {
            str(c["name"]): (_type_text(c["type"]), bool(c["nullable"]))
            for c in inspector.get_columns(_TABLE)
        },
        "indexes": indexes,
        "foreign_keys": {
            str(key["name"]): (
                tuple(key["constrained_columns"]),
                str(key["referred_table"]),
                tuple(key["referred_columns"]),
                rules.get(key["name"]),
            )
            for key in inspector.get_foreign_keys(_TABLE)
        },
        "checks": {str(check["name"]) for check in inspector.get_check_constraints(_TABLE)},
    }


def _assert_design_shape(engine: Engine) -> None:
    assert _table_shape(engine) == _model_shape() == _DESIGN_SHAPE
    inspector = inspect(engine)
    assert [str(c["name"]) for c in inspector.get_columns(_TABLE)] == list(_DESIGN_COLUMNS)
    assert inspector.get_pk_constraint(_TABLE)["constrained_columns"] == ["id"]
    id_column = next(c for c in inspector.get_columns(_TABLE) if c["name"] == "id")
    assert not id_column.get("autoincrement"), "id must not be AUTO_INCREMENT"


def _seed_rows(engine: Engine) -> list[dict[str, object]]:
    with engine.connect() as connection:
        rows = [dict(row._mapping) for row in connection.execute(_SELECT_ROWS)]
    for row in rows:
        presets = row["preset_amounts"]
        row["preset_amounts"] = json.loads(presets) if isinstance(presets, str) else presets
    return rows


def _assert_seed(engine: Engine, earliest: dt.datetime, latest: dt.datetime) -> None:
    """Design §2: exactly one row, the seed's values, stamped between the two database
    times."""
    rows = _seed_rows(engine)
    assert len(rows) == 1
    row = rows[0]
    updated_at = row.pop("updated_at")
    assert row == _DESIGN_SEED
    assert all(isinstance(amount, str) for amount in row["preset_amounts"])
    assert isinstance(updated_at, dt.datetime)
    assert updated_at.tzinfo is None
    assert earliest <= updated_at <= latest


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
    """The snapshot with `payment_settings` and its checks taken out."""
    tables = snapshot["tables"]
    assert isinstance(tables, dict)
    checks = snapshot["checks"]
    assert isinstance(checks, set)
    return {
        "tables": {name: shape for name, shape in tables.items() if name != _TABLE},
        "checks": {check for check in checks if check[0] != _TABLE},
    }


def _ledger_and_wallets(engine: Engine) -> tuple[list[tuple[object, ...]], ...]:
    with engine.connect() as connection:
        wallets = [tuple(row) for row in connection.execute(_WALLET_ROWS).all()]
        ledger = [tuple(row) for row in connection.execute(_LEDGER_ROWS).all()]
    return wallets, ledger


def _at_0037(alembic_config: Config, engine: Engine) -> None:
    """Head, then down to 0038 (later revisions are not under observation), then to 0037."""
    _at_0038(alembic_config, engine)
    command.downgrade(alembic_config, _DOWN_REVISION)
    assert _version(engine) == _DOWN_REVISION
    assert not _has_table(engine)


def _at_0038(alembic_config: Config, engine: Engine) -> None:
    """Head, then down to 0038: later revisions are not under observation."""
    command.upgrade(alembic_config, "head")
    command.downgrade(alembic_config, _REVISION)
    assert _version(engine) == _REVISION


def _back_to_head(alembic_config: Config, engine: Engine) -> None:
    """Drops a hand-made table left at 0037, steps a table-less 0038 back to 0037, and upgrades
    to head again (which writes the seed row)."""
    if _version(engine) == _REVISION and not _has_table(engine):
        command.downgrade(alembic_config, _DOWN_REVISION)
    if _version(engine) == _DOWN_REVISION and _has_table(engine):
        _execute(engine, f"DROP TABLE {_TABLE}")
    command.upgrade(alembic_config, "head")


@needs_mysql
def test_0038_round_trip(alembic_config: Config, engine: Engine) -> None:
    """Design §7 "迁移与 CHECK" / §8: 0037 → 0038 → 0037 → 0038. The upgrade sends one
    `CREATE TABLE` and one `INSERT`, the design's; the shape is the model's and the design's;
    the seed row is the design's, stamped at migration time; the downgrade sends one
    `DROP TABLE`; every other table, key and check and the wallet and ledger rows are the same
    throughout."""
    _at_0037(alembic_config, engine)
    try:
        at_0037 = _schema_snapshot(engine)
        rows = _ledger_and_wallets(engine)

        earliest = _database_now(engine)
        with _writes() as statements:
            command.upgrade(alembic_config, _REVISION)
        latest = _database_now(engine)
        assert [_compact(s) for s in statements] == [
            _compact(_DESIGN_CREATE),
            _compact(_DESIGN_INSERT),
        ]
        assert _version(engine) == _REVISION
        _assert_design_shape(engine)
        _assert_seed(engine, earliest, latest)
        at_0038 = _schema_snapshot(engine)
        assert _without_ours(at_0038) == at_0037
        assert _ledger_and_wallets(engine) == rows

        with _writes() as statements:
            command.downgrade(alembic_config, _DOWN_REVISION)
        assert [_normalised(s) for s in statements] == [f"DROP TABLE {_TABLE.upper()}"]
        assert _version(engine) == _DOWN_REVISION
        assert not _has_table(engine)
        assert _schema_snapshot(engine) == at_0037
        assert _ledger_and_wallets(engine) == rows

        earliest = _database_now(engine)
        command.upgrade(alembic_config, _REVISION)
        latest = _database_now(engine)
        assert _schema_snapshot(engine) == at_0038
        _assert_design_shape(engine)
        _assert_seed(engine, earliest, latest)
        assert _ledger_and_wallets(engine) == rows
    finally:
        _back_to_head(alembic_config, engine)


@needs_mysql
def test_0038_upgrade_with_the_table_already_there_fails_before_any_ddl(
    alembic_config: Config, engine: Engine
) -> None:
    """Design §8: version 0037 and a `payment_settings` already there — the upgrade fails
    naming it, issues no DDL and writes no row, the version stays 0037 and the schema is as it
    was."""
    _at_0037(alembic_config, engine)
    try:
        _execute(engine, f"CREATE TABLE {_TABLE} (id INTEGER NOT NULL, PRIMARY KEY (id))")
        before = _schema_snapshot(engine)

        with _writes() as statements:
            with pytest.raises(RuntimeError) as raised:
                command.upgrade(alembic_config, _REVISION)

        assert statements == []
        message = str(raised.value)
        assert f"table {_TABLE} already exists" in message
        assert "No DDL was issued" in message
        assert _version(engine) == _DOWN_REVISION
        assert _schema_snapshot(engine) == before
        with engine.connect() as connection:
            assert int(connection.execute(_COUNT).scalar_one()) == 0
    finally:
        _back_to_head(alembic_config, engine)


@needs_mysql
def test_0038_downgrade_without_the_table_is_skipped(
    alembic_config: Config, engine: Engine
) -> None:
    """Design §8: at 0038 with the table gone, the downgrade issues no DDL and the version goes
    to 0037."""
    _at_0038(alembic_config, engine)
    try:
        _execute(engine, f"DROP TABLE {_TABLE}")
        before = _schema_snapshot(engine)

        with _writes() as statements:
            command.downgrade(alembic_config, _DOWN_REVISION)

        assert statements == []
        assert _version(engine) == _DOWN_REVISION
        assert _schema_snapshot(engine) == before
    finally:
        _back_to_head(alembic_config, engine)


@needs_mysql
def test_0038_downgrade_drops_a_changed_row_too(alembic_config: Config, engine: Engine) -> None:
    """Design §8: the downgrade does not look at the row — settings changed after the seed go
    with the table (their history is in the audit log); the upgrade writes the seed again."""
    _at_0038(alembic_config, engine)
    try:
        _execute(
            engine,
            f"UPDATE {_TABLE} SET min_amount = 20.00, version = version + 1 WHERE id = 1",
        )

        command.downgrade(alembic_config, _DOWN_REVISION)
        assert _version(engine) == _DOWN_REVISION
        assert not _has_table(engine)

        earliest = _database_now(engine)
        command.upgrade(alembic_config, _REVISION)
        _assert_seed(engine, earliest, _database_now(engine))
    finally:
        _back_to_head(alembic_config, engine)


@needs_mysql
def test_0038_offline_upgrade_and_downgrade(alembic_config: Config) -> None:
    """Design §8: `--sql` upgrade emits the `CREATE TABLE` and the `INSERT` without reading the
    database; the `--sql` downgrade fails saying it must run online, and its output has no
    DROP."""
    upgrade_buffer = io.StringIO()
    command.upgrade(
        Config("alembic.ini", output_buffer=upgrade_buffer),
        f"{_DOWN_REVISION}:{_REVISION}",
        sql=True,
    )
    emitted = _compact(upgrade_buffer.getvalue())
    assert _compact(_DESIGN_CREATE) in emitted
    assert _compact(_DESIGN_INSERT) in emitted
    assert emitted.index(_compact(_DESIGN_CREATE)) < emitted.index(_compact(_DESIGN_INSERT))

    downgrade_buffer = io.StringIO()
    with pytest.raises(RuntimeError) as raised:
        command.downgrade(
            Config("alembic.ini", output_buffer=downgrade_buffer),
            f"{_REVISION}:{_DOWN_REVISION}",
            sql=True,
        )

    message = str(raised.value)
    assert _TABLE in message
    assert "online" in message
    assert "DROP" not in _normalised(downgrade_buffer.getvalue())


@contextmanager
def _mysql_emptied(engine: Engine) -> Iterator[sqlalchemy.Connection]:
    """A connection whose writes are all rolled back, with the seed row deleted first, so a
    row can be inserted alone."""
    with engine.connect() as connection:
        transaction = connection.begin()
        try:
            connection.execute(text(f"DELETE FROM {_TABLE}"))
            yield connection
        finally:
            transaction.rollback()


@needs_mysql
@pytest.mark.parametrize("row", list(_LEGAL_ROWS))
def test_0038_mysql_takes_the_legal_rows(alembic_config: Config, engine: Engine, row: str) -> None:
    command.upgrade(alembic_config, "head")
    with _mysql_emptied(engine) as connection:
        _insert(connection, _LEGAL_ROWS[row])
        count = int(connection.execute(_COUNT).scalar_one())

    assert count == 1


@needs_mysql
@pytest.mark.parametrize("row", list(_ILLEGAL_ROWS))
def test_0038_mysql_checks_refuse_the_illegal_rows(
    alembic_config: Config, engine: Engine, row: str
) -> None:
    """Each illegal value on MySQL is refused (3819, the check named). Everything is rolled
    back."""
    command.upgrade(alembic_config, "head")
    overrides, checks = _ILLEGAL_ROWS[row]
    with _mysql_emptied(engine) as connection:
        with pytest.raises(DBAPIError) as raised:
            with connection.begin_nested():
                _insert(connection, overrides)
        assert int(raised.value.orig.args[0]) == _ER_CHECK_CONSTRAINT_VIOLATED, row
        assert any(check in str(raised.value.orig.args[1]) for check in checks), row
        assert int(connection.execute(_COUNT).scalar_one()) == 0


@needs_mysql
def test_0038_mysql_refuses_a_second_row(alembic_config: Config, engine: Engine) -> None:
    """Design §4: exactly one row — next to the migrated seed, `id = 2` breaks the singleton
    check (3819) and `id = 1` again the primary key (1062); the seed row is unchanged."""
    command.upgrade(alembic_config, "head")
    with engine.connect() as connection:
        transaction = connection.begin()
        try:
            before = [tuple(row) for row in connection.execute(_SELECT_ROWS)]
            assert len(before) == 1

            with pytest.raises(DBAPIError) as raised:
                with connection.begin_nested():
                    _insert(connection, {"id": 2})
            assert int(raised.value.orig.args[0]) == _ER_CHECK_CONSTRAINT_VIOLATED
            assert "ck_payment_settings_singleton" in str(raised.value.orig.args[1])

            with pytest.raises(IntegrityError) as raised:
                with connection.begin_nested():
                    _insert(connection, {})
            assert int(raised.value.orig.args[0]) == _ER_DUP_ENTRY

            assert [tuple(row) for row in connection.execute(_SELECT_ROWS)] == before
        finally:
            transaction.rollback()

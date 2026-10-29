"""0028_notifications（AIH-TASK-125，设计闸门 #471 v7）.

Design §2 "数据库", §3 INV-4 / INV-5, §5 (the downgrade row), §7 (the migration rows and
the CHECK row), §8.

The first cases need no database: the revision chain; the columns, indexes, foreign key and checks
in the migration equal the model's and the design's; the upgrade's check (missing / design shape /
each wrong shape) and the downgrade that drops only an empty table, with `alembic.op` and the
inspector stood in; `create_all` on SQLite (what the unit tests run) builds the table and its two
checks refuse the three illegal rows.

On a real MySQL (`BILLING_TEST_DATABASE_URL`; skipped when unset, never skipped in CI): design §7's
migration rows — the 0027 → 0028 → 0027 → 0028 round trip on an empty table (every other table,
key, check and trigger and the wallet and ledger rows unchanged, INV-4 / INV-5), the rerun with the
table already in the design shape, the four wrong shapes, the downgrade with rows, the `--sql`
downgrade — and the CHECK row. The Alembic chain does not run on SQLite (0006 onwards ask for
`@@log_bin`), so every migration scenario is MySQL's, as in test_migrations.py. Captured
statements are compared after normalising (backticks, case, spaces).
"""

from __future__ import annotations

import copy
import datetime as dt
import importlib.util
import io
import os
import pathlib
import re
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from decimal import Decimal
from types import ModuleType, SimpleNamespace

import pytest
import sqlalchemy
from alembic.config import Config
from sqlalchemy import (
    CheckConstraint,
    UniqueConstraint,
    create_engine,
    delete,
    event,
    insert,
    inspect,
    text,
)
from sqlalchemy.dialects import mysql
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError

from alembic import command
from app.models.base import Base
from app.models.notifications import (
    THRESHOLD_CHECK,
    TYPE_CHECK,
    Notification,
    NotificationType,
)
from app.models.tenancy import Tenant

TEST_DATABASE_URL = os.environ.get("BILLING_TEST_DATABASE_URL", "")

needs_mysql = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="BILLING_TEST_DATABASE_URL is not set; migration tests need a real MySQL",
)

_REVISION = "0028_notifications"
_DOWN_REVISION = "0027_wallet_tx_created_index"
_MIGRATION = pathlib.Path("alembic/versions/20261028_0028_notifications.py")
_TABLE = "notifications"

# Design §2 "数据库".
_DESIGN_COLUMNS = {
    "id": ("BIGINT", False),
    "public_id": ("CHAR(36)", False),
    "tenant_id": ("BIGINT", False),
    "notification_type": ("VARCHAR(32)", False),
    "source_outbox_id": ("BIGINT", False),
    "balance": ("DECIMAL(20,8)", False),
    "threshold": ("DECIMAL(20,8)", True),
    "occurred_at": ("DATETIME", False),
    "created_at": ("DATETIME", False),
    "read_at": ("DATETIME", True),
}
_DESIGN_INDEXES = {
    "uq_notifications_public_id": (("public_id",), True),
    "uq_notifications_source_outbox": (("source_outbox_id",), True),
    "ix_notifications_tenant_occurred": (("tenant_id", "source_outbox_id"), False),
}
_FK = "fk_notifications_tenant"
_DESIGN_FOREIGN_KEYS = {_FK: (("tenant_id",), "tenants", ("id",), "RESTRICT")}
_TYPE_CHECK = "ck_notifications_type"
_THRESHOLD_CHECK = "ck_notifications_threshold"
_DESIGN_CHECKS = {_TYPE_CHECK, _THRESHOLD_CHECK}

# MySQL reports a violated CHECK as 3819 (ER_CHECK_CONSTRAINT_VIOLATED).
_ER_CHECK_CONSTRAINT_VIOLATED = 3819

_NOW = dt.datetime(2026, 10, 28, 8, 30, 0)

_ALEMBIC_VERSION = text("SELECT version_num FROM alembic_version")
_SET_VERSION = "UPDATE alembic_version SET version_num = :version"
_DELETE_RULES = text(
    "SELECT CONSTRAINT_NAME, DELETE_RULE FROM information_schema.REFERENTIAL_CONSTRAINTS"
    f" WHERE CONSTRAINT_SCHEMA = DATABASE() AND TABLE_NAME = '{_TABLE}'"
)
_TRIGGERS_QUERY = text(
    "SELECT TRIGGER_NAME, EVENT_OBJECT_TABLE, ACTION_TIMING, EVENT_MANIPULATION, ACTION_STATEMENT"
    " FROM information_schema.TRIGGERS WHERE TRIGGER_SCHEMA = DATABASE()"
)
_CHECKS_QUERY = text(
    "SELECT TABLE_NAME, CONSTRAINT_NAME FROM information_schema.TABLE_CONSTRAINTS"
    " WHERE CONSTRAINT_SCHEMA = DATABASE() AND CONSTRAINT_TYPE = 'CHECK'"
)
_NOTIFICATION_ROWS = text(f"SELECT * FROM {_TABLE} ORDER BY id")
_WALLET_ROWS = text("SELECT * FROM wallets ORDER BY id")
_LEDGER_ROWS = text("SELECT * FROM wallet_transactions ORDER BY id")

# Raw SQL: the type goes past the enum, straight to the database's checks.
_INSERT = text(
    f"INSERT INTO {_TABLE} (public_id, tenant_id, notification_type, source_outbox_id, balance,"
    " threshold, occurred_at, created_at, read_at) VALUES (:public_id, :tenant_id, :type,"
    " :source, :balance, :threshold, :now, :now, :read_at)"
)

# Design §7 "CHECK": (type, threshold) the database refuses, and the ones it takes.
_ILLEGAL_ROWS = {
    "LOW_BALANCE without a threshold": ("LOW_BALANCE", None),
    "SUSPENDED with a threshold": ("SUSPENDED", "50.00000000"),
    "an unknown type": ("UNKNOWN", None),
}
_LEGAL_ROWS = [
    ("LOW_BALANCE", "50.00000000"),
    ("SUSPENDED", None),
    ("REACTIVATED", None),
]


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("migration_0028", _MIGRATION)
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


def _model_shape() -> dict[str, object]:
    """The model's columns, indexes, foreign key and check names, shaped like the migration's."""
    table = Notification.__table__
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


def _model_check_texts() -> dict[str, str]:
    return {
        str(constraint.name): _normalised(str(constraint.sqltext))
        for constraint in Notification.__table__.constraints
        if isinstance(constraint, CheckConstraint)
    }


# The design's MySQL table, clause by clause; the wrong shapes below are edits of it.
_DESIGN_CLAUSES = [
    "id BIGINT NOT NULL AUTO_INCREMENT",
    "public_id CHAR(36) NOT NULL",
    "tenant_id BIGINT NOT NULL",
    "notification_type VARCHAR(32) NOT NULL",
    "source_outbox_id BIGINT NOT NULL",
    "balance DECIMAL(20, 8) NOT NULL",
    "threshold DECIMAL(20, 8) NULL",
    "occurred_at DATETIME NOT NULL",
    "created_at DATETIME NOT NULL",
    "read_at DATETIME NULL",
    "PRIMARY KEY (id)",
    "CONSTRAINT uq_notifications_public_id UNIQUE (public_id)",
    "CONSTRAINT uq_notifications_source_outbox UNIQUE (source_outbox_id)",
    "INDEX ix_notifications_tenant_occurred (tenant_id, source_outbox_id)",
    f"CONSTRAINT {_FK} FOREIGN KEY (tenant_id) REFERENCES tenants (id) ON DELETE RESTRICT",
    f"CONSTRAINT {_TYPE_CHECK} CHECK ({TYPE_CHECK})",
    f"CONSTRAINT {_THRESHOLD_CHECK} CHECK ({THRESHOLD_CHECK})",
]


def _create_table(clauses: list[str]) -> str:
    return f"CREATE TABLE {_TABLE} ({', '.join(clauses)})"


def _without(clauses: list[str], prefix: str) -> list[str]:
    kept = [clause for clause in clauses if not clause.startswith(prefix)]
    assert len(kept) == len(clauses) - 1, prefix
    return kept


def _replaced(clauses: list[str], old: str, new: str) -> list[str]:
    assert sum(old in clause for clause in clauses) == 1, old
    return [clause.replace(old, new) for clause in clauses]


def _with_extra_column(clauses: list[str]) -> list[str]:
    at = clauses.index("read_at DATETIME NULL") + 1
    return [*clauses[:at], "extra_note VARCHAR(16) NULL", *clauses[at:]]


# Design §7 "重跑：表已存在但形状不对": the four sub-scenarios, each a fresh hand-made table.
_WRONG_SHAPE_DDL = {
    "a check missing": _create_table(_without(_DESIGN_CLAUSES, f"CONSTRAINT {_THRESHOLD_CHECK}")),
    "the tenant index missing": _create_table(
        _without(_DESIGN_CLAUSES, "INDEX ix_notifications_tenant_occurred")
    ),
    "the foreign key not RESTRICT": _create_table(
        _replaced(_DESIGN_CLAUSES, "ON DELETE RESTRICT", "ON DELETE CASCADE")
    ),
    "an extra column": _create_table(_with_extra_column(_DESIGN_CLAUSES)),
}


# ---------------------------------------------------------------------------
# No database
# ---------------------------------------------------------------------------


def test_0028_follows_0027() -> None:
    migration = _load()

    assert migration.revision == _REVISION
    assert migration.down_revision == _DOWN_REVISION
    # `alembic_version.version_num` is VARCHAR(32).
    assert len(migration.revision) <= 32


def test_0028_shape_is_the_design_and_the_models() -> None:
    """The migration's frozen literals, the model's declarations and design §2 agree: ten columns
    (type, width, nullability), two unique indexes and the tenant index in this column order, the
    tenant foreign key RESTRICT and nothing on `source_outbox_id`, the two checks by name."""
    migration = _load()
    model = _model_shape()

    assert migration._TABLE == Notification.__tablename__ == _TABLE
    assert migration._COLUMNS == _DESIGN_COLUMNS == model["columns"]
    assert list(migration._COLUMNS) == [c.name for c in Notification.__table__.columns]
    assert migration._INDEXES == _DESIGN_INDEXES == model["indexes"]
    assert migration._FOREIGN_KEYS == _DESIGN_FOREIGN_KEYS == model["foreign_keys"]
    assert set(migration._CHECKS) == _DESIGN_CHECKS == model["checks"]
    # Design §9 B: the outbox reference has no foreign key.
    assert not Notification.__table__.c.source_outbox_id.foreign_keys


def test_0028_index_and_check_order_is_the_models() -> None:
    """`__table_args__` declares the indexes and checks under the migration's names and in its
    order."""
    migration = _load()
    declared = [
        str(arg.name)
        for arg in Notification.__table_args__
        if isinstance(arg, UniqueConstraint | CheckConstraint | sqlalchemy.Index)
    ]

    assert declared == [*migration._INDEXES, *migration._CHECKS]


def test_0028_checks_are_the_ones_the_model_declares() -> None:
    """Same conditions in the migration and on the model (whitespace aside); the type check
    allows exactly the enum's three values; the threshold check is two OR'd branches."""
    migration = _load()

    assert {name: _normalised(c) for name, c in migration._CHECKS.items()} == _model_check_texts()
    assert [member.value for member in NotificationType] == [
        "LOW_BALANCE",
        "SUSPENDED",
        "REACTIVATED",
    ]
    allowed = ", ".join(f"'{member.value}'" for member in NotificationType)
    assert _normalised(TYPE_CHECK) == _normalised(f"notification_type IN ({allowed})")
    assert _normalised(THRESHOLD_CHECK) == _normalised(
        "(notification_type = 'LOW_BALANCE' AND threshold IS NOT NULL)"
        " OR (notification_type <> 'LOW_BALANCE' AND threshold IS NULL)"
    )


def test_0028_mysql_create_table_is_the_design_in_one_statement() -> None:
    """The one `CREATE TABLE` the MySQL upgrade sends is the design's clauses, checks included."""
    migration = _load()

    assert _compact(migration._CREATE_TABLE_MYSQL) == _compact(_create_table(_DESIGN_CLAUSES))
    assert migration._CREATE_TABLE_MYSQL.count(";") == 0


class _Result:
    def __init__(self, rows: list[tuple[object, ...]]) -> None:
        self._rows = rows

    def all(self) -> list[tuple[object, ...]]:
        return list(self._rows)

    def first(self) -> tuple[object, ...] | None:
        return self._rows[0] if self._rows else None


class _FakeBind:
    """Answers the two queries the migration sends: the delete rules and "any row?"."""

    def __init__(self, delete_rules: dict[str, str], has_rows: bool) -> None:
        self._delete_rules = delete_rules
        self._has_rows = has_rows
        self.queries: list[str] = []

    def execute(self, statement: object) -> _Result:
        sql = str(statement)
        self.queries.append(sql)
        if "REFERENTIAL_CONSTRAINTS" in sql:
            return _Result(list(self._delete_rules.items()))
        if sql == f"SELECT 1 FROM {_TABLE} LIMIT 1":
            return _Result([(1,)] if self._has_rows else [])
        raise AssertionError(sql)


class _FakeInspector:
    """`Inspector` over one reflected table (or none)."""

    def __init__(self, table: dict | None) -> None:
        self._table = table
        self.asked: list[str] = []

    def _get(self, key: str, name: str) -> list[dict]:
        assert name == _TABLE
        assert self._table is not None
        self.asked.append(key)
        return copy.deepcopy(self._table[key])

    def has_table(self, name: str) -> bool:
        assert name == _TABLE
        self.asked.append("has_table")
        return self._table is not None

    def get_columns(self, name: str) -> list[dict]:
        return self._get("columns", name)

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


def _design_table() -> dict:
    """The table the way MySQL's inspector reflects it: unique constraints are also indexes."""
    types = {
        "BIGINT": mysql.BIGINT(),
        "CHAR(36)": mysql.CHAR(length=36),
        "VARCHAR(32)": mysql.VARCHAR(length=32),
        "DECIMAL(20,8)": mysql.DECIMAL(precision=20, scale=8),
        "DATETIME": mysql.DATETIME(),
    }
    return {
        "columns": [
            {"name": name, "type": types[kind], "nullable": nullable}
            for name, (kind, nullable) in _DESIGN_COLUMNS.items()
        ],
        "indexes": [
            {"name": name, "column_names": list(columns), "unique": unique}
            for name, (columns, unique) in _DESIGN_INDEXES.items()
        ],
        "unique_constraints": [
            {"name": name, "column_names": list(columns)}
            for name, (columns, unique) in _DESIGN_INDEXES.items()
            if unique
        ],
        "foreign_keys": [
            {
                "name": _FK,
                "constrained_columns": ["tenant_id"],
                "referred_schema": None,
                "referred_table": "tenants",
                "referred_columns": ["id"],
                "options": {"ondelete": "RESTRICT"},
            }
        ],
        "check_constraints": [
            {"name": name, "sqltext": "…"} for name in (_TYPE_CHECK, _THRESHOLD_CHECK)
        ],
        "delete_rules": {_FK: "RESTRICT"},
    }


def _with(
    monkeypatch,
    table: dict | None,
    *,
    dialect: str = "mysql",
    as_sql: bool = False,
    has_rows: bool = False,
) -> tuple[ModuleType, _FakeOp, _FakeInspector, _FakeBind]:
    migration = _load()
    rules = dict(table["delete_rules"]) if table is not None else {}
    bind = _FakeBind(rules, has_rows)
    fake = _FakeOp(bind, dialect=dialect, as_sql=as_sql)
    inspector = _FakeInspector(table)
    monkeypatch.setattr(migration, "op", fake)
    monkeypatch.setattr(migration, "sa", _Sa(inspector))
    return migration, fake, inspector, bind


def test_0028_upgrade_creates_the_missing_table_in_one_statement(monkeypatch) -> None:
    migration, fake, _, _ = _with(monkeypatch, None)
    migration.upgrade()

    [(kind, (statement,))] = fake.calls
    assert kind == "execute"
    assert _normalised(statement).startswith(f"CREATE TABLE {_TABLE.upper()} (")


@pytest.mark.parametrize("dialect", ["mysql", "sqlite"])
def test_0028_upgrade_with_the_design_shape_issues_no_ddl(monkeypatch, dialect: str) -> None:
    migration, fake, inspector, _ = _with(monkeypatch, _design_table(), dialect=dialect)
    migration.upgrade()

    assert fake.calls == []
    assert {"columns", "indexes", "foreign_keys", "check_constraints"} <= set(inspector.asked)


def _drop_check(table: dict) -> None:
    table["check_constraints"] = [
        c for c in table["check_constraints"] if c["name"] != _THRESHOLD_CHECK
    ]


def _extra_check(table: dict) -> None:
    table["check_constraints"].append({"name": "ck_notifications_extra", "sqltext": "…"})


def _drop_tenant_index(table: dict) -> None:
    # What MySQL leaves without it: an index of its own for the foreign key.
    table["indexes"] = [
        i for i in table["indexes"] if i["name"] != "ix_notifications_tenant_occurred"
    ]
    table["indexes"].append({"name": _FK, "column_names": ["tenant_id"], "unique": False})


def _reverse_tenant_index(table: dict) -> None:
    for index in table["indexes"]:
        if index["name"] == "ix_notifications_tenant_occurred":
            index["column_names"] = ["source_outbox_id", "tenant_id"]


def _unique_tenant_index(table: dict) -> None:
    for index in table["indexes"]:
        if index["name"] == "ix_notifications_tenant_occurred":
            index["unique"] = True


def _public_id_not_unique(table: dict) -> None:
    for index in table["indexes"]:
        if index["name"] == "uq_notifications_public_id":
            index["unique"] = False
    table["unique_constraints"] = [
        u for u in table["unique_constraints"] if u["name"] != "uq_notifications_public_id"
    ]


def _prefix_length(table: dict) -> None:
    for index in table["indexes"]:
        if index["name"] == "uq_notifications_public_id":
            index["dialect_options"] = {"mysql_length": {"public_id": 8}}


def _cascade(table: dict) -> None:
    table["delete_rules"] = {_FK: "CASCADE"}
    table["foreign_keys"][0]["options"] = {"ondelete": "CASCADE"}


def _no_foreign_key(table: dict) -> None:
    table["foreign_keys"] = []
    table["delete_rules"] = {}


def _outbox_foreign_key(table: dict) -> None:
    table["foreign_keys"].append(
        {
            "name": "fk_notifications_source_outbox",
            "constrained_columns": ["source_outbox_id"],
            "referred_schema": None,
            "referred_table": "domain_outbox",
            "referred_columns": ["id"],
            "options": {"ondelete": "RESTRICT"},
        }
    )
    table["delete_rules"]["fk_notifications_source_outbox"] = "RESTRICT"


def _extra_column(table: dict) -> None:
    table["columns"].append({"name": "extra_note", "type": mysql.VARCHAR(16), "nullable": True})


def _missing_column(table: dict) -> None:
    table["columns"] = [c for c in table["columns"] if c["name"] != "read_at"]


def _nullable_balance(table: dict) -> None:
    for column in table["columns"]:
        if column["name"] == "balance":
            column["nullable"] = True


def _wider_type(table: dict) -> None:
    for column in table["columns"]:
        if column["name"] == "notification_type":
            column["type"] = mysql.VARCHAR(64)


def _float_balance(table: dict) -> None:
    for column in table["columns"]:
        if column["name"] == "balance":
            column["type"] = mysql.DOUBLE()


_WRONG_SHAPES = {
    "a check missing": _drop_check,
    "an extra check": _extra_check,
    "the tenant index missing": _drop_tenant_index,
    "the tenant index in another order": _reverse_tenant_index,
    "the tenant index unique": _unique_tenant_index,
    "public_id not unique": _public_id_not_unique,
    "a prefix length": _prefix_length,
    "the foreign key not RESTRICT": _cascade,
    "no foreign key": _no_foreign_key,
    "a foreign key on source_outbox_id": _outbox_foreign_key,
    "an extra column": _extra_column,
    "a column missing": _missing_column,
    "balance nullable": _nullable_balance,
    "notification_type wider": _wider_type,
    "balance a float": _float_balance,
}


@pytest.mark.parametrize("shape", list(_WRONG_SHAPES))
@pytest.mark.parametrize("dialect", ["mysql", "sqlite"])
def test_0028_upgrade_refuses_a_wrong_shape_before_any_ddl(
    monkeypatch, shape: str, dialect: str
) -> None:
    """Design §8: any item missing, extra or different — nothing is built, the message names the
    table and how to clear it."""
    table = _design_table()
    _WRONG_SHAPES[shape](table)
    migration, fake, _, _ = _with(monkeypatch, table, dialect=dialect)

    with pytest.raises(RuntimeError) as raised:
        migration.upgrade()

    assert fake.calls == []
    message = str(raised.value)
    assert f"Table {_TABLE} exists" in message
    assert "No DDL was issued" in message
    assert f"DROP TABLE {_TABLE}" in message


def test_0028_upgrade_on_sqlite_creates_the_table_and_its_index(monkeypatch) -> None:
    migration, fake, _, _ = _with(monkeypatch, None, dialect="sqlite")
    migration.upgrade()

    assert fake.calls == [
        ("create_table", (_TABLE,)),
        (
            "create_index",
            ("ix_notifications_tenant_occurred", _TABLE, ["tenant_id", "source_outbox_id"], False),
        ),
    ]


def test_0028_offline_upgrade_emits_the_table_without_reading(monkeypatch) -> None:
    migration, fake, inspector, bind = _with(monkeypatch, _design_table(), as_sql=True)
    migration.upgrade()

    assert inspector.asked == []
    assert bind.queries == []
    [(kind, (statement,))] = fake.calls
    assert kind == "execute"
    assert _normalised(statement).startswith(f"CREATE TABLE {_TABLE.upper()} (")


def test_0028_offline_downgrade_refuses_before_reading_or_ddl(monkeypatch) -> None:
    """Design §8: offline the emptiness cannot be checked — refuse, no DROP TABLE."""
    migration, fake, inspector, bind = _with(monkeypatch, _design_table(), as_sql=True)

    with pytest.raises(RuntimeError) as raised:
        migration.downgrade()

    assert fake.calls == []
    assert inspector.asked == []
    assert bind.queries == []
    message = str(raised.value)
    assert _TABLE in message
    assert "online" in message


@pytest.mark.parametrize("dialect", ["mysql", "sqlite"])
def test_0028_downgrade_drops_an_empty_table(monkeypatch, dialect: str) -> None:
    migration, fake, _, bind = _with(monkeypatch, _design_table(), dialect=dialect)
    migration.downgrade()

    assert bind.queries == [f"SELECT 1 FROM {_TABLE} LIMIT 1"]
    assert fake.calls == [("drop_table", (_TABLE,))]


@pytest.mark.parametrize("dialect", ["mysql", "sqlite"])
def test_0028_downgrade_refuses_with_rows_before_any_ddl(monkeypatch, dialect: str) -> None:
    """Design §5 / §8: a row in the table — no DDL, no row deleted; the message names the table,
    that it is not empty, and to roll back only the application image."""
    migration, fake, _, _ = _with(monkeypatch, _design_table(), dialect=dialect, has_rows=True)

    with pytest.raises(RuntimeError) as raised:
        migration.downgrade()

    assert fake.calls == []
    message = str(raised.value)
    assert f"table {_TABLE} is not empty" in message
    assert "roll back only the application image" in message


@pytest.mark.parametrize("dialect", ["mysql", "sqlite"])
def test_0028_downgrade_without_the_table_does_nothing(monkeypatch, dialect: str) -> None:
    migration, fake, inspector, bind = _with(monkeypatch, None, dialect=dialect)
    migration.downgrade()

    assert inspector.asked == ["has_table"]
    assert bind.queries == []
    assert fake.calls == []


@contextmanager
def _sqlite() -> Iterator[Engine]:
    """The unit tests build SQLite with `create_all` from the models, not with the migrations."""
    engine = create_engine("sqlite+pysqlite:///:memory:")
    try:
        Base.metadata.create_all(engine, tables=[Tenant.__table__, Notification.__table__])
        yield engine
    finally:
        engine.dispose()


def _insert_tenant(connection: sqlalchemy.Connection) -> int:
    return int(
        connection.execute(
            insert(Tenant).values(
                public_id=str(uuid.uuid4()),
                company_name="Notifications Migration Sdn Bhd",
                email="ops@example.com",
                created_at=_NOW,
                updated_at=_NOW,
            )
        ).inserted_primary_key[0]
    )


def _params(tenant_id: int, kind: str, threshold: str | None, source: int) -> dict[str, object]:
    return {
        "public_id": str(uuid.uuid4()),
        "tenant_id": tenant_id,
        "type": kind,
        "source": source,
        "balance": "45.00000000",
        "threshold": threshold,
        # A literal, as the amounts: both databases take it, no driver adapter is involved.
        "now": _NOW.strftime("%Y-%m-%d %H:%M:%S"),
        "read_at": None,
    }


def test_0028_sqlite_tables_get_the_indexes_and_the_foreign_key() -> None:
    with _sqlite() as engine:
        inspector = inspect(engine)
        indexes = {
            str(index["name"]): (tuple(index["column_names"]), bool(index["unique"]))
            for index in inspector.get_indexes(_TABLE)
        }
        uniques = {
            str(unique["name"]): tuple(unique["column_names"])
            for unique in inspector.get_unique_constraints(_TABLE)
        }
        [key] = inspector.get_foreign_keys(_TABLE)

    assert indexes["ix_notifications_tenant_occurred"] == (("tenant_id", "source_outbox_id"), False)
    assert uniques == {
        "uq_notifications_public_id": ("public_id",),
        "uq_notifications_source_outbox": ("source_outbox_id",),
    }
    assert (key["name"], key["constrained_columns"], key["referred_table"]) == (
        _FK,
        ["tenant_id"],
        "tenants",
    )
    assert str(key["options"].get("ondelete")).upper() == "RESTRICT"


@pytest.mark.parametrize("row", list(_ILLEGAL_ROWS))
def test_0028_sqlite_checks_refuse_the_illegal_rows(row: str) -> None:
    """Design §7 "CHECK" on SQLite: each of the three is refused; the three legal kinds go in."""
    kind, threshold = _ILLEGAL_ROWS[row]
    with _sqlite() as engine:
        with engine.begin() as connection:
            tenant_id = _insert_tenant(connection)
            for source, (good_kind, good_threshold) in enumerate(_LEGAL_ROWS, start=1):
                connection.execute(_INSERT, _params(tenant_id, good_kind, good_threshold, source))

        with pytest.raises(DBAPIError) as raised:
            with engine.begin() as connection:
                connection.execute(_INSERT, _params(tenant_id, kind, threshold, 100))
        assert "CHECK constraint failed" in str(raised.value)

        with engine.connect() as connection:
            count = connection.execute(text(f"SELECT COUNT(*) FROM {_TABLE}")).scalar_one()
        assert count == len(_LEGAL_ROWS)


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


def _has_table(engine: Engine) -> bool:
    return inspect(engine).has_table(_TABLE)


def _table_shape(engine: Engine) -> dict[str, object]:
    """`notifications` as reflected, in the migration's terms; the delete rule from
    information_schema; prefix lengths kept so they would show."""
    inspector = inspect(engine)
    with engine.connect() as connection:
        rules = dict(connection.execute(_DELETE_RULES).all())
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
    """Design §7 "迁移形状": reflected = the model's = the design's."""
    model = _model_shape()
    shape = _table_shape(engine)
    assert shape["columns"] == model["columns"] == _DESIGN_COLUMNS
    assert shape["indexes"] == model["indexes"] == _DESIGN_INDEXES
    assert shape["foreign_keys"] == model["foreign_keys"] == _DESIGN_FOREIGN_KEYS
    assert shape["checks"] == model["checks"] == _DESIGN_CHECKS


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
        triggers = {tuple(row) for row in connection.execute(_TRIGGERS_QUERY).all()}
        checks = {tuple(row) for row in connection.execute(_CHECKS_QUERY).all()}
    return {"tables": tables, "triggers": triggers, "checks": checks}


def _without_ours(snapshot: dict[str, object]) -> dict[str, object]:
    tables = snapshot["tables"]
    assert isinstance(tables, dict)
    checks = snapshot["checks"]
    assert isinstance(checks, set)
    return {
        "tables": {name: shape for name, shape in tables.items() if name != _TABLE},
        "triggers": snapshot["triggers"],
        "checks": {check for check in checks if check[0] != _TABLE},
    }


def _ledger_and_wallets(engine: Engine) -> tuple[list[tuple[object, ...]], ...]:
    with engine.connect() as connection:
        wallets = [tuple(row) for row in connection.execute(_WALLET_ROWS).all()]
        ledger = [tuple(row) for row in connection.execute(_LEDGER_ROWS).all()]
    return wallets, ledger


def _at_0027(alembic_config: Config, engine: Engine) -> None:
    command.upgrade(alembic_config, "head")
    command.downgrade(alembic_config, _DOWN_REVISION)
    assert _version(engine) == _DOWN_REVISION
    assert not _has_table(engine)


def _back_to_head(alembic_config: Config, engine: Engine) -> None:
    # A hand-made wrong shape would make the upgrade refuse; it is empty and is dropped.
    if _has_table(engine) and _version(engine) == _DOWN_REVISION:
        if _table_shape(engine) != _model_shape():
            _execute(engine, f"DROP TABLE {_TABLE}")
    command.upgrade(alembic_config, "head")


@needs_mysql
def test_0028_round_trip_on_an_empty_table(alembic_config: Config, engine: Engine) -> None:
    """Design §7 "迁移形状": 0027 → 0028 → 0027 → 0028 with the table empty. The upgrade
    sends one `CREATE TABLE`, the shape is the model's; the downgrade sends one `DROP TABLE`;
    every other table, key, check and trigger and the wallet and ledger rows are the same
    throughout (§3 INV-4 / INV-5)."""
    _at_0027(alembic_config, engine)
    try:
        at_0027 = _schema_snapshot(engine)
        rows = _ledger_and_wallets(engine)

        with _ddl_statements() as statements:
            command.upgrade(alembic_config, _REVISION)
        assert len(statements) == 1, statements
        assert _normalised(statements[0]).startswith(f"CREATE TABLE {_TABLE.upper()} (")
        assert _version(engine) == _REVISION
        _assert_design_shape(engine)
        at_0028 = _schema_snapshot(engine)
        assert _without_ours(at_0028) == at_0027
        assert _ledger_and_wallets(engine) == rows

        with _ddl_statements() as statements:
            command.downgrade(alembic_config, _DOWN_REVISION)
        assert len(statements) == 1, statements
        assert _normalised(statements[0]) == f"DROP TABLE {_TABLE.upper()}"
        assert _version(engine) == _DOWN_REVISION
        assert not _has_table(engine)
        assert _schema_snapshot(engine) == at_0027
        assert _ledger_and_wallets(engine) == rows

        command.upgrade(alembic_config, _REVISION)
        assert _schema_snapshot(engine) == at_0028
        _assert_design_shape(engine)
        assert _ledger_and_wallets(engine) == rows
    finally:
        _back_to_head(alembic_config, engine)


@needs_mysql
def test_0028_rerun_with_the_table_built_only_moves_the_version(
    alembic_config: Config, engine: Engine
) -> None:
    """Design §7 "重跑：表已存在且形状正确": upgraded, then `alembic_version` put back to
    0027 (as if writing the version failed after the table was built) — no DDL, the version
    moves to 0028, the table is as after the first upgrade."""
    _at_0027(alembic_config, engine)
    try:
        command.upgrade(alembic_config, _REVISION)
        after_first = _schema_snapshot(engine)
        _execute(engine, _SET_VERSION, {"version": _DOWN_REVISION})
        assert _version(engine) == _DOWN_REVISION

        with _ddl_statements() as statements:
            command.upgrade(alembic_config, _REVISION)

        assert statements == []
        assert _version(engine) == _REVISION
        assert _schema_snapshot(engine) == after_first
        _assert_design_shape(engine)
    finally:
        _back_to_head(alembic_config, engine)


@needs_mysql
@pytest.mark.parametrize("shape", list(_WRONG_SHAPE_DDL))
def test_0028_rerun_with_a_wrong_shape_fails_before_any_ddl(
    alembic_config: Config, engine: Engine, shape: str
) -> None:
    """Design §7 "重跑：表已存在但形状不对": version 0027 and a fresh hand-made
    `notifications` of another shape — the upgrade fails naming the table, issues no DDL, the
    version stays 0027 and the hand-made table is as it was."""
    _at_0027(alembic_config, engine)
    try:
        _execute(engine, _WRONG_SHAPE_DDL[shape])
        hand_made = _table_shape(engine)
        assert hand_made != _model_shape()

        with _ddl_statements() as statements:
            with pytest.raises(RuntimeError) as raised:
                command.upgrade(alembic_config, _REVISION)

        assert statements == []
        message = str(raised.value)
        assert f"Table {_TABLE} exists" in message
        assert f"DROP TABLE {_TABLE}" in message
        assert _version(engine) == _DOWN_REVISION
        assert _table_shape(engine) == hand_made
    finally:
        if _has_table(engine) and _version(engine) == _DOWN_REVISION:
            _execute(engine, f"DROP TABLE {_TABLE}")
        _back_to_head(alembic_config, engine)


def _seed_notifications(engine: Engine) -> int:
    """A tenant and two notifications, the second one read. Returns the tenant id."""
    with engine.begin() as connection:
        tenant_id = _insert_tenant(connection)
        connection.execute(
            insert(Notification).values(
                public_id=str(uuid.uuid4()),
                tenant_id=tenant_id,
                notification_type=NotificationType.LOW_BALANCE,
                source_outbox_id=910001,
                balance=Decimal("45.00000000"),
                threshold=Decimal("50.00000000"),
                occurred_at=_NOW,
                created_at=_NOW,
                read_at=None,
            )
        )
        connection.execute(
            insert(Notification).values(
                public_id=str(uuid.uuid4()),
                tenant_id=tenant_id,
                notification_type=NotificationType.SUSPENDED,
                source_outbox_id=910002,
                balance=Decimal("0.00000000"),
                threshold=None,
                occurred_at=_NOW,
                created_at=_NOW,
                read_at=_NOW + dt.timedelta(minutes=5),
            )
        )
    return tenant_id


def _clean_notifications(engine: Engine, tenant_id: int) -> None:
    with engine.begin() as connection:
        if inspect(connection).has_table(_TABLE):
            connection.execute(delete(Notification).where(Notification.tenant_id == tenant_id))
        connection.execute(delete(Tenant).where(Tenant.id == tenant_id))


@needs_mysql
def test_0028_downgrade_with_rows_refuses_before_any_ddl(
    alembic_config: Config, engine: Engine
) -> None:
    """Design §7 "带数据降级" / §5: two notifications, one read — the downgrade fails, no
    DDL, the table, both rows and `read_at` are as they were, `alembic_version` stays 0028."""
    command.upgrade(alembic_config, "head")
    tenant_id: int | None = None
    try:
        # Later revisions are not this downgrade's: start at 0028.
        command.downgrade(alembic_config, _REVISION)
        tenant_id = _seed_notifications(engine)
        before_shape = _schema_snapshot(engine)
        with engine.connect() as connection:
            before_rows = [tuple(row) for row in connection.execute(_NOTIFICATION_ROWS).all()]
        assert len(before_rows) == 2
        assert [row[-1] is None for row in before_rows] == [True, False]

        with _ddl_statements() as statements:
            with pytest.raises(RuntimeError) as raised:
                command.downgrade(alembic_config, _DOWN_REVISION)

        assert statements == []
        message = str(raised.value)
        assert f"table {_TABLE} is not empty" in message
        assert "roll back only the application image" in message
        assert _version(engine) == _REVISION
        assert _schema_snapshot(engine) == before_shape
        with engine.connect() as connection:
            assert [tuple(row) for row in connection.execute(_NOTIFICATION_ROWS).all()] == (
                before_rows
            )
    finally:
        if tenant_id is not None:
            _clean_notifications(engine, tenant_id)
        _back_to_head(alembic_config, engine)


@needs_mysql
def test_0028_offline_downgrade_refuses_without_drop_table(alembic_config: Config) -> None:
    """Design §8: `--sql` cannot tell whether the table is empty — the downgrade fails saying it
    must run online, and its output has no DROP TABLE. The offline upgrade, as 0027's, emits the
    table without reading the database."""
    upgrade_buffer = io.StringIO()
    command.upgrade(
        Config("alembic.ini", output_buffer=upgrade_buffer),
        f"{_DOWN_REVISION}:{_REVISION}",
        sql=True,
    )
    assert f"CREATE TABLE {_TABLE.upper()} (" in _normalised(upgrade_buffer.getvalue())

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
    assert "DROP TABLE" not in _normalised(downgrade_buffer.getvalue())


@needs_mysql
@pytest.mark.parametrize("row", list(_ILLEGAL_ROWS))
def test_0028_mysql_checks_refuse_the_illegal_rows(
    alembic_config: Config, engine: Engine, row: str
) -> None:
    """Design §7 "CHECK" on MySQL: each of the three is refused by its CHECK (3819); the three
    legal kinds go in. Everything is rolled back."""
    command.upgrade(alembic_config, "head")
    kind, threshold = _ILLEGAL_ROWS[row]
    with engine.connect() as connection:
        transaction = connection.begin()
        try:
            tenant_id = _insert_tenant(connection)
            for source, (good_kind, good_threshold) in enumerate(_LEGAL_ROWS, start=920001):
                connection.execute(_INSERT, _params(tenant_id, good_kind, good_threshold, source))

            with pytest.raises(DBAPIError) as raised:
                with connection.begin_nested():
                    connection.execute(_INSERT, _params(tenant_id, kind, threshold, 920100))
            assert int(raised.value.orig.args[0]) == _ER_CHECK_CONSTRAINT_VIOLATED, row

            count = connection.execute(
                text(f"SELECT COUNT(*) FROM {_TABLE} WHERE tenant_id = :tenant_id"),
                {"tenant_id": tenant_id},
            ).scalar_one()
            assert count == len(_LEGAL_ROWS)
        finally:
            transaction.rollback()

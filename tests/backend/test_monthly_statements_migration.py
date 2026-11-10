"""0035_monthly_statements（AIH-TASK-154，设计闸门 #565 v6）.

Design §2 "数据库", §3 INV-12, §4 (the UPDATE / DELETE row), §7 (the "触发器" and "迁移" rows,
task 154), §8.

The first cases need no database: the revision chain; the columns, indexes, foreign keys and
checks in the migration equal the model's and the design's; the `CREATE TABLE` and the two
`CREATE TRIGGER` the MySQL upgrade sends; every branch of the upgrade (missing table / design
shape with both triggers / one trigger missing / a trigger that differs / each wrong shape /
the prerequisite check) and of the downgrade, with `alembic.op` and the inspector stood in;
`create_all` on SQLite (what the unit tests run) builds the table, and its four checks refuse
every illegal row.

On a real MySQL (`BILLING_TEST_DATABASE_URL`; skipped when unset, never skipped in CI): design
§7's migration row — the 0034 → 0035 → 0034 → 0035 round trip on an empty table (every other
table, key, check and trigger and the wallet and ledger rows unchanged), the rerun with
everything already built, the wrong shapes (each with a fresh hand-made table), only one
trigger missing (only that one is built), a trigger whose body or timing differs (refused, no
DDL), the downgrade with a row, the `--sql` downgrade — the four checks, and design §7's
trigger row: UPDATE (status, snapshot_json, statement_number) and DELETE of a FINALIZED and of
a VOID row fail with SQLSTATE 45000, a DRAFT row can be changed and deleted. The Alembic chain
does not run on SQLite (0006 onwards ask for `@@log_bin`), so every migration scenario is
MySQL's, as in test_migrations.py. Captured statements are compared after normalising
(backticks, case, spaces). Rows the tests write are removed with foreign key checks off and
TRUNCATE (the triggers refuse DELETE; they are never dropped to clean up).
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
from sqlalchemy.exc import DBAPIError

from alembic import command
from app.models.base import Base
from app.models.statements import (
    FINALIZED_AT_CHECK,
    HIGH_SEQUENCE_CHECK,
    PERIOD_CHECK,
    STATUS_CHECK,
    MonthlyStatement,
    StatementStatus,
)

TEST_DATABASE_URL = os.environ.get("BILLING_TEST_DATABASE_URL", "")

needs_mysql = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="BILLING_TEST_DATABASE_URL is not set; migration tests need a real MySQL",
)

_REVISION = "0035_monthly_statements"
_DOWN_REVISION = "0034_tax_policy_backstop"
_MIGRATION = pathlib.Path("alembic/versions/20261104_0035_monthly_statements.py")
_TABLE = "monthly_statements"
_UPDATE_TRIGGER = "trg_monthly_statements_before_update"
_DELETE_TRIGGER = "trg_monthly_statements_before_delete"

# Design §2 "数据库".
_DESIGN_COLUMNS = {
    "id": ("BIGINT", False),
    "public_id": ("CHAR(36)", False),
    "tenant_id": ("BIGINT", False),
    "statement_number": ("VARCHAR(32)", False),
    "period_start": ("DATETIME", False),
    "period_end": ("DATETIME", False),
    "cutoff_at": ("DATETIME", False),
    "status": ("VARCHAR(16)", False),
    "ledger_high_sequence": ("BIGINT", False),
    "snapshot_json": ("JSON", False),
    "generated_by": ("BIGINT", True),
    "tax_policy_version_id": ("BIGINT", False),
    "finalized_at": ("DATETIME", True),
    "created_at": ("DATETIME", False),
}
_TENANT_PERIOD_UNIQUE = "uq_monthly_statements_tenant_period"
_GENERATED_BY_INDEX = "ix_monthly_statements_generated_by"
_TAX_POLICY_INDEX = "ix_monthly_statements_tax_policy_version_id"
_DESIGN_INDEXES = {
    "uq_monthly_statements_public_id": (("public_id",), True),
    "uq_monthly_statements_statement_number": (("statement_number",), True),
    _TENANT_PERIOD_UNIQUE: (("tenant_id", "period_start"), True),
    "ix_monthly_statements_period": (("period_start", "tenant_id"), False),
    _GENERATED_BY_INDEX: (("generated_by",), False),
    _TAX_POLICY_INDEX: (("tax_policy_version_id",), False),
}
_FK_TENANT = "fk_monthly_statements_tenant_id"
_FK_GENERATED_BY = "fk_monthly_statements_generated_by"
_FK_TAX_POLICY = "fk_monthly_statements_tax_policy_version_id"
_DESIGN_FOREIGN_KEYS = {
    _FK_TENANT: (("tenant_id",), "tenants", ("id",), "RESTRICT"),
    _FK_GENERATED_BY: (("generated_by",), "users", ("id",), "RESTRICT"),
    _FK_TAX_POLICY: (("tax_policy_version_id",), "tax_policy_versions", ("id",), "RESTRICT"),
}
_DESIGN_CHECK_TEXTS = {
    "ck_monthly_statements_status": "status IN ('DRAFT', 'FINALIZED', 'VOID')",
    "ck_monthly_statements_period": "period_start < period_end AND period_end < cutoff_at",
    "ck_monthly_statements_high_sequence": "ledger_high_sequence >= 0",
    "ck_monthly_statements_finalized_at": "status <> 'FINALIZED' OR finalized_at IS NOT NULL",
}
_DESIGN_CHECKS = set(_DESIGN_CHECK_TEXTS)
# The task contract's trigger body.
_DESIGN_TRIGGER_BODY = (
    "BEGIN IF OLD.status <> 'DRAFT' THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT ="
    " 'monthly_statements: a finalized or void statement is immutable'; END IF; END"
)
_DESIGN_TRIGGERS = {
    _UPDATE_TRIGGER: (_TABLE, "BEFORE", "UPDATE"),
    _DELETE_TRIGGER: (_TABLE, "BEFORE", "DELETE"),
}
_IMMUTABLE = "monthly_statements: a finalized or void statement is immutable"

# MySQL reports a violated CHECK as 3819 (ER_CHECK_CONSTRAINT_VIOLATED); a SIGNAL with
# SQLSTATE '45000' (an unhandled user-defined exception) as 1644 (ER_SIGNAL_EXCEPTION).
_ER_CHECK_CONSTRAINT_VIOLATED = 3819
_ER_SIGNAL_EXCEPTION = 1644

# Period 2026-12 (design §7 "账期边界"), as literals: both databases take them.
_PERIOD_START = "2026-11-30 16:00:00"
_PERIOD_END = "2026-12-31 16:00:00"
_CUTOFF_AT = "2027-01-01 16:00:00"
_CREATED_AT = "2027-01-02 01:00:00"

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
_WALLET_ROWS = text("SELECT * FROM wallets ORDER BY id")
_LEDGER_ROWS = text("SELECT * FROM wallet_transactions ORDER BY id")

# Raw SQL: the status goes past the enum, straight to the database's checks.
_INSERT_STATEMENT = text(
    f"INSERT INTO {_TABLE} (public_id, tenant_id, statement_number, period_start, period_end,"
    " cutoff_at, status, ledger_high_sequence, snapshot_json, generated_by,"
    " tax_policy_version_id, finalized_at, created_at) VALUES (:public_id, :tenant_id,"
    " :statement_number, :period_start, :period_end, :cutoff_at, :status, :high, :snapshot,"
    " :generated_by, :tax_policy_version_id, :finalized_at, :created_at)"
)
_ID_OF = text(f"SELECT id FROM {_TABLE} WHERE public_id = :public_id")
_COUNT = text(f"SELECT COUNT(*) FROM {_TABLE}")

# The four checks: name → (overrides on a legal FINALIZED row, the check that refuses it).
_ILLEGAL_ROWS: dict[str, tuple[dict[str, object], str]] = {
    "an unknown status": ({"status": "PAID"}, "ck_monthly_statements_status"),
    "period_start equal to period_end": (
        {"period_end": _PERIOD_START},
        "ck_monthly_statements_period",
    ),
    "period_end after cutoff_at": (
        {"cutoff_at": "2026-12-31 15:59:59"},
        "ck_monthly_statements_period",
    ),
    "cutoff_at equal to period_end": ({"cutoff_at": _PERIOD_END}, "ck_monthly_statements_period"),
    "a negative ledger_high_sequence": ({"high": -1}, "ck_monthly_statements_high_sequence"),
    "FINALIZED without finalized_at": (
        {"finalized_at": None},
        "ck_monthly_statements_finalized_at",
    ),
}
# Legal rows next to them: the boundaries the checks let through.
_LEGAL_ROWS: dict[str, dict[str, object]] = {
    "FINALIZED": {},
    "DRAFT without finalized_at": {"status": "DRAFT", "finalized_at": None},
    "VOID without finalized_at": {"status": "VOID", "finalized_at": None},
    "ledger_high_sequence 0": {"high": 0},
    "generated by an admin": {"generated_by": 7},
}


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("migration_0035", _MIGRATION)
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
    return _type_text(column.type.compile(dialect=mysql.dialect()))


def _model_shape() -> dict[str, object]:
    """The model's columns, indexes, foreign keys and check names, shaped like the migration's."""
    table = MonthlyStatement.__table__
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


def _design_shape() -> dict[str, object]:
    return {
        "columns": _DESIGN_COLUMNS,
        "indexes": _DESIGN_INDEXES,
        "foreign_keys": _DESIGN_FOREIGN_KEYS,
        "checks": _DESIGN_CHECKS,
    }


def _migration_shape(migration: ModuleType) -> dict[str, object]:
    return {
        "columns": migration._COLUMNS,
        "indexes": migration._INDEXES,
        "foreign_keys": migration._FOREIGN_KEYS,
        "checks": set(migration._CHECKS),
    }


# The design's MySQL table, clause by clause; the wrong shapes below are edits of it.
_CLAUSES = [
    "id BIGINT NOT NULL AUTO_INCREMENT",
    "public_id CHAR(36) NOT NULL",
    "tenant_id BIGINT NOT NULL",
    "statement_number VARCHAR(32) NOT NULL",
    "period_start DATETIME NOT NULL",
    "period_end DATETIME NOT NULL",
    "cutoff_at DATETIME NOT NULL",
    "status VARCHAR(16) NOT NULL",
    "ledger_high_sequence BIGINT NOT NULL",
    "snapshot_json JSON NOT NULL",
    "generated_by BIGINT NULL",
    "tax_policy_version_id BIGINT NOT NULL",
    "finalized_at DATETIME NULL",
    "created_at DATETIME NOT NULL",
    "PRIMARY KEY (id)",
    "CONSTRAINT uq_monthly_statements_public_id UNIQUE (public_id)",
    "CONSTRAINT uq_monthly_statements_statement_number UNIQUE (statement_number)",
    f"CONSTRAINT {_TENANT_PERIOD_UNIQUE} UNIQUE (tenant_id, period_start)",
    "INDEX ix_monthly_statements_period (period_start, tenant_id)",
    f"INDEX {_GENERATED_BY_INDEX} (generated_by)",
    f"INDEX {_TAX_POLICY_INDEX} (tax_policy_version_id)",
    f"CONSTRAINT {_FK_TENANT} FOREIGN KEY (tenant_id) REFERENCES tenants (id) ON DELETE RESTRICT",
    f"CONSTRAINT {_FK_GENERATED_BY} FOREIGN KEY (generated_by) REFERENCES users (id)"
    " ON DELETE RESTRICT",
    f"CONSTRAINT {_FK_TAX_POLICY} FOREIGN KEY (tax_policy_version_id)"
    " REFERENCES tax_policy_versions (id) ON DELETE RESTRICT",
    f"CONSTRAINT ck_monthly_statements_status CHECK ({STATUS_CHECK})",
    f"CONSTRAINT ck_monthly_statements_period CHECK ({PERIOD_CHECK})",
    f"CONSTRAINT ck_monthly_statements_high_sequence CHECK ({HIGH_SEQUENCE_CHECK})",
    f"CONSTRAINT ck_monthly_statements_finalized_at CHECK ({FINALIZED_AT_CHECK})",
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
    at = clauses.index("created_at DATETIME NOT NULL") + 1
    return [*clauses[:at], "pdf_path VARCHAR(255) NULL", *clauses[at:]]


_DDL = _create_table(_CLAUSES)

# Design §7 "迁移" (形状校验): each sub-scenario is a fresh hand-made table.
_WRONG_SHAPE_DDL: dict[str, str] = {
    # MySQL then builds an index of its own, named after the foreign key.
    "without the tax policy version index": _create_table(
        _without(_CLAUSES, f"INDEX {_TAX_POLICY_INDEX} ")
    ),
    "the tax policy version foreign key not RESTRICT": _create_table(
        _replaced(
            _CLAUSES,
            "REFERENCES tax_policy_versions (id) ON DELETE RESTRICT",
            "REFERENCES tax_policy_versions (id) ON DELETE CASCADE",
        )
    ),
    "tax_policy_version_id nullable": _create_table(
        _replaced(
            _CLAUSES, "tax_policy_version_id BIGINT NOT NULL", "tax_policy_version_id BIGINT NULL"
        )
    ),
    "without the generated_by foreign key": _create_table(
        _without(_CLAUSES, f"CONSTRAINT {_FK_GENERATED_BY} ")
    ),
    "the tenant and period unique a plain index": _create_table(
        _replaced(
            _CLAUSES,
            f"CONSTRAINT {_TENANT_PERIOD_UNIQUE} UNIQUE (tenant_id, period_start)",
            f"INDEX {_TENANT_PERIOD_UNIQUE} (tenant_id, period_start)",
        )
    ),
    "without the finalized_at check": _create_table(
        _without(_CLAUSES, "CONSTRAINT ck_monthly_statements_finalized_at ")
    ),
    "snapshot_json as text": _create_table(
        _replaced(_CLAUSES, "snapshot_json JSON NOT NULL", "snapshot_json TEXT NOT NULL")
    ),
    "with an extra column": _create_table(_with_extra_column(_CLAUSES)),
}


# ---------------------------------------------------------------------------
# No database
# ---------------------------------------------------------------------------


def test_0035_follows_0034() -> None:
    migration = _load()

    assert migration.revision == _REVISION
    assert migration.down_revision == _DOWN_REVISION
    # `alembic_version.version_num` is VARCHAR(32).
    assert len(migration.revision) <= 32


def test_0035_shape_is_the_design_and_the_model() -> None:
    """The migration's frozen literals, the model's declarations and design §2 agree: columns
    (type, width, nullability, in this order), the three unique and three plain indexes in this
    column order, the three foreign keys RESTRICT, the four checks by name."""
    migration = _load()
    model = _model_shape()

    assert MonthlyStatement.__tablename__ == _TABLE
    assert _migration_shape(migration) == _design_shape() == model
    assert list(migration._COLUMNS) == [c.name for c in MonthlyStatement.__table__.columns]
    # Money aside there is no DECIMAL column (and there is no money column at all).
    assert not [kind for kind, _ in migration._COLUMNS.values() if kind.startswith("DECIMAL")]


def test_0035_table_args_order_is_the_migrations() -> None:
    """`__table_args__` declares the unique constraints, indexes, foreign keys and checks under
    the migration's names and in its order."""
    migration = _load()
    kinds = UniqueConstraint | sqlalchemy.Index | ForeignKeyConstraint | CheckConstraint
    declared = [str(arg.name) for arg in MonthlyStatement.__table_args__ if isinstance(arg, kinds)]

    assert declared == [*migration._INDEXES, *migration._FOREIGN_KEYS, *migration._CHECKS]


def test_0035_checks_are_the_ones_the_model_declares() -> None:
    """Same conditions in the migration, on the model and in the design (whitespace aside); the
    status check allows exactly the enum's three values."""
    migration = _load()
    model = {
        str(constraint.name): _normalised(str(constraint.sqltext))
        for constraint in MonthlyStatement.__table__.constraints
        if isinstance(constraint, CheckConstraint)
    }

    assert {name: _normalised(c) for name, c in migration._CHECKS.items()} == model
    assert {name: _normalised(c) for name, c in _DESIGN_CHECK_TEXTS.items()} == model
    assert [member.value for member in StatementStatus] == ["DRAFT", "FINALIZED", "VOID"]
    allowed = ", ".join(f"'{member.value}'" for member in StatementStatus)
    assert _normalised(STATUS_CHECK) == _normalised(f"status IN ({allowed})")


def test_0035_status_column_is_a_non_native_enum_of_width_16() -> None:
    status = MonthlyStatement.__table__.c.status.type
    assert isinstance(status, sqlalchemy.Enum)
    assert status.native_enum is False
    assert status.length == 16


def test_0035_model_tells_not_to_update_or_delete() -> None:
    doc = MonthlyStatement.__doc__ or ""
    assert "UPDATE" in doc and "DELETE" in doc
    assert "触发器" in doc


def test_0035_mysql_create_table_is_the_design_one_statement() -> None:
    """The `CREATE TABLE` the MySQL upgrade sends is the design's clauses, checks included."""
    migration = _load()

    assert _compact(migration._CREATE_TABLE_MYSQL) == _compact(_DDL)
    assert migration._CREATE_TABLE_MYSQL.count(";") == 0


def test_0035_triggers_are_the_design_ones() -> None:
    """Both triggers: BEFORE UPDATE / BEFORE DELETE on monthly_statements, the body the task
    contract writes (BEGIN … END around it)."""
    migration = _load()

    assert _normalised(migration._TRIGGER_BODY) == _normalised(_DESIGN_TRIGGER_BODY)
    assert migration._TRIGGER_BODY == migration._TRIGGER_BODY.strip()
    assert list(migration._TRIGGERS) == [_UPDATE_TRIGGER, _DELETE_TRIGGER]
    for name, (table, timing, kind) in _DESIGN_TRIGGERS.items():
        assert _compact(migration._TRIGGERS[name]) == _compact(
            f"CREATE TRIGGER {name} {timing} {kind} ON {table} FOR EACH ROW {_DESIGN_TRIGGER_BODY}"
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


class _FakeBind:
    """Answers what the migration asks: delete rules, the triggers, the prerequisite variables
    and "any row?"."""

    def __init__(
        self,
        delete_rules: dict[str, str],
        triggers: dict[str, tuple[str, str, str, str]],
        *,
        not_empty: bool = False,
        log_bin: int = 1,
        trusted: int = 1,
    ) -> None:
        self._delete_rules = delete_rules
        self._triggers = triggers
        self._not_empty = not_empty
        self._variables = (log_bin, trusted)
        self.queries: list[str] = []

    def execute(self, statement: object) -> _Result:
        sql = str(statement)
        self.queries.append(sql)
        if "REFERENTIAL_CONSTRAINTS" in sql:
            return _Result(list(self._delete_rules.items()))
        if "information_schema.TRIGGERS" in sql:
            return _Result([(name, *row) for name, row in self._triggers.items()])
        if "@@log_bin" in sql:
            return _Result([self._variables])
        if sql == f"SELECT 1 FROM {_TABLE} LIMIT 1":
            return _Result([(1,)] if self._not_empty else [])
        raise AssertionError(sql)


class _FakeInspector:
    """`Inspector` over the reflected table, when it exists."""

    def __init__(self, table: dict | None) -> None:
        self._table = table
        self.asked: list[str] = []

    def _get(self, key: str, name: str) -> list[dict]:
        assert name == _TABLE and self._table is not None, name
        self.asked.append(key)
        return copy.deepcopy(self._table[key])

    def has_table(self, name: str) -> bool:
        assert name == _TABLE, name
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


_MYSQL_TYPES = {
    "BIGINT": mysql.BIGINT(),
    "CHAR(36)": mysql.CHAR(length=36),
    "VARCHAR(16)": mysql.VARCHAR(length=16),
    "VARCHAR(32)": mysql.VARCHAR(length=32),
    "DATETIME": mysql.DATETIME(),
    "JSON": mysql.JSON(),
}


def _design_table() -> dict:
    """The table the way MySQL's inspector reflects it: unique constraints are also indexes."""
    return {
        "columns": [
            {"name": name, "type": _MYSQL_TYPES[kind], "nullable": nullable}
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
                "name": name,
                "constrained_columns": list(columns),
                "referred_schema": None,
                "referred_table": referred,
                "referred_columns": list(referred_columns),
                "options": {"ondelete": rule},
            }
            for name, (columns, referred, referred_columns, rule) in _DESIGN_FOREIGN_KEYS.items()
        ],
        "check_constraints": [{"name": name, "sqltext": "…"} for name in _DESIGN_CHECKS],
        "delete_rules": {name: key[3] for name, key in _DESIGN_FOREIGN_KEYS.items()},
    }


def _design_triggers(migration: ModuleType) -> dict[str, tuple[str, str, str, str]]:
    """As information_schema.TRIGGERS keeps them: the body with MySQL's own blanks around it."""
    return {
        name: (table, timing, kind, f"\n{migration._TRIGGER_BODY}\n")
        for name, (table, timing, kind) in _DESIGN_TRIGGERS.items()
    }


def _with(
    monkeypatch,
    table: dict | None,
    triggers: dict[str, tuple[str, str, str, str]] | None = None,
    *,
    dialect: str = "mysql",
    as_sql: bool = False,
    not_empty: bool = False,
    trusted: int = 1,
) -> tuple[ModuleType, _FakeOp, _FakeInspector, _FakeBind]:
    migration = _load()
    rules = dict(table["delete_rules"]) if table is not None else {}
    bind = _FakeBind(rules, triggers or {}, not_empty=not_empty, trusted=trusted)
    fake = _FakeOp(bind, dialect=dialect, as_sql=as_sql)
    inspector = _FakeInspector(table)
    monkeypatch.setattr(migration, "op", fake)
    monkeypatch.setattr(migration, "sa", _Sa(inspector))
    return migration, fake, inspector, bind


def _executed(fake: _FakeOp) -> list[str]:
    """What the MySQL upgrade sends, each as `CREATE TABLE <name>` / `CREATE TRIGGER <name>`."""
    heads = []
    for kind, (statement, *_) in fake.calls:
        assert kind == "execute"
        match = re.match(r"(CREATE (?:TABLE|TRIGGER) \w+)", _normalised(str(statement)))
        assert match is not None, statement
        heads.append(match.group(1).lower())
    return heads


def _prerequisite_asked(bind: _FakeBind) -> bool:
    return any("@@log_bin" in query for query in bind.queries)


def test_0035_upgrade_creates_the_table_then_both_triggers(monkeypatch) -> None:
    migration, fake, _, bind = _with(monkeypatch, None)
    migration.upgrade()

    assert _executed(fake) == [
        f"create table {_TABLE}",
        f"create trigger {_UPDATE_TRIGGER}",
        f"create trigger {_DELETE_TRIGGER}",
    ]
    assert fake.calls[0][1][0] == migration._CREATE_TABLE_MYSQL
    # The prerequisite check comes before the first DDL.
    assert _prerequisite_asked(bind)


def test_0035_upgrade_refuses_before_any_ddl_without_the_trigger_privilege(monkeypatch) -> None:
    """binlog on and log_bin_trust_function_creators off: the account could not create the
    triggers (ERROR 1419) — refuse before CREATE TABLE."""
    migration, fake, _, _ = _with(monkeypatch, None, trusted=0)

    with pytest.raises(RuntimeError, match="log_bin_trust_function_creators"):
        migration.upgrade()

    assert fake.calls == []


def test_0035_upgrade_with_everything_built_issues_no_ddl(monkeypatch) -> None:
    migration = _load()
    migration, fake, inspector, bind = _with(
        monkeypatch, _design_table(), _design_triggers(migration)
    )
    migration.upgrade()

    assert fake.calls == []
    for key in ("columns", "indexes", "foreign_keys", "check_constraints"):
        assert key in inspector.asked
    # Nothing to build: the prerequisite is not asked either.
    assert not _prerequisite_asked(bind)


def test_0035_upgrade_on_sqlite_with_the_design_shape_issues_no_ddl(monkeypatch) -> None:
    migration, fake, _, bind = _with(monkeypatch, _design_table(), dialect="sqlite")
    migration.upgrade()

    assert fake.calls == []
    assert bind.queries == []


@pytest.mark.parametrize("missing", [_UPDATE_TRIGGER, _DELETE_TRIGGER, "both"])
def test_0035_upgrade_builds_only_the_missing_trigger(monkeypatch, missing: str) -> None:
    """Design §2: the table built, a trigger not (the interrupted upgrade) — only that one."""
    migration = _load()
    triggers = _design_triggers(migration)
    gone = list(triggers) if missing == "both" else [missing]
    for name in gone:
        del triggers[name]
    migration, fake, _, bind = _with(monkeypatch, _design_table(), triggers)
    migration.upgrade()

    assert _executed(fake) == [f"create trigger {name}" for name in gone]
    assert [statement for _, (statement, *_) in fake.calls] == [
        migration._TRIGGERS[name] for name in gone
    ]
    assert _prerequisite_asked(bind)


def _trigger_changed(name: str, position: int, value: str):
    def mutate(triggers: dict[str, tuple[str, str, str, str]]) -> None:
        row = list(triggers[name])
        row[position] = value
        triggers[name] = (row[0], row[1], row[2], row[3])

    return mutate


# Each trigger that differs: the refusal names it.
_WRONG_TRIGGERS = {
    "update body allows VOID": (
        _UPDATE_TRIGGER,
        _trigger_changed(
            _UPDATE_TRIGGER,
            3,
            _DESIGN_TRIGGER_BODY.replace("OLD.status <> 'DRAFT'", "OLD.status = 'FINALIZED'"),
        ),
    ),
    "update trigger AFTER": (_UPDATE_TRIGGER, _trigger_changed(_UPDATE_TRIGGER, 1, "AFTER")),
    "delete trigger on UPDATE": (_DELETE_TRIGGER, _trigger_changed(_DELETE_TRIGGER, 2, "UPDATE")),
    "delete trigger on another table": (
        _DELETE_TRIGGER,
        _trigger_changed(_DELETE_TRIGGER, 0, "notifications"),
    ),
}


@pytest.mark.parametrize("case", list(_WRONG_TRIGGERS))
@pytest.mark.parametrize("other_present", [True, False], ids=["other present", "other missing"])
def test_0035_upgrade_refuses_a_trigger_that_differs_before_any_ddl(
    monkeypatch, case: str, other_present: bool
) -> None:
    """Design §2: a trigger there but with another timing, event, table or body — refuse, no
    DDL (the missing other one is not built either)."""
    migration = _load()
    name, mutate = _WRONG_TRIGGERS[case]
    triggers = _design_triggers(migration)
    mutate(triggers)
    if not other_present:
        del triggers[_DELETE_TRIGGER if name == _UPDATE_TRIGGER else _UPDATE_TRIGGER]
    migration, fake, _, bind = _with(monkeypatch, _design_table(), triggers)

    with pytest.raises(RuntimeError) as raised:
        migration.upgrade()

    assert fake.calls == []
    assert not _prerequisite_asked(bind)
    message = str(raised.value)
    assert f"trigger {name} exists but is not the one 0035 builds" in message
    assert "No DDL was issued" in message


def test_0035_upgrade_refuses_a_trigger_of_ours_on_another_table_without_the_table(
    monkeypatch,
) -> None:
    """No table yet, but one of the names is taken on another table: CREATE TRIGGER would fail
    after CREATE TABLE — refuse before both."""
    migration = _load()
    triggers = {_UPDATE_TRIGGER: ("notifications", "BEFORE", "UPDATE", migration._TRIGGER_BODY)}
    migration, fake, _, _ = _with(monkeypatch, None, triggers)

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


def _reverse_period_index(table: dict) -> None:
    for index in table["indexes"]:
        if index["name"] == "ix_monthly_statements_period":
            index["column_names"] = ["tenant_id", "period_start"]


def _prefix_length(table: dict) -> None:
    for index in table["indexes"]:
        if index["name"] == "uq_monthly_statements_statement_number":
            index["dialect_options"] = {"mysql_length": {"statement_number": 8}}


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


_WRONG_SHAPES = {
    "without the tax policy version index": _drop_tax_policy_index,
    "without the tenant and period unique": _drop_index(_TENANT_PERIOD_UNIQUE),
    "the period index in another order": _reverse_period_index,
    "a prefix length": _prefix_length,
    "the tax policy version foreign key not RESTRICT": _cascade(_FK_TAX_POLICY),
    "the tenant foreign key not RESTRICT": _cascade(_FK_TENANT),
    "without the generated_by foreign key": _no_foreign_key(_FK_GENERATED_BY),
    "without the tax policy version foreign key": _no_foreign_key(_FK_TAX_POLICY),
    "without the period check": _drop_check("ck_monthly_statements_period"),
    "with an extra column": _extra_column,
    "tax_policy_version_id nullable": _column("tax_policy_version_id", nullable=True),
    "status wider": _column("status", type=mysql.VARCHAR(32)),
    "snapshot_json as text": _column("snapshot_json", type=mysql.TEXT()),
    "ledger_high_sequence a decimal": _column(
        "ledger_high_sequence", type=mysql.DECIMAL(precision=20, scale=0)
    ),
}


@pytest.mark.parametrize("shape", list(_WRONG_SHAPES))
@pytest.mark.parametrize("dialect", ["mysql", "sqlite"])
def test_0035_upgrade_refuses_a_wrong_shape_before_any_ddl(
    monkeypatch, shape: str, dialect: str
) -> None:
    """Design §2: the table there, any item missing, extra or different — nothing is built (not
    even a missing trigger), the message names the table and says what to do."""
    migration = _load()
    table = _design_table()
    _WRONG_SHAPES[shape](table)
    migration, fake, _, bind = _with(monkeypatch, table, {}, dialect=dialect)

    with pytest.raises(RuntimeError) as raised:
        migration.upgrade()

    assert fake.calls == []
    assert not _prerequisite_asked(bind)
    message = str(raised.value)
    assert f"table {_TABLE} exists but differs" in message
    assert "No DDL was issued" in message
    assert f"DROP TABLE {_TABLE}" in message
    assert "data incident" in message


def test_0035_upgrade_on_sqlite_creates_the_table_and_its_indexes_without_triggers(
    monkeypatch,
) -> None:
    migration, fake, _, bind = _with(monkeypatch, None, dialect="sqlite")
    migration.upgrade()

    assert fake.calls == [
        ("create_table", (_TABLE,)),
        (
            "create_index",
            ("ix_monthly_statements_period", _TABLE, ["period_start", "tenant_id"], False),
        ),
        ("create_index", (_GENERATED_BY_INDEX, _TABLE, ["generated_by"], False)),
        ("create_index", (_TAX_POLICY_INDEX, _TABLE, ["tax_policy_version_id"], False)),
    ]
    assert bind.queries == []


def test_0035_offline_upgrade_emits_the_table_and_both_triggers_without_reading(
    monkeypatch,
) -> None:
    migration = _load()
    migration, fake, inspector, bind = _with(
        monkeypatch, _design_table(), _design_triggers(migration), as_sql=True
    )
    migration.upgrade()

    assert inspector.asked == []
    assert bind.queries == []
    assert _executed(fake) == [
        f"create table {_TABLE}",
        f"create trigger {_UPDATE_TRIGGER}",
        f"create trigger {_DELETE_TRIGGER}",
    ]


def test_0035_offline_downgrade_refuses_before_reading_or_ddl(monkeypatch) -> None:
    """Design §2: offline the emptiness cannot be checked — refuse, no DROP."""
    migration = _load()
    migration, fake, inspector, bind = _with(
        monkeypatch, _design_table(), _design_triggers(migration), as_sql=True
    )

    with pytest.raises(RuntimeError) as raised:
        migration.downgrade()

    assert fake.calls == []
    assert inspector.asked == []
    assert bind.queries == []
    message = str(raised.value)
    assert _TABLE in message
    assert "online" in message


def test_0035_downgrade_drops_both_triggers_then_the_empty_table(monkeypatch) -> None:
    migration = _load()
    migration, fake, _, bind = _with(monkeypatch, _design_table(), _design_triggers(migration))
    migration.downgrade()

    assert f"SELECT 1 FROM {_TABLE} LIMIT 1" in bind.queries
    assert fake.calls == [
        ("execute", (f"DROP TRIGGER {_UPDATE_TRIGGER}",)),
        ("execute", (f"DROP TRIGGER {_DELETE_TRIGGER}",)),
        ("drop_table", (_TABLE,)),
    ]


def test_0035_downgrade_drops_only_the_triggers_that_exist(monkeypatch) -> None:
    """After an upgrade interrupted between the two CREATE TRIGGER: only the one there."""
    migration = _load()
    triggers = _design_triggers(migration)
    del triggers[_DELETE_TRIGGER]
    migration, fake, _, _ = _with(monkeypatch, _design_table(), triggers)
    migration.downgrade()

    assert fake.calls == [
        ("execute", (f"DROP TRIGGER {_UPDATE_TRIGGER}",)),
        ("drop_table", (_TABLE,)),
    ]


def test_0035_downgrade_on_sqlite_drops_the_empty_table(monkeypatch) -> None:
    migration, fake, _, bind = _with(monkeypatch, _design_table(), dialect="sqlite")
    migration.downgrade()

    assert bind.queries == [f"SELECT 1 FROM {_TABLE} LIMIT 1"]
    assert fake.calls == [("drop_table", (_TABLE,))]


@pytest.mark.parametrize("dialect", ["mysql", "sqlite"])
def test_0035_downgrade_refuses_with_rows_before_any_ddl(monkeypatch, dialect: str) -> None:
    """Design §2 / §8: a row — no DDL, no row deleted; the message names the table, that it is
    not empty, and to keep it and roll back only the application image."""
    migration = _load()
    migration, fake, _, _ = _with(
        monkeypatch, _design_table(), _design_triggers(migration), dialect=dialect, not_empty=True
    )

    with pytest.raises(RuntimeError) as raised:
        migration.downgrade()

    assert fake.calls == []
    message = str(raised.value)
    assert f"table {_TABLE} is not empty" in message
    assert "No DDL was issued and no row was deleted" in message
    assert "roll back only the application image" in message


@pytest.mark.parametrize("dialect", ["mysql", "sqlite"])
def test_0035_downgrade_without_the_table_does_nothing(monkeypatch, dialect: str) -> None:
    migration, fake, inspector, bind = _with(monkeypatch, None, dialect=dialect)
    migration.downgrade()

    assert inspector.asked == ["has_table"]
    assert bind.queries == []
    assert fake.calls == []


# ---------------------------------------------------------------------------
# SQLite (the model's metadata)
# ---------------------------------------------------------------------------


@contextmanager
def _sqlite() -> Iterator[Engine]:
    """The unit tests build SQLite with `create_all` from the models, not with the migrations.
    SQLite does not enforce foreign keys by default: the parent tables are not needed."""
    engine = create_engine("sqlite+pysqlite:///:memory:")
    try:
        Base.metadata.create_all(engine, tables=[MonthlyStatement.__table__])
        yield engine
    finally:
        engine.dispose()


def _statement_params(overrides: dict[str, object], tenant_id: int = 1) -> dict[str, object]:
    """A legal FINALIZED statement for 2026-12, with `overrides` on top."""
    params: dict[str, object] = {
        "public_id": str(uuid.uuid4()),
        "tenant_id": tenant_id,
        "statement_number": f"ST-202612-{uuid.uuid4().hex[:8].upper()}",
        "period_start": _PERIOD_START,
        "period_end": _PERIOD_END,
        "cutoff_at": _CUTOFF_AT,
        "status": "FINALIZED",
        "high": 12,
        "snapshot": '{"schema_version": 1}',
        "generated_by": None,
        "tax_policy_version_id": 1,
        "finalized_at": _CREATED_AT,
        "created_at": _CREATED_AT,
    }
    params.update(overrides)
    return params


def _insert_statement(
    connection: sqlalchemy.Connection, overrides: dict[str, object], tenant_id: int = 1
) -> int:
    params = _statement_params(overrides, tenant_id)
    connection.execute(_INSERT_STATEMENT, params)
    return int(connection.execute(_ID_OF, {"public_id": params["public_id"]}).scalar_one())


def test_0035_sqlite_table_gets_the_indexes_and_the_foreign_keys() -> None:
    with _sqlite() as engine:
        inspector = inspect(engine)
        indexes = {
            str(index["name"]): (tuple(index["column_names"]), bool(index["unique"]))
            for index in inspector.get_indexes(_TABLE)
        }
        for unique in inspector.get_unique_constraints(_TABLE):
            indexes[str(unique["name"])] = (tuple(unique["column_names"]), True)
        keys = {
            str(key["name"]): (
                tuple(key["constrained_columns"]),
                str(key["referred_table"]),
                tuple(key["referred_columns"]),
                str(key["options"].get("ondelete")).upper(),
            )
            for key in inspector.get_foreign_keys(_TABLE)
        }

    # The checks are judged by what they refuse (below), not by SQLite's reflection of them.
    assert indexes == _DESIGN_INDEXES
    assert keys == _DESIGN_FOREIGN_KEYS


def test_0035_sqlite_takes_the_legal_rows() -> None:
    with _sqlite() as engine:
        with engine.begin() as connection:
            for tenant_id, overrides in enumerate(_LEGAL_ROWS.values(), start=1):
                _insert_statement(connection, overrides, tenant_id)
            assert int(connection.execute(_COUNT).scalar_one()) == len(_LEGAL_ROWS)


@pytest.mark.parametrize("row", list(_ILLEGAL_ROWS))
def test_0035_sqlite_checks_refuse_the_illegal_rows(row: str) -> None:
    overrides, _ = _ILLEGAL_ROWS[row]
    with _sqlite() as engine:
        with pytest.raises(DBAPIError) as raised:
            with engine.begin() as connection:
                _insert_statement(connection, overrides)
        assert "CHECK constraint failed" in str(raised.value), row

        with engine.connect() as connection:
            assert int(connection.execute(_COUNT).scalar_one()) == 0


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


def _heads(statements: list[str]) -> list[str]:
    """`CREATE TABLE MONTHLY_STATEMENTS`, `CREATE TRIGGER TRG_…`, `DROP TRIGGER TRG_…`, …"""
    return [" ".join(_normalised(s).split(" ")[:3]) for s in statements]


def _version(engine: Engine) -> str:
    with engine.connect() as connection:
        return str(connection.execute(_ALEMBIC_VERSION).scalar_one())


def _execute(engine: Engine, statement: str, params: dict[str, object] | None = None) -> None:
    with engine.begin() as connection:
        connection.execute(text(statement), params or {})


def _present(engine: Engine) -> bool:
    return inspect(engine).has_table(_TABLE)


def _truncate(engine: Engine) -> None:
    """The triggers refuse DELETE of a FINALIZED or VOID row: TRUNCATE (DDL, no triggers) with
    foreign key checks off. The triggers are never dropped for this."""
    with engine.begin() as connection:
        connection.exec_driver_sql("SET FOREIGN_KEY_CHECKS = 0")
        try:
            connection.exec_driver_sql(f"TRUNCATE TABLE {_TABLE}")
        finally:
            connection.exec_driver_sql("SET FOREIGN_KEY_CHECKS = 1")


def _table_shape(engine: Engine) -> dict[str, object]:
    """The table as reflected, in the migration's terms; delete rules from information_schema;
    prefix lengths kept so they would show."""
    inspector = inspect(engine)
    with engine.connect() as connection:
        rules = dict(
            connection.execute(
                text(
                    "SELECT CONSTRAINT_NAME, DELETE_RULE"
                    " FROM information_schema.REFERENTIAL_CONSTRAINTS"
                    " WHERE CONSTRAINT_SCHEMA = DATABASE() AND TABLE_NAME = :table"
                ),
                {"table": _TABLE},
            ).all()
        )
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
    """Reflected = the model's = the design's; both triggers on the table, the design's timing,
    event and body (word for word, blanks at either end aside)."""
    migration = _load()
    assert _table_shape(engine) == _model_shape() == _design_shape()
    assert _our_triggers(engine) == {
        name: (*row, migration._TRIGGER_BODY) for name, row in _DESIGN_TRIGGERS.items()
    }
    assert _normalised(migration._TRIGGER_BODY) == _normalised(_DESIGN_TRIGGER_BODY)


def _ours(engine: Engine) -> dict[str, object]:
    """The table's shape, rows and triggers, when it exists."""
    if not _present(engine):
        return {"triggers": _our_triggers(engine)}
    with engine.connect() as connection:
        rows = [
            tuple(row) for row in connection.execute(text(f"SELECT * FROM {_TABLE} ORDER BY id"))
        ]
    return {"shape": _table_shape(engine), "rows": rows, "triggers": _our_triggers(engine)}


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
        "tables": {name: shape for name, shape in tables.items() if name != _TABLE},
        "triggers": {trigger for trigger in triggers if trigger[1] != _TABLE},
        "checks": {check for check in checks if check[0] != _TABLE},
    }


def _ledger_and_wallets(engine: Engine) -> tuple[list[tuple[object, ...]], ...]:
    with engine.connect() as connection:
        wallets = [tuple(row) for row in connection.execute(_WALLET_ROWS).all()]
        ledger = [tuple(row) for row in connection.execute(_LEDGER_ROWS).all()]
    return wallets, ledger


def _at_0034(alembic_config: Config, engine: Engine) -> None:
    command.upgrade(alembic_config, "head")
    command.downgrade(alembic_config, _DOWN_REVISION)
    assert _version(engine) == _DOWN_REVISION
    assert not _present(engine)
    assert _our_triggers(engine) == {}


def _back_to_head(alembic_config: Config, engine: Engine) -> None:
    """Empties the table if it is there, drops a hand-made or half-built one left at 0034 (its
    triggers go with it), and upgrades to head again."""
    present = _present(engine)
    if present:
        _truncate(engine)
    if _version(engine) == _DOWN_REVISION and present:
        _execute(engine, f"DROP TABLE {_TABLE}")
    command.upgrade(alembic_config, "head")


def _insert_committed(engine: Engine, rows: list[tuple[dict[str, object], int]]) -> list[int]:
    """Statements written with foreign key checks off (no tenant, user or tax policy version is
    needed for this); returns their ids."""
    ids = []
    with engine.begin() as connection:
        connection.exec_driver_sql("SET FOREIGN_KEY_CHECKS = 0")
        try:
            for overrides, tenant_id in rows:
                ids.append(_insert_statement(connection, overrides, tenant_id))
        finally:
            connection.exec_driver_sql("SET FOREIGN_KEY_CHECKS = 1")
    return ids


@needs_mysql
def test_0035_round_trip_on_an_empty_table(alembic_config: Config, engine: Engine) -> None:
    """Design §7 "迁移": 0034 → 0035 → 0034 → 0035 with the table empty. The upgrade sends one
    `CREATE TABLE`, then the two `CREATE TRIGGER`; the shape is the model's and the triggers the
    design's; the downgrade drops both triggers, then the table; every other table, key, check
    and trigger and the wallet and ledger rows are the same throughout."""
    _at_0034(alembic_config, engine)
    try:
        at_0034 = _schema_snapshot(engine)
        rows = _ledger_and_wallets(engine)

        with _ddl_statements() as statements:
            command.upgrade(alembic_config, _REVISION)
        assert _heads(statements) == [
            f"CREATE TABLE {_TABLE.upper()}",
            f"CREATE TRIGGER {_UPDATE_TRIGGER.upper()}",
            f"CREATE TRIGGER {_DELETE_TRIGGER.upper()}",
        ]
        assert _version(engine) == _REVISION
        _assert_design_shape(engine)
        at_0035 = _schema_snapshot(engine)
        assert _without_ours(at_0035) == at_0034
        assert _ledger_and_wallets(engine) == rows

        with _ddl_statements() as statements:
            command.downgrade(alembic_config, _DOWN_REVISION)
        assert [_normalised(s) for s in statements] == [
            f"DROP TRIGGER {_UPDATE_TRIGGER.upper()}",
            f"DROP TRIGGER {_DELETE_TRIGGER.upper()}",
            f"DROP TABLE {_TABLE.upper()}",
        ]
        assert _version(engine) == _DOWN_REVISION
        assert not _present(engine)
        assert _schema_snapshot(engine) == at_0034
        assert _ledger_and_wallets(engine) == rows

        command.upgrade(alembic_config, _REVISION)
        assert _schema_snapshot(engine) == at_0035
        _assert_design_shape(engine)
        assert _ledger_and_wallets(engine) == rows
    finally:
        _back_to_head(alembic_config, engine)


@needs_mysql
def test_0035_rerun_with_everything_built_only_moves_the_version(
    alembic_config: Config, engine: Engine
) -> None:
    """Upgraded, then `alembic_version` put back to 0034 (as if writing the version failed after
    the table and both triggers were built) — no DDL, the version moves to 0035, the schema is
    as after the first upgrade."""
    _at_0034(alembic_config, engine)
    try:
        command.upgrade(alembic_config, _REVISION)
        after_first = _schema_snapshot(engine)
        _execute(engine, _SET_VERSION, {"version": _DOWN_REVISION})

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
def test_0035_rerun_with_a_wrong_shape_fails_before_any_ddl(
    alembic_config: Config, engine: Engine, shape: str
) -> None:
    """Design §7 "迁移" (形状校验): version 0034 and a fresh hand-made table — the upgrade fails
    naming the table, issues no DDL (no trigger either), the version stays 0034 and the table is
    as it was."""
    _at_0034(alembic_config, engine)
    try:
        _execute(engine, _WRONG_SHAPE_DDL[shape])
        hand_made = _ours(engine)

        with _ddl_statements() as statements:
            with pytest.raises(RuntimeError) as raised:
                command.upgrade(alembic_config, _REVISION)

        assert statements == []
        message = str(raised.value)
        assert f"table {_TABLE} exists but differs" in message
        assert "No DDL was issued" in message
        assert "data incident" in message
        assert _version(engine) == _DOWN_REVISION
        assert _ours(engine) == hand_made
    finally:
        _back_to_head(alembic_config, engine)


@needs_mysql
@pytest.mark.parametrize("missing", [_UPDATE_TRIGGER, _DELETE_TRIGGER])
def test_0035_upgrade_builds_only_the_missing_trigger_on_mysql(
    alembic_config: Config, engine: Engine, missing: str
) -> None:
    """Design §2 "补完中断的升级": version 0034, the table built and one trigger dropped — the
    upgrade sends only that one `CREATE TRIGGER`; the version is 0035; all is the design's."""
    migration = _load()
    _at_0034(alembic_config, engine)
    try:
        command.upgrade(alembic_config, _REVISION)
        _execute(engine, f"DROP TRIGGER {missing}")
        _execute(engine, _SET_VERSION, {"version": _DOWN_REVISION})

        with _ddl_statements() as statements:
            command.upgrade(alembic_config, _REVISION)

        assert [_compact(s) for s in statements] == [_compact(migration._TRIGGERS[missing])]
        assert _version(engine) == _REVISION
        _assert_design_shape(engine)
    finally:
        _back_to_head(alembic_config, engine)


def _wrong_trigger(name: str, change: str) -> str:
    migration = _load()
    statement = migration._TRIGGERS[name]
    if change == "body":
        return statement.replace("OLD.status <> 'DRAFT'", "OLD.status = 'FINALIZED'")
    return statement.replace("\nBEFORE ", "\nAFTER ")


@needs_mysql
@pytest.mark.parametrize("name", [_UPDATE_TRIGGER, _DELETE_TRIGGER])
@pytest.mark.parametrize("change", ["body", "timing"])
def test_0035_upgrade_refuses_a_trigger_that_differs_on_mysql(
    alembic_config: Config, engine: Engine, name: str, change: str
) -> None:
    """Design §2: version 0034, the table built and a trigger replaced by hand with another body
    (FINALIZED only, so VOID rows could change) or AFTER instead of BEFORE — the upgrade fails
    naming the trigger, issues no DDL, the version stays 0034 and the table and triggers are as
    they were."""
    _at_0034(alembic_config, engine)
    try:
        command.upgrade(alembic_config, _REVISION)
        _execute(engine, f"DROP TRIGGER {name}")
        _execute(engine, _wrong_trigger(name, change))
        _execute(engine, _SET_VERSION, {"version": _DOWN_REVISION})
        before = _ours(engine)

        with _ddl_statements() as statements:
            with pytest.raises(RuntimeError) as raised:
                command.upgrade(alembic_config, _REVISION)

        assert statements == []
        message = str(raised.value)
        assert f"trigger {name} exists but is not the one 0035 builds" in message
        assert "No DDL was issued" in message
        assert _version(engine) == _DOWN_REVISION
        assert _ours(engine) == before
    finally:
        _back_to_head(alembic_config, engine)


@needs_mysql
def test_0035_downgrade_with_a_row_refuses_before_any_ddl(
    alembic_config: Config, engine: Engine
) -> None:
    """Design §2 / §8: a FINALIZED statement — the downgrade fails naming the table, no DDL, the
    table, its row and its triggers are as they were, `alembic_version` stays 0035. Taken at
    0035: the later migrations' downgrades (0037 drops its columns, 0036 its empty tables) are
    not the part under observation."""
    command.upgrade(alembic_config, "head")
    command.downgrade(alembic_config, _REVISION)
    assert _version(engine) == _REVISION
    try:
        _insert_committed(engine, [({}, 1)])
        before_schema = _schema_snapshot(engine)
        before = _ours(engine)

        with _ddl_statements() as statements:
            with pytest.raises(RuntimeError) as raised:
                command.downgrade(alembic_config, _DOWN_REVISION)

        assert statements == []
        message = str(raised.value)
        assert f"table {_TABLE} is not empty" in message
        assert "roll back only the application image" in message
        assert _version(engine) == _REVISION
        assert _schema_snapshot(engine) == before_schema
        assert _ours(engine) == before
    finally:
        _back_to_head(alembic_config, engine)


@needs_mysql
def test_0035_offline_downgrade_refuses_without_drop(alembic_config: Config) -> None:
    """Design §2: `--sql` cannot tell whether the table is empty — the downgrade fails saying it
    must run online, and its output has no DROP. The offline upgrade, as 0030's, emits the table
    and both triggers without reading the database."""
    upgrade_buffer = io.StringIO()
    command.upgrade(
        Config("alembic.ini", output_buffer=upgrade_buffer),
        f"{_DOWN_REVISION}:{_REVISION}",
        sql=True,
    )
    emitted = _normalised(upgrade_buffer.getvalue())
    assert f"CREATE TABLE {_TABLE.upper()} (" in emitted
    assert f"CREATE TRIGGER {_UPDATE_TRIGGER.upper()} BEFORE UPDATE" in emitted
    assert f"CREATE TRIGGER {_DELETE_TRIGGER.upper()} BEFORE DELETE" in emitted
    assert "@@LOG_BIN" not in emitted

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


@needs_mysql
@pytest.mark.parametrize("row", list(_ILLEGAL_ROWS))
def test_0035_mysql_checks_refuse_the_illegal_rows(
    alembic_config: Config, engine: Engine, row: str
) -> None:
    """The four checks on MySQL: each illegal row is refused (3819, the check named); the legal
    rows go in. Everything is rolled back."""
    command.upgrade(alembic_config, "head")
    overrides, check = _ILLEGAL_ROWS[row]
    with engine.connect() as connection:
        transaction = connection.begin()
        try:
            # Session-wide: no tenant, user or tax policy version is needed for this.
            connection.exec_driver_sql("SET FOREIGN_KEY_CHECKS = 0")
            for tenant_id, legal in enumerate(_LEGAL_ROWS.values(), start=1):
                _insert_statement(connection, legal, tenant_id)
            before = int(connection.execute(_COUNT).scalar_one())

            with pytest.raises(DBAPIError) as raised:
                with connection.begin_nested():
                    _insert_statement(connection, overrides, len(_LEGAL_ROWS) + 1)
            assert int(raised.value.orig.args[0]) == _ER_CHECK_CONSTRAINT_VIOLATED, row
            assert check in str(raised.value.orig.args[1]), row

            assert int(connection.execute(_COUNT).scalar_one()) == before
        finally:
            transaction.rollback()
            connection.exec_driver_sql("SET FOREIGN_KEY_CHECKS = 1")


# Design §7 "触发器": at least status, snapshot_json and statement_number, one UPDATE each.
_UPDATES = {
    "status": "UPDATE monthly_statements SET status = 'DRAFT' WHERE id = :id",
    "snapshot_json": (
        "UPDATE monthly_statements SET snapshot_json = JSON_OBJECT('schema_version', 2)"
        " WHERE id = :id"
    ),
    "statement_number": (
        "UPDATE monthly_statements SET statement_number = 'ST-202612-ZZZZZZZZ' WHERE id = :id"
    ),
    "finalized_at": (
        "UPDATE monthly_statements SET finalized_at = '2027-01-03 00:00:00' WHERE id = :id"
    ),
}
_DELETE = "DELETE FROM monthly_statements WHERE id = :id"


def _assert_immutable(engine: Engine, statement: str, statement_id: int) -> None:
    with pytest.raises(DBAPIError) as raised:
        _execute(engine, statement, {"id": statement_id})
    # 1644 is ER_SIGNAL_EXCEPTION: an unhandled SIGNAL SQLSTATE '45000'.
    assert int(raised.value.orig.args[0]) == _ER_SIGNAL_EXCEPTION, statement
    assert _IMMUTABLE in str(raised.value.orig.args[1]), statement


@needs_mysql
@pytest.mark.parametrize("status", ["FINALIZED", "VOID"])
def test_0035_triggers_refuse_update_and_delete_of_a_finalized_or_void_row(
    alembic_config: Config, engine: Engine, status: str
) -> None:
    """Design §7 "触发器" / §4 / INV-12: every UPDATE (status, snapshot_json, statement_number,
    finalized_at) and the DELETE of a FINALIZED or VOID row fail with SQLSTATE 45000; the row is
    unchanged."""
    command.upgrade(alembic_config, "head")
    try:
        overrides: dict[str, object] = {"status": status}
        if status == "VOID":
            overrides["finalized_at"] = None
        [statement_id] = _insert_committed(engine, [(overrides, 1)])
        before = _ours(engine)

        for update in _UPDATES.values():
            _assert_immutable(engine, update, statement_id)
        _assert_immutable(engine, _DELETE, statement_id)

        assert _ours(engine) == before
    finally:
        _truncate(engine)


@needs_mysql
def test_0035_triggers_let_a_draft_row_be_changed_and_deleted(
    alembic_config: Config, engine: Engine
) -> None:
    """Design §7 "触发器": a DRAFT row can be updated (each column above) and deleted; once it is
    FINALIZED, it is not any more."""
    command.upgrade(alembic_config, "head")
    try:
        draft, finalized_later = _insert_committed(
            engine,
            [
                ({"status": "DRAFT", "finalized_at": None}, 1),
                ({"status": "DRAFT", "finalized_at": None}, 2),
            ],
        )

        _execute(
            engine,
            "UPDATE monthly_statements SET snapshot_json = JSON_OBJECT('schema_version', 2),"
            " statement_number = 'ST-202612-DRAFT001', ledger_high_sequence = 13 WHERE id = :id",
            {"id": draft},
        )
        with engine.connect() as connection:
            number, high = connection.execute(
                text(
                    "SELECT statement_number, ledger_high_sequence FROM monthly_statements"
                    " WHERE id = :id"
                ),
                {"id": draft},
            ).one()
        assert (number, int(high)) == ("ST-202612-DRAFT001", 13)
        _execute(engine, _DELETE, {"id": draft})

        # DRAFT → FINALIZED is an UPDATE of a DRAFT row: allowed; after it the row is immutable.
        _execute(
            engine,
            "UPDATE monthly_statements SET status = 'FINALIZED', finalized_at = :at WHERE id = :id",
            {"id": finalized_later, "at": _CREATED_AT},
        )
        _assert_immutable(engine, _UPDATES["status"], finalized_later)
        _assert_immutable(engine, _DELETE, finalized_later)

        with engine.connect() as connection:
            assert [
                int(i) for i in connection.execute(text(f"SELECT id FROM {_TABLE}")).scalars()
            ] == [finalized_later]
    finally:
        _truncate(engine)

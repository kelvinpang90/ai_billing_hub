"""0029_wallet_reconciliation（AIH-TASK-129，设计闸门 #474 v13）.

Design §2 "数据库" and "迁移写法", §3 INV-4 / INV-5, §5 (the migration rows), §7 (the migration
rows and the CHECK row), §8.

The first cases need no database: the revision chain; the columns, indexes, foreign keys
and checks of both tables in the migration equal the models' and the design's; the
upgrade's check (both missing / both in the design shape / only runs, empty, in the design
shape / each refused case) and the downgrade that drops only empty tables, with
`alembic.op` and the inspector stood in; `create_all` on SQLite (what the unit tests run)
builds both tables, and their checks and the unique `running_slot` refuse every illegal row
of design §7 "CHECK".

On a real MySQL (`BILLING_TEST_DATABASE_URL`; skipped when unset, never skipped in CI):
design §7's migration rows — the 0028 → 0029 → 0028 → 0029 round trip on empty tables
(every other table, key, check and trigger and the wallet and ledger rows unchanged,
INV-4 / INV-5), the rerun with both tables already in the design shape, the wrong shapes
(each with fresh hand-made tables), the interrupted upgrade, the downgrade with rows, the
downgrade with only runs left (empty and with a row), the `--sql` downgrade — and the CHECK
row. The Alembic chain does not run on SQLite (0006 onwards ask for `@@log_bin`), so every
migration scenario is MySQL's, as in test_migrations.py. Captured statements are compared
after normalising (backticks, case, spaces).
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
    ForeignKeyConstraint,
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
from app.models.tenancy import Tenant
from app.models.wallet_reconciliation import (
    CODES_CHECK,
    COUNTS_CHECK,
    FINISHED_CHECK,
    LEASE_CHECK,
    RUNNING_SLOT_CHECK,
    STATUS_CHECK,
    ReconciliationRunStatus,
    WalletReconciliationFinding,
    WalletReconciliationRun,
)

TEST_DATABASE_URL = os.environ.get("BILLING_TEST_DATABASE_URL", "")

needs_mysql = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="BILLING_TEST_DATABASE_URL is not set; migration tests need a real MySQL",
)

_REVISION = "0029_wallet_reconciliation"
_DOWN_REVISION = "0028_notifications"
_MIGRATION = pathlib.Path("alembic/versions/20261029_0029_wallet_reconciliation.py")
_RUNS = "wallet_reconciliation_runs"
_FINDINGS = "wallet_reconciliation_findings"
# Drop order: findings refer to runs.
_TABLES = (_FINDINGS, _RUNS)
_MODELS = {_RUNS: WalletReconciliationRun, _FINDINGS: WalletReconciliationFinding}

# Design §2 "数据库".
_DESIGN_COLUMNS = {
    _RUNS: {
        "id": ("BIGINT", False),
        "public_id": ("CHAR(36)", False),
        "run_date": ("DATE", False),
        "status": ("VARCHAR(16)", False),
        "started_at": ("DATETIME", False),
        "finished_at": ("DATETIME", True),
        "cursor_tenant_id": ("BIGINT", False),
        "wallets_checked": ("INTEGER", False),
        "wallets_inconsistent": ("INTEGER", False),
        "lease_owner": ("CHAR(36)", True),
        "lease_until": ("DATETIME", True),
        "running_slot": ("SMALLINT", True),
    },
    _FINDINGS: {
        "id": ("BIGINT", False),
        "run_id": ("BIGINT", False),
        "tenant_id": ("BIGINT", False),
        "problem_codes": ("VARCHAR(255)", False),
        "balance": ("DECIMAL(20,8)", True),
        "wallet_version": ("BIGINT", True),
        "ledger_rows": ("BIGINT", False),
        "checked_at": ("DATETIME", False),
    },
}
_RUNNING_SLOT_UNIQUE = "uq_wallet_reconciliation_runs_running_slot"
_FINDINGS_TENANT_INDEX = "ix_wallet_reconciliation_findings_tenant"
_DESIGN_INDEXES = {
    _RUNS: {
        "uq_wallet_reconciliation_runs_public_id": (("public_id",), True),
        _RUNNING_SLOT_UNIQUE: (("running_slot",), True),
        "ix_wallet_reconciliation_runs_run_date": (("run_date",), False),
        "ix_wallet_reconciliation_runs_status_finished": (("status", "finished_at"), False),
    },
    _FINDINGS: {
        "uq_wallet_reconciliation_findings_run_tenant": (("run_id", "tenant_id"), True),
        _FINDINGS_TENANT_INDEX: (("tenant_id",), False),
    },
}
_FK_RUN = "fk_wallet_reconciliation_findings_run"
_FK_TENANT = "fk_wallet_reconciliation_findings_tenant"
_DESIGN_FOREIGN_KEYS = {
    _RUNS: {},
    _FINDINGS: {
        _FK_RUN: (("run_id",), _RUNS, ("id",), "RESTRICT"),
        _FK_TENANT: (("tenant_id",), "tenants", ("id",), "RESTRICT"),
    },
}
_DESIGN_CHECKS = {
    _RUNS: {
        "ck_wallet_reconciliation_runs_status",
        "ck_wallet_reconciliation_runs_finished",
        "ck_wallet_reconciliation_runs_counts",
        "ck_wallet_reconciliation_runs_lease",
        "ck_wallet_reconciliation_runs_running_slot",
    },
    _FINDINGS: {"ck_wallet_reconciliation_findings_codes"},
}

# MySQL reports a violated CHECK as 3819 (ER_CHECK_CONSTRAINT_VIOLATED), a duplicate key as 1062.
_ER_CHECK_CONSTRAINT_VIOLATED = 3819
_ER_DUP_ENTRY = 1062

_NOW = dt.datetime(2026, 10, 29, 8, 30, 0)
# Literals, as the amounts: both databases take them, no driver adapter is involved.
_NOW_TEXT = _NOW.strftime("%Y-%m-%d %H:%M:%S")
_TODAY_TEXT = "2026-10-29"
_TOMORROW_TEXT = "2026-10-30"

_ALEMBIC_VERSION = text("SELECT version_num FROM alembic_version")
_SET_VERSION = "UPDATE alembic_version SET version_num = :version"
_TRIGGERS_QUERY = text(
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
_INSERT_RUN = text(
    f"INSERT INTO {_RUNS} (public_id, run_date, status, started_at, finished_at,"
    " cursor_tenant_id, wallets_checked, wallets_inconsistent, lease_owner, lease_until,"
    " running_slot) VALUES (:public_id, :run_date, :status, :started_at, :finished_at, :cursor,"
    " :checked, :inconsistent, :lease_owner, :lease_until, :running_slot)"
)
_INSERT_FINDING = text(
    f"INSERT INTO {_FINDINGS} (run_id, tenant_id, problem_codes, balance, wallet_version,"
    " ledger_rows, checked_at) VALUES (:run_id, :tenant_id, :problem_codes, :balance,"
    " :wallet_version, :ledger_rows, :checked_at)"
)
_RUN_ID = text(f"SELECT id FROM {_RUNS} WHERE public_id = :public_id")

_RUNNING = {"status": "RUNNING", "finished_at": None, "running_slot": 1}
_RUNNING_LEASED = {**_RUNNING, "lease_owner": str(uuid.uuid4()), "lease_until": _NOW_TEXT}

# Design §7 "CHECK": name → (table, overrides, legal runs inserted first, what refuses it).
_ILLEGAL_ROWS: dict[str, tuple[str, dict[str, object], list[dict[str, object]], str]] = {
    "RUNNING with finished_at": ("run", {**_RUNNING, "finished_at": _NOW_TEXT}, [], "check"),
    "COMPLETED without finished_at": ("run", {"finished_at": None}, [], "check"),
    "an unknown status": ("run", {"status": "PAUSED"}, [], "check"),
    "wallets_inconsistent above wallets_checked": (
        "run",
        {"checked": 1, "inconsistent": 2},
        [],
        "check",
    ),
    "an empty problem_codes": ("finding", {"problem_codes": ""}, [], "check"),
    "lease_owner without lease_until": (
        "run",
        {**_RUNNING, "lease_owner": str(uuid.uuid4())},
        [],
        "check",
    ),
    "lease_until without lease_owner": ("run", {**_RUNNING, "lease_until": _NOW_TEXT}, [], "check"),
    "RUNNING with running_slot NULL": ("run", {**_RUNNING, "running_slot": None}, [], "check"),
    "COMPLETED with running_slot 1": ("run", {"running_slot": 1}, [], "check"),
    # Another run_date: the database, not run_date, keeps a second RUNNING out.
    "two rows with running_slot 1": (
        "run",
        {**_RUNNING, "run_date": _TOMORROW_TEXT},
        [_RUNNING_LEASED],
        "unique",
    ),
}


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("migration_0029", _MIGRATION)
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
    """A model's columns, indexes, foreign keys and check names, shaped like the migration's."""
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
        "indexes": indexes,
        "foreign_keys": foreign_keys,
        "checks": checks,
    }


def _design_shape(table: str) -> dict[str, object]:
    return {
        "columns": _DESIGN_COLUMNS[table],
        "indexes": _DESIGN_INDEXES[table],
        "foreign_keys": _DESIGN_FOREIGN_KEYS[table],
        "checks": _DESIGN_CHECKS[table],
    }


def _model_check_texts(table: str) -> dict[str, str]:
    return {
        str(constraint.name): _normalised(str(constraint.sqltext))
        for constraint in _MODELS[table].__table__.constraints
        if isinstance(constraint, CheckConstraint)
    }


# The design's MySQL tables, clause by clause; the wrong shapes below are edits of them.
_RUNS_CLAUSES = [
    "id BIGINT NOT NULL AUTO_INCREMENT",
    "public_id CHAR(36) NOT NULL",
    "run_date DATE NOT NULL",
    "status VARCHAR(16) NOT NULL",
    "started_at DATETIME NOT NULL",
    "finished_at DATETIME NULL",
    "cursor_tenant_id BIGINT NOT NULL DEFAULT 0",
    "wallets_checked INT NOT NULL DEFAULT 0",
    "wallets_inconsistent INT NOT NULL DEFAULT 0",
    "lease_owner CHAR(36) NULL",
    "lease_until DATETIME NULL",
    "running_slot SMALLINT NULL",
    "PRIMARY KEY (id)",
    "CONSTRAINT uq_wallet_reconciliation_runs_public_id UNIQUE (public_id)",
    f"CONSTRAINT {_RUNNING_SLOT_UNIQUE} UNIQUE (running_slot)",
    "INDEX ix_wallet_reconciliation_runs_run_date (run_date)",
    "INDEX ix_wallet_reconciliation_runs_status_finished (status, finished_at)",
    f"CONSTRAINT ck_wallet_reconciliation_runs_status CHECK ({STATUS_CHECK})",
    f"CONSTRAINT ck_wallet_reconciliation_runs_finished CHECK ({FINISHED_CHECK})",
    f"CONSTRAINT ck_wallet_reconciliation_runs_counts CHECK ({COUNTS_CHECK})",
    f"CONSTRAINT ck_wallet_reconciliation_runs_lease CHECK ({LEASE_CHECK})",
    f"CONSTRAINT ck_wallet_reconciliation_runs_running_slot CHECK ({RUNNING_SLOT_CHECK})",
]
_FK_RUN_CLAUSE = (
    f"CONSTRAINT {_FK_RUN} FOREIGN KEY (run_id) REFERENCES {_RUNS} (id) ON DELETE RESTRICT"
)
_FINDINGS_CLAUSES = [
    "id BIGINT NOT NULL AUTO_INCREMENT",
    "run_id BIGINT NOT NULL",
    "tenant_id BIGINT NOT NULL",
    "problem_codes VARCHAR(255) NOT NULL",
    "balance DECIMAL(20, 8) NULL",
    "wallet_version BIGINT NULL",
    "ledger_rows BIGINT NOT NULL",
    "checked_at DATETIME NOT NULL",
    "PRIMARY KEY (id)",
    "CONSTRAINT uq_wallet_reconciliation_findings_run_tenant UNIQUE (run_id, tenant_id)",
    f"INDEX {_FINDINGS_TENANT_INDEX} (tenant_id)",
    _FK_RUN_CLAUSE,
    f"CONSTRAINT {_FK_TENANT} FOREIGN KEY (tenant_id) REFERENCES tenants (id) ON DELETE RESTRICT",
    f"CONSTRAINT ck_wallet_reconciliation_findings_codes CHECK ({CODES_CHECK})",
]


def _create_table(table: str, clauses: list[str]) -> str:
    return f"CREATE TABLE {table} ({', '.join(clauses)})"


def _without(clauses: list[str], prefix: str) -> list[str]:
    kept = [clause for clause in clauses if not clause.startswith(prefix)]
    assert len(kept) == len(clauses) - 1, prefix
    return kept


def _replaced(clauses: list[str], old: str, new: str) -> list[str]:
    assert sum(old in clause for clause in clauses) == 1, old
    return [clause.replace(old, new) for clause in clauses]


def _with_extra_column(clauses: list[str]) -> list[str]:
    at = clauses.index("running_slot SMALLINT NULL") + 1
    return [*clauses[:at], "extra_note VARCHAR(16) NULL", *clauses[at:]]


_RUNS_DDL = _create_table(_RUNS, _RUNS_CLAUSES)
_FINDINGS_DDL = _create_table(_FINDINGS, _FINDINGS_CLAUSES)

# Design §7 "迁移重跑：形状不对": each sub-scenario is a list of fresh hand-made tables (runs
# first) and whether runs gets a row.
_WRONG_SHAPE_DDL: dict[str, tuple[list[str], bool]] = {
    # Without runs there is nothing to refer to: findings without its run foreign key.
    "only findings": (
        [_create_table(_FINDINGS, _without(_FINDINGS_CLAUSES, f"CONSTRAINT {_FK_RUN} "))],
        False,
    ),
    "only runs, with a row": ([_RUNS_DDL], True),
    "runs without the running_slot unique": (
        [
            _create_table(_RUNS, _without(_RUNS_CLAUSES, f"CONSTRAINT {_RUNNING_SLOT_UNIQUE} ")),
            _FINDINGS_DDL,
        ],
        False,
    ),
    "findings without the tenant index": (
        [_RUNS_DDL, _create_table(_FINDINGS, _without(_FINDINGS_CLAUSES, "INDEX "))],
        False,
    ),
    "findings' run foreign key not RESTRICT": (
        [
            _RUNS_DDL,
            _create_table(
                _FINDINGS,
                _replaced(
                    _FINDINGS_CLAUSES,
                    f"REFERENCES {_RUNS} (id) ON DELETE RESTRICT",
                    f"REFERENCES {_RUNS} (id) ON DELETE CASCADE",
                ),
            ),
        ],
        False,
    ),
    "runs with an extra column": (
        [_create_table(_RUNS, _with_extra_column(_RUNS_CLAUSES)), _FINDINGS_DDL],
        False,
    ),
}


# ---------------------------------------------------------------------------
# No database
# ---------------------------------------------------------------------------


def test_0029_follows_0028() -> None:
    migration = _load()

    assert migration.revision == _REVISION
    assert migration.down_revision == _DOWN_REVISION
    # `alembic_version.version_num` is VARCHAR(32).
    assert len(migration.revision) <= 32


@pytest.mark.parametrize("table", [_RUNS, _FINDINGS])
def test_0029_shapes_are_the_design_and_the_models(table: str) -> None:
    """The migration's frozen literals, the models' declarations and design §2 agree: columns
    (type, width, nullability, in this order), unique and plain indexes in this column order, the
    two foreign keys RESTRICT (none on runs), the six checks by name."""
    migration = _load()
    design = migration._DESIGN[table]
    model = _model_shape(table)

    assert _MODELS[table].__tablename__ == table
    assert design["columns"] == _DESIGN_COLUMNS[table] == model["columns"]
    assert list(design["columns"]) == [c.name for c in _MODELS[table].__table__.columns]
    assert design["indexes"] == _DESIGN_INDEXES[table] == model["indexes"]
    assert design["foreign_keys"] == _DESIGN_FOREIGN_KEYS[table] == model["foreign_keys"]
    assert design["checks"] == _DESIGN_CHECKS[table] == model["checks"]


def test_0029_table_args_order_is_the_migrations() -> None:
    """`__table_args__` declares the indexes, foreign keys and checks under the migration's names
    and in its order."""
    migration = _load()
    kinds = UniqueConstraint | sqlalchemy.Index | ForeignKeyConstraint | CheckConstraint

    def declared(model: type) -> list[str]:
        return [str(arg.name) for arg in model.__table_args__ if isinstance(arg, kinds)]

    assert declared(WalletReconciliationRun) == [
        *migration._RUNS_INDEXES,
        *migration._RUNS_FOREIGN_KEYS,
        *migration._RUNS_CHECKS,
    ]
    assert declared(WalletReconciliationFinding) == [
        *migration._FINDINGS_INDEXES,
        *migration._FINDINGS_FOREIGN_KEYS,
        *migration._FINDINGS_CHECKS,
    ]


def test_0029_checks_are_the_ones_the_models_declare() -> None:
    """Same conditions in the migration and on the models (whitespace aside); the status check
    allows exactly the enum's three values; the running_slot check spells out `running_slot IS NOT
    NULL` (design §2: without it RUNNING with NULL is UNKNOWN and gets in)."""
    migration = _load()

    assert {
        name: _normalised(c) for name, c in migration._RUNS_CHECKS.items()
    } == _model_check_texts(_RUNS)
    assert {
        name: _normalised(c) for name, c in migration._FINDINGS_CHECKS.items()
    } == _model_check_texts(_FINDINGS)
    assert [member.value for member in ReconciliationRunStatus] == [
        "RUNNING",
        "COMPLETED",
        "ABANDONED",
    ]
    allowed = ", ".join(f"'{member.value}'" for member in ReconciliationRunStatus)
    assert _normalised(STATUS_CHECK) == _normalised(f"status IN ({allowed})")
    assert _normalised(RUNNING_SLOT_CHECK) == _normalised(
        "(status = 'RUNNING' AND running_slot IS NOT NULL AND running_slot = 1)"
        " OR (status <> 'RUNNING' AND running_slot IS NULL)"
    )
    assert _normalised(FINISHED_CHECK) == _normalised(
        "(status = 'RUNNING' AND finished_at IS NULL)"
        " OR (status <> 'RUNNING' AND finished_at IS NOT NULL)"
    )
    assert _normalised(LEASE_CHECK) == _normalised(
        "(lease_owner IS NULL AND lease_until IS NULL)"
        " OR (lease_owner IS NOT NULL AND lease_until IS NOT NULL)"
    )
    assert _normalised(COUNTS_CHECK) == _normalised(
        "wallets_inconsistent >= 0 AND wallets_inconsistent <= wallets_checked"
    )
    assert _normalised(CODES_CHECK) == _normalised("problem_codes <> ''")


def test_0029_model_defaults() -> None:
    """The three counters default to 0 in Python and in the database; the lease columns and
    `running_slot` are nullable without a default (design §2)."""
    columns = WalletReconciliationRun.__table__.c
    for name in ("cursor_tenant_id", "wallets_checked", "wallets_inconsistent"):
        column = columns[name]
        assert column.default is not None and column.default.arg == 0, name
        assert column.server_default is not None, name
        assert str(column.server_default.arg) == "0", name
    for name in ("lease_owner", "lease_until", "running_slot"):
        column = columns[name]
        assert column.nullable, name
        assert column.default is None and column.server_default is None, name


def test_0029_mysql_create_tables_are_the_design_one_statement_each() -> None:
    """The two `CREATE TABLE` the MySQL upgrade sends are the design's clauses, checks included."""
    migration = _load()

    assert _compact(migration._CREATE_RUNS_MYSQL) == _compact(_RUNS_DDL)
    assert _compact(migration._CREATE_FINDINGS_MYSQL) == _compact(_FINDINGS_DDL)
    assert migration._CREATE_RUNS_MYSQL.count(";") == 0
    assert migration._CREATE_FINDINGS_MYSQL.count(";") == 0


class _Result:
    def __init__(self, rows: list[tuple[object, ...]]) -> None:
        self._rows = rows

    def all(self) -> list[tuple[object, ...]]:
        return list(self._rows)

    def first(self) -> tuple[object, ...] | None:
        return self._rows[0] if self._rows else None


class _FakeBind:
    """Answers the two queries the migration sends: the delete rules and "any row?"."""

    def __init__(self, delete_rules: dict[str, dict[str, str]], not_empty: set[str]) -> None:
        self._delete_rules = delete_rules
        self._not_empty = not_empty
        self.queries: list[str] = []

    def execute(self, statement: object) -> _Result:
        sql = str(statement)
        self.queries.append(sql)
        if "REFERENTIAL_CONSTRAINTS" in sql:
            for table, rules in self._delete_rules.items():
                if f"TABLE_NAME = '{table}'" in sql:
                    return _Result(list(rules.items()))
            raise AssertionError(sql)
        for table in _TABLES:
            if sql == f"SELECT 1 FROM {table} LIMIT 1":
                return _Result([(1,)] if table in self._not_empty else [])
        raise AssertionError(sql)


class _FakeInspector:
    """`Inspector` over the reflected tables that exist."""

    def __init__(self, tables: dict[str, dict]) -> None:
        self._tables = tables
        self.asked: list[tuple[str, str]] = []

    def _get(self, key: str, name: str) -> list[dict]:
        assert name in self._tables, name
        self.asked.append((key, name))
        return copy.deepcopy(self._tables[name][key])

    def has_table(self, name: str) -> bool:
        assert name in _TABLES, name
        self.asked.append(("has_table", name))
        return name in self._tables

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
    "DATE": mysql.DATE(),
    "VARCHAR(16)": mysql.VARCHAR(length=16),
    "VARCHAR(255)": mysql.VARCHAR(length=255),
    "DATETIME": mysql.DATETIME(),
    "INTEGER": mysql.INTEGER(),
    "SMALLINT": mysql.SMALLINT(),
    "DECIMAL(20,8)": mysql.DECIMAL(precision=20, scale=8),
}


def _design_table(table: str) -> dict:
    """The table the way MySQL's inspector reflects it: unique constraints are also indexes."""
    return {
        "columns": [
            {"name": name, "type": _MYSQL_TYPES[kind], "nullable": nullable}
            for name, (kind, nullable) in _DESIGN_COLUMNS[table].items()
        ],
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
        "check_constraints": [{"name": name, "sqltext": "…"} for name in _DESIGN_CHECKS[table]],
        "delete_rules": {name: key[3] for name, key in _DESIGN_FOREIGN_KEYS[table].items()},
    }


def _both() -> dict[str, dict]:
    return {_RUNS: _design_table(_RUNS), _FINDINGS: _design_table(_FINDINGS)}


def _with(
    monkeypatch,
    tables: dict[str, dict],
    *,
    dialect: str = "mysql",
    as_sql: bool = False,
    not_empty: tuple[str, ...] = (),
) -> tuple[ModuleType, _FakeOp, _FakeInspector, _FakeBind]:
    migration = _load()
    rules = {name: dict(table["delete_rules"]) for name, table in tables.items()}
    bind = _FakeBind(rules, set(not_empty))
    fake = _FakeOp(bind, dialect=dialect, as_sql=as_sql)
    inspector = _FakeInspector(tables)
    monkeypatch.setattr(migration, "op", fake)
    monkeypatch.setattr(migration, "sa", _Sa(inspector))
    return migration, fake, inspector, bind


def _created(fake: _FakeOp) -> list[str]:
    """The tables the MySQL upgrade's `CREATE TABLE` statements build, in order."""
    names = []
    for kind, (statement, *_) in fake.calls:
        assert kind == "execute"
        match = re.match(r"CREATE TABLE (\w+) \(", _normalised(str(statement)))
        assert match is not None, statement
        names.append(match.group(1).lower())
    return names


def test_0029_upgrade_creates_both_missing_tables_one_statement_each(monkeypatch) -> None:
    migration, fake, _, _ = _with(monkeypatch, {})
    migration.upgrade()

    assert _created(fake) == [_RUNS, _FINDINGS]


@pytest.mark.parametrize("dialect", ["mysql", "sqlite"])
def test_0029_upgrade_with_the_design_shape_issues_no_ddl(monkeypatch, dialect: str) -> None:
    migration, fake, inspector, bind = _with(monkeypatch, _both(), dialect=dialect)
    migration.upgrade()

    assert fake.calls == []
    for table in _TABLES:
        for key in ("columns", "indexes", "foreign_keys", "check_constraints"):
            assert (key, table) in inspector.asked
    # Both in place: no "any row?" question either.
    assert not [query for query in bind.queries if query.startswith("SELECT 1 FROM")]


def _drop_index(table: str, name: str):
    def mutate(tables: dict[str, dict]) -> None:
        reflected = tables[table]
        reflected["indexes"] = [i for i in reflected["indexes"] if i["name"] != name]
        reflected["unique_constraints"] = [
            u for u in reflected["unique_constraints"] if u["name"] != name
        ]

    return mutate


def _drop_tenant_index(tables: dict[str, dict]) -> None:
    # What MySQL leaves without it: an index of its own for the foreign key.
    _drop_index(_FINDINGS, _FINDINGS_TENANT_INDEX)(tables)
    tables[_FINDINGS]["indexes"].append(
        {"name": _FK_TENANT, "column_names": ["tenant_id"], "unique": False}
    )


def _run_date_unique(tables: dict[str, dict]) -> None:
    for index in tables[_RUNS]["indexes"]:
        if index["name"] == "ix_wallet_reconciliation_runs_run_date":
            index["unique"] = True


def _reverse_status_index(tables: dict[str, dict]) -> None:
    for index in tables[_RUNS]["indexes"]:
        if index["name"] == "ix_wallet_reconciliation_runs_status_finished":
            index["column_names"] = ["finished_at", "status"]


def _prefix_length(tables: dict[str, dict]) -> None:
    for index in tables[_RUNS]["indexes"]:
        if index["name"] == "uq_wallet_reconciliation_runs_public_id":
            index["dialect_options"] = {"mysql_length": {"public_id": 8}}


def _cascade(tables: dict[str, dict]) -> None:
    tables[_FINDINGS]["delete_rules"][_FK_RUN] = "CASCADE"
    for key in tables[_FINDINGS]["foreign_keys"]:
        if key["name"] == _FK_RUN:
            key["options"] = {"ondelete": "CASCADE"}


def _no_run_foreign_key(tables: dict[str, dict]) -> None:
    tables[_FINDINGS]["foreign_keys"] = [
        k for k in tables[_FINDINGS]["foreign_keys"] if k["name"] != _FK_RUN
    ]
    del tables[_FINDINGS]["delete_rules"][_FK_RUN]


def _drop_check(table: str, name: str):
    def mutate(tables: dict[str, dict]) -> None:
        tables[table]["check_constraints"] = [
            c for c in tables[table]["check_constraints"] if c["name"] != name
        ]

    return mutate


def _extra_check(tables: dict[str, dict]) -> None:
    tables[_RUNS]["check_constraints"].append(
        {"name": "ck_wallet_reconciliation_runs_extra", "sqltext": "…"}
    )


def _extra_column(tables: dict[str, dict]) -> None:
    tables[_RUNS]["columns"].append(
        {"name": "extra_note", "type": mysql.VARCHAR(16), "nullable": True}
    )


def _missing_column(tables: dict[str, dict]) -> None:
    tables[_RUNS]["columns"] = [c for c in tables[_RUNS]["columns"] if c["name"] != "lease_until"]


def _column(table: str, name: str, **changes: object):
    def mutate(tables: dict[str, dict]) -> None:
        for column in tables[table]["columns"]:
            if column["name"] == name:
                column.update(changes)

    return mutate


# Each wrong shape with the table it is on: the refusal names that table.
_WRONG_SHAPES = {
    "runs without the running_slot unique": (_RUNS, _drop_index(_RUNS, _RUNNING_SLOT_UNIQUE)),
    "run_date unique": (_RUNS, _run_date_unique),
    "the status index in another order": (_RUNS, _reverse_status_index),
    "a prefix length": (_RUNS, _prefix_length),
    "the running_slot check missing": (
        _RUNS,
        _drop_check(_RUNS, "ck_wallet_reconciliation_runs_running_slot"),
    ),
    "an extra check": (_RUNS, _extra_check),
    "runs with an extra column": (_RUNS, _extra_column),
    "a column missing": (_RUNS, _missing_column),
    "running_slot not nullable": (_RUNS, _column(_RUNS, "running_slot", nullable=False)),
    "status wider": (_RUNS, _column(_RUNS, "status", type=mysql.VARCHAR(32))),
    "balance a float": (_FINDINGS, _column(_FINDINGS, "balance", type=mysql.DOUBLE())),
    "findings without the tenant index": (_FINDINGS, _drop_tenant_index),
    "findings' run foreign key not RESTRICT": (_FINDINGS, _cascade),
    "no run foreign key": (_FINDINGS, _no_run_foreign_key),
    "the codes check missing": (
        _FINDINGS,
        _drop_check(_FINDINGS, "ck_wallet_reconciliation_findings_codes"),
    ),
}


@pytest.mark.parametrize("shape", list(_WRONG_SHAPES))
@pytest.mark.parametrize("dialect", ["mysql", "sqlite"])
def test_0029_upgrade_refuses_a_wrong_shape_before_any_ddl(
    monkeypatch, shape: str, dialect: str
) -> None:
    """Design §2 "迁移写法": both tables there, any item missing, extra or different —
    nothing is built, the message names the table and says what to do."""
    table, mutate = _WRONG_SHAPES[shape]
    tables = _both()
    mutate(tables)
    migration, fake, _, _ = _with(monkeypatch, tables, dialect=dialect)

    with pytest.raises(RuntimeError) as raised:
        migration.upgrade()

    assert fake.calls == []
    message = str(raised.value)
    assert f"table {table} exists but differs" in message
    assert "No DDL was issued" in message
    assert f"DROP TABLE {_FINDINGS}, then DROP TABLE {_RUNS}" in message
    assert "data incident" in message


@pytest.mark.parametrize("dialect", ["mysql", "sqlite"])
def test_0029_upgrade_refuses_findings_without_runs(monkeypatch, dialect: str) -> None:
    migration, fake, _, bind = _with(
        monkeypatch, {_FINDINGS: _design_table(_FINDINGS)}, dialect=dialect
    )

    with pytest.raises(RuntimeError) as raised:
        migration.upgrade()

    assert fake.calls == []
    assert not [query for query in bind.queries if query.startswith("SELECT 1 FROM")]
    message = str(raised.value)
    assert f"table {_FINDINGS} exists without table {_RUNS}" in message
    assert "No DDL was issued" in message
    assert f"DROP TABLE {_FINDINGS})" in message
    assert "data incident" in message


def test_0029_upgrade_completes_an_interrupted_upgrade(monkeypatch) -> None:
    """Design §2 "迁移写法": only runs, of the design shape, empty — findings is built."""
    migration, fake, _, bind = _with(monkeypatch, {_RUNS: _design_table(_RUNS)})
    migration.upgrade()

    assert f"SELECT 1 FROM {_RUNS} LIMIT 1" in bind.queries
    assert _created(fake) == [_FINDINGS]


def test_0029_upgrade_on_sqlite_completes_an_interrupted_upgrade(monkeypatch) -> None:
    migration, fake, _, _ = _with(monkeypatch, {_RUNS: _design_table(_RUNS)}, dialect="sqlite")
    migration.upgrade()

    assert fake.calls == [
        ("create_table", (_FINDINGS,)),
        ("create_index", (_FINDINGS_TENANT_INDEX, _FINDINGS, ["tenant_id"], False)),
    ]


@pytest.mark.parametrize("dialect", ["mysql", "sqlite"])
def test_0029_upgrade_refuses_runs_alone_with_a_row(monkeypatch, dialect: str) -> None:
    migration, fake, _, _ = _with(
        monkeypatch, {_RUNS: _design_table(_RUNS)}, dialect=dialect, not_empty=(_RUNS,)
    )

    with pytest.raises(RuntimeError) as raised:
        migration.upgrade()

    assert fake.calls == []
    message = str(raised.value)
    assert f"table {_RUNS} exists without table {_FINDINGS} and is not empty" in message
    assert "No DDL was issued" in message
    assert "data incident: do not drop it" in message


@pytest.mark.parametrize("dialect", ["mysql", "sqlite"])
def test_0029_upgrade_refuses_runs_alone_in_a_wrong_shape(monkeypatch, dialect: str) -> None:
    tables = _both()
    _extra_column(tables)
    del tables[_FINDINGS]
    migration, fake, _, _ = _with(monkeypatch, tables, dialect=dialect)

    with pytest.raises(RuntimeError) as raised:
        migration.upgrade()

    assert fake.calls == []
    message = str(raised.value)
    assert f"table {_RUNS} exists but differs" in message
    assert f"(DROP TABLE {_RUNS})" in message


def test_0029_upgrade_on_sqlite_creates_both_tables_and_their_indexes(monkeypatch) -> None:
    migration, fake, _, _ = _with(monkeypatch, {}, dialect="sqlite")
    migration.upgrade()

    assert fake.calls == [
        ("create_table", (_RUNS,)),
        ("create_index", ("ix_wallet_reconciliation_runs_run_date", _RUNS, ["run_date"], False)),
        (
            "create_index",
            (
                "ix_wallet_reconciliation_runs_status_finished",
                _RUNS,
                ["status", "finished_at"],
                False,
            ),
        ),
        ("create_table", (_FINDINGS,)),
        ("create_index", (_FINDINGS_TENANT_INDEX, _FINDINGS, ["tenant_id"], False)),
    ]


def test_0029_offline_upgrade_emits_both_tables_without_reading(monkeypatch) -> None:
    migration, fake, inspector, bind = _with(monkeypatch, _both(), as_sql=True)
    migration.upgrade()

    assert inspector.asked == []
    assert bind.queries == []
    assert _created(fake) == [_RUNS, _FINDINGS]


def test_0029_offline_downgrade_refuses_before_reading_or_ddl(monkeypatch) -> None:
    """Design §2 "迁移写法": offline the emptiness cannot be checked — refuse, no DROP TABLE."""
    migration, fake, inspector, bind = _with(monkeypatch, _both(), as_sql=True)

    with pytest.raises(RuntimeError) as raised:
        migration.downgrade()

    assert fake.calls == []
    assert inspector.asked == []
    assert bind.queries == []
    message = str(raised.value)
    assert _RUNS in message and _FINDINGS in message
    assert "online" in message


@pytest.mark.parametrize("dialect", ["mysql", "sqlite"])
def test_0029_downgrade_drops_both_empty_tables_findings_first(monkeypatch, dialect: str) -> None:
    migration, fake, _, bind = _with(monkeypatch, _both(), dialect=dialect)
    migration.downgrade()

    assert bind.queries == [f"SELECT 1 FROM {table} LIMIT 1" for table in _TABLES]
    assert fake.calls == [("drop_table", (_FINDINGS,)), ("drop_table", (_RUNS,))]


@pytest.mark.parametrize("not_empty", [(_RUNS,), (_FINDINGS,), (_FINDINGS, _RUNS)])
@pytest.mark.parametrize("dialect", ["mysql", "sqlite"])
def test_0029_downgrade_refuses_with_rows_before_any_ddl(
    monkeypatch, dialect: str, not_empty: tuple[str, ...]
) -> None:
    """Design §5 / §8: a row in either table — no DDL, no row deleted; the message names the
    table, that it is not empty, and to keep the tables and roll back only the application
    image."""
    migration, fake, _, _ = _with(monkeypatch, _both(), dialect=dialect, not_empty=not_empty)

    with pytest.raises(RuntimeError) as raised:
        migration.downgrade()

    assert fake.calls == []
    message = str(raised.value)
    for table in _TABLES:
        assert (f"table {table} is not empty" in message) == (table in not_empty), table
    assert "No DDL was issued and no row was deleted" in message
    assert "roll back only the application image" in message


@pytest.mark.parametrize("dialect", ["mysql", "sqlite"])
def test_0029_downgrade_without_the_tables_does_nothing(monkeypatch, dialect: str) -> None:
    migration, fake, inspector, bind = _with(monkeypatch, {}, dialect=dialect)
    migration.downgrade()

    assert inspector.asked == [("has_table", table) for table in _TABLES]
    assert bind.queries == []
    assert fake.calls == []


@pytest.mark.parametrize("table", [_RUNS, _FINDINGS])
@pytest.mark.parametrize("dialect", ["mysql", "sqlite"])
def test_0029_downgrade_with_one_empty_table_drops_it(
    monkeypatch, dialect: str, table: str
) -> None:
    """Design §2 "迁移写法": completes a downgrade interrupted between the two DROP TABLE."""
    migration, fake, _, bind = _with(monkeypatch, {table: _design_table(table)}, dialect=dialect)
    migration.downgrade()

    assert bind.queries == [f"SELECT 1 FROM {table} LIMIT 1"]
    assert fake.calls == [("drop_table", (table,))]


@pytest.mark.parametrize("table", [_RUNS, _FINDINGS])
@pytest.mark.parametrize("dialect", ["mysql", "sqlite"])
def test_0029_downgrade_with_one_table_with_rows_refuses(
    monkeypatch, dialect: str, table: str
) -> None:
    migration, fake, _, _ = _with(
        monkeypatch, {table: _design_table(table)}, dialect=dialect, not_empty=(table,)
    )

    with pytest.raises(RuntimeError) as raised:
        migration.downgrade()

    assert fake.calls == []
    assert f"table {table} is not empty" in str(raised.value)


# ---------------------------------------------------------------------------
# SQLite (models' metadata)
# ---------------------------------------------------------------------------


@contextmanager
def _sqlite() -> Iterator[Engine]:
    """The unit tests build SQLite with `create_all` from the models, not with the migrations."""
    engine = create_engine("sqlite+pysqlite:///:memory:")
    try:
        Base.metadata.create_all(
            engine,
            tables=[
                Tenant.__table__,
                WalletReconciliationRun.__table__,
                WalletReconciliationFinding.__table__,
            ],
        )
        yield engine
    finally:
        engine.dispose()


def _insert_tenant(connection: sqlalchemy.Connection) -> int:
    return int(
        connection.execute(
            insert(Tenant).values(
                public_id=str(uuid.uuid4()),
                company_name="Wallet Reconciliation Migration Sdn Bhd",
                email="ops@example.com",
                created_at=_NOW,
                updated_at=_NOW,
            )
        ).inserted_primary_key[0]
    )


def _run_params(overrides: dict[str, object]) -> dict[str, object]:
    """A legal COMPLETED run, with `overrides` on top."""
    params: dict[str, object] = {
        "public_id": str(uuid.uuid4()),
        "run_date": _TODAY_TEXT,
        "status": "COMPLETED",
        "started_at": _NOW_TEXT,
        "finished_at": _NOW_TEXT,
        "cursor": 3,
        "checked": 3,
        "inconsistent": 1,
        "lease_owner": None,
        "lease_until": None,
        "running_slot": None,
    }
    params.update(overrides)
    return params


def _insert_run(connection: sqlalchemy.Connection, overrides: dict[str, object]) -> int:
    params = _run_params(overrides)
    connection.execute(_INSERT_RUN, params)
    return int(connection.execute(_RUN_ID, {"public_id": params["public_id"]}).scalar_one())


def _finding_params(run_id: int, tenant_id: int, overrides: dict[str, object]) -> dict[str, object]:
    params: dict[str, object] = {
        "run_id": run_id,
        "tenant_id": tenant_id,
        "problem_codes": "BALANCE_NOT_LEDGER_SUM,BALANCE_NOT_LAST_BALANCE_AFTER",
        "balance": "12.50000000",
        "wallet_version": 4,
        "ledger_rows": 4,
        "checked_at": _NOW_TEXT,
    }
    params.update(overrides)
    return params


def _seed_legal(connection: sqlalchemy.Connection) -> dict[str, int]:
    """A tenant, a COMPLETED and an ABANDONED run (two NULL running_slot rows) and one finding
    (on the COMPLETED run): all legal."""
    tenant_id = _insert_tenant(connection)
    completed = _insert_run(connection, {})
    abandoned = _insert_run(connection, {"status": "ABANDONED", "inconsistent": 0})
    connection.execute(_INSERT_FINDING, _finding_params(completed, tenant_id, {}))
    return {"tenant_id": tenant_id, "abandoned": abandoned}


def _insert_illegal(connection: sqlalchemy.Connection, ids: dict[str, int], row: str) -> None:
    table, overrides, _, _ = _ILLEGAL_ROWS[row]
    if table == "finding":
        # On the ABANDONED run: (run_id, tenant_id) is not taken there.
        connection.execute(
            _INSERT_FINDING, _finding_params(ids["abandoned"], ids["tenant_id"], overrides)
        )
    else:
        connection.execute(_INSERT_RUN, _run_params(overrides))


def _counts(connection: sqlalchemy.Connection) -> tuple[int, int]:
    return (
        int(connection.execute(text(f"SELECT COUNT(*) FROM {_RUNS}")).scalar_one()),
        int(connection.execute(text(f"SELECT COUNT(*) FROM {_FINDINGS}")).scalar_one()),
    )


def test_0029_sqlite_tables_get_the_indexes_and_the_foreign_keys() -> None:
    with _sqlite() as engine:
        inspector = inspect(engine)
        shapes = {}
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
            shapes[table] = (indexes, keys)

    # The checks are judged by what they refuse (below), not by SQLite's reflection of them.
    for table in _TABLES:
        assert shapes[table] == (_DESIGN_INDEXES[table], _DESIGN_FOREIGN_KEYS[table]), table


@pytest.mark.parametrize("row", list(_ILLEGAL_ROWS))
def test_0029_sqlite_checks_refuse_the_illegal_rows(row: str) -> None:
    """Design §7 "CHECK" on SQLite: each is refused (a CHECK, or the running_slot unique); the
    legal rows go in, two NULL running_slot rows among them."""
    _, _, setup, refused_by = _ILLEGAL_ROWS[row]
    with _sqlite() as engine:
        with engine.begin() as connection:
            ids = _seed_legal(connection)
            for overrides in setup:
                _insert_run(connection, overrides)
            before = _counts(connection)

        with pytest.raises(DBAPIError) as raised:
            with engine.begin() as connection:
                _insert_illegal(connection, ids, row)
        expected = (
            "CHECK constraint failed" if refused_by == "check" else "UNIQUE constraint failed"
        )
        assert expected in str(raised.value), row

        with engine.connect() as connection:
            assert _counts(connection) == before


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


def _present(engine: Engine) -> set[str]:
    inspector = inspect(engine)
    return {table for table in _TABLES if inspector.has_table(table)}


def _table_shape(engine: Engine, table: str) -> dict[str, object]:
    """One table as reflected, in the migration's terms; delete rules from information_schema;
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
                {"table": table},
            ).all()
        )
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


def _assert_design_shape(engine: Engine) -> None:
    """Design §7 "迁移形状": reflected = the model's = the design's, for both tables."""
    for table in _TABLES:
        assert _table_shape(engine, table) == _model_shape(table) == _design_shape(table), table


def _ours(engine: Engine) -> dict[str, object]:
    """The shape and the rows of whichever of the two tables exist."""
    state: dict[str, object] = {}
    for table in sorted(_present(engine)):
        with engine.connect() as connection:
            rows = [
                tuple(row) for row in connection.execute(text(f"SELECT * FROM {table} ORDER BY id"))
            ]
        state[table] = (_table_shape(engine, table), rows)
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
        triggers = {tuple(row) for row in connection.execute(_TRIGGERS_QUERY).all()}
        checks = {tuple(row) for row in connection.execute(_CHECKS_QUERY).all()}
    return {"tables": tables, "triggers": triggers, "checks": checks}


def _without_ours(snapshot: dict[str, object]) -> dict[str, object]:
    tables = snapshot["tables"]
    assert isinstance(tables, dict)
    checks = snapshot["checks"]
    assert isinstance(checks, set)
    return {
        "tables": {name: shape for name, shape in tables.items() if name not in _TABLES},
        "triggers": snapshot["triggers"],
        "checks": {check for check in checks if check[0] not in _TABLES},
    }


def _ledger_and_wallets(engine: Engine) -> tuple[list[tuple[object, ...]], ...]:
    with engine.connect() as connection:
        wallets = [tuple(row) for row in connection.execute(_WALLET_ROWS).all()]
        ledger = [tuple(row) for row in connection.execute(_LEDGER_ROWS).all()]
    return wallets, ledger


def _at_0028(alembic_config: Config, engine: Engine) -> None:
    command.upgrade(alembic_config, "head")
    command.downgrade(alembic_config, _DOWN_REVISION)
    assert _version(engine) == _DOWN_REVISION
    assert _present(engine) == set()


def _back_to_head(alembic_config: Config, engine: Engine, tenant_ids: list[int]) -> None:
    """Empties whatever is left of the two tables (and removes the seeded tenants), drops a
    hand-made or half-built pair, and upgrades to head again."""
    present = _present(engine)
    with engine.begin() as connection:
        for table in _TABLES:
            if table in present:
                connection.execute(text(f"DELETE FROM {table}"))
        if tenant_ids:
            connection.execute(delete(Tenant).where(Tenant.id.in_(tenant_ids)))
    version = _version(engine)
    if version == _DOWN_REVISION:
        for table in _TABLES:
            if table in present:
                _execute(engine, f"DROP TABLE {table}")
    elif version == _REVISION and len(present) == 1:
        # Only one left at 0029 (the interrupted downgrade): the downgrade drops it.
        command.downgrade(alembic_config, _DOWN_REVISION)
    command.upgrade(alembic_config, "head")


@needs_mysql
def test_0029_round_trip_on_empty_tables(alembic_config: Config, engine: Engine) -> None:
    """Design §7 "迁移形状": 0028 → 0029 → 0028 → 0029 with both tables empty. The upgrade
    sends one `CREATE TABLE` per table (runs first), the shapes are the models'; the downgrade
    drops findings, then runs; every other table, key, check and trigger and the wallet and ledger
    rows are the same throughout (§3 INV-4 / INV-5)."""
    _at_0028(alembic_config, engine)
    try:
        at_0028 = _schema_snapshot(engine)
        rows = _ledger_and_wallets(engine)

        with _ddl_statements() as statements:
            command.upgrade(alembic_config, _REVISION)
        assert [_normalised(s).split(" (")[0] for s in statements] == [
            f"CREATE TABLE {_RUNS.upper()}",
            f"CREATE TABLE {_FINDINGS.upper()}",
        ]
        assert _version(engine) == _REVISION
        _assert_design_shape(engine)
        at_0029 = _schema_snapshot(engine)
        assert _without_ours(at_0029) == at_0028
        assert _ledger_and_wallets(engine) == rows

        with _ddl_statements() as statements:
            command.downgrade(alembic_config, _DOWN_REVISION)
        assert [_normalised(s) for s in statements] == [
            f"DROP TABLE {_FINDINGS.upper()}",
            f"DROP TABLE {_RUNS.upper()}",
        ]
        assert _version(engine) == _DOWN_REVISION
        assert _present(engine) == set()
        assert _schema_snapshot(engine) == at_0028
        assert _ledger_and_wallets(engine) == rows

        command.upgrade(alembic_config, _REVISION)
        assert _schema_snapshot(engine) == at_0029
        _assert_design_shape(engine)
        assert _ledger_and_wallets(engine) == rows
    finally:
        _back_to_head(alembic_config, engine, [])


@needs_mysql
def test_0029_rerun_with_both_tables_built_only_moves_the_version(
    alembic_config: Config, engine: Engine
) -> None:
    """Design §7 "迁移重跑：形状正确": upgraded, then `alembic_version` put back to 0028 (as if
    writing the version failed after both tables were built) — no DDL, the version moves to 0029,
    the tables are as after the first upgrade."""
    _at_0028(alembic_config, engine)
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
        _back_to_head(alembic_config, engine, [])


@needs_mysql
@pytest.mark.parametrize("shape", list(_WRONG_SHAPE_DDL))
def test_0029_rerun_with_a_wrong_shape_fails_before_any_ddl(
    alembic_config: Config, engine: Engine, shape: str
) -> None:
    """Design §7 "迁移重跑：形状不对": version 0028 and fresh hand-made tables (or a runs table
    with a row) — the upgrade fails naming the table, issues no DDL, the version stays 0028 and
    the hand-made tables and their rows are as they were."""
    _at_0028(alembic_config, engine)
    statements_ddl, with_row = _WRONG_SHAPE_DDL[shape]
    try:
        for statement in statements_ddl:
            _execute(engine, statement)
        if with_row:
            with engine.begin() as connection:
                _insert_run(connection, {})
        hand_made = _ours(engine)

        with _ddl_statements() as statements:
            with pytest.raises(RuntimeError) as raised:
                command.upgrade(alembic_config, _REVISION)

        assert statements == []
        message = str(raised.value)
        assert "No DDL was issued" in message
        assert "data incident" in message
        assert any(f"table {table}" in message for table in hand_made)
        assert _version(engine) == _DOWN_REVISION
        assert _ours(engine) == hand_made
    finally:
        _back_to_head(alembic_config, engine, [])


@needs_mysql
def test_0029_mysql_upgrade_completes_an_interrupted_upgrade(
    alembic_config: Config, engine: Engine
) -> None:
    """Design §7 "补完中断的升级": version 0028, only an empty runs table of the design
    shape — the upgrade sends one `CREATE TABLE` for findings; the version is 0029; both
    tables are the models'."""
    _at_0028(alembic_config, engine)
    try:
        _execute(engine, _RUNS_DDL)

        with _ddl_statements() as statements:
            command.upgrade(alembic_config, _REVISION)

        assert [_normalised(s).split(" (")[0] for s in statements] == [
            f"CREATE TABLE {_FINDINGS.upper()}"
        ]
        assert _version(engine) == _REVISION
        _assert_design_shape(engine)
    finally:
        _back_to_head(alembic_config, engine, [])


def _seed_run_and_finding(engine: Engine) -> int:
    """A tenant, a COMPLETED run and a finding of it. Returns the tenant id."""
    with engine.begin() as connection:
        tenant_id = _insert_tenant(connection)
        run_id = int(
            connection.execute(
                insert(WalletReconciliationRun).values(
                    public_id=str(uuid.uuid4()),
                    run_date=_NOW.date(),
                    status=ReconciliationRunStatus.COMPLETED,
                    started_at=_NOW,
                    finished_at=_NOW + dt.timedelta(minutes=5),
                    cursor_tenant_id=tenant_id,
                    wallets_checked=1,
                    wallets_inconsistent=1,
                )
            ).inserted_primary_key[0]
        )
        connection.execute(
            insert(WalletReconciliationFinding).values(
                run_id=run_id,
                tenant_id=tenant_id,
                problem_codes="LEDGER_MISSING",
                balance=Decimal("12.50000000"),
                wallet_version=3,
                ledger_rows=0,
                checked_at=_NOW,
            )
        )
    return tenant_id


@needs_mysql
def test_0029_downgrade_with_rows_refuses_before_any_ddl(
    alembic_config: Config, engine: Engine
) -> None:
    """Design §7 "带数据降级" / §5: a run and its finding — the downgrade fails naming both
    tables, no DDL, the tables and rows are as they were, `alembic_version` stays 0029."""
    command.upgrade(alembic_config, "head")
    tenant_ids: list[int] = []
    try:
        # Later revisions are not this downgrade's: start at 0029.
        command.downgrade(alembic_config, _REVISION)
        tenant_ids.append(_seed_run_and_finding(engine))
        before_schema = _schema_snapshot(engine)
        before = _ours(engine)

        with _ddl_statements() as statements:
            with pytest.raises(RuntimeError) as raised:
                command.downgrade(alembic_config, _DOWN_REVISION)

        assert statements == []
        message = str(raised.value)
        assert f"table {_FINDINGS} is not empty" in message
        assert f"table {_RUNS} is not empty" in message
        assert "roll back only the application image" in message
        assert _version(engine) == _REVISION
        assert _schema_snapshot(engine) == before_schema
        assert _ours(engine) == before
    finally:
        _back_to_head(alembic_config, engine, tenant_ids)


@needs_mysql
@pytest.mark.parametrize("with_row", [False, True], ids=["empty", "one row"])
def test_0029_downgrade_with_only_runs_left(
    alembic_config: Config, engine: Engine, with_row: bool
) -> None:
    """Design §7 "只存在一张时降级": at 0029 with findings dropped by hand (the downgrade
    interrupted between the two DROP TABLE). Empty runs: the downgrade drops it, version 0028.
    A row in runs: it fails naming runs, no DDL, runs as it was, version still 0029."""
    command.upgrade(alembic_config, "head")
    try:
        command.downgrade(alembic_config, _REVISION)
        _execute(engine, f"DROP TABLE {_FINDINGS}")
        if with_row:
            with engine.begin() as connection:
                _insert_run(connection, {})
        before = _ours(engine)

        with _ddl_statements() as statements:
            if with_row:
                with pytest.raises(RuntimeError) as raised:
                    command.downgrade(alembic_config, _DOWN_REVISION)
            else:
                command.downgrade(alembic_config, _DOWN_REVISION)

        if with_row:
            assert statements == []
            message = str(raised.value)
            assert f"table {_RUNS} is not empty" in message
            assert f"table {_FINDINGS} is not empty" not in message
            assert _version(engine) == _REVISION
            assert _ours(engine) == before
        else:
            assert [_normalised(s) for s in statements] == [f"DROP TABLE {_RUNS.upper()}"]
            assert _version(engine) == _DOWN_REVISION
            assert _present(engine) == set()
    finally:
        _back_to_head(alembic_config, engine, [])


@needs_mysql
def test_0029_offline_downgrade_refuses_without_drop_table(alembic_config: Config) -> None:
    """Design §2 "迁移写法": `--sql` cannot tell whether the tables are empty — the downgrade
    fails saying it must run online, and its output has no DROP TABLE. The offline upgrade, as
    0027's, emits both tables without reading the database."""
    upgrade_buffer = io.StringIO()
    command.upgrade(
        Config("alembic.ini", output_buffer=upgrade_buffer),
        f"{_DOWN_REVISION}:{_REVISION}",
        sql=True,
    )
    emitted = _normalised(upgrade_buffer.getvalue())
    assert f"CREATE TABLE {_RUNS.upper()} (" in emitted
    assert f"CREATE TABLE {_FINDINGS.upper()} (" in emitted

    downgrade_buffer = io.StringIO()
    with pytest.raises(RuntimeError) as raised:
        command.downgrade(
            Config("alembic.ini", output_buffer=downgrade_buffer),
            f"{_REVISION}:{_DOWN_REVISION}",
            sql=True,
        )

    message = str(raised.value)
    assert _RUNS in message and _FINDINGS in message
    assert "online" in message
    assert "DROP TABLE" not in _normalised(downgrade_buffer.getvalue())


@needs_mysql
@pytest.mark.parametrize("row", list(_ILLEGAL_ROWS))
def test_0029_mysql_checks_refuse_the_illegal_rows(
    alembic_config: Config, engine: Engine, row: str
) -> None:
    """Design §7 "CHECK" on MySQL: each is refused by its CHECK (3819) or, for the second
    running_slot 1, by the unique index (1062); the legal rows go in. All is rolled back."""
    command.upgrade(alembic_config, "head")
    _, _, setup, refused_by = _ILLEGAL_ROWS[row]
    expected = _ER_CHECK_CONSTRAINT_VIOLATED if refused_by == "check" else _ER_DUP_ENTRY
    with engine.connect() as connection:
        transaction = connection.begin()
        try:
            ids = _seed_legal(connection)
            for overrides in setup:
                _insert_run(connection, overrides)
            before = _counts(connection)

            with pytest.raises(DBAPIError) as raised:
                with connection.begin_nested():
                    _insert_illegal(connection, ids, row)
            assert int(raised.value.orig.args[0]) == expected, row

            assert _counts(connection) == before
        finally:
            transaction.rollback()

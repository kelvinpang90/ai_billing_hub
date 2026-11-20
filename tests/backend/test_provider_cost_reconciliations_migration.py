"""0041_provider_cost_recon（AIH-TASK-242，设计闸门 #755 v1）.

Design §2 "数据库（迁移 0041，AIH-TASK-242）", §3 INV-6 / INV-10, §4 (the last row of the
transition table), §5 (the "迁移中断" row), §6 "公开仓库与保留", §7 (the four rows of task 242),
§8 "数据迁移" / "回滚或前滚".

The first cases need no database: the revision chain; the columns, the generated column, the
primary key, the indexes, the foreign keys and the checks in the migration equal the model's and
the design's, under the same names and in the same order; the `CREATE TABLE` and the two
`CREATE TRIGGER` the MySQL upgrade sends; every branch of the upgrade (nothing yet / everything
built / a trigger missing / a trigger that differs / each wrong shape / the prerequisite check /
`--sql`) and of the downgrade (empty / with a row / `--sql` / one trigger left / the table already
gone), with `alembic.op` and the inspector stood in. `create_all` on SQLite (what the unit tests
run) builds the table; its checks and unique indexes refuse every illegal row. SQLite does not
enforce foreign keys in the backend tests (no `PRAGMA foreign_keys`) and this task does not turn
them on, so the composite self-referencing foreign key is only tested on MySQL.

On a real MySQL (`BILLING_TEST_DATABASE_URL`; skipped when unset, never skipped in CI): the
0040 → 0041 → 0040 → 0041 round trip (every other table, key, check and trigger unchanged), the
index set read from information_schema.STATISTICS (exactly the six besides the primary key: no
hidden index for a foreign key), the rerun with everything built, one trigger missing, wrong
shapes (each a fresh hand-made table), the downgrade with a row and with the table gone, the
`--sql` upgrade and downgrade — the checks, the unique indexes and the composite foreign key on
the same illegal rows, and the triggers: an UPDATE and a DELETE of a row fail with SQLSTATE
45000. The Alembic chain does not run on SQLite (0006 onwards ask for `@@log_bin`), so every
migration scenario is MySQL's, as in test_payment_receipts_migration.py. Captured statements are
compared after normalising (backticks, case, spaces). Rows the tests commit are removed with
TRUNCATE with foreign key checks off (the triggers refuse DELETE; they are never dropped to clean
up). No clock is read: every time is a constant.
"""

from __future__ import annotations

import copy
import importlib.util
import io
import os
import pathlib
import re
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

from alembic import command
from app.models.base import Base
from app.models.provider_cost_reconciliations import (
    COVERAGE_METHOD_CHECK,
    FX_CHECK,
    PERIOD_CHECK,
    REASON_CHECK,
    ROOT_SLOT_EXPRESSION,
    SCOPE_CHECK,
    ProviderCostReconciliation,
)

TEST_DATABASE_URL = os.environ.get("BILLING_TEST_DATABASE_URL", "")

needs_mysql = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="BILLING_TEST_DATABASE_URL is not set; migration tests need a real MySQL",
)

_REVISION = "0041_provider_cost_recon"
_DOWN_REVISION = "0040_reset_request_index"
_MIGRATION = pathlib.Path("alembic/versions/20261117_0041_provider_cost_reconciliations.py")
_TABLE = "provider_cost_reconciliations"
_UPDATE_TRIGGER = "trg_provider_cost_reconciliations_before_update"
_DELETE_TRIGGER = "trg_provider_cost_reconciliations_before_delete"

# Design §2 "数据库": the columns, in order.
_DESIGN_COLUMNS = {
    "id": ("BIGINT", False),
    "public_id": ("CHAR(36)", False),
    "provider_id": ("BIGINT", False),
    "scope": ("VARCHAR(16)", False),
    "billing_month": ("CHAR(7)", False),
    "period_start": ("DATETIME", False),
    "period_end": ("DATETIME", False),
    "source_document_reference": ("VARCHAR(255)", False),
    "source_currency": ("CHAR(3)", False),
    "source_total": ("DECIMAL(20,8)", False),
    "fx_rate_version_id": ("BIGINT", True),
    "fx_rate_applied": ("DECIMAL(24,10)", True),
    "reconciled_cost_myr": ("DECIMAL(20,8)", False),
    "coverage_method": ("VARCHAR(32)", False),
    "estimated_event_count": ("BIGINT", False),
    "unprocessed_event_count": ("BIGINT", False),
    "outbox_unsettled_count": ("BIGINT", False),
    "outbox_discarded_count": ("BIGINT", False),
    "estimated_source_cost": ("DECIMAL(20,8)", False),
    "estimated_cost_myr": ("DECIMAL(20,8)", False),
    "supersedes_id": ("BIGINT", True),
    "reason": ("VARCHAR(255)", True),
    "created_by": ("BIGINT", False),
    "created_at": ("DATETIME", False),
    "root_slot": ("INTEGER", True),
}
_DESIGN_ROOT_SLOT = "CASE WHEN supersedes_id IS NULL THEN 1 END"
_DESIGN_PRIMARY_KEY = ("id",)
_ROOT_UNIQUE = "ux_provider_cost_reconciliations_root"
_PUBLIC_ID_UNIQUE = "uq_provider_cost_reconciliations_public_id"
_SUPERSEDES_UNIQUE = "uq_provider_cost_reconciliations_supersedes"
_CHAIN_KEY_UNIQUE = "uq_provider_cost_reconciliations_chain_key"
_FX_INDEX = "ix_provider_cost_reconciliations_fx_rate_version_id"
_CREATED_BY_INDEX = "ix_provider_cost_reconciliations_created_by"
_DESIGN_INDEXES: dict[str, tuple[tuple[str, ...], bool]] = {
    _ROOT_UNIQUE: (("provider_id", "scope", "billing_month", "root_slot"), True),
    _PUBLIC_ID_UNIQUE: (("public_id",), True),
    _SUPERSEDES_UNIQUE: (("supersedes_id", "provider_id", "scope", "billing_month"), True),
    _CHAIN_KEY_UNIQUE: (("id", "provider_id", "scope", "billing_month"), True),
    _FX_INDEX: (("fx_rate_version_id",), False),
    _CREATED_BY_INDEX: (("created_by",), False),
}
_FK_PROVIDER = "fk_provider_cost_reconciliations_provider_id"
_FK_FX = "fk_provider_cost_reconciliations_fx_rate_version_id"
_FK_CREATED_BY = "fk_provider_cost_reconciliations_created_by"
_FK_SUPERSEDES = "fk_provider_cost_reconciliations_supersedes"
_DESIGN_FOREIGN_KEYS: dict[str, tuple[tuple[str, ...], str, tuple[str, ...], str]] = {
    _FK_PROVIDER: (("provider_id",), "ai_providers", ("id",), "RESTRICT"),
    _FK_FX: (("fx_rate_version_id",), "fx_rate_versions", ("id",), "RESTRICT"),
    _FK_CREATED_BY: (("created_by",), "users", ("id",), "RESTRICT"),
    _FK_SUPERSEDES: (
        ("supersedes_id", "provider_id", "scope", "billing_month"),
        _TABLE,
        ("id", "provider_id", "scope", "billing_month"),
        "RESTRICT",
    ),
}


def _check(name: str) -> str:
    return f"ck_provider_cost_reconciliations_{name}"


# Design §2: exactly 13, in this order.
_DESIGN_CHECK_TEXTS = {
    _check("fx"): (
        "(source_currency = 'MYR' AND fx_rate_version_id IS NULL AND fx_rate_applied IS NULL"
        " AND reconciled_cost_myr = source_total) OR (source_currency <> 'MYR'"
        " AND fx_rate_version_id IS NOT NULL AND fx_rate_applied IS NOT NULL"
        " AND fx_rate_applied > 0)"
    ),
    _check("reason"): (
        "(supersedes_id IS NULL AND reason IS NULL)"
        " OR (supersedes_id IS NOT NULL AND reason IS NOT NULL)"
    ),
    _check("scope"): "scope IN ('AI_MODULE')",
    _check("coverage_method"): "coverage_method IN ('PERIOD_AGGREGATE')",
    _check("period"): "period_start < period_end",
    _check("source_total"): "source_total >= 0",
    _check("reconciled_cost"): "reconciled_cost_myr >= 0",
    _check("estimated_source_cost"): "estimated_source_cost >= 0",
    _check("estimated_cost_myr"): "estimated_cost_myr >= 0",
    _check("estimated_event_count"): "estimated_event_count >= 0",
    _check("unprocessed_event_count"): "unprocessed_event_count >= 0",
    _check("outbox_unsettled_count"): "outbox_unsettled_count >= 0",
    _check("outbox_discarded_count"): "outbox_discarded_count >= 0",
}
# The task contract's trigger body: unconditional.
_IMMUTABLE = "provider_cost_reconciliations rows are immutable"
_DESIGN_TRIGGER_BODY = f"BEGIN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = '{_IMMUTABLE}'; END"
_DESIGN_TRIGGERS = {
    _UPDATE_TRIGGER: (_TABLE, "BEFORE", "UPDATE"),
    _DELETE_TRIGGER: (_TABLE, "BEFORE", "DELETE"),
}

# The design's MySQL table, clause by clause; the wrong shapes below are edits of it.
_CLAUSES = [
    "id BIGINT NOT NULL AUTO_INCREMENT",
    "public_id CHAR(36) NOT NULL",
    "provider_id BIGINT NOT NULL",
    "scope VARCHAR(16) NOT NULL",
    "billing_month CHAR(7) NOT NULL",
    "period_start DATETIME NOT NULL",
    "period_end DATETIME NOT NULL",
    "source_document_reference VARCHAR(255) NOT NULL",
    "source_currency CHAR(3) NOT NULL",
    "source_total DECIMAL(20, 8) NOT NULL",
    "fx_rate_version_id BIGINT NULL",
    "fx_rate_applied DECIMAL(24, 10) NULL",
    "reconciled_cost_myr DECIMAL(20, 8) NOT NULL",
    "coverage_method VARCHAR(32) NOT NULL",
    "estimated_event_count BIGINT NOT NULL",
    "unprocessed_event_count BIGINT NOT NULL",
    "outbox_unsettled_count BIGINT NOT NULL",
    "outbox_discarded_count BIGINT NOT NULL",
    "estimated_source_cost DECIMAL(20, 8) NOT NULL",
    "estimated_cost_myr DECIMAL(20, 8) NOT NULL",
    "supersedes_id BIGINT NULL",
    "reason VARCHAR(255) NULL",
    "created_by BIGINT NOT NULL",
    "created_at DATETIME NOT NULL",
    f"root_slot INT GENERATED ALWAYS AS ({_DESIGN_ROOT_SLOT}) STORED",
    "PRIMARY KEY (id)",
    f"UNIQUE INDEX {_ROOT_UNIQUE} (provider_id, scope, billing_month, root_slot)",
    f"CONSTRAINT {_PUBLIC_ID_UNIQUE} UNIQUE (public_id)",
    f"CONSTRAINT {_SUPERSEDES_UNIQUE} UNIQUE (supersedes_id, provider_id, scope, billing_month)",
    f"CONSTRAINT {_CHAIN_KEY_UNIQUE} UNIQUE (id, provider_id, scope, billing_month)",
    f"INDEX {_FX_INDEX} (fx_rate_version_id)",
    f"INDEX {_CREATED_BY_INDEX} (created_by)",
    f"CONSTRAINT {_FK_PROVIDER} FOREIGN KEY (provider_id) REFERENCES ai_providers (id)"
    " ON DELETE RESTRICT",
    f"CONSTRAINT {_FK_FX} FOREIGN KEY (fx_rate_version_id) REFERENCES fx_rate_versions (id)"
    " ON DELETE RESTRICT",
    f"CONSTRAINT {_FK_CREATED_BY} FOREIGN KEY (created_by) REFERENCES users (id)"
    " ON DELETE RESTRICT",
    f"CONSTRAINT {_FK_SUPERSEDES} FOREIGN KEY (supersedes_id, provider_id, scope, billing_month)"
    f" REFERENCES {_TABLE} (id, provider_id, scope, billing_month) ON DELETE RESTRICT",
    *(f"CONSTRAINT {name} CHECK ({condition})" for name, condition in _DESIGN_CHECK_TEXTS.items()),
]

# MySQL reports a violated CHECK as 3819 (ER_CHECK_CONSTRAINT_VIOLATED), a duplicate key as 1062
# (ER_DUP_ENTRY), a missing parent row as 1452 (ER_NO_REFERENCED_ROW_2), a SIGNAL with SQLSTATE
# '45000' as 1644 (ER_SIGNAL_EXCEPTION).
_ER_CHECK_CONSTRAINT_VIOLATED = 3819
_ER_DUP_ENTRY = 1062
_ER_NO_REFERENCED_ROW_2 = 1452
_ER_SIGNAL_EXCEPTION = 1644

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
_GENERATED_QUERY = text(
    "SELECT COLUMN_NAME, GENERATION_EXPRESSION, EXTRA FROM information_schema.COLUMNS"
    " WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :table"
    " AND EXTRA IN ('STORED GENERATED', 'VIRTUAL GENERATED')"
)
_STATISTICS_QUERY = text(
    "SELECT DISTINCT INDEX_NAME FROM information_schema.STATISTICS"
    " WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :table AND INDEX_NAME <> 'PRIMARY'"
)
_ROW_COUNT = text(f"SELECT COUNT(*) FROM {_TABLE}")
_ROWS = text(f"SELECT * FROM {_TABLE} ORDER BY id")

# Raw SQL: the values go straight to the database's checks. `id` and `root_slot` are the
# database's.
_INSERT_COLUMNS = [name for name in _DESIGN_COLUMNS if name not in ("id", "root_slot")]
_INSERT_ROW = text(
    f"INSERT INTO {_TABLE} ({', '.join(_INSERT_COLUMNS)})"
    f" VALUES ({', '.join(f':{name}' for name in _INSERT_COLUMNS)})"
)
_ID_OF = text(f"SELECT id FROM {_TABLE} WHERE public_id = :public_id")
_ROOT_SLOT_OF = text(f"SELECT root_slot FROM {_TABLE} WHERE id = :id")

_PROVIDER_ID = 1
_CREATED_BY = 1
_FX_RATE_VERSION_ID = 1
_DECEMBER = {
    "billing_month": "2026-12",
    "period_start": "2026-12-01 00:00:00",
    "period_end": "2027-01-01 00:00:00",
    "source_document_reference": "test-statement-2026-12",
    "created_at": "2027-01-02 00:00:00",
}
_JANUARY = {
    "billing_month": "2027-01",
    "period_start": "2027-01-01 00:00:00",
    "period_end": "2027-02-01 00:00:00",
    "source_document_reference": "test-statement-2027-01",
    "created_at": "2027-02-02 00:00:00",
}
# A USD record converted at a published rate (design §7: 11.70 × 4.4 = 51.48).
_USD = {
    "source_currency": "USD",
    "source_total": "11.70",
    "fx_rate_version_id": _FX_RATE_VERSION_ID,
    "fx_rate_applied": "4.4",
    "reconciled_cost_myr": "51.48",
}


def _public_id(n: int) -> str:
    """The all-zero example uuid, numbered so that the rows of one test differ."""
    return f"00000000-0000-0000-0000-{n:012d}"


def _row_params(overrides: dict[str, object], n: int = 1) -> dict[str, object]:
    """A legal root (December, MYR 5.00, every snapshot 0), with `overrides` on top."""
    params: dict[str, object] = {
        "public_id": _public_id(n),
        "provider_id": _PROVIDER_ID,
        "scope": "AI_MODULE",
        **_DECEMBER,
        "source_currency": "MYR",
        "source_total": "5.00",
        "fx_rate_version_id": None,
        "fx_rate_applied": None,
        "reconciled_cost_myr": "5.00",
        "coverage_method": "PERIOD_AGGREGATE",
        "estimated_event_count": 0,
        "unprocessed_event_count": 0,
        "outbox_unsettled_count": 0,
        "outbox_discarded_count": 0,
        "estimated_source_cost": "0",
        "estimated_cost_myr": "0",
        "supersedes_id": None,
        "reason": None,
        "created_by": _CREATED_BY,
    }
    params.update(overrides)
    return params


def _insert_row(connection: sqlalchemy.Connection, overrides: dict[str, object], n: int = 1) -> int:
    params = _row_params(overrides, n)
    connection.execute(_INSERT_ROW, params)
    return int(connection.execute(_ID_OF, {"public_id": params["public_id"]}).scalar_one())


def _correction(supersedes_id: int, **overrides: object) -> dict[str, object]:
    return {"supersedes_id": supersedes_id, "reason": "corrected statement", **overrides}


# Design §7 "CHECK": illegal rows (each alone in the table) → the check that refuses it.
_ILLEGAL_ROWS: dict[str, tuple[dict[str, object], str]] = {
    "MYR with an fx rate version": ({"fx_rate_version_id": 1}, _check("fx")),
    "MYR with an fx rate": ({"fx_rate_applied": "4.4"}, _check("fx")),
    "MYR converted to another amount": ({"reconciled_cost_myr": "5.01"}, _check("fx")),
    "USD without an fx rate version": ({**_USD, "fx_rate_version_id": None}, _check("fx")),
    "USD without an fx rate": ({**_USD, "fx_rate_applied": None}, _check("fx")),
    "USD at a rate of 0": ({**_USD, "fx_rate_applied": "0"}, _check("fx")),
    "a root with a reason": ({"reason": "no reason for a root"}, _check("reason")),
    "a correction without a reason": ({"supersedes_id": 999}, _check("reason")),
    "another scope": ({"scope": "INTEGRATION"}, _check("scope")),
    "another coverage method": ({"coverage_method": "PER_EVENT"}, _check("coverage_method")),
    "an empty period": ({"period_end": _DECEMBER["period_start"]}, _check("period")),
    "a negative source total": ({**_USD, "source_total": "-1.00"}, _check("source_total")),
    "a negative reconciled cost": (
        {**_USD, "reconciled_cost_myr": "-1.00"},
        _check("reconciled_cost"),
    ),
    "a negative estimated source cost": (
        {"estimated_source_cost": "-0.00000001"},
        _check("estimated_source_cost"),
    ),
    "a negative estimated MYR cost": ({"estimated_cost_myr": "-1"}, _check("estimated_cost_myr")),
    "a negative estimated event count": (
        {"estimated_event_count": -1},
        _check("estimated_event_count"),
    ),
    "a negative unprocessed count": (
        {"unprocessed_event_count": -1},
        _check("unprocessed_event_count"),
    ),
    "a negative unsettled count": (
        {"outbox_unsettled_count": -1},
        _check("outbox_unsettled_count"),
    ),
    "a negative discarded count": (
        {"outbox_discarded_count": -1},
        _check("outbox_discarded_count"),
    ),
}
# Legal rows next to them: the edges the checks let through (each alone in the table).
_LEGAL_ROWS: dict[str, dict[str, object]] = {
    "a MYR root": {},
    "a USD root": dict(_USD),
    "a MYR root of 0": {"source_total": "0", "reconciled_cost_myr": "0"},
    "a USD root of 0": {**_USD, "source_total": "0", "reconciled_cost_myr": "0"},
    "counts and estimates above 0": {
        "estimated_event_count": 2,
        "unprocessed_event_count": 1,
        "outbox_unsettled_count": 1,
        "outbox_discarded_count": 1,
        "estimated_source_cost": "10.50",
        "estimated_cost_myr": "46.80",
    },
}


def _load(path: pathlib.Path = _MIGRATION, name: str = "migration_0041") -> ModuleType:
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


def _expression(statement: str) -> str:
    """The migration's own normalisation of a generated column's expression."""
    return re.sub(r"[\s()`]", "", statement).upper()


def _type_text(column_type: object) -> str:
    """The migration's own normalisation: type and width only."""
    return str(column_type).replace(", ", ",").split(" ")[0].upper()


def _model_type_text(column: sqlalchemy.Column) -> str:
    # The generic `Numeric` compiles to NUMERIC on MySQL, which stores (and reflects) DECIMAL.
    compiled = column.type.compile(dialect=mysql.dialect())
    return _type_text(compiled).replace("NUMERIC", "DECIMAL")


def _model_shape() -> dict[str, object]:
    """The model's columns, generated column, primary key, indexes, foreign keys and checks."""
    table = ProviderCostReconciliation.__table__
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
    generated = {
        c.name: (_expression(str(c.computed.sqltext)), bool(c.computed.persisted))
        for c in table.columns
        if c.computed is not None
    }
    return {
        "columns": {c.name: (_model_type_text(c), bool(c.nullable)) for c in table.columns},
        "generated": generated,
        "primary_key": tuple(c.name for c in table.primary_key.columns),
        "indexes": indexes,
        "foreign_keys": foreign_keys,
        "checks": checks,
    }


def _design_shape() -> dict[str, object]:
    return {
        "columns": _DESIGN_COLUMNS,
        "generated": {"root_slot": (_expression(_DESIGN_ROOT_SLOT), True)},
        "primary_key": _DESIGN_PRIMARY_KEY,
        "indexes": _DESIGN_INDEXES,
        "foreign_keys": _DESIGN_FOREIGN_KEYS,
        "checks": set(_DESIGN_CHECK_TEXTS),
    }


# ---------------------------------------------------------------------------
# No database
# ---------------------------------------------------------------------------


def test_0041_follows_0040() -> None:
    migration = _load()

    assert migration.revision == _REVISION
    assert migration.down_revision == _DOWN_REVISION
    # `alembic_version.version_num` is VARCHAR(32).
    assert len(migration.revision) == 24
    assert len(migration.revision) <= 32


def test_0041_shape_is_the_design_and_the_model() -> None:
    """The migration's frozen literals, the model's declarations and design §2 agree: columns
    (type, width, nullability, in this order), the STORED generated column, the primary key, the
    six indexes in their column order, the four foreign keys RESTRICT, the 13 checks by name."""
    migration = _load()

    assert ProviderCostReconciliation.__tablename__ == _TABLE
    assert migration._TABLE == _TABLE
    assert migration._SHAPE == _design_shape() == _model_shape()
    assert list(migration._SHAPE["columns"]) == list(_DESIGN_COLUMNS)
    assert [c.name for c in ProviderCostReconciliation.__table__.columns] == list(_DESIGN_COLUMNS)
    assert len(_DESIGN_CHECK_TEXTS) == 13
    assert len(_DESIGN_INDEXES) == 6


def test_0041_has_no_metadata_json_and_no_status_column() -> None:
    """Design §2: every field is a structured column; "current" is derived, not stored."""
    names = {c.name for c in ProviderCostReconciliation.__table__.columns}

    assert "metadata_json" not in names
    assert not {name for name in names if "status" in name}


def test_0041_table_args_order_is_the_migrations() -> None:
    """`__table_args__` declares the indexes, foreign keys and checks under the migration's names
    and in its order."""
    migration = _load()
    kinds = UniqueConstraint | sqlalchemy.Index | ForeignKeyConstraint | CheckConstraint
    declared = [
        str(arg.name) for arg in ProviderCostReconciliation.__table_args__ if isinstance(arg, kinds)
    ]

    assert declared == [*migration._INDEXES, *migration._FOREIGN_KEYS, *migration._CHECKS]
    assert list(migration._INDEXES) == list(_DESIGN_INDEXES)
    assert list(migration._FOREIGN_KEYS) == [_FK_PROVIDER, _FK_FX, _FK_CREATED_BY, _FK_SUPERSEDES]


def test_0041_checks_are_the_designs_and_the_models() -> None:
    """Same conditions in the migration, on the model and in the design (whitespace aside)."""
    migration = _load()
    model = {
        str(constraint.name): _normalised(str(constraint.sqltext))
        for constraint in ProviderCostReconciliation.__table__.constraints
        if isinstance(constraint, CheckConstraint)
    }

    assert {name: _normalised(c) for name, c in migration._CHECKS.items()} == model
    assert {name: _normalised(c) for name, c in _DESIGN_CHECK_TEXTS.items()} == model
    assert list(migration._CHECKS) == list(_DESIGN_CHECK_TEXTS)
    assert _normalised(FX_CHECK) == _normalised(_DESIGN_CHECK_TEXTS[_check("fx")])
    assert _normalised(REASON_CHECK) == _normalised(_DESIGN_CHECK_TEXTS[_check("reason")])
    assert _normalised(SCOPE_CHECK) == _normalised(_DESIGN_CHECK_TEXTS[_check("scope")])
    assert _normalised(COVERAGE_METHOD_CHECK) == _normalised(
        _DESIGN_CHECK_TEXTS[_check("coverage_method")]
    )
    assert _normalised(PERIOD_CHECK) == _normalised(_DESIGN_CHECK_TEXTS[_check("period")])


def test_0041_checks_use_no_row_constructor() -> None:
    """Design §2: `IS NOT NULL` column by column, never `(a, b) IS NOT NULL`."""
    migration = _load()
    for name, condition in migration._CHECKS.items():
        assert not re.search(r"\([^()]*,[^()]*\)\s+IS\s", condition, re.IGNORECASE), name


def test_0041_root_slot_is_the_stored_design_expression() -> None:
    migration = _load()
    column = ProviderCostReconciliation.__table__.c.root_slot

    assert column.computed is not None
    assert column.computed.persisted is True
    assert _normalised(str(column.computed.sqltext)) == _normalised(_DESIGN_ROOT_SLOT)
    assert _normalised(ROOT_SLOT_EXPRESSION) == _normalised(_DESIGN_ROOT_SLOT)
    assert _normalised(migration._ROOT_SLOT_EXPRESSION) == _normalised(_DESIGN_ROOT_SLOT)
    assert column.nullable is True


@pytest.mark.parametrize(
    "name",
    [
        "source_total",
        "reconciled_cost_myr",
        "estimated_source_cost",
        "estimated_cost_myr",
    ],
)
def test_0041_amounts_are_decimal_20_8(name: str) -> None:
    """INV-10: every amount is DECIMAL(20,8), read as Decimal."""
    column = ProviderCostReconciliation.__table__.c[name]
    assert isinstance(column.type, sqlalchemy.Numeric)
    assert (column.type.precision, column.type.scale) == (20, 8)
    assert column.type.asdecimal is True


def test_0041_fx_rate_is_decimal_24_10() -> None:
    """INV-10: the rate applied has the precision of `fx_rate_versions.rate`."""
    column = ProviderCostReconciliation.__table__.c.fx_rate_applied
    assert isinstance(column.type, sqlalchemy.Numeric)
    assert (column.type.precision, column.type.scale) == (24, 10)
    assert column.type.asdecimal is True


def test_0041_model_tells_not_to_update_or_delete() -> None:
    doc = ProviderCostReconciliation.__doc__ or ""
    assert "UPDATE" in doc and "DELETE" in doc
    assert "触发器" in doc
    assert "confirm" in doc


def test_0041_mysql_create_table_is_the_design_one() -> None:
    """The `CREATE TABLE` the MySQL upgrade sends is the design's clauses, the generated column,
    the indexes, the foreign keys and the checks included; it is one statement."""
    migration = _load()

    assert _compact(migration._CREATE_TABLE_MYSQL) == _compact(
        f"CREATE TABLE {_TABLE} ({', '.join(_CLAUSES)})"
    )
    assert migration._CREATE_TABLE_MYSQL.count(";") == 0
    assert _normalised(migration._CREATE_TABLE_MYSQL).count("CREATE TABLE") == 1


def test_0041_triggers_are_the_design_ones() -> None:
    """Both triggers: BEFORE UPDATE / BEFORE DELETE on the table, the unconditional body of the
    task contract (BEGIN … END around it)."""
    migration = _load()

    assert _normalised(migration._TRIGGER_BODY) == _normalised(_DESIGN_TRIGGER_BODY)
    assert migration._TRIGGER_BODY == migration._TRIGGER_BODY.strip()
    assert " IF " not in f" {_normalised(migration._TRIGGER_BODY)} "
    assert list(migration._TRIGGERS) == [_UPDATE_TRIGGER, _DELETE_TRIGGER]
    for name, (table, timing, kind) in _DESIGN_TRIGGERS.items():
        assert _compact(migration._TRIGGERS[name]) == _compact(
            f"CREATE TRIGGER {name} {timing} {kind} ON {table} FOR EACH ROW {_DESIGN_TRIGGER_BODY}"
        )


def test_0041_prerequisite_text_is_0039s() -> None:
    """The prerequisite's query, check and wording follow 0039 (the trigger noun aside)."""
    migration = _load()
    earlier = _load(
        pathlib.Path("alembic/versions/20261109_0039_payment_receipts.py"), "migration_0039"
    )

    assert migration._PREREQUISITE_QUERY == earlier._PREREQUISITE_QUERY
    assert migration._PREREQUISITE_ERROR == earlier._PREREQUISITE_ERROR.replace(
        "payment receipt triggers", "provider cost reconciliation triggers"
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


_HAS_ROWS_SQL = f"SELECT 1 FROM {_TABLE} LIMIT 1"
# How MySQL keeps the design's expression in information_schema.COLUMNS.
_MYSQL_ROOT_SLOT = "(case when (`supersedes_id` is null) then 1 end)"


class _FakeBind:
    """Answers what the migration asks: delete rules, generated columns, the triggers, the
    prerequisite variables and "any row?"."""

    def __init__(
        self,
        table: dict | None,
        triggers: dict[str, tuple[str, str, str, str]],
        *,
        has_rows: bool = False,
        log_bin: int = 1,
        trusted: int = 1,
    ) -> None:
        self._table = table
        self._triggers = triggers
        self._has_rows = has_rows
        self._variables = (log_bin, trusted)
        self.queries: list[str] = []

    def execute(self, statement: object) -> _Result:
        sql = str(statement)
        self.queries.append(sql)
        if "REFERENTIAL_CONSTRAINTS" in sql:
            assert f"TABLE_NAME = '{_TABLE}'" in sql, sql
            return _Result(list((self._table or {}).get("delete_rules", {}).items()))
        if "information_schema.COLUMNS" in sql:
            assert f"TABLE_NAME = '{_TABLE}'" in sql, sql
            generated = (self._table or {}).get("generated", {})
            return _Result(
                [
                    (name, expression, "STORED GENERATED" if stored else "VIRTUAL GENERATED")
                    for name, (expression, stored) in generated.items()
                ]
            )
        if "information_schema.TRIGGERS" in sql:
            return _Result([(name, *row) for name, row in self._triggers.items()])
        if "@@log_bin" in sql:
            return _Result([self._variables])
        if sql == _HAS_ROWS_SQL:
            return _Result([(1,)] if self._has_rows else [])
        raise AssertionError(sql)


class _FakeInspector:
    """`Inspector` over the reflected table, when it exists."""

    def __init__(self, table: dict | None) -> None:
        self._table = table
        self.asked: list[str] = []

    def _get(self, key: str, name: str):
        assert name == _TABLE and self._table is not None, name
        self.asked.append(key)
        return copy.deepcopy(self._table[key])

    def has_table(self, name: str) -> bool:
        assert name == _TABLE, name
        self.asked.append("has_table")
        return self._table is not None

    def get_columns(self, name: str) -> list[dict]:
        columns = self._get("columns", name)
        # As the SQLite inspector reflects a generated column.
        for column in columns:
            generated = self._table["generated"].get(column["name"])
            if generated is not None:
                expression, stored = generated
                column["computed"] = {"sqltext": expression, "persisted": stored}
        return columns

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
    "INTEGER": mysql.INTEGER(),
    "CHAR(3)": mysql.CHAR(length=3),
    "CHAR(7)": mysql.CHAR(length=7),
    "CHAR(36)": mysql.CHAR(length=36),
    "VARCHAR(16)": mysql.VARCHAR(length=16),
    "VARCHAR(32)": mysql.VARCHAR(length=32),
    "VARCHAR(255)": mysql.VARCHAR(length=255),
    "DECIMAL(20,8)": mysql.DECIMAL(precision=20, scale=8),
    "DECIMAL(24,10)": mysql.DECIMAL(precision=24, scale=10),
    "DATETIME": mysql.DATETIME(),
}


def _design_table() -> dict:
    """The table the way MySQL's inspector reflects it: unique constraints are also indexes; the
    generated column as information_schema keeps it."""
    return {
        "columns": [
            {
                "name": name,
                "type": _MYSQL_TYPES[kind],
                "nullable": nullable,
                "autoincrement": name == "id",
            }
            for name, (kind, nullable) in _DESIGN_COLUMNS.items()
        ],
        "generated": {"root_slot": (_MYSQL_ROOT_SLOT, True)},
        "primary_key": {"constrained_columns": list(_DESIGN_PRIMARY_KEY), "name": None},
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
        "check_constraints": [{"name": name, "sqltext": "…"} for name in _DESIGN_CHECK_TEXTS],
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
    has_rows: bool = False,
    dialect: str = "mysql",
    as_sql: bool = False,
    trusted: int = 1,
) -> tuple[ModuleType, _FakeOp, _FakeInspector, _FakeBind]:
    migration = _load()
    bind = _FakeBind(table, triggers or {}, has_rows=has_rows, trusted=trusted)
    fake = _FakeOp(bind, dialect=dialect, as_sql=as_sql)
    inspector = _FakeInspector(table)
    monkeypatch.setattr(migration, "op", fake)
    monkeypatch.setattr(migration, "sa", _Sa(inspector))
    return migration, fake, inspector, bind


def _executed(fake: _FakeOp) -> list[str]:
    """Each call as `create table <name>`, `create trigger <name>`, `drop trigger <name>`, or
    `<kind> <name>` for the other `op` calls."""
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
    f"create table {_TABLE}",
    f"create trigger {_UPDATE_TRIGGER}",
    f"create trigger {_DELETE_TRIGGER}",
]


def test_0041_upgrade_builds_everything_in_the_design_order(monkeypatch) -> None:
    migration, fake, _, bind = _with(monkeypatch, None)
    migration.upgrade()

    assert _executed(fake) == _FULL_UPGRADE
    assert [args[0] for _, args in fake.calls] == [
        migration._CREATE_TABLE_MYSQL,
        migration._TRIGGERS[_UPDATE_TRIGGER],
        migration._TRIGGERS[_DELETE_TRIGGER],
    ]
    # The prerequisite check comes right before the first DDL; no row is read.
    assert _prerequisite_asked(bind)
    assert "@@log_bin" in bind.queries[-1]
    assert _HAS_ROWS_SQL not in bind.queries


def test_0041_upgrade_refuses_before_any_ddl_without_the_trigger_privilege(monkeypatch) -> None:
    """binlog on and log_bin_trust_function_creators off: the account could not create the
    triggers (ERROR 1419) — refuse before CREATE TABLE."""
    migration, fake, _, _ = _with(monkeypatch, None, trusted=0)

    with pytest.raises(RuntimeError, match="log_bin_trust_function_creators"):
        migration.upgrade()

    assert fake.calls == []


def test_0041_upgrade_with_everything_built_issues_nothing(monkeypatch) -> None:
    migration = _load()
    migration, fake, inspector, bind = _with(
        monkeypatch, _design_table(), _design_triggers(migration)
    )
    migration.upgrade()

    assert fake.calls == []
    for key in ("columns", "primary_key", "indexes", "foreign_keys", "check_constraints"):
        assert key in inspector.asked
    assert any("information_schema.COLUMNS" in query for query in bind.queries)
    assert any("REFERENTIAL_CONSTRAINTS" in query for query in bind.queries)
    # Nothing to build: the prerequisite is not asked either.
    assert not _prerequisite_asked(bind)


def test_0041_upgrade_on_sqlite_with_everything_built_issues_nothing(monkeypatch) -> None:
    migration, fake, _, bind = _with(monkeypatch, _design_table(), dialect="sqlite")
    migration.upgrade()

    assert fake.calls == []
    assert bind.queries == []


@pytest.mark.parametrize("missing", [_UPDATE_TRIGGER, _DELETE_TRIGGER, "both"])
def test_0041_upgrade_builds_only_the_missing_trigger(monkeypatch, missing: str) -> None:
    """The table built, a trigger not (the interrupted upgrade) — only that one, after the
    prerequisite check."""
    migration = _load()
    triggers = _design_triggers(migration)
    gone = list(triggers) if missing == "both" else [missing]
    for name in gone:
        del triggers[name]
    migration, fake, _, bind = _with(monkeypatch, _design_table(), triggers)
    migration.upgrade()

    assert _executed(fake) == [f"create trigger {name}" for name in gone]
    assert [args[0] for _, args in fake.calls] == [migration._TRIGGERS[name] for name in gone]
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
            "BEGIN IF OLD.reason <> NEW.reason THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT ="
            f" '{_IMMUTABLE}'; END IF; END",
        ),
    ),
    "update trigger AFTER": (_UPDATE_TRIGGER, _trigger_changed(_UPDATE_TRIGGER, 1, "AFTER")),
    "delete trigger on UPDATE": (_DELETE_TRIGGER, _trigger_changed(_DELETE_TRIGGER, 2, "UPDATE")),
    "delete trigger on another table": (
        _DELETE_TRIGGER,
        _trigger_changed(_DELETE_TRIGGER, 0, "payment_receipts"),
    ),
}


@pytest.mark.parametrize("case", list(_WRONG_TRIGGERS))
@pytest.mark.parametrize("other_present", [True, False], ids=["other present", "other missing"])
def test_0041_upgrade_refuses_a_trigger_that_differs_before_any_ddl(
    monkeypatch, case: str, other_present: bool
) -> None:
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
    assert f"trigger {name} exists but is not the one 0041 builds" in message
    assert "No DDL was issued" in message


def test_0041_upgrade_refuses_a_trigger_of_ours_on_another_table_without_the_table(
    monkeypatch,
) -> None:
    migration = _load()
    triggers = {_UPDATE_TRIGGER: ("payment_receipts", "BEFORE", "UPDATE", migration._TRIGGER_BODY)}
    migration, fake, _, _ = _with(monkeypatch, None, triggers)

    with pytest.raises(RuntimeError, match=_UPDATE_TRIGGER):
        migration.upgrade()

    assert fake.calls == []


def _drop_index(name: str):
    def mutate(table: dict) -> None:
        table["indexes"] = [i for i in table["indexes"] if i["name"] != name]
        table["unique_constraints"] = [u for u in table["unique_constraints"] if u["name"] != name]

    return mutate


def _drop_fx_index(table: dict) -> None:
    # What MySQL leaves without it: a hidden index of its own for the foreign key.
    _drop_index(_FX_INDEX)(table)
    table["indexes"].append(
        {"name": _FK_FX, "column_names": ["fx_rate_version_id"], "unique": False}
    )


def _index_columns(name: str, columns: list[str]):
    def mutate(table: dict) -> None:
        for entry in (*table["indexes"], *table["unique_constraints"]):
            if entry["name"] == name:
                entry["column_names"] = list(columns)

    return mutate


def _unique_as_plain(name: str):
    def mutate(table: dict) -> None:
        for index in table["indexes"]:
            if index["name"] == name:
                index["unique"] = False
        table["unique_constraints"] = [u for u in table["unique_constraints"] if u["name"] != name]

    return mutate


def _prefix_length(table: dict) -> None:
    for index in table["indexes"]:
        if index["name"] == _PUBLIC_ID_UNIQUE:
            index["dialect_options"] = {"mysql_length": {"public_id": 8}}


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


def _supersedes_key_on_id_only(table: dict) -> None:
    for key in table["foreign_keys"]:
        if key["name"] == _FK_SUPERSEDES:
            key["constrained_columns"] = ["supersedes_id"]
            key["referred_columns"] = ["id"]


def _drop_check(name: str):
    def mutate(table: dict) -> None:
        table["check_constraints"] = [c for c in table["check_constraints"] if c["name"] != name]

    return mutate


def _extra_column(name: str):
    def mutate(table: dict) -> None:
        table["columns"].append({"name": name, "type": mysql.JSON(), "nullable": True})

    return mutate


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


def _root_slot(expression: str | None, stored: bool = True):
    def mutate(table: dict) -> None:
        if expression is None:
            del table["generated"]["root_slot"]
        else:
            table["generated"]["root_slot"] = (expression, stored)

    return mutate


_WRONG_SHAPES: dict[str, object] = {
    "without the fx rate version index": _drop_fx_index,
    "without the created_by index": _drop_index(_CREATED_BY_INDEX),
    "without the chain key": _drop_index(_CHAIN_KEY_UNIQUE),
    "supersedes unique a plain index": _unique_as_plain(_SUPERSEDES_UNIQUE),
    "root unique without root_slot": _index_columns(
        _ROOT_UNIQUE, ["provider_id", "scope", "billing_month"]
    ),
    "supersedes unique in another order": _index_columns(
        _SUPERSEDES_UNIQUE, ["provider_id", "supersedes_id", "scope", "billing_month"]
    ),
    "a prefix length": _prefix_length,
    "provider foreign key not RESTRICT": _cascade(_FK_PROVIDER),
    "supersedes foreign key not RESTRICT": _cascade(_FK_SUPERSEDES),
    "without the created_by foreign key": _no_foreign_key(_FK_CREATED_BY),
    "supersedes foreign key on id only": _supersedes_key_on_id_only,
    "without the reason check": _drop_check(_check("reason")),
    "without the outbox discarded check": _drop_check(_check("outbox_discarded_count")),
    "with a metadata_json column": _extra_column("metadata_json"),
    "with a status column": _extra_column("status"),
    "reason not nullable": _column("reason", nullable=False),
    "fx_rate_version_id not nullable": _column("fx_rate_version_id", nullable=False),
    "source_total DECIMAL(20,2)": _column("source_total", type=mysql.DECIMAL(20, 2)),
    "fx_rate_applied DECIMAL(20,8)": _column("fx_rate_applied", type=mysql.DECIMAL(20, 8)),
    "scope VARCHAR(32)": _column("scope", type=mysql.VARCHAR(32)),
    "billing_month CHAR(10)": _column("billing_month", type=mysql.CHAR(10)),
    "a count as INTEGER": _column("estimated_event_count", type=mysql.INTEGER()),
    "primary key on public_id": _primary_key("public_id"),
    "root_slot VIRTUAL": _root_slot(_MYSQL_ROOT_SLOT, stored=False),
    "root_slot another expression": _root_slot("(case when (`reason` is null) then 1 end)"),
    "root_slot not generated": _root_slot(None),
}


@pytest.mark.parametrize("shape", list(_WRONG_SHAPES))
@pytest.mark.parametrize("dialect", ["mysql", "sqlite"])
def test_0041_upgrade_refuses_a_wrong_shape_before_any_ddl(
    monkeypatch, shape: str, dialect: str
) -> None:
    """Design §2: the table there, any item missing, extra or different — nothing is built (not
    even a missing trigger); the message names the table."""
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


def test_0041_upgrade_does_not_compare_server_defaults(monkeypatch) -> None:
    """Design §2 / task contract: the shape check does not look at server defaults."""
    table = _design_table()
    _column("estimated_event_count", default="0")(table)
    migration = _load()
    migration, fake, _, _ = _with(monkeypatch, table, _design_triggers(migration))
    migration.upgrade()

    assert fake.calls == []


def test_0041_upgrade_on_sqlite_creates_the_table_without_triggers(monkeypatch) -> None:
    migration, fake, _, bind = _with(monkeypatch, None, dialect="sqlite")
    migration.upgrade()

    assert fake.calls == [
        ("create_table", (_TABLE,)),
        (
            "create_index",
            (_ROOT_UNIQUE, _TABLE, ["provider_id", "scope", "billing_month", "root_slot"], True),
        ),
        ("create_index", (_FX_INDEX, _TABLE, ["fx_rate_version_id"], False)),
        ("create_index", (_CREATED_BY_INDEX, _TABLE, ["created_by"], False)),
    ]
    assert bind.queries == []


def test_0041_offline_upgrade_emits_everything_without_reading(monkeypatch) -> None:
    migration = _load()
    migration, fake, inspector, bind = _with(
        monkeypatch, _design_table(), _design_triggers(migration), as_sql=True
    )
    migration.upgrade()

    assert inspector.asked == []
    assert bind.queries == []
    assert _executed(fake) == _FULL_UPGRADE


def test_0041_offline_downgrade_refuses_before_reading_or_ddl(monkeypatch) -> None:
    """Design §8: offline the emptiness cannot be checked — refuse, no DROP."""
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


def test_0041_downgrade_drops_both_triggers_then_the_empty_table(monkeypatch) -> None:
    migration = _load()
    migration, fake, _, bind = _with(monkeypatch, _design_table(), _design_triggers(migration))
    migration.downgrade()

    assert _HAS_ROWS_SQL in bind.queries
    assert fake.calls == [
        ("execute", (f"DROP TRIGGER {_UPDATE_TRIGGER}",)),
        ("execute", (f"DROP TRIGGER {_DELETE_TRIGGER}",)),
        ("drop_table", (_TABLE,)),
    ]


def test_0041_downgrade_drops_only_the_triggers_that_exist(monkeypatch) -> None:
    migration = _load()
    triggers = _design_triggers(migration)
    del triggers[_UPDATE_TRIGGER]
    migration, fake, _, _ = _with(monkeypatch, _design_table(), triggers)
    migration.downgrade()

    assert fake.calls == [
        ("execute", (f"DROP TRIGGER {_DELETE_TRIGGER}",)),
        ("drop_table", (_TABLE,)),
    ]


def test_0041_downgrade_on_sqlite_drops_the_empty_table(monkeypatch) -> None:
    migration, fake, _, bind = _with(monkeypatch, _design_table(), dialect="sqlite")
    migration.downgrade()

    assert bind.queries == [_HAS_ROWS_SQL]
    assert fake.calls == [("drop_table", (_TABLE,))]


@pytest.mark.parametrize("dialect", ["mysql", "sqlite"])
def test_0041_downgrade_refuses_with_a_row_before_any_ddl(monkeypatch, dialect: str) -> None:
    """Design §8: a record — no DDL, no row deleted; the message names the table, that it is not
    empty, and to roll back only the application image."""
    migration = _load()
    migration, fake, _, _ = _with(
        monkeypatch, _design_table(), _design_triggers(migration), has_rows=True, dialect=dialect
    )

    with pytest.raises(RuntimeError) as raised:
        migration.downgrade()

    assert fake.calls == []
    message = str(raised.value)
    assert f"table {_TABLE} is not empty" in message
    assert "No DDL was issued and no row was deleted" in message
    assert "roll back only the application image" in message


@pytest.mark.parametrize("dialect", ["mysql", "sqlite"])
def test_0041_downgrade_without_the_table_does_nothing(monkeypatch, dialect: str) -> None:
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
    SQLite does not enforce foreign keys here (no `PRAGMA foreign_keys`): the parent tables are
    not needed, and the composite foreign key is MySQL's to test."""
    engine = create_engine("sqlite+pysqlite:///:memory:")
    try:
        Base.metadata.create_all(engine, tables=[ProviderCostReconciliation.__table__])
        yield engine
    finally:
        engine.dispose()


def test_0041_sqlite_table_gets_the_indexes_the_keys_and_the_checks() -> None:
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
        checks = {str(c["name"]) for c in inspector.get_check_constraints(_TABLE)}
        primary_key = tuple(inspector.get_pk_constraint(_TABLE)["constrained_columns"])

    assert indexes == _DESIGN_INDEXES
    assert keys == _DESIGN_FOREIGN_KEYS
    assert checks == set(_DESIGN_CHECK_TEXTS)
    assert primary_key == _DESIGN_PRIMARY_KEY


@pytest.mark.parametrize("row", list(_LEGAL_ROWS))
def test_0041_sqlite_takes_the_legal_rows(row: str) -> None:
    with _sqlite() as engine:
        with engine.begin() as connection:
            row_id = _insert_row(connection, _LEGAL_ROWS[row])
            assert int(connection.execute(_ROW_COUNT).scalar_one()) == 1
            # A root: `root_slot` is 1.
            assert connection.execute(_ROOT_SLOT_OF, {"id": row_id}).scalar_one() == 1


@pytest.mark.parametrize("row", list(_ILLEGAL_ROWS))
def test_0041_sqlite_checks_refuse_the_illegal_rows(row: str) -> None:
    overrides, check = _ILLEGAL_ROWS[row]
    with _sqlite() as engine:
        with pytest.raises(DBAPIError) as raised:
            with engine.begin() as connection:
                _insert_row(connection, overrides)
        message = str(raised.value)
        assert "CHECK constraint failed" in message, row
        assert check in message, row

        with engine.connect() as connection:
            assert int(connection.execute(_ROW_COUNT).scalar_one()) == 0


def test_0041_sqlite_takes_a_chain_and_one_root_a_month() -> None:
    """A root, its correction and the correction's correction: one chain; `root_slot` is 1 for
    the root and NULL for the corrections. Another month has a root of its own."""
    with _sqlite() as engine:
        with engine.begin() as connection:
            root = _insert_row(connection, {}, 1)
            second = _insert_row(connection, _correction(root), 2)
            third = _insert_row(connection, _correction(second), 3)
            january = _insert_row(connection, dict(_JANUARY), 4)

            slots = [
                connection.execute(_ROOT_SLOT_OF, {"id": row_id}).scalar_one()
                for row_id in (root, second, third, january)
            ]
            assert slots == [1, None, None, 1]
            assert int(connection.execute(_ROW_COUNT).scalar_one()) == 4


# Design §7 "根唯一与单链": a second row next to a root → (the unique index, a column it names).
def _second_root(_root: int) -> dict[str, object]:
    return {}


def _second_correction(root: int) -> dict[str, object]:
    return _correction(root, reason="second correction")


_DUPLICATES = {
    "two roots of a provider and month": (_second_root, _ROOT_UNIQUE, "root_slot"),
    "two corrections of one row": (_second_correction, _SUPERSEDES_UNIQUE, "supersedes_id"),
}


@pytest.mark.parametrize("case", list(_DUPLICATES))
def test_0041_sqlite_unique_indexes_refuse_a_second_root_or_correction(case: str) -> None:
    second, _, column = _DUPLICATES[case]
    with _sqlite() as engine:
        with engine.begin() as connection:
            root = _insert_row(connection, {}, 1)
            if case == "two corrections of one row":
                _insert_row(connection, _correction(root), 2)
        with pytest.raises(IntegrityError) as raised:
            with engine.begin() as connection:
                _insert_row(connection, second(root), 3)
        message = str(raised.value)
        assert "UNIQUE constraint failed" in message, case
        assert f"{_TABLE}.{column}" in message, case

        with engine.connect() as connection:
            expected = 2 if case == "two corrections of one row" else 1
            assert int(connection.execute(_ROW_COUNT).scalar_one()) == expected


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
    """`CREATE TABLE PROVIDER_COST_RECONCILIATIONS`, `CREATE TRIGGER TRG_…`, …"""
    return [" ".join(_normalised(s).split(" ")[:3]) for s in _writes(statements)]


def _asked_prerequisite(statements: list[str]) -> bool:
    return any("@@LOG_BIN" in _normalised(s) for s in statements)


def _version(engine: Engine) -> str:
    with engine.connect() as connection:
        return str(connection.execute(_ALEMBIC_VERSION).scalar_one())


def _execute(engine: Engine, statement: str, params: dict[str, object] | None = None) -> None:
    with engine.begin() as connection:
        connection.execute(text(statement), params or {})


def _present(engine: Engine) -> bool:
    return inspect(engine).has_table(_TABLE)


def _truncate(engine: Engine) -> None:
    """The triggers refuse DELETE: TRUNCATE (DDL, no triggers) with foreign key checks off (the
    table refers to itself), as payment_world.clean_receipts. The triggers are never dropped for
    this."""
    with engine.begin() as connection:
        connection.exec_driver_sql("SET FOREIGN_KEY_CHECKS = 0")
        try:
            connection.exec_driver_sql(f"TRUNCATE TABLE {_TABLE}")
        finally:
            connection.exec_driver_sql("SET FOREIGN_KEY_CHECKS = 1")


def _index_names(engine: Engine) -> set[str]:
    """Every index on the table besides the primary key, from information_schema.STATISTICS."""
    with engine.connect() as connection:
        return {
            str(name) for name in connection.execute(_STATISTICS_QUERY, {"table": _TABLE}).scalars()
        }


def _table_shape(engine: Engine) -> dict[str, object]:
    """The table as reflected, in the migration's terms; delete rules and the generated column
    from information_schema; prefix lengths kept so they would show."""
    inspector = inspect(engine)
    with engine.connect() as connection:
        rules = dict(connection.execute(_DELETE_RULES_QUERY, {"table": _TABLE}).all())
        generated = {
            str(name): (_expression(str(expression)), str(extra) == "STORED GENERATED")
            for name, expression, extra in connection.execute(
                _GENERATED_QUERY, {"table": _TABLE}
            ).all()
        }
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
        "generated": generated,
        "primary_key": tuple(inspector.get_pk_constraint(_TABLE)["constrained_columns"]),
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
    """The table reflected = the model's = the design's; exactly the six indexes besides the
    primary key (no hidden index for a foreign key); both triggers with the design's table,
    timing, event and body."""
    migration = _load()
    assert _table_shape(engine) == _model_shape() == _design_shape()
    assert _index_names(engine) == set(_DESIGN_INDEXES)
    assert _our_triggers(engine) == {
        name: (*row, migration._TRIGGER_BODY) for name, row in _DESIGN_TRIGGERS.items()
    }


def _ours(engine: Engine) -> dict[str, object]:
    """The table's shape and rows (when it exists) and our triggers."""
    state: dict[str, object] = {"triggers": _our_triggers(engine)}
    if _present(engine):
        with engine.connect() as connection:
            rows = [tuple(r) for r in connection.execute(_ROWS)]
        state[_TABLE] = (_table_shape(engine), rows)
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
        "tables": {name: shape for name, shape in tables.items() if name != _TABLE},
        "triggers": {trigger for trigger in triggers if trigger[1] != _TABLE},
        "checks": {check for check in checks if check[0] != _TABLE},
    }


def _at_0040(alembic_config: Config, engine: Engine) -> None:
    command.upgrade(alembic_config, "head")
    command.downgrade(alembic_config, _DOWN_REVISION)
    assert _version(engine) == _DOWN_REVISION
    assert not _present(engine)
    assert _our_triggers(engine) == {}


def _at_0041(alembic_config: Config, engine: Engine) -> None:
    # Go to head first and come down, so that a later head does not change what this sees.
    command.upgrade(alembic_config, "head")
    command.downgrade(alembic_config, _REVISION)
    assert _version(engine) == _REVISION
    assert _present(engine)


def _back_to_head(alembic_config: Config, engine: Engine) -> None:
    """Empties the table, puts `alembic_version` back to 0040, drops the table (and with it the
    triggers, hand-made or not) and upgrades to head again: the database ends at head with the
    table empty."""
    if _present(engine):
        _truncate(engine)
    if _version(engine) == _REVISION:
        _execute(engine, _SET_VERSION, {"version": _DOWN_REVISION})
    if _version(engine) == _DOWN_REVISION and _present(engine):
        _execute(engine, f"DROP TABLE {_TABLE}")
    command.upgrade(alembic_config, "head")


def _insert_committed(engine: Engine, rows: list[dict[str, object]]) -> list[int]:
    """Rows written with foreign key checks off (no provider, user or fx rate version is needed
    for this); returns their ids."""
    ids = []
    with engine.begin() as connection:
        connection.exec_driver_sql("SET FOREIGN_KEY_CHECKS = 0")
        try:
            for n, overrides in enumerate(rows, start=1):
                ids.append(_insert_row(connection, overrides, n))
        finally:
            connection.exec_driver_sql("SET FOREIGN_KEY_CHECKS = 1")
    return ids


@needs_mysql
def test_0041_round_trip(alembic_config: Config, engine: Engine) -> None:
    """Design §7 "表结构与触发器": 0040 → 0041 → 0040 → 0041. The upgrade sends the one
    `CREATE TABLE` and the two `CREATE TRIGGER` (after the prerequisite check); the shape is the
    model's, the index set exactly the six, the triggers the design's; the downgrade drops both
    triggers, then the table; every other table, key, check and trigger is the same throughout."""
    _at_0040(alembic_config, engine)
    try:
        at_0040 = _schema_snapshot(engine)

        with _statements() as statements:
            command.upgrade(alembic_config, _REVISION)
        assert _heads(statements) == [
            f"CREATE TABLE {_TABLE.upper()}",
            f"CREATE TRIGGER {_UPDATE_TRIGGER.upper()}",
            f"CREATE TRIGGER {_DELETE_TRIGGER.upper()}",
        ]
        assert _asked_prerequisite(statements)
        assert _version(engine) == _REVISION
        _assert_design_shape(engine)
        with engine.connect() as connection:
            assert int(connection.execute(_ROW_COUNT).scalar_one()) == 0
        at_0041 = _schema_snapshot(engine)
        assert _without_ours(at_0041) == at_0040

        with _statements() as statements:
            command.downgrade(alembic_config, _DOWN_REVISION)
        assert [_normalised(s) for s in _writes(statements)] == [
            f"DROP TRIGGER {_UPDATE_TRIGGER.upper()}",
            f"DROP TRIGGER {_DELETE_TRIGGER.upper()}",
            f"DROP TABLE {_TABLE.upper()}",
        ]
        assert _version(engine) == _DOWN_REVISION
        assert _schema_snapshot(engine) == at_0040

        command.upgrade(alembic_config, _REVISION)
        assert _schema_snapshot(engine) == at_0041
        _assert_design_shape(engine)
    finally:
        _back_to_head(alembic_config, engine)


@needs_mysql
def test_0041_mysql_has_exactly_the_six_indexes(alembic_config: Config, engine: Engine) -> None:
    """Design §2 / §7: information_schema.STATISTICS lists, besides the primary key, exactly
    ux_…_root, uq_…_public_id, uq_…_supersedes, uq_…_chain_key, ix_…_fx_rate_version_id and
    ix_…_created_by — MySQL built no hidden index for a foreign key."""
    _at_0041(alembic_config, engine)

    assert _index_names(engine) == {
        _ROOT_UNIQUE,
        _PUBLIC_ID_UNIQUE,
        _SUPERSEDES_UNIQUE,
        _CHAIN_KEY_UNIQUE,
        _FX_INDEX,
        _CREATED_BY_INDEX,
    }
    assert not _index_names(engine) & set(_DESIGN_FOREIGN_KEYS)


@needs_mysql
def test_0041_rerun_with_everything_built_only_moves_the_version(
    alembic_config: Config, engine: Engine
) -> None:
    """Upgraded, then `alembic_version` put back to 0040 (as if writing the version failed after
    everything was built) — no DDL, no prerequisite check; the version moves to 0041 and the
    schema is as after the first upgrade."""
    _at_0041(alembic_config, engine)
    try:
        after_first = _schema_snapshot(engine)
        _execute(engine, _SET_VERSION, {"version": _DOWN_REVISION})

        with _statements() as statements:
            command.upgrade(alembic_config, _REVISION)

        assert _writes(statements) == []
        assert not _asked_prerequisite(statements)
        assert _version(engine) == _REVISION
        assert _schema_snapshot(engine) == after_first
        _assert_design_shape(engine)
    finally:
        _back_to_head(alembic_config, engine)


@needs_mysql
@pytest.mark.parametrize("missing", [_UPDATE_TRIGGER, _DELETE_TRIGGER, "both"])
def test_0041_rerun_with_the_table_and_no_trigger_builds_only_the_triggers(
    alembic_config: Config, engine: Engine, missing: str
) -> None:
    """Design §5 / §7 "迁移中断": version 0040, the table built and a trigger (or both) dropped —
    the upgrade sends only those `CREATE TRIGGER` (after the prerequisite check); the version is
    0041; all is the design's."""
    migration = _load()
    gone = list(_DESIGN_TRIGGERS) if missing == "both" else [missing]
    _at_0041(alembic_config, engine)
    try:
        for name in gone:
            _execute(engine, f"DROP TRIGGER {name}")
        _execute(engine, _SET_VERSION, {"version": _DOWN_REVISION})

        with _statements() as statements:
            command.upgrade(alembic_config, _REVISION)

        assert [_compact(s) for s in _writes(statements)] == [
            _compact(migration._TRIGGERS[name]) for name in gone
        ]
        assert _asked_prerequisite(statements)
        assert _version(engine) == _REVISION
        _assert_design_shape(engine)
    finally:
        _back_to_head(alembic_config, engine)


def _create(clauses: list[str]) -> str:
    return f"CREATE TABLE {_TABLE} ({', '.join(clauses)})"


def _without(clauses: list[str], prefix: str) -> list[str]:
    kept = [clause for clause in clauses if not clause.startswith(prefix)]
    assert len(kept) == len(clauses) - 1, prefix
    return kept


def _replaced(clauses: list[str], old: str, new: str) -> list[str]:
    assert sum(old in clause for clause in clauses) == 1, old
    return [clause.replace(old, new) for clause in clauses]


# Design §7 "迁移中断与降级" (同名不同形状的表): each a fresh hand-made table at 0040.
_WRONG_SHAPE_DDL: dict[str, str] = {
    # MySQL then builds a hidden index of its own, named after the foreign key.
    "without the fx rate version index": _create(_without(_CLAUSES, f"INDEX {_FX_INDEX} ")),
    "supersedes unique a plain index": _create(
        _replaced(
            _CLAUSES,
            f"CONSTRAINT {_SUPERSEDES_UNIQUE} UNIQUE (",
            f"INDEX {_SUPERSEDES_UNIQUE} (",
        )
    ),
    "root_slot VIRTUAL": _create(
        _replaced(_CLAUSES, f"({_DESIGN_ROOT_SLOT}) STORED", f"({_DESIGN_ROOT_SLOT}) VIRTUAL")
    ),
    "provider foreign key not RESTRICT": _create(
        _replaced(
            _CLAUSES,
            "REFERENCES ai_providers (id) ON DELETE RESTRICT",
            "REFERENCES ai_providers (id) ON DELETE CASCADE",
        )
    ),
    "without the reason check": _create(_without(_CLAUSES, f"CONSTRAINT {_check('reason')} ")),
    "source_total with two decimals": _create(
        _replaced(_CLAUSES, "source_total DECIMAL(20, 8)", "source_total DECIMAL(20, 2)")
    ),
    "with a metadata_json column": _create(
        _replaced(
            _CLAUSES,
            "created_at DATETIME NOT NULL",
            "created_at DATETIME NOT NULL, metadata_json JSON",
        )
    ),
}


@needs_mysql
@pytest.mark.parametrize("shape", list(_WRONG_SHAPE_DDL))
def test_0041_rerun_with_a_wrong_shape_fails_before_any_ddl(
    alembic_config: Config, engine: Engine, shape: str
) -> None:
    """Version 0040 and a fresh hand-made table of the same name — the upgrade fails naming the
    table, issues no DDL (no trigger either), the version stays 0040 and the table is as it
    was."""
    _at_0040(alembic_config, engine)
    try:
        _execute(engine, _WRONG_SHAPE_DDL[shape])
        hand_made = _ours(engine)

        with _statements() as statements:
            with pytest.raises(RuntimeError) as raised:
                command.upgrade(alembic_config, _REVISION)

        assert _writes(statements) == []
        assert not _asked_prerequisite(statements)
        message = str(raised.value)
        assert f"table {_TABLE} exists but differs" in message
        assert "No DDL was issued" in message
        assert "data incident" in message
        assert _version(engine) == _DOWN_REVISION
        assert _ours(engine) == hand_made
    finally:
        _back_to_head(alembic_config, engine)


@needs_mysql
def test_0041_downgrade_with_a_row_refuses_before_any_ddl(
    alembic_config: Config, engine: Engine
) -> None:
    """Design §8: a record — the downgrade fails naming the table, no DDL, the table, its rows and
    the triggers are as they were, `alembic_version` stays 0041."""
    _at_0041(alembic_config, engine)
    try:
        _insert_committed(engine, [{}])
        before_schema = _schema_snapshot(engine)
        before = _ours(engine)

        with _statements() as statements:
            with pytest.raises(RuntimeError) as raised:
                command.downgrade(alembic_config, _DOWN_REVISION)

        assert _writes(statements) == []
        message = str(raised.value)
        assert f"table {_TABLE} is not empty" in message
        assert "roll back only the application image" in message
        assert _version(engine) == _REVISION
        assert _schema_snapshot(engine) == before_schema
        assert _ours(engine) == before
    finally:
        _back_to_head(alembic_config, engine)


@needs_mysql
def test_0041_downgrade_with_the_table_gone_issues_no_ddl(
    alembic_config: Config, engine: Engine
) -> None:
    _at_0041(alembic_config, engine)
    try:
        _execute(engine, f"DROP TABLE {_TABLE}")

        with _statements() as statements:
            command.downgrade(alembic_config, _DOWN_REVISION)

        assert _writes(statements) == []
        assert _version(engine) == _DOWN_REVISION
        assert _our_triggers(engine) == {}
    finally:
        _back_to_head(alembic_config, engine)


@needs_mysql
def test_0041_offline_upgrade_and_downgrade(alembic_config: Config) -> None:
    """Design §8: `--sql` upgrade emits the table and both triggers without reading the database;
    the `--sql` downgrade fails saying it must run online, and its output has no DROP."""
    migration = _load()
    upgrade_buffer = io.StringIO()
    command.upgrade(
        Config("alembic.ini", output_buffer=upgrade_buffer),
        f"{_DOWN_REVISION}:{_REVISION}",
        sql=True,
    )
    emitted = _compact(upgrade_buffer.getvalue())
    expected = [
        _compact(migration._CREATE_TABLE_MYSQL),
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
    assert _TABLE in message
    assert "online" in message
    assert "DROP" not in _normalised(downgrade_buffer.getvalue())


@contextmanager
def _rolled_back(engine: Engine, *, foreign_keys: bool = False) -> Iterator[sqlalchemy.Connection]:
    """A connection whose writes are all rolled back; foreign key checks off unless asked for (no
    provider, user or fx rate version is needed then)."""
    with engine.connect() as connection:
        transaction = connection.begin()
        if not foreign_keys:
            connection.exec_driver_sql("SET FOREIGN_KEY_CHECKS = 0")
        try:
            yield connection
        finally:
            transaction.rollback()
            connection.exec_driver_sql("SET FOREIGN_KEY_CHECKS = 1")


@needs_mysql
@pytest.mark.parametrize("row", list(_LEGAL_ROWS))
def test_0041_mysql_takes_the_legal_rows(alembic_config: Config, engine: Engine, row: str) -> None:
    _at_0041(alembic_config, engine)
    with _rolled_back(engine) as connection:
        row_id = _insert_row(connection, _LEGAL_ROWS[row])
        assert int(connection.execute(_ROW_COUNT).scalar_one()) == 1
        assert connection.execute(_ROOT_SLOT_OF, {"id": row_id}).scalar_one() == 1


@needs_mysql
@pytest.mark.parametrize("row", list(_ILLEGAL_ROWS))
def test_0041_mysql_checks_refuse_the_illegal_rows(
    alembic_config: Config, engine: Engine, row: str
) -> None:
    """Design §7 "CHECK": each illegal row on MySQL is refused (3819, the check named). Everything
    is rolled back."""
    _at_0041(alembic_config, engine)
    overrides, check = _ILLEGAL_ROWS[row]
    with _rolled_back(engine) as connection:
        with pytest.raises(DBAPIError) as raised:
            with connection.begin_nested():
                _insert_row(connection, overrides)
        assert int(raised.value.orig.args[0]) == _ER_CHECK_CONSTRAINT_VIOLATED, row
        assert check in str(raised.value.orig.args[1]), row
        assert int(connection.execute(_ROW_COUNT).scalar_one()) == 0


@needs_mysql
@pytest.mark.parametrize("case", list(_DUPLICATES))
def test_0041_mysql_unique_indexes_refuse_a_second_root_or_correction(
    alembic_config: Config, engine: Engine, case: str
) -> None:
    """Design §7 "根唯一与单链": two roots of one provider and month — 1062 on ux_…_root; two
    rows superseding one row — 1062 on uq_…_supersedes. Everything is rolled back."""
    _at_0041(alembic_config, engine)
    second, unique, _ = _DUPLICATES[case]
    with _rolled_back(engine) as connection:
        root = _insert_row(connection, {}, 1)
        if case == "two corrections of one row":
            _insert_row(connection, _correction(root), 2)
        before = int(connection.execute(_ROW_COUNT).scalar_one())
        with pytest.raises(IntegrityError) as raised:
            with connection.begin_nested():
                _insert_row(connection, second(root), 3)
        assert int(raised.value.orig.args[0]) == _ER_DUP_ENTRY, case
        assert unique in str(raised.value.orig.args[1]), case
        assert int(connection.execute(_ROW_COUNT).scalar_one()) == before


@needs_mysql
def test_0041_mysql_takes_a_chain_and_one_root_a_month(
    alembic_config: Config, engine: Engine
) -> None:
    """A root, its correction and the correction's correction, and another month's root: legal;
    `root_slot` is 1 for each root and NULL for the corrections."""
    _at_0041(alembic_config, engine)
    with _rolled_back(engine) as connection:
        root = _insert_row(connection, {}, 1)
        second = _insert_row(connection, _correction(root), 2)
        third = _insert_row(connection, _correction(second), 3)
        january = _insert_row(connection, dict(_JANUARY), 4)

        slots = [
            connection.execute(_ROOT_SLOT_OF, {"id": row_id}).scalar_one()
            for row_id in (root, second, third, january)
        ]
        assert slots == [1, None, None, 1]


# The parents the composite foreign key test needs (foreign key checks on), rolled back with it.
_INSERT_PROVIDER = text(
    "INSERT INTO ai_providers (public_id, code, display_name, status, created_at, updated_at)"
    " VALUES (:public_id, :code, :display_name, 'ACTIVE', :at, :at)"
)
_PROVIDER_ID_OF = text("SELECT id FROM ai_providers WHERE public_id = :public_id")
_INSERT_USER = text(
    "INSERT INTO users (email, password_hash, role, status, failed_login_count, lockout_level,"
    " password_version, created_at, updated_at)"
    " VALUES (:email, 'not-a-hash', 'ADMIN', 'ACTIVE', 0, 0, 0, :at, :at)"
)
_USER_ID_OF = text("SELECT id FROM users WHERE email = :email")
_PARENT_PUBLIC_ID = "00000000-0000-0000-0000-000000000000"
_PARENT_AT = "2026-11-17 00:00:00"


@needs_mysql
def test_0041_mysql_composite_foreign_key_refuses_a_correction_of_another_month(
    alembic_config: Config, engine: Engine
) -> None:
    """Design §7 "根唯一与单链" (MySQL only: the backend tests' SQLite does not enforce foreign
    keys): with foreign key checks on, a correction whose month differs from the row it
    supersedes fails on fk_…_supersedes (1452); the same correction in the root's month is
    taken. Everything is rolled back."""
    _at_0041(alembic_config, engine)
    with _rolled_back(engine, foreign_keys=True) as connection:
        connection.execute(
            _INSERT_PROVIDER,
            {
                "public_id": _PARENT_PUBLIC_ID,
                "code": "provider-cost-migration-test",
                "display_name": "Provider cost migration test",
                "at": _PARENT_AT,
            },
        )
        provider_id = int(
            connection.execute(_PROVIDER_ID_OF, {"public_id": _PARENT_PUBLIC_ID}).scalar_one()
        )
        email = "provider-cost-migration@example.test"
        connection.execute(_INSERT_USER, {"email": email, "at": _PARENT_AT})
        user_id = int(connection.execute(_USER_ID_OF, {"email": email}).scalar_one())
        parents = {"provider_id": provider_id, "created_by": user_id}

        root = _insert_row(connection, parents, 1)
        with pytest.raises(IntegrityError) as raised:
            with connection.begin_nested():
                _insert_row(connection, {**parents, **_JANUARY, **_correction(root)}, 2)
        assert int(raised.value.orig.args[0]) == _ER_NO_REFERENCED_ROW_2
        assert _FK_SUPERSEDES in str(raised.value.orig.args[1])

        _insert_row(connection, {**parents, **_correction(root)}, 3)
        # Another month has a root of its own.
        _insert_row(connection, {**parents, **_JANUARY}, 4)
        assert int(connection.execute(_ROW_COUNT).scalar_one()) == 3


# Design §2 / §4 / §6: an UPDATE, whatever it changes (nothing included), and the DELETE.
_UPDATES = {
    "source_total": (
        f"UPDATE {_TABLE} SET source_total = 6.00, reconciled_cost_myr = 6.00 WHERE id = :id"
    ),
    "reason": f"UPDATE {_TABLE} SET reason = 'edited' WHERE id = :id",
    "nothing changed": f"UPDATE {_TABLE} SET scope = scope WHERE id = :id",
}
_DELETE = f"DELETE FROM {_TABLE} WHERE id = :id"


def _assert_immutable(engine: Engine, statement: str, row_id: int) -> None:
    with pytest.raises(DBAPIError) as raised:
        _execute(engine, statement, {"id": row_id})
    # 1644 is ER_SIGNAL_EXCEPTION: an unhandled SIGNAL SQLSTATE '45000'.
    assert int(raised.value.orig.args[0]) == _ER_SIGNAL_EXCEPTION, statement
    assert _IMMUTABLE in str(raised.value.orig.args[1]), statement


@needs_mysql
def test_0041_triggers_refuse_update_and_delete_of_a_row(
    alembic_config: Config, engine: Engine
) -> None:
    """Design §7 "表结构与触发器": an UPDATE of a row and its DELETE fail with SQLSTATE 45000;
    the rows are unchanged."""
    _at_0041(alembic_config, engine)
    try:
        row_ids = _insert_committed(engine, [{}, dict(_JANUARY)])
        before = _ours(engine)

        for row_id in row_ids:
            for update in _UPDATES.values():
                _assert_immutable(engine, update, row_id)
            _assert_immutable(engine, _DELETE, row_id)

        assert _ours(engine) == before
    finally:
        _truncate(engine)

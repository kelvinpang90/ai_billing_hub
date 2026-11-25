"""0042_usage_rebills（AIH-TASK-253，设计闸门 #830 v12）.

Design §2 "数据库（迁移 0042，AIH-TASK-253）", §3 INV-5 / INV-6 / INV-10, §7 (the "只追加" and
"降级保护" rows), §8, §11 (AIH-TASK-253).

The first cases need no database: the revision chain; the columns, the primary key, the indexes,
the foreign keys and the checks of both tables in the migration equal the models' and the
design's, under the same names and in the same order; every foreign key has an index with the
same leading column; the two `CREATE TABLE` and the four `CREATE TRIGGER` the MySQL upgrade sends;
every branch of the upgrade (nothing yet / everything built / only `usage_rebills` built / a
trigger missing / a trigger that differs / each wrong shape / the prerequisite check / `--sql`) and
of the downgrade (both empty / a row in either / `--sql` / only one table left, with and without a
row / both gone), with `alembic.op` and the inspector stood in. `create_all` on SQLite (what the
unit tests run) builds both tables; their checks and unique keys refuse every illegal row, the
reason check included (empty, spaces only, tabs only, line feeds and carriage returns), and a
reason of only no-break spaces is taken (the known edge, design §2). There is no check for
"difference = corrected − previous" (design v9): AIH-TASK-255's service keeps it.

On a real MySQL (`BILLING_TEST_DATABASE_URL`; skipped when unset, never skipped in CI): the
0041 → 0042 → 0041 → 0042 round trip (every other table, key, check and trigger unchanged; the
downgrade is one `DROP TABLE` naming both tables), the index sets read from
information_schema.STATISTICS (exactly the migration's besides the primary key: no hidden index
for a foreign key), the rerun with everything built, with only `usage_rebills` (the upgrade cut
between the two `CREATE TABLE`), with a trigger missing, wrong shapes, the downgrade with rows,
with only one table left (with and without a row) and with both gone, the `--sql` upgrade and
downgrade, the checks on the same illegal rows, and the triggers: an UPDATE and a DELETE of a row
of either table fail with SQLSTATE 45000. The Alembic chain does not run on SQLite (0006 onwards
ask for `@@log_bin`), so every migration scenario is MySQL's, as in
test_provider_cost_reconciliations_migration.py. Captured statements are compared after
normalising (backticks, case, spaces). Rows the tests commit are removed with TRUNCATE with
foreign key checks off (the triggers refuse DELETE; they are never dropped to clean up). No clock
is read: every time is a constant.
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
from alembic.script import ScriptDirectory
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
from app.models.usage_rebills import (
    DIFFERENCE_CHECK,
    EVENT_COUNT_CHECK,
    REASON_CHECK,
    UsageRebill,
    UsageRebillLine,
)

TEST_DATABASE_URL = os.environ.get("BILLING_TEST_DATABASE_URL", "")

needs_mysql = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="BILLING_TEST_DATABASE_URL is not set; migration tests need a real MySQL",
)

_REVISION = "0042_usage_rebills"
_DOWN_REVISION = "0041_provider_cost_recon"
_MIGRATION = pathlib.Path("alembic/versions/20261124_0042_usage_rebills.py")
_REBILLS = "usage_rebills"
_LINES = "usage_rebill_lines"
_TABLES = (_REBILLS, _LINES)
_MODELS = {_REBILLS: UsageRebill, _LINES: UsageRebillLine}

# Design §2 "数据库": the columns of each table, in order. Nullable are exactly the four the design
# names.
_DESIGN_COLUMNS: dict[str, dict[str, tuple[str, bool]]] = {
    _REBILLS: {
        "id": ("BIGINT", False),
        "public_id": ("CHAR(36)", False),
        "tenant_id": ("BIGINT", False),
        "idempotency_key": ("CHAR(36)", False),
        "request_fingerprint": ("CHAR(64)", False),
        "corrective_pricing_rule_id": ("BIGINT", True),
        "corrective_provider_price_version_id": ("BIGINT", True),
        "event_count": ("INTEGER", False),
        "previous_total": ("DECIMAL(20,8)", False),
        "corrected_total": ("DECIMAL(20,8)", False),
        "difference": ("DECIMAL(20,8)", False),
        "wallet_transaction_id": ("BIGINT", False),
        "reason": ("VARCHAR(255)", False),
        "created_by": ("BIGINT", False),
        "created_at": ("DATETIME", False),
    },
    _LINES: {
        "id": ("BIGINT", False),
        "rebill_id": ("BIGINT", False),
        "usage_event_id": ("BIGINT", False),
        "original_wallet_transaction_id": ("BIGINT", True),
        "previous_charge": ("DECIMAL(20,8)", False),
        "corrected_charge": ("DECIMAL(20,8)", False),
        "difference": ("DECIMAL(20,8)", False),
        "previous_pricing_rule_id": ("BIGINT", False),
        "previous_provider_price_version_id": ("BIGINT", False),
        "corrected_pricing_rule_id": ("BIGINT", False),
        "corrected_provider_price_version_id": ("BIGINT", False),
        "corrected_estimated_provider_cost_myr": ("DECIMAL(20,8)", False),
        "original_period": ("CHAR(7)", False),
        "original_statement_id": ("BIGINT", True),
        "created_at": ("DATETIME", False),
    },
}
_DESIGN_NULLABLE = {
    (_REBILLS, "corrective_pricing_rule_id"),
    (_REBILLS, "corrective_provider_price_version_id"),
    (_LINES, "original_wallet_transaction_id"),
    (_LINES, "original_statement_id"),
}
_DESIGN_PRIMARY_KEY = ("id",)

_PUBLIC_ID_UNIQUE = "uq_usage_rebills_public_id"
_IDEMPOTENCY_UNIQUE = "uq_usage_rebills_idempotency_key"
_WALLET_TRANSACTION_UNIQUE = "uq_usage_rebills_wallet_transaction_id"
_TENANT_INDEX = "ix_usage_rebills_tenant"
_REBILL_EVENT_UNIQUE = "uq_usage_rebill_lines_rebill_event"
_USAGE_EVENT_INDEX = "ix_usage_rebill_lines_usage_event"
_STATEMENT_INDEX = "ix_usage_rebill_lines_original_statement_id"
_DESIGN_INDEXES: dict[str, dict[str, tuple[tuple[str, ...], bool]]] = {
    _REBILLS: {
        _PUBLIC_ID_UNIQUE: (("public_id",), True),
        _IDEMPOTENCY_UNIQUE: (("idempotency_key",), True),
        _WALLET_TRANSACTION_UNIQUE: (("wallet_transaction_id",), True),
        _TENANT_INDEX: (("tenant_id", "id"), False),
        "ix_usage_rebills_corrective_pricing_rule_id": (("corrective_pricing_rule_id",), False),
        "ix_usage_rebills_corrective_provider_price_version_id": (
            ("corrective_provider_price_version_id",),
            False,
        ),
        "ix_usage_rebills_created_by": (("created_by",), False),
    },
    _LINES: {
        _REBILL_EVENT_UNIQUE: (("rebill_id", "usage_event_id"), True),
        _USAGE_EVENT_INDEX: (("usage_event_id", "id"), False),
        "ix_usage_rebill_lines_original_wallet_transaction_id": (
            ("original_wallet_transaction_id",),
            False,
        ),
        "ix_usage_rebill_lines_previous_pricing_rule_id": (("previous_pricing_rule_id",), False),
        "ix_usage_rebill_lines_previous_provider_price_version_id": (
            ("previous_provider_price_version_id",),
            False,
        ),
        "ix_usage_rebill_lines_corrected_pricing_rule_id": (("corrected_pricing_rule_id",), False),
        "ix_usage_rebill_lines_corrected_provider_price_version_id": (
            ("corrected_provider_price_version_id",),
            False,
        ),
        _STATEMENT_INDEX: (("original_statement_id",), False),
    },
}

# Design §2: every foreign key RESTRICT, named fk_<table>_<column>.
_REFERENCES: dict[str, dict[str, str]] = {
    _REBILLS: {
        "tenant_id": "tenants",
        "corrective_pricing_rule_id": "pricing_rules",
        "corrective_provider_price_version_id": "provider_price_versions",
        "wallet_transaction_id": "wallet_transactions",
        "created_by": "users",
    },
    _LINES: {
        "rebill_id": _REBILLS,
        "usage_event_id": "usage_events",
        "original_wallet_transaction_id": "wallet_transactions",
        "previous_pricing_rule_id": "pricing_rules",
        "previous_provider_price_version_id": "provider_price_versions",
        "corrected_pricing_rule_id": "pricing_rules",
        "corrected_provider_price_version_id": "provider_price_versions",
        "original_statement_id": "monthly_statements",
    },
}
_DESIGN_FOREIGN_KEYS: dict[str, dict[str, tuple[tuple[str, ...], str, tuple[str, ...], str]]] = {
    table: {
        f"fk_{table}_{column}": ((column,), referred, ("id",), "RESTRICT")
        for column, referred in references.items()
    }
    for table, references in _REFERENCES.items()
}
_FK_TENANT = "fk_usage_rebills_tenant_id"
_FK_REBILL = "fk_usage_rebill_lines_rebill_id"
_FK_STATEMENT = "fk_usage_rebill_lines_original_statement_id"

# Design §2, word for word (the reason check as v12 has it).
_DESIGN_REASON = (
    "TRIM(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(reason, CHAR(9), ' '), CHAR(10), ' '),"
    " CHAR(11), ' '), CHAR(12), ' '), CHAR(13), ' ')) <> ''"
)
_DESIGN_CHECK_TEXTS: dict[str, dict[str, str]] = {
    _REBILLS: {
        "ck_usage_rebills_event_count": "event_count BETWEEN 1 AND 1000",
        "ck_usage_rebills_previous_total": "previous_total >= 0",
        "ck_usage_rebills_corrected_total": "corrected_total >= 0",
        "ck_usage_rebills_difference_nonzero": "difference <> 0",
        "ck_usage_rebills_reason": _DESIGN_REASON,
    },
    _LINES: {
        "ck_usage_rebill_lines_previous_charge": "previous_charge >= 0",
        "ck_usage_rebill_lines_corrected_charge": "corrected_charge >= 0",
        "ck_usage_rebill_lines_estimated_cost": "corrected_estimated_provider_cost_myr >= 0",
        "ck_usage_rebill_lines_difference_nonzero": "difference <> 0",
    },
}

# The task contract's trigger bodies: unconditional.
_IMMUTABLE = {table: f"{table} rows are immutable" for table in _TABLES}
_DESIGN_TRIGGER_BODIES = {
    table: f"BEGIN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = '{_IMMUTABLE[table]}'; END"
    for table in _TABLES
}
_REBILLS_UPDATE = "trg_usage_rebills_before_update"
_REBILLS_DELETE = "trg_usage_rebills_before_delete"
_LINES_UPDATE = "trg_usage_rebill_lines_before_update"
_LINES_DELETE = "trg_usage_rebill_lines_before_delete"
_DESIGN_TRIGGERS = {
    _REBILLS_UPDATE: (_REBILLS, "BEFORE", "UPDATE"),
    _REBILLS_DELETE: (_REBILLS, "BEFORE", "DELETE"),
    _LINES_UPDATE: (_LINES, "BEFORE", "UPDATE"),
    _LINES_DELETE: (_LINES, "BEFORE", "DELETE"),
}

# Design §8, word for word.
_NOT_EMPTY = (
    "usage_rebills is not empty: downgrading would delete rebill records (spec §19);"
    " restore from backup or keep this revision"
)


def _clauses(table: str, column_clauses: list[str]) -> list[str]:
    """The design's MySQL table, clause by clause; the wrong shapes below are edits of it."""
    indexes = _DESIGN_INDEXES[table]
    return [
        *column_clauses,
        "PRIMARY KEY (id)",
        *(
            f"CONSTRAINT {name} UNIQUE ({', '.join(columns)})"
            for name, (columns, unique) in indexes.items()
            if unique
        ),
        *(
            f"INDEX {name} ({', '.join(columns)})"
            for name, (columns, unique) in indexes.items()
            if not unique
        ),
        *(
            f"CONSTRAINT {name} FOREIGN KEY ({key[0][0]}) REFERENCES {key[1]} (id)"
            " ON DELETE RESTRICT"
            for name, key in _DESIGN_FOREIGN_KEYS[table].items()
        ),
        *(
            f"CONSTRAINT {name} CHECK ({condition})"
            for name, condition in _DESIGN_CHECK_TEXTS[table].items()
        ),
    ]


_CLAUSES = {
    _REBILLS: _clauses(
        _REBILLS,
        [
            "id BIGINT NOT NULL AUTO_INCREMENT",
            "public_id CHAR(36) NOT NULL",
            "tenant_id BIGINT NOT NULL",
            "idempotency_key CHAR(36) NOT NULL",
            "request_fingerprint CHAR(64) NOT NULL",
            "corrective_pricing_rule_id BIGINT NULL",
            "corrective_provider_price_version_id BIGINT NULL",
            "event_count INT NOT NULL",
            "previous_total DECIMAL(20, 8) NOT NULL",
            "corrected_total DECIMAL(20, 8) NOT NULL",
            "difference DECIMAL(20, 8) NOT NULL",
            "wallet_transaction_id BIGINT NOT NULL",
            "reason VARCHAR(255) NOT NULL",
            "created_by BIGINT NOT NULL",
            "created_at DATETIME NOT NULL",
        ],
    ),
    _LINES: _clauses(
        _LINES,
        [
            "id BIGINT NOT NULL AUTO_INCREMENT",
            "rebill_id BIGINT NOT NULL",
            "usage_event_id BIGINT NOT NULL",
            "original_wallet_transaction_id BIGINT NULL",
            "previous_charge DECIMAL(20, 8) NOT NULL",
            "corrected_charge DECIMAL(20, 8) NOT NULL",
            "difference DECIMAL(20, 8) NOT NULL",
            "previous_pricing_rule_id BIGINT NOT NULL",
            "previous_provider_price_version_id BIGINT NOT NULL",
            "corrected_pricing_rule_id BIGINT NOT NULL",
            "corrected_provider_price_version_id BIGINT NOT NULL",
            "corrected_estimated_provider_cost_myr DECIMAL(20, 8) NOT NULL",
            "original_period CHAR(7) NOT NULL",
            "original_statement_id BIGINT NULL",
            "created_at DATETIME NOT NULL",
        ],
    ),
}

# MySQL reports a violated CHECK as 3819 (ER_CHECK_CONSTRAINT_VIOLATED), a duplicate key as 1062
# (ER_DUP_ENTRY), a SIGNAL with SQLSTATE '45000' as 1644 (ER_SIGNAL_EXCEPTION).
_ER_CHECK_CONSTRAINT_VIOLATED = 3819
_ER_DUP_ENTRY = 1062
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
_STATISTICS_QUERY = text(
    "SELECT DISTINCT INDEX_NAME FROM information_schema.STATISTICS"
    " WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :table AND INDEX_NAME <> 'PRIMARY'"
)

# Raw SQL: the values go straight to the database's checks. `id` is the database's.
_INSERT_COLUMNS = {
    table: [name for name in columns if name != "id"] for table, columns in _DESIGN_COLUMNS.items()
}
_INSERTS = {
    table: text(
        f"INSERT INTO {table} ({', '.join(columns)})"
        f" VALUES ({', '.join(f':{name}' for name in columns)})"
    )
    for table, columns in _INSERT_COLUMNS.items()
}
_REBILL_ID_OF = text(f"SELECT id FROM {_REBILLS} WHERE public_id = :public_id")
_LINE_ID_OF = text(
    f"SELECT id FROM {_LINES} WHERE rebill_id = :rebill_id AND usage_event_id = :usage_event_id"
)


def _count(table: str) -> sqlalchemy.TextClause:
    return text(f"SELECT COUNT(*) FROM {table}")


def _rows(table: str) -> sqlalchemy.TextClause:
    return text(f"SELECT * FROM {table} ORDER BY id")


_CREATED_AT = "2026-11-24 00:00:00"


def _uuid(n: int) -> str:
    """The all-zero example uuid, numbered so that the rows of one test differ."""
    return f"00000000-0000-0000-0000-{n:012d}"


def _rebill_params(overrides: dict[str, object], n: int = 1) -> dict[str, object]:
    """A legal rebill (a refund of 0.60 over three events), with `overrides` on top. The parents
    are ids only: SQLite does not enforce foreign keys here, MySQL runs with the checks off."""
    params: dict[str, object] = {
        "public_id": _uuid(n),
        "tenant_id": 1,
        "idempotency_key": _uuid(n),
        "request_fingerprint": "0" * 64,
        "corrective_pricing_rule_id": 2,
        "corrective_provider_price_version_id": None,
        "event_count": 3,
        "previous_total": "3.00",
        "corrected_total": "2.40",
        "difference": "-0.60",
        "wallet_transaction_id": n,
        "reason": "pricing rule multiplier corrected",
        "created_by": 1,
        "created_at": _CREATED_AT,
    }
    params.update(overrides)
    return params


def _line_params(overrides: dict[str, object], n: int = 1) -> dict[str, object]:
    """A legal line (1.50 → 1.20), with `overrides` on top."""
    params: dict[str, object] = {
        "rebill_id": 1,
        "usage_event_id": n,
        "original_wallet_transaction_id": 10 + n,
        "previous_charge": "1.50",
        "corrected_charge": "1.20",
        "difference": "-0.30",
        "previous_pricing_rule_id": 1,
        "previous_provider_price_version_id": 1,
        "corrected_pricing_rule_id": 2,
        "corrected_provider_price_version_id": 1,
        "corrected_estimated_provider_cost_myr": "0.50",
        "original_period": "2026-10",
        "original_statement_id": None,
        "created_at": _CREATED_AT,
    }
    params.update(overrides)
    return params


def _insert_rebill(
    connection: sqlalchemy.Connection, overrides: dict[str, object], n: int = 1
) -> int:
    params = _rebill_params(overrides, n)
    connection.execute(_INSERTS[_REBILLS], params)
    return int(connection.execute(_REBILL_ID_OF, {"public_id": params["public_id"]}).scalar_one())


def _insert_line(
    connection: sqlalchemy.Connection, overrides: dict[str, object], n: int = 1
) -> int:
    params = _line_params(overrides, n)
    connection.execute(_INSERTS[_LINES], params)
    keys = {"rebill_id": params["rebill_id"], "usage_event_id": params["usage_event_id"]}
    return int(connection.execute(_LINE_ID_OF, keys).scalar_one())


def _insert(
    connection: sqlalchemy.Connection, table: str, overrides: dict[str, object], n: int = 1
) -> int:
    if table == _REBILLS:
        return _insert_rebill(connection, overrides, n)
    return _insert_line(connection, overrides, n)


def _check(table: str, name: str) -> str:
    return f"ck_{table}_{name}"


# Design §7 "只追加": illegal rows (each alone in its table) → the check that refuses it.
_ILLEGAL_ROWS: dict[str, tuple[str, dict[str, object], str]] = {
    "a rebill with a difference of 0": (
        _REBILLS,
        {"corrected_total": "3.00", "difference": "0"},
        _check(_REBILLS, "difference_nonzero"),
    ),
    "a rebill of 0 events": (_REBILLS, {"event_count": 0}, _check(_REBILLS, "event_count")),
    "a rebill of 1001 events": (_REBILLS, {"event_count": 1001}, _check(_REBILLS, "event_count")),
    "a negative previous total": (
        _REBILLS,
        {"previous_total": "-0.00000001"},
        _check(_REBILLS, "previous_total"),
    ),
    "a negative corrected total": (
        _REBILLS,
        {"corrected_total": "-1.00"},
        _check(_REBILLS, "corrected_total"),
    ),
    "a line with a difference of 0": (
        _LINES,
        {"corrected_charge": "1.50", "difference": "0"},
        _check(_LINES, "difference_nonzero"),
    ),
    "a negative previous charge": (
        _LINES,
        {"previous_charge": "-1.50"},
        _check(_LINES, "previous_charge"),
    ),
    "a negative corrected charge": (
        _LINES,
        {"corrected_charge": "-0.00000001"},
        _check(_LINES, "corrected_charge"),
    ),
    "a negative estimated cost": (
        _LINES,
        {"corrected_estimated_provider_cost_myr": "-0.50"},
        _check(_LINES, "estimated_cost"),
    ),
}
# Design §2 / §7: reasons made of ASCII blanks only — refused by ck_usage_rebills_reason.
_BLANK_REASONS = {
    "empty": "",
    "one space": " ",
    "spaces": "     ",
    "one tab": "\t",
    "tabs": "\t\t\t",
    "a line feed": "\n",
    "a carriage return": "\r",
    "CR LF": "\r\n",
    "line feeds and carriage returns": "\n\r\n\r",
    "line feeds, carriage returns, tabs and spaces": " \r\n\t \n",
    "a vertical tab": "\x0b",
    "a form feed": "\x0c",
}
for _label, _reason in _BLANK_REASONS.items():
    _ILLEGAL_ROWS[f"a reason of {_label}"] = (
        _REBILLS,
        {"reason": _reason},
        _check(_REBILLS, "reason"),
    )

# Legal rows next to them: the edges the checks let through (each alone in its table).
_LEGAL_ROWS: dict[str, tuple[str, dict[str, object]]] = {
    "a refund": (_REBILLS, {}),
    "a charge": (
        _REBILLS,
        {"corrected_total": "3.60", "difference": "0.60", "corrective_pricing_rule_id": None},
    ),
    "a rebill of 1 event": (_REBILLS, {"event_count": 1}),
    "a rebill of 1000 events": (_REBILLS, {"event_count": 1000}),
    "totals of 0 on one side": (
        _REBILLS,
        {"previous_total": "0", "corrected_total": "0.60", "difference": "0.60"},
    ),
    "a reason with blanks around it": (_REBILLS, {"reason": "\t fixed rule \r\n"}),
    "a line": (_LINES, {}),
    "a line from a charge of 0": (
        _LINES,
        {
            "original_wallet_transaction_id": None,
            "previous_charge": "0",
            "corrected_charge": "0.40",
            "difference": "0.40",
            "original_statement_id": 1,
        },
    ),
    "a line down to 0": (_LINES, {"corrected_charge": "0", "difference": "-1.50"}),
}
# The known edge (design §2): only no-break spaces — the database takes it; the confirm request's
# validation refuses it (AIH-TASK-256).
_NO_BREAK_SPACES = "  "


def _load(path: pathlib.Path = _MIGRATION, name: str = "migration_0042") -> ModuleType:
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
    """The migration's own normalisation: type and width only."""
    return str(column_type).replace(", ", ",").split(" ")[0].upper()


def _model_type_text(column: sqlalchemy.Column) -> str:
    # The generic `Numeric` compiles to NUMERIC on MySQL, which stores (and reflects) DECIMAL.
    compiled = column.type.compile(dialect=mysql.dialect())
    return _type_text(compiled).replace("NUMERIC", "DECIMAL")


def _model_shape(table_name: str) -> dict[str, object]:
    """The model's columns, primary key, indexes, foreign keys and checks."""
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
        "primary_key": _DESIGN_PRIMARY_KEY,
        "indexes": _DESIGN_INDEXES[table],
        "foreign_keys": _DESIGN_FOREIGN_KEYS[table],
        "checks": set(_DESIGN_CHECK_TEXTS[table]),
    }


# ---------------------------------------------------------------------------
# No database
# ---------------------------------------------------------------------------


def test_0042_follows_0041_and_is_the_head() -> None:
    migration = _load()

    assert migration.revision == _REVISION
    assert migration.down_revision == _DOWN_REVISION
    # `alembic_version.version_num` is VARCHAR(32).
    assert len(migration.revision) <= 32
    assert ScriptDirectory.from_config(Config("alembic.ini")).get_current_head() == _REVISION


@pytest.mark.parametrize("table", _TABLES)
def test_0042_shape_is_the_design_and_the_model(table: str) -> None:
    """The migration's frozen literals, the model's declarations and design §2 agree: columns
    (type, width, nullability, in this order), the primary key, the indexes in their column order,
    the foreign keys RESTRICT, the checks by name."""
    migration = _load()

    assert _MODELS[table].__tablename__ == table
    assert migration._TABLES == _TABLES
    assert migration._SHAPES[table] == _design_shape(table) == _model_shape(table)
    assert list(migration._SHAPES[table]["columns"]) == list(_DESIGN_COLUMNS[table])
    assert [c.name for c in _MODELS[table].__table__.columns] == list(_DESIGN_COLUMNS[table])


def test_0042_nullable_columns_are_exactly_the_designs_four() -> None:
    nullable = {
        (table, name)
        for table in _TABLES
        for name, (_, is_nullable) in _model_shape(table)["columns"].items()  # type: ignore[union-attr]
        if is_nullable
    }
    assert nullable == _DESIGN_NULLABLE


def test_0042_public_id_and_idempotency_key_are_char_36_without_a_collation() -> None:
    """Typed and collated as 0041's `public_id`: CHAR(36), no COLLATE of their own."""
    migration = _load()
    for name in ("public_id", "idempotency_key"):
        column = UsageRebill.__table__.c[name]
        assert isinstance(column.type, sqlalchemy.CHAR)
        assert column.type.length == 36
        assert column.type.collation is None
        assert f"{name} CHAR(36) NOT NULL" in migration._CREATE_REBILLS_MYSQL
    assert "COLLATE" not in _normalised(migration._CREATE_REBILLS_MYSQL)
    assert "COLLATE" not in _normalised(migration._CREATE_LINES_MYSQL)


@pytest.mark.parametrize("table", _TABLES)
def test_0042_table_args_order_is_the_migrations(table: str) -> None:
    """`__table_args__` declares the indexes, foreign keys and checks under the migration's names
    and in its order."""
    migration = _load()
    kinds = UniqueConstraint | sqlalchemy.Index | ForeignKeyConstraint | CheckConstraint
    declared = [str(arg.name) for arg in _MODELS[table].__table_args__ if isinstance(arg, kinds)]

    assert declared == [
        *migration._INDEXES[table],
        *migration._FOREIGN_KEYS[table],
        *migration._CHECKS[table],
    ]
    assert list(migration._INDEXES[table]) == list(_DESIGN_INDEXES[table])
    assert list(migration._FOREIGN_KEYS[table]) == list(_DESIGN_FOREIGN_KEYS[table])


@pytest.mark.parametrize("table", _TABLES)
def test_0042_checks_are_the_designs_and_the_models(table: str) -> None:
    """Same conditions in the migration, on the model and in the design (whitespace aside)."""
    migration = _load()
    model = {
        str(constraint.name): _normalised(str(constraint.sqltext))
        for constraint in _MODELS[table].__table__.constraints
        if isinstance(constraint, CheckConstraint)
    }

    assert {name: _normalised(c) for name, c in migration._CHECKS[table].items()} == model
    assert {name: _normalised(c) for name, c in _DESIGN_CHECK_TEXTS[table].items()} == model
    assert list(migration._CHECKS[table]) == list(_DESIGN_CHECK_TEXTS[table])


def test_0042_reason_check_is_the_design_expression_word_for_word() -> None:
    """Design §2 (v12): CHAR(9), CHAR(10), CHAR(11), CHAR(12), CHAR(13) replaced by a space in this
    order, then TRIM, not the empty string."""
    migration = _load()

    assert migration._REBILL_CHECKS["ck_usage_rebills_reason"] == _DESIGN_REASON
    assert REASON_CHECK == _DESIGN_REASON
    codes = [int(code) for code in re.findall(r"CHAR\((\d+)\)", _DESIGN_REASON)]
    assert codes == [9, 10, 11, 12, 13]
    assert _normalised(EVENT_COUNT_CHECK) == _normalised("event_count BETWEEN 1 AND 1000")
    assert _normalised(DIFFERENCE_CHECK) == _normalised("difference <> 0")


def test_0042_has_no_equation_check() -> None:
    """Design v9: no "difference = corrected − previous" check (SQLite compares DECIMAL as a
    float and would refuse legal rows); AIH-TASK-255's service keeps the equation."""
    migration = _load()
    for table in _TABLES:
        for name, condition in migration._CHECKS[table].items():
            assert "-" not in condition, name
            assert not re.search(r"\bdifference\s*=", condition, re.IGNORECASE), name


@pytest.mark.parametrize("table", _TABLES)
def test_0042_every_foreign_key_has_an_index_with_its_leading_column(table: str) -> None:
    """MySQL builds no hidden index for a foreign key: each has a declared index (a unique key or
    an index) whose leading column is the key's column; the others are named ix_<table>_<column>."""
    leading = {columns[0]: name for name, (columns, _) in _DESIGN_INDEXES[table].items()}
    # The indexes design §2 names and the unique keys serve their foreign keys too.
    serving = {_TENANT_INDEX, _USAGE_EVENT_INDEX}
    for key in _DESIGN_FOREIGN_KEYS[table].values():
        column = key[0][0]
        assert column in leading, column
        name = leading[column]
        assert name in serving or name.startswith("uq_") or name == f"ix_{table}_{column}", name


@pytest.mark.parametrize(
    ("table", "name"),
    [
        (_REBILLS, "previous_total"),
        (_REBILLS, "corrected_total"),
        (_REBILLS, "difference"),
        (_LINES, "previous_charge"),
        (_LINES, "corrected_charge"),
        (_LINES, "difference"),
        (_LINES, "corrected_estimated_provider_cost_myr"),
    ],
)
def test_0042_amounts_are_decimal_20_8(table: str, name: str) -> None:
    """INV-10: every amount is DECIMAL(20,8), read as Decimal."""
    column = _MODELS[table].__table__.c[name]
    assert isinstance(column.type, sqlalchemy.Numeric)
    assert (column.type.precision, column.type.scale) == (20, 8)
    assert column.type.asdecimal is True


@pytest.mark.parametrize("table", _TABLES)
def test_0042_model_tells_not_to_update_or_delete(table: str) -> None:
    doc = _MODELS[table].__doc__ or ""
    assert "UPDATE" in doc and "DELETE" in doc
    assert "触发器" in doc


@pytest.mark.parametrize("table", _TABLES)
def test_0042_mysql_create_table_is_the_design_one(table: str) -> None:
    """Each `CREATE TABLE` the MySQL upgrade sends is the design's clauses, the indexes, the
    foreign keys and the checks included; it is one statement."""
    migration = _load()
    statement = migration._CREATE_TABLES_MYSQL[table]

    assert _compact(statement) == _compact(f"CREATE TABLE {table} ({', '.join(_CLAUSES[table])})")
    assert statement.count(";") == 0
    assert _normalised(statement).count("CREATE TABLE") == 1


def test_0042_triggers_are_the_design_ones() -> None:
    """Four triggers: BEFORE UPDATE / BEFORE DELETE on each table, the unconditional body of the
    task contract (BEGIN … END around it), in this order."""
    migration = _load()

    assert list(migration._TRIGGERS) == list(_DESIGN_TRIGGERS)
    for table in _TABLES:
        body = migration._TRIGGER_BODIES[table]
        assert _normalised(body) == _normalised(_DESIGN_TRIGGER_BODIES[table])
        assert body == body.strip()
        assert " IF " not in f" {_normalised(body)} "
    for name, (table, timing, kind) in _DESIGN_TRIGGERS.items():
        assert _compact(migration._TRIGGERS[name]) == _compact(
            f"CREATE TRIGGER {name} {timing} {kind} ON {table} FOR EACH ROW"
            f" {_DESIGN_TRIGGER_BODIES[table]}"
        )


def test_0042_prerequisite_text_is_0041s() -> None:
    """The prerequisite's query, check and wording follow 0041 (the trigger noun aside)."""
    migration = _load()
    earlier = _load(
        pathlib.Path("alembic/versions/20261117_0041_provider_cost_reconciliations.py"),
        "migration_0041",
    )

    assert migration._PREREQUISITE_QUERY == earlier._PREREQUISITE_QUERY
    assert migration._PREREQUISITE_ERROR == earlier._PREREQUISITE_ERROR.replace(
        "provider cost reconciliation triggers", "usage rebill triggers"
    )


def test_0042_downgrade_message_and_statement_are_the_designs() -> None:
    migration = _load()

    assert migration._not_empty_error(_REBILLS) == _NOT_EMPTY
    assert _normalised(migration._DROP_BOTH_MYSQL) == _normalised(
        f"DROP TABLE {_LINES}, {_REBILLS}"
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


def _has_rows_sql(table: str) -> str:
    return f"SELECT 1 FROM {table} LIMIT 1"


class _FakeBind:
    """Answers what the migration asks: delete rules, the triggers, the prerequisite variables
    and "any row?" for each table."""

    def __init__(
        self,
        tables: dict[str, dict],
        triggers: dict[str, tuple[str, str, str, str]],
        *,
        with_rows: frozenset[str] = frozenset(),
        log_bin: int = 1,
        trusted: int = 1,
    ) -> None:
        self._tables = tables
        self._triggers = triggers
        self._with_rows = with_rows
        self._variables = (log_bin, trusted)
        self.queries: list[str] = []

    def execute(self, statement: object) -> _Result:
        sql = str(statement)
        self.queries.append(sql)
        if "REFERENTIAL_CONSTRAINTS" in sql:
            table = re.search(r"TABLE_NAME = '(\w+)'", sql)
            assert table is not None and table.group(1) in self._tables, sql
            return _Result(list(self._tables[table.group(1)]["delete_rules"].items()))
        if "information_schema.TRIGGERS" in sql:
            return _Result([(name, *row) for name, row in self._triggers.items()])
        if "@@log_bin" in sql:
            return _Result([self._variables])
        for table in _TABLES:
            if sql == _has_rows_sql(table):
                assert table in self._tables, sql
                return _Result([(1,)] if table in self._with_rows else [])
        raise AssertionError(sql)


class _FakeInspector:
    """`Inspector` over the reflected tables that exist."""

    def __init__(self, tables: dict[str, dict]) -> None:
        self._tables = tables
        self.asked: list[tuple[str, str]] = []

    def _get(self, key: str, name: str):
        assert name in self._tables, name
        self.asked.append((key, name))
        return copy.deepcopy(self._tables[name][key])

    def has_table(self, name: str) -> bool:
        assert name in _TABLES, name
        self.asked.append(("has_table", name))
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
    "INTEGER": mysql.INTEGER(),
    "CHAR(7)": mysql.CHAR(length=7),
    "CHAR(36)": mysql.CHAR(length=36),
    "CHAR(64)": mysql.CHAR(length=64),
    "VARCHAR(255)": mysql.VARCHAR(length=255),
    "DECIMAL(20,8)": mysql.DECIMAL(precision=20, scale=8),
    "DATETIME": mysql.DATETIME(),
}


def _design_table(table: str) -> dict:
    """The table the way MySQL's inspector reflects it: unique constraints are also indexes."""
    indexes = _DESIGN_INDEXES[table]
    foreign_keys = _DESIGN_FOREIGN_KEYS[table]
    return {
        "columns": [
            {
                "name": name,
                "type": _MYSQL_TYPES[kind],
                "nullable": nullable,
                "autoincrement": name == "id",
            }
            for name, (kind, nullable) in _DESIGN_COLUMNS[table].items()
        ],
        "primary_key": {"constrained_columns": list(_DESIGN_PRIMARY_KEY), "name": None},
        "indexes": [
            {"name": name, "column_names": list(columns), "unique": unique}
            for name, (columns, unique) in indexes.items()
        ],
        "unique_constraints": [
            {"name": name, "column_names": list(columns)}
            for name, (columns, unique) in indexes.items()
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
            for name, (columns, referred, referred_columns, rule) in foreign_keys.items()
        ],
        "check_constraints": [
            {"name": name, "sqltext": "…"} for name in _DESIGN_CHECK_TEXTS[table]
        ],
        "delete_rules": {name: key[3] for name, key in foreign_keys.items()},
    }


def _design_tables(*tables: str) -> dict[str, dict]:
    return {table: _design_table(table) for table in (tables or _TABLES)}


def _design_triggers(migration: ModuleType) -> dict[str, tuple[str, str, str, str]]:
    """As information_schema.TRIGGERS keeps them: the body with MySQL's own blanks around it."""
    return {
        name: (table, timing, kind, f"\n{migration._TRIGGER_BODIES[table]}\n")
        for name, (table, timing, kind) in _DESIGN_TRIGGERS.items()
    }


def _with(
    monkeypatch,
    tables: dict[str, dict],
    triggers: dict[str, tuple[str, str, str, str]] | None = None,
    *,
    with_rows: frozenset[str] = frozenset(),
    dialect: str = "mysql",
    as_sql: bool = False,
    trusted: int = 1,
) -> tuple[ModuleType, _FakeOp, _FakeInspector, _FakeBind]:
    migration = _load()
    bind = _FakeBind(tables, triggers or {}, with_rows=with_rows, trusted=trusted)
    fake = _FakeOp(bind, dialect=dialect, as_sql=as_sql)
    inspector = _FakeInspector(tables)
    monkeypatch.setattr(migration, "op", fake)
    monkeypatch.setattr(migration, "sa", _Sa(inspector))
    return migration, fake, inspector, bind


def _executed(fake: _FakeOp) -> list[str]:
    """Each call as `create table <name>`, `create trigger <name>`, or `<kind> <name>` for the
    other `op` calls."""
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
    f"create table {_REBILLS}",
    f"create table {_LINES}",
    *(f"create trigger {name}" for name in _DESIGN_TRIGGERS),
]


def test_0042_upgrade_builds_everything_in_the_design_order(monkeypatch) -> None:
    migration, fake, _, bind = _with(monkeypatch, {})
    migration.upgrade()

    assert _executed(fake) == _FULL_UPGRADE
    assert [args[0] for _, args in fake.calls] == [
        migration._CREATE_REBILLS_MYSQL,
        migration._CREATE_LINES_MYSQL,
        *(migration._TRIGGERS[name] for name in _DESIGN_TRIGGERS),
    ]
    # The prerequisite check comes right before the first DDL; no row is read.
    assert "@@log_bin" in bind.queries[-1]
    assert not any(query.startswith("SELECT 1 FROM") for query in bind.queries)


def test_0042_upgrade_refuses_before_any_ddl_without_the_trigger_privilege(monkeypatch) -> None:
    """binlog on and log_bin_trust_function_creators off: the account could not create the
    triggers (ERROR 1419) — refuse before CREATE TABLE."""
    migration, fake, _, _ = _with(monkeypatch, {}, trusted=0)

    with pytest.raises(RuntimeError, match="log_bin_trust_function_creators"):
        migration.upgrade()

    assert fake.calls == []


def test_0042_upgrade_with_everything_built_issues_nothing(monkeypatch) -> None:
    migration = _load()
    migration, fake, inspector, bind = _with(
        monkeypatch, _design_tables(), _design_triggers(migration)
    )
    migration.upgrade()

    assert fake.calls == []
    for table in _TABLES:
        for key in ("columns", "primary_key", "indexes", "foreign_keys", "check_constraints"):
            assert (key, table) in inspector.asked
    assert sum("REFERENTIAL_CONSTRAINTS" in query for query in bind.queries) == 2
    # Nothing to build: the prerequisite is not asked either.
    assert not _prerequisite_asked(bind)


def test_0042_upgrade_on_sqlite_with_everything_built_issues_nothing(monkeypatch) -> None:
    migration, fake, _, bind = _with(monkeypatch, _design_tables(), dialect="sqlite")
    migration.upgrade()

    assert fake.calls == []
    assert bind.queries == []


def test_0042_upgrade_after_the_first_create_table_builds_the_rest(monkeypatch) -> None:
    """Design §8: the upgrade cut between the two `CREATE TABLE` — `usage_rebills` there (no
    trigger yet): the rerun builds `usage_rebill_lines` and the four triggers."""
    migration, fake, _, bind = _with(monkeypatch, _design_tables(_REBILLS))
    migration.upgrade()

    assert _executed(fake) == _FULL_UPGRADE[1:]
    assert _prerequisite_asked(bind)


@pytest.mark.parametrize(
    "missing",
    [[name] for name in _DESIGN_TRIGGERS] + [list(_DESIGN_TRIGGERS)],
    ids=[*_DESIGN_TRIGGERS, "all four"],
)
def test_0042_upgrade_builds_only_the_missing_triggers(monkeypatch, missing: list[str]) -> None:
    """Both tables built, a trigger not (the interrupted upgrade) — only those, after the
    prerequisite check."""
    migration = _load()
    triggers = _design_triggers(migration)
    for name in missing:
        del triggers[name]
    migration, fake, _, bind = _with(monkeypatch, _design_tables(), triggers)
    migration.upgrade()

    assert _executed(fake) == [f"create trigger {name}" for name in missing]
    assert [args[0] for _, args in fake.calls] == [migration._TRIGGERS[name] for name in missing]
    assert _prerequisite_asked(bind)


def _trigger_changed(name: str, position: int, value: str):
    def mutate(triggers: dict[str, tuple[str, str, str, str]]) -> None:
        row = list(triggers[name])
        row[position] = value
        triggers[name] = (row[0], row[1], row[2], row[3])

    return mutate


# Each trigger that differs: the refusal names it.
_WRONG_TRIGGERS = {
    "rebills update body conditional": (
        _REBILLS_UPDATE,
        _trigger_changed(
            _REBILLS_UPDATE,
            3,
            "BEGIN IF OLD.reason <> NEW.reason THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT ="
            f" '{_IMMUTABLE[_REBILLS]}'; END IF; END",
        ),
    ),
    "lines update body of the other table": (
        _LINES_UPDATE,
        _trigger_changed(_LINES_UPDATE, 3, _DESIGN_TRIGGER_BODIES[_REBILLS]),
    ),
    "rebills delete trigger AFTER": (
        _REBILLS_DELETE,
        _trigger_changed(_REBILLS_DELETE, 1, "AFTER"),
    ),
    "lines delete trigger on UPDATE": (
        _LINES_DELETE,
        _trigger_changed(_LINES_DELETE, 2, "UPDATE"),
    ),
    "lines delete trigger on the rebills table": (
        _LINES_DELETE,
        _trigger_changed(_LINES_DELETE, 0, _REBILLS),
    ),
}


@pytest.mark.parametrize("case", list(_WRONG_TRIGGERS))
def test_0042_upgrade_refuses_a_trigger_that_differs_before_any_ddl(monkeypatch, case: str) -> None:
    migration = _load()
    name, mutate = _WRONG_TRIGGERS[case]
    triggers = _design_triggers(migration)
    mutate(triggers)
    migration, fake, _, bind = _with(monkeypatch, _design_tables(), triggers)

    with pytest.raises(RuntimeError) as raised:
        migration.upgrade()

    assert fake.calls == []
    assert not _prerequisite_asked(bind)
    message = str(raised.value)
    assert f"trigger {name} exists but is not the one 0042 builds" in message
    assert "No DDL was issued" in message


def test_0042_upgrade_refuses_a_trigger_of_ours_on_another_table_without_the_tables(
    monkeypatch,
) -> None:
    migration = _load()
    triggers = {
        _REBILLS_UPDATE: (
            "provider_cost_reconciliations",
            "BEFORE",
            "UPDATE",
            migration._TRIGGER_BODIES[_REBILLS],
        )
    }
    migration, fake, _, _ = _with(monkeypatch, {}, triggers)

    with pytest.raises(RuntimeError, match=_REBILLS_UPDATE):
        migration.upgrade()

    assert fake.calls == []


def _drop_index(name: str, hidden_for: str | None = None, column: str | None = None):
    def mutate(table: dict) -> None:
        table["indexes"] = [i for i in table["indexes"] if i["name"] != name]
        table["unique_constraints"] = [u for u in table["unique_constraints"] if u["name"] != name]
        if hidden_for is not None:
            # What MySQL leaves without it: a hidden index of its own for the foreign key.
            table["indexes"].append({"name": hidden_for, "column_names": [column], "unique": False})

    return mutate


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


def _drop_check(name: str):
    def mutate(table: dict) -> None:
        table["check_constraints"] = [c for c in table["check_constraints"] if c["name"] != name]

    return mutate


def _extra_check(name: str):
    def mutate(table: dict) -> None:
        table["check_constraints"].append({"name": name, "sqltext": "…"})

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


_WRONG_SHAPES: dict[str, tuple[str, object]] = {
    "rebills without the created_by index": (
        _REBILLS,
        _drop_index("ix_usage_rebills_created_by", "fk_usage_rebills_created_by", "created_by"),
    ),
    "rebills without the tenant index": (
        _REBILLS,
        _drop_index(_TENANT_INDEX, _FK_TENANT, "tenant_id"),
    ),
    "rebills tenant index on tenant_id only": (
        _REBILLS,
        _index_columns(_TENANT_INDEX, ["tenant_id"]),
    ),
    "rebills idempotency key a plain index": (_REBILLS, _unique_as_plain(_IDEMPOTENCY_UNIQUE)),
    "rebills without the wallet transaction unique key": (
        _REBILLS,
        _drop_index(_WALLET_TRANSACTION_UNIQUE),
    ),
    "rebills a prefix length": (_REBILLS, _prefix_length),
    "rebills tenant foreign key not RESTRICT": (_REBILLS, _cascade(_FK_TENANT)),
    "rebills without the created_by foreign key": (
        _REBILLS,
        _no_foreign_key("fk_usage_rebills_created_by"),
    ),
    "rebills without the reason check": (_REBILLS, _drop_check(_check(_REBILLS, "reason"))),
    "rebills with an equation check": (_REBILLS, _extra_check(_check(_REBILLS, "difference"))),
    "rebills with a metadata_json column": (_REBILLS, _extra_column("metadata_json")),
    "rebills reason nullable": (_REBILLS, _column("reason", nullable=True)),
    "rebills corrective rule not nullable": (
        _REBILLS,
        _column("corrective_pricing_rule_id", nullable=False),
    ),
    "rebills difference DECIMAL(20,2)": (
        _REBILLS,
        _column("difference", type=mysql.DECIMAL(20, 2)),
    ),
    "rebills event_count BIGINT": (_REBILLS, _column("event_count", type=mysql.BIGINT())),
    "rebills public_id VARCHAR(36)": (_REBILLS, _column("public_id", type=mysql.VARCHAR(36))),
    "rebills fingerprint CHAR(40)": (
        _REBILLS,
        _column("request_fingerprint", type=mysql.CHAR(40)),
    ),
    "rebills primary key on public_id": (_REBILLS, _primary_key("public_id")),
    "lines without the statement index": (
        _LINES,
        _drop_index(_STATEMENT_INDEX, _FK_STATEMENT, "original_statement_id"),
    ),
    "lines usage event index in another order": (
        _LINES,
        _index_columns(_USAGE_EVENT_INDEX, ["id", "usage_event_id"]),
    ),
    "lines rebill and event a plain index": (_LINES, _unique_as_plain(_REBILL_EVENT_UNIQUE)),
    "lines rebill foreign key not RESTRICT": (_LINES, _cascade(_FK_REBILL)),
    "lines without the estimated cost check": (
        _LINES,
        _drop_check(_check(_LINES, "estimated_cost")),
    ),
    "lines original period CHAR(10)": (_LINES, _column("original_period", type=mysql.CHAR(10))),
    "lines previous rule nullable": (_LINES, _column("previous_pricing_rule_id", nullable=True)),
    "lines original statement not nullable": (
        _LINES,
        _column("original_statement_id", nullable=False),
    ),
}


@pytest.mark.parametrize("shape", list(_WRONG_SHAPES))
@pytest.mark.parametrize("dialect", ["mysql", "sqlite"])
def test_0042_upgrade_refuses_a_wrong_shape_before_any_ddl(
    monkeypatch, shape: str, dialect: str
) -> None:
    """Design §2: a table there, any item missing, extra or different — nothing is built (not even
    a missing trigger or the other table); the message names the table."""
    table, mutate = _WRONG_SHAPES[shape]
    tables = _design_tables()
    mutate(tables[table])  # type: ignore[operator]
    migration, fake, _, bind = _with(monkeypatch, tables, {}, dialect=dialect)

    with pytest.raises(RuntimeError) as raised:
        migration.upgrade()

    assert fake.calls == []
    assert not _prerequisite_asked(bind)
    message = str(raised.value)
    assert f"table {table} exists but differs" in message
    assert "No DDL was issued" in message
    assert f"DROP TABLE {table}" in message
    assert "data incident" in message


def test_0042_upgrade_refuses_a_wrong_lines_table_without_the_rebills_table(monkeypatch) -> None:
    tables = _design_tables(_LINES)
    _column("difference", nullable=True)(tables[_LINES])
    migration, fake, _, _ = _with(monkeypatch, tables)

    with pytest.raises(RuntimeError, match=f"table {_LINES} exists but differs"):
        migration.upgrade()

    assert fake.calls == []


def test_0042_upgrade_does_not_compare_server_defaults(monkeypatch) -> None:
    """Design §2 / task contract: the shape check does not look at server defaults."""
    tables = _design_tables()
    _column("event_count", default="1")(tables[_REBILLS])
    migration = _load()
    migration, fake, _, _ = _with(monkeypatch, tables, _design_triggers(migration))
    migration.upgrade()

    assert fake.calls == []


def test_0042_upgrade_on_sqlite_creates_the_tables_without_triggers(monkeypatch) -> None:
    migration, fake, _, bind = _with(monkeypatch, {}, dialect="sqlite")
    migration.upgrade()

    expected: list[tuple[str, tuple[object, ...]]] = []
    for table in _TABLES:
        expected.append(("create_table", (table,)))
        expected += [
            ("create_index", (name, table, list(columns), False))
            for name, (columns, unique) in _DESIGN_INDEXES[table].items()
            if not unique
        ]
    assert fake.calls == expected
    assert bind.queries == []


def test_0042_offline_upgrade_emits_everything_without_reading(monkeypatch) -> None:
    migration = _load()
    migration, fake, inspector, bind = _with(
        monkeypatch, _design_tables(), _design_triggers(migration), as_sql=True
    )
    migration.upgrade()

    assert inspector.asked == []
    assert bind.queries == []
    assert _executed(fake) == _FULL_UPGRADE


def test_0042_offline_downgrade_refuses_before_reading_or_ddl(monkeypatch) -> None:
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
    assert _REBILLS in message
    assert "online" in message


def test_0042_downgrade_drops_both_empty_tables_in_one_statement(monkeypatch) -> None:
    """Design §8: both empty — one `DROP TABLE` naming both (the triggers go with them); the
    triggers are not looked up."""
    migration = _load()
    migration, fake, _, bind = _with(monkeypatch, _design_tables(), _design_triggers(migration))
    migration.downgrade()

    assert bind.queries == [_has_rows_sql(_REBILLS), _has_rows_sql(_LINES)]
    assert fake.calls == [("execute", (f"DROP TABLE {_LINES}, {_REBILLS}",))]


def test_0042_downgrade_on_sqlite_drops_the_empty_tables_one_by_one(monkeypatch) -> None:
    migration, fake, _, bind = _with(monkeypatch, _design_tables(), dialect="sqlite")
    migration.downgrade()

    assert bind.queries == [_has_rows_sql(_REBILLS), _has_rows_sql(_LINES)]
    assert fake.calls == [("drop_table", (_LINES,)), ("drop_table", (_REBILLS,))]


@pytest.mark.parametrize(
    "with_rows",
    [frozenset({_REBILLS, _LINES}), frozenset({_REBILLS}), frozenset({_LINES})],
    ids=["both", "rebills", "lines"],
)
@pytest.mark.parametrize("dialect", ["mysql", "sqlite"])
def test_0042_downgrade_refuses_with_a_row_before_any_ddl(
    monkeypatch, with_rows: frozenset[str], dialect: str
) -> None:
    """Design §8: a row in either table — RuntimeError, no DDL, no row deleted."""
    migration = _load()
    migration, fake, _, _ = _with(
        monkeypatch,
        _design_tables(),
        _design_triggers(migration),
        with_rows=with_rows,
        dialect=dialect,
    )

    with pytest.raises(RuntimeError) as raised:
        migration.downgrade()

    assert fake.calls == []
    first = _REBILLS if _REBILLS in with_rows else _LINES
    assert str(raised.value) == _NOT_EMPTY.replace(_REBILLS, first, 1)
    if first == _REBILLS:
        assert str(raised.value) == _NOT_EMPTY


@pytest.mark.parametrize("left", _TABLES)
@pytest.mark.parametrize("dialect", ["mysql", "sqlite"])
def test_0042_downgrade_with_one_table_left_drops_only_it_when_empty(
    monkeypatch, left: str, dialect: str
) -> None:
    """Design §8: only one table (the upgrade cut between the two `CREATE TABLE`) and no row —
    only it is dropped."""
    migration, fake, _, bind = _with(monkeypatch, _design_tables(left), dialect=dialect)
    migration.downgrade()

    assert bind.queries == [_has_rows_sql(left)]
    assert fake.calls == [("drop_table", (left,))]


@pytest.mark.parametrize("left", _TABLES)
def test_0042_downgrade_with_one_table_left_refuses_with_a_row(monkeypatch, left: str) -> None:
    migration, fake, _, _ = _with(monkeypatch, _design_tables(left), with_rows=frozenset({left}))

    with pytest.raises(RuntimeError, match=f"^{left} is not empty"):
        migration.downgrade()

    assert fake.calls == []


@pytest.mark.parametrize("dialect", ["mysql", "sqlite"])
def test_0042_downgrade_without_the_tables_does_nothing(monkeypatch, dialect: str) -> None:
    migration, fake, inspector, bind = _with(monkeypatch, {}, dialect=dialect)
    migration.downgrade()

    assert inspector.asked == [("has_table", _REBILLS), ("has_table", _LINES)]
    assert bind.queries == []
    assert fake.calls == []


def test_0042_downgrade_does_not_rename() -> None:
    """Design v9 withdrew the rename protocol."""
    source = _MIGRATION.read_text(encoding="utf-8")
    code = source.split('"""', 2)[2]
    assert "RENAME" not in code.upper()


# ---------------------------------------------------------------------------
# SQLite (the models' metadata)
# ---------------------------------------------------------------------------


@contextmanager
def _sqlite() -> Iterator[Engine]:
    """The unit tests build SQLite with `create_all` from the models, not with the migrations.
    SQLite does not enforce foreign keys here (no `PRAGMA foreign_keys`): the parent tables are
    not needed."""
    engine = create_engine("sqlite+pysqlite:///:memory:")
    try:
        Base.metadata.create_all(engine, tables=[UsageRebill.__table__, UsageRebillLine.__table__])
        yield engine
    finally:
        engine.dispose()


@pytest.mark.parametrize("table", _TABLES)
def test_0042_sqlite_tables_get_the_indexes_the_keys_and_the_checks(table: str) -> None:
    with _sqlite() as engine:
        inspector = inspect(engine)
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

    assert indexes == _DESIGN_INDEXES[table]
    assert keys == _DESIGN_FOREIGN_KEYS[table]
    assert checks == set(_DESIGN_CHECK_TEXTS[table])
    assert primary_key == _DESIGN_PRIMARY_KEY


@pytest.mark.parametrize("row", list(_LEGAL_ROWS))
def test_0042_sqlite_takes_the_legal_rows(row: str) -> None:
    table, overrides = _LEGAL_ROWS[row]
    with _sqlite() as engine:
        with engine.begin() as connection:
            _insert(connection, table, overrides)
            assert int(connection.execute(_count(table)).scalar_one()) == 1


def test_0042_sqlite_takes_a_reason_of_only_no_break_spaces() -> None:
    """The known edge (design §2): not an ASCII blank, so the database takes it."""
    with _sqlite() as engine:
        with engine.begin() as connection:
            _insert_rebill(connection, {"reason": _NO_BREAK_SPACES})
            assert int(connection.execute(_count(_REBILLS)).scalar_one()) == 1


@pytest.mark.parametrize("row", list(_ILLEGAL_ROWS))
def test_0042_sqlite_checks_refuse_the_illegal_rows(row: str) -> None:
    table, overrides, check = _ILLEGAL_ROWS[row]
    with _sqlite() as engine:
        with pytest.raises(DBAPIError) as raised:
            with engine.begin() as connection:
                _insert(connection, table, overrides)
        message = str(raised.value)
        assert "CHECK constraint failed" in message, row
        assert check in message, row

        with engine.connect() as connection:
            assert int(connection.execute(_count(table)).scalar_one()) == 0


# Design §4 "数据库保证的唯一性": a second row → (its table, its overrides, the unique key, a column
# the SQLite message names).
_DUPLICATES: dict[str, tuple[str, dict[str, object], str, str]] = {
    "the same public_id": (_REBILLS, {"public_id": _uuid(1)}, _PUBLIC_ID_UNIQUE, "public_id"),
    "the same idempotency key": (
        _REBILLS,
        {"idempotency_key": _uuid(1)},
        _IDEMPOTENCY_UNIQUE,
        "idempotency_key",
    ),
    "the same ledger row": (
        _REBILLS,
        {"wallet_transaction_id": 1},
        _WALLET_TRANSACTION_UNIQUE,
        "wallet_transaction_id",
    ),
    "the same event twice in a rebill": (
        _LINES,
        {"usage_event_id": 1},
        _REBILL_EVENT_UNIQUE,
        "usage_event_id",
    ),
}


@pytest.mark.parametrize("case", list(_DUPLICATES))
def test_0042_sqlite_unique_keys_refuse_a_duplicate(case: str) -> None:
    table, overrides, _, column = _DUPLICATES[case]
    with _sqlite() as engine:
        with engine.begin() as connection:
            _insert(connection, table, {}, 1)
        with pytest.raises(IntegrityError) as raised:
            with engine.begin() as connection:
                _insert(connection, table, overrides, 2)
        message = str(raised.value)
        assert "UNIQUE constraint failed" in message, case
        assert f"{table}.{column}" in message, case

        with engine.connect() as connection:
            assert int(connection.execute(_count(table)).scalar_one()) == 1


def test_0042_sqlite_takes_one_event_in_two_rebills() -> None:
    """Design §1 第 6 条: an event can be corrected more than once — one line per rebill."""
    with _sqlite() as engine:
        with engine.begin() as connection:
            _insert_line(connection, {"rebill_id": 1}, 1)
            _insert_line(
                connection,
                {
                    "rebill_id": 2,
                    "previous_charge": "1.20",
                    "corrected_charge": "1.40",
                    "difference": "0.20",
                },
                1,
            )
            assert int(connection.execute(_count(_LINES)).scalar_one()) == 2


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
    """`CREATE TABLE USAGE_REBILLS`, `CREATE TRIGGER TRG_…`, …"""
    return [" ".join(_normalised(s).split(" ")[:3]) for s in _writes(statements)]


def _asked_prerequisite(statements: list[str]) -> bool:
    return any("@@LOG_BIN" in _normalised(s) for s in statements)


def _version(engine: Engine) -> str:
    with engine.connect() as connection:
        return str(connection.execute(_ALEMBIC_VERSION).scalar_one())


def _execute(engine: Engine, statement: str, params: dict[str, object] | None = None) -> None:
    with engine.begin() as connection:
        connection.execute(text(statement), params or {})


def _present(engine: Engine) -> list[str]:
    inspector = inspect(engine)
    return [table for table in _TABLES if inspector.has_table(table)]


@contextmanager
def _foreign_key_checks_off(engine: Engine) -> Iterator[sqlalchemy.Connection]:
    with engine.begin() as connection:
        connection.exec_driver_sql("SET FOREIGN_KEY_CHECKS = 0")
        try:
            yield connection
        finally:
            connection.exec_driver_sql("SET FOREIGN_KEY_CHECKS = 1")


def _truncate(engine: Engine) -> None:
    """The triggers refuse DELETE: TRUNCATE (DDL, no triggers) with foreign key checks off (the
    lines refer to the rebills). The triggers are never dropped for this."""
    present = _present(engine)
    with _foreign_key_checks_off(engine) as connection:
        for table in present:
            connection.exec_driver_sql(f"TRUNCATE TABLE {table}")


def _drop_ours(engine: Engine) -> None:
    """Only at 0041: drops what a test left of the two tables (hand-made or not, with their
    triggers)."""
    with _foreign_key_checks_off(engine) as connection:
        connection.exec_driver_sql(f"DROP TABLE IF EXISTS {_LINES}, {_REBILLS}")


def _index_names(engine: Engine, table: str) -> set[str]:
    """Every index on the table besides the primary key, from information_schema.STATISTICS."""
    with engine.connect() as connection:
        return {
            str(name) for name in connection.execute(_STATISTICS_QUERY, {"table": table}).scalars()
        }


def _table_shape(engine: Engine, table: str) -> dict[str, object]:
    """The table as reflected, in the migration's terms; delete rules from information_schema;
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
    """Triggers by our four names (wherever they are): (table, timing, event, stripped body)."""
    with engine.connect() as connection:
        rows = connection.execute(_ALL_TRIGGERS_QUERY).all()
    return {
        str(name): (str(table), str(timing), str(kind), str(body).strip())
        for name, table, timing, kind, body in rows
        if name in _DESIGN_TRIGGERS
    }


def _assert_design_shape(engine: Engine) -> None:
    """Both tables reflected = the models' = the design's; exactly the declared indexes besides
    the primary key (no hidden index for a foreign key); the four triggers with the design's
    table, timing, event and body."""
    migration = _load()
    for table in _TABLES:
        assert _table_shape(engine, table) == _model_shape(table) == _design_shape(table)
        assert _index_names(engine, table) == set(_DESIGN_INDEXES[table])
        assert not _index_names(engine, table) & set(_DESIGN_FOREIGN_KEYS[table])
    assert _our_triggers(engine) == {
        name: (*row, migration._TRIGGER_BODIES[row[0]]) for name, row in _DESIGN_TRIGGERS.items()
    }


def _ours(engine: Engine) -> dict[str, object]:
    """The tables' shapes and rows (those that exist) and our triggers."""
    state: dict[str, object] = {"triggers": _our_triggers(engine)}
    for table in _present(engine):
        with engine.connect() as connection:
            rows = [tuple(r) for r in connection.execute(_rows(table))]
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
        "triggers": {trigger for trigger in triggers if trigger[1] not in _TABLES},
        "checks": {check for check in checks if check[0] not in _TABLES},
    }


def _at_0041(alembic_config: Config, engine: Engine) -> None:
    command.upgrade(alembic_config, "head")
    command.downgrade(alembic_config, _DOWN_REVISION)
    assert _version(engine) == _DOWN_REVISION
    assert _present(engine) == []
    assert _our_triggers(engine) == {}


def _at_0042(alembic_config: Config, engine: Engine) -> None:
    # Go to head first and come down, so that a later head does not change what this sees.
    command.upgrade(alembic_config, "head")
    command.downgrade(alembic_config, _REVISION)
    assert _version(engine) == _REVISION
    assert _present(engine) == list(_TABLES)


def _back_to_head(alembic_config: Config, engine: Engine) -> None:
    """Empties the tables; at 0042 with a table or a trigger gone (a test dropped it), puts
    `alembic_version` back to 0041; at 0041 drops what is left of the tables (and with them the
    triggers, hand-made or not); upgrades to head again: the database ends at head with both
    tables empty."""
    _truncate(engine)
    if _version(engine) == _REVISION and (
        _present(engine) != list(_TABLES) or set(_our_triggers(engine)) != set(_DESIGN_TRIGGERS)
    ):
        _execute(engine, _SET_VERSION, {"version": _DOWN_REVISION})
    if _version(engine) == _DOWN_REVISION:
        _drop_ours(engine)
    command.upgrade(alembic_config, "head")


def _commit_rebill_with_lines(engine: Engine, lines: int = 2) -> int:
    """One rebill and its lines, written with foreign key checks off (no tenant, ledger row, event,
    rule, price version or user is needed for this); returns the rebill's id."""
    with _foreign_key_checks_off(engine) as connection:
        rebill_id = _insert_rebill(connection, {"event_count": lines})
        for n in range(1, lines + 1):
            _insert_line(connection, {"rebill_id": rebill_id}, n)
    return rebill_id


@needs_mysql
def test_0042_round_trip(alembic_config: Config, engine: Engine) -> None:
    """0041 → 0042 → 0041 → 0042. The upgrade sends the two `CREATE TABLE` and the four
    `CREATE TRIGGER` (after the prerequisite check); the shapes are the models', the index sets
    exactly the declared ones, the triggers the design's; the downgrade (both empty) is one
    `DROP TABLE` naming both tables; every other table, key, check and trigger is the same
    throughout."""
    _at_0041(alembic_config, engine)
    try:
        at_0041 = _schema_snapshot(engine)

        with _statements() as statements:
            command.upgrade(alembic_config, _REVISION)
        assert _heads(statements) == [
            f"CREATE TABLE {_REBILLS.upper()}",
            f"CREATE TABLE {_LINES.upper()}",
            *(f"CREATE TRIGGER {name.upper()}" for name in _DESIGN_TRIGGERS),
        ]
        assert _asked_prerequisite(statements)
        assert _version(engine) == _REVISION
        _assert_design_shape(engine)
        with engine.connect() as connection:
            for table in _TABLES:
                assert int(connection.execute(_count(table)).scalar_one()) == 0
        at_0042 = _schema_snapshot(engine)
        assert _without_ours(at_0042) == at_0041

        with _statements() as statements:
            command.downgrade(alembic_config, _DOWN_REVISION)
        # Design §7 "降级保护": one statement drops both tables (and with them the triggers).
        assert [_normalised(s) for s in _writes(statements)] == [
            f"DROP TABLE {_LINES.upper()}, {_REBILLS.upper()}"
        ]
        assert _version(engine) == _DOWN_REVISION
        assert _present(engine) == []
        assert _our_triggers(engine) == {}
        assert _schema_snapshot(engine) == at_0041

        command.upgrade(alembic_config, _REVISION)
        assert _schema_snapshot(engine) == at_0042
        _assert_design_shape(engine)
    finally:
        _back_to_head(alembic_config, engine)


@needs_mysql
def test_0042_mysql_has_exactly_the_declared_indexes(
    alembic_config: Config, engine: Engine
) -> None:
    """information_schema.STATISTICS lists, besides the primary key, exactly the indexes the
    migration declares on each table — MySQL built no hidden index for a foreign key."""
    _at_0042(alembic_config, engine)

    assert _index_names(engine, _REBILLS) == {
        _PUBLIC_ID_UNIQUE,
        _IDEMPOTENCY_UNIQUE,
        _WALLET_TRANSACTION_UNIQUE,
        _TENANT_INDEX,
        "ix_usage_rebills_corrective_pricing_rule_id",
        "ix_usage_rebills_corrective_provider_price_version_id",
        "ix_usage_rebills_created_by",
    }
    assert _index_names(engine, _LINES) == {
        _REBILL_EVENT_UNIQUE,
        _USAGE_EVENT_INDEX,
        "ix_usage_rebill_lines_original_wallet_transaction_id",
        "ix_usage_rebill_lines_previous_pricing_rule_id",
        "ix_usage_rebill_lines_previous_provider_price_version_id",
        "ix_usage_rebill_lines_corrected_pricing_rule_id",
        "ix_usage_rebill_lines_corrected_provider_price_version_id",
        _STATEMENT_INDEX,
    }
    migration = _load()
    for table in _TABLES:
        assert _index_names(engine, table) == set(migration._INDEXES[table])


@needs_mysql
def test_0042_rerun_with_everything_built_only_moves_the_version(
    alembic_config: Config, engine: Engine
) -> None:
    """Upgraded, then `alembic_version` put back to 0041 (as if writing the version failed after
    everything was built) — no DDL, no prerequisite check; the version moves to 0042."""
    _at_0042(alembic_config, engine)
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
def test_0042_rerun_after_the_first_create_table_builds_the_rest(
    alembic_config: Config, engine: Engine
) -> None:
    """Version 0041 and only `usage_rebills` (the upgrade cut between the two `CREATE TABLE`):
    the rerun sends the `CREATE TABLE` of the lines and the four `CREATE TRIGGER`."""
    migration = _load()
    _at_0041(alembic_config, engine)
    try:
        _execute(engine, migration._CREATE_REBILLS_MYSQL)

        with _statements() as statements:
            command.upgrade(alembic_config, _REVISION)

        assert [_compact(s) for s in _writes(statements)] == [
            _compact(migration._CREATE_LINES_MYSQL),
            *(_compact(migration._TRIGGERS[name]) for name in _DESIGN_TRIGGERS),
        ]
        assert _asked_prerequisite(statements)
        assert _version(engine) == _REVISION
        _assert_design_shape(engine)
    finally:
        _back_to_head(alembic_config, engine)


@needs_mysql
@pytest.mark.parametrize(
    "missing",
    [[_REBILLS_UPDATE], [_LINES_DELETE], list(_DESIGN_TRIGGERS)],
    ids=[_REBILLS_UPDATE, _LINES_DELETE, "all four"],
)
def test_0042_rerun_with_a_trigger_missing_builds_only_it(
    alembic_config: Config, engine: Engine, missing: list[str]
) -> None:
    migration = _load()
    _at_0042(alembic_config, engine)
    try:
        for name in missing:
            _execute(engine, f"DROP TRIGGER {name}")
        _execute(engine, _SET_VERSION, {"version": _DOWN_REVISION})

        with _statements() as statements:
            command.upgrade(alembic_config, _REVISION)

        assert [_compact(s) for s in _writes(statements)] == [
            _compact(migration._TRIGGERS[name]) for name in missing
        ]
        assert _asked_prerequisite(statements)
        assert _version(engine) == _REVISION
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


# Each a fresh hand-made table at 0041: (its table, its DDL). A wrong lines table comes after the
# design's rebills table.
_WRONG_SHAPE_DDL: dict[str, tuple[str, str]] = {
    # MySQL then builds a hidden index of its own, named after the foreign key.
    "rebills without the created_by index": (
        _REBILLS,
        _create(_REBILLS, _without(_CLAUSES[_REBILLS], "INDEX ix_usage_rebills_created_by ")),
    ),
    "rebills idempotency key a plain index": (
        _REBILLS,
        _create(
            _REBILLS,
            _replaced(
                _CLAUSES[_REBILLS],
                f"CONSTRAINT {_IDEMPOTENCY_UNIQUE} UNIQUE (",
                f"INDEX {_IDEMPOTENCY_UNIQUE} (",
            ),
        ),
    ),
    "rebills tenant foreign key not RESTRICT": (
        _REBILLS,
        _create(
            _REBILLS,
            _replaced(
                _CLAUSES[_REBILLS],
                "REFERENCES tenants (id) ON DELETE RESTRICT",
                "REFERENCES tenants (id) ON DELETE CASCADE",
            ),
        ),
    ),
    "rebills without the reason check": (
        _REBILLS,
        _create(
            _REBILLS,
            _without(_CLAUSES[_REBILLS], f"CONSTRAINT {_check(_REBILLS, 'reason')} "),
        ),
    ),
    "rebills reason nullable": (
        _REBILLS,
        _create(
            _REBILLS,
            _replaced(
                _CLAUSES[_REBILLS], "reason VARCHAR(255) NOT NULL", "reason VARCHAR(255) NULL"
            ),
        ),
    ),
    "lines difference with two decimals": (
        _LINES,
        _create(
            _LINES,
            _replaced(_CLAUSES[_LINES], "difference DECIMAL(20, 8)", "difference DECIMAL(20, 2)"),
        ),
    ),
    "lines without the statement index": (
        _LINES,
        _create(_LINES, _without(_CLAUSES[_LINES], f"INDEX {_STATEMENT_INDEX} ")),
    ),
}


@needs_mysql
@pytest.mark.parametrize("shape", list(_WRONG_SHAPE_DDL))
def test_0042_rerun_with_a_wrong_shape_fails_before_any_ddl(
    alembic_config: Config, engine: Engine, shape: str
) -> None:
    """Version 0041 and a fresh hand-made table of the same name — the upgrade fails naming the
    table, issues no DDL (no table, no trigger), the version stays 0041 and the tables are as
    they were."""
    migration = _load()
    table, ddl = _WRONG_SHAPE_DDL[shape]
    _at_0041(alembic_config, engine)
    try:
        with _foreign_key_checks_off(engine) as connection:
            if table == _LINES:
                connection.exec_driver_sql(migration._CREATE_REBILLS_MYSQL)
            connection.exec_driver_sql(ddl)
        hand_made = _ours(engine)

        with _statements() as statements:
            with pytest.raises(RuntimeError) as raised:
                command.upgrade(alembic_config, _REVISION)

        assert _writes(statements) == []
        assert not _asked_prerequisite(statements)
        message = str(raised.value)
        assert f"table {table} exists but differs" in message
        assert "No DDL was issued" in message
        assert "data incident" in message
        assert _version(engine) == _DOWN_REVISION
        assert _ours(engine) == hand_made
    finally:
        _back_to_head(alembic_config, engine)


@needs_mysql
def test_0042_downgrade_with_rows_refuses_before_any_ddl(
    alembic_config: Config, engine: Engine
) -> None:
    """Design §7 "降级保护": a rebill and its lines — RuntimeError (design §8's words), no DDL;
    both tables, the triggers and the rows are as they were; `alembic_version` stays 0042."""
    _at_0042(alembic_config, engine)
    try:
        _commit_rebill_with_lines(engine)
        before_schema = _schema_snapshot(engine)
        before = _ours(engine)

        with _statements() as statements:
            with pytest.raises(RuntimeError) as raised:
                command.downgrade(alembic_config, _DOWN_REVISION)

        assert _writes(statements) == []
        assert str(raised.value) == _NOT_EMPTY
        assert _version(engine) == _REVISION
        assert _schema_snapshot(engine) == before_schema
        assert _ours(engine) == before
        _assert_design_shape(engine)
    finally:
        _back_to_head(alembic_config, engine)


@needs_mysql
@pytest.mark.parametrize("with_row", [False, True], ids=["empty", "with a row"])
def test_0042_downgrade_with_only_the_rebills_table_left(
    alembic_config: Config, engine: Engine, with_row: bool
) -> None:
    """Design §7 "降级保护": only `usage_rebills` (as after an upgrade cut between the two
    `CREATE TABLE`) — empty: only it is dropped; with a row: RuntimeError, it and its row stay."""
    _at_0042(alembic_config, engine)
    try:
        _execute(engine, f"DROP TABLE {_LINES}")
        if with_row:
            with _foreign_key_checks_off(engine) as connection:
                _insert_rebill(connection, {})
        before = _ours(engine)

        with _statements() as statements:
            if with_row:
                with pytest.raises(RuntimeError) as raised:
                    command.downgrade(alembic_config, _DOWN_REVISION)
                assert str(raised.value) == _NOT_EMPTY
            else:
                command.downgrade(alembic_config, _DOWN_REVISION)

        if with_row:
            assert _writes(statements) == []
            assert _version(engine) == _REVISION
            assert _ours(engine) == before
        else:
            assert [_normalised(s) for s in _writes(statements)] == [
                f"DROP TABLE {_REBILLS.upper()}"
            ]
            assert _version(engine) == _DOWN_REVISION
            assert _present(engine) == []
            assert _our_triggers(engine) == {}
    finally:
        _back_to_head(alembic_config, engine)


@needs_mysql
@pytest.mark.parametrize("with_row", [False, True], ids=["empty", "with a row"])
def test_0042_downgrade_with_only_the_lines_table_left(
    alembic_config: Config, engine: Engine, with_row: bool
) -> None:
    _at_0042(alembic_config, engine)
    try:
        with _foreign_key_checks_off(engine) as connection:
            connection.exec_driver_sql(f"DROP TABLE {_REBILLS}")
            if with_row:
                _insert_line(connection, {})
        before = _ours(engine)

        with _statements() as statements:
            if with_row:
                with pytest.raises(RuntimeError, match=f"^{_LINES} is not empty"):
                    command.downgrade(alembic_config, _DOWN_REVISION)
            else:
                command.downgrade(alembic_config, _DOWN_REVISION)

        if with_row:
            assert _writes(statements) == []
            assert _version(engine) == _REVISION
            assert _ours(engine) == before
        else:
            assert [_normalised(s) for s in _writes(statements)] == [f"DROP TABLE {_LINES.upper()}"]
            assert _version(engine) == _DOWN_REVISION
            assert _present(engine) == []
    finally:
        _back_to_head(alembic_config, engine)


@needs_mysql
def test_0042_downgrade_with_both_tables_gone_issues_no_ddl(
    alembic_config: Config, engine: Engine
) -> None:
    _at_0042(alembic_config, engine)
    try:
        _execute(engine, f"DROP TABLE {_LINES}, {_REBILLS}")

        with _statements() as statements:
            command.downgrade(alembic_config, _DOWN_REVISION)

        assert _writes(statements) == []
        assert _version(engine) == _DOWN_REVISION
        assert _our_triggers(engine) == {}
    finally:
        _back_to_head(alembic_config, engine)


@needs_mysql
def test_0042_offline_upgrade_and_downgrade(alembic_config: Config) -> None:
    """Design §8: `--sql` upgrade emits both tables and the four triggers without reading the
    database; the `--sql` downgrade fails saying it must run online, and its output has no DROP."""
    migration = _load()
    upgrade_buffer = io.StringIO()
    command.upgrade(
        Config("alembic.ini", output_buffer=upgrade_buffer),
        f"{_DOWN_REVISION}:{_REVISION}",
        sql=True,
    )
    emitted = _compact(upgrade_buffer.getvalue())
    expected = [
        _compact(migration._CREATE_REBILLS_MYSQL),
        _compact(migration._CREATE_LINES_MYSQL),
        *(_compact(migration._TRIGGERS[name]) for name in _DESIGN_TRIGGERS),
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
    assert _REBILLS in message
    assert "online" in message
    assert "DROP" not in _normalised(downgrade_buffer.getvalue())


@contextmanager
def _rolled_back(engine: Engine) -> Iterator[sqlalchemy.Connection]:
    """A connection whose writes are all rolled back; foreign key checks off (no parent row is
    needed then)."""
    with engine.connect() as connection:
        transaction = connection.begin()
        connection.exec_driver_sql("SET FOREIGN_KEY_CHECKS = 0")
        try:
            yield connection
        finally:
            transaction.rollback()
            connection.exec_driver_sql("SET FOREIGN_KEY_CHECKS = 1")


@needs_mysql
@pytest.mark.parametrize("row", list(_LEGAL_ROWS))
def test_0042_mysql_takes_the_legal_rows(alembic_config: Config, engine: Engine, row: str) -> None:
    _at_0042(alembic_config, engine)
    table, overrides = _LEGAL_ROWS[row]
    with _rolled_back(engine) as connection:
        _insert(connection, table, overrides)
        assert int(connection.execute(_count(table)).scalar_one()) == 1


@needs_mysql
def test_0042_mysql_takes_a_reason_of_only_no_break_spaces(
    alembic_config: Config, engine: Engine
) -> None:
    """The known edge (design §2): the database takes it; the confirm request refuses it."""
    _at_0042(alembic_config, engine)
    with _rolled_back(engine) as connection:
        _insert_rebill(connection, {"reason": _NO_BREAK_SPACES})
        stored = connection.execute(text(f"SELECT reason FROM {_REBILLS}")).scalar_one()
        assert stored == _NO_BREAK_SPACES


@needs_mysql
@pytest.mark.parametrize("row", list(_ILLEGAL_ROWS))
def test_0042_mysql_checks_refuse_the_illegal_rows(
    alembic_config: Config, engine: Engine, row: str
) -> None:
    """Design §7 "只追加": each illegal row on MySQL is refused (3819, the check named), as on
    SQLite. Everything is rolled back."""
    _at_0042(alembic_config, engine)
    table, overrides, check = _ILLEGAL_ROWS[row]
    with _rolled_back(engine) as connection:
        with pytest.raises(DBAPIError) as raised:
            with connection.begin_nested():
                _insert(connection, table, overrides)
        assert int(raised.value.orig.args[0]) == _ER_CHECK_CONSTRAINT_VIOLATED, row
        assert check in str(raised.value.orig.args[1]), row
        assert int(connection.execute(_count(table)).scalar_one()) == 0


@needs_mysql
@pytest.mark.parametrize("case", list(_DUPLICATES))
def test_0042_mysql_unique_keys_refuse_a_duplicate(
    alembic_config: Config, engine: Engine, case: str
) -> None:
    _at_0042(alembic_config, engine)
    table, overrides, unique, _ = _DUPLICATES[case]
    with _rolled_back(engine) as connection:
        _insert(connection, table, {}, 1)
        with pytest.raises(IntegrityError) as raised:
            with connection.begin_nested():
                _insert(connection, table, overrides, 2)
        assert int(raised.value.orig.args[0]) == _ER_DUP_ENTRY, case
        assert unique in str(raised.value.orig.args[1]), case
        assert int(connection.execute(_count(table)).scalar_one()) == 1


# Design §7 "只追加": an UPDATE, whatever it changes (nothing included), and the DELETE.
_UPDATES = {
    _REBILLS: [
        f"UPDATE {_REBILLS} SET reason = 'edited' WHERE id = :id",
        f"UPDATE {_REBILLS} SET corrected_total = 2.50, difference = -0.50 WHERE id = :id",
        f"UPDATE {_REBILLS} SET event_count = event_count WHERE id = :id",
    ],
    _LINES: [
        f"UPDATE {_LINES} SET corrected_charge = 1.10, difference = -0.40 WHERE id = :id",
        f"UPDATE {_LINES} SET original_period = '2026-09' WHERE id = :id",
        f"UPDATE {_LINES} SET usage_event_id = usage_event_id WHERE id = :id",
    ],
}


def _assert_immutable(engine: Engine, table: str, statement: str, row_id: int) -> None:
    with pytest.raises(DBAPIError) as raised:
        _execute(engine, statement, {"id": row_id})
    # 1644 is ER_SIGNAL_EXCEPTION: an unhandled SIGNAL SQLSTATE '45000'.
    assert int(raised.value.orig.args[0]) == _ER_SIGNAL_EXCEPTION, statement
    assert _IMMUTABLE[table] in str(raised.value.orig.args[1]), statement


@needs_mysql
def test_0042_triggers_refuse_update_and_delete_of_a_row(
    alembic_config: Config, engine: Engine
) -> None:
    """Design §7 "只追加": an UPDATE of a row of either table and its DELETE fail with SQLSTATE
    45000; the rows are unchanged."""
    _at_0042(alembic_config, engine)
    try:
        rebill_id = _commit_rebill_with_lines(engine)
        with engine.connect() as connection:
            line_ids = [
                int(row_id)
                for row_id in connection.execute(text(f"SELECT id FROM {_LINES} ORDER BY id"))
                .scalars()
                .all()
            ]
        before = _ours(engine)

        for line_id in line_ids:
            for update in _UPDATES[_LINES]:
                _assert_immutable(engine, _LINES, update, line_id)
            _assert_immutable(engine, _LINES, f"DELETE FROM {_LINES} WHERE id = :id", line_id)
        for update in _UPDATES[_REBILLS]:
            _assert_immutable(engine, _REBILLS, update, rebill_id)
        _assert_immutable(engine, _REBILLS, f"DELETE FROM {_REBILLS} WHERE id = :id", rebill_id)

        assert _ours(engine) == before
    finally:
        _truncate(engine)

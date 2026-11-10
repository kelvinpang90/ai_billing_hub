"""0036_payments（AIH-TASK-172，设计闸门 #580 v3）.

Design §2 "数据库" and "幂等的三层", §3 INV-3 / INV-6 / INV-13, §7 (the "迁移与约束" row, task
172), §8 (the migration item).

The first cases need no database: the revision chain; the columns, indexes, foreign keys and
checks of both tables in the migration equal the model's and the design's, under the same names
and in the same order; the two `CREATE TABLE` the MySQL upgrade sends; every branch of the
upgrade (both tables missing / either or both already there — refused, no DDL / `--sql`) and of
the downgrade (`--sql` refused / no table / a row in either table refused, no DDL / both empty —
events first, then payments), with `alembic.op` and the inspector stood in. The SQLite path of
the migration also runs for real through Alembic's `Operations` on an in-memory database, and
`create_all` on SQLite (what the unit tests run) builds both tables whose checks and unique
constraints refuse every illegal row.

On a real MySQL (`BILLING_TEST_DATABASE_URL`; skipped when unset, never skipped in CI): the
0035 → 0036 → 0035 → 0036 round trip on empty tables (every other table, key and check and the
wallet and ledger rows unchanged), a table already there (each case refused, no DDL, version
stays 0035), the downgrade with a row in either table, the `--sql` upgrade and downgrade, and
the checks and unique constraints refusing the illegal rows. The Alembic chain does not run on
SQLite (0006 onwards ask for `@@log_bin`), so every Alembic scenario is MySQL's, as in
test_monthly_statements_migration.py. Captured statements are compared after normalising
(backticks, case, spaces). No gateway is called: this task has no adapter.
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
from alembic.migration import MigrationContext
from alembic.operations import Operations
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
from app.models.payments import (
    Payment,
    PaymentEventProcessingStatus,
    PaymentEventVerificationStatus,
    PaymentGateway,
    PaymentGatewayEvent,
    PaymentStatus,
)

TEST_DATABASE_URL = os.environ.get("BILLING_TEST_DATABASE_URL", "")

needs_mysql = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="BILLING_TEST_DATABASE_URL is not set; migration tests need a real MySQL",
)

_REVISION = "0036_payments"
_DOWN_REVISION = "0035_monthly_statements"
_MIGRATION = pathlib.Path("alembic/versions/20261106_0036_payments.py")
_PAYMENTS = "payments"
_EVENTS = "payment_gateway_events"
_TABLES = (_PAYMENTS, _EVENTS)
_MODELS = {_PAYMENTS: Payment, _EVENTS: PaymentGatewayEvent}

# What 0037_payment_review (AIH-TASK-179) later adds to `payments`: four columns, an index, a
# foreign key and two checks. The model is head's shape, so the comparisons with 0036 leave
# them out; 0037 itself is test_payment_review_migration.py's.
_ADDED_BY_0037 = {
    "resolution",
    "resolved_at",
    "resolved_by_user_id",
    "resolution_reason",
    "ix_payments_resolved_by_user_id",
    "fk_payments_resolved_by_user_id",
    "ck_payments_resolution",
    "ck_payments_resolution_fields",
}

# Design §2 "数据库", table `payments`.
_DESIGN_PAYMENT_COLUMNS = {
    "id": ("BIGINT", False),
    "public_id": ("CHAR(36)", False),
    "tenant_id": ("BIGINT", False),
    "gateway": ("VARCHAR(16)", False),
    "expected_amount": ("DECIMAL(20,8)", False),
    "expected_currency": ("CHAR(3)", False),
    "wallet_credit_amount": ("DECIMAL(20,8)", False),
    "tax_policy_version_id": ("BIGINT", False),
    "tax_amount": ("DECIMAL(20,8)", True),
    "confirmed_amount": ("DECIMAL(20,8)", True),
    "confirmed_currency": ("CHAR(3)", True),
    "gateway_payment_id": ("VARCHAR(255)", True),
    "gateway_payment_intent_id": ("VARCHAR(255)", True),
    "gateway_fee": ("DECIMAL(20,8)", True),
    "status": ("VARCHAR(20)", False),
    "status_reason": ("VARCHAR(64)", True),
    "review_required_at": ("DATETIME", True),
    "expires_at": ("DATETIME", False),
    "paid_at": ("DATETIME", True),
    "last_reconciled_at": ("DATETIME", True),
    "created_at": ("DATETIME", False),
    "updated_at": ("DATETIME", False),
}
_DESIGN_PAYMENT_INDEXES = {
    "uq_payments_public_id": (("public_id",), True),
    "uq_payments_gateway_payment_id": (("gateway_payment_id",), True),
    "uq_payments_gateway_payment_intent_id": (("gateway_payment_intent_id",), True),
    "ix_payments_tenant_created": (("tenant_id", "created_at"), False),
    "ix_payments_status_created": (("status", "created_at"), False),
    "ix_payments_reuse": (("tenant_id", "status", "expected_amount", "created_at"), False),
    "ix_payments_tax_policy_version_id": (("tax_policy_version_id",), False),
    "ix_payments_review_required_at": (("review_required_at",), False),
}
_DESIGN_PAYMENT_FOREIGN_KEYS = {
    "fk_payments_tenant_id": (("tenant_id",), "tenants", ("id",), "RESTRICT"),
    "fk_payments_tax_policy_version_id": (
        ("tax_policy_version_id",),
        "tax_policy_versions",
        ("id",),
        "RESTRICT",
    ),
}
_PAYMENT_STATUSES = ("PENDING", "PAID", "FAILED", "EXPIRED", "CANCELLED", "REVIEW_REQUIRED")
_PAYMENT_STATUS_LIST = ", ".join(f"'{status}'" for status in _PAYMENT_STATUSES)
_DESIGN_PAYMENT_CHECKS = {
    "ck_payments_gateway": "gateway = 'STRIPE'",
    "ck_payments_expected_amount": (
        "expected_amount > 0 AND expected_amount = ROUND(expected_amount, 2)"
    ),
    "ck_payments_expected_currency": "expected_currency = 'MYR'",
    "ck_payments_wallet_credit_amount": "wallet_credit_amount = expected_amount",
    "ck_payments_tax_amount": "tax_amount IS NULL",
    "ck_payments_status": f"status IN ({_PAYMENT_STATUS_LIST})",
    "ck_payments_paid_fields": (
        "status <> 'PAID' OR (paid_at IS NOT NULL AND confirmed_amount IS NOT NULL"
        " AND confirmed_currency IS NOT NULL)"
    ),
    "ck_payments_review_at": "status <> 'REVIEW_REQUIRED' OR review_required_at IS NOT NULL",
}

# Design §2 "数据库", table `payment_gateway_events`.
_DESIGN_EVENT_COLUMNS = {
    "id": ("BIGINT", False),
    "gateway": ("VARCHAR(16)", False),
    "gateway_event_id": ("VARCHAR(255)", False),
    "event_type": ("VARCHAR(64)", False),
    "payment_id": ("BIGINT", True),
    "payload_fingerprint": ("CHAR(64)", False),
    "verification_status": ("VARCHAR(16)", False),
    "processing_status": ("VARCHAR(16)", False),
    "attempts": ("INTEGER", False),
    "next_attempt_at": ("DATETIME", False),
    "lease_until": ("DATETIME", True),
    "last_error_code": ("VARCHAR(64)", True),
    "fingerprint_conflict_at": ("DATETIME", True),
    "received_at": ("DATETIME", False),
    "processed_at": ("DATETIME", True),
    "metadata_json": ("JSON", False),
}
_DESIGN_EVENT_INDEXES = {
    "uq_payment_gateway_events_event": (("gateway", "gateway_event_id"), True),
    "ix_payment_gateway_events_due": (("processing_status", "next_attempt_at"), False),
    "ix_payment_gateway_events_payment_id": (("payment_id",), False),
    "ix_payment_gateway_events_received": (("received_at",), False),
}
_DESIGN_EVENT_FOREIGN_KEYS = {
    "fk_payment_gateway_events_payment_id": (("payment_id",), "payments", ("id",), "RESTRICT"),
}
_PROCESSING_STATUSES = (
    "RECEIVED",
    "PROCESSING",
    "RETRY",
    "PROCESSED",
    "IGNORED",
    "UNMATCHED",
    "REJECTED",
)
_PROCESSING_STATUS_LIST = ", ".join(f"'{status}'" for status in _PROCESSING_STATUSES)
_DESIGN_EVENT_CHECKS = {
    "ck_payment_gateway_events_gateway": "gateway = 'STRIPE'",
    "ck_payment_gateway_events_verification_status": "verification_status = 'VERIFIED'",
    "ck_payment_gateway_events_processing_status": (
        f"processing_status IN ({_PROCESSING_STATUS_LIST})"
    ),
}

_DESIGN = {
    _PAYMENTS: (
        _DESIGN_PAYMENT_COLUMNS,
        _DESIGN_PAYMENT_INDEXES,
        _DESIGN_PAYMENT_FOREIGN_KEYS,
        _DESIGN_PAYMENT_CHECKS,
    ),
    _EVENTS: (
        _DESIGN_EVENT_COLUMNS,
        _DESIGN_EVENT_INDEXES,
        _DESIGN_EVENT_FOREIGN_KEYS,
        _DESIGN_EVENT_CHECKS,
    ),
}

# MySQL reports a violated CHECK as 3819 (ER_CHECK_CONSTRAINT_VIOLATED), a duplicate key as
# 1062 (ER_DUP_ENTRY).
_ER_CHECK_CONSTRAINT_VIOLATED = 3819
_ER_DUP_ENTRY = 1062

_NOW = "2026-11-06 08:00:00"
_EXPIRES = "2026-11-06 09:00:00"

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

# Raw SQL: the values go past the enums and the Decimal type, straight to the database's checks.
_PAYMENT_INSERT_COLUMNS = [name for name in _DESIGN_PAYMENT_COLUMNS if name != "id"]
_INSERT_PAYMENT = text(
    f"INSERT INTO {_PAYMENTS} ({', '.join(_PAYMENT_INSERT_COLUMNS)})"
    f" VALUES ({', '.join(f':{name}' for name in _PAYMENT_INSERT_COLUMNS)})"
)
# `attempts` left out: its server default (0) fills it.
_EVENT_INSERT_COLUMNS = [name for name in _DESIGN_EVENT_COLUMNS if name not in {"id", "attempts"}]
_INSERT_EVENT = text(
    f"INSERT INTO {_EVENTS} ({', '.join(_EVENT_INSERT_COLUMNS)})"
    f" VALUES ({', '.join(f':{name}' for name in _EVENT_INSERT_COLUMNS)})"
)
_COUNT = {table: text(f"SELECT COUNT(*) FROM {table}") for table in _TABLES}
_ATTEMPTS = text(f"SELECT attempts FROM {_EVENTS}")


def _payment_params(overrides: dict[str, object]) -> dict[str, object]:
    """A legal PENDING payment of RM100.00, with `overrides` on top."""
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
    }
    params.update(overrides)
    return params


def _event_params(overrides: dict[str, object]) -> dict[str, object]:
    """A legal RECEIVED event, with `overrides` on top."""
    params: dict[str, object] = {
        "gateway": "STRIPE",
        "gateway_event_id": f"evt_{uuid.uuid4().hex}",
        "event_type": "checkout.session.completed",
        "payment_id": None,
        "payload_fingerprint": "0" * 64,
        "verification_status": "VERIFIED",
        "processing_status": "RECEIVED",
        "next_attempt_at": _NOW,
        "lease_until": None,
        "last_error_code": None,
        "fingerprint_conflict_at": None,
        "received_at": _NOW,
        "processed_at": None,
        "metadata_json": '{"type": "checkout.session.completed"}',
    }
    params.update(overrides)
    return params


_PAID = {
    "status": "PAID",
    "paid_at": _NOW,
    "confirmed_amount": "100.00",
    "confirmed_currency": "MYR",
}

# Illegal rows: (table, overrides on a legal row, the check that refuses it). Each breaks one
# check only.
_ILLEGAL_ROWS: dict[str, tuple[str, dict[str, object], str]] = {
    "a gateway other than STRIPE": (_PAYMENTS, {"gateway": "PAYPAL"}, "ck_payments_gateway"),
    "expected_amount 0": (
        _PAYMENTS,
        {"expected_amount": "0.00", "wallet_credit_amount": "0.00"},
        "ck_payments_expected_amount",
    ),
    "a negative expected_amount": (
        _PAYMENTS,
        {"expected_amount": "-10.00", "wallet_credit_amount": "-10.00"},
        "ck_payments_expected_amount",
    ),
    "expected_amount with three decimals": (
        _PAYMENTS,
        {"expected_amount": "10.001", "wallet_credit_amount": "10.001"},
        "ck_payments_expected_amount",
    ),
    "a currency other than MYR": (
        _PAYMENTS,
        {"expected_currency": "USD"},
        "ck_payments_expected_currency",
    ),
    "wallet_credit_amount not expected_amount": (
        _PAYMENTS,
        {"wallet_credit_amount": "99.99"},
        "ck_payments_wallet_credit_amount",
    ),
    "a tax_amount": (_PAYMENTS, {"tax_amount": "0.00"}, "ck_payments_tax_amount"),
    "an unknown payment status": (_PAYMENTS, {"status": "REFUNDED"}, "ck_payments_status"),
    "PAID without paid_at": (_PAYMENTS, {**_PAID, "paid_at": None}, "ck_payments_paid_fields"),
    "PAID without confirmed_amount": (
        _PAYMENTS,
        {**_PAID, "confirmed_amount": None},
        "ck_payments_paid_fields",
    ),
    "PAID without confirmed_currency": (
        _PAYMENTS,
        {**_PAID, "confirmed_currency": None},
        "ck_payments_paid_fields",
    ),
    "REVIEW_REQUIRED without review_required_at": (
        _PAYMENTS,
        {"status": "REVIEW_REQUIRED"},
        "ck_payments_review_at",
    ),
    "an event gateway other than STRIPE": (
        _EVENTS,
        {"gateway": "PAYPAL"},
        "ck_payment_gateway_events_gateway",
    ),
    "an unverified event": (
        _EVENTS,
        {"verification_status": "UNVERIFIED"},
        "ck_payment_gateway_events_verification_status",
    ),
    "an unknown processing status": (
        _EVENTS,
        {"processing_status": "DONE"},
        "ck_payment_gateway_events_processing_status",
    ),
}

# Legal rows next to them: the boundaries the checks let through. Several PENDING rows have no
# gateway ids: NULL does not collide in a unique index.
_LEGAL_ROWS: dict[str, tuple[str, dict[str, object]]] = {
    "PENDING": (_PAYMENTS, {}),
    "PENDING, the Stripe MYR minimum": (
        _PAYMENTS,
        {"expected_amount": "2.00", "wallet_credit_amount": "2.00"},
    ),
    "PENDING, one sen": (_PAYMENTS, {"expected_amount": "0.01", "wallet_credit_amount": "0.01"}),
    "PENDING, the Stripe maximum": (
        _PAYMENTS,
        {"expected_amount": "999999.99", "wallet_credit_amount": "999999.99"},
    ),
    "PAID with its three fields and a fee": (
        _PAYMENTS,
        {
            **_PAID,
            "gateway_payment_id": "cs_test_paid",
            "gateway_payment_intent_id": "pi_test_paid",
            "gateway_fee": "2.90",
        },
    ),
    "REVIEW_REQUIRED with review_required_at": (
        _PAYMENTS,
        {
            "status": "REVIEW_REQUIRED",
            "status_reason": "AMOUNT_MISMATCH",
            "review_required_at": _NOW,
            "confirmed_amount": "99.00",
            "confirmed_currency": "MYR",
            "gateway_payment_id": "cs_test_review",
        },
    ),
    "FAILED": (_PAYMENTS, {"status": "FAILED", "status_reason": "GATEWAY_FAILED"}),
    "EXPIRED": (_PAYMENTS, {"status": "EXPIRED", "status_reason": "GATEWAY_EXPIRED"}),
    "CANCELLED": (_PAYMENTS, {"status": "CANCELLED"}),
    **{
        f"event {status}": (_EVENTS, {"processing_status": status})
        for status in _PROCESSING_STATUSES
    },
}

# Unique constraints: (table, the first row, the second row, the constraint that refuses it).
_DUPLICATES: dict[str, tuple[str, dict[str, object], dict[str, object], str]] = {
    "the same public_id": (
        _PAYMENTS,
        {"public_id": "00000000-0000-4000-8000-000000000001"},
        {"public_id": "00000000-0000-4000-8000-000000000001"},
        "uq_payments_public_id",
    ),
    "the same Checkout Session": (
        _PAYMENTS,
        {"gateway_payment_id": "cs_test_same"},
        {"gateway_payment_id": "cs_test_same"},
        "uq_payments_gateway_payment_id",
    ),
    "the same PaymentIntent": (
        _PAYMENTS,
        {**_PAID, "gateway_payment_intent_id": "pi_test_same"},
        {**_PAID, "gateway_payment_intent_id": "pi_test_same"},
        "uq_payments_gateway_payment_intent_id",
    ),
    "the same gateway event": (
        _EVENTS,
        {"gateway_event_id": "evt_test_same"},
        {"gateway_event_id": "evt_test_same", "payload_fingerprint": "f" * 64},
        "uq_payment_gateway_events_event",
    ),
}


def _insert(connection: sqlalchemy.Connection, table: str, overrides: dict[str, object]) -> None:
    if table == _PAYMENTS:
        connection.execute(_INSERT_PAYMENT, _payment_params(overrides))
    else:
        connection.execute(_INSERT_EVENT, _event_params(overrides))


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("migration_0036", _MIGRATION)
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


def _model_columns(table_name: str) -> list[sqlalchemy.Column]:
    """The model's columns as of 0036 (0037's left out), in order."""
    return [c for c in _MODELS[table_name].__table__.columns if c.name not in _ADDED_BY_0037]


def _model_shape(table_name: str) -> dict[str, object]:
    """The model's columns, indexes, foreign keys and check names as of 0036, shaped like the
    migration's."""
    table = _MODELS[table_name].__table__
    indexes: dict[str, tuple[tuple[str, ...], bool]] = {}
    checks: set[str] = set()
    for constraint in table.constraints:
        if str(constraint.name) in _ADDED_BY_0037:
            continue
        if isinstance(constraint, UniqueConstraint):
            indexes[str(constraint.name)] = (tuple(c.name for c in constraint.columns), True)
        elif isinstance(constraint, CheckConstraint):
            checks.add(str(constraint.name))
    for index in table.indexes:
        if str(index.name) not in _ADDED_BY_0037:
            indexes[str(index.name)] = (tuple(c.name for c in index.columns), bool(index.unique))
    foreign_keys = {
        str(key.name): (
            tuple(key.column_keys),
            key.referred_table.name,
            tuple(element.column.name for element in key.elements),
            str(key.ondelete).upper(),
        )
        for key in table.foreign_key_constraints
        if str(key.name) not in _ADDED_BY_0037
    }
    return {
        "columns": {
            c.name: (_model_type_text(c), bool(c.nullable)) for c in _model_columns(table_name)
        },
        "indexes": indexes,
        "foreign_keys": foreign_keys,
        "checks": checks,
    }


def _design_shape(table: str) -> dict[str, object]:
    columns, indexes, foreign_keys, checks = _DESIGN[table]
    return {
        "columns": columns,
        "indexes": indexes,
        "foreign_keys": foreign_keys,
        "checks": set(checks),
    }


def _migration_shape(migration: ModuleType, table: str) -> dict[str, object]:
    columns, indexes, foreign_keys, checks = migration._SHAPES[table]
    return {
        "columns": columns,
        "indexes": indexes,
        "foreign_keys": foreign_keys,
        "checks": set(checks),
    }


# The design's MySQL tables, clause by clause.
_PAYMENT_CLAUSES = [
    "id BIGINT NOT NULL AUTO_INCREMENT",
    "public_id CHAR(36) NOT NULL",
    "tenant_id BIGINT NOT NULL",
    "gateway VARCHAR(16) NOT NULL",
    "expected_amount DECIMAL(20, 8) NOT NULL",
    "expected_currency CHAR(3) NOT NULL",
    "wallet_credit_amount DECIMAL(20, 8) NOT NULL",
    "tax_policy_version_id BIGINT NOT NULL",
    "tax_amount DECIMAL(20, 8) NULL",
    "confirmed_amount DECIMAL(20, 8) NULL",
    "confirmed_currency CHAR(3) NULL",
    "gateway_payment_id VARCHAR(255) NULL",
    "gateway_payment_intent_id VARCHAR(255) NULL",
    "gateway_fee DECIMAL(20, 8) NULL",
    "status VARCHAR(20) NOT NULL",
    "status_reason VARCHAR(64) NULL",
    "review_required_at DATETIME NULL",
    "expires_at DATETIME NOT NULL",
    "paid_at DATETIME NULL",
    "last_reconciled_at DATETIME NULL",
    "created_at DATETIME NOT NULL",
    "updated_at DATETIME NOT NULL",
    "PRIMARY KEY (id)",
    "CONSTRAINT uq_payments_public_id UNIQUE (public_id)",
    "CONSTRAINT uq_payments_gateway_payment_id UNIQUE (gateway_payment_id)",
    "CONSTRAINT uq_payments_gateway_payment_intent_id UNIQUE (gateway_payment_intent_id)",
    "INDEX ix_payments_tenant_created (tenant_id, created_at)",
    "INDEX ix_payments_status_created (status, created_at)",
    "INDEX ix_payments_reuse (tenant_id, status, expected_amount, created_at)",
    "INDEX ix_payments_tax_policy_version_id (tax_policy_version_id)",
    "INDEX ix_payments_review_required_at (review_required_at)",
    "CONSTRAINT fk_payments_tenant_id FOREIGN KEY (tenant_id) REFERENCES tenants (id)"
    " ON DELETE RESTRICT",
    "CONSTRAINT fk_payments_tax_policy_version_id FOREIGN KEY (tax_policy_version_id)"
    " REFERENCES tax_policy_versions (id) ON DELETE RESTRICT",
    *(f"CONSTRAINT {name} CHECK ({check})" for name, check in _DESIGN_PAYMENT_CHECKS.items()),
]
_EVENT_CLAUSES = [
    "id BIGINT NOT NULL AUTO_INCREMENT",
    "gateway VARCHAR(16) NOT NULL",
    "gateway_event_id VARCHAR(255) NOT NULL",
    "event_type VARCHAR(64) NOT NULL",
    "payment_id BIGINT NULL",
    "payload_fingerprint CHAR(64) NOT NULL",
    "verification_status VARCHAR(16) NOT NULL",
    "processing_status VARCHAR(16) NOT NULL",
    "attempts INTEGER NOT NULL DEFAULT 0",
    "next_attempt_at DATETIME NOT NULL",
    "lease_until DATETIME NULL",
    "last_error_code VARCHAR(64) NULL",
    "fingerprint_conflict_at DATETIME NULL",
    "received_at DATETIME NOT NULL",
    "processed_at DATETIME NULL",
    "metadata_json JSON NOT NULL",
    "PRIMARY KEY (id)",
    "CONSTRAINT uq_payment_gateway_events_event UNIQUE (gateway, gateway_event_id)",
    "INDEX ix_payment_gateway_events_due (processing_status, next_attempt_at)",
    "INDEX ix_payment_gateway_events_payment_id (payment_id)",
    "INDEX ix_payment_gateway_events_received (received_at)",
    "CONSTRAINT fk_payment_gateway_events_payment_id FOREIGN KEY (payment_id)"
    " REFERENCES payments (id) ON DELETE RESTRICT",
    *(f"CONSTRAINT {name} CHECK ({check})" for name, check in _DESIGN_EVENT_CHECKS.items()),
]
_DESIGN_DDL = {
    _PAYMENTS: f"CREATE TABLE {_PAYMENTS} ({', '.join(_PAYMENT_CLAUSES)})",
    _EVENTS: f"CREATE TABLE {_EVENTS} ({', '.join(_EVENT_CLAUSES)})",
}


# ---------------------------------------------------------------------------
# No database
# ---------------------------------------------------------------------------


def test_0036_follows_0035() -> None:
    migration = _load()

    assert migration.revision == _REVISION
    assert migration.down_revision == _DOWN_REVISION
    # `alembic_version.version_num` is VARCHAR(32).
    assert len(migration.revision) <= 32


@pytest.mark.parametrize("table", _TABLES)
def test_0036_shape_is_the_design_and_the_model(table: str) -> None:
    """The migration's frozen literals, the model's declarations and design §2 agree: columns
    (type, width, nullability, in this order), the unique and plain indexes in this column
    order, the foreign keys RESTRICT, the checks by name."""
    migration = _load()
    model = _MODELS[table]

    assert model.__tablename__ == table
    assert _migration_shape(migration, table) == _design_shape(table) == _model_shape(table)
    columns, *_ = migration._SHAPES[table]
    assert list(columns) == [c.name for c in _model_columns(table)]
    assert list(columns) == list(_DESIGN[table][0])
    assert list(migration._SHAPES) == list(_TABLES)


@pytest.mark.parametrize("table", _TABLES)
def test_0036_table_args_order_is_the_migrations(table: str) -> None:
    """`__table_args__` declares the unique constraints, indexes, foreign keys and checks under
    the migration's names and in its order (0037's come after them all)."""
    migration = _load()
    _, indexes, foreign_keys, checks = migration._SHAPES[table]
    kinds = UniqueConstraint | sqlalchemy.Index | ForeignKeyConstraint | CheckConstraint
    declared = [str(arg.name) for arg in _MODELS[table].__table_args__ if isinstance(arg, kinds)]
    ours = [name for name in declared if name not in _ADDED_BY_0037]

    assert ours == [*indexes, *foreign_keys, *checks]
    assert declared[: len(ours)] == ours


@pytest.mark.parametrize("table", _TABLES)
def test_0036_checks_are_the_ones_the_model_declares(table: str) -> None:
    """Same conditions in the migration, on the model and in the design (whitespace aside)."""
    migration = _load()
    *_, checks = migration._SHAPES[table]
    model = {
        str(constraint.name): _normalised(str(constraint.sqltext))
        for constraint in _MODELS[table].__table__.constraints
        if isinstance(constraint, CheckConstraint) and constraint.name not in _ADDED_BY_0037
    }

    assert {name: _normalised(c) for name, c in checks.items()} == model
    assert {name: _normalised(c) for name, c in _DESIGN[table][3].items()} == model


def test_0036_enums_are_the_designs_and_the_checks_allow_exactly_them() -> None:
    assert [m.value for m in PaymentStatus] == list(_PAYMENT_STATUSES)
    assert [m.value for m in PaymentEventProcessingStatus] == list(_PROCESSING_STATUSES)
    assert [m.value for m in PaymentGateway] == ["STRIPE"]
    assert [m.value for m in PaymentEventVerificationStatus] == ["VERIFIED"]

    migration = _load()
    allowed = ", ".join(f"'{m.value}'" for m in PaymentStatus)
    assert _normalised(migration._PAYMENT_CHECKS["ck_payments_status"]) == _normalised(
        f"status IN ({allowed})"
    )
    allowed = ", ".join(f"'{m.value}'" for m in PaymentEventProcessingStatus)
    assert _normalised(
        migration._EVENT_CHECKS["ck_payment_gateway_events_processing_status"]
    ) == _normalised(f"processing_status IN ({allowed})")


_ENUM_COLUMNS = {
    (_PAYMENTS, "gateway"): (PaymentGateway, 16),
    (_PAYMENTS, "status"): (PaymentStatus, 20),
    (_EVENTS, "gateway"): (PaymentGateway, 16),
    (_EVENTS, "verification_status"): (PaymentEventVerificationStatus, 16),
    (_EVENTS, "processing_status"): (PaymentEventProcessingStatus, 16),
}


@pytest.mark.parametrize("column", list(_ENUM_COLUMNS), ids=lambda c: ".".join(c))
def test_0036_status_columns_are_non_native_enums(column: tuple[str, str]) -> None:
    table, name = column
    enum_class, length = _ENUM_COLUMNS[column]
    kind = _MODELS[table].__table__.c[name].type

    assert isinstance(kind, sqlalchemy.Enum)
    assert kind.native_enum is False
    assert kind.enum_class is enum_class
    assert kind.length == length


def test_0036_attempts_defaults_to_zero() -> None:
    column = PaymentGatewayEvent.__table__.c.attempts
    assert column.server_default is not None
    assert str(column.server_default.arg) == "0"


@pytest.mark.parametrize("table", _TABLES)
def test_0036_mysql_create_table_is_the_design_one_statement(table: str) -> None:
    """The `CREATE TABLE` the MySQL upgrade sends is the design's clauses, checks included."""
    migration = _load()
    statement = migration._CREATE_TABLES_MYSQL[table]

    assert _compact(statement) == _compact(_DESIGN_DDL[table])
    assert statement.count(";") == 0


class _Result:
    def __init__(self, rows: list[tuple[object, ...]]) -> None:
        self._rows = rows

    def first(self) -> tuple[object, ...] | None:
        return self._rows[0] if self._rows else None


class _FakeBind:
    """Answers "any row?" for the tables named in `not_empty`."""

    def __init__(self, not_empty: set[str]) -> None:
        self._not_empty = not_empty
        self.queries: list[str] = []

    def execute(self, statement: object) -> _Result:
        sql = str(statement)
        self.queries.append(sql)
        for table in _TABLES:
            if sql == f"SELECT 1 FROM {table} LIMIT 1":
                return _Result([(1,)] if table in self._not_empty else [])
        raise AssertionError(sql)


class _FakeInspector:
    def __init__(self, present: set[str]) -> None:
        self._present = present
        self.asked: list[str] = []

    def has_table(self, name: str) -> bool:
        assert name in _TABLES, name
        self.asked.append(name)
        return name in self._present


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
        columns = [e.name for e in elements if isinstance(e, sqlalchemy.Column)]
        self.calls.append(("create_table", (name, columns)))

    def create_index(self, name: str, table: str, columns: list[str], unique: bool = False) -> None:
        self.calls.append(("create_index", (name, table, list(columns), unique)))

    def drop_table(self, name: str) -> None:
        self.calls.append(("drop_table", (name,)))


def _with(
    monkeypatch,
    present: set[str],
    *,
    dialect: str = "mysql",
    as_sql: bool = False,
    not_empty: set[str] | None = None,
) -> tuple[ModuleType, _FakeOp, _FakeInspector, _FakeBind]:
    migration = _load()
    bind = _FakeBind(not_empty or set())
    fake = _FakeOp(bind, dialect=dialect, as_sql=as_sql)
    inspector = _FakeInspector(present)
    monkeypatch.setattr(migration, "op", fake)
    monkeypatch.setattr(migration, "sa", _Sa(inspector))
    return migration, fake, inspector, bind


def test_0036_upgrade_creates_payments_then_the_events_table(monkeypatch) -> None:
    migration, fake, inspector, bind = _with(monkeypatch, set())
    migration.upgrade()

    assert fake.calls == [
        ("execute", (migration._CREATE_PAYMENTS_MYSQL,)),
        ("execute", (migration._CREATE_EVENTS_MYSQL,)),
    ]
    assert inspector.asked == list(_TABLES)
    assert bind.queries == []


def test_0036_upgrade_on_sqlite_creates_both_tables_and_their_indexes(monkeypatch) -> None:
    migration, fake, _, bind = _with(monkeypatch, set(), dialect="sqlite")
    migration.upgrade()

    expected: list[tuple[str, tuple[object, ...]]] = []
    for table in _TABLES:
        columns, indexes, _, _ = _DESIGN[table]
        expected.append(("create_table", (table, list(columns))))
        expected.extend(
            ("create_index", (name, table, list(index_columns), False))
            for name, (index_columns, unique) in indexes.items()
            if not unique
        )
    assert fake.calls == expected
    assert bind.queries == []


_PRESENT_CASES = {
    "payments": {_PAYMENTS},
    "payment_gateway_events": {_EVENTS},
    "both": {_PAYMENTS, _EVENTS},
}


@pytest.mark.parametrize("case", list(_PRESENT_CASES))
@pytest.mark.parametrize("dialect", ["mysql", "sqlite"])
def test_0036_upgrade_refuses_a_table_already_there_before_any_ddl(
    monkeypatch, case: str, dialect: str
) -> None:
    """Design §8: either table already there — refuse, no DDL (the missing one is not built
    either), the message names the table that is there (and not the one that is not)."""
    present = _PRESENT_CASES[case]
    migration, fake, _, bind = _with(monkeypatch, present, dialect=dialect)

    with pytest.raises(RuntimeError) as raised:
        migration.upgrade()

    assert fake.calls == []
    assert bind.queries == []
    message = str(raised.value)
    for table in _TABLES:
        assert (f"table {table} already exists" in message) == (table in present), table
    assert "No DDL was issued" in message
    assert "data incident" in message


@pytest.mark.parametrize("dialect", ["mysql", "sqlite"])
def test_0036_offline_upgrade_emits_both_tables_without_reading(monkeypatch, dialect: str) -> None:
    migration, fake, inspector, bind = _with(
        monkeypatch, {_PAYMENTS, _EVENTS}, dialect=dialect, as_sql=True
    )
    migration.upgrade()

    assert inspector.asked == []
    assert bind.queries == []
    created = [args[0] for kind, args in fake.calls if kind in {"execute", "create_table"}]
    if dialect == "mysql":
        assert created == [migration._CREATE_PAYMENTS_MYSQL, migration._CREATE_EVENTS_MYSQL]
    else:
        assert created == list(_TABLES)


def test_0036_offline_downgrade_refuses_before_reading_or_ddl(monkeypatch) -> None:
    """Design §8: offline the emptiness cannot be checked — refuse, no DROP."""
    migration, fake, inspector, bind = _with(monkeypatch, {_PAYMENTS, _EVENTS}, as_sql=True)

    with pytest.raises(RuntimeError) as raised:
        migration.downgrade()

    assert fake.calls == []
    assert inspector.asked == []
    assert bind.queries == []
    message = str(raised.value)
    assert _PAYMENTS in message and _EVENTS in message
    assert "online" in message


@pytest.mark.parametrize("dialect", ["mysql", "sqlite"])
def test_0036_downgrade_drops_the_events_table_then_payments(monkeypatch, dialect: str) -> None:
    migration, fake, _, bind = _with(monkeypatch, {_PAYMENTS, _EVENTS}, dialect=dialect)
    migration.downgrade()

    # Both checked before the first DROP.
    assert bind.queries == [f"SELECT 1 FROM {table} LIMIT 1" for table in _TABLES]
    assert fake.calls == [("drop_table", (_EVENTS,)), ("drop_table", (_PAYMENTS,))]


@pytest.mark.parametrize("table", _TABLES)
def test_0036_downgrade_skips_a_table_that_is_not_there(monkeypatch, table: str) -> None:
    migration, fake, _, bind = _with(monkeypatch, {table})
    migration.downgrade()

    assert bind.queries == [f"SELECT 1 FROM {table} LIMIT 1"]
    assert fake.calls == [("drop_table", (table,))]


@pytest.mark.parametrize("dialect", ["mysql", "sqlite"])
def test_0036_downgrade_without_the_tables_does_nothing(monkeypatch, dialect: str) -> None:
    migration, fake, inspector, bind = _with(monkeypatch, set(), dialect=dialect)
    migration.downgrade()

    assert inspector.asked == list(_TABLES)
    assert bind.queries == []
    assert fake.calls == []


@pytest.mark.parametrize("not_empty", _TABLES)
@pytest.mark.parametrize("dialect", ["mysql", "sqlite"])
def test_0036_downgrade_refuses_with_rows_before_any_ddl(
    monkeypatch, not_empty: str, dialect: str
) -> None:
    """Design §8: a row in either table — no DDL (the other, empty table is not dropped
    either), no row deleted; the message names the table that is not empty."""
    migration, fake, _, _ = _with(
        monkeypatch, {_PAYMENTS, _EVENTS}, dialect=dialect, not_empty={not_empty}
    )

    with pytest.raises(RuntimeError) as raised:
        migration.downgrade()

    assert fake.calls == []
    message = str(raised.value)
    assert f"table {not_empty} is not empty" in message
    assert "No DDL was issued and no row was deleted" in message


# ---------------------------------------------------------------------------
# SQLite
# ---------------------------------------------------------------------------


@contextmanager
def _sqlite() -> Iterator[Engine]:
    """The unit tests build SQLite with `create_all` from the models, not with the migrations.
    SQLite does not enforce foreign keys by default: the parent tables are not needed."""
    engine = create_engine("sqlite+pysqlite:///:memory:")
    try:
        Base.metadata.create_all(engine, tables=[Payment.__table__, PaymentGatewayEvent.__table__])
        yield engine
    finally:
        engine.dispose()


def _reflected_keys(engine: Engine, table: str) -> tuple[dict[str, object], dict[str, object]]:
    """Indexes (unique constraints included) and foreign keys, in the design's terms."""
    inspector = inspect(engine)
    indexes: dict[str, object] = {
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
    return indexes, keys


@pytest.mark.parametrize("table", _TABLES)
def test_0036_sqlite_tables_get_the_indexes_and_the_foreign_keys(table: str) -> None:
    with _sqlite() as engine:
        indexes, keys = _reflected_keys(engine, table)

    # The checks are judged by what they refuse (below), not by SQLite's reflection of them.
    # `create_all` builds head's shape: 0037's index and key are left out here.
    assert {k: v for k, v in indexes.items() if k not in _ADDED_BY_0037} == _DESIGN[table][1]
    assert {k: v for k, v in keys.items() if k not in _ADDED_BY_0037} == _DESIGN[table][2]


def test_0036_sqlite_migration_path_builds_refuses_and_drops(monkeypatch) -> None:
    """The migration's own non-MySQL branch, run through Alembic's `Operations` on SQLite:
    the upgrade builds both tables with the design's indexes and keys; a second upgrade is
    refused naming both; a row in payments makes the downgrade refuse; emptied, the downgrade
    drops both."""
    migration = _load()
    engine = create_engine("sqlite+pysqlite:///:memory:")
    try:
        with engine.begin() as connection:
            monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(connection)))
            migration.upgrade()
        for table in _TABLES:
            assert _reflected_keys(engine, table) == (_DESIGN[table][1], _DESIGN[table][2])
            assert [c["name"] for c in inspect(engine).get_columns(table)] == list(
                _DESIGN[table][0]
            )

        with engine.begin() as connection:
            monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(connection)))
            with pytest.raises(RuntimeError) as raised:
                migration.upgrade()
            assert "table payments already exists" in str(raised.value)
            assert "table payment_gateway_events already exists" in str(raised.value)

            _insert(connection, _PAYMENTS, {})
            with pytest.raises(RuntimeError, match="table payments is not empty"):
                migration.downgrade()
            assert set(inspect(connection).get_table_names()) >= set(_TABLES)

            connection.execute(text(f"DELETE FROM {_PAYMENTS}"))
            migration.downgrade()
        assert not set(inspect(engine).get_table_names()) & set(_TABLES)
    finally:
        engine.dispose()


def test_0036_sqlite_takes_the_legal_rows() -> None:
    with _sqlite() as engine:
        with engine.begin() as connection:
            for table, overrides in _LEGAL_ROWS.values():
                _insert(connection, table, overrides)
            counts = {t: int(connection.execute(_COUNT[t]).scalar_one()) for t in _TABLES}
            attempts = set(connection.execute(_ATTEMPTS).scalars())

    assert counts == {
        table: sum(1 for t, _ in _LEGAL_ROWS.values() if t == table) for table in _TABLES
    }
    assert attempts == {0}


@pytest.mark.parametrize("row", list(_ILLEGAL_ROWS))
def test_0036_sqlite_checks_refuse_the_illegal_rows(row: str) -> None:
    table, overrides, _ = _ILLEGAL_ROWS[row]
    with _sqlite() as engine:
        with pytest.raises(DBAPIError) as raised:
            with engine.begin() as connection:
                _insert(connection, table, overrides)
        assert "CHECK constraint failed" in str(raised.value), row

        with engine.connect() as connection:
            assert int(connection.execute(_COUNT[table]).scalar_one()) == 0


@pytest.mark.parametrize("case", list(_DUPLICATES))
def test_0036_sqlite_unique_constraints_refuse_the_second_row(case: str) -> None:
    table, first, second, _ = _DUPLICATES[case]
    with _sqlite() as engine:
        with engine.begin() as connection:
            _insert(connection, table, first)
        with pytest.raises(DBAPIError) as raised:
            with engine.begin() as connection:
                _insert(connection, table, second)
        assert "UNIQUE constraint failed" in str(raised.value), case

        with engine.connect() as connection:
            assert int(connection.execute(_COUNT[table]).scalar_one()) == 1


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
    """`CREATE TABLE PAYMENTS`, `DROP TABLE PAYMENT_GATEWAY_EVENTS`, …"""
    return [" ".join(_normalised(s).split(" ")[:3]) for s in statements]


def _version(engine: Engine) -> str:
    with engine.connect() as connection:
        return str(connection.execute(_ALEMBIC_VERSION).scalar_one())


def _execute(engine: Engine, statement: str, params: dict[str, object] | None = None) -> None:
    with engine.begin() as connection:
        connection.execute(text(statement), params or {})


def _present(engine: Engine) -> set[str]:
    return set(inspect(engine).get_table_names()) & set(_TABLES)


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
    for table in _TABLES:
        assert _table_shape(engine, table) == _model_shape(table) == _design_shape(table), table
        columns = [str(c["name"]) for c in inspect(engine).get_columns(table)]
        assert columns == list(_DESIGN[table][0]), table


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
    tables = snapshot["tables"]
    assert isinstance(tables, dict)
    checks = snapshot["checks"]
    assert isinstance(checks, set)
    return {
        "tables": {name: shape for name, shape in tables.items() if name not in _TABLES},
        "checks": {check for check in checks if check[0] not in _TABLES},
    }


def _ledger_and_wallets(engine: Engine) -> tuple[list[tuple[object, ...]], ...]:
    with engine.connect() as connection:
        wallets = [tuple(row) for row in connection.execute(_WALLET_ROWS).all()]
        ledger = [tuple(row) for row in connection.execute(_LEDGER_ROWS).all()]
    return wallets, ledger


def _at_0035(alembic_config: Config, engine: Engine) -> None:
    command.upgrade(alembic_config, "head")
    command.downgrade(alembic_config, _DOWN_REVISION)
    assert _version(engine) == _DOWN_REVISION
    assert _present(engine) == set()


def _empty(engine: Engine) -> None:
    """Rows the tests wrote: events first (they refer to payments). No trigger refuses DELETE."""
    present = _present(engine)
    with engine.begin() as connection:
        for table in reversed(_TABLES):
            if table in present:
                connection.execute(text(f"DELETE FROM {table}"))


def _back_to_head(alembic_config: Config, engine: Engine) -> None:
    """Empties the tables that are there, drops hand-made ones left at 0035 (events first), and
    upgrades to head again."""
    _empty(engine)
    if _version(engine) == _DOWN_REVISION:
        present = _present(engine)
        for table in reversed(_TABLES):
            if table in present:
                _execute(engine, f"DROP TABLE {table}")
    command.upgrade(alembic_config, "head")


@needs_mysql
def test_0036_round_trip_on_empty_tables(alembic_config: Config, engine: Engine) -> None:
    """Design §7 "迁移与约束" / §8: 0035 → 0036 → 0035 → 0036 with the tables empty. The upgrade
    sends one `CREATE TABLE` per table, payments first; the shapes are the model's and the
    design's; the downgrade drops the events table, then payments; every other table, key and
    check and the wallet and ledger rows are the same throughout."""
    _at_0035(alembic_config, engine)
    try:
        at_0035 = _schema_snapshot(engine)
        rows = _ledger_and_wallets(engine)

        with _ddl_statements() as statements:
            command.upgrade(alembic_config, _REVISION)
        assert _heads(statements) == [f"CREATE TABLE {table.upper()}" for table in _TABLES]
        assert _version(engine) == _REVISION
        _assert_design_shape(engine)
        at_0036 = _schema_snapshot(engine)
        assert _without_ours(at_0036) == at_0035
        assert _ledger_and_wallets(engine) == rows

        with _ddl_statements() as statements:
            command.downgrade(alembic_config, _DOWN_REVISION)
        assert [_normalised(s) for s in statements] == [
            f"DROP TABLE {_EVENTS.upper()}",
            f"DROP TABLE {_PAYMENTS.upper()}",
        ]
        assert _version(engine) == _DOWN_REVISION
        assert _present(engine) == set()
        assert _schema_snapshot(engine) == at_0035
        assert _ledger_and_wallets(engine) == rows

        command.upgrade(alembic_config, _REVISION)
        assert _schema_snapshot(engine) == at_0036
        _assert_design_shape(engine)
        assert _ledger_and_wallets(engine) == rows
    finally:
        _back_to_head(alembic_config, engine)


# Hand-made tables under our names: anything at all, the upgrade does not compare shapes.
_HAND_MADE = {table: f"CREATE TABLE {table} (id BIGINT NOT NULL PRIMARY KEY)" for table in _TABLES}


@needs_mysql
@pytest.mark.parametrize("case", list(_PRESENT_CASES))
def test_0036_upgrade_with_a_table_already_there_fails_before_any_ddl(
    alembic_config: Config, engine: Engine, case: str
) -> None:
    """Design §8: version 0035 and a table of ours already there (one or both) — the upgrade
    fails naming it, issues no DDL (the missing one is not built either), the version stays
    0035 and the schema is as it was."""
    _at_0035(alembic_config, engine)
    present = _PRESENT_CASES[case]
    try:
        for table in _TABLES:
            if table in present:
                _execute(engine, _HAND_MADE[table])
        before = _schema_snapshot(engine)

        with _ddl_statements() as statements:
            with pytest.raises(RuntimeError) as raised:
                command.upgrade(alembic_config, _REVISION)

        assert statements == []
        message = str(raised.value)
        for table in _TABLES:
            assert (f"table {table} already exists" in message) == (table in present), table
        assert "No DDL was issued" in message
        assert _version(engine) == _DOWN_REVISION
        assert _schema_snapshot(engine) == before
    finally:
        _back_to_head(alembic_config, engine)


@needs_mysql
@pytest.mark.parametrize("not_empty", _TABLES)
def test_0036_downgrade_with_a_row_refuses_before_any_ddl(
    alembic_config: Config, engine: Engine, not_empty: str
) -> None:
    """Design §8: a row in either table — the downgrade fails naming it, no DDL (the other table
    is not dropped either), the rows are as they were, `alembic_version` stays 0036. Taken at
    0036: the later migrations' downgrades (0037 drops its columns) are not the part under
    observation."""
    command.upgrade(alembic_config, "head")
    command.downgrade(alembic_config, _REVISION)
    assert _version(engine) == _REVISION
    try:
        with engine.begin() as connection:
            # No tenant or tax policy version is needed for this.
            connection.exec_driver_sql("SET FOREIGN_KEY_CHECKS = 0")
            try:
                _insert(connection, not_empty, {})
            finally:
                connection.exec_driver_sql("SET FOREIGN_KEY_CHECKS = 1")
        before = _schema_snapshot(engine)

        with _ddl_statements() as statements:
            with pytest.raises(RuntimeError) as raised:
                command.downgrade(alembic_config, _DOWN_REVISION)

        assert statements == []
        message = str(raised.value)
        assert f"table {not_empty} is not empty" in message
        assert "No DDL was issued and no row was deleted" in message
        assert _version(engine) == _REVISION
        assert _schema_snapshot(engine) == before
        with engine.connect() as connection:
            counts = {t: int(connection.execute(_COUNT[t]).scalar_one()) for t in _TABLES}
        assert counts == {table: int(table == not_empty) for table in _TABLES}
    finally:
        _back_to_head(alembic_config, engine)


@needs_mysql
def test_0036_offline_upgrade_and_downgrade(alembic_config: Config) -> None:
    """Design §8: `--sql` upgrade emits both `CREATE TABLE` without reading the database; the
    `--sql` downgrade fails saying it must run online, and its output has no DROP."""
    upgrade_buffer = io.StringIO()
    command.upgrade(
        Config("alembic.ini", output_buffer=upgrade_buffer),
        f"{_DOWN_REVISION}:{_REVISION}",
        sql=True,
    )
    emitted = _normalised(upgrade_buffer.getvalue())
    assert f"CREATE TABLE {_PAYMENTS.upper()} (" in emitted
    assert f"CREATE TABLE {_EVENTS.upper()} (" in emitted
    assert emitted.index(f"CREATE TABLE {_PAYMENTS.upper()} (") < emitted.index(
        f"CREATE TABLE {_EVENTS.upper()} ("
    )

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
    """A connection with foreign key checks off (no tenant or tax policy version is needed),
    whose writes are all rolled back."""
    with engine.connect() as connection:
        transaction = connection.begin()
        try:
            connection.exec_driver_sql("SET FOREIGN_KEY_CHECKS = 0")
            yield connection
        finally:
            transaction.rollback()
            connection.exec_driver_sql("SET FOREIGN_KEY_CHECKS = 1")


@needs_mysql
def test_0036_mysql_takes_the_legal_rows(alembic_config: Config, engine: Engine) -> None:
    command.upgrade(alembic_config, "head")
    with _mysql_rolled_back(engine) as connection:
        for table, overrides in _LEGAL_ROWS.values():
            _insert(connection, table, overrides)
        counts = {t: int(connection.execute(_COUNT[t]).scalar_one()) for t in _TABLES}
        attempts = {int(a) for a in connection.execute(_ATTEMPTS).scalars()}

    assert counts == {
        table: sum(1 for t, _ in _LEGAL_ROWS.values() if t == table) for table in _TABLES
    }
    assert attempts == {0}


@needs_mysql
@pytest.mark.parametrize("row", list(_ILLEGAL_ROWS))
def test_0036_mysql_checks_refuse_the_illegal_rows(
    alembic_config: Config, engine: Engine, row: str
) -> None:
    """Each check on MySQL: the illegal row is refused (3819, the check named). Everything is
    rolled back."""
    command.upgrade(alembic_config, "head")
    table, overrides, check = _ILLEGAL_ROWS[row]
    with _mysql_rolled_back(engine) as connection:
        with pytest.raises(DBAPIError) as raised:
            with connection.begin_nested():
                _insert(connection, table, overrides)
        assert int(raised.value.orig.args[0]) == _ER_CHECK_CONSTRAINT_VIOLATED, row
        assert check in str(raised.value.orig.args[1]), row
        assert int(connection.execute(_COUNT[table]).scalar_one()) == 0


@needs_mysql
@pytest.mark.parametrize("case", list(_DUPLICATES))
def test_0036_mysql_unique_constraints_refuse_the_second_row(
    alembic_config: Config, engine: Engine, case: str
) -> None:
    """Design §2 "幂等的三层", the two layers on these tables: the second row is refused (1062,
    the unique constraint named). Everything is rolled back."""
    command.upgrade(alembic_config, "head")
    table, first, second, constraint = _DUPLICATES[case]
    with _mysql_rolled_back(engine) as connection:
        _insert(connection, table, first)
        with pytest.raises(DBAPIError) as raised:
            with connection.begin_nested():
                _insert(connection, table, second)
        assert int(raised.value.orig.args[0]) == _ER_DUP_ENTRY, case
        assert constraint in str(raised.value.orig.args[1]), case
        assert int(connection.execute(_COUNT[table]).scalar_one()) == 1

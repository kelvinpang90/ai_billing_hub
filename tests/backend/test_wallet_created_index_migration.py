"""0027_wallet_tx_created_index（AIH-TASK-113，设计闸门 #435 v10）.

Design §2 "数据库", §3 INV-4 / INV-5, §5 (the migration row), §7 (the migration table), §8 step 1.

The first cases need no database: the revision chain; the index in the migration equals the model's
and the design's (name, column order, not unique); the upgrade's check (missing / design shape /
wrong shape) and the single `ALTER TABLE` it builds, and the downgrade that drops only what exists,
with `alembic.op` stood in; `create_all` on SQLite (what the unit tests run) builds it.

On a real MySQL (`BILLING_TEST_DATABASE_URL`; skipped when unset, never skipped in CI):
design §7's migration table — "迁移可重跑" ①–④, "索引可用" and "迁移往返"
(with the ledger's triggers and the wallet and ledger rows unchanged). The Alembic chain does not
run on SQLite (0006 onwards ask for `@@log_bin`), so every migration scenario is MySQL's, as in
test_migrations.py. Captured statements are compared after normalising (backticks, case, spaces).
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import os
import pathlib
import re
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from decimal import Decimal
from types import ModuleType, SimpleNamespace

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, delete, event, insert, inspect, text
from sqlalchemy.engine import Engine

from alembic import command

# 与 alembic/env.py 同一组模块：让 Base.metadata 装上全部的表，`create_all` 才建得出外键。
from app.ai_api import models as _ai_client_key_models  # noqa: F401
from app.ai_api import outbox as _ai_outbox_models  # noqa: F401
from app.models import auth as _auth_models  # noqa: F401
from app.models import tax_policy as _tax_policy_models  # noqa: F401
from app.models.base import Base
from app.models.tenancy import Tenant
from app.models.wallet import (
    REFERENCE_TYPE_FOR,
    WALLET_CURRENCY,
    TransactionType,
    Wallet,
    WalletTransaction,
)

TEST_DATABASE_URL = os.environ.get("BILLING_TEST_DATABASE_URL", "")

needs_mysql = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="BILLING_TEST_DATABASE_URL is not set; migration tests need a real MySQL",
)

_REVISION = "0027_wallet_tx_created_index"
_DOWN_REVISION = "0026_usage_customer_indexes"
_MIGRATION = pathlib.Path("alembic/versions/20261026_0027_wallet_transactions_created_index.py")
_TABLE = "wallet_transactions"

# Design §2 "数据库".
_INDEX = "ix_wallet_transactions_tenant_created"
_COLUMNS = ["tenant_id", "created_at", "wallet_sequence"]

# The ledger's triggers (0006); 0027 must leave every one of them as it is (INV-5).
_LEDGER_TRIGGERS = {
    "trg_wallet_transactions_before_insert",
    "trg_wallet_transactions_after_insert",
    "trg_wallet_transactions_before_update",
    "trg_wallet_transactions_before_delete",
    "trg_wallets_before_insert",
    "trg_wallets_before_update",
}

_NOW = dt.datetime(2026, 10, 26, 8, 30, 0)

_ALEMBIC_VERSION = text("SELECT version_num FROM alembic_version")
_SHOW_INDEX = text(f"SHOW INDEX FROM {_TABLE}")
_TRIGGERS_QUERY = text(
    "SELECT TRIGGER_NAME, EVENT_OBJECT_TABLE, ACTION_TIMING, EVENT_MANIPULATION, ACTION_STATEMENT"
    " FROM information_schema.TRIGGERS WHERE TRIGGER_SCHEMA = DATABASE()"
)
_CHECKS_QUERY = text(
    "SELECT CONSTRAINT_NAME FROM information_schema.CHECK_CONSTRAINTS"
    " WHERE CONSTRAINT_SCHEMA = DATABASE()"
)
_WALLET_ROWS = text("SELECT * FROM wallets ORDER BY id")
_LEDGER_ROWS = text("SELECT * FROM wallet_transactions ORDER BY id")

# Design §2 "接口二" reduced to the ledger: a tenant and a booking-date range, newest first.
_EXPLAIN_BY_DATE = text(
    "EXPLAIN SELECT public_id FROM wallet_transactions"
    " WHERE tenant_id = :tenant_id AND created_at >= :from_utc AND created_at < :to_utc"
    " ORDER BY wallet_sequence DESC LIMIT 20001"
)
_EXPLAIN_PARAMS = {
    "tenant_id": 1,
    "from_utc": dt.datetime(2026, 9, 30, 16, 0, 0),
    "to_utc": dt.datetime(2026, 10, 31, 16, 0, 0),
}


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("migration_0027", _MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _normalised(statement: str) -> str:
    return re.sub(r"\s+", " ", statement.replace("`", "").replace('"', "")).strip().upper()


def _added(statement: str) -> list[str]:
    return re.findall(r"\bADD INDEX (\w+)", _normalised(statement))


def _dropped(statement: str) -> list[str]:
    return re.findall(r"\bDROP INDEX (\w+)", _normalised(statement))


# ---------------------------------------------------------------------------
# No database
# ---------------------------------------------------------------------------


def test_0027_follows_0026() -> None:
    migration = _load()

    assert migration.revision == _REVISION
    assert migration.down_revision == _DOWN_REVISION
    # `alembic_version.version_num` is VARCHAR(32).
    assert len(migration.revision) <= 32


def test_0027_index_is_the_one_the_model_declares() -> None:
    """The migration's frozen literal, the model's `__table_args__` and design §2 agree; the
    model's other ledger indexes are still there."""
    migration = _load()
    model = {
        str(index.name): ([column.name for column in index.columns], bool(index.unique))
        for index in WalletTransaction.__table__.indexes
    }

    assert migration._TABLE == _TABLE
    assert (migration._INDEX, migration._COLUMNS) == (_INDEX, _COLUMNS)
    assert model[_INDEX] == (_COLUMNS, False)
    assert model["ix_wallet_transactions_tenant_sequence"] == (
        ["tenant_id", "wallet_sequence"],
        False,
    )
    assert model["ix_wallet_transactions_tenant_reference_sequence"] == (
        ["tenant_id", "reference_type", "wallet_sequence"],
        False,
    )


class _FakeOp:
    """Stands in for `alembic.op`: a dialect, online or `--sql`, and the calls it got."""

    def __init__(self, dialect: str = "mysql", as_sql: bool = False) -> None:
        self.calls: list[tuple[str, tuple[object, ...]]] = []
        self._context = SimpleNamespace(as_sql=as_sql, dialect=SimpleNamespace(name=dialect))

    def get_context(self) -> SimpleNamespace:
        return self._context

    def get_bind(self) -> object:
        return object()

    def execute(self, statement: str) -> None:
        self.calls.append(("execute", (statement,)))

    def create_index(self, name: str, table: str, columns: list[str], unique: bool = False) -> None:
        self.calls.append(("create_index", (name, table, list(columns), unique)))

    def drop_index(self, name: str, table_name: str) -> None:
        self.calls.append(("drop_index", (name, table_name)))


def _reflected(name: str, columns: list[str], unique: bool = False, **options: object) -> dict:
    """An index the way `Inspector.get_indexes` returns it."""
    index: dict = {"name": name, "column_names": columns, "unique": unique}
    if options:
        index["dialect_options"] = dict(options)
    return index


def _design() -> dict:
    return _reflected(_INDEX, list(_COLUMNS))


# Indexes that are not ours to judge, whatever their columns.
_UNRELATED = [
    _reflected("ix_wallet_transactions_tenant_sequence", ["tenant_id", "wallet_sequence"]),
    _reflected(
        "ix_wallet_transactions_tenant_reference_sequence",
        ["tenant_id", "reference_type", "wallet_sequence"],
    ),
]


def _with(monkeypatch, fake: _FakeOp, indexes: list[dict]) -> ModuleType:
    migration = _load()
    asked: list[str] = []

    def get_indexes(table: str) -> list[dict]:
        asked.append(table)
        return indexes

    monkeypatch.setattr(migration, "op", fake)
    monkeypatch.setattr(
        migration,
        "sa",
        SimpleNamespace(inspect=lambda _bind: SimpleNamespace(get_indexes=get_indexes)),
    )
    migration.asked = asked
    return migration


def test_0027_upgrade_adds_the_missing_index_in_one_alter_table(monkeypatch) -> None:
    fake = _FakeOp()
    migration = _with(monkeypatch, fake, list(_UNRELATED))
    migration.upgrade()

    assert migration.asked == [_TABLE]
    [(kind, (statement,))] = fake.calls
    assert kind == "execute"
    assert _normalised(statement).startswith(f"ALTER TABLE {_TABLE.upper()} ")
    assert _added(statement) == [_INDEX.upper()]
    columns = ", ".join(_COLUMNS).upper()
    assert f"ADD INDEX {_INDEX.upper()} ({columns})" in _normalised(statement)


@pytest.mark.parametrize("dialect", ["mysql", "sqlite"])
def test_0027_upgrade_with_the_design_shape_issues_no_ddl(monkeypatch, dialect: str) -> None:
    fake = _FakeOp(dialect=dialect)
    migration = _with(monkeypatch, fake, [*_UNRELATED, _design()])
    migration.upgrade()

    assert fake.calls == []


_WRONG_SHAPES = {
    "other columns": _reflected(_INDEX, ["tenant_id", "created_at"]),
    "other order": _reflected(_INDEX, ["created_at", "tenant_id", "wallet_sequence"]),
    "unique": _reflected(_INDEX, list(_COLUMNS), unique=True),
    # The same column names, the prefix length only in the dialect options.
    "prefix length": _reflected(_INDEX, list(_COLUMNS), mysql_length={"wallet_sequence": 4}),
}


@pytest.mark.parametrize("shape", list(_WRONG_SHAPES))
@pytest.mark.parametrize("dialect", ["mysql", "sqlite"])
def test_0027_upgrade_refuses_a_wrong_shape_before_any_ddl(
    monkeypatch, shape: str, dialect: str
) -> None:
    """Design §2 "数据库": a same-named index of another shape — nothing is built, the message
    names the index and how to clear it."""
    fake = _FakeOp(dialect=dialect)
    migration = _with(monkeypatch, fake, [*_UNRELATED, _WRONG_SHAPES[shape]])

    with pytest.raises(RuntimeError) as raised:
        migration.upgrade()

    assert fake.calls == []
    message = str(raised.value)
    assert _INDEX in message
    assert f"DROP INDEX {_INDEX} ON {_TABLE}" in message


def test_0027_upgrade_on_sqlite_creates_the_missing_index(monkeypatch) -> None:
    fake = _FakeOp(dialect="sqlite")
    migration = _with(monkeypatch, fake, list(_UNRELATED))
    migration.upgrade()

    assert fake.calls == [("create_index", (_INDEX, _TABLE, _COLUMNS, False))]


def test_0027_offline_upgrade_and_downgrade_emit_it_without_reading(monkeypatch) -> None:
    fake = _FakeOp(as_sql=True)
    migration = _with(monkeypatch, fake, [])
    migration.upgrade()
    migration.downgrade()

    assert migration.asked == []
    [(_, (added,)), (_, (dropped,))] = fake.calls
    assert _added(added) == [_INDEX.upper()]
    assert _dropped(dropped) == [_INDEX.upper()]


def test_0027_downgrade_drops_it_in_one_alter_table_when_it_exists(monkeypatch) -> None:
    fake = _FakeOp()
    migration = _with(monkeypatch, fake, [*_UNRELATED, _design()])
    migration.downgrade()

    [(kind, (statement,))] = fake.calls
    assert kind == "execute"
    assert _normalised(statement).startswith(f"ALTER TABLE {_TABLE.upper()} ")
    assert _dropped(statement) == [_INDEX.upper()]


@pytest.mark.parametrize("dialect", ["mysql", "sqlite"])
def test_0027_downgrade_without_it_issues_no_ddl(monkeypatch, dialect: str) -> None:
    fake = _FakeOp(dialect=dialect)
    migration = _with(monkeypatch, fake, list(_UNRELATED))
    migration.downgrade()

    assert migration.asked == [_TABLE]
    assert fake.calls == []


def test_0027_downgrade_on_sqlite_drops_it(monkeypatch) -> None:
    fake = _FakeOp(dialect="sqlite")
    migration = _with(monkeypatch, fake, [_design()])
    migration.downgrade()

    assert fake.calls == [("drop_index", (_INDEX, _TABLE))]


def test_0027_sqlite_tables_get_the_index_in_order_and_not_unique() -> None:
    """The unit tests build SQLite with `create_all` from the models, not with the migrations
    (0006's precheck and triggers are MySQL's); this is the SQLite side of design §7."""
    engine = create_engine("sqlite+pysqlite:///:memory:")
    try:
        Base.metadata.create_all(engine)
        indexes = {
            str(index["name"]): (list(index["column_names"]), bool(index["unique"]))
            for index in inspect(engine).get_indexes(_TABLE)
        }
    finally:
        engine.dispose()

    assert indexes[_INDEX] == (_COLUMNS, False)


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


def _index_shapes(engine: Engine) -> dict[str, tuple[list[str], bool, object]]:
    """Name → (columns, unique, prefix lengths) of every index on `wallet_transactions`."""
    return {
        str(index["name"]): (
            list(index["column_names"]),
            bool(index["unique"]),
            (index.get("dialect_options") or {}).get("mysql_length"),
        )
        for index in inspect(engine).get_indexes(_TABLE)
    }


def _assert_design_shape(engine: Engine) -> None:
    assert _index_shapes(engine).get(_INDEX) == (_COLUMNS, False, None)


def _assert_model_indexes(engine: Engine, *, with_ours: bool) -> None:
    """Every index the model declares on `wallet_transactions` is reflected in its declared
    shape — ours only when `with_ours`, and then it is absent otherwise."""
    shapes = _index_shapes(engine)
    for index in WalletTransaction.__table__.indexes:
        if str(index.name) == _INDEX and not with_ours:
            assert _INDEX not in shapes
            continue
        declared = ([column.name for column in index.columns], bool(index.unique), None)
        assert shapes.get(str(index.name)) == declared, index.name


def _execute(engine: Engine, statement: str) -> None:
    with engine.begin() as connection:
        connection.execute(text(statement))


def _drop_if_exists(engine: Engine) -> None:
    if _INDEX in _index_shapes(engine):
        _execute(engine, f"DROP INDEX {_INDEX} ON {_TABLE}")


def _at_0026(alembic_config: Config, engine: Engine) -> None:
    command.upgrade(alembic_config, "head")
    command.downgrade(alembic_config, _DOWN_REVISION)
    assert _version(engine) == _DOWN_REVISION
    assert _INDEX not in _index_shapes(engine)


def _back_to_head(alembic_config: Config, engine: Engine) -> None:
    # A wrong-shaped leftover would make the upgrade refuse; the right-shaped one is kept.
    shapes = _index_shapes(engine)
    if _INDEX in shapes and shapes[_INDEX] != (_COLUMNS, False, None):
        _drop_if_exists(engine)
    command.upgrade(alembic_config, "head")


@needs_mysql
def test_0027_upgrade_sends_one_alter_table(alembic_config: Config, engine: Engine) -> None:
    """Design §7 "迁移可重跑" ①: from 0026 to 0027, one `ALTER TABLE wallet_transactions ADD
    INDEX`."""
    _at_0026(alembic_config, engine)
    try:
        with _ddl_statements() as statements:
            command.upgrade(alembic_config, _REVISION)

        assert len(statements) == 1, statements
        assert _normalised(statements[0]).startswith(f"ALTER TABLE {_TABLE.upper()} ")
        assert _added(statements[0]) == [_INDEX.upper()]
        assert _version(engine) == _REVISION
        _assert_design_shape(engine)
    finally:
        _back_to_head(alembic_config, engine)


@needs_mysql
def test_0027_rerun_with_the_index_built_only_moves_the_version(
    alembic_config: Config, engine: Engine
) -> None:
    """Design §7 "迁移可重跑" ②: built by hand in the design shape, the version still 0026
    (the process died between `ALTER TABLE` and `alembic_version`) — no DDL, the version moves
    on."""
    _at_0026(alembic_config, engine)
    try:
        _execute(engine, f"ALTER TABLE {_TABLE} ADD INDEX {_INDEX} ({', '.join(_COLUMNS)})")
        before = _index_shapes(engine)

        with _ddl_statements() as statements:
            command.upgrade(alembic_config, _REVISION)

        assert statements == []
        assert _version(engine) == _REVISION
        assert _index_shapes(engine) == before
        _assert_design_shape(engine)
    finally:
        _back_to_head(alembic_config, engine)


# `created_at` and `wallet_sequence` are not strings, so MySQL takes no prefix length on them;
# the prefix-length shape is covered by the unit case above.
_WRONG_SHAPE_DDL = {
    "other columns": f"CREATE INDEX {_INDEX} ON {_TABLE} (tenant_id, created_at)",
    "other order": (f"CREATE INDEX {_INDEX} ON {_TABLE} (created_at, tenant_id, wallet_sequence)"),
    "unique": (
        f"CREATE UNIQUE INDEX {_INDEX} ON {_TABLE} (tenant_id, created_at, wallet_sequence)"
    ),
}


@needs_mysql
@pytest.mark.parametrize("shape", list(_WRONG_SHAPE_DDL))
def test_0027_rerun_with_a_wrong_shape_fails_before_any_ddl(
    alembic_config: Config, engine: Engine, shape: str
) -> None:
    """Design §7 "迁移可重跑" ③: a hand-made index named like ours but shaped otherwise — the
    upgrade fails naming it and the fix, issues no DDL, the version stays 0026; after dropping
    the hand-made one the upgrade succeeds."""
    _at_0026(alembic_config, engine)
    try:
        _execute(engine, _WRONG_SHAPE_DDL[shape])
        hand_made = _index_shapes(engine)[_INDEX]

        with _ddl_statements() as statements:
            with pytest.raises(RuntimeError) as raised:
                command.upgrade(alembic_config, _REVISION)

        assert statements == []
        message = str(raised.value)
        assert _INDEX in message
        assert f"DROP INDEX {_INDEX} ON {_TABLE}" in message
        assert _version(engine) == _DOWN_REVISION
        assert _index_shapes(engine)[_INDEX] == hand_made

        _drop_if_exists(engine)
        command.upgrade(alembic_config, _REVISION)
        assert _version(engine) == _REVISION
        _assert_design_shape(engine)
    finally:
        _back_to_head(alembic_config, engine)


@needs_mysql
def test_0027_downgrade_after_a_manual_drop_issues_no_ddl(
    alembic_config: Config, engine: Engine
) -> None:
    """Design §7 "迁移可重跑" ④: at 0027 with the index dropped by hand, the downgrade issues no
    DDL, does not fail, and the version becomes 0026."""
    command.upgrade(alembic_config, "head")
    try:
        # Later revisions are not this downgrade's: start at 0027.
        command.downgrade(alembic_config, _REVISION)
        assert _version(engine) == _REVISION
        _drop_if_exists(engine)

        with _ddl_statements() as statements:
            command.downgrade(alembic_config, _DOWN_REVISION)

        assert statements == []
        assert _version(engine) == _DOWN_REVISION
        assert _INDEX not in _index_shapes(engine)
    finally:
        _back_to_head(alembic_config, engine)


@needs_mysql
def test_0027_downgrade_drops_it_in_one_alter_table(alembic_config: Config, engine: Engine) -> None:
    """The other half of ④: with the index there, one `ALTER TABLE … DROP INDEX`."""
    command.upgrade(alembic_config, "head")
    try:
        command.downgrade(alembic_config, _REVISION)

        with _ddl_statements() as statements:
            command.downgrade(alembic_config, _DOWN_REVISION)

        assert len(statements) == 1, statements
        assert _normalised(statements[0]).startswith(f"ALTER TABLE {_TABLE.upper()} ")
        assert _dropped(statements[0]) == [_INDEX.upper()]
        assert _version(engine) == _DOWN_REVISION
        assert _INDEX not in _index_shapes(engine)
    finally:
        _back_to_head(alembic_config, engine)


@needs_mysql
def test_0027_index_exists_at_head_in_order_and_is_a_possible_key(
    alembic_config: Config, engine: Engine
) -> None:
    """Design §7 "索引可用": `SHOW INDEX` lists it in the design's column order, not unique,
    without prefix lengths; it is a possible key for a tenant's booking-date range. Which key the
    optimizer finally picks is not asserted: on a near-empty table that is not stable."""
    command.upgrade(alembic_config, "head")
    with engine.connect() as connection:
        rows = connection.execute(_SHOW_INDEX).mappings().all()
        plan = connection.execute(_EXPLAIN_BY_DATE, _EXPLAIN_PARAMS).mappings().all()

    ours = sorted(
        (row for row in rows if row["Key_name"] == _INDEX), key=lambda row: row["Seq_in_index"]
    )
    assert [row["Column_name"] for row in ours] == _COLUMNS
    assert [int(row["Non_unique"]) for row in ours] == [1, 1, 1]
    assert [row["Sub_part"] for row in ours] == [None, None, None]

    [row] = [row for row in plan if row["table"] == _TABLE]
    possible_keys = str(row["possible_keys"] or "").split(",")
    assert _INDEX in possible_keys, possible_keys


def _schema_snapshot(engine: Engine) -> dict[str, dict[str, object]]:
    """Every table's columns, indexes (with prefix lengths), unique constraints and foreign keys."""
    inspector = inspect(engine)
    snapshot: dict[str, dict[str, object]] = {}
    for table in sorted(inspector.get_table_names()):
        snapshot[table] = {
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
    return snapshot


def _triggers_and_checks(engine: Engine) -> tuple[set[tuple[object, ...]], set[str]]:
    with engine.connect() as connection:
        triggers = {tuple(row) for row in connection.execute(_TRIGGERS_QUERY).all()}
        checks = set(connection.execute(_CHECKS_QUERY).scalars())
    return triggers, checks


def _ledger_and_wallets(
    engine: Engine,
) -> tuple[list[tuple[object, ...]], list[tuple[object, ...]]]:
    with engine.connect() as connection:
        wallets = [tuple(row) for row in connection.execute(_WALLET_ROWS).all()]
        ledger = [tuple(row) for row in connection.execute(_LEDGER_ROWS).all()]
    return wallets, ledger


def _seed_ledger(engine: Engine) -> tuple[int, int]:
    """A tenant, its wallet and two ledger rows (a top-up and a usage debit), written the way the
    triggers require (`balance_before` = the wallet's balance, `wallet_sequence` = `version + 1`);
    the AFTER INSERT trigger moves the wallet. Returns (tenant id, wallet id)."""
    with engine.begin() as connection:
        tenant_id = int(
            connection.execute(
                insert(Tenant).values(
                    public_id=str(uuid.uuid4()),
                    company_name="Wallet Index Migration Sdn Bhd",
                    email="ops@example.com",
                    created_at=_NOW,
                    updated_at=_NOW,
                )
            ).inserted_primary_key[0]
        )
        wallet_id = int(
            connection.execute(
                insert(Wallet).values(
                    tenant_id=tenant_id,
                    currency=WALLET_CURRENCY,
                    balance=Decimal(0),
                    version=0,
                    created_at=_NOW,
                    updated_at=_NOW,
                )
            ).inserted_primary_key[0]
        )
        postings = [
            (TransactionType.TOPUP, Decimal("10.00000000"), Decimal(0)),
            (TransactionType.AI_USAGE, Decimal("-0.04120000"), Decimal("10.00000000")),
        ]
        for sequence, (kind, amount, before) in enumerate(postings, start=1):
            connection.execute(
                insert(WalletTransaction).values(
                    public_id=str(uuid.uuid4()),
                    wallet_id=wallet_id,
                    tenant_id=tenant_id,
                    wallet_sequence=sequence,
                    transaction_type=kind,
                    amount=amount,
                    balance_before=before,
                    balance_after=before + amount,
                    reference_type=REFERENCE_TYPE_FOR[kind],
                    reference_id=str(uuid.uuid4()),
                    created_at=_NOW + dt.timedelta(minutes=sequence),
                )
            )
    return tenant_id, wallet_id


def _clean_ledger(engine: Engine, tenant_id: int, wallet_id: int) -> None:
    """The ledger refuses DELETE (0006's triggers), so it is emptied with TRUNCATE, which is DDL
    and skips them (as test_wallet_repository.py does); then the wallet and the tenant go."""
    with engine.begin() as connection:
        # 0018 起 usage_events 有外键引用账本：被引用的表要关掉外键检查才能 TRUNCATE。
        connection.execute(text("SET FOREIGN_KEY_CHECKS = 0"))
        try:
            connection.execute(text(f"TRUNCATE TABLE {_TABLE}"))
        finally:
            connection.execute(text("SET FOREIGN_KEY_CHECKS = 1"))
    with engine.begin() as connection:
        connection.execute(delete(Wallet).where(Wallet.id == wallet_id))
        connection.execute(delete(Tenant).where(Tenant.id == tenant_id))


@needs_mysql
def test_0027_round_trip_changes_only_the_index_and_no_row(
    alembic_config: Config, engine: Engine
) -> None:
    """Design §7 "迁移往返" (§3 INV-4 / INV-5): upgrade → downgrade → upgrade builds, drops and
    rebuilds the index; at every step the reflected ledger indexes are the model's; every other
    table, column, index, key, check and trigger is the same throughout, the ledger's six
    triggers are still there, and the wallet and ledger rows are the same."""
    command.upgrade(alembic_config, "head")
    seeded: tuple[int, int] | None = None
    try:
        # Later revisions are not this round trip's: compare at 0027.
        command.downgrade(alembic_config, _REVISION)
        seeded = _seed_ledger(engine)
        at_0027 = _schema_snapshot(engine)
        others = _triggers_and_checks(engine)
        rows = _ledger_and_wallets(engine)
        wallets, ledger = rows
        assert len(ledger) >= 2 and len(wallets) >= 1
        assert _LEDGER_TRIGGERS <= {str(trigger[0]) for trigger in others[0]}
        _assert_design_shape(engine)
        _assert_model_indexes(engine, with_ours=True)

        command.downgrade(alembic_config, _DOWN_REVISION)
        without = _schema_snapshot(engine)
        expected = {table: dict(shape) for table, shape in at_0027.items()}
        expected[_TABLE]["indexes"] = [
            index for index in at_0027[_TABLE]["indexes"] if index[0] != _INDEX
        ]
        assert without == expected
        assert _triggers_and_checks(engine) == others
        assert _ledger_and_wallets(engine) == rows
        _assert_model_indexes(engine, with_ours=False)

        command.upgrade(alembic_config, _REVISION)
        assert _schema_snapshot(engine) == at_0027
        assert _triggers_and_checks(engine) == others
        assert _ledger_and_wallets(engine) == rows
        _assert_design_shape(engine)
        _assert_model_indexes(engine, with_ours=True)
    finally:
        _back_to_head(alembic_config, engine)
        if seeded is not None:
            _clean_ledger(engine, *seeded)

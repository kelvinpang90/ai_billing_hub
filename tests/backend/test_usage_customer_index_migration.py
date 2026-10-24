"""0026_usage_customer_indexes（AIH-TASK-103，设计闸门 #398 v4 §2「数据库」、§5、§7、§8 第 1 步）.

The first cases need no database: the revision chain; the two indexes in the migration equal the
model's and the design's (name, column order, not unique); the upgrade's check (missing / design
shape / wrong shape) and the single `ALTER TABLE` it builds, and the downgrade that drops only what
exists, with `alembic.op` stood in; `create_all` on SQLite (what the unit tests run) builds both.

On a real MySQL (`BILLING_TEST_DATABASE_URL`; skipped when unset, never skipped in CI): design §7
"迁移可重跑" ①–③ and the contract's ④a / ④b, "索引存在且可用" and "迁移往返". The Alembic chain does
not run on SQLite (0006 onwards ask for `@@log_bin`), so every migration scenario is MySQL's, as in
test_migrations.py. Captured statements are compared after normalising (backticks, case, spaces).
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import os
import pathlib
import re
from collections.abc import Iterator
from contextlib import contextmanager
from types import ModuleType, SimpleNamespace

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.engine import Engine

from alembic import command

# 与 alembic/env.py 同一组模块：让 Base.metadata 装上全部的表，`create_all` 才建得出外键。
from app.ai_api import models as _ai_client_key_models  # noqa: F401
from app.ai_api import outbox as _ai_outbox_models  # noqa: F401
from app.models import auth as _auth_models  # noqa: F401
from app.models import tax_policy as _tax_policy_models  # noqa: F401
from app.models.base import Base
from app.models.usage import UsageEvent

TEST_DATABASE_URL = os.environ.get("BILLING_TEST_DATABASE_URL", "")

needs_mysql = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="BILLING_TEST_DATABASE_URL is not set; migration tests need a real MySQL",
)

_REVISION = "0026_usage_customer_indexes"
_DOWN_REVISION = "0025_customer_portal_accounts"
_MIGRATION = pathlib.Path("alembic/versions/20261023_0026_usage_customer_indexes.py")
_TABLE = "usage_events"

# Design §2 "数据库", in the order the migration builds them.
_TENANT_INDEX = "ix_usage_events_tenant_occurred"
_PROJECT_INDEX = "ix_usage_events_project_occurred"
_DESIGN_INDEXES = {
    _TENANT_INDEX: ["tenant_id", "occurred_at", "public_id"],
    _PROJECT_INDEX: ["project_id", "occurred_at", "public_id"],
}

_ALEMBIC_VERSION = text("SELECT version_num FROM alembic_version")
_SHOW_INDEX = text(f"SHOW INDEX FROM {_TABLE}")
_TRIGGERS_QUERY = text(
    "SELECT TRIGGER_NAME, EVENT_OBJECT_TABLE, ACTION_TIMING, EVENT_MANIPULATION"
    " FROM information_schema.TRIGGERS WHERE TRIGGER_SCHEMA = DATABASE()"
)
_CHECKS_QUERY = text(
    "SELECT CONSTRAINT_NAME FROM information_schema.CHECK_CONSTRAINTS"
    " WHERE CONSTRAINT_SCHEMA = DATABASE()"
)

# Design §2 "查询" reduced to `usage_events` (EXPLAIN on near-empty joined tables is not stable):
# only a date range, and a date range with a project.
_EXPLAIN_BY_DATE = text(
    "EXPLAIN SELECT public_id FROM usage_events"
    " WHERE tenant_id = :tenant_id AND status = 'PROCESSED'"
    " AND occurred_at >= :from_utc AND occurred_at < :to_utc"
    " ORDER BY occurred_at DESC, public_id DESC LIMIT 21"
)
_EXPLAIN_BY_PROJECT = text(
    "EXPLAIN SELECT public_id FROM usage_events"
    " WHERE tenant_id = :tenant_id AND project_id = :project_id AND status = 'PROCESSED'"
    " AND occurred_at >= :from_utc AND occurred_at < :to_utc"
    " ORDER BY occurred_at DESC, public_id DESC LIMIT 21"
)
_EXPLAIN_PARAMS = {
    "tenant_id": 1,
    "project_id": 1,
    "from_utc": dt.datetime(2026, 9, 30, 16, 0, 0),
    "to_utc": dt.datetime(2026, 10, 31, 16, 0, 0),
}


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("migration_0026", _MIGRATION)
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


def _upper(names: list[str]) -> list[str]:
    return [name.upper() for name in names]


# ---------------------------------------------------------------------------
# No database
# ---------------------------------------------------------------------------


def test_0026_follows_0025() -> None:
    migration = _load()

    assert migration.revision == _REVISION
    assert migration.down_revision == _DOWN_REVISION
    # `alembic_version.version_num` is VARCHAR(32).
    assert len(migration.revision) <= 32


def test_0026_indexes_are_the_ones_the_model_declares() -> None:
    """The migration's frozen literal, the model's `__table_args__` and design §2 agree."""
    migration = _load()
    model = {
        str(index.name): ([column.name for column in index.columns], bool(index.unique))
        for index in UsageEvent.__table__.indexes
    }

    assert migration._TABLE == _TABLE
    assert list(migration._INDEXES.items()) == list(_DESIGN_INDEXES.items())
    for name, columns in _DESIGN_INDEXES.items():
        assert model[name] == (columns, False), name


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


def _design(name: str) -> dict:
    return _reflected(name, list(_DESIGN_INDEXES[name]))


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


@pytest.mark.parametrize(
    ("present", "expected"),
    [
        ([], [_TENANT_INDEX, _PROJECT_INDEX]),
        ([_TENANT_INDEX], [_PROJECT_INDEX]),
        ([_PROJECT_INDEX], [_TENANT_INDEX]),
    ],
)
def test_0026_upgrade_adds_only_the_missing_ones_in_one_alter_table(
    monkeypatch, present: list[str], expected: list[str]
) -> None:
    fake = _FakeOp()
    # An unrelated index with the same columns is not ours to judge.
    unrelated = _reflected("ix_usage_events_tenant_id", ["tenant_id"])
    migration = _with(monkeypatch, fake, [unrelated, *(_design(name) for name in present)])
    migration.upgrade()

    assert migration.asked == [_TABLE]
    [(kind, (statement,))] = fake.calls
    assert kind == "execute"
    assert _normalised(statement).startswith(f"ALTER TABLE {_TABLE.upper()} ")
    assert _added(statement) == _upper(expected)
    for name in expected:
        columns = ", ".join(_DESIGN_INDEXES[name]).upper()
        assert f"ADD INDEX {name.upper()} ({columns})" in _normalised(statement)


def test_0026_upgrade_with_both_in_the_design_shape_issues_no_ddl(monkeypatch) -> None:
    fake = _FakeOp()
    migration = _with(monkeypatch, fake, [_design(_TENANT_INDEX), _design(_PROJECT_INDEX)])
    migration.upgrade()

    assert fake.calls == []


_WRONG_SHAPES = {
    "other columns": _reflected(_PROJECT_INDEX, ["project_id", "occurred_at"]),
    "other order": _reflected(_PROJECT_INDEX, ["occurred_at", "project_id", "public_id"]),
    "unique": _reflected(_PROJECT_INDEX, list(_DESIGN_INDEXES[_PROJECT_INDEX]), unique=True),
    # `public_id(8)`: the same column names, the prefix length only in the dialect options.
    "prefix length": _reflected(
        _PROJECT_INDEX, list(_DESIGN_INDEXES[_PROJECT_INDEX]), mysql_length={"public_id": 8}
    ),
}


@pytest.mark.parametrize("shape", list(_WRONG_SHAPES))
@pytest.mark.parametrize("dialect", ["mysql", "sqlite"])
def test_0026_upgrade_refuses_a_wrong_shape_before_any_ddl(
    monkeypatch, shape: str, dialect: str
) -> None:
    """Design §2 "数据库" 1: the first index is missing, the second is wrong — nothing is built."""
    fake = _FakeOp(dialect=dialect)
    migration = _with(monkeypatch, fake, [_WRONG_SHAPES[shape]])

    with pytest.raises(RuntimeError) as raised:
        migration.upgrade()

    assert fake.calls == []
    message = str(raised.value)
    assert _PROJECT_INDEX in message
    assert _TENANT_INDEX not in message
    assert f"DROP INDEX {_PROJECT_INDEX} ON {_TABLE}" in message


def test_0026_upgrade_on_sqlite_creates_the_missing_ones(monkeypatch) -> None:
    fake = _FakeOp(dialect="sqlite")
    migration = _with(monkeypatch, fake, [_design(_TENANT_INDEX)])
    migration.upgrade()

    assert fake.calls == [
        ("create_index", (_PROJECT_INDEX, _TABLE, _DESIGN_INDEXES[_PROJECT_INDEX], False))
    ]


def test_0026_offline_upgrade_and_downgrade_emit_both_without_reading(monkeypatch) -> None:
    fake = _FakeOp(as_sql=True)
    migration = _with(monkeypatch, fake, [])
    migration.upgrade()
    migration.downgrade()

    assert migration.asked == []
    [(_, (added,)), (_, (dropped,))] = fake.calls
    assert _added(added) == _upper([_TENANT_INDEX, _PROJECT_INDEX])
    assert _dropped(dropped) == _upper([_PROJECT_INDEX, _TENANT_INDEX])


@pytest.mark.parametrize(
    ("present", "expected"),
    [
        ([_TENANT_INDEX, _PROJECT_INDEX], [_PROJECT_INDEX, _TENANT_INDEX]),
        ([_TENANT_INDEX], [_TENANT_INDEX]),
        ([_PROJECT_INDEX], [_PROJECT_INDEX]),
        ([], []),
    ],
)
def test_0026_downgrade_builds_one_drop_for_what_exists(
    monkeypatch, present: list[str], expected: list[str]
) -> None:
    fake = _FakeOp()
    migration = _with(monkeypatch, fake, [_design(name) for name in present])
    migration.downgrade()

    if not expected:
        assert fake.calls == []
        return
    [(kind, (statement,))] = fake.calls
    assert kind == "execute"
    assert _normalised(statement).startswith(f"ALTER TABLE {_TABLE.upper()} ")
    assert _dropped(statement) == _upper(expected)


def test_0026_sqlite_tables_get_both_indexes_in_order_and_not_unique() -> None:
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

    for name, columns in _DESIGN_INDEXES.items():
        assert indexes[name] == (columns, False), name


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
    """Name → (columns, unique, prefix lengths) of every index on `usage_events`."""
    return {
        str(index["name"]): (
            list(index["column_names"]),
            bool(index["unique"]),
            (index.get("dialect_options") or {}).get("mysql_length"),
        )
        for index in inspect(engine).get_indexes(_TABLE)
    }


def _assert_design_shapes(engine: Engine) -> None:
    shapes = _index_shapes(engine)
    for name, columns in _DESIGN_INDEXES.items():
        assert shapes.get(name) == (columns, False, None), name


def _execute(engine: Engine, statement: str) -> None:
    with engine.begin() as connection:
        connection.execute(text(statement))


def _add_in_design_shape(engine: Engine, names: list[str]) -> None:
    clauses = [f"ADD INDEX {name} ({', '.join(_DESIGN_INDEXES[name])})" for name in names]
    _execute(engine, f"ALTER TABLE {_TABLE} " + ", ".join(clauses))


def _drop_if_exists(engine: Engine, names: list[str]) -> None:
    for name in names:
        if name in _index_shapes(engine):
            _execute(engine, f"DROP INDEX {name} ON {_TABLE}")


def _at_0025(alembic_config: Config, engine: Engine) -> None:
    command.upgrade(alembic_config, "head")
    command.downgrade(alembic_config, _DOWN_REVISION)
    assert _version(engine) == _DOWN_REVISION
    assert not set(_DESIGN_INDEXES) & set(_index_shapes(engine))


def _back_to_head(alembic_config: Config, engine: Engine) -> None:
    # A wrong-shaped leftover would make the upgrade refuse; the right-shaped ones are kept.
    shapes = _index_shapes(engine)
    for name, columns in _DESIGN_INDEXES.items():
        if name in shapes and shapes[name] != (columns, False, None):
            _drop_if_exists(engine, [name])
    command.upgrade(alembic_config, "head")


@needs_mysql
def test_0026_upgrade_sends_one_alter_table_for_both(
    alembic_config: Config, engine: Engine
) -> None:
    """Design §7 "迁移可重跑", normal path: one `ALTER TABLE usage_events` with both indexes."""
    _at_0025(alembic_config, engine)
    try:
        with _ddl_statements() as statements:
            command.upgrade(alembic_config, _REVISION)

        assert len(statements) == 1, statements
        assert _normalised(statements[0]).startswith(f"ALTER TABLE {_TABLE.upper()} ")
        assert _added(statements[0]) == _upper(list(_DESIGN_INDEXES))
        assert _version(engine) == _REVISION
        _assert_design_shapes(engine)
    finally:
        _back_to_head(alembic_config, engine)


@needs_mysql
def test_0026_rerun_after_ddl_succeeded_only_moves_the_version(
    alembic_config: Config, engine: Engine
) -> None:
    """Design §7 "迁移可重跑" ①: both built in the design shape and the version still 0025 (the
    process died between `ALTER TABLE` and `alembic_version`) — no DDL, the version moves on."""
    _at_0025(alembic_config, engine)
    try:
        _add_in_design_shape(engine, list(_DESIGN_INDEXES))
        before = _index_shapes(engine)

        with _ddl_statements() as statements:
            command.upgrade(alembic_config, _REVISION)

        assert statements == []
        assert _version(engine) == _REVISION
        assert _index_shapes(engine) == before
        _assert_design_shapes(engine)
    finally:
        _back_to_head(alembic_config, engine)


@needs_mysql
def test_0026_rerun_with_the_first_built_adds_only_the_second(
    alembic_config: Config, engine: Engine
) -> None:
    """Design §7 "迁移可重跑" ②: only the first exists — one `ALTER TABLE` with only the second."""
    _at_0025(alembic_config, engine)
    try:
        _add_in_design_shape(engine, [_TENANT_INDEX])

        with _ddl_statements() as statements:
            command.upgrade(alembic_config, _REVISION)

        assert len(statements) == 1, statements
        assert _normalised(statements[0]).startswith(f"ALTER TABLE {_TABLE.upper()} ")
        assert _added(statements[0]) == _upper([_PROJECT_INDEX])
        assert _version(engine) == _REVISION
        _assert_design_shapes(engine)
    finally:
        _back_to_head(alembic_config, engine)


_WRONG_SECOND = {
    "other columns": f"CREATE INDEX {_PROJECT_INDEX} ON {_TABLE} (project_id, occurred_at)",
    "prefix length": (
        f"CREATE INDEX {_PROJECT_INDEX} ON {_TABLE} (project_id, occurred_at, public_id(8))"
    ),
    "unique": (
        f"CREATE UNIQUE INDEX {_PROJECT_INDEX} ON {_TABLE} (project_id, occurred_at, public_id)"
    ),
}


@needs_mysql
@pytest.mark.parametrize("shape", list(_WRONG_SECOND))
def test_0026_rerun_with_a_wrong_second_fails_before_any_ddl(
    alembic_config: Config, engine: Engine, shape: str
) -> None:
    """Design §7 "迁移可重跑" ③: a hand-made index named like the second but shaped otherwise —
    the upgrade fails naming it, the first is **not** built, the version stays 0025; after
    dropping the hand-made one the upgrade succeeds."""
    _at_0025(alembic_config, engine)
    try:
        _execute(engine, _WRONG_SECOND[shape])
        hand_made = _index_shapes(engine)[_PROJECT_INDEX]

        with _ddl_statements() as statements:
            with pytest.raises(RuntimeError, match=_PROJECT_INDEX):
                command.upgrade(alembic_config, _REVISION)

        assert statements == []
        assert _version(engine) == _DOWN_REVISION
        shapes = _index_shapes(engine)
        assert _TENANT_INDEX not in shapes
        assert shapes[_PROJECT_INDEX] == hand_made

        _drop_if_exists(engine, [_PROJECT_INDEX])
        command.upgrade(alembic_config, _REVISION)
        assert _version(engine) == _REVISION
        _assert_design_shapes(engine)
    finally:
        _back_to_head(alembic_config, engine)


@needs_mysql
@pytest.mark.parametrize(
    ("dropped_by_hand", "expected"),
    [
        # ④a: one of them dropped by hand.
        ([_TENANT_INDEX], [_PROJECT_INDEX]),
        ([_PROJECT_INDEX], [_TENANT_INDEX]),
        # ④b: both dropped by hand.
        ([_TENANT_INDEX, _PROJECT_INDEX], []),
    ],
)
def test_0026_downgrade_drops_only_what_exists(
    alembic_config: Config, engine: Engine, dropped_by_hand: list[str], expected: list[str]
) -> None:
    """Contract's ④a / ④b (in place of design §7 ④): from 0026, with one or both indexes dropped
    by hand, the downgrade to 0025 drops only the rest in one `ALTER TABLE` (none when nothing is
    left), does not fail, and the version becomes 0025."""
    command.upgrade(alembic_config, "head")
    try:
        # Later revisions are not this downgrade's: start at 0026.
        command.downgrade(alembic_config, _REVISION)
        assert _version(engine) == _REVISION
        _drop_if_exists(engine, dropped_by_hand)

        with _ddl_statements() as statements:
            command.downgrade(alembic_config, _DOWN_REVISION)

        if expected:
            assert len(statements) == 1, statements
            assert _normalised(statements[0]).startswith(f"ALTER TABLE {_TABLE.upper()} ")
            assert _dropped(statements[0]) == _upper(expected)
        else:
            assert statements == []
        assert _version(engine) == _DOWN_REVISION
        assert not set(_DESIGN_INDEXES) & set(_index_shapes(engine))
    finally:
        _back_to_head(alembic_config, engine)


@needs_mysql
def test_0026_indexes_exist_at_head_in_order_and_are_possible_keys(
    alembic_config: Config, engine: Engine
) -> None:
    """Design §7 "索引存在且可用": `SHOW INDEX` lists both in the design's column order, not
    unique, without prefix lengths; each is a possible key for its page. Which key the optimizer
    finally picks is not asserted: on a near-empty table that is not stable (design §10)."""
    command.upgrade(alembic_config, "head")
    with engine.connect() as connection:
        rows = connection.execute(_SHOW_INDEX).mappings().all()
        plans = {
            _TENANT_INDEX: connection.execute(_EXPLAIN_BY_DATE, _EXPLAIN_PARAMS).mappings().all(),
            _PROJECT_INDEX: connection.execute(_EXPLAIN_BY_PROJECT, _EXPLAIN_PARAMS)
            .mappings()
            .all(),
        }

    for name, columns in _DESIGN_INDEXES.items():
        ours = sorted(
            (row for row in rows if row["Key_name"] == name), key=lambda row: row["Seq_in_index"]
        )
        assert [row["Column_name"] for row in ours] == columns, name
        assert [int(row["Non_unique"]) for row in ours] == [1, 1, 1], name
        assert [row["Sub_part"] for row in ours] == [None, None, None], name

    for name, plan in plans.items():
        [row] = [row for row in plan if row["table"] == _TABLE]
        possible_keys = str(row["possible_keys"] or "").split(",")
        assert name in possible_keys, (name, possible_keys)


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


@needs_mysql
def test_0026_round_trip_drops_and_recreates_only_the_two_indexes(
    alembic_config: Config, engine: Engine
) -> None:
    """Design §7 "迁移往返": upgrade → downgrade → upgrade builds, drops and rebuilds the two;
    every other table, column, index, key, check and trigger is the same throughout; at 0026 the
    reflected `usage_events` indexes are the ones the model declares."""
    command.upgrade(alembic_config, "head")
    try:
        # Later revisions are not this round trip's: compare at 0026.
        command.downgrade(alembic_config, _REVISION)
        at_0026 = _schema_snapshot(engine)
        others = _triggers_and_checks(engine)
        _assert_design_shapes(engine)
        shapes = _index_shapes(engine)
        for index in UsageEvent.__table__.indexes:
            declared = ([column.name for column in index.columns], bool(index.unique), None)
            assert shapes.get(str(index.name)) == declared, index.name

        command.downgrade(alembic_config, _DOWN_REVISION)
        without = _schema_snapshot(engine)
        expected = {table: dict(shape) for table, shape in at_0026.items()}
        expected[_TABLE]["indexes"] = [
            index for index in at_0026[_TABLE]["indexes"] if index[0] not in _DESIGN_INDEXES
        ]
        assert without == expected
        assert _triggers_and_checks(engine) == others

        command.upgrade(alembic_config, _REVISION)
        assert _schema_snapshot(engine) == at_0026
        assert _triggers_and_checks(engine) == others
        _assert_design_shapes(engine)
    finally:
        _back_to_head(alembic_config, engine)

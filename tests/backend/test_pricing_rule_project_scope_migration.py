"""0030_pricing_rule_project_scope（AIH-TASK-136，设计闸门 #507 v4）.

Design §2 "数据库", §3 INV-6 / INV-8, §4 "迁移与在线计费的并发", §5 (the migration rows), §7 (the
migration and CHECK rows), §8.

The first cases need no database: the revision chain; the migration's literals equal the models'
(the two scope checks, `scope_key`) and 0014's (what the downgrade restores); the rebuilt trigger
differs from 0014's in exactly the two places of design §2; the guard triggers only SIGNAL; the
`--sql` upgrade emits the design order without reading, the `--sql` downgrade refuses first; the
precheck refuses only binlog without the switch; `create_all` on SQLite (what the unit tests run)
lets in exactly the eight combinations and gives two projects two timelines.

On a real MySQL (`BILLING_TEST_DATABASE_URL`; skipped when unset, never skipped in CI): every
migration row of design §7 — the offline upgrade, the structure, existing rules untouched, the
interrupted upgrade (three ways) and downgrade (two ways), one guard trigger left (two places each
way), the guards' order, the wrong shape, writes refused while upgrading (three places) and
downgrading (two places), the precheck, the refused downgrade (online and offline), the restoring
downgrade — and the CHECK, foreign key, timeline and immutability rows. The Alembic chain does not
run on SQLite (0006 onwards ask for `@@log_bin`), so every migration scenario is MySQL's.

Project-level rows are written and changed only with SQL text: `PricingScope` does not have the
three values yet, so the ORM cannot read those rows. Interruptions are made by raising from a
`before_cursor_execute` listener just before the statement the step would send next. Cleaning
follows design §7 "MySQL 用例的清理": every case starts and ends with `TRUNCATE` of the two rule
tables (foreign key checks off; no trigger is dropped or rebuilt), and its `finally` brings the
database back to head (0030 runs again, so a guard trigger left behind is removed), then checks
there is no project-level row and no guard trigger.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import io
import os
import pathlib
import re
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest
from alembic.config import Config
from sqlalchemy import Connection, create_engine, delete, event, insert, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError

from alembic import command
from app.core.database import create_session_factory
from app.models import auth as _auth_models  # noqa: F401 - 让 Base.metadata 装上这些表
from app.models.ai_catalog import AiModel, AiProvider
from app.models.auth import User, UserRole, UserStatus
from app.models.base import Base
from app.models.integration import IntegrationCredential
from app.models.pricing_rules import (
    PRIORITY_SCOPE_CHECK,
    SCOPE_COLUMNS_CHECK,
    SCOPE_KEY_EXPRESSION,
    SQLITE_SCOPE_KEY_EXPRESSION,
    PricingRule,
)
from app.models.tenancy import Project, Tenant
from app.models.usage import UsageEvent
from app.repositories.pricing_rules import resolve_pricing_rule

TEST_DATABASE_URL = os.environ.get("BILLING_TEST_DATABASE_URL", "")

needs_mysql = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="BILLING_TEST_DATABASE_URL is not set; migration tests need a real MySQL",
)

_REVISION = "0030_pricing_rule_project_scope"
_DOWN_REVISION = "0029_wallet_reconciliation"
_MIGRATION = pathlib.Path("alembic/versions/20261030_0030_pricing_rule_project_scope.py")
_MIGRATION_0014 = pathlib.Path("alembic/versions/20260929_0014_pricing_rules.py")
_BEFORE_0014 = "0013_provider_prices"

_RULES = "pricing_rules"
_GUARD_INSERT = "trg_pricing_rules_migration_guard_insert"
_GUARD_UPDATE = "trg_pricing_rules_migration_guard_update"
_GUARDS = (_GUARD_INSERT, _GUARD_UPDATE)
_MAIN_INSERT = "trg_pricing_rules_before_insert"
_MAIN_UPDATE = "trg_pricing_rules_before_update"
_MAIN_DELETE = "trg_pricing_rules_before_delete"
_GUARD_MESSAGE = "pricing_rules is being migrated"
_IMMUTABLE_MESSAGE = "a published pricing rule is immutable"
_OVERLAP_MESSAGE = "pricing rule periods overlap"

# MySQL error numbers: SIGNAL 1644, CHECK 3819, foreign key 1452, unknown column 1054.
_ER_SIGNAL_EXCEPTION = 1644
_ER_CHECK_CONSTRAINT_VIOLATED = 3819
_ER_NO_REFERENCED_ROW = 1452
_ER_BAD_FIELD = 1054

_SCOPE_COLUMNS = ("tenant_id", "project_id", "provider_id", "model_id")
# Design §2 "数据库": level → which of the four columns are set, in the order above.
_LEVELS: dict[str, tuple[bool, bool, bool, bool]] = {
    "PROJECT_PROVIDER_MODEL": (True, True, True, True),
    "PROJECT_PROVIDER": (True, True, True, False),
    "PROJECT": (True, True, False, False),
    "CUSTOMER_PROVIDER_MODEL": (True, False, True, True),
    "CUSTOMER_PROVIDER": (True, False, True, False),
    "CUSTOMER": (True, False, False, False),
    "GLOBAL_PROVIDER_MODEL": (False, False, True, True),
    "GLOBAL": (False, False, False, False),
}
_PROJECT_LEVELS = ("PROJECT_PROVIDER_MODEL", "PROJECT_PROVIDER", "PROJECT")
# Each level with one of its four columns flipped (set ↔ NULL): never legal.
_FLIPPED = [(level, column) for level in _LEVELS for column in _SCOPE_COLUMNS]

# Literals, as in test_wallet_reconciliation_migration.py: no driver adapter involved.
_NOW = "2026-10-30 08:00:00"
_T0 = "2026-10-31 00:00:00"
_T1 = "2026-11-02 00:00:00"
_LATER = "2026-11-05 00:00:00"
_OCCURRED_AT = dt.datetime(2026, 11, 1, 12, 0, 0)
_TEST_EMAIL_DOMAIN = "@pricing-project-scope-migration-test.example.com"

_ALEMBIC_VERSION = text("SELECT version_num FROM alembic_version")
_SET_VERSION = text("UPDATE alembic_version SET version_num = :version")
_PRECHECK = "SELECT @@log_bin, @@log_bin_trust_function_creators"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("migration_0030", _MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_0014() -> ModuleType:
    spec = importlib.util.spec_from_file_location("migration_0014", _MIGRATION_0014)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _spaces(statement: str) -> str:
    return " ".join(statement.split())


def _normalised(statement: str) -> str:
    return re.sub(r"\s+", " ", statement.replace("`", "")).strip().upper()


def _head(statement: str) -> str:
    """The first three words: `CREATE TRIGGER <name>`, `ALTER TABLE PRICING_RULES`, …"""
    return " ".join(_normalised(statement).split(" ")[:3])


def _body(statement: str) -> str:
    """What MySQL keeps as ACTION_STATEMENT: the body after `FOR EACH ROW`."""
    return _spaces(statement.split("FOR EACH ROW", 1)[1])


# ---------------------------------------------------------------------------
# No database
# ---------------------------------------------------------------------------


def test_0030_follows_0029() -> None:
    migration = _load()

    assert migration.revision == _REVISION
    assert migration.down_revision == _DOWN_REVISION
    # `alembic_version.version_num` is VARCHAR(32).
    assert len(migration.revision) <= 32


def test_0030_literals_are_the_models_and_what_the_downgrade_restores_is_0014s() -> None:
    migration = _load()
    earlier = _load_0014()

    assert {name: _spaces(condition) for name, condition in migration._CHECKS.items()} == {
        "ck_pricing_rules_priority_scope": _spaces(PRIORITY_SCOPE_CHECK),
        "ck_pricing_rules_scope_columns": _spaces(SCOPE_COLUMNS_CHECK),
    }
    assert _spaces(migration._SCOPE_KEY) == _spaces(SCOPE_KEY_EXPRESSION)
    # The SQLite form is the same key, part by part.
    sqlite_parts = [part.strip() for part in SQLITE_SCOPE_KEY_EXPRESSION.split("||")]
    assert sqlite_parts == [
        "priority_scope",
        "':'",
        "COALESCE(tenant_id, 0)",
        "':'",
        "COALESCE(project_id, 0)",
        "':'",
        "COALESCE(provider_id, 0)",
        "':'",
        "COALESCE(model_id, 0)",
    ]
    for name, condition in migration._CHECKS_0014.items():
        assert _spaces(condition) == _spaces(earlier._CHECKS[name][1]), name
    assert _spaces(migration._SCOPE_KEY_0014) == _spaces(earlier._SCOPE_KEY)
    assert set(migration._TRIGGERS) == set(migration._TRIGGERS_0014) == {_MAIN_UPDATE}
    assert _spaces(migration._TRIGGERS_0014[_MAIN_UPDATE]) == _spaces(
        earlier._TRIGGERS[_MAIN_UPDATE]
    )
    # Design §2: eight levels, the three project levels first, each with one combination.
    assert list(_LEVELS) == [
        *_PROJECT_LEVELS,
        "CUSTOMER_PROVIDER_MODEL",
        "CUSTOMER_PROVIDER",
        "CUSTOMER",
        "GLOBAL_PROVIDER_MODEL",
        "GLOBAL",
    ]
    for level, columns in _LEVELS.items():
        parts = " AND ".join(
            f"{column} IS NOT NULL" if present else f"{column} IS NULL"
            for column, present in zip(_SCOPE_COLUMNS, columns, strict=True)
        )
        assert f"(priority_scope = '{level}' AND {parts})" in _spaces(SCOPE_COLUMNS_CHECK), level
    assert _spaces(PRIORITY_SCOPE_CHECK) == "priority_scope IN ({})".format(
        ", ".join(f"'{level}'" for level in _LEVELS)
    )


def test_0030_trigger_differs_from_0014_only_in_the_two_places() -> None:
    """Design §2: ② `project_id` joins the immutable columns, ④ the scope key gets the project part
    (word for word the new `scope_key` with `NEW.`); ①, ①′, ③, ⑤ are 0014's."""
    migration = _load()
    old = _spaces(_load_0014()._TRIGGERS[_MAIN_UPDATE])
    new = _spaces(migration._TRIGGERS[_MAIN_UPDATE])

    expected = old.replace(
        "OR NOT (NEW.tenant_id <=> OLD.tenant_id)",
        "OR NOT (NEW.tenant_id <=> OLD.tenant_id) OR NOT (NEW.project_id <=> OLD.project_id)",
    ).replace(
        "COALESCE(NEW.tenant_id, 0), ':', COALESCE(NEW.provider_id, 0)",
        "COALESCE(NEW.tenant_id, 0), ':', COALESCE(NEW.project_id, 0), ':',"
        " COALESCE(NEW.provider_id, 0)",
    )
    assert expected != old
    assert new == expected
    key = re.search(r"o\.scope_key = (CONCAT\(.*?\)\))", new)
    assert key is not None
    assert key.group(1).replace("NEW.", "") == _spaces(SCOPE_KEY_EXPRESSION)
    # MySQL triggers cannot refer to generated columns through NEW / OLD.
    for column in ("scope_key", "open_slot"):
        assert f"NEW.{column}" not in new
        assert f"OLD.{column}" not in new
    assert migration._UPGRADED_TRIGGER_MARK in new
    assert "project_id" not in _spaces(migration._TRIGGERS_0014[_MAIN_UPDATE])


@pytest.mark.parametrize("name", _GUARDS)
def test_0030_guard_triggers_only_signal(name: str) -> None:
    """Design §2 step 2: one SIGNAL, no column; PRECEDES the main trigger of the same timing when
    it is there, without it otherwise."""
    migration = _load()
    event_name, main = migration._GUARDS[name]

    ordered = _spaces(migration._guard_statement(name, main))
    assert ordered == (
        f"CREATE TRIGGER {name} BEFORE {event_name} ON {_RULES} FOR EACH ROW PRECEDES {main}"
        f" SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = '{_GUARD_MESSAGE}'"
    )
    alone = _spaces(migration._guard_statement(name, None))
    assert alone == ordered.replace(f" PRECEDES {main}", "")
    assert main == {"INSERT": _MAIN_INSERT, "UPDATE": _MAIN_UPDATE}[event_name]
    assert list(migration._GUARDS) == list(_GUARDS)


def test_0030_each_alter_is_one_statement_in_the_design_order() -> None:
    migration = _load()
    upgrade = _spaces(migration._ALTER_UPGRADE)
    downgrade = _spaces(migration._ALTER_DOWNGRADE)

    for statement in (migration._ALTER_UPGRADE, migration._ALTER_DOWNGRADE):
        assert ";" not in statement
        assert _spaces(statement).startswith(f"ALTER TABLE {_RULES} ")
    up_clauses = [
        "ADD COLUMN project_id BIGINT NULL AFTER tenant_id",
        "ADD INDEX ix_pricing_rules_project (project_id, tenant_id)",
        "ADD CONSTRAINT fk_pricing_rules_project FOREIGN KEY (project_id, tenant_id)"
        " REFERENCES projects (id, tenant_id) ON DELETE RESTRICT",
        "DROP CHECK ck_pricing_rules_priority_scope",
        f"ADD CONSTRAINT ck_pricing_rules_priority_scope CHECK ({_spaces(PRIORITY_SCOPE_CHECK)})",
        "DROP CHECK ck_pricing_rules_scope_columns",
        f"ADD CONSTRAINT ck_pricing_rules_scope_columns CHECK ({_spaces(SCOPE_COLUMNS_CHECK)})",
        "DROP INDEX ux_pricing_rules_open_slot",
        "MODIFY COLUMN scope_key VARCHAR(128)"
        f" GENERATED ALWAYS AS ({_spaces(SCOPE_KEY_EXPRESSION)}) STORED",
        "ADD UNIQUE INDEX ux_pricing_rules_open_slot (scope_key, open_slot)",
    ]
    positions = [upgrade.index(clause) for clause in up_clauses]
    assert positions == sorted(positions)
    earlier = _load_0014()
    down_clauses = [
        "DROP FOREIGN KEY fk_pricing_rules_project",
        "DROP INDEX ix_pricing_rules_project",
        "DROP CHECK ck_pricing_rules_priority_scope",
        "ADD CONSTRAINT ck_pricing_rules_priority_scope CHECK"
        f" ({_spaces(earlier._CHECKS['ck_pricing_rules_priority_scope'][1])})",
        "DROP CHECK ck_pricing_rules_scope_columns",
        "ADD CONSTRAINT ck_pricing_rules_scope_columns CHECK"
        f" ({_spaces(earlier._CHECKS['ck_pricing_rules_scope_columns'][1])})",
        "DROP INDEX ux_pricing_rules_open_slot",
        "MODIFY COLUMN scope_key VARCHAR(128)"
        f" GENERATED ALWAYS AS ({_spaces(earlier._SCOPE_KEY)}) STORED",
        "ADD UNIQUE INDEX ux_pricing_rules_open_slot (scope_key, open_slot)",
        "DROP COLUMN project_id",
    ]
    positions = [downgrade.index(clause) for clause in down_clauses]
    assert positions == sorted(positions)


class _OfflineOp:
    """`alembic.op` in `--sql` mode: no bind to read from; every statement is recorded."""

    def __init__(self) -> None:
        self.statements: list[str] = []

    def get_context(self) -> SimpleNamespace:
        return SimpleNamespace(as_sql=True, dialect=SimpleNamespace(name="mysql"))

    def get_bind(self) -> object:
        raise AssertionError("offline mode reads nothing from the database")

    def execute(self, statement: object) -> None:
        self.statements.append(str(statement))


def test_0030_offline_upgrade_emits_the_design_order_without_reading(monkeypatch) -> None:
    migration = _load()
    fake = _OfflineOp()
    monkeypatch.setattr(migration, "op", fake)
    migration.upgrade()

    assert [_spaces(s) for s in fake.statements] == [
        _spaces(migration._guard_statement(_GUARD_INSERT, _MAIN_INSERT)),
        _spaces(migration._guard_statement(_GUARD_UPDATE, _MAIN_UPDATE)),
        _spaces(migration._ALTER_UPGRADE),
        f"DROP TRIGGER IF EXISTS {_MAIN_UPDATE}",
        _spaces(migration._TRIGGERS[_MAIN_UPDATE]),
        f"DROP TRIGGER {_GUARD_INSERT}",
        f"DROP TRIGGER {_GUARD_UPDATE}",
    ]


def test_0030_offline_downgrade_refuses_before_anything(monkeypatch) -> None:
    migration = _load()
    fake = _OfflineOp()
    monkeypatch.setattr(migration, "op", fake)

    with pytest.raises(RuntimeError) as raised:
        migration.downgrade()

    assert fake.statements == []
    assert "online" in str(raised.value)
    assert _RULES in str(raised.value)


class _PrecheckBind:
    def __init__(self, log_bin: int, trusted: int) -> None:
        self.row = (log_bin, trusted)

    def execute(self, _statement: object) -> SimpleNamespace:
        return SimpleNamespace(one=lambda: self.row)


@pytest.mark.parametrize(
    ("log_bin", "trusted", "refused"),
    [(1, 0, True), (1, 1, False), (0, 0, False), (0, 1, False)],
)
def test_0030_precheck_refuses_only_binlog_without_the_switch(
    log_bin: int, trusted: int, refused: bool
) -> None:
    """Query, decision and message as 0014's."""
    migration = _load()
    earlier = _load_0014()

    assert migration._PREREQUISITE_QUERY == earlier._PREREQUISITE_QUERY == _PRECHECK
    assert migration._PREREQUISITE_ERROR == earlier._PREREQUISITE_ERROR
    if refused:
        with pytest.raises(RuntimeError, match="log_bin_trust_function_creators"):
            migration._require_trigger_privilege(_PrecheckBind(log_bin, trusted))
    else:
        migration._require_trigger_privilege(_PrecheckBind(log_bin, trusted))


def test_0030_model_declares_the_column_key_and_index() -> None:
    table = PricingRule.__table__
    names = [column.name for column in table.columns]

    assert names[names.index("tenant_id") + 1] == "project_id"
    assert table.c.project_id.nullable
    assert type(table.c.project_id.type) is type(table.c.tenant_id.type)
    keys = {str(key.name): key for key in table.foreign_key_constraints}
    project = keys["fk_pricing_rules_project"]
    assert list(project.column_keys) == ["project_id", "tenant_id"]
    assert [element.target_fullname for element in project.elements] == [
        "projects.id",
        "projects.tenant_id",
    ]
    assert project.ondelete == "RESTRICT"
    indexes = {str(index.name): index for index in table.indexes}
    assert [c.name for c in indexes["ix_pricing_rules_project"].columns] == [
        "project_id",
        "tenant_id",
    ]
    assert not indexes["ix_pricing_rules_project"].unique
    # Not changed by this task.
    assert [c.name for c in indexes["ix_pricing_rules_resolve"].columns] == [
        "priority_scope",
        "tenant_id",
        "provider_id",
        "model_id",
        "effective_from",
    ]


# ---------------------------------------------------------------------------
# SQLite (models' metadata)
# ---------------------------------------------------------------------------


@contextmanager
def _sqlite() -> Iterator[Engine]:
    """The unit tests build SQLite with `create_all` from the models, not with the migrations.
    SQLite does not enforce foreign keys here (as in the unit tests): the ids need no parents."""
    engine = create_engine("sqlite+pysqlite:///:memory:")
    try:
        Base.metadata.create_all(engine)
        yield engine
    finally:
        engine.dispose()


@dataclass(frozen=True)
class _Ids:
    user_id: int
    tenant_id: int
    other_tenant_id: int
    project_id: int
    second_project_id: int
    other_project_id: int
    provider_id: int
    model_id: int


_SQLITE_IDS = _Ids(1, 1, 2, 1, 2, 3, 1, 1)


def _scope_values(ids: _Ids, level: str, flipped: str | None = None) -> dict[str, int | None]:
    """The four scope columns of `level`, with `flipped` (if any) set ↔ NULL."""
    present = dict(zip(_SCOPE_COLUMNS, _LEVELS[level], strict=True))
    if flipped is not None:
        present[flipped] = not present[flipped]
    values = {
        "tenant_id": ids.tenant_id,
        "project_id": ids.project_id,
        "provider_id": ids.provider_id,
        "model_id": ids.model_id,
    }
    return {column: values[column] if present[column] else None for column in _SCOPE_COLUMNS}


def _rule_values(ids: _Ids, level: str, **overrides: object) -> dict[str, object]:
    """A MARKUP draft of `level` (the scope columns from `_scope_values`), then `overrides`."""
    values: dict[str, object] = {
        "public_id": str(uuid.uuid4()),
        "priority_scope": level,
        **_scope_values(ids, level),
        "strategy": "MARKUP",
        "markup_multiplier": "1.50000000",
        "status": "DRAFT",
        "created_by": ids.user_id,
        "created_at": _NOW,
        "updated_at": _NOW,
    }
    values.update(overrides)
    return values


def _insert_rule(connection: Connection, values: dict[str, object]) -> int:
    """SQL text, not the ORM: the column list is whatever `values` has (no `project_id` at 0029)."""
    columns = ", ".join(values)
    placeholders = ", ".join(f":{name}" for name in values)
    connection.execute(text(f"INSERT INTO {_RULES} ({columns}) VALUES ({placeholders})"), values)
    found = text(f"SELECT id FROM {_RULES} WHERE public_id = :public_id")
    return int(connection.execute(found, {"public_id": values["public_id"]}).scalar_one())


def _without_project(values: dict[str, object]) -> dict[str, object]:
    assert values.get("project_id") is None
    return {name: value for name, value in values.items() if name != "project_id"}


_PUBLISH = text(
    f"UPDATE {_RULES} SET status = 'PUBLISHED', effective_from = :effective_from,"
    " approved_by = :user_id, approved_at = :now, updated_at = :now WHERE id = :id"
)
_TRUNCATE_AT = text(
    f"UPDATE {_RULES} SET effective_to = :effective_to, updated_at = :now WHERE id = :id"
)


def _publish(connection: Connection, ids: _Ids, rule_id: int, effective_from: str) -> None:
    connection.execute(
        _PUBLISH,
        {"effective_from": effective_from, "user_id": ids.user_id, "now": _NOW, "id": rule_id},
    )


@pytest.mark.parametrize("level", list(_LEVELS))
def test_0030_sqlite_lets_in_each_legal_combination(level: str) -> None:
    """Design §7 "CHECK 只放行八种组合" on SQLite; the key has the project part."""
    with _sqlite() as engine:
        with engine.begin() as connection:
            rule_id = _insert_rule(connection, _rule_values(_SQLITE_IDS, level))
            key = connection.execute(
                text(f"SELECT scope_key FROM {_RULES} WHERE id = :id"), {"id": rule_id}
            ).scalar_one()

    values = _scope_values(_SQLITE_IDS, level)
    assert key == ":".join([level, *(str(values[c] or 0) for c in _SCOPE_COLUMNS)])


@pytest.mark.parametrize(("level", "flipped"), _FLIPPED)
def test_0030_sqlite_refuses_each_flipped_combination(level: str, flipped: str) -> None:
    with _sqlite() as engine:
        with pytest.raises(DBAPIError) as raised:
            with engine.begin() as connection:
                values = _rule_values(_SQLITE_IDS, level)
                values.update(_scope_values(_SQLITE_IDS, level, flipped))
                _insert_rule(connection, values)
        assert "CHECK constraint failed" in str(raised.value)
        with engine.connect() as connection:
            assert connection.execute(text(f"SELECT COUNT(*) FROM {_RULES}")).scalar_one() == 0


def _published_values(ids: _Ids, level: str, **overrides: object) -> dict[str, object]:
    """A PUBLISHED, open-ended row written directly (SQLite has no triggers)."""
    return _rule_values(
        ids,
        level,
        status="PUBLISHED",
        effective_from=_T0,
        approved_by=ids.user_id,
        approved_at=_NOW,
        **overrides,
    )


def test_0030_sqlite_two_projects_have_two_timelines() -> None:
    """Design §7 "两个项目各自一条时间线", written directly to hit `ux_pricing_rules_open_slot`:
    one open rule per project goes in; a second open rule on the same project does not."""
    with _sqlite() as engine:
        with engine.begin() as connection:
            first = _insert_rule(connection, _published_values(_SQLITE_IDS, "PROJECT"))
            second = _insert_rule(
                connection,
                _published_values(_SQLITE_IDS, "PROJECT", project_id=_SQLITE_IDS.second_project_id),
            )
            keys = dict(
                connection.execute(
                    text(f"SELECT id, scope_key FROM {_RULES} WHERE open_slot = 1")
                ).all()
            )
        assert keys == {first: "PROJECT:1:1:0:0", second: "PROJECT:1:2:0:0"}

        with pytest.raises(DBAPIError) as raised:
            with engine.begin() as connection:
                _insert_rule(connection, _published_values(_SQLITE_IDS, "PROJECT"))
        assert "UNIQUE constraint failed" in str(raised.value)


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
def _statements() -> Iterator[list[str]]:
    """Every statement any engine (Alembic's own included) sends while the block runs."""
    statements: list[str] = []

    def record(_conn, _cursor, statement, _params, _context, _many) -> None:
        statements.append(statement)

    event.listen(Engine, "before_cursor_execute", record)
    try:
        yield statements
    finally:
        event.remove(Engine, "before_cursor_execute", record)


def _ddl(statements: list[str]) -> list[str]:
    return [s for s in statements if _normalised(s).split(" ")[0] in {"ALTER", "CREATE", "DROP"}]


class _Interrupted(Exception):
    """Raised in place of the statement a step would send next."""


@contextmanager
def _interrupt_before(head: str) -> Iterator[None]:
    """Stops the migration just before the first statement whose first three words are `head`."""

    def stop(_conn, _cursor, statement, _params, _context, _many) -> None:
        if _head(statement) == head:
            raise _Interrupted(head)

    event.listen(Engine, "before_cursor_execute", stop)
    try:
        yield
    finally:
        event.remove(Engine, "before_cursor_execute", stop)


@contextmanager
def _binlog_without_the_switch() -> Iterator[None]:
    """Design §7 "预检": the precheck query answers binlog on, switch off."""

    def answer(_conn, _cursor, statement, params, _context, _many):
        if _spaces(statement) == _PRECHECK:
            return "SELECT 1, 0", params
        return statement, params

    event.listen(Engine, "before_cursor_execute", answer, retval=True)
    try:
        yield
    finally:
        event.remove(Engine, "before_cursor_execute", answer)


def _version(engine: Engine) -> str:
    with engine.connect() as connection:
        return str(connection.execute(_ALEMBIC_VERSION).scalar_one())


def _set_version(engine: Engine, version: str) -> None:
    with engine.begin() as connection:
        connection.execute(_SET_VERSION, {"version": version})


def _execute(engine: Engine, statement: str, params: dict[str, object] | None = None) -> None:
    with engine.begin() as connection:
        connection.execute(text(statement), params or {})


def _to_head(alembic_config: Config, engine: Engine) -> None:
    """Head, complete: at 0030 the version goes back to 0029 first, so 0030 runs again — no DDL
    when complete, otherwise it finishes the half-done step and removes any guard trigger."""
    if _version(engine) == _REVISION:
        _set_version(engine, _DOWN_REVISION)
    command.upgrade(alembic_config, "head")


def _at_0029(alembic_config: Config, engine: Engine) -> None:
    _to_head(alembic_config, engine)
    command.downgrade(alembic_config, _DOWN_REVISION)
    assert _version(engine) == _DOWN_REVISION


def _clean(engine: Engine) -> None:
    """Design §7 "MySQL 用例的清理", as test_pricing_rules_service.py: TRUNCATE (DDL, no trigger
    fires, none is dropped or rebuilt) with foreign key checks off."""
    with engine.begin() as connection:
        connection.exec_driver_sql("SET FOREIGN_KEY_CHECKS = 0")
        try:
            connection.exec_driver_sql("TRUNCATE TABLE pricing_rule_components")
            connection.exec_driver_sql("TRUNCATE TABLE pricing_rules")
        finally:
            connection.exec_driver_sql("SET FOREIGN_KEY_CHECKS = 1")
    with engine.connect() as connection:
        # No row at all, so no project-level row either.
        assert connection.execute(text(f"SELECT COUNT(*) FROM {_RULES}")).scalar_one() == 0


def _triggers(engine: Engine) -> dict[str, tuple[str, str, int, str]]:
    """Triggers on `pricing_rules`: name → (timing, event, order, body)."""
    query = text(
        "SELECT TRIGGER_NAME, ACTION_TIMING, EVENT_MANIPULATION, ACTION_ORDER, ACTION_STATEMENT"
        f" FROM information_schema.TRIGGERS WHERE TRIGGER_SCHEMA = DATABASE()"
        f" AND EVENT_OBJECT_TABLE = '{_RULES}'"
    )
    with engine.connect() as connection:
        return {
            str(name): (str(timing), str(kind), int(order), _spaces(str(body)))
            for name, timing, kind, order, body in connection.execute(query).all()
        }


def _guards(engine: Engine) -> set[str]:
    return set(_triggers(engine)) & set(_GUARDS)


def _structure(engine: Engine) -> dict[str, Any]:
    """`pricing_rules` from information_schema, word for word: columns in order (type, nullability,
    generation expression), indexes, foreign keys with their rules, check clauses, triggers (timing,
    event, body; the order among same-timing triggers is `test_0030_guards_precede_…`'s)."""
    queries = {
        "columns": (
            "SELECT ORDINAL_POSITION, COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE, COLUMN_DEFAULT,"
            " EXTRA, GENERATION_EXPRESSION FROM information_schema.COLUMNS"
            f" WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = '{_RULES}'"
            " ORDER BY ORDINAL_POSITION"
        ),
        "indexes": (
            "SELECT INDEX_NAME, SEQ_IN_INDEX, COLUMN_NAME, NON_UNIQUE, SUB_PART"
            " FROM information_schema.STATISTICS"
            f" WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = '{_RULES}'"
            " ORDER BY INDEX_NAME, SEQ_IN_INDEX"
        ),
        "foreign_keys": (
            "SELECT k.CONSTRAINT_NAME, k.ORDINAL_POSITION, k.COLUMN_NAME, k.REFERENCED_TABLE_NAME,"
            " k.REFERENCED_COLUMN_NAME, r.UPDATE_RULE, r.DELETE_RULE"
            " FROM information_schema.KEY_COLUMN_USAGE k"
            " JOIN information_schema.REFERENTIAL_CONSTRAINTS r"
            " ON r.CONSTRAINT_SCHEMA = k.CONSTRAINT_SCHEMA"
            " AND r.CONSTRAINT_NAME = k.CONSTRAINT_NAME"
            f" WHERE k.CONSTRAINT_SCHEMA = DATABASE() AND k.TABLE_NAME = '{_RULES}'"
            " ORDER BY k.CONSTRAINT_NAME, k.ORDINAL_POSITION"
        ),
        "checks": (
            "SELECT t.CONSTRAINT_NAME, c.CHECK_CLAUSE FROM information_schema.TABLE_CONSTRAINTS t"
            " JOIN information_schema.CHECK_CONSTRAINTS c"
            " ON c.CONSTRAINT_SCHEMA = t.CONSTRAINT_SCHEMA"
            " AND c.CONSTRAINT_NAME = t.CONSTRAINT_NAME"
            f" WHERE t.TABLE_SCHEMA = DATABASE() AND t.TABLE_NAME = '{_RULES}'"
            " AND t.CONSTRAINT_TYPE = 'CHECK' ORDER BY t.CONSTRAINT_NAME"
        ),
    }
    with engine.connect() as connection:
        found: dict[str, Any] = {
            key: [tuple(row) for row in connection.execute(text(query)).all()]
            for key, query in queries.items()
        }
    found["triggers"] = {
        name: (timing, kind, body) for name, (timing, kind, _, body) in _triggers(engine).items()
    }
    return found


def _rows(engine: Engine) -> list[dict[str, object]]:
    with engine.connect() as connection:
        return [
            dict(row)
            for row in connection.execute(text(f"SELECT * FROM {_RULES} ORDER BY id")).mappings()
        ]


@dataclass(frozen=True)
class _Seed(_Ids):
    credential_id: int


def _seed(connection: Connection) -> _Seed:
    """An admin, tenants A and B, projects P1 and P2 of A and Q1 of B, a provider with a model and
    a credential of P1. Core inserts: none of these tables changes in 0030."""
    marker = uuid.uuid4().hex[:12]
    now = dt.datetime(2026, 10, 30, 8, 0, 0)

    def new_id(statement) -> int:
        return int(connection.execute(statement).inserted_primary_key[0])

    user_id = new_id(
        insert(User).values(
            email=f"{marker}{_TEST_EMAIL_DOMAIN}",
            password_hash="not-a-real-hash",
            role=UserRole.ADMIN,
            status=UserStatus.ACTIVE,
            created_at=now,
            updated_at=now,
        )
    )
    tenants = [
        new_id(
            insert(Tenant).values(
                public_id=str(uuid.uuid4()),
                company_name=f"Fictional Project Scope {name} Sdn Bhd",
                email=f"{marker}-{name}{_TEST_EMAIL_DOMAIN}",
                created_at=now,
                updated_at=now,
            )
        )
        for name in ("a", "b")
    ]

    def project(tenant_id: int, name: str) -> int:
        return new_id(
            insert(Project).values(
                public_id=str(uuid.uuid4()),
                tenant_id=tenant_id,
                name=name,
                created_at=now,
                updated_at=now,
            )
        )

    provider_id = new_id(
        insert(AiProvider).values(
            public_id=str(uuid.uuid4()),
            code=f"p136-{marker}",
            display_name="Fictional provider",
            created_at=now,
            updated_at=now,
        )
    )
    model_id = new_id(
        insert(AiModel).values(
            public_id=str(uuid.uuid4()),
            provider_id=provider_id,
            code=f"m136-{marker}",
            display_name="Fictional model",
            created_at=now,
            updated_at=now,
        )
    )
    p1 = project(tenants[0], "AI Chatbot")
    p2 = project(tenants[0], "AI API")
    q1 = project(tenants[1], "Other customer's project")
    credential_id = new_id(
        insert(IntegrationCredential).values(
            tenant_id=tenants[0],
            project_id=p1,
            public_api_key="ak_" + uuid.uuid4().hex,
            key_version=1,
            encrypted_secret="not-a-real-secret",
            encryption_key_version=1,
            valid_from=now,
            created_at=now,
        )
    )
    return _Seed(
        user_id=user_id,
        tenant_id=tenants[0],
        other_tenant_id=tenants[1],
        project_id=p1,
        second_project_id=p2,
        other_project_id=q1,
        provider_id=provider_id,
        model_id=model_id,
        credential_id=credential_id,
    )


def _committed_seed(engine: Engine) -> _Seed:
    with engine.begin() as connection:
        return _seed(connection)


def _insert_event(engine: Engine, seed: _Seed, rule_id: int) -> int:
    """A received usage event of P1 that refers to `rule_id` (as a priced one would)."""
    with engine.begin() as connection:
        kind_id, shape, unit, quantity_kind = connection.execute(
            text(
                "SELECT id, payload_shape, unit, quantity_kind FROM usage_meter_types"
                " ORDER BY id LIMIT 1"
            )
        ).one()
        tokens = shape == "LLM_TOKEN_FIELDS"
        return int(
            connection.execute(
                insert(UsageEvent).values(
                    public_id=str(uuid.uuid4()),
                    event_id=uuid.uuid4().hex,
                    schema_version="1.0",
                    tenant_id=seed.tenant_id,
                    project_id=seed.project_id,
                    integration_credential_id=seed.credential_id,
                    request_id=f"req-{uuid.uuid4().hex}",
                    provider_code_raw="fictional",
                    model_code_raw="fictional",
                    provider_id=seed.provider_id,
                    model_id=seed.model_id,
                    usage_meter_type_id=kind_id,
                    payload_shape=shape,
                    quantity_kind=quantity_kind,
                    unit=unit,
                    input_tokens=10 if tokens else None,
                    output_tokens=5 if tokens else None,
                    cache_creation_input_tokens=0 if tokens else None,
                    cache_read_input_tokens=0 if tokens else None,
                    quantity=None if tokens else 1,
                    payload_fingerprint="0" * 64,
                    occurred_at=_OCCURRED_AT,
                    received_at=_OCCURRED_AT,
                    created_at=_OCCURRED_AT,
                    pricing_rule_id=rule_id,
                )
            ).inserted_primary_key[0]
        )


def _finish(alembic_config: Config, engine: Engine, seed: _Seed | None) -> None:
    """Design §7 "MySQL 用例的清理": our events, then TRUNCATE, then head again, then the seeded
    parents; the database ends at head with no project-level row and no guard trigger."""
    if seed is not None:
        with engine.begin() as connection:
            connection.execute(delete(UsageEvent).where(UsageEvent.tenant_id == seed.tenant_id))
    _clean(engine)
    _to_head(alembic_config, engine)
    if seed is not None:
        with engine.begin() as connection:
            connection.execute(
                delete(IntegrationCredential).where(IntegrationCredential.id == seed.credential_id)
            )
            tenants = [seed.tenant_id, seed.other_tenant_id]
            connection.execute(delete(Project).where(Project.tenant_id.in_(tenants)))
            connection.execute(delete(Tenant).where(Tenant.id.in_(tenants)))
            connection.execute(delete(AiModel).where(AiModel.id == seed.model_id))
            connection.execute(delete(AiProvider).where(AiProvider.id == seed.provider_id))
            connection.execute(delete(User).where(User.id == seed.user_id))
    assert _version(engine) == _REVISION
    assert _guards(engine) == set()
    with engine.connect() as connection:
        project_rows = connection.execute(
            text(
                f"SELECT COUNT(*) FROM {_RULES} WHERE project_id IS NOT NULL"
                " OR priority_scope IN ('PROJECT_PROVIDER_MODEL', 'PROJECT_PROVIDER', 'PROJECT')"
            )
        ).scalar_one()
    assert project_rows == 0


def _start(alembic_config: Config, engine: Engine) -> None:
    _to_head(alembic_config, engine)
    _clean(engine)


def _refusal(engine: Engine, statement: str, params: dict[str, object]) -> tuple[int, str]:
    """Runs one write in its own transaction; returns the MySQL error number and message."""
    with pytest.raises(DBAPIError) as raised:
        with engine.begin() as connection:
            connection.execute(text(statement), params)
    code, message = raised.value.orig.args[:2]
    return int(code), str(message)


def _five_published(engine: Engine, seed: _Seed) -> dict[str, int]:
    """One published MARKUP rule on each of the five non-project levels (SQL text, no
    `project_id` column needed): level → id."""
    published = {}
    with engine.begin() as connection:
        for level in _LEVELS:
            if level in _PROJECT_LEVELS:
                continue
            rule_id = _insert_rule(connection, _without_project(_rule_values(seed, level)))
            _publish(connection, seed, rule_id, _T0)
            published[level] = rule_id
    return published


def _published_and_draft(engine: Engine, seed: _Seed) -> tuple[int, int]:
    """A GLOBAL rule published from T0 (open) and a GLOBAL draft."""
    with engine.begin() as connection:
        published = _insert_rule(connection, _without_project(_rule_values(seed, "GLOBAL")))
        _publish(connection, seed, published, _T0)
        draft = _insert_rule(connection, _without_project(_rule_values(seed, "GLOBAL")))
    return published, draft


def _writes(seed: _Seed, published: int, draft: int) -> dict[str, tuple[str, dict[str, object]]]:
    """Design §7 "迁移期间的写入隔离": a new rule, a change to a published rule, a period that
    overlaps the published one."""
    values = _without_project(_rule_values(seed, "GLOBAL"))
    return {
        "insert": (
            f"INSERT INTO {_RULES} ({', '.join(values)})"
            f" VALUES ({', '.join(f':{name}' for name in values)})",
            values,
        ),
        "change published": (
            f"UPDATE {_RULES} SET effective_from = :later, updated_at = :now WHERE id = :id",
            {"later": _LATER, "now": _NOW, "id": published},
        ),
        "overlap": (
            str(_PUBLISH),
            {"effective_from": _T1, "user_id": seed.user_id, "now": _NOW, "id": draft},
        ),
    }


def _assert_all_guarded(engine: Engine, writes: dict[str, tuple[str, dict[str, object]]]) -> None:
    before = _rows(engine)
    for name, (statement, params) in writes.items():
        assert _refusal(engine, statement, params) == (_ER_SIGNAL_EXCEPTION, _GUARD_MESSAGE), name
    assert _rows(engine) == before


def _assert_head_structure(engine: Engine) -> None:
    """Design §7 "迁移结构": what 0030 leaves, against the model and the migration's literals."""
    migration = _load()
    structure = _structure(engine)
    columns = {row[1]: row for row in structure["columns"]}
    assert list(columns) == [c.name for c in PricingRule.__table__.columns]
    assert columns["project_id"][2:5] == ("bigint", "YES", None)
    scope_key = str(columns["scope_key"][6])
    positions = [scope_key.index(name) for name in ("tenant_id", "project_id", "provider_id")]
    assert positions == sorted(positions)
    assert "STORED GENERATED" in str(columns["scope_key"][5]).upper()
    # (position, column, referred table, referred column, delete rule).
    keys = [
        (*row[1:5], row[6])
        for row in structure["foreign_keys"]
        if row[0] == "fk_pricing_rules_project"
    ]
    assert keys == [
        (1, "project_id", "projects", "id", "RESTRICT"),
        (2, "tenant_id", "projects", "tenant_id", "RESTRICT"),
    ]
    indexes: dict[str, list[tuple[object, ...]]] = {}
    for name, _, column, non_unique, sub_part in structure["indexes"]:
        indexes.setdefault(str(name), []).append((column, int(non_unique), sub_part))
    assert indexes["ix_pricing_rules_project"] == [("project_id", 1, None), ("tenant_id", 1, None)]
    assert indexes["ux_pricing_rules_open_slot"] == [("scope_key", 0, None), ("open_slot", 0, None)]
    checks = dict(structure["checks"])
    assert "PROJECT_PROVIDER_MODEL" in checks["ck_pricing_rules_priority_scope"]
    assert "project_id" in checks["ck_pricing_rules_scope_columns"]
    triggers = structure["triggers"]
    assert set(triggers) == {_MAIN_INSERT, _MAIN_UPDATE, _MAIN_DELETE}
    assert triggers[_MAIN_UPDATE] == ("BEFORE", "UPDATE", _body(migration._TRIGGERS[_MAIN_UPDATE]))
    # The migration's own shape check passes on what it built.
    with engine.connect() as connection:
        assert migration._shape_problems(connection, migration._columns(connection)) == []


@needs_mysql
def test_0030_offline_upgrade_and_downgrade(alembic_config: Config) -> None:
    """Design §7 "离线升级" and "降级拒绝（离线）": the `--sql` upgrade emits the guards, the
    ALTER, the trigger's DROP IF EXISTS and CREATE, the guards' DROP, in this order; the `--sql`
    downgrade fails saying it must run online and emits no DDL of its own."""
    upgrade_buffer = io.StringIO()
    command.upgrade(
        Config("alembic.ini", output_buffer=upgrade_buffer),
        f"{_DOWN_REVISION}:{_REVISION}",
        sql=True,
    )
    emitted = _normalised(upgrade_buffer.getvalue())
    order = [
        f"CREATE TRIGGER {_GUARD_INSERT} BEFORE INSERT ON PRICING_RULES FOR EACH ROW PRECEDES"
        f" {_MAIN_INSERT}",
        f"CREATE TRIGGER {_GUARD_UPDATE} BEFORE UPDATE ON PRICING_RULES FOR EACH ROW PRECEDES"
        f" {_MAIN_UPDATE}",
        "ALTER TABLE PRICING_RULES ADD COLUMN PROJECT_ID BIGINT NULL AFTER TENANT_ID",
        f"DROP TRIGGER IF EXISTS {_MAIN_UPDATE}",
        f"CREATE TRIGGER {_MAIN_UPDATE} BEFORE UPDATE ON PRICING_RULES",
        f"DROP TRIGGER {_GUARD_INSERT}",
        f"DROP TRIGGER {_GUARD_UPDATE}",
    ]
    positions = [emitted.index(_normalised(part)) for part in order]
    assert positions == sorted(positions)
    assert "@@LOG_BIN" not in emitted

    downgrade_buffer = io.StringIO()
    with pytest.raises(RuntimeError) as raised:
        command.downgrade(
            Config("alembic.ini", output_buffer=downgrade_buffer),
            f"{_REVISION}:{_DOWN_REVISION}",
            sql=True,
        )
    assert "online" in str(raised.value)
    emitted = _normalised(downgrade_buffer.getvalue())
    assert "ALTER TABLE PRICING_RULES" not in emitted
    assert "TRIGGER" not in emitted


@needs_mysql
def test_0030_structure_at_head(alembic_config: Config, engine: Engine) -> None:
    _start(alembic_config, engine)
    try:
        _assert_head_structure(engine)
        assert _guards(engine) == set()
    finally:
        _finish(alembic_config, engine, None)


@needs_mysql
def test_0030_upgrade_leaves_existing_rules_as_they_were(
    alembic_config: Config, engine: Engine
) -> None:
    """Design §7 "迁移不改已有规则" / INV-6: five published rules (one referred to by an event) at
    0029; after the upgrade every column but `scope_key` is the same, `scope_key` has one more
    `:0`, `project_id` is NULL; the event's `pricing_rule_id` is the same and resolving it again
    with today's code picks the same rule."""
    _start(alembic_config, engine)
    seed = None
    try:
        _at_0029(alembic_config, engine)
        seed = _committed_seed(engine)
        published = _five_published(engine, seed)
        referred = published["CUSTOMER_PROVIDER_MODEL"]
        event_id = _insert_event(engine, seed, referred)
        before = _rows(engine)
        assert all("project_id" not in row for row in before)

        command.upgrade(alembic_config, _REVISION)

        after = _rows(engine)
        assert len(after) == len(before) == 5
        for old, new in zip(before, after, strict=True):
            assert new["project_id"] is None
            parts = str(old["scope_key"]).split(":")
            assert new["scope_key"] == ":".join([parts[0], parts[1], "0", *parts[2:]])
            unchanged = {k: v for k, v in new.items() if k not in {"project_id", "scope_key"}}
            assert unchanged == {k: v for k, v in old.items() if k != "scope_key"}
        assert len({row["scope_key"] for row in after}) == 5
        with engine.connect() as connection:
            event_rule = connection.execute(
                select(UsageEvent.pricing_rule_id).where(UsageEvent.id == event_id)
            ).scalar_one()
        assert event_rule == referred
        with create_session_factory(engine)() as session:
            found = resolve_pricing_rule(
                session, seed.tenant_id, seed.provider_id, seed.model_id, _OCCURRED_AT
            )
            session.commit()
        public_ids = {row["id"]: row["public_id"] for row in after}
        assert found is not None
        assert found.public_id == public_ids[referred]
    finally:
        _finish(alembic_config, engine, seed)


@needs_mysql
@pytest.mark.parametrize("left", ["alter only", "alter and trigger dropped", "complete"])
def test_0030_upgrade_completes_an_interrupted_upgrade(
    alembic_config: Config, engine: Engine, left: str
) -> None:
    """Design §7 "补完中断的迁移": ① only the table's ALTER was run; ② the ALTER and the trigger's
    DROP, not its CREATE; ③ everything, only the version not written. Each rerun ends with the
    structure of one uninterrupted upgrade and no guard; ③ sends no DDL (no guard either)."""
    _start(alembic_config, engine)
    migration = _load()
    try:
        reference = _structure(engine)
        if left == "complete":
            _set_version(engine, _DOWN_REVISION)
        else:
            command.downgrade(alembic_config, _DOWN_REVISION)
            _execute(engine, migration._ALTER_UPGRADE)
            if left == "alter and trigger dropped":
                _execute(engine, f"DROP TRIGGER {_MAIN_UPDATE}")

        with _statements() as statements:
            command.upgrade(alembic_config, _REVISION)

        ddl = _ddl(statements)
        if left == "complete":
            assert ddl == []
        else:
            heads = [_head(s) for s in ddl]
            create_main = [f"CREATE TRIGGER {_MAIN_UPDATE.upper()}"]
            if left == "alter only":
                create_main = [f"DROP TRIGGER {_MAIN_UPDATE.upper()}", *create_main]
            assert heads == [
                f"CREATE TRIGGER {_GUARD_INSERT.upper()}",
                f"CREATE TRIGGER {_GUARD_UPDATE.upper()}",
                *create_main,
                f"DROP TRIGGER {_GUARD_INSERT.upper()}",
                f"DROP TRIGGER {_GUARD_UPDATE.upper()}",
            ]
            assert f"PRECEDES {_MAIN_INSERT.upper()}" in _normalised(ddl[0])
            # Without its main trigger the UPDATE guard is built without PRECEDES.
            precedes = f"PRECEDES {_MAIN_UPDATE.upper()}" in _normalised(ddl[1])
            assert precedes == (left == "alter only")
        assert _version(engine) == _REVISION
        assert _structure(engine) == reference
        assert _guards(engine) == set()
    finally:
        _finish(alembic_config, engine, None)


@needs_mysql
def test_0030_downgrade_completes_an_interrupted_downgrade(
    alembic_config: Config, engine: Engine
) -> None:
    """Design §7 "补完中断的降级": ① the guards removed by hand, only the downgrade's ALTER run —
    the 0030 trigger now refers to a column that is gone, so an UPDATE in between fails (1054)
    instead of slipping past the period check; the rerun downgrade ends at 0029's structure.
    ② a rerun of a complete downgrade sends no DDL and does not query the rules."""
    _start(alembic_config, engine)
    migration = _load()
    seed = None
    try:
        command.downgrade(alembic_config, _DOWN_REVISION)
        at_0029 = _structure(engine)
        command.upgrade(alembic_config, _REVISION)
        seed = _committed_seed(engine)
        with engine.begin() as connection:
            draft = _insert_rule(connection, _rule_values(seed, "GLOBAL"))

        _execute(engine, migration._ALTER_DOWNGRADE)
        assert _guards(engine) == set()
        code, _ = _refusal(
            engine,
            f"UPDATE {_RULES} SET updated_at = :now WHERE id = :id",
            {"now": _T1, "id": draft},
        )
        assert code == _ER_BAD_FIELD

        command.downgrade(alembic_config, _DOWN_REVISION)
        assert _version(engine) == _DOWN_REVISION
        assert _structure(engine) == at_0029

        _set_version(engine, _REVISION)
        with _statements() as statements:
            command.downgrade(alembic_config, _DOWN_REVISION)
        assert _ddl(statements) == []
        assert not [s for s in statements if f"FROM {_RULES.upper()}" in _normalised(s)]
        assert _version(engine) == _DOWN_REVISION
        assert _structure(engine) == at_0029
    finally:
        _finish(alembic_config, engine, seed)


# Design §7 "只有一个隔离触发器": (direction, where it stopped) → the statement it stopped before.
_ONE_GUARD_LEFT = {
    ("upgrade", "only the INSERT guard built"): f"CREATE TRIGGER {_GUARD_UPDATE.upper()}",
    ("upgrade", "only the INSERT guard dropped"): f"DROP TRIGGER {_GUARD_UPDATE.upper()}",
    ("downgrade", "only the INSERT guard built"): f"CREATE TRIGGER {_GUARD_UPDATE.upper()}",
    ("downgrade", "only the INSERT guard dropped"): f"DROP TRIGGER {_GUARD_UPDATE.upper()}",
}


@needs_mysql
@pytest.mark.parametrize(("direction", "where"), list(_ONE_GUARD_LEFT))
def test_0030_rerun_with_one_guard_left(
    alembic_config: Config, engine: Engine, direction: str, where: str
) -> None:
    """Built only one: the rerun builds the other and completes. Dropped only one (steps 3 and 4,
    or 4 and 5 of the downgrade, done): the rerun sends exactly one DROP TRIGGER. Before each
    rerun the one guard left still refuses its kind of write."""
    _start(alembic_config, engine)
    seed = None
    try:
        command.downgrade(alembic_config, _DOWN_REVISION)
        at_0029 = _structure(engine)
        command.upgrade(alembic_config, _REVISION)
        at_head = _structure(engine)
        if direction == "upgrade":
            command.downgrade(alembic_config, _DOWN_REVISION)
        seed = _committed_seed(engine)
        _, draft = _published_and_draft(engine, seed)
        writes = _writes(seed, 0, draft)

        run = command.upgrade if direction == "upgrade" else command.downgrade
        target = _REVISION if direction == "upgrade" else _DOWN_REVISION
        with _interrupt_before(_ONE_GUARD_LEFT[(direction, where)]):
            with pytest.raises(_Interrupted):
                run(alembic_config, target)

        built_one = where == "only the INSERT guard built"
        left = _GUARD_INSERT if built_one else _GUARD_UPDATE
        assert _guards(engine) == {left}
        statement, params = writes["insert"] if built_one else _touch(draft)
        assert _refusal(engine, statement, params) == (_ER_SIGNAL_EXCEPTION, _GUARD_MESSAGE)

        with _statements() as statements:
            run(alembic_config, target)

        heads = [_head(s) for s in _ddl(statements)]
        if built_one:
            assert heads[0] == f"CREATE TRIGGER {_GUARD_UPDATE.upper()}"
            assert heads[-2:] == [
                f"DROP TRIGGER {_GUARD_INSERT.upper()}",
                f"DROP TRIGGER {_GUARD_UPDATE.upper()}",
            ]
        else:
            assert heads == [f"DROP TRIGGER {_GUARD_UPDATE.upper()}"]
        assert _version(engine) == target
        assert _guards(engine) == set()
        assert _structure(engine) == (at_head if direction == "upgrade" else at_0029)
    finally:
        _finish(alembic_config, engine, seed)


def _touch(rule_id: int) -> tuple[str, dict[str, object]]:
    return (
        f"UPDATE {_RULES} SET updated_at = :now WHERE id = :id",
        {"now": _T1, "id": rule_id},
    )


@needs_mysql
def test_0030_guards_precede_the_main_triggers(alembic_config: Config, engine: Engine) -> None:
    """Design §7 "隔离触发器的顺序": stopped after building the guards, each guard's ACTION_ORDER is
    below the main trigger of the same timing."""
    _start(alembic_config, engine)
    try:
        _at_0029(alembic_config, engine)
        with _interrupt_before("ALTER TABLE PRICING_RULES"):
            with pytest.raises(_Interrupted):
                command.upgrade(alembic_config, _REVISION)

        triggers = _triggers(engine)
        assert _guards(engine) == set(_GUARDS)
        assert triggers[_GUARD_INSERT][:2] == triggers[_MAIN_INSERT][:2] == ("BEFORE", "INSERT")
        assert triggers[_GUARD_UPDATE][:2] == triggers[_MAIN_UPDATE][:2] == ("BEFORE", "UPDATE")
        assert triggers[_GUARD_INSERT][2] < triggers[_MAIN_INSERT][2]
        assert triggers[_GUARD_UPDATE][2] < triggers[_MAIN_UPDATE][2]
    finally:
        _finish(alembic_config, engine, None)


@needs_mysql
def test_0030_upgrade_refuses_a_wrong_shape_before_any_ddl(
    alembic_config: Config, engine: Engine
) -> None:
    """Design §7 "形状不符": `project_id` added by hand, the checks not changed — the upgrade fails,
    sends no DDL, builds no guard; the version stays 0029."""
    _start(alembic_config, engine)
    added = False
    try:
        _at_0029(alembic_config, engine)
        _execute(engine, f"ALTER TABLE {_RULES} ADD COLUMN project_id BIGINT NULL AFTER tenant_id")
        added = True
        before = _structure(engine)

        with _statements() as statements:
            with pytest.raises(RuntimeError) as raised:
                command.upgrade(alembic_config, _REVISION)

        assert _ddl(statements) == []
        message = str(raised.value)
        assert "No DDL was issued" in message
        assert "ck_pricing_rules_priority_scope" in message
        assert "fk_pricing_rules_project" in message
        assert _guards(engine) == set()
        assert _version(engine) == _DOWN_REVISION
        assert _structure(engine) == before
    finally:
        if added:
            _execute(engine, f"ALTER TABLE {_RULES} DROP COLUMN project_id")
        _finish(alembic_config, engine, None)


# Design §7 "迁移期间的写入隔离": where the upgrade stopped → the statement it stopped before.
_UPGRADE_STOPS = {
    "after the guards": "ALTER TABLE PRICING_RULES",
    "after the ALTER": f"DROP TRIGGER {_MAIN_UPDATE.upper()}",
    "after the trigger's DROP": f"CREATE TRIGGER {_MAIN_UPDATE.upper()}",
}


@needs_mysql
@pytest.mark.parametrize("stop", list(_UPGRADE_STOPS))
def test_0030_writes_are_refused_while_upgrading(
    alembic_config: Config, engine: Engine, stop: str
) -> None:
    """Every write is refused with the guard's message and the table is unchanged; after the
    rerun the guards are gone, the same illegal writes get the main trigger's own messages and
    the legal one goes in."""
    _start(alembic_config, engine)
    seed = None
    try:
        _at_0029(alembic_config, engine)
        seed = _committed_seed(engine)
        published, draft = _published_and_draft(engine, seed)
        writes = _writes(seed, published, draft)
        with _interrupt_before(_UPGRADE_STOPS[stop]):
            with pytest.raises(_Interrupted):
                command.upgrade(alembic_config, _REVISION)
        assert _guards(engine) == set(_GUARDS)
        assert _version(engine) == _DOWN_REVISION

        _assert_all_guarded(engine, writes)

        command.upgrade(alembic_config, _REVISION)
        assert _guards(engine) == set()
        statement, params = writes["change published"]
        assert _refusal(engine, statement, params) == (_ER_SIGNAL_EXCEPTION, _IMMUTABLE_MESSAGE)
        statement, params = writes["overlap"]
        assert _refusal(engine, statement, params) == (_ER_SIGNAL_EXCEPTION, _OVERLAP_MESSAGE)
        statement, params = writes["insert"]
        _execute(engine, statement, params)
        assert len(_rows(engine)) == 3
    finally:
        _finish(alembic_config, engine, seed)


# Design §7 "降级期间的写入隔离".
_DOWNGRADE_STOPS = {
    "after the guards": "ALTER TABLE PRICING_RULES",
    "after the ALTER": f"DROP TRIGGER {_MAIN_UPDATE.upper()}",
}


@needs_mysql
@pytest.mark.parametrize("stop", list(_DOWNGRADE_STOPS))
def test_0030_writes_are_refused_while_downgrading(
    alembic_config: Config, engine: Engine, stop: str
) -> None:
    _start(alembic_config, engine)
    seed = None
    try:
        command.downgrade(alembic_config, _DOWN_REVISION)
        at_0029 = _structure(engine)
        command.upgrade(alembic_config, _REVISION)
        seed = _committed_seed(engine)
        published, draft = _published_and_draft(engine, seed)
        writes = _writes(seed, published, draft)
        with _interrupt_before(_DOWNGRADE_STOPS[stop]):
            with pytest.raises(_Interrupted):
                command.downgrade(alembic_config, _DOWN_REVISION)
        assert _guards(engine) == set(_GUARDS)
        assert _version(engine) == _REVISION

        _assert_all_guarded(engine, writes)

        command.downgrade(alembic_config, _DOWN_REVISION)
        assert _version(engine) == _DOWN_REVISION
        assert _guards(engine) == set()
        assert _structure(engine) == at_0029
    finally:
        _finish(alembic_config, engine, seed)


@needs_mysql
@pytest.mark.parametrize("direction", ["upgrade", "downgrade"])
def test_0030_precheck_refuses_before_any_ddl(
    alembic_config: Config, engine: Engine, direction: str
) -> None:
    """Design §7 "预检": binlog on and the switch off — both directions fail before any DDL with
    0014's message; nothing changes."""
    _start(alembic_config, engine)
    try:
        if direction == "upgrade":
            command.downgrade(alembic_config, _DOWN_REVISION)
        version = _version(engine)
        before = _structure(engine)
        run = command.upgrade if direction == "upgrade" else command.downgrade
        target = _REVISION if direction == "upgrade" else _DOWN_REVISION

        with _binlog_without_the_switch(), _statements() as statements:
            with pytest.raises(RuntimeError) as raised:
                run(alembic_config, target)

        assert str(raised.value) == _load_0014()._PREREQUISITE_ERROR
        assert _ddl(statements) == []
        assert _version(engine) == version
        assert _structure(engine) == before
    finally:
        _finish(alembic_config, engine, None)


@needs_mysql
def test_0030_downgrade_refuses_with_a_project_rule(alembic_config: Config, engine: Engine) -> None:
    """Design §7 "降级拒绝" / INV-6: a PROJECT draft — the downgrade fails saying why, after
    removing its guards; the table, its rows and its triggers are as before; the version stays
    0030."""
    _start(alembic_config, engine)
    seed = None
    try:
        seed = _committed_seed(engine)
        with engine.begin() as connection:
            _insert_rule(connection, _rule_values(seed, "PROJECT"))
        before = (_structure(engine), _rows(engine))

        with _statements() as statements:
            with pytest.raises(RuntimeError) as raised:
                command.downgrade(alembic_config, _DOWN_REVISION)

        message = str(raised.value)
        assert "project-level pricing rules" in message
        assert "pricing_rule_id" in message
        assert [_head(s) for s in _ddl(statements)] == [
            f"CREATE TRIGGER {_GUARD_INSERT.upper()}",
            f"CREATE TRIGGER {_GUARD_UPDATE.upper()}",
            f"DROP TRIGGER {_GUARD_INSERT.upper()}",
            f"DROP TRIGGER {_GUARD_UPDATE.upper()}",
        ]
        assert _version(engine) == _REVISION
        assert (_structure(engine), _rows(engine)) == before
    finally:
        _finish(alembic_config, engine, seed)


@needs_mysql
def test_0030_downgrade_restores_0014(alembic_config: Config, engine: Engine) -> None:
    """Design §7 "降级恢复": `pricing_rules` as 0014 built it (down to 0013 and up to 0029), then
    five rules; 0030 up and down again — the structure is 0029's word for word and the rules are
    as they were (their `scope_key` back to the 0014 form)."""
    _start(alembic_config, engine)
    seed = None
    try:
        command.downgrade(alembic_config, _BEFORE_0014)
        command.upgrade(alembic_config, _DOWN_REVISION)
        pristine = _structure(engine)
        seed = _committed_seed(engine)
        _five_published(engine, seed)
        rules = _rows(engine)

        command.upgrade(alembic_config, _REVISION)
        command.downgrade(alembic_config, _DOWN_REVISION)

        assert _version(engine) == _DOWN_REVISION
        assert _structure(engine) == pristine
        assert _rows(engine) == rules
        assert _guards(engine) == set()
    finally:
        _finish(alembic_config, engine, seed)


@contextmanager
def _rolled_back(alembic_config: Config, engine: Engine) -> Iterator[tuple[Connection, _Seed]]:
    """At head, a seed and whatever the case writes in one transaction that is rolled back; the
    rule tables are truncated before and after (design §7 "MySQL 用例的清理")."""
    _start(alembic_config, engine)
    try:
        with engine.connect() as connection:
            transaction = connection.begin()
            try:
                yield connection, _seed(connection)
            finally:
                transaction.rollback()
    finally:
        _finish(alembic_config, engine, None)


def _refused_in(connection: Connection, write: Callable[[], object]) -> tuple[int, str]:
    with pytest.raises(DBAPIError) as raised:
        with connection.begin_nested():
            write()
    code, message = raised.value.orig.args[:2]
    return int(code), str(message)


@needs_mysql
@pytest.mark.parametrize("level", list(_LEVELS))
def test_0030_mysql_lets_in_each_legal_combination(
    alembic_config: Config, engine: Engine, level: str
) -> None:
    """Design §7 "CHECK 只放行八种组合" on the migrated MySQL; the key has the project part."""
    with _rolled_back(alembic_config, engine) as (connection, seed):
        rule_id = _insert_rule(connection, _rule_values(seed, level))
        key = connection.execute(
            text(f"SELECT scope_key FROM {_RULES} WHERE id = :id"), {"id": rule_id}
        ).scalar_one()
        values = _scope_values(seed, level)
        assert key == ":".join([level, *(str(values[c] or 0) for c in _SCOPE_COLUMNS)])


@needs_mysql
@pytest.mark.parametrize(("level", "flipped"), _FLIPPED)
def test_0030_mysql_refuses_each_flipped_combination(
    alembic_config: Config, engine: Engine, level: str, flipped: str
) -> None:
    with _rolled_back(alembic_config, engine) as (connection, seed):
        values = _rule_values(seed, level)
        values.update(_scope_values(seed, level, flipped))

        code, _ = _refused_in(connection, lambda: _insert_rule(connection, values))

        assert code == _ER_CHECK_CONSTRAINT_VIOLATED
        assert connection.execute(text(f"SELECT COUNT(*) FROM {_RULES}")).scalar_one() == 0


@needs_mysql
@pytest.mark.parametrize("level", _PROJECT_LEVELS)
def test_0030_project_must_belong_to_the_tenant(
    alembic_config: Config, engine: Engine, level: str
) -> None:
    """Design §7 "项目属于租户" / INV-8: tenant A with tenant B's project — the foreign key
    refuses."""
    with _rolled_back(alembic_config, engine) as (connection, seed):
        values = _rule_values(seed, level, project_id=seed.other_project_id)

        code, _ = _refused_in(connection, lambda: _insert_rule(connection, values))

        assert code == _ER_NO_REFERENCED_ROW


@needs_mysql
def test_0030_two_projects_have_two_timelines(alembic_config: Config, engine: Engine) -> None:
    """Design §7 "两个项目各自一条时间线": one customer's two projects each publish an open PROJECT
    rule (`open_slot` does not clash); on one project a second open rule is refused until the first
    is cut, then goes in."""
    with _rolled_back(alembic_config, engine) as (connection, seed):
        p1 = _insert_rule(connection, _rule_values(seed, "PROJECT"))
        p2 = _insert_rule(
            connection, _rule_values(seed, "PROJECT", project_id=seed.second_project_id)
        )
        _publish(connection, seed, p1, _T0)
        _publish(connection, seed, p2, _T0)
        second = _insert_rule(connection, _rule_values(seed, "PROJECT"))

        refused = _refused_in(connection, lambda: _publish(connection, seed, second, _T1))
        assert refused == (_ER_SIGNAL_EXCEPTION, _OVERLAP_MESSAGE)

        connection.execute(_TRUNCATE_AT, {"effective_to": _T1, "now": _NOW, "id": p1})
        _publish(connection, seed, second, _T1)
        slots = dict(
            connection.execute(
                text(f"SELECT id, open_slot FROM {_RULES} WHERE id IN (:a, :b, :c)"),
                {"a": p1, "b": p2, "c": second},
            ).all()
        )
        assert slots == {p1: None, p2: 1, second: 1}
        keys = dict(connection.execute(text(f"SELECT id, scope_key FROM {_RULES}")).all())
        assert keys[p1] == keys[second] == f"PROJECT:{seed.tenant_id}:{seed.project_id}:0:0"
        assert keys[p2] == f"PROJECT:{seed.tenant_id}:{seed.second_project_id}:0:0"


@needs_mysql
def test_0030_a_published_rules_project_cannot_change(
    alembic_config: Config, engine: Engine
) -> None:
    """Design §7 "发布后项目不可改" / INV-6: the rebuilt trigger refuses a new `project_id`."""
    with _rolled_back(alembic_config, engine) as (connection, seed):
        rule_id = _insert_rule(connection, _rule_values(seed, "PROJECT"))
        _publish(connection, seed, rule_id, _T0)

        refused = _refused_in(
            connection,
            lambda: connection.execute(
                text(f"UPDATE {_RULES} SET project_id = :project_id WHERE id = :id"),
                {"project_id": seed.second_project_id, "id": rule_id},
            ),
        )

        assert refused == (_ER_SIGNAL_EXCEPTION, _IMMUTABLE_MESSAGE)

"""0032_pricing_rule_backstop（AIH-TASK-146，设计闸门 #539 v1）.

Design §2 "设计概要" (the pricing rule row), §3 INV-6, §4, §5 (the migration rows), §7 (the
migration rows, "迁移结构" to "降级不需要数据前提"), §8.

The first cases need no database: the revision chain; the new trigger's body differs from 0030's
in exactly P1–P5 of design §2 (line by line) and holds no sentinel date; the frozen copy of
0030's trigger is 0030's; the `--sql` upgrade and downgrade emit the design order without
reading; the precheck refuses only binlog without the switch, before any DDL, both ways; a
complete run sends nothing; the trigger comparison drops only the blanks at either end.

On a real MySQL (`BILLING_TEST_DATABASE_URL`; skipped when unset, never skipped in CI): the
structure at 0032, the offline upgrade and downgrade, existing rows untouched, the interrupted
upgrade and downgrade (two places each), the intermediate state no weaker than before the
migration, the restoring downgrade, the shape check (six objects) and the downgrade with rows of
every state. What the new trigger refuses or lets through, written directly, is in
test_pricing_rules_service.py.

What is observed is 0032 alone (design §7 "迁移用例的观察位置"): every case goes back to head
first and then down to 0032 (outside the part under observation); an upgrade is only observed
from 0031 to 0032 and a downgrade from 0032 to 0031 — statements are never counted from head, so
later revisions do not affect this module. Interruptions are made by raising from a
`before_cursor_execute` listener just before the statement the step would send next. Rows are
written with SQL text (MARKUP rules on GLOBAL_PROVIDER_MODEL, one model per timeline). Every case
ends with `TRUNCATE` of the two rule tables (foreign key checks off; no trigger is dropped or
rebuilt), back at head, and checks that `pricing_rules` is empty and its only BEFORE UPDATE
trigger is the new one.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import io
import os
import pathlib
import re
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import Connection, create_engine, delete, event, insert, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError

from alembic import command
from app.models import auth as _auth_models  # noqa: F401 - 让 Base.metadata 装上这些表
from app.models.ai_catalog import AiModel, AiModelAlias, AiProvider
from app.models.auth import User, UserRole, UserStatus

TEST_DATABASE_URL = os.environ.get("BILLING_TEST_DATABASE_URL", "")

needs_mysql = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="BILLING_TEST_DATABASE_URL is not set; migration tests need a real MySQL",
)

_REVISION = "0032_pricing_rule_backstop"
_DOWN_REVISION = "0031_provider_price_backstop"
_MIGRATION = pathlib.Path("alembic/versions/20261101_0032_pricing_rule_backstop.py")
_MIGRATION_0030 = pathlib.Path("alembic/versions/20261030_0030_pricing_rule_project_scope.py")

_RULES = "pricing_rules"
_COMPONENTS = "pricing_rule_components"
_OLD = "trg_pricing_rules_before_update"
_NEW = "trg_pricing_rules_before_update_v2"

# Trigger → (table, timing, event) on `pricing_rules` at 0032.
_TRIGGERS_AT_0032 = {
    "trg_pricing_rules_before_insert": (_RULES, "BEFORE", "INSERT"),
    _NEW: (_RULES, "BEFORE", "UPDATE"),
    "trg_pricing_rules_before_delete": (_RULES, "BEFORE", "DELETE"),
}

_ER_SIGNAL_EXCEPTION = 1644

_PRECHECK = "SELECT @@log_bin, @@log_bin_trust_function_creators"
_ALEMBIC_VERSION = text("SELECT version_num FROM alembic_version")
_SET_VERSION = text("UPDATE alembic_version SET version_num = :version")

T0 = dt.datetime(2026, 11, 1, 8, 0, 0)
SECOND = dt.timedelta(seconds=1)
DAY = dt.timedelta(days=1)
# Design §7 "相交判定的边界": an existing period that starts on the last day DATETIME holds.
SENTINEL_DAY = dt.datetime(9999, 12, 31, 0, 0, 0)
_TEST_EMAIL_DOMAIN = "@pricing-rule-backstop-migration-test.example.com"


def _load(path: pathlib.Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_0032() -> ModuleType:
    return _load(_MIGRATION, "migration_0032")


def _load_0030() -> ModuleType:
    return _load(_MIGRATION_0030, "migration_0030")


def _spaces(statement: str) -> str:
    return " ".join(statement.split())


def _normalised(statement: str) -> str:
    return re.sub(r"\s+", " ", statement.replace("`", "")).strip().upper()


def _head(statement: str) -> str:
    """The first three words: `CREATE TRIGGER <name>`, `DROP TRIGGER <name>`, …"""
    return " ".join(_normalised(statement).split(" ")[:3])


def _body_lines(statement: str) -> list[str]:
    """The body after `FOR EACH ROW`, one stripped line each."""
    return [line.strip() for line in statement.split("FOR EACH ROW", 1)[1].strip().splitlines()]


def _replace_block(lines: list[str], old: list[str], new: list[str]) -> list[str]:
    """`old` must be found exactly once, as consecutive lines."""
    starts = [i for i in range(len(lines) - len(old) + 1) if lines[i : i + len(old)] == old]
    assert len(starts) == 1, old
    start = starts[0]
    return [*lines[:start], *new, *lines[start + len(old) :]]


# ---------------------------------------------------------------------------
# No database
# ---------------------------------------------------------------------------


def test_0032_follows_0031() -> None:
    migration = _load_0032()

    assert migration.revision == _REVISION
    assert migration.down_revision == _DOWN_REVISION
    # `alembic_version.version_num` is VARCHAR(32).
    assert len(migration.revision) <= 32


def test_0032_new_trigger_is_0030s_with_exactly_p1_to_p5() -> None:
    """Design §2 "新触发器正文": applying P1–P5, line by line, to 0030's body gives the new body;
    every other line is 0030's. No sentinel date is left."""
    migration = _load_0032()
    old_statement = _load_0030()._TRIGGERS[_OLD]
    new_statement = migration._TRIGGERS[_NEW]

    lines = _body_lines(old_statement)
    # P1: RETIRED and DISCARDED are both final.
    lines = _replace_block(
        lines,
        [
            "IF OLD.status = 'DISCARDED' THEN",
            "SIGNAL SQLSTATE '45000'",
            "SET MESSAGE_TEXT = 'a discarded pricing rule is final';",
        ],
        [
            "IF OLD.status IN ('RETIRED', 'DISCARDED') THEN",
            "SIGNAL SQLSTATE '45000'",
            "SET MESSAGE_TEXT = 'a retired or discarded pricing rule is final';",
        ],
    )
    # P2: right after "a draft only becomes …", a publish has no end.
    draft_block = [
        "SET MESSAGE_TEXT = 'a draft pricing rule is published or discarded';",
        "END IF;",
    ]
    lines = _replace_block(
        lines,
        draft_block,
        [
            *draft_block,
            "IF OLD.status = 'DRAFT' AND NEW.status = 'PUBLISHED' AND NEW.effective_to IS NOT NULL"
            " THEN",
            "SIGNAL SQLSTATE '45000'",
            "SET MESSAGE_TEXT = 'a pricing rule is published without an end';",
            "END IF;",
        ],
    )
    # P3: the immutability block only for PUBLISHED.
    lines = _replace_block(
        lines,
        ["IF OLD.status IN ('PUBLISHED', 'RETIRED') THEN"],
        ["IF OLD.status = 'PUBLISHED' THEN"],
    )
    # P4: intersection by NULL, term by term.
    lines = _replace_block(
        lines,
        [
            "AND COALESCE(o.effective_from, '1000-01-01')",
            "< COALESCE(NEW.effective_to, '9999-12-31')",
            "AND COALESCE(NEW.effective_from, '1000-01-01')",
            "< COALESCE(o.effective_to, '9999-12-31')) THEN",
        ],
        [
            "AND (o.effective_from IS NULL OR NEW.effective_to IS NULL",
            "OR o.effective_from < NEW.effective_to)",
            "AND (NEW.effective_from IS NULL OR o.effective_to IS NULL",
            "OR NEW.effective_from < o.effective_to)) THEN",
        ],
    )
    # P5: inside the immutability block, after "can only be retired": the end changes three ways.
    retire_block = [
        "SET MESSAGE_TEXT = 'a published pricing rule can only be retired';",
        "END IF;",
    ]
    lines = _replace_block(
        lines,
        retire_block,
        [
            *retire_block,
            "IF (NOT (NEW.effective_to <=> OLD.effective_to)",
            "AND OLD.effective_to IS NOT NULL",
            "AND NOT (NEW.effective_to IS NULL AND NEW.status = OLD.status))",
            "OR (NEW.status = 'RETIRED' AND OLD.effective_to IS NOT NULL) THEN",
            "SIGNAL SQLSTATE '45000'",
            "SET MESSAGE_TEXT =",
            "'a published pricing rule ends only by truncation, restore or retirement';",
            "END IF;",
        ],
    )

    assert lines == _body_lines(new_statement)
    for sentinel in ("'1000-01-01'", "'9999-12-31'"):
        assert sentinel in old_statement
        assert sentinel not in new_statement
    # The header: the new name, the same timing and table, no FOLLOWS / PRECEDES.
    assert _spaces(old_statement.split("FOR EACH ROW", 1)[0]) == (
        f"CREATE TRIGGER {_OLD} BEFORE UPDATE ON {_RULES}"
    )
    assert _spaces(new_statement.split("FOR EACH ROW", 1)[0]) == (
        f"CREATE TRIGGER {_NEW} BEFORE UPDATE ON {_RULES}"
    )
    assert "FOLLOWS" not in new_statement
    assert "PRECEDES" not in new_statement
    assert set(migration._TRIGGERS) == {_NEW}
    # ruff's line length holds for the new lines as well.
    assert max(len(line) for line in new_statement.splitlines()) <= 100


def test_0032_frozen_copy_is_0030s_word_for_word() -> None:
    migration = _load_0032()

    assert set(migration._TRIGGERS_0030) == {_OLD}
    assert migration._TRIGGERS_0030[_OLD] == _load_0030()._TRIGGERS[_OLD]


def test_0032_does_not_import_other_migrations() -> None:
    source = _MIGRATION.read_text(encoding="utf-8")

    assert "importlib" not in source
    assert "alembic/versions" not in source
    assert re.search(r"^\s*(from|import)\s+.*0030", source, re.MULTILINE) is None


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


def test_0032_offline_upgrade_and_downgrade_emit_the_design_order(monkeypatch) -> None:
    migration = _load_0032()
    up = _OfflineOp()
    monkeypatch.setattr(migration, "op", up)
    migration.upgrade()
    down = _OfflineOp()
    monkeypatch.setattr(migration, "op", down)
    migration.downgrade()

    assert [_spaces(s) for s in up.statements] == [
        _spaces(migration._TRIGGERS[_NEW]),
        f"DROP TRIGGER IF EXISTS {_OLD}",
    ]
    assert [_spaces(s) for s in down.statements] == [
        _spaces(migration._TRIGGERS_0030[_OLD]),
        f"DROP TRIGGER IF EXISTS {_NEW}",
    ]


class _Bind:
    """Answers the migration's two queries from fixed rows."""

    def __init__(
        self,
        migration: ModuleType,
        *,
        log_bin: int = 0,
        trusted: int = 0,
        triggers: list[tuple[str, str, str, str, str]] | None = None,
    ) -> None:
        self.answers: dict[str, Any] = {
            migration._PREREQUISITE_QUERY: SimpleNamespace(one=lambda: (log_bin, trusted)),
            migration._TRIGGERS_QUERY: SimpleNamespace(all=lambda: list(triggers or [])),
        }

    def execute(self, statement: object) -> Any:
        return self.answers[str(statement)]


class _OnlineOp(_OfflineOp):
    def __init__(self, bind: _Bind) -> None:
        super().__init__()
        self.bind = bind

    def get_context(self) -> SimpleNamespace:
        return SimpleNamespace(as_sql=False, dialect=SimpleNamespace(name="mysql"))

    def get_bind(self) -> _Bind:
        return self.bind


def _trigger_row(name: str, statement: str) -> tuple[str, str, str, str, str]:
    """A TRIGGERS row as MySQL keeps it: the body after `FOR EACH ROW`."""
    return (name, _RULES, "BEFORE", "UPDATE", statement.split("FOR EACH ROW", 1)[1])


def _at_0031(migration: ModuleType, log_bin: int, trusted: int) -> _Bind:
    old = _trigger_row(_OLD, migration._TRIGGERS_0030[_OLD])
    return _Bind(migration, log_bin=log_bin, trusted=trusted, triggers=[old])


def _at_0032(migration: ModuleType, log_bin: int, trusted: int) -> _Bind:
    new = _trigger_row(_NEW, migration._TRIGGERS[_NEW])
    return _Bind(migration, log_bin=log_bin, trusted=trusted, triggers=[new])


@pytest.mark.parametrize("direction", ["upgrade", "downgrade"])
@pytest.mark.parametrize(
    ("log_bin", "trusted", "refused"),
    [(1, 0, True), (1, 1, False), (0, 0, False), (0, 1, False)],
)
def test_0032_precheck_refuses_only_binlog_without_the_switch_before_any_ddl(
    monkeypatch, direction: str, log_bin: int, trusted: int, refused: bool
) -> None:
    """Design §7 "预检": query, decision and message as 0030's. Refused → no statement at all;
    otherwise the same run reaches its DDL (so the empty list is not for want of getting there)."""
    migration = _load_0032()
    earlier = _load_0030()
    assert migration._PREREQUISITE_QUERY == earlier._PREREQUISITE_QUERY == _PRECHECK
    assert migration._PREREQUISITE_ERROR == earlier._PREREQUISITE_ERROR
    at = _at_0031 if direction == "upgrade" else _at_0032
    fake = _OnlineOp(at(migration, log_bin=log_bin, trusted=trusted))
    monkeypatch.setattr(migration, "op", fake)
    run = migration.upgrade if direction == "upgrade" else migration.downgrade

    if refused:
        with pytest.raises(RuntimeError) as raised:
            run()
        assert str(raised.value) == migration._PREREQUISITE_ERROR
        assert fake.statements == []
        return
    run()
    if direction == "upgrade":
        assert fake.statements == [migration._TRIGGERS[_NEW], f"DROP TRIGGER {_OLD}"]
    else:
        assert fake.statements == [migration._TRIGGERS_0030[_OLD], f"DROP TRIGGER {_NEW}"]


@pytest.mark.parametrize("direction", ["upgrade", "downgrade"])
def test_0032_a_complete_run_sends_nothing(monkeypatch, direction: str) -> None:
    """Already done: no statement, not even the precheck (binlog on and the switch off here)."""
    migration = _load_0032()
    at = _at_0032 if direction == "upgrade" else _at_0031
    fake = _OnlineOp(at(migration, log_bin=1, trusted=0))
    monkeypatch.setattr(migration, "op", fake)

    (migration.upgrade if direction == "upgrade" else migration.downgrade)()

    assert fake.statements == []


def test_0032_trigger_comparison_drops_only_the_blanks_at_either_end() -> None:
    migration = _load_0032()
    statement = migration._TRIGGERS[_NEW]
    body = statement.split("FOR EACH ROW", 1)[1]

    assert migration._trigger_matches((_RULES, "BEFORE", "UPDATE", body), statement)
    assert migration._trigger_matches((_RULES, "BEFORE", "UPDATE", body.strip()), statement)
    one_more_space = body.replace(
        "'pricing rule periods overlap'", "'pricing rule  periods overlap'"
    )
    for row in [
        (_RULES, "BEFORE", "UPDATE", one_more_space),
        (_RULES, "AFTER", "UPDATE", body),
        (_RULES, "BEFORE", "INSERT", body),
        (_COMPONENTS, "BEFORE", "UPDATE", body),
    ]:
        assert not migration._trigger_matches(row, statement), row[:3]


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


def _version(engine: Engine) -> str:
    with engine.connect() as connection:
        return str(connection.execute(_ALEMBIC_VERSION).scalar_one())


def _set_version(engine: Engine, version: str) -> None:
    with engine.begin() as connection:
        connection.execute(_SET_VERSION, {"version": version})


def _execute(engine: Engine, statement: str, params: dict[str, object] | None = None) -> None:
    with engine.begin() as connection:
        connection.execute(text(statement), params or {})


def _head_revision(alembic_config: Config) -> str:
    return str(ScriptDirectory.from_config(alembic_config).get_current_head())


def _to_head(alembic_config: Config, engine: Engine) -> None:
    """Head, complete: at 0032 the version goes back to 0031 first, so 0032 runs again — nothing
    when complete, otherwise it finishes the half-done steps."""
    if _version(engine) == _REVISION:
        _set_version(engine, _DOWN_REVISION)
    command.upgrade(alembic_config, "head")


def _clean(engine: Engine) -> None:
    """TRUNCATE (DDL: no trigger fires, none is dropped or rebuilt), foreign key checks off."""
    with engine.begin() as connection:
        connection.exec_driver_sql("SET FOREIGN_KEY_CHECKS = 0")
        try:
            connection.exec_driver_sql(f"TRUNCATE TABLE {_COMPONENTS}")
            connection.exec_driver_sql(f"TRUNCATE TABLE {_RULES}")
        finally:
            connection.exec_driver_sql("SET FOREIGN_KEY_CHECKS = 1")


def _start(alembic_config: Config, engine: Engine) -> None:
    """Head, then down to 0032: what follows is 0032's, whatever later revisions do."""
    _to_head(alembic_config, engine)
    command.downgrade(alembic_config, _REVISION)
    assert _version(engine) == _REVISION
    _clean(engine)


def _triggers(engine: Engine) -> dict[str, tuple[str, str, str, str]]:
    """Triggers on `pricing_rules`, or named like ours anywhere: name → (table, timing, event,
    body). Same-timing order is left out."""
    query = text(
        "SELECT TRIGGER_NAME, EVENT_OBJECT_TABLE, ACTION_TIMING, EVENT_MANIPULATION,"
        " ACTION_STATEMENT FROM information_schema.TRIGGERS WHERE TRIGGER_SCHEMA = DATABASE()"
        f" AND (EVENT_OBJECT_TABLE = '{_RULES}' OR TRIGGER_NAME IN ('{_OLD}', '{_NEW}'))"
    )
    with engine.connect() as connection:
        return {
            str(name): (str(table), str(timing), str(kind), str(body))
            for name, table, timing, kind, body in connection.execute(query).all()
        }


def _trigger_list(engine: Engine) -> dict[str, tuple[str, str, str]]:
    return {name: row[:3] for name, row in _triggers(engine).items()}


def _before_updates(engine: Engine) -> set[str]:
    return {
        name for name, row in _triggers(engine).items() if row[:3] == (_RULES, "BEFORE", "UPDATE")
    }


def _rows(engine: Engine) -> dict[str, list[dict[str, object]]]:
    with engine.connect() as connection:
        return {
            table: [
                dict(row)
                for row in connection.execute(text(f"SELECT * FROM {table} ORDER BY id")).mappings()
            ]
            for table in (_RULES, _COMPONENTS)
        }


def _finish(alembic_config: Config, engine: Engine, seed: _Seed | None = None) -> None:
    """Design §7 cleaning: TRUNCATE, back at head, then the seeded parents; the database ends at
    head with `pricing_rules` empty and the new trigger as its only BEFORE UPDATE."""
    _clean(engine)
    _to_head(alembic_config, engine)
    if seed is not None:
        seed.remove(engine)
    assert _version(engine) == _head_revision(alembic_config)
    assert _rows(engine)[_RULES] == []
    assert _before_updates(engine) == {_NEW}


def _refusal(engine: Engine, statement: str, params: dict[str, object]) -> tuple[int, str]:
    """Runs one write in its own transaction; returns the MySQL error number and message."""
    with pytest.raises(DBAPIError) as raised:
        with engine.begin() as connection:
            connection.execute(text(statement), params)
    code, message = raised.value.orig.args[:2]
    return int(code), str(message)


# --- direct rows (SQL text) -----------------------------------------------------------------------

_INSERT_RULE = (
    f"INSERT INTO {_RULES} (public_id, priority_scope, tenant_id, project_id, provider_id,"
    " model_id, strategy, markup_multiplier, status, effective_from, effective_to, created_by,"
    " approved_by, created_at, updated_at, approved_at) VALUES (:public_id,"
    " 'GLOBAL_PROVIDER_MODEL', NULL, NULL, :provider_id, :model_id, 'MARKUP', 1.23456789, 'DRAFT',"
    " NULL, NULL, :user, NULL, :now, :now, NULL)"
)
_PUBLISH = (
    f"UPDATE {_RULES} SET status = :status, effective_from = :start, effective_to = :end,"
    " approved_by = :user, approved_at = :now WHERE id = :id"
)
_SET_END = f"UPDATE {_RULES} SET effective_to = :end WHERE id = :id"
_RETIRE = f"UPDATE {_RULES} SET status = 'RETIRED', effective_to = :end WHERE id = :id"
_WITHDRAW = f"UPDATE {_RULES} SET status = 'RETIRED', effective_to = effective_from WHERE id = :id"
_DISCARD = f"UPDATE {_RULES} SET status = 'DISCARDED' WHERE id = :id"


@dataclass(frozen=True)
class _Seed:
    """A user, a provider and its models (Core inserts: none of these tables changes in 0032)."""

    user_id: int
    provider_id: int
    model_ids: tuple[int, ...]

    def remove(self, engine: Engine) -> None:
        with engine.begin() as connection:
            connection.execute(
                delete(AiModelAlias).where(AiModelAlias.model_id.in_(self.model_ids))
            )
            connection.execute(delete(AiModel).where(AiModel.id.in_(self.model_ids)))
            connection.execute(delete(AiProvider).where(AiProvider.id == self.provider_id))
            connection.execute(delete(User).where(User.id == self.user_id))


def _seed(engine: Engine, models: int = 5) -> _Seed:
    marker = uuid.uuid4().hex[:12]
    now = dt.datetime(2026, 11, 1, 8, 0, 0)
    with engine.begin() as connection:

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
        provider_id = new_id(
            insert(AiProvider).values(
                public_id=str(uuid.uuid4()),
                code=f"p146-{marker}",
                display_name="Fictional provider",
                created_at=now,
                updated_at=now,
            )
        )
        model_ids = tuple(
            new_id(
                insert(AiModel).values(
                    public_id=str(uuid.uuid4()),
                    provider_id=provider_id,
                    code=f"m146-{marker}-{index}",
                    display_name="Fictional model",
                    created_at=now,
                    updated_at=now,
                )
            )
            for index in range(models)
        )
    return _Seed(user_id=user_id, provider_id=provider_id, model_ids=model_ids)


def _draft(connection: Connection, seed: _Seed, model: int) -> int:
    """A MARKUP draft on GLOBAL_PROVIDER_MODEL of `seed.model_ids[model]`."""
    public_id = str(uuid.uuid4())
    connection.execute(
        text(_INSERT_RULE),
        {
            "public_id": public_id,
            "provider_id": seed.provider_id,
            "model_id": seed.model_ids[model],
            "user": seed.user_id,
            "now": T0,
        },
    )
    found = text(f"SELECT id FROM {_RULES} WHERE public_id = :public_id")
    return int(connection.execute(found, {"public_id": public_id}).scalar_one())


def _publishing(
    seed: _Seed,
    rule_id: int,
    period: tuple[dt.datetime | None, dt.datetime | None],
    status: str = "PUBLISHED",
) -> dict[str, object]:
    start, end = period
    return {
        "status": status,
        "start": start,
        "end": end,
        "user": seed.user_id,
        "now": T0,
        "id": rule_id,
    }


def _published(connection: Connection, seed: _Seed, model: int, start: dt.datetime) -> int:
    """A draft published directly as `[start, NULL)`."""
    rule_id = _draft(connection, seed, model)
    connection.execute(text(_PUBLISH), _publishing(seed, rule_id, (start, None)))
    return rule_id


def _truncated(
    connection: Connection, seed: _Seed, model: int, start: dt.datetime, end: dt.datetime
) -> int:
    """Published as `[start, NULL)`, then truncated to `[start, end)` (the service's two steps)."""
    rule_id = _published(connection, seed, model, start)
    connection.execute(text(_SET_END), {"end": end, "id": rule_id})
    return rule_id


def _rows_of_every_state(engine: Engine, seed: _Seed) -> None:
    """Design §7 "不改已有行": an open published and a truncated published rule (model 0), a
    retired rule with an end (model 1), the empty period a withdrawn reservation leaves (model 2),
    a draft and a discarded draft. Written the service's way, so both triggers let them in."""
    with engine.begin() as connection:
        _truncated(connection, seed, 0, T0, T0 + DAY)
        _published(connection, seed, 0, T0 + DAY)
        retired = _published(connection, seed, 1, T0)
        connection.execute(text(_RETIRE), {"end": T0 + DAY, "id": retired})
        reserved = _published(connection, seed, 2, T0 + 2 * DAY)
        connection.execute(text(_WITHDRAW), {"id": reserved})
        _draft(connection, seed, 0)
        discarded = _draft(connection, seed, 1)
        connection.execute(text(_DISCARD), {"id": discarded})


def _assert_rows_of_every_state(rows: dict[str, list[dict[str, object]]]) -> None:
    states = sorted(
        (
            str(row["status"]),
            row["effective_to"] is None,
            row["effective_from"] is not None and row["effective_from"] == row["effective_to"],
        )
        for row in rows[_RULES]
    )
    assert states == sorted(
        [
            ("PUBLISHED", True, False),  # open
            ("PUBLISHED", False, False),  # truncated
            ("RETIRED", False, False),  # retired with an end
            ("RETIRED", False, True),  # withdrawn reservation
            ("DRAFT", True, False),
            ("DISCARDED", True, False),
        ]
    )


# --- the cases ------------------------------------------------------------------------------------


@needs_mysql
def test_0032_structure(alembic_config: Config, engine: Engine) -> None:
    """Design §7 "迁移结构": at 0032 our table's triggers are exactly BEFORE INSERT, `…_v2` and
    BEFORE DELETE; the new trigger's ACTION_STATEMENT, without the blanks at either end, is the
    frozen body, and the shape comparison says "the same" — what it sees is what MySQL keeps."""
    _start(alembic_config, engine)
    migration = _load_0032()
    try:
        assert _trigger_list(engine) == _TRIGGERS_AT_0032
        body = _triggers(engine)[_NEW][3]
        assert body.strip() == migration._body(migration._TRIGGERS[_NEW])
        with engine.connect() as connection:
            found = migration._triggers(connection)
        assert set(found) == {_NEW}
        assert migration._trigger_matches(found[_NEW], migration._TRIGGERS[_NEW])
    finally:
        _finish(alembic_config, engine)


@needs_mysql
def test_0032_offline_upgrade_and_downgrade(alembic_config: Config) -> None:
    """Design §7 "离线": between 0031 and 0032 only, the `--sql` upgrade emits the new trigger's
    CREATE and the old one's DROP IF EXISTS, in this order, reading nothing; the `--sql`
    downgrade emits the old trigger's CREATE and the new one's DROP IF EXISTS."""
    up_buffer = io.StringIO()
    command.upgrade(
        Config("alembic.ini", output_buffer=up_buffer), f"{_DOWN_REVISION}:{_REVISION}", sql=True
    )
    emitted = _normalised(up_buffer.getvalue())
    order = [f"CREATE TRIGGER {_NEW} BEFORE UPDATE ON {_RULES}", f"DROP TRIGGER IF EXISTS {_OLD};"]
    positions = [emitted.index(_normalised(part)) for part in order]
    assert positions == sorted(positions)
    assert f"CREATE TRIGGER {_OLD} " not in emitted
    assert "@@LOG_BIN" not in emitted
    assert "INFORMATION_SCHEMA" not in emitted

    down_buffer = io.StringIO()
    command.downgrade(
        Config("alembic.ini", output_buffer=down_buffer), f"{_REVISION}:{_DOWN_REVISION}", sql=True
    )
    emitted = _normalised(down_buffer.getvalue())
    order = [f"CREATE TRIGGER {_OLD} BEFORE UPDATE ON {_RULES}", f"DROP TRIGGER IF EXISTS {_NEW};"]
    positions = [emitted.index(_normalised(part)) for part in order]
    assert positions == sorted(positions)
    assert "@@LOG_BIN" not in emitted
    assert "INFORMATION_SCHEMA" not in emitted


@needs_mysql
def test_0032_upgrade_leaves_existing_rows_as_they_were(
    alembic_config: Config, engine: Engine
) -> None:
    """Design §7 "不改已有行" / INV-6: at 0031, rows of every state; after the upgrade both rule
    tables are the same, row by row and column by column."""
    _start(alembic_config, engine)
    seed = None
    try:
        command.downgrade(alembic_config, _DOWN_REVISION)
        seed = _seed(engine)
        _rows_of_every_state(engine, seed)
        before = _rows(engine)
        _assert_rows_of_every_state(before)

        command.upgrade(alembic_config, _REVISION)

        assert _rows(engine) == before
    finally:
        _finish(alembic_config, engine, seed)


@needs_mysql
def test_0032_downgrade_needs_no_data_precondition(alembic_config: Config, engine: Engine) -> None:
    """Design §7 "降级不需要数据前提": with rows of every state the downgrade goes through and
    the rows are as they were."""
    _start(alembic_config, engine)
    seed = None
    try:
        seed = _seed(engine)
        _rows_of_every_state(engine, seed)
        before = _rows(engine)
        _assert_rows_of_every_state(before)

        command.downgrade(alembic_config, _DOWN_REVISION)

        assert _version(engine) == _DOWN_REVISION
        assert _rows(engine) == before
    finally:
        _finish(alembic_config, engine, seed)


# Design §7 "升级中断后重跑": where the upgrade stopped → the statement it stopped before.
_UPGRADE_STOPS = {"both triggers": f"DROP TRIGGER {_OLD.upper()}"}


@needs_mysql
@pytest.mark.parametrize("left", [*_UPGRADE_STOPS, "complete"])
def test_0032_upgrade_completes_an_interrupted_upgrade(
    alembic_config: Config, engine: Engine, left: str
) -> None:
    """① stopped before the old trigger's DROP (both triggers): the rerun sends only that DROP;
    ② everything done, only the version not written: the rerun sends no DDL. Each ends with the
    structure of one uninterrupted upgrade."""
    _start(alembic_config, engine)
    try:
        reference = _triggers(engine)
        if left == "complete":
            _set_version(engine, _DOWN_REVISION)
        else:
            command.downgrade(alembic_config, _DOWN_REVISION)
            with _interrupt_before(_UPGRADE_STOPS[left]):
                with pytest.raises(_Interrupted):
                    command.upgrade(alembic_config, _REVISION)
            assert _version(engine) == _DOWN_REVISION
            assert _before_updates(engine) == {_OLD, _NEW}

        with _statements() as statements:
            command.upgrade(alembic_config, _REVISION)

        heads = [_head(s) for s in _ddl(statements)]
        expected = {"both triggers": [f"DROP TRIGGER {_OLD.upper()}"], "complete": []}
        assert heads == expected[left]
        assert _version(engine) == _REVISION
        assert _triggers(engine) == reference
        assert _trigger_list(engine) == _TRIGGERS_AT_0032
    finally:
        _finish(alembic_config, engine)


@needs_mysql
def test_0032_the_intermediate_state_is_no_weaker_than_before(
    alembic_config: Config, engine: Engine
) -> None:
    """Design §7 "中间状态不弱于迁移之前": where both triggers are there, what 0030's trigger
    already refused is refused (a published rule's multiplier, a draft straight to RETIRED, an
    overlapping period), and so is what this design newly refuses: P1 (a retired rule's end), P2
    (a publish with an end), P4 (a period before one that starts on 9999-12-31) and P5 (a
    truncated rule's end moved). The rows do not change."""
    _start(alembic_config, engine)
    seed = None
    try:
        command.downgrade(alembic_config, _DOWN_REVISION)
        seed = _seed(engine)
        with engine.begin() as connection:
            published = _published(connection, seed, 0, T0)
            draft = _draft(connection, seed, 0)
            retired = _published(connection, seed, 1, T0)
            connection.execute(text(_RETIRE), {"end": T0 + DAY, "id": retired})
            truncated = _truncated(connection, seed, 2, T0, T0 + DAY)
            _truncated(connection, seed, 3, SENTINEL_DAY, SENTINEL_DAY + SECOND)
            before_sentinel = _draft(connection, seed, 3)
            lone_draft = _draft(connection, seed, 4)
        with _interrupt_before(_UPGRADE_STOPS["both triggers"]):
            with pytest.raises(_Interrupted):
                command.upgrade(alembic_config, _REVISION)
        assert _before_updates(engine) == {_OLD, _NEW}
        before = _rows(engine)

        writes = {
            # Refused before the migration already.
            "multiplier": (
                f"UPDATE {_RULES} SET markup_multiplier = 9.99999999 WHERE id = :id",
                {"id": published},
            ),
            "draft to retired": (_PUBLISH, _publishing(seed, draft, (T0, T0 + DAY), "RETIRED")),
            "overlap": (_PUBLISH, _publishing(seed, draft, (T0 + SECOND, None))),
            # Newly refused.
            "P1 retired end": (_SET_END, {"end": T0 + 2 * DAY, "id": retired}),
            "P2 publish with an end": (_PUBLISH, _publishing(seed, lone_draft, (T0, T0 + DAY))),
            "P4 before the last day": (_PUBLISH, _publishing(seed, before_sentinel, (T0, None))),
            "P5 truncated end moved": (_SET_END, {"end": T0 + 2 * DAY, "id": truncated}),
        }
        for name, (statement, params) in writes.items():
            assert _refusal(engine, statement, params)[0] == _ER_SIGNAL_EXCEPTION, name
        assert _rows(engine) == before

        command.upgrade(alembic_config, _REVISION)
        assert _version(engine) == _REVISION
    finally:
        _finish(alembic_config, engine, seed)


# Design §7 "降级中断后重跑".
_DOWNGRADE_STOPS = {"both triggers": f"DROP TRIGGER {_NEW.upper()}"}


@needs_mysql
@pytest.mark.parametrize("left", [*_DOWNGRADE_STOPS, "complete"])
def test_0032_downgrade_completes_an_interrupted_downgrade(
    alembic_config: Config, engine: Engine, left: str
) -> None:
    """① stopped before the new trigger's DROP (both triggers): the rerun sends only that DROP;
    ② everything done, only the version not written: the rerun sends no DDL. Each ends with the
    old trigger as the only BEFORE UPDATE and 0031's structure."""
    _start(alembic_config, engine)
    try:
        command.downgrade(alembic_config, _DOWN_REVISION)
        at_0031 = _triggers(engine)
        if left == "complete":
            _set_version(engine, _REVISION)
        else:
            command.upgrade(alembic_config, _REVISION)
            with _interrupt_before(_DOWNGRADE_STOPS[left]):
                with pytest.raises(_Interrupted):
                    command.downgrade(alembic_config, _DOWN_REVISION)
            assert _version(engine) == _REVISION
            assert _before_updates(engine) == {_OLD, _NEW}

        with _statements() as statements:
            command.downgrade(alembic_config, _DOWN_REVISION)

        heads = [_head(s) for s in _ddl(statements)]
        expected = {"both triggers": [f"DROP TRIGGER {_NEW.upper()}"], "complete": []}
        assert heads == expected[left]
        assert _version(engine) == _DOWN_REVISION
        assert _before_updates(engine) == {_OLD}
        assert _triggers(engine) == at_0031
    finally:
        _finish(alembic_config, engine)


@needs_mysql
def test_0032_downgrade_restores_0030s_trigger(alembic_config: Config, engine: Engine) -> None:
    """Design §7 "降级恢复": down to 0031 — our table's only BEFORE UPDATE trigger is
    `trg_pricing_rules_before_update` with 0030's body, and what 0030 let through (moving a
    retired rule's end) goes through again."""
    _start(alembic_config, engine)
    seed = None
    try:
        command.downgrade(alembic_config, _DOWN_REVISION)

        assert _before_updates(engine) == {_OLD}
        old_body = _load_0030()._TRIGGERS[_OLD].split("FOR EACH ROW", 1)[1]
        assert _triggers(engine)[_OLD][3].strip() == old_body.strip()
        seed = _seed(engine)
        with engine.begin() as connection:
            retired = _published(connection, seed, 0, T0)
            connection.execute(text(_RETIRE), {"end": T0 + DAY, "id": retired})
        _execute(engine, _SET_END, {"end": T0 + 2 * DAY, "id": retired})
        with engine.connect() as connection:
            row = connection.execute(
                text(f"SELECT status, effective_to FROM {_RULES} WHERE id = :id"), {"id": retired}
            ).one()
        assert tuple(row) == ("RETIRED", T0 + 2 * DAY)
    finally:
        _finish(alembic_config, engine, seed)


# --- the shape check ------------------------------------------------------------------------------

_SHAPE_TABLE = "pricing_rule_backstop_shape_probe"
# Every column the trigger body refers to through NEW / OLD (MySQL checks them on CREATE TRIGGER).
_SHAPE_TABLE_DDL = (
    f"CREATE TABLE {_SHAPE_TABLE} (id BIGINT PRIMARY KEY, public_id CHAR(36),"
    " priority_scope VARCHAR(32), tenant_id BIGINT, project_id BIGINT, provider_id BIGINT,"
    " model_id BIGINT, strategy VARCHAR(16), markup_multiplier DECIMAL(20, 8),"
    " status VARCHAR(16), effective_from DATETIME, effective_to DATETIME, created_by BIGINT,"
    " approved_by BIGINT, created_at DATETIME, updated_at DATETIME, approved_at DATETIME)"
)

_P5_BLOCK = (
    "        IF (NOT (NEW.effective_to <=> OLD.effective_to)\n"
    "            AND OLD.effective_to IS NOT NULL\n"
    "            AND NOT (NEW.effective_to IS NULL AND NEW.status = OLD.status))\n"
    "           OR (NEW.status = 'RETIRED' AND OLD.effective_to IS NOT NULL) THEN\n"
    "            SIGNAL SQLSTATE '45000'\n"
    "                SET MESSAGE_TEXT =\n"
    "                    'a published pricing rule ends only by truncation,"
    " restore or retirement';\n"
    "        END IF;\n"
)
_EXTRA_SIGNAL = (
    "    IF NEW.id IS NULL THEN\n"
    "        SIGNAL SQLSTATE '45000'\n"
    "            SET MESSAGE_TEXT = 'a pricing rule needs an id';\n"
    "    END IF;\n"
    "END\n"
)


def _changed(statement: str, change: tuple[str, str]) -> str:
    old, new = change
    assert statement.count(old) == 1, old
    return statement.replace(old, new)


# Design §7 "形状核对" (upgrade, at 0031): the new trigger's name made by hand, changed as named.
_UPGRADE_LOOK_ALIKES: dict[str, tuple[str, str]] = {
    "① without P5": (_P5_BLOCK, ""),
    "② one more signal": ("END\n", _EXTRA_SIGNAL),
    "③ one more space": ("'pricing rule periods overlap'", "'pricing rule  periods overlap'"),
    "④ on another table": (
        f"BEFORE UPDATE ON {_RULES}\n",
        f"BEFORE UPDATE ON {_SHAPE_TABLE}\n",
    ),
}


def test_0032_look_alikes_are_what_their_names_say() -> None:
    """The hand-made triggers of the shape cases differ from the target as named; ④ differs only
    in its table."""
    target = _load_0032()._TRIGGERS[_NEW]
    for name, (old, new) in _UPGRADE_LOOK_ALIKES.items():
        made = _changed(target, (old, new))
        assert made != target, name
        if new:
            assert made.replace(new, old, 1) == target, name
        else:
            assert old not in made, name
    on_another_table = _changed(target, _UPGRADE_LOOK_ALIKES["④ on another table"])
    assert _body_lines(on_another_table) == _body_lines(target)


@needs_mysql
@pytest.mark.parametrize("shape", list(_UPGRADE_LOOK_ALIKES))
def test_0032_upgrade_refuses_a_look_alike_before_any_ddl(
    alembic_config: Config, engine: Engine, shape: str
) -> None:
    """Design §7 "形状核对" ①–④: at 0031 a trigger of the new name made by hand — the upgrade
    fails naming it, sends no DDL (information_schema the same before and after), the version
    stays 0031."""
    _start(alembic_config, engine)
    statement = _changed(_load_0032()._TRIGGERS[_NEW], _UPGRADE_LOOK_ALIKES[shape])
    on_another_table = shape == "④ on another table"
    made = False
    try:
        command.downgrade(alembic_config, _DOWN_REVISION)
        with engine.begin() as connection:
            if on_another_table:
                connection.exec_driver_sql(_SHAPE_TABLE_DDL)
            made = True
            connection.exec_driver_sql(statement)
        before = _triggers(engine)
        assert _NEW in before
        assert _OLD in before

        with _statements() as statements:
            with pytest.raises(RuntimeError) as raised:
                command.upgrade(alembic_config, _REVISION)

        message = str(raised.value)
        assert _NEW in message
        assert "No DDL was issued" in message
        assert _ddl(statements) == []
        assert _triggers(engine) == before
        assert _version(engine) == _DOWN_REVISION
    finally:
        if made:
            with engine.begin() as connection:
                if on_another_table:
                    connection.exec_driver_sql(f"DROP TABLE IF EXISTS {_SHAPE_TABLE}")
                else:
                    connection.exec_driver_sql(f"DROP TRIGGER IF EXISTS {_NEW}")
        _finish(alembic_config, engine)


@needs_mysql
def test_0032_downgrade_refuses_a_hand_made_old_trigger(
    alembic_config: Config, engine: Engine
) -> None:
    """Design §7 "形状核对" ⑤: at 0032 the new trigger is there and the old trigger's name is
    taken by a hand-made body — the downgrade fails naming it, sends no DDL, the version stays
    0032."""
    _start(alembic_config, engine)
    statement = _changed(
        _load_0030()._TRIGGERS[_OLD],
        ("'pricing rule periods overlap'", "'pricing rule periods overlap (hand-made)'"),
    )
    made = False
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql(statement)
        made = True
        before = _triggers(engine)
        assert _before_updates(engine) == {_OLD, _NEW}

        with _statements() as statements:
            with pytest.raises(RuntimeError) as raised:
                command.downgrade(alembic_config, _DOWN_REVISION)

        message = str(raised.value)
        assert _OLD in message
        assert "No DDL was issued" in message
        assert _ddl(statements) == []
        assert _triggers(engine) == before
        assert _version(engine) == _REVISION
    finally:
        if made:
            with engine.begin() as connection:
                connection.exec_driver_sql(f"DROP TRIGGER IF EXISTS {_OLD}")
        _finish(alembic_config, engine)


@needs_mysql
def test_0032_downgrade_refuses_a_hand_made_new_trigger(
    alembic_config: Config, engine: Engine
) -> None:
    """Design §7 "形状核对" ⑥: the old trigger is there and the same as 0030's, the new trigger's
    name is taken by a hand-made body — the downgrade fails naming it (it does not drop a trigger
    it cannot account for), sends no DDL, the version stays 0032."""
    _start(alembic_config, engine)
    statement = _changed(
        _load_0032()._TRIGGERS[_NEW],
        ("'pricing rule periods overlap'", "'pricing rule periods overlap (hand-made)'"),
    )
    made = False
    try:
        command.downgrade(alembic_config, _DOWN_REVISION)
        with engine.begin() as connection:
            connection.exec_driver_sql(statement)
        made = True
        _set_version(engine, _REVISION)
        before = _triggers(engine)
        assert _before_updates(engine) == {_OLD, _NEW}

        with _statements() as statements:
            with pytest.raises(RuntimeError) as raised:
                command.downgrade(alembic_config, _DOWN_REVISION)

        message = str(raised.value)
        assert _NEW in message
        assert _OLD not in message.replace(_NEW, "")
        assert "No DDL was issued" in message
        assert _ddl(statements) == []
        assert _triggers(engine) == before
        assert _version(engine) == _REVISION
    finally:
        if made:
            with engine.begin() as connection:
                connection.exec_driver_sql(f"DROP TRIGGER IF EXISTS {_NEW}")
        _finish(alembic_config, engine)

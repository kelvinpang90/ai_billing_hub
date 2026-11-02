"""0031_provider_price_backstop（AIH-TASK-145，设计闸门 #535 v5）.

Design §2 "数据库（迁移 0031）", §3 INV-6, §4, §5 (the migration rows), §7 (the migration rows),
§8.

The first cases need no database: the revision chain; the migration's CHECK is the model's; the
new trigger's body differs from 0013's in exactly the five places of design §2 (line by line) and
holds no sentinel date; the frozen copy of 0013's body is 0013's; the `--sql` upgrade and downgrade
emit the design order without reading; the precheck refuses only binlog without the switch, before
any DDL, both ways; the clause pattern matches the three canonical spellings and nothing else; the
CHECK comparison refuses a constraint that is `NOT ENFORCED`, on another table, or not alone.

On a real MySQL (`BILLING_TEST_DATABASE_URL`; skipped when unset, never skipped in CI): the
structure at head, the offline upgrade and downgrade, existing rows untouched, the interrupted
upgrade (three places) and downgrade (two places), the intermediate states no weaker than 0013
(two places), the restoring downgrade, the data check, the shape check (nine objects), and the
downgrade with rows of every state. What the new trigger and CHECK refuse or let through, written
directly, is in test_provider_prices_service.py.

What is observed is 0031 alone: every case goes back to head first and then down to 0031 (outside
the part under observation), and a downgrade is only observed from 0031 to 0030 — statements are
never counted from head, so later revisions do not affect this module. Interruptions are made by
raising from a `before_cursor_execute` listener just before the statement the step would send
next. Cleaning follows design §7: every case ends with `TRUNCATE` of the two price tables (foreign
key checks off; no trigger is dropped or rebuilt), back at head, and checks that the two tables are
empty, the trigger list is head's and the CHECK is there.
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
from app.core.database import create_session_factory
from app.models import auth as _auth_models  # noqa: F401 - 让 Base.metadata 装上这些表
from app.models.ai_catalog import AiModel, AiModelAlias, AiProvider
from app.models.auth import User, UserRole, UserStatus
from app.models.provider_prices import RETIRED_END_CHECK, ProviderPriceVersion
from app.schemas.provider_prices import ComponentInput
from app.services import ai_catalog, provider_prices
from app.services.auth import RequestContext, utc_now

TEST_DATABASE_URL = os.environ.get("BILLING_TEST_DATABASE_URL", "")

needs_mysql = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="BILLING_TEST_DATABASE_URL is not set; migration tests need a real MySQL",
)

_REVISION = "0031_provider_price_backstop"
_DOWN_REVISION = "0030_pricing_rule_project_scope"
_MIGRATION = pathlib.Path("alembic/versions/20261031_0031_provider_price_backstop.py")
_MIGRATION_0013 = pathlib.Path("alembic/versions/20260929_0013_provider_prices.py")

_VERSIONS = "provider_price_versions"
_COMPONENTS = "provider_price_components"
_PRICE_TABLES = (_VERSIONS, _COMPONENTS)
_CHECK_NAME = "ck_provider_price_versions_retired_end"
_OLD = "trg_provider_price_versions_before_update"
_NEW = "trg_provider_price_versions_before_update_v2"

# Trigger → (table, timing, event) on the two price tables at head.
_HEAD_TRIGGERS = {
    "trg_provider_price_versions_before_insert": (_VERSIONS, "BEFORE", "INSERT"),
    _NEW: (_VERSIONS, "BEFORE", "UPDATE"),
    "trg_provider_price_versions_before_delete": (_VERSIONS, "BEFORE", "DELETE"),
    "trg_provider_price_components_before_insert": (_COMPONENTS, "BEFORE", "INSERT"),
    "trg_provider_price_components_before_update": (_COMPONENTS, "BEFORE", "UPDATE"),
    "trg_provider_price_components_before_delete": (_COMPONENTS, "BEFORE", "DELETE"),
}

# MySQL error numbers: SIGNAL 1644, CHECK 3819.
_ER_SIGNAL_EXCEPTION = 1644
_ER_CHECK_CONSTRAINT_VIOLATED = 3819

_PRECHECK = "SELECT @@log_bin, @@log_bin_trust_function_creators"
_ALEMBIC_VERSION = text("SELECT version_num FROM alembic_version")
_SET_VERSION = text("UPDATE alembic_version SET version_num = :version")

T0 = dt.datetime(2026, 10, 31, 8, 0, 0)
SECOND = dt.timedelta(seconds=1)
DAY = dt.timedelta(days=1)
_TEST_EMAIL_DOMAIN = "@provider-price-backstop-migration-test.example.com"
_CONTEXT = RequestContext(ip_address="203.0.113.31", user_agent="provider-price-backstop-test")

# What MySQL 8.4 keeps as CHECK_CLAUSE for the target condition, per connection charset
# (design §2 upgrade step 2), and for the three look-alike conditions of design §7 (①②③).
_CANONICAL = r"((`status` <> _{charset}\'RETIRED\') or (`effective_to` is not null))"
_STORED_WEAKER = (
    r"((`status` <> _utf8mb4\'RETIRED\') or (`effective_to` is not null)"
    r" or (`source_type` = _utf8mb4\'MANUAL\'))"
)
_STORED_SWAPPED = r"((`effective_to` is not null) or (`status` <> _utf8mb4\'RETIRED\'))"
_STORED_ESCAPED = r"((`status` <> _utf8mb4\'\\rETIRED\') or (`effective_to` is not null))"
# The conditions written by hand for them (③ holds one more escape: `\r` inside the literal).
_CONDITION_WEAKER = "status <> 'RETIRED' OR effective_to IS NOT NULL OR source_type = 'MANUAL'"
_CONDITION_SWAPPED = "effective_to IS NOT NULL OR status <> 'RETIRED'"
_CONDITION_ESCAPED = r"status <> '\rETIRED' OR effective_to IS NOT NULL"


def _load(path: pathlib.Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_0031() -> ModuleType:
    return _load(_MIGRATION, "migration_0031")


def _load_0013() -> ModuleType:
    return _load(_MIGRATION_0013, "migration_0013")


def _spaces(statement: str) -> str:
    return " ".join(statement.split())


def _normalised(statement: str) -> str:
    return re.sub(r"\s+", " ", statement.replace("`", "")).strip().upper()


def _head(statement: str) -> str:
    """The first three words: `CREATE TRIGGER <name>`, `ALTER TABLE PROVIDER_PRICE_VERSIONS`, …"""
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


def test_0031_follows_0030() -> None:
    migration = _load_0031()

    assert migration.revision == _REVISION
    assert migration.down_revision == _DOWN_REVISION
    # `alembic_version.version_num` is VARCHAR(32).
    assert len(migration.revision) <= 32


def test_0031_check_is_the_models() -> None:
    migration = _load_0031()
    checks = {
        str(constraint.name): _spaces(str(constraint.sqltext))
        for constraint in ProviderPriceVersion.__table__.constraints
        if constraint.name == _CHECK_NAME
    }

    assert migration._RETIRED_END_CHECK_NAME == _CHECK_NAME
    assert _spaces(migration._RETIRED_END_CHECK) == _spaces(RETIRED_END_CHECK)
    assert checks == {_CHECK_NAME: _spaces(RETIRED_END_CHECK)}
    # The same writing as the FX rate, pricing rule and tax policy tables.
    assert _spaces(RETIRED_END_CHECK) == "status <> 'RETIRED' OR effective_to IS NOT NULL"
    # Design §2 step 4: one ALTER, ENFORCED written out.
    assert _spaces(migration._ADD_CHECK) == (
        f"ALTER TABLE {_VERSIONS} ADD CONSTRAINT {_CHECK_NAME}"
        f" CHECK ({_spaces(RETIRED_END_CHECK)}) ENFORCED"
    )
    assert _spaces(migration._DROP_CHECK) == f"ALTER TABLE {_VERSIONS} DROP CHECK {_CHECK_NAME}"


def test_0031_new_trigger_differs_from_0013_in_exactly_the_five_places() -> None:
    """Design §2 "新触发器": applying the five changes, line by line, to 0013's body gives the new
    body; every other line is 0013's. No sentinel date is left."""
    migration = _load_0031()
    old_statement = _load_0013()._TRIGGERS[_OLD]
    new_statement = migration._TRIGGERS[_NEW]

    lines = _body_lines(old_statement)
    # ① RETIRED and DISCARDED are both final.
    lines = _replace_block(
        lines,
        [
            "IF OLD.status = 'DISCARDED' THEN",
            "SIGNAL SQLSTATE '45000'",
            "SET MESSAGE_TEXT = 'a discarded provider price version is final';",
        ],
        [
            "IF OLD.status IN ('RETIRED', 'DISCARDED') THEN",
            "SIGNAL SQLSTATE '45000'",
            "SET MESSAGE_TEXT = 'a retired or discarded provider price version is final';",
        ],
    )
    # ② Right after "a draft only becomes …": a publish has no end.
    draft_block = [
        "SET MESSAGE_TEXT = 'a draft provider price version is published or discarded';",
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
            "SET MESSAGE_TEXT = 'a provider price version is published without an end';",
            "END IF;",
        ],
    )
    # ③ The immutability block only for PUBLISHED.
    lines = _replace_block(
        lines,
        ["IF OLD.status IN ('PUBLISHED', 'RETIRED') THEN"],
        ["IF OLD.status = 'PUBLISHED' THEN"],
    )
    # ④ Intersection by NULL, term by term.
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
    # ⑤ Inside the immutability block, after "can only be retired": the end changes three ways.
    retire_block = [
        "SET MESSAGE_TEXT = 'a published provider price version can only be retired';",
        "END IF;",
    ]
    lines = _replace_block(
        lines,
        retire_block,
        [
            *retire_block,
            "IF (NOT (NEW.effective_to <=> OLD.effective_to)",
            "AND NOT (OLD.effective_to IS NULL AND NEW.effective_to IS NOT NULL)",
            "AND NOT (OLD.effective_to IS NOT NULL AND NEW.effective_to IS NULL",
            "AND NEW.status <=> OLD.status))",
            "OR (NOT (NEW.status <=> OLD.status) AND OLD.effective_to IS NOT NULL) THEN",
            "SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT =",
            "'a published provider price version ends only by truncation, restore or retirement';",
            "END IF;",
        ],
    )

    assert lines == _body_lines(new_statement)
    for sentinel in ("'1000-01-01'", "'9999-12-31'"):
        assert sentinel in old_statement
        assert sentinel not in new_statement
    # The header: the new name, the same timing and table, no FOLLOWS / PRECEDES.
    assert _spaces(new_statement.split("FOR EACH ROW", 1)[0]) == (
        f"CREATE TRIGGER {_NEW} BEFORE UPDATE ON {_VERSIONS}"
    )
    assert "FOLLOWS" not in new_statement
    assert "PRECEDES" not in new_statement
    assert set(migration._TRIGGERS) == {_NEW}


def test_0031_frozen_copy_is_0013s_word_for_word() -> None:
    migration = _load_0031()

    assert set(migration._TRIGGERS_0013) == {_OLD}
    assert migration._TRIGGERS_0013[_OLD] == _load_0013()._TRIGGERS[_OLD]


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


def test_0031_offline_upgrade_and_downgrade_emit_the_design_order(monkeypatch) -> None:
    migration = _load_0031()
    up = _OfflineOp()
    monkeypatch.setattr(migration, "op", up)
    migration.upgrade()
    down = _OfflineOp()
    monkeypatch.setattr(migration, "op", down)
    migration.downgrade()

    assert [_spaces(s) for s in up.statements] == [
        _spaces(migration._ADD_CHECK),
        _spaces(migration._TRIGGERS[_NEW]),
        f"DROP TRIGGER IF EXISTS {_OLD}",
    ]
    assert [_spaces(s) for s in down.statements] == [
        _spaces(migration._TRIGGERS_0013[_OLD]),
        f"DROP TRIGGER IF EXISTS {_NEW}",
        _spaces(migration._DROP_CHECK),
    ]


class _Bind:
    """Answers the migration's four queries from fixed rows."""

    def __init__(
        self,
        migration: ModuleType,
        *,
        log_bin: int = 0,
        trusted: int = 0,
        checks: list[tuple[str, str, str, str | None]] | None = None,
        triggers: list[tuple[str, str, str, str, str]] | None = None,
        retired: list[int] | None = None,
    ) -> None:
        self.answers: dict[str, Any] = {
            migration._PREREQUISITE_QUERY: SimpleNamespace(one=lambda: (log_bin, trusted)),
            migration._CHECK_QUERY: SimpleNamespace(all=lambda: list(checks or [])),
            migration._TRIGGERS_QUERY: SimpleNamespace(all=lambda: list(triggers or [])),
            migration._RETIRED_WITHOUT_END_QUERY: SimpleNamespace(
                scalars=lambda: list(retired or [])
            ),
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
    return (name, _VERSIONS, "BEFORE", "UPDATE", statement.split("FOR EACH ROW", 1)[1])


def _at_0030(migration: ModuleType, log_bin: int, trusted: int) -> _Bind:
    old = _trigger_row(_OLD, migration._TRIGGERS_0013[_OLD])
    return _Bind(migration, log_bin=log_bin, trusted=trusted, triggers=[old])


def _at_0031(migration: ModuleType, log_bin: int, trusted: int) -> _Bind:
    new = _trigger_row(_NEW, migration._TRIGGERS[_NEW])
    check = (_VERSIONS, "CHECK", "YES", _CANONICAL.format(charset="utf8mb4"))
    return _Bind(migration, log_bin=log_bin, trusted=trusted, checks=[check], triggers=[new])


@pytest.mark.parametrize("direction", ["upgrade", "downgrade"])
@pytest.mark.parametrize(
    ("log_bin", "trusted", "refused"),
    [(1, 0, True), (1, 1, False), (0, 0, False), (0, 1, False)],
)
def test_0031_precheck_refuses_only_binlog_without_the_switch_before_any_ddl(
    monkeypatch, direction: str, log_bin: int, trusted: int, refused: bool
) -> None:
    """Design §7 "预检": query, decision and message as 0013's. Refused → no statement at all;
    otherwise the same run reaches its DDL (so the empty list is not for want of getting there)."""
    migration = _load_0031()
    earlier = _load_0013()
    assert migration._PREREQUISITE_QUERY == earlier._PREREQUISITE_QUERY == _PRECHECK
    assert migration._PREREQUISITE_ERROR == earlier._PREREQUISITE_ERROR
    at = _at_0030 if direction == "upgrade" else _at_0031
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
        assert fake.statements == [
            migration._ADD_CHECK,
            migration._TRIGGERS[_NEW],
            f"DROP TRIGGER {_OLD}",
        ]
    else:
        assert fake.statements == [
            migration._TRIGGERS_0013[_OLD],
            f"DROP TRIGGER {_NEW}",
            migration._DROP_CHECK,
        ]


@pytest.mark.parametrize("direction", ["upgrade", "downgrade"])
def test_0031_a_complete_run_sends_nothing(monkeypatch, direction: str) -> None:
    """Already done: no statement, not even the precheck (binlog on and the switch off here)."""
    migration = _load_0031()
    at = _at_0031 if direction == "upgrade" else _at_0030
    fake = _OnlineOp(at(migration, log_bin=1, trusted=0))
    monkeypatch.setattr(migration, "op", fake)

    (migration.upgrade if direction == "upgrade" else migration.downgrade)()

    assert fake.statements == []


@pytest.mark.parametrize("charset", ["utf8mb4", "utf8mb3", "latin1"])
def test_0031_pattern_matches_the_canonical_clause(charset: str) -> None:
    migration = _load_0031()

    clause = _CANONICAL.format(charset=charset)
    assert re.fullmatch(migration._RETIRED_END_CLAUSE_PATTERN, clause) is not None


@pytest.mark.parametrize(
    "clause",
    [
        _CANONICAL.format(charset="binary"),
        _STORED_WEAKER,
        _STORED_SWAPPED,
        _STORED_ESCAPED,
        " " + _CANONICAL.format(charset="utf8mb4"),
        _CANONICAL.format(charset="utf8mb4") + " ",
    ],
    ids=["binary", "weaker", "swapped", "escaped", "space-before", "space-after"],
)
def test_0031_pattern_refuses_anything_else(clause: str) -> None:
    migration = _load_0031()

    assert re.fullmatch(migration._RETIRED_END_CLAUSE_PATTERN, clause) is None


@pytest.mark.parametrize(
    ("rows", "matches"),
    [
        ([(_VERSIONS, "CHECK", "YES", _CANONICAL.format(charset="utf8mb4"))], True),
        ([(_VERSIONS, "CHECK", "NO", _CANONICAL.format(charset="utf8mb4"))], False),
        ([("another_table", "CHECK", "YES", _CANONICAL.format(charset="utf8mb4"))], False),
        (
            [
                (_VERSIONS, "CHECK", "YES", _CANONICAL.format(charset="utf8mb4")),
                ("another_table", "CHECK", "YES", _CANONICAL.format(charset="utf8mb4")),
            ],
            False,
        ),
        ([(_VERSIONS, "CHECK", "YES", _STORED_WEAKER)], False),
        ([], False),
    ],
    ids=["target", "not-enforced", "another-table", "two", "weaker", "none"],
)
def test_0031_check_comparison(rows: list[tuple[str, str, str, str]], matches: bool) -> None:
    assert _load_0031()._check_matches(rows) is matches


def test_0031_trigger_comparison_drops_only_the_blanks_at_either_end() -> None:
    migration = _load_0031()
    statement = migration._TRIGGERS[_NEW]
    body = statement.split("FOR EACH ROW", 1)[1]

    assert migration._trigger_matches((_VERSIONS, "BEFORE", "UPDATE", body), statement)
    assert migration._trigger_matches((_VERSIONS, "BEFORE", "UPDATE", body.strip()), statement)
    one_more_space = body.replace(
        "'provider price periods overlap'", "'provider price  periods overlap'"
    )
    for row in [
        (_VERSIONS, "BEFORE", "UPDATE", one_more_space),
        (_VERSIONS, "AFTER", "UPDATE", body),
        (_VERSIONS, "BEFORE", "INSERT", body),
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
    """Head, complete: at 0031 the version goes back to 0030 first, so 0031 runs again — nothing
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
            connection.exec_driver_sql(f"TRUNCATE TABLE {_VERSIONS}")
        finally:
            connection.exec_driver_sql("SET FOREIGN_KEY_CHECKS = 1")


def _start(alembic_config: Config, engine: Engine) -> None:
    """Head, then down to 0031: what follows is 0031's, whatever later revisions do."""
    _to_head(alembic_config, engine)
    command.downgrade(alembic_config, _REVISION)
    assert _version(engine) == _REVISION
    _clean(engine)


def _structure(engine: Engine) -> dict[str, Any]:
    """From information_schema, word for word: the CHECKs on the price tables and any constraint
    named like ours anywhere in the database (table, name, type, enforced, clause), and the
    triggers on the price tables (table, timing, event, body). Same-timing order is left out."""
    checks = text(
        "SELECT t.TABLE_NAME, t.CONSTRAINT_NAME, t.CONSTRAINT_TYPE, t.ENFORCED, c.CHECK_CLAUSE"
        " FROM information_schema.TABLE_CONSTRAINTS t"
        " LEFT JOIN information_schema.CHECK_CONSTRAINTS c"
        " ON c.CONSTRAINT_SCHEMA = t.CONSTRAINT_SCHEMA AND c.CONSTRAINT_NAME = t.CONSTRAINT_NAME"
        " WHERE t.CONSTRAINT_SCHEMA = DATABASE()"
        " AND ((t.CONSTRAINT_TYPE = 'CHECK'"
        f" AND t.TABLE_NAME IN ('{_VERSIONS}', '{_COMPONENTS}'))"
        f" OR t.CONSTRAINT_NAME = '{_CHECK_NAME}')"
        " ORDER BY t.TABLE_NAME, t.CONSTRAINT_NAME"
    )
    triggers = text(
        "SELECT TRIGGER_NAME, EVENT_OBJECT_TABLE, ACTION_TIMING, EVENT_MANIPULATION,"
        " ACTION_STATEMENT FROM information_schema.TRIGGERS WHERE TRIGGER_SCHEMA = DATABASE()"
        f" AND EVENT_OBJECT_TABLE IN ('{_VERSIONS}', '{_COMPONENTS}')"
    )
    with engine.connect() as connection:
        return {
            "checks": [tuple(row) for row in connection.execute(checks).all()],
            "triggers": {
                str(name): (str(table), str(timing), str(kind), str(body))
                for name, table, timing, kind, body in connection.execute(triggers).all()
            },
        }


def _trigger_list(engine: Engine) -> dict[str, tuple[str, str, str]]:
    return {name: row[:3] for name, row in _structure(engine)["triggers"].items()}


def _has_check(engine: Engine) -> bool:
    return any(
        row[0] == _VERSIONS and row[1] == _CHECK_NAME for row in _structure(engine)["checks"]
    )


def _rows(engine: Engine) -> dict[str, list[dict[str, object]]]:
    with engine.connect() as connection:
        return {
            table: [
                dict(row)
                for row in connection.execute(text(f"SELECT * FROM {table} ORDER BY id")).mappings()
            ]
            for table in _PRICE_TABLES
        }


def _finish(alembic_config: Config, engine: Engine, seed: _Seed | None = None) -> None:
    """Design §7 cleaning: TRUNCATE, back at head, then the seeded parents; the database ends at
    head with both price tables empty, head's trigger list and the CHECK."""
    _clean(engine)
    _to_head(alembic_config, engine)
    if seed is not None:
        seed.remove(engine)
    assert _version(engine) == _head_revision(alembic_config)
    assert _rows(engine) == {_VERSIONS: [], _COMPONENTS: []}
    assert _trigger_list(engine) == _HEAD_TRIGGERS
    assert _has_check(engine)


def _refusal(engine: Engine, statement: str, params: dict[str, object]) -> tuple[int, str]:
    """Runs one write in its own transaction; returns the MySQL error number and message."""
    with pytest.raises(DBAPIError) as raised:
        with engine.begin() as connection:
            connection.execute(text(statement), params)
    code, message = raised.value.orig.args[:2]
    return int(code), str(message)


# --- direct rows (SQL text, as 0013's triggers let them in) -------------------------------------

_INSERT_VERSION = (
    f"INSERT INTO {_VERSIONS} (public_id, provider_id, model_id, source_currency, source_type,"
    " source_reference, status, effective_from, effective_to, created_by, approved_by,"
    " created_at, updated_at, approved_at) VALUES (:public_id, :provider_id, :model_id, 'USD',"
    " 'MANUAL', 'Fictional price sheet', 'DRAFT', NULL, NULL, :user, NULL, :now, :now, NULL)"
)
_INSERT_COMPONENT = (
    f"INSERT INTO {_COMPONENTS} (provider_price_version_id, usage_meter_component_id,"
    " unit_quantity, rate_amount, created_at)"
    " SELECT :id, id, 1, 1, :now FROM usage_meter_components"
    " WHERE component_code = 'EMBEDDING_TOKEN'"
)
_PUBLISH = (
    f"UPDATE {_VERSIONS} SET status = :status, effective_from = :start, effective_to = :end,"
    " approved_by = :user, approved_at = :now WHERE id = :id"
)


@dataclass(frozen=True)
class _Seed:
    """A user, a provider and its models (Core inserts: none of these tables changes in 0031)."""

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


def _seed(engine: Engine, models: int = 3) -> _Seed:
    marker = uuid.uuid4().hex[:12]
    now = dt.datetime(2026, 10, 31, 8, 0, 0)
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
                code=f"p145-{marker}",
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
                    code=f"m145-{marker}-{index}",
                    display_name="Fictional model",
                    created_at=now,
                    updated_at=now,
                )
            )
            for index in range(models)
        )
    return _Seed(user_id=user_id, provider_id=provider_id, model_ids=model_ids)


def _draft(connection: Connection, seed: _Seed, model: int = 0) -> int:
    """A complete draft (one EMBEDDING_TOKEN component) on `seed.model_ids[model]`."""
    public_id = str(uuid.uuid4())
    connection.execute(
        text(_INSERT_VERSION),
        {
            "public_id": public_id,
            "provider_id": seed.provider_id,
            "model_id": seed.model_ids[model],
            "user": seed.user_id,
            "now": T0,
        },
    )
    found = text(f"SELECT id FROM {_VERSIONS} WHERE public_id = :public_id")
    version_id = int(connection.execute(found, {"public_id": public_id}).scalar_one())
    connection.execute(text(_INSERT_COMPONENT), {"id": version_id, "now": T0})
    return version_id


def _publishing(
    seed: _Seed,
    version_id: int,
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
        "id": version_id,
    }


def _published(connection: Connection, seed: _Seed, model: int, start: dt.datetime) -> int:
    """A draft published directly as `[start, NULL)`."""
    version_id = _draft(connection, seed, model)
    connection.execute(text(_PUBLISH), _publishing(seed, version_id, (start, None)))
    return version_id


# --- rows through the services --------------------------------------------------------------------


class _Clock:
    def __init__(self, moment: dt.datetime) -> None:
        self.now = moment

    def __call__(self) -> dt.datetime:
        return self.now


def _components() -> list[ComponentInput]:
    """The one component of EMBEDDING: a complete set (a clearly fictional rate)."""
    values = {"component_code": "EMBEDDING_TOKEN", "unit_quantity": "1000000"}
    return [ComponentInput.model_validate({**values, "rate_amount": "1.11111111"})]


def _service_timeline(engine: Engine) -> _Seed:
    """Through the services, one row of every state (design §7 "不改已有行"): an open published
    and a truncated published version (model 0), a retired version with an end (model 1), the
    empty period a withdrawn reservation leaves (model 2), a draft and a discarded draft, both
    with components."""
    factory = create_session_factory(engine)
    clock = _Clock(utc_now())
    now = utc_now()
    with factory() as session:
        admin = User(
            email=f"{uuid.uuid4().hex[:12]}{_TEST_EMAIL_DOMAIN}",
            password_hash="not-a-real-hash",
            role=UserRole.ADMIN,
            status=UserStatus.ACTIVE,
            created_at=now,
            updated_at=now,
        )
        session.add(admin)
        session.commit()
    common = {"actor": admin, "context": _CONTEXT, "clock": clock}
    marker = uuid.uuid4().hex[:12]
    provider = ai_catalog.create_provider(
        factory, code=f"p145-{marker}", display_name="Fictional provider", **common
    )
    models = [
        ai_catalog.create_model(
            factory,
            provider_id=provider.id,
            code=f"m145-{marker}-{index}",
            display_name="Fictional model",
            **common,
        )
        for index in range(3)
    ]

    def draft(model: int) -> str:
        return provider_prices.create_draft(
            factory,
            provider_id=provider.id,
            model_id=models[model].id,
            source_currency="USD",
            source_reference="Fictional price sheet",
            components=_components(),
            **common,
        ).id

    def publish(version_id: str, effective_from: dt.datetime | None = None) -> str:
        return provider_prices.publish(
            factory, price_version_id=version_id, effective_from=effective_from, **common
        ).id

    def retire(version_id: str) -> None:
        provider_prices.retire(
            factory, price_version_id=version_id, reason="Fictional retirement", **common
        )

    publish(draft(0))
    clock.now += 3 * SECOND
    publish(draft(0))
    retired = publish(draft(1))
    clock.now += 3 * SECOND
    retire(retired)
    reserved = publish(draft(2), clock.now.replace(microsecond=0) + DAY)
    retire(reserved)
    draft(0)
    provider_prices.discard_draft(factory, price_version_id=draft(1), **common)

    with engine.connect() as connection:
        provider_id = int(
            connection.execute(
                text("SELECT id FROM ai_providers WHERE public_id = :id"), {"id": provider.id}
            ).scalar_one()
        )
        model_ids = tuple(
            int(i)
            for i in connection.execute(
                text("SELECT id FROM ai_models WHERE provider_id = :id ORDER BY id"),
                {"id": provider_id},
            ).scalars()
        )
    return _Seed(user_id=int(admin.id), provider_id=provider_id, model_ids=model_ids)


def _remove_service_seed(engine: Engine, seed: _Seed) -> None:
    """Audit rows refer to the admin and refuse DELETE (0010): TRUNCATE them, as
    test_provider_prices_service.py does."""
    with engine.begin() as connection:
        connection.exec_driver_sql("TRUNCATE TABLE audit_logs")
    seed.remove(engine)


def _assert_rows_of_every_state(rows: dict[str, list[dict[str, object]]]) -> None:
    states = sorted(
        (
            str(row["status"]),
            row["effective_to"] is None,
            row["effective_from"] is not None and row["effective_from"] == row["effective_to"],
        )
        for row in rows[_VERSIONS]
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
    assert len(rows[_COMPONENTS]) == 6


# --- the cases ------------------------------------------------------------------------------------


@needs_mysql
def test_0031_structure_at_head(alembic_config: Config, engine: Engine) -> None:
    """Design §7 "迁移结构": the CHECK is there, on our table, enforced, its clause the
    canonical text; our table's triggers are exactly BEFORE INSERT, `…_v2` and BEFORE DELETE;
    the new trigger's ACTION_STATEMENT, without the blanks at either end, is the frozen body —
    what the comparison sees is what MySQL keeps."""
    _start(alembic_config, engine)
    migration = _load_0031()
    try:
        structure = _structure(engine)
        ours = [row for row in structure["checks"] if row[1] == _CHECK_NAME]
        assert len(ours) == 1
        table, _, kind, enforced, clause = ours[0]
        assert (table, kind, enforced) == (_VERSIONS, "CHECK", "YES")
        assert "'RETIRED'" in str(clause).replace("\\", "")
        assert "effective_to" in str(clause)
        assert re.fullmatch(migration._RETIRED_END_CLAUSE_PATTERN, str(clause)) is not None
        assert _trigger_list(engine) == _HEAD_TRIGGERS
        assert _OLD not in structure["triggers"]
        body = structure["triggers"][_NEW][3]
        assert body.strip() == migration._body(migration._TRIGGERS[_NEW])
        with engine.connect() as connection:
            assert migration._check_matches(migration._check_rows(connection))
            new = migration._triggers(connection)[_NEW]
            assert migration._trigger_matches(new, migration._TRIGGERS[_NEW])
    finally:
        _finish(alembic_config, engine)


@needs_mysql
def test_0031_offline_upgrade_and_downgrade(alembic_config: Config) -> None:
    """Design §7 "离线": the `--sql` upgrade emits the ALTER, the new trigger's CREATE and
    the old one's DROP IF EXISTS, in this order, reading nothing; the `--sql` downgrade emits
    the old trigger's CREATE, the new one's DROP IF EXISTS and the DROP CHECK."""
    up_buffer = io.StringIO()
    command.upgrade(
        Config("alembic.ini", output_buffer=up_buffer), f"{_DOWN_REVISION}:{_REVISION}", sql=True
    )
    emitted = _normalised(up_buffer.getvalue())
    order = [
        f"ALTER TABLE {_VERSIONS} ADD CONSTRAINT {_CHECK_NAME} CHECK",
        f"CREATE TRIGGER {_NEW} BEFORE UPDATE ON {_VERSIONS}",
        f"DROP TRIGGER IF EXISTS {_OLD};",
    ]
    positions = [emitted.index(_normalised(part)) for part in order]
    assert positions == sorted(positions)
    assert "@@LOG_BIN" not in emitted
    assert "INFORMATION_SCHEMA" not in emitted

    down_buffer = io.StringIO()
    command.downgrade(
        Config("alembic.ini", output_buffer=down_buffer), f"{_REVISION}:{_DOWN_REVISION}", sql=True
    )
    emitted = _normalised(down_buffer.getvalue())
    order = [
        f"CREATE TRIGGER {_OLD} BEFORE UPDATE ON {_VERSIONS}",
        f"DROP TRIGGER IF EXISTS {_NEW};",
        f"ALTER TABLE {_VERSIONS} DROP CHECK {_CHECK_NAME}",
    ]
    positions = [emitted.index(_normalised(part)) for part in order]
    assert positions == sorted(positions)
    assert "@@LOG_BIN" not in emitted
    assert "INFORMATION_SCHEMA" not in emitted


@needs_mysql
def test_0031_upgrade_leaves_existing_rows_as_they_were(
    alembic_config: Config, engine: Engine
) -> None:
    """Design §7 "不改已有行" / INV-6: at 0030, rows of every state through the services;
    after the upgrade both price tables are the same, row by row and column by column."""
    _start(alembic_config, engine)
    seed = None
    try:
        command.downgrade(alembic_config, _DOWN_REVISION)
        seed = _service_timeline(engine)
        before = _rows(engine)
        _assert_rows_of_every_state(before)

        command.upgrade(alembic_config, _REVISION)

        assert _rows(engine) == before
    finally:
        _finish(alembic_config, engine)
        if seed is not None:
            _remove_service_seed(engine, seed)


@needs_mysql
def test_0031_downgrade_needs_no_data_precondition(alembic_config: Config, engine: Engine) -> None:
    """Design §7 "降级不需要数据前提": with rows of every state the downgrade goes through
    and the rows are as they were."""
    _start(alembic_config, engine)
    seed = None
    try:
        seed = _service_timeline(engine)
        before = _rows(engine)
        _assert_rows_of_every_state(before)

        command.downgrade(alembic_config, _DOWN_REVISION)

        assert _version(engine) == _DOWN_REVISION
        assert _rows(engine) == before
    finally:
        _finish(alembic_config, engine)
        if seed is not None:
            _remove_service_seed(engine, seed)


# Design §7 "升级中断后重跑": where the upgrade stopped → the statement it stopped before.
_UPGRADE_STOPS = {
    "check added": f"CREATE TRIGGER {_NEW.upper()}",
    "both triggers": f"DROP TRIGGER {_OLD.upper()}",
}


@needs_mysql
@pytest.mark.parametrize("left", [*_UPGRADE_STOPS, "complete"])
def test_0031_upgrade_completes_an_interrupted_upgrade(
    alembic_config: Config, engine: Engine, left: str
) -> None:
    """① stopped before the new trigger's CREATE (the CHECK is there): the rerun sends the CREATE
    and the old trigger's DROP; ② stopped before the DROP (both triggers): the rerun sends only the
    DROP; ③ everything done, only the version not written: the rerun sends no DDL. Each ends with
    the structure of one uninterrupted upgrade."""
    _start(alembic_config, engine)
    try:
        reference = _structure(engine)
        if left == "complete":
            _set_version(engine, _DOWN_REVISION)
        else:
            command.downgrade(alembic_config, _DOWN_REVISION)
            with _interrupt_before(_UPGRADE_STOPS[left]):
                with pytest.raises(_Interrupted):
                    command.upgrade(alembic_config, _REVISION)
            assert _version(engine) == _DOWN_REVISION
            assert _has_check(engine)
            assert (_NEW in _trigger_list(engine)) == (left == "both triggers")
            assert _OLD in _trigger_list(engine)

        with _statements() as statements:
            command.upgrade(alembic_config, _REVISION)

        heads = [_head(s) for s in _ddl(statements)]
        expected = {
            "check added": [f"CREATE TRIGGER {_NEW.upper()}", f"DROP TRIGGER {_OLD.upper()}"],
            "both triggers": [f"DROP TRIGGER {_OLD.upper()}"],
            "complete": [],
        }
        assert heads == expected[left]
        assert _version(engine) == _REVISION
        assert _structure(engine) == reference
    finally:
        _finish(alembic_config, engine)


@needs_mysql
@pytest.mark.parametrize("left", list(_UPGRADE_STOPS))
def test_0031_intermediate_states_are_no_weaker_than_0013(
    alembic_config: Config, engine: Engine, left: str
) -> None:
    """Design §7 "中间状态不弱于 0013": at both places where the upgrade can stop, what
    0013 already refused is refused (a change to a published version's currency, retired back
    to published, a draft straight to RETIRED, an overlapping period); of what this design
    newly refuses, a publish with an end is refused where both triggers are there, and retiring
    without an end is refused in both places (the CHECK). The rows do not change."""
    _start(alembic_config, engine)
    seed = None
    try:
        command.downgrade(alembic_config, _DOWN_REVISION)
        seed = _seed(engine)
        with engine.begin() as connection:
            published = _published(connection, seed, 0, T0)
            retired = _published(connection, seed, 1, T0)
            retire = (
                f"UPDATE {_VERSIONS} SET status = 'RETIRED', effective_to = :end WHERE id = :id"
            )
            connection.execute(text(retire), {"end": T0 + DAY, "id": retired})
            draft = _draft(connection, seed, 0)
            lone_draft = _draft(connection, seed, 2)
        with _interrupt_before(_UPGRADE_STOPS[left]):
            with pytest.raises(_Interrupted):
                command.upgrade(alembic_config, _REVISION)
        before = _rows(engine)

        writes_0013 = {
            "currency": (
                f"UPDATE {_VERSIONS} SET source_currency = 'EUR' WHERE id = :id",
                {"id": published},
            ),
            "retired back to published": (
                f"UPDATE {_VERSIONS} SET status = 'PUBLISHED' WHERE id = :id",
                {"id": retired},
            ),
            "draft to retired": (_PUBLISH, _publishing(seed, draft, (None, T0), "RETIRED")),
            "overlap": (_PUBLISH, _publishing(seed, draft, (T0 + SECOND, None))),
        }
        for name, (statement, params) in writes_0013.items():
            assert _refusal(engine, statement, params)[0] == _ER_SIGNAL_EXCEPTION, name
        if left == "both triggers":
            params = _publishing(seed, lone_draft, (T0, T0 + DAY))
            assert _refusal(engine, _PUBLISH, params)[0] == _ER_SIGNAL_EXCEPTION
        without_end = f"UPDATE {_VERSIONS} SET status = 'RETIRED' WHERE id = :id"
        code, message = _refusal(engine, without_end, {"id": published})
        assert code == _ER_CHECK_CONSTRAINT_VIOLATED
        assert _CHECK_NAME in message
        assert _rows(engine) == before

        command.upgrade(alembic_config, _REVISION)
        assert _version(engine) == _REVISION
    finally:
        _finish(alembic_config, engine, seed)


# Design §7 "降级中断后重跑".
_DOWNGRADE_STOPS = {
    "both triggers": f"DROP TRIGGER {_NEW.upper()}",
    "check left": f"ALTER TABLE {_VERSIONS.upper()}",
}


@needs_mysql
@pytest.mark.parametrize("left", list(_DOWNGRADE_STOPS))
def test_0031_downgrade_completes_an_interrupted_downgrade(
    alembic_config: Config, engine: Engine, left: str
) -> None:
    """① stopped before the new trigger's DROP (both triggers): the rerun sends that DROP and the
    DROP CHECK; ② stopped before the DROP CHECK: the rerun sends only it. Each ends with 0030's
    structure."""
    _start(alembic_config, engine)
    try:
        command.downgrade(alembic_config, _DOWN_REVISION)
        at_0030 = _structure(engine)
        command.upgrade(alembic_config, _REVISION)

        with _interrupt_before(_DOWNGRADE_STOPS[left]):
            with pytest.raises(_Interrupted):
                command.downgrade(alembic_config, _DOWN_REVISION)
        assert _version(engine) == _REVISION
        assert _OLD in _trigger_list(engine)
        assert (_NEW in _trigger_list(engine)) == (left == "both triggers")
        assert _has_check(engine)

        with _statements() as statements:
            command.downgrade(alembic_config, _DOWN_REVISION)

        heads = [_head(s) for s in _ddl(statements)]
        drop_check = f"ALTER TABLE {_VERSIONS.upper()}"
        if left == "both triggers":
            assert heads == [f"DROP TRIGGER {_NEW.upper()}", drop_check]
        else:
            assert heads == [drop_check]
        assert _version(engine) == _DOWN_REVISION
        assert _structure(engine) == at_0030
    finally:
        _finish(alembic_config, engine)


@needs_mysql
def test_0031_downgrade_restores_0013(alembic_config: Config, engine: Engine) -> None:
    """Design §7 "降级恢复 0013": down to 0030 — our table's only BEFORE UPDATE trigger is
    `trg_provider_price_versions_before_update` with 0013's body (blanks aside), the CHECK is
    gone, and what 0013 let through (PUBLISHED to RETIRED without an end) goes through again."""
    _start(alembic_config, engine)
    seed = None
    try:
        command.downgrade(alembic_config, _DOWN_REVISION)

        triggers = _structure(engine)["triggers"]
        updates = {
            name for name, row in triggers.items() if row[:3] == (_VERSIONS, "BEFORE", "UPDATE")
        }
        assert updates == {_OLD}
        old_body = _load_0013()._TRIGGERS[_OLD].split("FOR EACH ROW", 1)[1]
        assert _spaces(triggers[_OLD][3]) == _spaces(old_body)
        assert not _has_check(engine)
        seed = _seed(engine)
        with engine.begin() as connection:
            published = _published(connection, seed, 0, T0)
        _execute(
            engine, f"UPDATE {_VERSIONS} SET status = 'RETIRED' WHERE id = :id", {"id": published}
        )
        with engine.connect() as connection:
            row = connection.execute(
                text(f"SELECT status, effective_to FROM {_VERSIONS} WHERE id = :id"),
                {"id": published},
            ).one()
        assert tuple(row) == ("RETIRED", None)
    finally:
        _finish(alembic_config, engine, seed)


@needs_mysql
def test_0031_upgrade_refuses_a_retired_version_without_an_end(
    alembic_config: Config, engine: Engine
) -> None:
    """Design §7 "数据核对": at 0030, a version written directly as RETIRED without an end —
    the upgrade fails naming its id, sends no DDL (no CHECK, only the old trigger), the version
    stays 0030; once a person sets the end, the upgrade goes through."""
    _start(alembic_config, engine)
    seed = None
    try:
        command.downgrade(alembic_config, _DOWN_REVISION)
        seed = _seed(engine)
        with engine.begin() as connection:
            broken = _published(connection, seed, 0, T0)
            retire = text(f"UPDATE {_VERSIONS} SET status = 'RETIRED' WHERE id = :id")
            connection.execute(retire, {"id": broken})
        before = _structure(engine)

        with _statements() as statements:
            with pytest.raises(RuntimeError) as raised:
                command.upgrade(alembic_config, _REVISION)

        message = str(raised.value)
        assert str(broken) in message
        assert "No DDL was issued" in message
        assert _ddl(statements) == []
        assert _structure(engine) == before
        assert not _has_check(engine)
        assert _OLD in _trigger_list(engine)
        assert _NEW not in _trigger_list(engine)
        assert _version(engine) == _DOWN_REVISION

        # 0013's trigger lets a person write the end of a retired version.
        end = f"UPDATE {_VERSIONS} SET effective_to = :end WHERE id = :id"
        _execute(engine, end, {"end": T0 + DAY, "id": broken})
        command.upgrade(alembic_config, _REVISION)
        assert _version(engine) == _REVISION
        assert _has_check(engine)
    finally:
        _finish(alembic_config, engine, seed)


_SHAPE_TABLE = "provider_price_backstop_shape_probe"


def _new_trigger(body_change: tuple[str, str] | None = None) -> str:
    statement = _load_0031()._TRIGGERS[_NEW]
    if body_change is None:
        return statement
    old, new = body_change
    assert statement.count(old) == 1
    return statement.replace(old, new)


_MISSING_CHECK = (
    "    IF OLD.status = 'DRAFT' AND NEW.status = 'PUBLISHED' AND NEW.effective_to IS NOT NULL"
    " THEN\n"
    "        SIGNAL SQLSTATE '45000'\n"
    "            SET MESSAGE_TEXT = 'a provider price version is published without an end';\n"
    "    END IF;\n"
)
_EXTRA_SIGNAL = (
    "    IF NEW.id IS NULL THEN\n"
    "        SIGNAL SQLSTATE '45000'\n"
    "            SET MESSAGE_TEXT = 'a provider price version needs an id';\n"
    "    END IF;\n"
    "END\n"
)

# Design §7 "形状核对" (upgrade): object → (statements that make it, statements that
# remove it, what the message names, the clause MySQL keeps for it if a CHECK on our table).
_Shape = tuple[list[str], list[str], str, str | None]

# The new trigger, changed as named (built when the case runs, from the migration's literal).
_TRIGGER_CHANGES: dict[str, tuple[str, str]] = {
    "④ trigger missing a check": (_MISSING_CHECK, ""),
    "⑤ trigger with one more signal": ("END\n", _EXTRA_SIGNAL),
    "⑥ trigger with one more space": (
        "'provider price periods overlap'",
        "'provider price  periods overlap'",
    ),
}

_CHECK_SHAPES: dict[str, _Shape] = {
    "① weaker check": (
        [f"ALTER TABLE {_VERSIONS} ADD CONSTRAINT {_CHECK_NAME} CHECK ({_CONDITION_WEAKER})"],
        [f"ALTER TABLE {_VERSIONS} DROP CHECK {_CHECK_NAME}"],
        _CHECK_NAME,
        _STORED_WEAKER,
    ),
    "② swapped check": (
        [f"ALTER TABLE {_VERSIONS} ADD CONSTRAINT {_CHECK_NAME} CHECK ({_CONDITION_SWAPPED})"],
        [f"ALTER TABLE {_VERSIONS} DROP CHECK {_CHECK_NAME}"],
        _CHECK_NAME,
        _STORED_SWAPPED,
    ),
    "③ escaped check": (
        [f"ALTER TABLE {_VERSIONS} ADD CONSTRAINT {_CHECK_NAME} CHECK ({_CONDITION_ESCAPED})"],
        [f"ALTER TABLE {_VERSIONS} DROP CHECK {_CHECK_NAME}"],
        _CHECK_NAME,
        _STORED_ESCAPED,
    ),
    "⑧ not enforced": (
        [
            f"ALTER TABLE {_VERSIONS} ADD CONSTRAINT {_CHECK_NAME}"
            f" CHECK ({RETIRED_END_CHECK}) NOT ENFORCED"
        ],
        [f"ALTER TABLE {_VERSIONS} DROP CHECK {_CHECK_NAME}"],
        _CHECK_NAME,
        None,
    ),
    "⑨ on another table": (
        [
            f"CREATE TABLE {_SHAPE_TABLE} (id BIGINT PRIMARY KEY, status VARCHAR(16) NOT NULL,"
            f" effective_to DATETIME NULL, CONSTRAINT {_CHECK_NAME} CHECK ({RETIRED_END_CHECK}))"
        ],
        [f"DROP TABLE {_SHAPE_TABLE}"],
        _CHECK_NAME,
        None,
    ),
}


def _shape(name: str) -> _Shape:
    if name in _CHECK_SHAPES:
        return _CHECK_SHAPES[name]
    return ([_new_trigger(_TRIGGER_CHANGES[name])], [f"DROP TRIGGER {_NEW}"], _NEW, None)


def test_0031_trigger_look_alikes_are_what_their_names_say() -> None:
    """The three hand-made triggers of the shape cases differ from the target as named."""
    target = _new_trigger()
    for name, (old, new) in _TRIGGER_CHANGES.items():
        made = _new_trigger((old, new))
        assert made != target, name
        if new:
            assert made.replace(new, old, 1) == target, name
        else:
            assert old not in made, name


@needs_mysql
@pytest.mark.parametrize("shape", [*_CHECK_SHAPES, *_TRIGGER_CHANGES])
def test_0031_upgrade_refuses_a_look_alike_before_any_ddl(
    alembic_config: Config, engine: Engine, shape: str
) -> None:
    """Design §7 "形状核对": at 0030 an object of the migration's name made by hand — the
    upgrade fails naming it, sends no DDL (information_schema the same before and after), the
    version stays 0030. For ①②③ what MySQL keeps is the literal the unit cases test the
    pattern on."""
    _start(alembic_config, engine)
    make, remove, named, stored = _shape(shape)
    made = False
    try:
        command.downgrade(alembic_config, _DOWN_REVISION)
        for statement in make:
            with engine.begin() as connection:
                connection.exec_driver_sql(statement)
        made = True
        before = _structure(engine)
        if stored is not None:
            clauses = [row[4] for row in before["checks"] if row[1] == _CHECK_NAME]
            assert clauses == [stored]

        with _statements() as statements:
            with pytest.raises(RuntimeError) as raised:
                command.upgrade(alembic_config, _REVISION)

        message = str(raised.value)
        assert named in message
        assert "No DDL was issued" in message
        assert _ddl(statements) == []
        assert _structure(engine) == before
        assert _version(engine) == _DOWN_REVISION
    finally:
        if made:
            for statement in remove:
                with engine.begin() as connection:
                    connection.exec_driver_sql(statement)
        _finish(alembic_config, engine)


@needs_mysql
def test_0031_downgrade_refuses_a_hand_made_old_trigger(
    alembic_config: Config, engine: Engine
) -> None:
    """Design §7 "形状核对" ⑦: at 0031 the new trigger is there and the old trigger's name
    is taken by a hand-made body — the downgrade fails naming it, sends no DDL, the version
    stays 0031."""
    _start(alembic_config, engine)
    statement = (
        _load_0013()
        ._TRIGGERS[_OLD]
        .replace("'provider price periods overlap'", "'provider price periods overlap (hand-made)'")
    )
    made = False
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql(statement)
        made = True
        before = _structure(engine)

        with _statements() as statements:
            with pytest.raises(RuntimeError) as raised:
                command.downgrade(alembic_config, _DOWN_REVISION)

        message = str(raised.value)
        assert _OLD in message
        assert "No DDL was issued" in message
        assert _ddl(statements) == []
        assert _structure(engine) == before
        assert _version(engine) == _REVISION
    finally:
        if made:
            with engine.begin() as connection:
                connection.exec_driver_sql(f"DROP TRIGGER {_OLD}")
        _finish(alembic_config, engine)

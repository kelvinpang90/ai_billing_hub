"""0031_provider_price_backstop（AIH-TASK-145，设计闸门 #535 v5）.

Design §2 "数据库（迁移 0031）", §3 INV-6, §4, §5 (the migration rows), §7 (the migration rows), §8.

The first cases need no database: the revision chain; the literals (the CHECK equals the model's
`RETIRED_END_CHECK`; the new trigger body differs from 0013's in exactly the five places of design
§2; the frozen copy is 0013's word for word); the clause pattern against MySQL's stored forms;
the `--sql` upgrade and downgrade orders; the precheck's four combinations, before any DDL; and
every branch of the upgrade and the downgrade (what is already there → what is sent) against a
stand-in for `information_schema`.

On a real MySQL (`BILLING_TEST_DATABASE_URL`; skipped when unset, never skipped in CI): every
migration row of design §7 — the offline upgrade and downgrade, the structure at head, existing
rows untouched, the interrupted upgrade (three places) and downgrade (two places), the
intermediate states no weaker than 0013 (two places), the restoring downgrade, the data check, the
shape check (nine objects), the precheck, the downgrade without data preconditions. The Alembic
chain does not run on SQLite (0006 onwards ask for `@@log_bin`), so every migration scenario is
MySQL's. What the new trigger and CHECK refuse at head is in test_provider_prices_service.py.

Rows are written with SQL text. Interruptions are made by raising from a `before_cursor_execute`
listener just before the statement the step would send next. Cleaning follows design §7: every case
starts at head with the two price tables empty and ends, in its `finally`, with `TRUNCATE` of the
two price tables (foreign key checks off; no trigger is dropped or rebuilt by the cleaning), head
again (0031 runs again, so a half-done step is finished), and checks that the price tables are
empty, the triggers are head's and the CHECK is there.
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
from sqlalchemy import Connection, create_engine, delete, event, insert, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError

from alembic import command
from app.core.database import create_session_factory
from app.models import auth as _auth_models  # noqa: F401 - 让 Base.metadata 装上这些表
from app.models.ai_catalog import AiModel, AiProvider
from app.models.auth import User, UserRole, UserStatus
from app.models.provider_prices import RETIRED_END_CHECK, ProviderPriceVersion
from app.schemas.provider_prices import ComponentInput
from app.services import provider_prices
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
_CHECK = "ck_provider_price_versions_retired_end"
_OLD = "trg_provider_price_versions_before_update"
_NEW = "trg_provider_price_versions_before_update_v2"

# The price tables' triggers at head: name → (table, timing, event).
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
_REFUSED = {_ER_SIGNAL_EXCEPTION, _ER_CHECK_CONSTRAINT_VIOLATED}

# What MySQL 8.4 keeps as CHECK_CLAUSE (design §2 step 2), word for word. The target, with each of
# the three charset prefixes the pattern allows:
_CANONICAL = "((`status` <> _{}\\'RETIRED\\') or (`effective_to` is not null))"
# Design §7 "形状核对" ①: the target's whole text plus `OR source_type = 'MANUAL'` (weaker).
_STORED_WEAKER = (
    "((`status` <> _utf8mb4\\'RETIRED\\') or (`effective_to` is not null)"
    " or (`source_type` = _utf8mb4\\'MANUAL\\'))"
)
# ②: the same two terms, the other way round.
_STORED_REVERSED = "((`effective_to` is not null) or (`status` <> _utf8mb4\\'RETIRED\\'))"
# ③: one more escape in the literal (`'\rETIRED'`).
_STORED_ESCAPED = "((`status` <> _utf8mb4\\'\\\\rETIRED\\') or (`effective_to` is not null))"

# Design §7 "形状核对": the condition written by hand → what MySQL keeps for it.
_HAND_MADE_CHECKS = {
    "weaker": (f"{RETIRED_END_CHECK} OR source_type = 'MANUAL'", _STORED_WEAKER),
    "reversed": ("effective_to IS NOT NULL OR status <> 'RETIRED'", _STORED_REVERSED),
    "escaped": ("status <> '\\rETIRED' OR effective_to IS NOT NULL", _STORED_ESCAPED),
}

_T0 = dt.datetime(2026, 10, 31, 8, 0, 0)
_SECOND = dt.timedelta(seconds=1)
_DAY = dt.timedelta(days=1)
_TEST_EMAIL_DOMAIN = "@provider-price-backstop-migration-test.example.com"
_CONTEXT = RequestContext(ip_address="203.0.113.9", user_agent="provider-price-backstop-test")

_ALEMBIC_VERSION = text("SELECT version_num FROM alembic_version")
_SET_VERSION = text("UPDATE alembic_version SET version_num = :version")
_PRECHECK = "SELECT @@log_bin, @@log_bin_trust_function_creators"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("migration_0031", _MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_0013() -> ModuleType:
    spec = importlib.util.spec_from_file_location("migration_0013", _MIGRATION_0013)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _spaces(statement: str) -> str:
    return " ".join(statement.split())


def _normalised(statement: str) -> str:
    return re.sub(r"\s+", " ", statement.replace("`", "")).strip().upper()


def _head(statement: str) -> str:
    """The first three words: `CREATE TRIGGER <name>`, `ALTER TABLE PROVIDER_PRICE_VERSIONS`, …"""
    return " ".join(_normalised(statement).split(" ")[:3])


def _body(statement: str) -> str:
    """What MySQL keeps as ACTION_STATEMENT: the text after `FOR EACH ROW`, ends stripped."""
    return statement.split("FOR EACH ROW", 1)[1].strip()


# ---------------------------------------------------------------------------
# No database
# ---------------------------------------------------------------------------


def test_0031_follows_0030() -> None:
    migration = _load()

    assert migration.revision == _REVISION
    assert migration.down_revision == _DOWN_REVISION
    # `alembic_version.version_num` is VARCHAR(32).
    assert len(migration.revision) <= 32


def test_0031_check_is_the_models_and_the_alter_says_enforced() -> None:
    migration = _load()

    assert _spaces(migration._RETIRED_END_CHECK) == _spaces(RETIRED_END_CHECK)
    assert migration._CHECK_NAME == _CHECK
    declared = {
        str(constraint.name): _spaces(str(constraint.sqltext))
        for constraint in ProviderPriceVersion.__table__.constraints
        if constraint.name == _CHECK
    }
    assert declared == {_CHECK: _spaces(RETIRED_END_CHECK)}
    assert _spaces(migration._ALTER_ADD_CHECK) == (
        f"ALTER TABLE {_VERSIONS} ADD CONSTRAINT {_CHECK} CHECK ({RETIRED_END_CHECK}) ENFORCED"
    )
    assert _spaces(migration._ALTER_DROP_CHECK) == f"ALTER TABLE {_VERSIONS} DROP CHECK {_CHECK}"


# Design §2: the five places, as (0013's text, the new text). Everything else is 0013's.
_FIVE_PLACES = [
    # 1: RETIRED and DISCARDED are final.
    (
        "    IF OLD.status = 'DISCARDED' THEN\n"
        "        SIGNAL SQLSTATE '45000'\n"
        "            SET MESSAGE_TEXT = 'a discarded provider price version is final';\n",
        "    IF OLD.status IN ('RETIRED', 'DISCARDED') THEN\n"
        "        SIGNAL SQLSTATE '45000'\n"
        "            SET MESSAGE_TEXT ="
        " 'a retired or discarded provider price version is final';\n",
    ),
    # 2: right after "a draft … is published or discarded", a publish carries no end.
    (
        "            SET MESSAGE_TEXT ="
        " 'a draft provider price version is published or discarded';\n"
        "    END IF;\n",
        "            SET MESSAGE_TEXT ="
        " 'a draft provider price version is published or discarded';\n"
        "    END IF;\n"
        "    IF OLD.status = 'DRAFT' AND NEW.status = 'PUBLISHED'"
        " AND NEW.effective_to IS NOT NULL THEN\n"
        "        SIGNAL SQLSTATE '45000'\n"
        "            SET MESSAGE_TEXT ="
        " 'a provider price version is published without an end';\n"
        "    END IF;\n",
    ),
    # 3: the immutability check covers PUBLISHED only.
    (
        "    IF OLD.status IN ('PUBLISHED', 'RETIRED') THEN\n",
        "    IF OLD.status = 'PUBLISHED' THEN\n",
    ),
    # 4: intersection by NULLs, no sentinel dates.
    (
        "               AND COALESCE(o.effective_from, '1000-01-01')\n"
        "                   < COALESCE(NEW.effective_to, '9999-12-31')\n"
        "               AND COALESCE(NEW.effective_from, '1000-01-01')\n"
        "                   < COALESCE(o.effective_to, '9999-12-31')) THEN\n",
        "               AND (o.effective_from IS NULL OR NEW.effective_to IS NULL\n"
        "                    OR o.effective_from < NEW.effective_to)\n"
        "               AND (NEW.effective_from IS NULL OR o.effective_to IS NULL\n"
        "                    OR NEW.effective_from < o.effective_to)) THEN\n",
    ),
    # 5: inside the immutability check, after "can only be retired": the end of a published
    # version changes by truncation, restore or retirement only.
    (
        "                SET MESSAGE_TEXT ="
        " 'a published provider price version can only be retired';\n"
        "        END IF;\n",
        "                SET MESSAGE_TEXT ="
        " 'a published provider price version can only be retired';\n"
        "        END IF;\n"
        "        IF (NOT (NEW.effective_to <=> OLD.effective_to)\n"
        "            AND NOT (OLD.effective_to IS NULL AND NEW.effective_to IS NOT NULL)\n"
        "            AND NOT (OLD.effective_to IS NOT NULL AND NEW.effective_to IS NULL\n"
        "                     AND NEW.status <=> OLD.status))\n"
        "           OR (NEW.status = 'RETIRED' AND OLD.effective_to IS NOT NULL) THEN\n"
        "            SIGNAL SQLSTATE '45000'\n"
        "                SET MESSAGE_TEXT = 'a published provider price version ends only by"
        " truncation, restore or retirement';\n"
        "        END IF;\n",
    ),
]


def test_0031_new_trigger_differs_from_0013_in_exactly_the_five_places() -> None:
    """Design §7 "字面量": each of the five places is in 0013's body once; applying the five to it
    gives the new body word for word, so every other line is 0013's. No sentinel date is left."""
    migration = _load()
    old = _body(_load_0013()._TRIGGERS[_OLD])
    new = _body(migration._TRIGGERS[_NEW])

    expected = old
    for before, after in _FIVE_PLACES:
        assert expected.count(before) == 1, before
        assert before != after
        expected = expected.replace(before, after)
    assert new == expected
    for sentinel in ("'1000-01-01'", "'9999-12-31'", "COALESCE"):
        assert sentinel not in new
    # The new statement creates the new name on the same table and timing.
    assert _spaces(migration._TRIGGERS[_NEW]).startswith(
        f"CREATE TRIGGER {_NEW} BEFORE UPDATE ON {_VERSIONS} FOR EACH ROW BEGIN"
    )
    # Lock first, then look for an overlap (as 0013).
    locked = _spaces(new).index("FROM ai_providers WHERE id = NEW.provider_id FOR UPDATE")
    assert locked < _spaces(new).index("provider price periods overlap")
    # No FOLLOWS / PRECEDES (design §2 step 5).
    assert "FOLLOWS" not in new and "PRECEDES" not in new


def test_0031_frozen_copy_is_0013s_word_for_word() -> None:
    migration = _load()

    assert set(migration._TRIGGERS) == {_NEW}
    assert set(migration._TRIGGERS_0013) == {_OLD}
    assert migration._TRIGGERS_0013[_OLD] == _load_0013()._TRIGGERS[_OLD]


@pytest.mark.parametrize("charset", ["utf8mb4", "utf8mb3", "latin1"])
def test_0031_pattern_matches_the_canonical_clause_with_each_charset(charset: str) -> None:
    migration = _load()

    assert re.fullmatch(migration._CHECK_CLAUSE_PATTERN, _CANONICAL.format(charset))


@pytest.mark.parametrize(
    "clause",
    [
        _CANONICAL.format("binary"),
        _STORED_WEAKER,
        _STORED_REVERSED,
        _STORED_ESCAPED,
        " " + _CANONICAL.format("utf8mb4"),
        _CANONICAL.format("utf8mb4") + " ",
        _CANONICAL.format("utf8mb4") + "\n",
    ],
    ids=["binary", "weaker", "reversed", "escaped", "space-before", "space-after", "newline"],
)
def test_0031_pattern_refuses_anything_else(clause: str) -> None:
    """Design §7 "形状核对" unit: no normalising, the whole text has to match."""
    migration = _load()

    assert re.fullmatch(migration._CHECK_CLAUSE_PATTERN, clause) is None


class _Result:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self.rows = rows

    def all(self) -> list[tuple[Any, ...]]:
        return list(self.rows)

    def one(self) -> tuple[Any, ...]:
        assert len(self.rows) == 1
        return self.rows[0]

    def __iter__(self) -> Iterator[tuple[Any, ...]]:
        return iter(self.rows)


class _FakeBind:
    """Answers the migration's four queries the way `information_schema` would."""

    def __init__(
        self,
        *,
        checks: list[tuple[str, str, str, str | None]],
        triggers: dict[str, tuple[str, str, str, str]],
        log_bin: int = 0,
        trusted: int = 0,
        violating: tuple[int, ...] = (),
    ) -> None:
        self.checks = checks
        self.triggers = triggers
        self.precheck = (log_bin, trusted)
        self.violating = violating
        self.queries: list[str] = []

    def execute(self, statement: object) -> _Result:
        query = str(statement)
        self.queries.append(query)
        if query == _PRECHECK:
            return _Result([self.precheck])
        if "TABLE_CONSTRAINTS" in query:
            return _Result(list(self.checks))
        if "information_schema.TRIGGERS" in query:
            return _Result([(name, *row) for name, row in self.triggers.items()])
        if "status = 'RETIRED' AND effective_to IS NULL" in query:
            return _Result([(row_id,) for row_id in self.violating])
        raise AssertionError(f"unexpected query: {query}")


class _RecordingOp:
    """Stands in for `alembic.op` online: every statement is recorded instead of run."""

    def __init__(self, bind: _FakeBind) -> None:
        self.bind = bind
        self.statements: list[str] = []

    def get_context(self) -> SimpleNamespace:
        return SimpleNamespace(as_sql=False)

    def get_bind(self) -> _FakeBind:
        return self.bind

    def execute(self, statement: object) -> None:
        self.statements.append(str(statement))


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


def _found(statement: str) -> tuple[str, str, str, str]:
    """A trigger as `information_schema.TRIGGERS` shows it (whitespace around the body kept)."""
    return (_VERSIONS, "BEFORE", "UPDATE", "\n" + _body(statement) + "\n")


def _target_check() -> tuple[str, str, str, str | None]:
    return (_VERSIONS, "CHECK", "YES", _CANONICAL.format("utf8mb4"))


# State → (CHECK rows, trigger names present). The new trigger and the old one are the frozen ones.
_STATES: dict[str, tuple[bool, tuple[str, ...]]] = {
    "at 0030": (False, (_OLD,)),
    "check only": (True, (_OLD,)),
    "both triggers": (True, (_OLD, _NEW)),
    "at head": (True, (_NEW,)),
}


def _bind(migration: ModuleType, state: str, **kwargs: Any) -> _FakeBind:
    has_check, names = _STATES[state]
    statements = {_OLD: migration._TRIGGERS_0013[_OLD], _NEW: migration._TRIGGERS[_NEW]}
    return _FakeBind(
        checks=[_target_check()] if has_check else [],
        triggers={name: _found(statements[name]) for name in names},
        **kwargs,
    )


def _run(migration: ModuleType, monkeypatch, bind: _FakeBind, direction: str) -> _RecordingOp:
    recorder = _RecordingOp(bind)
    monkeypatch.setattr(migration, "op", recorder)
    getattr(migration, direction)()
    return recorder


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ("at 0030", ["alter add", "create new", "drop old"]),
        ("check only", ["create new", "drop old"]),
        ("both triggers", ["drop old"]),
        ("at head", []),
    ],
)
def test_0031_upgrade_sends_only_what_is_missing(
    monkeypatch, state: str, expected: list[str]
) -> None:
    """Design §2 "升级": each step judged on what is there; complete → not even the precheck."""
    migration = _load()
    named = {
        "alter add": migration._ALTER_ADD_CHECK,
        "create new": migration._TRIGGERS[_NEW],
        "drop old": f"DROP TRIGGER {_OLD}",
    }
    bind = _bind(migration, state)

    recorder = _run(migration, monkeypatch, bind, "upgrade")

    assert recorder.statements == [named[name] for name in expected]
    assert (_PRECHECK in bind.queries) == bool(expected)


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ("at head", ["create old", "drop new", "alter drop"]),
        ("both triggers", ["drop new", "alter drop"]),
        ("check only", ["alter drop"]),
        ("at 0030", []),
    ],
)
def test_0031_downgrade_sends_only_what_is_missing(
    monkeypatch, state: str, expected: list[str]
) -> None:
    """Design §2 "降级": build the old one first, then drop the new one, then the CHECK."""
    migration = _load()
    named = {
        "create old": migration._TRIGGERS_0013[_OLD],
        "drop new": f"DROP TRIGGER {_NEW}",
        "alter drop": migration._ALTER_DROP_CHECK,
    }
    bind = _bind(migration, state)

    recorder = _run(migration, monkeypatch, bind, "downgrade")

    assert recorder.statements == [named[name] for name in expected]
    assert (_PRECHECK in bind.queries) == bool(expected)


@pytest.mark.parametrize(
    ("log_bin", "trusted", "refused"),
    [(1, 0, True), (1, 1, False), (0, 0, False), (0, 1, False)],
)
@pytest.mark.parametrize(("direction", "state"), [("upgrade", "at 0030"), ("downgrade", "at head")])
def test_0031_precheck_refuses_only_binlog_without_the_switch_before_any_ddl(
    monkeypatch, log_bin: int, trusted: int, refused: bool, direction: str, state: str
) -> None:
    """Design §7 "预检": query and decision as 0013's; only "binlog on, switch off" refuses, and
    then nothing at all is sent; otherwise the same run does reach its DDL."""
    migration = _load()
    assert migration._PREREQUISITE_QUERY == _load_0013()._PREREQUISITE_QUERY == _PRECHECK
    bind = _bind(migration, state, log_bin=log_bin, trusted=trusted)
    recorder = _RecordingOp(bind)
    monkeypatch.setattr(migration, "op", recorder)

    if refused:
        with pytest.raises(RuntimeError) as raised:
            getattr(migration, direction)()
        assert str(raised.value) == migration._PREREQUISITE_ERROR
        assert "log_bin_trust_function_creators" in str(raised.value)
        assert recorder.statements == []
    else:
        getattr(migration, direction)()
        assert len(recorder.statements) == 3


def test_0031_data_check_lists_the_ids_and_sends_nothing(monkeypatch) -> None:
    """Design §2 step 3: RETIRED rows without an end → refused before any DDL, ids listed."""
    migration = _load()
    bind = _bind(migration, "at 0030", violating=tuple(range(101, 121)))
    recorder = _RecordingOp(bind)
    monkeypatch.setattr(migration, "op", recorder)

    with pytest.raises(RuntimeError) as raised:
        migration.upgrade()

    message = str(raised.value)
    assert recorder.statements == []
    assert all(str(row_id) in message for row_id in range(101, 121))
    assert _CHECK in message
    assert "No DDL was issued" in message
    assert "LIMIT 20" in [q for q in bind.queries if "RETIRED" in q][0]


# Design §7 "形状核对" unit: what is there by that name → the object the message names.
_WRONG_CHECKS: dict[str, list[tuple[str, str, str, str | None]]] = {
    "not enforced": [(_VERSIONS, "CHECK", "NO", _CANONICAL.format("utf8mb4"))],
    "other table": [("another_table", "CHECK", "YES", _CANONICAL.format("utf8mb4"))],
    "two of that name": [_target_check(), ("another_table", "CHECK", "YES", "(1)")],
    "not a check": [(_VERSIONS, "UNIQUE", "YES", None)],
    "weaker clause": [(_VERSIONS, "CHECK", "YES", _STORED_WEAKER)],
    "binary prefix": [(_VERSIONS, "CHECK", "YES", _CANONICAL.format("binary"))],
}


@pytest.mark.parametrize("wrong", list(_WRONG_CHECKS))
def test_0031_upgrade_refuses_a_check_of_that_name_that_is_not_the_target(
    monkeypatch, wrong: str
) -> None:
    migration = _load()
    bind = _bind(migration, "at 0030")
    bind.checks = _WRONG_CHECKS[wrong]
    recorder = _RecordingOp(bind)
    monkeypatch.setattr(migration, "op", recorder)

    with pytest.raises(RuntimeError) as raised:
        migration.upgrade()

    assert recorder.statements == []
    assert f"check {_CHECK}" in str(raised.value)
    assert "No DDL was issued" in str(raised.value)


def _variants(statement: str) -> dict[str, str]:
    """Bodies that look like `statement`'s but are not it, word for word."""
    body = _body(statement)
    extra = (
        "    IF NEW.id IS NULL THEN\n"
        "        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'one more';\n"
        "    END IF;\n"
        "END"
    )
    return {
        "one more signal": body[: body.rindex("END")] + extra,
        "one more space": body.replace("periods overlap", "periods  overlap"),
        "on another event": body,
    }


@pytest.mark.parametrize("wrong", ["one more signal", "one more space", "on another event"])
@pytest.mark.parametrize("direction", ["upgrade", "downgrade"])
def test_0031_refuses_a_trigger_of_that_name_that_is_not_the_frozen_one(
    monkeypatch, wrong: str, direction: str
) -> None:
    """Upgrade: the new name present but not this migration's. Downgrade: the old name present but
    not 0013's (the new one is this migration's). Neither sends anything."""
    migration = _load()
    if direction == "upgrade":
        bind = _bind(migration, "check only")
        name, statement = _NEW, migration._TRIGGERS[_NEW]
    else:
        bind = _bind(migration, "both triggers")
        name, statement = _OLD, migration._TRIGGERS_0013[_OLD]
    kind = "INSERT" if wrong == "on another event" else "UPDATE"
    bind.triggers[name] = (_VERSIONS, "BEFORE", kind, _variants(statement)[wrong])
    recorder = _RecordingOp(bind)
    monkeypatch.setattr(migration, "op", recorder)

    with pytest.raises(RuntimeError) as raised:
        getattr(migration, direction)()

    assert recorder.statements == []
    assert f"trigger {name}" in str(raised.value)


def test_0031_downgrade_refuses_a_new_trigger_that_is_not_this_migrations(monkeypatch) -> None:
    migration = _load()
    bind = _bind(migration, "at head")
    bind.triggers[_NEW] = (
        _VERSIONS,
        "BEFORE",
        "UPDATE",
        _variants(migration._TRIGGERS[_NEW])["one more space"],
    )
    recorder = _RecordingOp(bind)
    monkeypatch.setattr(migration, "op", recorder)

    with pytest.raises(RuntimeError) as raised:
        migration.downgrade()

    assert recorder.statements == []
    assert f"trigger {_NEW}" in str(raised.value)


def test_0031_offline_upgrade_and_downgrade_emit_the_design_order_without_reading(
    monkeypatch,
) -> None:
    migration = _load()
    upgrade = _OfflineOp()
    monkeypatch.setattr(migration, "op", upgrade)
    migration.upgrade()

    assert upgrade.statements == [
        migration._ALTER_ADD_CHECK,
        migration._TRIGGERS[_NEW],
        f"DROP TRIGGER IF EXISTS {_OLD}",
    ]

    downgrade = _OfflineOp()
    monkeypatch.setattr(migration, "op", downgrade)
    migration.downgrade()

    assert downgrade.statements == [
        migration._TRIGGERS_0013[_OLD],
        f"DROP TRIGGER IF EXISTS {_NEW}",
        migration._ALTER_DROP_CHECK,
    ]


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
    """The precheck query answers binlog on, switch off."""

    def answer(_conn, _cursor, statement, params, _context, _many):
        if _spaces(statement) == _PRECHECK:
            return "SELECT 1, 0", params
        return statement, params

    event.listen(Engine, "before_cursor_execute", answer, retval=True)
    try:
        yield
    finally:
        event.remove(Engine, "before_cursor_execute", answer)


_CREATE_NEW = f"CREATE TRIGGER {_NEW.upper()}"
_DROP_NEW = f"DROP TRIGGER {_NEW.upper()}"
_CREATE_OLD = f"CREATE TRIGGER {_OLD.upper()}"
_DROP_OLD = f"DROP TRIGGER {_OLD.upper()}"
_ALTER = f"ALTER TABLE {_VERSIONS.upper()}"


def _version(engine: Engine) -> str:
    with engine.connect() as connection:
        return str(connection.execute(_ALEMBIC_VERSION).scalar_one())


def _set_version(engine: Engine, version: str) -> None:
    with engine.begin() as connection:
        connection.execute(_SET_VERSION, {"version": version})


def _execute(engine: Engine, statement: str) -> None:
    """One statement as written (no bind parameters, so a backslash or colon stays as it is)."""
    with engine.begin() as connection:
        connection.exec_driver_sql(statement)


def _to_head(alembic_config: Config, engine: Engine) -> None:
    """Head, complete: at 0031 the version goes back to 0030 first, so 0031 runs again — nothing
    is sent when complete, otherwise it finishes the half-done step."""
    if _version(engine) == _REVISION:
        _set_version(engine, _DOWN_REVISION)
    command.upgrade(alembic_config, "head")


def _clean(engine: Engine, audited: bool = False) -> None:
    """Design §7: TRUNCATE (DDL, no trigger fires, none is dropped or rebuilt) with foreign key
    checks off. `audited`: the services wrote audit rows that refer to the seeded admin."""
    with engine.begin() as connection:
        connection.exec_driver_sql("SET FOREIGN_KEY_CHECKS = 0")
        try:
            connection.exec_driver_sql(f"TRUNCATE TABLE {_COMPONENTS}")
            connection.exec_driver_sql(f"TRUNCATE TABLE {_VERSIONS}")
        finally:
            connection.exec_driver_sql("SET FOREIGN_KEY_CHECKS = 1")
    if audited:
        with engine.begin() as connection:
            connection.exec_driver_sql("TRUNCATE TABLE audit_logs")


def _price_triggers(engine: Engine) -> dict[str, tuple[str, str, str, str]]:
    """Triggers on the two price tables: name → (table, timing, event, body stripped). Not the
    ACTION_ORDER: a trigger built next to another of the same timing and left alone afterwards
    may keep the number it got."""
    query = text(
        "SELECT TRIGGER_NAME, EVENT_OBJECT_TABLE, ACTION_TIMING, EVENT_MANIPULATION,"
        " ACTION_STATEMENT FROM information_schema.TRIGGERS"
        " WHERE TRIGGER_SCHEMA = DATABASE()"
        f" AND EVENT_OBJECT_TABLE IN ('{_VERSIONS}', '{_COMPONENTS}')"
    )
    with engine.connect() as connection:
        return {
            str(name): (str(table), str(timing), str(kind), str(body).strip())
            for name, table, timing, kind, body in connection.execute(query).all()
        }


def _checks_named(engine: Engine) -> list[tuple[str, str, str, str | None]]:
    """Every constraint in the schema named like ours: (table, type, enforced, clause)."""
    query = text(
        "SELECT t.TABLE_NAME, t.CONSTRAINT_TYPE, t.ENFORCED, c.CHECK_CLAUSE"
        " FROM information_schema.TABLE_CONSTRAINTS t"
        " LEFT JOIN information_schema.CHECK_CONSTRAINTS c"
        " ON c.CONSTRAINT_SCHEMA = t.CONSTRAINT_SCHEMA AND c.CONSTRAINT_NAME = t.CONSTRAINT_NAME"
        f" WHERE t.CONSTRAINT_SCHEMA = DATABASE() AND t.CONSTRAINT_NAME = '{_CHECK}'"
        " ORDER BY t.TABLE_NAME"
    )
    with engine.connect() as connection:
        return [
            (str(table), str(kind), str(enforced), None if clause is None else str(clause))
            for table, kind, enforced, clause in connection.execute(query).all()
        ]


def _version_checks(engine: Engine) -> list[tuple[str, str, str]]:
    """The CHECKs on `provider_price_versions`: (name, enforced, clause)."""
    query = text(
        "SELECT t.CONSTRAINT_NAME, t.ENFORCED, c.CHECK_CLAUSE"
        " FROM information_schema.TABLE_CONSTRAINTS t"
        " JOIN information_schema.CHECK_CONSTRAINTS c"
        " ON c.CONSTRAINT_SCHEMA = t.CONSTRAINT_SCHEMA AND c.CONSTRAINT_NAME = t.CONSTRAINT_NAME"
        f" WHERE t.TABLE_SCHEMA = DATABASE() AND t.TABLE_NAME = '{_VERSIONS}'"
        " AND t.CONSTRAINT_TYPE = 'CHECK' ORDER BY t.CONSTRAINT_NAME"
    )
    with engine.connect() as connection:
        return [tuple(str(value) for value in row) for row in connection.execute(query).all()]


def _structure(engine: Engine) -> dict[str, Any]:
    """What 0031 touches, from information_schema word for word: the CHECKs of the versions table,
    every constraint of our CHECK's name, and the price tables' triggers."""
    return {
        "checks": _version_checks(engine),
        "named": _checks_named(engine),
        "triggers": _price_triggers(engine),
    }


def _rows(engine: Engine) -> dict[str, list[dict[str, object]]]:
    with engine.connect() as connection:
        return {
            table: [
                dict(row)
                for row in connection.execute(text(f"SELECT * FROM {table} ORDER BY id")).mappings()
            ]
            for table in (_VERSIONS, _COMPONENTS)
        }


@dataclass(frozen=True)
class _Seed:
    user: User
    provider_id: int
    provider_public_id: str
    model_id: int
    model_public_id: str
    other_model_id: int
    other_model_public_id: str


def _seed(engine: Engine) -> _Seed:
    """An admin (detached, as `require_admin` returns it), a provider and two of its models."""
    marker = uuid.uuid4().hex[:12]
    now = dt.datetime(2026, 10, 31, 8, 0, 0)
    with create_session_factory(engine)() as session:
        user = User(
            email=f"{marker}{_TEST_EMAIL_DOMAIN}",
            password_hash="not-a-real-hash",
            role=UserRole.ADMIN,
            status=UserStatus.ACTIVE,
            created_at=now,
            updated_at=now,
        )
        session.add(user)
        session.commit()

    with engine.begin() as connection:

        def new_id(statement) -> int:
            return int(connection.execute(statement).inserted_primary_key[0])

        provider_public_id = str(uuid.uuid4())
        provider_id = new_id(
            insert(AiProvider).values(
                public_id=provider_public_id,
                code=f"p145-{marker}",
                display_name="Fictional provider",
                created_at=now,
                updated_at=now,
            )
        )
        models = []
        for name in ("m", "n"):
            public_id = str(uuid.uuid4())
            model_id = new_id(
                insert(AiModel).values(
                    public_id=public_id,
                    provider_id=provider_id,
                    code=f"{name}145-{marker}",
                    display_name="Fictional model",
                    created_at=now,
                    updated_at=now,
                )
            )
            models.append((model_id, public_id))
    return _Seed(
        user=user,
        provider_id=provider_id,
        provider_public_id=provider_public_id,
        model_id=models[0][0],
        model_public_id=models[0][1],
        other_model_id=models[1][0],
        other_model_public_id=models[1][1],
    )


def _finish(
    alembic_config: Config, engine: Engine, seed: _Seed | None, audited: bool = False
) -> None:
    """Design §7 cleaning: TRUNCATE, head again, the seeded parents; the database ends at head
    with both price tables empty, head's triggers and the CHECK."""
    _clean(engine, audited)
    _to_head(alembic_config, engine)
    if seed is not None:
        with engine.begin() as connection:
            connection.execute(delete(AiModel).where(AiModel.provider_id == seed.provider_id))
            connection.execute(delete(AiProvider).where(AiProvider.id == seed.provider_id))
            connection.execute(delete(User).where(User.id == seed.user.id))
    assert _version(engine) == _REVISION
    assert _rows(engine) == {_VERSIONS: [], _COMPONENTS: []}
    triggers = _price_triggers(engine)
    assert {name: row[:3] for name, row in triggers.items()} == _HEAD_TRIGGERS
    assert _load()._check_is_target(_checks_named(engine))


def _start(alembic_config: Config, engine: Engine) -> None:
    _to_head(alembic_config, engine)
    _clean(engine)


def _at_0030(alembic_config: Config, engine: Engine) -> None:
    command.downgrade(alembic_config, _DOWN_REVISION)
    assert _version(engine) == _DOWN_REVISION


_INSERT_DRAFT = text(
    f"INSERT INTO {_VERSIONS} (public_id, provider_id, model_id, source_currency, source_type,"
    " source_reference, status, created_by, created_at, updated_at) VALUES (:public_id,"
    " :provider_id, :model_id, 'USD', 'MANUAL', 'Fictional price sheet', 'DRAFT', :user, :now,"
    " :now)"
)
_INSERT_COMPONENT = text(
    f"INSERT INTO {_COMPONENTS} (provider_price_version_id, usage_meter_component_id,"
    " unit_quantity, rate_amount, created_at)"
    " SELECT :id, id, 1, 1, :now FROM usage_meter_components WHERE component_code = :code"
)
_PUBLISH = text(
    f"UPDATE {_VERSIONS} SET status = 'PUBLISHED', effective_from = :start, effective_to = :end,"
    " approved_by = :user, approved_at = :now, updated_at = :now WHERE id = :id"
)


def _draft(connection: Connection, seed: _Seed, model_id: int | None = None) -> int:
    """A complete draft (one EMBEDDING_TOKEN component) written with SQL text."""
    public_id = str(uuid.uuid4())
    connection.execute(
        _INSERT_DRAFT,
        {
            "public_id": public_id,
            "provider_id": seed.provider_id,
            "model_id": seed.model_id if model_id is None else model_id,
            "user": seed.user.id,
            "now": _T0,
        },
    )
    found = text(f"SELECT id FROM {_VERSIONS} WHERE public_id = :public_id")
    version_id = int(connection.execute(found, {"public_id": public_id}).scalar_one())
    connection.execute(_INSERT_COMPONENT, {"id": version_id, "now": _T0, "code": "EMBEDDING_TOKEN"})
    return version_id


def _publish_params(seed: _Seed, version_id: int, start, end=None) -> dict[str, object]:
    return {"start": start, "end": end, "user": seed.user.id, "now": _T0, "id": version_id}


def _publish(connection: Connection, seed: _Seed, version_id: int, start, end=None) -> None:
    connection.execute(_PUBLISH, _publish_params(seed, version_id, start, end))


def _set(connection: Connection, version_id: int, assignment: str, **params: object) -> None:
    connection.execute(
        text(f"UPDATE {_VERSIONS} SET {assignment} WHERE id = :id"), {"id": version_id, **params}
    )


def _refusal(engine: Engine, statement, params: dict[str, object]) -> tuple[int, str]:
    """Runs one write in its own transaction; returns the MySQL error number and message."""
    with pytest.raises(DBAPIError) as raised:
        with engine.begin() as connection:
            connection.execute(statement, params)
    code, message = raised.value.orig.args[:2]
    return int(code), str(message)


@dataclass(frozen=True)
class _Timeline:
    open_published: int
    truncated: int
    retired: int
    empty: int
    draft: int
    discarded: int


def _every_kind(engine: Engine, seed: _Seed) -> _Timeline:
    """One row of every kind, written with SQL text in the order the service would write them
    (valid both under 0013 and at head): on the first model a version truncated by its successor,
    the successor open, a draft, a discarded draft; on the other a retired version with an end and
    the empty period of a withdrawn reservation."""
    with engine.begin() as connection:
        truncated = _draft(connection, seed)
        _publish(connection, seed, truncated, _T0)
        _set(connection, truncated, "effective_to = :end", end=_T0 + _DAY)
        open_published = _draft(connection, seed)
        _publish(connection, seed, open_published, _T0 + _DAY)
        draft = _draft(connection, seed)
        discarded = _draft(connection, seed)
        _set(connection, discarded, "status = 'DISCARDED'")
        retired = _draft(connection, seed, seed.other_model_id)
        _publish(connection, seed, retired, _T0)
        _set(connection, retired, "status = 'RETIRED', effective_to = :end", end=_T0 + _SECOND)
        empty = _draft(connection, seed, seed.other_model_id)
        _publish(connection, seed, empty, _T0 + _DAY)
        _set(connection, empty, "status = 'RETIRED', effective_to = effective_from")
    return _Timeline(open_published, truncated, retired, empty, draft, discarded)


def _assert_head_structure(engine: Engine) -> None:
    """Design §7 "迁移结构": the CHECK on this table, ENFORCED, its stored clause matching the
    pattern in full; exactly the three version triggers, the new one's body the frozen one."""
    migration = _load()
    named = _checks_named(engine)
    assert len(named) == 1
    table, kind, enforced, clause = named[0]
    assert (table, kind, enforced) == (_VERSIONS, "CHECK", "YES")
    assert clause is not None
    assert "'RETIRED" in clause and "effective_to" in clause
    assert re.fullmatch(migration._CHECK_CLAUSE_PATTERN, clause)
    assert migration._check_is_target(named)
    triggers = _price_triggers(engine)
    on_versions = {name: row[1:3] for name, row in triggers.items() if row[0] == _VERSIONS}
    assert on_versions == {
        "trg_provider_price_versions_before_insert": ("BEFORE", "INSERT"),
        _NEW: ("BEFORE", "UPDATE"),
        "trg_provider_price_versions_before_delete": ("BEFORE", "DELETE"),
    }
    assert _OLD not in triggers
    assert triggers[_NEW][3] == _body(migration._TRIGGERS[_NEW])
    assert migration._trigger_is(triggers[_NEW], migration._TRIGGERS[_NEW])


@needs_mysql
def test_0031_offline_upgrade_and_downgrade(alembic_config: Config) -> None:
    """Design §7 "离线": the `--sql` upgrade emits the ALTER, the new trigger's CREATE and the old
    one's DROP IF EXISTS in this order and reads nothing; the downgrade emits the old trigger's
    CREATE, the new one's DROP IF EXISTS and the ALTER … DROP CHECK."""
    upgrade_buffer = io.StringIO()
    command.upgrade(
        Config("alembic.ini", output_buffer=upgrade_buffer),
        f"{_DOWN_REVISION}:{_REVISION}",
        sql=True,
    )
    emitted = _normalised(upgrade_buffer.getvalue())
    order = [
        rf"ALTER TABLE {_VERSIONS.upper()} ADD CONSTRAINT {_CHECK.upper()} CHECK",
        rf"CREATE TRIGGER {_NEW.upper()} BEFORE UPDATE ON {_VERSIONS.upper()}",
        rf"DROP TRIGGER IF EXISTS {_OLD.upper()}\b",
    ]
    positions = [re.search(part, emitted).start() for part in order]
    assert positions == sorted(positions)
    assert "@@LOG_BIN" not in emitted
    assert "INFORMATION_SCHEMA" not in emitted

    downgrade_buffer = io.StringIO()
    command.downgrade(
        Config("alembic.ini", output_buffer=downgrade_buffer),
        f"{_REVISION}:{_DOWN_REVISION}",
        sql=True,
    )
    emitted = _normalised(downgrade_buffer.getvalue())
    order = [
        rf"CREATE TRIGGER {_OLD.upper()} BEFORE UPDATE ON {_VERSIONS.upper()}",
        rf"DROP TRIGGER IF EXISTS {_NEW.upper()}\b",
        rf"ALTER TABLE {_VERSIONS.upper()} DROP CHECK {_CHECK.upper()}",
    ]
    positions = [re.search(part, emitted).start() for part in order]
    assert positions == sorted(positions)
    assert "INFORMATION_SCHEMA" not in emitted


@needs_mysql
def test_0031_structure_at_head(alembic_config: Config, engine: Engine) -> None:
    _start(alembic_config, engine)
    try:
        _assert_head_structure(engine)
    finally:
        _finish(alembic_config, engine, None)


class _Clock:
    def __init__(self, moment: dt.datetime) -> None:
        self.now = moment

    def __call__(self) -> dt.datetime:
        return self.now


@needs_mysql
def test_0031_upgrade_leaves_existing_rows_as_they_were(
    alembic_config: Config, engine: Engine
) -> None:
    """Design §7 "不改已有行" / INV-6: at 0030, through the services, an untruncated published
    version, a truncated one, a retired one with an end, the empty period of a withdrawn
    reservation, a draft and a discarded draft (with components); after the upgrade both price
    tables are the same, row by row and column by column."""
    _start(alembic_config, engine)
    seed = None
    try:
        _at_0030(alembic_config, engine)
        seed = _seed(engine)
        factory = create_session_factory(engine)
        clock = _Clock(utc_now().replace(microsecond=0))
        components = [
            ComponentInput.model_validate(
                {
                    "component_code": "EMBEDDING_TOKEN",
                    "unit_quantity": "1000000",
                    "rate_amount": "1.11111111",
                }
            )
        ]
        common = {"actor": seed.user, "context": _CONTEXT, "clock": clock}

        def draft(model_public_id: str) -> str:
            return provider_prices.create_draft(
                factory,
                provider_id=seed.provider_public_id,
                model_id=model_public_id,
                source_currency="USD",
                source_reference="Fictional price sheet",
                components=components,
                **common,
            ).id

        def publish(version_id: str, effective_from: dt.datetime | None) -> str:
            return provider_prices.publish(
                factory, price_version_id=version_id, effective_from=effective_from, **common
            ).id

        def retire(version_id: str) -> None:
            provider_prices.retire(
                factory, price_version_id=version_id, reason="Fictional retirement", **common
            )

        tomorrow = clock.now + _DAY
        publish(draft(seed.model_public_id), None)
        publish(draft(seed.model_public_id), tomorrow)
        draft(seed.model_public_id)
        provider_prices.discard_draft(
            factory, price_version_id=draft(seed.model_public_id), **common
        )
        retired = publish(draft(seed.other_model_public_id), None)
        clock.now += 5 * _SECOND
        retire(retired)
        retire(publish(draft(seed.other_model_public_id), tomorrow))
        before = _rows(engine)
        statuses = sorted(str(row["status"]) for row in before[_VERSIONS])
        assert statuses == ["DISCARDED", "DRAFT", "PUBLISHED", "PUBLISHED", "RETIRED", "RETIRED"]
        assert len(before[_COMPONENTS]) == 6

        command.upgrade(alembic_config, _REVISION)

        assert _rows(engine) == before
    finally:
        _finish(alembic_config, engine, seed, audited=True)


# Design §7 "中断后重跑" (upgrade): where it stopped → the statement it stopped before.
_UPGRADE_STOPS = {
    "check only": _CREATE_NEW,
    "both triggers": _DROP_OLD,
    "complete": None,
}


def _interrupted_upgrade(alembic_config: Config, engine: Engine, where: str) -> None:
    """Leaves the database where an upgrade from 0030 stopped; `complete`: everything done, only
    the version not written."""
    stop = _UPGRADE_STOPS[where]
    if stop is None:
        _set_version(engine, _DOWN_REVISION)
        return
    _at_0030(alembic_config, engine)
    with _interrupt_before(stop):
        with pytest.raises(_Interrupted):
            command.upgrade(alembic_config, _REVISION)
    assert _version(engine) == _DOWN_REVISION


@needs_mysql
@pytest.mark.parametrize("where", list(_UPGRADE_STOPS))
def test_0031_upgrade_completes_an_interrupted_upgrade(
    alembic_config: Config, engine: Engine, where: str
) -> None:
    """Design §7 "迁移：中断后重跑": each rerun sends only what is left (none for `complete`)
    and ends with the structure of one uninterrupted upgrade."""
    _start(alembic_config, engine)
    try:
        reference = _structure(engine)
        _interrupted_upgrade(alembic_config, engine, where)

        with _statements() as statements:
            command.upgrade(alembic_config, _REVISION)

        expected = {
            "check only": [_CREATE_NEW, _DROP_OLD],
            "both triggers": [_DROP_OLD],
            "complete": [],
        }[where]
        assert [_head(s) for s in _ddl(statements)] == expected
        assert _version(engine) == _REVISION
        assert _structure(engine) == reference
    finally:
        _finish(alembic_config, engine, None)


@needs_mysql
@pytest.mark.parametrize("where", ["check only", "both triggers"])
def test_0031_intermediate_states_are_no_weaker_than_0013(
    alembic_config: Config, engine: Engine, where: str
) -> None:
    """Design §7 "迁移：中间状态不弱于 0013": at both places the writes 0013 already refused are
    refused (a published version's currency, a retired one back to published, a draft straight to
    RETIRED, an overlapping period); with both triggers the two this design adds (a publish with an
    end, a retirement without an end) are refused too; with the CHECK only, the retirement without
    an end is. The tables do not change."""
    _start(alembic_config, engine)
    seed = None
    try:
        seed = _seed(engine)
        _interrupted_upgrade(alembic_config, engine, where)
        triggers = set(_price_triggers(engine))
        assert (_NEW in triggers) == (where == "both triggers")
        assert _OLD in triggers
        with engine.begin() as connection:
            published = _draft(connection, seed)
            _publish(connection, seed, published, _T0)
            retired = _draft(connection, seed, seed.other_model_id)
            _publish(connection, seed, retired, _T0)
            _set(connection, retired, "status = 'RETIRED', effective_to = :end", end=_T0 + _DAY)
            draft = _draft(connection, seed)
            other_draft = _draft(connection, seed)
        before = _rows(engine)

        update = f"UPDATE {_VERSIONS} SET {{}} WHERE id = :id"
        refused_by_0013 = {
            "currency": (text(update.format("source_currency = 'EUR'")), {"id": published}),
            "back to published": (text(update.format("status = 'PUBLISHED'")), {"id": retired}),
            "draft to retired": (
                text(
                    update.format(
                        "status = 'RETIRED', effective_from = :start, effective_to = :end,"
                        " approved_by = :user, approved_at = :now"
                    )
                ),
                _publish_params(seed, other_draft, _T0 - _DAY, _T0 - _DAY + _SECOND),
            ),
            "overlap": (_PUBLISH, _publish_params(seed, draft, _T0 + _SECOND)),
        }
        added_by_0031 = {
            "publish with an end": (
                _PUBLISH,
                _publish_params(seed, draft, _T0 - _DAY, _T0 - _DAY + _SECOND),
            ),
            "retire without an end": (
                text(update.format("status = 'RETIRED'")),
                {"id": published},
            ),
        }
        for name, (statement, params) in refused_by_0013.items():
            assert _refusal(engine, statement, params)[0] in _REFUSED, name
        if where == "both triggers":
            for name, (statement, params) in added_by_0031.items():
                assert _refusal(engine, statement, params)[0] in _REFUSED, name
        statement, params = added_by_0031["retire without an end"]
        assert _refusal(engine, statement, params)[0] == _ER_CHECK_CONSTRAINT_VIOLATED
        assert _rows(engine) == before
    finally:
        _finish(alembic_config, engine, seed)


# Design §7 "降级中断后重跑": where it stopped → the statement it stopped before.
_DOWNGRADE_STOPS = {
    "both triggers": _DROP_NEW,
    "check left": _ALTER,
}


@needs_mysql
@pytest.mark.parametrize("where", list(_DOWNGRADE_STOPS))
def test_0031_downgrade_completes_an_interrupted_downgrade(
    alembic_config: Config, engine: Engine, where: str
) -> None:
    """Each rerun sends only what is left and ends with 0030's structure."""
    _start(alembic_config, engine)
    try:
        _at_0030(alembic_config, engine)
        at_0030 = _structure(engine)
        command.upgrade(alembic_config, _REVISION)

        with _interrupt_before(_DOWNGRADE_STOPS[where]):
            with pytest.raises(_Interrupted):
                command.downgrade(alembic_config, _DOWN_REVISION)
        assert _version(engine) == _REVISION
        triggers = set(_price_triggers(engine))
        assert _OLD in triggers
        assert (_NEW in triggers) == (where == "both triggers")

        with _statements() as statements:
            command.downgrade(alembic_config, _DOWN_REVISION)

        expected = [_DROP_NEW, _ALTER] if where == "both triggers" else [_ALTER]
        assert [_head(s) for s in _ddl(statements)] == expected
        assert _version(engine) == _DOWN_REVISION
        assert _structure(engine) == at_0030
    finally:
        _finish(alembic_config, engine, None)


@needs_mysql
def test_0031_downgrade_restores_0013(alembic_config: Config, engine: Engine) -> None:
    """Design §7 "降级恢复 0013": the versions table's only BEFORE UPDATE is 0013's, its body
    0013's; the CHECK is gone; a write 0013 let through (PUBLISHED → RETIRED without an end) goes
    through again — the restore is real."""
    _start(alembic_config, engine)
    seed = None
    try:
        seed = _seed(engine)
        _at_0030(alembic_config, engine)

        triggers = _price_triggers(engine)
        updates = [
            name for name, row in triggers.items() if row[:3] == (_VERSIONS, "BEFORE", "UPDATE")
        ]
        assert updates == [_OLD]
        assert _spaces(triggers[_OLD][3]) == _spaces(_body(_load_0013()._TRIGGERS[_OLD]))
        assert _checks_named(engine) == []
        assert _CHECK not in [name for name, _, _ in _version_checks(engine)]

        with engine.begin() as connection:
            published = _draft(connection, seed)
            _publish(connection, seed, published, _T0)
        with engine.begin() as connection:
            _set(connection, published, "status = 'RETIRED'")
        assert [row["status"] for row in _rows(engine)[_VERSIONS]] == ["RETIRED"]
    finally:
        _finish(alembic_config, engine, seed)


@needs_mysql
def test_0031_data_check_refuses_before_any_ddl(alembic_config: Config, engine: Engine) -> None:
    """Design §7 "数据核对": at 0030 a RETIRED row without an end — the upgrade fails naming its
    id, sends no DDL (no CHECK, only the old trigger), stays at 0030; with the end set by hand
    (0013 lets it) the upgrade goes through."""
    _start(alembic_config, engine)
    seed = None
    try:
        seed = _seed(engine)
        _at_0030(alembic_config, engine)
        with engine.begin() as connection:
            retired = _draft(connection, seed)
            _publish(connection, seed, retired, _T0)
        with engine.begin() as connection:
            _set(connection, retired, "status = 'RETIRED'")
        before = _structure(engine)

        with _statements() as statements:
            with pytest.raises(RuntimeError) as raised:
                command.upgrade(alembic_config, _REVISION)

        message = str(raised.value)
        assert f"id {retired};" in message
        assert _CHECK in message
        assert _ddl(statements) == []
        assert _version(engine) == _DOWN_REVISION
        assert _structure(engine) == before
        assert _checks_named(engine) == []
        assert _NEW not in _price_triggers(engine)

        with engine.begin() as connection:
            _set(connection, retired, "effective_to = :end", end=_T0 + _DAY)
        command.upgrade(alembic_config, _REVISION)
        assert _version(engine) == _REVISION
        _assert_head_structure(engine)
    finally:
        _finish(alembic_config, engine, seed)


_OTHER_TABLE = "tmp_provider_price_backstop_shape"


@needs_mysql
@pytest.mark.parametrize("wrong", [*_HAND_MADE_CHECKS, "not enforced", "on another table"], ids=str)
def test_0031_upgrade_refuses_a_hand_made_check(
    alembic_config: Config, engine: Engine, wrong: str
) -> None:
    """Design §7 "形状核对" ① ② ③ ⑧ ⑨: at 0030 a CHECK of our name made by hand — weaker, the
    terms reversed, one more escape, NOT ENFORCED, or on another table of the same schema. The
    upgrade fails naming the CHECK and sends no DDL; information_schema is as before. For ① ② ③
    MySQL keeps exactly the text the unit cases refuse."""
    _start(alembic_config, engine)
    made: list[str] = []
    try:
        _at_0030(alembic_config, engine)
        if wrong == "on another table":
            _execute(
                engine,
                f"CREATE TABLE {_OTHER_TABLE} (status VARCHAR(16) NOT NULL,"
                f" effective_to DATETIME NULL, CONSTRAINT {_CHECK} CHECK ({RETIRED_END_CHECK}))",
            )
            made.append(f"DROP TABLE {_OTHER_TABLE}")
        else:
            condition = _HAND_MADE_CHECKS[wrong][0] if wrong in _HAND_MADE_CHECKS else None
            enforced = "NOT ENFORCED" if wrong == "not enforced" else "ENFORCED"
            _execute(
                engine,
                f"ALTER TABLE {_VERSIONS} ADD CONSTRAINT {_CHECK}"
                f" CHECK ({condition or RETIRED_END_CHECK}) {enforced}",
            )
            made.append(f"ALTER TABLE {_VERSIONS} DROP CHECK {_CHECK}")
        if wrong in _HAND_MADE_CHECKS:
            assert _checks_named(engine)[0][3] == _HAND_MADE_CHECKS[wrong][1]
        before = _structure(engine)

        with _statements() as statements:
            with pytest.raises(RuntimeError) as raised:
                command.upgrade(alembic_config, _REVISION)

        assert f"check {_CHECK}" in str(raised.value)
        assert "No DDL was issued" in str(raised.value)
        assert _ddl(statements) == []
        assert _version(engine) == _DOWN_REVISION
        assert _structure(engine) == before
    finally:
        for statement in made:
            _execute(engine, statement)
        _finish(alembic_config, engine, None)


def _hand_made_new_trigger(wrong: str) -> str:
    """Design §7 "形状核对" ④ ⑤ ⑥: the new trigger's statement, one check short, one SIGNAL more,
    or one space more inside a literal."""
    statement = _load()._TRIGGERS[_NEW]
    if wrong == "one check short":
        block = (
            "    IF OLD.status = 'DRAFT' AND NEW.status = 'PUBLISHED'"
            " AND NEW.effective_to IS NOT NULL THEN\n"
            "        SIGNAL SQLSTATE '45000'\n"
            "            SET MESSAGE_TEXT ="
            " 'a provider price version is published without an end';\n"
            "    END IF;\n"
        )
        assert statement.count(block) == 1
        return statement.replace(block, "")
    if wrong == "one signal more":
        end = statement.rindex("END")
        return (
            statement[:end] + "    IF NEW.id IS NULL THEN\n"
            "        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'one more';\n"
            "    END IF;\n" + statement[end:]
        )
    assert statement.count("'provider price periods overlap'") == 1
    return statement.replace(
        "'provider price periods overlap'", "'provider price  periods overlap'"
    )


@needs_mysql
@pytest.mark.parametrize("wrong", ["one check short", "one signal more", "one space more"])
def test_0031_upgrade_refuses_a_hand_made_new_trigger(
    alembic_config: Config, engine: Engine, wrong: str
) -> None:
    _start(alembic_config, engine)
    made = False
    try:
        _at_0030(alembic_config, engine)
        _execute(engine, _hand_made_new_trigger(wrong))
        made = True
        before = _structure(engine)

        with _statements() as statements:
            with pytest.raises(RuntimeError) as raised:
                command.upgrade(alembic_config, _REVISION)

        assert f"trigger {_NEW}" in str(raised.value)
        assert _ddl(statements) == []
        assert _version(engine) == _DOWN_REVISION
        assert _structure(engine) == before
    finally:
        if made:
            _execute(engine, f"DROP TRIGGER {_NEW}")
        _finish(alembic_config, engine, None)


@needs_mysql
def test_0031_downgrade_refuses_a_hand_made_old_trigger(
    alembic_config: Config, engine: Engine
) -> None:
    """Design §7 "形状核对" ⑦: at head, the old name made by hand with another body — the
    downgrade fails naming it and sends no DDL; the version stays 0031."""
    _start(alembic_config, engine)
    made = False
    try:
        old = _load()._TRIGGERS_0013[_OLD]
        _execute(engine, old.replace("periods overlap", "periods  overlap"))
        made = True
        before = _structure(engine)

        with _statements() as statements:
            with pytest.raises(RuntimeError) as raised:
                command.downgrade(alembic_config, _DOWN_REVISION)

        assert f"trigger {_OLD}" in str(raised.value)
        assert _ddl(statements) == []
        assert _version(engine) == _REVISION
        assert _structure(engine) == before
    finally:
        if made:
            _execute(engine, f"DROP TRIGGER {_OLD}")
        _finish(alembic_config, engine, None)


@needs_mysql
@pytest.mark.parametrize("direction", ["upgrade", "downgrade"])
def test_0031_precheck_refuses_before_any_ddl(
    alembic_config: Config, engine: Engine, direction: str
) -> None:
    """Binlog on and the switch off: both directions fail before any DDL with this migration's
    message; nothing changes."""
    _start(alembic_config, engine)
    try:
        if direction == "upgrade":
            _at_0030(alembic_config, engine)
        version = _version(engine)
        before = _structure(engine)
        run = command.upgrade if direction == "upgrade" else command.downgrade
        target = _REVISION if direction == "upgrade" else _DOWN_REVISION

        with _binlog_without_the_switch(), _statements() as statements:
            with pytest.raises(RuntimeError) as raised:
                run(alembic_config, target)

        assert str(raised.value) == _load()._PREREQUISITE_ERROR
        assert _ddl(statements) == []
        assert _version(engine) == version
        assert _structure(engine) == before
    finally:
        _finish(alembic_config, engine, None)


@needs_mysql
def test_0031_downgrade_needs_no_data_precondition(alembic_config: Config, engine: Engine) -> None:
    """Design §7 "降级不需要数据前提": with versions of every kind the downgrade goes through and
    leaves every row as it was; upgrading again does too."""
    _start(alembic_config, engine)
    seed = None
    try:
        seed = _seed(engine)
        _every_kind(engine, seed)
        before = _rows(engine)
        assert len(before[_VERSIONS]) == 6

        command.downgrade(alembic_config, _DOWN_REVISION)
        assert _version(engine) == _DOWN_REVISION
        assert _rows(engine) == before

        command.upgrade(alembic_config, _REVISION)
        assert _rows(engine) == before
    finally:
        _finish(alembic_config, engine, seed)

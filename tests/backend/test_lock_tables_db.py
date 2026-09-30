"""The two one-row lock tables reject UPDATE, on a real MySQL (design gate #200 v1,
AIH-TASK-045, migration 0016).

`pricing_rule_locks` (0014) and `fx_rate_locks` (0015) hold exactly the row `id = 1`: publishing
and retiring lock it exclusively, billing shares it, and the version tables' BEFORE UPDATE
triggers lock it `FOR UPDATE` before they look for overlapping periods. 0014 / 0015 refused a
second INSERT and every DELETE; 0016 adds an unconditional BEFORE UPDATE, so the row cannot be
moved away and the lock taken on `id = 1` never silently finds nothing.

Five rows of design §7, both tables each time (parametrised), each refusal paired with a case
that proves the database does not refuse more than it should:

- **normal path**: `UPDATE <table> SET id = 2 WHERE id = 1` is refused;
- **boundary values**: `SET id = 1` (no change) and `SET id = id + 1` (no WHERE) are refused;
- **no false refusal**: `FOR UPDATE` in one connection, then `FOR SHARE` in another after the
  first commits, both return `[1]`;
- **other ways to write**: `INSERT … ON DUPLICATE KEY UPDATE id = 2` and `REPLACE` are refused by
  the existing BEFORE INSERT;
- **migration**: downgrade to 0015 drops only the two triggers, upgrade to 0016 adds only them;
  tables, rows and every other trigger stay.

Every refusal is asserted with its MySQL error number, message and SQLSTATE, and every case
ends with each lock table still holding exactly `[1]`. Each write runs in a transaction that is
rolled back.

Needs `BILLING_TEST_DATABASE_URL` pointing at a database that may be wiped; skips without it —
**a skip is not a pass**, CI sets it.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from alembic.config import Config
from sqlalchemy import Engine, create_engine, inspect, text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import DBAPIError

from alembic import command

TEST_DATABASE_URL = os.environ.get("BILLING_TEST_DATABASE_URL", "")

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="BILLING_TEST_DATABASE_URL is not set; the lock table triggers need a real MySQL",
)

LOCK_TABLES = ("pricing_rule_locks", "fx_rate_locks")

_BEFORE_0016 = "0015_fx_rates"
_REVISION_0016 = "0016_lock_tables_no_update"

# Trigger → (table, timing, event): what 0016 adds, no more, no fewer.
_TRIGGERS_0016 = {
    "trg_pricing_rule_locks_before_update": ("pricing_rule_locks", "BEFORE", "UPDATE"),
    "trg_fx_rate_locks_before_update": ("fx_rate_locks", "BEFORE", "UPDATE"),
}

# SIGNAL with a user-defined SQLSTATE is MySQL error 1644 (ER_SIGNAL_EXCEPTION). The driver does
# not bring the SQLSTATE back; GET DIAGNOSTICS reads it.
_ER_SIGNAL_EXCEPTION = 1644
_SQLSTATE = "45000"
_GET_SQLSTATE = "GET DIAGNOSTICS CONDITION 1 @lock_sqlstate = RETURNED_SQLSTATE"
# CAST to CHAR: a user variable's character set follows its source; the driver may hand it back
# as bytes.
_READ_SQLSTATE = text("SELECT CAST(@lock_sqlstate AS CHAR)")
_TRIGGERS_QUERY = text(
    "SELECT TRIGGER_NAME, EVENT_OBJECT_TABLE, ACTION_TIMING, EVENT_MANIPULATION"
    " FROM information_schema.TRIGGERS WHERE TRIGGER_SCHEMA = DATABASE()"
)


def _message(table: str) -> str:
    """The text of the same table's INSERT / DELETE triggers; 0016 reuses it."""
    return f"{table} holds exactly one row"


def _ids(connection: Connection, table: str) -> list[int]:
    return list(connection.execute(text(f"SELECT id FROM {table} ORDER BY id")).scalars())


def _locked(connection: Connection, table: str, mode: str) -> list[int]:
    statement = text(f"SELECT id FROM {table} WHERE id = 1 {mode}")
    return list(connection.execute(statement).scalars())


def _refused(connection: Connection, statement: str) -> tuple[int, str, str]:
    """Run `statement`; return the MySQL error number, message and SQLSTATE it was refused with.

    ⚠️ Not in a savepoint: rolling back to one clears the diagnostics area, and GET DIAGNOSTICS
    would read nothing. A failed statement in MySQL undoes only itself; the outer transaction
    goes on (and is rolled back by the caller).
    """
    with pytest.raises(DBAPIError) as raised:
        connection.exec_driver_sql(statement)
    errno, message = raised.value.orig.args[:2]
    connection.exec_driver_sql(_GET_SQLSTATE)
    sqlstate = connection.execute(_READ_SQLSTATE).scalar_one()
    return int(errno), str(message), sqlstate


def _fresh_lock_ids() -> dict[str, list[int]]:
    """A fresh engine each time: nothing pooled holds a metadata lock across the DDL."""
    engine = create_engine(TEST_DATABASE_URL)
    try:
        with engine.connect() as connection:
            return {table: _ids(connection, table) for table in LOCK_TABLES}
    finally:
        engine.dispose()


def _schema() -> tuple[set[str], dict[str, tuple[str, ...]]]:
    """Tables and triggers (name → table, timing, event); a fresh engine, as above."""
    engine = create_engine(TEST_DATABASE_URL)
    try:
        tables = set(inspect(engine).get_table_names())
        with engine.connect() as connection:
            rows = connection.execute(_TRIGGERS_QUERY).all()
        return tables, {row[0]: tuple(row[1:]) for row in rows}
    finally:
        engine.dispose()


# --- fixtures ---------------------------------------------------------------------


@pytest.fixture
def alembic_config(monkeypatch) -> Iterator[Config]:
    """The lock tables and their triggers are the ones migrations 0014–0016 build."""
    from app.core.config import get_settings

    monkeypatch.setenv("BILLING_DATABASE_URL", TEST_DATABASE_URL)
    get_settings.cache_clear()
    config = Config("alembic.ini")
    command.upgrade(config, "head")
    yield config
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _each_lock_table_still_holds_one_row(alembic_config: Config) -> Iterator[None]:
    """Every case ends with both lock tables holding exactly `[1]`, whatever it did."""
    yield
    assert _fresh_lock_ids() == dict.fromkeys(LOCK_TABLES, [1])


@pytest.fixture
def engine(alembic_config: Config) -> Iterator[Engine]:
    engine = create_engine(TEST_DATABASE_URL)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def connection(engine: Engine) -> Iterator[Connection]:
    """⚠️ The database is shared: everything a case does is rolled back."""
    with engine.connect() as connection:
        transaction = connection.begin()
        try:
            yield connection
        finally:
            transaction.rollback()


parametrise_tables = pytest.mark.parametrize("table", LOCK_TABLES)


# --- normal path (design §7 "正常路径") ------------------------------------------------------


@parametrise_tables
def test_moving_the_lock_row_is_refused(connection: Connection, table: str) -> None:
    """The gap #199 found: `SET id = 2` would move the row away and the triggers' `WHERE id = 1
    FOR UPDATE` would lock nothing. The counterpart: the row is still there to lock."""
    refused = _refused(connection, f"UPDATE {table} SET id = 2 WHERE id = 1")

    assert refused == (_ER_SIGNAL_EXCEPTION, _message(table), _SQLSTATE)
    assert _ids(connection, table) == [1]
    assert _locked(connection, table, "FOR UPDATE") == [1]


# --- boundary values (design §7 "边界值") ----------------------------------------------------


@parametrise_tables
@pytest.mark.parametrize(
    "assignment",
    ["SET id = 1", "SET id = id + 1"],
    ids=["no-change", "no-where"],
)
def test_every_update_is_refused(connection: Connection, table: str, assignment: str) -> None:
    """Unconditional: an UPDATE that changes nothing is refused as well, and so is one with no
    WHERE. The counterpart: the row is still there to lock, exclusively and shared."""
    refused = _refused(connection, f"UPDATE {table} {assignment}")

    assert refused == (_ER_SIGNAL_EXCEPTION, _message(table), _SQLSTATE)
    assert _ids(connection, table) == [1]
    assert _locked(connection, table, "FOR UPDATE") == [1]
    assert _locked(connection, table, "FOR SHARE") == [1]


# --- no false refusal (design §7 "不误杀") ---------------------------------------------------


@parametrise_tables
def test_locking_reads_are_not_updates(engine: Engine, table: str) -> None:
    """`FOR UPDATE` / `FOR SHARE` are not UPDATE statements and do not fire the trigger: one
    connection locks the row exclusively and commits (it wrote nothing), then another takes it
    shared — both get `[1]` and no error."""
    with engine.connect() as first:
        with first.begin():
            assert _locked(first, table, "FOR UPDATE") == [1]

    with engine.connect() as second:
        transaction = second.begin()
        try:
            # Were the row still held, this would fail fast instead of hanging.
            second.execute(text("SET SESSION innodb_lock_wait_timeout = 1"))
            assert _locked(second, table, "FOR SHARE") == [1]
        finally:
            transaction.rollback()


# --- other ways to write (design §7 "其他写法") -----------------------------------------------


@parametrise_tables
@pytest.mark.parametrize(
    "statement",
    [
        "INSERT INTO {table} (id) VALUES (1) ON DUPLICATE KEY UPDATE id = 2",
        "REPLACE INTO {table} (id) VALUES (1)",
    ],
    ids=["on-duplicate-key-update", "replace"],
)
def test_upserts_are_refused_by_the_insert_trigger(
    connection: Connection, table: str, statement: str
) -> None:
    """Both go through BEFORE INSERT first; with the one row there it refuses them with the same
    message. The counterpart: the row is still there to lock."""
    refused = _refused(connection, statement.format(table=table))

    assert refused == (_ER_SIGNAL_EXCEPTION, _message(table), _SQLSTATE)
    assert _ids(connection, table) == [1]
    assert _locked(connection, table, "FOR UPDATE") == [1]


# --- migration (design §7 "迁移") ------------------------------------------------------------


@parametrise_tables
def test_upgrade_and_downgrade_add_and_drop_only_the_two_triggers(
    alembic_config: Config, table: str
) -> None:
    """From 0015, upgrading to 0016 adds exactly the two BEFORE UPDATE triggers and downgrading
    drops exactly them; tables, the lock rows and every other trigger stay. Without 0016 the
    gap is back (an UPDATE goes through, rolled back here); with it, it is refused again."""
    update = f"UPDATE {table} SET id = 2 WHERE id = 1"
    try:
        command.downgrade(alembic_config, _BEFORE_0016)
        tables, triggers = _schema()
        assert not set(_TRIGGERS_0016) & set(triggers)
        assert _fresh_lock_ids() == dict.fromkeys(LOCK_TABLES, [1])
        engine = create_engine(TEST_DATABASE_URL)
        try:
            with engine.connect() as connection:
                with connection.begin() as transaction:
                    connection.exec_driver_sql(update)
                    assert _ids(connection, table) == [2]
                    transaction.rollback()
        finally:
            engine.dispose()
        assert _fresh_lock_ids() == dict.fromkeys(LOCK_TABLES, [1])

        command.upgrade(alembic_config, _REVISION_0016)
        assert _schema() == (tables, triggers | _TRIGGERS_0016)
        assert _fresh_lock_ids() == dict.fromkeys(LOCK_TABLES, [1])
        engine = create_engine(TEST_DATABASE_URL)
        try:
            with engine.connect() as connection:
                with connection.begin() as transaction:
                    refused = _refused(connection, update)
                    assert refused == (_ER_SIGNAL_EXCEPTION, _message(table), _SQLSTATE)
                    transaction.rollback()
        finally:
            engine.dispose()

        command.downgrade(alembic_config, _BEFORE_0016)
        assert _schema() == (tables, triggers)
        assert _fresh_lock_ids() == dict.fromkeys(LOCK_TABLES, [1])
    finally:
        command.upgrade(alembic_config, "head")

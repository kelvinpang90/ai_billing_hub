"""The password version checks as the last line of defence (design gate #369 v6 §7 "数据库兜底").

A write that bypasses the services and sets `password_version = -1` is refused by the database,
once on `users` and once on `refresh_tokens`, each paired with the boundary value 0 that passes
(so the refusal is the check, not something else about the row). Rows are written with plain SQL
inside a transaction that is rolled back.

Both halves run the same cases: SQLite builds its tables from the models with `create_all` (what
the unit tests use), MySQL from the migrations (`alembic upgrade head`, so the checks are 0024's).
The MySQL half needs `BILLING_TEST_DATABASE_URL` pointing at a database that may be wiped and
skips without it — **a skip is not a pass**, CI sets it.
"""

from __future__ import annotations

import datetime as dt
import os
from collections.abc import Callable, Iterator

import pytest
from alembic.config import Config
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import DBAPIError

from alembic import command
from app.models.base import Base

TEST_DATABASE_URL = os.environ.get("BILLING_TEST_DATABASE_URL", "")
TEST_EMAIL_DOMAIN = "@session-revocation-test.example.com"
EMAIL = f"session-revocation-db{TEST_EMAIL_DOMAIN}"
WRITTEN_AT = dt.datetime(2026, 10, 19, 3, 4, 5)

USERS_CHECK = "ck_users_password_version"
REFRESH_TOKENS_CHECK = "ck_refresh_tokens_password_version"

# MySQL: "Check constraint '<name>' is violated."
_ER_CHECK_CONSTRAINT_VIOLATED = 3819

_INSERT_USER = text(
    "INSERT INTO users (email, password_hash, role, status, failed_login_count, lockout_level,"
    " password_version, created_at, updated_at) VALUES (:email, 'not-a-real-hash', 'ADMIN',"
    " 'ACTIVE', 0, 0, :version, :now, :now)"
)
_INSERT_REFRESH_TOKEN = text(
    "INSERT INTO refresh_tokens (user_id, family_id, token_hash, issued_at, expires_at,"
    " password_version) VALUES (:user_id, :family_id, :token_hash, :now, :expires_at, :version)"
)
_USER_ID = text("SELECT id FROM users WHERE email = :email")
_SET_USER_VERSION = text("UPDATE users SET password_version = :version WHERE id = :id")
_SET_TOKEN_VERSION = text("UPDATE refresh_tokens SET password_version = :version WHERE id = :id")
_TOKEN_ID = text("SELECT id FROM refresh_tokens WHERE token_hash = :token_hash")
_USER_VERSION = text("SELECT password_version FROM users WHERE id = :id")
_TOKEN_VERSION = text("SELECT password_version FROM refresh_tokens WHERE id = :id")


# --- fixtures ---------------------------------------------------------------------


@pytest.fixture(params=["sqlite", "mysql"])
def connection(request, monkeypatch) -> Iterator[Connection]:
    """One connection inside a transaction that is rolled back."""
    if request.param == "mysql":
        engine = _mysql_engine(monkeypatch)
    else:
        engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(engine)
    try:
        with engine.connect() as opened:
            transaction = opened.begin()
            try:
                yield opened
            finally:
                transaction.rollback()
    finally:
        engine.dispose()


def _mysql_engine(monkeypatch) -> Engine:
    if not TEST_DATABASE_URL:
        pytest.skip("BILLING_TEST_DATABASE_URL is not set; the MySQL half needs a real MySQL")
    # Tables and checks are the ones the migrations build (0024 adds these two).
    from app.core.config import get_settings

    monkeypatch.setenv("BILLING_DATABASE_URL", TEST_DATABASE_URL)
    get_settings.cache_clear()
    command.upgrade(Config("alembic.ini"), "head")
    get_settings.cache_clear()
    return create_engine(TEST_DATABASE_URL)


# --- helpers ----------------------------------------------------------------------


def _insert_user(connection: Connection, version: int, email: str = EMAIL) -> int:
    connection.execute(_INSERT_USER, {"email": email, "version": version, "now": WRITTEN_AT})
    return int(connection.execute(_USER_ID, {"email": email}).scalar_one())


def _insert_token(connection: Connection, user_id: int, version: int, token_hash: str) -> int:
    connection.execute(
        _INSERT_REFRESH_TOKEN,
        {
            "user_id": user_id,
            "family_id": "00000000-0000-0000-0000-000000000086",
            "token_hash": token_hash,
            "now": WRITTEN_AT,
            "expires_at": WRITTEN_AT + dt.timedelta(days=30),
            "version": version,
        },
    )
    return int(connection.execute(_TOKEN_ID, {"token_hash": token_hash}).scalar_one())


def refuses(connection: Connection, write: Callable[[], object], check: str) -> None:
    """`write` fails and the error names `check`.

    No savepoint: a CHECK failure undoes only its own statement on SQLite and on InnoDB, so the
    transaction goes on (and pysqlite's SAVEPOINT handling is not reliable)."""
    with pytest.raises(DBAPIError) as raised:
        write()
    # Both SQLite ("CHECK constraint failed: <name>") and MySQL name the constraint.
    assert check in str(raised.value.orig)
    if connection.dialect.name == "mysql":
        assert int(raised.value.orig.args[0]) == _ER_CHECK_CONSTRAINT_VIOLATED


# --- users ------------------------------------------------------------------------


def test_users_refuse_a_negative_password_version(connection: Connection) -> None:
    refuses(connection, lambda: _insert_user(connection, -1), USERS_CHECK)

    # The boundary passes, on insert and on update; -1 by update is refused too.
    user_id = _insert_user(connection, 0)
    assert connection.execute(_USER_VERSION, {"id": user_id}).scalar_one() == 0
    refuses(
        connection,
        lambda: connection.execute(_SET_USER_VERSION, {"version": -1, "id": user_id}),
        USERS_CHECK,
    )
    assert connection.execute(_USER_VERSION, {"id": user_id}).scalar_one() == 0


# --- refresh_tokens ---------------------------------------------------------------


def test_refresh_tokens_refuse_a_negative_password_version(connection: Connection) -> None:
    user_id = _insert_user(connection, 0)

    refuses(
        connection,
        lambda: _insert_token(connection, user_id, -1, "a" * 64),
        REFRESH_TOKENS_CHECK,
    )

    token_id = _insert_token(connection, user_id, 0, "b" * 64)
    assert connection.execute(_TOKEN_VERSION, {"id": token_id}).scalar_one() == 0
    refuses(
        connection,
        lambda: connection.execute(_SET_TOKEN_VERSION, {"version": -1, "id": token_id}),
        REFRESH_TOKENS_CHECK,
    )
    assert connection.execute(_TOKEN_VERSION, {"id": token_id}).scalar_one() == 0

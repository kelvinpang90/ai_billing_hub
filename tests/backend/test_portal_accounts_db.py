"""Portal account constraints as the last line of defence (design gate #368 v10 §7 "数据库兜底").

Five writes that bypass the services are refused by the database: an ADMIN with a `tenant_id`;
two users on the same `tenant_id`; an `INVITED` ADMIN; a user whose status is `OTHER`; a token
whose purpose is `OTHER`. Each is paired with a row that differs only in the refused value and
passes, so the refusal is that constraint and not something else about the row. Rows are written
with plain SQL inside a transaction that is rolled back.

One write is **allowed on purpose**: a CUSTOMER without a `tenant_id`. "A CUSTOMER belongs to a
tenant" is deliberately not a database constraint (design §9 alternative F): the only creation
path always writes `tenant_id`, `require_customer` refuses a CUSTOMER without one, and the existing
tests that build tenant-less CUSTOMERs to assert 403 keep working.

Both halves run the same cases: SQLite builds its tables from the models with `create_all` (what
the unit tests use), MySQL from the migrations (`alembic upgrade head`, so the constraints are
0025's). The MySQL half needs `BILLING_TEST_DATABASE_URL` pointing at a database that may be wiped
and skips without it — **a skip is not a pass**, CI sets it.
"""

from __future__ import annotations

import datetime as dt
import os
import uuid
from collections.abc import Callable, Iterator

import pytest
from alembic.config import Config
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import DBAPIError, IntegrityError

from alembic import command
from app.models.base import Base

TEST_DATABASE_URL = os.environ.get("BILLING_TEST_DATABASE_URL", "")
TEST_EMAIL_DOMAIN = "@portal-accounts-db-test.example.com"
WRITTEN_AT = dt.datetime(2026, 10, 19, 3, 4, 5)

ADMIN_NO_TENANT = "ck_users_admin_no_tenant"
USERS_STATUS = "ck_users_status"
INVITED_CUSTOMER = "ck_users_invited_customer"
TOKENS_PURPOSE = "ck_password_reset_tokens_purpose"
UNIQUE_TENANT = "uq_users_tenant"

# MySQL: "Check constraint '<name>' is violated." / "Duplicate entry '…' for key '<table>.<name>'".
_ER_CHECK_CONSTRAINT_VIOLATED = 3819
_ER_DUP_ENTRY = 1062

_INSERT_TENANT = text(
    "INSERT INTO tenants (public_id, company_name, email, billing_status, billing_mode,"
    " account_status, status_version, created_at, updated_at) VALUES (:public_id,"
    " 'Portal Accounts DB Test', :email, 'SUSPENDED', 'PREPAID', 'ENABLED', 0, :now, :now)"
)
_TENANT_ID = text("SELECT id FROM tenants WHERE public_id = :public_id")
_INSERT_USER = text(
    "INSERT INTO users (email, password_hash, role, status, tenant_id, failed_login_count,"
    " lockout_level, password_version, created_at, updated_at) VALUES (:email,"
    " 'not-a-real-hash', :role, :status, :tenant_id, 0, 0, 0, :now, :now)"
)
_USER_ID = text("SELECT id FROM users WHERE email = :email")
_USER_ROW = text("SELECT role, status, tenant_id FROM users WHERE id = :id")
_SET_USER_TENANT = text("UPDATE users SET tenant_id = :tenant_id WHERE id = :id")
_SET_USER_STATUS = text("UPDATE users SET status = :status WHERE id = :id")
_INSERT_TOKEN = text(
    "INSERT INTO password_reset_tokens (user_id, token_hash, expires_at, created_at, purpose)"
    " VALUES (:user_id, :token_hash, :expires_at, :now, :purpose)"
)
# Without `purpose`: what the code before 0025 writes.
_INSERT_TOKEN_WITHOUT_PURPOSE = text(
    "INSERT INTO password_reset_tokens (user_id, token_hash, expires_at, created_at)"
    " VALUES (:user_id, :token_hash, :expires_at, :now)"
)
_TOKEN_PURPOSE = text("SELECT purpose FROM password_reset_tokens WHERE token_hash = :token_hash")
_SET_TOKEN_PURPOSE = text(
    "UPDATE password_reset_tokens SET purpose = :purpose WHERE token_hash = :token_hash"
)


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
    # Tables and constraints are the ones the migrations build (0025 adds these).
    from app.core.config import get_settings

    monkeypatch.setenv("BILLING_DATABASE_URL", TEST_DATABASE_URL)
    get_settings.cache_clear()
    command.upgrade(Config("alembic.ini"), "head")
    get_settings.cache_clear()
    return create_engine(TEST_DATABASE_URL)


# --- helpers ----------------------------------------------------------------------


def _insert_tenant(connection: Connection) -> int:
    public_id = str(uuid.uuid4())
    connection.execute(
        _INSERT_TENANT,
        {"public_id": public_id, "email": f"contact{TEST_EMAIL_DOMAIN}", "now": WRITTEN_AT},
    )
    return int(connection.execute(_TENANT_ID, {"public_id": public_id}).scalar_one())


def _insert_user(
    connection: Connection,
    name: str,
    *,
    role: str = "ADMIN",
    status: str = "ACTIVE",
    tenant_id: int | None = None,
) -> int:
    email = f"{name}{TEST_EMAIL_DOMAIN}"
    connection.execute(
        _INSERT_USER,
        {"email": email, "role": role, "status": status, "tenant_id": tenant_id, "now": WRITTEN_AT},
    )
    return int(connection.execute(_USER_ID, {"email": email}).scalar_one())


def _insert_token(connection: Connection, user_id: int, token_hash: str, purpose: str) -> None:
    connection.execute(
        _INSERT_TOKEN,
        {
            "user_id": user_id,
            "token_hash": token_hash,
            "expires_at": WRITTEN_AT + dt.timedelta(hours=72),
            "now": WRITTEN_AT,
            "purpose": purpose,
        },
    )


def _user_row(connection: Connection, user_id: int) -> tuple[str, str, int | None]:
    role, status, tenant_id = connection.execute(_USER_ROW, {"id": user_id}).one()
    return str(role), str(status), None if tenant_id is None else int(tenant_id)


def _no_user(connection: Connection, name: str) -> bool:
    return connection.execute(_USER_ID, {"email": f"{name}{TEST_EMAIL_DOMAIN}"}).first() is None


def refuses(connection: Connection, write: Callable[[], object], check: str) -> None:
    """`write` fails on the CHECK named `check`.

    No savepoint: a CHECK failure undoes only its own statement on SQLite and on InnoDB, so the
    transaction goes on (and pysqlite's SAVEPOINT handling is not reliable)."""
    with pytest.raises(DBAPIError) as raised:
        write()
    # Both SQLite ("CHECK constraint failed: <name>") and MySQL name the constraint.
    assert check in str(raised.value.orig)
    if connection.dialect.name == "mysql":
        assert int(raised.value.orig.args[0]) == _ER_CHECK_CONSTRAINT_VIOLATED


def refuses_a_second_tenant_user(connection: Connection, write: Callable[[], object]) -> None:
    """`write` fails on `uq_users_tenant`. Same no-savepoint reasoning as `refuses`."""
    with pytest.raises(IntegrityError) as raised:
        write()
    message = str(raised.value.orig)
    if connection.dialect.name == "mysql":
        assert int(raised.value.orig.args[0]) == _ER_DUP_ENTRY
        assert UNIQUE_TENANT in message
    else:
        # SQLite names the column, not the constraint.
        assert "UNIQUE" in message
        assert "users.tenant_id" in message


# --- users: an ADMIN belongs to no tenant -------------------------------------------


def test_an_admin_with_a_tenant_is_refused(connection: Connection) -> None:
    tenant_id = _insert_tenant(connection)

    refuses(
        connection,
        lambda: _insert_user(connection, "admin-with-tenant", tenant_id=tenant_id),
        ADMIN_NO_TENANT,
    )
    assert _no_user(connection, "admin-with-tenant")

    # The same row without the tenant passes; giving it the tenant later is refused too.
    admin_id = _insert_user(connection, "admin")
    refuses(
        connection,
        lambda: connection.execute(_SET_USER_TENANT, {"tenant_id": tenant_id, "id": admin_id}),
        ADMIN_NO_TENANT,
    )
    assert _user_row(connection, admin_id) == ("ADMIN", "ACTIVE", None)
    # And a CUSTOMER on that tenant passes.
    customer_id = _insert_user(connection, "customer", role="CUSTOMER", tenant_id=tenant_id)
    assert _user_row(connection, customer_id) == ("CUSTOMER", "ACTIVE", tenant_id)


# --- users: one portal account per tenant ------------------------------------------


def test_a_second_user_on_the_same_tenant_is_refused(connection: Connection) -> None:
    tenant_id = _insert_tenant(connection)
    other_tenant_id = _insert_tenant(connection)
    first = _insert_user(connection, "first", role="CUSTOMER", tenant_id=tenant_id)

    refuses_a_second_tenant_user(
        connection,
        lambda: _insert_user(connection, "second", role="CUSTOMER", tenant_id=tenant_id),
    )
    assert _no_user(connection, "second")

    # The same row on another tenant passes; moving it onto the taken tenant is refused.
    second = _insert_user(connection, "second", role="CUSTOMER", tenant_id=other_tenant_id)
    refuses_a_second_tenant_user(
        connection,
        lambda: connection.execute(_SET_USER_TENANT, {"tenant_id": tenant_id, "id": second}),
    )
    assert _user_row(connection, first) == ("CUSTOMER", "ACTIVE", tenant_id)
    assert _user_row(connection, second) == ("CUSTOMER", "ACTIVE", other_tenant_id)

    # NULL is not a value: any number of ADMINs, all without a tenant (design §2).
    for name in ("admin-1", "admin-2", "admin-3"):
        assert _user_row(connection, _insert_user(connection, name)) == ("ADMIN", "ACTIVE", None)


# --- users: INVITED is a CUSTOMER's state only -------------------------------------


def test_an_invited_admin_is_refused(connection: Connection) -> None:
    refuses(
        connection,
        lambda: _insert_user(connection, "invited-admin", status="INVITED"),
        INVITED_CUSTOMER,
    )
    assert _no_user(connection, "invited-admin")

    admin_id = _insert_user(connection, "admin")
    refuses(
        connection,
        lambda: connection.execute(_SET_USER_STATUS, {"status": "INVITED", "id": admin_id}),
        INVITED_CUSTOMER,
    )
    assert _user_row(connection, admin_id) == ("ADMIN", "ACTIVE", None)

    # The same row as a CUSTOMER passes.
    tenant_id = _insert_tenant(connection)
    customer_id = _insert_user(
        connection, "invited-customer", role="CUSTOMER", status="INVITED", tenant_id=tenant_id
    )
    assert _user_row(connection, customer_id) == ("CUSTOMER", "INVITED", tenant_id)


# --- users: the status is one of the three -----------------------------------------


def test_an_unknown_user_status_is_refused(connection: Connection) -> None:
    refuses(
        connection,
        lambda: _insert_user(connection, "other-status", status="OTHER"),
        USERS_STATUS,
    )
    assert _no_user(connection, "other-status")

    admin_id = _insert_user(connection, "admin", status="DISABLED")
    assert _user_row(connection, admin_id) == ("ADMIN", "DISABLED", None)
    refuses(
        connection,
        lambda: connection.execute(_SET_USER_STATUS, {"status": "OTHER", "id": admin_id}),
        USERS_STATUS,
    )
    assert _user_row(connection, admin_id) == ("ADMIN", "DISABLED", None)


# --- password_reset_tokens: the purpose is one of the two --------------------------


def test_an_unknown_token_purpose_is_refused(connection: Connection) -> None:
    user_id = _insert_user(connection, "token-owner")

    refuses(
        connection,
        lambda: _insert_token(connection, user_id, "a" * 64, "OTHER"),
        TOKENS_PURPOSE,
    )
    assert connection.execute(_TOKEN_PURPOSE, {"token_hash": "a" * 64}).first() is None

    # Both purposes pass; a token written without one is a reset token (the code before 0025).
    _insert_token(connection, user_id, "b" * 64, "INVITATION")
    _insert_token(connection, user_id, "c" * 64, "PASSWORD_RESET")
    connection.execute(
        _INSERT_TOKEN_WITHOUT_PURPOSE,
        {
            "user_id": user_id,
            "token_hash": "d" * 64,
            "expires_at": WRITTEN_AT + dt.timedelta(hours=1),
            "now": WRITTEN_AT,
        },
    )
    purposes = {
        token_hash: connection.execute(_TOKEN_PURPOSE, {"token_hash": token_hash}).scalar_one()
        for token_hash in ("b" * 64, "c" * 64, "d" * 64)
    }
    assert purposes == {
        "b" * 64: "INVITATION",
        "c" * 64: "PASSWORD_RESET",
        "d" * 64: "PASSWORD_RESET",
    }

    refuses(
        connection,
        lambda: connection.execute(
            _SET_TOKEN_PURPOSE, {"purpose": "OTHER", "token_hash": "b" * 64}
        ),
        TOKENS_PURPOSE,
    )
    assert connection.execute(_TOKEN_PURPOSE, {"token_hash": "b" * 64}).scalar_one() == "INVITATION"


# --- deliberately allowed ------------------------------------------------------------


def test_a_customer_without_a_tenant_is_allowed_on_purpose(connection: Connection) -> None:
    """Design §9 alternative F: "a CUSTOMER belongs to a tenant" is **not** a database constraint.

    This is deliberate, not a gap: such a row only comes from writing the database directly (the
    one creation path always sets `tenant_id`), and `require_customer` keeps it out of every
    customer endpoint. Enforcing it here would break the existing tests that build tenant-less
    CUSTOMERs to assert 403. If this case goes red, that decision changed: go back to the design.
    """
    first = _insert_user(connection, "customer-no-tenant-1", role="CUSTOMER")
    second = _insert_user(connection, "customer-no-tenant-2", role="CUSTOMER", status="DISABLED")

    assert _user_row(connection, first) == ("CUSTOMER", "ACTIVE", None)
    assert _user_row(connection, second) == ("CUSTOMER", "DISABLED", None)
    # An ADMIN turned into a CUSTOMER in place (test_admin_customers_api.py does this) passes too.
    admin_id = _insert_user(connection, "admin-demoted")
    connection.execute(text("UPDATE users SET role = 'CUSTOMER' WHERE id = :id"), {"id": admin_id})
    assert _user_row(connection, admin_id) == ("CUSTOMER", "ACTIVE", None)

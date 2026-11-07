"""Customer monthly statements end to end (AIH-TASK-160, gate #565 v6 §2「客户接口」, §6).

Rows of design §7 marked 160 covered here (**客户接口**), plus what the task contract adds:

- the list holds only the caller's own `FINALIZED` statements (another customer's, and the
  caller's own `DRAFT` and `VOID` rows, never show), latest period first, paginated the way
  the admin list is; each item exactly its 5 keys;
- the detail: the 5 keys plus the whole snapshot, nested, its key set exactly that of design
  §2「快照」; no `cutoff_at`, `ledger_high_sequence` or generator;
- another customer's id, a malformed id, an absent id and a non-`FINALIZED` id are all the same
  404 `STATEMENT_NOT_FOUND`, with the same error body;
- an internal metered customer that never had a statement gets an empty page; a customer moved
  from prepaid to internal metering still sees the statements from before;
- a `DISABLED` account reads both as usual;
- an admin token is 403 `CUSTOMER_REQUIRED`, no token 401 `TOKEN_INVALID`, written the way
  test_customer_api.py asserts them;
- no cost-like key anywhere in a response (INV-7).

Ledger rows, events and tax policies are written as test_statement_generate.py does (its
`Statements`); statements by the system's `generate_statement`. `DRAFT` and `VOID` rows have no
writer in V1, so they are inserted directly. The MySQL half needs `BILLING_TEST_DATABASE_URL`;
it skips without it — **a skip is not a pass**, CI sets it. On MySQL, `monthly_statements` and
`tax_policy_versions` are truncated with foreign key checks off before and after every case
(their triggers refuse DELETE); the portal users go before `clean` deletes their tenants.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from decimal import Decimal
from typing import Any

import pytest
from alembic.config import Config
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, delete, update
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from test_statement_generate import (
    DAY,
    DECEMBER,
    FEBRUARY,
    JANUARY,
    SNAPSHOT_KEYS,
    Statements,
)
from test_usage_billing import (
    _SQLITE_LEDGER_TRIGGER,
    T0,
    TEST_DATABASE_URL,
    TEST_EMAIL_DOMAIN,
    Tenancy,
    clean,
    seed_meter_types,
)

from alembic import command
from app.core.config import Settings
from app.core.database import create_session_factory
from app.core.tokens import issue_access_token
from app.main import create_app
from app.models.auth import User, UserRole, UserStatus
from app.models.base import Base
from app.models.statements import MonthlyStatement, StatementStatus
from app.models.tenancy import AccountStatus, BillingMode, Tenant
from app.services.auth import utc_now
from app.services.statements import StatementPeriod, statement_period

STATEMENTS = "/api/v1/customer/statements"
MARCH = statement_period("2027-03")

AMOUNT = "-0.31415926"

# 设计 §2「客户接口」的字段白名单：断言「恰好等于」。
SUMMARY_FIELDS = {
    "statement_id",
    "statement_number",
    "period",
    "closing_balance",
    "finalized_at",
}
DETAIL_FIELDS = SUMMARY_FIELDS | {"snapshot"}
PAGE_FIELDS = {"items", "page", "page_size", "total"}
# 设计 §6：客户侧不出现的顶层键（管理端详情有其中几个）。
ADMIN_ONLY_FIELDS = {
    "customer_id",
    "company_name",
    "status",
    "cutoff_at",
    "ledger_high_sequence",
    "generated_by",
    "generated_by_email",
    "tax_policy_version_id",
}
# 成本类键的片段（INV-7）：响应里任何一层的任何键都不许含它们。
COST_FRAGMENTS = ("cost", "fx", "_rate", "markup", "margin", "profit", "price", "rule")


# --- fixtures ---------------------------------------------------------------------------


def _sqlite() -> Iterator[sessionmaker[Session]]:
    # StaticPool：TestClient 在线程池里跑同步端点，内存库必须所有连接共用一个。
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.exec_driver_sql(_SQLITE_LEDGER_TRIGGER)
    factory = create_session_factory(engine)
    seed_meter_types(factory)
    yield factory
    engine.dispose()


def _wipe(engine: Engine) -> None:
    """Statements and tax policy versions refuse DELETE (triggers) and reference the tenants and
    users `clean` deletes: TRUNCATE them first, with foreign key checks off. The portal users
    reference their tenants (RESTRICT): delete them before `clean` deletes the tenants."""
    with engine.begin() as connection:
        connection.exec_driver_sql("SET FOREIGN_KEY_CHECKS = 0")
        try:
            connection.exec_driver_sql("TRUNCATE TABLE monthly_statements")
            connection.exec_driver_sql("TRUNCATE TABLE tax_policy_versions")
        finally:
            connection.exec_driver_sql("SET FOREIGN_KEY_CHECKS = 1")
    with engine.begin() as connection:
        connection.execute(
            delete(User).where(
                User.role == UserRole.CUSTOMER, User.email.like(f"%{TEST_EMAIL_DOMAIN}")
            )
        )
    clean(engine)


def _mysql(monkeypatch: pytest.MonkeyPatch) -> Iterator[sessionmaker[Session]]:
    if not TEST_DATABASE_URL:
        pytest.skip("BILLING_TEST_DATABASE_URL is not set; the MySQL half needs a real MySQL")
    from app.core.config import get_settings

    monkeypatch.setenv("BILLING_DATABASE_URL", TEST_DATABASE_URL)
    get_settings.cache_clear()
    command.upgrade(Config("alembic.ini"), "head")
    get_settings.cache_clear()
    engine = create_engine(TEST_DATABASE_URL)
    _wipe(engine)
    try:
        yield create_session_factory(engine)
    finally:
        _wipe(engine)
        engine.dispose()


@pytest.fixture(params=["sqlite", "mysql"])
def factory(request, monkeypatch) -> Iterator[sessionmaker[Session]]:
    if request.param == "mysql":
        yield from _mysql(monkeypatch)
        return
    yield from _sqlite()


@pytest.fixture
def books(factory) -> Statements:
    """SQLite and a real MySQL, with a tax policy in effect from before every period."""
    return Statements(factory)


@pytest.fixture
def app(tmp_path, books) -> FastAPI:
    key = tmp_path / "jwt.key"
    key.write_text("a-test-signing-key-that-is-long-enough", encoding="utf-8")
    application = create_app(Settings(jwt_secret_file=str(key), database_url=""))
    application.state.session_factory = books.factory
    return application


@pytest.fixture
def client(app) -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


# --- helpers ----------------------------------------------------------------------------


def token_for(application: FastAPI, user_id: int, role: str) -> dict[str, str]:
    """A signed access token. `role` is only the claim; the server reads the db."""
    token = issue_access_token(
        application.state.settings,
        user_id=user_id,
        role=role,
        session_id=uuid.uuid4().hex,
        now=utc_now(),
    )
    return {"Authorization": f"Bearer {token}"}


def portal(application: FastAPI, books: Statements, tenant: Tenancy) -> dict[str, str]:
    """The headers of a new, active portal account of `tenant`."""
    with books.factory() as session:
        user = User(
            email=f"portal-{uuid.uuid4().hex}{TEST_EMAIL_DOMAIN}",
            password_hash="not-a-real-hash",
            role=UserRole.CUSTOMER,
            status=UserStatus.ACTIVE,
            tenant_id=tenant.id,
            activated_at=T0,
            created_at=T0,
            updated_at=T0,
        )
        session.add(user)
        session.commit()
        return token_for(application, int(user.id), "CUSTOMER")


def error_of(response) -> tuple[int, str]:
    return response.status_code, response.json()["error"]["code"]


def data(response) -> Any:
    assert response.status_code == 200, response.text
    return response.json()["data"]


def without_request_id(response) -> dict[str, Any]:
    return {key: value for key, value in response.json().items() if key != "request_id"}


def set_billing_mode(books: Statements, tenant: Tenancy, mode: BillingMode) -> None:
    with books.factory() as session:
        session.execute(update(Tenant).where(Tenant.id == tenant.id).values(billing_mode=mode))
        session.commit()


def unfinished(
    books: Statements, like: MonthlyStatement, period: StatementPeriod, status: StatementStatus
) -> MonthlyStatement:
    """A `DRAFT` or `VOID` row of `like`'s tenant for `period`. V1 has no writer for them, so it
    is inserted directly, its columns copied from `like`."""
    row = MonthlyStatement(
        public_id=str(uuid.uuid4()),
        tenant_id=like.tenant_id,
        statement_number=f"ST-{period.label.replace('-', '')}-{status.value[:4]}0000",
        period_start=period.start,
        period_end=period.end,
        cutoff_at=period.cutoff,
        status=status,
        ledger_high_sequence=like.ledger_high_sequence,
        snapshot_json={**like.snapshot_json, "period": period.label},
        generated_by=None,
        tax_policy_version_id=like.tax_policy_version_id,
        finalized_at=None,
        created_at=like.created_at,
    )
    with books.factory() as session:
        session.add(row)
        session.commit()
        session.refresh(row)
        session.expunge(row)
    return row


def _keys(value: Any) -> set[str]:
    """Every key at every depth of a JSON value."""
    if isinstance(value, dict):
        return set(value) | {key for child in value.values() for key in _keys(child)}
    if isinstance(value, list):
        return {key for child in value for key in _keys(child)}
    return set()


def assert_no_cost_keys(value: Any) -> None:
    for key in _keys(value):
        for fragment in COST_FRAGMENTS:
            assert fragment not in key.lower(), key


def summary_of(statement: MonthlyStatement) -> dict[str, Any]:
    snapshot = statement.snapshot_json
    return {
        "statement_id": statement.public_id,
        "statement_number": statement.statement_number,
        "period": snapshot["period"],
        "closing_balance": snapshot["closing_balance"],
        "finalized_at": statement.finalized_at.isoformat() if statement.finalized_at else None,
    }


def with_months(
    books: Statements, tenant: Tenancy, *periods: StatementPeriod
) -> list[MonthlyStatement]:
    """One charge in each period, then its statement generated by the system, in order."""
    made = []
    for period in periods:
        books.charge(tenant, period.start + DAY, period.start + DAY, AMOUNT)
        made.append(books.generate(tenant, period).statement)
    return made


# --- the list -----------------------------------------------------------------------------


def test_the_list_holds_only_the_callers_own_finalized_statements(client, app, books) -> None:
    mine, theirs = books.tenant("a"), books.tenant("b", opening=Decimal("500"))
    december, january = with_months(books, mine, DECEMBER, JANUARY)
    with_months(books, theirs, DECEMBER, JANUARY)
    # The caller's own rows that are not FINALIZED never show.
    unfinished(books, january, FEBRUARY, StatementStatus.DRAFT)
    unfinished(books, january, MARCH, StatementStatus.VOID)

    listing = data(client.get(STATEMENTS, headers=portal(app, books, mine)))

    assert set(listing) == PAGE_FIELDS
    assert (listing["page"], listing["page_size"], listing["total"]) == (1, 20, 2)
    assert listing["items"] == [summary_of(january), summary_of(december)]
    for item in listing["items"]:
        assert set(item) == SUMMARY_FIELDS
    assert_no_cost_keys(listing)


def test_the_list_is_latest_period_first_and_paginates(client, app, books) -> None:
    tenant = books.tenant("a")
    december, january, february = with_months(books, tenant, DECEMBER, JANUARY, FEBRUARY)
    headers = portal(app, books, tenant)

    pages = [
        data(client.get(STATEMENTS, params={"page": n, "page_size": 2}, headers=headers))
        for n in (1, 2, 3)
    ]

    assert [(p["page"], p["page_size"], p["total"]) for p in pages] == [
        (1, 2, 3),
        (2, 2, 3),
        (3, 2, 3),
    ]
    assert [[i["statement_id"] for i in p["items"]] for p in pages] == [
        [february.public_id, january.public_id],
        [december.public_id],
        [],
    ]


@pytest.mark.parametrize(
    "params", [{"page": "0"}, {"page": "10001"}, {"page_size": "0"}, {"page_size": "101"}]
)
def test_paging_out_of_range_is_422(client, app, books, params: dict[str, str]) -> None:
    headers = portal(app, books, books.tenant("a"))

    response = client.get(STATEMENTS, params=params, headers=headers)

    assert error_of(response) == (422, "VALIDATION_ERROR")


def test_an_internal_metered_customer_without_statements_gets_an_empty_page(
    client, app, books
) -> None:
    tenant = books.tenant("internal")
    set_billing_mode(books, tenant, BillingMode.INTERNAL_METERED_ONLY)
    # Another customer's statement exists: still nothing for this one.
    with_months(books, books.tenant("b"), DECEMBER)

    listing = data(client.get(STATEMENTS, headers=portal(app, books, tenant)))

    assert listing == {"items": [], "page": 1, "page_size": 20, "total": 0}


def test_a_customer_moved_to_internal_metering_still_sees_its_earlier_statements(
    client, app, books
) -> None:
    tenant = books.tenant("a")
    [december] = with_months(books, tenant, DECEMBER)
    set_billing_mode(books, tenant, BillingMode.INTERNAL_METERED_ONLY)
    headers = portal(app, books, tenant)

    listing = data(client.get(STATEMENTS, headers=headers))
    detail = data(client.get(f"{STATEMENTS}/{december.public_id}", headers=headers))

    assert listing["items"] == [summary_of(december)]
    assert detail["statement_id"] == december.public_id


# --- the detail -------------------------------------------------------------------------


def test_the_detail_is_the_summary_plus_the_whole_snapshot(client, app, books) -> None:
    tenant = books.tenant("a")
    [december] = with_months(books, tenant, DECEMBER)

    detail = data(
        client.get(f"{STATEMENTS}/{december.public_id}", headers=portal(app, books, tenant))
    )

    assert set(detail) == DETAIL_FIELDS
    assert not set(detail) & ADMIN_ONLY_FIELDS
    assert {key: detail[key] for key in SUMMARY_FIELDS} == summary_of(december)
    # The whole snapshot, nested and unchanged; its key set exactly that of design §2.
    assert detail["snapshot"] == december.snapshot_json
    assert set(detail["snapshot"]) == SNAPSHOT_KEYS
    assert set(detail["snapshot"]["tax_notice"]) == {"zh", "en"}
    assert set(detail["snapshot"]["prior_period"]) == {"usage", "rebill", "total"}
    assert_no_cost_keys(detail)


def test_every_id_the_caller_may_not_read_is_the_same_404(client, app, books) -> None:
    mine, theirs = books.tenant("a"), books.tenant("b")
    [own] = with_months(books, mine, DECEMBER)
    [other] = with_months(books, theirs, DECEMBER)
    draft = unfinished(books, own, JANUARY, StatementStatus.DRAFT)
    void = unfinished(books, own, FEBRUARY, StatementStatus.VOID)
    headers = portal(app, books, mine)

    responses = [
        client.get(f"{STATEMENTS}/{statement_id}", headers=headers)
        for statement_id in (
            other.public_id,
            str(uuid.uuid4()),
            own.public_id.upper(),
            own.public_id + "0",
            "not-a-statement",
            draft.public_id,
            void.public_id,
        )
    ]

    bodies = []
    for response in responses:
        assert error_of(response) == (404, "STATEMENT_NOT_FOUND")
        assert response.json()["data"] is None
        bodies.append(without_request_id(response))
    assert all(body == bodies[0] for body in bodies)
    assert other.statement_number not in responses[0].text
    # The caller's own finalized one is there.
    assert data(client.get(f"{STATEMENTS}/{own.public_id}", headers=headers))


# --- account status and authorization -----------------------------------------------------


def test_a_disabled_account_reads_its_statements(client, app, books) -> None:
    tenant = books.tenant("a")
    [december] = with_months(books, tenant, DECEMBER)
    books.world.set_account_status(tenant, AccountStatus.DISABLED)
    headers = portal(app, books, tenant)

    listing = data(client.get(STATEMENTS, headers=headers))
    detail = data(client.get(f"{STATEMENTS}/{december.public_id}", headers=headers))

    assert listing["items"] == [summary_of(december)]
    assert detail["snapshot"] == december.snapshot_json


ROUTES = [STATEMENTS, STATEMENTS + "/{statement_id}"]


@pytest.mark.parametrize("caller", ["admin", "anonymous"])
@pytest.mark.parametrize("path", ROUTES)
def test_admins_and_anonymous_callers_are_refused(
    client, app, books, path: str, caller: str
) -> None:
    """As test_customer_api.py asserts them: admin 403, anonymous 401, no data."""
    tenant = books.tenant("a")
    [december] = with_months(books, tenant, DECEMBER)
    headers = token_for(app, books.world.admin.id, "ADMIN") if caller == "admin" else {}

    response = client.get(path.replace("{statement_id}", december.public_id), headers=headers)

    expected = (403, "CUSTOMER_REQUIRED") if caller == "admin" else (401, "TOKEN_INVALID")
    assert error_of(response) == expected
    assert response.json()["data"] is None
    assert december.public_id not in response.text

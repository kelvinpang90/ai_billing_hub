"""Admin monthly statements end to end (AIH-TASK-159, gate #565 v6 §2「管理端接口」).

Rows of design §7 marked 159 covered here:

- **管理端** — the list filtered by customer and by period, paginated and ordered by period
  then row id, each item exactly its 8 keys with the company name from the snapshot; the
  detail with its 4 more keys and the whole snapshot (key set compared exactly); a 404 for an
  absent or malformed id; the manual catch-up: new statements (the response, the rows and one
  audit each), already there (an empty list, no new audit), prepaid with nothing due (200, an
  empty list), not prepaid 409, without a wallet 409, an unknown customer 404, a failing middle
  period (self-check 2 in the second period) 500 with the first period's statement and audit
  kept and nothing from the second on; anonymous 401 and CUSTOMER 403 on all three, written
  the way test_admin_customers_api.py asserts them; no cost-like key anywhere in a response;
- **审计** — the audit write failing leaves no statement either (same transaction, INV-13).

Ledger rows, events and tax policies are written as test_statement_generate.py does (its
`Statements`); the app's clock is pinned through `app.api.admin_statements.utc_now`.
The MySQL half needs `BILLING_TEST_DATABASE_URL`; it skips without it — **a skip is not a
pass**, CI sets it. On MySQL, `monthly_statements` and `tax_policy_versions` are truncated
with foreign key checks off before and after every case (their triggers refuse DELETE).
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from collections.abc import Iterator
from decimal import Decimal
from typing import Any

import pytest
from alembic.config import Config
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, delete, func, select, update
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from test_statement_generate import (
    DAY,
    DECEMBER,
    FEBRUARY,
    JANUARY,
    SECOND,
    SNAPSHOT_KEYS,
    Statements,
    due,
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
from app.api import admin_statements
from app.core.config import Settings
from app.core.database import create_session_factory
from app.core.tokens import issue_access_token
from app.main import create_app
from app.models.auth import AuditAction, AuditLog, User, UserRole, UserStatus
from app.models.base import Base
from app.models.statements import MonthlyStatement
from app.models.tenancy import BillingMode, Tenant
from app.models.wallet import Wallet
from app.services.auth import utc_now

STATEMENTS = "/api/v1/admin/statements"
CUSTOMERS = "/api/v1/admin/customers"
USER_AGENT = "admin-statements-test"

# Distinctive amounts: no error body may carry them.
AMOUNT = "-0.31415926"
WRONG_CHARGE = "0.27182818"

# 设计 §2「管理端接口」的字段白名单：断言「恰好等于」。
SUMMARY_FIELDS = {
    "statement_id",
    "statement_number",
    "customer_id",
    "company_name",
    "period",
    "status",
    "closing_balance",
    "finalized_at",
}
DETAIL_FIELDS = SUMMARY_FIELDS | {
    "cutoff_at",
    "ledger_high_sequence",
    "generated_by_email",
    "snapshot",
}
GENERATED_FIELDS = {"statement_id", "period"}
PAGE_FIELDS = {"items", "page", "page_size", "total"}
# 成本类键的片段（INV-7）：响应里任何一层的任何键都不许含它们。
COST_FRAGMENTS = ("cost", "fx", "_rate", "markup", "margin", "profit", "price", "rule")


# --- fixtures ---------------------------------------------------------------------------


def _sqlite() -> Iterator[sessionmaker[Session]]:
    # StaticPool：TestClient 在线程池里跑同步端点，内存库必须所有连接共用一个
    # （与 test_admin_customers_api.py 的 app 夹具同理）。
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
    users `clean` deletes: TRUNCATE them first, with foreign key checks off."""
    with engine.begin() as connection:
        connection.exec_driver_sql("SET FOREIGN_KEY_CHECKS = 0")
        try:
            connection.exec_driver_sql("TRUNCATE TABLE monthly_statements")
            connection.exec_driver_sql("TRUNCATE TABLE tax_policy_versions")
        finally:
            connection.exec_driver_sql("SET FOREIGN_KEY_CHECKS = 1")
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
    # 500 要以统一错误体返回，而不是在测试里抛出来。
    return TestClient(app, raise_server_exceptions=False, headers={"User-Agent": USER_AGENT})


@pytest.fixture
def admin(app, books) -> dict[str, str]:
    return token_for(app, books.world.admin.id, "ADMIN")


@pytest.fixture
def clock(monkeypatch) -> list[dt.datetime]:
    """The manual catch-up's `now`; tests set `clock[0]`."""
    moment = [due(DECEMBER)]
    monkeypatch.setattr(admin_statements, "utc_now", lambda: moment[0])
    return moment


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


def customer_headers(application: FastAPI, books: Statements) -> dict[str, str]:
    with books.factory() as session:
        user = User(
            email=f"portal-{uuid.uuid4().hex}{TEST_EMAIL_DOMAIN}",
            password_hash="not-a-real-hash",
            role=UserRole.CUSTOMER,
            status=UserStatus.ACTIVE,
            created_at=T0,
            updated_at=T0,
        )
        session.add(user)
        session.commit()
        return token_for(application, int(user.id), "CUSTOMER")


def error_code(response) -> str | None:
    body = response.json()
    return (body.get("error") or {}).get("code")


def data(response) -> Any:
    assert response.status_code == 200, response.text
    return response.json()["data"]


def generate_url(customer_id: str) -> str:
    return f"{CUSTOMERS}/{customer_id}/statements/generate"


def audits(books: Statements) -> list[AuditLog]:
    query = (
        select(AuditLog)
        .where(AuditLog.action == AuditAction.STATEMENT_GENERATE)
        .order_by(AuditLog.id)
    )
    with books.factory() as session:
        return list(session.execute(query).scalars())


def statement_count(books: Statements) -> int:
    with books.factory() as session:
        return int(session.execute(select(func.count()).select_from(MonthlyStatement)).scalar_one())


def _defect(books: Statements, tenant: Tenancy, at: dt.datetime) -> None:
    """A processed event whose `billable_cost` differs from its ledger row: self-check 2 fails."""
    books.charge(tenant, at, at, AMOUNT, billable=WRONG_CHARGE)


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


def summary_of(statement: MonthlyStatement, customer_id: str) -> dict[str, Any]:
    snapshot = statement.snapshot_json
    return {
        "statement_id": statement.public_id,
        "statement_number": statement.statement_number,
        "customer_id": customer_id,
        "company_name": snapshot["company_name"],
        "period": snapshot["period"],
        "status": "FINALIZED",
        "closing_balance": snapshot["closing_balance"],
        "finalized_at": statement.finalized_at.isoformat() if statement.finalized_at else None,
    }


def two_customers_two_months(books: Statements) -> tuple[Tenancy, Tenancy, dict[str, Any]]:
    """Customers `a` and `b`, each with December and January generated by the system, in the
    row order a-December, b-December, a-January, b-January."""
    a, b = books.tenant("a"), books.tenant("b", opening=Decimal("500"))
    for tenant in (a, b):
        books.charge(tenant, DECEMBER.start + DAY, DECEMBER.start + DAY, AMOUNT)
        books.charge(tenant, JANUARY.start + DAY, JANUARY.start + DAY, AMOUNT)
    made = {}
    for period in (DECEMBER, JANUARY):
        for name, tenant in (("a", a), ("b", b)):
            made[f"{name}-{period.label}"] = books.generate(tenant, period).statement
    return a, b, made


# --- the list -----------------------------------------------------------------------------


def test_the_list_is_newest_period_first_then_newest_row(client, admin, books) -> None:
    a, b, made = two_customers_two_months(books)
    # The company name comes from the snapshot: renaming the customer afterwards changes nothing.
    with books.factory() as session:
        session.execute(update(Tenant).where(Tenant.id == a.id).values(company_name="Renamed"))
        session.commit()

    listing = data(client.get(STATEMENTS, headers=admin))

    assert set(listing) == PAGE_FIELDS
    assert (listing["page"], listing["page_size"], listing["total"]) == (1, 20, 4)
    order = ["b-2027-01", "a-2027-01", "b-2026-12", "a-2026-12"]
    owners = {"a": a.public_id, "b": b.public_id}
    assert listing["items"] == [summary_of(made[key], owners[key[0]]) for key in order]
    for item in listing["items"]:
        assert set(item) == SUMMARY_FIELDS
    assert listing["items"][1]["company_name"] == "Fictional a Sdn Bhd"
    assert (
        listing["items"][0]["closing_balance"] == made["b-2027-01"].snapshot_json["closing_balance"]
    )
    assert_no_cost_keys(listing)


def test_the_list_filters_by_customer_and_by_period(client, admin, books) -> None:
    a, b, made = two_customers_two_months(books)

    def ids(**params: str) -> list[str]:
        listing = data(client.get(STATEMENTS, params=params, headers=admin))
        assert listing["total"] == len(listing["items"])
        return [item["statement_id"] for item in listing["items"]]

    def of(*keys: str) -> list[str]:
        return [made[key].public_id for key in keys]

    assert ids(customer_id=a.public_id) == of("a-2027-01", "a-2026-12")
    assert ids(period="2026-12") == of("b-2026-12", "a-2026-12")
    assert ids(customer_id=b.public_id, period="2027-01") == of("b-2027-01")
    # Nothing for a period without statements, an unknown customer, or a malformed id.
    assert ids(period="2027-02") == []
    assert ids(customer_id=str(uuid.uuid4())) == []
    assert ids(customer_id="not-a-customer") == []


def test_the_list_paginates(client, admin, books) -> None:
    _, _, made = two_customers_two_months(books)
    order = ["b-2027-01", "a-2027-01", "b-2026-12", "a-2026-12"]

    pages = [
        data(client.get(STATEMENTS, params={"page": n, "page_size": 3}, headers=admin))
        for n in (1, 2, 3)
    ]

    assert [(p["page"], p["page_size"], p["total"]) for p in pages] == [
        (1, 3, 4),
        (2, 3, 4),
        (3, 3, 4),
    ]
    assert [[i["statement_id"] for i in p["items"]] for p in pages] == [
        [made[key].public_id for key in order[:3]],
        [made[order[3]].public_id],
        [],
    ]


@pytest.mark.parametrize("period", ["2026-13", "2026-00", "1999-12", "3000-01", "2026-1", "x"])
def test_a_malformed_or_out_of_range_period_is_422(client, admin, books, period: str) -> None:
    response = client.get(STATEMENTS, params={"period": period}, headers=admin)

    assert (response.status_code, error_code(response)) == (422, "VALIDATION_ERROR")
    assert period not in response.json()["error"]["message"]


@pytest.mark.parametrize(
    "params", [{"page": "0"}, {"page": "10001"}, {"page_size": "0"}, {"page_size": "101"}]
)
def test_paging_out_of_range_is_422(client, admin, books, params: dict[str, str]) -> None:
    response = client.get(STATEMENTS, params=params, headers=admin)

    assert (response.status_code, error_code(response)) == (422, "VALIDATION_ERROR")


# --- the detail -------------------------------------------------------------------------


def test_the_detail_adds_cutoff_high_sequence_generator_and_snapshot(
    client, admin, books, clock
) -> None:
    tenant = books.tenant("a")
    books.charge(tenant, DECEMBER.start + DAY, DECEMBER.start + DAY, AMOUNT)
    system = books.generate(tenant, DECEMBER).statement
    clock[0] = due(JANUARY)
    [made] = data(client.post(generate_url(tenant.public_id), headers=admin))
    [_, manual] = books.statements(tenant)
    assert made["statement_id"] == manual.public_id

    for statement, email in ((system, None), (manual, books.world.admin.email)):
        detail = data(client.get(f"{STATEMENTS}/{statement.public_id}", headers=admin))
        assert set(detail) == DETAIL_FIELDS
        assert {key: detail[key] for key in SUMMARY_FIELDS} == summary_of(
            statement, tenant.public_id
        )
        assert detail["cutoff_at"] == statement.cutoff_at.isoformat()
        assert detail["ledger_high_sequence"] == statement.ledger_high_sequence
        assert detail["generated_by_email"] == email
        # The whole snapshot, nested and unchanged; its key set exactly that of design §2.
        assert detail["snapshot"] == statement.snapshot_json
        assert set(detail["snapshot"]) == SNAPSHOT_KEYS
        assert set(detail["snapshot"]["tax_notice"]) == {"zh", "en"}
        assert_no_cost_keys(detail)
    assert manual.cutoff_at.isoformat() == JANUARY.cutoff.isoformat()


def test_an_absent_or_malformed_statement_id_is_404(client, admin, books) -> None:
    tenant = books.tenant("a")
    stored = books.generate(tenant, DECEMBER).statement

    for statement_id in (
        str(uuid.uuid4()),
        stored.public_id.upper(),
        stored.public_id + "0",
        "not-a-statement",
    ):
        response = client.get(f"{STATEMENTS}/{statement_id}", headers=admin)
        assert (response.status_code, error_code(response)) == (404, "STATEMENT_NOT_FOUND")


# --- the manual catch-up ------------------------------------------------------------------


def test_a_manual_catch_up_generates_each_due_period_with_one_audit_each(
    client, admin, books, clock
) -> None:
    tenant = books.tenant("a")
    books.charge(tenant, DECEMBER.start + DAY, DECEMBER.start + DAY, AMOUNT)
    books.charge(tenant, JANUARY.start + DAY, JANUARY.start + DAY, AMOUNT)
    clock[0] = due(JANUARY)

    response = client.post(generate_url(tenant.public_id), headers=admin)

    made = data(response)
    december, january = books.statements(tenant)
    assert made == [
        {"statement_id": december.public_id, "period": "2026-12"},
        {"statement_id": january.public_id, "period": "2027-01"},
    ]
    for item in made:
        assert set(item) == GENERATED_FIELDS
    assert_no_cost_keys(made)
    for statement in (december, january):
        assert statement.generated_by == books.world.admin.id
        assert statement.created_at == statement.finalized_at == due(JANUARY)

    stored = audits(books)
    assert [(a.entity_type, a.entity_id) for a in stored] == [
        ("monthly_statement", december.public_id),
        ("monthly_statement", january.public_id),
    ]
    assert [json.loads(a.after_state or "{}") for a in stored] == [
        {"period": "2026-12"},
        {"period": "2027-01"},
    ]
    for audit in stored:
        assert (audit.actor_user_id, audit.actor_role) == (books.world.admin.id, "ADMIN")
        assert audit.before_state is None
        assert audit.reason is None
        assert (audit.ip_address, audit.user_agent) == ("testclient", USER_AGENT)


def test_a_manual_catch_up_with_nothing_missing_is_empty_and_writes_no_audit(
    client, admin, books, clock
) -> None:
    tenant = books.tenant("a")
    # December by the system (no audit), January by the admin.
    books.generate(tenant, DECEMBER)
    clock[0] = due(JANUARY)
    assert len(data(client.post(generate_url(tenant.public_id), headers=admin))) == 1
    assert len(audits(books)) == 1

    again = client.post(generate_url(tenant.public_id), headers=admin)

    assert data(again) == []
    assert statement_count(books) == 2
    assert len(audits(books)) == 1


def test_a_prepaid_customer_with_nothing_due_is_an_empty_list(client, admin, books, clock) -> None:
    tenant = books.tenant("a")
    clock[0] = due(DECEMBER) - SECOND

    response = client.post(generate_url(tenant.public_id), headers=admin)

    assert data(response) == []
    assert statement_count(books) == 0
    assert audits(books) == []


def test_a_customer_not_prepaid_is_409(client, admin, books, clock) -> None:
    tenant = books.tenant("a")
    with books.factory() as session:
        session.execute(
            update(Tenant)
            .where(Tenant.id == tenant.id)
            .values(billing_mode=BillingMode.INTERNAL_METERED_ONLY)
        )
        session.commit()
    clock[0] = due(JANUARY)

    response = client.post(generate_url(tenant.public_id), headers=admin)

    assert (response.status_code, error_code(response)) == (409, "STATEMENT_NOT_ELIGIBLE")
    assert statement_count(books) == 0
    assert audits(books) == []


def test_a_customer_without_a_wallet_is_409(client, admin, books, clock) -> None:
    tenant = books.customer_at("walletless", T0)
    with books.factory() as session:
        session.execute(delete(Wallet).where(Wallet.tenant_id == tenant.id))
        session.commit()
    clock[0] = due(JANUARY)

    response = client.post(generate_url(tenant.public_id), headers=admin)

    assert (response.status_code, error_code(response)) == (409, "STATEMENT_NOT_ELIGIBLE")
    assert statement_count(books) == 0
    assert audits(books) == []


@pytest.mark.parametrize("customer_id", ["uuid", "not-a-customer"])
def test_an_unknown_customer_is_404(client, admin, books, clock, customer_id: str) -> None:
    books.tenant("a")
    clock[0] = due(JANUARY)
    path_id = str(uuid.uuid4()) if customer_id == "uuid" else customer_id

    response = client.post(generate_url(path_id), headers=admin)

    assert (response.status_code, error_code(response)) == (404, "CUSTOMER_NOT_FOUND")
    assert statement_count(books) == 0
    assert audits(books) == []


def test_a_failing_middle_period_is_500_and_keeps_the_earlier_one(
    client, admin, books, clock
) -> None:
    tenant = books.tenant("a")
    for period in (DECEMBER, JANUARY, FEBRUARY):
        books.charge(tenant, period.start + DAY, period.start + DAY, AMOUNT)
    _defect(books, tenant, JANUARY.start + 2 * DAY)
    clock[0] = due(FEBRUARY)

    response = client.post(generate_url(tenant.public_id), headers=admin)

    assert (response.status_code, error_code(response)) == (500, "INTERNAL_ERROR")
    body = response.json()
    assert body["success"] is False and body["data"] is None
    for forbidden in (AMOUNT.lstrip("-"), WRONG_CHARGE, "Sdn Bhd"):
        assert forbidden not in response.text
    # December committed with its audit; January failed; February never tried.
    [december] = books.statements(tenant)
    assert december.period_start == DECEMBER.start
    assert december.generated_by == books.world.admin.id
    [audit] = audits(books)
    assert audit.entity_id == december.public_id
    assert json.loads(audit.after_state or "{}") == {"period": "2026-12"}


def test_a_failing_audit_write_leaves_no_statement(
    client, admin, books, clock, monkeypatch
) -> None:
    """INV-13: the audit and the statement commit together or not at all."""
    tenant = books.tenant("a")
    clock[0] = due(DECEMBER)
    real = admin_statements.record_audit

    def failing(session: Session, **kwargs: Any) -> None:
        real(session, **kwargs)
        raise RuntimeError("fictional audit failure")

    monkeypatch.setattr(admin_statements, "record_audit", failing)

    response = client.post(generate_url(tenant.public_id), headers=admin)

    assert (response.status_code, error_code(response)) == (500, "INTERNAL_ERROR")
    assert statement_count(books) == 0
    assert audits(books) == []


# --- authorization ------------------------------------------------------------------------

ROUTES = [
    ("GET", STATEMENTS),
    ("GET", STATEMENTS + "/{statement_id}"),
    ("POST", CUSTOMERS + "/{customer_id}/statements/generate"),
]


@pytest.mark.parametrize("caller", ["anonymous", "customer"])
@pytest.mark.parametrize(("method", "path"), ROUTES, ids=[f"{m} {p}" for m, p in ROUTES])
def test_non_admins_are_refused_and_nothing_is_written(
    client, app, books, clock, method: str, path: str, caller: str
) -> None:
    """Anonymous 401, CUSTOMER 403, as test_admin_customers_api.py asserts them."""
    tenant = books.tenant("a")
    stored = books.generate(tenant, DECEMBER).statement
    # Something is due: a handler missing `require_admin` would really generate January.
    clock[0] = due(JANUARY)
    url = path.replace("{statement_id}", stored.public_id).replace(
        "{customer_id}", tenant.public_id
    )
    headers = {} if caller == "anonymous" else customer_headers(app, books)

    response = client.request(method, url, headers=headers)

    expected = (401, "TOKEN_INVALID") if caller == "anonymous" else (403, "ADMIN_REQUIRED")
    assert (response.status_code, error_code(response)) == expected
    assert statement_count(books) == 1
    assert audits(books) == []

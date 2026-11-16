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

Rows of gate #708 v3 §7 marked AIH-TASK-217 covered here (the PDF download):

- **客户下载** — 200, `application/pdf`, the attachment and `no-store` headers, the body equal to
  `render_statement_pdf` called directly on the stored row; twice in a row, the same bytes;
- **租户越权** — another customer's, an absent (all-zero) uuid, an upper-case uuid,
  `not-a-uuid`, the caller's own `VOID` and `DRAFT` rows: the same 404 `STATEMENT_NOT_FOUND`,
  bodies equal but for `request_id`;
- **只读状态** — a `DISABLED` account downloads (200);
- **改名与新税务政策后字节不变** — renaming the customer and its project and publishing a new
  tax policy version change no byte;
- **快照不合规**, **渲染异常**, the font being unavailable — 500 with the fixed message and
  exactly one ERROR naming only the statement's `public_id` (and the exception's class name for
  the catch-all), never the company name or the exception's text;
- **下载不改行** — every column of the row the same afterwards, no audit row.

A row with a bad snapshot, like the `DRAFT` and `VOID` ones, is inserted as a new row (the MySQL
trigger refuses changing a finalized one).

Ledger rows, events and tax policies are written as test_statement_generate.py does (its
`Statements`); statements by the system's `generate_statement`. `DRAFT` and `VOID` rows have no
writer in V1, so they are inserted directly. The MySQL half needs `BILLING_TEST_DATABASE_URL`;
it skips without it — **a skip is not a pass**, CI sets it. On MySQL, `monthly_statements` and
`tax_policy_versions` are truncated with foreign key checks off before and after every case
(their triggers refuse DELETE); the portal users go before `clean` deletes their tenants.
"""

from __future__ import annotations

import logging
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
    NOTICE_B,
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
from app.api import statement_download
from app.core.config import Settings
from app.core.database import create_session_factory
from app.core.tokens import issue_access_token
from app.main import create_app
from app.models.auth import AuditLog, User, UserRole, UserStatus
from app.models.base import Base
from app.models.statements import MonthlyStatement, StatementStatus
from app.models.tenancy import AccountStatus, BillingMode, Project, Tenant
from app.services.auth import utc_now
from app.services.statement_pdf import StatementFontUnavailable, render_statement_pdf
from app.services.statements import StatementPeriod, statement_period

STATEMENTS = "/api/v1/customer/statements"
MARCH = statement_period("2027-03")

AMOUNT = "-0.31415926"
# `Statements.tenant("a")` 的公司名：不许出现在日志与错误体里（设计 §6）。
COMPANY = "Fictional a Sdn Bhd"
ZERO_UUID = "00000000-0000-0000-0000-000000000000"
INTERNAL_ERROR_MESSAGE = "An internal error occurred."

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


ROUTES = [STATEMENTS, STATEMENTS + "/{statement_id}", STATEMENTS + "/{statement_id}/download"]


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


# --- the PDF download (gate #708 v3, AIH-TASK-217) ----------------------------------------


def download(client: TestClient, headers: dict[str, str], statement_id: str):
    return client.get(f"{STATEMENTS}/{statement_id}/download", headers=headers)


def stored(books: Statements, public_id: str) -> MonthlyStatement:
    """The row as the database holds it (the JSON read back, not the object generation built)."""
    with books.factory() as session:
        row = session.execute(
            select(MonthlyStatement).where(MonthlyStatement.public_id == public_id)
        ).scalar_one()
        session.expunge(row)
    return row


def columns_of(books: Statements, public_id: str) -> tuple[Any, ...]:
    """Every column of the row, in table order."""
    with books.factory() as session:
        return tuple(
            session.execute(
                select(*MonthlyStatement.__table__.columns).where(
                    MonthlyStatement.public_id == public_id
                )
            ).one()
        )


def audit_count(books: Statements) -> int:
    with books.factory() as session:
        return int(session.execute(select(func.count()).select_from(AuditLog)).scalar_one())


def rendered(statement: MonthlyStatement) -> bytes:
    """`render_statement_pdf` called directly on the stored row."""
    assert statement.finalized_at is not None
    return render_statement_pdf(
        statement.snapshot_json,
        statement_number=statement.statement_number,
        finalized_at=statement.finalized_at,
    )


def finalized_copy(
    books: Statements, like: MonthlyStatement, period: StatementPeriod, snapshot: dict[str, Any]
) -> MonthlyStatement:
    """A new `FINALIZED` row of `like`'s tenant for `period` carrying `snapshot`. Inserted, never
    an UPDATE of an existing row: the MySQL trigger refuses changing a finalized one."""
    row = MonthlyStatement(
        public_id=str(uuid.uuid4()),
        tenant_id=like.tenant_id,
        statement_number=f"ST-{period.label.replace('-', '')}-00000000",
        period_start=period.start,
        period_end=period.end,
        cutoff_at=period.cutoff,
        status=StatementStatus.FINALIZED,
        ledger_high_sequence=like.ledger_high_sequence,
        snapshot_json=snapshot,
        generated_by=None,
        tax_policy_version_id=like.tax_policy_version_id,
        finalized_at=like.finalized_at,
        created_at=like.created_at,
    )
    with books.factory() as session:
        session.add(row)
        session.commit()
        session.refresh(row)
        session.expunge(row)
    return row


def download_errors(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [
        record
        for record in caplog.records
        if record.name == statement_download.__name__ and record.levelno == logging.ERROR
    ]


def extras_of(record: logging.LogRecord) -> dict[str, Any]:
    baseline = logging.LogRecord("x", logging.ERROR, "x", 0, "x", None, None).__dict__
    return {key: value for key, value in record.__dict__.items() if key not in baseline}


def assert_internal_error(response) -> None:
    assert error_of(response) == (500, "INTERNAL_ERROR")
    body = response.json()
    assert body["error"]["message"] == INTERNAL_ERROR_MESSAGE
    assert body["data"] is None
    assert body["request_id"]
    assert "content-disposition" not in response.headers


def test_a_customer_downloads_its_finalized_statement(client, app, books) -> None:
    tenant = books.tenant("a")
    [december] = with_months(books, tenant, DECEMBER)
    statement = stored(books, december.public_id)
    headers = portal(app, books, tenant)

    first = download(client, headers, statement.public_id)
    second = download(client, headers, statement.public_id)

    assert first.status_code == 200, first.text
    assert first.headers["content-type"] == "application/pdf"
    assert first.headers["content-disposition"] == (
        f'attachment; filename="{statement.statement_number}.pdf"'
    )
    assert first.headers["cache-control"] == "no-store"
    assert first.content.startswith(b"%PDF-")
    assert first.content == rendered(statement)
    # The same request twice: the same bytes.
    assert second.status_code == 200
    assert second.content == first.content


def test_every_statement_the_caller_may_not_download_is_the_same_404(client, app, books) -> None:
    mine, theirs = books.tenant("a"), books.tenant("b")
    [own] = with_months(books, mine, DECEMBER)
    [other] = with_months(books, theirs, DECEMBER)
    draft = unfinished(books, own, JANUARY, StatementStatus.DRAFT)
    void = unfinished(books, own, FEBRUARY, StatementStatus.VOID)
    headers = portal(app, books, mine)

    responses = [
        download(client, headers, statement_id)
        for statement_id in (
            other.public_id,
            ZERO_UUID,
            own.public_id.upper(),
            "not-a-uuid",
            void.public_id,
            draft.public_id,
        )
    ]

    bodies = []
    for response in responses:
        assert error_of(response) == (404, "STATEMENT_NOT_FOUND")
        assert response.json()["data"] is None
        assert "content-disposition" not in response.headers
        bodies.append(without_request_id(response))
    assert all(body == bodies[0] for body in bodies)
    assert other.statement_number not in responses[0].text
    # The other customer downloads its own; the caller its own.
    assert download(client, portal(app, books, theirs), other.public_id).status_code == 200
    assert download(client, headers, own.public_id).status_code == 200


def test_a_disabled_account_downloads_its_statement(client, app, books) -> None:
    tenant = books.tenant("a")
    [december] = with_months(books, tenant, DECEMBER)
    books.world.set_account_status(tenant, AccountStatus.DISABLED)

    response = download(client, portal(app, books, tenant), december.public_id)

    assert response.status_code == 200, response.text
    assert response.content == rendered(stored(books, december.public_id))


def test_renaming_and_a_new_tax_policy_change_no_byte(client, app, books) -> None:
    """INV-6: the PDF comes from the frozen snapshot, not from today's names or tax policy."""
    tenant = books.tenant("a")
    [december] = with_months(books, tenant, DECEMBER)
    headers = portal(app, books, tenant)
    before = download(client, headers, december.public_id)
    assert before.status_code == 200, before.text
    assert [line["project_name"] for line in december.snapshot_json["by_project"]] == ["Chatbot"]

    with books.factory() as session:
        session.execute(
            update(Tenant).where(Tenant.id == tenant.id).values(company_name="Renamed Sdn Bhd")
        )
        session.execute(
            update(Project).where(Project.id == tenant.project_id).values(name="Renamed project")
        )
        session.commit()
    assert books.tax_version is not None
    books.end_tax(books.tax_version, JANUARY.end)
    books.publish_tax(JANUARY.end, NOTICE_B)

    after = download(client, headers, december.public_id)

    assert after.status_code == 200, after.text
    assert after.content == before.content


def test_an_invalid_snapshot_is_500_with_one_error_log(client, app, books, caplog) -> None:
    tenant = books.tenant("a")
    [december] = with_months(books, tenant, DECEMBER)
    # One key too many (design §2「校验」): refused, never printed nor ignored.
    broken = finalized_copy(
        books,
        december,
        JANUARY,
        {**december.snapshot_json, "period": JANUARY.label, "estimated_cost": "0.00000000"},
    )
    headers = portal(app, books, tenant)

    with caplog.at_level(logging.ERROR, logger=statement_download.__name__):
        response = download(client, headers, broken.public_id)

    assert_internal_error(response)
    assert COMPANY not in response.text
    [record] = download_errors(caplog)
    assert record.getMessage() == "Statement snapshot invalid; download refused"
    assert record.exc_info is None
    extras = extras_of(record)
    assert extras == {"statement_id": broken.public_id}
    logged = record.getMessage() + repr(extras)
    for forbidden in (COMPANY, broken.statement_number, "Chatbot", AMOUNT.lstrip("-")):
        assert forbidden not in logged, forbidden
    assert COMPANY not in caplog.text
    # The good one next to it still downloads.
    assert download(client, headers, december.public_id).status_code == 200


def test_a_rendering_exception_is_500_naming_only_its_class(
    client, app, books, caplog, monkeypatch
) -> None:
    tenant = books.tenant("a")
    [december] = with_months(books, tenant, DECEMBER)
    text = f"fictional fpdf2 failure while printing {COMPANY}"

    def failing(*args: Any, **kwargs: Any) -> bytes:
        raise RuntimeError(text)

    monkeypatch.setattr(statement_download, "render_statement_pdf", failing)

    with caplog.at_level(logging.ERROR, logger=statement_download.__name__):
        response = download(client, portal(app, books, tenant), december.public_id)

    assert_internal_error(response)
    assert text not in response.text
    errors = [record for record in caplog.records if record.levelno >= logging.ERROR]
    [record] = download_errors(caplog)
    assert errors == [record]
    assert record.getMessage() == "Statement PDF rendering failed"
    assert record.exc_info is None
    extras = extras_of(record)
    assert extras == {"statement_id": december.public_id, "error_type": "RuntimeError"}
    assert text not in record.getMessage() + repr(extras)
    assert text not in caplog.text
    assert COMPANY not in caplog.text


def test_an_unavailable_font_is_500_with_its_own_error_log(
    client, app, books, caplog, monkeypatch
) -> None:
    tenant = books.tenant("a")
    [december] = with_months(books, tenant, DECEMBER)

    def failing(*args: Any, **kwargs: Any) -> bytes:
        raise StatementFontUnavailable(f"fictional missing font for {COMPANY}")

    monkeypatch.setattr(statement_download, "render_statement_pdf", failing)

    with caplog.at_level(logging.ERROR, logger=statement_download.__name__):
        response = download(client, portal(app, books, tenant), december.public_id)

    assert_internal_error(response)
    [record] = download_errors(caplog)
    # Its own message, not the catch-all's, and no `error_type`.
    assert record.getMessage() == "Statement font unavailable; download refused"
    assert record.exc_info is None
    assert extras_of(record) == {"statement_id": december.public_id}
    assert COMPANY not in caplog.text


def test_a_download_changes_no_row_and_writes_no_audit(client, app, books) -> None:
    """INV-12: read only; no audit (as the detail)."""
    tenant = books.tenant("a")
    [december] = with_months(books, tenant, DECEMBER)
    headers = portal(app, books, tenant)
    before = columns_of(books, december.public_id)
    audits = audit_count(books)

    assert download(client, headers, december.public_id).status_code == 200

    assert columns_of(books, december.public_id) == before
    assert audit_count(books) == audits

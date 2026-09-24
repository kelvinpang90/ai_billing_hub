"""Admin customer management end to end on SQLite (design gate #96 v3 §7).

事务在 MySQL 上的原子性与真实触发器在 test_customer_service.py；这里测接口契约：
鉴权、字段白名单、校验边界、分页、租户隔离、金额格式，以及日志里没有个人数据。

访问令牌直接用 `issue_access_token` 签：ADMIN 走完 2FA 才拿得到它，那条路径在
test_auth_api.py，这里只关心拿着令牌之后的事。
"""

from __future__ import annotations

import io
import json
import logging
import uuid

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select, text, update
from sqlalchemy.pool import StaticPool

from app.core.config import Settings
from app.core.database import create_session_factory
from app.core.logging import JsonFormatter
from app.core.tokens import issue_access_token, issue_pending_2fa_token
from app.main import create_app
from app.models.auth import AuditAction, AuditLog, DomainOutbox, User, UserRole, UserStatus
from app.models.base import Base
from app.models.tenancy import Project, Tenant
from app.models.wallet import Wallet, WalletTransaction
from app.services.auth import utc_now

ADMIN_PREFIX = "/api/v1/admin"
CUSTOMERS = f"{ADMIN_PREFIX}/customers"

# 响应字段白名单（设计 §2）。断言「恰好等于」：多一个键（内部 id、阈值、成本）就红。
CUSTOMER_FIELDS = {
    "id",
    "company_name",
    "contact_name",
    "email",
    "phone",
    "billing_status",
    "status_version",
    "created_at",
    "updated_at",
}
DETAIL_FIELDS = CUSTOMER_FIELDS | {"wallet"}
WALLET_FIELDS = {"currency", "balance", "version"}
PROJECT_FIELDS = {"id", "name", "description", "created_at", "updated_at"}
PAGE_FIELDS = {"items", "page", "page_size", "total"}
ENVELOPE_FIELDS = {"success", "data", "error", "request_id"}

# 设计 §2 的五个接口，加 AIH-TASK-009 的编辑客户、AIH-TASK-011 的调账。
EXPECTED_ADMIN_ROUTES = {
    ("POST", "/api/v1/admin/customers"),
    ("GET", "/api/v1/admin/customers"),
    ("GET", "/api/v1/admin/customers/{customer_id}"),
    ("PATCH", "/api/v1/admin/customers/{customer_id}"),
    ("POST", "/api/v1/admin/customers/{customer_id}/projects"),
    ("GET", "/api/v1/admin/customers/{customer_id}/projects"),
    ("POST", "/api/v1/admin/customers/{customer_id}/wallet/adjustments"),
}

# 鉴权用例给写接口的合法请求体：体不合法的话 FastAPI 在处理函数之前就回 422，
# 那条用例就测不到 `require_admin` 了。
VALID_BODIES = {
    ("POST", "/api/v1/admin/customers"): {"company_name": "Probe", "email": "probe@example.com"},
    ("PATCH", "/api/v1/admin/customers/{customer_id}"): {"company_name": "Renamed"},
    ("POST", "/api/v1/admin/customers/{customer_id}/projects"): {"name": "Probe"},
    ("POST", "/api/v1/admin/customers/{customer_id}/wallet/adjustments"): {
        "transaction_type": "ADJUSTMENT_CREDIT",
        "amount": "20",
        "reason": "Probe",
        "idempotency_key": "00000000-0000-4000-8000-000000000000",
    },
}


HTTP_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "TRACE"}


def admin_routes(application: FastAPI) -> set[tuple[str, str]]:
    """Every (method, path) the app serves under the admin prefix.

    两条来源取并集。⚠️ 本仓库的 FastAPI 延迟挂载子路由，`app.routes` 顶层只有
    `_IncludedRouter` 壳子（见 test_password_reset_link.py），所以逐层往里找；
    OpenAPI 文档是另一条来源，但它看不见 `include_in_schema=False` 的路由。
    两条都落空时，下面「恰好是这几个」那条用例会红。
    """
    found = {
        (method.upper(), path)
        for path, operations in application.openapi()["paths"].items()
        if path.startswith(ADMIN_PREFIX)
        for method in operations
        if method.upper() in HTTP_METHODS
    }
    for route in _walk(application.routes, set()):
        if isinstance(route, APIRoute) and route.path.startswith(ADMIN_PREFIX):
            found |= {(method, route.path) for method in route.methods}
    return found


def _walk(routes, seen: set[int]):
    for route in routes:
        if id(route) in seen:
            continue
        seen.add(id(route))
        yield route
        # Mount、Router 与延迟挂载的子路由都把下一层放在 `routes` 或 `router.routes` 上。
        children = getattr(route, "routes", None)
        if children is None:
            children = getattr(getattr(route, "router", None), "routes", None)
        if children:
            yield from _walk(children, seen)


# ⚠️ 从 `create_app()` 建出的应用里枚举，不手写清单：新增的管理端接口会自动进下面的
# 鉴权用例，漏调 `require_admin` 就红（设计 §7、§9）。
ADMIN_ROUTES = sorted(admin_routes(create_app(Settings(database_url="", jwt_secret_file=""))))
ROUTE_IDS = [f"{method} {path}" for method, path in ADMIN_ROUTES]


# --- 夹具与帮手 -----------------------------------------------------------------


def settings_for(tmp_path, database_url: str = "") -> Settings:
    key = tmp_path / "jwt.key"
    key.write_text("a-test-signing-key-that-is-long-enough", encoding="utf-8")
    return Settings(jwt_secret_file=str(key), database_url=database_url)


@pytest.fixture
def app(tmp_path):
    application = create_app(settings_for(tmp_path))
    # StaticPool：TestClient 在线程池里跑同步端点，内存库必须所有连接共用一个
    # （理由见 test_auth_api.py 的 in_memory_engine）。
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    application.state.engine = engine
    application.state.session_factory = create_session_factory(engine)
    yield application
    engine.dispose()


@pytest.fixture
def client(app) -> TestClient:
    return TestClient(app)


def make_user(
    application: FastAPI,
    *,
    role: UserRole = UserRole.ADMIN,
    status: UserStatus = UserStatus.ACTIVE,
) -> int:
    now = utc_now()
    with application.state.session_factory() as session:
        user = User(
            email=f"{uuid.uuid4().hex}@example.com",
            password_hash="not-a-real-hash",
            role=role,
            status=status,
            created_at=now,
            updated_at=now,
        )
        session.add(user)
        session.commit()
        return int(user.id)


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def token_for(application: FastAPI, user_id: int, role: str = "ADMIN") -> dict[str, str]:
    """A signed access token. `role` is only the claim; the server reads the db."""
    token = issue_access_token(
        application.state.settings,
        user_id=user_id,
        role=role,
        session_id=uuid.uuid4().hex,
        now=utc_now(),
    )
    return bearer(token)


@pytest.fixture
def admin_id(app) -> int:
    return make_user(app)


@pytest.fixture
def admin(app, admin_id) -> dict[str, str]:
    return token_for(app, admin_id)


def customer_headers(application: FastAPI) -> dict[str, str]:
    return token_for(application, make_user(application, role=UserRole.CUSTOMER), "CUSTOMER")


def count_rows(session, model) -> int:
    return session.execute(select(func.count()).select_from(model)).scalar_one()


def row_counts(application: FastAPI) -> dict[str, int]:
    """账本与出站事件也数进来：调账接口漏了鉴权的话，这两张表会多行（AIH-TASK-011）。"""
    with application.state.session_factory() as session:
        return {
            model.__tablename__: count_rows(session, model)
            for model in (Tenant, Project, Wallet, AuditLog, WalletTransaction, DomainOutbox)
        }


NO_ROWS = {
    "tenants": 0,
    "projects": 0,
    "wallets": 0,
    "audit_logs": 0,
    "wallet_transactions": 0,
    "domain_outbox": 0,
}


def audits(application: FastAPI, action: AuditAction) -> list[AuditLog]:
    statement = select(AuditLog).where(AuditLog.action == action).order_by(AuditLog.id)
    with application.state.session_factory() as session:
        return list(session.execute(statement).scalars())


def new_customer(client: TestClient, headers: dict[str, str], **fields: object) -> dict:
    body = {"company_name": "Acme Sdn Bhd", "email": "ops@example.com", **fields}
    response = client.post(CUSTOMERS, json=body, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()["data"]


def new_project(
    client: TestClient, headers: dict[str, str], customer_id: str, **fields: object
) -> dict:
    body = {"name": "Chatbot", **fields}
    response = client.post(f"{CUSTOMERS}/{customer_id}/projects", json=body, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()["data"]


def error_code(response) -> str:
    body = response.json()
    assert set(body) == ENVELOPE_FIELDS
    assert body["success"] is False
    assert body["data"] is None
    return body["error"]["code"]


# --- 建客户 ---------------------------------------------------------------------


def test_an_admin_creates_a_customer_with_an_empty_wallet(client, app, admin, admin_id) -> None:
    response = client.post(
        CUSTOMERS,
        json={
            "company_name": "  Acme Sdn Bhd  ",
            "email": "ops@example.com",
            "contact_name": "Contact Person",
            "phone": "+60 3-0000 0000",
        },
        headers={**admin, "User-Agent": "admin-console-test"},
    )

    assert response.status_code == 201
    body = response.json()
    assert set(body) == ENVELOPE_FIELDS
    assert body["success"] is True
    assert body["error"] is None
    data = body["data"]
    # 字段白名单：没有内部自增 id、没有 low_balance_threshold、没有成本或毛利。
    assert set(data) == DETAIL_FIELDS
    assert set(data["wallet"]) == WALLET_FIELDS
    assert str(uuid.UUID(data["id"])) == data["id"]
    assert data["company_name"] == "Acme Sdn Bhd"
    assert (data["email"], data["contact_name"], data["phone"]) == (
        "ops@example.com",
        "Contact Person",
        "+60 3-0000 0000",
    )
    assert (data["billing_status"], data["status_version"]) == ("SUSPENDED", 0)
    assert data["wallet"] == {"currency": "MYR", "balance": "0.00000000", "version": 0}

    assert row_counts(app) == {**NO_ROWS, "tenants": 1, "wallets": 1, "audit_logs": 1}
    [audit] = audits(app, AuditAction.CUSTOMER_CREATE)
    assert (audit.actor_user_id, audit.actor_role) == (admin_id, "ADMIN")
    assert (audit.entity_type, audit.entity_id) == ("tenant", data["id"])
    assert audit.user_agent == "admin-console-test"
    assert json.loads(audit.after_state or "{}") == {
        "public_id": data["id"],
        "company_name": "Acme Sdn Bhd",
        "billing_status": "SUSPENDED",
        "wallet_currency": "MYR",
    }
    for personal in ("ops@example.com", "Contact Person", "+60 3-0000 0000"):
        assert personal not in (audit.after_state or "")


def test_blank_optional_fields_are_stored_as_null(client, admin) -> None:
    created = new_customer(client, admin, contact_name="", phone="   ")

    assert (created["contact_name"], created["phone"]) == (None, None)
    fetched = client.get(f"{CUSTOMERS}/{created['id']}", headers=admin).json()["data"]
    assert (fetched["contact_name"], fetched["phone"]) == (None, None)


# --- 鉴权 -----------------------------------------------------------------------


def test_the_admin_routes_are_exactly_the_expected_ones() -> None:
    """既防枚举落空（下面的参数化用例一条都不跑也是绿的），也逼新增接口补进来。"""
    assert set(ADMIN_ROUTES) == EXPECTED_ADMIN_ROUTES


@pytest.mark.parametrize("caller", ["anonymous", "customer"])
@pytest.mark.parametrize(("method", "path"), ADMIN_ROUTES, ids=ROUTE_IDS)
def test_every_admin_route_refuses_non_admins(
    client, app, admin, method: str, path: str, caller: str
) -> None:
    """INV-8：匿名 401、CUSTOMER 403，且不写库 —— 漏调 `require_admin` 的接口在这里红。"""
    # 路径参数填一个真实客户：漏了鉴权的处理函数会真的成功，而不是碰巧 404。
    customer_id = new_customer(client, admin)["id"]
    url = path.replace("{customer_id}", customer_id)
    headers = {} if caller == "anonymous" else customer_headers(app)
    before, stored = row_counts(app), stored_tenant(app, customer_id)

    response = client.request(method, url, json=VALID_BODIES.get((method, path)), headers=headers)

    expected = (401, "TOKEN_INVALID") if caller == "anonymous" else (403, "ADMIN_REQUIRED")
    assert (response.status_code, error_code(response)) == expected
    # 行数看不出 UPDATE，所以客户行逐列再比一次（编辑接口漏了鉴权会在这里红）。
    assert row_counts(app) == before
    assert stored_tenant(app, customer_id) == stored


@pytest.mark.parametrize(
    ("kind", "expected"),
    [("bad", 401), ("pending", 401), ("disabled", 401), ("wrong_scheme", 401), ("admin", 200)],
)
def test_token_kinds(client, app, admin_id, kind: str, expected: int) -> None:
    settings = app.state.settings
    headers = {
        "bad": lambda: bearer("not-a-token"),
        # ⚠️ 2FA 的 pending 令牌不是访问令牌，拿它调业务接口必须被拒。
        "pending": lambda: bearer(
            issue_pending_2fa_token(settings, user_id=admin_id, now=utc_now(), ttl_seconds=600)
        ),
        # 停用的账号立刻失效，不等访问令牌过期。
        "disabled": lambda: token_for(app, make_user(app, status=UserStatus.DISABLED)),
        "wrong_scheme": lambda: {"Authorization": token_for(app, admin_id)["Authorization"][7:]},
        "admin": lambda: token_for(app, admin_id),
    }[kind]()

    response = client.get(CUSTOMERS, headers=headers)

    assert response.status_code == expected
    if expected == 401:
        assert error_code(response) == "TOKEN_INVALID"


def test_a_demoted_admin_is_refused_at_once(client, app, admin, admin_id) -> None:
    """角色以数据库为准：降权即时生效，手上的访问令牌不再管用。"""
    assert client.get(CUSTOMERS, headers=admin).status_code == 200
    with app.state.session_factory() as session:
        session.execute(update(User).where(User.id == admin_id).values(role=UserRole.CUSTOMER))
        session.commit()

    response = client.get(CUSTOMERS, headers=admin)

    assert (response.status_code, error_code(response)) == (403, "ADMIN_REQUIRED")


def test_the_role_claim_in_the_token_is_not_trusted(client, app) -> None:
    customer_id = make_user(app, role=UserRole.CUSTOMER)

    response = client.get(CUSTOMERS, headers=token_for(app, customer_id, role="ADMIN"))

    assert (response.status_code, error_code(response)) == (403, "ADMIN_REQUIRED")


def test_without_a_database_the_admin_routes_say_so(tmp_path) -> None:
    application = create_app(settings_for(tmp_path))
    token = issue_access_token(
        application.state.settings, user_id=1, role="ADMIN", session_id="s", now=utc_now()
    )

    response = TestClient(application).get(CUSTOMERS, headers=bearer(token))

    assert (response.status_code, error_code(response)) == (503, "DATABASE_NOT_CONFIGURED")


# --- 校验边界 -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("company_name", "expected"),
    [
        ("", 422),
        ("   ", 422),
        ("x" * 256, 422),
        ("x" * 255, 201),
        # 先去首尾空白再量长度。
        ("  " + "x" * 255 + "  ", 201),
    ],
    ids=["empty", "blank", "256", "255", "255-padded"],
)
def test_company_name_bounds(client, app, admin, company_name: str, expected: int) -> None:
    response = client.post(
        CUSTOMERS, json={"company_name": company_name, "email": "ops@example.com"}, headers=admin
    )

    assert response.status_code == expected
    if expected == 422:
        assert error_code(response) == "VALIDATION_ERROR"
        assert row_counts(app) == NO_ROWS
    else:
        assert response.json()["data"]["company_name"] == "x" * 255


@pytest.mark.parametrize(
    "fields",
    [
        {"email": "not-an-email"},
        {"email": "a" * 310 + "@example.com"},
        {"contact_name": "x" * 256},
        {"phone": "1" * 33},
        {"company_name": None},
    ],
    ids=["email-format", "email-length", "contact-256", "phone-33", "company-null"],
)
def test_invalid_customer_fields_are_rejected(client, app, admin, fields: dict) -> None:
    body = {"company_name": "Acme Sdn Bhd", "email": "ops@example.com", **fields}

    response = client.post(CUSTOMERS, json=body, headers=admin)

    assert (response.status_code, error_code(response)) == (422, "VALIDATION_ERROR")
    assert row_counts(app) == NO_ROWS


@pytest.mark.parametrize(
    "extra",
    [
        {"tenant_id": 1},
        {"billing_status": "ACTIVE"},
        {"public_id": "00000000-0000-4000-8000-000000000000"},
        {"id": "00000000-0000-4000-8000-000000000000"},
        {"balance": "100.00000000"},
    ],
    ids=["tenant_id", "billing_status", "public_id", "id", "balance"],
)
def test_the_customer_body_refuses_extra_fields(client, app, admin, extra: dict) -> None:
    """客户不能经请求体指定 id、计费状态或余额（extra="forbid"）。"""
    body = {"company_name": "Acme Sdn Bhd", "email": "ops@example.com", **extra}

    response = client.post(CUSTOMERS, json=body, headers=admin)

    assert (response.status_code, error_code(response)) == (422, "VALIDATION_ERROR")
    assert row_counts(app) == NO_ROWS


@pytest.mark.parametrize(
    "body",
    [
        {"name": "   "},
        {"name": "x" * 256},
        {"name": "Chatbot", "description": "x" * 1001},
        {"name": "Chatbot", "tenant_id": 1},
        {"name": "Chatbot", "customer_id": "00000000-0000-4000-8000-000000000000"},
    ],
    ids=["blank", "name-256", "description-1001", "tenant_id", "customer_id"],
)
def test_invalid_project_bodies_are_rejected(client, app, admin, body: dict) -> None:
    customer_id = new_customer(client, admin)["id"]
    before = row_counts(app)

    response = client.post(f"{CUSTOMERS}/{customer_id}/projects", json=body, headers=admin)

    assert (response.status_code, error_code(response)) == (422, "VALIDATION_ERROR")
    assert row_counts(app) == before


@pytest.mark.parametrize("listing", ["customers", "projects"])
@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("page_size=0", 422),
        ("page_size=101", 422),
        ("page_size=100", 200),
        ("page=0", 422),
        ("page=10001", 422),
        ("page=10000", 200),
        ("page=abc", 422),
    ],
)
def test_paging_bounds(client, admin, listing: str, query: str, expected: int) -> None:
    """超出范围是 422，不静默截断（spec §108）。"""
    customer_id = new_customer(client, admin)["id"]
    base = CUSTOMERS if listing == "customers" else f"{CUSTOMERS}/{customer_id}/projects"

    response = client.get(f"{base}?{query}", headers=admin)

    assert response.status_code == expected
    if expected == 422:
        assert error_code(response) == "VALIDATION_ERROR"
    else:
        # 合法边界照常回一页；远在末页之后的那一页是空列表。
        page = response.json()["data"]
        assert set(page) == PAGE_FIELDS
        if "page=10000" in query:
            assert page["items"] == []


# --- 分页 -----------------------------------------------------------------------


def test_customers_are_paged_newest_first(client, admin) -> None:
    created = [new_customer(client, admin, company_name=f"Company {n}")["id"] for n in range(25)]
    newest_first = created[::-1]

    def page(number: int) -> dict:
        response = client.get(CUSTOMERS, params={"page": number, "page_size": 20}, headers=admin)
        assert response.status_code == 200
        return response.json()["data"]

    first, second, beyond = page(1), page(2), page(3)

    assert [item["id"] for item in first["items"]] == newest_first[:20]
    assert [item["id"] for item in second["items"]] == newest_first[20:]
    assert beyond["items"] == []
    assert [p["total"] for p in (first, second, beyond)] == [25, 25, 25]
    assert [(p["page"], p["page_size"]) for p in (first, second, beyond)] == [
        (1, 20),
        (2, 20),
        (3, 20),
    ]
    # 列表项是客户详情去掉 wallet。
    assert all(set(item) == CUSTOMER_FIELDS for item in first["items"] + second["items"])


def test_listing_defaults_to_the_first_page_of_twenty(client, admin) -> None:
    new_customer(client, admin)

    page = client.get(CUSTOMERS, headers=admin).json()["data"]

    assert (page["page"], page["page_size"], page["total"]) == (1, 20, 1)


# --- 编辑客户（AIH-TASK-009） ---------------------------------------------------


def stored_tenant(application: FastAPI, customer_id: str) -> dict:
    with application.state.session_factory() as session:
        tenant = session.execute(select(Tenant).where(Tenant.public_id == customer_id)).scalar_one()
        return {
            column: getattr(tenant, column)
            for column in (
                "company_name",
                "contact_name",
                "email",
                "phone",
                "billing_status",
                "status_version",
                "low_balance_threshold",
                "updated_at",
            )
        }


def test_an_admin_edits_a_customer_and_it_is_audited(client, app, admin, admin_id) -> None:
    created = new_customer(
        client, admin, company_name="Old Name", contact_name=PII_CONTACT, phone=PII_PHONE
    )
    before = row_counts(app)

    response = client.patch(
        f"{CUSTOMERS}/{created['id']}",
        json={"company_name": "  New Name  ", "email": PII_EMAIL, "phone": None},
        headers={**admin, "User-Agent": "admin-console-test"},
    )

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert set(data) == DETAIL_FIELDS
    assert (data["company_name"], data["email"], data["contact_name"], data["phone"]) == (
        "New Name",
        PII_EMAIL,
        PII_CONTACT,
        None,
    )
    # 计费状态与钱包不受影响。
    assert (data["billing_status"], data["status_version"]) == ("SUSPENDED", 0)
    assert data["wallet"] == created["wallet"]
    assert client.get(f"{CUSTOMERS}/{created['id']}", headers=admin).json()["data"] == data

    assert row_counts(app) == {**before, "audit_logs": before["audit_logs"] + 1}
    [audit] = audits(app, AuditAction.CUSTOMER_UPDATE)
    assert (audit.actor_user_id, audit.actor_role) == (admin_id, "ADMIN")
    assert (audit.entity_type, audit.entity_id) == ("tenant", created["id"])
    assert audit.user_agent == "admin-console-test"
    assert json.loads(audit.before_state or "{}") == {
        "public_id": created["id"],
        "company_name": "Old Name",
    }
    assert json.loads(audit.after_state or "{}") == {
        "public_id": created["id"],
        "company_name": "New Name",
        "changed_fields": ["company_name", "email", "phone"],
    }
    # 前后状态都不含个人数据的值，旧值新值都不含。
    for personal in (PII_EMAIL, "ops@example.com", PII_CONTACT, PII_PHONE):
        assert personal not in (audit.before_state or "")
        assert personal not in (audit.after_state or "")


def test_editing_only_personal_fields_records_names_not_values(client, app, admin) -> None:
    created = new_customer(client, admin)

    response = client.patch(
        f"{CUSTOMERS}/{created['id']}",
        json={"contact_name": PII_CONTACT, "phone": PII_PHONE},
        headers=admin,
    )

    assert response.status_code == 200
    [audit] = audits(app, AuditAction.CUSTOMER_UPDATE)
    assert json.loads(audit.after_state or "{}")["changed_fields"] == ["contact_name", "phone"]
    for personal in (PII_CONTACT, PII_PHONE, "ops@example.com"):
        assert personal not in (audit.before_state or "")
        assert personal not in (audit.after_state or "")


def test_blank_optional_fields_clear_them_on_edit(client, admin) -> None:
    created = new_customer(client, admin, contact_name="Someone", phone="123")

    response = client.patch(
        f"{CUSTOMERS}/{created['id']}", json={"contact_name": "  ", "phone": None}, headers=admin
    )

    assert response.status_code == 200
    assert (response.json()["data"]["contact_name"], response.json()["data"]["phone"]) == (
        None,
        None,
    )


def test_an_edit_that_changes_nothing_writes_nothing(client, app, admin) -> None:
    created = new_customer(client, admin, company_name="Same Name")
    before, stored = row_counts(app), stored_tenant(app, created["id"])

    response = client.patch(
        f"{CUSTOMERS}/{created['id']}",
        json={"company_name": " Same Name ", "email": "ops@example.com"},
        headers=admin,
    )

    assert response.status_code == 200
    assert response.json()["data"] == created
    assert row_counts(app) == before
    assert stored_tenant(app, created["id"]) == stored
    assert audits(app, AuditAction.CUSTOMER_UPDATE) == []


@pytest.mark.parametrize(
    "body",
    [
        {"billing_status": "ACTIVE"},
        {"status_version": 5},
        {"public_id": "00000000-0000-4000-8000-000000000000"},
        {"id": "00000000-0000-4000-8000-000000000000"},
        {"tenant_id": 1},
        {"low_balance_threshold": "10.00000000"},
        {"balance": "100.00000000"},
        {"company_name": "Fine", "billing_status": "ACTIVE"},
        {"company_name": "Fine", "status_version": 5},
        {"company_name": "Fine", "public_id": "00000000-0000-4000-8000-000000000000"},
        {},
        {"company_name": None},
        {"email": None},
        {"company_name": "   "},
        {"company_name": "x" * 256},
        {"email": "not-an-email"},
        {"email": "a" * 310 + "@example.com"},
        {"contact_name": "x" * 256},
        {"phone": "1" * 33},
    ],
    ids=[
        "billing_status",
        "status_version",
        "public_id",
        "id",
        "tenant_id",
        "low_balance_threshold",
        "balance",
        "valid+billing_status",
        "valid+status_version",
        "valid+public_id",
        "empty",
        "company-null",
        "email-null",
        "company-blank",
        "company-256",
        "email-format",
        "email-length",
        "contact-256",
        "phone-33",
    ],
)
def test_invalid_edit_bodies_are_rejected_and_write_nothing(client, app, admin, body: dict) -> None:
    """extra="forbid"：计费状态、状态版本、public_id 都不能经这里改。"""
    created = new_customer(client, admin)
    before, stored = row_counts(app), stored_tenant(app, created["id"])

    response = client.patch(f"{CUSTOMERS}/{created['id']}", json=body, headers=admin)

    assert (response.status_code, error_code(response)) == (422, "VALIDATION_ERROR")
    assert row_counts(app) == before
    assert stored_tenant(app, created["id"]) == stored


def test_an_edit_does_not_leak_personal_data_into_a_validation_error(client, admin) -> None:
    created = new_customer(client, admin)

    response = client.patch(
        f"{CUSTOMERS}/{created['id']}",
        json={"email": PII_EMAIL, "phone": PII_PHONE + "x" * 40},
        headers=admin,
    )

    assert response.status_code == 422
    assert PII_EMAIL not in response.text
    assert PII_PHONE not in response.text


# --- 项目 -----------------------------------------------------------------------


def test_an_admin_creates_a_project_under_a_customer(client, app, admin, admin_id) -> None:
    customer = new_customer(client, admin)
    before = row_counts(app)

    response = client.post(
        f"{CUSTOMERS}/{customer['id']}/projects",
        json={"name": "  Chatbot  ", "description": ""},
        headers=admin,
    )

    assert response.status_code == 201
    project = response.json()["data"]
    assert set(project) == PROJECT_FIELDS
    assert str(uuid.UUID(project["id"])) == project["id"]
    assert (project["name"], project["description"]) == ("Chatbot", None)
    assert row_counts(app) == {
        **before,
        "projects": before["projects"] + 1,
        "audit_logs": before["audit_logs"] + 1,
    }
    [audit] = audits(app, AuditAction.PROJECT_CREATE)
    assert (audit.actor_user_id, audit.actor_role) == (admin_id, "ADMIN")
    assert (audit.entity_type, audit.entity_id) == ("project", project["id"])
    assert json.loads(audit.after_state or "{}") == {
        "public_id": project["id"],
        "name": "Chatbot",
        "tenant_public_id": customer["id"],
    }


def test_projects_are_paged_oldest_first(client, admin) -> None:
    customer_id = new_customer(client, admin)["id"]
    url = f"{CUSTOMERS}/{customer_id}/projects"
    created = [new_project(client, admin, customer_id, name=f"P{n}")["id"] for n in range(3)]

    first = client.get(url, params={"page": 1, "page_size": 2}, headers=admin).json()["data"]
    second = client.get(url, params={"page": 2, "page_size": 2}, headers=admin).json()["data"]

    assert [item["id"] for item in first["items"]] == created[:2]
    assert [item["id"] for item in second["items"]] == created[2:]
    assert first["total"] == second["total"] == 3
    assert all(set(item) == PROJECT_FIELDS for item in first["items"] + second["items"])


def test_a_project_for_an_unknown_customer_is_404_and_writes_nothing(client, app, admin) -> None:
    """设计审查 v3 的建议。"""
    new_customer(client, admin)
    before = row_counts(app)

    response = client.post(
        f"{CUSTOMERS}/{uuid.uuid4()}/projects", json={"name": "Orphan"}, headers=admin
    )

    assert (response.status_code, error_code(response)) == (404, "CUSTOMER_NOT_FOUND")
    assert row_counts(app) == before


# --- 租户隔离 -------------------------------------------------------------------


def test_projects_are_listed_only_under_their_own_customer(client, admin) -> None:
    first = new_customer(client, admin, company_name="First Sdn Bhd")["id"]
    second = new_customer(client, admin, company_name="Second Sdn Bhd")["id"]
    theirs = new_project(client, admin, first, name="First's project")["id"]
    mine = new_project(client, admin, second, name="Second's project")["id"]

    listed = client.get(f"{CUSTOMERS}/{second}/projects", headers=admin).json()["data"]

    assert [item["id"] for item in listed["items"]] == [mine]
    assert listed["total"] == 1
    assert theirs not in json.dumps(listed)


def test_unknown_ids_are_indistinguishable_404s(client, app, admin) -> None:
    """不存在的 public_id、别的实体的 public_id、内部自增 id：同一个 404，同一个错误体。"""
    customer_id = new_customer(client, admin)["id"]
    project_id = new_project(client, admin, customer_id)["id"]
    before = row_counts(app)

    responses = []
    for unknown in (str(uuid.uuid4()), project_id, "1"):
        responses += [
            client.get(f"{CUSTOMERS}/{unknown}", headers=admin),
            client.get(f"{CUSTOMERS}/{unknown}/projects", headers=admin),
            client.post(f"{CUSTOMERS}/{unknown}/projects", json={"name": "x"}, headers=admin),
            client.patch(f"{CUSTOMERS}/{unknown}", json={"company_name": "x"}, headers=admin),
        ]

    assert {response.status_code for response in responses} == {404}
    assert {error_code(response) for response in responses} == {"CUSTOMER_NOT_FOUND"}
    assert len({json.dumps(response.json()["error"]) for response in responses}) == 1
    assert row_counts(app) == before


# --- 金额与重放 -----------------------------------------------------------------


def test_the_balance_is_the_same_eight_place_string_on_post_and_get(client, admin) -> None:
    """INV-10：字符串，不是数字，也不是 `"0"`；POST 与 GET 一致。"""
    created = new_customer(client, admin)

    response = client.get(f"{CUSTOMERS}/{created['id']}", headers=admin)

    assert response.status_code == 200
    fetched = response.json()["data"]
    for wallet in (created["wallet"], fetched["wallet"]):
        assert isinstance(wallet["balance"], str)
        assert wallet["balance"] == "0.00000000"
    # 整个详情逐字相同（时刻截到整秒，见 app/services/customers.py 的 `_now`）。
    assert fetched == created


def test_posting_the_same_body_twice_makes_two_customers(client, app, admin) -> None:
    """设计 §4 明确接受的行为：不设幂等键，重复提交得到两个完整的客户。"""
    body = {"company_name": "Acme Sdn Bhd", "email": "ops@example.com"}

    first = client.post(CUSTOMERS, json=body, headers=admin).json()["data"]
    second = client.post(CUSTOMERS, json=body, headers=admin).json()["data"]

    assert first["id"] != second["id"]
    assert row_counts(app) == {**NO_ROWS, "tenants": 2, "wallets": 2, "audit_logs": 2}
    for customer_id in (first["id"], second["id"]):
        fetched = client.get(f"{CUSTOMERS}/{customer_id}", headers=admin).json()["data"]
        assert fetched["wallet"] == {"currency": "MYR", "balance": "0.00000000", "version": 0}


# --- 日志不含个人数据（REQ-PRIV-001） -------------------------------------------

PII_EMAIL = "pii-probe-7f3a@example.com"
PII_CONTACT = "Zebulon Quixote Harrington"
PII_PHONE = "+60 3-7788 9911"


def test_a_database_failure_does_not_log_personal_data(tmp_path) -> None:
    """数据库在 INSERT 时失败，全局处理器记下异常 —— 但异常文本里没有 SQL 参数。

    引擎由 `create_database_engine` 建（`create_app` 用的就是它，与生产同一个工厂），
    所以这一条验的是生产配置，不是测试里另配的引擎。
    """
    database = tmp_path / "billing.db"
    url = f"sqlite+pysqlite:///{database.as_posix()}"
    application = create_app(settings_for(tmp_path, database_url=url))
    engine = application.state.engine
    assert engine is not None
    Base.metadata.create_all(engine)
    headers = token_for(application, make_user(application))
    with engine.begin() as connection:
        connection.execute(text("DROP TABLE tenants"))

    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        response = TestClient(application, raise_server_exceptions=False).post(
            CUSTOMERS,
            json={
                "company_name": "Probe Sdn Bhd",
                "email": PII_EMAIL,
                "contact_name": PII_CONTACT,
                "phone": PII_PHONE,
            },
            headers=headers,
        )
    finally:
        root.removeHandler(handler)

    try:
        assert (response.status_code, error_code(response)) == (500, "INTERNAL_ERROR")
        logged = stream.getvalue()
        # 这次的异常确实进了日志 —— 否则下面几条是在空字符串上空转。
        assert "no such table: tenants" in logged
        assert "SQL parameters hidden" in logged
        for personal in (PII_EMAIL, PII_CONTACT, PII_PHONE):
            assert personal not in logged
            assert personal not in response.text
        # 库里没有新行（`tenants` 表已经删了，其余三张都是空的）。
        with application.state.session_factory() as session:
            counts = [count_rows(session, model) for model in (Wallet, Project, AuditLog)]
        assert counts == [0, 0, 0]
    finally:
        engine.dispose()

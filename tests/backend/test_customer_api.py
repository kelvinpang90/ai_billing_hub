"""Customer dependencies, `/customer/me`, `/me` and `/2fa/self/*` (design gate #368 v12).

覆盖设计 §7 里属于接口层（AIH-TASK-093）的行：

- 正常路径第二行：客户开启 2FA（`/2fa/self/enrol` → `/2fa/self/confirm` → 退出 → 登录），ADMIN 403、
  密码错、验证码错
- 只读契约：四种 `account_status`，`/customer/me`、一个挂了 `require_customer_writable` 的测试专用
  路由、`/2fa/self/enrol`
- 租户越权中关于客户端的两行、路由守卫、无租户的 CUSTOMER
- 停用后的 2FA 注册（含重新启用后的断言）、改密码后的旧会话（客户路径）中经 HTTP 的部分
- 两个响应的键集合逐字断言、`/api/v1/me` 的令牌过期、泄漏

门户账号的停用、启用直接调服务层（管理端接口归 AIH-TASK-094）。全部跑在内存 SQLite 上。
"""

from __future__ import annotations

import ast
import base64
import datetime as dt
import inspect
import io
import json
import logging
import os
import re
import textwrap

import pyotp
import pytest
from fastapi import FastAPI, Request
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select, update
from sqlalchemy.pool import StaticPool

from app.api.auth import require_customer_writable
from app.api.customer import router as customer_router
from app.core.config import Settings
from app.core.database import create_session_factory
from app.core.logging import JsonFormatter
from app.core.passwords import hash_password
from app.core.ratelimit import TokenBucket
from app.core.tokens import issue_access_token
from app.main import create_app
from app.models.auth import (
    AuditAction,
    AuditLog,
    DomainOutbox,
    RecoveryCode,
    TwoFactorSetting,
    User,
    UserRole,
    UserStatus,
)
from app.models.base import Base
from app.models.tenancy import AccountStatus, Tenant
from app.repositories import tenancy
from app.services.auth import STAGE_ENROL_2FA, STAGE_TOTP_REQUIRED, RequestContext, utc_now
from app.services.portal_accounts import (
    create_portal_account,
    disable_portal_account,
    enable_portal_account,
)

CUSTOMER_PREFIX = "/api/v1/customer"
CUSTOMER_ME = "/api/v1/customer/me"
ME = "/api/v1/me"
SELF_ENROL = "/api/v1/auth/2fa/self/enrol"
SELF_CONFIRM = "/api/v1/auth/2fa/self/confirm"
ENROL = "/api/v1/auth/2fa/enrol"
CONFIRM = "/api/v1/auth/2fa/confirm"
LOGIN = "/api/v1/auth/login"
# 测试专用的客户写路由：不在 `/api/v1/customer` 下，不混进路由枚举。
WRITE_PROBE = "/test-only/customer-write"

CONTEXT = RequestContext(ip_address="10.0.0.1", user_agent="pytest")
ADMIN_EMAIL = "admin@example.com"
ADMIN_PASSWORD = "an-admin-perfectly-fine-passphrase"
CUSTOMER_EMAIL = "portal@example.com"
OTHER_EMAIL = "other-portal@example.com"
PASSWORD = "a-customer-perfectly-fine-passphrase"
NEW_PASSWORD = "a-brand-new-customer-passphrase"
PLACEHOLDER_SESSION = "00000000-0000-0000-0000-000000000000"
CUSTOMER_ME_KEYS = {"customer_id", "company_name", "account_status", "read_only"}
ME_KEYS = {"role", "email", "two_factor_enabled"}
HTTP_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE"}


def build_app(tmp_path, **overrides) -> FastAPI:
    key = tmp_path / "jwt.key"
    key.write_text("a-test-signing-key-that-is-long-enough", encoding="utf-8")
    master = tmp_path / "master.key"
    master.write_text(f"1:{base64.b64encode(os.urandom(32)).decode()}\n", encoding="utf-8")
    application = create_app(
        Settings(
            jwt_secret_file=str(key),
            master_key_file=str(master),
            session_cookie_secure=False,
            auth_rate_limit_per_minute=600,
            auth_rate_limit_burst=200,
            frontend_base_url="https://billing.example.com",
            **overrides,
        )
    )
    # ⚠️ StaticPool：TestClient 在线程池里跑同步端点，`:memory:` 换个连接就是另一个空库。
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    application.state.engine = engine
    application.state.session_factory = create_session_factory(engine)
    application.add_api_route(WRITE_PROBE, write_probe, methods=["POST"])
    return application


def write_probe(request: Request) -> dict:
    """The test-only customer write: only the early refusal, no write of its own."""
    principal = require_customer_writable(request)
    return {"tenant_seen": principal.tenant_id is not None}


@pytest.fixture
def app(tmp_path):
    application = build_app(tmp_path)
    yield application
    application.state.engine.dispose()


@pytest.fixture
def client(app) -> TestClient:
    return TestClient(app)


@pytest.fixture
def admin(app) -> User:
    return make_user(app, email=ADMIN_EMAIL, password=ADMIN_PASSWORD)


# --- 帮手 -----------------------------------------------------------------------


def make_user(
    app,
    *,
    email: str,
    password: str,
    role: UserRole = UserRole.ADMIN,
    tenant_id: int | None = None,
) -> User:
    now = utc_now().replace(microsecond=0)
    with app.state.session_factory() as session:
        user = User(
            email=email,
            password_hash=hash_password(password),
            role=role,
            status=UserStatus.ACTIVE,
            tenant_id=tenant_id,
            activated_at=now if role is UserRole.CUSTOMER else None,
            created_at=now,
            updated_at=now,
        )
        session.add(user)
        session.commit()
        session.refresh(user)
        session.expunge(user)
        return user


def make_tenant(
    app, status: AccountStatus = AccountStatus.ENABLED, name: str = "Portal Test Sdn Bhd"
) -> Tenant:
    with app.state.session_factory() as session:
        tenant = tenancy.create_tenant(
            session, company_name=name, email="ops@example.com", now=utc_now()
        )
        tenant.account_status = status
        session.commit()
        session.refresh(tenant)
        session.expunge(tenant)
        return tenant


def make_customer(app, tenant: Tenant | None, email: str = CUSTOMER_EMAIL) -> User:
    return make_user(
        app,
        email=email,
        password=PASSWORD,
        role=UserRole.CUSTOMER,
        tenant_id=tenant.id if tenant is not None else None,
    )


def access_for(app, user: User, *, now: dt.datetime | None = None, version: int = 0) -> dict:
    token = issue_access_token(
        app.state.settings,
        user_id=user.id,
        role=user.role.value,
        session_id=PLACEHOLDER_SESSION,
        now=now or utc_now(),
        password_version=version,
    )
    return {"Authorization": f"Bearer {token}"}


def login(client: TestClient, email: str = CUSTOMER_EMAIL, password: str = PASSWORD) -> dict:
    response = client.post(LOGIN, json={"email": email, "password": password})
    assert response.status_code == 200, response.text
    return response.json()["data"]


def customer_login(client: TestClient, email: str = CUSTOMER_EMAIL) -> dict:
    """Sign a customer in over HTTP (no 2FA, not required): the headers of the new session."""
    data = login(client, email)
    assert data["stage"] is None
    assert data["access_token"]
    return {"Authorization": f"Bearer {data['access_token']}"}


def error_of(response) -> tuple[int, str]:
    return response.status_code, response.json()["error"]["code"]


def user_row(app, user_id: int) -> User:
    with app.state.session_factory() as session:
        user = session.get(User, user_id)
        assert user is not None
        session.expunge(user)
        return user


def two_factor_state(app, user_id: int) -> tuple[bool, bool, int]:
    """(has a settings row, that row is confirmed, recovery code rows)."""
    with app.state.session_factory() as session:
        row = session.get(TwoFactorSetting, user_id)
        codes = len(
            session.execute(select(RecoveryCode.id).where(RecoveryCode.user_id == user_id)).all()
        )
    return row is not None, row is not None and row.confirmed_at is not None, codes


def audit_count(app) -> int:
    with app.state.session_factory() as session:
        return len(session.execute(select(AuditLog.id)).all())


def self_enrol(client: TestClient, headers: dict, password: str = PASSWORD):
    return client.post(SELF_ENROL, json={"password": password}, headers=headers)


def self_confirm(client: TestClient, headers: dict, code: str):
    return client.post(SELF_CONFIRM, json={"code": code}, headers=headers)


def served_routes() -> set[tuple[str, str]]:
    """Every (method, path) under the customer prefix, from a freshly built app."""
    application = create_app(Settings(database_url="", jwt_secret_file=""))
    found = {
        (method.upper(), path)
        for path, operations in application.openapi()["paths"].items()
        if path.startswith(f"{CUSTOMER_PREFIX}/")
        for method in operations
        if method.upper() in HTTP_METHODS
    }
    for route in _walk(application.routes, set()):
        if isinstance(route, APIRoute) and route.path.startswith(f"{CUSTOMER_PREFIX}/"):
            found |= {(method, route.path) for method in route.methods}
    return found


def customer_handlers() -> tuple[set[tuple[str, str]], list]:
    """(method, path) pairs and handlers, straight from the customer router.

    不走 app 的路由树：子路由延迟挂载时那里可能一条都找不到。路由器带前缀，`route.path` 已是完整
    路径；守卫用例再把这些 (method, path) 与 `CUSTOMER_ROUTES` 对齐，app 新挂的客户接口不在
    这个路由器上也会失败。
    """
    pairs: set[tuple[str, str]] = set()
    handlers = []
    for route in customer_router.routes:
        if isinstance(route, APIRoute):
            pairs |= {(method, route.path) for method in route.methods}
            handlers.append(route.endpoint)
    return pairs, handlers


def _walk(routes, seen: set[int]):
    # 与 test_admin_customers_api.py 同一写法：子路由延迟挂载，逐层往里找。
    for route in routes:
        if id(route) in seen:
            continue
        seen.add(id(route))
        yield route
        children = getattr(route, "routes", None)
        if children is None:
            children = getattr(getattr(route, "router", None), "routes", None)
        if children:
            yield from _walk(children, seen)


CUSTOMER_ROUTES = sorted(served_routes())


# --- 正常路径：客户自己开启 2FA ---------------------------------------------------


def test_a_customer_turns_on_2fa_and_the_next_login_asks_for_a_code(app, client) -> None:
    tenant = make_tenant(app)
    customer = make_customer(app, tenant)
    headers = customer_login(client)

    enrolment = self_enrol(client, headers)
    assert enrolment.status_code == 200
    data = enrolment.json()["data"]
    assert set(data) == {"secret", "otpauth_uri"}
    secret = data["secret"]

    confirmed = self_confirm(client, headers, pyotp.TOTP(secret).now())
    assert confirmed.status_code == 200
    assert len(confirmed.json()["data"]["recovery_codes"]) == 10
    assert two_factor_state(app, customer.id) == (True, True, 10)
    assert client.get(ME, headers=headers).json()["data"]["two_factor_enabled"] is True
    with app.state.session_factory() as session:
        enabled = session.execute(
            select(AuditLog).where(AuditLog.action == AuditAction.TWO_FACTOR_ENABLED)
        ).scalar_one()
    assert enabled.actor_role == UserRole.CUSTOMER.value

    assert client.post("/api/v1/auth/logout").status_code == 200
    first = login(client)
    assert first["stage"] == STAGE_TOTP_REQUIRED
    assert first["access_token"] is None
    second = client.post(
        "/api/v1/auth/login/totp",
        json={"pending_token": first["pending_token"], "code": pyotp.TOTP(secret).now()},
    )
    assert second.status_code == 200
    assert second.json()["data"]["access_token"]


def test_admins_get_403_customer_required_on_both_self_endpoints(app, client, admin) -> None:
    """ADMIN 在登录时强制注册，不走这里（设计 §2）。"""
    headers = access_for(app, admin)

    enrol = self_enrol(client, headers, ADMIN_PASSWORD)
    confirm = self_confirm(client, headers, "000000")

    assert error_of(enrol) == (403, "CUSTOMER_REQUIRED")
    assert error_of(confirm) == (403, "CUSTOMER_REQUIRED")
    assert "secret" not in enrol.text
    assert two_factor_state(app, admin.id) == (False, False, 0)


def test_a_wrong_password_is_401_without_a_lockout_count_or_an_audit(app, client) -> None:
    """照 `/2fa/recovery-codes` 的先例：调用方已持有有效访问令牌。

    不计入登录锁定、不写审计、不写任何 2FA 行。
    """
    tenant = make_tenant(app)
    customer = make_customer(app, tenant)
    headers = customer_login(client)
    before_count = user_row(app, customer.id).failed_login_count
    before_audits = audit_count(app)

    response = self_enrol(client, headers, "not-the-password-at-all")

    assert error_of(response) == (401, "INVALID_CREDENTIALS")
    assert "secret" not in response.text
    assert user_row(app, customer.id).failed_login_count == before_count
    assert audit_count(app) == before_audits
    assert two_factor_state(app, customer.id) == (False, False, 0)


def test_a_wrong_code_is_401_invalid_totp_and_nothing_is_enabled(app, client) -> None:
    tenant = make_tenant(app)
    customer = make_customer(app, tenant)
    headers = customer_login(client)
    secret = self_enrol(client, headers).json()["data"]["secret"]
    wrong = "000000" if pyotp.TOTP(secret).now() != "000000" else "111111"

    response = self_confirm(client, headers, wrong)

    assert error_of(response) == (401, "INVALID_TOTP")
    assert two_factor_state(app, customer.id) == (True, False, 0)


def test_enrolling_again_after_2fa_is_on_is_409_without_a_new_secret(app, client) -> None:
    tenant = make_tenant(app)
    make_customer(app, tenant)
    headers = customer_login(client)
    secret = self_enrol(client, headers).json()["data"]["secret"]
    assert self_confirm(client, headers, pyotp.TOTP(secret).now()).status_code == 200

    again = self_enrol(client, headers)

    assert again.status_code == 409
    assert "secret" not in again.text


def test_self_enrol_takes_the_rate_limit_before_anything_else(app, client, admin) -> None:
    """设计 §2：`_rate_limit(request)` 是处理函数的第一步 —— 在鉴权与重验密码之前。"""
    app.state.auth_rate_limiter = TokenBucket(per_minute=1, burst=1)
    headers = access_for(app, admin)

    first = self_enrol(client, headers, ADMIN_PASSWORD)
    second = self_enrol(client, headers, ADMIN_PASSWORD)

    assert error_of(first) == (403, "CUSTOMER_REQUIRED")
    assert error_of(second) == (429, "TOO_MANY_REQUESTS")


def test_the_self_endpoints_are_under_the_auth_prefix() -> None:
    """落在 nginx 的认证限流区；前端对这个前缀下的 401 不触发刷新（设计 §2）。"""
    served = set(create_app(Settings(database_url="", jwt_secret_file="")).openapi()["paths"])
    for path in (SELF_ENROL, SELF_CONFIRM):
        assert path in served
        assert path.startswith("/api/v1/auth/")


# --- 只读契约 -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "read_only"),
    [
        (AccountStatus.PENDING_ACTIVATION, False),
        (AccountStatus.ENABLED, False),
        (AccountStatus.DISABLED, True),
        (AccountStatus.CLOSED, True),
    ],
)
def test_the_read_only_contract(app, client, status: AccountStatus, read_only: bool) -> None:
    """设计 §7「只读契约」：四种状态都能登录、能读。

    写依赖只在后两种拒绝；2FA 接口四种都允许。
    """
    tenant = make_tenant(app, status)
    make_customer(app, tenant)

    headers = customer_login(client)

    me = client.get(CUSTOMER_ME, headers=headers)
    assert me.status_code == 200
    assert me.json()["data"]["account_status"] == status.value
    assert me.json()["data"]["read_only"] is read_only

    write = client.post(WRITE_PROBE, headers=headers)
    if read_only:
        assert error_of(write) == (403, "ACCOUNT_READ_ONLY")
    else:
        assert write.status_code == 200
        assert write.json() == {"tenant_seen": True}

    enrol = self_enrol(client, headers)
    assert enrol.status_code == 200
    assert enrol.json()["data"]["secret"]


def test_the_writable_dependency_also_refuses_admins_and_anonymous_callers(
    app, client, admin
) -> None:
    assert error_of(client.post(WRITE_PROBE, headers=access_for(app, admin))) == (
        403,
        "CUSTOMER_REQUIRED",
    )
    assert error_of(client.post(WRITE_PROBE)) == (401, "TOKEN_INVALID")


# --- 租户越权 -------------------------------------------------------------------


def test_a_customer_always_sees_its_own_tenant_whatever_the_request_says(app, client) -> None:
    """INV-8：客户 A 带 B 的标识作查询参数与请求体，返回的始终是 A（spec §90）。"""
    mine = make_tenant(app, name="Mine Sdn Bhd")
    theirs = make_tenant(app, name="Theirs Sdn Bhd")
    make_customer(app, mine)
    make_customer(app, theirs, OTHER_EMAIL)
    headers = customer_login(client)

    plain = client.get(CUSTOMER_ME, headers=headers)
    forged = client.request(
        "GET",
        CUSTOMER_ME,
        params={
            "tenant_id": str(theirs.id),
            "customer_id": theirs.public_id,
            "id": theirs.public_id,
        },
        json={"tenant_id": theirs.id, "customer_id": theirs.public_id},
        headers=headers,
    )

    for response in (plain, forged):
        assert response.status_code == 200
        data = response.json()["data"]
        assert data["customer_id"] == mine.public_id
        assert data["company_name"] == "Mine Sdn Bhd"
    assert theirs.public_id not in forged.text
    assert "Theirs Sdn Bhd" not in forged.text


def test_a_customer_is_refused_by_the_admin_endpoints(app, client) -> None:
    make_customer(app, make_tenant(app))
    headers = customer_login(client)

    response = client.get("/api/v1/admin/customers", headers=headers)

    assert error_of(response) == (403, "ADMIN_REQUIRED")
    assert response.json()["data"] is None


def test_the_customer_routes_are_exactly_the_expected_ones() -> None:
    """既防枚举落空（下面的参数化用例一条都不跑也是绿的），也逼新增接口补进来。"""
    assert set(CUSTOMER_ROUTES) == {
        ("GET", CUSTOMER_ME),
        ("GET", f"{CUSTOMER_PREFIX}/wallet"),
        ("GET", f"{CUSTOMER_PREFIX}/wallet/transactions"),
        ("GET", f"{CUSTOMER_PREFIX}/projects"),
        ("GET", f"{CUSTOMER_PREFIX}/usage-events"),
        ("GET", f"{CUSTOMER_PREFIX}/usage-events/export"),
        ("GET", f"{CUSTOMER_PREFIX}/usage-summary"),
    }


@pytest.mark.parametrize(("method", "path"), CUSTOMER_ROUTES)
@pytest.mark.parametrize("caller", ["admin", "anonymous"])
def test_every_customer_route_refuses_admins_and_anonymous_callers(
    app, client, admin, method: str, path: str, caller: str
) -> None:
    make_customer(app, make_tenant(app))
    headers = access_for(app, admin) if caller == "admin" else {}

    response = client.request(method, path, headers=headers)

    expected = (403, "CUSTOMER_REQUIRED") if caller == "admin" else (401, "TOKEN_INVALID")
    assert error_of(response) == expected
    assert response.json()["data"] is None


# --- 路由守卫 -------------------------------------------------------------------


def test_every_customer_handler_calls_the_customer_dependency_first() -> None:
    """设计 §2「依赖」：函数体第一条语句（跳过 docstring）是对 `require_customer` 或
    `require_customer_writable` 的调用。与管理端 `require_admin` 的守卫同一写法。"""
    pairs, handlers = customer_handlers()
    assert handlers, "枚举落空"
    assert pairs == set(CUSTOMER_ROUTES)
    for handler in handlers:
        source = textwrap.dedent(inspect.getsource(handler))
        function = ast.parse(source).body[0]
        assert isinstance(function, ast.FunctionDef)
        body = function.body
        if ast.get_docstring(function) is not None:
            body = body[1:]
        first = body[0]
        call = first.value if isinstance(first, ast.Assign | ast.Expr) else None
        assert isinstance(call, ast.Call), handler.__name__
        assert isinstance(call.func, ast.Name), handler.__name__
        assert call.func.id in {"require_customer", "require_customer_writable"}, handler.__name__


# --- 无租户的 CUSTOMER ----------------------------------------------------------


def test_a_customer_without_a_tenant_is_403_and_no_tenant_data_is_read(app, client) -> None:
    """设计 §9 备选 F：数据库允许这样的行，`require_customer` 把它拒在所有客户接口之外。"""
    make_tenant(app)
    orphan = make_customer(app, None)
    headers = access_for(app, orphan)
    statements: list[str] = []

    def record(_conn, _cursor, statement, _params, _context, _many) -> None:
        statements.append(statement)

    event.listen(app.state.engine, "before_cursor_execute", record)
    try:
        me = client.get(CUSTOMER_ME, headers=headers)
        enrol = self_enrol(client, headers)
        write = client.post(WRITE_PROBE, headers=headers)
    finally:
        event.remove(app.state.engine, "before_cursor_execute", record)

    for response in (me, enrol, write):
        assert error_of(response) == (403, "CUSTOMER_REQUIRED")
    assert "secret" not in enrol.text
    assert two_factor_state(app, orphan.id) == (False, False, 0)
    assert statements, "监听落空"
    table = re.compile(rf"\b{Tenant.__tablename__}\b")
    assert not [statement for statement in statements if table.search(statement)]


# --- 停用后的 2FA 注册 ----------------------------------------------------------


def test_an_admin_disabled_after_getting_an_enrol_token_cannot_enrol(app, client, admin) -> None:
    pending = login(client, ADMIN_EMAIL, ADMIN_PASSWORD)
    assert pending["stage"] == STAGE_ENROL_2FA
    # ADMIN 没有停用接口：直接写库（与 CLI 的效果相同）。
    with app.state.session_factory() as session:
        session.execute(update(User).where(User.id == admin.id).values(status=UserStatus.DISABLED))
        session.commit()

    enrol = client.post(ENROL, json={"pending_token": pending["pending_token"]})
    confirm = client.post(
        CONFIRM, json={"pending_token": pending["pending_token"], "code": "000000"}
    )

    assert error_of(enrol) == (401, "TOKEN_INVALID")
    assert error_of(confirm) == (401, "TOKEN_INVALID")
    assert "secret" not in enrol.text
    assert two_factor_state(app, admin.id) == (False, False, 0)


def test_a_required_customer_disabled_after_getting_an_enrol_token_cannot_enrol(
    tmp_path,
) -> None:
    """停用把版本号 +1：重新启用之后，停用前拿到的 pending 令牌仍然无效；重新登录照常注册。"""
    application = build_app(tmp_path, customer_2fa_required=True)
    try:
        client = TestClient(application)
        admin = make_user(application, email=ADMIN_EMAIL, password=ADMIN_PASSWORD)
        tenant = make_tenant(application)
        customer = make_customer(application, tenant)
        pending = login(client)
        assert pending["stage"] == STAGE_ENROL_2FA
        old = pending["pending_token"]
        factory = application.state.session_factory

        disable_portal_account(factory, actor=admin, customer_id=tenant.public_id, context=CONTEXT)
        for response in (
            client.post(ENROL, json={"pending_token": old}),
            client.post(CONFIRM, json={"pending_token": old, "code": "000000"}),
        ):
            assert error_of(response) == (401, "TOKEN_INVALID")
        assert two_factor_state(application, customer.id) == (False, False, 0)

        enable_portal_account(factory, actor=admin, customer_id=tenant.public_id, context=CONTEXT)
        assert user_row(application, customer.id).status is UserStatus.ACTIVE
        for response in (
            client.post(ENROL, json={"pending_token": old}),
            client.post(CONFIRM, json={"pending_token": old, "code": "000000"}),
        ):
            assert error_of(response) == (401, "TOKEN_INVALID")
        assert two_factor_state(application, customer.id) == (False, False, 0)

        fresh = login(client)
        assert fresh["stage"] == STAGE_ENROL_2FA
        enrolment = client.post(ENROL, json={"pending_token": fresh["pending_token"]})
        assert enrolment.status_code == 200
        secret = enrolment.json()["data"]["secret"]
        confirmed = client.post(
            CONFIRM,
            json={"pending_token": fresh["pending_token"], "code": pyotp.TOTP(secret).now()},
        )
        assert confirmed.status_code == 200
        assert two_factor_state(application, customer.id) == (True, True, 10)
    finally:
        application.state.engine.dispose()


def test_a_customer_disabled_between_self_enrol_and_self_confirm(app, client, admin) -> None:
    tenant = make_tenant(app)
    customer = make_customer(app, tenant)
    headers = customer_login(client)
    secret = self_enrol(client, headers).json()["data"]["secret"]
    factory = app.state.session_factory

    disable_portal_account(factory, actor=admin, customer_id=tenant.public_id, context=CONTEXT)
    refused = self_confirm(client, headers, pyotp.TOTP(secret).now())

    assert error_of(refused) == (401, "TOKEN_INVALID")
    assert two_factor_state(app, customer.id) == (True, False, 0)

    enable_portal_account(factory, actor=admin, customer_id=tenant.public_id, context=CONTEXT)
    still = self_confirm(client, headers, pyotp.TOTP(secret).now())
    assert error_of(still) == (401, "TOKEN_INVALID"), "启用不会让停用前的访问令牌复活"
    assert two_factor_state(app, customer.id) == (True, False, 0)

    fresh = customer_login(client)
    again = self_enrol(client, fresh).json()["data"]["secret"]
    assert self_confirm(client, fresh, pyotp.TOTP(again).now()).status_code == 200
    assert two_factor_state(app, customer.id) == (True, True, 10)


# --- 改密码后的旧会话（客户路径） -------------------------------------------------


def test_a_password_reset_between_self_enrol_and_self_confirm_ends_the_old_session(
    app, client
) -> None:
    tenant = make_tenant(app)
    customer = make_customer(app, tenant)
    headers = customer_login(client)
    secret = self_enrol(client, headers).json()["data"]["secret"]

    assert (
        client.post("/api/v1/auth/password/forgot", json={"email": CUSTOMER_EMAIL}).status_code
        == 200
    )
    with app.state.session_factory() as session:
        row = session.execute(
            select(DomainOutbox).order_by(DomainOutbox.id.desc()).limit(1)
        ).scalar_one()
        reset_token = str(json.loads(row.payload_json or "{}")["token"])
    reset = client.post(
        "/api/v1/auth/password/reset",
        json={"token": reset_token, "new_password": NEW_PASSWORD},
    )
    assert reset.status_code == 200

    response = self_confirm(client, headers, pyotp.TOTP(secret).now())

    assert error_of(response) == (401, "TOKEN_INVALID")
    assert two_factor_state(app, customer.id) == (True, False, 0)


def test_accepting_an_invitation_ends_a_token_signed_before_it(app, client, admin) -> None:
    tenant = make_tenant(app)
    issued = create_portal_account(
        app.state.session_factory,
        app.state.settings,
        actor=admin,
        customer_id=tenant.public_id,
        email=CUSTOMER_EMAIL,
        context=CONTEXT,
    )
    with app.state.session_factory() as session:
        outbox = session.get(DomainOutbox, issued.outbox_id)
        assert outbox is not None
        invitation = str(json.loads(outbox.payload_json or "{}")["token"])
        user = session.execute(select(User).where(User.tenant_id == tenant.id)).scalar_one()
        session.expunge(user)
    # 接受之前、在别处签发的同一用户的令牌（版本 0）。
    before = access_for(app, user, version=0)

    accepted = client.post(
        "/api/v1/auth/invitation/accept", json={"token": invitation, "password": PASSWORD}
    )
    assert accepted.status_code == 200

    assert error_of(client.get(ME, headers=before)) == (401, "TOKEN_INVALID")
    assert user_row(app, user.id).password_version == 1
    assert client.get(ME, headers=customer_login(client)).status_code == 200


# --- 响应的键集合 ---------------------------------------------------------------


def test_customer_me_has_exactly_four_keys(app, client) -> None:
    """INV-7：没有任何金额、成本、毛利、内部 id、`billing_mode`。"""
    tenant = make_tenant(app, AccountStatus.ENABLED)
    make_customer(app, tenant)

    response = client.get(CUSTOMER_ME, headers=customer_login(client))

    assert response.status_code == 200
    data = response.json()["data"]
    assert set(data) == CUSTOMER_ME_KEYS
    assert data == {
        "customer_id": tenant.public_id,
        "company_name": "Portal Test Sdn Bhd",
        "account_status": "ENABLED",
        "read_only": False,
    }


@pytest.mark.parametrize("role", [UserRole.CUSTOMER, UserRole.ADMIN])
def test_me_has_exactly_three_keys_for_any_signed_in_user(app, client, admin, role) -> None:
    if role is UserRole.CUSTOMER:
        user = make_customer(app, make_tenant(app))
        headers = customer_login(client)
    else:
        user = admin
        headers = access_for(app, admin)

    response = client.get(ME, headers=headers)

    assert response.status_code == 200
    data = response.json()["data"]
    assert set(data) == ME_KEYS
    assert data == {"role": role.value, "email": user.email, "two_factor_enabled": False}


def test_me_with_an_expired_or_missing_token_is_401_token_invalid(app, client) -> None:
    customer = make_customer(app, make_tenant(app))
    long_ago = utc_now() - dt.timedelta(seconds=app.state.settings.access_token_ttl_seconds + 60)

    expired = client.get(ME, headers=access_for(app, customer, now=long_ago))
    missing = client.get(ME)

    assert error_of(expired) == (401, "TOKEN_INVALID")
    assert error_of(missing) == (401, "TOKEN_INVALID")


def test_me_is_not_under_the_auth_prefix_and_not_rate_limited(app, client) -> None:
    """设计 §2：前端对 `/api/v1/auth/` 下的 401 不触发刷新，而且那里受登录限流。"""
    served = set(create_app(Settings(database_url="", jwt_secret_file="")).openapi()["paths"])
    assert ME in served
    assert not ME.startswith("/api/v1/auth/")

    customer = make_customer(app, make_tenant(app))
    headers = access_for(app, customer)
    app.state.auth_rate_limiter = TokenBucket(per_minute=1, burst=1)
    assert [client.get(ME, headers=headers).status_code for _ in range(3)] == [200, 200, 200]


# --- 泄漏 -----------------------------------------------------------------------


def test_secrets_never_reach_logs_audit_or_the_wrong_responses(app, client) -> None:
    """spec §94：密码、TOTP 密钥、`otpauth_uri`、恢复码不进日志、审计、异常文本或别的响应。"""
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        tenant = make_tenant(app)
        make_customer(app, tenant)
        headers = customer_login(client)
        wrong = self_enrol(client, headers, "not-the-password-at-all")
        enrolment = self_enrol(client, headers).json()["data"]
        secret = enrolment["secret"]
        bad_code = self_confirm(client, headers, "abcdef")
        codes = self_confirm(client, headers, pyotp.TOTP(secret).now()).json()["data"][
            "recovery_codes"
        ]
        me = client.get(ME, headers=headers)
        customer_me = client.get(CUSTOMER_ME, headers=headers)
    finally:
        root.removeHandler(handler)

    logged = stream.getvalue()
    with app.state.session_factory() as session:
        audit_text = json.dumps(
            [
                [row.before_state, row.after_state, row.reason]
                for row in session.execute(select(AuditLog)).scalars()
            ]
        )
    others = wrong.text + bad_code.text + me.text + customer_me.text
    for haystack in (logged, audit_text, others):
        assert secret not in haystack
        assert "otpauth://" not in haystack
        assert PASSWORD not in haystack
        assert "$argon2" not in haystack
        for code in codes:
            assert code not in haystack
    for key in ("password", "tenant_id", "billing_mode", "balance"):
        assert key not in me.text
        assert key not in customer_me.text
    # 日志确实产生了 —— 否则上面几条断言是在空字符串上空转。
    assert [json.loads(line) for line in logged.splitlines() if line.strip()]

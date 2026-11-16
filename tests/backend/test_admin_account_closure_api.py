"""Admin account closure endpoints on SQLite.

Design gate #707 v4 §2「接口」, the §7 rows of AIH-TASK-213.
覆盖 §7「接口」与「余额边界」两行：

- **鉴权**：两个接口匿名 401、CUSTOMER 403，且不写库（全部管理端路由的枚举用例在
  test_admin_customers_api.py，这里对关户再显式测一次，客户是能被关掉的 `DISABLED`、余额 0）；
- **`closure-check`**：字段恰好是设计列出的八个；`blockers` 顺序同 POST 的判定顺序；
  `CLOSED` 时为空；网关事件扫描到上限时的截断标志；只读、不写审计；404；没有钱包 500；
- **`close`**：200（状态、版本、`status_changed_at`、审计带去空白的原因、一条出站事件）、
  已关户 200 不写、各 409（各自什么都不写）、404、
  422（缺原因、空白、256 码点、多余字段 `account_status`，且 422 先于 404）、没有钱包 500；
- **账户状态接口不变**：`CLOSED` 作目标 422，从 `CLOSED` 出发 409；
- **余额边界**：余额 0 与 `0.00000001` 时 `balance` 为 `"0.00000000"` 与 `"0.00000001"`，
  后者 `blockers` 含 `ACCOUNT_CLOSURE_BALANCE_NOT_ZERO`。

世界沿用 test_usage_billing.py 的 `World`；未结的用量、AI outbox、支付与网关事件用
test_account_closure_service.py 的造数帮手直接写表。关户的锁、事务与并发在服务层测试
（test_account_closure_service.py、test_account_closure_concurrency.py，后者在真 MySQL 上），
这里只测接口契约，全部在 SQLite 上跑，没有需要 MySQL 的用例。

时间注入：替换 `account_closure.utc_now`，两个接口的 `now` 都是 `CLOSE_AT`。uuid 与金额都是虚构的。
"""

from __future__ import annotations

import ast
import inspect
import textwrap
import uuid
from collections.abc import Callable, Iterator
from decimal import Decimal
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, delete, func, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from test_account_closure_service import (
    CLOSE_AT,
    REASON,
    add_ai_usage,
    add_gateway_event,
    add_payment,
    add_usage,
    disabled,
    set_balance,
    snapshot,
    status_audits,
    status_events,
)
from test_usage_billing import T0, TEST_EMAIL_DOMAIN, Tenancy, seed_meter_types
from test_usage_billing import World as BillingWorld

from app.ai_api.outbox import AiUsageOutboxStatus
from app.api import admin_customers
from app.core.config import Settings
from app.core.database import create_session_factory
from app.core.tokens import issue_access_token
from app.main import create_app
from app.models.auth import AuditLog, User, UserRole, UserStatus
from app.models.base import Base
from app.models.payments import PaymentEventProcessingStatus, PaymentStatus
from app.models.tenancy import AccountStatus
from app.models.usage import UsageEventStatus
from app.models.wallet import Wallet
from app.services import account_closure
from app.services.auth import utc_now

CUSTOMERS = "/api/v1/admin/customers"
ENVELOPE_FIELDS = {"success", "data", "error", "request_id"}

# 设计 §2「接口」的字段白名单：断言「恰好等于」，多一个键（成本、内部 id、金额明细）就红。
CHECK_FIELDS = {
    "account_status",
    "balance",
    "unresolved_usage_events",
    "unresolved_ai_usage",
    "unresolved_payments",
    "unresolved_payment_events",
    "payment_events_truncated",
    "blockers",
}
DETAIL_FIELDS = {
    "id",
    "company_name",
    "contact_name",
    "email",
    "phone",
    "billing_status",
    "billing_mode",
    "ai_service_enabled",
    "account_status",
    "status_version",
    "created_at",
    "updated_at",
    "wallet",
}

TRANSITION_INVALID = "ACCOUNT_STATUS_TRANSITION_INVALID"
BALANCE_NOT_ZERO = "ACCOUNT_CLOSURE_BALANCE_NOT_ZERO"
USAGE_PENDING = "ACCOUNT_CLOSURE_USAGE_PENDING"
AI_USAGE_PENDING = "ACCOUNT_CLOSURE_AI_USAGE_PENDING"
PAYMENTS_PENDING = "ACCOUNT_CLOSURE_PAYMENTS_PENDING"
PAYMENT_EVENTS_PENDING = "ACCOUNT_CLOSURE_PAYMENT_EVENTS_PENDING"

# 全零占位值（secret-scan）：一个不存在的客户。
ZERO_UUID = "00000000-0000-4000-8000-000000000000"


# --- 夹具 -----------------------------------------------------------------------
# 本任务的可改路径不含 conftest：夹具写在这里（CLAUDE.md「写后端测试的约定」）。


@pytest.fixture
def factory() -> Iterator[sessionmaker[Session]]:
    # StaticPool：TestClient 在线程池里跑同步端点，内存库必须所有连接共用一个
    # （与 test_admin_customers_api.py 的 app 夹具同理）。
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    session_factory = create_session_factory(engine)
    seed_meter_types(session_factory)
    yield session_factory
    engine.dispose()


@pytest.fixture
def world(factory) -> BillingWorld:
    return BillingWorld(factory)


@pytest.fixture(autouse=True)
def clock(monkeypatch) -> None:
    """Both endpoints' `now` is `CLOSE_AT` (the service reads `utc_now` when `now` is omitted)."""
    monkeypatch.setattr(account_closure, "utc_now", lambda: CLOSE_AT)


@pytest.fixture
def app(tmp_path, world) -> FastAPI:
    key = tmp_path / "jwt.key"
    key.write_text("a-test-signing-key-that-is-long-enough", encoding="utf-8")
    application = create_app(Settings(jwt_secret_file=str(key), database_url=""))
    application.state.session_factory = world.factory
    return application


@pytest.fixture
def client(app) -> TestClient:
    # 500 要以统一错误体返回，而不是在测试里抛出来。
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def admin(app, world) -> dict[str, str]:
    return token_for(app, world.admin.id, "ADMIN")


# --- 帮手 -----------------------------------------------------------------------


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


def customer_headers(application: FastAPI, world: BillingWorld) -> dict[str, str]:
    with world.factory() as session:
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


def check_url(customer_id: str) -> str:
    return f"{CUSTOMERS}/{customer_id}/closure-check"


def close_url(customer_id: str) -> str:
    return f"{CUSTOMERS}/{customer_id}/close"


def error_code(response) -> str:
    body = response.json()
    assert set(body) == ENVELOPE_FIELDS
    assert body["success"] is False
    assert body["data"] is None
    return body["error"]["code"]


def checked(client: TestClient, admin: dict[str, str], tenant: Tenancy) -> dict:
    response = client.get(check_url(tenant.public_id), headers=admin)
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == ENVELOPE_FIELDS
    assert body["success"] is True
    assert body["error"] is None
    assert set(body["data"]) == CHECK_FIELDS
    return body["data"]


def audit_count(world: BillingWorld) -> int:
    with world.factory() as session:
        return session.execute(select(func.count()).select_from(AuditLog)).scalar_one()


def drop_wallet(world: BillingWorld, tenant: Tenancy) -> None:
    """A data defect: the tenant has no wallet (SQLite here has no wallet triggers)."""
    with world.factory() as session:
        session.execute(delete(Wallet).where(Wallet.tenant_id == tenant.id))
        session.commit()


def closed(world: BillingWorld, client: TestClient, admin: dict[str, str]) -> Tenancy:
    """A customer closed through the endpoint."""
    tenant = disabled(world)
    response = client.post(close_url(tenant.public_id), json={"reason": REASON}, headers=admin)
    assert response.status_code == 200, response.text
    return tenant


# --- 鉴权 -----------------------------------------------------------------------


@pytest.mark.parametrize("caller", ["anonymous", "customer"])
@pytest.mark.parametrize("method", ["GET", "POST"], ids=["closure-check", "close"])
def test_non_admins_are_refused_and_nothing_is_written(
    client, app, world, method: str, caller: str
) -> None:
    """匿名 401、CUSTOMER 403。客户可以被关掉：漏了鉴权的 `close` 会真的把它关了。"""
    tenant = disabled(world)
    headers = {} if caller == "anonymous" else customer_headers(app, world)
    before = snapshot(world, tenant)
    audits = audit_count(world)
    url = check_url(tenant.public_id) if method == "GET" else close_url(tenant.public_id)
    body = {"reason": REASON} if method == "POST" else None

    response = client.request(method, url, json=body, headers=headers)

    expected = (401, "TOKEN_INVALID") if caller == "anonymous" else (403, "ADMIN_REQUIRED")
    assert (response.status_code, error_code(response)) == expected
    assert snapshot(world, tenant) == before
    assert audit_count(world) == audits


@pytest.mark.parametrize(
    "handler",
    [admin_customers.get_closure_check, admin_customers.close_account],
    ids=["closure-check", "close"],
)
def test_the_handlers_call_require_admin_first(handler: Callable[..., Any]) -> None:
    """设计 §6「鉴权」：函数体第一条语句（跳过 docstring）是对 `require_admin` 的调用。"""
    source = textwrap.dedent(inspect.getsource(handler))
    [function] = ast.parse(source).body
    assert isinstance(function, ast.FunctionDef)
    body = function.body
    if ast.get_docstring(function) is not None:
        body = body[1:]
    first = body[0]
    call = first.value if isinstance(first, ast.Assign | ast.Expr) else None
    assert isinstance(call, ast.Call)
    assert isinstance(call.func, ast.Name) and call.func.id == "require_admin"


# --- closure-check --------------------------------------------------------------


def test_the_check_has_exactly_the_listed_fields(client, admin, world) -> None:
    tenant = disabled(world)

    data = checked(client, admin, tenant)

    assert data == {
        "account_status": "DISABLED",
        "balance": "0.00000000",
        "unresolved_usage_events": 0,
        "unresolved_ai_usage": 0,
        "unresolved_payments": 0,
        "unresolved_payment_events": 0,
        "payment_events_truncated": False,
        "blockers": [],
    }


def test_the_blockers_follow_the_order_of_the_close(client, admin, world) -> None:
    """Every condition unmet at once: all six codes, in the order POST checks them."""
    tenant = world.tenant()
    set_balance(world, tenant, Decimal("12.50000000"))
    add_usage(world, tenant, UsageEventStatus.RECEIVED)
    add_usage(world, tenant, UsageEventStatus.PRICING_ERROR)
    add_ai_usage(world, tenant, AiUsageOutboxStatus.PENDING)
    payment = add_payment(world, tenant, PaymentStatus.PENDING)
    add_gateway_event(
        world,
        PaymentEventProcessingStatus.RECEIVED,
        metadata={"client_reference_id": payment.public_id},
    )
    before = snapshot(world, tenant)
    audits = audit_count(world)

    data = checked(client, admin, tenant)

    assert data == {
        "account_status": "ENABLED",
        "balance": "12.50000000",
        "unresolved_usage_events": 2,
        "unresolved_ai_usage": 1,
        "unresolved_payments": 1,
        "unresolved_payment_events": 1,
        "payment_events_truncated": False,
        "blockers": [
            TRANSITION_INVALID,
            BALANCE_NOT_ZERO,
            USAGE_PENDING,
            AI_USAGE_PENDING,
            PAYMENTS_PENDING,
            PAYMENT_EVENTS_PENDING,
        ],
    }
    # 只读：不写库、不写审计。
    assert snapshot(world, tenant) == before
    assert audit_count(world) == audits


@pytest.mark.parametrize("status", [AccountStatus.ENABLED, AccountStatus.PENDING_ACTIVATION])
def test_a_status_other_than_disabled_comes_first(client, admin, world, status) -> None:
    tenant = disabled(world)
    world.set_account_status(tenant, status)

    data = checked(client, admin, tenant)

    assert (data["account_status"], data["blockers"]) == (status.value, [TRANSITION_INVALID])


def test_a_closed_account_has_no_blockers(client, admin, world) -> None:
    """`CLOSED`: an empty list even with a balance and unresolved rows; the counts still show."""
    tenant = closed(world, client, admin)
    set_balance(world, tenant, Decimal("0.00000001"))
    add_ai_usage(world, tenant, AiUsageOutboxStatus.HELD)

    data = checked(client, admin, tenant)

    assert data["account_status"] == "CLOSED"
    assert data["balance"] == "0.00000001"
    assert data["unresolved_ai_usage"] == 1
    assert data["blockers"] == []


def test_a_full_scan_is_flagged_and_blocks(client, admin, world, monkeypatch) -> None:
    """The scan limit reached by another tenant's events: no count of ours, but truncated."""
    monkeypatch.setattr(account_closure, "PAYMENT_EVENT_SCAN_LIMIT", 1)
    tenant = disabled(world)
    other = world.tenant("b")
    payment = add_payment(world, other, PaymentStatus.PAID)
    add_gateway_event(
        world,
        PaymentEventProcessingStatus.RETRY,
        metadata={"client_reference_id": payment.public_id},
    )

    data = checked(client, admin, tenant)

    assert data["unresolved_payment_events"] == 0
    assert data["payment_events_truncated"] is True
    assert data["blockers"] == [PAYMENT_EVENTS_PENDING]


def test_the_check_of_an_unknown_customer_is_404(client, admin) -> None:
    response = client.get(check_url(ZERO_UUID), headers=admin)

    assert (response.status_code, error_code(response)) == (404, "CUSTOMER_NOT_FOUND")


def test_the_check_without_a_wallet_is_500(client, admin, world) -> None:
    tenant = disabled(world)
    drop_wallet(world, tenant)

    response = client.get(check_url(tenant.public_id), headers=admin)

    assert (response.status_code, error_code(response)) == (500, "INTERNAL_ERROR")


# --- 余额边界 -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("balance", "shown", "blockers"),
    [
        (Decimal("0"), "0.00000000", []),
        (Decimal("0.00000001"), "0.00000001", [BALANCE_NOT_ZERO]),
    ],
    ids=["zero", "one-unit"],
)
def test_the_balance_is_exact(client, admin, world, balance, shown, blockers) -> None:
    tenant = disabled(world)
    set_balance(world, tenant, balance)

    data = checked(client, admin, tenant)

    assert (data["balance"], data["blockers"]) == (shown, blockers)


# --- close ----------------------------------------------------------------------


def test_closing_returns_the_closed_customer(client, admin, world) -> None:
    tenant = disabled(world)
    version = world.tenant_row(tenant).status_version

    # 原因先去首尾空白再存。
    response = client.post(
        close_url(tenant.public_id), json={"reason": f"  {REASON}  "}, headers=admin
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == ENVELOPE_FIELDS
    assert (body["success"], body["error"]) == (True, None)
    data = body["data"]
    assert set(data) == DETAIL_FIELDS
    assert data["id"] == tenant.public_id
    assert data["account_status"] == "CLOSED"
    assert data["ai_service_enabled"] is False
    assert data["status_version"] == version + 1
    assert data["wallet"]["balance"] == "0.00000000"
    row = world.tenant_row(tenant)
    assert (row.account_status, row.status_changed_at) == (AccountStatus.CLOSED, CLOSE_AT)
    [audit] = status_audits(world, tenant)
    assert audit.reason == REASON
    assert len(status_events(world, tenant)) == 1


def test_closing_a_closed_account_is_200_and_writes_nothing(client, admin, world) -> None:
    tenant = closed(world, client, admin)
    before = snapshot(world, tenant)
    audits = audit_count(world)

    response = client.post(
        close_url(tenant.public_id), json={"reason": "A different reason"}, headers=admin
    )

    assert response.status_code == 200, response.text
    assert response.json()["data"]["account_status"] == "CLOSED"
    assert snapshot(world, tenant) == before
    assert audit_count(world) == audits
    assert [audit.reason for audit in status_audits(world, tenant)] == [REASON]


def _status(status: AccountStatus) -> Callable[[BillingWorld, Tenancy], None]:
    return lambda world, tenant: world.set_account_status(tenant, status)


def _balance(amount: str) -> Callable[[BillingWorld, Tenancy], None]:
    return lambda world, tenant: set_balance(world, tenant, Decimal(amount))


def _usage(world: BillingWorld, tenant: Tenancy) -> None:
    add_usage(world, tenant, UsageEventStatus.RECEIVED)


def _ai_usage(world: BillingWorld, tenant: Tenancy) -> None:
    add_ai_usage(world, tenant, AiUsageOutboxStatus.SENDING)


def _payment(world: BillingWorld, tenant: Tenancy) -> None:
    add_payment(world, tenant, PaymentStatus.REVIEW_REQUIRED)


def _payment_event(world: BillingWorld, tenant: Tenancy) -> None:
    # 支付本身已是终结的 PAID，只有网关事件未结。
    payment = add_payment(world, tenant, PaymentStatus.PAID)
    add_gateway_event(world, PaymentEventProcessingStatus.PROCESSING, payment_id=payment.id)


@pytest.mark.parametrize(
    ("make", "code"),
    [
        (_status(AccountStatus.ENABLED), TRANSITION_INVALID),
        (_status(AccountStatus.PENDING_ACTIVATION), TRANSITION_INVALID),
        (_balance("0.00000001"), BALANCE_NOT_ZERO),
        (_balance("-0.00000001"), BALANCE_NOT_ZERO),
        (_usage, USAGE_PENDING),
        (_ai_usage, AI_USAGE_PENDING),
        (_payment, PAYMENTS_PENDING),
        (_payment_event, PAYMENT_EVENTS_PENDING),
    ],
    ids=[
        "enabled",
        "pending-activation",
        "balance-positive",
        "balance-negative",
        "usage",
        "ai-usage",
        "payment",
        "payment-event",
    ],
)
def test_each_unmet_condition_is_409_and_writes_nothing(client, admin, world, make, code) -> None:
    tenant = disabled(world)
    make(world, tenant)
    before = snapshot(world, tenant)
    audits = audit_count(world)

    response = client.post(close_url(tenant.public_id), json={"reason": REASON}, headers=admin)

    assert (response.status_code, error_code(response)) == (409, code)
    assert snapshot(world, tenant) == before
    assert audit_count(world) == audits


def test_only_the_first_unmet_condition_is_reported(client, admin, world) -> None:
    tenant = disabled(world)
    set_balance(world, tenant, Decimal("1"))
    add_usage(world, tenant, UsageEventStatus.RECEIVED)

    response = client.post(close_url(tenant.public_id), json={"reason": REASON}, headers=admin)

    assert (response.status_code, error_code(response)) == (409, BALANCE_NOT_ZERO)


def test_closing_an_unknown_customer_is_404(client, admin, world) -> None:
    audits = audit_count(world)

    response = client.post(close_url(ZERO_UUID), json={"reason": REASON}, headers=admin)

    assert (response.status_code, error_code(response)) == (404, "CUSTOMER_NOT_FOUND")
    assert audit_count(world) == audits


def test_closing_without_a_wallet_is_500(client, admin, world) -> None:
    tenant = disabled(world)
    drop_wallet(world, tenant)

    response = client.post(close_url(tenant.public_id), json={"reason": REASON}, headers=admin)

    assert (response.status_code, error_code(response)) == (500, "INTERNAL_ERROR")
    assert world.tenant_row(tenant).account_status is AccountStatus.DISABLED
    assert status_audits(world, tenant) == []


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"reason": None},
        {"reason": ""},
        {"reason": "   "},
        # 256 个码点（每个在 UTF-8 里两字节）：按码点计长度。
        {"reason": "é" * 256},
        {"reason": REASON, "account_status": "CLOSED"},
    ],
    ids=["missing", "null", "empty", "blank", "256-code-points", "extra-account_status"],
)
def test_invalid_bodies_are_422_and_write_nothing(client, admin, world, body: dict) -> None:
    tenant = disabled(world)
    before = snapshot(world, tenant)

    response = client.post(close_url(tenant.public_id), json=body, headers=admin)

    assert (response.status_code, error_code(response)) == (422, "VALIDATION_ERROR")
    assert snapshot(world, tenant) == before


def test_255_code_points_are_accepted(client, admin, world) -> None:
    tenant = disabled(world)

    response = client.post(close_url(tenant.public_id), json={"reason": "é" * 255}, headers=admin)

    assert response.status_code == 200, response.text
    [audit] = status_audits(world, tenant)
    assert audit.reason == "é" * 255


def test_the_body_is_checked_before_the_customer(client, admin) -> None:
    """判定顺序：422（请求校验）→ 404。"""
    response = client.post(close_url(ZERO_UUID), json={}, headers=admin)

    assert (response.status_code, error_code(response)) == (422, "VALIDATION_ERROR")


# --- 账户状态接口不变 -----------------------------------------------------------


def test_the_account_status_endpoint_still_refuses_closed_as_a_target(client, admin, world) -> None:
    tenant = disabled(world)
    before = snapshot(world, tenant)

    response = client.post(
        f"{CUSTOMERS}/{tenant.public_id}/account-status",
        json={"account_status": "CLOSED", "reason": REASON},
        headers=admin,
    )

    assert (response.status_code, error_code(response)) == (422, "VALIDATION_ERROR")
    assert snapshot(world, tenant) == before


def test_the_account_status_endpoint_cannot_reopen_a_closed_account(client, admin, world) -> None:
    tenant = closed(world, client, admin)
    before = snapshot(world, tenant)

    response = client.post(
        f"{CUSTOMERS}/{tenant.public_id}/account-status",
        json={"account_status": "ENABLED", "reason": REASON},
        headers=admin,
    )

    assert (response.status_code, error_code(response)) == (409, TRANSITION_INVALID)
    assert snapshot(world, tenant) == before

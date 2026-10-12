"""Project integration status and status webhook endpoints (AIH-TASK-050; design gate #241 v6).

覆盖设计 §2「接口」的三个管理接口与项目对象的新字段，以及 §7 的这几行：

- **集成状态**：成功（项目对象、版本 +1、审计与事件各一条）、目标与当前相同 200 不写、
  「CLOSED 优先」（目标相同也 409）、404 越权（A 的客户 id 配 B 的项目 id）、422 校验、
  判定顺序 422 → 404 → 409；
- **地址 PUT**：规范化后保存、响应形状、「同一地址」与「清除从未设过的地址」200 不写、
  设为地址而 `CLOSED` 时 409 而清除不受限、404 越权、422 校验与判定顺序；
- **地址读取**（GET）：返回规范化地址与 `status_webhook_configured`，不写审计，
  非 ADMIN 403，越权 404；
- **项目对象**：列表与创建响应都有 `integration_status` 与 `status_webhook_configured`，
  不泄露地址；
- **非 ADMIN**：三个接口匿名 401、CUSTOMER 403，且不写库
  （全路由枚举另见 test_admin_customers_api.py）。

规则本身（URL 规范化的每一类、加锁顺序、回滚）由 test_project_integration_service.py 覆盖；
这里只验接口层把它们原样转出来。
"""

from __future__ import annotations

import ast
import inspect
import json
import textwrap
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select, update
from sqlalchemy.pool import StaticPool

from app.api import admin_customers
from app.core.config import Settings
from app.core.database import create_session_factory
from app.core.tokens import issue_access_token
from app.main import create_app
from app.models.auth import AuditAction, AuditLog, DomainOutbox, User, UserRole, UserStatus
from app.models.base import Base
from app.models.integration import ProjectStatusDelivery
from app.models.tenancy import AccountStatus, Project, Tenant
from app.services.auth import utc_now

CUSTOMERS = "/api/v1/admin/customers"
ENVELOPE_FIELDS = {"success", "data", "error", "request_id"}
PROJECT_FIELDS = {
    "id",
    "name",
    "description",
    "integration_status",
    "status_webhook_configured",
    "created_at",
    "updated_at",
}
WEBHOOK_FIELDS = {"status_webhook_url", "status_webhook_configured"}

# 路径里带一段像令牌的串：它只许出现在 status-webhook 的 PUT 与 GET 响应里（设计 §2、§6）。
# 全零占位值，地址是虚构的。
PATH_TOKEN = "tok_" + "0" * 32
HOOK = f"https://hooks.example.com/acuven/{PATH_TOKEN}?v=1"
HOOK_HOST = "hooks.example.com"
# 同一个地址的另一种写法：scheme 与主机名大写、主机名末尾一个点、显式 443。
HOOK_SPELLED_DIFFERENTLY = f"HTTPS://Hooks.Example.COM.:443/acuven/{PATH_TOKEN}?v=1"
OTHER_HOOK = "https://status.example.net/receive"
REASON = "Integration paused for maintenance"


# --- 夹具与帮手 -----------------------------------------------------------------


@pytest.fixture
def app(tmp_path):
    key = tmp_path / "jwt.key"
    key.write_text("a-test-signing-key-that-is-long-enough", encoding="utf-8")
    application = create_app(Settings(jwt_secret_file=str(key), database_url=""))
    # StaticPool：TestClient 在线程池里跑同步端点，内存库必须所有连接共用一个。
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


def make_user(application: FastAPI, role: UserRole) -> int:
    now = utc_now()
    with application.state.session_factory() as session:
        user = User(
            email=f"{uuid.uuid4().hex}@example.com",
            password_hash="not-a-real-hash",
            role=role,
            status=UserStatus.ACTIVE,
            created_at=now,
            updated_at=now,
        )
        session.add(user)
        session.commit()
        return int(user.id)


def token_for(application: FastAPI, role: UserRole) -> dict[str, str]:
    token = issue_access_token(
        application.state.settings,
        user_id=make_user(application, role),
        role=role.value,
        session_id=uuid.uuid4().hex,
        now=utc_now(),
    )
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def admin(app) -> dict[str, str]:
    return token_for(app, UserRole.ADMIN)


def new_customer(client: TestClient, headers: dict[str, str], name: str = "Acme Sdn Bhd") -> str:
    body = {"company_name": name, "email": "ops@example.com"}
    response = client.post(CUSTOMERS, json=body, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()["data"]["id"]


def new_project(client: TestClient, headers: dict[str, str], customer_id: str) -> dict:
    response = client.post(
        f"{CUSTOMERS}/{customer_id}/projects", json={"name": "Chatbot"}, headers=headers
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]


@pytest.fixture
def owned(client, admin) -> tuple[str, str]:
    """(customer_id, project_id) of a fresh customer and its one project."""
    customer_id = new_customer(client, admin)
    return customer_id, new_project(client, admin, customer_id)["id"]


def status_url(customer_id: str, project_id: str) -> str:
    return f"{CUSTOMERS}/{customer_id}/projects/{project_id}/integration-status"


def webhook_url(customer_id: str, project_id: str) -> str:
    return f"{CUSTOMERS}/{customer_id}/projects/{project_id}/status-webhook"


def error_code(response) -> str:
    body = response.json()
    assert set(body) == ENVELOPE_FIELDS
    assert body["success"] is False
    assert body["data"] is None
    return body["error"]["code"]


def audits(application: FastAPI, action: AuditAction) -> list[AuditLog]:
    statement = select(AuditLog).where(AuditLog.action == action).order_by(AuditLog.id)
    with application.state.session_factory() as session:
        return list(session.execute(statement).scalars())


def snapshot(application: FastAPI) -> dict[str, object]:
    """Everything the three endpoints may write: rows they add and columns they change."""
    tenants = select(Tenant.public_id, Tenant.status_version, Tenant.status_changed_at)
    projects = select(
        Project.public_id,
        Project.integration_status,
        Project.status_webhook_url,
        Project.updated_at,
    )
    deliveries = select(
        ProjectStatusDelivery.project_id,
        ProjectStatusDelivery.delivered_version,
        ProjectStatusDelivery.attempt_count,
        ProjectStatusDelivery.next_attempt_at,
    )
    stored: dict[str, object] = {}
    with application.state.session_factory() as session:
        for name, statement in (
            ("tenants", tenants.order_by(Tenant.id)),
            ("projects", projects.order_by(Project.id)),
            ("deliveries", deliveries.order_by(ProjectStatusDelivery.project_id)),
        ):
            stored[name] = [tuple(row) for row in session.execute(statement)]
        for model in (AuditLog, DomainOutbox):
            count = select(func.count()).select_from(model)
            stored[model.__tablename__] = session.execute(count).scalar_one()
    return stored


def close(application: FastAPI, customer_id: str) -> None:
    """`CLOSED` 还没有产生路径：直接改库造数据（设计 §7「CLOSED 优先」）。"""
    with application.state.session_factory() as session:
        session.execute(
            update(Tenant)
            .where(Tenant.public_id == customer_id)
            .values(account_status=AccountStatus.CLOSED)
        )
        session.commit()


def customer_version(client: TestClient, headers: dict[str, str], customer_id: str) -> int:
    response = client.get(f"{CUSTOMERS}/{customer_id}", headers=headers)
    return response.json()["data"]["status_version"]


# --- POST …/integration-status ---------------------------------------------------


def test_disabling_a_project_returns_it_and_bumps_the_version_once(client, app, admin, owned):
    customer_id, project_id = owned
    before = snapshot(app)

    response = client.post(
        status_url(customer_id, project_id),
        json={"integration_status": "DISABLED", "reason": f"  {REASON}  "},
        headers=admin,
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == ENVELOPE_FIELDS
    project = body["data"]
    assert set(project) == PROJECT_FIELDS
    assert (project["id"], project["integration_status"]) == (project_id, "DISABLED")
    assert project["status_webhook_configured"] is False
    assert customer_version(client, admin, customer_id) == 1
    after = snapshot(app)
    assert after["audit_logs"] == before["audit_logs"] + 1
    assert after["domain_outbox"] == before["domain_outbox"] + 1
    [audit] = audits(app, AuditAction.PROJECT_INTEGRATION_STATUS_CHANGED)
    assert (audit.entity_type, audit.entity_id, audit.reason) == ("project", project_id, REASON)
    assert json.loads(audit.before_state or "{}") == {
        "integration_status": "ENABLED",
        "status_version": 0,
    }
    assert json.loads(audit.after_state or "{}") == {
        "integration_status": "DISABLED",
        "status_version": 1,
    }

    # 再启用：又一次真实跃迁。
    enabled = client.post(
        status_url(customer_id, project_id),
        json={"integration_status": "ENABLED", "reason": REASON},
        headers=admin,
    )
    assert enabled.status_code == 200
    assert enabled.json()["data"]["integration_status"] == "ENABLED"
    assert customer_version(client, admin, customer_id) == 2


def test_the_same_integration_status_is_200_and_writes_nothing(client, app, admin, owned):
    customer_id, project_id = owned
    before = snapshot(app)

    response = client.post(
        status_url(customer_id, project_id),
        json={"integration_status": "ENABLED", "reason": REASON},
        headers=admin,
    )

    assert response.status_code == 200
    assert response.json()["data"]["integration_status"] == "ENABLED"
    assert snapshot(app) == before


@pytest.mark.parametrize("target", ["ENABLED", "DISABLED"])
def test_a_closed_customer_is_409_even_for_the_same_target(client, app, admin, owned, target):
    customer_id, project_id = owned
    close(app, customer_id)
    before = snapshot(app)

    response = client.post(
        status_url(customer_id, project_id),
        json={"integration_status": target, "reason": REASON},
        headers=admin,
    )

    assert (response.status_code, error_code(response)) == (409, "ACCOUNT_CLOSED")
    assert snapshot(app) == before


def test_another_customers_project_is_404_for_integration_status(client, app, admin) -> None:
    """A 的客户 id 配 B 的项目 id：与不存在一样 404，什么都不写（INV-8）。"""
    first = new_customer(client, admin, "First Sdn Bhd")
    new_project(client, admin, first)
    second = new_customer(client, admin, "Second Sdn Bhd")
    theirs = new_project(client, admin, second)["id"]
    before = snapshot(app)

    responses = [
        client.post(
            status_url(customer_id, project_id),
            json={"integration_status": "DISABLED", "reason": REASON},
            headers=admin,
        )
        for customer_id, project_id in ((first, theirs), (first, str(uuid.uuid4())))
    ]

    assert {(r.status_code, error_code(r)) for r in responses} == {(404, "PROJECT_NOT_FOUND")}
    assert len({json.dumps(r.json()["error"]) for r in responses}) == 1
    assert snapshot(app) == before


def test_an_unknown_customer_is_404_for_integration_status(client, app, admin, owned) -> None:
    _customer_id, project_id = owned
    before = snapshot(app)

    response = client.post(
        status_url(str(uuid.uuid4()), project_id),
        json={"integration_status": "DISABLED", "reason": REASON},
        headers=admin,
    )

    assert (response.status_code, error_code(response)) == (404, "CUSTOMER_NOT_FOUND")
    assert snapshot(app) == before


@pytest.mark.parametrize(
    "body",
    [
        {"integration_status": "PAUSED", "reason": REASON},
        {"integration_status": "disabled", "reason": REASON},
        {"integration_status": None, "reason": REASON},
        {"reason": REASON},
        {"integration_status": "DISABLED"},
        {"integration_status": "DISABLED", "reason": "   "},
        {"integration_status": "DISABLED", "reason": "x" * 256},
        {"integration_status": "DISABLED", "reason": REASON, "status_version": 9},
        {"integration_status": "DISABLED", "reason": REASON, "project_id": "x"},
    ],
)
def test_invalid_integration_status_bodies_are_422(client, app, admin, owned, body) -> None:
    customer_id, project_id = owned
    before = snapshot(app)

    response = client.post(status_url(customer_id, project_id), json=body, headers=admin)

    assert (response.status_code, error_code(response)) == (422, "VALIDATION_ERROR")
    assert snapshot(app) == before


def test_a_reason_of_exactly_255_characters_is_accepted(client, admin, owned) -> None:
    customer_id, project_id = owned

    response = client.post(
        status_url(customer_id, project_id),
        json={"integration_status": "DISABLED", "reason": "x" * 255},
        headers=admin,
    )

    assert response.status_code == 200


def test_integration_status_checks_422_then_404_then_409(client, app, admin, owned) -> None:
    customer_id, project_id = owned
    close(app, customer_id)
    bad = {"integration_status": "DISABLED", "reason": ""}
    good = {"integration_status": "DISABLED", "reason": REASON}

    invalid_and_unknown = client.post(
        status_url(str(uuid.uuid4()), project_id), json=bad, headers=admin
    )
    unknown_and_closed = client.post(
        status_url(customer_id, str(uuid.uuid4())), json=good, headers=admin
    )

    assert (invalid_and_unknown.status_code, error_code(invalid_and_unknown)) == (
        422,
        "VALIDATION_ERROR",
    )
    assert (unknown_and_closed.status_code, error_code(unknown_and_closed)) == (
        404,
        "PROJECT_NOT_FOUND",
    )


# --- PUT …/status-webhook --------------------------------------------------------


def test_setting_an_address_stores_its_normalized_form(client, app, admin, owned) -> None:
    customer_id, project_id = owned

    response = client.put(
        webhook_url(customer_id, project_id), json={"url": HOOK_SPELLED_DIFFERENTLY}, headers=admin
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == ENVELOPE_FIELDS
    assert body["data"] == {"status_webhook_url": HOOK, "status_webhook_configured": True}
    [audit] = audits(app, AuditAction.PROJECT_STATUS_WEBHOOK_SET)
    assert (audit.entity_type, audit.entity_id) == ("project", project_id)
    assert json.loads(audit.before_state or "{}") == {"status_webhook_host": None}
    assert json.loads(audit.after_state or "{}") == {"status_webhook_host": HOOK_HOST}
    # 审计只记主机名：路径里的令牌不进审计（设计 §6）。
    assert PATH_TOKEN not in (audit.before_state or "") + (audit.after_state or "")


def test_the_same_normalized_address_is_200_and_writes_nothing(client, app, admin, owned):
    customer_id, project_id = owned
    url = webhook_url(customer_id, project_id)
    assert client.put(url, json={"url": HOOK}, headers=admin).status_code == 200
    before = snapshot(app)

    response = client.put(url, json={"url": HOOK_SPELLED_DIFFERENTLY}, headers=admin)

    assert response.status_code == 200
    assert response.json()["data"]["status_webhook_url"] == HOOK
    assert snapshot(app) == before


def test_clearing_an_address_never_set_is_200_and_writes_nothing(client, app, admin, owned):
    customer_id, project_id = owned
    before = snapshot(app)

    response = client.put(webhook_url(customer_id, project_id), json={"url": None}, headers=admin)

    assert response.status_code == 200
    assert response.json()["data"] == {
        "status_webhook_url": None,
        "status_webhook_configured": False,
    }
    assert snapshot(app) == before


def test_clearing_twice_writes_once(client, app, admin, owned) -> None:
    customer_id, project_id = owned
    url = webhook_url(customer_id, project_id)
    client.put(url, json={"url": HOOK}, headers=admin)

    first = client.put(url, json={"url": None}, headers=admin)
    before = snapshot(app)
    second = client.put(url, json={"url": None}, headers=admin)

    assert first.status_code == second.status_code == 200
    assert second.json()["data"] == {"status_webhook_url": None, "status_webhook_configured": False}
    assert snapshot(app) == before
    assert len(audits(app, AuditAction.PROJECT_STATUS_WEBHOOK_SET)) == 2


def test_changing_the_address_resets_the_delivery_cursor(client, app, admin, owned) -> None:
    customer_id, project_id = owned
    url = webhook_url(customer_id, project_id)
    client.put(url, json={"url": HOOK}, headers=admin)
    with app.state.session_factory() as session:
        session.execute(update(ProjectStatusDelivery).values(delivered_version=0, attempt_count=3))
        session.commit()

    response = client.put(url, json={"url": OTHER_HOOK}, headers=admin)

    assert response.json()["data"]["status_webhook_url"] == OTHER_HOOK
    with app.state.session_factory() as session:
        row = session.execute(select(ProjectStatusDelivery)).scalar_one()
        assert (row.delivered_version, row.attempt_count, row.lease_token) == (None, 0, None)


def test_a_closed_customer_cannot_set_an_address_but_can_clear_it(client, app, admin, owned):
    customer_id, project_id = owned
    url = webhook_url(customer_id, project_id)
    client.put(url, json={"url": HOOK}, headers=admin)
    close(app, customer_id)
    before = snapshot(app)

    refused = client.put(url, json={"url": OTHER_HOOK}, headers=admin)
    assert (refused.status_code, error_code(refused)) == (409, "ACCOUNT_CLOSED")
    assert snapshot(app) == before

    cleared = client.put(url, json={"url": None}, headers=admin)
    assert cleared.status_code == 200
    assert cleared.json()["data"] == {
        "status_webhook_url": None,
        "status_webhook_configured": False,
    }


def test_another_customers_project_is_404_for_the_address(client, app, admin) -> None:
    first = new_customer(client, admin, "First Sdn Bhd")
    new_project(client, admin, first)
    second = new_customer(client, admin, "Second Sdn Bhd")
    theirs = new_project(client, admin, second)["id"]
    before = snapshot(app)

    put = client.put(webhook_url(first, theirs), json={"url": HOOK}, headers=admin)
    get = client.get(webhook_url(first, theirs), headers=admin)
    unknown = client.put(webhook_url(str(uuid.uuid4()), theirs), json={"url": HOOK}, headers=admin)

    assert (put.status_code, error_code(put)) == (404, "PROJECT_NOT_FOUND")
    assert (get.status_code, error_code(get)) == (404, "PROJECT_NOT_FOUND")
    assert (unknown.status_code, error_code(unknown)) == (404, "CUSTOMER_NOT_FOUND")
    assert snapshot(app) == before


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"url": HOOK, "status_webhook_host": HOOK_HOST},
        {"url": HOOK, "project_id": "x"},
        {"url": 443},
        {"url": "http://hooks.example.com/x"},
        {"url": "https://127.1/x"},
        {"url": "https://[::1]/x"},
        {"url": "https://user@hooks.example.com/x"},
        {"url": "https://hooks.example.com/x#frag"},
        {"url": "https://hooks.example.com:8443/x"},
        {"url": "https://hooks.example.com/" + "a" * 2048},
        {"url": "https://hooks.example.com/a b"},
        {"url": ""},
    ],
)
def test_invalid_address_bodies_are_422(client, app, admin, owned, body) -> None:
    customer_id, project_id = owned
    before = snapshot(app)

    response = client.put(webhook_url(customer_id, project_id), json=body, headers=admin)

    assert (response.status_code, error_code(response)) == (422, "VALIDATION_ERROR")
    assert snapshot(app) == before


def test_an_invalid_address_never_appears_in_the_error(client, admin, owned) -> None:
    customer_id, project_id = owned
    bad = f"http://hooks.example.com/{PATH_TOKEN}"

    response = client.put(webhook_url(customer_id, project_id), json={"url": bad}, headers=admin)

    assert response.status_code == 422
    assert PATH_TOKEN not in response.text
    assert "hooks.example.com" not in response.text


def test_the_address_checks_422_then_404_then_409(client, app, admin, owned) -> None:
    customer_id, project_id = owned
    close(app, customer_id)

    invalid_and_unknown = client.put(
        webhook_url(str(uuid.uuid4()), project_id), json={"url": "http://x.example"}, headers=admin
    )
    unknown_and_closed = client.put(
        webhook_url(customer_id, str(uuid.uuid4())), json={"url": HOOK}, headers=admin
    )

    assert (invalid_and_unknown.status_code, error_code(invalid_and_unknown)) == (
        422,
        "VALIDATION_ERROR",
    )
    assert (unknown_and_closed.status_code, error_code(unknown_and_closed)) == (
        404,
        "PROJECT_NOT_FOUND",
    )


# --- GET …/status-webhook（设计 §7「地址读取」） -------------------------------------


def test_reading_the_address_returns_it_and_writes_nothing(client, app, admin, owned) -> None:
    customer_id, project_id = owned
    url = webhook_url(customer_id, project_id)

    unset = client.get(url, headers=admin)
    client.put(url, json={"url": HOOK_SPELLED_DIFFERENTLY}, headers=admin)
    before = snapshot(app)
    stored = client.get(url, headers=admin)

    assert unset.status_code == stored.status_code == 200
    assert unset.json()["data"] == {"status_webhook_url": None, "status_webhook_configured": False}
    data = stored.json()["data"]
    assert set(data) == WEBHOOK_FIELDS
    assert data == {"status_webhook_url": HOOK, "status_webhook_configured": True}
    assert snapshot(app) == before


# --- 非 ADMIN ----------------------------------------------------------------------


@pytest.mark.parametrize("caller", ["anonymous", "customer"])
@pytest.mark.parametrize(
    ("method", "suffix", "body"),
    [
        ("POST", "integration-status", {"integration_status": "DISABLED", "reason": REASON}),
        ("PUT", "status-webhook", {"url": HOOK}),
        ("GET", "status-webhook", None),
    ],
)
def test_non_admins_are_refused_and_nothing_is_written(
    client, app, admin, owned, caller, method, suffix, body
) -> None:
    customer_id, project_id = owned
    # GET 也要看出漏鉴权：先设上地址，漏了鉴权的读取会把它返回出去。
    client.put(webhook_url(customer_id, project_id), json={"url": HOOK}, headers=admin)
    headers = {} if caller == "anonymous" else token_for(app, UserRole.CUSTOMER)
    before = snapshot(app)

    response = client.request(
        method,
        f"{CUSTOMERS}/{customer_id}/projects/{project_id}/{suffix}",
        json=body,
        headers=headers,
    )

    expected = (401, "TOKEN_INVALID") if caller == "anonymous" else (403, "ADMIN_REQUIRED")
    assert (response.status_code, error_code(response)) == expected
    assert PATH_TOKEN not in response.text
    assert snapshot(app) == before


@pytest.mark.parametrize(
    "handler",
    [
        admin_customers.change_integration_status,
        admin_customers.set_status_webhook,
        admin_customers.get_status_webhook,
    ],
)
def test_each_handler_calls_require_admin_first(handler) -> None:
    """函数体第一条语句（跳过 docstring）是对 `require_admin` 的调用。"""
    source = textwrap.dedent(inspect.getsource(handler))
    function = ast.parse(source).body[0]
    assert isinstance(function, ast.FunctionDef)
    body = function.body
    if ast.get_docstring(function) is not None:
        body = body[1:]
    first = body[0]
    call = first.value if isinstance(first, ast.Assign | ast.Expr) else None
    assert isinstance(call, ast.Call)
    assert isinstance(call.func, ast.Name) and call.func.id == "require_admin"


# --- 项目对象 ----------------------------------------------------------------------


def test_the_project_object_carries_the_new_fields_but_never_the_address(
    client, admin, owned
) -> None:
    customer_id, project_id = owned
    client.put(webhook_url(customer_id, project_id), json={"url": HOOK}, headers=admin)
    client.post(
        status_url(customer_id, project_id),
        json={"integration_status": "DISABLED", "reason": REASON},
        headers=admin,
    )
    other = new_project(client, admin, customer_id)

    listed = client.get(f"{CUSTOMERS}/{customer_id}/projects", headers=admin)

    assert listed.status_code == 200
    items = {item["id"]: item for item in listed.json()["data"]["items"]}
    assert all(set(item) == PROJECT_FIELDS for item in items.values())
    mine = items[project_id]
    assert (mine["integration_status"], mine["status_webhook_configured"]) == ("DISABLED", True)
    fresh = items[other["id"]]
    assert (fresh["integration_status"], fresh["status_webhook_configured"]) == ("ENABLED", False)
    # 地址只出现在 status-webhook 的 PUT 与 GET 里：列表里连主机名都没有（设计 §2）。
    assert PATH_TOKEN not in listed.text
    assert HOOK_HOST not in listed.text

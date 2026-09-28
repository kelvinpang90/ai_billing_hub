"""Tenant account status endpoint on SQLite (design gate #136 v2 §2, §6, §7).

这里测接口契约：正常路径 `ENABLED → DISABLED → ENABLED`、审计与 outbox 的字段、校验边界、
多余字段、幂等、404、从 `CLOSED` 出发的 409、客户对象的新字段、与计费状态互相独立、
日志里没有原因，以及处理函数第一条语句是 `require_admin`。匿名 401 / CUSTOMER 403 在
这里显式测一次，全部管理端路由的枚举用例在 test_admin_customers_api.py。

跃迁表、只锁租户行、事务中途失败的回滚（SQLite 与 MySQL 各一次）与并发版本（真 MySQL）
在 test_account_status_service.py；迁移在 test_migrations.py。

⚠️ 文件里的 uuid 字面量一律是全零占位值（secret-scan）。
"""

from __future__ import annotations

import ast
import inspect
import io
import json
import logging
import textwrap
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select, update
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.api import admin_customers
from app.core.config import Settings
from app.core.database import create_session_factory
from app.core.logging import JsonFormatter
from app.core.tokens import issue_access_token
from app.main import create_app
from app.models.auth import (
    AuditAction,
    AuditLog,
    DomainOutbox,
    OutboxStatus,
    User,
    UserRole,
    UserStatus,
)
from app.models.base import Base
from app.models.tenancy import AccountStatus, Tenant
from app.models.wallet import Wallet, WalletTransaction
from app.services import account_status
from app.services.auth import utc_now

CUSTOMERS = "/api/v1/admin/customers"

# 客户对象的字段白名单（设计 §2「客户对象」、§6）。断言「恰好等于」：内部 id、低余额阈值、
# 成本或毛利谁多出来都红。
CUSTOMER_FIELDS = {
    "id",
    "company_name",
    "contact_name",
    "email",
    "phone",
    "billing_status",
    "account_status",
    "status_version",
    "created_at",
    "updated_at",
}
DETAIL_FIELDS = CUSTOMER_FIELDS | {"wallet"}
WALLET_FIELDS = {"currency", "balance", "version"}
ENVELOPE_FIELDS = {"success", "data", "error", "request_id"}

# outbox payload 的字段（设计 §2「外部系统与异步边界」）：一个不多、一个不少，没有原因。
PAYLOAD_FIELDS = {
    "account_status",
    "previous_account_status",
    "billing_status",
    "status_version",
    "changed_at",
}

EVENT = "tenant.account_status_changed"
REASON = "Contract terminated pending settlement"
# 全零占位值（secret-scan）：只用来探「请求体不许带 id」。
ZERO_UUID = "00000000-0000-4000-8000-000000000000"


# --- 夹具与帮手 -----------------------------------------------------------------


def settings_for(tmp_path, database_url: str = "") -> Settings:
    key = tmp_path / "jwt.key"
    key.write_text("a-test-signing-key-that-is-long-enough", encoding="utf-8")
    return Settings(jwt_secret_file=str(key), database_url=database_url)


@pytest.fixture
def app(tmp_path):
    application = create_app(settings_for(tmp_path))
    # StaticPool：理由见 test_admin_customers_api.py 的同名夹具。
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


def make_user(application: FastAPI, role: UserRole = UserRole.ADMIN) -> int:
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


def token_for(application: FastAPI, user_id: int, role: str = "ADMIN") -> dict[str, str]:
    token = issue_access_token(
        application.state.settings,
        user_id=user_id,
        role=role,
        session_id=uuid.uuid4().hex,
        now=utc_now(),
    )
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def admin_id(app) -> int:
    return make_user(app)


@pytest.fixture
def admin(app, admin_id) -> dict[str, str]:
    return token_for(app, admin_id)


def error_code(response) -> str:
    body = response.json()
    assert set(body) == ENVELOPE_FIELDS
    assert body["success"] is False
    assert body["data"] is None
    return body["error"]["code"]


def new_customer(client: TestClient, headers: dict[str, str]) -> dict:
    body = {"company_name": "Acme Sdn Bhd", "email": "ops@example.com"}
    response = client.post(CUSTOMERS, json=body, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()["data"]


def change(client: TestClient, headers: dict[str, str], customer_id: str, body: object):
    return client.post(f"{CUSTOMERS}/{customer_id}/account-status", json=body, headers=headers)


def to(target: str, reason: str = REASON) -> dict[str, str]:
    return {"account_status": target, "reason": reason}


def count_rows(session: Session, model) -> int:
    return session.execute(select(func.count()).select_from(model)).scalar_one()


def row_counts(application: FastAPI) -> dict[str, int]:
    models = (Tenant, Wallet, WalletTransaction, AuditLog, DomainOutbox)
    with application.state.session_factory() as session:
        return {model.__tablename__: count_rows(session, model) for model in models}


def stored_tenant(application: FastAPI, customer_id: str) -> tuple:
    """Every column this endpoint may touch, as committed."""
    with application.state.session_factory() as session:
        tenant = session.execute(select(Tenant).where(Tenant.public_id == customer_id)).scalar_one()
        return (
            tenant.account_status,
            tenant.billing_status,
            tenant.status_version,
            tenant.updated_at,
        )


def audits(
    application: FastAPI, action: AuditAction = AuditAction.TENANT_ACCOUNT_STATUS_CHANGED
) -> list[AuditLog]:
    statement = select(AuditLog).where(AuditLog.action == action).order_by(AuditLog.id)
    with application.state.session_factory() as session:
        return list(session.execute(statement).scalars())


def events(application: FastAPI) -> list[DomainOutbox]:
    statement = (
        select(DomainOutbox).where(DomainOutbox.event_type == EVENT).order_by(DomainOutbox.id)
    )
    with application.state.session_factory() as session:
        return list(session.execute(statement).scalars())


def force_status(application: FastAPI, customer_id: str, value: AccountStatus) -> None:
    """造数据：直接改库（本任务不产生 PENDING_ACTIVATION 与 CLOSED，设计 §7「跃迁表」）。"""
    with application.state.session_factory() as session:
        session.execute(
            update(Tenant).where(Tenant.public_id == customer_id).values(account_status=value)
        )
        session.commit()


# --- 正常路径 -------------------------------------------------------------------


def test_enabled_disabled_enabled(client, app, admin, admin_id) -> None:
    """设计 §7「正常路径」：每步 200；版本各 +1；每步一条审计与一条 PENDING outbox。"""
    created = new_customer(client, admin)
    assert (created["account_status"], created["status_version"]) == ("ENABLED", 0)
    headers = {**admin, "User-Agent": "account-status-api-test"}

    steps = [("DISABLED", "ENABLED"), ("ENABLED", "DISABLED")]
    for number, (target, previous) in enumerate(steps, start=1):
        before = row_counts(app)
        response = change(client, headers, created["id"], to(target, f"  {REASON} {number}  "))

        assert response.status_code == 200, response.text
        body = response.json()
        assert set(body) == ENVELOPE_FIELDS and body["success"] is True
        data = body["data"]
        assert set(data) == DETAIL_FIELDS
        assert set(data["wallet"]) == WALLET_FIELDS
        assert (data["account_status"], data["status_version"]) == (target, number)
        # 计费状态与钱包不受影响（设计 §4、INV-4）。
        assert data["billing_status"] == "SUSPENDED"
        assert data["wallet"] == created["wallet"]
        # 详情就是之后 GET 读回的那一份。
        assert client.get(f"{CUSTOMERS}/{created['id']}", headers=admin).json()["data"] == data

        # 恰好多一条审计、一条 outbox，别的表不动。
        assert row_counts(app) == {
            **before,
            "audit_logs": before["audit_logs"] + 1,
            "domain_outbox": before["domain_outbox"] + 1,
        }
        audit = audits(app)[-1]
        assert (audit.actor_user_id, audit.actor_role) == (admin_id, "ADMIN")
        assert (audit.entity_type, audit.entity_id) == ("tenant", created["id"])
        assert audit.user_agent == "account-status-api-test"
        assert json.loads(audit.before_state or "{}") == {
            "account_status": previous,
            "status_version": number - 1,
        }
        assert json.loads(audit.after_state or "{}") == {
            "account_status": target,
            "status_version": number,
        }
        # 原因去了首尾空白，只进审计的 reason。
        assert audit.reason == f"{REASON} {number}"

        event = events(app)[-1]
        assert (event.aggregate_type, event.aggregate_id) == ("tenant", created["id"])
        assert (event.status, event.attempt_count) == (OutboxStatus.PENDING, 0)
        payload = json.loads(event.payload_json or "{}")
        assert set(payload) == PAYLOAD_FIELDS
        assert payload == {
            "account_status": target,
            "previous_account_status": previous,
            "billing_status": "SUSPENDED",
            "status_version": number,
            "changed_at": data["updated_at"],
        }
        assert REASON not in (event.payload_json or "")

    assert len(audits(app)) == 2
    assert len(events(app)) == 2


def test_the_audit_and_the_event_carry_no_personal_data(client, app, admin) -> None:
    """设计 §6：审计前后状态只有两项；payload 没有联系人、邮箱、电话，也没有原因。"""
    body = {
        "company_name": "Acme Sdn Bhd",
        "email": "pii-probe-4c1e@example.com",
        "contact_name": "Zebulon Quixote Harrington",
        "phone": "+60 3-7788 9911",
    }
    created = client.post(CUSTOMERS, json=body, headers=admin).json()["data"]

    assert change(client, admin, created["id"], to("DISABLED")).status_code == 200

    [audit] = audits(app)
    [event] = events(app)
    for text in (audit.before_state or "", audit.after_state or "", event.payload_json or ""):
        for personal in (body["email"], body["contact_name"], body["phone"]):
            assert personal not in text
    assert REASON not in (event.payload_json or "")


# --- 校验边界 -------------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        to("PENDING_ACTIVATION"),
        to("CLOSED"),
        to("disabled"),
        to("Enabled"),
        to(""),
        {"account_status": None, "reason": REASON},
        {"reason": REASON},
        to("DISABLED", ""),
        to("DISABLED", "   "),
        to("DISABLED", "x" * 256),
        {"account_status": "DISABLED"},
        {"account_status": "DISABLED", "reason": None},
        {},
        ["DISABLED"],
    ],
    ids=[
        "pending-activation",
        "closed",
        "lowercase",
        "mixed-case",
        "empty-status",
        "null-status",
        "missing-status",
        "empty-reason",
        "blank-reason",
        "reason-256",
        "missing-reason",
        "null-reason",
        "empty-body",
        "not-an-object",
    ],
)
def test_invalid_bodies_are_422_and_write_nothing(client, app, admin, body: object) -> None:
    """设计 §7「边界值」：`PENDING_ACTIVATION` / `CLOSED` 不能作为目标；原因去空白后 1–255。"""
    created = new_customer(client, admin)
    before, stored = row_counts(app), stored_tenant(app, created["id"])

    response = change(client, admin, created["id"], body)

    assert (response.status_code, error_code(response)) == (422, "VALIDATION_ERROR")
    assert row_counts(app) == before
    assert stored_tenant(app, created["id"]) == stored


@pytest.mark.parametrize(
    "extra",
    [
        {"status_version": 5},
        {"tenant_id": 1},
        {"customer_id": ZERO_UUID},
        {"id": ZERO_UUID},
        {"public_id": ZERO_UUID},
        {"billing_status": "ACTIVE"},
        {"balance": "100.00000000"},
    ],
    ids=[
        "status_version",
        "tenant_id",
        "customer_id",
        "id",
        "public_id",
        "billing_status",
        "balance",
    ],
)
def test_extra_fields_are_422_and_write_nothing(client, app, admin, extra: dict) -> None:
    """INV-8：客户只来自路径；版本、计费状态、余额、任何 id 都不能经请求体带进来。"""
    created = new_customer(client, admin)
    before, stored = row_counts(app), stored_tenant(app, created["id"])

    response = change(client, admin, created["id"], {**to("DISABLED"), **extra})

    assert (response.status_code, error_code(response)) == (422, "VALIDATION_ERROR")
    assert row_counts(app) == before
    assert stored_tenant(app, created["id"]) == stored


def test_a_reason_of_255_characters_after_stripping_is_accepted(client, app, admin) -> None:
    created = new_customer(client, admin)

    response = change(client, admin, created["id"], to("DISABLED", "  " + "x" * 255 + "  "))

    assert response.status_code == 200, response.text
    [audit] = audits(app)
    assert audit.reason == "x" * 255


# --- 幂等、404、409 ---------------------------------------------------------------


def test_the_same_target_twice_writes_once(client, app, admin) -> None:
    """设计 §7「幂等」：目标等于当前 → 200，不写审计、不加版本、不写 outbox、不动 updated_at。

    原因不同的重发（目标相同）按幂等处理，保留第一次的审计（设计 §4）。
    """
    created = new_customer(client, admin)
    first = change(client, admin, created["id"], to("DISABLED")).json()["data"]
    before, stored = row_counts(app), stored_tenant(app, created["id"])

    again = change(client, admin, created["id"], to("DISABLED", "A different reason"))

    assert again.status_code == 200
    assert again.json()["data"] == first
    assert row_counts(app) == before
    assert stored_tenant(app, created["id"]) == stored
    [audit] = audits(app)
    assert audit.reason == REASON


def test_the_current_status_on_a_new_customer_writes_nothing(client, app, admin) -> None:
    created = new_customer(client, admin)
    before, stored = row_counts(app), stored_tenant(app, created["id"])

    response = change(client, admin, created["id"], to("ENABLED"))

    assert response.status_code == 200
    assert response.json()["data"] == created
    assert row_counts(app) == before
    assert stored_tenant(app, created["id"]) == stored
    assert (audits(app), events(app)) == ([], [])


@pytest.mark.parametrize("unknown", ["random", "internal-id", "zero"])
def test_an_unknown_customer_is_404_and_writes_nothing(client, app, admin, unknown: str) -> None:
    created = new_customer(client, admin)
    before, stored = row_counts(app), stored_tenant(app, created["id"])
    customer_id = {"random": str(uuid.uuid4()), "internal-id": "1", "zero": ZERO_UUID}[unknown]

    response = change(client, admin, customer_id, to("DISABLED"))

    assert (response.status_code, error_code(response)) == (404, "CUSTOMER_NOT_FOUND")
    assert row_counts(app) == before
    assert stored_tenant(app, created["id"]) == stored


@pytest.mark.parametrize("target", ["ENABLED", "DISABLED"])
def test_closed_is_terminal(client, app, admin, target: str) -> None:
    """设计 §4：从 `CLOSED` 出去一格都没有 → 409，不写库。"""
    created = new_customer(client, admin)
    force_status(app, created["id"], AccountStatus.CLOSED)
    before, stored = row_counts(app), stored_tenant(app, created["id"])

    response = change(client, admin, created["id"], to(target))

    assert (response.status_code, error_code(response)) == (
        409,
        "ACCOUNT_STATUS_TRANSITION_INVALID",
    )
    assert row_counts(app) == before
    assert stored_tenant(app, created["id"]) == stored
    # 详情照常能读，显示的就是 CLOSED。
    fetched = client.get(f"{CUSTOMERS}/{created['id']}", headers=admin).json()["data"]
    assert fetched["account_status"] == "CLOSED"


@pytest.mark.parametrize("target", ["ENABLED", "DISABLED"])
def test_pending_activation_can_go_either_way(client, app, admin, target: str) -> None:
    """设计 §4：`PENDING_ACTIVATION` 出发的两格都合法。"""
    created = new_customer(client, admin)
    force_status(app, created["id"], AccountStatus.PENDING_ACTIVATION)

    response = change(client, admin, created["id"], to(target))

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert (data["account_status"], data["status_version"]) == (target, 1)
    [audit] = audits(app)
    assert json.loads(audit.before_state or "{}")["account_status"] == "PENDING_ACTIVATION"
    [event] = events(app)
    assert json.loads(event.payload_json or "{}")["previous_account_status"] == (
        "PENDING_ACTIVATION"
    )


# --- 鉴权 -----------------------------------------------------------------------


@pytest.mark.parametrize("caller", ["anonymous", "customer"])
def test_non_admins_are_refused_and_nothing_is_written(client, app, admin, caller: str) -> None:
    """设计 §7「鉴权与越权」：匿名 401、CUSTOMER 403，都不写库。"""
    created = new_customer(client, admin)
    before, stored = row_counts(app), stored_tenant(app, created["id"])
    headers = {}
    if caller == "customer":
        headers = token_for(app, make_user(app, UserRole.CUSTOMER), "CUSTOMER")

    response = change(client, headers, created["id"], to("DISABLED"))

    expected = (401, "TOKEN_INVALID") if caller == "anonymous" else (403, "ADMIN_REQUIRED")
    assert (response.status_code, error_code(response)) == expected
    assert row_counts(app) == before
    assert stored_tenant(app, created["id"]) == stored


def test_the_handler_calls_require_admin_first() -> None:
    """设计 §7「鉴权」：函数体第一条语句（跳过 docstring）是对 `require_admin` 的调用。"""
    handler = admin_customers.change_account_status
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


# --- 客户对象 -------------------------------------------------------------------


def test_detail_and_list_show_the_account_status(client, app, admin) -> None:
    """设计 §7「响应字段」：有 `account_status`，没有阈值、成本与内部 id。"""
    first = new_customer(client, admin)
    second = new_customer(client, admin)
    assert change(client, admin, second["id"], to("DISABLED")).status_code == 200

    detail = client.get(f"{CUSTOMERS}/{second['id']}", headers=admin).json()["data"]
    listing = client.get(CUSTOMERS, headers=admin).json()["data"]

    assert set(detail) == DETAIL_FIELDS
    assert detail["account_status"] == "DISABLED"
    assert all(set(item) == CUSTOMER_FIELDS for item in listing["items"])
    by_id = {item["id"]: item for item in listing["items"]}
    assert by_id[first["id"]]["account_status"] == "ENABLED"
    assert (by_id[second["id"]]["account_status"], by_id[second["id"]]["status_version"]) == (
        "DISABLED",
        1,
    )
    for forbidden in ("low_balance_threshold", "tenant_id", "cost", "margin"):
        assert forbidden not in json.dumps(detail)
        assert forbidden not in json.dumps(listing)


# --- 与计费状态互相独立 ------------------------------------------------------------


def test_billing_status_moves_independently_while_disabled(client, app, admin) -> None:
    """设计 §7「与计费状态独立」：DISABLED 下调账跨零，计费照常跃迁、账户状态不变，版本各 +1。

    钱包 `version` 在账户状态跃迁前后不变的断言在 test_account_status_service.py（真 MySQL 上
    触发器推进钱包，SQLite 上没有触发器）。
    """
    created = new_customer(client, admin)
    assert change(client, admin, created["id"], to("DISABLED")).status_code == 200

    adjusted = client.post(
        f"{CUSTOMERS}/{created['id']}/wallet/adjustments",
        json={
            "transaction_type": "ADJUSTMENT_CREDIT",
            "amount": "20",
            "reason": "Settlement credit",
            "idempotency_key": str(uuid.uuid4()),
        },
        headers=admin,
    )
    assert adjusted.status_code == 201, adjusted.text

    fetched = client.get(f"{CUSTOMERS}/{created['id']}", headers=admin).json()["data"]
    assert (fetched["account_status"], fetched["billing_status"], fetched["status_version"]) == (
        "DISABLED",
        "ACTIVE",
        2,
    )
    [billing] = audits(app, AuditAction.TENANT_BILLING_STATUS_CHANGED)
    assert json.loads(billing.after_state or "{}")["status_version"] == 2

    enabled = change(client, admin, created["id"], to("ENABLED")).json()["data"]
    assert (enabled["account_status"], enabled["billing_status"], enabled["status_version"]) == (
        "ENABLED",
        "ACTIVE",
        3,
    )
    [_, event] = events(app)
    assert json.loads(event.payload_json or "{}")["billing_status"] == "ACTIVE"


# --- 日志里没有原因 ---------------------------------------------------------------


def test_the_reason_never_reaches_the_logs(tmp_path, monkeypatch) -> None:
    """设计 §6：原因只进审计。成功一次、再让一次在写审计时失败：日志与响应里都没有原因。

    引擎由 `create_database_engine` 建（`create_app` 用的就是它），与生产同一个工厂。
    """
    database = tmp_path / "billing.db"
    url = f"sqlite+pysqlite:///{database.as_posix()}"
    application = create_app(settings_for(tmp_path, database_url=url))
    engine = application.state.engine
    assert engine is not None
    Base.metadata.create_all(engine)
    http = TestClient(application, raise_server_exceptions=False)
    probe = "Reason-probe-9d27 abuse report"

    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        headers = token_for(application, make_user(application))
        created = new_customer(http, headers)
        succeeded = change(http, headers, created["id"], to("DISABLED", probe))
        real_audit = account_status.record_audit

        def broken_audit(session: Session, **options: object) -> None:
            # created_at 为 NULL：提交时 NOT NULL 失败，整个事务回滚，按 500 返回。
            real_audit(session, **{**options, "now": None})

        monkeypatch.setattr(account_status, "record_audit", broken_audit)
        failed = change(http, headers, created["id"], to("ENABLED", probe))
    finally:
        root.removeHandler(handler)

    try:
        assert succeeded.status_code == 200, succeeded.text
        assert (failed.status_code, error_code(failed)) == (500, "INTERNAL_ERROR")
        logged = stream.getvalue()
        # 这次的异常确实进了日志 —— 否则下面那条是在空字符串上空转。
        assert "NOT NULL constraint failed: audit_logs.created_at" in logged
        assert "SQL parameters hidden" in logged
        assert probe not in logged
        assert probe not in failed.text
        # 失败那次什么都没留下：仍是 DISABLED、版本 1，只有成功那一条审计与事件。
        status, _billing, version, _updated = stored_tenant(application, created["id"])
        assert (status, version) == (AccountStatus.DISABLED, 1)
        assert len(audits(application)) == 1
        assert len(events(application)) == 1
    finally:
        engine.dispose()

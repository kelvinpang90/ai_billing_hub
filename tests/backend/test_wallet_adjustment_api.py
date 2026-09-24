"""Admin wallet adjustments end to end on SQLite (design gate #111 v1 §7).

这里测接口契约：鉴权、校验、字段白名单、幂等重放与冲突、404、日志里没有原因文本。

⚠️ SQLite 上没有迁移 0006 的钱包触发器，钱包余额不随账本推进（设计 §7）。所以这里
**每个客户只记第一笔**（钱包从 0 开始，那一笔的结果与 MySQL 一致）；凡是依赖记账前
余额的场景（跨零、连续记账、余额超出范围、并发同键）都在
test_wallet_adjustment_service.py 的真 MySQL 那一半。

鉴权的 401 / 403 与「不写库」由 test_admin_customers_api.py 的路由枚举用例覆盖
（本接口已加进 `EXPECTED_ADMIN_ROUTES` 与 `VALID_BODIES`）。
"""

from __future__ import annotations

import ast
import inspect
import io
import json
import logging
import textwrap
import typing
import uuid
from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, delete, func, select, text
from sqlalchemy.pool import StaticPool

from app.api import admin_customers
from app.core.config import Settings
from app.core.database import create_session_factory
from app.core.logging import JsonFormatter
from app.core.tokens import issue_access_token
from app.main import create_app
from app.models.auth import AuditAction, AuditLog, DomainOutbox, User, UserRole, UserStatus
from app.models.base import Base
from app.models.tenancy import BillingStatus, Tenant
from app.models.wallet import REFERENCE_TYPE_FOR, ReferenceType, Wallet, WalletTransaction
from app.schemas.wallet_adjustments import AdjustmentType
from app.services.auth import utc_now

CUSTOMERS = "/api/v1/admin/customers"

# 响应字段白名单（设计 §2）。断言「恰好等于」：多一个键（内部 id、created_by、metadata、
# 阈值、成本）就红（INV-7）。
ADJUSTMENT_FIELDS = {
    "id",
    "customer_id",
    "transaction_type",
    "amount",
    "balance_before",
    "balance_after",
    "wallet_sequence",
    "reason",
    "idempotency_key",
    "created_at",
    "replayed",
    "billing_status",
    "status_version",
}
ENVELOPE_FIELDS = {"success", "data", "error", "request_id"}

REASON = "Goodwill credit for outage 2026-09-20"
USER_AGENT = "admin-console-test"


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


def token_for(application: FastAPI, user_id: int) -> dict[str, str]:
    token = issue_access_token(
        application.state.settings,
        user_id=user_id,
        role="ADMIN",
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


def new_customer(client: TestClient, headers: dict[str, str], name: str = "Acme Sdn Bhd") -> str:
    body = {"company_name": name, "email": "ops@example.com"}
    response = client.post(CUSTOMERS, json=body, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()["data"]["id"]


@pytest.fixture
def customer_id(client, admin) -> str:
    return new_customer(client, admin)


def url(customer_id: str) -> str:
    return f"{CUSTOMERS}/{customer_id}/wallet/adjustments"


def body(**fields: object) -> dict[str, object]:
    """A valid credit of 20, unless overridden. A fresh idempotency key every time."""
    values: dict[str, object] = {
        "transaction_type": "ADJUSTMENT_CREDIT",
        "amount": "20",
        "reason": REASON,
        "idempotency_key": str(uuid.uuid4()),
    }
    values.update(fields)
    return values


def count_rows(session, model, *conditions) -> int:
    statement = select(func.count()).select_from(model)
    for condition in conditions:
        statement = statement.where(condition)
    return session.execute(statement).scalar_one()


def counts(application: FastAPI) -> dict[str, int]:
    """Every table an adjustment could write to, and the one it must never add to."""
    posted = AuditLog.action == AuditAction.WALLET_ADJUSTMENT_POSTED
    changed = AuditLog.action == AuditAction.TENANT_BILLING_STATUS_CHANGED
    with application.state.session_factory() as session:
        return {
            "wallet_transactions": count_rows(session, WalletTransaction),
            "adjustment_audits": count_rows(session, AuditLog, posted),
            "status_audits": count_rows(session, AuditLog, changed),
            "audit_logs": count_rows(session, AuditLog),
            "domain_outbox": count_rows(session, DomainOutbox),
            "tenants": count_rows(session, Tenant),
            "wallets": count_rows(session, Wallet),
        }


def tenant_state(application: FastAPI, customer_id: str) -> tuple[BillingStatus, int]:
    statement = select(Tenant).where(Tenant.public_id == customer_id)
    with application.state.session_factory() as session:
        tenant = session.execute(statement).scalar_one()
        return tenant.billing_status, tenant.status_version


def ledger(application: FastAPI) -> list[WalletTransaction]:
    statement = select(WalletTransaction).order_by(WalletTransaction.id)
    with application.state.session_factory() as session:
        return list(session.execute(statement).scalars())


def audits(application: FastAPI, action: AuditAction) -> list[AuditLog]:
    statement = select(AuditLog).where(AuditLog.action == action).order_by(AuditLog.id)
    with application.state.session_factory() as session:
        return list(session.execute(statement).scalars())


def error_code(response) -> str:
    payload = response.json()
    assert set(payload) == ENVELOPE_FIELDS
    assert payload["success"] is False
    assert payload["data"] is None
    return payload["error"]["code"]


# --- 正常路径 -------------------------------------------------------------------


def test_an_admin_posts_a_credit_and_it_is_audited(
    client, app, admin, admin_id, customer_id
) -> None:
    before = counts(app)
    key = str(uuid.uuid4())

    response = client.post(
        url(customer_id),
        json=body(amount="20", idempotency_key=key, reason=f"  {REASON}  "),
        headers={**admin, "User-Agent": USER_AGENT},
    )

    assert response.status_code == 201, response.text
    payload = response.json()
    assert set(payload) == ENVELOPE_FIELDS
    assert (payload["success"], payload["error"]) == (True, None)
    data = payload["data"]
    assert set(data) == ADJUSTMENT_FIELDS
    assert str(uuid.UUID(data["id"])) == data["id"]
    assert data["customer_id"] == customer_id
    assert data["transaction_type"] == "ADJUSTMENT_CREDIT"
    # INV-10：字符串、恰好 8 位小数。
    assert (data["amount"], data["balance_before"], data["balance_after"]) == (
        "20.00000000",
        "0.00000000",
        "20.00000000",
    )
    assert data["wallet_sequence"] == 1
    # 原因存去掉首尾空白后的值。
    assert data["reason"] == REASON
    assert data["idempotency_key"] == key
    assert data["replayed"] is False
    # 余额 0 → 20 跨零：同一事务里恢复 ACTIVE，状态版本 +1。
    assert (data["billing_status"], data["status_version"]) == ("ACTIVE", 1)
    assert tenant_state(app, customer_id) == (BillingStatus.ACTIVE, 1)

    assert counts(app) == {
        **before,
        "wallet_transactions": 1,
        "adjustment_audits": 1,
        "status_audits": 1,
        "audit_logs": before["audit_logs"] + 2,
        "domain_outbox": 1,
    }
    [row] = ledger(app)
    assert row.public_id == data["id"]
    assert row.reference_type is ReferenceType.ADMIN_ADJUSTMENT
    assert row.reference_id == key
    assert row.created_by == admin_id
    assert row.description == REASON
    assert row.metadata_json is None
    assert row.created_at.isoformat() == data["created_at"]
    # 时刻截到整秒（MySQL 的 DATETIME 不存小数秒）。
    assert row.created_at.microsecond == 0

    [audit] = audits(app, AuditAction.WALLET_ADJUSTMENT_POSTED)
    assert (audit.actor_user_id, audit.actor_role) == (admin_id, "ADMIN")
    assert (audit.entity_type, audit.entity_id) == ("wallet_transaction", data["id"])
    assert json.loads(audit.before_state or "{}") == {"balance": "0.00000000"}
    assert json.loads(audit.after_state or "{}") == {"balance": "20.00000000"}
    assert audit.reason == REASON
    # TestClient 的对端地址就是 "testclient"。
    assert (audit.ip_address, audit.user_agent) == ("testclient", USER_AGENT)
    assert audit.created_at == row.created_at

    # 跃迁审计的操作者是系统：不带发起请求的人的 ip 与 user agent（设计 §2）。
    [transition] = audits(app, AuditAction.TENANT_BILLING_STATUS_CHANGED)
    assert (transition.actor_user_id, transition.actor_role) == (None, "SYSTEM")
    assert (transition.ip_address, transition.user_agent) == (None, None)
    assert transition.reason == "BALANCE_POSITIVE"
    assert json.loads(transition.after_state or "{}")["wallet_transaction"] == data["id"]


@pytest.mark.parametrize(
    ("kind", "amount", "posted"),
    [
        ("ADJUSTMENT_CREDIT", "20", "20.00000000"),
        ("BONUS", "5.5", "5.50000000"),
        ("ADJUSTMENT_DEBIT", "-5.12345678", "-5.12345678"),
        ("REFUND_ADJUSTMENT", "-999999999999.99999999", "-999999999999.99999999"),
    ],
)
def test_each_adjustment_type_posts_with_its_sign(
    client, app, admin, customer_id, kind: str, amount: str, posted: str
) -> None:
    response = client.post(
        url(customer_id), json=body(transaction_type=kind, amount=amount), headers=admin
    )

    assert response.status_code == 201, response.text
    data = response.json()["data"]
    assert (data["transaction_type"], data["amount"], data["balance_after"]) == (
        kind,
        posted,
        posted,
    )
    [row] = ledger(app)
    assert row.transaction_type == kind


def test_a_long_user_agent_is_cut_to_the_column_width(client, app, admin, customer_id) -> None:
    response = client.post(
        url(customer_id), json=body(), headers={**admin, "User-Agent": "x" * 600}
    )

    assert response.status_code == 201
    [audit] = audits(app, AuditAction.WALLET_ADJUSTMENT_POSTED)
    assert audit.user_agent == "x" * 512


# --- 鉴权 -----------------------------------------------------------------------


def test_the_handler_calls_require_admin_first() -> None:
    """设计 §7：处理函数体的第一条语句就是 `require_admin(...)`，前面什么都没有。"""
    source = textwrap.dedent(inspect.getsource(admin_customers.post_wallet_adjustment))
    [function] = ast.parse(source).body
    assert isinstance(function, ast.FunctionDef)
    first = function.body[0]

    assert isinstance(first, ast.Assign | ast.Expr)
    call = first.value
    assert isinstance(call, ast.Call)
    assert isinstance(call.func, ast.Name)
    assert call.func.id == "require_admin"


def test_the_adjustment_types_are_exactly_the_admin_adjustment_ones() -> None:
    """请求的类型白名单与 `REFERENCE_TYPE_FOR` 里映射到 ADMIN_ADJUSTMENT 的四种一致。"""
    admin_adjustment = {
        kind.value
        for kind, source in REFERENCE_TYPE_FOR.items()
        if source is ReferenceType.ADMIN_ADJUSTMENT
    }

    assert set(typing.get_args(AdjustmentType)) == admin_adjustment


def test_a_customer_token_cannot_adjust(client, app, customer_id) -> None:
    headers = token_for(app, make_user(app, role=UserRole.CUSTOMER))
    before = counts(app)

    response = client.post(url(customer_id), json=body(), headers=headers)

    assert (response.status_code, error_code(response)) == (403, "ADMIN_REQUIRED")
    assert counts(app) == before
    assert tenant_state(app, customer_id) == (BillingStatus.SUSPENDED, 0)


def test_without_a_database_the_route_says_so(tmp_path) -> None:
    application = create_app(settings_for(tmp_path))
    token = issue_access_token(
        application.state.settings, user_id=1, role="ADMIN", session_id="s", now=utc_now()
    )

    response = TestClient(application).post(
        url(str(uuid.uuid4())), json=body(), headers={"Authorization": f"Bearer {token}"}
    )

    assert (response.status_code, error_code(response)) == (503, "DATABASE_NOT_CONFIGURED")


# --- 校验：422，不写库 ----------------------------------------------------------


def assert_refused(client, app, customer_id: str, payload: dict, headers) -> None:
    before, state = counts(app), tenant_state(app, customer_id)

    response = client.post(url(customer_id), json=payload, headers=headers)

    assert (response.status_code, error_code(response)) == (422, "VALIDATION_ERROR")
    assert counts(app) == before
    assert tenant_state(app, customer_id) == state


@pytest.mark.parametrize(
    ("reason", "expected"),
    [
        (None, 422),
        ("   ", 422),
        ("", 422),
        ("x" * 256, 422),
        ("x" * 255, 201),
        # 先去首尾空白再量长度。
        ("  " + "x" * 255 + "  ", 201),
    ],
    ids=["missing", "blank", "empty", "256", "255", "255-padded"],
)
def test_reason_bounds(client, app, admin, customer_id, reason, expected: int) -> None:
    payload = body(reason=reason)
    if reason is None:
        del payload["reason"]

    if expected == 422:
        assert_refused(client, app, customer_id, payload, admin)
        return
    response = client.post(url(customer_id), json=payload, headers=admin)
    assert response.status_code == 201
    assert response.json()["data"]["reason"] == "x" * 255
    assert ledger(app)[0].description == "x" * 255


@pytest.mark.parametrize(
    "amount",
    [
        "0.000000001",
        "0",
        "-0.00000000",
        20,
        20.5,
        "1e2",
        "1" * 13,
        "-" + "1" * 13,
        "",
        " 20",
        "20 ",
        "+20",
        "20.",
        ".5",
        "NaN",
        "Infinity",
        "٢٠",
        None,
    ],
    ids=[
        "9-places",
        "zero",
        "negative-zero",
        "json-int",
        "json-float",
        "exponent",
        "13-digits",
        "13-digits-negative",
        "empty",
        "leading-space",
        "trailing-space",
        "plus-sign",
        "trailing-dot",
        "leading-dot",
        "nan",
        "infinity",
        "arabic-indic-digits",
        "null",
    ],
)
def test_bad_amounts_are_refused(client, app, admin, customer_id, amount) -> None:
    """INV-10：只收带符号的十进制字符串，超过 8 位小数一律 422，不舍入。"""
    assert_refused(client, app, customer_id, body(amount=amount), admin)


def test_the_smallest_amount_posts_exactly(client, app, admin, customer_id) -> None:
    response = client.post(url(customer_id), json=body(amount="0.00000001"), headers=admin)

    assert response.status_code == 201
    assert response.json()["data"]["amount"] == "0.00000001"
    [row] = ledger(app)
    assert row.amount == Decimal("0.00000001")


def test_a_missing_amount_is_refused(client, app, admin, customer_id) -> None:
    payload = body()
    del payload["amount"]

    assert_refused(client, app, customer_id, payload, admin)


@pytest.mark.parametrize(
    ("kind", "amount"),
    [
        ("ADJUSTMENT_CREDIT", "-20"),
        ("BONUS", "-20"),
        ("ADJUSTMENT_DEBIT", "20"),
        ("REFUND_ADJUSTMENT", "20"),
        ("TOPUP", "20"),
        ("AI_USAGE", "-20"),
        ("SYSTEM_CORRECTION", "20"),
        ("SYSTEM_CORRECTION", "-20"),
        ("REBILL_CREDIT", "20"),
        ("REBILL_DEBIT", "-20"),
        ("adjustment_credit", "20"),
        (None, "20"),
    ],
    ids=str,
)
def test_type_and_sign_must_agree(client, app, admin, customer_id, kind, amount: str) -> None:
    """INV-2 / INV-3：用量与充值不能经调账进来；符号与类型不符也是 422。"""
    assert_refused(client, app, customer_id, body(transaction_type=kind, amount=amount), admin)


@pytest.mark.parametrize(
    "extra",
    [
        {"customer_id": "00000000-0000-4000-8000-000000000000"},
        {"tenant_id": 1},
        {"created_by": 1},
        {"metadata": {"note": "x"}},
        {"balance": "100.00000000"},
        {"id": "00000000-0000-4000-8000-000000000000"},
        {"reference_type": "PAYMENT"},
    ],
    ids=["customer_id", "tenant_id", "created_by", "metadata", "balance", "id", "reference_type"],
)
def test_the_body_refuses_extra_fields(client, app, admin, customer_id, extra: dict) -> None:
    """INV-8：客户只来自路径，操作者只来自令牌（extra="forbid"）。"""
    assert_refused(client, app, customer_id, body(**extra), admin)


@pytest.mark.parametrize(
    "key",
    [
        None,
        "",
        "not-a-uuid",
        "B7A1C2D3-0000-4000-8000-000000000000",
        "b7a1c2d3000040008000000000000000",
        "{b7a1c2d3-0000-4000-8000-000000000000}",
        "b7a1c2d3-0000-4000-8000-000000000000 ",
        12345,
    ],
    ids=["missing", "empty", "not-uuid", "uppercase", "no-hyphens", "braces", "space", "number"],
)
def test_the_idempotency_key_is_a_lowercase_uuid(client, app, admin, customer_id, key) -> None:
    payload = body(idempotency_key=key)
    if key is None:
        del payload["idempotency_key"]

    assert_refused(client, app, customer_id, payload, admin)


def test_a_validation_error_does_not_echo_the_reason(client, admin, customer_id) -> None:
    secret = "reason-probe-5c1e " + "x" * 300

    response = client.post(url(customer_id), json=body(reason=secret), headers=admin)

    assert response.status_code == 422
    assert "reason-probe-5c1e" not in response.text


# --- 幂等 -----------------------------------------------------------------------


def test_the_same_request_twice_posts_once(client, app, admin, customer_id) -> None:
    """INV-11：第一次 201，第二次 200 + `replayed: true`，同一个账本行，不再写任何东西。"""
    payload = body()

    first = client.post(url(customer_id), json=payload, headers=admin)
    after_first = counts(app)
    second = client.post(url(customer_id), json=payload, headers=admin)

    assert (first.status_code, second.status_code) == (201, 200)
    one, two = first.json()["data"], second.json()["data"]
    assert (one["replayed"], two["replayed"]) == (False, True)
    # 除 `replayed` 外逐字相同（中间没有别的记账，状态也一样）。
    assert {**two, "replayed": False} == one
    assert counts(app) == after_first
    assert len(ledger(app)) == 1
    assert len(audits(app, AuditAction.WALLET_ADJUSTMENT_POSTED)) == 1


def test_a_replay_with_another_reason_returns_the_first_reason(
    client, app, admin, customer_id
) -> None:
    """设计 §4：判等只看财务效果；原因不同仍是重放，响应里是库里那一份。"""
    payload = body(reason="First reason")
    client.post(url(customer_id), json=payload, headers=admin)

    response = client.post(url(customer_id), json={**payload, "reason": "Other"}, headers=admin)

    assert response.status_code == 200
    assert response.json()["data"]["reason"] == "First reason"
    assert len(ledger(app)) == 1


@pytest.mark.parametrize(
    "change",
    [
        {"amount": "21"},
        {"amount": "20.00000001"},
        {"transaction_type": "BONUS"},
    ],
    ids=["amount", "amount-last-place", "type"],
)
def test_the_same_key_with_another_payload_conflicts(
    client, app, admin, customer_id, change: dict
) -> None:
    payload = body()
    first = client.post(url(customer_id), json=payload, headers=admin)
    assert first.status_code == 201
    before, state = counts(app), tenant_state(app, customer_id)

    response = client.post(url(customer_id), json={**payload, **change}, headers=admin)

    assert (response.status_code, error_code(response)) == (409, "ADJUSTMENT_CONFLICT")
    assert counts(app) == before
    assert tenant_state(app, customer_id) == state
    [row] = ledger(app)
    assert row.public_id == first.json()["data"]["id"]


def test_a_key_used_on_one_customer_conflicts_on_another(client, app, admin) -> None:
    """INV-8：A 的键在 B 上是 409，拿不到 A 的账本行，也看不到 A 的任何字段。"""
    first_customer = new_customer(client, admin, "First Sdn Bhd")
    second_customer = new_customer(client, admin, "Second Sdn Bhd")
    payload = body(reason="First customer's credit")
    posted = client.post(url(first_customer), json=payload, headers=admin).json()["data"]
    before = counts(app)
    second_state = tenant_state(app, second_customer)

    response = client.post(url(second_customer), json=payload, headers=admin)

    assert (response.status_code, error_code(response)) == (409, "ADJUSTMENT_CONFLICT")
    for value in (first_customer, posted["id"], "First customer's credit", "20.00000000"):
        assert value not in response.text
    assert counts(app) == before
    assert tenant_state(app, second_customer) == second_state


# --- 客户不存在 -----------------------------------------------------------------


def test_unknown_customers_are_404_and_write_nothing(client, app, admin, customer_id) -> None:
    before = counts(app)

    responses = [
        client.post(url(unknown), json=body(), headers=admin)
        for unknown in (str(uuid.uuid4()), "1", "0")
    ]

    assert {response.status_code for response in responses} == {404}
    assert {error_code(response) for response in responses} == {"CUSTOMER_NOT_FOUND"}
    assert counts(app) == before


def test_a_customer_without_a_wallet_is_an_internal_error(client, app, admin, customer_id) -> None:
    """数据不一致（每个客户都应有钱包）：500，固定文案，什么都不写。"""
    with app.state.session_factory() as session:
        session.execute(delete(Wallet))
        session.commit()
    before = counts(app)

    response = TestClient(app, raise_server_exceptions=False).post(
        url(customer_id), json=body(), headers=admin
    )

    assert (response.status_code, error_code(response)) == (500, "INTERNAL_ERROR")
    assert counts(app) == before


# --- 日志不含原因文本（设计 §6） ------------------------------------------------

SUCCESS_REASON = "reason-probe-success-8d2f Contact Person 012-345 6789"
FAILURE_REASON = "reason-probe-failure-3b7c Contact Person 012-345 6789"


def test_reasons_never_reach_the_logs(tmp_path) -> None:
    """一次成功、一次在数据库层失败，两段原因文本都不进日志。

    引擎由 `create_database_engine` 建（`create_app` 用的就是它，与生产同一个工厂），
    失败那一次的 INSERT 参数里正带着原因 —— `hide_parameters=True` 让它不进异常文本。
    """
    database = tmp_path / "billing.db"
    application = create_app(
        settings_for(tmp_path, database_url=f"sqlite+pysqlite:///{database.as_posix()}")
    )
    engine = application.state.engine
    assert engine is not None
    Base.metadata.create_all(engine)
    headers = token_for(application, make_user(application))
    client = TestClient(application, raise_server_exceptions=False)
    first = new_customer(client, headers, "First Sdn Bhd")
    second = new_customer(client, headers, "Second Sdn Bhd")

    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        succeeded = client.post(url(first), json=body(reason=SUCCESS_REASON), headers=headers)
        # 调账审计的 INSERT 找不到表：flush 失败，整个事务回滚（OperationalError，不重试）。
        with engine.begin() as connection:
            connection.execute(text("DROP TABLE audit_logs"))
        failed = client.post(url(second), json=body(reason=FAILURE_REASON), headers=headers)
    finally:
        root.removeHandler(handler)

    try:
        assert succeeded.status_code == 201
        assert (failed.status_code, error_code(failed)) == (500, "INTERNAL_ERROR")
        logged = stream.getvalue()
        # 这次的异常确实进了日志 —— 否则下面几条是在空字符串上空转。
        assert "no such table: audit_logs" in logged
        assert "SQL parameters hidden" in logged
        for reason in (SUCCESS_REASON, FAILURE_REASON):
            assert reason not in logged
        assert "Contact Person" not in logged
        assert FAILURE_REASON not in failed.text
        # 失败的那一笔没有留下账本行。
        with application.state.session_factory() as session:
            assert count_rows(session, WalletTransaction) == 1
    finally:
        engine.dispose()

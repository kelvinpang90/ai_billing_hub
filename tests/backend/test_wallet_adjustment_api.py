"""Admin wallet adjustment endpoint on SQLite (design gate #111 v1 §7).

⚠️ SQLite 上没有钱包触发器，钱包余额不会随账本推进（AIH-TASK-005 §7）。所以这里
**每个客户至多成功记一笔**：钱包从 0 开始的第一笔结果是对的，可以测响应格式；第二笔
会拿到同一个序号。凡是依赖「记账前余额」的场景（跨零、连续记账、余额超出范围、并发）
都在 test_wallet_adjustment_service.py 的真 MySQL 上。

这里测接口契约：鉴权、字段白名单、校验边界、幂等重放与冲突、租户越权、日志里没有
原因文本。匿名 401 / CUSTOMER 403 由 test_admin_customers_api.py 的路由枚举用例覆盖。
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
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.pool import StaticPool

from app.api import admin_customers
from app.core.config import Settings
from app.core.database import create_session_factory
from app.core.logging import JsonFormatter
from app.core.tokens import issue_access_token
from app.main import create_app
from app.models.auth import AuditAction, AuditLog, DomainOutbox, User, UserRole, UserStatus
from app.models.base import Base
from app.models.tenancy import Tenant
from app.models.wallet import Wallet, WalletTransaction
from app.repositories.tenancy import create_tenant
from app.services.auth import utc_now

CUSTOMERS = "/api/v1/admin/customers"

# 响应字段白名单（设计 §2）。断言「恰好等于」：多一个键（内部 id、created_by、
# metadata、成本）就红（INV-7）。
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
# 取自账本行的字段：重放时与第一次逐字相同。
LEDGER_FIELDS = ADJUSTMENT_FIELDS - {"replayed", "billing_status", "status_version"}
ENVELOPE_FIELDS = {"success", "data", "error", "request_id"}

USER_AGENT = "admin-console-adjustment-test"


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


def make_user(application: FastAPI, *, role: UserRole = UserRole.ADMIN) -> int:
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


def new_customer(client: TestClient, headers: dict[str, str]) -> str:
    body = {"company_name": "Acme Sdn Bhd", "email": "ops@example.com"}
    response = client.post(CUSTOMERS, json=body, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()["data"]["id"]


def adjustments_url(customer_id: str) -> str:
    return f"{CUSTOMERS}/{customer_id}/wallet/adjustments"


def adjustment(**overrides: object) -> dict[str, object]:
    """A valid request body with a fresh idempotency key, unless overridden."""
    body: dict[str, object] = {
        "transaction_type": "ADJUSTMENT_CREDIT",
        "amount": "20",
        "reason": "Goodwill credit for outage 2026-09-20",
        "idempotency_key": str(uuid.uuid4()),
    }
    body.update(overrides)
    return body


def without(field: str) -> dict[str, object]:
    body = adjustment()
    del body[field]
    return body


def count_rows(session, model) -> int:
    return session.execute(select(func.count()).select_from(model)).scalar_one()


def row_counts(application: FastAPI) -> dict[str, int]:
    """The tables an adjustment may write (design §7: none of them may move on a refusal)."""
    with application.state.session_factory() as session:
        return {
            model.__tablename__: count_rows(session, model)
            for model in (WalletTransaction, AuditLog, Tenant, DomainOutbox, Wallet)
        }


def ledger(application: FastAPI) -> list[WalletTransaction]:
    statement = select(WalletTransaction).order_by(WalletTransaction.id)
    with application.state.session_factory() as session:
        return list(session.execute(statement).scalars())


def audits(application: FastAPI, action: AuditAction) -> list[AuditLog]:
    statement = select(AuditLog).where(AuditLog.action == action).order_by(AuditLog.id)
    with application.state.session_factory() as session:
        return list(session.execute(statement).scalars())


def error_code(response) -> str:
    body = response.json()
    assert set(body) == ENVELOPE_FIELDS
    assert body["success"] is False
    assert body["data"] is None
    return body["error"]["code"]


# --- 正常路径 -------------------------------------------------------------------


def test_an_admin_posts_a_credit_with_its_audit(client, app, admin, admin_id) -> None:
    customer_id = new_customer(client, admin)
    body = adjustment()
    before = row_counts(app)

    response = client.post(
        adjustments_url(customer_id), json=body, headers={**admin, "User-Agent": USER_AGENT}
    )

    assert response.status_code == 201, response.text
    envelope = response.json()
    assert set(envelope) == ENVELOPE_FIELDS
    assert (envelope["success"], envelope["error"]) == (True, None)
    data = envelope["data"]
    assert set(data) == ADJUSTMENT_FIELDS
    assert str(uuid.UUID(data["id"])) == data["id"]
    assert data["customer_id"] == customer_id
    assert data["transaction_type"] == "ADJUSTMENT_CREDIT"
    assert (data["amount"], data["balance_before"], data["balance_after"]) == (
        "20.00000000",
        "0.00000000",
        "20.00000000",
    )
    assert data["wallet_sequence"] == 1
    assert data["reason"] == body["reason"]
    assert data["idempotency_key"] == body["idempotency_key"]
    assert data["replayed"] is False
    # 0 → 20 跨过零线：同一事务里 SUSPENDED → ACTIVE。
    assert (data["billing_status"], data["status_version"]) == ("ACTIVE", 1)

    [row] = ledger(app)
    assert row.public_id == data["id"]
    assert row.created_by == admin_id
    assert (row.reference_type.value, row.reference_id) == (
        "ADMIN_ADJUSTMENT",
        body["idempotency_key"],
    )
    assert row.description == body["reason"]
    assert row.metadata_json is None
    assert row.created_at.isoformat() == data["created_at"]

    [audit] = audits(app, AuditAction.WALLET_ADJUSTMENT_POSTED)
    assert (audit.actor_user_id, audit.actor_role) == (admin_id, "ADMIN")
    assert (audit.entity_type, audit.entity_id) == ("wallet_transaction", data["id"])
    assert audit.reason == body["reason"]
    assert json.loads(audit.before_state or "{}") == {"balance": "0.00000000"}
    assert json.loads(audit.after_state or "{}") == {"balance": "20.00000000"}
    # TestClient 的对端地址就是这个串；没配可信代理时审计记的就是直连对端。
    assert audit.ip_address == "testclient"
    assert audit.user_agent == USER_AGENT
    # 计费状态跃迁的操作者是系统：不带 ip 与 user agent。
    [transition] = audits(app, AuditAction.TENANT_BILLING_STATUS_CHANGED)
    assert (transition.actor_user_id, transition.actor_role) == (None, "SYSTEM")
    assert (transition.ip_address, transition.user_agent) == (None, None)

    assert row_counts(app) == {
        **before,
        "wallet_transactions": 1,
        "audit_logs": before["audit_logs"] + 2,
        "domain_outbox": before["domain_outbox"] + 1,
    }
    # 客户详情接口看得到新的计费状态。
    detail = client.get(f"{CUSTOMERS}/{customer_id}", headers=admin).json()["data"]
    assert (detail["billing_status"], detail["status_version"]) == ("ACTIVE", 1)


# 12 位整数的上限在 MySQL 上测：SQLite 把 Numeric 当浮点数存，存不下 20 位有效数字。
@pytest.mark.parametrize(
    ("kind", "amount", "shown"),
    [
        ("ADJUSTMENT_CREDIT", "20", "20.00000000"),
        ("BONUS", "0.5", "0.50000000"),
        ("ADJUSTMENT_DEBIT", "-5.12345678", "-5.12345678"),
        ("REFUND_ADJUSTMENT", "-12345.6789", "-12345.67890000"),
    ],
)
def test_each_adjustment_type_posts_with_its_own_sign(
    client, app, admin, kind: str, amount: str, shown: str
) -> None:
    customer_id = new_customer(client, admin)

    response = client.post(
        adjustments_url(customer_id),
        json=adjustment(transaction_type=kind, amount=amount),
        headers=admin,
    )

    assert response.status_code == 201, response.text
    data = response.json()["data"]
    assert data["transaction_type"] == kind
    assert (data["amount"], data["balance_after"]) == (shown, shown)
    assert len(ledger(app)) == 1


# --- 校验：422，不写库 ----------------------------------------------------------


def assert_refused(client, app, admin, body: object) -> None:
    customer_id = new_customer(client, admin)
    before = row_counts(app)

    response = client.post(adjustments_url(customer_id), json=body, headers=admin)

    assert (response.status_code, error_code(response)) == (422, "VALIDATION_ERROR")
    assert row_counts(app) == before


@pytest.mark.parametrize(
    ("reason", "expected"),
    [
        (None, 422),
        ("   ", 422),
        ("x" * 256, 422),
        ("x" * 255, 201),
        # 先去首尾空白再量长度，存的也是去掉之后的值。
        ("  " + "x" * 255 + "\t", 201),
    ],
    ids=["missing", "blank", "256", "255", "255-padded"],
)
def test_reason_bounds(client, app, admin, reason: str | None, expected: int) -> None:
    body = without("reason") if reason is None else adjustment(reason=reason)
    if expected == 422:
        assert_refused(client, app, admin, body)
        return

    response = client.post(adjustments_url(new_customer(client, admin)), json=body, headers=admin)

    assert response.status_code == 201, response.text
    assert response.json()["data"]["reason"] == "x" * 255
    [row] = ledger(app)
    assert row.description == "x" * 255


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
        "",
        " 20",
        "+20",
        "20.",
        ".5",
        "NaN",
        "Infinity",
        "１２",
        None,
    ],
    ids=[
        "9-places",
        "zero",
        "negative-zero",
        "json-int",
        "json-float",
        "exponent",
        "13-integer-digits",
        "empty",
        "leading-space",
        "plus-sign",
        "trailing-dot",
        "leading-dot",
        "nan",
        "infinity",
        "fullwidth-digits",
        "null",
    ],
)
def test_bad_amounts_are_refused_without_rounding(client, app, admin, amount: object) -> None:
    """INV-10：只收带符号的十进制字符串；超过 8 位小数是 422，不舍入。"""
    assert_refused(client, app, admin, adjustment(amount=amount))


def test_the_smallest_amount_posts_exactly(client, app, admin) -> None:
    customer_id = new_customer(client, admin)

    response = client.post(
        adjustments_url(customer_id), json=adjustment(amount="0.00000001"), headers=admin
    )

    assert response.status_code == 201, response.text
    assert response.json()["data"]["amount"] == "0.00000001"


def test_a_missing_amount_is_refused(client, app, admin) -> None:
    assert_refused(client, app, admin, without("amount"))


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
        ("REBILL_CREDIT", "20"),
        ("REBILL_DEBIT", "-20"),
        ("NOT_A_TYPE", "20"),
        ("adjustment_credit", "20"),
        (None, "20"),
    ],
    ids=str,
)
def test_type_and_sign_must_agree(client, app, admin, kind: str | None, amount: str) -> None:
    """INV-2 / INV-3：调账冒充不了扣费或充值；方向写反被 422 挡住（设计 §9）。"""
    body = without("transaction_type") if kind is None else adjustment(transaction_type=kind)
    body["amount"] = amount
    assert_refused(client, app, admin, body)


@pytest.mark.parametrize(
    "key",
    [
        None,
        "",
        "not-a-uuid",
        str(uuid.uuid4()).upper(),
        uuid.uuid4().hex,
        str(uuid.uuid4()) + " ",
        12345,
    ],
    ids=["missing", "empty", "not-a-uuid", "uppercase", "no-dashes", "trailing-space", "number"],
)
def test_the_idempotency_key_must_be_a_lowercase_uuid(client, app, admin, key: object) -> None:
    body = without("idempotency_key") if key is None else adjustment(idempotency_key=key)
    assert_refused(client, app, admin, body)


@pytest.mark.parametrize(
    "extra",
    [
        {"customer_id": "00000000-0000-4000-8000-000000000000"},
        {"tenant_id": 1},
        {"created_by": 1},
        {"metadata": {"note": "x"}},
        {"balance": "100.00000000"},
        {"id": "00000000-0000-4000-8000-000000000000"},
    ],
    ids=["customer_id", "tenant_id", "created_by", "metadata", "balance", "id"],
)
def test_the_body_refuses_extra_fields(client, app, admin, extra: dict) -> None:
    """INV-8：客户只来自路径，操作者只来自令牌（extra="forbid"）。"""
    assert_refused(client, app, admin, {**adjustment(), **extra})


def test_a_validation_error_does_not_echo_the_reason(client, app, admin) -> None:
    reason = "Probe reason 5c1f-echo that must not come back"
    customer_id = new_customer(client, admin)

    response = client.post(
        adjustments_url(customer_id), json=adjustment(reason=reason, amount=20), headers=admin
    )

    assert response.status_code == 422
    assert reason not in response.text
    assert "amount" in response.json()["error"]["message"]


# --- 幂等 -----------------------------------------------------------------------


def test_the_same_body_twice_posts_once(client, app, admin) -> None:
    customer_id = new_customer(client, admin)
    body = adjustment()

    first = client.post(adjustments_url(customer_id), json=body, headers=admin)
    second = client.post(adjustments_url(customer_id), json=body, headers=admin)

    assert (first.status_code, second.status_code) == (201, 200)
    one, two = first.json()["data"], second.json()["data"]
    assert (one["replayed"], two["replayed"]) == (False, True)
    assert set(two) == ADJUSTMENT_FIELDS
    same = {field for field in LEDGER_FIELDS if one[field] == two[field]}
    assert same == LEDGER_FIELDS
    assert len(ledger(app)) == 1
    assert len(audits(app, AuditAction.WALLET_ADJUSTMENT_POSTED)) == 1
    assert len(audits(app, AuditAction.TENANT_BILLING_STATUS_CHANGED)) == 1


def test_a_replay_with_another_reason_returns_the_first_reason(client, app, admin) -> None:
    """设计 §4：判等只看财务效果；原因不同按重放处理，返回库里那一份。"""
    customer_id = new_customer(client, admin)
    body = adjustment(reason="First reason")
    client.post(adjustments_url(customer_id), json=body, headers=admin)

    response = client.post(
        adjustments_url(customer_id), json={**body, "reason": "Second reason"}, headers=admin
    )

    assert response.status_code == 200
    assert response.json()["data"]["reason"] == "First reason"
    assert [row.description for row in ledger(app)] == ["First reason"]


@pytest.mark.parametrize(
    "change",
    [
        {"amount": "21"},
        {"amount": "20.00000001"},
        {"transaction_type": "BONUS"},
        {"transaction_type": "ADJUSTMENT_DEBIT", "amount": "-20"},
    ],
    ids=["amount", "amount-last-place", "type", "type-and-sign"],
)
def test_the_same_key_with_another_payload_conflicts(client, app, admin, change: dict) -> None:
    customer_id = new_customer(client, admin)
    body = adjustment()
    client.post(adjustments_url(customer_id), json=body, headers=admin)
    before = row_counts(app)

    response = client.post(adjustments_url(customer_id), json={**body, **change}, headers=admin)

    assert (response.status_code, error_code(response)) == (409, "ADJUSTMENT_CONFLICT")
    assert row_counts(app) == before
    assert [row.amount for row in ledger(app)] == [20]


def test_a_key_used_on_one_customer_conflicts_on_another(client, app, admin) -> None:
    """INV-8：A 的键在 B 上是冲突，拿不到 A 的账本行，也看不到 A 的任何字段。"""
    first = new_customer(client, admin)
    second = new_customer(client, admin)
    body = adjustment()
    posted = client.post(adjustments_url(first), json=body, headers=admin).json()["data"]
    before = row_counts(app)

    response = client.post(adjustments_url(second), json=body, headers=admin)

    assert (response.status_code, error_code(response)) == (409, "ADJUSTMENT_CONFLICT")
    for leaked in (posted["id"], first, posted["reason"], posted["created_at"]):
        assert leaked not in response.text
    assert row_counts(app) == before
    with app.state.session_factory() as session:
        tenant = session.execute(select(Tenant).where(Tenant.public_id == second)).scalar_one()
        assert (tenant.billing_status.value, tenant.status_version) == ("SUSPENDED", 0)


# --- 租户越权与数据不一致 -------------------------------------------------------


def test_unknown_customers_are_404_and_write_nothing(client, app, admin) -> None:
    new_customer(client, admin)
    before = row_counts(app)

    responses = [
        client.post(adjustments_url(unknown), json=adjustment(), headers=admin)
        for unknown in (str(uuid.uuid4()), "1")
    ]

    assert [response.status_code for response in responses] == [404, 404]
    assert {error_code(response) for response in responses} == {"CUSTOMER_NOT_FOUND"}
    assert row_counts(app) == before


def test_a_customer_without_a_wallet_is_a_500_and_writes_nothing(app, admin) -> None:
    """每个客户都应有钱包；缺了是数据不一致，按意外错误处理（设计 §2「错误」）。"""
    with app.state.session_factory() as session:
        tenant = create_tenant(
            session, company_name="No Wallet Sdn Bhd", email="ops@example.com", now=utc_now()
        )
        session.commit()
        customer_id = tenant.public_id
    before = row_counts(app)

    response = TestClient(app, raise_server_exceptions=False).post(
        adjustments_url(customer_id), json=adjustment(), headers=admin
    )

    assert (response.status_code, error_code(response)) == (500, "INTERNAL_ERROR")
    assert row_counts(app) == before


def test_without_a_database_the_endpoint_says_so(tmp_path) -> None:
    application = create_app(settings_for(tmp_path))
    token = issue_access_token(
        application.state.settings, user_id=1, role="ADMIN", session_id="s", now=utc_now()
    )

    response = TestClient(application).post(
        adjustments_url(str(uuid.uuid4())),
        json=adjustment(),
        headers={"Authorization": f"Bearer {token}"},
    )

    assert (response.status_code, error_code(response)) == (503, "DATABASE_NOT_CONFIGURED")


# --- 鉴权：处理函数第一条语句 ---------------------------------------------------


def test_the_handler_calls_require_admin_first() -> None:
    """设计 §7：函数体第一条语句（跳过 docstring）是对 `require_admin` 的调用。"""
    source = textwrap.dedent(inspect.getsource(admin_customers.post_wallet_adjustment))
    [function] = ast.parse(source).body
    assert isinstance(function, ast.FunctionDef)
    body = function.body
    if ast.get_docstring(function) is not None:
        body = body[1:]
    first = body[0]
    call = first.value if isinstance(first, ast.Assign | ast.Expr) else None
    assert isinstance(call, ast.Call)
    assert isinstance(call.func, ast.Name) and call.func.id == "require_admin"


# --- 日志不含原因文本 -----------------------------------------------------------

PROBE_REASON = "Probe reason 9e2b: goodwill for Zebulon Quixote"


def test_the_reason_never_reaches_the_logs(tmp_path) -> None:
    """成功一次、再让它在数据库层失败一次：日志里都没有原因文本（设计 §6）。

    引擎由 `create_database_engine` 建（`create_app` 用的就是它），与生产同一个工厂。
    """
    database = tmp_path / "billing.db"
    url = f"sqlite+pysqlite:///{database.as_posix()}"
    application = create_app(settings_for(tmp_path, database_url=url))
    engine = application.state.engine
    assert engine is not None
    Base.metadata.create_all(engine)
    headers = token_for(application, make_user(application))
    http = TestClient(application, raise_server_exceptions=False)

    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        customer_id = new_customer(http, headers)
        posted = http.post(
            adjustments_url(customer_id), json=adjustment(reason=PROBE_REASON), headers=headers
        )
        with engine.begin() as connection:
            connection.execute(text("DROP TABLE wallet_transactions"))
        failed = http.post(
            adjustments_url(customer_id), json=adjustment(reason=PROBE_REASON), headers=headers
        )
    finally:
        root.removeHandler(handler)

    try:
        assert posted.status_code == 201, posted.text
        assert (failed.status_code, error_code(failed)) == (500, "INTERNAL_ERROR")
        logged = stream.getvalue()
        # 这次的异常确实进了日志 —— 否则下面那条是在空字符串上空转。
        assert "no such table: wallet_transactions" in logged
        assert "SQL parameters hidden" in logged
        assert PROBE_REASON not in logged
        assert PROBE_REASON not in failed.text
    finally:
        engine.dispose()

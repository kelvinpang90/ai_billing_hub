"""Payment routes end to end (design gate #580 v3, AIH-TASK-177).

`POST /api/v1/webhooks/payments/{gateway}` 与 `POST /api/v1/customer/topups`。
设计 §7 标 177 的各行与「建单校验」的接口部分：

- **回调验签与落库**：合规、错签名、改过的请求体、缺头、时间窗外、超大（声明的与分块的）、
  恰好 64 KiB、非 JSON、缺 `id` / `type`、`livemode` 不一致（`livemode` 取自密钥文件）、
  重复相同、重复不同、`gateway` 不是 `stripe`、密钥读不到 503、数据库不可用 503；
  WARNING 日志不带请求体与请求头；回调不在请求里确认；
- **伪造成功跳转**：只访问跳回地址、不发回调 → 支付仍 `PENDING`，无账本行；
- **端到端**：建单 → 回调 → 直接调用事件扫描 → 同支付的另一个事件 → 重复投递：钱包只加一次、
  支付 `PAID`、`gateway_fee` 写入；
- **建单校验**（接口部分）：开关关闭 404、鉴权与只读先于请求体、多余字段 422 先于
  `create_topup` 的有序校验、缺 `amount` 或不是字符串 422 `TOPUP_AMOUNT_INVALID`、
  未开放与名单、内部租户、无税务政策、60 秒复用、Stripe 失败 503 后重试复用同一笔与同一个幂等键；
- **客户接口字段**：只有三个；**跨租户**：请求体带别的租户字段被 422 拒绝、
  回调只给支付所属租户入账。

开关打开的应用用测试内的 `Settings` 构造；密钥文件写在 `tmp_path`。Stripe 一律是注入了假传输
（`httpx2.MockTransport`）的 `StripeGateway`（test_payment_create.py 的 `FakeStripe`），
不联网；地址是虚构的。事件扫描不经 Celery，直接调用 `process_due`。

世界沿用 test_payment_create.py（SQLite 与真 MySQL 各跑一次）。SQLite 用 `StaticPool`（TestClient
在线程池里跑处理函数）并装上 test_usage_billing 那条代替触发器推进钱包的测试触发器。MySQL 那部分需要
`BILLING_TEST_DATABASE_URL` 指向一个**可以被清空的**库，没设时 skip —— **不要把 skipped 读成
passed**，CI 设了它并把 skipped 判成失败。
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import logging
import re
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, delete, func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from test_payment_confirm import stripe_session
from test_payment_create import (
    API_BASE_URL,
    CHECKOUT_BASE_URL,
    CREATED,
    FRONTEND_BASE_URL,
    SECRET_KEY,
    TAX_FROM,
    TEST_EMAIL_DOMAIN,
    WEBHOOK_SECRET,
    Customer,
    World,
    _mysql_world,
    _populate,
    stripe_error,
)
from test_payment_events import event_body
from test_usage_billing import _SQLITE_LEDGER_TRIGGER

from app.api import payments as payments_api
from app.core.config import Settings
from app.core.database import create_session_factory
from app.core.tokens import issue_access_token
from app.main import create_app
from app.models.auth import User, UserRole, UserStatus
from app.models.base import Base
from app.models.payments import (
    Payment,
    PaymentEventProcessingStatus,
    PaymentGatewayEvent,
    PaymentStatus,
)
from app.models.tenancy import AccountStatus, BillingMode
from app.models.wallet import TransactionType, WalletTransaction
from app.payments.base import GatewayState
from app.repositories import tenancy
from app.repositories.wallet import create_wallet, get_wallet_for_tenant
from app.services.auth import utc_now
from app.services.payment_events import LIVEMODE_MISMATCH
from app.services.payments import GATEWAY_CREATE_FAILED
from app.tasks import payment_events as task

TOPUPS = "/api/v1/customer/topups"
STRIPE_WEBHOOK = "/api/v1/webhooks/payments/stripe"
LIMIT = payments_api.WEBHOOK_MAX_BODY_BYTES

COMPLETED = "checkout.session.completed"
ASYNC_SUCCEEDED = "checkout.session.async_payment_succeeded"

LIVE_SECRET_KEY = "sk_live_payments_api_secret_value"
PLACEHOLDER_SESSION = "00000000-0000-0000-0000-000000000000"
API_LOGGER = "app.api.payments"

# INV-7：建单响应恰好这三个键。
TOPUP_FIELDS = {"payment_id", "redirect_url", "expires_at"}
# 不带时区的 UTC，精确到秒，不带 `Z`。
SECOND_FORMAT = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}")


# --- 应用 -------------------------------------------------------------------------------


@dataclass(frozen=True)
class Account:
    """A tenant with a wallet, and the headers of one of its portal users."""

    tenant: Customer
    headers: dict[str, str]


class Api:
    """An app with payments switched on, over the world's database and fake Stripe."""

    def __init__(
        self, world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **overrides: Any
    ) -> None:
        self.world = world
        self._tmp_path = tmp_path
        self._monkeypatch = monkeypatch
        self.settings = app_settings(tmp_path, **overrides)
        self.app: FastAPI = create_app(self.settings)
        self.app.state.engine = world.factory.kw["bind"]
        self.app.state.session_factory = world.factory
        self.client = TestClient(self.app)
        # 路由与建单都经这个名字取网关；`world.configured = False` 模拟它返回 None。
        monkeypatch.setattr(
            payments_api,
            "get_payment_gateway",
            lambda settings: world.stripe.gateway() if world.configured else None,
        )

    @property
    def factory(self) -> sessionmaker[Session]:
        return self.world.factory

    def with_settings(self, **overrides: Any) -> Api:
        """Another app on the same world. ⚠️ Rebuilding reinstalls the root log handler."""
        return Api(self.world, self._tmp_path, self._monkeypatch, **overrides)

    # -- callers --

    def account(
        self,
        *,
        account_status: AccountStatus = AccountStatus.ENABLED,
        billing_mode: BillingMode = BillingMode.PREPAID,
    ) -> Account:
        with self.factory() as session:
            tenant = tenancy.create_tenant(
                session,
                company_name="Payments Api Test Sdn Bhd",
                email="ops@example.com",
                now=CREATED,
            )
            tenant.account_status = account_status
            tenant.billing_mode = billing_mode
            create_wallet(session, tenant_id=tenant.id, now=CREATED)
            user = User(
                email=f"{uuid.uuid4().hex}{TEST_EMAIL_DOMAIN}",
                password_hash="not-a-real-hash",
                role=UserRole.CUSTOMER,
                status=UserStatus.ACTIVE,
                tenant_id=tenant.id,
                activated_at=CREATED,
                created_at=CREATED,
                updated_at=CREATED,
            )
            session.add(user)
            session.commit()
            customer = Customer(int(tenant.id), tenant.public_id)
            user_id = int(user.id)
        return Account(customer, self.token(user_id, UserRole.CUSTOMER))

    def token(self, user_id: int, role: UserRole) -> dict[str, str]:
        token = issue_access_token(
            self.settings,
            user_id=user_id,
            role=role.value,
            session_id=PLACEHOLDER_SESSION,
            now=utc_now(),
        )
        return {"Authorization": f"Bearer {token}"}

    def topup(self, account: Account, body: Any = None) -> Any:
        payload = {"amount": "100.00"} if body is None else body
        return self.client.post(TOPUPS, json=payload, headers=account.headers)

    def webhook(self, body: bytes, headers: dict[str, str] | None = None) -> Any:
        return self.client.post(
            STRIPE_WEBHOOK, content=body, headers=sign(body) if headers is None else headers
        )

    # -- reading back --

    def events(self) -> list[PaymentGatewayEvent]:
        with self.factory() as session:
            rows = list(
                session.execute(select(PaymentGatewayEvent).order_by(PaymentGatewayEvent.id))
                .scalars()
                .all()
            )
            for row in rows:
                session.expunge(row)
            return rows

    def topups(self, tenant: Customer) -> list[WalletTransaction]:
        statement = select(WalletTransaction).where(
            WalletTransaction.tenant_id == tenant.id,
            WalletTransaction.transaction_type == TransactionType.TOPUP,
        )
        with self.factory() as session:
            rows = list(session.execute(statement).scalars().all())
            for row in rows:
                session.expunge(row)
            return rows

    def balance(self, tenant: Customer) -> Decimal:
        with self.factory() as session:
            wallet = get_wallet_for_tenant(session, tenant.id)
            assert wallet is not None
            return wallet.balance

    def ledger_rows(self) -> int:
        with self.factory() as session:
            return int(
                session.execute(select(func.count()).select_from(WalletTransaction)).scalar_one()
            )


def app_settings(tmp_path: Path, **overrides: Any) -> Settings:
    """Payments on and open; the key files hold the fake Stripe's keys (test mode)."""
    jwt = tmp_path / "jwt.key"
    secret_key = tmp_path / "stripe-secret.key"
    webhook_secret = tmp_path / "stripe-webhook.key"
    jwt.write_text("a-test-signing-key-that-is-long-enough", encoding="utf-8")
    if not secret_key.exists():
        secret_key.write_text(f"{SECRET_KEY}\n", encoding="utf-8")
    webhook_secret.write_text(f"{WEBHOOK_SECRET}\n", encoding="utf-8")
    options: dict[str, Any] = {
        "jwt_secret_file": str(jwt),
        "database_url": "",
        "payments_enabled": True,
        "payment_topups_open": True,
        "frontend_base_url": FRONTEND_BASE_URL,
        "stripe_secret_key_file": str(secret_key),
        "stripe_webhook_secret_file": str(webhook_secret),
        "stripe_api_base_url": API_BASE_URL,
    }
    options.update(overrides)
    return Settings(**options)


def sign(body: bytes, *, secret: str = WEBHOOK_SECRET, at: int | None = None) -> dict[str, str]:
    """A `Stripe-Signature` header over `body`, as Stripe computes it."""
    timestamp = int(time.time()) if at is None else at
    digest = hmac.new(
        secret.encode("utf-8"), str(timestamp).encode("ascii") + b"." + body, hashlib.sha256
    ).hexdigest()
    return {"Stripe-Signature": f"t={timestamp},v1={digest}", "Content-Type": "application/json"}


def error_of(response: Any) -> tuple[int, str]:
    body = response.json()
    assert body["success"] is False and body["data"] is None, body
    return response.status_code, body["error"]["code"]


def wire_task(monkeypatch: pytest.MonkeyPatch, world: World) -> None:
    """Point the event scan at this world's database and fake Stripe, as a worker would see them."""
    monkeypatch.setattr(task, "_session_factory", lambda: world.factory)
    monkeypatch.setattr(task, "get_settings", lambda: Settings(payments_enabled=True))
    monkeypatch.setattr(task, "get_payment_gateway", lambda settings: world.stripe.gateway())


def pay(world: World, payment: Payment, *, fee: int | None = 290) -> None:
    """The customer paid on Stripe's page: Stripe now reports the session as paid."""
    session_id = payment.gateway_payment_id
    assert session_id is not None
    world.stripe.sessions[session_id] = stripe_session(
        GatewayState.PAID,
        session_id=session_id,
        reference=payment.public_id,
        amount_total=amount_minor(payment),
        fee=fee,
    )


def amount_minor(payment: Payment) -> int:
    return int(payment.expected_amount * 100)


def event_for(payment: Payment, event_type: str = COMPLETED, **options: Any) -> bytes:
    return event_body(
        event_type, reference=payment.public_id, session_id=payment.gateway_payment_id, **options
    )


# --- 夹具 -------------------------------------------------------------------------------


def _sqlite_world() -> Iterator[World]:
    # StaticPool：TestClient 在线程池里跑处理函数，内存库必须所有连接共用一个。
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.exec_driver_sql(_SQLITE_LEDGER_TRIGGER)
    try:
        yield _populate(create_session_factory(engine))
    finally:
        engine.dispose()


def _delete_portal_users(factory: sessionmaker[Session]) -> None:
    # 门户账号挂在租户上（RESTRICT）：先删它们，test_payment_create 的清场才删得掉租户。
    with factory() as session:
        session.execute(
            delete(User).where(
                User.role == UserRole.CUSTOMER, User.email.like(f"%{TEST_EMAIL_DOMAIN}")
            )
        )
        session.commit()


@pytest.fixture(params=["sqlite", "mysql"])
def api(request, monkeypatch, tmp_path) -> Iterator[Api]:
    """An untaxed MY / SST version in effect since `TAX_FROM`; payments on and open."""
    source = _mysql_world(monkeypatch) if request.param == "mysql" else _sqlite_world()
    world = next(source)
    try:
        world.publish_tax()
        yield Api(world, tmp_path, monkeypatch)
    finally:
        _delete_portal_users(world.factory)
        for _ in source:
            pass


# --- 开关 -------------------------------------------------------------------------------


def test_payments_off_means_neither_route_exists(api: Api) -> None:
    """设计 §2：`payments_enabled` 关闭时两个路由都不注册，404。"""
    account = api.account()
    off = api.with_settings(payments_enabled=False)

    paths = {getattr(route, "path", None) for route in off.app.routes}
    assert TOPUPS not in paths
    assert "/api/v1/webhooks/payments/{gateway}" not in paths
    topup = off.client.post(TOPUPS, json={"amount": "100.00"}, headers=account.headers)
    body = event_body(reference=str(uuid.uuid4()), session_id="cs_test_off")
    webhook = off.client.post(STRIPE_WEBHOOK, content=body, headers=sign(body))

    assert topup.status_code == webhook.status_code == 404
    assert api.world.payments() == [] and api.events() == []
    assert api.world.stripe.created == []


def test_payments_on_registers_both_routes(api: Api) -> None:
    paths = {getattr(route, "path", None) for route in api.app.routes}
    assert {TOPUPS, "/api/v1/webhooks/payments/{gateway}"} <= paths


# --- 回调：落库 -----------------------------------------------------------------------


def test_a_signed_event_is_recorded_and_not_confirmed_in_the_request(api: Api) -> None:
    """验签通过才落库、立即 200；不在请求里向 Stripe 查询，也不改支付。"""
    account = api.account()
    assert api.topup(account).status_code == 201
    [payment] = api.world.payments()
    pay(api.world, payment)
    body = event_for(payment)

    response = api.webhook(body)

    assert response.status_code == 200, response.text
    envelope = response.json()
    assert envelope["success"] is True and envelope["error"] is None
    assert envelope["data"] == {"received": True}
    [row] = api.events()
    assert row.processing_status is PaymentEventProcessingStatus.RECEIVED
    assert row.payload_fingerprint == hashlib.sha256(body).hexdigest()
    assert row.metadata_json["client_reference_id"] == payment.public_id
    # 白名单：不存原始请求体里的个人数据。
    assert "payer@example.com" not in json.dumps(row.metadata_json)
    assert api.world.stripe.asked == []
    assert api.world.row(payment.public_id).status is PaymentStatus.PENDING
    assert api.topups(account.tenant) == []


def test_an_unknown_gateway_is_not_found(api: Api) -> None:
    body = event_body(reference=str(uuid.uuid4()))

    for gateway in ("paypal", "Stripe", "STRIPE"):
        response = api.client.post(
            f"/api/v1/webhooks/payments/{gateway}", content=body, headers=sign(body)
        )
        assert error_of(response) == (404, "NOT_FOUND")

    assert api.events() == []


def _bad_signatures(body: bytes) -> dict[str, dict[str, str]]:
    now = int(time.time())
    good = sign(body)["Stripe-Signature"]
    return {
        "wrong-secret": sign(body, secret="whsec_not_this_endpoint"),
        "tampered-body": sign(body + b" "),
        "missing-header": {"Content-Type": "application/json"},
        "malformed-header": {"Stripe-Signature": "v1=abc"},
        # 时间窗的精确边界（300 / 301 秒）在适配器的单元测试里；这里留出请求途中走过的秒数。
        "310s-old": sign(body, at=now - 310),
        "310s-ahead": sign(body, at=now + 310),
        "v1-only-wrong": {"Stripe-Signature": good.replace("v1=", "v1=0", 1)},
    }


@pytest.mark.parametrize(
    "case",
    [
        "wrong-secret",
        "tampered-body",
        "missing-header",
        "malformed-header",
        "310s-old",
        "310s-ahead",
        "v1-only-wrong",
    ],
)
def test_a_bad_signature_is_400_and_stores_nothing(
    api: Api, case: str, caplog: pytest.LogCaptureFixture
) -> None:
    marker = str(uuid.uuid4())
    body = event_body(reference=marker, session_id="cs_test_bad_signature")
    headers = _bad_signatures(body)[case]

    with caplog.at_level(logging.INFO, logger=API_LOGGER):
        response = api.webhook(body, headers)

    assert error_of(response) == (400, "WEBHOOK_SIGNATURE_INVALID")
    assert api.events() == []
    warnings = [r for r in caplog.records if r.name == API_LOGGER and r.levelno == logging.WARNING]
    assert len(warnings) == 1
    # 日志不带请求体、签名头原文与密钥（设计 §6）。
    signature = headers.get("Stripe-Signature", "")
    for record in caplog.records:
        text = f"{record.getMessage()} {vars(record)}"
        assert marker not in text and "payer@example.com" not in text
        assert WEBHOOK_SECRET not in text and SECRET_KEY not in text
        if signature:
            assert signature not in text


def test_signatures_within_the_window_are_accepted(api: Api) -> None:
    now = int(time.time())
    for offset in (-290, 290):
        body = event_body(reference=str(uuid.uuid4()))
        assert api.webhook(body, sign(body, at=now + offset)).status_code == 200
    assert len(api.events()) == 2


@pytest.mark.parametrize(
    "payload",
    [
        b"not json",
        b"[]",
        json.dumps({"type": COMPLETED, "livemode": False}).encode(),
        json.dumps({"id": "evt_test_no_type", "livemode": False}).encode(),
        json.dumps({"id": "", "type": COMPLETED, "livemode": False}).encode(),
    ],
    ids=["not-json", "not-an-object", "no-id", "no-type", "empty-id"],
)
def test_an_unusable_payload_is_400_and_stores_nothing(api: Api, payload: bytes) -> None:
    response = api.webhook(payload)

    assert error_of(response) == (400, "WEBHOOK_PAYLOAD_INVALID")
    assert api.events() == []


def test_a_body_over_64_kib_is_413_and_stores_nothing(api: Api) -> None:
    body = event_body(reference=str(uuid.uuid4()))
    oversized = body + b" " * (LIMIT + 1 - len(body))
    assert len(oversized) == LIMIT + 1

    declared = api.webhook(oversized)
    # 不声明长度、分块发送：边读边数。
    chunked = api.client.post(
        STRIPE_WEBHOOK,
        content=iter([oversized[: LIMIT // 2], oversized[LIMIT // 2 :]]),
        headers=sign(oversized),
    )

    assert error_of(declared) == (413, "PAYLOAD_TOO_LARGE")
    assert error_of(chunked) == (413, "PAYLOAD_TOO_LARGE")
    assert api.events() == []


def test_a_body_of_exactly_64_kib_is_accepted(api: Api) -> None:
    body = event_body(reference=str(uuid.uuid4()))
    padded = body + b" " * (LIMIT - len(body))
    assert len(padded) == LIMIT

    assert api.webhook(padded).status_code == 200
    [row] = api.events()
    assert row.payload_fingerprint == hashlib.sha256(padded).hexdigest()


def test_a_livemode_mismatch_is_stored_as_ignored_with_200(api: Api) -> None:
    body = event_body(reference=str(uuid.uuid4()), livemode=True)

    assert api.webhook(body).status_code == 200

    [row] = api.events()
    assert row.processing_status is PaymentEventProcessingStatus.IGNORED
    assert row.last_error_code == LIVEMODE_MISMATCH


def test_livemode_comes_from_the_configured_secret_key(api: Api) -> None:
    """`livemode` 取 `read_stripe_credentials`。

    密钥文件是 `sk_live_` 时，live 事件照常 RECEIVED。
    """
    (Path(api.settings.stripe_secret_key_file)).write_text(LIVE_SECRET_KEY, encoding="utf-8")
    live = event_body(reference=str(uuid.uuid4()), livemode=True)
    test_mode = event_body(reference=str(uuid.uuid4()), livemode=False)

    assert api.webhook(live).status_code == 200
    assert api.webhook(test_mode).status_code == 200

    first, second = api.events()
    assert (first.processing_status, first.last_error_code) == (
        PaymentEventProcessingStatus.RECEIVED,
        None,
    )
    assert (second.processing_status, second.last_error_code) == (
        PaymentEventProcessingStatus.IGNORED,
        LIVEMODE_MISMATCH,
    )


def test_redeliveries_are_200_and_a_different_body_is_a_conflict(api: Api) -> None:
    """重复相同 → 200、一行；同一事件 id 内容不同 → 200、记 `fingerprint_conflict_at`、原行不变。"""
    event_id = f"evt_test_{uuid.uuid4().hex}"
    original = event_body(event_id=event_id, reference=str(uuid.uuid4()))
    tampered = event_body(event_id=event_id, reference=str(uuid.uuid4()), amount_total=1)

    assert api.webhook(original).status_code == 200
    [before] = api.events()
    assert api.webhook(original).status_code == 200
    [again] = api.events()
    assert again.fingerprint_conflict_at is None
    conflict = api.webhook(tampered)

    assert conflict.status_code == 200
    assert conflict.json()["data"] == {"received": True}
    [after] = api.events()
    assert after.fingerprint_conflict_at is not None
    assert after.payload_fingerprint == before.payload_fingerprint
    assert after.metadata_json == before.metadata_json
    assert after.processing_status is before.processing_status


def test_unreadable_credentials_are_503_and_store_nothing(api: Api) -> None:
    body = event_body(reference=str(uuid.uuid4()))
    missing = api.with_settings(stripe_webhook_secret_file=str(Path("missing") / "whsec.key"))

    assert error_of(missing.webhook(body)) == (503, "PAYMENTS_MISCONFIGURED")
    no_base_url = api.with_settings(stripe_api_base_url="")
    assert error_of(no_base_url.webhook(body)) == (503, "PAYMENTS_MISCONFIGURED")
    api.world.configured = False
    assert error_of(api.webhook(body)) == (503, "PAYMENTS_MISCONFIGURED")
    assert api.events() == []


def test_an_unavailable_database_is_503(api: Api, monkeypatch: pytest.MonkeyPatch) -> None:
    body = event_body(reference=str(uuid.uuid4()))

    def database_down(*_args: Any, **_kwargs: Any) -> None:
        raise OperationalError("INSERT", {}, Exception("database is down"))

    monkeypatch.setattr(payments_api, "record_event", database_down)
    assert error_of(api.webhook(body)) == (503, "SERVICE_UNAVAILABLE")

    api.app.state.session_factory = None
    assert error_of(api.webhook(body)) == (503, "SERVICE_UNAVAILABLE")


# --- 伪造成功跳转 ---------------------------------------------------------------------


def test_a_forged_success_redirect_changes_nothing(
    api: Api, monkeypatch: pytest.MonkeyPatch
) -> None:
    """只访问跳回地址、不发回调：支付仍 PENDING、无账本行（spec §43）。

    跳回地址是门户的页面，后端没有这个路由；Stripe 那边即使已经报「已付」，
    没有回调就没有事件可处理。
    """
    account = api.account()
    response = api.topup(account)
    [payment] = api.world.payments()
    [created] = api.world.stripe.created
    pay(api.world, payment)
    success_url = created.form["success_url"]
    assert success_url.endswith(f"/portal/payments/{payment.public_id}?result=success")

    forged = api.client.get(success_url.removeprefix(FRONTEND_BASE_URL))
    wire_task(monkeypatch, api.world)
    handled = task.process_due()

    assert response.status_code == 201
    assert forged.status_code == 404
    assert handled == 0
    assert api.world.row(payment.public_id).status is PaymentStatus.PENDING
    assert api.ledger_rows() == 0
    assert api.balance(account.tenant) == 0


# --- 端到端 -----------------------------------------------------------------------------


def test_topup_webhook_scan_another_event_and_a_redelivery_credit_once(
    api: Api, monkeypatch: pytest.MonkeyPatch
) -> None:
    """设计 §7「端到端」：钱包只 +金额一次；支付 PAID；`gateway_fee` 写入。"""
    wire_task(monkeypatch, api.world)
    account = api.account()
    other = api.account()

    created = api.topup(account, {"amount": "100.00"})
    assert created.status_code == 201, created.text
    payment = api.world.row(created.json()["data"]["payment_id"])
    assert payment.tenant_id == account.tenant.id
    assert payment.status is PaymentStatus.PENDING

    # 客户在 Stripe 付款页付款；Stripe 发回调。
    pay(api.world, payment, fee=290)
    first = event_for(payment, COMPLETED)
    assert api.webhook(first).status_code == 200
    assert task.process_due() == 1

    paid = api.world.row(payment.public_id)
    assert paid.status is PaymentStatus.PAID and paid.status_reason is None
    assert paid.gateway_fee == Decimal("2.90")
    assert paid.confirmed_amount == Decimal("100.00") and paid.confirmed_currency == "MYR"
    assert paid.paid_at is not None
    [topup] = api.topups(account.tenant)
    assert topup.amount == Decimal("100.00")
    assert topup.reference_id == payment.public_id
    assert api.balance(account.tenant) == Decimal("100.00")

    # 同一笔支付的另一个事件（不同 id）。
    second = event_for(payment, ASYNC_SUCCEEDED)
    assert api.webhook(second).status_code == 200
    assert task.process_due() == 1

    # 第一个事件的重复投递：200、不落第二行、没有可处理的事件。
    assert api.webhook(first).status_code == 200
    assert task.process_due() == 0

    events = api.events()
    assert len(events) == 2
    assert {row.processing_status for row in events} == {PaymentEventProcessingStatus.PROCESSED}
    assert {row.payment_id for row in events} == {paid.id}
    assert len(api.topups(account.tenant)) == 1
    assert api.balance(account.tenant) == Decimal("100.00")
    assert api.world.row(payment.public_id).status is PaymentStatus.PAID
    # 别的租户的钱包不动（INV-8）。
    assert api.topups(other.tenant) == []
    assert api.balance(other.tenant) == 0


# --- 建单：成功、字段与复用 ---------------------------------------------------------------


def test_a_topup_returns_exactly_the_three_customer_fields(api: Api) -> None:
    """设计 §7「客户接口字段」（INV-7）：只有 `payment_id`、`redirect_url`、`expires_at`。"""
    account = api.account()

    response = api.topup(account, {"amount": "100.00"})

    assert response.status_code == 201, response.text
    envelope = response.json()
    assert envelope["success"] is True and envelope["error"] is None
    data = envelope["data"]
    assert set(data) == TOPUP_FIELDS
    [row] = api.world.payments(account.tenant)
    assert data["payment_id"] == row.public_id
    assert data["redirect_url"] == f"{CHECKOUT_BASE_URL}/{row.gateway_payment_id}"
    assert SECOND_FORMAT.fullmatch(data["expires_at"])
    assert data["expires_at"] == row.expires_at.isoformat()
    assert row.expected_amount == Decimal("100.00")
    # 手续费、状态原因、税务版本与内部 id 都不出现。
    text = response.text
    for absent in ("gateway_fee", "status_reason", "tenant_id", "tax_policy", "status"):
        assert absent not in text


def test_the_same_amount_within_60_seconds_reuses_the_payment(api: Api) -> None:
    account = api.account()

    first = api.topup(account)
    second = api.topup(account)

    assert first.status_code == second.status_code == 201
    assert first.json()["data"] == second.json()["data"]
    assert len(api.world.payments()) == 1
    assert len(api.world.stripe.created) == 1


def test_a_stripe_failure_is_503_and_the_retry_reuses_the_idempotency_key(api: Api) -> None:
    account = api.account()
    api.world.stripe.create_failures.append(stripe_error(503))

    failed = api.topup(account)

    assert error_of(failed) == (503, "PAYMENT_GATEWAY_UNAVAILABLE")
    [row] = api.world.payments()
    assert (row.status, row.status_reason) == (PaymentStatus.PENDING, GATEWAY_CREATE_FAILED)

    retried = api.topup(account)

    assert retried.status_code == 201, retried.text
    assert retried.json()["data"]["payment_id"] == row.public_id
    assert [c.idempotency_key for c in api.world.stripe.created] == [f"topup-{row.public_id}"] * 2
    [after] = api.world.payments()
    assert (after.status_reason, after.gateway_payment_id is not None) == (None, True)


def test_an_allowlisted_tenant_may_top_up_while_topups_are_not_open(api: Api) -> None:
    listed = api.account()
    outsider = api.account()
    closed = api.with_settings(
        payment_topups_open=False, payment_topup_tenant_allowlist=listed.tenant.public_id
    )

    refused = closed.topup(outsider)
    accepted = closed.topup(listed)

    assert error_of(refused) == (403, "TOPUPS_NOT_OPEN")
    assert accepted.status_code == 201, accepted.text
    assert api.world.payments(outsider.tenant) == []
    assert len(api.world.payments(listed.tenant)) == 1


# --- 建单：鉴权、请求体与校验顺序 ---------------------------------------------------------


@pytest.mark.parametrize("status", [AccountStatus.DISABLED, AccountStatus.CLOSED])
def test_a_read_only_account_is_403_before_the_body_is_looked_at(
    api: Api, status: AccountStatus
) -> None:
    account = api.account(account_status=status)

    plain = api.topup(account)
    with_extra = api.topup(account, {"amount": "100.00", "tenant_id": "x"})
    not_json = api.client.post(TOPUPS, content=b"not json", headers=account.headers)

    for response in (plain, with_extra, not_json):
        assert error_of(response) == (403, "ACCOUNT_READ_ONLY")
    assert api.world.payments() == []


def test_callers_are_authenticated_before_the_body_is_looked_at(api: Api) -> None:
    body = {"amount": "100.00", "tenant_id": "x"}

    anonymous = api.client.post(TOPUPS, json=body)
    admin = api.client.post(
        TOPUPS, json=body, headers=api.token(api.world.admin_id, UserRole.ADMIN)
    )

    assert error_of(anonymous) == (401, "TOKEN_INVALID")
    assert error_of(admin) == (403, "CUSTOMER_REQUIRED")
    assert api.world.payments() == []


def test_a_body_naming_another_tenant_is_refused_and_creates_nothing(api: Api) -> None:
    """设计 §7「跨租户」：请求体带别的租户字段被 422 拒绝，A、B 都不建单。"""
    mine = api.account()
    theirs = api.account()

    responses = [
        api.topup(mine, {"amount": "100.00", field: value})
        for field, value in (
            ("tenant_id", theirs.tenant.id),
            ("customer_id", theirs.tenant.public_id),
            ("payment_id", str(uuid.uuid4())),
        )
    ]

    for response in responses:
        assert error_of(response) == (422, "VALIDATION_ERROR")
        assert theirs.tenant.public_id not in response.text
    assert api.world.payments() == []
    assert api.world.stripe.created == []


def test_a_customer_tops_up_only_its_own_tenant(api: Api) -> None:
    """租户只来自令牌主体：A 与 B 同额建单，各得各的一笔，不互相复用。"""
    first = api.account()
    second = api.account()

    a = api.topup(first)
    b = api.topup(second)

    assert a.status_code == b.status_code == 201
    assert a.json()["data"]["payment_id"] != b.json()["data"]["payment_id"]
    [mine] = api.world.payments(first.tenant)
    [theirs] = api.world.payments(second.tenant)
    assert (mine.public_id, theirs.public_id) == (
        a.json()["data"]["payment_id"],
        b.json()["data"]["payment_id"],
    )


def test_unknown_fields_are_refused_before_any_ordered_check(api: Api) -> None:
    """多余字段的 422 先于 `create_topup` 的全部有序校验：未开放、内部租户、金额都排在它后面。"""
    outsider = api.account(billing_mode=BillingMode.INTERNAL_METERED_ONLY)
    closed = api.with_settings(payment_topups_open=False)

    response = closed.topup(outsider, {"amount": "abc", "note": "x"})

    assert error_of(response) == (422, "VALIDATION_ERROR")
    assert "note" in response.json()["error"]["message"]
    assert "abc" not in response.json()["error"]["message"]


@pytest.mark.parametrize(
    "raw",
    [b"not json", b"", b"[]", b'"100.00"'],
    ids=["not-json", "empty", "array", "string"],
)
def test_a_body_that_is_not_an_object_is_a_validation_error(api: Api, raw: bytes) -> None:
    account = api.account()

    response = api.client.post(
        TOPUPS,
        content=raw,
        headers={**account.headers, "Content-Type": "application/json"},
    )

    assert error_of(response) == (422, "VALIDATION_ERROR")
    assert api.world.payments() == []


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"amount": None},
        {"amount": 100},
        {"amount": 100.0},
        {"amount": True},
        {"amount": "9.99"},
    ],
    ids=["missing", "null", "integer", "float", "boolean", "below-minimum"],
)
def test_a_missing_or_non_string_amount_is_topup_amount_invalid(
    api: Api, body: dict[str, Any]
) -> None:
    account = api.account()

    response = api.topup(account, body)

    assert error_of(response) == (422, "TOPUP_AMOUNT_INVALID")
    assert api.world.payments() == []


def test_the_amount_is_checked_after_the_billing_mode(api: Api) -> None:
    """缺 `amount` 也在 `create_topup` 的顺序里判：内部租户先得到 403。"""
    account = api.account(billing_mode=BillingMode.INTERNAL_METERED_ONLY)

    assert error_of(api.topup(account, {})) == (403, "TOPUP_NOT_ALLOWED")
    assert error_of(api.topup(account)) == (403, "TOPUP_NOT_ALLOWED")
    assert api.world.payments() == []


def test_no_tax_policy_in_effect_is_503(api: Api) -> None:
    account = api.account()
    version = api.world._any_tax_version()
    api.world.end_tax(version, TAX_FROM + dt.timedelta(days=1))

    assert error_of(api.topup(account)) == (503, "TAX_POLICY_UNAVAILABLE")
    assert api.world.payments() == []


def test_misconfigured_payments_are_503(api: Api) -> None:
    account = api.account()

    no_frontend = api.with_settings(frontend_base_url="")
    assert error_of(no_frontend.topup(account)) == (503, "PAYMENTS_MISCONFIGURED")
    api.world.configured = False
    assert error_of(api.topup(account)) == (503, "PAYMENTS_MISCONFIGURED")
    assert api.world.payments() == []

"""Customer top-up creation (design gate #580 v3, AIH-TASK-176).

设计 §7 标 176 的各行与「建单校验」的服务部分：

- **建单校验**：配置三项；校验顺序与错误（账户可写 → 开放 → 计费模式 → 金额
  → 税务政策）；锁内重查；建单未开放时名单内外两组；建单记税务版本；
- **60 秒复用**、剩余有效期不足 31 分钟不复用、**待付超时与 Stripe 区间**；
- **Stripe 失败后用同一幂等键重试复用**、**并发复用同一幂等键**
  （`idempotency_key_in_use`）、并发者先写了网关 id（相同 / 不同）；
- **复用排除**：候选会话已不是 `OPEN`、查询被拒、查询不可用；锁内找到并发产生的、
  已有网关 id 的一笔；两个并发请求只新建一笔（真 MySQL）。

以上除最后一条外都用 `payment_world` 夹具在 SQLite 与真 MySQL 上各跑一次。「并发」的
那几组在 SQLite 上用确定的写法造出：假 Stripe 在收到建单请求时先跑另一个请求（此时
第一个请求不持任何锁，设计 §2「调 `create_payment`（在事务外，不持任何锁）」），或在
锁租户之前先提交一笔「并发者」的支付。

Stripe 一律是注入了假传输（`httpx2.MockTransport`）的 `StripeGateway`，不联网；
地址是虚构的。

MySQL 那部分需要 `BILLING_TEST_DATABASE_URL` 指向一个**可以被清空的**库，没设时 skip ——
**不要把 skipped 读成 passed**，CI 设了它并把 skipped 判成失败。
"""

from __future__ import annotations

import calendar
import dataclasses
import datetime as dt
import logging
import os
import threading
import uuid
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any
from urllib.parse import parse_qs

import httpx2
import pytest
from alembic.config import Config
from sqlalchemy import Engine, create_engine, delete, select, text, update
from sqlalchemy.orm import Session, sessionmaker

from alembic import command
from app.core.config import Settings
from app.core.database import create_session_factory
from app.models.auth import DomainOutbox, User, UserRole, UserStatus
from app.models.base import Base
from app.models.payments import Payment, PaymentGateway, PaymentGatewayEvent, PaymentStatus
from app.models.tax_policy import TaxJurisdiction, TaxPolicyStatus, TaxPolicyType, TaxPolicyVersion
from app.models.tenancy import AccountStatus, BillingMode, Project, Tenant
from app.models.wallet import Wallet
from app.payments.stripe_gateway import StripeGateway
from app.repositories import tenancy
from app.services import payments as payments_service
from app.services.account_status import AccountReadOnly
from app.services.customers import CustomerNotFound
from app.services.payments import (
    GATEWAY_CREATE_FAILED,
    PaymentGatewayUnavailable,
    PaymentsMisconfigured,
    TaxPolicyUnavailable,
    TaxPolicyUnsupported,
    TopupAmountInvalid,
    TopupNotAllowed,
    TopupResult,
    TopupsNotOpen,
    create_topup,
)

TEST_EMAIL_DOMAIN = "@payment-create-test.example.com"
# 虚构的地址：这些用例不会连到任何主机。
API_BASE_URL = "https://stripe-api.example.test"
CHECKOUT_BASE_URL = "https://checkout.example.test/c"
FRONTEND_BASE_URL = "https://portal.example.test"
SECRET_KEY = "sk_test_payment_create_secret_value"
WEBHOOK_SECRET = "whsec_payment_create_signing_value"

TAX_FROM = dt.datetime(2026, 1, 1, 0, 0, 0)
CREATED = dt.datetime(2026, 11, 8, 7, 0, 0)
NOW = dt.datetime(2026, 11, 8, 8, 0, 0)
SECOND = dt.timedelta(seconds=1)
MINUTE = dt.timedelta(minutes=1)

LOGGER = "app.services.payments"
NOTICE = {"zh": "本公司未注册销售及服务税。", "en": "We are not registered for SST."}


def _unix(moment: dt.datetime) -> int:
    return calendar.timegm(moment.utctimetuple())


# --- 假 Stripe -----------------------------------------------------------------------


@dataclass(frozen=True)
class Created:
    """One `POST /v1/checkout/sessions` as Stripe received it."""

    idempotency_key: str
    form: dict[str, str]


class FakeStripe:
    """Creates Checkout Sessions (idempotent by `Idempotency-Key`, as Stripe is) and answers
    `GET /v1/checkout/sessions/{id}` from `sessions`; records everything it was asked."""

    def __init__(self) -> None:
        self.sessions: dict[str, dict[str, Any]] = {}
        self.created: list[Created] = []
        self.asked: list[str] = []
        # 按到达顺序消耗：每个建单请求取走一个失败响应，取完了就成功。
        self.create_failures: list[httpx2.Response] = []
        # 状态查询的 HTTP 状态；不是 200 时一律回它。
        self.status_code = 200
        # 每次建单都给新的会话 id（模拟「同一幂等键却得到两个会话」这种不该出现的情形）。
        self.distinct_sessions = False
        # 建单请求到达时、回答之前跑一次（另一个并发请求就在这里发生）。
        self.on_create: Callable[[], None] | None = None
        self._by_key: dict[str, str] = {}
        self._lock = threading.Lock()

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        if request.method == "POST":
            return self._create(request)
        session_id = request.url.path.rsplit("/", 1)[-1]
        with self._lock:
            self.asked.append(session_id)
        if self.status_code != 200:
            return httpx2.Response(self.status_code, json={})
        body = self.sessions.get(session_id)
        if body is None:
            return httpx2.Response(404, json={"error": {"code": "resource_missing"}})
        return httpx2.Response(200, json=body)

    def _create(self, request: httpx2.Request) -> httpx2.Response:
        form = {key: values[0] for key, values in parse_qs(request.content.decode()).items()}
        key = request.headers["Idempotency-Key"]
        with self._lock:
            self.created.append(Created(key, form))
            hook, self.on_create = self.on_create, None
        if hook is not None:
            hook()
        with self._lock:
            if self.create_failures:
                return self.create_failures.pop(0)
            if self.distinct_sessions:
                session_id = f"cs_test_{uuid.uuid4().hex}"
            else:
                session_id = self._by_key.setdefault(key, f"cs_test_{uuid.uuid4().hex}")
            body = self.sessions.setdefault(
                session_id,
                {
                    "id": session_id,
                    "status": "open",
                    "payment_status": "unpaid",
                    "url": f"{CHECKOUT_BASE_URL}/{session_id}",
                    "client_reference_id": form["client_reference_id"],
                    "amount_total": int(form["line_items[0][price_data][unit_amount]"]),
                    "currency": "myr",
                    "payment_intent": None,
                },
            )
        return httpx2.Response(200, json=body)

    def open(self, session_id: str) -> None:
        self.sessions[session_id] = {
            "id": session_id,
            "status": "open",
            "payment_status": "unpaid",
            "url": f"{CHECKOUT_BASE_URL}/{session_id}",
            "client_reference_id": None,
            "amount_total": 10000,
            "currency": "myr",
            "payment_intent": None,
        }

    def expire(self, session_id: str) -> None:
        self.sessions[session_id].update(status="expired", url=None)

    def gateway(self) -> StripeGateway:
        return StripeGateway(
            SECRET_KEY,
            WEBHOOK_SECRET,
            timeout_seconds=5,
            webhook_tolerance_seconds=300,
            transport=httpx2.MockTransport(self.handle),
            api_base_url=API_BASE_URL,
        )


def stripe_error(status_code: int, code: str | None = None) -> httpx2.Response:
    body: dict[str, Any] = {"error": {"code": code}} if code is not None else {}
    return httpx2.Response(status_code, json=body)


# --- 世界 -----------------------------------------------------------------------------


@dataclass(frozen=True)
class Customer:
    id: int
    public_id: str


@dataclass
class World:
    factory: sessionmaker[Session]
    admin_id: int
    stripe: FakeStripe = field(default_factory=FakeStripe)
    # 每次调用都回同一个假 Stripe；`configured = False` 模拟凭据读不到。
    configured: bool = True

    def customer(
        self,
        *,
        account_status: AccountStatus = AccountStatus.ENABLED,
        billing_mode: BillingMode = BillingMode.PREPAID,
    ) -> Customer:
        with self.factory() as session:
            tenant = tenancy.create_tenant(
                session,
                company_name="Payment Create Test Sdn Bhd",
                email="ops@example.com",
                now=CREATED,
            )
            tenant.account_status = account_status
            tenant.billing_mode = billing_mode
            session.commit()
            return Customer(int(tenant.id), tenant.public_id)

    def set_tenant(self, tenant: Customer, **values: Any) -> None:
        with self.factory() as session:
            session.execute(update(Tenant).where(Tenant.id == tenant.id).values(**values))
            session.commit()

    # -- tax policies (the writes of test_tax_policy_db.py, through the ORM) --

    def publish_tax(
        self,
        start: dt.datetime = TAX_FROM,
        *,
        treatment: dict[str, Any] | None = None,
        rate: Decimal | None = None,
    ) -> int:
        """A MY / SST version inserted as a draft, then published from `start` without an end."""
        with self.factory() as session:
            row = TaxPolicyVersion(
                public_id=str(uuid.uuid4()),
                jurisdiction=TaxJurisdiction.MY,
                policy_type=TaxPolicyType.SST,
                rate=rate,
                treatment_json=treatment
                if treatment is not None
                else {
                    "schema_version": 1,
                    "sst_registered": False,
                    "tax_charged": False,
                    "document_notice": dict(NOTICE),
                },
                status=TaxPolicyStatus.DRAFT,
                effective_from=None,
                effective_to=None,
                source_reference="Fictional policy for a top-up test",
                created_by=self.admin_id,
                approved_by=None,
                approved_at=None,
                created_at=CREATED,
                updated_at=CREATED,
            )
            session.add(row)
            session.flush()
            row.status = TaxPolicyStatus.PUBLISHED
            row.effective_from = start
            row.approved_by = self.admin_id
            row.approved_at = CREATED
            session.commit()
            return int(row.id)

    def end_tax(self, version: int, end: dt.datetime) -> None:
        with self.factory() as session:
            session.execute(
                update(TaxPolicyVersion)
                .where(TaxPolicyVersion.id == version)
                .values(effective_to=end, updated_at=CREATED)
            )
            session.commit()

    # -- top-ups --

    def settings(self, **overrides: Any) -> Settings:
        options: dict[str, Any] = {
            "payments_enabled": True,
            "payment_topups_open": True,
            "frontend_base_url": FRONTEND_BASE_URL,
        }
        options.update(overrides)
        return Settings(**options)

    def gateway_factory(self, settings: Settings) -> StripeGateway | None:
        return self.stripe.gateway() if self.configured else None

    def topup(
        self,
        tenant: Customer,
        amount: object = "100.00",
        *,
        now: dt.datetime = NOW,
        settings: Settings | None = None,
    ) -> TopupResult:
        return create_topup(
            self.factory,
            self.gateway_factory,
            tenant.id,
            amount,
            now=now,
            settings=settings or self.settings(),
        )

    def pending(
        self,
        tenant: Customer,
        *,
        amount: str = "100.00",
        created_at: dt.datetime = NOW,
        expires_at: dt.datetime | None = None,
        session_id: str | None = None,
    ) -> Payment:
        """A `PENDING` payment another request left behind, inserted directly."""
        tax_version = self._any_tax_version()
        with self.factory() as session:
            row = Payment(
                public_id=str(uuid.uuid4()),
                tenant_id=tenant.id,
                gateway=PaymentGateway.STRIPE,
                expected_amount=Decimal(amount),
                expected_currency="MYR",
                wallet_credit_amount=Decimal(amount),
                tax_policy_version_id=tax_version,
                tax_amount=None,
                gateway_payment_id=session_id,
                status=PaymentStatus.PENDING,
                status_reason=None,
                expires_at=expires_at or created_at + dt.timedelta(hours=1),
                created_at=created_at,
                updated_at=created_at,
            )
            session.add(row)
            session.commit()
            session.expunge(row)
            return row

    def _any_tax_version(self) -> int:
        with self.factory() as session:
            return int(session.execute(select(TaxPolicyVersion.id).limit(1)).scalar_one())

    # -- reading back --

    def payments(self, tenant: Customer | None = None) -> list[Payment]:
        statement = select(Payment).order_by(Payment.id)
        if tenant is not None:
            statement = statement.where(Payment.tenant_id == tenant.id)
        with self.factory() as session:
            rows = list(session.execute(statement).scalars())
            for row in rows:
                session.expunge(row)
            return rows

    def row(self, public_id: str) -> Payment:
        with self.factory() as session:
            row = session.execute(
                select(Payment).where(Payment.public_id == public_id)
            ).scalar_one()
            session.expunge(row)
            return row


def _key(public_id: str) -> str:
    return f"topup-{public_id}"


# --- 夹具 -----------------------------------------------------------------------------


@pytest.fixture(params=["sqlite", "mysql"])
def payment_world(request, monkeypatch) -> Iterator[World]:
    if request.param == "mysql":
        yield from _mysql_world(monkeypatch)
        return
    yield from _sqlite_world()


@pytest.fixture
def mysql_payment_world(monkeypatch) -> Iterator[World]:
    yield from _mysql_world(monkeypatch)


def _sqlite_world() -> Iterator[World]:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    try:
        yield _populate(create_session_factory(engine))
    finally:
        engine.dispose()


def _mysql_url() -> str:
    # 环境变量在夹具里读：xdist 换库名（conftest 的 pytest_configure）之后才有最终值。
    url = os.environ.get("BILLING_TEST_DATABASE_URL", "")
    if not url:
        pytest.skip("BILLING_TEST_DATABASE_URL is not set; the MySQL half needs a real MySQL")
    return url


def _mysql_world(monkeypatch) -> Iterator[World]:
    url = _mysql_url()
    # ⚠️ 建表必须走 alembic：税务政策的触发器只有迁移会建。
    from app.core.config import get_settings

    monkeypatch.setenv("BILLING_DATABASE_URL", url)
    get_settings.cache_clear()
    command.upgrade(Config("alembic.ini"), "head")
    get_settings.cache_clear()

    engine = create_engine(url, pool_size=10, max_overflow=4)
    _clean(engine)
    try:
        yield _populate(create_session_factory(engine))
    finally:
        _clean(engine)
        engine.dispose()


def _clean(engine: Engine) -> None:
    """⚠️ 库是共享的，每个用例前后都清场。

    税务政策版本拒绝 DELETE（触发器），只能在关掉外键检查后 TRUNCATE。
    支付与事件行在用例后删掉：告警用例与本文件共用这个库（设计 §2「告警」）。
    """
    with engine.begin() as connection:
        connection.execute(text("SET FOREIGN_KEY_CHECKS = 0"))
        try:
            connection.execute(text("TRUNCATE TABLE wallet_transactions"))
            connection.execute(text("TRUNCATE TABLE audit_logs"))
            connection.execute(text("TRUNCATE TABLE tax_policy_versions"))
        finally:
            connection.execute(text("SET FOREIGN_KEY_CHECKS = 1"))
    with engine.begin() as connection:
        connection.execute(delete(PaymentGatewayEvent))
        connection.execute(delete(Payment))
        connection.execute(delete(Wallet))
        connection.execute(delete(Project))
        connection.execute(delete(Tenant))
        connection.execute(delete(DomainOutbox).where(DomainOutbox.aggregate_type == "tenant"))
        connection.execute(delete(User).where(User.email.like(f"%{TEST_EMAIL_DOMAIN}")))


def _populate(factory: sessionmaker[Session]) -> World:
    """An admin: tax policy versions need a creator. No version is published here."""
    with factory() as session:
        admin = User(
            email=f"{uuid.uuid4().hex}{TEST_EMAIL_DOMAIN}",
            password_hash="not-a-real-hash",
            role=UserRole.ADMIN,
            status=UserStatus.ACTIVE,
            created_at=CREATED,
            updated_at=CREATED,
        )
        session.add(admin)
        session.commit()
        return World(factory=factory, admin_id=int(admin.id))


@pytest.fixture
def world(payment_world: World) -> World:
    """`payment_world` with an untaxed MY / SST version in effect since `TAX_FROM`."""
    payment_world.publish_tax()
    return payment_world


def _assert_nothing_happened(world: World) -> None:
    assert world.payments() == []
    assert world.stripe.created == []
    assert world.stripe.asked == []


def _lock_hook(monkeypatch, before_lock: Callable[[int], None]) -> list[int]:
    """Run `before_lock(pass number)` just before each `lock_tenant_for_customer_write`.

    第一条语句之前跑：此时建单的写事务还没开始，`before_lock` 用自己的会话提交的
    东西，锁内一定读得到。
    """
    real = payments_service.lock_tenant_for_customer_write
    calls: list[int] = []

    def locking(session: Session, tenant_id: int) -> Tenant:
        calls.append(len(calls) + 1)
        before_lock(calls[-1])
        return real(session, tenant_id)

    monkeypatch.setattr(payments_service, "lock_tenant_for_customer_write", locking)
    return calls


# --- 新建：写入的行、发给 Stripe 的请求、返回值 -----------------------------------------


def test_a_new_topup_is_a_pending_payment_with_a_stripe_payment_page(world: World) -> None:
    tenant = world.customer()
    version = world._any_tax_version()

    result = world.topup(tenant, "100.00")

    [row] = world.payments()
    assert result == TopupResult(
        payment_public_id=row.public_id,
        redirect_url=f"{CHECKOUT_BASE_URL}/{row.gateway_payment_id}",
        expires_at=NOW + dt.timedelta(seconds=3600),
    )
    # INV-7：客户只拿到这三项。
    assert [f.name for f in dataclasses.fields(TopupResult)] == [
        "payment_public_id",
        "redirect_url",
        "expires_at",
    ]
    uuid.UUID(row.public_id)
    assert row.tenant_id == tenant.id
    assert row.gateway is PaymentGateway.STRIPE
    assert (row.status, row.status_reason) == (PaymentStatus.PENDING, None)
    assert row.expected_amount == row.wallet_credit_amount == Decimal("100.00")
    assert row.expected_currency == "MYR"
    assert row.tax_policy_version_id == version
    assert row.tax_amount is None
    assert row.gateway_payment_id is not None and row.gateway_payment_id.startswith("cs_test_")
    assert row.gateway_payment_intent_id is None
    assert (row.confirmed_amount, row.confirmed_currency, row.gateway_fee) == (None, None, None)
    assert (row.paid_at, row.review_required_at, row.last_reconciled_at) == (None, None, None)
    assert row.expires_at == NOW + dt.timedelta(seconds=3600)
    assert (row.created_at, row.updated_at) == (NOW, NOW)

    [created] = world.stripe.created
    assert created.idempotency_key == _key(row.public_id)
    form = created.form
    assert form["client_reference_id"] == form["metadata[payment_id]"] == row.public_id
    assert form["line_items[0][price_data][unit_amount]"] == "10000"
    assert form["expires_at"] == str(_unix(row.expires_at))
    assert (
        form["success_url"] == f"{FRONTEND_BASE_URL}/portal/payments/{row.public_id}?result=success"
    )
    assert (
        form["cancel_url"]
        == f"{FRONTEND_BASE_URL}/portal/payments/{row.public_id}?result=cancelled"
    )
    # 新建不查任何会话。
    assert world.stripe.asked == []


def test_return_urls_do_not_double_the_slash_of_the_frontend_base_url(world: World) -> None:
    tenant = world.customer()

    world.topup(tenant, settings=world.settings(frontend_base_url=f"{FRONTEND_BASE_URL}/"))

    [row] = world.payments()
    [created] = world.stripe.created
    assert created.form["success_url"] == (
        f"{FRONTEND_BASE_URL}/portal/payments/{row.public_id}?result=success"
    )


def test_a_pending_activation_tenant_may_top_up(world: World) -> None:
    tenant = world.customer(account_status=AccountStatus.PENDING_ACTIVATION)

    world.topup(tenant)

    assert len(world.payments(tenant)) == 1


def test_the_topup_records_the_tax_policy_version_in_effect(payment_world: World) -> None:
    """设计 §7「建单记税务版本」（INV-6）：取建单时刻生效的版本。"""
    world = payment_world
    old = world.publish_tax(TAX_FROM)
    switch = NOW - dt.timedelta(hours=1)
    world.end_tax(old, switch)
    current = world.publish_tax(switch)
    tenant = world.customer()

    world.topup(tenant)

    [row] = world.payments()
    assert row.tax_policy_version_id == current != old


# --- 配置 -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("overrides", "configured"),
    [
        ({"payments_enabled": False}, True),
        ({"frontend_base_url": ""}, True),
        ({"frontend_base_url": "   "}, True),
        ({}, False),
    ],
    ids=["switch-off", "no-frontend", "blank-frontend", "no-gateway"],
)
def test_misconfigured_payments_refuse_before_anything_else(
    world: World, overrides: dict[str, Any], configured: bool
) -> None:
    # 账户只读、金额不合法也一样：配置在一切校验之前。
    tenant = world.customer(account_status=AccountStatus.DISABLED)
    world.configured = configured

    with pytest.raises(PaymentsMisconfigured) as raised:
        world.topup(tenant, "abc", settings=world.settings(**overrides))

    assert (raised.value.code, raised.value.http_status) == ("PAYMENTS_MISCONFIGURED", 503)
    _assert_nothing_happened(world)


# --- 校验与顺序 --------------------------------------------------------------------------


@pytest.mark.parametrize("status", [AccountStatus.DISABLED, AccountStatus.CLOSED])
def test_a_read_only_account_is_refused_first(world: World, status: AccountStatus) -> None:
    """校验 1 在最前：未开放、内部租户、金额不合法都排在它后面。"""
    tenant = world.customer(account_status=status, billing_mode=BillingMode.INTERNAL_METERED_ONLY)

    with pytest.raises(AccountReadOnly) as raised:
        world.topup(tenant, "abc", settings=world.settings(payment_topups_open=False))

    assert (raised.value.code, raised.value.http_status) == ("ACCOUNT_READ_ONLY", 403)
    _assert_nothing_happened(world)


def test_topups_not_open_refuse_tenants_outside_the_allowlist(world: World) -> None:
    """设计 §7「建单校验」名单外一组：在计费模式与金额之前。"""
    listed = world.customer()
    outsider = world.customer(billing_mode=BillingMode.INTERNAL_METERED_ONLY)
    settings = world.settings(
        payment_topups_open=False, payment_topup_tenant_allowlist=listed.public_id
    )

    with pytest.raises(TopupsNotOpen) as raised:
        world.topup(outsider, "abc", settings=settings)

    assert (raised.value.code, raised.value.http_status) == ("TOPUPS_NOT_OPEN", 403)
    _assert_nothing_happened(world)


def test_topups_not_open_still_serve_the_allowlisted_tenant(world: World) -> None:
    """名单内一组：名单里的写法不区分大小写（启动时已规范成小写）。"""
    listed = world.customer()
    other = world.customer()
    settings = world.settings(
        payment_topups_open=False,
        payment_topup_tenant_allowlist=f"{other.public_id.upper()}, {listed.public_id.upper()}",
    )

    result = world.topup(listed, settings=settings)

    [row] = world.payments(listed)
    assert result.payment_public_id == row.public_id


def test_open_topups_need_no_allowlist(world: World) -> None:
    tenant = world.customer()

    world.topup(tenant, settings=world.settings(payment_topup_tenant_allowlist=""))

    assert len(world.payments(tenant)) == 1


def test_an_internal_tenant_cannot_top_up(world: World) -> None:
    """校验 2 在金额之前。"""
    tenant = world.customer(billing_mode=BillingMode.INTERNAL_METERED_ONLY)

    with pytest.raises(TopupNotAllowed) as raised:
        world.topup(tenant, "abc")

    assert (raised.value.code, raised.value.http_status) == ("TOPUP_NOT_ALLOWED", 403)
    _assert_nothing_happened(world)


INVALID_AMOUNTS: list[object] = [
    "",
    "abc",
    "9.99",
    "0",
    "1000000",
    "1000000.00",
    "10.001",
    "-10",
    "+10",
    " 10",
    "10 ",
    "10.",
    ".50",
    "1e3",
    "10,00",
    "１０",
    "10\n",
    10,
    10.0,
    Decimal("10"),
    True,
    None,
    ["10"],
]


@pytest.mark.parametrize("amount", INVALID_AMOUNTS, ids=repr)
def test_an_invalid_amount_is_refused(payment_world: World, amount: object) -> None:
    """校验 3 在税务政策之前：这个世界没有生效的税务政策，仍报金额错。"""
    world = payment_world
    tenant = world.customer()

    with pytest.raises(TopupAmountInvalid) as raised:
        world.topup(tenant, amount)

    assert (raised.value.code, raised.value.http_status) == ("TOPUP_AMOUNT_INVALID", 422)
    _assert_nothing_happened(world)


@pytest.mark.parametrize(
    ("amount", "stored", "minor"),
    [
        ("10", Decimal("10.00"), "1000"),
        ("10.5", Decimal("10.50"), "1050"),
        ("0010.00", Decimal("10.00"), "1000"),
        ("999999.99", Decimal("999999.99"), "99999999"),
    ],
)
def test_valid_amounts_are_stored_with_two_decimals(
    world: World, amount: str, stored: Decimal, minor: str
) -> None:
    tenant = world.customer()

    world.topup(tenant, amount)

    [row] = world.payments()
    assert row.expected_amount == row.wallet_credit_amount == stored
    [created] = world.stripe.created
    assert created.form["line_items[0][price_data][unit_amount]"] == minor


@pytest.mark.parametrize(
    ("overrides", "amount", "valid"),
    [
        ({"payment_topup_min": Decimal("2.00")}, "2", True),
        ({"payment_topup_min": Decimal("2.00")}, "1.99", False),
        ({"payment_topup_max": Decimal("500.00")}, "500.00", True),
        ({"payment_topup_max": Decimal("500.00")}, "500.01", False),
    ],
    ids=["min-2", "below-min", "at-max", "above-max"],
)
def test_the_configured_minimum_and_maximum_are_inclusive(
    world: World, overrides: dict[str, Any], amount: str, valid: bool
) -> None:
    tenant = world.customer()
    settings = world.settings(**overrides)

    if valid:
        world.topup(tenant, amount, settings=settings)
        assert len(world.payments()) == 1
    else:
        with pytest.raises(TopupAmountInvalid):
            world.topup(tenant, amount, settings=settings)
        _assert_nothing_happened(world)


def test_no_tax_policy_in_effect_is_unavailable_before_any_reuse(payment_world: World) -> None:
    """校验 4：没有生效政策就在复用之前报错。

    60 秒内有一笔会话还开着的单，也不查它、不复用。
    """
    world = payment_world
    # 只有一个明天才生效的版本（支付行的外键要它）。
    world.publish_tax(NOW + dt.timedelta(days=1))
    tenant = world.customer()
    world.stripe.open("cs_test_still_open")
    world.pending(tenant, session_id="cs_test_still_open", created_at=NOW - 10 * SECOND)

    with pytest.raises(TaxPolicyUnavailable) as raised:
        world.topup(tenant)

    assert (raised.value.code, raised.value.http_status) == ("TAX_POLICY_UNAVAILABLE", 503)
    assert len(world.payments()) == 1
    assert world.stripe.asked == [] and world.stripe.created == []


def test_a_tax_policy_failing_its_check_on_read_is_unavailable(payment_world: World) -> None:
    world = payment_world
    world.publish_tax(
        treatment={
            "schema_version": 1,
            "sst_registered": False,
            "tax_charged": False,
            # 末尾空白：`parse_treatment` 读出时拒绝。
            "document_notice": {"zh": NOTICE["zh"], "en": NOTICE["en"] + " "},
        }
    )
    tenant = world.customer()

    with pytest.raises(TaxPolicyUnavailable):
        world.topup(tenant)

    _assert_nothing_happened(world)


def test_a_tax_policy_that_charges_tax_is_unsupported(payment_world: World) -> None:
    world = payment_world
    world.publish_tax(
        treatment={
            "schema_version": 1,
            "sst_registered": True,
            "tax_charged": True,
            "document_notice": dict(NOTICE),
        },
        rate=Decimal("0.080000"),
    )
    tenant = world.customer()

    with pytest.raises(TaxPolicyUnsupported) as raised:
        world.topup(tenant)

    assert (raised.value.code, raised.value.http_status) == ("TAX_POLICY_UNSUPPORTED", 503)
    _assert_nothing_happened(world)


def test_an_unknown_tenant_is_not_found(world: World) -> None:
    with pytest.raises(CustomerNotFound):
        create_topup(
            world.factory,
            world.gateway_factory,
            987_654_321,
            "100.00",
            now=NOW,
            settings=world.settings(),
        )
    _assert_nothing_happened(world)


# --- 锁内重查 ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("change", "error"),
    [
        ({"account_status": AccountStatus.DISABLED}, AccountReadOnly),
        ({"account_status": AccountStatus.CLOSED}, AccountReadOnly),
        ({"billing_mode": BillingMode.INTERNAL_METERED_ONLY}, TopupNotAllowed),
    ],
    ids=["disabled", "closed", "internal"],
)
def test_the_locked_transaction_re_checks_the_tenant(
    world: World, monkeypatch, change: dict[str, Any], error: type[Exception]
) -> None:
    """锁外校验通过之后、锁租户之前管理员改了租户：锁内重查而拒绝。

    AccountReadOnly 原样抛出。
    """
    tenant = world.customer()
    calls = _lock_hook(monkeypatch, lambda _pass: world.set_tenant(tenant, **change))

    with pytest.raises(error):
        world.topup(tenant)

    assert calls == [1]
    _assert_nothing_happened(world)


def test_the_locked_transaction_re_resolves_the_tax_policy(world: World, monkeypatch) -> None:
    tenant = world.customer()
    version = world._any_tax_version()
    _lock_hook(monkeypatch, lambda _pass: world.end_tax(version, NOW))

    with pytest.raises(TaxPolicyUnavailable):
        world.topup(tenant)

    _assert_nothing_happened(world)


# --- 60 秒同额复用 -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("later", "window", "reused"),
    [
        (59 * SECOND, None, True),
        (60 * SECOND, None, True),
        (61 * SECOND, None, False),
        (90 * SECOND, 120, True),
        (0 * SECOND, 0, False),
    ],
    ids=["59s", "60s", "61s", "window-120-at-90s", "window-0"],
)
def test_the_same_amount_within_the_reuse_window_reuses_the_payment(
    world: World, later: dt.timedelta, window: int | None, reused: bool
) -> None:
    """ADR-0007：窗口内同额复用（会话还开着就直接返回它）。

    `payment_reuse_window_seconds` 为 0 时不复用。
    """
    tenant = world.customer()
    settings = (
        world.settings() if window is None else world.settings(payment_reuse_window_seconds=window)
    )
    first = world.topup(tenant, settings=settings)

    second = world.topup(tenant, now=NOW + later, settings=settings)

    rows = world.payments()
    if reused:
        assert second == first
        assert len(rows) == 1
        # 复用只查了一次会话，没有再建。
        assert world.stripe.asked == [rows[0].gateway_payment_id]
        assert len(world.stripe.created) == 1
    else:
        assert second.payment_public_id != first.payment_public_id
        assert len(rows) == 2
        assert world.stripe.asked == []
        assert len(world.stripe.created) == 2


@pytest.mark.parametrize("other", ["amount", "tenant"])
def test_another_amount_or_another_tenant_is_never_reused(world: World, other: str) -> None:
    tenant = world.customer()
    first = world.topup(tenant, "100.00")

    if other == "amount":
        second = world.topup(tenant, "100.01", now=NOW + SECOND)
    else:
        second = world.topup(world.customer(), "100.00", now=NOW + SECOND)

    assert second.payment_public_id != first.payment_public_id
    assert world.stripe.asked == []
    assert len(world.payments()) == 2


@pytest.mark.parametrize(
    ("remaining", "reused"),
    [(31 * MINUTE - SECOND, False), (31 * MINUTE, True)],
    ids=["30m59s", "31m"],
)
def test_a_payment_with_less_than_31_minutes_left_is_not_reused(
    world: World, remaining: dt.timedelta, reused: bool
) -> None:
    """设计 §7「待付超时与 Stripe 区间」：超时配成 2400 秒。

    剩余不足 31 分钟的单不复用、新建一笔；恰好 31 分钟仍复用。
    """
    tenant = world.customer()
    world.stripe.open("cs_test_short_lived")
    old = world.pending(
        tenant,
        session_id="cs_test_short_lived",
        created_at=NOW - 10 * SECOND,
        expires_at=NOW + remaining,
    )
    settings = world.settings(payment_pending_timeout_seconds=2400)

    result = world.topup(tenant, settings=settings)

    if reused:
        assert result.payment_public_id == old.public_id
        assert result.expires_at == old.expires_at
        assert world.stripe.created == []
        return
    assert result.payment_public_id != old.public_id
    assert world.stripe.asked == []
    new = world.row(result.payment_public_id)
    assert new.expires_at == result.expires_at == NOW + dt.timedelta(seconds=2400)


@pytest.mark.parametrize("timeout", [2400, 3600, 86_400])
def test_the_expiry_sent_to_stripe_is_within_its_window(world: World, timeout: int) -> None:
    """Stripe 要求会话过期时刻在创建后 30 分钟到 24 小时之间。"""
    tenant = world.customer()

    world.topup(tenant, settings=world.settings(payment_pending_timeout_seconds=timeout))

    [row] = world.payments()
    assert row.expires_at == NOW + dt.timedelta(seconds=timeout)
    [created] = world.stripe.created
    lead = int(created.form["expires_at"]) - _unix(NOW)
    assert lead == timeout
    assert 30 * 60 <= lead <= 24 * 60 * 60


# --- 复用排除 ---------------------------------------------------------------------------


@pytest.mark.parametrize("closed", ["expired", "open-without-url", "not-found"])
def test_a_candidate_whose_session_cannot_be_reused_is_excluded(world: World, closed: str) -> None:
    """设计 §7「复用排除」第一组：60 秒内的候选会话已不是 OPEN。

    或 OPEN 却没有地址、或 Stripe 拒绝查询：一样排除它、新建一笔。
    """
    tenant = world.customer()
    first = world.topup(tenant)
    session_id = world.row(first.payment_public_id).gateway_payment_id
    assert session_id is not None
    if closed == "expired":
        world.stripe.expire(session_id)
    elif closed == "open-without-url":
        world.stripe.sessions[session_id]["url"] = None
    else:
        del world.stripe.sessions[session_id]

    second = world.topup(tenant, now=NOW + 30 * SECOND)

    assert second.payment_public_id != first.payment_public_id
    assert world.stripe.asked == [session_id]
    rows = world.payments()
    assert [row.public_id for row in rows] == [first.payment_public_id, second.payment_public_id]
    # 被排除的那笔不变（状态归确认与 B）。
    assert rows[0].status is PaymentStatus.PENDING and rows[0].status_reason is None


@pytest.mark.parametrize("status_code", [503, 429])
def test_an_unavailable_reuse_check_is_retryable_and_creates_nothing(
    world: World, status_code: int
) -> None:
    tenant = world.customer()
    world.topup(tenant)
    world.stripe.status_code = status_code

    with pytest.raises(PaymentGatewayUnavailable) as raised:
        world.topup(tenant, now=NOW + 30 * SECOND)

    assert (raised.value.code, raised.value.http_status) == ("PAYMENT_GATEWAY_UNAVAILABLE", 503)
    assert len(world.payments()) == 1
    assert len(world.stripe.created) == 1


def test_a_concurrent_payment_found_under_the_lock_is_checked_and_reused(
    world: World, monkeypatch
) -> None:
    """锁内找到并发产生的、已有网关 id 的一笔。

    提交后回到事务外查询它，会话开着就复用。
    """
    tenant = world.customer()
    world.stripe.open("cs_test_concurrent")
    concurrent: list[Payment] = []

    def concurrent_request(locked_pass: int) -> None:
        if locked_pass == 1:
            concurrent.append(world.pending(tenant, session_id="cs_test_concurrent"))

    calls = _lock_hook(monkeypatch, concurrent_request)

    result = world.topup(tenant)

    [row] = world.payments()
    assert row.public_id == concurrent[0].public_id
    assert result == TopupResult(
        row.public_id, f"{CHECKOUT_BASE_URL}/cs_test_concurrent", row.expires_at
    )
    assert calls == [1]
    assert world.stripe.asked == ["cs_test_concurrent"]
    assert world.stripe.created == []


def test_a_second_concurrent_payment_under_the_lock_is_not_checked_a_new_one_is_created(
    world: World, monkeypatch
) -> None:
    """至多再来一次。

    第一轮找到的会话已过期 → 排除；第二轮又找到一笔已有网关 id 的 → 不再查询，新建。
    """
    tenant = world.customer()
    concurrent: list[Payment] = []

    def concurrent_request(locked_pass: int) -> None:
        session_id = f"cs_test_concurrent_{locked_pass}"
        world.stripe.open(session_id)
        world.stripe.expire(session_id)
        concurrent.append(world.pending(tenant, session_id=session_id))

    calls = _lock_hook(monkeypatch, concurrent_request)

    result = world.topup(tenant)

    assert calls == [1, 2]
    assert world.stripe.asked == ["cs_test_concurrent_1"]
    rows = world.payments()
    assert [row.public_id for row in rows] == [
        concurrent[0].public_id,
        concurrent[1].public_id,
        result.payment_public_id,
    ]
    [created] = world.stripe.created
    assert created.idempotency_key == _key(result.payment_public_id)
    assert rows[2].gateway_payment_id is not None


# --- Stripe 建单失败、重试与并发 ----------------------------------------------------


@pytest.mark.parametrize(
    ("status_code", "code"),
    [
        (503, None),
        (429, None),
        (500, "api_error"),
        (400, "parameter_invalid_integer"),
        (402, None),
    ],
    ids=["503", "429", "500", "400", "402"],
)
def test_a_stripe_failure_then_a_retry_reuses_the_payment_with_the_same_idempotency_key(
    world: World, status_code: int, code: str | None
) -> None:
    """设计 §7「建单校验」：Stripe 失败 503 后重试复用同一笔并用同一个幂等键。
    不可用（5xx、429）与拒绝（其他 4xx）都写 GATEWAY_CREATE_FAILED。"""
    tenant = world.customer()
    world.stripe.create_failures.append(stripe_error(status_code, code))

    with pytest.raises(PaymentGatewayUnavailable) as raised:
        world.topup(tenant)

    assert (raised.value.code, raised.value.http_status) == ("PAYMENT_GATEWAY_UNAVAILABLE", 503)
    [row] = world.payments()
    assert (row.status, row.status_reason) == (PaymentStatus.PENDING, GATEWAY_CREATE_FAILED)
    assert row.gateway_payment_id is None
    assert row.updated_at == NOW

    retried = world.topup(tenant, now=NOW + 30 * SECOND)

    assert retried.payment_public_id == row.public_id
    assert retried.expires_at == row.expires_at
    [after] = world.payments()
    assert (after.status, after.status_reason) == (PaymentStatus.PENDING, None)
    assert after.gateway_payment_id is not None
    assert retried.redirect_url == f"{CHECKOUT_BASE_URL}/{after.gateway_payment_id}"
    assert after.updated_at == NOW + 30 * SECOND
    assert [c.idempotency_key for c in world.stripe.created] == [_key(row.public_id)] * 2
    # 没有网关 id 的候选不必查会话。
    assert world.stripe.asked == []


def test_a_stripe_failure_logs_the_error_code_without_secrets(
    world: World, caplog: pytest.LogCaptureFixture
) -> None:
    tenant = world.customer()
    world.stripe.create_failures.append(stripe_error(400, "parameter_invalid_integer"))

    with caplog.at_level(logging.INFO, logger=LOGGER), pytest.raises(PaymentGatewayUnavailable):
        world.topup(tenant)

    [row] = world.payments()
    warnings = [r for r in caplog.records if r.name == LOGGER and r.levelno == logging.WARNING]
    assert [(r.payment_id, r.error_code) for r in warnings] == [  # type: ignore[attr-defined]
        (row.public_id, "parameter_invalid_integer")
    ]
    for record in caplog.records:
        assert SECRET_KEY not in record.getMessage()
        assert "100.00" not in record.getMessage()


def test_a_retry_after_the_window_creates_a_new_payment(world: World) -> None:
    tenant = world.customer()
    world.stripe.create_failures.append(stripe_error(503))
    with pytest.raises(PaymentGatewayUnavailable):
        world.topup(tenant)

    retried = world.topup(tenant, now=NOW + 61 * SECOND)

    old, new = world.payments()
    assert retried.payment_public_id == new.public_id
    # 旧的那笔留给 B 置为过期。
    assert (old.status, old.status_reason, old.gateway_payment_id) == (
        PaymentStatus.PENDING,
        GATEWAY_CREATE_FAILED,
        None,
    )


def test_a_concurrent_reuse_refused_with_idempotency_key_in_use_writes_no_reason(
    world: World,
) -> None:
    """设计 §7「并发复用同一幂等键」。

    两个请求在 60 秒内复用同一笔，Stripe 对后一个回 `idempotency_key_in_use`：
    后一个 503、不写 GATEWAY_CREATE_FAILED；先一个写入网关 id 并清空原因。
    """
    tenant = world.customer()
    # 先有一次失败：那笔带着 GATEWAY_CREATE_FAILED（updated_at = NOW）等重试。
    world.stripe.create_failures.append(stripe_error(503))
    with pytest.raises(PaymentGatewayUnavailable):
        world.topup(tenant)
    [row] = world.payments()

    seen: list[tuple[object, Payment]] = []

    def later_request() -> None:
        # 先一个请求正在调 Stripe（不持任何锁）；后一个复用同一笔，
        # Stripe 说这个幂等键正在用。
        world.stripe.create_failures.append(stripe_error(400, "idempotency_key_in_use"))
        try:
            outcome: object = world.topup(tenant, now=NOW + 20 * SECOND)
        except PaymentGatewayUnavailable as error:
            outcome = error
        seen.append((outcome, world.row(row.public_id)))

    world.stripe.on_create = later_request

    first = world.topup(tenant, now=NOW + 10 * SECOND)

    [(outcome, during)] = seen
    assert isinstance(outcome, PaymentGatewayUnavailable)
    # 后一个没有写原因：仍是第一次失败写下的那一份。
    assert (during.status_reason, during.updated_at) == (GATEWAY_CREATE_FAILED, NOW)
    assert during.gateway_payment_id is None
    [after] = world.payments()
    assert first.payment_public_id == row.public_id
    assert (after.status, after.status_reason) == (PaymentStatus.PENDING, None)
    assert after.gateway_payment_id is not None
    assert after.updated_at == NOW + 10 * SECOND
    assert [c.idempotency_key for c in world.stripe.created] == [_key(row.public_id)] * 3


def test_a_gateway_id_already_written_by_a_concurrent_reuse_is_accepted(world: World) -> None:
    """条件更新影响 0 行：并发的复用请求已写，两者相同即可。"""
    tenant = world.customer()
    results: list[TopupResult] = []
    world.stripe.on_create = lambda: results.append(world.topup(tenant, now=NOW + 5 * SECOND))

    first = world.topup(tenant)

    [concurrent] = results
    assert first == concurrent
    [row] = world.payments()
    assert row.public_id == first.payment_public_id
    # 写入的是后到的那一个（它先拿到 Stripe 的回答）。
    assert row.updated_at == NOW + 5 * SECOND
    assert row.status_reason is None
    assert [c.idempotency_key for c in world.stripe.created] == [_key(row.public_id)] * 2


def test_a_different_gateway_id_already_written_is_an_error(
    world: World, caplog: pytest.LogCaptureFixture
) -> None:
    """库里已有的网关 id 与这次 Stripe 返回的不同 → 记 ERROR、503。

    同一幂等键正常不会出现。
    """
    tenant = world.customer()
    world.stripe.distinct_sessions = True
    results: list[TopupResult] = []
    world.stripe.on_create = lambda: results.append(world.topup(tenant, now=NOW + 5 * SECOND))

    with caplog.at_level(logging.INFO, logger=LOGGER), pytest.raises(PaymentGatewayUnavailable):
        world.topup(tenant)

    [concurrent] = results
    [row] = world.payments()
    assert concurrent.redirect_url == f"{CHECKOUT_BASE_URL}/{row.gateway_payment_id}"
    errors = [r for r in caplog.records if r.name == LOGGER and r.levelno == logging.ERROR]
    assert [r.payment_id for r in errors] == [row.public_id]  # type: ignore[attr-defined]

    # 客户重试时复用库里那一笔。
    retried = world.topup(tenant, now=NOW + 10 * SECOND)
    assert retried == concurrent


# --- 真 MySQL：两个并发请求只新建一笔 ---------------------------------------------


def test_two_concurrent_requests_create_one_payment(mysql_payment_world: World) -> None:
    """设计 §7「复用排除」第二组。

    60 秒内的候选会话已不是 OPEN；两个请求同时到达 → 都排除它，在租户锁上排队，
    新建一笔且只新建一笔，后到者复用它。
    """
    world = mysql_payment_world
    world.publish_tax()
    tenant = world.customer()
    old = world.topup(tenant, now=NOW - 30 * SECOND)
    old_session = world.row(old.payment_public_id).gateway_payment_id
    assert old_session is not None
    world.stripe.expire(old_session)
    start = threading.Barrier(2)
    settings = world.settings()

    def attempt(_index: int) -> TopupResult:
        start.wait(timeout=30)
        return create_topup(
            world.factory, world.gateway_factory, tenant.id, "100.00", now=NOW, settings=settings
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = pool.map(attempt, range(2))

    assert first == second
    rows = world.payments()
    assert [row.public_id for row in rows] == [old.payment_public_id, first.payment_public_id]
    new = rows[1]
    assert new.gateway_payment_id is not None and new.status_reason is None
    keys = {c.idempotency_key for c in world.stripe.created}
    assert keys == {_key(old.payment_public_id), _key(new.public_id)}

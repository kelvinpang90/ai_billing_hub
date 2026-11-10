"""Trusted payment confirmation and TOPUP posting (design gate #580 v3, AIH-TASK-174).

设计 §7 标 174 的各行：

- **两张决策表每一行**（`state = PAID` 的六条判定与其顺序；其他 `state` × 当前状态）、金额精度三组、
  手续费三种、复核记录两组、过期后到账清原因、同一网关支付号两笔内部支付、事务中途失败与撞唯一约束：
  `payment_world` 夹具的两个参数，在 SQLite 与真 MySQL 上各跑一次；
- **入账走账本与复机**（余额为负的暂停租户）、**多事件并发只入账一次**（两线程）、**关户与入账互斥**
  （两种先后）：只在真 MySQL 上。SQLite 没有钱包触发器（余额不随账本推进），`FOR UPDATE` 也被忽略。

Stripe 一律是注入了假传输（`httpx2.MockTransport`）的 `StripeGateway`，不联网；地址是虚构的。

MySQL 那部分需要 `BILLING_TEST_DATABASE_URL` 指向一个**可以被清空的**库，没设时 skip ——
**不要把 skipped 读成 passed**，CI 设了它并把 skipped 判成失败。
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import httpx2
import pytest
from alembic.config import Config
from sqlalchemy import Engine, create_engine, delete, event, func, select, text, update
from sqlalchemy.orm import Session, sessionmaker

from alembic import command
from app.core.database import create_session_factory
from app.models.auth import AuditAction, AuditLog, DomainOutbox, User, UserRole, UserStatus
from app.models.base import Base
from app.models.payments import Payment, PaymentGateway, PaymentGatewayEvent, PaymentStatus
from app.models.tax_policy import TaxJurisdiction, TaxPolicyStatus, TaxPolicyType, TaxPolicyVersion
from app.models.tenancy import AccountStatus, BillingMode, BillingStatus, Project, Tenant
from app.models.wallet import ReferenceType, TransactionType, Wallet, WalletTransaction
from app.payments.base import GatewayRequestRejected, GatewayState, GatewayUnavailable
from app.payments.stripe_gateway import StripeGateway
from app.repositories import tenancy
from app.repositories.wallet import (
    EVENT_BILLING_STATUS_CHANGED,
    REASON_BALANCE_POSITIVE,
    create_wallet,
    get_wallet_for_tenant,
    post_transaction,
    verify_wallet,
)
from app.services import payments as payments_service
from app.services.audit_query import PUBLIC_ENTITY_TYPES
from app.services.payments import (
    AMOUNT_MISMATCH,
    CURRENCY_MISMATCH,
    ENTITY_PAYMENT,
    GATEWAY_EXPIRED,
    GATEWAY_FAILED,
    GATEWAY_INCONSISTENT,
    GATEWAY_PAYMENT_REUSED,
    INTERNAL_TENANT,
    REFERENCE_MISMATCH,
    TENANT_CLOSED,
    ConfirmConflict,
    ConfirmResult,
    PaymentNotFound,
    confirm_payment,
)

TEST_EMAIL_DOMAIN = "@payment-confirm-test.example.com"
# 虚构的地址：这些用例不会连到任何主机。
API_BASE_URL = "https://stripe-api.example.test"
SECRET_KEY = "sk_test_payment_confirm_secret_value"
WEBHOOK_SECRET = "whsec_payment_confirm_signing_value"

# 固定值而不是 utc_now()：断言「存进去的就是调用方给的那个时刻」要能逐字比对。
CREATED = dt.datetime(2026, 11, 8, 8, 0, 0)
EXPIRES = dt.datetime(2026, 11, 8, 9, 0, 0)
NOW = dt.datetime(2026, 11, 8, 8, 20, 0)
LATER = dt.datetime(2026, 11, 8, 8, 25, 0)

LOGGER = "app.services.payments"
AUDIT_FIELDS = {"status", "status_reason", "expected_amount", "confirmed_amount", "gateway_fee"}

# 非终结状态（设计 §4：「终结」只指 PAID 与 REVIEW_REQUIRED）。
OPEN_STATUSES = [
    PaymentStatus.PENDING,
    PaymentStatus.EXPIRED,
    PaymentStatus.FAILED,
    PaymentStatus.CANCELLED,
]
FINAL_STATUSES = [PaymentStatus.PAID, PaymentStatus.REVIEW_REQUIRED]
# 建单与之前的确认会留下的原因，用来看清「不变」确实没有动它。
PRIOR_REASON = {
    PaymentStatus.PENDING: None,
    PaymentStatus.EXPIRED: GATEWAY_EXPIRED,
    PaymentStatus.FAILED: GATEWAY_FAILED,
    PaymentStatus.CANCELLED: None,
    PaymentStatus.PAID: None,
    PaymentStatus.REVIEW_REQUIRED: AMOUNT_MISMATCH,
}


# --- 假 Stripe -----------------------------------------------------------------------


def stripe_session(
    state: GatewayState,
    *,
    session_id: str,
    reference: str | None,
    amount_total: int | None = 10000,
    currency: str | None = "myr",
    intent_id: str | None = None,
    fee: int | None = 290,
    fee_currency: str | None = "myr",
) -> dict[str, Any]:
    """A Checkout Session, payment intent expanded down to its fee, that maps to `state`."""
    intent_id = intent_id or f"pi_test_{uuid.uuid4().hex}"

    def intent(status: str, received: int | None) -> dict[str, Any]:
        charge: dict[str, Any] = {"id": f"ch_test_{uuid.uuid4().hex}"}
        if fee is not None:
            charge["balance_transaction"] = {"fee": fee, "currency": fee_currency}
        return {
            "id": intent_id,
            "status": status,
            "amount_received": received,
            "latest_charge": charge,
        }

    body: dict[str, Any] = {
        "id": session_id,
        "client_reference_id": reference,
        "amount_total": amount_total,
        "currency": currency,
        "url": None,
        "payment_intent": None,
    }
    if state is GatewayState.PAID:
        body.update(
            status="complete",
            payment_status="paid",
            payment_intent=intent("succeeded", amount_total),
        )
    elif state is GatewayState.INCONSISTENT:
        body.update(
            status="complete", payment_status="paid", payment_intent=intent("processing", 0)
        )
    elif state is GatewayState.FAILED:
        body.update(
            status="complete",
            payment_status="unpaid",
            payment_intent=intent("requires_payment_method", 0),
        )
    elif state is GatewayState.EXPIRED:
        body.update(status="expired", payment_status="unpaid")
    elif state is GatewayState.OPEN:
        body.update(status="open", payment_status="unpaid", url="https://checkout.example.test/s")
    else:
        body.update(status="mystery", payment_status="unpaid")
    return body


class FakeStripe:
    """Answers `GET /v1/checkout/sessions/{id}` from `sessions`; counts what it was asked."""

    def __init__(self) -> None:
        self.sessions: dict[str, dict[str, Any]] = {}
        self.asked: list[str] = []
        self.status_code = 200
        self._lock = threading.Lock()

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        session_id = request.url.path.rsplit("/", 1)[-1]
        with self._lock:
            self.asked.append(session_id)
        if self.status_code != 200:
            return httpx2.Response(self.status_code, json={})
        body = self.sessions.get(session_id)
        if body is None:
            return httpx2.Response(404, json={"error": {"code": "resource_missing"}})
        return httpx2.Response(200, json=body)

    def gateway(self) -> StripeGateway:
        return StripeGateway(
            SECRET_KEY,
            WEBHOOK_SECRET,
            timeout_seconds=5,
            webhook_tolerance_seconds=300,
            transport=httpx2.MockTransport(self.handle),
            api_base_url=API_BASE_URL,
        )


# --- 世界 -----------------------------------------------------------------------------


@dataclass(frozen=True)
class Customer:
    id: int
    public_id: str


@dataclass(frozen=True)
class PaymentRef:
    public_id: str
    session_id: str | None
    tenant: Customer


@dataclass
class World:
    factory: sessionmaker[Session]
    admin_id: int
    tax_version_id: int
    stripe: FakeStripe = field(default_factory=FakeStripe)

    def customer(
        self,
        *,
        account_status: AccountStatus = AccountStatus.ENABLED,
        billing_mode: BillingMode = BillingMode.PREPAID,
    ) -> Customer:
        """A tenant with an empty wallet: balance 0, SUSPENDED, status_version 0."""
        with self.factory() as session:
            tenant = tenancy.create_tenant(
                session,
                company_name="Payment Confirm Test Sdn Bhd",
                email="ops@example.com",
                now=CREATED,
            )
            tenant.account_status = account_status
            tenant.billing_mode = billing_mode
            create_wallet(session, tenant_id=tenant.id, now=CREATED)
            session.commit()
            return Customer(int(tenant.id), tenant.public_id)

    def payment(
        self,
        tenant: Customer,
        *,
        amount: str = "100.00",
        status: PaymentStatus = PaymentStatus.PENDING,
        reason: str | None = None,
        with_session: bool = True,
        intent_id: str | None = None,
    ) -> PaymentRef:
        """A payment as the top-up and earlier confirmations would have left it."""
        public_id = str(uuid.uuid4())
        session_id = f"cs_test_{uuid.uuid4().hex}" if with_session else None
        paid = status is PaymentStatus.PAID
        with self.factory() as session:
            session.add(
                Payment(
                    public_id=public_id,
                    tenant_id=tenant.id,
                    gateway=PaymentGateway.STRIPE,
                    expected_amount=Decimal(amount),
                    expected_currency="MYR",
                    wallet_credit_amount=Decimal(amount),
                    tax_policy_version_id=self.tax_version_id,
                    tax_amount=None,
                    confirmed_amount=Decimal(amount) if paid else None,
                    confirmed_currency="MYR" if paid else None,
                    gateway_payment_id=session_id,
                    gateway_payment_intent_id=intent_id,
                    gateway_fee=None,
                    status=status,
                    status_reason=reason,
                    review_required_at=CREATED if status is PaymentStatus.REVIEW_REQUIRED else None,
                    expires_at=EXPIRES,
                    paid_at=CREATED if paid else None,
                    last_reconciled_at=None,
                    created_at=CREATED,
                    updated_at=CREATED,
                )
            )
            session.commit()
        return PaymentRef(public_id, session_id, tenant)

    def serve(self, payment: PaymentRef, state: GatewayState, **overrides: Any) -> dict[str, Any]:
        """What Stripe will answer for this payment's Checkout Session."""
        assert payment.session_id is not None
        options: dict[str, Any] = {"session_id": payment.session_id, "reference": payment.public_id}
        options.update(overrides)
        body = stripe_session(state, **options)
        self.stripe.sessions[payment.session_id] = body
        return body

    def confirm(self, payment: PaymentRef, *, now: dt.datetime = NOW) -> ConfirmResult:
        return confirm_payment(self.factory, self.stripe.gateway(), payment.public_id, now=now)

    # -- reading back --

    def row(self, payment: PaymentRef) -> Payment:
        with self.factory() as session:
            row = session.execute(
                select(Payment).where(Payment.public_id == payment.public_id)
            ).scalar_one()
            session.expunge(row)
            return row

    def topups(self, payment: PaymentRef | None = None) -> list[WalletTransaction]:
        statement = select(WalletTransaction).where(
            WalletTransaction.transaction_type == TransactionType.TOPUP
        )
        if payment is not None:
            statement = statement.where(WalletTransaction.reference_id == payment.public_id)
        with self.factory() as session:
            rows = list(session.execute(statement.order_by(WalletTransaction.id)).scalars())
            for row in rows:
                session.expunge(row)
            return rows

    def audits(self, payment: PaymentRef) -> list[AuditLog]:
        statement = (
            select(AuditLog)
            .where(AuditLog.action == AuditAction.PAYMENT_STATUS_CHANGE)
            .where(AuditLog.entity_id == payment.public_id)
            .order_by(AuditLog.id)
        )
        with self.factory() as session:
            rows = list(session.execute(statement).scalars())
            for row in rows:
                session.expunge(row)
            return rows

    def wallet(self, tenant: Customer) -> tuple[Decimal, int, BillingStatus, int]:
        """(balance, wallet version, billing_status, status_version), as committed."""
        with self.factory() as session:
            wallet = get_wallet_for_tenant(session, tenant.id)
            row = session.get(Tenant, tenant.id)
            assert wallet is not None and row is not None
            return wallet.balance, wallet.version, row.billing_status, row.status_version

    def snapshot(self, payment: PaymentRef) -> dict[str, object]:
        """Everything a confirmation may write, as committed."""
        row = self.row(payment)
        with self.factory() as session:
            ledger = session.execute(
                select(func.count()).select_from(WalletTransaction)
            ).scalar_one()
            audits = session.execute(select(func.count()).select_from(AuditLog)).scalar_one()
            outbox = session.execute(select(func.count()).select_from(DomainOutbox)).scalar_one()
        return {
            "payment": _columns(row),
            "ledger": ledger,
            "audits": audits,
            "outbox": outbox,
        }


def _columns(row: Payment) -> dict[str, object]:
    return {column.key: getattr(row, column.key) for column in Payment.__table__.columns}


def _state(text_value: str | None) -> dict[str, Any]:
    return json.loads(text_value or "{}")


def _money(value: object) -> Decimal | None:
    """An audit money field: fixed-point text (never `1E+2` / `0E-8`) or null."""
    if value is None:
        return None
    assert isinstance(value, str) and "E" not in value.upper(), value
    return Decimal(value)


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
    # ⚠️ 建表必须走 alembic：钱包的触发器只有迁移会建（同 test_wallet_adjustment_service.py）。
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

    账本、审计与税务政策版本都拒绝 DELETE（触发器），只能在关掉外键检查后 TRUNCATE。
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
    """An admin (the tax policy version needs a creator) and a draft MY / SST version."""
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
        session.flush()
        version = TaxPolicyVersion(
            public_id=str(uuid.uuid4()),
            jurisdiction=TaxJurisdiction.MY,
            policy_type=TaxPolicyType.SST,
            rate=None,
            treatment_json={"schema_version": 1, "sst_registered": False, "tax_charged": False},
            status=TaxPolicyStatus.DRAFT,
            effective_from=None,
            effective_to=None,
            source_reference="Fictional policy for a payment confirmation test",
            created_by=admin.id,
            approved_by=None,
            approved_at=None,
            created_at=CREATED,
            updated_at=CREATED,
        )
        session.add(version)
        session.commit()
        return World(factory=factory, admin_id=int(admin.id), tax_version_id=int(version.id))


# --- 第一张表：state = PAID 的判定（设计 §2） -----------------------------------------


def test_a_paid_session_that_meets_every_condition_posts_one_topup(payment_world: World) -> None:
    """全部满足：PAID、写确认字段与手续费、一条 TOPUP（金额 = wallet_credit_amount）、一条审计。"""
    world = payment_world
    tenant = world.customer()
    payment = world.payment(tenant)
    body = world.serve(payment, GatewayState.PAID, fee=290)
    intent_id = body["payment_intent"]["id"]

    result = world.confirm(payment)

    assert result == ConfirmResult(
        payment_id=payment.public_id,
        old_status=PaymentStatus.PENDING,
        new_status=PaymentStatus.PAID,
        reason=None,
        posted=True,
        state=GatewayState.PAID,
    )
    row = world.row(payment)
    assert row.status is PaymentStatus.PAID
    assert row.status_reason is None
    assert row.confirmed_amount == Decimal("100.00")
    assert row.confirmed_currency == "MYR"
    assert row.paid_at == NOW
    assert row.last_reconciled_at == NOW
    assert row.updated_at == NOW
    assert row.gateway_payment_intent_id == intent_id
    assert row.gateway_fee == Decimal("2.90")
    assert row.review_required_at is None

    [topup] = world.topups()
    assert topup.amount == Decimal("100.00")
    assert (topup.reference_type, topup.reference_id) == (ReferenceType.PAYMENT, payment.public_id)
    assert topup.tenant_id == tenant.id
    assert topup.created_at == NOW
    assert world.stripe.asked == [payment.session_id]

    [audit] = world.audits(payment)
    assert (audit.actor_user_id, audit.actor_role) == (None, "SYSTEM")
    assert (audit.entity_type, audit.entity_id) == (ENTITY_PAYMENT, payment.public_id)
    assert audit.created_at == NOW
    before, after = _state(audit.before_state), _state(audit.after_state)
    assert set(before) == set(after) == AUDIT_FIELDS
    assert (before["status"], before["status_reason"]) == ("PENDING", None)
    assert (after["status"], after["status_reason"]) == ("PAID", None)
    assert _money(before["expected_amount"]) == _money(after["expected_amount"]) == Decimal("100")
    assert (_money(before["confirmed_amount"]), _money(before["gateway_fee"])) == (None, None)
    assert _money(after["confirmed_amount"]) == Decimal("100")
    assert _money(after["gateway_fee"]) == Decimal("2.9")


def test_a_disabled_tenant_is_credited_as_usual(payment_world: World) -> None:
    """Kelvin 2026-11-06：`DISABLED` 照常入账（充值只改计费状态，不解除停用）。"""
    world = payment_world
    tenant = world.customer(account_status=AccountStatus.DISABLED)
    payment = world.payment(tenant)
    world.serve(payment, GatewayState.PAID)

    result = world.confirm(payment)

    assert (result.new_status, result.posted) == (PaymentStatus.PAID, True)
    assert len(world.topups(payment)) == 1
    with world.factory() as session:
        row = session.get(Tenant, tenant.id)
        assert row is not None and row.account_status is AccountStatus.DISABLED


@dataclass(frozen=True)
class Unmet:
    """One way the PAID checks fail: how to set it up, and the reason it must give."""

    reason: str
    tenant: dict[str, Any] = field(default_factory=dict)
    session: dict[str, Any] = field(default_factory=dict)
    # 另一笔内部支付先占了 Stripe 报的那个 PI。
    pi_taken: bool = False


UNMET = {
    "client_reference_id is another payment": Unmet(
        REFERENCE_MISMATCH, session={"reference": str(uuid.uuid4())}
    ),
    "client_reference_id missing": Unmet(REFERENCE_MISMATCH, session={"reference": None}),
    "session id is another session": Unmet(
        REFERENCE_MISMATCH, session={"session_id": "cs_test_someone_else"}
    ),
    "currency USD": Unmet(CURRENCY_MISMATCH, session={"currency": "usd"}),
    "currency missing": Unmet(CURRENCY_MISMATCH, session={"currency": None}),
    "one sen short": Unmet(AMOUNT_MISMATCH, session={"amount_total": 9999}),
    "PI held by another payment": Unmet(GATEWAY_PAYMENT_REUSED, pi_taken=True),
    "tenant CLOSED": Unmet(TENANT_CLOSED, tenant={"account_status": AccountStatus.CLOSED}),
    "internal tenant": Unmet(
        INTERNAL_TENANT, tenant={"billing_mode": BillingMode.INTERNAL_METERED_ONLY}
    ),
    # 顺序：多条不满足时由表里靠前的那条决定原因。
    "reference and currency": Unmet(
        REFERENCE_MISMATCH, session={"reference": None, "currency": "usd"}
    ),
    "currency and amount": Unmet(CURRENCY_MISMATCH, session={"currency": "usd", "amount_total": 1}),
    "amount and PI held": Unmet(AMOUNT_MISMATCH, session={"amount_total": 1}, pi_taken=True),
    "PI held and CLOSED": Unmet(
        GATEWAY_PAYMENT_REUSED, tenant={"account_status": AccountStatus.CLOSED}, pi_taken=True
    ),
    "CLOSED and internal": Unmet(
        TENANT_CLOSED,
        tenant={
            "account_status": AccountStatus.CLOSED,
            "billing_mode": BillingMode.INTERNAL_METERED_ONLY,
        },
    ),
}


@pytest.mark.parametrize("case", list(UNMET))
def test_the_first_unmet_paid_condition_decides_the_review_reason(
    payment_world: World, case: str
) -> None:
    """设计 §2 第一张表：按表的顺序判定，第一条不满足的决定原因；不入账，写复核字段与一条审计。"""
    world = payment_world
    unmet = UNMET[case]
    tenant = world.customer(**unmet.tenant)
    payment = world.payment(tenant)
    intent_id = f"pi_test_{uuid.uuid4().hex}"
    holder = None
    if unmet.pi_taken:
        holder = world.payment(world.customer(), status=PaymentStatus.PAID, intent_id=intent_id)
    world.serve(payment, GatewayState.PAID, intent_id=intent_id, **unmet.session)

    result = world.confirm(payment)

    assert (result.old_status, result.new_status) == (
        PaymentStatus.PENDING,
        PaymentStatus.REVIEW_REQUIRED,
    )
    assert (result.reason, result.posted, result.state) == (unmet.reason, False, GatewayState.PAID)
    row = world.row(payment)
    assert row.status is PaymentStatus.REVIEW_REQUIRED
    assert row.status_reason == unmet.reason
    assert row.review_required_at == NOW
    assert row.last_reconciled_at == NOW
    assert row.paid_at is None
    assert row.gateway_fee is None
    # PI id 只在没被占用时写入（不论原因是哪一条）。
    assert row.gateway_payment_intent_id == (None if unmet.pi_taken else intent_id)
    assert world.topups() == []
    [audit] = world.audits(payment)
    after = _state(audit.after_state)
    assert (after["status"], after["status_reason"]) == ("REVIEW_REQUIRED", unmet.reason)
    assert audit.reason == unmet.reason
    if holder is not None:
        assert world.row(holder).gateway_payment_intent_id == intent_id


@pytest.mark.parametrize(
    ("expected", "amount_total", "reason"),
    [("100.00", 10000, None), ("99.99", 9999, None), ("100.01", 10000, AMOUNT_MISMATCH)],
    ids=["100.00-10000", "99.99-9999", "100.01-10000"],
)
def test_amounts_are_compared_as_exact_integer_sen(
    payment_world: World, expected: str, amount_total: int, reason: str | None
) -> None:
    """设计 §7「金额精度」：前两组通过，第三组 AMOUNT_MISMATCH。"""
    world = payment_world
    payment = world.payment(world.customer(), amount=expected)
    world.serve(payment, GatewayState.PAID, amount_total=amount_total)

    result = world.confirm(payment)

    assert result.reason == reason
    if reason is None:
        assert (result.new_status, result.posted) == (PaymentStatus.PAID, True)
        [topup] = world.topups(payment)
        assert topup.amount == Decimal(expected)
    else:
        assert (result.new_status, result.posted) == (PaymentStatus.REVIEW_REQUIRED, False)
        assert world.topups() == []
    assert world.row(payment).confirmed_amount == Decimal(amount_total) / 100


# --- 手续费 -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("fee", "fee_currency", "recorded", "warned"),
    [
        (290, "myr", Decimal("2.90"), False),
        (290, "MYR", Decimal("2.90"), False),
        (None, None, None, False),
        (290, "usd", None, True),
    ],
    ids=["myr", "upper-case", "not-yet-available", "not-myr"],
)
def test_the_gateway_fee_is_recorded_only_in_myr(
    payment_world: World,
    caplog: pytest.LogCaptureFixture,
    fee: int | None,
    fee_currency: str | None,
    recorded: Decimal | None,
    warned: bool,
) -> None:
    """拿得到且币种为 MYR 时 `fee_minor / 100`；拿不到为空、不记日志；不是 MYR 为空并记 WARNING。
    手续费不影响入账额（spec §7、§44）。"""
    world = payment_world
    payment = world.payment(world.customer())
    world.serve(payment, GatewayState.PAID, fee=fee, fee_currency=fee_currency)

    with caplog.at_level(logging.INFO, logger=LOGGER):
        result = world.confirm(payment)

    assert (result.new_status, result.posted) == (PaymentStatus.PAID, True)
    assert world.row(payment).gateway_fee == recorded
    [topup] = world.topups(payment)
    assert topup.amount == Decimal("100.00")
    fee_warnings = [
        record
        for record in caplog.records
        if record.name == LOGGER
        and record.levelno == logging.WARNING
        and "fee" in record.getMessage().lower()
    ]
    assert len(fee_warnings) == int(warned)
    for record in caplog.records:
        # 日志里没有金额、密钥与会话 id 之外的 Stripe 原文（设计 §6）。
        assert SECRET_KEY not in record.getMessage()


# --- 第二张表：其他 state × 当前状态 ------------------------------------------------------


@pytest.mark.parametrize("current", OPEN_STATUSES, ids=lambda s: s.value)
def test_a_paid_session_credits_any_unfinished_payment(
    payment_world: World, current: PaymentStatus
) -> None:
    """ADR-0007：过期或失败后到账照样入账；进入 PAID 时清空原来的原因。"""
    world = payment_world
    payment = world.payment(world.customer(), status=current, reason=PRIOR_REASON[current])
    world.serve(payment, GatewayState.PAID)

    result = world.confirm(payment)

    assert (result.old_status, result.new_status) == (current, PaymentStatus.PAID)
    assert (result.reason, result.posted) == (None, True)
    assert world.row(payment).status_reason is None
    assert len(world.topups(payment)) == 1
    [audit] = world.audits(payment)
    before = _state(audit.before_state)
    assert (before["status"], before["status_reason"]) == (current.value, PRIOR_REASON[current])


@pytest.mark.parametrize(
    ("state", "status", "reason"),
    [
        (GatewayState.FAILED, PaymentStatus.FAILED, GATEWAY_FAILED),
        (GatewayState.EXPIRED, PaymentStatus.EXPIRED, GATEWAY_EXPIRED),
    ],
    ids=["failed", "expired"],
)
def test_a_pending_payment_follows_a_failed_or_expired_session(
    payment_world: World, state: GatewayState, status: PaymentStatus, reason: str
) -> None:
    world = payment_world
    payment = world.payment(world.customer())
    world.serve(payment, state)

    result = world.confirm(payment)

    assert (result.old_status, result.new_status) == (PaymentStatus.PENDING, status)
    assert (result.reason, result.posted, result.state) == (reason, False, state)
    row = world.row(payment)
    assert (row.status, row.status_reason) == (status, reason)
    assert (row.last_reconciled_at, row.updated_at) == (NOW, NOW)
    assert row.review_required_at is None and row.paid_at is None
    assert world.topups() == []
    [audit] = world.audits(payment)
    assert _state(audit.before_state)["status"] == "PENDING"
    after = _state(audit.after_state)
    assert (after["status"], after["status_reason"]) == (status.value, reason)


@pytest.mark.parametrize(
    "state", [GatewayState.FAILED, GatewayState.EXPIRED], ids=lambda s: s.value
)
@pytest.mark.parametrize(
    "current",
    [PaymentStatus.EXPIRED, PaymentStatus.FAILED, PaymentStatus.CANCELLED],
    ids=lambda s: s.value,
)
def test_failed_or_expired_does_not_move_an_already_ended_payment(
    payment_world: World, current: PaymentStatus, state: GatewayState
) -> None:
    """EXPIRED / FAILED / CANCELLED 收到 FAILED 或 EXPIRED：不变，只更新 last_reconciled_at。"""
    _assert_unchanged(payment_world, current, state)


@pytest.mark.parametrize("current", OPEN_STATUSES, ids=lambda s: s.value)
def test_an_inconsistent_session_sends_any_unfinished_payment_to_review(
    payment_world: World, current: PaymentStatus
) -> None:
    world = payment_world
    payment = world.payment(world.customer(), status=current, reason=PRIOR_REASON[current])
    body = world.serve(payment, GatewayState.INCONSISTENT)

    result = world.confirm(payment)

    assert (result.old_status, result.new_status) == (current, PaymentStatus.REVIEW_REQUIRED)
    assert (result.reason, result.posted) == (GATEWAY_INCONSISTENT, False)
    row = world.row(payment)
    assert row.review_required_at == NOW
    assert (row.confirmed_amount, row.confirmed_currency) == (Decimal("100.00"), "MYR")
    assert row.gateway_payment_intent_id == body["payment_intent"]["id"]
    assert world.topups() == []
    assert len(world.audits(payment)) == 1


@pytest.mark.parametrize("state", [GatewayState.OPEN, GatewayState.UNKNOWN], ids=lambda s: s.value)
@pytest.mark.parametrize("current", OPEN_STATUSES, ids=lambda s: s.value)
def test_open_or_unknown_changes_nothing(
    payment_world: World, current: PaymentStatus, state: GatewayState
) -> None:
    _assert_unchanged(payment_world, current, state)


@pytest.mark.parametrize("state", list(GatewayState), ids=lambda s: s.value)
@pytest.mark.parametrize("current", FINAL_STATUSES, ids=lambda s: s.value)
def test_paid_and_review_required_are_never_changed(
    payment_world: World, current: PaymentStatus, state: GatewayState
) -> None:
    """第 3 步：已是 PAID 或 REVIEW_REQUIRED 只更新 last_reconciled_at.

    REVIEW_REQUIRED 永不自动入账。
    """
    _assert_unchanged(payment_world, current, state)


def _assert_unchanged(world: World, current: PaymentStatus, state: GatewayState) -> None:
    payment = world.payment(world.customer(), status=current, reason=PRIOR_REASON[current])
    world.serve(payment, state)
    before = world.snapshot(payment)

    result = world.confirm(payment)

    assert (result.old_status, result.new_status) == (current, current)
    assert (result.reason, result.posted, result.state) == (PRIOR_REASON[current], False, state)
    after = world.snapshot(payment)
    changed = {
        name
        for name, value in after["payment"].items()  # type: ignore[union-attr]
        if before["payment"][name] != value  # type: ignore[index]
    }
    assert changed == {"last_reconciled_at"}
    assert world.row(payment).last_reconciled_at == NOW
    assert {k: v for k, v in after.items() if k != "payment"} == {
        k: v for k, v in before.items() if k != "payment"
    }
    assert world.audits(payment) == []


# --- 第 1、2 步：无可查询、网关错误、未知支付 ---------------------------------------------


def test_a_payment_without_a_gateway_id_has_nothing_to_query(payment_world: World) -> None:
    world = payment_world
    payment = world.payment(world.customer(), with_session=False)
    before = world.snapshot(payment)

    result = world.confirm(payment)

    assert result.state is None and result.nothing_to_query
    assert (result.old_status, result.new_status, result.posted) == (
        PaymentStatus.PENDING,
        PaymentStatus.PENDING,
        False,
    )
    assert world.stripe.asked == []
    assert world.snapshot(payment) == before


@pytest.mark.parametrize(
    ("status_code", "error"),
    [(503, GatewayUnavailable), (429, GatewayUnavailable), (404, GatewayRequestRejected)],
    ids=["503", "429", "404"],
)
def test_gateway_errors_reach_the_caller_and_nothing_is_written(
    payment_world: World, status_code: int, error: type[Exception]
) -> None:
    world = payment_world
    payment = world.payment(world.customer())
    world.serve(payment, GatewayState.PAID)
    world.stripe.status_code = status_code
    before = world.snapshot(payment)

    with pytest.raises(error):
        world.confirm(payment)

    assert world.snapshot(payment) == before


def test_an_unknown_payment_is_not_found(payment_world: World) -> None:
    world = payment_world
    with pytest.raises(PaymentNotFound):
        confirm_payment(world.factory, world.stripe.gateway(), str(uuid.uuid4()), now=NOW)
    assert world.stripe.asked == []


# --- 复核记录、过期后到账、同一网关支付号 ----------------------------------------------------


def test_an_amount_mismatch_keeps_what_stripe_reported_for_review(payment_world: World) -> None:
    """设计 §7「复核记录」第一组。"""
    world = payment_world
    payment = world.payment(world.customer())
    body = world.serve(payment, GatewayState.PAID, amount_total=9999)

    result = world.confirm(payment)

    assert (result.new_status, result.reason) == (PaymentStatus.REVIEW_REQUIRED, AMOUNT_MISMATCH)
    row = world.row(payment)
    assert row.review_required_at == NOW
    assert (row.confirmed_amount, row.confirmed_currency) == (Decimal("99.99"), "MYR")
    assert row.gateway_payment_intent_id == body["payment_intent"]["id"]
    [audit] = world.audits(payment)
    assert _money(_state(audit.after_state)["confirmed_amount"]) == Decimal("99.99")


def test_a_held_pi_with_the_wrong_currency_is_reviewed_without_writing_the_pi(
    payment_world: World,
) -> None:
    """设计 §7「复核记录」第二组：原因 CURRENCY_MISMATCH、不写 PI id、不撞唯一约束、不进入重试。"""
    world = payment_world
    intent_id = f"pi_test_{uuid.uuid4().hex}"
    holder = world.payment(world.customer(), status=PaymentStatus.PAID, intent_id=intent_id)
    payment = world.payment(world.customer())
    world.serve(payment, GatewayState.PAID, intent_id=intent_id, currency="usd")

    result = world.confirm(payment)  # 不是 ConfirmConflict

    assert (result.new_status, result.reason) == (PaymentStatus.REVIEW_REQUIRED, CURRENCY_MISMATCH)
    row = world.row(payment)
    assert row.gateway_payment_intent_id is None
    assert (row.confirmed_amount, row.confirmed_currency) == (Decimal("100.00"), "USD")
    assert row.review_required_at == NOW
    assert world.row(holder).gateway_payment_intent_id == intent_id
    assert world.topups() == []


def test_a_review_without_amount_or_currency_from_stripe_stores_nulls(payment_world: World) -> None:
    world = payment_world
    payment = world.payment(world.customer())
    world.serve(payment, GatewayState.INCONSISTENT, amount_total=None, currency=None)

    result = world.confirm(payment)

    assert (result.new_status, result.reason) == (
        PaymentStatus.REVIEW_REQUIRED,
        GATEWAY_INCONSISTENT,
    )
    row = world.row(payment)
    assert (row.confirmed_amount, row.confirmed_currency) == (None, None)
    assert row.review_required_at == NOW


def test_money_arriving_after_expiry_is_credited_and_clears_the_reason(
    payment_world: World,
) -> None:
    """设计 §7「过期后到账清原因」：EXPIRED（GATEWAY_EXPIRED）之后收到 PAID → PAID、原因为空。"""
    world = payment_world
    payment = world.payment(world.customer())
    world.serve(payment, GatewayState.EXPIRED)
    expired = world.confirm(payment)
    assert (expired.new_status, expired.reason) == (PaymentStatus.EXPIRED, GATEWAY_EXPIRED)

    world.serve(payment, GatewayState.PAID)
    paid = world.confirm(payment, now=LATER)

    assert (paid.old_status, paid.new_status, paid.reason, paid.posted) == (
        PaymentStatus.EXPIRED,
        PaymentStatus.PAID,
        None,
        True,
    )
    row = world.row(payment)
    assert (row.status_reason, row.paid_at) == (None, LATER)
    assert len(world.topups(payment)) == 1
    first, second = world.audits(payment)
    assert _state(first.after_state)["status"] == "EXPIRED"
    assert _state(second.before_state)["status_reason"] == GATEWAY_EXPIRED
    assert _state(second.after_state)["status_reason"] is None


def test_a_second_confirmation_of_a_paid_payment_posts_nothing(payment_world: World) -> None:
    """幂等的第三层：重领、重复事件、补对账再确认一次都是空操作。"""
    world = payment_world
    payment = world.payment(world.customer())
    world.serve(payment, GatewayState.PAID)
    world.confirm(payment)

    again = world.confirm(payment, now=LATER)

    assert (again.old_status, again.new_status, again.posted) == (
        PaymentStatus.PAID,
        PaymentStatus.PAID,
        False,
    )
    assert len(world.topups()) == 1
    assert len(world.audits(payment)) == 1
    row = world.row(payment)
    assert (row.paid_at, row.last_reconciled_at) == (NOW, LATER)


def test_one_gateway_payment_for_two_internal_payments_credits_only_the_first(
    payment_world: World,
) -> None:
    """设计 §7「同一网关支付号两笔内部支付」.

    第二笔 REVIEW_REQUIRED（GATEWAY_PAYMENT_REUSED），不入账。
    """
    world = payment_world
    intent_id = f"pi_test_{uuid.uuid4().hex}"
    first = world.payment(world.customer())
    second = world.payment(world.customer())
    world.serve(first, GatewayState.PAID, intent_id=intent_id)
    world.serve(second, GatewayState.PAID, intent_id=intent_id)

    assert world.confirm(first).posted is True
    result = world.confirm(second)

    assert (result.new_status, result.reason, result.posted) == (
        PaymentStatus.REVIEW_REQUIRED,
        GATEWAY_PAYMENT_REUSED,
        False,
    )
    assert world.row(second).gateway_payment_intent_id is None
    assert [topup.reference_id for topup in world.topups()] == [first.public_id]


# --- 事务中途失败与撞唯一约束（INV-13） -----------------------------------------------------


def test_a_failure_while_posting_leaves_the_payment_and_the_ledger_untouched(
    payment_world: World, monkeypatch
) -> None:
    """设计 §7「事务中途失败」：入账时抛错 → 支付仍是原状态、无账本行、无审计。"""
    world = payment_world
    payment = world.payment(world.customer())
    world.serve(payment, GatewayState.PAID)
    before = world.snapshot(payment)
    calls: list[str] = []

    def broken_post(session: Session, **options: object):
        calls.append(str(options["reference_id"]))
        raise RuntimeError("injected ledger failure")

    monkeypatch.setattr(payments_service, "post_transaction", broken_post)

    with pytest.raises(RuntimeError, match="injected ledger failure"):
        world.confirm(payment)

    assert calls == [payment.public_id]
    assert world.snapshot(payment) == before
    assert world.row(payment).status is PaymentStatus.PENDING


def test_a_failure_after_posting_rolls_the_topup_back_too(
    payment_world: World, monkeypatch
) -> None:
    """账本行已 flush、审计写入时失败：账本行随整个事务回滚。"""
    world = payment_world
    payment = world.payment(world.customer())
    world.serve(payment, GatewayState.PAID)
    before = world.snapshot(payment)

    def broken_audit(*args: object, **options: object) -> None:
        raise RuntimeError("injected audit failure")

    monkeypatch.setattr(payments_service, "_audit", broken_audit)

    with pytest.raises(RuntimeError, match="injected audit failure"):
        world.confirm(payment)

    assert world.snapshot(payment) == before


def test_a_pi_taken_after_the_check_is_a_retryable_conflict(
    payment_world: World, monkeypatch
) -> None:
    """并发者在占用查询之后才占了同一个 PI：写入撞唯一约束 → 整体回滚、ConfirmConflict；
    下一次判定查到占用而进入 REVIEW_REQUIRED（设计 §2）。"""
    world = payment_world
    intent_id = f"pi_test_{uuid.uuid4().hex}"
    world.payment(world.customer(), status=PaymentStatus.PAID, intent_id=intent_id)
    payment = world.payment(world.customer())
    world.serve(payment, GatewayState.PAID, intent_id=intent_id)
    before = world.snapshot(payment)
    real = payments_service._payment_intent_taken
    monkeypatch.setattr(payments_service, "_payment_intent_taken", lambda *args: False)

    with pytest.raises(ConfirmConflict):
        world.confirm(payment)

    assert world.snapshot(payment) == before

    monkeypatch.setattr(payments_service, "_payment_intent_taken", real)
    retried = world.confirm(payment, now=LATER)
    assert (retried.new_status, retried.reason) == (
        PaymentStatus.REVIEW_REQUIRED,
        GATEWAY_PAYMENT_REUSED,
    )
    assert world.topups() == []


def test_payments_audits_are_public_ids() -> None:
    """`entity_id` 是 `payments.public_id`：审计查询原样返回、可按它筛选。"""
    assert ENTITY_PAYMENT in PUBLIC_ENTITY_TYPES


# --- 真 MySQL：入账走账本与复机 ------------------------------------------------------------


def test_a_suspended_tenant_in_debt_is_credited_through_the_ledger_and_resumed(
    mysql_payment_world: World,
) -> None:
    """设计 §7「入账走账本、复机」：余额 -5 的暂停租户付 100 → 一条 TOPUP、余额 95、ACTIVE、
    复机 outbox 与跃迁审计（都由 `post_transaction` 在同一事务里写）。"""
    world = mysql_payment_world
    tenant = world.customer()
    with world.factory() as session:
        post_transaction(
            session,
            tenant_id=tenant.id,
            transaction_type=TransactionType.ADJUSTMENT_DEBIT,
            amount=Decimal("-5"),
            reference_type=ReferenceType.ADMIN_ADJUSTMENT,
            reference_id=str(uuid.uuid4()),
            now=CREATED,
            created_by=world.admin_id,
            description="Fictional debit before a top-up",
            actor_role="ADMIN",
        )
        session.commit()
    assert world.wallet(tenant) == (Decimal("-5"), 1, BillingStatus.SUSPENDED, 0)
    payment = world.payment(tenant)
    world.serve(payment, GatewayState.PAID, fee=290)

    result = world.confirm(payment)

    assert (result.new_status, result.posted) == (PaymentStatus.PAID, True)
    [topup] = world.topups()
    assert (topup.balance_before, topup.amount, topup.balance_after) == (
        Decimal("-5"),
        Decimal("100"),
        Decimal("95"),
    )
    assert topup.wallet_sequence == 2
    assert world.wallet(tenant) == (Decimal("95"), 2, BillingStatus.ACTIVE, 1)
    with world.factory() as session:
        assert verify_wallet(session, tenant.id) == []
        transitions = list(
            session.execute(
                select(AuditLog)
                .where(AuditLog.action == AuditAction.TENANT_BILLING_STATUS_CHANGED)
                .where(AuditLog.entity_id == tenant.public_id)
            ).scalars()
        )
        events = list(
            session.execute(
                select(DomainOutbox)
                .where(DomainOutbox.event_type == EVENT_BILLING_STATUS_CHANGED)
                .where(DomainOutbox.aggregate_id == tenant.public_id)
            ).scalars()
        )
    [transition] = transitions
    assert transition.reason == REASON_BALANCE_POSITIVE
    assert transition.created_at == NOW
    [outbox] = events
    payload = json.loads(outbox.payload_json or "{}")
    assert (payload["billing_status"], payload["balance"]) == ("ACTIVE", "95.00000000")
    assert payload["wallet_transaction"] == topup.public_id
    # 手续费不影响入账额。
    assert world.row(payment).gateway_fee == Decimal("2.90")


# --- 真 MySQL：多事件并发只入账一次 ---------------------------------------------------------


def test_two_events_confirming_one_payment_at_once_post_once(mysql_payment_world: World) -> None:
    """设计 §7「多事件并发」：同一支付的两个事件由两个线程同时确认 → 只有一条 TOPUP。"""
    world = mysql_payment_world
    tenant = world.customer()
    payment = world.payment(tenant)
    world.serve(payment, GatewayState.PAID)
    start = threading.Barrier(2)

    def attempt(_index: int) -> ConfirmResult:
        gateway = world.stripe.gateway()
        start.wait(timeout=30)
        return confirm_payment(world.factory, gateway, payment.public_id, now=NOW)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, range(2)))

    assert sorted(result.posted for result in results) == [False, True]
    assert {result.new_status for result in results} == {PaymentStatus.PAID}
    assert len(world.topups()) == 1
    assert len(world.audits(payment)) == 1
    assert world.wallet(tenant) == (Decimal("100"), 1, BillingStatus.ACTIVE, 1)


# --- 真 MySQL：关户与入账互斥 ---------------------------------------------------------------

# 暂停点最多停这么久；「对方在等行锁」的轮询间隔与上限
# （照 test_session_revocation_concurrency.py）。
GATE_TIMEOUT_SECONDS = 15.0
LOCK_POLL_INTERVAL_SECONDS = 0.05
LOCK_POLL_LIMIT_SECONDS = 10.0
LOCK_WAIT_TIMEOUT_SECONDS = 20
JOIN_TIMEOUT_SECONDS = 40.0

# ⚠️ 查 performance_schema.data_lock_waits，不查 INNODB_TRX（CLAUDE.md「写后端测试的约定」）。
_WAITING_ON_TABLE = text(
    "SELECT COUNT(*) FROM performance_schema.data_lock_waits AS w"
    " JOIN performance_schema.threads AS t ON t.THREAD_ID = w.REQUESTING_THREAD_ID"
    " JOIN performance_schema.data_locks AS l"
    " ON l.ENGINE = w.ENGINE AND l.ENGINE_LOCK_ID = w.REQUESTING_ENGINE_LOCK_ID"
    " WHERE t.PROCESSLIST_ID = :connection_id"
    " AND l.OBJECT_SCHEMA = DATABASE() AND l.OBJECT_NAME = :table_name"
)


def _sql(statement: str) -> str:
    return " ".join(statement.replace("`", "").lower().split())


def _locks(table: str) -> Callable[[str], bool]:
    """`SELECT … FROM <table> … FOR UPDATE`.

    整个用例只在 MySQL 上跑，匹配的是 MySQL 编译出的文本。
    """

    def matches(statement: str) -> bool:
        sql = _sql(statement)
        return (
            sql.startswith("select ") and f" from {table} " in sql and sql.endswith(" for update")
        )

    return matches


class Gate:
    """The first statement on `engine` matching `matches` waits here until `release()`."""

    def __init__(self, engine: Engine, matches: Callable[[str], bool], *, after: bool) -> None:
        self.reached = threading.Event()
        self.released = threading.Event()
        self._fired = False
        self._lock = threading.Lock()

        def listener(conn, cursor, statement, parameters, context, executemany) -> None:
            if not matches(statement):
                return
            with self._lock:
                if self._fired:
                    return
                self._fired = True
            self.reached.set()
            if not self.released.wait(GATE_TIMEOUT_SECONDS):
                raise AssertionError("nobody released the gate")

        event.listen(engine, "after_cursor_execute" if after else "before_cursor_execute", listener)

    def wait_reached(self) -> None:
        if not self.reached.wait(GATE_TIMEOUT_SECONDS):
            pytest.fail(f"the gate was not reached within {GATE_TIMEOUT_SECONDS}s")

    def release(self) -> None:
        self.released.set()


def _limit_lock_waits(dbapi_connection: Any, _record: Any) -> None:
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute(f"SET SESSION innodb_lock_wait_timeout = {LOCK_WAIT_TIMEOUT_SECONDS}")
    finally:
        cursor.close()
    dbapi_connection.rollback()


@pytest.fixture
def engines() -> Iterator[Callable[..., Engine]]:
    """Engines of their own, one per side of a race, so a gate only stops that side."""
    url = _mysql_url()
    made: list[Engine] = []

    def make(*, observer: bool = False) -> Engine:
        if observer:
            engine = create_engine(url, isolation_level="AUTOCOMMIT", pool_size=1)
        else:
            engine = create_engine(url, pool_size=2, max_overflow=2)
            event.listen(engine, "connect", _limit_lock_waits)
        made.append(engine)
        return engine

    try:
        yield make
    finally:
        for engine in made:
            engine.dispose()


def _close_tenant(engine: Engine, tenant_id: int, connection_ids: list[int]) -> None:
    """Close an account the way an account change does: lock the tenant row only, then write."""
    with engine.begin() as connection:
        connection_ids.append(int(connection.execute(text("SELECT CONNECTION_ID()")).scalar_one()))
        connection.execute(select(Tenant.id).where(Tenant.id == tenant_id).with_for_update())
        connection.execute(
            update(Tenant)
            .where(Tenant.id == tenant_id)
            .values(account_status=AccountStatus.CLOSED, updated_at=LATER)
        )


def _wait_until_waiting(observer: Engine, connection_ids: list[int], table: str) -> None:
    deadline = time.monotonic() + LOCK_POLL_LIMIT_SECONDS
    with observer.connect() as connection:
        while time.monotonic() < deadline:
            if connection_ids:
                waiting = connection.execute(
                    _WAITING_ON_TABLE, {"connection_id": connection_ids[-1], "table_name": table}
                ).scalar_one()
                if waiting:
                    return
            time.sleep(LOCK_POLL_INTERVAL_SECONDS)
    pytest.fail(f"the account change was not seen waiting for a {table} row lock")


def test_closing_waits_for_a_confirmation_holding_the_tenant_lock(
    mysql_payment_world: World, engines: Callable[..., Engine]
) -> None:
    """设计 §7「关户与入账互斥」：确认持锁期间另一个连接关户 → 关户等入账提交，入账照常。"""
    world = mysql_payment_world
    tenant = world.customer()
    payment = world.payment(tenant)
    world.serve(payment, GatewayState.PAID)
    confirm_engine, close_engine = engines(), engines()
    observer = engines(observer=True)
    gate = Gate(confirm_engine, _locks("tenants"), after=True)
    connection_ids: list[int] = []
    gateway = world.stripe.gateway()

    with ThreadPoolExecutor(max_workers=2) as pool:
        try:
            confirming = pool.submit(
                confirm_payment,
                create_session_factory(confirm_engine),
                gateway,
                payment.public_id,
                now=NOW,
            )
            gate.wait_reached()
            closing = pool.submit(_close_tenant, close_engine, tenant.id, connection_ids)
            _wait_until_waiting(observer, connection_ids, "tenants")
        finally:
            gate.release()
        result = confirming.result(timeout=JOIN_TIMEOUT_SECONDS)
        closing.result(timeout=JOIN_TIMEOUT_SECONDS)

    assert (result.new_status, result.reason, result.posted) == (PaymentStatus.PAID, None, True)
    assert len(world.topups(payment)) == 1
    with world.factory() as session:
        row = session.get(Tenant, tenant.id)
        assert row is not None and row.account_status is AccountStatus.CLOSED


def test_a_confirmation_after_the_close_commits_sends_the_payment_to_review(
    mysql_payment_world: World, engines: Callable[..., Engine]
) -> None:
    """另一种先后：确认查完 Stripe、锁支付行之前停住.

    关户提交；放行 → 读到 CLOSED，TENANT_CLOSED。
    """
    world = mysql_payment_world
    tenant = world.customer()
    payment = world.payment(tenant)
    world.serve(payment, GatewayState.PAID)
    confirm_engine, close_engine = engines(), engines()
    gate = Gate(confirm_engine, _locks("payments"), after=False)
    gateway = world.stripe.gateway()

    with ThreadPoolExecutor(max_workers=1) as pool:
        try:
            confirming = pool.submit(
                confirm_payment,
                create_session_factory(confirm_engine),
                gateway,
                payment.public_id,
                now=NOW,
            )
            gate.wait_reached()
            # 这时 Stripe 已查过（第 2 步在事务外）。
            assert world.stripe.asked == [payment.session_id]
            _close_tenant(close_engine, tenant.id, [])
        finally:
            gate.release()
        result = confirming.result(timeout=JOIN_TIMEOUT_SECONDS)

    assert (result.new_status, result.reason, result.posted) == (
        PaymentStatus.REVIEW_REQUIRED,
        TENANT_CLOSED,
        False,
    )
    assert world.topups() == []
    assert world.row(payment).review_required_at == NOW

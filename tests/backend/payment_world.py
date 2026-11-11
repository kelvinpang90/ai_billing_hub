"""The payment world shared by the payment tests: tenants, payments, a fake Stripe, webhook events.

不以 `test_` 开头，pytest 不会把它当用例收集。用例模块按名字导入这里的构件，各自用
`build_sqlite_world` / `build_mysql_world` 写自己的夹具；跨文件共用的夹具在 conftest.py 注册。
库地址在夹具里读（`mysql_url`），不在模块顶层读：xdist 换库名（conftest 的 `pytest_configure`）
之后才有最终值。

Stripe 一律是注入了假传输（`httpx2.MockTransport`）的 `StripeGateway`，不联网；地址是虚构的。
MySQL 那部分需要 `BILLING_TEST_DATABASE_URL` 指向一个**可以被清空的**库，没设时 skip ——
**不要把 skipped 读成 passed**，CI 设了它并把 skipped 判成失败。
"""

from __future__ import annotations

import datetime as dt
import json
import os
import threading
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import httpx2
import pytest
from alembic.config import Config
from sqlalchemy import Connection, Engine, create_engine, delete, func, insert, select, text, update
from sqlalchemy.orm import Session, sessionmaker

from alembic import command
from app.core.config import Settings
from app.core.database import create_session_factory
from app.models.auth import AuditAction, AuditLog, DomainOutbox, User, UserRole, UserStatus
from app.models.base import Base
from app.models.payment_settings import PAYMENT_SETTINGS_ID, PaymentSettings
from app.models.payments import (
    Payment,
    PaymentGateway,
    PaymentGatewayEvent,
    PaymentResolution,
    PaymentStatus,
)
from app.models.tax_policy import TaxJurisdiction, TaxPolicyStatus, TaxPolicyType, TaxPolicyVersion
from app.models.tenancy import AccountStatus, BillingMode, BillingStatus, Project, Tenant
from app.models.wallet import TransactionType, Wallet, WalletTransaction
from app.payments.base import GatewayState
from app.payments.stripe_gateway import StripeGateway
from app.repositories import tenancy
from app.repositories.wallet import create_wallet, get_wallet_for_tenant
from app.services.payment_events import RecordOutcome, record_event
from app.services.payments import ConfirmResult, confirm_payment
from app.tasks import payment_events as events_task

TEST_EMAIL_DOMAIN = "@payment-confirm-test.example.com"
# 虚构的地址：这些用例不会连到任何主机。
API_BASE_URL = "https://stripe-api.example.test"
SECRET_KEY = "sk_test_payment_confirm_secret_value"
WEBHOOK_SECRET = "whsec_payment_confirm_signing_value"

# 固定值而不是 utc_now()：断言「存进去的就是调用方给的那个时刻」要能逐字比对。
CREATED = dt.datetime(2026, 11, 8, 8, 0, 0)
EXPIRES = dt.datetime(2026, 11, 8, 9, 0, 0)
NOW = dt.datetime(2026, 11, 8, 8, 20, 0)

AUDIT_FIELDS = {"status", "status_reason", "expected_amount", "confirmed_amount", "gateway_fee"}

CHECKOUT_COMPLETED = "checkout.session.completed"

# 迁移 0038 的种子行（设计闸门 #587 v3 §2「数据库」），`updated_at` 除外（迁移写迁移时刻）。
# `create_all` 建的库没有这一行：建单之前用 `seed_payment_settings` 写入。
SEED_PAYMENT_SETTINGS: dict[str, Any] = {
    "min_amount": Decimal("10.00"),
    "max_amount": None,
    "recovery_buffer": Decimal("10.00"),
    "preset_amounts": ["50.00", "100.00", "300.00", "500.00", "1000.00"],
    "reuse_window_seconds": 60,
    "pending_timeout_seconds": 3600,
    "version": 1,
    "updated_by_user_id": None,
}
SEED_SETTINGS_AT = dt.datetime(2026, 11, 8, 0, 0, 0)


# --- 支付设置 -------------------------------------------------------------------------


def _settings_row(overrides: dict[str, Any]) -> dict[str, Any]:
    unknown = set(overrides) - set(SEED_PAYMENT_SETTINGS)
    assert not unknown, f"not a payment settings column: {sorted(unknown)}"
    return {
        "id": PAYMENT_SETTINGS_ID,
        **SEED_PAYMENT_SETTINGS,
        **overrides,
        "updated_at": SEED_SETTINGS_AT,
    }


def reset_payment_settings(connection: Connection, **overrides: Any) -> None:
    """Replace the one `payment_settings` row with the seed values (and `overrides`)."""
    connection.execute(delete(PaymentSettings))
    connection.execute(insert(PaymentSettings).values(**_settings_row(overrides)))


def seed_payment_settings(factory: sessionmaker[Session], **overrides: Any) -> None:
    """Write the seed row of migration 0038, with `overrides` for some columns.

    SQLite（`create_all`）上本来没有这一行；MySQL 上迁移已写过，这里换成同样的值，所以用例之间
    不会互相带着改过的设置。直接写表，不经 `update_payment_settings`：不写审计、不加版本。
    """
    with factory() as session:
        reset_payment_settings(session.connection(), **overrides)
        session.commit()


def remove_payment_settings(factory: sessionmaker[Session]) -> None:
    """The row is gone, as before migration 0038 ran."""
    with factory() as session:
        session.execute(delete(PaymentSettings))
        session.commit()


@pytest.fixture
def payment_settings_seeder() -> Callable[..., None]:
    """`seed_payment_settings`: call it with the world's session factory before any top-up.

    conftest.py 注册它；世界（与它的会话工厂）由各用例模块自己的夹具建。
    """
    return seed_payment_settings


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
            "payment": payment_columns(row),
            "ledger": ledger,
            "audits": audits,
            "outbox": outbox,
        }


def payment_columns(row: Payment) -> dict[str, object]:
    return {column.key: getattr(row, column.key) for column in Payment.__table__.columns}


def audit_state(text_value: str | None) -> dict[str, Any]:
    return json.loads(text_value or "{}")


def close_by_review(world: World, payment: PaymentRef) -> None:
    """What the manual CLOSE (AIH-TASK-182) leaves: `FAILED`, the review reason kept, resolved."""
    with world.factory() as session:
        session.execute(
            update(Payment)
            .where(Payment.public_id == payment.public_id)
            .values(
                status=PaymentStatus.FAILED,
                resolution=PaymentResolution.CLOSED,
                resolved_at=CREATED,
                resolved_by_user_id=world.admin_id,
                resolution_reason="Fictional review closed without credit",
            )
        )
        session.commit()


# --- 建世界 ---------------------------------------------------------------------------


def build_sqlite_world() -> Iterator[World]:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    try:
        yield _populate(create_session_factory(engine))
    finally:
        engine.dispose()


def mysql_url() -> str:
    # 环境变量在夹具里读：xdist 换库名（conftest 的 pytest_configure）之后才有最终值。
    url = os.environ.get("BILLING_TEST_DATABASE_URL", "")
    if not url:
        pytest.skip("BILLING_TEST_DATABASE_URL is not set; the MySQL half needs a real MySQL")
    return url


def build_mysql_world(monkeypatch: pytest.MonkeyPatch) -> Iterator[World]:
    url = mysql_url()
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
    支付与事件行在用例后删掉：告警用例与支付用例共用这个库（设计 §2「告警」）。
    支付设置换回种子行：改过设置的测试管理员（外键 RESTRICT）才删得掉，下一个用例也不带着改过的值。
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
        reset_payment_settings(connection)
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


# --- 回调事件 -------------------------------------------------------------------------


def checkout_event_body(payment: PaymentRef, event_type: str = CHECKOUT_COMPLETED) -> bytes:
    """A Stripe event body around `payment`'s Checkout Session (carrying its session id)."""
    session: dict[str, Any] = {
        "id": payment.session_id,
        "object": "checkout.session",
        "client_reference_id": payment.public_id,
        "payment_status": "paid",
        "status": "complete",
        "amount_total": 10000,
        "currency": "myr",
        "payment_intent": f"pi_test_{uuid.uuid4().hex}",
        "metadata": {"payment_id": payment.public_id},
    }
    event = {
        "id": f"evt_test_{uuid.uuid4().hex}",
        "object": "event",
        "type": event_type,
        "livemode": False,
        "created": 1762588800,
        "data": {"object": session},
    }
    return json.dumps(event, sort_keys=True).encode("utf-8")


def record_payment_event(
    world: World,
    payment: PaymentRef,
    *,
    received: dt.datetime,
    event_type: str = CHECKOUT_COMPLETED,
) -> bytes:
    """Record an event for `payment`, received (and so due) at `received`; return its body."""
    body = checkout_event_body(payment, event_type)
    parsed = world.stripe.gateway().parse_webhook(body)
    outcome = record_event(world.factory, parsed, body, now=received, livemode=False)
    assert outcome is RecordOutcome.RECORDED
    return body


def event_row(world: World, body: bytes) -> PaymentGatewayEvent:
    gateway_event_id = str(json.loads(body)["id"])
    with world.factory() as session:
        row = session.execute(
            select(PaymentGatewayEvent).where(
                PaymentGatewayEvent.gateway_event_id == gateway_event_id
            )
        ).scalar_one()
        session.expunge(row)
        return row


def wire_event_task(monkeypatch: pytest.MonkeyPatch, world: World) -> None:
    """Point the event processing task at this world's database and fake Stripe."""
    monkeypatch.setattr(events_task, "_session_factory", lambda: world.factory)
    monkeypatch.setattr(events_task, "get_settings", lambda: Settings(payments_enabled=True))
    monkeypatch.setattr(events_task, "get_payment_gateway", lambda settings: world.stripe.gateway())

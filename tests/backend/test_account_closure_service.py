"""The account closure service (design gate #707 v4 §2「契约三」, §7 rows of AIH-TASK-212).

覆盖 §7 标 AIH-TASK-212 的非并发各行（并发三行在 test_account_closure_concurrency.py）：

- **关户正常路径**（SQLite 与真 MySQL）：`CLOSED`、`status_version` +1、
  `status_changed_at = now`、一行 `tenant.account_status_changed` outbox（payload 键与
  `change_account_status` 相同）、投递行 `next_attempt_at` 提前到 `now`、一行审计（`reason`、
  白名单前后状态）；钱包 `version` 不变；
- **前置条件逐条**：每条各自的 409、什么都不写；设计列出的终结状态都不挡；
- **判定顺序**、**网关事件扫描上限**（上限注入为 3）、**已关户**、**跨租户**；
- **事务中途失败**（SQLite 与真 MySQL）：审计写入时注入数据库错误；
- **Redis/Celery 丢失**：不注入任何 Celery 依赖照常成功。

世界沿用 test_usage_billing.py 的 `World`（客户、钱包、项目、集成凭据与投递行都经服务建）；
支付、网关事件与 AI 模块 outbox 行在这里直接写表造。前置条件各行只在 SQLite 上跑。
MySQL 那部分需要 `BILLING_TEST_DATABASE_URL`；没设时 skip —— **不要把 skipped 读成 passed**，
CI 设了它并把 skipped 判成失败。uuid 与金额都是虚构的。
"""

from __future__ import annotations

import datetime as dt
import inspect
import json
import logging
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import delete, event, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker
from test_usage_billing import T0, ZERO_TOKENS, Tenancy, _mysql_factory, _sqlite_factory
from test_usage_billing import World as BillingWorld

from app.ai_api.outbox import AiUsageOutbox, AiUsageOutboxStatus
from app.models.auth import AuditAction, AuditLog, DomainOutbox, OutboxStatus
from app.models.integration import ProjectStatusDelivery
from app.models.payments import (
    Payment,
    PaymentEventProcessingStatus,
    PaymentEventVerificationStatus,
    PaymentGateway,
    PaymentGatewayEvent,
    PaymentStatus,
)
from app.models.tax_policy import TaxJurisdiction, TaxPolicyStatus, TaxPolicyType, TaxPolicyVersion
from app.models.tenancy import AccountStatus, Tenant
from app.models.usage import UsageEvent, UsageEventStatus
from app.models.wallet import Wallet
from app.services import account_closure, account_status
from app.services.account_closure import (
    AccountClosureAiUsagePending,
    AccountClosureBalanceNotZero,
    AccountClosurePaymentEventsPending,
    AccountClosurePaymentsPending,
    AccountClosureUsagePending,
    ClosureBlockers,
    close_account,
    closure_blockers,
    get_closure_check,
)
from app.services.account_status import (
    EVENT_ACCOUNT_STATUS_CHANGED,
    AccountStatusTransitionInvalid,
    change_account_status,
)
from app.services.auth import RequestContext
from app.services.customers import CustomerNotFound

CONTEXT = RequestContext(ip_address="203.0.113.21", user_agent="account-closure-service-test")
REASON = "Closure ticket CL-0001: refunded and settled"

# 关户时刻：晚于世界的 T0（2026-09-29），整秒。
CLOSE_AT = dt.datetime(2026, 10, 6, 9, 0, 0)
LATER = CLOSE_AT + dt.timedelta(hours=1)
SECOND = dt.timedelta(seconds=1)
SETTLE = dt.timedelta(days=account_closure.EXPIRED_PAYMENT_SETTLE_DAYS)

TRANSITION_INVALID = "ACCOUNT_STATUS_TRANSITION_INVALID"
BALANCE_NOT_ZERO = "ACCOUNT_CLOSURE_BALANCE_NOT_ZERO"
USAGE_PENDING = "ACCOUNT_CLOSURE_USAGE_PENDING"
AI_USAGE_PENDING = "ACCOUNT_CLOSURE_AI_USAGE_PENDING"
PAYMENTS_PENDING = "ACCOUNT_CLOSURE_PAYMENTS_PENDING"
PAYMENT_EVENTS_PENDING = "ACCOUNT_CLOSURE_PAYMENT_EVENTS_PENDING"

NOTHING = ClosureBlockers(
    usage_events=0, ai_usage=0, payments=0, payment_events=0, payment_events_truncated=False
)

# 设计 §7 列出的七种非终结用量状态（不由被测代码推导：新增的状态要在这里显式加上）。
UNRESOLVED_USAGE = [
    UsageEventStatus.RECEIVED,
    UsageEventStatus.PROCESSING,
    UsageEventStatus.PRICING_ERROR,
    UsageEventStatus.FX_RATE_ERROR,
    UsageEventStatus.MODEL_UNKNOWN,
    UsageEventStatus.IDEMPOTENCY_CONFLICT,
    UsageEventStatus.FAILED_RETRYABLE,
]
EVENT_FINAL = {
    PaymentEventProcessingStatus.PROCESSED,
    PaymentEventProcessingStatus.IGNORED,
    PaymentEventProcessingStatus.UNMATCHED,
    PaymentEventProcessingStatus.REJECTED,
}


# --- 夹具 -----------------------------------------------------------------------


@pytest.fixture
def sqlite_factory() -> Iterator[sessionmaker[Session]]:
    yield from _sqlite_factory()


@pytest.fixture(params=["sqlite", "mysql"])
def factory(request, monkeypatch) -> Iterator[sessionmaker[Session]]:
    if request.param == "mysql":
        yield from _mysql_factory(monkeypatch)
        return
    yield from _sqlite_factory()


@pytest.fixture
def world(sqlite_factory) -> BillingWorld:
    return BillingWorld(sqlite_factory)


@pytest.fixture
def both(factory) -> BillingWorld:
    """The world on SQLite and on a real MySQL."""
    return BillingWorld(factory)


# --- 造数据 ---------------------------------------------------------------------


@dataclass(frozen=True)
class PaymentRef:
    id: int
    public_id: str
    session_id: str
    intent_id: str


def disabled(world: BillingWorld, name: str = "a") -> Tenancy:
    """A customer (wallet 0, one project and its delivery row), set to DISABLED in the db."""
    tenant = world.tenant(name)
    world.set_account_status(tenant, AccountStatus.DISABLED)
    return tenant


def set_balance(world: BillingWorld, tenant: Tenancy, amount: Decimal) -> None:
    """Directly in the db (SQLite has no wallet triggers): only the closure's reading matters."""
    with world.factory() as session:
        session.execute(update(Wallet).where(Wallet.tenant_id == tenant.id).values(balance=amount))
        session.commit()


def push_delivery(world: BillingWorld, tenant: Tenancy, at: dt.datetime) -> None:
    """Put the tenant's delivery rows' next attempt at `at` (a backoff the bump must cancel)."""
    with world.factory() as session:
        session.execute(
            update(ProjectStatusDelivery)
            .where(ProjectStatusDelivery.tenant_id == tenant.id)
            .values(next_attempt_at=at)
        )
        session.commit()


def add_usage(world: BillingWorld, tenant: Tenancy, status: UsageEventStatus) -> int:
    event_id = world.event(tenant)
    set_usage_status(world, event_id, status)
    return event_id


def set_usage_status(world: BillingWorld, event_id: int, status: UsageEventStatus) -> None:
    values: dict[str, Any] = {"status": status}
    if status is UsageEventStatus.PROCESSING:
        # 认领 CHECK：PROCESSING 时三列认领字段齐全。
        values.update(
            claim_token=str(uuid.uuid4()),
            claimed_at=T0,
            lease_expires_at=T0 + dt.timedelta(minutes=2),
        )
    with world.factory() as session:
        session.execute(update(UsageEvent).where(UsageEvent.id == event_id).values(**values))
        session.commit()


def add_ai_usage(world: BillingWorld, tenant: Tenancy, status: AiUsageOutboxStatus) -> int:
    with world.factory() as session:
        row = AiUsageOutbox(
            event_id=uuid.uuid4().hex[:26].upper(),
            request_id=uuid.uuid4().hex[:26].upper(),
            claim_id=None,
            tenant_id=tenant.id,
            project_id=tenant.project_id,
            feature="chat",
            payload_json="{}",
            status=status,
            attempts=0,
            next_attempt_at=T0,
            last_error=None,
            first_attempted_at=None,
            created_at=T0,
            delivered_at=None,
            resolved_at=None,
            review_required_at=None,
            reviewed_at=None,
            dead_acked_at=None,
        )
        session.add(row)
        session.commit()
        return int(row.id)


def set_ai_usage_status(world: BillingWorld, row_id: int, status: AiUsageOutboxStatus) -> None:
    with world.factory() as session:
        session.execute(
            update(AiUsageOutbox).where(AiUsageOutbox.id == row_id).values(status=status)
        )
        session.commit()


def add_payment(
    world: BillingWorld,
    tenant: Tenancy,
    status: PaymentStatus,
    *,
    created_at: dt.datetime = CLOSE_AT - dt.timedelta(days=1),
) -> PaymentRef:
    """A payment of the tenant with a Checkout Session id and a PaymentIntent id."""
    public_id = str(uuid.uuid4())
    session_id = f"cs_test_{uuid.uuid4().hex}"
    intent_id = f"pi_test_{uuid.uuid4().hex}"
    paid = status is PaymentStatus.PAID
    amount = Decimal("100.00")
    with world.factory() as session:
        # 支付行引用一个税务政策版本（建单时生效的那一版）：这里给一个草稿就够了。
        version = TaxPolicyVersion(
            public_id=str(uuid.uuid4()),
            jurisdiction=TaxJurisdiction.MY,
            policy_type=TaxPolicyType.SST,
            rate=None,
            treatment_json={"schema_version": 1, "sst_registered": False, "tax_charged": False},
            status=TaxPolicyStatus.DRAFT,
            effective_from=None,
            effective_to=None,
            source_reference="Fictional policy for an account closure test",
            created_by=world.admin.id,
            approved_by=None,
            approved_at=None,
            created_at=T0,
            updated_at=T0,
        )
        session.add(version)
        session.flush()
        row = Payment(
            public_id=public_id,
            tenant_id=tenant.id,
            gateway=PaymentGateway.STRIPE,
            expected_amount=amount,
            expected_currency="MYR",
            wallet_credit_amount=amount,
            tax_policy_version_id=version.id,
            tax_amount=None,
            confirmed_amount=amount if paid else None,
            confirmed_currency="MYR" if paid else None,
            gateway_payment_id=session_id,
            gateway_payment_intent_id=intent_id,
            gateway_fee=None,
            status=status,
            status_reason=None,
            review_required_at=created_at if status is PaymentStatus.REVIEW_REQUIRED else None,
            expires_at=created_at + dt.timedelta(hours=1),
            paid_at=created_at if paid else None,
            last_reconciled_at=None,
            created_at=created_at,
            updated_at=created_at,
        )
        session.add(row)
        session.commit()
        return PaymentRef(int(row.id), public_id, session_id, intent_id)


def set_payment_status(world: BillingWorld, payment: PaymentRef, status: PaymentStatus) -> None:
    with world.factory() as session:
        session.execute(update(Payment).where(Payment.id == payment.id).values(status=status))
        session.commit()


def add_gateway_event(
    world: BillingWorld,
    status: PaymentEventProcessingStatus,
    *,
    payment_id: int | None = None,
    metadata: dict[str, Any] | None = None,
    event_type: str = "checkout.session.completed",
) -> int:
    received = CLOSE_AT - dt.timedelta(hours=2)
    with world.factory() as session:
        row = PaymentGatewayEvent(
            gateway=PaymentGateway.STRIPE,
            gateway_event_id=f"evt_test_{uuid.uuid4().hex}",
            event_type=event_type,
            payment_id=payment_id,
            payload_fingerprint="0" * 64,
            verification_status=PaymentEventVerificationStatus.VERIFIED,
            processing_status=status,
            attempts=0,
            next_attempt_at=received,
            lease_until=None,
            last_error_code=None,
            fingerprint_conflict_at=None,
            received_at=received,
            processed_at=received if status in EVENT_FINAL else None,
            metadata_json=dict(metadata or {}),
        )
        session.add(row)
        session.commit()
        return int(row.id)


# --- 读回 -----------------------------------------------------------------------


def close(
    world: BillingWorld,
    tenant: Tenancy,
    *,
    now: dt.datetime = CLOSE_AT,
    reason: str = REASON,
):
    return close_account(
        world.factory,
        actor=world.admin,
        customer_id=tenant.public_id,
        reason=reason,
        context=CONTEXT,
        now=now,
    )


def blockers(world: BillingWorld, tenant: Tenancy, now: dt.datetime = CLOSE_AT) -> ClosureBlockers:
    with world.factory() as session:
        return closure_blockers(session, tenant_id=tenant.id, now=now)


def check(world: BillingWorld, tenant: Tenancy):
    return get_closure_check(world.factory, customer_id=tenant.public_id, now=CLOSE_AT)


def snapshot(world: BillingWorld, tenant: Tenancy) -> dict[str, object]:
    """Everything a closure may write, and the wallet it must not touch, as committed."""
    with world.factory() as session:
        row = session.get(Tenant, tenant.id)
        wallet = session.execute(
            select(Wallet).where(Wallet.tenant_id == tenant.id)
        ).scalar_one_or_none()
        assert row is not None
        audits = select(func.count()).select_from(AuditLog)
        outbox = select(func.count()).select_from(DomainOutbox)
        deliveries = select(ProjectStatusDelivery.next_attempt_at).where(
            ProjectStatusDelivery.tenant_id == tenant.id
        )
        return {
            "tenant": (
                row.account_status,
                row.status_version,
                row.status_changed_at,
                row.updated_at,
            ),
            "wallet": None if wallet is None else (wallet.balance, wallet.version),
            "audits": session.execute(
                audits.where(AuditLog.entity_id == tenant.public_id)
            ).scalar_one(),
            "outbox": session.execute(
                outbox.where(DomainOutbox.aggregate_id == tenant.public_id)
            ).scalar_one(),
            "deliveries": sorted(session.execute(deliveries).scalars()),
        }


def status_audits(world: BillingWorld, tenant: Tenancy) -> list[AuditLog]:
    statement = (
        select(AuditLog)
        .where(
            AuditLog.action == AuditAction.TENANT_ACCOUNT_STATUS_CHANGED,
            AuditLog.entity_id == tenant.public_id,
        )
        .order_by(AuditLog.id)
    )
    with world.factory() as session:
        return list(session.execute(statement).scalars())


def status_events(world: BillingWorld, tenant: Tenancy) -> list[DomainOutbox]:
    statement = (
        select(DomainOutbox)
        .where(
            DomainOutbox.event_type == EVENT_ACCOUNT_STATUS_CHANGED,
            DomainOutbox.aggregate_id == tenant.public_id,
        )
        .order_by(DomainOutbox.id)
    )
    with world.factory() as session:
        return list(session.execute(statement).scalars())


def deliveries(world: BillingWorld, tenant: Tenancy) -> list[dt.datetime]:
    statement = select(ProjectStatusDelivery.next_attempt_at).where(
        ProjectStatusDelivery.tenant_id == tenant.id
    )
    with world.factory() as session:
        return list(session.execute(statement).scalars())


def break_the_audit(monkeypatch) -> None:
    """The audit row fails at flush: `created_at` is NULL (NOT NULL → IntegrityError).

    与 test_account_status_service.py 同一写法：按名字替换 `account_status.record_audit`，
    `_record_transition` 照常取到替身。
    """
    real = account_status.record_audit

    def broken(session: Session, **options: object) -> None:
        real(session, **{**options, "now": None})  # type: ignore[arg-type]

    monkeypatch.setattr(account_status, "record_audit", broken)


def our_records(caplog) -> list[logging.LogRecord]:
    return [
        record
        for record in caplog.records
        if record.name in ("app.services.account_closure", "app.services.account_status")
    ]


# --- 关户正常路径（SQLite 与 MySQL） ------------------------------------------------


def test_closing_writes_status_version_event_delivery_and_audit(both, caplog) -> None:
    world = both
    tenant = disabled(world)
    push_delivery(world, tenant, LATER)
    before = snapshot(world, tenant)
    # 对照：`change_account_status` 写出的 payload 键。
    other = world.tenant("b")
    change_account_status(
        world.factory,
        actor=world.admin,
        customer_id=other.public_id,
        account_status=AccountStatus.DISABLED,
        reason="Reference transition",
        context=CONTEXT,
        now=CLOSE_AT,
    )
    [reference] = status_events(world, other)

    with caplog.at_level(logging.DEBUG):
        detail = close(world, tenant)

    assert (detail.id, detail.account_status, detail.status_version) == (
        tenant.public_id,
        "CLOSED",
        1,
    )
    after = snapshot(world, tenant)
    assert after["tenant"] == (AccountStatus.CLOSED, 1, CLOSE_AT, CLOSE_AT)
    # INV-4：关户只读钱包，不写（`version` 与余额都不变）。
    assert after["wallet"] == before["wallet"]
    # 投递行的退避被作废：提前到关户时刻。
    assert after["deliveries"] == [CLOSE_AT]

    [row] = status_events(world, tenant)
    assert (row.aggregate_type, row.status, row.attempt_count) == (
        "tenant",
        OutboxStatus.PENDING,
        0,
    )
    assert (row.created_at, row.next_retry_at) == (CLOSE_AT, CLOSE_AT)
    payload = json.loads(row.payload_json or "{}")
    assert set(payload) == set(json.loads(reference.payload_json or "{}"))
    assert payload == {
        "account_status": "CLOSED",
        "previous_account_status": "DISABLED",
        "billing_status": "SUSPENDED",
        "billing_mode": "PREPAID",
        "effective_status": "BLOCK_AI",
        "status_version": 1,
        "changed_at": CLOSE_AT.isoformat(),
    }
    assert REASON not in (row.payload_json or "")

    [audit] = status_audits(world, tenant)
    assert (audit.actor_user_id, audit.actor_role) == (world.admin.id, "ADMIN")
    assert (audit.entity_type, audit.entity_id) == ("tenant", tenant.public_id)
    assert (audit.ip_address, audit.user_agent) == (CONTEXT.ip_address, CONTEXT.user_agent)
    assert (audit.reason, audit.created_at) == (REASON, CLOSE_AT)
    assert json.loads(audit.before_state or "{}") == {
        "account_status": "DISABLED",
        "status_version": 0,
    }
    assert json.loads(audit.after_state or "{}") == {
        "account_status": "CLOSED",
        "status_version": 1,
    }
    # 关户服务不写日志（只有扫描截断那一条 WARNING，这里没有截断）。
    assert our_records(caplog) == []


def test_now_defaults_to_whole_seconds(world) -> None:
    tenant = disabled(world)

    close_account(
        world.factory,
        actor=world.admin,
        customer_id=tenant.public_id,
        reason=REASON,
        context=CONTEXT,
    )

    changed_at = snapshot(world, tenant)["tenant"][2]  # type: ignore[index]
    assert changed_at is not None and changed_at.microsecond == 0


def test_the_wallet_is_locked_before_the_tenant_and_never_written(both) -> None:
    """设计 §2：事务 1 先锁钱包、再锁租户（MySQL 上都是 FOR UPDATE）；不写钱包。"""
    world = both
    tenant = disabled(world)
    engine = world.factory.kw["bind"]
    statements: list[str] = []

    def record(_conn, _cursor, statement, _parameters, _context, _executemany) -> None:
        statements.append(" ".join(statement.replace("`", "").split()).upper())

    event.listen(engine, "before_cursor_execute", record)
    try:
        close(world, tenant)
    finally:
        event.remove(engine, "before_cursor_execute", record)

    selects = [s for s in statements if s.startswith("SELECT")]
    wallet_lock = next(i for i, s in enumerate(selects) if "FROM WALLETS" in s)
    tenant_lock = next(i for i, s in enumerate(selects) if i > wallet_lock and "FROM TENANTS" in s)
    # 两把锁之前只有事务 0 的那一次取 id；四类未结的计数都在两把锁之后。
    assert [s for s in selects[:wallet_lock] if "FROM TENANTS" not in s] == []
    counted = [i for i, s in enumerate(selects) if "COUNT(" in s]
    assert counted and min(counted) > tenant_lock
    assert not [s for s in statements if s.startswith("UPDATE WALLETS")]
    if engine.dialect.name == "mysql":
        assert selects[wallet_lock].endswith("FOR UPDATE")
        assert selects[tenant_lock].endswith("FOR UPDATE")


# --- 前置条件逐条（SQLite） ---------------------------------------------------------


def _status(status: AccountStatus) -> Callable[[BillingWorld, Tenancy], None]:
    return lambda world, tenant: world.set_account_status(tenant, status)


def _balance(amount: str) -> Callable[[BillingWorld, Tenancy], None]:
    return lambda world, tenant: set_balance(world, tenant, Decimal(amount))


def _usage(status: UsageEventStatus) -> Callable[[BillingWorld, Tenancy], None]:
    def make(world: BillingWorld, tenant: Tenancy) -> None:
        add_usage(world, tenant, status)

    return make


def _ai(status: AiUsageOutboxStatus) -> Callable[[BillingWorld, Tenancy], None]:
    def make(world: BillingWorld, tenant: Tenancy) -> None:
        add_ai_usage(world, tenant, status)

    return make


def _payment(status: PaymentStatus, age: dt.timedelta) -> Callable[[BillingWorld, Tenancy], None]:
    def make(world: BillingWorld, tenant: Tenancy) -> None:
        add_payment(world, tenant, status, created_at=CLOSE_AT - age)

    return make


def _event(
    status: PaymentEventProcessingStatus, by: str, event_type: str = "checkout.session.completed"
) -> Callable[[BillingWorld, Tenancy], None]:
    """An event of the tenant's (already PAID) payment, attributable only through `by`."""

    def make(world: BillingWorld, tenant: Tenancy) -> None:
        payment = add_payment(world, tenant, PaymentStatus.PAID)
        if by == "payment_id":
            add_gateway_event(world, status, payment_id=payment.id, event_type=event_type)
            return
        value = {
            "client_reference_id": payment.public_id,
            "object_id": payment.session_id,
            "payment_intent": payment.intent_id,
        }[by]
        add_gateway_event(world, status, metadata={by: value}, event_type=event_type)

    return make


_BLOCKING: list[Any] = [
    pytest.param(
        _status(AccountStatus.ENABLED),
        AccountStatusTransitionInvalid,
        TRANSITION_INVALID,
        id="enabled",
    ),
    pytest.param(
        _status(AccountStatus.PENDING_ACTIVATION),
        AccountStatusTransitionInvalid,
        TRANSITION_INVALID,
        id="pending-activation",
    ),
    pytest.param(
        _balance("0.00000001"), AccountClosureBalanceNotZero, BALANCE_NOT_ZERO, id="balance+"
    ),
    pytest.param(
        _balance("-0.00000001"), AccountClosureBalanceNotZero, BALANCE_NOT_ZERO, id="balance-"
    ),
    *[
        pytest.param(
            _usage(status), AccountClosureUsagePending, USAGE_PENDING, id=f"usage-{status}"
        )
        for status in UNRESOLVED_USAGE
    ],
    *[
        pytest.param(_ai(status), AccountClosureAiUsagePending, AI_USAGE_PENDING, id=f"ai-{status}")
        for status in (
            AiUsageOutboxStatus.PENDING,
            AiUsageOutboxStatus.SENDING,
            AiUsageOutboxStatus.HELD,
        )
    ],
    pytest.param(
        _payment(PaymentStatus.PENDING, dt.timedelta(days=30)),
        AccountClosurePaymentsPending,
        PAYMENTS_PENDING,
        id="payment-pending",
    ),
    pytest.param(
        _payment(PaymentStatus.REVIEW_REQUIRED, dt.timedelta(days=30)),
        AccountClosurePaymentsPending,
        PAYMENTS_PENDING,
        id="payment-review-required",
    ),
    pytest.param(
        _payment(PaymentStatus.EXPIRED, dt.timedelta(days=2)),
        AccountClosurePaymentsPending,
        PAYMENTS_PENDING,
        id="payment-expired-within-7-days",
    ),
    pytest.param(
        _payment(PaymentStatus.EXPIRED, SETTLE - SECOND),
        AccountClosurePaymentsPending,
        PAYMENTS_PENDING,
        id="payment-expired-one-second-after-the-boundary",
    ),
    pytest.param(
        _event(PaymentEventProcessingStatus.RECEIVED, "client_reference_id"),
        AccountClosurePaymentEventsPending,
        PAYMENT_EVENTS_PENDING,
        id="event-received-by-client-reference-id",
    ),
    pytest.param(
        _event(PaymentEventProcessingStatus.PROCESSING, "object_id"),
        AccountClosurePaymentEventsPending,
        PAYMENT_EVENTS_PENDING,
        id="event-processing-by-object-id",
    ),
    pytest.param(
        _event(PaymentEventProcessingStatus.RETRY, "payment_id"),
        AccountClosurePaymentEventsPending,
        PAYMENT_EVENTS_PENDING,
        id="event-retry-by-payment-id",
    ),
    pytest.param(
        _event(PaymentEventProcessingStatus.RECEIVED, "payment_intent", "charge.dispute.created"),
        AccountClosurePaymentEventsPending,
        PAYMENT_EVENTS_PENDING,
        id="dispute-event-by-payment-intent",
    ),
]


@pytest.mark.parametrize(("make", "error", "code"), _BLOCKING)
def test_each_unmet_condition_is_refused_and_writes_nothing(world, make, error, code) -> None:
    tenant = disabled(world)
    make(world, tenant)
    before = snapshot(world, tenant)

    with pytest.raises(error) as raised:
        close(world, tenant)

    assert (raised.value.code, raised.value.http_status) == (code, 409)
    # 消息是固定的英文句子：不带原因、计数与金额。
    message = str(raised.value)
    assert REASON not in message and "0.00000001" not in message
    assert not any(character.isdigit() for character in message)
    assert snapshot(world, tenant) == before
    assert status_audits(world, tenant) == [] and status_events(world, tenant) == []
    assert check(world, tenant).codes == (code,)


def test_the_unresolved_usage_statuses_are_everything_but_the_two_final_ones() -> None:
    final = {UsageEventStatus.PROCESSED, UsageEventStatus.FAILED_FINAL}
    assert set(account_closure.UNRESOLVED_USAGE_STATUSES) == set(UsageEventStatus) - final
    assert set(account_closure.UNRESOLVED_USAGE_STATUSES) == set(UNRESOLVED_USAGE)


def test_each_category_counts_its_own_rows(world) -> None:
    tenant = disabled(world)
    for status in UNRESOLVED_USAGE:
        add_usage(world, tenant, status)
    for status in (AiUsageOutboxStatus.PENDING, AiUsageOutboxStatus.HELD):
        add_ai_usage(world, tenant, status)
    add_payment(world, tenant, PaymentStatus.PENDING)
    paid = add_payment(world, tenant, PaymentStatus.PAID)
    add_gateway_event(world, PaymentEventProcessingStatus.RETRY, payment_id=paid.id)
    # 一个事件同时以几种引用归属本租户：只算一次。
    add_gateway_event(
        world,
        PaymentEventProcessingStatus.RECEIVED,
        metadata={"client_reference_id": paid.public_id, "object_id": paid.session_id},
    )

    found = blockers(world, tenant)

    assert found == ClosureBlockers(
        usage_events=7, ai_usage=2, payments=1, payment_events=2, payment_events_truncated=False
    )
    assert found.codes() == (
        USAGE_PENDING,
        AI_USAGE_PENDING,
        PAYMENTS_PENDING,
        PAYMENT_EVENTS_PENDING,
    )


def test_the_final_states_do_not_block(world) -> None:
    """设计 §7：PROCESSED、FAILED_FINAL、DELIVERED、DEAD、DISCARDED、PAID、FAILED、CANCELLED、
    恰好 7 天前与 7 天零 1 秒前的 EXPIRED，
    网关事件 PROCESSED / IGNORED / UNMATCHED / REJECTED。"""
    tenant = disabled(world)
    # PROCESSED 只能经计费产生（快照 CHECK）：0 元事件，不写账本、余额仍 0。
    world.publish_markup_usd()
    processed = world.event(tenant, tokens=ZERO_TOKENS)
    world.bill()
    with world.factory() as session:
        row = session.get(UsageEvent, processed)
        assert row is not None and row.status is UsageEventStatus.PROCESSED
    add_usage(world, tenant, UsageEventStatus.FAILED_FINAL)
    for status in (
        AiUsageOutboxStatus.DELIVERED,
        AiUsageOutboxStatus.DEAD,
        AiUsageOutboxStatus.DISCARDED,
    ):
        add_ai_usage(world, tenant, status)
    for status in (PaymentStatus.PAID, PaymentStatus.FAILED, PaymentStatus.CANCELLED):
        add_payment(world, tenant, status)
    add_payment(world, tenant, PaymentStatus.EXPIRED, created_at=CLOSE_AT - SETTLE)
    add_payment(world, tenant, PaymentStatus.EXPIRED, created_at=CLOSE_AT - SETTLE - SECOND)
    paid = add_payment(world, tenant, PaymentStatus.PAID)
    for status in EVENT_FINAL:
        add_gateway_event(
            world,
            status,
            payment_id=paid.id,
            metadata={
                "client_reference_id": paid.public_id,
                "object_id": paid.session_id,
                "payment_intent": paid.intent_id,
            },
        )

    assert blockers(world, tenant) == NOTHING
    view = check(world, tenant)
    assert (view.account_status, view.balance, view.codes) == (
        AccountStatus.DISABLED,
        Decimal(0),
        (),
    )
    assert close(world, tenant).account_status == "CLOSED"


def test_the_seven_day_boundary_is_exclusive(world) -> None:
    """`created_at` 恰好等于 `now - 7 天` 不挡；晚一秒挡。"""
    tenant = disabled(world)
    add_payment(world, tenant, PaymentStatus.EXPIRED, created_at=CLOSE_AT - SETTLE)
    assert blockers(world, tenant).payments == 0
    assert blockers(world, tenant, now=CLOSE_AT - SECOND).payments == 1


def test_non_string_references_are_ignored(world) -> None:
    tenant = disabled(world)
    paid = add_payment(world, tenant, PaymentStatus.PAID)
    add_gateway_event(
        world,
        PaymentEventProcessingStatus.RECEIVED,
        metadata={
            "client_reference_id": [paid.public_id],
            "object_id": {"id": paid.session_id},
            "payment_intent": None,
        },
    )

    assert blockers(world, tenant) == NOTHING


# --- 判定顺序 -------------------------------------------------------------------


def test_only_the_first_unmet_condition_is_reported(world) -> None:
    """设计 §2：CLOSED → 不是 DISABLED → 余额 → 用量 → AI outbox → 支付 → 网关事件。"""
    tenant = world.tenant()  # ENABLED
    set_balance(world, tenant, Decimal("5"))
    usage = add_usage(world, tenant, UsageEventStatus.RECEIVED)
    ai_row = add_ai_usage(world, tenant, AiUsageOutboxStatus.PENDING)
    pending = add_payment(world, tenant, PaymentStatus.PENDING)
    paid = add_payment(world, tenant, PaymentStatus.PAID)
    add_gateway_event(
        world,
        PaymentEventProcessingStatus.RECEIVED,
        metadata={"client_reference_id": paid.public_id},
    )
    assert check(world, tenant).codes == (
        TRANSITION_INVALID,
        BALANCE_NOT_ZERO,
        USAGE_PENDING,
        AI_USAGE_PENDING,
        PAYMENTS_PENDING,
        PAYMENT_EVENTS_PENDING,
    )

    steps: list[tuple[Callable[[], None] | None, type[Exception]]] = [
        (None, AccountStatusTransitionInvalid),
        (
            lambda: world.set_account_status(tenant, AccountStatus.DISABLED),
            AccountClosureBalanceNotZero,
        ),
        (lambda: set_balance(world, tenant, Decimal(0)), AccountClosureUsagePending),
        (
            lambda: set_usage_status(world, usage, UsageEventStatus.FAILED_FINAL),
            AccountClosureAiUsagePending,
        ),
        (
            lambda: set_ai_usage_status(world, ai_row, AiUsageOutboxStatus.DELIVERED),
            AccountClosurePaymentsPending,
        ),
        (
            lambda: set_payment_status(world, pending, PaymentStatus.FAILED),
            AccountClosurePaymentEventsPending,
        ),
    ]
    for fix, error in steps:
        if fix is not None:
            fix()
        before = snapshot(world, tenant)
        with pytest.raises(error):
            close(world, tenant)
        assert snapshot(world, tenant) == before


# --- 网关事件扫描上限 ---------------------------------------------------------------


def test_a_full_scan_counts_as_unresolved(world, monkeypatch, caplog) -> None:
    """设计 §7：上限注入为 3，3 条别的租户的未结事件 —— 计数 0、截断、409、一条 WARNING。"""
    monkeypatch.setattr(account_closure, "PAYMENT_EVENT_SCAN_LIMIT", 3)
    ours = disabled(world)
    other = world.tenant("b")
    theirs = add_payment(world, other, PaymentStatus.PAID)
    for _ in range(3):
        add_gateway_event(
            world,
            PaymentEventProcessingStatus.RECEIVED,
            metadata={"client_reference_id": theirs.public_id},
        )

    found = blockers(world, ours)
    assert (found.payment_events, found.payment_events_truncated) == (0, True)
    assert found.codes() == (PAYMENT_EVENTS_PENDING,)
    view = check(world, ours)
    assert (view.blockers, view.codes) == (found, (PAYMENT_EVENTS_PENDING,))
    before = snapshot(world, ours)
    caplog.clear()

    with caplog.at_level(logging.DEBUG), pytest.raises(AccountClosurePaymentEventsPending):
        close(world, ours)

    assert snapshot(world, ours) == before
    [record] = our_records(caplog)
    assert (record.name, record.levelname) == ("app.services.account_closure", "WARNING")
    message = record.getMessage()
    assert "3" in message
    assert ours.public_id not in message and theirs.public_id not in message


def test_a_scan_below_the_limit_is_complete(world, monkeypatch, caplog) -> None:
    monkeypatch.setattr(account_closure, "PAYMENT_EVENT_SCAN_LIMIT", 3)
    ours = disabled(world)
    other = world.tenant("b")
    theirs = add_payment(world, other, PaymentStatus.PAID)
    for _ in range(2):
        add_gateway_event(
            world,
            PaymentEventProcessingStatus.RECEIVED,
            metadata={"client_reference_id": theirs.public_id},
        )

    assert blockers(world, ours) == NOTHING
    with caplog.at_level(logging.DEBUG):
        assert close(world, ours).account_status == "CLOSED"
    assert our_records(caplog) == []


# --- 已关户、跨租户、客户不存在 ---------------------------------------------------------


def test_closing_a_closed_account_writes_nothing(world) -> None:
    tenant = disabled(world)
    first = close(world, tenant)
    before = snapshot(world, tenant)

    again = close(world, tenant, now=LATER, reason="Another reason")

    assert again == first
    assert snapshot(world, tenant) == before
    assert len(status_audits(world, tenant)) == 1 and len(status_events(world, tenant)) == 1


def test_a_closed_account_returns_before_any_other_check(world) -> None:
    """已是 CLOSED：直接返回、不写；余额与未结都不看（直接改库造的 CLOSED）。"""
    tenant = world.tenant()
    world.set_account_status(tenant, AccountStatus.CLOSED)
    set_balance(world, tenant, Decimal("3"))
    add_usage(world, tenant, UsageEventStatus.RECEIVED)
    before = snapshot(world, tenant)

    detail = close(world, tenant)

    assert detail.account_status == "CLOSED"
    assert snapshot(world, tenant) == before
    view = check(world, tenant)
    assert (view.codes, view.blockers.usage_events) == ((), 1)


def test_another_tenants_unresolved_rows_do_not_count(world) -> None:
    """INV-8：别的租户有各类未结与非零余额，本租户关户成功、计数为 0、别的租户不受影响。"""
    other = disabled(world, "b")
    set_balance(world, other, Decimal("7"))
    add_usage(world, other, UsageEventStatus.RECEIVED)
    add_ai_usage(world, other, AiUsageOutboxStatus.HELD)
    add_payment(world, other, PaymentStatus.PENDING)
    theirs = add_payment(world, other, PaymentStatus.PAID)
    add_gateway_event(world, PaymentEventProcessingStatus.RETRY, payment_id=theirs.id)
    add_gateway_event(
        world,
        PaymentEventProcessingStatus.RECEIVED,
        metadata={
            "client_reference_id": theirs.public_id,
            "object_id": theirs.session_id,
            "payment_intent": theirs.intent_id,
        },
    )
    ours = disabled(world)
    # 本租户也有支付，但事件引用的都是别人的。
    add_payment(world, ours, PaymentStatus.PAID)
    theirs_before = snapshot(world, other)
    theirs_codes = check(world, other).codes

    assert blockers(world, ours) == NOTHING
    assert close(world, ours).account_status == "CLOSED"

    assert snapshot(world, other) == theirs_before
    assert (
        check(world, other).codes
        == theirs_codes
        == (
            BALANCE_NOT_ZERO,
            USAGE_PENDING,
            AI_USAGE_PENDING,
            PAYMENTS_PENDING,
            PAYMENT_EVENTS_PENDING,
        )
    )


def test_an_unknown_customer_is_not_found(world) -> None:
    tenant = disabled(world)
    before = snapshot(world, tenant)
    unknown = Tenancy(str(uuid.uuid4()), 0, 0, 0)

    with pytest.raises(CustomerNotFound):
        close(world, unknown)
    with pytest.raises(CustomerNotFound):
        check(world, unknown)

    assert snapshot(world, tenant) == before


def test_a_missing_wallet_is_a_data_defect(world) -> None:
    """每个租户都有钱包；缺了 → RuntimeError("WALLET_MISSING")（接口 500），什么都不写。"""
    tenant = disabled(world)
    with world.factory() as session:
        session.execute(delete(Wallet).where(Wallet.tenant_id == tenant.id))
        session.commit()
    before = snapshot(world, tenant)

    with pytest.raises(RuntimeError, match="^WALLET_MISSING$"):
        close(world, tenant)
    with pytest.raises(RuntimeError, match="^WALLET_MISSING$"):
        check(world, tenant)

    assert snapshot(world, tenant) == before


# --- 只读检查 -------------------------------------------------------------------


def test_the_check_reads_everything_and_writes_nothing(world) -> None:
    tenant = world.tenant()  # ENABLED
    set_balance(world, tenant, Decimal("0.00000001"))
    add_ai_usage(world, tenant, AiUsageOutboxStatus.SENDING)
    before = snapshot(world, tenant)

    view = check(world, tenant)

    assert view.account_status is AccountStatus.ENABLED
    assert view.balance == Decimal("0.00000001")
    assert view.blockers == ClosureBlockers(
        usage_events=0, ai_usage=1, payments=0, payment_events=0, payment_events_truncated=False
    )
    assert view.codes == (TRANSITION_INVALID, BALANCE_NOT_ZERO, AI_USAGE_PENDING)
    assert snapshot(world, tenant) == before


# --- 事务中途失败（SQLite 与 MySQL） ----------------------------------------------------


def test_a_failure_while_writing_leaves_the_account_disabled(both, monkeypatch) -> None:
    """设计 §7：审计写入时注入数据库错误 —— 异常传出。

    账户仍 DISABLED，没有 outbox、版本不变，投递行的退避也回滚。
    """
    world = both
    tenant = disabled(world)
    push_delivery(world, tenant, LATER)
    before = snapshot(world, tenant)
    break_the_audit(monkeypatch)

    with pytest.raises(IntegrityError):
        close(world, tenant)

    assert snapshot(world, tenant) == before
    assert before["tenant"][:2] == (AccountStatus.DISABLED, 0)  # type: ignore[index]
    assert status_events(world, tenant) == [] and status_audits(world, tenant) == []
    assert deliveries(world, tenant) == [LATER]


# --- Redis/Celery 丢失 ----------------------------------------------------------------


def test_closing_needs_no_queue(world) -> None:
    """设计 §7：不注入任何 Celery 依赖，关户照常成功；投递行已重排、outbox 行 PENDING。"""
    source = inspect.getsource(account_closure)
    assert "celery" not in source.lower()
    assert ".delay(" not in source and "apply_async" not in source
    tenant = disabled(world)
    push_delivery(world, tenant, LATER)

    assert close(world, tenant).account_status == "CLOSED"

    [row] = status_events(world, tenant)
    assert row.status is OutboxStatus.PENDING
    assert deliveries(world, tenant) == [CLOSE_AT]

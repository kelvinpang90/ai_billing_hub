"""The two manual exits of a review (design gate #585 v3, AIH-TASK-182).

设计 §2「人工复核出口」；§7 标 182 的各行：

- **CLOSE** 三组（结案、重复结案、非复核状态结案）；
- **人工入账核对**：第 1 步网关 id 为空、第 2 步（Stripe 不可用 503、
  拒绝查询 409）、第 3 步每一行与顺序（含部分退款、全部退款、有争议、
  charge 信息缺失）、第 4 步每一行、`gateway` 为 `None` 时 CREDIT 503 而
  CLOSE 照常、第 5 步的手续费、第 6 步（重放、`LedgerConflict`、撞唯一
  约束、余额越界与其他错误）；
- **人工入账金额**（应收 100.00、实收 99.99）、**事务中途失败**、
  **原因不进日志**、`DISABLED` 租户照常入账：`review_world` 夹具的两个
  参数，在 SQLite 与真 MySQL 上各跑一次；
- **入账走账本与复机**（余额为负的暂停租户）、**余额真的越界**、
  **两个管理员并发**、**CREDIT 对自动确认并发**、**CREDIT 对关户**
  （两种先后）：只在真 MySQL 上。SQLite 没有钱包触发器（余额不随账本
  推进），`FOR UPDATE` 也被忽略。

支付、租户与假 Stripe 用共用的 payment_world.py；本文件的夹具只在这里用，
定义在这里。Stripe 一律是注入了假传输的 `StripeGateway`，不联网；时间一律
注入，不 sleep。

MySQL 那部分需要 `BILLING_TEST_DATABASE_URL`，没设时 skip ——
**不要把 skipped 读成 passed**，CI 设了它并把 skipped 判成失败。
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from typing import Any

import pytest
from payment_world import (
    CREATED,
    NOW,
    Customer,
    PaymentRef,
    World,
    audit_state,
    build_mysql_world,
    build_sqlite_world,
    mysql_url,
)
from sqlalchemy import Engine, create_engine, event, select, text, update
from sqlalchemy.orm import Session

from app.core.database import create_session_factory
from app.core.errors import AppError
from app.models.auth import AuditAction, AuditLog, DomainOutbox, User
from app.models.payments import Payment, PaymentResolution, PaymentStatus
from app.models.tenancy import AccountStatus, BillingMode, BillingStatus, Tenant
from app.models.wallet import ReferenceType, TransactionType
from app.payments.base import GatewayPaymentStatus, GatewayState, PaymentGateway
from app.repositories.wallet import (
    EVENT_BILLING_STATUS_CHANGED,
    REASON_BALANCE_POSITIVE,
    InvalidAmount,
    InvalidTransaction,
    LedgerConflict,
    post_transaction,
    verify_wallet,
)
from app.services import payment_review as service
from app.services.audit_query import PUBLIC_ENTITY_TYPES
from app.services.auth import RequestContext
from app.services.payment_review import (
    CURRENCY_NOT_MYR,
    GATEWAY_NOT_PAID,
    GATEWAY_PAYMENT_REUSED,
    GATEWAY_QUERY_REJECTED,
    GATEWAY_REFERENCE_MISMATCH,
    GATEWAY_REFERENCE_MISSING,
    GATEWAY_REFUNDED_OR_DISPUTED,
    INTERNAL_TENANT,
    PAYMENT_GATEWAY_UNAVAILABLE,
    PAYMENT_NOT_FOUND,
    PAYMENT_NOT_IN_REVIEW,
    PAYMENTS_MISCONFIGURED,
    RESOLUTION_CONFLICT,
    TENANT_CLOSED,
    ReviewAction,
    resolve_review,
)
from app.services.payments import AMOUNT_MISMATCH, confirm_payment
from app.services.wallet_adjustments import BalanceOutOfRange

CREDIT = ReviewAction.CREDIT
CLOSE = ReviewAction.CLOSE
REVIEW = PaymentStatus.REVIEW_REQUIRED

# 固定值而不是 utc_now()：断言「存进去的就是调用方给的那个时刻」要能逐字比对。
RESOLVED_AT = dt.datetime(2026, 11, 9, 10, 0, 0)
CONTEXT = RequestContext(ip_address="203.0.113.9", user_agent="payment-review-test")
# 足够独特，日志里出现任何一段都认得出来。
REASON = "Fictional review note: payer Siti confirmed by phone, ref Q7XK2"

BEFORE_FIELDS = {"status", "status_reason", "resolution", "confirmed_amount", "confirmed_currency"}
AFTER_FIELDS = BEFORE_FIELDS | {"credited_amount", "resolution_reason"}

# 「用 world 的假 Stripe」；显式传 None 表示接口层没有网关。
_WORLD_GATEWAY: Any = object()


# --- 夹具 ---------------------------------------------------------------


@pytest.fixture(params=["sqlite", "mysql"])
def review_world(request, monkeypatch) -> Iterator[World]:
    if request.param == "mysql":
        yield from build_mysql_world(monkeypatch)
        return
    yield from build_sqlite_world()


@pytest.fixture
def mysql_review_world(monkeypatch) -> Iterator[World]:
    yield from build_mysql_world(monkeypatch)


# --- 构件 ---------------------------------------------------------------


def admin_of(world: World) -> User:
    with world.factory() as session:
        user = session.get(User, world.admin_id)
        assert user is not None
        session.expunge(user)
        return user


def in_review(
    world: World,
    tenant: Customer | None = None,
    *,
    reason: str = AMOUNT_MISMATCH,
    confirmed: tuple[str, str] | None = ("99.99", "MYR"),
    intent_id: str | None = None,
    with_session: bool = True,
) -> PaymentRef:
    """A `REVIEW_REQUIRED` payment (应收 100.00) as A's confirmation would have left it."""
    payment = world.payment(
        tenant or world.customer(),
        status=REVIEW,
        reason=reason,
        with_session=with_session,
        intent_id=intent_id,
    )
    if confirmed is not None:
        with world.factory() as session:
            session.execute(
                update(Payment)
                .where(Payment.public_id == payment.public_id)
                .values(confirmed_amount=Decimal(confirmed[0]), confirmed_currency=confirmed[1])
            )
            session.commit()
    return payment


def serve_paid(
    world: World,
    payment: PaymentRef,
    *,
    state: GatewayState = GatewayState.PAID,
    refunded: int | None = 0,
    disputed: bool | None = False,
    charge: bool = True,
    **overrides: Any,
) -> dict[str, Any]:
    """What Stripe answers, with the latest charge's refund and dispute fields (None = absent)."""
    body = world.serve(payment, state, **overrides)
    intent = body.get("payment_intent")
    if isinstance(intent, dict):
        if not charge:
            del intent["latest_charge"]
        else:
            latest = intent["latest_charge"]
            if refunded is not None:
                latest["amount_refunded"] = refunded
            if disputed is not None:
                latest["disputed"] = disputed
    return body


def resolve(
    world: World,
    payment: PaymentRef | str,
    action: ReviewAction,
    *,
    gateway: Any = _WORLD_GATEWAY,
    now: dt.datetime = RESOLVED_AT,
    reason: str = REASON,
    factory: Any = None,
) -> str:
    public_id = payment if isinstance(payment, str) else payment.public_id
    return resolve_review(
        factory or world.factory,
        world.stripe.gateway() if gateway is _WORLD_GATEWAY else gateway,
        actor=admin_of(world),
        payment_id=public_id,
        action=action,
        reason=reason,
        context=CONTEXT,
        now=now,
    )


def refused(
    world: World,
    payment: PaymentRef | str,
    action: ReviewAction,
    *,
    gateway: Any = _WORLD_GATEWAY,
) -> tuple[str, int]:
    """(code, HTTP status) of the AppError the call raised."""
    with pytest.raises(AppError) as raised:
        resolve(world, payment, action, gateway=gateway)
    return raised.value.code, raised.value.http_status


def review_audits(world: World, payment: PaymentRef) -> list[AuditLog]:
    with world.factory() as session:
        rows = list(
            session.execute(
                select(AuditLog)
                .where(AuditLog.action == AuditAction.PAYMENT_REVIEW_RESOLVED)
                .where(AuditLog.entity_id == payment.public_id)
                .order_by(AuditLog.id)
            ).scalars()
        )
        for row in rows:
            session.expunge(row)
        return rows


def money(value: object) -> Decimal | None:
    """An audit money field: fixed-point text (never `1E+2` / `0E-8`) or null."""
    if value is None:
        return None
    assert isinstance(value, str) and "E" not in value.upper(), value
    return Decimal(value)


def money_fields(state: dict[str, Any]) -> dict[str, Any]:
    """The audit state with its amounts read as Decimals (the text scale is dialect-neutral)."""
    amounts = {"confirmed_amount", "credited_amount"}
    return {key: money(value) if key in amounts else value for key, value in state.items()}


def post_directly(
    world: World, tenant: Customer, kind: TransactionType, amount: str, *, reference: str
) -> None:
    source = (
        ReferenceType.PAYMENT if kind is TransactionType.TOPUP else ReferenceType.ADMIN_ADJUSTMENT
    )
    with world.factory() as session:
        post_transaction(
            session,
            tenant_id=tenant.id,
            transaction_type=kind,
            amount=Decimal(amount),
            reference_type=source,
            reference_id=reference,
            now=CREATED,
            created_by=world.admin_id,
            description="Fictional ledger row before a review",
            actor_role="ADMIN",
        )
        session.commit()


# --- CLOSE（不入账结案） ------------------------------------------------


def test_close_fails_the_payment_and_records_the_resolution(review_world: World) -> None:
    """设计 §7「CLOSE」：`FAILED`、原因保留、四个结案字段、一条审计；

    不写账本、不查 Stripe；不需要网关。
    """
    world = review_world
    payment = in_review(world)
    before = world.snapshot(payment)

    returned = resolve(world, payment, CLOSE, gateway=None)

    assert returned == payment.public_id
    row = world.row(payment)
    assert row.status is PaymentStatus.FAILED
    assert row.status_reason == AMOUNT_MISMATCH
    assert row.resolution is PaymentResolution.CLOSED
    assert row.resolved_at == RESOLVED_AT
    assert row.resolved_by_user_id == world.admin_id
    assert row.resolution_reason == REASON
    assert row.updated_at == RESOLVED_AT
    # 其余列不变。
    unchanged = {"status", "resolution", "resolved_at", "resolved_by_user_id"}
    unchanged |= {"resolution_reason", "updated_at"}
    columns = {key: value for key, value in before["payment"].items() if key not in unchanged}
    after = world.snapshot(payment)
    assert {key: after["payment"][key] for key in columns} == columns
    assert world.topups() == []
    assert after["ledger"] == before["ledger"]
    assert after["outbox"] == before["outbox"]
    assert world.stripe.asked == []

    [audit] = review_audits(world, payment)
    assert (audit.actor_user_id, audit.actor_role) == (world.admin_id, "ADMIN")
    assert (audit.entity_type, audit.entity_id) == ("payments", payment.public_id)
    assert (audit.ip_address, audit.user_agent) == (CONTEXT.ip_address, CONTEXT.user_agent)
    assert (audit.reason, audit.created_at) == (REASON, RESOLVED_AT)
    before_state, after_state = audit_state(audit.before_state), audit_state(audit.after_state)
    assert set(before_state) == BEFORE_FIELDS
    assert set(after_state) == AFTER_FIELDS
    assert money_fields(before_state) == {
        "status": "REVIEW_REQUIRED",
        "status_reason": AMOUNT_MISMATCH,
        "resolution": None,
        "confirmed_amount": Decimal("99.99"),
        "confirmed_currency": "MYR",
    }
    assert money_fields(after_state) == {
        "status": "FAILED",
        "status_reason": AMOUNT_MISMATCH,
        "resolution": "CLOSED",
        "confirmed_amount": Decimal("99.99"),
        "confirmed_currency": "MYR",
        "credited_amount": None,
        "resolution_reason": REASON,
    }
    assert after["audits"] == before["audits"] + 1


@pytest.mark.parametrize("action", [CLOSE, CREDIT])
def test_a_payment_already_resolved_is_409_and_unchanged(review_world: World, action) -> None:
    """重复结案（以及结案后再入账）：409 `PAYMENT_NOT_IN_REVIEW`，什么都不写。"""
    world = review_world
    payment = in_review(world)
    serve_paid(world, payment)
    resolve(world, payment, CLOSE)
    before = world.snapshot(payment)

    assert refused(world, payment, action) == (PAYMENT_NOT_IN_REVIEW, 409)

    assert world.snapshot(payment) == before
    assert len(review_audits(world, payment)) == 1
    assert world.stripe.asked == []


@pytest.mark.parametrize(
    "status",
    [
        PaymentStatus.PENDING,
        PaymentStatus.PAID,
        PaymentStatus.FAILED,
        PaymentStatus.EXPIRED,
        PaymentStatus.CANCELLED,
    ],
)
@pytest.mark.parametrize("action", [CLOSE, CREDIT])
def test_a_payment_not_in_review_is_409_and_unchanged(
    review_world: World, status: PaymentStatus, action
) -> None:
    """非复核状态：409 `PAYMENT_NOT_IN_REVIEW`（先于网关检查：`gateway` 为 None 也一样）。"""
    world = review_world
    payment = world.payment(world.customer(), status=status)
    serve_paid(world, payment)
    before = world.snapshot(payment)

    assert refused(world, payment, action) == (PAYMENT_NOT_IN_REVIEW, 409)
    assert refused(world, payment, action, gateway=None) == (PAYMENT_NOT_IN_REVIEW, 409)

    assert world.snapshot(payment) == before
    assert world.stripe.asked == []


@pytest.mark.parametrize("action", [CLOSE, CREDIT])
def test_an_unknown_payment_is_404(review_world: World, action) -> None:
    world = review_world

    assert refused(world, str(uuid.uuid4()), action) == (PAYMENT_NOT_FOUND, 404)
    assert refused(world, str(uuid.uuid4()), action, gateway=None) == (PAYMENT_NOT_FOUND, 404)


def test_payment_review_audits_are_public_ids() -> None:
    """`entity_id` 是 `payments.public_id`：审计查询原样返回。"""
    assert "payments" in PUBLIC_ENTITY_TYPES


# --- CREDIT（按实付入账）：成功 -----------------------------------------


def test_credit_posts_the_received_amount_and_marks_the_payment_paid(review_world: World) -> None:
    """全部核对通过：`PAID`、原因保留、确认字段、PI、手续费、结案字段、一条 TOPUP、一条审计。"""
    world = review_world
    tenant = world.customer()
    payment = in_review(world, tenant, confirmed=("100.00", "MYR"), reason="GATEWAY_INCONSISTENT")
    body = serve_paid(world, payment, fee=290)
    intent_id = body["payment_intent"]["id"]
    before = world.snapshot(payment)

    returned = resolve(world, payment, CREDIT)

    assert returned == payment.public_id
    assert world.stripe.asked == [payment.session_id]
    row = world.row(payment)
    assert row.status is PaymentStatus.PAID
    assert row.status_reason == "GATEWAY_INCONSISTENT"
    assert (row.confirmed_amount, row.confirmed_currency) == (Decimal("100"), "MYR")
    assert (row.paid_at, row.last_reconciled_at, row.updated_at) == (RESOLVED_AT,) * 3
    assert row.gateway_payment_intent_id == intent_id
    assert row.gateway_fee == Decimal("2.90")
    assert row.resolution is PaymentResolution.CREDITED
    assert (row.resolved_at, row.resolved_by_user_id) == (RESOLVED_AT, world.admin_id)
    assert row.resolution_reason == REASON
    # 建单时的计划入账额不变（A 的含义）。
    assert row.wallet_credit_amount == Decimal("100")

    [topup] = world.topups(payment)
    assert topup.tenant_id == tenant.id
    assert topup.amount == Decimal("100")
    assert (topup.reference_type, topup.reference_id) == (ReferenceType.PAYMENT, payment.public_id)
    assert topup.description == REASON
    assert topup.created_by == world.admin_id
    assert topup.created_at == RESOLVED_AT

    [audit] = review_audits(world, payment)
    assert (audit.actor_user_id, audit.actor_role) == (world.admin_id, "ADMIN")
    assert (audit.entity_type, audit.entity_id) == ("payments", payment.public_id)
    assert (audit.ip_address, audit.user_agent) == (CONTEXT.ip_address, CONTEXT.user_agent)
    assert (audit.reason, audit.created_at) == (REASON, RESOLVED_AT)
    before_state, after_state = audit_state(audit.before_state), audit_state(audit.after_state)
    assert set(before_state) == BEFORE_FIELDS
    assert set(after_state) == AFTER_FIELDS
    assert money_fields(before_state) == {
        "status": "REVIEW_REQUIRED",
        "status_reason": "GATEWAY_INCONSISTENT",
        "resolution": None,
        "confirmed_amount": Decimal("100"),
        "confirmed_currency": "MYR",
    }
    assert money_fields(after_state) == {
        "status": "PAID",
        "status_reason": "GATEWAY_INCONSISTENT",
        "resolution": "CREDITED",
        "confirmed_amount": Decimal("100"),
        "confirmed_currency": "MYR",
        "credited_amount": Decimal("100"),
        "resolution_reason": REASON,
    }
    after = world.snapshot(payment)
    assert after["ledger"] == before["ledger"] + 1


def test_credit_posts_what_stripe_received_not_what_was_expected(review_world: World) -> None:
    """设计 §7「人工入账金额」：应收 100.00、实收 99.99（`AMOUNT_MISMATCH`）→ TOPUP 99.99、
    `confirmed_amount` 99.99、审计的 `credited_amount` 99.99。"""
    world = review_world
    payment = in_review(world, reason=AMOUNT_MISMATCH, confirmed=("99.99", "MYR"))
    serve_paid(world, payment, amount_total=9999)

    resolve(world, payment, CREDIT)

    [topup] = world.topups(payment)
    assert topup.amount == Decimal("99.99")
    row = world.row(payment)
    assert row.confirmed_amount == Decimal("99.99")
    assert row.expected_amount == row.wallet_credit_amount == Decimal("100")
    assert row.status_reason == AMOUNT_MISMATCH
    [audit] = review_audits(world, payment)
    after_state = audit_state(audit.after_state)
    assert money(after_state["credited_amount"]) == Decimal("99.99")
    assert money(after_state["confirmed_amount"]) == Decimal("99.99")


@pytest.mark.parametrize(
    ("fee", "fee_currency", "expected"),
    [(290, "myr", Decimal("2.90")), (290, "usd", None), (None, None, None)],
    ids=["myr", "not-myr", "missing"],
)
def test_credit_records_the_fee_only_when_it_is_in_myr(
    review_world: World, fee: int | None, fee_currency: str | None, expected: Decimal | None
) -> None:
    """第 5 步：手续费拿得到且为 MYR 时写入（写法同 A），否则为空；都不影响入账额。"""
    world = review_world
    payment = in_review(world)
    serve_paid(world, payment, fee=fee, fee_currency=fee_currency)

    resolve(world, payment, CREDIT)

    assert world.row(payment).gateway_fee == expected
    [topup] = world.topups(payment)
    assert topup.amount == Decimal("100")


def test_credit_accepts_the_payment_intent_already_recorded_on_the_payment(
    review_world: World,
) -> None:
    """第 4 步：支付行的 PI 等于 Stripe 返回的 PI → 照常入账。"""
    world = review_world
    intent_id = f"pi_test_{uuid.uuid4().hex}"
    payment = in_review(world, intent_id=intent_id)
    serve_paid(world, payment, intent_id=intent_id)

    resolve(world, payment, CREDIT)

    assert world.row(payment).status is PaymentStatus.PAID
    assert len(world.topups(payment)) == 1


def test_a_disabled_tenant_is_credited(review_world: World) -> None:
    """`DISABLED` 租户照常入账（与 A 一致）。"""
    world = review_world
    tenant = world.customer(account_status=AccountStatus.DISABLED)
    payment = in_review(world, tenant)
    serve_paid(world, payment)

    resolve(world, payment, CREDIT)

    assert world.row(payment).resolution is PaymentResolution.CREDITED
    assert len(world.topups(payment)) == 1


# --- CREDIT：第 1、2 步 -------------------------------------------------


@pytest.mark.parametrize("configured", [True, False], ids=["gateway", "no-gateway"])
def test_a_payment_without_a_gateway_reference_is_409(review_world: World, configured) -> None:
    """第 1 步：网关 id 为空 → 409 `GATEWAY_REFERENCE_MISSING`，不查 Stripe（先于网关检查）。"""
    world = review_world
    payment = in_review(world, with_session=False)
    before = world.snapshot(payment)
    gateway = _WORLD_GATEWAY if configured else None

    assert refused(world, payment, CREDIT, gateway=gateway) == (GATEWAY_REFERENCE_MISSING, 409)

    assert world.snapshot(payment) == before
    assert world.stripe.asked == []


def test_without_a_gateway_credit_is_503_and_close_still_works(review_world: World) -> None:
    """`gateway` 为 None：CREDIT 503 `PAYMENTS_MISCONFIGURED`、不写；CLOSE 照常。"""
    world = review_world
    payment = in_review(world)
    serve_paid(world, payment)
    before = world.snapshot(payment)

    assert refused(world, payment, CREDIT, gateway=None) == (PAYMENTS_MISCONFIGURED, 503)
    assert world.snapshot(payment) == before

    resolve(world, payment, CLOSE, gateway=None)
    assert world.row(payment).resolution is PaymentResolution.CLOSED
    assert world.stripe.asked == []


def test_stripe_unavailable_is_503_and_changes_nothing(review_world: World) -> None:
    """第 2 步：`GatewayUnavailable` → 503 `PAYMENT_GATEWAY_UNAVAILABLE`。"""
    world = review_world
    payment = in_review(world)
    serve_paid(world, payment)
    world.stripe.status_code = 503
    before = world.snapshot(payment)

    assert refused(world, payment, CREDIT) == (PAYMENT_GATEWAY_UNAVAILABLE, 503)

    assert world.snapshot(payment) == before
    assert world.stripe.asked == [payment.session_id]


def test_stripe_rejecting_the_query_is_409_and_changes_nothing(review_world: World) -> None:
    """第 2 步：`GatewayRequestRejected`（会话不存在）→ 409 `GATEWAY_QUERY_REJECTED`。"""
    world = review_world
    payment = in_review(world)
    before = world.snapshot(payment)

    assert refused(world, payment, CREDIT) == (GATEWAY_QUERY_REJECTED, 409)

    assert world.snapshot(payment) == before


# --- CREDIT：第 3 步的核对 ----------------------------------------------


def _without_intent_id(world: World, payment: PaymentRef) -> None:
    del serve_paid(world, payment)["payment_intent"]["id"]


STEP_3_CASES: list[tuple[str, Callable[[World, PaymentRef], object], str]] = [
    (
        "client-reference",
        lambda w, p: serve_paid(w, p, reference=str(uuid.uuid4())),
        GATEWAY_REFERENCE_MISMATCH,
    ),
    (
        "no-client-reference",
        lambda w, p: serve_paid(w, p, reference=None),
        GATEWAY_REFERENCE_MISMATCH,
    ),
    (
        "session-id",
        lambda w, p: serve_paid(w, p, session_id=f"cs_test_{uuid.uuid4().hex}"),
        GATEWAY_REFERENCE_MISMATCH,
    ),
    ("open", lambda w, p: serve_paid(w, p, state=GatewayState.OPEN), GATEWAY_NOT_PAID),
    (
        "inconsistent",
        lambda w, p: serve_paid(w, p, state=GatewayState.INCONSISTENT),
        GATEWAY_NOT_PAID,
    ),
    ("failed", lambda w, p: serve_paid(w, p, state=GatewayState.FAILED), GATEWAY_NOT_PAID),
    ("expired", lambda w, p: serve_paid(w, p, state=GatewayState.EXPIRED), GATEWAY_NOT_PAID),
    ("unknown", lambda w, p: serve_paid(w, p, state=GatewayState.UNKNOWN), GATEWAY_NOT_PAID),
    ("not-myr", lambda w, p: serve_paid(w, p, currency="usd"), CURRENCY_NOT_MYR),
    ("no-currency", lambda w, p: serve_paid(w, p, currency=None), CURRENCY_NOT_MYR),
    ("no-payment-intent-id", _without_intent_id, GATEWAY_NOT_PAID),
    ("no-latest-charge", lambda w, p: serve_paid(w, p, charge=False), GATEWAY_NOT_PAID),
    ("no-amount-refunded", lambda w, p: serve_paid(w, p, refunded=None), GATEWAY_NOT_PAID),
    ("no-disputed", lambda w, p: serve_paid(w, p, disputed=None), GATEWAY_NOT_PAID),
    ("partial-refund-1", lambda w, p: serve_paid(w, p, refunded=1), GATEWAY_REFUNDED_OR_DISPUTED),
    ("partial-refund", lambda w, p: serve_paid(w, p, refunded=5000), GATEWAY_REFUNDED_OR_DISPUTED),
    ("full-refund", lambda w, p: serve_paid(w, p, refunded=10000), GATEWAY_REFUNDED_OR_DISPUTED),
    ("disputed", lambda w, p: serve_paid(w, p, disputed=True), GATEWAY_REFUNDED_OR_DISPUTED),
    (
        "refunded-and-disputed",
        lambda w, p: serve_paid(w, p, refunded=10000, disputed=True),
        GATEWAY_REFUNDED_OR_DISPUTED,
    ),
    # 顺序：第一条不满足的那一条决定错误码。
    (
        "reference-before-state",
        lambda w, p: serve_paid(w, p, state=GatewayState.OPEN, reference=None),
        GATEWAY_REFERENCE_MISMATCH,
    ),
    (
        "state-before-currency",
        lambda w, p: serve_paid(w, p, state=GatewayState.INCONSISTENT, currency="usd"),
        GATEWAY_NOT_PAID,
    ),
    (
        "currency-before-refund",
        lambda w, p: serve_paid(w, p, currency="usd", refunded=10000, disputed=True),
        CURRENCY_NOT_MYR,
    ),
    (
        "charge-info-before-refund",
        lambda w, p: serve_paid(w, p, refunded=10000, disputed=None),
        GATEWAY_NOT_PAID,
    ),
]


@pytest.mark.parametrize(
    ("serve", "code"),
    [(serve, code) for _, serve, code in STEP_3_CASES],
    ids=[name for name, _, _ in STEP_3_CASES],
)
def test_every_row_of_the_gateway_check_refuses_and_changes_nothing(
    review_world: World, serve: Callable[[World, PaymentRef], object], code: str
) -> None:
    """第 3 步：按顺序核对，第一条不满足即 409 对应的码；

    支付、账本、审计与出站事件都不变。
    """
    world = review_world
    payment = in_review(world)
    serve(world, payment)
    before = world.snapshot(payment)

    assert refused(world, payment, CREDIT) == (code, 409)

    assert world.snapshot(payment) == before
    assert world.stripe.asked == [payment.session_id]


# --- CREDIT：第 4 步的重查 ----------------------------------------------


def test_a_payment_intent_held_by_another_payment_is_409(review_world: World) -> None:
    """第 4 步：PI 已被另一笔支付占用 → 409 `GATEWAY_PAYMENT_REUSED`。"""
    world = review_world
    intent_id = f"pi_test_{uuid.uuid4().hex}"
    world.payment(world.customer(), status=PaymentStatus.PAID, intent_id=intent_id)
    payment = in_review(world, reason="GATEWAY_PAYMENT_REUSED")
    serve_paid(world, payment, intent_id=intent_id)
    before = world.snapshot(payment)

    assert refused(world, payment, CREDIT) == (GATEWAY_PAYMENT_REUSED, 409)

    assert world.snapshot(payment) == before


def test_a_different_payment_intent_on_the_payment_is_409(review_world: World) -> None:
    """第 4 步：支付行已记下的 PI 与 Stripe 返回的不同 → 409 引用不符。"""
    world = review_world
    payment = in_review(world, intent_id=f"pi_test_{uuid.uuid4().hex}")
    serve_paid(world, payment)
    before = world.snapshot(payment)

    assert refused(world, payment, CREDIT) == (GATEWAY_REFERENCE_MISMATCH, 409)

    assert world.snapshot(payment) == before


@pytest.mark.parametrize(
    ("account_status", "billing_mode", "code"),
    [
        (AccountStatus.CLOSED, BillingMode.PREPAID, TENANT_CLOSED),
        (AccountStatus.ENABLED, BillingMode.INTERNAL_METERED_ONLY, INTERNAL_TENANT),
        # 顺序：关户先于内部租户。
        (AccountStatus.CLOSED, BillingMode.INTERNAL_METERED_ONLY, TENANT_CLOSED),
    ],
    ids=["closed", "internal", "closed-and-internal"],
)
def test_a_tenant_that_may_not_be_credited_is_409(
    review_world: World, account_status, billing_mode, code: str
) -> None:
    """第 4 步：关户 → `TENANT_CLOSED`；不是预付 → `INTERNAL_TENANT`。"""
    world = review_world
    tenant = world.customer(account_status=account_status, billing_mode=billing_mode)
    payment = in_review(world, tenant)
    serve_paid(world, payment)
    before = world.snapshot(payment)

    assert refused(world, payment, CREDIT) == (code, 409)

    assert world.snapshot(payment) == before


class _AfterQuery:
    """A gateway that runs `hook` after answering, before the caller opens its transaction."""

    def __init__(self, inner: PaymentGateway, hook: Callable[[], None]) -> None:
        self._inner = inner
        self._hook = hook

    @property
    def livemode(self) -> bool:
        return self._inner.livemode

    def get_payment_status(self, gateway_payment_id: str) -> GatewayPaymentStatus:
        reported = self._inner.get_payment_status(gateway_payment_id)
        self._hook()
        return reported


def test_a_payment_closed_after_the_query_is_409_and_not_credited(review_world: World) -> None:
    """第 4 步：查完 Stripe 后另一位管理员先结案 → 锁内重查前提，409。"""
    world = review_world
    payment = in_review(world)
    serve_paid(world, payment)
    gateway = _AfterQuery(world.stripe.gateway(), lambda: resolve(world, payment, CLOSE))

    assert refused(world, payment, CREDIT, gateway=gateway) == (PAYMENT_NOT_IN_REVIEW, 409)

    row = world.row(payment)
    assert (row.status, row.resolution) == (PaymentStatus.FAILED, PaymentResolution.CLOSED)
    assert world.topups() == []
    assert len(review_audits(world, payment)) == 1


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ({"account_status": AccountStatus.CLOSED}, TENANT_CLOSED),
        ({"billing_mode": BillingMode.INTERNAL_METERED_ONLY}, INTERNAL_TENANT),
    ],
    ids=["closed", "internal"],
)
def test_a_tenant_changed_after_the_query_is_409(
    review_world: World, change: dict[str, Any], code: str
) -> None:
    """租户在查询与加锁之间变了：以锁内读到的为准。"""
    world = review_world
    tenant = world.customer()
    payment = in_review(world, tenant)
    serve_paid(world, payment)

    def change_tenant() -> None:
        with world.factory() as session:
            session.execute(update(Tenant).where(Tenant.id == tenant.id).values(**change))
            session.commit()

    gateway = _AfterQuery(world.stripe.gateway(), change_tenant)
    before = world.snapshot(payment)

    assert refused(world, payment, CREDIT, gateway=gateway) == (code, 409)

    assert world.snapshot(payment) == before


# --- CREDIT：第 6 步 ----------------------------------------------------


def test_a_ledger_row_already_posted_for_the_payment_is_a_conflict(review_world: World) -> None:
    """第 6 步：`post_transaction` 返回重放（账本里已有同额 TOPUP）→ 回滚，409。"""
    world = review_world
    tenant = world.customer()
    payment = in_review(world, tenant)
    serve_paid(world, payment)
    post_directly(world, tenant, TransactionType.TOPUP, "100", reference=payment.public_id)
    before = world.snapshot(payment)

    assert refused(world, payment, CREDIT) == (RESOLUTION_CONFLICT, 409)

    assert world.snapshot(payment) == before
    assert review_audits(world, payment) == []


def test_a_ledger_row_with_another_amount_is_a_conflict(review_world: World) -> None:
    """第 6 步：`post_transaction` 抛 `LedgerConflict`（同一引用、不同金额）→ 409。"""
    world = review_world
    tenant = world.customer()
    payment = in_review(world, tenant)
    serve_paid(world, payment)
    post_directly(world, tenant, TransactionType.TOPUP, "50", reference=payment.public_id)
    before = world.snapshot(payment)

    assert refused(world, payment, CREDIT) == (RESOLUTION_CONFLICT, 409)

    assert world.snapshot(payment) == before


def test_a_payment_intent_taken_after_the_check_is_a_conflict(
    review_world: World, monkeypatch
) -> None:
    """第 6 步：写入撞 PI 的唯一约束（并发者在重查之后才占了它）→ 回滚，409。"""
    world = review_world
    intent_id = f"pi_test_{uuid.uuid4().hex}"
    world.payment(world.customer(), status=PaymentStatus.PAID, intent_id=intent_id)
    payment = in_review(world)
    serve_paid(world, payment, intent_id=intent_id)
    before = world.snapshot(payment)
    monkeypatch.setattr(service, "_locked_check_failure", lambda *args: None)

    assert refused(world, payment, CREDIT) == (RESOLUTION_CONFLICT, 409)

    assert world.snapshot(payment) == before


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        (LedgerConflict("REFERENCE_ALREADY_POSTED_WITH_DIFFERENT_PAYLOAD"), RESOLUTION_CONFLICT),
        (InvalidAmount("BALANCE_OUT_OF_RANGE"), BalanceOutOfRange),
        (InvalidAmount("AMOUNT_ZERO"), InvalidAmount),
        (InvalidTransaction("REFERENCE_TYPE_MISMATCH"), InvalidTransaction),
        (RuntimeError("injected ledger failure"), RuntimeError),
    ],
    ids=[
        "ledger-conflict",
        "balance-out-of-range",
        "invalid-amount",
        "invalid-transaction",
        "other",
    ],
)
def test_a_failure_while_posting_leaves_everything_untouched(
    review_world: World, monkeypatch, failure: Exception, expected: Any
) -> None:
    """设计 §7「事务中途失败」与第 6 步的映射：

    支付仍是 `REVIEW_REQUIRED`、无账本行、无审计。
    """
    world = review_world
    payment = in_review(world)
    serve_paid(world, payment)
    before = world.snapshot(payment)
    calls: list[str] = []

    def broken_post(session: Session, **options: object):
        calls.append(str(options["reference_id"]))
        raise failure

    monkeypatch.setattr(service, "post_transaction", broken_post)

    if expected == RESOLUTION_CONFLICT:
        assert refused(world, payment, CREDIT) == (RESOLUTION_CONFLICT, 409)
    elif expected is BalanceOutOfRange:
        assert refused(world, payment, CREDIT) == ("BALANCE_OUT_OF_RANGE", 422)
    else:
        with pytest.raises(expected) as raised:
            resolve(world, payment, CREDIT)
        assert raised.value is failure

    assert calls == [payment.public_id]
    assert world.snapshot(payment) == before
    assert world.row(payment).status is REVIEW
    assert review_audits(world, payment) == []


def test_a_failure_after_posting_rolls_the_topup_back_too(review_world: World, monkeypatch) -> None:
    """账本行已 flush、写审计时失败：账本行随整个事务回滚。"""
    world = review_world
    payment = in_review(world)
    serve_paid(world, payment)
    before = world.snapshot(payment)

    def broken_audit(*args: object, **options: object) -> None:
        raise RuntimeError("injected audit failure")

    monkeypatch.setattr(service, "_audit", broken_audit)

    with pytest.raises(RuntimeError, match="injected audit failure"):
        resolve(world, payment, CREDIT)

    assert world.snapshot(payment) == before


# --- 日志 ---------------------------------------------------------------


def test_the_reason_never_reaches_the_logs(review_world: World, caplog) -> None:
    """设计 §6：原因文本只进库与审计，任何日志都不记它（成功、拒绝、结案都一样）。"""
    world = review_world
    # 应用自己的日志（`app.*`）；SQLAlchemy 的 SQL 回显不在这条规则里。
    caplog.set_level(logging.DEBUG, logger="app")
    credited = in_review(world)
    serve_paid(world, credited, fee=290, fee_currency="usd")
    refunded = in_review(world)
    serve_paid(world, refunded, refunded=10000)
    closed = in_review(world)

    resolve(world, credited, CREDIT)
    with pytest.raises(AppError):
        resolve(world, refunded, CREDIT)
    resolve(world, closed, CLOSE)

    ours = [record for record in caplog.records if record.name == service.__name__]
    assert len(ours) >= 3
    pieces = [REASON, "Siti", "Q7XK2"]
    for record in caplog.records:
        rendered = record.getMessage() + " " + " ".join(map(repr, vars(record).values()))
        for piece in pieces:
            assert piece not in rendered, record.name
    # 日志里有 public_id 与错误码，排查用得上。
    assert any(getattr(record, "payment_id", None) == refunded.public_id for record in ours)
    assert any(
        getattr(record, "error_code", None) == GATEWAY_REFUNDED_OR_DISPUTED for record in ours
    )


# --- 真 MySQL：入账走账本与复机、余额越界 -------------------------------


def test_a_suspended_tenant_in_debt_is_credited_through_the_ledger_and_resumed(
    mysql_review_world: World,
) -> None:
    """设计 §7「人工入账走账本与复机」。

    余额 -5 的暂停租户人工入账 100 → 一条 TOPUP、余额 95、ACTIVE、复机
    outbox 与跃迁审计（都由 `post_transaction` 在同一事务里写）。
    """
    world = mysql_review_world
    tenant = world.customer()
    post_directly(
        world, tenant, TransactionType.ADJUSTMENT_DEBIT, "-5", reference=str(uuid.uuid4())
    )
    assert world.wallet(tenant) == (Decimal("-5"), 1, BillingStatus.SUSPENDED, 0)
    payment = in_review(world, tenant)
    serve_paid(world, payment)

    resolve(world, payment, CREDIT)

    [topup] = world.topups()
    assert (topup.balance_before, topup.amount, topup.balance_after) == (
        Decimal("-5"),
        Decimal("100"),
        Decimal("95"),
    )
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
    assert (transition.reason, transition.created_at) == (REASON_BALANCE_POSITIVE, RESOLVED_AT)
    [outbox] = events
    payload = json.loads(outbox.payload_json or "{}")
    assert (payload["billing_status"], payload["wallet_transaction"]) == (
        "ACTIVE",
        topup.public_id,
    )
    assert len(review_audits(world, payment)) == 1


def test_a_credit_that_would_overflow_the_balance_is_422_and_writes_nothing(
    mysql_review_world: World,
) -> None:
    """第 6 步：入账后余额越界 → 422 `BALANCE_OUT_OF_RANGE`；支付、账本与审计都不变。"""
    world = mysql_review_world
    tenant = world.customer()
    post_directly(
        world,
        tenant,
        TransactionType.ADJUSTMENT_CREDIT,
        "999999999999",
        reference=str(uuid.uuid4()),
    )
    payment = in_review(world, tenant)
    serve_paid(world, payment)
    before = world.snapshot(payment)

    with pytest.raises(BalanceOutOfRange) as raised:
        resolve(world, payment, CREDIT)

    assert (raised.value.code, raised.value.http_status) == ("BALANCE_OUT_OF_RANGE", 422)
    assert world.snapshot(payment) == before
    assert world.row(payment).status is REVIEW
    assert world.wallet(tenant)[0] == Decimal("999999999999")


# --- 真 MySQL：并发 -----------------------------------------------------


def _outcome(call: Callable[[], object]) -> object:
    try:
        return call()
    except AppError as error:
        return error.code


def test_two_admins_crediting_at_once_post_once(mysql_review_world: World) -> None:
    """设计 §7「人工入账并发」：两个管理员同时 CREDIT → 一条 TOPUP；后到者 409。"""
    world = mysql_review_world
    tenant = world.customer()
    payment = in_review(world, tenant)
    serve_paid(world, payment)
    start = threading.Barrier(2)

    def attempt(_index: int) -> object:
        start.wait(timeout=30)
        return _outcome(lambda: resolve(world, payment, CREDIT))

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(attempt, range(2)))

    assert sorted(map(str, outcomes)) == sorted([payment.public_id, PAYMENT_NOT_IN_REVIEW])
    assert len(world.topups()) == 1
    assert len(review_audits(world, payment)) == 1
    assert world.wallet(tenant) == (Decimal("100"), 1, BillingStatus.ACTIVE, 1)


def test_a_credit_racing_an_automatic_confirmation_posts_once(mysql_review_world: World) -> None:
    """设计 §7「人工入账并发」：CREDIT 与自动确认同时 → 只有一条 TOPUP。"""
    world = mysql_review_world
    tenant = world.customer()
    payment = in_review(world, tenant)
    serve_paid(world, payment)
    start = threading.Barrier(2)

    def credit() -> object:
        start.wait(timeout=30)
        return _outcome(lambda: resolve(world, payment, CREDIT))

    def confirm() -> object:
        gateway = world.stripe.gateway()
        start.wait(timeout=30)
        return confirm_payment(world.factory, gateway, payment.public_id, now=NOW)

    with ThreadPoolExecutor(max_workers=2) as pool:
        crediting = pool.submit(credit)
        confirming = pool.submit(confirm)
        credited = crediting.result(timeout=60)
        confirmed = confirming.result(timeout=60)

    assert credited == payment.public_id
    assert confirmed.posted is False
    assert confirmed.changed is False
    row = world.row(payment)
    assert (row.status, row.resolution) == (PaymentStatus.PAID, PaymentResolution.CREDITED)
    assert len(world.topups()) == 1
    assert world.wallet(tenant) == (Decimal("100"), 1, BillingStatus.ACTIVE, 1)


# --- 真 MySQL：人工入账与关户 -------------------------------------------

# 暂停点最多停这么久；「对方在等行锁」的观察上限（照 test_payment_confirm.py）。
GATE_TIMEOUT_SECONDS = 15.0
LOCK_POLL_LIMIT_SECONDS = 10.0
LOCK_WAIT_TIMEOUT_SECONDS = 20
JOIN_TIMEOUT_SECONDS = 40.0
CLOSED_AT = dt.datetime(2026, 11, 9, 10, 0, 5)

# ⚠️ 查 performance_schema.data_lock_waits，不查 INNODB_TRX（CLAUDE.md「写后端测试的约定」）。
_WAITING_ON_TABLE = text(
    "SELECT COUNT(*) FROM performance_schema.data_lock_waits AS w"
    " JOIN performance_schema.threads AS t ON t.THREAD_ID = w.REQUESTING_THREAD_ID"
    " JOIN performance_schema.data_locks AS l"
    " ON l.ENGINE = w.ENGINE AND l.ENGINE_LOCK_ID = w.REQUESTING_ENGINE_LOCK_ID"
    " WHERE t.PROCESSLIST_ID = :connection_id"
    " AND l.OBJECT_SCHEMA = DATABASE() AND l.OBJECT_NAME = :table_name"
)


def _locks(table: str) -> Callable[[str], bool]:
    """`SELECT … FROM <table> … FOR UPDATE`.

    整个用例只在 MySQL 上跑，匹配的是 MySQL 编译出的文本。
    """

    def matches(statement: str) -> bool:
        sql = " ".join(statement.replace("`", "").lower().split())
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
    url = mysql_url()
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
            .values(account_status=AccountStatus.CLOSED, updated_at=CLOSED_AT)
        )


def _wait_until_waiting(observer: Engine, connection_ids: list[int], table: str) -> None:
    """Poll until the account change waits on a `table` row lock (每轮一次查询，不 sleep)."""
    deadline = time.monotonic() + LOCK_POLL_LIMIT_SECONDS
    with observer.connect() as connection:
        while time.monotonic() < deadline:
            if connection_ids:
                waiting = connection.execute(
                    _WAITING_ON_TABLE, {"connection_id": connection_ids[-1], "table_name": table}
                ).scalar_one()
                if waiting:
                    return
            else:
                connection.execute(text("SELECT 1"))
    pytest.fail(f"the account change was not seen waiting for a {table} row lock")


def test_closing_waits_for_a_credit_holding_the_tenant_lock(
    mysql_review_world: World, engines: Callable[..., Engine]
) -> None:
    """设计 §7「人工入账 vs 关户」：CREDIT 持锁时另一连接关户 → 关户等入账提交。"""
    world = mysql_review_world
    tenant = world.customer()
    payment = in_review(world, tenant)
    serve_paid(world, payment)
    credit_engine, close_engine = engines(), engines()
    observer = engines(observer=True)
    gate = Gate(credit_engine, _locks("tenants"), after=True)
    connection_ids: list[int] = []

    with ThreadPoolExecutor(max_workers=2) as pool:
        try:
            crediting = pool.submit(
                resolve,
                world,
                payment,
                CREDIT,
                factory=create_session_factory(credit_engine),
            )
            gate.wait_reached()
            closing = pool.submit(_close_tenant, close_engine, tenant.id, connection_ids)
            _wait_until_waiting(observer, connection_ids, "tenants")
        finally:
            gate.release()
        credited = crediting.result(timeout=JOIN_TIMEOUT_SECONDS)
        closing.result(timeout=JOIN_TIMEOUT_SECONDS)

    assert credited == payment.public_id
    assert len(world.topups(payment)) == 1
    assert world.row(payment).resolution is PaymentResolution.CREDITED
    with world.factory() as session:
        row = session.get(Tenant, tenant.id)
        assert row is not None and row.account_status is AccountStatus.CLOSED


def test_a_credit_after_the_close_commits_is_409_tenant_closed(
    mysql_review_world: World, engines: Callable[..., Engine]
) -> None:
    """另一种先后：CREDIT 查完 Stripe、锁支付行之前停住；关户提交；放行 → 409。"""
    world = mysql_review_world
    tenant = world.customer()
    payment = in_review(world, tenant)
    serve_paid(world, payment)
    credit_engine, close_engine = engines(), engines()
    gate = Gate(credit_engine, _locks("payments"), after=False)

    with ThreadPoolExecutor(max_workers=1) as pool:
        try:
            crediting = pool.submit(
                _outcome,
                lambda: resolve(
                    world, payment, CREDIT, factory=create_session_factory(credit_engine)
                ),
            )
            gate.wait_reached()
            # 这时 Stripe 已查过（第 2 步在事务外）。
            assert world.stripe.asked == [payment.session_id]
            _close_tenant(close_engine, tenant.id, [])
        finally:
            gate.release()
        outcome = crediting.result(timeout=JOIN_TIMEOUT_SECONDS)

    assert outcome == TENANT_CLOSED
    assert world.topups() == []
    row = world.row(payment)
    assert (row.status, row.resolution) == (REVIEW, None)
    assert review_audits(world, payment) == []

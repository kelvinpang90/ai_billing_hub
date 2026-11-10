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
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import pytest
from payment_world import (
    AUDIT_FIELDS,
    CREATED,
    NOW,
    SECRET_KEY,
    PaymentRef,
    World,
    close_by_review,
    mysql_url,
)

# test_payment_events 与 test_payments_api 从本模块导入这几个名字（明确的再导出）。
from payment_world import EXPIRES as EXPIRES
from payment_world import WEBHOOK_SECRET as WEBHOOK_SECRET
from payment_world import audit_state as _state
from payment_world import build_mysql_world as _mysql_world
from payment_world import build_sqlite_world as _sqlite_world
from payment_world import stripe_session as stripe_session
from sqlalchemy import Engine, create_engine, event, select, text, update
from sqlalchemy.orm import Session

from app.core.database import create_session_factory
from app.models.auth import AuditAction, AuditLog, DomainOutbox
from app.models.payments import PaymentStatus
from app.models.tenancy import AccountStatus, BillingMode, BillingStatus, Tenant
from app.models.wallet import ReferenceType, TransactionType
from app.payments.base import GatewayRequestRejected, GatewayState, GatewayUnavailable
from app.repositories.wallet import (
    EVENT_BILLING_STATUS_CHANGED,
    REASON_BALANCE_POSITIVE,
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

LATER = dt.datetime(2026, 11, 8, 8, 25, 0)

LOGGER = "app.services.payments"

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


@pytest.mark.parametrize("state", list(GatewayState), ids=lambda s: s.value)
def test_a_payment_closed_by_review_is_never_changed(
    payment_world: World, state: GatewayState
) -> None:
    """设计闸门 #585 v3 §2「对 A 的 confirm_payment 的一处修改」：`resolution` 不为空（不入账结案，
    状态是 `FAILED`）→ 只更新 last_reconciled_at；收到 PAID 也不入账、不再进入复核。"""
    world = payment_world
    payment = world.payment(
        world.customer(), status=PaymentStatus.REVIEW_REQUIRED, reason=AMOUNT_MISMATCH
    )
    close_by_review(world, payment)
    _assert_unchanged(world, PaymentStatus.FAILED, state, payment=payment)


def _assert_unchanged(
    world: World,
    current: PaymentStatus,
    state: GatewayState,
    *,
    payment: PaymentRef | None = None,
) -> None:
    if payment is None:
        payment = world.payment(world.customer(), status=current, reason=PRIOR_REASON[current])
    world.serve(payment, state)
    reason = world.row(payment).status_reason
    before = world.snapshot(payment)

    result = world.confirm(payment)

    assert (result.old_status, result.new_status) == (current, current)
    assert (result.reason, result.posted, result.state) == (reason, False, state)
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

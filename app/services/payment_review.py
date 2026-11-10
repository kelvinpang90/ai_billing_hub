"""The two manual exits of a `REVIEW_REQUIRED` payment (AIH-TASK-182).

设计闸门 #585 v3 §2「人工复核出口」。`resolve_review` 是管理员对复核单的
唯一写入口（路由属于 AIH-TASK-183）：

- **CLOSE（不入账结案）**：一个事务，`SELECT … FOR UPDATE` 锁支付行、
  重查前提，置 `FAILED`（`status_reason` 保留进入复核时的原因）、写四个
  结案字段与 `PAYMENT_REVIEW_RESOLVED` 审计。不写账本，不需要网关
  （开关关闭时也能结案）。
- **CREDIT（按实付入账）**：不加锁检查前提与网关 id → **事务外**
  `get_payment_status` → 按设计表的顺序核对六条 → 新事务依次锁支付、
  钱包、租户（与 A 的顺序相同）并重查五条 → 写入、`post_transaction`
  （`TOPUP`，金额为 Stripe 实收额）、审计 → 提交。

检查顺序：先共同前提（404 `PAYMENT_NOT_FOUND`、409
`PAYMENT_NOT_IN_REVIEW`），CREDIT 再看网关 id 与 `gateway`（`None` 为 503
`PAYMENTS_MISCONFIGURED`）。核对或重查不过时什么都不写。

⚠️ **同一事务**（INV-13）：改状态、账本行（以及 `post_transaction` 一并写的
复机跃迁、审计与出站事件）、`PAYMENT_REVIEW_RESOLVED` 审计一起提交；任何
异常整个回滚。`post_transaction` 返回重放、抛 `LedgerConflict` 或写入撞
唯一约束（并发者先一步）→ 409 `RESOLUTION_CONFLICT`；余额越界 → 沿用
人工调账的 422 `BALANCE_OUT_OF_RANGE`；其他 `InvalidAmount` /
`InvalidTransaction` 是程序错误，原样抛出。

⚠️ 余额只经 `post_transaction` 的账本行变动（INV-4）；入账的租户以
`payments.tenant_id` 为准，并核对 Stripe 的 `client_reference_id`（INV-8）；
入账额是 `amount_received_minor / 100`，Decimal，不经 float（INV-10）。

⚠️ 日志只带 `public_id`、动作、错误码与管理员的用户 id；**不记原因文本**
（管理员的自由文本，只进库与审计，与人工调账同一规则，设计 §6）。
"""

from __future__ import annotations

import datetime as dt
import enum
import logging
from collections.abc import Mapping
from decimal import Decimal
from typing import Final

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.core.database import session_scope
from app.core.errors import AppError
from app.models.auth import AuditAction, AuditLog, User
from app.models.payments import Payment, PaymentResolution, PaymentStatus
from app.models.tenancy import AccountStatus, BillingMode, Tenant
from app.models.wallet import ReferenceType, TransactionType, Wallet
from app.payments.base import (
    GatewayPaymentStatus,
    GatewayRequestRejected,
    GatewayState,
    GatewayUnavailable,
    PaymentGateway,
    minor_to_amount,
)
from app.repositories.wallet import (
    InvalidAmount,
    LedgerConflict,
    _money_text,
    post_transaction,
)
from app.services.auth import RequestContext, utc_now
from app.services.payments import (
    PAYMENT_CURRENCY,
    _gateway_fee,
    _is_unique_violation,
    _json,
    _money,
)
from app.services.wallet_adjustments import BALANCE_OUT_OF_RANGE, BalanceOutOfRange

logger = logging.getLogger(__name__)

# 审计的 entity_type，与 app.services.payments.ENTITY_PAYMENT 同值
# （审计分类扫描只认本模块的字面量常量）。
_ENTITY_PAYMENT: Final = "payments"
# 与 AuditLog.user_agent 的列宽一致。
_USER_AGENT_LENGTH: Final = 512

# 设计 §2「人工复核出口」的错误码。
PAYMENT_NOT_FOUND: Final = "PAYMENT_NOT_FOUND"
PAYMENT_NOT_IN_REVIEW: Final = "PAYMENT_NOT_IN_REVIEW"
GATEWAY_REFERENCE_MISSING: Final = "GATEWAY_REFERENCE_MISSING"
GATEWAY_QUERY_REJECTED: Final = "GATEWAY_QUERY_REJECTED"
GATEWAY_REFERENCE_MISMATCH: Final = "GATEWAY_REFERENCE_MISMATCH"
GATEWAY_NOT_PAID: Final = "GATEWAY_NOT_PAID"
GATEWAY_REFUNDED_OR_DISPUTED: Final = "GATEWAY_REFUNDED_OR_DISPUTED"
CURRENCY_NOT_MYR: Final = "CURRENCY_NOT_MYR"
GATEWAY_PAYMENT_REUSED: Final = "GATEWAY_PAYMENT_REUSED"
TENANT_CLOSED: Final = "TENANT_CLOSED"
INTERNAL_TENANT: Final = "INTERNAL_TENANT"
RESOLUTION_CONFLICT: Final = "RESOLUTION_CONFLICT"
PAYMENTS_MISCONFIGURED: Final = "PAYMENTS_MISCONFIGURED"
PAYMENT_GATEWAY_UNAVAILABLE: Final = "PAYMENT_GATEWAY_UNAVAILABLE"

# 409 的固定文案：只说发生了什么，不带金额、原因或 Stripe 的数据。
_CONFLICT_MESSAGES: Final = {
    PAYMENT_NOT_IN_REVIEW: "The payment is not awaiting review.",
    GATEWAY_REFERENCE_MISSING: "The payment has no gateway reference.",
    GATEWAY_QUERY_REJECTED: "The payment gateway rejected the status query.",
    GATEWAY_REFERENCE_MISMATCH: "The gateway payment does not match this payment.",
    GATEWAY_NOT_PAID: "The payment gateway does not report this payment as paid.",
    GATEWAY_REFUNDED_OR_DISPUTED: "The gateway payment was refunded or is disputed.",
    CURRENCY_NOT_MYR: "The gateway payment is not in MYR.",
    GATEWAY_PAYMENT_REUSED: "The gateway payment was already credited to another payment.",
    TENANT_CLOSED: "The customer account is closed.",
    INTERNAL_TENANT: "The customer is not a prepaid account.",
    RESOLUTION_CONFLICT: "The payment was resolved concurrently. Refresh and try again.",
}


class ReviewAction(enum.StrEnum):
    """The two exits (设计 §2)."""

    CREDIT = "CREDIT"
    CLOSE = "CLOSE"


class ReviewPaymentNotFound(AppError):
    """No payment has this `public_id`."""

    code = PAYMENT_NOT_FOUND
    http_status = 404

    def __init__(self) -> None:
        super().__init__("Payment not found.")


class ReviewConflict(AppError):
    """A 409 of design §2; `code` is one of the conflict codes. Nothing was written."""

    http_status = 409

    def __init__(self, code: str) -> None:
        super().__init__(_CONFLICT_MESSAGES[code], code=code)


class ReviewPaymentsMisconfigured(AppError):
    """CREDIT without a gateway: payments are off or the credentials are unusable."""

    code = PAYMENTS_MISCONFIGURED
    http_status = 503

    def __init__(self) -> None:
        super().__init__("Payments are not configured.")


class ReviewGatewayUnavailable(AppError):
    """The gateway could not be asked. Retryable; nothing was written."""

    code = PAYMENT_GATEWAY_UNAVAILABLE
    http_status = 503

    def __init__(self) -> None:
        super().__init__("The payment gateway is unavailable. Please try again.")


def _now() -> dt.datetime:
    # MySQL 的 DATETIME 不存小数秒（与人工调账同一写法）。
    return utc_now().replace(microsecond=0)


def resolve_review(
    session_factory: sessionmaker[Session],
    gateway: PaymentGateway | None,
    *,
    actor: User,
    payment_id: str,
    action: ReviewAction | str,
    reason: str,
    context: RequestContext,
    now: dt.datetime | None = None,
) -> str:
    """Resolve one `REVIEW_REQUIRED` payment; return its `public_id`.

    `payment_id` 是 `payments.public_id`；`reason` 已由请求层去掉首尾空白、
    长度 1–255；`gateway` 由接口层传入（开关关闭或凭据读不到时为 `None`，
    只有 CREDIT 需要它）。`now` 是不带时区的 UTC、精确到秒，`resolved_at`、
    `paid_at`、账本行与审计都用它。
    """
    kind = ReviewAction(action)
    moment = now if now is not None else _now()
    # 共同前提：不加锁读一次（事务内再查一次）。读完就结束这个会话，
    # 之后才发网络请求。
    with session_factory() as session:
        row = session.execute(
            select(Payment.status, Payment.resolution, Payment.gateway_payment_id).where(
                Payment.public_id == payment_id
            )
        ).one_or_none()
    if row is None:
        raise ReviewPaymentNotFound
    status, resolution, gateway_payment_id = row
    if not _in_review(status, resolution):
        raise _refused(payment_id, kind, PAYMENT_NOT_IN_REVIEW)

    if kind is ReviewAction.CLOSE:
        _close(
            session_factory,
            actor=actor,
            payment_id=payment_id,
            reason=reason,
            context=context,
            now=moment,
        )
    else:
        _credit(
            session_factory,
            gateway,
            actor=actor,
            payment_id=payment_id,
            gateway_payment_id=gateway_payment_id,
            reason=reason,
            context=context,
            now=moment,
        )
    logger.info(
        "Payment review resolved",
        extra={"payment_id": payment_id, "action": kind.value, "admin_user_id": actor.id},
    )
    return payment_id


def _in_review(status: PaymentStatus, resolution: PaymentResolution | None) -> bool:
    return status is PaymentStatus.REVIEW_REQUIRED and resolution is None


def _refused(payment_id: str, action: ReviewAction, code: str) -> ReviewConflict:
    """Log a 409 and return it for the caller to raise. ⚠️ 不记原因文本。"""
    logger.warning(
        "Payment review was refused",
        extra={"payment_id": payment_id, "action": action.value, "error_code": code},
    )
    return ReviewConflict(code)


# --- CLOSE ------------------------------------------------------------------


def _close(
    session_factory: sessionmaker[Session],
    *,
    actor: User,
    payment_id: str,
    reason: str,
    context: RequestContext,
    now: dt.datetime,
) -> None:
    """One transaction: lock the payment row, re-check, `FAILED` + resolution fields + audit."""
    with session_scope(session_factory) as session:
        payment = _lock_payment(session, payment_id)
        if not _in_review(payment.status, payment.resolution):
            raise _refused(payment_id, ReviewAction.CLOSE, PAYMENT_NOT_IN_REVIEW)
        before = _before_state(payment)
        # `status_reason` 保留进入复核时的原因。
        payment.status = PaymentStatus.FAILED
        _mark_resolved(payment, PaymentResolution.CLOSED, actor=actor, reason=reason, now=now)
        _audit(session, payment, before, None, actor=actor, context=context, now=now)


# --- CREDIT -----------------------------------------------------------------


def _credit(
    session_factory: sessionmaker[Session],
    gateway: PaymentGateway | None,
    *,
    actor: User,
    payment_id: str,
    gateway_payment_id: str | None,
    reason: str,
    context: RequestContext,
    now: dt.datetime,
) -> None:
    # 第 1 步（共同前提已由调用方查过）。
    if gateway_payment_id is None:
        raise _refused(payment_id, ReviewAction.CREDIT, GATEWAY_REFERENCE_MISSING)
    if gateway is None:
        logger.warning(
            "Payment review credit needs the payment gateway, which is not configured",
            extra={"payment_id": payment_id, "error_code": PAYMENTS_MISCONFIGURED},
        )
        raise ReviewPaymentsMisconfigured

    # 第 2 步：事务外查 Stripe。
    try:
        reported = gateway.get_payment_status(gateway_payment_id)
    except GatewayUnavailable as error:
        logger.warning(
            "Gateway unavailable during a payment review credit",
            extra={"payment_id": payment_id, "error_code": error.code},
        )
        raise ReviewGatewayUnavailable from None
    except GatewayRequestRejected as error:
        logger.warning(
            "Gateway rejected the payment review status query",
            extra={"payment_id": payment_id, "error_code": error.code},
        )
        raise ReviewConflict(GATEWAY_QUERY_REJECTED) from None

    # 第 3 步：按顺序核对，第一条不满足即拒绝、什么都不写。
    failure = _gateway_check_failure(payment_id, gateway_payment_id, reported)
    if failure is not None:
        raise _refused(payment_id, ReviewAction.CREDIT, failure)
    assert reported.amount_received_minor is not None
    # 入账额 = 实收 / 100，Decimal（INV-10）；写法与 A 的 `confirmed_amount`
    # 相同（八位小数），审计里同一列前后不会一边两位、一边八位。
    credit_amount = _money(minor_to_amount(reported.amount_received_minor))

    # 第 4–6 步：一个事务。
    try:
        with session_scope(session_factory) as session:
            _credit_locked(
                session,
                reported,
                credit_amount,
                actor=actor,
                payment_id=payment_id,
                reason=reason,
                context=context,
                now=now,
            )
    except LedgerConflict:
        raise _refused(payment_id, ReviewAction.CREDIT, RESOLUTION_CONFLICT) from None
    except IntegrityError as error:
        # 撞 PI 或账本引用的唯一约束：并发者先一步。
        if not _is_unique_violation(error):
            raise
        raise _refused(payment_id, ReviewAction.CREDIT, RESOLUTION_CONFLICT) from None
    except InvalidAmount as error:
        if str(error) == BALANCE_OUT_OF_RANGE:
            logger.warning(
                "Payment review credit would put the balance out of range",
                extra={"payment_id": payment_id, "error_code": BALANCE_OUT_OF_RANGE},
            )
            raise BalanceOutOfRange from None
        raise


def _gateway_check_failure(
    payment_id: str, gateway_payment_id: str, reported: GatewayPaymentStatus
) -> str | None:
    """The code of the first unmet row of design §2 step 3, in order; `None` if all hold."""
    if reported.client_reference_id != payment_id or reported.session_id != gateway_payment_id:
        return GATEWAY_REFERENCE_MISMATCH
    # A 的映射：`PAID` 时实收等于应收、两者都不为空；实收为空只作防御。
    if reported.state is not GatewayState.PAID or reported.amount_received_minor is None:
        return GATEWAY_NOT_PAID
    if reported.currency != PAYMENT_CURRENCY:
        return CURRENCY_NOT_MYR
    if reported.payment_intent_id is None:
        return GATEWAY_NOT_PAID
    if reported.charge_amount_refunded_minor is None or reported.charge_disputed is None:
        return GATEWAY_NOT_PAID
    if reported.charge_amount_refunded_minor != 0 or reported.charge_disputed:
        return GATEWAY_REFUNDED_OR_DISPUTED
    return None


def _credit_locked(
    session: Session,
    reported: GatewayPaymentStatus,
    credit_amount: Decimal,
    *,
    actor: User,
    payment_id: str,
    reason: str,
    context: RequestContext,
    now: dt.datetime,
) -> None:
    """Steps 4 and 5 inside the caller's transaction. Flushes, never commits."""
    payment_intent_id = reported.payment_intent_id
    assert payment_intent_id is not None
    # 加锁顺序「支付 → 钱包 → 租户」，与 A 相同；`post_transaction` 再锁时
    # 拿到的是本事务已持有的锁。
    payment = _lock_payment(session, payment_id)
    session.execute(
        select(Wallet.id).where(Wallet.tenant_id == payment.tenant_id).with_for_update()
    )
    tenant = session.execute(
        select(Tenant)
        .where(Tenant.id == payment.tenant_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one()

    failure = _locked_check_failure(session, payment, tenant, payment_intent_id)
    if failure is not None:
        raise _refused(payment_id, ReviewAction.CREDIT, failure)

    before = _before_state(payment)
    # `status_reason` 保留进入复核时的原因。
    payment.status = PaymentStatus.PAID
    payment.confirmed_amount = credit_amount
    payment.confirmed_currency = PAYMENT_CURRENCY
    payment.paid_at = now
    payment.gateway_payment_intent_id = payment_intent_id
    payment.gateway_fee = _gateway_fee(payment, reported)
    payment.last_reconciled_at = now
    _mark_resolved(payment, PaymentResolution.CREDITED, actor=actor, reason=reason, now=now)
    outcome = post_transaction(
        session,
        tenant_id=payment.tenant_id,
        transaction_type=TransactionType.TOPUP,
        amount=credit_amount,
        reference_type=ReferenceType.PAYMENT,
        reference_id=payment.public_id,
        now=now,
        created_by=actor.id,
        description=reason,
        actor_role=actor.role.value,
        ip_address=context.ip_address,
        user_agent=context.user_agent,
    )
    if outcome.replayed:
        # 账本里已有这笔支付的 TOPUP：前面的锁与状态检查下不该出现。
        # 整个事务回滚。
        raise _refused(payment_id, ReviewAction.CREDIT, RESOLUTION_CONFLICT)
    _audit(
        session,
        payment,
        before,
        outcome.transaction.amount,
        actor=actor,
        context=context,
        now=now,
    )


def _locked_check_failure(
    session: Session, payment: Payment, tenant: Tenant, payment_intent_id: str
) -> str | None:
    """The code of the first unmet re-check of design §2 step 4, in its order (rows locked)."""
    if not _in_review(payment.status, payment.resolution):
        return PAYMENT_NOT_IN_REVIEW
    if payment.gateway_payment_intent_id not in (None, payment_intent_id):
        return GATEWAY_REFERENCE_MISMATCH
    taken = session.execute(
        select(Payment.id).where(
            Payment.gateway_payment_intent_id == payment_intent_id,
            Payment.id != payment.id,
        )
    ).first()
    if taken is not None:
        return GATEWAY_PAYMENT_REUSED
    if tenant.account_status is AccountStatus.CLOSED:
        return TENANT_CLOSED
    if tenant.billing_mode is not BillingMode.PREPAID:
        return INTERNAL_TENANT
    return None


# --- 共用 -------------------------------------------------------------------


def _lock_payment(session: Session, payment_id: str) -> Payment:
    # ⚠️ `populate_existing=True`：锁拿到之后读到的必须是最新提交的值。
    payment = session.execute(
        select(Payment)
        .where(Payment.public_id == payment_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if payment is None:
        raise ReviewPaymentNotFound
    return payment


def _mark_resolved(
    payment: Payment,
    resolution: PaymentResolution,
    *,
    actor: User,
    reason: str,
    now: dt.datetime,
) -> None:
    payment.resolution = resolution
    payment.resolved_at = now
    payment.resolved_by_user_id = actor.id
    payment.resolution_reason = reason
    payment.updated_at = now


def _before_state(payment: Payment) -> dict[str, object]:
    """Exactly the `before` fields of design §2 (`resolution` is null: the premise holds)."""
    return {
        "status": payment.status.value,
        "status_reason": payment.status_reason,
        "resolution": None,
        "confirmed_amount": _money_text(payment.confirmed_amount),
        "confirmed_currency": payment.confirmed_currency,
    }


def _after_state(payment: Payment, credited_amount: Decimal | None) -> dict[str, object]:
    """Exactly the `after` fields of design §2; `credited_amount` is null on CLOSE."""
    assert payment.resolution is not None
    return {
        "status": payment.status.value,
        "status_reason": payment.status_reason,
        "resolution": payment.resolution.value,
        "confirmed_amount": _money_text(payment.confirmed_amount),
        "confirmed_currency": payment.confirmed_currency,
        "credited_amount": _money_text(credited_amount),
        "resolution_reason": payment.resolution_reason,
    }


def _audit(
    session: Session,
    payment: Payment,
    before: Mapping[str, object],
    credited_amount: Decimal | None,
    *,
    actor: User,
    context: RequestContext,
    now: dt.datetime,
) -> None:
    user_agent = context.user_agent
    session.add(
        AuditLog(
            actor_user_id=actor.id,
            actor_role=actor.role.value,
            action=AuditAction.PAYMENT_REVIEW_RESOLVED,
            entity_type=_ENTITY_PAYMENT,
            entity_id=payment.public_id,
            before_state=_json(before),
            after_state=_json(_after_state(payment, credited_amount)),
            ip_address=context.ip_address,
            # 截到列宽：User-Agent 是客户端可控的。
            user_agent=user_agent[:_USER_AGENT_LENGTH] if user_agent else None,
            reason=payment.resolution_reason,
            created_at=now,
        )
    )


__all__ = [
    "CURRENCY_NOT_MYR",
    "GATEWAY_NOT_PAID",
    "GATEWAY_PAYMENT_REUSED",
    "GATEWAY_QUERY_REJECTED",
    "GATEWAY_REFERENCE_MISMATCH",
    "GATEWAY_REFERENCE_MISSING",
    "GATEWAY_REFUNDED_OR_DISPUTED",
    "INTERNAL_TENANT",
    "PAYMENTS_MISCONFIGURED",
    "PAYMENT_GATEWAY_UNAVAILABLE",
    "PAYMENT_NOT_FOUND",
    "PAYMENT_NOT_IN_REVIEW",
    "RESOLUTION_CONFLICT",
    "TENANT_CLOSED",
    "ReviewAction",
    "ReviewConflict",
    "ReviewGatewayUnavailable",
    "ReviewPaymentNotFound",
    "ReviewPaymentsMisconfigured",
    "resolve_review",
]

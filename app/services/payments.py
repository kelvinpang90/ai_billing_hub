"""Trusted payment confirmation and TOPUP posting (AIH-TASK-174).

设计闸门 #580 v3 §2「可信确认」。

`confirm_payment` 是唯一改支付状态的入口（建单之外）：本批的事件处理（AIH-TASK-175）调用它，
B 的补对账复用它。照设计五步：

1. 不加锁读 `payments`；`gateway_payment_id` 为空就返回「无可查询」（`state is None`），不改任何行；
2. **事务外**向网关查权威状态（不在持锁时发网络请求）；`GatewayUnavailable` 与
   `GatewayRequestRejected` 原样抛给调用方；
3. 新事务：`SELECT … FOR UPDATE` 依次锁支付行、该租户的钱包行、租户行（顺序「支付 → 钱包 → 租户」，
   与 `post_transaction` 的「钱包 → 租户」一致），再读一次支付状态与租户的 `account_status`、
   `billing_mode`。已是 `PAID` 或 `REVIEW_REQUIRED` 只更新 `last_reconciled_at`；
4. 按两张决策表落状态；状态每改变一次写一条 `PAYMENT_STATUS_CHANGE` 审计；不论是否改变都更新
   `last_reconciled_at`；
5. 提交。`post_transaction` 再锁钱包、租户时拿到的是本事务已持有的锁。

⚠️ **幂等的第三层**（INV-3）：锁住支付行后再判状态，已是 `PAID` 就什么也不做；绕过它的并发者还会被
账本 `(PAYMENT, public_id)` 唯一约束挡住。写入撞任何唯一约束（例如并发者先占了同一个 PI）：整个事务
回滚、支付与账本都不变，抛 `ConfirmConflict`，调用方按可重试处理。其他任何异常同样整个回滚后原样抛出
（INV-13）。

⚠️ 余额只经 `post_transaction` 的账本行变动（INV-4）：这里锁钱包行只为固定加锁顺序，不改它。

⚠️ 日志只带 `payments.public_id`、状态与原因码（设计 §6），不带金额、请求体与密钥。
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Final

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.core.database import session_scope
from app.models.auth import AuditAction, AuditLog
from app.models.base import MONEY_SCALE
from app.models.payments import Payment, PaymentStatus
from app.models.tenancy import AccountStatus, BillingMode, Tenant
from app.models.wallet import ReferenceType, TransactionType, Wallet
from app.payments.base import (
    GatewayPaymentStatus,
    GatewayState,
    PaymentGateway,
    amount_to_minor,
    minor_to_amount,
)
from app.repositories.wallet import SYSTEM_ACTOR_ROLE, post_transaction

logger = logging.getLogger(__name__)

# 审计的 `entity_type`；`entity_id` 是 `payments.public_id`
# （对外 id，见 app/services/audit_query.py）。
ENTITY_PAYMENT: Final = "payments"

# 设计 §2「状态原因」里由本函数写入的取值（`GATEWAY_CREATE_FAILED` 归建单）。
REFERENCE_MISMATCH: Final = "REFERENCE_MISMATCH"
CURRENCY_MISMATCH: Final = "CURRENCY_MISMATCH"
AMOUNT_MISMATCH: Final = "AMOUNT_MISMATCH"
GATEWAY_PAYMENT_REUSED: Final = "GATEWAY_PAYMENT_REUSED"
TENANT_CLOSED: Final = "TENANT_CLOSED"
INTERNAL_TENANT: Final = "INTERNAL_TENANT"
GATEWAY_INCONSISTENT: Final = "GATEWAY_INCONSISTENT"
GATEWAY_FAILED: Final = "GATEWAY_FAILED"
GATEWAY_EXPIRED: Final = "GATEWAY_EXPIRED"

# 本系统只收令吉（设计 §2：`expected_currency` CHECK = MYR）。
PAYMENT_CURRENCY: Final = "MYR"

# 「终结」只指这两种（设计 §4）：本函数不再改变它们。
FINAL_STATUSES: Final = frozenset({PaymentStatus.PAID, PaymentStatus.REVIEW_REQUIRED})

# 网关报 `FAILED` / `EXPIRED` 时只从 `PENDING` 跃迁（设计 §2 第二张表）；其他未终结状态不变。
_GATEWAY_ENDINGS: Final = {
    GatewayState.FAILED: (PaymentStatus.FAILED, GATEWAY_FAILED),
    GatewayState.EXPIRED: (PaymentStatus.EXPIRED, GATEWAY_EXPIRED),
}

_MONEY_QUANTUM = Decimal(1).scaleb(-MONEY_SCALE)
# MySQL 的 ER_DUP_ENTRY；SQLite 的唯一约束错误只能从消息认出。
_ER_DUP_ENTRY = 1062
_SQLITE_UNIQUE = "UNIQUE constraint failed"


class PaymentNotFound(LookupError):
    """No payment has this `public_id`. The message carries no business data."""


class ConfirmConflict(Exception):
    """A write hit a unique constraint; everything was rolled back. Retryable.

    典型情形：并发者先占了同一个 PaymentIntent。下一次判定时 `pi_taken` 查到占用而进入
    `REVIEW_REQUIRED`（设计 §2）。
    """


@dataclass(frozen=True)
class ConfirmResult:
    """What one confirmation did.

    `state` 是网关的权威状态；`None` = 「无可查询」（支付还没有网关 id，没有查询网关）。
    `reason` 是支付在本次之后的 `status_reason`。`posted` = 本次写了 `TOPUP` 账本行。
    """

    payment_id: str
    old_status: PaymentStatus
    new_status: PaymentStatus
    reason: str | None
    posted: bool
    state: GatewayState | None

    @property
    def nothing_to_query(self) -> bool:
        return self.state is None

    @property
    def changed(self) -> bool:
        return self.old_status is not self.new_status


def confirm_payment(
    session_factory: sessionmaker[Session],
    gateway: PaymentGateway,
    payment_public_id: str,
    *,
    now: dt.datetime,
) -> ConfirmResult:
    """Ask the gateway for the authoritative state of one payment and apply it (设计 §2 五步).

    `now` 是不带时区的 UTC、精确到秒；`paid_at`、`review_required_at`、`last_reconciled_at`、
    账本行与审计都用它。
    """
    # 第 1 步：不加锁读，读完就结束这个会话，之后才发网络请求。
    with session_factory() as session:
        row = session.execute(
            select(
                Payment.id, Payment.status, Payment.status_reason, Payment.gateway_payment_id
            ).where(Payment.public_id == payment_public_id)
        ).one_or_none()
    if row is None:
        raise PaymentNotFound("PAYMENT_NOT_FOUND")
    payment_id, status, reason, gateway_payment_id = row
    if gateway_payment_id is None:
        return ConfirmResult(payment_public_id, status, status, reason, False, None)

    # 第 2 步：事务外查网关。异常原样抛出，什么都没写。
    reported = gateway.get_payment_status(gateway_payment_id)
    if reported.state is GatewayState.UNKNOWN:
        logger.warning(
            "Gateway reported an unknown payment state",
            extra={"payment_id": payment_public_id, "state": reported.state.value},
        )

    # 第 3–5 步：一个事务。
    try:
        with session_scope(session_factory) as session:
            result = _apply(session, payment_id, reported, now=now)
    except IntegrityError as error:
        if not _is_unique_violation(error):
            raise
        logger.warning(
            "Payment confirmation hit a unique constraint and was rolled back",
            extra={"payment_id": payment_public_id, "state": reported.state.value},
        )
        raise ConfirmConflict("CONFIRM_CONFLICT") from None
    if result.changed:
        logger.info(
            "Payment status changed",
            extra={
                "payment_id": payment_public_id,
                "old_status": result.old_status.value,
                "new_status": result.new_status.value,
                "reason": result.reason,
                "state": reported.state.value,
            },
        )
    return result


def _apply(
    session: Session,
    payment_id: int,
    reported: GatewayPaymentStatus,
    *,
    now: dt.datetime,
) -> ConfirmResult:
    """Steps 3 and 4, inside the caller's transaction. Flushes through the ORM, never commits."""
    # ⚠️ `populate_existing=True`：锁拿到之后读到的必须是最新提交的值，不是会话里的旧对象。
    payment = session.execute(
        select(Payment)
        .where(Payment.id == payment_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one()
    session.execute(
        select(Wallet.id).where(Wallet.tenant_id == payment.tenant_id).with_for_update()
    )
    tenant = session.execute(
        select(Tenant)
        .where(Tenant.id == payment.tenant_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one()

    old_status = payment.status
    payment.last_reconciled_at = now
    if old_status in FINAL_STATUSES:
        # 幂等：`PAID` 与 `REVIEW_REQUIRED` 都不会被本函数改变（第 3 步）。
        return _unchanged(payment, reported)

    # PI 占用在锁内**先查**，对任何 state 都查
    # （设计 §2「第四条的占用查询不论前面几条结果如何都先做」）。
    pi_taken = _payment_intent_taken(session, payment, reported.payment_intent_id)
    before = _audit_state(payment)

    if reported.state is GatewayState.PAID:
        failure = _paid_check_failure(payment, tenant, reported, pi_taken=pi_taken)
        if failure is None:
            _mark_paid(payment, reported, now=now)
            outcome = post_transaction(
                session,
                tenant_id=payment.tenant_id,
                transaction_type=TransactionType.TOPUP,
                amount=payment.wallet_credit_amount,
                reference_type=ReferenceType.PAYMENT,
                reference_id=payment.public_id,
                now=now,
                actor_role=SYSTEM_ACTOR_ROLE,
            )
            _audit(session, payment, before, now=now)
            return _result(payment, old_status, reported, posted=not outcome.replayed)
        _mark_review(payment, reported, failure, pi_taken=pi_taken, now=now)
    elif reported.state is GatewayState.INCONSISTENT:
        _mark_review(payment, reported, GATEWAY_INCONSISTENT, pi_taken=pi_taken, now=now)
    elif reported.state in _GATEWAY_ENDINGS and old_status is PaymentStatus.PENDING:
        new_status, reason = _GATEWAY_ENDINGS[reported.state]
        payment.status = new_status
        payment.status_reason = reason
        payment.updated_at = now
    else:
        # `OPEN` / `UNKNOWN`，或 `EXPIRED` / `FAILED` / `CANCELLED`
        # 收到 `FAILED` / `EXPIRED`：不变。
        return _unchanged(payment, reported)

    _audit(session, payment, before, now=now)
    return _result(payment, old_status, reported, posted=False)


def _payment_intent_taken(
    session: Session, payment: Payment, payment_intent_id: str | None
) -> bool:
    """Another internal payment already holds this PaymentIntent (唯一索引上的普通读)."""
    if payment_intent_id is None:
        return False
    other = session.execute(
        select(Payment.id).where(
            Payment.gateway_payment_intent_id == payment_intent_id,
            Payment.id != payment.id,
        )
    ).first()
    return other is not None


def _paid_check_failure(
    payment: Payment,
    tenant: Tenant,
    reported: GatewayPaymentStatus,
    *,
    pi_taken: bool,
) -> str | None:
    """The reason of the first unmet condition, in the design's order; `None` when all hold.

    spec §43 的六条：签名已在落库时验过，其余五条（加内部租户）在这里，全部在任何写入之前判定。
    """
    if (
        reported.client_reference_id != payment.public_id
        or reported.session_id != payment.gateway_payment_id
    ):
        return REFERENCE_MISMATCH
    if reported.currency != PAYMENT_CURRENCY:
        return CURRENCY_MISMATCH
    # 精确的整数分比较，不经 float（设计 §2「金额与精度」）。
    if reported.amount_minor is None or reported.amount_minor != amount_to_minor(
        payment.expected_amount
    ):
        return AMOUNT_MISMATCH
    if pi_taken:
        return GATEWAY_PAYMENT_REUSED
    if tenant.account_status is AccountStatus.CLOSED:
        return TENANT_CLOSED
    if tenant.billing_mode is not BillingMode.PREPAID:
        return INTERNAL_TENANT
    return None


def _mark_paid(payment: Payment, reported: GatewayPaymentStatus, *, now: dt.datetime) -> None:
    # 判定已通过：币种是 MYR、金额等于建单金额，两者都不为空（`ck_payments_paid_fields`）。
    payment.status = PaymentStatus.PAID
    payment.status_reason = None
    payment.confirmed_amount = _confirmed_amount(reported)
    payment.confirmed_currency = reported.currency
    payment.paid_at = now
    payment.gateway_payment_intent_id = reported.payment_intent_id
    payment.gateway_fee = _gateway_fee(payment, reported)
    payment.updated_at = now


def _gateway_fee(payment: Payment, reported: GatewayPaymentStatus) -> Decimal | None:
    """`fee_minor / 100` when the fee is in MYR; `None` otherwise (设计 §2「金额与精度」)."""
    if reported.fee_minor is None:
        # 拿不到（例如余额流水还没生成）：为空、不记日志，由 B 补。
        return None
    if (reported.fee_currency or "").upper() != PAYMENT_CURRENCY:
        logger.warning(
            "Gateway fee is not in MYR; not recorded",
            extra={"payment_id": payment.public_id, "fee_currency": reported.fee_currency},
        )
        return None
    return _money(minor_to_amount(reported.fee_minor))


def _mark_review(
    payment: Payment,
    reported: GatewayPaymentStatus,
    reason: str,
    *,
    pi_taken: bool,
    now: dt.datetime,
) -> None:
    """`REVIEW_REQUIRED`, never posted; keep what the gateway reported for the human review."""
    payment.status = PaymentStatus.REVIEW_REQUIRED
    payment.status_reason = reason
    payment.review_required_at = now
    # 网关没给金额或币种时写空：CHECK 只要求 `PAID` 时非空。
    payment.confirmed_amount = _confirmed_amount(reported)
    payment.confirmed_currency = reported.currency
    # 只在没被别的支付占用时写 PI id（不论原因是哪一条）：复核分支因此不会撞唯一约束。
    if not pi_taken:
        payment.gateway_payment_intent_id = reported.payment_intent_id
    payment.updated_at = now
    logger.warning(
        "Payment needs review",
        extra={"payment_id": payment.public_id, "reason": reason, "state": reported.state.value},
    )


def _unchanged(payment: Payment, reported: GatewayPaymentStatus) -> ConfirmResult:
    return _result(payment, payment.status, reported, posted=False)


def _result(
    payment: Payment,
    old_status: PaymentStatus,
    reported: GatewayPaymentStatus,
    *,
    posted: bool,
) -> ConfirmResult:
    return ConfirmResult(
        payment_id=payment.public_id,
        old_status=old_status,
        new_status=payment.status,
        reason=payment.status_reason,
        posted=posted,
        state=reported.state,
    )


def _audit_state(payment: Payment) -> dict[str, object]:
    """Exactly the five fields design §2 allows in `before` / `after`."""
    return {
        "status": payment.status.value,
        "status_reason": payment.status_reason,
        "expected_amount": _money_text(payment.expected_amount),
        "confirmed_amount": _money_text(payment.confirmed_amount),
        "gateway_fee": _money_text(payment.gateway_fee),
    }


def _audit(
    session: Session,
    payment: Payment,
    before: Mapping[str, object],
    *,
    now: dt.datetime,
) -> None:
    session.add(
        AuditLog(
            actor_user_id=None,
            actor_role=SYSTEM_ACTOR_ROLE,
            action=AuditAction.PAYMENT_STATUS_CHANGE,
            entity_type=ENTITY_PAYMENT,
            entity_id=payment.public_id,
            before_state=_json(before),
            after_state=_json(_audit_state(payment)),
            reason=payment.status_reason,
            created_at=now,
        )
    )


def _confirmed_amount(reported: GatewayPaymentStatus) -> Decimal | None:
    """The amount the gateway reported, `amount_minor / 100`; `None` when it gave none."""
    if reported.amount_minor is None:
        return None
    return _money(minor_to_amount(reported.amount_minor))


def _money(value: Decimal) -> Decimal:
    # 与 DECIMAL(20,8) 读回来的写法一致，审计里同一列前后不会一边两位、一边八位。
    return value.quantize(_MONEY_QUANTUM)


def _money_text(value: Decimal | None) -> str | None:
    # 与 app/repositories/wallet.py 的 `_money_text` 同一写法：定点，不出现 "0E-8"。
    return None if value is None else format(value, "f")


def _json(value: Mapping[str, object]) -> str:
    return json.dumps(value, sort_keys=True)


def _is_unique_violation(error: IntegrityError) -> bool:
    original = error.orig
    args = getattr(original, "args", ())
    if args and args[0] == _ER_DUP_ENTRY:
        return True
    return _SQLITE_UNIQUE in str(original)


__all__ = [
    "AMOUNT_MISMATCH",
    "CURRENCY_MISMATCH",
    "ENTITY_PAYMENT",
    "FINAL_STATUSES",
    "GATEWAY_EXPIRED",
    "GATEWAY_FAILED",
    "GATEWAY_INCONSISTENT",
    "GATEWAY_PAYMENT_REUSED",
    "INTERNAL_TENANT",
    "PAYMENT_CURRENCY",
    "REFERENCE_MISMATCH",
    "TENANT_CLOSED",
    "ConfirmConflict",
    "ConfirmResult",
    "PaymentNotFound",
    "confirm_payment",
]

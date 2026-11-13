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

`create_topup`（AIH-TASK-176，设计 §2「建单」）是客户建单：照设计的校验顺序、60 秒同额复用的两步
（锁外预查、`lock_tenant_for_customer_write` 锁内重查）、两段事务与事务外调 Stripe。它只插入
`PENDING` 与写网关 id / `GATEWAY_CREATE_FAILED`，不改支付状态。

AIH-TASK-189（设计闸门 #587 v3 §2「对 A 的建单的修改」）：金额规则、复用窗口与待付超时改为在快速
校验开始时读一次支付设置（app/services/payment_settings.py）并整次使用；金额第 3 步加最小恢复额
（`TOPUP_BELOW_RECOVERY_MINIMUM`），锁内在复用查找之前用普通读的余额重判一次。

AIH-TASK-200（设计闸门 #589 v2 §2「对 A 的 confirm_payment 与 B 的 resolve_review 的修改」）：
判定通过、写 `PAID`、入账与审计之后、提交之前在同一事务里调用 `issue_receipt`
（`send_email=True`）；锁顺序变为「支付 → 钱包 → 租户 → 当年系列行」。
返回 `None` 时结果与提交不变；其他分支不调用。
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
import uuid
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Final

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError, MultipleResultsFound
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.core.database import session_scope
from app.core.errors import AppError
from app.models.auth import AuditAction, AuditLog
from app.models.base import MONEY_SCALE
from app.models.payments import Payment, PaymentStatus
from app.models.payments import PaymentGateway as GatewayName
from app.models.tax_policy import TaxJurisdiction, TaxPolicyType
from app.models.tenancy import AccountStatus, BillingMode, Tenant
from app.models.wallet import ReferenceType, TransactionType, Wallet
from app.payments.base import (
    CreatedPayment,
    GatewayPaymentStatus,
    GatewayRequestRejected,
    GatewayState,
    GatewayUnavailable,
    PaymentGateway,
    amount_to_minor,
    minor_to_amount,
)
from app.repositories.tax_policy import resolve_tax_policy
from app.repositories.wallet import SYSTEM_ACTOR_ROLE, post_transaction
from app.services.account_status import (
    READ_ONLY_STATUSES,
    AccountReadOnly,
    lock_tenant_for_customer_write,
)
from app.services.customers import CustomerNotFound
from app.services.payment_settings import (
    PaymentSettings,
    PaymentSettingsMissing,
    effective_minimum,
    get_payment_settings,
)
from app.services.receipts import issue_receipt

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
# 设计闸门 #585 v3 §2「状态原因」：由补对账（app/services/payment_reconcile.py）写入。
NO_GATEWAY_SESSION: Final = "NO_GATEWAY_SESSION"
RECONCILE_TIMEOUT: Final = "RECONCILE_TIMEOUT"

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
    if old_status in FINAL_STATUSES or payment.resolution is not None:
        # 幂等：`PAID` 与 `REVIEW_REQUIRED` 都不会被本函数改变（第 3 步）；已人工结案的也不会
        # （设计闸门 #585 v3 §2：不入账结案是 `FAILED`，自动入账等于推翻人工结论）。
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
            # 设计闸门 #589 v2 §2：同一事务开具收据；返回 None（检查不过）不改变结果，入账照常提交，
            # 由补发扫描再试。异常不捕获：随整个事务回滚。
            issue_receipt(
                session,
                payment=payment,
                payment_method=reported.payment_method_type,
                now=now,
                send_email=True,
            )
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


# --- 建单（AIH-TASK-176，设计 §2「建单」） --------------------------------------------------

# 建单时 Stripe 不可用或拒绝（设计 §2「状态原因」）。
GATEWAY_CREATE_FAILED: Final = "GATEWAY_CREATE_FAILED"
# Stripe 因同一幂等键正被并发使用而拒绝：先到的那个请求还在建，不写原因。
IDEMPOTENCY_KEY_IN_USE: Final = "idempotency_key_in_use"
# 复用要求剩余有效期不少于 31 分钟：Stripe 要求会话过期时刻在创建后至少 30 分钟。
REUSE_MIN_REMAINING: Final = dt.timedelta(minutes=31)
# 锁内重查至多两轮：第一轮找到并发产生的、已有网关 id 的一笔，回到事务外查询它；第二轮仍不能
# 复用就新建（设计 §2「60 秒同额复用」）。
_LOCKED_PASSES: Final = 2

# Stripe 单笔上限 999,999.99：至多六位整数、两位小数（设计 §2「建单」校验 3）。
# `[0-9]` 而不是 `\d`：全角数字等 Unicode 数字不算。
_AMOUNT_PATTERN: Final = re.compile(r"[0-9]{1,6}(\.[0-9]{1,2})?")
_CENT: Final = Decimal("0.01")


class PaymentsMisconfigured(AppError):
    """Payments are off, the gateway credentials are unusable, or `frontend_base_url` is empty."""

    code = "PAYMENTS_MISCONFIGURED"
    http_status = 503

    def __init__(self) -> None:
        super().__init__("Payments are not configured.")


class TopupsNotOpen(AppError):
    """`payment_topups_open` is off and the tenant is not on the allowlist."""

    code = "TOPUPS_NOT_OPEN"
    http_status = 403

    def __init__(self) -> None:
        super().__init__("Top-ups are not open for this account.")


class TopupNotAllowed(AppError):
    """The tenant is not `PREPAID` (内部计量租户不能充值)."""

    code = "TOPUP_NOT_ALLOWED"
    http_status = 403

    def __init__(self) -> None:
        super().__init__("This account cannot top up.")


class TopupAmountInvalid(AppError):
    """Not a string, not in the amount format, or outside the minimum and maximum of the
    payment settings."""

    code = "TOPUP_AMOUNT_INVALID"
    http_status = 422

    def __init__(self) -> None:
        super().__init__("The top-up amount is invalid.")


class TopupBelowRecoveryMinimum(AppError):
    """A suspended `PREPAID` tenant's amount is below `max(min_amount, debt + buffer)`.

    金额本身由充值选项（AIH-TASK-190）给出，不放在错误里（错误信封没有附加字段）。
    """

    code = "TOPUP_BELOW_RECOVERY_MINIMUM"
    http_status = 422

    def __init__(self) -> None:
        super().__init__("The top-up amount is below the minimum needed to restore the service.")


class TaxPolicyUnavailable(AppError):
    """No tax policy version is in effect, or the one in effect fails its check on read."""

    code = "TAX_POLICY_UNAVAILABLE"
    http_status = 503

    def __init__(self) -> None:
        super().__init__("The tax policy is unavailable.")


class TaxPolicyUnsupported(AppError):
    """The version in effect charges tax (ADR-0008：未注册、不收税；收税另过设计闸门)."""

    code = "TAX_POLICY_UNSUPPORTED"
    http_status = 503

    def __init__(self) -> None:
        super().__init__("The tax policy in effect is not supported for top-ups.")


class PaymentGatewayUnavailable(AppError):
    """The gateway could not create or report the payment page.

    可重试：60 秒内重试复用同一笔、用同一个幂等键。
    """

    code = "PAYMENT_GATEWAY_UNAVAILABLE"
    http_status = 503

    def __init__(self) -> None:
        super().__init__("The payment gateway is unavailable. Please try again.")


@dataclass(frozen=True)
class TopupResult:
    """What the customer gets back: the payment, where to pay, until when (INV-7：只有这三项)."""

    payment_public_id: str
    redirect_url: str
    expires_at: dt.datetime


@dataclass(frozen=True)
class _Candidate:
    """A `PENDING` payment the reuse query found."""

    id: int
    public_id: str
    gateway_payment_id: str | None
    expires_at: dt.datetime


def create_topup(
    session_factory: sessionmaker[Session],
    gateway_factory: Callable[[Settings], PaymentGateway | None],
    tenant_id: int,
    amount_text: object,
    *,
    now: dt.datetime,
    settings: Settings,
) -> TopupResult:
    """Create (or reuse) a `PENDING` top-up and its Stripe payment page (设计 §2「建单」).

    `tenant_id` 来自客户令牌主体（INV-8）；`amount_text` 是请求体里的原值（不是字符串也是
    `TopupAmountInvalid`）。`now` 是不带时区的 UTC、精确到秒。

    顺序：配置 → 锁外快速校验（读支付设置；账户可写、开放、计费模式、金额、税务政策）→ 锁外预查
    候选并在事务外查询它的会话 → 锁租户的事务里重查（含最小恢复额）并复用或插入 `PENDING`、
    提交 → 事务外调 Stripe → 新事务写网关 id。
    """
    if not settings.payments_enabled:
        raise PaymentsMisconfigured
    base_url = settings.frontend_base_url.strip().rstrip("/")
    if not base_url:
        raise PaymentsMisconfigured
    gateway = gateway_factory(settings)
    if gateway is None:
        raise PaymentsMisconfigured

    # 锁外快速校验与预查。没有生效的税务政策就在复用之前报错。
    with session_factory() as session:
        # 读一次、整次使用：锁内不再读，同一次建单前后不会用两份设置（设计闸门 #587 v3 §2）。
        rules = _payment_settings(session)
        tenant = session.get(Tenant, tenant_id)
        if tenant is None:
            raise CustomerNotFound
        if tenant.account_status in READ_ONLY_STATUSES:
            raise AccountReadOnly
        _check_tenant(tenant, settings)
        amount = _parse_amount(
            amount_text,
            rules,
            balance=_wallet_balance(session, tenant_id),
            billing_mode=tenant.billing_mode,
        )
        _tax_policy_version_id(session, now)
        candidate = _reusable(session, tenant_id, amount, now=now, rules=rules, excluded=())

    excluded: set[int] = set()
    if candidate is not None and candidate.gateway_payment_id is not None:
        # 直接复用一笔开着的候选时不再重判恢复额：它是窗口内同金额建的，建它时已按当时的余额
        # 校验过，快速校验也刚用当前余额判过一次。
        reused = _reuse_open_session(gateway, candidate)
        if reused is not None:
            return reused
        excluded.add(candidate.id)

    payment: Payment | None = None
    for locked_pass in range(1, _LOCKED_PASSES + 1):
        with session_scope(session_factory) as session:
            found = _locked_pass(
                session,
                tenant_id,
                amount,
                now=now,
                settings=settings,
                rules=rules,
                excluded=excluded,
                may_query=locked_pass < _LOCKED_PASSES,
            )
        if isinstance(found, Payment):
            payment = found
            break
        # 并发产生的、已有网关 id 的一笔：提交之后回到事务外查询它。
        reused = _reuse_open_session(gateway, found)
        if reused is not None:
            return reused
        excluded.add(found.id)
    # 最后一轮总是复用没有网关 id 的一笔或新建。
    assert payment is not None
    return _create_on_gateway(session_factory, gateway, payment, base_url=base_url, now=now)


def _check_tenant(tenant: Tenant, settings: Settings) -> None:
    """Checks 1a and 2, in that order (账户可写由调用方先查)."""
    if not settings.payment_topups_open and (
        tenant.public_id.lower() not in settings.payment_topup_tenant_ids
    ):
        raise TopupsNotOpen
    if tenant.billing_mode is not BillingMode.PREPAID:
        raise TopupNotAllowed


def _payment_settings(session: Session) -> PaymentSettings:
    try:
        return get_payment_settings(session)
    except PaymentSettingsMissing:
        logger.error("Payment settings are missing; top-ups are refused")
        raise PaymentsMisconfigured from None


def _wallet_balance(session: Session, tenant_id: int) -> Decimal | None:
    """The committed wallet balance, a plain read (不加锁，不改变 A 的加锁顺序)."""
    return session.execute(
        select(Wallet.balance).where(Wallet.tenant_id == tenant_id)
    ).scalar_one_or_none()


def _parse_amount(
    amount_text: object,
    rules: PaymentSettings,
    *,
    balance: Decimal | None,
    billing_mode: BillingMode,
) -> Decimal:
    """Check 3, first failure wins: the format, `≥ min_amount`, `≥ effective_minimum`,
    `≤ max_amount` (设计闸门 #587 v3 §2). Two decimal places."""
    if type(amount_text) is not str or not _AMOUNT_PATTERN.fullmatch(amount_text):
        raise TopupAmountInvalid
    amount = Decimal(amount_text).quantize(_CENT)
    if amount < rules.min_amount:
        raise TopupAmountInvalid
    _check_recovery(amount, rules, balance=balance, billing_mode=billing_mode)
    maximum = rules.max_amount
    if maximum is not None and amount > maximum:
        raise TopupAmountInvalid
    return amount


def _check_recovery(
    amount: Decimal,
    rules: PaymentSettings,
    *,
    balance: Decimal | None,
    billing_mode: BillingMode,
) -> None:
    """Check 3c: not below the effective minimum. 没有钱包行就没有欠款，只按最低额判。"""
    if balance is None:
        return
    if amount < effective_minimum(balance, billing_mode, rules):
        raise TopupBelowRecoveryMinimum


def _tax_policy_version_id(session: Session, now: dt.datetime) -> int:
    """Check 4: the SST / Malaysia version in effect at `now` (INV-6). Fail closed."""
    try:
        policy = resolve_tax_policy(session, TaxJurisdiction.MY, TaxPolicyType.SST, now)
    except (ValueError, MultipleResultsFound):
        logger.error("Tax policy for top-ups failed its check on read")
        raise TaxPolicyUnavailable from None
    if policy is None:
        logger.error("No tax policy is in effect for top-ups")
        raise TaxPolicyUnavailable
    if policy.treatment.tax_charged:
        logger.error(
            "Tax policy in effect charges tax; top-ups do not support it",
            extra={"tax_policy_version": policy.public_id},
        )
        raise TaxPolicyUnsupported
    return policy.version_id


def _reusable(
    session: Session,
    tenant_id: int,
    amount: Decimal,
    *,
    now: dt.datetime,
    rules: PaymentSettings,
    excluded: Collection[int],
) -> _Candidate | None:
    """The newest `PENDING` of this tenant and amount created within the reuse window and with
    at least 31 minutes left (ADR-0007); `None` when the window is 0. A plain read: 建单不锁已有
    支付行（设计 §2「可信确认」第 5 步）。窗口来自支付设置。"""
    window = rules.reuse_window_seconds
    if window <= 0:
        return None
    statement = select(
        Payment.id, Payment.public_id, Payment.gateway_payment_id, Payment.expires_at
    ).where(
        Payment.tenant_id == tenant_id,
        Payment.status == PaymentStatus.PENDING,
        Payment.expected_amount == amount,
        Payment.created_at >= now - dt.timedelta(seconds=window),
        Payment.expires_at >= now + REUSE_MIN_REMAINING,
    )
    if excluded:
        statement = statement.where(Payment.id.not_in(list(excluded)))
    row = session.execute(
        statement.order_by(Payment.created_at.desc(), Payment.id.desc()).limit(1)
    ).one_or_none()
    if row is None:
        return None
    return _Candidate(int(row.id), row.public_id, row.gateway_payment_id, row.expires_at)


def _reuse_open_session(gateway: PaymentGateway, candidate: _Candidate) -> TopupResult | None:
    """Ask the gateway about a candidate's session, outside any transaction.

    `OPEN` 且有 `redirect_url`：复用完成。其他状态或 `GatewayRequestRejected`：`None`（调用方记为
    排除）。`GatewayUnavailable`：`PaymentGatewayUnavailable`。
    """
    assert candidate.gateway_payment_id is not None
    try:
        reported = gateway.get_payment_status(candidate.gateway_payment_id)
    except GatewayUnavailable as error:
        logger.warning(
            "Gateway unavailable while checking a top-up for reuse",
            extra={"payment_id": candidate.public_id, "error_code": error.code},
        )
        raise PaymentGatewayUnavailable from None
    except GatewayRequestRejected as error:
        logger.warning(
            "Gateway rejected the reuse check; the top-up is not reused",
            extra={"payment_id": candidate.public_id, "error_code": error.code},
        )
        return None
    if reported.state is GatewayState.OPEN and reported.redirect_url:
        logger.info("Top-up reused", extra={"payment_id": candidate.public_id})
        return TopupResult(candidate.public_id, reported.redirect_url, candidate.expires_at)
    logger.info(
        "Top-up session is no longer open; not reused",
        extra={"payment_id": candidate.public_id, "state": reported.state.value},
    )
    return None


def _locked_pass(
    session: Session,
    tenant_id: int,
    amount: Decimal,
    *,
    now: dt.datetime,
    settings: Settings,
    rules: PaymentSettings,
    excluded: Collection[int],
    may_query: bool,
) -> Payment | _Candidate:
    """In the caller's write transaction: lock the tenant, re-check, then reuse or insert.

    返回要调 Stripe 的支付（复用的、没有网关 id 的一笔，或新插入的 `PENDING`，都已脱离会话），
    或一个已有网关 id 的候选（`may_query` 为真时：调用方提交后在事务外查询它）。并发的两个请求在
    租户锁上排队，后到者找到先到者刚插入的一笔而复用它。

    最小恢复额用锁内普通读到的余额重判，排在复用查找之前：不过就既不复用、也不新建
    （设计闸门 #587 v3 §2）。余额已被另一笔入账变正时按普通最低额判。
    """
    tenant = lock_tenant_for_customer_write(session, tenant_id)
    _check_tenant(tenant, settings)
    _check_recovery(
        amount,
        rules,
        balance=_wallet_balance(session, tenant_id),
        billing_mode=tenant.billing_mode,
    )
    tax_policy_version_id = _tax_policy_version_id(session, now)
    found = _reusable(session, tenant_id, amount, now=now, rules=rules, excluded=excluded)
    if found is not None and found.gateway_payment_id is not None and may_query:
        return found
    if found is not None and found.gateway_payment_id is None:
        payment = session.execute(select(Payment).where(Payment.id == found.id)).scalar_one()
    else:
        payment = Payment(
            public_id=str(uuid.uuid4()),
            tenant_id=tenant_id,
            gateway=GatewayName.STRIPE,
            expected_amount=amount,
            expected_currency=PAYMENT_CURRENCY,
            # ADR-0008：入账额等于含税毛额；未注册 SST，税额为空。
            wallet_credit_amount=amount,
            tax_policy_version_id=tax_policy_version_id,
            tax_amount=None,
            status=PaymentStatus.PENDING,
            status_reason=None,
            expires_at=now + dt.timedelta(seconds=rules.pending_timeout_seconds),
            created_at=now,
            updated_at=now,
        )
        session.add(payment)
        session.flush()
        logger.info("Top-up created", extra={"payment_id": payment.public_id})
    # 脱离会话：提交不让它过期，事务外调 Stripe 时照样读得到。
    session.expunge(payment)
    return payment


def _create_on_gateway(
    session_factory: sessionmaker[Session],
    gateway: PaymentGateway,
    payment: Payment,
    *,
    base_url: str,
    now: dt.datetime,
) -> TopupResult:
    """Call `create_payment` holding no lock, then record the outcome in a new transaction."""
    try:
        created = gateway.create_payment(
            payment,
            success_url=_return_url(base_url, payment.public_id, "success"),
            cancel_url=_return_url(base_url, payment.public_id, "cancelled"),
        )
    except (GatewayUnavailable, GatewayRequestRejected) as error:
        in_use = error.code == IDEMPOTENCY_KEY_IN_USE
        if not in_use:
            _record_create_failed(session_factory, payment.id, now=now)
        logger.warning(
            "Gateway could not create the top-up payment page",
            extra={"payment_id": payment.public_id, "error_code": error.code},
        )
        raise PaymentGatewayUnavailable from None
    _record_gateway_id(session_factory, payment, created, now=now)
    return TopupResult(payment.public_id, created.redirect_url, payment.expires_at)


def _return_url(base_url: str, public_id: str, result: str) -> str:
    # 跳回门户本身不改任何状态（spec §43）。
    return f"{base_url}/portal/payments/{public_id}?result={result}"


def _record_create_failed(
    session_factory: sessionmaker[Session], payment_id: int, *, now: dt.datetime
) -> None:
    """`status_reason = GATEWAY_CREATE_FAILED`, only while there is no gateway id; stays PENDING."""
    with session_scope(session_factory) as session:
        session.execute(
            update(Payment)
            .where(Payment.id == payment_id, Payment.gateway_payment_id.is_(None))
            .values(status_reason=GATEWAY_CREATE_FAILED, updated_at=now)
            .execution_options(synchronize_session=False)
        )


def _record_gateway_id(
    session_factory: sessionmaker[Session],
    payment: Payment,
    created: CreatedPayment,
    *,
    now: dt.datetime,
) -> None:
    """Write the gateway id where there is none and clear the reason.

    影响 0 行：并发的复用请求已写；读出比较，相同即可。不同（同一幂等键正常不会出现）：记 ERROR、
    `PaymentGatewayUnavailable`，客户重试时复用库里那一笔。
    """
    with session_scope(session_factory) as session:
        written = session.execute(
            update(Payment)
            .where(Payment.id == payment.id, Payment.gateway_payment_id.is_(None))
            .values(
                gateway_payment_id=created.gateway_payment_id,
                status_reason=None,
                updated_at=now,
            )
            .execution_options(synchronize_session=False)
        ).rowcount
        stored = (
            created.gateway_payment_id
            if written
            else session.execute(
                select(Payment.gateway_payment_id).where(Payment.id == payment.id)
            ).scalar_one()
        )
    if stored != created.gateway_payment_id:
        logger.error(
            "Top-up already holds a different gateway payment id",
            extra={"payment_id": payment.public_id},
        )
        raise PaymentGatewayUnavailable


__all__ = [
    "AMOUNT_MISMATCH",
    "CURRENCY_MISMATCH",
    "ENTITY_PAYMENT",
    "FINAL_STATUSES",
    "GATEWAY_CREATE_FAILED",
    "GATEWAY_EXPIRED",
    "GATEWAY_FAILED",
    "GATEWAY_INCONSISTENT",
    "GATEWAY_PAYMENT_REUSED",
    "IDEMPOTENCY_KEY_IN_USE",
    "INTERNAL_TENANT",
    "NO_GATEWAY_SESSION",
    "PAYMENT_CURRENCY",
    "RECONCILE_TIMEOUT",
    "REFERENCE_MISMATCH",
    "REUSE_MIN_REMAINING",
    "TENANT_CLOSED",
    "ConfirmConflict",
    "ConfirmResult",
    "PaymentGatewayUnavailable",
    "PaymentNotFound",
    "PaymentsMisconfigured",
    "TaxPolicyUnavailable",
    "TaxPolicyUnsupported",
    "TopupAmountInvalid",
    "TopupBelowRecoveryMinimum",
    "TopupNotAllowed",
    "TopupResult",
    "TopupsNotOpen",
    "confirm_payment",
    "create_topup",
]

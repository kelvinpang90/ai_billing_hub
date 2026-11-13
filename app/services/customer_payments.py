"""Customer payment queries: top-up options, the payment list and one payment (AIH-TASK-190).

设计闸门 #587 v3 §2「客户接口」「最小恢复额」、§6；
契约见 [docs/api.md](../../docs/api.md)「支付（客户）」。

只读：调用方给一个普通会话，这里不加锁、不提交、不写审计，也不写日志。租户一律是调用方传进来的
`tenant_id`（来自客户令牌主体，INV-8），每个查询都带这个条件。

- `topup_options`：只有它读支付设置（读不到 → `PaymentSettingsMissing`，503
  `PAYMENTS_MISCONFIGURED`）。不可充值的原因按设计的五条顺序取第一个成立的，开放判断与 A 的建单
  相同（含允许名单）；最小恢复额与生效的最低额调用 app/services/payment_settings.py，余额是普通读。
  不查 Stripe、不读密钥文件。
- `list_payments` / `get_payment`：不读设置，设置行缺失时照常返回。`credited_amount` 是这笔支付的
  `TOPUP` 账本行（`(PAYMENT, public_id)` 唯一，至多一行）；客户侧状态由 `customer_status` 映射，
  原始状态、原因、手续费、结案字段与网关编号都不出这里之外的视图（INV-7，白名单在
  app/schemas/customer_payments.py）。每一项另带这笔支付的收据 `public_id` 与收据号（左连接
  `payment_receipts`，`payment_id` 唯一，没有为 None；设计闸门 #589 v2 §2「客户接口」，
  AIH-TASK-202）。
- `list_receipts`（AIH-TASK-202）：本租户的收据，`issued_at` 降序、`id` 降序；只读列，不读快照。
  收据下载不在这里：读快照与渲染在 app/api/receipt_download.py（管理端共用）。

⚠️ 金额一律是 `Decimal`，比较不经 `float`（INV-10）。
"""

from __future__ import annotations

import datetime as dt
import enum
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import Select, and_, func, select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.models.payments import Payment, PaymentResolution, PaymentStatus
from app.models.receipts import PaymentReceipt
from app.models.tenancy import BillingMode, Tenant
from app.models.wallet import ReferenceType, TransactionType, Wallet, WalletTransaction
from app.services.account_status import READ_ONLY_STATUSES
from app.services.customers import CustomerNotFound
from app.services.payment_settings import (
    PaymentSettings,
    effective_minimum,
    get_payment_settings,
    recovery_exceeds_cap,
    recovery_minimum,
)


class UnavailableReason(enum.StrEnum):
    """Why the caller cannot top up, in the order they are judged (设计 §2「客户接口」)."""

    PAYMENTS_DISABLED = "PAYMENTS_DISABLED"
    ACCOUNT_READ_ONLY = "ACCOUNT_READ_ONLY"
    TOPUPS_NOT_OPEN = "TOPUPS_NOT_OPEN"
    TOPUP_NOT_ALLOWED = "TOPUP_NOT_ALLOWED"
    RECOVERY_EXCEEDS_MAXIMUM = "RECOVERY_EXCEEDS_MAXIMUM"


class CustomerPaymentStatus(enum.StrEnum):
    """The status a customer sees (设计 §2 的七行映射)."""

    PENDING = "PENDING"
    PAID = "PAID"
    IN_REVIEW = "IN_REVIEW"
    NOT_CREDITED = "NOT_CREDITED"
    FAILED = "FAILED"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"


_STATUS_MAP = {
    PaymentStatus.PENDING: CustomerPaymentStatus.PENDING,
    # 含人工按实付入账（`resolution = CREDITED`）。
    PaymentStatus.PAID: CustomerPaymentStatus.PAID,
    PaymentStatus.REVIEW_REQUIRED: CustomerPaymentStatus.IN_REVIEW,
    PaymentStatus.FAILED: CustomerPaymentStatus.FAILED,
    PaymentStatus.EXPIRED: CustomerPaymentStatus.EXPIRED,
    PaymentStatus.CANCELLED: CustomerPaymentStatus.CANCELLED,
}


def customer_status(
    status: PaymentStatus, resolution: PaymentResolution | None
) -> CustomerPaymentStatus:
    """`FAILED` closed by a review without credit is `NOT_CREDITED`; the rest map one to one."""
    status = PaymentStatus(status)
    if status is PaymentStatus.FAILED and resolution is not None:
        if PaymentResolution(resolution) is PaymentResolution.CLOSED:
            return CustomerPaymentStatus.NOT_CREDITED
    return _STATUS_MAP[status]


@dataclass(frozen=True)
class TopupOptions:
    """What the top-up page needs; amounts are two-decimal `Decimal`s."""

    unavailable_reason: UnavailableReason | None
    min_amount: Decimal
    max_amount: Decimal | None
    preset_amounts: tuple[Decimal, ...]
    recovery_minimum: Decimal | None
    effective_minimum: Decimal

    @property
    def available(self) -> bool:
        return self.unavailable_reason is None


@dataclass(frozen=True)
class CustomerPaymentRow:
    payment: Payment
    # 这笔支付的 `TOPUP` 账本行金额；没有为 None。
    credited_amount: Decimal | None
    # 这笔支付的收据（`public_id` 与收据号）；没有为 None。
    receipt_id: str | None
    receipt_number: str | None


@dataclass(frozen=True)
class CustomerReceiptRow:
    # 收据的 `public_id`。
    receipt_id: str
    receipt_number: str
    # 支付的 `public_id`。
    payment_id: str
    amount: Decimal
    currency: str
    # 收据只开给 `PAID` 的支付，`paid_at` 不为空。
    paid_at: dt.datetime
    issued_at: dt.datetime


def topup_options(session: Session, tenant_id: int, *, settings: Settings) -> TopupOptions:
    """The caller's top-up options. `PaymentSettingsMissing` when the settings row is absent."""
    rules = get_payment_settings(session)
    tenant = session.get(Tenant, tenant_id)
    if tenant is None:
        # 外键保证不会发生（`require_customer` 的用户行指向它）。
        raise CustomerNotFound
    balance = session.execute(
        select(Wallet.balance).where(Wallet.tenant_id == tenant_id)
    ).scalar_one_or_none()
    # 与建单相同：没有钱包行就没有欠款，只按最低额判。
    recovery = None if balance is None else recovery_minimum(balance, tenant.billing_mode, rules)
    minimum = (
        rules.min_amount
        if balance is None
        else effective_minimum(balance, tenant.billing_mode, rules)
    )
    return TopupOptions(
        unavailable_reason=_unavailable_reason(tenant, minimum, rules, settings),
        min_amount=rules.min_amount,
        max_amount=rules.max_amount,
        preset_amounts=rules.preset_amounts,
        recovery_minimum=recovery,
        effective_minimum=minimum,
    )


def _unavailable_reason(
    tenant: Tenant, minimum: Decimal, rules: PaymentSettings, settings: Settings
) -> UnavailableReason | None:
    """The first reason that holds, in the order of A's top-up checks (开关放最前)."""
    if not settings.payments_enabled:
        return UnavailableReason.PAYMENTS_DISABLED
    if tenant.account_status in READ_ONLY_STATUSES:
        return UnavailableReason.ACCOUNT_READ_ONLY
    # 与 A 的 `_check_tenant` 同一判断：开关关闭时只有允许名单里的租户能建单。
    if not settings.payment_topups_open and (
        tenant.public_id.lower() not in settings.payment_topup_tenant_ids
    ):
        return UnavailableReason.TOPUPS_NOT_OPEN
    if tenant.billing_mode is not BillingMode.PREPAID:
        return UnavailableReason.TOPUP_NOT_ALLOWED
    if recovery_exceeds_cap(minimum, rules):
        return UnavailableReason.RECOVERY_EXCEEDS_MAXIMUM
    return None


def _with_credit() -> Select[tuple[Payment, Decimal, str, str]]:
    # 左连接：没有入账的支付没有账本行（金额为 None）。账本引用 `(PAYMENT, public_id)` 唯一，
    # 至多一行。收据同样左连接：`payment_id` 唯一，至多一张，没有为 None（设计闸门 #589 v2）。
    return (
        select(
            Payment,
            WalletTransaction.amount,
            PaymentReceipt.public_id,
            PaymentReceipt.receipt_number,
        )
        .outerjoin(
            WalletTransaction,
            and_(
                WalletTransaction.reference_type == ReferenceType.PAYMENT,
                WalletTransaction.reference_id == Payment.public_id,
                WalletTransaction.transaction_type == TransactionType.TOPUP,
            ),
        )
        .outerjoin(PaymentReceipt, PaymentReceipt.payment_id == Payment.id)
    )


def list_payments(
    session: Session, tenant_id: int, *, offset: int, limit: int
) -> tuple[list[CustomerPaymentRow], int]:
    """One page of the tenant's payments, newest `created_at` first then descending id."""
    listing = (
        _with_credit()
        .where(Payment.tenant_id == tenant_id)
        .order_by(Payment.created_at.desc(), Payment.id.desc())
        .offset(offset)
        .limit(limit)
    )
    counting = select(func.count()).select_from(Payment).where(Payment.tenant_id == tenant_id)
    rows = [
        CustomerPaymentRow(payment, credited, receipt_id, receipt_number)
        for payment, credited, receipt_id, receipt_number in session.execute(listing).all()
    ]
    total = session.execute(counting).scalar_one()
    return rows, total


def get_payment(session: Session, tenant_id: int, payment_id: str) -> CustomerPaymentRow | None:
    """The tenant's payment with this `public_id`; `None` when absent or another tenant's.

    不做格式校验：不像 uuid 的值只是查不到（设计 §2，几种情况不区分）。
    """
    found = session.execute(
        _with_credit().where(Payment.tenant_id == tenant_id, Payment.public_id == payment_id)
    ).one_or_none()
    if found is None:
        return None
    payment, credited, receipt_id, receipt_number = found
    return CustomerPaymentRow(payment, credited, receipt_id, receipt_number)


def list_receipts(
    session: Session, tenant_id: int, *, offset: int, limit: int
) -> tuple[list[CustomerReceiptRow], int]:
    """One page of the tenant's receipts, newest `issued_at` first then descending id.

    排序与 `ix_payment_receipts_tenant_issued (tenant_id, issued_at, id)` 一致。只读列，不读快照。
    """
    listing = (
        select(
            PaymentReceipt.public_id,
            PaymentReceipt.receipt_number,
            Payment.public_id,
            PaymentReceipt.amount,
            PaymentReceipt.currency,
            Payment.paid_at,
            PaymentReceipt.issued_at,
        )
        .join(Payment, Payment.id == PaymentReceipt.payment_id)
        .where(PaymentReceipt.tenant_id == tenant_id)
        .order_by(PaymentReceipt.issued_at.desc(), PaymentReceipt.id.desc())
        .offset(offset)
        .limit(limit)
    )
    counting = (
        select(func.count())
        .select_from(PaymentReceipt)
        .where(PaymentReceipt.tenant_id == tenant_id)
    )
    rows = [CustomerReceiptRow(*row) for row in session.execute(listing).all()]
    total = session.execute(counting).scalar_one()
    return rows, total


__all__ = [
    "CustomerPaymentRow",
    "CustomerPaymentStatus",
    "CustomerReceiptRow",
    "TopupOptions",
    "UnavailableReason",
    "customer_status",
    "get_payment",
    "list_payments",
    "list_receipts",
    "topup_options",
]

"""Customer payment views: top-up options, the payment list / detail and receipts (AIH-TASK-190).

设计闸门 #587 v3 §2「客户接口」、§6；收据见设计闸门 #589 v2 §2「客户接口」（AIH-TASK-202）；
契约见 [docs/api.md](../../docs/api.md)「支付（客户）」。

⚠️ **视图是字段白名单，不直接序列化 ORM 对象**：充值选项恰好 8 个键，支付恰好 10 个键（#587 的
8 个之后追加 `receipt_id`、`receipt_number`），收据恰好 7 个键，都按设计的顺序。收据不带快照、
`tax_policy_version_id` 与内部 id。手续费、状态原因、原始状态、`resolution` 与其他结案字段、
网关编号（`cs_…` / `pi_…`）、`confirmed_*`、内部 id 与 `tenant_id`
一律不出这里（INV-7、INV-8）。

⚠️ 金额是**两位小数**的定点字符串（例如 `"50.00"`），不经 float（INV-10）：与 8 位小数的通用写法
不同，是设计 §2 对这三个接口的规定（金额本身就是整分）。时刻是不带时区的 UTC。
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Final

from pydantic import BaseModel

from app.services.customer_payments import (
    CustomerPaymentRow,
    CustomerReceiptRow,
    TopupOptions,
    customer_status,
)
from app.services.payments import PAYMENT_CURRENCY

_CENT: Final = Decimal("0.01")


def cents_text(value: Decimal) -> str:
    """`Decimal` → a two-decimal fixed-point string, e.g. `"10.00"` (never `"1E+1"`)."""
    return format(value.quantize(_CENT), "f")


def _cents(value: Decimal | None) -> str | None:
    return None if value is None else cents_text(value)


class CustomerTopupOptions(BaseModel):
    """`GET /api/v1/customer/topup-options`."""

    currency: str
    available: bool
    # `available` 为真时为 null；否则是五个原因之一。
    unavailable_reason: str | None
    min_amount: str
    max_amount: str | None
    preset_amounts: list[str]
    # 只对余额 ≤ 0 的预付客户有值。
    recovery_minimum: str | None
    effective_minimum: str


class CustomerPaymentView(BaseModel):
    """One of the caller's payments. `id` is the payment's `public_id`."""

    id: str
    # `expected_amount`：建单时要付的金额。
    amount: str
    currency: str
    # 客户侧状态（七种之一），不是原始状态。
    status: str
    # 这笔支付的 `TOPUP` 账本行金额；没有入账为 null。
    credited_amount: str | None
    created_at: dt.datetime
    paid_at: dt.datetime | None
    expires_at: dt.datetime
    # 这笔支付的收据（AIH-TASK-202）：收据的 `public_id` 与收据号；没有为 null。
    receipt_id: str | None
    receipt_number: str | None


class CustomerReceiptView(BaseModel):
    """One of the caller's receipts (`GET /api/v1/customer/receipts`). `id` is its `public_id`."""

    id: str
    receipt_number: str
    # 支付的 `public_id`。
    payment_id: str
    # 收据金额（这笔支付的 `TOPUP` 账本行），两位小数。
    amount: str
    currency: str
    paid_at: dt.datetime
    issued_at: dt.datetime


def customer_topup_options(options: TopupOptions) -> CustomerTopupOptions:
    reason = options.unavailable_reason
    return CustomerTopupOptions(
        currency=PAYMENT_CURRENCY,
        available=options.available,
        unavailable_reason=None if reason is None else reason.value,
        min_amount=cents_text(options.min_amount),
        max_amount=_cents(options.max_amount),
        preset_amounts=[cents_text(amount) for amount in options.preset_amounts],
        recovery_minimum=_cents(options.recovery_minimum),
        effective_minimum=cents_text(options.effective_minimum),
    )


def customer_payment_view(row: CustomerPaymentRow) -> CustomerPaymentView:
    payment = row.payment
    return CustomerPaymentView(
        id=payment.public_id,
        amount=cents_text(payment.expected_amount),
        currency=payment.expected_currency,
        status=customer_status(payment.status, payment.resolution).value,
        credited_amount=_cents(row.credited_amount),
        created_at=payment.created_at,
        paid_at=payment.paid_at,
        expires_at=payment.expires_at,
        receipt_id=row.receipt_id,
        receipt_number=row.receipt_number,
    )


def customer_receipt_view(row: CustomerReceiptRow) -> CustomerReceiptView:
    return CustomerReceiptView(
        id=row.receipt_id,
        receipt_number=row.receipt_number,
        payment_id=row.payment_id,
        amount=cents_text(row.amount),
        currency=row.currency,
        paid_at=row.paid_at,
        issued_at=row.issued_at,
    )


__all__ = [
    "CustomerPaymentView",
    "CustomerReceiptView",
    "CustomerTopupOptions",
    "cents_text",
    "customer_payment_view",
    "customer_receipt_view",
    "customer_topup_options",
]

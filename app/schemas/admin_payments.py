"""Admin payment views and the review resolution request (AIH-TASK-183).

设计闸门 #585 v3 §2「管理端接口」；契约见 [docs/api.md](../../docs/api.md)「支付（管理端）」。

⚠️ **视图是字段白名单，不直接序列化 ORM 对象**：列表项恰好 14 个键，详情在其上按设计的顺序
加 10 个；事件每项恰好 7 个键。内部自增 id、`tenant_id`、`resolved_by_user_id`、
`tax_policy_version_id`、`wallet_credit_amount`、事件的 `metadata_json`、指纹与租约一律不出这里，
任何密钥都不在这些表里。对外的引用是 `public_id`（支付与客户）与 Stripe 的对象 id。

⚠️ 金额是恰好 8 位小数的字符串（`money_text`），不经 float（INV-10）；时刻是不带时区的 UTC。
手续费、状态原因与结案信息只在管理端（INV-7）。

结案请求体 `extra="forbid"`：`action` 只收 `CREDIT` / `CLOSE`，`reason` 沿用人工调账的类型
（去掉首尾空白后 1–255 字）。
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict

from app.models.payments import (
    Payment,
    PaymentEventProcessingStatus,
    PaymentGateway,
    PaymentGatewayEvent,
    PaymentResolution,
    PaymentStatus,
)
from app.schemas.customers import money_text
from app.schemas.wallet_adjustments import _Reason
from app.services.payment_admin import PaymentDetailRow, PaymentListRow
from app.services.payment_review import ReviewAction


class ResolvePaymentRequest(BaseModel):
    """`POST …/payments/{payment_id}/resolve`."""

    model_config = ConfigDict(extra="forbid")

    action: ReviewAction
    reason: _Reason


class AdminPaymentSummary(BaseModel):
    """One payment in the admin list. `id` / `customer_id` are `public_id`s."""

    id: str
    customer_id: str
    # 客户当前的公司名。
    customer_name: str
    # `expected_amount`：客户要付的含税毛额。
    amount: str
    currency: str
    # `STRIPE`。
    gateway: str
    # Checkout Session id（`cs_…`）；建单时 Stripe 调用失败留下的单为 null。
    gateway_reference: str | None
    # Stripe 的实际手续费（MYR）；拿不到时为 null。
    gateway_fee: str | None
    status: str
    status_reason: str | None
    # `CREDITED` / `CLOSED`；未人工结案为 null。
    resolution: str | None
    created_at: dt.datetime
    paid_at: dt.datetime | None
    review_required_at: dt.datetime | None


class AdminPaymentEventView(BaseModel):
    """One gateway event linked to the payment; never its `metadata_json`."""

    gateway_event_id: str
    event_type: str
    processing_status: str
    last_error_code: str | None
    received_at: dt.datetime
    processed_at: dt.datetime | None
    fingerprint_conflict_at: dt.datetime | None


class AdminPaymentDetail(AdminPaymentSummary):
    """One payment with its confirmation, credit, resolution and events."""

    confirmed_amount: str | None
    confirmed_currency: str | None
    # 这笔支付的 `TOPUP` 账本行金额（实际入账额）；没有为 null。
    credited_amount: str | None
    gateway_payment_intent_id: str | None
    expires_at: dt.datetime
    last_reconciled_at: dt.datetime | None
    resolved_at: dt.datetime | None
    # 结案管理员的邮箱。
    resolved_by: str | None
    resolution_reason: str | None
    # 按 `received_at` 升序、同一时刻按内部 id 升序，最多 100 条。
    events: list[AdminPaymentEventView]


def _money(value: Decimal | None) -> str | None:
    return money_text(value) if value is not None else None


def _summary_fields(payment: Payment, customer_id: str, customer_name: str) -> dict[str, Any]:
    resolution = payment.resolution
    return {
        "id": payment.public_id,
        "customer_id": customer_id,
        "customer_name": customer_name,
        "amount": money_text(payment.expected_amount),
        "currency": payment.expected_currency,
        "gateway": PaymentGateway(payment.gateway).value,
        "gateway_reference": payment.gateway_payment_id,
        "gateway_fee": _money(payment.gateway_fee),
        "status": PaymentStatus(payment.status).value,
        "status_reason": payment.status_reason,
        "resolution": PaymentResolution(resolution).value if resolution is not None else None,
        "created_at": payment.created_at,
        "paid_at": payment.paid_at,
        "review_required_at": payment.review_required_at,
    }


def admin_payment_summary(row: PaymentListRow) -> AdminPaymentSummary:
    return AdminPaymentSummary(**_summary_fields(row.payment, row.customer_id, row.customer_name))


def admin_payment_event(event: PaymentGatewayEvent) -> AdminPaymentEventView:
    return AdminPaymentEventView(
        gateway_event_id=event.gateway_event_id,
        event_type=event.event_type,
        processing_status=PaymentEventProcessingStatus(event.processing_status).value,
        last_error_code=event.last_error_code,
        received_at=event.received_at,
        processed_at=event.processed_at,
        fingerprint_conflict_at=event.fingerprint_conflict_at,
    )


def admin_payment_detail(row: PaymentDetailRow) -> AdminPaymentDetail:
    payment = row.payment
    return AdminPaymentDetail(
        **_summary_fields(payment, row.customer_id, row.customer_name),
        confirmed_amount=_money(payment.confirmed_amount),
        confirmed_currency=payment.confirmed_currency,
        credited_amount=_money(row.credited_amount),
        gateway_payment_intent_id=payment.gateway_payment_intent_id,
        expires_at=payment.expires_at,
        last_reconciled_at=payment.last_reconciled_at,
        resolved_at=payment.resolved_at,
        resolved_by=row.resolved_by,
        resolution_reason=payment.resolution_reason,
        events=[admin_payment_event(event) for event in row.events],
    )


__all__ = [
    "AdminPaymentDetail",
    "AdminPaymentEventView",
    "AdminPaymentSummary",
    "ResolvePaymentRequest",
    "admin_payment_detail",
    "admin_payment_event",
    "admin_payment_summary",
]

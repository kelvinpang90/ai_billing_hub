"""Admin payment queries: the list and one payment's detail (AIH-TASK-183).

设计闸门 #585 v3 §2「管理端接口」；契约见 [docs/api.md](../../docs/api.md)「支付（管理端）」。

只读：调用方给一个普通会话，这里不加锁、不提交、不写审计，也不写日志。结案接口在
`resolve_review` 提交之后用同一个 `get_payment_detail` 重新读出详情。

⚠️ 创建日期区间按**马来西亚日历日**（固定 UTC+8，复用 `KUALA_LUMPUR_OFFSET`，不引入 tzdata），
含两端：`[created_from 00:00 MYT, (created_to + 1 天) 00:00 MYT)`，换算成不带时区的 UTC 再比较。
严格解析：`^[0-9]{4}-[0-9]{2}-[0-9]{2}$`、合法日期、年份 2000–2099，先判年份再做日期运算；
不合规与起晚于止都是 422 `VALIDATION_ERROR`，文案只列字段名，不回显值。

⚠️ 这一层只返回 ORM 行与少量连接列；字段白名单在 app/schemas/admin_payments.py。
`metadata_json` 不出这里之外的任何响应。
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Final

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.core.fx_source import KUALA_LUMPUR_OFFSET
from app.models.auth import User
from app.models.payments import Payment, PaymentGatewayEvent, PaymentStatus
from app.models.tenancy import Tenant
from app.models.wallet import ReferenceType, TransactionType, WalletTransaction

# 设计 §2：日期的形状与年份范围（含两端）。⚠️ 用 [0-9] 而不是 \d：\d 还认全角与其他文字的数字。
CREATED_DATE_PATTERN: Final = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
MIN_CREATED_YEAR: Final = 2000
MAX_CREATED_YEAR: Final = 2099
# 详情里最多列出的回调事件数。
MAX_DETAIL_EVENTS: Final = 100


class InvalidPaymentQuery(AppError):
    """A creation date the handler judges after `require_admin`.

    与 FastAPI 的 422 同一个码、同一种文案：只列字段名，不回显值（app/core/errors.py）。
    """

    def __init__(self, *fields: str) -> None:
        names = ", ".join(f"query.{name}" for name in sorted(fields))
        super().__init__(
            f"Invalid request fields: {names}", code="VALIDATION_ERROR", http_status=422
        )


def _created_date(field: str, value: str | None) -> dt.date | None:
    if value is None:
        return None
    if CREATED_DATE_PATTERN.fullmatch(value) is None:
        raise InvalidPaymentQuery(field)
    year, month, day = (int(part) for part in value.split("-"))
    # 先判年份，再构造日期：极值得到 422，而不是换算溢出的 500。
    if not MIN_CREATED_YEAR <= year <= MAX_CREATED_YEAR:
        raise InvalidPaymentQuery(field)
    try:
        return dt.date(year, month, day)
    except ValueError:
        raise InvalidPaymentQuery(field) from None


def _myt_midnight_utc(day: dt.date) -> dt.datetime:
    """00:00 Malaysian time of `day`, as naive UTC."""
    return dt.datetime.combine(day, dt.time()) - KUALA_LUMPUR_OFFSET


def parse_created_range(
    created_from: str | None, created_to: str | None
) -> tuple[dt.datetime | None, dt.datetime | None]:
    """`[start, end)` as naive UTC for the inclusive Malaysian calendar days given."""
    first = _created_date("created_from", created_from)
    last = _created_date("created_to", created_to)
    if first is not None and last is not None and first > last:
        raise InvalidPaymentQuery("created_from", "created_to")
    start = _myt_midnight_utc(first) if first is not None else None
    end = _myt_midnight_utc(last + dt.timedelta(days=1)) if last is not None else None
    return start, end


@dataclass(frozen=True)
class PaymentListRow:
    payment: Payment
    customer_id: str
    customer_name: str


@dataclass(frozen=True)
class PaymentDetailRow:
    payment: Payment
    customer_id: str
    customer_name: str
    # 这笔支付的 `TOPUP` 账本行金额；没有为 None。
    credited_amount: Decimal | None
    # 结案管理员的 `users.email`；未结案为 None。
    resolved_by: str | None
    # 按 `received_at` 升序、同一时刻按 `id` 升序，最多 `MAX_DETAIL_EVENTS` 条。
    events: list[PaymentGatewayEvent]


def list_payments(
    session: Session,
    *,
    offset: int,
    limit: int,
    status: PaymentStatus | None = None,
    customer_id: str | None = None,
    created_from: dt.datetime | None = None,
    created_to: dt.datetime | None = None,
) -> tuple[list[PaymentListRow], int]:
    """One page, newest `created_at` first then descending id, and the total.

    `customer_id` 是租户的 `public_id`，没有这个客户时得到空页；`created_from` /
    `created_to` 是 `parse_created_range` 的半开区间。
    """
    conditions = []
    if status is not None:
        conditions.append(Payment.status == status)
    if customer_id is not None:
        conditions.append(Tenant.public_id == customer_id)
    if created_from is not None:
        conditions.append(Payment.created_at >= created_from)
    if created_to is not None:
        conditions.append(Payment.created_at < created_to)
    listing = (
        select(Payment, Tenant.public_id, Tenant.company_name)
        .join(Tenant, Tenant.id == Payment.tenant_id)
        .where(*conditions)
        .order_by(Payment.created_at.desc(), Payment.id.desc())
        .offset(offset)
        .limit(limit)
    )
    counting = (
        select(func.count())
        .select_from(Payment)
        .join(Tenant, Tenant.id == Payment.tenant_id)
        .where(*conditions)
    )
    rows = [
        PaymentListRow(payment, public_id, company_name)
        for payment, public_id, company_name in session.execute(listing).all()
    ]
    total = session.execute(counting).scalar_one()
    return rows, total


def get_payment_detail(session: Session, payment_id: str) -> PaymentDetailRow | None:
    """One payment by `public_id` with its credit, resolver and events; `None` if absent."""
    # 左连接：未结案的支付没有结案管理员。
    found = session.execute(
        select(Payment, Tenant.public_id, Tenant.company_name, User.email)
        .join(Tenant, Tenant.id == Payment.tenant_id)
        .outerjoin(User, User.id == Payment.resolved_by_user_id)
        .where(Payment.public_id == payment_id)
    ).one_or_none()
    if found is None:
        return None
    payment, customer_id, customer_name, resolved_by = found
    # 账本引用 `(PAYMENT, public_id)` 唯一，至多一行。
    credited_amount = session.execute(
        select(WalletTransaction.amount).where(
            WalletTransaction.reference_type == ReferenceType.PAYMENT,
            WalletTransaction.reference_id == payment.public_id,
            WalletTransaction.transaction_type == TransactionType.TOPUP,
        )
    ).scalar_one_or_none()
    events = list(
        session.execute(
            select(PaymentGatewayEvent)
            .where(PaymentGatewayEvent.payment_id == payment.id)
            .order_by(PaymentGatewayEvent.received_at, PaymentGatewayEvent.id)
            .limit(MAX_DETAIL_EVENTS)
        ).scalars()
    )
    return PaymentDetailRow(
        payment=payment,
        customer_id=customer_id,
        customer_name=customer_name,
        credited_amount=credited_amount,
        resolved_by=resolved_by,
        events=events,
    )


__all__ = [
    "CREATED_DATE_PATTERN",
    "MAX_CREATED_YEAR",
    "MAX_DETAIL_EVENTS",
    "MIN_CREATED_YEAR",
    "InvalidPaymentQuery",
    "PaymentDetailRow",
    "PaymentListRow",
    "get_payment_detail",
    "list_payments",
    "parse_created_range",
]

"""Issuing payment receipts: number, snapshot, checks and the email outbox row (gate #589 v2).

设计 §2「编号」「快照」「开具」「付款邮件」（outbox 行部分），AIH-TASK-197。本任务只有服务：
入账路径（A 的 `confirm_payment`、B 的 `resolve_review`）的接入属于 AIH-TASK-200，补发扫描属于
201，邮件渲染属于 198。

⚠️ **只 flush，不 commit**：开具与入账同一事务（INV-13），提交归调用方。

⚠️ **检查在任何写入之前、只读**：检查不过时记一条 ERROR、返回 `None`，调用方事务里只有它自己的
写入（入账照常提交，收据由补发扫描再试）。第 3 条检查对当年系列行 `SELECT … FOR UPDATE`：这是
锁，不是写；锁顺序是「支付 → 钱包 → 租户 → 当年系列行」（设计 §2）。

⚠️ 日志只带原因码与支付的 `public_id`，不带公司名、邮箱、金额（设计 §6）。

时间一律不带时区的 UTC；编号年份按马来西亚时间（固定 UTC+8）换算，不存换算后的值。
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.fx_source import KUALA_LUMPUR_OFFSET
from app.models.auth import DomainOutbox, OutboxStatus
from app.models.payments import Payment, PaymentStatus
from app.models.receipts import PaymentReceipt, ReceiptNumberSeries
from app.models.tax_policy import TaxPolicyVersion
from app.models.tenancy import Tenant
from app.models.wallet import ReferenceType, TransactionType, WalletTransaction
from app.repositories.tax_policy import DocumentNotice, parse_treatment

logger = logging.getLogger(__name__)

# spec §45 原文。
RECEIPT_ISSUER_NAME: Final = "Acuven Technology Sdn Bhd"
# outbox 行的事件类型（AIH-TASK-198 由 app/tasks/outbox.py 导入、登记渲染）。⚠️ 改它等于让已经
# 躺在 outbox 里的行认不出自己是什么。
EVENT_RECEIPT_EMAIL: Final = "PAYMENT_RECEIPT_EMAIL_REQUESTED"
AGGREGATE_PAYMENT_RECEIPT: Final = "payment_receipt"

# 开具检查的原因码（设计 §2「开具」第 2 步）。
RECEIPT_LEDGER_MISSING: Final = "RECEIPT_LEDGER_MISSING"
RECEIPT_TAX_POLICY_UNREADABLE: Final = "RECEIPT_TAX_POLICY_UNREADABLE"
RECEIPT_SERIES_UNAVAILABLE: Final = "RECEIPT_SERIES_UNAVAILABLE"

SNAPSHOT_SCHEMA_VERSION: Final = 1
RECEIPT_CURRENCY: Final = "MYR"
# `ck_receipt_number_series_sequence` 的上界。
MAX_SEQUENCE: Final = 999999
_CENT: Final = Decimal("0.01")


@dataclass(frozen=True)
class IssuedReceipt:
    """The receipt of a payment; `created` is true only when this call issued it."""

    public_id: str
    receipt_number: str
    created: bool


def receipt_series_year(issued_at: dt.datetime) -> int:
    """The Malaysian calendar year of a naive UTC moment."""
    return (issued_at + KUALA_LUMPUR_OFFSET).year


def format_receipt_number(series_year: int, sequence: int) -> str:
    return f"ACV-AIR-{series_year:04d}-{sequence:06d}"


def _timestamp(moment: dt.datetime) -> str:
    return moment.isoformat(timespec="seconds")


def _refuse(code: str, payment: Payment) -> None:
    # 固定文本：不带公司名、邮箱、金额（设计 §6）。
    logger.error(
        "Receipt not issued; a pre-issue check failed",
        extra={"reason": code, "payment_id": payment.public_id},
    )


def _existing(session: Session, payment: Payment) -> PaymentReceipt | None:
    statement = select(PaymentReceipt).where(PaymentReceipt.payment_id == payment.id)
    return session.execute(statement).scalar_one_or_none()


def _topup_amount(session: Session, payment: Payment) -> Decimal | None:
    """Check 1: the payment's `TOPUP` ledger amount, when it is > 0 with two decimals."""
    statement = select(WalletTransaction.amount).where(
        WalletTransaction.reference_type == ReferenceType.PAYMENT,
        WalletTransaction.reference_id == payment.public_id,
        WalletTransaction.transaction_type == TransactionType.TOPUP,
    )
    amount = session.execute(statement).scalar_one_or_none()
    if amount is None or amount <= 0 or amount != amount.quantize(_CENT):
        return None
    return amount.quantize(_CENT)


def _tax_notice(session: Session, payment: Payment) -> DocumentNotice | None:
    """Check 2: the `document_notice` of the payment's own tax policy version."""
    version = session.get(TaxPolicyVersion, payment.tax_policy_version_id)
    if version is None:
        return None
    try:
        return parse_treatment(version.treatment_json).document_notice
    except ValueError:
        return None


def _lock_series(session: Session, series_year: int) -> ReceiptNumberSeries | None:
    """Check 3: the year's series row, `FOR UPDATE`, when it still has a number left."""
    statement = (
        select(ReceiptNumberSeries)
        .where(ReceiptNumberSeries.series_year == series_year)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    series = session.execute(statement).scalar_one_or_none()
    if series is None or series.last_sequence >= MAX_SEQUENCE:
        return None
    return series


def _snapshot(
    *,
    receipt_number: str,
    issued_at: dt.datetime,
    company_name: str,
    payment: Payment,
    amount_text: str,
    paid_at_text: str,
    payment_method: str | None,
    notice: DocumentNotice,
) -> dict[str, Any]:
    """The frozen receipt data (设计 §2「快照」): exactly these keys."""
    return {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "receipt_number": receipt_number,
        "issued_at": _timestamp(issued_at),
        "issuer": {"name": RECEIPT_ISSUER_NAME},
        "customer": {"company_name": company_name},
        "payment": {
            "payment_id": payment.public_id,
            "paid_at": paid_at_text,
            "amount": amount_text,
            "currency": RECEIPT_CURRENCY,
            "status": PaymentStatus.PAID.value,
            "gateway": payment.gateway.value,
            "gateway_reference": payment.gateway_payment_intent_id or payment.gateway_payment_id,
            "payment_method": payment_method,
        },
        "tax_notice": {"zh": notice.zh, "en": notice.en},
    }


def issue_receipt(
    session: Session,
    *,
    payment: Payment,
    payment_method: str | None,
    now: dt.datetime,
    send_email: bool,
) -> IssuedReceipt | None:
    """Issue the one receipt of a `PAID` payment the caller has locked. Flushes, never commits.

    已有收据 → 返回它（`created=False`），不写任何行；三条检查任一不过 → 记 ERROR、返回 `None`，
    不写任何行；否则分配编号、插入收据与（`send_email` 为真时）付款邮件 outbox 行。
    状态不是 `PAID` 抛 `ValueError`（程序错误）。数据库错误照原样抛出。
    """
    if payment.status is not PaymentStatus.PAID:
        raise ValueError("only a PAID payment gets a receipt")
    # 第 0 步：MySQL DATETIME 对微秒四舍五入，不截掉会让存下的时刻比快照晚一秒、年末还会跨年。
    issued_at = now.replace(microsecond=0)

    existing = _existing(session, payment)
    if existing is not None:
        return IssuedReceipt(existing.public_id, existing.receipt_number, created=False)

    amount = _topup_amount(session, payment)
    if amount is None:
        _refuse(RECEIPT_LEDGER_MISSING, payment)
        return None
    notice = _tax_notice(session, payment)
    if notice is None:
        _refuse(RECEIPT_TAX_POLICY_UNREADABLE, payment)
        return None
    series_year = receipt_series_year(issued_at)
    series = _lock_series(session, series_year)
    if series is None:
        _refuse(RECEIPT_SERIES_UNAVAILABLE, payment)
        return None

    # 支付行是 PAID：`ck_payments_paid_fields` 保证 paid_at 不为空。
    assert payment.paid_at is not None
    tenant = session.get(Tenant, payment.tenant_id)
    assert tenant is not None
    sequence = series.last_sequence + 1
    receipt_number = format_receipt_number(series_year, sequence)
    amount_text = f"{amount:f}"
    paid_at_text = _timestamp(payment.paid_at)

    series.last_sequence = sequence
    receipt = PaymentReceipt(
        public_id=str(uuid.uuid4()),
        payment_id=payment.id,
        tenant_id=payment.tenant_id,
        receipt_number=receipt_number,
        series_year=series_year,
        sequence=sequence,
        amount=amount,
        currency=RECEIPT_CURRENCY,
        tax_policy_version_id=payment.tax_policy_version_id,
        snapshot_json=_snapshot(
            receipt_number=receipt_number,
            issued_at=issued_at,
            company_name=tenant.company_name,
            payment=payment,
            amount_text=amount_text,
            paid_at_text=paid_at_text,
            payment_method=payment_method,
            notice=notice,
        ),
        issued_at=issued_at,
    )
    session.add(receipt)
    if send_email:
        session.add(
            DomainOutbox(
                event_type=EVENT_RECEIPT_EMAIL,
                aggregate_type=AGGREGATE_PAYMENT_RECEIPT,
                aggregate_id=receipt.public_id,
                # 恰好 6 个键（设计 §2「付款邮件」）；投递成功或死信时由 `_settle` 清空。
                payload_json=json.dumps(
                    {
                        "to": tenant.email,
                        "company_name": tenant.company_name,
                        "receipt_number": receipt_number,
                        "amount": amount_text,
                        "paid_at": paid_at_text,
                        "payment_id": payment.public_id,
                    },
                    sort_keys=True,
                ),
                status=OutboxStatus.PENDING,
                attempt_count=0,
                # 不 delay()：只靠 recover 领取。
                next_retry_at=now,
                created_at=now,
            )
        )
    session.flush()
    return IssuedReceipt(receipt.public_id, receipt_number, created=True)


__all__ = [
    "AGGREGATE_PAYMENT_RECEIPT",
    "EVENT_RECEIPT_EMAIL",
    "MAX_SEQUENCE",
    "RECEIPT_CURRENCY",
    "RECEIPT_ISSUER_NAME",
    "RECEIPT_LEDGER_MISSING",
    "RECEIPT_SERIES_UNAVAILABLE",
    "RECEIPT_TAX_POLICY_UNREADABLE",
    "SNAPSHOT_SCHEMA_VERSION",
    "IssuedReceipt",
    "format_receipt_number",
    "issue_receipt",
    "receipt_series_year",
]

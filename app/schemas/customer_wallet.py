"""Response shapes for the customer wallet (design gate #393 v3 §2, §6).

⚠️ **这是客户侧的字段白名单**（INV-7）：钱包恰好 6 个键，流水每项恰好 7 个键。刻意不继承、不复用
app/schemas/wallet_transactions.py 的管理端模型：管理端以后加字段，客户端不会跟着出现。库里的
`description`（管理员写的调账原因）、`reference_type`、调账的 `reference_id`（幂等键）、
操作者邮箱、`balance_before`、用量事件的 `public_id`、钱包 `version` 与任何内部 id 一律不出这里。

⚠️ 金额是恰好 8 位小数的字符串（`money_text`），原样来自 `DECIMAL(20,8)` 列，不经 float、不舍入
（INV-10）。显示精度是前端的事（设计 §2「金额（前端显示）」）。
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence

from pydantic import BaseModel

from app.models.tenancy import IntegrationStatus, compose_effective_status
from app.models.wallet import ReferenceType, TransactionType
from app.repositories.wallet import LedgerRow, WalletSummary
from app.schemas.customers import money_text


class CustomerWalletView(BaseModel):
    """The caller's own wallet: balance, billing state, and whether AI service is on."""

    currency: str
    balance: str
    billing_mode: str
    billing_status: str
    ai_service: str
    # `ai_service` 为 `BLOCK_AI` 时才有。
    ai_service_reason: str | None


class CustomerWalletTransactionView(BaseModel):
    """One ledger row as the customer sees it. `id` is the row's `public_id`."""

    id: str
    wallet_sequence: int
    transaction_type: str
    amount: str
    balance_after: str
    # 只有 `USAGE_EVENT` 来源的行有：接入方上报（或 AI 模块代为生成）的 `event_id`。
    event_id: str | None
    created_at: dt.datetime


class CustomerWalletTransactionPage(BaseModel):
    """Newest first. `next_before_sequence` is `None` when there is nothing older; no `total`."""

    items: list[CustomerWalletTransactionView]
    page_size: int
    next_before_sequence: int | None


def customer_wallet_view(summary: WalletSummary) -> CustomerWalletView:
    # 租户级：项目维度以 `ENABLED` 代入，与 `ai_service_enabled` 同一公式（设计 §2）。
    effective, reason = compose_effective_status(
        summary.account_status,
        summary.billing_status,
        summary.billing_mode,
        IntegrationStatus.ENABLED,
    )
    return CustomerWalletView(
        currency=summary.currency,
        balance=money_text(summary.balance),
        billing_mode=summary.billing_mode.value,
        billing_status=summary.billing_status.value,
        ai_service=effective.value,
        ai_service_reason=None if reason is None else reason.value,
    )


def customer_wallet_transaction_view(row: LedgerRow) -> CustomerWalletTransactionView:
    # 调账、系统更正、rebill 的 `reference_id` 是内部的幂等键，不给客户（设计 §2「接口二」）。
    usage = ReferenceType(row.reference_type) is ReferenceType.USAGE_EVENT
    return CustomerWalletTransactionView(
        id=row.public_id,
        wallet_sequence=row.wallet_sequence,
        transaction_type=TransactionType(row.transaction_type).value,
        amount=money_text(row.amount),
        balance_after=money_text(row.balance_after),
        event_id=row.reference_id if usage else None,
        created_at=row.created_at,
    )


def customer_wallet_transaction_page(
    rows: Sequence[LedgerRow], *, page_size: int
) -> CustomerWalletTransactionPage:
    """`rows` holds up to `page_size + 1` rows, newest first; the extra one only says "more"."""
    shown = rows[:page_size]
    more = len(rows) > page_size
    return CustomerWalletTransactionPage(
        items=[customer_wallet_transaction_view(row) for row in shown],
        page_size=page_size,
        next_before_sequence=shown[-1].wallet_sequence if more else None,
    )

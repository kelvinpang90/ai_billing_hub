"""Response shapes for the admin wallet ledger view (design gate #351 v2 §2「接口」).

⚠️ **响应模型是字段白名单**（与 app/schemas/wallet_adjustments.py 同一条）：每项恰好设计 §2
的 12 个键。内部自增 id、`tenant_id`、`wallet_id`、`created_by`（内部用户 id）、
`metadata_json`、用量事件的成本与毛利都不会因为表多了一列而静默出现在响应里（INV-7）。

⚠️ 金额是恰好 8 位小数的字符串（`money_text`），原样来自 `DECIMAL(20,8)` 列，不经 float
（INV-10）。

分页是序号游标，不是 spec §108 的 `page` / `total`（设计 §9）：调用方取 `page_size + 1`
行，多出来的那一行只用来判断还有没有下一页，不返回、也不当游标；游标是本页最后一行的
序号（设计 v2 §2）。
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from typing import Final

from pydantic import BaseModel

from app.models.wallet import ReferenceType, TransactionType
from app.repositories.wallet import LedgerRow
from app.schemas.customers import money_text

# `before_sequence` 的上界：`wallet_sequence` 是 BIGINT（设计 §2）。超出是 422。
MAX_WALLET_SEQUENCE: Final = 2**63 - 1


class WalletTransactionView(BaseModel):
    """One ledger row. `id` and `usage_event_id` are `public_id`s."""

    id: str
    wallet_sequence: int
    transaction_type: str
    amount: str
    balance_before: str
    balance_after: str
    reference_type: str
    reference_id: str
    # 只有反查得到用量事件的 `USAGE_EVENT` 行才有。
    usage_event_id: str | None
    description: str | None
    created_by_email: str | None
    created_at: dt.datetime


class WalletTransactionPage(BaseModel):
    """Newest first. `next_before_sequence` is `None` when there is nothing older."""

    items: list[WalletTransactionView]
    page_size: int
    next_before_sequence: int | None


def wallet_transaction_view(row: LedgerRow) -> WalletTransactionView:
    return WalletTransactionView(
        id=row.public_id,
        wallet_sequence=row.wallet_sequence,
        transaction_type=TransactionType(row.transaction_type).value,
        amount=money_text(row.amount),
        balance_before=money_text(row.balance_before),
        balance_after=money_text(row.balance_after),
        reference_type=ReferenceType(row.reference_type).value,
        reference_id=row.reference_id,
        usage_event_id=row.usage_event_public_id,
        description=row.description,
        created_by_email=row.created_by_email,
        created_at=row.created_at,
    )


def wallet_transaction_page(rows: Sequence[LedgerRow], *, page_size: int) -> WalletTransactionPage:
    """`rows` holds up to `page_size + 1` rows, newest first; the extra one only says "more"."""
    shown = rows[:page_size]
    more = len(rows) > page_size
    return WalletTransactionPage(
        items=[wallet_transaction_view(row) for row in shown],
        page_size=page_size,
        next_before_sequence=shown[-1].wallet_sequence if more else None,
    )

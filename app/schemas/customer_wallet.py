"""Response shapes for the customer wallet (design gate #393 v3 §2, §6; #433 v2 §2「接口三」).

⚠️ **这是客户侧的字段白名单**（INV-7）：钱包恰好 8 个键，流水每项恰好 7 个键。刻意不继承、不复用
app/schemas/wallet_transactions.py 的管理端模型：管理端以后加字段，客户端不会跟着出现。库里的
`description`（管理员写的调账原因）、`reference_type`、调账的 `reference_id`（幂等键）、
操作者邮箱、`balance_before`、用量事件的 `public_id`、钱包 `version` 与任何内部 id 一律不出这里。

⚠️ 金额是恰好 8 位小数的字符串（`money_text`），原样来自 `DECIMAL(20,8)` 列，不经 float、不舍入
（INV-10）。显示精度是前端的事（设计 §2「金额（前端显示）」）。

钱包流水导出（AIH-TASK-116，设计闸门 #435 v10 §2「接口二」）：列恰好是流水每项的
7 个键，只有时间列改名为 `created_at_utc`（值同 `created_at`）。流水接口与导出共用
`customer_ledger_line_view` 这一份映射，它只读 `CustomerLedgerLine` 的 8 个属性：
白名单只有一份。
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from decimal import Decimal
from typing import Final, Protocol

from pydantic import BaseModel

from app.models.tenancy import BillingMode, IntegrationStatus, compose_effective_status
from app.models.wallet import ReferenceType, TransactionType
from app.repositories.wallet import LedgerRow, WalletSummary
from app.schemas.customers import money_text

# 钱包流水导出的表头（设计 #435 §2「接口二」），按这个顺序。
WALLET_EXPORT_COLUMNS: Final = (
    "id",
    "wallet_sequence",
    "transaction_type",
    "amount",
    "balance_after",
    "event_id",
    "created_at_utc",
)
# 加公式注入前缀的文本列（设计 #435 §2「CSV 格式」）；序号、金额与时间列不加。
WALLET_EXPORT_TEXT_COLUMNS: Final = frozenset({"id", "transaction_type", "event_id"})
# 唯一改名的列：导出列名 → 白名单视图的键。
_WALLET_EXPORT_RENAMED: Final = {"created_at_utc": "created_at"}


class CustomerLedgerLine(Protocol):
    """The only attributes of a ledger row the customer whitelist may read."""

    @property
    def public_id(self) -> str: ...
    @property
    def wallet_sequence(self) -> int: ...
    @property
    def transaction_type(self) -> str: ...
    @property
    def amount(self) -> Decimal: ...
    @property
    def balance_after(self) -> Decimal: ...
    @property
    def reference_type(self) -> str: ...
    @property
    def reference_id(self) -> str: ...
    @property
    def created_at(self) -> dt.datetime: ...


class CustomerWalletView(BaseModel):
    """The caller's own wallet: balance, billing state, and whether AI service is on."""

    currency: str
    balance: str
    billing_mode: str
    billing_status: str
    ai_service: str
    # `ai_service` 为 `BLOCK_AI` 时才有。
    ai_service_reason: str | None
    # 管理员设的提醒线，未设置为 `None`。
    low_balance_threshold: str | None
    low_balance: bool


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
    threshold = summary.low_balance_threshold
    return CustomerWalletView(
        currency=summary.currency,
        balance=money_text(summary.balance),
        billing_mode=summary.billing_mode.value,
        billing_status=summary.billing_status.value,
        ai_service=effective.value,
        ai_service_reason=None if reason is None else reason.value,
        low_balance_threshold=None if threshold is None else money_text(threshold),
        low_balance=is_low_balance(summary),
    )


def is_low_balance(summary: WalletSummary) -> bool:
    """设计闸门 #433 v2 §2「接口三」：PREPAID、阈值非空、且余额 ≤ 阈值（等号算低余额）。

    与 `post_transaction` 写 `tenant.low_balance` 同一规则。两边都是 `Decimal`，不经 float
    （INV-10）。内部计量恒为 `False`：它的余额通常为 0、AI 照常可用，显示低余额就是误报。
    """
    threshold = summary.low_balance_threshold
    return (
        summary.billing_mode is BillingMode.PREPAID
        and threshold is not None
        and summary.balance <= threshold
    )


def customer_wallet_transaction_view(row: LedgerRow) -> CustomerWalletTransactionView:
    return customer_ledger_line_view(row)


def customer_ledger_line_view(row: CustomerLedgerLine) -> CustomerWalletTransactionView:
    """The 7-key whitelist, shared by the ledger page and the ledger export."""
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


def customer_wallet_export_cells(row: CustomerLedgerLine) -> list[str | None]:
    """One CSV row in `WALLET_EXPORT_COLUMNS` order: the JSON values of the 7-key view as text.

    时间与 JSON 接口的 `created_at` 逐字相同（不带时区的 UTC）；序号变成十进制字符串，
    金额是原样的 8 位小数字符串；`null`（非用量行的 `event_id`）留给 CSV 层写成空字段。
    """
    view = customer_ledger_line_view(row).model_dump(mode="json")
    cells: list[str | None] = []
    for name in WALLET_EXPORT_COLUMNS:
        value = view[_WALLET_EXPORT_RENAMED.get(name, name)]
        cells.append(None if value is None else str(value))
    return cells

"""Request and response shapes for admin wallet adjustments (design gate #111 v1 §2).

⚠️ **金额只收带符号的 JSON 字符串**，就是记进账本的那个数（INV-10）。JSON 数字一律
422：浮点数进不来。超过 8 位小数也是 422，**不舍入** —— spec §80 那唯一一次舍入在
定价层，账本层再舍入一次就成了第二次。

⚠️ 请求体 `extra="forbid"`：不能带 `customer_id`、`tenant_id`、`created_by`、余额、
`metadata` 或任何 id。客户只来自路径，操作者只来自令牌（INV-8）。

⚠️ 响应模型是字段白名单，不直接序列化 ORM 对象：内部自增 id、`created_by`（内部
用户 id）、`metadata_json` 都不会因为账本多了一列而静默出现在响应里（INV-7）。
"""

from __future__ import annotations

import datetime as dt
import re
from decimal import Decimal
from typing import Annotated, Final

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    StringConstraints,
    ValidationInfo,
    WithJsonSchema,
    field_validator,
)

from app.models.tenancy import BillingStatus
from app.models.wallet import (
    CREDIT_TYPES,
    REFERENCE_TYPE_FOR,
    ReferenceType,
    TransactionType,
    WalletTransaction,
)
from app.schemas.customers import money_text

# 调账只收映射到 ADMIN_ADJUSTMENT 的四种。TOPUP、AI_USAGE、SYSTEM_CORRECTION、rebill
# 都不从这里进（设计 §1）。
ADJUSTMENT_TYPES: Final = frozenset(
    kind for kind, source in REFERENCE_TYPE_FOR.items() if source is ReferenceType.ADMIN_ADJUSTMENT
)

# 整数部分最多 12 位（DECIMAL(20,8)），小数最多 8 位。⚠️ 用 [0-9] 而不是 \d：
# \d 还认全角与其他文字的数字。
_AMOUNT_PATTERN: Final = re.compile(r"-?[0-9]{1,12}(\.[0-9]{1,8})?")
# 小写 uuid，由前端每次打开调账表单时生成。存成账本的 reference_id。
_IDEMPOTENCY_KEY_PATTERN: Final = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
)


def _parse_amount(value: object) -> Decimal:
    """A signed decimal string → `Decimal`, never through float. Anything else is refused."""
    # 422 只列字段名，不回显值（app/core/errors.py），所以这里的文案里也不放值。
    if not isinstance(value, str):
        raise ValueError("amount must be a JSON string")
    if _AMOUNT_PATTERN.fullmatch(value) is None:
        raise ValueError("amount is not a decimal with at most 12 integer and 8 fraction digits")
    amount = Decimal(value)
    if amount.is_zero():
        raise ValueError("amount must not be zero")
    return amount


_Amount = Annotated[
    Decimal,
    BeforeValidator(_parse_amount),
    WithJsonSchema({"type": "string", "pattern": _AMOUNT_PATTERN.pattern}),
]
# 去掉首尾空白后长度 1–255：就是账本的 `description` 列，也就是 spec §60 的 reason。
_Reason = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)]


class CreateAdjustmentRequest(BaseModel):
    """⚠️ 字段的声明顺序有用：校验 `amount` 的符号时要读已经校验过的 `transaction_type`。"""

    model_config = ConfigDict(extra="forbid")

    transaction_type: TransactionType
    amount: _Amount
    reason: _Reason
    idempotency_key: str

    @field_validator("transaction_type")
    @classmethod
    def _only_adjustment_types(cls, value: TransactionType) -> TransactionType:
        if value not in ADJUSTMENT_TYPES:
            raise ValueError("transaction_type is not an adjustment type")
        return value

    @field_validator("amount")
    @classmethod
    def _sign_matches_the_type(cls, value: Decimal, info: ValidationInfo) -> Decimal:
        kind = info.data.get("transaction_type")
        if kind is None:
            # 类型本身已经不合法，那一条错误已经记下了。
            return value
        # 贷方为正、借方为负（设计 §9 第一行：方向要写对两次）。
        if (kind in CREDIT_TYPES) != (value > 0):
            raise ValueError("the sign of amount does not match transaction_type")
        return value

    @field_validator("idempotency_key", mode="before")
    @classmethod
    def _lowercase_uuid(cls, value: object) -> object:
        # MySQL 上 reference_id 是二进制排序规则，大小写不同就是不同的键，所以在这里
        # 统一成只收小写（设计 §2「数据库」）。
        if not isinstance(value, str) or _IDEMPOTENCY_KEY_PATTERN.fullmatch(value) is None:
            raise ValueError("idempotency_key must be a lowercase uuid")
        return value


class AdjustmentView(BaseModel):
    """`id` 到 `created_at` 取自账本行，重放时与第一次逐字相同。

    `billing_status` / `status_version` 是提交时租户的当前值，重放时可能与第一次不同。
    """

    id: str
    customer_id: str
    transaction_type: str
    # 金额都是恰好 8 位小数的字符串，永远不是浮点数。
    amount: str
    balance_before: str
    balance_after: str
    wallet_sequence: int
    reason: str
    idempotency_key: str
    created_at: dt.datetime
    replayed: bool
    billing_status: str
    status_version: int


def adjustment_view(
    row: WalletTransaction,
    *,
    customer_id: str,
    replayed: bool,
    billing_status: BillingStatus,
    status_version: int,
) -> AdjustmentView:
    return AdjustmentView(
        id=row.public_id,
        customer_id=customer_id,
        transaction_type=TransactionType(row.transaction_type).value,
        amount=money_text(row.amount),
        balance_before=money_text(row.balance_before),
        balance_after=money_text(row.balance_after),
        wallet_sequence=row.wallet_sequence,
        reason=row.description or "",
        idempotency_key=row.reference_id,
        created_at=row.created_at,
        replayed=replayed,
        billing_status=BillingStatus(billing_status).value,
        status_version=status_version,
    )

"""Request and response shapes for admin wallet adjustments (design gate #111 v1 §2).

⚠️ **金额只收 JSON 字符串**（INV-10）：JSON 数字一律 422，不让浮点数进来。字符串按
正则校验后 `Decimal(value)`，超过 8 位小数是 422，**不舍入** —— spec §80 那唯一一次舍入
在定价层。符号就是记进账本的符号，必须与类型一致（设计 §9 第一行）。

⚠️ 响应模型是字段白名单，不直接序列化 ORM 对象（与 app/schemas/customers.py 同一条）：
没有内部自增 id、`created_by`、`metadata_json`、低余额阈值、成本或毛利（INV-7）。
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Annotated, Final, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Strict,
    StringConstraints,
    ValidationInfo,
    field_validator,
)

from app.models.tenancy import Tenant
from app.models.wallet import CREDIT_TYPES, TransactionType, WalletTransaction
from app.schemas.customers import money_text

# `REFERENCE_TYPE_FOR` 里映射到 ADMIN_ADJUSTMENT 的四种（设计 §2）。TOPUP、AI_USAGE、
# SYSTEM_CORRECTION 与 rebill 都不从这里进；两边一致由测试钉住。
AdjustmentType = Literal["ADJUSTMENT_CREDIT", "ADJUSTMENT_DEBIT", "BONUS", "REFUND_ADJUSTMENT"]

# 整数部分最多 12 位、小数最多 8 位，即 DECIMAL(20,8)。⚠️ 用 `[0-9]` 而不是 `\d`：
# `\d` 还匹配全角与别的文字的数字，`Decimal()` 也认它们。
AMOUNT_PATTERN: Final = r"^-?[0-9]{1,12}(\.[0-9]{1,8})?$"
IDEMPOTENCY_KEY_PATTERN: Final = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"

# `Strict()`：只收 JSON 字符串，数字不会被转成字符串放进来。
_Amount = Annotated[str, Strict(), StringConstraints(pattern=AMOUNT_PATTERN)]
# 就是账本的 `description`（spec §60 的 reason）：存去掉首尾空白后的值。
_Reason = Annotated[
    str, Strict(), StringConstraints(strip_whitespace=True, min_length=1, max_length=255)
]
# 小写 uuid，由前端每次打开调账表单时生成，存成账本的 `reference_id`。
_IdempotencyKey = Annotated[str, Strict(), StringConstraints(pattern=IDEMPOTENCY_KEY_PATTERN)]


class PostAdjustmentRequest(BaseModel):
    """⚠️ 多余字段一律拒绝：客户只来自路径，操作者只来自令牌（INV-8）。

    请求体不能带 `tenant_id`、`customer_id`、`created_by`、`balance`、`metadata` 或任何 id。
    """

    model_config = ConfigDict(extra="forbid")

    # ⚠️ 字段顺序有意义：`amount` 的校验要读已经校验过的 `transaction_type`。
    transaction_type: AdjustmentType
    amount: _Amount
    reason: _Reason
    idempotency_key: _IdempotencyKey

    @field_validator("amount")
    @classmethod
    def _non_zero_and_signed_like_the_type(cls, value: str, info: ValidationInfo) -> str:
        # 422 只列字段名，不回显这个值（app/core/errors.py）。
        number = Decimal(value)
        if number.is_zero():
            raise ValueError("amount must not be zero")
        kind = info.data.get("transaction_type")
        if kind is None:
            # 类型本身已经不合法，那边会报错；这里不再猜方向。
            return value
        if (TransactionType(kind) in CREDIT_TYPES) != (number > 0):
            raise ValueError("amount sign does not match transaction_type")
        return value

    def decimal_amount(self) -> Decimal:
        """The amount to post, built from the string, never through float."""
        return Decimal(self.amount)


class AdjustmentView(BaseModel):
    """设计 §2 的调账对象。`id` 到 `created_at` 取自账本行，重放时与第一次逐字相同。"""

    id: str
    customer_id: str
    transaction_type: str
    # 恰好 8 位小数的定点字符串，永远不是浮点数。
    amount: str
    balance_before: str
    balance_after: str
    wallet_sequence: int
    reason: str
    idempotency_key: str
    created_at: dt.datetime
    replayed: bool
    # 提交时租户的当前值：重放时可能与第一次不同（中间有别的记账）。
    billing_status: str
    status_version: int


def adjustment_view(row: WalletTransaction, tenant: Tenant, *, replayed: bool) -> AdjustmentView:
    return AdjustmentView(
        id=row.public_id,
        customer_id=tenant.public_id,
        transaction_type=row.transaction_type.value,
        amount=money_text(row.amount),
        balance_before=money_text(row.balance_before),
        balance_after=money_text(row.balance_after),
        wallet_sequence=row.wallet_sequence,
        # 调账的原因必填（数据库 CHECK），这里不会是 NULL。
        reason=row.description or "",
        idempotency_key=row.reference_id,
        created_at=row.created_at,
        replayed=replayed,
        billing_status=tenant.billing_status.value,
        status_version=tenant.status_version,
    )

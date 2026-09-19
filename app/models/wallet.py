"""Wallet and immutable ledger tables (spec §77, §78; design gate #88 v6).

⚠️ **余额只经账本变动（INV-4），账本只能插入（INV-5）。**两条都由迁移 0006
建的 MySQL 触发器强制，不靠这里的模型：模型上没有任何更新路径，而运行账号
直接写 SQL 也绕不过触发器。SQLite 上没有那些触发器，所以记账行为只在真
MySQL 上测（tests/backend/test_wallet_repository.py）。

金额一律 `Money`（DECIMAL(20,8)），有符号：贷方为正、借方为负。
"""

from __future__ import annotations

import datetime as dt
import enum
from collections.abc import Iterable
from decimal import Decimal
from typing import Any, Final

from sqlalchemy import (
    CHAR,
    JSON,
    BigInteger,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

# 账本行的外键指向 users 与 tenants：import 这两个模块，表才在同一个 metadata 里。
from app.models import auth as _auth_models  # noqa: F401
from app.models import tenancy as _tenancy_models  # noqa: F401
from app.models.base import Base, Money

# 与 app/models/auth.py 同一个写法（SQLite 只对 INTEGER PRIMARY KEY 自增）。
_PrimaryKey = BigInteger().with_variant(Integer, "sqlite")
_ForeignKeyInt = BigInteger().with_variant(Integer, "sqlite")

# 枚举列宽写死，理由见 app/models/auth.py 的 `_ENUM_LENGTH`。
_ENUM_LENGTH = 64
_REFERENCE_TYPE_LENGTH = 32
_REFERENCE_ID_LENGTH = 64
_DESCRIPTION_LENGTH = 255
_PUBLIC_ID_LENGTH = 36

# spec §5：V1 只有 MYR。
WALLET_CURRENCY: Final = "MYR"


class TransactionType(enum.StrEnum):
    """spec §8 的九种。"""

    TOPUP = "TOPUP"
    AI_USAGE = "AI_USAGE"
    ADJUSTMENT_CREDIT = "ADJUSTMENT_CREDIT"
    ADJUSTMENT_DEBIT = "ADJUSTMENT_DEBIT"
    REBILL_CREDIT = "REBILL_CREDIT"
    REBILL_DEBIT = "REBILL_DEBIT"
    # 系统外人工退款之后的扣回（spec §8），所以归借方。
    REFUND_ADJUSTMENT = "REFUND_ADJUSTMENT"
    BONUS = "BONUS"
    SYSTEM_CORRECTION = "SYSTEM_CORRECTION"


class ReferenceType(enum.StrEnum):
    """财务来源。`(reference_type, reference_id)` 唯一：每个来源只有一次效果。"""

    USAGE_EVENT = "USAGE_EVENT"
    PAYMENT = "PAYMENT"
    ADMIN_ADJUSTMENT = "ADMIN_ADJUSTMENT"
    REBILL = "REBILL"
    SYSTEM = "SYSTEM"


# 类型 ↔ 符号。三组互不相交且覆盖全部九种（test_wallet_rules.py 钉住）。
CREDIT_TYPES: Final = frozenset(
    {
        TransactionType.TOPUP,
        TransactionType.ADJUSTMENT_CREDIT,
        TransactionType.REBILL_CREDIT,
        TransactionType.BONUS,
    }
)
DEBIT_TYPES: Final = frozenset(
    {
        TransactionType.AI_USAGE,
        TransactionType.ADJUSTMENT_DEBIT,
        TransactionType.REBILL_DEBIT,
        TransactionType.REFUND_ADJUSTMENT,
    }
)
# 系统更正可正可负，只要求非零。
NONZERO_TYPES: Final = frozenset({TransactionType.SYSTEM_CORRECTION})

# 类型 ↔ 来源类型（设计 §2）。
REFERENCE_TYPE_FOR: Final = {
    TransactionType.AI_USAGE: ReferenceType.USAGE_EVENT,
    TransactionType.TOPUP: ReferenceType.PAYMENT,
    TransactionType.ADJUSTMENT_CREDIT: ReferenceType.ADMIN_ADJUSTMENT,
    TransactionType.ADJUSTMENT_DEBIT: ReferenceType.ADMIN_ADJUSTMENT,
    TransactionType.BONUS: ReferenceType.ADMIN_ADJUSTMENT,
    TransactionType.REFUND_ADJUSTMENT: ReferenceType.ADMIN_ADJUSTMENT,
    TransactionType.REBILL_CREDIT: ReferenceType.REBILL,
    TransactionType.REBILL_DEBIT: ReferenceType.REBILL,
    TransactionType.SYSTEM_CORRECTION: ReferenceType.SYSTEM,
}

# 调账与系统更正：原因必填（spec §8、§60），并与一条审计同事务写入。
ADJUSTMENT_REFERENCE_TYPES: Final = frozenset(
    {ReferenceType.ADMIN_ADJUSTMENT, ReferenceType.SYSTEM}
)


def _quoted(values: Iterable[str]) -> str:
    return ", ".join(f"'{value}'" for value in sorted(values))


def _reference_check() -> str:
    clauses = []
    for source in ReferenceType:
        allowed = _quoted(kind for kind, owner in REFERENCE_TYPE_FOR.items() if owner is source)
        clauses.append(f"(reference_type = '{source}' AND transaction_type IN ({allowed}))")
    return " OR ".join(clauses)


# ⚠️ 数据库 CHECK 由上面这几组映射**生成**，repository 的校验读的也是它们 ——
# 两层只有一份定义。迁移 0006 里是冻结的字面量，test_migrations.py 比对两边。
SIGN_CHECK: Final = " OR ".join(
    f"(transaction_type IN ({_quoted(kinds)}) AND amount {operator} 0)"
    for kinds, operator in ((CREDIT_TYPES, ">"), (DEBIT_TYPES, "<"), (NONZERO_TYPES, "<>"))
)
REFERENCE_CHECK: Final = _reference_check()
# 原因：非空，且去掉首尾空白后非空。⚠️ `IS NOT NULL` 不能省：TRIM(NULL) 是
# NULL，而 CHECK 把 NULL 当作「未违反」。
REASON_CHECK: Final = (
    f"reference_type NOT IN ({_quoted(ADJUSTMENT_REFERENCE_TYPES)})"
    + " OR (description IS NOT NULL AND TRIM(description) <> '')"
)
ACTOR_CHECK: Final = "reference_type <> 'ADMIN_ADJUSTMENT' OR created_by IS NOT NULL"
ARITHMETIC_CHECK: Final = "balance_after = balance_before + amount"
CURRENCY_CHECK: Final = f"currency = '{WALLET_CURRENCY}'"


class Wallet(Base):
    """spec §77. One per tenant; the balance may go negative (spec §7, rules 7–8).

    ⚠️ **不要直接改 `balance` / `version`。**库里的触发器拒绝任何不对应一行新
    账本的变动；唯一的写入口是 app/repositories/wallet.py 的 `post_transaction`。
    `version` 等于最后一行账本的 `wallet_sequence`。
    """

    __tablename__ = "wallets"

    id: Mapped[int] = mapped_column(_PrimaryKey, primary_key=True, autoincrement=True)
    # ⚠️ RESTRICT：删租户不许连带删掉钱包（钱包挂着账本，账本是财务记录）。
    tenant_id: Mapped[int] = mapped_column(
        _ForeignKeyInt,
        ForeignKey("tenants.id", name="fk_wallets_tenant_id", ondelete="RESTRICT"),
        nullable=False,
    )
    currency: Mapped[str] = mapped_column(CHAR(3), nullable=False)
    balance: Mapped[Money] = mapped_column(nullable=False, default=Decimal(0))
    version: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False)

    __table_args__ = (
        # 一租户一钱包。
        UniqueConstraint("tenant_id", name="uq_wallets_tenant_id"),
        # 供账本的复合外键引用：数据库保证账本行的租户与钱包一致（INV-8）。
        UniqueConstraint("id", "tenant_id", name="uq_wallets_id_tenant_id"),
        CheckConstraint(CURRENCY_CHECK, name="ck_wallets_currency"),
    )


class WalletTransaction(Base):
    """spec §78. Append-only: there is no update or delete path, here or in the database."""

    __tablename__ = "wallet_transactions"

    id: Mapped[int] = mapped_column(_PrimaryKey, primary_key=True, autoincrement=True)
    public_id: Mapped[str] = mapped_column(CHAR(_PUBLIC_ID_LENGTH), nullable=False)
    wallet_id: Mapped[int] = mapped_column(_ForeignKeyInt, nullable=False)
    tenant_id: Mapped[int] = mapped_column(_ForeignKeyInt, nullable=False)
    # 从 1 连续递增。与 `(wallet_id, wallet_sequence)` 唯一一起，丢更新会变成
    # 唯一冲突，而不是悄悄覆盖。
    wallet_sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    transaction_type: Mapped[TransactionType] = mapped_column(
        Enum(TransactionType, native_enum=False, length=_ENUM_LENGTH),
        nullable=False,
    )
    amount: Mapped[Money] = mapped_column(nullable=False)
    balance_before: Mapped[Money] = mapped_column(nullable=False)
    balance_after: Mapped[Money] = mapped_column(nullable=False)
    reference_type: Mapped[ReferenceType] = mapped_column(
        Enum(ReferenceType, native_enum=False, length=_REFERENCE_TYPE_LENGTH),
        nullable=False,
    )
    reference_id: Mapped[str] = mapped_column(String(_REFERENCE_ID_LENGTH), nullable=False)
    # 调账与系统更正时就是 spec §60 的 reason。⚠️ 只放业务说明，不放对话内容。
    description: Mapped[str | None] = mapped_column(String(_DESCRIPTION_LENGTH), nullable=True)
    # ⚠️ 只放客户可见的计费上下文：成本、毛利、prompt / response 类的键由
    # repository 拒绝（INV-7、INV-9）。
    metadata_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    # NULL = 系统。RESTRICT：删用户不许连带删账本。
    created_by: Mapped[int | None] = mapped_column(
        _ForeignKeyInt,
        ForeignKey("users.id", name="fk_wallet_transactions_created_by", ondelete="RESTRICT"),
        nullable=True,
    )
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False)

    __table_args__ = (
        ForeignKeyConstraint(
            ["wallet_id", "tenant_id"],
            ["wallets.id", "wallets.tenant_id"],
            name="fk_wallet_transactions_wallet",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("public_id", name="uq_wallet_transactions_public_id"),
        UniqueConstraint(
            "wallet_id",
            "wallet_sequence",
            name="uq_wallet_transactions_wallet_sequence",
        ),
        # 每个财务来源至多一行（INV-2、INV-3、INV-11 的数据库兜底）。
        UniqueConstraint(
            "reference_type",
            "reference_id",
            name="uq_wallet_transactions_reference",
        ),
        # 按租户分页查流水（keyset，按序号倒序）。
        Index("ix_wallet_transactions_tenant_sequence", "tenant_id", "wallet_sequence"),
        CheckConstraint(ARITHMETIC_CHECK, name="ck_wallet_transactions_arithmetic"),
        CheckConstraint(SIGN_CHECK, name="ck_wallet_transactions_sign"),
        CheckConstraint(REFERENCE_CHECK, name="ck_wallet_transactions_reference_type"),
        CheckConstraint(REASON_CHECK, name="ck_wallet_transactions_reason"),
        CheckConstraint(ACTOR_CHECK, name="ck_wallet_transactions_actor"),
    )

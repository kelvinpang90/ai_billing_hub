"""Provider price versions and their price components (design gate #177 v3, AIH-TASK-026).

spec §14、§15.1、§17、§74.1。两张表：

- `provider_price_versions`：一个（供应商, 模型）的一版成本价，原币种（ADR-0005 §5）。
  状态 `DRAFT → PUBLISHED → RETIRED`，草稿也可以 `DISCARDED`。已发布（`PUBLISHED` /
  `RETIRED`）的版本在 `[effective_from, effective_to)` 内生效；同一（供应商, 模型）的
  非空区间首尾相接、不重叠，至多一个未截断的已发布版本（生成列 `open_slot` + 唯一索引）；
- `provider_price_components`：版本的价格分量。每个分量引用 025 的一个计价分量
  （`usage_meter_components`），`component_code`、计量类型与单位都由它唯一确定。

⚠️ **发布后不可变**由 MySQL 触发器保证（迁移 0013）：只能以草稿插入；已发布的行只许改
`effective_to`、`PUBLISHED → RETIRED` 与 `updated_at`；分量只在草稿阶段可增删改；版本一律
不许删除；「草稿 → 已发布」跃迁上校验分量完整；锁供应商行后检查区间不重叠。SQLite 上没有
这些触发器，服务层按同样的规则写。

⚠️ 条件文本与生成列表达式同迁移 0013 逐字一致（空白除外），test_migrations.py 比对两边。
"""

from __future__ import annotations

import datetime as dt
import enum
from decimal import Decimal
from typing import Any, Final

from sqlalchemy import (
    CHAR,
    JSON,
    BigInteger,
    CheckConstraint,
    Computed,
    DateTime,
    Enum,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import MONEY_PRECISION, MONEY_SCALE, Base

# 与 app/models/auth.py 同一个写法（SQLite 只对 INTEGER PRIMARY KEY 自增）。
_PrimaryKey = BigInteger().with_variant(Integer, "sqlite")
_ForeignKeyInt = BigInteger().with_variant(Integer, "sqlite")

_PUBLIC_ID_LENGTH = 36

# 设计 §2 写死的列宽。
CURRENCY_LENGTH: Final = 3
SOURCE_TYPE_LENGTH: Final = 16
SOURCE_REFERENCE_LENGTH: Final = 255
STATUS_LENGTH: Final = 16


class PriceVersionStatus(enum.StrEnum):
    """设计 §2「状态」。`DISCARDED` 是 §74.1 三个取值之外补的：丢弃的草稿留痕、不删除。"""

    DRAFT = "DRAFT"
    PUBLISHED = "PUBLISHED"
    RETIRED = "RETIRED"
    DISCARDED = "DISCARDED"


class PriceSourceType(enum.StrEnum):
    """本任务只写 `MANUAL`；`SYNC` 留给 Phase 8 的供应商价格同步。"""

    MANUAL = "MANUAL"
    SYNC = "SYNC"


# 已发布过的两种状态：有区间、参与取价。
PERIOD_STATUSES: Final = (PriceVersionStatus.PUBLISHED, PriceVersionStatus.RETIRED)

# ⚠️ 条件文本与迁移 0013 逐字一致（空白除外），test_migrations.py 比对两边。
SOURCE_TYPE_CHECK: Final = "source_type IN ('MANUAL', 'SYNC')"
VERSION_STATUS_CHECK: Final = "status IN ('DRAFT', 'PUBLISHED', 'RETIRED', 'DISCARDED')"
# 草稿与丢弃的草稿没有区间、没有发布人。
UNPUBLISHED_CHECK: Final = (
    "status NOT IN ('DRAFT', 'DISCARDED') OR (effective_from IS NULL AND effective_to IS NULL"
    " AND approved_by IS NULL AND approved_at IS NULL)"
)
# 发布过的一定有发布人与发布时刻。
APPROVED_CHECK: Final = (
    "status NOT IN ('PUBLISHED', 'RETIRED')"
    " OR (approved_by IS NOT NULL AND approved_at IS NOT NULL)"
)
# 区间不能倒置；唯一允许的空区间是「撤销尚未生效的预约版本」留下的（设计 §2「退役」）。
PERIOD_CHECK: Final = (
    "effective_from IS NULL OR effective_to IS NULL OR effective_from < effective_to"
    " OR (status = 'RETIRED' AND effective_from = effective_to)"
)
# 只有未截断的已发布版本有值，其余是 NULL。唯一索引允许多个 NULL，所以
# 「至多一个未截断的已发布版本」由数据库保证（写法同 `ai_model_aliases.open_slot`）。
OPEN_SLOT_EXPRESSION: Final = "CASE WHEN status = 'PUBLISHED' AND effective_to IS NULL THEN 1 END"
UNIT_QUANTITY_CHECK: Final = "unit_quantity > 0"
RATE_AMOUNT_CHECK: Final = "rate_amount > 0"


def _decimal_20_8() -> Numeric:
    # 与金额同一精度（DECIMAL(20,8)，INV-10）；单价与「多少个单位」都不经过 float。
    return Numeric(MONEY_PRECISION, MONEY_SCALE, asdecimal=True)


class ProviderPriceVersion(Base):
    """One version of a provider's cost price for one model, in the source currency.

    `effective_from IS NULL` = 「一直以来」，只有该（供应商, 模型）第一个已发布版本在未指定
    生效时刻时是它；`effective_to IS NULL` = 仍生效（或尚未发布）。发布后只有 `effective_to`
    （截断、撤销预约时恢复）、`status`（`PUBLISHED → RETIRED`）与 `updated_at` 会变。
    """

    __tablename__ = "provider_price_versions"

    id: Mapped[int] = mapped_column(_PrimaryKey, primary_key=True, autoincrement=True)
    public_id: Mapped[str] = mapped_column(CHAR(_PUBLIC_ID_LENGTH), nullable=False)
    provider_id: Mapped[int] = mapped_column(_ForeignKeyInt, nullable=False)
    model_id: Mapped[int] = mapped_column(_ForeignKeyInt, nullable=False)
    # ISO 4217 大写三字母，格式在接口层校验。可以是 MYR（此时 T-G 不需要 FX）。
    source_currency: Mapped[str] = mapped_column(CHAR(CURRENCY_LENGTH), nullable=False)
    source_type: Mapped[PriceSourceType] = mapped_column(
        Enum(PriceSourceType, native_enum=False, length=SOURCE_TYPE_LENGTH), nullable=False
    )
    # 价格出处（例如供应商价格页与查看日期）。
    source_reference: Mapped[str] = mapped_column(String(SOURCE_REFERENCE_LENGTH), nullable=False)
    status: Mapped[PriceVersionStatus] = mapped_column(
        Enum(PriceVersionStatus, native_enum=False, length=STATUS_LENGTH), nullable=False
    )
    effective_from: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    effective_to: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    # 生成列（STORED）：只读，ORM 从不写它。
    open_slot: Mapped[int | None] = mapped_column(
        Integer, Computed(OPEN_SLOT_EXPRESSION, persisted=True)
    )
    created_by: Mapped[int] = mapped_column(_ForeignKeyInt, nullable=False)
    # 发布人；发布时写。
    approved_by: Mapped[int | None] = mapped_column(_ForeignKeyInt, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False)
    # 发布时刻。
    approved_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)

    __table_args__ = (
        # 版本只能挂在同一供应商的模型上，由数据库保证。RESTRICT：模型删不掉（INV-6）。
        ForeignKeyConstraint(
            ["model_id", "provider_id"],
            ["ai_models.id", "ai_models.provider_id"],
            name="fk_provider_price_versions_model",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name="fk_provider_price_versions_created_by",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["approved_by"],
            ["users.id"],
            name="fk_provider_price_versions_approved_by",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("public_id", name="uq_provider_price_versions_public_id"),
        # 一个（供应商, 模型）至多一个未截断的已发布版本；也是按（供应商, 模型）查版本的索引。
        Index(
            "ux_provider_price_versions_open_slot",
            "provider_id",
            "model_id",
            "open_slot",
            unique=True,
        ),
        # 复合外键在子表上的索引，显式建出来（不让 MySQL 自动起名）。
        Index("ix_provider_price_versions_model", "model_id", "provider_id"),
        CheckConstraint(SOURCE_TYPE_CHECK, name="ck_provider_price_versions_source_type"),
        CheckConstraint(VERSION_STATUS_CHECK, name="ck_provider_price_versions_status"),
        CheckConstraint(UNPUBLISHED_CHECK, name="ck_provider_price_versions_unpublished"),
        CheckConstraint(APPROVED_CHECK, name="ck_provider_price_versions_approved"),
        CheckConstraint(PERIOD_CHECK, name="ck_provider_price_versions_period"),
    )


class ProviderPriceComponent(Base):
    """One priced component of a version: `rate_amount` per `unit_quantity` units.

    `metadata_json` 只作备注（例如供应商的档位名），**计价不读它**：区分价格的维度必须是
    不同的分量（§15.1）。
    """

    __tablename__ = "provider_price_components"

    id: Mapped[int] = mapped_column(_PrimaryKey, primary_key=True, autoincrement=True)
    provider_price_version_id: Mapped[int] = mapped_column(_ForeignKeyInt, nullable=False)
    usage_meter_component_id: Mapped[int] = mapped_column(_ForeignKeyInt, nullable=False)
    # 多少个计量单位对应一个 `rate_amount`（例如每 1 000 000 个 token）。
    unit_quantity: Mapped[Decimal] = mapped_column(_decimal_20_8(), nullable=False)
    # 原币种单价，必须为正（§74.1）。
    rate_amount: Mapped[Decimal] = mapped_column(_decimal_20_8(), nullable=False)
    metadata_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False)

    __table_args__ = (
        ForeignKeyConstraint(
            ["provider_price_version_id"],
            ["provider_price_versions.id"],
            name="fk_provider_price_components_version",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["usage_meter_component_id"],
            ["usage_meter_components.id"],
            name="fk_provider_price_components_meter_component",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "provider_price_version_id",
            "usage_meter_component_id",
            name="uq_provider_price_components_version_component",
        ),
        Index("ix_provider_price_components_meter_component", "usage_meter_component_id"),
        CheckConstraint(UNIT_QUANTITY_CHECK, name="ck_provider_price_components_unit_quantity"),
        CheckConstraint(RATE_AMOUNT_CHECK, name="ck_provider_price_components_rate_amount"),
    )


__all__ = [
    "APPROVED_CHECK",
    "CURRENCY_LENGTH",
    "OPEN_SLOT_EXPRESSION",
    "PERIOD_CHECK",
    "PERIOD_STATUSES",
    "RATE_AMOUNT_CHECK",
    "SOURCE_REFERENCE_LENGTH",
    "SOURCE_TYPE_CHECK",
    "UNIT_QUANTITY_CHECK",
    "UNPUBLISHED_CHECK",
    "VERSION_STATUS_CHECK",
    "PriceSourceType",
    "PriceVersionStatus",
    "ProviderPriceComponent",
    "ProviderPriceVersion",
]

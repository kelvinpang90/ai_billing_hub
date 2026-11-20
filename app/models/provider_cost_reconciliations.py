"""Provider cost reconciliations (design gate #755 v1, AIH-TASK-242).

spec §14、§17 / §17.1、§19、§74.5（`provider_cost_reconciliations`，只追加）；Invariant 6、10。
一行是一个供应商 × 范围 × UTC 自然月的实际成本记录：管理员按供应商账单或 Console 成本页录入的
来源币种原值（不含税）、按期间最后一刻生效的已发布汇率版本换算后的 MYR、确认那一刻范围内的估算
与诊断计数快照。范围只有 `AI_MODULE`（经 AI 模块发出的调用），覆盖方式只有 `PERIOD_AGGREGATE`。

更正 = 追加一行取代行（`supersedes_id` 指向被取代的那一行，必填 `reason`），不改旧行。同一供应商、
范围、月份至多一个根（`ux_provider_cost_reconciliations_root` 与生成列 `root_slot`），每行至多被
取代一次（`uq_provider_cost_reconciliations_supersedes`），取代者与被取代者必是同一供应商、范围、
月份（复合自引用外键）：所以每月的记录是一条单链。「当前版本」= 没有任何行的 `supersedes_id`
指向它；「已被取代」= 有。没有状态列，也没有 `metadata_json`。

本任务只有数据库层：唯一的写入方是 AIH-TASK-244 的 `confirm`（只 INSERT），仓储、接口与页面属于
AIH-TASK-243 起的任务。时间一律不带时区的 UTC。

⚠️ **行不可改、不可删**由 MySQL 触发器保证（迁移 0041）：
`trg_provider_cost_reconciliations_before_update` 与
`trg_provider_cost_reconciliations_before_delete` 无条件 SIGNAL 45000。
SQLite 上没有这些触发器，服务层本来就没有更新与删除路径。

⚠️ 索引名、外键名、生成列表达式与条件文本同迁移 0041 一致（空白除外），
tests/backend/test_provider_cost_reconciliations_migration.py 比对两边。
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Final

from sqlalchemy import (
    CHAR,
    BigInteger,
    CheckConstraint,
    Computed,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

# 外键指向 ai_providers、fx_rate_versions 与 users：import 这几个模块，表才在同一个 metadata 里
# （fx_rates 由下面取汇率精度的 import 一并装上）。
from app.models import ai_catalog as _ai_catalog_models  # noqa: F401
from app.models import auth as _auth_models  # noqa: F401
from app.models.base import Base, Money
from app.models.fx_rates import RATE_PRECISION, RATE_SCALE

# 与 app/models/receipts.py 同一个写法（SQLite 只对 INTEGER PRIMARY KEY 自增）。
_PrimaryKey = BigInteger().with_variant(Integer, "sqlite")
_ForeignKeyInt = BigInteger().with_variant(Integer, "sqlite")

_PUBLIC_ID_LENGTH = 36
_CURRENCY_LENGTH = 3
# 设计 §2 写死的列宽。
SCOPE_LENGTH: Final = 16
BILLING_MONTH_LENGTH: Final = 7
SOURCE_DOCUMENT_REFERENCE_LENGTH: Final = 255
COVERAGE_METHOD_LENGTH: Final = 32
REASON_LENGTH: Final = 255

_TABLE = "provider_cost_reconciliations"

# 生成列（STORED）：根为 1，取代行为 NULL（不是 0）；唯一索引不管 NULL，所以「同一供应商、范围、
# 月份至多一个根」由数据库保证（写法同 `fx_rate_versions.open_slot`）。
ROOT_SLOT_EXPRESSION: Final = "CASE WHEN supersedes_id IS NULL THEN 1 END"

# ⚠️ 条件文本与迁移 0041 一致（空白除外）。MYR 录入时两个汇率列都为空、换算后等于原值；否则都
# 非空且汇率大于 0。逐列写 `IS NOT NULL`，不用行构造器（同 app/models/usage.py 的
# `PROCESSED_SNAPSHOT_CHECK` 注释的理由）。
FX_CHECK: Final = (
    "(source_currency = 'MYR' AND fx_rate_version_id IS NULL AND fx_rate_applied IS NULL"
    " AND reconciled_cost_myr = source_total)"
    " OR (source_currency <> 'MYR' AND fx_rate_version_id IS NOT NULL"
    " AND fx_rate_applied IS NOT NULL AND fx_rate_applied > 0)"
)
# 根没有理由；取代行必有理由。
REASON_CHECK: Final = (
    "(supersedes_id IS NULL AND reason IS NULL)"
    " OR (supersedes_id IS NOT NULL AND reason IS NOT NULL)"
)
SCOPE_CHECK: Final = "scope IN ('AI_MODULE')"
COVERAGE_METHOD_CHECK: Final = "coverage_method IN ('PERIOD_AGGREGATE')"
PERIOD_CHECK: Final = "period_start < period_end"
# 金额与计数都不为负：检查名的后缀 → 列。
NON_NEGATIVE_COLUMNS: Final = {
    "source_total": "source_total",
    "reconciled_cost": "reconciled_cost_myr",
    "estimated_source_cost": "estimated_source_cost",
    "estimated_cost_myr": "estimated_cost_myr",
    "estimated_event_count": "estimated_event_count",
    "unprocessed_event_count": "unprocessed_event_count",
    "outbox_unsettled_count": "outbox_unsettled_count",
    "outbox_discarded_count": "outbox_discarded_count",
}


def _rate() -> Numeric:
    # DECIMAL(24,10)，与 `fx_rate_versions.rate` 同精度，读出来是 Decimal（INV-10）。
    return Numeric(RATE_PRECISION, RATE_SCALE, asdecimal=True)


class ProviderCostReconciliation(Base):
    """One provider's actual cost for one scope and UTC calendar month (设计 §2「数据库」).

    ⚠️ 不要直接 UPDATE 或 DELETE 这张表：MySQL 上的触发器无条件拒绝改删任何一行（SQLSTATE
    45000），对账记录与审计永久保留。记录写错 → 追加一行取代行（带理由），不删行。唯一的写入方是
    AIH-TASK-244 的 `confirm`，只 INSERT。
    """

    __tablename__ = _TABLE

    id: Mapped[int] = mapped_column(_PrimaryKey, primary_key=True, autoincrement=True)
    # uuid4；对外 id（接口里的 `reconciliation_id`）。
    public_id: Mapped[str] = mapped_column(CHAR(_PUBLIC_ID_LENGTH), nullable=False)
    provider_id: Mapped[int] = mapped_column(_ForeignKeyInt, nullable=False)
    # 只有 `AI_MODULE`：`event_id` 出现在 `ai_usage_outbox` 里的事件（CHECK）。
    scope: Mapped[str] = mapped_column(String(SCOPE_LENGTH), nullable=False)
    # `YYYY-MM`，UTC 自然月；格式由应用层校验（同 `base_currency` 不做正则 CHECK 的惯例）。
    billing_month: Mapped[str] = mapped_column(CHAR(BILLING_MONTH_LENGTH), nullable=False)
    # `[月首 00:00, 下月首 00:00)`，不带时区的 UTC。
    period_start: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False)
    period_end: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False)
    # 来源说明（例：工作区名称 + 月份 + 页面或账单号）；不写合同价。
    source_document_reference: Mapped[str] = mapped_column(
        String(SOURCE_DOCUMENT_REFERENCE_LENGTH), nullable=False
    )
    source_currency: Mapped[str] = mapped_column(CHAR(_CURRENCY_LENGTH), nullable=False)
    # 录入的实际成本（来源币种原值，不含税）。
    source_total: Mapped[Money] = mapped_column(nullable=False)
    # 期间最后一刻（`period_end − 1µs`）生效的已发布汇率版本与汇率值；MYR 录入时都为空。
    fx_rate_version_id: Mapped[int | None] = mapped_column(_ForeignKeyInt, nullable=True)
    fx_rate_applied: Mapped[Decimal | None] = mapped_column(_rate(), nullable=True)
    # 换算后的实际 MYR 成本；MYR 录入时等于 `source_total`。
    reconciled_cost_myr: Mapped[Money] = mapped_column(nullable=False)
    # 只有 `PERIOD_AGGREGATE`：供应商 × 期间的一个合计数（CHECK）。
    coverage_method: Mapped[str] = mapped_column(String(COVERAGE_METHOD_LENGTH), nullable=False)
    # 确认那一刻的估算与诊断计数快照（设计 §2「口径」）。
    estimated_event_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    unprocessed_event_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    outbox_unsettled_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    outbox_discarded_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    estimated_source_cost: Mapped[Money] = mapped_column(nullable=False)
    estimated_cost_myr: Mapped[Money] = mapped_column(nullable=False)
    # 被本行取代的那一行；根为空。
    supersedes_id: Mapped[int | None] = mapped_column(_ForeignKeyInt, nullable=True)
    # 更正理由；根为空，取代行必填（CHECK）。
    reason: Mapped[str | None] = mapped_column(String(REASON_LENGTH), nullable=True)
    created_by: Mapped[int] = mapped_column(_ForeignKeyInt, nullable=False)
    # 整秒 UTC。
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False)
    # 生成列（STORED）：只读，ORM 从不写它。
    root_slot: Mapped[int | None] = mapped_column(
        Integer, Computed(ROOT_SLOT_EXPRESSION, persisted=True)
    )

    __table_args__ = (
        # 同一供应商、范围、月份至多一个根；也是 provider_id 外键用的索引。
        Index(
            "ux_provider_cost_reconciliations_root",
            "provider_id",
            "scope",
            "billing_month",
            "root_slot",
            unique=True,
        ),
        UniqueConstraint("public_id", name="uq_provider_cost_reconciliations_public_id"),
        # 每行至多被取代一次；也是复合自引用外键在子表上的索引（前导列同名同序）。
        UniqueConstraint(
            "supersedes_id",
            "provider_id",
            "scope",
            "billing_month",
            name="uq_provider_cost_reconciliations_supersedes",
        ),
        # 复合自引用外键在父表一侧引用的键。
        UniqueConstraint(
            "id",
            "provider_id",
            "scope",
            "billing_month",
            name="uq_provider_cost_reconciliations_chain_key",
        ),
        # 显式声明：外键用它们，MySQL 不另建隐式索引。
        Index("ix_provider_cost_reconciliations_fx_rate_version_id", "fx_rate_version_id"),
        Index("ix_provider_cost_reconciliations_created_by", "created_by"),
        # RESTRICT：删供应商、汇率版本、用户或被取代的行都不许连带删掉记录。
        ForeignKeyConstraint(
            ["provider_id"],
            ["ai_providers.id"],
            name="fk_provider_cost_reconciliations_provider_id",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["fx_rate_version_id"],
            ["fx_rate_versions.id"],
            name="fk_provider_cost_reconciliations_fx_rate_version_id",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name="fk_provider_cost_reconciliations_created_by",
            ondelete="RESTRICT",
        ),
        # 取代者与被取代者必是同一供应商、范围、月份；supersedes_id 为空时不检查（MATCH SIMPLE）。
        ForeignKeyConstraint(
            ["supersedes_id", "provider_id", "scope", "billing_month"],
            [
                f"{_TABLE}.id",
                f"{_TABLE}.provider_id",
                f"{_TABLE}.scope",
                f"{_TABLE}.billing_month",
            ],
            name="fk_provider_cost_reconciliations_supersedes",
            ondelete="RESTRICT",
        ),
        CheckConstraint(FX_CHECK, name="ck_provider_cost_reconciliations_fx"),
        CheckConstraint(REASON_CHECK, name="ck_provider_cost_reconciliations_reason"),
        CheckConstraint(SCOPE_CHECK, name="ck_provider_cost_reconciliations_scope"),
        CheckConstraint(
            COVERAGE_METHOD_CHECK, name="ck_provider_cost_reconciliations_coverage_method"
        ),
        CheckConstraint(PERIOD_CHECK, name="ck_provider_cost_reconciliations_period"),
        *(
            CheckConstraint(f"{column} >= 0", name=f"ck_provider_cost_reconciliations_{suffix}")
            for suffix, column in NON_NEGATIVE_COLUMNS.items()
        ),
    )


__all__ = [
    "BILLING_MONTH_LENGTH",
    "COVERAGE_METHOD_CHECK",
    "COVERAGE_METHOD_LENGTH",
    "FX_CHECK",
    "NON_NEGATIVE_COLUMNS",
    "PERIOD_CHECK",
    "REASON_CHECK",
    "REASON_LENGTH",
    "ROOT_SLOT_EXPRESSION",
    "SCOPE_CHECK",
    "SCOPE_LENGTH",
    "SOURCE_DOCUMENT_REFERENCE_LENGTH",
    "ProviderCostReconciliation",
]

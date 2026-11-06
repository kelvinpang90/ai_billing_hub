"""Monthly statements (design gate #565 v6, AIH-TASK-154).

spec §46、§74.8、§130「monthly statements」；ADR-0003；Invariant 12。`monthly_statements` 一个
预付租户一个账期（马来西亚日历月）一行，`(tenant_id, period_start)` 唯一：每个租户每个账期至多
一份（V1 没有更正版本）。

本任务只有数据库层：账期、账本归属与期初期末属于 AIH-TASK-155，行由 AIH-TASK-156 的
`generate_statement` 写（生成即定稿，V1 不产生 `DRAFT` 行），任务属于 AIH-TASK-157。时间一律
不带时区的 UTC。

⚠️ **定稿后不可变**（INV-12）由 MySQL 触发器保证（迁移 0035）：
`trg_monthly_statements_before_update` 与 `trg_monthly_statements_before_delete` 在
`OLD.status <> 'DRAFT'` 时 SIGNAL 45000。SQLite 上没有这些触发器，服务层本来就没有更新与删除
路径。

⚠️ 索引名、外键名与条件文本同迁移 0035 一致（空白除外），
tests/backend/test_monthly_statements_migration.py 比对两边。
"""

from __future__ import annotations

import datetime as dt
import enum
from typing import Any, Final

from sqlalchemy import (
    CHAR,
    JSON,
    BigInteger,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

# 外键指向 tenants、users 与 tax_policy_versions：import 这几个模块，表才在同一个 metadata 里。
from app.models import auth as _auth_models  # noqa: F401
from app.models import tax_policy as _tax_policy_models  # noqa: F401
from app.models import tenancy as _tenancy_models  # noqa: F401
from app.models.base import Base

# 与 app/models/wallet.py 同一个写法（SQLite 只对 INTEGER PRIMARY KEY 自增）。
_PrimaryKey = BigInteger().with_variant(Integer, "sqlite")
_ForeignKeyInt = BigInteger().with_variant(Integer, "sqlite")

_PUBLIC_ID_LENGTH = 36
# 设计 §2 写死的列宽。
STATEMENT_NUMBER_LENGTH: Final = 32
STATEMENT_STATUS_LENGTH: Final = 16


class StatementStatus(enum.StrEnum):
    """设计 §4 的三个值。V1 生成即定稿（`FINALIZED`）；`DRAFT` 与 `VOID` 只作为状态值存在，
    没有写入方（作废与更正版本由后续设计闸门定）。"""

    DRAFT = "DRAFT"
    FINALIZED = "FINALIZED"
    VOID = "VOID"


# ⚠️ 条件文本与迁移 0035 一致（空白除外）。
STATUS_CHECK: Final = "status IN ('DRAFT', 'FINALIZED', 'VOID')"
PERIOD_CHECK: Final = "period_start < period_end AND period_end < cutoff_at"
HIGH_SEQUENCE_CHECK: Final = "ledger_high_sequence >= 0"
FINALIZED_AT_CHECK: Final = "status <> 'FINALIZED' OR finalized_at IS NOT NULL"


class MonthlyStatement(Base):
    """One tenant's statement for one period (设计 §2「数据库」).

    ⚠️ 不要直接 UPDATE 或 DELETE 这张表：MySQL 上的触发器拒绝改删任何非 `DRAFT` 的行
    （`FINALIZED` 与 `VOID`，SQLSTATE 45000），已定稿的对账单是不可变的财务快照（INV-12）。
    唯一的写入方是 AIH-TASK-156 的生成函数，只 INSERT。
    """

    __tablename__ = "monthly_statements"

    id: Mapped[int] = mapped_column(_PrimaryKey, primary_key=True, autoincrement=True)
    # uuid4；接口里的 `statement_id`。
    public_id: Mapped[str] = mapped_column(CHAR(_PUBLIC_ID_LENGTH), nullable=False)
    tenant_id: Mapped[int] = mapped_column(_ForeignKeyInt, nullable=False)
    # `ST-<YYYYMM>-` 加 8 个 Crockford base32 随机字符。
    statement_number: Mapped[str] = mapped_column(String(STATEMENT_NUMBER_LENGTH), nullable=False)
    # 账期 `[period_start, period_end)` 与 T+1 cut-off 时刻（ADR-0003）。
    period_start: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False)
    period_end: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False)
    cutoff_at: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False)
    status: Mapped[StatementStatus] = mapped_column(
        Enum(StatementStatus, native_enum=False, length=STATEMENT_STATUS_LENGTH),
        nullable=False,
    )
    # 生成时读到的 `wallets.version`：账本归属的高水位，序号不大于它的账本行一经生成就冻结。
    ledger_high_sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # 快照（schema_version = 1，键集合固定，设计 §2「快照」）。
    snapshot_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    # 管理员手动补生成时为其 `users.id`；系统生成为 NULL。
    generated_by: Mapped[int | None] = mapped_column(_ForeignKeyInt, nullable=True)
    # `cutoff_at` 时刻生效的税务政策版本（spec §45.1 `REQ-TAX-001`）。
    tax_policy_version_id: Mapped[int] = mapped_column(_ForeignKeyInt, nullable=False)
    finalized_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False)

    __table_args__ = (
        UniqueConstraint("public_id", name="uq_monthly_statements_public_id"),
        UniqueConstraint("statement_number", name="uq_monthly_statements_statement_number"),
        # 每个租户每个账期至多一份：生成的幂等键，也是 tenant_id 外键用的索引。
        UniqueConstraint("tenant_id", "period_start", name="uq_monthly_statements_tenant_period"),
        # 管理端按账期列、告警查缺失。
        Index("ix_monthly_statements_period", "period_start", "tenant_id"),
        # 显式声明：两个外键用它们，MySQL 不另建隐式索引。
        Index("ix_monthly_statements_generated_by", "generated_by"),
        Index("ix_monthly_statements_tax_policy_version_id", "tax_policy_version_id"),
        # RESTRICT：删租户、用户或税务政策版本不许连带删掉对账单。
        ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_monthly_statements_tenant_id",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["generated_by"],
            ["users.id"],
            name="fk_monthly_statements_generated_by",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["tax_policy_version_id"],
            ["tax_policy_versions.id"],
            name="fk_monthly_statements_tax_policy_version_id",
            ondelete="RESTRICT",
        ),
        CheckConstraint(STATUS_CHECK, name="ck_monthly_statements_status"),
        CheckConstraint(PERIOD_CHECK, name="ck_monthly_statements_period"),
        CheckConstraint(HIGH_SEQUENCE_CHECK, name="ck_monthly_statements_high_sequence"),
        CheckConstraint(FINALIZED_AT_CHECK, name="ck_monthly_statements_finalized_at"),
    )


__all__ = [
    "FINALIZED_AT_CHECK",
    "HIGH_SEQUENCE_CHECK",
    "PERIOD_CHECK",
    "STATEMENT_NUMBER_LENGTH",
    "STATEMENT_STATUS_LENGTH",
    "STATUS_CHECK",
    "MonthlyStatement",
    "StatementStatus",
]

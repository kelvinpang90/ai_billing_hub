"""Tax policy versions and the tax policy lock (design gate #332 v3, AIH-TASK-077).

spec §45、§45.1、§74.9；ADR-0008。两张表（本任务是设计 §11 的 077，只有数据库层）：

- `tax_policy_locks`：只有一行（`id = 1`）、没有业务字段。服务层发布对它
  `SELECT … FOR UPDATE`，版本表的区间不重叠触发器同样先锁它；
- `tax_policy_versions`：一个（辖区, 政策类型）的一版税务政策。状态
  `DRAFT → PUBLISHED → RETIRED`（spec §74.9 的三个值，不加 `DISCARDED`）。已发布
  （`PUBLISHED` / `RETIRED`）的版本在 `[effective_from, effective_to)` 内生效；同一
  （辖区, 政策类型）的非空区间互不相交，至多一个未截断的已发布版本（生成列 `open_slot`
  + 唯一索引）。`treatment_json` 的形状在应用层校验（设计 §2）。

⚠️ **发布后不可变**由 MySQL 触发器保证（迁移 0022）：锁表恰好一行、不许改、不许删；
版本只能以草稿插入、草稿只能改成草稿 / 已发布、发布跃迁上尽头为空且税务标志与税率一致；
已发布的行只许改 `effective_to`、`PUBLISHED → RETIRED` 与 `updated_at`；已退役的行不可改；
锁 `tax_policy_locks` 后检查区间不重叠（空区间跳过）；版本一律不许删除。SQLite 上没有
这些触发器，服务层按同样的规则写。

⚠️ 条件文本与生成列表达式同迁移 0022 逐字一致（空白除外），test_migrations.py 比对两边。
"""

from __future__ import annotations

import datetime as dt
import enum
from decimal import Decimal
from typing import Any, Final

from sqlalchemy import (
    CHAR,
    DDL,
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
    event,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

# 与 app/models/fx_rates.py 同一个写法（SQLite 只对 INTEGER PRIMARY KEY 自增）。
_PrimaryKey = BigInteger().with_variant(Integer, "sqlite")
_ForeignKeyInt = BigInteger().with_variant(Integer, "sqlite")

_PUBLIC_ID_LENGTH = 36

# 设计 §2 写死的列宽。
JURISDICTION_LENGTH: Final = 16
POLICY_TYPE_LENGTH: Final = 16
STATUS_LENGTH: Final = 16
SOURCE_REFERENCE_LENGTH: Final = 255

# `rate` 的精度：DECIMAL(9,6)。整数部分留 3 位，让「把 8% 录成 8」落在有名字的
# CHECK 上，而不是列宽上（设计 §2）。
RATE_PRECISION: Final = 9
RATE_SCALE: Final = 6

# 锁表唯一一行的 id。
TAX_POLICY_LOCK_ID: Final = 1


class TaxJurisdiction(enum.StrEnum):
    """只有 `MY`：没有写入方的取值不预先加（app/models/auth.py 的同一条规矩）。"""

    MY = "MY"


class TaxPolicyType(enum.StrEnum):
    """只有 `SST`，理由同上。"""

    SST = "SST"


class TaxPolicyStatus(enum.StrEnum):
    """spec §74.9 的三个值。不加 `DISCARDED`：草稿与发布在同一事务里，没有写入方（设计 §9）。"""

    DRAFT = "DRAFT"
    PUBLISHED = "PUBLISHED"
    RETIRED = "RETIRED"


# 已发布过的两种状态：有区间、参与解析。
PERIOD_STATUSES: Final = (TaxPolicyStatus.PUBLISHED, TaxPolicyStatus.RETIRED)

# ⚠️ 条件文本与迁移 0022 逐字一致（空白除外），test_migrations.py 比对两边。
JURISDICTION_CHECK: Final = "jurisdiction IN ('MY')"
POLICY_TYPE_CHECK: Final = "policy_type IN ('SST')"
RATE_CHECK: Final = "rate IS NULL OR (rate >= 0 AND rate < 1)"
STATUS_CHECK: Final = "status IN ('DRAFT', 'PUBLISHED', 'RETIRED')"
# 草稿没有区间、没有发布人。
UNPUBLISHED_CHECK: Final = (
    "status <> 'DRAFT' OR (effective_from IS NULL AND effective_to IS NULL"
    " AND approved_by IS NULL AND approved_at IS NULL)"
)
# 发布过的一定有发布人与发布时刻。
APPROVED_CHECK: Final = (
    "status NOT IN ('PUBLISHED', 'RETIRED')"
    " OR (approved_by IS NOT NULL AND approved_at IS NOT NULL)"
)
# 与汇率不同：税务政策没有「一直以来」，发布过的一定有具体起点（设计 §9）。
PUBLISHED_START_CHECK: Final = (
    "status NOT IN ('PUBLISHED', 'RETIRED') OR effective_from IS NOT NULL"
)
# 区间不倒置；空区间只能出现在 RETIRED 行上（与 app/models/fx_rates.py 逐字相同）。
PERIOD_CHECK: Final = (
    "effective_from IS NULL OR effective_to IS NULL OR effective_from < effective_to"
    " OR (status = 'RETIRED' AND effective_from = effective_to)"
)
# 停用必有尽头。
RETIRED_END_CHECK: Final = "status <> 'RETIRED' OR effective_to IS NOT NULL"

# 只有未截断的已发布版本为 1，其余一律为 NULL（不是 0）；唯一索引允许多个 NULL，
# 所以「同一（辖区, 政策类型）至多一个未截断的已发布版本」由数据库保证。
OPEN_SLOT_EXPRESSION: Final = "CASE WHEN status = 'PUBLISHED' AND effective_to IS NULL THEN 1 END"


def _rate() -> Numeric:
    # DECIMAL(9,6)，读出来是 Decimal（INV-10）：税率不经过 float。
    return Numeric(RATE_PRECISION, RATE_SCALE, asdecimal=True)


class TaxPolicyLock(Base):
    """The one-row lock that serialises publishing tax policies (设计 §2).

    服务层发布：`FOR UPDATE`，是主事务的第一条语句。区间不重叠触发器同样先对它
    `FOR UPDATE`，绕过服务的直接写入也被串行起来。
    """

    __tablename__ = "tax_policy_locks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)


# `Base.metadata.create_all`（单元测试的 SQLite）建表时同样写入那一行；生产上由迁移 0022 写入，
# 迁移不经过这里。
event.listen(
    TaxPolicyLock.__table__,
    "after_create",
    DDL(f"INSERT INTO tax_policy_locks (id) VALUES ({TAX_POLICY_LOCK_ID})"),
)


class TaxPolicyVersion(Base):
    """One version of the tax policy for one jurisdiction and policy type.

    `effective_from` / `effective_to` 是不带时区的 UTC，草稿两者为空；发布过的必有起点，
    `effective_to IS NULL` = 仍生效。发布后只有 `effective_to`（截断）、`status`
    （`PUBLISHED → RETIRED`）与 `updated_at` 会变；退役后什么都不变。
    """

    __tablename__ = "tax_policy_versions"

    id: Mapped[int] = mapped_column(_PrimaryKey, primary_key=True, autoincrement=True)
    public_id: Mapped[str] = mapped_column(CHAR(_PUBLIC_ID_LENGTH), nullable=False)
    jurisdiction: Mapped[TaxJurisdiction] = mapped_column(
        Enum(TaxJurisdiction, native_enum=False, length=JURISDICTION_LENGTH), nullable=False
    )
    policy_type: Mapped[TaxPolicyType] = mapped_column(
        Enum(TaxPolicyType, native_enum=False, length=POLICY_TYPE_LENGTH), nullable=False
    )
    # 小数表示（0.08 = 8%）；未注册、不适用时为 NULL。
    rate: Mapped[Decimal | None] = mapped_column(_rate(), nullable=True)
    # 形状在应用层校验（设计 §2「`treatment_json` 的形状」）。
    treatment_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    status: Mapped[TaxPolicyStatus] = mapped_column(
        Enum(TaxPolicyStatus, native_enum=False, length=STATUS_LENGTH), nullable=False
    )
    effective_from: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    effective_to: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    # 生成列（STORED）：只读，ORM 从不写它。
    open_slot: Mapped[int | None] = mapped_column(
        Integer, Computed(OPEN_SLOT_EXPRESSION, persisted=True)
    )
    # 政策出处，纯文本，不写链接。
    source_reference: Mapped[str] = mapped_column(String(SOURCE_REFERENCE_LENGTH), nullable=False)
    # 政策只能由人建，没有系统写入方：非空（与汇率不同）。
    created_by: Mapped[int] = mapped_column(_ForeignKeyInt, nullable=False)
    # 发布人与发布时刻；发布时写。
    approved_by: Mapped[int | None] = mapped_column(_ForeignKeyInt, nullable=True)
    approved_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False)

    __table_args__ = (
        ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name="fk_tax_policy_versions_created_by",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["approved_by"],
            ["users.id"],
            name="fk_tax_policy_versions_approved_by",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("public_id", name="uq_tax_policy_versions_public_id"),
        # 同一（辖区, 政策类型）至多一个未截断的已发布版本；前缀也服务按时刻解析的查询。
        Index(
            "ux_tax_policy_versions_open_slot",
            "jurisdiction",
            "policy_type",
            "open_slot",
            unique=True,
        ),
        CheckConstraint(JURISDICTION_CHECK, name="ck_tax_policy_versions_jurisdiction"),
        CheckConstraint(POLICY_TYPE_CHECK, name="ck_tax_policy_versions_policy_type"),
        CheckConstraint(RATE_CHECK, name="ck_tax_policy_versions_rate"),
        CheckConstraint(STATUS_CHECK, name="ck_tax_policy_versions_status"),
        CheckConstraint(UNPUBLISHED_CHECK, name="ck_tax_policy_versions_unpublished"),
        CheckConstraint(APPROVED_CHECK, name="ck_tax_policy_versions_approved"),
        CheckConstraint(PUBLISHED_START_CHECK, name="ck_tax_policy_versions_published_start"),
        CheckConstraint(PERIOD_CHECK, name="ck_tax_policy_versions_period"),
        CheckConstraint(RETIRED_END_CHECK, name="ck_tax_policy_versions_retired_end"),
    )


__all__ = [
    "APPROVED_CHECK",
    "JURISDICTION_CHECK",
    "OPEN_SLOT_EXPRESSION",
    "PERIOD_CHECK",
    "PERIOD_STATUSES",
    "POLICY_TYPE_CHECK",
    "PUBLISHED_START_CHECK",
    "RATE_CHECK",
    "RATE_PRECISION",
    "RATE_SCALE",
    "RETIRED_END_CHECK",
    "STATUS_CHECK",
    "TAX_POLICY_LOCK_ID",
    "UNPUBLISHED_CHECK",
    "TaxJurisdiction",
    "TaxPolicyLock",
    "TaxPolicyStatus",
    "TaxPolicyType",
    "TaxPolicyVersion",
]

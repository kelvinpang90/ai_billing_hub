"""Tenant and project identity tables (spec §75, §76; docs/database-schema.md).

AIH-TASK-004 只建了身份与归属字段。AIH-TASK-005（设计闸门 #88）在 `tenants` 上
加了由余额驱动的 `billing_status`、`status_version` 与 `low_balance_threshold`；
币种由 `wallets.currency` 承载，不在租户上重复。

⚠️ 其余 §75 / §76 列（`account_status`、`integration_status`、webhook 的 URL 与
密钥列、`backend_base_url`）刻意不在这里 —— 它们各归一个后续任务，届时都是纯新增
列。清单与理由见 `docs/database-schema.md`「尚未建的列」。

对外只用 `public_id`（uuid4 字符串），内部自增 `id` 不出库（spec §115：猜不到
别人的 tenant ID）。时间一律不带时区的 UTC（spec §109），由调用方传入。
"""

from __future__ import annotations

import datetime as dt
import enum
from decimal import Decimal

from sqlalchemy import (
    CHAR,
    BigInteger,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import MONEY_PRECISION, MONEY_SCALE, Base

# 与 app/models/auth.py 同一个写法：生产 MySQL 要 BIGINT，而内存 SQLite 只对
# `INTEGER PRIMARY KEY` 自增。
_PrimaryKey = BigInteger().with_variant(Integer, "sqlite")
_ForeignKeyInt = BigInteger().with_variant(Integer, "sqlite")

# uuid4 的规范字符串形式恰好 36 个字符，定长。
_PUBLIC_ID_LENGTH = 36

# 枚举列宽写死，理由见 app/models/auth.py 的 `_ENUM_LENGTH`。
_ENUM_LENGTH = 64


class BillingStatus(enum.StrEnum):
    """由余额驱动的计费状态（spec §7 第 8–10 条、§24；设计闸门 #88）。

    余额 > 0 为 `ACTIVE`，≤ 0 为 `SUSPENDED` —— **正好为 0 也是暂停**。
    只由 `app/repositories/wallet.py` 的 `post_transaction` 改变。管理员控制的
    `account_status` 是另一维，归状态模型任务。
    """

    ACTIVE = "ACTIVE"
    SUSPENDED = "SUSPENDED"


class Tenant(Base):
    """spec §75: identity columns plus the balance-driven billing status."""

    __tablename__ = "tenants"

    id: Mapped[int] = mapped_column(_PrimaryKey, primary_key=True, autoincrement=True)
    public_id: Mapped[str] = mapped_column(CHAR(_PUBLIC_ID_LENGTH), unique=True, nullable=False)
    company_name: Mapped[str] = mapped_column(String(255), nullable=False)
    # ⚠️ contact_name / email / phone 是个人数据（REQ-PRIV-001）：不进日志、不进
    # 异常消息。email 是联系邮箱不是登录账号，**刻意不唯一**：同一联系人可以
    # 对应多家公司。
    contact_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    phone: Mapped[str | None] = mapped_column(String(32), nullable=True)

    # ⚠️ 这两列只经 `post_transaction` 改变，与账本行同一事务（INV-13）。
    # 新租户余额为 0，按 spec §7 第 10 条就是 SUSPENDED；充值使余额 > 0 后
    # 自动变成 ACTIVE。
    billing_status: Mapped[BillingStatus] = mapped_column(
        Enum(BillingStatus, native_enum=False, length=_ENUM_LENGTH),
        nullable=False,
        default=BillingStatus.SUSPENDED,
    )
    # 只增不减（spec §24）：每次状态跃迁 +1。
    status_version: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    # 金额列，与 `Money` 同精度。NULL = 不发低余额事件。
    low_balance_threshold: Mapped[Decimal | None] = mapped_column(
        Numeric(MONEY_PRECISION, MONEY_SCALE, asdecimal=True),
        nullable=True,
    )

    created_at: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False)

    # ⚠️ 条件文本与迁移 0006 逐字一致（空白除外），test_migrations.py 比对两边。
    __table_args__ = (
        CheckConstraint(
            "billing_status IN ('ACTIVE', 'SUSPENDED')",
            name="ck_tenants_billing_status",
        ),
        CheckConstraint("status_version >= 0", name="ck_tenants_status_version"),
        CheckConstraint(
            "low_balance_threshold IS NULL OR low_balance_threshold >= 0",
            name="ck_tenants_low_balance_threshold",
        ),
    )


class Project(Base):
    """spec §76, identity and ownership columns only; `description` per §57."""

    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(_PrimaryKey, primary_key=True, autoincrement=True)
    public_id: Mapped[str] = mapped_column(CHAR(_PUBLIC_ID_LENGTH), unique=True, nullable=False)
    # ⚠️ RESTRICT 而不是 CASCADE：项目将来挂着钱包扣费、用量与账本，删租户时
    # 级联删掉它们等于删财务记录。要删租户，先显式处理它的项目。
    tenant_id: Mapped[int] = mapped_column(
        _ForeignKeyInt, ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(String(1000), nullable=True)

    created_at: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False)

    # 按租户列项目的查询走这条索引。
    __table_args__ = (Index("ix_projects_tenant_id", "tenant_id"),)

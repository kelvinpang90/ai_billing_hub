"""Tenant and project identity tables (spec §75, §76; docs/database-schema.md).

⚠️ **只建身份与归属字段。**§75 / §76 里的状态列、`status_version`、
`low_balance_threshold`、`currency`、webhook 的 URL 与密钥列、`backend_base_url`
刻意不在这里 —— 它们各归一个后续任务，届时都是纯新增列。清单与理由见
`docs/database-schema.md`「尚未建的列」。

对外只用 `public_id`（uuid4 字符串），内部自增 `id` 不出库（spec §115：猜不到
别人的 tenant ID）。时间一律不带时区的 UTC（spec §109），由调用方传入。
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import CHAR, BigInteger, DateTime, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

# 与 app/models/auth.py 同一个写法：生产 MySQL 要 BIGINT，而内存 SQLite 只对
# `INTEGER PRIMARY KEY` 自增。
_PrimaryKey = BigInteger().with_variant(Integer, "sqlite")
_ForeignKeyInt = BigInteger().with_variant(Integer, "sqlite")

# uuid4 的规范字符串形式恰好 36 个字符，定长。
_PUBLIC_ID_LENGTH = 36


class Tenant(Base):
    """spec §75, identity columns only."""

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

    created_at: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False)


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

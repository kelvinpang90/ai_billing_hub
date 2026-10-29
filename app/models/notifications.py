"""Portal notifications (design gate #471 v7, AIH-TASK-125).

spec §25、§27 第 4 步、§47–§50、§74（`notifications`）。一行 = 一条逻辑通知：由一条
`domain_outbox` 行（`tenant.low_balance` / `tenant.billing_status_changed`）派生，
`source_outbox_id` 唯一，同一条 outbox 行不论投递几次最多一条通知（设计 §4）。

本任务只有数据库层：行由 AIH-TASK-126 的 outbox 投递写，客户接口属于 AIH-TASK-127。
金额只存与回显、不做运算（设计 §2「金额」）；时间一律不带时区的 UTC。

⚠️ 索引名与条件文本同迁移 0028 一致（空白除外），
tests/backend/test_notifications_migration.py 比对两边。
"""

from __future__ import annotations

import datetime as dt
import enum
from typing import Final

from sqlalchemy import (
    CHAR,
    BigInteger,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

# 外键指向 tenants：import 这个模块，表才在同一个 metadata 里。
from app.models import tenancy as _tenancy_models  # noqa: F401
from app.models.base import Base, Money

# 与 app/models/wallet.py 同一个写法（SQLite 只对 INTEGER PRIMARY KEY 自增）。
_PrimaryKey = BigInteger().with_variant(Integer, "sqlite")
_ForeignKeyInt = BigInteger().with_variant(Integer, "sqlite")

_PUBLIC_ID_LENGTH = 36
# 设计 §2 写死的列宽。
NOTIFICATION_TYPE_LENGTH: Final = 32


class NotificationType(enum.StrEnum):
    """设计 §2 的三种；文案在前端，后端不存。"""

    LOW_BALANCE = "LOW_BALANCE"
    SUSPENDED = "SUSPENDED"
    REACTIVATED = "REACTIVATED"


# ⚠️ 条件文本与迁移 0028 一致（空白除外）。
TYPE_CHECK: Final = "notification_type IN ('LOW_BALANCE', 'SUSPENDED', 'REACTIVATED')"
# (notification_type = 'LOW_BALANCE') = (threshold IS NOT NULL)，写成两条
# 分支：布尔等式的写法两库不同义。
THRESHOLD_CHECK: Final = (
    "(notification_type = 'LOW_BALANCE' AND threshold IS NOT NULL)"
    " OR (notification_type <> 'LOW_BALANCE' AND threshold IS NULL)"
)


class Notification(Base):
    """One portal notification for one tenant (设计 §2「数据库」).

    写入后只有 `read_at` 会变（首次标已读时写，之后不变；没有「已读 → 未读」）。
    """

    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(_PrimaryKey, primary_key=True, autoincrement=True)
    # uuid4；客户侧唯一可见的标识。
    public_id: Mapped[str] = mapped_column(CHAR(_PUBLIC_ID_LENGTH), nullable=False)
    # RESTRICT：删租户不许连带删掉通知。
    tenant_id: Mapped[int] = mapped_column(
        _ForeignKeyInt,
        ForeignKey("tenants.id", name="fk_notifications_tenant", ondelete="RESTRICT"),
        nullable=False,
    )
    notification_type: Mapped[NotificationType] = mapped_column(
        Enum(NotificationType, native_enum=False, length=NOTIFICATION_TYPE_LENGTH),
        nullable=False,
    )
    # 产生它的 `domain_outbox.id`，幂等键。⚠️ 刻意不建外键（设计 §9 方案
    # 取舍 B）：日后清理 outbox 不被通知表挡住。
    source_outbox_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # 事件 payload 的 `balance`：触发事件那笔流水之后的余额，可以为负。
    balance: Mapped[Money] = mapped_column(nullable=False)
    # 只有 LOW_BALANCE 非空：事件 payload 的 `threshold`。
    threshold: Mapped[Money | None] = mapped_column(nullable=True)
    # 事件发生时刻 = outbox 行的 `created_at`（= 账本行的 `created_at`），不取处理时刻。
    occurred_at: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False)
    # 通知行写入时刻，取整到秒。
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False)
    # 首次标已读的时刻；NULL = 未读。
    read_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)

    __table_args__ = (
        UniqueConstraint("public_id", name="uq_notifications_public_id"),
        # 一条 outbox 行最多一条通知（设计 §4 的数据库兜底）。
        UniqueConstraint("source_outbox_id", name="uq_notifications_source_outbox"),
        # 列表按租户、`source_outbox_id`（记账顺序）倒序翻页；未读数走它的
        # 前缀。名字沿用设计 v7，不含 `occurred_at`。
        Index("ix_notifications_tenant_occurred", "tenant_id", "source_outbox_id"),
        CheckConstraint(TYPE_CHECK, name="ck_notifications_type"),
        CheckConstraint(THRESHOLD_CHECK, name="ck_notifications_threshold"),
    )


__all__ = [
    "NOTIFICATION_TYPE_LENGTH",
    "THRESHOLD_CHECK",
    "TYPE_CHECK",
    "Notification",
    "NotificationType",
]

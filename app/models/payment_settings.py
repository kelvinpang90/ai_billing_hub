"""Payment settings: the one-row table of payment rules (design gate #587 v3, AIH-TASK-188).

spec §42、§71；ADR-0007、ADR-0010；Invariant 6、10。`payment_settings` **恰好一行**（`id = 1`）：
最低充值额、最高充值额（可空）、最小恢复额的余额缓冲、推荐金额、同额复用窗口与待付超时，管理端可改
（乐观锁 `version`）。

本任务只有数据库层：生产上那一行由迁移 0038 写入种子值；读写它的服务（`get_payment_settings`、
`update_payment_settings`、最小恢复额）属于 AIH-TASK-189，接口属于 190、191。
`update_payment_settings` 是唯一的写入方：条件更新 `WHERE id = 1 AND version = …`，与
`PAYMENT_SETTINGS_UPDATE` 审计同一事务（INV-6、INV-13）；没有插入与删除路径。
`Base.metadata.create_all`（单元测试的 SQLite）**不**写种子行，需要它的测试自己写入。
时间一律不带时区的 UTC。

⚠️ 金额两位小数、`Decimal`（INV-10）；`preset_amounts` 在 JSON 里存定点字符串（例如 `"50.00"`），
不存 JSON 数字（浮点解析）。数组的形状（1–8 个、互不相同、严格升序、在最低额与最高额之间）由
应用校验，数据库只管下面的范围与精度 CHECK。

⚠️ 索引名、外键名与条件文本同迁移 0038 一致（空白除外），
tests/backend/test_payment_settings_migration.py 比对两边。
"""

from __future__ import annotations

import datetime as dt
from typing import Final

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
)
from sqlalchemy.orm import Mapped, mapped_column

# 外键指向 users：import 它，表才在同一个 metadata 里。
from app.models import auth as _auth_models  # noqa: F401
from app.models.base import Base, Money

# 与 app/models/payments.py 同一个写法。
_ForeignKeyInt = BigInteger().with_variant(Integer, "sqlite")

# 唯一那一行的主键。
PAYMENT_SETTINGS_ID: Final = 1

# ⚠️ 条件文本与迁移 0038 一致（空白除外）。
SINGLETON_CHECK: Final = "id = 1"
MIN_AMOUNT_CHECK: Final = (
    "min_amount >= 2 AND min_amount <= 999999.99 AND min_amount = ROUND(min_amount, 2)"
)
MAX_AMOUNT_CHECK: Final = (
    "max_amount IS NULL OR (max_amount >= min_amount AND max_amount <= 999999.99"
    " AND max_amount = ROUND(max_amount, 2))"
)
# 缓冲至少 0.01（不能为 0）：按最小恢复额充值后余额严格大于 0（设计 §2「最小恢复额」）。
RECOVERY_BUFFER_CHECK: Final = (
    "recovery_buffer >= 0.01 AND recovery_buffer <= 999999.99"
    " AND recovery_buffer = ROUND(recovery_buffer, 2)"
)
REUSE_WINDOW_SECONDS_CHECK: Final = "reuse_window_seconds >= 0 AND reuse_window_seconds <= 300"
PENDING_TIMEOUT_SECONDS_CHECK: Final = (
    "pending_timeout_seconds >= 2400 AND pending_timeout_seconds <= 86400"
)
VERSION_CHECK: Final = "version >= 1"


class PaymentSettings(Base):
    """The one row of payment rules (设计 §2「数据库」).

    ⚠️ 不要绕过 `update_payment_settings`（AIH-TASK-189）改这一行：每次修改都要 `version + 1` 并在
    同一事务写 `PAYMENT_SETTINGS_UPDATE` 审计（含前后值与版本），否则看不出某笔建单当时用的规则
    （INV-6），两个管理员同时改也会静默覆盖。
    """

    __tablename__ = "payment_settings"

    # 恒为 1（`ck_payment_settings_singleton`），不自增。
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    # 最低充值额，2.00–999999.99，两位小数。
    min_amount: Mapped[Money] = mapped_column(nullable=False)
    # 最高充值额；为空时单笔上限是 999999.99（A 的金额正则）。
    max_amount: Mapped[Money | None] = mapped_column(nullable=True)
    # 最小恢复额的余额缓冲（ADR-0010），0.01–999999.99，两位小数。
    recovery_buffer: Mapped[Money] = mapped_column(nullable=False)
    # 推荐金额：定点字符串的数组，例如 `["50.00", "100.00"]`。
    preset_amounts: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    # 60 秒同额复用的窗口（ADR-0007），0–300 秒。
    reuse_window_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    # 待付超时：`expires_at = now + pending_timeout_seconds`，2400–86400 秒。
    pending_timeout_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    # 乐观锁；种子行为 1，每次修改 +1。
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False)
    # 最后修改的管理员；种子行为空。
    updated_by_user_id: Mapped[int | None] = mapped_column(_ForeignKeyInt, nullable=True)

    __table_args__ = (
        # 显式声明：外键用它，MySQL 不另建隐式索引。
        Index("ix_payment_settings_updated_by_user_id", "updated_by_user_id"),
        # RESTRICT：改过设置的管理员账号不许连带删掉这一行。
        ForeignKeyConstraint(
            ["updated_by_user_id"],
            ["users.id"],
            name="fk_payment_settings_updated_by_user_id",
            ondelete="RESTRICT",
        ),
        CheckConstraint(SINGLETON_CHECK, name="ck_payment_settings_singleton"),
        CheckConstraint(MIN_AMOUNT_CHECK, name="ck_payment_settings_min_amount"),
        CheckConstraint(MAX_AMOUNT_CHECK, name="ck_payment_settings_max_amount"),
        CheckConstraint(RECOVERY_BUFFER_CHECK, name="ck_payment_settings_recovery_buffer"),
        CheckConstraint(
            REUSE_WINDOW_SECONDS_CHECK, name="ck_payment_settings_reuse_window_seconds"
        ),
        CheckConstraint(
            PENDING_TIMEOUT_SECONDS_CHECK, name="ck_payment_settings_pending_timeout_seconds"
        ),
        CheckConstraint(VERSION_CHECK, name="ck_payment_settings_version"),
    )


__all__ = [
    "MAX_AMOUNT_CHECK",
    "MIN_AMOUNT_CHECK",
    "PAYMENT_SETTINGS_ID",
    "PENDING_TIMEOUT_SECONDS_CHECK",
    "RECOVERY_BUFFER_CHECK",
    "REUSE_WINDOW_SECONDS_CHECK",
    "SINGLETON_CHECK",
    "VERSION_CHECK",
    "PaymentSettings",
]

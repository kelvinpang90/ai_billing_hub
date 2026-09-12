"""Authentication tables (spec §53, §54, §66, §74).

表名照 spec §74 的清单：`users` / `refresh_tokens` / `audit_logs`。
`two_factor_settings` 与 `recovery_codes` 归 T0.8b，`password_reset_tokens`
与 `domain_outbox` 归 T0.8d（设计闸门 Issue #32 v5）。

⚠️ **时间一律 UTC**（spec §109）。列上不带时区：MySQL 的 `DATETIME` 本来就
不存时区，而 compose 里的 MySQL 已经钉死 `--default-time-zone=+00:00`。
展示层再转 Asia/Kuala_Lumpur。
"""

from __future__ import annotations

import datetime as dt
import enum

from sqlalchemy import BigInteger, DateTime, Enum, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base

# ⚠️ SQLite 只对 `INTEGER PRIMARY KEY` 做自增 —— `BIGINT` 主键在 SQLite 上拿不到
# 自增值，插入时直接撞 NOT NULL。生产是 MySQL，要的是 BIGINT（用户与审计行的
# 数量会超过 32 位）；而单元测试跑在内存 SQLite 上。`with_variant` 让两边各取
# 所需，且**不需要在测试里写特例**。
_PrimaryKey = BigInteger().with_variant(Integer, "sqlite")
_ForeignKeyInt = BigInteger().with_variant(Integer, "sqlite")


class UserRole(enum.StrEnum):
    """spec §51：一套认证同时服务 ADMIN 与 CUSTOMER，角色只决定可见的路由。

    ⚠️ **授权必须由后端独立强制执行**，前端藏菜单不构成任何访问控制。
    CUSTOMER 现在建不出来（Customer Portal 是 Phase 4），但列先留着，
    免得 Phase 4 要改一次已有数据的表。
    """

    ADMIN = "ADMIN"
    CUSTOMER = "CUSTOMER"


class UserStatus(enum.StrEnum):
    ACTIVE = "ACTIVE"
    DISABLED = "DISABLED"


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(_PrimaryKey, primary_key=True, autoincrement=True)
    # 320 = 邮箱地址的规范上限（本地部分 64 + @ + 域名 255）。
    email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False)
    # Argon2id 的完整编码串，**参数嵌在里面**，所以以后能逐个升级开销参数。
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[UserRole] = mapped_column(Enum(UserRole, native_enum=False), nullable=False)
    status: Mapped[UserStatus] = mapped_column(
        Enum(UserStatus, native_enum=False), nullable=False, default=UserStatus.ACTIVE
    )

    # ⚠️ 这个计数器同时统计密码失败与（T0.8b 之后的）TOTP 失败。分开计数只会
    # 多一个能被分别耗尽的额度。清零的触发点是**令牌真的发出去了**，不是
    # 「某一个因子过了」—— 否则知道密码的人可以无限次猜验证码。
    failed_login_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    locked_until: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    # ⚠️ 递增锁定的档位。**它刻意不随锁到期而清零** —— `failed_login_count` 会清
    # （否则锁一解开下一次失败立刻又达阈值），但档位必须留着，否则每一轮锁定
    # 都是同样的 15 分钟，攻击者每 15 分钟白拿一轮 5 次猜测窗口，**永远不会被
    # 真正挡住**。只有完整认证成功才清零。
    lockout_level: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_login_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)

    created_at: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False)

    refresh_tokens: Mapped[list[RefreshToken]] = relationship(back_populates="user")


class RefreshToken(Base):
    """One row per issued refresh token. Rotation inserts a new row in the same family."""

    __tablename__ = "refresh_tokens"

    id: Mapped[int] = mapped_column(_PrimaryKey, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        _ForeignKeyInt, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    # 一次登录 = 一个家族。轮换沿用同一个 family_id，所以检测到重放时可以
    # **一次吊销整条链**，而不是只吊销被重放的那一个。
    family_id: Mapped[str] = mapped_column(String(36), nullable=False)
    # SHA-256 十六进制。UNIQUE 既是查找索引，也是并发下的兜底。
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)

    issued_at: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False)
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False)
    # 非空 = 已经被换过一次。再被提交就是重放。
    used_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    revoked_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)

    user: Mapped[User] = relationship(back_populates="refresh_tokens")

    __table_args__ = (Index("ix_refresh_tokens_family", "user_id", "family_id"),)


class AuditAction(enum.StrEnum):
    """spec §66 的动作清单，本任务只落地登录相关的几项。

    其余（`CUSTOMER_CREATE`、`WALLET_ADJUSTMENT` 等）在实现它们的那个任务里加 ——
    **不预先把整张清单塞进来**：没有写入方的枚举值会让人以为那件事已经在记了。
    """

    LOGIN = "LOGIN"
    LOGIN_FAILED = "LOGIN_FAILED"
    LOGOUT = "LOGOUT"
    TOKEN_REUSED = "TOKEN_REUSED"
    USER_CREATED = "USER_CREATED"


class AuditLog(Base):
    """spec §66. Append-only.

    ⚠️ 「Audit records must not be editable through normal application APIs」——
    所以**这个模型没有任何更新路径**，服务层只暴露一个 record() 插入函数。
    字段照 §66 列的那一份。
    """

    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(_PrimaryKey, primary_key=True, autoincrement=True)
    # actor 可以为空：登录失败时我们**不一定知道是谁**（邮箱可能根本不存在），
    # 而那一条恰恰是最该记下来的。
    actor_user_id: Mapped[int | None] = mapped_column(_ForeignKeyInt, nullable=True)
    actor_role: Mapped[str | None] = mapped_column(String(32), nullable=True)
    action: Mapped[AuditAction] = mapped_column(
        Enum(AuditAction, native_enum=False), nullable=False
    )
    entity_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    entity_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # ⚠️ before/after 是自由 JSON，**最容易被人塞进整个请求体**。写入走服务层的
    # 字段白名单，密码、令牌、TOTP 密钥一律不进（Invariant 9 的同一条道理）。
    before_state: Mapped[str | None] = mapped_column(Text, nullable=True)
    after_state: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 45 = IPv6 加映射前缀的最长形式。
    ip_address: Mapped[str | None] = mapped_column(String(45), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(512), nullable=True)
    reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False)

    __table_args__ = (Index("ix_audit_logs_actor_created", "actor_user_id", "created_at"),)

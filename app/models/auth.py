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

# ⚠️ 枚举列的宽度**必须写死**。
#
# `Enum(native_enum=False)` 生成的 VARCHAR 宽度默认按**建表那一刻最长的成员**
# 算。往枚举里加一个更长的值不会有任何提示，直到真 MySQL 在插入时报
# `Data too long for column`。而**单元测试看不见它** —— SQLite 根本不强制
# VARCHAR 长度，所以整套用例照样全绿。T0.8b 上真踩过一次：加了
# `RECOVERY_CODES_REGENERATED`（26 字符），列还是按 `LOGIN_FAILED`（12）建的。
#
# 写死一个宽裕的宽度之后，加枚举值不再需要配一次 ALTER。
_ENUM_LENGTH = 64


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
    role: Mapped[UserRole] = mapped_column(
        Enum(UserRole, native_enum=False, length=_ENUM_LENGTH), nullable=False
    )
    status: Mapped[UserStatus] = mapped_column(
        Enum(UserStatus, native_enum=False, length=_ENUM_LENGTH),
        nullable=False,
        default=UserStatus.ACTIVE,
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
    TWO_FACTOR_ENABLED = "TWO_FACTOR_ENABLED"
    RECOVERY_CODES_REGENERATED = "RECOVERY_CODES_REGENERATED"
    PASSWORD_RESET_REQUESTED = "PASSWORD_RESET_REQUESTED"
    PASSWORD_RESET = "PASSWORD_RESET"


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
        Enum(AuditAction, native_enum=False, length=_ENUM_LENGTH), nullable=False
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


class TwoFactorSetting(Base):
    """One row per user's TOTP enrolment (spec §54; design gate Issue #32 v5).

    ⚠️ `confirmed_at IS NULL` = `PENDING`，**一律按「未启用 2FA」处理**。
    否则「生成了密钥但没扫码确认」会把管理员锁在门外。
    """

    __tablename__ = "two_factor_settings"

    user_id: Mapped[int] = mapped_column(
        _ForeignKeyInt, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    # 信封加密后的自描述字符串，格式见 app/core/crypto.py。
    encrypted_totp_secret: Mapped[str] = mapped_column(Text, nullable=False)
    # 单独存一列是为了轮换主密钥时能查出还有哪些行用着老版本 —— 只存在
    # 密文字符串里就得全表扫。
    key_version: Mapped[int] = mapped_column(Integer, nullable=False)

    # ⚠️ 每次 `enrol` +1。`confirm` 必须带上它校验时读到的版本号：期间若有人
    # 重新 `enrol` 换了密钥，版本对不上、确认落空 —— 否则可能把一个**从未被
    # 验证过的密钥**标成已确认。
    secret_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    confirmed_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)

    # ⚠️ 防同一个验证码在有效期内被用第二次。判定靠**条件更新**的受影响行数，
    # 不靠应用层先读后写 —— 见 app/services/two_factor.py。
    last_used_counter: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    created_at: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False)


class RecoveryCode(Base):
    """Single-use backup codes (spec §54: provide recovery codes, stored hashed)."""

    __tablename__ = "recovery_codes"

    id: Mapped[int] = mapped_column(_PrimaryKey, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        _ForeignKeyInt, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    # Argon2id。⚠️ 恢复码等价于第二因子本身，**必须哈希**，不能像 TOTP 密钥
    # 那样可还原 —— 校验它只需要比对，不需要读出原文。
    code_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    used_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    # 重新生成时把旧码全部置上，保证任何时刻至多 10 个可用。
    revoked_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False)

    __table_args__ = (Index("ix_recovery_codes_user", "user_id", "used_at", "revoked_at"),)


class PasswordResetToken(Base):
    """One row per reset request (spec §53; design gate Issue #32 v5).

    ⚠️ **只存哈希。**库里没有任何能直接用来重置别人密码的东西 —— 与
    `refresh_tokens` 同一条道理。用 SHA-256 而不是 Argon2id：这是一个 256 位的
    随机串，没有字典可猜，而查表需要**等值索引**（Argon2 每行盐不同，只能全表
    逐行 verify）。密码与恢复码那种「人选的 / 短的」才必须用 Argon2id。
    """

    __tablename__ = "password_reset_tokens"

    id: Mapped[int] = mapped_column(_PrimaryKey, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        _ForeignKeyInt, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    # SHA-256 十六进制。UNIQUE 既是查找索引，也是并发下的兜底。
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False)
    # 非空 = 已经用掉。⚠️ 单用**靠条件更新的受影响行数**判定，不靠先读后写 ——
    # 中间那一瞬就是 TOCTOU 窗口，同一张令牌会被两个并发请求各用一次。
    used_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False)

    __table_args__ = (Index("ix_password_reset_tokens_user", "user_id", "used_at"),)


class OutboxStatus(enum.StrEnum):
    """⚠️ 刻意没有 `PROCESSING`。

    「领取中」这个状态需要配一个可见性超时，否则 worker 崩在中间的那一行会
    **永远卡在 PROCESSING**，谁也不会再碰它 —— 而那正是 Invariant 14 要防的
    「队列丢了，已持久化的工作也跟着没了」。

    这里改成：领取时把 `next_retry_at` 推后一个退避间隔，状态仍是 `PENDING`。
    worker 崩掉的后果因此变成「这一行晚几分钟重投」，不需要任何额外的超时清扫
    逻辑。代价是崩溃那一次白占一个 `attempt_count` 名额 —— 便宜得多。
    """

    PENDING = "PENDING"
    SENT = "SENT"
    # 重试次数用尽。⚠️ 这是**死信**，不是「失败了以后会再试」：它不会被恢复
    # 任务再捡起来，需要人介入。连续失败告警归 T0.9。
    FAILED = "FAILED"


class DomainOutbox(Base):
    """Transactional outbox (spec §74.6, §25, §98.1; ADR-0009).

    ⚠️ **这张表是事实来源，队列只是投递触发。**行与触发它的业务变更**同事务**
    写入（Invariant 13），Redis / Celery 整个丢掉之后，周期恢复任务仍能从这张表
    把该做而没做的投递重建出来（Invariant 14）。

    反过来说：**任何「只发了队列消息、没写这张表」的做法都是错的** —— Redis
    一丢，那件事就再也没人知道该做了。
    """

    __tablename__ = "domain_outbox"

    id: Mapped[int] = mapped_column(_PrimaryKey, primary_key=True, autoincrement=True)
    # 字段清单照 spec §74.6，一列不多一列不少。
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    aggregate_type: Mapped[str] = mapped_column(String(64), nullable=False)
    aggregate_id: Mapped[str] = mapped_column(String(64), nullable=False)
    # ⚠️ 投递密码重置信时，这里**含令牌明文** —— 邮件必须带着它，而库里其他
    # 地方只有哈希。所以投递成功的那一刻会把这一列置空（见 app/tasks/outbox.py）：
    # 它的用途在投递完成的一瞬就结束了，而 outbox 行是长期保留的。
    payload_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[OutboxStatus] = mapped_column(
        Enum(OutboxStatus, native_enum=False, length=_ENUM_LENGTH),
        nullable=False,
        default=OutboxStatus.PENDING,
    )
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # 下一次可以被领取的时刻。新行填 `created_at`（立刻可投）。
    next_retry_at: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False)
    processed_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)

    # 恢复任务每分钟扫一次「到期的待投递行」，这条索引就是为那个查询建的。
    __table_args__ = (Index("ix_domain_outbox_due", "status", "next_retry_at"),)

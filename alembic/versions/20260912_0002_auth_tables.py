"""auth tables: users, refresh_tokens, audit_logs

**这是本仓库第一次建真实业务表。**T0.4 的基线迁移是空的，而 T0.4 的记录里写的是
「业务表从 Phase 1 开始」—— 那个前提被 T0.8 打破了，所以 spec §132 第 13 条
（迁移要有生产锁、备份与失败处理分析）落在这里，不是 Phase 1。

§132 第 13 条分析
-----------------

**锁表与停机**：三张表全是新建，`CREATE TABLE` 在空库上瞬时完成，**不锁任何既有
表**（此刻还没有既有表）。这是本项目在锁表上唯一轻松的一次 —— Phase 1 起对已有
数据的表加列就要重新评估。

**部署顺序**：迁移可以**先于**应用滚动。新表不被旧代码读写，旧实例遇到新端点
只会返回 404，不会误读新表。

**备份**：此版之前库里只有 `alembic_version`，无数据可丢，因此不要求前置备份。
⚠️ **下一版起就要求了** —— 一旦有真实管理员账号，`downgrade` 就是数据丢失。

**失败处理**：⚠️ **MySQL 的 DDL 不参与事务**，所以「三条 CREATE TABLE 要么全成
要么全不成」是**不成立的**。中途失败时 `alembic_version` 不前进，但先建好的表
**会留在库里**，重跑会撞上 "table already exists"。处置是人工删掉已建的表再重跑 ——
所以 `upgrade` 里**不做任何非幂等的数据写入**（比如顺手插一个默认管理员），
让「删表重跑」始终是安全的。

这不是推测：本版的 `downgrade` 实测中途失败过一次（见下面 `downgrade()` 里的
注释），留下的正是 schema 与版本号对不上的状态。

**回滚**：`downgrade` 直接 `DROP TABLE`。**生产上不要 downgrade 这一版**：它会
连同所有管理员账号与审计记录一起删掉，而审计记录按 §66 是不可重建的。出事往前
修，不往回退 —— 这条写进 runbook。

Revision ID: 0002_auth_tables
Revises: 0001_baseline
Create Date: 2026-09-12
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0002_auth_tables"
down_revision: str | None = "0001_baseline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 枚举用 VARCHAR + CHECK 语义（SQLAlchemy 的 native_enum=False），不用 MySQL 的
# 原生 ENUM：原生 ENUM 加一个取值要 ALTER TABLE 重建，而角色与状态的取值一定会
# 增加（Phase 4 的 CUSTOMER 已经在路上）。
_ROLE = sa.Enum("ADMIN", "CUSTOMER", name="user_role", native_enum=False)
_STATUS = sa.Enum("ACTIVE", "DISABLED", name="user_status", native_enum=False)
_ACTION = sa.Enum(
    "LOGIN",
    "LOGIN_FAILED",
    "LOGOUT",
    "TOKEN_REUSED",
    "USER_CREATED",
    name="audit_action",
    native_enum=False,
)


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("role", _ROLE, nullable=False),
        sa.Column("status", _STATUS, nullable=False),
        sa.Column("failed_login_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("locked_until", sa.DateTime(), nullable=True),
        # 递增锁定的档位。不随锁到期清零 —— 见 app/models/auth.py 的注释。
        sa.Column("lockout_level", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_login_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        # 登录的唯一入口。唯一约束在**数据库**上，不只在应用里查一次 ——
        # 并发注册时应用层的先查后插挡不住。
        sa.UniqueConstraint("email", name="uq_users_email"),
    )

    op.create_table(
        "refresh_tokens",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("family_id", sa.String(length=36), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("issued_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("used_at", sa.DateTime(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        # 既是查找索引，也是并发下的兜底：同一个令牌不可能存在两行。
        sa.UniqueConstraint("token_hash", name="uq_refresh_tokens_token_hash"),
    )
    # 检测到重放时要一次吊销整个家族，按 (user_id, family_id) 扫。
    op.create_index("ix_refresh_tokens_family", "refresh_tokens", ["user_id", "family_id"])

    op.create_table(
        "audit_logs",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        # 可空：登录失败时我们不一定知道是谁（邮箱可能根本不存在），
        # 而那一条恰恰最该记下来。
        sa.Column("actor_user_id", sa.BigInteger(), nullable=True),
        sa.Column("actor_role", sa.String(length=32), nullable=True),
        sa.Column("action", _ACTION, nullable=False),
        sa.Column("entity_type", sa.String(length=64), nullable=True),
        sa.Column("entity_id", sa.String(length=64), nullable=True),
        sa.Column("before_state", sa.Text(), nullable=True),
        sa.Column("after_state", sa.Text(), nullable=True),
        sa.Column("ip_address", sa.String(length=45), nullable=True),
        sa.Column("user_agent", sa.String(length=512), nullable=True),
        sa.Column("reason", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        # ⚠️ **刻意不加 actor_user_id 的外键**：§66 要求审计记录长期保留，
        # 而外键会让「删用户」变成「要么级联删掉他的审计记录、要么删不掉用户」。
        # 两种都不对 —— 审计必须比它记录的对象活得更久。
    )
    op.create_index("ix_audit_logs_actor_created", "audit_logs", ["actor_user_id", "created_at"])


def downgrade() -> None:
    # ⚠️ 见文件顶部：**生产上不要跑这个**。它会连同管理员账号与审计记录一起
    # 删掉，而审计记录按 §66 不可重建。
    #
    # ⚠️ **不要在 DROP TABLE 之前显式 drop_index。**`DROP TABLE` 本来就会一并
    # 删掉索引，而在 MySQL 上显式删会失败：`ix_refresh_tokens_family` 的首列是
    # `user_id`，MySQL 用它满足外键约束的索引要求，于是拒绝 DROP INDEX。
    # 实测过一次：audit_logs 已经删掉、refresh_tokens 的 DROP INDEX 报错中断，
    # 而 `alembic_version` 仍停在本版 —— schema 与版本号对不上，**再 upgrade
    # 也不会把 audit_logs 补回来**，因为 alembic 认为这一版已经应用过了。
    #
    # 顺序按外键反向：refresh_tokens 引用 users。
    op.drop_table("audit_logs")
    op.drop_table("refresh_tokens")
    op.drop_table("users")

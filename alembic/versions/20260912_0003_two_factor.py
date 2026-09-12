"""two-factor authentication: two_factor_settings, recovery_codes

§132 第 13 条分析
-----------------

**锁表与停机**：两张表全是新建，`CREATE TABLE` 瞬时完成。外键指向 `users`，
MySQL 会在建表时对 `users` 取一个元数据锁 —— 空库上可以忽略，但**有数据之后
对繁忙的表加外键是要评估的**（Phase 1 起每次都要重新看）。

**备份**：上一版（`0002_auth_tables`）之后可能已经有真实管理员账号了，所以
**这一版之前要求前置备份**。`0002` 那时候库里只有 `alembic_version`，不要求。

**部署顺序**：迁移可以先于应用滚动。旧代码不认识这两张表，不会误读。

**失败处理**：⚠️ MySQL 的 DDL **不参与事务**。两张表之间失败时，先建好的那张
会留在库里而 `alembic_version` 不前进，重跑会撞 "table already exists"。处置是
人工删掉已建的表再重跑 —— 所以 `upgrade` 里不做任何非幂等的数据写入。

**回滚**：`downgrade` 只 `DROP TABLE`，**不显式 drop_index**。
⚠️ `0002` 上实测踩过：`ix_recovery_codes_user` 这类首列是外键列的索引，MySQL
会拒绝单独 DROP（外键需要它），结果是删了一半、版本号没退的坏状态。
`DROP TABLE` 本来就会一并删索引。

⚠️ **生产上不要 downgrade 这一版**：它会删掉所有人的 2FA 注册与恢复码，
而恢复码是一次性发给用户、我们只存哈希的 —— **删了就再也发不回去**，
每个管理员都要重新走一遍注册流程。

Revision ID: 0003_two_factor
Revises: 0002_auth_tables
Create Date: 2026-09-12
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0003_two_factor"
down_revision: str | None = "0002_auth_tables"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "two_factor_settings",
        # user_id 直接做主键：一个用户至多一份 TOTP 注册，**由主键保证**，
        # 不靠应用层「先查有没有再插」。
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("encrypted_totp_secret", sa.Text(), nullable=False),
        sa.Column("key_version", sa.Integer(), nullable=False),
        sa.Column("secret_version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("confirmed_at", sa.DateTime(), nullable=True),
        sa.Column("last_used_counter", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("user_id"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
    )

    op.create_table(
        "recovery_codes",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("code_hash", sa.String(length=255), nullable=False),
        sa.Column("used_at", sa.DateTime(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
    )
    # 登录时要按用户取「还没用过、也没作废」的码，逐个比对哈希。
    op.create_index(
        "ix_recovery_codes_user", "recovery_codes", ["user_id", "used_at", "revoked_at"]
    )

    # ⚠️ **加宽三个枚举列。**`Enum(native_enum=False)` 的 VARCHAR 宽度按建表那一刻
    # 最长的成员算：0002 建 `action` 时最长是 `LOGIN_FAILED`（12 字符），而本版
    # 新增了 `RECOVERY_CODES_REGENERATED`（26 字符）—— 真 MySQL 会在插入时报
    # `Data too long for column 'action'`。
    #
    # ⚠️ **单元测试看不见这个**：SQLite 根本不强制 VARCHAR 长度，208 条用例照样
    # 全绿。是整栈实测走一遍真实的 2FA 注册才把它暴露出来的。
    #
    # 统一钉成 64（`app/models/auth.py` 的 `_ENUM_LENGTH`），以后加枚举值不必
    # 再配一次 ALTER。
    for table, column in (("audit_logs", "action"), ("users", "role"), ("users", "status")):
        op.alter_column(
            table,
            column,
            existing_type=sa.String(length=32),
            type_=sa.String(length=64),
            existing_nullable=False,
        )


def downgrade() -> None:
    # 枚举列宽回不去：0002 建表时的宽度是从当时的成员推出来的，而那些成员现在
    # 已经变了。窄回去等于可能截断已有数据 —— **宁可留宽**。

    # ⚠️ 见文件顶部：生产上不要跑这个，恢复码删了发不回去。
    # 不显式 drop_index —— DROP TABLE 会一并删，而单独删首列是外键列的索引
    # 在 MySQL 上会被拒绝（0002 上实测踩过）。
    op.drop_table("recovery_codes")
    op.drop_table("two_factor_settings")

"""password reset + domain outbox: password_reset_tokens, domain_outbox

§132 第 13 条分析
-----------------

**锁表与停机**：两张表全是新建，`CREATE TABLE` 瞬时完成。`password_reset_tokens`
的外键指向 `users`，MySQL 建表时会对 `users` 取一个元数据锁 —— 此刻 `users` 里
只有少量管理员账号，可以忽略；`domain_outbox` **没有任何外键**（它将来要装的是
钱包、支付、状态跃迁等各种聚合的事件，`aggregate_id` 是字符串而不是外键列，
照 spec §74.6）。

**备份**：要求前置备份。`0002` / `0003` 之后库里已经有真实管理员账号、TOTP
注册与恢复码。

**部署顺序**：迁移可以先于应用滚动。旧代码不认识这两张表，不会误读。
⚠️ **反过来不行**：新代码先上、迁移没跑，忘记密码端点会在插入时直接报错。

**失败处理**：⚠️ MySQL 的 DDL **不参与事务**。两张表之间失败时，先建好的那张
会留在库里而 `alembic_version` 不前进，重跑会撞 "table already exists"。处置是
人工删掉已建的表再重跑 —— 所以 `upgrade` 里不做任何非幂等的数据写入。

**回滚**：`downgrade` 只 `DROP TABLE`，**不显式 drop_index**（`0002` 上实测踩过：
首列是外键列的索引，MySQL 会拒绝单独 DROP）。

⚠️ **生产上 downgrade 这一版的后果**：
- 所有**未投递**的 outbox 行连同它们记录的「该发而没发的信」一起消失，
  且**没有任何别处留有副本** —— 这张表就是事实来源（Invariant 14）
- 进行中的密码重置全部作废（用户手上的链接会变成 `INVALID_RESET_TOKEN`）

前者不可恢复，后者用户重新申请即可。

Revision ID: 0004_password_reset_outbox
Revises: 0003_two_factor
Create Date: 2026-09-12
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0004_password_reset_outbox"
down_revision: str | None = "0003_two_factor"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# ⚠️ **`length=64` 必须显式写出来。**
#
# 不写的话，`Enum(native_enum=False)` 会按**建表那一刻最长的成员**推宽度 ——
# 这里是 `PENDING`，于是列建成 `VARCHAR(7)`，而 `app/models/auth.py` 那边是
# `VARCHAR(64)`：模型与库悄悄对不上。以后往 `OutboxStatus` 加一个更长的值
# （`CANCELLED` 就够了）会在真 MySQL 上报 `Data too long`，而**单元测试全绿**
# —— SQLite 根本不强制 VARCHAR 长度。
#
# 0003 的文件头记着同一个坑在 `audit_logs.action` 上真发生过一次。本版初稿
# 注释里写着「钉成 64」、代码却没写 length，**是整栈实测对着真 MySQL
# `DESCRIBE` 才看出来的**：它的现象在本地一次也不会出现。
_OUTBOX_STATUS = sa.Enum(
    "PENDING", "SENT", "FAILED", name="outbox_status", native_enum=False, length=64
)


def upgrade() -> None:
    op.create_table(
        "password_reset_tokens",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        # 只存 SHA-256 哈希。库里没有任何能直接拿去重置别人密码的东西。
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("used_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        # UNIQUE 既是查找索引，也是并发下的兜底。
        sa.UniqueConstraint("token_hash", name="uq_password_reset_tokens_token_hash"),
    )
    # 重置成功时要把该用户其余未用的令牌一并作废，按 (user_id, used_at) 取。
    op.create_index(
        "ix_password_reset_tokens_user", "password_reset_tokens", ["user_id", "used_at"]
    )

    op.create_table(
        "domain_outbox",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        # 字段清单照 spec §74.6，一列不多一列不少。
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("aggregate_type", sa.String(length=64), nullable=False),
        # ⚠️ **不是外键。**这张表将来要装钱包、支付、状态跃迁等各种聚合的事件，
        # 指向哪张表由 `aggregate_type` 说明。照 spec §74.6 存字符串。
        sa.Column("aggregate_id", sa.String(length=64), nullable=False),
        # 可空：投递成功后会被置空（密码重置的 payload 里含令牌明文，
        # 而它的用途在投递完成的那一瞬就结束了）。
        sa.Column("payload_json", sa.Text(), nullable=True),
        sa.Column("status", _OUTBOX_STATUS, nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_retry_at", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("processed_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    # 恢复任务每分钟扫一次「到期的待投递行」，这条索引就是为那个查询建的。
    op.create_index("ix_domain_outbox_due", "domain_outbox", ["status", "next_retry_at"])


def downgrade() -> None:
    # ⚠️ 见文件顶部：未投递的 outbox 行没有任何别处的副本，删了就没了。
    # 不显式 drop_index —— DROP TABLE 会一并删。
    op.drop_table("domain_outbox")
    op.drop_table("password_reset_tokens")

"""users and refresh_tokens password_version, with their checks

照设计闸门 #369 v6（docs/design/AIH-TASK-086-session-revocation.md）
§2「数据库（迁移）」与 §8；本迁移是 §11 的 P1。只加列与 CHECK：
`users.password_version` 与 `refresh_tokens.password_version`，
都是 `INT NOT NULL DEFAULT 0`、`CHECK (password_version >= 0)`。
不加索引：比较都发生在按主键读到的行上（设计 §2）。
不改任何服务、令牌与接口：递增是 P3（`reset_password`），
签发与比较是 P2（AIH-TASK-087）。

`app/models/auth.py` 的 `User`、`RefreshToken` 同步声明了这两列与两条 CHECK，
`tests/backend/test_migrations.py` 比对两边一致。

步骤
----

1. `add_column users.password_version`（服务器端默认 0），
   再 `create_check_constraint ck_users_password_version`
2. `add_column refresh_tokens.password_version`（服务器端默认 0），
   再 `create_check_constraint ck_refresh_tokens_password_version`

§132 第 13 条分析
-----------------

**已有数据**：不改任何已有列、约束与触发器。两张表的已有行都取服务器端
默认值 0（设计 §2「兼容」：部署前发出的令牌在用户第一次改密码之前照常
可用）。不另写回填语句。

**锁表与停机**：两张表都很小（生产 `users` 2 行，`refresh_tokens` 是会话
量级）。MySQL 8 加带默认值的列可以即时完成（`ALGORITHM=INSTANT`），
加 CHECK 要扫一遍已有行，毫秒到秒级（设计 §8）。期间登录、刷新会短暂
等元数据锁。不需要停机。

**备份**：要求前置备份（与 0005–0023 同一口径）。本版只写默认值 0，
不改任何既有值。

**部署顺序**：`deploy.sh` 先迁移、再换镜像。迁移先于代码生效：旧代码
插入刷新令牌行时不写这一列，取默认值 0；旧代码不读它。所以
「新迁移 + 旧代码」行为不变（设计 §8「兼容窗口」）。

**失败处理**：⚠️ MySQL 的 DDL **不参与事务**。任何一步失败或进程在
中途被杀：已加的列或 CHECK 留着而 `alembic_version` 没有前进，重跑会撞
"Duplicate column name" 或同名约束。确认 `alembic_version` 仍是
`0023_wallet_ledger_ref_index` 后按存在与否清场：
`ALTER TABLE refresh_tokens DROP CHECK ck_refresh_tokens_password_version`；
`ALTER TABLE refresh_tokens DROP COLUMN password_version`；
`ALTER TABLE users DROP CHECK ck_users_password_version`；
`ALTER TABLE users DROP COLUMN password_version`，再重新部署。
此时还没有写入方（P3 未上线），列里只有默认值 0，
所以「清场重跑」始终安全。

**回滚**：`downgrade` 按升级的倒序撤回：先删 CHECK 再删列（MySQL 不许
删掉 CHECK 还在引用的列），`refresh_tokens` 先于 `users`。其余表、列、
约束与数据都不动。⚠️ P3 上线之后版本号会大于 0，降级会丢掉它们；代码
回滚到旧版本时新列本来就被忽略（设计 §8「回滚或前滚」），生产只前滚。

Revision ID: 0024_password_version
Revises: 0023_wallet_ledger_ref_index
Create Date: 2026-10-19
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0024_password_version"
down_revision: str | None = "0023_wallet_ledger_ref_index"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_USERS = "users"
_REFRESH_TOKENS = "refresh_tokens"
_COLUMN = "password_version"
# 服务器端默认值：已有行与不写这一列的插入（旧代码）都取 0。
_DEFAULT = "0"

# 表 → (CHECK 名, 条件)，按升级顺序。⚠️ 冻结的字面量：与 app/models/auth.py
# 上的 CheckConstraint 一致（空白除外），test_migrations.py 比对两边。
_CHECKS: dict[str, tuple[str, str]] = {
    _USERS: ("ck_users_password_version", "password_version >= 0"),
    _REFRESH_TOKENS: ("ck_refresh_tokens_password_version", "password_version >= 0"),
}


def upgrade() -> None:
    for table, (name, condition) in _CHECKS.items():
        op.add_column(
            table,
            sa.Column(_COLUMN, sa.Integer(), nullable=False, server_default=_DEFAULT),
        )
        op.create_check_constraint(name, table, condition)


def downgrade() -> None:
    # 倒序；先删 CHECK 再删列：MySQL 不许删掉 CHECK 还在引用的列。
    for table, (name, _condition) in reversed(_CHECKS.items()):
        op.execute(f"ALTER TABLE {table} DROP CHECK {name}")
        op.drop_column(table, _COLUMN)

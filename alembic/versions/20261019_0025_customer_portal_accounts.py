"""users tenant link and invitation status, password_reset_tokens purpose

照设计闸门 #368 v10（docs/design/AIH-TASK-089-customer-portal-auth.md）§2「数据库」与 §8；
本迁移是 §11 的任务 A（AIH-TASK-089）。只改表结构：

- `users` 加 `tenant_id`（可空，外键 `fk_users_tenant` → `tenants.id`，`ON DELETE RESTRICT`）
  与 `activated_at`（可空）；`UNIQUE (tenant_id)`（`uq_users_tenant`，spec §52「一个客户
  一个门户账号」的数据库兜底；ADMIN 为 NULL，唯一索引允许多个 NULL）；三条 CHECK：
  `ck_users_admin_no_tenant`、`ck_users_status`、`ck_users_invited_customer`。
  ⚠️ **刻意不加**「CUSTOMER 必须有 `tenant_id`」：理由与代价见设计 §9 备选 F。
- `password_reset_tokens` 加 `purpose VARCHAR(32) NOT NULL DEFAULT 'PASSWORD_RESET'` 与
  `ck_password_reset_tokens_purpose`。索引不动：`ix_password_reset_tokens_user` 同时是
  `user_id` 外键在 MySQL 上依赖的索引（设计 §2）。

不改任何服务、接口与配置：用途过滤、开通与接受邀请属于 AIH-TASK-090 / 091。
`app/models/auth.py` 的 `User`、`PasswordResetToken` 同步声明了这些列与约束，
`tests/backend/test_migrations.py` 比对两边一致。

步骤
----

1. `add_column users.tenant_id`、`add_column users.activated_at`（都可空，不回填）
2. `create_unique_constraint uq_users_tenant`，再 `create_foreign_key fk_users_tenant`。
   ⚠️ 顺序不能反：MySQL 建外键时若列上还没有可用的索引，会自动建一个与外键同名的索引，
   之后再建唯一索引时又把它静默删掉 —— 结构取决于建的先后。先建唯一索引，外键直接用它。
3. `create_check_constraint` 三条（`users`）
4. `add_column password_reset_tokens.purpose`（服务器端默认 `PASSWORD_RESET`），
   再 `create_check_constraint ck_password_reset_tokens_purpose`

§132 第 13 条分析
-----------------

**已有数据**：不改任何已有列、索引与触发器。`users` 的两个新列都可空、不回填；现有行都是
ADMIN、`ACTIVE` 或 `DISABLED`、`tenant_id` 为 NULL，三条 CHECK 与唯一约束全部满足（设计 §8
第 2 步）。`password_reset_tokens` 的已有行取服务器端默认值 `PASSWORD_RESET`，不另写回填语句。

**锁表与停机**：两张表都很小（生产 `users` 2 行，重置令牌是请求量级）。MySQL 8 加可空列与
带默认值的列可以即时完成（`ALGORITHM=INSTANT`）；加唯一索引、外键与 CHECK 要扫一遍已有行，
毫秒级（设计 §8）。期间登录、刷新、重置会短暂等元数据锁。不需要停机。

**备份**：要求前置备份（与 0005–0024 同一口径）。本版不改任何既有值。

**部署顺序**：`deploy.sh` 先迁移、再换镜像。迁移先于代码生效：旧代码读 `users` 时多出的列
被忽略；插入重置令牌时不写 `purpose`，取默认值 `PASSWORD_RESET`。所以「新迁移 + 旧代码」
窗口里登录与重置照常（设计 §8「兼容窗口」）。本任务不带写入方，部署后不会出现 CUSTOMER
用户与 `INVITATION` 令牌。

**失败处理**：⚠️ MySQL 的 DDL **不参与事务**。任何一步失败或进程在中途被杀：已加的列、
索引、外键或 CHECK 留着而 `alembic_version` 没有前进，重跑会撞 "Duplicate column name" 或
同名约束。确认 `alembic_version` 仍是 `0024_password_version` 后按存在与否清场：
`ALTER TABLE password_reset_tokens DROP CHECK ck_password_reset_tokens_purpose`；
`ALTER TABLE password_reset_tokens DROP COLUMN purpose`；
`ALTER TABLE users DROP CHECK ck_users_invited_customer`；
`ALTER TABLE users DROP CHECK ck_users_status`；
`ALTER TABLE users DROP CHECK ck_users_admin_no_tenant`；
`ALTER TABLE users DROP FOREIGN KEY fk_users_tenant`；
`ALTER TABLE users DROP INDEX uq_users_tenant`；
`ALTER TABLE users DROP COLUMN activated_at`；`ALTER TABLE users DROP COLUMN tenant_id`，
再重新部署。此时还没有写入方，新列里只有 NULL 与默认值，所以「清场重跑」始终安全。

**回滚**：`downgrade` 先查有没有 role 为 CUSTOMER 的用户或 purpose 为 INVITATION 的令牌，
有就在任何 DDL 之前拒绝 —— 删列会丢掉门户账号与租户的关联，剩下的 CUSTOMER 与邀请令牌在
旧结构里也无从区分（设计 §8「回滚或前滚」）。没有时按升级的倒序撤回：先删 CHECK 再删列
（MySQL 不许删掉 CHECK 还在引用的列），外键先于它所用的唯一索引，`password_reset_tokens`
先于 `users`。其余表、列、约束与数据都不动。`--sql` 离线模式读不到数据，跳过这一查、只输出
DDL（与 0022 相同）。生产只前滚。

Revision ID: 0025_customer_portal_accounts
Revises: 0024_password_version
Create Date: 2026-10-19
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0025_customer_portal_accounts"
down_revision: str | None = "0024_password_version"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_USERS = "users"
_TOKENS = "password_reset_tokens"
_TENANTS = "tenants"

_TENANT_ID = "tenant_id"
_ACTIVATED_AT = "activated_at"
_PURPOSE = "purpose"
_PURPOSE_LENGTH = 32
# 服务器端默认值：已有行与不写这一列的插入（旧代码）都是重置令牌。
_PURPOSE_DEFAULT = "PASSWORD_RESET"

_UNIQUE_TENANT = "uq_users_tenant"
_FK_TENANT = "fk_users_tenant"

# 名字 → 条件，按升级顺序。⚠️ 冻结的字面量：与 app/models/auth.py 上的 CheckConstraint
# 一致（空白除外），test_migrations.py 比对两边。
_USER_CHECKS: dict[str, str] = {
    # ADMIN 一定不属于任何租户。「CUSTOMER 一定属于某个租户」刻意不写（设计 §9 备选 F）。
    "ck_users_admin_no_tenant": "role = 'CUSTOMER' OR tenant_id IS NULL",
    "ck_users_status": "status IN ('ACTIVE', 'INVITED', 'DISABLED')",
    # 只有门户账号有「已邀请、未接受」这一态。
    "ck_users_invited_customer": "status <> 'INVITED' OR role = 'CUSTOMER'",
}
_TOKEN_CHECKS: dict[str, str] = {
    "ck_password_reset_tokens_purpose": "purpose IN ('PASSWORD_RESET', 'INVITATION')",
}

# 降级前的数据检查（设计 §8「回滚或前滚」）：条件 → 拒绝时的原因。
_LOSSY: dict[str, str] = {
    "SELECT 1 FROM users WHERE role = 'CUSTOMER' LIMIT 1": "a CUSTOMER user exists",
    "SELECT 1 FROM password_reset_tokens WHERE purpose = 'INVITATION' LIMIT 1": (
        "an INVITATION token exists"
    ),
}


def upgrade() -> None:
    op.add_column(_USERS, sa.Column(_TENANT_ID, sa.BigInteger(), nullable=True))
    op.add_column(_USERS, sa.Column(_ACTIVATED_AT, sa.DateTime(), nullable=True))
    # 唯一索引在外键之前：外键直接用它，MySQL 不另建同名索引。
    op.create_unique_constraint(_UNIQUE_TENANT, _USERS, [_TENANT_ID])
    op.create_foreign_key(_FK_TENANT, _USERS, _TENANTS, [_TENANT_ID], ["id"], ondelete="RESTRICT")
    for name, condition in _USER_CHECKS.items():
        op.create_check_constraint(name, _USERS, condition)

    op.add_column(
        _TOKENS,
        sa.Column(
            _PURPOSE,
            sa.String(length=_PURPOSE_LENGTH),
            nullable=False,
            server_default=_PURPOSE_DEFAULT,
        ),
    )
    for name, condition in _TOKEN_CHECKS.items():
        op.create_check_constraint(name, _TOKENS, condition)


def downgrade() -> None:
    # ⚠️ 在任何 DDL 之前：有门户账号或邀请令牌就拒绝，删列等于丢掉租户关联。
    # `--sql` 离线模式读不了数据：跳过这一查，DDL 照旧。
    if not op.get_context().as_sql:
        bind = op.get_bind()
        for query, reason in _LOSSY.items():
            if bind.execute(sa.text(query)).first():
                raise RuntimeError(
                    f"Cannot downgrade customer portal accounts: {reason};"
                    " dropping the columns would lose the tenant link. Roll forward instead"
                )

    # 倒序；先删 CHECK 再删列：MySQL 不许删掉 CHECK 还在引用的列。
    for name in reversed(_TOKEN_CHECKS):
        op.execute(f"ALTER TABLE {_TOKENS} DROP CHECK {name}")
    op.drop_column(_TOKENS, _PURPOSE)

    for name in reversed(_USER_CHECKS):
        op.execute(f"ALTER TABLE {_USERS} DROP CHECK {name}")
    # 外键先于它所用的唯一索引：MySQL 不许删掉外键还在用的索引。
    op.drop_constraint(_FK_TENANT, _USERS, type_="foreignkey")
    op.drop_constraint(_UNIQUE_TENANT, _USERS, type_="unique")
    op.drop_column(_USERS, _ACTIVATED_AT)
    op.drop_column(_USERS, _TENANT_ID)

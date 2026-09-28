"""tenants.account_status: admin-controlled account lifecycle

照设计闸门 #136 v2（docs/design/AIH-TASK-020-tenant-account-status.md）§2「数据库」与 §8。

步骤
----

1. `tenants` 加 `account_status VARCHAR(32) NOT NULL DEFAULT 'ENABLED'`：服务端默认值给
   已有行回填，不另写 UPDATE
2. 加 `ck_tenants_account_status`：`PENDING_ACTIVATION` / `ENABLED` / `DISABLED` / `CLOSED`
   四个取值。本版代码只产生 `ENABLED` / `DISABLED`；另两个现在就允许，免得关户任务再改列

不加索引：没有按账户状态筛选的查询。

§132 第 13 条分析
-----------------

**锁表与停机**：只 ALTER `tenants`（加一列、一条 CHECK），生产上 2 行。MySQL 8.4 给表尾
加带默认值的列走 INSTANT，不重建表；加 CHECK 要校验已有行，行数两行，瞬时完成。不读写
任何别的表，不碰钱包与账本。不需要停机。

**备份**：要求前置备份（与 0005–0008 同一口径）。本版只给已有行填默认值 `ENABLED`，不改动
任何已有列的数据。

**部署顺序**：`deploy.sh` 先迁移、再换镜像。旧代码不认识新列、也不写它（服务端默认值兜底），
不受影响。新代码的客户详情、客户列表与账户状态接口依赖新列，所以迁移必须先于代码；迁移失败
时容器启动失败，旧容器继续服务。

**失败处理**：⚠️ MySQL 的 DDL **不参与事务**。
- 发出的是两条 `ALTER TABLE tenants`：加列、加 CHECK。
- 加列失败：列不存在、`alembic_version` 仍是 `0008_webhook_signing`，修好后重新部署即可。
- 加 CHECK 失败，或进程在加列之后被杀：列已存在而 `alembic_version` 没有前进，重跑会撞
  "Duplicate column name"。确认 `alembic_version` 仍是 `0008_webhook_signing` 后
  `ALTER TABLE tenants DROP COLUMN account_status`（CHECK 若已建，先
  `ALTER TABLE tenants DROP CHECK ck_tenants_account_status`），再重新部署。
- 本版除默认值外不写数据，所以「清场重跑」始终安全。

**回滚**：`downgrade` 先删 CHECK、再删列（MySQL 不许删掉 CHECK 还在引用的列）。⚠️ 已有租户
被置为 `DISABLED` 时，回滚会丢掉这个状态，客户随之恢复可用 —— 那时应前滚修复而不是回滚
（设计 §8）。

Revision ID: 0009_tenant_account_status
Revises: 0008_webhook_signing
Create Date: 2026-09-28
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0009_tenant_account_status"
down_revision: str | None = "0008_webhook_signing"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "tenants"
_COLUMN = "account_status"
_DEFAULT = "ENABLED"

# 名字 → 条件。⚠️ 冻结的字面量：与 app/models/tenancy.py 上的 CheckConstraint 一致
# （空白除外），tests/backend/test_migrations.py 比对两边。
_CHECKS: dict[str, str] = {
    "ck_tenants_account_status": (
        "account_status IN ('PENDING_ACTIVATION', 'ENABLED', 'DISABLED', 'CLOSED')"
    ),
}


def upgrade() -> None:
    op.add_column(
        _TABLE,
        sa.Column(_COLUMN, sa.String(length=32), nullable=False, server_default=_DEFAULT),
    )
    for name, condition in _CHECKS.items():
        op.create_check_constraint(name, _TABLE, condition)


def downgrade() -> None:
    # 先删 CHECK 再删列：MySQL 不许删掉 CHECK 还在引用的列。
    for name in _CHECKS:
        op.execute(f"ALTER TABLE {_TABLE} DROP CHECK {name}")
    op.drop_column(_TABLE, _COLUMN)

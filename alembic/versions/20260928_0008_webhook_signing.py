"""project_webhook_secrets: outbound status-webhook signing secrets

照设计闸门 #135 v1（docs/design/AIH-TASK-019-webhook-signing.md）§2「数据库」与 §8。
文件名用 `webhook_signing` 而不是表名：后者撞上 Worker 的敏感路径规则（设计 §11）。

步骤
----

1. `create_table project_webhook_secrets`：两个 STORED 生成列 `active_slot` /
   `pending_slot` 与它们的唯一索引（每个项目至多一个 ACTIVE、至多一个 PENDING）、
   `(project_id, key_version)` 唯一、三条 CHECK（版本下限、状态取值、状态与时间一致）、
   复合外键 `(project_id, tenant_id)` → `projects(id, tenant_id)` `ON DELETE RESTRICT`
   （被引用的 `uq_projects_id_tenant` 是 0007 建的）

§132 第 13 条分析
-----------------

**锁表与停机**：只建一张新表与它的索引，不 ALTER、不读写任何既有表。外键指向 `projects`，
MySQL 建外键时对 `projects` 只取元数据锁，行数两行，瞬时完成。不需要停机。

**备份**：要求前置备份（与 0005–0007 同一口径）。本版不改动、不回填任何既有数据。

**部署顺序**：`deploy.sh` 先迁移、再换镜像。旧代码不认识新表，不受影响。新代码的四个管理端
接口依赖新表；签发还依赖主密钥文件（生产已配置，2FA 与集成凭据在用）。

**失败处理**：⚠️ MySQL 的 DDL **不参与事务**。
- 发出的是一条 `CREATE TABLE`（列、CHECK、唯一约束、外键）加两条 `CREATE UNIQUE INDEX`
  （生成列上的索引，alembic 在建表之后单独发）。
- `CREATE TABLE` 失败：表不存在、`alembic_version` 仍是 `0007_integration_access`，
  修好后重新部署即可。
- 建索引失败，或进程在建表之后被杀：表已存在而 `alembic_version` 没有前进，重跑会撞
  "Table already exists"。确认 `alembic_version` 仍是 `0007_integration_access`、
  `project_webhook_secrets` 为空后 `DROP TABLE project_webhook_secrets`，再重新部署。
- 本版不写任何数据，所以「清场重跑」始终安全。

**回滚**：`downgrade` 删表（连同外键与索引），不碰 `projects`。⚠️ 表里若已有密钥，回滚会
连同全部密文一起删掉，集成方手里的 secret 再也无法验证 —— 那时应前滚修复而不是回滚
（设计 §8）。

Revision ID: 0008_webhook_signing
Revises: 0007_integration_access
Create Date: 2026-09-28
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0008_webhook_signing"
down_revision: str | None = "0007_integration_access"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "project_webhook_secrets"

# 名字 → 条件。⚠️ 冻结的字面量：与 app/models/integration.py 上的 CheckConstraint 逐条一致
# （空白除外），tests/backend/test_migrations.py 比对两边。
_CHECKS: dict[str, str] = {
    "ck_project_webhook_secrets_key_version": "key_version >= 1",
    "ck_project_webhook_secrets_status": "status IN ('PENDING', 'ACTIVE', 'RETIRED')",
    # 时间与状态一致：PENDING 两个时刻都没有；ACTIVE 有启用、没有退役；RETIRED 有退役
    # （从 PENDING 直接退役的没有启用时刻）。
    "ck_project_webhook_secrets_times": (
        "(status = 'PENDING' AND activated_at IS NULL AND retired_at IS NULL)"
        " OR (status = 'ACTIVE' AND activated_at IS NOT NULL AND retired_at IS NULL)"
        " OR (status = 'RETIRED' AND retired_at IS NOT NULL)"
    ),
}

# 生成列名 → 表达式。同上，冻结的字面量，与模型的 Computed 比对。
_SLOTS: dict[str, str] = {
    "active_slot": "CASE WHEN status = 'ACTIVE' THEN project_id END",
    "pending_slot": "CASE WHEN status = 'PENDING' THEN project_id END",
}


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("tenant_id", sa.BigInteger(), nullable=False),
        # 签名版本（客户可见，`X-Acuven-Key-Version`）。
        sa.Column("key_version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("encrypted_secret", sa.Text(), nullable=False),
        # 主密钥版本（ADR-0004 §4），与 key_version 分开。
        sa.Column("encryption_key_version", sa.Integer(), nullable=False),
        *(
            sa.Column(name, sa.BigInteger(), sa.Computed(expression, persisted=True))
            for name, expression in _SLOTS.items()
        ),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("activated_at", sa.DateTime(), nullable=True),
        sa.Column("retired_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        # 复合外键：密钥行的租户必须是项目所属的租户（INV-8）。RESTRICT：项目删不掉，
        # 以后的投递记录要引用版本（设计 §6）。
        sa.ForeignKeyConstraint(
            ["project_id", "tenant_id"],
            ["projects.id", "projects.tenant_id"],
            name="fk_project_webhook_secrets_project",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "project_id", "key_version", name="uq_project_webhook_secrets_key_version"
        ),
        # 唯一索引允许多个 NULL：只有 ACTIVE / PENDING 行在各自的列上有值。
        sa.Index("ux_project_webhook_secrets_active_slot", "active_slot", unique=True),
        sa.Index("ux_project_webhook_secrets_pending_slot", "pending_slot", unique=True),
        *(sa.CheckConstraint(condition, name=name) for name, condition in _CHECKS.items()),
    )


def downgrade() -> None:
    op.drop_table(_TABLE)

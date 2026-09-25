"""integration_credentials, and projects (id, tenant_id) unique for its composite foreign key

照设计闸门 #118 v1（docs/design/AIH-TASK-012-integration-access.md）§2「数据库」与 §8。
文件名用 `integration_access` 而不是设计 §11 的 `integration_credentials`：后者撞上
Worker 的敏感路径规则，只改文件名，表名与表结构不变。

步骤
----

1. `projects` 加唯一约束 `uq_projects_id_tenant (id, tenant_id)`。`id` 本来就唯一，这个
   约束只为让下一步的复合外键成立（MySQL 要求被引用的列上有索引）
2. `create_table integration_credentials`：`CHECK`、`(public_api_key, key_version)` 唯一、
   `ix_integration_credentials_project_id`、复合外键 `(project_id, tenant_id)` →
   `projects(id, tenant_id)` `ON DELETE RESTRICT`；`public_api_key` 在 MySQL 上是
   `utf8mb4_0900_bin`

§132 第 13 条分析
-----------------

**锁表与停机**：对既有表只 ALTER `projects`（加一个唯一索引）。生产上 `projects` 只有两行
（验收夹具与废弃的核对客户），MySQL 8 在线建索引，瞬时完成。新表 DDL 瞬时完成。不需要停机。

**备份**：要求前置备份（与 0005 / 0006 同一口径）。本版不改动、不回填任何既有数据。

**部署顺序**：`deploy.sh` 先迁移、再换镜像。旧代码不认识新表，也不受 `projects` 上多一个
唯一约束的影响（`id` 本来就唯一，任何已有的写入都不会因此失败）。新代码的五个管理端接口
依赖新表。前提：生产已配置主密钥文件（2FA 在用，已满足）。

**失败处理**：⚠️ MySQL 的 DDL **不参与事务**。
- 第 1 步失败：什么都没变，`alembic_version` 仍是 0006，修好后重新部署。
- 第 2 步失败：`projects` 上已有 `uq_projects_id_tenant` 而 `alembic_version` 不前进，
  重跑会撞 "Duplicate key name"。确认 `alembic_version` 仍是 `0006_wallets_ledger`、
  `integration_credentials` 不存在后
  `ALTER TABLE projects DROP INDEX uq_projects_id_tenant`，再重新部署。
- 本版不写任何数据，所以「清场重跑」始终安全。

**回滚**：`downgrade` 先删表（连同它的外键），再删 `projects` 的约束 —— 反过来的话，外键还在
引用那个唯一索引，MySQL 拒绝删除。⚠️ 表里若已有凭据，先确认没有人依赖它们（Phase 1 没有
任何消费方）；downgrade 会连同全部凭据密文一起删掉，那些 `secret` 再也无法验证。

Revision ID: 0007_integration_access
Revises: 0006_wallets_ledger
Create Date: 2026-09-25
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0007_integration_access"
down_revision: str | None = "0006_wallets_ledger"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 冻结的字面量，与 app/models/integration.py 的 API_KEY_COLLATION 一致
# （test_0007_column_shape 守住）。
_API_KEY_COLLATION = "utf8mb4_0900_bin"

# 名字 → 条件。⚠️ 冻结的字面量：与 app/models/integration.py 上的 CheckConstraint 逐条一致
# （空白除外），tests/backend/test_migrations.py 比对两边。
_CHECKS: dict[str, str] = {
    "ck_integration_credentials_key_version": "key_version >= 1",
    "ck_integration_credentials_status": "status IN ('ACTIVE', 'REVOKED')",
    # 吊销时刻与状态同进退：REVOKED 必有 revoked_at，ACTIVE 必没有。
    "ck_integration_credentials_revoked_at": "(status = 'REVOKED') = (revoked_at IS NOT NULL)",
}


def upgrade() -> None:
    op.create_unique_constraint("uq_projects_id_tenant", "projects", ["id", "tenant_id"])

    op.create_table(
        "integration_credentials",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("tenant_id", sa.BigInteger(), nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        # ⚠️ 二进制、NO PAD 排序：只差大小写或尾部空格的两个 key 不是同一个。
        sa.Column(
            "public_api_key",
            sa.String(length=64, collation=_API_KEY_COLLATION),
            nullable=False,
        ),
        # 签名密钥版本（客户可见）。
        sa.Column("key_version", sa.Integer(), nullable=False),
        sa.Column("encrypted_secret", sa.Text(), nullable=False),
        # 主密钥版本（ADR-0004 §4），与 key_version 分开。
        sa.Column("encryption_key_version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("valid_from", sa.DateTime(), nullable=False),
        sa.Column("valid_until", sa.DateTime(), nullable=True),
        sa.Column("last_used_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        # 复合外键：凭据行的租户必须是项目所属的租户（INV-8）。RESTRICT：以后的用量事件
        # 要引用凭据行，项目删不掉就不会留下悬空引用（INV-6）。
        sa.ForeignKeyConstraint(
            ["project_id", "tenant_id"],
            ["projects.id", "projects.tenant_id"],
            name="fk_integration_credentials_project",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "public_api_key",
            "key_version",
            name="uq_integration_credentials_key_version",
        ),
        sa.Index("ix_integration_credentials_project_id", "project_id"),
        *(sa.CheckConstraint(condition, name=name) for name, condition in _CHECKS.items()),
    )


def downgrade() -> None:
    # 先删表（连同复合外键），再删它引用的唯一约束。
    op.drop_table("integration_credentials")
    op.drop_constraint("uq_projects_id_tenant", "projects", type_="unique")

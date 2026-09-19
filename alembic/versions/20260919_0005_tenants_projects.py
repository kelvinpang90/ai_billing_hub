"""tenants + projects: identity and ownership columns only

表结构照 `docs/database-schema.md`（spec §75 / §76 的身份与归属字段，外加 §57 的
`description`）。状态、金额、webhook、`backend_base_url` 各列留给后续任务，届时
都是纯新增列。

§132 第 13 条分析
-----------------

**锁表与停机**：两张表全是新建，`CREATE TABLE` 瞬时完成，本版**不 ALTER、不删除
任何已有表**。`projects` 的外键指向同一版新建的 `tenants`，不碰 `users` 等既有表，
所以不在任何有数据的表上取元数据锁。不需要停机。

**备份**：要求前置备份（与 0003 / 0004 同一口径：库里已有真实管理员账号、TOTP
注册、审计与 outbox 记录）。本版本身不改动它们，备份是给「迁移中途出事要人工
清场」兜底。

**部署顺序**：迁移可以先于应用滚动。本任务没有任何代码读写这两张表（没有 API、
没有服务层），旧代码与新代码都不会误读。

**失败处理**：⚠️ MySQL 的 DDL **不参与事务**。`tenants` 建好而 `projects` 失败时，
`tenants` 会留在库里而 `alembic_version` 不前进，重跑会撞 "table already exists"。
处置是人工 `DROP TABLE tenants` 再重跑 —— 所以 `upgrade` 里不做任何数据写入
（比如顺手插一个默认租户），让「删表重跑」始终安全。

**回滚**：`downgrade` 按外键反向 `DROP TABLE`（先 `projects` 后 `tenants`），
**不显式 drop_index**（0002 上实测踩过：首列是外键列的索引，MySQL 拒绝单独
DROP）。此刻两张表没有任何写入方，downgrade 不丢业务数据；⚠️ 一旦 Phase 1 的
客户管理上线，**生产上就不要 downgrade 这一版** —— 它会连同全部租户与项目一起
删掉，而钱包、账本将来都挂在它们上面。

Revision ID: 0005_tenants_projects
Revises: 0004_password_reset_outbox
Create Date: 2026-09-19
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0005_tenants_projects"
down_revision: str | None = "0004_password_reset_outbox"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "tenants",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        # uuid4 字符串。对外只用它，内部 id 不出库（spec §115）。
        sa.Column("public_id", sa.CHAR(length=36), nullable=False),
        sa.Column("company_name", sa.String(length=255), nullable=False),
        sa.Column("contact_name", sa.String(length=255), nullable=True),
        # 联系邮箱，不是登录账号：**刻意不加唯一约束**。
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("phone", sa.String(length=32), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("public_id", name="uq_tenants_public_id"),
    )

    op.create_table(
        "projects",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("public_id", sa.CHAR(length=36), nullable=False),
        sa.Column("tenant_id", sa.BigInteger(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        # spec §57 的管理端字段（docs/database-schema.md 的裁决）。
        sa.Column("description", sa.String(length=1000), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        # ⚠️ RESTRICT：删租户不许级联删掉项目（将来挂着钱包与账本）。
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name="fk_projects_tenant_id", ondelete="RESTRICT"
        ),
        sa.UniqueConstraint("public_id", name="uq_projects_public_id"),
        # 按租户列项目走这条索引。写在 create_table 里，随表一起建、随表一起删。
        sa.Index("ix_projects_tenant_id", "tenant_id"),
    )


def downgrade() -> None:
    # 顺序按外键反向：projects 引用 tenants。
    # 不显式 drop_index —— DROP TABLE 会一并删（见文件顶部）。
    op.drop_table("projects")
    op.drop_table("tenants")

"""Status sync layer 1: project integration columns, tenants.status_changed_at, delivery rows

照设计闸门 #241 v6（docs/design/PHASE-3-status-sync.md）§2「数据库（迁移 0020）」与 §8，拆分第 1 项
（AIH-TASK-047）。只建结构与投递行，不改任何状态跃迁、接口与投递。

步骤
----

1. `projects` 加 `integration_status VARCHAR(16) NOT NULL DEFAULT 'ENABLED'`：服务端默认值给已有行
   回填 `ENABLED`，不另写 UPDATE；再加 `ck_projects_integration_status`（`ENABLED` / `DISABLED`）
2. `projects` 加 `status_webhook_url VARCHAR(2048) NULL`：已有行为空
3. `tenants` 加 `status_changed_at DATETIME NULL`：旧行保持空，读取时回退到 `created_at`
4. `create_table project_status_deliveries`：主键 `project_id`；复合外键 `(project_id, tenant_id)` →
   `projects(id, tenant_id)` `ON DELETE RESTRICT`（被引用的 `uq_projects_id_tenant` 是 0007 建的）；
   两条 CHECK（`attempt_count >= 0`、`delivered_version` 为空或 ≥ 0）；索引 `(next_attempt_at)`
5. 给每个已有项目插入一行投递行：`delivered_version` 为空、`attempt_count` 0、`next_attempt_at` 取
   迁移时刻（`UTC_TIMESTAMP()`，整秒）。从此每个项目恰好一行；以后建项目时在同一事务里插入

§132 第 13 条分析
-----------------

**锁表与停机**：ALTER `projects`（两列、一条 CHECK）与 `tenants`（一列），生产上各是个位数行。
MySQL 8.4 给表尾加列走 INSTANT，不重建表；加 CHECK 要校验已有行，行数个位数，瞬时完成。
新表的外键对 `projects` 只取元数据锁。回填只读 `projects`、只写新表。不碰钱包、账本与用量事件。
不需要停机。

**备份**：要求前置备份（与 0005–0019 同一口径）。本版不改动任何已有列的数据：两列取默认值 / 空，
新表是新插入的行。

**部署顺序**：`deploy.sh` 先迁移、再换镜像。旧代码不认识新列与新表、也不写它们（服务端默认值兜底），
但旧代码建的项目**没有**投递行 —— 迁移与换镜像之间若有人建项目，那个项目会缺一行（见下面「失败处理」
最后一条的补行语句）。新代码建项目时插入投递行，依赖新表，所以迁移必须先于代码；迁移失败时容器启动
失败，旧容器继续服务。部署后没有任何项目配置地址，不会发出任何请求（投递归后续任务）。

**失败处理**：⚠️ MySQL 的 DDL **不参与事务**。
- 发出的依次是：`ALTER TABLE projects` 加两列、加 CHECK，`ALTER TABLE tenants` 加列，`CREATE TABLE`
  （列、CHECK、外键），`CREATE INDEX`，最后一条回填 `INSERT … SELECT`。
- 第一条就失败：什么都没留下、`alembic_version` 仍是 `0019_internal_metering`，修好后重新部署即可。
- 中途失败或进程被杀：已建的部分留着而 `alembic_version` 没有前进，重跑会撞 "Duplicate column name"
  或 "Table already exists"。确认 `alembic_version` 仍是 `0019_internal_metering` 后按存在与否清场：
  `DROP TABLE project_status_deliveries`；
  `ALTER TABLE projects DROP CHECK ck_projects_integration_status`；
  `ALTER TABLE projects DROP COLUMN status_webhook_url, DROP COLUMN integration_status`；
  `ALTER TABLE tenants DROP COLUMN status_changed_at`，再重新部署。清场前这些列与表里除默认值
  和回填外没有别的数据，所以「清场重跑」始终安全。
- 迁移成功后若发现有项目缺投递行（上面部署窗口里旧代码建的）：
  `INSERT INTO project_status_deliveries (project_id, tenant_id, attempt_count, next_attempt_at)
  SELECT p.id, p.tenant_id, 0, UTC_TIMESTAMP() FROM projects p LEFT JOIN project_status_deliveries d
  ON d.project_id = p.id WHERE d.project_id IS NULL`。

**回滚**：生产上只前滚；关掉推送的办法是清掉项目的地址（设计 §8）。`downgrade` 照 0019 的写法，只在
不会丢数据时执行：有项目是 `DISABLED`、有项目设了地址、有投递行有 `delivered_version`、有租户有
`status_changed_at`，任何一条成立就抛错拒绝。否则删新表、删 CHECK（MySQL 不许删掉 CHECK 还在引用的
列）、删三列。`--sql` 离线模式读不了数据，不做这项检查。

Revision ID: 0020_status_sync
Revises: 0019_internal_metering
Create Date: 2026-10-09
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0020_status_sync"
down_revision: str | None = "0019_internal_metering"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PROJECTS = "projects"
_TENANTS = "tenants"
_DELIVERIES = "project_status_deliveries"
_DEFAULT = "ENABLED"
_NEXT_ATTEMPT_INDEX = "ix_project_status_deliveries_next_attempt_at"

# 名字 → 条件。⚠️ 冻结的字面量：与 app/models/tenancy.py、app/models/integration.py 上的
# CheckConstraint 一致（空白除外），tests/backend/test_migrations.py 比对两边。
_PROJECT_CHECKS: dict[str, str] = {
    "ck_projects_integration_status": "integration_status IN ('ENABLED', 'DISABLED')",
}
_DELIVERY_CHECKS: dict[str, str] = {
    "ck_project_status_deliveries_attempt_count": "attempt_count >= 0",
    "ck_project_status_deliveries_delivered_version": (
        "delivered_version IS NULL OR delivered_version >= 0"
    ),
}

# 第 5 步。`UTC_TIMESTAMP()` 不带小数秒，与服务端其余时间同为整秒 UTC（设计 §2「时间」）。
_BACKFILL = """
INSERT INTO project_status_deliveries (project_id, tenant_id, attempt_count, next_attempt_at)
SELECT id, tenant_id, 0, UTC_TIMESTAMP()
  FROM projects
"""

# downgrade 会丢掉的数据（设计 §8）：任何一条查到行就拒绝降级。
_LOSSY = (
    "SELECT 1 FROM projects WHERE integration_status <> 'ENABLED' LIMIT 1",
    "SELECT 1 FROM projects WHERE status_webhook_url IS NOT NULL LIMIT 1",
    "SELECT 1 FROM project_status_deliveries WHERE delivered_version IS NOT NULL LIMIT 1",
    "SELECT 1 FROM tenants WHERE status_changed_at IS NOT NULL LIMIT 1",
)


def upgrade() -> None:
    op.add_column(
        _PROJECTS,
        sa.Column(
            "integration_status", sa.String(length=16), nullable=False, server_default=_DEFAULT
        ),
    )
    op.add_column(_PROJECTS, sa.Column("status_webhook_url", sa.String(length=2048), nullable=True))
    for name, condition in _PROJECT_CHECKS.items():
        op.create_check_constraint(name, _PROJECTS, condition)
    op.add_column(_TENANTS, sa.Column("status_changed_at", sa.DateTime(), nullable=True))
    op.create_table(
        _DELIVERIES,
        sa.Column("project_id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("tenant_id", sa.BigInteger(), nullable=False),
        # 已确认送达的最大版本；空 = 从未送达。
        sa.Column("delivered_version", sa.BigInteger(), nullable=True),
        # 当前租约正在发送的版本。
        sa.Column("attempted_version", sa.BigInteger(), nullable=True),
        # 自上次成功以来的连续失败次数。
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.DateTime(), nullable=False),
        # 领取时写入的 uuid4，结算时作围栏。
        sa.Column("lease_token", sa.CHAR(length=36), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(), nullable=True),
        sa.Column("last_attempt_at", sa.DateTime(), nullable=True),
        sa.Column("delivered_at", sa.DateTime(), nullable=True),
        sa.Column("last_http_status", sa.SmallInteger(), nullable=True),
        # 只存错误码，不存地址、响应体与异常文本（设计 §6）。
        sa.Column("last_error_code", sa.String(length=64), nullable=True),
        sa.PrimaryKeyConstraint("project_id"),
        # 复合外键：投递行的租户必须是项目所属的租户（INV-8）。RESTRICT：行与项目都不删除。
        sa.ForeignKeyConstraint(
            ["project_id", "tenant_id"],
            ["projects.id", "projects.tenant_id"],
            name="fk_project_status_deliveries_project",
            ondelete="RESTRICT",
        ),
        *(sa.CheckConstraint(condition, name=name) for name, condition in _DELIVERY_CHECKS.items()),
    )
    op.create_index(_NEXT_ATTEMPT_INDEX, _DELIVERIES, ["next_attempt_at"])
    op.execute(_BACKFILL)


def downgrade() -> None:
    if not op.get_context().as_sql:
        bind = op.get_bind()
        if any(bind.execute(sa.text(query)).first() for query in _LOSSY):
            raise RuntimeError(
                "Cannot downgrade status sync after a project was disabled, a status webhook"
                " URL was set, a status was delivered or a tenant status transition was stamped"
            )
    op.drop_table(_DELIVERIES)
    # 先删 CHECK 再删列：MySQL 不许删掉 CHECK 还在引用的列。
    for name in _PROJECT_CHECKS:
        op.execute(f"ALTER TABLE {_PROJECTS} DROP CHECK {name}")
    op.drop_column(_PROJECTS, "status_webhook_url")
    op.drop_column(_PROJECTS, "integration_status")
    op.drop_column(_TENANTS, "status_changed_at")

"""audit_logs query indexes for the admin audit log API

照 AIH-TASK-022：`GET /api/v1/admin/audit-logs`（spec §89）按动作、实体、操作者与时间段
筛选，按 `id` 倒序分页。原先只有 `ix_audit_logs_actor_created (actor_user_id, created_at)`
（操作者筛选用它），这里补另外三个：

- `ix_audit_logs_entity (entity_type, entity_id, id)`：按实体筛选，末尾的 `id` 让倒序分页直接
  沿索引读；
- `ix_audit_logs_action (action, id)`：按动作筛选，同上；
- `ix_audit_logs_created_at (created_at)`：时间段筛选。

`app/models/auth.py` 的 `AuditLog.__table_args__` 同步声明了这三个，
`tests/backend/test_migrations.py` 比对两边一致。

§132 第 13 条分析
-----------------

**已有数据**：不改列、不回填、不改任何一行。建索引只读已有行。

**锁表与停机**：MySQL 8 的 InnoDB 加二级索引默认就是在线 DDL（`ALGORITHM=INPLACE`、
`LOCK=NONE`）：建索引期间 `audit_logs` 照常可读可写，只在开始与结束时各短暂拿一次元数据锁
（等正在进行的、碰过这张表的事务结束）。生产上 `audit_logs` 行数少（Phase 1，只有管理员
操作与登录的审计），每个索引秒级完成。不需要停机。0010 的两个触发器不受影响。

**备份**：要求前置备份（与 0005–0010 同一口径）。本版不写任何数据。

**部署顺序**：`deploy.sh` 先迁移、再换镜像。新代码的查询在没有这三个索引时照样正确，只是
慢；旧代码不读这三个索引。先迁移后换镜像、或反过来都安全。

**失败处理**：⚠️ MySQL 的 DDL **不参与事务**。
- 第 1 个索引失败：什么都没建，修好后重新部署。
- 第 2 或第 3 个失败，或进程在中途被杀：前面的索引已存在而 `alembic_version` 仍是
  `0010_audit_logs_append_only`，重跑会撞 "Duplicate key name"。确认版本仍是 0010 后，
  `DROP INDEX` 掉已建的那几个（名字见下面的 `_INDEXES`），再重新部署。
- 本版不写数据，所以「清场重跑」始终安全。

**回滚**：`downgrade` 反向删掉这三个索引，表、数据与触发器都不动；查询接口照常工作，只是
变慢。

Revision ID: 0011_audit_logs_query_indexes
Revises: 0010_audit_logs_append_only
Create Date: 2026-09-28
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0011_audit_logs_query_indexes"
down_revision: str | None = "0010_audit_logs_append_only"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "audit_logs"

# 索引名 → 列。冻结的字面量；模型那边的声明由 test_migrations.py 比对。
_INDEXES: dict[str, list[str]] = {
    "ix_audit_logs_entity": ["entity_type", "entity_id", "id"],
    "ix_audit_logs_action": ["action", "id"],
    "ix_audit_logs_created_at": ["created_at"],
}


def upgrade() -> None:
    for name, columns in _INDEXES.items():
        op.create_index(name, _TABLE, columns)


def downgrade() -> None:
    # 反向删。表、数据与触发器不动。
    for name in reversed(_INDEXES):
        op.drop_index(name, table_name=_TABLE)

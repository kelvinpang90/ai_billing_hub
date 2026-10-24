"""usage_events indexes for the customer usage page by date and by project

照设计闸门 #398 v4（docs/design/AIH-TASK-103-customer-usage.md）§2「数据库」与 §8 第 1 步；
本迁移是 §8 的 103。只加两个非唯一索引，都与客户用量明细的 `ORDER BY occurred_at DESC,
public_id DESC` 一致，键集分页倒序取 `page_size + 1` 行即停（AIH-TASK-104 的查询）：

- `ix_usage_events_tenant_occurred (tenant_id, occurred_at, public_id)`：只带日期区间（以及只带
  供应商 / 模型）时用；
- `ix_usage_events_project_occurred (project_id, occurred_at, public_id)`：带项目时用。

既有索引一个都不动。`app/models/usage.py` 的 `UsageEvent.__table_args__` 同步声明了这两个索引，
`tests/backend/test_usage_customer_index_migration.py` 比对两边一致。

步骤
----

1. 用 inspector 读 `usage_events` 现有的索引，两个目标名各判一次：不存在 → 待建；存在、非唯一、
   列恰好按序是上面的三列且没有前缀长度 → 已建好，跳过；其余（列或唯一性不同、带前缀长度）→
   形状不对。有任何一个形状不对就抛错，**不发任何 DDL**。
2. MySQL：待建的（0、1 或 2 个）合成**一条** `ALTER TABLE usage_events ADD INDEX …[, ADD INDEX …]`；
   一个都不缺时不发 DDL。SQLite：逐个 `create_index`（DDL 在 Alembic 的迁移事务里）。

§132 第 13 条分析
-----------------

**已有数据**：不改列、不改约束、不改触发器、不回填、不改任何一行。建索引只读已有行。

**锁表与停机**：MySQL 8 的 InnoDB 加二级索引默认就是在线 DDL（`ALGORITHM=INPLACE`、
`LOCK=NONE`）：建索引期间 `usage_events` 照常可读可写，摄取与计费不被阻塞，只在开始与结束时各
短暂拿一次元数据锁（等正在进行的、碰过这张表的事务结束）。生产上 `usage_events` 现在只有夹具与
内部租户的少量行，构建瞬时完成（设计 §8）。不需要停机。写入代价：每条事件插入时多维护两个二级
索引条目；这些列插入后不再更新，计费 worker 的状态更新不碰它们。

**备份**：要求前置备份（与 0005–0025 同一口径）。本版不写任何数据。

**部署顺序**：`deploy.sh` 先迁移、再换镜像。本任务不带查询与接口（AIH-TASK-104），部署后只多
两个没人用的索引；旧代码不读它们。先迁移后换镜像、或反过来都安全。

**失败处理**：⚠️ MySQL 的 DDL **不参与事务**，`ALTER TABLE` 与写 `alembic_version` 之间有失败
窗口（与 0023 相同）。两个索引在**一条** `ALTER TABLE` 里加，MySQL 8 的 InnoDB DDL 是原子的，
加上第 1 步的检查，三个窗口都有确定的结局（设计 §2「数据库」第 3 条）：

- `ALTER TABLE` 失败（锁等待超时、进程被杀）→ 整条回滚，两个索引都不存在，`alembic_version`
  仍是 `0025_customer_portal_accounts` → 原样重新部署；
- `ALTER TABLE` 成功、写 `alembic_version` 之前进程退出 → 两个索引都在，版本仍是 0025 →
  原样重新部署：第 1 步判两个都已建好，不发 DDL，只把版本前进；
- 有人手工建过同名但形状不同的索引 → 重跑在第 1 步失败，报错写明是哪个索引，不发 DDL，版本仍是
  0025 → 按报错 `DROP INDEX <那个索引> ON usage_events` 后重新部署。

`--sql` 离线模式读不了库：跳过检查，按两个都缺输出 DDL（downgrade 按两个都在）。

**回滚**：`downgrade` 只删这两个索引里存在的那几个（MySQL 上一条
`ALTER TABLE usage_events DROP INDEX …[, DROP INDEX …]`，都不存在时不发 DDL），同样可以重跑；
表、列、约束、其他索引、数据与触发器都不动，没有写入方依赖它们。

Revision ID: 0026_usage_customer_indexes
Revises: 0025_customer_portal_accounts
Create Date: 2026-10-23
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0026_usage_customer_indexes"
down_revision: str | None = "0025_customer_portal_accounts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "usage_events"

# 名字 → 列，按建的顺序。⚠️ 冻结的字面量；模型那边的声明由
# tests/backend/test_usage_customer_index_migration.py 比对。都非唯一。
_INDEXES: dict[str, list[str]] = {
    "ix_usage_events_tenant_occurred": ["tenant_id", "occurred_at", "public_id"],
    "ix_usage_events_project_occurred": ["project_id", "occurred_at", "public_id"],
}


def _existing() -> dict[str, dict]:
    """`usage_events` 上与目标同名的索引（inspector 的原样字典）；离线模式读不了库，返回空。"""
    if op.get_context().as_sql:
        return {}
    indexes = sa.inspect(op.get_bind()).get_indexes(_TABLE)
    return {str(index["name"]): dict(index) for index in indexes if index["name"] in _INDEXES}


def _has_design_shape(name: str, index: dict) -> bool:
    """非唯一、列恰好按序是设计的三列、任何一列都没有前缀长度（例如 `public_id(8)`：inspector 的
    `column_names` 相同，前缀长度只在 `dialect_options` 的 `mysql_length` 里）。"""
    if index.get("unique"):
        return False
    if list(index.get("column_names") or []) != _INDEXES[name]:
        return False
    return not (index.get("dialect_options") or {}).get("mysql_length")


def upgrade() -> None:
    existing = _existing()
    # ⚠️ 先判完两个、再发 DDL：任何一个形状不对都不动库，版本也不前进。
    for name, index in existing.items():
        if not _has_design_shape(name, index):
            raise RuntimeError(
                f"Index {name} on {_TABLE} exists but is not ({', '.join(_INDEXES[name])})"
                f" non-unique without prefix lengths; no DDL was issued."
                f" Run DROP INDEX {name} ON {_TABLE}, then upgrade again"
            )
    missing = [name for name in _INDEXES if name not in existing]
    if not missing:
        return

    if op.get_context().dialect.name == "mysql":
        # 一条原子的 ALTER TABLE：失败时整条回滚，不会只建成其中一个。
        clauses = [f"ADD INDEX {name} ({', '.join(_INDEXES[name])})" for name in missing]
        op.execute(f"ALTER TABLE {_TABLE} " + ", ".join(clauses))
    else:
        for name in missing:
            op.create_index(name, _TABLE, _INDEXES[name], unique=False)


def downgrade() -> None:
    # 只删存在的，倒序；离线模式按两个都在输出。表、数据与触发器不动。
    if op.get_context().as_sql:
        present = list(reversed(_INDEXES))
    else:
        existing = _existing()
        present = [name for name in reversed(_INDEXES) if name in existing]
    if not present:
        return

    if op.get_context().dialect.name == "mysql":
        clauses = [f"DROP INDEX {name}" for name in present]
        op.execute(f"ALTER TABLE {_TABLE} " + ", ".join(clauses))
    else:
        for name in present:
            op.drop_index(name, table_name=_TABLE)

"""wallet_transactions index for the customer export by booking date

照设计闸门 #435 v10（docs/design/AIH-TASK-113-customer-export.md）§2「数据库」与 §8 第 1 步；
本迁移是 AIH-TASK-113。只加一个非唯一索引：

- `ix_wallet_transactions_tenant_created (tenant_id, created_at, wallet_sequence)`：
  客户按马来西亚日期区间导出钱包流水时，
  `WHERE tenant_id = ? AND created_at >= ? AND created_at < ?` 走它做范围扫描，
  不必扫这个租户从开户起的全部流水（导出接口属于 AIH-TASK-116，本任务不带查询）。

既有索引一个都不动。`app/models/wallet.py` 的 `WalletTransaction.__table_args__` 同步声明了这个
索引，`tests/backend/test_wallet_created_index_migration.py` 比对两边一致。

步骤
----

1. 用 inspector 读 `wallet_transactions` 现有的索引，目标名判一次：不存在 → 待建；存在、非唯一、
   列恰好按序是上面的三列且没有前缀长度 → 已建好，跳过；其余（列或唯一性不同、带前缀长度）→
   形状不对，抛错，**不发任何 DDL**。
2. MySQL：缺时发**一条** `ALTER TABLE wallet_transactions ADD INDEX …`；已建好时不发 DDL。
   SQLite：`create_index`（DDL 在 Alembic 的迁移事务里）。

§132 第 13 条分析
-----------------

**已有数据**：不改列、不改约束、不改触发器、不回填、不改任何一行。建索引只读已有行，所以
`wallet_transactions` 的四个触发器（只许插入、插入时校验并推进钱包）与 `wallets` 的两个触发器
都不会被触发，也不被改动；余额与账本都不变（INV-4、INV-5）。

**锁表与停机**：MySQL 8 的 InnoDB 加二级索引默认就是在线 DDL（`ALGORITHM=INPLACE`、
`LOCK=NONE`）：建索引期间 `wallet_transactions` 照常可读可写，记账不被阻塞，只在开始与结束时各
短暂拿一次元数据锁（等正在进行的、碰过这张表的事务结束），与 0023、0026 相同。
生产上流水行数很少，构建瞬时完成（设计 §2「数据库」）。不需要停机。
写入代价：每条流水插入时多维护一个二级索引条目；`created_at` 插入后不再更新（流水不可变）。

**备份**：要求前置备份（与 0005–0026 同一口径）。本版不写任何数据。

**部署顺序**：`deploy.sh` 先迁移、再换镜像。本任务不带查询与接口，部署后只多一个
没人用的索引；旧代码不读它。先迁移后换镜像、或反过来都安全。

**失败处理**：⚠️ MySQL 的 DDL **不参与事务**，`ALTER TABLE` 与写 `alembic_version` 之间有失败
窗口（与 0023、0026 相同）。索引在**一条** `ALTER TABLE` 里加，MySQL 8 的 InnoDB DDL 是原子的，
加上第 1 步的检查，三个窗口都有确定的结局（设计 §5 迁移一行）：

- `ALTER TABLE` 失败（锁等待超时、进程被杀）→ 整条回滚，索引不存在，`alembic_version` 仍是
  `0026_usage_customer_indexes` → 原样重新部署；
- `ALTER TABLE` 成功、写 `alembic_version` 之前进程退出 → 索引在，版本仍是 0026 →
  原样重新部署：第 1 步判它已建好，不发 DDL，只把版本前进；
- 有人手工建过同名但形状不同的索引 → 重跑在第 1 步失败，报错写明是哪个索引，不发 DDL，版本仍是
  0026 → 按报错 `DROP INDEX ix_wallet_transactions_tenant_created ON wallet_transactions` 后重新
  部署。

`--sql` 离线模式读不了库：跳过检查，按缺输出 DDL（downgrade 按存在）。

**回滚**：`downgrade` 只在这个索引存在时删（MySQL 上一条
`ALTER TABLE wallet_transactions DROP INDEX …`，不存在时不发 DDL），同样可以重跑；表、列、约束、
其他索引、数据与触发器都不动，没有写入方依赖它。

Revision ID: 0027_wallet_tx_created_index
Revises: 0026_usage_customer_indexes
Create Date: 2026-10-26
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0027_wallet_tx_created_index"
down_revision: str | None = "0026_usage_customer_indexes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "wallet_transactions"

# ⚠️ 冻结的字面量；非唯一。模型那边的声明由
# tests/backend/test_wallet_created_index_migration.py 比对。
_INDEX = "ix_wallet_transactions_tenant_created"
_COLUMNS = ["tenant_id", "created_at", "wallet_sequence"]


def _existing() -> dict | None:
    """与目标同名的索引（inspector 的原样字典）；没有或离线模式读不了库时返回 None。"""
    if op.get_context().as_sql:
        return None
    for index in sa.inspect(op.get_bind()).get_indexes(_TABLE):
        if index["name"] == _INDEX:
            return dict(index)
    return None


def _has_design_shape(index: dict) -> bool:
    """非唯一、列恰好按序是设计的三列、任何一列都没有前缀长度
    （前缀长度只在 `dialect_options` 的 `mysql_length` 里，`column_names` 看不出来）。"""
    if index.get("unique"):
        return False
    if list(index.get("column_names") or []) != _COLUMNS:
        return False
    return not (index.get("dialect_options") or {}).get("mysql_length")


def upgrade() -> None:
    existing = _existing()
    # ⚠️ 先判、再发 DDL：形状不对时不动库，版本也不前进。
    if existing is not None:
        if not _has_design_shape(existing):
            raise RuntimeError(
                f"Index {_INDEX} on {_TABLE} exists but is not ({', '.join(_COLUMNS)})"
                f" non-unique without prefix lengths; no DDL was issued."
                f" Run DROP INDEX {_INDEX} ON {_TABLE}, then upgrade again"
            )
        return

    if op.get_context().dialect.name == "mysql":
        op.execute(f"ALTER TABLE {_TABLE} ADD INDEX {_INDEX} ({', '.join(_COLUMNS)})")
    else:
        op.create_index(_INDEX, _TABLE, _COLUMNS, unique=False)


def downgrade() -> None:
    # 只在存在时删；离线模式按存在输出。表、数据与触发器不动。
    if not op.get_context().as_sql and _existing() is None:
        return

    if op.get_context().dialect.name == "mysql":
        op.execute(f"ALTER TABLE {_TABLE} DROP INDEX {_INDEX}")
    else:
        op.drop_index(_INDEX, table_name=_TABLE)

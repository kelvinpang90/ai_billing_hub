"""wallet_transactions index for paging the ledger by source

照设计闸门 #351 v1（docs/design/AIH-TASK-083-wallet-ledger-view.md）§2「数据库」与 §8 第 1 步；
本迁移是 §8 的 083。只加一个非唯一索引
`ix_wallet_transactions_tenant_reference_sequence (tenant_id, reference_type, wallet_sequence)`：
管理端按来源（`reference_type`）筛选流水时，等值前缀 `(tenant_id, reference_type)` 加有序的
`wallet_sequence`，键集分页倒序取 `page_size + 1` 行即停（AIH-TASK-084 的查询）。不筛选时仍走
原有的 `ix_wallet_transactions_tenant_sequence`，它不动。

`app/models/wallet.py` 的 `WalletTransaction.__table_args__` 同步声明了这个索引，
`tests/backend/test_migrations.py` 比对两边一致。

步骤
----

1. `create_index ix_wallet_transactions_tenant_reference_sequence`

§132 第 13 条分析
-----------------

**已有数据**：不改列、不改约束、不改触发器、不回填、不改任何一行。建索引只读已有行。

**锁表与停机**：MySQL 8 的 InnoDB 加二级索引默认就是在线 DDL（`ALGORITHM=INPLACE`、
`LOCK=NONE`）：建索引期间 `wallet_transactions` 照常可读可写，记账（0006 的触发器）不被阻塞，
只在开始与结束时各短暂拿一次元数据锁（等正在进行的、碰过这张表的事务结束）。生产上
`wallet_transactions` 现在只有夹具与内部租户的少量行，构建瞬时完成（设计 §8）。不需要停机。

**备份**：要求前置备份（与 0005–0022 同一口径）。本版不写任何数据。

**部署顺序**：`deploy.sh` 先迁移、再换镜像。本任务不带查询与接口（AIH-TASK-084），部署后只多
一个没人用的索引；旧代码不读它。先迁移后换镜像、或反过来都安全。

**失败处理**：⚠️ MySQL 的 DDL **不参与事务**，但这里只有一条 DDL：失败即索引不存在、
`alembic_version` 仍是 `0022_tax_policy`，修好后重新部署。进程恰好在索引建成之后、版本前进
之前被杀时，重跑会撞 "Duplicate key name"：确认版本仍是 0022 后
`DROP INDEX ix_wallet_transactions_tenant_reference_sequence ON wallet_transactions`，再重新部署。
本版不写数据，所以「清场重跑」始终安全。

**回滚**：`downgrade` 只删这个索引，表、列、约束、数据与触发器都不动；没有写入方依赖它。

Revision ID: 0023_wallet_ledger_reference_index
Revises: 0022_tax_policy
Create Date: 2026-10-18
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0023_wallet_ledger_reference_index"
down_revision: str | None = "0022_tax_policy"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "wallet_transactions"

# 冻结的字面量；模型那边的声明由 test_migrations.py 比对。非唯一。
_INDEX = "ix_wallet_transactions_tenant_reference_sequence"
_COLUMNS = ["tenant_id", "reference_type", "wallet_sequence"]


def upgrade() -> None:
    op.create_index(_INDEX, _TABLE, _COLUMNS, unique=False)


def downgrade() -> None:
    # 只删这一个。表、数据与触发器不动。
    op.drop_index(_INDEX, table_name=_TABLE)

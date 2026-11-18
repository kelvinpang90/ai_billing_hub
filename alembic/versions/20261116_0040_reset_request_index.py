"""password_reset_tokens index for the per-account reset request window

照设计闸门 #751 v3（docs/design/AIH-TASK-231-auth-hardening.md）§2「数据库（迁移 0040，I1）」与
§8「迁移 0040」；本迁移是设计 §11 的 I1。只加一个非唯一索引
`ix_password_reset_tokens_user_purpose_created (user_id, purpose, created_at)`：忘记密码按账号
限频（设计 §2「限频规则」，I4）的窗口查询（READ COMMITTED 下的普通读，不加锁）
按等值前缀 `(user_id, purpose)` 加 `created_at` 的范围扫描，只读窗口内的行（正常 0–1 行），
不受该账号历史令牌与邀请令牌数量影响。原有的
`ix_password_reset_tokens_user (user_id, used_at)`（0004 建，`user_id` 外键在 MySQL 上依赖它）
与 `token_hash` 的唯一索引都不动。

`app/models/auth.py` 的 `PasswordResetToken.__table_args__` 同步声明了这个索引，
`tests/backend/test_migrations.py` 比对两边一致。

步骤
----

1. `create_index ix_password_reset_tokens_user_purpose_created`

§132 第 13 条分析
-----------------

**已有数据**：不改列、不改约束、不改触发器、不回填、不改任何一行。建索引只读已有行。

**锁表与停机**：MySQL 8.4 的 InnoDB 加二级索引默认就是在线 DDL（`ALGORITHM=INPLACE`、
`LOCK=NONE`）：建索引期间 `password_reset_tokens` 照常可读可写，忘记密码与邀请照常签发，
只在开始与结束时各短暂拿一次元数据锁（等正在进行的、碰过这张表的事务结束）。生产上这张表
行数少，构建瞬时完成。不需要停机。

**备份**：要求前置备份（与 0005–0039 同一口径）。本版不写任何数据。

**部署顺序**：`deploy.sh` 先迁移、再换镜像。本任务不带查询（限频是设计 #751 的 I4，人工 PR），
部署后只多一个没人用的索引；旧代码不读它。本迁移须先于 I4 部署（设计 §8「部署顺序」第 1 步）。
L 批的 0041 以 `0040_reset_request_index` 为 `down_revision`。

**失败处理**：⚠️ MySQL 的 DDL **不参与事务**，但这里只有一条 DDL，没有半成品：建索引失败即
索引不存在、`alembic_version` 仍是 `0039_payment_receipts`，修好后重新部署。索引建成之后、版本
前进之前出事（进程被杀，或写 `alembic_version` 本身失败）时，索引已留下而版本仍是 0039，重跑会撞
"Duplicate key name"：确认版本仍是 0039 后
`DROP INDEX ix_password_reset_tokens_user_purpose_created ON password_reset_tokens`，再重新部署。
本版不写数据，所以「清场重跑」始终安全。

**回滚**：`downgrade` 只删这个索引，表、列、约束、数据都不动。按设计 §8「回滚」，回滚应用镜像时
不需要降级本版（旧代码不读这个索引）。

Revision ID: 0040_reset_request_index
Revises: 0039_payment_receipts
Create Date: 2026-11-16
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# `alembic_version.version_num` 是 VARCHAR(32)，ID 必须不超过 32 个字符（设计 §2「数据库」）。
revision: str = "0040_reset_request_index"
down_revision: str | None = "0039_payment_receipts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "password_reset_tokens"

# 冻结的字面量；模型那边的声明由 test_migrations.py 比对。非唯一。
_INDEX = "ix_password_reset_tokens_user_purpose_created"
_COLUMNS = ["user_id", "purpose", "created_at"]


def upgrade() -> None:
    op.create_index(_INDEX, _TABLE, _COLUMNS, unique=False)


def downgrade() -> None:
    # 只删这一个。表、列、约束与数据不动。
    op.drop_index(_INDEX, table_name=_TABLE)

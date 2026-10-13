"""AI module layer 1: client keys, local usage outbox and usage feature label.

照设计闸门 #281 v23（docs/design/AI-MODULE.md）§2「数据库」与 §8，拆分第 1 项
（AIH-TASK-056）。只建结构；不写服务函数、接口、请求路径、投递或审计动作。

步骤
----

1. `create_table ai_client_keys`：项目 / 租户复合外键、client_id 唯一、状态与吊销时刻的 CHECK，
   以及项目索引；行以吊销代替删除。
2. `create_table ai_usage_outbox`：项目 / 租户复合外键、event_id 唯一、状态与 attempts 的 CHECK，
   以及投递扫描索引；行不删除。
3. `usage_events` 加可空 `feature VARCHAR(64)`，并建按租户、功能、发生时刻的索引。

§132 第 13 条分析
-----------------

**锁表与停机**：两张新表只对 `projects` 取元数据锁。`usage_events` 只在表尾增加可空列，
MySQL 8.4 走 INSTANT；新增索引走 INPLACE，不锁写。生产表与项目表均很小，不需要停机。

**备份**：要求前置备份（与 0005–0020 同一口径）。本版不改已有行；新增 `feature` 对既有
事件为 NULL，新表为空。

**部署顺序**：先迁移再换镜像。旧代码不读写新增结构；本任务也没有新写入方，所以迁移与
换镜像之间没有业务语义窗口。后续任务依赖这些表和列，必须在本迁移之后部署。

**失败处理**：MySQL DDL 不参与事务。第一条失败不留东西；中途失败而版本仍为
`0020_status_sync` 时，确认表 / 列存在情况后依次删除 `ai_usage_outbox`、`ai_client_keys`、
`ix_usage_events_tenant_feature_occurred` 与 `usage_events.feature`，再重跑。此时没有写入方，
清场不丢业务数据。

**回滚**：生产只前滚。downgrade 仅在两张新表为空且所有 `usage_events.feature` 都是 NULL 时
执行，否则会丢失 AI 密钥、outbox 或功能标签，故明确拒绝；`--sql` 离线模式无法读数据，跳过
该预检并只输出 DDL。

Revision ID: 0021_ai_module
Revises: 0020_status_sync
Create Date: 2026-10-13
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import mysql

from alembic import op

revision: str = "0021_ai_module"
down_revision: str | None = "0020_status_sync"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CLIENT_KEYS = "ai_client_keys"
_OUTBOX = "ai_usage_outbox"
_USAGE_EVENTS = "usage_events"
_FEATURE_INDEX = "ix_usage_events_tenant_feature_occurred"
_CLIENT_KEY_CHECKS = {
    "ck_ai_client_keys_status": "status IN ('ACTIVE', 'REVOKED')",
    "ck_ai_client_keys_revoked_at": "(status = 'REVOKED') = (revoked_at IS NOT NULL)",
}
_OUTBOX_CHECKS = {
    "ck_ai_usage_outbox_status": (
        "status IN ('PENDING', 'SENDING', 'DELIVERED', 'DEAD', 'HELD', 'DISCARDED')"
    ),
    "ck_ai_usage_outbox_attempts": "attempts >= 0",
}
_LOSSY = (
    "SELECT 1 FROM ai_client_keys LIMIT 1",
    "SELECT 1 FROM ai_usage_outbox LIMIT 1",
    "SELECT 1 FROM usage_events WHERE feature IS NOT NULL LIMIT 1",
)


def _ascii(length: int) -> sa.CHAR:
    return sa.CHAR(length=length).with_variant(
        mysql.CHAR(length=length, collation="ascii_bin"), "mysql"
    )


def _text(length: int) -> sa.String:
    return sa.String(length=length).with_variant(
        mysql.VARCHAR(length=length, collation="utf8mb4_0900_bin"), "mysql"
    )


def _datetime6() -> sa.DateTime:
    return sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql")


def upgrade() -> None:
    op.create_table(
        _CLIENT_KEYS,
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("tenant_id", sa.BigInteger(), nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("client_id", _text(40), nullable=False),
        sa.Column("token_sha256", _ascii(64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["project_id", "tenant_id"],
            ["projects.id", "projects.tenant_id"],
            name="fk_ai_client_keys_project",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("client_id", name="uq_ai_client_keys_client_id"),
        *(
            sa.CheckConstraint(condition, name=name)
            for name, condition in _CLIENT_KEY_CHECKS.items()
        ),
    )
    op.create_index("ix_ai_client_keys_project_id", _CLIENT_KEYS, ["project_id"])
    op.create_table(
        _OUTBOX,
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("event_id", _ascii(26), nullable=False),
        sa.Column("request_id", _ascii(26), nullable=False),
        sa.Column("claim_id", _ascii(26), nullable=True),
        sa.Column("tenant_id", sa.BigInteger(), nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("feature", _text(64), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", _datetime6(), nullable=False),
        sa.Column("last_error", sa.String(length=64), nullable=True),
        sa.Column("first_attempted_at", _datetime6(), nullable=True),
        sa.Column("created_at", _datetime6(), nullable=False),
        sa.Column("delivered_at", _datetime6(), nullable=True),
        sa.Column("resolved_at", _datetime6(), nullable=True),
        sa.Column("review_required_at", _datetime6(), nullable=True),
        sa.Column("reviewed_at", _datetime6(), nullable=True),
        sa.Column("dead_acked_at", _datetime6(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["project_id", "tenant_id"],
            ["projects.id", "projects.tenant_id"],
            name="fk_ai_usage_outbox_project",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("event_id", name="uq_ai_usage_outbox_event_id"),
        *(sa.CheckConstraint(condition, name=name) for name, condition in _OUTBOX_CHECKS.items()),
    )
    op.create_index(
        "ix_ai_usage_outbox_status_next_attempt_at", _OUTBOX, ["status", "next_attempt_at"]
    )
    op.add_column(_USAGE_EVENTS, sa.Column("feature", _text(64), nullable=True))
    op.create_index(_FEATURE_INDEX, _USAGE_EVENTS, ["tenant_id", "feature", "occurred_at"])


def downgrade() -> None:
    if not op.get_context().as_sql:
        bind = op.get_bind()
        if any(bind.execute(sa.text(query)).first() for query in _LOSSY):
            raise RuntimeError(
                "Cannot downgrade AI module after an AI client key, AI usage outbox row or usage"
                " feature was written"
            )
    op.drop_index(_FEATURE_INDEX, table_name=_USAGE_EVENTS)
    op.drop_column(_USAGE_EVENTS, "feature")
    op.drop_table(_OUTBOX)
    op.drop_table(_CLIENT_KEYS)

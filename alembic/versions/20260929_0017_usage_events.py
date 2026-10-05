"""usage events and idempotency conflicts, with the conflict table's append-only triggers

照设计闸门 #176 v8（docs/design/AIH-TASK-029-usage-ingest.md）§2「数据库」与 §8。

步骤
----

0. **预检**：与 0006 相同，查 `@@log_bin` 与 `@@log_bin_trust_function_creators`。
   binlog 开着而开关关着时，应用账号建不了触发器（ERROR 1419），所以**在任何 DDL
   之前**抛出明确的错误，`alembic_version` 仍是 0016。`--sql` 离线模式跳过（与 0015 相同）。
1. `integration_credentials` 加 UNIQUE `(id, project_id, tenant_id)`
   （`uq_integration_credentials_id_scope`）：只为下面的三列复合外键，`id` 本来就唯一
2. `usage_meter_types` 加 UNIQUE `(id, payload_shape, unit, quantity_kind)`
   （`uq_usage_meter_types_id_shape_unit_kind`）：只为下面的四列复合外键
3. `create_table usage_events`：`public_id`、`event_id`（`utf8mb4_0900_bin`）唯一；
   复合外键 `(project_id, tenant_id)` → `projects`、
   `(integration_credential_id, project_id, tenant_id)` → `integration_credentials`、
   `(usage_meter_type_id, payload_shape, unit, quantity_kind)` → `usage_meter_types`、
   `(model_id, provider_id)` → `ai_models`，单列 `provider_id` → `ai_providers`，全部 RESTRICT；
   设计逐列列出的必需列全部 NOT NULL；九条 CHECK（形态与字段组、整数数量、四个 token
   与 `quantity` 非负、状态、目录引用）；索引按设计 §2
4. `create_table usage_event_conflicts`：所有列 NOT NULL；外键 → `usage_events`、
   `integration_credentials`，RESTRICT；去重 UNIQUE `(event_id, api_key, payload_fingerprint)`；
   `mismatch` CHECK
5. 2 个触发器：`usage_event_conflicts` 的 BEFORE UPDATE / BEFORE DELETE 一律
   `SIGNAL SQLSTATE '45000'`（照 0010 给 `audit_logs` 的写法）

§132 第 13 条分析
-----------------

**已有数据**：两张新表；既有表只加两个唯一索引，不改列、不读写任何行。
`integration_credentials.id` 与 `usage_meter_types.id` 本来就是主键，新索引在任何已有数据上
都不可能冲突，加索引不会失败在数据上。

**锁表与停机**：`ADD UNIQUE` 在 InnoDB 上是在线 DDL（就地建索引、期间允许并发读写），开始与
结束各取一次短暂的元数据锁；两张表都只有几十行（凭据、计量类型），瞬时完成。建新表的外键时
MySQL 对被引用的五张表取短暂的元数据锁，不扫行。不需要停机。

**备份**：要求前置备份（与 0005–0016 同一口径）。本版不改动、不回填任何既有数据。

**部署顺序**：`deploy.sh` 先迁移、再换镜像。旧代码不读新表，也不受两个新唯一索引影响
（它们不收紧任何已有行能满足的条件）。迁移成功、代码未更新时新表空置；代码更新、迁移失败时
新容器起不来、旧容器继续服务（端点不存在，集成方 404 后重试），见设计 §8。

**失败处理**：⚠️ MySQL 的 DDL **不参与事务**，每一步各自隐式提交。
- 预检失败：什么都没改，`alembic_version` 仍是 `0016_lock_tables_no_update`。给账号打开
  `log_bin_trust_function_creators` 后重新部署。
- 第 1 步失败：什么都没改，修好后重新部署。
- 之后任何一步失败，或进程在中途被杀：已建的索引 / 表 / 触发器存在而 `alembic_version`
  没有前进，重跑会撞 "Duplicate key name" / "Table already exists"。清场步骤：确认
  `alembic_version` 仍是 `0016_lock_tables_no_update`、两张新表为空后，按依赖倒序
  `DROP TABLE usage_event_conflicts`、`DROP TABLE usage_events`（不存在的跳过；触发器随表删除），
  再 `ALTER TABLE usage_meter_types DROP INDEX uq_usage_meter_types_id_shape_unit_kind`、
  `ALTER TABLE integration_credentials DROP INDEX uq_integration_credentials_id_scope`
  （不存在的跳过），然后重新部署。
- 本版不写数据，所以「清场重跑」在上线前始终安全。

**回滚**：`downgrade` 按外键依赖倒序删表（冲突 → 事件），触发器随表删除，再删两个唯一索引
（必须在删表之后：外键还在引用它们时 MySQL 拒绝删）。⚠️ 上线后到 Phase 3 试点前没有集成方
在发事件，表是空的；试点之后回滚会丢事件 —— 那时只能前滚（设计 §8）。

**残余风险**：`TRUNCATE` / `DROP` 是 DDL，不经触发器（与 0006 / 0010 相同）；能执行它们的账号
同样能删掉触发器。归「迁移账号与运行账号拆分」。

Revision ID: 0017_usage_events
Revises: 0016_lock_tables_no_update
Create Date: 2026-09-29
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import mysql

from alembic import op

revision: str = "0017_usage_events"
down_revision: str | None = "0016_lock_tables_no_update"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_EVENTS = "usage_events"
_CONFLICTS = "usage_event_conflicts"

# 依赖倒序：downgrade 按这个顺序删。
_DROP_ORDER = (_CONFLICTS, _EVENTS)

# ⚠️ 二进制、NO PAD 排序（与 0007 / 0012 相同）。`unit` 参与复合外键，必须与
# `usage_meter_types.unit` 同一个排序规则。
_TEXT_COLLATION = "utf8mb4_0900_bin"

# 唯一约束名 → (表, 列)。只为复合外键（先例：0007 的 `uq_projects_id_tenant`）。
_PARENT_UNIQUES: dict[str, tuple[str, list[str]]] = {
    "uq_integration_credentials_id_scope": (
        "integration_credentials",
        ["id", "project_id", "tenant_id"],
    ),
    "uq_usage_meter_types_id_shape_unit_kind": (
        "usage_meter_types",
        ["id", "payload_shape", "unit", "quantity_kind"],
    ),
}

_PREREQUISITE_QUERY = "SELECT @@log_bin, @@log_bin_trust_function_creators"
_PREREQUISITE_ERROR = (
    "MySQL has binary logging on and log_bin_trust_function_creators OFF, "
    + "so this account cannot create the usage event conflict triggers (ERROR 1419). "
    + "Set log_bin_trust_function_creators = ON first; nothing has been changed."
)

# 名字 → (表, 条件)。⚠️ 冻结的字面量：与 app/models/usage.py 上的 CheckConstraint
# 逐条一致（空白除外），test_migrations.py 比对两边。
_CHECKS: dict[str, tuple[str, str]] = {
    # 形态与字段组：显式写出两组条件，不用链式比较（设计 v2）。
    "ck_usage_events_shape_fields": (
        _EVENTS,
        "(payload_shape = 'LLM_TOKEN_FIELDS'"
        " AND input_tokens IS NOT NULL AND output_tokens IS NOT NULL"
        " AND cache_creation_input_tokens IS NOT NULL AND cache_read_input_tokens IS NOT NULL"
        " AND quantity IS NULL)"
        " OR (payload_shape = 'QUANTITY'"
        " AND input_tokens IS NULL AND output_tokens IS NULL"
        " AND cache_creation_input_tokens IS NULL AND cache_read_input_tokens IS NULL"
        " AND quantity IS NOT NULL)",
    ),
    # 整数类型的数量不许有小数部分（设计 v3）。
    "ck_usage_events_integer_quantity": (
        _EVENTS,
        "quantity_kind <> 'INTEGER' OR quantity IS NULL OR quantity = FLOOR(quantity)",
    ),
    "ck_usage_events_input_tokens": (_EVENTS, "input_tokens IS NULL OR input_tokens >= 0"),
    "ck_usage_events_output_tokens": (_EVENTS, "output_tokens IS NULL OR output_tokens >= 0"),
    "ck_usage_events_cache_creation_input_tokens": (
        _EVENTS,
        "cache_creation_input_tokens IS NULL OR cache_creation_input_tokens >= 0",
    ),
    "ck_usage_events_cache_read_input_tokens": (
        _EVENTS,
        "cache_read_input_tokens IS NULL OR cache_read_input_tokens >= 0",
    ),
    "ck_usage_events_quantity": (_EVENTS, "quantity IS NULL OR quantity >= 0"),
    # spec §83 的九个取值。
    "ck_usage_events_status": (
        _EVENTS,
        "status IN ('RECEIVED', 'PROCESSING', 'PROCESSED', 'PRICING_ERROR', 'FX_RATE_ERROR',"
        " 'MODEL_UNKNOWN', 'IDEMPOTENCY_CONFLICT', 'FAILED_RETRYABLE', 'FAILED_FINAL')",
    ),
    # §79 的目录引用：有模型必有供应商（设计 v7）。
    "ck_usage_events_catalog_reference": (_EVENTS, "model_id IS NULL OR provider_id IS NOT NULL"),
    "ck_usage_event_conflicts_mismatch": (
        _CONFLICTS,
        "mismatch IN ('OWNERSHIP', 'FINGERPRINT', 'BOTH')",
    ),
}

# 触发器名 → 建触发器的语句。写法照 0010 的 `audit_logs`（单条 SIGNAL，整条 CREATE TRIGGER
# 是一条语句）。冲突行是「这个 event_id 被别人用过」的证据，也是告警维度的依据：只增。
_TRIGGERS: dict[str, str] = {
    "trg_usage_event_conflicts_before_update": """
CREATE TRIGGER trg_usage_event_conflicts_before_update
BEFORE UPDATE ON usage_event_conflicts
FOR EACH ROW
SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'usage_event_conflicts is append-only'
""",
    "trg_usage_event_conflicts_before_delete": """
CREATE TRIGGER trg_usage_event_conflicts_before_delete
BEFORE DELETE ON usage_event_conflicts
FOR EACH ROW
SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'usage_event_conflicts is append-only'
""",
}


def _text(length: int) -> sa.String:
    return sa.String(length=length, collation=_TEXT_COLLATION)


def _checks(table: str) -> list[sa.CheckConstraint]:
    return [
        sa.CheckConstraint(condition, name=name)
        for name, (owner, condition) in _CHECKS.items()
        if owner == table
    ]


def _require_trigger_privilege(bind: sa.engine.Connection) -> None:
    """Refuse to run where the migration account could not create triggers."""
    log_bin, trusted = bind.execute(sa.text(_PREREQUISITE_QUERY)).one()
    if int(log_bin) == 1 and int(trusted) != 1:
        raise RuntimeError(_PREREQUISITE_ERROR)


def upgrade() -> None:
    # 第 0 步。⚠️ 必须排在任何 DDL 之前：MySQL 的 DDL 不参与事务，建了一半要人工清场。
    # `--sql` 离线模式不连库，也就没有可查的变量。
    if not op.get_context().as_sql:
        _require_trigger_privilege(op.get_bind())

    for name, (table, columns) in _PARENT_UNIQUES.items():
        op.create_unique_constraint(name, table, columns)

    op.create_table(
        _EVENTS,
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("public_id", sa.CHAR(length=36), nullable=False),
        sa.Column("event_id", _text(64), nullable=False),
        sa.Column("schema_version", sa.String(length=8), nullable=False),
        sa.Column("tenant_id", sa.BigInteger(), nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("integration_credential_id", sa.BigInteger(), nullable=False),
        sa.Column("request_id", _text(128), nullable=False),
        sa.Column("conversation_id", _text(128), nullable=True),
        sa.Column("provider_code_raw", _text(64), nullable=False),
        sa.Column("model_code_raw", _text(128), nullable=False),
        # T-H 在计费事务里解析后写入；本任务写入时为空。
        sa.Column("provider_id", sa.BigInteger(), nullable=True),
        sa.Column("model_id", sa.BigInteger(), nullable=True),
        sa.Column("usage_meter_type_id", sa.BigInteger(), nullable=False),
        # 与 `usage_meter_types` 的同名列同类型、同排序规则（复合外键）。
        sa.Column("payload_shape", sa.String(length=32), nullable=False),
        sa.Column("quantity_kind", sa.String(length=16), nullable=False),
        sa.Column("input_tokens", sa.BigInteger(), nullable=True),
        sa.Column("output_tokens", sa.BigInteger(), nullable=True),
        sa.Column("cache_creation_input_tokens", sa.BigInteger(), nullable=True),
        sa.Column("cache_read_input_tokens", sa.BigInteger(), nullable=True),
        sa.Column("quantity", sa.Numeric(precision=20, scale=8), nullable=True),
        sa.Column("unit", _text(16), nullable=False),
        sa.Column("payload_fingerprint", sa.CHAR(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), server_default="RECEIVED", nullable=False),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.String(length=255), nullable=True),
        # 保留微秒（025 §7「边界时刻」）。
        sa.Column("occurred_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.Column("received_at", sa.DateTime(), nullable=False),
        sa.Column("processed_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["project_id", "tenant_id"],
            ["projects.id", "projects.tenant_id"],
            name="fk_usage_events_project",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["integration_credential_id", "project_id", "tenant_id"],
            [
                "integration_credentials.id",
                "integration_credentials.project_id",
                "integration_credentials.tenant_id",
            ],
            name="fk_usage_events_credential",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["usage_meter_type_id", "payload_shape", "unit", "quantity_kind"],
            [
                "usage_meter_types.id",
                "usage_meter_types.payload_shape",
                "usage_meter_types.unit",
                "usage_meter_types.quantity_kind",
            ],
            name="fk_usage_events_meter_type",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["model_id", "provider_id"],
            ["ai_models.id", "ai_models.provider_id"],
            name="fk_usage_events_model",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["provider_id"],
            ["ai_providers.id"],
            name="fk_usage_events_provider",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("public_id", name="uq_usage_events_public_id"),
        sa.UniqueConstraint("event_id", name="uq_usage_events_event_id"),
        sa.Index("ix_usage_events_status_occurred", "status", "occurred_at"),
        sa.Index("ix_usage_events_tenant_id", "tenant_id"),
        # `project_id` / `model_id` 的索引取复合外键需要的形式（首列就是该列）。
        sa.Index("ix_usage_events_project_id", "project_id", "tenant_id"),
        sa.Index("ix_usage_events_model_id", "model_id", "provider_id"),
        sa.Index("ix_usage_events_provider_id", "provider_id"),
        sa.Index("ix_usage_events_conversation_id", "conversation_id"),
        sa.Index("ix_usage_events_request_id", "request_id"),
        sa.Index("ix_usage_events_occurred_at", "occurred_at"),
        sa.Index(
            "ix_usage_events_credential",
            "integration_credential_id",
            "project_id",
            "tenant_id",
        ),
        sa.Index(
            "ix_usage_events_meter_type",
            "usage_meter_type_id",
            "payload_shape",
            "unit",
            "quantity_kind",
        ),
        *_checks(_EVENTS),
    )

    op.create_table(
        _CONFLICTS,
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("usage_event_id", sa.BigInteger(), nullable=False),
        sa.Column("event_id", _text(64), nullable=False),
        sa.Column("integration_credential_id", sa.BigInteger(), nullable=False),
        sa.Column("api_key", _text(64), nullable=False),
        sa.Column("payload_fingerprint", sa.CHAR(length=64), nullable=False),
        sa.Column("mismatch", sa.String(length=32), nullable=False),
        sa.Column("received_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["usage_event_id"],
            [f"{_EVENTS}.id"],
            name="fk_usage_event_conflicts_event",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["integration_credential_id"],
            ["integration_credentials.id"],
            name="fk_usage_event_conflicts_credential",
            ondelete="RESTRICT",
        ),
        # 同一冲突只记一次（设计 §2）。
        sa.UniqueConstraint(
            "event_id", "api_key", "payload_fingerprint", name="uq_usage_event_conflicts_dedupe"
        ),
        *_checks(_CONFLICTS),
    )

    for statement in _TRIGGERS.values():
        op.execute(statement)


def downgrade() -> None:
    # 按外键依赖倒序：冲突 → 事件，触发器随表删除；再删两个唯一索引（外键已随表删除）。
    for table in _DROP_ORDER:
        op.drop_table(table)
    for name, (table, _columns) in reversed(_PARENT_UNIQUES.items()):
        op.drop_constraint(name, table, type_="unique")

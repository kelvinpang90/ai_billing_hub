"""usage events: billing snapshot and claim columns, their checks, indexes and three triggers

照设计闸门 #181 v2（docs/design/AIH-TASK-032-usage-billing.md）§2「数据库」与 §8。

步骤
----

0. **预检**：与 0006 相同，查 `@@log_bin` 与 `@@log_bin_trust_function_creators`。
   binlog 开着而开关关着时，应用账号建不了触发器（ERROR 1419），所以**在任何 DDL
   之前**抛出明确的错误，`alembic_version` 仍是 0017。`--sql` 离线模式跳过（与 0015、0017 相同）。
1. `usage_events` 加 14 列（设计 §2 的表）：快照 9 列（三个版本引用、原币币种与成本、
   汇率原值、MYR 估算成本、计费额、账本行）全部可空；`attempt_count` NOT NULL DEFAULT 0；
   `next_attempt_at`、`claim_token`、`claimed_at`、`lease_expires_at` 可空
2. 索引：认领 `(status, next_attempt_at, id)`、卡住回收 `(status, lease_expires_at)`、
   三个快照外键各一个；UNIQUE `wallet_transaction_id`
3. 外键：→ `provider_price_versions`、`pricing_rules`、`fx_rate_versions`、
   `wallet_transactions`，全部 RESTRICT
4. 七条 CHECK：已处理 ⇒ 快照齐全（含 MYR 与非 MYR 的汇率列规则）；已处理且计费额 > 0 ⇒
   有账本行；未处理 ⇒ 没有账本行；三个成本列非负；认领字段与状态一致。全部逐列写
   `IS NOT NULL` / `IS NULL`，不用行构造器与布尔等式
5. 三个 BEFORE UPDATE 触发器（写法照 0006，整条 CREATE TRIGGER 是一条语句）：
   `PROCESSED` 不可改；`PROCESSED` 且计费额 > 0 时账本行必须是这个事件的、金额 = −计费额；
   错误状态改回 `RECEIVED`（重新入队）时账本里不能有这个事件的行

§132 第 13 条分析
-----------------

**已有数据**：只加列与约束，不改、不读、不回填任何行。上线前 `usage_events` 里只有尚未计费的
`RECEIVED` 事件（Phase 3 试点前没有集成方在发事件，设计 §8），它们的新列取默认值：快照与认领
列为空、`attempt_count = 0`。七条 CHECK 在这样的行上全部成立（状态不是 `PROCESSED`、不是
`PROCESSING`，认领列与账本行都为空），所以加 CHECK 不会失败在数据上。表里若意外有
`PROCESSING` 的行（摄取从不写它），认领 CHECK 会让第 4 步失败 —— 那是数据已经不对，按下面的
「失败处理」查清再部署，不要放宽 CHECK。

**锁表与停机**：InnoDB 上 `ADD COLUMN`（尾部、可空或带常量默认值）是 INSTANT / 在线 DDL；
加索引与外键是就地在线建（`foreign_key_checks` 开着时加外键会扫一遍表校验，表很小）；
加 CHECK 要校验已有行，MySQL 以 COPY 方式重建表、期间阻塞写 —— 表里只有少量未计费事件，
瞬时完成。摄取在这几秒里的插入会等元数据锁，不报错。不需要停机。

**备份**：要求前置备份（与 0005–0017 同一口径）。本版不改动、不回填任何既有数据。

**部署顺序**：`deploy.sh` 先迁移、再换镜像。迁移成功、代码未更新时：新列为空，旧代码没有计费
worker，事件留在 `RECEIVED`，摄取不读新列、照常插入（`attempt_count` 由库默认值补 0）。
代码更新、迁移失败时：新容器起不来，旧容器继续服务（设计 §8）。新的两个 Beat 条目随新镜像生效。

**失败处理**：⚠️ MySQL 的 DDL **不参与事务**，每一步各自隐式提交。
- 预检失败：什么都没改，`alembic_version` 仍是 `0017_usage_events`。给账号打开
  `log_bin_trust_function_creators` 后重新部署。
- 之后任何一步失败，或进程在中途被杀：已加的列 / 索引 / 外键 / CHECK / 触发器留在库里而
  `alembic_version` 没有前进，重跑会撞 "Duplicate column name" / "Duplicate key name"。清场：
  确认 `alembic_version` 仍是 `0017_usage_events`、没有任何事件的新列被写过（没有新代码在跑）后，
  按本文件 `downgrade()` 的顺序手工删掉**已经存在的**部分（触发器 → CHECK → 外键 → 唯一与普通
  索引 → 列；不存在的跳过），然后重新部署。
- 本版不写数据，所以「清场重跑」在上线前始终安全。

**回滚**：`downgrade` 先删三个触发器，再删七条 CHECK（MySQL 不许删掉 CHECK 还在引用的列）、
四个外键、唯一与普通索引，最后删 14 列。⚠️ 一旦有事件被计费，回滚会丢掉快照（账本行仍在，
但事件与它的对应、版本引用都没了）—— **只能前滚**（设计 §8）。

**残余风险**：`TRUNCATE` / `DROP` 是 DDL，不经触发器（与 0006 / 0010 / 0017 相同）；能执行它们
的账号同样能删掉触发器。归「迁移账号与运行账号拆分」。直接 INSERT 一行 `PROCESSED` 的事件
不经 BEFORE UPDATE 触发器（设计只要求 UPDATE 路径）；快照齐全与有账本行由 CHECK 挡住，账本行
是否属于这个事件只有在 UPDATE 路径上校验 —— 摄取只插入 `RECEIVED`，计费只经 UPDATE 写
`PROCESSED`。

Revision ID: 0018_usage_billing
Revises: 0017_usage_events
Create Date: 2026-09-29
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0018_usage_billing"
down_revision: str | None = "0017_usage_events"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_EVENTS = "usage_events"

_PREREQUISITE_QUERY = "SELECT @@log_bin, @@log_bin_trust_function_creators"
_PREREQUISITE_ERROR = (
    "MySQL has binary logging on and log_bin_trust_function_creators OFF, "
    + "so this account cannot create the usage event billing triggers (ERROR 1419). "
    + "Set log_bin_trust_function_creators = ON first; nothing has been changed."
)

# 第 1 步，按设计 §2 的表的顺序。⚠️ 类型与 app/models/usage.py 一致：金额 DECIMAL(20,8)、
# 汇率 DECIMAL(24,10)、时刻整秒（与其他服务端时间一致）。
_COLUMN_NAMES = (
    "provider_price_version_id",
    "pricing_rule_id",
    "fx_rate_version_id",
    "provider_source_currency",
    "provider_source_cost",
    "fx_rate_applied",
    "estimated_provider_cost_myr",
    "billable_cost",
    "wallet_transaction_id",
    "attempt_count",
    "next_attempt_at",
    "claim_token",
    "claimed_at",
    "lease_expires_at",
)


def _columns() -> list[sa.Column]:
    """Fresh column objects each time: `add_column` attaches them to a table."""
    columns = [
        sa.Column("provider_price_version_id", sa.BigInteger(), nullable=True),
        sa.Column("pricing_rule_id", sa.BigInteger(), nullable=True),
        sa.Column("fx_rate_version_id", sa.BigInteger(), nullable=True),
        sa.Column("provider_source_currency", sa.CHAR(length=3), nullable=True),
        sa.Column("provider_source_cost", sa.Numeric(precision=20, scale=8), nullable=True),
        sa.Column("fx_rate_applied", sa.Numeric(precision=24, scale=10), nullable=True),
        sa.Column("estimated_provider_cost_myr", sa.Numeric(precision=20, scale=8), nullable=True),
        sa.Column("billable_cost", sa.Numeric(precision=20, scale=8), nullable=True),
        sa.Column("wallet_transaction_id", sa.BigInteger(), nullable=True),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(), nullable=True),
        sa.Column("claim_token", sa.CHAR(length=36), nullable=True),
        sa.Column("claimed_at", sa.DateTime(), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(), nullable=True),
    ]
    assert tuple(column.name for column in columns) == _COLUMN_NAMES
    return columns


# 第 2 步。索引名 → 列。
_INDEXES: dict[str, list[str]] = {
    # 认领 Ⓐ：status IN (...) AND next_attempt_at … ORDER BY id。
    "ix_usage_events_claim": ["status", "next_attempt_at", "id"],
    # 卡住回收：status = 'PROCESSING' AND lease_expires_at < now。
    "ix_usage_events_lease": ["status", "lease_expires_at"],
    # 三个快照外键在子表上的索引（不让 MySQL 自动起名）。
    "ix_usage_events_price_version_id": ["provider_price_version_id"],
    "ix_usage_events_pricing_rule_id": ["pricing_rule_id"],
    "ix_usage_events_fx_rate_version_id": ["fx_rate_version_id"],
}
# 一行账本至多对应一个事件；也是外键 `fk_usage_events_wallet_transaction` 用的索引。
_UNIQUE_NAME = "uq_usage_events_wallet_transaction_id"
_UNIQUE_COLUMNS = ["wallet_transaction_id"]

# 第 3 步。外键名 → (本表列, 被引用表, 被引用列)。全部 RESTRICT：版本、规则、账本行永不删除。
_FOREIGN_KEYS: dict[str, tuple[list[str], str, list[str]]] = {
    "fk_usage_events_price_version": (
        ["provider_price_version_id"],
        "provider_price_versions",
        ["id"],
    ),
    "fk_usage_events_pricing_rule": (["pricing_rule_id"], "pricing_rules", ["id"]),
    "fk_usage_events_fx_rate_version": (["fx_rate_version_id"], "fx_rate_versions", ["id"]),
    "fk_usage_events_wallet_transaction": (
        ["wallet_transaction_id"],
        "wallet_transactions",
        ["id"],
    ),
}

# 第 4 步的七个条件。⚠️ 冻结的字面量：与 app/models/usage.py 上的 CheckConstraint
# 逐条一致（空白除外），test_migrations.py 比对两边。逐列写 `IS NOT NULL` / `IS NULL`，
# 不用行构造器、不用布尔等式。
_PROCESSED_SNAPSHOT = (
    "status <> 'PROCESSED'"
    " OR (provider_id IS NOT NULL AND model_id IS NOT NULL"
    " AND provider_price_version_id IS NOT NULL AND pricing_rule_id IS NOT NULL"
    " AND provider_source_currency IS NOT NULL AND provider_source_cost IS NOT NULL"
    " AND estimated_provider_cost_myr IS NOT NULL AND billable_cost IS NOT NULL"
    " AND processed_at IS NOT NULL"
    " AND ((provider_source_currency = 'MYR'"
    " AND fx_rate_version_id IS NULL AND fx_rate_applied IS NULL)"
    " OR (provider_source_currency <> 'MYR'"
    " AND fx_rate_version_id IS NOT NULL AND fx_rate_applied IS NOT NULL)))"
)
_PROCESSED_LEDGER = (
    "status <> 'PROCESSED' OR billable_cost = 0 OR wallet_transaction_id IS NOT NULL"
)
_UNPROCESSED_LEDGER = "status = 'PROCESSED' OR wallet_transaction_id IS NULL"
_BILLABLE_COST = "billable_cost IS NULL OR billable_cost >= 0"
_PROVIDER_SOURCE_COST = "provider_source_cost IS NULL OR provider_source_cost >= 0"
_ESTIMATED_COST = "estimated_provider_cost_myr IS NULL OR estimated_provider_cost_myr >= 0"
_CLAIM_FIELDS = (
    "(status = 'PROCESSING'"
    " AND claim_token IS NOT NULL AND claimed_at IS NOT NULL AND lease_expires_at IS NOT NULL"
    " AND claimed_at < lease_expires_at)"
    " OR (status <> 'PROCESSING'"
    " AND claim_token IS NULL AND claimed_at IS NULL AND lease_expires_at IS NULL)"
)

# 名字 → 条件，按建的顺序。
_CHECKS: dict[str, str] = {
    "ck_usage_events_processed_snapshot": _PROCESSED_SNAPSHOT,
    "ck_usage_events_processed_ledger": _PROCESSED_LEDGER,
    "ck_usage_events_unprocessed_ledger": _UNPROCESSED_LEDGER,
    "ck_usage_events_billable_cost": _BILLABLE_COST,
    "ck_usage_events_provider_source_cost": _PROVIDER_SOURCE_COST,
    "ck_usage_events_estimated_cost": _ESTIMATED_COST,
    "ck_usage_events_claim_fields": _CLAIM_FIELDS,
}

# 第 5 步。触发器名 → 建触发器的语句。⚠️ 整条 CREATE TRIGGER 是**一条**语句（写法照 0006）：
# DELIMITER 只是 mysql 命令行客户端的东西，驱动把 BEGIN … END 原样发给服务器。三个都是
# BEFORE UPDATE（MySQL 8 允许同一时机多个触发器，按创建顺序执行），按设计 §2 的顺序建。
_TRIGGERS: dict[str, str] = {
    # INV-6 / INV-11：已计费事件的快照与状态永不改变。Phase 8 的 rebill 另写调整行，不改原事件。
    "trg_usage_events_processed_immutable": """
CREATE TRIGGER trg_usage_events_processed_immutable
BEFORE UPDATE ON usage_events
FOR EACH ROW
BEGIN
    IF OLD.status = 'PROCESSED' THEN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'a processed usage event is immutable';
    END IF;
END
""",
    # 事件与账本的对应：计费额 > 0 的事件变成 PROCESSED 时，链接的账本行必须是这个事件的
    # （USAGE_EVENT / event_id），金额恰好是 −计费额。链接为空时 NOT EXISTS 同样成立 → 拒绝。
    "trg_usage_events_ledger_link": """
CREATE TRIGGER trg_usage_events_ledger_link
BEFORE UPDATE ON usage_events
FOR EACH ROW
BEGIN
    IF NEW.status = 'PROCESSED' AND NEW.billable_cost > 0
       AND NOT EXISTS (
            SELECT 1
              FROM wallet_transactions
             WHERE id = NEW.wallet_transaction_id
               AND reference_type = 'USAGE_EVENT'
               AND reference_id = NEW.event_id
               AND amount = -NEW.billable_cost) THEN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'a billed usage event must link its own ledger row';
    END IF;
END
""",
    # 重新入队只对从未产生财务效果的事件：错误状态改回 RECEIVED 时，账本里不能有它的行。
    "trg_usage_events_requeue_unbilled": """
CREATE TRIGGER trg_usage_events_requeue_unbilled
BEFORE UPDATE ON usage_events
FOR EACH ROW
BEGIN
    IF OLD.status IN ('MODEL_UNKNOWN', 'PRICING_ERROR', 'FX_RATE_ERROR', 'FAILED_FINAL')
       AND NEW.status = 'RECEIVED'
       AND EXISTS (
            SELECT 1
              FROM wallet_transactions
             WHERE reference_type = 'USAGE_EVENT'
               AND reference_id = NEW.event_id) THEN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'a usage event with a ledger row cannot be requeued';
    END IF;
END
""",
}


def _require_trigger_privilege(bind: sa.engine.Connection) -> None:
    """Refuse to run where the migration account could not create triggers."""
    log_bin, trusted = bind.execute(sa.text(_PREREQUISITE_QUERY)).one()
    if int(log_bin) == 1 and int(trusted) != 1:
        raise RuntimeError(_PREREQUISITE_ERROR)


def upgrade() -> None:
    # 第 0 步。⚠️ 必须排在任何 DDL 之前：MySQL 的 DDL 不参与事务，加了一半要人工清场。
    # `--sql` 离线模式不连库，也就没有可查的变量。
    if not op.get_context().as_sql:
        _require_trigger_privilege(op.get_bind())

    for column in _columns():
        op.add_column(_EVENTS, column)

    for name, columns in _INDEXES.items():
        op.create_index(name, _EVENTS, columns)
    op.create_unique_constraint(_UNIQUE_NAME, _EVENTS, _UNIQUE_COLUMNS)

    for name, (local, referred, remote) in _FOREIGN_KEYS.items():
        op.create_foreign_key(name, _EVENTS, referred, local, remote, ondelete="RESTRICT")

    for name, condition in _CHECKS.items():
        op.create_check_constraint(name, _EVENTS, condition)

    for statement in _TRIGGERS.values():
        op.execute(statement)


def downgrade() -> None:
    # 倒序：触发器 → CHECK（MySQL 不许删掉 CHECK 还在引用的列）→ 外键（之后才能删它们用的
    # 索引）→ 唯一与普通索引 → 列。
    for name in reversed(_TRIGGERS):
        op.execute(f"DROP TRIGGER {name}")
    for name in reversed(_CHECKS):
        op.execute(f"ALTER TABLE {_EVENTS} DROP CHECK {name}")
    for name in reversed(_FOREIGN_KEYS):
        op.drop_constraint(name, _EVENTS, type_="foreignkey")
    op.drop_constraint(_UNIQUE_NAME, _EVENTS, type_="unique")
    for name in reversed(_INDEXES):
        op.drop_index(name, table_name=_EVENTS)
    for name in reversed(_COLUMN_NAMES):
        op.drop_column(_EVENTS, name)

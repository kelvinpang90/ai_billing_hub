"""provider cost reconciliations: the insert-only table of monthly provider costs and its two
immutability triggers

照设计闸门 #755 v1（docs/design/AIH-TASK-242-provider-cost-reconciliation.md）§2「数据库（迁移
0041，AIH-TASK-242）」、§3 INV-6、INV-10、§4 跃迁表最后一行、§5「迁移中断」、§6「公开仓库与保留」与
§8；本设计建立在毛利分析（设计闸门 #709 v2，docs/design/AIH-TASK-221-margin-analytics.md）之上。
本迁移是 AIH-TASK-242。**只建一张新表与两个触发器**，不改任何已有表的列、索引、约束与触发器，
不回填，不读写任何已有表的行：

- `provider_cost_reconciliations`（25 列，一个供应商 × 范围 × UTC 自然月的实际成本记录，
  行**只插入**）：
  - STORED 生成列 `root_slot`：`CASE WHEN supersedes_id IS NULL THEN 1 END`（根为 1，取代行为 NULL；
    写法同 `fx_rate_versions.open_slot`）；
  - 唯一 `ux_provider_cost_reconciliations_root (provider_id, scope, billing_month, root_slot)`：
    同一供应商、范围、月份至多一个根（唯一索引不管 NULL）；也是 `provider_id` 外键用的索引；
  - 唯一 `uq_provider_cost_reconciliations_public_id`；
  - 唯一 `uq_provider_cost_reconciliations_supersedes (supersedes_id, provider_id, scope,
    billing_month)`：复合自引用外键在子表上的索引（前导列与外键列同名同序），同时让每行至多被取代
    一次（外键保证后三列等于被取代行的，四列唯一等价于 `supersedes_id` 唯一）；不另建单列索引；
  - 唯一 `uq_provider_cost_reconciliations_chain_key (id, provider_id, scope, billing_month)`：复合
    自引用外键在父表一侧引用的键；
  - `ix_provider_cost_reconciliations_fx_rate_version_id` 与
    `ix_provider_cost_reconciliations_created_by`：**显式声明**，供外键使用，所以 MySQL 不另建
    与外键同名的隐式索引（与 0039 同一理由）；
  - 外键 `fk_provider_cost_reconciliations_provider_id` → `ai_providers.id`、
    `fk_provider_cost_reconciliations_fx_rate_version_id` → `fx_rate_versions.id`、
    `fk_provider_cost_reconciliations_created_by` → `users.id`、复合自引用
    `fk_provider_cost_reconciliations_supersedes (supersedes_id, provider_id, scope,
    billing_month)` → `(id, provider_id, scope, billing_month)`（取代者与被取代者必是
    同一供应商、范围、月份；`supersedes_id` 为 NULL 时 MATCH SIMPLE 不检查），都是
    `ON DELETE RESTRICT`；
  - 13 条 CHECK：`fx`、`reason`（逐列写 `IS NOT NULL`，不用行构造器）、`scope`（只许 `AI_MODULE`）、
    `coverage_method`（只许 `PERIOD_AGGREGATE`）、`period`，以及八条 `>= 0`（金额四条、计数四条）；
  - 不建 `metadata_json`（所有字段都是结构化列），不建状态列（「当前版本」= 没有任何行的
    `supersedes_id` 指向它）。
- MySQL 上两个触发器 `trg_provider_cost_reconciliations_before_update`（BEFORE UPDATE）与
  `trg_provider_cost_reconciliations_before_delete`（BEFORE DELETE）：**无条件** SIGNAL 45000
  （`provider_cost_reconciliations rows are immutable`），行不可改、不可删
  （§4 跃迁表最后一行、§6）。SQLite 不建触发器（与 0039 同一惯例）。

没有写入方：行只由 AIH-TASK-244 的 `confirm` 写（只 INSERT）。
`app/models/provider_cost_reconciliations.py` 的 `ProviderCostReconciliation`
同步声明了这些列、索引与约束（同名同序），
`tests/backend/test_provider_cost_reconciliations_migration.py` 比对两边一致。

步骤
----

1. 用 inspector 判表。存在时逐项比「形状」（设计 §2「数据库」）：恰好设计的列（名称、类型与宽度、
   可空性；**不比服务端默认值**）、生成列（`root_slot` 是 STORED、表达式去掉空白、括号与反引号后
   与设计逐字相同；MySQL 上读 `information_schema.COLUMNS` 的 `GENERATION_EXPRESSION` 与 `EXTRA`）、
   主键列、索引（名称、列与列顺序、唯一与否，没有前缀长度）、外键（列、引用表与列、
   `ON DELETE RESTRICT`，MySQL 上的删除规则读 `information_schema.REFERENTIAL_CONSTRAINTS`）、CHECK
   （名称）。任何一项缺失、多出或不同都算形状不对 → 抛错，点名表，**不发任何 DDL**。
2. MySQL：读 `information_schema.TRIGGERS` 里这两个名字的触发器，逐个比 `EVENT_OBJECT_TABLE`、
   `ACTION_TIMING`、`EVENT_MANIPULATION` 与 `ACTION_STATEMENT`（去掉首尾空白后与 `_TRIGGER_BODY`
   逐字相等）。存在但任一项不一致 → 抛错，**不发任何 DDL**。
3. 表在、两个触发器都在且一致 → 不发 DDL，只把版本前进。
4. **预检**（MySQL，只在要发 DDL 时，紧接在第一条 DDL 之前）：查 `@@log_bin` 与
   `@@log_bin_trust_function_creators`（查询、判定与报错文案照 0039）。binlog 开着而开关关着时，
   应用账号建不了触发器（ERROR 1419），所以在 `CREATE TABLE` 之前抛错。
5. 缺什么建什么，按这个顺序：表不在时 `provider_cost_reconciliations`（MySQL 上**一条**
   `CREATE TABLE`，生成列、索引、外键与 CHECK 都在里面）→ 缺哪个触发器建哪个（先 BEFORE UPDATE 后
   BEFORE DELETE），已在的不动。其他方言：`create_table` 加 `create_index`，不建触发器。

`--sql` 离线模式读不了库：照 0039 的 `_upgrade_offline`，跳过预检与全部检查，按「什么都没做过」
输出 `CREATE TABLE` 与两条 `CREATE TRIGGER`。

§132 第 13 条分析
-----------------

**已有数据**：只建一张新表与它上面的两个触发器；不改任何已有表的列、索引、约束与触发器，不回填，
不读写任何已有行。用量事件的估算快照（INV-6）、钱包与账本（INV-4、INV-5）、对账单（INV-12）都不被碰。
外键只指向 `ai_providers`、`fx_rate_versions`、`users` 与本表自己。

**锁表与停机**：建新表不锁任何已有表的行；建三个外键时 MySQL 对 `ai_providers`、`fx_rate_versions`
与 `users` 各取短暂的元数据锁（等正在进行的、碰过它们的事务结束），不扫行。建触发器只锁
`provider_cost_reconciliations`（空表，没有读写方）。不需要停机。

**备份**：要求前置备份（与 0005–0040 同一口径）。本版不写任何行。

**部署顺序**：`deploy.sh` 先迁移、再换镜像。本迁移的 `down_revision` 是 AIH-TASK-231 的
`0040_reset_request_index`，须在它之后部署（设计 §8「部署顺序」：AIH-TASK-231 → AIH-TASK-242 →
243 → 244 → 245 → 246 → …）。本任务不带写入方与接口，部署后只多一张没人读写的空表；旧代码不知道它。
先迁移后换镜像、或反过来都安全。

**失败处理**：⚠️ MySQL 的 DDL **不参与事务**：`CREATE TABLE` 与每条 `CREATE TRIGGER` 都隐式提交。
表连同生成列、索引、外键与 CHECK 在**一条** `CREATE TABLE` 里，MySQL 8 的 InnoDB DDL 是原子的，
加上第 1–2 步的检查，每个窗口都有确定的结局：

- 预检失败：什么都没改，`alembic_version` 仍是 `0040_reset_request_index`。给账号打开
  `log_bin_trust_function_creators` 后重新部署。
- 建表失败（锁等待超时、进程被杀）→ 整条回滚，表不存在，版本仍是 0040 → 原样重新部署。
- 表建成、某个触发器建成之前中断 → 表在、缺一个或两个触发器，版本仍是 0040 → 原样重新部署：只建
  缺的那个（或那两个）。这期间没有写入方（AIH-TASK-244 上线之前），缺触发器不会让记录被改。
- 全部建成、写 `alembic_version` 之前进程退出 → 原样重新部署：判为已完成，不发 DDL，只把版本前进。
- 有人手工建过同名但形状不同的表 → 重跑在第 1 步失败，报错点名这张表，不发 DDL，版本仍是 0040 →
  先确认表里没有任何行：为空则 `DROP TABLE` 它（它上面的触发器随表删除）再重新部署；有行则停下，
  按数据事故处理，不要删。
- 同名触发器存在但时机、事件、所在表或正文不同 → 重跑在第 2 步失败，报错点名触发器，不发 DDL，
  版本仍是 0040 → 人对照设计查看；确认是手工建的之后 `DROP TRIGGER` 它，再重新部署（只补建缺的
  那个）。

**回滚**：生产上的回滚**只回滚应用镜像，不降级 0041**（设计 §8「回滚或前滚」）；记录写错用更正
（追加取代行），不删行。`downgrade` **只删空表**：`--sql` 离线模式判断不了表是否为空，直接抛错
（须在线执行），不输出任何 `DROP`；表已不在时什么都不做（不发 DDL）；先
`SELECT 1 FROM provider_cost_reconciliations LIMIT 1`，有任何一行就抛错（消息点名这张表、处理方法是
保留表只回滚应用镜像），不发任何 DDL、不删任何行 —— 对账记录是永久保留的财务记录（§6）；为空时
MySQL 上先删这张表上的那两个触发器（只删存在的），再删表。

Revision ID: 0041_provider_cost_recon
Revises: 0040_reset_request_index
Create Date: 2026-11-17
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import NoReturn

import sqlalchemy as sa

from alembic import op

# `alembic_version.version_num` 是 VARCHAR(32)，ID 必须不超过 32 个字符（本 ID 24 个字符）。
revision: str = "0041_provider_cost_recon"
down_revision: str | None = "0040_reset_request_index"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "provider_cost_reconciliations"
_AI_PROVIDERS = "ai_providers"
_FX_RATE_VERSIONS = "fx_rate_versions"
_USERS = "users"

# ⚠️ 下面都是冻结的字面量；模型那边的声明由
# tests/backend/test_provider_cost_reconciliations_migration.py 比对。

# 列名 → (类型与宽度, 可空)，按建表顺序（设计 §2 的表）。类型写成 MySQL 反射出来的样子（去掉空白）。
_COLUMNS: dict[str, tuple[str, bool]] = {
    "id": ("BIGINT", False),
    "public_id": ("CHAR(36)", False),
    "provider_id": ("BIGINT", False),
    "scope": ("VARCHAR(16)", False),
    "billing_month": ("CHAR(7)", False),
    "period_start": ("DATETIME", False),
    "period_end": ("DATETIME", False),
    "source_document_reference": ("VARCHAR(255)", False),
    "source_currency": ("CHAR(3)", False),
    "source_total": ("DECIMAL(20,8)", False),
    "fx_rate_version_id": ("BIGINT", True),
    "fx_rate_applied": ("DECIMAL(24,10)", True),
    "reconciled_cost_myr": ("DECIMAL(20,8)", False),
    "coverage_method": ("VARCHAR(32)", False),
    "estimated_event_count": ("BIGINT", False),
    "unprocessed_event_count": ("BIGINT", False),
    "outbox_unsettled_count": ("BIGINT", False),
    "outbox_discarded_count": ("BIGINT", False),
    "estimated_source_cost": ("DECIMAL(20,8)", False),
    "estimated_cost_myr": ("DECIMAL(20,8)", False),
    "supersedes_id": ("BIGINT", True),
    "reason": ("VARCHAR(255)", True),
    "created_by": ("BIGINT", False),
    "created_at": ("DATETIME", False),
    "root_slot": ("INTEGER", True),
}
_PRIMARY_KEY: tuple[str, ...] = ("id",)

# 生成列表达式（设计 §2）：根为 1，取代行一律为 NULL（不是 0）；唯一索引不管 NULL，只有根参与
# 唯一性（写法同 0015 的 `_OPEN_SLOT`）。
_ROOT_SLOT = "root_slot"
_ROOT_SLOT_EXPRESSION = "CASE WHEN supersedes_id IS NULL THEN 1 END"


def _expression_text(expression: str) -> str:
    """去掉空白、括号与反引号再大写：MySQL 存的是改写过的
    `(case when (`supersedes_id` is null) then 1 end)`，比的是这一层之下的文字。"""
    return re.sub(r"[\s()`]", "", expression).upper()


# 生成列名 → (规范化的表达式, 是否 STORED)。
_GENERATED: dict[str, tuple[str, bool]] = {
    _ROOT_SLOT: (_expression_text(_ROOT_SLOT_EXPRESSION), True),
}

_UNIQUE_ROOT = "ux_provider_cost_reconciliations_root"
_UNIQUE_PUBLIC_ID = "uq_provider_cost_reconciliations_public_id"
_UNIQUE_SUPERSEDES = "uq_provider_cost_reconciliations_supersedes"
_UNIQUE_CHAIN_KEY = "uq_provider_cost_reconciliations_chain_key"
_INDEX_FX_RATE_VERSION = "ix_provider_cost_reconciliations_fx_rate_version_id"
_INDEX_CREATED_BY = "ix_provider_cost_reconciliations_created_by"
# 索引名 → (列, 唯一)，按建的顺序；除主键外恰好这六个（设计 §2）。
_INDEXES: dict[str, tuple[tuple[str, ...], bool]] = {
    _UNIQUE_ROOT: (("provider_id", "scope", "billing_month", _ROOT_SLOT), True),
    _UNIQUE_PUBLIC_ID: (("public_id",), True),
    _UNIQUE_SUPERSEDES: (("supersedes_id", "provider_id", "scope", "billing_month"), True),
    _UNIQUE_CHAIN_KEY: (("id", "provider_id", "scope", "billing_month"), True),
    _INDEX_FX_RATE_VERSION: (("fx_rate_version_id",), False),
    _INDEX_CREATED_BY: (("created_by",), False),
}
# 唯一索引（不是唯一约束）：模型上是 `Index(unique=True)`，其他方言上 `create_table` 之后
# `create_index`。其余唯一的是 `UniqueConstraint`，在建表语句里。
_UNIQUE_INDEXES = (_UNIQUE_ROOT,)
# 普通索引：其他方言上 `create_table` 之后逐个 `create_index`。
_PLAIN_INDEXES = (_INDEX_FX_RATE_VERSION, _INDEX_CREATED_BY)

_FK_PROVIDER = "fk_provider_cost_reconciliations_provider_id"
_FK_FX_RATE_VERSION = "fk_provider_cost_reconciliations_fx_rate_version_id"
_FK_CREATED_BY = "fk_provider_cost_reconciliations_created_by"
_FK_SUPERSEDES = "fk_provider_cost_reconciliations_supersedes"
# 外键名 → (列, 引用表, 引用列, 删除规则)。每个外键在子表上都有前导列一致的索引（见 `_INDEXES`）：
# `provider_id` → `ux_…_root`；`fx_rate_version_id` → `ix_…_fx_rate_version_id`；`created_by` →
# `ix_…_created_by`；复合自引用 → `uq_…_supersedes`。
_FOREIGN_KEYS: dict[str, tuple[tuple[str, ...], str, tuple[str, ...], str]] = {
    _FK_PROVIDER: (("provider_id",), _AI_PROVIDERS, ("id",), "RESTRICT"),
    _FK_FX_RATE_VERSION: (("fx_rate_version_id",), _FX_RATE_VERSIONS, ("id",), "RESTRICT"),
    _FK_CREATED_BY: (("created_by",), _USERS, ("id",), "RESTRICT"),
    _FK_SUPERSEDES: (
        ("supersedes_id", "provider_id", "scope", "billing_month"),
        _TABLE,
        ("id", "provider_id", "scope", "billing_month"),
        "RESTRICT",
    ),
}


def _check(name: str) -> str:
    return f"ck_{_TABLE}_{name}"


# 名字 → 条件，恰好 13 条（设计 §2）。与 app/models/provider_cost_reconciliations.py 上的
# CheckConstraint 一致（空白除外）。逐列写 `IS NOT NULL`，不用行构造器（同
# app/models/usage.py 的 `PROCESSED_SNAPSHOT_CHECK` 注释的理由）。
_CHECKS: dict[str, str] = {
    _check("fx"): (
        "(source_currency = 'MYR' AND fx_rate_version_id IS NULL AND fx_rate_applied IS NULL"
        " AND reconciled_cost_myr = source_total)"
        " OR (source_currency <> 'MYR' AND fx_rate_version_id IS NOT NULL"
        " AND fx_rate_applied IS NOT NULL AND fx_rate_applied > 0)"
    ),
    _check("reason"): (
        "(supersedes_id IS NULL AND reason IS NULL)"
        " OR (supersedes_id IS NOT NULL AND reason IS NOT NULL)"
    ),
    _check("scope"): "scope IN ('AI_MODULE')",
    _check("coverage_method"): "coverage_method IN ('PERIOD_AGGREGATE')",
    _check("period"): "period_start < period_end",
    _check("source_total"): "source_total >= 0",
    _check("reconciled_cost"): "reconciled_cost_myr >= 0",
    _check("estimated_source_cost"): "estimated_source_cost >= 0",
    _check("estimated_cost_myr"): "estimated_cost_myr >= 0",
    _check("estimated_event_count"): "estimated_event_count >= 0",
    _check("unprocessed_event_count"): "unprocessed_event_count >= 0",
    _check("outbox_unsettled_count"): "outbox_unsettled_count >= 0",
    _check("outbox_discarded_count"): "outbox_discarded_count >= 0",
}

# 设计的形状（第 1 步逐项比）。
_SHAPE: dict[str, object] = {
    "columns": _COLUMNS,
    "generated": _GENERATED,
    "primary_key": _PRIMARY_KEY,
    "indexes": _INDEXES,
    "foreign_keys": _FOREIGN_KEYS,
    "checks": set(_CHECKS),
}


def _constraints_ddl() -> list[str]:
    uniques = [
        f"    UNIQUE INDEX {name} ({', '.join(columns)})"
        if name in _UNIQUE_INDEXES
        else f"    CONSTRAINT {name} UNIQUE ({', '.join(columns)})"
        for name, (columns, unique) in _INDEXES.items()
        if unique
    ]
    plain = [
        f"    INDEX {name} ({', '.join(columns)})"
        for name, (columns, unique) in _INDEXES.items()
        if not unique
    ]
    keys = [
        f"    CONSTRAINT {name} FOREIGN KEY ({', '.join(columns)})"
        f" REFERENCES {referred} ({', '.join(referred_columns)}) ON DELETE {rule}"
        for name, (columns, referred, referred_columns, rule) in _FOREIGN_KEYS.items()
    ]
    conditions = [
        f"    CONSTRAINT {name} CHECK ({condition})" for name, condition in _CHECKS.items()
    ]
    return [*uniques, *plain, *keys, *conditions]


# MySQL 上的建表语句：一条，原子。
_CREATE_TABLE_MYSQL = (
    f"CREATE TABLE {_TABLE} (\n"
    + ",\n".join(
        [
            "    id BIGINT NOT NULL AUTO_INCREMENT",
            "    public_id CHAR(36) NOT NULL",
            "    provider_id BIGINT NOT NULL",
            "    scope VARCHAR(16) NOT NULL",
            "    billing_month CHAR(7) NOT NULL",
            "    period_start DATETIME NOT NULL",
            "    period_end DATETIME NOT NULL",
            "    source_document_reference VARCHAR(255) NOT NULL",
            "    source_currency CHAR(3) NOT NULL",
            "    source_total DECIMAL(20, 8) NOT NULL",
            "    fx_rate_version_id BIGINT NULL",
            "    fx_rate_applied DECIMAL(24, 10) NULL",
            "    reconciled_cost_myr DECIMAL(20, 8) NOT NULL",
            "    coverage_method VARCHAR(32) NOT NULL",
            "    estimated_event_count BIGINT NOT NULL",
            "    unprocessed_event_count BIGINT NOT NULL",
            "    outbox_unsettled_count BIGINT NOT NULL",
            "    outbox_discarded_count BIGINT NOT NULL",
            "    estimated_source_cost DECIMAL(20, 8) NOT NULL",
            "    estimated_cost_myr DECIMAL(20, 8) NOT NULL",
            "    supersedes_id BIGINT NULL",
            "    reason VARCHAR(255) NULL",
            "    created_by BIGINT NOT NULL",
            "    created_at DATETIME NOT NULL",
            f"    {_ROOT_SLOT} INT GENERATED ALWAYS AS ({_ROOT_SLOT_EXPRESSION}) STORED",
            "    PRIMARY KEY (id)",
            *_constraints_ddl(),
        ]
    )
    + "\n)"
)

# 两个触发器共用的主体（设计 §2「数据库」）：无条件拒绝。MySQL 把 `FOR EACH ROW` 之后的部分原样
# 存进 ACTION_STATEMENT，第 2 步去掉首尾空白后与它逐字比较。
_TRIGGER_BODY = """
BEGIN
    SIGNAL SQLSTATE '45000'
        SET MESSAGE_TEXT = 'provider_cost_reconciliations rows are immutable';
END
""".strip()

_UPDATE_TRIGGER = "trg_provider_cost_reconciliations_before_update"
_DELETE_TRIGGER = "trg_provider_cost_reconciliations_before_delete"
# 触发器名 → (时机, 事件)，按建的顺序。
_TRIGGER_EVENTS: dict[str, tuple[str, str]] = {
    _UPDATE_TRIGGER: ("BEFORE", "UPDATE"),
    _DELETE_TRIGGER: ("BEFORE", "DELETE"),
}


def _create_trigger(name: str) -> str:
    """整条 CREATE TRIGGER 是**一条**语句（与 0039 同一写法）。"""
    timing, event = _TRIGGER_EVENTS[name]
    return f"""
CREATE TRIGGER {name}
{timing} {event} ON {_TABLE}
FOR EACH ROW
{_TRIGGER_BODY}
"""


_TRIGGERS: dict[str, str] = {name: _create_trigger(name) for name in _TRIGGER_EVENTS}

_PREREQUISITE_QUERY = "SELECT @@log_bin, @@log_bin_trust_function_creators"
_PREREQUISITE_ERROR = (
    "MySQL has binary logging on and log_bin_trust_function_creators OFF, "
    + "so this account cannot create the provider cost reconciliation triggers (ERROR 1419). "
    + "Set log_bin_trust_function_creators = ON first; nothing has been changed."
)

# MySQL 上外键的删除规则以 information_schema 为准（与 0039 同一个来源）：反射不返回缺省的
# RESTRICT。
_DELETE_RULES_QUERY = (
    "SELECT CONSTRAINT_NAME, DELETE_RULE FROM information_schema.REFERENTIAL_CONSTRAINTS"
    f" WHERE CONSTRAINT_SCHEMA = DATABASE() AND TABLE_NAME = '{_TABLE}'"
)
# MySQL 上生成列以 information_schema 为准：`EXTRA` 是 `STORED GENERATED` 或 `VIRTUAL GENERATED`。
_GENERATED_QUERY = (
    "SELECT COLUMN_NAME, GENERATION_EXPRESSION, EXTRA FROM information_schema.COLUMNS"
    f" WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = '{_TABLE}'"
    " AND EXTRA IN ('STORED GENERATED', 'VIRTUAL GENERATED')"
)
# 按名字找：同名触发器挂在别的表上也要看见（触发器名在库内唯一）。
_TRIGGERS_QUERY = (
    "SELECT TRIGGER_NAME, EVENT_OBJECT_TABLE, ACTION_TIMING, EVENT_MANIPULATION, ACTION_STATEMENT"
    " FROM information_schema.TRIGGERS WHERE TRIGGER_SCHEMA = DATABASE()"
    f" AND TRIGGER_NAME IN ('{_UPDATE_TRIGGER}', '{_DELETE_TRIGGER}')"
)

# 降级前的数据检查（设计 §8）。
_HAS_ROWS = f"SELECT 1 FROM {_TABLE} LIMIT 1"

_DESIGN = "docs/design/AIH-TASK-242-provider-cost-reconciliation.md"


def _is_mysql() -> bool:
    return op.get_context().dialect.name == "mysql"


def _type_text(column_type: object) -> str:
    """`VARCHAR(16) COLLATE …` → `VARCHAR(16)`：只留类型与宽度。"""
    return str(column_type).replace(", ", ",").split(" ")[0].upper()


def _generated_columns(bind: sa.engine.Connection, columns: list[dict]) -> dict[str, tuple]:
    """生成列名 → (规范化的表达式, 是否 STORED)。"""
    if _is_mysql():
        return {
            str(name): (_expression_text(str(expression)), str(extra).upper() == "STORED GENERATED")
            for name, expression, extra in bind.execute(sa.text(_GENERATED_QUERY)).all()
        }
    generated = {}
    for column in columns:
        computed = column.get("computed")
        if computed:
            generated[str(column["name"])] = (
                _expression_text(str(computed["sqltext"])),
                bool(computed.get("persisted")),
            )
    return generated


def _reflected_shape() -> dict | None:
    """表的现状（列、生成列、主键、索引、外键、CHECK 名）；表不存在时返回 None。"""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table(_TABLE):
        return None

    reflected_columns = inspector.get_columns(_TABLE)
    columns = {
        str(column["name"]): (_type_text(column["type"]), bool(column["nullable"]))
        for column in reflected_columns
    }
    primary_key = tuple(inspector.get_pk_constraint(_TABLE).get("constrained_columns") or ())
    # 前缀长度只在 `dialect_options` 的 `mysql_length` 里，`column_names`
    # 看不出来：带前缀的记成另一种形状。唯一约束在 MySQL 上就是唯一索引；
    # SQLite 只在 get_unique_constraints 里有。
    indexes: dict[str, tuple[tuple[str, ...], bool] | str] = {}
    for index in inspector.get_indexes(_TABLE):
        if (index.get("dialect_options") or {}).get("mysql_length"):
            indexes[str(index["name"])] = "prefix length"
        else:
            indexes[str(index["name"])] = (
                tuple(index["column_names"]),
                bool(index.get("unique")),
            )
    for unique in inspector.get_unique_constraints(_TABLE):
        indexes.setdefault(str(unique["name"]), (tuple(unique["column_names"]), True))

    if _is_mysql():
        delete_rules = dict(bind.execute(sa.text(_DELETE_RULES_QUERY)).all())
    else:
        delete_rules = {}
    foreign_keys = {}
    for key in inspector.get_foreign_keys(_TABLE):
        name = str(key["name"])
        rule = delete_rules.get(name, (key.get("options") or {}).get("ondelete"))
        foreign_keys[name] = (
            tuple(key["constrained_columns"]),
            str(key["referred_table"]),
            tuple(key["referred_columns"]),
            str(rule or "").upper(),
        )

    checks = {str(check["name"]) for check in inspector.get_check_constraints(_TABLE)}
    return {
        "columns": columns,
        "generated": _generated_columns(bind, reflected_columns),
        "primary_key": primary_key,
        "indexes": indexes,
        "foreign_keys": foreign_keys,
        "checks": checks,
    }


def _shape_problems(shape: dict) -> list[str]:
    """与设计不一致的项；空列表 = 形状正确。不比服务端默认值（设计 §2）。"""
    problems = []
    if shape["columns"] != _SHAPE["columns"]:
        problems.append("columns (name, type and width, nullability)")
    if shape["generated"] != _SHAPE["generated"]:
        problems.append(f"generated column {_ROOT_SLOT} (STORED, {_ROOT_SLOT_EXPRESSION})")
    if shape["primary_key"] != _SHAPE["primary_key"]:
        problems.append("primary key")
    if shape["indexes"] != _SHAPE["indexes"]:
        problems.append("indexes (name, columns and their order, uniqueness)")
    if shape["foreign_keys"] != _SHAPE["foreign_keys"]:
        problems.append("foreign keys (columns, referred table, ON DELETE RESTRICT)")
    if shape["checks"] != _SHAPE["checks"]:
        problems.append("check constraints")
    return problems


def _triggers(bind: sa.engine.Connection) -> dict[str, tuple[str, str, str, str]]:
    """这两个名字的现有触发器：名字 → (所在表, 时机, 事件, ACTION_STATEMENT)。"""
    return {
        str(name): (str(table), str(timing), str(kind), str(statement))
        for name, table, timing, kind, statement in bind.execute(sa.text(_TRIGGERS_QUERY)).all()
    }


def _trigger_matches(name: str, row: tuple[str, str, str, str]) -> bool:
    """在本表上、时机与事件是设计的，主体与 `_TRIGGER_BODY` 逐字相同（只去掉首尾空白）。"""
    table, timing, kind, body = row
    return (
        table == _TABLE
        and (timing, kind) == _TRIGGER_EVENTS[name]
        and body.strip() == _TRIGGER_BODY
    )


def _require_trigger_privilege(bind: sa.engine.Connection) -> None:
    """Refuse to run where the migration account could not create triggers (as 0039)."""
    log_bin, trusted = bind.execute(sa.text(_PREREQUISITE_QUERY)).one()
    if int(log_bin) == 1 and int(trusted) != 1:
        raise RuntimeError(_PREREQUISITE_ERROR)


def _refuse_shape(problems: list[str]) -> NoReturn:
    raise RuntimeError(
        f"Cannot upgrade to 0041: table {_TABLE} exists but differs from the design in "
        + "; ".join(problems)
        + f". No DDL was issued. First check that {_TABLE} holds no row. If it is empty, drop the"
        f" table by hand (DROP TABLE {_TABLE}) and upgrade again; if any row is there, stop and"
        " handle it as a data incident: do not drop it"
    )


def _refuse_triggers(names: list[str]) -> NoReturn:
    raise RuntimeError(
        "Cannot upgrade to 0041: "
        + "; ".join(
            f"trigger {name} exists but is not the one 0041 builds (table {_TABLE},"
            f" {' '.join(_TRIGGER_EVENTS[name])}, body word for word)"
            for name in names
        )
        + f". No DDL was issued. Have a person compare it with {_DESIGN} §2; once it is known to"
        " be hand-made, DROP TRIGGER it and upgrade again: only the missing trigger is built"
    )


def _create_table() -> None:
    if _is_mysql():
        op.execute(_CREATE_TABLE_MYSQL)
        return
    op.create_table(
        _TABLE,
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("public_id", sa.CHAR(length=36), nullable=False),
        sa.Column("provider_id", sa.BigInteger(), nullable=False),
        sa.Column("scope", sa.String(length=16), nullable=False),
        sa.Column("billing_month", sa.CHAR(length=7), nullable=False),
        sa.Column("period_start", sa.DateTime(), nullable=False),
        sa.Column("period_end", sa.DateTime(), nullable=False),
        sa.Column("source_document_reference", sa.String(length=255), nullable=False),
        sa.Column("source_currency", sa.CHAR(length=3), nullable=False),
        sa.Column("source_total", sa.Numeric(precision=20, scale=8), nullable=False),
        sa.Column("fx_rate_version_id", sa.BigInteger(), nullable=True),
        sa.Column("fx_rate_applied", sa.Numeric(precision=24, scale=10), nullable=True),
        sa.Column("reconciled_cost_myr", sa.Numeric(precision=20, scale=8), nullable=False),
        sa.Column("coverage_method", sa.String(length=32), nullable=False),
        sa.Column("estimated_event_count", sa.BigInteger(), nullable=False),
        sa.Column("unprocessed_event_count", sa.BigInteger(), nullable=False),
        sa.Column("outbox_unsettled_count", sa.BigInteger(), nullable=False),
        sa.Column("outbox_discarded_count", sa.BigInteger(), nullable=False),
        sa.Column("estimated_source_cost", sa.Numeric(precision=20, scale=8), nullable=False),
        sa.Column("estimated_cost_myr", sa.Numeric(precision=20, scale=8), nullable=False),
        sa.Column("supersedes_id", sa.BigInteger(), nullable=True),
        sa.Column("reason", sa.String(length=255), nullable=True),
        sa.Column("created_by", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column(_ROOT_SLOT, sa.Integer(), sa.Computed(_ROOT_SLOT_EXPRESSION, persisted=True)),
        sa.PrimaryKeyConstraint("id"),
        *(
            sa.UniqueConstraint(*columns, name=name)
            for name, (columns, unique) in _INDEXES.items()
            if unique and name not in _UNIQUE_INDEXES
        ),
        *(
            sa.ForeignKeyConstraint(
                list(columns),
                [f"{referred}.{column}" for column in referred_columns],
                name=name,
                ondelete=rule,
            )
            for name, (columns, referred, referred_columns, rule) in _FOREIGN_KEYS.items()
        ),
        *(sa.CheckConstraint(condition, name=name) for name, condition in _CHECKS.items()),
    )
    for name in (*_UNIQUE_INDEXES, *_PLAIN_INDEXES):
        columns, unique = _INDEXES[name]
        op.create_index(name, _TABLE, list(columns), unique=unique)


def _upgrade_offline() -> None:
    # 离线读不了库：按「什么都没做过」输出（照 0039）。触发器只在 MySQL 上有。
    _create_table()
    if _is_mysql():
        for name in _TRIGGER_EVENTS:
            op.execute(_TRIGGERS[name])


def upgrade() -> None:
    if op.get_context().as_sql:
        _upgrade_offline()
        return

    bind = op.get_bind()
    mysql = _is_mysql()
    # ⚠️ 先判、再写：任何一项不对都不动库，版本也不前进。
    # 第 1 步。
    shape = _reflected_shape()
    if shape is not None:
        problems = _shape_problems(shape)
        if problems:
            _refuse_shape(problems)
    # 第 2 步。表不存在时，同名触发器只可能挂在别的表上：同样拒绝。
    triggers = _triggers(bind) if mysql else {}
    wrong = [name for name, row in triggers.items() if not _trigger_matches(name, row)]
    if wrong:
        _refuse_triggers(sorted(wrong, key=list(_TRIGGER_EVENTS).index))
    missing = [name for name in _TRIGGER_EVENTS if name not in triggers] if mysql else []
    create_table = shape is None
    # 第 3 步：已完成。
    if not create_table and not missing:
        return

    # 第 4 步。⚠️ 只在要发 DDL 时，紧接在第一条 DDL 之前。
    if mysql:
        _require_trigger_privilege(bind)
    # 第 5 步：缺什么建什么。
    if create_table:
        _create_table()
    for name in missing:
        op.execute(_TRIGGERS[name])


def downgrade() -> None:
    # ⚠️ 离线模式判断不了表是否为空：拒绝，不输出任何 DROP（设计 §8）。
    if op.get_context().as_sql:
        raise RuntimeError(
            f"Cannot downgrade 0041 in --sql offline mode: whether table {_TABLE} is empty can"
            " only be checked against the database. Run the downgrade online"
        )
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    # 表已不在：不发 DDL。
    if not inspector.has_table(_TABLE):
        return
    # ⚠️ 在任何 DDL 之前：有记录就拒绝，删表等于删掉永久保留的财务记录（设计 §6、§8）。
    if bind.execute(sa.text(_HAS_ROWS)).first() is not None:
        raise RuntimeError(
            f"Cannot downgrade 0041: table {_TABLE} is not empty (provider cost reconciliations"
            " are financial records kept for good). No DDL was issued and no row was deleted."
            f" Keep the table {_TABLE} and roll back only the application image"
        )
    if _is_mysql():
        # 先删触发器、再删表；只删在这张表上、确实存在的（补完中断的升级之后也能降级）。
        present = {name for name, row in _triggers(bind).items() if row[0] == _TABLE}
        for name in _TRIGGER_EVENTS:
            if name in present:
                op.execute(f"DROP TRIGGER {name}")
    op.drop_table(_TABLE)

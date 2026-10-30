"""pricing rules get a project scope: project_id, eight-level checks and scope key

照设计闸门 #507 v4（docs/design/AIH-TASK-136-pricing-project-scope.md）§2「数据库」与 §8；
本迁移是 AIH-TASK-136。只改 `pricing_rules` 的结构与它的 BEFORE UPDATE 触发器：

- 新列 `project_id BIGINT NULL`，紧接 `tenant_id`（`AFTER tenant_id`，与模型的列序一致）；
- 复合外键 `fk_pricing_rules_project (project_id, tenant_id)` → `projects (id, tenant_id)`
  `ON DELETE RESTRICT`，与它在子表上的索引 `ix_pricing_rules_project (project_id, tenant_id)`；
- `ck_pricing_rules_priority_scope` 改为八个值，`ck_pricing_rules_scope_columns` 改为八种
  四列 NULL 组合（三个项目级在 §16 的五级之前）；
- 生成列 `scope_key` 加上 `COALESCE(project_id, 0)` 一段，`ux_pricing_rules_open_slot` 随之重建；
- `trg_pricing_rules_before_update` 重建：② 的不可变列加 `project_id`，④ 现算的范围键加项目
  一段，其余与 0014 逐字相同。

`app/models/pricing_rules.py` 同步了列、外键、索引、两条 CHECK 与 `scope_key` 表达式，
tests/backend/test_migrations.py 与 tests/backend/test_pricing_rule_project_scope_migration.py
比对两边。`PricingScope` 本任务不改：应用层还建不出、也读不出项目级规则（AIH-TASK-137 起）。

步骤（升级，MySQL）
------------------

在线时**先判断是否已完成**：`project_id` 列存在、形状核对通过（第 3 步的标记）、主触发器含
`NEW.project_id`、两个隔离触发器都不存在 → 直接返回，不发任何 DDL。否则依次：

1. **预检**：查 `@@log_bin` 与 `@@log_bin_trust_function_creators`（查询、判定与报错文案照
   0014）。binlog 开着而开关关着时，应用账号建不了触发器（ERROR 1419），所以**在任何 DDL 之前**
   抛错。`project_id` 列已存在时紧接着核对形状（标记见 `_shape_problems`）：任何一项不符就抛错、
   不发任何 DDL，已有的隔离触发器原样留下（只可能是手工改过表，需要人来看）。
2. **写入隔离**：建 `trg_pricing_rules_migration_guard_insert`（BEFORE INSERT）与
   `trg_pricing_rules_migration_guard_update`（BEFORE UPDATE），正文只有一句 SIGNAL
   `'pricing_rules is being migrated'`，用 `PRECEDES` 排在同一时机的主触发器之前（主触发器不存在
   时不带）。两个各自按是否存在判定，已存在的跳过；第 3、4 步都已完成时整步跳过。从这里到第 5 步，
   对 `pricing_rules` 的任何 INSERT 与 UPDATE 都被拒绝（DELETE 本来就被 0014 拒绝）。
3. **表**：`project_id` 列不存在时，用**一条** `ALTER TABLE` 完成全部表结构改动
   （`_ALTER_UPGRADE`）；列存在就跳过（MySQL 8 的 InnoDB DDL 是原子的，
   「列存在」就是这一步已完成的标志）。
4. **主触发器**：`ACTION_STATEMENT` 含 `NEW.project_id` → 跳过；存在但不含 → `DROP TRIGGER` 后
   `CREATE`；不存在（上一次在两者之间中断）→ 直接 `CREATE`。
5. **解除隔离**：先查两个隔离触发器各自是否存在，只对存在的发 `DROP TRIGGER`。

`--sql` 离线模式读不了库：照 0014 与 0027 的惯例跳过预检与全部检查，按「什么都没做过」依次输出
建两个隔离触发器、那条 ALTER、主触发器的 `DROP TRIGGER IF EXISTS` 与 `CREATE TRIGGER`、删两个
隔离触发器。

§132 第 13 条分析
-----------------

**已有数据**：**不改任何已有行的业务列**，不回填、不删行。新列 `project_id` 对已有行全为 NULL；
已有行全部满足新的两条 CHECK（五种旧组合是八种新组合的子集）；生成列 `scope_key` 随表达式重算，
**已有行的 `scope_key` 只多出一段 `:0`**（例如 `GLOBAL:0:7:0` → `GLOBAL:0:0:7:0`），彼此仍不同，
唯一索引重建不会冲突；`open_slot` 不变。规则的 `id`、`public_id` 不变，用量事件的
`pricing_rule_id` 与分量表的外键不动（INV-6）。

**锁表与停机**：`MODIFY COLUMN … STORED` 与加外键让这条 ALTER 重建 `pricing_rules`、持元数据锁：
正在进行的计费事务持有规则行的共享锁，ALTER 等它们提交；进行期间新的计费在元数据锁上排队，不丢
事件。规则表是管理员手建的小表（生产上至多数十行），重建在秒级完成。加外键时对 `projects` 取短暂
的元数据锁。写入隔离期间（第 2 步到第 5 步，秒级）管理员的建草稿、改草稿、发布、停用、丢弃得到
错误、什么都不写；计费只读这张表，不受影响。不需要停机。

**备份**：要求前置备份（与 0005–0029 同一口径）。本版不写任何业务数据。

**部署顺序**：`deploy.sh` 先迁移、再换镜像（设计 §8）。迁移之后、新容器起来之前，旧代码照旧运行：
旧代码不写 `project_id`，插入的五级规则满足新 CHECK；旧代码按五级解析，表里没有项目级规则，结果
相同。本任务的新代码同样不写、不读项目级规则。之后依次是 AIH-TASK-137、138、139、140。

**失败处理**：⚠️ MySQL 的 DDL **不参与事务**，每一步与写 `alembic_version` 之间都有失败窗口；
每一步都可以安全重跑（设计 §5「迁移中途失败」）：

- 预检失败：什么都没改，`alembic_version` 仍是 `0029_wallet_reconciliation`。给账号打开
  `log_bin_trust_function_creators` 后重新部署。
- 第 2 步之后任何一步失败、或进程在中途被杀：隔离触发器留在库里，`pricing_rules` 拒绝一切 INSERT
  与 UPDATE（数据库层的不可变与区间不重叠因此不会被绕过）；表的那一步要么完全没改、要么全部改完；
  主触发器可能是旧的、或不存在（DROP 之后中断）。修好原因后原样重新部署：隔离触发器已存在就跳过，
  表按「`project_id` 列存在」跳过，主触发器按 `NEW.project_id` 标记跳过或补建，最后删隔离触发器。
  隔离触发器还在，就说明迁移没有完成。
- 全部完成、写 `alembic_version` 之前进程退出：重新部署时判为已完成，不发任何 DDL，只把版本前进。
- 形状核对不符（手工改过表）：抛错、不发任何 DDL，版本仍是 0029，需要人对照设计查看这张表。

**回滚**：代码回滚到本迁移之前要先降级。`downgrade` 同样分步、可重跑：
`--sql` 离线模式**最先抛错**（读不了库，既判断不了是否已完成，也判断不了有没有项目级规则）。在线时
先判断是否已完成（列不存在、主触发器存在且不含 `project_id`、两个隔离触发器都不存在 → 直接返回）；
否则依次：预检；写入隔离（同升级第 2 步；第 4、5 步都已完成时跳过）；列存在时，表里有任何
`project_id IS NOT NULL` 或 `priority_scope` 为三个项目级之一的行，就先删隔离触发器、再抛错，表与
触发器回到降级之前的样子——这些规则可能已被用量事件的 `pricing_rule_id` 引用（INV-6），只能前滚
修复；列存在时用一条 ALTER 恢复 0014 的两条 CHECK、`scope_key` 表达式与唯一索引并删外键、索引与列；
主触发器恢复为 0014 的正文（存在且不含 `project_id` → 跳过）；最后删隔离触发器。⚠️ 顺序必须是
**先改表、后换触发器**（设计 §2「为什么降级先改表、后换触发器」）：ALTER 失败时表完全不变，0030 的
触发器照旧与表一致；ALTER 完成、触发器还没换时，0030 的触发器引用的 `NEW.project_id` 已不存在，
每一次 UPDATE 都报错（ERROR 1054，失败即拒绝），直到重跑降级换上 0014 的触发器。

Revision ID: 0030_pricing_rule_project_scope
Revises: 0029_wallet_reconciliation
Create Date: 2026-10-30
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0030_pricing_rule_project_scope"
down_revision: str | None = "0029_wallet_reconciliation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_RULES = "pricing_rules"
_PROJECT_COLUMN = "project_id"
_FK_PROJECT = "fk_pricing_rules_project"
_INDEX_PROJECT = "ix_pricing_rules_project"
_OPEN_SLOT_INDEX = "ux_pricing_rules_open_slot"
_PRIORITY_SCOPE_CHECK = "ck_pricing_rules_priority_scope"
_SCOPE_COLUMNS_CHECK = "ck_pricing_rules_scope_columns"
# 三个项目级（降级时据此判断表里有没有项目级规则）。
_PROJECT_SCOPES = ("PROJECT_PROVIDER_MODEL", "PROJECT_PROVIDER", "PROJECT")

_INSERT_TRIGGER = "trg_pricing_rules_before_insert"
_UPDATE_TRIGGER = "trg_pricing_rules_before_update"
# 升级第 4 步的「已完成」标记；降级第 5 步看的是不含 `project_id`。
_UPGRADED_TRIGGER_MARK = "NEW.project_id"
_DOWNGRADED_TRIGGER_MARK = "project_id"

_PREREQUISITE_QUERY = "SELECT @@log_bin, @@log_bin_trust_function_creators"
_PREREQUISITE_ERROR = (
    "MySQL has binary logging on and log_bin_trust_function_creators OFF, "
    + "so this account cannot create the pricing rule triggers (ERROR 1419). "
    + "Set log_bin_trust_function_creators = ON first; nothing has been changed."
)

# ⚠️ 下面都是冻结的字面量：两条 CHECK 与 `scope_key` 与 app/models/pricing_rules.py 的
# PRIORITY_SCOPE_CHECK、SCOPE_COLUMNS_CHECK、SCOPE_KEY_EXPRESSION 逐字一致（空白除外），
# tests/backend/test_migrations.py 比对两边。

# 名字 → 条件（0030 之后）。
_CHECKS: dict[str, str] = {
    _PRIORITY_SCOPE_CHECK: (
        "priority_scope IN ('PROJECT_PROVIDER_MODEL', 'PROJECT_PROVIDER', 'PROJECT',"
        " 'CUSTOMER_PROVIDER_MODEL', 'CUSTOMER_PROVIDER', 'CUSTOMER',"
        " 'GLOBAL_PROVIDER_MODEL', 'GLOBAL')"
    ),
    _SCOPE_COLUMNS_CHECK: (
        "(priority_scope = 'PROJECT_PROVIDER_MODEL' AND tenant_id IS NOT NULL"
        " AND project_id IS NOT NULL AND provider_id IS NOT NULL AND model_id IS NOT NULL)"
        " OR (priority_scope = 'PROJECT_PROVIDER' AND tenant_id IS NOT NULL"
        " AND project_id IS NOT NULL AND provider_id IS NOT NULL AND model_id IS NULL)"
        " OR (priority_scope = 'PROJECT' AND tenant_id IS NOT NULL"
        " AND project_id IS NOT NULL AND provider_id IS NULL AND model_id IS NULL)"
        " OR (priority_scope = 'CUSTOMER_PROVIDER_MODEL' AND tenant_id IS NOT NULL"
        " AND project_id IS NULL AND provider_id IS NOT NULL AND model_id IS NOT NULL)"
        " OR (priority_scope = 'CUSTOMER_PROVIDER' AND tenant_id IS NOT NULL"
        " AND project_id IS NULL AND provider_id IS NOT NULL AND model_id IS NULL)"
        " OR (priority_scope = 'CUSTOMER' AND tenant_id IS NOT NULL"
        " AND project_id IS NULL AND provider_id IS NULL AND model_id IS NULL)"
        " OR (priority_scope = 'GLOBAL_PROVIDER_MODEL' AND tenant_id IS NULL"
        " AND project_id IS NULL AND provider_id IS NOT NULL AND model_id IS NOT NULL)"
        " OR (priority_scope = 'GLOBAL' AND tenant_id IS NULL"
        " AND project_id IS NULL AND provider_id IS NULL AND model_id IS NULL)"
    ),
}
# 降级恢复的 0014 原文（与 0014 的 _CHECKS 逐字一致，测试比对）。
_CHECKS_0014: dict[str, str] = {
    _PRIORITY_SCOPE_CHECK: (
        "priority_scope IN ('CUSTOMER_PROVIDER_MODEL', 'CUSTOMER_PROVIDER', 'CUSTOMER',"
        " 'GLOBAL_PROVIDER_MODEL', 'GLOBAL')"
    ),
    _SCOPE_COLUMNS_CHECK: (
        "(priority_scope = 'CUSTOMER_PROVIDER_MODEL' AND tenant_id IS NOT NULL"
        " AND provider_id IS NOT NULL AND model_id IS NOT NULL)"
        " OR (priority_scope = 'CUSTOMER_PROVIDER' AND tenant_id IS NOT NULL"
        " AND provider_id IS NOT NULL AND model_id IS NULL)"
        " OR (priority_scope = 'CUSTOMER' AND tenant_id IS NOT NULL"
        " AND provider_id IS NULL AND model_id IS NULL)"
        " OR (priority_scope = 'GLOBAL_PROVIDER_MODEL' AND tenant_id IS NULL"
        " AND provider_id IS NOT NULL AND model_id IS NOT NULL)"
        " OR (priority_scope = 'GLOBAL' AND tenant_id IS NULL"
        " AND provider_id IS NULL AND model_id IS NULL)"
    ),
}

# 生成列表达式：0030 之后与降级恢复的 0014 原文。
_SCOPE_KEY = (
    "CONCAT(priority_scope, ':', COALESCE(tenant_id, 0), ':', COALESCE(project_id, 0),"
    " ':', COALESCE(provider_id, 0), ':', COALESCE(model_id, 0))"
)
_SCOPE_KEY_0014 = (
    "CONCAT(priority_scope, ':', COALESCE(tenant_id, 0), ':', COALESCE(provider_id, 0),"
    " ':', COALESCE(model_id, 0))"
)
_SCOPE_KEY_LENGTH = 128


def _check_clauses(checks: dict[str, str]) -> str:
    """同名 CHECK 先删后加（同一条 ALTER 里）。"""
    return ",\n".join(
        f"    DROP CHECK {name},\n    ADD CONSTRAINT {name} CHECK ({condition})"
        for name, condition in checks.items()
    )


def _scope_key_clauses(expression: str) -> str:
    """唯一索引先删、生成列改表达式、再按同名加回唯一索引。"""
    return (
        f"    DROP INDEX {_OPEN_SLOT_INDEX},\n"
        f"    MODIFY COLUMN scope_key VARCHAR({_SCOPE_KEY_LENGTH})"
        f" GENERATED ALWAYS AS ({expression}) STORED,\n"
        f"    ADD UNIQUE INDEX {_OPEN_SLOT_INDEX} (scope_key, open_slot)"
    )


# 升级第 3 步：**一条** ALTER，原子。
_ALTER_UPGRADE = f"""
ALTER TABLE {_RULES}
    ADD COLUMN {_PROJECT_COLUMN} BIGINT NULL AFTER tenant_id,
    ADD INDEX {_INDEX_PROJECT} ({_PROJECT_COLUMN}, tenant_id),
    ADD CONSTRAINT {_FK_PROJECT} FOREIGN KEY ({_PROJECT_COLUMN}, tenant_id)
        REFERENCES projects (id, tenant_id) ON DELETE RESTRICT,
{_check_clauses(_CHECKS)},
{_scope_key_clauses(_SCOPE_KEY)}
"""

# 降级第 4 步：同样一条 ALTER，恢复 0014 的两条 CHECK、生成列与唯一索引，再删外键、索引与列。
_ALTER_DOWNGRADE = f"""
ALTER TABLE {_RULES}
    DROP FOREIGN KEY {_FK_PROJECT},
    DROP INDEX {_INDEX_PROJECT},
{_check_clauses(_CHECKS_0014)},
{_scope_key_clauses(_SCOPE_KEY_0014)},
    DROP COLUMN {_PROJECT_COLUMN}
"""

# 触发器名 → 建触发器的语句（写法照 0014 的 _TRIGGERS：整条 CREATE TRIGGER 是一条语句）。
# 与 0014 的 `trg_pricing_rules_before_update` 只差两处（设计 §2）：② 的不可变列加
# `project_id`；④ 现算的范围键加 `COALESCE(NEW.project_id, 0)` 一段，与 `_SCOPE_KEY` 逐字一致
# （去掉 `NEW.` 之后）。其余检查（①、①′、③、⑤）与 0014 逐字相同。
_TRIGGERS: dict[str, str] = {
    _UPDATE_TRIGGER: """
CREATE TRIGGER trg_pricing_rules_before_update
BEFORE UPDATE ON pricing_rules
FOR EACH ROW
BEGIN
    DECLARE locked_id INT DEFAULT NULL;
    IF OLD.status = 'DISCARDED' THEN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'a discarded pricing rule is final';
    END IF;
    IF OLD.status = 'DRAFT' AND NEW.status NOT IN ('DRAFT', 'PUBLISHED', 'DISCARDED') THEN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'a draft pricing rule is published or discarded';
    END IF;
    IF OLD.status IN ('PUBLISHED', 'RETIRED') THEN
        IF NOT (NEW.id <=> OLD.id)
           OR NOT (NEW.public_id <=> OLD.public_id)
           OR NOT (NEW.priority_scope <=> OLD.priority_scope)
           OR NOT (NEW.tenant_id <=> OLD.tenant_id)
           OR NOT (NEW.project_id <=> OLD.project_id)
           OR NOT (NEW.provider_id <=> OLD.provider_id)
           OR NOT (NEW.model_id <=> OLD.model_id)
           OR NOT (NEW.strategy <=> OLD.strategy)
           OR NOT (NEW.markup_multiplier <=> OLD.markup_multiplier)
           OR NOT (NEW.effective_from <=> OLD.effective_from)
           OR NOT (NEW.created_by <=> OLD.created_by)
           OR NOT (NEW.approved_by <=> OLD.approved_by)
           OR NOT (NEW.created_at <=> OLD.created_at)
           OR NOT (NEW.approved_at <=> OLD.approved_at) THEN
            SIGNAL SQLSTATE '45000'
                SET MESSAGE_TEXT = 'a published pricing rule is immutable';
        END IF;
        IF NOT (NEW.status <=> OLD.status)
           AND NOT (OLD.status = 'PUBLISHED' AND NEW.status = 'RETIRED') THEN
            SIGNAL SQLSTATE '45000'
                SET MESSAGE_TEXT = 'a published pricing rule can only be retired';
        END IF;
    END IF;
    IF OLD.status = 'DRAFT' AND NEW.status = 'PUBLISHED' AND NEW.strategy = 'FIXED_RATE' THEN
        IF (SELECT COUNT(*)
              FROM pricing_rule_components
             WHERE pricing_rule_id = NEW.id) = 0 THEN
            SIGNAL SQLSTATE '45000'
                SET MESSAGE_TEXT = 'a fixed rate pricing rule needs at least one component';
        END IF;
        IF EXISTS (
            SELECT 1
              FROM usage_meter_components umc
             WHERE umc.meter_type_id IN (
                       SELECT m.meter_type_id
                         FROM pricing_rule_components c
                         JOIN usage_meter_components m ON m.id = c.usage_meter_component_id
                        WHERE c.pricing_rule_id = NEW.id)
               AND umc.id NOT IN (
                       SELECT usage_meter_component_id
                         FROM pricing_rule_components
                        WHERE pricing_rule_id = NEW.id)) THEN
            SIGNAL SQLSTATE '45000'
                SET MESSAGE_TEXT = 'a fixed rate pricing rule misses a meter type component';
        END IF;
    END IF;
    IF NEW.status IN ('PUBLISHED', 'RETIRED')
       AND (NOT (NEW.effective_from <=> OLD.effective_from)
            OR NOT (NEW.effective_to <=> OLD.effective_to)
            OR NOT (NEW.status <=> OLD.status)) THEN
        SELECT id INTO locked_id
          FROM pricing_rule_locks
         WHERE id = 1
           FOR UPDATE;
        IF (NEW.effective_from IS NULL OR NEW.effective_to IS NULL
            OR NEW.effective_from < NEW.effective_to)
           AND EXISTS (
            SELECT 1
              FROM pricing_rules o
             WHERE o.scope_key = CONCAT(NEW.priority_scope, ':', COALESCE(NEW.tenant_id, 0),
                                        ':', COALESCE(NEW.project_id, 0),
                                        ':', COALESCE(NEW.provider_id, 0),
                                        ':', COALESCE(NEW.model_id, 0))
               AND o.id <> NEW.id
               AND o.status IN ('PUBLISHED', 'RETIRED')
               AND (o.effective_from IS NULL OR o.effective_to IS NULL
                    OR o.effective_from < o.effective_to)
               AND COALESCE(o.effective_from, '1000-01-01')
                   < COALESCE(NEW.effective_to, '9999-12-31')
               AND COALESCE(NEW.effective_from, '1000-01-01')
                   < COALESCE(o.effective_to, '9999-12-31')) THEN
            SIGNAL SQLSTATE '45000'
                SET MESSAGE_TEXT = 'pricing rule periods overlap';
        END IF;
        IF OLD.status = 'DRAFT' AND NEW.status = 'PUBLISHED' AND NEW.effective_from IS NULL
           AND (NEW.priority_scope <> 'GLOBAL'
                OR EXISTS (
                    SELECT 1
                      FROM pricing_rules o
                     WHERE o.priority_scope = 'GLOBAL'
                       AND o.id <> NEW.id
                       AND o.status IN ('PUBLISHED', 'RETIRED'))) THEN
            SIGNAL SQLSTATE '45000'
                SET MESSAGE_TEXT = 'only the first global pricing rule starts with no time';
        END IF;
    END IF;
END
""",
}

# 降级恢复的 0014 原文（与 0014 的 _TRIGGERS 里同名的那条逐字一致，测试比对）。
_TRIGGERS_0014: dict[str, str] = {
    _UPDATE_TRIGGER: """
CREATE TRIGGER trg_pricing_rules_before_update
BEFORE UPDATE ON pricing_rules
FOR EACH ROW
BEGIN
    DECLARE locked_id INT DEFAULT NULL;
    IF OLD.status = 'DISCARDED' THEN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'a discarded pricing rule is final';
    END IF;
    IF OLD.status = 'DRAFT' AND NEW.status NOT IN ('DRAFT', 'PUBLISHED', 'DISCARDED') THEN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'a draft pricing rule is published or discarded';
    END IF;
    IF OLD.status IN ('PUBLISHED', 'RETIRED') THEN
        IF NOT (NEW.id <=> OLD.id)
           OR NOT (NEW.public_id <=> OLD.public_id)
           OR NOT (NEW.priority_scope <=> OLD.priority_scope)
           OR NOT (NEW.tenant_id <=> OLD.tenant_id)
           OR NOT (NEW.provider_id <=> OLD.provider_id)
           OR NOT (NEW.model_id <=> OLD.model_id)
           OR NOT (NEW.strategy <=> OLD.strategy)
           OR NOT (NEW.markup_multiplier <=> OLD.markup_multiplier)
           OR NOT (NEW.effective_from <=> OLD.effective_from)
           OR NOT (NEW.created_by <=> OLD.created_by)
           OR NOT (NEW.approved_by <=> OLD.approved_by)
           OR NOT (NEW.created_at <=> OLD.created_at)
           OR NOT (NEW.approved_at <=> OLD.approved_at) THEN
            SIGNAL SQLSTATE '45000'
                SET MESSAGE_TEXT = 'a published pricing rule is immutable';
        END IF;
        IF NOT (NEW.status <=> OLD.status)
           AND NOT (OLD.status = 'PUBLISHED' AND NEW.status = 'RETIRED') THEN
            SIGNAL SQLSTATE '45000'
                SET MESSAGE_TEXT = 'a published pricing rule can only be retired';
        END IF;
    END IF;
    IF OLD.status = 'DRAFT' AND NEW.status = 'PUBLISHED' AND NEW.strategy = 'FIXED_RATE' THEN
        IF (SELECT COUNT(*)
              FROM pricing_rule_components
             WHERE pricing_rule_id = NEW.id) = 0 THEN
            SIGNAL SQLSTATE '45000'
                SET MESSAGE_TEXT = 'a fixed rate pricing rule needs at least one component';
        END IF;
        IF EXISTS (
            SELECT 1
              FROM usage_meter_components umc
             WHERE umc.meter_type_id IN (
                       SELECT m.meter_type_id
                         FROM pricing_rule_components c
                         JOIN usage_meter_components m ON m.id = c.usage_meter_component_id
                        WHERE c.pricing_rule_id = NEW.id)
               AND umc.id NOT IN (
                       SELECT usage_meter_component_id
                         FROM pricing_rule_components
                        WHERE pricing_rule_id = NEW.id)) THEN
            SIGNAL SQLSTATE '45000'
                SET MESSAGE_TEXT = 'a fixed rate pricing rule misses a meter type component';
        END IF;
    END IF;
    IF NEW.status IN ('PUBLISHED', 'RETIRED')
       AND (NOT (NEW.effective_from <=> OLD.effective_from)
            OR NOT (NEW.effective_to <=> OLD.effective_to)
            OR NOT (NEW.status <=> OLD.status)) THEN
        SELECT id INTO locked_id
          FROM pricing_rule_locks
         WHERE id = 1
           FOR UPDATE;
        IF (NEW.effective_from IS NULL OR NEW.effective_to IS NULL
            OR NEW.effective_from < NEW.effective_to)
           AND EXISTS (
            SELECT 1
              FROM pricing_rules o
             WHERE o.scope_key = CONCAT(NEW.priority_scope, ':', COALESCE(NEW.tenant_id, 0),
                                        ':', COALESCE(NEW.provider_id, 0),
                                        ':', COALESCE(NEW.model_id, 0))
               AND o.id <> NEW.id
               AND o.status IN ('PUBLISHED', 'RETIRED')
               AND (o.effective_from IS NULL OR o.effective_to IS NULL
                    OR o.effective_from < o.effective_to)
               AND COALESCE(o.effective_from, '1000-01-01')
                   < COALESCE(NEW.effective_to, '9999-12-31')
               AND COALESCE(NEW.effective_from, '1000-01-01')
                   < COALESCE(o.effective_to, '9999-12-31')) THEN
            SIGNAL SQLSTATE '45000'
                SET MESSAGE_TEXT = 'pricing rule periods overlap';
        END IF;
        IF OLD.status = 'DRAFT' AND NEW.status = 'PUBLISHED' AND NEW.effective_from IS NULL
           AND (NEW.priority_scope <> 'GLOBAL'
                OR EXISTS (
                    SELECT 1
                      FROM pricing_rules o
                     WHERE o.priority_scope = 'GLOBAL'
                       AND o.id <> NEW.id
                       AND o.status IN ('PUBLISHED', 'RETIRED'))) THEN
            SIGNAL SQLSTATE '45000'
                SET MESSAGE_TEXT = 'only the first global pricing rule starts with no time';
        END IF;
    END IF;
END
""",
}

# 写入隔离（设计 §2 升级第 2 步）：隔离触发器名 → (事件, 排在它之后的主触发器)。按这个顺序建与删。
_GUARD_MESSAGE = "pricing_rules is being migrated"
_GUARDS: dict[str, tuple[str, str]] = {
    "trg_pricing_rules_migration_guard_insert": ("INSERT", _INSERT_TRIGGER),
    "trg_pricing_rules_migration_guard_update": ("UPDATE", _UPDATE_TRIGGER),
}

_TRIGGERS_QUERY = (
    "SELECT TRIGGER_NAME, ACTION_STATEMENT FROM information_schema.TRIGGERS"
    f" WHERE TRIGGER_SCHEMA = DATABASE() AND EVENT_OBJECT_TABLE = '{_RULES}'"
)
_COLUMNS_QUERY = (
    "SELECT COLUMN_NAME, GENERATION_EXPRESSION FROM information_schema.COLUMNS"
    f" WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = '{_RULES}'"
    f" AND COLUMN_NAME IN ('{_PROJECT_COLUMN}', 'scope_key')"
)
_FOREIGN_KEY_QUERY = (
    "SELECT k.COLUMN_NAME, k.REFERENCED_TABLE_NAME, k.REFERENCED_COLUMN_NAME, r.DELETE_RULE"
    " FROM information_schema.KEY_COLUMN_USAGE k"
    " JOIN information_schema.REFERENTIAL_CONSTRAINTS r"
    " ON r.CONSTRAINT_SCHEMA = k.CONSTRAINT_SCHEMA AND r.CONSTRAINT_NAME = k.CONSTRAINT_NAME"
    f" WHERE k.CONSTRAINT_SCHEMA = DATABASE() AND k.TABLE_NAME = '{_RULES}'"
    f" AND k.CONSTRAINT_NAME = '{_FK_PROJECT}'"
    " ORDER BY k.ORDINAL_POSITION"
)
_INDEX_QUERY = (
    "SELECT INDEX_NAME, NON_UNIQUE, COLUMN_NAME FROM information_schema.STATISTICS"
    f" WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = '{_RULES}'"
    f" AND INDEX_NAME IN ('{_INDEX_PROJECT}', '{_OPEN_SLOT_INDEX}')"
    " ORDER BY INDEX_NAME, SEQ_IN_INDEX"
)
# MySQL 的 CHECK 约束名在库内唯一。
_CHECK_CLAUSES_QUERY = (
    "SELECT CONSTRAINT_NAME, CHECK_CLAUSE FROM information_schema.CHECK_CONSTRAINTS"
    " WHERE CONSTRAINT_SCHEMA = DATABASE()"
    f" AND CONSTRAINT_NAME IN ('{_PRIORITY_SCOPE_CHECK}', '{_SCOPE_COLUMNS_CHECK}')"
)
_PROJECT_RULES_QUERY = (
    f"SELECT 1 FROM {_RULES} WHERE {_PROJECT_COLUMN} IS NOT NULL"
    f" OR priority_scope IN ({', '.join(repr(scope) for scope in _PROJECT_SCOPES)}) LIMIT 1"
)


def _guard_statement(name: str, precedes: str | None) -> str:
    """一个只 SIGNAL 的隔离触发器；`precedes` 是同一时机的主触发器（不存在时为 None）。"""
    event, _ = _GUARDS[name]
    order = f"PRECEDES {precedes}\n" if precedes is not None else ""
    return f"""
CREATE TRIGGER {name}
BEFORE {event} ON {_RULES}
FOR EACH ROW
{order}SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = '{_GUARD_MESSAGE}'
"""


def _drop_trigger(name: str) -> str:
    return f"DROP TRIGGER {name}"


def _require_trigger_privilege(bind: sa.engine.Connection) -> None:
    """Refuse to run where the migration account could not create triggers (as 0014)."""
    log_bin, trusted = bind.execute(sa.text(_PREREQUISITE_QUERY)).one()
    if int(log_bin) == 1 and int(trusted) != 1:
        raise RuntimeError(_PREREQUISITE_ERROR)


def _triggers(bind: sa.engine.Connection) -> dict[str, str]:
    """`pricing_rules` 上现有的触发器：名字 → ACTION_STATEMENT。"""
    return {
        str(name): str(statement)
        for name, statement in bind.execute(sa.text(_TRIGGERS_QUERY)).all()
    }


def _columns(bind: sa.engine.Connection) -> dict[str, str]:
    """`project_id` 与 `scope_key` 中存在的列：名字 → GENERATION_EXPRESSION（普通列为空串）。"""
    return {
        str(name): str(expression or "")
        for name, expression in bind.execute(sa.text(_COLUMNS_QUERY)).all()
    }


def _shape_problems(bind: sa.engine.Connection, columns: dict[str, str]) -> list[str]:
    """`project_id` 列存在时，第 3 步的标记里不符的项；空列表 = 形状核对通过。

    只查子串与列序，不比较 MySQL 改写过的全文（设计 §2 升级第 3 步）。
    """
    problems = []
    foreign_key = [tuple(row) for row in bind.execute(sa.text(_FOREIGN_KEY_QUERY)).all()]
    expected_key = [
        (_PROJECT_COLUMN, "projects", "id", "RESTRICT"),
        ("tenant_id", "projects", "tenant_id", "RESTRICT"),
    ]
    if [(str(c), str(t), str(r), str(d).upper()) for c, t, r, d in foreign_key] != expected_key:
        problems.append(
            f"foreign key {_FK_PROJECT} ({_PROJECT_COLUMN}, tenant_id) → projects (id, tenant_id)"
            " ON DELETE RESTRICT"
        )

    indexes: dict[str, list[tuple[int, str]]] = {}
    for name, non_unique, column in bind.execute(sa.text(_INDEX_QUERY)).all():
        indexes.setdefault(str(name), []).append((int(non_unique), str(column)))
    if [column for _, column in indexes.get(_INDEX_PROJECT, [])] != [_PROJECT_COLUMN, "tenant_id"]:
        problems.append(f"index {_INDEX_PROJECT} ({_PROJECT_COLUMN}, tenant_id)")
    if indexes.get(_OPEN_SLOT_INDEX) != [(0, "scope_key"), (0, "open_slot")]:
        problems.append(f"unique index {_OPEN_SLOT_INDEX} (scope_key, open_slot)")

    clauses = {
        str(name): str(clause) for name, clause in bind.execute(sa.text(_CHECK_CLAUSES_QUERY)).all()
    }
    if "PROJECT_PROVIDER_MODEL" not in clauses.get(_PRIORITY_SCOPE_CHECK, ""):
        problems.append(f"check {_PRIORITY_SCOPE_CHECK} (eight scopes)")
    if _PROJECT_COLUMN not in clauses.get(_SCOPE_COLUMNS_CHECK, ""):
        problems.append(f"check {_SCOPE_COLUMNS_CHECK} (with {_PROJECT_COLUMN})")
    if _PROJECT_COLUMN not in columns.get("scope_key", ""):
        problems.append(f"generated column scope_key (with {_PROJECT_COLUMN})")
    return problems


def _guard(triggers: dict[str, str]) -> None:
    """写入隔离：哪个隔离触发器不存在就建哪个，排在同一时机的主触发器之前。"""
    for name, (_, main) in _GUARDS.items():
        if name not in triggers:
            op.execute(_guard_statement(name, main if main in triggers else None))


def _unguard(bind: sa.engine.Connection) -> None:
    """解除隔离：先查，只对存在的隔离触发器发 DROP TRIGGER。"""
    present = _triggers(bind)
    for name in _GUARDS:
        if name in present:
            op.execute(_drop_trigger(name))


def _upgrade_offline() -> None:
    # 离线读不了库：按「什么都没做过」输出（照 0014 与 0027 的惯例）。
    for name, (_, main) in _GUARDS.items():
        op.execute(_guard_statement(name, main))
    op.execute(_ALTER_UPGRADE)
    op.execute(f"DROP TRIGGER IF EXISTS {_UPDATE_TRIGGER}")
    op.execute(_TRIGGERS[_UPDATE_TRIGGER])
    for name in _GUARDS:
        op.execute(_drop_trigger(name))


def upgrade() -> None:
    if op.get_context().as_sql:
        _upgrade_offline()
        return

    bind = op.get_bind()
    columns = _columns(bind)
    triggers = _triggers(bind)
    has_column = _PROJECT_COLUMN in columns
    problems = _shape_problems(bind, columns) if has_column else []
    table_done = has_column and not problems
    trigger = triggers.get(_UPDATE_TRIGGER)
    trigger_done = trigger is not None and _UPGRADED_TRIGGER_MARK in trigger
    guarded = [name for name in _GUARDS if name in triggers]
    # 已完成：不发任何 DDL（连隔离触发器也不建）。
    if table_done and trigger_done and not guarded:
        return

    # 第 1 步。⚠️ 必须排在任何 DDL 之前。
    _require_trigger_privilege(bind)
    # 形状核对紧接预检、在建隔离触发器之前：不符就不动库，已有的隔离触发器原样留下。
    if problems:
        raise RuntimeError(
            f"Cannot upgrade to 0030: column {_RULES}.{_PROJECT_COLUMN} exists but the table"
            " differs from the design in " + "; ".join(problems) + ". No DDL was issued; guard"
            " triggers already there (if any) were left in place. The table was changed by hand:"
            " have a person compare it with docs/design/AIH-TASK-136-pricing-project-scope.md"
            " §2 before upgrading again"
        )

    # 第 2 步。第 3、4 步都已完成时跳过（上一次停在解除隔离的中途）。
    if not (table_done and trigger_done):
        _guard(triggers)
    # 第 3 步：一条原子 ALTER。
    if not has_column:
        op.execute(_ALTER_UPGRADE)
    # 第 4 步。
    if not trigger_done:
        if trigger is not None:
            op.execute(_drop_trigger(_UPDATE_TRIGGER))
        op.execute(_TRIGGERS[_UPDATE_TRIGGER])
    # 第 5 步：只有第 3、4 步都完成才走到这里。
    _unguard(bind)


def downgrade() -> None:
    # ⚠️ 最先：离线既判断不了是否已完成，也判断不了有没有项目级规则（设计 §2「降级」）。
    if op.get_context().as_sql:
        raise RuntimeError(
            f"Cannot downgrade 0030 in --sql offline mode: whether {_RULES} holds project-level"
            " pricing rules, and how far an earlier downgrade got, can only be checked against"
            " the database. Run the downgrade online"
        )

    bind = op.get_bind()
    has_column = _PROJECT_COLUMN in _columns(bind)
    triggers = _triggers(bind)
    trigger = triggers.get(_UPDATE_TRIGGER)
    trigger_done = trigger is not None and _DOWNGRADED_TRIGGER_MARK not in trigger
    guarded = [name for name in _GUARDS if name in triggers]
    if not has_column and trigger_done and not guarded:
        return

    # 第 1 步：降级同样要重建触发器。
    _require_trigger_privilege(bind)
    # 第 2 步：放在拒绝检查之前，检查与之后的 DDL 之间就不会有人插入项目级规则。
    if has_column or not trigger_done:
        _guard(triggers)
    if has_column:
        # 第 3 步。列已不存在时跳过：没有这一列就不可能有项目级规则（CHECK 不允许）。
        if bind.execute(sa.text(_PROJECT_RULES_QUERY)).first() is not None:
            _unguard(bind)
            raise RuntimeError(
                f"Cannot downgrade 0030: {_RULES} holds project-level pricing rules (project_id"
                " set, or a PROJECT_* priority_scope). The downgrade would lose them, and usage"
                " events may already refer to them through pricing_rule_id (INV-6). Nothing was"
                " changed; fix forward instead of downgrading"
            )
        # 第 4 步：⚠️ 先改表、后换触发器（设计 §2「为什么降级先改表、后换触发器」）。
        op.execute(_ALTER_DOWNGRADE)
    # 第 5 步。
    if not trigger_done:
        if trigger is not None:
            op.execute(_drop_trigger(_UPDATE_TRIGGER))
        op.execute(_TRIGGERS_0014[_UPDATE_TRIGGER])
    # 第 6 步：只有第 4、5 步都完成才走到这里。
    _unguard(bind)

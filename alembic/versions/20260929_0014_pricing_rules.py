"""pricing rule lock, pricing rules and their FIXED_RATE components, with triggers

照设计闸门 #178 v3（docs/design/AIH-TASK-027-pricing-rules.md）§2「数据库」与 §8。

步骤
----

0. **预检**：查 `@@log_bin` 与 `@@log_bin_trust_function_creators`（与 0006 同一个）。
   binlog 开着而开关关着时，应用账号建不了触发器（ERROR 1419），所以**在任何 DDL 之前**
   抛出明确的错误，`alembic_version` 仍是 0013。
1. `create_table pricing_rule_locks`，写入唯一一行 `id = 1`（发布 / 停用的排他锁与计费的
   共享锁都锁这一行，设计 §2「为什么用一张单行锁表」）
2. `create_table pricing_rules`：`public_id` 唯一；外键 `tenant_id` → `tenants.id`、
   `provider_id` → `ai_providers.id`、复合外键 `(model_id, provider_id)` →
   `ai_models(id, provider_id)`、`created_by` / `approved_by` → `users.id`，都是 RESTRICT；
   STORED 生成列 `scope_key` 与 `open_slot`，唯一索引 `(scope_key, open_slot)`；
   `(id, strategy)` 唯一（给分量表的复合外键用）；解析用的索引；八条 CHECK
3. `create_table pricing_rule_components`：复合外键 `(pricing_rule_id, strategy)` →
   `pricing_rules(id, strategy)`（`strategy` 由 CHECK 钉成 `FIXED_RATE`：MARKUP 规则挂不上
   分量）、外键 → `usage_meter_components`，都是 RESTRICT；
   `(pricing_rule_id, usage_meter_component_id)` 唯一；四条 CHECK
4. 8 个触发器：锁表的 BEFORE INSERT（已有一行时）/ BEFORE DELETE；规则的 BEFORE INSERT
   （只能以草稿插入）/ BEFORE UPDATE（丢弃的不可改、草稿只能改成草稿 / 已发布 / 已丢弃、
   发布后只许截断与停用、发布跃迁上 FIXED_RATE 的分量完整性、锁 `pricing_rule_locks` 后检查
   同一范围的区间不重叠）/ BEFORE DELETE（一律拒绝）；分量的 BEFORE INSERT / UPDATE /
   DELETE（所属规则不是草稿就拒绝）

§132 第 13 条分析
-----------------

**已有数据**：不 ALTER、不读写任何既有表；三张都是新表。外键指向 `tenants`、`ai_providers`、
`ai_models`、`users`、`usage_meter_components`，建外键只在新表上建索引。

**锁表与停机**：只建新表、索引与触发器，外加锁表的一行。建外键时 MySQL 对被引用表取短暂的
元数据锁；目录表很小（生产上几十行），`tenants` 与 `users` 也只是元数据锁、不扫行。不需要停机。

**备份**：要求前置备份（与 0005–0013 同一口径）。本版不改动、不回填任何既有数据，也不预置任何
规则（设计 §8：上线后由管理员录入）。

**部署顺序**：`deploy.sh` 先迁移、再换镜像。旧代码不读新表，不受影响。新代码的管理端定价规则
接口依赖新表；`resolve_pricing_rule` 与取共享锁的函数本任务不接到任何计费路径。

**失败处理**：⚠️ MySQL 的 DDL **不参与事务**。
- 预检失败：什么都没建，`alembic_version` 仍是 `0013_provider_prices`。给账号打开
  `log_bin_trust_function_creators` 后重新部署。
- 第 1 张表就失败：什么都没建，修好后重新部署即可。
- 之后任何一步失败，或进程在中途被杀：已建的表（可能带锁表的那一行、一部分触发器）存在而
  `alembic_version` 没有前进，重跑会撞 "Table already exists"。确认 `alembic_version` 仍是
  `0013_provider_prices`、两张规则表为空后，按依赖倒序 `DROP TABLE pricing_rule_components`、
  `DROP TABLE pricing_rules`、`DROP TABLE pricing_rule_locks`（不存在的跳过；触发器随表
  删除），再重新部署。
- 本版只写锁表的那一行，所以「清场重跑」始终安全。

**回滚**：`downgrade` 按外键依赖倒序删表（分量 → 规则 → 锁表），触发器随表删除，不碰任何
既有表。⚠️ 生产上录了规则之后回滚会连同规则一起删掉 —— 那时应前滚修复而不是回滚（设计 §8）。

**残余风险**：`TRUNCATE` / `DROP` 是 DDL，不经触发器（与 0006 相同）。

Revision ID: 0014_pricing_rules
Revises: 0013_provider_prices
Create Date: 2026-09-29
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0014_pricing_rules"
down_revision: str | None = "0013_provider_prices"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_LOCKS = "pricing_rule_locks"
_RULES = "pricing_rules"
_COMPONENTS = "pricing_rule_components"

# 依赖倒序：downgrade 按这个顺序删。
_DROP_ORDER = (_COMPONENTS, _RULES, _LOCKS)

# 锁表唯一一行的 id。
_LOCK_ID = 1
_INSERT_LOCK_ROW = f"INSERT INTO {_LOCKS} (id) VALUES ({_LOCK_ID})"

_PREREQUISITE_QUERY = "SELECT @@log_bin, @@log_bin_trust_function_creators"
_PREREQUISITE_ERROR = (
    "MySQL has binary logging on and log_bin_trust_function_creators OFF, "
    + "so this account cannot create the pricing rule triggers (ERROR 1419). "
    + "Set log_bin_trust_function_creators = ON first; nothing has been changed."
)

# 名字 → (表, 条件)。⚠️ 冻结的字面量：与 app/models/pricing_rules.py 上的
# CheckConstraint 逐条一致（空白除外），tests/backend/test_migrations.py 比对两边。
_CHECKS: dict[str, tuple[str, str]] = {
    "ck_pricing_rules_priority_scope": (
        _RULES,
        "priority_scope IN ('CUSTOMER_PROVIDER_MODEL', 'CUSTOMER_PROVIDER', 'CUSTOMER',"
        " 'GLOBAL_PROVIDER_MODEL', 'GLOBAL')",
    ),
    "ck_pricing_rules_scope_columns": (
        _RULES,
        "(priority_scope = 'CUSTOMER_PROVIDER_MODEL' AND tenant_id IS NOT NULL"
        " AND provider_id IS NOT NULL AND model_id IS NOT NULL)"
        " OR (priority_scope = 'CUSTOMER_PROVIDER' AND tenant_id IS NOT NULL"
        " AND provider_id IS NOT NULL AND model_id IS NULL)"
        " OR (priority_scope = 'CUSTOMER' AND tenant_id IS NOT NULL"
        " AND provider_id IS NULL AND model_id IS NULL)"
        " OR (priority_scope = 'GLOBAL_PROVIDER_MODEL' AND tenant_id IS NULL"
        " AND provider_id IS NOT NULL AND model_id IS NOT NULL)"
        " OR (priority_scope = 'GLOBAL' AND tenant_id IS NULL"
        " AND provider_id IS NULL AND model_id IS NULL)",
    ),
    "ck_pricing_rules_strategy": (_RULES, "strategy IN ('MARKUP', 'FIXED_RATE')"),
    "ck_pricing_rules_markup": (
        _RULES,
        "(strategy = 'MARKUP' AND markup_multiplier IS NOT NULL AND markup_multiplier > 0)"
        " OR (strategy = 'FIXED_RATE' AND markup_multiplier IS NULL)",
    ),
    "ck_pricing_rules_status": (
        _RULES,
        "status IN ('DRAFT', 'PUBLISHED', 'RETIRED', 'DISCARDED')",
    ),
    "ck_pricing_rules_unpublished": (
        _RULES,
        "status NOT IN ('DRAFT', 'DISCARDED') OR (effective_from IS NULL AND effective_to IS NULL"
        " AND approved_by IS NULL AND approved_at IS NULL)",
    ),
    "ck_pricing_rules_approved": (
        _RULES,
        "status NOT IN ('PUBLISHED', 'RETIRED')"
        " OR (approved_by IS NOT NULL AND approved_at IS NOT NULL)",
    ),
    "ck_pricing_rules_period": (
        _RULES,
        "effective_from IS NULL OR effective_to IS NULL OR effective_from < effective_to"
        " OR (status = 'RETIRED' AND effective_from = effective_to)",
    ),
    "ck_pricing_rule_components_strategy": (_COMPONENTS, "strategy = 'FIXED_RATE'"),
    "ck_pricing_rule_components_unit_quantity": (_COMPONENTS, "unit_quantity > 0"),
    "ck_pricing_rule_components_rate_amount": (_COMPONENTS, "rate_amount > 0"),
    "ck_pricing_rule_components_currency": (_COMPONENTS, "currency = 'MYR'"),
}

# 生成列表达式。同上，冻结的字面量，与模型的 Computed 比对。
# `scope_key` 把「同一范围」变成一个非空值：MySQL 的唯一约束不管 NULL，直接用
# `(priority_scope, tenant_id, provider_id, model_id, open_slot)` 挡不住全局范围的两条。
_SCOPE_KEY = (
    "CONCAT(priority_scope, ':', COALESCE(tenant_id, 0), ':', COALESCE(provider_id, 0),"
    " ':', COALESCE(model_id, 0))"
)
_OPEN_SLOT = "CASE WHEN status = 'PUBLISHED' AND effective_to IS NULL THEN 1 END"

# 触发器名 → 建触发器的语句。⚠️ 整条 CREATE TRIGGER 是**一条**语句：DELIMITER 只是
# mysql 命令行客户端的东西，驱动把 BEGIN … END 原样发给服务器（与 0006 同一写法）。
_TRIGGERS: dict[str, str] = {
    # 锁表恰好一行：迁移写入之后，再插入一律拒绝。
    "trg_pricing_rule_locks_before_insert": """
CREATE TRIGGER trg_pricing_rule_locks_before_insert
BEFORE INSERT ON pricing_rule_locks
FOR EACH ROW
BEGIN
    IF (SELECT COUNT(*) FROM pricing_rule_locks) > 0 THEN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'pricing_rule_locks holds exactly one row';
    END IF;
END
""",
    "trg_pricing_rule_locks_before_delete": """
CREATE TRIGGER trg_pricing_rule_locks_before_delete
BEFORE DELETE ON pricing_rule_locks
FOR EACH ROW
SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'pricing_rule_locks holds exactly one row'
""",
    # 规则只能以草稿进入：「草稿 → 已发布」跃迁上的检查因此绕不过去（设计 §2）。
    "trg_pricing_rules_before_insert": """
CREATE TRIGGER trg_pricing_rules_before_insert
BEFORE INSERT ON pricing_rules
FOR EACH ROW
BEGIN
    IF NOT (NEW.status <=> 'DRAFT') THEN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'a pricing rule starts as a draft';
    END IF;
END
""",
    # 五道检查，按顺序：
    # ① 丢弃的草稿不可改；
    # ①′ 草稿只能改成草稿、已发布或已丢弃（设计 §4 的状态表）：不许 DRAFT → RETIRED 绕过 ③；
    # ② 发布过的行只许改 effective_to、PUBLISHED → RETIRED 与 updated_at（INV-6）；
    # ③ 「草稿 → 已发布」跃迁上，FIXED_RATE 规则：至少一个分量，出现的计量类型的全部分量都在；
    # ④ 发布过的行区间或状态有变化时：先锁 pricing_rule_locks（与服务层发布同一把锁），再查
    #    同一范围里有没有另一个非空区间与本行相交。空区间（撤销的预约）不参与判定。
    # ⚠️ MySQL 的触发器不能用 NEW / OLD 引用生成列，所以「同一范围」按 scope_key 的定义现算
    # 出本行的值，与另一行存下的 `o.scope_key` 比较（即设计的 `o.scope_key = NEW.scope_key`）；
    # 不可变检查也不比两个生成列（它们只由被比较的列算出）。
    "trg_pricing_rules_before_update": """
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
    END IF;
END
""",
    # 规则永不删除：会被用量事件的快照永久引用（INV-6），丢弃的草稿也留痕。
    "trg_pricing_rules_before_delete": """
CREATE TRIGGER trg_pricing_rules_before_delete
BEFORE DELETE ON pricing_rules
FOR EACH ROW
SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'pricing rules are never deleted'
""",
    # 分量只在所属规则是草稿时可增删改（草稿编辑是「整体替换」：删掉旧的、插入新的）。
    "trg_pricing_rule_components_before_insert": """
CREATE TRIGGER trg_pricing_rule_components_before_insert
BEFORE INSERT ON pricing_rule_components
FOR EACH ROW
BEGIN
    DECLARE rule_status VARCHAR(16) DEFAULT NULL;
    SELECT status INTO rule_status
      FROM pricing_rules
     WHERE id = NEW.pricing_rule_id;
    IF NOT (rule_status <=> 'DRAFT') THEN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'pricing rule components change only on a draft';
    END IF;
END
""",
    "trg_pricing_rule_components_before_update": """
CREATE TRIGGER trg_pricing_rule_components_before_update
BEFORE UPDATE ON pricing_rule_components
FOR EACH ROW
BEGIN
    DECLARE old_status VARCHAR(16) DEFAULT NULL;
    DECLARE new_status VARCHAR(16) DEFAULT NULL;
    SELECT status INTO old_status
      FROM pricing_rules
     WHERE id = OLD.pricing_rule_id;
    SELECT status INTO new_status
      FROM pricing_rules
     WHERE id = NEW.pricing_rule_id;
    IF NOT (old_status <=> 'DRAFT') OR NOT (new_status <=> 'DRAFT') THEN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'pricing rule components change only on a draft';
    END IF;
END
""",
    "trg_pricing_rule_components_before_delete": """
CREATE TRIGGER trg_pricing_rule_components_before_delete
BEFORE DELETE ON pricing_rule_components
FOR EACH ROW
BEGIN
    DECLARE rule_status VARCHAR(16) DEFAULT NULL;
    SELECT status INTO rule_status
      FROM pricing_rules
     WHERE id = OLD.pricing_rule_id;
    IF NOT (rule_status <=> 'DRAFT') THEN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'pricing rule components change only on a draft';
    END IF;
END
""",
}


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


def _decimal_20_8() -> sa.Numeric:
    return sa.Numeric(precision=20, scale=8)


def upgrade() -> None:
    # 第 0 步。⚠️ 必须排在任何 DDL 之前：MySQL 的 DDL 不参与事务，建了一半的表要人工
    # 清场。`--sql` 离线模式不连库，也就没有可查的变量。
    if not op.get_context().as_sql:
        _require_trigger_privilege(op.get_bind())

    op.create_table(
        _LOCKS,
        sa.Column("id", sa.Integer(), autoincrement=False, nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    # 唯一一行。排在触发器之前（此刻表是空的，BEFORE INSERT 本来也放行）。
    op.execute(_INSERT_LOCK_ROW)

    op.create_table(
        _RULES,
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("public_id", sa.CHAR(length=36), nullable=False),
        sa.Column("priority_scope", sa.String(length=32), nullable=False),
        sa.Column("tenant_id", sa.BigInteger(), nullable=True),
        sa.Column("provider_id", sa.BigInteger(), nullable=True),
        sa.Column("model_id", sa.BigInteger(), nullable=True),
        sa.Column("strategy", sa.String(length=16), nullable=False),
        sa.Column("markup_multiplier", _decimal_20_8(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("effective_from", sa.DateTime(), nullable=True),
        sa.Column("effective_to", sa.DateTime(), nullable=True),
        sa.Column("scope_key", sa.String(length=128), sa.Computed(_SCOPE_KEY, persisted=True)),
        sa.Column("open_slot", sa.Integer(), sa.Computed(_OPEN_SLOT, persisted=True)),
        sa.Column("created_by", sa.BigInteger(), nullable=False),
        sa.Column("approved_by", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("approved_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        # 客户级规则以租户外键绑定（INV-8）。RESTRICT：有规则的租户删不掉。
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_pricing_rules_tenant",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["provider_id"],
            ["ai_providers.id"],
            name="fk_pricing_rules_provider",
            ondelete="RESTRICT",
        ),
        # 规则只能挂在同一供应商的模型上。`model_id` 为空时 MySQL 不检查这条外键。
        sa.ForeignKeyConstraint(
            ["model_id", "provider_id"],
            ["ai_models.id", "ai_models.provider_id"],
            name="fk_pricing_rules_model",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name="fk_pricing_rules_created_by",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["approved_by"],
            ["users.id"],
            name="fk_pricing_rules_approved_by",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("public_id", name="uq_pricing_rules_public_id"),
        # `id` 本来就唯一；这个约束只为让分量表的复合外键成立。
        sa.UniqueConstraint("id", "strategy", name="uq_pricing_rules_id_strategy"),
        # 唯一索引允许多个 NULL：只有未截断的已发布规则在 open_slot 上有值。
        sa.Index("ux_pricing_rules_open_slot", "scope_key", "open_slot", unique=True),
        # 解析时逐级查。
        sa.Index(
            "ix_pricing_rules_resolve",
            "priority_scope",
            "tenant_id",
            "provider_id",
            "model_id",
            "effective_from",
        ),
        sa.Index("ix_pricing_rules_model", "model_id", "provider_id"),
        *_checks(_RULES),
    )

    op.create_table(
        _COMPONENTS,
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("pricing_rule_id", sa.BigInteger(), nullable=False),
        sa.Column("strategy", sa.String(length=16), nullable=False),
        sa.Column("usage_meter_component_id", sa.BigInteger(), nullable=False),
        sa.Column("unit_quantity", _decimal_20_8(), nullable=False),
        sa.Column("rate_amount", _decimal_20_8(), nullable=False),
        sa.Column("currency", sa.CHAR(length=3), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        # MARKUP 规则在数据库层挂不上分量：子表的 strategy 由 CHECK 钉成 FIXED_RATE。
        # RESTRICT（删与改）：有分量时父行的 strategy 改不成 MARKUP（设计 §10 假设 2）。
        sa.ForeignKeyConstraint(
            ["pricing_rule_id", "strategy"],
            [f"{_RULES}.id", f"{_RULES}.strategy"],
            name="fk_pricing_rule_components_rule",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        # RESTRICT：被规则引用的计价分量删不掉（INV-6）。
        sa.ForeignKeyConstraint(
            ["usage_meter_component_id"],
            ["usage_meter_components.id"],
            name="fk_pricing_rule_components_meter_component",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "pricing_rule_id",
            "usage_meter_component_id",
            name="uq_pricing_rule_components_rule_component",
        ),
        sa.Index("ix_pricing_rule_components_rule", "pricing_rule_id", "strategy"),
        sa.Index("ix_pricing_rule_components_meter_component", "usage_meter_component_id"),
        *_checks(_COMPONENTS),
    )

    for statement in _TRIGGERS.values():
        op.execute(statement)


def downgrade() -> None:
    # 按外键依赖倒序：分量 → 规则 → 锁表。触发器随表删除。不碰任何既有表。
    for table in _DROP_ORDER:
        op.drop_table(table)

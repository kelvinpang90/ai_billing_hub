"""pricing rules: retired is final, published starts open, ends change three ways

照设计闸门 #539 v1（docs/design/AIH-TASK-146-period-version-backstop.md）
§2「设计概要」与 §8 的定价规则一行；本迁移是 AIH-TASK-146。
只改 `pricing_rules` 的 BEFORE UPDATE 触发器，不加 CHECK、不改任何行：

- 新触发器 `trg_pricing_rules_before_update_v2`（BEFORE UPDATE），
  正文由 0030 的 `trg_pricing_rules_before_update` 按设计 §2 的 P1–P5 逐字替换得到：
  P1 `RETIRED` 与 `DISCARDED` 都是终态；
  P2 草稿发布时 `effective_to` 必须为空；
  P3 不可变检查只对 `PUBLISHED`；
  P4 区间相交按空值逐项判断，不再用哨兵日期；
  P5 已发布规则的 `effective_to` 只按截断、恢复、停用三种方式变；
- 先建新的、再删 0030 的 `trg_pricing_rules_before_update`。
  MySQL 8 允许同一时机多个触发器，任一 SIGNAL 都拒绝整条语句；
  新的每一处都不弱于旧的，所以任何时刻都有保护。

合起来：发布的区间都是 `[S, NULL)` 或 `[NULL, NULL)`，只能排在该范围
所有非空区间之后；`[NULL, NULL)` 只在时间线为空时通过，再由 0030 的
「起点为空只许全局默认的第一条」限定（设计 §2「新规则合起来的效果」）。

步骤（升级，MySQL）
------------------

在线时**先判断是否已完成**：新触发器存在且与目标逐字相同、旧触发器不存在
（比较见 `_trigger_matches`）→ 直接返回，不发任何语句。否则依次：

1. **预检**：查 `@@log_bin` 与 `@@log_bin_trust_function_creators`
   （查询、判定与报错文案照 0030）。binlog 开着而开关关着时，
   应用账号建不了触发器（ERROR 1419），所以**在任何 DDL 之前**抛错。
2. **形状核对**：已存在的新触发器必须在本表上、BEFORE UPDATE，
   `ACTION_STATEMENT` 去掉首尾空白后逐字等于冻结正文。
   不符就抛错、不发任何 DDL（只可能是手工建过同名对象，
   需要人对照设计查看）。
3. **建新触发器**：新触发器不存在时 `CREATE TRIGGER`
   （不带 `FOLLOWS` / `PRECEDES`）。
4. **删旧触发器**：旧触发器存在时 `DROP TRIGGER`。
   第 4 步一定在第 3 步之后：先有新的，才删旧的。

`--sql` 离线模式读不了库：照 0013 与 0030 的惯例跳过预检与全部判断，
按「什么都没做过」依次输出第 3 步的 CREATE TRIGGER、
`DROP TRIGGER IF EXISTS` 旧触发器。

§132 第 13 条分析
-----------------

**已有数据**：**不改任何已有行**，不回填、不删行；只建一个触发器、
删一个触发器，不加 CHECK。规则的 `id`、`public_id` 不变，用量事件的
`pricing_rule_id` 与分量表的外键不动（INV-6）。迁移不追溯检查已有行
当初是否按新规则写入（设计 §10 假设 2）。

**锁表与停机**：`CREATE TRIGGER` / `DROP TRIGGER` 各取一次短暂的
排他元数据锁：计费事务持有的规则行共享锁让它们等事务提交，进行期间
新的读写在元数据锁上排队，不丢事件。不 `ALTER TABLE`、不重建表
（生产 1 行，毫秒级）。没有任何时刻这张表缺 BEFORE UPDATE 保护，
也没有整表不可写的时刻；管理员的发布、停用照常可用。不需要停机。

**备份**：要求前置备份（与 0005–0031 同一口径）。本版不写任何业务数据。

**部署顺序**：`deploy.sh` 先迁移、再换镜像（设计 §8）。本迁移接在
AIH-TASK-145 的 0031 之后。新触发器只拒绝服务层从不写出的状态，
所以迁移之后、新容器起来之前的旧代码照常运行；本任务不改应用代码。
没有新 CHECK，不需要上线前的数据核对。

**失败处理**：⚠️ MySQL 的 DDL **不参与事务**，每一步与写
`alembic_version` 之间都有失败窗口；每一步都可以安全重跑（设计 §5），
每个中间状态都不弱于 0031：

- 预检失败：什么都没改，`alembic_version` 仍是
  `0031_provider_price_backstop`。给账号打开
  `log_bin_trust_function_creators` 后重新部署。
- 形状核对不符：什么都没改，版本仍是 0031；人对照设计查看
  手工建过的同名对象，删掉或改正后重新部署。
- 建新触发器失败：只有旧触发器，与迁移之前相同；重新部署。
- 建了新触发器、删旧的之前中断：两个触发器并存，效果等于新的；
  重新部署时形状核对通过，只删旧的。
- 全部完成、写 `alembic_version` 之前退出：重新部署时判为已完成，
  不发任何语句，只前进版本。

**回滚**：代码回滚到本迁移之前要先降级。`downgrade` 同样分步、
可重跑，只放宽约束、不改任何行，所以没有数据前提，也不拒绝离线降级。
在线时先判断是否已完成（旧触发器存在且与冻结的 0030 正文逐字相同、
新触发器不存在 → 直接返回）；否则依次：预检；旧触发器存在但与冻结的
0030 正文不是逐字相同、或新触发器存在但与本迁移的正文不是逐字相同
→ 抛错、不发任何 DDL；旧触发器不存在时按冻结的 0030 正文建它；
新触发器存在时删它（先建旧的、再删新的，理由同升级）。
`--sql` 离线降级依次输出建旧触发器、`DROP TRIGGER IF EXISTS` 新触发器。

Revision ID: 0032_pricing_rule_backstop
Revises: 0031_provider_price_backstop
Create Date: 2026-11-01
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0032_pricing_rule_backstop"
down_revision: str | None = "0031_provider_price_backstop"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_RULES = "pricing_rules"
_OLD_TRIGGER = "trg_pricing_rules_before_update"
_NEW_TRIGGER = "trg_pricing_rules_before_update_v2"

_PREREQUISITE_QUERY = "SELECT @@log_bin, @@log_bin_trust_function_creators"
_PREREQUISITE_ERROR = (
    "MySQL has binary logging on and log_bin_trust_function_creators OFF, "
    + "so this account cannot create the pricing rule triggers (ERROR 1419). "
    + "Set log_bin_trust_function_creators = ON first; nothing has been changed."
)

# 触发器名 → 建触发器的语句（写法照 0030 的 _TRIGGERS：整条 CREATE TRIGGER
# 是一条语句）。与 0030 的 `trg_pricing_rules_before_update` 只差触发器名与
# 设计 §2 的 P1–P5，其余逐字相同：
# P1 `RETIRED` 与 `DISCARDED` 都是终态；
# P2 草稿发布时 `effective_to` 必须为空（与 0015 同一写法）；
# P3 不可变检查只对 `PUBLISHED`（`RETIRED` 已由 P1 整体拒绝）；
# P4 相交判定按空值逐项判断，对 `DATETIME` 的任何取值都成立；
# P5 已发布规则的 `effective_to` 只许「空 → 有值」（截断，或停用时写尽头）与
#    「有值 → 空、状态不变」（恢复为未截断），停用只许未截断的。
_TRIGGERS: dict[str, str] = {
    _NEW_TRIGGER: """
CREATE TRIGGER trg_pricing_rules_before_update_v2
BEFORE UPDATE ON pricing_rules
FOR EACH ROW
BEGIN
    DECLARE locked_id INT DEFAULT NULL;
    IF OLD.status IN ('RETIRED', 'DISCARDED') THEN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'a retired or discarded pricing rule is final';
    END IF;
    IF OLD.status = 'DRAFT' AND NEW.status NOT IN ('DRAFT', 'PUBLISHED', 'DISCARDED') THEN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'a draft pricing rule is published or discarded';
    END IF;
    IF OLD.status = 'DRAFT' AND NEW.status = 'PUBLISHED' AND NEW.effective_to IS NOT NULL THEN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'a pricing rule is published without an end';
    END IF;
    IF OLD.status = 'PUBLISHED' THEN
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
        IF (NOT (NEW.effective_to <=> OLD.effective_to)
            AND OLD.effective_to IS NOT NULL
            AND NOT (NEW.effective_to IS NULL AND NEW.status = OLD.status))
           OR (NEW.status = 'RETIRED' AND OLD.effective_to IS NOT NULL) THEN
            SIGNAL SQLSTATE '45000'
                SET MESSAGE_TEXT =
                    'a published pricing rule ends only by truncation, restore or retirement';
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
               AND (o.effective_from IS NULL OR NEW.effective_to IS NULL
                    OR o.effective_from < NEW.effective_to)
               AND (NEW.effective_from IS NULL OR o.effective_to IS NULL
                    OR NEW.effective_from < o.effective_to)) THEN
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

# 降级恢复的 0030 原文（与 0030 的 _TRIGGERS 里同名的那条逐字一致，测试比对）。
_TRIGGERS_0030: dict[str, str] = {
    _OLD_TRIGGER: """
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

# 触发器名在库内唯一。
_TRIGGERS_QUERY = (
    "SELECT TRIGGER_NAME, EVENT_OBJECT_TABLE, ACTION_TIMING, EVENT_MANIPULATION, ACTION_STATEMENT"
    " FROM information_schema.TRIGGERS WHERE TRIGGER_SCHEMA = DATABASE()"
    f" AND TRIGGER_NAME IN ('{_OLD_TRIGGER}', '{_NEW_TRIGGER}')"
)

_DESIGN = "docs/design/AIH-TASK-146-period-version-backstop.md"


def _body(statement: str) -> str:
    """What MySQL keeps as ACTION_STATEMENT (the part after `FOR EACH ROW`), without the
    blanks at either end."""
    return statement.split("FOR EACH ROW", 1)[1].strip()


def _require_trigger_privilege(bind: sa.engine.Connection) -> None:
    """Refuse to run where the migration account could not create triggers (as 0030)."""
    log_bin, trusted = bind.execute(sa.text(_PREREQUISITE_QUERY)).one()
    if int(log_bin) == 1 and int(trusted) != 1:
        raise RuntimeError(_PREREQUISITE_ERROR)


def _triggers(bind: sa.engine.Connection) -> dict[str, tuple[str, str, str, str]]:
    """The old and the new trigger, those that exist: name → (table, timing, event, body)."""
    return {
        str(name): (str(table), str(timing), str(kind), str(statement))
        for name, table, timing, kind, statement in bind.execute(sa.text(_TRIGGERS_QUERY)).all()
    }


def _trigger_matches(row: tuple[str, str, str, str], statement: str) -> bool:
    """On our table, BEFORE UPDATE, and the body word for word the frozen one (only the blanks
    at either end are dropped). Design §2 upgrade step 2: shared by "already done", the shape
    check and downgrade step 2."""
    table, timing, kind, body = row
    return (
        table == _RULES
        and timing == "BEFORE"
        and kind == "UPDATE"
        and body.strip() == _body(statement)
    )


def _shape_error(direction: str, problems: list[str]) -> RuntimeError:
    return RuntimeError(
        f"Cannot {direction} 0032: " + "; ".join(problems) + ". No DDL was issued. An object"
        " of that name was made by hand: have a person compare it with"
        f" {_DESIGN} §2 before running the migration again"
    )


def _new_trigger_problem() -> str:
    return (
        f"trigger {_NEW_TRIGGER} exists but is not, word for word, the BEFORE UPDATE trigger"
        f" on {_RULES} this migration builds"
    )


def _upgrade_offline() -> None:
    # 离线读不了库：按「什么都没做过」输出（照 0013 与 0030 的惯例）。
    op.execute(_TRIGGERS[_NEW_TRIGGER])
    op.execute(f"DROP TRIGGER IF EXISTS {_OLD_TRIGGER}")


def upgrade() -> None:
    if op.get_context().as_sql:
        _upgrade_offline()
        return

    bind = op.get_bind()
    triggers = _triggers(bind)
    new = triggers.get(_NEW_TRIGGER)
    new_done = new is not None and _trigger_matches(new, _TRIGGERS[_NEW_TRIGGER])
    # 已完成：不发任何语句。
    if new_done and _OLD_TRIGGER not in triggers:
        return

    # 第 1 步。⚠️ 必须排在任何 DDL 之前。
    _require_trigger_privilege(bind)
    # 第 2 步。
    if new is not None and not new_done:
        raise _shape_error("upgrade to", [_new_trigger_problem()])
    # 第 3 步。
    if new is None:
        op.execute(_TRIGGERS[_NEW_TRIGGER])
    # 第 4 步：一定在第 3 步之后。
    if _OLD_TRIGGER in triggers:
        op.execute(f"DROP TRIGGER {_OLD_TRIGGER}")


def _downgrade_offline() -> None:
    # 离线读不了库：按「升级已完成」输出。降级只放宽，离线输出不会造成数据错误。
    op.execute(_TRIGGERS_0030[_OLD_TRIGGER])
    op.execute(f"DROP TRIGGER IF EXISTS {_NEW_TRIGGER}")


def downgrade() -> None:
    if op.get_context().as_sql:
        _downgrade_offline()
        return

    bind = op.get_bind()
    triggers = _triggers(bind)
    old = triggers.get(_OLD_TRIGGER)
    new = triggers.get(_NEW_TRIGGER)
    old_done = old is not None and _trigger_matches(old, _TRIGGERS_0030[_OLD_TRIGGER])
    if old_done and new is None:
        return

    # 第 1 步：降级要重建旧触发器。
    _require_trigger_privilege(bind)
    # 第 2 步。
    problems = []
    if old is not None and not old_done:
        problems.append(
            f"trigger {_OLD_TRIGGER} exists but is not, word for word, the one 0030 built"
        )
    if new is not None and not _trigger_matches(new, _TRIGGERS[_NEW_TRIGGER]):
        problems.append(_new_trigger_problem())
    if problems:
        raise _shape_error("downgrade", problems)
    if old is None:
        op.execute(_TRIGGERS_0030[_OLD_TRIGGER])
    # 第 3 步：先建旧的、再删新的。
    if new is not None:
        op.execute(f"DROP TRIGGER {_NEW_TRIGGER}")

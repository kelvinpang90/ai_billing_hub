"""provider price versions: retired needs an end, published starts open, retired is final

照设计闸门 #535 v5（docs/design/AIH-TASK-145-provider-price-backstop.md）
§2「数据库（迁移 0031）」与 §8；本迁移是 AIH-TASK-145。
只改 `provider_price_versions` 的约束与它的 BEFORE UPDATE 触发器：

- 新 CHECK `ck_provider_price_versions_retired_end`：
  `status <> 'RETIRED' OR effective_to IS NOT NULL`
  （与汇率、定价规则、税率三张表的同名 CHECK 同一写法）；
- 新触发器 `trg_provider_price_versions_before_update_v2`（BEFORE UPDATE），
  正文由 0013 的 `trg_provider_price_versions_before_update` 改五处得到：
  ① `RETIRED` 与 `DISCARDED` 都是终态；
  ② 草稿发布时 `effective_to` 必须为空；
  ③ 不可变检查只对 `PUBLISHED`；
  ④ 区间相交按空值逐项判断，不再用哨兵日期；
  ⑤ 已发布版本的 `effective_to` 只按截断、恢复、退役三种方式变；
- 先建新的、再删 0013 的 `trg_provider_price_versions_before_update`。
  MySQL 8 允许同一时机多个触发器，任一 SIGNAL 都拒绝整条语句；
  新的每一处都不弱于旧的，所以任何时刻都有保护。

合起来：发布的区间都是 `[F, NULL)`，起点为空的发布一定是
`[NULL, NULL)`，与任何非空区间相交，所以只在该（供应商, 模型）
没有区间非空的已发布 / 已退役版本时通过。

`app/models/provider_prices.py` 同步了这条 CHECK（`RETIRED_END_CHECK`），
tests/backend/test_migrations.py 与
tests/backend/test_provider_price_backstop_migration.py 比对两边。

步骤（升级，MySQL）
------------------

在线时**先判断是否已完成**：CHECK 存在且与目标逐字相同、
新触发器存在且与目标逐字相同、旧触发器不存在
（比较见 `_check_matches` 与 `_trigger_matches`）→ 直接返回，
不发任何语句。否则依次：

1. **预检**：查 `@@log_bin` 与 `@@log_bin_trust_function_creators`
   （查询、判定与报错文案照 0013）。binlog 开着而开关关着时，
   应用账号建不了触发器（ERROR 1419），所以**在任何 DDL 之前**抛错。
2. **形状核对**：已存在的同名 CHECK 必须恰好一行、在本表上、
   `ENFORCED = 'YES'`，`CHECK_CLAUSE` 的原文全文匹配
   `_RETIRED_END_CLAUSE_PATTERN`（不做任何归一化，只有字符集前缀
   可变且限三种）；已存在的新触发器必须在本表上、BEFORE UPDATE，
   `ACTION_STATEMENT` 去掉首尾空白后逐字等于冻结正文。
   任何一项不符就抛错、不发任何 DDL（只可能是手工建过同名对象，
   需要人对照设计查看）。
3. **数据核对**：CHECK 不存在时，查退役而无尽头的行；有就抛错
   （列出至多 20 个 `id`）、不发任何 DDL。
   迁移不替人补尽头：尽头决定哪些时刻有价。
4. **加 CHECK**：CHECK 不存在时一条
   `ALTER TABLE … ADD CONSTRAINT … CHECK (…) ENFORCED`。
5. **建新触发器**：新触发器不存在时 `CREATE TRIGGER`
   （不带 `FOLLOWS` / `PRECEDES`）。
6. **删旧触发器**：旧触发器存在时 `DROP TRIGGER`。
   第 6 步一定在第 5 步之后：先有新的，才删旧的。

`--sql` 离线模式读不了库：照 0013 与 0030 的惯例跳过预检与全部判断，
按「什么都没做过」依次输出第 4 步的 ALTER、第 5 步的 CREATE TRIGGER、
`DROP TRIGGER IF EXISTS` 旧触发器。离线脚本没有第 3 步：
已有违反行时 ALTER 得到 ERROR 3819、后面不再执行。

§132 第 13 条分析
-----------------

**已有数据**：**不改任何已有行**，不回填、不删行；只加一条 CHECK、
建一个触发器、删一个触发器。MySQL 加 CHECK 时校验全部已有行，
任一违反就整条失败、表不变（第 3 步先列出违反行）。
版本的 `id`、`public_id` 不变，用量事件的
`provider_price_version_id` 不动（INV-6）。

**锁表与停机**：`ADD CONSTRAINT … CHECK` 重建 `provider_price_versions`
（`ALGORITHM=COPY`）并持元数据锁：重建期间允许读、写入排队；
计费事务持有的版本行共享锁让 ALTER 等它们提交，进行期间新的计费
在元数据锁上排队，不丢事件。表很小（生产 1 行），秒级以内。
`CREATE TRIGGER` / `DROP TRIGGER` 各取一次短暂的排他元数据锁。
没有任何时刻这张表缺 BEFORE UPDATE 保护，也没有整表不可写的时刻。
不需要停机。

**备份**：要求前置备份（与 0005–0030 同一口径）。本版不写任何业务数据。

**部署顺序**：`deploy.sh` 先迁移、再换镜像（设计 §8）。
新约束只拒绝服务层从不写出的状态，所以迁移之后、新容器起来之前的
旧代码照常运行；新代码只多了模型上的 CHECK 声明（SQLite 建表用）。
上线前核对（只读）：在生产库上跑第 3 步的同一条查询（设计 §8）。

**失败处理**：⚠️ MySQL 的 DDL **不参与事务**，每一步与写
`alembic_version` 之间都有失败窗口；每一步都可以安全重跑（设计 §5），
每个中间状态都不弱于 0013：

- 预检失败：什么都没改，`alembic_version` 仍是
  `0030_pricing_rule_project_scope`。给账号打开
  `log_bin_trust_function_creators` 后重新部署。
- 形状核对不符：什么都没改，版本仍是 0030；人对照设计查看
  手工建过的同名对象，删掉或改正后重新部署。
- 数据核对发现退役无尽头的行：什么都没改；人逐行判断应有的尽头、
  手工补上（0013 的触发器允许 `UPDATE … SET effective_to = …`）后
  重新部署。
- 加 CHECK 失败（第 3 步之后才写进的违反行，或其他 DDL 错误）：
  表不变（InnoDB DDL 原子），触发器仍是 0013 的；重新部署，
  第 3 步列出违反行。
- 加了 CHECK、建新触发器之前中断：有新 CHECK、只有旧触发器；
  重新部署时 CHECK 存在跳过，建新触发器、删旧的。
- 建了新触发器、删旧的之前中断：两个触发器并存，效果等于新的；
  重新部署时只删旧的。
- 全部完成、写 `alembic_version` 之前退出：重新部署时判为已完成，
  不发任何语句，只前进版本。

**回滚**：代码回滚到本迁移之前要先降级。`downgrade` 同样分步、
可重跑，只放宽约束、不改任何行，所以没有数据前提，也不拒绝离线降级。
在线时先判断是否已完成（CHECK 不存在、旧触发器存在且与冻结的
0013 正文逐字相同、新触发器不存在 → 直接返回）；否则依次：预检；
旧触发器存在但与冻结的 0013 正文不是逐字相同、或新触发器存在但
与本迁移的正文不是逐字相同 → 抛错、不发任何 DDL；旧触发器不存在时
按冻结的 0013 正文建它；新触发器存在时删它；CHECK 存在时删它
（先建旧的、再删新的，理由同升级）。`--sql` 离线降级依次输出
建旧触发器、`DROP TRIGGER IF EXISTS` 新触发器、删 CHECK 的 ALTER。

Revision ID: 0031_provider_price_backstop
Revises: 0030_pricing_rule_project_scope
Create Date: 2026-10-31
"""

from __future__ import annotations

import re
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0031_provider_price_backstop"
down_revision: str | None = "0030_pricing_rule_project_scope"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_VERSIONS = "provider_price_versions"
_OLD_TRIGGER = "trg_provider_price_versions_before_update"
_NEW_TRIGGER = "trg_provider_price_versions_before_update_v2"

_PREREQUISITE_QUERY = "SELECT @@log_bin, @@log_bin_trust_function_creators"
_PREREQUISITE_ERROR = (
    "MySQL has binary logging on and log_bin_trust_function_creators OFF, "
    + "so this account cannot create the provider price triggers (ERROR 1419). "
    + "Set log_bin_trust_function_creators = ON first; nothing has been changed."
)

# ⚠️ 冻结的字面量：与 app/models/provider_prices.py 的 RETIRED_END_CHECK
# 逐字一致（空白除外），tests/backend/test_migrations.py 比对两边。
_RETIRED_END_CHECK_NAME = "ck_provider_price_versions_retired_end"
_RETIRED_END_CHECK = "status <> 'RETIRED' OR effective_to IS NOT NULL"
# MySQL 8.4 保存这一条件的规范写法（`CHECK_CLAUSE` 原文，不做任何归一化）。
# 唯一可变的是字符集前缀（取决于建约束时连接的字符集），只能是这三个之一
# （设计 §2 升级第 2 步）。用 `re.fullmatch`。
_RETIRED_END_CLAUSE_PATTERN = (
    r"^\(\(`status` <> _(utf8mb4|utf8mb3|latin1)\\'RETIRED\\'\)"
    r" or \(`effective_to` is not null\)\)$"
)
# 第 3 步：至多列出这么多个违反的 `id`。
_VIOLATION_LIMIT = 20

# 升级第 4 步与降级第 4 步。
_ADD_CHECK = (
    f"ALTER TABLE {_VERSIONS} ADD CONSTRAINT {_RETIRED_END_CHECK_NAME}"
    f" CHECK ({_RETIRED_END_CHECK}) ENFORCED"
)
_DROP_CHECK = f"ALTER TABLE {_VERSIONS} DROP CHECK {_RETIRED_END_CHECK_NAME}"

# 触发器名 → 建触发器的语句（写法照 0013 的 _TRIGGERS：整条 CREATE TRIGGER
# 是一条语句）。与 0013 的 `trg_provider_price_versions_before_update` 只差
# 五处（设计 §2），其余逐字相同：
# ① `RETIRED` 与 `DISCARDED` 都是终态；
# ② 草稿发布时 `effective_to` 必须为空（与 0015 同一写法）；
# ③ 不可变检查只对 `PUBLISHED`（`RETIRED` 已由 ① 整体拒绝）；
# ④ 相交判定按空值逐项判断，对 `DATETIME` 的任何取值都成立；
# ⑤ 已发布版本的 `effective_to` 只许「空 → 有值」（截断，或退役时写尽头）与
#    「有值 → 空、状态不变」（恢复为未截断），退役只许未截断的。
_TRIGGERS: dict[str, str] = {
    _NEW_TRIGGER: """
CREATE TRIGGER trg_provider_price_versions_before_update_v2
BEFORE UPDATE ON provider_price_versions
FOR EACH ROW
BEGIN
    DECLARE locked_provider_id BIGINT DEFAULT NULL;
    IF OLD.status IN ('RETIRED', 'DISCARDED') THEN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'a retired or discarded provider price version is final';
    END IF;
    IF OLD.status = 'DRAFT' AND NEW.status NOT IN ('DRAFT', 'PUBLISHED', 'DISCARDED') THEN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'a draft provider price version is published or discarded';
    END IF;
    IF OLD.status = 'DRAFT' AND NEW.status = 'PUBLISHED' AND NEW.effective_to IS NOT NULL THEN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'a provider price version is published without an end';
    END IF;
    IF OLD.status = 'PUBLISHED' THEN
        IF NOT (NEW.id <=> OLD.id)
           OR NOT (NEW.public_id <=> OLD.public_id)
           OR NOT (NEW.provider_id <=> OLD.provider_id)
           OR NOT (NEW.model_id <=> OLD.model_id)
           OR NOT (NEW.source_currency <=> OLD.source_currency)
           OR NOT (NEW.source_type <=> OLD.source_type)
           OR NOT (NEW.source_reference <=> OLD.source_reference)
           OR NOT (NEW.effective_from <=> OLD.effective_from)
           OR NOT (NEW.created_by <=> OLD.created_by)
           OR NOT (NEW.approved_by <=> OLD.approved_by)
           OR NOT (NEW.created_at <=> OLD.created_at)
           OR NOT (NEW.approved_at <=> OLD.approved_at) THEN
            SIGNAL SQLSTATE '45000'
                SET MESSAGE_TEXT = 'a published provider price version is immutable';
        END IF;
        IF NOT (NEW.status <=> OLD.status)
           AND NOT (OLD.status = 'PUBLISHED' AND NEW.status = 'RETIRED') THEN
            SIGNAL SQLSTATE '45000'
                SET MESSAGE_TEXT = 'a published provider price version can only be retired';
        END IF;
        IF (NOT (NEW.effective_to <=> OLD.effective_to)
            AND NOT (OLD.effective_to IS NULL AND NEW.effective_to IS NOT NULL)
            AND NOT (OLD.effective_to IS NOT NULL AND NEW.effective_to IS NULL
                     AND NEW.status <=> OLD.status))
           OR (NOT (NEW.status <=> OLD.status) AND OLD.effective_to IS NOT NULL) THEN
            SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT =
            'a published provider price version ends only by truncation, restore or retirement';
        END IF;
    END IF;
    IF OLD.status = 'DRAFT' AND NEW.status = 'PUBLISHED' THEN
        IF (SELECT COUNT(*)
              FROM provider_price_components
             WHERE provider_price_version_id = NEW.id) = 0 THEN
            SIGNAL SQLSTATE '45000'
                SET MESSAGE_TEXT = 'a provider price version needs at least one component';
        END IF;
        IF EXISTS (
            SELECT 1
              FROM usage_meter_components umc
             WHERE umc.meter_type_id IN (
                       SELECT m.meter_type_id
                         FROM provider_price_components c
                         JOIN usage_meter_components m ON m.id = c.usage_meter_component_id
                        WHERE c.provider_price_version_id = NEW.id)
               AND umc.id NOT IN (
                       SELECT usage_meter_component_id
                         FROM provider_price_components
                        WHERE provider_price_version_id = NEW.id)) THEN
            SIGNAL SQLSTATE '45000'
                SET MESSAGE_TEXT = 'a provider price version misses a meter type component';
        END IF;
    END IF;
    IF NEW.status IN ('PUBLISHED', 'RETIRED')
       AND (NOT (NEW.effective_from <=> OLD.effective_from)
            OR NOT (NEW.effective_to <=> OLD.effective_to)
            OR NOT (NEW.status <=> OLD.status)) THEN
        SELECT id INTO locked_provider_id
          FROM ai_providers
         WHERE id = NEW.provider_id
           FOR UPDATE;
        IF (NEW.effective_from IS NULL OR NEW.effective_to IS NULL
            OR NEW.effective_from < NEW.effective_to)
           AND EXISTS (
            SELECT 1
              FROM provider_price_versions o
             WHERE o.provider_id = NEW.provider_id
               AND o.model_id = NEW.model_id
               AND o.id <> NEW.id
               AND o.status IN ('PUBLISHED', 'RETIRED')
               AND (o.effective_from IS NULL OR o.effective_to IS NULL
                    OR o.effective_from < o.effective_to)
               AND (o.effective_from IS NULL OR NEW.effective_to IS NULL
                    OR o.effective_from < NEW.effective_to)
               AND (NEW.effective_from IS NULL OR o.effective_to IS NULL
                    OR NEW.effective_from < o.effective_to)) THEN
            SIGNAL SQLSTATE '45000'
                SET MESSAGE_TEXT = 'provider price periods overlap';
        END IF;
    END IF;
END
""",
}

# 降级恢复的 0013 原文（与 0013 的 _TRIGGERS 里同名的那条逐字一致，测试比对）。
_TRIGGERS_0013: dict[str, str] = {
    _OLD_TRIGGER: """
CREATE TRIGGER trg_provider_price_versions_before_update
BEFORE UPDATE ON provider_price_versions
FOR EACH ROW
BEGIN
    DECLARE locked_provider_id BIGINT DEFAULT NULL;
    IF OLD.status = 'DISCARDED' THEN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'a discarded provider price version is final';
    END IF;
    IF OLD.status = 'DRAFT' AND NEW.status NOT IN ('DRAFT', 'PUBLISHED', 'DISCARDED') THEN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'a draft provider price version is published or discarded';
    END IF;
    IF OLD.status IN ('PUBLISHED', 'RETIRED') THEN
        IF NOT (NEW.id <=> OLD.id)
           OR NOT (NEW.public_id <=> OLD.public_id)
           OR NOT (NEW.provider_id <=> OLD.provider_id)
           OR NOT (NEW.model_id <=> OLD.model_id)
           OR NOT (NEW.source_currency <=> OLD.source_currency)
           OR NOT (NEW.source_type <=> OLD.source_type)
           OR NOT (NEW.source_reference <=> OLD.source_reference)
           OR NOT (NEW.effective_from <=> OLD.effective_from)
           OR NOT (NEW.created_by <=> OLD.created_by)
           OR NOT (NEW.approved_by <=> OLD.approved_by)
           OR NOT (NEW.created_at <=> OLD.created_at)
           OR NOT (NEW.approved_at <=> OLD.approved_at) THEN
            SIGNAL SQLSTATE '45000'
                SET MESSAGE_TEXT = 'a published provider price version is immutable';
        END IF;
        IF NOT (NEW.status <=> OLD.status)
           AND NOT (OLD.status = 'PUBLISHED' AND NEW.status = 'RETIRED') THEN
            SIGNAL SQLSTATE '45000'
                SET MESSAGE_TEXT = 'a published provider price version can only be retired';
        END IF;
    END IF;
    IF OLD.status = 'DRAFT' AND NEW.status = 'PUBLISHED' THEN
        IF (SELECT COUNT(*)
              FROM provider_price_components
             WHERE provider_price_version_id = NEW.id) = 0 THEN
            SIGNAL SQLSTATE '45000'
                SET MESSAGE_TEXT = 'a provider price version needs at least one component';
        END IF;
        IF EXISTS (
            SELECT 1
              FROM usage_meter_components umc
             WHERE umc.meter_type_id IN (
                       SELECT m.meter_type_id
                         FROM provider_price_components c
                         JOIN usage_meter_components m ON m.id = c.usage_meter_component_id
                        WHERE c.provider_price_version_id = NEW.id)
               AND umc.id NOT IN (
                       SELECT usage_meter_component_id
                         FROM provider_price_components
                        WHERE provider_price_version_id = NEW.id)) THEN
            SIGNAL SQLSTATE '45000'
                SET MESSAGE_TEXT = 'a provider price version misses a meter type component';
        END IF;
    END IF;
    IF NEW.status IN ('PUBLISHED', 'RETIRED')
       AND (NOT (NEW.effective_from <=> OLD.effective_from)
            OR NOT (NEW.effective_to <=> OLD.effective_to)
            OR NOT (NEW.status <=> OLD.status)) THEN
        SELECT id INTO locked_provider_id
          FROM ai_providers
         WHERE id = NEW.provider_id
           FOR UPDATE;
        IF (NEW.effective_from IS NULL OR NEW.effective_to IS NULL
            OR NEW.effective_from < NEW.effective_to)
           AND EXISTS (
            SELECT 1
              FROM provider_price_versions o
             WHERE o.provider_id = NEW.provider_id
               AND o.model_id = NEW.model_id
               AND o.id <> NEW.id
               AND o.status IN ('PUBLISHED', 'RETIRED')
               AND (o.effective_from IS NULL OR o.effective_to IS NULL
                    OR o.effective_from < o.effective_to)
               AND COALESCE(o.effective_from, '1000-01-01')
                   < COALESCE(NEW.effective_to, '9999-12-31')
               AND COALESCE(NEW.effective_from, '1000-01-01')
                   < COALESCE(o.effective_to, '9999-12-31')) THEN
            SIGNAL SQLSTATE '45000'
                SET MESSAGE_TEXT = 'provider price periods overlap';
        END IF;
    END IF;
END
""",
}

# MySQL 的 CHECK 约束名在库内唯一：按名字查整个库，挂在别的表上的同名约束同样查得到。
_CHECK_QUERY = (
    "SELECT t.TABLE_NAME, t.CONSTRAINT_TYPE, t.ENFORCED, c.CHECK_CLAUSE"
    " FROM information_schema.TABLE_CONSTRAINTS t"
    " LEFT JOIN information_schema.CHECK_CONSTRAINTS c"
    " ON c.CONSTRAINT_SCHEMA = t.CONSTRAINT_SCHEMA AND c.CONSTRAINT_NAME = t.CONSTRAINT_NAME"
    " WHERE t.CONSTRAINT_SCHEMA = DATABASE()"
    f" AND t.CONSTRAINT_NAME = '{_RETIRED_END_CHECK_NAME}'"
)
# 触发器名在库内唯一。
_TRIGGERS_QUERY = (
    "SELECT TRIGGER_NAME, EVENT_OBJECT_TABLE, ACTION_TIMING, EVENT_MANIPULATION, ACTION_STATEMENT"
    " FROM information_schema.TRIGGERS WHERE TRIGGER_SCHEMA = DATABASE()"
    f" AND TRIGGER_NAME IN ('{_OLD_TRIGGER}', '{_NEW_TRIGGER}')"
)
_RETIRED_WITHOUT_END_QUERY = (
    f"SELECT id FROM {_VERSIONS} WHERE status = 'RETIRED' AND effective_to IS NULL"
    f" ORDER BY id LIMIT {_VIOLATION_LIMIT}"
)

_DESIGN = "docs/design/AIH-TASK-145-provider-price-backstop.md"


def _body(statement: str) -> str:
    """What MySQL keeps as ACTION_STATEMENT (the part after `FOR EACH ROW`), without the
    blanks at either end."""
    return statement.split("FOR EACH ROW", 1)[1].strip()


def _require_trigger_privilege(bind: sa.engine.Connection) -> None:
    """Refuse to run where the migration account could not create triggers (as 0013)."""
    log_bin, trusted = bind.execute(sa.text(_PREREQUISITE_QUERY)).one()
    if int(log_bin) == 1 and int(trusted) != 1:
        raise RuntimeError(_PREREQUISITE_ERROR)


def _check_rows(bind: sa.engine.Connection) -> list[tuple[str, str, str, str | None]]:
    """Every constraint of the database named like ours: (table, type, enforced, clause)."""
    return [
        (str(table), str(kind), str(enforced), None if clause is None else str(clause))
        for table, kind, enforced, clause in bind.execute(sa.text(_CHECK_QUERY)).all()
    ]


def _check_matches(rows: list[tuple[str, str, str, str | None]]) -> bool:
    """Exactly one row: a CHECK on our table, enforced, whose clause is the canonical text
    (design §2 upgrade step 2: no normalisation, the whole text must match)."""
    if len(rows) != 1:
        return False
    table, kind, enforced, clause = rows[0]
    return (
        table == _VERSIONS
        and kind == "CHECK"
        and enforced == "YES"
        and clause is not None
        and re.fullmatch(_RETIRED_END_CLAUSE_PATTERN, clause) is not None
    )


def _triggers(bind: sa.engine.Connection) -> dict[str, tuple[str, str, str, str]]:
    """The old and the new trigger, those that exist: name → (table, timing, event, body)."""
    return {
        str(name): (str(table), str(timing), str(kind), str(statement))
        for name, table, timing, kind, statement in bind.execute(sa.text(_TRIGGERS_QUERY)).all()
    }


def _trigger_matches(row: tuple[str, str, str, str], statement: str) -> bool:
    """On our table, BEFORE UPDATE, and the body word for word the frozen one (only the blanks
    at either end are dropped)."""
    table, timing, kind, body = row
    return (
        table == _VERSIONS
        and timing == "BEFORE"
        and kind == "UPDATE"
        and body.strip() == _body(statement)
    )


def _shape_error(direction: str, problems: list[str]) -> RuntimeError:
    return RuntimeError(
        f"Cannot {direction} 0031: " + "; ".join(problems) + ". No DDL was issued. An object"
        " of that name was made by hand: have a person compare it with"
        f" {_DESIGN} §2 before running the migration again"
    )


def _new_trigger_problem() -> str:
    return (
        f"trigger {_NEW_TRIGGER} exists but is not, word for word, the BEFORE UPDATE trigger"
        f" on {_VERSIONS} this migration builds"
    )


def _upgrade_offline() -> None:
    # 离线读不了库：按「什么都没做过」输出（照 0013 与 0030 的惯例）。
    op.execute(_ADD_CHECK)
    op.execute(_TRIGGERS[_NEW_TRIGGER])
    op.execute(f"DROP TRIGGER IF EXISTS {_OLD_TRIGGER}")


def upgrade() -> None:
    if op.get_context().as_sql:
        _upgrade_offline()
        return

    bind = op.get_bind()
    check_rows = _check_rows(bind)
    triggers = _triggers(bind)
    new = triggers.get(_NEW_TRIGGER)
    has_check = bool(check_rows)
    check_done = has_check and _check_matches(check_rows)
    new_done = new is not None and _trigger_matches(new, _TRIGGERS[_NEW_TRIGGER])
    # 已完成：不发任何语句。
    if check_done and new_done and _OLD_TRIGGER not in triggers:
        return

    # 第 1 步。⚠️ 必须排在任何 DDL 之前。
    _require_trigger_privilege(bind)
    # 第 2 步。
    problems = []
    if has_check and not check_done:
        problems.append(
            f"check {_RETIRED_END_CHECK_NAME} exists but is not exactly one enforced CHECK on"
            f" {_VERSIONS} with the condition {_RETIRED_END_CHECK}"
        )
    if new is not None and not new_done:
        problems.append(_new_trigger_problem())
    if problems:
        raise _shape_error("upgrade to", problems)

    if not has_check:
        # 第 3 步。
        ids = [int(i) for i in bind.execute(sa.text(_RETIRED_WITHOUT_END_QUERY)).scalars()]
        if ids:
            raise RuntimeError(
                f"Cannot upgrade to 0031: {_VERSIONS} has RETIRED versions without"
                f" effective_to (id {', '.join(str(i) for i in ids)}; at most"
                f' {_VIOLATION_LIMIT} listed). They break "a retired version has an end".'
                " No DDL was issued. Have a person decide each version's end and set it"
                " (UPDATE ... SET effective_to = ...), then deploy again"
            )
        # 第 4 步：一条原子 ALTER。
        op.execute(_ADD_CHECK)
    # 第 5 步。
    if new is None:
        op.execute(_TRIGGERS[_NEW_TRIGGER])
    # 第 6 步：一定在第 5 步之后。
    if _OLD_TRIGGER in triggers:
        op.execute(f"DROP TRIGGER {_OLD_TRIGGER}")


def _downgrade_offline() -> None:
    # 离线读不了库：按「升级已完成」输出。降级只放宽，离线输出不会造成数据错误。
    op.execute(_TRIGGERS_0013[_OLD_TRIGGER])
    op.execute(f"DROP TRIGGER IF EXISTS {_NEW_TRIGGER}")
    op.execute(_DROP_CHECK)


def downgrade() -> None:
    if op.get_context().as_sql:
        _downgrade_offline()
        return

    bind = op.get_bind()
    has_check = any(
        table == _VERSIONS and kind == "CHECK" for table, kind, _, _ in _check_rows(bind)
    )
    triggers = _triggers(bind)
    old = triggers.get(_OLD_TRIGGER)
    new = triggers.get(_NEW_TRIGGER)
    old_done = old is not None and _trigger_matches(old, _TRIGGERS_0013[_OLD_TRIGGER])
    if not has_check and old_done and new is None:
        return

    # 第 1 步：降级要重建旧触发器。
    _require_trigger_privilege(bind)
    # 第 2 步。
    problems = []
    if old is not None and not old_done:
        problems.append(
            f"trigger {_OLD_TRIGGER} exists but is not, word for word, the one 0013 built"
        )
    if new is not None and not _trigger_matches(new, _TRIGGERS[_NEW_TRIGGER]):
        problems.append(_new_trigger_problem())
    if problems:
        raise _shape_error("downgrade", problems)
    if old is None:
        op.execute(_TRIGGERS_0013[_OLD_TRIGGER])
    # 第 3 步：先建旧的、再删新的。
    if new is not None:
        op.execute(f"DROP TRIGGER {_NEW_TRIGGER}")
    # 第 4 步。
    if has_check:
        op.execute(_DROP_CHECK)

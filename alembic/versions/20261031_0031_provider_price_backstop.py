"""provider price versions get their database backstop: retired end check, stricter trigger

照设计闸门 #535 v5（docs/design/AIH-TASK-145-provider-price-backstop.md）
§2「数据库（迁移 0031）」与 §8；本迁移是 AIH-TASK-145。只改 `provider_price_versions`
的一条 CHECK 与它的 BEFORE UPDATE 触发器：

- 新 CHECK `ck_provider_price_versions_retired_end`：
  `status <> 'RETIRED' OR effective_to IS NOT NULL`（退役必有尽头；与汇率、
  定价规则、税率三张表的同名 CHECK 同一写法）；
- 新触发器 `trg_provider_price_versions_before_update_v2`，正文由 0013 的
  `trg_provider_price_versions_before_update` 改五处（设计 §2）：
  ① `RETIRED` 与 `DISCARDED` 都是终态；② 草稿发布时尽头必须为空；
  ③ 不可变检查只对 `PUBLISHED`；④ 区间相交按空值逐项判断、不再用哨兵日期；
  ⑤ 已发布版本的尽头只按截断、恢复、退役三种方式变。其余逐字相同；
- 删掉 0013 的 `trg_provider_price_versions_before_update`。

合起来：发布的区间都是 `[F, NULL)`，起点为空的发布一定是 `[NULL, NULL)`，
与任何非空区间都相交，所以只在该（供应商, 模型）没有区间非空的已发布 / 已退役
版本时通过（设计 §2「新规则合起来的效果」）。

`app/models/provider_prices.py` 同步了这条 CHECK（`RETIRED_END_CHECK`），
tests/backend/test_migrations.py 与
tests/backend/test_provider_price_backstop_migration.py 比对两边。
服务层、计费、接口与前端都不改。

步骤（升级，MySQL）
------------------

在线时**先判断是否已完成**：CHECK 存在且与目标逐字相同（`_check_is_target`）、
新触发器存在且与冻结正文逐字相同（`_trigger_is`）、旧触发器不存在
→ 直接返回，不发任何语句。否则依次：

1. **预检**：查 `@@log_bin` 与 `@@log_bin_trust_function_creators`（查询、判定与
   报错文案照 0013）。binlog 开着而开关关着时，应用账号建不了触发器
   （ERROR 1419），所以**在任何 DDL 之前**抛错。
2. **形状核对**（不做任何归一化）：已存在的同名 CHECK 必须恰好一行、在本表上、
   `ENFORCED = 'YES'`，`CHECK_CLAUSE` 的原文全文匹配 `_CHECK_CLAUSE_PATTERN`
   （MySQL 8.4 的规范写法，唯一可变的是字符集前缀，限三种）；已存在的新触发器
   必须在本表上、`BEFORE UPDATE`，`ACTION_STATEMENT` 去掉首尾空白后等于冻结正文
   （`FOR EACH ROW` 之后的部分）去掉首尾空白。不符就抛错、不发任何 DDL：
   只可能是手工建过同名对象，要人对照设计查看。
3. **数据核对**：CHECK 不存在时查退役而无尽头的行（`_RETIRED_WITHOUT_END_QUERY`），
   有就抛错、列出至多 20 个 `id`、不发任何 DDL。迁移不替人补尽头：尽头决定
   哪些时刻有价，猜错就是计费错误。
4. **加 CHECK**：CHECK 不存在时一条
   `ALTER TABLE … ADD CONSTRAINT … CHECK (…) ENFORCED`。MySQL 8 加 CHECK 时
   校验全部已有行，任一违反就整条失败（ERROR 3819）、表不变。
5. **建新触发器**：新触发器不存在时 `CREATE TRIGGER`（不带 `FOLLOWS` /
   `PRECEDES`：与旧的并存时排在它之后，两者任一拒绝即拒绝整条语句）。
6. **删旧触发器**：旧触发器存在时 `DROP TRIGGER`。

第 4 步在第 5 步之前，第 6 步一定在第 5 步之后：先有新的，才删旧的。
新触发器在每一处都不弱于旧的，所以「只有旧的」「两个都有」「只有新的」
三种状态下保护都不弱于 0013，不存在没有保护的时刻。

`--sql` 离线模式读不了库：照 0013 与 0030 的惯例跳过预检与全部判断，
按「什么都没做过」依次输出第 4 步的 `ALTER`、第 5 步的 `CREATE TRIGGER`、
旧触发器的 `DROP TRIGGER IF EXISTS`。离线脚本没有第 3 步：已有违反行时，
执行脚本的人在 `ALTER` 上得到 ERROR 3819、后面不再执行。

§132 第 13 条分析
-----------------

**已有数据**：**不改任何已有行**，不回填、不删行；只加一条 CHECK、建一个
触发器、删一个触发器。版本的 `id`、`public_id` 与区间都不变，用量事件的
`provider_price_version_id` 不动（INV-6）。已有行若违反新 CHECK（退役而无尽头），
第 3 步拒绝并列出它们，什么都不改。迁移不追溯判断已有行当初是否按规则发布
（设计 §10 假设 2，§8 的上线前核对）。

**锁表与停机**：`ADD CONSTRAINT … CHECK` 在 MySQL 8 上重建表
（`ALGORITHM=COPY`）并持元数据锁：重建期间允许读、写入排队；计费事务持有的
版本行共享锁让 ALTER 在取排他元数据锁前等它们提交，进行期间新的计费在元数据锁
上排队，不丢事件。表很小（生产 1 行），毫秒到秒级。`CREATE TRIGGER` /
`DROP TRIGGER` 各取一次短暂的排他元数据锁。任何时刻这张表都有 BEFORE UPDATE
保护，也没有整表不可写的时刻。不需要停机。

**备份**：要求前置备份（与 0005–0030 同一口径）。本版不写任何业务数据。

**部署顺序**：`deploy.sh` 先迁移、再换镜像（设计 §8）。新约束只拒绝服务层从不
写出的状态，所以迁移之后、新容器起来之前的旧代码照常运行；新代码只在模型上
多声明这条 CHECK（SQLite 建表用）。上线前在生产库上只读跑一次第 3 步的查询
（设计 §8「上线前核对」）。

**失败处理**：⚠️ MySQL 的 DDL **不参与事务**，每一步与写 `alembic_version`
之间都有失败窗口；每一步都按库的现状判断，可以安全重跑（设计 §5）：

- 预检失败：什么都没改，`alembic_version` 仍是
  `0030_pricing_rule_project_scope`。给账号打开
  `log_bin_trust_function_creators` 后重新部署。
- 形状核对不符：什么都没改。人对照设计查看手工建过的同名对象，删掉或改正后
  重新部署。
- 数据核对发现退役无尽头的行：什么都没改。人逐行判断应有的尽头、手工补上
  （0013 的触发器下 `UPDATE … SET effective_to = …` 允许）后重新部署。
- 加 CHECK 失败（第 3 步之后才写进的违反行，或其他 DDL 错误）：表不变
  （InnoDB DDL 原子），触发器仍是 0013 的；重新部署时第 3 步列出违反行。
- 加了 CHECK、建新触发器之前中断：有新 CHECK、只有旧触发器，不弱于 0013；
  重新部署时跳过 CHECK，建新的、删旧的。
- 建了新触发器、删旧的之前中断：两个并存，效果等于新的；重新部署时只删旧的。
- 全部完成、写 `alembic_version` 之前退出：重新部署时判为已完成，不发任何
  语句，只前进版本。

**回滚**：可降级，降级只放宽约束、不改任何行，没有数据前提。`downgrade` 同样
按现状判断、可重跑：先判断是否已完成（CHECK 不存在、旧触发器存在且与冻结的
0013 正文逐字相同、新触发器不存在 → 直接返回）；否则依次：预检（降级要重建
旧触发器）；旧触发器存在但与冻结的 0013 正文不同、或新触发器存在但与本迁移的
正文不同 → 抛错、不发任何 DDL；旧触发器不存在时按冻结的 0013 正文建它；
新触发器存在时删它；CHECK 存在时删它。先建旧的、再删新的，理由同升级。
`--sql` 离线模式的降级按「升级已完成」依次输出旧触发器的 `CREATE TRIGGER`、
新触发器的 `DROP TRIGGER IF EXISTS`、`ALTER … DROP CHECK`（只放宽，离线输出
不会造成数据错误）。代码回滚到本迁移之前要先降级。

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
_CHECK_NAME = "ck_provider_price_versions_retired_end"
_OLD_TRIGGER = "trg_provider_price_versions_before_update"
_NEW_TRIGGER = "trg_provider_price_versions_before_update_v2"
# 形状核对里触发器的时机与事件（information_schema.TRIGGERS 的写法）。
_TRIGGER_TIMING = "BEFORE"
_TRIGGER_EVENT = "UPDATE"
# 升级第 3 步的消息里至多列出这么多个 id。
_LISTED_IDS = 20

_PREREQUISITE_QUERY = "SELECT @@log_bin, @@log_bin_trust_function_creators"
_PREREQUISITE_ERROR = (
    "MySQL has binary logging on and log_bin_trust_function_creators OFF, "
    + "so this account cannot create the provider price backstop trigger (ERROR 1419). "
    + "Set log_bin_trust_function_creators = ON first; nothing has been changed."
)

# ⚠️ 冻结的字面量：与 app/models/provider_prices.py 的 RETIRED_END_CHECK
# 逐字一致（空白除外），tests/backend/test_migrations.py 比对两边。
_RETIRED_END_CHECK = "status <> 'RETIRED' OR effective_to IS NOT NULL"
# MySQL 8.4 保存这一条件的规范写法（CHECK_CONSTRAINTS.CHECK_CLAUSE 的原文）。
# 唯一可变的是字符集前缀，且只能是这三个之一（取决于建约束时连接的字符集）。
# 用 re.fullmatch 判断，不做任何归一化（设计 §2 升级第 2 步）。
_CHECK_CLAUSE_PATTERN = (
    r"^\(\(`status` <> _(utf8mb4|utf8mb3|latin1)\\'RETIRED\\'\)"
    r" or \(`effective_to` is not null\)\)$"
)

# 升级第 4 步与降级第 4 步：各一条 ALTER。显式写 ENFORCED（与默认相同）。
_ALTER_ADD_CHECK = (
    f"ALTER TABLE {_VERSIONS} ADD CONSTRAINT {_CHECK_NAME} CHECK ({_RETIRED_END_CHECK}) ENFORCED"
)
_ALTER_DROP_CHECK = f"ALTER TABLE {_VERSIONS} DROP CHECK {_CHECK_NAME}"

# 触发器名 → 建触发器的语句（写法照 0013 的 _TRIGGERS：整条 CREATE TRIGGER 是
# 一条语句）。与 0013 的 `trg_provider_price_versions_before_update` 只差五处
# （设计 §2）：
# ① 第一段 `OLD.status IN ('RETIRED', 'DISCARDED')`，消息随之改；
# ② 「草稿只能改成 …」之后新增：发布时 `NEW.effective_to` 必须为空
#    （与 0015 同一写法）；
# ③ 不可变检查只对 `OLD.status = 'PUBLISHED'`（`RETIRED` 已由 ① 整体拒绝）；
# ④ 区间相交按空值逐项判断，不再用哨兵日期（半开区间，空端为无穷）；
# ⑤ 不可变检查之内、「只能退役」之后新增：尽头只许空 → 有值（截断，或退役时
#    写尽头）、有值 → 空且状态不变（恢复为未截断）；退役只许退役前未截断。
# 分量完整一段一字不改；区间不重叠一段除 ④ 外不改（先锁供应商行，再查）。
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
           OR (NEW.status = 'RETIRED' AND OLD.effective_to IS NOT NULL) THEN
            SIGNAL SQLSTATE '45000'
                SET MESSAGE_TEXT = 'a published provider price version ends only by truncation, restore or retirement';
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
""",  # noqa: E501 - 第 5 处的消息是设计写死的文本；正文逐字冻结，不折行
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

# CHECK 约束名在库内唯一：按库与名字查，挂在别的表上的同名约束也查得到
# （形状核对判为不符）。
_CHECK_QUERY = (
    "SELECT t.TABLE_NAME, t.CONSTRAINT_TYPE, t.ENFORCED, c.CHECK_CLAUSE"
    " FROM information_schema.TABLE_CONSTRAINTS t"
    " LEFT JOIN information_schema.CHECK_CONSTRAINTS c"
    " ON c.CONSTRAINT_SCHEMA = t.CONSTRAINT_SCHEMA AND c.CONSTRAINT_NAME = t.CONSTRAINT_NAME"
    f" WHERE t.CONSTRAINT_SCHEMA = DATABASE() AND t.CONSTRAINT_NAME = '{_CHECK_NAME}'"
)
# 触发器名在库内唯一。
_TRIGGERS_QUERY = (
    "SELECT TRIGGER_NAME, EVENT_OBJECT_TABLE, ACTION_TIMING, EVENT_MANIPULATION, ACTION_STATEMENT"
    " FROM information_schema.TRIGGERS WHERE TRIGGER_SCHEMA = DATABASE()"
    f" AND TRIGGER_NAME IN ('{_OLD_TRIGGER}', '{_NEW_TRIGGER}')"
)
# 升级第 3 步：违反「退役必有尽头」的行。
_RETIRED_WITHOUT_END_QUERY = (
    f"SELECT id FROM {_VERSIONS} WHERE status = 'RETIRED' AND effective_to IS NULL"
    f" ORDER BY id LIMIT {_LISTED_IDS}"
)

_DESIGN = "docs/design/AIH-TASK-145-provider-price-backstop.md"


def _body(statement: str) -> str:
    """`FOR EACH ROW` 之后的部分去掉首尾空白：MySQL 保存为 ACTION_STATEMENT 的那段。"""
    return statement.split("FOR EACH ROW", 1)[1].strip()


def _drop_trigger(name: str) -> str:
    return f"DROP TRIGGER {name}"


def _require_trigger_privilege(bind: sa.engine.Connection) -> None:
    """Refuse to run where the migration account could not create triggers (as 0013)."""
    log_bin, trusted = bind.execute(sa.text(_PREREQUISITE_QUERY)).one()
    if int(log_bin) == 1 and int(trusted) != 1:
        raise RuntimeError(_PREREQUISITE_ERROR)


def _check_rows(bind: sa.engine.Connection) -> list[tuple[str, str, str, str | None]]:
    """库里叫这个名字的约束：(表, 类型, ENFORCED, CHECK_CLAUSE)。空列表 = 不存在。"""
    return [
        (str(table), str(kind), str(enforced), None if clause is None else str(clause))
        for table, kind, enforced, clause in bind.execute(sa.text(_CHECK_QUERY)).all()
    ]


def _check_is_target(rows: list[tuple[str, str, str, str | None]]) -> bool:
    """同名 CHECK 与本迁移要建的逐字相同（设计 §2 升级第 2 步）：恰好一行、
    是 CHECK、在本表上、`ENFORCED = 'YES'`，原文全文匹配 `_CHECK_CLAUSE_PATTERN`。"""
    if len(rows) != 1:
        return False
    table, kind, enforced, clause = rows[0]
    return (
        kind == "CHECK"
        and table == _VERSIONS
        and enforced == "YES"
        and clause is not None
        and re.fullmatch(_CHECK_CLAUSE_PATTERN, clause) is not None
    )


def _check_on_table(rows: list[tuple[str, str, str, str | None]]) -> bool:
    """本表上有这条 CHECK（降级第 4 步删的就是它）。"""
    return any(kind == "CHECK" and table == _VERSIONS for table, kind, _, _ in rows)


def _triggers(bind: sa.engine.Connection) -> dict[str, tuple[str, str, str, str]]:
    """新旧两个触发器中存在的：名字 → (表, 时机, 事件, ACTION_STATEMENT)。"""
    return {
        str(name): (str(table), str(timing), str(kind), str(statement))
        for name, table, timing, kind, statement in bind.execute(sa.text(_TRIGGERS_QUERY)).all()
    }


def _trigger_is(found: tuple[str, str, str, str], statement: str) -> bool:
    """存在的触发器与冻结的语句逐字相同：本表、`BEFORE UPDATE`，ACTION_STATEMENT
    去掉首尾空白后等于冻结正文去掉首尾空白，不做其他任何改写。"""
    table, timing, kind, action = found
    return (
        table == _VERSIONS
        and timing == _TRIGGER_TIMING
        and kind == _TRIGGER_EVENT
        and action.strip() == _body(statement)
    )


def _upgrade_offline() -> None:
    # 离线读不了库：按「什么都没做过」输出（照 0013 与 0030 的惯例）。
    op.execute(_ALTER_ADD_CHECK)
    op.execute(_TRIGGERS[_NEW_TRIGGER])
    op.execute(f"DROP TRIGGER IF EXISTS {_OLD_TRIGGER}")


def upgrade() -> None:
    if op.get_context().as_sql:
        _upgrade_offline()
        return

    bind = op.get_bind()
    check = _check_rows(bind)
    triggers = _triggers(bind)
    new = triggers.get(_NEW_TRIGGER)
    check_done = _check_is_target(check)
    new_done = new is not None and _trigger_is(new, _TRIGGERS[_NEW_TRIGGER])
    # 已完成：不发任何语句。
    if check_done and new_done and _OLD_TRIGGER not in triggers:
        return

    # 第 1 步。⚠️ 必须排在任何 DDL 之前。
    _require_trigger_privilege(bind)

    # 第 2 步：同名对象存在就必须逐字相同，否则不动库。
    problems = []
    if check and not check_done:
        problems.append(
            f"check {_CHECK_NAME} exists but is not the one this migration builds (it must be the"
            f" only constraint of that name, a CHECK on {_VERSIONS}, ENFORCED, with the clause"
            f" {_RETIRED_END_CHECK!r} as MySQL stores it)"
        )
    if new is not None and not new_done:
        problems.append(
            f"trigger {_NEW_TRIGGER} exists but is not the one this migration builds (it must be"
            f" BEFORE UPDATE on {_VERSIONS} with this migration's body, word for word)"
        )
    if problems:
        raise RuntimeError(
            "Cannot upgrade to 0031: " + "; ".join(problems) + ". No DDL was issued. An object of"
            f" that name was made by hand: have a person compare it with {_DESIGN} §2 before"
            " upgrading again"
        )

    if not check:
        # 第 3 步。
        violating = [int(row_id) for (row_id,) in bind.execute(sa.text(_RETIRED_WITHOUT_END_QUERY))]
        if violating:
            listed = ", ".join(str(row_id) for row_id in violating)
            raise RuntimeError(
                f"Cannot upgrade to 0031: {_VERSIONS} has RETIRED rows without effective_to (id"
                f" {listed}; at most {_LISTED_IDS} listed). They break 'a retired version has an"
                f" end' ({_CHECK_NAME}). No DDL was issued. A person must decide each row's end"
                f" and set it by hand (UPDATE {_VERSIONS} SET effective_to = ... is allowed by"
                " the 0013 trigger), then deploy again; the migration does not guess an end"
            )
        # 第 4 步：一条 ALTER，原子。
        op.execute(_ALTER_ADD_CHECK)
    # 第 5 步。不带 FOLLOWS / PRECEDES。
    if new is None:
        op.execute(_TRIGGERS[_NEW_TRIGGER])
    # 第 6 步：一定在第 5 步之后。
    if _OLD_TRIGGER in triggers:
        op.execute(_drop_trigger(_OLD_TRIGGER))


def _downgrade_offline() -> None:
    # 离线按「升级已完成」输出；降级只放宽，不像 0030 那样拒绝离线降级。
    op.execute(_TRIGGERS_0013[_OLD_TRIGGER])
    op.execute(f"DROP TRIGGER IF EXISTS {_NEW_TRIGGER}")
    op.execute(_ALTER_DROP_CHECK)


def downgrade() -> None:
    if op.get_context().as_sql:
        _downgrade_offline()
        return

    bind = op.get_bind()
    check = _check_on_table(_check_rows(bind))
    triggers = _triggers(bind)
    old = triggers.get(_OLD_TRIGGER)
    new = triggers.get(_NEW_TRIGGER)
    old_done = old is not None and _trigger_is(old, _TRIGGERS_0013[_OLD_TRIGGER])
    # 已完成：不发任何语句。
    if not check and old_done and new is None:
        return

    # 第 1 步：降级要重建旧触发器。
    _require_trigger_privilege(bind)

    # 第 2 步：不建、不删来历不明的触发器。
    problems = []
    if old is not None and not old_done:
        problems.append(
            f"trigger {_OLD_TRIGGER} exists but is not 0013's (it must be BEFORE UPDATE on"
            f" {_VERSIONS} with 0013's body, word for word)"
        )
    if new is not None and not _trigger_is(new, _TRIGGERS[_NEW_TRIGGER]):
        problems.append(
            f"trigger {_NEW_TRIGGER} exists but is not the one this migration builds (it must be"
            f" BEFORE UPDATE on {_VERSIONS} with this migration's body, word for word)"
        )
    if problems:
        raise RuntimeError(
            "Cannot downgrade 0031: " + "; ".join(problems) + ". No DDL was issued. A trigger of"
            f" that name was made by hand: have a person compare it with {_DESIGN} §2 before"
            " downgrading again"
        )
    if old is None:
        op.execute(_TRIGGERS_0013[_OLD_TRIGGER])
    # 第 3 步：先建旧的、再删新的。
    if new is not None:
        op.execute(_drop_trigger(_NEW_TRIGGER))
    # 第 4 步。
    if check:
        op.execute(_ALTER_DROP_CHECK)

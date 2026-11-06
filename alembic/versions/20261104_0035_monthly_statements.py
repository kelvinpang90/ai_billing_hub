"""monthly statements table with its two immutability triggers

照设计闸门 #565 v6（docs/design/AIH-TASK-154-monthly-statements.md）§2「数据库」、§3 INV-12、
§4 与 §8；本迁移是 AIH-TASK-154。只建一张新表 `monthly_statements`（14 列）与它的两个触发器，
不改任何已有表、不回填：

- `uq_monthly_statements_public_id (public_id)`、
  `uq_monthly_statements_statement_number (statement_number)`；
- `uq_monthly_statements_tenant_period (tenant_id, period_start)`：每个租户每个账期至多一份
  （V1 没有更正版本），也是生成的幂等键；同时是 `tenant_id` 外键用的索引；
- `ix_monthly_statements_period (period_start, tenant_id)`：管理端按账期列、告警查缺失；
- `ix_monthly_statements_generated_by (generated_by)` 与
  `ix_monthly_statements_tax_policy_version_id (tax_policy_version_id)`：**显式声明**，供两个
  外键使用，所以 MySQL 不另建与外键同名的隐式索引（否则形状核对对不上）；
- 三个外键 `fk_monthly_statements_tenant_id` → `tenants.id`、
  `fk_monthly_statements_generated_by` → `users.id`、
  `fk_monthly_statements_tax_policy_version_id` → `tax_policy_versions.id`，都是
  `ON DELETE RESTRICT`；
- 四条 CHECK：`status`、`period`、`high_sequence`、`finalized_at`；
- MySQL 上两个触发器 `trg_monthly_statements_before_update`（BEFORE UPDATE）与
  `trg_monthly_statements_before_delete`（BEFORE DELETE）：`OLD.status <> 'DRAFT'` 时 SIGNAL
  45000（INV-12 的数据库兜底）。SQLite 不建触发器（与 0006 等迁移同一惯例）。

没有写入方：账本归属与期初期末属于 AIH-TASK-155，行由 AIH-TASK-156 的生成函数写，任务属于
AIH-TASK-157。`app/models/statements.py` 的 `MonthlyStatement` 同步声明了这些列、索引与约束，
`tests/backend/test_monthly_statements_migration.py` 比对两边一致。

步骤
----

1. 用 inspector 判 `monthly_statements` 表。「形状」逐项比（设计 §2「数据库」）：恰好这 14 列
   （名称、类型与宽度、可空性）；恰好这六个索引（三个唯一、三个普通；名称、列与列顺序，没有
   前缀长度）；恰好这三个外键（列、引用表与列、`ON DELETE RESTRICT`，MySQL 上的删除规则读
   `information_schema.REFERENTIAL_CONSTRAINTS`）；恰好这四条 CHECK（名称）。任何一项缺失、
   多出或不同都算形状不对 → 抛错，点名表，**不发任何 DDL**。
2. MySQL：读 `information_schema.TRIGGERS` 里这两个名字的触发器，逐个比 `EVENT_OBJECT_TABLE`、
   `ACTION_TIMING`、`EVENT_MANIPULATION` 与 `ACTION_STATEMENT`（去掉首尾空白后与 `_TRIGGER_BODY`
   逐字相等）。存在但任一项不一致 → 抛错，**不发任何 DDL**。
3. 表在、两个触发器都在且一致 → 不发 DDL，只把版本前进。
4. **预检**（MySQL，紧接在第一条 DDL 之前）：查 `@@log_bin` 与
   `@@log_bin_trust_function_creators`（查询、判定与报错文案照 0022、0030）。binlog 开着而开关
   关着时，应用账号建不了触发器（ERROR 1419），所以**在任何 DDL 之前**抛错。
5. 表不存在 → 建表。MySQL：**一条** `CREATE TABLE monthly_statements (…)`，索引、外键与 CHECK
   都写在这一条里。其他方言：`create_table` 加 `create_index`（DDL 在 Alembic 的迁移事务里）。
6. MySQL：缺哪个触发器就建哪个（先 BEFORE UPDATE 后 BEFORE DELETE），已在的不动 —— 补完在建表
   与建触发器之间、或两个触发器之间中断的升级。

`--sql` 离线模式读不了库：照 0030 的 `_upgrade_offline`，跳过预检与全部检查，按「什么都没
做过」输出 `CREATE TABLE` 与两条 `CREATE TRIGGER`。

§132 第 13 条分析
-----------------

**已有数据**：只建一张新表与它的两个触发器，不改任何已有表的列、索引、约束与触发器，不回填，
不读写任何已有行；钱包与账本不被碰（INV-4、INV-5）。外键只指向 `tenants`、`users` 与
`tax_policy_versions`。

**锁表与停机**：建新表不锁任何已有表的行；建三个外键时 MySQL 对 `tenants`、`users` 与
`tax_policy_versions` 各取短暂的元数据锁（等正在进行的、碰过它们的事务结束），不扫行。建
触发器只锁这张新表（空表，没有读写方）。不需要停机。

**备份**：要求前置备份（与 0005–0034 同一口径）。本版不写任何数据。

**部署顺序**：`deploy.sh` 先迁移、再换镜像（设计 §8）。本任务不带写入方与接口，部署后只多一张
没人读写的空表；旧代码不知道它。先迁移后换镜像、或反过来都安全（设计 §8「兼容窗口」）。之后
依次是 AIH-TASK-155、156、157。

**失败处理**：⚠️ MySQL 的 DDL **不参与事务**，建表、两条建触发器与写 `alembic_version` 之间
都有失败窗口。表连同索引、外键与 CHECK 在**一条** `CREATE TABLE` 里建，MySQL 8 的 InnoDB DDL
是原子的，加上第 1、2 步的检查，每个窗口都有确定的结局：

- 预检失败：什么都没改，`alembic_version` 仍是 `0034_tax_policy_backstop`。给账号打开
  `log_bin_trust_function_creators` 后重新部署。
- `CREATE TABLE` 失败（锁等待超时、进程被杀）→ 整条回滚，表不存在，版本仍是 0034 → 原样
  重新部署。
- 表建成、某个触发器建成之前中断 → 表在、缺一个或两个触发器，版本仍是 0034 → 原样重新部署：
  第 1、2 步判形状与已在的触发器正确，只建缺的那个（或那两个）。这期间没有写入方（156 上线之前），
  缺触发器不会让定稿行被改。
- 全部建成、写 `alembic_version` 之前进程退出 → 原样重新部署：判为已完成，不发 DDL，只把版本
  前进。
- 有人手工建过同名但形状不同的表 → 重跑在第 1 步失败，报错点名这张表与处理方法，不发 DDL，版本
  仍是 0034 → 先确认表里没有任何行：为空则 `DROP TABLE monthly_statements`（触发器随表删除）再
  重新部署；有行则停下，按数据事故处理，不要删。
- 同名触发器存在但时机、事件、所在表或正文不同 → 重跑在第 2 步失败，报错点名触发器，不发 DDL，
  版本仍是 0034 → 人对照设计查看；确认是手工建的之后 `DROP TRIGGER` 它，再重新部署（只补建
  缺的那个）。

**回滚**：生产上的回滚**只回滚应用镜像，不降级 0035**（设计 §8）。`downgrade` **只删空表**：
`--sql` 离线模式判断不了表是否为空，直接抛错（须在线执行），不输出任何 `DROP`；表不存在时什么
都不做；先 `SELECT 1 FROM monthly_statements LIMIT 1`，有任何一行就抛错（消息点名这张表、处理
方法是保留表只回滚应用镜像），不发任何 DDL、不删任何行 —— 已定稿的对账单是不可变的财务快照，
删了无法重建；表为空时 MySQL 上先删在这张表上的那两个触发器（只删存在的），再删表，不碰任何
已有表。

Revision ID: 0035_monthly_statements
Revises: 0034_tax_policy_backstop
Create Date: 2026-11-04
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import NoReturn

import sqlalchemy as sa

from alembic import op

revision: str = "0035_monthly_statements"
down_revision: str | None = "0034_tax_policy_backstop"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "monthly_statements"
_TENANTS = "tenants"
_USERS = "users"
_TAX_POLICY_VERSIONS = "tax_policy_versions"

# ⚠️ 下面都是冻结的字面量；模型那边的声明由
# tests/backend/test_monthly_statements_migration.py 比对。

# 列名 → (类型与宽度, 可空)，按建表顺序。类型写成 MySQL 反射出来的样子（去掉空白）。
_COLUMNS: dict[str, tuple[str, bool]] = {
    "id": ("BIGINT", False),
    "public_id": ("CHAR(36)", False),
    "tenant_id": ("BIGINT", False),
    "statement_number": ("VARCHAR(32)", False),
    "period_start": ("DATETIME", False),
    "period_end": ("DATETIME", False),
    "cutoff_at": ("DATETIME", False),
    "status": ("VARCHAR(16)", False),
    "ledger_high_sequence": ("BIGINT", False),
    "snapshot_json": ("JSON", False),
    "generated_by": ("BIGINT", True),
    "tax_policy_version_id": ("BIGINT", False),
    "finalized_at": ("DATETIME", True),
    "created_at": ("DATETIME", False),
}

_UNIQUE_PUBLIC_ID = "uq_monthly_statements_public_id"
_UNIQUE_STATEMENT_NUMBER = "uq_monthly_statements_statement_number"
_UNIQUE_TENANT_PERIOD = "uq_monthly_statements_tenant_period"
_INDEX_PERIOD = "ix_monthly_statements_period"
_INDEX_GENERATED_BY = "ix_monthly_statements_generated_by"
_INDEX_TAX_POLICY_VERSION = "ix_monthly_statements_tax_policy_version_id"
# 索引名 → (列, 唯一)，按建的顺序。
_INDEXES: dict[str, tuple[tuple[str, ...], bool]] = {
    _UNIQUE_PUBLIC_ID: (("public_id",), True),
    _UNIQUE_STATEMENT_NUMBER: (("statement_number",), True),
    _UNIQUE_TENANT_PERIOD: (("tenant_id", "period_start"), True),
    _INDEX_PERIOD: (("period_start", "tenant_id"), False),
    _INDEX_GENERATED_BY: (("generated_by",), False),
    _INDEX_TAX_POLICY_VERSION: (("tax_policy_version_id",), False),
}
# 普通索引：其他方言上 `create_table` 之后逐个 `create_index`。
_PLAIN_INDEXES = (_INDEX_PERIOD, _INDEX_GENERATED_BY, _INDEX_TAX_POLICY_VERSION)

_FK_TENANT = "fk_monthly_statements_tenant_id"
_FK_GENERATED_BY = "fk_monthly_statements_generated_by"
_FK_TAX_POLICY_VERSION = "fk_monthly_statements_tax_policy_version_id"
# 外键名 → (列, 引用表, 引用列, 删除规则)。
_FOREIGN_KEYS: dict[str, tuple[tuple[str, ...], str, tuple[str, ...], str]] = {
    _FK_TENANT: (("tenant_id",), _TENANTS, ("id",), "RESTRICT"),
    _FK_GENERATED_BY: (("generated_by",), _USERS, ("id",), "RESTRICT"),
    _FK_TAX_POLICY_VERSION: (("tax_policy_version_id",), _TAX_POLICY_VERSIONS, ("id",), "RESTRICT"),
}

# 名字 → 条件。与 app/models/statements.py 上的 CheckConstraint 一致（空白除外）。
_CHECKS: dict[str, str] = {
    "ck_monthly_statements_status": "status IN ('DRAFT', 'FINALIZED', 'VOID')",
    "ck_monthly_statements_period": "period_start < period_end AND period_end < cutoff_at",
    "ck_monthly_statements_high_sequence": "ledger_high_sequence >= 0",
    "ck_monthly_statements_finalized_at": "status <> 'FINALIZED' OR finalized_at IS NOT NULL",
}


def _constraints_ddl() -> str:
    uniques = [
        f"    CONSTRAINT {name} UNIQUE ({', '.join(columns)})"
        for name, (columns, unique) in _INDEXES.items()
        if unique
    ]
    indexes = [
        f"    INDEX {name} ({', '.join(columns)})"
        for name, (columns, unique) in _INDEXES.items()
        if not unique
    ]
    keys = [
        f"    CONSTRAINT {name} FOREIGN KEY ({', '.join(columns)})"
        f" REFERENCES {referred} ({', '.join(referred_columns)}) ON DELETE {rule}"
        for name, (columns, referred, referred_columns, rule) in _FOREIGN_KEYS.items()
    ]
    checks = [f"    CONSTRAINT {name} CHECK ({condition})" for name, condition in _CHECKS.items()]
    return ",\n".join([*uniques, *indexes, *keys, *checks])


# MySQL 上的建表语句：一条，原子。
_CREATE_TABLE_MYSQL = f"""
CREATE TABLE {_TABLE} (
    id BIGINT NOT NULL AUTO_INCREMENT,
    public_id CHAR(36) NOT NULL,
    tenant_id BIGINT NOT NULL,
    statement_number VARCHAR(32) NOT NULL,
    period_start DATETIME NOT NULL,
    period_end DATETIME NOT NULL,
    cutoff_at DATETIME NOT NULL,
    status VARCHAR(16) NOT NULL,
    ledger_high_sequence BIGINT NOT NULL,
    snapshot_json JSON NOT NULL,
    generated_by BIGINT NULL,
    tax_policy_version_id BIGINT NOT NULL,
    finalized_at DATETIME NULL,
    created_at DATETIME NOT NULL,
    PRIMARY KEY (id),
{_constraints_ddl()}
)
"""

# 两个触发器共用的主体（设计 §2「数据库」）：MySQL 把 `FOR EACH ROW` 之后的部分原样存进
# ACTION_STATEMENT，第 2 步去掉首尾空白后与它逐字比较。
_TRIGGER_BODY = """
BEGIN
    IF OLD.status <> 'DRAFT' THEN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'monthly_statements: a finalized or void statement is immutable';
    END IF;
END
""".strip()

_UPDATE_TRIGGER = "trg_monthly_statements_before_update"
_DELETE_TRIGGER = "trg_monthly_statements_before_delete"
# 触发器名 → (时机, 事件)，按建的顺序。
_TRIGGER_EVENTS: dict[str, tuple[str, str]] = {
    _UPDATE_TRIGGER: ("BEFORE", "UPDATE"),
    _DELETE_TRIGGER: ("BEFORE", "DELETE"),
}


def _create_trigger(name: str) -> str:
    """整条 CREATE TRIGGER 是**一条**语句（与 0022 同一写法）。"""
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
    + "so this account cannot create the monthly statement triggers (ERROR 1419). "
    + "Set log_bin_trust_function_creators = ON first; nothing has been changed."
)

# MySQL 上外键的删除规则以 information_schema 为准（与 test_migrations.py 同一个来源）：
# 反射不返回缺省的 RESTRICT。
_DELETE_RULES_MYSQL = (
    "SELECT CONSTRAINT_NAME, DELETE_RULE FROM information_schema.REFERENTIAL_CONSTRAINTS"
    f" WHERE CONSTRAINT_SCHEMA = DATABASE() AND TABLE_NAME = '{_TABLE}'"
)
# 按名字找：同名触发器挂在别的表上也要看见（触发器名在库内唯一）。
_TRIGGERS_QUERY = (
    "SELECT TRIGGER_NAME, EVENT_OBJECT_TABLE, ACTION_TIMING, EVENT_MANIPULATION, ACTION_STATEMENT"
    " FROM information_schema.TRIGGERS WHERE TRIGGER_SCHEMA = DATABASE()"
    f" AND TRIGGER_NAME IN ('{_UPDATE_TRIGGER}', '{_DELETE_TRIGGER}')"
)

# 降级前的数据检查（设计 §2、§8）：有任何一行就拒绝。
_HAS_ROWS = f"SELECT 1 FROM {_TABLE} LIMIT 1"

_DESIGN = "docs/design/AIH-TASK-154-monthly-statements.md"


def _is_mysql() -> bool:
    return op.get_context().dialect.name == "mysql"


def _type_text(column_type: object) -> str:
    """`VARCHAR(16) COLLATE …` → `VARCHAR(16)`：只留类型与宽度。"""
    return str(column_type).replace(", ", ",").split(" ")[0].upper()


def _reflected_shape() -> dict | None:
    """`monthly_statements` 的现状（列、索引、外键、CHECK 名）；表不存在时返回 None。"""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table(_TABLE):
        return None

    columns = {
        str(column["name"]): (_type_text(column["type"]), bool(column["nullable"]))
        for column in inspector.get_columns(_TABLE)
    }
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
        delete_rules = dict(bind.execute(sa.text(_DELETE_RULES_MYSQL)).all())
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
        "indexes": indexes,
        "foreign_keys": foreign_keys,
        "checks": checks,
    }


def _shape_problems(shape: dict) -> list[str]:
    """与设计不一致的项；空列表 = 形状正确。"""
    problems = []
    if shape["columns"] != _COLUMNS:
        problems.append("columns (name, type and width, nullability)")
    if shape["indexes"] != _INDEXES:
        problems.append("indexes (name, columns and their order, uniqueness)")
    if shape["foreign_keys"] != _FOREIGN_KEYS:
        problems.append("foreign keys (columns, referred table, ON DELETE RESTRICT)")
    if shape["checks"] != set(_CHECKS):
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
    """Refuse to run where the migration account could not create triggers (as 0022, 0030)."""
    log_bin, trusted = bind.execute(sa.text(_PREREQUISITE_QUERY)).one()
    if int(log_bin) == 1 and int(trusted) != 1:
        raise RuntimeError(_PREREQUISITE_ERROR)


def _refuse_shape(problems: list[str]) -> NoReturn:
    raise RuntimeError(
        f"Cannot upgrade to 0035: table {_TABLE} exists but differs from the design in "
        + "; ".join(problems)
        + f". No DDL was issued. First check that {_TABLE} holds no row. If empty, drop it by hand"
        f" (DROP TABLE {_TABLE}) and upgrade again; if any row is there, stop and handle it as a"
        " data incident: do not drop it"
    )


def _refuse_triggers(names: list[str]) -> NoReturn:
    raise RuntimeError(
        "Cannot upgrade to 0035: "
        + "; ".join(
            f"trigger {name} exists but is not the one 0035 builds (table {_TABLE},"
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
        sa.Column("tenant_id", sa.BigInteger(), nullable=False),
        sa.Column("statement_number", sa.String(length=32), nullable=False),
        sa.Column("period_start", sa.DateTime(), nullable=False),
        sa.Column("period_end", sa.DateTime(), nullable=False),
        sa.Column("cutoff_at", sa.DateTime(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("ledger_high_sequence", sa.BigInteger(), nullable=False),
        sa.Column("snapshot_json", sa.JSON(), nullable=False),
        sa.Column("generated_by", sa.BigInteger(), nullable=True),
        sa.Column("tax_policy_version_id", sa.BigInteger(), nullable=False),
        sa.Column("finalized_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        *(
            sa.UniqueConstraint(*columns, name=name)
            for name, (columns, unique) in _INDEXES.items()
            if unique
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
    for name in _PLAIN_INDEXES:
        columns, _ = _INDEXES[name]
        op.create_index(name, _TABLE, list(columns), unique=False)


def _upgrade_offline() -> None:
    # 离线读不了库：按「什么都没做过」输出（照 0030）。触发器只在 MySQL 上有。
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
    # ⚠️ 先判、再发 DDL：任何一项不对都不动库，版本也不前进。
    # 第 1 步。
    existing = _reflected_shape()
    if existing is not None:
        problems = _shape_problems(existing)
        if problems:
            _refuse_shape(problems)
    # 第 2 步。表不存在时，同名触发器只可能挂在别的表上：同样拒绝。
    triggers = _triggers(bind) if mysql else {}
    wrong = [name for name, row in triggers.items() if not _trigger_matches(name, row)]
    if wrong:
        _refuse_triggers(sorted(wrong, key=list(_TRIGGER_EVENTS).index))
    missing = [name for name in _TRIGGER_EVENTS if name not in triggers] if mysql else []
    # 第 3 步：已完成。
    if existing is not None and not missing:
        return

    # 第 4 步。⚠️ 必须排在任何 DDL 之前。
    if mysql:
        _require_trigger_privilege(bind)
    # 第 5 步：一条原子 CREATE TABLE。
    if existing is None:
        _create_table()
    # 第 6 步：只建缺的。
    for name in missing:
        op.execute(_TRIGGERS[name])


def downgrade() -> None:
    # ⚠️ 离线模式判断不了表是否为空：拒绝，不输出任何 DROP（设计 §2「数据库」）。
    if op.get_context().as_sql:
        raise RuntimeError(
            f"Cannot downgrade 0035 in --sql offline mode: whether table {_TABLE} is empty can"
            " only be checked against the database. Run the downgrade online"
        )
    bind = op.get_bind()
    if not sa.inspect(bind).has_table(_TABLE):
        return
    # ⚠️ 在任何 DDL 之前：有对账单就拒绝，删表等于删掉已定稿的财务快照。
    if bind.execute(sa.text(_HAS_ROWS)).first() is not None:
        raise RuntimeError(
            f"Cannot downgrade 0035: table {_TABLE} is not empty (finalized monthly statements"
            " are immutable financial snapshots). No DDL was issued and no row was deleted."
            f" Keep the table {_TABLE} and roll back only the application image"
        )
    if _is_mysql():
        # 先删触发器、再删表；只删在这张表上、确实存在的（补完中断的升级之后也能降级）。
        present = {name for name, row in _triggers(bind).items() if row[0] == _TABLE}
        for name in _TRIGGER_EVENTS:
            if name in present:
                op.execute(f"DROP TRIGGER {name}")
    op.drop_table(_TABLE)

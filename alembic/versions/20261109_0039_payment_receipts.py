"""payment receipts: the receipt number series with its 74 rows, the receipts table and its two
immutability triggers

照设计闸门 #589 v2（docs/design/AIH-TASK-196-payment-receipts.md）§2「数据库」「编号」、§3 INV-3、
INV-6、INV-10、§6「数据保留」与 §8；本迁移是 AIH-TASK-196，建立在 0036 / 0037（设计闸门 #580 v3、
#585 v3）与 0038（#587 v3）之上。**只建两张新表、插入系列行、建两个触发器**，不改任何已有表的列、
索引、约束与触发器，不读写任何已有表的行：

- `receipt_number_series`（编号计数器，每个马来西亚日历年一行）：`series_year SMALLINT` 主键
  （**不自增**，MySQL 上没有 `AUTO_INCREMENT`）、`last_sequence INT NOT NULL DEFAULT 0`；两条 CHECK
  `ck_receipt_number_series_year`（`BETWEEN 2026 AND 2099`）与 `ck_receipt_number_series_sequence`
  （`BETWEEN 0 AND 999999`）。插入 2026 至 2099 每年一行（74 行，`last_sequence = 0`）。
- `payment_receipts`（12 列，每笔 `PAID` 支付至多一张收据的不可变快照）：
  - 唯一 `uq_payment_receipts_public_id`、`uq_payment_receipts_payment_id`（每笔支付至多一张，也是
    `payment_id` 外键用的索引）、`uq_payment_receipts_receipt_number`、
    `uq_payment_receipts_series_sequence (series_year, sequence)`（同一年份内序号不重复）；
  - `ix_payment_receipts_tenant_issued (tenant_id, issued_at, id)`（客户列表，同时是 `tenant_id`
    外键用的索引）与 `ix_payment_receipts_tax_policy_version_id (tax_policy_version_id)`：**显式
    声明**，供外键使用，所以 MySQL 不另建与外键同名的隐式索引（与 0035 同一理由）；
  - 三个外键 `fk_payment_receipts_payment_id` → `payments.id`、
    `fk_payment_receipts_tenant_id` → `tenants.id`、
    `fk_payment_receipts_tax_policy_version_id` → `tax_policy_versions.id`，都是
    `ON DELETE RESTRICT`；
  - 四条 CHECK：`series_year`（2026–2099）、`sequence`（1–999999）、`amount`（大于 0 且只有两位
    小数，INV-10）、`currency`（`MYR`）。
- MySQL 上两个触发器 `trg_payment_receipts_before_update`（BEFORE UPDATE）与
  `trg_payment_receipts_before_delete`（BEFORE DELETE）：**无条件** SIGNAL 45000
  （`payment_receipts rows are immutable`），收据行不可改、不可删（§6「数据保留」）。SQLite 不建
  触发器（与 0035 同一惯例）。

没有写入方：系列行与收据行只由 AIH-TASK-197 的 `issue_receipt` 写（系列行 `last_sequence + 1`、
收据只 INSERT）。`app/models/receipts.py` 的 `ReceiptNumberSeries` 与 `PaymentReceipt` 同步声明了
这些列、索引与约束（同名同序），`tests/backend/test_payment_receipts_migration.py` 比对两边一致。

步骤
----

1. 用 inspector 判两张表。存在的表逐项比「形状」（设计 §2「数据库」）：恰好设计的列（名称、类型与
   宽度、可空性；**不比服务端默认值**）、主键列、索引（名称、列与列顺序、唯一与否，没有前缀长度）、
   外键（列、引用表与列、`ON DELETE RESTRICT`，MySQL 上的删除规则读
   `information_schema.REFERENTIAL_CONSTRAINTS`）、CHECK（名称）；`receipt_number_series` 另核对
   `series_year` **没有** `AUTO_INCREMENT`（MySQL 上读 `information_schema.COLUMNS.EXTRA`）。任何
   一项缺失、多出或不同都算形状不对 → 抛错，点名表，**不发任何 DDL、不写任何行**。
2. MySQL：读 `information_schema.TRIGGERS` 里这两个名字的触发器，逐个比 `EVENT_OBJECT_TABLE`、
   `ACTION_TIMING`、`EVENT_MANIPULATION` 与 `ACTION_STATEMENT`（去掉首尾空白后与 `_TRIGGER_BODY`
   逐字相等）。存在但任一项不一致 → 抛错，**不发任何 DDL**。
3. 系列行（设计 §2「数据库」）。MySQL 的 `CREATE TABLE` 与插入系列行不在同一个事务里，所以先看
   `payment_receipts`：
   - `payment_receipts` 不存在或为空：系列表不存在或 0 行 → 要插入 74 行（补完在建表与插入之间
     中断的升级）；恰好 74 行、年份恰好 2026–2099、`last_sequence` 都在 0–999999 → 不动；其他 →
     抛错。
   - `payment_receipts` 有行：系列表不存在或 0 行 → 抛错，不建表、不补插（补插会从 0 起编号，与
     已开具的序号相撞）；恰好 74 行时除上面的条件外，每个年份的 `last_sequence` 还要不小于
     `payment_receipts` 里该年份的 `MAX(sequence)`，否则抛错。
   抛错时都**不发 DDL、不写行**。
4. 两张表都在、系列行不缺、两个触发器都在且一致 → 不发 DDL，只把版本前进。
5. **预检**（MySQL，只在要发 DDL 时；**只缺系列行、不需要任何 DDL 时不跑**）：查 `@@log_bin` 与
   `@@log_bin_trust_function_creators`（查询、判定与报错文案照 0035）。binlog 开着而开关关着时，
   应用账号建不了触发器（ERROR 1419），所以在第一条 DDL 之前抛错。它排在本次升级的任何写入之前：
   系列表已在而为空、`payment_receipts` 要建时，它也在补插系列行之前。
6. 缺什么建什么，按这个顺序：`receipt_number_series`（MySQL 上**一条** `CREATE TABLE`，主键与
   CHECK 都在里面）→ 插入 74 行（**一条** `INSERT`）→ `payment_receipts`（MySQL 上**一条**
   `CREATE TABLE`，索引、外键与 CHECK 都在里面）→ 缺哪个触发器建哪个（先 BEFORE UPDATE 后 BEFORE
   DELETE），已在的不动。其他方言：`create_table` 加 `create_index`，不建触发器。

`--sql` 离线模式读不了库：照 0035 的 `_upgrade_offline`，跳过预检与全部检查，按「什么都没做过」
输出两条 `CREATE TABLE`、插入系列行的 `INSERT` 与两条 `CREATE TRIGGER`。

§132 第 13 条分析
-----------------

**已有数据**：只建两张新表、插入新系列表的 74 行、建 `payment_receipts` 上的两个触发器；不改任何
已有表的列、索引、约束与触发器，不回填，不读写任何已有行；钱包与账本不被碰（INV-4、INV-5），支付、
网关事件与支付设置也不被碰。外键只指向 `payments`、`tenants` 与 `tax_policy_versions`。

**锁表与停机**：建新表不锁任何已有表的行；建三个外键时 MySQL 对 `payments`、`tenants` 与
`tax_policy_versions` 各取短暂的元数据锁（等正在进行的、碰过它们的事务结束），不扫行。插入系列行
只碰新表；建触发器只锁 `payment_receipts`（空表，没有读写方）。不需要停机。

**备份**：要求前置备份（与 0005–0038 同一口径）。本版只写新系列表的 74 行，不写任何已有表。

**部署顺序**：`deploy.sh` 先迁移、再换镜像（设计 §8：A、B、C 全部合并之后）。本任务不带写入方与
接口，部署后只多两张没人读写的表（系列表的 74 行都是 0）；旧代码不知道它们。先迁移后换镜像、或
反过来都安全。之后依次是 AIH-TASK-197（开具）、198（邮件）、199（PDF）、200（入账时开具）、
201（补发扫描）。

**失败处理**：⚠️ MySQL 的 DDL **不参与事务**：每条 `CREATE TABLE`、`CREATE TRIGGER` 都隐式提交，
插入系列行的 `INSERT` 由它之后的那条 DDL 隐式提交（全部建好时与写 `alembic_version` 一起提交）。
每张表连同索引、外键与 CHECK 在**一条** `CREATE TABLE` 里，MySQL 8 的 InnoDB DDL 是原子的，加上
第 1–3 步的检查，每个窗口都有确定的结局：

- 预检失败：什么都没改，`alembic_version` 仍是 `0038_payment_settings`。给账号打开
  `log_bin_trust_function_creators` 后重新部署。
- 建 `receipt_number_series` 失败（锁等待超时、进程被杀）→ 整条回滚，两张表都不存在，版本仍是
  0038 → 原样重新部署。
- 系列表建成、`INSERT` 提交之前中断（`INSERT` 本身失败、或建 `payment_receipts` 之前进程退出）→
  系列表在、0 行，`payment_receipts` 不在，版本仍是 0038 → 原样重新部署：第 3 步判为「缺系列行」，
  预检之后插入 74 行，再建 `payment_receipts` 与两个触发器。
- 建 `payment_receipts` 失败 → 这条 DDL 开始前已隐式提交了 74 行，整条 `CREATE TABLE` 回滚；系列
  表在且 74 行，`payment_receipts` 不在 → 原样重新部署：只建 `payment_receipts` 与两个触发器。
- `payment_receipts` 建成、某个触发器建成之前中断 → 两张表在、缺一个或两个触发器，版本仍是 0038
  → 原样重新部署：只建缺的那个（或那两个）。这期间没有写入方（197 上线之前），缺触发器不会让收据
  被改。
- 全部建成、写 `alembic_version` 之前进程退出 → 原样重新部署：判为已完成，不发 DDL，只把版本前进。
- 只缺系列行（例如有人手工清空了系列表、`payment_receipts` 为空）→ 重跑只发那一条 `INSERT`，不跑
  预检、不发 DDL。
- 有人手工建过同名但形状不同的表 → 重跑在第 1 步失败，报错点名那张表，不发 DDL、不写行，版本仍是
  0038 → 先确认 `payment_receipts` 里没有任何行：为空则 `DROP TABLE` 那张表（`payment_receipts`
  的触发器随表删除）再重新部署；有行则停下，按数据事故处理，不要删。
- 同名触发器存在但时机、事件、所在表或正文不同 → 重跑在第 2 步失败，报错点名触发器，不发 DDL，
  版本仍是 0038 → 人对照设计查看；确认是手工建的之后 `DROP TRIGGER` 它，再重新部署（只补建缺的
  那个）。
- 系列行不对（行数不是 74、年份不是恰好 2026–2099、`last_sequence` 越界；或 `payment_receipts`
  有行而系列表不存在、为空、或某年的 `last_sequence` 小于已开具的最大序号）→ 重跑在第 3 步失败，
  报错点名系列表，不发 DDL、不写行，版本仍是 0038 → 这只可能来自手工改库或改 `alembic_version`
  （收据只在 0039 到 head 之后由新代码写，降级碰到收据就拒绝），停下按数据事故处理：**不要补插
  或把计数改小**，否则编号会复用。

**回滚**：生产上的回滚**只回滚应用镜像，不降级 0039**（设计 §8）。`downgrade` **只删空表**：
`--sql` 离线模式判断不了 `payment_receipts` 是否为空，直接抛错（须在线执行），不输出任何 `DROP`；
先 `SELECT 1 FROM payment_receipts LIMIT 1`，有任何一行就抛错（消息点名这张表、处理方法是保留表
只回滚应用镜像），不发任何 DDL、不删任何行 —— 收据是不可变的财务快照，删了编号就会复用；为空时
MySQL 上先删在这张表上的那两个触发器（只删存在的），再删 `payment_receipts`、再删
`receipt_number_series`。每张表只在存在时删，**不照 0035「表不在就什么都不做」**：
`payment_receipts` 已不存在时照样删 `receipt_number_series`（补完在两次 `DROP` 之间中断的降级），
两张都不在时不发 DDL。

Revision ID: 0039_payment_receipts
Revises: 0038_payment_settings
Create Date: 2026-11-09
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import NoReturn

import sqlalchemy as sa

from alembic import op

revision: str = "0039_payment_receipts"
down_revision: str | None = "0038_payment_settings"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SERIES = "receipt_number_series"
_RECEIPTS = "payment_receipts"
_PAYMENTS = "payments"
_TENANTS = "tenants"
_TAX_POLICY_VERSIONS = "tax_policy_versions"
# 建表顺序；降级按相反顺序删。
_TABLES = (_SERIES, _RECEIPTS)

# ⚠️ 下面都是冻结的字面量；模型那边的声明由
# tests/backend/test_payment_receipts_migration.py 比对。

# 系列行：2026 至 2099 每年一行（74 行），序号 1–999999（设计 §2「数据库」「编号」）。
_FIRST_YEAR = 2026
_LAST_YEAR = 2099
_MAX_SEQUENCE = 999999
_SERIES_YEARS: tuple[int, ...] = tuple(range(_FIRST_YEAR, _LAST_YEAR + 1))

# 列名 → (类型与宽度, 可空)，按建表顺序。类型写成 MySQL 反射出来的样子（去掉空白）。
_SERIES_COLUMNS: dict[str, tuple[str, bool]] = {
    "series_year": ("SMALLINT", False),
    "last_sequence": ("INTEGER", False),
}
_RECEIPT_COLUMNS: dict[str, tuple[str, bool]] = {
    "id": ("BIGINT", False),
    "public_id": ("CHAR(36)", False),
    "payment_id": ("BIGINT", False),
    "tenant_id": ("BIGINT", False),
    "receipt_number": ("VARCHAR(32)", False),
    "series_year": ("SMALLINT", False),
    "sequence": ("INTEGER", False),
    "amount": ("DECIMAL(20,8)", False),
    "currency": ("CHAR(3)", False),
    "tax_policy_version_id": ("BIGINT", False),
    "snapshot_json": ("JSON", False),
    "issued_at": ("DATETIME", False),
}

# 主键列。系列表的主键不自增（第 1 步另核对）。
_SERIES_YEAR = "series_year"
_PRIMARY_KEYS: dict[str, tuple[str, ...]] = {
    _SERIES: (_SERIES_YEAR,),
    _RECEIPTS: ("id",),
}

_UNIQUE_PUBLIC_ID = "uq_payment_receipts_public_id"
_UNIQUE_PAYMENT_ID = "uq_payment_receipts_payment_id"
_UNIQUE_RECEIPT_NUMBER = "uq_payment_receipts_receipt_number"
_UNIQUE_SERIES_SEQUENCE = "uq_payment_receipts_series_sequence"
_INDEX_TENANT_ISSUED = "ix_payment_receipts_tenant_issued"
_INDEX_TAX_POLICY_VERSION = "ix_payment_receipts_tax_policy_version_id"
# 索引名 → (列, 唯一)，按建的顺序。系列表只有主键。
_SERIES_INDEXES: dict[str, tuple[tuple[str, ...], bool]] = {}
_RECEIPT_INDEXES: dict[str, tuple[tuple[str, ...], bool]] = {
    _UNIQUE_PUBLIC_ID: (("public_id",), True),
    _UNIQUE_PAYMENT_ID: (("payment_id",), True),
    _UNIQUE_RECEIPT_NUMBER: (("receipt_number",), True),
    _UNIQUE_SERIES_SEQUENCE: (("series_year", "sequence"), True),
    _INDEX_TENANT_ISSUED: (("tenant_id", "issued_at", "id"), False),
    _INDEX_TAX_POLICY_VERSION: (("tax_policy_version_id",), False),
}
# 普通索引：其他方言上 `create_table` 之后逐个 `create_index`。
_PLAIN_INDEXES = (_INDEX_TENANT_ISSUED, _INDEX_TAX_POLICY_VERSION)

_FK_PAYMENT = "fk_payment_receipts_payment_id"
_FK_TENANT = "fk_payment_receipts_tenant_id"
_FK_TAX_POLICY_VERSION = "fk_payment_receipts_tax_policy_version_id"
# 外键名 → (列, 引用表, 引用列, 删除规则)。
_SERIES_FOREIGN_KEYS: dict[str, tuple[tuple[str, ...], str, tuple[str, ...], str]] = {}
_RECEIPT_FOREIGN_KEYS: dict[str, tuple[tuple[str, ...], str, tuple[str, ...], str]] = {
    _FK_PAYMENT: (("payment_id",), _PAYMENTS, ("id",), "RESTRICT"),
    _FK_TENANT: (("tenant_id",), _TENANTS, ("id",), "RESTRICT"),
    _FK_TAX_POLICY_VERSION: (("tax_policy_version_id",), _TAX_POLICY_VERSIONS, ("id",), "RESTRICT"),
}

# 名字 → 条件。与 app/models/receipts.py 上的 CheckConstraint 一致（空白除外）。
_SERIES_CHECKS: dict[str, str] = {
    "ck_receipt_number_series_year": "series_year BETWEEN 2026 AND 2099",
    "ck_receipt_number_series_sequence": "last_sequence BETWEEN 0 AND 999999",
}
_RECEIPT_CHECKS: dict[str, str] = {
    "ck_payment_receipts_series_year": "series_year BETWEEN 2026 AND 2099",
    "ck_payment_receipts_sequence": "sequence BETWEEN 1 AND 999999",
    "ck_payment_receipts_amount": "amount > 0 AND amount = ROUND(amount, 2)",
    "ck_payment_receipts_currency": "currency = 'MYR'",
}

# 表名 → 设计的形状（第 1 步逐项比）。
_SHAPES: dict[str, dict[str, object]] = {
    _SERIES: {
        "columns": _SERIES_COLUMNS,
        "primary_key": _PRIMARY_KEYS[_SERIES],
        "indexes": _SERIES_INDEXES,
        "foreign_keys": _SERIES_FOREIGN_KEYS,
        "checks": set(_SERIES_CHECKS),
    },
    _RECEIPTS: {
        "columns": _RECEIPT_COLUMNS,
        "primary_key": _PRIMARY_KEYS[_RECEIPTS],
        "indexes": _RECEIPT_INDEXES,
        "foreign_keys": _RECEIPT_FOREIGN_KEYS,
        "checks": set(_RECEIPT_CHECKS),
    },
}


def _constraints_ddl(
    indexes: dict[str, tuple[tuple[str, ...], bool]],
    foreign_keys: dict[str, tuple[tuple[str, ...], str, tuple[str, ...], str]],
    checks: dict[str, str],
) -> list[str]:
    uniques = [
        f"    CONSTRAINT {name} UNIQUE ({', '.join(columns)})"
        for name, (columns, unique) in indexes.items()
        if unique
    ]
    plain = [
        f"    INDEX {name} ({', '.join(columns)})"
        for name, (columns, unique) in indexes.items()
        if not unique
    ]
    keys = [
        f"    CONSTRAINT {name} FOREIGN KEY ({', '.join(columns)})"
        f" REFERENCES {referred} ({', '.join(referred_columns)}) ON DELETE {rule}"
        for name, (columns, referred, referred_columns, rule) in foreign_keys.items()
    ]
    conditions = [
        f"    CONSTRAINT {name} CHECK ({condition})" for name, condition in checks.items()
    ]
    return [*uniques, *plain, *keys, *conditions]


# MySQL 上的建表语句：每张表一条，原子。`series_year` 没有 AUTO_INCREMENT。
_CREATE_SERIES_MYSQL = (
    f"CREATE TABLE {_SERIES} (\n"
    + ",\n".join(
        [
            "    series_year SMALLINT NOT NULL",
            "    last_sequence INT NOT NULL DEFAULT 0",
            "    PRIMARY KEY (series_year)",
            *_constraints_ddl(_SERIES_INDEXES, _SERIES_FOREIGN_KEYS, _SERIES_CHECKS),
        ]
    )
    + "\n)"
)
_CREATE_RECEIPTS_MYSQL = (
    f"CREATE TABLE {_RECEIPTS} (\n"
    + ",\n".join(
        [
            "    id BIGINT NOT NULL AUTO_INCREMENT",
            "    public_id CHAR(36) NOT NULL",
            "    payment_id BIGINT NOT NULL",
            "    tenant_id BIGINT NOT NULL",
            "    receipt_number VARCHAR(32) NOT NULL",
            "    series_year SMALLINT NOT NULL",
            "    sequence INT NOT NULL",
            "    amount DECIMAL(20, 8) NOT NULL",
            "    currency CHAR(3) NOT NULL",
            "    tax_policy_version_id BIGINT NOT NULL",
            "    snapshot_json JSON NOT NULL",
            "    issued_at DATETIME NOT NULL",
            "    PRIMARY KEY (id)",
            *_constraints_ddl(_RECEIPT_INDEXES, _RECEIPT_FOREIGN_KEYS, _RECEIPT_CHECKS),
        ]
    )
    + "\n)"
)

# 系列行：**一条** INSERT，74 行，`last_sequence = 0`。各方言通用的写法。
_INSERT_SERIES = f"INSERT INTO {_SERIES} (series_year, last_sequence) VALUES " + ", ".join(
    f"({year}, 0)" for year in _SERIES_YEARS
)

# 两个触发器共用的主体（设计 §2「数据库」）：无条件拒绝。MySQL 把 `FOR EACH ROW` 之后的部分原样
# 存进 ACTION_STATEMENT，第 2 步去掉首尾空白后与它逐字比较。
_TRIGGER_BODY = """
BEGIN
    SIGNAL SQLSTATE '45000'
        SET MESSAGE_TEXT = 'payment_receipts rows are immutable';
END
""".strip()

_UPDATE_TRIGGER = "trg_payment_receipts_before_update"
_DELETE_TRIGGER = "trg_payment_receipts_before_delete"
# 触发器名 → (时机, 事件)，按建的顺序。
_TRIGGER_EVENTS: dict[str, tuple[str, str]] = {
    _UPDATE_TRIGGER: ("BEFORE", "UPDATE"),
    _DELETE_TRIGGER: ("BEFORE", "DELETE"),
}


def _create_trigger(name: str) -> str:
    """整条 CREATE TRIGGER 是**一条**语句（与 0035 同一写法）。"""
    timing, event = _TRIGGER_EVENTS[name]
    return f"""
CREATE TRIGGER {name}
{timing} {event} ON {_RECEIPTS}
FOR EACH ROW
{_TRIGGER_BODY}
"""


_TRIGGERS: dict[str, str] = {name: _create_trigger(name) for name in _TRIGGER_EVENTS}

_PREREQUISITE_QUERY = "SELECT @@log_bin, @@log_bin_trust_function_creators"
_PREREQUISITE_ERROR = (
    "MySQL has binary logging on and log_bin_trust_function_creators OFF, "
    + "so this account cannot create the payment receipt triggers (ERROR 1419). "
    + "Set log_bin_trust_function_creators = ON first; nothing has been changed."
)


def _delete_rules_query(table: str) -> str:
    # MySQL 上外键的删除规则以 information_schema 为准（与 0035 同一个来源）：反射不返回缺省的
    # RESTRICT。
    return (
        "SELECT CONSTRAINT_NAME, DELETE_RULE FROM information_schema.REFERENTIAL_CONSTRAINTS"
        f" WHERE CONSTRAINT_SCHEMA = DATABASE() AND TABLE_NAME = '{table}'"
    )


# MySQL 上 AUTO_INCREMENT 以 information_schema 为准（设计 §2：形状检查核对 `series_year`）。
_AUTO_INCREMENT_MYSQL = (
    "SELECT COLUMN_NAME FROM information_schema.COLUMNS"
    f" WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = '{_SERIES}'"
    " AND EXTRA LIKE '%auto_increment%'"
)
# 按名字找：同名触发器挂在别的表上也要看见（触发器名在库内唯一）。
_TRIGGERS_QUERY = (
    "SELECT TRIGGER_NAME, EVENT_OBJECT_TABLE, ACTION_TIMING, EVENT_MANIPULATION, ACTION_STATEMENT"
    " FROM information_schema.TRIGGERS WHERE TRIGGER_SCHEMA = DATABASE()"
    f" AND TRIGGER_NAME IN ('{_UPDATE_TRIGGER}', '{_DELETE_TRIGGER}')"
)

# 第 3 步与降级前的数据检查（设计 §2、§8）。
_HAS_RECEIPTS = f"SELECT 1 FROM {_RECEIPTS} LIMIT 1"
_SERIES_ROWS = f"SELECT series_year, last_sequence FROM {_SERIES}"
_MAX_SEQUENCES = f"SELECT series_year, MAX(sequence) FROM {_RECEIPTS} GROUP BY series_year"

_DESIGN = "docs/design/AIH-TASK-196-payment-receipts.md"


def _is_mysql() -> bool:
    return op.get_context().dialect.name == "mysql"


def _type_text(column_type: object) -> str:
    """`VARCHAR(32) COLLATE …` → `VARCHAR(32)`：只留类型与宽度。"""
    return str(column_type).replace(", ", ",").split(" ")[0].upper()


def _auto_increment_columns(bind: sa.engine.Connection, columns: list[dict]) -> set[str]:
    """系列表上带 AUTO_INCREMENT 的列名。"""
    if _is_mysql():
        return {str(name) for (name,) in bind.execute(sa.text(_AUTO_INCREMENT_MYSQL)).all()}
    return {str(column["name"]) for column in columns if column.get("autoincrement") is True}


def _reflected_shape(table: str) -> dict | None:
    """一张表的现状（列、主键、索引、外键、CHECK 名；系列表另有 AUTO_INCREMENT 列）；表不存在时
    返回 None。"""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table(table):
        return None

    reflected_columns = inspector.get_columns(table)
    columns = {
        str(column["name"]): (_type_text(column["type"]), bool(column["nullable"]))
        for column in reflected_columns
    }
    primary_key = tuple(inspector.get_pk_constraint(table).get("constrained_columns") or ())
    # 前缀长度只在 `dialect_options` 的 `mysql_length` 里，`column_names`
    # 看不出来：带前缀的记成另一种形状。唯一约束在 MySQL 上就是唯一索引；
    # SQLite 只在 get_unique_constraints 里有。
    indexes: dict[str, tuple[tuple[str, ...], bool] | str] = {}
    for index in inspector.get_indexes(table):
        if (index.get("dialect_options") or {}).get("mysql_length"):
            indexes[str(index["name"])] = "prefix length"
        else:
            indexes[str(index["name"])] = (
                tuple(index["column_names"]),
                bool(index.get("unique")),
            )
    for unique in inspector.get_unique_constraints(table):
        indexes.setdefault(str(unique["name"]), (tuple(unique["column_names"]), True))

    if _is_mysql():
        delete_rules = dict(bind.execute(sa.text(_delete_rules_query(table))).all())
    else:
        delete_rules = {}
    foreign_keys = {}
    for key in inspector.get_foreign_keys(table):
        name = str(key["name"])
        rule = delete_rules.get(name, (key.get("options") or {}).get("ondelete"))
        foreign_keys[name] = (
            tuple(key["constrained_columns"]),
            str(key["referred_table"]),
            tuple(key["referred_columns"]),
            str(rule or "").upper(),
        )

    checks = {str(check["name"]) for check in inspector.get_check_constraints(table)}
    shape: dict[str, object] = {
        "columns": columns,
        "primary_key": primary_key,
        "indexes": indexes,
        "foreign_keys": foreign_keys,
        "checks": checks,
    }
    if table == _SERIES:
        shape["auto_increment"] = _auto_increment_columns(bind, reflected_columns)
    return shape


def _shape_problems(table: str, shape: dict) -> list[str]:
    """与设计不一致的项；空列表 = 形状正确。不比服务端默认值（设计 §2）。"""
    expected = _SHAPES[table]
    problems = []
    if shape["columns"] != expected["columns"]:
        problems.append("columns (name, type and width, nullability)")
    if shape["primary_key"] != expected["primary_key"]:
        problems.append("primary key")
    if shape["indexes"] != expected["indexes"]:
        problems.append("indexes (name, columns and their order, uniqueness)")
    if shape["foreign_keys"] != expected["foreign_keys"]:
        problems.append("foreign keys (columns, referred table, ON DELETE RESTRICT)")
    if shape["checks"] != expected["checks"]:
        problems.append("check constraints")
    if _SERIES_YEAR in shape.get("auto_increment", set()):
        problems.append(f"{_SERIES_YEAR} is AUTO_INCREMENT")
    return problems


def _triggers(bind: sa.engine.Connection) -> dict[str, tuple[str, str, str, str]]:
    """这两个名字的现有触发器：名字 → (所在表, 时机, 事件, ACTION_STATEMENT)。"""
    return {
        str(name): (str(table), str(timing), str(kind), str(statement))
        for name, table, timing, kind, statement in bind.execute(sa.text(_TRIGGERS_QUERY)).all()
    }


def _trigger_matches(name: str, row: tuple[str, str, str, str]) -> bool:
    """在收据表上、时机与事件是设计的，主体与 `_TRIGGER_BODY` 逐字相同（只去掉首尾空白）。"""
    table, timing, kind, body = row
    return (
        table == _RECEIPTS
        and (timing, kind) == _TRIGGER_EVENTS[name]
        and body.strip() == _TRIGGER_BODY
    )


def _require_trigger_privilege(bind: sa.engine.Connection) -> None:
    """Refuse to run where the migration account could not create triggers (as 0035)."""
    log_bin, trusted = bind.execute(sa.text(_PREREQUISITE_QUERY)).one()
    if int(log_bin) == 1 and int(trusted) != 1:
        raise RuntimeError(_PREREQUISITE_ERROR)


def _refuse_shape(table: str, problems: list[str]) -> NoReturn:
    raise RuntimeError(
        f"Cannot upgrade to 0039: table {table} exists but differs from the design in "
        + "; ".join(problems)
        + ". No DDL was issued and no row was written. First check that"
        f" {_RECEIPTS} holds no row. If it is empty, drop the table by hand"
        f" (DROP TABLE {table}) and upgrade again; if any row is there, stop and handle it as a"
        " data incident: do not drop it"
    )


def _refuse_triggers(names: list[str]) -> NoReturn:
    raise RuntimeError(
        "Cannot upgrade to 0039: "
        + "; ".join(
            f"trigger {name} exists but is not the one 0039 builds (table {_RECEIPTS},"
            f" {' '.join(_TRIGGER_EVENTS[name])}, body word for word)"
            for name in names
        )
        + f". No DDL was issued. Have a person compare it with {_DESIGN} §2; once it is known to"
        " be hand-made, DROP TRIGGER it and upgrade again: only the missing trigger is built"
    )


def _refuse_series(problem: str) -> NoReturn:
    raise RuntimeError(
        f"Cannot upgrade to 0039: {problem}. No DDL was issued and no row was written. This can"
        " only come from a hand-made change to the database or to alembic_version: stop and"
        f" handle it as a data incident. Do not insert {_SERIES} rows or lower last_sequence by"
        f" hand, or receipt numbers would be issued twice ({_DESIGN} §2)"
    )


def _series_rows_missing(
    bind: sa.engine.Connection, series_present: bool, receipts_present: bool
) -> bool:
    """第 3 步：True = 要插入 74 行；系列行不对时抛错（不发 DDL、不写行）。"""
    has_receipts = receipts_present and bind.execute(sa.text(_HAS_RECEIPTS)).first() is not None
    rows = (
        [(int(year), int(last)) for year, last in bind.execute(sa.text(_SERIES_ROWS)).all()]
        if series_present
        else []
    )
    if not rows:
        if has_receipts:
            state = "has no row" if series_present else "does not exist"
            _refuse_series(
                f"table {_SERIES} {state} while {_RECEIPTS} holds receipts (inserting the rows"
                " would number from 0 again and collide with the receipts issued)"
            )
        return True

    counters = dict(rows)
    if (
        len(rows) != len(_SERIES_YEARS)
        or sorted(counters) != list(_SERIES_YEARS)
        or any(not 0 <= last <= _MAX_SEQUENCE for last in counters.values())
    ):
        _refuse_series(
            f"table {_SERIES} does not hold exactly one row for each year {_FIRST_YEAR}–"
            f"{_LAST_YEAR} ({len(_SERIES_YEARS)} rows) with last_sequence 0–{_MAX_SEQUENCE}"
        )
    if has_receipts:
        behind = sorted(
            int(year)
            for year, top in bind.execute(sa.text(_MAX_SEQUENCES)).all()
            if counters.get(int(year), -1) < int(top)
        )
        if behind:
            _refuse_series(
                f"last_sequence in table {_SERIES} is below MAX(sequence) in {_RECEIPTS} for "
                + ", ".join(str(year) for year in behind)
            )
    return False


def _create_series() -> None:
    if _is_mysql():
        op.execute(_CREATE_SERIES_MYSQL)
        return
    op.create_table(
        _SERIES,
        sa.Column("series_year", sa.SmallInteger(), autoincrement=False, nullable=False),
        sa.Column("last_sequence", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.PrimaryKeyConstraint("series_year"),
        *(sa.CheckConstraint(condition, name=name) for name, condition in _SERIES_CHECKS.items()),
    )


def _create_receipts() -> None:
    if _is_mysql():
        op.execute(_CREATE_RECEIPTS_MYSQL)
        return
    op.create_table(
        _RECEIPTS,
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("public_id", sa.CHAR(length=36), nullable=False),
        sa.Column("payment_id", sa.BigInteger(), nullable=False),
        sa.Column("tenant_id", sa.BigInteger(), nullable=False),
        sa.Column("receipt_number", sa.String(length=32), nullable=False),
        sa.Column("series_year", sa.SmallInteger(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("amount", sa.Numeric(precision=20, scale=8), nullable=False),
        sa.Column("currency", sa.CHAR(length=3), nullable=False),
        sa.Column("tax_policy_version_id", sa.BigInteger(), nullable=False),
        sa.Column("snapshot_json", sa.JSON(), nullable=False),
        sa.Column("issued_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        *(
            sa.UniqueConstraint(*columns, name=name)
            for name, (columns, unique) in _RECEIPT_INDEXES.items()
            if unique
        ),
        *(
            sa.ForeignKeyConstraint(
                list(columns),
                [f"{referred}.{column}" for column in referred_columns],
                name=name,
                ondelete=rule,
            )
            for name, (columns, referred, referred_columns, rule) in _RECEIPT_FOREIGN_KEYS.items()
        ),
        *(sa.CheckConstraint(condition, name=name) for name, condition in _RECEIPT_CHECKS.items()),
    )
    for name in _PLAIN_INDEXES:
        columns, _ = _RECEIPT_INDEXES[name]
        op.create_index(name, _RECEIPTS, list(columns), unique=False)


def _upgrade_offline() -> None:
    # 离线读不了库：按「什么都没做过」输出（照 0035）。触发器只在 MySQL 上有。
    _create_series()
    op.execute(_INSERT_SERIES)
    _create_receipts()
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
    shapes = {table: _reflected_shape(table) for table in _TABLES}
    for table, shape in shapes.items():
        if shape is not None:
            problems = _shape_problems(table, shape)
            if problems:
                _refuse_shape(table, problems)
    # 第 2 步。收据表不存在时，同名触发器只可能挂在别的表上：同样拒绝。
    triggers = _triggers(bind) if mysql else {}
    wrong = [name for name, row in triggers.items() if not _trigger_matches(name, row)]
    if wrong:
        _refuse_triggers(sorted(wrong, key=list(_TRIGGER_EVENTS).index))
    missing = [name for name in _TRIGGER_EVENTS if name not in triggers] if mysql else []
    # 第 3 步。
    create_series = shapes[_SERIES] is None
    create_receipts = shapes[_RECEIPTS] is None
    insert_series = _series_rows_missing(bind, not create_series, not create_receipts)
    # 第 4 步：已完成。
    needs_ddl = create_series or create_receipts or bool(missing)
    if not needs_ddl and not insert_series:
        return

    # 第 5 步。⚠️ 只在要发 DDL 时，且排在本次的任何写入之前。
    if mysql and needs_ddl:
        _require_trigger_privilege(bind)
    # 第 6 步：缺什么建什么。
    if create_series:
        _create_series()
    if insert_series:
        op.execute(_INSERT_SERIES)
    if create_receipts:
        _create_receipts()
    for name in missing:
        op.execute(_TRIGGERS[name])


def downgrade() -> None:
    # ⚠️ 离线模式判断不了收据表是否为空：拒绝，不输出任何 DROP（设计 §8）。
    if op.get_context().as_sql:
        raise RuntimeError(
            f"Cannot downgrade 0039 in --sql offline mode: whether table {_RECEIPTS} is empty can"
            " only be checked against the database. Run the downgrade online"
        )
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    receipts_present = inspector.has_table(_RECEIPTS)
    series_present = inspector.has_table(_SERIES)
    # ⚠️ 在任何 DDL 之前：有收据就拒绝，删表等于删掉不可变的财务快照，编号也会复用。
    if receipts_present and bind.execute(sa.text(_HAS_RECEIPTS)).first() is not None:
        raise RuntimeError(
            f"Cannot downgrade 0039: table {_RECEIPTS} is not empty (payment receipts are"
            " immutable financial snapshots and their numbers must never be reused). No DDL was"
            f" issued and no row was deleted. Keep the tables {_RECEIPTS} and {_SERIES} and roll"
            " back only the application image"
        )
    if receipts_present:
        if _is_mysql():
            # 先删触发器、再删表；只删在这张表上、确实存在的（补完中断的升级之后也能降级）。
            present = {name for name, row in _triggers(bind).items() if row[0] == _RECEIPTS}
            for name in _TRIGGER_EVENTS:
                if name in present:
                    op.execute(f"DROP TRIGGER {name}")
        op.drop_table(_RECEIPTS)
    # 收据表已不在时照样删系列表：补完在两次 DROP 之间中断的降级（设计 §8）。
    if series_present:
        op.drop_table(_SERIES)

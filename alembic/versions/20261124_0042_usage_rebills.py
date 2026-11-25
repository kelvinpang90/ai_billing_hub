"""usage rebills: the insert-only record of each rebill, its per-event lines, and the four
immutability triggers

照设计闸门 #830 v12（docs/design/AIH-TASK-253-usage-rebill.md）§2「数据库（迁移 0042，
AIH-TASK-253）」、§3 INV-5、INV-6、INV-10、§7「只追加」「降级保护」两行与 §8。本迁移是
AIH-TASK-253。**只建两张新表与四个触发器**，不改任何已有表的列、索引、约束与触发器，不回填，
不读写任何已有表的行：

- `usage_rebills`（15 列，一次 Rebill 一行，行**只插入**）：
  - 唯一 `uq_usage_rebills_public_id`、`uq_usage_rebills_idempotency_key`、
    `uq_usage_rebills_wallet_transaction_id`（一次 Rebill 恰有一行账本；也是该外键用的索引）；
  - 索引 `ix_usage_rebills_tenant (tenant_id, id)`（按客户列 Rebill；也是 `tenant_id` 外键用的
    索引）；`ix_usage_rebills_corrective_pricing_rule_id`、
    `ix_usage_rebills_corrective_provider_price_version_id`、`ix_usage_rebills_created_by`：
    **显式声明**，供外键使用，所以 MySQL 不另建与外键同名的隐式索引（与 0039、0041 同一理由）；
  - 外键 `fk_usage_rebills_<列>` → `tenants`、`pricing_rules`、`provider_price_versions`、
    `wallet_transactions`、`users`，都是 `ON DELETE RESTRICT`；
  - 5 条 CHECK：`event_count`（1..1000）、`previous_total`、`corrected_total`（`>= 0`）、
    `difference_nonzero`（`difference <> 0`）、`reason`（把 CHAR(9)、CHAR(10)、CHAR(11)、
    CHAR(12)、CHAR(13) 依次替换为空格后 `TRIM`，不为空串：只由 ASCII 空白组成的原因由数据库拒绝；
    不换行空格等非 ASCII 空白由唯一的写入方的请求校验拒绝，设计 §2）。
- `usage_rebill_lines`（15 列，一次 Rebill 的逐事件明细，行**只插入**）：
  - 唯一 `uq_usage_rebill_lines_rebill_event (rebill_id, usage_event_id)`（也是 `rebill_id`
    外键用的索引）；索引 `ix_usage_rebill_lines_usage_event (usage_event_id, id)`（取一个事件的
    最近明细；也是 `usage_event_id` 外键用的索引）；其余六个外键列各一个显式的
    `ix_usage_rebill_lines_<列>`；
  - 外键 `fk_usage_rebill_lines_<列>` → `usage_rebills`、`usage_events`、`wallet_transactions`、
    `pricing_rules`（两个）、`provider_price_versions`（两个）、`monthly_statements`，都是
    `ON DELETE RESTRICT`；
  - 4 条 CHECK：`previous_charge`、`corrected_charge`、`estimated_cost`（`>= 0`）、
    `difference_nonzero`；
  - `original_period` 是 `CHAR(7)`（`YYYY-MM`），不在数据库里校验月份格式（与 0041 的
    `billing_month` 同一做法）。
- **不建「差额 = 新 − 旧」的等式 CHECK**（设计 v9）：后端测试的 SQLite 把 `DECIMAL` 当浮点比较，
  合法行会被拒；等式由 AIH-TASK-255 的服务计算并写入，服务测试对每条写入路径断言。
- MySQL 上四个触发器 `trg_usage_rebills_before_update`、`trg_usage_rebills_before_delete`、
  `trg_usage_rebill_lines_before_update`、`trg_usage_rebill_lines_before_delete`：**无条件**
  SIGNAL 45000（`<表名> rows are immutable`），行不可改、不可删（INV-5）。SQLite 不建触发器
  （与 0039、0041 同一惯例）。

没有写入方：行只由 AIH-TASK-255 的确认（`confirm_rebill`，只 INSERT）写。
`app/models/usage_rebills.py` 的 `UsageRebill` 与 `UsageRebillLine` 同步声明了这些列、索引与约束
（同名同序），`tests/backend/test_usage_rebills_migration.py` 比对两边一致。

步骤
----

1. 用 inspector 逐张判表（先 `usage_rebills`，后 `usage_rebill_lines`）。存在时逐项比「形状」
   （设计 §2「数据库」）：恰好设计的列（名称、类型与宽度、可空性；**不比服务端默认值**）、主键列、
   索引（名称、列与列顺序、唯一与否，没有前缀长度）、外键（列、引用表与列、`ON DELETE RESTRICT`，
   MySQL 上的删除规则读 `information_schema.REFERENTIAL_CONSTRAINTS`）、CHECK（名称）。任何一项
   缺失、多出或不同都算形状不对 → 抛错，点名表，**不发任何 DDL**。
2. MySQL：读 `information_schema.TRIGGERS` 里这四个名字的触发器，逐个比 `EVENT_OBJECT_TABLE`、
   `ACTION_TIMING`、`EVENT_MANIPULATION` 与 `ACTION_STATEMENT`（去掉首尾空白后与该表的触发器主体
   逐字相等）。存在但任一项不一致 → 抛错，**不发任何 DDL**。
3. 两张表在、四个触发器都在且一致 → 不发 DDL，只把版本前进。
4. **预检**（MySQL，只在要发 DDL 时，紧接在第一条 DDL 之前）：查 `@@log_bin` 与
   `@@log_bin_trust_function_creators`（查询、判定与报错文案照 0041）。binlog 开着而开关关着时，
   应用账号建不了触发器（ERROR 1419），所以在 `CREATE TABLE` 之前抛错。
5. 缺什么建什么，按这个顺序：`usage_rebills`（MySQL 上**一条** `CREATE TABLE`，索引、外键与
   CHECK 都在里面）→ `usage_rebill_lines`（同上；它的外键引用前者，所以在后）→ 缺哪个触发器建哪个
   （`usage_rebills` 的 BEFORE UPDATE、BEFORE DELETE，再 `usage_rebill_lines` 的），已在的不动。
   其他方言：`create_table` 加 `create_index`，不建触发器。

`--sql` 离线模式读不了库：照 0041 的 `_upgrade_offline`，跳过预检与全部检查，按「什么都没做过」
输出两条 `CREATE TABLE` 与四条 `CREATE TRIGGER`。

§132 第 13 条分析
-----------------

**已有数据**：只建两张新表与它们上面的四个触发器；不改任何已有表的列、索引、约束与触发器，不回填，
不读写任何已有行。用量事件与它的版本快照（INV-6）、钱包与账本（INV-4、INV-5）、对账单（INV-12）
都不被碰。外键只指向 `tenants`、`pricing_rules`、`provider_price_versions`、`wallet_transactions`、
`users`、`usage_events`、`monthly_statements` 与 `usage_rebills`。

**锁表与停机**：建新表不锁任何已有表的行；建外键时 MySQL 对每张父表各取短暂的元数据锁（等正在
进行的、碰过它们的事务结束），不扫行。`usage_events` 与 `wallet_transactions` 是写得最多的两张
父表：元数据锁只等当时在跑的事务结束（计费与入账的事务都很短）。建触发器只锁两张新表（空表，
没有读写方）。不需要停机。

**备份**：要求前置备份（与 0005–0041 同一口径）。本版不写任何行。

**部署顺序**：`deploy.sh` 先迁移、再换镜像。本迁移的 `down_revision` 是 AIH-TASK-242 的
`0041_provider_cost_recon`，须在它之后部署（设计 §11 的合并顺序：253 → 254 → 255 → 262 → 258 →
256 → …）。本任务不带写入方与接口，部署后只多两张没人读写的空表；旧代码不知道它们。先迁移后换镜像、
或反过来都安全。

**失败处理**：⚠️ MySQL 的 DDL **不参与事务**：每条 `CREATE TABLE` 与每条 `CREATE TRIGGER` 都隐式
提交。每张表连同索引、外键与 CHECK 在**一条** `CREATE TABLE` 里，MySQL 8 的 InnoDB DDL 是原子的，
加上第 1–2 步的检查，每个窗口都有确定的结局：

- 预检失败：什么都没改，`alembic_version` 仍是 `0041_provider_cost_recon`。给账号打开
  `log_bin_trust_function_creators` 后重新部署。
- 建 `usage_rebills` 失败（锁等待超时、进程被杀）→ 整条回滚，两张表都不存在，版本仍是 0041 →
  原样重新部署。
- `usage_rebills` 建成、`usage_rebill_lines` 建成之前中断 → 只有前一张表（空），版本仍是 0041 →
  原样重新部署：判前一张形状正确，只建后一张与四个触发器。
- 两张表建成、某个触发器建成之前中断 → 表都在、缺一到四个触发器，版本仍是 0041 → 原样重新部署：
  只建缺的那几个。这期间没有写入方（AIH-TASK-256 上线之前），缺触发器不会让记录被改。
- 全部建成、写 `alembic_version` 之前进程退出 → 原样重新部署：判为已完成，不发 DDL，只把版本前进。
- 有人手工建过同名但形状不同的表 → 重跑在第 1 步失败，报错点名这张表，不发 DDL，版本仍是 0041 →
  先确认表里没有任何行：为空则 `DROP TABLE` 它（它上面的触发器随表删除；要删的是 `usage_rebills`
  而 `usage_rebill_lines` 也在时，先删后者）再重新部署；有行则停下，按数据事故处理，不要删。
- 同名触发器存在但时机、事件、所在表或正文不同 → 重跑在第 2 步失败，报错点名触发器，不发 DDL，
  版本仍是 0041 → 人对照设计查看；确认是手工建的之后 `DROP TRIGGER` 它，再重新部署（只补建缺的
  那个）。

**回滚**：生产上的回滚**只回滚应用镜像，不降级 0042**（设计 §8「回滚」）。

- **有了 Rebill 之后 0042 不可降级**：两张表保存的逐事件金额、新旧规则与价格版本、原账本行与原
  对账单的引用是 spec §19 要求的审计链；账本里的 `REBILL_CREDIT` / `REBILL_DEBIT` 行离开它们就
  无法解释（只知道金额，不知道改了哪些事件、按什么版本、从多少改到多少），而账本行本身不可改、
  不可删。所以降级只在两张表都为空时执行；有行时只回滚应用代码（新表对旧代码无害：旧代码不读它们），
  不得为了降级而清表或手工删行。
- **执行前提**：降级不是部署流水线的一部分（`deploy/deploy.sh` 的回滚只切换镜像、不降级迁移），
  只由人手工执行：先停 `api` 容器（Rebill 唯一的写入方是管理端确认接口），确认没有进行中的请求，
  再执行 `alembic downgrade 0041_provider_cost_recon`。写入方停了，「检查之后、删表之前有人提交
  Rebill」的窗口就不存在。**这是操作前提，不由数据库强制**：要在数据库里挡住并发写入，只能在检查
  之前对两张表 `RENAME TABLE` 或加表锁，而 MySQL 8 对带外键的子表做这些要拿它全部父表（`tenants`、
  `wallet_transactions`、`usage_events`、`users`、价格与规则表、`monthly_statements`）的排他元数据锁，
  排队等锁期间计费、钱包、登录等读写全部被挡住 —— 为了防一个停掉 api 就不存在的并发，换来一次可能
  的全站停顿（设计 v9 撤回改名协议的理由），不用 RENAME。
- **步骤**（设计 §8）：`--sql` 离线模式判断不了表是否为空，直接抛错（须在线执行），不输出任何
  `DROP`；两张表都已不在时什么都不做（不发 DDL）；对在的每张表先
  `SELECT 1 FROM <表> LIMIT 1`（先 `usage_rebills` 后 `usage_rebill_lines`），任一有行就抛
  `RuntimeError`（`<表> is not empty: downgrading would delete rebill records (spec §19); restore
  from backup or keep this revision`），不发任何 DDL、不删任何行；都为空时：两张都在 → MySQL 上
  **一条** `DROP TABLE usage_rebill_lines, usage_rebills`（MySQL 8 的原子 DDL：要么都删、
  要么都不删，触发器随表删除），SQLite 上逐个删（先明细后 Rebill）；只剩一张（upgrade 在
  两条 `CREATE TABLE` 之间中断）→ 只删这一张。

Revision ID: 0042_usage_rebills
Revises: 0041_provider_cost_recon
Create Date: 2026-11-24
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import NoReturn

import sqlalchemy as sa

from alembic import op

# `alembic_version.version_num` 是 VARCHAR(32)，ID 必须不超过 32 个字符（本 ID 18 个字符）。
revision: str = "0042_usage_rebills"
down_revision: str | None = "0041_provider_cost_recon"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_REBILLS = "usage_rebills"
_LINES = "usage_rebill_lines"
# 建的顺序（明细的外键引用 Rebill）。
_TABLES = (_REBILLS, _LINES)
_TENANTS = "tenants"
_PRICING_RULES = "pricing_rules"
_PRICE_VERSIONS = "provider_price_versions"
_WALLET_TRANSACTIONS = "wallet_transactions"
_USERS = "users"
_USAGE_EVENTS = "usage_events"
_STATEMENTS = "monthly_statements"

# ⚠️ 下面都是冻结的字面量；模型那边的声明由 tests/backend/test_usage_rebills_migration.py 比对。

# 列名 → (类型与宽度, 可空)，按建表顺序（设计 §2 的两张表）。类型写成 MySQL 反射出来的样子
# （去掉空白）。可空的恰好是设计写明可空的四列。
_REBILL_COLUMNS: dict[str, tuple[str, bool]] = {
    "id": ("BIGINT", False),
    "public_id": ("CHAR(36)", False),
    "tenant_id": ("BIGINT", False),
    "idempotency_key": ("CHAR(36)", False),
    "request_fingerprint": ("CHAR(64)", False),
    "corrective_pricing_rule_id": ("BIGINT", True),
    "corrective_provider_price_version_id": ("BIGINT", True),
    "event_count": ("INTEGER", False),
    "previous_total": ("DECIMAL(20,8)", False),
    "corrected_total": ("DECIMAL(20,8)", False),
    "difference": ("DECIMAL(20,8)", False),
    "wallet_transaction_id": ("BIGINT", False),
    "reason": ("VARCHAR(255)", False),
    "created_by": ("BIGINT", False),
    "created_at": ("DATETIME", False),
}
_LINE_COLUMNS: dict[str, tuple[str, bool]] = {
    "id": ("BIGINT", False),
    "rebill_id": ("BIGINT", False),
    "usage_event_id": ("BIGINT", False),
    "original_wallet_transaction_id": ("BIGINT", True),
    "previous_charge": ("DECIMAL(20,8)", False),
    "corrected_charge": ("DECIMAL(20,8)", False),
    "difference": ("DECIMAL(20,8)", False),
    "previous_pricing_rule_id": ("BIGINT", False),
    "previous_provider_price_version_id": ("BIGINT", False),
    "corrected_pricing_rule_id": ("BIGINT", False),
    "corrected_provider_price_version_id": ("BIGINT", False),
    "corrected_estimated_provider_cost_myr": ("DECIMAL(20,8)", False),
    "original_period": ("CHAR(7)", False),
    "original_statement_id": ("BIGINT", True),
    "created_at": ("DATETIME", False),
}
_PRIMARY_KEY: tuple[str, ...] = ("id",)

# 索引名 → (列, 唯一)，按建的顺序：唯一约束在前（建表语句里），普通索引在后。每个外键在子表上都有
# 前导列一致的索引：唯一约束或设计点名的索引能充当的不另建，其余按 `ix_<表>_<列>` 显式建。
_REBILL_INDEXES: dict[str, tuple[tuple[str, ...], bool]] = {
    "uq_usage_rebills_public_id": (("public_id",), True),
    "uq_usage_rebills_idempotency_key": (("idempotency_key",), True),
    # 也是 `wallet_transaction_id` 外键用的索引。
    "uq_usage_rebills_wallet_transaction_id": (("wallet_transaction_id",), True),
    # 也是 `tenant_id` 外键用的索引。
    "ix_usage_rebills_tenant": (("tenant_id", "id"), False),
    "ix_usage_rebills_corrective_pricing_rule_id": (("corrective_pricing_rule_id",), False),
    "ix_usage_rebills_corrective_provider_price_version_id": (
        ("corrective_provider_price_version_id",),
        False,
    ),
    "ix_usage_rebills_created_by": (("created_by",), False),
}
_LINE_INDEXES: dict[str, tuple[tuple[str, ...], bool]] = {
    # 也是 `rebill_id` 外键用的索引。
    "uq_usage_rebill_lines_rebill_event": (("rebill_id", "usage_event_id"), True),
    # 取一个事件的最近明细（`id` 最大）；也是 `usage_event_id` 外键用的索引。
    "ix_usage_rebill_lines_usage_event": (("usage_event_id", "id"), False),
    "ix_usage_rebill_lines_original_wallet_transaction_id": (
        ("original_wallet_transaction_id",),
        False,
    ),
    "ix_usage_rebill_lines_previous_pricing_rule_id": (("previous_pricing_rule_id",), False),
    "ix_usage_rebill_lines_previous_provider_price_version_id": (
        ("previous_provider_price_version_id",),
        False,
    ),
    "ix_usage_rebill_lines_corrected_pricing_rule_id": (("corrected_pricing_rule_id",), False),
    "ix_usage_rebill_lines_corrected_provider_price_version_id": (
        ("corrected_provider_price_version_id",),
        False,
    ),
    "ix_usage_rebill_lines_original_statement_id": (("original_statement_id",), False),
}


def _foreign_key(
    table: str, column: str, referred: str
) -> tuple[str, tuple[tuple[str, ...], str, tuple[str, ...], str]]:
    """`fk_<表>_<列>` → ((列,), 引用表, ("id",), "RESTRICT")。"""
    return f"fk_{table}_{column}", ((column,), referred, ("id",), "RESTRICT")


# 外键名 → (列, 引用表, 引用列, 删除规则)，都是 RESTRICT：删父行不许连带删掉财务记录。
_REBILL_FOREIGN_KEYS: dict[str, tuple[tuple[str, ...], str, tuple[str, ...], str]] = dict(
    [
        _foreign_key(_REBILLS, "tenant_id", _TENANTS),
        _foreign_key(_REBILLS, "corrective_pricing_rule_id", _PRICING_RULES),
        _foreign_key(_REBILLS, "corrective_provider_price_version_id", _PRICE_VERSIONS),
        _foreign_key(_REBILLS, "wallet_transaction_id", _WALLET_TRANSACTIONS),
        _foreign_key(_REBILLS, "created_by", _USERS),
    ]
)
_LINE_FOREIGN_KEYS: dict[str, tuple[tuple[str, ...], str, tuple[str, ...], str]] = dict(
    [
        _foreign_key(_LINES, "rebill_id", _REBILLS),
        _foreign_key(_LINES, "usage_event_id", _USAGE_EVENTS),
        _foreign_key(_LINES, "original_wallet_transaction_id", _WALLET_TRANSACTIONS),
        _foreign_key(_LINES, "previous_pricing_rule_id", _PRICING_RULES),
        _foreign_key(_LINES, "previous_provider_price_version_id", _PRICE_VERSIONS),
        _foreign_key(_LINES, "corrected_pricing_rule_id", _PRICING_RULES),
        _foreign_key(_LINES, "corrected_provider_price_version_id", _PRICE_VERSIONS),
        _foreign_key(_LINES, "original_statement_id", _STATEMENTS),
    ]
)

# 原因去掉 ASCII 空白后不为空串：表达式逐字照设计 §2（CHAR(9) 制表、CHAR(10) 换行、CHAR(11)
# 垂直制表、CHAR(12) 换页、CHAR(13) 回车依次替换为空格，再 TRIM）。
_REASON_CHECK = (
    "TRIM(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(reason, CHAR(9), ' '), CHAR(10), ' '),"
    " CHAR(11), ' '), CHAR(12), ' '), CHAR(13), ' ')) <> ''"
)

# 名字 → 条件（设计 §2）。与 app/models/usage_rebills.py 上的 CheckConstraint 一致（空白除外）。
# 不建「差额 = 新 − 旧」的等式（设计 v9）。
_REBILL_CHECKS: dict[str, str] = {
    "ck_usage_rebills_event_count": "event_count BETWEEN 1 AND 1000",
    "ck_usage_rebills_previous_total": "previous_total >= 0",
    "ck_usage_rebills_corrected_total": "corrected_total >= 0",
    "ck_usage_rebills_difference_nonzero": "difference <> 0",
    "ck_usage_rebills_reason": _REASON_CHECK,
}
_LINE_CHECKS: dict[str, str] = {
    "ck_usage_rebill_lines_previous_charge": "previous_charge >= 0",
    "ck_usage_rebill_lines_corrected_charge": "corrected_charge >= 0",
    "ck_usage_rebill_lines_estimated_cost": "corrected_estimated_provider_cost_myr >= 0",
    "ck_usage_rebill_lines_difference_nonzero": "difference <> 0",
}

_COLUMNS = {_REBILLS: _REBILL_COLUMNS, _LINES: _LINE_COLUMNS}
_INDEXES = {_REBILLS: _REBILL_INDEXES, _LINES: _LINE_INDEXES}
_FOREIGN_KEYS = {_REBILLS: _REBILL_FOREIGN_KEYS, _LINES: _LINE_FOREIGN_KEYS}
_CHECKS = {_REBILLS: _REBILL_CHECKS, _LINES: _LINE_CHECKS}

# 设计的形状（第 1 步逐项比），按表。
_SHAPES: dict[str, dict[str, object]] = {
    table: {
        "columns": _COLUMNS[table],
        "primary_key": _PRIMARY_KEY,
        "indexes": _INDEXES[table],
        "foreign_keys": _FOREIGN_KEYS[table],
        "checks": set(_CHECKS[table]),
    }
    for table in _TABLES
}


def _constraints_ddl(table: str) -> list[str]:
    indexes = _INDEXES[table]
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
        for name, (columns, referred, referred_columns, rule) in _FOREIGN_KEYS[table].items()
    ]
    conditions = [
        f"    CONSTRAINT {name} CHECK ({condition})" for name, condition in _CHECKS[table].items()
    ]
    return [*uniques, *plain, *keys, *conditions]


def _create_table_mysql(table: str, columns: list[str]) -> str:
    """MySQL 上的建表语句：一条，原子。"""
    return (
        f"CREATE TABLE {table} (\n"
        + ",\n".join([*columns, "    PRIMARY KEY (id)", *_constraints_ddl(table)])
        + "\n)"
    )


_CREATE_REBILLS_MYSQL = _create_table_mysql(
    _REBILLS,
    [
        "    id BIGINT NOT NULL AUTO_INCREMENT",
        "    public_id CHAR(36) NOT NULL",
        "    tenant_id BIGINT NOT NULL",
        "    idempotency_key CHAR(36) NOT NULL",
        "    request_fingerprint CHAR(64) NOT NULL",
        "    corrective_pricing_rule_id BIGINT NULL",
        "    corrective_provider_price_version_id BIGINT NULL",
        "    event_count INT NOT NULL",
        "    previous_total DECIMAL(20, 8) NOT NULL",
        "    corrected_total DECIMAL(20, 8) NOT NULL",
        "    difference DECIMAL(20, 8) NOT NULL",
        "    wallet_transaction_id BIGINT NOT NULL",
        "    reason VARCHAR(255) NOT NULL",
        "    created_by BIGINT NOT NULL",
        "    created_at DATETIME NOT NULL",
    ],
)
_CREATE_LINES_MYSQL = _create_table_mysql(
    _LINES,
    [
        "    id BIGINT NOT NULL AUTO_INCREMENT",
        "    rebill_id BIGINT NOT NULL",
        "    usage_event_id BIGINT NOT NULL",
        "    original_wallet_transaction_id BIGINT NULL",
        "    previous_charge DECIMAL(20, 8) NOT NULL",
        "    corrected_charge DECIMAL(20, 8) NOT NULL",
        "    difference DECIMAL(20, 8) NOT NULL",
        "    previous_pricing_rule_id BIGINT NOT NULL",
        "    previous_provider_price_version_id BIGINT NOT NULL",
        "    corrected_pricing_rule_id BIGINT NOT NULL",
        "    corrected_provider_price_version_id BIGINT NOT NULL",
        "    corrected_estimated_provider_cost_myr DECIMAL(20, 8) NOT NULL",
        "    original_period CHAR(7) NOT NULL",
        "    original_statement_id BIGINT NULL",
        "    created_at DATETIME NOT NULL",
    ],
)
_CREATE_TABLES_MYSQL = {_REBILLS: _CREATE_REBILLS_MYSQL, _LINES: _CREATE_LINES_MYSQL}


def _trigger_body(table: str) -> str:
    """一张表的两个触发器共用的主体（设计 §2「只追加」）：无条件拒绝。MySQL 把 `FOR EACH ROW` 之后
    的部分原样存进 ACTION_STATEMENT，第 2 步去掉首尾空白后与它逐字比较。"""
    return f"""
BEGIN
    SIGNAL SQLSTATE '45000'
        SET MESSAGE_TEXT = '{table} rows are immutable';
END
""".strip()


_TRIGGER_BODIES: dict[str, str] = {table: _trigger_body(table) for table in _TABLES}

# 触发器名 → (所在表, 时机, 事件)，按建的顺序。
_TRIGGER_EVENTS: dict[str, tuple[str, str, str]] = {
    "trg_usage_rebills_before_update": (_REBILLS, "BEFORE", "UPDATE"),
    "trg_usage_rebills_before_delete": (_REBILLS, "BEFORE", "DELETE"),
    "trg_usage_rebill_lines_before_update": (_LINES, "BEFORE", "UPDATE"),
    "trg_usage_rebill_lines_before_delete": (_LINES, "BEFORE", "DELETE"),
}


def _create_trigger(name: str) -> str:
    """整条 CREATE TRIGGER 是**一条**语句（与 0041 同一写法）。"""
    table, timing, event = _TRIGGER_EVENTS[name]
    return f"""
CREATE TRIGGER {name}
{timing} {event} ON {table}
FOR EACH ROW
{_TRIGGER_BODIES[table]}
"""


_TRIGGERS: dict[str, str] = {name: _create_trigger(name) for name in _TRIGGER_EVENTS}

_PREREQUISITE_QUERY = "SELECT @@log_bin, @@log_bin_trust_function_creators"
_PREREQUISITE_ERROR = (
    "MySQL has binary logging on and log_bin_trust_function_creators OFF, "
    + "so this account cannot create the usage rebill triggers (ERROR 1419). "
    + "Set log_bin_trust_function_creators = ON first; nothing has been changed."
)


def _delete_rules_query(table: str) -> str:
    # MySQL 上外键的删除规则以 information_schema 为准（与 0041 同一个来源）：反射不返回缺省的
    # RESTRICT。
    return (
        "SELECT CONSTRAINT_NAME, DELETE_RULE FROM information_schema.REFERENTIAL_CONSTRAINTS"
        f" WHERE CONSTRAINT_SCHEMA = DATABASE() AND TABLE_NAME = '{table}'"
    )


# 按名字找：同名触发器挂在别的表上也要看见（触发器名在库内唯一）。
_TRIGGERS_QUERY = (
    "SELECT TRIGGER_NAME, EVENT_OBJECT_TABLE, ACTION_TIMING, EVENT_MANIPULATION, ACTION_STATEMENT"
    " FROM information_schema.TRIGGERS WHERE TRIGGER_SCHEMA = DATABASE()"
    " AND TRIGGER_NAME IN (" + ", ".join(f"'{name}'" for name in _TRIGGER_EVENTS) + ")"
)


def _has_rows_query(table: str) -> str:
    # 降级前的数据检查（设计 §8）。
    return f"SELECT 1 FROM {table} LIMIT 1"


# 降级：都为空时一条语句删两张表（MySQL 8 的原子 DDL；触发器随表删除）。
_DROP_BOTH_MYSQL = f"DROP TABLE {_LINES}, {_REBILLS}"

_DESIGN = "docs/design/AIH-TASK-253-usage-rebill.md"


def _not_empty_error(table: str) -> str:
    # 文案照设计 §8。
    return (
        f"{table} is not empty: downgrading would delete rebill records (spec §19);"
        " restore from backup or keep this revision"
    )


def _is_mysql() -> bool:
    return op.get_context().dialect.name == "mysql"


def _type_text(column_type: object) -> str:
    """`CHAR(36) COLLATE …` → `CHAR(36)`：只留类型与宽度。"""
    return str(column_type).replace(", ", ",").split(" ")[0].upper()


def _reflected_shape(table: str) -> dict | None:
    """一张表的现状（列、主键、索引、外键、CHECK 名）；表不存在时返回 None。"""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table(table):
        return None

    columns = {
        str(column["name"]): (_type_text(column["type"]), bool(column["nullable"]))
        for column in inspector.get_columns(table)
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
    return {
        "columns": columns,
        "primary_key": primary_key,
        "indexes": indexes,
        "foreign_keys": foreign_keys,
        "checks": checks,
    }


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
    return problems


def _triggers(bind: sa.engine.Connection) -> dict[str, tuple[str, str, str, str]]:
    """这四个名字的现有触发器：名字 → (所在表, 时机, 事件, ACTION_STATEMENT)。"""
    return {
        str(name): (str(table), str(timing), str(kind), str(statement))
        for name, table, timing, kind, statement in bind.execute(sa.text(_TRIGGERS_QUERY)).all()
    }


def _trigger_matches(name: str, row: tuple[str, str, str, str]) -> bool:
    """在设计的表上、时机与事件是设计的，主体与那张表的触发器主体逐字相同（只去掉首尾空白）。"""
    expected = _TRIGGER_EVENTS[name]
    table, timing, kind, body = row
    return (table, timing, kind) == expected and body.strip() == _TRIGGER_BODIES[expected[0]]


def _require_trigger_privilege(bind: sa.engine.Connection) -> None:
    """Refuse to run where the migration account could not create triggers (as 0041)."""
    log_bin, trusted = bind.execute(sa.text(_PREREQUISITE_QUERY)).one()
    if int(log_bin) == 1 and int(trusted) != 1:
        raise RuntimeError(_PREREQUISITE_ERROR)


def _refuse_shape(table: str, problems: list[str]) -> NoReturn:
    raise RuntimeError(
        f"Cannot upgrade to 0042: table {table} exists but differs from the design in "
        + "; ".join(problems)
        + f". No DDL was issued. First check that {table} holds no row. If it is empty, drop the"
        f" table by hand (DROP TABLE {table}; drop {_LINES} first when it is {_REBILLS} and"
        f" {_LINES} is there too) and upgrade again; if any row is there, stop and handle it as a"
        " data incident: do not drop it"
    )


def _refuse_triggers(names: list[str]) -> NoReturn:
    raise RuntimeError(
        "Cannot upgrade to 0042: "
        + "; ".join(
            f"trigger {name} exists but is not the one 0042 builds (table"
            f" {_TRIGGER_EVENTS[name][0]}, {' '.join(_TRIGGER_EVENTS[name][1:])}, body word for"
            " word)"
            for name in names
        )
        + f". No DDL was issued. Have a person compare it with {_DESIGN} §2; once it is known to"
        " be hand-made, DROP TRIGGER it and upgrade again: only the missing trigger is built"
    )


def _create_table(table: str) -> None:
    if _is_mysql():
        op.execute(_CREATE_TABLES_MYSQL[table])
        return
    if table == _REBILLS:
        columns = [
            sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
            sa.Column("public_id", sa.CHAR(length=36), nullable=False),
            sa.Column("tenant_id", sa.BigInteger(), nullable=False),
            sa.Column("idempotency_key", sa.CHAR(length=36), nullable=False),
            sa.Column("request_fingerprint", sa.CHAR(length=64), nullable=False),
            sa.Column("corrective_pricing_rule_id", sa.BigInteger(), nullable=True),
            sa.Column("corrective_provider_price_version_id", sa.BigInteger(), nullable=True),
            sa.Column("event_count", sa.Integer(), nullable=False),
            sa.Column("previous_total", sa.Numeric(precision=20, scale=8), nullable=False),
            sa.Column("corrected_total", sa.Numeric(precision=20, scale=8), nullable=False),
            sa.Column("difference", sa.Numeric(precision=20, scale=8), nullable=False),
            sa.Column("wallet_transaction_id", sa.BigInteger(), nullable=False),
            sa.Column("reason", sa.String(length=255), nullable=False),
            sa.Column("created_by", sa.BigInteger(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
        ]
    else:
        columns = [
            sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
            sa.Column("rebill_id", sa.BigInteger(), nullable=False),
            sa.Column("usage_event_id", sa.BigInteger(), nullable=False),
            sa.Column("original_wallet_transaction_id", sa.BigInteger(), nullable=True),
            sa.Column("previous_charge", sa.Numeric(precision=20, scale=8), nullable=False),
            sa.Column("corrected_charge", sa.Numeric(precision=20, scale=8), nullable=False),
            sa.Column("difference", sa.Numeric(precision=20, scale=8), nullable=False),
            sa.Column("previous_pricing_rule_id", sa.BigInteger(), nullable=False),
            sa.Column("previous_provider_price_version_id", sa.BigInteger(), nullable=False),
            sa.Column("corrected_pricing_rule_id", sa.BigInteger(), nullable=False),
            sa.Column("corrected_provider_price_version_id", sa.BigInteger(), nullable=False),
            sa.Column(
                "corrected_estimated_provider_cost_myr",
                sa.Numeric(precision=20, scale=8),
                nullable=False,
            ),
            sa.Column("original_period", sa.CHAR(length=7), nullable=False),
            sa.Column("original_statement_id", sa.BigInteger(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
        ]
    indexes = _INDEXES[table]
    op.create_table(
        table,
        *columns,
        sa.PrimaryKeyConstraint("id"),
        *(
            sa.UniqueConstraint(*index_columns, name=name)
            for name, (index_columns, unique) in indexes.items()
            if unique
        ),
        *(
            sa.ForeignKeyConstraint(
                list(key_columns),
                [f"{referred}.{column}" for column in referred_columns],
                name=name,
                ondelete=rule,
            )
            for name, (key_columns, referred, referred_columns, rule) in _FOREIGN_KEYS[
                table
            ].items()
        ),
        *(sa.CheckConstraint(condition, name=name) for name, condition in _CHECKS[table].items()),
    )
    for name, (index_columns, unique) in indexes.items():
        if not unique:
            op.create_index(name, table, list(index_columns), unique=False)


def _upgrade_offline() -> None:
    # 离线读不了库：按「什么都没做过」输出（照 0041）。触发器只在 MySQL 上有。
    for table in _TABLES:
        _create_table(table)
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
    # 第 2 步。表不存在时，同名触发器只可能挂在别的表上：同样拒绝。
    triggers = _triggers(bind) if mysql else {}
    wrong = [name for name, row in triggers.items() if not _trigger_matches(name, row)]
    if wrong:
        _refuse_triggers(sorted(wrong, key=list(_TRIGGER_EVENTS).index))
    missing = [name for name in _TRIGGER_EVENTS if name not in triggers] if mysql else []
    create = [table for table in _TABLES if shapes[table] is None]
    # 第 3 步：已完成。
    if not create and not missing:
        return

    # 第 4 步。⚠️ 只在要发 DDL 时，紧接在第一条 DDL 之前。
    if mysql:
        _require_trigger_privilege(bind)
    # 第 5 步：缺什么建什么。
    for table in create:
        _create_table(table)
    for name in missing:
        op.execute(_TRIGGERS[name])


def downgrade() -> None:
    # ⚠️ 离线模式判断不了表是否为空：拒绝，不输出任何 DROP（设计 §8）。
    if op.get_context().as_sql:
        raise RuntimeError(
            f"Cannot downgrade 0042 in --sql offline mode: whether tables {_REBILLS} and {_LINES}"
            " are empty can only be checked against the database. Run the downgrade online"
        )
    # ⚠️ 执行前提（见文件头「回滚」）：已停掉 api 容器（唯一的写入方），由人手工执行。
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    present = [table for table in _TABLES if inspector.has_table(table)]
    # 两张表都已不在：不发 DDL。
    if not present:
        return
    # ⚠️ 在任何 DDL 之前：任一张有行就拒绝，删表等于删掉 Rebill 的审计链（设计 §8、spec §19）。
    for table in present:
        if bind.execute(sa.text(_has_rows_query(table))).first() is not None:
            raise RuntimeError(_not_empty_error(table))
    if len(present) == len(_TABLES):
        if _is_mysql():
            # 一条语句删两张表：要么都删、要么都不删；触发器随表删除。
            op.execute(_DROP_BOTH_MYSQL)
        else:
            # SQLite（只在测试里用）逐个删：先明细，后 Rebill。
            for table in reversed(_TABLES):
                op.drop_table(table)
        return
    # 只剩一张（upgrade 在两条 CREATE TABLE 之间中断）：只删它。
    op.drop_table(present[0])

"""wallet reconciliation runs and findings tables

照设计闸门 #474 v13（docs/design/AIH-TASK-129-wallet-reconciliation.md）§2「数据库」与
「迁移写法」、§8；本迁移是 AIH-TASK-129。只建两张新表，不改任何已有表、不回填、不读
钱包与账本：

`wallet_reconciliation_runs`（一次核对运行一行，12 列）

- `uq_wallet_reconciliation_runs_public_id (public_id)`；
- `uq_wallet_reconciliation_runs_running_slot (running_slot)`：`RUNNING` 时恒为 1、其余为
  NULL，**同一时刻至多一条 `RUNNING` 由数据库保证**（NULL 可有多行）；
- `ix_wallet_reconciliation_runs_run_date (run_date)`：普通索引，**不唯一**——被收尾的
  `ABANDONED` 行不挡住当天再开始；
- `ix_wallet_reconciliation_runs_status_finished (status, finished_at)`：告警取最近一次
  `COMPLETED`；
- 五条 CHECK：`status`、`finished`、`counts`、`lease`、`running_slot`（`running_slot` 那条
  写上 `running_slot IS NOT NULL`：少了它「`RUNNING` 且 NULL」得 UNKNOWN、被放行）；
- `cursor_tenant_id`、`wallets_checked`、`wallets_inconsistent` 库级缺省 0。

`wallet_reconciliation_findings`（一次运行里一个不一致的钱包一行，8 列）

- `uq_wallet_reconciliation_findings_run_tenant (run_id, tenant_id)`：一次运行一个租户至多
  一行；同时是 `run_id` 外键用的索引；
- `ix_wallet_reconciliation_findings_tenant (tenant_id)`：**显式声明**，供 `tenant_id` 外键
  使用，所以 MySQL 不另建隐式索引；
- `fk_wallet_reconciliation_findings_run` → `wallet_reconciliation_runs.id`、
  `fk_wallet_reconciliation_findings_tenant` → `tenants.id`，都是 `ON DELETE RESTRICT`；
- 一条 CHECK：`ck_wallet_reconciliation_findings_codes`（`problem_codes <> ''`）。

没有写入方：行由 AIH-TASK-131、132 的核对运行写，告警属于 AIH-TASK-133，接口属于
AIH-TASK-134。`app/models/wallet_reconciliation.py` 同步声明了这些列、索引与约束，
`tests/backend/test_wallet_reconciliation_migration.py` 比对两边一致。

步骤
----

1. 用 inspector 判两张表的现状。「形状」逐项比（设计 §2「迁移写法」）：恰好这些列（名称、
   类型与宽度、可空性；**不比缺省值**）；恰好这些索引（唯一与普通，名称、列与列顺序，没有
   前缀长度）；恰好这些外键（列、引用表与列、`ON DELETE RESTRICT`，MySQL 上的删除规则读
   `information_schema.REFERENTIAL_CONSTRAINTS`）；恰好这些 CHECK（名称）。
   - 都不存在 → 两张都待建；
   - 都存在且形状都正确 → 跳过、不发 DDL；
   - 只存在 runs、形状正确且为空 → 只待建 findings（补完在两条 `CREATE TABLE` 之间中断的
     升级）；
   - 其余（只存在 findings；只存在的 runs 有行或形状不对；任何一项缺失、多出或不同）→
     抛错，点名表，**不发任何 DDL**。
2. MySQL：每张待建的表发**一条** `CREATE TABLE`（索引、外键与 CHECK 都写在里面），先 runs
   后 findings。其他方言：`create_table` 加 `create_index`（DDL 在 Alembic 的迁移事务里）。

§132 第 13 条分析
-----------------

**已有数据**：只建两张新表，不改任何已有表的列、索引、约束与触发器，不回填，不读写任何已有
行；钱包与账本不被碰（INV-4、INV-5）。外键只指向 `tenants` 与本迁移建的 runs。

**锁表与停机**：建新表不锁任何已有表的行；建 `tenants` 外键时 MySQL 对 `tenants` 取短暂的
元数据锁（等正在进行的、碰过 `tenants` 的事务结束），不扫行。不需要停机。

**备份**：要求前置备份（与 0005–0028 同一口径）。本版不写任何数据。

**部署顺序**：`deploy.sh` 先迁移、再换镜像。本任务不带写入方与接口，部署后只多两张没人读写的
空表；旧代码不知道它们。先迁移后换镜像、或反过来都安全（设计 §8「兼容窗口」）。

**失败处理**：⚠️ MySQL 的 DDL **不参与事务**，两条 `CREATE TABLE` 之间、以及它们与写
`alembic_version` 之间都有失败窗口（与 0026–0028 相同）。每张表连同索引、外键与 CHECK 在
**一条** `CREATE TABLE` 里建，MySQL 8 的 InnoDB DDL 是原子的，加上第 1 步的检查，每个窗口都有
确定的结局：

- 建 runs 失败（锁等待超时、进程被杀）→ 整条回滚，两张表都不存在，`alembic_version` 仍是
  `0028_notifications` → 原样重新部署；
- runs 建成、建 findings 之前或建 findings 失败 → 只剩 runs（空），版本仍是 0028 → 原样
  重新部署：第 1 步判它形状正确且为空，只建 findings；
- 两张都建成、写 `alembic_version` 之前进程退出 → 版本仍是 0028 → 原样重新部署：第 1 步判
  形状正确，不发 DDL，只把版本前进；
- 有人手工建过同名但形状不同的表、只剩 findings、或只剩的 runs 有行 → 重跑在第 1 步失败，
  报错点名表与处理方法，不发 DDL，版本仍是 0028 → 先确认那（两）张表没有任何行：为空则手工
  删掉（先 findings 后 runs）再重新部署；有行则停下，按数据事故处理，不要删。

`--sql` 离线模式读不了库：upgrade 跳过检查，按两张表都不存在输出两条 `CREATE TABLE`（与
0027、0028 相同）。

**回滚**：生产上的回滚**只回滚应用镜像，不降级 0029**（设计 §8）。`downgrade` **只删空表**：
先对存在的每张表 `SELECT 1 … LIMIT 1`，任何一张有行就抛错（消息点名表、处理方法是保留表只
回滚应用镜像），不发任何 DDL、不删任何行——运行与 findings 是过去核对的记录，删了无法重建；
两张都存在且都为空 → 先删 findings 再删 runs；只存在其中一张且它为空 → 删掉它（补完在两条
`DROP TABLE` 之间中断的降级：MySQL 的 DDL 自动提交，中断后会只剩 runs、版本仍是 0029）；两张
都不存在时什么都不做。`--sql` 离线模式判断不了表是否为空：downgrade 直接抛错（须在线执行），
不输出 `DROP TABLE`。

Revision ID: 0029_wallet_reconciliation
Revises: 0028_notifications
Create Date: 2026-10-29
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import NoReturn

import sqlalchemy as sa

from alembic import op

revision: str = "0029_wallet_reconciliation"
down_revision: str | None = "0028_notifications"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_RUNS = "wallet_reconciliation_runs"
_FINDINGS = "wallet_reconciliation_findings"
_TENANTS = "tenants"

# ⚠️ 下面都是冻结的字面量；模型那边的声明由
# tests/backend/test_wallet_reconciliation_migration.py 比对。

# 列名 → (类型与宽度, 可空)，按建表顺序。类型写成 MySQL 反射出来的样子
# （去掉空白）。缺省值不在形状里（设计 §2）。
_RUNS_COLUMNS: dict[str, tuple[str, bool]] = {
    "id": ("BIGINT", False),
    "public_id": ("CHAR(36)", False),
    "run_date": ("DATE", False),
    "status": ("VARCHAR(16)", False),
    "started_at": ("DATETIME", False),
    "finished_at": ("DATETIME", True),
    "cursor_tenant_id": ("BIGINT", False),
    "wallets_checked": ("INTEGER", False),
    "wallets_inconsistent": ("INTEGER", False),
    "lease_owner": ("CHAR(36)", True),
    "lease_until": ("DATETIME", True),
    "running_slot": ("SMALLINT", True),
}
_FINDINGS_COLUMNS: dict[str, tuple[str, bool]] = {
    "id": ("BIGINT", False),
    "run_id": ("BIGINT", False),
    "tenant_id": ("BIGINT", False),
    "problem_codes": ("VARCHAR(255)", False),
    "balance": ("DECIMAL(20,8)", True),
    "wallet_version": ("BIGINT", True),
    "ledger_rows": ("BIGINT", False),
    "checked_at": ("DATETIME", False),
}

_UNIQUE_RUNS_PUBLIC_ID = "uq_wallet_reconciliation_runs_public_id"
_UNIQUE_RUNS_RUNNING_SLOT = "uq_wallet_reconciliation_runs_running_slot"
_INDEX_RUNS_RUN_DATE = "ix_wallet_reconciliation_runs_run_date"
_INDEX_RUNS_STATUS_FINISHED = "ix_wallet_reconciliation_runs_status_finished"
_UNIQUE_FINDINGS_RUN_TENANT = "uq_wallet_reconciliation_findings_run_tenant"
_INDEX_FINDINGS_TENANT = "ix_wallet_reconciliation_findings_tenant"
# 索引名 → (列, 唯一)，按建的顺序。
_RUNS_INDEXES: dict[str, tuple[tuple[str, ...], bool]] = {
    _UNIQUE_RUNS_PUBLIC_ID: (("public_id",), True),
    _UNIQUE_RUNS_RUNNING_SLOT: (("running_slot",), True),
    _INDEX_RUNS_RUN_DATE: (("run_date",), False),
    _INDEX_RUNS_STATUS_FINISHED: (("status", "finished_at"), False),
}
_FINDINGS_INDEXES: dict[str, tuple[tuple[str, ...], bool]] = {
    _UNIQUE_FINDINGS_RUN_TENANT: (("run_id", "tenant_id"), True),
    _INDEX_FINDINGS_TENANT: (("tenant_id",), False),
}

_FK_FINDINGS_RUN = "fk_wallet_reconciliation_findings_run"
_FK_FINDINGS_TENANT = "fk_wallet_reconciliation_findings_tenant"
# 外键名 → (列, 引用表, 引用列, 删除规则)。runs 没有外键。
_RUNS_FOREIGN_KEYS: dict[str, tuple[tuple[str, ...], str, tuple[str, ...], str]] = {}
_FINDINGS_FOREIGN_KEYS: dict[str, tuple[tuple[str, ...], str, tuple[str, ...], str]] = {
    _FK_FINDINGS_RUN: (("run_id",), _RUNS, ("id",), "RESTRICT"),
    _FK_FINDINGS_TENANT: (("tenant_id",), _TENANTS, ("id",), "RESTRICT"),
}

# 名字 → 条件。与 app/models/wallet_reconciliation.py 上的 CheckConstraint 一致
# （空白除外）。「A = B」式的布尔等式写成两条分支的 OR：布尔等式的写法两库不同义。
_RUNS_CHECKS: dict[str, str] = {
    "ck_wallet_reconciliation_runs_status": "status IN ('RUNNING', 'COMPLETED', 'ABANDONED')",
    # (status = 'RUNNING') = (finished_at IS NULL)
    "ck_wallet_reconciliation_runs_finished": (
        "(status = 'RUNNING' AND finished_at IS NULL)"
        " OR (status <> 'RUNNING' AND finished_at IS NOT NULL)"
    ),
    "ck_wallet_reconciliation_runs_counts": (
        "wallets_inconsistent >= 0 AND wallets_inconsistent <= wallets_checked"
    ),
    # (lease_owner IS NULL) = (lease_until IS NULL)
    "ck_wallet_reconciliation_runs_lease": (
        "(lease_owner IS NULL AND lease_until IS NULL)"
        " OR (lease_owner IS NOT NULL AND lease_until IS NOT NULL)"
    ),
    # ⚠️ `running_slot IS NOT NULL` 不能省：NULL = 1 是 UNKNOWN，少了它「RUNNING 且
    # NULL」整条得 UNKNOWN，CHECK 只拒绝 FALSE，会放行（设计 §2）。
    "ck_wallet_reconciliation_runs_running_slot": (
        "(status = 'RUNNING' AND running_slot IS NOT NULL AND running_slot = 1)"
        " OR (status <> 'RUNNING' AND running_slot IS NULL)"
    ),
}
_FINDINGS_CHECKS: dict[str, str] = {
    "ck_wallet_reconciliation_findings_codes": "problem_codes <> ''",
}

# 表名 → 设计形状（与 _reflected_shape 的返回同一结构）。
_DESIGN: dict[str, dict] = {
    _RUNS: {
        "columns": _RUNS_COLUMNS,
        "indexes": _RUNS_INDEXES,
        "foreign_keys": _RUNS_FOREIGN_KEYS,
        "checks": set(_RUNS_CHECKS),
    },
    _FINDINGS: {
        "columns": _FINDINGS_COLUMNS,
        "indexes": _FINDINGS_INDEXES,
        "foreign_keys": _FINDINGS_FOREIGN_KEYS,
        "checks": set(_FINDINGS_CHECKS),
    },
}


def _checks_ddl(checks: dict[str, str]) -> str:
    return ",\n".join(
        f"    CONSTRAINT {name} CHECK ({condition})" for name, condition in checks.items()
    )


# MySQL 上的建表语句：每张表一条，原子。
_CREATE_RUNS_MYSQL = f"""
CREATE TABLE {_RUNS} (
    id BIGINT NOT NULL AUTO_INCREMENT,
    public_id CHAR(36) NOT NULL,
    run_date DATE NOT NULL,
    status VARCHAR(16) NOT NULL,
    started_at DATETIME NOT NULL,
    finished_at DATETIME NULL,
    cursor_tenant_id BIGINT NOT NULL DEFAULT 0,
    wallets_checked INT NOT NULL DEFAULT 0,
    wallets_inconsistent INT NOT NULL DEFAULT 0,
    lease_owner CHAR(36) NULL,
    lease_until DATETIME NULL,
    running_slot SMALLINT NULL,
    PRIMARY KEY (id),
    CONSTRAINT {_UNIQUE_RUNS_PUBLIC_ID} UNIQUE (public_id),
    CONSTRAINT {_UNIQUE_RUNS_RUNNING_SLOT} UNIQUE (running_slot),
    INDEX {_INDEX_RUNS_RUN_DATE} (run_date),
    INDEX {_INDEX_RUNS_STATUS_FINISHED} (status, finished_at),
{_checks_ddl(_RUNS_CHECKS)}
)
"""

_CREATE_FINDINGS_MYSQL = f"""
CREATE TABLE {_FINDINGS} (
    id BIGINT NOT NULL AUTO_INCREMENT,
    run_id BIGINT NOT NULL,
    tenant_id BIGINT NOT NULL,
    problem_codes VARCHAR(255) NOT NULL,
    balance DECIMAL(20, 8) NULL,
    wallet_version BIGINT NULL,
    ledger_rows BIGINT NOT NULL,
    checked_at DATETIME NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT {_UNIQUE_FINDINGS_RUN_TENANT} UNIQUE (run_id, tenant_id),
    INDEX {_INDEX_FINDINGS_TENANT} (tenant_id),
    CONSTRAINT {_FK_FINDINGS_RUN} FOREIGN KEY (run_id) REFERENCES {_RUNS} (id)
        ON DELETE RESTRICT,
    CONSTRAINT {_FK_FINDINGS_TENANT} FOREIGN KEY (tenant_id) REFERENCES {_TENANTS} (id)
        ON DELETE RESTRICT,
{_checks_ddl(_FINDINGS_CHECKS)}
)
"""


def _delete_rules_mysql(table: str) -> str:
    """MySQL 上外键的删除规则以 information_schema 为准（与 test_migrations.py 同一个来源）：
    反射不返回缺省的 RESTRICT。"""
    return (
        "SELECT CONSTRAINT_NAME, DELETE_RULE FROM information_schema.REFERENTIAL_CONSTRAINTS"
        f" WHERE CONSTRAINT_SCHEMA = DATABASE() AND TABLE_NAME = '{table}'"
    )


def _has_rows_sql(table: str) -> str:
    return f"SELECT 1 FROM {table} LIMIT 1"


def _type_text(column_type: object) -> str:
    """`DECIMAL(20, 8) COLLATE …` → `DECIMAL(20,8)`：只留类型与宽度。"""
    return str(column_type).replace(", ", ",").split(" ")[0].upper()


def _reflected_shape(table: str) -> dict | None:
    """一张表的现状（列、索引、外键、CHECK 名）；表不存在或离线模式读不了库时返回 None。"""
    if op.get_context().as_sql:
        return None
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table(table):
        return None

    columns = {
        str(column["name"]): (_type_text(column["type"]), bool(column["nullable"]))
        for column in inspector.get_columns(table)
    }
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

    if op.get_context().dialect.name == "mysql":
        delete_rules = dict(bind.execute(sa.text(_delete_rules_mysql(table))).all())
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
        "indexes": indexes,
        "foreign_keys": foreign_keys,
        "checks": checks,
    }


def _shape_problems(table: str, shape: dict) -> list[str]:
    """与设计不一致的项；空列表 = 形状正确。"""
    design = _DESIGN[table]
    problems = []
    if shape["columns"] != design["columns"]:
        problems.append("columns (name, type and width, nullability)")
    if shape["indexes"] != design["indexes"]:
        problems.append("indexes (name, columns and their order, uniqueness)")
    if shape["foreign_keys"] != design["foreign_keys"]:
        problems.append("foreign keys (columns, referred table, ON DELETE RESTRICT)")
    if shape["checks"] != design["checks"]:
        problems.append("check constraints")
    return problems


def _has_rows(table: str) -> bool:
    return op.get_bind().execute(sa.text(_has_rows_sql(table))).first() is not None


def _refuse_upgrade(reasons: list[str], tables: list[str]) -> NoReturn:
    """`tables` 按删除顺序（先 findings 后 runs）。"""
    names = " and ".join(tables)
    drops = ", then ".join(f"DROP TABLE {table}" for table in tables)
    raise RuntimeError(
        "Cannot upgrade to 0029: "
        + "; ".join(reasons)
        + f". No DDL was issued. First check that {names} hold no row. If empty, drop by hand"
        f" ({drops}) and upgrade again; if any row is there, stop and handle it as a data"
        " incident: do not drop it"
    )


def _create_runs() -> None:
    if op.get_context().dialect.name == "mysql":
        op.execute(_CREATE_RUNS_MYSQL)
        return
    op.create_table(
        _RUNS,
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("public_id", sa.CHAR(length=36), nullable=False),
        sa.Column("run_date", sa.Date(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("cursor_tenant_id", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("wallets_checked", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("wallets_inconsistent", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("lease_owner", sa.CHAR(length=36), nullable=True),
        sa.Column("lease_until", sa.DateTime(), nullable=True),
        sa.Column("running_slot", sa.SmallInteger(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("public_id", name=_UNIQUE_RUNS_PUBLIC_ID),
        sa.UniqueConstraint("running_slot", name=_UNIQUE_RUNS_RUNNING_SLOT),
        *(sa.CheckConstraint(condition, name=name) for name, condition in _RUNS_CHECKS.items()),
    )
    for name in (_INDEX_RUNS_RUN_DATE, _INDEX_RUNS_STATUS_FINISHED):
        columns, _ = _RUNS_INDEXES[name]
        op.create_index(name, _RUNS, list(columns), unique=False)


def _create_findings() -> None:
    if op.get_context().dialect.name == "mysql":
        op.execute(_CREATE_FINDINGS_MYSQL)
        return
    op.create_table(
        _FINDINGS,
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("run_id", sa.BigInteger(), nullable=False),
        sa.Column("tenant_id", sa.BigInteger(), nullable=False),
        sa.Column("problem_codes", sa.String(length=255), nullable=False),
        sa.Column("balance", sa.DECIMAL(precision=20, scale=8), nullable=True),
        sa.Column("wallet_version", sa.BigInteger(), nullable=True),
        sa.Column("ledger_rows", sa.BigInteger(), nullable=False),
        sa.Column("checked_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", "tenant_id", name=_UNIQUE_FINDINGS_RUN_TENANT),
        sa.ForeignKeyConstraint(
            ["run_id"], [f"{_RUNS}.id"], name=_FK_FINDINGS_RUN, ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], [f"{_TENANTS}.id"], name=_FK_FINDINGS_TENANT, ondelete="RESTRICT"
        ),
        *(sa.CheckConstraint(condition, name=name) for name, condition in _FINDINGS_CHECKS.items()),
    )
    columns, _ = _FINDINGS_INDEXES[_INDEX_FINDINGS_TENANT]
    op.create_index(_INDEX_FINDINGS_TENANT, _FINDINGS, list(columns), unique=False)


def upgrade() -> None:
    runs = _reflected_shape(_RUNS)
    findings = _reflected_shape(_FINDINGS)
    # ⚠️ 先判、再发 DDL：任何一项不对都不动库，版本也不前进。
    if runs is None and findings is None:
        _create_runs()
        _create_findings()
        return
    if runs is None:
        _refuse_upgrade([f"table {_FINDINGS} exists without table {_RUNS}"], [_FINDINGS])

    reasons = []
    runs_problems = _shape_problems(_RUNS, runs)
    if runs_problems:
        reasons.append(
            f"table {_RUNS} exists but differs from the design in " + ", ".join(runs_problems)
        )
    if findings is not None:
        findings_problems = _shape_problems(_FINDINGS, findings)
        if findings_problems:
            reasons.append(
                f"table {_FINDINGS} exists but differs from the design in "
                + ", ".join(findings_problems)
            )
        if reasons:
            _refuse_upgrade(reasons, [_FINDINGS, _RUNS])
        # 两张都在且形状正确：上次建好后写 alembic_version 失败，只把版本前进。
        return

    if reasons:
        _refuse_upgrade(reasons, [_RUNS])
    # 只剩 runs：只有它为空时才是「两条 CREATE TABLE 之间中断」，补建 findings。
    if _has_rows(_RUNS):
        _refuse_upgrade(
            [f"table {_RUNS} exists without table {_FINDINGS} and is not empty"], [_RUNS]
        )
    _create_findings()


def downgrade() -> None:
    # ⚠️ 离线模式判断不了表是否为空：拒绝，不输出 DROP TABLE（设计 §2「迁移写法」）。
    if op.get_context().as_sql:
        raise RuntimeError(
            f"Cannot downgrade 0029 in --sql offline mode: whether tables {_FINDINGS} and"
            f" {_RUNS} are empty can only be checked against the database. Run the downgrade"
            " online"
        )
    inspector = sa.inspect(op.get_bind())
    # 删除顺序：先 findings（它引用 runs）后 runs。
    present = [table for table in (_FINDINGS, _RUNS) if inspector.has_table(table)]
    # ⚠️ 在任何 DDL 之前：任一张有行就整体拒绝，一张都不删。
    not_empty = [table for table in present if _has_rows(table)]
    if not_empty:
        raise RuntimeError(
            "Cannot downgrade 0029: "
            + "; ".join(f"table {table} is not empty" for table in not_empty)
            + " (the record of past wallet reconciliation runs). No DDL was issued and no row"
            f" was deleted. Keep the tables {_FINDINGS} and {_RUNS} and roll back only the"
            " application image"
        )
    # 两张都在 → 两张都删；只剩一张（中断的降级）→ 删掉它；都不在 → 什么都不做。
    for table in present:
        op.drop_table(table)

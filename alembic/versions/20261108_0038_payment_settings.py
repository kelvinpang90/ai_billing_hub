"""payment_settings: the one-row table of payment rules, with its seed row

照设计闸门 #587 v3（docs/design/AIH-TASK-188-payment-settings-portal.md）§2「数据库」、§3 INV-6、
INV-10、§4「数据库负责的」与 §8；本迁移是 AIH-TASK-188，建立在 0037（设计闸门 #585 v3）之上，
0036 / 0037 没有被本设计改写的地方照旧。**只建一张新表 `payment_settings` 并插入种子行**，不改
任何已有表的列、索引、约束与触发器，不读写任何已有行：

- `payment_settings` **恰好一行**：主键 `id` 加 `ck_payment_settings_singleton`（`id = 1`）；
- 列：`min_amount`、`max_amount`（可空）、`recovery_buffer`（DECIMAL(20,8)，INV-10）、
  `preset_amounts`（JSON，定点金额字符串的数组，形状由应用校验）、`reuse_window_seconds`、
  `pending_timeout_seconds`、`version`（乐观锁）、`updated_at`、`updated_by_user_id`（可空）；
- 六条字段 CHECK `ck_payment_settings_<列>`：三个金额的范围与两位小数（`max_amount` 为空或不小于
  `min_amount`；`recovery_buffer` 至少 0.01，不能为 0）、两个秒数的范围、`version >= 1`；
- `ix_payment_settings_updated_by_user_id (updated_by_user_id)`：外键列的显式索引（与 0036 / 0037
  同一理由：MySQL 不另建一个模型里没有的隐式索引）；
- `fk_payment_settings_updated_by_user_id` → `users.id`，`ON DELETE RESTRICT`。

种子行（设计 §2，与 A 的配置项缺省一致，部署后建单行为不变）：`min_amount = 10.00`、
`max_amount = NULL`、`recovery_buffer = 10.00`、
`preset_amounts = ["50.00","100.00","300.00","500.00","1000.00"]`（定点字符串，不用 JSON 数字）、
`reuse_window_seconds = 60`、`pending_timeout_seconds = 3600`、`version = 1`、
`updated_at = UTC_TIMESTAMP()`（迁移执行的时刻，不带时区的 UTC、精确到秒）、
`updated_by_user_id = NULL`。

写入方：只有 AIH-TASK-189 的 `update_payment_settings`（条件更新 `WHERE id = 1 AND version = …`，与
`PAYMENT_SETTINGS_UPDATE` 审计同一事务，INV-6、INV-13）；没有插入与删除路径。
`app/models/payment_settings.py` 的 `PaymentSettings` 同步声明了这些列、索引、外键与 CHECK（同名
同序），`tests/backend/test_payment_settings_migration.py` 比对两边一致。

步骤
----

1. 用 inspector 判 `payment_settings` 是否存在（设计 §8：不做形状比较）。已存在 → 抛错，**不发
   任何 DDL**、不写任何行。
2. 不存在 → **一条** `CREATE TABLE (…)`（`_CREATE_TABLE`，列、主键、索引、外键与七条 CHECK 都写在
   这一条里），再**一条** `INSERT`（`_INSERT_SEED`）写种子行。

`--sql` 离线模式读不了库：跳过第 1 步，按「什么都没做过」输出这两条语句。

只支持 MySQL：Alembic 的迁移链本来就只在 MySQL 上跑（0006 起查 `@@log_bin`），种子行的时刻用
MySQL 的 `UTC_TIMESTAMP()`。单元测试的 SQLite 由模型 `create_all` 建，同样带七条 CHECK 与外键
（不带种子行：需要它的测试自己写入）。其他方言在线、离线都直接抛错，不发 DDL。

§132 第 13 条分析
-----------------

**已有数据**：只建一张新表并写它的种子行，不改任何已有表的列、索引、约束与触发器，不回填，不读写
任何已有行；钱包与账本不被碰（INV-4、INV-5），支付与网关事件也不被碰。外键只指向 `users`，种子行
的 `updated_by_user_id` 为空，不引用任何用户。

**锁表与停机**：建新表不锁任何已有表的行；建外键时 MySQL 对 `users` 取短暂的元数据锁（等正在
进行的、碰过它的事务结束），不扫行。插入种子行只碰新表。不需要停机。

**备份**：要求前置备份（与 0005–0037 同一口径）。本版只写新表的一行种子，不写任何已有表。

**部署顺序**：`deploy.sh` 先迁移、再换镜像（设计 §8：A、B 全部合并之后，合并即部署）。迁移之后、
新容器起来之前，旧代码照旧运行：旧代码不知道这张表，仍从配置项读金额规则；种子值等于那些配置项的
缺省，所以前后行为一致。之后读写这张表的服务与接口属于 AIH-TASK-189..191；生产的建单开关仍关着。

**失败处理**：⚠️ MySQL 的 DDL **不参与事务**：`CREATE TABLE` 隐式提交，之后的 `INSERT` 与写
`alembic_version` 在同一个事务里一起提交。建表连同索引、外键与 CHECK 在**一条** `CREATE TABLE`
里，MySQL 8 的 InnoDB DDL 是原子的，所以每个窗口都有确定的结局：

- `CREATE TABLE` 失败（锁等待超时、进程被杀）→ 整条回滚，表不存在，版本仍是
  `0037_payment_review` → 修好原因后原样重新部署。
- 表建成、`INSERT` 与写 `alembic_version` 提交之前中断 → 表在、没有行，版本仍是 0037 → 重跑在
  第 1 步失败，报错点名这张表，不发 DDL。这期间没有写入方：先确认 `SELECT 1 FROM payment_settings
  LIMIT 1` 没有行，为空则 `DROP TABLE payment_settings` 再重新部署；有行则停下，需要人对照设计查看。
- 种子行插入失败（种子值是冻结的字面量、满足每条 CHECK，所以只会是连接中断之类）→ 插入随事务
  回滚，同上一条。
- 有人手工建过同名的表 → 同上，在第 1 步失败、不发 DDL；需要人对照设计查看这张表。

**回滚**：生产上的回滚是关充值开关（设计 §8），**不降级 0038**。`downgrade`：`--sql` 离线模式直接
抛错（须在线执行，与 0036 / 0037 同一口径），不输出任何 DDL；表不存在 → 跳过；否则删表。设置的
每次修改都写了 `PAYMENT_SETTINGS_UPDATE` 审计（含前后值与版本），删表不丢财务数据，所以不做非空
检查；再升级时重新写入种子行，之前改过的值要由管理员照审计重新设置。

Revision ID: 0038_payment_settings
Revises: 0037_payment_review
Create Date: 2026-11-08
"""

from __future__ import annotations

import json
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0038_payment_settings"
down_revision: str | None = "0037_payment_review"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "payment_settings"
_USERS = "users"

# ⚠️ 下面都是冻结的字面量；模型那边的声明由 tests/backend/test_payment_settings_migration.py 比对。

# 列名 → (类型与宽度, 可空)，按建表顺序。类型写成 MySQL 反射出来的样子（去掉空白）。
_COLUMNS: dict[str, tuple[str, bool]] = {
    "id": ("INTEGER", False),
    "min_amount": ("DECIMAL(20,8)", False),
    "max_amount": ("DECIMAL(20,8)", True),
    "recovery_buffer": ("DECIMAL(20,8)", False),
    "preset_amounts": ("JSON", False),
    "reuse_window_seconds": ("INTEGER", False),
    "pending_timeout_seconds": ("INTEGER", False),
    "version": ("INTEGER", False),
    "updated_at": ("DATETIME", False),
    "updated_by_user_id": ("BIGINT", True),
}

# 索引名 → (列, 唯一)。
_INDEXES: dict[str, tuple[tuple[str, ...], bool]] = {
    "ix_payment_settings_updated_by_user_id": (("updated_by_user_id",), False),
}

# 外键名 → (列, 引用表, 引用列, 删除规则)。
_FOREIGN_KEYS: dict[str, tuple[tuple[str, ...], str, tuple[str, ...], str]] = {
    "fk_payment_settings_updated_by_user_id": (
        ("updated_by_user_id",),
        _USERS,
        ("id",),
        "RESTRICT",
    ),
}

# 名字 → 条件。与 app/models/payment_settings.py 上的 CheckConstraint 一致（空白除外）。
_CHECKS: dict[str, str] = {
    "ck_payment_settings_singleton": "id = 1",
    "ck_payment_settings_min_amount": (
        "min_amount >= 2 AND min_amount <= 999999.99 AND min_amount = ROUND(min_amount, 2)"
    ),
    "ck_payment_settings_max_amount": (
        "max_amount IS NULL OR (max_amount >= min_amount AND max_amount <= 999999.99"
        " AND max_amount = ROUND(max_amount, 2))"
    ),
    "ck_payment_settings_recovery_buffer": (
        "recovery_buffer >= 0.01 AND recovery_buffer <= 999999.99"
        " AND recovery_buffer = ROUND(recovery_buffer, 2)"
    ),
    "ck_payment_settings_reuse_window_seconds": (
        "reuse_window_seconds >= 0 AND reuse_window_seconds <= 300"
    ),
    "ck_payment_settings_pending_timeout_seconds": (
        "pending_timeout_seconds >= 2400 AND pending_timeout_seconds <= 86400"
    ),
    "ck_payment_settings_version": "version >= 1",
}

# 种子行（设计 §2），`updated_at` 之外的列 → SQL 字面量。金额是定点文本，推荐金额是定点字符串的
# JSON 数组（INV-10：不经浮点）。
_SEED_PRESET_AMOUNTS: tuple[str, ...] = ("50.00", "100.00", "300.00", "500.00", "1000.00")
_SEED: dict[str, str] = {
    "id": "1",
    "min_amount": "10.00",
    "max_amount": "NULL",
    "recovery_buffer": "10.00",
    "preset_amounts": "'" + json.dumps(list(_SEED_PRESET_AMOUNTS)) + "'",
    "reuse_window_seconds": "60",
    "pending_timeout_seconds": "3600",
    "version": "1",
    # 迁移执行的时刻（不带时区的 UTC，DATETIME 精确到秒）；`--sql` 输出里同样在执行时取值。
    "updated_at": "UTC_TIMESTAMP()",
    "updated_by_user_id": "NULL",
}


def _create_table() -> str:
    columns = [
        f"    {name} {kind.replace(',', ', ')} {'NULL' if nullable else 'NOT NULL'}"
        for name, (kind, nullable) in _COLUMNS.items()
    ]
    indexes = [
        f"    INDEX {name} ({', '.join(index_columns)})"
        for name, (index_columns, _) in _INDEXES.items()
    ]
    keys = [
        f"    CONSTRAINT {name} FOREIGN KEY ({', '.join(key_columns)})"
        f" REFERENCES {referred} ({', '.join(referred_columns)}) ON DELETE {rule}"
        for name, (key_columns, referred, referred_columns, rule) in _FOREIGN_KEYS.items()
    ]
    checks = [f"    CONSTRAINT {name} CHECK ({condition})" for name, condition in _CHECKS.items()]
    return (
        f"CREATE TABLE {_TABLE} (\n"
        + ",\n".join([*columns, "    PRIMARY KEY (id)", *indexes, *keys, *checks])
        + "\n)"
    )


# 升级第 2 步：**一条** CREATE TABLE，原子；再一条 INSERT 写种子行。
_CREATE_TABLE = _create_table()
_INSERT_SEED = f"INSERT INTO {_TABLE} ({', '.join(_SEED)}) VALUES ({', '.join(_SEED.values())})"


def _require_mysql(action: str) -> None:
    dialect = op.get_context().dialect.name
    if dialect != "mysql":
        raise RuntimeError(
            f"Cannot {action} 0038 on {dialect}: it only runs on MySQL (the seed row's time is"
            " MySQL's UTC_TIMESTAMP(); the unit tests build SQLite from the models)."
            " No DDL was issued"
        )


def upgrade() -> None:
    _require_mysql("upgrade to")
    if op.get_context().as_sql:
        # 离线读不了库：按「什么都没做过」输出。
        op.execute(_CREATE_TABLE)
        op.execute(_INSERT_SEED)
        return

    # 第 1 步。⚠️ 先判、再发 DDL：表已存在就不动库，版本也不前进。
    if sa.inspect(op.get_bind()).has_table(_TABLE):
        raise RuntimeError(
            f"Cannot upgrade to 0038: table {_TABLE} already exists. No DDL was issued."
            " 0038 only creates a new table with its seed row and does not compare shapes."
            " The table was made by hand or an earlier run stopped before writing"
            " alembic_version: if it holds no row, drop it by hand and upgrade again;"
            " otherwise have a person compare it with"
            " docs/design/AIH-TASK-188-payment-settings-portal.md §2 before upgrading again"
        )
    # 第 2 步：一条原子 CREATE TABLE，再写种子行。
    op.execute(_CREATE_TABLE)
    op.execute(_INSERT_SEED)


def downgrade() -> None:
    _require_mysql("downgrade")
    # ⚠️ 离线模式判断不了表在不在：拒绝，不输出任何 DDL（设计 §8）。
    if op.get_context().as_sql:
        raise RuntimeError(
            f"Cannot downgrade 0038 in --sql offline mode: whether table {_TABLE} exists can"
            " only be checked against the database. Run the downgrade online"
        )
    if not sa.inspect(op.get_bind()).has_table(_TABLE):
        return
    # 设置的历史在审计里（PAYMENT_SETTINGS_UPDATE），删表不丢财务数据（设计 §8）。
    op.drop_table(_TABLE)

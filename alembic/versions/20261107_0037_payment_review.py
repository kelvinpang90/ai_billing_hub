"""payments get the four manual resolution columns, two checks, a foreign key and an index

照设计闸门 #585 v3（docs/design/AIH-TASK-179-payments-review.md）§2「数据库」、§3 INV-3、
INV-13、§4「已结案」一行与 §8；本迁移是 AIH-TASK-179，建立在 0036（设计闸门 #580 v3）之上。
**只给 `payments` 加**下面这些，不改任何已有的列、索引、约束与其他表，不回填：

- 四列，全部可空：`resolution VARCHAR(16)`（人工结案的方式）、`resolved_at DATETIME`、
  `resolved_by_user_id BIGINT`（操作的管理员）、`resolution_reason VARCHAR(255)`（原因）；
- `ix_payments_resolved_by_user_id (resolved_by_user_id)`：外键列的显式索引（与 0036 同一理由：
  MySQL 不另建一个模型里没有的隐式索引）；
- `fk_payments_resolved_by_user_id` → `users.id`，`ON DELETE RESTRICT`；
- `ck_payments_resolution`：为空或 `CREDITED` / `CLOSED`；
- `ck_payments_resolution_fields`：四列全为空，或四列全不为空、`review_required_at` 不为空、且
  `CREDITED` 配 `PAID` / `CLOSED` 配 `FAILED`。**结案即终态**：任何路径把已结案的行改成别的
  状态、或只清掉部分结案字段，都违反它（INV-3）。

没有写入方：四列只由 AIH-TASK-182 的 `resolve_review` 写，与支付状态、账本、审计同一事务
（INV-13）。`app/models/payments.py` 的 `Payment` 同步声明了这些列、索引、外键与 CHECK（同名
同序），`tests/backend/test_payment_review_migration.py` 比对两边一致。

步骤
----

1. 用 inspector 读 `payments` 的列，四个新列任一已存在 → 抛错，点名存在的那几列，**不发任何
   DDL**（设计 §8：不做形状比较）。
2. 都不存在 → **一条** `ALTER TABLE payments`（`_ALTER_UPGRADE`）同时加四列、索引、外键与两条
   CHECK。

`--sql` 离线模式读不了库：跳过第 1 步，按「什么都没做过」输出那一条 ALTER。

只支持 MySQL：Alembic 的迁移链本来就只在 MySQL 上跑（0006 起查 `@@log_bin`）；SQLite 不能用
`ALTER TABLE` 加 CHECK 与外键。单元测试的 SQLite 由模型 `create_all` 建，同样带这两条 CHECK
与外键。其他方言在线、离线都直接抛错，不发 DDL。

§132 第 13 条分析
-----------------

**已有数据**：已有支付行的四个新列全为空，满足两条 CHECK（第一个分支），**不回填**、不改任何
已有行的任何列；`payment_gateway_events`、钱包与账本都不被碰（INV-4、INV-5）。

**锁表与停机**：加外键让这条 ALTER 用 `ALGORITHM=COPY` 重建 `payments`：期间对它的写入在元数据
锁上排队（读照常），ALTER 等正在进行的、碰过它的事务结束；对 `users` 取短暂的元数据锁，不扫行。
`payments` 是 0036 刚建的表，生产上至多数十行，重建在秒级完成。部署时支付开关默认关
（`payments_enabled`），没有写入在排队。不需要停机。

**备份**：要求前置备份（与 0005–0036 同一口径）。本版不写任何数据。

**部署顺序**：`deploy.sh` 先迁移、再换镜像（设计 §8：A 的 172..178 全部合并之后，合并即部署）。
迁移之后、新容器起来之前，旧代码照旧运行：旧代码不读不写这四列，它写入的行四列全为空，满足
CHECK。之后的补对账、结案服务与接口属于 AIH-TASK-180..187。

**失败处理**：⚠️ MySQL 的 DDL **不参与事务**，`ALTER TABLE` 与写 `alembic_version` 之间有失败
窗口。四列、索引、外键与两条 CHECK 在**一条** `ALTER TABLE` 里加，MySQL 8 的 InnoDB DDL 是
原子的，所以每个窗口都有确定的结局：

- `ALTER TABLE` 失败（锁等待超时、进程被杀、某个已有行意外违反 CHECK）→ 整条回滚，表结构不变，
  一列也没有加上，`alembic_version` 仍是 `0036_payments` → 修好原因后原样重新部署。
- `ALTER TABLE` 成功、写 `alembic_version` 之前进程退出 → 四列都在，版本仍是 0036 → 重跑在第 1 步
  失败，报错点名四列，不发 DDL。这期间没有写入方：先确认 `SELECT 1 FROM payments WHERE
  resolution IS NOT NULL LIMIT 1` 没有行，再按 `downgrade` 的顺序手工删掉（
  `ALTER TABLE payments DROP CHECK ck_payments_resolution_fields`、`… DROP CHECK
  ck_payments_resolution`、`… DROP FOREIGN KEY fk_payments_resolved_by_user_id`、
  `DROP INDEX ix_payments_resolved_by_user_id ON payments`、四条 `… DROP COLUMN`），然后重新
  部署；有结案行则停下，按数据事故处理，不要删。
- 有人手工加过同名的列 → 同上，在第 1 步失败、不发 DDL；需要人对照设计查看这张表。

**回滚**：生产上的回滚是关开关（设计 §8），**不降级 0037**；已结案的数据保留。`downgrade`：
`--sql` 离线模式判断不了有没有结案行，直接抛错（须在线执行），不输出任何 DDL；在线时只要
`resolution` 列存在、且有 `resolution IS NOT NULL` 的行，就抛错，不发任何 DDL、不删任何行——
结案记录是审计相关的财务结论，删了无法重建；否则按「两条 CHECK → 外键 → 索引 → 四列」的顺序删
（MySQL 不许删掉 CHECK 还在引用的列；不许先删外键依赖的索引，错误 1553，与 0002、0005、0018
的写法相同），每一项只在仍存在时删，中途失败可以直接重跑。

Revision ID: 0037_payment_review
Revises: 0036_payments
Create Date: 2026-11-07
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0037_payment_review"
down_revision: str | None = "0036_payments"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PAYMENTS = "payments"
_USERS = "users"

# ⚠️ 下面都是冻结的字面量；模型那边的声明由 tests/backend/test_payment_review_migration.py 比对。

# 列名 → (类型与宽度, 可空)，按加的顺序（接在 0036 的 `updated_at` 之后）。类型写成 MySQL 反射
# 出来的样子（去掉空白）。
_COLUMNS: dict[str, tuple[str, bool]] = {
    "resolution": ("VARCHAR(16)", True),
    "resolved_at": ("DATETIME", True),
    "resolved_by_user_id": ("BIGINT", True),
    "resolution_reason": ("VARCHAR(255)", True),
}

# 索引名 → (列, 唯一)。
_INDEXES: dict[str, tuple[tuple[str, ...], bool]] = {
    "ix_payments_resolved_by_user_id": (("resolved_by_user_id",), False),
}

# 外键名 → (列, 引用表, 引用列, 删除规则)。
_FOREIGN_KEYS: dict[str, tuple[tuple[str, ...], str, tuple[str, ...], str]] = {
    "fk_payments_resolved_by_user_id": (("resolved_by_user_id",), _USERS, ("id",), "RESTRICT"),
}

# 名字 → 条件。与 app/models/payments.py 上的 CheckConstraint 一致（空白除外）；
# `ck_payments_resolution_fields` 与设计 §2 的表达式逐字同一逻辑。
_CHECKS: dict[str, str] = {
    "ck_payments_resolution": "resolution IS NULL OR resolution IN ('CREDITED', 'CLOSED')",
    "ck_payments_resolution_fields": (
        "(resolution IS NULL AND resolved_at IS NULL AND resolved_by_user_id IS NULL"
        " AND resolution_reason IS NULL)"
        " OR (resolution IS NOT NULL AND resolved_at IS NOT NULL"
        " AND resolved_by_user_id IS NOT NULL"
        " AND resolution_reason IS NOT NULL AND review_required_at IS NOT NULL"
        " AND ((resolution = 'CREDITED' AND status = 'PAID')"
        " OR (resolution = 'CLOSED' AND status = 'FAILED')))"
    ),
}


def _column_ddl(name: str) -> str:
    kind, nullable = _COLUMNS[name]
    return f"    ADD COLUMN {name} {kind} {'NULL' if nullable else 'NOT NULL'}"


def _alter_upgrade() -> str:
    columns = [_column_ddl(name) for name in _COLUMNS]
    indexes = [
        f"    ADD INDEX {name} ({', '.join(index_columns)})"
        for name, (index_columns, _) in _INDEXES.items()
    ]
    keys = [
        f"    ADD CONSTRAINT {name} FOREIGN KEY ({', '.join(key_columns)})"
        f" REFERENCES {referred} ({', '.join(referred_columns)}) ON DELETE {rule}"
        for name, (key_columns, referred, referred_columns, rule) in _FOREIGN_KEYS.items()
    ]
    checks = [
        f"    ADD CONSTRAINT {name} CHECK ({condition})" for name, condition in _CHECKS.items()
    ]
    return f"ALTER TABLE {_PAYMENTS}\n" + ",\n".join([*columns, *indexes, *keys, *checks])


# 升级第 2 步：**一条** ALTER，原子。
_ALTER_UPGRADE = _alter_upgrade()

# 降级前的数据检查（设计 §8）：有任何一行已结案就拒绝。
_RESOLVED_ROWS = f"SELECT 1 FROM {_PAYMENTS} WHERE resolution IS NOT NULL LIMIT 1"


def _require_mysql(action: str) -> None:
    dialect = op.get_context().dialect.name
    if dialect != "mysql":
        raise RuntimeError(
            f"Cannot {action} 0037 on {dialect}: it only runs on MySQL (SQLite cannot add checks"
            " or foreign keys with ALTER TABLE; the unit tests build SQLite from the models)."
            " No DDL was issued"
        )


def upgrade() -> None:
    _require_mysql("upgrade to")
    if op.get_context().as_sql:
        # 离线读不了库：按「什么都没做过」输出。
        op.execute(_ALTER_UPGRADE)
        return

    # 第 1 步。⚠️ 先判、再发 DDL：任一列已存在都不动库，版本也不前进。
    present = {str(column["name"]) for column in sa.inspect(op.get_bind()).get_columns(_PAYMENTS)}
    existing = [name for name in _COLUMNS if name in present]
    if existing:
        raise RuntimeError(
            "Cannot upgrade to 0037: "
            + "; ".join(f"column {_PAYMENTS}.{name} already exists" for name in existing)
            + ". No DDL was issued. 0037 only adds new columns and does not compare shapes."
            " The table was changed by hand or an earlier run stopped before writing"
            " alembic_version: have a person compare it with"
            " docs/design/AIH-TASK-179-payments-review.md §2 before upgrading again"
        )
    # 第 2 步：一条原子 ALTER。
    op.execute(_ALTER_UPGRADE)


def downgrade() -> None:
    _require_mysql("downgrade")
    # ⚠️ 离线模式判断不了有没有结案行：拒绝，不输出任何 DDL（设计 §8）。
    if op.get_context().as_sql:
        raise RuntimeError(
            f"Cannot downgrade 0037 in --sql offline mode: whether {_PAYMENTS} holds resolved"
            " payments can only be checked against the database. Run the downgrade online"
        )
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {str(column["name"]) for column in inspector.get_columns(_PAYMENTS)}
    # ⚠️ 在任何 DDL 之前：有结案行就拒绝，删列等于删掉人工结案的结论。
    if "resolution" in columns and bind.execute(sa.text(_RESOLVED_ROWS)).first() is not None:
        raise RuntimeError(
            f"Cannot downgrade 0037: table {_PAYMENTS} holds resolved payments (resolution is"
            " set). No DDL was issued and no row was deleted. Keep the columns and turn payments"
            " off instead"
        )
    checks = {str(check["name"]) for check in inspector.get_check_constraints(_PAYMENTS)}
    keys = {str(key["name"]) for key in inspector.get_foreign_keys(_PAYMENTS)}
    indexes = {str(index["name"]) for index in inspector.get_indexes(_PAYMENTS)}
    # 倒序：CHECK（MySQL 不许删掉 CHECK 还在引用的列）→ 外键（之后才能删它用的索引，错误
    # 1553）→ 索引 → 列。每一项只在仍存在时删：中途失败可以直接重跑。
    for name in reversed(_CHECKS):
        if name in checks:
            op.execute(f"ALTER TABLE {_PAYMENTS} DROP CHECK {name}")
    for name in reversed(_FOREIGN_KEYS):
        if name in keys:
            op.drop_constraint(name, _PAYMENTS, type_="foreignkey")
    for name in reversed(_INDEXES):
        if name in indexes:
            op.drop_index(name, table_name=_PAYMENTS)
    for name in reversed(_COLUMNS):
        if name in columns:
            op.drop_column(_PAYMENTS, name)

"""notifications table for portal notifications

照设计闸门 #471 v7（docs/design/AIH-TASK-125-portal-notifications.md）
§2「数据库」与 §8；本迁移是 AIH-TASK-125。只建一张新表 `notifications`
（10 列），不改任何已有表、不回填：

- `uq_notifications_public_id (public_id)`：客户侧唯一可见的标识；
- `uq_notifications_source_outbox (source_outbox_id)`：「一条 outbox 行
  最多一条通知」的数据库兜底（设计 §4）；**不建外键**到 `domain_outbox`
  （设计 §9 方案取舍 B）；
- `ix_notifications_tenant_occurred (tenant_id, source_outbox_id)`：
  列表按租户、记账顺序倒序翻页，未读数走它的前缀（名字沿用，不含
  `occurred_at`，设计 v7）；它同时是租户外键在 MySQL 上用的索引；
- `fk_notifications_tenant`：`tenant_id` → `tenants.id`，
  `ON DELETE RESTRICT`；
- 两条 CHECK：`ck_notifications_type`（类型只许三个值）、
  `ck_notifications_threshold`（`threshold` 非空当且仅当类型为
  `LOW_BALANCE`，写成两条分支的 OR，两库同义）。

没有写入方：通知由 AIH-TASK-126 的 outbox 投递写，接口属于 AIH-TASK-127。
`app/models/notifications.py` 的 `Notification` 同步声明了这些列、索引与
约束，`tests/backend/test_notifications_migration.py` 比对两边一致。

步骤
----

1. 用 inspector 判 `notifications` 表：不存在 → 待建；存在且形状与设计
   一致 → 跳过、不发 DDL；其余 → 形状不对，抛错，**不发任何 DDL**。
   「形状」逐项比（设计 §8）：恰好这 10 列（名称、类型与宽度、可空性）；
   恰好这三个索引（两个唯一、一个非唯一；名称、列与列顺序，没有前缀
   长度）；恰好这一个外键（列、引用表与列、`ON DELETE RESTRICT`，MySQL
   上的删除规则读 `information_schema`）；恰好这两条 CHECK（名称）。
   任何一项缺失、多出或不同都算形状不对。
2. MySQL：缺时发**一条** `CREATE TABLE notifications (…)`，索引、外键与
   CHECK 都写在这一条里；已建好时不发 DDL。其他方言：`create_table` 加
   `create_index`（DDL 在 Alembic 的迁移事务里）。

§132 第 13 条分析
-----------------

**已有数据**：只建新表，不改任何已有表的列、索引、约束与触发器，不回填，
不读写任何已有行。钱包与账本不被碰（INV-4、INV-5）。外键只指向 `tenants`。

**锁表与停机**：建新表不锁任何已有表的行；建外键时 MySQL 对 `tenants`
取短暂的元数据锁（等正在进行的、碰过 `tenants` 的事务结束），不扫行。
不需要停机。

**备份**：要求前置备份（与 0005–0027 同一口径）。本版不写任何数据。

**部署顺序**：`deploy.sh` 先迁移、再换镜像。本任务不带写入方与接口，
部署后只多一张没人读写的空表；旧代码不知道它。先迁移后换镜像、或反过来
都安全（设计 §8「兼容窗口」）。

**失败处理**：⚠️ MySQL 的 DDL **不参与事务**，`CREATE TABLE` 与写
`alembic_version` 之间有失败窗口（与 0026、0027 相同）。表连同索引、外键
与 CHECK 在**一条** `CREATE TABLE` 里建，MySQL 8 的 InnoDB DDL 是原子的，
加上第 1 步的检查，三个窗口都有确定的结局：

- `CREATE TABLE` 失败（锁等待超时、进程被杀）→ 整条回滚，表不存在，
  `alembic_version` 仍是 `0027_wallet_tx_created_index` → 原样重新部署；
- `CREATE TABLE` 成功、写 `alembic_version` 之前进程退出 → 表在，版本
  仍是 0027 → 原样重新部署：第 1 步判它形状正确，不发 DDL，只把版本前进；
- 有人手工建过同名但形状不同的表 → 重跑在第 1 步失败，报错写明这张表与
  处理方法，不发 DDL，版本仍是 0027 → 确认表是空的（126 上线之前没有
  写入方）后 `DROP TABLE notifications`，再重新部署。

`--sql` 离线模式读不了库：upgrade 跳过检查，按表不存在输出
`CREATE TABLE`（与 0027 相同）。

**回滚**：生产上的回滚**只回滚应用镜像，不降级 0028**（设计 §8）。
`downgrade` 只在表为空时删表：先 `SELECT 1 FROM notifications LIMIT 1`，
有任何一行就抛错（消息点名这张表、处理方法是保留表只回滚应用镜像），
不发任何 DDL、不删任何行 —— 已交付的通知（含已读状态）的来源 outbox 行
已是 `SENT`，删了无法重建；表不存在时什么都不做；表为空时只
`DROP TABLE notifications`，不碰任何已有表。`--sql` 离线模式判断不了表
是否为空：downgrade 直接抛错（须在线执行），不输出 `DROP TABLE`。

Revision ID: 0028_notifications
Revises: 0027_wallet_tx_created_index
Create Date: 2026-10-28
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0028_notifications"
down_revision: str | None = "0027_wallet_tx_created_index"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "notifications"
_TENANTS = "tenants"

# ⚠️ 下面都是冻结的字面量；模型那边的声明由
# tests/backend/test_notifications_migration.py 比对。

# 列名 → (类型与宽度, 可空)，按建表顺序。类型写成 MySQL 反射出来的样子
# （去掉空白）。
_COLUMNS: dict[str, tuple[str, bool]] = {
    "id": ("BIGINT", False),
    "public_id": ("CHAR(36)", False),
    "tenant_id": ("BIGINT", False),
    "notification_type": ("VARCHAR(32)", False),
    "source_outbox_id": ("BIGINT", False),
    "balance": ("DECIMAL(20,8)", False),
    "threshold": ("DECIMAL(20,8)", True),
    "occurred_at": ("DATETIME", False),
    "created_at": ("DATETIME", False),
    "read_at": ("DATETIME", True),
}

_UNIQUE_PUBLIC_ID = "uq_notifications_public_id"
_UNIQUE_SOURCE_OUTBOX = "uq_notifications_source_outbox"
_INDEX_TENANT = "ix_notifications_tenant_occurred"
# 索引名 → (列, 唯一)，按建的顺序。
_INDEXES: dict[str, tuple[tuple[str, ...], bool]] = {
    _UNIQUE_PUBLIC_ID: (("public_id",), True),
    _UNIQUE_SOURCE_OUTBOX: (("source_outbox_id",), True),
    _INDEX_TENANT: (("tenant_id", "source_outbox_id"), False),
}

_FK_TENANT = "fk_notifications_tenant"
# 外键名 → (列, 引用表, 引用列, 删除规则)。`source_outbox_id` 刻意没有外键
# （设计 §9 B）。
_FOREIGN_KEYS: dict[str, tuple[tuple[str, ...], str, tuple[str, ...], str]] = {
    _FK_TENANT: (("tenant_id",), _TENANTS, ("id",), "RESTRICT"),
}

# 名字 → 条件。与 app/models/notifications.py 上的 CheckConstraint 一致
# （空白除外）。
_CHECKS: dict[str, str] = {
    "ck_notifications_type": ("notification_type IN ('LOW_BALANCE', 'SUSPENDED', 'REACTIVATED')"),
    # (notification_type = 'LOW_BALANCE') = (threshold IS NOT NULL)，写成两条
    # 分支：布尔等式的写法两库不同义。
    "ck_notifications_threshold": (
        "(notification_type = 'LOW_BALANCE' AND threshold IS NOT NULL)"
        " OR (notification_type <> 'LOW_BALANCE' AND threshold IS NULL)"
    ),
}

# MySQL 上的建表语句：一条，原子。
_CREATE_TABLE_MYSQL = f"""
CREATE TABLE {_TABLE} (
    id BIGINT NOT NULL AUTO_INCREMENT,
    public_id CHAR(36) NOT NULL,
    tenant_id BIGINT NOT NULL,
    notification_type VARCHAR(32) NOT NULL,
    source_outbox_id BIGINT NOT NULL,
    balance DECIMAL(20, 8) NOT NULL,
    threshold DECIMAL(20, 8) NULL,
    occurred_at DATETIME NOT NULL,
    created_at DATETIME NOT NULL,
    read_at DATETIME NULL,
    PRIMARY KEY (id),
    CONSTRAINT {_UNIQUE_PUBLIC_ID} UNIQUE (public_id),
    CONSTRAINT {_UNIQUE_SOURCE_OUTBOX} UNIQUE (source_outbox_id),
    INDEX {_INDEX_TENANT} (tenant_id, source_outbox_id),
    CONSTRAINT {_FK_TENANT} FOREIGN KEY (tenant_id) REFERENCES {_TENANTS} (id)
        ON DELETE RESTRICT,
    CONSTRAINT ck_notifications_type CHECK ({_CHECKS["ck_notifications_type"]}),
    CONSTRAINT ck_notifications_threshold CHECK ({_CHECKS["ck_notifications_threshold"]})
)
"""

# MySQL 上外键的删除规则以 information_schema 为准（与 test_migrations.py
# 同一个来源）。
_DELETE_RULES_MYSQL = (
    "SELECT CONSTRAINT_NAME, DELETE_RULE FROM information_schema.REFERENTIAL_CONSTRAINTS"
    f" WHERE CONSTRAINT_SCHEMA = DATABASE() AND TABLE_NAME = '{_TABLE}'"
)

# 降级前的数据检查（设计 §8）：有任何一行就拒绝。
_HAS_ROWS = f"SELECT 1 FROM {_TABLE} LIMIT 1"


def _type_text(column_type: object) -> str:
    """`DECIMAL(20, 8) COLLATE …` → `DECIMAL(20,8)`：只留类型与宽度。"""
    return str(column_type).replace(", ", ",").split(" ")[0].upper()


def _reflected_shape() -> dict | None:
    """`notifications` 的现状（列、索引、外键、CHECK 名）；表不存在或离线
    模式读不了库时返回 None。"""
    if op.get_context().as_sql:
        return None
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

    if op.get_context().dialect.name == "mysql":
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


def upgrade() -> None:
    existing = _reflected_shape()
    # ⚠️ 先判、再发 DDL：形状不对时不动库，版本也不前进。
    if existing is not None:
        problems = _shape_problems(existing)
        if problems:
            raise RuntimeError(
                f"Table {_TABLE} exists but is not the one 0028 builds; it differs in "
                + "; ".join(problems)
                + ". No DDL was issued. The table has no writer before AIH-TASK-126:"
                f" check that it is empty, run DROP TABLE {_TABLE}, then upgrade again"
            )
        return

    if op.get_context().dialect.name == "mysql":
        op.execute(_CREATE_TABLE_MYSQL)
        return

    op.create_table(
        _TABLE,
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("public_id", sa.CHAR(length=36), nullable=False),
        sa.Column("tenant_id", sa.BigInteger(), nullable=False),
        sa.Column("notification_type", sa.String(length=32), nullable=False),
        sa.Column("source_outbox_id", sa.BigInteger(), nullable=False),
        sa.Column("balance", sa.DECIMAL(precision=20, scale=8), nullable=False),
        sa.Column("threshold", sa.DECIMAL(precision=20, scale=8), nullable=True),
        sa.Column("occurred_at", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("read_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("public_id", name=_UNIQUE_PUBLIC_ID),
        sa.UniqueConstraint("source_outbox_id", name=_UNIQUE_SOURCE_OUTBOX),
        sa.ForeignKeyConstraint(
            ["tenant_id"], [f"{_TENANTS}.id"], name=_FK_TENANT, ondelete="RESTRICT"
        ),
        *(sa.CheckConstraint(condition, name=name) for name, condition in _CHECKS.items()),
    )
    columns, _ = _INDEXES[_INDEX_TENANT]
    op.create_index(_INDEX_TENANT, _TABLE, list(columns), unique=False)


def downgrade() -> None:
    # ⚠️ 离线模式判断不了表是否为空：拒绝，不输出 DROP TABLE（设计 §8）。
    if op.get_context().as_sql:
        raise RuntimeError(
            f"Cannot downgrade 0028 in --sql offline mode: whether table {_TABLE} is empty"
            " can only be checked against the database. Run the downgrade online"
        )
    bind = op.get_bind()
    if not sa.inspect(bind).has_table(_TABLE):
        return
    # ⚠️ 在任何 DDL 之前：有通知就拒绝，删表等于删掉已交付的通知与已读状态。
    if bind.execute(sa.text(_HAS_ROWS)).first():
        raise RuntimeError(
            f"Cannot downgrade 0028: table {_TABLE} is not empty (its row count is non-zero:"
            " delivered notifications and their read state). No DDL was issued and no row"
            f" was deleted. Keep the table {_TABLE} and roll back only the application image"
        )
    op.drop_table(_TABLE)

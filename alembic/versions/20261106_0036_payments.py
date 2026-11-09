"""payments and payment_gateway_events tables

照设计闸门 #580 v3（docs/design/AIH-TASK-172-payments-gateway.md）§2「数据库」「幂等的三层」、
§3 INV-3、INV-6、INV-13 与 §8；本迁移是 AIH-TASK-172。只建两张新表 `payments`（22 列）与
`payment_gateway_events`（16 列），不改任何已有表、不回填：

- `payments`：
  - `uq_payments_public_id (public_id)`；`uq_payments_gateway_payment_id (gateway_payment_id)`
    与 `uq_payments_gateway_payment_intent_id (gateway_payment_intent_id)` 是幂等的第二层
    （一个 Stripe 付款对象只能对应一笔内部支付，spec §118）；
  - `ix_payments_tenant_created (tenant_id, created_at)`（同时是 `tenant_id` 外键用的索引）、
    `ix_payments_status_created (status, created_at)`、
    `ix_payments_reuse (tenant_id, status, expected_amount, created_at)`（60 秒同额复用的查找）、
    `ix_payments_review_required_at (review_required_at)`（告警的 24 小时窗口）；
  - `ix_payments_tax_policy_version_id (tax_policy_version_id)`：**显式声明**，供外键使用，所以
    MySQL 不另建一个模型里没有的隐式索引（与 0035 同一理由）；
  - 两个外键 `fk_payments_tenant_id` → `tenants.id`、
    `fk_payments_tax_policy_version_id` → `tax_policy_versions.id`，都是 `ON DELETE RESTRICT`；
  - 八条 CHECK：`gateway`、`expected_amount`（大于 0 且只有两位小数）、`expected_currency`、
    `wallet_credit_amount`（等于 `expected_amount`，ADR-0008）、`tax_amount`（为空）、`status`
    （六种）、`paid_fields`（`PAID` 时 `paid_at`、`confirmed_amount`、`confirmed_currency` 非空）、
    `review_at`（`REVIEW_REQUIRED` 时 `review_required_at` 非空）。
- `payment_gateway_events`：
  - `uq_payment_gateway_events_event (gateway, gateway_event_id)`：幂等的第一层（同一事件重复
    投递只落一行，ADR-0004）；
  - `ix_payment_gateway_events_due (processing_status, next_attempt_at)`（领取到期事件）、
    `ix_payment_gateway_events_payment_id (payment_id)`（外键用）、
    `ix_payment_gateway_events_received (received_at)`（告警查积压）；
  - 外键 `fk_payment_gateway_events_payment_id` → `payments.id`，`ON DELETE RESTRICT`；
  - 三条 CHECK：`gateway`、`verification_status`（只有 `VERIFIED`）、`processing_status`（七种）。

幂等的第三层（账本 `(PAYMENT, payments.public_id)` 唯一）是已有的
`uq_wallet_transactions_reference`，本迁移不碰。

没有写入方：`payments` 行由 AIH-TASK-176 的建单、174 的 `confirm_payment` 与 175 的认回会话写，
事件行由 AIH-TASK-175 写。`app/models/payments.py` 的 `Payment` 与 `PaymentGatewayEvent` 同步声明
了这些列、索引与约束，`tests/backend/test_payments_migration.py` 比对两边一致。

步骤
----

1. 用 inspector 判两张表是否存在（设计 §8 的简化写法：不做逐项形状比较，生产上只会执行一次）。
   任一已存在 → 抛错，点名那张表（两张都在就点名两张），**不发任何 DDL**。
2. 两张都不存在 → 先建 `payments`、再建 `payment_gateway_events`（后者的外键指向前者）。
   MySQL：每张表**一条** `CREATE TABLE (…)`，索引、外键与 CHECK 都写在这一条里。其他方言：
   `create_table` 加 `create_index`（DDL 在 Alembic 的迁移事务里）。

`--sql` 离线模式读不了库：跳过第 1 步，按「什么都没做过」输出两条 `CREATE TABLE`。

§132 第 13 条分析
-----------------

**已有数据**：只建两张新表，不改任何已有表的列、索引、约束与触发器，不回填，不读写任何已有行；
钱包与账本不被碰（INV-4、INV-5）。外键只指向 `tenants`、`tax_policy_versions`，以及事件表指向
本迁移新建的 `payments`。

**锁表与停机**：建新表不锁任何已有表的行；建外键时 MySQL 对 `tenants` 与 `tax_policy_versions`
各取短暂的元数据锁（等正在进行的、碰过它们的事务结束），不扫行。不需要停机。

**备份**：要求前置备份（与 0005–0035 同一口径）。本版不写任何数据。

**部署顺序**：`deploy.sh` 先迁移、再换镜像（设计 §8：合并即部署）。本任务不带写入方与接口，部署
后只多两张没人读写的空表；旧代码不知道它们。之后的适配器、服务、任务与接口属于 AIH-TASK-173..177，
开关默认关。

**失败处理**：⚠️ MySQL 的 DDL **不参与事务**，两条 `CREATE TABLE` 之间、以及它们与写
`alembic_version` 之间都有失败窗口。每张表连同索引、外键与 CHECK 在**一条** `CREATE TABLE` 里建，
MySQL 8 的 InnoDB DDL 是原子的，所以每个窗口都有确定的结局：

- 建 `payments` 失败（锁等待超时、进程被杀）→ 整条回滚，两张表都不存在，版本仍是
  `0035_monthly_statements` → 原样重新部署。
- `payments` 建成、建 `payment_gateway_events` 之前或之中中断 → `payments` 在、事件表不在，版本
  仍是 0035 → 重跑在第 1 步失败，报错点名 `payments`，不发 DDL。这期间没有写入方：先确认
  `payments` 里没有任何行，为空则 `DROP TABLE payments` 再重新部署；有行则停下，按数据事故处理，
  不要删。
- 两张都建成、写 `alembic_version` 之前进程退出 → 重跑在第 1 步失败，报错点名两张表 → 确认两张
  表都为空后先 `DROP TABLE payment_gateway_events` 再 `DROP TABLE payments`，再重新部署。
- 有人手工建过同名的表 → 同上，在第 1 步失败、不发 DDL；为空则手工删掉再重新部署，有行则按数据
  事故处理。

**回滚**：生产上的回滚是关开关（设计 §8），**不降级 0036**；已有支付与账本行保留。`downgrade`
**只删空表**：`--sql` 离线模式判断不了表是否为空，直接抛错（须在线执行），不输出任何 `DROP`；
不存在的表跳过；先对存在的每张表 `SELECT 1 … LIMIT 1`，任何一张有行就抛错（消息点名那张表），
不发任何 DDL、不删任何行 —— 支付与网关事件是财务记录，删了无法重建；都为空时先删
`payment_gateway_events`、再删 `payments`，不碰任何已有表。

Revision ID: 0036_payments
Revises: 0035_monthly_statements
Create Date: 2026-11-06
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0036_payments"
down_revision: str | None = "0035_monthly_statements"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PAYMENTS = "payments"
_EVENTS = "payment_gateway_events"
_TENANTS = "tenants"
_TAX_POLICY_VERSIONS = "tax_policy_versions"
# 建表顺序；降级按相反顺序删（事件表的外键指向 payments）。
_TABLES = (_PAYMENTS, _EVENTS)

# ⚠️ 下面都是冻结的字面量；模型那边的声明由 tests/backend/test_payments_migration.py 比对。

# 列名 → (类型与宽度, 可空)，按建表顺序。类型写成 MySQL 反射出来的样子（去掉空白）。
_PAYMENT_COLUMNS: dict[str, tuple[str, bool]] = {
    "id": ("BIGINT", False),
    "public_id": ("CHAR(36)", False),
    "tenant_id": ("BIGINT", False),
    "gateway": ("VARCHAR(16)", False),
    "expected_amount": ("DECIMAL(20,8)", False),
    "expected_currency": ("CHAR(3)", False),
    "wallet_credit_amount": ("DECIMAL(20,8)", False),
    "tax_policy_version_id": ("BIGINT", False),
    "tax_amount": ("DECIMAL(20,8)", True),
    "confirmed_amount": ("DECIMAL(20,8)", True),
    "confirmed_currency": ("CHAR(3)", True),
    "gateway_payment_id": ("VARCHAR(255)", True),
    "gateway_payment_intent_id": ("VARCHAR(255)", True),
    "gateway_fee": ("DECIMAL(20,8)", True),
    "status": ("VARCHAR(20)", False),
    "status_reason": ("VARCHAR(64)", True),
    "review_required_at": ("DATETIME", True),
    "expires_at": ("DATETIME", False),
    "paid_at": ("DATETIME", True),
    "last_reconciled_at": ("DATETIME", True),
    "created_at": ("DATETIME", False),
    "updated_at": ("DATETIME", False),
}
_EVENT_COLUMNS: dict[str, tuple[str, bool]] = {
    "id": ("BIGINT", False),
    "gateway": ("VARCHAR(16)", False),
    "gateway_event_id": ("VARCHAR(255)", False),
    "event_type": ("VARCHAR(64)", False),
    "payment_id": ("BIGINT", True),
    "payload_fingerprint": ("CHAR(64)", False),
    "verification_status": ("VARCHAR(16)", False),
    "processing_status": ("VARCHAR(16)", False),
    "attempts": ("INTEGER", False),
    "next_attempt_at": ("DATETIME", False),
    "lease_until": ("DATETIME", True),
    "last_error_code": ("VARCHAR(64)", True),
    "fingerprint_conflict_at": ("DATETIME", True),
    "received_at": ("DATETIME", False),
    "processed_at": ("DATETIME", True),
    "metadata_json": ("JSON", False),
}

# 索引名 → (列, 唯一)，按建的顺序。
_PAYMENT_INDEXES: dict[str, tuple[tuple[str, ...], bool]] = {
    "uq_payments_public_id": (("public_id",), True),
    "uq_payments_gateway_payment_id": (("gateway_payment_id",), True),
    "uq_payments_gateway_payment_intent_id": (("gateway_payment_intent_id",), True),
    "ix_payments_tenant_created": (("tenant_id", "created_at"), False),
    "ix_payments_status_created": (("status", "created_at"), False),
    "ix_payments_reuse": (("tenant_id", "status", "expected_amount", "created_at"), False),
    "ix_payments_tax_policy_version_id": (("tax_policy_version_id",), False),
    "ix_payments_review_required_at": (("review_required_at",), False),
}
_EVENT_INDEXES: dict[str, tuple[tuple[str, ...], bool]] = {
    "uq_payment_gateway_events_event": (("gateway", "gateway_event_id"), True),
    "ix_payment_gateway_events_due": (("processing_status", "next_attempt_at"), False),
    "ix_payment_gateway_events_payment_id": (("payment_id",), False),
    "ix_payment_gateway_events_received": (("received_at",), False),
}

# 外键名 → (列, 引用表, 引用列, 删除规则)。
_PAYMENT_FOREIGN_KEYS: dict[str, tuple[tuple[str, ...], str, tuple[str, ...], str]] = {
    "fk_payments_tenant_id": (("tenant_id",), _TENANTS, ("id",), "RESTRICT"),
    "fk_payments_tax_policy_version_id": (
        ("tax_policy_version_id",),
        _TAX_POLICY_VERSIONS,
        ("id",),
        "RESTRICT",
    ),
}
_EVENT_FOREIGN_KEYS: dict[str, tuple[tuple[str, ...], str, tuple[str, ...], str]] = {
    "fk_payment_gateway_events_payment_id": (("payment_id",), _PAYMENTS, ("id",), "RESTRICT"),
}

# 名字 → 条件。与 app/models/payments.py 上的 CheckConstraint 一致（空白除外）。
_PAYMENT_CHECKS: dict[str, str] = {
    "ck_payments_gateway": "gateway = 'STRIPE'",
    "ck_payments_expected_amount": (
        "expected_amount > 0 AND expected_amount = ROUND(expected_amount, 2)"
    ),
    "ck_payments_expected_currency": "expected_currency = 'MYR'",
    "ck_payments_wallet_credit_amount": "wallet_credit_amount = expected_amount",
    "ck_payments_tax_amount": "tax_amount IS NULL",
    "ck_payments_status": (
        "status IN ('PENDING', 'PAID', 'FAILED', 'EXPIRED', 'CANCELLED', 'REVIEW_REQUIRED')"
    ),
    "ck_payments_paid_fields": (
        "status <> 'PAID' OR (paid_at IS NOT NULL AND confirmed_amount IS NOT NULL"
        " AND confirmed_currency IS NOT NULL)"
    ),
    "ck_payments_review_at": "status <> 'REVIEW_REQUIRED' OR review_required_at IS NOT NULL",
}
_EVENT_CHECKS: dict[str, str] = {
    "ck_payment_gateway_events_gateway": "gateway = 'STRIPE'",
    "ck_payment_gateway_events_verification_status": "verification_status = 'VERIFIED'",
    "ck_payment_gateway_events_processing_status": (
        "processing_status IN ('RECEIVED', 'PROCESSING', 'RETRY', 'PROCESSED', 'IGNORED',"
        " 'UNMATCHED', 'REJECTED')"
    ),
}

# 表名 → (列, 索引, 外键, CHECK)。
_SHAPES: dict[
    str,
    tuple[
        dict[str, tuple[str, bool]],
        dict[str, tuple[tuple[str, ...], bool]],
        dict[str, tuple[tuple[str, ...], str, tuple[str, ...], str]],
        dict[str, str],
    ],
] = {
    _PAYMENTS: (_PAYMENT_COLUMNS, _PAYMENT_INDEXES, _PAYMENT_FOREIGN_KEYS, _PAYMENT_CHECKS),
    _EVENTS: (_EVENT_COLUMNS, _EVENT_INDEXES, _EVENT_FOREIGN_KEYS, _EVENT_CHECKS),
}


def _constraints_ddl(table: str) -> str:
    _, indexes, foreign_keys, checks = _SHAPES[table]
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
    return ",\n".join([*uniques, *plain, *keys, *conditions])


# MySQL 上的建表语句：每张表一条，原子。
_CREATE_PAYMENTS_MYSQL = f"""
CREATE TABLE {_PAYMENTS} (
    id BIGINT NOT NULL AUTO_INCREMENT,
    public_id CHAR(36) NOT NULL,
    tenant_id BIGINT NOT NULL,
    gateway VARCHAR(16) NOT NULL,
    expected_amount DECIMAL(20, 8) NOT NULL,
    expected_currency CHAR(3) NOT NULL,
    wallet_credit_amount DECIMAL(20, 8) NOT NULL,
    tax_policy_version_id BIGINT NOT NULL,
    tax_amount DECIMAL(20, 8) NULL,
    confirmed_amount DECIMAL(20, 8) NULL,
    confirmed_currency CHAR(3) NULL,
    gateway_payment_id VARCHAR(255) NULL,
    gateway_payment_intent_id VARCHAR(255) NULL,
    gateway_fee DECIMAL(20, 8) NULL,
    status VARCHAR(20) NOT NULL,
    status_reason VARCHAR(64) NULL,
    review_required_at DATETIME NULL,
    expires_at DATETIME NOT NULL,
    paid_at DATETIME NULL,
    last_reconciled_at DATETIME NULL,
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL,
    PRIMARY KEY (id),
{_constraints_ddl(_PAYMENTS)}
)
"""

_CREATE_EVENTS_MYSQL = f"""
CREATE TABLE {_EVENTS} (
    id BIGINT NOT NULL AUTO_INCREMENT,
    gateway VARCHAR(16) NOT NULL,
    gateway_event_id VARCHAR(255) NOT NULL,
    event_type VARCHAR(64) NOT NULL,
    payment_id BIGINT NULL,
    payload_fingerprint CHAR(64) NOT NULL,
    verification_status VARCHAR(16) NOT NULL,
    processing_status VARCHAR(16) NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at DATETIME NOT NULL,
    lease_until DATETIME NULL,
    last_error_code VARCHAR(64) NULL,
    fingerprint_conflict_at DATETIME NULL,
    received_at DATETIME NOT NULL,
    processed_at DATETIME NULL,
    metadata_json JSON NOT NULL,
    PRIMARY KEY (id),
{_constraints_ddl(_EVENTS)}
)
"""

_CREATE_TABLES_MYSQL: dict[str, str] = {
    _PAYMENTS: _CREATE_PAYMENTS_MYSQL,
    _EVENTS: _CREATE_EVENTS_MYSQL,
}


def _is_mysql() -> bool:
    return op.get_context().dialect.name == "mysql"


def _has_rows(table: str) -> str:
    # 降级前的数据检查（设计 §8）：有任何一行就拒绝。
    return f"SELECT 1 FROM {table} LIMIT 1"


def _column_type(kind: str) -> sa.types.TypeEngine:
    """`_*_COLUMNS` 里的类型文本 → 其他方言上 `create_table` 用的类型。"""
    if kind == "BIGINT":
        return sa.BigInteger()
    if kind == "INTEGER":
        return sa.Integer()
    if kind == "DATETIME":
        return sa.DateTime()
    if kind == "JSON":
        return sa.JSON()
    if kind == "DECIMAL(20,8)":
        return sa.Numeric(precision=20, scale=8)
    name, width = kind.rstrip(")").split("(")
    if name == "CHAR":
        return sa.CHAR(length=int(width))
    assert name == "VARCHAR", kind
    return sa.String(length=int(width))


def _create_table(table: str) -> None:
    if _is_mysql():
        op.execute(_CREATE_TABLES_MYSQL[table])
        return
    columns, indexes, foreign_keys, checks = _SHAPES[table]
    op.create_table(
        table,
        *(
            sa.Column(
                name,
                _column_type(kind),
                nullable=nullable,
                autoincrement=name == "id",
                server_default=sa.text("0") if name == "attempts" else None,
            )
            for name, (kind, nullable) in columns.items()
        ),
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
            for name, (key_columns, referred, referred_columns, rule) in foreign_keys.items()
        ),
        *(sa.CheckConstraint(condition, name=name) for name, condition in checks.items()),
    )
    for name, (index_columns, unique) in indexes.items():
        if not unique:
            op.create_index(name, table, list(index_columns), unique=False)


def upgrade() -> None:
    if op.get_context().as_sql:
        # 离线读不了库：按「什么都没做过」输出。
        for table in _TABLES:
            _create_table(table)
        return

    # 第 1 步。⚠️ 先判、再发 DDL：任一表已存在都不动库，版本也不前进。
    inspector = sa.inspect(op.get_bind())
    existing = [table for table in _TABLES if inspector.has_table(table)]
    if existing:
        raise RuntimeError(
            "Cannot upgrade to 0036: "
            + "; ".join(f"table {table} already exists" for table in existing)
            + ". No DDL was issued. 0036"
            " only creates new tables and does not compare shapes. First check that the table"
            " holds no row. If empty, drop it by hand (payment_gateway_events before payments)"
            " and upgrade again; if any row is there, stop and handle it as a data incident:"
            " do not drop it"
        )
    # 第 2 步：先 payments，再指向它的事件表。
    for table in _TABLES:
        _create_table(table)


def downgrade() -> None:
    # ⚠️ 离线模式判断不了表是否为空：拒绝，不输出任何 DROP（设计 §8）。
    if op.get_context().as_sql:
        raise RuntimeError(
            f"Cannot downgrade 0036 in --sql offline mode: whether tables {_PAYMENTS} and"
            f" {_EVENTS} are empty can only be checked against the database. Run the downgrade"
            " online"
        )
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    present = [table for table in _TABLES if inspector.has_table(table)]
    # ⚠️ 在任何 DDL 之前：任何一张有行就拒绝，删表等于删掉支付记录。
    for table in present:
        if bind.execute(sa.text(_has_rows(table))).first() is not None:
            raise RuntimeError(
                f"Cannot downgrade 0036: table {table} is not empty (payments and gateway events"
                " are financial records). No DDL was issued and no row was deleted. Keep the"
                " tables and turn payments off instead"
            )
    for table in reversed(_TABLES):
        if table in present:
            op.drop_table(table)

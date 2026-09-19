"""wallets + wallet_transactions, ledger triggers, tenants billing status

照设计闸门 #88 v6（docs/design/AIH-TASK-005-wallet-ledger.md）§2 与 §8。

步骤
----

0. **预检**：查 `@@log_bin` 与 `@@log_bin_trust_function_creators`。binlog 开着而
   开关关着时，应用账号建不了触发器（ERROR 1419），所以**在任何 DDL 之前**抛出
   明确的错误，`alembic_version` 仍是 0005。
1. `create_table wallets`
2. `create_table wallet_transactions`
3. 6 个触发器：账本的 BEFORE INSERT / AFTER INSERT / BEFORE UPDATE / BEFORE DELETE，
   钱包的 BEFORE INSERT / BEFORE UPDATE
4. `tenants` 加 `billing_status`（默认 SUSPENDED）、`status_version`（默认 0）、
   `low_balance_threshold`（可空）与对应的 CHECK
5. 给每个既有租户补一个余额为 0 的钱包（插入，不触发钱包的 UPDATE 触发器）

§132 第 13 条分析
-----------------

**锁表与停机**：两张新表的 DDL 瞬时完成。对既有表只 ALTER `tenants`（加三列与
三条 CHECK）：它在生产上是 0 行。回填只读 `tenants`。不需要停机。

**备份**：要求前置备份（与 0005 同一口径）。本版不改动既有数据。

**部署顺序**：`deploy.sh` 先迁移、再换镜像。旧代码不认识这两张表；新代码在本任务
里没有任何调用方。

**失败处理**：⚠️ MySQL 的 DDL **不参与事务**。
- 第 1 步建好、第 2 步失败：`wallets` 留在库里而 `alembic_version` 不前进。确认
  `alembic_version` 仍是 `0005_tenants_projects`、`wallets` 为空后 `DROP TABLE
  wallets`，再重新部署。
- 第 3、4、5 步失败：两张新表都已存在（触发器可能建了一部分，`tenants` 可能加了
  一部分列与 CHECK）。确认为空后按反向顺序删表（连带删掉表上的触发器），再删掉
  `tenants` 上已加的 CHECK 与列，然后重新部署。
- 只有第 5 步写数据，而且它可以重做。

**回滚**：`downgrade` 先删 `tenants` 的三条 CHECK 与三列，再按外键反向删两张表
（触发器随表删除）。⚠️ 一旦有任何真实记账，**生产上不要 downgrade 这一版**：
它会连同全部账本一起删掉。

**残余风险**：`TRUNCATE` / `DROP` 是 DDL，不经触发器（设计 §10）。

Revision ID: 0006_wallets_ledger
Revises: 0005_tenants_projects
Create Date: 2026-09-19
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0006_wallets_ledger"
down_revision: str | None = "0005_tenants_projects"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PREREQUISITE_QUERY = "SELECT @@log_bin, @@log_bin_trust_function_creators"
_PREREQUISITE_ERROR = (
    "MySQL has binary logging on and log_bin_trust_function_creators OFF, "
    + "so this account cannot create the wallet ledger triggers (ERROR 1419). "
    + "Set log_bin_trust_function_creators = ON first; nothing has been changed."
)

# 名字 → (表, 条件)。⚠️ 冻结的字面量：与 app/models 上的 CheckConstraint 逐条一致
# （空白除外），tests/backend/test_migrations.py 比对两边。
_CHECKS: dict[str, tuple[str, str]] = {
    "ck_wallets_currency": ("wallets", "currency = 'MYR'"),
    "ck_wallet_transactions_arithmetic": (
        "wallet_transactions",
        "balance_after = balance_before + amount",
    ),
    "ck_wallet_transactions_sign": (
        "wallet_transactions",
        """
        (transaction_type IN ('ADJUSTMENT_CREDIT', 'BONUS', 'REBILL_CREDIT', 'TOPUP')
            AND amount > 0)
        OR (transaction_type IN ('ADJUSTMENT_DEBIT', 'AI_USAGE', 'REBILL_DEBIT',
                'REFUND_ADJUSTMENT')
            AND amount < 0)
        OR (transaction_type IN ('SYSTEM_CORRECTION') AND amount <> 0)
        """,
    ),
    "ck_wallet_transactions_reference_type": (
        "wallet_transactions",
        """
        (reference_type = 'USAGE_EVENT' AND transaction_type IN ('AI_USAGE'))
        OR (reference_type = 'PAYMENT' AND transaction_type IN ('TOPUP'))
        OR (reference_type = 'ADMIN_ADJUSTMENT'
            AND transaction_type IN ('ADJUSTMENT_CREDIT', 'ADJUSTMENT_DEBIT', 'BONUS',
                'REFUND_ADJUSTMENT'))
        OR (reference_type = 'REBILL'
            AND transaction_type IN ('REBILL_CREDIT', 'REBILL_DEBIT'))
        OR (reference_type = 'SYSTEM' AND transaction_type IN ('SYSTEM_CORRECTION'))
        """,
    ),
    # ⚠️ `IS NOT NULL` 不能省：TRIM(NULL) 是 NULL，而 CHECK 把 NULL 当作「未违反」。
    "ck_wallet_transactions_reason": (
        "wallet_transactions",
        """
        reference_type NOT IN ('ADMIN_ADJUSTMENT', 'SYSTEM')
        OR (description IS NOT NULL AND TRIM(description) <> '')
        """,
    ),
    "ck_wallet_transactions_actor": (
        "wallet_transactions",
        "reference_type <> 'ADMIN_ADJUSTMENT' OR created_by IS NOT NULL",
    ),
    "ck_tenants_billing_status": ("tenants", "billing_status IN ('ACTIVE', 'SUSPENDED')"),
    "ck_tenants_status_version": ("tenants", "status_version >= 0"),
    "ck_tenants_low_balance_threshold": (
        "tenants",
        "low_balance_threshold IS NULL OR low_balance_threshold >= 0",
    ),
}

# 触发器名 → 建触发器的语句。⚠️ 整条 CREATE TRIGGER 是**一条**语句：DELIMITER 只是
# mysql 命令行客户端的东西，驱动把 BEGIN … END 原样发给服务器。
_TRIGGERS: dict[str, str] = {
    # 账本插入必须接在钱包当前状态之后：锁住钱包，校验租户、前余额与序号。
    # 没有它，运行账号可以插入孤立账本 —— 余额不变，却永久占住幂等键。
    "trg_wallet_transactions_before_insert": """
CREATE TRIGGER trg_wallet_transactions_before_insert
BEFORE INSERT ON wallet_transactions
FOR EACH ROW
BEGIN
    DECLARE wallet_tenant_id BIGINT DEFAULT NULL;
    DECLARE wallet_balance DECIMAL(20, 8) DEFAULT NULL;
    DECLARE wallet_version BIGINT DEFAULT NULL;
    SELECT tenant_id, balance, version
      INTO wallet_tenant_id, wallet_balance, wallet_version
      FROM wallets
     WHERE id = NEW.wallet_id
       FOR UPDATE;
    IF wallet_tenant_id IS NULL
       OR wallet_tenant_id <> NEW.tenant_id
       OR wallet_balance <> NEW.balance_before
       OR wallet_version + 1 <> NEW.wallet_sequence THEN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'ledger row does not extend the wallet';
    END IF;
END
""",
    # 插入账本的同一条语句里，钱包被推进到这一行的结果。repository 不直接 UPDATE 钱包。
    "trg_wallet_transactions_after_insert": """
CREATE TRIGGER trg_wallet_transactions_after_insert
AFTER INSERT ON wallet_transactions
FOR EACH ROW
UPDATE wallets
   SET balance = NEW.balance_after,
       version = NEW.wallet_sequence,
       updated_at = NEW.created_at
 WHERE id = NEW.wallet_id
""",
    # INV-5：账本只能插入。
    "trg_wallet_transactions_before_update": """
CREATE TRIGGER trg_wallet_transactions_before_update
BEFORE UPDATE ON wallet_transactions
FOR EACH ROW
SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'wallet_transactions is append-only'
""",
    "trg_wallet_transactions_before_delete": """
CREATE TRIGGER trg_wallet_transactions_before_delete
BEFORE DELETE ON wallet_transactions
FOR EACH ROW
SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'wallet_transactions is append-only'
""",
    # 新钱包只能从空开始：余额只能经账本出现，也挡住「删空钱包再带余额插回」。
    "trg_wallets_before_insert": """
CREATE TRIGGER trg_wallets_before_insert
BEFORE INSERT ON wallets
FOR EACH ROW
BEGIN
    IF NEW.balance <> 0 OR NEW.version <> 0 THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'a wallet starts empty';
    END IF;
END
""",
    # INV-4：余额或版本的每次变动都必须对应一行刚插入的账本。只有账本的
    # AFTER INSERT 触发器发出的更新满足这个条件。
    "trg_wallets_before_update": """
CREATE TRIGGER trg_wallets_before_update
BEFORE UPDATE ON wallets
FOR EACH ROW
BEGIN
    IF NEW.tenant_id <> OLD.tenant_id
       OR NEW.currency <> OLD.currency
       OR NEW.created_at <> OLD.created_at THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'wallet identity is immutable';
    END IF;
    IF (NEW.balance <> OLD.balance OR NEW.version <> OLD.version)
       AND (NEW.version <> OLD.version + 1
            OR NOT EXISTS (
                SELECT 1
                  FROM wallet_transactions
                 WHERE wallet_id = NEW.id
                   AND wallet_sequence = NEW.version
                   AND balance_before = OLD.balance
                   AND balance_after = NEW.balance)) THEN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'wallet balance moves only with a new ledger row';
    END IF;
END
""",
}

# 第 5 步。钱包的 BEFORE INSERT 触发器要求余额与版本为 0，这里插入的正是 0。
_BACKFILL = """
INSERT INTO wallets (tenant_id, currency, balance, version, created_at, updated_at)
SELECT id, 'MYR', 0, 0, UTC_TIMESTAMP(), UTC_TIMESTAMP()
  FROM tenants
"""


def _checks_for(table: str) -> list[sa.CheckConstraint]:
    return [
        sa.CheckConstraint(condition, name=name)
        for name, (owner, condition) in _CHECKS.items()
        if owner == table
    ]


def _require_trigger_privilege(bind: sa.engine.Connection) -> None:
    """Refuse to run where the migration account could not create triggers."""
    log_bin, trusted = bind.execute(sa.text(_PREREQUISITE_QUERY)).one()
    if int(log_bin) == 1 and int(trusted) != 1:
        raise RuntimeError(_PREREQUISITE_ERROR)


def upgrade() -> None:
    # 第 0 步。⚠️ 必须排在任何 DDL 之前：MySQL 的 DDL 不参与事务，建了一半的表
    # 要人工清场。`--sql` 离线模式不连库，也就没有可查的变量。
    if not op.get_context().as_sql:
        _require_trigger_privilege(op.get_bind())

    op.create_table(
        "wallets",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("tenant_id", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.CHAR(length=3), nullable=False),
        # 允许为负（spec §7 第 7–8 条）。
        sa.Column(
            "balance",
            sa.Numeric(precision=20, scale=8),
            nullable=False,
            server_default="0",
        ),
        sa.Column("version", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_wallets_tenant_id",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("tenant_id", name="uq_wallets_tenant_id"),
        sa.UniqueConstraint("id", "tenant_id", name="uq_wallets_id_tenant_id"),
        *_checks_for("wallets"),
    )

    op.create_table(
        "wallet_transactions",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("public_id", sa.CHAR(length=36), nullable=False),
        sa.Column("wallet_id", sa.BigInteger(), nullable=False),
        sa.Column("tenant_id", sa.BigInteger(), nullable=False),
        sa.Column("wallet_sequence", sa.BigInteger(), nullable=False),
        # 宽度写死，不让 Enum 按最长成员推（0004 文件头记着那个坑）。
        sa.Column("transaction_type", sa.String(length=64), nullable=False),
        sa.Column("amount", sa.Numeric(precision=20, scale=8), nullable=False),
        sa.Column("balance_before", sa.Numeric(precision=20, scale=8), nullable=False),
        sa.Column("balance_after", sa.Numeric(precision=20, scale=8), nullable=False),
        sa.Column("reference_type", sa.String(length=32), nullable=False),
        sa.Column("reference_id", sa.String(length=64), nullable=False),
        sa.Column("description", sa.String(length=255), nullable=True),
        sa.Column("metadata_json", sa.JSON(), nullable=True),
        sa.Column("created_by", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        # 复合外键：数据库保证账本行的租户与钱包一致（INV-8）。
        sa.ForeignKeyConstraint(
            ["wallet_id", "tenant_id"],
            ["wallets.id", "wallets.tenant_id"],
            name="fk_wallet_transactions_wallet",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name="fk_wallet_transactions_created_by",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("public_id", name="uq_wallet_transactions_public_id"),
        sa.UniqueConstraint(
            "wallet_id",
            "wallet_sequence",
            name="uq_wallet_transactions_wallet_sequence",
        ),
        # 每个财务来源至多一行。
        sa.UniqueConstraint(
            "reference_type",
            "reference_id",
            name="uq_wallet_transactions_reference",
        ),
        sa.Index("ix_wallet_transactions_tenant_sequence", "tenant_id", "wallet_sequence"),
        *_checks_for("wallet_transactions"),
    )

    for statement in _TRIGGERS.values():
        op.execute(statement)

    op.add_column(
        "tenants",
        sa.Column(
            "billing_status",
            sa.String(length=64),
            nullable=False,
            server_default="SUSPENDED",
        ),
    )
    op.add_column(
        "tenants",
        sa.Column("status_version", sa.BigInteger(), nullable=False, server_default="0"),
    )
    op.add_column(
        "tenants",
        sa.Column("low_balance_threshold", sa.Numeric(precision=20, scale=8), nullable=True),
    )
    for name, (owner, condition) in _CHECKS.items():
        if owner == "tenants":
            op.create_check_constraint(name, "tenants", condition)

    op.execute(_BACKFILL)


def downgrade() -> None:
    # 先删 tenants 的三条 CHECK 与三列（MySQL 不许删掉 CHECK 还在引用的列），
    # 再按外键反向删两张表。触发器随表删除。
    for name, (owner, _condition) in _CHECKS.items():
        if owner == "tenants":
            op.execute(f"ALTER TABLE tenants DROP CHECK {name}")
    op.drop_column("tenants", "low_balance_threshold")
    op.drop_column("tenants", "status_version")
    op.drop_column("tenants", "billing_status")
    op.drop_table("wallet_transactions")
    op.drop_table("wallets")

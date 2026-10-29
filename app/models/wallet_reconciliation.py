"""Wallet reconciliation runs and findings (design gate #474 v13, AIH-TASK-129).

spec §78（「verify `wallet.balance` against the ordered ledger and alert on any mismatch」）。
`wallet_reconciliation_runs` 一次核对运行一行；`wallet_reconciliation_findings` 一次运行里一个
**不一致**的钱包一行，一致的钱包不写行。

本任务只有数据库层：行由 AIH-TASK-131、132 的核对运行写，告警属于 AIH-TASK-133，管理端接口
属于 AIH-TASK-134。核对只读钱包与账本，这两张表是它唯一写的地方（INV-4、INV-5）。时间一律
不带时区的 UTC、取整到秒；`run_date` 是马来西亚日历日。

⚠️ 索引名、外键名与条件文本同迁移 0029 一致（空白除外），
tests/backend/test_wallet_reconciliation_migration.py 比对两边。
"""

from __future__ import annotations

import datetime as dt
import enum
from typing import Final

from sqlalchemy import (
    CHAR,
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    ForeignKeyConstraint,
    Index,
    Integer,
    SmallInteger,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

# 外键指向 tenants：import 这个模块，表才在同一个 metadata 里。
from app.models import tenancy as _tenancy_models  # noqa: F401
from app.models.base import Base, Money

# 与 app/models/wallet.py 同一个写法（SQLite 只对 INTEGER PRIMARY KEY 自增）。
_PrimaryKey = BigInteger().with_variant(Integer, "sqlite")
_ForeignKeyInt = BigInteger().with_variant(Integer, "sqlite")

_PUBLIC_ID_LENGTH = 36
# 设计 §2 写死的列宽。
RUN_STATUS_LENGTH: Final = 16
PROBLEM_CODES_LENGTH: Final = 255


class ReconciliationRunStatus(enum.StrEnum):
    """设计 §4 的状态机：`RUNNING → COMPLETED / ABANDONED`，结束后不再变。"""

    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    ABANDONED = "ABANDONED"


# ⚠️ 条件文本与迁移 0029 一致（空白除外）。「A = B」式的布尔等式写成两条分支
# 的 OR：布尔等式的写法两库不同义。
STATUS_CHECK: Final = "status IN ('RUNNING', 'COMPLETED', 'ABANDONED')"
# (status = 'RUNNING') = (finished_at IS NULL)
FINISHED_CHECK: Final = (
    "(status = 'RUNNING' AND finished_at IS NULL)"
    " OR (status <> 'RUNNING' AND finished_at IS NOT NULL)"
)
COUNTS_CHECK: Final = "wallets_inconsistent >= 0 AND wallets_inconsistent <= wallets_checked"
# (lease_owner IS NULL) = (lease_until IS NULL)
LEASE_CHECK: Final = (
    "(lease_owner IS NULL AND lease_until IS NULL)"
    " OR (lease_owner IS NOT NULL AND lease_until IS NOT NULL)"
)
# ⚠️ `running_slot IS NOT NULL` 不能省：NULL = 1 是 UNKNOWN，少了它「RUNNING 且 NULL」
# 整条得 UNKNOWN，CHECK 只拒绝 FALSE，会放行（设计 §2）。
RUNNING_SLOT_CHECK: Final = (
    "(status = 'RUNNING' AND running_slot IS NOT NULL AND running_slot = 1)"
    " OR (status <> 'RUNNING' AND running_slot IS NULL)"
)
CODES_CHECK: Final = "problem_codes <> ''"


class WalletReconciliationRun(Base):
    """One reconciliation run (设计 §2「数据库」).

    同一时刻至多一条 `RUNNING`：`running_slot` 在 `RUNNING` 时恒为 1、其余为 NULL，唯一约束由
    数据库保证。`run_date` 不唯一：被收尾的 `ABANDONED` 行不挡住当天再开始。
    """

    __tablename__ = "wallet_reconciliation_runs"

    id: Mapped[int] = mapped_column(_PrimaryKey, primary_key=True, autoincrement=True)
    # uuid4；管理端接口里的 `run_id`。
    public_id: Mapped[str] = mapped_column(CHAR(_PUBLIC_ID_LENGTH), nullable=False)
    # 运行所属的马来西亚日历日（`kuala_lumpur_date(started_at)`）。
    run_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    status: Mapped[ReconciliationRunStatus] = mapped_column(
        Enum(ReconciliationRunStatus, native_enum=False, length=RUN_STATUS_LENGTH),
        nullable=False,
    )
    started_at: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False)
    # `COMPLETED` / `ABANDONED` 的时刻；`RUNNING` 时为 NULL。
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    # 已核对到的最大 `tenants.id`；新运行为 0。不建外键：它是游标，不是引用。
    # 三列计数写入方显式给值，缺省值只是兜底（设计 §2）。
    cursor_tenant_id: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    # 已核对的租户数（每个租户一个钱包）。
    wallets_checked: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    # 其中有问题码的租户数。
    wallets_inconsistent: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    # 当前持有推进权的 worker 令牌（每次 step 新生成的 uuid4）与租约到期时刻；
    # 两者同为 NULL 或同非 NULL。
    lease_owner: Mapped[str | None] = mapped_column(CHAR(_PUBLIC_ID_LENGTH), nullable=True)
    lease_until: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    # `RUNNING` 时恒为 1，其余状态恒为 NULL。
    running_slot: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)

    __table_args__ = (
        UniqueConstraint("public_id", name="uq_wallet_reconciliation_runs_public_id"),
        # 同一时刻至多一条 RUNNING（NULL 可有多行）。
        UniqueConstraint("running_slot", name="uq_wallet_reconciliation_runs_running_slot"),
        # 普通索引，不唯一。
        Index("ix_wallet_reconciliation_runs_run_date", "run_date"),
        # 告警取最近一次 COMPLETED。
        Index("ix_wallet_reconciliation_runs_status_finished", "status", "finished_at"),
        CheckConstraint(STATUS_CHECK, name="ck_wallet_reconciliation_runs_status"),
        CheckConstraint(FINISHED_CHECK, name="ck_wallet_reconciliation_runs_finished"),
        CheckConstraint(COUNTS_CHECK, name="ck_wallet_reconciliation_runs_counts"),
        CheckConstraint(LEASE_CHECK, name="ck_wallet_reconciliation_runs_lease"),
        CheckConstraint(RUNNING_SLOT_CHECK, name="ck_wallet_reconciliation_runs_running_slot"),
    )


class WalletReconciliationFinding(Base):
    """One inconsistent wallet in one run (设计 §2「数据库」).

    一致的钱包不写行。值都取自这个钱包自己的快照事务。
    """

    __tablename__ = "wallet_reconciliation_findings"

    id: Mapped[int] = mapped_column(_PrimaryKey, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(_ForeignKeyInt, nullable=False)
    tenant_id: Mapped[int] = mapped_column(_ForeignKeyInt, nullable=False)
    # 问题码按 `ledger_problems` 的输出顺序以英文逗号连接（`WALLET_MISSING` 单独一个）。
    problem_codes: Mapped[str] = mapped_column(String(PROBLEM_CODES_LENGTH), nullable=False)
    # 快照里的 `wallets.balance` 与 `wallets.version`；钱包不存在时为 NULL。
    balance: Mapped[Money | None] = mapped_column(nullable=True)
    wallet_version: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # 快照里读到的账本行数。
    ledger_rows: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # 这个钱包的快照事务开始前取的时刻。
    checked_at: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False)

    __table_args__ = (
        # 一次运行一个租户至多一行；也是按运行列出与 run_id 外键用的索引。
        UniqueConstraint(
            "run_id", "tenant_id", name="uq_wallet_reconciliation_findings_run_tenant"
        ),
        # 显式声明：tenant_id 外键用它，MySQL 不另建隐式索引。
        Index("ix_wallet_reconciliation_findings_tenant", "tenant_id"),
        # RESTRICT：删运行或删租户不许连带删掉记录。
        ForeignKeyConstraint(
            ["run_id"],
            ["wallet_reconciliation_runs.id"],
            name="fk_wallet_reconciliation_findings_run",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_wallet_reconciliation_findings_tenant",
            ondelete="RESTRICT",
        ),
        CheckConstraint(CODES_CHECK, name="ck_wallet_reconciliation_findings_codes"),
    )


__all__ = [
    "CODES_CHECK",
    "COUNTS_CHECK",
    "FINISHED_CHECK",
    "LEASE_CHECK",
    "PROBLEM_CODES_LENGTH",
    "RUNNING_SLOT_CHECK",
    "RUN_STATUS_LENGTH",
    "STATUS_CHECK",
    "ReconciliationRunStatus",
    "WalletReconciliationFinding",
    "WalletReconciliationRun",
]

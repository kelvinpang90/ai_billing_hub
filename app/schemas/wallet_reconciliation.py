"""Admin views of wallet reconciliation runs and findings (AIH-TASK-134).

设计闸门 #474 v13 §2「管理端接口」。

⚠️ **两个视图都是字段白名单**：运行恰好 7 个键，不一致恰好 7 个键。
刻意不继承、不复用别的模型：别处以后加字段，这里不会跟着出现。
内部自增 `id`、`run_id` / `tenant_id` 外键、游标、租约与 `running_slot`
一律不出这里；运行与客户对外都只用 `public_id`。

⚠️ `balance` 是恰好 8 位小数的字符串（`money_text`），原样来自
`DECIMAL(20,8)` 列，不经 float（INV-10）；钱包不存在（`WALLET_MISSING`）时
与 `wallet_version` 同为 `null`。时间与管理端审计列表的时间字段
同一个类型、同一种格式：不带时区的 UTC。

⚠️ 路径参数 `run_id` 只收小写规范 UUID 形状（8-4-4-4-12）。形状在鉴权之后判，
不合规与查不到是同一个 404 `RECONCILIATION_RUN_NOT_FOUND`。
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Final

from pydantic import BaseModel

from app.core.errors import AppError
from app.models.wallet_reconciliation import (
    ReconciliationRunStatus,
    WalletReconciliationFinding,
    WalletReconciliationRun,
)
from app.schemas.customers import money_text

_RUN_ID: Final = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
# 存储时以英文逗号连接（设计 §2「数据库」）。
_CODE_SEPARATOR: Final = ","


class ReconciliationRunNotFound(AppError):
    """No such reconciliation run (absent, or not a lowercase canonical uuid)."""

    def __init__(self) -> None:
        super().__init__(
            "Reconciliation run not found.",
            code="RECONCILIATION_RUN_NOT_FOUND",
            http_status=404,
        )


class WalletReconciliationRunView(BaseModel):
    """One reconciliation run as an admin sees it. `run_id` is the run's `public_id`."""

    run_id: str
    # 运行所属的马来西亚日历日，`YYYY-MM-DD`。
    run_date: dt.date
    # `RUNNING` / `COMPLETED` / `ABANDONED`。
    status: str
    started_at: dt.datetime
    # `RUNNING` 时为 null。
    finished_at: dt.datetime | None
    wallets_checked: int
    wallets_inconsistent: int


class WalletReconciliationFindingView(BaseModel):
    """One inconsistent wallet in one run. `customer_id` is the tenant's `public_id`."""

    customer_id: str
    company_name: str
    # 按存储顺序（即 `ledger_problems` 的输出顺序）拆开。
    problem_codes: list[str]
    # 快照里的余额与版本；钱包不存在时都是 null。
    balance: str | None
    wallet_version: int | None
    ledger_rows: int
    checked_at: dt.datetime


def is_run_id(value: str) -> bool:
    """A lowercase canonical uuid (36 characters, 8-4-4-4-12)."""
    return _RUN_ID.fullmatch(value) is not None


def reconciliation_run_view(run: WalletReconciliationRun) -> WalletReconciliationRunView:
    return WalletReconciliationRunView(
        run_id=run.public_id,
        run_date=run.run_date,
        status=ReconciliationRunStatus(run.status).value,
        started_at=run.started_at,
        finished_at=run.finished_at,
        wallets_checked=run.wallets_checked,
        wallets_inconsistent=run.wallets_inconsistent,
    )


def reconciliation_finding_view(
    finding: WalletReconciliationFinding, *, customer_id: str, company_name: str
) -> WalletReconciliationFindingView:
    balance = finding.balance
    return WalletReconciliationFindingView(
        customer_id=customer_id,
        company_name=company_name,
        problem_codes=finding.problem_codes.split(_CODE_SEPARATOR),
        balance=None if balance is None else money_text(balance),
        wallet_version=finding.wallet_version,
        ledger_rows=finding.ledger_rows,
        checked_at=finding.checked_at,
    )


__all__ = [
    "ReconciliationRunNotFound",
    "WalletReconciliationFindingView",
    "WalletReconciliationRunView",
    "is_run_id",
    "reconciliation_finding_view",
    "reconciliation_run_view",
]

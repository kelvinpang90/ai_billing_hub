"""Wallet reconciliation runs: steps 0–2 (design gate #474 v13 §2「核对运行」, AIH-TASK-131).

本模块目前只有三个步骤函数与租约的尽力释放，**没有调用方**：扫描、续租、进度提交、`step`
主流程、Celery 任务与 beat 属于 AIH-TASK-132。

- 第 0 步 `retire_stale_runs`：日期不是今天的 `RUNNING`（早于或晚于）一律收尾为 `ABANDONED`；
- 第 1 步 `start_run`：按开始规则插入新运行，单独一个事务只做这一条 INSERT；
- 第 2 步 `claim_run`：读进行中的运行、领租约，领到之后再读游标。

三个函数都接收调用方传入的同一个 `now`（不带时区的 UTC，取整到秒），各自只开自己的短事务；
数据库异常回滚自己的事务后原样抛出，由 AIH-TASK-132 的 `step` 统一记
`wallet_reconciliation_failed`。只读钱包与账本——本模块不碰它们，只写运行表（INV-4、INV-5）。
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from typing import Final, Literal, NamedTuple

from sqlalchemy import insert, or_, select, update
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.core.fx_source import KUALA_LUMPUR_OFFSET, kuala_lumpur_date
from app.models.wallet_reconciliation import ReconciliationRunStatus, WalletReconciliationRun

logger = logging.getLogger(__name__)

# 设计 §2「核对运行」的常量。
STEP_SECONDS: Final = 300  # beat 周期
STEP_BUDGET_SECONDS: Final = 20
TENANT_BATCH: Final = 50
START_HOUR_MYT: Final = 3
STALE_AFTER: Final = dt.timedelta(hours=30)
LEASE_SECONDS: Final = 290
LEASE_RENEW_AFTER_SECONDS: Final = 60

# MySQL 错误号：开始阶段把它们与 IntegrityError 一样当作并发插入的正常结果
# （设计 §2 第 1 步）。
_ER_LOCK_WAIT_TIMEOUT: Final = 1205
_ER_LOCK_DEADLOCK: Final = 1213

_RUNNING = ReconciliationRunStatus.RUNNING
_Run = WalletReconciliationRun

StartResult = Literal["started", "already_running", "not_due"]


class ClaimedRun(NamedTuple):
    """The run this step holds the lease on, and the cursor read after taking it."""

    run_id: int
    public_id: str
    token: str
    old_cursor: int


class ClaimError(Exception):
    """`claim_run` failed after it had read the run; the original error is `__cause__`.

    带着这次运行的 `public_id`，`step` 据此记 `run_id`（设计 §2 第 5 步）。
    """

    def __init__(self, public_id: str) -> None:
        super().__init__(public_id)
        self.public_id = public_id


def retire_stale_runs(factory: sessionmaker[Session], *, now: dt.datetime) -> int:
    """Step 0: mark every `RUNNING` whose `run_date` is not today `ABANDONED`.

    普通读（不加锁）出 id，再按主键逐行更新（不按范围，避免间隙锁），一个事务提交。
    返回这次真正收尾的行数（已被别的 worker 收尾的行受影响 0 行，不计）。
    """
    today = kuala_lumpur_date(now)
    with factory() as session:
        stale_ids = session.scalars(
            select(_Run.id).where(_Run.status == _RUNNING, _Run.run_date != today).order_by(_Run.id)
        ).all()
        retired = 0
        for run_id in stale_ids:
            result = session.execute(
                update(_Run)
                .where(_Run.id == run_id, _Run.status == _RUNNING)
                .values(
                    status=ReconciliationRunStatus.ABANDONED,
                    finished_at=now,
                    running_slot=None,
                    lease_owner=None,
                    lease_until=None,
                )
            )
            retired += result.rowcount
        session.commit()
    return retired


def start_run(factory: sessionmaker[Session], *, now: dt.datetime) -> StartResult:
    """Step 1: insert today's run when the start rule allows it.

    开始规则：今天没有 `COMPLETED`（同日的 `ABANDONED` 不算）、当前没有任何
    `RUNNING`，且马来西亚时间不早于 03:00 或表为空。只要存在 `RUNNING`（插入前发现，
    或插入失败后的锁读发现）就返回 `already_running`；`not_due` 只用于时间未到或
    今天已有 `COMPLETED`。
    """
    today = kuala_lumpur_date(now)
    with factory() as session:
        running = session.scalar(select(_Run.id).where(_Run.status == _RUNNING).limit(1))
        completed_today = session.scalar(
            select(_Run.id)
            .where(_Run.run_date == today, _Run.status == ReconciliationRunStatus.COMPLETED)
            .limit(1)
        )
        empty = session.scalar(select(_Run.id).limit(1)) is None
    if running is not None:
        return "already_running"
    if completed_today is not None:
        return "not_due"
    if not empty and (now + KUALA_LUMPUR_OFFSET).hour < START_HOUR_MYT:
        return "not_due"

    try:
        with factory() as session:
            session.execute(
                insert(_Run).values(
                    public_id=str(uuid.uuid4()),
                    run_date=today,
                    status=_RUNNING,
                    started_at=now,
                    finished_at=None,
                    cursor_tenant_id=0,
                    wallets_checked=0,
                    wallets_inconsistent=0,
                    lease_owner=None,
                    lease_until=None,
                    running_slot=1,
                )
            )
            session.commit()
    except DBAPIError as error:
        if not _is_start_conflict(error):
            raise
        # 已回滚（会话关闭）。锁读会等正在插入的一方提交或回滚，读到最新已提交的值；普通读在
        # REPEATABLE READ 下可能看不到对方刚插入的行。
        with factory() as session:
            holder = session.scalar(
                select(_Run.status).where(_Run.running_slot == 1).with_for_update(read=True)
            )
        if holder != _RUNNING:
            raise
        return "already_running"
    return "started"


def claim_run(
    factory: sessionmaker[Session], *, now: dt.datetime
) -> ClaimedRun | Literal["idle", "busy"]:
    """Step 2: read the run in progress, take its lease, then read its cursor.

    `idle`：没有可推进的运行；`busy`：另一个 worker 持有未到期的租约（或运行刚结束）。读到运行
    之后的失败包成 `ClaimError`（已领到租约时先尽力释放）；读到运行之前的失败原样抛出。
    """
    today = kuala_lumpur_date(now)
    with factory() as session:
        # 通常至多一行；多行时取 run_date 最大的，其余留给第 0 步。这里不读游标。
        found = session.execute(
            select(_Run.id, _Run.public_id)
            .where(_Run.status == _RUNNING, _Run.run_date <= today)
            .order_by(_Run.run_date.desc(), _Run.id.desc())
            .limit(1)
        ).first()
    if found is None:
        return "idle"
    run_id, public_id = found

    token = str(uuid.uuid4())
    claimed = False
    try:
        with factory() as session:
            result = session.execute(
                update(_Run)
                .where(
                    _Run.id == run_id,
                    _Run.status == _RUNNING,
                    or_(_Run.lease_until.is_(None), _Run.lease_until < now),
                )
                .values(
                    lease_owner=token,
                    lease_until=now + dt.timedelta(seconds=LEASE_SECONDS),
                )
            )
            # 匹配到了就当作可能已领到：提交本身失败时释放也无害（条件带令牌）。
            claimed = result.rowcount == 1
            session.commit()
        if not claimed:
            return "busy"
        # 上一个持有者只在提交进度的同一条 UPDATE 里释放租约，
        # 所以这里读到的是最新提交的游标。
        with factory() as session:
            old_cursor = session.scalars(
                select(_Run.cursor_tenant_id).where(_Run.id == run_id)
            ).one()
    except Exception as error:
        if claimed:
            release_lease(factory, run_id=run_id, public_id=public_id, token=token)
        raise ClaimError(public_id) from error
    return ClaimedRun(run_id, public_id, token, old_cursor)


def release_lease(
    factory: sessionmaker[Session], *, run_id: int, public_id: str, token: str
) -> None:
    """Best-effort release of a lease this step holds (设计 §2 第 5 步的释放语句).

    新会话执行；失败时记一条 warning、不重试、不向外抛——释放不了就等租约自然过期。
    """
    try:
        with factory() as session:
            session.execute(
                update(_Run)
                .where(_Run.id == run_id, _Run.lease_owner == token)
                .values(lease_owner=None, lease_until=None)
            )
            session.commit()
    except Exception as error:
        logger.warning(
            "wallet_reconciliation_lease_release_failed",
            extra={"run_id": public_id, "error_type": type(error).__name__},
        )


def _is_start_conflict(error: DBAPIError) -> bool:
    """Unique conflict, deadlock (1213) or lock wait timeout (1205) on the start INSERT."""
    if isinstance(error, IntegrityError):
        return True
    arguments = getattr(error.orig, "args", ()) if error.orig is not None else ()
    number = arguments[0] if arguments else None
    return number in (_ER_LOCK_DEADLOCK, _ER_LOCK_WAIT_TIMEOUT)


__all__ = [
    "LEASE_RENEW_AFTER_SECONDS",
    "LEASE_SECONDS",
    "STALE_AFTER",
    "START_HOUR_MYT",
    "STEP_BUDGET_SECONDS",
    "STEP_SECONDS",
    "TENANT_BATCH",
    "ClaimError",
    "ClaimedRun",
    "StartResult",
    "claim_run",
    "release_lease",
    "retire_stale_runs",
    "start_run",
]

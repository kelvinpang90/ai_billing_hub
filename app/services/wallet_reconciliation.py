"""Wallet reconciliation runs (design gate #474 v13 §2「核对运行」, AIH-TASK-131, AIH-TASK-132).

- 第 0 步 `retire_stale_runs`：日期不是今天的 `RUNNING`（早于或晚于）一律收尾为 `ABANDONED`；
- 第 1 步 `start_run`：按开始规则插入新运行，单独一个事务只做这一条 INSERT；
- 第 2 步 `claim_run`：读进行中的运行、领租约，领到之后再读游标；
- 第 3 步：从游标之后逐个租户，每个钱包一个只读快照事务，期间按需续租，预算 20 秒；
- 第 4 步：带租约与游标的进度 CAS 与这一段的 findings 同一事务提交（INV-13）；
- 第 5 步：任何异常回滚、记一条 `wallet_reconciliation_failed`，持有租约时尽力释放。

`step` 把它们串起来，由 Celery 任务 `app.tasks.wallet_reconciliation.step` 每 300 秒触发一次。
第 0–2 步的三个函数接收 `step` 传入的同一个 `now`（不带时区的 UTC，取整到秒），各自只开自己的
短事务；数据库异常回滚自己的事务后原样抛出，由 `step` 统一记日志。扫描期间不保持任何事务打开。
只读钱包与账本——本模块不碰它们，只写运行表与 findings（INV-4、INV-5）。
"""

from __future__ import annotations

import datetime as dt
import logging
import time
import uuid
from collections.abc import Callable
from decimal import Decimal
from typing import Final, Literal, NamedTuple

from sqlalchemy import insert, or_, select, update
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.core.fx_source import KUALA_LUMPUR_OFFSET, kuala_lumpur_date
from app.models.tenancy import Tenant
from app.models.wallet_reconciliation import (
    ReconciliationRunStatus,
    WalletReconciliationFinding,
    WalletReconciliationRun,
)
from app.repositories.wallet import check_wallet_snapshot
from app.services.auth import utc_now

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
StepResult = Literal["idle", "busy", "progressed", "completed", "superseded", "error"]
Stage = Literal["retire", "start", "advance", "commit"]


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


class _Finding(NamedTuple):
    """One inconsistent wallet of this segment, written only if the progress CAS holds."""

    tenant_id: int
    problem_codes: str
    balance: Decimal | None
    wallet_version: int | None
    ledger_rows: int
    checked_at: dt.datetime


class _Segment(NamedTuple):
    """What step 3 scanned: new cursor, count, findings, and whether it reached the end."""

    cursor: int
    checked: int
    findings: list[_Finding]
    at_end: bool


class _LeaseLost(Exception):
    """A renewal touched no row: the lease expired and was taken over, or the run was retired."""


def step(
    factory: sessionmaker[Session],
    *,
    clock: Callable[[], dt.datetime] = utc_now,
    monotonic: Callable[[], float] = time.monotonic,
) -> StepResult:
    """One beat of the reconciliation: steps 0–4, with step 5 for any failure.

    `now`（与由它算出的今天）只在开头读一次，第 0、1、2 步共用；第 3 步每个钱包的 `checked_at`
    与续租时刻、第 4 步的 `commit_now` 另读。`monotonic` 只用于 20 秒预算与续租间隔。
    不向外抛：失败返回 `error`。
    """
    now = _whole_seconds(clock())
    try:
        retire_stale_runs(factory, now=now)
    except Exception as error:
        # 收尾失败则这次不开始也不推进：不会出现新旧两条 RUNNING 并存。
        _log_failure("retire", None, error)
        return "error"
    try:
        start_run(factory, now=now)
    except Exception as error:
        _log_failure("start", None, error)
        return "error"
    try:
        claimed = claim_run(factory, now=now)
    except ClaimError as error:
        # claim_run 已尽力释放租约；类名记原异常的，不记包装。
        cause = error.__cause__ if error.__cause__ is not None else error
        _log_failure("advance", error.public_id, cause)
        return "error"
    except Exception as error:
        # 读到运行之前就失败：还不知道是哪次运行。
        _log_failure("advance", None, error)
        return "error"
    if not isinstance(claimed, ClaimedRun):
        return claimed

    # 从这里起本次 step 持有租约：凡不是以进度提交结束的，返回前都尽力释放。
    try:
        segment = _scan(factory, claimed, clock=clock, monotonic=monotonic)
    except _LeaseLost:
        _release(factory, claimed)
        return "superseded"
    except Exception as error:
        _log_failure("advance", claimed.public_id, error)
        _release(factory, claimed)
        return "error"
    try:
        committed = _commit_progress(factory, claimed, segment, clock=clock)
    except Exception as error:
        _log_failure("commit", claimed.public_id, error)
        _release(factory, claimed)
        return "error"
    if not committed:
        _release(factory, claimed)
        return "superseded"
    return "completed" if segment.at_end else "progressed"


def _scan(
    factory: sessionmaker[Session],
    claimed: ClaimedRun,
    *,
    clock: Callable[[], dt.datetime],
    monotonic: Callable[[], float],
) -> _Segment:
    """Step 3: check wallet after wallet from the cursor until the budget runs out or the end.

    每批租户 id 一个短的只读事务；每个钱包一个新会话的只读事务（一致性快照，结束时回滚）。
    预算只在租户之间检查；续租在每读完一块与每核对完一个钱包时按需进行。租约已丢时抛
    `_LeaseLost`，这一段什么都不提交。
    """
    started = monotonic()
    deadline = started + STEP_BUDGET_SECONDS
    last_lease = started

    def keep_lease(moment: float | None = None) -> None:
        nonlocal last_lease
        if moment is None:
            moment = monotonic()
        if moment - last_lease <= LEASE_RENEW_AFTER_SECONDS:
            return
        if not _renew_lease(factory, claimed, clock=clock):
            raise _LeaseLost
        last_lease = moment

    cursor = claimed.old_cursor
    checked = 0
    findings: list[_Finding] = []
    while True:
        with factory() as session:
            tenant_ids = session.scalars(
                select(Tenant.id).where(Tenant.id > cursor).order_by(Tenant.id).limit(TENANT_BATCH)
            ).all()
            session.rollback()
        if not tenant_ids:
            return _Segment(cursor, checked, findings, at_end=True)
        for tenant_id in tenant_ids:
            checked_at = _whole_seconds(clock())
            with factory() as session:
                result = check_wallet_snapshot(session, tenant_id, on_chunk=keep_lease)
                session.rollback()
            if result.problem_codes:
                findings.append(
                    _Finding(
                        tenant_id=tenant_id,
                        problem_codes=",".join(result.problem_codes),
                        balance=result.balance,
                        wallet_version=result.version,
                        ledger_rows=result.ledger_rows,
                        checked_at=checked_at,
                    )
                )
            cursor = tenant_id
            checked += 1
            moment = monotonic()
            keep_lease(moment)
            if moment >= deadline:
                return _Segment(cursor, checked, findings, at_end=False)


def _renew_lease(
    factory: sessionmaker[Session], claimed: ClaimedRun, *, clock: Callable[[], dt.datetime]
) -> bool:
    """Push `lease_until` to now + 290 seconds while the lease is still ours; False if lost.

    单独一个短事务、与快照读不同的会话，不打断快照。
    """
    renewed_until = _whole_seconds(clock()) + dt.timedelta(seconds=LEASE_SECONDS)
    with factory() as session:
        result = session.execute(
            update(_Run)
            .where(
                _Run.id == claimed.run_id,
                _Run.lease_owner == claimed.token,
                _Run.status == _RUNNING,
            )
            .values(lease_until=renewed_until)
        )
        renewed = result.rowcount == 1
        session.commit()
    return renewed


def _commit_progress(
    factory: sessionmaker[Session],
    claimed: ClaimedRun,
    segment: _Segment,
    *,
    clock: Callable[[], dt.datetime],
) -> bool:
    """Step 4: the progress CAS and the segment's findings in one transaction (INV-13).

    CAS 带 `status = RUNNING`、旧游标与本次令牌，提交即释放租约；到末尾时同一条置 `COMPLETED`。
    受影响 1 行才插入 findings 并提交；否则回滚、返回 False（`superseded`）。
    """
    commit_now = _whole_seconds(clock())
    values: dict[str, object] = {
        "cursor_tenant_id": segment.cursor,
        "wallets_checked": _Run.wallets_checked + segment.checked,
        "wallets_inconsistent": _Run.wallets_inconsistent + len(segment.findings),
        "lease_owner": None,
        "lease_until": None,
    }
    if segment.at_end:
        values.update(
            status=ReconciliationRunStatus.COMPLETED, finished_at=commit_now, running_slot=None
        )
    with factory() as session:
        result = session.execute(
            update(_Run)
            .where(
                _Run.id == claimed.run_id,
                _Run.status == _RUNNING,
                _Run.cursor_tenant_id == claimed.old_cursor,
                _Run.lease_owner == claimed.token,
            )
            .values(**values)
        )
        if result.rowcount != 1:
            session.rollback()
            return False
        if segment.findings:
            session.execute(
                insert(WalletReconciliationFinding),
                [{"run_id": claimed.run_id, **finding._asdict()} for finding in segment.findings],
            )
        session.commit()
    return True


def _release(factory: sessionmaker[Session], claimed: ClaimedRun) -> None:
    release_lease(factory, run_id=claimed.run_id, public_id=claimed.public_id, token=claimed.token)


def _log_failure(stage: Stage, run_id: str | None, error: BaseException) -> None:
    """Step 5's one error line: exactly `stage`, `run_id` and `error_type` (设计 §6)."""
    logger.error(
        "wallet_reconciliation_failed",
        extra={"stage": stage, "run_id": run_id, "error_type": type(error).__name__},
    )


def _whole_seconds(moment: dt.datetime) -> dt.datetime:
    return moment.replace(microsecond=0)


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
    "Stage",
    "StartResult",
    "StepResult",
    "claim_run",
    "release_lease",
    "retire_stale_runs",
    "start_run",
    "step",
]

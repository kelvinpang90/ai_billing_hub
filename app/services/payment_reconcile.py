"""Payment status reconciliation (design gate #585 v3 §2「补对账」, AIH-TASK-180).

丢失回调的支付只靠它收口（spec §43、§127）。Beat 经 app/tasks/payment_reconcile.py 每
`payment_reconcile_poll_seconds` 调一次 `run_due`；**数据库是事实来源**，到期条件全部写在候选查询里
（INV-14）。

一轮：预算 45 秒（`time.monotonic`）、最多 50 次 Stripe 查询（两段共用）；`now` 未注入时本轮开始读
一次当前 UTC 时间，整轮共用（候选查询、满上限的判断、写入的时刻）。每处理一笔之前检查预算与
查询次数，任一用完就结束本轮（正在处理的一笔不中断）。

**第 1 段：`PENDING` 补对账**。候选查询照设计的条件块（孤儿、已满上限、未过期两档、过期后 1 小时内、
过期 1 小时以后），时刻参数由应用按 `now` 算好再绑定，不用数据库的日期函数；按
`COALESCE(last_reconciled_at, created_at)`、`id` 排序，最多 200 行。逐笔：

1. 没有网关 id（只会因孤儿条件入选）→ 孤儿过期（`NO_GATEWAY_SESSION`），不查 Stripe；
2. 有网关 id → A 的 `confirm_payment`：
   - 正常返回 → 已满上限且仍是 `PENDING` 时超时过期（`RECONCILE_TIMEOUT`）；
   - `GatewayRequestRejected` → 条件更新（`status = 'PENDING'`）把 `last_reconciled_at` 置为 now，
     记 ERROR，继续下一笔；已满上限时超时过期；
   - `GatewayUnavailable` → 结束本轮（不连续冲击 Stripe），不做超时过期；
   - `ConfirmConflict` → 跳过这一笔，本轮继续，不做超时过期（设计 §5 v2 的拆分）。
3. 其他意外异常 → 这一笔的事务已回滚，记 ERROR（只带 `public_id` 与异常类型），结束本轮。

**孤儿过期 / 超时过期**：新事务，只锁这一行 `payments`（不锁钱包与租户），重查仍是 `PENDING`、
`resolution` 为空（孤儿过期还要求 `gateway_payment_id` 仍为空）才置 `EXPIRED` 并在同一事务写
`PAYMENT_STATUS_CHANGE` 审计（系统，字段与写法照 A）；不满足什么也不做。之后收到付款成功照 A 入账
（ADR-0007）。

**第 2 段：手续费补抓**属于 AIH-TASK-187；本任务里 `_reconcile_fees` 什么也不做。

⚠️ 不跨笔开事务（设计 §2「事务边界」）；补对账与事件处理同时确认同一笔时，A 的「锁支付行后再判、
`PAID` 不再变、账本引用唯一」保证只入账一次（INV-3）。

⚠️ 日志只带 `public_id`、状态、原因码、Stripe 错误码与异常类型（设计 §6）。
"""

from __future__ import annotations

import datetime as dt
import enum
import logging
import time
from dataclasses import dataclass
from typing import Final

from sqlalchemy import ColumnElement, Row, Select, and_, func, or_, select, update
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings
from app.core.database import session_scope
from app.models.payments import Payment, PaymentStatus
from app.payments.base import GatewayRequestRejected, GatewayUnavailable, PaymentGateway
from app.services.auth import utc_now
from app.services.payments import (
    NO_GATEWAY_SESSION,
    RECONCILE_TIMEOUT,
    ConfirmConflict,
    _audit,
    _audit_state,
    confirm_payment,
)

logger = logging.getLogger(__name__)

# 设计 §2「补对账」：一轮的预算与 Stripe 查询上限（两段共用）。
RUN_BUDGET_SECONDS: Final = 45.0
MAX_QUERIES: Final = 50
# 第 1 段候选查询的行数上限。
CANDIDATE_LIMIT: Final = 200

# 设计 §2「配置」：候选查询里的常量（改它们要过闸门）。
ORPHAN_AFTER_EXPIRY_SECONDS: Final = 600
YOUNG_SECONDS: Final = 7200
YOUNG_INTERVAL_SECONDS: Final = 300
OLD_INTERVAL_SECONDS: Final = 1800
JUST_EXPIRED_SECONDS: Final = 3600
JUST_EXPIRED_INTERVAL_SECONDS: Final = 300
LONG_EXPIRED_INTERVAL_SECONDS: Final = 21600

# 候选行：`id`、`public_id`、`gateway_payment_id`、`created_at`。
_Candidate = tuple[int, str, str | None, dt.datetime]


class StopReason(enum.StrEnum):
    """Why one run ended. Only for logs and tests."""

    # 第 1 段（与第 2 段）没有更多到期的支付。
    DONE = "DONE"
    BUDGET_SPENT = "BUDGET_SPENT"
    QUERY_LIMIT = "QUERY_LIMIT"
    GATEWAY_UNAVAILABLE = "GATEWAY_UNAVAILABLE"
    UNEXPECTED_ERROR = "UNEXPECTED_ERROR"


@dataclass(frozen=True)
class ReconcileRun:
    """What one run did. Only for logs and tests."""

    queries: int
    orphans_expired: int
    timeouts_expired: int
    # 第 2 段（AIH-TASK-187）补到的手续费；本任务恒为 0。
    fees_recorded: int
    stop_reason: StopReason


class _Round:
    """The budget and the Stripe query count of one run, shared by both stages."""

    def __init__(self, *, budget_seconds: float, max_queries: int) -> None:
        self.started = time.monotonic()
        self.budget_seconds = budget_seconds
        self.max_queries = max_queries
        self.queries = 0
        self.orphans_expired = 0
        self.timeouts_expired = 0
        self.fees_recorded = 0

    def exhausted(self) -> StopReason | None:
        """Checked before each payment: `None` while both the budget and the queries remain."""
        if time.monotonic() - self.started >= self.budget_seconds:
            return StopReason.BUDGET_SPENT
        if self.queries >= self.max_queries:
            return StopReason.QUERY_LIMIT
        return None


def run_due(
    session_factory: sessionmaker[Session],
    gateway: PaymentGateway,
    *,
    now: dt.datetime | None = None,
    budget_seconds: float = RUN_BUDGET_SECONDS,
    max_queries: int = MAX_QUERIES,
) -> ReconcileRun:
    """One reconciliation run: stage 1, then stage 2 while budget and queries remain.

    `now` 为 `None` 时本轮开始读一次当前 UTC 时间（精确到秒），整轮共用。
    """
    settings = get_settings()
    moment = now if now is not None else utc_now().replace(microsecond=0)
    round_ = _Round(budget_seconds=budget_seconds, max_queries=max_queries)
    stop = _reconcile_pending(
        session_factory,
        gateway,
        round_,
        now=moment,
        grace=dt.timedelta(seconds=settings.payment_reconcile_grace_seconds),
        maximum=dt.timedelta(seconds=settings.payment_reconcile_max_seconds),
    )
    if stop is StopReason.DONE:
        stop = round_.exhausted() or _reconcile_fees(session_factory, gateway, round_, now=moment)
    run = ReconcileRun(
        queries=round_.queries,
        orphans_expired=round_.orphans_expired,
        timeouts_expired=round_.timeouts_expired,
        fees_recorded=round_.fees_recorded,
        stop_reason=stop,
    )
    logger.info(
        "Payment reconciliation run finished",
        extra={
            "queries": run.queries,
            "orphans_expired": run.orphans_expired,
            "timeouts_expired": run.timeouts_expired,
            "fees_recorded": run.fees_recorded,
            "stop_reason": run.stop_reason.value,
        },
    )
    return run


# --- 第 1 段：PENDING 补对账 ------------------------------------------------------------------


def _reconcile_pending(
    session_factory: sessionmaker[Session],
    gateway: PaymentGateway,
    round_: _Round,
    *,
    now: dt.datetime,
    grace: dt.timedelta,
    maximum: dt.timedelta,
) -> StopReason:
    with session_factory() as session:
        candidates = session.execute(
            pending_candidates(now=now, grace=grace, maximum=maximum)
        ).all()
    for row in candidates:
        exhausted = round_.exhausted()
        if exhausted is not None:
            return exhausted
        try:
            stop = _reconcile_one(session_factory, gateway, round_, row, now=now, maximum=maximum)
        except Exception as error:
            # 这一笔的事务已由 `session_scope` 回滚；数据库出错通常不是单行问题，结束本轮。
            logger.error(
                "Payment reconciliation failed unexpectedly; the run ends",
                extra={"payment_id": row.public_id, "error_type": type(error).__name__},
            )
            return StopReason.UNEXPECTED_ERROR
        if stop is not None:
            return stop
    return StopReason.DONE


def pending_candidates(
    *, now: dt.datetime, grace: dt.timedelta, maximum: dt.timedelta
) -> Select[_Candidate]:
    """The stage 1 candidate query, condition for condition as design §2 writes it."""
    payment = Payment
    reconciled = payment.last_reconciled_at

    def ago(seconds: int) -> dt.datetime:
        return now - dt.timedelta(seconds=seconds)

    def reconciled_before(seconds: int) -> ColumnElement[bool]:
        return or_(reconciled.is_(None), reconciled <= ago(seconds))

    return (
        select(payment.id, payment.public_id, payment.gateway_payment_id, payment.created_at)
        .where(
            payment.status == PaymentStatus.PENDING,
            payment.created_at <= now - grace,
            or_(
                # 孤儿
                and_(
                    payment.gateway_payment_id.is_(None),
                    payment.expires_at <= ago(ORPHAN_AFTER_EXPIRY_SECONDS),
                ),
                and_(
                    payment.gateway_payment_id.is_not(None),
                    or_(
                        # 已满上限
                        payment.created_at <= now - maximum,
                        # 未过期、建单 2 小时内
                        and_(
                            payment.expires_at > now,
                            payment.created_at > ago(YOUNG_SECONDS),
                            reconciled_before(YOUNG_INTERVAL_SECONDS),
                        ),
                        # 未过期、建单超过 2 小时
                        and_(
                            payment.expires_at > now,
                            payment.created_at <= ago(YOUNG_SECONDS),
                            reconciled_before(OLD_INTERVAL_SECONDS),
                        ),
                        # 过期后 1 小时内
                        and_(
                            payment.expires_at <= now,
                            payment.expires_at > ago(JUST_EXPIRED_SECONDS),
                            reconciled_before(JUST_EXPIRED_INTERVAL_SECONDS),
                        ),
                        # 过期 1 小时以后
                        and_(
                            payment.expires_at <= ago(JUST_EXPIRED_SECONDS),
                            reconciled_before(LONG_EXPIRED_INTERVAL_SECONDS),
                        ),
                    ),
                ),
            ),
        )
        .order_by(func.coalesce(reconciled, payment.created_at), payment.id)
        .limit(CANDIDATE_LIMIT)
    )


def _reconcile_one(
    session_factory: sessionmaker[Session],
    gateway: PaymentGateway,
    round_: _Round,
    row: Row[_Candidate],
    *,
    now: dt.datetime,
    maximum: dt.timedelta,
) -> StopReason | None:
    """One candidate. `None`: go on with the next one; otherwise the run ends with that reason."""
    if row.gateway_payment_id is None:
        if _expire(session_factory, row.id, NO_GATEWAY_SESSION, orphan=True, now=now):
            round_.orphans_expired += 1
        return None

    at_maximum = row.created_at <= now - maximum
    round_.queries += 1
    try:
        result = confirm_payment(session_factory, gateway, row.public_id, now=now)
    except GatewayUnavailable as error:
        logger.warning(
            "Gateway unavailable during payment reconciliation; the run ends",
            extra={"payment_id": row.public_id, "error_code": error.code},
        )
        return StopReason.GATEWAY_UNAVAILABLE
    except ConfirmConflict:
        # A 已记 WARNING；下一轮再来。
        return None
    except GatewayRequestRejected as error:
        _mark_reconciled(session_factory, row.id, now=now)
        logger.error(
            "Gateway rejected the payment reconciliation query",
            extra={"payment_id": row.public_id, "error_code": error.code},
        )
        if at_maximum:
            _expire_timeout(session_factory, round_, row, now=now)
        return None
    if at_maximum and result.new_status is PaymentStatus.PENDING:
        _expire_timeout(session_factory, round_, row, now=now)
    return None


def _mark_reconciled(
    session_factory: sessionmaker[Session], payment_id: int, *, now: dt.datetime
) -> None:
    """`last_reconciled_at = now` while the payment is still `PENDING`; nothing else."""
    with session_scope(session_factory) as session:
        session.execute(
            update(Payment)
            .where(Payment.id == payment_id, Payment.status == PaymentStatus.PENDING)
            .values(last_reconciled_at=now)
            .execution_options(synchronize_session=False)
        )


def _expire_timeout(
    session_factory: sessionmaker[Session],
    round_: _Round,
    row: Row[_Candidate],
    *,
    now: dt.datetime,
) -> None:
    if _expire(session_factory, row.id, RECONCILE_TIMEOUT, orphan=False, now=now):
        round_.timeouts_expired += 1


def _expire(
    session_factory: sessionmaker[Session],
    payment_id: int,
    reason: str,
    *,
    orphan: bool,
    now: dt.datetime,
) -> bool:
    """Orphan or timeout expiry in a transaction of its own that locks only this payment row.

    `False`：重查不满足（已不是 `PENDING`、已结案，或孤儿已被认回会话），什么也没写。
    """
    with session_scope(session_factory) as session:
        payment = session.execute(
            select(Payment)
            .where(Payment.id == payment_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).scalar_one()
        if payment.status is not PaymentStatus.PENDING or payment.resolution is not None:
            return False
        if orphan and payment.gateway_payment_id is not None:
            return False
        before = _audit_state(payment)
        payment.status = PaymentStatus.EXPIRED
        payment.status_reason = reason
        payment.updated_at = now
        _audit(session, payment, before, now=now)
        public_id = payment.public_id
    logger.info(
        "Payment status changed",
        extra={
            "payment_id": public_id,
            "old_status": PaymentStatus.PENDING.value,
            "new_status": PaymentStatus.EXPIRED.value,
            "reason": reason,
        },
    )
    return True


# --- 第 2 段：手续费补抓（AIH-TASK-187） ---------------------------------------------------------


def _reconcile_fees(
    session_factory: sessionmaker[Session],
    gateway: PaymentGateway,
    round_: _Round,
    *,
    now: dt.datetime,
) -> StopReason:
    """Stage 2. AIH-TASK-187 implements it; here it does nothing."""
    return StopReason.DONE


__all__ = [
    "CANDIDATE_LIMIT",
    "MAX_QUERIES",
    "RUN_BUDGET_SECONDS",
    "ReconcileRun",
    "StopReason",
    "pending_candidates",
    "run_due",
]

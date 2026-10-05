"""Requeue usage events that never had a financial effect (design gate #181 v2 §2「重新入队」).

spec §84「reprocess event」的无财务效果版本，AIH-TASK-032。管理员修好目录、价格、规则或汇率后，
把停在四个错误状态之一（`MODEL_UNKNOWN` / `PRICING_ERROR` / `FX_RATE_ERROR` / `FAILED_FINAL`）的
事件改回 `RECEIVED`，清空错误码、尝试次数与退避时刻，下一轮扫描照常认领。

⚠️ 路径与动作名用 `requeue`，不用 §89 的 `reprocess`：后者在 Phase 8 专指「对已计费事件重算并
生成调整行」，财务含义完全不同。这里**从不**碰已计费的事件：`PROCESSED` 不是可重新入队的状态，
账本里已有这个 `event_id` 的行（`LEDGER_CONFLICT` 的 `FAILED_FINAL`）同样拒绝 —— MySQL 上迁移
0018 的触发器再兜一层。

⚠️ **每个事件一个事务**（INV-13）：锁事件行 → 判状态 → 改回 `RECEIVED` → 审计
`USAGE_EVENT_REQUEUE`（`reason`、原状态、原错误码与尝试次数）→ 提交。批量逐条事务，不符合的
跳过并计数。事件行只锁它自己，锁顺序与计费一致（事件行在最前）。

⚠️ 不自动重新入队（例如发布价格后把 `PRICING_ERROR` 全部放回）：原因多样，自动放回可能反复
失败；V1 由管理员看告警、修配置、再重新入队（docs/runbook.md）。响应只含数量与 `public_id`，
不含成本。这一层不写日志。
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.core.database import session_scope
from app.core.errors import AppError
from app.models.auth import AuditAction, User
from app.models.usage import UsageEvent, UsageEventStatus
from app.models.wallet import ReferenceType, WalletTransaction
from app.repositories import tenancy
from app.services.auth import RequestContext, record_audit, utc_now
from app.services.customers import CustomerNotFound

# 审计的 entity_type；entity_id 是事件的 public_id（AIH-TASK-029 已归入 PUBLIC_ENTITY_TYPES）。
ENTITY_USAGE_EVENT: Final = "usage_event"

# 从未产生财务效果、可以放回的四个错误状态（设计 §2 状态表最后一行）。
REQUEUABLE_STATUSES: Final = (
    UsageEventStatus.MODEL_UNKNOWN,
    UsageEventStatus.PRICING_ERROR,
    UsageEventStatus.FX_RATE_ERROR,
    UsageEventStatus.FAILED_FINAL,
)
# 批量一次最多处理多少条（设计 §2）。
MAX_BULK_REQUEUE: Final = 1000

# 取当前时间（不带时区的 UTC）。可注入，测试据此冻结时钟。
type Clock = Callable[[], dt.datetime]


class UsageEventNotFound(AppError):
    def __init__(self) -> None:
        super().__init__("Usage event not found.", code="USAGE_EVENT_NOT_FOUND", http_status=404)


class UsageEventNotRequeuable(AppError):
    """Not in one of the four error states, or the ledger already has a row for it."""

    def __init__(self) -> None:
        super().__init__(
            "Only a usage event in an error state that was never billed can be requeued.",
            code="USAGE_EVENT_NOT_REQUEUABLE",
            http_status=409,
        )


@dataclass(frozen=True)
class BulkRequeueResult:
    requeued: int
    skipped: int
    # 重新入队了的事件的 public_id，按内部 id 升序。
    ids: tuple[str, ...]


def _seconds(moment: dt.datetime) -> dt.datetime:
    # MySQL 的 DATETIME 不存小数秒：与其他服务端时间一致。
    return moment.replace(microsecond=0)


def _lock(
    session: Session, *, public_id: str | None = None, row_id: int | None = None
) -> UsageEvent | None:
    """The event row, `FOR UPDATE`, refreshed (the only row a requeue locks)."""
    statement = select(UsageEvent)
    if public_id is not None:
        statement = statement.where(UsageEvent.public_id == public_id)
    else:
        statement = statement.where(UsageEvent.id == row_id)
    statement = statement.with_for_update().execution_options(populate_existing=True)
    return session.execute(statement).scalar_one_or_none()


def _has_ledger_row(session: Session, event_id: str) -> bool:
    statement = (
        select(func.count())
        .select_from(WalletTransaction)
        .where(
            WalletTransaction.reference_type == ReferenceType.USAGE_EVENT,
            WalletTransaction.reference_id == event_id,
        )
    )
    return bool(session.execute(statement).scalar_one())


def _requeue(
    session: Session,
    event: UsageEvent,
    *,
    actor: User,
    reason: str,
    context: RequestContext,
    now: dt.datetime,
) -> bool:
    """Back to RECEIVED with its audit, inside the caller's transaction. False: not requeuable."""
    if event.status not in REQUEUABLE_STATUSES or _has_ledger_row(session, event.event_id):
        return False
    before = {
        "status": UsageEventStatus(event.status).value,
        "error_code": event.error_code,
        "attempt_count": event.attempt_count,
    }
    event.status = UsageEventStatus.RECEIVED
    event.error_code = None
    event.error_message = None
    event.attempt_count = 0
    event.next_attempt_at = None
    record_audit(
        session,
        action=AuditAction.USAGE_EVENT_REQUEUE,
        context=context,
        now=now,
        actor=actor,
        entity_type=ENTITY_USAGE_EVENT,
        entity_id=event.public_id,
        before_state=before,
        after_state={"status": UsageEventStatus.RECEIVED.value},
        reason=reason,
    )
    return True


def requeue_event(
    session_factory: sessionmaker[Session],
    *,
    actor: User,
    usage_event_id: str,
    reason: str,
    context: RequestContext,
    clock: Clock = utc_now,
) -> str:
    """One event by `public_id`, one transaction. 404 if unknown, 409 if not requeuable."""
    with session_scope(session_factory) as session:
        event = _lock(session, public_id=usage_event_id)
        if event is None:
            raise UsageEventNotFound
        now = _seconds(clock())
        if not _requeue(session, event, actor=actor, reason=reason, context=context, now=now):
            raise UsageEventNotRequeuable
        return event.public_id


def requeue_events(
    session_factory: sessionmaker[Session],
    *,
    actor: User,
    status: UsageEventStatus,
    reason: str,
    context: RequestContext,
    error_code: str | None = None,
    customer_id: str | None = None,
    provider: str | None = None,
    model: str | None = None,
    occurred_from: dt.datetime | None = None,
    occurred_to: dt.datetime | None = None,
    clock: Clock = utc_now,
) -> BulkRequeueResult:
    """Up to 1000 events in `status` matching the filters, oldest first, one transaction each.

    `provider` / `model` 比的是上报的原始字符串（模型未知的事件没有目录引用）；时间段按
    `occurred_at` 的 `[occurred_from, occurred_to)`。选出之后状态变了（已被别人放回、已被处理）
    或账本里有它的行的事件跳过并计数。客户不存在：404 `CUSTOMER_NOT_FOUND`。
    """
    if status not in REQUEUABLE_STATUSES:
        raise UsageEventNotRequeuable
    with session_factory() as session:
        conditions = [UsageEvent.status == status]
        if customer_id is not None:
            tenant = tenancy.get_tenant_by_public_id(session, customer_id)
            if tenant is None:
                raise CustomerNotFound
            conditions.append(UsageEvent.tenant_id == tenant.id)
        if error_code is not None:
            conditions.append(UsageEvent.error_code == error_code)
        if provider is not None:
            conditions.append(UsageEvent.provider_code_raw == provider)
        if model is not None:
            conditions.append(UsageEvent.model_code_raw == model)
        if occurred_from is not None:
            conditions.append(UsageEvent.occurred_at >= occurred_from)
        if occurred_to is not None:
            conditions.append(UsageEvent.occurred_at < occurred_to)
        statement = (
            select(UsageEvent.id).where(*conditions).order_by(UsageEvent.id).limit(MAX_BULK_REQUEUE)
        )
        candidates = list(session.execute(statement).scalars().all())
        session.rollback()

    requeued: list[str] = []
    skipped = 0
    for row_id in candidates:
        with session_scope(session_factory) as session:
            event = _lock(session, row_id=row_id)
            now = _seconds(clock())
            if (
                event is None
                or event.status is not status
                or not _requeue(
                    session, event, actor=actor, reason=reason, context=context, now=now
                )
            ):
                skipped += 1
                continue
            requeued.append(event.public_id)
    return BulkRequeueResult(requeued=len(requeued), skipped=skipped, ids=tuple(requeued))


__all__ = [
    "ENTITY_USAGE_EVENT",
    "MAX_BULK_REQUEUE",
    "REQUEUABLE_STATUSES",
    "BulkRequeueResult",
    "UsageEventNotFound",
    "UsageEventNotRequeuable",
    "requeue_event",
    "requeue_events",
]

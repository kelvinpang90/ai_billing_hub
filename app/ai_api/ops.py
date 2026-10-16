"""Audited operational commands for AI usage outbox incidents."""

from __future__ import annotations

import datetime as dt
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Final, Literal

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.ai_api.database import create_ai_database_engine, create_ai_session_factory
from app.ai_api.outbox import AiUsageOutbox, AiUsageOutboxStatus
from app.core.config import get_settings
from app.models.auth import AuditAction
from app.services.auth import RequestContext, record_audit, utc_now

ENTITY_AI_USAGE_OUTBOX: Final = "ai_usage_outbox"
REQUEUABLE_DEAD_ERRORS: Final = frozenset({"INTEGRATION_AUTH_FAILED", "INVALID_BILLING_RESPONSE"})
_CLI_CONTEXT: Final = RequestContext(ip_address=None, user_agent="cli")

type Clock = Callable[[], dt.datetime]
type Operation = Literal["requeued", "acknowledged", "failed"]


@dataclass(frozen=True)
class OperationResult:
    event_id: str
    operation: Operation


def _reason(reason: str) -> str | None:
    cleaned = reason.strip()
    return cleaned if 1 <= len(cleaned) <= 255 else None


def _lock(session: Session, event_id: str) -> AiUsageOutbox | None:
    statement = (
        select(AiUsageOutbox)
        .where(AiUsageOutbox.event_id == event_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return session.execute(statement).scalar_one_or_none()


def _requeue_one(
    session: Session,
    row: AiUsageOutbox,
    *,
    reason: str,
    include_delivered: bool,
    now: dt.datetime,
) -> bool:
    is_dead = row.status is AiUsageOutboxStatus.DEAD
    is_delivered = include_delivered and row.status is AiUsageOutboxStatus.DELIVERED
    if not is_delivered and (not is_dead or row.last_error not in REQUEUABLE_DEAD_ERRORS):
        return False

    before = {
        "status": row.status.value,
        "last_error": row.last_error,
        "attempts": row.attempts,
        "dead_acked_at": row.dead_acked_at.isoformat() if row.dead_acked_at else None,
    }
    if is_delivered:
        before["delivered_at"] = row.delivered_at.isoformat() if row.delivered_at else None
        row.delivered_at = None
    row.status = AiUsageOutboxStatus.PENDING
    row.next_attempt_at = now
    row.claim_id = None
    row.last_error = None
    row.dead_acked_at = None
    row.attempts = 0
    after: dict[str, object] = {
        "status": AiUsageOutboxStatus.PENDING.value,
        "last_error": None,
        "attempts": 0,
        "dead_acked_at": None,
    }
    if is_delivered:
        after["delivered_at"] = None
    record_audit(
        session,
        action=AuditAction.AI_USAGE_REQUEUE,
        context=_CLI_CONTEXT,
        now=now,
        entity_type=ENTITY_AI_USAGE_OUTBOX,
        entity_id=row.event_id,
        before_state=before,
        after_state=after,
        reason=reason,
    )
    return True


def _ack_one(session: Session, row: AiUsageOutbox, *, reason: str, now: dt.datetime) -> bool:
    if row.status is not AiUsageOutboxStatus.DEAD or row.dead_acked_at is not None:
        return False
    before = {"status": AiUsageOutboxStatus.DEAD.value, "dead_acked_at": None}
    row.dead_acked_at = now
    record_audit(
        session,
        action=AuditAction.AI_USAGE_DEAD_ACK,
        context=_CLI_CONTEXT,
        now=now,
        entity_type=ENTITY_AI_USAGE_OUTBOX,
        entity_id=row.event_id,
        before_state=before,
        after_state={"status": AiUsageOutboxStatus.DEAD.value, "dead_acked_at": now.isoformat()},
        reason=reason,
    )
    return True


def _operate(
    session_factory: sessionmaker[Session],
    event_ids: Sequence[str],
    *,
    reason: str,
    include_delivered: bool = False,
    acknowledge: bool = False,
    clock: Clock = utc_now,
) -> tuple[OperationResult, ...]:
    """Apply one independently committed, locked transaction per event id."""
    cleaned = _reason(reason)
    if cleaned is None:
        raise ValueError("reason must be 1 to 255 characters after trimming")
    results: list[OperationResult] = []
    for event_id in event_ids:
        try:
            with session_factory.begin() as session:
                row = _lock(session, event_id)
                if row is None:
                    changed = False
                elif acknowledge:
                    changed = _ack_one(session, row, reason=cleaned, now=clock())
                else:
                    changed = _requeue_one(
                        session,
                        row,
                        reason=cleaned,
                        include_delivered=include_delivered,
                        now=clock(),
                    )
        except Exception:
            changed = False
        done: Operation = "acknowledged" if acknowledge else "requeued"
        results.append(OperationResult(event_id, done if changed else "failed"))
    return tuple(results)


def requeue(
    session_factory: sessionmaker[Session],
    event_ids: Sequence[str],
    *,
    reason: str,
    include_delivered: bool = False,
    clock: Clock = utc_now,
) -> tuple[OperationResult, ...]:
    return _operate(
        session_factory,
        event_ids,
        reason=reason,
        include_delivered=include_delivered,
        clock=clock,
    )


def dead_ack(
    session_factory: sessionmaker[Session],
    event_ids: Sequence[str],
    *,
    reason: str,
    clock: Clock = utc_now,
) -> tuple[OperationResult, ...]:
    return _operate(session_factory, event_ids, reason=reason, acknowledge=True, clock=clock)


def _run(
    event_ids: Sequence[str],
    *,
    reason: str,
    include_delivered: bool = False,
    acknowledge: bool = False,
) -> int:
    if _reason(reason) is None:
        print("--reason must be 1 to 255 characters after trimming.", file=sys.stderr)
        return 2
    settings = get_settings()
    # 设计 §2「API 进程之外的库访问」：每次执行建一个自己的 engine，结束时 dispose。
    engine = create_ai_database_engine(
        settings.database_url,
        threads=1,
        pool_timeout_seconds=settings.ai_db_pool_timeout_seconds,
        connect_timeout_seconds=settings.ai_db_connect_timeout_seconds,
        rw_timeout_seconds=settings.ai_db_rw_timeout_seconds,
    )
    try:
        factory = create_ai_session_factory(engine)
        results = (
            dead_ack(factory, event_ids, reason=reason)
            if acknowledge
            else requeue(factory, event_ids, reason=reason, include_delivered=include_delivered)
        )
    finally:
        engine.dispose()
    for result in results:
        print(f"{result.event_id} {result.operation}")
    changed = sum(result.operation != "failed" for result in results)
    print(f"Changed {changed} / {len(results)}")
    return 0 if changed == len(results) else 1


def run_requeue(event_ids: Sequence[str], *, reason: str, include_delivered: bool = False) -> int:
    return _run(event_ids, reason=reason, include_delivered=include_delivered)


def run_dead_ack(event_ids: Sequence[str], *, reason: str) -> int:
    return _run(event_ids, reason=reason, acknowledge=True)


__all__ = [
    "ENTITY_AI_USAGE_OUTBOX",
    "REQUEUABLE_DEAD_ERRORS",
    "OperationResult",
    "dead_ack",
    "requeue",
    "run_dead_ack",
    "run_requeue",
]

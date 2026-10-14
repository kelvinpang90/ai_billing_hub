"""The bounded in-memory safety buffer for usage not confirmed in the database."""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.exc import TimeoutError as SATimeoutError
from sqlalchemy.orm import Session, sessionmaker

from app.ai_api.database import AiDatabaseCapacityExceeded, AiDatabaseExecutor
from app.ai_api.outbox import AiUsageOutbox, AiUsageOutboxStatus

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class HeldUsage:
    payload: dict[str, Any]
    internal_tenant_id: int
    internal_project_id: int
    ai_request_id: str
    commit_outcome: str


def log_fields(item: HeldUsage) -> dict[str, Any]:
    p = item.payload
    return {
        "event_id": p["event_id"],
        "ai_request_id": item.ai_request_id,
        "feature": p["feature"],
        "tenant_public_id": p["tenant_id"],
        "project_public_id": p["project_id"],
        "model": p["model"],
        "usage_input": p["input_tokens"],
        "usage_output": p["output_tokens"],
        "usage_cache_write": p["cache_creation_input_tokens"],
        "usage_cache_read": p["cache_read_input_tokens"],
        "occurred_at": p["occurred_at"],
        "commit_outcome": item.commit_outcome,
    }


class HeldBuffer:
    def __init__(self, maximum: int) -> None:
        self.maximum = maximum
        self._items: deque[HeldUsage] = deque()

    def __len__(self) -> int:
        return len(self._items)

    def take(self) -> HeldUsage | None:
        return self._items.popleft() if self._items else None

    def put_front(self, item: HeldUsage) -> None:
        self._items.appendleft(item)

    def put_back(self, item: HeldUsage) -> None:
        self._items.append(item)

    def put(self, item: HeldUsage) -> None:
        if len(self._items) >= self.maximum:
            logger.critical(
                "AI usage held buffer overflow", extra=log_fields(self._items.popleft())
            )
        self._items.append(item)


def _row(item: HeldUsage) -> AiUsageOutbox:
    p, now = item.payload, dt.datetime.now(dt.UTC)
    return AiUsageOutbox(
        event_id=p["event_id"],
        request_id=p["request_id"],
        claim_id=None,
        tenant_id=item.internal_tenant_id,
        project_id=item.internal_project_id,
        feature=p["feature"],
        payload_json=json.dumps(p, separators=(",", ":")),
        status=AiUsageOutboxStatus.HELD,
        attempts=0,
        next_attempt_at=now,
        last_error=None,
        first_attempted_at=None,
        created_at=now,
        delivered_at=None,
        resolved_at=None,
        review_required_at=None,
        reviewed_at=None,
        dead_acked_at=None,
    )


def _review_log(item: HeldUsage, status: AiUsageOutboxStatus) -> None:
    p = item.payload
    logger.critical(
        "AI usage entered billing although the request returned 503",
        extra={
            "event_id": p["event_id"],
            "ai_request_id": item.ai_request_id,
            "feature": p["feature"],
            "tenant_public_id": p["tenant_id"],
            "project_public_id": p["project_id"],
            "model": p["model"],
            "outbox_status": status.value,
        },
    )


def _duplicate_event_id(error: IntegrityError) -> bool:
    # Only MySQL 1062 on the event_id key is a collision; any other integrity error
    # (foreign key, NOT NULL, CHECK) must reach the drain loop as an ordinary failure.
    args = getattr(error.orig, "args", ())
    return len(args) >= 2 and args[0] == 1062 and "uq_ai_usage_outbox_event_id" in str(args[1])


def persist_held_once(factory: sessionmaker[Session], item: HeldUsage) -> None:
    """Persist one HELD row, including the uncertain-commit collision rules."""
    event_id, session = str(item.payload["event_id"]), factory()
    try:
        session.add(_row(item))
        try:
            session.commit()
            return
        except IntegrityError as error:
            if not _duplicate_event_id(error):
                raise
            session.rollback()
        result = session.execute(
            update(AiUsageOutbox)
            .where(
                AiUsageOutbox.event_id == event_id,
                AiUsageOutbox.status == AiUsageOutboxStatus.PENDING,
                AiUsageOutbox.first_attempted_at.is_(None),
                AiUsageOutbox.resolved_at.is_(None),
            )
            .values(status=AiUsageOutboxStatus.HELD)
        )
        if result.rowcount == 1:
            session.commit()
            return
        result = session.execute(
            update(AiUsageOutbox)
            .where(
                AiUsageOutbox.event_id == event_id,
                AiUsageOutbox.resolved_at.is_(None),
                AiUsageOutbox.review_required_at.is_(None),
                (
                    AiUsageOutbox.status.in_(
                        (
                            AiUsageOutboxStatus.SENDING,
                            AiUsageOutboxStatus.DELIVERED,
                            AiUsageOutboxStatus.DEAD,
                        )
                    )
                    | (
                        (AiUsageOutbox.status == AiUsageOutboxStatus.PENDING)
                        & AiUsageOutbox.first_attempted_at.is_not(None)
                    )
                ),
            )
            .values(review_required_at=dt.datetime.now(dt.UTC))
        )
        session.commit()
        row = session.scalar(select(AiUsageOutbox).where(AiUsageOutbox.event_id == event_id))
        if result.rowcount == 1 or (
            row is not None and row.review_required_at is not None and row.reviewed_at is None
        ):
            if row is not None:
                _review_log(item, row.status)
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _connection_error(error: BaseException) -> bool:
    if isinstance(error, SATimeoutError):
        return True
    return isinstance(error, DBAPIError) and getattr(error.orig, "args", [None])[0] in {
        2003,
        2006,
        2013,
    }


async def drain_held_once(
    buffer: HeldBuffer,
    executor: AiDatabaseExecutor,
    factory: sessionmaker[Session],
    *,
    timeout_seconds: float,
) -> None:
    for _ in range(len(buffer)):
        item = buffer.take()
        if item is None:
            return
        try:
            await executor.run_with_timeout(timeout_seconds, persist_held_once, factory, item)
        except (TimeoutError, AiDatabaseCapacityExceeded):
            buffer.put_front(item)
            return
        except Exception as error:
            if _connection_error(error):
                buffer.put_front(item)
                return
            logger.error("AI usage held persistence failed", exc_info=True)
            buffer.put_back(item)
        except BaseException:
            # An outer shutdown wall clock can cancel this coroutine while the database
            # worker is still running.  The worker owns no queue state, so retain the
            # item for the next process-lifetime attempt before propagating cancellation.
            buffer.put_front(item)
            raise


async def held_retry_loop(
    buffer: HeldBuffer,
    executor: AiDatabaseExecutor,
    factory: sessionmaker[Session],
    *,
    retry_seconds: float,
    timeout_seconds: float,
    stopping: Callable[[], bool] | None = None,
) -> None:
    while stopping is None or not stopping():
        await drain_held_once(buffer, executor, factory, timeout_seconds=timeout_seconds)
        await asyncio.sleep(retry_seconds)


async def final_held_attempt(
    buffer: HeldBuffer,
    executor: AiDatabaseExecutor,
    factory: sessionmaker[Session],
    *,
    timeout_seconds: float,
) -> None:
    async with asyncio.timeout(5):
        await drain_held_once(buffer, executor, factory, timeout_seconds=timeout_seconds)

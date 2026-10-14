"""AI usage outbox persistence model (design gate #281 v23 §2)."""

from __future__ import annotations

import asyncio
import datetime as dt
import enum
import json
import logging
from dataclasses import replace
from typing import TYPE_CHECKING, Any, Final, Literal

from sqlalchemy import (
    CHAR,
    BigInteger,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    select,
)
from sqlalchemy.dialects import mysql
from sqlalchemy.orm import Mapped, Session, mapped_column, sessionmaker

from app.ai_api.database import AiDatabaseCapacityExceeded, AiDatabaseExecutor
from app.models import tenancy as _tenancy_models  # noqa: F401
from app.models.base import Base

logger = logging.getLogger(__name__)
CommitOutcome = Literal["written", "written_after_error", "absent", "unknown"]

if TYPE_CHECKING:
    from app.ai_api.held import HeldBuffer, HeldUsage

_PrimaryKey = BigInteger().with_variant(Integer, "sqlite")
_ForeignKeyInt = BigInteger().with_variant(Integer, "sqlite")
ULID_LENGTH: Final = 26
FEATURE_LENGTH: Final = 64
STATUS_LENGTH: Final = 16
TEXT_COLLATION: Final = "utf8mb4_0900_bin"
OUTBOX_STATUS_CHECK: Final = (
    "status IN ('PENDING', 'SENDING', 'DELIVERED', 'DEAD', 'HELD', 'DISCARDED')"
)
OUTBOX_ATTEMPTS_CHECK: Final = "attempts >= 0"


class AiUsageOutboxStatus(enum.StrEnum):
    PENDING = "PENDING"
    SENDING = "SENDING"
    DELIVERED = "DELIVERED"
    DEAD = "DEAD"
    HELD = "HELD"
    DISCARDED = "DISCARDED"


def _ascii(length: int) -> CHAR:
    return CHAR(length).with_variant(mysql.CHAR(length, collation="ascii_bin"), "mysql")


def _text(length: int) -> String:
    return String(length).with_variant(String(length, collation=TEXT_COLLATION), "mysql")


_MicrosecondDateTime = DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql")


class AiUsageOutbox(Base):
    """Append-only local record of usage awaiting ingestion."""

    __tablename__ = "ai_usage_outbox"

    id: Mapped[int] = mapped_column(_PrimaryKey, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(_ascii(ULID_LENGTH), nullable=False)
    request_id: Mapped[str] = mapped_column(_ascii(ULID_LENGTH), nullable=False)
    claim_id: Mapped[str | None] = mapped_column(_ascii(ULID_LENGTH), nullable=True)
    tenant_id: Mapped[int] = mapped_column(_ForeignKeyInt, nullable=False)
    project_id: Mapped[int] = mapped_column(_ForeignKeyInt, nullable=False)
    feature: Mapped[str] = mapped_column(_text(FEATURE_LENGTH), nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[AiUsageOutboxStatus] = mapped_column(
        Enum(AiUsageOutboxStatus, native_enum=False, length=STATUS_LENGTH), nullable=False
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False)
    next_attempt_at: Mapped[dt.datetime] = mapped_column(_MicrosecondDateTime, nullable=False)
    last_error: Mapped[str | None] = mapped_column(String(64), nullable=True)
    first_attempted_at: Mapped[dt.datetime | None] = mapped_column(
        _MicrosecondDateTime, nullable=True
    )
    created_at: Mapped[dt.datetime] = mapped_column(_MicrosecondDateTime, nullable=False)
    delivered_at: Mapped[dt.datetime | None] = mapped_column(_MicrosecondDateTime, nullable=True)
    resolved_at: Mapped[dt.datetime | None] = mapped_column(_MicrosecondDateTime, nullable=True)
    review_required_at: Mapped[dt.datetime | None] = mapped_column(
        _MicrosecondDateTime, nullable=True
    )
    reviewed_at: Mapped[dt.datetime | None] = mapped_column(_MicrosecondDateTime, nullable=True)
    dead_acked_at: Mapped[dt.datetime | None] = mapped_column(_MicrosecondDateTime, nullable=True)

    __table_args__ = (
        ForeignKeyConstraint(
            ["project_id", "tenant_id"],
            ["projects.id", "projects.tenant_id"],
            name="fk_ai_usage_outbox_project",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("event_id", name="uq_ai_usage_outbox_event_id"),
        Index("ix_ai_usage_outbox_status_next_attempt_at", "status", "next_attempt_at"),
        CheckConstraint(OUTBOX_STATUS_CHECK, name="ck_ai_usage_outbox_status"),
        CheckConstraint(OUTBOX_ATTEMPTS_CHECK, name="ck_ai_usage_outbox_attempts"),
    )


def _pending_row(payload: dict[str, Any], tenant_id: int, project_id: int) -> AiUsageOutbox:
    now = dt.datetime.now(dt.UTC)
    return AiUsageOutbox(
        event_id=payload["event_id"],
        request_id=payload["request_id"],
        claim_id=None,
        tenant_id=tenant_id,
        project_id=project_id,
        feature=payload["feature"],
        payload_json=json.dumps(payload, separators=(",", ":")),
        status=AiUsageOutboxStatus.PENDING,
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


def write_pending(
    factory: sessionmaker[Session],
    *,
    payload: dict[str, Any],
    internal_tenant_id: int,
    internal_project_id: int,
) -> CommitOutcome:
    """Write PENDING; a failed commit acknowledgement is checked on a fresh session."""
    try:
        session = factory()
    except Exception:
        return "unknown"
    try:
        try:
            session.add(_pending_row(payload, internal_tenant_id, internal_project_id))
            session.commit()
            return "written"
        except Exception:
            try:
                session.rollback()
            except Exception:
                pass
    finally:
        try:
            session.close()
        except Exception:
            pass
    try:
        check = factory()
        try:
            found = check.scalar(
                select(AiUsageOutbox.status).where(AiUsageOutbox.event_id == payload["event_id"])
            )
            return "written_after_error" if found is not None else "absent"
        finally:
            try:
                check.close()
            except Exception:
                pass
    except Exception:
        return "unknown"


async def persist_usage(
    executor: AiDatabaseExecutor,
    factory: sessionmaker[Session],
    held_buffer: HeldBuffer,
    held_item: HeldUsage,
    *,
    timeout_seconds: float,
) -> CommitOutcome:
    """The only owner of cancellation, critical logging and the held buffer."""
    recorded = False

    def record(outcome: Literal["absent", "unknown"]) -> None:
        nonlocal recorded
        if recorded:
            return
        recorded = True
        item = replace(held_item, commit_outcome=outcome)
        payload = item.payload
        logger.critical(
            "AI usage not persisted",
            extra={
                "event_id": payload["event_id"],
                "ai_request_id": item.ai_request_id,
                "feature": payload["feature"],
                "tenant_public_id": payload["tenant_id"],
                "project_public_id": payload["project_id"],
                "model": payload["model"],
                "usage_input": payload["input_tokens"],
                "usage_output": payload["output_tokens"],
                "usage_cache_write": payload["cache_creation_input_tokens"],
                "usage_cache_read": payload["cache_read_input_tokens"],
                "occurred_at": payload["occurred_at"],
                "commit_outcome": outcome,
            },
        )
        held_buffer.put(item)

    try:
        outcome = await executor.run_with_timeout(
            timeout_seconds,
            write_pending,
            factory,
            payload=held_item.payload,
            internal_tenant_id=held_item.internal_tenant_id,
            internal_project_id=held_item.internal_project_id,
        )
    except asyncio.CancelledError:
        record("unknown")
        raise
    except AiDatabaseCapacityExceeded:
        record("absent")
        return "absent"
    except TimeoutError:
        record("unknown")
        return "unknown"
    except Exception:
        record("unknown")
        return "unknown"
    if outcome == "written_after_error":
        logger.warning("AI usage commit acknowledgement failed but row was written")
    elif outcome in {"absent", "unknown"}:
        record(outcome)
    return outcome

"""AI usage outbox persistence model (design gate #281 v23 §2)."""

from __future__ import annotations

import datetime as dt
import enum
from typing import Final

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
)
from sqlalchemy.dialects import mysql
from sqlalchemy.orm import Mapped, mapped_column

from app.models import tenancy as _tenancy_models  # noqa: F401
from app.models.base import Base

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

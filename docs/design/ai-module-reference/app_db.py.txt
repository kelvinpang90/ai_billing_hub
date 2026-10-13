"""Local durable usage outbox. Business context and model responses are not stored here."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import DateTime, Integer, String, Text, create_engine
from sqlalchemy.dialects import mysql
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker


# MySQL's DATETIME keeps whole seconds and rounds: a row due now could round into the future and
# miss its claim. Microseconds keep MySQL ordered like SQLite.
_TIMESTAMP = DateTime(timezone=True).with_variant(mysql.DATETIME(fsp=6), "mysql")


class Base(DeclarativeBase):
    pass


class UsageOutbox(Base):
    __tablename__ = "usage_outbox"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_id: Mapped[str] = mapped_column(String(26), unique=True, nullable=False)
    client_id: Mapped[str] = mapped_column(String(128), nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="PENDING")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(_TIMESTAMP, nullable=False)
    claim_id: Mapped[str | None] = mapped_column(String(26))
    last_error: Mapped[str | None] = mapped_column(String(64))


def create_session_factory(database_url: str) -> sessionmaker:
    engine = create_engine(database_url, pool_pre_ping=True)
    Base.metadata.create_all(engine)
    return sessionmaker(engine, expire_on_commit=False)


def utcnow() -> datetime:
    return datetime.now(UTC)

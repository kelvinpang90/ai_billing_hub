"""AI client-key persistence model (design gate #281 v23 §2)."""

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
    UniqueConstraint,
)
from sqlalchemy.dialects import mysql
from sqlalchemy.orm import Mapped, mapped_column

from app.models import tenancy as _tenancy_models  # noqa: F401
from app.models.base import Base

_PrimaryKey = BigInteger().with_variant(Integer, "sqlite")
_ForeignKeyInt = BigInteger().with_variant(Integer, "sqlite")
CLIENT_ID_LENGTH: Final = 40
TOKEN_SHA256_LENGTH: Final = 64
TEXT_COLLATION: Final = "utf8mb4_0900_bin"
TOKEN_COLLATION: Final = "ascii_bin"
STATUS_LENGTH: Final = 16
CLIENT_KEY_STATUS_CHECK: Final = "status IN ('ACTIVE', 'REVOKED')"
CLIENT_KEY_REVOKED_AT_CHECK: Final = "(status = 'REVOKED') = (revoked_at IS NOT NULL)"


class AiClientKeyStatus(enum.StrEnum):
    ACTIVE = "ACTIVE"
    REVOKED = "REVOKED"


def _text(length: int) -> String:
    return String(length).with_variant(String(length, collation=TEXT_COLLATION), "mysql")


class AiClientKey(Base):
    """A non-secret client identifier and the SHA-256 digest of its bearer token."""

    __tablename__ = "ai_client_keys"

    id: Mapped[int] = mapped_column(_PrimaryKey, primary_key=True, autoincrement=True)
    tenant_id: Mapped[int] = mapped_column(_ForeignKeyInt, nullable=False)
    project_id: Mapped[int] = mapped_column(_ForeignKeyInt, nullable=False)
    client_id: Mapped[str] = mapped_column(_text(CLIENT_ID_LENGTH), nullable=False)
    token_sha256: Mapped[str] = mapped_column(
        CHAR(TOKEN_SHA256_LENGTH).with_variant(
            mysql.CHAR(TOKEN_SHA256_LENGTH, collation=TOKEN_COLLATION), "mysql"
        ),
        nullable=False,
    )
    status: Mapped[AiClientKeyStatus] = mapped_column(
        Enum(AiClientKeyStatus, native_enum=False, length=STATUS_LENGTH), nullable=False
    )
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False)
    revoked_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)

    __table_args__ = (
        ForeignKeyConstraint(
            ["project_id", "tenant_id"],
            ["projects.id", "projects.tenant_id"],
            name="fk_ai_client_keys_project",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("client_id", name="uq_ai_client_keys_client_id"),
        Index("ix_ai_client_keys_project_id", "project_id"),
        CheckConstraint(CLIENT_KEY_STATUS_CHECK, name="ck_ai_client_keys_status"),
        CheckConstraint(CLIENT_KEY_REVOKED_AT_CHECK, name="ck_ai_client_keys_revoked_at"),
    )

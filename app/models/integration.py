"""Integration API credentials (spec §36, §37, §74.4; design gate #118 v1, AIH-TASK-012).

一行是**一个 `api_key` 的一个签名版本**。轮换时 `api_key` 不变、新增一行（Kelvin
2026-09-25 选的方案 A），所以唯一约束是 `(public_api_key, key_version)`，而不是 §74.4
字面的「`public_api_key` 唯一」（理由见设计 §2、§9）。

⚠️ **两个版本号不是一回事**（ADR-0004 §4）：

- `key_version`：签名密钥版本，客户在 `X-Acuven-Key-Version` 里看到的就是它；
- `encryption_key_version`：加密 `encrypted_secret` 的**主密钥**版本（`encrypt_secret`
  的第二个返回值）。重新包裹只改它，不改 `key_version`。

⚠️ **行永不删除**：以后的用量事件要引用它（INV-6），外键是 `RESTRICT`，repository 里
也没有删除函数。吊销只改状态。

状态只存 `ACTIVE` / `REVOKED`；「已过期」由 `valid_until` 推出，不存（设计 §4）。
"""

from __future__ import annotations

import datetime as dt
import enum
from typing import Final

from sqlalchemy import (
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
from sqlalchemy.orm import Mapped, mapped_column

# 复合外键指向 projects：import 这个模块，表才在同一个 metadata 里。
from app.models import tenancy as _tenancy_models  # noqa: F401
from app.models.base import Base

# 与 app/models/auth.py 同一个写法（SQLite 只对 INTEGER PRIMARY KEY 自增）。
_PrimaryKey = BigInteger().with_variant(Integer, "sqlite")
_ForeignKeyInt = BigInteger().with_variant(Integer, "sqlite")

API_KEY_LENGTH: Final = 64
# ⚠️ 按字节比较（区分大小写、NO PAD），与 `wallet_transactions.reference_id` 同一理由：
# 库默认的 utf8mb4_0900_ai_ci 会把只差大小写的两个 key 判成同一个。只在 MySQL 上指定。
API_KEY_COLLATION: Final = "utf8mb4_0900_bin"
# 设计 §2 写死的列宽。
_STATUS_LENGTH = 16


class CredentialStatus(enum.StrEnum):
    """`REVOKED` 是终态：没有「恢复」。"""

    ACTIVE = "ACTIVE"
    REVOKED = "REVOKED"


# ⚠️ 条件文本与迁移 0007 逐字一致（空白除外），test_migrations.py 比对两边。
KEY_VERSION_CHECK: Final = "key_version >= 1"
STATUS_CHECK: Final = "status IN ('ACTIVE', 'REVOKED')"
REVOKED_AT_CHECK: Final = "(status = 'REVOKED') = (revoked_at IS NOT NULL)"


class IntegrationCredential(Base):
    """spec §74.4, plus the master-key version column ADR-0004 §4 asks for."""

    __tablename__ = "integration_credentials"

    id: Mapped[int] = mapped_column(_PrimaryKey, primary_key=True, autoincrement=True)
    tenant_id: Mapped[int] = mapped_column(_ForeignKeyInt, nullable=False)
    project_id: Mapped[int] = mapped_column(_ForeignKeyInt, nullable=False)
    # `ak_` + 32 个小写十六进制字符。非机密的查找标识（§36）。
    public_api_key: Mapped[str] = mapped_column(
        String(API_KEY_LENGTH).with_variant(
            String(API_KEY_LENGTH, collation=API_KEY_COLLATION), "mysql"
        ),
        nullable=False,
    )
    key_version: Mapped[int] = mapped_column(Integer, nullable=False)
    # `encrypt_secret` 的输出，带行 AAD（见 app/services/integration_auth.py 的
    # `credential_aad`）。库里没有明文。
    encrypted_secret: Mapped[str] = mapped_column(Text, nullable=False)
    encryption_key_version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[CredentialStatus] = mapped_column(
        Enum(CredentialStatus, native_enum=False, length=_STATUS_LENGTH),
        nullable=False,
        default=CredentialStatus.ACTIVE,
    )
    valid_from: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False)
    # NULL = 没有截止；轮换时写入 `now + 重叠期`。
    valid_until: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    # 本任务不写（设计 §1），随摄取端点做。
    last_used_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False)
    revoked_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)

    __table_args__ = (
        # 数据库保证凭据行的租户就是项目所属的租户（INV-8）。
        ForeignKeyConstraint(
            ["project_id", "tenant_id"],
            ["projects.id", "projects.tenant_id"],
            name="fk_integration_credentials_project",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "public_api_key",
            "key_version",
            name="uq_integration_credentials_key_version",
        ),
        Index("ix_integration_credentials_project_id", "project_id"),
        CheckConstraint(KEY_VERSION_CHECK, name="ck_integration_credentials_key_version"),
        CheckConstraint(STATUS_CHECK, name="ck_integration_credentials_status"),
        CheckConstraint(REVOKED_AT_CHECK, name="ck_integration_credentials_revoked_at"),
    )

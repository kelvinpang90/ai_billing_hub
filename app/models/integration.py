"""Integration API credentials (spec §36, §37, §74.4; design gate #118 v1, AIH-TASK-012),
outbound webhook signing secrets (spec §28; design gate #135 v1, AIH-TASK-019), and the
per-project status webhook delivery cursor (spec §29; design gate #241 v6, AIH-TASK-047).

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
    CHAR,
    BigInteger,
    CheckConstraint,
    Computed,
    DateTime,
    Enum,
    ForeignKeyConstraint,
    Index,
    Integer,
    SmallInteger,
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
    # 摄取端点在响应之后节流写入（AIH-TASK-029：每个凭据行每 60 秒至多一次）。
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
        # `id` 本来就唯一；这个约束只为让 `usage_events` 的三列复合外键成立
        # （AIH-TASK-029 迁移 0017，先例：`uq_projects_id_tenant`）。
        UniqueConstraint(
            "id", "project_id", "tenant_id", name="uq_integration_credentials_id_scope"
        ),
        Index("ix_integration_credentials_project_id", "project_id"),
        CheckConstraint(KEY_VERSION_CHECK, name="ck_integration_credentials_key_version"),
        CheckConstraint(STATUS_CHECK, name="ck_integration_credentials_status"),
        CheckConstraint(REVOKED_AT_CHECK, name="ck_integration_credentials_revoked_at"),
    )


# --- 出站状态 webhook 的签名密钥（AIH-TASK-019，设计闸门 #135 v1） -----------------------


class WebhookSecretStatus(enum.StrEnum):
    """`PENDING` → `ACTIVE` → `RETIRED`；`PENDING` 也可直接 `RETIRED`。`RETIRED` 是终态。

    平台同一时刻只用 `ACTIVE` 那一把签名（设计 §4、§9）。
    """

    PENDING = "PENDING"
    ACTIVE = "ACTIVE"
    RETIRED = "RETIRED"


# ⚠️ 条件与生成列表达式同迁移 0008 逐字一致（空白除外），test_migrations.py 比对两边。
WEBHOOK_KEY_VERSION_CHECK: Final = "key_version >= 1"
WEBHOOK_STATUS_CHECK: Final = "status IN ('PENDING', 'ACTIVE', 'RETIRED')"
WEBHOOK_TIMES_CHECK: Final = (
    "(status = 'PENDING' AND activated_at IS NULL AND retired_at IS NULL)"
    " OR (status = 'ACTIVE' AND activated_at IS NOT NULL AND retired_at IS NULL)"
    " OR (status = 'RETIRED' AND retired_at IS NOT NULL)"
)
# 只有该状态的行才有值，其余是 NULL；唯一索引允许多个 NULL，所以「每个项目至多一个
# ACTIVE、至多一个 PENDING」由数据库保证（设计 §2、§10 假设 1）。
ACTIVE_SLOT_EXPRESSION: Final = "CASE WHEN status = 'ACTIVE' THEN project_id END"
PENDING_SLOT_EXPRESSION: Final = "CASE WHEN status = 'PENDING' THEN project_id END"


class ProjectWebhookSecret(Base):
    """One signing version of a project's outbound status-webhook secret (ADR-0004 §4a 方案 i).

    ⚠️ 与 `integration_credentials` 一样，`key_version`（签名版本，客户在
    `X-Acuven-Key-Version` 里看到）与 `encryption_key_version`（主密钥版本）是两列。

    ⚠️ 行不删除：退役只改状态、不清密文（设计 §6）。
    """

    __tablename__ = "project_webhook_secrets"

    id: Mapped[int] = mapped_column(_PrimaryKey, primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(_ForeignKeyInt, nullable=False)
    tenant_id: Mapped[int] = mapped_column(_ForeignKeyInt, nullable=False)
    key_version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[WebhookSecretStatus] = mapped_column(
        Enum(WebhookSecretStatus, native_enum=False, length=_STATUS_LENGTH),
        nullable=False,
        default=WebhookSecretStatus.PENDING,
    )
    # `encrypt_secret` 的输出，AAD 见 app/services/webhook_signing.py 的 `webhook_secret_aad`。
    encrypted_secret: Mapped[str] = mapped_column(Text, nullable=False)
    encryption_key_version: Mapped[int] = mapped_column(Integer, nullable=False)
    # 生成列（STORED）：只读，ORM 从不写它们。
    active_slot: Mapped[int | None] = mapped_column(
        _ForeignKeyInt, Computed(ACTIVE_SLOT_EXPRESSION, persisted=True)
    )
    pending_slot: Mapped[int | None] = mapped_column(
        _ForeignKeyInt, Computed(PENDING_SLOT_EXPRESSION, persisted=True)
    )
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False)
    activated_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    retired_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)

    __table_args__ = (
        # 数据库保证行的租户就是项目所属的租户（INV-8）。
        ForeignKeyConstraint(
            ["project_id", "tenant_id"],
            ["projects.id", "projects.tenant_id"],
            name="fk_project_webhook_secrets_project",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "project_id", "key_version", name="uq_project_webhook_secrets_key_version"
        ),
        Index("ux_project_webhook_secrets_active_slot", "active_slot", unique=True),
        Index("ux_project_webhook_secrets_pending_slot", "pending_slot", unique=True),
        CheckConstraint(WEBHOOK_KEY_VERSION_CHECK, name="ck_project_webhook_secrets_key_version"),
        CheckConstraint(WEBHOOK_STATUS_CHECK, name="ck_project_webhook_secrets_status"),
        CheckConstraint(WEBHOOK_TIMES_CHECK, name="ck_project_webhook_secrets_times"),
    )


# --- 出站状态 webhook 的投递游标（AIH-TASK-047，设计闸门 #241 v6） -----------------------

# ⚠️ 条件同迁移 0020 逐字一致（空白除外），test_migrations.py 比对两边。
DELIVERY_ATTEMPT_COUNT_CHECK: Final = "attempt_count >= 0"
DELIVERY_VERSION_CHECK: Final = "delivered_version IS NULL OR delivered_version >= 0"
# uuid4 的规范字符串形式。
_LEASE_TOKEN_LENGTH = 36
_ERROR_CODE_LENGTH = 64


class ProjectStatusDelivery(Base):
    """The status webhook cursor of one project: **exactly one row per project** (design §2).

    迁移 0020 给已有项目各插一行，建项目时在同一事务里插入（`app/services/customers.py`）。
    之后的所有路径只 UPDATE、不再插入，所以没有「先查后插」的间隙锁。行从不删除：外键
    `RESTRICT`，项目也不能删。

    投递状态不另存列，由数据推导（§29 的四个状态）：`delivered_version` 等于租户的
    `status_version` 是 `DELIVERED`；落后时按 `attempt_count` 分 `PENDING` / `RETRYING` /
    `FAILED`。领取、发送与结算归投递任务（设计 §8 拆分第 5 项）。
    """

    __tablename__ = "project_status_deliveries"

    project_id: Mapped[int] = mapped_column(_ForeignKeyInt, primary_key=True, autoincrement=False)
    # 领取时直接 JOIN `tenants`；复合外键保证它就是项目所属的租户（INV-8）。
    tenant_id: Mapped[int] = mapped_column(_ForeignKeyInt, nullable=False)
    # 已确认送达的最大版本；空 = 从未送达。
    delivered_version: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # 当前租约正在发送的版本。
    attempted_version: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # 自上次成功以来的连续失败次数。
    attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    next_attempt_at: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False)
    # 领取时写入的 uuid4，结算时作围栏。
    lease_token: Mapped[str | None] = mapped_column(CHAR(_LEASE_TOKEN_LENGTH), nullable=True)
    lease_expires_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    last_attempt_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    delivered_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    last_http_status: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    # 只存错误码，不存地址、响应体与异常文本（设计 §6）。
    last_error_code: Mapped[str | None] = mapped_column(String(_ERROR_CODE_LENGTH), nullable=True)

    __table_args__ = (
        ForeignKeyConstraint(
            ["project_id", "tenant_id"],
            ["projects.id", "projects.tenant_id"],
            name="fk_project_status_deliveries_project",
            ondelete="RESTRICT",
        ),
        Index("ix_project_status_deliveries_next_attempt_at", "next_attempt_at"),
        CheckConstraint(
            DELIVERY_ATTEMPT_COUNT_CHECK, name="ck_project_status_deliveries_attempt_count"
        ),
        CheckConstraint(
            DELIVERY_VERSION_CHECK, name="ck_project_status_deliveries_delivered_version"
        ),
    )

"""Usage events and idempotency conflicts (design gate #176 v8, AIH-TASK-029).

spec §20、§23、§79、§83。两张表：

- `usage_events`：一行是一个被持久接收的用量事件。`event_id` 全局唯一（§23），
  插入优先、由唯一约束 `uq_usage_events_event_id` 仲裁。本任务只写 `RECEIVED`；
  `provider_id` / `model_id`、`error_*`、`processed_at` 由 T-H 写。
- `usage_event_conflicts`：同一 `event_id` 被另一归属或另一指纹重用时记一行，只增。
  原事件一个字节都不改（INV-11）。

⚠️ **归属与形态由数据库保证**（设计 §2「数据库」）：

- `(integration_credential_id, project_id, tenant_id)` → `integration_credentials`
  的同名三列：事件的租户与项目就是凭据所属的租户与项目；
- `(usage_meter_type_id, payload_shape, unit, quantity_kind)` → `usage_meter_types`
  的同名四列：形态、单位、数量类型与计量类型一致；
- `(model_id, provider_id)` → `ai_models(id, provider_id)`：模型属于该供应商。

这些外键与形态 CHECK 只在组成列非空时生效，所以设计逐列列出的必需列全部
`NOT NULL`（MySQL 对含 NULL 的外键不检查、对结果为 UNKNOWN 的 CHECK 放行）。

⚠️ **行永不删除**：两张表都没有删除路径；冲突表由触发器拒绝 UPDATE / DELETE
（迁移 0017，只在 MySQL 上）。

AIH-TASK-032（设计闸门 #181 v2 §2「数据库」，迁移 0018）给 `usage_events` 加了计费快照
（§14 / §79：价格版本、规则、汇率、原币成本、MYR 估算成本、计费额、账本行）与认领列
（`attempt_count`、`next_attempt_at`、`claim_token`、`claimed_at`、`lease_expires_at`）。
写入方只有 app/services/usage_billing.py（认领、处理、失败记录、卡住回收）与
app/services/usage_requeue.py（重新入队）。⚠️ `PROCESSED` 是终态：MySQL 上由迁移 0018 的触发器
拒绝对它的任何 UPDATE；事件与账本行的对应、「重新入队时账本无行」同样由触发器保证。

⚠️ 条件文本与迁移 0017 / 0018 逐字一致（空白除外），test_migrations.py 比对两边。
"""

from __future__ import annotations

import datetime as dt
import enum
from decimal import Decimal
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
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects import mysql
from sqlalchemy.orm import Mapped, mapped_column

# 复合外键指向这些表：import 这些模块，表才在同一个 metadata 里。
from app.models import ai_catalog as _ai_catalog_models  # noqa: F401
from app.models import fx_rates as _fx_rate_models  # noqa: F401
from app.models import integration as _integration_models  # noqa: F401
from app.models import pricing_rules as _pricing_rule_models  # noqa: F401
from app.models import provider_prices as _provider_price_models  # noqa: F401
from app.models import tenancy as _tenancy_models  # noqa: F401
from app.models import wallet as _wallet_models  # noqa: F401
from app.models.ai_catalog import (
    MODEL_CODE_LENGTH,
    PAYLOAD_SHAPE_LENGTH,
    PROVIDER_CODE_LENGTH,
    QUANTITY_KIND_LENGTH,
    UNIT_LENGTH,
    PayloadShape,
    QuantityKind,
)
from app.models.base import MONEY_PRECISION, MONEY_SCALE, Base
from app.models.fx_rates import RATE_PRECISION, RATE_SCALE

# 与 app/models/auth.py 同一个写法（SQLite 只对 INTEGER PRIMARY KEY 自增）。
_PrimaryKey = BigInteger().with_variant(Integer, "sqlite")
_ForeignKeyInt = BigInteger().with_variant(Integer, "sqlite")

_PUBLIC_ID_LENGTH = 36
# 防护令牌：uuid4 的规范写法。
CLAIM_TOKEN_LENGTH: Final = 36
CURRENCY_LENGTH: Final = 3

# ⚠️ 按字节比较（区分大小写、NO PAD）。只在 MySQL 上指定。`unit` 是复合外键的一部分，
# 排序规则必须与 `usage_meter_types.unit` 相同。
TEXT_COLLATION: Final = "utf8mb4_0900_bin"

# 设计 §2 写死的列宽。
EVENT_ID_LENGTH: Final = 64
SCHEMA_VERSION_LENGTH: Final = 8
REQUEST_ID_LENGTH: Final = 128
FINGERPRINT_LENGTH: Final = 64
STATUS_LENGTH: Final = 32
ERROR_CODE_LENGTH: Final = 64
ERROR_MESSAGE_LENGTH: Final = 255
API_KEY_LENGTH: Final = 64
MISMATCH_LENGTH: Final = 32
# `quantity` 是用量不是金额，但同一个精度、同样不经过 float（设计 §2「金额精度」）。
QUANTITY_PRECISION: Final = MONEY_PRECISION
QUANTITY_SCALE: Final = MONEY_SCALE


def _text(length: int) -> String:
    return String(length).with_variant(String(length, collation=TEXT_COLLATION), "mysql")


def _mysql_only_check(condition: str, name: str) -> CheckConstraint:
    """A CHECK that `create_all` emits only on MySQL (the migration builds it there anyway)."""
    return CheckConstraint(condition, name=name).ddl_if(dialect="mysql")


class UsageEventStatus(enum.StrEnum):
    """spec §83 的九个取值。摄取（AIH-TASK-029）只写 `RECEIVED`；其余由计费 worker
    （AIH-TASK-032，app/services/usage_billing.py）写，重新入队把错误状态改回 `RECEIVED`。

    `IDEMPOTENCY_CONFLICT` 留在 CHECK 里但没有写入方：冲突是**请求**的结果，
    不是原事件的状态（设计闸门 #176 §2「幂等与冲突」）。
    """

    RECEIVED = "RECEIVED"
    PROCESSING = "PROCESSING"
    PROCESSED = "PROCESSED"
    PRICING_ERROR = "PRICING_ERROR"
    FX_RATE_ERROR = "FX_RATE_ERROR"
    MODEL_UNKNOWN = "MODEL_UNKNOWN"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    FAILED_RETRYABLE = "FAILED_RETRYABLE"
    FAILED_FINAL = "FAILED_FINAL"


class ConflictMismatch(enum.StrEnum):
    """冲突请求与原事件哪里不同（设计 §2「幂等与冲突」）。"""

    # 归属（租户、项目或 api_key）不同，指纹相同。
    OWNERSHIP = "OWNERSHIP"
    # 归属相同，指纹不同。
    FINGERPRINT = "FINGERPRINT"
    BOTH = "BOTH"


# ⚠️ 条件文本与迁移 0017 逐字一致（空白除外），test_migrations.py 比对两边。
# 形态与字段组：显式写出两组条件，不用链式比较（v2：链式 `=` 挡不住交错空值）。
SHAPE_FIELDS_CHECK: Final = (
    "(payload_shape = 'LLM_TOKEN_FIELDS'"
    " AND input_tokens IS NOT NULL AND output_tokens IS NOT NULL"
    " AND cache_creation_input_tokens IS NOT NULL AND cache_read_input_tokens IS NOT NULL"
    " AND quantity IS NULL)"
    " OR (payload_shape = 'QUANTITY'"
    " AND input_tokens IS NULL AND output_tokens IS NULL"
    " AND cache_creation_input_tokens IS NULL AND cache_read_input_tokens IS NULL"
    " AND quantity IS NOT NULL)"
)
# 整数类型的数量不许有小数部分。⚠️ 只在 MySQL 上建：SQLite 不一定编译了 FLOOR，
# 而 CHECK 里的未知函数要到插入时才报错。服务层同样校验（设计 §2 字段规则）。
INTEGER_QUANTITY_CHECK: Final = (
    "quantity_kind <> 'INTEGER' OR quantity IS NULL OR quantity = FLOOR(quantity)"
)
INPUT_TOKENS_CHECK: Final = "input_tokens IS NULL OR input_tokens >= 0"
OUTPUT_TOKENS_CHECK: Final = "output_tokens IS NULL OR output_tokens >= 0"
CACHE_CREATION_TOKENS_CHECK: Final = (
    "cache_creation_input_tokens IS NULL OR cache_creation_input_tokens >= 0"
)
CACHE_READ_TOKENS_CHECK: Final = "cache_read_input_tokens IS NULL OR cache_read_input_tokens >= 0"
QUANTITY_CHECK: Final = "quantity IS NULL OR quantity >= 0"
EVENT_STATUS_CHECK: Final = (
    "status IN ('RECEIVED', 'PROCESSING', 'PROCESSED', 'PRICING_ERROR', 'FX_RATE_ERROR',"
    " 'MODEL_UNKNOWN', 'IDEMPOTENCY_CONFLICT', 'FAILED_RETRYABLE', 'FAILED_FINAL')"
)
# 只知道模型而不知道供应商的行不可能出现（§79 的目录引用）。
CATALOG_REFERENCE_CHECK: Final = "model_id IS NULL OR provider_id IS NOT NULL"
MISMATCH_CHECK: Final = "mismatch IN ('OWNERSHIP', 'FINGERPRINT', 'BOTH')"

# --- 迁移 0018（AIH-TASK-032，设计闸门 #181 v2 §2「数据库」）--------------------------------
# 已处理 ⇒ 快照齐全；MYR 原币时两个汇率列都为空，否则都非空。逐列写 `IS NOT NULL`，不用行构造器。
PROCESSED_SNAPSHOT_CHECK: Final = (
    "status <> 'PROCESSED'"
    " OR (provider_id IS NOT NULL AND model_id IS NOT NULL"
    " AND provider_price_version_id IS NOT NULL AND pricing_rule_id IS NOT NULL"
    " AND provider_source_currency IS NOT NULL AND provider_source_cost IS NOT NULL"
    " AND estimated_provider_cost_myr IS NOT NULL AND billable_cost IS NOT NULL"
    " AND processed_at IS NOT NULL"
    " AND ((provider_source_currency = 'MYR'"
    " AND fx_rate_version_id IS NULL AND fx_rate_applied IS NULL)"
    " OR (provider_source_currency <> 'MYR'"
    " AND fx_rate_version_id IS NOT NULL AND fx_rate_applied IS NOT NULL)))"
)
# 计费额大于 0 的已处理事件必有账本行；0 元事件不写账本（账本 CHECK 要求 AI_USAGE < 0）。
PROCESSED_LEDGER_CHECK: Final = (
    "status <> 'PROCESSED' OR billable_cost = 0 OR wallet_transaction_id IS NOT NULL"
)
# 未处理 ⇒ 没有财务链接。
UNPROCESSED_LEDGER_CHECK: Final = "status = 'PROCESSED' OR wallet_transaction_id IS NULL"
BILLABLE_COST_CHECK: Final = "billable_cost IS NULL OR billable_cost >= 0"
PROVIDER_SOURCE_COST_CHECK: Final = "provider_source_cost IS NULL OR provider_source_cost >= 0"
ESTIMATED_COST_CHECK: Final = (
    "estimated_provider_cost_myr IS NULL OR estimated_provider_cost_myr >= 0"
)
# 认领字段与状态一致：PROCESSING 时三列齐全且租约晚于认领时刻，否则三列都为空。逐列写，
# 不用布尔等式比较（`(a) = (b)` 遇到 NULL 得 UNKNOWN，CHECK 会放行）。
CLAIM_FIELDS_CHECK: Final = (
    "(status = 'PROCESSING'"
    " AND claim_token IS NOT NULL AND claimed_at IS NOT NULL AND lease_expires_at IS NOT NULL"
    " AND claimed_at < lease_expires_at)"
    " OR (status <> 'PROCESSING'"
    " AND claim_token IS NULL AND claimed_at IS NULL AND lease_expires_at IS NULL)"
)


class UsageEvent(Base):
    """One durably received usage event (spec §79).

    归属（`tenant_id`、`project_id`、`integration_credential_id`）**只来自凭据行**，
    载荷里的 `tenant_id` / `project_id` 只作比对、从不写进这里（设计 §6）。
    """

    __tablename__ = "usage_events"

    id: Mapped[int] = mapped_column(_PrimaryKey, primary_key=True, autoincrement=True)
    # 管理端以后用（T-J）；冲突审计的 entity_id。
    public_id: Mapped[str] = mapped_column(CHAR(_PUBLIC_ID_LENGTH), nullable=False)
    # UUIDv7 或 ULID 的规范写法（设计 §2 字段规则），按字节唯一。
    event_id: Mapped[str] = mapped_column(_text(EVENT_ID_LENGTH), nullable=False)
    schema_version: Mapped[str] = mapped_column(String(SCHEMA_VERSION_LENGTH), nullable=False)
    tenant_id: Mapped[int] = mapped_column(_ForeignKeyInt, nullable=False)
    project_id: Mapped[int] = mapped_column(_ForeignKeyInt, nullable=False)
    # 接收时用的那一行（含版本）。
    integration_credential_id: Mapped[int] = mapped_column(_ForeignKeyInt, nullable=False)
    # AI 调用的 id。
    request_id: Mapped[str] = mapped_column(_text(REQUEST_ID_LENGTH), nullable=False)
    conversation_id: Mapped[str | None] = mapped_column(_text(REQUEST_ID_LENGTH), nullable=True)
    # 上报的原始字符串（计划第 1 节第 5 条）；T-H 解析成下面两列。
    provider_code_raw: Mapped[str] = mapped_column(_text(PROVIDER_CODE_LENGTH), nullable=False)
    model_code_raw: Mapped[str] = mapped_column(_text(MODEL_CODE_LENGTH), nullable=False)
    # §79 的目录引用。本任务写入时为空。
    provider_id: Mapped[int | None] = mapped_column(_ForeignKeyInt, nullable=True)
    model_id: Mapped[int | None] = mapped_column(_ForeignKeyInt, nullable=True)
    usage_meter_type_id: Mapped[int] = mapped_column(_ForeignKeyInt, nullable=False)
    payload_shape: Mapped[PayloadShape] = mapped_column(
        Enum(PayloadShape, native_enum=False, length=PAYLOAD_SHAPE_LENGTH), nullable=False
    )
    quantity_kind: Mapped[QuantityKind] = mapped_column(
        Enum(QuantityKind, native_enum=False, length=QUANTITY_KIND_LENGTH), nullable=False
    )
    # 列名与载荷字段、与 025 的 `quantity_field` 一致（§79 写作 cache_creation_tokens /
    # cache_read_tokens，§74 允许按仓库约定命名）。
    input_tokens: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    cache_creation_input_tokens: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    cache_read_input_tokens: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    quantity: Mapped[Decimal | None] = mapped_column(
        Numeric(QUANTITY_PRECISION, QUANTITY_SCALE, asdecimal=True), nullable=True
    )
    # 计量类型的单位：`QUANTITY` 形态即上报的 `unit`，`LLM_TOKEN_FIELDS` 形态是 `TOKEN`。
    unit: Mapped[str] = mapped_column(_text(UNIT_LENGTH), nullable=False)
    payload_fingerprint: Mapped[str] = mapped_column(CHAR(FINGERPRINT_LENGTH), nullable=False)
    status: Mapped[UsageEventStatus] = mapped_column(
        Enum(UsageEventStatus, native_enum=False, length=STATUS_LENGTH),
        nullable=False,
        default=UsageEventStatus.RECEIVED,
        server_default=UsageEventStatus.RECEIVED.value,
    )
    error_code: Mapped[str | None] = mapped_column(String(ERROR_CODE_LENGTH), nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(ERROR_MESSAGE_LENGTH), nullable=True)
    # 保留微秒：别名与版本边界按 `[from, to)` 比较（025 §7「边界时刻」）。
    occurred_at: Mapped[dt.datetime] = mapped_column(
        DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql"), nullable=False
    )
    # 整秒，与其他服务端时间一致。
    received_at: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False)
    processed_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False)

    # --- 计费快照（§14、§79；迁移 0018）。计价成功时写入，之后不再变（PROCESSED 不可改）。
    provider_price_version_id: Mapped[int | None] = mapped_column(_ForeignKeyInt, nullable=True)
    pricing_rule_id: Mapped[int | None] = mapped_column(_ForeignKeyInt, nullable=True)
    # MYR 原币时为空。
    fx_rate_version_id: Mapped[int | None] = mapped_column(_ForeignKeyInt, nullable=True)
    provider_source_currency: Mapped[str | None] = mapped_column(
        CHAR(CURRENCY_LENGTH), nullable=True
    )
    provider_source_cost: Mapped[Decimal | None] = mapped_column(
        Numeric(MONEY_PRECISION, MONEY_SCALE, asdecimal=True), nullable=True
    )
    # 汇率原值，DECIMAL(24,10)（与 `fx_rate_versions.rate` 同精度）；MYR 原币时为空。
    fx_rate_applied: Mapped[Decimal | None] = mapped_column(
        Numeric(RATE_PRECISION, RATE_SCALE, asdecimal=True), nullable=True
    )
    estimated_provider_cost_myr: Mapped[Decimal | None] = mapped_column(
        Numeric(MONEY_PRECISION, MONEY_SCALE, asdecimal=True), nullable=True
    )
    # 含税（ADR-0008）。
    billable_cost: Mapped[Decimal | None] = mapped_column(
        Numeric(MONEY_PRECISION, MONEY_SCALE, asdecimal=True), nullable=True
    )
    # 那一行 `AI_USAGE` 账本；计费额为 0 时为空。
    wallet_transaction_id: Mapped[int | None] = mapped_column(_ForeignKeyInt, nullable=True)

    # --- 认领（§82、§83、§110；迁移 0018）。离开 PROCESSING 时三列认领字段一起清空。
    # 认领时 +1；重新入队清零。
    attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    # 失败退避后最早何时再认领（整秒）。
    next_attempt_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    # 当前认领的防护令牌：处理事务锁住事件行后核对它，不是自己的就放弃。
    claim_token: Mapped[str | None] = mapped_column(CHAR(CLAIM_TOKEN_LENGTH), nullable=True)
    claimed_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    lease_expires_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)

    __table_args__ = (
        ForeignKeyConstraint(
            ["project_id", "tenant_id"],
            ["projects.id", "projects.tenant_id"],
            name="fk_usage_events_project",
            ondelete="RESTRICT",
        ),
        # 事件的租户与项目就是凭据所属的租户与项目（INV-8）。
        ForeignKeyConstraint(
            ["integration_credential_id", "project_id", "tenant_id"],
            [
                "integration_credentials.id",
                "integration_credentials.project_id",
                "integration_credentials.tenant_id",
            ],
            name="fk_usage_events_credential",
            ondelete="RESTRICT",
        ),
        # 形态、单位、数量类型与所属计量类型一致。
        ForeignKeyConstraint(
            ["usage_meter_type_id", "payload_shape", "unit", "quantity_kind"],
            [
                "usage_meter_types.id",
                "usage_meter_types.payload_shape",
                "usage_meter_types.unit",
                "usage_meter_types.quantity_kind",
            ],
            name="fk_usage_events_meter_type",
            ondelete="RESTRICT",
        ),
        # 模型必然属于该供应商。
        ForeignKeyConstraint(
            ["model_id", "provider_id"],
            ["ai_models.id", "ai_models.provider_id"],
            name="fk_usage_events_model",
            ondelete="RESTRICT",
        ),
        # 只知道供应商、模型未知时也受约束。
        ForeignKeyConstraint(
            ["provider_id"],
            ["ai_providers.id"],
            name="fk_usage_events_provider",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("public_id", name="uq_usage_events_public_id"),
        # 全局 event_id 幂等的最终仲裁（§23）。
        UniqueConstraint("event_id", name="uq_usage_events_event_id"),
        # T-H 扫描 RECEIVED（REQ-INGEST-002）。
        Index("ix_usage_events_status_occurred", "status", "occurred_at"),
        Index("ix_usage_events_tenant_id", "tenant_id"),
        # 设计 §2 的 `project_id`、`model_id` 索引取复合外键需要的形式（首列就是该列），
        # 一个索引同时服务查询与外键，不让 MySQL 再自动建一个。
        Index("ix_usage_events_project_id", "project_id", "tenant_id"),
        Index("ix_usage_events_model_id", "model_id", "provider_id"),
        Index("ix_usage_events_provider_id", "provider_id"),
        Index("ix_usage_events_conversation_id", "conversation_id"),
        Index("ix_usage_events_request_id", "request_id"),
        Index("ix_usage_events_occurred_at", "occurred_at"),
        # 另外两个复合外键在子表上的索引，显式建出来（不让 MySQL 自动起名）。
        Index(
            "ix_usage_events_credential",
            "integration_credential_id",
            "project_id",
            "tenant_id",
        ),
        Index(
            "ix_usage_events_meter_type",
            "usage_meter_type_id",
            "payload_shape",
            "unit",
            "quantity_kind",
        ),
        CheckConstraint(SHAPE_FIELDS_CHECK, name="ck_usage_events_shape_fields"),
        _mysql_only_check(INTEGER_QUANTITY_CHECK, "ck_usage_events_integer_quantity"),
        CheckConstraint(INPUT_TOKENS_CHECK, name="ck_usage_events_input_tokens"),
        CheckConstraint(OUTPUT_TOKENS_CHECK, name="ck_usage_events_output_tokens"),
        CheckConstraint(
            CACHE_CREATION_TOKENS_CHECK, name="ck_usage_events_cache_creation_input_tokens"
        ),
        CheckConstraint(CACHE_READ_TOKENS_CHECK, name="ck_usage_events_cache_read_input_tokens"),
        CheckConstraint(QUANTITY_CHECK, name="ck_usage_events_quantity"),
        CheckConstraint(EVENT_STATUS_CHECK, name="ck_usage_events_status"),
        CheckConstraint(CATALOG_REFERENCE_CHECK, name="ck_usage_events_catalog_reference"),
        # --- 迁移 0018 ---
        ForeignKeyConstraint(
            ["provider_price_version_id"],
            ["provider_price_versions.id"],
            name="fk_usage_events_price_version",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["pricing_rule_id"],
            ["pricing_rules.id"],
            name="fk_usage_events_pricing_rule",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["fx_rate_version_id"],
            ["fx_rate_versions.id"],
            name="fk_usage_events_fx_rate_version",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["wallet_transaction_id"],
            ["wallet_transactions.id"],
            name="fk_usage_events_wallet_transaction",
            ondelete="RESTRICT",
        ),
        # 一行账本至多对应一个事件（账本本身已对 (USAGE_EVENT, event_id) 唯一）。
        UniqueConstraint("wallet_transaction_id", name="uq_usage_events_wallet_transaction_id"),
        # 认领（Ⓐ）与卡住回收的扫描。
        Index("ix_usage_events_claim", "status", "next_attempt_at", "id"),
        Index("ix_usage_events_lease", "status", "lease_expires_at"),
        # 三个快照外键在子表上的索引，显式建出来（不让 MySQL 自动起名）。
        Index("ix_usage_events_price_version_id", "provider_price_version_id"),
        Index("ix_usage_events_pricing_rule_id", "pricing_rule_id"),
        Index("ix_usage_events_fx_rate_version_id", "fx_rate_version_id"),
        # 只在 MySQL 上建：SQLite 上的既有摄取测试直接把事件改成 PROCESSED、不带快照
        # （test_usage_ingest_api.py）；MySQL 上的覆盖在 test_usage_billing_concurrency.py。
        _mysql_only_check(PROCESSED_SNAPSHOT_CHECK, "ck_usage_events_processed_snapshot"),
        CheckConstraint(PROCESSED_LEDGER_CHECK, name="ck_usage_events_processed_ledger"),
        CheckConstraint(UNPROCESSED_LEDGER_CHECK, name="ck_usage_events_unprocessed_ledger"),
        CheckConstraint(BILLABLE_COST_CHECK, name="ck_usage_events_billable_cost"),
        CheckConstraint(PROVIDER_SOURCE_COST_CHECK, name="ck_usage_events_provider_source_cost"),
        CheckConstraint(ESTIMATED_COST_CHECK, name="ck_usage_events_estimated_cost"),
        CheckConstraint(CLAIM_FIELDS_CHECK, name="ck_usage_events_claim_fields"),
    )


class UsageEventConflict(Base):
    """One rejected reuse of an `event_id`. Append-only.

    同一 `(event_id, 请求方 api_key, 指纹)` 只记一次：客户端反复重试同一个冲突请求
    不会刷爆这张表（设计 §2）。所有列 `NOT NULL` —— 去重唯一约束遇到 NULL 会失效。
    """

    __tablename__ = "usage_event_conflicts"

    id: Mapped[int] = mapped_column(_PrimaryKey, primary_key=True, autoincrement=True)
    # 被撞的原事件。
    usage_event_id: Mapped[int] = mapped_column(_ForeignKeyInt, nullable=False)
    event_id: Mapped[str] = mapped_column(_text(EVENT_ID_LENGTH), nullable=False)
    # 冲突请求用的凭据行。
    integration_credential_id: Mapped[int] = mapped_column(_ForeignKeyInt, nullable=False)
    # 冲突请求的 `public_api_key`（去重键的一部分）。
    api_key: Mapped[str] = mapped_column(_text(API_KEY_LENGTH), nullable=False)
    payload_fingerprint: Mapped[str] = mapped_column(CHAR(FINGERPRINT_LENGTH), nullable=False)
    mismatch: Mapped[ConflictMismatch] = mapped_column(
        Enum(ConflictMismatch, native_enum=False, length=MISMATCH_LENGTH), nullable=False
    )
    received_at: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False)

    __table_args__ = (
        ForeignKeyConstraint(
            ["usage_event_id"],
            ["usage_events.id"],
            name="fk_usage_event_conflicts_event",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["integration_credential_id"],
            ["integration_credentials.id"],
            name="fk_usage_event_conflicts_credential",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "event_id", "api_key", "payload_fingerprint", name="uq_usage_event_conflicts_dedupe"
        ),
        CheckConstraint(MISMATCH_CHECK, name="ck_usage_event_conflicts_mismatch"),
    )


__all__ = [
    "BILLABLE_COST_CHECK",
    "CACHE_CREATION_TOKENS_CHECK",
    "CACHE_READ_TOKENS_CHECK",
    "CATALOG_REFERENCE_CHECK",
    "CLAIM_FIELDS_CHECK",
    "CLAIM_TOKEN_LENGTH",
    "CURRENCY_LENGTH",
    "ESTIMATED_COST_CHECK",
    "EVENT_ID_LENGTH",
    "EVENT_STATUS_CHECK",
    "INPUT_TOKENS_CHECK",
    "INTEGER_QUANTITY_CHECK",
    "MISMATCH_CHECK",
    "OUTPUT_TOKENS_CHECK",
    "PROCESSED_LEDGER_CHECK",
    "PROCESSED_SNAPSHOT_CHECK",
    "PROVIDER_SOURCE_COST_CHECK",
    "QUANTITY_CHECK",
    "QUANTITY_PRECISION",
    "QUANTITY_SCALE",
    "SHAPE_FIELDS_CHECK",
    "TEXT_COLLATION",
    "UNPROCESSED_LEDGER_CHECK",
    "ConflictMismatch",
    "UsageEvent",
    "UsageEventConflict",
    "UsageEventStatus",
]

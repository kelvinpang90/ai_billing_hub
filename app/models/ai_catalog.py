"""AI provider, model and usage meter catalog (design gate #163 v4, AIH-TASK-025).

spec §11、§12、§15.1、§58、§74、§84。五张表：

- `usage_meter_types` / `usage_meter_components`：计量类型与它的计价分量。
  类型按**上报形态**（`payload_shape`）分两种。分量的形态由复合外键
  `(meter_type_id, payload_shape)` 保证与所属类型一致，取数字段由 CHECK 限定，
  `(meter_type_id, quantity_field)` 唯一 —— 同一批数量不会被算两次（设计 §3 ①）；
- `ai_providers` / `ai_models`：供应商与模型，`code` 与上报事件的
  `provider` / `model` 精确比较；
- `ai_model_aliases`：§84 的「映射」，按 `occurred_at` 分段。一个字符串的各段
  首尾相接、不重叠，至多一段未截断（生成列 `open_slot` + 唯一索引）。

⚠️ **代码列一律 `utf8mb4_0900_bin`**（只在 MySQL 上指定）：库默认的
`utf8mb4_0900_ai_ci` 会把 `GPT-4o` 与 `gpt-4o` 当成同一个值。

⚠️ **行永不删除**：价格版本与用量事件会永久引用它们（INV-6）。外键都是
`RESTRICT`，repository 里也没有删除函数。停用只改状态。

⚠️ 条件文本、生成列表达式与种子同迁移 0012 逐字一致（空白除外），
test_migrations.py 比对两边。
"""

from __future__ import annotations

import datetime as dt
import enum
from typing import Final, NamedTuple

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
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

# 与 app/models/auth.py 同一个写法（SQLite 只对 INTEGER PRIMARY KEY 自增）。
_PrimaryKey = BigInteger().with_variant(Integer, "sqlite")
_ForeignKeyInt = BigInteger().with_variant(Integer, "sqlite")

_PUBLIC_ID_LENGTH = 36

# ⚠️ 按字节比较（区分大小写、NO PAD）。只在 MySQL 上指定。
CODE_COLLATION: Final = "utf8mb4_0900_bin"

# 设计 §2 写死的列宽。
METER_TYPE_CODE_LENGTH: Final = 32
COMPONENT_CODE_LENGTH: Final = 64
UNIT_LENGTH: Final = 16
QUANTITY_FIELD_LENGTH: Final = 64
PROVIDER_CODE_LENGTH: Final = 64
MODEL_CODE_LENGTH: Final = 128
DISPLAY_NAME_LENGTH: Final = 255
PAYLOAD_SHAPE_LENGTH: Final = 32
QUANTITY_KIND_LENGTH: Final = 16
STATUS_LENGTH: Final = 16


def _code(length: int) -> String:
    return String(length).with_variant(String(length, collation=CODE_COLLATION), "mysql")


class PayloadShape(enum.StrEnum):
    """上报形态（设计 §2）。摄取（T-E）与计价（T-G）只认形态、不认具体类型。"""

    # 四个 token 字段，每个分量对应一个。只有迁移种子（`LLM_TOKEN`）。
    LLM_TOKEN_FIELDS = "LLM_TOKEN_FIELDS"
    # `quantity` + `unit`，唯一一个分量取 `quantity`。管理员新建的类型都是它。
    QUANTITY = "QUANTITY"


class QuantityKind(enum.StrEnum):
    INTEGER = "INTEGER"
    DECIMAL = "DECIMAL"


class CatalogStatus(enum.StrEnum):
    """停用只影响「以后选不选它」，不影响计费（设计 §2「停用的含义」）。

    `ACTIVE ↔ RETIRED` 双向都允许，没有终态。
    """

    ACTIVE = "ACTIVE"
    RETIRED = "RETIRED"


# `QUANTITY` 形态唯一的取数字段。
QUANTITY_FIELD: Final = "quantity"
# `LLM_TOKEN_FIELDS` 形态的四个取数字段（spec §11 的上报载荷）。
TOKEN_FIELDS: Final = (
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)

# ⚠️ 条件文本与迁移 0012 逐字一致（空白除外），test_migrations.py 比对两边。
PAYLOAD_SHAPE_CHECK: Final = "payload_shape IN ('LLM_TOKEN_FIELDS', 'QUANTITY')"
QUANTITY_KIND_CHECK: Final = "quantity_kind IN ('INTEGER', 'DECIMAL')"
STATUS_CHECK: Final = "status IN ('ACTIVE', 'RETIRED')"
# `LLM_TOKEN_FIELDS` 的类型：单位必须是 TOKEN、数量必须是整数（设计 §2）。
TOKEN_SHAPE_CHECK: Final = (
    "payload_shape <> 'LLM_TOKEN_FIELDS' OR (unit = 'TOKEN' AND quantity_kind = 'INTEGER')"
)
# 取数字段与形态的搭配：`QUANTITY` 只能取 `quantity`，`LLM_TOKEN_FIELDS` 只能取
# 四个 token 字段之一。加上 `(meter_type_id, quantity_field)` 唯一，
# `QUANTITY` 类型至多一个分量。
QUANTITY_FIELD_CHECK: Final = (
    "(payload_shape = 'QUANTITY' AND quantity_field = 'quantity')"
    " OR (payload_shape = 'LLM_TOKEN_FIELDS' AND quantity_field IN ('input_tokens',"
    " 'output_tokens', 'cache_creation_input_tokens', 'cache_read_input_tokens'))"
)
# 段不能倒置；任一端为 NULL（「一直以来」/「仍生效」）时不比。
ALIAS_PERIOD_CHECK: Final = (
    "effective_from IS NULL OR effective_to IS NULL OR effective_from < effective_to"
)
# 只有未截断的段有值，其余是 NULL；唯一索引允许多个 NULL，所以「一个字符串
# 至多一段未截断」由数据库保证（写法同 `project_webhook_secrets.active_slot`）。
OPEN_SLOT_EXPRESSION: Final = "CASE WHEN effective_to IS NULL THEN 1 END"


class SeedComponent(NamedTuple):
    component_code: str
    quantity_field: str


class SeedMeterType(NamedTuple):
    code: str
    display_name: str
    payload_shape: PayloadShape
    unit: str
    quantity_kind: QuantityKind
    components: tuple[SeedComponent, ...]


def _single(code: str, display_name: str, unit: str, kind: QuantityKind) -> SeedMeterType:
    component = SeedComponent(code, QUANTITY_FIELD)
    return SeedMeterType(code, display_name, PayloadShape.QUANTITY, unit, kind, (component,))


# 迁移 0012 写入的种子：9 个类型（spec §12）、12 个分量（spec §15.1）。
# ⚠️ 迁移里是一份冻结的副本（迁移不 import 应用代码）；test_migrations.py
# 逐行比对迁移、这里与库里三方（设计 §5）。
SEED_METER_TYPES: Final = (
    SeedMeterType(
        "LLM_TOKEN",
        "LLM tokens",
        PayloadShape.LLM_TOKEN_FIELDS,
        "TOKEN",
        QuantityKind.INTEGER,
        (
            SeedComponent("LLM_INPUT_TOKEN", "input_tokens"),
            SeedComponent("LLM_OUTPUT_TOKEN", "output_tokens"),
            SeedComponent("LLM_CACHE_WRITE_TOKEN", "cache_creation_input_tokens"),
            SeedComponent("LLM_CACHE_READ_TOKEN", "cache_read_input_tokens"),
        ),
    ),
    _single("EMBEDDING_TOKEN", "Embedding tokens", "TOKEN", QuantityKind.INTEGER),
    _single("AUDIO_SECOND", "Audio seconds", "SECOND", QuantityKind.DECIMAL),
    _single("AUDIO_MINUTE", "Audio minutes", "MINUTE", QuantityKind.DECIMAL),
    _single("TTS_CHARACTER", "TTS characters", "CHARACTER", QuantityKind.INTEGER),
    _single("IMAGE_GENERATION", "Image generations", "IMAGE", QuantityKind.INTEGER),
    _single("OCR_PAGE", "OCR pages", "PAGE", QuantityKind.INTEGER),
    _single("DOCUMENT_PAGE", "Document pages", "PAGE", QuantityKind.INTEGER),
    _single("CUSTOM", "Custom units", "UNIT", QuantityKind.DECIMAL),
)


def _status_column() -> Mapped[CatalogStatus]:
    return mapped_column(
        Enum(CatalogStatus, native_enum=False, length=STATUS_LENGTH),
        nullable=False,
        default=CatalogStatus.ACTIVE,
        server_default=CatalogStatus.ACTIVE.value,
    )


def _payload_shape_column() -> Mapped[PayloadShape]:
    return mapped_column(
        Enum(PayloadShape, native_enum=False, length=PAYLOAD_SHAPE_LENGTH), nullable=False
    )


class UsageMeterType(Base):
    """A meter type: the `usage_type` of a usage event (spec §12).

    `code`、`payload_shape`、`unit`、`quantity_kind` 建后不可改（改单位等于重新
    解释历史数量）；只有 `display_name` 与 `status` 可改。
    """

    __tablename__ = "usage_meter_types"

    id: Mapped[int] = mapped_column(_PrimaryKey, primary_key=True, autoincrement=True)
    public_id: Mapped[str] = mapped_column(CHAR(_PUBLIC_ID_LENGTH), nullable=False)
    code: Mapped[str] = mapped_column(_code(METER_TYPE_CODE_LENGTH), nullable=False)
    display_name: Mapped[str] = mapped_column(String(DISPLAY_NAME_LENGTH), nullable=False)
    payload_shape: Mapped[PayloadShape] = _payload_shape_column()
    # 上报的 `unit` 必须与它相等（T-E）。
    unit: Mapped[str] = mapped_column(_code(UNIT_LENGTH), nullable=False)
    quantity_kind: Mapped[QuantityKind] = mapped_column(
        Enum(QuantityKind, native_enum=False, length=QUANTITY_KIND_LENGTH), nullable=False
    )
    status: Mapped[CatalogStatus] = _status_column()
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False)

    __table_args__ = (
        UniqueConstraint("public_id", name="uq_usage_meter_types_public_id"),
        UniqueConstraint("code", name="uq_usage_meter_types_code"),
        # `id` 本来就唯一；这个约束只为让分量表的复合外键成立
        # （先例：`uq_projects_id_tenant`）。
        UniqueConstraint("id", "payload_shape", name="uq_usage_meter_types_id_shape"),
        # 同上，为 `usage_events` 的四列复合外键（AIH-TASK-029 迁移 0017）：事件记下的
        # 形态、单位、数量类型由数据库保证与所属类型一致。
        UniqueConstraint(
            "id",
            "payload_shape",
            "unit",
            "quantity_kind",
            name="uq_usage_meter_types_id_shape_unit_kind",
        ),
        CheckConstraint(PAYLOAD_SHAPE_CHECK, name="ck_usage_meter_types_payload_shape"),
        CheckConstraint(QUANTITY_KIND_CHECK, name="ck_usage_meter_types_quantity_kind"),
        CheckConstraint(STATUS_CHECK, name="ck_usage_meter_types_status"),
        CheckConstraint(TOKEN_SHAPE_CHECK, name="ck_usage_meter_types_token_shape"),
    )


class UsageMeterComponent(Base):
    """A priced component of a meter type (spec §15.1). No public id, no status.

    T-B 的价格分量、T-D 的 FIXED_RATE 分量按 `component_code` 挂在这里；T-G 按
    `quantity_field` 取数。「每个类型至少一个分量」数据库表达不了：由「建类型与
    建分量同一事务」保证。
    """

    __tablename__ = "usage_meter_components"

    id: Mapped[int] = mapped_column(_PrimaryKey, primary_key=True, autoincrement=True)
    meter_type_id: Mapped[int] = mapped_column(_ForeignKeyInt, nullable=False)
    payload_shape: Mapped[PayloadShape] = _payload_shape_column()
    component_code: Mapped[str] = mapped_column(_code(COMPONENT_CODE_LENGTH), nullable=False)
    quantity_field: Mapped[str] = mapped_column(String(QUANTITY_FIELD_LENGTH), nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False)

    __table_args__ = (
        # 分量的形态由数据库保证与所属类型一致（设计 §2）。
        ForeignKeyConstraint(
            ["meter_type_id", "payload_shape"],
            ["usage_meter_types.id", "usage_meter_types.payload_shape"],
            name="fk_usage_meter_components_type",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("component_code", name="uq_usage_meter_components_code"),
        # 同一类型下一个字段只对应一个分量：同一批数量不会被算两次。
        UniqueConstraint(
            "meter_type_id", "quantity_field", name="uq_usage_meter_components_type_field"
        ),
        # 复合外键在子表上的索引，显式建出来（不让 MySQL 自动起名）。
        Index("ix_usage_meter_components_type_shape", "meter_type_id", "payload_shape"),
        CheckConstraint(PAYLOAD_SHAPE_CHECK, name="ck_usage_meter_components_payload_shape"),
        CheckConstraint(QUANTITY_FIELD_CHECK, name="ck_usage_meter_components_quantity_field"),
    )


class AiProvider(Base):
    """An AI provider; `code` is compared exactly with the `provider` of a usage event.

    ⚠️ 这一行也是解析与改映射的串行点：改映射 `SELECT … FOR UPDATE`，
    解析 `FOR SHARE`（设计 §2「时间」）。
    """

    __tablename__ = "ai_providers"

    id: Mapped[int] = mapped_column(_PrimaryKey, primary_key=True, autoincrement=True)
    public_id: Mapped[str] = mapped_column(CHAR(_PUBLIC_ID_LENGTH), nullable=False)
    code: Mapped[str] = mapped_column(_code(PROVIDER_CODE_LENGTH), nullable=False)
    display_name: Mapped[str] = mapped_column(String(DISPLAY_NAME_LENGTH), nullable=False)
    status: Mapped[CatalogStatus] = _status_column()
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False)

    __table_args__ = (
        UniqueConstraint("public_id", name="uq_ai_providers_public_id"),
        UniqueConstraint("code", name="uq_ai_providers_code"),
        CheckConstraint(STATUS_CHECK, name="ck_ai_providers_status"),
    )


class AiModel(Base):
    """A model of one provider; `code` is compared exactly with the `model` of an event."""

    __tablename__ = "ai_models"

    id: Mapped[int] = mapped_column(_PrimaryKey, primary_key=True, autoincrement=True)
    public_id: Mapped[str] = mapped_column(CHAR(_PUBLIC_ID_LENGTH), nullable=False)
    provider_id: Mapped[int] = mapped_column(_ForeignKeyInt, nullable=False)
    code: Mapped[str] = mapped_column(_code(MODEL_CODE_LENGTH), nullable=False)
    display_name: Mapped[str] = mapped_column(String(DISPLAY_NAME_LENGTH), nullable=False)
    status: Mapped[CatalogStatus] = _status_column()
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False)

    __table_args__ = (
        ForeignKeyConstraint(
            ["provider_id"],
            ["ai_providers.id"],
            name="fk_ai_models_provider",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("public_id", name="uq_ai_models_public_id"),
        UniqueConstraint("provider_id", "code", name="uq_ai_models_provider_code"),
        # `id` 本来就唯一；这个约束只为让别名表的复合外键成立。
        UniqueConstraint("id", "provider_id", name="uq_ai_models_id_provider"),
        CheckConstraint(STATUS_CHECK, name="ck_ai_models_status"),
    )


class AiModelAlias(Base):
    """One segment: `alias` points at `model_id` for `[effective_from, effective_to)`.

    `effective_from IS NULL` = 「一直以来」，只有一个字符串的第一段是它；
    `effective_to IS NULL` = 仍生效。段写下之后只改一次：把 `effective_to` 从 NULL
    改成一个时刻（截断），同时写 `closed_at`。已有的段从不改指向、从不往过去
    延伸（设计 §2）。
    """

    __tablename__ = "ai_model_aliases"

    id: Mapped[int] = mapped_column(_PrimaryKey, primary_key=True, autoincrement=True)
    public_id: Mapped[str] = mapped_column(CHAR(_PUBLIC_ID_LENGTH), nullable=False)
    provider_id: Mapped[int] = mapped_column(_ForeignKeyInt, nullable=False)
    model_id: Mapped[int] = mapped_column(_ForeignKeyInt, nullable=False)
    alias: Mapped[str] = mapped_column(_code(MODEL_CODE_LENGTH), nullable=False)
    effective_from: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    effective_to: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    # 生成列（STORED）：只读，ORM 从不写它。
    open_slot: Mapped[int | None] = mapped_column(
        Integer, Computed(OPEN_SLOT_EXPRESSION, persisted=True)
    )
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False)
    closed_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)

    __table_args__ = (
        # 别名只能指向同一供应商的模型，由数据库保证。
        ForeignKeyConstraint(
            ["model_id", "provider_id"],
            ["ai_models.id", "ai_models.provider_id"],
            name="fk_ai_model_aliases_model",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("public_id", name="uq_ai_model_aliases_public_id"),
        # 一个字符串至多一段未截断；也是按 (供应商, 字符串) 查段的索引。
        Index("ux_ai_model_aliases_open_slot", "provider_id", "alias", "open_slot", unique=True),
        Index("ix_ai_model_aliases_model", "model_id", "provider_id"),
        CheckConstraint(ALIAS_PERIOD_CHECK, name="ck_ai_model_aliases_period"),
    )


__all__ = [
    "ALIAS_PERIOD_CHECK",
    "CODE_COLLATION",
    "OPEN_SLOT_EXPRESSION",
    "PAYLOAD_SHAPE_CHECK",
    "QUANTITY_FIELD",
    "QUANTITY_FIELD_CHECK",
    "QUANTITY_KIND_CHECK",
    "SEED_METER_TYPES",
    "STATUS_CHECK",
    "TOKEN_FIELDS",
    "TOKEN_SHAPE_CHECK",
    "AiModel",
    "AiModelAlias",
    "AiProvider",
    "CatalogStatus",
    "PayloadShape",
    "QuantityKind",
    "SeedComponent",
    "SeedMeterType",
    "UsageMeterComponent",
    "UsageMeterType",
]

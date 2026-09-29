"""AI catalog: usage meter types and components (seeded), providers, models, aliases

照设计闸门 #163 v4（docs/design/AIH-TASK-025-ai-catalog.md）§2「数据库」与 §8。

步骤
----

1. `create_table usage_meter_types`：`public_id` / `code` 唯一、`(id, payload_shape)`
   唯一（给分量表的复合外键用）、四条 CHECK；`code` 与 `unit` 在 MySQL 上是
   `utf8mb4_0900_bin`
2. `create_table usage_meter_components`：复合外键 `(meter_type_id, payload_shape)`
   → `usage_meter_types(id, payload_shape)` `ON DELETE RESTRICT`、`component_code`
   唯一、`(meter_type_id, quantity_field)` 唯一、两条 CHECK
3. `create_table ai_providers`：`public_id` / `code` 唯一、一条 CHECK
4. `create_table ai_models`：外键 `provider_id` → `ai_providers(id)` RESTRICT、
   `(provider_id, code)` 唯一、`(id, provider_id)` 唯一（给别名表的复合外键用）、
   一条 CHECK
5. `create_table ai_model_aliases`：STORED 生成列 `open_slot` 与唯一索引
   `(provider_id, alias, open_slot)`（一个字符串至多一段未截断）、复合外键
   `(model_id, provider_id)` → `ai_models(id, provider_id)` RESTRICT、一条 CHECK
6. 种子：9 个计量类型（spec §12）、12 个分量（spec §15.1）。⚠️ 常量写死在这个
   文件里、不 import 应用代码：迁移要能在代码以后改动之后原样重放。
   `app/models/ai_catalog.py` 的 `SEED_METER_TYPES` 是同一份清单，
   `tests/backend/test_migrations.py` 逐行比对迁移、代码常量与库里三方

本任务**不**预置任何供应商与模型（设计 §8）：上线后由管理员经接口录入。

§132 第 13 条分析
-----------------

**已有数据**：不 ALTER、不读写任何既有表；五张都是新表。

**锁表与停机**：只建新表与它们的索引。外键只指向本迁移新建的表，不对任何既有表
取锁。种子 21 行，瞬时完成。不需要停机。

**备份**：要求前置备份（与 0005–0011 同一口径）。本版不改动、不回填任何既有数据。

**部署顺序**：`deploy.sh` 先迁移、再换镜像。旧代码不读新表，不受影响。新代码的
管理端目录接口依赖新表；`resolve_model` 本任务不接到任何计费路径。

**失败处理**：⚠️ MySQL 的 DDL **不参与事务**。
- 五条 `CREATE TABLE` 依次执行（`ai_model_aliases` 生成列上的唯一索引由 alembic
  在建表之后单独发一条 `CREATE UNIQUE INDEX`），之后写种子。
- 第 1 张表就失败：什么都没建，`alembic_version` 仍是
  `0011_audit_logs_query_indexes`，修好后重新部署即可。
- 中途失败，或进程在中途被杀：已建的表存在而 `alembic_version` 没有前进，重跑会撞
  "Table already exists"。确认 `alembic_version` 仍是 `0011_audit_logs_query_indexes`
  后，按依赖倒序 `DROP TABLE` 已建的那几张（`ai_model_aliases`、`ai_models`、
  `ai_providers`、`usage_meter_components`、`usage_meter_types`，不存在的跳过），
  再重新部署。
- 种子写入失败：两张计量表已存在、可能有部分种子行。同上清场重跑：表里只有种子。
- 本版写入的只有种子，所以「清场重跑」始终安全。

**回滚**：`downgrade` 按外键依赖倒序删表（别名 → 模型 → 供应商 → 分量 → 类型），
不碰任何既有表。⚠️ 生产上若已录入供应商、模型或别名，回滚会连同它们一起删掉
—— 那时应前滚修复而不是回滚（设计 §8）。

Revision ID: 0012_ai_catalog
Revises: 0011_audit_logs_query_indexes
Create Date: 2026-09-29
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0012_ai_catalog"
down_revision: str | None = "0011_audit_logs_query_indexes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 冻结的字面量，与 app/models/ai_catalog.py 的 CODE_COLLATION 一致。
_CODE_COLLATION = "utf8mb4_0900_bin"

_METER_TYPES = "usage_meter_types"
_COMPONENTS = "usage_meter_components"
_PROVIDERS = "ai_providers"
_MODELS = "ai_models"
_ALIASES = "ai_model_aliases"

# 依赖倒序：downgrade 按这个顺序删。
_DROP_ORDER = (_ALIASES, _MODELS, _PROVIDERS, _COMPONENTS, _METER_TYPES)

# 名字 → (表, 条件)。⚠️ 冻结的字面量：与 app/models/ai_catalog.py 上的
# CheckConstraint 逐条一致（空白除外），tests/backend/test_migrations.py 比对两边。
_CHECKS: dict[str, tuple[str, str]] = {
    "ck_usage_meter_types_payload_shape": (
        _METER_TYPES,
        "payload_shape IN ('LLM_TOKEN_FIELDS', 'QUANTITY')",
    ),
    "ck_usage_meter_types_quantity_kind": (
        _METER_TYPES,
        "quantity_kind IN ('INTEGER', 'DECIMAL')",
    ),
    "ck_usage_meter_types_status": (
        _METER_TYPES,
        "status IN ('ACTIVE', 'RETIRED')",
    ),
    "ck_usage_meter_types_token_shape": (
        _METER_TYPES,
        "payload_shape <> 'LLM_TOKEN_FIELDS' OR (unit = 'TOKEN' AND quantity_kind = 'INTEGER')",
    ),
    "ck_usage_meter_components_payload_shape": (
        _COMPONENTS,
        "payload_shape IN ('LLM_TOKEN_FIELDS', 'QUANTITY')",
    ),
    "ck_usage_meter_components_quantity_field": (
        _COMPONENTS,
        "(payload_shape = 'QUANTITY' AND quantity_field = 'quantity')"
        " OR (payload_shape = 'LLM_TOKEN_FIELDS' AND quantity_field IN ('input_tokens',"
        " 'output_tokens', 'cache_creation_input_tokens', 'cache_read_input_tokens'))",
    ),
    "ck_ai_providers_status": (
        _PROVIDERS,
        "status IN ('ACTIVE', 'RETIRED')",
    ),
    "ck_ai_models_status": (
        _MODELS,
        "status IN ('ACTIVE', 'RETIRED')",
    ),
    "ck_ai_model_aliases_period": (
        _ALIASES,
        "effective_from IS NULL OR effective_to IS NULL OR effective_from < effective_to",
    ),
}

# 生成列表达式。同上，冻结的字面量，与模型的 Computed 比对。
_OPEN_SLOT = "CASE WHEN effective_to IS NULL THEN 1 END"

# 种子：(code, display_name, payload_shape, unit, quantity_kind,
# ((component_code, quantity_field), …))。⚠️ 冻结的字面量，不 import 应用代码；
# 与 app/models/ai_catalog.py 的 SEED_METER_TYPES 逐行一致。
_SEED_METER_TYPES: tuple[tuple[str, str, str, str, str, tuple[tuple[str, str], ...]], ...] = (
    (
        "LLM_TOKEN",
        "LLM tokens",
        "LLM_TOKEN_FIELDS",
        "TOKEN",
        "INTEGER",
        (
            ("LLM_INPUT_TOKEN", "input_tokens"),
            ("LLM_OUTPUT_TOKEN", "output_tokens"),
            ("LLM_CACHE_WRITE_TOKEN", "cache_creation_input_tokens"),
            ("LLM_CACHE_READ_TOKEN", "cache_read_input_tokens"),
        ),
    ),
    (
        "EMBEDDING_TOKEN",
        "Embedding tokens",
        "QUANTITY",
        "TOKEN",
        "INTEGER",
        (("EMBEDDING_TOKEN", "quantity"),),
    ),
    (
        "AUDIO_SECOND",
        "Audio seconds",
        "QUANTITY",
        "SECOND",
        "DECIMAL",
        (("AUDIO_SECOND", "quantity"),),
    ),
    (
        "AUDIO_MINUTE",
        "Audio minutes",
        "QUANTITY",
        "MINUTE",
        "DECIMAL",
        (("AUDIO_MINUTE", "quantity"),),
    ),
    (
        "TTS_CHARACTER",
        "TTS characters",
        "QUANTITY",
        "CHARACTER",
        "INTEGER",
        (("TTS_CHARACTER", "quantity"),),
    ),
    (
        "IMAGE_GENERATION",
        "Image generations",
        "QUANTITY",
        "IMAGE",
        "INTEGER",
        (("IMAGE_GENERATION", "quantity"),),
    ),
    (
        "OCR_PAGE",
        "OCR pages",
        "QUANTITY",
        "PAGE",
        "INTEGER",
        (("OCR_PAGE", "quantity"),),
    ),
    (
        "DOCUMENT_PAGE",
        "Document pages",
        "QUANTITY",
        "PAGE",
        "INTEGER",
        (("DOCUMENT_PAGE", "quantity"),),
    ),
    (
        "CUSTOM",
        "Custom units",
        "QUANTITY",
        "UNIT",
        "DECIMAL",
        (("CUSTOM", "quantity"),),
    ),
)


def _code(length: int) -> sa.String:
    # ⚠️ 二进制、NO PAD 排序：只差大小写或尾部空格的两个代码不是同一个。
    return sa.String(length=length, collation=_CODE_COLLATION)


def _checks(table: str) -> list[sa.CheckConstraint]:
    return [
        sa.CheckConstraint(condition, name=name)
        for name, (owner, condition) in _CHECKS.items()
        if owner == table
    ]


def _status() -> sa.Column:
    return sa.Column("status", sa.String(length=16), nullable=False, server_default="ACTIVE")


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    ]


def upgrade() -> None:
    op.create_table(
        _METER_TYPES,
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("public_id", sa.CHAR(length=36), nullable=False),
        sa.Column("code", _code(32), nullable=False),
        sa.Column("display_name", sa.String(length=255), nullable=False),
        sa.Column("payload_shape", sa.String(length=32), nullable=False),
        sa.Column("unit", _code(16), nullable=False),
        sa.Column("quantity_kind", sa.String(length=16), nullable=False),
        _status(),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("public_id", name="uq_usage_meter_types_public_id"),
        sa.UniqueConstraint("code", name="uq_usage_meter_types_code"),
        sa.UniqueConstraint("id", "payload_shape", name="uq_usage_meter_types_id_shape"),
        *_checks(_METER_TYPES),
    )

    op.create_table(
        _COMPONENTS,
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("meter_type_id", sa.BigInteger(), nullable=False),
        sa.Column("payload_shape", sa.String(length=32), nullable=False),
        sa.Column("component_code", _code(64), nullable=False),
        sa.Column("quantity_field", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        # 分量的形态由数据库保证与所属类型一致。RESTRICT：类型删不掉（INV-6）。
        sa.ForeignKeyConstraint(
            ["meter_type_id", "payload_shape"],
            [f"{_METER_TYPES}.id", f"{_METER_TYPES}.payload_shape"],
            name="fk_usage_meter_components_type",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("component_code", name="uq_usage_meter_components_code"),
        sa.UniqueConstraint(
            "meter_type_id", "quantity_field", name="uq_usage_meter_components_type_field"
        ),
        sa.Index("ix_usage_meter_components_type_shape", "meter_type_id", "payload_shape"),
        *_checks(_COMPONENTS),
    )

    op.create_table(
        _PROVIDERS,
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("public_id", sa.CHAR(length=36), nullable=False),
        sa.Column("code", _code(64), nullable=False),
        sa.Column("display_name", sa.String(length=255), nullable=False),
        _status(),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("public_id", name="uq_ai_providers_public_id"),
        sa.UniqueConstraint("code", name="uq_ai_providers_code"),
        *_checks(_PROVIDERS),
    )

    op.create_table(
        _MODELS,
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("public_id", sa.CHAR(length=36), nullable=False),
        sa.Column("provider_id", sa.BigInteger(), nullable=False),
        sa.Column("code", _code(128), nullable=False),
        sa.Column("display_name", sa.String(length=255), nullable=False),
        _status(),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id"),
        # RESTRICT：有模型的供应商删不掉（INV-6）。
        sa.ForeignKeyConstraint(
            ["provider_id"],
            [f"{_PROVIDERS}.id"],
            name="fk_ai_models_provider",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("public_id", name="uq_ai_models_public_id"),
        sa.UniqueConstraint("provider_id", "code", name="uq_ai_models_provider_code"),
        sa.UniqueConstraint("id", "provider_id", name="uq_ai_models_id_provider"),
        *_checks(_MODELS),
    )

    op.create_table(
        _ALIASES,
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("public_id", sa.CHAR(length=36), nullable=False),
        sa.Column("provider_id", sa.BigInteger(), nullable=False),
        sa.Column("model_id", sa.BigInteger(), nullable=False),
        sa.Column("alias", _code(128), nullable=False),
        sa.Column("effective_from", sa.DateTime(), nullable=True),
        sa.Column("effective_to", sa.DateTime(), nullable=True),
        sa.Column("open_slot", sa.Integer(), sa.Computed(_OPEN_SLOT, persisted=True)),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("closed_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        # 别名只能指向同一供应商的模型。RESTRICT：模型删不掉（INV-6）。
        sa.ForeignKeyConstraint(
            ["model_id", "provider_id"],
            [f"{_MODELS}.id", f"{_MODELS}.provider_id"],
            name="fk_ai_model_aliases_model",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("public_id", name="uq_ai_model_aliases_public_id"),
        # 唯一索引允许多个 NULL：只有未截断的段在 open_slot 上有值。
        sa.Index(
            "ux_ai_model_aliases_open_slot",
            "provider_id",
            "alias",
            "open_slot",
            unique=True,
        ),
        sa.Index("ix_ai_model_aliases_model", "model_id", "provider_id"),
        *_checks(_ALIASES),
    )

    _seed()


def _seed() -> None:
    """9 types, then 12 components that find their type by code (works with `--sql` too)."""
    now = dt.datetime.now(dt.UTC).replace(tzinfo=None, microsecond=0)
    types = sa.table(
        _METER_TYPES,
        sa.column("id", sa.BigInteger()),
        sa.column("public_id", sa.CHAR(36)),
        sa.column("code", sa.String()),
        sa.column("display_name", sa.String()),
        sa.column("payload_shape", sa.String()),
        sa.column("unit", sa.String()),
        sa.column("quantity_kind", sa.String()),
        sa.column("status", sa.String()),
        sa.column("created_at", sa.DateTime()),
        sa.column("updated_at", sa.DateTime()),
    )
    components = sa.table(
        _COMPONENTS,
        sa.column("meter_type_id", sa.BigInteger()),
        sa.column("payload_shape", sa.String()),
        sa.column("component_code", sa.String()),
        sa.column("quantity_field", sa.String()),
        sa.column("created_at", sa.DateTime()),
    )
    op.bulk_insert(
        types,
        [
            {
                "public_id": str(uuid.uuid4()),
                "code": code,
                "display_name": display_name,
                "payload_shape": shape,
                "unit": unit,
                "quantity_kind": kind,
                "status": "ACTIVE",
                "created_at": now,
                "updated_at": now,
            }
            for code, display_name, shape, unit, kind, _ in _SEED_METER_TYPES
        ],
    )
    columns = ["meter_type_id", "payload_shape", "component_code", "quantity_field", "created_at"]
    for code, _, _, _, _, seeded in _SEED_METER_TYPES:
        for component_code, quantity_field in seeded:
            # 分量的形态取自所属类型那一行：复合外键本来也要求两者一致。
            source = sa.select(
                types.c.id,
                types.c.payload_shape,
                sa.literal(component_code, sa.String()),
                sa.literal(quantity_field, sa.String()),
                sa.literal(now, sa.DateTime()),
            ).where(types.c.code == code)
            op.execute(components.insert().from_select(columns, source))


def downgrade() -> None:
    # 按外键依赖倒序：别名 → 模型 → 供应商 → 分量 → 类型。不碰任何既有表。
    for table in _DROP_ORDER:
        op.drop_table(table)

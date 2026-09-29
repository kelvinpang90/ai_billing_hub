"""Request and response shapes for the AI catalog (design gate #163 v4 §2「接口」).

⚠️ 请求体一律 `extra="forbid"`：新建计量类型不收 `payload_shape` / `quantity_field`
（固定为 `QUANTITY`、取 `quantity`）；PATCH 只收 `display_name` 与 `status`，带
`code`、`alias`、`provider_id`、`unit`、`quantity_kind`、`payload_shape`、
`component_code` 或任何多余字段都是 422 —— 代码建后不可改（INV-6）。

⚠️ 代码不去空白、不改大小写：含空白就是 422，大小写原样保存（解析精确比较）。

⚠️ **响应模型是字段白名单，不直接序列化 ORM 对象**：内部自增 id、`provider_id`、
`model_id`、`meter_type_id` 与生成列 `open_slot` 都不会因为表多了一列而出现在响应里。
对外的 id 一律是 `public_id`；别名段的指向同时给出模型的 `public_id` 与 `code`。
"""

from __future__ import annotations

import datetime as dt
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, StringConstraints, field_validator, model_validator

from app.models.ai_catalog import (
    AiModel,
    AiModelAlias,
    AiProvider,
    CatalogStatus,
    PayloadShape,
    QuantityKind,
    UsageMeterComponent,
    UsageMeterType,
)

# 设计 §2「字段规则」。`component_code` 列宽 64，管理员新建时同样限 32。
MeterCode = Annotated[str, StringConstraints(pattern=r"^[A-Z][A-Z0-9_]{1,31}$")]
UnitCode = Annotated[str, StringConstraints(pattern=r"^[A-Z][A-Z0-9_]{0,15}$")]
ProviderCode = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")]
# 覆盖 `claude-sonnet-4-5-20250929`、`gpt-4o-mini-transcribe`、`models/gemini-x`。
ModelCode = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/@-]{0,127}$")]
# 显示名：去首尾空白后 1–255。
Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)]
StatusValue = Literal["ACTIVE", "RETIRED"]
QuantityKindValue = Literal["INTEGER", "DECIMAL"]


class CreateMeterTypeRequest(BaseModel):
    """新建一个 `QUANTITY` 类型，同一事务建出它唯一的分量（取 `quantity`）。"""

    model_config = ConfigDict(extra="forbid")

    code: MeterCode
    display_name: Name
    unit: UnitCode
    quantity_kind: QuantityKindValue
    component_code: MeterCode


class UpdateCatalogEntryRequest(BaseModel):
    """PATCH 计量类型、供应商或模型：只改请求体里出现的字段。

    两个字段都不许显式传 `null`；一个字段都不带是 422（与编辑客户一致）。
    """

    model_config = ConfigDict(extra="forbid")

    display_name: Name | None = None
    status: StatusValue | None = None

    @field_validator("display_name", "status", mode="before")
    @classmethod
    def _not_null(cls, value: object) -> object:
        # 只对显式传入的值运行（默认值不校验），所以「没带」与「带了 null」分得开。
        if value is None:
            raise ValueError("must not be null")
        return value

    @model_validator(mode="after")
    def _at_least_one_field(self) -> UpdateCatalogEntryRequest:
        if not self.model_fields_set:
            raise ValueError("no fields to update")
        return self

    def changes(self) -> dict[str, object]:
        """Only the fields the client actually sent."""
        return {name: getattr(self, name) for name in sorted(self.model_fields_set)}


class CreateProviderRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: ProviderCode
    display_name: Name


class CreateModelRequest(BaseModel):
    """供应商只来自路径，请求体里的 `provider_id` 之类一律 422。"""

    model_config = ConfigDict(extra="forbid")

    code: ModelCode
    display_name: Name


class MapAliasRequest(BaseModel):
    """把上报的字符串映射到同一供应商下的模型（`model_id` 是模型的 `public_id`）。"""

    # `model_id` 是契约里的字段名；关掉 pydantic 对 `model_` 前缀的保留提示。
    model_config = ConfigDict(extra="forbid", protected_namespaces=())

    alias: ModelCode
    model_id: Annotated[str, StringConstraints(min_length=1, max_length=64)]


class RetireAliasRequest(BaseModel):
    """请求体就是 `{}`：要撤销的段只来自路径。"""

    model_config = ConfigDict(extra="forbid")


# --- 响应 -------------------------------------------------------------------------


class MeterComponentView(BaseModel):
    component_code: str
    quantity_field: str
    created_at: dt.datetime


class MeterTypeView(BaseModel):
    id: str
    code: str
    display_name: str
    payload_shape: str
    unit: str
    quantity_kind: str
    status: str
    components: list[MeterComponentView]
    created_at: dt.datetime
    updated_at: dt.datetime


class ProviderView(BaseModel):
    id: str
    code: str
    display_name: str
    status: str
    created_at: dt.datetime
    updated_at: dt.datetime


class ModelView(BaseModel):
    id: str
    provider_id: str
    provider_code: str
    code: str
    display_name: str
    status: str
    created_at: dt.datetime
    updated_at: dt.datetime


class AliasSegmentView(BaseModel):
    """一段映射：`[effective_from, effective_to)` 内 `alias` 指向 `model_id`。

    `effective_from` 为 `null` = 「一直以来」；`effective_to` 为 `null` = 仍未截断。
    """

    model_config = ConfigDict(protected_namespaces=())

    id: str
    alias: str
    model_id: str
    model_code: str
    effective_from: dt.datetime | None
    effective_to: dt.datetime | None
    created_at: dt.datetime
    closed_at: dt.datetime | None


class ModelDetail(ModelView):
    """详情多一项：当前（未截断的段）指向它的别名。"""

    aliases: list[AliasSegmentView]


def meter_type_view(row: UsageMeterType, components: list[UsageMeterComponent]) -> MeterTypeView:
    return MeterTypeView(
        id=row.public_id,
        code=row.code,
        display_name=row.display_name,
        payload_shape=PayloadShape(row.payload_shape).value,
        unit=row.unit,
        quantity_kind=QuantityKind(row.quantity_kind).value,
        status=CatalogStatus(row.status).value,
        components=[
            MeterComponentView(
                component_code=component.component_code,
                quantity_field=component.quantity_field,
                created_at=component.created_at,
            )
            for component in components
        ],
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def provider_view(row: AiProvider) -> ProviderView:
    return ProviderView(
        id=row.public_id,
        code=row.code,
        display_name=row.display_name,
        status=CatalogStatus(row.status).value,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def model_view(row: AiModel, provider: AiProvider) -> ModelView:
    return ModelView(
        id=row.public_id,
        provider_id=provider.public_id,
        provider_code=provider.code,
        code=row.code,
        display_name=row.display_name,
        status=CatalogStatus(row.status).value,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def alias_segment_view(row: AiModelAlias, *, model_id: str, model_code: str) -> AliasSegmentView:
    return AliasSegmentView(
        id=row.public_id,
        alias=row.alias,
        model_id=model_id,
        model_code=model_code,
        effective_from=row.effective_from,
        effective_to=row.effective_to,
        created_at=row.created_at,
        closed_at=row.closed_at,
    )


def model_detail(
    row: AiModel, provider: AiProvider, segments: list[AliasSegmentView]
) -> ModelDetail:
    return ModelDetail(**model_view(row, provider).model_dump(), aliases=segments)

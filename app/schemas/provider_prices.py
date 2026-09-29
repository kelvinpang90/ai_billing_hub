"""Request and response shapes for provider prices (design gate #177 v3 §2「接口」).

⚠️ **单价与数量只收 JSON 字符串**，按十进制正则解析、不经过 `float`（INV-10）。JSON 数字、
指数写法、符号、0 一律 422；超过 8 位小数也是 422，**不舍入**
（照 app/schemas/wallet_adjustments.py）。
本任务不做任何乘除（§80 的那一次舍入归 T-G）。

⚠️ 请求体一律 `extra="forbid"`：状态、区间、发布人、`source_type` 与任何 id 都只由服务端写。
PATCH 不收 `provider_id` / `model_id`：一个版本挂在哪个模型上建后不可改。

⚠️ 发布的 `effective_from` 是 RFC 3339、**必须带时区**，换算成 UTC 后必须是整秒（有小数秒
422），免得区间边界落在秒内（设计 §2「时间语义」）。

⚠️ **响应模型是字段白名单，不直接序列化 ORM 对象**：内部自增 id、`provider_id` /
`model_id` / `created_by` / `approved_by` 的内部值与生成列 `open_slot` 都不会因为表多了一列
而出现在响应里。对外的 id 一律是 `public_id`，人一律用登录邮箱表示（与审计查询同一口径）。
"""

from __future__ import annotations

import datetime as dt
import re
from decimal import Decimal
from typing import Annotated, Any, Final

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StringConstraints,
    WithJsonSchema,
    field_validator,
    model_validator,
)

from app.models.ai_catalog import UsageMeterType
from app.models.provider_prices import PriceSourceType, PriceVersionStatus, ProviderPriceVersion
from app.repositories.provider_prices import PricedComponent
from app.schemas.customers import money_text

# 整数部分最多 12 位（DECIMAL(20,8)），小数最多 8 位，不带符号。⚠️ 用 [0-9] 而不是 \d：
# \d 还认全角与其他文字的数字。
_AMOUNT_PATTERN: Final = re.compile(r"[0-9]{1,12}(\.[0-9]{1,8})?")
# RFC 3339 的 date-time，时区必填（`Z` 或 `±hh:mm`）；小数秒最多 6 位。
_MOMENT_PATTERN: Final = re.compile(
    r"(?P<date>[0-9]{4}-[0-9]{2}-[0-9]{2})T(?P<time>[0-9]{2}:[0-9]{2}:[0-9]{2})"
    r"(?P<fraction>\.[0-9]{1,6})?(?P<zone>Z|[+-][0-9]{2}:[0-9]{2})"
)


def _parse_positive(value: object) -> Decimal:
    """A positive decimal string → `Decimal`, never through float. Anything else is refused."""
    # 422 只列字段名，不回显值（app/core/errors.py），所以这里的文案里也不放值。
    if not isinstance(value, str):
        raise ValueError("must be a JSON string")
    if _AMOUNT_PATTERN.fullmatch(value) is None:
        raise ValueError("is not a decimal with at most 12 integer and 8 fraction digits")
    amount = Decimal(value)
    if amount.is_zero():
        raise ValueError("must be positive")
    return amount


def _parse_moment(value: object) -> dt.datetime | None:
    """RFC 3339 with a zone → naive UTC on a whole second (`None` stays `None`)."""
    if value is None:
        return None
    if not isinstance(value, str) or _MOMENT_PATTERN.fullmatch(value) is None:
        raise ValueError("must be an RFC 3339 date-time with a time zone")
    try:
        moment = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        utc = moment.astimezone(dt.UTC).replace(tzinfo=None)
    except (ValueError, OverflowError):
        raise ValueError("is not a valid date-time") from None
    if utc.microsecond:
        raise ValueError("must be a whole second")
    return utc


PositiveDecimal = Annotated[
    Decimal,
    BeforeValidator(_parse_positive),
    WithJsonSchema({"type": "string", "pattern": _AMOUNT_PATTERN.pattern}),
]
Moment = Annotated[
    dt.datetime | None,
    BeforeValidator(_parse_moment),
    WithJsonSchema({"type": "string", "format": "date-time"}),
]
# ISO 4217：大写三字母。
Currency = Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")]
# 去首尾空白后 1–255：价格出处（例如供应商价格页与查看日期），不写合同价。
SourceReference = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)
]
Reason = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)]
PublicId = Annotated[str, StringConstraints(min_length=1, max_length=64)]
ComponentCode = Annotated[str, StringConstraints(min_length=1, max_length=64)]


class ComponentInput(BaseModel):
    """一个价格分量：每 `unit_quantity` 个计量单位 `rate_amount`（原币种）。"""

    model_config = ConfigDict(extra="forbid")

    # 025 的计价分量代码，例如 `LLM_INPUT_TOKEN`。
    component_code: ComponentCode
    unit_quantity: PositiveDecimal
    rate_amount: PositiveDecimal
    # 只作备注（例如供应商的档位名），计价不读它。
    metadata: dict[str, Any] | None = None


Components = Annotated[list[ComponentInput], Field(min_length=1)]


def _distinct(components: list[ComponentInput]) -> list[ComponentInput]:
    codes = [component.component_code for component in components]
    if len(set(codes)) != len(codes):
        raise ValueError("a component_code appears more than once")
    return components


class CreatePriceRequest(BaseModel):
    """建草稿。供应商与模型都是 `public_id`；模型必须属于该供应商。"""

    # `model_id` 是契约里的字段名；关掉 pydantic 对 `model_` 前缀的保留提示。
    model_config = ConfigDict(extra="forbid", protected_namespaces=())

    provider_id: PublicId
    model_id: PublicId
    source_currency: Currency
    source_reference: SourceReference
    components: Components

    @field_validator("components")
    @classmethod
    def _no_duplicates(cls, value: list[ComponentInput]) -> list[ComponentInput]:
        return _distinct(value)


class UpdatePriceRequest(BaseModel):
    """PATCH 草稿：只改请求体里出现的字段；`components` 是整体替换。

    三个字段都不许显式传 `null`；一个字段都不带是 422（与目录的 PATCH 一致）。
    """

    model_config = ConfigDict(extra="forbid")

    source_currency: Currency | None = None
    source_reference: SourceReference | None = None
    components: Components | None = None

    @field_validator("source_currency", "source_reference", "components", mode="before")
    @classmethod
    def _not_null(cls, value: object) -> object:
        # 只对显式传入的值运行（默认值不校验），所以「没带」与「带了 null」分得开。
        if value is None:
            raise ValueError("must not be null")
        return value

    @field_validator("components")
    @classmethod
    def _no_duplicates(cls, value: list[ComponentInput] | None) -> list[ComponentInput] | None:
        return None if value is None else _distinct(value)

    @model_validator(mode="after")
    def _at_least_one_field(self) -> UpdatePriceRequest:
        if not self.model_fields_set:
            raise ValueError("no fields to update")
        return self


class PublishPriceRequest(BaseModel):
    """`effective_from` 可选：不带 = 从服务端的下一个整秒起（第一个版本则是「一直以来」）。"""

    model_config = ConfigDict(extra="forbid")

    effective_from: Moment = None


class RetirePriceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: Reason


class DiscardPriceRequest(BaseModel):
    """请求体就是 `{}`：要丢弃的草稿只来自路径。"""

    model_config = ConfigDict(extra="forbid")


# --- 响应 -------------------------------------------------------------------------


class PriceComponentView(BaseModel):
    component_code: str
    # 分量所属的计量类型与它的单位：`unit_quantity` 个这种单位对应一个 `rate_amount`。
    meter_type_code: str
    unit: str
    # 恰好 8 位小数的字符串，永远不是浮点数。
    unit_quantity: str
    rate_amount: str
    metadata: dict[str, Any] | None
    created_at: dt.datetime


class PriceVersionView(BaseModel):
    """一版成本价（原币种，客户不可见）。

    `effective_from` 为 `null`：草稿，或该（供应商, 模型）第一个版本「一直以来」生效；
    `effective_to` 为 `null`：草稿，或仍生效。
    """

    model_config = ConfigDict(protected_namespaces=())

    id: str
    provider_id: str
    provider_code: str
    model_id: str
    model_code: str
    source_currency: str
    source_type: str
    source_reference: str
    status: str
    effective_from: dt.datetime | None
    effective_to: dt.datetime | None
    components: list[PriceComponentView]
    created_by_email: str | None
    approved_by_email: str | None
    created_at: dt.datetime
    updated_at: dt.datetime
    approved_at: dt.datetime | None


def price_component_view(priced: PricedComponent) -> PriceComponentView:
    kind: UsageMeterType = priced.meter_type
    return PriceComponentView(
        component_code=priced.component.component_code,
        meter_type_code=kind.code,
        unit=kind.unit,
        unit_quantity=money_text(priced.row.unit_quantity),
        rate_amount=money_text(priced.row.rate_amount),
        metadata=priced.row.metadata_json,
        created_at=priced.row.created_at,
    )


def price_version_view(
    row: ProviderPriceVersion,
    *,
    provider: tuple[str, str],
    model: tuple[str, str],
    components: list[PricedComponent],
    emails: dict[int, str],
) -> PriceVersionView:
    """`provider` / `model` are (public id, code)."""
    return PriceVersionView(
        id=row.public_id,
        provider_id=provider[0],
        provider_code=provider[1],
        model_id=model[0],
        model_code=model[1],
        source_currency=row.source_currency,
        source_type=PriceSourceType(row.source_type).value,
        source_reference=row.source_reference,
        status=PriceVersionStatus(row.status).value,
        effective_from=row.effective_from,
        effective_to=row.effective_to,
        components=[price_component_view(priced) for priced in components],
        created_by_email=emails.get(row.created_by),
        approved_by_email=emails.get(row.approved_by) if row.approved_by is not None else None,
        created_at=row.created_at,
        updated_at=row.updated_at,
        approved_at=row.approved_at,
    )

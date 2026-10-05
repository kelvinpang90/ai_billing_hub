"""Request and response shapes of `POST /api/v1/admin/pricing-preview` (design gate #179 v1 §2).

⚠️ **只在管理端**（INV-7 / REQ-PRIV-002）：响应里有供应商成本、汇率与倍数。

⚠️ 请求体与摄取同一套字段写法（app/schemas/usage_ingest.py）：严格类型
（`strict=True`，JSON 的 `true` 不是整数）、`extra="forbid"`；四个 token 是整数，
`quantity` 只收十进制字符串、不经过浮点。按计量类型形态的字段组校验要先查到
`usage_type`，在服务层（app/services/pricing_engine.py）。`occurred_at` 必填、
RFC 3339 带时区，**可以是将来**（§59：预约生效的价格与规则也能预览）。

⚠️ **金额与未舍入值都是十进制字符串**，从不是 JSON 数字：存储值恰好 8 位小数，
未舍入值是计算时的精确十进制写法（最多 50 位有效数字）。`tax_inclusive` 恒为
`true`：计费额是含税金额（ADR-0008）。

⚠️ **响应模型是字段白名单**：版本与规则用 `public_id`，不含任何内部自增 id。
"""

from __future__ import annotations

import datetime as dt
from typing import Final

from pydantic import BaseModel, ConfigDict, Field

from app.models.ai_catalog import METER_TYPE_CODE_LENGTH, UNIT_LENGTH
from app.schemas.provider_prices import PublicId
from app.schemas.usage_ingest import (
    MAX_TOKENS,
    MODEL_PATTERN,
    OCCURRED_AT_PATTERN,
    PROVIDER_PATTERN,
    QUANTITY_PATTERN,
)

BILLABLE_DESCRIPTION: Final = (
    "The customer's billed amount, tax-inclusive (ADR-0008); a decimal string, 8 fraction digits."
)


class PricingPreviewRequest(BaseModel):
    """一次试算：客户、供应商与模型字符串、计量类型、用量与显式的 `occurred_at`。

    用量按 `usage_type` 的形态给：`LLM_TOKEN_FIELDS` 四个 token 整数都必填、不带
    `quantity` / `unit`；`QUANTITY` 带 `quantity`（十进制字符串，整数类型不许有
    小数部分）与等于类型单位的 `unit`、不带 token。
    """

    model_config = ConfigDict(extra="forbid", strict=True, protected_namespaces=())

    # 租户的 public_id。
    customer_id: PublicId
    provider: str = Field(pattern=PROVIDER_PATTERN)
    # 模型代码或别名，按 `occurred_at` 解析（AIH-TASK-025）。
    model: str = Field(pattern=MODEL_PATTERN)
    usage_type: str = Field(min_length=1, max_length=METER_TYPE_CODE_LENGTH)
    input_tokens: int | None = Field(default=None, ge=0, le=MAX_TOKENS)
    output_tokens: int | None = Field(default=None, ge=0, le=MAX_TOKENS)
    cache_creation_input_tokens: int | None = Field(default=None, ge=0, le=MAX_TOKENS)
    cache_read_input_tokens: int | None = Field(default=None, ge=0, le=MAX_TOKENS)
    quantity: str | None = Field(default=None, pattern=QUANTITY_PATTERN)
    unit: str | None = Field(default=None, min_length=1, max_length=UNIT_LENGTH)
    # RFC 3339，必须带时区；日历上是否存在由服务层换算时判（与摄取一致）。
    occurred_at: str = Field(pattern=OCCURRED_AT_PATTERN)


# --- 响应 -------------------------------------------------------------------------


class PreviewModel(BaseModel):
    provider: str
    # 解析到的模型代码（用别名试算时也是模型自己的代码）。
    model: str
    # `code` / `alias`
    matched_via: str


class PreviewPriceVersion(BaseModel):
    id: str
    source_currency: str
    effective_from: dt.datetime | None
    effective_to: dt.datetime | None


class PreviewFxRate(BaseModel):
    id: str
    # 1 单位原币 = 多少 MYR，原值（10 位小数），不舍入。
    rate: str
    observed_at: dt.datetime


class PreviewPricingRule(BaseModel):
    id: str
    priority_scope: str
    strategy: str
    # MARKUP 的倍数（8 位小数）；FIXED_RATE 为 null。
    markup_multiplier: str | None


class PreviewComponent(BaseModel):
    component_code: str
    quantity: str
    provider_cost_unrounded: str
    # FIXED_RATE 的客户分量价（MYR 含税）；MARKUP 为 null。
    customer_price_unrounded: str | None


class PricingPreviewView(BaseModel):
    """试算结果。错误状态也是 200，给出已解析到的部分；其余为 null，计算结果全为 null。"""

    model_config = ConfigDict(protected_namespaces=())

    # `PRICED` / `MODEL_UNKNOWN` / `PRICING_ERROR` / `FX_RATE_ERROR`
    status: str
    # `PRICING_ERROR` 的细分码：`NO_PROVIDER_PRICE` / `MISSING_PROVIDER_COMPONENT` /
    # `NO_PRICING_RULE` / `MISSING_RULE_COMPONENT`；其余为 null。
    error_code: str | None
    model: PreviewModel | None
    provider_price_version: PreviewPriceVersion | None
    # 原币为 MYR 时为 null（不查汇率）。
    fx_rate_version: PreviewFxRate | None
    pricing_rule: PreviewPricingRule | None
    components: list[PreviewComponent]
    provider_source_cost_unrounded: str | None
    provider_source_cost: str | None
    estimated_provider_cost_myr_unrounded: str | None
    estimated_provider_cost_myr: str | None
    billable_cost_unrounded: str | None
    billable_cost: str | None = Field(description=BILLABLE_DESCRIPTION)
    tax_inclusive: bool


__all__ = [
    "BILLABLE_DESCRIPTION",
    "PreviewComponent",
    "PreviewFxRate",
    "PreviewModel",
    "PreviewPriceVersion",
    "PreviewPricingRule",
    "PricingPreviewRequest",
    "PricingPreviewView",
]

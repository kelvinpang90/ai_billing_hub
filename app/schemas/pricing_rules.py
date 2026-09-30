"""Request and response shapes for customer pricing rules (design gate #178 v4 §2「接口」).

⚠️ **价格是 MYR 含税价（tax-inclusive，ADR-0008）。**FIXED_RATE 分量的 `rate_amount` 是含税
单价；MARKUP 的倍数乘的是 MYR 估算成本，得到的就是含税计费额。本任务不存税额。

⚠️ **单价、数量与倍数只收 JSON 字符串**，按十进制正则解析、不经过 `float`（INV-10）。JSON
数字、指数写法、符号、0 一律 422；超过 8 位小数也是 422，**不舍入**（与供应商价格同一套解析，
见 app/schemas/provider_prices.py）。本任务不做任何乘除（§80 的那一次舍入归 T-G）。

⚠️ 请求体一律 `extra="forbid"`：状态、区间、发布人、币种与任何内部 id 都只由服务端写。
**范围字段必须与 `priority_scope` 恰好匹配**（多给或少给都是 422）；PATCH 不收任何范围字段：
改范围请新建。

⚠️ **响应模型是字段白名单，不直接序列化 ORM 对象**：内部自增 id、`tenant_id` /
`provider_id` / `model_id` / `created_by` / `approved_by` 的内部值与两个生成列都不会因为表多了
一列而出现在响应里。对外的 id 一律是 `public_id`（客户即租户的），人一律用登录邮箱。
"""

from __future__ import annotations

import datetime as dt
from typing import Annotated, Final

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.models.ai_catalog import UsageMeterType
from app.models.pricing_rules import PricingRule, PricingRuleStatus, PricingScope, PricingStrategy
from app.repositories.pricing_rules import RuleComponent
from app.schemas.customers import money_text
from app.schemas.provider_prices import (
    MAX_COMPONENTS,
    ComponentCode,
    Moment,
    PositiveDecimal,
    PublicId,
    Reason,
)

# 字段说明（进 OpenAPI 文档）：含税一定写明（设计 §2「含税」）。
RATE_AMOUNT_DESCRIPTION: Final = (
    "MYR tax-inclusive price (ADR-0008) per `unit_quantity` units; a decimal string,"
    " at most 8 fraction digits."
)
MARKUP_DESCRIPTION: Final = (
    "MARKUP only: multiplies the MYR estimated provider cost; the result is the tax-inclusive"
    " billed amount (ADR-0008). A positive decimal string, at most 8 fraction digits."
)

# 每一级要哪几个范围字段（设计 §2「接口」：多给或少给都是 422）。
SCOPE_FIELDS: Final = ("customer_id", "provider_id", "model_id")


def required_scope_fields(scope: PricingScope) -> frozenset[str]:
    wanted = {
        "customer_id": scope.has_customer,
        "provider_id": scope.has_provider,
        "model_id": scope.has_model,
    }
    return frozenset(name for name, needed in wanted.items() if needed)


class RuleComponentInput(BaseModel):
    """一个 FIXED_RATE 分量：每 `unit_quantity` 个计量单位 `rate_amount` MYR（含税）。"""

    model_config = ConfigDict(extra="forbid")

    # 025 的计价分量代码，例如 `LLM_INPUT_TOKEN`。
    component_code: ComponentCode
    unit_quantity: PositiveDecimal
    rate_amount: PositiveDecimal = Field(description=RATE_AMOUNT_DESCRIPTION)


# 草稿可以没有分量（完整性在发布时校验，设计 §2「完整性」）。
RuleComponents = Annotated[list[RuleComponentInput], Field(max_length=MAX_COMPONENTS)]


def _distinct(components: list[RuleComponentInput]) -> list[RuleComponentInput]:
    codes = [component.component_code for component in components]
    if len(set(codes)) != len(codes):
        raise ValueError("a component_code appears more than once")
    return components


def _check_strategy_fields(
    strategy: PricingStrategy | None, *, has_multiplier: bool, has_components: bool
) -> None:
    """MARKUP carries a multiplier and no components; FIXED_RATE the other way round."""
    if has_multiplier and has_components:
        raise ValueError("markup_multiplier and components never go together")
    if strategy is PricingStrategy.MARKUP and has_components:
        raise ValueError("a MARKUP rule has no components")
    if strategy is PricingStrategy.FIXED_RATE and has_multiplier:
        raise ValueError("a FIXED_RATE rule has no markup_multiplier")


class CreatePricingRuleRequest(BaseModel):
    """建草稿。范围字段按 `priority_scope` 给：客户（租户 `public_id`）、供应商、模型。

    MARKUP 必带 `markup_multiplier`、不带 `components`；FIXED_RATE 必带 `components`
    （可以是空列表：草稿可以不完整）、不带 `markup_multiplier`。
    """

    # `model_id` 是契约里的字段名；关掉 pydantic 对 `model_` 前缀的保留提示。
    model_config = ConfigDict(extra="forbid", protected_namespaces=())

    priority_scope: PricingScope
    customer_id: PublicId | None = None
    provider_id: PublicId | None = None
    model_id: PublicId | None = None
    strategy: PricingStrategy
    markup_multiplier: PositiveDecimal | None = Field(default=None, description=MARKUP_DESCRIPTION)
    components: RuleComponents | None = None

    @field_validator("components")
    @classmethod
    def _no_duplicates(
        cls, value: list[RuleComponentInput] | None
    ) -> list[RuleComponentInput] | None:
        return None if value is None else _distinct(value)

    @model_validator(mode="after")
    def _shape(self) -> CreatePricingRuleRequest:
        given = frozenset(name for name in SCOPE_FIELDS if getattr(self, name) is not None)
        if given != required_scope_fields(self.priority_scope):
            raise ValueError("the scope fields do not match priority_scope")
        has_multiplier = self.markup_multiplier is not None
        has_components = self.components is not None
        _check_strategy_fields(
            self.strategy, has_multiplier=has_multiplier, has_components=has_components
        )
        if self.strategy is PricingStrategy.MARKUP and not has_multiplier:
            raise ValueError("a MARKUP rule needs markup_multiplier")
        if self.strategy is PricingStrategy.FIXED_RATE and not has_components:
            raise ValueError("a FIXED_RATE rule needs components")
        return self


class UpdatePricingRuleRequest(BaseModel):
    """PATCH 草稿：只改请求体里出现的字段；`components` 是整体替换。范围字段不可改。

    三个字段都不许显式传 `null`；一个字段都不带是 422。改成 MARKUP 要同时给倍数
    （原本就是 MARKUP 的可以只改倍数）；改成 FIXED_RATE 时倍数去掉，分量取请求里的
    （没给就是空）。与规则现状合起来仍然矛盾的（例如给 FIXED_RATE 规则只带倍数）
    由服务层判 422。
    """

    model_config = ConfigDict(extra="forbid")

    strategy: PricingStrategy | None = None
    markup_multiplier: PositiveDecimal | None = Field(default=None, description=MARKUP_DESCRIPTION)
    components: RuleComponents | None = None

    @field_validator("strategy", "markup_multiplier", "components", mode="before")
    @classmethod
    def _not_null(cls, value: object) -> object:
        # 只对显式传入的值运行（默认值不校验），所以「没带」与「带了 null」分得开。
        if value is None:
            raise ValueError("must not be null")
        return value

    @field_validator("components")
    @classmethod
    def _no_duplicates(
        cls, value: list[RuleComponentInput] | None
    ) -> list[RuleComponentInput] | None:
        return None if value is None else _distinct(value)

    @model_validator(mode="after")
    def _shape(self) -> UpdatePricingRuleRequest:
        if not self.model_fields_set:
            raise ValueError("no fields to update")
        _check_strategy_fields(
            self.strategy,
            has_multiplier=self.markup_multiplier is not None,
            has_components=self.components is not None,
        )
        return self


class PublishPricingRuleRequest(BaseModel):
    """`effective_from` 可选：不带 = 从服务端的下一个整秒起（只有 `GLOBAL` 范围的第一条是
    「一直以来」）。带时区的 RFC 3339、整秒，不早于服务端的下一个整秒。"""

    model_config = ConfigDict(extra="forbid")

    effective_from: Moment = None


class RetirePricingRuleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: Reason


class DiscardPricingRuleRequest(BaseModel):
    """请求体就是 `{}`：要丢弃的草稿只来自路径。"""

    model_config = ConfigDict(extra="forbid")


# --- 响应 -------------------------------------------------------------------------


class PricingRuleComponentView(BaseModel):
    component_code: str
    # 分量所属的计量类型与它的单位：`unit_quantity` 个这种单位对应一个 `rate_amount`。
    meter_type_code: str
    unit: str
    # 恰好 8 位小数的字符串，永远不是浮点数。
    unit_quantity: str
    rate_amount: str = Field(description=RATE_AMOUNT_DESCRIPTION)
    # 永远是 MYR。
    currency: str
    created_at: dt.datetime


class PricingRuleView(BaseModel):
    """一条客户定价规则（只在管理端可见：倍数即 markup，INV-7）。价格一律 MYR 含税。

    范围字段按 `priority_scope` 有值，其余为 `null`。`effective_from` 为 `null`：草稿，或
    `GLOBAL` 范围第一条「一直以来」生效；`effective_to` 为 `null`：草稿，或仍生效。
    """

    model_config = ConfigDict(protected_namespaces=())

    id: str
    priority_scope: str
    customer_id: str | None
    provider_id: str | None
    provider_code: str | None
    model_id: str | None
    model_code: str | None
    strategy: str
    markup_multiplier: str | None = Field(description=MARKUP_DESCRIPTION)
    status: str
    effective_from: dt.datetime | None
    effective_to: dt.datetime | None
    components: list[PricingRuleComponentView]
    created_by_email: str | None
    approved_by_email: str | None
    created_at: dt.datetime
    updated_at: dt.datetime
    approved_at: dt.datetime | None


def rule_component_view(item: RuleComponent) -> PricingRuleComponentView:
    kind: UsageMeterType = item.meter_type
    return PricingRuleComponentView(
        component_code=item.component.component_code,
        meter_type_code=kind.code,
        unit=kind.unit,
        unit_quantity=money_text(item.row.unit_quantity),
        rate_amount=money_text(item.row.rate_amount),
        currency=item.row.currency,
        created_at=item.row.created_at,
    )


def pricing_rule_view(
    row: PricingRule,
    *,
    customer_id: str | None,
    provider: tuple[str, str] | None,
    model: tuple[str, str] | None,
    components: list[RuleComponent],
    emails: dict[int, str],
) -> PricingRuleView:
    """`customer_id` is the tenant's public id; `provider` / `model` are (public id, code)."""
    multiplier = row.markup_multiplier
    return PricingRuleView(
        id=row.public_id,
        priority_scope=PricingScope(row.priority_scope).value,
        customer_id=customer_id,
        provider_id=provider[0] if provider is not None else None,
        provider_code=provider[1] if provider is not None else None,
        model_id=model[0] if model is not None else None,
        model_code=model[1] if model is not None else None,
        strategy=PricingStrategy(row.strategy).value,
        markup_multiplier=money_text(multiplier) if multiplier is not None else None,
        status=PricingRuleStatus(row.status).value,
        effective_from=row.effective_from,
        effective_to=row.effective_to,
        components=[rule_component_view(item) for item in components],
        created_by_email=emails.get(row.created_by),
        approved_by_email=emails.get(row.approved_by) if row.approved_by is not None else None,
        created_at=row.created_at,
        updated_at=row.updated_at,
        approved_at=row.approved_at,
    )

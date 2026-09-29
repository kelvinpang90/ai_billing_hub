"""Admin pricing rule endpoints: drafts, publish, retire, discard (design gate #178 v3).

契约见 [docs/api.md](../../docs/api.md)「管理端定价规则」。价格一律 MYR 含税（ADR-0008）。

⚠️ **每个处理函数的第一条语句是 `require_admin(request)`**，与
app/api/admin_customers.py 同一条：鉴权是显式调用，不挂在路由器上。
`tests/backend/test_admin_customers_api.py` 从应用的路由表枚举 `/api/v1/admin`
下的全部路由逐个验 401 / 403 且不写库；`tests/backend/test_pricing_rules_api.py`
用 AST 钉住「第一条语句」。

⚠️ 倍数（即 markup）与单价只在管理端出现（INV-7 / REQ-PRIV-002）：没有任何客户
或集成侧的定价规则接口。

⚠️ 请求体与查询参数由 FastAPI 在处理函数**之前**校验，所以没带令牌、参数又不合法
的请求拿到的是 422 而不是 401。422 只列字段名、不回显值（app/core/errors.py）。

路径里的 id 一律是规则的 `public_id`；响应不含内部自增 id。没有删除接口：草稿只能
丢弃，规则行永不删除。这一层不写日志。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query, Request, status

from app.api.auth import request_context, require_admin, require_session_factory
from app.core.logging import current_request_id
from app.models.pricing_rules import PricingRuleStatus, PricingScope
from app.schemas.customers import DEFAULT_PAGE_SIZE, MAX_PAGE, MAX_PAGE_SIZE, Page
from app.schemas.envelope import ApiResponse, success
from app.schemas.pricing_rules import (
    CreatePricingRuleRequest,
    DiscardPricingRuleRequest,
    PricingRuleView,
    PublishPricingRuleRequest,
    RetirePricingRuleRequest,
    UpdatePricingRuleRequest,
)
from app.services import pricing_rules

router = APIRouter(prefix="/api/v1/admin", tags=["admin"])

# 超出范围是 422，不静默截断（spec §108）。与 admin_customers.py 的同名别名一致。
PageNumber = Annotated[int, Query(ge=1, le=MAX_PAGE)]
PageSize = Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)]
# 取值以外的是 422。参数名另取，免得遮住 `fastapi.status`。
StatusFilter = Annotated[PricingRuleStatus | None, Query(alias="status")]
ScopeFilter = Annotated[PricingScope | None, Query()]
PublicIdFilter = Annotated[str | None, Query(min_length=1, max_length=64)]

_RULES = "/pricing-rules"
_RULE = _RULES + "/{rule_id}"


@router.get(_RULES, response_model=ApiResponse[Page[PricingRuleView]])
def list_pricing_rules(
    request: Request,
    priority_scope: ScopeFilter = None,
    customer_id: PublicIdFilter = None,
    provider_id: PublicIdFilter = None,
    model_id: PublicIdFilter = None,
    status_filter: StatusFilter = None,
    page: PageNumber = 1,
    page_size: PageSize = DEFAULT_PAGE_SIZE,
) -> ApiResponse[Page[PricingRuleView]]:
    """§59 price history: by scope (§16 order), then `effective_from`; with components."""
    require_admin(request)
    listing = pricing_rules.list_pricing_rules(
        require_session_factory(request),
        priority_scope=priority_scope,
        customer_id=customer_id,
        provider_id=provider_id,
        model_id=model_id,
        status=status_filter,
        page=page,
        page_size=page_size,
    )
    return success(listing, request_id=current_request_id())


@router.post(
    _RULES,
    status_code=status.HTTP_201_CREATED,
    response_model=ApiResponse[PricingRuleView],
)
def create_pricing_rule(
    request: Request, payload: CreatePricingRuleRequest
) -> ApiResponse[PricingRuleView]:
    """A draft with its components and the audit, at once. Completeness is checked on publish."""
    admin = require_admin(request)
    created = pricing_rules.create_draft(
        require_session_factory(request),
        actor=admin,
        priority_scope=payload.priority_scope,
        customer_id=payload.customer_id,
        provider_id=payload.provider_id,
        model_id=payload.model_id,
        strategy=payload.strategy,
        markup_multiplier=payload.markup_multiplier,
        components=payload.components,
        context=request_context(request),
    )
    return success(created, request_id=current_request_id())


@router.get(_RULE, response_model=ApiResponse[PricingRuleView])
def get_pricing_rule(request: Request, rule_id: str) -> ApiResponse[PricingRuleView]:
    require_admin(request)
    detail = pricing_rules.get_pricing_rule(require_session_factory(request), rule_id)
    return success(detail, request_id=current_request_id())


@router.patch(_RULE, response_model=ApiResponse[PricingRuleView])
def update_pricing_rule(
    request: Request, rule_id: str, payload: UpdatePricingRuleRequest
) -> ApiResponse[PricingRuleView]:
    """Drafts only; `components` replaces the whole set. No real change: 200, nothing written."""
    admin = require_admin(request)
    updated = pricing_rules.update_draft(
        require_session_factory(request),
        actor=admin,
        rule_id=rule_id,
        strategy=payload.strategy,
        markup_multiplier=payload.markup_multiplier,
        components=payload.components,
        context=request_context(request),
    )
    return success(updated, request_id=current_request_id())


@router.post(_RULE + "/publish", response_model=ApiResponse[PricingRuleView])
def publish_pricing_rule(
    request: Request, rule_id: str, payload: PublishPricingRuleRequest
) -> ApiResponse[PricingRuleView]:
    """Publish inside the pricing rule lock. Already published: 200 and nothing is written."""
    admin = require_admin(request)
    published = pricing_rules.publish(
        require_session_factory(request),
        actor=admin,
        rule_id=rule_id,
        effective_from=payload.effective_from,
        context=request_context(request),
    )
    return success(published, request_id=current_request_id())


@router.post(_RULE + "/retire", response_model=ApiResponse[PricingRuleView])
def retire_pricing_rule(
    request: Request, rule_id: str, payload: RetirePricingRuleRequest
) -> ApiResponse[PricingRuleView]:
    """Disable the current rule (§59), or withdraw a reservation that has not started."""
    admin = require_admin(request)
    retired = pricing_rules.retire(
        require_session_factory(request),
        actor=admin,
        rule_id=rule_id,
        reason=payload.reason,
        context=request_context(request),
    )
    return success(retired, request_id=current_request_id())


@router.post(_RULE + "/discard", response_model=ApiResponse[PricingRuleView])
def discard_pricing_rule(
    request: Request, rule_id: str, payload: DiscardPricingRuleRequest
) -> ApiResponse[PricingRuleView]:
    """DRAFT → DISCARDED. The row stays (rules are never deleted)."""
    admin = require_admin(request)
    discarded = pricing_rules.discard_draft(
        require_session_factory(request),
        actor=admin,
        rule_id=rule_id,
        context=request_context(request),
    )
    return success(discarded, request_id=current_request_id())

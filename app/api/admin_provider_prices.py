"""Admin provider price endpoints: drafts, publish, retire, discard (design gate #177 v3).

契约见 [docs/api.md](../../docs/api.md)「管理端供应商价格」。

⚠️ **每个处理函数的第一条语句是 `require_admin(request)`**，与 app/api/admin_customers.py
同一条：鉴权是显式调用，不挂在路由器上。`tests/backend/test_admin_customers_api.py` 从
应用的路由表枚举 `/api/v1/admin` 下的全部路由逐个验 401 / 403 且不写库；
`tests/backend/test_provider_prices_api.py` 用 AST 钉住「第一条语句」。

⚠️ 成本价只在管理端出现（INV-7 / REQ-PRIV-002）：没有任何客户或集成侧的价格接口。

⚠️ 请求体与查询参数由 FastAPI 在处理函数**之前**校验，所以没带令牌、参数又不合法的
请求拿到的是 422 而不是 401。422 只列字段名、不回显值（app/core/errors.py）。

路径里的 id 一律是版本的 `public_id`；响应不含内部自增 id。没有删除接口：草稿只能丢弃，
版本行永不删除。这一层不写日志。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query, Request, status

from app.api.auth import request_context, require_admin, require_session_factory
from app.core.logging import current_request_id
from app.models.provider_prices import PriceVersionStatus
from app.schemas.customers import DEFAULT_PAGE_SIZE, MAX_PAGE, MAX_PAGE_SIZE, Page
from app.schemas.envelope import ApiResponse, success
from app.schemas.provider_prices import (
    CreatePriceRequest,
    DiscardPriceRequest,
    PriceVersionView,
    PublishPriceRequest,
    RetirePriceRequest,
    UpdatePriceRequest,
)
from app.services import provider_prices

router = APIRouter(prefix="/api/v1/admin", tags=["admin"])

# 超出范围是 422，不静默截断（spec §108）。与 admin_customers.py 的同名别名一致。
PageNumber = Annotated[int, Query(ge=1, le=MAX_PAGE)]
PageSize = Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)]
# 取值以外的是 422。参数名另取，免得遮住 `fastapi.status`。
StatusFilter = Annotated[PriceVersionStatus | None, Query(alias="status")]
PublicIdFilter = Annotated[str | None, Query(min_length=1, max_length=64)]

_PRICES = "/provider-prices"
_PRICE = _PRICES + "/{price_version_id}"


@router.get(_PRICES, response_model=ApiResponse[Page[PriceVersionView]])
def list_provider_prices(
    request: Request,
    provider_id: PublicIdFilter = None,
    model_id: PublicIdFilter = None,
    status_filter: StatusFilter = None,
    page: PageNumber = 1,
    page_size: PageSize = DEFAULT_PAGE_SIZE,
) -> ApiResponse[Page[PriceVersionView]]:
    """By provider code, model code, then `effective_from`; each with its components."""
    require_admin(request)
    listing = provider_prices.list_price_versions(
        require_session_factory(request),
        provider_id=provider_id,
        model_id=model_id,
        status=status_filter,
        page=page,
        page_size=page_size,
    )
    return success(listing, request_id=current_request_id())


@router.post(
    _PRICES,
    status_code=status.HTTP_201_CREATED,
    response_model=ApiResponse[PriceVersionView],
)
def create_provider_price(
    request: Request, payload: CreatePriceRequest
) -> ApiResponse[PriceVersionView]:
    """A draft with its components and the audit, at once. Completeness is checked on publish."""
    admin = require_admin(request)
    created = provider_prices.create_draft(
        require_session_factory(request),
        actor=admin,
        provider_id=payload.provider_id,
        model_id=payload.model_id,
        source_currency=payload.source_currency,
        source_reference=payload.source_reference,
        components=payload.components,
        context=request_context(request),
    )
    return success(created, request_id=current_request_id())


@router.get(_PRICE, response_model=ApiResponse[PriceVersionView])
def get_provider_price(request: Request, price_version_id: str) -> ApiResponse[PriceVersionView]:
    require_admin(request)
    detail = provider_prices.get_price_version(require_session_factory(request), price_version_id)
    return success(detail, request_id=current_request_id())


@router.patch(_PRICE, response_model=ApiResponse[PriceVersionView])
def update_provider_price(
    request: Request, price_version_id: str, payload: UpdatePriceRequest
) -> ApiResponse[PriceVersionView]:
    """Drafts only; `components` replaces the whole set. No real change: 200, nothing written."""
    admin = require_admin(request)
    updated = provider_prices.update_draft(
        require_session_factory(request),
        actor=admin,
        price_version_id=price_version_id,
        source_currency=payload.source_currency,
        source_reference=payload.source_reference,
        components=payload.components,
        context=request_context(request),
    )
    return success(updated, request_id=current_request_id())


@router.post(_PRICE + "/publish", response_model=ApiResponse[PriceVersionView])
def publish_provider_price(
    request: Request, price_version_id: str, payload: PublishPriceRequest
) -> ApiResponse[PriceVersionView]:
    """Publish inside the provider row lock. Already published: 200 and nothing is written."""
    admin = require_admin(request)
    published = provider_prices.publish(
        require_session_factory(request),
        actor=admin,
        price_version_id=price_version_id,
        effective_from=payload.effective_from,
        context=request_context(request),
    )
    return success(published, request_id=current_request_id())


@router.post(_PRICE + "/retire", response_model=ApiResponse[PriceVersionView])
def retire_provider_price(
    request: Request, price_version_id: str, payload: RetirePriceRequest
) -> ApiResponse[PriceVersionView]:
    """Retire the current version, or withdraw a reservation that has not started."""
    admin = require_admin(request)
    retired = provider_prices.retire(
        require_session_factory(request),
        actor=admin,
        price_version_id=price_version_id,
        reason=payload.reason,
        context=request_context(request),
    )
    return success(retired, request_id=current_request_id())


@router.post(_PRICE + "/discard", response_model=ApiResponse[PriceVersionView])
def discard_provider_price(
    request: Request, price_version_id: str, payload: DiscardPriceRequest
) -> ApiResponse[PriceVersionView]:
    """DRAFT → DISCARDED. The row stays (drafts are never deleted)."""
    admin = require_admin(request)
    discarded = provider_prices.discard_draft(
        require_session_factory(request),
        actor=admin,
        price_version_id=price_version_id,
        context=request_context(request),
    )
    return success(discarded, request_id=current_request_id())

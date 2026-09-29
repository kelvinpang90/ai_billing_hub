"""Admin AI catalog endpoints: meter types, providers, models, aliases (design gate #163 v4).

契约见 [docs/api.md](../../docs/api.md)「AI 目录」。

⚠️ **每个处理函数的第一条语句是 `require_admin(request)`**，与 app/api/admin_customers.py
同一条：鉴权是显式调用，不挂在路由器上。`tests/backend/test_admin_customers_api.py` 从
应用的路由表枚举 `/api/v1/admin` 下的全部路由逐个验 401 / 403 且不写库；
`tests/backend/test_ai_catalog_api.py` 用 AST 钉住「第一条语句」。

⚠️ 请求体与查询参数由 FastAPI 在处理函数**之前**校验，所以没带令牌、参数又不合法的
请求拿到的是 422 而不是 401。422 只列字段名、不回显值（app/core/errors.py）。

路径里的 id 一律是 `public_id`；响应不含内部自增 id。别名段没有 PATCH：改映射只能再
POST 一次（开新段）。这一层不写日志。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query, Request, Response, status

from app.api.auth import request_context, require_admin, require_session_factory
from app.core.logging import current_request_id
from app.models.ai_catalog import MODEL_CODE_LENGTH, CatalogStatus, QuantityKind
from app.schemas.ai_catalog import (
    AliasSegmentView,
    CreateMeterTypeRequest,
    CreateModelRequest,
    CreateProviderRequest,
    MapAliasRequest,
    MeterTypeView,
    ModelDetail,
    ModelView,
    ProviderView,
    RetireAliasRequest,
    UpdateCatalogEntryRequest,
)
from app.schemas.customers import DEFAULT_PAGE_SIZE, MAX_PAGE, MAX_PAGE_SIZE, Page
from app.schemas.envelope import ApiResponse, success
from app.services import ai_catalog

router = APIRouter(prefix="/api/v1/admin", tags=["admin"])

# 超出范围是 422，不静默截断（spec §108）。与 admin_customers.py 的同名别名一致。
PageNumber = Annotated[int, Query(ge=1, le=MAX_PAGE)]
PageSize = Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)]
# 列表默认显示全部，可按 `status` 筛（设计 §2「停用的含义」）。取值以外的是 422。
# 参数名另取，免得遮住 `fastapi.status`。
StatusFilter = Annotated[CatalogStatus | None, Query(alias="status")]

_METER_TYPES = "/usage-meter-types"
_PROVIDERS = "/ai-providers"
_MODELS = _PROVIDERS + "/{provider_id}/models"
_ALIASES = _PROVIDERS + "/{provider_id}/model-aliases"


# --- 计量类型 ---------------------------------------------------------------------


@router.get(_METER_TYPES, response_model=ApiResponse[Page[MeterTypeView]])
def list_meter_types(
    request: Request,
    status_filter: StatusFilter = None,
    page: PageNumber = 1,
    page_size: PageSize = DEFAULT_PAGE_SIZE,
) -> ApiResponse[Page[MeterTypeView]]:
    """`code` ascending, each with its components."""
    require_admin(request)
    listing = ai_catalog.list_meter_types(
        require_session_factory(request), status=status_filter, page=page, page_size=page_size
    )
    return success(listing, request_id=current_request_id())


@router.post(
    _METER_TYPES,
    status_code=status.HTTP_201_CREATED,
    response_model=ApiResponse[MeterTypeView],
)
def create_meter_type(
    request: Request, payload: CreateMeterTypeRequest
) -> ApiResponse[MeterTypeView]:
    """A `QUANTITY` type and its one component (from `quantity`), with the audit, at once."""
    admin = require_admin(request)
    created = ai_catalog.create_meter_type(
        require_session_factory(request),
        actor=admin,
        code=payload.code,
        display_name=payload.display_name,
        unit=payload.unit,
        quantity_kind=QuantityKind(payload.quantity_kind),
        component_code=payload.component_code,
        context=request_context(request),
    )
    return success(created, request_id=current_request_id())


@router.get(_METER_TYPES + "/{meter_type_id}", response_model=ApiResponse[MeterTypeView])
def get_meter_type(request: Request, meter_type_id: str) -> ApiResponse[MeterTypeView]:
    require_admin(request)
    detail = ai_catalog.get_meter_type(require_session_factory(request), meter_type_id)
    return success(detail, request_id=current_request_id())


@router.patch(_METER_TYPES + "/{meter_type_id}", response_model=ApiResponse[MeterTypeView])
def update_meter_type(
    request: Request, meter_type_id: str, payload: UpdateCatalogEntryRequest
) -> ApiResponse[MeterTypeView]:
    """Only `display_name` and `status`. No real change: 200 and nothing is written."""
    admin = require_admin(request)
    updated = ai_catalog.update_meter_type(
        require_session_factory(request),
        actor=admin,
        meter_type_id=meter_type_id,
        changes=payload.changes(),
        context=request_context(request),
    )
    return success(updated, request_id=current_request_id())


# --- 供应商 -----------------------------------------------------------------------


@router.get(_PROVIDERS, response_model=ApiResponse[Page[ProviderView]])
def list_providers(
    request: Request,
    status_filter: StatusFilter = None,
    page: PageNumber = 1,
    page_size: PageSize = DEFAULT_PAGE_SIZE,
) -> ApiResponse[Page[ProviderView]]:
    """`code` ascending."""
    require_admin(request)
    listing = ai_catalog.list_providers(
        require_session_factory(request), status=status_filter, page=page, page_size=page_size
    )
    return success(listing, request_id=current_request_id())


@router.post(
    _PROVIDERS,
    status_code=status.HTTP_201_CREATED,
    response_model=ApiResponse[ProviderView],
)
def create_provider(request: Request, payload: CreateProviderRequest) -> ApiResponse[ProviderView]:
    admin = require_admin(request)
    created = ai_catalog.create_provider(
        require_session_factory(request),
        actor=admin,
        code=payload.code,
        display_name=payload.display_name,
        context=request_context(request),
    )
    return success(created, request_id=current_request_id())


@router.get(_PROVIDERS + "/{provider_id}", response_model=ApiResponse[ProviderView])
def get_provider(request: Request, provider_id: str) -> ApiResponse[ProviderView]:
    require_admin(request)
    detail = ai_catalog.get_provider(require_session_factory(request), provider_id)
    return success(detail, request_id=current_request_id())


@router.patch(_PROVIDERS + "/{provider_id}", response_model=ApiResponse[ProviderView])
def update_provider(
    request: Request, provider_id: str, payload: UpdateCatalogEntryRequest
) -> ApiResponse[ProviderView]:
    """Only `display_name` and `status`. Retiring does not affect resolution or billing."""
    admin = require_admin(request)
    updated = ai_catalog.update_provider(
        require_session_factory(request),
        actor=admin,
        provider_id=provider_id,
        changes=payload.changes(),
        context=request_context(request),
    )
    return success(updated, request_id=current_request_id())


# --- 模型 -------------------------------------------------------------------------


@router.get(_MODELS, response_model=ApiResponse[Page[ModelView]])
def list_models(
    request: Request,
    provider_id: str,
    status_filter: StatusFilter = None,
    page: PageNumber = 1,
    page_size: PageSize = DEFAULT_PAGE_SIZE,
) -> ApiResponse[Page[ModelView]]:
    """Only this provider's models, `code` ascending."""
    require_admin(request)
    listing = ai_catalog.list_models(
        require_session_factory(request),
        provider_id=provider_id,
        status=status_filter,
        page=page,
        page_size=page_size,
    )
    return success(listing, request_id=current_request_id())


@router.post(
    _MODELS,
    status_code=status.HTTP_201_CREATED,
    response_model=ApiResponse[ModelView],
)
def create_model(
    request: Request, provider_id: str, payload: CreateModelRequest
) -> ApiResponse[ModelView]:
    """The provider comes from the path only; the write runs inside its row lock."""
    admin = require_admin(request)
    created = ai_catalog.create_model(
        require_session_factory(request),
        actor=admin,
        provider_id=provider_id,
        code=payload.code,
        display_name=payload.display_name,
        context=request_context(request),
    )
    return success(created, request_id=current_request_id())


@router.get(_MODELS + "/{model_id}", response_model=ApiResponse[ModelDetail])
def get_model(request: Request, provider_id: str, model_id: str) -> ApiResponse[ModelDetail]:
    """With the untruncated alias segments that point at the model."""
    require_admin(request)
    detail = ai_catalog.get_model(
        require_session_factory(request), provider_id=provider_id, model_id=model_id
    )
    return success(detail, request_id=current_request_id())


@router.patch(_MODELS + "/{model_id}", response_model=ApiResponse[ModelView])
def update_model(
    request: Request, provider_id: str, model_id: str, payload: UpdateCatalogEntryRequest
) -> ApiResponse[ModelView]:
    """Only `display_name` and `status`. A retired model still resolves."""
    admin = require_admin(request)
    updated = ai_catalog.update_model(
        require_session_factory(request),
        actor=admin,
        provider_id=provider_id,
        model_id=model_id,
        changes=payload.changes(),
        context=request_context(request),
    )
    return success(updated, request_id=current_request_id())


# --- 别名段 -----------------------------------------------------------------------


@router.get(_ALIASES, response_model=ApiResponse[Page[AliasSegmentView]])
def list_model_aliases(
    request: Request,
    provider_id: str,
    alias: Annotated[str | None, Query(max_length=MODEL_CODE_LENGTH)] = None,
    current: bool = False,
    page: PageNumber = 1,
    page_size: PageSize = DEFAULT_PAGE_SIZE,
) -> ApiResponse[Page[AliasSegmentView]]:
    """The whole history, by `alias` then `effective_from`; `current=true`: untruncated only."""
    require_admin(request)
    listing = ai_catalog.list_aliases(
        require_session_factory(request),
        provider_id=provider_id,
        alias=alias,
        current=current,
        page=page,
        page_size=page_size,
    )
    return success(listing, request_id=current_request_id())


@router.post(
    _ALIASES,
    status_code=status.HTTP_201_CREATED,
    response_model=ApiResponse[AliasSegmentView],
)
def map_model_alias(
    request: Request, response: Response, provider_id: str, payload: MapAliasRequest
) -> ApiResponse[AliasSegmentView]:
    """Map from now on; the response is the untruncated segment.

    写了新段是 201；已经指向这个模型是 200，什么都不写。
    """
    admin = require_admin(request)
    mapped = ai_catalog.map_alias(
        require_session_factory(request),
        actor=admin,
        provider_id=provider_id,
        alias=payload.alias,
        model_id=payload.model_id,
        context=request_context(request),
    )
    if not mapped.created:
        response.status_code = status.HTTP_200_OK
    return success(mapped.segment, request_id=current_request_id())


@router.post(_ALIASES + "/{alias_id}/retire", response_model=ApiResponse[AliasSegmentView])
def retire_model_alias(
    request: Request, provider_id: str, alias_id: str, payload: RetireAliasRequest
) -> ApiResponse[AliasSegmentView]:
    """Truncate the untruncated segment; anything else is a 404 and writes nothing."""
    admin = require_admin(request)
    retired = ai_catalog.retire_alias(
        require_session_factory(request),
        actor=admin,
        provider_id=provider_id,
        alias_id=alias_id,
        context=request_context(request),
    )
    return success(retired, request_id=current_request_id())

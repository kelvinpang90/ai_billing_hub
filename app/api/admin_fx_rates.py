"""Admin FX rate endpoints: versions, manual drafts, publish, retire, fetch attempts.

设计闸门 #183 v3 §2「接口」（AIH-TASK-041）。
契约见 [docs/api.md](../../docs/api.md)「管理端汇率」。

⚠️ **每个处理函数的第一条语句是 `require_admin(request)`**，与 app/api/admin_customers.py
同一条：鉴权是显式调用，不挂在路由器上。`tests/backend/test_admin_customers_api.py` 从
应用的路由表枚举 `/api/v1/admin` 下的全部路由逐个验 401 / 403 且不写库；
`tests/backend/test_fx_rates_api.py` 用 AST 钉住「第一条语句」。

⚠️ **`GET …/fx-rates/fetch-attempts` 必须先于 `GET …/fx-rates/{fx_rate_id}` 注册**：路由按
注册顺序匹配，反过来 `fetch-attempts` 会被当成版本 id、得到 404。

⚠️ 请求体与查询参数由 FastAPI 在处理函数**之前**校验，所以请求校验先于状态判断（设计 v3）；
没带令牌、参数又不合法的请求拿到的是 422 而不是 401。422 只列字段名、不回显值。

⚠️ 读接口（列表、详情、拉取记录）是普通读，不拿 `fx_rate_locks`：只展示，不参与区间计算，
不应让发布等它们。

路径里的 id 一律是版本的 `public_id`；响应不含内部自增 id。没有删除接口：草稿只能丢弃，
版本行永不删除。这一层不写日志。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query, Request, status

from app.api.auth import request_context, require_admin, require_session_factory
from app.core.logging import current_request_id
from app.models.fx_rates import FxFetchOutcome, FxRateSourceType, FxRateStatus
from app.schemas.customers import DEFAULT_PAGE_SIZE, MAX_PAGE, MAX_PAGE_SIZE, Page
from app.schemas.envelope import ApiResponse, success
from app.schemas.fx_rates import (
    CreateFxRateRequest,
    DiscardFxRateRequest,
    FxFetchAttemptItem,
    FxRateView,
    PublishFxRateRequest,
    RetireFxRateRequest,
    UpdateFxRateRequest,
    fetch_attempt_item,
    fx_rate_view,
)
from app.services import fx_rates

router = APIRouter(prefix="/api/v1/admin", tags=["admin"])

# 超出范围是 422，不静默截断（spec §108）。与 admin_customers.py 的同名别名一致。
PageNumber = Annotated[int, Query(ge=1, le=MAX_PAGE)]
PageSize = Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)]
# 取值以外的是 422。参数名另取，免得遮住 `fastapi.status`。
StatusFilter = Annotated[FxRateStatus | None, Query(alias="status")]
SourceFilter = Annotated[FxRateSourceType | None, Query(alias="source")]
OutcomeFilter = Annotated[FxFetchOutcome | None, Query(alias="outcome")]
# 格式不对是 422，不是空列表。
BaseCurrencyFilter = Annotated[str | None, Query(pattern=r"^[A-Z]{3}$")]

_RATES = "/fx-rates"
_RATE = _RATES + "/{fx_rate_id}"


@router.get(_RATES, response_model=ApiResponse[Page[FxRateView]])
def list_fx_rates(
    request: Request,
    base_currency: BaseCurrencyFilter = None,
    status_filter: StatusFilter = None,
    source_filter: SourceFilter = None,
    page: PageNumber = 1,
    page_size: PageSize = DEFAULT_PAGE_SIZE,
) -> ApiResponse[Page[FxRateView]]:
    """Newest first (`id` descending); filter by currency, `status` and `source`."""
    require_admin(request)
    versions, total = fx_rates.list_fx_rates(
        require_session_factory(request),
        base_currency=base_currency,
        status=status_filter,
        source=source_filter,
        page=page,
        page_size=page_size,
    )
    listing = Page[FxRateView](
        items=[fx_rate_view(version) for version in versions],
        page=page,
        page_size=page_size,
        total=total,
    )
    return success(listing, request_id=current_request_id())


@router.post(_RATES, status_code=status.HTTP_201_CREATED, response_model=ApiResponse[FxRateView])
def create_fx_rate(request: Request, payload: CreateFxRateRequest) -> ApiResponse[FxRateView]:
    """A `MANUAL` draft and its audit, at once. No FX lock (drafts are not billed)."""
    admin = require_admin(request)
    created = fx_rates.create_draft(
        require_session_factory(request),
        actor=admin,
        base_currency=payload.base_currency,
        rate=payload.rate,
        observed_at=payload.observed_at,
        source_reference=payload.source_reference,
        context=request_context(request),
    )
    return success(fx_rate_view(created), request_id=current_request_id())


# ⚠️ 先于 `_RATE` 注册（见模块说明）。
@router.get(_RATES + "/fetch-attempts", response_model=ApiResponse[Page[FxFetchAttemptItem]])
def list_fx_fetch_attempts(
    request: Request,
    base_currency: BaseCurrencyFilter = None,
    outcome_filter: OutcomeFilter = None,
    page: PageNumber = 1,
    page_size: PageSize = DEFAULT_PAGE_SIZE,
) -> ApiResponse[Page[FxFetchAttemptItem]]:
    """BNM fetch attempts (spec §58), by `attempted_at` then `id`, both descending."""
    require_admin(request)
    attempts, total = fx_rates.list_fetch_attempts(
        require_session_factory(request),
        base_currency=base_currency,
        outcome=outcome_filter,
        page=page,
        page_size=page_size,
    )
    listing = Page[FxFetchAttemptItem](
        items=[fetch_attempt_item(attempt) for attempt in attempts],
        page=page,
        page_size=page_size,
        total=total,
    )
    return success(listing, request_id=current_request_id())


@router.get(_RATE, response_model=ApiResponse[FxRateView])
def get_fx_rate(request: Request, fx_rate_id: str) -> ApiResponse[FxRateView]:
    require_admin(request)
    detail = fx_rates.get_fx_rate(require_session_factory(request), fx_rate_id)
    return success(fx_rate_view(detail), request_id=current_request_id())


@router.patch(_RATE, response_model=ApiResponse[FxRateView])
def update_fx_rate(
    request: Request, fx_rate_id: str, payload: UpdateFxRateRequest
) -> ApiResponse[FxRateView]:
    """Manual drafts only (a BNM draft is 409). No real change: 200, nothing written."""
    admin = require_admin(request)
    updated = fx_rates.update_draft(
        require_session_factory(request),
        actor=admin,
        fx_rate_id=fx_rate_id,
        rate=payload.rate,
        observed_at=payload.observed_at,
        source_reference=payload.source_reference,
        context=request_context(request),
    )
    return success(fx_rate_view(updated), request_id=current_request_id())


@router.post(_RATE + "/publish", response_model=ApiResponse[FxRateView])
def publish_fx_rate(
    request: Request, fx_rate_id: str, payload: PublishFxRateRequest
) -> ApiResponse[FxRateView]:
    """Publish inside the FX lock. Already published: 200, nothing written, F ignored."""
    admin = require_admin(request)
    published = fx_rates.publish(
        require_session_factory(request),
        actor=admin,
        fx_rate_id=fx_rate_id,
        effective_from=payload.effective_from,
        context=request_context(request),
    )
    return success(fx_rate_view(published), request_id=current_request_id())


@router.post(_RATE + "/retire", response_model=ApiResponse[FxRateView])
def retire_fx_rate(
    request: Request, fx_rate_id: str, payload: RetireFxRateRequest
) -> ApiResponse[FxRateView]:
    """Retire the current version, or withdraw a reservation that has not started."""
    admin = require_admin(request)
    retired = fx_rates.retire(
        require_session_factory(request),
        actor=admin,
        fx_rate_id=fx_rate_id,
        reason=payload.reason,
        context=request_context(request),
    )
    return success(fx_rate_view(retired), request_id=current_request_id())


@router.post(_RATE + "/discard", response_model=ApiResponse[FxRateView])
def discard_fx_rate(
    request: Request, fx_rate_id: str, payload: DiscardFxRateRequest
) -> ApiResponse[FxRateView]:
    """DRAFT → DISCARDED, BNM drafts too. The row stays (drafts are never deleted)."""
    admin = require_admin(request)
    discarded = fx_rates.discard_draft(
        require_session_factory(request),
        actor=admin,
        fx_rate_id=fx_rate_id,
        context=request_context(request),
    )
    return success(fx_rate_view(discarded), request_id=current_request_id())

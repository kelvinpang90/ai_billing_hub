"""Admin rebill endpoints: preview, confirm, list and detail.

设计闸门 #830 v12 §2「管理端接口」。

契约见 [docs/api.md](../../docs/api.md)「管理端 Rebill」。实现登记为 AIH-TASK-256；口径、选事件、
预览指纹与确认的两个事务都在 app/services/usage_rebill.py，这一层只做请求校验、鉴权与响应形状。

⚠️ **每个处理函数的第一条语句是 `require_admin(request)`**，与 app/api/admin_usage_events.py
同一条：鉴权是显式调用，不挂在路由器上。`tests/backend/test_admin_customers_api.py` 从应用的
路由表枚举 `/api/v1/admin` 下的全部路由逐个验 401 / 403 且不写库；
`tests/backend/test_admin_rebills_api.py` 用 AST 钉住「第一条语句」。

⚠️ 路径名用 `rebills`，不是 spec §89 的 `reprocess`（设计 §1 第 9 条）。路径里的 id 是 Rebill 的
`public_id`。明细里有按更正价格版本算出的估算成本：只在管理端（INV-7）。

请求体、`limit` 由 FastAPI 在处理函数**之前**校验（422 先于鉴权）；列表的 `cursor` 在鉴权**之后**
判。查询只读：一个普通会话，不提交、不写任何行，不写审计。确认 201，同一幂等键重放 200 加
`replayed: true`。这一层不写日志。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query, Request, Response, status

from app.api.auth import request_context, require_admin, require_session_factory
from app.core.errors import AppError
from app.core.logging import current_request_id
from app.repositories import tenancy
from app.repositories import usage_rebills as rebill_repository
from app.schemas.envelope import ApiResponse, success
from app.schemas.usage_rebills import (
    DEFAULT_LIST_LIMIT,
    MAX_LIST_LIMIT,
    ConfirmRebillRequest,
    PreviewRebillRequest,
    RebillConfirmView,
    RebillDetailView,
    RebillPageView,
    RebillPreviewView,
    rebill_confirm_view,
    rebill_detail_view,
    rebill_page_view,
    rebill_preview_view,
)
from app.services import usage_rebill
from app.services.auth import utc_now

router = APIRouter(prefix="/api/v1/admin", tags=["admin"])

_REBILLS = "/rebills"

# 超出范围是 422，不静默截断（spec §108）。
Limit = Annotated[int, Query(ge=1, le=MAX_LIST_LIMIT)]


class RebillNotFound(AppError):
    def __init__(self) -> None:
        super().__init__("Rebill not found.", code="REBILL_NOT_FOUND", http_status=404)


class InvalidRebillQuery(AppError):
    """A query value only the handler can judge (after `require_admin`). Names, never values."""

    def __init__(self, *fields: str) -> None:
        names = ", ".join(f"query.{name}" for name in sorted(fields))
        super().__init__(
            f"Invalid request fields: {names}", code="VALIDATION_ERROR", http_status=422
        )


@router.post(_REBILLS + "/preview", response_model=ApiResponse[RebillPreviewView])
def preview_usage_rebill(
    request: Request, payload: PreviewRebillRequest
) -> ApiResponse[RebillPreviewView]:
    """Old charge, new charge and difference per event, the totals and the preview token.

    只读：不写库、不写审计。没有可入账的差额时照常 200，`ledger_type` / `ledger_amount` 为空。
    """
    require_admin(request)
    preview = usage_rebill.preview_rebill(
        require_session_factory(request), request=payload.rebill_request()
    )
    return success(rebill_preview_view(preview), request_id=current_request_id())


@router.post(
    _REBILLS,
    status_code=status.HTTP_201_CREATED,
    response_model=ApiResponse[RebillConfirmView],
)
def confirm_usage_rebill(
    request: Request, response: Response, payload: ConfirmRebillRequest
) -> ApiResponse[RebillConfirmView]:
    """Post the previewed rebill: 201 the first time, 200 when the key is replayed.

    客户只来自请求体的 `customer_id`，操作者只来自令牌。预览之后数据或参数变了：409
    `REBILL_PREVIEW_STALE`，重新预览后再确认。
    """
    admin = require_admin(request)
    result = usage_rebill.confirm_rebill(
        require_session_factory(request),
        actor=admin,
        request=payload.rebill_request(),
        preview_token=payload.preview_token,
        reason=payload.reason,
        idempotency_key=payload.idempotency_key,
        context=request_context(request),
        clock=utc_now,
    )
    if result.replayed:
        # 重放不入账：200 加 `replayed: true`，调用方看得出这次没有新的财务效果。
        response.status_code = status.HTTP_200_OK
    return success(rebill_confirm_view(result), request_id=current_request_id())


@router.get(_REBILLS, response_model=ApiResponse[RebillPageView])
def list_usage_rebills(
    request: Request,
    customer_id: str | None = None,
    cursor: str | None = None,
    limit: Limit = DEFAULT_LIST_LIMIT,
) -> ApiResponse[RebillPageView]:
    """Newest first by row id; `next_cursor` for the next page. No total.

    `cursor` 是上一页最后一条的 `public_id`，不存在 → 422。`customer_id` 不存在时是空页（与
    用量事件列表的同名筛选一样）。
    """
    require_admin(request)
    with require_session_factory(request)() as session:
        before_id: int | None = None
        if cursor is not None:
            anchor = rebill_repository.get_rebill_row(session, cursor)
            if anchor is None:
                raise InvalidRebillQuery("cursor")
            before_id = anchor.id
        tenant_id: int | None = None
        if customer_id is not None:
            tenant = tenancy.get_tenant_by_public_id(session, customer_id)
            if tenant is None:
                empty = RebillPageView(items=[], next_cursor=None)
                return success(empty, request_id=current_request_id())
            tenant_id = tenant.id
        page = rebill_repository.list_rebills(
            session, tenant_id=tenant_id, before_id=before_id, limit=limit
        )
        listing = rebill_page_view(page)
    return success(listing, request_id=current_request_id())


@router.get(_REBILLS + "/{rebill_id}", response_model=ApiResponse[RebillDetailView])
def get_usage_rebill(request: Request, rebill_id: str) -> ApiResponse[RebillDetailView]:
    """The rebill, its two corrective references, its ledger row and every line."""
    require_admin(request)
    with require_session_factory(request)() as session:
        found = rebill_repository.get_rebill(session, rebill_id)
        if found is None:
            raise RebillNotFound
        detail = rebill_detail_view(found)
    return success(detail, request_id=current_request_id())

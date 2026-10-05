"""Admin usage event endpoints: requeue one, requeue many (design gate #181 v2 §2「重新入队」);
list and detail (spec §61 / §89, AIH-TASK-034).

契约见 [docs/api.md](../../docs/api.md)「管理端用量事件：查询」与「管理端用量事件：重新入队」。

⚠️ **每个处理函数的第一条语句是 `require_admin(request)`**，与 app/api/admin_customers.py
同一条：鉴权是显式调用，不挂在路由器上。`tests/backend/test_admin_customers_api.py` 从
应用的路由表枚举 `/api/v1/admin` 下的全部路由逐个验 401 / 403 且不写库；
`tests/backend/test_usage_requeue_api.py` 用 AST 钉住「第一条语句」。

⚠️ 路径名用 `requeue`，不是 §89 的 `reprocess`：Phase 8 的 reprocess 会改变已计费金额。
路径里的 id 是事件的 `public_id`；重新入队的响应只含数量与 `public_id`，不含成本。

查询只读：一个普通会话，不提交、不写任何行，不写审计（与审计日志查询一样）。分页与 `status`
由 FastAPI 在处理函数**之前**校验（422 先于鉴权）；时间段在鉴权**之后**解析。成本、计费额与
毛利只在这两个管理端接口里（INV-7）。这一层不写日志。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query, Request

from app.api.auth import request_context, require_admin, require_session_factory
from app.core.logging import current_request_id
from app.models.usage import UsageEventStatus
from app.repositories import usage_events as event_repository
from app.schemas.customers import DEFAULT_PAGE_SIZE, MAX_PAGE, MAX_PAGE_SIZE, Page
from app.schemas.envelope import ApiResponse, success
from app.schemas.usage_events import (
    UsageEventDetail,
    UsageEventSummary,
    parse_period,
    usage_event_detail,
    usage_event_summary,
)
from app.schemas.usage_requeue import (
    RequeuedUsageEventsView,
    RequeuedUsageEventView,
    RequeueUsageEventRequest,
    RequeueUsageEventsRequest,
)
from app.services import usage_requeue

router = APIRouter(prefix="/api/v1/admin", tags=["admin"])

_EVENTS = "/usage-events"

# 超出范围是 422，不静默截断（spec §108）。与 admin_audit.py 的同名别名一致。
PageNumber = Annotated[int, Query(ge=1, le=MAX_PAGE)]
PageSize = Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)]


@router.get(_EVENTS, response_model=ApiResponse[Page[UsageEventSummary]])
def list_usage_events(
    request: Request,
    page: PageNumber = 1,
    page_size: PageSize = DEFAULT_PAGE_SIZE,
    customer_id: str | None = None,
    project_id: str | None = None,
    conversation_id: str | None = None,
    request_id: str | None = None,
    provider: str | None = None,
    model: str | None = None,
    occurred_from: str | None = None,
    occurred_to: str | None = None,
    status: UsageEventStatus | None = None,
    error_code: str | None = None,
) -> ApiResponse[Page[UsageEventSummary]]:
    """Newest first by row id; every filter optional, combined with AND."""
    require_admin(request)
    start, end = parse_period(occurred_from, occurred_to)
    with require_session_factory(request)() as session:
        rows, total = event_repository.list_events(
            session,
            offset=(page - 1) * page_size,
            limit=page_size,
            customer_id=customer_id,
            project_id=project_id,
            conversation_id=conversation_id,
            request_id=request_id,
            provider=provider,
            model=model,
            occurred_from=start,
            occurred_to=end,
            status=status,
            error_code=error_code,
        )
        listing = Page[UsageEventSummary](
            items=[usage_event_summary(row) for row in rows],
            page=page,
            page_size=page_size,
            total=total,
        )
    return success(listing, request_id=current_request_id())


@router.get(_EVENTS + "/{usage_event_id}", response_model=ApiResponse[UsageEventDetail])
def get_usage_event(request: Request, usage_event_id: str) -> ApiResponse[UsageEventDetail]:
    """Snapshots, version and rule ids, cost, billable amount, margin, ledger row, conflicts."""
    require_admin(request)
    with require_session_factory(request)() as session:
        found = event_repository.get_event_detail(session, usage_event_id)
        if found is None:
            raise usage_requeue.UsageEventNotFound
        detail = usage_event_detail(found)
    return success(detail, request_id=current_request_id())


@router.post(_EVENTS + "/requeue", response_model=ApiResponse[RequeuedUsageEventsView])
def requeue_usage_events(
    request: Request, payload: RequeueUsageEventsRequest
) -> ApiResponse[RequeuedUsageEventsView]:
    """Up to 1000 events in one error state back to RECEIVED, one transaction and audit each."""
    admin = require_admin(request)
    result = usage_requeue.requeue_events(
        require_session_factory(request),
        actor=admin,
        status=UsageEventStatus(payload.status),
        error_code=payload.error_code,
        customer_id=payload.customer_id,
        provider=payload.provider,
        model=payload.model,
        occurred_from=payload.occurred_from,
        occurred_to=payload.occurred_to,
        reason=payload.reason,
        context=request_context(request),
    )
    view = RequeuedUsageEventsView(
        requeued=result.requeued, skipped=result.skipped, ids=list(result.ids)
    )
    return success(view, request_id=current_request_id())


@router.post(
    _EVENTS + "/{usage_event_id}/requeue", response_model=ApiResponse[RequeuedUsageEventView]
)
def requeue_usage_event(
    request: Request, usage_event_id: str, payload: RequeueUsageEventRequest
) -> ApiResponse[RequeuedUsageEventView]:
    """One event in an error state, never billed, back to RECEIVED (409 otherwise)."""
    admin = require_admin(request)
    public_id = usage_requeue.requeue_event(
        require_session_factory(request),
        actor=admin,
        usage_event_id=usage_event_id,
        reason=payload.reason,
        context=request_context(request),
    )
    view = RequeuedUsageEventView(id=public_id, status=UsageEventStatus.RECEIVED.value)
    return success(view, request_id=current_request_id())

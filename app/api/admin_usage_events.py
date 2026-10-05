"""Admin usage event endpoints: requeue one, requeue many (design gate #181 v2 §2「重新入队」).

AIH-TASK-032 只有这两个写接口；管理端用量查询（AIH-TASK-034）以后加在同一个文件里。
契约见 [docs/api.md](../../docs/api.md)「管理端用量事件：重新入队」。

⚠️ **每个处理函数的第一条语句是 `require_admin(request)`**，与 app/api/admin_customers.py
同一条：鉴权是显式调用，不挂在路由器上。`tests/backend/test_admin_customers_api.py` 从
应用的路由表枚举 `/api/v1/admin` 下的全部路由逐个验 401 / 403 且不写库；
`tests/backend/test_usage_requeue_api.py` 用 AST 钉住「第一条语句」。

⚠️ 路径名用 `requeue`，不是 §89 的 `reprocess`：Phase 8 的 reprocess 会改变已计费金额。
路径里的 id 是事件的 `public_id`；响应只含数量与 `public_id`，不含成本。这一层不写日志。
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from app.api.auth import request_context, require_admin, require_session_factory
from app.core.logging import current_request_id
from app.models.usage import UsageEventStatus
from app.schemas.envelope import ApiResponse, success
from app.schemas.usage_requeue import (
    RequeuedUsageEventsView,
    RequeuedUsageEventView,
    RequeueUsageEventRequest,
    RequeueUsageEventsRequest,
)
from app.services import usage_requeue

router = APIRouter(prefix="/api/v1/admin", tags=["admin"])

_EVENTS = "/usage-events"


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

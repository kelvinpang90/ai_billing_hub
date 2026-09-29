"""Admin audit log query endpoint (spec §89; AIH-TASK-022).

契约见 [docs/api.md](../../docs/api.md)「管理端审计日志」。

⚠️ **处理函数的第一件事是 `require_admin(request)`**，与 app/api/admin_customers.py 同一条：
鉴权是显式调用，不挂在路由器上。`tests/backend/test_admin_customers_api.py` 从路由表枚举
`/api/v1/admin` 下的全部路由逐个验 401 / 403。

⚠️ 分页与 `action` 由 FastAPI 在处理函数**之前**校验（不合法就 422，先于鉴权）；时间格式、
时间区间与「用户类型带 `entity_id`」由服务层在鉴权**之后**判，同样是 422
`VALIDATION_ERROR`。未知的查询参数不报错，与其他列表接口一致。

只读：不写审计，也不写日志。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query, Request

from app.api.auth import require_admin, require_session_factory
from app.core.logging import current_request_id
from app.models.auth import AuditAction
from app.schemas.audit_logs import AuditLogView
from app.schemas.customers import DEFAULT_PAGE_SIZE, MAX_PAGE, MAX_PAGE_SIZE, Page
from app.schemas.envelope import ApiResponse, success
from app.services import audit_query

router = APIRouter(prefix="/api/v1/admin", tags=["admin"])

# 超出范围是 422，不静默截断（spec §108）。与 admin_customers.py 的同名别名一致。
PageNumber = Annotated[int, Query(ge=1, le=MAX_PAGE)]
PageSize = Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)]


@router.get("/audit-logs", response_model=ApiResponse[Page[AuditLogView]])
def list_audit_logs(
    request: Request,
    page: PageNumber = 1,
    page_size: PageSize = DEFAULT_PAGE_SIZE,
    action: AuditAction | None = None,
    entity_type: str | None = None,
    entity_id: str | None = None,
    actor_email: str | None = None,
    created_from: str | None = None,
    created_to: str | None = None,
) -> ApiResponse[Page[AuditLogView]]:
    """Newest first by row id; internal ids are replaced by emails."""
    require_admin(request)
    listing = audit_query.list_audit_logs(
        require_session_factory(request),
        page=page,
        page_size=page_size,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        actor_email=actor_email,
        created_from=created_from,
        created_to=created_to,
    )
    return success(listing, request_id=current_request_id())

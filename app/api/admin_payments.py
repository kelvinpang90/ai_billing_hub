"""Admin payments: list, detail and the review resolution (AIH-TASK-183).

设计闸门 #585 v3 §2「管理端接口」、§6；契约见 [docs/api.md](../../docs/api.md)「支付（管理端）」。

⚠️ **每个处理函数的第一条语句是 `require_admin(request)`**，与 app/api/admin_customers.py
同一条：鉴权是显式调用，不挂在路由器上。`tests/backend/test_admin_customers_api.py` 从路由表枚举
`/api/v1/admin` 下的全部路由逐个验 401 / 403 且不写库。

⚠️ 路由**总是注册**（app/main.py），与 `payments_enabled` 无关：开关关闭时列表为空、读接口照常
可用。结案时开关为真取 `get_payment_gateway(settings)`、否则为 `None`，交给 `resolve_review`：
CREDIT 没有网关是 503 `PAYMENTS_MISCONFIGURED`（在 404 / 409 前提之后），CLOSE 不需要网关。
`resolve_review` 的异常都是 `AppError`，原样交给错误信封。

列表与详情只读：一个普通会话（不加锁、不提交），不写审计。分页与 `status` 由 FastAPI 在处理函数
**之前**校验（422 先于鉴权）；创建日期在鉴权**之后**解析。结案的请求体同样在处理函数之前校验。

⚠️ 这一层不写日志：原因文本只进库与审计（设计 §6），`resolve_review` 自己的日志不带它。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query, Request
from sqlalchemy.orm import Session, sessionmaker

from app.api.auth import request_context, require_admin, require_session_factory
from app.core.logging import current_request_id
from app.models.payments import PaymentStatus
from app.payments import get_payment_gateway
from app.schemas.admin_payments import (
    AdminPaymentDetail,
    AdminPaymentSummary,
    ResolvePaymentRequest,
    admin_payment_detail,
    admin_payment_summary,
)
from app.schemas.customers import DEFAULT_PAGE_SIZE, MAX_PAGE, MAX_PAGE_SIZE, Page
from app.schemas.envelope import ApiResponse, success
from app.services import payment_admin
from app.services.auth import utc_now
from app.services.payment_review import ReviewPaymentNotFound, resolve_review

router = APIRouter(prefix="/api/v1/admin", tags=["admin"])

_PAYMENTS = "/payments"

# 超出范围是 422，不静默截断（spec §108）。与 admin_usage_events.py 的同名别名一致。
PageNumber = Annotated[int, Query(ge=1, le=MAX_PAGE)]
PageSize = Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)]


@router.get(_PAYMENTS, response_model=ApiResponse[Page[AdminPaymentSummary]])
def list_payments(
    request: Request,
    page: PageNumber = 1,
    page_size: PageSize = DEFAULT_PAGE_SIZE,
    status: PaymentStatus | None = None,
    customer_id: str | None = None,
    created_from: str | None = None,
    created_to: str | None = None,
) -> ApiResponse[Page[AdminPaymentSummary]]:
    """Newest `created_at` first, then descending row id; every filter optional, ANDed."""
    require_admin(request)
    start, end = payment_admin.parse_created_range(created_from, created_to)
    with require_session_factory(request)() as session:
        rows, total = payment_admin.list_payments(
            session,
            offset=(page - 1) * page_size,
            limit=page_size,
            status=status,
            customer_id=customer_id,
            created_from=start,
            created_to=end,
        )
        listing = Page[AdminPaymentSummary](
            items=[admin_payment_summary(row) for row in rows],
            page=page,
            page_size=page_size,
            total=total,
        )
    return success(listing, request_id=current_request_id())


@router.get(_PAYMENTS + "/{payment_id}", response_model=ApiResponse[AdminPaymentDetail])
def get_payment(request: Request, payment_id: str) -> ApiResponse[AdminPaymentDetail]:
    """One payment with its credited amount, resolver and events; 404 `PAYMENT_NOT_FOUND`."""
    require_admin(request)
    detail = _detail(require_session_factory(request), payment_id)
    return success(detail, request_id=current_request_id())


@router.post(_PAYMENTS + "/{payment_id}/resolve", response_model=ApiResponse[AdminPaymentDetail])
def resolve_payment(
    request: Request, payment_id: str, payload: ResolvePaymentRequest
) -> ApiResponse[AdminPaymentDetail]:
    """CREDIT or CLOSE one `REVIEW_REQUIRED` payment; 200 with the detail read back."""
    admin = require_admin(request)
    settings = request.app.state.settings
    gateway = get_payment_gateway(settings) if settings.payments_enabled else None
    factory = require_session_factory(request)
    resolved = resolve_review(
        factory,
        gateway,
        actor=admin,
        payment_id=payment_id,
        action=payload.action,
        reason=payload.reason,
        context=request_context(request),
        # MySQL 的 DATETIME 不存小数秒。
        now=utc_now().replace(microsecond=0),
    )
    # `resolve_review` 已提交；用与 GET 详情相同的查询重新读出。
    detail = _detail(factory, resolved)
    return success(detail, request_id=current_request_id())


def _detail(factory: sessionmaker[Session], payment_id: str) -> AdminPaymentDetail:
    with factory() as session:
        found = payment_admin.get_payment_detail(session, payment_id)
        if found is None:
            raise ReviewPaymentNotFound
        return admin_payment_detail(found)


__all__ = ["router"]

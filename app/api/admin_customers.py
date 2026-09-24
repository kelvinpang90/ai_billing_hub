"""Admin customer management endpoints (spec §56, §124; design gate #96 v3 §2).

契约见 [docs/api.md](../../docs/api.md)。

⚠️ **每个处理函数的第一件事是 `require_admin(request)`。**鉴权是显式调用，不是挂在
路由器上的依赖（设计 §9，与 `require_current_user` 的用法一致）。漏调一个就是一个
谁都能调的管理端接口 —— `tests/backend/test_admin_customers_api.py` 从应用的路由表
枚举本前缀下的全部路由，逐个验 401 / 403 且不写库；新增接口不补进那里，测试会红。

⚠️ 请求体与查询参数由 FastAPI 在处理函数**之前**校验，所以没带令牌、参数又不合法的
请求拿到的是 422 而不是 401。422 只列字段名、不回显值（app/core/errors.py）。

这一层不记请求体，也不记 `email` / `contact_name` / `phone`（REQ-PRIV-001）。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query, Request, Response, status

from app.api.auth import request_context, require_admin, require_session_factory
from app.core.logging import current_request_id
from app.schemas.customers import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE,
    MAX_PAGE_SIZE,
    CreateCustomerRequest,
    CreateProjectRequest,
    CustomerDetail,
    CustomerSummary,
    Page,
    ProjectView,
    UpdateCustomerRequest,
)
from app.schemas.envelope import ApiResponse, success
from app.schemas.wallet_adjustments import AdjustmentView, PostAdjustmentRequest
from app.services import customers, wallet_adjustments

router = APIRouter(prefix="/api/v1/admin", tags=["admin"])

# 超出范围是 422，不静默截断（spec §108）。
PageNumber = Annotated[int, Query(ge=1, le=MAX_PAGE)]
PageSize = Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)]


@router.post(
    "/customers",
    status_code=status.HTTP_201_CREATED,
    response_model=ApiResponse[CustomerDetail],
)
def create_customer(
    request: Request, payload: CreateCustomerRequest
) -> ApiResponse[CustomerDetail]:
    """Create a customer; its MYR wallet and the audit row commit with it."""
    admin = require_admin(request)
    detail = customers.create_customer(
        require_session_factory(request),
        actor=admin,
        company_name=payload.company_name,
        email=payload.email,
        contact_name=payload.contact_name,
        phone=payload.phone,
        context=request_context(request),
    )
    return success(detail, request_id=current_request_id())


@router.get("/customers", response_model=ApiResponse[Page[CustomerSummary]])
def list_customers(
    request: Request, page: PageNumber = 1, page_size: PageSize = DEFAULT_PAGE_SIZE
) -> ApiResponse[Page[CustomerSummary]]:
    """Newest first."""
    require_admin(request)
    listing = customers.list_customers(
        require_session_factory(request), page=page, page_size=page_size
    )
    return success(listing, request_id=current_request_id())


@router.get("/customers/{customer_id}", response_model=ApiResponse[CustomerDetail])
def get_customer(request: Request, customer_id: str) -> ApiResponse[CustomerDetail]:
    """`customer_id` is the customer's `public_id`; anything else is a 404."""
    require_admin(request)
    detail = customers.get_customer(require_session_factory(request), customer_id)
    return success(detail, request_id=current_request_id())


@router.patch("/customers/{customer_id}", response_model=ApiResponse[CustomerDetail])
def update_customer(
    request: Request, customer_id: str, payload: UpdateCustomerRequest
) -> ApiResponse[CustomerDetail]:
    """Partial profile edit; the row and its `CUSTOMER_UPDATE` audit commit together."""
    admin = require_admin(request)
    detail = customers.update_customer(
        require_session_factory(request),
        actor=admin,
        customer_id=customer_id,
        changes=payload.changes(),
        context=request_context(request),
    )
    return success(detail, request_id=current_request_id())


@router.post(
    "/customers/{customer_id}/projects",
    status_code=status.HTTP_201_CREATED,
    response_model=ApiResponse[ProjectView],
)
def create_project(
    request: Request, customer_id: str, payload: CreateProjectRequest
) -> ApiResponse[ProjectView]:
    """The project belongs to the customer in the path, never to anything in the body."""
    admin = require_admin(request)
    project = customers.create_project(
        require_session_factory(request),
        actor=admin,
        customer_id=customer_id,
        name=payload.name,
        description=payload.description,
        context=request_context(request),
    )
    return success(project, request_id=current_request_id())


@router.get("/customers/{customer_id}/projects", response_model=ApiResponse[Page[ProjectView]])
def list_projects(
    request: Request,
    customer_id: str,
    page: PageNumber = 1,
    page_size: PageSize = DEFAULT_PAGE_SIZE,
) -> ApiResponse[Page[ProjectView]]:
    """Oldest first, only this customer's projects."""
    require_admin(request)
    listing = customers.list_projects(
        require_session_factory(request),
        customer_id=customer_id,
        page=page,
        page_size=page_size,
    )
    return success(listing, request_id=current_request_id())


# 手工调账（AIH-TASK-011，设计闸门 #111 v1）。首次 201，同一个幂等键同一载荷的重放 200、
# `replayed: true`。⚠️ 这里刻意不写 docstring：函数体的第一条语句必须就是 `require_admin`，
# `tests/backend/test_wallet_adjustment_api.py` 读源码钉住这一条。
@router.post(
    "/customers/{customer_id}/wallet/adjustments",
    status_code=status.HTTP_201_CREATED,
    response_model=ApiResponse[AdjustmentView],
)
def post_wallet_adjustment(
    request: Request, response: Response, customer_id: str, payload: PostAdjustmentRequest
) -> ApiResponse[AdjustmentView]:
    admin = require_admin(request)
    adjustment = wallet_adjustments.post_adjustment(
        require_session_factory(request),
        actor=admin,
        customer_id=customer_id,
        transaction_type=payload.transaction_type,
        amount=payload.decimal_amount(),
        reason=payload.reason,
        idempotency_key=payload.idempotency_key,
        context=request_context(request),
    )
    if adjustment.replayed:
        response.status_code = status.HTTP_200_OK
    return success(adjustment, request_id=current_request_id())

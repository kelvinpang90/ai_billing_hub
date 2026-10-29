"""Admin read-only views of wallet reconciliation (AIH-TASK-134).

设计闸门 #474 v13 §2「管理端接口」；契约见 [docs/api.md](../../docs/api.md)「钱包核对」。

⚠️ **处理函数的第一件事是 `require_admin(request)`**，与 app/api/admin_audit.py 同一条：
鉴权是显式调用，不挂在路由器上。`tests/backend/test_admin_customers_api.py` 从路由表枚举
`/api/v1/admin` 下的全部路由逐个验 401 / 403。

⚠️ `run_id` 是普通字符串路径参数：路由守卫用字面路径请求、期望 401 / 403，
所以形状不在 FastAPI 层校验，而在鉴权**之后**判；
不是小写规范 UUID 与查不到同为 404。
分页由 FastAPI 在处理函数之前校验（超出范围 422，先于鉴权）。

只读：一个普通会话（不加锁、不提交），不写审计，也不写日志（设计 §6）。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query, Request
from sqlalchemy import func, select

from app.api.auth import require_admin, require_session_factory
from app.core.logging import current_request_id
from app.models.tenancy import Tenant
from app.models.wallet_reconciliation import WalletReconciliationFinding, WalletReconciliationRun
from app.schemas.customers import DEFAULT_PAGE_SIZE, MAX_PAGE, MAX_PAGE_SIZE, Page
from app.schemas.envelope import ApiResponse, success
from app.schemas.wallet_reconciliation import (
    ReconciliationRunNotFound,
    WalletReconciliationFindingView,
    WalletReconciliationRunView,
    is_run_id,
    reconciliation_finding_view,
    reconciliation_run_view,
)

router = APIRouter(prefix="/api/v1/admin", tags=["admin"])

# 超出范围是 422，不静默截断（spec §108）。与 admin_audit.py 的同名别名一致。
PageNumber = Annotated[int, Query(ge=1, le=MAX_PAGE)]
PageSize = Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)]

Run = WalletReconciliationRun
Finding = WalletReconciliationFinding


def _offset(page: int, page_size: int) -> int:
    return (page - 1) * page_size


@router.get(
    "/wallet-reconciliation/runs",
    response_model=ApiResponse[Page[WalletReconciliationRunView]],
)
def list_reconciliation_runs(
    request: Request,
    page: PageNumber = 1,
    page_size: PageSize = DEFAULT_PAGE_SIZE,
) -> ApiResponse[Page[WalletReconciliationRunView]]:
    """Latest start first; runs started in the same second by descending row id."""
    require_admin(request)
    listing = (
        select(Run)
        .order_by(Run.started_at.desc(), Run.id.desc())
        .offset(_offset(page, page_size))
        .limit(page_size)
    )
    counting = select(func.count()).select_from(Run)
    with require_session_factory(request)() as session:
        runs = session.scalars(listing).all()
        total = session.execute(counting).scalar_one()
        listing_page = Page[WalletReconciliationRunView](
            items=[reconciliation_run_view(run) for run in runs],
            page=page,
            page_size=page_size,
            total=total,
        )
    return success(listing_page, request_id=current_request_id())


@router.get(
    "/wallet-reconciliation/runs/{run_id}/findings",
    response_model=ApiResponse[Page[WalletReconciliationFindingView]],
)
def list_reconciliation_findings(
    request: Request,
    run_id: str,
    page: PageNumber = 1,
    page_size: PageSize = DEFAULT_PAGE_SIZE,
) -> ApiResponse[Page[WalletReconciliationFindingView]]:
    """The inconsistent wallets of one run, by ascending tenant row id."""
    require_admin(request)
    if not is_run_id(run_id):
        raise ReconciliationRunNotFound
    with require_session_factory(request)() as session:
        internal_id = session.execute(
            select(Run.id).where(Run.public_id == run_id)
        ).scalar_one_or_none()
        if internal_id is None:
            raise ReconciliationRunNotFound
        listing = (
            select(Finding, Tenant.public_id, Tenant.company_name)
            .join(Tenant, Tenant.id == Finding.tenant_id)
            .where(Finding.run_id == internal_id)
            .order_by(Finding.tenant_id.asc())
            .offset(_offset(page, page_size))
            .limit(page_size)
        )
        counting = select(func.count()).select_from(Finding).where(Finding.run_id == internal_id)
        found = session.execute(listing).all()
        total = session.execute(counting).scalar_one()
        listing_page = Page[WalletReconciliationFindingView](
            items=[
                reconciliation_finding_view(
                    finding, customer_id=customer_id, company_name=company_name
                )
                for finding, customer_id, company_name in found
            ],
            page=page,
            page_size=page_size,
            total=total,
        )
    return success(listing_page, request_id=current_request_id())

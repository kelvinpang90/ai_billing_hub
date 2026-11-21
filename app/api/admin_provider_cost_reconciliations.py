"""Admin provider cost reconciliation: months, preview, confirm, detail (AIH-TASK-245).

设计闸门 #755 v1 §2「管理端接口」、§6；
契约见 [docs/api.md](../../docs/api.md)「管理端供应商成本对账」。业务规则、检查顺序、事务边界与
审计全在 `app/services/provider_cost_reconciliation.py`，这一层只做鉴权、解析与格式化。

⚠️ **每个处理函数的第一条语句是 `require_admin(request)`**，与 app/api/admin_customers.py
同一条：鉴权是显式调用，不挂在路由器上。`tests/backend/test_admin_customers_api.py` 从
应用的路由表枚举 `/api/v1/admin` 下的全部路由逐个验 401 / 403 且不写库。

⚠️ **`GET …/months` 与 `POST …/preview` 必须先于 `GET …/{reconciliation_id}` 注册**：路由按
注册顺序匹配（app/api/admin_fx_rates.py 文件头记的同一个坑），反过来 `months` 会被当成记录 id、
得到 404。

⚠️ 请求体与查询参数由 FastAPI 在处理函数**之前**校验：没带令牌、参数又不合法的请求拿到的是 422
而不是 401。422 只列字段名、不回显值。路径里的 id 是记录的 `public_id`，**不做格式校验**：查不到
（含不是 UUID 的值）一律 404 `PROVIDER_COST_RECONCILIATION_NOT_FOUND`。

⚠️ 这一层不写日志：金额与来源说明只进记录与审计（设计 §6）。`now` 取 `utc_now()`（测试 monkeypatch
本模块的这个名字）。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query, Request, status

from app.api.auth import request_context, require_admin, require_session_factory
from app.core.logging import current_request_id
from app.schemas.envelope import ApiResponse, success
from app.schemas.provider_cost_reconciliations import (
    ConfirmReconciliationRequest,
    PreviewReconciliationRequest,
    ReconciliationDetailView,
    ReconciliationMonthList,
    ReconciliationView,
    detail_view,
    month_list,
    reconciliation_view,
)
from app.services import provider_cost_reconciliation as reconciliation
from app.services.auth import utc_now

router = APIRouter(prefix="/api/v1/admin", tags=["admin"])

# 必填；格式不对是 422。格式对但目录里没有是 404 `AI_PROVIDER_NOT_FOUND`（服务层判）。
ProviderCodeQuery = Annotated[str, Query(pattern=r"^[a-z0-9_.-]{1,64}$")]

_RECONCILIATIONS = "/provider-cost-reconciliations"


# ⚠️ 先于 `/{reconciliation_id}` 注册（见模块说明）。
@router.get(_RECONCILIATIONS + "/months", response_model=ApiResponse[ReconciliationMonthList])
def list_reconciliation_months(
    request: Request, provider_code: ProviderCodeQuery
) -> ApiResponse[ReconciliationMonthList]:
    """Every ended UTC month from the first reconcilable one, latest first, at most 24."""
    require_admin(request)
    months = reconciliation.months_overview(
        require_session_factory(request), provider_code=provider_code, now=utc_now()
    )
    return success(month_list(months), request_id=current_request_id())


# ⚠️ 先于 `/{reconciliation_id}` 注册（见模块说明）。
@router.post(_RECONCILIATIONS + "/preview", response_model=ApiResponse[ReconciliationView])
def preview_reconciliation(
    request: Request, payload: PreviewReconciliationRequest
) -> ApiResponse[ReconciliationView]:
    """What the confirmation would write; nothing is written (the transaction is rolled back)."""
    require_admin(request)
    previewed = reconciliation.preview(
        require_session_factory(request),
        provider_code=payload.provider_code,
        billing_month=payload.billing_month,
        source_currency=payload.source_currency,
        source_total=payload.source_total,
        source_document_reference=payload.source_document_reference,
        supersedes_id=payload.supersedes_id,
        reason=payload.reason,
        now=utc_now(),
    )
    return success(reconciliation_view(previewed), request_id=current_request_id())


@router.post(
    _RECONCILIATIONS,
    status_code=status.HTTP_201_CREATED,
    response_model=ApiResponse[ReconciliationView],
)
def confirm_reconciliation(
    request: Request, payload: ConfirmReconciliationRequest
) -> ApiResponse[ReconciliationView]:
    """One immutable record and its audit, at once; 409 if the estimate moved since the preview."""
    admin = require_admin(request)
    confirmed = reconciliation.confirm(
        require_session_factory(request),
        actor=admin,
        context=request_context(request),
        provider_code=payload.provider_code,
        billing_month=payload.billing_month,
        source_currency=payload.source_currency,
        source_total=payload.source_total,
        source_document_reference=payload.source_document_reference,
        supersedes_id=payload.supersedes_id,
        reason=payload.reason,
        expected_event_count=payload.expected_event_count,
        expected_estimated_source_cost=payload.expected_estimated_source_cost,
        now=utc_now(),
    )
    return success(reconciliation_view(confirmed), request_id=current_request_id())


@router.get(
    _RECONCILIATIONS + "/{reconciliation_id}",
    response_model=ApiResponse[ReconciliationDetailView],
)
def get_reconciliation(
    request: Request, reconciliation_id: str
) -> ApiResponse[ReconciliationDetailView]:
    """The record, its correction chain and the live estimate of its month."""
    require_admin(request)
    detail = reconciliation.get_detail(
        require_session_factory(request), reconciliation_id, now=utc_now()
    )
    return success(detail_view(detail), request_id=current_request_id())

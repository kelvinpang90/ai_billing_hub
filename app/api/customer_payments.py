"""Customer payments: top-up options, payments and receipts (AIH-TASK-190, AIH-TASK-202).

设计闸门 #587 v3 §2「客户接口」、§6；收据见设计闸门 #589 v2 §2「客户接口」、§6（AIH-TASK-202）；
契约见 [docs/api.md](../../docs/api.md)「支付（客户）」。

⚠️ **每个处理函数的第一条语句是 `require_customer(request)`**（与 app/api/customer.py 同一条）：
五个接口都只读，`DISABLED` / `CLOSED` 也能看。漏调由 `tests/backend/test_customer_api.py` 的路由
守卫用例兜底。

收据下载由 app/api/receipt_download.py 完成（管理端共用）：这里只传令牌主体的租户。`receipt_id`
同样不做格式校验：不存在、别的客户的、不像 uuid 的同是 404 `RECEIPT_NOT_FOUND`；快照不合规是
500 `INTERNAL_ERROR`（那里记 ERROR）。

⚠️ **租户只取自 `CustomerPrincipal.tenant_id`**（INV-8）：路径、查询串里都没有租户参数，带了也不读。

⚠️ 路由**总是注册**（app/main.py），与 `payments_enabled` 无关：开关关闭时充值选项给出
`PAYMENTS_DISABLED`，列表与详情照常可读。只有充值选项读支付设置（读不到 503
`PAYMENTS_MISCONFIGURED`）；不查 Stripe、不读密钥文件。

两个列表的 `page` / `page_size` 由 FastAPI 在处理函数**之前**校验（422 先于鉴权），与管理端支付列表
相同。详情的 `payment_id` 不做格式校验：不存在、别的客户的、不像 uuid 的同是 404
`PAYMENT_NOT_FOUND`。

只读：一个普通会话（不加锁、不提交），不写审计，这一层也不写日志。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query, Request

from app.api.auth import require_customer, require_session_factory
from app.api.receipt_download import ReceiptPdfResponse, receipt_pdf_response
from app.core.logging import current_request_id
from app.schemas.customer_payments import (
    CustomerPaymentView,
    CustomerReceiptView,
    CustomerTopupOptions,
    customer_payment_view,
    customer_receipt_view,
    customer_topup_options,
)
from app.schemas.customers import DEFAULT_PAGE_SIZE, MAX_PAGE, MAX_PAGE_SIZE, Page
from app.schemas.envelope import ApiResponse, success
from app.services import customer_payments
from app.services.payment_review import ReviewPaymentNotFound

router = APIRouter(prefix="/api/v1/customer", tags=["customer"])

_PAYMENTS = "/payments"
_RECEIPTS = "/receipts"

# 超出范围是 422，不静默截断（spec §108）。与 admin_payments.py 的同名别名一致。
PageNumber = Annotated[int, Query(ge=1, le=MAX_PAGE)]
PageSize = Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)]


@router.get("/topup-options", response_model=ApiResponse[CustomerTopupOptions])
def customer_topup_options_view(request: Request) -> ApiResponse[CustomerTopupOptions]:
    """Whether the caller can top up now and which amounts are accepted."""
    principal = require_customer(request)
    with require_session_factory(request)() as session:
        options = customer_payments.topup_options(
            session, principal.tenant_id, settings=request.app.state.settings
        )
    return success(customer_topup_options(options), request_id=current_request_id())


@router.get(_PAYMENTS, response_model=ApiResponse[Page[CustomerPaymentView]])
def customer_payment_list(
    request: Request,
    page: PageNumber = 1,
    page_size: PageSize = DEFAULT_PAGE_SIZE,
) -> ApiResponse[Page[CustomerPaymentView]]:
    """The caller's own payments, newest `created_at` first, then descending row id."""
    principal = require_customer(request)
    with require_session_factory(request)() as session:
        rows, total = customer_payments.list_payments(
            session, principal.tenant_id, offset=(page - 1) * page_size, limit=page_size
        )
        listing = Page[CustomerPaymentView](
            items=[customer_payment_view(row) for row in rows],
            page=page,
            page_size=page_size,
            total=total,
        )
    return success(listing, request_id=current_request_id())


@router.get(_PAYMENTS + "/{payment_id}", response_model=ApiResponse[CustomerPaymentView])
def customer_payment_detail(request: Request, payment_id: str) -> ApiResponse[CustomerPaymentView]:
    """One of the caller's own payments; 404 `PAYMENT_NOT_FOUND` otherwise."""
    principal = require_customer(request)
    with require_session_factory(request)() as session:
        found = customer_payments.get_payment(session, principal.tenant_id, payment_id)
        if found is None:
            raise ReviewPaymentNotFound
        view = customer_payment_view(found)
    return success(view, request_id=current_request_id())


@router.get(_RECEIPTS, response_model=ApiResponse[Page[CustomerReceiptView]])
def customer_receipt_list(
    request: Request,
    page: PageNumber = 1,
    page_size: PageSize = DEFAULT_PAGE_SIZE,
) -> ApiResponse[Page[CustomerReceiptView]]:
    """The caller's own receipts, newest `issued_at` first, then descending row id."""
    principal = require_customer(request)
    with require_session_factory(request)() as session:
        rows, total = customer_payments.list_receipts(
            session, principal.tenant_id, offset=(page - 1) * page_size, limit=page_size
        )
        listing = Page[CustomerReceiptView](
            items=[customer_receipt_view(row) for row in rows],
            page=page,
            page_size=page_size,
            total=total,
        )
    return success(listing, request_id=current_request_id())


@router.get(_RECEIPTS + "/{receipt_id}/download", response_class=ReceiptPdfResponse)
def customer_receipt_download(request: Request, receipt_id: str) -> ReceiptPdfResponse:
    """One of the caller's own receipts as a PDF attachment; 404 `RECEIPT_NOT_FOUND` otherwise."""
    principal = require_customer(request)
    return receipt_pdf_response(
        require_session_factory(request), receipt_id, tenant_id=principal.tenant_id
    )


__all__ = ["router"]

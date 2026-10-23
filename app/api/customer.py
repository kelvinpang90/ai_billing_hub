"""Customer portal endpoints (design gate #368 v12 §2「客户端」, §6).

契约见 [docs/api.md](../../docs/api.md)「客户门户认证」。

⚠️ **每个处理函数的第一条语句是 `require_customer(request)`**（写接口用
`require_customer_writable(request)`）。与管理端的 `require_admin` 一样是显式调用，不挂路由器
依赖；漏调由 `tests/backend/test_customer_api.py` 的路由守卫用例兜底。

⚠️ **租户只取自 `CustomerPrincipal.tenant_id`**（INV-8、spec §90）：这里的路径、查询串、请求体都
没有租户参数，带了也不读。

⚠️ 以后的客户写接口用 `require_customer_writable`，并且**必须**在自己的写事务里、写入之前调用
`app.services.account_status.lock_tenant_for_customer_write`：依赖只是提前拒绝
（设计 §2「依赖」）。

钱包与流水（AIH-TASK-099，设计闸门 #393 v3 §2）：只读，四种账户状态都能读，
用 `require_customer`。一个普通会话、不加锁、不提交、不写审计；与管理端钱包流水一样直接调
repository。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query, Request

from app.api.auth import require_customer, require_session_factory
from app.core.logging import current_request_id
from app.models.tenancy import Tenant
from app.repositories import wallet as wallet_repository
from app.schemas.customer import CustomerMe
from app.schemas.customer_wallet import (
    CustomerWalletTransactionPage,
    CustomerWalletView,
    customer_wallet_transaction_page,
    customer_wallet_view,
)
from app.schemas.customers import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE
from app.schemas.envelope import ApiResponse, success
from app.schemas.wallet_transactions import MAX_WALLET_SEQUENCE
from app.services.account_status import READ_ONLY_STATUSES
from app.services.customers import CustomerNotFound

router = APIRouter(prefix="/api/v1/customer", tags=["customer"])

# 与管理端钱包流水同一范围；超出是 422，不静默截断。
PageSize = Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)]
BeforeSequence = Annotated[int | None, Query(ge=1, le=MAX_WALLET_SEQUENCE)]


@router.get("/me", response_model=ApiResponse[CustomerMe])
def customer_me(request: Request) -> ApiResponse[CustomerMe]:
    """The caller's own customer: `{customer_id, company_name, account_status, read_only}`.

    四种账户状态都能读：只读限制在写接口那一层（AIH-TASK-020 的契约）。
    """
    principal = require_customer(request)
    with require_session_factory(request)() as session:
        tenant = session.get(Tenant, principal.tenant_id)
        if tenant is None:
            # 外键保证不会发生。
            raise CustomerNotFound
        view = CustomerMe(
            customer_id=tenant.public_id,
            company_name=tenant.company_name,
            account_status=tenant.account_status,
            # 与 `require_customer_writable` 同一个集合：两处对「只读」的判定不会对不上。
            read_only=tenant.account_status in READ_ONLY_STATUSES,
        )
    return success(view, request_id=current_request_id())


@router.get("/wallet", response_model=ApiResponse[CustomerWalletView])
def customer_wallet(request: Request) -> ApiResponse[CustomerWalletView]:
    """The caller's own wallet: balance, billing state, and whether AI service is on.

    四种账户状态都能读（#368 只读契约）。
    """
    principal = require_customer(request)
    with require_session_factory(request)() as session:
        summary = wallet_repository.get_wallet_summary(session, principal.tenant_id)
        if summary is None:
            # 每个客户都有钱包（建客户同事务、迁移 0006 回填）。缺了是数据不一致，照管理端客户详情
            # 的先例走 500；消息只是问题码，不含任何客户数据。
            raise RuntimeError(wallet_repository.WALLET_MISSING)
        view = customer_wallet_view(summary)
    return success(view, request_id=current_request_id())


@router.get("/wallet/transactions", response_model=ApiResponse[CustomerWalletTransactionPage])
def customer_wallet_transactions(
    request: Request,
    page_size: PageSize = DEFAULT_PAGE_SIZE,
    before_sequence: BeforeSequence = None,
) -> ApiResponse[CustomerWalletTransactionPage]:
    """The caller's own ledger, newest first; pass `next_before_sequence` back for the next page.

    游标是本租户内的序号，拿别的客户的序号来也只会翻到自己的行（INV-8）。
    """
    principal = require_customer(request)
    with require_session_factory(request)() as session:
        # 先确认钱包存在：否则「没有钱包、也没有账本行」会被一个看似正常的空页掩盖
        # （设计 v3）。
        if wallet_repository.get_wallet_for_tenant(session, principal.tenant_id) is None:
            raise RuntimeError(wallet_repository.WALLET_MISSING)
        # 多取一行，只用来判断还有没有下一页；不按来源筛选。
        rows = wallet_repository.list_transactions_for_tenant(
            session,
            principal.tenant_id,
            limit=page_size + 1,
            before_sequence=before_sequence,
            reference_type=None,
        )
        page = customer_wallet_transaction_page(rows, page_size=page_size)
    return success(page, request_id=current_request_id())

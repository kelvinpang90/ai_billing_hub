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

项目与用量（AIH-TASK-104，设计闸门 #398 v4 §2）：同样只读、用 `require_customer`。用量只列
`PROCESSED`，日期按马来西亚日历日；类型、范围与格式由 FastAPI 在处理函数之前判（422），日期是否
存在、必填与先后、92 天与游标在鉴权之后判（422）。MySQL 上查询超过执行时间预算是 422
`QUERY_TOO_BROAD`，另记一条 `customer_usage_query_timeout` 日志：只带 `request_id` 与客户
`public_id`，不带任何查询参数的值（设计 §6）。
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Query, Request

from app.api.auth import require_customer, require_session_factory
from app.core.logging import current_request_id
from app.models.tenancy import Tenant
from app.repositories import tenancy as tenancy_repository
from app.repositories import usage_events as usage_repository
from app.repositories import wallet as wallet_repository
from app.schemas.customer import CustomerMe
from app.schemas.customer_usage import (
    USAGE_DATE_PATTERN,
    CustomerProjectList,
    CustomerUsageEventPage,
    CustomerUsageQueryTooBroad,
    customer_project_list,
    customer_usage_event_page,
    decode_cursor,
    usage_period,
)
from app.schemas.customer_wallet import (
    CustomerWalletTransactionPage,
    CustomerWalletView,
    customer_wallet_transaction_page,
    customer_wallet_view,
)
from app.schemas.customers import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE
from app.schemas.envelope import ApiResponse, success
from app.schemas.usage_ingest import (
    EVENT_ID_PATTERN,
    MODEL_PATTERN,
    PROVIDER_PATTERN,
    REQUEST_ID_PATTERN,
)
from app.schemas.wallet_transactions import MAX_WALLET_SEQUENCE
from app.services.account_status import READ_ONLY_STATUSES
from app.services.customers import CustomerNotFound

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/customer", tags=["customer"])

# 与管理端钱包流水同一范围；超出是 422，不静默截断。
PageSize = Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)]
BeforeSequence = Annotated[int | None, Query(ge=1, le=MAX_WALLET_SEQUENCE)]
# 用量筛选的格式与用量摄取相同（设计 §2「接口二」）。
UsageDate = Annotated[str | None, Query(pattern=USAGE_DATE_PATTERN)]
ProviderFilter = Annotated[str | None, Query(pattern=PROVIDER_PATTERN)]
ModelFilter = Annotated[str | None, Query(pattern=MODEL_PATTERN)]
ConversationFilter = Annotated[str | None, Query(pattern=REQUEST_ID_PATTERN)]
EventIdFilter = Annotated[str | None, Query(pattern=EVENT_ID_PATTERN)]


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


@router.get("/projects", response_model=ApiResponse[CustomerProjectList])
def customer_projects(request: Request) -> ApiResponse[CustomerProjectList]:
    """Every project of the caller, oldest first, each with exactly `id` and `name`. Not paged."""
    principal = require_customer(request)
    with require_session_factory(request)() as session:
        projects = tenancy_repository.list_projects_for_tenant(session, principal.tenant_id)
        view = customer_project_list(projects)
    return success(view, request_id=current_request_id())


@router.get("/usage-events", response_model=ApiResponse[CustomerUsageEventPage])
def customer_usage_events(
    request: Request,
    page_size: PageSize = DEFAULT_PAGE_SIZE,
    from_date: UsageDate = None,
    to_date: UsageDate = None,
    project_id: str | None = None,
    provider: ProviderFilter = None,
    model: ModelFilter = None,
    conversation_id: ConversationFilter = None,
    event_id: EventIdFilter = None,
    cursor: str | None = None,
) -> ApiResponse[CustomerUsageEventPage]:
    """The caller's billed usage events, newest first; pass `next_cursor` back for older ones.

    项目、`event_id`、`conversation_id` 与游标都只是租户条件之上的附加条件：拿别的客户的值来
    只会得到空页（INV-8）。
    """
    principal = require_customer(request)
    occurred_from, occurred_to = usage_period(from_date, to_date, has_event_id=event_id is not None)
    before = None if cursor is None else decode_cursor(cursor)
    factory = require_session_factory(request)
    try:
        with factory() as session:
            # 多取一行，只用来判断还有没有下一页。
            rows = usage_repository.list_processed_events_for_customer(
                session,
                principal.tenant_id,
                limit=page_size + 1,
                project_id=project_id,
                provider=provider,
                model=model,
                conversation_id=conversation_id,
                event_id=event_id,
                occurred_from=occurred_from,
                occurred_to=occurred_to,
                before=before,
            )
            page = customer_usage_event_page(rows, page_size=page_size)
    except usage_repository.QueryTooBroad:
        with factory() as session:
            tenant = session.get(Tenant, principal.tenant_id)
            customer_id = None if tenant is None else tenant.public_id
        # 不带任何查询参数的值（日期、项目、`event_id`、`conversation_id`）。
        logger.warning(
            "customer_usage_query_timeout",
            extra={"request_id": current_request_id(), "customer_id": customer_id},
        )
        raise CustomerUsageQueryTooBroad from None
    return success(page, request_id=current_request_id())

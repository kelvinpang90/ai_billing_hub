"""Admin monthly statements: list, detail and a manual catch-up per customer (AIH-TASK-159).

设计闸门 #565 v6 §2「管理端接口」、§6；
契约见 [docs/api.md](../../docs/api.md)「月度对账单（管理端）」。

⚠️ **每个处理函数的第一件事是 `require_admin(request)`**，与 app/api/admin_customers.py 同一条：
鉴权是显式调用，不挂在路由器上。`tests/backend/test_admin_customers_api.py` 从路由表枚举
`/api/v1/admin` 下的全部路由逐个验 401 / 403 且不写库。

列表与详情只读：一个普通会话（不加锁、不提交），不写审计。

手动补生成：生成逻辑全在 `app/services/statements.py` 的 `catch_up_tenant`，这一层只做鉴权、
判客户与合格性、转发，并经 `before_commit` 钩子在每份新对账单的同一事务里写一条
`STATEMENT_GENERATE` 审计（INV-13）。某一期失败时异常原样抛出，成为统一错误体的 500；
已提交的前几期与它们的审计保留。

⚠️ 这一层不写日志；错误体只有固定文案。金额与公司名都不出现在日志与错误体里（设计 §6）。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.auth import request_context, require_admin, require_session_factory
from app.core.logging import current_request_id
from app.models.auth import AuditAction, User
from app.models.statements import MonthlyStatement
from app.models.tenancy import BillingMode, Tenant
from app.models.wallet import Wallet
from app.repositories.tenancy import get_tenant_by_public_id
from app.schemas.customers import DEFAULT_PAGE_SIZE, MAX_PAGE, MAX_PAGE_SIZE, Page
from app.schemas.envelope import ApiResponse, success
from app.schemas.statements import (
    GeneratedStatement,
    StatementDetail,
    StatementGenerationNotEligible,
    StatementNotFound,
    StatementSummary,
    generated_statement,
    is_statement_id,
    parse_period,
    statement_detail,
    statement_summary,
)
from app.services import statements
from app.services.auth import record_audit, utc_now
from app.services.customers import CustomerNotFound

router = APIRouter(prefix="/api/v1/admin", tags=["admin"])

# 超出范围是 422，不静默截断（spec §108）。与 admin_customers.py 的同名别名一致。
PageNumber = Annotated[int, Query(ge=1, le=MAX_PAGE)]
PageSize = Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)]

# 审计的 entity_type（app/services/audit_query.py 的 PUBLIC_ENTITY_TYPES）。
STATEMENT_ENTITY_TYPE = "monthly_statement"


def _offset(page: int, page_size: int) -> int:
    return (page - 1) * page_size


@router.get("/statements", response_model=ApiResponse[Page[StatementSummary]])
def list_statements(
    request: Request,
    customer_id: str | None = None,
    period: str | None = None,
    page: PageNumber = 1,
    page_size: PageSize = DEFAULT_PAGE_SIZE,
) -> ApiResponse[Page[StatementSummary]]:
    """Latest period first, then descending row id. An unknown `customer_id` is an empty page."""
    require_admin(request)
    month = parse_period(period)
    conditions = []
    if customer_id is not None:
        conditions.append(Tenant.public_id == customer_id)
    if month is not None:
        conditions.append(MonthlyStatement.period_start == month.start)
    listing = (
        select(MonthlyStatement, Tenant.public_id)
        .join(Tenant, Tenant.id == MonthlyStatement.tenant_id)
        .where(*conditions)
        .order_by(MonthlyStatement.period_start.desc(), MonthlyStatement.id.desc())
        .offset(_offset(page, page_size))
        .limit(page_size)
    )
    counting = (
        select(func.count())
        .select_from(MonthlyStatement)
        .join(Tenant, Tenant.id == MonthlyStatement.tenant_id)
        .where(*conditions)
    )
    with require_session_factory(request)() as session:
        found = session.execute(listing).all()
        total = session.execute(counting).scalar_one()
        listing_page = Page[StatementSummary](
            items=[
                statement_summary(statement, customer_id=public_id)
                for statement, public_id in found
            ],
            page=page,
            page_size=page_size,
            total=total,
        )
    return success(listing_page, request_id=current_request_id())


@router.get("/statements/{statement_id}", response_model=ApiResponse[StatementDetail])
def get_statement(request: Request, statement_id: str) -> ApiResponse[StatementDetail]:
    """One statement with its whole snapshot; absent or malformed ids are the same 404."""
    require_admin(request)
    if not is_statement_id(statement_id):
        raise StatementNotFound
    # 左连接：系统生成的对账单没有生成者。
    query = (
        select(MonthlyStatement, Tenant.public_id, User.email)
        .join(Tenant, Tenant.id == MonthlyStatement.tenant_id)
        .outerjoin(User, User.id == MonthlyStatement.generated_by)
        .where(MonthlyStatement.public_id == statement_id)
    )
    with require_session_factory(request)() as session:
        row = session.execute(query).one_or_none()
        if row is None:
            raise StatementNotFound
        statement, customer_id, email = row
        detail = statement_detail(statement, customer_id=customer_id, generated_by_email=email)
    return success(detail, request_id=current_request_id())


@router.post(
    "/customers/{customer_id}/statements/generate",
    response_model=ApiResponse[list[GeneratedStatement]],
)
def generate_statements(
    request: Request, customer_id: str
) -> ApiResponse[list[GeneratedStatement]]:
    """Catch up every due statement the customer lacks; returns the new ones, oldest first.

    客户只来自路径（INV-8）。非预付或没有钱包 409；预付但没有到期账期 200 空列表。
    """
    admin = require_admin(request)
    factory = require_session_factory(request)
    with factory() as session:
        tenant = get_tenant_by_public_id(session, customer_id)
        if tenant is None:
            raise CustomerNotFound
        tenant_id = tenant.id
        has_wallet = (
            session.execute(select(Wallet.id).where(Wallet.tenant_id == tenant_id)).first()
            is not None
        )
        if tenant.billing_mode != BillingMode.PREPAID or not has_wallet:
            raise StatementGenerationNotEligible

    context = request_context(request)
    # MySQL 的 DATETIME 不存小数秒：与 app/tasks/statements.py 一样截到整秒。
    now = utc_now().replace(microsecond=0)

    def write_audit(session: Session, statement: MonthlyStatement) -> None:
        # 与对账单行同一事务；元数据只有账期，不带金额与公司名（设计 §6）。
        record_audit(
            session,
            action=AuditAction.STATEMENT_GENERATE,
            context=context,
            now=now,
            actor=admin,
            entity_type=STATEMENT_ENTITY_TYPE,
            entity_id=statement.public_id,
            after_state={"period": statement.snapshot_json["period"]},
        )

    generated = statements.catch_up_tenant(
        factory,
        tenant_id,
        now,
        generated_by=admin.id,
        before_commit=write_audit,
        raise_errors=True,
    )
    return success(
        [generated_statement(statement) for statement in generated],
        request_id=current_request_id(),
    )

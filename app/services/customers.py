"""Admin customer management (spec §56, §57, §124; design gate #96 v3).

⚠️ **每个写操作一个 `session_scope()`**（INV-13）。建客户：租户、钱包、`CUSTOMER_CREATE`
审计三行；建项目：项目与 `PROJECT_CREATE` 审计两行；改客户：租户行与 `CUSTOMER_UPDATE`
审计（AIH-TASK-009）。要么全在、要么全不在。repository
只 flush，提交与回滚都发生在这里的 `session_scope` 里。

⚠️ **客户只从路径里的 `public_id` 解析**，项目的 `tenant_id` 取解析出来的内部 id，
不来自请求体（INV-8）。查不到就是 `CUSTOMER_NOT_FOUND`。

⚠️ 这里**不写日志**。审计的 `after_state` 只放设计 §6 列出的字段：`email`、
`contact_name`、`phone` 是个人数据（REQ-PRIV-001），只存业务表，不进日志、不进长期
保留的审计表。
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from typing import Final

from sqlalchemy.orm import Session, sessionmaker

from app.core.database import session_scope
from app.core.errors import AppError
from app.models.auth import AuditAction, User
from app.models.tenancy import Tenant
from app.repositories import tenancy
from app.repositories.wallet import WALLET_MISSING, create_wallet, get_wallet_for_tenant
from app.schemas.customers import (
    CustomerDetail,
    CustomerSummary,
    Page,
    ProjectView,
    customer_detail,
    customer_summary,
    project_view,
)
from app.services.auth import RequestContext, record_audit, utc_now

# 审计的 entity_type。
ENTITY_TENANT: Final = "tenant"
ENTITY_PROJECT: Final = "project"


class CustomerNotFound(AppError):
    """No customer has this public id.

    管理端跨租户，所以这里没有「属于别人」一说；但内部 id、项目 id 或随手编的串都
    一样得到这一个 404，猜不出库里有什么。
    """

    def __init__(self) -> None:
        super().__init__("Customer not found.", code="CUSTOMER_NOT_FOUND", http_status=404)


def _now() -> dt.datetime:
    """`utc_now()` truncated to whole seconds.

    ⚠️ MySQL 的 `DATETIME` 不存小数秒（写入时四舍五入到秒）。不截的话，POST 响应里的
    时刻与之后 GET 读回的会差一点 —— 与余额 `"0"` / `"0.00000000"` 同一类不一致。
    """
    return utc_now().replace(microsecond=0)


def _offset(page: int, page_size: int) -> int:
    return (page - 1) * page_size


def _require_tenant(session: Session, customer_id: str, *, for_update: bool = False) -> Tenant:
    tenant = tenancy.get_tenant_by_public_id(session, customer_id, for_update=for_update)
    if tenant is None:
        raise CustomerNotFound
    return tenant


def create_customer(
    session_factory: sessionmaker[Session],
    *,
    actor: User,
    company_name: str,
    email: str,
    context: RequestContext,
    contact_name: str | None = None,
    phone: str | None = None,
    now: dt.datetime | None = None,
) -> CustomerDetail:
    """Tenant, empty MYR wallet and `CUSTOMER_CREATE` audit, committed together.

    新客户的 `billing_status` 取表的默认值 `SUSPENDED`（余额 0），不是跃迁，所以不写
    跃迁审计、不发 `tenant.billing_status_changed`（设计 §4）。
    """
    moment = now or _now()
    with session_scope(session_factory) as session:
        tenant = tenancy.create_tenant(
            session,
            company_name=company_name,
            email=email,
            contact_name=contact_name,
            phone=phone,
            now=moment,
        )
        wallet = create_wallet(session, tenant_id=tenant.id, now=moment)
        record_audit(
            session,
            action=AuditAction.CUSTOMER_CREATE,
            context=context,
            now=moment,
            actor=actor,
            entity_type=ENTITY_TENANT,
            entity_id=tenant.public_id,
            # ⚠️ 只有这四项（设计 §6）。email / contact_name / phone 不进审计。
            after_state={
                "public_id": tenant.public_id,
                "company_name": tenant.company_name,
                "billing_status": tenant.billing_status.value,
                "wallet_currency": wallet.currency,
            },
        )
        detail = customer_detail(tenant, wallet)
    return detail


def get_customer(session_factory: sessionmaker[Session], customer_id: str) -> CustomerDetail:
    with session_factory() as session:
        tenant = _require_tenant(session, customer_id)
        return _detail(session, tenant)


def _detail(session: Session, tenant: Tenant) -> CustomerDetail:
    wallet = get_wallet_for_tenant(session, tenant.id)
    if wallet is None:
        # 每个客户都有钱包（建客户同事务、迁移 0006 回填既有租户）。缺了是数据
        # 不一致，按意外异常走 500；消息只是问题码，不含任何客户数据。
        raise RuntimeError(WALLET_MISSING)
    return customer_detail(tenant, wallet)


def update_customer(
    session_factory: sessionmaker[Session],
    *,
    actor: User,
    customer_id: str,
    changes: Mapping[str, str | None],
    context: RequestContext,
    now: dt.datetime | None = None,
) -> CustomerDetail:
    """Edit the profile columns; the tenant row and `CUSTOMER_UPDATE` audit commit together.

    ⚠️ 只改 `tenancy.PROFILE_FIELDS` 里的列，钱包只读不写。客户行加锁再读，审计的
    前后状态就是这一次改动真正的前后。值与现有的完全相同时什么都不写：没有改动
    就没有可审计的动作，`updated_at` 也不变。

    审计里 `company_name` 记前后值；`email` / `contact_name` / `phone` 是个人数据
    （REQ-PRIV-001），只在 `changed_fields` 里记**字段名**，不记值。
    """
    moment = now or _now()
    with session_scope(session_factory) as session:
        tenant = _require_tenant(session, customer_id, for_update=True)
        before: dict[str, object] = {
            "public_id": tenant.public_id,
            "company_name": tenant.company_name,
        }
        changed = tenancy.update_tenant_profile(session, tenant, changes=changes, now=moment)
        if changed:
            record_audit(
                session,
                action=AuditAction.CUSTOMER_UPDATE,
                context=context,
                now=moment,
                actor=actor,
                entity_type=ENTITY_TENANT,
                entity_id=tenant.public_id,
                # ⚠️ 字段白名单：不要换成 dump 整行或请求体。
                before_state=before,
                after_state={
                    "public_id": tenant.public_id,
                    "company_name": tenant.company_name,
                    "changed_fields": changed,
                },
            )
        detail = _detail(session, tenant)
    return detail


def list_customers(
    session_factory: sessionmaker[Session], *, page: int, page_size: int
) -> Page[CustomerSummary]:
    """Newest first."""
    with session_factory() as session:
        rows, total = tenancy.list_tenants(
            session, offset=_offset(page, page_size), limit=page_size
        )
        return Page[CustomerSummary](
            items=[customer_summary(row) for row in rows],
            page=page,
            page_size=page_size,
            total=total,
        )


def create_project(
    session_factory: sessionmaker[Session],
    *,
    actor: User,
    customer_id: str,
    name: str,
    context: RequestContext,
    description: str | None = None,
    now: dt.datetime | None = None,
) -> ProjectView:
    """Project and `PROJECT_CREATE` audit in one commit. Unknown customer: 404, no writes."""
    moment = now or _now()
    with session_scope(session_factory) as session:
        tenant = _require_tenant(session, customer_id)
        project = tenancy.create_project(
            session,
            tenant_id=tenant.id,
            name=name,
            description=description,
            now=moment,
        )
        record_audit(
            session,
            action=AuditAction.PROJECT_CREATE,
            context=context,
            now=moment,
            actor=actor,
            entity_type=ENTITY_PROJECT,
            entity_id=project.public_id,
            after_state={
                "public_id": project.public_id,
                "name": project.name,
                "tenant_public_id": tenant.public_id,
            },
        )
        view = project_view(project)
    return view


def list_projects(
    session_factory: sessionmaker[Session], *, customer_id: str, page: int, page_size: int
) -> Page[ProjectView]:
    """Oldest first, only the projects of this customer."""
    with session_factory() as session:
        tenant = _require_tenant(session, customer_id)
        rows = tenancy.list_projects_for_tenant(
            session, tenant.id, offset=_offset(page, page_size), limit=page_size
        )
        return Page[ProjectView](
            items=[project_view(row) for row in rows],
            page=page,
            page_size=page_size,
            total=tenancy.count_projects_for_tenant(session, tenant.id),
        )


__all__ = [
    "ENTITY_PROJECT",
    "ENTITY_TENANT",
    "CustomerNotFound",
    "create_customer",
    "create_project",
    "get_customer",
    "list_customers",
    "list_projects",
    "update_customer",
]

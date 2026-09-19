"""Tenant and project data access (spec §75, §76, §97, §115).

⚠️ **只 flush，不 commit。**事务边界归调用方（`session_scope()`）：将来「建客户
→ 自动有钱包 → 写审计」要在同一个事务里成败一致，repository 自己提交就拆散了它。

⚠️ **按项目的读取一律带 `tenant_id`。**给定 `(tenant_id, public_id)` 查不到就是
`None`，不区分「不存在」与「属于别的租户」—— 区分开来本身就泄露了别人的项目
存在（spec §97、§115）。`tenant_id` 必须来自已认证的上下文，不能来自客户端输入。

⚠️ 这里**不写任何日志**：`contact_name` / `email` / `phone` 是个人数据
（REQ-PRIV-001），最省事的不泄露办法是这一层根本不记。
"""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.tenancy import Project, Tenant


def _new_public_id() -> str:
    return str(uuid.uuid4())


def create_tenant(
    session: Session,
    *,
    company_name: str,
    email: str,
    now: dt.datetime,
    contact_name: str | None = None,
    phone: str | None = None,
) -> Tenant:
    """Insert and flush a tenant. `now` is naive UTC supplied by the caller."""
    tenant = Tenant(
        public_id=_new_public_id(),
        company_name=company_name,
        contact_name=contact_name,
        email=email,
        phone=phone,
        created_at=now,
        updated_at=now,
    )
    session.add(tenant)
    session.flush()
    return tenant


def get_tenant_by_public_id(session: Session, public_id: str) -> Tenant | None:
    statement = select(Tenant).where(Tenant.public_id == public_id)
    return session.execute(statement).scalar_one_or_none()


def list_tenants(session: Session, *, offset: int, limit: int) -> tuple[list[Tenant], int]:
    """One page of tenants, newest first, and the total count.

    按内部 `id` 倒序：自增 id 就是创建顺序，`created_at` 同一秒内会并列。
    `total` 用一条 `COUNT`（数据量是百级，设计闸门 #96 §2）。
    """
    _check_page(offset, limit)
    total = session.execute(select(func.count()).select_from(Tenant)).scalar_one()
    statement = select(Tenant).order_by(Tenant.id.desc()).offset(offset).limit(limit)
    return list(session.execute(statement).scalars().all()), int(total)


def create_project(
    session: Session,
    *,
    tenant_id: int,
    name: str,
    now: dt.datetime,
    description: str | None = None,
) -> Project:
    """Insert and flush a project under `tenant_id` (internal id, not public_id)."""
    project = Project(
        public_id=_new_public_id(),
        tenant_id=tenant_id,
        name=name,
        description=description,
        created_at=now,
        updated_at=now,
    )
    session.add(project)
    session.flush()
    return project


def get_project_for_tenant(session: Session, tenant_id: int, public_id: str) -> Project | None:
    """None when absent **or** owned by another tenant — deliberately indistinguishable."""
    statement = select(Project).where(
        Project.tenant_id == tenant_id, Project.public_id == public_id
    )
    return session.execute(statement).scalar_one_or_none()


def list_projects_for_tenant(
    session: Session,
    tenant_id: int,
    *,
    offset: int | None = None,
    limit: int | None = None,
) -> list[Project]:
    """Oldest first. Without `offset` / `limit` it returns every project of the tenant."""
    statement = select(Project).where(Project.tenant_id == tenant_id).order_by(Project.id)
    _check_page(offset, limit)
    if offset is not None:
        statement = statement.offset(offset)
    if limit is not None:
        statement = statement.limit(limit)
    return list(session.execute(statement).scalars().all())


def count_projects_for_tenant(session: Session, tenant_id: int) -> int:
    statement = select(func.count()).select_from(Project).where(Project.tenant_id == tenant_id)
    return int(session.execute(statement).scalar_one())


def _check_page(offset: int | None, limit: int | None) -> None:
    # 边界由接口层校验（422）；这里只挡住调用方写错，免得负数被数据库各自解释。
    if offset is not None and offset < 0:
        raise ValueError("offset must not be negative")
    if limit is not None and limit < 1:
        raise ValueError("limit must be at least 1")

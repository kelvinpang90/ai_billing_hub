"""Read-only grouped usage analytics queries."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import NamedTuple

from sqlalchemy import ColumnElement, String, case, cast, func, select
from sqlalchemy.orm import Session

from app.models.ai_catalog import AiModel, AiProvider, PayloadShape
from app.models.tenancy import BillingMode, Project, Tenant
from app.models.usage import UsageEvent, UsageEventStatus


class UsageAnalyticsRow(NamedTuple):
    group_key: str | None
    group_name: str | None
    event_count: int
    token_event_count: int
    input_tokens: int
    output_tokens: int
    cache_creation_input_tokens: int
    cache_read_input_tokens: int
    prepaid_event_count: int
    prepaid_billable_cost: Decimal
    prepaid_estimated_cost: Decimal
    internal_event_count: int
    internal_estimated_cost: Decimal
    internal_reference_price: Decimal


def _sum(expression: ColumnElement[object]) -> ColumnElement[object]:
    return func.coalesce(func.sum(expression), 0)


def list_usage_analytics(
    session: Session,
    *,
    group_by: str,
    offset: int,
    limit: int,
    customer_id: str | None,
    project_id: str | None,
    provider: str | None,
    model: str | None,
    feature: str | None,
    occurred_from: dt.datetime,
    occurred_to: dt.datetime,
) -> tuple[list[UsageAnalyticsRow], int]:
    """Aggregate only processed events; filters use the list endpoint's raw-value semantics."""
    conditions: list[ColumnElement[bool]] = [
        UsageEvent.status == UsageEventStatus.PROCESSED,
        UsageEvent.occurred_at >= occurred_from,
        UsageEvent.occurred_at < occurred_to,
    ]
    if customer_id is not None:
        tenant_id = session.execute(
            select(Tenant.id).where(Tenant.public_id == customer_id)
        ).scalar_one_or_none()
        if tenant_id is None:
            return [], 0
        conditions.append(UsageEvent.tenant_id == tenant_id)
    if project_id is not None:
        project_row_id = session.execute(
            select(Project.id).where(Project.public_id == project_id)
        ).scalar_one_or_none()
        if project_row_id is None:
            return [], 0
        conditions.append(UsageEvent.project_id == project_row_id)
    if provider is not None:
        conditions.append(UsageEvent.provider_code_raw == provider)
    if model is not None:
        conditions.append(UsageEvent.model_code_raw == model)
    if feature is not None:
        conditions.append(UsageEvent.feature == feature)

    joins: list[object] = []
    if group_by == "provider":
        group_key, group_name = AiProvider.code, None
        joins.append((AiProvider, AiProvider.id == UsageEvent.provider_id))
    elif group_by == "model":
        group_key = AiProvider.code + ":" + AiModel.code
        group_name = None
        joins.extend(
            (
                (AiProvider, AiProvider.id == UsageEvent.provider_id),
                (AiModel, AiModel.id == UsageEvent.model_id),
            )
        )
    elif group_by == "customer":
        group_key, group_name = Tenant.public_id, Tenant.company_name
        joins.append((Tenant, Tenant.id == UsageEvent.tenant_id))
    elif group_by == "project":
        group_key, group_name = Project.public_id, Project.name
        joins.append((Project, Project.id == UsageEvent.project_id))
    else:
        group_key, group_name = UsageEvent.feature, None

    token = UsageEvent.payload_shape == PayloadShape.LLM_TOKEN_FIELDS
    prepaid = UsageEvent.billing_mode_snapshot == BillingMode.PREPAID.value
    internal = UsageEvent.billing_mode_snapshot == BillingMode.INTERNAL_METERED_ONLY.value
    token_count = _sum(case((token, 1), else_=0))
    event_count = func.count(UsageEvent.id)
    statement = select(
        group_key.label("group_key"),
        group_name.label("group_name")
        if group_name is not None
        else cast(None, String).label("group_name"),
        event_count.label("event_count"),
        token_count.label("token_event_count"),
        _sum(UsageEvent.input_tokens).label("input_tokens"),
        _sum(UsageEvent.output_tokens).label("output_tokens"),
        _sum(UsageEvent.cache_creation_input_tokens).label("cache_creation_input_tokens"),
        _sum(UsageEvent.cache_read_input_tokens).label("cache_read_input_tokens"),
        _sum(case((prepaid, 1), else_=0)).label("prepaid_event_count"),
        _sum(case((prepaid, UsageEvent.billable_cost), else_=0)).label("prepaid_billable_cost"),
        _sum(case((prepaid, UsageEvent.estimated_provider_cost_myr), else_=0)).label(
            "prepaid_estimated_cost"
        ),
        _sum(case((internal, 1), else_=0)).label("internal_event_count"),
        _sum(case((internal, UsageEvent.estimated_provider_cost_myr), else_=0)).label(
            "internal_estimated_cost"
        ),
        _sum(case((internal, UsageEvent.reference_customer_price), else_=0)).label(
            "internal_reference_price"
        ),
    ).select_from(UsageEvent)
    for table, onclause in joins:
        statement = statement.outerjoin(table, onclause)
    statement = statement.where(*conditions).group_by(group_key, group_name)
    count_statement = select(func.count()).select_from(statement.subquery())
    statement = statement.order_by(event_count.desc(), group_key.asc()).offset(offset).limit(limit)
    rows = [UsageAnalyticsRow(*row) for row in session.execute(statement).all()]
    return rows, int(session.execute(count_statement).scalar_one())

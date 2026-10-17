"""Read-only administrator usage analytics endpoint."""

from __future__ import annotations

from decimal import Decimal

from fastapi import APIRouter, Request

from app.api.auth import require_admin, require_session_factory
from app.core.logging import current_request_id
from app.repositories import usage_analytics as analytics_repository
from app.schemas.customers import DEFAULT_PAGE_SIZE, MAX_PAGE, MAX_PAGE_SIZE
from app.schemas.envelope import ApiResponse, success
from app.schemas.usage_analytics import (
    UsageAnalyticsInternalMeteredOnly,
    UsageAnalyticsItem,
    UsageAnalyticsPrepaid,
    UsageAnalyticsView,
    decimal_text,
    parse_analytics_query,
    ratio_text,
)

router = APIRouter(prefix="/api/v1/admin", tags=["admin"])


@router.get("/usage-analytics", response_model=ApiResponse[UsageAnalyticsView])
def get_usage_analytics(
    request: Request,
    group_by: str | None = None,
    occurred_from: str | None = None,
    occurred_to: str | None = None,
    page: int = 1,
    page_size: int = DEFAULT_PAGE_SIZE,
    customer_id: str | None = None,
    project_id: str | None = None,
    provider: str | None = None,
    model: str | None = None,
    feature: str | None = None,
) -> ApiResponse[UsageAnalyticsView]:
    require_admin(request)
    group_by, start, end, feature = parse_analytics_query(
        group_by=group_by,
        occurred_from=occurred_from,
        occurred_to=occurred_to,
        feature=feature,
        page=page,
        page_size=page_size,
        max_page=MAX_PAGE,
        max_page_size=MAX_PAGE_SIZE,
    )
    with require_session_factory(request)() as session:
        rows, total = analytics_repository.list_usage_analytics(
            session,
            group_by=group_by,
            offset=(page - 1) * page_size,
            limit=page_size,
            customer_id=customer_id,
            project_id=project_id,
            provider=provider,
            model=model,
            feature=feature,
            occurred_from=start,
            occurred_to=end,
        )
    items = []
    for row in rows:
        margin = row.prepaid_billable_cost - row.prepaid_estimated_cost
        rate = None if row.prepaid_billable_cost == 0 else margin / row.prepaid_billable_cost
        items.append(
            UsageAnalyticsItem(
                group_key=row.group_key,
                group_name=row.group_name,
                event_count=row.event_count,
                token_event_count=row.token_event_count,
                input_tokens=row.input_tokens,
                output_tokens=row.output_tokens,
                cache_creation_input_tokens=row.cache_creation_input_tokens,
                cache_read_input_tokens=row.cache_read_input_tokens,
                average_input_tokens=(
                    None
                    if row.token_event_count == 0
                    else ratio_text(Decimal(row.input_tokens) / Decimal(row.token_event_count))
                ),
                average_output_tokens=(
                    None
                    if row.token_event_count == 0
                    else ratio_text(Decimal(row.output_tokens) / Decimal(row.token_event_count))
                ),
                prepaid=UsageAnalyticsPrepaid(
                    event_count=row.prepaid_event_count,
                    billable_cost=decimal_text(row.prepaid_billable_cost) or "0.00000000",
                    estimated_provider_cost_myr=decimal_text(row.prepaid_estimated_cost)
                    or "0.00000000",
                    gross_margin=decimal_text(margin) or "0.00000000",
                    gross_margin_rate=ratio_text(rate),
                ),
                internal_metered_only=UsageAnalyticsInternalMeteredOnly(
                    event_count=row.internal_event_count,
                    estimated_provider_cost_myr=decimal_text(row.internal_estimated_cost)
                    or "0.00000000",
                    reference_customer_price=decimal_text(row.internal_reference_price)
                    or "0.00000000",
                ),
            )
        )
    return success(
        UsageAnalyticsView(items=items, page=page, page_size=page_size, total=total),
        request_id=current_request_id(),
    )

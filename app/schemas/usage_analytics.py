"""Response shapes and post-auth validation for admin usage analytics."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel

from app.schemas.customers import money_text
from app.schemas.usage_events import InvalidUsageEventQuery, parse_feature, parse_period

UsageAnalyticsGroupBy = Literal["provider", "model", "customer", "project", "feature"]


def parse_analytics_query(
    *,
    group_by: str | None,
    occurred_from: str | None,
    occurred_to: str | None,
    feature: str | None,
    page: int,
    page_size: int,
    max_page: int,
    max_page_size: int,
) -> tuple[UsageAnalyticsGroupBy, dt.datetime, dt.datetime, str | None]:
    """Validate all query input after administrator authentication."""
    invalid: list[str] = []
    if group_by not in {"provider", "model", "customer", "project", "feature"}:
        invalid.append("group_by")
    if page < 1 or page > max_page:
        invalid.append("page")
    if page_size < 1 or page_size > max_page_size:
        invalid.append("page_size")
    if occurred_from is None:
        invalid.append("occurred_from")
    if occurred_to is None:
        invalid.append("occurred_to")
    if invalid:
        raise InvalidUsageEventQuery(*invalid)
    parsed_feature = parse_feature(feature)
    start, end = parse_period(occurred_from, occurred_to)
    assert start is not None and end is not None
    if end - start > dt.timedelta(days=366):
        raise InvalidUsageEventQuery("occurred_from", "occurred_to")
    return group_by, start, end, parsed_feature


def decimal_text(value: Decimal | None) -> str | None:
    return None if value is None else money_text(value)


def ratio_text(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return format(value.normalize(), "f")


class UsageAnalyticsPrepaid(BaseModel):
    event_count: int
    billable_cost: str
    estimated_provider_cost_myr: str
    gross_margin: str
    gross_margin_rate: str | None


class UsageAnalyticsInternalMeteredOnly(BaseModel):
    event_count: int
    estimated_provider_cost_myr: str
    reference_customer_price: str


class UsageAnalyticsItem(BaseModel):
    group_key: str | None
    group_name: str | None
    event_count: int
    token_event_count: int
    input_tokens: int
    output_tokens: int
    cache_creation_input_tokens: int
    cache_read_input_tokens: int
    average_input_tokens: str | None
    average_output_tokens: str | None
    prepaid: UsageAnalyticsPrepaid
    internal_metered_only: UsageAnalyticsInternalMeteredOnly


class UsageAnalyticsView(BaseModel):
    cost_basis: Literal["estimated"] = "estimated"
    items: list[UsageAnalyticsItem]
    page: int
    page_size: int
    total: int

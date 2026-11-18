"""Response shapes and post-auth validation for admin usage analytics."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel

from app.schemas.customers import money_text
from app.schemas.usage_events import InvalidUsageEventQuery, parse_feature, parse_period

UsageAnalyticsGroupBy = Literal[
    "provider", "model", "customer", "project", "feature", "day", "month"
]


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
    if group_by not in {"provider", "model", "customer", "project", "feature", "day", "month"}:
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


class UsageAnalyticsPayments(BaseModel):
    """`PAID` MYR payments whose `paid_at` falls in the period: cash period, not revenue."""

    period_basis: Literal["paid_at"] = "paid_at"
    paid_count: int
    topup_amount: str
    gateway_fee: str
    gateway_fee_unknown_count: int


class UsageAnalyticsTotalsView(BaseModel):
    """设计闸门 #709 v2 §2「接口一」：前 10 个键与分组项同形、同一个构造函数，不分组、不分页。

    `payments` 带项目 / 供应商 / 模型 / 功能筛选时为 null，此时两个毛利也为 null；
    `gross_profit` = 用量毛利 − 已知手续费，手续费未知的那几笔没有扣。
    """

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
    payments: UsageAnalyticsPayments | None
    gross_profit: str | None
    gross_profit_rate: str | None


class UsageAnalyticsView(BaseModel):
    cost_basis: Literal["estimated"] = "estimated"
    # 用量、成本与用量毛利按 `occurred_at` 归期；手续费块自己的期间口径在 `totals.payments`。
    usage_period_basis: Literal["occurred_at"] = "occurred_at"
    items: list[UsageAnalyticsItem]
    page: int
    page_size: int
    total: int
    totals: UsageAnalyticsTotalsView

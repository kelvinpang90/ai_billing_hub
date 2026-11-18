"""Response shapes and post-auth validation for admin usage analytics.

导出（AIH-TASK-223，设计闸门 #709 v2 §2「接口二」）：31 列的列常量、文本列集合与 GROUP / TOTAL
两种行的单元格函数。单元格全部取接口一 JSON 视图的值转成字符串（整数 `str()`、金额与比率沿用
接口一的字符串、null 为空）；含内部成本，只给管理端用。
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any, Final, Literal

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


# --- 导出（设计闸门 #709 v2 §2「接口二」） ------------------------------------------------

# 恰好 31 列，按这个顺序。
USAGE_ANALYTICS_EXPORT_COLUMNS: Final = (
    "row_type",
    "group_by",
    "group_key",
    "group_name",
    "event_count",
    "token_event_count",
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
    "average_input_tokens",
    "average_output_tokens",
    "prepaid_event_count",
    "prepaid_billable_cost",
    "prepaid_estimated_provider_cost_myr",
    "prepaid_gross_margin",
    "prepaid_gross_margin_rate",
    "internal_event_count",
    "internal_estimated_provider_cost_myr",
    "internal_reference_customer_price",
    "paid_payment_count",
    "topup_amount",
    "gateway_fee",
    "gateway_fee_unknown_count",
    "gross_profit",
    "gross_profit_rate",
    "cost_basis",
    "usage_period_basis",
    "fee_period_basis",
    "occurred_from",
    "occurred_to",
)
# 加公式注入前缀的文本列（功能标签、公司名、模型代码来自输入）；数值列不加：负毛利要保持是数。
USAGE_ANALYTICS_EXPORT_TEXT_COLUMNS: Final = frozenset({"group_key", "group_name"})

_USAGE_KEYS: Final = (
    "event_count",
    "token_event_count",
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
    "average_input_tokens",
    "average_output_tokens",
)
_PREPAID_KEYS: Final = (
    "event_count",
    "billable_cost",
    "estimated_provider_cost_myr",
    "gross_margin",
    "gross_margin_rate",
)
_INTERNAL_KEYS: Final = ("event_count", "estimated_provider_cost_myr", "reference_customer_price")
_PAYMENT_KEYS: Final = ("paid_count", "topup_amount", "gateway_fee", "gateway_fee_unknown_count")


def rfc3339_utc(moment: dt.datetime) -> str:
    """A naive UTC moment as RFC 3339: whole seconds without a fraction, else exactly 6 places."""
    text = moment.strftime("%Y-%m-%dT%H:%M:%S")
    if moment.microsecond:
        text += f".{moment.microsecond:06d}"
    return text + "Z"


def _cell(value: Any) -> str | None:
    return None if value is None else str(value)


def _usage_cells(view: dict[str, Any]) -> list[str | None]:
    """Columns 5–20: the ten keys a group item and `totals` share, in their JSON values."""
    return [
        *(_cell(view[key]) for key in _USAGE_KEYS),
        *(_cell(view["prepaid"][key]) for key in _PREPAID_KEYS),
        *(_cell(view["internal_metered_only"][key]) for key in _INTERNAL_KEYS),
    ]


def usage_analytics_export_group_cells(
    item: UsageAnalyticsItem,
    *,
    group_by: UsageAnalyticsGroupBy,
    occurred_from: dt.datetime,
    occurred_to: dt.datetime,
) -> list[str | None]:
    """One GROUP row in `USAGE_ANALYTICS_EXPORT_COLUMNS` order; fees and gross profit are empty."""
    view = item.model_dump(mode="json")
    return [
        "GROUP",
        group_by,
        _cell(view["group_key"]),
        _cell(view["group_name"]),
        *_usage_cells(view),
        *([None] * (len(_PAYMENT_KEYS) + 2)),
        "estimated",
        "occurred_at",
        None,
        rfc3339_utc(occurred_from),
        rfc3339_utc(occurred_to),
    ]


def usage_analytics_export_total_cells(
    totals: UsageAnalyticsTotalsView,
    *,
    group_by: UsageAnalyticsGroupBy,
    occurred_from: dt.datetime,
    occurred_to: dt.datetime,
) -> list[str | None]:
    """The TOTAL row: no group key or name; fee columns empty when `payments` is null."""
    view = totals.model_dump(mode="json")
    payments: dict[str, Any] | None = view["payments"]
    return [
        "TOTAL",
        group_by,
        None,
        None,
        *_usage_cells(view),
        *(
            [None] * len(_PAYMENT_KEYS)
            if payments is None
            else [_cell(payments[key]) for key in _PAYMENT_KEYS]
        ),
        _cell(view["gross_profit"]),
        _cell(view["gross_profit_rate"]),
        "estimated",
        "occurred_at",
        None if payments is None else _cell(payments["period_basis"]),
        rfc3339_utc(occurred_from),
        rfc3339_utc(occurred_to),
    ]

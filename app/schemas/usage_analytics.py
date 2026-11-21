"""Response shapes and post-auth validation for admin usage analytics.

导出（AIH-TASK-223，设计闸门 #709 v2 §2「接口二」）：31 列的列常量、文本列集合与 GROUP / TOTAL
两种行的单元格函数；xlsx 的整数列（AIH-TASK-237，设计闸门 #753 v1）。
单元格全部取接口一 JSON 视图的值转成字符串（整数 `str()`、金额与比率沿用
接口一的字符串、null 为空）；含内部成本，只给管理端用。

对账后成本（AIH-TASK-248，设计闸门 #756 v2 §2「接口与导出」）：两块各加按估算成本比例分摊的
供应商成本与口径，`totals` 加基于它的毛利、覆盖的对账记录与不可分摊的披露；导出在 31 列之后
追加 10 列（CSV 与 xlsx 共用同一个列常量，共 41 列）。
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


# 一块的供应商成本口径：覆盖 = 0 → estimated；覆盖 = 估算且 > 0 → reconciled；其余 mixed。
ProviderCostBasis = Literal["estimated", "reconciled", "mixed"]


class UsageAnalyticsPrepaid(BaseModel):
    event_count: int
    billable_cost: str
    estimated_provider_cost_myr: str
    gross_margin: str
    gross_margin_rate: str | None
    # 对账后成本（设计闸门 #756 v2）：覆盖的估算、分摊额、
    # 供应商成本 = 分摊额 + 未覆盖的估算。
    covered_estimated_cost_myr: str
    reconciled_provider_cost_myr: str
    provider_cost_myr: str
    provider_cost_basis: ProviderCostBasis
    gross_margin_on_provider_cost: str
    gross_margin_on_provider_cost_rate: str | None


class UsageAnalyticsInternalMeteredOnly(BaseModel):
    event_count: int
    estimated_provider_cost_myr: str
    reference_customer_price: str
    covered_estimated_cost_myr: str
    reconciled_provider_cost_myr: str
    provider_cost_myr: str
    provider_cost_basis: ProviderCostBasis


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


class UsageAnalyticsReconciliationCoverage(BaseModel):
    """One current reconciliation overlapping the period; times as naive UTC, like `FxRateView`."""

    reconciliation_id: str
    provider_code: str
    billing_month: str
    period_start: dt.datetime
    period_end: dt.datetime
    # occurred_from ≤ period_start 且 period_end ≤ occurred_to。
    fully_in_range: bool
    # (E > 0) 或 (R = 0)。
    allocatable: bool


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
    # = `prepaid.gross_margin_on_provider_cost` − `payments.gateway_fee`；
    # `payments` 为 null 时两者都为 null。
    gross_profit_on_provider_cost: str | None
    gross_profit_on_provider_cost_rate: str | None
    # 与区间重叠的当前对账记录，按供应商、期间起点升序。
    reconciliation_coverage: list[UsageAnalyticsReconciliationCoverage]
    # 不可分摊（E = 0 而 R > 0）的记录与它们的 R 之和：只披露，不并进任何成本与毛利。
    unallocated_reconciled_months: list[str]
    unallocated_reconciled_cost_myr: str | None


class UsageAnalyticsView(BaseModel):
    # 指原有的 `estimated_provider_cost_myr`，含义不变；对账后成本的口径见下面两个键。
    cost_basis: Literal["estimated"] = "estimated"
    # 用量、成本与用量毛利按 `occurred_at` 归期；手续费块自己的期间口径在 `totals.payments`。
    usage_period_basis: Literal["occurred_at"] = "occurred_at"
    provider_cost_allocation: Literal["pro_rata_estimated_cost"] = "pro_rata_estimated_cost"
    reconciliation_period_basis: Literal["utc_calendar_month"] = "utc_calendar_month"
    items: list[UsageAnalyticsItem]
    page: int
    page_size: int
    total: int
    totals: UsageAnalyticsTotalsView


# --- 导出（设计闸门 #709 v2 §2「接口二」） ------------------------------------------------

# 恰好 41 列，按这个顺序：AIH-TASK-223 的 31 列，之后是对账后成本的 10 列（设计闸门 #756 v2）。
# CSV 与 xlsx 共用这一个常量。
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
    "prepaid_provider_cost_myr",
    "prepaid_provider_cost_basis",
    "prepaid_gross_margin_on_provider_cost",
    "internal_provider_cost_myr",
    "internal_provider_cost_basis",
    "gross_profit_on_provider_cost",
    "provider_cost_allocation",
    "reconciled_months",
    "unallocated_reconciled_months",
    "unallocated_reconciled_cost_myr",
)
# 加公式注入前缀的文本列（功能标签、公司名、模型代码来自输入；两个月份列含供应商代码）；
# 数值列不加：负毛利要保持是数。
USAGE_ANALYTICS_EXPORT_TEXT_COLUMNS: Final = frozenset(
    {"group_key", "group_name", "reconciled_months", "unallocated_reconciled_months"}
)
# 第 38 列：两种行都是这个值。
_PROVIDER_COST_ALLOCATION: Final = "pro_rata_estimated_cost"
# 第 39、40 列里多条记录之间的分隔符。
_MONTH_SEPARATOR: Final = ";"
# xlsx 里写数字单元格的计数与 token 列（设计闸门 #753 v1 §2「单元格类型」）；
# 金额、比率与平均值仍是文本。
USAGE_ANALYTICS_EXPORT_INTEGER_COLUMNS: Final = frozenset(
    {
        "event_count",
        "token_event_count",
        "input_tokens",
        "output_tokens",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
        "prepaid_event_count",
        "internal_event_count",
        "paid_payment_count",
        "gateway_fee_unknown_count",
    }
)

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


def _provider_cost_cells(view: dict[str, Any]) -> list[str | None]:
    """Columns 32–36: the provider cost of both blocks, as in the JSON view."""
    prepaid, internal = view["prepaid"], view["internal_metered_only"]
    return [
        _cell(prepaid["provider_cost_myr"]),
        _cell(prepaid["provider_cost_basis"]),
        _cell(prepaid["gross_margin_on_provider_cost"]),
        _cell(internal["provider_cost_myr"]),
        _cell(internal["provider_cost_basis"]),
    ]


def _joined(months: list[str]) -> str | None:
    return _MONTH_SEPARATOR.join(months) if months else None


def usage_analytics_export_group_cells(
    item: UsageAnalyticsItem,
    *,
    group_by: UsageAnalyticsGroupBy,
    occurred_from: dt.datetime,
    occurred_to: dt.datetime,
) -> list[str | None]:
    """One GROUP row in `USAGE_ANALYTICS_EXPORT_COLUMNS` order; fees and gross profit are empty.

    第 37、39–41 列只在 TOTAL 行有值。
    """
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
        *_provider_cost_cells(view),
        None,
        _PROVIDER_COST_ALLOCATION,
        None,
        None,
        None,
    ]


def usage_analytics_export_total_cells(
    totals: UsageAnalyticsTotalsView,
    *,
    group_by: UsageAnalyticsGroupBy,
    occurred_from: dt.datetime,
    occurred_to: dt.datetime,
) -> list[str | None]:
    """The TOTAL row: no group key or name; fee columns empty when `payments` is null.

    第 39 列：可分摊的覆盖记录 `provider_code:YYYY-MM` 以 `;` 连接；第 40、41 列：`totals` 的
    两个不可分摊披露字段。没有时空。
    """
    view = totals.model_dump(mode="json")
    payments: dict[str, Any] | None = view["payments"]
    reconciled_months = [
        f"{coverage['provider_code']}:{coverage['billing_month']}"
        for coverage in view["reconciliation_coverage"]
        if coverage["allocatable"]
    ]
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
        *_provider_cost_cells(view),
        _cell(view["gross_profit_on_provider_cost"]),
        _PROVIDER_COST_ALLOCATION,
        _joined(reconciled_months),
        _joined(view["unallocated_reconciled_months"]),
        _cell(view["unallocated_reconciled_cost_myr"]),
    ]

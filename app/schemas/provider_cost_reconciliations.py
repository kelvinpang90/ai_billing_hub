"""Request and response shapes for provider cost reconciliation (gate #755 v1 §2, AIH-TASK-245).

契约见 [docs/api.md](../../docs/api.md)「管理端供应商成本对账」。只给管理端用：含内部成本（INV-7）。

⚠️ **金额只收 JSON 字符串**，按十进制正则精确解析、不经过 `float`（INV-10）：JSON 数字、指数写法、
符号一律 422；**允许 0**。`source_total` 整数部分只到 9 位（设计 §2：使换算后的积放进
DECIMAL(20,8)，服务第 5a 步兜底）；`expected_estimated_source_cost` 与 DECIMAL(20,8) 同形
（整数 12 位）。小数都最多 8 位，多了 422、不舍入。

⚠️ `provider_code`（1–64）与 `billing_month`（1–7）**只限长度、不加格式正则**：格式与取值由服务层判
（`AI_PROVIDER_NOT_FOUND`、`RECONCILIATION_MONTH_INVALID`）。`supersedes_id` 只限 1–64，**不校验
UUID 格式**：查不到（含不是 UUID 的值）是 404，不是 422。

⚠️ 请求体一律 `extra="forbid"`：预览恰好七个字段，带 `expected_*` 是 422；确认在其上加两个必填的
期望值共九个。`supersedes_id` 与 `reason` 的配对由模型校验（422 `VALIDATION_ERROR`）；服务层的
第 1a 步再挡一次，不依赖这里。

⚠️ **格式化全在这里做**，服务只给类型化的值：金额 `money_text`（恰好 8 位）、汇率 `rate_text`、
差额率与阈值 `ratio_text`；时间字段的类型是 `dt.datetime`（不带时区的 UTC），序列化同
`FxRateView`（没有 `Z`）。`period_basis` 与 `variance_threshold` 是常量，在这里补进响应。
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Callable
from decimal import Decimal
from typing import Annotated, Final

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StringConstraints,
    WithJsonSchema,
    model_validator,
)

from app.schemas.customers import money_text
from app.schemas.provider_prices import Currency, PublicId, Reason, SourceReference
from app.schemas.usage_analytics import ratio_text
from app.services import provider_cost_reconciliation as reconciliation
from app.services.fx_rates import rate_text

# 设计 §2：期间恒为 UTC 自然月。
PERIOD_BASIS: Final = "utc_calendar_month"

# ⚠️ 用 [0-9] 而不是 \d：\d 还认全角与其他文字的数字。
# 实际成本：整数最多 9 位（设计 §2「请求体」），小数最多 8 位。
_SOURCE_TOTAL_PATTERN: Final = re.compile(r"[0-9]{1,9}(\.[0-9]{1,8})?")
# 期望的估算来源成本：DECIMAL(20,8)，同 app/schemas/provider_prices.py 的 `_AMOUNT_PATTERN`。
_EXPECTED_COST_PATTERN: Final = re.compile(r"[0-9]{1,12}(\.[0-9]{1,8})?")
# BIGINT 的上界。
_MAX_COUNT: Final = 2**63 - 1


def _decimal_parser(pattern: re.Pattern[str], description: str) -> Callable[[object], Decimal]:
    def parse(value: object) -> Decimal:
        """A non-negative decimal string → `Decimal`, never through float. Zero is allowed."""
        # 422 只列字段名，不回显值（app/core/errors.py），所以这里的文案里也不放值。
        if not isinstance(value, str):
            raise ValueError("must be a JSON string")
        if pattern.fullmatch(value) is None:
            raise ValueError(f"is not a decimal with {description}")
        return Decimal(value)

    return parse


SourceTotal = Annotated[
    Decimal,
    BeforeValidator(
        _decimal_parser(_SOURCE_TOTAL_PATTERN, "at most 9 integer and 8 fraction digits")
    ),
    WithJsonSchema({"type": "string", "pattern": _SOURCE_TOTAL_PATTERN.pattern}),
]
ExpectedCost = Annotated[
    Decimal,
    BeforeValidator(
        _decimal_parser(_EXPECTED_COST_PATTERN, "at most 12 integer and 8 fraction digits")
    ),
    WithJsonSchema({"type": "string", "pattern": _EXPECTED_COST_PATTERN.pattern}),
]
# 严格类型：JSON 的 `true` 与 `"2"` 都不是整数（同 app/schemas/integration_access.py）。
ExpectedCount = Annotated[int, Field(strict=True, ge=0, le=_MAX_COUNT)]
ProviderCode = Annotated[str, StringConstraints(min_length=1, max_length=64)]
BillingMonth = Annotated[str, StringConstraints(min_length=1, max_length=7)]


class PreviewReconciliationRequest(BaseModel):
    """预览：恰好七个字段。不写库。"""

    model_config = ConfigDict(extra="forbid")

    provider_code: ProviderCode
    billing_month: BillingMonth
    source_currency: Currency
    source_total: SourceTotal
    source_document_reference: SourceReference
    # 被更正的那一行的 `public_id`；不带 = 该月的第一条记录。
    supersedes_id: PublicId | None = None
    # 更正理由：带 `supersedes_id` 时必填，不带时必须为 null。
    reason: Reason | None = None

    @model_validator(mode="after")
    def _reason_goes_with_supersedes(self) -> PreviewReconciliationRequest:
        if (self.supersedes_id is None) != (self.reason is None):
            raise ValueError("reason is required exactly when supersedes_id is given")
        return self


class ConfirmReconciliationRequest(PreviewReconciliationRequest):
    """确认：预览的七个字段加预览返回的两个估算值，共九个。估算变了是 409。"""

    expected_event_count: ExpectedCount
    expected_estimated_source_cost: ExpectedCost


# --- 响应 -------------------------------------------------------------------------


class ReconciliationView(BaseModel):
    """一条对账记录（或预览）。预览时 `reconciliation_id`、`is_current`、`created_at`、
    `created_by_email` 为 null；MYR 录入时三个汇率键为 null。"""

    reconciliation_id: str | None
    provider_code: str
    scope: str
    billing_month: str
    # 恒为 `utc_calendar_month`。
    period_basis: str
    period_start: dt.datetime
    period_end: dt.datetime
    source_document_reference: str
    source_currency: str
    source_total: str
    fx_rate_version_id: str | None
    fx_rate_applied: str | None
    fx_rate_moment: dt.datetime | None
    reconciled_cost_myr: str
    estimated_event_count: int
    estimated_source_cost: str
    estimated_cost_myr: str
    source_variance: str
    source_variance_rate: str | None
    myr_variance: str
    myr_variance_rate: str | None
    exceeds_threshold: bool
    variance_threshold: str
    unprocessed_event_count: int
    outbox_unsettled_count: int
    outbox_discarded_count: int
    coverage_method: str
    supersedes_id: str | None
    reason: str | None
    is_current: bool | None
    created_at: dt.datetime | None
    created_by_email: str | None


class ReconciliationMonthItem(BaseModel):
    billing_month: str
    period_start: dt.datetime
    period_end: dt.datetime
    # `RECONCILED` 或 `NOT_RECONCILED`。
    status: str
    current: ReconciliationView | None


class ReconciliationMonthList(BaseModel):
    """最近一个已结束的 UTC 月在前，直到首个对账月；不分页。"""

    items: list[ReconciliationMonthItem]


class LiveEstimateView(BaseModel):
    estimated_event_count: int
    estimated_source_cost: str
    estimated_cost_myr: str
    currency_mismatch_count: int
    unprocessed_event_count: int


class ReconciliationDetailView(BaseModel):
    reconciliation: ReconciliationView
    # 整条更正链，`id` 升序（最早的在前）。
    chain: list[ReconciliationView]
    live_estimate: LiveEstimateView
    estimate_changed_since_confirmation: bool


def _threshold_text() -> str:
    text = ratio_text(reconciliation.VARIANCE_THRESHOLD)
    assert text is not None
    return text


def reconciliation_view(view: reconciliation.ReconciliationView) -> ReconciliationView:
    return ReconciliationView(
        reconciliation_id=view.reconciliation_id,
        provider_code=view.provider_code,
        scope=view.scope,
        billing_month=view.billing_month,
        period_basis=PERIOD_BASIS,
        period_start=view.period_start,
        period_end=view.period_end,
        source_document_reference=view.source_document_reference,
        source_currency=view.source_currency,
        source_total=money_text(view.source_total),
        fx_rate_version_id=view.fx_rate_version_id,
        fx_rate_applied=rate_text(view.fx_rate_applied)
        if view.fx_rate_applied is not None
        else None,
        fx_rate_moment=view.fx_rate_moment,
        reconciled_cost_myr=money_text(view.reconciled_cost_myr),
        estimated_event_count=view.estimated_event_count,
        estimated_source_cost=money_text(view.estimated_source_cost),
        estimated_cost_myr=money_text(view.estimated_cost_myr),
        source_variance=money_text(view.source_variance),
        source_variance_rate=ratio_text(view.source_variance_rate),
        myr_variance=money_text(view.myr_variance),
        myr_variance_rate=ratio_text(view.myr_variance_rate),
        exceeds_threshold=view.exceeds_threshold,
        variance_threshold=_threshold_text(),
        unprocessed_event_count=view.unprocessed_event_count,
        outbox_unsettled_count=view.outbox_unsettled_count,
        outbox_discarded_count=view.outbox_discarded_count,
        coverage_method=view.coverage_method,
        supersedes_id=view.supersedes_id,
        reason=view.reason,
        is_current=view.is_current,
        created_at=view.created_at,
        created_by_email=view.created_by_email,
    )


def month_list(months: list[reconciliation.MonthView]) -> ReconciliationMonthList:
    return ReconciliationMonthList(
        items=[
            ReconciliationMonthItem(
                billing_month=month.billing_month,
                period_start=month.period_start,
                period_end=month.period_end,
                status=month.status,
                current=reconciliation_view(month.current) if month.current is not None else None,
            )
            for month in months
        ]
    )


def detail_view(detail: reconciliation.ReconciliationDetail) -> ReconciliationDetailView:
    live = detail.live_estimate
    return ReconciliationDetailView(
        reconciliation=reconciliation_view(detail.reconciliation),
        chain=[reconciliation_view(link) for link in detail.chain],
        live_estimate=LiveEstimateView(
            estimated_event_count=live.estimated_event_count,
            estimated_source_cost=money_text(live.estimated_source_cost),
            estimated_cost_myr=money_text(live.estimated_cost_myr),
            currency_mismatch_count=live.currency_mismatch_count,
            unprocessed_event_count=live.unprocessed_event_count,
        ),
        estimate_changed_since_confirmation=detail.estimate_changed_since_confirmation,
    )

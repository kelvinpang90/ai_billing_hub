"""Request and response shapes for admin rebills (AIH-TASK-256).

设计闸门 #830 v12 §2「管理端接口」。

⚠️ **请求体 `extra="forbid"`**：客户只来自 `customer_id`，操作者只来自令牌（INV-8）；不能带金额、
`tenant_id`、`created_by` 或任何内部 id。不许手填金额（设计 §1 第 2 条）。

⚠️ `selection` 是两种形状之一（设计「选事件」）：
- 显式事件：`{"usage_event_ids": [...]}`，1 到 1000 个不重复的事件 `public_id`；
- 条件：`occurred_from` / `occurred_to` 必填，`project_id`、`provider`、`model` 可选。时间是
  RFC 3339、必须带时区，小数秒至多 6 位（微秒），换成不带时区的 UTC；`occurred_from` 必须早于
  `occurred_to`（`[from, to)`）。交给服务时写成设计规范化的 `YYYY-MM-DDTHH:MM:SS.ffffffZ`。
两种都给、都不给、混用：422 `VALIDATION_ERROR`。

⚠️ 确认另加 `preview_token`（64 位小写十六进制）、`reason`（去首尾空白后 1–255）与
`idempotency_key`（小写 UUID）。校验写法照 app/schemas/wallet_adjustments.py。

⚠️ **响应模型是字段白名单**，不直接序列化 ORM 对象：内部自增 id 一律不出现，引用都是
`public_id`（规则、价格版本、对账单、事件、项目、账本行）；供应商与模型是目录的 `code`。金额是
恰好 8 位小数的字符串（`money_text`）。明细的 `corrected_estimated_provider_cost_myr` 是 Acuven
内部成本，只在 `/api/v1/admin` 下（INV-7）。超限的匹配数与不适用的事件只在错误消息里，不加字段。
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Sequence
from typing import Annotated, Final, Self

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StringConstraints,
    WithJsonSchema,
    field_validator,
    model_validator,
)

from app.models.usage_rebills import MAX_EVENT_COUNT, REASON_LENGTH
from app.models.wallet import TransactionType
from app.repositories.usage_rebills import RebillDetail, RebillLineDetail, RebillPage, RebillSummary
from app.schemas.customers import money_text

# 服务的明细（金额是 `Decimal`）；本模块的 `RebillLineView` 是响应模型。
from app.services.usage_rebill import RebillLineView as RebillLine
from app.services.usage_rebill import RebillPreview, RebillRequest, RebillResult

# 列表每页条数（设计 §2「管理端接口」：游标分页）。
DEFAULT_LIST_LIMIT: Final = 50
MAX_LIST_LIMIT: Final = 100

# RFC 3339 的 date-time，时区必填（`Z` 或 `±hh:mm`）；小数秒至多 6 位。⚠️ 用 [0-9] 而不是 \d。
_OCCURRED_PATTERN: Final = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]{1,6})?(Z|[+-][0-9]{2}:[0-9]{2})"
)
_PREVIEW_TOKEN_PATTERN: Final = re.compile(r"[0-9a-f]{64}")
# 小写 uuid，由前端进入确认步骤时生成一次，重试沿用。
_IDEMPOTENCY_KEY_PATTERN: Final = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
)


def _utc_text(moment: dt.datetime) -> str:
    """Naive UTC → `YYYY-MM-DDTHH:MM:SS.ffffffZ`, the normalized selection time (设计 v7)."""
    return moment.isoformat(timespec="microseconds") + "Z"


def _parse_occurred(value: object) -> dt.datetime:
    """RFC 3339 with a zone, up to microseconds → naive UTC. Anything else is refused."""
    # 422 只列字段名，不回显值（app/core/errors.py），所以这里的文案里也不放值。
    if not isinstance(value, str) or _OCCURRED_PATTERN.fullmatch(value) is None:
        raise ValueError("must be an RFC 3339 date-time with a time zone")
    try:
        moment = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        return moment.astimezone(dt.UTC).replace(tzinfo=None)
    except (ValueError, OverflowError):
        raise ValueError("is not a valid date-time") from None


_Occurred = Annotated[
    dt.datetime,
    BeforeValidator(_parse_occurred),
    WithJsonSchema({"type": "string", "format": "date-time"}),
]
_PublicId = Annotated[str, StringConstraints(min_length=1, max_length=64)]
# 目录的 `code`（与供应商、模型目录的列宽一致）。
_CatalogCode = Annotated[str, StringConstraints(min_length=1, max_length=64)]
# 去掉首尾空白（Python `str.strip()` 认的全部空白，含不换行空格）后长度 1–255。
_Reason = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=REASON_LENGTH)
]


class RebillSelection(BaseModel):
    """显式事件或条件，二选一（设计「选事件」）。"""

    model_config = ConfigDict(extra="forbid")

    usage_event_ids: (
        Annotated[list[_PublicId], Field(min_length=1, max_length=MAX_EVENT_COUNT)] | None
    ) = None
    occurred_from: _Occurred | None = None
    occurred_to: _Occurred | None = None
    project_id: _PublicId | None = None
    provider: _CatalogCode | None = None
    model: _CatalogCode | None = None

    @field_validator("usage_event_ids")
    @classmethod
    def _distinct(cls, value: list[str] | None) -> list[str] | None:
        if value is not None and len(set(value)) != len(value):
            raise ValueError("usage_event_ids must not repeat")
        return value

    @model_validator(mode="after")
    def _one_shape(self) -> Self:
        criteria = (
            self.occurred_from,
            self.occurred_to,
            self.project_id,
            self.provider,
            self.model,
        )
        if self.usage_event_ids is not None:
            if any(item is not None for item in criteria):
                raise ValueError("a selection is either usage_event_ids or criteria")
            return self
        if self.occurred_from is None or self.occurred_to is None:
            raise ValueError("occurred_from and occurred_to are required")
        if self.occurred_from >= self.occurred_to:
            raise ValueError("occurred_from must be earlier than occurred_to")
        return self

    def as_request(self) -> dict[str, object]:
        """What the service takes: the ids, or the five criteria keys with UTC times."""
        if self.usage_event_ids is not None:
            return {"usage_event_ids": list(self.usage_event_ids)}
        assert self.occurred_from is not None and self.occurred_to is not None
        return {
            "occurred_from": _utc_text(self.occurred_from),
            "occurred_to": _utc_text(self.occurred_to),
            "project_id": self.project_id,
            "provider": self.provider,
            "model": self.model,
        }


class PreviewRebillRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    customer_id: _PublicId
    selection: RebillSelection
    # 都是 `public_id`；空的那一项按当前有效版本（设计「口径」）。
    corrective_pricing_rule_id: _PublicId | None = None
    corrective_provider_price_version_id: _PublicId | None = None

    def rebill_request(self) -> RebillRequest:
        return RebillRequest(
            customer_id=self.customer_id,
            selection=self.selection.as_request(),
            corrective_pricing_rule_id=self.corrective_pricing_rule_id,
            corrective_provider_price_version_id=self.corrective_provider_price_version_id,
        )


class ConfirmRebillRequest(PreviewRebillRequest):
    preview_token: str
    reason: _Reason
    idempotency_key: str

    @field_validator("preview_token", mode="before")
    @classmethod
    def _lowercase_hex(cls, value: object) -> object:
        if not isinstance(value, str) or _PREVIEW_TOKEN_PATTERN.fullmatch(value) is None:
            raise ValueError("preview_token must be 64 lowercase hex digits")
        return value

    @field_validator("idempotency_key", mode="before")
    @classmethod
    def _lowercase_uuid(cls, value: object) -> object:
        # MySQL 上是二进制排序规则，大小写不同就是不同的键，所以只收小写。
        if not isinstance(value, str) or _IDEMPOTENCY_KEY_PATTERN.fullmatch(value) is None:
            raise ValueError("idempotency_key must be a lowercase uuid")
        return value


# --- 响应 ------------------------------------------------------------------------------


class RebillLineView(BaseModel):
    """预览的一行（设计「管理端接口」预览一行）。"""

    usage_event_id: str
    occurred_at: dt.datetime
    # 项目的 `public_id`；事件没有项目时为空。
    project: str | None
    # 目录的 `code`，与条件选择用的一致。
    provider: str
    model: str
    previous_charge: str
    corrected_charge: str
    difference: str
    previous_pricing_rule: str
    corrected_pricing_rule: str
    previous_provider_price_version: str
    corrected_provider_price_version: str
    # 按更正价格版本算出的估算成本：只给管理端看（INV-7）。
    corrected_estimated_provider_cost_myr: str
    # `YYYY-MM`。
    original_period: str
    original_statement_id: str | None


class RebillDetailLineView(RebillLineView):
    """Rebill 详情的一行：预览明细的字段，加明细所属 Rebill 的 `public_id`。"""

    rebill_id: str


class RebillPreviewView(BaseModel):
    # 只含差额不为 0 的事件，按事件 `id` 升序。
    lines: list[RebillLineView]
    unchanged_count: int
    # 下面四个只按差额不为 0 的事件算（设计 v9）。
    event_count: int
    previous_total: str
    corrected_total: str
    difference: str
    # 没有可入账的差额时都为空（设计 v7），页面据此禁用确认。
    ledger_type: str | None
    ledger_amount: str | None
    balance: str
    # 当前余额 − 合计差额，只作提示。
    balance_after: str
    preview_token: str


class RebillSummaryView(BaseModel):
    """列表项。`id` 是 Rebill 的 `public_id`，`customer_id` 是客户的。"""

    id: str
    customer_id: str
    event_count: int
    difference: str
    ledger_type: str
    ledger_amount: str
    reason: str
    created_by_email: str
    created_at: dt.datetime


class RebillPageView(BaseModel):
    """按 `id` 倒序。`next_cursor` 是本页最后一条的 `public_id`，没有下一页时为空；不给总数。"""

    items: list[RebillSummaryView]
    next_cursor: str | None


class RebillDetailView(RebillSummaryView):
    previous_total: str
    corrected_total: str
    # 请求里的更正版本；没有为空。
    corrective_pricing_rule_id: str | None
    corrective_provider_price_version_id: str | None
    # 那一行 `REBILL_*` 账本的 `public_id`。
    wallet_transaction_id: str
    # 明细 `id` 升序。
    lines: list[RebillDetailLineView]


class RebillConfirmView(RebillDetailView):
    # 同一幂等键重发、返回第一次的结果时为真（HTTP 200）；首次为假（HTTP 201）。
    replayed: bool


def _kind(value: TransactionType) -> str:
    return TransactionType(value).value


def _line_fields(line: RebillLine) -> dict[str, object]:
    return {
        "usage_event_id": line.usage_event_id,
        "occurred_at": line.occurred_at,
        "project": line.project,
        "provider": line.provider,
        "model": line.model,
        "previous_charge": money_text(line.previous_charge),
        "corrected_charge": money_text(line.corrected_charge),
        "difference": money_text(line.difference),
        "previous_pricing_rule": line.previous_pricing_rule,
        "corrected_pricing_rule": line.corrected_pricing_rule,
        "previous_provider_price_version": line.previous_provider_price_version,
        "corrected_provider_price_version": line.corrected_provider_price_version,
        "corrected_estimated_provider_cost_myr": money_text(
            line.corrected_estimated_provider_cost_myr
        ),
        "original_period": line.original_period,
        "original_statement_id": line.original_statement_id,
    }


def _stored_line(item: RebillLineDetail) -> RebillLine:
    """A stored line in the same shape as a preview line (references already public ids)."""
    line = item.line
    return RebillLine(
        usage_event_id=item.usage_event_id,
        occurred_at=item.occurred_at,
        project=item.project_id,
        provider=item.provider,
        model=item.model,
        previous_charge=line.previous_charge,
        corrected_charge=line.corrected_charge,
        difference=line.difference,
        previous_pricing_rule=item.previous_pricing_rule_id,
        corrected_pricing_rule=item.corrected_pricing_rule_id,
        previous_provider_price_version=item.previous_provider_price_version_id,
        corrected_provider_price_version=item.corrected_provider_price_version_id,
        corrected_estimated_provider_cost_myr=line.corrected_estimated_provider_cost_myr,
        original_period=line.original_period,
        original_statement_id=item.original_statement_id,
    )


def rebill_preview_view(preview: RebillPreview) -> RebillPreviewView:
    return RebillPreviewView(
        lines=[RebillLineView.model_validate(_line_fields(line)) for line in preview.lines],
        unchanged_count=preview.unchanged_count,
        event_count=preview.event_count,
        previous_total=money_text(preview.previous_total),
        corrected_total=money_text(preview.corrected_total),
        difference=money_text(preview.difference),
        ledger_type=None if preview.ledger_type is None else _kind(preview.ledger_type),
        ledger_amount=None if preview.ledger_amount is None else money_text(preview.ledger_amount),
        balance=money_text(preview.balance),
        balance_after=money_text(preview.balance_after),
        preview_token=preview.preview_token,
    )


def rebill_summary_view(summary: RebillSummary) -> RebillSummaryView:
    rebill = summary.rebill
    return RebillSummaryView(
        id=rebill.public_id,
        customer_id=summary.customer_id,
        event_count=rebill.event_count,
        difference=money_text(rebill.difference),
        ledger_type=_kind(summary.ledger_type),
        ledger_amount=money_text(summary.ledger_amount),
        reason=rebill.reason,
        created_by_email=summary.created_by_email,
        created_at=rebill.created_at,
    )


def rebill_page_view(page: RebillPage) -> RebillPageView:
    items = [rebill_summary_view(summary) for summary in page.items]
    return RebillPageView(
        items=items, next_cursor=items[-1].id if page.has_more and items else None
    )


def _detail_lines(rebill_id: str, lines: Sequence[RebillLine]) -> list[RebillDetailLineView]:
    return [
        RebillDetailLineView.model_validate({**_line_fields(line), "rebill_id": rebill_id})
        for line in lines
    ]


def rebill_detail_view(detail: RebillDetail) -> RebillDetailView:
    summary = detail.summary
    rebill = summary.rebill
    return RebillDetailView(
        **rebill_summary_view(summary).model_dump(),
        previous_total=money_text(rebill.previous_total),
        corrected_total=money_text(rebill.corrected_total),
        corrective_pricing_rule_id=detail.corrective_pricing_rule_id,
        corrective_provider_price_version_id=detail.corrective_provider_price_version_id,
        wallet_transaction_id=summary.wallet_transaction_id,
        lines=_detail_lines(rebill.public_id, [_stored_line(item) for item in detail.lines]),
    )


def rebill_confirm_view(result: RebillResult) -> RebillConfirmView:
    return RebillConfirmView(
        id=result.id,
        customer_id=result.customer_id,
        event_count=result.event_count,
        difference=money_text(result.difference),
        ledger_type=_kind(result.ledger_type),
        ledger_amount=money_text(result.ledger_amount),
        reason=result.reason,
        created_by_email=result.created_by_email,
        created_at=result.created_at,
        previous_total=money_text(result.previous_total),
        corrected_total=money_text(result.corrected_total),
        corrective_pricing_rule_id=result.corrective_pricing_rule_id,
        corrective_provider_price_version_id=result.corrective_provider_price_version_id,
        wallet_transaction_id=result.wallet_transaction_id,
        lines=_detail_lines(result.id, result.lines),
        replayed=result.replayed,
    )


__all__ = [
    "DEFAULT_LIST_LIMIT",
    "MAX_LIST_LIMIT",
    "ConfirmRebillRequest",
    "PreviewRebillRequest",
    "RebillConfirmView",
    "RebillDetailLineView",
    "RebillDetailView",
    "RebillLineView",
    "RebillPageView",
    "RebillPreviewView",
    "RebillSelection",
    "RebillSummaryView",
    "rebill_confirm_view",
    "rebill_detail_view",
    "rebill_page_view",
    "rebill_preview_view",
    "rebill_summary_view",
]

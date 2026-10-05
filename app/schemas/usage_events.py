"""Response shapes and query parsing for the admin usage event query (spec §61, §89; AIH-TASK-034).

⚠️ **响应模型是字段白名单，不直接序列化 ORM 对象**（与 app/schemas/customers.py 同一条）。
内部自增 id（事件、租户、项目、凭据、目录、版本、规则、账本行、冲突行）一律不出现：
对外的引用都是 `public_id`。签名、凭据 secret、加密材料不在任何一个查询里。

⚠️ 成本、计费额与毛利**只在管理端**（INV-7）。金额与数量是恰好 8 位小数的字符串
（`money_text`），汇率是去掉尾零的精确十进制字符串（与汇率版本对象一致）。毛利 =
`billable_cost − estimated_provider_cost_myr`，`gross_margin_basis` 恒为 `estimated`：V1
没有对账成本（spec §14「Reports must expose which basis was used」）。

⚠️ 时间段在鉴权**之后**解析（与审计日志的 `created_from` / `created_to` 同一个顺序与错误）：
RFC 3339、必须带时区、整秒（与发布的 `effective_from` 同一个 `Moment`），换成不带时区的 UTC；
`occurred_from` 必须早于 `occurred_to`。
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, TypeAdapter, ValidationError

from app.core.errors import AppError
from app.models.usage import UsageEventConflict, UsageEventStatus
from app.repositories.usage_events import EventDetailRow, EventListRow
from app.schemas.customers import money_text
from app.schemas.provider_prices import Moment
from app.services.fx_rates import rate_text

_MOMENT: TypeAdapter[dt.datetime | None] = TypeAdapter(Moment)


class InvalidUsageEventQuery(AppError):
    """A time value or period only the handler can judge (after `require_admin`).

    与 FastAPI 的 422 同一个码、同一种文案：只列字段名，不回显值（app/core/errors.py）。
    """

    def __init__(self, *fields: str) -> None:
        names = ", ".join(f"query.{name}" for name in sorted(fields))
        super().__init__(
            f"Invalid request fields: {names}", code="VALIDATION_ERROR", http_status=422
        )


def _moment(field: str, value: str | None) -> dt.datetime | None:
    if value is None:
        return None
    try:
        return _MOMENT.validate_python(value)
    except ValidationError:
        raise InvalidUsageEventQuery(field) from None


def parse_period(
    occurred_from: str | None, occurred_to: str | None
) -> tuple[dt.datetime | None, dt.datetime | None]:
    """Both bounds as naive UTC; the period `[occurred_from, occurred_to)` must not be empty."""
    start = _moment("occurred_from", occurred_from)
    end = _moment("occurred_to", occurred_to)
    if start is not None and end is not None and start >= end:
        raise InvalidUsageEventQuery("occurred_from", "occurred_to")
    return start, end


class UsageEventSummary(BaseModel):
    """列表项（spec §61 的请求表）。`id` 是事件的 `public_id`。"""

    id: str
    event_id: str
    customer_id: str
    customer_company_name: str
    project_id: str
    request_id: str
    conversation_id: str | None
    # 上报的原始字符串。
    provider: str
    model: str
    usage_type: str
    # `LLM_TOKEN_FIELDS` 形态四个 token 数非空、`quantity` 为空；`QUANTITY` 形态相反。
    input_tokens: int | None
    output_tokens: int | None
    cache_creation_input_tokens: int | None
    cache_read_input_tokens: int | None
    quantity: str | None
    unit: str
    status: str
    error_code: str | None
    occurred_at: dt.datetime
    received_at: dt.datetime
    processed_at: dt.datetime | None
    billable_cost: str | None
    estimated_provider_cost_myr: str | None


class LedgerEntryView(BaseModel):
    """那一行 `AI_USAGE` 账本：`public_id` 与金额（负数，= −计费额）。"""

    id: str
    amount: str


class UsageEventConflictView(BaseModel):
    """一次被拒的 `event_id` 重用：请求方的 `api_key`、哪里不同、何时收到。"""

    api_key: str
    mismatch: str
    received_at: dt.datetime


class UsageEventDetail(UsageEventSummary):
    schema_version: str
    payload_shape: str
    quantity_kind: str
    payload_fingerprint: str
    error_message: str | None
    created_at: dt.datetime
    # §79 的目录引用（T-H 解析出来的 `public_id`；模型未知时为空）。
    provider_ref_id: str | None
    model_ref_id: str | None
    # §14 / §79 的计费快照：所用版本与规则的 `public_id`、原币成本、汇率、MYR 估算成本。
    provider_price_version_id: str | None
    pricing_rule_id: str | None
    # MYR 原币时汇率版本与汇率都为空。
    fx_rate_version_id: str | None
    fx_rate_applied: str | None
    provider_source_currency: str | None
    provider_source_cost: str | None
    gross_margin: str | None
    gross_margin_basis: Literal["estimated"] | None
    # 0 元事件与未计费事件为空。
    wallet_transaction: LedgerEntryView | None
    # 认领（§82、§83、§110）。
    attempt_count: int
    next_attempt_at: dt.datetime | None
    claim_token: str | None
    claimed_at: dt.datetime | None
    lease_expires_at: dt.datetime | None
    conflicts: list[UsageEventConflictView]


def _decimal_text(value: Decimal | None) -> str | None:
    return None if value is None else money_text(value)


def _summary_fields(row: EventListRow) -> dict[str, object]:
    event = row.event
    return {
        "id": event.public_id,
        "event_id": event.event_id,
        "customer_id": row.customer_public_id,
        "customer_company_name": row.company_name,
        "project_id": row.project_public_id,
        "request_id": event.request_id,
        "conversation_id": event.conversation_id,
        "provider": event.provider_code_raw,
        "model": event.model_code_raw,
        "usage_type": row.usage_type,
        "input_tokens": event.input_tokens,
        "output_tokens": event.output_tokens,
        "cache_creation_input_tokens": event.cache_creation_input_tokens,
        "cache_read_input_tokens": event.cache_read_input_tokens,
        "quantity": _decimal_text(event.quantity),
        "unit": event.unit,
        "status": UsageEventStatus(event.status).value,
        "error_code": event.error_code,
        "occurred_at": event.occurred_at,
        "received_at": event.received_at,
        "processed_at": event.processed_at,
        "billable_cost": _decimal_text(event.billable_cost),
        "estimated_provider_cost_myr": _decimal_text(event.estimated_provider_cost_myr),
    }


def usage_event_summary(row: EventListRow) -> UsageEventSummary:
    return UsageEventSummary.model_validate(_summary_fields(row))


def _conflict_view(conflict: UsageEventConflict) -> UsageEventConflictView:
    return UsageEventConflictView(
        api_key=conflict.api_key,
        mismatch=conflict.mismatch.value,
        received_at=conflict.received_at,
    )


def usage_event_detail(row: EventDetailRow) -> UsageEventDetail:
    event = row.listed.event
    margin = None
    if event.billable_cost is not None and event.estimated_provider_cost_myr is not None:
        margin = money_text(event.billable_cost - event.estimated_provider_cost_myr)
    ledger = None
    if row.ledger_public_id is not None and row.ledger_amount is not None:
        ledger = LedgerEntryView(id=row.ledger_public_id, amount=money_text(row.ledger_amount))
    return UsageEventDetail.model_validate(
        {
            **_summary_fields(row.listed),
            "schema_version": event.schema_version,
            "payload_shape": event.payload_shape.value,
            "quantity_kind": event.quantity_kind.value,
            "payload_fingerprint": event.payload_fingerprint,
            "error_message": event.error_message,
            "created_at": event.created_at,
            "provider_ref_id": row.provider_public_id,
            "model_ref_id": row.model_public_id,
            "provider_price_version_id": row.price_version_public_id,
            "pricing_rule_id": row.pricing_rule_public_id,
            "fx_rate_version_id": row.fx_rate_version_public_id,
            "fx_rate_applied": (
                None if event.fx_rate_applied is None else rate_text(event.fx_rate_applied)
            ),
            "provider_source_currency": event.provider_source_currency,
            "provider_source_cost": _decimal_text(event.provider_source_cost),
            "gross_margin": margin,
            "gross_margin_basis": None if margin is None else "estimated",
            "wallet_transaction": ledger,
            "attempt_count": event.attempt_count,
            "next_attempt_at": event.next_attempt_at,
            "claim_token": event.claim_token,
            "claimed_at": event.claimed_at,
            "lease_expires_at": event.lease_expires_at,
            "conflicts": [_conflict_view(conflict) for conflict in row.conflicts],
        }
    )


__all__ = [
    "InvalidUsageEventQuery",
    "LedgerEntryView",
    "UsageEventConflictView",
    "UsageEventDetail",
    "UsageEventSummary",
    "parse_period",
    "usage_event_detail",
    "usage_event_summary",
]

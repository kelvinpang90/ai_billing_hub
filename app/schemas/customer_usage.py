"""Customer projects and billed usage: shapes, dates and the cursor (design gate #398 v4 §2).

⚠️ **这是客户侧的字段白名单**（INV-7）：项目每项恰好 `id`、`name`；用量 `data` 恰好 `items`、
`page_size`、`next_cursor`，每项恰好 18 个键。刻意不继承、不复用 app/schemas/usage_events.py 的
管理端模型：管理端以后加字段，客户端不会跟着出现。成本、原币成本、汇率、参考价、计费模式快照、
状态、错误、版本与规则引用、账本行、凭据、冲突记录、任何内部 id 一律不出这里。项目的
`description`、`integration_status`、`status_webhook_url` 同样不出这里。

⚠️ 金额与数量是恰好 8 位小数的字符串（`money_text`），原样来自 `DECIMAL(20,8)` 列，不经 float、
不舍入（INV-10）。`charged_amount` 就是 `billable_cost`（含税；内部计量租户恒为 0）。

⚠️ 日期按**马来西亚日历日**理解（固定 UTC+8，复用 `KUALA_LUMPUR_OFFSET`，不引入 tzdata）：
`[from_date 00:00 − 8h, (to_date + 1 天) 00:00 − 8h)` 的 UTC 区间。每个日期**先**判是否在
2020-01-01 到 2099-12-31 之间，再做任何日期运算：极值得到 422，而不是换算溢出的 500。

⚠️ 游标是本页最后一行 `occurred_at` 与 `public_id` 的 base64url（不带填充），客户端只能原样传回。
解码失败或内容不合规都是 422 `VALIDATION_ERROR`；所有 422 文案只列字段名，不回显值。
"""

from __future__ import annotations

import base64
import binascii
import datetime as dt
import re
from collections.abc import Sequence
from typing import Final

from pydantic import BaseModel

from app.core.errors import AppError
from app.core.fx_source import KUALA_LUMPUR_OFFSET
from app.models.tenancy import Project
from app.repositories.usage_events import CustomerUsageRow
from app.schemas.customers import money_text

# 设计 §2：两个日期都必须落在这个区间里（含两端）。
MIN_USAGE_DATE: Final = dt.date(2020, 1, 1)
MAX_USAGE_DATE: Final = dt.date(2099, 12, 31)
# 区间最多 92 天（含两端）：`to_date − from_date ≤ 91`。
MAX_USAGE_PERIOD_DAYS: Final = 92
# 查询参数的形状，FastAPI 在鉴权之前判；是否是真实存在的日期在鉴权之后判。
USAGE_DATE_PATTERN: Final = r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$"

_CURSOR_TIME_FORMAT: Final = "%Y-%m-%dT%H:%M:%S.%f"
# 编码后最长约 84 个字符；放宽一些，超出即不合规。
_MAX_CURSOR_LENGTH: Final = 256
_CURSOR_TEXT: Final = re.compile(r"[A-Za-z0-9_-]+")
_CURSOR_BODY: Final = re.compile(
    r"([0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6})"
    r"\|([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"
)


class InvalidCustomerUsageQuery(AppError):
    """A date, period or cursor only the handler can judge (after `require_customer`).

    与 FastAPI 的 422 同一个码、同一种文案：只列字段名，不回显值（app/core/errors.py）。
    """

    def __init__(self, *fields: str) -> None:
        names = ", ".join(f"query.{name}" for name in sorted(fields))
        super().__init__(
            f"Invalid request fields: {names}", code="VALIDATION_ERROR", http_status=422
        )


class CustomerUsageQueryTooBroad(AppError):
    """The query hit its execution-time budget; narrow the dates or change the filters."""

    def __init__(self) -> None:
        super().__init__(
            "The query took too long. Narrow the date range or change the filters.",
            code="QUERY_TOO_BROAD",
            http_status=422,
        )


class CustomerProjectView(BaseModel):
    """One project of the caller. `id` is the project's `public_id`."""

    id: str
    name: str


class CustomerProjectList(BaseModel):
    """Every project of the caller, oldest first. Not paged."""

    items: list[CustomerProjectView]


class CustomerUsageEventView(BaseModel):
    """One billed usage event as the customer sees it. `id` is the event's `public_id`."""

    id: str
    event_id: str
    request_id: str
    # AI 模块代为上报的事件为 null。
    conversation_id: str | None
    feature: str | None
    project_id: str
    project_name: str
    # 上报的原始字符串。
    provider: str
    model: str
    usage_type: str
    # `LLM_TOKEN_FIELDS` 形态四个 token 非空、`quantity` 为 null；`QUANTITY` 形态相反。
    input_tokens: int | None
    output_tokens: int | None
    cache_creation_input_tokens: int | None
    cache_read_input_tokens: int | None
    quantity: str | None
    unit: str
    charged_amount: str
    # 不带时区的 UTC，保留微秒。
    occurred_at: dt.datetime


class CustomerUsageEventPage(BaseModel):
    """Newest first. `next_cursor` is `None` when there is nothing older; no `total`."""

    items: list[CustomerUsageEventView]
    page_size: int
    next_cursor: str | None


# --- 日期 -------------------------------------------------------------------------------


def malaysia_day_start_utc(day: dt.date) -> dt.datetime:
    """00:00 of a Malaysian calendar day as naive UTC (the previous day at 16:00)."""
    return dt.datetime.combine(day, dt.time()) - KUALA_LUMPUR_OFFSET


def _usage_date(field: str, value: str | None) -> dt.date | None:
    if value is None:
        return None
    try:
        day = dt.date.fromisoformat(value)
    except ValueError:
        raise InvalidCustomerUsageQuery(field) from None
    # 先判范围，再做任何日期运算（设计 §2）。
    if not MIN_USAGE_DATE <= day <= MAX_USAGE_DATE:
        raise InvalidCustomerUsageQuery(field)
    return day


def usage_period(
    from_date: str | None, to_date: str | None, *, has_event_id: bool
) -> tuple[dt.datetime | None, dt.datetime | None]:
    """`[from_utc, to_utc)` for the Malaysian days `from_date`..`to_date`, both included.

    没有 `event_id` 时两个都必填；`to_date` 不得早于 `from_date`；最多 92 天。带 `event_id` 时
    日期可省略，给了的照常生效、照常校验。
    """
    start = _usage_date("from_date", from_date)
    end = _usage_date("to_date", to_date)
    if not has_event_id:
        missing = [name for name, day in (("from_date", start), ("to_date", end)) if day is None]
        if missing:
            raise InvalidCustomerUsageQuery(*missing)
    if start is not None and end is not None:
        if end < start or (end - start).days > MAX_USAGE_PERIOD_DAYS - 1:
            raise InvalidCustomerUsageQuery("from_date", "to_date")
    from_utc = None if start is None else malaysia_day_start_utc(start)
    to_utc = None if end is None else malaysia_day_start_utc(end + dt.timedelta(days=1))
    return from_utc, to_utc


# --- 游标 -------------------------------------------------------------------------------


def encode_cursor(occurred_at: dt.datetime, public_id: str) -> str:
    """The opaque cursor after this row. Clients pass it back as is and never parse it."""
    body = f"{occurred_at.strftime(_CURSOR_TIME_FORMAT)}|{public_id}"
    return base64.urlsafe_b64encode(body.encode("ascii")).decode("ascii").rstrip("=")


def decode_cursor(cursor: str) -> tuple[dt.datetime, str]:
    """`(occurred_at, public_id)` of the last row of the previous page, or 422 `cursor`."""
    if len(cursor) > _MAX_CURSOR_LENGTH or _CURSOR_TEXT.fullmatch(cursor) is None:
        raise InvalidCustomerUsageQuery("cursor")
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        body = base64.urlsafe_b64decode(padded).decode("ascii")
    except (binascii.Error, ValueError):
        raise InvalidCustomerUsageQuery("cursor") from None
    found = _CURSOR_BODY.fullmatch(body)
    if found is None:
        raise InvalidCustomerUsageQuery("cursor")
    try:
        occurred_at = dt.datetime.strptime(found[1], _CURSOR_TIME_FORMAT)
    except ValueError:
        raise InvalidCustomerUsageQuery("cursor") from None
    return occurred_at, found[2]


# --- 投影 -------------------------------------------------------------------------------


def customer_project_list(projects: Sequence[Project]) -> CustomerProjectList:
    return CustomerProjectList(
        items=[CustomerProjectView(id=project.public_id, name=project.name) for project in projects]
    )


def customer_usage_event_view(row: CustomerUsageRow) -> CustomerUsageEventView:
    return CustomerUsageEventView(
        id=row.public_id,
        event_id=row.event_id,
        request_id=row.request_id,
        conversation_id=row.conversation_id,
        feature=row.feature,
        project_id=row.project_public_id,
        project_name=row.project_name,
        provider=row.provider,
        model=row.model,
        usage_type=row.usage_type,
        input_tokens=row.input_tokens,
        output_tokens=row.output_tokens,
        cache_creation_input_tokens=row.cache_creation_input_tokens,
        cache_read_input_tokens=row.cache_read_input_tokens,
        quantity=None if row.quantity is None else money_text(row.quantity),
        unit=row.unit,
        charged_amount=money_text(row.billable_cost),
        occurred_at=row.occurred_at,
    )


def customer_usage_event_page(
    rows: Sequence[CustomerUsageRow], *, page_size: int
) -> CustomerUsageEventPage:
    """`rows` holds up to `page_size + 1` rows, newest first; the extra one only says "more"."""
    shown = rows[:page_size]
    more = len(rows) > page_size
    return CustomerUsageEventPage(
        items=[customer_usage_event_view(row) for row in shown],
        page_size=page_size,
        next_cursor=encode_cursor(shown[-1].occurred_at, shown[-1].public_id) if more else None,
    )


__all__ = [
    "MAX_USAGE_DATE",
    "MAX_USAGE_PERIOD_DAYS",
    "MIN_USAGE_DATE",
    "USAGE_DATE_PATTERN",
    "CustomerProjectList",
    "CustomerProjectView",
    "CustomerUsageEventPage",
    "CustomerUsageEventView",
    "CustomerUsageQueryTooBroad",
    "InvalidCustomerUsageQuery",
    "customer_project_list",
    "customer_usage_event_page",
    "customer_usage_event_view",
    "decode_cursor",
    "encode_cursor",
    "malaysia_day_start_utc",
    "usage_period",
]

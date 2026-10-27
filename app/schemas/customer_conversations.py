"""Customer conversations: the 9-key view, the page and the cursor (design gate #460 v2 §2).

⚠️ **这是客户侧的字段白名单**（INV-7）：`data` 恰好 `items`、`page_size`、`next_cursor`，每项恰好
9 个键。刻意不继承、不复用管理端模型，也不复用 app/schemas/customer_usage.py 的请求级视图：
别处以后加字段，这里不会跟着出现。成本、原币成本、汇率、参考价、毛利、计费模式、状态、
版本与规则引用、供应商 / 模型拆分、缓存 token 与 `quantity` 合计、任何内部 id、总行数
一律不出这里。

⚠️ `charged_amount` 是区间内该会话 `billable_cost` 之和，恰好 8 位小数的字符串（`money_text`），
不经 float、不舍入（INV-10）。两个时间与 `GET /usage-events` 的 `occurred_at` 同一格式：不带时区的
UTC，保留微秒。

⚠️ 游标是本页最后一行的 `last_occurred_at`、项目 `public_id`、`conversation_id` 以 `|` 连接后的
base64url（不带填充），客户端只能原样传回。与用量事件的游标是两种格式、两个函数：拿事件游标来翻
会话得到 422，不会错位。解码失败或内容不合规都是 422 `VALIDATION_ERROR`（字段名 `cursor`），
文案不回显值。

⚠️ 会话摘要导出（AIH-TASK-122，§2「接口二」）的每个单元格都取自上面 9 键视图的
JSON 值，不直接读查询结果：白名单只有这一份。两个时间列改名加 `_utc`。
"""

from __future__ import annotations

import base64
import binascii
import datetime as dt
import re
from collections.abc import Sequence
from typing import Final

from pydantic import BaseModel

from app.repositories.usage_events import CustomerConversationRow
from app.schemas.customer_usage import InvalidCustomerUsageQuery
from app.schemas.customers import money_text
from app.schemas.usage_ingest import REQUEST_ID_PATTERN

_CURSOR_TIME_FORMAT: Final = "%Y-%m-%dT%H:%M:%S.%f"
# 编码前最长约 192 字节、编码后最长 256 个字符；解码前超过这个长度即不合规（设计 §2「游标」）。
_MAX_CURSOR_LENGTH: Final = 512
_CURSOR_TEXT: Final = re.compile(r"[A-Za-z0-9_-]+")
_CURSOR_TIME: Final = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}")
# 项目 `public_id` 是小写 uuid。
_CURSOR_PROJECT: Final = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
# 与摄取同一个格式；字符集不含 `|`，三段可以无歧义地切开。
_CURSOR_CONVERSATION: Final = re.compile(REQUEST_ID_PATTERN)

# 会话摘要导出的表头（设计 §2「接口二」），按这个顺序。
CONVERSATION_EXPORT_COLUMNS: Final = (
    "conversation_id",
    "project_id",
    "project_name",
    "first_occurred_at_utc",
    "last_occurred_at_utc",
    "request_count",
    "input_tokens",
    "output_tokens",
    "charged_amount",
)
# 加公式注入前缀的文本列；时间、整数与金额列不加。
CONVERSATION_EXPORT_TEXT_COLUMNS: Final = frozenset(
    {"conversation_id", "project_id", "project_name"}
)
# 改名的列：导出列名 → 白名单视图的键。
_CONVERSATION_EXPORT_RENAMED: Final = {
    "first_occurred_at_utc": "first_occurred_at",
    "last_occurred_at_utc": "last_occurred_at",
}


class CustomerConversationView(BaseModel):
    """One conversation (a project and a `conversation_id`) in the period.

    `project_id` 是项目的 `public_id`。合计只算区间内已计费的请求。
    """

    conversation_id: str
    project_id: str
    project_name: str
    # 不带时区的 UTC，保留微秒；区间内的第一条 / 最后一条已计费请求。
    first_occurred_at: dt.datetime
    last_occurred_at: dt.datetime
    request_count: int
    # `QUANTITY` 形态的请求不计 token。
    input_tokens: int
    output_tokens: int
    charged_amount: str


class CustomerConversationPage(BaseModel):
    """Latest activity first. `next_cursor` is `None` when there is nothing older; no `total`."""

    items: list[CustomerConversationView]
    page_size: int
    next_cursor: str | None


# --- 游标 -------------------------------------------------------------------------------


def encode_conversation_cursor(
    last_occurred_at: dt.datetime, project_public_id: str, conversation_id: str
) -> str:
    """The opaque cursor after this row. Clients pass it back as is and never parse it."""
    moment = last_occurred_at.strftime(_CURSOR_TIME_FORMAT)
    body = f"{moment}|{project_public_id}|{conversation_id}"
    return base64.urlsafe_b64encode(body.encode("ascii")).decode("ascii").rstrip("=")


def decode_conversation_cursor(cursor: str) -> tuple[dt.datetime, str, str]:
    """`(last_occurred_at, project public_id, conversation_id)` of the previous page's last row.

    任何不合规（含用量事件的游标）都是 422 `cursor`。
    """
    if len(cursor) > _MAX_CURSOR_LENGTH or _CURSOR_TEXT.fullmatch(cursor) is None:
        raise InvalidCustomerUsageQuery("cursor")
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        body = base64.urlsafe_b64decode(padded).decode("ascii")
    except (binascii.Error, ValueError):
        raise InvalidCustomerUsageQuery("cursor") from None
    parts = body.split("|")
    if len(parts) != 3:
        raise InvalidCustomerUsageQuery("cursor")
    moment, project_public_id, conversation_id = parts
    if (
        _CURSOR_TIME.fullmatch(moment) is None
        or _CURSOR_PROJECT.fullmatch(project_public_id) is None
        or _CURSOR_CONVERSATION.fullmatch(conversation_id) is None
    ):
        raise InvalidCustomerUsageQuery("cursor")
    try:
        last_occurred_at = dt.datetime.strptime(moment, _CURSOR_TIME_FORMAT)
    except ValueError:
        raise InvalidCustomerUsageQuery("cursor") from None
    return last_occurred_at, project_public_id, conversation_id


# --- 投影 -------------------------------------------------------------------------------


def customer_conversation_view(row: CustomerConversationRow) -> CustomerConversationView:
    return CustomerConversationView(
        conversation_id=row.conversation_id,
        project_id=row.project_public_id,
        project_name=row.project_name,
        first_occurred_at=row.first_occurred_at,
        last_occurred_at=row.last_occurred_at,
        request_count=row.request_count,
        input_tokens=row.input_tokens,
        output_tokens=row.output_tokens,
        charged_amount=money_text(row.charged_amount),
    )


def customer_conversation_page(
    rows: Sequence[CustomerConversationRow], *, page_size: int
) -> CustomerConversationPage:
    """`rows` holds up to `page_size + 1` rows, latest first; the extra one only says "more"."""
    shown = rows[:page_size]
    more = len(rows) > page_size
    last = shown[-1] if more else None
    return CustomerConversationPage(
        items=[customer_conversation_view(row) for row in shown],
        page_size=page_size,
        next_cursor=None
        if last is None
        else encode_conversation_cursor(
            last.last_occurred_at, last.project_public_id, last.conversation_id
        ),
    )


def customer_conversation_export_cells(row: CustomerConversationRow) -> list[str | None]:
    """One CSV row in `CONVERSATION_EXPORT_COLUMNS` order: the JSON values of the 9-key view.

    时间与 JSON 接口的 `first_occurred_at` / `last_occurred_at` 逐字相同（不带时区的 UTC、保留
    微秒）；整数变成十进制字符串，金额是原样的 8 位小数字符串。
    """
    view = customer_conversation_view(row).model_dump(mode="json")
    cells: list[str | None] = []
    for name in CONVERSATION_EXPORT_COLUMNS:
        value = view[_CONVERSATION_EXPORT_RENAMED.get(name, name)]
        cells.append(None if value is None else str(value))
    return cells


__all__ = [
    "CONVERSATION_EXPORT_COLUMNS",
    "CONVERSATION_EXPORT_TEXT_COLUMNS",
    "CustomerConversationPage",
    "CustomerConversationView",
    "customer_conversation_export_cells",
    "customer_conversation_page",
    "customer_conversation_view",
    "decode_conversation_cursor",
    "encode_conversation_cursor",
]

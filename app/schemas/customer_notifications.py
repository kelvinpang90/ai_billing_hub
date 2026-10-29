"""Customer notifications: the 6-key view, the page and the errors.

设计闸门 #471 v7 §2「客户接口」。

⚠️ **这是客户侧的字段白名单**（INV-7）：每条恰好 6 个键，页恰好 `items`、`next_cursor`、
`unread_count`，全部已读的响应恰好 `marked`。刻意不继承、不复用别的模型：别处以后加字段，这里
不会跟着出现。内部 `id`、`tenant_id`、`source_outbox_id`、行的 `created_at` 与 outbox payload 里的
其他字段（`status_version`、`reason`、`effective_status`、流水 `public_id`）一律不出这里。

⚠️ 金额是恰好 8 位小数的字符串（`money_text`），原样来自 `DECIMAL(20,8)` 列，不经 float
（INV-10）。时间与客户钱包流水的 `created_at` 同一个类型、同一种格式：不带时区的 UTC。

⚠️ 游标与路径参数都是通知的 `public_id`：只收小写规范 UUID 形状（8-4-4-4-12）。形状在鉴权之后判，
游标不合规是 422 `cursor`，路径参数不合规是 404 `NOTIFICATION_NOT_FOUND`（与查不到同一个结果）。
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Sequence
from typing import Final

from pydantic import BaseModel

from app.core.errors import AppError
from app.models.notifications import Notification, NotificationType
from app.schemas.customers import money_text

_NOTIFICATION_ID: Final = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
)


class NotificationNotFound(AppError):
    """No such notification for this customer (absent, another customer's, or not a uuid)."""

    def __init__(self) -> None:
        super().__init__(
            "Notification not found.",
            code="NOTIFICATION_NOT_FOUND",
            http_status=404,
        )


class CustomerNotificationView(BaseModel):
    """One notification as the customer sees it. `notification_id` is the row's `public_id`."""

    notification_id: str
    # `LOW_BALANCE` / `SUSPENDED` / `REACTIVATED`；文案在前端。
    notification_type: str
    # 事件发生时刻（= 账本行的 `created_at`），不是通知写入时刻。
    occurred_at: dt.datetime
    # 事件当时的余额，可以为负。
    balance: str
    # 只有 `LOW_BALANCE` 有：事件当时的阈值。
    threshold: str | None
    # 首次标已读的时刻；`None` = 未读。
    read_at: dt.datetime | None


class CustomerNotificationPage(BaseModel):
    """Newest booking first. `next_cursor` is `None` when there is nothing older; no `total`."""

    items: list[CustomerNotificationView]
    next_cursor: str | None
    # 本客户全部未读的条数，不受分页影响。
    unread_count: int


class CustomerNotificationsMarked(BaseModel):
    """`marked`: how many unread notifications this call marked read (0 when none)."""

    marked: int


def is_notification_id(value: str) -> bool:
    """A lowercase canonical uuid (36 characters, 8-4-4-4-12)."""
    return _NOTIFICATION_ID.fullmatch(value) is not None


def customer_notification_view(row: Notification) -> CustomerNotificationView:
    threshold = row.threshold
    return CustomerNotificationView(
        notification_id=row.public_id,
        notification_type=NotificationType(row.notification_type).value,
        occurred_at=row.occurred_at,
        balance=money_text(row.balance),
        threshold=None if threshold is None else money_text(threshold),
        read_at=row.read_at,
    )


def customer_notification_page(
    rows: Sequence[Notification], *, page_size: int, unread_count: int
) -> CustomerNotificationPage:
    """`rows` holds up to `page_size + 1` rows, newest first; the extra one only says "more"."""
    shown = rows[:page_size]
    more = len(rows) > page_size
    return CustomerNotificationPage(
        items=[customer_notification_view(row) for row in shown],
        next_cursor=shown[-1].public_id if more else None,
        unread_count=unread_count,
    )


__all__ = [
    "CustomerNotificationPage",
    "CustomerNotificationView",
    "CustomerNotificationsMarked",
    "NotificationNotFound",
    "customer_notification_page",
    "customer_notification_view",
    "is_notification_id",
]

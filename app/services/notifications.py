"""Portal notification rules (design gate #471 v7 §2「通知规则」, AIH-TASK-126).

一个纯函数：输入一条 outbox 行的 `event_type`、`aggregate_type`、
`payload_json`、`created_at`，输出三者之一：一条通知草稿（`NotificationDraft`）、
不出通知（`NoNotification`）、payload 无效（`InvalidPayload`）。

⚠️ **不读数据库、不写数据库、不记日志。**
租户查找在投递分支（app/tasks/outbox.py）的结算事务里做。
判定只看 payload（事件发生时的快照），不读租户当前的计费模式或余额：
通知说的是当时发生了什么。

⚠️ **先校验、再分类**（设计 §2 规则表「任一 → 无效」那一行对所有事件先适用）：
内部计量租户的 payload 不合法时同样是无效，不是「不出通知」。
"""

from __future__ import annotations

import datetime as dt
import json
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Final

from app.models.notifications import NotificationType
from app.models.tenancy import BillingMode, BillingStatus
from app.repositories.wallet import (
    AGGREGATE_TENANT,
    EVENT_BILLING_STATUS_CHANGED,
    EVENT_LOW_BALANCE,
)

# 写入方 `_money_text` 用定点格式，余额可以为负；至多 12 位整数、8 位小数 = `DECIMAL(20,8)`。
# 整串匹配（`fullmatch`）：指数写法、`NaN` / `Infinity`、首尾空白都不算金额。
_MONEY: Final = re.compile(r"-?[0-9]{1,12}(\.[0-9]{1,8})?")

# 停机 / 复机：只有这两个计费状态是已知值。
_STATUS_TYPES: Final = {
    BillingStatus.SUSPENDED.value: NotificationType.SUSPENDED,
    BillingStatus.ACTIVE.value: NotificationType.REACTIVATED,
}
# `billing_mode` 键的已知值恰好这两个（设计 §2）：以后新增的计费模式不会被默默当成预付。
_KNOWN_MODES: Final = (BillingMode.PREPAID.value, BillingMode.INTERNAL_METERED_ONLY.value)


@dataclass(frozen=True)
class NotificationDraft:
    """What the delivery branch inserts; `tenant_id` is looked up there, not here."""

    notification_type: NotificationType
    balance: Decimal
    # 只有 LOW_BALANCE 非空。
    threshold: Decimal | None
    # outbox 行的 `created_at`，不取处理时刻（设计 §2「时间」）。
    occurred_at: dt.datetime


@dataclass(frozen=True)
class NoNotification:
    """A valid event that produces no notification (an internal metered tenant's transition)."""


@dataclass(frozen=True)
class InvalidPayload:
    """The row cannot be turned into a notification; delivery treats it as a failure.

    `reason` 是固定文本，不带 payload 里的任何值。
    """

    reason: str


type NotificationOutcome = NotificationDraft | NoNotification | InvalidPayload


def _money(raw: object, *, non_negative: bool = False) -> Decimal | None:
    """A fixed-point money string as a Decimal, or None if it is not one. Never via float."""
    if not isinstance(raw, str) or _MONEY.fullmatch(raw) is None:
        return None
    try:
        value = Decimal(raw)
    except InvalidOperation:
        return None
    if non_negative and value < 0:
        return None
    return value


def _payload(raw: str | None) -> dict[str, object] | None:
    if raw is None:
        return None
    try:
        payload = json.loads(raw)
    except (ValueError, RecursionError):
        return None
    return payload if isinstance(payload, dict) else None


def _low_balance(payload: dict[str, object], occurred_at: dt.datetime) -> NotificationOutcome:
    # 不检查计费模式：写入方只对预付客户写这类事件（设计闸门 #433 v2 §2）。
    balance = _money(payload.get("balance"))
    if balance is None:
        return InvalidPayload("balance is not a valid amount")
    threshold = _money(payload.get("threshold"), non_negative=True)
    if threshold is None:
        return InvalidPayload("threshold is not a valid non-negative amount")
    return NotificationDraft(NotificationType.LOW_BALANCE, balance, threshold, occurred_at)


def _billing_status_changed(
    payload: dict[str, object], occurred_at: dt.datetime
) -> NotificationOutcome:
    status = payload.get("billing_status")
    if not isinstance(status, str) or status not in _STATUS_TYPES:
        return InvalidPayload("billing_status is not a known value")
    balance = _money(payload.get("balance"))
    if balance is None:
        return InvalidPayload("balance is not a valid amount")
    # ⚠️ 没有这个键按预付：键与 `billing_mode` 列同一个提交加入（迁移 0019），
    # 没有它的行只能写于内部计量模式出现之前（设计 §2）。
    # 键存在但为 null 或非字符串 → 无效。
    if "billing_mode" not in payload:
        mode = BillingMode.PREPAID.value
    else:
        mode = payload["billing_mode"]
        if not isinstance(mode, str) or mode not in _KNOWN_MODES:
            return InvalidPayload("billing_mode is not a known value")
    # 校验全部通过之后才分类。
    if mode == BillingMode.INTERNAL_METERED_ONLY.value:
        return NoNotification()
    return NotificationDraft(_STATUS_TYPES[status], balance, None, occurred_at)


def notification_for(
    *,
    event_type: str,
    aggregate_type: str,
    payload_json: str | None,
    created_at: dt.datetime,
) -> NotificationOutcome:
    """The notification an outbox row stands for (设计 §2 规则表)."""
    if aggregate_type != AGGREGATE_TENANT:
        return InvalidPayload("aggregate_type is not tenant")
    payload = _payload(payload_json)
    if payload is None:
        return InvalidPayload("payload is not a JSON object")
    if event_type == EVENT_LOW_BALANCE:
        return _low_balance(payload, created_at)
    if event_type == EVENT_BILLING_STATUS_CHANGED:
        return _billing_status_changed(payload, created_at)
    return InvalidPayload("event_type has no notification rule")


__all__ = [
    "InvalidPayload",
    "NoNotification",
    "NotificationDraft",
    "NotificationOutcome",
    "notification_for",
]

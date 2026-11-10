"""Recording verified payment webhook events (design gate #580 v3 §2「支付 Webhook」, AIH-TASK-175).

回调路由（AIH-TASK-177）验签、解析之后调用 `record_event`：一个事务插入一行
`payment_gateway_events` 后提交。这里不查 Stripe、不改支付：事件由后台任务
（app/tasks/payment_events.py）领取处理。

- 四种处理的事件类型与争议、退款两种（设计闸门 #585 v3 §2「争议与退款」，AIH-TASK-181）落为
  `RECEIVED`，其余类型落为 `IGNORED`；
- 事件的 `livemode` 与配置的密钥模式不一致：落为 `IGNORED`、`last_error_code = LIVEMODE_MISMATCH`；
- `IGNORED` 落库时写 `processed_at`；`next_attempt_at` 一律等于 `received_at`；
- `payload_fingerprint` 是原始请求体的 SHA-256（小写十六进制），`metadata_json` 就是
  `parsed.fields`（§6 白名单，**不存原始请求体**）。

⚠️ 幂等的第一层（ADR-0004）：同一 `(gateway, gateway_event_id)` 只落一行。撞唯一约束时比对指纹：
相同是重复投递（`DUPLICATE`）；不同则把 `fingerprint_conflict_at` 更新为本次的 `now`（每次冲突都
更新，24 小时窗口的告警对再次冲突也生效）、记 ERROR、返回 `CONFLICT`。两种情形都不覆盖原行的其他列。

⚠️ 日志只带事件 id、事件类型与原因码（设计 §6），不带请求体、签名头与密钥。
"""

from __future__ import annotations

import datetime as dt
import enum
import hashlib
import logging
from typing import Final

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.core.database import session_scope
from app.models.payments import (
    PaymentEventProcessingStatus,
    PaymentEventVerificationStatus,
    PaymentGateway,
    PaymentGatewayEvent,
)
from app.payments.base import ParsedEvent
from app.payments.stripe_gateway import CHARGE_EVENT_TYPES

logger = logging.getLogger(__name__)

# 订阅并处理的四种事件类型（设计 §2「支付 Webhook」）；Stripe Dashboard 的回调端点只订阅它们。
HANDLED_EVENT_TYPES: Final = frozenset(
    {
        "checkout.session.completed",
        "checkout.session.async_payment_succeeded",
        "checkout.session.async_payment_failed",
        "checkout.session.expired",
    }
)
# 争议与退款（设计闸门 #585 v3 §2「争议与退款」，AIH-TASK-181）：同样落为 `RECEIVED`，
# 事件任务只关联支付、不查 Stripe、不改支付。
ALERT_ONLY_EVENT_TYPES: Final = CHARGE_EVENT_TYPES

LIVEMODE_MISMATCH: Final = "LIVEMODE_MISMATCH"

# MySQL 的 ER_DUP_ENTRY；SQLite 的唯一约束错误只能从消息认出（同 app/services/payments.py）。
_ER_DUP_ENTRY = 1062
_SQLITE_UNIQUE = "UNIQUE constraint failed"


class RecordOutcome(enum.StrEnum):
    """What `record_event` did. 三种都让回调返回 200。"""

    RECORDED = "RECORDED"
    # 同一事件 id、同一请求体：重复投递。
    DUPLICATE = "DUPLICATE"
    # 同一事件 id、请求体不同：记下 `fingerprint_conflict_at`，原行不变。
    CONFLICT = "CONFLICT"


def payload_fingerprint(raw_body: bytes) -> str:
    """SHA-256 of the raw request body, lower-case hex."""
    return hashlib.sha256(raw_body).hexdigest()


def record_event(
    session_factory: sessionmaker[Session],
    parsed: ParsedEvent,
    raw_body: bytes,
    *,
    now: dt.datetime,
    livemode: bool,
) -> RecordOutcome:
    """Insert one verified event, or tell a redelivery from a conflicting body.

    `livemode` 是配置的密钥模式（`sk_live_` 为真）。`now` 是不带时区的 UTC、精确到秒。
    数据库错误（唯一约束以外的）原样抛出，路由据此返回 503，Stripe 会重试。
    """
    fingerprint = payload_fingerprint(raw_body)
    status, error_code = _initial_status(parsed, livemode=livemode)
    try:
        with session_scope(session_factory) as session:
            session.add(
                PaymentGatewayEvent(
                    gateway=PaymentGateway.STRIPE,
                    gateway_event_id=parsed.gateway_event_id,
                    event_type=parsed.event_type,
                    payment_id=None,
                    payload_fingerprint=fingerprint,
                    verification_status=PaymentEventVerificationStatus.VERIFIED,
                    processing_status=status,
                    attempts=0,
                    next_attempt_at=now,
                    lease_until=None,
                    last_error_code=error_code,
                    fingerprint_conflict_at=None,
                    received_at=now,
                    processed_at=now if status is PaymentEventProcessingStatus.IGNORED else None,
                    metadata_json=dict(parsed.fields),
                )
            )
    except IntegrityError as error:
        if not _is_unique_violation(error):
            raise
        return _already_recorded(session_factory, parsed, fingerprint, now=now)
    logger.info(
        "Payment event recorded",
        extra={
            "gateway_event_id": parsed.gateway_event_id,
            "event_type": parsed.event_type,
            "processing_status": status.value,
            "error_code": error_code,
        },
    )
    return RecordOutcome.RECORDED


def _initial_status(
    parsed: ParsedEvent, *, livemode: bool
) -> tuple[PaymentEventProcessingStatus, str | None]:
    if parsed.livemode is not livemode:
        return PaymentEventProcessingStatus.IGNORED, LIVEMODE_MISMATCH
    if parsed.event_type in HANDLED_EVENT_TYPES or parsed.event_type in ALERT_ONLY_EVENT_TYPES:
        return PaymentEventProcessingStatus.RECEIVED, None
    return PaymentEventProcessingStatus.IGNORED, None


def _already_recorded(
    session_factory: sessionmaker[Session],
    parsed: ParsedEvent,
    fingerprint: str,
    *,
    now: dt.datetime,
) -> RecordOutcome:
    """The event id is taken: same body → `DUPLICATE`; another body → `CONFLICT`."""
    same_event = (
        PaymentGatewayEvent.gateway == PaymentGateway.STRIPE,
        PaymentGatewayEvent.gateway_event_id == parsed.gateway_event_id,
    )
    with session_scope(session_factory) as session:
        stored = session.execute(
            select(PaymentGatewayEvent.payload_fingerprint).where(*same_event)
        ).scalar_one()
        if stored == fingerprint:
            return RecordOutcome.DUPLICATE
        # 只改这一列：原行的内容、状态与指纹都不覆盖。
        session.execute(
            update(PaymentGatewayEvent).where(*same_event).values(fingerprint_conflict_at=now)
        )
    logger.error(
        "Payment event redelivered with a different body; the stored row is kept",
        extra={"gateway_event_id": parsed.gateway_event_id, "event_type": parsed.event_type},
    )
    return RecordOutcome.CONFLICT


def _is_unique_violation(error: IntegrityError) -> bool:
    original = error.orig
    args = getattr(original, "args", ())
    if args and args[0] == _ER_DUP_ENTRY:
        return True
    return _SQLITE_UNIQUE in str(original)


__all__ = [
    "ALERT_ONLY_EVENT_TYPES",
    "HANDLED_EVENT_TYPES",
    "LIVEMODE_MISMATCH",
    "RecordOutcome",
    "payload_fingerprint",
    "record_event",
]

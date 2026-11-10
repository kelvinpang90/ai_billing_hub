"""Processing stored payment events (design gate #580 v3 §2「事件处理任务」, AIH-TASK-175).

**数据库是事实来源，Beat 只是触发**（INV-14）：回调只落库、不入队；Beat 每
`payment_event_poll_seconds`（缺省 15 秒）调 `process_due`，扫库领取到期的事件。Redis 丢了、
worker 在处理中途挂了，事件仍在库里，租约过期后被重领。

一轮：

1. 开关为假或 `get_payment_gateway` 返回 `None`（凭据读不到，告警见 `wallet_negative_balance`）：
   直接返回，不领取事件；
2. 取 MySQL 命名锁 `GET_LOCK(CONCAT('payment_events:', DATABASE()), 0)`（照 app/ai_api/delivery.py；
   SQLite 下跳过），取不到直接返回：同一时刻只跑一份，免得每轮预算 45 秒、间隔 15 秒造成多份并跑；
3. 每轮最多 50 个、预算 45 秒（`time.monotonic`，与注入的 `now` 无关）；**一次领一个**：一个短事务
   按 `next_attempt_at` 升序 `FOR UPDATE SKIP LOCKED` 取一个到期的 `RECEIVED` / `RETRY` 或租约过期的
   `PROCESSING`，置 `PROCESSING`、租约 60 秒、`attempts + 1`，提交；处理完再领下一个；预算用完
   就停，不留下已领未处理的事件。

处理一个事件（`decide`）：

1. 按 `metadata_json.client_reference_id` 找支付；没有它 → `UNMATCHED`（`NO_CLIENT_REFERENCE`），
   找不到 → `UNMATCHED`（`PAYMENT_NOT_FOUND`）；
2. 写 `payment_id`（随结果一起落）。**认回会话**：支付的 `gateway_payment_id` 为空而事件带会话 id
   （`metadata_json.object_id`）时，条件更新（`gateway_payment_id IS NULL`）写入；撞唯一约束 →
   `REJECTED`（`GATEWAY_REFERENCE_TAKEN`）；
3. `confirm_payment`；
4. 成功 → `PROCESSED`（清空 `last_error_code`）；仍是「无可查询」：支付已过 `expires_at` →
   `REJECTED`（`NO_GATEWAY_REFERENCE`），否则 `RETRY`（同一原因码）；`GatewayUnavailable`、
   `state = UNKNOWN`、`ConfirmConflict` → `RETRY`；`GatewayRequestRejected` → `REJECTED`，
   `last_error_code` 记 Stripe 的错误码。

`RETRY` 的 `next_attempt_at = now + min(3600, 2^min(attempts, 12)) × uniform(0.5, 1.0)` 秒（向上取整
到秒，DATETIME 不存小数秒），**不放弃**。进入终止状态（`PROCESSED`、`UNMATCHED`、`REJECTED`）时写
`processed_at`。

争议与退款（`charge.dispute.created`、`charge.refunded`，设计闸门 #585 v3 §2「争议与退款」，
AIH-TASK-181）走另一个分支：不查 Stripe、不调 `confirm_payment`，按 `metadata_json.payment_intent`
找 `payments.gateway_payment_intent_id`：找到 → 写 `payment_id`、`PROCESSED`；没有它或找不到 →
`UNMATCHED`（`PAYMENT_NOT_FOUND`）。支付与钱包都不变。

⚠️ 落事件结果是**另一个事务**，用条件更新（`processing_status = PROCESSING` 且 `lease_until` 等于
领取时写的值）：影响 0 行说明租约已过期、被别人重领，本次结果丢弃，只记 WARNING。确认已提交而落结果
失败时，事件租约到期后被重领、再确认一次：`confirm_payment` 锁住支付行后看到 `PAID` 就什么也不做，
账本 `(PAYMENT, public_id)` 唯一约束兜底（INV-3、INV-11）。

⚠️ 日志只带事件 id、事件类型、状态与原因码（设计 §6）。
"""

from __future__ import annotations

import datetime as dt
import logging
import math
import random
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Final

from celery import shared_task
from sqlalchemy import ColumnElement, and_, or_, select, text, update
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings
from app.core.database import create_database_engine, create_session_factory, session_scope
from app.models.payments import Payment, PaymentEventProcessingStatus, PaymentGatewayEvent
from app.payments import (
    GatewayRequestRejected,
    GatewayState,
    GatewayUnavailable,
    PaymentGateway,
    get_payment_gateway,
)
from app.services.auth import utc_now
from app.services.payment_events import ALERT_ONLY_EVENT_TYPES
from app.services.payments import ConfirmConflict, PaymentNotFound, confirm_payment

logger = logging.getLogger(__name__)

# 设计 §2「事件处理任务」。
BATCH_LIMIT: Final = 50
RUN_BUDGET_SECONDS: Final = 45.0
LEASE: Final = dt.timedelta(seconds=60)
RETRY_CAP_SECONDS: Final = 3600
RETRY_EXPONENT_CAP: Final = 12

# `last_error_code` 的取值（`GatewayRequestRejected` 时是 Stripe 的错误码）。
NO_CLIENT_REFERENCE: Final = "NO_CLIENT_REFERENCE"
PAYMENT_NOT_FOUND: Final = "PAYMENT_NOT_FOUND"
GATEWAY_REFERENCE_TAKEN: Final = "GATEWAY_REFERENCE_TAKEN"
NO_GATEWAY_REFERENCE: Final = "NO_GATEWAY_REFERENCE"
GATEWAY_UNAVAILABLE: Final = "GATEWAY_UNAVAILABLE"
GATEWAY_STATE_UNKNOWN: Final = "GATEWAY_STATE_UNKNOWN"
CONFIRM_CONFLICT: Final = "CONFIRM_CONFLICT"

_LOCK_SQL: Final = "SELECT GET_LOCK(CONCAT('payment_events:', DATABASE()), 0)"
_UNLOCK_SQL: Final = "SELECT RELEASE_LOCK(CONCAT('payment_events:', DATABASE()))"

# 进入这些状态时写 `processed_at`。
_TERMINAL: Final = frozenset(
    {
        PaymentEventProcessingStatus.PROCESSED,
        PaymentEventProcessingStatus.UNMATCHED,
        PaymentEventProcessingStatus.REJECTED,
    }
)

# MySQL 的 ER_DUP_ENTRY；SQLite 的唯一约束错误只能从消息认出（同 app/services/payments.py）。
_ER_DUP_ENTRY = 1062
_SQLITE_UNIQUE = "UNIQUE constraint failed"


@dataclass(frozen=True)
class Claim:
    """One event this run holds: what processing needs, and the lease that fences its result."""

    id: int
    gateway_event_id: str
    event_type: str
    attempts: int
    lease_until: dt.datetime
    metadata: Mapping[str, Any]


@dataclass(frozen=True)
class Outcome:
    """Where one processed event goes. `payment_id` 是匹配到的 `payments.id`（没匹配到为空）。"""

    status: PaymentEventProcessingStatus
    error_code: str | None
    payment_id: int | None = None


@lru_cache(maxsize=1)
def _session_factory() -> sessionmaker[Session]:
    """One engine per worker process (同 app/tasks/outbox.py)."""
    return create_session_factory(create_database_engine(get_settings()))


def _now() -> dt.datetime:
    # MySQL 的 DATETIME 不存小数秒。
    return utc_now().replace(microsecond=0)


@shared_task(name="app.tasks.payment_events.process_due")
def process_due(*, now: dt.datetime | None = None, rng: random.Random | None = None) -> int:
    """Process due payment events. Returns how many were claimed and processed.

    `now` 为 `None` 时每个事件处理前各读一次当前 UTC 时间；传入时整轮都用它。`rng` 只用于退避的
    `uniform(0.5, 1.0)`，测试注入。
    """
    settings = get_settings()
    if not settings.payments_enabled:
        return 0
    gateway = get_payment_gateway(settings)
    if gateway is None:
        # 凭据读不到：`read_stripe_credentials` 已记 ERROR，告警见 `wallet_negative_balance`。
        return 0
    factory = _session_factory()
    started = time.monotonic()
    handled = 0
    with _run_lock(factory) as locked:
        if not locked:
            logger.info("Payment event processing is already running elsewhere")
            return 0
        while handled < BATCH_LIMIT and time.monotonic() - started < RUN_BUDGET_SECONDS:
            moment = now if now is not None else _now()
            claim = claim_one(factory, now=moment)
            if claim is None:
                break
            outcome = decide(factory, gateway, claim, now=moment)
            settle(factory, claim, outcome, now=moment, rng=rng)
            handled += 1
    if handled:
        logger.info("Processed payment events", extra={"count": handled})
    return handled


@contextmanager
def _run_lock(factory: sessionmaker[Session]) -> Iterator[bool]:
    """MySQL named lock: only one run at a time (照 app/ai_api/delivery.py). SQLite: skipped."""
    engine: Engine = factory.kw["bind"]
    if engine.dialect.name == "sqlite":
        yield True
        return
    connection = engine.connect()
    acquired = False
    try:
        acquired = connection.scalar(text(_LOCK_SQL)) == 1
        yield acquired
    finally:
        if acquired:
            connection.execute(text(_UNLOCK_SQL))
        connection.close()


def _due(now: dt.datetime) -> ColumnElement[bool]:
    event = PaymentGatewayEvent
    return or_(
        and_(
            event.processing_status.in_(
                (PaymentEventProcessingStatus.RECEIVED, PaymentEventProcessingStatus.RETRY)
            ),
            event.next_attempt_at <= now,
        ),
        and_(
            event.processing_status == PaymentEventProcessingStatus.PROCESSING,
            event.lease_until < now,
        ),
    )


def claim_one(factory: sessionmaker[Session], *, now: dt.datetime) -> Claim | None:
    """Claim the due event with the earliest `next_attempt_at`, in one short transaction."""
    event = PaymentGatewayEvent
    with session_scope(factory) as session:
        row = session.execute(
            select(event)
            .where(_due(now))
            .order_by(event.next_attempt_at, event.id)
            .limit(1)
            .with_for_update(skip_locked=True)
        ).scalar_one_or_none()
        if row is None:
            return None
        # 整秒：落结果时按它等值比较，而 MySQL 的 DATETIME 不存小数秒。
        lease_until = (now + LEASE).replace(microsecond=0)
        row.processing_status = PaymentEventProcessingStatus.PROCESSING
        row.lease_until = lease_until
        row.attempts = row.attempts + 1
        claim = Claim(
            id=int(row.id),
            gateway_event_id=row.gateway_event_id,
            event_type=row.event_type,
            attempts=row.attempts,
            lease_until=lease_until,
            metadata=dict(row.metadata_json or {}),
        )
    return claim


def decide(
    factory: sessionmaker[Session],
    gateway: PaymentGateway,
    claim: Claim,
    *,
    now: dt.datetime,
) -> Outcome:
    """Process one claimed event (设计 §2 四步) and say where it goes.

    Never touches the event row."""
    if claim.event_type in ALERT_ONLY_EVENT_TYPES:
        return _link_charge_event(factory, claim)
    reference = claim.metadata.get("client_reference_id")
    if not isinstance(reference, str) or not reference:
        return Outcome(PaymentEventProcessingStatus.UNMATCHED, NO_CLIENT_REFERENCE)
    with factory() as session:
        found = session.execute(
            select(Payment.id, Payment.gateway_payment_id, Payment.expires_at).where(
                Payment.public_id == reference
            )
        ).one_or_none()
    if found is None:
        return Outcome(PaymentEventProcessingStatus.UNMATCHED, PAYMENT_NOT_FOUND)
    payment_id, gateway_payment_id, expires_at = found

    session_id = claim.metadata.get("object_id")
    if gateway_payment_id is None and isinstance(session_id, str) and session_id:
        if not _recover_session(factory, payment_id, session_id, now=now):
            return Outcome(
                PaymentEventProcessingStatus.REJECTED, GATEWAY_REFERENCE_TAKEN, payment_id
            )

    try:
        result = confirm_payment(factory, gateway, reference, now=now)
    except GatewayUnavailable:
        return Outcome(PaymentEventProcessingStatus.RETRY, GATEWAY_UNAVAILABLE, payment_id)
    except GatewayRequestRejected as error:
        return Outcome(PaymentEventProcessingStatus.REJECTED, error.code, payment_id)
    except ConfirmConflict:
        return Outcome(PaymentEventProcessingStatus.RETRY, CONFIRM_CONFLICT, payment_id)
    except PaymentNotFound:
        # 支付行不删除，正常走不到这里。
        return Outcome(PaymentEventProcessingStatus.UNMATCHED, PAYMENT_NOT_FOUND)

    if result.nothing_to_query:
        # 事件不带会话 id，支付也没有：过期之前还可能由同一会话的另一个事件认回。
        if now >= expires_at:
            return Outcome(PaymentEventProcessingStatus.REJECTED, NO_GATEWAY_REFERENCE, payment_id)
        return Outcome(PaymentEventProcessingStatus.RETRY, NO_GATEWAY_REFERENCE, payment_id)
    if result.state is GatewayState.UNKNOWN:
        return Outcome(PaymentEventProcessingStatus.RETRY, GATEWAY_STATE_UNKNOWN, payment_id)
    return Outcome(PaymentEventProcessingStatus.PROCESSED, None, payment_id)


def _link_charge_event(factory: sessionmaker[Session], claim: Claim) -> Outcome:
    """A dispute or a refund: link it to its payment by `payment_intent`, nothing else.

    设计闸门 #585 v3 §2「争议与退款」：不查 Stripe、不调 `confirm_payment`，支付与钱包都不变
    （INV-4、INV-11）；扣回由人工调账做，告警见 `wallet_negative_balance`。
    """
    intent = claim.metadata.get("payment_intent")
    if not isinstance(intent, str) or not intent:
        return Outcome(PaymentEventProcessingStatus.UNMATCHED, PAYMENT_NOT_FOUND)
    with factory() as session:
        payment_id = session.execute(
            select(Payment.id).where(Payment.gateway_payment_intent_id == intent)
        ).scalar_one_or_none()
    if payment_id is None:
        return Outcome(PaymentEventProcessingStatus.UNMATCHED, PAYMENT_NOT_FOUND)
    return Outcome(PaymentEventProcessingStatus.PROCESSED, None, int(payment_id))


def _recover_session(
    factory: sessionmaker[Session], payment_id: int, session_id: str, *, now: dt.datetime
) -> bool:
    """Write the event's session id into a payment that has none. `False`: another payment has it.

    条件更新 `gateway_payment_id IS NULL`：影响 0 行说明并发者已写入，交给之后的权威查询核对。
    """
    try:
        with session_scope(factory) as session:
            session.execute(
                update(Payment)
                .where(Payment.id == payment_id, Payment.gateway_payment_id.is_(None))
                .values(gateway_payment_id=session_id, updated_at=now)
            )
    except IntegrityError as error:
        if not _is_unique_violation(error):
            raise
        return False
    return True


def retry_delay_seconds(attempts: int, rng: random.Random | None = None) -> int:
    """`min(3600, 2^min(attempts, 12)) × uniform(0.5, 1.0)`, rounded up to a whole second."""
    base = min(RETRY_CAP_SECONDS, 2 ** min(attempts, RETRY_EXPONENT_CAP))
    uniform = rng.uniform if rng is not None else random.uniform
    return math.ceil(base * uniform(0.5, 1.0))


def settle(
    factory: sessionmaker[Session],
    claim: Claim,
    outcome: Outcome,
    *,
    now: dt.datetime,
    rng: random.Random | None = None,
) -> bool:
    """Write the outcome, fenced by the lease this run holds. `False`: someone re-claimed it."""
    values: dict[str, Any] = {
        "processing_status": outcome.status,
        "last_error_code": outcome.error_code,
        "lease_until": None,
    }
    if outcome.payment_id is not None:
        values["payment_id"] = outcome.payment_id
    if outcome.status in _TERMINAL:
        values["processed_at"] = now
    elif outcome.status is PaymentEventProcessingStatus.RETRY:
        delay = retry_delay_seconds(claim.attempts, rng)
        values["next_attempt_at"] = now + dt.timedelta(seconds=delay)
    event = PaymentGatewayEvent
    with session_scope(factory) as session:
        changed = session.execute(
            update(event)
            .where(
                event.id == claim.id,
                event.processing_status == PaymentEventProcessingStatus.PROCESSING,
                event.lease_until == claim.lease_until,
            )
            .values(**values)
            .execution_options(synchronize_session=False)
        ).rowcount
    details = {
        "gateway_event_id": claim.gateway_event_id,
        "event_type": claim.event_type,
        "processing_status": outcome.status.value,
        "error_code": outcome.error_code,
        "attempts": claim.attempts,
    }
    if changed != 1:
        logger.warning("Payment event lease was lost; the result is discarded", extra=details)
        return False
    if outcome.status is PaymentEventProcessingStatus.PROCESSED:
        logger.info("Payment event processed", extra=details)
    else:
        logger.warning("Payment event not processed", extra=details)
    return True


def _is_unique_violation(error: IntegrityError) -> bool:
    original = error.orig
    args = getattr(original, "args", ())
    if args and args[0] == _ER_DUP_ENTRY:
        return True
    return _SQLITE_UNIQUE in str(original)


__all__ = [
    "BATCH_LIMIT",
    "CONFIRM_CONFLICT",
    "GATEWAY_REFERENCE_TAKEN",
    "GATEWAY_STATE_UNKNOWN",
    "GATEWAY_UNAVAILABLE",
    "LEASE",
    "NO_CLIENT_REFERENCE",
    "NO_GATEWAY_REFERENCE",
    "PAYMENT_NOT_FOUND",
    "RUN_BUDGET_SECONDS",
    "Claim",
    "Outcome",
    "claim_one",
    "decide",
    "process_due",
    "retry_delay_seconds",
    "settle",
]

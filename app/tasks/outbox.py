"""Domain Outbox delivery (spec §74.6, §25, §98.1; ADR-0009 第 3 节).

**数据库是事实来源，队列只是投递触发。**这条不是措辞，是整个模块的形状：

- `deliver(id)` 只做一件事：把某一行投出去。它**不假设自己是被谁调起来的** ——
  正常路径是业务提交后 `delay()` 一下，异常路径是 `recover()` 从库里扫出来。
  两条路径走同一个函数，所以「Redis 整个丢了」与「worker 崩在半路」都不需要
  第二套逻辑（Invariant 14）。
- 领取靠一条**带完整前置条件的 UPDATE 的受影响行数**，不靠先读后写。

⚠️ **投递是 at-least-once，不是 exactly-once。**发信这一步在事务外（SMTP 往返
可能很慢，不能占着数据库事务），所以「信已经发出去、worker 在标记 SENT 之前
崩掉」会导致同一封信发第二遍。对密码重置这是无害的：两封信里是**同一张令牌**，
用掉一次另一封自然失效。ADR-0009 说的「重试不得产生第二条逻辑通知」指的是不
产生第二行 outbox —— 那一条这里是满足的。
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from functools import lru_cache
from urllib.parse import quote

from celery import shared_task
from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings, get_settings
from app.core.database import create_database_engine, create_session_factory
from app.core.mailer import OutgoingEmail, build_transport
from app.models.auth import DomainOutbox, OutboxStatus
from app.services.auth import utc_now
from app.services.password_reset import EVENT_PASSWORD_RESET

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def _session_factory() -> sessionmaker[Session]:
    """One engine per worker process. ⚠️ 每个任务建一个 engine 会把连接池的意义
    抹掉 —— 每次投递都要重新握手，而投递是这个 worker 最频繁的动作。"""
    return create_session_factory(create_database_engine(get_settings()))


def _backoff_seconds(settings: Settings, attempt: int) -> int:
    """Wait before attempt `attempt` (1-based): base * 2^(attempt-1)."""
    return settings.outbox_retry_base_seconds * (2 ** max(attempt - 1, 0))


def _render_password_reset(settings: Settings, payload: dict[str, object]) -> OutgoingEmail | None:
    """Build the reset email, or None when we cannot (which counts as a failure)."""
    base = settings.frontend_base_url.strip().rstrip("/")
    if not base:
        # ⚠️ 没有基址就拼不出可点的链接。**不发一封带半截 URL 的信** ——
        # 那比不发更糟：用户以为流程走通了，实际点不动，还会以为是自己的问题。
        logger.error("BILLING_FRONTEND_BASE_URL is not configured; cannot build a reset link")
        return None

    to = str(payload.get("to") or "")
    token = str(payload.get("token") or "")
    if not to or not token:
        logger.error("A password reset outbox row is missing its recipient or token")
        return None

    # quote 不是装饰：令牌是 URL-safe base64，但**别的事件将来未必是**，而一个
    # 没转义的查询参数会把链接在某些邮件客户端里截断 —— 症状是「有些人点不开」。
    link = f"{base}/reset-password?token={quote(token, safe='')}"
    body = (
        "We received a request to reset the password for your Acuven Billing account.\n\n"
        f"Open this link to choose a new password:\n{link}\n\n"
        "The link can be used once, and expires shortly.\n\n"
        "If you did not ask for this, you can ignore this email — "
        "your password has not changed."
    )
    return OutgoingEmail(to=to, subject="Reset your Acuven Billing password", body=body)


# event_type → 渲染函数。
#
# ⚠️ 认不出来的事件类型走**普通失败**（重试，最终进死信），不是立刻死信。
# 因为最可能的原因是**滚动更新的时间差**：API 已经在写新事件，worker 镜像还是
# 旧的。这种行在 worker 更新后就能正常投出去 —— 立刻判死信等于把它们永久丢掉，
# 而它们本来只需要等几分钟。重试上限兜住了「真的没人认识它」那种情况。
_RENDERERS = {EVENT_PASSWORD_RESET: _render_password_reset}


@shared_task(name="app.tasks.outbox.deliver")
def deliver(outbox_id: int) -> str:
    """Deliver one outbox row. Returns what happened, for the logs."""
    settings = get_settings()
    factory = _session_factory()
    now = utc_now()

    with factory() as session:
        row = session.get(DomainOutbox, outbox_id)
        if row is None:
            # 行被删了（或者根本没提交）。不是错误：恢复任务会处理真正该处理的行。
            return "missing"
        if row.status is not OutboxStatus.PENDING:
            return "already-settled"

        attempt = row.attempt_count + 1
        event_type = row.event_type
        raw_payload = row.payload_json

        # ⚠️ 领取 = 一条带完整前置条件的 UPDATE。`attempt_count` 同时充当乐观锁：
        # 两个 worker 同时被触发时，只有一个的受影响行数是 1。
        #
        # ⚠️ 领取的同时就把 `next_retry_at` 推后。这样「worker 崩在发信中途」
        # 的后果只是这一行晚几分钟重投 —— 不需要任何「卡在 PROCESSING 超过 N
        # 分钟就放回去」的清扫逻辑，也就不会有那种逻辑写错时的永久卡死。
        claimed = session.execute(
            update(DomainOutbox)
            .where(
                DomainOutbox.id == outbox_id,
                DomainOutbox.status == OutboxStatus.PENDING,
                DomainOutbox.next_retry_at <= now,
                DomainOutbox.attempt_count == row.attempt_count,
            )
            .values(
                attempt_count=attempt,
                next_retry_at=now + dt.timedelta(seconds=_backoff_seconds(settings, attempt)),
            )
        )
        if int(claimed.rowcount) != 1:
            # 还没到重试时刻，或者被另一个 worker 抢走了。
            return "not-claimed"
        session.commit()

    sent = _attempt_send(settings, event_type=event_type, raw_payload=raw_payload)

    if sent:
        # ⚠️ 连同 payload 一起清空：密码重置的 payload 里是**令牌明文**，
        # 而这一行是长期保留的。它的用途在投递完成的这一瞬就结束了。
        _settle(factory, outbox_id, status=OutboxStatus.SENT)
        return "sent"

    if attempt >= settings.outbox_max_attempts:
        _settle(factory, outbox_id, status=OutboxStatus.FAILED)
        # ⚠️ **error 级别**：没有别人会再碰这一行了，只能靠人。
        logger.error(
            "Giving up on an outbox row after repeated failures",
            extra={"outbox_id": outbox_id, "event_type": event_type, "attempts": attempt},
        )
        return "dead-lettered"

    logger.warning(
        "Outbox delivery failed; it will be retried",
        extra={"outbox_id": outbox_id, "event_type": event_type, "attempt": attempt},
    )
    return "retry-later"


def _settle(factory: sessionmaker[Session], outbox_id: int, *, status: OutboxStatus) -> None:
    """Close out one row. `status == PENDING` 是前置条件，所以重复结算是无害的。

    ⚠️ `payload_json` 一律置空 —— 无论成功还是死信。密码重置的 payload 里是
    令牌明文，而死信那一行同样是长期保留的：投不出去不等于那把钥匙就该一直留着。
    """
    with factory() as session:
        session.execute(
            update(DomainOutbox)
            .where(DomainOutbox.id == outbox_id, DomainOutbox.status == OutboxStatus.PENDING)
            .values(status=status, processed_at=utc_now(), payload_json=None)
        )
        session.commit()


def _attempt_send(settings: Settings, *, event_type: str, raw_payload: str | None) -> bool:
    """Render and hand off one message. **Never raises** — failure is a False."""
    renderer = _RENDERERS.get(event_type)
    if renderer is None:
        logger.error("No renderer for this outbox event type", extra={"event_type": event_type})
        return False
    try:
        payload = json.loads(raw_payload) if raw_payload else {}
    except json.JSONDecodeError:
        logger.error("An outbox row has unreadable payload JSON", exc_info=True)
        return False
    if not isinstance(payload, dict):
        logger.error("An outbox payload is not an object")
        return False

    message = renderer(settings, payload)
    if message is None:
        return False
    return build_transport(settings).send(message)


@shared_task(name="app.tasks.outbox.recover")
def recover() -> int:
    """Re-trigger every due row. **This is the Invariant 14 half.**

    ⚠️ 没有这个周期任务，「Redis 被清空」或者「worker 在 `delay()` 之后、投递
    之前挂掉」都会让那些行**永远躺在库里**：状态是 PENDING、谁也不会再看它们
    一眼，而用户那边的表现只是「没收到信」。

    返回重新触发了多少行（给日志与将来的积压告警用，T0.9）。
    """
    settings = get_settings()
    now = utc_now()
    with _session_factory()() as session:
        due = (
            session.execute(
                select(DomainOutbox.id)
                .where(
                    DomainOutbox.status == OutboxStatus.PENDING,
                    DomainOutbox.next_retry_at <= now,
                )
                .order_by(DomainOutbox.id)
                .limit(settings.outbox_recovery_batch)
            )
            .scalars()
            .all()
        )

    for outbox_id in due:
        deliver.delay(outbox_id)
    if due:
        logger.info("Re-queued due outbox rows", extra={"count": len(due)})
    return len(due)


__all__ = ["deliver", "recover"]

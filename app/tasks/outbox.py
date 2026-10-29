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

**通知类事件**（`tenant.low_balance`、`tenant.billing_status_changed`，
设计闸门 #471 v7 §2「投递」）不发信：领取之后在一个事务里结算这一行并插入一条门户通知，
二者原子提交，所以同一行不论投递几次最多一条通知。
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import uuid
from dataclasses import dataclass
from functools import lru_cache
from typing import Final
from urllib.parse import quote

from celery import shared_task
from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings, get_settings
from app.core.database import create_database_engine, create_session_factory
from app.core.mailer import OutgoingEmail, build_transport
from app.models.auth import DomainOutbox, OutboxStatus
from app.models.notifications import Notification
from app.models.tenancy import Tenant
from app.repositories.wallet import EVENT_BILLING_STATUS_CHANGED, EVENT_LOW_BALANCE
from app.services.auth import utc_now
from app.services.notifications import InvalidPayload, NotificationDraft, notification_for
from app.services.password_reset import EVENT_PASSWORD_RESET
from app.services.portal_accounts import EVENT_PORTAL_INVITATION

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


# 马来西亚时间：固定偏移 UTC+8，不依赖 tzdata
# （设计闸门 #368 v12 §2「邀请邮件」）。马来西亚不实行夏令时，
# 固定偏移就是它的全部规则；worker 镜像里有没有 tzdata 不该决定信能不能发出去。
_MALAYSIA = dt.timezone(dt.timedelta(hours=8))


def _invitation_deadline(raw: object) -> str | None:
    """`expires_at` from the payload, as Malaysia time to the minute plus UTC; None if unusable.

    ⚠️ **只用 payload 里的值，不读当前配置、不写相对时长**（设计 §2）：
    它与令牌行的 `expires_at` 是同一个值（不带时区的 UTC、整秒），
    outbox 重试或有效期配置改动之后，信里的到期时刻仍与令牌真实的到期时刻一致。
    """
    if not isinstance(raw, str) or not raw:
        return None
    try:
        moment = dt.datetime.fromisoformat(raw)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.UTC)
    utc = moment.astimezone(dt.UTC)
    local = moment.astimezone(_MALAYSIA)
    return f"{local:%Y-%m-%d %H:%M} Malaysia time, UTC+8 ({utc:%Y-%m-%d %H:%M} UTC)"


def _render_portal_invitation(
    settings: Settings, payload: dict[str, object]
) -> OutgoingEmail | None:
    """Build the portal invitation email, or None when we cannot (which counts as a failure)."""
    base = settings.frontend_base_url.strip().rstrip("/")
    if not base:
        # 与重置邮件同一处理：拼不出可点的链接就不发。
        # 客户的账号是 `INVITED`、没有可用密码，一封点不动的邀请信等于一条死路。
        logger.error("BILLING_FRONTEND_BASE_URL is not configured; cannot build an invitation link")
        return None

    to = str(payload.get("to") or "")
    token = str(payload.get("token") or "")
    deadline = _invitation_deadline(payload.get("expires_at"))
    if not to or not token or deadline is None:
        logger.error("A portal invitation outbox row is missing its recipient, token or expiry")
        return None

    # 路径与参数名由前端 `routes/paths.ts` 登记
    # （`acceptInvitation`、`ACCEPT_INVITATION_TOKEN_PARAM`），
    # 由 tests/backend/test_invitation_link.py 机械比对。
    link = f"{base}/accept-invitation?token={quote(token, safe='')}"
    body = (
        "You have been invited to the Acuven Billing customer portal.\n\n"
        f"Open this link to set your password and activate your account:\n{link}\n\n"
        f"The link can be used once, and expires at {deadline}. "
        "If it has expired, ask your Acuven contact to send a new invitation.\n\n"
        "If you are waiting for another email from us and cannot find it, "
        "please check your spam or junk folder.\n\n"
        "If you were not expecting this invitation, you can ignore this email."
    )
    return OutgoingEmail(
        to=to, subject="Your invitation to the Acuven Billing customer portal", body=body
    )


# event_type → 渲染函数。
#
# ⚠️ 认不出来的事件类型走**普通失败**（重试，最终进死信），不是立刻死信。
# 因为最可能的原因是**滚动更新的时间差**：API 已经在写新事件，worker 镜像还是
# 旧的。这种行在 worker 更新后就能正常投出去 —— 立刻判死信等于把它们永久丢掉，
# 而它们本来只需要等几分钟。重试上限兜住了「真的没人认识它」那种情况。
#
# ⚠️ `recover` 只重投有处理器的类型（`handled_event_types()`，设计闸门 #88 §2）。
# 仍没有处理器的类型（`tenant.account_status_changed`、`tenant.billing_mode_changed`）
# 以 PENDING 持久躺在库里，等以后的任务加上处理器再接手，
# 不会被反复重投、退避成死信（INV-14）。
#
# 门户邀请（AIH-TASK-094）登记之后，恢复扫描才会领
# `PORTAL_INVITATION_REQUESTED` 行；在那之前它们以 PENDING 躺在库里，等的就是这一行。
_RENDERERS = {
    EVENT_PASSWORD_RESET: _render_password_reset,
    EVENT_PORTAL_INVITATION: _render_portal_invitation,
}

# 通知类事件（设计闸门 #471 v7 §2「投递」）：不调 SMTP、不需要前端基址，
# 规则在 app/services/notifications.py。
NOTIFICATION_EVENTS: Final = frozenset({EVENT_LOW_BALANCE, EVENT_BILLING_STATUS_CHANGED})


def handled_event_types() -> list[str]:
    """Every event type with a handler: the email ones and the notification ones.

    ⚠️ `recover` 的扫描与 `alerts.outbox_backlog` 的计数用这同一份名单：
    两处各自维护时，一边加了类型另一边忘了，就是「投了却不报积压」或「报积压却没人投」。
    """
    return sorted(set(_RENDERERS) | NOTIFICATION_EVENTS)


@dataclass(frozen=True)
class _Claim:
    """What `deliver` read from the row before claiming it, and the attempt it claimed."""

    outbox_id: int
    # 本次领取写入的 `attempt_count`：结算的前置条件，只有当前领取者能结算这一行。
    attempt: int
    event_type: str
    aggregate_type: str
    aggregate_id: str
    payload_json: str | None
    created_at: dt.datetime


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
        claim = _Claim(
            outbox_id=outbox_id,
            attempt=attempt,
            event_type=event_type,
            aggregate_type=row.aggregate_type,
            aggregate_id=row.aggregate_id,
            payload_json=raw_payload,
            created_at=row.created_at,
        )

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

    if event_type in NOTIFICATION_EVENTS:
        return _deliver_notification(factory, settings, claim)

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


# --- 通知类事件（设计闸门 #471 v7 §2「投递」、§4、§5） ------------------------------------
#
# ⚠️ 日志只带 `outbox_id`、`event_type` 与尝试次数（设计 §6）：
# 不带余额、阈值、租户标识，也不带 exc_info —— 数据库异常的消息里有绑定参数。
# 排查看 `domain_outbox` 里保留的 payload。


def _now() -> dt.datetime:
    """`utc_now()` truncated to whole seconds, as in app/services/account_status.py."""
    return utc_now().replace(microsecond=0)


def _deliver_notification(factory: sessionmaker[Session], settings: Settings, claim: _Claim) -> str:
    """Settle one claimed notification row. **Never raises** — the return value says what
    happened."""
    try:
        settled = _settle_notification(factory, claim)
    except Exception:
        # 事务已随会话关闭回滚：行仍 PENDING，没有通知行。
        settled = None
    if settled is not None:
        return settled

    if claim.attempt < settings.outbox_max_attempts:
        logger.warning(
            "Outbox notification failed; it will be retried",
            extra={
                "outbox_id": claim.outbox_id,
                "event_type": claim.event_type,
                "attempt": claim.attempt,
            },
        )
        return "retry-later"

    try:
        dead = _dead_letter_notification(factory, claim)
    except Exception:
        logger.error(
            "Could not dead-letter an outbox notification; it will be retried",
            extra={
                "outbox_id": claim.outbox_id,
                "event_type": claim.event_type,
                "attempts": claim.attempt,
            },
        )
        return "retry-later"
    if not dead:
        # 这一行已被更晚的一次领取接手（或已结算）：由那一方结算，这里不记 error。
        return "superseded"
    # ⚠️ **error 级别**：没有别人会再碰这一行了，只能靠人。payload 保留着供排查。
    logger.error(
        "Giving up on an outbox notification after repeated failures",
        extra={
            "outbox_id": claim.outbox_id,
            "event_type": claim.event_type,
            "attempts": claim.attempt,
        },
    )
    return "dead-lettered"


def _settle_notification(factory: sessionmaker[Session], claim: _Claim) -> str | None:
    """One transaction: settle the row as SENT and insert its notification (INV-13).

    返回值：
    - `notified`：插入了一条；
    - `no-notification`：规则判不出通知，行照常 SENT；
    - `already-settled`：结算 UPDATE 受影响行数不是 1
      （另一个 worker 已结算，或这一行已被更晚的一次领取接手）；
    - None：payload 无效、租户查不到，已回滚，由调用方走失败分支。

    事务内的异常照常抛出，会话关闭时回滚。
    """
    now = _now()
    with factory() as session:
        settled = session.execute(
            update(DomainOutbox)
            .where(
                DomainOutbox.id == claim.outbox_id,
                DomainOutbox.status == OutboxStatus.PENDING,
                DomainOutbox.attempt_count == claim.attempt,
            )
            # ⚠️ 不清空 payload：通知类 payload 只有余额、阈值、状态与流水 `public_id`，
            # 不含令牌或个人数据，留着供排查（设计 §2）。
            .values(status=OutboxStatus.SENT, processed_at=now)
        )
        if int(settled.rowcount) != 1:
            session.rollback()
            return "already-settled"

        outcome = notification_for(
            event_type=claim.event_type,
            aggregate_type=claim.aggregate_type,
            payload_json=claim.payload_json,
            created_at=claim.created_at,
        )
        if isinstance(outcome, InvalidPayload):
            session.rollback()
            return None
        if not isinstance(outcome, NotificationDraft):
            session.commit()
            return "no-notification"

        tenant_id = session.execute(
            select(Tenant.id).where(Tenant.public_id == claim.aggregate_id)
        ).scalar_one_or_none()
        if tenant_id is None:
            # 租户从不删除，不应发生；与 payload 无效同样处理。
            session.rollback()
            return None
        _insert_notification(session, claim=claim, tenant_id=int(tenant_id), draft=outcome, now=now)
        session.commit()
        return "notified"


def _insert_notification(
    session: Session,
    *,
    claim: _Claim,
    tenant_id: int,
    draft: NotificationDraft,
    now: dt.datetime,
) -> None:
    """Add the notification row in the settling transaction. `source_outbox_id` is unique: the
    database's own guarantee of one notification per outbox row."""
    session.add(
        Notification(
            public_id=str(uuid.uuid4()),
            tenant_id=tenant_id,
            notification_type=draft.notification_type,
            source_outbox_id=claim.outbox_id,
            balance=draft.balance,
            threshold=draft.threshold,
            occurred_at=draft.occurred_at,
            created_at=now,
        )
    )
    session.flush()


def _dead_letter_notification(factory: sessionmaker[Session], claim: _Claim) -> bool:
    """FAILED, only if this claim is still the current one. Keeps the payload.

    ⚠️ 与成功结算一样带 `attempt_count = :attempt`：
    旧领取者停顿期间行被更晚的一次领取接手时，它的死信结算受影响 0 行，
    不会把新领取者正在处理的行置为 FAILED（设计 §4「串行化」）。
    """
    with factory() as session:
        result = session.execute(
            update(DomainOutbox)
            .where(
                DomainOutbox.id == claim.outbox_id,
                DomainOutbox.status == OutboxStatus.PENDING,
                DomainOutbox.attempt_count == claim.attempt,
            )
            .values(status=OutboxStatus.FAILED, processed_at=_now())
        )
        session.commit()
        return int(result.rowcount) == 1


@shared_task(name="app.tasks.outbox.recover")
def recover() -> int:
    """Re-trigger every due row. **This is the Invariant 14 half.**

    ⚠️ 没有这个周期任务，「Redis 被清空」或者「worker 在 `delay()` 之后、投递
    之前挂掉」都会让那些行**永远躺在库里**：状态是 PENDING、谁也不会再看它们
    一眼，而用户那边的表现只是「没收到信」。

    ⚠️ 只捡有处理器的事件类型（`handled_event_types()`：邮件类与通知类）。没有处理器的行
    重投了也只会失败、退避，最后成为死信 —— 而它们本来只是在等以后的任务来处理
    （见 `_RENDERERS` 上方的注释）。

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
                    DomainOutbox.event_type.in_(handled_event_types()),
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


__all__ = ["NOTIFICATION_EVENTS", "deliver", "handled_event_types", "recover"]

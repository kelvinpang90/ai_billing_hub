"""Outbox delivery, retry and recovery (spec §74.6, §25, §98.1; Invariant 14).

⚠️ **这个文件里绝大多数用例钉的是失败路径**，因为这一层的正常路径几乎不会出错，
而失败路径出错时全都是安静的：一封永远重投的信、一行永远卡住没人再看的记录、
一个投递成功却把令牌明文永久留在库里的 payload。
"""

from __future__ import annotations

import datetime as dt
import json

import pytest
from sqlalchemy import create_engine, select

from app.core.config import Settings
from app.core.database import create_session_factory
from app.models.auth import DomainOutbox, OutboxStatus, User, UserRole, UserStatus
from app.models.base import Base
from app.repositories import tenancy
from app.repositories.wallet import (
    AGGREGATE_TENANT,
    EVENT_BILLING_STATUS_CHANGED,
    EVENT_LOW_BALANCE,
)
from app.services import alerts
from app.services.account_status import EVENT_ACCOUNT_STATUS_CHANGED
from app.services.auth import RequestContext, utc_now
from app.services.internal_billing import EVENT_BILLING_MODE_CHANGED
from app.services.password_reset import AGGREGATE_USERS, EVENT_PASSWORD_RESET
from app.services.portal_accounts import EVENT_PORTAL_INVITATION, create_portal_account
from app.tasks import outbox as outbox_task
from app.tasks.outbox import _backoff_seconds, _render_portal_invitation

TOKEN = "a-token-that-looks-like-the-real-thing"
# 邀请令牌的到期时刻：与服务写进 payload 的形状相同（不带时区的 UTC、整秒、ISO 8601）。
# 换算到马来西亚时间（UTC+8）是同一天 16:00。
EXPIRES_AT = "2026-10-24T08:00:00"
INVITATION_PAYLOAD = {"to": "portal@example.com", "token": TOKEN, "expires_at": EXPIRES_AT}


@pytest.fixture
def settings() -> Settings:
    return Settings(
        frontend_base_url="https://billing.example.com",
        outbox_retry_base_seconds=60,
        outbox_max_attempts=3,
    )


@pytest.fixture
def session_factory():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    return create_session_factory(engine)


@pytest.fixture
def wired(monkeypatch, session_factory, settings: Settings):
    """Point the task at this test's database and settings.

    ⚠️ 任务里的 `_session_factory()` 带 `lru_cache`（一个 worker 进程一个引擎）。
    不换掉的话，第一个用例会把自己的内存库缓存给后面所有用例。
    """
    monkeypatch.setattr(outbox_task, "_session_factory", lambda: session_factory)
    monkeypatch.setattr(outbox_task, "get_settings", lambda: settings)
    return session_factory


class FakeTransport:
    """Records what it was asked to send, and can be told to fail."""

    def __init__(self, *, succeed: bool = True) -> None:
        self.succeed = succeed
        self.sent: list[object] = []

    def send(self, message) -> bool:  # noqa: ANN001 - 形状由 EmailTransport 协议定
        self.sent.append(message)
        return self.succeed


@pytest.fixture
def transport(monkeypatch) -> FakeTransport:
    fake = FakeTransport()
    monkeypatch.setattr(outbox_task, "build_transport", lambda _settings: fake)
    return fake


def add_row(session_factory, *, event_type: str = EVENT_PASSWORD_RESET, **overrides) -> int:
    now = utc_now()
    payload = {"to": "admin@example.com", "token": TOKEN}
    fields = {
        "event_type": event_type,
        "aggregate_type": AGGREGATE_USERS,
        "aggregate_id": "1",
        "payload_json": json.dumps(payload, sort_keys=True),
        "status": OutboxStatus.PENDING,
        "attempt_count": 0,
        "next_retry_at": now,
        "created_at": now,
        **overrides,
    }
    with session_factory() as session:
        row = DomainOutbox(**fields)
        session.add(row)
        session.commit()
        return int(row.id)


def read(session_factory, outbox_id: int) -> DomainOutbox:
    with session_factory() as session:
        row = session.get(DomainOutbox, outbox_id)
        assert row is not None
        session.expunge(row)
        return row


# --- 正常路径 -----------------------------------------------------------------


def test_a_delivered_row_is_marked_sent(wired, transport: FakeTransport) -> None:
    outbox_id = add_row(wired)

    assert outbox_task.deliver(outbox_id) == "sent"

    row = read(wired, outbox_id)
    assert row.status is OutboxStatus.SENT
    assert row.processed_at is not None
    assert len(transport.sent) == 1


def test_the_token_does_not_stay_in_the_database_after_delivery(
    wired, transport: FakeTransport
) -> None:
    """⚠️ payload 里是**令牌明文** —— 而 outbox 行是长期保留的。

    它的用途在投递完成的那一瞬就结束了。留着等于在库里放一把随时可用的钥匙，
    而这张表将来还会装别的事件，谁都不会想到去翻它。
    """
    outbox_id = add_row(wired)

    outbox_task.deliver(outbox_id)

    assert read(wired, outbox_id).payload_json is None


def test_the_email_carries_a_link_to_the_configured_front_end(
    wired, transport: FakeTransport
) -> None:
    outbox_id = add_row(wired)

    outbox_task.deliver(outbox_id)

    body = transport.sent[0].body
    assert f"https://billing.example.com/reset-password?token={TOKEN}" in body


# --- 失败与重试 ---------------------------------------------------------------


def test_a_failed_send_is_left_pending_with_a_later_retry(wired, monkeypatch) -> None:
    """⚠️ 失败**不能**把行标成终态：那样一次 SMTP 抖动就等于这封信永久消失。"""
    monkeypatch.setattr(outbox_task, "build_transport", lambda _s: FakeTransport(succeed=False))
    outbox_id = add_row(wired)
    before = read(wired, outbox_id).next_retry_at

    assert outbox_task.deliver(outbox_id) == "retry-later"

    row = read(wired, outbox_id)
    assert row.status is OutboxStatus.PENDING
    assert row.attempt_count == 1
    assert row.next_retry_at > before


def test_the_backoff_doubles_each_attempt(settings: Settings) -> None:
    """固定间隔的重试会在一个持续故障里把 worker 变成一台空转的机器。

    ⚠️ **这条用例是变异测试逼出来的。**原来只在集成层断言「第二次的间隔比第一次
    大」—— 而那**毫秒级的执行抖动就能满足**：把退避改成固定间隔，用例照样全绿。
    一条看起来在测退避的用例，实际上什么也没钉住。

    退避是个纯函数，直接钉它的值，不留任何解释空间。
    """
    assert [_backoff_seconds(settings, n) for n in (1, 2, 3, 4)] == [60, 120, 240, 480]


def test_the_retry_interval_grows_between_real_attempts(wired, monkeypatch) -> None:
    """接上一条：那个纯函数确实被投递路径用上了，没有被常数绕过。"""
    monkeypatch.setattr(outbox_task, "build_transport", lambda _s: FakeTransport(succeed=False))
    outbox_id = add_row(wired)

    gaps = []
    for _ in range(2):
        start = utc_now()
        outbox_task.deliver(outbox_id)
        gaps.append((read(wired, outbox_id).next_retry_at - start).total_seconds())
        # 把时间推到「下次可投」，好让下一轮能领到。
        with wired() as session:
            row = session.get(DomainOutbox, outbox_id)
            assert row is not None
            row.next_retry_at = utc_now()
            session.commit()

    # ⚠️ 比的是**倍数**，不是「大一点」—— 后者被执行抖动满足。
    assert gaps[1] >= gaps[0] * 1.8


def test_it_gives_up_after_the_configured_number_of_attempts(wired, monkeypatch) -> None:
    """⚠️ 没有上限的重试会把一封永远发不出去的信（比如收件地址根本不存在）
    变成一个永久转的循环，而且它会一直排在别的信前面。"""
    monkeypatch.setattr(outbox_task, "build_transport", lambda _s: FakeTransport(succeed=False))
    outbox_id = add_row(wired)

    outcomes = []
    for _ in range(3):
        outcomes.append(outbox_task.deliver(outbox_id))
        with wired() as session:
            row = session.get(DomainOutbox, outbox_id)
            assert row is not None
            row.next_retry_at = utc_now()
            session.commit()

    assert outcomes == ["retry-later", "retry-later", "dead-lettered"]
    row = read(wired, outbox_id)
    assert row.status is OutboxStatus.FAILED
    # ⚠️ 死信同样要清 payload：投不出去不等于那把钥匙就该一直留在库里。
    assert row.payload_json is None


def test_a_row_that_is_not_due_yet_is_left_alone(wired, transport: FakeTransport) -> None:
    """⚠️ 领取的前置条件里有 `next_retry_at <= now`。

    没有这一条，恢复任务与 API 的即时触发会一起把同一行反复投出去 —— 用户收到
    一串重复的信，而退避看起来「配了」。
    """
    outbox_id = add_row(wired, next_retry_at=utc_now() + dt.timedelta(minutes=5))

    assert outbox_task.deliver(outbox_id) == "not-claimed"
    assert transport.sent == []


def test_an_already_settled_row_is_not_sent_again(wired, transport: FakeTransport) -> None:
    outbox_id = add_row(wired, status=OutboxStatus.SENT, processed_at=utc_now())

    assert outbox_task.deliver(outbox_id) == "already-settled"
    assert transport.sent == []


def test_a_missing_row_is_not_an_error(wired, transport: FakeTransport) -> None:
    assert outbox_task.deliver(9_999) == "missing"


def test_an_unknown_event_type_goes_nowhere_near_the_transport(
    wired, transport: FakeTransport
) -> None:
    """⚠️ 走**重试**，不是立刻死信。

    最可能的原因是滚动更新的时间差：API 已经在写新事件、worker 镜像还是旧的。
    那种行在 worker 更新后就能投出去，立刻判死信等于把它们永久丢掉。
    真的没人认识它时，重试上限会兜住。
    """
    outbox_id = add_row(wired, event_type="SOMETHING_FROM_THE_FUTURE")

    assert outbox_task.deliver(outbox_id) == "retry-later"
    assert transport.sent == []


def test_a_row_without_a_front_end_base_url_is_not_sent(wired, monkeypatch, transport) -> None:
    """⚠️ **不发一封带半截 URL 的信** —— 那比不发更糟：用户以为流程走通了。"""
    monkeypatch.setattr(outbox_task, "get_settings", lambda: Settings(frontend_base_url=""))
    outbox_id = add_row(wired)

    assert outbox_task.deliver(outbox_id) == "retry-later"
    assert transport.sent == []


def test_unreadable_payload_json_does_not_raise(wired, transport: FakeTransport) -> None:
    """任务里抛异常会让 Celery 重投，绕过我们自己的退避。"""
    outbox_id = add_row(wired)
    with wired() as session:
        row = session.get(DomainOutbox, outbox_id)
        assert row is not None
        row.payload_json = "{not json"
        session.commit()

    assert outbox_task.deliver(outbox_id) == "retry-later"


# --- 周期恢复：Invariant 14 的那一半 --------------------------------------------


def test_recovery_requeues_every_due_row(wired, monkeypatch, settings: Settings) -> None:
    """⚠️ 没有这个扫描，「Redis 被清空」或「worker 在触发之后挂掉」会让那些行
    **永远躺在库里**：状态 PENDING、谁也不再看一眼，用户只表现为没收到信。"""
    due = [add_row(wired), add_row(wired)]
    add_row(wired, next_retry_at=utc_now() + dt.timedelta(minutes=5))
    add_row(wired, status=OutboxStatus.SENT, processed_at=utc_now())

    queued: list[int] = []
    monkeypatch.setattr(outbox_task.deliver, "delay", lambda outbox_id: queued.append(outbox_id))

    assert outbox_task.recover() == 2
    assert queued == due


def test_recovery_leaves_events_without_a_handler_pending(wired, monkeypatch) -> None:
    """INV-14（设计闸门 #88 §2）：没有处理器的事件**不重投**，原样等着。

    ⚠️ 账户状态与计费模式事件现在还没有投递方。重投它们只会失败、退避，几轮之后
    成为 `FAILED` 死信 —— 等以后的任务接上处理器时，它们已经没了。
    两类通知事件（设计闸门 #471 v7）已有处理器，照常重投。
    """
    reset = add_row(wired)
    notifications = [
        add_row(
            wired,
            event_type=event_type,
            aggregate_type=AGGREGATE_TENANT,
            payload_json=json.dumps({"billing_status": "SUSPENDED"}),
        )
        for event_type in (EVENT_BILLING_STATUS_CHANGED, EVENT_LOW_BALANCE)
    ]
    payload = json.dumps({"account_status": "DISABLED"})
    waiting = [
        add_row(
            wired,
            event_type=event_type,
            aggregate_type=AGGREGATE_TENANT,
            payload_json=payload,
        )
        for event_type in (EVENT_ACCOUNT_STATUS_CHANGED, EVENT_BILLING_MODE_CHANGED)
    ]
    queued: list[int] = []
    monkeypatch.setattr(outbox_task.deliver, "delay", lambda outbox_id: queued.append(outbox_id))

    assert outbox_task.recover() == 3
    assert queued == [reset, *notifications], "密码重置与两类通知事件重投，只有它们"
    for outbox_id in waiting:
        row = read(wired, outbox_id)
        assert row.status is OutboxStatus.PENDING
        assert row.attempt_count == 0
        assert row.payload_json == payload


def test_the_handled_types_are_the_email_and_the_notification_events() -> None:
    """设计闸门 #471 v7 §7「recover 与告警名单」：一个函数，邮件类并通知类。"""
    assert set(outbox_task.handled_event_types()) == {
        EVENT_PASSWORD_RESET,
        EVENT_PORTAL_INVITATION,
        EVENT_LOW_BALANCE,
        EVENT_BILLING_STATUS_CHANGED,
    }
    assert outbox_task.NOTIFICATION_EVENTS == {EVENT_LOW_BALANCE, EVENT_BILLING_STATUS_CHANGED}
    for still_waiting in (EVENT_ACCOUNT_STATUS_CHANGED, EVENT_BILLING_MODE_CHANGED):
        assert still_waiting not in outbox_task.handled_event_types()


def test_recovery_and_the_backlog_alert_read_the_same_list(wired, monkeypatch) -> None:
    """两处不再各自引用 `_RENDERERS`：换掉名单函数，两边一起跟着变。"""
    row = add_row(wired, event_type=EVENT_ACCOUNT_STATUS_CHANGED, aggregate_type=AGGREGATE_TENANT)
    monkeypatch.setattr(outbox_task, "handled_event_types", lambda: [EVENT_ACCOUNT_STATUS_CHANGED])
    queued: list[int] = []
    monkeypatch.setattr(outbox_task.deliver, "delay", lambda outbox_id: queued.append(outbox_id))

    assert outbox_task.recover() == 1
    assert queued == [row]
    context = alerts.AlertContext(now=utc_now() + dt.timedelta(hours=1), currencies=())
    with wired() as session:
        assert alerts.outbox_backlog(session, context).level is alerts.AlertLevel.P2


def test_recovery_takes_at_most_one_batch(wired, monkeypatch) -> None:
    """一次积压不该把 worker 淹掉。"""
    for _ in range(5):
        add_row(wired)
    monkeypatch.setattr(
        outbox_task,
        "get_settings",
        lambda: Settings(frontend_base_url="https://x.example.com", outbox_recovery_batch=2),
    )
    monkeypatch.setattr(outbox_task.deliver, "delay", lambda outbox_id: None)

    assert outbox_task.recover() == 2


def test_a_row_survives_a_worker_that_dies_mid_delivery(wired, monkeypatch) -> None:
    """⚠️ **这条钉的是「为什么没有 PROCESSING 状态」。**

    领取的同时就把 `next_retry_at` 推后，状态仍是 PENDING。于是 worker 崩在发信
    中途的后果只是「这一行晚几分钟重投」—— 不需要任何「卡在 PROCESSING 超过 N
    分钟就放回去」的清扫逻辑，也就不会有那种逻辑写错时的永久卡死。
    """

    def explode(*_args, **_kwargs):
        raise RuntimeError("the worker died here")

    monkeypatch.setattr(outbox_task, "_attempt_send", explode)
    outbox_id = add_row(wired)

    with pytest.raises(RuntimeError):
        outbox_task.deliver(outbox_id)

    row = read(wired, outbox_id)
    assert row.status is OutboxStatus.PENDING, "崩溃不该让这一行变成任何终态"
    assert row.next_retry_at > utc_now(), "而且它必须自己回到队列里，不靠别人来捞"


def test_recovery_can_find_that_row_once_it_is_due(wired, monkeypatch) -> None:
    """接上一条：那一行到期之后，周期扫描确实会重新触发它。"""
    outbox_id = add_row(wired, next_retry_at=utc_now() - dt.timedelta(seconds=1), attempt_count=1)
    queued: list[int] = []
    monkeypatch.setattr(outbox_task.deliver, "delay", lambda i: queued.append(i))

    outbox_task.recover()

    assert queued == [outbox_id]


def test_the_due_index_matches_the_recovery_query() -> None:
    """扫描是每分钟一次的全表条件查询 —— 没有索引时它会随 outbox 增长线性变慢，
    而症状是「通知越来越晚」，不是任何一条报错。"""
    index = next(i for i in DomainOutbox.__table__.indexes if i.name == "ix_domain_outbox_due")

    assert [c.name for c in index.columns] == ["status", "next_retry_at"]


def test_every_pending_row_is_eventually_visible_to_recovery(wired) -> None:
    """恢复的条件只看 `status` 与 `next_retry_at` —— **不看 `attempt_count`**。

    ⚠️ 如果把「试过几次」也写进条件，重试过的行就会从扫描里消失，而它们恰恰是
    最需要被补投的那些。
    """
    tried = add_row(wired, attempt_count=2, next_retry_at=utc_now() - dt.timedelta(minutes=1))

    with wired() as session:
        visible = session.execute(
            select(DomainOutbox.id).where(
                DomainOutbox.status == OutboxStatus.PENDING,
                DomainOutbox.next_retry_at <= utc_now(),
            )
        ).scalars()
        assert tried in list(visible)


# --- 门户邀请邮件（AIH-TASK-094，设计闸门 #368 v12 §2「邀请邮件与重置的隔离」） ----------


def add_invitation(session_factory, payload: dict[str, object] | None = None, **overrides) -> int:
    return add_row(
        session_factory,
        event_type=EVENT_PORTAL_INVITATION,
        payload_json=json.dumps(payload or INVITATION_PAYLOAD, sort_keys=True),
        **overrides,
    )


def test_an_invitation_is_delivered_with_a_link_to_the_accept_page(
    wired, transport: FakeTransport
) -> None:
    outbox_id = add_invitation(wired)

    assert outbox_task.deliver(outbox_id) == "sent"

    [message] = transport.sent
    assert message.to == "portal@example.com"
    assert f"https://billing.example.com/accept-invitation?token={TOKEN}" in message.body
    assert "reset-password" not in message.body
    # 令牌明文同样在投递完成时清掉。
    assert read(wired, outbox_id).payload_json is None


def test_the_invitation_states_the_deadline_in_malaysia_time_with_utc() -> None:
    """设计 §2：马来西亚时间（UTC+8）精确到分钟，括号附 UTC。

    另提示查垃圾箱；不写「72 小时」这类相对时长。
    """
    message = _render_portal_invitation(
        Settings(frontend_base_url="https://billing.example.com"), INVITATION_PAYLOAD
    )

    assert message is not None
    assert "2026-10-24 16:00 Malaysia time, UTC+8 (2026-10-24 08:00 UTC)" in message.body
    assert "spam" in message.body
    for relative in ("72", "hour", "day"):
        assert relative not in message.body.lower()


def test_the_deadline_crosses_midnight_in_malaysia_time() -> None:
    """换算在日期上也要对：UTC 20:30 是马来西亚第二天 04:30。"""
    payload = {**INVITATION_PAYLOAD, "expires_at": "2026-12-31T20:30:00"}

    message = _render_portal_invitation(
        Settings(frontend_base_url="https://billing.example.com"), payload
    )

    assert message is not None
    assert "2027-01-01 04:30 Malaysia time, UTC+8 (2026-12-31 20:30 UTC)" in message.body


def test_the_deadline_comes_from_the_payload_not_from_the_current_setting() -> None:
    """设计 §7「邮件到期时刻」：把有效期配置改掉重新渲染，两次正文的到期时刻相同。

    outbox 重试时配置可能已经变了，信里的时刻仍须与令牌行真实的 `expires_at` 一致。
    """
    first = _render_portal_invitation(
        Settings(
            frontend_base_url="https://billing.example.com",
            customer_invitation_ttl_seconds=259_200,
        ),
        INVITATION_PAYLOAD,
    )
    second = _render_portal_invitation(
        Settings(
            frontend_base_url="https://billing.example.com",
            customer_invitation_ttl_seconds=3_600,
        ),
        INVITATION_PAYLOAD,
    )

    assert first is not None and second is not None
    assert first.body == second.body
    assert "2026-10-24 16:00 Malaysia time, UTC+8 (2026-10-24 08:00 UTC)" in second.body


def test_an_invitation_without_a_front_end_base_url_is_not_sent(
    wired, monkeypatch, transport: FakeTransport
) -> None:
    """与重置邮件同一处理：不发一封带半截 URL 的信，走普通失败、等配置补上后重试。"""
    monkeypatch.setattr(outbox_task, "get_settings", lambda: Settings(frontend_base_url=""))
    outbox_id = add_invitation(wired)

    assert outbox_task.deliver(outbox_id) == "retry-later"
    assert transport.sent == []
    assert read(wired, outbox_id).payload_json is not None


@pytest.mark.parametrize(
    "payload",
    [
        {"to": "portal@example.com", "token": TOKEN},
        {**INVITATION_PAYLOAD, "expires_at": "not-a-moment"},
        {**INVITATION_PAYLOAD, "token": ""},
        {**INVITATION_PAYLOAD, "to": ""},
    ],
    ids=["no-expiry", "bad-expiry", "no-token", "no-recipient"],
)
def test_an_invitation_missing_a_field_is_not_sent(
    wired, transport: FakeTransport, payload: dict[str, object]
) -> None:
    """缺到期时刻时不能退回去读当前配置：那样写出来的时刻可能与令牌不一致。"""
    outbox_id = add_invitation(wired, payload)

    assert outbox_task.deliver(outbox_id) == "retry-later"
    assert transport.sent == []


def test_recovery_picks_up_an_invitation_left_behind_by_a_lost_trigger(
    wired, monkeypatch, settings: Settings, transport: FakeTransport
) -> None:
    """设计 §7「Redis/Celery 丢失」的接口层部分：开通后没有任何触发，行留在 `PENDING`。

    渲染器登记之后，现有恢复扫描能领到它（扫描只选有渲染器的事件类型），投出去的信里就是
    这次开通签发的那张令牌。
    """
    now = utc_now().replace(microsecond=0)
    with wired() as session:
        tenant = tenancy.create_tenant(
            session, company_name="Portal Sdn Bhd", email="ops@example.com", now=now
        )
        admin = User(
            email="admin@example.com",
            password_hash="not-a-real-hash",
            role=UserRole.ADMIN,
            status=UserStatus.ACTIVE,
            created_at=now,
            updated_at=now,
        )
        session.add(admin)
        session.commit()
        session.refresh(tenant)
        session.refresh(admin)
        session.expunge_all()
    issued = create_portal_account(
        wired,
        settings,
        actor=admin,
        customer_id=tenant.public_id,
        email="portal@example.com",
        context=RequestContext(ip_address="10.0.0.1", user_agent="pytest"),
    )
    row = read(wired, issued.outbox_id)
    assert (row.event_type, row.status) == (EVENT_PORTAL_INVITATION, OutboxStatus.PENDING)
    token = json.loads(row.payload_json or "{}")["token"]

    queued: list[int] = []
    monkeypatch.setattr(outbox_task.deliver, "delay", lambda outbox_id: queued.append(outbox_id))
    assert outbox_task.recover() == 1
    assert queued == [issued.outbox_id]

    assert outbox_task.deliver(issued.outbox_id) == "sent"
    [message] = transport.sent
    assert f"https://billing.example.com/accept-invitation?token={token}" in message.body

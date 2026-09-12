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
from app.models.auth import DomainOutbox, OutboxStatus
from app.models.base import Base
from app.services.auth import utc_now
from app.services.password_reset import AGGREGATE_USERS, EVENT_PASSWORD_RESET
from app.tasks import outbox as outbox_task
from app.tasks.outbox import _backoff_seconds

TOKEN = "a-token-that-looks-like-the-real-thing"


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

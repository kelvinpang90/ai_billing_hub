"""Password changed emails: template, rendering and delivery (design gate #751 v3, I5).

设计 §7 标 I5 的几行：

- **模板**（unit）：`render_email(TEMPLATE_PASSWORD_CHANGED, …)` 标题与正文与 §2 逐字一致；
  参数缺一个、多一个返回 `None`；
- **渲染**（unit）：payload 合规时 `Channel.EMAIL`、收件人为 `to`、`changed_at` 为 MYT 显示值、
  链接为 `{base}/forgot-password`；键多一个少一个、`to` 为空 / 非字符串 / 含 `\\r` `\\n`、
  `changed_at` 读不出、`frontend_base_url` 为空时返回 `None`，日志不含 payload 值；
- **邮件接入 outbox**（integration）：`recover` 领取并投递一行，投递后 `SENT`、payload 清空。

「链接对得上前端」在 test_password_reset_link.py；「邮件类型进名单」的集合断言在 test_outbox.py。
I5 只有渲染、没有生产者（I6 才在重置成功的同一事务里写这一行），所以这里的 outbox 行按设计 §2 的
字段直接插入。只在 SQLite 上跑：投递路径与方言无关。时间一律注入，不 sleep。示例邮箱都是虚构的。
"""

from __future__ import annotations

import datetime as dt
import json
import logging

import pytest
from sqlalchemy import create_engine

from app.core.config import Settings
from app.core.database import create_session_factory
from app.core.email_templates import TEMPLATE_PASSWORD_CHANGED, render_email
from app.core.notification_adapter import Channel, EmailAdapter
from app.models.auth import DomainOutbox, OutboxStatus
from app.models.base import Base
from app.services.password_reset import AGGREGATE_USERS, EVENT_PASSWORD_CHANGED
from app.tasks import outbox as outbox_task
from app.tasks.outbox import _render_password_changed

BASE_URL = "https://billing.example.com"
SUBJECT = "Your Acuven Billing password was changed"

# 与 I6 写进 payload 的形状相同：不带时区的 UTC、整秒、ISO 8601。
# 2026-12-31 16:30 UTC 在马来西亚已是次年 1 月 1 日。
PAYLOAD = {"to": "admin@contoh.example.com", "changed_at": "2026-12-31T16:30:00"}
CHANGED_AT_SHOWN = "2027-01-01 00:30 Malaysia time, UTC+8 (2026-12-31 16:30 UTC)"

# 设计 §2「改密通知」的代码块，逐字写出：段落之间一个空行，末尾无换行。
EXPECTED_BODY = (
    "The password for your Acuven Billing account was changed at "
    f"{CHANGED_AT_SHOWN}.\n"
    "For your security, every signed-in session has been signed out.\n"
    "\n"
    "If you made this change, you do not need to do anything.\n"
    "\n"
    "If you did not make this change, someone else may have access to your email.\n"
    "Reset your password again straight away from this page, and contact Acuven:\n"
    "https://billing.example.com/forgot-password"
)


@pytest.fixture
def settings() -> Settings:
    return Settings(
        frontend_base_url=BASE_URL,
        outbox_retry_base_seconds=60,
        outbox_max_attempts=2,
    )


class FakeTransport:
    """Records what it was asked to send (the SMTP stand-in of test_receipt_email.py)."""

    def __init__(self) -> None:
        self.sent: list[object] = []

    def send(self, message) -> bool:  # noqa: ANN001 - 形状由 EmailTransport 协议定
        self.sent.append(message)
        return True


def send(message) -> object:  # noqa: ANN001
    """Through the real `EmailAdapter`: what would go out over SMTP."""
    fake = FakeTransport()
    assert EmailAdapter(fake).send(message).accepted
    [email] = fake.sent
    return email


def outbox_logs(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [record for record in caplog.records if record.name == outbox_task.__name__]


# --- 模板 -----------------------------------------------------------------------------------


def test_the_template_has_a_fixed_subject_and_exactly_the_design_params() -> None:
    params = {"changed_at": "T", "link": "L"}
    rendered = render_email(TEMPLATE_PASSWORD_CHANGED, params)

    assert rendered is not None
    assert rendered.subject == SUBJECT
    assert TEMPLATE_PASSWORD_CHANGED == "password_changed"
    for missing in params:
        partial = {key: value for key, value in params.items() if key != missing}
        assert render_email(TEMPLATE_PASSWORD_CHANGED, partial) is None
    assert render_email(TEMPLATE_PASSWORD_CHANGED, {**params, "to": "X"}) is None


# --- 渲染 -----------------------------------------------------------------------------------


def test_the_password_changed_email_is_rendered_word_for_word(settings: Settings) -> None:
    """经渲染函数、真实 `EmailAdapter` 与记录型假 transport。"""
    message = _render_password_changed(settings, PAYLOAD)
    assert message is not None
    assert message.channel is Channel.EMAIL
    assert message.template == TEMPLATE_PASSWORD_CHANGED
    assert message.params == {
        "changed_at": CHANGED_AT_SHOWN,
        "link": f"{BASE_URL}/forgot-password",
    }

    email = send(message)

    assert (email.to, email.subject, email.body) == (PAYLOAD["to"], SUBJECT, EXPECTED_BODY)
    # 标题与正文都不含收件人：信里不写邮箱（设计 §6）。
    assert PAYLOAD["to"] not in email.subject
    assert PAYLOAD["to"] not in email.body


def test_the_link_trims_the_configured_base(settings: Settings) -> None:
    """与其他邮件同一个基址取法：`strip().rstrip("/")`。"""
    padded = _render_password_changed(Settings(frontend_base_url=f"  {BASE_URL}/  "), PAYLOAD)

    assert padded is not None
    assert padded.params["link"] == f"{BASE_URL}/forgot-password"


SECRET = {"to": "hidden-recipient@secret.example.com", "changed_at": "2026-11-08T08:47:00"}
LEAKS = ("hidden-recipient", "secret.example.com", "08:47", "2026-11-08")

INVALID = {
    "extra-key": {**SECRET, "user_id": 8765},
    "missing-to": {"changed_at": SECRET["changed_at"]},
    "missing-changed-at": {"to": SECRET["to"]},
    "empty-recipient": {**SECRET, "to": ""},
    "number-recipient": {**SECRET, "to": 8765},
    "newline-in-recipient": {**SECRET, "to": "hidden-recipient@secret.example.com\nBcc: x@y.z"},
    "carriage-return-in-recipient": {**SECRET, "to": "hidden-recipient@secret.example.com\r"},
    "unreadable-changed-at": {**SECRET, "changed_at": "2026-11-08 at 08:47 sharp"},
    "empty-changed-at": {**SECRET, "changed_at": ""},
    "null-changed-at": {**SECRET, "changed_at": None},
    "number-changed-at": {**SECRET, "changed_at": 20261108},
}

_STANDARD_RECORD_KEYS = set(logging.makeLogRecord({}).__dict__) | {"message", "asctime"}


def _assert_nothing_leaked(caplog: pytest.LogCaptureFixture) -> None:
    records = outbox_logs(caplog)
    assert any(record.levelno == logging.ERROR for record in records), "每种失败记一条 error"
    for record in records:
        # 只查消息与 extra 字段：`created` / `msecs` 这类时间戳可能碰巧含示例值。
        extra = {k: v for k, v in record.__dict__.items() if k not in _STANDARD_RECORD_KEYS}
        text = f"{record.getMessage()} {extra!r}"
        for leak in LEAKS:
            assert leak not in text


@pytest.mark.parametrize("payload", list(INVALID.values()), ids=list(INVALID))
def test_an_unusable_payload_renders_nothing_and_logs_no_values(
    settings: Settings, caplog: pytest.LogCaptureFixture, payload: dict[str, object]
) -> None:
    with caplog.at_level(logging.DEBUG, logger=outbox_task.__name__):
        assert _render_password_changed(settings, payload) is None

    _assert_nothing_leaked(caplog)


@pytest.mark.parametrize("base", ["", "   "])
def test_without_a_front_end_base_url_nothing_is_rendered(
    caplog: pytest.LogCaptureFixture, base: str
) -> None:
    with caplog.at_level(logging.DEBUG, logger=outbox_task.__name__):
        assert _render_password_changed(Settings(frontend_base_url=base), SECRET) is None

    _assert_nothing_leaked(caplog)


def test_the_valid_secret_payload_does_render(settings: Settings) -> None:
    """上面的失败用例由合法 payload 改一处得来：合法的那份本身能渲染，失败才有区分力。"""
    assert _render_password_changed(settings, SECRET) is not None


# --- 邮件接入 outbox ------------------------------------------------------------------------

# 写入时刻与投递时钟都是固定值，注入而不是读系统时钟。
CHANGED = dt.datetime(2026, 11, 10, 6, 5, 9)
DELIVER_NOW = CHANGED + dt.timedelta(minutes=1)


def test_the_password_changed_email_type_is_handled() -> None:
    assert EVENT_PASSWORD_CHANGED == "PASSWORD_CHANGED_EMAIL_REQUESTED"
    assert EVENT_PASSWORD_CHANGED in outbox_task.handled_event_types()
    assert outbox_task._RENDERERS[EVENT_PASSWORD_CHANGED] is _render_password_changed


@pytest.fixture
def wired(monkeypatch, settings: Settings):
    """In-memory SQLite; the outbox task points at it, with these settings and a fixed clock
    (see test_outbox.py for why `_session_factory` is replaced)."""
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    monkeypatch.setattr(outbox_task, "_session_factory", lambda: factory)
    monkeypatch.setattr(outbox_task, "get_settings", lambda: settings)
    monkeypatch.setattr(outbox_task, "utc_now", lambda: DELIVER_NOW)
    yield factory
    engine.dispose()


def add_row(factory, payload: dict[str, object]) -> int:
    """The row I6 will write in the reset transaction (design §2 「改密通知」「写入」)."""
    with factory() as session:
        row = DomainOutbox(
            event_type=EVENT_PASSWORD_CHANGED,
            aggregate_type=AGGREGATE_USERS,
            aggregate_id="42",
            payload_json=json.dumps(payload, sort_keys=True),
            status=OutboxStatus.PENDING,
            attempt_count=0,
            next_retry_at=CHANGED,
            created_at=CHANGED,
        )
        session.add(row)
        session.commit()
        return int(row.id)


def read(factory, outbox_id: int) -> DomainOutbox:
    with factory() as session:
        row = session.get(DomainOutbox, outbox_id)
        assert row is not None
        session.expunge(row)
        return row


def test_recover_claims_and_delivers_a_password_changed_row(wired, monkeypatch) -> None:
    """设计 §7「邮件接入 outbox」：`recover` 领取 → `deliver` 经 SMTP 替身发出 →
    `SENT`、payload 清空。"""
    transport = FakeTransport()
    monkeypatch.setattr(
        outbox_task, "build_adapter", lambda _channel, _settings: EmailAdapter(transport)
    )
    outbox_id = add_row(wired, {"to": "admin@example.com", "changed_at": CHANGED.isoformat()})
    queued: list[int] = []
    monkeypatch.setattr(outbox_task.deliver, "delay", queued.append)

    assert outbox_task.recover() == 1
    assert queued == [outbox_id]
    assert outbox_task.deliver(outbox_id) == "sent"

    [email] = transport.sent
    assert email.to == "admin@example.com"
    assert email.subject == SUBJECT
    assert email.body.startswith(
        "The password for your Acuven Billing account was changed at "
        "2026-11-10 14:05 Malaysia time, UTC+8 (2026-11-10 06:05 UTC).\n"
    )
    row = read(wired, outbox_id)
    assert row.status is OutboxStatus.SENT
    assert row.attempt_count == 1
    assert row.processed_at is not None
    assert row.payload_json is None

    # 已结算的行不再被领取。
    queued.clear()
    assert outbox_task.recover() == 0
    assert queued == []


def test_an_unrenderable_row_counts_as_one_failure_not_an_immediate_dead_letter(
    wired, monkeypatch
) -> None:
    """渲染返回 `None` 按现有规则算一次失败（设计 §2「渲染」）：先退避重试，到上限才死信。"""
    transport = FakeTransport()
    monkeypatch.setattr(
        outbox_task, "build_adapter", lambda _channel, _settings: EmailAdapter(transport)
    )
    outbox_id = add_row(wired, {"to": "admin@example.com", "changed_at": "not a moment"})

    assert outbox_task.deliver(outbox_id) == "retry-later"

    row = read(wired, outbox_id)
    assert transport.sent == []
    assert row.status is OutboxStatus.PENDING
    assert row.attempt_count == 1
    assert row.next_retry_at > DELIVER_NOW
    assert row.payload_json is not None

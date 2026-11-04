"""Low balance, suspended and reactivated emails: rendering and delivery (design gate #559 v2).

设计 §7 标注 152 的各行：渲染逐字、显示金额、渲染失败、链接守卫、投递正常路径、
投递失败到死信、邮件结算带领取次数的两种交错。
「邮件类型进名单」在 test_outbox.py 的集合断言里。

本任务不生成 `NOTIFICATION_EMAIL_REQUESTED` 行（生产者是 AIH-TASK-153），
这里的行都由测试自己插入。示例公司名、邮箱与金额都是虚构的。
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlparse

import pytest
from sqlalchemy import create_engine, update

from app.core.config import Settings
from app.core.database import create_session_factory
from app.core.notification_adapter import EmailAdapter
from app.models.auth import DomainOutbox, OutboxStatus
from app.models.base import Base
from app.services.auth import utc_now
from app.services.password_reset import AGGREGATE_USERS, EVENT_PASSWORD_RESET
from app.tasks import outbox as outbox_task
from app.tasks.outbox import (
    EVENT_NOTIFICATION_EMAIL,
    _display_money,
    _render_notification_email,
)

PATHS_TS = Path(__file__).resolve().parents[2] / "frontend" / "src" / "routes" / "paths.ts"

BASE_URL = "https://billing.example.com"
ZERO_UUID = "00000000-0000-0000-0000-000000000000"

LOW = {
    "to": "ops@contoh.example.com",
    "company_name": "Contoh Sdn Bhd",
    "notification_type": "LOW_BALANCE",
    "balance": "45.12500000",
    "threshold": "50.00000000",
    "occurred_at": "2026-10-27T05:12:09",
}
SUSPENDED = {
    "to": "billing@asia-trading.example.com",
    # `$` 不会被再次解释：`substitute` 只替换模板里的占位符。
    "company_name": "A$ia Trading Sdn Bhd",
    "notification_type": "SUSPENDED",
    "balance": "-1234.56500000",
    "threshold": None,
    "occurred_at": "2026-12-31T20:30:00",
}
REACTIVATED = {
    "to": "ops@contoh.example.com",
    "company_name": "Contoh Sdn Bhd",
    "notification_type": "REACTIVATED",
    "balance": "5.00000000",
    "threshold": None,
    "occurred_at": "2026-11-04T00:00:00",
}

# 设计 §2「模板」三个代码块，按 §7 的约定逐字写出：段落之间一个空行，末尾无换行。
EXPECTED = {
    "LOW_BALANCE": (
        "ops@contoh.example.com",
        "Your Acuven Billing balance is low",
        "The prepaid balance of the Acuven Billing account for Contoh Sdn Bhd has fallen to "
        "RM 45.13, at or below your low-balance threshold of RM 50.00.\n"
        "\n"
        "This was recorded at 2026-10-27 13:12 Malaysia time, UTC+8 (2026-10-27 05:12 UTC).\n"
        "\n"
        "AI services billed to this account are suspended when the balance reaches RM 0.00. "
        "To top up, please contact your Acuven contact.\n"
        "\n"
        "You can check your balance in the customer portal:\n"
        "https://billing.example.com/portal/wallet",
    ),
    "SUSPENDED": (
        "billing@asia-trading.example.com",
        "Your Acuven Billing account is suspended",
        "The prepaid balance of the Acuven Billing account for A$ia Trading Sdn Bhd reached "
        "RM -1,234.57 at 2027-01-01 04:30 Malaysia time, UTC+8 (2026-12-31 20:30 UTC), "
        "so AI services billed to this account are now suspended.\n"
        "\n"
        "This balance suspension is lifted automatically once the balance is above RM 0.00. "
        "To top up, please contact your Acuven contact.\n"
        "\n"
        "You can check your balance in the customer portal:\n"
        "https://billing.example.com/portal/wallet",
    ),
    "REACTIVATED": (
        "ops@contoh.example.com",
        "Your Acuven Billing balance suspension has been lifted",
        "The prepaid balance of the Acuven Billing account for Contoh Sdn Bhd is RM 5.00 "
        "as of 2026-11-04 08:00 Malaysia time, UTC+8 (2026-11-04 00:00 UTC), "
        "so this account is no longer suspended for its balance.\n"
        "\n"
        "If AI services are still unavailable, the account or one of its projects may be "
        "disabled for another reason; please contact your Acuven contact.\n"
        "\n"
        "You can check your balance in the customer portal:\n"
        "https://billing.example.com/portal/wallet",
    ),
}


@pytest.fixture
def settings() -> Settings:
    return Settings(
        frontend_base_url=BASE_URL,
        outbox_retry_base_seconds=60,
        outbox_max_attempts=2,
    )


@pytest.fixture
def wired(monkeypatch, settings: Settings):
    """Point the task at an in-memory database and these settings (see test_outbox.py)."""
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    monkeypatch.setattr(outbox_task, "_session_factory", lambda: factory)
    monkeypatch.setattr(outbox_task, "get_settings", lambda: settings)
    return factory


class FakeTransport:
    """Records what it was asked to send, and can be told to fail."""

    def __init__(self, *, succeed: bool = True) -> None:
        self.succeed = succeed
        self.sent: list[object] = []

    def send(self, message) -> bool:  # noqa: ANN001 - 形状由 EmailTransport 协议定
        self.sent.append(message)
        return self.succeed


def use_transport(monkeypatch, fake: FakeTransport) -> FakeTransport:
    """Replace `build_adapter` with a real `EmailAdapter` around `fake`: 渲染路径是真的。"""
    monkeypatch.setattr(
        outbox_task, "build_adapter", lambda _channel, _settings: EmailAdapter(fake)
    )
    return fake


def add_row(factory, *, event_type: str, payload: dict[str, object], **overrides) -> int:
    now = utc_now()
    fields = {
        "event_type": event_type,
        "aggregate_type": "notification",
        "aggregate_id": ZERO_UUID,
        "payload_json": json.dumps(payload, sort_keys=True),
        "status": OutboxStatus.PENDING,
        "attempt_count": 0,
        "next_retry_at": now,
        "created_at": now,
        **overrides,
    }
    with factory() as session:
        row = DomainOutbox(**fields)
        session.add(row)
        session.commit()
        return int(row.id)


def add_notification_email(factory, payload: dict[str, object] | None = None, **overrides) -> int:
    return add_row(
        factory, event_type=EVENT_NOTIFICATION_EMAIL, payload=payload or LOW, **overrides
    )


def read(factory, outbox_id: int) -> DomainOutbox:
    with factory() as session:
        row = session.get(DomainOutbox, outbox_id)
        assert row is not None
        session.expunge(row)
        return row


def make_due(factory, outbox_id: int) -> None:
    with factory() as session:
        session.execute(
            update(DomainOutbox)
            .where(DomainOutbox.id == outbox_id)
            .values(next_retry_at=utc_now() - dt.timedelta(seconds=1))
        )
        session.commit()


def claim(factory, outbox_id: int):
    """Run the claim UPDATE of `deliver` directly (another worker taking the row)."""
    make_due(factory, outbox_id)
    row = read(factory, outbox_id)
    attempt = row.attempt_count + 1
    with factory() as session:
        claimed = session.execute(
            update(DomainOutbox)
            .where(
                DomainOutbox.id == outbox_id,
                DomainOutbox.status == OutboxStatus.PENDING,
                DomainOutbox.next_retry_at <= utc_now(),
                DomainOutbox.attempt_count == row.attempt_count,
            )
            .values(attempt_count=attempt, next_retry_at=utc_now() + dt.timedelta(minutes=5))
        )
        assert int(claimed.rowcount) == 1
        session.commit()
    return outbox_task._Claim(
        outbox_id=outbox_id,
        attempt=attempt,
        event_type=row.event_type,
        aggregate_type=row.aggregate_type,
        aggregate_id=row.aggregate_id,
        payload_json=row.payload_json,
        created_at=row.created_at,
    )


def outbox_logs(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [record for record in caplog.records if record.name == outbox_task.__name__]


# --- 渲染逐字 -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload", [LOW, SUSPENDED, REACTIVATED], ids=lambda p: p["notification_type"]
)
def test_each_notification_email_is_rendered_word_for_word(
    settings: Settings, payload: dict[str, object]
) -> None:
    """设计 §7「渲染逐字」：经渲染函数、真实 `EmailAdapter` 与记录型假 transport。"""
    message = _render_notification_email(settings, payload)
    assert message is not None
    fake = FakeTransport()

    assert EmailAdapter(fake).send(message).accepted

    [email] = fake.sent
    assert (email.to, email.subject, email.body) == EXPECTED[str(payload["notification_type"])]


@pytest.mark.parametrize(
    "payload", [LOW, SUSPENDED, REACTIVATED], ids=lambda p: p["notification_type"]
)
def test_notification_emails_speak_of_billing_status_not_service(
    settings: Settings, payload: dict[str, object]
) -> None:
    """设计 §2：复机后 AI 服务是否可用还看账户与项目状态，信里不说服务已恢复。"""
    message = _render_notification_email(settings, payload)
    assert message is not None
    fake = FakeTransport()
    EmailAdapter(fake).send(message)

    [email] = fake.sent
    for text in (email.subject, email.body):
        assert "resumed" not in text.lower()
        assert "active again" not in text.lower()
    # 标题固定：不含公司名或任何 payload 值。
    assert str(payload["company_name"]) not in email.subject
    assert "$" not in email.subject


def test_only_the_low_balance_email_carries_a_threshold(settings: Settings) -> None:
    low = _render_notification_email(settings, LOW)
    suspended = _render_notification_email(settings, SUSPENDED)
    reactivated = _render_notification_email(settings, REACTIVATED)
    assert low is not None and suspended is not None and reactivated is not None

    assert set(low.params) == {"company_name", "balance", "threshold", "occurred_at", "link"}
    assert set(suspended.params) == {"company_name", "balance", "occurred_at", "link"}
    assert set(reactivated.params) == {"company_name", "balance", "occurred_at", "link"}
    assert low.params["company_name"] == "Contoh Sdn Bhd"
    assert (low.params["balance"], low.params["threshold"]) == ("45.13", "50.00")


# --- 显示金额 -------------------------------------------------------------------------------


def test_display_money_rounds_half_up_with_thousands_separators() -> None:
    """设计 §7「显示金额」：spec §80，两位小数、HALF_UP、千分位，不出现 `-0.00`。"""
    # 用例有区分力：只靠 `format` 时，默认上下文的 HALF_EVEN 把 0.125 显示成 0.12。
    assert format(Decimal("0.125"), ".2f") == "0.12"

    examples = {
        "0.125": "0.13",
        "1234.56500000": "1,234.57",
        "-0.00400000": "0.00",
        "-12.50000000": "-12.50",
        "999999999999.99999999": "1,000,000,000,000.00",
    }
    for raw, shown in examples.items():
        assert _display_money(Decimal(raw)) == shown, raw


# --- 渲染失败 -------------------------------------------------------------------------------

SECRET = {
    "to": "hidden-recipient@secret.example.com",
    "company_name": "Undisclosed Holdings Sdn Bhd",
    "notification_type": "LOW_BALANCE",
    "balance": "7654.32100000",
    "threshold": "8765.43200000",
    "occurred_at": "2026-10-27T05:12:09",
}
SECRET_SUSPENDED = {**SECRET, "notification_type": "SUSPENDED", "threshold": None}
LEAKS = ("hidden-recipient", "secret.example.com", "Undisclosed", "7654", "7,654", "8765", "8,765")


def _without(payload: dict[str, object], key: str) -> dict[str, object]:
    return {name: value for name, value in payload.items() if name != key}


INVALID = {
    "empty-recipient": {**SECRET, "to": ""},
    "number-recipient": {**SECRET, "to": 12345},
    "newline-in-recipient": {**SECRET, "to": "hidden-recipient@secret.example.com\nBcc: x@y.z"},
    "carriage-return-in-recipient": {**SECRET, "to": "hidden-recipient@secret.example.com\r"},
    "empty-company": {**SECRET, "company_name": ""},
    "number-company": {**SECRET, "company_name": 7654},
    "unknown-type": {**SECRET, "notification_type": "OVERDRAWN"},
    "list-type": {**SECRET, "notification_type": ["LOW_BALANCE"]},
    "exponent-balance": {**SECRET, "balance": "1e3"},
    "number-balance": {**SECRET, "balance": 7654.321},
    "low-without-threshold": {**SECRET, "threshold": None},
    "low-negative-threshold": {**SECRET, "threshold": "-8765.43200000"},
    "suspended-with-threshold": {**SECRET_SUSPENDED, "threshold": "8765.43200000"},
    "reactivated-with-threshold": {
        **SECRET_SUSPENDED,
        "notification_type": "REACTIVATED",
        "threshold": "8765.43200000",
    },
    "unparseable-occurred-at": {**SECRET, "occurred_at": "yesterday at 7654"},
    "null-occurred-at": {**SECRET, "occurred_at": None},
    "extra-key": {**SECRET, "tenant_id": 7654},
    "missing-key": _without(SECRET, "threshold"),
    "missing-recipient": _without(SECRET, "to"),
}


def _assert_nothing_leaked(caplog: pytest.LogCaptureFixture) -> None:
    records = outbox_logs(caplog)
    assert any(record.levelno == logging.ERROR for record in records), "每种失败记一条 error"
    for record in records:
        text = f"{record.getMessage()} {record.__dict__!r}"
        for leak in LEAKS:
            assert leak not in text
    for leak in LEAKS:
        assert leak not in caplog.text


@pytest.mark.parametrize("payload", list(INVALID.values()), ids=list(INVALID))
def test_an_unusable_payload_renders_nothing_and_logs_no_values(
    settings: Settings, caplog: pytest.LogCaptureFixture, payload: dict[str, object]
) -> None:
    with caplog.at_level(logging.DEBUG, logger=outbox_task.__name__):
        assert _render_notification_email(settings, payload) is None

    _assert_nothing_leaked(caplog)


@pytest.mark.parametrize("base", ["", "   "])
def test_without_a_front_end_base_url_nothing_is_rendered(
    caplog: pytest.LogCaptureFixture, base: str
) -> None:
    with caplog.at_level(logging.DEBUG, logger=outbox_task.__name__):
        assert _render_notification_email(Settings(frontend_base_url=base), SECRET) is None

    _assert_nothing_leaked(caplog)


def test_the_valid_secret_payloads_do_render(settings: Settings) -> None:
    """上面的失败用例由合法 payload 改一个键得来：合法的那份本身能渲染，失败才有区分力。"""
    assert _render_notification_email(settings, SECRET) is not None
    assert _render_notification_email(settings, SECRET_SUSPENDED) is not None


# --- 链接守卫 -------------------------------------------------------------------------------


def frontend_portal_wallet_path() -> str:
    """从 `paths.ts` 取 `portalWallet` 字面量；取不到就失败。

    取法同 test_invitation_link.py，不从它导入：两份守卫互不依赖。
    """
    source = PATHS_TS.read_text(encoding="utf-8")
    match = re.search(r'portalWallet:\s*"([^"]+)"', source)
    assert match is not None, f"ROUTES.portalWallet is no longer declared in {PATHS_TS.name}"
    return match.group(1)


def rendered_link(settings: Settings) -> str:
    """后端真的会发出去的那封信里的那条链接。"""
    message = _render_notification_email(settings, LOW)
    assert message is not None
    fake = FakeTransport()
    EmailAdapter(fake).send(message)
    [email] = fake.sent
    match = re.search(r"(https://\S+)", email.body)
    assert match is not None, "the notification email no longer contains a link"
    return match.group(1)


def test_the_wallet_link_lands_on_the_route_the_frontend_registers(settings: Settings) -> None:
    link = urlparse(rendered_link(settings))

    assert link.path == frontend_portal_wallet_path()
    assert link.query == ""
    assert f"{link.scheme}://{link.netloc}" == BASE_URL


def test_the_wallet_link_trims_the_configured_base(settings: Settings) -> None:
    """与重置信同一个基址取法：`strip().rstrip("/")`。"""
    padded = Settings(frontend_base_url=f"  {BASE_URL}/  ")

    assert rendered_link(padded) == rendered_link(settings) == f"{BASE_URL}/portal/wallet"


# --- 投递 -----------------------------------------------------------------------------------


def test_a_notification_email_row_is_recovered_and_sent(wired, monkeypatch) -> None:
    """设计 §7「投递正常路径」：手工插入一行，跑 `recover` 与 `deliver`。"""
    transport = use_transport(monkeypatch, FakeTransport())
    outbox_id = add_notification_email(wired)
    queued: list[int] = []
    monkeypatch.setattr(outbox_task.deliver, "delay", lambda i: queued.append(i))

    assert outbox_task.recover() == 1
    assert queued == [outbox_id]
    assert outbox_task.deliver(outbox_id) == "sent"

    [email] = transport.sent
    assert (email.to, email.subject, email.body) == EXPECTED["LOW_BALANCE"]
    row = read(wired, outbox_id)
    assert row.status is OutboxStatus.SENT
    assert row.processed_at is not None
    assert row.payload_json is None


def test_a_failing_notification_email_ends_as_a_dead_letter(
    wired, monkeypatch, caplog: pytest.LogCaptureFixture
) -> None:
    """设计 §7「投递失败到死信」：transport 恒失败，`outbox_max_attempts` 2。"""
    use_transport(monkeypatch, FakeTransport(succeed=False))
    outbox_id = add_notification_email(wired)

    with caplog.at_level(logging.WARNING, logger=outbox_task.__name__):
        first = outbox_task.deliver(outbox_id)
        make_due(wired, outbox_id)
        second = outbox_task.deliver(outbox_id)

    assert (first, second) == ("retry-later", "dead-lettered")
    row = read(wired, outbox_id)
    assert row.status is OutboxStatus.FAILED
    assert row.payload_json is None
    assert any(
        record.levelno == logging.ERROR and "Giving up" in record.getMessage()
        for record in outbox_logs(caplog)
    )


# --- 邮件结算带领取次数（设计 §2「邮件类结算带上领取次数」，§4） --------------------------------

RESET_PAYLOAD = {"to": "admin@example.com", "token": "a-token-that-looks-like-the-real-thing"}


def add_email_row(factory, kind: str) -> int:
    """A row already tried once, so the next claim is attempt 2 (the last one)."""
    if kind == "password-reset":
        return add_row(
            factory,
            event_type=EVENT_PASSWORD_RESET,
            payload=RESET_PAYLOAD,
            aggregate_type=AGGREGATE_USERS,
            aggregate_id="1",
            attempt_count=1,
        )
    return add_notification_email(factory, attempt_count=1)


def b_claims_during_the_send(monkeypatch, factory, outbox_id: int) -> list[object]:
    """While A is sending, worker B claims the same row (A paused past its backoff)."""
    real_send = outbox_task._attempt_send
    newer: list[object] = []

    def send_then_b_claims(*args, **kwargs) -> bool:
        sent = real_send(*args, **kwargs)
        newer.append(claim(factory, outbox_id))
        return sent

    monkeypatch.setattr(outbox_task, "_attempt_send", send_then_b_claims)
    return newer


@pytest.mark.parametrize("kind", ["password-reset", "notification-email"])
def test_an_old_claimer_failing_last_does_not_dead_letter_a_newer_claim(
    wired, monkeypatch, caplog: pytest.LogCaptureFixture, kind: str
) -> None:
    """A 以 2 领取、发信失败走死信；期间 B 以 3 领取：A `superseded`、不记 error、行不变；
    B 发信成功后结算为 `SENT`。"""
    use_transport(monkeypatch, FakeTransport(succeed=False))
    outbox_id = add_email_row(wired, kind)
    newer = b_claims_during_the_send(monkeypatch, wired, outbox_id)

    with caplog.at_level(logging.WARNING, logger=outbox_task.__name__):
        assert outbox_task.deliver(outbox_id) == "superseded"

    assert not any(record.levelno >= logging.ERROR for record in outbox_logs(caplog))
    [claim_b] = newer
    assert claim_b.attempt == 3
    row = read(wired, outbox_id)
    assert (row.status, row.attempt_count) == (OutboxStatus.PENDING, 3)
    assert row.payload_json is not None, "B 还要用它发信"

    assert outbox_task._settle(wired, claim_b, status=OutboxStatus.SENT) is True

    row = read(wired, outbox_id)
    assert row.status is OutboxStatus.SENT
    assert row.payload_json is None


@pytest.mark.parametrize("kind", ["password-reset", "notification-email"])
def test_an_old_claimer_succeeding_late_leaves_the_row_to_the_newer_claim(
    wired, monkeypatch, kind: str
) -> None:
    """A 发信成功，但期间 B 已领取：A 的结算受影响 0 行（仍返回 `sent`），行由 B 结算。"""
    use_transport(monkeypatch, FakeTransport())
    outbox_id = add_email_row(wired, kind)
    newer = b_claims_during_the_send(monkeypatch, wired, outbox_id)
    real_settle = outbox_task._settle
    settled: list[bool] = []

    def recording_settle(*args, **kwargs) -> bool:
        result = real_settle(*args, **kwargs)
        settled.append(result)
        return result

    monkeypatch.setattr(outbox_task, "_settle", recording_settle)

    assert outbox_task.deliver(outbox_id) == "sent"

    assert settled == [False]
    [claim_b] = newer
    row = read(wired, outbox_id)
    assert (row.status, row.attempt_count) == (OutboxStatus.PENDING, 3)
    assert row.payload_json is not None

    assert real_settle(wired, claim_b, status=OutboxStatus.SENT) is True

    row = read(wired, outbox_id)
    assert row.status is OutboxStatus.SENT
    assert row.payload_json is None


def test_a_single_claimer_settles_as_before(wired, monkeypatch) -> None:
    """没有交错时 `_settle` 受影响 1 行，与改动前相同。"""
    use_transport(monkeypatch, FakeTransport())
    outbox_id = add_email_row(wired, "password-reset")
    real_settle = outbox_task._settle
    settled: list[bool] = []

    def recording_settle(*args, **kwargs) -> bool:
        result = real_settle(*args, **kwargs)
        settled.append(result)
        return result

    monkeypatch.setattr(outbox_task, "_settle", recording_settle)

    assert outbox_task.deliver(outbox_id) == "sent"
    assert settled == [True]
    assert read(wired, outbox_id).status is OutboxStatus.SENT

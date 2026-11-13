"""Payment received emails: rendering and delivery (design gate #589 v2, AIH-TASK-198).

设计 §7 标 198 的两行：

- **付款邮件**（unit）：payload 合规时标题固定、正文与 §2 逐字一致（金额千分位、MYT 时刻）；
  每一条不合规返回 `None`、日志不带 payload 值；链接路径与前端 `paths.ts` 登记的
  `portalPaymentResult` 一致；
- **邮件接入 outbox**（integration）：`handled_event_types()` 含新类型；`recover` 领取并投递一行
  （行由真的 `issue_receipt` 写），投递后 `SENT`、payload 清空。

「邮件类型进名单」的集合断言在 test_outbox.py。支付与 TOPUP 账本行用共用的 payment_world.py
（`paid_payment_maker` 在 conftest.py 注册）；本文件其余夹具只在这里用，定义在这里。
只在 SQLite 上跑：投递路径与方言无关，开具在真 MySQL 上的行为由 test_receipts.py 覆盖。
时间一律注入，不 sleep。示例公司名、邮箱与金额都是虚构的。
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
from collections.abc import Callable, Iterator
from pathlib import Path
from urllib.parse import urlparse

import pytest
from payment_world import (
    CREATED,
    PaymentRef,
    World,
    build_sqlite_world,
    insert_tax_version,
    seed_receipt_series,
)
from sqlalchemy import delete, select

from app.core.config import Settings
from app.core.email_templates import TEMPLATE_PAYMENT_RECEIPT, render_email
from app.core.notification_adapter import EmailAdapter
from app.models.auth import DomainOutbox, OutboxStatus
from app.models.payments import Payment
from app.services.receipts import AGGREGATE_PAYMENT_RECEIPT, EVENT_RECEIPT_EMAIL, issue_receipt
from app.tasks import outbox as outbox_task
from app.tasks.outbox import _render_receipt_email

PATHS_TS = Path(__file__).resolve().parents[2] / "frontend" / "src" / "routes" / "paths.ts"

BASE_URL = "https://billing.example.com"
PAYMENT_ID = "0f8e2b7c-3d4a-4b5c-9d6e-7f8091a2b3c4"
SUBJECT = "We have received your Acuven Billing payment"

PAYLOAD = {
    "to": "ops@contoh.example.com",
    # `$` 不会被再次解释：`substitute` 只替换模板里的占位符。
    "company_name": "Contoh $ Sdn Bhd",
    "receipt_number": "ACV-AIR-2026-000123",
    "amount": "12345.60",
    "paid_at": "2026-12-31T16:30:00",
    "payment_id": PAYMENT_ID,
}

# 设计 §2「付款邮件」的代码块，逐字写出：段落之间一个空行，末尾无换行。
# 2026-12-31 16:30 UTC 在马来西亚已是次年 1 月 1 日。
EXPECTED_BODY = (
    "We have received your payment of RM 12,345.60 for the Acuven Billing account of "
    "Contoh $ Sdn Bhd, made at 2027-01-01 00:30 Malaysia time, UTC+8 (2026-12-31 16:30 UTC).\n"
    "It has been added to your prepaid balance.\n"
    "\n"
    "Receipt number: ACV-AIR-2026-000123\n"
    "\n"
    "You can view this payment and download the receipt in the customer portal:\n"
    f"https://billing.example.com/portal/payments/{PAYMENT_ID}"
)


@pytest.fixture
def settings() -> Settings:
    return Settings(
        frontend_base_url=BASE_URL,
        outbox_retry_base_seconds=60,
        outbox_max_attempts=2,
    )


class FakeTransport:
    """Records what it was asked to send (the SMTP stand-in of test_notification_email.py)."""

    def __init__(self, *, succeed: bool = True) -> None:
        self.succeed = succeed
        self.sent: list[object] = []

    def send(self, message) -> bool:  # noqa: ANN001 - 形状由 EmailTransport 协议定
        self.sent.append(message)
        return self.succeed


def send(message) -> object:  # noqa: ANN001
    """Through the real `EmailAdapter`: what would go out over SMTP."""
    fake = FakeTransport()
    assert EmailAdapter(fake).send(message).accepted
    [email] = fake.sent
    return email


def outbox_logs(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [record for record in caplog.records if record.name == outbox_task.__name__]


# --- 模板与渲染逐字 -------------------------------------------------------------------------


def test_the_template_has_a_fixed_subject_and_exactly_the_design_params() -> None:
    params = {
        "amount": "1.00",
        "company_name": "X",
        "paid_at": "T",
        "receipt_number": "R",
        "link": "L",
    }
    rendered = render_email(TEMPLATE_PAYMENT_RECEIPT, params)

    assert rendered is not None
    assert rendered.subject == SUBJECT
    for missing in params:
        partial = {key: value for key, value in params.items() if key != missing}
        assert render_email(TEMPLATE_PAYMENT_RECEIPT, partial) is None
    assert render_email(TEMPLATE_PAYMENT_RECEIPT, {**params, "payment_id": "P"}) is None


def test_the_payment_email_is_rendered_word_for_word(settings: Settings) -> None:
    """设计 §7「付款邮件」：经渲染函数、真实 `EmailAdapter` 与记录型假 transport。"""
    message = _render_receipt_email(settings, PAYLOAD)
    assert message is not None

    email = send(message)

    assert (email.to, email.subject, email.body) == (
        "ops@contoh.example.com",
        SUBJECT,
        EXPECTED_BODY,
    )
    assert message.template == TEMPLATE_PAYMENT_RECEIPT
    # 标题固定：不含任何 payload 值。
    for value in PAYLOAD.values():
        assert value not in email.subject


def test_the_amount_is_shown_half_up_with_thousands_separators(settings: Settings) -> None:
    for raw, shown in {"0.01": "0.01", "1000.00": "1,000.00", "999999.99": "999,999.99"}.items():
        message = _render_receipt_email(settings, {**PAYLOAD, "amount": raw})
        assert message is not None, raw
        assert message.params["amount"] == shown


# --- 渲染失败 -------------------------------------------------------------------------------

SECRET = {
    "to": "hidden-recipient@secret.example.com",
    "company_name": "Undisclosed Holdings Sdn Bhd",
    "receipt_number": "ACV-AIR-2026-007654",
    "amount": "8765.43",
    "paid_at": "2026-11-08T08:00:00",
    "payment_id": "c0ffee00-1111-4222-8333-444455556666",
}
LEAKS = (
    "hidden-recipient",
    "secret.example.com",
    "Undisclosed",
    "007654",
    "8765",
    "8,765",
    "c0ffee00",
)


def _without(payload: dict[str, object], key: str) -> dict[str, object]:
    return {name: value for name, value in payload.items() if name != key}


INVALID = {
    "extra-key": {**SECRET, "tenant_id": 8765},
    "missing-key": _without(SECRET, "payment_id"),
    "empty-recipient": {**SECRET, "to": ""},
    "number-recipient": {**SECRET, "to": 8765},
    "newline-in-recipient": {**SECRET, "to": "hidden-recipient@secret.example.com\nBcc: x@y.z"},
    "carriage-return-in-recipient": {**SECRET, "to": "hidden-recipient@secret.example.com\r"},
    "empty-company": {**SECRET, "company_name": ""},
    "number-company": {**SECRET, "company_name": 8765},
    "short-receipt-number": {**SECRET, "receipt_number": "ACV-AIR-2026-7654"},
    "other-prefix-receipt-number": {**SECRET, "receipt_number": "ACV-XYZ-2026-007654"},
    "trailing-receipt-number": {**SECRET, "receipt_number": "ACV-AIR-2026-007654\n"},
    "zero-amount": {**SECRET, "amount": "0.00"},
    "negative-amount": {**SECRET, "amount": "-8765.43"},
    "unreadable-amount": {**SECRET, "amount": "8765,43"},
    "exponent-amount": {**SECRET, "amount": "8.76543e3"},
    "number-amount": {**SECRET, "amount": 8765.43},
    "unreadable-paid-at": {**SECRET, "paid_at": "yesterday at 8765"},
    "null-paid-at": {**SECRET, "paid_at": None},
    "payment-id-not-uuid": {**SECRET, "payment_id": "c0ffee00"},
    "uppercase-payment-id": {**SECRET, "payment_id": "C0FFEE00-1111-4222-8333-444455556666"},
    "payment-id-with-path": {**SECRET, "payment_id": "c0ffee00-1111-4222-8333-444455556666/x"},
}


_STANDARD_RECORD_KEYS = set(logging.makeLogRecord({}).__dict__) | {"message", "asctime"}


def _assert_nothing_leaked(caplog: pytest.LogCaptureFixture) -> None:
    records = outbox_logs(caplog)
    assert any(record.levelno == logging.ERROR for record in records), "每种失败记一条 error"
    for record in records:
        # 只查消息与 extra 字段：`created` / `msecs` 这类时间戳碰巧含 "8765" 会误报。
        extra = {k: v for k, v in record.__dict__.items() if k not in _STANDARD_RECORD_KEYS}
        text = f"{record.getMessage()} {extra!r}"
        for leak in LEAKS:
            assert leak not in text
    for leak in LEAKS:
        assert leak not in caplog.text


@pytest.mark.parametrize("payload", list(INVALID.values()), ids=list(INVALID))
def test_an_unusable_payload_renders_nothing_and_logs_no_values(
    settings: Settings, caplog: pytest.LogCaptureFixture, payload: dict[str, object]
) -> None:
    with caplog.at_level(logging.DEBUG, logger=outbox_task.__name__):
        assert _render_receipt_email(settings, payload) is None

    _assert_nothing_leaked(caplog)


@pytest.mark.parametrize("base", ["", "   ", "/"])
def test_without_a_front_end_base_url_nothing_is_rendered(
    caplog: pytest.LogCaptureFixture, base: str
) -> None:
    with caplog.at_level(logging.DEBUG, logger=outbox_task.__name__):
        assert _render_receipt_email(Settings(frontend_base_url=base), SECRET) is None

    _assert_nothing_leaked(caplog)


def test_the_valid_secret_payload_does_render(settings: Settings) -> None:
    """上面的失败用例由合法 payload 改一处得来：合法的那份本身能渲染，失败才有区分力。"""
    assert _render_receipt_email(settings, SECRET) is not None


# --- 链接守卫 -------------------------------------------------------------------------------


def frontend_payment_result_route() -> str:
    """从 `paths.ts` 取 `portalPaymentResult` 字面量；取不到就失败。

    取法同 test_notification_email.py 的 `portalWallet`，不从它导入：两份守卫互不依赖。
    """
    source = PATHS_TS.read_text(encoding="utf-8")
    match = re.search(r'portalPaymentResult:\s*"([^"]+)"', source)
    assert match is not None, f"ROUTES.portalPaymentResult is no longer declared in {PATHS_TS.name}"
    return match.group(1)


def rendered_link(settings: Settings) -> str:
    """后端真的会发出去的那封信里的那条链接。"""
    message = _render_receipt_email(settings, PAYLOAD)
    assert message is not None
    match = re.search(r"(https://\S+)", send(message).body)
    assert match is not None, "the payment email no longer contains a link"
    return match.group(1)


def test_the_payment_link_lands_on_the_route_the_frontend_registers(settings: Settings) -> None:
    route = frontend_payment_result_route()
    # 路由恰好一个参数（支付的对外 id），代入后应与信里的路径相同。
    assert re.findall(r":\w+", route) == [":paymentId"]
    link = urlparse(rendered_link(settings))

    assert link.path == route.replace(":paymentId", PAYMENT_ID)
    assert link.query == ""
    assert link.fragment == ""
    assert f"{link.scheme}://{link.netloc}" == BASE_URL


def test_the_payment_link_trims_the_configured_base(settings: Settings) -> None:
    """与其他邮件同一个基址取法：`strip().rstrip("/")`。"""
    padded = Settings(frontend_base_url=f"  {BASE_URL}/  ")

    assert (
        rendered_link(padded)
        == rendered_link(settings)
        == f"{BASE_URL}/portal/payments/{PAYMENT_ID}"
    )


# --- 邮件接入 outbox ------------------------------------------------------------------------

# 开具时刻与投递时钟都是固定值，注入而不是读系统时钟。
ISSUE_NOW = dt.datetime(2026, 11, 10, 6, 5, 9)
DELIVER_NOW = ISSUE_NOW + dt.timedelta(minutes=1)


def test_the_payment_email_type_is_handled() -> None:
    assert EVENT_RECEIPT_EMAIL == "PAYMENT_RECEIPT_EMAIL_REQUESTED"
    assert EVENT_RECEIPT_EMAIL in outbox_task.handled_event_types()
    assert outbox_task._RENDERERS[EVENT_RECEIPT_EMAIL] is _render_receipt_email


@pytest.fixture
def world(monkeypatch, settings: Settings) -> Iterator[World]:
    """In-memory SQLite with the 74 series rows; the outbox task points at it, with these
    settings and a fixed clock (see test_outbox.py for why `_session_factory` is replaced)."""
    for built in build_sqlite_world():
        seed_receipt_series(built.factory)
        monkeypatch.setattr(outbox_task, "_session_factory", lambda factory=built.factory: factory)
        monkeypatch.setattr(outbox_task, "get_settings", lambda: settings)
        monkeypatch.setattr(outbox_task, "utc_now", lambda: DELIVER_NOW)
        yield built


def issue(world: World, payment: PaymentRef) -> str:
    """What the confirmation path does: lock the payment, issue with email, commit."""
    with world.factory() as session:
        row = session.execute(
            select(Payment).where(Payment.public_id == payment.public_id).with_for_update()
        ).scalar_one()
        issued = issue_receipt(
            session, payment=row, payment_method="fpx", now=ISSUE_NOW, send_email=True
        )
        session.commit()
    assert issued is not None
    return issued.public_id


def receipt_email_rows(world: World) -> list[DomainOutbox]:
    with world.factory() as session:
        rows = list(
            session.execute(
                select(DomainOutbox)
                .where(DomainOutbox.event_type == EVENT_RECEIPT_EMAIL)
                .order_by(DomainOutbox.id)
            ).scalars()
        )
        session.expunge_all()
        return rows


def test_recover_claims_and_delivers_the_row_issue_receipt_wrote(
    world: World, paid_payment_maker: Callable[..., PaymentRef], monkeypatch
) -> None:
    """设计 §7「邮件接入 outbox」：开具写行 → `recover` 领取 → `deliver` 经 SMTP 替身发出 →
    `SENT`、payload 清空。"""
    transport = FakeTransport()
    monkeypatch.setattr(
        outbox_task, "build_adapter", lambda _channel, _settings: EmailAdapter(transport)
    )
    tenant = world.customer()
    payment = paid_payment_maker(
        world, tenant, tax_version_id=insert_tax_version(world), amount="1234.50"
    )
    # 造 TOPUP 账本行时写下的租户事件也是 `recover` 会领的类型；清掉，本例只看付款邮件这一行。
    with world.factory() as session:
        session.execute(delete(DomainOutbox))
        session.commit()
    receipt_id = issue(world, payment)
    [pending] = receipt_email_rows(world)
    assert (pending.aggregate_type, pending.aggregate_id) == (AGGREGATE_PAYMENT_RECEIPT, receipt_id)
    assert pending.status is OutboxStatus.PENDING
    assert set(json.loads(pending.payload_json or "")) == set(PAYLOAD)

    queued: list[int] = []
    monkeypatch.setattr(outbox_task.deliver, "delay", queued.append)

    assert outbox_task.recover() == 1
    assert queued == [pending.id]
    assert outbox_task.deliver(pending.id) == "sent"

    [email] = transport.sent
    assert email.to == "ops@example.com"
    assert email.subject == SUBJECT
    # 支付在 `CREATED`（2026-11-08 08:00 UTC）付款。
    assert CREATED == dt.datetime(2026, 11, 8, 8, 0, 0)
    assert email.body == (
        "We have received your payment of RM 1,234.50 for the Acuven Billing account of "
        "Payment Confirm Test Sdn Bhd, made at "
        "2026-11-08 16:00 Malaysia time, UTC+8 (2026-11-08 08:00 UTC).\n"
        "It has been added to your prepaid balance.\n"
        "\n"
        "Receipt number: ACV-AIR-2026-000001\n"
        "\n"
        "You can view this payment and download the receipt in the customer portal:\n"
        f"{BASE_URL}/portal/payments/{payment.public_id}"
    )
    [row] = receipt_email_rows(world)
    assert row.status is OutboxStatus.SENT
    assert row.attempt_count == 1
    assert row.processed_at is not None
    assert row.payload_json is None

    # 已结算的行不再被领取。
    queued.clear()
    assert outbox_task.recover() == 0
    assert queued == []

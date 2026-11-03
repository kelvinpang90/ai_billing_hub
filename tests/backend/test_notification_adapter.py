"""Notification Adapter and email templates (AIH-TASK-151; spec §47; ADR-0009 第 2 节).

⚠️ 第一组用例钉的是**逐字不变**：抽出模板层之后，两封现有邮件的收件人、标题与正文
必须与抽出之前一个字符都不差。下面的字面量是改动之前 main 上
`_render_password_reset` / `_render_portal_invitation` 对同一组固定输入的输出 ——
把那两个函数里的 f-string 按这组输入（基址、收件人、令牌、`expires_at`）展开得到，
不是按新模板反推出来的。
"""

from __future__ import annotations

import logging
from urllib.parse import quote

import pytest

from app.core.config import Settings
from app.core.email_templates import (
    TEMPLATE_PASSWORD_RESET,
    TEMPLATE_PORTAL_INVITATION,
    RenderedEmail,
    render_email,
)
from app.core.mailer import OutgoingEmail, SmtpEmailTransport
from app.core.notification_adapter import (
    Channel,
    DeliveryResult,
    EmailAdapter,
    OutboundMessage,
    build_adapter,
)
from app.tasks.outbox import _render_password_reset, _render_portal_invitation

BASE_URL = "https://billing.example.com"
TOKEN = "Zm9v+YmFy/YmF6="
QUOTED_TOKEN = "Zm9v%2BYmFy%2FYmF6%3D"
RESET_TO = "admin@example.com"
INVITATION_TO = "portal@example.com"
EXPIRES_AT = "2026-10-24T08:00:00"

RESET_SUBJECT = "Reset your Acuven Billing password"
RESET_BODY = (
    "We received a request to reset the password for your Acuven Billing account.\n\n"
    "Open this link to choose a new password:\n"
    "https://billing.example.com/reset-password?token=Zm9v%2BYmFy%2FYmF6%3D\n\n"
    "The link can be used once, and expires shortly.\n\n"
    "If you did not ask for this, you can ignore this email — your password has not changed."
)

INVITATION_SUBJECT = "Your invitation to the Acuven Billing customer portal"
INVITATION_BODY = (
    "You have been invited to the Acuven Billing customer portal.\n\n"
    "Open this link to set your password and activate your account:\n"
    "https://billing.example.com/accept-invitation?token=Zm9v%2BYmFy%2FYmF6%3D\n\n"
    "The link can be used once, and expires at "
    "2026-10-24 16:00 Malaysia time, UTC+8 (2026-10-24 08:00 UTC). "
    "If it has expired, ask your Acuven contact to send a new invitation.\n\n"
    "If you are waiting for another email from us and cannot find it, "
    "please check your spam or junk folder.\n\n"
    "If you were not expecting this invitation, you can ignore this email."
)


class RecordingTransport:
    """Records every `OutgoingEmail` it is handed; answers `succeed`."""

    def __init__(self, *, succeed: bool = True) -> None:
        self.succeed = succeed
        self.sent: list[OutgoingEmail] = []

    def send(self, message: OutgoingEmail) -> bool:
        self.sent.append(message)
        return self.succeed


def settings() -> Settings:
    return Settings(frontend_base_url=BASE_URL)


def reset_message() -> OutboundMessage:
    message = _render_password_reset(settings(), {"to": RESET_TO, "token": TOKEN})
    assert message is not None
    return message


def invitation_message() -> OutboundMessage:
    message = _render_portal_invitation(
        settings(), {"to": INVITATION_TO, "token": TOKEN, "expires_at": EXPIRES_AT}
    )
    assert message is not None
    return message


def test_the_fixture_token_needs_quoting() -> None:
    """字面量里的令牌是转义过的：输入里带 `+ / =`，quote 这一步才被钉住。"""
    assert quote(TOKEN, safe="") == QUOTED_TOKEN != TOKEN


# --- ① 逐字不变 ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("build", "to", "subject", "body"),
    [
        (reset_message, RESET_TO, RESET_SUBJECT, RESET_BODY),
        (invitation_message, INVITATION_TO, INVITATION_SUBJECT, INVITATION_BODY),
    ],
    ids=["password-reset", "portal-invitation"],
)
def test_the_existing_emails_are_unchanged_word_for_word(build, to, subject, body) -> None:
    transport = RecordingTransport()

    result = EmailAdapter(transport).send(build())

    assert result == DeliveryResult(accepted=True, provider_message_id=None)
    [sent] = transport.sent
    assert sent.to == to
    assert sent.subject == subject
    assert sent.body == body


def test_the_renderers_build_email_messages_for_their_templates() -> None:
    reset, invitation = reset_message(), invitation_message()

    assert (reset.channel, reset.template) == (Channel.EMAIL, TEMPLATE_PASSWORD_RESET)
    assert set(reset.params) == {"link"}
    assert (invitation.channel, invitation.template) == (Channel.EMAIL, TEMPLATE_PORTAL_INVITATION)
    assert set(invitation.params) == {"link", "deadline"}


# --- ② render_email ------------------------------------------------------------------------


def test_render_email_fills_in_a_known_template() -> None:
    rendered = render_email(TEMPLATE_PASSWORD_RESET, {"link": "https://x.example.com/r"})

    assert rendered == RenderedEmail(
        subject=RESET_SUBJECT,
        body=RESET_BODY.replace(
            "https://billing.example.com/reset-password?token=Zm9v%2BYmFy%2FYmF6%3D",
            "https://x.example.com/r",
        ),
    )


@pytest.mark.parametrize(
    ("template", "params"),
    [
        ("no_such_template", {"link": "https://x.example.com/r"}),
        (TEMPLATE_PASSWORD_RESET, {}),
        (TEMPLATE_PORTAL_INVITATION, {"link": "https://x.example.com/a"}),
        (TEMPLATE_PASSWORD_RESET, {"link": "https://x.example.com/r", "deadline": "soon"}),
        (TEMPLATE_PORTAL_INVITATION, {"link": "l", "deadline": "d", "extra": "e"}),
    ],
    ids=["unknown-template", "reset-missing", "invitation-missing", "reset-extra", "extra"],
)
def test_render_email_refuses_what_does_not_fit(template: str, params: dict[str, str]) -> None:
    assert render_email(template, params) is None


# --- ③ EmailAdapter ------------------------------------------------------------------------


def test_a_message_that_does_not_render_never_reaches_the_transport(caplog) -> None:
    transport = RecordingTransport()
    broken = OutboundMessage(
        channel=Channel.EMAIL,
        recipient=INVITATION_TO,
        template=TEMPLATE_PORTAL_INVITATION,
        params={"link": f"{BASE_URL}/accept-invitation?token={QUOTED_TOKEN}", "token": TOKEN},
    )

    with caplog.at_level(logging.DEBUG):
        result = EmailAdapter(transport).send(broken)

    assert result.accepted is False
    assert transport.sent == []
    errors = [record for record in caplog.records if record.levelno == logging.ERROR]
    assert errors, "a render failure must be visible in the logs"
    for record in caplog.records:
        text = record.getMessage() + str(vars(record))
        for secret in (TOKEN, QUOTED_TOKEN, INVITATION_TO):
            assert secret not in text


def test_a_message_for_another_channel_never_reaches_the_transport() -> None:
    transport = RecordingTransport()
    stray = OutboundMessage(
        channel="whatsapp",  # type: ignore[arg-type]
        recipient=RESET_TO,
        template=TEMPLATE_PASSWORD_RESET,
        params={"link": f"{BASE_URL}/reset-password?token={QUOTED_TOKEN}"},
    )

    assert EmailAdapter(transport).send(stray).accepted is False
    assert transport.sent == []


def test_a_transport_failure_is_not_accepted() -> None:
    transport = RecordingTransport(succeed=False)

    result = EmailAdapter(transport).send(reset_message())

    assert result.accepted is False
    assert len(transport.sent) == 1


def test_a_sent_email_is_accepted_without_a_provider_message_id() -> None:
    result = EmailAdapter(RecordingTransport()).send(reset_message())

    assert result.accepted is True
    assert result.provider_message_id is None


# --- ④ repr ------------------------------------------------------------------------------


def test_the_repr_of_a_message_carries_no_token_and_no_recipient() -> None:
    for message in (reset_message(), invitation_message()):
        text = repr(message)
        for secret in (TOKEN, QUOTED_TOKEN, RESET_TO, INVITATION_TO):
            assert secret not in text
        assert message.template in text


# --- ⑤ build_adapter ---------------------------------------------------------------------


def test_the_email_channel_is_served_by_the_email_adapter_over_smtp() -> None:
    adapter = build_adapter(Channel.EMAIL, Settings())

    assert isinstance(adapter, EmailAdapter)
    assert adapter.channel is Channel.EMAIL
    assert isinstance(adapter.transport, SmtpEmailTransport)

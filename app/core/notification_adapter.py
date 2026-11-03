"""Channel-independent Notification Adapter (spec §47; ADR-0009).

业务代码按「收件人 + 模板键 + 参数」发一条通知，不知道它走哪个通道、怎么渲染、
由谁投递。`build_adapter` 是唯一决定用哪个实现的地方。

本任务只有 Email 一个通道：`EmailAdapter` 用 app/core/email_templates.py 渲染，
交给 app/core/mailer.py 的 `EmailTransport` 发出。

⚠️ **日志只带模板键与通道**：不带收件人、参数值（里面有带令牌的链接），
也不带异常的绑定内容。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

from app.core.config import Settings
from app.core.email_templates import render_email
from app.core.mailer import EmailTransport, OutgoingEmail, build_transport

logger = logging.getLogger(__name__)


class Channel(StrEnum):
    """Delivery channels. 只列已有实现的通道，不预留。"""

    EMAIL = "email"


@dataclass(frozen=True)
class OutboundMessage:
    """One notification to send: who, through which channel, which template, with what.

    ⚠️ `recipient` 与 `params` 不进 repr：params 里有带令牌的链接，
    一条顺手的 `logger.debug(message)` 不该把它写进日志。
    """

    channel: Channel
    recipient: str = field(repr=False)
    template: str
    params: Mapping[str, str] = field(repr=False)


@dataclass(frozen=True)
class DeliveryResult:
    """What the channel said.

    `provider_message_id`：Email 恒为 None。这个值留给需要投递回执的通道
    （ADR-0009 第 5 节，WhatsApp）；本任务不接收回执、不持久化它。
    """

    accepted: bool
    provider_message_id: str | None = None


class NotificationAdapter(Protocol):
    """One channel's way of sending an `OutboundMessage`."""

    @property
    def channel(self) -> Channel: ...

    def send(self, message: OutboundMessage) -> DeliveryResult:
        """**任何投递失败都返回 `accepted=False`，不抛异常。**"""
        ...


class EmailAdapter:
    """Render with app/core/email_templates.py, send through an `EmailTransport`."""

    def __init__(self, transport: EmailTransport) -> None:
        self._transport = transport

    @property
    def channel(self) -> Channel:
        return Channel.EMAIL

    @property
    def transport(self) -> EmailTransport:
        return self._transport

    def send(self, message: OutboundMessage) -> DeliveryResult:
        if message.channel is not Channel.EMAIL:
            logger.error(
                "A message for another channel reached the email adapter",
                extra={"channel": str(message.channel), "template": message.template},
            )
            return DeliveryResult(accepted=False)

        rendered = render_email(message.template, message.params)
        if rendered is None:
            # ⚠️ 只记模板键：参数缺了哪个、多了哪个，排查看调用方，不把值写进日志。
            logger.error(
                "Could not render an email template",
                extra={"channel": str(message.channel), "template": message.template},
            )
            return DeliveryResult(accepted=False)

        # 传输层自己保证不抛、失败返回 False（app/core/mailer.py 的 `EmailTransport`）。
        sent = self._transport.send(
            OutgoingEmail(to=message.recipient, subject=rendered.subject, body=rendered.body)
        )
        return DeliveryResult(accepted=bool(sent))


def build_adapter(channel: Channel, settings: Settings) -> NotificationAdapter:
    """The one place that decides which adapter implementation serves a channel."""
    if channel is Channel.EMAIL:
        return EmailAdapter(build_transport(settings))
    raise ValueError(f"no adapter for channel {channel!r}")


__all__ = [
    "Channel",
    "DeliveryResult",
    "EmailAdapter",
    "NotificationAdapter",
    "OutboundMessage",
    "build_adapter",
]

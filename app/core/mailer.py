"""Outbound email transport (ADR-0009; spec §47).

**这一层只负责「把一封信交给 SMTP 服务器」**，不知道信是什么、也不负责重试。
重试、去重、状态持久化全在 `domain_outbox` 那一侧（Invariant 14）——两件事混在
一起正是 ADR-0009 拒绝照搬 `rs-roof-pms` 的原因。

从 `rs-roof-pms` 借来的几条（都是别人已经踩过的坑）：

- **不绑供应商**：直连 SMTP，换服务商只改配置
- **465 / 587 的 TLS 分支**：两个 TLS 参数互斥，传错**不是报错而是静默挂起到超时**
- **空 host = 未配置，是合法状态**：开发与 CI 不需要真实邮箱凭据就能跑通全流程
- **发送不抛异常、返回布尔**：邮件服务器抖动不该把调用方打成 500（Invariant 1）
- **发件人地址与认证用户名分开配**：用 relay + API key 认证时两者不是一回事

⚠️ **一处对 ADR-0009 的偏离，明写在这里**：ADR 提的是 `aiosmtplib`，这里用标准库
`smtplib`。理由是调用方是**同步的 Celery worker**，用异步库就得在每次发送时
`asyncio.run()` 起停一次事件循环——为一个纯粹的同步调用引入一个依赖和一层包装。
ADR 借 `aiosmtplib` 的实质（上面那五条）一条不少地保留了。**如果审查方认为该照
ADR 原文走，这里换掉只影响本文件一个函数。**
"""

from __future__ import annotations

import logging
import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage as MimeMessage
from pathlib import Path
from typing import Protocol

from app.core.config import Settings

logger = logging.getLogger(__name__)

# 隐式 TLS 的端口。⚠️ 465 与 587 要走**互斥**的两条路：465 是连上来就是 TLS，
# 587 是先明文连、再 STARTTLS 升级。搞反了不会报错，会**挂到超时**。
_IMPLICIT_TLS_PORT = 465


@dataclass(frozen=True)
class OutgoingEmail:
    """One rendered message. 渲染在调用方，这里只管发。"""

    to: str
    subject: str
    body: str


class EmailTransport(Protocol):
    """spec §47 强制的 Notification Adapter 抽象在 Email 这一侧的最小形态。

    ⚠️ 业务代码只依赖这个协议，**不直接调 SMTP**。ADR-0009 备选方案 A 记着：
    将来若换成 SendGrid / SES 这类 API 服务，改的只是本文件里的一个实现类。
    """

    def send(self, message: OutgoingEmail) -> bool:
        """True 表示服务器收下了。**任何失败都返回 False，不抛异常。**"""
        ...


def _load_password(settings: Settings) -> str:
    """Read the SMTP password from the file named by settings.

    ⚠️ 从**文件**读，不从环境变量读（ADR-0004 第 2 节）。没配就返回空串 ——
    有些 relay 只认 IP 白名单，不需要密码，那是合法配置。
    """
    path = settings.smtp_password_file.strip()
    if not path:
        return ""
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        # ⚠️ 不把路径写进日志消息本身。
        logger.error("Could not read the SMTP password file", exc_info=True)
        return ""


class SmtpEmailTransport:
    """Send one message per connection over SMTP."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    @property
    def configured(self) -> bool:
        return bool(self._settings.smtp_host.strip())

    def send(self, message: OutgoingEmail) -> bool:
        settings = self._settings
        if not self.configured:
            # 未配置是合法状态，但**要看得见** —— 否则「信没发出去」会安静地
            # 表现成「用户没收到」，而那是最难查的一类报障。
            logger.warning("Email transport is not configured; nothing was sent")
            return False

        sender = settings.smtp_from.strip() or settings.smtp_username.strip()
        if not sender:
            logger.error("No sender address configured for outbound email")
            return False

        mime = MimeMessage()
        mime["From"] = sender
        mime["To"] = message.to
        mime["Subject"] = message.subject
        mime.set_content(message.body)

        try:
            self._deliver(mime)
        except (OSError, smtplib.SMTPException):
            # ⚠️ **不抛**：调用方是 outbox 投递任务，它要的是「成没成」，
            # 失败的处置（退避重试）在那边。异常穿出去只会让任务被 Celery
            # 重投一次，绕过我们自己的退避。
            logger.warning("Sending an email failed", exc_info=True)
            return False
        return True

    def _deliver(self, mime: MimeMessage) -> None:
        settings = self._settings
        host = settings.smtp_host.strip()
        password = _load_password(settings)
        username = settings.smtp_username.strip()

        if settings.smtp_port == _IMPLICIT_TLS_PORT:
            # 465：连上来就是 TLS。
            context = ssl.create_default_context()
            with smtplib.SMTP_SSL(
                host, settings.smtp_port, timeout=settings.smtp_timeout_seconds, context=context
            ) as client:
                if username:
                    client.login(username, password)
                client.send_message(mime)
            return

        # 587（以及其它端口）：先明文连，再 STARTTLS 升级。
        with smtplib.SMTP(
            host, settings.smtp_port, timeout=settings.smtp_timeout_seconds
        ) as client:
            client.starttls(context=ssl.create_default_context())
            if username:
                client.login(username, password)
            client.send_message(mime)


def build_transport(settings: Settings) -> EmailTransport:
    """The one place that decides which transport implementation is in use."""
    return SmtpEmailTransport(settings)


__all__ = [
    "EmailTransport",
    "OutgoingEmail",
    "SmtpEmailTransport",
    "build_transport",
]

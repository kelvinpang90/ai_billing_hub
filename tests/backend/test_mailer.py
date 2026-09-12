"""SMTP transport (ADR-0009).

⚠️ **465 / 587 那条分支是这个文件存在的主要理由。**两个 TLS 参数互斥，传错不是
报错而是**静默挂起到超时** —— 现象是「邮件队列不动了」，指不回配置。
"""

from __future__ import annotations

import smtplib

import pytest

from app.core.config import Settings
from app.core.mailer import OutgoingEmail, SmtpEmailTransport, build_transport

MESSAGE = OutgoingEmail(to="admin@example.com", subject="Subject", body="Body")


class FakeSmtp:
    """Stands in for smtplib.SMTP / SMTP_SSL, recording how it was driven."""

    instances: list[FakeSmtp] = []

    def __init__(self, host, port, timeout=None, context=None) -> None:  # noqa: ANN001
        self.host = host
        self.port = port
        self.timeout = timeout
        self.context = context
        self.started_tls = False
        self.logged_in: tuple[str, str] | None = None
        self.sent: list[object] = []
        FakeSmtp.instances.append(self)

    def __enter__(self) -> FakeSmtp:
        return self

    def __exit__(self, *_exc) -> None:
        return None

    def starttls(self, context=None) -> None:  # noqa: ANN001
        self.started_tls = True

    def login(self, username, password) -> None:  # noqa: ANN001
        self.logged_in = (username, password)

    def send_message(self, message) -> None:  # noqa: ANN001
        self.sent.append(message)


@pytest.fixture(autouse=True)
def _reset_instances():
    FakeSmtp.instances = []
    yield
    FakeSmtp.instances = []


@pytest.fixture
def smtp(monkeypatch):
    monkeypatch.setattr(smtplib, "SMTP", FakeSmtp)
    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSmtp)
    return FakeSmtp


def configured(**overrides) -> Settings:
    return Settings(
        smtp_host="smtp.example.com",
        smtp_username="mailer@example.com",
        smtp_from="billing@example.com",
        **overrides,
    )


# --- 未配置也是合法状态 ---------------------------------------------------------


def test_an_unconfigured_transport_reports_failure_without_raising() -> None:
    """ADR-0009：空 host = 未配置，开发与 CI 不需要真实凭据就能跑通全流程。

    ⚠️ 但它必须**返回 False** —— 悄悄返回 True 会让 outbox 行被标成 SENT，
    于是「没发出去」这件事从此再也查不到。
    """
    assert SmtpEmailTransport(Settings()).send(MESSAGE) is False


def test_a_transport_without_any_sender_address_refuses_to_send(smtp) -> None:
    """没有 From 的信会被大多数服务器直接拒掉；在这里挡住比在那边挡住可诊断得多。"""
    assert SmtpEmailTransport(Settings(smtp_host="smtp.example.com")).send(MESSAGE) is False
    assert smtp.instances == []


def test_the_sender_falls_back_to_the_username(smtp) -> None:
    """用普通 SMTP 账号时两者就是一回事，不该逼人配两遍。"""
    settings = Settings(smtp_host="smtp.example.com", smtp_username="mailer@example.com")

    assert SmtpEmailTransport(settings).send(MESSAGE) is True
    assert smtp.instances[0].sent[0]["From"] == "mailer@example.com"


# --- TLS 分支 -----------------------------------------------------------------


def test_port_465_uses_implicit_tls_and_never_calls_starttls(smtp) -> None:
    """⚠️ 465 是「连上来就是 TLS」。在它上面再调一次 STARTTLS 是协议错误。"""
    assert SmtpEmailTransport(configured(smtp_port=465)).send(MESSAGE) is True

    client = smtp.instances[0]
    assert client.context is not None, "465 必须带 TLS context"
    assert client.started_tls is False


def test_port_587_upgrades_with_starttls(smtp) -> None:
    """⚠️ 587 是先明文连再升级。**不升级就等于把密码明文发出去**。"""
    assert SmtpEmailTransport(configured(smtp_port=587)).send(MESSAGE) is True

    client = smtp.instances[0]
    assert client.started_tls is True


def test_the_connection_always_carries_a_timeout(smtp) -> None:
    """⚠️ 不设超时的话，一个卡住的连接会把 worker 那个进程永久占着 ——
    积压的信一封也发不出去，而现象只是「队列不动了」。"""
    assert SmtpEmailTransport(configured(smtp_timeout_seconds=7)).send(MESSAGE) is True

    assert smtp.instances[0].timeout == 7


# --- 凭据 ---------------------------------------------------------------------


def test_the_password_comes_from_a_file_not_an_environment_variable(smtp, tmp_path) -> None:
    """ADR-0004 第 2 节：环境变量会进 /proc/<pid>/environ、崩溃转储，并被子进程继承。"""
    secret = tmp_path / "smtp.password"
    secret.write_text("the-password\n", encoding="utf-8")

    assert SmtpEmailTransport(configured(smtp_password_file=str(secret))).send(MESSAGE) is True

    assert smtp.instances[0].logged_in == ("mailer@example.com", "the-password")


def test_an_unreadable_password_file_does_not_raise(smtp, tmp_path) -> None:
    """配错路径不该把 worker 打崩 —— 它该表现成一次普通的投递失败。"""
    settings = configured(smtp_password_file=str(tmp_path / "nope"))

    assert SmtpEmailTransport(settings).send(MESSAGE) is True  # 空密码仍会尝试登录
    assert smtp.instances[0].logged_in == ("mailer@example.com", "")


def test_no_login_is_attempted_without_a_username(smtp) -> None:
    """有些 relay 只认 IP 白名单。空用户名下强行 login 会被服务器拒掉。"""
    settings = Settings(smtp_host="smtp.example.com", smtp_from="billing@example.com")

    assert SmtpEmailTransport(settings).send(MESSAGE) is True
    assert smtp.instances[0].logged_in is None


# --- 失败一律返回 False --------------------------------------------------------


@pytest.mark.parametrize(
    "failure",
    [smtplib.SMTPException("refused"), OSError("connection reset")],
    ids=["smtp-error", "socket-error"],
)
def test_a_failing_server_is_reported_not_raised(monkeypatch, failure) -> None:
    """⚠️ **不抛**：调用方是 outbox 投递任务，异常穿出去会让 Celery 重投一次，
    绕过我们自己的退避（Invariant 1 的同一条道理）。"""

    def explode(*_args, **_kwargs):
        raise failure

    monkeypatch.setattr(smtplib, "SMTP", explode)

    assert SmtpEmailTransport(configured()).send(MESSAGE) is False


def test_the_message_carries_what_it_was_given(smtp) -> None:
    assert SmtpEmailTransport(configured()).send(MESSAGE) is True

    mime = smtp.instances[0].sent[0]
    assert mime["To"] == MESSAGE.to
    assert mime["Subject"] == MESSAGE.subject
    assert MESSAGE.body in mime.get_content()


def test_build_transport_is_the_single_place_that_picks_an_implementation() -> None:
    """ADR-0009 备选方案 A：换成 SendGrid / SES 这类 API 服务时，只改这一个函数。"""
    assert isinstance(build_transport(Settings()), SmtpEmailTransport)

"""Email templates (ADR-0009 第 2 节; spec §47).

**只用标准库**（`string.Template`），不加依赖。每个模板是标题、正文与一组必需参数；
渲染只做占位符替换，链接与到期时刻怎么拼由调用方决定（app/tasks/outbox.py）。

⚠️ **只有英文**（Kelvin 2026-11-03 定）：不加语言字段，要双语时再加。

⚠️ 以后新增一类邮件 = **在这里加一个模板**，不在 outbox 里拼字符串 ——
ADR-0009 记着 `rs-roof-pms` 没有模板层是缺点，第二类邮件出现之前先抽出来，正是为此。

两个现有模板的标题与正文从改动前的 app/tasks/outbox.py 逐字搬来，只把链接与到期时刻
换成占位符；tests/backend/test_notification_adapter.py 逐字钉住渲染结果。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from string import Template
from typing import Final

TEMPLATE_PASSWORD_RESET: Final = "password_reset"
TEMPLATE_PORTAL_INVITATION: Final = "portal_invitation"


@dataclass(frozen=True)
class RenderedEmail:
    """A template filled in: what goes into `OutgoingEmail` besides the recipient."""

    subject: str
    body: str


@dataclass(frozen=True)
class _EmailTemplate:
    subject: str
    body: Template
    required: frozenset[str]


_TEMPLATES: Final[Mapping[str, _EmailTemplate]] = {
    TEMPLATE_PASSWORD_RESET: _EmailTemplate(
        subject="Reset your Acuven Billing password",
        body=Template(
            "We received a request to reset the password for your Acuven Billing account.\n\n"
            "Open this link to choose a new password:\n$link\n\n"
            "The link can be used once, and expires shortly.\n\n"
            "If you did not ask for this, you can ignore this email — "
            "your password has not changed."
        ),
        required=frozenset({"link"}),
    ),
    TEMPLATE_PORTAL_INVITATION: _EmailTemplate(
        subject="Your invitation to the Acuven Billing customer portal",
        body=Template(
            "You have been invited to the Acuven Billing customer portal.\n\n"
            "Open this link to set your password and activate your account:\n$link\n\n"
            "The link can be used once, and expires at $deadline. "
            "If it has expired, ask your Acuven contact to send a new invitation.\n\n"
            "If you are waiting for another email from us and cannot find it, "
            "please check your spam or junk folder.\n\n"
            "If you were not expecting this invitation, you can ignore this email."
        ),
        required=frozenset({"link", "deadline"}),
    ),
}


def render_email(template: str, params: Mapping[str, str]) -> RenderedEmail | None:
    """Fill in one template, or None when the key is unknown or the params do not fit.

    ⚠️ 参数的键集合必须与必需集合**完全相等**：缺一个会留下半封信，多一个说明调用方与
    模板对不上 —— 两种都不发。用 `substitute` 而不是 `safe_substitute`：
    后者会把没替换的 `$link` 原样留在正文里发出去。**不抛异常。**
    """
    found = _TEMPLATES.get(template)
    if found is None or set(params) != found.required:
        return None
    try:
        body = found.body.substitute(params)
    except (KeyError, ValueError):
        return None
    return RenderedEmail(subject=found.subject, body=body)


__all__ = [
    "TEMPLATE_PASSWORD_RESET",
    "TEMPLATE_PORTAL_INVITATION",
    "RenderedEmail",
    "render_email",
]

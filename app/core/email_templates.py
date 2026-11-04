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
# 低余额、停机、复机通知邮件（设计闸门 #559 v2 §2「模板」，AIH-TASK-152）。
TEMPLATE_NOTIFICATION_LOW_BALANCE: Final = "notification_low_balance"
TEMPLATE_NOTIFICATION_SUSPENDED: Final = "notification_suspended"
TEMPLATE_NOTIFICATION_REACTIVATED: Final = "notification_reactivated"


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
    # ⚠️ 三封通知信的标题固定、不含任何 payload 值；文案**只说计费状态，不说服务状态**：
    # 复机后 AI 服务是否可用还看账户与项目状态（spec §24、§27），所以不写「服务已恢复」。
    TEMPLATE_NOTIFICATION_LOW_BALANCE: _EmailTemplate(
        subject="Your Acuven Billing balance is low",
        body=Template(
            "The prepaid balance of the Acuven Billing account for $company_name "
            "has fallen to RM $balance, "
            "at or below your low-balance threshold of RM $threshold.\n\n"
            "This was recorded at $occurred_at.\n\n"
            "AI services billed to this account are suspended when the balance reaches RM 0.00. "
            "To top up, please contact your Acuven contact.\n\n"
            "You can check your balance in the customer portal:\n$link"
        ),
        required=frozenset({"company_name", "balance", "threshold", "occurred_at", "link"}),
    ),
    TEMPLATE_NOTIFICATION_SUSPENDED: _EmailTemplate(
        subject="Your Acuven Billing account is suspended",
        body=Template(
            "The prepaid balance of the Acuven Billing account for $company_name "
            "reached RM $balance at $occurred_at, "
            "so AI services billed to this account are now suspended.\n\n"
            "This balance suspension is lifted automatically once the balance is above RM 0.00. "
            "To top up, please contact your Acuven contact.\n\n"
            "You can check your balance in the customer portal:\n$link"
        ),
        required=frozenset({"company_name", "balance", "occurred_at", "link"}),
    ),
    TEMPLATE_NOTIFICATION_REACTIVATED: _EmailTemplate(
        subject="Your Acuven Billing balance suspension has been lifted",
        body=Template(
            "The prepaid balance of the Acuven Billing account for $company_name "
            "is RM $balance as of $occurred_at, "
            "so this account is no longer suspended for its balance.\n\n"
            "If AI services are still unavailable, the account or one of its projects "
            "may be disabled for another reason; please contact your Acuven contact.\n\n"
            "You can check your balance in the customer portal:\n$link"
        ),
        required=frozenset({"company_name", "balance", "occurred_at", "link"}),
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
    "TEMPLATE_NOTIFICATION_LOW_BALANCE",
    "TEMPLATE_NOTIFICATION_REACTIVATED",
    "TEMPLATE_NOTIFICATION_SUSPENDED",
    "TEMPLATE_PASSWORD_RESET",
    "TEMPLATE_PORTAL_INVITATION",
    "RenderedEmail",
    "render_email",
]

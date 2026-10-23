"""Request and response shapes for the admin portal account endpoints (design gate #368 §2).

⚠️ **响应是字段白名单**，与 app/schemas/customers.py 同一条：
恰好设计 §2 的七个键，由服务返回的 `PortalAccountView` 逐字段搬过来，
不直接序列化 ORM 对象。没有内部用户 id、`tenant_id`、哈希、锁定信息 ——
以后给 `users` 加列，不会因此**静默**出现在响应里。

⚠️ 请求体多余字段一律 422：客户只来自路径（INV-8），
状态、`tenant_id`、令牌都不能经请求体带进来。
"""

from __future__ import annotations

import datetime as dt

from pydantic import BaseModel, ConfigDict, EmailStr, field_validator

from app.models.auth import UserStatus
from app.schemas.customers import _Reason
from app.services.portal_accounts import PortalAccountView

# 与 `users.email` 的列宽一致。
_EMAIL_LENGTH = 320


def _check_email_length(value: object) -> object:
    """去首尾空白后再查列宽：首尾空白不同的同一地址按同一个处理（设计 §7「边界值」）。"""
    if not isinstance(value, str):
        return value
    value = value.strip()
    if len(value) > _EMAIL_LENGTH:
        # 422 只列字段名，不回显这个值。
        raise ValueError("email is too long")
    return value


class CreatePortalAccountRequest(BaseModel):
    """开通：登录邮箱必填。归一（去首尾空白、转小写）在服务层，与登录同一规则。"""

    model_config = ConfigDict(extra="forbid")

    email: EmailStr

    @field_validator("email", mode="before")
    @classmethod
    def _email_fits_the_column(cls, value: object) -> object:
        return _check_email_length(value)


class ResendInvitationRequest(BaseModel):
    """重发：`email` 可省略。省略、`null`，或归一后与当前邮箱相同，都不算改邮箱（设计 §4）。"""

    model_config = ConfigDict(extra="forbid")

    email: EmailStr | None = None

    @field_validator("email", mode="before")
    @classmethod
    def _email_fits_the_column(cls, value: object) -> object:
        return _check_email_length(value)


class ResetPortalTwoFactorRequest(BaseModel):
    """重置客户 2FA：原因必填，去首尾空白后 1–255 个字符（与改账户状态共用 `_Reason`）。

    原因只进审计，这一层不记。管理员必须先在线下核实身份
    （docs/runbook.md「客户门户账号」）。
    """

    model_config = ConfigDict(extra="forbid")

    reason: _Reason


class PortalAccount(BaseModel):
    """The seven keys of the admin GET (design §2「API」); every endpoint returns this shape."""

    email: str
    status: UserStatus
    invited_at: dt.datetime | None
    invitation_expires_at: dt.datetime | None
    activated_at: dt.datetime | None
    last_login_at: dt.datetime | None
    two_factor_enabled: bool


def portal_account(view: PortalAccountView) -> PortalAccount:
    """`invited_at` / `invitation_expires_at` 已在服务里按设计 §2 取好，这里只搬字段。"""
    return PortalAccount(
        email=view.email,
        status=view.status,
        invited_at=view.invited_at,
        invitation_expires_at=view.invitation_expires_at,
        activated_at=view.activated_at,
        last_login_at=view.last_login_at,
        two_factor_enabled=view.two_factor_enabled,
    )

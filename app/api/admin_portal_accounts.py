"""Admin customer portal account endpoints (design gate #368 v12 §2「API」管理端).

契约见 [docs/api.md](../../docs/api.md)「管理端门户账号」。

⚠️ **每个处理函数的第一条语句是 `require_admin(request)`**，
与 app/api/admin_customers.py 同一条：显式调用，不挂路由器依赖。
漏调由 `tests/backend/test_admin_customers_api.py` 的路由枚举用例兜底
（CUSTOMER 令牌 403、匿名 401，且不写库）。

规则全在 `app/services/portal_accounts.py`（判定顺序、加锁、审计、
`invited_at` 与 `invitation_expires_at` 的取法），
这一层只做鉴权、请求体形状、转发与序列化。

⚠️ 开通与重发成功后，用服务返回的 outbox 行 id 调 `_trigger_outbox` 立即触发投递
（与忘记密码同一做法）。触发失败不影响响应：
行已经和账号同事务落库，周期恢复会补投（INV-14）。

这一层不记请求体，也不记邮箱（REQ-PRIV-001）；重置 2FA 的原因只进审计，这里不记。
"""

from __future__ import annotations

from fastapi import APIRouter, Request, status

from app.api.auth import _trigger_outbox, request_context, require_admin, require_session_factory
from app.core.logging import current_request_id
from app.schemas.envelope import ApiResponse, success
from app.schemas.portal_accounts import (
    CreatePortalAccountRequest,
    PortalAccount,
    ResendInvitationRequest,
    ResetPortalTwoFactorRequest,
    portal_account,
)
from app.services import portal_accounts

router = APIRouter(prefix="/api/v1/admin", tags=["admin"])

_PORTAL_ACCOUNT = "/customers/{customer_id}/portal-account"


@router.get(_PORTAL_ACCOUNT, response_model=ApiResponse[PortalAccount])
def get_portal_account(request: Request, customer_id: str) -> ApiResponse[PortalAccount]:
    """Read-only, no audit. 404 `CUSTOMER_NOT_FOUND` / `PORTAL_ACCOUNT_NOT_FOUND`."""
    require_admin(request)
    view = portal_accounts.get_portal_account(
        require_session_factory(request), customer_id=customer_id
    )
    return success(portal_account(view), request_id=current_request_id())


@router.post(
    _PORTAL_ACCOUNT,
    status_code=status.HTTP_201_CREATED,
    response_model=ApiResponse[PortalAccount],
)
def create_portal_account(
    request: Request, customer_id: str, payload: CreatePortalAccountRequest
) -> ApiResponse[PortalAccount]:
    """Create the `INVITED` account and send the invitation (outbox, same commit).

    409 `ACCOUNT_CLOSED` / `PORTAL_ACCOUNT_EXISTS` / `EMAIL_IN_USE`；邮箱格式 422。
    """
    admin = require_admin(request)
    issued = portal_accounts.create_portal_account(
        require_session_factory(request),
        request.app.state.settings,
        actor=admin,
        customer_id=customer_id,
        email=payload.email,
        context=request_context(request),
    )
    _trigger_outbox(request, issued.outbox_id)
    return success(portal_account(issued.account), request_id=current_request_id())


@router.post(_PORTAL_ACCOUNT + "/resend-invitation", response_model=ApiResponse[PortalAccount])
def resend_invitation(
    request: Request, customer_id: str, payload: ResendInvitationRequest | None = None
) -> ApiResponse[PortalAccount]:
    """Void the old invitation links and send a new one; optionally correct the email.

    只在 `INVITED` 时可用，否则 409 `PORTAL_ACCOUNT_NOT_INVITED`；新邮箱被占用 409
    `EMAIL_IN_USE`；租户已关户 409 `ACCOUNT_CLOSED`。请求体可以省略。
    """
    admin = require_admin(request)
    issued = portal_accounts.resend_invitation(
        require_session_factory(request),
        request.app.state.settings,
        actor=admin,
        customer_id=customer_id,
        email=payload.email if payload is not None else None,
        context=request_context(request),
    )
    _trigger_outbox(request, issued.outbox_id)
    return success(portal_account(issued.account), request_id=current_request_id())


@router.post(_PORTAL_ACCOUNT + "/disable", response_model=ApiResponse[PortalAccount])
def disable_portal_account(request: Request, customer_id: str) -> ApiResponse[PortalAccount]:
    """`INVITED` / `ACTIVE` → `DISABLED`: sessions and every unused token end at once.

    已停用 409 `PORTAL_ACCOUNT_ALREADY_DISABLED`。
    """
    admin = require_admin(request)
    view = portal_accounts.disable_portal_account(
        require_session_factory(request),
        actor=admin,
        customer_id=customer_id,
        context=request_context(request),
    )
    return success(portal_account(view), request_id=current_request_id())


@router.post(_PORTAL_ACCOUNT + "/enable", response_model=ApiResponse[PortalAccount])
def enable_portal_account(request: Request, customer_id: str) -> ApiResponse[PortalAccount]:
    """`DISABLED` → `INVITED` (never accepted) or `ACTIVE`. Does not resend the invitation.

    不是 `DISABLED` 409 `PORTAL_ACCOUNT_NOT_DISABLED`；租户已关户 409 `ACCOUNT_CLOSED`。
    """
    admin = require_admin(request)
    view = portal_accounts.enable_portal_account(
        require_session_factory(request),
        actor=admin,
        customer_id=customer_id,
        context=request_context(request),
    )
    return success(portal_account(view), request_id=current_request_id())


@router.post(_PORTAL_ACCOUNT + "/reset-2fa", response_model=ApiResponse[PortalAccount])
def reset_portal_two_factor(
    request: Request, customer_id: str, payload: ResetPortalTwoFactorRequest
) -> ApiResponse[PortalAccount]:
    """Delete the account's 2FA and recovery codes and end its sessions, after offline checks.

    没有已确认或待确认的 2FA 409 `PORTAL_2FA_NOT_ENABLED`；原因为空或超过 255 个字符 422。
    """
    admin = require_admin(request)
    view = portal_accounts.reset_portal_two_factor(
        require_session_factory(request),
        actor=admin,
        customer_id=customer_id,
        reason=payload.reason,
        context=request_context(request),
    )
    return success(portal_account(view), request_id=current_request_id())

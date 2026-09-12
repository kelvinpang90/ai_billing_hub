"""Two-factor enrolment endpoints (spec §54; design gate Issue #32 v5).

**为什么注册端点收的是 `pending_token` 而不是访问令牌**：ADMIN 的 2FA 是强制的，
所以一个刚建好的管理员**第一次登录时还没有访问令牌** —— 他停在 `ENROL_2FA`
那一步，手上只有 pending 令牌。注册必须能用它走完，否则新管理员永远进不来。

⚠️ 反过来说，pending 令牌**只能**用来走这条路：它的 `typ` 与访问令牌不同，
拿它去调业务端点会被 `decode_token(expected_type=...)` 直接拒掉。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request

from app.api.auth import client_ip, request_context, require_session_factory
from app.core.logging import current_request_id
from app.core.tokens import TOKEN_TYPE_PENDING_2FA, decode_token
from app.schemas.auth import (
    ConfirmEnrolRequest,
    EnrolRequest,
    EnrolResponse,
    RecoveryCodesResponse,
    RegenerateRecoveryCodesRequest,
)
from app.schemas.envelope import ApiResponse, success
from app.services.auth import InvalidCredentials
from app.services.two_factor import (
    confirm_enrolment,
    regenerate_recovery_codes,
    start_enrolment,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/auth/2fa", tags=["auth"])


def _pending_user_id(request: Request, token: str) -> int:
    settings = request.app.state.settings
    payload = decode_token(settings, token, expected_type=TOKEN_TYPE_PENDING_2FA)
    return int(payload["sub"])


@router.post("/enrol", response_model=ApiResponse[EnrolResponse])
def enrol(request: Request, payload: EnrolRequest) -> ApiResponse[EnrolResponse]:
    """Generate a TOTP secret and return it **once**.

    ⚠️ 已确认过的账号会拿到 409，而且**不会重新发一把密钥** —— 否则任何能调
    这个端点的人都可以把别人已启用的 2FA 换成自己的。
    """
    user_id = _pending_user_id(request, payload.pending_token)
    enrolment = start_enrolment(
        require_session_factory(request), request.app.state.settings, user_id=user_id
    )
    # ⚠️ 不记 access log 之外的任何东西：`otpauth_uri` 的查询串里就是密钥本身。
    # T0.3 的脱敏已按 `otpauth` 键名兜住，但这里首先就不该往日志里写。
    return success(
        EnrolResponse(secret=enrolment.secret, otpauth_uri=enrolment.otpauth_uri),
        request_id=current_request_id(),
    )


@router.post("/confirm", response_model=ApiResponse[RecoveryCodesResponse])
def confirm(request: Request, payload: ConfirmEnrolRequest) -> ApiResponse[RecoveryCodesResponse]:
    """Verify the first code, enable 2FA, and hand out recovery codes **once**."""
    user_id = _pending_user_id(request, payload.pending_token)
    codes = confirm_enrolment(
        require_session_factory(request),
        request.app.state.settings,
        user_id=user_id,
        code=payload.code,
        context=request_context(request),
    )
    return success(RecoveryCodesResponse(recovery_codes=codes), request_id=current_request_id())


@router.post("/recovery-codes", response_model=ApiResponse[RecoveryCodesResponse])
def regenerate(
    request: Request, payload: RegenerateRecoveryCodesRequest
) -> ApiResponse[RecoveryCodesResponse]:
    """Replace every live recovery code. Requires the password again.

    ⚠️ 光有访问令牌不够。恢复码等价于第二因子，一张被偷走的访问令牌就能换出
    十个新的 —— 那等于把 2FA 绕过了。所以这里**重新验一次密码**。
    """
    from app.api.auth import require_current_user
    from app.core.passwords import verify_password

    user = require_current_user(request)
    if not verify_password(user.password_hash, payload.password):
        # 复用同一个错误：这里没有枚举问题，但保持一种失败形状更省事。
        raise InvalidCredentials

    codes = regenerate_recovery_codes(
        require_session_factory(request),
        user_id=user.id,
        context=request_context(request),
    )
    logger.info(
        "Recovery codes regenerated",
        extra={"component": "auth", "user_id": user.id, "client": client_ip(request)},
    )
    return success(RecoveryCodesResponse(recovery_codes=codes), request_id=current_request_id())

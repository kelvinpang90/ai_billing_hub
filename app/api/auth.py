"""Authentication endpoints (spec §53; design gate Issue #32 v5).

路径前缀是 `/api/v1/` 而不是裸路径：`deploy/nginx/billing.conf` 把 `/api/` 整段
转给后端且**不改写路径**，所以浏览器、nginx 日志、应用日志里的 uri 是同一个
字符串。改成裸路径就要在边缘加一条规则，三处也就对不上了。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request, Response

from app.core.clientip import FORWARDED_FOR_HEADER, resolve_client_ip
from app.core.errors import AppError
from app.core.logging import current_request_id
from app.core.ratelimit import RateLimited, TokenBucket
from app.core.tokens import TOKEN_TYPE_ACCESS, AuthNotConfigured, InvalidToken, decode_token
from app.models.auth import User, UserStatus
from app.schemas.auth import LoginRequest, LoginResponse, SecondFactorRequest
from app.schemas.envelope import ApiResponse, success
from app.services.auth import (
    REFRESH_COOKIE_NAME,
    REFRESH_COOKIE_PATH,
    IssuedSession,
    RequestContext,
    TokenReused,
    authenticate,
    complete_second_factor,
    logout,
    refresh_session,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


def client_ip(request: Request) -> str | None:
    """The caller's address, honouring `X-Forwarded-For` only from trusted peers.

    ⚠️ **不能直接用 `request.client.host`。**经 nginx 时它对每个请求都是同一个
    值（nginx 的容器地址），后果是按来源限流退化成**全局**限流 —— 任何人发到
    第 21 个认证请求，所有人都拿 429。审计里的地址也会全都一样，取证时没用。
    判定逻辑与「为什么只在可信代理后面才采信 XFF」见 app/core/clientip.py。
    """
    return resolve_client_ip(
        peer=request.client.host if request.client else None,
        forwarded_for=request.headers.get(FORWARDED_FOR_HEADER),
        trusted=getattr(request.app.state, "trusted_proxies", ()),
    )


def request_context(request: Request) -> RequestContext:
    """Who is calling, for the audit trail (spec §66)."""
    return RequestContext(
        ip_address=client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )


def _rate_limit(request: Request) -> None:
    """Apply the in-process backstop. The primary control is nginx's limit_req."""
    bucket: TokenBucket | None = getattr(request.app.state, "auth_rate_limiter", None)
    if bucket is None:
        return
    bucket.check(client_ip(request) or "unknown")


def require_session_factory(request: Request):  # noqa: ANN202 - sessionmaker type is verbose
    factory = getattr(request.app.state, "session_factory", None)
    if factory is None:
        # 与 `/readyz` 的处置一致：没有数据库就明说，不要拿一个假地址去连。
        raise AppError(
            "The service is not ready to authenticate.",
            code="DATABASE_NOT_CONFIGURED",
            http_status=503,
        )
    return factory


def _set_refresh_cookie(response: Response, request: Request, issued: IssuedSession) -> None:
    settings = request.app.state.settings
    response.set_cookie(
        REFRESH_COOKIE_NAME,
        issued.refresh_token,
        # httpOnly：XSS 拿不到刷新令牌。访问令牌则由前端只放在内存里。
        httponly=True,
        # 默认 True。本地跑 http 时要在 .env 里显式关掉 —— 让不安全成为
        # 一次有意识的选择，而不是默认值。
        secure=settings.session_cookie_secure,
        # strict 而不是 lax：刷新端点没有任何跨站使用场景。
        samesite="strict",
        path=REFRESH_COOKIE_PATH,
        max_age=settings.refresh_token_ttl_seconds,
    )


@router.post("/login", response_model=ApiResponse[LoginResponse])
def login(
    request: Request, response: Response, payload: LoginRequest
) -> ApiResponse[LoginResponse]:
    """First step: email + password (spec §53).

    ⚠️ **密码对不等于登进来了。**启用了 2FA、或身为必须启用 2FA 的 ADMIN 时，
    这里只发一张 `pending_token`，客户端要拿它去走 `/login/totp`（或先
    `/2fa/enrol`）。
    """
    _rate_limit(request)
    settings = request.app.state.settings
    outcome = authenticate(
        require_session_factory(request),
        settings,
        email=payload.email,
        password=payload.password,
        context=request_context(request),
    )
    if outcome.issued is None:
        # 停在第二因子那一步：**不发 cookie**，因为还没有会话。
        return success(
            LoginResponse(stage=outcome.stage, pending_token=outcome.pending_token),
            request_id=current_request_id(),
        )

    _set_refresh_cookie(response, request, outcome.issued)
    return success(
        LoginResponse(
            access_token=outcome.issued.access_token,
            token_type="Bearer",
            expires_in=settings.access_token_ttl_seconds,
        ),
        request_id=current_request_id(),
    )


@router.post("/login/totp", response_model=ApiResponse[LoginResponse])
def login_second_factor(
    request: Request, response: Response, payload: SecondFactorRequest
) -> ApiResponse[LoginResponse]:
    """Second step: a TOTP code, or a recovery code if the authenticator is gone."""
    _rate_limit(request)
    settings = request.app.state.settings
    issued, remaining = complete_second_factor(
        require_session_factory(request),
        settings,
        pending_token=payload.pending_token,
        code=payload.code,
        context=request_context(request),
    )
    _set_refresh_cookie(response, request, issued)
    return success(
        LoginResponse(
            access_token=issued.access_token,
            token_type="Bearer",
            expires_in=settings.access_token_ttl_seconds,
            recovery_codes_remaining=remaining,
        ),
        request_id=current_request_id(),
    )


@router.post("/refresh", response_model=ApiResponse[LoginResponse])
def refresh(request: Request, response: Response) -> ApiResponse[LoginResponse]:
    """Rotate the refresh token and mint a new access token."""
    _rate_limit(request)
    settings = request.app.state.settings
    raw = request.cookies.get(REFRESH_COOKIE_NAME)
    if not raw:
        raise TokenReused
    issued = refresh_session(
        require_session_factory(request),
        settings,
        raw_token=raw,
        context=request_context(request),
    )
    _set_refresh_cookie(response, request, issued)
    return success(
        LoginResponse(
            access_token=issued.access_token,
            expires_in=settings.access_token_ttl_seconds,
        ),
        request_id=current_request_id(),
    )


@router.post("/logout", response_model=ApiResponse[dict])
def logout_endpoint(request: Request, response: Response) -> ApiResponse[dict]:
    """Revoke the whole token family. Idempotent — repeating it still returns 200."""
    raw = request.cookies.get(REFRESH_COOKIE_NAME)
    if raw:
        logout(require_session_factory(request), raw_token=raw, context=request_context(request))
    # 无论如何都清 cookie：留着一个已吊销的令牌只会让下次刷新拿到 401。
    response.delete_cookie(REFRESH_COOKIE_NAME, path=REFRESH_COOKIE_PATH)
    return success({}, request_id=current_request_id())


def require_current_user(request: Request) -> User:
    """Resolve the caller from the `Authorization: Bearer` header.

    ⚠️ **主体只从已验签的令牌里取。**请求体、查询串、任何头里的 `user_id`
    一律忽略 —— 那是 Invariant 8（租户不可互访）的地基，主体错了后面每一条
    租户过滤都建在错的东西上。

    ⚠️ `expected_type` 显式写成访问令牌：2FA 的 pending 令牌拿到这里必须被拒，
    否则第二因子等于不存在。
    """
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise InvalidToken

    payload = decode_token(request.app.state.settings, token, expected_type=TOKEN_TYPE_ACCESS)
    factory = require_session_factory(request)
    with factory() as session:
        user = session.get(User, int(payload["sub"]))
        if user is None or user.status is not UserStatus.ACTIVE:
            # 停用的账号必须立刻失效，不能等访问令牌自然过期。
            raise InvalidToken
        session.expunge(user)
        return user


__all__ = [
    "AuthNotConfigured",
    "RateLimited",
    "client_ip",
    "request_context",
    "require_current_user",
    "require_session_factory",
    "router",
]

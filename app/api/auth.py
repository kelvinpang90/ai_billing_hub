"""Authentication endpoints (spec §53; design gate Issue #32 v5).

路径前缀是 `/api/v1/` 而不是裸路径：`deploy/nginx/billing.conf` 把 `/api/` 整段
转给后端且**不改写路径**，所以浏览器、nginx 日志、应用日志里的 uri 是同一个
字符串。改成裸路径就要在边缘加一条规则，三处也就对不上了。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request, Response

from app.core.errors import AppError
from app.core.logging import current_request_id
from app.core.ratelimit import RateLimited, TokenBucket
from app.core.tokens import AuthNotConfigured
from app.schemas.auth import LoginRequest, LoginResponse
from app.schemas.envelope import ApiResponse, success
from app.services.auth import (
    REFRESH_COOKIE_NAME,
    REFRESH_COOKIE_PATH,
    IssuedSession,
    RequestContext,
    TokenReused,
    authenticate,
    logout,
    refresh_session,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


def _context(request: Request) -> RequestContext:
    """Who is calling, for the audit trail and the rate limiter.

    ⚠️ 这里用的是 `request.client.host`，也就是**直连对端**。经过 nginx 时那是
    nginx 的地址，所以按来源限流真正起作用的是边缘那一层（`limit_req`）。
    要让应用侧也看到真实客户端地址，得让边缘写 `X-Forwarded-For` 并在这里信任
    它 —— **而信任转发头必须先限定可信代理**，否则任何人都能伪造来源。
    那属于生产拓扑，归 T0.9，和 `/readyz` 的网段限制是同一条待办。
    """
    return RequestContext(
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )


def _rate_limit(request: Request) -> None:
    """Apply the in-process backstop. The primary control is nginx's limit_req."""
    bucket: TokenBucket | None = getattr(request.app.state, "auth_rate_limiter", None)
    if bucket is None:
        return
    source = request.client.host if request.client else "unknown"
    bucket.check(source)


def _require_session_factory(request: Request):  # noqa: ANN202 - sessionmaker type is verbose
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
    """Email + password (spec §53).

    ⚠️ T0.8a 还没有第二因子，所以这里直接发令牌。T0.8b 接上 TOTP 后，
    ADMIN 必须改成先返回 pending 令牌 —— 这条是 T0.9 的上线前置。
    """
    _rate_limit(request)
    settings = request.app.state.settings
    issued = authenticate(
        _require_session_factory(request),
        settings,
        email=payload.email,
        password=payload.password,
        context=_context(request),
    )
    _set_refresh_cookie(response, request, issued)
    return success(
        LoginResponse(
            access_token=issued.access_token,
            expires_in=settings.access_token_ttl_seconds,
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
        _require_session_factory(request),
        settings,
        raw_token=raw,
        context=_context(request),
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
        logout(_require_session_factory(request), raw_token=raw, context=_context(request))
    # 无论如何都清 cookie：留着一个已吊销的令牌只会让下次刷新拿到 401。
    response.delete_cookie(REFRESH_COOKIE_NAME, path=REFRESH_COOKIE_PATH)
    return success({}, request_id=current_request_id())


__all__ = ["AuthNotConfigured", "RateLimited", "router"]

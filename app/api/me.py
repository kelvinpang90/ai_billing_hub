"""The current user (design gate #368 v12 §2「当前用户」).

⚠️ **刻意不放在 `/api/v1/auth/` 下**：前端对那个前缀下的 401 不触发刷新，访问令牌一过期，每次打开
页面都会被直接踢回登录；那个前缀还受 nginx 的登录限流，每次进页面都调它会撞上限流。放在
`/api/v1/me`，过期时照常走刷新，也不进认证限流区。

任一已登录用户（ADMIN 或 CUSTOMER）都能调；前端据 `role` 分流。
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from app.api.auth import require_current_user, require_session_factory
from app.core.logging import current_request_id
from app.schemas.auth import CurrentUserResponse
from app.schemas.envelope import ApiResponse, success
from app.services.auth import two_factor_confirmed

router = APIRouter(prefix="/api/v1", tags=["me"])


@router.get("/me", response_model=ApiResponse[CurrentUserResponse])
def me(request: Request) -> ApiResponse[CurrentUserResponse]:
    """`{role, email, two_factor_enabled}` — exactly these three (design §6).

    `two_factor_enabled` 只算**已确认**的注册：生成了密钥但没确认的不算。
    """
    user = require_current_user(request)
    with require_session_factory(request)() as session:
        enabled = two_factor_confirmed(session, user.id)
    return success(
        CurrentUserResponse(role=user.role.value, email=user.email, two_factor_enabled=enabled),
        request_id=current_request_id(),
    )

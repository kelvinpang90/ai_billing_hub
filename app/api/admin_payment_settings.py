"""Admin payment settings: read and whole replacement (AIH-TASK-191).

设计闸门 #587 v3 §2「管理端接口」、§4「设置的并发」、§6；契约见 [docs/api.md](../../docs/api.md)
「支付设置（管理端）」。

⚠️ **每个处理函数的第一条语句是 `require_admin(request)`**，与 app/api/admin_customers.py
同一条：鉴权是显式调用，不挂在路由器上。`tests/backend/test_admin_customers_api.py` 从路由表枚举
`/api/v1/admin` 下的全部路由逐个验 401 / 403 且不写库。

⚠️ 路由**总是注册**（app/main.py），与 `payments_enabled` 无关。

`PUT` 的请求体在处理函数**之前**严格校验（422 `VALIDATION_ERROR`）；规则、行缺失与版本冲突由
`update_payment_settings` 抛出（422 `PAYMENT_SETTINGS_INVALID`、503 `PAYMENTS_MISCONFIGURED`、
409 `PAYMENT_SETTINGS_VERSION_CONFLICT`），都是 `AppError`，原样交给错误信封。成功时用服务返回的
新值与当前管理员的邮箱作答，与 `GET` 同一个视图。

⚠️ 这一层不写日志：服务只记管理员用户 id 与新版本号，请求体不进日志（设计 §6）。
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from sqlalchemy import select

from app.api.auth import request_context, require_admin, require_session_factory
from app.core.logging import current_request_id
from app.models.auth import User
from app.schemas.admin_payment_settings import (
    AdminPaymentSettingsView,
    PaymentSettingsUpdateRequest,
    admin_payment_settings_view,
)
from app.schemas.envelope import ApiResponse, success
from app.services.auth import utc_now
from app.services.payment_settings import get_payment_settings, update_payment_settings

router = APIRouter(prefix="/api/v1/admin", tags=["admin"])

_PAYMENT_SETTINGS = "/payment-settings"


@router.get(_PAYMENT_SETTINGS, response_model=ApiResponse[AdminPaymentSettingsView])
def read_payment_settings(request: Request) -> ApiResponse[AdminPaymentSettingsView]:
    """The one row with its version; 503 `PAYMENTS_MISCONFIGURED` when it is missing."""
    require_admin(request)
    with require_session_factory(request)() as session:
        settings = get_payment_settings(session)
        updated_by = None
        if settings.updated_by_user_id is not None:
            updated_by = session.execute(
                select(User.email).where(User.id == settings.updated_by_user_id)
            ).scalar_one()
    view = admin_payment_settings_view(settings, updated_by)
    return success(view, request_id=current_request_id())


@router.put(_PAYMENT_SETTINGS, response_model=ApiResponse[AdminPaymentSettingsView])
def replace_payment_settings(
    request: Request, payload: PaymentSettingsUpdateRequest
) -> ApiResponse[AdminPaymentSettingsView]:
    """Replace every business field when `version` is current; 200 with what `GET` returns."""
    admin = require_admin(request)
    updated = update_payment_settings(
        require_session_factory(request),
        actor=admin,
        values=payload.to_values(),
        expected_version=payload.version,
        context=request_context(request),
        # 服务把它截到整秒（MySQL 的 DATETIME 不存小数秒）。
        now=utc_now(),
    )
    view = admin_payment_settings_view(updated, admin.email)
    return success(view, request_id=current_request_id())


__all__ = ["router"]

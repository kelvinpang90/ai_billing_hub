"""Domain error hierarchy and the handlers that turn errors into §107 envelopes.

规矩只有两条，但都很硬：

1. **失败响应的形状与成功一致**（`{success, data, error, request_id}`），
   客户端不必为错误路径写第二套解析。
2. **绝不外泄内部细节**（§107）。栈、SQL、异常原文只进日志。未预期的异常
   一律返回 `INTERNAL_ERROR` 加一句固定文案——把 `str(exc)` 塞进 message
   是最常见的信息泄漏，那句话里可能有表名、文件路径，甚至密钥。

**集成端点的 `retryable`**（设计闸门 #176 v8 §2）：路径在 `/api/v1/integration/` 下的
**每一个**错误响应都在信封顶层多带一个布尔 `retryable`，包括 FastAPI 自己的 422、框架的
404 / 405 与未预期的 500。集成方只看它决定是否重试，不按 HTTP 状态码推断。取值：错误类上
显式写了 `retryable` 的用它；否则 5xx 为 `true`、其余为 `false`。这是对 §107 信封的只增
扩展，管理端与认证端点的响应不变。
"""

from __future__ import annotations

import logging
from typing import Any, Final

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.logging import current_request_id
from app.core.middleware import REQUEST_ID_HEADER
from app.schemas.envelope import failure

logger = logging.getLogger(__name__)

# 带 `retryable` 的路径前缀。与 deploy/nginx/billing.conf 的集成前缀块是同一个字符串。
INTEGRATION_PREFIX: Final = "/api/v1/integration/"


class AppError(Exception):
    """Base class for errors this application raises deliberately.

    `code` 是对客户端的稳定契约，**子类必须覆盖**；`http_status` 决定状态码。
    后续 Phase 的领域异常（钱包余额不足、定价规则缺失……）都从这里派生，
    这样处理器只需要认一个基类。
    """

    code = "APP_ERROR"
    http_status = status.HTTP_400_BAD_REQUEST

    # 少数错误必须带响应头才有意义（429 的 Retry-After 是标准要求：没有它，
    # 客户端只能瞎猜多久以后重试）。子类覆盖这个属性，处理器统一加上。
    headers: dict[str, str] = {}

    # 只在集成端点的响应里出现（见模块说明）。`None` = 按状态码推：5xx 可重试，其余不可。
    # 每个集成错误码的取值是固定契约（设计 §2 错误表），需要偏离默认的子类显式覆盖。
    retryable: bool | None = None

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        http_status: int | None = None,
        retryable: bool | None = None,
    ):
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        if http_status is not None:
            self.http_status = http_status
        if retryable is not None:
            self.retryable = retryable


def _retryable(request: Request, http_status: int, explicit: bool | None = None) -> bool | None:
    """The `retryable` flag for an integration response, or `None` everywhere else."""
    if not request.url.path.startswith(INTEGRATION_PREFIX):
        return None
    if explicit is not None:
        return explicit
    return http_status >= status.HTTP_500_INTERNAL_SERVER_ERROR


def _envelope(
    request_id: str | None,
    code: str,
    message: str,
    http_status: int,
    headers: dict[str, str] | None = None,
    retryable: bool | None = None,
) -> JSONResponse:
    body = failure(code=code, message=message, request_id=request_id)
    content: dict[str, Any] = body.model_dump()
    if retryable is not None:
        content["retryable"] = retryable
    response = JSONResponse(status_code=http_status, content=content)
    for name, value in (headers or {}).items():
        response.headers[name] = value
    # 响应头也要带 —— 未处理异常的响应由最外层的 ServerErrorMiddleware 产出，
    # **绕过了 RequestContextMiddleware**，指望那边加头是加不上的。而 500 恰恰
    # 是最需要客户端报得出 id 的场合。
    if request_id is not None:
        response.headers[REQUEST_ID_HEADER] = request_id
    return response


async def handle_app_error(request: Request, exc: AppError) -> JSONResponse:
    # 这是「预期内的失败」，warning 就够；用 error 会让真正的故障淹没在噪声里。
    logger.warning("Request failed", extra={"error_code": exc.code, "path": request.url.path})
    return _envelope(
        current_request_id(),
        exc.code,
        exc.message,
        exc.http_status,
        exc.headers,
        _retryable(request, exc.http_status, exc.retryable),
    )


async def handle_http_exception(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    # 404 / 405 这类由框架抛出，仍要走同一个信封，否则客户端要认两种形状。
    return _envelope(
        current_request_id(),
        "HTTP_ERROR",
        str(exc.detail),
        exc.status_code,
        retryable=_retryable(request, exc.status_code),
    )


async def handle_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    # 只回「哪些字段不合法」，不回收到的值 —— 校验失败的请求体里可能有密钥。
    fields = sorted({".".join(str(part) for part in err.get("loc", ())) for err in exc.errors()})
    message = f"Invalid request fields: {', '.join(fields)}" if fields else "Invalid request"
    http_status = status.HTTP_422_UNPROCESSABLE_CONTENT
    return _envelope(
        current_request_id(),
        "VALIDATION_ERROR",
        message,
        http_status,
        retryable=_retryable(request, http_status),
    )


async def handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
    # 栈进日志，不进响应。message 是固定文案，不能是 str(exc)。
    logger.exception("Unhandled exception", extra={"path": request.url.path})
    http_status = status.HTTP_500_INTERNAL_SERVER_ERROR
    return _envelope(
        current_request_id(),
        "INTERNAL_ERROR",
        "An internal error occurred.",
        http_status,
        retryable=_retryable(request, http_status),
    )


def register_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(AppError, handle_app_error)
    app.add_exception_handler(StarletteHTTPException, handle_http_exception)
    app.add_exception_handler(RequestValidationError, handle_validation_error)
    app.add_exception_handler(Exception, handle_unexpected_error)

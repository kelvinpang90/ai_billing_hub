"""Per-request correlation context (spec §94)."""

from __future__ import annotations

import logging
import re
import time
import uuid
from collections.abc import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.core.logging import bind_log_context, clear_log_context

logger = logging.getLogger(__name__)

REQUEST_ID_HEADER = "X-Request-ID"

# 入站 request id 是**不可信输入**：它会进日志，也会回到响应头里。限死长度与
# 字符集，挡住换行注入（伪造日志行）与超长值撑爆日志。不合规就当没给。
_SAFE_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Assign a request id, expose it, and log how the request ended.

    接受上游传来的 id（而不是一律自己生成），是为了让计费平台与 Integrated
    Application Backend 两侧的日志能对上同一次调用——否则跨服务排障只能靠
    时间戳猜。
    """

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        inbound = request.headers.get(REQUEST_ID_HEADER, "")
        request_id = inbound if _SAFE_REQUEST_ID.match(inbound) else str(uuid.uuid4())

        # 每个请求从空上下文开始。不清空的话，复用的 worker 任务会把上一个
        # 请求的 tenant_id 带进这一个，日志归属就错了。
        clear_log_context()
        bind_log_context(request_id=request_id)
        started = time.perf_counter()

        try:
            response = await call_next(request)
        except Exception:
            # 这里只记账，不吞异常 —— 让它继续走到异常处理器去成形为 §107 信封。
            logger.warning(
                "Request raised",
                extra={
                    "method": request.method,
                    "path": request.url.path,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                },
            )
            raise

        response.headers[REQUEST_ID_HEADER] = request_id
        logger.info(
            "Request completed",
            extra={
                "method": request.method,
                "path": request.url.path,
                "status_code": response.status_code,
                "duration_ms": round((time.perf_counter() - started) * 1000, 2),
            },
        )
        return response

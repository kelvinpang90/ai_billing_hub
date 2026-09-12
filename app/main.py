"""FastAPI application factory."""

from __future__ import annotations

import logging

from fastapi import FastAPI

from app.api.auth import router as auth_router
from app.api.health import router as health_router
from app.core.clientip import parse_trusted_proxies
from app.core.config import Settings, get_settings
from app.core.database import (
    DatabaseNotConfigured,
    create_database_engine,
    create_session_factory,
)
from app.core.errors import register_error_handlers
from app.core.logging import configure_logging
from app.core.middleware import RequestContextMiddleware
from app.core.ratelimit import TokenBucket

logger = logging.getLogger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the ASGI app.

    用工厂而不是模块级单例，测试才能拿到互不干扰的实例；API 保持无状态，
    共享状态一律放 Redis / DB（spec §100）。
    """
    settings = settings or get_settings()
    configure_logging(settings.log_level)

    app = FastAPI(
        title="Acuven Central AI Billing Platform",
        debug=settings.debug,
    )
    # 中间件要在异常处理器之前装：处理器要读中间件绑上的 request_id，
    # 才能把它写进 §107 的信封。
    app.add_middleware(RequestContextMiddleware)
    register_error_handlers(app)
    app.include_router(health_router)
    app.include_router(auth_router)

    app.state.settings = settings
    # 进程内限流兜底（主控是 nginx 的 limit_req，见 deploy/nginx/billing.conf）。
    # 每个应用实例一个桶：它是瞬时状态，重启清空可接受 —— 失效模式是退化到
    # nginx 那一层，而不是安全控制消失。
    # 只有直连对端落在这里时才采信 X-Forwarded-For（见 app/core/clientip.py）。
    app.state.trusted_proxies = parse_trusted_proxies(settings.trusted_proxies)
    app.state.auth_rate_limiter = TokenBucket(
        per_minute=settings.auth_rate_limit_per_minute,
        burst=settings.auth_rate_limit_burst,
    )
    if not settings.jwt_secret_file.strip():
        # 与「没配数据库」同一种处置：照常启动让存活探针能应答，但把缺失说清楚。
        # 认证端点会明确返回 AUTH_NOT_CONFIGURED，而不是签出一个临时密钥——
        # 临时密钥会让配置缺失变成静默的。
        logger.warning("Starting without a JWT signing key; authentication will refuse to serve")
    # 没配数据库不等于起不来。**存活探针必须能应答**，`/readyz` 会明确报
    # DATABASE_NOT_CONFIGURED —— 启动时直接崩掉的话，一个配置笔误会让容器
    # 进入重启循环，而日志里只有一行没人看得见的堆栈。
    try:
        engine = create_database_engine(settings)
    except DatabaseNotConfigured:
        logger.warning("Starting without a database; /readyz will report not configured")
        app.state.engine = None
        app.state.session_factory = None
    else:
        app.state.engine = engine
        app.state.session_factory = create_session_factory(engine)
    return app


app = create_app()

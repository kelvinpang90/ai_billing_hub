"""FastAPI application factory."""

from __future__ import annotations

import logging

from fastapi import FastAPI

from app.api.health import router as health_router
from app.core.config import Settings, get_settings
from app.core.database import (
    DatabaseNotConfigured,
    create_database_engine,
    create_session_factory,
)
from app.core.errors import register_error_handlers
from app.core.logging import configure_logging
from app.core.middleware import RequestContextMiddleware

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

    app.state.settings = settings
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

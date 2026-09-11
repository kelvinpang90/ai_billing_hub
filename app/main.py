"""FastAPI application factory."""

from __future__ import annotations

from fastapi import FastAPI

from app.api.health import router as health_router
from app.core.config import Settings, get_settings


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the ASGI app.

    用工厂而不是模块级单例，测试才能拿到互不干扰的实例；API 保持无状态，
    共享状态一律放 Redis / DB（spec §100）。
    """
    settings = settings or get_settings()
    app = FastAPI(
        title="Acuven Central AI Billing Platform",
        debug=settings.debug,
    )
    app.include_router(health_router)
    return app


app = create_app()

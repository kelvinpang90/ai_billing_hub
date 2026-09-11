"""Application settings, read from environment variables.

所有配置只有这一个入口。仓库是公开的，默认值里**不许**出现任何真实主机名、
凭据或密钥 —— 需要密钥的配置项在用到它的那个任务里加，并走 Docker secrets
文件注入（ADR-0004），不走环境变量。
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

Environment = Literal["local", "production"]


class Settings(BaseSettings):
    """Environment-driven settings. See .env.example for the local template."""

    model_config = SettingsConfigDict(
        env_prefix="BILLING_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    environment: Environment = "local"
    debug: bool = False
    log_level: str = "INFO"

    # 空串 = 未配置。默认值里**不许**出现任何真实主机名或凭据（仓库是公开的），
    # 所以这里不能给一个「看起来能用」的默认连接串。未配置时 `/readyz` 会明确
    # 报 DATABASE_NOT_CONFIGURED，而不是拿着假地址去连然后超时。
    database_url: str = ""

    # 同样是空串 = 未配置。⚠️ Redis **不是**就绪阻断项：spec §74.6 规定
    # 「Redis/Celery 只承载投递触发，数据库 Outbox 才是可恢复的事实来源」，
    # REQ-AVAIL-001 又要求 Redis 不可用不得成为终端 AI 请求路径上的同步依赖。
    # 因为 Redis 挂了就把 API 摘出轮转，恰好制造出 Invariant 1 要防的那种中断。
    redis_url: str = ""


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached accessor so settings are parsed once per process."""
    return Settings()

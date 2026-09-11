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


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached accessor so settings are parsed once per process."""
    return Settings()

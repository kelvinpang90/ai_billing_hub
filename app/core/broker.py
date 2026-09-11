"""Redis reachability, for reporting — not for gating.

⚠️ **Redis 不可用绝不能让 `/readyz` 返回 503。**

spec §74.6：「Redis/Celery carries delivery triggers only; database Outbox
state remains the recoverable source of truth」。REQ-AVAIL-001 更直接：Redis
不可用**不得**成为终端 AI 请求路径上的同步依赖。

所以 Redis 挂了的正确行为是：API 照常收用量事件、照常落库、照常返回 202，
投递触发欠着，等 Redis 回来由周期恢复补上（Invariant 14）。**把 Redis 做成
就绪阻断项，等于 Redis 一挂就把所有实例摘出轮转 —— 那正是 Invariant 1 要防的
那种中断，而且是我们自己造出来的。**

这个模块只回答「现在通不通」，让 `/readyz` 把它如实报出来（`degraded`），
由监控告警，不由负载均衡处置。
"""

from __future__ import annotations

import logging
from enum import StrEnum

import redis

from app.core.config import Settings

logger = logging.getLogger(__name__)

# 探针要快。连不上时卡住几秒，就绪检查自己会变成故障放大器。
_PROBE_TIMEOUT_SECONDS = 2


class BrokerStatus(StrEnum):
    OK = "ok"
    NOT_CONFIGURED = "not_configured"
    UNAVAILABLE = "unavailable"


def check_broker(settings: Settings) -> BrokerStatus:
    """Report whether Redis answers. Never raises."""
    if not settings.redis_url:
        return BrokerStatus.NOT_CONFIGURED
    try:
        client = redis.from_url(
            settings.redis_url,
            socket_connect_timeout=_PROBE_TIMEOUT_SECONDS,
            socket_timeout=_PROBE_TIMEOUT_SECONDS,
        )
        client.ping()
    except (redis.RedisError, OSError, ValueError):
        # 连接错误里带着主机名与端口（§94），所以只记日志、不进响应。
        logger.exception("Redis probe failed")
        return BrokerStatus.UNAVAILABLE
    return BrokerStatus.OK

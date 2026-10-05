"""Replay protection for signed integration requests (design gate #176 v8 §2; ADR-0004 §5).

验签通过之后，按 `nonce:{api_key}:{X-Acuven-Request-Id}` 在 Redis 里 `SET … NX PXAT`，
过期时刻 = 请求时间戳 + 300 秒（毫秒）。已存在 = 同一个 Request-Id 第二次出现 = 重放。

⚠️ **Redis 不可用（连不上、超时 200 ms、没配置）时放行**，由调用方写一条 WARNING
（REQ-AVAIL-001：计费侧一个非持久组件的故障不能阻塞上报；ADR-0004 §5 显式接受
Redis 丢失时最多约 10 分钟失去 nonce 层）。**财务安全网是 `usage_events.event_id`
的唯一约束**：被重放的请求最多得到一次「合法重复」，没有第二次效果。

⚠️ 进程级客户端，自带连接池，**不复用** app/core/broker.py 的探活连接：探活每次新建、
超时 2 秒，放在请求路径上会把一次 Redis 故障放大成每个请求多等 2 秒。

`PXAT` 需要 redis-py ≥ 4.0 与 Redis ≥ 6.2：仓库没有锁文件，redis-py 经 `celery[redis]>=5.4`
间接安装，kombu 5.4 的 `redis` extra 要求 4.5.2 以上；compose 里的 Redis 是 `redis:7-alpine`。
两边都支持，所以没有走设计 §10 假设 2 的固定 TTL 退路。

这一层不写日志：放行与否、为什么，由调用方记（它知道请求的上下文）。异常消息里没有
api_key 与 Request-Id。
"""

from __future__ import annotations

from typing import Final

import redis

from app.core.config import Settings

# 与验签时间窗同一个 300 秒：时间戳出窗的请求在验签时就被拒，过期之后的重放
# 也必然出窗，键不需要活得更久。
NONCE_WINDOW_SECONDS: Final = 300
# 连接与读写超时（设计 §2）：Redis 慢了就当它不可用，不让上报路径等。
REDIS_TIMEOUT_SECONDS: Final = 0.2
KEY_PREFIX: Final = "nonce"


class NonceStoreUnavailable(Exception):
    """Redis did not answer (not configured, connection refused, timed out). Not a replay."""


def nonce_key(api_key: str, request_id: str) -> str:
    """`nonce:{api_key}:{request_id}`. Upper bound: 6 + 64 + 1 + 128 characters."""
    return f"{KEY_PREFIX}:{api_key}:{request_id}"


def expires_at_ms(timestamp: int) -> int:
    """The `PXAT` value: the request timestamp plus 300 seconds, in Unix milliseconds."""
    return (timestamp + NONCE_WINDOW_SECONDS) * 1000


class NonceStore:
    """`claim` a Request-Id once. One instance per process (`app.state.nonce_store`)."""

    def __init__(self, client: redis.Redis | None) -> None:
        self._client = client

    @classmethod
    def from_settings(cls, settings: Settings) -> NonceStore:
        """A pooled client on `BILLING_REDIS_URL`; no URL (or a malformed one) = unavailable."""
        if not settings.redis_url:
            return cls(None)
        try:
            client = redis.from_url(
                settings.redis_url,
                socket_timeout=REDIS_TIMEOUT_SECONDS,
                socket_connect_timeout=REDIS_TIMEOUT_SECONDS,
            )
        except ValueError:
            # URL 写错：与没配置同样处置（放行 + 每次 WARNING），不让 API 起不来。
            return cls(None)
        return cls(client)

    def claim(self, api_key: str, request_id: str, timestamp: int) -> bool:
        """`True` the first time this `(api_key, request_id)` is seen; `False` on a replay.

        Raises `NonceStoreUnavailable` when Redis cannot answer; the caller lets the request
        through. `from_url` 不连库，第一次 `set` 才连，所以连不上也在这里体现。
        """
        if self._client is None:
            raise NonceStoreUnavailable("the nonce store is not configured")
        try:
            created = self._client.set(
                nonce_key(api_key, request_id), 1, nx=True, pxat=expires_at_ms(timestamp)
            )
        except (redis.RedisError, OSError) as exc:
            # 异常原文里有主机名与端口（§94），只交给调用方决定记什么。
            raise NonceStoreUnavailable(type(exc).__name__) from None
        # NX：键已存在时返回 None。
        return bool(created)


__all__ = [
    "KEY_PREFIX",
    "NONCE_WINDOW_SECONDS",
    "REDIS_TIMEOUT_SECONDS",
    "NonceStore",
    "NonceStoreUnavailable",
    "expires_at_ms",
    "nonce_key",
]

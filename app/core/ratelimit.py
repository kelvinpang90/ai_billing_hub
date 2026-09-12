"""In-process rate limiting for the auth endpoints (spec §53).

**这是兜底，不是主控。**主控是边缘 nginx 的 `limit_req`（见
`deploy/nginx/billing.conf`）——它在请求到达 Python 之前就挡掉，所以能真正保护
CPU。这一层存在的理由是「有人不经 nginx 直接跑 uvicorn」时，Argon2 的调用次数
仍然有上界。

为什么状态放进程内存，而不放 Redis 或数据库：

- **Redis**：丢失时限流会 fail-open。一个丢了就失效的安全控制比没有更危险——
  它制造「已经限流了」的假象。同一条理由让 `/readyz` 的 Redis 降级不改 503，
  也让访问令牌不用 Redis 黑名单做吊销。
- **数据库**：每个被拒的请求写一次库，等于把 CPU 放大换成数据库写放大。
- **进程内**：不引入任何外部依赖。它的失效模式是**退化到 nginx 那一层**，
  而不是「安全控制消失」。

⚠️ 与 spec §100「no process-local persistent state」不冲突：那一条针对的是持久
状态与会话粘连。这里的桶是纯瞬时的，重启清空不影响正确性，也不要求同一个客户端
每次打到同一个实例。
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict

from app.core.errors import AppError

# 记住多少个来源。⚠️ **必须有上限**：不设上限的话，换着 IP 发请求就能把内存
# 撑爆——限流器自己变成拒绝服务的入口。满了按最久未用淘汰。
_MAX_TRACKED_SOURCES = 10_000


class RateLimited(AppError):
    """The caller exceeded the per-source allowance."""

    def __init__(self, retry_after_seconds: int):
        super().__init__(
            # ⚠️ 文案里不含任何账号信息：限流按来源计数，与邮箱是否存在无关。
            # 泄露一个字都会把它变成用户枚举通道。
            "Too many requests. Try again later.",
            code="TOO_MANY_REQUESTS",
            http_status=429,
        )
        self.retry_after_seconds = retry_after_seconds
        # 没有 Retry-After，客户端只能瞎猜多久以后重试。
        self.headers = {"Retry-After": str(retry_after_seconds)}


class TokenBucket:
    """A fixed-capacity bucket per source, refilled at a steady rate.

    用令牌桶而不是固定窗口计数：固定窗口在窗口边界上允许两倍突发（窗口末尾用满
    + 新窗口开头再用满），而登录端点恰恰是突发最该被压住的地方。
    """

    def __init__(self, *, per_minute: int, burst: int) -> None:
        if per_minute <= 0 or burst <= 0:
            raise ValueError("per_minute and burst must be positive")
        self._refill_per_second = per_minute / 60.0
        self._capacity = float(burst)
        self._buckets: OrderedDict[str, tuple[float, float]] = OrderedDict()
        # 多个 worker 线程会同时碰它（Starlette 的同步端点跑在线程池里）。
        self._lock = threading.Lock()

    def check(self, source: str, *, now: float | None = None) -> None:
        """Consume one token for `source`, or raise :class:`RateLimited`."""
        current = time.monotonic() if now is None else now
        with self._lock:
            tokens, last_seen = self._buckets.get(source, (self._capacity, current))
            tokens = min(self._capacity, tokens + (current - last_seen) * self._refill_per_second)

            if tokens < 1.0:
                # 放回去，否则被拒的请求不会推进 last_seen，桶永远不回填。
                self._buckets[source] = (tokens, current)
                self._buckets.move_to_end(source)
                deficit = 1.0 - tokens
                raise RateLimited(max(1, int(deficit / self._refill_per_second) + 1))

            self._buckets[source] = (tokens - 1.0, current)
            self._buckets.move_to_end(source)
            while len(self._buckets) > _MAX_TRACKED_SOURCES:
                self._buckets.popitem(last=False)

    def reset(self) -> None:
        """Drop all state. Only for tests."""
        with self._lock:
            self._buckets.clear()

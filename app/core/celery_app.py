"""Celery application factory.

**Redis 只承载投递触发，数据库才是事实来源**（spec §74.6：「Redis/Celery
carries delivery triggers only; database Outbox state remains the recoverable
source of truth」）。下面每一条默认值都是从这句话和 Invariant 14 推出来的，
不是抄的模板：

- **不存任务结果**（`task_ignore_result`）。结果放 Redis 会让人依赖它做判断，
  而 Redis 是可丢的 —— Invariant 14 要求丢了队列也不能摧毁已持久化的财务工作。
  任务的产出必须落库，落不了库就是这个任务没做完。
- **`task_acks_late` + `task_reject_on_worker_lost`**：worker 被 kill 时消息
  回到队列重投，而不是「已 ack 但没做完」。代价是任务必须幂等 —— 对这个系统
  本来就是硬要求（Invariant 2/3）。
- **`worker_prefetch_multiplier = 1`**：配合 acks_late。预取多了，一个 worker
  崩掉会让一批消息同时重投，放大重复。
- **只收 JSON**。pickle 反序列化等于允许 broker 上的任意代码执行 —— broker
  被攻破时，那是从「能读队列」直接升级成「能在 worker 里跑代码」。
- **UTC**（spec §109）。beat 的调度时刻要和账本时间同一个基准。
"""

from __future__ import annotations

from celery import Celery

from app.core.config import Settings

# 任务模块要显式列出来，不用 autodiscover：autodiscover 靠约定扫包，加错目录
# 或改了包名时它只是**安静地少注册一个任务**，调用方拿到 NotRegistered 才发现。
TASK_MODULES = ["app.tasks.ping"]


class RedisNotConfigured(RuntimeError):
    """No broker URL configured; the worker cannot start."""


def create_celery_app(settings: Settings) -> Celery:
    if not settings.redis_url:
        raise RedisNotConfigured("BILLING_REDIS_URL is not configured.")

    app = Celery("acuven_billing", broker=settings.redis_url, include=TASK_MODULES)
    app.conf.update(
        task_ignore_result=True,
        result_backend=None,
        task_serializer="json",
        result_serializer="json",
        accept_content=["json"],
        task_acks_late=True,
        task_reject_on_worker_lost=True,
        worker_prefetch_multiplier=1,
        timezone="UTC",
        enable_utc=True,
        # broker 启动时不可用不该让 worker 直接退出 —— 容器编排下那只是
        # 无限重启；重试等它起来才是对的。
        broker_connection_retry_on_startup=True,
        # ⚠️ Celery 默认**劫持 root logger**，把 T0.3 装的 JSON handler 顶掉。
        # 不关掉的话，worker 打的是纯文本、API 打的是 JSON，集中日志里两半
        # 对不上，而且 worker 那半**完全不过脱敏**（§94）。这是实测发现的：
        # 起一次真 worker 才看得见，单元测试看不见。
        worker_hijack_root_logger=False,
        # 同理：默认会把 worker 的 stdout/stderr 重定向进 logger，print 出来的
        # 东西会绕过我们的 formatter。
        worker_redirect_stdouts=False,
        # 周期任务在需要它们的那个任务里注册（对账扫描、状态轮询……）。
        # 这里留空而不是先塞几个示例条目 —— 示例条目会被真的跑起来。
        beat_schedule={},
    )
    return app

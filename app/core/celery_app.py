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
TASK_MODULES = ["app.tasks.ping", "app.tasks.outbox"]

# Outbox 的周期恢复（spec §25、§98.1；Invariant 14）。
# ⚠️ **这一条不是优化，是可恢复性本身。**没有它，「Redis 被清空」或者「worker 在
# 触发之后、投递之前挂掉」都会让那些行永远躺在库里：状态 PENDING、谁也不再看
# 它们一眼，而用户那边只表现为「没收到信」。
# 60 秒是与「用户点了忘记密码之后能接受等多久」对齐的 —— 正常路径由 API 直接
# 触发，这一条是兜底。
OUTBOX_RECOVERY_SECONDS = 60.0


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
        # ⚠️ 每派发一次就把调度状态落盘（默认是**每 3 分钟**才同步一次）。
        #
        # 这不是为了持久化，是为了**让 beat 有一个可探测的存活信号**：beat 自己
        # 不接受 `celery inspect ping`（那问的是 worker），而它崩掉是**静默故障** ——
        # 周期任务全停、API 一切正常、没有任何报错。改成每次派发都落盘之后，
        # `beat-schedule` 文件的 mtime 就成了「beat 上一次真的干活是什么时候」，
        # 容器健康检查拿它做判据（见 docker-compose.yml 的 celery-beat）。
        #
        # ⚠️ 默认值下这个信号**不够用**：实测连续观察三个 60 秒周期，mtime 停在
        # 第一次同步的时刻不动。拿默认行为做探针，等于给它一个 3 分钟的盲区，
        # 而且那个盲区的宽度是 celery 的内部默认值，我们改不了也看不见。
        beat_sync_every=1,
        # 周期任务在需要它们的那个任务里注册（对账扫描、状态轮询……）。
        # **不放示例条目** —— 示例条目会被真的跑起来。
        beat_schedule={
            "outbox-recovery": {
                "task": "app.tasks.outbox.recover",
                "schedule": OUTBOX_RECOVERY_SECONDS,
                # ⚠️ `expires` 比周期略短：beat 停了一小时再起来时，**不要**把
                # 攒下的几十次触发一口气全放出去 —— 它们做的是同一件事，
                # 只会让 worker 白白抢同一批行。
                "options": {"expires": OUTBOX_RECOVERY_SECONDS * 0.9},
            }
        },
    )
    return app

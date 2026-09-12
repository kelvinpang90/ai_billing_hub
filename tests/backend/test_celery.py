"""Celery wiring and the defaults that protect the financial invariants."""

from __future__ import annotations

import os

import pytest

from app.core.celery_app import TASK_MODULES, RedisNotConfigured, create_celery_app
from app.core.config import Settings
from app.tasks.ping import ping

MEMORY_BROKER = "memory://"
TEST_REDIS_URL = os.environ.get("BILLING_TEST_REDIS_URL", "")

needs_redis = pytest.mark.skipif(
    not TEST_REDIS_URL,
    reason="BILLING_TEST_REDIS_URL is not set; broker tests need a real Redis",
)


@pytest.fixture
def celery_app():
    return create_celery_app(Settings(redis_url=MEMORY_BROKER))


def test_worker_refuses_to_start_without_a_broker() -> None:
    """API 没配数据库要能起（存活探针），worker 没配 broker 不能起 —— 它没有存在意义。"""
    with pytest.raises(RedisNotConfigured):
        create_celery_app(Settings(redis_url=""))


def test_results_are_not_stored_in_redis(celery_app) -> None:
    """Invariant 14：丢了 Redis 不能摧毁已持久化的财务工作。

    结果放 Redis 会让人依赖它做判断，而 Redis 是可丢的。任务的产出必须落库，
    落不了库就是这个任务没做完。
    """
    assert celery_app.conf.task_ignore_result is True
    assert celery_app.conf.result_backend is None


def test_worker_crash_redelivers_instead_of_losing_the_message(celery_app) -> None:
    """acks_late + reject_on_worker_lost：被 kill 时消息回队列，不是「已 ack 没做完」。

    代价是任务必须幂等 —— 对这个系统本来就是硬要求（Invariant 2/3）。
    """
    assert celery_app.conf.task_acks_late is True
    assert celery_app.conf.task_reject_on_worker_lost is True
    # 配合 acks_late：预取多了，一个 worker 崩掉会让一批消息同时重投。
    assert celery_app.conf.worker_prefetch_multiplier == 1


def test_pickle_is_not_accepted(celery_app) -> None:
    """pickle 反序列化 = 允许 broker 上的任意代码在 worker 里执行。"""
    assert celery_app.conf.accept_content == ["json"]
    assert celery_app.conf.task_serializer == "json"
    assert "pickle" not in celery_app.conf.accept_content


def test_worker_does_not_hijack_our_json_logging(celery_app) -> None:
    """Celery 默认劫持 root logger，把 T0.3 装的 JSON handler 顶掉。

    不关的话，worker 打纯文本、API 打 JSON，集中日志里两半对不上，**而且
    worker 那半完全不过脱敏**（§94）。这是起一次真 worker 才看得见的。
    """
    assert celery_app.conf.worker_hijack_root_logger is False
    assert celery_app.conf.worker_redirect_stdouts is False


def test_schedule_runs_on_utc(celery_app) -> None:
    """spec §109：时间戳存 UTC。beat 的调度时刻要和账本时间同一个基准。"""
    assert celery_app.conf.timezone == "UTC"
    assert celery_app.conf.enable_utc is True


def test_the_only_periodic_task_is_the_outbox_sweep(celery_app) -> None:
    """周期条目只放**真的需要跑**的那些，不放示例条目（示例条目会被真的跑起来）。

    ⚠️ T0.8d 之前这里断言的是「beat 为空」。现在有一条了，而它不是可选的：
    没有这个扫描，Redis 一丢、或者 worker 在触发之后挂掉，那些 outbox 行就
    **永远躺在库里**没人再看一眼（Invariant 14），而用户那边只表现为没收到信。
    """
    assert set(celery_app.conf.beat_schedule) == {"outbox-recovery"}
    entry = celery_app.conf.beat_schedule["outbox-recovery"]
    assert entry["task"] == "app.tasks.outbox.recover"


def test_the_sweep_does_not_pile_up_while_beat_is_down(celery_app) -> None:
    """⚠️ beat 停了一小时再起来时，攒下的几十次触发**不该**一口气全放出去。

    它们做的是同一件事，只会让 worker 抢同一批行。`expires` 短于周期就让过期的
    那些自己消失 —— 没有它，一次 beat 重启会制造一波自己打自己的重复投递。
    """
    entry = celery_app.conf.beat_schedule["outbox-recovery"]

    assert entry["options"]["expires"] < entry["schedule"]


def test_task_modules_are_listed_explicitly(celery_app) -> None:
    """不用 autodiscover：它靠约定扫包，改了包名时只是**安静地少注册一个任务**。"""
    assert TASK_MODULES == ["app.tasks.ping", "app.tasks.outbox"]
    assert celery_app.conf.include == TASK_MODULES


def test_ping_returns_a_fresh_utc_timestamp() -> None:
    first = ping()

    assert first.endswith("+00:00")
    assert ping() >= first


@needs_redis
def test_dispatching_reaches_a_real_redis_queue() -> None:
    """eager 模式跑不到的那一半：序列化、连接、入队。

    不起 worker —— 只验消息**进得去队列**。worker 消费是 T0.6 起完整栈之后
    的事；这里要挡的是「配线错了，任务安静地没被投出去」。
    """
    import redis

    client = redis.from_url(TEST_REDIS_URL)
    queue = "t05-probe"
    client.delete(queue)

    app = create_celery_app(Settings(redis_url=TEST_REDIS_URL))
    app.send_task("app.tasks.ping", queue=queue)

    assert client.llen(queue) == 1
    client.delete(queue)

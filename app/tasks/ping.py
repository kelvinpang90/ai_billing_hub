"""A task that proves the wiring works, and nothing else.

存在的理由：Celery 的配线错了（任务没注册、队列名不对、序列化器不匹配）**不会
在启动时报错**，只会在第一个真任务被丢进队列后安静地不执行。有一个最小任务
能端到端跑通，才谈得上排查后面那些真任务。

它**不碰数据库、不碰钱**。往这里加业务逻辑就失去了「探活」的意义。
"""

from __future__ import annotations

import datetime as dt

from celery import shared_task


@shared_task(name="app.tasks.ping")
def ping() -> str:
    """Return the current UTC timestamp so a caller can tell a fresh run from a cached one."""
    return dt.datetime.now(dt.UTC).isoformat()

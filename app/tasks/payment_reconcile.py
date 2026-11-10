"""Payment reconciliation task (design gate #585 v3 §2「补对账」, AIH-TASK-180).

**数据库是事实来源，Beat 只是触发**（INV-14）：Beat 每 `payment_reconcile_poll_seconds`（缺省
60 秒）调 `run_due`，扫库找到期的 `PENDING`；Redis 丢了、worker 在处理中途挂了，下一轮按候选查询
重来。规则都在 app/services/payment_reconcile.py。

一轮：

1. 开关为假或 `get_payment_gateway` 返回 `None`（凭据读不到，告警沿用 A）：直接返回；
2. 取 MySQL 命名锁 `GET_LOCK(CONCAT('payment_reconcile:', DATABASE()), 0)`（照
   app/tasks/payment_events.py 与 app/ai_api/delivery.py；SQLite 下跳过），取不到直接返回：同一时刻
   只跑一份；结束时释放；
3. 服务的 `run_due`（预算 45 秒、最多 50 次 Stripe 查询）。
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache
from typing import Final

from celery import shared_task
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings
from app.core.database import create_database_engine, create_session_factory
from app.payments import get_payment_gateway
from app.services import payment_reconcile as service
from app.services.payment_reconcile import ReconcileRun

logger = logging.getLogger(__name__)

_LOCK_SQL: Final = "SELECT GET_LOCK(CONCAT('payment_reconcile:', DATABASE()), 0)"
_UNLOCK_SQL: Final = "SELECT RELEASE_LOCK(CONCAT('payment_reconcile:', DATABASE()))"


@lru_cache(maxsize=1)
def _session_factory() -> sessionmaker[Session]:
    """One engine per worker process (同 app/tasks/outbox.py)."""
    return create_session_factory(create_database_engine(get_settings()))


@shared_task(name="app.tasks.payment_reconcile.run_due")
def run_due(*, now: dt.datetime | None = None) -> ReconcileRun | None:
    """One reconciliation run.

    `None`: payments are off, unconfigured, or another run holds the lock.
    `now` 为 `None` 时服务在本轮开始读一次当前 UTC 时间；测试注入。
    """
    settings = get_settings()
    if not settings.payments_enabled:
        return None
    gateway = get_payment_gateway(settings)
    if gateway is None:
        # 凭据读不到：`read_stripe_credentials` 已记 ERROR，告警见 `wallet_negative_balance`。
        return None
    factory = _session_factory()
    with _run_lock(factory) as locked:
        if not locked:
            logger.info("Payment reconciliation is already running elsewhere")
            return None
        return service.run_due(factory, gateway, now=now)


@contextmanager
def _run_lock(factory: sessionmaker[Session]) -> Iterator[bool]:
    """MySQL named lock: only one run at a time. SQLite: skipped.

    照 app/tasks/payment_events.py。
    """
    engine: Engine = factory.kw["bind"]
    if engine.dialect.name == "sqlite":
        yield True
        return
    connection = engine.connect()
    acquired = False
    try:
        acquired = connection.scalar(text(_LOCK_SQL)) == 1
        yield acquired
    finally:
        if acquired:
            connection.execute(text(_UNLOCK_SQL))
        connection.close()


__all__ = ["run_due"]

"""Status webhook dispatch, every 15 seconds (design gate #241 v6 §2, AIH-TASK-051).

**数据库是事实来源，Beat 只是触发**（INV-14）：
待推送的状态完全由 MySQL 推导（版本差 + 投递行），不靠任何队列消息携带状态。
Redis 丢了、Beat 停了，恢复后第一轮 dispatch 全表扫描到期行，照常补发。

重叠运行是安全的：领取是 `SKIP LOCKED` 加单行租约，结算核对租约令牌。逻辑全在
app/services/status_delivery.py，这里只取配置、建连接池、记一行日志（不含地址）。
"""

from __future__ import annotations

import logging
from functools import lru_cache

from celery import shared_task
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings
from app.core.database import create_database_engine, create_session_factory
from app.services import status_delivery as delivery

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def _session_factory() -> sessionmaker[Session]:
    """One engine per worker process (同 app/tasks/outbox.py)."""
    return create_session_factory(create_database_engine(get_settings()))


@shared_task(name="app.tasks.status_webhook.dispatch")
def dispatch() -> int:
    """Claim, send and settle due deliveries for up to 12 seconds. Returns how many."""
    result = delivery.dispatch(_session_factory(), get_settings())
    if result.claimed:
        logger.info(
            "Dispatched status webhooks",
            extra={"claimed": result.claimed, **dict(sorted(result.outcomes.items()))},
        )
    return result.claimed


__all__ = ["dispatch"]

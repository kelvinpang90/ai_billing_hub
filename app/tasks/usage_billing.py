"""Usage billing: the 10-second sweep and the 60-second stale recovery (design gate #181 v2 §2).

**数据库是事实来源，Beat 只是触发**（REQ-INGEST-002、INV-14）：摄取端不入队，待办清单就是库里的
`RECEIVED` / 到期的 `FAILED_RETRYABLE` / 租约过期的 `PROCESSING`。队列丢了、Beat 停了，恢复后
第一轮扫描与回收接着做，什么都不丢；停摆期间 `usage_processing_backlog` 告警。

重叠运行是安全的：认领与回收都是 `SKIP LOCKED`，处理事务核对防护令牌，账本
`(USAGE_EVENT, event_id)` 唯一约束兜底。逻辑全在 app/services/usage_billing.py，这里只取配置、
建连接池、记一行日志。
"""

from __future__ import annotations

import logging
from functools import lru_cache

from celery import shared_task
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings
from app.core.database import create_database_engine, create_session_factory
from app.services import usage_billing as billing

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def _session_factory() -> sessionmaker[Session]:
    """One engine per worker process, of its own: billing sets a session-level lock wait
    timeout (10 seconds) on its connections, and they never serve anything else."""
    return create_session_factory(create_database_engine(get_settings()))


@shared_task(name="app.tasks.usage_billing.bill_pending_events")
def bill_pending_events() -> int:
    """Claim and bill due events for up to 8 seconds. Returns how many were claimed."""
    settings = get_settings()
    result = billing.bill_pending_events(
        _session_factory(),
        lease_seconds=settings.usage_lease_seconds,
        max_attempts=settings.usage_max_attempts,
    )
    if result.claimed:
        counts = {outcome.value: count for outcome, count in sorted(result.outcomes.items())}
        logger.info("Billed claimed usage events", extra={"claimed": result.claimed, **counts})
    return result.claimed


@shared_task(name="app.tasks.usage_billing.recover_stale_processing")
def recover_stale_processing() -> int:
    """Release PROCESSING events whose lease expired (§110). Returns how many."""
    settings = get_settings()
    recovered = billing.recover_stale_processing(
        _session_factory(), max_attempts=settings.usage_max_attempts
    )
    if recovered:
        logger.warning("Recovered stale PROCESSING usage events", extra={"count": recovered})
    return recovered


__all__ = ["bill_pending_events", "recover_stale_processing"]

"""Monthly statements, generated every hour on the hour (design gate #565 v6 §2, AIH-TASK-157).

**数据库是事实来源，Beat 只是触发**（INV-14）：每一轮都从 `FIRST_STATEMENT_PERIOD` 扫到最近
一个到期的账期，缺什么补什么。Redis 丢了、Beat 停了，下一个整点补上。首个账期 `2026-12`
在 2027-01-02 01:00（马来西亚时间）才到期，之前每一轮都是空跑。

重叠运行是安全的：`(tenant_id, period_start)` 唯一约束决出一份，输家当作成功。逻辑全在
app/services/statements.py，这里只建连接池、取当前时刻、记一行日志。
"""

from __future__ import annotations

import logging
from functools import lru_cache

from celery import shared_task
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings
from app.core.database import create_database_engine, create_session_factory
from app.services import statements
from app.services.auth import utc_now

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def _session_factory() -> sessionmaker[Session]:
    """One engine per worker process (同 app/tasks/outbox.py)."""
    return create_session_factory(create_database_engine(get_settings()))


@shared_task(name="app.tasks.statements.generate_due")
def generate_due() -> int:
    """Generate every due statement the database lacks. Returns how many were generated."""
    # MySQL 的 DATETIME 不存小数秒：`created_at` / `finalized_at` 截到整秒。
    generated = statements.generate_due(_session_factory(), utc_now().replace(microsecond=0))
    logger.info("Generated due monthly statements", extra={"count": generated})
    return generated


__all__ = ["generate_due"]

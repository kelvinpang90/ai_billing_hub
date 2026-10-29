"""Wallet reconciliation, one step every 300 seconds (design gate #474 v13 §2, AIH-TASK-132).

**数据库是事实来源，Beat 只是触发**（INV-14）：运行、游标与租约都在
`wallet_reconciliation_runs` 里。Redis 丢了、Beat 停了，恢复后的第一次触发从库里的游标继续。

重叠运行是安全的：推进先领租约，领不到返回 `busy`。逻辑全在
app/services/wallet_reconciliation.py，这里只建连接池；不另记日志（设计 §6 只有两种日志，
都由服务层记）。
"""

from __future__ import annotations

from functools import lru_cache

from celery import shared_task
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings
from app.core.database import create_database_engine, create_session_factory
from app.services import wallet_reconciliation as reconciliation


@lru_cache(maxsize=1)
def _session_factory() -> sessionmaker[Session]:
    """One engine per worker process (同 app/tasks/outbox.py)."""
    return create_session_factory(create_database_engine(get_settings()))


@shared_task(name="app.tasks.wallet_reconciliation.step")
def step() -> str:
    """Retire, start, claim, scan for up to 20 seconds and commit. Never raises."""
    return reconciliation.step(_session_factory())


__all__ = ["step"]

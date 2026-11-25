"""Daily retention purge (design gate #833 v7 §2「任务与配置」, AIH-TASK-264).

Beat 每天 20:17 UTC（马来西亚 04:17）触发一次 `purge`：

1. 读三项开关（`retention_<项名>_mode`）；三项都 `off`（缺省）时直接返回 0，不碰数据库；
2. 取 MySQL 命名锁 `GET_LOCK(CONCAT('retention_purge:', DATABASE()), 0)`
   （照 app/tasks/payment_events.py；SQLite 下跳过），取不到记一行 INFO、返回 0：另一个实例正在跑；
3. 调 `run_purge`（app/services/retention_purge.py，AIH-TASK-263），返回删除总数。

每项的 INFO 与出错的 ERROR 日志由 `run_purge` 写，这里不重复记。规则可重放（INV-14）：漏跑一天、
中途出错，下一轮按同样的条件继续。
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

from app.core.config import Settings, get_settings
from app.core.database import create_database_engine, create_session_factory
from app.services.auth import utc_now
from app.services.retention_purge import ITEMS, Mode, run_purge

logger = logging.getLogger(__name__)

_LOCK_SQL: Final = "SELECT GET_LOCK(CONCAT('retention_purge:', DATABASE()), 0)"
_UNLOCK_SQL: Final = "SELECT RELEASE_LOCK(CONCAT('retention_purge:', DATABASE()))"


@lru_cache(maxsize=1)
def _session_factory() -> sessionmaker[Session]:
    """One engine per worker process (同 app/tasks/statements.py)."""
    return create_session_factory(create_database_engine(get_settings()))


def _modes(settings: Settings) -> dict[str, Mode]:
    # 配置键是 `retention_<项名>_mode`（app/services/retention_purge.py 的 `PurgeItem.name`）。
    return {item.name: getattr(settings, f"retention_{item.name}_mode") for item in ITEMS}


@shared_task(name="app.tasks.retention.purge")
def purge(*, now: dt.datetime | None = None) -> int:
    """Run one retention purge round. Returns how many rows were deleted.

    `now` 为 `None` 时取当前 UTC 时刻；传入时用它（测试注入）。都截到整秒，整轮共用一个截止时刻。
    """
    settings = get_settings()
    modes = _modes(settings)
    if all(mode == "off" for mode in modes.values()):
        return 0
    factory = _session_factory()
    with _run_lock(factory) as locked:
        if not locked:
            logger.info("retention_purge_already_running")
            return 0
        # MySQL 的 DATETIME 不存小数秒。
        moment = (now if now is not None else utc_now()).replace(microsecond=0)
        result = run_purge(
            factory,
            modes=modes,
            max_rows_per_item=settings.retention_max_rows_per_item,
            now=moment,
        )
    return result.deleted


@contextmanager
def _run_lock(factory: sessionmaker[Session]) -> Iterator[bool]:
    """MySQL named lock: only one run at a time. SQLite: skipped.

    写法照 app/tasks/payment_events.py。
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


__all__ = ["purge"]

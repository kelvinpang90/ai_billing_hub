"""Engine, session lifecycle and the readiness probe.

**同步 SQLAlchemy，不是 async。**理由有二：Celery worker 是同步的，两边共用
一套仓储代码才不会写两遍；钱包变更要 `SELECT ... FOR UPDATE`（§81），同步
写法里事务边界一眼看得出来。FastAPI 会把同步依赖丢进线程池，不会阻塞事件循环。

会话的生命周期规矩只有一条：**谁开谁关，异常一律回滚。**`session_scope()` 是
唯一的入口，不要在别处自己 `Session()` —— 漏掉 `rollback()` 的连接会带着脏
事务回到连接池，下一个请求拿到它才报错，现场已经没了。
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager

from fastapi import status
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine.url import make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import QueuePool

from app.core.config import Settings
from app.core.errors import AppError

logger = logging.getLogger(__name__)

# MySQL 默认 8 小时后掐掉空闲连接，池子里那条连接不会知道。pre_ping 在借出前
# 探一次、recycle 主动换掉老连接 —— 少了这两样，夜里没流量之后的第一个请求
# 必定报 "MySQL server has gone away"。
_POOL_RECYCLE_SECONDS = 1800


class DatabaseNotConfigured(AppError):
    """No database URL is configured at all."""

    code = "DATABASE_NOT_CONFIGURED"
    http_status = status.HTTP_503_SERVICE_UNAVAILABLE


class DatabaseUnavailable(AppError):
    """Configured, but the database did not answer."""

    code = "DATABASE_UNAVAILABLE"
    http_status = status.HTTP_503_SERVICE_UNAVAILABLE


def create_database_engine(settings: Settings) -> Engine:
    """Build the engine. Empty URL means "not configured" and is an error here."""
    if not settings.database_url:
        raise DatabaseNotConfigured("Database URL is not configured.")
    return create_engine(
        settings.database_url,
        pool_pre_ping=True,
        pool_recycle=_POOL_RECYCLE_SECONDS,
        future=True,
        **_pool_options(settings),
    )


def _pool_options(settings: Settings) -> dict[str, int]:
    """池的三个参数，**只在池类真的接受它们时才传**。

    取值理由在 `app/core/config.py` 那几行注释里，实测数据在
    [perf-baseline.md](../../docs/perf-baseline.md)。其中 `pool_timeout` 是
    T0.10 唯一改了行为的一个：SQLAlchemy 默认 30 秒，池满时请求会在那里干等
    半分钟 —— 对 HTTP 接口来说，等 30 秒再成功比立刻失败更糟。

    ⚠️ 这个判断不是多余的。只有 `QueuePool` 一族认 `max_overflow` /
    `pool_timeout`；内存 SQLite 用的是 `SingletonThreadPool`，把这几个参数传给它
    的话 `create_engine` 直接抛 `TypeError`。**用方言名写死（「以 sqlite 开头就
    跳过」）同样不行** —— 那是在猜哪些方言用哪种池，而 SQLAlchemy 自己就能回答。
    """
    url = make_url(settings.database_url)
    if not issubclass(url.get_dialect().get_pool_class(url), QueuePool):
        return {}
    return {
        "pool_size": settings.database_pool_size,
        "max_overflow": settings.database_pool_max_overflow,
        "pool_timeout": settings.database_pool_timeout_seconds,
    }


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    # expire_on_commit=False：提交后还要读刚写的对象是常态（比如把新账本行
    # 回给调用方），默认行为会为此再查一次库。
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


@contextmanager
def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    """One transaction. Commit on success, roll back on any exception."""
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def check_database(engine: Engine | None) -> None:
    """Readiness probe. Raises an AppError the handlers already know how to render.

    只回「能不能用」，**不回为什么**：连接错误里带着主机名、端口、有时还有
    用户名，那是 §94 不许外泄的东西。详细原因进日志。
    """
    if engine is None:
        raise DatabaseNotConfigured("Database URL is not configured.")
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except SQLAlchemyError:
        logger.exception("Database readiness probe failed")
        raise DatabaseUnavailable("Database is not reachable.") from None

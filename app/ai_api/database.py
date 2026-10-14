"""Database resources owned by the AI module."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from threading import Lock
from typing import ParamSpec, TypeVar

from sqlalchemy import Engine, create_engine
from sqlalchemy.engine.url import make_url
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import QueuePool

_P = ParamSpec("_P")
_T = TypeVar("_T")


class AiDatabaseCapacityExceeded(Exception):
    """The bounded AI database executor cannot accept another operation."""


def create_ai_database_engine(
    database_url: str,
    *,
    threads: int,
    pool_timeout_seconds: float,
    connect_timeout_seconds: float,
    rw_timeout_seconds: float,
) -> Engine:
    """Make an engine with an isolated, fixed-size AI connection pool."""
    url = make_url(database_url)
    options: dict[str, object] = {
        "pool_pre_ping": True,
        "pool_recycle": 1800,
        "future": True,
        "hide_parameters": True,
    }
    if issubclass(url.get_dialect().get_pool_class(url), QueuePool):
        options.update(pool_size=threads, max_overflow=0, pool_timeout=pool_timeout_seconds)
    if url.drivername.startswith("mysql"):
        options["connect_args"] = {
            "connect_timeout": connect_timeout_seconds,
            "read_timeout": rw_timeout_seconds,
            "write_timeout": rw_timeout_seconds,
        }
    return create_engine(database_url, **options)


def create_ai_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


class AiDatabaseExecutor:
    """Fixed workers and bounded admission, released only by the real future."""

    def __init__(self, *, threads: int, queue_max: int) -> None:
        self._executor = ThreadPoolExecutor(max_workers=threads, thread_name_prefix="ai-db")
        self._limit = threads + queue_max
        self._active = 0
        self._lock = Lock()

    def submit(
        self, function: Callable[_P, _T], /, *args: _P.args, **kwargs: _P.kwargs
    ) -> Future[_T]:
        with self._lock:
            if self._active >= self._limit:
                raise AiDatabaseCapacityExceeded
            self._active += 1
        try:
            future = self._executor.submit(function, *args, **kwargs)
        except BaseException:
            with self._lock:
                self._active -= 1
            raise

        def release(_: Future[_T]) -> None:
            with self._lock:
                self._active -= 1

        future.add_done_callback(release)
        return future

    async def run(self, function: Callable[_P, _T], /, *args: _P.args, **kwargs: _P.kwargs) -> _T:
        future = self.submit(function, *args, **kwargs)
        try:
            return await asyncio.wrap_future(future)
        except asyncio.CancelledError:
            future.cancel()
            raise

    async def run_with_timeout(
        self,
        timeout_seconds: float,
        function: Callable[_P, _T],
        /,
        *args: _P.args,
        **kwargs: _P.kwargs,
    ) -> _T:
        """Wait only up to the wall-clock budget; running work keeps its admission slot."""
        async with asyncio.timeout(timeout_seconds):
            return await self.run(function, *args, **kwargs)

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)

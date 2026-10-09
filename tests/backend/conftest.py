"""CI-only test plumbing: one MySQL database per pytest-xdist worker, and sharding across jobs.

两件事都只在对应环境变量存在时生效；本地直接 `python -m pytest` 时什么都不做。
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url


def pytest_configure(config) -> None:
    """Give every xdist worker its own MySQL database.

    MySQL 用例都在同一个库上「升到 head → 清表 → 断言」，迁移用例还会 downgrade 到 base。
    多个 worker 共用一个库会互相踩，所以并行时每个 worker 改用 `<库名>_<worker id>`。
    用例模块在导入时读 `BILLING_TEST_DATABASE_URL`，而 `pytest_configure` 先于收集执行，
    所以只改这一个环境变量就够了；不并行（没有 worker id）时什么都不做。
    """
    worker = os.environ.get("PYTEST_XDIST_WORKER")
    url = os.environ.get("BILLING_TEST_DATABASE_URL")
    if not worker or not url:
        return
    base = make_url(url)
    database = f"{base.database}_{worker}"
    admin = create_engine(base.set(database=""), isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as connection:
            connection.exec_driver_sql(f"CREATE DATABASE IF NOT EXISTS `{database}`")
    finally:
        admin.dispose()
    os.environ["BILLING_TEST_DATABASE_URL"] = base.set(database=database).render_as_string(
        hide_password=False
    )


def pytest_collection_modifyitems(config, items) -> None:
    """Keep only this CI shard's tests: position in the collection % total == index.

    CI 的 `backend-tests` 矩阵从 `strategy.job-index` / `job-total` 传进这两个值，所以各片
    合起来**恰好**是全集，不重不漏。按位置轮流分而不是按文件分：同一个文件里的慢用例与 MySQL
    用例会摊到各片上。分出去的用例记为 deselected，不是 skipped，CI 的「不许 skip」判定照旧。
    ⚠️ 只设了其中一个，或者值不合法，直接报错 —— 静默跑全集或跑空集都会让人误读结果。
    """
    index = os.environ.get("BILLING_TEST_SHARD_INDEX")
    total = os.environ.get("BILLING_TEST_SHARD_TOTAL")
    if index is None and total is None:
        return
    try:
        index_value, total_value = int(index or ""), int(total or "")
    except ValueError:
        raise pytest.UsageError(f"Bad shard settings: index={index!r}, total={total!r}") from None
    if not 0 <= index_value < total_value:
        raise pytest.UsageError(f"Bad shard settings: index={index!r}, total={total!r}")
    kept = [item for position, item in enumerate(items) if position % total_value == index_value]
    dropped = [item for position, item in enumerate(items) if position % total_value != index_value]
    if dropped:
        config.hook.pytest_deselected(items=dropped)
    items[:] = kept

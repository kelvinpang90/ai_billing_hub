"""Give every pytest-xdist worker its own MySQL database.

MySQL 用例都在同一个库上「升到 head → 清表 → 断言」，迁移用例还会 downgrade 到 base。
多个 worker 共用一个库会互相踩，所以并行时每个 worker 改用 `<库名>_<worker id>`。
用例模块在导入时读 `BILLING_TEST_DATABASE_URL`，而 `pytest_configure` 先于收集执行，
所以只改这一个环境变量就够了；不并行（没有 worker id）时什么都不做。
"""

from __future__ import annotations

import os

from sqlalchemy import create_engine
from sqlalchemy.engine import make_url


def pytest_configure(config) -> None:
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

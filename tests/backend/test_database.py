"""Engine construction, session lifecycle and the readiness probe."""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text

from app.core.config import Settings
from app.core.database import (
    DatabaseNotConfigured,
    DatabaseUnavailable,
    check_database,
    create_database_engine,
    create_session_factory,
    session_scope,
)

# 会话生命周期与探针语义与方言无关，用内存 SQLite 就能验，不必起 MySQL。
# 迁移能不能跑是另一回事，那条必须对着真 MySQL 验（见 test_migrations.py）。
MEMORY_URL = "sqlite+pysqlite:///:memory:"
UNREACHABLE_URL = "sqlite+pysqlite:////nonexistent-directory-for-tests/billing.db"


@pytest.fixture
def factory():
    engine = create_engine(MEMORY_URL)
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE probe (id INTEGER PRIMARY KEY)"))
    return create_session_factory(engine)


def test_empty_url_is_not_configured_rather_than_a_bad_connection() -> None:
    """默认值里不许有真实地址，所以「没配」必须是一个明确状态。"""
    with pytest.raises(DatabaseNotConfigured):
        create_database_engine(Settings(database_url=""))


def test_check_database_reports_not_configured_when_there_is_no_engine() -> None:
    with pytest.raises(DatabaseNotConfigured):
        check_database(None)


def test_check_database_reports_unavailable_without_leaking_the_target() -> None:
    """连接错误里带着主机名、端口、有时还有用户名 —— §94 不许外泄。"""
    engine = create_engine(UNREACHABLE_URL)

    with pytest.raises(DatabaseUnavailable) as raised:
        check_database(engine)

    assert "nonexistent-directory-for-tests" not in str(raised.value)
    assert raised.value.code == "DATABASE_UNAVAILABLE"
    assert raised.value.http_status == 503


def test_check_database_passes_against_a_live_engine() -> None:
    check_database(create_engine(MEMORY_URL))


def test_session_scope_commits_on_success(factory) -> None:
    with session_scope(factory) as session:
        session.execute(text("INSERT INTO probe (id) VALUES (1)"))

    with session_scope(factory) as session:
        assert session.execute(text("SELECT count(*) FROM probe")).scalar_one() == 1


def test_session_scope_rolls_back_on_exception(factory) -> None:
    """漏掉 rollback 的连接会带着脏事务回到连接池，下一个请求才报错。"""
    with pytest.raises(RuntimeError):
        with session_scope(factory) as session:
            session.execute(text("INSERT INTO probe (id) VALUES (2)"))
            raise RuntimeError("boom")

    with session_scope(factory) as session:
        assert session.execute(text("SELECT count(*) FROM probe")).scalar_one() == 0


def test_session_scope_closes_the_session_either_way(factory) -> None:
    with session_scope(factory) as ok_session:
        pass
    assert not ok_session.is_active or ok_session.get_bind() is not None

    with pytest.raises(RuntimeError):
        with session_scope(factory) as failed_session:
            raise RuntimeError("boom")

    # close() 之后 identity map 必须是空的，否则对象还挂在会话上。
    assert not ok_session.identity_map.keys()
    assert not failed_session.identity_map.keys()

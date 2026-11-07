"""Audit timestamps are truncated down to the second on write (design gate #569 v1 §1, §2, §7).

- **截到整秒**：`record_audit` 传入 `.6`、`.0`、`.999999` 三种时刻，提交后读出都是整秒，
  在 SQLite 与真 MySQL 上各跑一次（MySQL 的 `DATETIME` 不截会四舍五入，`.6` 存成下一秒）；
- **原问题的复现**：MySQL 上先写 `…:15.6`、再写 `…:15.9`，两行都是 `…:15`，先写的不晚于后写的；
- **登录路径**：`authenticate` 传入带微秒的 `now`，`LOGIN_FAILED` 与 `LOGIN` 都按整秒落库；
- **同事务（INV-13）**：`record_audit` 后回滚，审计行不存在。

MySQL 那部分需要 `BILLING_TEST_DATABASE_URL` 指向一个**可以被清空的**库，没设时 skip ——
**不要把 skipped 读成 passed**，CI 设了它并把 skipped 判成失败。
"""

from __future__ import annotations

import base64
import datetime as dt
import os
from collections.abc import Iterator

import pytest
from alembic.config import Config
from sqlalchemy import Engine, create_engine, func, select, text
from sqlalchemy.orm import Session, sessionmaker

from alembic import command
from app.core.config import Settings
from app.core.database import create_session_factory
from app.core.passwords import hash_password
from app.models.auth import AuditAction, AuditLog, User, UserRole, UserStatus
from app.models.base import Base
from app.repositories import tenancy
from app.services.auth import (
    InvalidCredentials,
    RequestContext,
    authenticate,
    record_audit,
    utc_now,
)

TEST_DATABASE_URL = os.environ.get("BILLING_TEST_DATABASE_URL", "")

PASSWORD = "a-perfectly-fine-passphrase"
CONTEXT = RequestContext(ip_address="203.0.113.21", user_agent="audit-timestamp-test")
WHOLE_SECOND = dt.datetime(2026, 11, 5, 2, 49, 15)


# --- 夹具 -----------------------------------------------------------------------


@pytest.fixture(params=["sqlite", "mysql"])
def factory(request, monkeypatch) -> Iterator[sessionmaker[Session]]:
    if request.param == "mysql":
        yield from _mysql_factory(monkeypatch)
        return
    yield from _sqlite_factory()


@pytest.fixture
def sqlite_factory() -> Iterator[sessionmaker[Session]]:
    yield from _sqlite_factory()


@pytest.fixture
def mysql_factory(monkeypatch) -> Iterator[sessionmaker[Session]]:
    yield from _mysql_factory(monkeypatch)


def _sqlite_factory() -> Iterator[sessionmaker[Session]]:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    yield create_session_factory(engine)
    engine.dispose()


def _mysql_factory(monkeypatch) -> Iterator[sessionmaker[Session]]:
    if not TEST_DATABASE_URL:
        pytest.skip("BILLING_TEST_DATABASE_URL is not set; the MySQL half needs a real MySQL")
    # 建表走 alembic：列类型（无小数秒的 DATETIME）以迁移建出来的为准。
    from app.core.config import get_settings

    monkeypatch.setenv("BILLING_DATABASE_URL", TEST_DATABASE_URL)
    get_settings.cache_clear()
    command.upgrade(Config("alembic.ini"), "head")
    get_settings.cache_clear()

    engine = create_engine(TEST_DATABASE_URL)
    _clean(engine)
    try:
        yield create_session_factory(engine)
    finally:
        _clean(engine)
        engine.dispose()


def _clean(engine: Engine) -> None:
    """⚠️ 库是共享的，每个用例前后都清场。0010 的触发器拒绝 DELETE，TRUNCATE 不经触发器。"""
    with engine.begin() as connection:
        connection.execute(text("TRUNCATE TABLE audit_logs"))


@pytest.fixture
def settings(tmp_path) -> Settings:
    key = tmp_path / "jwt.key"
    key.write_text("a-test-signing-key-that-is-long-enough", encoding="utf-8")
    master = tmp_path / "master.key"
    master.write_text(f"1:{base64.b64encode(os.urandom(32)).decode()}\n", encoding="utf-8")
    return Settings(
        jwt_secret_file=str(key),
        master_key_file=str(master),
        customer_2fa_required=False,
    )


# --- 帮手 -----------------------------------------------------------------------


def write_audit(factory: sessionmaker[Session], now: dt.datetime, reason: str) -> None:
    with factory() as session:
        record_audit(
            session, action=AuditAction.CUSTOMER_CREATE, context=CONTEXT, now=now, reason=reason
        )
        session.commit()


def stored(factory: sessionmaker[Session]) -> list[tuple[AuditAction, dt.datetime]]:
    with factory() as session:
        rows = session.execute(
            select(AuditLog.action, AuditLog.created_at).order_by(AuditLog.id)
        ).all()
        return [(AuditAction(action), created_at) for action, created_at in rows]


# --- 截到整秒 -------------------------------------------------------------------


@pytest.mark.parametrize("microsecond", [600000, 0, 999999])
def test_record_audit_truncates_down_to_the_second(factory, microsecond: int) -> None:
    write_audit(factory, WHOLE_SECOND.replace(microsecond=microsecond), "truncate")

    assert stored(factory) == [(AuditAction.CUSTOMER_CREATE, WHOLE_SECOND)]


def test_rounding_no_longer_reorders_audit_rows(mysql_factory) -> None:
    """原问题：`.6` 写的登录类审计曾在 MySQL 上存成下一秒，比之后 `.9` 写的业务审计还晚。"""
    write_audit(mysql_factory, WHOLE_SECOND.replace(microsecond=600000), "login-like")
    write_audit(mysql_factory, WHOLE_SECOND.replace(microsecond=900000), "business-like")

    times = [created_at for _, created_at in stored(mysql_factory)]

    assert times == [WHOLE_SECOND, WHOLE_SECOND]
    assert times[0] <= times[1]


# --- 登录路径 -------------------------------------------------------------------


def test_login_path_audits_are_whole_seconds(sqlite_factory, settings: Settings) -> None:
    """登录成功与失败都经 `record_audit`：带微秒的 `now` 落库时截到整秒。"""
    moment = utc_now().replace(microsecond=654321)
    with sqlite_factory() as session:
        tenant = tenancy.create_tenant(
            session, company_name="Example Sdn Bhd", email="contact@example.com", now=moment
        )
        session.commit()
        session.add(
            User(
                email="customer@example.com",
                password_hash=hash_password(PASSWORD),
                role=UserRole.CUSTOMER,
                status=UserStatus.ACTIVE,
                tenant_id=tenant.id,
                created_at=moment,
                updated_at=moment,
            )
        )
        session.commit()

    with pytest.raises(InvalidCredentials):
        authenticate(
            sqlite_factory,
            settings,
            email="customer@example.com",
            password="not-the-password",
            context=CONTEXT,
            now=moment,
        )
    outcome = authenticate(
        sqlite_factory,
        settings,
        email="customer@example.com",
        password=PASSWORD,
        context=CONTEXT,
        now=moment,
    )
    assert outcome.issued is not None

    rows = stored(sqlite_factory)
    assert [action for action, _ in rows] == [AuditAction.LOGIN_FAILED, AuditAction.LOGIN]
    for _, created_at in rows:
        assert created_at.microsecond == 0
        assert created_at == moment.replace(microsecond=0)


# --- 同事务（INV-13）------------------------------------------------------------


def test_audit_rolls_back_with_the_callers_transaction(sqlite_factory) -> None:
    with sqlite_factory() as session:
        record_audit(
            session,
            action=AuditAction.CUSTOMER_CREATE,
            context=CONTEXT,
            now=WHOLE_SECOND.replace(microsecond=600000),
            reason="rolled back",
        )
        session.flush()
        session.rollback()

    with sqlite_factory() as session:
        assert session.execute(select(func.count()).select_from(AuditLog)).scalar_one() == 0

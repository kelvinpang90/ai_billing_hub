"""Audit `created_at` is truncated to the whole second on write (AIH-TASK-163).

依据设计闸门 #569 v1 §7。

`record_audit` 是经应用写审计的唯一入口（钱包仓库里直接构造的两处用账本行的整秒时间）。
SQLite 保留微秒、MySQL 的 `DATETIME` 对小数秒四舍五入 —— 截在写入点，两种后端存下的值才相同，
登录路径（带微秒的 `utc_now()`）与业务路径（调用方先截到整秒）也才不再倒序。

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
from app.services.auth import InvalidCredentials, RequestContext, authenticate, record_audit

TEST_DATABASE_URL = os.environ.get("BILLING_TEST_DATABASE_URL", "")

CONTEXT = RequestContext(ip_address="203.0.113.16", user_agent="audit-timestamp-test")
PASSWORD = "a-perfectly-fine-passphrase"
EMAIL = "customer@audit-timestamp-test.example.com"

WHOLE_SECOND = dt.datetime(2026, 11, 5, 2, 49, 15)


def at(microsecond: int) -> dt.datetime:
    return WHOLE_SECOND.replace(microsecond=microsecond)


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
    # 建表走 alembic：列类型（`DATETIME` 无小数秒）以迁移建出来的为准。
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
    """⚠️ 库是共享的，每个用例前后都清场（与 test_account_status_service.py 同一做法）。

    这里只写不带操作者的审计行；0010 的触发器拒绝 DELETE，TRUNCATE 是 DDL、不经触发器。
    """
    with engine.begin() as connection:
        connection.execute(text("TRUNCATE TABLE audit_logs"))


@pytest.fixture
def settings(tmp_path) -> Settings:
    key = tmp_path / "jwt.key"
    key.write_text("a-test-signing-key-that-is-long-enough", encoding="utf-8")
    master = tmp_path / "master.key"
    master.write_text(f"1:{base64.b64encode(os.urandom(32)).decode()}\n", encoding="utf-8")
    return Settings(jwt_secret_file=str(key), master_key_file=str(master))


# --- 帮手 -----------------------------------------------------------------------


def write(factory: sessionmaker[Session], *, now: dt.datetime, reason: str) -> None:
    with factory() as session:
        record_audit(
            session, action=AuditAction.LOGIN_FAILED, context=CONTEXT, now=now, reason=reason
        )
        session.commit()


def stored(factory: sessionmaker[Session]) -> list[tuple[str | None, dt.datetime]]:
    with factory() as session:
        rows = session.execute(
            select(AuditLog.reason, AuditLog.created_at).order_by(AuditLog.id)
        ).all()
        return [(reason, created_at) for reason, created_at in rows]


def make_customer(factory: sessionmaker[Session]) -> None:
    """A tenant and its ACTIVE CUSTOMER user, the way test_auth_service.py builds them."""
    with factory() as session:
        tenant = tenancy.create_tenant(
            session,
            company_name="Audit Timestamp Test Sdn Bhd",
            email="contact@audit-timestamp-test.example.com",
            now=WHOLE_SECOND,
        )
        session.add(
            User(
                email=EMAIL,
                password_hash=hash_password(PASSWORD),
                role=UserRole.CUSTOMER,
                status=UserStatus.ACTIVE,
                tenant_id=tenant.id,
                created_at=WHOLE_SECOND,
                updated_at=WHOLE_SECOND,
            )
        )
        session.commit()


# --- 截到整秒（两种后端） ---------------------------------------------------------


@pytest.mark.parametrize("microsecond", [600_000, 0, 999_999])
def test_record_audit_truncates_down_to_the_whole_second(factory, microsecond: int) -> None:
    """`.6` 与 `.999999` 都存为 `:15`：MySQL 上不是四舍五入出来的 `:16`；整秒原样。"""
    write(factory, now=at(microsecond), reason="only")

    assert stored(factory) == [("only", WHOLE_SECOND)]


def test_login_then_business_audit_no_longer_reverses_on_mysql(mysql_factory) -> None:
    """原问题的复现：`…:15.6` 的登录类审计曾存为 `:16`，晚于随后 `…:15.9` 的业务类审计。"""
    write(mysql_factory, now=at(600_000), reason="login first")
    write(mysql_factory, now=at(900_000), reason="business second")

    rows = stored(mysql_factory)

    assert rows == [("login first", WHOLE_SECOND), ("business second", WHOLE_SECOND)]
    assert rows[0][1] <= rows[1][1]


# --- 登录路径 ---------------------------------------------------------------------


def test_login_audits_are_written_at_the_truncated_moment(
    sqlite_factory, settings: Settings
) -> None:
    """密码错误一次、成功一次：`LOGIN_FAILED` 与 `LOGIN` 都是传入时刻截到整秒。

    `now` 带微秒，正是 `authenticate` 自己取 `utc_now()` 时的样子。
    """
    local = settings.model_copy(update={"customer_2fa_required": False})
    make_customer(sqlite_factory)
    moment = at(654_321)

    with pytest.raises(InvalidCredentials):
        authenticate(
            sqlite_factory,
            local,
            email=EMAIL,
            password="not-the-password",
            context=CONTEXT,
            now=moment,
        )
    outcome = authenticate(
        sqlite_factory, local, email=EMAIL, password=PASSWORD, context=CONTEXT, now=moment
    )
    assert outcome.issued is not None

    with sqlite_factory() as session:
        rows = session.execute(
            select(AuditLog.action, AuditLog.created_at).order_by(AuditLog.id)
        ).all()
    assert [action for action, _ in rows] == [AuditAction.LOGIN_FAILED, AuditAction.LOGIN]
    for _, created_at in rows:
        assert created_at.microsecond == 0
        assert created_at == moment.replace(microsecond=0)


# --- 同事务（INV-13） -------------------------------------------------------------


def test_record_audit_stays_in_the_callers_transaction(sqlite_factory) -> None:
    """`record_audit` 只 `session.add`：调用方回滚，这一行也不存在。"""
    with sqlite_factory() as session:
        record_audit(session, action=AuditAction.LOGIN_FAILED, context=CONTEXT, now=at(600_000))
        session.rollback()

    with sqlite_factory() as session:
        assert session.execute(select(func.count()).select_from(AuditLog)).scalar_one() == 0

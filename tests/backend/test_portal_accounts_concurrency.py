"""Portal account services under concurrency (design gate #368 v10 §4, §7).

设计 §7 里属于本任务（AIH-TASK-091）的并发行：并发开通（同租户、同邮箱）、同一邀请令牌并发提交、
关户与接受、唯一冲突映射。停用与接受、重置 2FA 与确认 2FA 两行归 AIH-TASK-098。

⚠️ **只能对着真 MySQL 跑**：SQLite 方言下 SQLAlchemy 静默忽略 `FOR UPDATE`，没有行锁、间隙锁与
死锁检测，把服务里的加锁全部删掉，SQLite 上的用例照样全绿。

需要 `BILLING_TEST_DATABASE_URL`，没设时 skip —— **不要把 skipped 读成 passed**，CI 设了它。
用一个独立的库（`<库名>_portal_accounts`），不和迁移用例共用：那边靠 `alembic_version` 记状态，
这里 `create_all` / `drop_all` 会把它的表删掉（test_customer_write_lock.py 同一做法）。
两个线程各用一个连接；先后由 `threading.Barrier` / `Event` 控制，每个等待都设了超时。
"""

from __future__ import annotations

import datetime as dt
import json
import os
import threading
import time
from collections.abc import Callable, Iterator

import pytest
from sqlalchemy import Engine, create_engine, event, func, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.core.database import create_session_factory
from app.core.passwords import hash_password, verify_password
from app.models.auth import (
    AuditLog,
    DomainOutbox,
    PasswordResetToken,
    User,
    UserRole,
    UserStatus,
)
from app.models.base import Base
from app.models.tenancy import AccountStatus, Tenant
from app.repositories import tenancy
from app.services import portal_accounts
from app.services.auth import RequestContext
from app.services.portal_accounts import (
    EmailInUse,
    InvalidInvitationToken,
    InvitationIssued,
    PortalAccountExists,
    accept_invitation,
    create_portal_account,
)
from app.services.project_integration import AccountClosed

TEST_DATABASE_URL = os.environ.get("BILLING_TEST_DATABASE_URL", "")
CONTEXT = RequestContext(ip_address="10.0.0.1", user_agent="pytest")
NOW = dt.datetime(2026, 10, 21, 8, 0, 0)
ADMIN_EMAIL = "admin@example.com"
PORTAL_EMAIL = "portal@example.com"
PASSWORD = "a-customer-perfectly-fine-passphrase"
OTHER_PASSWORD = "another-customer-fine-passphrase"

# 持锁的一方在屏障之后多等这么久再继续，给另一方时间撞上行锁。断言看的是结果与事件先后，
# 这段时间只让「没加锁」的实现有机会抢到前面、从而让用例变红。
HOLD_SECONDS = 0.5
WAIT_TIMEOUT = 15
JOIN_TIMEOUT = 30


@pytest.fixture
def settings() -> Settings:
    return Settings(customer_invitation_ttl_seconds=259_200)


@pytest.fixture
def factory() -> Iterator[sessionmaker[Session]]:
    for engine in _mysql_engine():
        yield create_session_factory(engine)


def _mysql_engine() -> Iterator[Engine]:
    if not TEST_DATABASE_URL:
        pytest.skip("BILLING_TEST_DATABASE_URL is not set; row locking needs a real MySQL")
    base_url = make_url(TEST_DATABASE_URL)
    database = f"{base_url.database}_portal_accounts"
    admin = create_engine(base_url.set(database=""), isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.exec_driver_sql(f"CREATE DATABASE IF NOT EXISTS `{database}`")
    admin.dispose()

    engine = create_engine(base_url.set(database=database), pool_size=6, max_overflow=4)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    try:
        yield engine
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


# --- 帮手 -----------------------------------------------------------------------


@pytest.fixture
def admin(factory) -> User:
    with factory() as session:
        user = User(
            email=ADMIN_EMAIL,
            password_hash=hash_password("an-admin-perfectly-fine-passphrase"),
            role=UserRole.ADMIN,
            status=UserStatus.ACTIVE,
            created_at=NOW,
            updated_at=NOW,
        )
        session.add(user)
        session.commit()
        session.refresh(user)
        session.expunge(user)
        return user


def make_tenant(factory) -> Tenant:
    with factory() as session:
        tenant = tenancy.create_tenant(
            session, company_name="Portal Race Sdn Bhd", email="ops@example.com", now=NOW
        )
        tenant.account_status = AccountStatus.ENABLED
        session.commit()
        session.refresh(tenant)
        session.expunge(tenant)
        return tenant


def create(factory, settings, admin, tenant, email: str = PORTAL_EMAIL) -> InvitationIssued:
    return create_portal_account(
        factory,
        settings,
        actor=admin,
        customer_id=tenant.public_id,
        email=email,
        context=CONTEXT,
        now=NOW,
    )


def accept(factory, token: str, password: str = PASSWORD) -> None:
    accept_invitation(factory, token=token, password=password, context=CONTEXT, now=NOW)


def token_of(factory, issued: InvitationIssued) -> str:
    with factory() as session:
        row = session.get(DomainOutbox, issued.outbox_id)
        assert row is not None
        return str(json.loads(row.payload_json or "{}")["token"])


def count(factory, model, *conditions) -> int:
    with factory() as session:
        statement = select(func.count()).select_from(model).where(*conditions)
        return int(session.execute(statement).scalar_one())


def table_counts(factory) -> tuple[int, int, int, int]:
    """(users, tokens, outbox, audit) row counts."""
    return (
        count(factory, User),
        count(factory, PasswordResetToken),
        count(factory, DomainOutbox),
        count(factory, AuditLog),
    )


def portal_user(factory, tenant: Tenant) -> User:
    with factory() as session:
        user = session.execute(select(User).where(User.tenant_id == tenant.id)).scalar_one()
        session.expunge(user)
        return user


def tenant_status(factory, tenant: Tenant) -> AccountStatus:
    with factory() as session:
        statement = select(Tenant.account_status).where(Tenant.id == tenant.id)
        return session.execute(statement).scalar_one()


def lock_tenant(session: Session, tenant: Tenant) -> Tenant:
    """The tenant row lock a closing job would take (written directly; no closing service yet)."""
    return session.execute(
        select(Tenant).where(Tenant.id == tenant.id).with_for_update()
    ).scalar_one()


def run_pair(**work: Callable[[], None]) -> dict[str, Exception | None]:
    """Run two callables on two threads (so two connections); return what each raised."""
    outcomes: dict[str, Exception | None] = {}

    def runner(name: str, fn: Callable[[], None]) -> None:
        try:
            fn()
            outcomes[name] = None
        except Exception as exc:  # noqa: BLE001 - 由调用方逐个断言
            outcomes[name] = exc

    threads = [threading.Thread(target=runner, args=item) for item in work.items()]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=JOIN_TIMEOUT)
    assert not any(thread.is_alive() for thread in threads), "a side never finished"
    return outcomes


# --- 并发开通 -------------------------------------------------------------------


def test_two_creates_for_one_customer_leave_one_account(factory, settings, admin) -> None:
    """同一租户、邮箱不同：一个成功、一个 `PORTAL_ACCOUNT_EXISTS`，库里一个用户，没有 500。"""
    tenant = make_tenant(factory)
    barrier = threading.Barrier(2, timeout=WAIT_TIMEOUT)

    def attempt(email: str) -> Callable[[], None]:
        def go() -> None:
            barrier.wait()
            create(factory, settings, admin, tenant, email)

        return go

    outcomes = run_pair(a=attempt("first@example.com"), b=attempt("second@example.com"))

    assert sorted(type(o).__name__ for o in outcomes.values()) == [
        "NoneType",
        "PortalAccountExists",
    ], outcomes
    assert count(factory, User, User.tenant_id == tenant.id) == 1
    assert count(factory, PasswordResetToken) == 1
    assert count(factory, DomainOutbox) == 1


def test_two_customers_racing_for_one_address_leave_one_user(
    factory, settings, admin, monkeypatch
) -> None:
    """两个租户、同一邮箱：一个成功、一个 `EMAIL_IN_USE`。

    两边都在通过邮箱检查之后才放行（屏障），所以落败的一方一定是撞上 `users.email` 的唯一约束，
    验的是映射而不是检查：回滚后在新事务里查到地址已被占用，不出现 500 或死锁。
    """
    first, second = make_tenant(factory), make_tenant(factory)
    barrier = threading.Barrier(2, timeout=WAIT_TIMEOUT)
    real_email_taken = portal_accounts._email_taken

    def staged(session: Session, address: str) -> bool:
        taken = real_email_taken(session, address)
        barrier.wait()
        return taken

    monkeypatch.setattr(portal_accounts, "_email_taken", staged)

    outcomes = run_pair(
        a=lambda: create(factory, settings, admin, first),
        b=lambda: create(factory, settings, admin, second),
    )

    assert sorted(type(o).__name__ for o in outcomes.values()) == [
        "EmailInUse",
        "NoneType",
    ], outcomes
    assert count(factory, User, User.email == PORTAL_EMAIL) == 1
    assert count(factory, PasswordResetToken) == 1
    assert count(factory, DomainOutbox) == 1


# --- 同一邀请令牌并发提交 -------------------------------------------------------


def test_the_same_invitation_submitted_twice_at_once_lands_once(factory, settings, admin) -> None:
    tenant = make_tenant(factory)
    token = token_of(factory, create(factory, settings, admin, tenant))
    barrier = threading.Barrier(2, timeout=WAIT_TIMEOUT)
    passwords = {"a": PASSWORD, "b": OTHER_PASSWORD}

    def attempt(name: str) -> Callable[[], None]:
        def go() -> None:
            barrier.wait()
            accept(factory, token, passwords[name])

        return go

    outcomes = run_pair(a=attempt("a"), b=attempt("b"))

    winners = [name for name, outcome in outcomes.items() if outcome is None]
    losers = [
        name for name, outcome in outcomes.items() if isinstance(outcome, InvalidInvitationToken)
    ]
    assert (len(winners), len(losers)) == (1, 1), outcomes
    user = portal_user(factory, tenant)
    assert (user.status, user.password_version) == (UserStatus.ACTIVE, 1)
    # 最终的密码就是成功那一次的，落败的一方没有覆盖它。
    assert verify_password(user.password_hash, passwords[winners[0]])
    assert not verify_password(user.password_hash, passwords[losers[0]])


# --- 关户与接受 -----------------------------------------------------------------


def test_a_close_that_commits_first_refuses_the_accept(factory, settings, admin) -> None:
    """关户先提交：接受拿到租户锁后读到 `CLOSED`，409，账号仍是 `INVITED`、令牌未消费。

    接受方第 1 步的普通读在关户提交之前就建立了快照（关户方此刻已持锁、尚未提交）：只有锁读
    才读得到 `CLOSED`，用快照里的租户状态判定的实现会让这条用例变红。
    """
    tenant = make_tenant(factory)
    token = token_of(factory, create(factory, settings, admin, tenant))
    barrier = threading.Barrier(2, timeout=WAIT_TIMEOUT)
    events: list[str] = []

    def close() -> None:
        with factory() as session:
            lock_tenant(session, tenant).account_status = AccountStatus.CLOSED
            session.flush()
            barrier.wait()  # 关户方已持锁、尚未提交
            time.sleep(HOLD_SECONDS)
            events.append("close committing")
            session.commit()

    def attempt() -> None:
        barrier.wait()
        try:
            accept(factory, token)
        except AccountClosed:
            events.append("accept refused")
            raise

    outcomes = run_pair(close=close, accept=attempt)

    assert outcomes["close"] is None
    assert isinstance(outcomes["accept"], AccountClosed)
    assert events == ["close committing", "accept refused"]
    user = portal_user(factory, tenant)
    assert (user.status, user.password_version) == (UserStatus.INVITED, 0)
    assert count(factory, PasswordResetToken, PasswordResetToken.used_at.is_(None)) == 1


def test_a_close_between_the_plain_reads_and_the_lock_is_still_seen(
    factory, settings, admin
) -> None:
    """接受方先做完第 1 步的普通读（建立快照），关户随即完整提交，接受方再去锁租户行。

    锁读总是读最新已提交版本：409，账号仍是 `INVITED`、令牌未消费。关户在接受方锁租户行的那条
    查询发出之前插进来（会话事件钩子，只触发一次）。
    """
    tenant = make_tenant(factory)
    token = token_of(factory, create(factory, settings, admin, tenant))
    fired = threading.Event()

    def close() -> None:
        with factory() as session:
            lock_tenant(session, tenant).account_status = AccountStatus.CLOSED
            session.commit()

    def before_tenant_lock(state) -> None:
        mappers = {mapper.class_ for mapper in state.all_mappers}
        if (
            fired.is_set()
            or Tenant not in mappers
            or not state.execution_options.get("populate_existing")
        ):
            return
        fired.set()
        closer = threading.Thread(target=close)
        closer.start()
        closer.join(timeout=JOIN_TIMEOUT)
        assert not closer.is_alive(), "the close never finished"

    event.listen(factory, "do_orm_execute", before_tenant_lock)
    try:
        with pytest.raises(AccountClosed):
            accept(factory, token)
    finally:
        event.remove(factory, "do_orm_execute", before_tenant_lock)

    assert fired.is_set()
    user = portal_user(factory, tenant)
    assert (user.status, user.password_version) == (UserStatus.INVITED, 0)
    assert count(factory, PasswordResetToken, PasswordResetToken.used_at.is_(None)) == 1


def test_an_accept_that_takes_the_lock_first_completes_before_the_close(
    factory, settings, admin, monkeypatch
) -> None:
    """接受先拿到租户锁：200，账号 `ACTIVE`；关户排在它后面，锁到时看到的已是 `ACTIVE`。"""
    tenant = make_tenant(factory)
    token = token_of(factory, create(factory, settings, admin, tenant))
    barrier = threading.Barrier(2, timeout=WAIT_TIMEOUT)
    real_validate = portal_accounts.validate_password_strength
    seen_by_close: list[UserStatus] = []

    def staged_validate(password: str, *, email: str | None = None) -> None:
        barrier.wait()  # 接受方已持租户、用户、令牌三把锁（判定顺序第 5 步）
        time.sleep(HOLD_SECONDS)
        real_validate(password, email=email)

    monkeypatch.setattr(portal_accounts, "validate_password_strength", staged_validate)

    def close() -> None:
        barrier.wait()
        with factory() as session:
            lock_tenant(session, tenant).account_status = AccountStatus.CLOSED
            status = session.execute(
                select(User.status).where(User.tenant_id == tenant.id)
            ).scalar_one()
            seen_by_close.append(status)
            session.commit()

    outcomes = run_pair(accept=lambda: accept(factory, token), close=close)

    assert outcomes == {"accept": None, "close": None}
    # 不加锁的实现里，关户在接受方等待期间就拿到了锁，看到的还是 INVITED。
    assert seen_by_close == [UserStatus.ACTIVE]
    assert portal_user(factory, tenant).status is UserStatus.ACTIVE
    assert tenant_status(factory, tenant) is AccountStatus.CLOSED


# --- 唯一冲突映射 ---------------------------------------------------------------


def test_a_same_customer_account_committed_after_the_check_maps_to_exists(
    factory, settings, admin, monkeypatch
) -> None:
    """检查之后、插入之前，别的事务提交了同租户的门户账号：409 `PORTAL_ACCOUNT_EXISTS`，不是 500。

    真并发里插不进这一行：开通持有租户行的排他锁，另一边插入用户时外键检查要对同一行加共享锁，
    只能等开通结束。所以这里让那一行先提交，再让检查「没看见」它（替换掉检查），只验映射本身：
    撞上 `uq_users_tenant` 后回滚、在新事务里查到该租户已有账号。
    """
    tenant = make_tenant(factory)
    with factory() as session:
        session.add(
            User(
                email="already-there@example.com",
                password_hash=hash_password("a-placeholder-fine-passphrase"),
                role=UserRole.CUSTOMER,
                status=UserStatus.INVITED,
                tenant_id=tenant.id,
                created_at=NOW,
                updated_at=NOW,
            )
        )
        session.commit()
    monkeypatch.setattr(portal_accounts, "_portal_user", lambda *_args, **_kwargs: None)
    before = table_counts(factory)

    with pytest.raises(PortalAccountExists):
        create(factory, settings, admin, tenant)

    assert table_counts(factory) == before


def test_a_same_address_committed_after_the_check_maps_to_email_in_use(
    factory, settings, admin, monkeypatch
) -> None:
    """检查之后、插入之前，另一个连接提交了同邮箱的用户：409 `EMAIL_IN_USE`，库里没有多出的行。

    那个用户不挂租户，插入不碰开通方锁住的租户行，所以能在开通方的事务进行中真的提交。
    """
    tenant = make_tenant(factory)
    real_email_taken = portal_accounts._email_taken

    def commit_a_rival() -> None:
        with factory() as session:
            session.add(
                User(
                    email=PORTAL_EMAIL,
                    password_hash=hash_password("a-placeholder-fine-passphrase"),
                    role=UserRole.ADMIN,
                    status=UserStatus.ACTIVE,
                    created_at=NOW,
                    updated_at=NOW,
                )
            )
            session.commit()

    def staged(session: Session, address: str) -> bool:
        taken = real_email_taken(session, address)
        rival = threading.Thread(target=commit_a_rival)
        rival.start()
        rival.join(timeout=JOIN_TIMEOUT)
        assert not rival.is_alive(), "the rival insert never finished"
        return taken

    monkeypatch.setattr(portal_accounts, "_email_taken", staged)

    with pytest.raises(EmailInUse):
        create(factory, settings, admin, tenant)

    assert count(factory, User, User.email == PORTAL_EMAIL) == 1
    assert count(factory, User, User.tenant_id == tenant.id) == 0
    assert (count(factory, PasswordResetToken), count(factory, DomainOutbox)) == (0, 0)
    assert count(factory, AuditLog) == 0

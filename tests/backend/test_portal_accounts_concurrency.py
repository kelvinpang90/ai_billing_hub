"""Portal account services under concurrency (design gate #368 v10 §4, §7).

设计 §7 的并发行。AIH-TASK-091：并发开通（同租户、同邮箱）、同一邀请令牌并发提交、关户与接受、
唯一冲突映射。AIH-TASK-098：停用与接受、重置 2FA 与客户确认 2FA（各两种先后），以及「重置客户
2FA」里 255 个字符的原因在真 MySQL 上逐字保存。
设计 #368 v12（管理员 PR）：停用与忘记密码（两种先后），停用提交后不留可用的重置令牌。

⚠️ **只能对着真 MySQL 跑**：SQLite 方言下 SQLAlchemy 静默忽略 `FOR UPDATE`，没有行锁、间隙锁与
死锁检测，把服务里的加锁全部删掉，SQLite 上的用例照样全绿。

需要 `BILLING_TEST_DATABASE_URL`，没设时 skip —— **不要把 skipped 读成 passed**，CI 设了它。
用一个独立的库（`<库名>_portal_accounts`），不和迁移用例共用：那边靠 `alembic_version` 记状态，
这里 `create_all` / `drop_all` 会把它的表删掉（test_customer_write_lock.py 同一做法）。
两个线程各用一个连接；先后由 `threading.Barrier` / `Event` 控制，每个等待都设了超时。
"""

from __future__ import annotations

import base64
import datetime as dt
import json
import os
import threading
import time
from collections.abc import Callable, Iterator

import pyotp
import pytest
from sqlalchemy import Engine, create_engine, event, func, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.core.database import create_session_factory
from app.core.passwords import hash_password, verify_password
from app.core.tokens import InvalidToken
from app.models.auth import (
    AuditAction,
    AuditLog,
    DomainOutbox,
    PasswordResetToken,
    RecoveryCode,
    TokenPurpose,
    TwoFactorSetting,
    User,
    UserRole,
    UserStatus,
)
from app.models.base import Base
from app.models.tenancy import AccountStatus, Tenant
from app.repositories import tenancy
from app.services import password_reset, portal_accounts, two_factor
from app.services.auth import RequestContext
from app.services.password_reset import InvalidResetToken, request_reset, reset_password
from app.services.portal_accounts import (
    EmailInUse,
    InvalidInvitationToken,
    InvitationIssued,
    PortalAccountExists,
    accept_invitation,
    create_portal_account,
    disable_portal_account,
    enable_portal_account,
    reset_portal_two_factor,
)
from app.services.project_integration import AccountClosed
from app.services.two_factor import confirm_enrolment, start_enrolment

TEST_DATABASE_URL = os.environ.get("BILLING_TEST_DATABASE_URL", "")
CONTEXT = RequestContext(ip_address="10.0.0.1", user_agent="pytest")
NOW = dt.datetime(2026, 10, 21, 8, 0, 0)
ADMIN_EMAIL = "admin@example.com"
PORTAL_EMAIL = "portal@example.com"
PASSWORD = "a-customer-perfectly-fine-passphrase"
OTHER_PASSWORD = "another-customer-fine-passphrase"
REASON = "Lost phone; identity confirmed by a call-back to the registered number."

# 持锁的一方在屏障之后多等这么久再继续，给另一方时间撞上行锁。断言看的是结果与事件先后，
# 这段时间只让「没加锁」的实现有机会抢到前面、从而让用例变红。
HOLD_SECONDS = 0.5
WAIT_TIMEOUT = 15
JOIN_TIMEOUT = 30


@pytest.fixture
def settings(tmp_path) -> Settings:
    # 主密钥只给重置 2FA 与确认 2FA 那两条用例注册 2FA 用。
    master = tmp_path / "master.key"
    master.write_text(f"1:{base64.b64encode(os.urandom(32)).decode()}\n", encoding="utf-8")
    return Settings(customer_invitation_ttl_seconds=259_200, master_key_file=str(master))


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


# --- 停用、重置 2FA 的帮手（AIH-TASK-098） --------------------------------------


def disable(factory, admin, tenant) -> None:
    disable_portal_account(
        factory, actor=admin, customer_id=tenant.public_id, context=CONTEXT, now=NOW
    )


def reset_2fa(factory, admin, tenant, reason: str = REASON) -> None:
    reset_portal_two_factor(
        factory,
        actor=admin,
        customer_id=tenant.public_id,
        reason=reason,
        context=CONTEXT,
        now=NOW,
    )


def make_active_customer(factory, tenant: Tenant) -> User:
    """An `ACTIVE` portal account at `password_version` 0, written directly."""
    with factory() as session:
        user = User(
            email=PORTAL_EMAIL,
            password_hash=hash_password(PASSWORD),
            role=UserRole.CUSTOMER,
            status=UserStatus.ACTIVE,
            tenant_id=tenant.id,
            activated_at=NOW,
            created_at=NOW,
            updated_at=NOW,
        )
        session.add(user)
        session.commit()
        session.refresh(user)
        session.expunge(user)
        return user


def audit_actions(factory) -> list[AuditAction]:
    with factory() as session:
        return list(session.execute(select(AuditLog.action).order_by(AuditLog.id)).scalars())


# --- 停用与接受 -----------------------------------------------------------------


def test_a_disable_that_commits_first_refuses_the_accept(
    factory, settings, admin, monkeypatch
) -> None:
    """停用先拿到锁：令牌已作废、账号 `DISABLED`，接受锁到后 400，账号不会变成 `ACTIVE`。

    接受方第 1 步的普通读在停用提交之前就建立了快照：只有锁读才看得到作废与停用。
    """
    tenant = make_tenant(factory)
    token = token_of(factory, create(factory, settings, admin, tenant))
    barrier = threading.Barrier(2, timeout=WAIT_TIMEOUT)
    events: list[str] = []
    real_portal_user = portal_accounts._portal_user

    def staged(session: Session, tenant_id: int, *, for_update: bool) -> User | None:
        user = real_portal_user(session, tenant_id, for_update=for_update)
        if for_update:
            barrier.wait()  # 停用方已持租户、用户两把锁，尚未改任何东西
            time.sleep(HOLD_SECONDS)
            events.append("disable proceeding")
        return user

    monkeypatch.setattr(portal_accounts, "_portal_user", staged)

    def attempt() -> None:
        barrier.wait()
        try:
            accept(factory, token)
        except InvalidInvitationToken:
            events.append("accept refused")
            raise

    outcomes = run_pair(disable=lambda: disable(factory, admin, tenant), accept=attempt)

    assert outcomes["disable"] is None
    assert isinstance(outcomes["accept"], InvalidInvitationToken), outcomes
    assert events == ["disable proceeding", "accept refused"]
    user = portal_user(factory, tenant)
    assert (user.status, user.password_version, user.activated_at) == (
        UserStatus.DISABLED,
        1,
        None,
    )
    assert count(factory, PasswordResetToken, PasswordResetToken.used_at.is_(None)) == 0


def test_an_accept_that_takes_the_lock_first_is_then_disabled(
    factory, settings, admin, monkeypatch
) -> None:
    """接受先拿到锁：200，账号 `ACTIVE`；停用排在它后面，锁到时看到的已是 `ACTIVE`。

    停用照常生效：最终是 `DISABLED`，不会出现停用之后账号仍是 `ACTIVE`。
    """
    tenant = make_tenant(factory)
    token = token_of(factory, create(factory, settings, admin, tenant))
    barrier = threading.Barrier(2, timeout=WAIT_TIMEOUT)
    real_validate = portal_accounts.validate_password_strength
    real_portal_user = portal_accounts._portal_user
    seen_by_disable: list[UserStatus] = []

    def staged_validate(password: str, *, email: str | None = None) -> None:
        barrier.wait()  # 接受方已持租户、用户、令牌三把锁（判定顺序第 5 步）
        time.sleep(HOLD_SECONDS)
        real_validate(password, email=email)

    def watched(session: Session, tenant_id: int, *, for_update: bool) -> User | None:
        user = real_portal_user(session, tenant_id, for_update=for_update)
        if for_update and user is not None:
            seen_by_disable.append(user.status)
        return user

    monkeypatch.setattr(portal_accounts, "validate_password_strength", staged_validate)
    monkeypatch.setattr(portal_accounts, "_portal_user", watched)

    def go_disable() -> None:
        barrier.wait()
        disable(factory, admin, tenant)

    outcomes = run_pair(accept=lambda: accept(factory, token), disable=go_disable)

    assert outcomes == {"accept": None, "disable": None}
    # 不加锁的实现里，停用在接受方等待期间就读到了 INVITED。
    assert seen_by_disable == [UserStatus.ACTIVE]
    user = portal_user(factory, tenant)
    # 接受 +1、停用 +1；停用之后账号不会是 ACTIVE。
    assert (user.status, user.password_version) == (UserStatus.DISABLED, 2)
    assert user.activated_at == NOW
    assert count(factory, PasswordResetToken, PasswordResetToken.used_at.is_(None)) == 0


# --- 重置 2FA 与客户确认 2FA ---------------------------------------------------


def test_a_confirm_that_commits_first_is_then_reset(factory, settings, admin, monkeypatch) -> None:
    """确认先提交：2FA 启用，随后重置把它与恢复码删掉，版本号 +1。"""
    tenant = make_tenant(factory)
    user = make_active_customer(factory, tenant)
    enrolment = start_enrolment(factory, settings, user_id=user.id, password_version=0)
    code = pyotp.TOTP(enrolment.secret).now()
    barrier = threading.Barrier(2, timeout=WAIT_TIMEOUT)
    events: list[str] = []
    real_replace = two_factor._replace_recovery_codes

    def staged_replace(session: Session, *, user_id: int, now: dt.datetime) -> list[str]:
        barrier.wait()  # 确认方已锁用户行、置位 confirmed_at，尚未提交
        time.sleep(HOLD_SECONDS)
        events.append("confirm committing")
        return real_replace(session, user_id=user_id, now=now)

    monkeypatch.setattr(two_factor, "_replace_recovery_codes", staged_replace)

    def go_confirm() -> None:
        confirm_enrolment(
            factory,
            settings,
            user_id=user.id,
            password_version=0,
            code=code,
            context=CONTEXT,
        )

    def go_reset() -> None:
        barrier.wait()
        reset_2fa(factory, admin, tenant)
        events.append("reset done")

    outcomes = run_pair(confirm=go_confirm, reset=go_reset)

    assert outcomes == {"confirm": None, "reset": None}
    assert events == ["confirm committing", "reset done"]
    assert (count(factory, TwoFactorSetting), count(factory, RecoveryCode)) == (0, 0)
    assert portal_user(factory, tenant).password_version == 1
    actions = audit_actions(factory)
    assert actions.index(AuditAction.TWO_FACTOR_ENABLED) < actions.index(
        AuditAction.PORTAL_2FA_RESET
    )


def test_a_reset_that_commits_first_refuses_the_confirm(
    factory, settings, admin, monkeypatch
) -> None:
    """重置先提交：确认在锁用户行后读到新版本号，401 `TOKEN_INVALID`，不启用、不写恢复码。"""
    tenant = make_tenant(factory)
    user = make_active_customer(factory, tenant)
    enrolment = start_enrolment(factory, settings, user_id=user.id, password_version=0)
    code = pyotp.TOTP(enrolment.secret).now()
    barrier = threading.Barrier(2, timeout=WAIT_TIMEOUT)
    events: list[str] = []
    real_audit = portal_accounts.record_audit

    def staged_audit(session: Session, **kwargs) -> None:
        barrier.wait()  # 重置方已锁租户、用户行并删了 2FA 行，尚未提交
        time.sleep(HOLD_SECONDS)
        events.append("reset committing")
        real_audit(session, **kwargs)

    monkeypatch.setattr(portal_accounts, "record_audit", staged_audit)

    def go_confirm() -> None:
        barrier.wait()
        try:
            confirm_enrolment(
                factory,
                settings,
                user_id=user.id,
                password_version=0,
                code=code,
                context=CONTEXT,
            )
        except InvalidToken:
            events.append("confirm refused")
            raise

    outcomes = run_pair(reset=lambda: reset_2fa(factory, admin, tenant), confirm=go_confirm)

    assert outcomes["reset"] is None
    refused = outcomes["confirm"]
    assert isinstance(refused, InvalidToken), outcomes
    assert (refused.code, refused.http_status) == ("TOKEN_INVALID", 401)
    assert events == ["reset committing", "confirm refused"]
    assert (count(factory, TwoFactorSetting), count(factory, RecoveryCode)) == (0, 0)
    assert portal_user(factory, tenant).password_version == 1
    assert AuditAction.TWO_FACTOR_ENABLED not in audit_actions(factory)


# --- 重置 2FA 的原因长度（真 MySQL） --------------------------------------------


def test_a_255_character_reason_is_kept_verbatim_on_mysql(factory, admin) -> None:
    """`audit_logs.reason` 是 `String(255)`：恰好 255 个字符在真 MySQL 上不报错、不截断。"""
    tenant = make_tenant(factory)
    user = make_active_customer(factory, tenant)
    with factory() as session:
        session.add(
            TwoFactorSetting(
                user_id=user.id,
                encrypted_totp_secret="not-a-real-ciphertext",
                key_version=1,
                secret_version=1,
                confirmed_at=NOW,
                created_at=NOW,
                updated_at=NOW,
            )
        )
        session.commit()
    reason = ("0123456789" * 26)[:255]

    reset_2fa(factory, admin, tenant, reason=reason)

    with factory() as session:
        stored = session.execute(
            select(AuditLog.reason).where(AuditLog.action == AuditAction.PORTAL_2FA_RESET)
        ).scalar_one()
    assert stored == reason
    assert count(factory, TwoFactorSetting) == 0


# --- 停用与忘记密码（设计 #368 v12 §4「串行化」、§7「并发（v11）」） -----------------


def forgot(factory, settings) -> int | None:
    return request_reset(factory, settings, email=PORTAL_EMAIL, context=CONTEXT, now=NOW)


def reset_token_of(factory, outbox_id: int) -> str:
    with factory() as session:
        row = session.get(DomainOutbox, outbox_id)
        assert row is not None
        return str(json.loads(row.payload_json or "{}")["token"])


def live_reset_tokens(factory) -> int:
    return count(
        factory,
        PasswordResetToken,
        PasswordResetToken.purpose == TokenPurpose.PASSWORD_RESET,
        PasswordResetToken.used_at.is_(None),
    )


def test_a_disable_between_the_plain_read_and_the_lock_issues_no_reset_token(
    factory, settings, admin
) -> None:
    """忘记密码的普通读看到 `ACTIVE`，停用随即完整提交，忘记密码再去锁用户行。

    锁读看到 `DISABLED`：不写令牌、outbox、审计，对外仍是同一个结果。旧实现读到 `ACTIVE` 就直接
    插入，这张令牌躲过停用的作废 —— 去掉锁读或锁内复查，这条都会变红。停用在锁读那条查询发出之前
    插进来（会话事件钩子，只触发一次）。
    """
    tenant = make_tenant(factory)
    make_active_customer(factory, tenant)
    fired = threading.Event()
    failures: list[BaseException] = []

    def go_disable() -> None:
        try:
            disable(factory, admin, tenant)
        except BaseException as exc:  # noqa: BLE001 - 在主线程断言
            failures.append(exc)

    def before_locked_read(state) -> None:
        mappers = {mapper.class_ for mapper in state.all_mappers}
        if (
            fired.is_set()
            or User not in mappers
            or not state.execution_options.get("populate_existing")
        ):
            return
        fired.set()
        disabler = threading.Thread(target=go_disable)
        disabler.start()
        disabler.join(timeout=JOIN_TIMEOUT)
        assert not disabler.is_alive(), "the disable never finished"

    event.listen(factory, "do_orm_execute", before_locked_read)
    try:
        assert forgot(factory, settings) is None
    finally:
        event.remove(factory, "do_orm_execute", before_locked_read)

    assert fired.is_set()
    assert failures == []
    assert portal_user(factory, tenant).status is UserStatus.DISABLED
    assert count(factory, PasswordResetToken) == 0
    assert count(factory, DomainOutbox) == 0
    assert audit_actions(factory) == [AuditAction.PORTAL_ACCOUNT_DISABLED]


def test_a_reset_request_holding_the_lock_is_then_voided_by_the_disable(
    factory, settings, admin, monkeypatch
) -> None:
    """忘记密码先锁到用户行：停用排在它后面，等它提交后才继续，并把它刚写的令牌一起作废。

    重新启用之后，那封信里的令牌仍然用不了。去掉 `FOR UPDATE` 的话，停用不必等、在令牌落库前
    就提交完，令牌于是躲过作废 —— 这条会变红。
    """
    tenant = make_tenant(factory)
    make_active_customer(factory, tenant)
    disabled = threading.Event()
    failures: list[BaseException] = []
    events: list[str] = []
    real_generate = password_reset.generate_opaque_token

    def go_disable() -> None:
        try:
            disable(factory, admin, tenant)
            events.append("disable committed")
            disabled.set()
        except BaseException as exc:  # noqa: BLE001 - 在主线程断言
            failures.append(exc)

    disabler = threading.Thread(target=go_disable)

    def staged_generate() -> str:
        # 忘记密码已持用户行锁、复查通过，令牌还没写。停用这时出发，必须撞上这把锁。
        disabler.start()
        time.sleep(HOLD_SECONDS)
        events.append("disable still waiting" if not disabled.is_set() else "disable got through")
        return real_generate()

    monkeypatch.setattr(password_reset, "generate_opaque_token", staged_generate)

    outbox_id = forgot(factory, settings)
    events.append("reset request committed")
    disabler.join(timeout=JOIN_TIMEOUT)
    assert not disabler.is_alive(), "the disable never finished"

    assert failures == []
    assert outbox_id is not None
    assert events == ["disable still waiting", "reset request committed", "disable committed"]
    assert portal_user(factory, tenant).status is UserStatus.DISABLED
    assert count(factory, PasswordResetToken) == 1
    assert live_reset_tokens(factory) == 0

    enable_portal_account(
        factory, actor=admin, customer_id=tenant.public_id, context=CONTEXT, now=NOW
    )
    assert portal_user(factory, tenant).status is UserStatus.ACTIVE
    with pytest.raises(InvalidResetToken):
        reset_password(
            factory,
            settings,
            token=reset_token_of(factory, outbox_id),
            new_password=OTHER_PASSWORD,
            context=CONTEXT,
            now=NOW,
        )
    assert verify_password(portal_user(factory, tenant).password_hash, PASSWORD)

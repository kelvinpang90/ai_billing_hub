"""Portal account services: view, create, resend, accept (design gate #368 v10 §2, §4, §5, §6, §7).

覆盖设计 §7 里属于服务层（任务 B，AIH-TASK-091）的行：正常路径的状态与审计、「邀请字段」（停用、
启用那几步归 AIH-TASK-098）、令牌边界、邮箱归一与占用、租户 `CLOSED`、令牌隔离、判定顺序、重发
邮箱比较、事务中途失败、Redis/Celery 丢失（只看 outbox 行是 `PENDING`，恢复扫描归 AIH-TASK-094）、
泄漏。全部跑在内存 SQLite 上；加锁与并发只能对着真 MySQL 验，见
tests/backend/test_portal_accounts_concurrency.py。
"""

from __future__ import annotations

import datetime as dt
import json
import logging

import pytest
from sqlalchemy import create_engine, event, select, update
from sqlalchemy.exc import OperationalError

from app.core.config import Settings
from app.core.database import create_session_factory
from app.core.errors import AppError
from app.core.passwords import WeakPassword, hash_password, verify_password
from app.core.tokens import hash_opaque_token
from app.models.auth import (
    AuditAction,
    AuditLog,
    DomainOutbox,
    OutboxStatus,
    PasswordResetToken,
    TokenPurpose,
    User,
    UserRole,
    UserStatus,
)
from app.models.base import Base
from app.models.tenancy import AccountStatus, Tenant
from app.repositories import tenancy
from app.services import portal_accounts
from app.services.auth import RequestContext
from app.services.customers import CustomerNotFound
from app.services.password_reset import InvalidResetToken, request_reset, reset_password
from app.services.portal_accounts import (
    ENTITY_USERS,
    EVENT_PORTAL_INVITATION,
    EmailInUse,
    InvalidInvitationToken,
    InvitationIssued,
    PortalAccountExists,
    PortalAccountNotFound,
    PortalAccountNotInvited,
    PortalAccountView,
    accept_invitation,
    create_portal_account,
    get_portal_account,
    resend_invitation,
)
from app.services.project_integration import AccountClosed

CONTEXT = RequestContext(ip_address="10.0.0.1", user_agent="pytest")
TTL_SECONDS = 259_200
TTL = dt.timedelta(seconds=TTL_SECONDS)
# 带微秒：邀请令牌的 `expires_at` 必须截到整秒（设计 §2「邀请邮件」）。
NOW = dt.datetime(2026, 10, 21, 8, 0, 0, 654_321)
T0 = NOW.replace(microsecond=0)
ADMIN_EMAIL = "admin@example.com"
ADMIN_PASSWORD = "an-admin-perfectly-fine-passphrase"
PORTAL_EMAIL = "portal@example.com"
PASSWORD = "a-customer-perfectly-fine-passphrase"
OTHER_PASSWORD = "another-customer-fine-passphrase"
WEAK = "tiny-pw"
UNKNOWN_CUSTOMER = "00000000-0000-0000-0000-000000000000"


@pytest.fixture
def settings() -> Settings:
    return Settings(customer_invitation_ttl_seconds=TTL_SECONDS, password_reset_ttl_seconds=1_800)


@pytest.fixture
def factory():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    yield create_session_factory(engine)
    engine.dispose()


@pytest.fixture
def admin(factory) -> User:
    return make_user(factory, email=ADMIN_EMAIL, password=ADMIN_PASSWORD)


# --- 帮手 -----------------------------------------------------------------------


def make_user(
    factory,
    *,
    email: str,
    password: str,
    role: UserRole = UserRole.ADMIN,
    status: UserStatus = UserStatus.ACTIVE,
    tenant_id: int | None = None,
) -> User:
    with factory() as session:
        user = User(
            email=email,
            password_hash=hash_password(password),
            role=role,
            status=status,
            tenant_id=tenant_id,
            created_at=T0,
            updated_at=T0,
        )
        session.add(user)
        session.commit()
        session.refresh(user)
        session.expunge(user)
        return user


def make_tenant(factory, status: AccountStatus = AccountStatus.ENABLED) -> Tenant:
    with factory() as session:
        tenant = tenancy.create_tenant(
            session, company_name="Portal Test Sdn Bhd", email="ops@example.com", now=T0
        )
        tenant.account_status = status
        session.commit()
        session.refresh(tenant)
        session.expunge(tenant)
        return tenant


def set_account_status(factory, tenant: Tenant, status: AccountStatus) -> None:
    with factory() as session:
        session.execute(update(Tenant).where(Tenant.id == tenant.id).values(account_status=status))
        session.commit()


def create(factory, settings, admin, tenant, *, email: str = PORTAL_EMAIL, now=NOW):
    return create_portal_account(
        factory,
        settings,
        actor=admin,
        customer_id=tenant.public_id,
        email=email,
        context=CONTEXT,
        now=now,
    )


def resend(factory, settings, admin, tenant, *, email: str | None = None, now=NOW):
    return resend_invitation(
        factory,
        settings,
        actor=admin,
        customer_id=tenant.public_id,
        context=CONTEXT,
        email=email,
        now=now,
    )


def accept(factory, token: str, *, password: str = PASSWORD, now=NOW) -> None:
    accept_invitation(factory, token=token, password=password, context=CONTEXT, now=now)


def outbox_row(factory, outbox_id: int) -> DomainOutbox:
    with factory() as session:
        row = session.get(DomainOutbox, outbox_id)
        assert row is not None
        session.expunge(row)
        return row


def token_of(factory, issued: InvitationIssued) -> str:
    """The plaintext token, dug out of the outbox payload."""
    payload = json.loads(outbox_row(factory, issued.outbox_id).payload_json or "{}")
    return str(payload["token"])


def portal_user(factory, tenant: Tenant) -> User:
    with factory() as session:
        user = session.execute(select(User).where(User.tenant_id == tenant.id)).scalar_one()
        session.expunge(user)
        return user


def tokens(factory) -> list[PasswordResetToken]:
    with factory() as session:
        rows = list(
            session.execute(select(PasswordResetToken).order_by(PasswordResetToken.id)).scalars()
        )
        for row in rows:
            session.expunge(row)
        return rows


def outbox_rows(factory) -> list[DomainOutbox]:
    with factory() as session:
        rows = list(session.execute(select(DomainOutbox).order_by(DomainOutbox.id)).scalars())
        for row in rows:
            session.expunge(row)
        return rows


def audits(factory) -> list[AuditLog]:
    with factory() as session:
        rows = list(session.execute(select(AuditLog).order_by(AuditLog.id)).scalars())
        for row in rows:
            session.expunge(row)
        return rows


def counts(factory) -> tuple[int, int, int, int]:
    """(users, tokens, outbox, audit) row counts."""
    with factory() as session:
        users = len(session.execute(select(User.id)).all())
    return users, len(tokens(factory)), len(outbox_rows(factory)), len(audits(factory))


def insert_token(factory, user_id: int, raw: str, purpose: TokenPurpose) -> None:
    """Bypass the services: a token row with the right state and the given purpose."""
    with factory() as session:
        session.add(
            PasswordResetToken(
                user_id=user_id,
                token_hash=hash_opaque_token(raw),
                expires_at=T0 + dt.timedelta(hours=1),
                created_at=T0,
                purpose=purpose,
            )
        )
        session.commit()


# --- 正常路径 -------------------------------------------------------------------


def test_create_writes_the_invited_user_token_outbox_and_audit_together(
    factory, settings, admin
) -> None:
    tenant = make_tenant(factory)

    issued = create(factory, settings, admin, tenant)

    user = portal_user(factory, tenant)
    assert (user.role, user.status, user.tenant_id) == (
        UserRole.CUSTOMER,
        UserStatus.INVITED,
        tenant.id,
    )
    assert (user.email, user.password_version, user.activated_at) == (PORTAL_EMAIL, 0, None)
    # 不可用密码：Argon2id 哈希，秘密没有保留，任何常见输入都验不过。
    assert user.password_hash.startswith("$argon2id$")
    assert not any(verify_password(user.password_hash, guess) for guess in ("", PASSWORD))

    [token] = tokens(factory)
    assert (token.user_id, token.purpose, token.used_at) == (user.id, TokenPurpose.INVITATION, None)
    assert (token.created_at, token.expires_at) == (T0, T0 + TTL)

    [outbox] = outbox_rows(factory)
    assert issued.outbox_id == outbox.id
    assert (outbox.event_type, outbox.aggregate_type, outbox.aggregate_id) == (
        EVENT_PORTAL_INVITATION,
        "users",
        str(user.id),
    )
    payload = json.loads(outbox.payload_json or "{}")
    assert set(payload) == {"to", "token", "expires_at"}
    assert payload["to"] == PORTAL_EMAIL
    assert hash_opaque_token(payload["token"]) == token.token_hash
    # 与令牌行同一个值，整秒。
    assert payload["expires_at"] == (T0 + TTL).isoformat()
    assert dt.datetime.fromisoformat(payload["expires_at"]) == token.expires_at

    [audit] = audits(factory)
    assert audit.action is AuditAction.PORTAL_ACCOUNT_CREATED
    assert (audit.actor_user_id, audit.actor_role) == (admin.id, "ADMIN")
    assert (audit.entity_type, audit.entity_id) == (ENTITY_USERS, str(user.id))
    assert json.loads(audit.after_state or "{}") == {
        "tenant_public_id": tenant.public_id,
        "status": "INVITED",
    }

    assert issued.account == PortalAccountView(
        email=PORTAL_EMAIL,
        status=UserStatus.INVITED,
        invited_at=T0,
        invitation_expires_at=T0 + TTL,
        activated_at=None,
        last_login_at=None,
        two_factor_enabled=False,
    )


def test_the_outbox_row_stays_pending_without_a_worker(factory, settings, admin) -> None:
    """Redis/Celery 丢失（设计 §5、INV-14）：接口照常成功，行留在 `PENDING`、立刻可投。"""
    issued = create(factory, settings, admin, make_tenant(factory))

    row = outbox_row(factory, issued.outbox_id)
    assert (row.status, row.attempt_count, row.processed_at) == (OutboxStatus.PENDING, 0, None)
    assert row.next_retry_at == T0


def test_accepting_activates_the_account_and_bumps_the_password_version(
    factory, settings, admin
) -> None:
    tenant = make_tenant(factory)
    issued = create(factory, settings, admin, tenant)
    later = NOW + dt.timedelta(hours=1)

    assert accept(factory, token_of(factory, issued), now=later) is None

    user = portal_user(factory, tenant)
    assert (user.status, user.password_version) == (UserStatus.ACTIVE, 1)
    assert user.activated_at == later.replace(microsecond=0)
    assert verify_password(user.password_hash, PASSWORD)
    [token] = tokens(factory)
    assert token.used_at == later.replace(microsecond=0)

    created, accepted = audits(factory)
    assert [created.action, accepted.action] == [
        AuditAction.PORTAL_ACCOUNT_CREATED,
        AuditAction.PORTAL_INVITATION_ACCEPTED,
    ]
    assert (accepted.actor_user_id, accepted.actor_role) == (user.id, "CUSTOMER")
    assert (accepted.entity_type, accepted.entity_id) == (ENTITY_USERS, str(user.id))
    assert json.loads(accepted.after_state or "{}") == {
        "tenant_public_id": tenant.public_id,
        "status": "ACTIVE",
    }


def test_accepting_voids_the_other_unused_invitations(factory, settings, admin) -> None:
    tenant = make_tenant(factory)
    create(factory, settings, admin, tenant)
    user = portal_user(factory, tenant)
    # 绕过服务再放一张未用的邀请令牌（正常途径下重发会先作废旧的）。
    insert_token(factory, user.id, "a-second-live-invitation", TokenPurpose.INVITATION)
    first = tokens(factory)[0]
    with factory() as session:
        session.execute(
            update(PasswordResetToken)
            .where(PasswordResetToken.id == first.id)
            .values(token_hash=hash_opaque_token("the-first-invitation"))
        )
        session.commit()

    accept(factory, "the-first-invitation")

    assert all(row.used_at is not None for row in tokens(factory))


# --- 查看与邀请字段 -------------------------------------------------------------


def test_viewing_an_unknown_customer_or_one_without_an_account(factory) -> None:
    with pytest.raises(CustomerNotFound):
        get_portal_account(factory, customer_id=UNKNOWN_CUSTOMER)
    tenant = make_tenant(factory)
    with pytest.raises(PortalAccountNotFound) as caught:
        get_portal_account(factory, customer_id=tenant.public_id)
    assert (caught.value.code, caught.value.http_status) == ("PORTAL_ACCOUNT_NOT_FOUND", 404)


def test_the_invitation_fields_follow_the_latest_invitation(factory, settings, admin) -> None:
    """设计 §7「邀请字段」里属于本任务的几步：刚开通、重发之后、接受之后、令牌过期之后。"""
    tenant = make_tenant(factory)
    create(factory, settings, admin, tenant)
    just_created = get_portal_account(factory, customer_id=tenant.public_id, now=NOW)
    assert (just_created.invited_at, just_created.invitation_expires_at) == (T0, T0 + TTL)

    t1 = T0 + dt.timedelta(hours=5)
    issued = resend(factory, settings, admin, tenant, now=t1)
    after_resend = get_portal_account(factory, customer_id=tenant.public_id, now=t1)
    assert (after_resend.invited_at, after_resend.invitation_expires_at) == (t1, t1 + TTL)
    assert issued.account == after_resend

    t2 = t1 + dt.timedelta(hours=1)
    accept(factory, token_of(factory, issued), now=t2)
    after_accept = get_portal_account(factory, customer_id=tenant.public_id, now=t2)
    assert (after_accept.invited_at, after_accept.invitation_expires_at) == (t1, None)
    assert (after_accept.status, after_accept.activated_at) == (UserStatus.ACTIVE, t2)


def test_an_expired_invitation_has_no_expiry_to_show(factory, settings, admin) -> None:
    tenant = make_tenant(factory)
    create(factory, settings, admin, tenant)

    at_expiry = get_portal_account(factory, customer_id=tenant.public_id, now=T0 + TTL)
    a_second_before = get_portal_account(
        factory, customer_id=tenant.public_id, now=T0 + TTL - dt.timedelta(seconds=1)
    )

    assert (at_expiry.invited_at, at_expiry.invitation_expires_at) == (T0, None)
    assert a_second_before.invitation_expires_at == T0 + TTL


def test_an_account_never_invited_shows_no_invitation(factory) -> None:
    tenant = make_tenant(factory)
    make_user(
        factory,
        email=PORTAL_EMAIL,
        password=PASSWORD,
        role=UserRole.CUSTOMER,
        status=UserStatus.INVITED,
        tenant_id=tenant.id,
    )

    view = get_portal_account(factory, customer_id=tenant.public_id, now=NOW)

    assert (view.invited_at, view.invitation_expires_at) == (None, None)


# --- 令牌边界 -------------------------------------------------------------------


def test_a_token_is_still_good_one_second_before_it_expires(factory, settings, admin) -> None:
    tenant = make_tenant(factory)
    issued = create(factory, settings, admin, tenant)

    accept(factory, token_of(factory, issued), now=T0 + TTL - dt.timedelta(seconds=1))

    assert portal_user(factory, tenant).status is UserStatus.ACTIVE


@pytest.mark.parametrize("offset", [dt.timedelta(0), dt.timedelta(microseconds=500_000)])
def test_a_token_is_dead_at_its_expiry(factory, settings, admin, offset) -> None:
    """`expires_at > now` 才有效，与重置一致；当刻（含同一秒内的小数部分）已经失效。"""
    tenant = make_tenant(factory)
    issued = create(factory, settings, admin, tenant)

    with pytest.raises(InvalidInvitationToken) as caught:
        accept(factory, token_of(factory, issued), now=T0 + TTL + offset)

    assert (caught.value.code, caught.value.http_status) == ("INVALID_INVITATION_TOKEN", 400)
    user = portal_user(factory, tenant)
    assert (user.status, user.password_version) == (UserStatus.INVITED, 0)
    assert tokens(factory)[0].used_at is None


def test_an_unknown_token_is_refused(factory) -> None:
    with pytest.raises(InvalidInvitationToken):
        accept(factory, "no-such-token")


# --- 邮箱归一与占用 -------------------------------------------------------------


def test_the_login_email_is_normalised_before_it_is_stored(factory, settings, admin) -> None:
    tenant = make_tenant(factory)

    issued = create(factory, settings, admin, tenant, email="  Portal@Example.COM ")

    assert portal_user(factory, tenant).email == PORTAL_EMAIL
    assert issued.account.email == PORTAL_EMAIL
    assert json.loads(outbox_row(factory, issued.outbox_id).payload_json or "{}")["to"] == (
        PORTAL_EMAIL
    )


@pytest.mark.parametrize("variant", [" PORTAL@example.com", "portal@EXAMPLE.com  "])
def test_an_address_already_used_by_a_customer_is_refused(
    factory, settings, admin, variant
) -> None:
    create(factory, settings, admin, make_tenant(factory))
    before = counts(factory)

    with pytest.raises(EmailInUse) as caught:
        create(factory, settings, admin, make_tenant(factory), email=variant)

    assert (caught.value.code, caught.value.http_status) == ("EMAIL_IN_USE", 409)
    assert counts(factory) == before


def test_an_address_already_used_by_an_admin_is_refused(factory, settings, admin) -> None:
    before = counts(factory)

    with pytest.raises(EmailInUse):
        create(factory, settings, admin, make_tenant(factory), email=" Admin@Example.com")

    assert counts(factory) == before


def test_a_second_account_for_the_same_customer_is_refused(factory, settings, admin) -> None:
    tenant = make_tenant(factory)
    create(factory, settings, admin, tenant)
    before = counts(factory)

    with pytest.raises(PortalAccountExists) as caught:
        create(factory, settings, admin, tenant, email="someone-else@example.com")

    assert (caught.value.code, caught.value.http_status) == ("PORTAL_ACCOUNT_EXISTS", 409)
    # 响应里不带邮箱（设计 §4「幂等与重放」）。
    assert PORTAL_EMAIL not in str(caught.value)
    assert counts(factory) == before


def test_creating_for_an_unknown_customer_writes_nothing(factory, settings, admin) -> None:
    before = counts(factory)
    with pytest.raises(CustomerNotFound):
        create_portal_account(
            factory,
            settings,
            actor=admin,
            customer_id=UNKNOWN_CUSTOMER,
            email=PORTAL_EMAIL,
            context=CONTEXT,
            now=NOW,
        )
    assert counts(factory) == before


# --- 租户 CLOSED ----------------------------------------------------------------


def test_creating_for_a_closed_customer_is_refused(factory, settings, admin) -> None:
    tenant = make_tenant(factory, AccountStatus.CLOSED)
    before = counts(factory)

    with pytest.raises(AccountClosed) as caught:
        create(factory, settings, admin, tenant)

    assert (caught.value.code, caught.value.http_status) == ("ACCOUNT_CLOSED", 409)
    assert counts(factory) == before


@pytest.mark.parametrize(
    "status",
    [AccountStatus.PENDING_ACTIVATION, AccountStatus.ENABLED, AccountStatus.DISABLED],
)
def test_creating_is_allowed_for_every_other_account_status(
    factory, settings, admin, status
) -> None:
    tenant = make_tenant(factory, status)
    create(factory, settings, admin, tenant)
    assert portal_user(factory, tenant).status is UserStatus.INVITED


def test_resending_for_a_closed_customer_is_refused(factory, settings, admin) -> None:
    tenant = make_tenant(factory)
    create(factory, settings, admin, tenant)
    set_account_status(factory, tenant, AccountStatus.CLOSED)
    before = counts(factory)

    with pytest.raises(AccountClosed):
        resend(factory, settings, admin, tenant, email="new@example.com")

    assert counts(factory) == before
    assert portal_user(factory, tenant).email == PORTAL_EMAIL
    assert tokens(factory)[0].used_at is None


def test_accepting_for_a_closed_customer_is_refused(factory, settings, admin) -> None:
    tenant = make_tenant(factory)
    issued = create(factory, settings, admin, tenant)
    set_account_status(factory, tenant, AccountStatus.CLOSED)
    before = counts(factory)

    with pytest.raises(AccountClosed):
        accept(factory, token_of(factory, issued))

    assert counts(factory) == before
    user = portal_user(factory, tenant)
    assert (user.status, user.password_version) == (UserStatus.INVITED, 0)
    assert tokens(factory)[0].used_at is None


# --- 令牌隔离 -------------------------------------------------------------------


def test_a_reset_token_cannot_accept_an_invitation(factory, settings, admin) -> None:
    """状态对（`INVITED`）、用途错（`PASSWORD_RESET`）：只有用途过滤能拦下它。"""
    tenant = make_tenant(factory)
    create(factory, settings, admin, tenant)
    user = portal_user(factory, tenant)
    insert_token(factory, user.id, "a-reset-token-for-an-invited-user", TokenPurpose.PASSWORD_RESET)

    with pytest.raises(InvalidInvitationToken):
        accept(factory, "a-reset-token-for-an-invited-user")

    after = portal_user(factory, tenant)
    assert (after.status, after.password_hash, after.password_version) == (
        UserStatus.INVITED,
        user.password_hash,
        0,
    )
    assert all(row.used_at is None for row in tokens(factory))


def test_an_invitation_token_cannot_reset_a_password(factory, settings, admin) -> None:
    """状态对（`ACTIVE`）、用途错（`INVITATION`）：直接调重置服务。"""
    insert_token(factory, admin.id, "an-invitation-token-for-an-admin", TokenPurpose.INVITATION)

    with pytest.raises(InvalidResetToken):
        reset_password(
            factory,
            settings,
            token="an-invitation-token-for-an-admin",
            new_password=PASSWORD,
            context=CONTEXT,
            now=NOW,
        )

    with factory() as session:
        after = session.get(User, admin.id)
        assert after is not None
        assert (after.password_hash, after.password_version) == (admin.password_hash, 0)
    assert [row.used_at for row in tokens(factory)] == [None]


def test_an_invited_customer_asking_for_a_reset_gets_no_token(factory, settings, admin) -> None:
    create(factory, settings, admin, make_tenant(factory))
    before = (len(tokens(factory)), len(outbox_rows(factory)))

    assert request_reset(factory, settings, email=PORTAL_EMAIL, context=CONTEXT) is None

    assert (len(tokens(factory)), len(outbox_rows(factory))) == before


# --- 判定顺序与重放 -------------------------------------------------------------


def test_a_used_token_with_a_weak_password_is_an_invalid_token(factory, settings, admin) -> None:
    issued = create(factory, settings, admin, make_tenant(factory))
    token = token_of(factory, issued)
    accept(factory, token)

    with pytest.raises(InvalidInvitationToken):
        accept(factory, token, password=WEAK)


def test_a_weak_password_does_not_burn_a_live_token(factory, settings, admin) -> None:
    tenant = make_tenant(factory)
    token = token_of(factory, create(factory, settings, admin, tenant))

    with pytest.raises(WeakPassword) as caught:
        accept(factory, token, password=WEAK)

    assert (caught.value.code, caught.value.http_status) == ("WEAK_PASSWORD", 400)
    assert tokens(factory)[0].used_at is None
    assert portal_user(factory, tenant).status is UserStatus.INVITED

    accept(factory, token)
    assert portal_user(factory, tenant).status is UserStatus.ACTIVE


def test_a_closed_customer_wins_over_a_weak_password(factory, settings, admin) -> None:
    tenant = make_tenant(factory)
    token = token_of(factory, create(factory, settings, admin, tenant))
    set_account_status(factory, tenant, AccountStatus.CLOSED)

    with pytest.raises(AccountClosed):
        accept(factory, token, password=WEAK)


@pytest.mark.parametrize("status", [UserStatus.ACTIVE, UserStatus.DISABLED])
def test_a_live_token_of_an_account_that_is_not_invited_is_refused(
    factory, settings, admin, status
) -> None:
    tenant = make_tenant(factory)
    token = token_of(factory, create(factory, settings, admin, tenant))
    with factory() as session:
        session.execute(update(User).where(User.tenant_id == tenant.id).values(status=status))
        session.commit()

    with pytest.raises(InvalidInvitationToken):
        accept(factory, token)

    assert tokens(factory)[0].used_at is None


def test_the_same_token_twice_keeps_the_first_password(factory, settings, admin) -> None:
    tenant = make_tenant(factory)
    token = token_of(factory, create(factory, settings, admin, tenant))
    accept(factory, token)

    with pytest.raises(InvalidInvitationToken):
        accept(factory, token, password=OTHER_PASSWORD)

    user = portal_user(factory, tenant)
    assert verify_password(user.password_hash, PASSWORD)
    assert user.password_version == 1


def test_an_old_link_is_dead_after_a_resend(factory, settings, admin) -> None:
    tenant = make_tenant(factory)
    old = token_of(factory, create(factory, settings, admin, tenant))
    new = token_of(factory, resend(factory, settings, admin, tenant))

    with pytest.raises(InvalidInvitationToken):
        accept(factory, old)
    accept(factory, new)

    assert portal_user(factory, tenant).status is UserStatus.ACTIVE


# --- 重发 -----------------------------------------------------------------------


def last_resent_audit(factory) -> dict[str, object]:
    row = audits(factory)[-1]
    assert row.action is AuditAction.PORTAL_INVITATION_RESENT
    return dict(json.loads(row.after_state or "{}"))


@pytest.mark.parametrize("email", [None, "  PORTAL@Example.com "])
def test_resending_to_the_same_address_is_not_an_email_change(
    factory, settings, admin, email
) -> None:
    tenant = make_tenant(factory)
    create(factory, settings, admin, tenant)
    # 当前邮箱本身就被这个账号「占用」：没改邮箱时做了占用检查，就会误报 EMAIL_IN_USE。
    t1 = T0 + dt.timedelta(minutes=10)

    issued = resend(factory, settings, admin, tenant, email=email, now=t1)

    assert portal_user(factory, tenant).email == PORTAL_EMAIL
    assert last_resent_audit(factory) == {
        "tenant_public_id": tenant.public_id,
        "status": "INVITED",
        "email_changed": False,
    }
    first, second = tokens(factory)
    assert (first.used_at, second.used_at) == (t1, None)
    assert (second.created_at, second.expires_at) == (t1, t1 + TTL)
    payload = json.loads(outbox_row(factory, issued.outbox_id).payload_json or "{}")
    assert (payload["to"], payload["expires_at"]) == (PORTAL_EMAIL, (t1 + TTL).isoformat())


def test_resending_to_an_address_in_use_is_refused(factory, settings, admin) -> None:
    tenant = make_tenant(factory)
    create(factory, settings, admin, tenant)
    before = counts(factory)

    with pytest.raises(EmailInUse):
        resend(factory, settings, admin, tenant, email=" ADMIN@example.com")

    assert counts(factory) == before
    assert portal_user(factory, tenant).email == PORTAL_EMAIL
    assert tokens(factory)[0].used_at is None


def test_resending_to_a_new_address_changes_it(factory, settings, admin) -> None:
    tenant = make_tenant(factory)
    create(factory, settings, admin, tenant)

    issued = resend(factory, settings, admin, tenant, email=" Fixed@Example.com ")

    assert portal_user(factory, tenant).email == "fixed@example.com"
    assert issued.account.email == "fixed@example.com"
    assert last_resent_audit(factory)["email_changed"] is True
    payload = json.loads(outbox_row(factory, issued.outbox_id).payload_json or "{}")
    assert payload["to"] == "fixed@example.com"


def test_resending_an_active_account_is_refused(factory, settings, admin) -> None:
    tenant = make_tenant(factory)
    accept(factory, token_of(factory, create(factory, settings, admin, tenant)))
    before = counts(factory)

    with pytest.raises(PortalAccountNotInvited) as caught:
        resend(factory, settings, admin, tenant)

    assert (caught.value.code, caught.value.http_status) == ("PORTAL_ACCOUNT_NOT_INVITED", 409)
    assert counts(factory) == before


def test_resending_without_an_account_is_not_found(factory, settings, admin) -> None:
    with pytest.raises(PortalAccountNotFound):
        resend(factory, settings, admin, make_tenant(factory))
    with pytest.raises(CustomerNotFound):
        resend_invitation(
            factory, settings, actor=admin, customer_id=UNKNOWN_CUSTOMER, context=CONTEXT
        )


# --- 事务中途失败 ---------------------------------------------------------------


def test_a_failure_while_inserting_the_outbox_row_leaves_nothing_behind(
    factory, settings, admin
) -> None:
    """INV-13：在 outbox 插入那一步注入 `OperationalError`，用户、令牌、outbox、审计都没有新行。"""
    tenant = make_tenant(factory)
    before = counts(factory)

    def explode(*_args) -> None:
        raise OperationalError("INSERT INTO domain_outbox", {}, Exception("injected"))

    event.listen(DomainOutbox, "before_insert", explode)
    try:
        with pytest.raises(OperationalError):
            create(factory, settings, admin, tenant)
    finally:
        event.remove(DomainOutbox, "before_insert", explode)

    assert counts(factory) == before
    # 原样重试得到确定的成功（设计 §5）。
    create(factory, settings, admin, tenant)
    assert portal_user(factory, tenant).status is UserStatus.INVITED


def test_a_failure_while_writing_the_accept_audit_keeps_the_link_usable(
    factory, settings, admin, monkeypatch
) -> None:
    tenant = make_tenant(factory)
    token = token_of(factory, create(factory, settings, admin, tenant))
    before = counts(factory)

    def explode(*_args, **_kwargs) -> None:
        raise RuntimeError("injected")

    monkeypatch.setattr(portal_accounts, "record_audit", explode)
    with pytest.raises(RuntimeError):
        accept(factory, token)
    monkeypatch.undo()

    assert counts(factory) == before
    user = portal_user(factory, tenant)
    assert (user.status, user.password_version, user.activated_at) == (UserStatus.INVITED, 0, None)
    assert tokens(factory)[0].used_at is None

    accept(factory, token)
    assert portal_user(factory, tenant).status is UserStatus.ACTIVE


# --- 泄漏 -----------------------------------------------------------------------


def test_nothing_secret_reaches_logs_audit_or_errors(factory, settings, admin, caplog) -> None:
    """密码、令牌原文不进日志、审计与异常；门户审计的状态里没有邮箱值（设计 §6）。"""
    caplog.set_level(logging.DEBUG)
    tenant = make_tenant(factory)
    errors: list[AppError] = []

    issued = create(factory, settings, admin, tenant)
    first = token_of(factory, issued)
    resent = resend(factory, settings, admin, tenant, email="changed@example.com")
    second = token_of(factory, resent)
    for attempt in (
        lambda: accept(factory, first),
        lambda: accept(factory, second, password=WEAK),
        lambda: create(factory, settings, admin, tenant),
        lambda: create(factory, settings, admin, make_tenant(factory), email="changed@example.com"),
    ):
        with pytest.raises(AppError) as caught:
            attempt()
        errors.append(caught.value)
    accept(factory, second)
    with pytest.raises(AppError) as caught:
        accept(factory, second, password=OTHER_PASSWORD)
    errors.append(caught.value)

    secrets = [first, second, PASSWORD, WEAK, OTHER_PASSWORD]
    texts = [record.getMessage() for record in caplog.records]
    texts += [str(error) for error in errors]
    texts += [repr(issued), repr(resent)]
    for row in audits(factory):
        texts += [row.before_state or "", row.after_state or "", row.reason or ""]
    assert not [(s, t) for s in secrets for t in texts if s in t]

    portal = [row for row in audits(factory) if row.action.value.startswith("PORTAL_")]
    assert len(portal) == 3
    for row in portal:
        state = row.after_state or ""
        assert PORTAL_EMAIL not in state
        assert "changed@example.com" not in state
        assert "@" not in state

"""Portal account services (design gate #368 v10 §2, §4, §5, §6, §7).

覆盖设计 §7 里属于服务层（任务 B）的行。AIH-TASK-091：正常路径的状态与审计、「邀请字段」、令牌
边界、邮箱归一与占用、租户 `CLOSED`、令牌隔离、判定顺序、重发邮箱比较、事务中途失败、
Redis/Celery 丢失（只看 outbox 行是 `PENDING`，恢复扫描归 AIH-TASK-094）、泄漏。AIH-TASK-098：
「停用 → 启用」、「邀请字段」中停用与启用那几步、「重置客户 2FA」、改密码后的旧会话（客户路径）
的服务层部分、停用 / 启用 / 重置 2FA 的事务中途失败。

全部跑在内存 SQLite 上；加锁与并发只能对着真 MySQL 验，见
tests/backend/test_portal_accounts_concurrency.py。访问令牌是否仍可用，用接口层同一个
`require_current_user` 判定（喂给它一个只带请求头与 `app.state` 的替身）。
"""

from __future__ import annotations

import base64
import datetime as dt
import json
import logging
import os
from types import SimpleNamespace

import pyotp
import pytest
from sqlalchemy import create_engine, event, select, update
from sqlalchemy.exc import OperationalError

from app.api.auth import require_current_user
from app.core.config import Settings
from app.core.database import create_session_factory
from app.core.errors import AppError
from app.core.passwords import WeakPassword, hash_password, verify_password
from app.core.tokens import (
    InvalidToken,
    hash_opaque_token,
    hash_refresh_token,
    issue_access_token,
)
from app.models.auth import (
    AuditAction,
    AuditLog,
    DomainOutbox,
    OutboxStatus,
    PasswordResetToken,
    RecoveryCode,
    RefreshToken,
    TokenPurpose,
    TwoFactorSetting,
    User,
    UserRole,
    UserStatus,
)
from app.models.base import Base
from app.models.tenancy import AccountStatus, Tenant
from app.repositories import tenancy
from app.services import portal_accounts
from app.services.auth import (
    IssuedSession,
    RequestContext,
    TokenReused,
    authenticate,
    refresh_session,
    utc_now,
)
from app.services.customers import CustomerNotFound
from app.services.password_reset import InvalidResetToken, request_reset, reset_password
from app.services.portal_accounts import (
    ENTITY_USERS,
    EVENT_PORTAL_INVITATION,
    EmailInUse,
    InvalidInvitationToken,
    InvalidResetReason,
    InvitationIssued,
    PortalAccountAlreadyDisabled,
    PortalAccountExists,
    PortalAccountNotDisabled,
    PortalAccountNotFound,
    PortalAccountNotInvited,
    PortalAccountView,
    PortalTwoFactorNotEnabled,
    accept_invitation,
    create_portal_account,
    disable_portal_account,
    enable_portal_account,
    get_portal_account,
    resend_invitation,
    reset_portal_two_factor,
)
from app.services.project_integration import AccountClosed
from app.services.two_factor import confirm_enrolment, start_enrolment

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
PLACEHOLDER_SESSION = "00000000-0000-0000-0000-000000000000"
REASON = "Lost phone; identity confirmed by a call-back to the registered number."


@pytest.fixture
def settings(tmp_path) -> Settings:
    # 签名密钥与主密钥只给停用 / 启用 / 重置 2FA 那几条用例签发会话、注册 2FA 用。
    key = tmp_path / "jwt.key"
    key.write_text("a-test-signing-key-that-is-long-enough", encoding="utf-8")
    master = tmp_path / "master.key"
    master.write_text(f"1:{base64.b64encode(os.urandom(32)).decode()}\n", encoding="utf-8")
    return Settings(
        customer_invitation_ttl_seconds=TTL_SECONDS,
        password_reset_ttl_seconds=1_800,
        jwt_secret_file=str(key),
        master_key_file=str(master),
    )


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


# --- 停用、启用、重置 2FA 的帮手（AIH-TASK-098） --------------------------------


def disable(factory, admin, tenant, *, now=NOW) -> PortalAccountView:
    return disable_portal_account(
        factory, actor=admin, customer_id=tenant.public_id, context=CONTEXT, now=now
    )


def enable(factory, admin, tenant, *, now=NOW) -> PortalAccountView:
    return enable_portal_account(
        factory, actor=admin, customer_id=tenant.public_id, context=CONTEXT, now=now
    )


def reset_2fa(factory, admin, tenant, *, reason: str = REASON, now=NOW) -> PortalAccountView:
    return reset_portal_two_factor(
        factory,
        actor=admin,
        customer_id=tenant.public_id,
        reason=reason,
        context=CONTEXT,
        now=now,
    )


def activate(factory, settings, admin, tenant) -> User:
    """Create and accept: an `ACTIVE` portal account at `password_version` 1."""
    accept(factory, token_of(factory, create(factory, settings, admin, tenant)))
    user = portal_user(factory, tenant)
    assert (user.status, user.password_version) == (UserStatus.ACTIVE, 1)
    return user


def login(factory, settings) -> IssuedSession:
    """Customer login, no 2FA: a session straight away (wall clock, so the JWT is current)."""
    outcome = authenticate(
        factory, settings, email=PORTAL_EMAIL, password=PASSWORD, context=CONTEXT
    )
    assert outcome.issued is not None, outcome
    return outcome.issued


def current_user(factory, settings, access_token: str) -> User:
    """What `require_current_user` makes of this access token (a stand-in request)."""
    request = SimpleNamespace(
        headers={"authorization": f"Bearer {access_token}"},
        app=SimpleNamespace(state=SimpleNamespace(settings=settings, session_factory=factory)),
    )
    return require_current_user(request)  # type: ignore[arg-type]


def refresh(factory, settings, issued: IssuedSession) -> IssuedSession:
    return refresh_session(factory, settings, raw_token=issued.refresh_token, context=CONTEXT)


def insert_refresh_token(factory, user_id: int, raw: str, *, password_version: int) -> None:
    with factory() as session:
        session.add(
            RefreshToken(
                user_id=user_id,
                family_id=PLACEHOLDER_SESSION,
                token_hash=hash_refresh_token(raw),
                issued_at=T0,
                expires_at=T0 + dt.timedelta(days=1),
                password_version=password_version,
            )
        )
        session.commit()


def insert_two_factor(factory, user_id: int, *, confirmed: bool, codes: int = 2) -> None:
    """Bypass the 2FA services: a confirmed or pending enrolment and some recovery codes."""
    with factory() as session:
        session.add(
            TwoFactorSetting(
                user_id=user_id,
                encrypted_totp_secret="not-a-real-ciphertext",
                key_version=1,
                secret_version=1,
                confirmed_at=T0 if confirmed else None,
                created_at=T0,
                updated_at=T0,
            )
        )
        for _ in range(codes):
            session.add(RecoveryCode(user_id=user_id, code_hash="not-a-real-hash", created_at=T0))
        session.commit()


def two_factor_rows(factory) -> tuple[int, int]:
    """(two_factor_settings, recovery_codes) row counts."""
    with factory() as session:
        settings_rows = len(session.execute(select(TwoFactorSetting.user_id)).all())
        codes = len(session.execute(select(RecoveryCode.id)).all())
    return settings_rows, codes


def refresh_rows(factory) -> list[RefreshToken]:
    with factory() as session:
        rows = list(session.execute(select(RefreshToken).order_by(RefreshToken.id)).scalars())
        for row in rows:
            session.expunge(row)
        return rows


def snapshot(factory, tenant: Tenant) -> tuple[object, ...]:
    """Everything a portal admin action may touch, for「整体回滚」assertions."""
    user = portal_user(factory, tenant)
    return (
        user.status,
        user.password_version,
        user.updated_at,
        [(row.id, row.used_at) for row in tokens(factory)],
        [(row.id, row.revoked_at) for row in refresh_rows(factory)],
        two_factor_rows(factory),
        counts(factory),
    )


def last_audit(factory, action: AuditAction) -> AuditLog:
    row = audits(factory)[-1]
    assert row.action is action
    return row


def explode_on_audit(monkeypatch) -> None:
    """Fail the transaction at its last step, writing the audit row."""

    def explode(*_args, **_kwargs) -> None:
        raise OperationalError("INSERT INTO audit_logs", {}, Exception("injected"))

    monkeypatch.setattr(portal_accounts, "record_audit", explode)


# --- 停用 → 启用 ----------------------------------------------------------------


def test_disable_then_enable_a_never_activated_account_returns_it_to_invited(
    factory, settings, admin
) -> None:
    """设计 §7「停用 → 启用」（从未激活）与「邀请字段」的停用、启用两步。"""
    tenant = make_tenant(factory)
    issued = create(factory, settings, admin, tenant)
    old_link = token_of(factory, issued)
    t1, t2 = T0 + dt.timedelta(hours=1), T0 + dt.timedelta(hours=2)

    disabled = disable(factory, admin, tenant, now=t1)

    assert disabled.status is UserStatus.DISABLED
    assert (disabled.invited_at, disabled.invitation_expires_at) == (T0, None)
    assert get_portal_account(factory, customer_id=tenant.public_id, now=t1) == disabled
    user = portal_user(factory, tenant)
    assert (user.status, user.password_version, user.updated_at) == (UserStatus.DISABLED, 1, t1)
    assert [row.used_at for row in tokens(factory)] == [t1]
    audit = last_audit(factory, AuditAction.PORTAL_ACCOUNT_DISABLED)
    assert (audit.actor_user_id, audit.actor_role) == (admin.id, "ADMIN")
    assert (audit.entity_type, audit.entity_id) == (ENTITY_USERS, str(user.id))
    assert json.loads(audit.after_state or "{}") == {
        "tenant_public_id": tenant.public_id,
        "status": "DISABLED",
    }

    enabled = enable(factory, admin, tenant, now=t2)

    # 回到 INVITED；旧邀请在停用时已作废，所以没有可显示的到期时刻，需要重发。
    assert enabled.status is UserStatus.INVITED
    assert (enabled.invited_at, enabled.invitation_expires_at, enabled.activated_at) == (
        T0,
        None,
        None,
    )
    assert get_portal_account(factory, customer_id=tenant.public_id, now=t2) == enabled
    user = portal_user(factory, tenant)
    assert (user.status, user.password_version) == (UserStatus.INVITED, 1)
    audit = last_audit(factory, AuditAction.PORTAL_ACCOUNT_ENABLED)
    assert (audit.actor_user_id, audit.entity_id) == (admin.id, str(user.id))
    assert json.loads(audit.after_state or "{}") == {
        "tenant_public_id": tenant.public_id,
        "status": "INVITED",
    }
    # 不自动重发邀请。
    assert len(outbox_rows(factory)) == 1
    assert len(tokens(factory)) == 1

    with pytest.raises(InvalidInvitationToken):
        accept(factory, old_link, now=t2)
    accept(factory, token_of(factory, resend(factory, settings, admin, tenant, now=t2)), now=t2)
    assert portal_user(factory, tenant).status is UserStatus.ACTIVE


def test_disable_then_enable_an_activated_account_keeps_old_sessions_dead(
    factory, settings, admin
) -> None:
    """设计 §7「停用 → 启用」（已激活）：重新启用不会让停用前的令牌复活，版本号比停用前大 1。"""
    tenant = make_tenant(factory)
    activate(factory, settings, admin, tenant)
    issued = login(factory, settings)
    assert current_user(factory, settings, issued.access_token).tenant_id == tenant.id
    version_before = portal_user(factory, tenant).password_version

    disable(factory, admin, tenant)

    # 停用期间：访问令牌立即 401，刷新令牌已吊销。
    with pytest.raises(InvalidToken) as caught:
        current_user(factory, settings, issued.access_token)
    assert (caught.value.code, caught.value.http_status) == ("TOKEN_INVALID", 401)
    with pytest.raises(TokenReused):
        refresh(factory, settings, issued)
    assert portal_user(factory, tenant).password_version == version_before + 1
    assert all(row.revoked_at == T0 for row in refresh_rows(factory))

    enabled = enable(factory, admin, tenant)

    assert enabled.status is UserStatus.ACTIVE
    assert (enabled.activated_at, enabled.invitation_expires_at) == (T0, None)
    after = portal_user(factory, tenant)
    assert (after.status, after.password_version) == (UserStatus.ACTIVE, version_before + 1)
    # 重新启用之后，停用前的访问令牌与刷新令牌仍然无效。
    with pytest.raises(InvalidToken) as caught:
        current_user(factory, settings, issued.access_token)
    assert (caught.value.code, caught.value.http_status) == ("TOKEN_INVALID", 401)
    with pytest.raises(TokenReused):
        refresh(factory, settings, issued)
    # 重新登录签发的新令牌可用。
    fresh = login(factory, settings)
    assert current_user(factory, settings, fresh.access_token).password_version == (
        version_before + 1
    )


def test_disabling_voids_every_unused_token_and_revokes_every_refresh_token(
    factory, settings, admin
) -> None:
    tenant = make_tenant(factory)
    user = activate(factory, settings, admin, tenant)
    [invitation] = tokens(factory)
    insert_token(factory, user.id, "a-live-reset-token", TokenPurpose.PASSWORD_RESET)
    insert_token(factory, user.id, "a-stray-invitation", TokenPurpose.INVITATION)
    insert_refresh_token(factory, user.id, "a-first-refresh-token", password_version=1)
    insert_refresh_token(factory, user.id, "a-second-refresh-token", password_version=1)
    t1 = T0 + dt.timedelta(hours=1)

    disable(factory, admin, tenant, now=t1)

    used = {row.id: row.used_at for row in tokens(factory)}
    # 已用的邀请保留原来的时刻；两种用途的未用令牌都作废。
    assert used.pop(invitation.id) == T0
    assert list(used.values()) == [t1, t1]
    assert [row.revoked_at for row in refresh_rows(factory)] == [t1, t1]
    # 在令牌到期之前用它：被拒是因为已作废，不是因为过期。
    with pytest.raises(InvalidResetToken):
        reset_password(
            factory,
            settings,
            token="a-live-reset-token",
            new_password=OTHER_PASSWORD,
            context=CONTEXT,
            now=NOW,
        )


def test_disabling_twice_is_refused(factory, settings, admin) -> None:
    tenant = make_tenant(factory)
    create(factory, settings, admin, tenant)
    disable(factory, admin, tenant)
    before = snapshot(factory, tenant)

    with pytest.raises(PortalAccountAlreadyDisabled) as caught:
        disable(factory, admin, tenant, now=NOW + dt.timedelta(hours=1))

    assert (caught.value.code, caught.value.http_status) == ("PORTAL_ACCOUNT_ALREADY_DISABLED", 409)
    assert snapshot(factory, tenant) == before


def test_disabling_is_allowed_for_a_closed_customer(factory, settings, admin) -> None:
    """停用没有租户前置条件（设计 §4 状态表）：关户之后照样能停用门户账号。"""
    tenant = make_tenant(factory)
    activate(factory, settings, admin, tenant)
    set_account_status(factory, tenant, AccountStatus.CLOSED)

    assert disable(factory, admin, tenant).status is UserStatus.DISABLED


@pytest.mark.parametrize("activated", [False, True])
def test_enabling_an_account_that_is_not_disabled_is_refused(
    factory, settings, admin, activated
) -> None:
    tenant = make_tenant(factory)
    if activated:
        activate(factory, settings, admin, tenant)
    else:
        create(factory, settings, admin, tenant)
    before = snapshot(factory, tenant)

    with pytest.raises(PortalAccountNotDisabled) as caught:
        enable(factory, admin, tenant)

    assert (caught.value.code, caught.value.http_status) == ("PORTAL_ACCOUNT_NOT_DISABLED", 409)
    assert snapshot(factory, tenant) == before


def test_enabling_checks_the_account_before_the_customer(factory, settings, admin) -> None:
    """判定顺序：先看账号状态，再看租户。没停用的账号配 `CLOSED` 租户是 NOT_DISABLED。"""
    tenant = make_tenant(factory)
    create(factory, settings, admin, tenant)
    set_account_status(factory, tenant, AccountStatus.CLOSED)

    with pytest.raises(PortalAccountNotDisabled):
        enable(factory, admin, tenant)


def test_enabling_for_a_closed_customer_is_refused(factory, settings, admin) -> None:
    tenant = make_tenant(factory)
    activate(factory, settings, admin, tenant)
    disable(factory, admin, tenant)
    set_account_status(factory, tenant, AccountStatus.CLOSED)
    before = snapshot(factory, tenant)

    with pytest.raises(AccountClosed) as caught:
        enable(factory, admin, tenant)

    assert (caught.value.code, caught.value.http_status) == ("ACCOUNT_CLOSED", 409)
    assert snapshot(factory, tenant) == before


@pytest.mark.parametrize("action", ["disable", "enable", "reset_2fa"])
def test_admin_actions_on_an_unknown_customer_or_one_without_an_account(
    factory, admin, action
) -> None:
    call = {"disable": disable, "enable": enable, "reset_2fa": reset_2fa}[action]
    before = counts(factory)

    with pytest.raises(CustomerNotFound):
        call(factory, admin, SimpleNamespace(public_id=UNKNOWN_CUSTOMER))
    with pytest.raises(PortalAccountNotFound):
        call(factory, admin, make_tenant(factory))

    assert counts(factory) == before


# --- 重置客户 2FA ---------------------------------------------------------------


def test_resetting_2fa_removes_it_and_ends_every_session(factory, settings, admin) -> None:
    """设计 §7「重置客户 2FA」：客户已开启 2FA 并持有会话。"""
    tenant = make_tenant(factory)
    user = activate(factory, settings, admin, tenant)
    issued = login(factory, settings)
    enrolment = start_enrolment(factory, settings, user_id=user.id, password_version=1)
    confirm_enrolment(
        factory,
        settings,
        user_id=user.id,
        password_version=1,
        code=pyotp.TOTP(enrolment.secret).now(),
        context=CONTEXT,
    )
    assert get_portal_account(factory, customer_id=tenant.public_id).two_factor_enabled
    assert two_factor_rows(factory)[0] == 1
    assert two_factor_rows(factory)[1] > 0
    assert current_user(factory, settings, issued.access_token).id == user.id

    view = reset_2fa(factory, admin, tenant, reason=f"  {REASON} \n")

    assert (view.status, view.two_factor_enabled) == (UserStatus.ACTIVE, False)
    assert two_factor_rows(factory) == (0, 0)
    after = portal_user(factory, tenant)
    assert (after.status, after.password_version, after.updated_at) == (UserStatus.ACTIVE, 2, T0)
    with pytest.raises(InvalidToken):
        current_user(factory, settings, issued.access_token)
    with pytest.raises(TokenReused):
        refresh(factory, settings, issued)

    audit = last_audit(factory, AuditAction.PORTAL_2FA_RESET)
    assert (audit.actor_user_id, audit.actor_role) == (admin.id, "ADMIN")
    assert (audit.entity_type, audit.entity_id) == (ENTITY_USERS, str(user.id))
    assert audit.reason == REASON
    assert json.loads(audit.after_state or "{}") == {"tenant_public_id": tenant.public_id}
    assert "@" not in (audit.after_state or "")

    # 客户随后用密码登录，不再要求 TOTP。
    fresh = login(factory, settings)
    assert current_user(factory, settings, fresh.access_token).password_version == 2


def test_resetting_clears_a_pending_enrolment_too(factory, settings, admin) -> None:
    tenant = make_tenant(factory)
    user = activate(factory, settings, admin, tenant)
    insert_two_factor(factory, user.id, confirmed=False, codes=0)

    view = reset_2fa(factory, admin, tenant)

    assert view.two_factor_enabled is False
    assert two_factor_rows(factory) == (0, 0)
    assert portal_user(factory, tenant).password_version == 2
    last_audit(factory, AuditAction.PORTAL_2FA_RESET)


def test_resetting_a_disabled_account_keeps_it_disabled(factory, settings, admin) -> None:
    tenant = make_tenant(factory)
    user = activate(factory, settings, admin, tenant)
    insert_two_factor(factory, user.id, confirmed=True)
    disable(factory, admin, tenant)

    view = reset_2fa(factory, admin, tenant)

    assert view.status is UserStatus.DISABLED
    assert two_factor_rows(factory) == (0, 0)
    assert portal_user(factory, tenant).password_version == 3


def test_resetting_an_account_without_2fa_is_refused(factory, settings, admin) -> None:
    tenant = make_tenant(factory)
    activate(factory, settings, admin, tenant)
    before = snapshot(factory, tenant)

    with pytest.raises(PortalTwoFactorNotEnabled) as caught:
        reset_2fa(factory, admin, tenant)

    assert (caught.value.code, caught.value.http_status) == ("PORTAL_2FA_NOT_ENABLED", 409)
    assert snapshot(factory, tenant) == before


@pytest.mark.parametrize("reason", ["", "   ", "\t\n", "x" * 256, " " + "x" * 256 + " "])
def test_a_missing_or_overlong_reason_is_refused_before_any_write(
    factory, settings, admin, reason
) -> None:
    tenant = make_tenant(factory)
    user = activate(factory, settings, admin, tenant)
    insert_two_factor(factory, user.id, confirmed=True)
    before = snapshot(factory, tenant)

    with pytest.raises(InvalidResetReason) as caught:
        reset_2fa(factory, admin, tenant, reason=reason)

    assert (caught.value.code, caught.value.http_status) == ("VALIDATION_ERROR", 422)
    assert snapshot(factory, tenant) == before


@pytest.mark.parametrize("padding", ["", "  "])
def test_a_255_character_reason_is_recorded_verbatim(factory, settings, admin, padding) -> None:
    """255 个字符（去首尾空白之后）成功，审计里的原因逐字相同。

    真 MySQL 上的同一条见 tests/backend/test_portal_accounts_concurrency.py。
    """
    tenant = make_tenant(factory)
    user = activate(factory, settings, admin, tenant)
    insert_two_factor(factory, user.id, confirmed=True)
    reason = "原因" + "x" * 253

    reset_2fa(factory, admin, tenant, reason=f"{padding}{reason}{padding}")

    assert len(reason) == 255
    assert last_audit(factory, AuditAction.PORTAL_2FA_RESET).reason == reason
    assert two_factor_rows(factory) == (0, 0)


# --- 改密码后的旧会话（客户路径，服务层部分） ---------------------------------


def test_an_enrolment_started_before_a_password_reset_cannot_be_confirmed(
    factory, settings, admin
) -> None:
    """客户开始注册 2FA 之后重置了密码：用旧会话的版本号确认，401 `TOKEN_INVALID`，不启用。"""
    tenant = make_tenant(factory)
    user = activate(factory, settings, admin, tenant)
    issued = login(factory, settings)
    enrolment = start_enrolment(factory, settings, user_id=user.id, password_version=1)
    insert_token(factory, user.id, "a-reset-token-for-the-customer", TokenPurpose.PASSWORD_RESET)
    reset_password(
        factory,
        settings,
        token="a-reset-token-for-the-customer",
        new_password=OTHER_PASSWORD,
        context=CONTEXT,
        now=NOW,
    )

    with pytest.raises(InvalidToken) as caught:
        confirm_enrolment(
            factory,
            settings,
            user_id=user.id,
            password_version=1,
            code=pyotp.TOTP(enrolment.secret).now(),
            context=CONTEXT,
        )

    assert (caught.value.code, caught.value.http_status) == ("TOKEN_INVALID", 401)
    with factory() as session:
        row = session.get(TwoFactorSetting, user.id)
        assert row is not None
        assert row.confirmed_at is None
    assert two_factor_rows(factory)[1] == 0
    assert not get_portal_account(factory, customer_id=tenant.public_id).two_factor_enabled
    with pytest.raises(InvalidToken):
        current_user(factory, settings, issued.access_token)


def test_a_token_from_before_the_invitation_was_accepted_is_dead(factory, settings, admin) -> None:
    """接受邀请之后，手工签发的版本 0 访问令牌 401 `TOKEN_INVALID`；库里版本号为 1。"""
    tenant = make_tenant(factory)
    user = activate(factory, settings, admin, tenant)
    stale = issue_access_token(
        settings,
        user_id=user.id,
        role=UserRole.CUSTOMER.value,
        session_id=PLACEHOLDER_SESSION,
        now=utc_now(),
        password_version=0,
    )

    with pytest.raises(InvalidToken) as caught:
        current_user(factory, settings, stale)

    assert (caught.value.code, caught.value.http_status) == ("TOKEN_INVALID", 401)
    assert portal_user(factory, tenant).password_version == 1


# --- 事务中途失败（停用、启用、重置 2FA） ---------------------------------------


def test_a_failure_while_writing_the_disable_audit_rolls_everything_back(
    factory, settings, admin, monkeypatch
) -> None:
    tenant = make_tenant(factory)
    user = activate(factory, settings, admin, tenant)
    insert_token(factory, user.id, "a-live-reset-token", TokenPurpose.PASSWORD_RESET)
    insert_refresh_token(factory, user.id, "a-live-refresh-token", password_version=1)
    before = snapshot(factory, tenant)

    explode_on_audit(monkeypatch)
    with pytest.raises(OperationalError):
        disable(factory, admin, tenant, now=NOW + dt.timedelta(hours=1))
    monkeypatch.undo()

    # 状态、版本号、两种令牌与刷新令牌都不变。
    assert snapshot(factory, tenant) == before
    assert portal_user(factory, tenant).status is UserStatus.ACTIVE
    disable(factory, admin, tenant)
    assert portal_user(factory, tenant).status is UserStatus.DISABLED


def test_a_failure_while_writing_the_enable_audit_rolls_everything_back(
    factory, settings, admin, monkeypatch
) -> None:
    tenant = make_tenant(factory)
    activate(factory, settings, admin, tenant)
    disable(factory, admin, tenant)
    before = snapshot(factory, tenant)

    explode_on_audit(monkeypatch)
    with pytest.raises(OperationalError):
        enable(factory, admin, tenant, now=NOW + dt.timedelta(hours=1))
    monkeypatch.undo()

    assert snapshot(factory, tenant) == before
    assert portal_user(factory, tenant).status is UserStatus.DISABLED
    enable(factory, admin, tenant)
    assert portal_user(factory, tenant).status is UserStatus.ACTIVE


def test_a_failure_while_writing_the_reset_audit_rolls_everything_back(
    factory, settings, admin, monkeypatch
) -> None:
    """设计 §5「DB 提交失败（重置 2FA）」：2FA 设置、恢复码、版本号都不变，会话照旧有效。"""
    tenant = make_tenant(factory)
    user = activate(factory, settings, admin, tenant)
    # 先登录再放 2FA 行：已确认 2FA 的账号登录会停在 TOTP_REQUIRED。
    issued = login(factory, settings)
    insert_two_factor(factory, user.id, confirmed=True)
    before = snapshot(factory, tenant)

    explode_on_audit(monkeypatch)
    with pytest.raises(OperationalError):
        reset_2fa(factory, admin, tenant, now=NOW + dt.timedelta(hours=1))
    monkeypatch.undo()

    assert snapshot(factory, tenant) == before
    assert two_factor_rows(factory) == (1, 2)
    assert current_user(factory, settings, issued.access_token).id == user.id
    reset_2fa(factory, admin, tenant)
    assert two_factor_rows(factory) == (0, 0)

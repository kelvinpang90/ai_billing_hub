"""Admin portal account endpoints at the HTTP boundary (design gate #368 v12 §2, §4, §6, §7).

设计 §7 中属于管理端六个接口的行：开通、查看、重发（含改邮箱）、停用、启用、重置 2FA，
重复开通、开通后 outbox 与审计、「邀请字段」。服务层的判定细节与服务层并发归
tests/backend/test_portal_accounts_service.py 与 test_portal_accounts_concurrency.py；
这里验接口层成形的东西：状态码、错误码、七个键、立即触发投递、422 与泄漏。

文件末尾是设计 §7「并发（v12，任务 D 的准入）」：
停用与忘记密码都经接口、两种先后各一次，
**只能对着真 MySQL 跑**（SQLite 下 `FOR UPDATE` 被静默忽略）。没设
`BILLING_TEST_DATABASE_URL` 时 skip —— **不要把 skipped 读成 passed**，CI 设了它。

管理员的访问令牌直接签（ADMIN 过 2FA 的路径在 test_auth_api.py）；邀请令牌从 outbox
payload 里取，与邮件里拿到的是同一个值。
"""

from __future__ import annotations

import ast
import base64
import datetime as dt
import io
import json
import logging
import os
import pathlib
import threading
import time
import uuid
from collections.abc import Callable, Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, event, func, select, update
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.core.config import Settings
from app.core.database import create_session_factory
from app.core.logging import JsonFormatter
from app.core.passwords import hash_password, verify_password
from app.core.tokens import issue_access_token
from app.main import create_app
from app.models.auth import (
    AuditAction,
    AuditLog,
    DomainOutbox,
    OutboxStatus,
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
from app.services import password_reset, portal_accounts
from app.services.auth import utc_now
from app.services.password_reset import EVENT_PASSWORD_RESET
from app.services.portal_accounts import EVENT_PORTAL_INVITATION

CUSTOMERS = "/api/v1/admin/customers"
ACCEPT = "/api/v1/auth/invitation/accept"
FORGOT = "/api/v1/auth/password/forgot"
RESET = "/api/v1/auth/password/reset"
ME = "/api/v1/me"
ROUTER_FILE = pathlib.Path("app/api/admin_portal_accounts.py")

TTL_SECONDS = 259_200
ADMIN_EMAIL = "admin@example.com"
PORTAL_EMAIL = "portal@example.com"
PASSWORD = "a-customer-perfectly-fine-passphrase"
OTHER_PASSWORD = "another-customer-fine-passphrase"
REASON = "Lost phone; identity confirmed by a call-back to the registered number."
# 全零占位值：不存在的客户。
UNKNOWN_CUSTOMER = "00000000-0000-4000-8000-000000000000"

# 设计 §2：成功响应恰好这七个键。多一个（内部 id、tenant_id、哈希、锁定信息）就红。
ACCOUNT_FIELDS = {
    "email",
    "status",
    "invited_at",
    "invitation_expires_at",
    "activated_at",
    "last_login_at",
    "two_factor_enabled",
}


def portal_url(customer_id: str, suffix: str = "") -> str:
    return f"{CUSTOMERS}/{customer_id}/portal-account{suffix}"


# --- 夹具 -----------------------------------------------------------------------


def settings_for(tmp_path) -> Settings:
    key = tmp_path / "jwt.key"
    key.write_text("a-test-signing-key-that-is-long-enough", encoding="utf-8")
    master = tmp_path / "master.key"
    master.write_text(f"1:{base64.b64encode(os.urandom(32)).decode()}\n", encoding="utf-8")
    return Settings(
        jwt_secret_file=str(key),
        master_key_file=str(master),
        database_url="",
        session_cookie_secure=False,
        auth_rate_limit_per_minute=600,
        auth_rate_limit_burst=200,
        customer_invitation_ttl_seconds=TTL_SECONDS,
        frontend_base_url="https://billing.example.com",
    )


class FakeCelery:
    """Records the delivery triggers the API sends, and can be told to fail."""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.sent: list[tuple[str, list[object]]] = []

    def send_task(self, name: str, args: list[object]) -> None:
        if self.fail:
            raise RuntimeError("the broker is gone")
        self.sent.append((name, args))


@pytest.fixture
def app(tmp_path) -> Iterator[FastAPI]:
    application = create_app(settings_for(tmp_path))
    # StaticPool：TestClient 在线程池里跑同步端点，`:memory:` 换个连接就是另一个空库。
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    application.state.engine = engine
    application.state.session_factory = create_session_factory(engine)
    application.state.celery_app = FakeCelery()
    yield application
    engine.dispose()


@pytest.fixture
def client(app) -> TestClient:
    return TestClient(app)


@pytest.fixture
def admin_user(app) -> User:
    return make_user(app, email=ADMIN_EMAIL)


@pytest.fixture
def admin(app, admin_user) -> dict[str, str]:
    return access_headers(app, admin_user.id, "ADMIN")


class Clock:
    """The portal services' clock, moved by hand in whole seconds."""

    def __init__(self) -> None:
        self.moment = utc_now().replace(microsecond=0)

    def now(self) -> dt.datetime:
        return self.moment

    def advance(self, seconds: int) -> dt.datetime:
        self.moment += dt.timedelta(seconds=seconds)
        return self.moment


@pytest.fixture
def clock(monkeypatch) -> Clock:
    """只换门户服务的时钟：访问令牌仍按真实时间签发与校验。"""
    fixed = Clock()
    monkeypatch.setattr(portal_accounts, "utc_now", fixed.now)
    return fixed


# --- 帮手 -----------------------------------------------------------------------


def make_user(
    app,
    *,
    email: str,
    role: UserRole = UserRole.ADMIN,
    status: UserStatus = UserStatus.ACTIVE,
) -> User:
    now = utc_now().replace(microsecond=0)
    with app.state.session_factory() as session:
        user = User(
            email=email,
            password_hash="not-a-real-hash",
            role=role,
            status=status,
            created_at=now,
            updated_at=now,
        )
        session.add(user)
        session.commit()
        session.refresh(user)
        session.expunge(user)
        return user


def access_headers(app, user_id: int, role: str, password_version: int = 0) -> dict[str, str]:
    token = issue_access_token(
        app.state.settings,
        user_id=user_id,
        role=role,
        session_id=uuid.uuid4().hex,
        now=utc_now(),
        password_version=password_version,
    )
    return {"Authorization": f"Bearer {token}"}


def make_tenant(app, status: AccountStatus = AccountStatus.ENABLED) -> Tenant:
    with app.state.session_factory() as session:
        tenant = tenancy.create_tenant(
            session,
            company_name="Portal Test Sdn Bhd",
            email="ops@example.com",
            now=utc_now().replace(microsecond=0),
        )
        tenant.account_status = status
        session.commit()
        session.refresh(tenant)
        session.expunge(tenant)
        return tenant


def set_tenant_status(app, tenant: Tenant, status: AccountStatus) -> None:
    with app.state.session_factory() as session:
        session.execute(update(Tenant).where(Tenant.id == tenant.id).values(account_status=status))
        session.commit()


def open_account(client, admin, tenant: Tenant, email: str = PORTAL_EMAIL) -> dict:
    response = client.post(portal_url(tenant.public_id), json={"email": email}, headers=admin)
    assert response.status_code == 201, response.text
    return response.json()["data"]


def latest_invitation(app) -> tuple[int, str]:
    """(outbox id, raw token) of the newest invitation outbox row."""
    with app.state.session_factory() as session:
        row = session.execute(
            select(DomainOutbox)
            .where(DomainOutbox.event_type == EVENT_PORTAL_INVITATION)
            .order_by(DomainOutbox.id.desc())
            .limit(1)
        ).scalar_one()
        return int(row.id), str(json.loads(row.payload_json or "{}")["token"])


def accept(client, token: str, password: str = PASSWORD):
    return client.post(ACCEPT, json={"token": token, "password": password})


def activate(app, client, admin, tenant: Tenant) -> dict:
    """Open the account and accept the invitation through the API: an `ACTIVE` account."""
    open_account(client, admin, tenant)
    _, token = latest_invitation(app)
    assert accept(client, token).status_code == 200
    return get_account(client, admin, tenant)


def get_account(client, admin, tenant: Tenant) -> dict:
    response = client.get(portal_url(tenant.public_id), headers=admin)
    assert response.status_code == 200, response.text
    return response.json()["data"]


def portal_user(app, tenant: Tenant) -> User:
    with app.state.session_factory() as session:
        user = session.execute(select(User).where(User.tenant_id == tenant.id)).scalar_one()
        session.expunge(user)
        return user


def count(app, model, *conditions) -> int:
    with app.state.session_factory() as session:
        statement = select(func.count()).select_from(model).where(*conditions)
        return int(session.execute(statement).scalar_one())


def table_counts(app) -> tuple[int, int, int, int]:
    """(users, tokens, outbox, audit) row counts."""
    return (
        count(app, User),
        count(app, PasswordResetToken),
        count(app, DomainOutbox),
        count(app, AuditLog),
    )


def audits(app, action: AuditAction) -> list[AuditLog]:
    statement = select(AuditLog).where(AuditLog.action == action).order_by(AuditLog.id)
    with app.state.session_factory() as session:
        rows = list(session.execute(statement).scalars())
        session.expunge_all()
        return rows


def error_of(response) -> tuple[int, str]:
    return response.status_code, response.json()["error"]["code"]


def stamp(moment: dt.datetime | None) -> str | None:
    """How the API writes a naive UTC datetime."""
    return moment.isoformat() if moment is not None else None


# --- 开通 -----------------------------------------------------------------------


def test_opening_an_account_returns_the_seven_keys_and_queues_one_invitation(
    app, client, admin, admin_user, clock
) -> None:
    tenant = make_tenant(app)

    response = client.post(
        portal_url(tenant.public_id), json={"email": " Portal@Example.COM "}, headers=admin
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert (body["success"], body["error"]) == (True, None)
    data = body["data"]
    assert set(data) == ACCOUNT_FIELDS
    assert data == {
        "email": PORTAL_EMAIL,
        "status": "INVITED",
        "invited_at": stamp(clock.now()),
        "invitation_expires_at": stamp(clock.now() + dt.timedelta(seconds=TTL_SECONDS)),
        "activated_at": None,
        "last_login_at": None,
        "two_factor_enabled": False,
    }

    user = portal_user(app, tenant)
    assert (user.role, user.status, user.tenant_id) == (
        UserRole.CUSTOMER,
        UserStatus.INVITED,
        tenant.id,
    )
    # 开通后 outbox 恰好一行，事件类型是邀请，payload 带到期时刻。
    with app.state.session_factory() as session:
        [row] = session.execute(select(DomainOutbox)).scalars()
        assert row.event_type == EVENT_PORTAL_INVITATION
        payload = json.loads(row.payload_json or "{}")
    assert payload["to"] == PORTAL_EMAIL
    assert payload["expires_at"] == data["invitation_expires_at"]
    # 立即触发了投递，用的正是这一行的 id（与忘记密码同一做法）。
    assert app.state.celery_app.sent == [("app.tasks.outbox.deliver", [row.id])]

    [audit] = audits(app, AuditAction.PORTAL_ACCOUNT_CREATED)
    assert (audit.actor_user_id, audit.actor_role) == (admin_user.id, "ADMIN")
    assert (audit.entity_type, audit.entity_id) == ("users", str(user.id))
    assert json.loads(audit.after_state or "{}") == {
        "tenant_public_id": tenant.public_id,
        "status": "INVITED",
    }
    # 审计不放邮箱值（设计 §6，REQ-PRIV-001）。
    for field in (audit.before_state, audit.after_state, audit.reason):
        assert PORTAL_EMAIL not in (field or "").lower()


def test_a_broker_failure_does_not_change_the_response(app, client, admin) -> None:
    """触发失败当没事发生：行已落库，周期恢复会补投（INV-14）。"""
    app.state.celery_app = FakeCelery(fail=True)
    tenant = make_tenant(app)

    created = client.post(portal_url(tenant.public_id), json={"email": PORTAL_EMAIL}, headers=admin)
    resent = client.post(portal_url(tenant.public_id, "/resend-invitation"), headers=admin)

    assert (created.status_code, resent.status_code) == (201, 200)
    assert count(app, DomainOutbox, DomainOutbox.status == OutboxStatus.PENDING) == 2


def test_without_a_broker_the_row_waits_for_the_sweep(app, client, admin) -> None:
    app.state.celery_app = None
    tenant = make_tenant(app)

    open_account(client, admin, tenant)

    assert count(app, DomainOutbox, DomainOutbox.event_type == EVENT_PORTAL_INVITATION) == 1


def test_opening_twice_is_409_and_does_not_reveal_the_email(app, client, admin) -> None:
    tenant = make_tenant(app)
    open_account(client, admin, tenant)
    before = table_counts(app)

    again = client.post(
        portal_url(tenant.public_id), json={"email": "someone-else@example.com"}, headers=admin
    )

    assert error_of(again) == (409, "PORTAL_ACCOUNT_EXISTS")
    assert again.json()["data"] is None
    assert PORTAL_EMAIL not in again.text
    assert table_counts(app) == before


@pytest.mark.parametrize("variant", ["ADMIN@example.com", "  admin@EXAMPLE.com  "])
def test_an_address_used_by_any_user_is_409_email_in_use(app, client, admin, variant: str) -> None:
    """大小写、首尾空白不同但实为同一地址：归一后判同一个；与 ADMIN 的邮箱相同也算。"""
    tenant = make_tenant(app)
    before = table_counts(app)

    response = client.post(portal_url(tenant.public_id), json={"email": variant}, headers=admin)

    assert error_of(response) == (409, "EMAIL_IN_USE")
    assert table_counts(app) == before


def test_opening_for_a_closed_customer_is_409_account_closed(app, client, admin) -> None:
    tenant = make_tenant(app, AccountStatus.CLOSED)
    before = table_counts(app)

    response = client.post(
        portal_url(tenant.public_id), json={"email": PORTAL_EMAIL}, headers=admin
    )

    assert error_of(response) == (409, "ACCOUNT_CLOSED")
    assert table_counts(app) == before


@pytest.mark.parametrize(
    "body",
    [
        {"email": "not-an-address"},
        {},
        {"email": ("x" * 320) + "@example.com"},
        {"email": PORTAL_EMAIL, "tenant_id": 1},
        {"email": PORTAL_EMAIL, "status": "ACTIVE"},
    ],
    ids=["format", "missing", "too-long", "extra-tenant", "extra-status"],
)
def test_a_malformed_open_request_is_422(app, client, admin, body: dict) -> None:
    tenant = make_tenant(app)
    before = table_counts(app)

    response = client.post(portal_url(tenant.public_id), json=body, headers=admin)

    assert error_of(response) == (422, "VALIDATION_ERROR")
    assert table_counts(app) == before


# --- 客户不存在、未开通 -----------------------------------------------------------


ROUTE_CALLS: dict[str, tuple[str, str, dict | None]] = {
    "get": ("GET", "", None),
    "resend": ("POST", "/resend-invitation", {}),
    "disable": ("POST", "/disable", None),
    "enable": ("POST", "/enable", None),
    "reset-2fa": ("POST", "/reset-2fa", {"reason": REASON}),
}


@pytest.mark.parametrize("call", [*sorted(ROUTE_CALLS), "open"])
def test_an_unknown_customer_is_404_customer_not_found(app, client, admin, call: str) -> None:
    method, suffix, body = (
        ("POST", "", {"email": PORTAL_EMAIL}) if call == "open" else ROUTE_CALLS[call]
    )
    before = table_counts(app)

    response = client.request(
        method, portal_url(UNKNOWN_CUSTOMER, suffix), json=body, headers=admin
    )

    assert error_of(response) == (404, "CUSTOMER_NOT_FOUND")
    assert table_counts(app) == before


@pytest.mark.parametrize("call", sorted(ROUTE_CALLS))
def test_a_customer_without_an_account_is_404_portal_account_not_found(
    app, client, admin, call: str
) -> None:
    method, suffix, body = ROUTE_CALLS[call]
    tenant = make_tenant(app)
    before = table_counts(app)

    response = client.request(
        method, portal_url(tenant.public_id, suffix), json=body, headers=admin
    )

    assert error_of(response) == (404, "PORTAL_ACCOUNT_NOT_FOUND")
    assert table_counts(app) == before


# --- 查看与「邀请字段」 -------------------------------------------------------------


def test_the_invitation_fields_follow_the_latest_invitation(app, client, admin, clock) -> None:
    """设计 §7「邀请字段」。

    依次是：刚开通、重发之后、接受之后、停用后再启用（回到 `INVITED`）、令牌过期之后。

    `invited_at` 依次为首个与最新邀请的时刻、之后保持最新邀请的时刻；
    `invitation_expires_at` 依次为令牌到期时刻、新令牌到期时刻、null、null、null。
    """
    ttl = dt.timedelta(seconds=TTL_SECONDS)

    # 刚开通。
    accepted = make_tenant(app)
    first = clock.now()
    open_account(client, admin, accepted)
    view = get_account(client, admin, accepted)
    assert (view["invited_at"], view["invitation_expires_at"]) == (
        stamp(first),
        stamp(first + ttl),
    )

    # 重发之后：最新那一封。
    latest = clock.advance(60)
    resent = client.post(portal_url(accepted.public_id, "/resend-invitation"), headers=admin)
    assert resent.status_code == 200
    view = get_account(client, admin, accepted)
    assert (view["invited_at"], view["invitation_expires_at"]) == (
        stamp(latest),
        stamp(latest + ttl),
    )
    assert resent.json()["data"] == view

    # 接受之后。
    clock.advance(60)
    _, token = latest_invitation(app)
    assert accept(client, token).status_code == 200
    view = get_account(client, admin, accepted)
    assert view["status"] == "ACTIVE"
    assert view["activated_at"] == stamp(clock.now())
    assert (view["invited_at"], view["invitation_expires_at"]) == (stamp(latest), None)

    # 停用后再启用，回到 `INVITED`：旧邀请已在停用时作废，需要重发。
    reenabled = make_tenant(app)
    opened_at = clock.advance(60)
    open_account(client, admin, reenabled, email="second@example.com")
    clock.advance(60)
    assert (
        client.post(portal_url(reenabled.public_id, "/disable"), headers=admin).status_code == 200
    )
    enabled = client.post(portal_url(reenabled.public_id, "/enable"), headers=admin)
    assert enabled.status_code == 200
    view = enabled.json()["data"]
    assert view["status"] == "INVITED"
    assert (view["invited_at"], view["invitation_expires_at"]) == (stamp(opened_at), None)

    # 令牌过期：到期前一秒仍在，到期那一刻起为 null（`expires_at > now` 才有效）。
    expired = make_tenant(app)
    issued_at = clock.advance(60)
    open_account(client, admin, expired, email="third@example.com")
    clock.advance(TTL_SECONDS - 1)
    assert get_account(client, admin, expired)["invitation_expires_at"] == stamp(issued_at + ttl)
    clock.advance(1)
    view = get_account(client, admin, expired)
    assert (view["invited_at"], view["invitation_expires_at"]) == (stamp(issued_at), None)


def test_reading_writes_nothing(app, client, admin) -> None:
    tenant = make_tenant(app)
    open_account(client, admin, tenant)
    before = table_counts(app)

    assert set(get_account(client, admin, tenant)) == ACCOUNT_FIELDS
    assert table_counts(app) == before


# --- 重发 -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [None, {}, {"email": None}, {"email": "  PORTAL@Example.com "}],
    ids=["no-body", "empty", "null", "same-address"],
)
def test_resending_without_a_new_address_keeps_the_email(
    app, client, admin, body: dict | None
) -> None:
    """不给邮箱，或归一后与当前邮箱相同：不算改邮箱，审计 `email_changed: false`。"""
    tenant = make_tenant(app)
    open_account(client, admin, tenant)
    old_outbox, old_token = latest_invitation(app)

    response = client.post(
        portal_url(tenant.public_id, "/resend-invitation"), json=body, headers=admin
    )

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert set(data) == ACCOUNT_FIELDS
    assert (data["email"], data["status"]) == (PORTAL_EMAIL, "INVITED")
    new_outbox, new_token = latest_invitation(app)
    assert new_outbox != old_outbox
    assert app.state.celery_app.sent[-1] == ("app.tasks.outbox.deliver", [new_outbox])
    [audit] = audits(app, AuditAction.PORTAL_INVITATION_RESENT)
    assert json.loads(audit.after_state or "{}") == {
        "tenant_public_id": tenant.public_id,
        "status": "INVITED",
        "email_changed": False,
    }
    # 旧链接立即失效，新链接可用。
    assert error_of(accept(client, old_token)) == (400, "INVALID_INVITATION_TOKEN")
    assert accept(client, new_token).status_code == 200


def test_resending_to_a_new_address_changes_the_email(app, client, admin) -> None:
    tenant = make_tenant(app)
    open_account(client, admin, tenant)

    response = client.post(
        portal_url(tenant.public_id, "/resend-invitation"),
        json={"email": "Corrected@Example.com"},
        headers=admin,
    )

    assert response.status_code == 200, response.text
    assert response.json()["data"]["email"] == "corrected@example.com"
    assert portal_user(app, tenant).email == "corrected@example.com"
    with app.state.session_factory() as session:
        row = session.get(DomainOutbox, latest_invitation(app)[0])
        assert row is not None
        assert json.loads(row.payload_json or "{}")["to"] == "corrected@example.com"
    [audit] = audits(app, AuditAction.PORTAL_INVITATION_RESENT)
    after = json.loads(audit.after_state or "{}")
    assert after["email_changed"] is True
    for address in (PORTAL_EMAIL, "corrected@example.com"):
        assert address not in (audit.after_state or "").lower()


def test_resending_to_an_address_in_use_is_409(app, client, admin) -> None:
    tenant = make_tenant(app)
    open_account(client, admin, tenant)
    before = table_counts(app)

    response = client.post(
        portal_url(tenant.public_id, "/resend-invitation"),
        json={"email": ADMIN_EMAIL.upper()},
        headers=admin,
    )

    assert error_of(response) == (409, "EMAIL_IN_USE")
    assert table_counts(app) == before
    assert portal_user(app, tenant).email == PORTAL_EMAIL


def test_resending_to_an_active_account_is_409_not_invited(app, client, admin) -> None:
    tenant = make_tenant(app)
    activate(app, client, admin, tenant)
    before = table_counts(app)

    response = client.post(portal_url(tenant.public_id, "/resend-invitation"), headers=admin)

    assert error_of(response) == (409, "PORTAL_ACCOUNT_NOT_INVITED")
    assert table_counts(app) == before


def test_resending_for_a_closed_customer_is_409_account_closed(app, client, admin) -> None:
    tenant = make_tenant(app)
    open_account(client, admin, tenant)
    set_tenant_status(app, tenant, AccountStatus.CLOSED)
    before = table_counts(app)

    response = client.post(portal_url(tenant.public_id, "/resend-invitation"), headers=admin)

    assert error_of(response) == (409, "ACCOUNT_CLOSED")
    assert table_counts(app) == before


def test_a_malformed_resend_request_is_422(app, client, admin) -> None:
    tenant = make_tenant(app)
    open_account(client, admin, tenant)
    before = table_counts(app)

    for body in ({"email": "not-an-address"}, {"status": "ACTIVE"}):
        response = client.post(
            portal_url(tenant.public_id, "/resend-invitation"), json=body, headers=admin
        )
        assert error_of(response) == (422, "VALIDATION_ERROR")
    assert table_counts(app) == before


# --- 停用与启用 -------------------------------------------------------------------


def test_disabling_then_enabling_an_active_account(app, client, admin) -> None:
    """停用立即生效：访问令牌 401；启用回到 `ACTIVE`，停用前的令牌不会复活。"""
    tenant = make_tenant(app)
    activate(app, client, admin, tenant)
    user = portal_user(app, tenant)
    customer = access_headers(app, user.id, "CUSTOMER", user.password_version)
    assert client.get(ME, headers=customer).status_code == 200

    disabled = client.post(portal_url(tenant.public_id, "/disable"), headers=admin)

    assert disabled.status_code == 200, disabled.text
    assert set(disabled.json()["data"]) == ACCOUNT_FIELDS
    assert disabled.json()["data"]["status"] == "DISABLED"
    assert portal_user(app, tenant).password_version == user.password_version + 1
    assert error_of(client.get(ME, headers=customer)) == (401, "TOKEN_INVALID")
    again = client.post(portal_url(tenant.public_id, "/disable"), headers=admin)
    assert error_of(again) == (409, "PORTAL_ACCOUNT_ALREADY_DISABLED")

    enabled = client.post(portal_url(tenant.public_id, "/enable"), headers=admin)

    assert enabled.status_code == 200, enabled.text
    assert enabled.json()["data"]["status"] == "ACTIVE"
    assert error_of(client.get(ME, headers=customer)) == (401, "TOKEN_INVALID")
    assert [
        len(audits(app, action))
        for action in (
            AuditAction.PORTAL_ACCOUNT_DISABLED,
            AuditAction.PORTAL_ACCOUNT_ENABLED,
        )
    ] == [1, 1]


def test_a_never_accepted_account_returns_to_invited_and_its_link_stays_dead(
    app, client, admin
) -> None:
    tenant = make_tenant(app)
    open_account(client, admin, tenant)
    _, token = latest_invitation(app)

    assert client.post(portal_url(tenant.public_id, "/disable"), headers=admin).status_code == 200
    enabled = client.post(portal_url(tenant.public_id, "/enable"), headers=admin)

    assert enabled.json()["data"]["status"] == "INVITED"
    # 不自动重发：outbox 仍只有开通那一行；旧链接在停用时已作废。
    assert count(app, DomainOutbox) == 1
    assert error_of(accept(client, token)) == (400, "INVALID_INVITATION_TOKEN")


def test_enabling_an_account_that_is_not_disabled_is_409(app, client, admin) -> None:
    tenant = make_tenant(app)
    open_account(client, admin, tenant)
    before = table_counts(app)

    response = client.post(portal_url(tenant.public_id, "/enable"), headers=admin)

    assert error_of(response) == (409, "PORTAL_ACCOUNT_NOT_DISABLED")
    assert table_counts(app) == before


def test_enabling_for_a_closed_customer_is_409_but_disabling_is_allowed(app, client, admin) -> None:
    tenant = make_tenant(app)
    open_account(client, admin, tenant)
    set_tenant_status(app, tenant, AccountStatus.CLOSED)

    # 停用的前置条件是「—」：关户之后照样能停用（设计 §4 状态表）。
    assert client.post(portal_url(tenant.public_id, "/disable"), headers=admin).status_code == 200
    before = table_counts(app)
    response = client.post(portal_url(tenant.public_id, "/enable"), headers=admin)

    assert error_of(response) == (409, "ACCOUNT_CLOSED")
    assert table_counts(app) == before
    assert portal_user(app, tenant).status is UserStatus.DISABLED


# --- 重置 2FA ---------------------------------------------------------------------


def enrol_two_factor(app, tenant: Tenant, *, confirmed: bool) -> None:
    """A 2FA row (confirmed or pending) and one recovery code, written directly."""
    now = utc_now().replace(microsecond=0)
    user = portal_user(app, tenant)
    with app.state.session_factory() as session:
        session.add(
            TwoFactorSetting(
                user_id=user.id,
                encrypted_totp_secret="not-a-real-ciphertext",
                key_version=1,
                secret_version=1,
                confirmed_at=now if confirmed else None,
                created_at=now,
                updated_at=now,
            )
        )
        session.add(RecoveryCode(user_id=user.id, code_hash="not-a-real-hash", created_at=now))
        session.commit()


def reset_2fa(client, admin, tenant: Tenant, reason: object = REASON):
    return client.post(
        portal_url(tenant.public_id, "/reset-2fa"), json={"reason": reason}, headers=admin
    )


def test_resetting_two_factor_clears_it_and_ends_the_sessions(app, client, admin) -> None:
    tenant = make_tenant(app)
    activate(app, client, admin, tenant)
    enrol_two_factor(app, tenant, confirmed=True)
    assert get_account(client, admin, tenant)["two_factor_enabled"] is True
    user = portal_user(app, tenant)
    customer = access_headers(app, user.id, "CUSTOMER", user.password_version)
    assert client.get(ME, headers=customer).status_code == 200

    response = reset_2fa(client, admin, tenant)

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert set(data) == ACCOUNT_FIELDS
    assert (data["status"], data["two_factor_enabled"]) == ("ACTIVE", False)
    assert (count(app, TwoFactorSetting), count(app, RecoveryCode)) == (0, 0)
    assert portal_user(app, tenant).password_version == user.password_version + 1
    assert error_of(client.get(ME, headers=customer)) == (401, "TOKEN_INVALID")
    [audit] = audits(app, AuditAction.PORTAL_2FA_RESET)
    assert audit.reason == REASON
    assert json.loads(audit.after_state or "{}") == {"tenant_public_id": tenant.public_id}
    assert PORTAL_EMAIL not in json.dumps([audit.before_state, audit.after_state, audit.reason])
    # 客户随后用密码登录不再要求 TOTP。
    login = client.post("/api/v1/auth/login", json={"email": PORTAL_EMAIL, "password": PASSWORD})
    assert login.status_code == 200, login.text
    assert login.json()["data"]["access_token"]


def test_a_pending_enrolment_is_reset_too(app, client, admin) -> None:
    tenant = make_tenant(app)
    activate(app, client, admin, tenant)
    enrol_two_factor(app, tenant, confirmed=False)

    response = reset_2fa(client, admin, tenant)

    assert response.status_code == 200, response.text
    assert (count(app, TwoFactorSetting), count(app, RecoveryCode)) == (0, 0)


def test_resetting_two_factor_that_was_never_set_up_is_409(app, client, admin) -> None:
    tenant = make_tenant(app)
    activate(app, client, admin, tenant)
    before = table_counts(app)
    version = portal_user(app, tenant).password_version

    response = reset_2fa(client, admin, tenant)

    assert error_of(response) == (409, "PORTAL_2FA_NOT_ENABLED")
    assert table_counts(app) == before
    assert portal_user(app, tenant).password_version == version


def test_a_255_character_reason_is_kept_verbatim(app, client, admin) -> None:
    tenant = make_tenant(app)
    activate(app, client, admin, tenant)
    enrol_two_factor(app, tenant, confirmed=True)
    reason = ("0123456789" * 26)[:255]

    response = reset_2fa(client, admin, tenant, reason)

    assert response.status_code == 200, response.text
    [audit] = audits(app, AuditAction.PORTAL_2FA_RESET)
    assert audit.reason == reason


@pytest.mark.parametrize(
    "reason",
    [("0123456789" * 26)[:256], "", "   ", None],
    ids=["256-characters", "empty", "blank", "null"],
)
def test_a_bad_reason_is_422_and_writes_nothing(app, client, admin, reason) -> None:
    tenant = make_tenant(app)
    activate(app, client, admin, tenant)
    enrol_two_factor(app, tenant, confirmed=True)
    before = (*table_counts(app), count(app, TwoFactorSetting), count(app, RecoveryCode))
    version = portal_user(app, tenant).password_version

    response = reset_2fa(client, admin, tenant, reason)

    assert error_of(response) == (422, "VALIDATION_ERROR")
    after = (*table_counts(app), count(app, TwoFactorSetting), count(app, RecoveryCode))
    assert after == before
    assert portal_user(app, tenant).password_version == version


def test_the_reset_request_has_no_room_for_anything_but_the_reason(app, client, admin) -> None:
    tenant = make_tenant(app)
    activate(app, client, admin, tenant)
    enrol_two_factor(app, tenant, confirmed=True)

    response = client.post(
        portal_url(tenant.public_id, "/reset-2fa"),
        json={"reason": REASON, "user_id": 1},
        headers=admin,
    )

    assert error_of(response) == (422, "VALIDATION_ERROR")
    assert count(app, TwoFactorSetting) == 1


# --- 鉴权与泄漏 -------------------------------------------------------------------


def _handlers() -> list[ast.FunctionDef]:
    tree = ast.parse(ROUTER_FILE.read_text(encoding="utf-8"))
    return [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and any(
            isinstance(decorator, ast.Call)
            and isinstance(decorator.func, ast.Attribute)
            and isinstance(decorator.func.value, ast.Name)
            and decorator.func.value.id == "router"
            for decorator in node.decorator_list
        )
    ]


def _calls_require_admin(statement: ast.stmt) -> bool:
    value = statement.value if isinstance(statement, ast.Assign | ast.Expr) else None
    return (
        isinstance(value, ast.Call)
        and isinstance(value.func, ast.Name)
        and value.func.id == "require_admin"
        and [ast.unparse(arg) for arg in value.args] == ["request"]
    )


def test_every_handler_starts_with_require_admin() -> None:
    """第一条语句（文档字符串之后）就是 `require_admin(request)`。"""
    handlers = _handlers()
    # 六个接口；少了说明 AST 扫描落空了。
    assert len(handlers) == 6

    for handler in handlers:
        body = handler.body
        first = body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            body = body[1:]
        assert body, handler.name
        assert _calls_require_admin(body[0]), handler.name


def test_the_email_and_token_never_reach_logs_or_audit(app, client, admin) -> None:
    """邮箱是个人数据（REQ-PRIV-001），令牌原文只进 outbox payload（设计 §6）。"""
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        tenant = make_tenant(app)
        open_account(client, admin, tenant)
        client.post(
            portal_url(tenant.public_id, "/resend-invitation"),
            json={"email": "corrected@example.com"},
            headers=admin,
        )
        _, token = latest_invitation(app)
        accept(client, token)
        enrol_two_factor(app, tenant, confirmed=True)
        reset_2fa(client, admin, tenant)
        client.post(portal_url(tenant.public_id, "/disable"), headers=admin)
        client.post(portal_url(tenant.public_id, "/enable"), headers=admin)
    finally:
        root.removeHandler(handler)

    logged = stream.getvalue()
    with app.state.session_factory() as session:
        audit_text = json.dumps(
            [
                [row.before_state, row.after_state, row.reason]
                for row in session.execute(select(AuditLog)).scalars()
            ]
        )
    for haystack in (logged, audit_text):
        assert token not in haystack
        assert PASSWORD not in haystack
        for address in (PORTAL_EMAIL, "corrected@example.com"):
            assert address not in haystack.lower()
    assert [json.loads(line) for line in logged.splitlines() if line.strip()]


# --- 并发（v12，任务 D 的准入）：停用与忘记密码，只在 MySQL 上跑 ---------
#
# 设计 §7「并发（v12，任务 D 的准入）」与 §8「部署顺序」：
# 停用走 POST …/portal-account/disable，忘记密码走 POST /api/v1/auth/password/forgot，
# 两种先后各跑一次。两次都断言停用提交后该用户没有 `used_at` 为空的
# `PASSWORD_RESET` 令牌；忘记密码先提交那一次，重新启用后用那张令牌调重置
# 得到 400 `INVALID_RESET_TOKEN`、密码不变。
#
# `request_reset` 未锁用户行时，「停用先提交」那一次必然变红：
# 不加锁读到 `ACTIVE` 后插入令牌，插入时外键检查对用户行加共享锁，
# 于是等到停用提交后照样落库，躲过了作废。

TEST_DATABASE_URL = os.environ.get("BILLING_TEST_DATABASE_URL", "")
# 持锁的一方多等这么久再继续，给另一方时间撞上行锁
# （同 test_portal_accounts_concurrency.py）。
HOLD_SECONDS = 0.5
WAIT_TIMEOUT = 15
JOIN_TIMEOUT = 30


def _mysql_engine() -> Iterator[Engine]:
    if not TEST_DATABASE_URL:
        pytest.skip("BILLING_TEST_DATABASE_URL is not set; row locking needs a real MySQL")
    base_url = make_url(TEST_DATABASE_URL)
    # 独立的库：不和迁移用例、服务层并发用例共用
    # （`create_all` / `drop_all` 会删掉它们的表）。
    database = f"{base_url.database}_portal_accounts_api"
    root = create_engine(base_url.set(database=""), isolation_level="AUTOCOMMIT")
    with root.connect() as conn:
        conn.exec_driver_sql(f"CREATE DATABASE IF NOT EXISTS `{database}`")
    root.dispose()

    engine = create_engine(base_url.set(database=database), pool_size=6, max_overflow=4)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    try:
        yield engine
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


@pytest.fixture
def mysql_app(tmp_path) -> Iterator[FastAPI]:
    for engine in _mysql_engine():
        application = create_app(settings_for(tmp_path))
        application.state.engine = engine
        application.state.session_factory = create_session_factory(engine)
        application.state.celery_app = None
        yield application


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


def active_customer(app) -> tuple[Tenant, dict[str, str]]:
    """An `ACTIVE` portal account, opened and accepted through the API; returns admin headers."""
    with app.state.session_factory() as session:
        admin_user = User(
            email=ADMIN_EMAIL,
            password_hash=hash_password("an-admin-perfectly-fine-passphrase"),
            role=UserRole.ADMIN,
            status=UserStatus.ACTIVE,
            created_at=utc_now().replace(microsecond=0),
            updated_at=utc_now().replace(microsecond=0),
        )
        session.add(admin_user)
        session.commit()
        admin_id = int(admin_user.id)
    headers = access_headers(app, admin_id, "ADMIN")
    tenant = make_tenant(app)
    client = TestClient(app)
    open_account(client, headers, tenant)
    _, token = latest_invitation(app)
    assert accept(client, token).status_code == 200
    assert portal_user(app, tenant).status is UserStatus.ACTIVE
    return tenant, headers


def reset_tokens(app, *conditions) -> int:
    return count(
        app,
        PasswordResetToken,
        PasswordResetToken.purpose == TokenPurpose.PASSWORD_RESET,
        *conditions,
    )


def reset_outbox_token(app) -> str:
    with app.state.session_factory() as session:
        row = session.execute(
            select(DomainOutbox).where(DomainOutbox.event_type == EVENT_PASSWORD_RESET)
        ).scalar_one()
        return str(json.loads(row.payload_json or "{}")["token"])


def _locks_the_user_by_email(state) -> bool:
    """`request_reset` 锁用户行的那条查询：`populate_existing`，WHERE 里带邮箱。

    停用锁用户行的查询 WHERE 只有 `tenant_id`，
    鉴权读用户不带 `populate_existing`，都不会命中。
    """
    mappers = {mapper.class_ for mapper in state.all_mappers}
    where = getattr(state.statement, "whereclause", None)
    return (
        User in mappers
        and bool(state.execution_options.get("populate_existing"))
        and where is not None
        and "users.email" in str(where)
    )


def test_a_disable_that_commits_first_leaves_no_reset_token(mysql_app, monkeypatch) -> None:
    """停用先提交：停用锁住行、尚未提交时，忘记密码先完成按邮箱的不加锁读取。

    忘记密码读到 `ACTIVE` 后再去等用户行锁；锁到时读到 `DISABLED`：
    不写令牌、outbox、审计，对外仍是同一个 200。
    """
    app = mysql_app
    tenant, admin = active_customer(app)
    factory = app.state.session_factory
    disable_locked = threading.Event()
    forgot_read = threading.Event()
    events: list[str] = []
    real_portal_user = portal_accounts._portal_user

    def staged(session: Session, tenant_id: int, *, for_update: bool) -> User | None:
        user = real_portal_user(session, tenant_id, for_update=for_update)
        if for_update:
            disable_locked.set()  # 停用方已持租户、用户两把锁，尚未改任何东西
            assert forgot_read.wait(WAIT_TIMEOUT), "the reset request never read the user"
            time.sleep(HOLD_SECONDS)  # 让忘记密码撞上用户行锁
            events.append("disable proceeding")
        return user

    def after_plain_read(state) -> None:
        if not forgot_read.is_set() and _locks_the_user_by_email(state):
            events.append("reset request read the user")
            forgot_read.set()

    monkeypatch.setattr(portal_accounts, "_portal_user", staged)
    event.listen(factory, "do_orm_execute", after_plain_read)

    def go_disable() -> None:
        response = TestClient(app).post(portal_url(tenant.public_id, "/disable"), headers=admin)
        assert response.status_code == 200, response.text
        events.append("disable committed")

    def go_forgot() -> None:
        assert disable_locked.wait(WAIT_TIMEOUT), "the disable never took the lock"
        response = TestClient(app).post(FORGOT, json={"email": PORTAL_EMAIL})
        assert response.status_code == 200, response.text
        assert response.json()["data"] == {}

    try:
        outcomes = run_pair(disable=go_disable, forgot=go_forgot)
    finally:
        event.remove(factory, "do_orm_execute", after_plain_read)

    assert outcomes == {"disable": None, "forgot": None}
    assert events[:3] == [
        "reset request read the user",
        "disable proceeding",
        "disable committed",
    ]
    assert portal_user(app, tenant).status is UserStatus.DISABLED
    # 停用提交后没有可用的重置令牌；这一次连一行都没写。
    assert reset_tokens(app, PasswordResetToken.used_at.is_(None)) == 0
    assert reset_tokens(app) == 0
    assert count(app, DomainOutbox, DomainOutbox.event_type == EVENT_PASSWORD_RESET) == 0
    assert len(audits(app, AuditAction.PASSWORD_RESET_REQUESTED)) == 0

    # 重新启用也不会冒出一张可用的重置令牌。
    client = TestClient(app)
    assert client.post(portal_url(tenant.public_id, "/enable"), headers=admin).status_code == 200
    assert portal_user(app, tenant).status is UserStatus.ACTIVE
    assert reset_tokens(app, PasswordResetToken.used_at.is_(None)) == 0


def test_a_reset_request_that_commits_first_is_voided_by_the_disable(
    mysql_app, monkeypatch
) -> None:
    """忘记密码先锁到用户行：停用排在它后面，等它提交后才继续，并把它刚写的令牌一起作废。

    重新启用之后，用那封信里的令牌调重置得到 400 `INVALID_RESET_TOKEN`，密码不变。
    """
    app = mysql_app
    tenant, admin = active_customer(app)
    forgot_locked = threading.Event()
    disabled = threading.Event()
    events: list[str] = []
    real_generate = password_reset.generate_opaque_token

    def staged_generate() -> str:
        # 忘记密码已持用户行锁、复查通过，令牌还没写。停用这时出发，必须撞上这把锁。
        forgot_locked.set()
        time.sleep(HOLD_SECONDS)
        events.append("disable got through" if disabled.is_set() else "disable still waiting")
        return real_generate()

    monkeypatch.setattr(password_reset, "generate_opaque_token", staged_generate)

    def go_forgot() -> None:
        response = TestClient(app).post(FORGOT, json={"email": PORTAL_EMAIL})
        assert response.status_code == 200, response.text

    def go_disable() -> None:
        assert forgot_locked.wait(WAIT_TIMEOUT), "the reset request never took the lock"
        response = TestClient(app).post(portal_url(tenant.public_id, "/disable"), headers=admin)
        assert response.status_code == 200, response.text
        disabled.set()

    outcomes = run_pair(forgot=go_forgot, disable=go_disable)

    assert outcomes == {"forgot": None, "disable": None}
    assert events == ["disable still waiting"]
    assert portal_user(app, tenant).status is UserStatus.DISABLED
    # 令牌落了库（忘记密码先提交），又被停用作废。
    assert reset_tokens(app) == 1
    assert reset_tokens(app, PasswordResetToken.used_at.is_(None)) == 0

    client = TestClient(app)
    assert client.post(portal_url(tenant.public_id, "/enable"), headers=admin).status_code == 200
    assert portal_user(app, tenant).status is UserStatus.ACTIVE
    response = client.post(
        RESET, json={"token": reset_outbox_token(app), "new_password": OTHER_PASSWORD}
    )
    assert error_of(response) == (400, "INVALID_RESET_TOKEN")
    assert verify_password(portal_user(app, tenant).password_hash, PASSWORD)
    assert not verify_password(portal_user(app, tenant).password_hash, OTHER_PASSWORD)

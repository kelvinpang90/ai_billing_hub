"""`POST /api/v1/auth/invitation/accept` at the HTTP boundary (design gate #368 v12 §2, §4, §6).

服务层的判定顺序与并发归 tests/backend/test_portal_accounts_service.py 与
test_portal_accounts_concurrency.py；这里验接口层成形的东西：状态码、错误码、信封、限流、
路径前缀与泄漏。门户账号用服务层开通（管理端开通接口归 AIH-TASK-094），令牌从 outbox
payload 里取，与邮件拿到的是同一个值。
"""

from __future__ import annotations

import base64
import datetime as dt
import io
import json
import logging
import os

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, update
from sqlalchemy.pool import StaticPool

from app.core.config import Settings
from app.core.database import create_session_factory
from app.core.logging import JsonFormatter
from app.core.passwords import hash_password, verify_password
from app.core.ratelimit import TokenBucket
from app.core.tokens import hash_opaque_token
from app.main import create_app
from app.models.auth import (
    AuditAction,
    AuditLog,
    DomainOutbox,
    PasswordResetToken,
    TokenPurpose,
    User,
    UserRole,
    UserStatus,
)
from app.models.base import Base
from app.models.tenancy import AccountStatus, Tenant
from app.repositories import tenancy
from app.services.auth import RequestContext, utc_now
from app.services.portal_accounts import create_portal_account

ACCEPT = "/api/v1/auth/invitation/accept"
TTL_SECONDS = 259_200
CONTEXT = RequestContext(ip_address="10.0.0.1", user_agent="pytest")
ADMIN_EMAIL = "admin@example.com"
ADMIN_PASSWORD = "an-admin-perfectly-fine-passphrase"
PORTAL_EMAIL = "portal@example.com"
PASSWORD = "a-customer-perfectly-fine-passphrase"
WEAK = "tiny-pw"


@pytest.fixture
def app(tmp_path):
    key = tmp_path / "jwt.key"
    key.write_text("a-test-signing-key-that-is-long-enough", encoding="utf-8")
    master = tmp_path / "master.key"
    master.write_text(f"1:{base64.b64encode(os.urandom(32)).decode()}\n", encoding="utf-8")
    application = create_app(
        Settings(
            jwt_secret_file=str(key),
            master_key_file=str(master),
            session_cookie_secure=False,
            auth_rate_limit_per_minute=600,
            auth_rate_limit_burst=200,
            customer_invitation_ttl_seconds=TTL_SECONDS,
        )
    )
    # ⚠️ StaticPool：TestClient 在线程池里跑同步端点，`:memory:` 换个连接就是另一个空库。
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    application.state.engine = engine
    application.state.session_factory = create_session_factory(engine)
    yield application
    engine.dispose()


@pytest.fixture
def client(app) -> TestClient:
    return TestClient(app)


@pytest.fixture
def admin(app) -> User:
    return make_user(app, email=ADMIN_EMAIL, password=ADMIN_PASSWORD)


# --- 帮手 -----------------------------------------------------------------------


def make_user(
    app,
    *,
    email: str,
    password: str,
    role: UserRole = UserRole.ADMIN,
    status: UserStatus = UserStatus.ACTIVE,
    tenant_id: int | None = None,
) -> User:
    now = utc_now().replace(microsecond=0)
    with app.state.session_factory() as session:
        user = User(
            email=email,
            password_hash=hash_password(password),
            role=role,
            status=status,
            tenant_id=tenant_id,
            activated_at=now if role is UserRole.CUSTOMER else None,
            created_at=now,
            updated_at=now,
        )
        session.add(user)
        session.commit()
        session.refresh(user)
        session.expunge(user)
        return user


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


def invite(
    app,
    admin: User,
    tenant: Tenant,
    *,
    email: str = PORTAL_EMAIL,
    now: dt.datetime | None = None,
) -> str:
    """Open the portal account through the service; return the token the email would carry."""
    issued = create_portal_account(
        app.state.session_factory,
        app.state.settings,
        actor=admin,
        customer_id=tenant.public_id,
        email=email,
        context=CONTEXT,
        now=now,
    )
    with app.state.session_factory() as session:
        row = session.get(DomainOutbox, issued.outbox_id)
        assert row is not None
        return str(json.loads(row.payload_json or "{}")["token"])


def insert_token(app, user_id: int, raw: str, purpose: TokenPurpose) -> None:
    """Bypass the services: a live token row of the given purpose."""
    now = utc_now().replace(microsecond=0)
    with app.state.session_factory() as session:
        session.add(
            PasswordResetToken(
                user_id=user_id,
                token_hash=hash_opaque_token(raw),
                expires_at=now + dt.timedelta(hours=1),
                created_at=now,
                purpose=purpose,
            )
        )
        session.commit()


def portal_user(app, tenant: Tenant) -> User:
    with app.state.session_factory() as session:
        user = session.execute(select(User).where(User.tenant_id == tenant.id)).scalar_one()
        session.expunge(user)
        return user


def live_tokens(app, user_id: int) -> int:
    with app.state.session_factory() as session:
        return len(
            session.execute(
                select(PasswordResetToken.id).where(
                    PasswordResetToken.user_id == user_id, PasswordResetToken.used_at.is_(None)
                )
            ).all()
        )


def accept(client: TestClient, token: str, password: str = PASSWORD):
    return client.post(ACCEPT, json={"token": token, "password": password})


def error_of(response) -> tuple[int, str]:
    return response.status_code, response.json()["error"]["code"]


# --- 正常路径 -------------------------------------------------------------------


def test_accepting_activates_the_account_and_the_customer_can_sign_in(app, client, admin) -> None:
    tenant = make_tenant(app)
    token = invite(app, admin, tenant)

    response = accept(client, token)

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["data"] == {}
    assert body["error"] is None
    # 不自动登录：没有刷新 cookie，也没有访问令牌。
    assert "billing_refresh" not in response.headers.get("set-cookie", "")

    user = portal_user(app, tenant)
    assert user.status is UserStatus.ACTIVE
    assert user.tenant_id == tenant.id
    assert user.activated_at is not None
    assert user.password_version == 1
    assert verify_password(user.password_hash, PASSWORD)
    with app.state.session_factory() as session:
        actions = list(session.execute(select(AuditLog.action).order_by(AuditLog.id)).scalars())
    assert actions == [AuditAction.PORTAL_ACCOUNT_CREATED, AuditAction.PORTAL_INVITATION_ACCEPTED]

    login = client.post("/api/v1/auth/login", json={"email": PORTAL_EMAIL, "password": PASSWORD})
    assert login.status_code == 200
    assert login.json()["data"]["access_token"]


# --- 令牌错误：五种情况同一个 400 --------------------------------------------------


def _unknown(app, client, admin, email: str) -> str:
    invite(app, admin, make_tenant(app), email=email)
    return f"never-issued-{email}"


def _used(app, client, admin, email: str) -> str:
    token = invite(app, admin, make_tenant(app), email=email)
    assert accept(client, token).status_code == 200
    return token


def _expired(app, client, admin, email: str) -> str:
    past = utc_now() - dt.timedelta(seconds=TTL_SECONDS + 3_600)
    return invite(app, admin, make_tenant(app), email=email, now=past)


def _wrong_purpose(app, client, admin, email: str) -> str:
    # 状态对（`INVITED`）、用途错：只有绕过服务直接造，删掉用途条件时这一格才会红。
    tenant = make_tenant(app)
    invite(app, admin, tenant, email=email)
    raw = f"a-password-reset-token-for-{email}"
    insert_token(app, portal_user(app, tenant).id, raw, TokenPurpose.PASSWORD_RESET)
    return raw


def _not_invited(app, client, admin, email: str) -> str:
    # 用途对（`INVITATION`）、未用、未过期，但账号已是 `ACTIVE`。
    tenant = make_tenant(app)
    user = make_user(
        app,
        email=email,
        password=PASSWORD,
        role=UserRole.CUSTOMER,
        tenant_id=tenant.id,
    )
    raw = f"an-invitation-token-for-{email}"
    insert_token(app, user.id, raw, TokenPurpose.INVITATION)
    return raw


TOKEN_CASES = {
    "unknown": _unknown,
    "used": _used,
    "expired": _expired,
    "wrong-purpose": _wrong_purpose,
    "account-not-invited": _not_invited,
}


@pytest.mark.parametrize("case", sorted(TOKEN_CASES))
def test_every_token_problem_is_the_same_400(app, client, admin, case: str) -> None:
    """设计 §6「限流与枚举」：不细分。分开报会告诉对方「这张令牌存在过」。"""
    token = TOKEN_CASES[case](app, client, admin, PORTAL_EMAIL)

    response = accept(client, token, "a-different-fine-passphrase-1")

    assert error_of(response) == (400, "INVALID_INVITATION_TOKEN")
    assert response.json()["data"] is None


def test_the_five_token_problems_are_indistinguishable(app, client, admin) -> None:
    """同一个状态码、同一个错误码、同一句文案。"""
    bodies = set()
    for case in sorted(TOKEN_CASES):
        token = TOKEN_CASES[case](app, client, admin, f"{case}@example.com")
        response = accept(client, token, "a-different-fine-passphrase-1")
        error = response.json()["error"]
        bodies.add((response.status_code, error["code"], error["message"]))
    assert len(bodies) == 1
    assert next(iter(bodies))[:2] == (400, "INVALID_INVITATION_TOKEN")


# --- 弱密码与关户 ---------------------------------------------------------------


def test_a_weak_password_is_400_and_does_not_burn_the_token(app, client, admin) -> None:
    tenant = make_tenant(app)
    token = invite(app, admin, tenant)
    user = portal_user(app, tenant)

    weak = accept(client, token, WEAK)

    assert error_of(weak) == (400, "WEAK_PASSWORD")
    assert portal_user(app, tenant).status is UserStatus.INVITED
    assert live_tokens(app, user.id) == 1, "弱密码不烧掉仍然有效的令牌"

    assert accept(client, token).status_code == 200


def test_a_closed_tenant_is_409_account_closed(app, client, admin) -> None:
    tenant = make_tenant(app)
    token = invite(app, admin, tenant)
    with app.state.session_factory() as session:
        session.execute(
            update(Tenant).where(Tenant.id == tenant.id).values(account_status=AccountStatus.CLOSED)
        )
        session.commit()
    user = portal_user(app, tenant)

    response = accept(client, token)

    assert error_of(response) == (409, "ACCOUNT_CLOSED")
    after = portal_user(app, tenant)
    assert after.status is UserStatus.INVITED
    assert after.password_version == user.password_version
    assert live_tokens(app, user.id) == 1


# --- 路径前缀与限流 -------------------------------------------------------------


def test_the_path_is_under_the_auth_prefix() -> None:
    """设计 §6：落在 nginx 的 `/api/v1/auth/` 认证限流区。"""
    served = set(create_app(Settings(database_url="", jwt_secret_file="")).openapi()["paths"])
    assert ACCEPT in served
    assert ACCEPT.startswith("/api/v1/auth/")


def test_the_accept_endpoint_is_rate_limited(app, client) -> None:
    """进程内令牌桶只在处理函数显式调用 `_rate_limit` 时生效；删掉那一行这条就红。"""
    app.state.auth_rate_limiter = TokenBucket(per_minute=1, burst=1)
    body = {"token": "never-issued-this-one", "password": PASSWORD}

    first = client.post(ACCEPT, json=body)
    second = client.post(ACCEPT, json=body)

    assert error_of(first) == (400, "INVALID_INVITATION_TOKEN")
    assert error_of(second) == (429, "TOO_MANY_REQUESTS")


# --- 泄漏 -----------------------------------------------------------------------


def test_the_token_and_password_never_reach_logs_audit_or_responses(app, client, admin) -> None:
    """spec §94：令牌原文与密码不进日志、审计、异常文本或响应。"""
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        tenant = make_tenant(app)
        token = invite(app, admin, tenant)
        weak = accept(client, token, WEAK)
        ok = accept(client, token)
        again = accept(client, token)
    finally:
        root.removeHandler(handler)

    logged = stream.getvalue()
    responses = weak.text + ok.text + again.text
    with app.state.session_factory() as session:
        audit_text = json.dumps(
            [
                [row.before_state, row.after_state, row.reason]
                for row in session.execute(select(AuditLog)).scalars()
            ]
        )
    for haystack in (logged, responses, audit_text):
        assert token not in haystack
        assert PASSWORD not in haystack
        assert WEAK not in haystack
        assert "$argon2" not in haystack
    # 门户审计的 `after_state` 不放邮箱值（设计 §6）。
    assert PORTAL_EMAIL not in audit_text
    # 日志确实产生了 —— 否则上面几条断言是在空字符串上空转。
    assert [json.loads(line) for line in logged.splitlines() if line.strip()]

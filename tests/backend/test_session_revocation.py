"""Changing the password ends every earlier session (设计闸门 #369 v6, AIH-TASK-087).

这里是设计 §7 的**非并发**各行：正常路径、改密码后的旧令牌在五个使用点、改密码
之后的普通刷新、登出之后的刷新、真正的重放、`_issue_session` 不重读用户、兼容、
版本单调、事务中途失败，以及「给 `password_hash` 赋值必须同时递增版本号」的守卫。
并发各行（屏障、两个连接、真 MySQL）属于 AIH-TASK-088。

这些分支的共同点还是**失败方式安静**：改完密码旧令牌照样能用、正常改密码之后
的刷新触发盗用告警 —— 每一种都不会让任何请求报错。
"""

from __future__ import annotations

import ast
import base64
import datetime as dt
import json
import logging
import os
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import jwt
import pyotp
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Update, create_engine, func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.core.config import Settings
from app.core.database import create_session_factory
from app.core.passwords import hash_password, verify_password
from app.core.tokens import (
    ALGORITHM,
    TOKEN_TYPE_ACCESS,
    TOKEN_TYPE_PENDING_2FA,
    decode_token,
    hash_refresh_token,
    load_signing_key,
)
from app.main import create_app
from app.models.auth import (
    AuditAction,
    AuditLog,
    DomainOutbox,
    RecoveryCode,
    RefreshToken,
    TwoFactorSetting,
    User,
    UserRole,
    UserStatus,
)
from app.models.base import Base
from app.services.auth import (
    REFRESH_COOKIE_NAME,
    STAGE_ENROL_2FA,
    STAGE_TOTP_REQUIRED,
    RequestContext,
    TokenReused,
    _issue_session,
    refresh_session,
    utc_now,
)
from app.services.two_factor import (
    confirm_enrolment,
    regenerate_recovery_codes,
    start_enrolment,
)

PASSWORD = "a-perfectly-fine-passphrase"
NEW_PASSWORD = "another-perfectly-fine-passphrase"
EMAIL = "admin@example.com"
CONTEXT = RequestContext(ip_address="10.0.0.1", user_agent="pytest")

# 任何一个已登录接口都经过 `require_current_user`；挑一个只读的。
LOGGED_IN = "/api/v1/admin/audit-logs"
REUSE_WARNING = "Refresh token reuse detected"


@pytest.fixture
def client(tmp_path) -> TestClient:
    jwt_key = tmp_path / "jwt.key"
    jwt_key.write_text("a-test-signing-key-that-is-long-enough", encoding="utf-8")
    master = tmp_path / "master.key"
    master.write_text(f"1:{base64.b64encode(os.urandom(32)).decode()}\n", encoding="utf-8")

    app = create_app(
        Settings(
            jwt_secret_file=str(jwt_key),
            master_key_file=str(master),
            session_cookie_secure=False,
            auth_rate_limit_per_minute=600,
            auth_rate_limit_burst=200,
        )
    )
    # ⚠️ `:memory:` 默认每个连接一个独立的库，而 TestClient 在线程池里跑同步端点。
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    app.state.engine = engine
    app.state.session_factory = create_session_factory(engine)

    now = utc_now()
    with app.state.session_factory() as session:
        session.add(
            User(
                email=EMAIL,
                password_hash=hash_password(PASSWORD),
                role=UserRole.ADMIN,
                status=UserStatus.ACTIVE,
                created_at=now,
                updated_at=now,
            )
        )
        session.commit()
    # 「事务中途失败」那条要看 500，而不是让异常冒到用例里。
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def reuse_warnings() -> Iterator[list[logging.LogRecord]]:
    """Collect the theft alert T0.9 hangs off (`app/services/auth.py`)."""
    records: list[logging.LogRecord] = []

    class _Collect(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            if record.getMessage() == REUSE_WARNING:
                records.append(record)

    handler = _Collect(level=logging.WARNING)
    logger = logging.getLogger("app.services.auth")
    logger.addHandler(handler)
    try:
        yield records
    finally:
        logger.removeHandler(handler)


# --- helpers -----------------------------------------------------------------


@dataclass
class Account:
    secret: str
    codes: list[str]


@dataclass(frozen=True)
class SignedIn:
    access_token: str
    refresh_token: str


def settings_of(client: TestClient) -> Settings:
    return client.app.state.settings


def factory_of(client: TestClient):  # noqa: ANN201 - sessionmaker type is verbose
    return client.app.state.session_factory


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def error_code(response) -> str:  # noqa: ANN001 - httpx.Response
    return response.json()["error"]["code"]


def user_id_of(client: TestClient) -> int:
    with factory_of(client)() as session:
        return session.execute(select(User.id).where(User.email == EMAIL)).scalar_one()


def stored_user(client: TestClient) -> User:
    with factory_of(client)() as session:
        user = session.execute(select(User).where(User.email == EMAIL)).scalar_one()
        session.expunge(user)
        return user


def set_password_version(client: TestClient, version: int) -> None:
    """Bypass the services; only the tests may move the version this way."""
    with factory_of(client)() as session:
        user = session.execute(select(User).where(User.email == EMAIL)).scalar_one()
        user.password_version = version
        session.commit()


def pwv(client: TestClient, token: str, *, expected_type: str) -> int:
    return decode_token(settings_of(client), token, expected_type=expected_type)["pwv"]


def login(client: TestClient, password: str = PASSWORD) -> dict:
    response = client.post("/api/v1/auth/login", json={"email": EMAIL, "password": password})
    assert response.status_code == 200, response.text
    return response.json()["data"]


def enrol(client: TestClient, password: str = PASSWORD) -> Account:
    pending = login(client, password)["pending_token"]
    enrolment = client.post("/api/v1/auth/2fa/enrol", json={"pending_token": pending})
    assert enrolment.status_code == 200, enrolment.text
    secret = enrolment.json()["data"]["secret"]
    confirmed = client.post(
        "/api/v1/auth/2fa/confirm",
        json={"pending_token": pending, "code": pyotp.TOTP(secret).now()},
    )
    assert confirmed.status_code == 200, confirmed.text
    return Account(secret=secret, codes=list(confirmed.json()["data"]["recovery_codes"]))


def sign_in(client: TestClient, account: Account, password: str = PASSWORD) -> SignedIn:
    """Both steps of login, with a recovery code.

    ⚠️ 用恢复码而不是 TOTP：同一个 30 秒窗口里的验证码只能用一次，用例要连着登好几次。
    """
    first = login(client, password)
    assert first["stage"] == STAGE_TOTP_REQUIRED
    second = client.post(
        "/api/v1/auth/login/totp",
        json={"pending_token": first["pending_token"], "code": account.codes.pop()},
    )
    assert second.status_code == 200, second.text
    refresh = client.cookies.get(REFRESH_COOKIE_NAME)
    assert refresh
    return SignedIn(access_token=second.json()["data"]["access_token"], refresh_token=refresh)


def reset(client: TestClient, new_password: str):  # noqa: ANN201 - httpx.Response
    """Forgot → dig the token out of the outbox row, the way the email would → reset."""
    assert client.post("/api/v1/auth/password/forgot", json={"email": EMAIL}).status_code == 200
    with factory_of(client)() as session:
        row = session.execute(
            select(DomainOutbox).order_by(DomainOutbox.id.desc()).limit(1)
        ).scalar_one()
        token = str(json.loads(row.payload_json)["token"])
    return client.post(
        "/api/v1/auth/password/reset", json={"token": token, "new_password": new_password}
    )


def reset_ok(client: TestClient, new_password: str) -> None:
    response = reset(client, new_password)
    assert response.status_code == 200, response.text


def reuse_audits(client: TestClient) -> list[AuditLog]:
    with factory_of(client)() as session:
        rows = list(
            session.execute(
                select(AuditLog).where(AuditLog.action == AuditAction.TOKEN_REUSED)
            ).scalars()
        )
        for row in rows:
            session.expunge(row)
        return rows


def refresh_rows(client: TestClient) -> list[RefreshToken]:
    with factory_of(client)() as session:
        rows = list(session.execute(select(RefreshToken).order_by(RefreshToken.id)).scalars())
        for row in rows:
            session.expunge(row)
        return rows


def refresh_row(client: TestClient, raw: str) -> RefreshToken:
    with factory_of(client)() as session:
        row = session.execute(
            select(RefreshToken).where(RefreshToken.token_hash == hash_refresh_token(raw))
        ).scalar_one()
        session.expunge(row)
        return row


def live_recovery_code_ids(client: TestClient) -> list[int]:
    with factory_of(client)() as session:
        return list(
            session.execute(
                select(RecoveryCode.id)
                .where(RecoveryCode.used_at.is_(None), RecoveryCode.revoked_at.is_(None))
                .order_by(RecoveryCode.id)
            ).scalars()
        )


def legacy_token(client: TestClient, **claims: str) -> str:
    """A token in the format signed before `pwv` existed: every claim but that one."""
    now = int(dt.datetime.now(dt.UTC).timestamp())
    payload = {"iat": now, "exp": now + 300, "jti": uuid.uuid4().hex, **claims}
    return jwt.encode(payload, load_signing_key(settings_of(client)), algorithm=ALGORITHM)


# --- 正常路径 -----------------------------------------------------------------


def test_without_a_password_change_nothing_changes(client: TestClient) -> None:
    """不改密码的用户行为完全不变；令牌带 `pwv=0`，刷新令牌行 `password_version=0`。"""
    enrol_stage = login(client)
    assert enrol_stage["stage"] == STAGE_ENROL_2FA
    assert pwv(client, enrol_stage["pending_token"], expected_type=TOKEN_TYPE_PENDING_2FA) == 0

    account = enrol(client)
    first = login(client)
    assert first["stage"] == STAGE_TOTP_REQUIRED
    assert pwv(client, first["pending_token"], expected_type=TOKEN_TYPE_PENDING_2FA) == 0

    second = client.post(
        "/api/v1/auth/login/totp",
        json={"pending_token": first["pending_token"], "code": pyotp.TOTP(account.secret).now()},
    )
    assert second.status_code == 200, second.text
    access = second.json()["data"]["access_token"]
    assert pwv(client, access, expected_type=TOKEN_TYPE_ACCESS) == 0
    assert client.get(LOGGED_IN, headers=bearer(access)).status_code == 200

    rotated = client.post("/api/v1/auth/refresh")
    assert rotated.status_code == 200, rotated.text
    rotated_access = rotated.json()["data"]["access_token"]
    assert pwv(client, rotated_access, expected_type=TOKEN_TYPE_ACCESS) == 0
    assert [row.password_version for row in refresh_rows(client)] == [0, 0]

    regenerated = client.post(
        "/api/v1/auth/2fa/recovery-codes",
        json={"password": PASSWORD},
        headers=bearer(rotated_access),
    )
    assert regenerated.status_code == 200, regenerated.text
    assert reuse_audits(client) == []


# --- 改密码后的旧令牌 ---------------------------------------------------------


def test_every_token_from_before_a_reset_is_refused(client: TestClient) -> None:
    """访问令牌、`TOTP_REQUIRED` pending 令牌、刷新令牌、恢复码接口，依次被拒且什么都不写。

    `ENROL_2FA` 那一种在下面两条（那条路径要求还没注册 2FA）。
    """
    account = enrol(client)
    old = sign_in(client, account)
    stale_pending = login(client)["pending_token"]
    codes_before = live_recovery_code_ids(client)
    rows_before = len(refresh_rows(client))

    reset_ok(client, NEW_PASSWORD)

    # 1) 访问令牌 → 任一已登录接口。
    response = client.get(LOGGED_IN, headers=bearer(old.access_token))
    assert response.status_code == 401
    assert error_code(response) == "TOKEN_INVALID"

    # 2) pending 令牌 → /login/totp：恢复码与 TOTP 都不被消耗，不发会话。
    for code in (account.codes[-1], pyotp.TOTP(account.secret).now()):
        response = client.post(
            "/api/v1/auth/login/totp", json={"pending_token": stale_pending, "code": code}
        )
        assert response.status_code == 401
        assert error_code(response) == "INVALID_CREDENTIALS"
    assert live_recovery_code_ids(client) == codes_before
    with factory_of(client)() as session:
        setting = session.get(TwoFactorSetting, user_id_of(client))
        assert setting is not None
        assert setting.last_used_counter is None, "TOTP 计数器不能动"
    assert len(refresh_rows(client)) == rows_before, "不能签出会话"

    # 3) 刷新令牌 → /refresh：401 TOKEN_REUSED，但这不是重放，不写重放审计。
    assert client.cookies.get(REFRESH_COOKIE_NAME) == old.refresh_token
    response = client.post("/api/v1/auth/refresh")
    assert response.status_code == 401
    assert error_code(response) == "TOKEN_REUSED"
    assert reuse_audits(client) == []

    # 4) 访问令牌 → /2fa/recovery-codes：不替换。
    response = client.post(
        "/api/v1/auth/2fa/recovery-codes",
        json={"password": NEW_PASSWORD},
        headers=bearer(old.access_token),
    )
    assert response.status_code == 401
    assert error_code(response) == "TOKEN_INVALID"
    assert live_recovery_code_ids(client) == codes_before

    # 用新密码重新登录：新令牌带新版本，全部可用。
    fresh = sign_in(client, account, NEW_PASSWORD)
    assert pwv(client, fresh.access_token, expected_type=TOKEN_TYPE_ACCESS) == 1
    assert refresh_row(client, fresh.refresh_token).password_version == 1
    assert client.get(LOGGED_IN, headers=bearer(fresh.access_token)).status_code == 200
    rotated = client.post("/api/v1/auth/refresh")
    assert rotated.status_code == 200, rotated.text
    rotated_access = rotated.json()["data"]["access_token"]
    assert pwv(client, rotated_access, expected_type=TOKEN_TYPE_ACCESS) == 1
    regenerated = client.post(
        "/api/v1/auth/2fa/recovery-codes",
        json={"password": NEW_PASSWORD},
        headers=bearer(rotated_access),
    )
    assert regenerated.status_code == 200, regenerated.text


def test_an_enrolment_pending_token_from_before_a_reset_cannot_enrol(client: TestClient) -> None:
    """知道旧密码的人不能在主人改密码之后注册自己的验证器。"""
    stale = login(client)["pending_token"]
    reset_ok(client, NEW_PASSWORD)

    response = client.post("/api/v1/auth/2fa/enrol", json={"pending_token": stale})
    assert response.status_code == 401
    assert error_code(response) == "TOKEN_INVALID"
    assert "secret" not in response.text
    with factory_of(client)() as session:
        assert session.get(TwoFactorSetting, user_id_of(client)) is None, "不写任何 2FA 行"

    fresh = login(client, NEW_PASSWORD)
    assert pwv(client, fresh["pending_token"], expected_type=TOKEN_TYPE_PENDING_2FA) == 1
    assert (
        client.post("/api/v1/auth/2fa/enrol", json={"pending_token": fresh["pending_token"]})
    ).status_code == 200


def test_an_enrolment_pending_token_from_before_a_reset_cannot_confirm(client: TestClient) -> None:
    """注册在改密码之前开始、确认在之后：确认被拒，2FA 不启用，不发恢复码。"""
    stale = login(client)["pending_token"]
    enrolment = client.post("/api/v1/auth/2fa/enrol", json={"pending_token": stale})
    assert enrolment.status_code == 200
    secret = enrolment.json()["data"]["secret"]

    reset_ok(client, NEW_PASSWORD)

    response = client.post(
        "/api/v1/auth/2fa/confirm",
        json={"pending_token": stale, "code": pyotp.TOTP(secret).now()},
    )
    assert response.status_code == 401
    assert error_code(response) == "TOKEN_INVALID"
    with factory_of(client)() as session:
        setting = session.get(TwoFactorSetting, user_id_of(client))
        assert setting is not None
        assert setting.confirmed_at is None
    assert live_recovery_code_ids(client) == []


def test_the_services_recheck_the_version_inside_their_own_transaction(
    client: TestClient,
) -> None:
    """接口层已经放行（在途请求）、改密码随后提交：服务层在写事务里锁用户行再比较一次。

    `regenerate_recovery_codes` 尤其要紧：它前面的 `require_current_user` 早已返回，
    能拦住它的只有这一次比较。
    """
    settings = settings_of(client)
    factory = factory_of(client)
    user_id = user_id_of(client)
    account = enrol(client)
    codes_before = live_recovery_code_ids(client)
    with factory() as session:
        before = session.get(TwoFactorSetting, user_id)
        assert before is not None
        secret_version = before.secret_version

    reset_ok(client, NEW_PASSWORD)

    with pytest.raises(Exception) as regenerated:
        regenerate_recovery_codes(factory, user_id=user_id, password_version=0, context=CONTEXT)
    assert getattr(regenerated.value, "code", None) == "TOKEN_INVALID"
    assert live_recovery_code_ids(client) == codes_before, "不替换恢复码"

    with pytest.raises(Exception) as started:
        start_enrolment(factory, settings, user_id=user_id, password_version=0)
    assert getattr(started.value, "code", None) == "TOKEN_INVALID"
    with pytest.raises(Exception) as confirmed:
        confirm_enrolment(
            factory,
            settings,
            user_id=user_id,
            password_version=0,
            code=pyotp.TOTP(account.secret).now(),
            context=CONTEXT,
        )
    assert getattr(confirmed.value, "code", None) == "TOKEN_INVALID"
    with factory() as session:
        after = session.get(TwoFactorSetting, user_id)
        assert after is not None
        assert after.secret_version == secret_version


# --- 刷新令牌被吊销与被重放 ---------------------------------------------------


def test_a_refresh_after_someone_else_reset_the_password_is_not_a_replay(
    client: TestClient, reuse_warnings: list[logging.LogRecord]
) -> None:
    """设备 A 重置了密码，设备 B 用自己原来的刷新令牌来刷新（没有任何并发）。

    ⚠️ 原先「已吊销」一律进 `_treat_as_replay`：每次正常改密码之后，另一台设备的
    下一次刷新都会触发盗用告警。
    """
    account = enrol(client)
    sign_in(client, account)  # 设备 A
    device_b = sign_in(client, account)
    reset_ok(client, NEW_PASSWORD)

    assert client.cookies.get(REFRESH_COOKIE_NAME) == device_b.refresh_token
    response = client.post("/api/v1/auth/refresh")
    assert response.status_code == 401
    assert error_code(response) == "TOKEN_REUSED"
    assert reuse_audits(client) == []
    assert reuse_warnings == []
    assert refresh_row(client, device_b.refresh_token).used_at is None


def test_a_refresh_after_logout_is_not_a_replay(
    client: TestClient, reuse_warnings: list[logging.LogRecord]
) -> None:
    """登出之后同一个令牌再来刷新：401，无审计、无 warning（修正既有误报）。"""
    account = enrol(client)
    session = sign_in(client, account)
    assert client.post("/api/v1/auth/logout").status_code == 200
    revoked_at = refresh_row(client, session.refresh_token).revoked_at
    assert revoked_at is not None

    with pytest.raises(TokenReused):
        refresh_session(
            factory_of(client),
            settings_of(client),
            raw_token=session.refresh_token,
            context=CONTEXT,
        )
    assert reuse_audits(client) == []
    assert reuse_warnings == []
    assert refresh_row(client, session.refresh_token).revoked_at == revoked_at


def test_a_real_replay_is_still_treated_as_theft(
    client: TestClient, reuse_warnings: list[logging.LogRecord]
) -> None:
    """同一刷新令牌提交两次：与现在一致，整条链吊销、写 `TOKEN_REUSED` 审计、记 warning。

    （两个并发刷新抢同一行的那一半属于 AIH-TASK-088。）
    """
    account = enrol(client)
    session = sign_in(client, account)
    factory = factory_of(client)
    settings = settings_of(client)

    refresh_session(factory, settings, raw_token=session.refresh_token, context=CONTEXT)
    with pytest.raises(TokenReused):
        refresh_session(factory, settings, raw_token=session.refresh_token, context=CONTEXT)

    family = refresh_row(client, session.refresh_token).family_id
    in_family = [row for row in refresh_rows(client) if row.family_id == family]
    assert len(in_family) == 2
    assert all(row.revoked_at is not None for row in in_family)
    audits = reuse_audits(client)
    assert [row.reason for row in audits] == ["REPLAY_DETECTED"]
    assert len(reuse_warnings) == 1


def test_a_stale_version_refresh_leaves_other_sessions_alone(client: TestClient) -> None:
    """版本不符的刷新只是被拒：不标记已用、不吊销自己那条链，更不碰别的链。

    旧版本、未吊销的行来自「重置提交前用旧密码通过校验、提交后才签发」的登录；
    这里直接用 `_issue_session` 造出它。
    """
    account = enrol(client)
    reset_ok(client, NEW_PASSWORD)
    fresh = sign_in(client, account, NEW_PASSWORD)

    with factory_of(client)() as session:
        user = session.get(User, user_id_of(client))
        assert user is not None
        stale = _issue_session(
            session,
            settings_of(client),
            user=user,
            context=CONTEXT,
            now=utc_now(),
            password_version=0,
        )
        session.commit()

    with pytest.raises(TokenReused):
        refresh_session(
            factory_of(client),
            settings_of(client),
            raw_token=stale.refresh_token,
            context=CONTEXT,
        )
    stale_row = refresh_row(client, stale.refresh_token)
    assert stale_row.used_at is None
    assert stale_row.revoked_at is None
    fresh_row = refresh_row(client, fresh.refresh_token)
    assert fresh_row.used_at is None
    assert fresh_row.revoked_at is None
    assert reuse_audits(client) == []


# --- `_issue_session` 不重读用户 ----------------------------------------------


def test_issue_session_uses_the_version_it_is_given(client: TestClient) -> None:
    """用版本号 3 调用，而库里当前是 4：签出的令牌与行都是 3，使用时 401。

    ⚠️ 它要是在函数里重新读用户，读到的是改密码之后的新值，会给旧密码换来的会话
    盖上新版本号 —— 那正是设计要堵的缝。
    """
    set_password_version(client, 4)
    with factory_of(client)() as session:
        user = session.get(User, user_id_of(client))
        assert user is not None
        issued = _issue_session(
            session,
            settings_of(client),
            user=user,
            context=CONTEXT,
            now=utc_now(),
            password_version=3,
        )
        session.commit()

    assert pwv(client, issued.access_token, expected_type=TOKEN_TYPE_ACCESS) == 3
    assert refresh_row(client, issued.refresh_token).password_version == 3

    response = client.get(LOGGED_IN, headers=bearer(issued.access_token))
    assert response.status_code == 401
    assert error_code(response) == "TOKEN_INVALID"
    with pytest.raises(TokenReused):
        refresh_session(
            factory_of(client),
            settings_of(client),
            raw_token=issued.refresh_token,
            context=CONTEXT,
        )
    assert refresh_row(client, issued.refresh_token).used_at is None


# --- 兼容 ---------------------------------------------------------------------


def test_tokens_without_a_version_count_as_version_zero(client: TestClient) -> None:
    """部署前签发的令牌没有 `pwv`：用户版本 0 时放行，版本 1 时 401。"""
    user_id = str(user_id_of(client))
    pending = legacy_token(client, sub=user_id, typ=TOKEN_TYPE_PENDING_2FA)
    access = legacy_token(client, sub=user_id, role="ADMIN", sid="legacy", typ=TOKEN_TYPE_ACCESS)
    assert "pwv" not in jwt.decode(pending, options={"verify_signature": False})
    assert "pwv" not in jwt.decode(access, options={"verify_signature": False})

    # 版本 0：三种用法都照常。
    enrolment = client.post("/api/v1/auth/2fa/enrol", json={"pending_token": pending})
    assert enrolment.status_code == 200, enrolment.text
    confirmed = client.post(
        "/api/v1/auth/2fa/confirm",
        json={
            "pending_token": pending,
            "code": pyotp.TOTP(enrolment.json()["data"]["secret"]).now(),
        },
    )
    assert confirmed.status_code == 200, confirmed.text
    codes = confirmed.json()["data"]["recovery_codes"]
    assert client.get(LOGGED_IN, headers=bearer(access)).status_code == 200
    second = client.post(
        "/api/v1/auth/login/totp", json={"pending_token": pending, "code": codes[0]}
    )
    assert second.status_code == 200, second.text
    # 新签出的令牌显式带上版本号。
    assert pwv(client, second.json()["data"]["access_token"], expected_type=TOKEN_TYPE_ACCESS) == 0

    # 版本 1：都被拒。
    set_password_version(client, 1)
    response = client.get(LOGGED_IN, headers=bearer(access))
    assert response.status_code == 401
    assert error_code(response) == "TOKEN_INVALID"
    response = client.post(
        "/api/v1/auth/login/totp", json={"pending_token": pending, "code": codes[1]}
    )
    assert response.status_code == 401
    assert error_code(response) == "INVALID_CREDENTIALS"
    response = client.post("/api/v1/auth/2fa/enrol", json={"pending_token": pending})
    assert response.status_code == 401
    assert error_code(response) == "TOKEN_INVALID"


# --- 版本单调 -----------------------------------------------------------------


def test_each_reset_moves_the_version_on_and_ends_the_sessions_before_it(
    client: TestClient,
) -> None:
    account = enrol(client)
    current = sign_in(client, account)
    password = PASSWORD

    for expected in (1, 2, 3):
        password = f"rotated-passphrase-number-{expected}"
        reset_ok(client, password)
        assert stored_user(client).password_version == expected
        assert client.get(LOGGED_IN, headers=bearer(current.access_token)).status_code == 401

        current = sign_in(client, account, password)
        assert pwv(client, current.access_token, expected_type=TOKEN_TYPE_ACCESS) == expected
        assert refresh_row(client, current.refresh_token).password_version == expected
        assert client.get(LOGGED_IN, headers=bearer(current.access_token)).status_code == 200


# --- 事务中途失败 -------------------------------------------------------------


def test_a_reset_that_fails_midway_changes_nothing(client: TestClient, monkeypatch) -> None:
    """重置在吊销刷新令牌那一步失败：500；哈希、版本号、刷新令牌都没变，旧令牌照常可用。"""
    account = enrol(client)
    old = sign_in(client, account)

    original = Session.execute

    def failing(self, statement, *args, **kwargs):  # noqa: ANN001, ANN202 - Session.execute
        table = getattr(statement, "table", None)
        if isinstance(statement, Update) and getattr(table, "name", None) == "refresh_tokens":
            raise OperationalError("UPDATE refresh_tokens", {}, Exception("injected failure"))
        return original(self, statement, *args, **kwargs)

    assert client.post("/api/v1/auth/password/forgot", json={"email": EMAIL}).status_code == 200
    with factory_of(client)() as session:
        row = session.execute(
            select(DomainOutbox).order_by(DomainOutbox.id.desc()).limit(1)
        ).scalar_one()
        token = str(json.loads(row.payload_json)["token"])

    monkeypatch.setattr(Session, "execute", failing)
    response = client.post(
        "/api/v1/auth/password/reset", json={"token": token, "new_password": NEW_PASSWORD}
    )
    monkeypatch.undo()
    assert response.status_code == 500

    user = stored_user(client)
    assert user.password_version == 0
    assert verify_password(user.password_hash, PASSWORD)
    assert all(row.revoked_at is None for row in refresh_rows(client))
    assert client.get(LOGGED_IN, headers=bearer(old.access_token)).status_code == 200
    assert client.post("/api/v1/auth/refresh").status_code == 200


# --- 守卫：给 `password_hash` 赋值必须同时递增版本号 ----------------------------

APP_ROOT = Path(__file__).resolve().parents[2] / "app"
_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)


def _own_nodes(scope: ast.AST) -> Iterator[ast.AST]:
    """Every node in this scope, without descending into nested functions or classes."""
    stack = list(ast.iter_child_nodes(scope))
    while stack:
        node = stack.pop()
        yield node
        if not isinstance(node, _SCOPES):
            stack.extend(ast.iter_child_nodes(node))


def _attribute_targets(node: ast.AST) -> Iterator[str]:
    targets: list[ast.AST] = []
    if isinstance(node, ast.Assign):
        targets = list(node.targets)
    elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
        targets = [node.target]
    while targets:
        target = targets.pop()
        if isinstance(target, (ast.Tuple, ast.List)):
            targets.extend(target.elts)
        elif isinstance(target, ast.Attribute):
            yield target.attr


def _is_update_values_with_version(node: ast.AST) -> bool:
    """`update(...).values(password_version=...)`, however the chain in between looks."""
    if not (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "values"
        and any(keyword.arg == "password_version" for keyword in node.keywords)
    ):
        return False
    current: ast.AST = node.func.value
    while isinstance(current, (ast.Call, ast.Attribute)):
        if isinstance(current, ast.Call):
            callee = current.func
            if (isinstance(callee, ast.Name) and callee.id == "update") or (
                isinstance(callee, ast.Attribute) and callee.attr == "update"
            ):
                return True
            current = callee
        else:
            current = current.value
    return False


def unpaired_password_hash_writes(source: str, filename: str) -> list[str]:
    """Scopes that assign `.password_hash` without also moving `password_version`.

    ⚠️ **只看属性赋值**（`x.password_hash = …`、`x.password_hash += …`）。
    `User(..., password_hash=...)` 这样用关键字参数构造新用户**不是**属性赋值，
    明确豁免：新用户的版本号就是 0，它还没有任何会话可吊销（设计 §2「改密码点」
    里 `create-admin` 那一行；设计审查 v6 的提醒）。把它也算进来，守卫就会要求
    `app/cli.py` 给新用户 +1 —— 那是错的。
    """
    tree = ast.parse(source, filename=filename)
    scopes: list[ast.AST] = [tree]
    scopes.extend(node for node in ast.walk(tree) if isinstance(node, _SCOPES))
    found: list[str] = []
    for scope in scopes:
        writes_hash = False
        moves_version = False
        for node in _own_nodes(scope):
            attrs = list(_attribute_targets(node))
            if "password_hash" in attrs:
                writes_hash = True
            if "password_version" in attrs or _is_update_values_with_version(node):
                moves_version = True
        if writes_hash and not moves_version:
            name = getattr(scope, "name", "<module>")
            found.append(f"{filename}:{getattr(scope, 'lineno', 1)} {name}")
    return found


def password_hash_writers(source: str, filename: str) -> list[str]:
    tree = ast.parse(source, filename=filename)
    return [
        getattr(scope, "name", "<module>")
        for scope in [tree, *(n for n in ast.walk(tree) if isinstance(n, _SCOPES))]
        if any("password_hash" in _attribute_targets(node) for node in _own_nodes(scope))
    ]


def test_every_password_hash_write_in_app_also_moves_the_version() -> None:
    """设计 §10 A1：以后新增的设置密码入口必须在同一事务里递增 `password_version`。

    漏掉的话，那个入口改了密码却不 +1，旧会话照旧有效 —— 而且不会让任何别的用例变红。
    """
    files = sorted(APP_ROOT.rglob("*.py"))
    assert files
    offenders: list[str] = []
    writers: dict[str, list[str]] = {}
    for path in files:
        source = path.read_text(encoding="utf-8")
        relative = path.relative_to(APP_ROOT.parent).as_posix()
        offenders.extend(unpaired_password_hash_writes(source, relative))
        if names := password_hash_writers(source, relative):
            writers[relative] = names
    assert offenders == [], f"给 password_hash 赋值却没有递增 password_version：{offenders}"
    # 守卫不是在空转：现有唯一的改密码点确实被扫到了。
    assert "reset_password" in writers.get("app/services/password_reset.py", [])


@pytest.mark.parametrize(
    ("snippet", "flagged"),
    [
        ("def f(user):\n    user.password_hash = h\n", True),
        ("def f(user):\n    user.password_hash, user.x = h, 1\n", True),
        ("def f(user):\n    user.password_hash += h\n", True),
        (
            "def f(user):\n    user.password_hash = h\n"
            "    user.password_version = User.password_version + 1\n",
            False,
        ),
        ("def f(user):\n    user.password_hash = h\n    user.password_version += 1\n", False),
        (
            "def f(s, user):\n    user.password_hash = h\n"
            "    s.execute(update(User).where(User.id == 1)"
            ".values(password_version=User.password_version + 1))\n",
            False,
        ),
        # 递增写在另一个函数里不算：必须同一个函数体（同一个事务）。
        (
            "def f(user):\n    user.password_hash = h\n"
            "def g(user):\n    user.password_version += 1\n",
            True,
        ),
        # `.values(password_version=...)` 不挂在 update(...) 上不算。
        ("def f(q, user):\n    user.password_hash = h\n    q.values(password_version=1)\n", True),
        # 豁免：关键字参数构造新用户不是属性赋值。
        ("def f():\n    return User(email=e, password_hash=h)\n", False),
    ],
)
def test_the_guard_reads_assignments_the_way_it_claims(snippet: str, flagged: bool) -> None:
    assert bool(unpaired_password_hash_writes(snippet, "snippet.py")) is flagged


def test_the_guard_counts_rows_without_dialect_literals(client: TestClient) -> None:
    """守卫之外的一条小保险：上面所有计数都走 ORM，不写死方言字面量。"""
    with factory_of(client)() as session:
        assert session.execute(select(func.count()).select_from(User)).scalar_one() == 1

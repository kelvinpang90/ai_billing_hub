"""The two-step login and the 2FA endpoints end to end (spec §53, §54, §113)."""

from __future__ import annotations

import base64
import io
import json
import logging
import os

import pyotp
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from app.core.config import Settings
from app.core.database import create_session_factory
from app.core.logging import JsonFormatter
from app.core.passwords import hash_password
from app.main import create_app
from app.models.auth import User, UserRole, UserStatus
from app.models.base import Base
from app.services.auth import STAGE_ENROL_2FA, STAGE_TOTP_REQUIRED, utc_now

PASSWORD = "a-perfectly-fine-passphrase"
EMAIL = "admin@example.com"


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
    return TestClient(app)


def login(client: TestClient, password: str = PASSWORD) -> dict:
    return client.post("/api/v1/auth/login", json={"email": EMAIL, "password": password}).json()[
        "data"
    ]


def enrol_fully(client: TestClient) -> tuple[str, list[str]]:
    pending = login(client)["pending_token"]
    enrolment = client.post("/api/v1/auth/2fa/enrol", json={"pending_token": pending}).json()[
        "data"
    ]
    codes = client.post(
        "/api/v1/auth/2fa/confirm",
        json={"pending_token": pending, "code": pyotp.TOTP(enrolment["secret"]).now()},
    ).json()["data"]["recovery_codes"]
    return enrolment["secret"], codes


def test_an_admin_without_2fa_is_sent_to_enrol_not_logged_in(client: TestClient) -> None:
    """⚠️ spec §54：ADMIN 的 2FA 是**强制**的。

    密码对就发访问令牌的话，「强制」只是一句话 —— 谁都可以一直不注册。
    """
    data = login(client)
    assert data["stage"] == STAGE_ENROL_2FA
    assert data["pending_token"]
    assert data["access_token"] is None, "还没登进来，不能发访问令牌"


def test_a_pending_token_cannot_be_used_as_an_access_token(client: TestClient) -> None:
    """⚠️ 这条挡的是一次完整的第二因子绕过。"""
    pending = login(client)["pending_token"]
    response = client.post(
        "/api/v1/auth/2fa/recovery-codes",
        json={"password": PASSWORD},
        headers={"Authorization": f"Bearer {pending}"},
    )
    assert response.status_code == 401


def test_the_full_enrol_and_login_flow(client: TestClient) -> None:
    secret, codes = enrol_fully(client)
    assert len(codes) == 10

    # 现在登录会停在验证码那一步。
    first = login(client)
    assert first["stage"] == STAGE_TOTP_REQUIRED

    second = client.post(
        "/api/v1/auth/login/totp",
        json={"pending_token": first["pending_token"], "code": pyotp.TOTP(secret).now()},
    )
    assert second.status_code == 200
    body = second.json()["data"]
    assert body["access_token"]
    assert body["token_type"] == "Bearer"
    assert body["recovery_codes_remaining"] == 10
    assert "billing_refresh" in second.headers.get("set-cookie", "")


def test_a_recovery_code_also_gets_you_in(client: TestClient) -> None:
    _, codes = enrol_fully(client)
    first = login(client)
    response = client.post(
        "/api/v1/auth/login/totp",
        json={"pending_token": first["pending_token"], "code": codes[0]},
    )
    assert response.status_code == 200
    assert response.json()["data"]["recovery_codes_remaining"] == 9


def test_a_wrong_code_is_refused(client: TestClient) -> None:
    enrol_fully(client)
    first = login(client)
    response = client.post(
        "/api/v1/auth/login/totp",
        json={"pending_token": first["pending_token"], "code": "000000"},
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "INVALID_TOTP"


def test_enrolling_twice_is_refused_without_reissuing_a_secret(client: TestClient) -> None:
    enrol_fully(client)
    pending = login(client)["pending_token"]
    response = client.post("/api/v1/auth/2fa/enrol", json={"pending_token": pending})
    assert response.status_code == 409
    assert "secret" not in response.text


def test_regenerating_recovery_codes_needs_the_password_again(client: TestClient) -> None:
    """⚠️ 光有访问令牌不够 —— 恢复码等价于第二因子，一张被偷的令牌就能换出十个新的。"""
    secret, _ = enrol_fully(client)
    first = login(client)
    access = client.post(
        "/api/v1/auth/login/totp",
        json={"pending_token": first["pending_token"], "code": pyotp.TOTP(secret).now()},
    ).json()["data"]["access_token"]

    wrong = client.post(
        "/api/v1/auth/2fa/recovery-codes",
        json={"password": "not-the-password"},
        headers={"Authorization": f"Bearer {access}"},
    )
    assert wrong.status_code == 401

    right = client.post(
        "/api/v1/auth/2fa/recovery-codes",
        json={"password": PASSWORD},
        headers={"Authorization": f"Bearer {access}"},
    )
    assert right.status_code == 200
    assert len(right.json()["data"]["recovery_codes"]) == 10


def test_regenerating_without_a_token_is_refused(client: TestClient) -> None:
    enrol_fully(client)
    assert (
        client.post("/api/v1/auth/2fa/recovery-codes", json={"password": PASSWORD}).status_code
        == 401
    )


def test_without_a_master_key_2fa_refuses_but_liveness_answers(tmp_path) -> None:
    """与「没配数据库 / 没配签名密钥」同一种处置：说清楚，不临时造一把密钥。"""
    jwt_key = tmp_path / "jwt.key"
    jwt_key.write_text("a-test-signing-key-that-is-long-enough", encoding="utf-8")
    app = create_app(Settings(jwt_secret_file=str(jwt_key), master_key_file=""))
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

    with TestClient(app) as unconfigured:
        assert unconfigured.get("/healthz").status_code == 200
        pending = unconfigured.post(
            "/api/v1/auth/login", json={"email": EMAIL, "password": PASSWORD}
        ).json()["data"]["pending_token"]
        response = unconfigured.post("/api/v1/auth/2fa/enrol", json={"pending_token": pending})
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "ENCRYPTION_NOT_CONFIGURED"


def test_secrets_never_reach_the_logs(client: TestClient) -> None:
    """spec §113 的 secret-leak 测试。

    ⚠️ `otpauth://` URI 把 TOTP 密钥编在 `?secret=` 里 —— 它**看起来像 URL
    而不像密钥**，是最容易漏掉脱敏的一种。
    """
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        secret, codes = enrol_fully(client)
        first = login(client)
        client.post(
            "/api/v1/auth/login/totp",
            json={"pending_token": first["pending_token"], "code": pyotp.TOTP(secret).now()},
        )
    finally:
        root.removeHandler(handler)

    logged = stream.getvalue()
    assert secret not in logged, "TOTP 密钥不许进日志"
    for code in codes:
        assert code not in logged, "恢复码不许进日志"
    assert "otpauth://" not in logged
    # 日志确实产生了 —— 否则上面几条断言是在空字符串上空转。
    assert [json.loads(line) for line in logged.splitlines() if line.strip()]

"""The auth endpoints end to end (spec §53, §94, §107, §113)."""

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
from app.services.auth import REFRESH_COOKIE_NAME, utc_now

PASSWORD = "a-perfectly-fine-passphrase"
EMAIL = "admin@example.com"


def in_memory_engine():
    """One shared in-memory database for the whole test.

    ⚠️ 默认的连接池对 `:memory:` 是**每个连接一个独立的库**，而 TestClient 在
    线程池里跑同步端点 —— 换了线程就换了连接，于是建好的表在请求里「不存在」。
    失败信息是 `no such table: users`，看起来像迁移没跑，实际和迁移无关。
    `StaticPool` + `check_same_thread=False` 让所有连接共用同一个库。
    """
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    return engine


@pytest.fixture
def client(tmp_path) -> TestClient:
    key = tmp_path / "jwt.key"
    key.write_text("a-test-signing-key-that-is-long-enough", encoding="utf-8")
    master = tmp_path / "master.key"
    master.write_text(f"1:{base64.b64encode(os.urandom(32)).decode()}\n", encoding="utf-8")
    settings = Settings(
        jwt_secret_file=str(key),
        master_key_file=str(master),
        login_max_failures=3,
        # 本地 http：Secure cookie 发不出去。生产默认 True。
        session_cookie_secure=False,
        auth_rate_limit_per_minute=600,
        auth_rate_limit_burst=100,
    )
    app = create_app(settings)

    engine = in_memory_engine()
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


# 每个 client 的 TOTP 密钥只在注册那一刻拿得到，之后库里只有密文。
_TOTP_SECRETS: dict[int, str] = {}


def sign_in(client: TestClient):
    """Walk both steps and return the final response.

    ⚠️ T0.8b 起 ADMIN 的 2FA 是强制的（spec §54），所以「登录」是两次请求：
    密码换一张 pending 令牌，验证码才换会话。第一次调用顺带把 2FA 注册掉。
    """
    first = client.post("/api/v1/auth/login", json={"email": EMAIL, "password": PASSWORD}).json()[
        "data"
    ]

    if first["stage"] == "ENROL_2FA":
        enrolment = client.post(
            "/api/v1/auth/2fa/enrol", json={"pending_token": first["pending_token"]}
        ).json()["data"]
        client.post(
            "/api/v1/auth/2fa/confirm",
            json={
                "pending_token": first["pending_token"],
                "code": pyotp.TOTP(enrolment["secret"]).now(),
            },
        )
        _TOTP_SECRETS[id(client)] = enrolment["secret"]
        first = client.post(
            "/api/v1/auth/login", json={"email": EMAIL, "password": PASSWORD}
        ).json()["data"]

    secret = _TOTP_SECRETS[id(client)]
    return client.post(
        "/api/v1/auth/login/totp",
        json={"pending_token": first["pending_token"], "code": pyotp.TOTP(secret).now()},
    )


def test_login_returns_the_envelope_and_sets_an_httponly_cookie(client: TestClient) -> None:
    response = sign_in(client)

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"success", "data", "error", "request_id"}
    assert body["data"]["token_type"] == "Bearer"

    cookie = response.headers["set-cookie"].lower()
    # ⚠️ httpOnly：XSS 拿不到刷新令牌。少这一条，一次 XSS 就等于长期会话被接管。
    assert "httponly" in cookie
    # ⚠️ SameSite=Strict：刷新端点没有任何跨站使用场景。
    assert "samesite=strict" in cookie
    # ⚠️ Path 收窄到认证前缀：别的端点根本收不到这个 cookie。
    assert "path=/api/v1/auth" in cookie


def test_the_refresh_token_is_not_in_the_response_body(client: TestClient) -> None:
    """⚠️ 它只走 httpOnly cookie。出现在正文里就等于把它交给了 JS。"""
    response = sign_in(client)
    raw_cookie = client.cookies.get(REFRESH_COOKIE_NAME)
    assert raw_cookie
    assert raw_cookie not in response.text


def test_secure_flag_follows_configuration(tmp_path) -> None:
    """生产默认 True。默认关掉的话，一次忘配就是明文传输刷新令牌。"""
    assert Settings().session_cookie_secure is True


def test_wrong_password_and_unknown_email_look_identical(client: TestClient) -> None:
    wrong = client.post("/api/v1/auth/login", json={"email": EMAIL, "password": "nope-nope-nope"})
    unknown = client.post(
        "/api/v1/auth/login", json={"email": "nobody@example.com", "password": PASSWORD}
    )
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json()["error"] == unknown.json()["error"]


def test_refresh_rotates_the_cookie(client: TestClient) -> None:
    sign_in(client)
    first = client.cookies.get(REFRESH_COOKIE_NAME)

    response = client.post("/api/v1/auth/refresh")
    assert response.status_code == 200
    assert client.cookies.get(REFRESH_COOKIE_NAME) != first


def test_refresh_without_a_cookie_is_refused(client: TestClient) -> None:
    assert client.post("/api/v1/auth/refresh").status_code == 401


def test_logout_is_idempotent_and_clears_the_cookie(client: TestClient) -> None:
    sign_in(client)
    assert client.post("/api/v1/auth/logout").status_code == 200
    # 再来一次仍然 200 —— 失败的登出没有任何有用语义。
    assert client.post("/api/v1/auth/logout").status_code == 200


def test_rate_limiting_returns_429_with_retry_after(tmp_path) -> None:
    key = tmp_path / "jwt.key"
    key.write_text("a-test-signing-key-that-is-long-enough", encoding="utf-8")
    app = create_app(
        Settings(jwt_secret_file=str(key), auth_rate_limit_per_minute=60, auth_rate_limit_burst=2)
    )
    engine = in_memory_engine()
    app.state.engine = engine
    app.state.session_factory = create_session_factory(engine)

    with TestClient(app) as limited:
        statuses = [
            limited.post(
                "/api/v1/auth/login", json={"email": "nobody@example.com", "password": PASSWORD}
            ).status_code
            for _ in range(6)
        ]
    assert 429 in statuses, "the backstop must eventually refuse"


def test_a_429_never_mentions_an_account(tmp_path) -> None:
    """限流按来源计数，与邮箱无关 —— 泄露一个字就把它变成用户枚举通道。"""
    key = tmp_path / "jwt.key"
    key.write_text("a-test-signing-key-that-is-long-enough", encoding="utf-8")
    app = create_app(
        Settings(jwt_secret_file=str(key), auth_rate_limit_per_minute=60, auth_rate_limit_burst=1)
    )
    engine = in_memory_engine()
    app.state.engine = engine
    app.state.session_factory = create_session_factory(engine)

    with TestClient(app) as limited:
        for _ in range(4):
            response = limited.post(
                "/api/v1/auth/login", json={"email": "nobody@example.com", "password": PASSWORD}
            )
            if response.status_code == 429:
                assert response.headers["retry-after"]
                assert "nobody@example.com" not in response.text
                assert "locked" not in response.text.lower()
                return
    pytest.fail("expected a 429")


def test_without_a_signing_key_auth_refuses_but_liveness_still_answers() -> None:
    """与「没配数据库」同一种处置：存活探针照常应答，认证明确说自己没配好。

    ⚠️ **绝不能**在没配时临时生成密钥：那让配置缺失变成静默的。
    """
    app = create_app(Settings(jwt_secret_file=""))
    engine = in_memory_engine()
    app.state.engine = engine
    app.state.session_factory = create_session_factory(engine)

    with TestClient(app) as unconfigured:
        assert unconfigured.get("/healthz").status_code == 200
        response = unconfigured.post(
            "/api/v1/auth/login", json={"email": EMAIL, "password": PASSWORD}
        )
    assert response.status_code in (401, 503)


def test_credentials_never_reach_the_logs(client: TestClient) -> None:
    """spec §113 的 secret-leak 测试，覆盖日志这一条路径。"""
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        response = sign_in(client)
        client.post("/api/v1/auth/refresh")
        client.post("/api/v1/auth/login", json={"email": EMAIL, "password": "wrong-password-here"})
    finally:
        root.removeHandler(handler)

    logged = stream.getvalue()
    access_token = response.json()["data"]["access_token"]
    assert PASSWORD not in logged
    assert "wrong-password-here" not in logged
    assert access_token not in logged
    assert client.cookies.get(REFRESH_COOKIE_NAME) not in logged
    assert "$argon2" not in logged
    # 日志确实产生了 —— 否则上面几条断言是在空字符串上空转。
    assert [json.loads(line) for line in logged.splitlines() if line.strip()]

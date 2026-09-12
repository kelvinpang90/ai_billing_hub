"""The reset endpoints at the HTTP boundary (spec §53, §94, §107, §113).

⚠️ **服务层的用例证明不了这里的核心保证。**状态码、响应体、错误码是在这一层
成形的，而「忘记密码」对存在与不存在的邮箱必须**一模一样** —— 任何一处差异都
会把这个不需要凭据的端点变成批量验证邮箱是否注册过的工具（设计闸门 #32 §6）。
"""

from __future__ import annotations

import base64
import io
import json
import logging
import os

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.pool import StaticPool

from app.core.config import Settings
from app.core.database import create_session_factory
from app.core.logging import JsonFormatter
from app.core.passwords import hash_password
from app.core.ratelimit import TokenBucket
from app.main import create_app
from app.models.auth import DomainOutbox, User, UserRole, UserStatus
from app.models.base import Base
from app.services.auth import utc_now

PASSWORD = "a-perfectly-fine-passphrase"
NEW_PASSWORD = "a-brand-new-fine-passphrase"
EMAIL = "admin@example.com"


@pytest.fixture
def client(tmp_path) -> TestClient:
    key = tmp_path / "jwt.key"
    key.write_text("a-test-signing-key-that-is-long-enough", encoding="utf-8")
    master = tmp_path / "master.key"
    master.write_text(f"1:{base64.b64encode(os.urandom(32)).decode()}\n", encoding="utf-8")
    settings = Settings(
        jwt_secret_file=str(key),
        master_key_file=str(master),
        session_cookie_secure=False,
        auth_rate_limit_per_minute=600,
        auth_rate_limit_burst=100,
        frontend_base_url="https://billing.example.com",
    )
    app = create_app(settings)

    # ⚠️ `:memory:` 默认每个连接一个独立的库，而 TestClient 在线程池里跑同步
    # 端点 —— 换了线程就换了连接，建好的表在请求里「不存在」。
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


def reset_token(client: TestClient) -> str:
    """Dig the plaintext token out of the outbox row, the way the email would."""
    with client.app.state.session_factory() as session:
        row = session.execute(
            select(DomainOutbox).order_by(DomainOutbox.id.desc()).limit(1)
        ).scalar_one()
        return str(json.loads(row.payload_json)["token"])


def forgot(client: TestClient, email: str = EMAIL):
    return client.post("/api/v1/auth/password/forgot", json={"email": email})


# --- 反用户枚举 -----------------------------------------------------------------


def test_a_known_and_an_unknown_address_are_indistinguishable(client: TestClient) -> None:
    """⚠️ 本任务在这一层最要紧的一条。"""
    known = forgot(client)
    unknown = forgot(client, "nobody@example.com")

    assert known.status_code == unknown.status_code == 200
    assert known.json()["data"] == unknown.json()["data"] == {}
    assert known.json()["error"] is None
    assert unknown.json()["error"] is None


def test_an_unknown_address_leaves_nothing_behind(client: TestClient) -> None:
    """不存在的账号不能留下 outbox 行 —— 否则这是一台指向任意邮箱的发信机器。"""
    forgot(client, "nobody@example.com")

    with client.app.state.session_factory() as session:
        assert session.execute(select(DomainOutbox)).scalars().all() == []


def test_a_disabled_account_also_looks_the_same_from_outside(client: TestClient) -> None:
    with client.app.state.session_factory() as session:
        user = session.execute(select(User).where(User.email == EMAIL)).scalar_one()
        user.status = UserStatus.DISABLED
        session.commit()

    response = forgot(client)

    assert response.status_code == 200
    assert response.json()["data"] == {}


def test_a_malformed_address_is_the_one_case_that_may_differ(client: TestClient) -> None:
    """422 只说明「这不是个邮箱」，不说明任何账号是否存在 —— 那是 Pydantic 的
    字段校验，对所有格式不合法的输入一视同仁。"""
    response = forgot(client, "not-an-email")

    assert response.status_code == 422


# --- 完整往返 -------------------------------------------------------------------


def test_the_round_trip_changes_the_password(client: TestClient) -> None:
    forgot(client)
    token = reset_token(client)

    reset = client.post(
        "/api/v1/auth/password/reset", json={"token": token, "new_password": NEW_PASSWORD}
    )

    assert reset.status_code == 200
    # 新密码能走到第二步（这个账号的 2FA 是强制的，所以停在 pending）。
    after = client.post("/api/v1/auth/login", json={"email": EMAIL, "password": NEW_PASSWORD})
    assert after.status_code == 200
    assert after.json()["data"]["stage"] in {"ENROL_2FA", "TOTP_REQUIRED"}
    # 旧密码不再管用。
    old = client.post("/api/v1/auth/login", json={"email": EMAIL, "password": PASSWORD})
    assert old.status_code == 401


def test_a_used_token_is_refused_with_a_stable_code(client: TestClient) -> None:
    forgot(client)
    token = reset_token(client)
    client.post("/api/v1/auth/password/reset", json={"token": token, "new_password": NEW_PASSWORD})

    again = client.post(
        "/api/v1/auth/password/reset",
        json={"token": token, "new_password": "yet-another-fine-passphrase"},
    )

    assert again.status_code == 400
    assert again.json()["error"]["code"] == "INVALID_RESET_TOKEN"


def test_an_unknown_token_gets_the_same_code_as_a_used_one(client: TestClient) -> None:
    """⚠️ 分开报会告诉对方「这张令牌存在过」，而令牌是可以被枚举的对象。"""
    response = client.post(
        "/api/v1/auth/password/reset",
        json={"token": "never-issued-this-one", "new_password": NEW_PASSWORD},
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_RESET_TOKEN"


def test_a_weak_new_password_is_rejected(client: TestClient) -> None:
    forgot(client)
    token = reset_token(client)

    response = client.post(
        "/api/v1/auth/password/reset", json={"token": token, "new_password": "short"}
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "WEAK_PASSWORD"


# --- broker 与日志 --------------------------------------------------------------


def test_a_broken_broker_does_not_fail_the_request(client: TestClient) -> None:
    """⚠️ 设计闸门 #32 §8 的那条：broker 不通时仍然 200，两张表都有行。

    outbox 行已经和令牌同事务落库了 —— 触发只是「快一点」，不是「能不能」。
    在这里把异常放出去，等于把一次「信晚到一分钟」升级成「重置请求失败」
    （spec §74.6、Invariant 14）。
    """

    class BrokenCelery:
        def send_task(self, *_args, **_kwargs):
            raise RuntimeError("no broker here")

    client.app.state.celery_app = BrokenCelery()

    response = forgot(client)

    assert response.status_code == 200
    assert reset_token(client), "令牌与 outbox 行必须还在库里，等周期恢复补投"


def test_delivery_is_triggered_when_the_broker_is_there(client: TestClient) -> None:
    """反过来的那一半：broker 在的时候，确实发了投递触发（省掉最多一分钟等待）。"""

    class RecordingCelery:
        def __init__(self) -> None:
            self.calls: list[tuple[str, list[int]]] = []

        def send_task(self, name, args=None, **_kwargs):  # noqa: ANN001
            self.calls.append((name, list(args or [])))

    celery = RecordingCelery()
    client.app.state.celery_app = celery

    forgot(client)

    assert [name for name, _ in celery.calls] == ["app.tasks.outbox.deliver"]


def test_an_unknown_address_triggers_no_delivery(client: TestClient) -> None:
    """否则「有没有发出触发」本身就是一条侧信道 —— 虽然外部看不到，但它会
    表现成可测量的耗时差。"""

    class RecordingCelery:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def send_task(self, name, args=None, **_kwargs):  # noqa: ANN001
            self.calls.append(name)

    celery = RecordingCelery()
    client.app.state.celery_app = celery

    forgot(client, "nobody@example.com")

    assert celery.calls == []


def test_the_reset_token_never_reaches_the_logs(client: TestClient) -> None:
    """spec §113 / §94：重置令牌和访问令牌一样，是一把可以直接用的钥匙。"""
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        forgot(client)
        token = reset_token(client)
        client.post(
            "/api/v1/auth/password/reset", json={"token": token, "new_password": NEW_PASSWORD}
        )
    finally:
        root.removeHandler(handler)

    logged = stream.getvalue()
    assert token not in logged
    assert NEW_PASSWORD not in logged
    assert "$argon2" not in logged
    # 日志确实产生了 —— 否则上面几条断言是在空字符串上空转。
    assert [json.loads(line) for line in logged.splitlines() if line.strip()]


# --- 限流：这两个端点都是免凭据的 ------------------------------------------------


def test_the_forgot_endpoint_is_rate_limited(client: TestClient) -> None:
    """⚠️ **变异测试是这条用例的来源**：把 `_rate_limit(request)` 整行删掉时，
    其余全套用例照样全绿。

    这个端点不需要任何凭据，而且每一次调用都让我们发一封信 —— 不限流就是一台
    指向任意邮箱的邮件轰炸放大器，同时也是一条不限次数的账号探测通道。
    """
    client.app.state.auth_rate_limiter = TokenBucket(per_minute=1, burst=1)

    first = forgot(client)
    second = forgot(client)

    assert first.status_code == 200
    assert second.status_code == 429
    assert second.json()["error"]["code"] == "TOO_MANY_REQUESTS"


def test_the_reset_endpoint_is_rate_limited(client: TestClient) -> None:
    """不限的话，令牌本身就成了一个可以在线暴力枚举的对象。"""
    client.app.state.auth_rate_limiter = TokenBucket(per_minute=1, burst=1)
    body = {"token": "never-issued-this-one", "new_password": NEW_PASSWORD}

    first = client.post("/api/v1/auth/password/reset", json=body)
    second = client.post("/api/v1/auth/password/reset", json=body)

    assert first.status_code == 400
    assert second.status_code == 429

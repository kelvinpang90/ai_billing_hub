"""Admin payment settings end to end (design gate #587 v3 §2「管理端接口」, AIH-TASK-191).

设计 §7 标 191 的一行：

- **鉴权**（匿名 401、客户 403）：由 test_admin_customers_api.py 的路由枚举用例覆盖
  （两个接口已补进 `EXPECTED_ADMIN_ROUTES`）；
- **GET**：字段与顺序、两位小数的金额、种子行的 `updated_by` 为 null、改过之后为管理员邮箱；
- **PUT**：合规（200 返回与 GET 相同的内容，版本 +1、写审计、请求体不进日志）；规则不过（422
  `PAYMENT_SETTINGS_INVALID`，点名第一个不合规的字段）；缺字段、多余字段、布尔值或字符串形式的秒数
  与版本、数字形式的金额（422 `VALIDATION_ERROR`）；版本冲突（409 且数据不变）；
- **行缺失**：GET 与 PUT 都是 503 `PAYMENTS_MISCONFIGURED`。

世界用共用的 payment_world.py（SQLite 与真 MySQL 各跑一次）；设置的种子行经 conftest 注册的
`payment_settings_seeder` 写入。本文件的夹具只在这里用，定义在这里。时刻经
`admin_payment_settings.utc_now` 注入，不 sleep；这两个接口不碰 Stripe。

MySQL 那部分需要 `BILLING_TEST_DATABASE_URL`，没设时 skip ——
**不要把 skipped 读成 passed**，CI 设了它并把 skipped 判成失败。
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from payment_world import (
    SEED_SETTINGS_AT,
    World,
    _populate,
    audit_state,
    build_mysql_world,
    remove_payment_settings,
)
from sqlalchemy import create_engine, func, select
from sqlalchemy.pool import StaticPool

from app.api import admin_payment_settings
from app.core.config import Settings
from app.core.database import create_session_factory
from app.core.tokens import issue_access_token
from app.main import create_app
from app.models.auth import AuditAction, AuditLog, User
from app.models.base import Base
from app.models.payment_settings import PaymentSettings as PaymentSettingsRow
from app.services.auth import utc_now

SETTINGS = "/api/v1/admin/payment-settings"

# 带小数秒：响应与库里都应截到整秒。
NOW = dt.datetime(2026, 11, 9, 9, 30, 0, 750_000)
STAMP = NOW.replace(microsecond=0)

# 设计 §2 的字段与顺序：断言「恰好等于」，按顺序比。
SETTINGS_KEYS = [
    "min_amount",
    "max_amount",
    "recovery_buffer",
    "preset_amounts",
    "reuse_window_seconds",
    "pending_timeout_seconds",
    "version",
    "updated_at",
    "updated_by",
]
ENVELOPE_KEYS = {"success", "data", "error", "request_id"}
SEED_PRESETS = ["50.00", "100.00", "300.00", "500.00", "1000.00"]
SEED_VIEW = {
    "min_amount": "10.00",
    "max_amount": None,
    "recovery_buffer": "10.00",
    "preset_amounts": SEED_PRESETS,
    "reuse_window_seconds": 60,
    "pending_timeout_seconds": 3600,
    "version": 1,
    "updated_at": SEED_SETTINGS_AT.isoformat(),
    "updated_by": None,
}
# 一份合规的新值：金额的写法不统一（"20"、"4321.9"），返回时一律两位小数。
# "4321.9" 足够独特，日志里出现就认得出来。
NEW_BODY: dict[str, Any] = {
    "min_amount": "20",
    "max_amount": "4321.9",
    "recovery_buffer": "0.01",
    "preset_amounts": ["20", "100.5", "4321.90"],
    "reuse_window_seconds": 0,
    "pending_timeout_seconds": 2400,
    "version": 1,
}


# --- 夹具 ---------------------------------------------------------------


def _sqlite_world() -> Iterator[World]:
    # StaticPool：TestClient 在线程池里跑同步端点，内存库必须所有连接共用一个
    # （与 test_admin_payments_api.py 同理）。
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    try:
        yield _populate(create_session_factory(engine))
    finally:
        engine.dispose()


@pytest.fixture(params=["sqlite", "mysql"])
def world(request, monkeypatch, payment_settings_seeder) -> Iterator[World]:
    """The world with the seed payment settings of migration 0038."""
    source = build_mysql_world(monkeypatch) if request.param == "mysql" else _sqlite_world()
    built = next(source)
    try:
        payment_settings_seeder(built.factory)
        yield built
    finally:
        for _ in source:
            pass


@pytest.fixture
def make_client(tmp_path, world, monkeypatch) -> Callable[..., TestClient]:
    """A signed-in admin client; the clock is injected."""
    monkeypatch.setattr(admin_payment_settings, "utc_now", lambda: NOW)
    key = tmp_path / "jwt.key"
    key.write_text("a-test-signing-key-that-is-long-enough", encoding="utf-8")

    def build(*, payments_enabled: bool = True) -> TestClient:
        application = create_app(
            Settings(jwt_secret_file=str(key), database_url="", payments_enabled=payments_enabled)
        )
        application.state.session_factory = world.factory
        token = issue_access_token(
            application.state.settings,
            user_id=world.admin_id,
            role="ADMIN",
            session_id=uuid.uuid4().hex,
            now=utc_now(),
        )
        # 500 要以统一错误体返回，而不是在测试里抛出来。
        return TestClient(
            application,
            raise_server_exceptions=False,
            headers={
                "Authorization": f"Bearer {token}",
                "User-Agent": "admin-payment-settings-test",
            },
        )

    return build


@pytest.fixture
def client(make_client) -> TestClient:
    return make_client()


# --- 构件 ---------------------------------------------------------------


def data_of(response) -> Any:
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == ENVELOPE_KEYS
    assert body["success"] is True
    return body["data"]


def error_of(response) -> tuple[int, str]:
    body = response.json()
    assert set(body) == ENVELOPE_KEYS
    assert body["success"] is False
    assert body["data"] is None
    return response.status_code, body["error"]["code"]


def body(**overrides: Any) -> dict[str, Any]:
    return {**NEW_BODY, **overrides}


def without(field: str) -> dict[str, Any]:
    return {key: value for key, value in NEW_BODY.items() if key != field}


def admin_email(world: World) -> str:
    with world.factory() as session:
        user = session.get(User, world.admin_id)
        assert user is not None
        return user.email


def stored(world: World) -> dict[str, Any] | None:
    """The committed row, every column; None when there is no row."""
    with world.factory() as session:
        row = session.execute(select(PaymentSettingsRow)).scalar_one_or_none()
        if row is None:
            return None
        return {
            column.key: getattr(row, column.key) for column in PaymentSettingsRow.__table__.columns
        }


def settings_audits(world: World) -> list[AuditLog]:
    statement = (
        select(AuditLog)
        .where(AuditLog.action == AuditAction.PAYMENT_SETTINGS_UPDATE)
        .order_by(AuditLog.id)
    )
    with world.factory() as session:
        rows = list(session.execute(statement).scalars())
        for row in rows:
            session.expunge(row)
        return rows


def audit_count(world: World) -> int:
    with world.factory() as session:
        return int(session.execute(select(func.count()).select_from(AuditLog)).scalar_one())


# --- GET ----------------------------------------------------------------


def test_get_returns_the_seed_row_with_the_design_fields_in_order(world, client) -> None:
    data = data_of(client.get(SETTINGS))

    assert list(data) == SETTINGS_KEYS
    # 种子行从未被管理员改过：`updated_by` 为 null；时刻不带时区、精确到秒、不带 Z。
    assert data == SEED_VIEW


def test_get_writes_nothing(world, client) -> None:
    before, audits = stored(world), audit_count(world)

    data_of(client.get(SETTINGS))

    assert stored(world) == before
    assert audit_count(world) == audits


def test_the_routes_are_served_with_payments_switched_off(world, make_client) -> None:
    client = make_client(payments_enabled=False)

    assert data_of(client.get(SETTINGS)) == SEED_VIEW
    assert data_of(client.put(SETTINGS, json=body()))["version"] == 2


# --- PUT：合规 ----------------------------------------------------------


def test_put_replaces_every_field_and_returns_what_get_returns(world, client, caplog) -> None:
    caplog.set_level(logging.DEBUG)

    data = data_of(client.put(SETTINGS, json=body()))

    expected = {
        "min_amount": "20.00",
        "max_amount": "4321.90",
        "recovery_buffer": "0.01",
        "preset_amounts": ["20.00", "100.50", "4321.90"],
        "reuse_window_seconds": 0,
        "pending_timeout_seconds": 2400,
        "version": 2,
        "updated_at": STAMP.isoformat(),
        "updated_by": admin_email(world),
    }
    assert list(data) == SETTINGS_KEYS
    assert data == expected
    # 与之后的 GET 逐字相同（`updated_by` 由用户 id 查回邮箱）。
    assert data_of(client.get(SETTINGS)) == expected
    row = stored(world)
    assert row is not None
    assert (row["version"], row["updated_at"], row["updated_by_user_id"]) == (
        2,
        STAMP,
        world.admin_id,
    )
    assert row["preset_amounts"] == ["20.00", "100.50", "4321.90"]
    [audit] = settings_audits(world)
    assert (audit.actor_user_id, audit.entity_type, audit.entity_id) == (
        world.admin_id,
        "payment_settings",
        "1",
    )
    assert audit.user_agent == "admin-payment-settings-test"
    assert audit_state(audit.before_state)["version"] == 1
    assert audit_state(audit.after_state) == {key: expected[key] for key in SETTINGS_KEYS[:7]}
    # 日志只记管理员用户 id 与新版本号，不记请求体（设计 §6）。
    assert "4321.9" not in caplog.text
    assert "100.5" not in caplog.text


def test_the_same_values_again_still_bump_the_version(world, client) -> None:
    first = data_of(client.put(SETTINGS, json=body()))
    again = data_of(client.put(SETTINGS, json=body(version=first["version"])))

    assert again == {**first, "version": 3}
    assert [audit_state(audit.after_state)["version"] for audit in settings_audits(world)] == [
        2,
        3,
    ]


def test_max_amount_null_and_the_range_edges_are_accepted(world, client) -> None:
    data = data_of(
        client.put(
            SETTINGS,
            json=body(
                min_amount="2.00",
                max_amount=None,
                preset_amounts=["2", "999999.99"],
                reuse_window_seconds=300,
                pending_timeout_seconds=86400,
            ),
        )
    )

    assert (data["min_amount"], data["max_amount"], data["preset_amounts"]) == (
        "2.00",
        None,
        ["2.00", "999999.99"],
    )
    assert (data["reuse_window_seconds"], data["pending_timeout_seconds"]) == (300, 86400)


# --- PUT：规则不过 -------------------------------------------------------


@pytest.mark.parametrize(
    ("overrides", "field"),
    [
        ({"min_amount": "1.99"}, "min_amount"),
        ({"min_amount": "10.001"}, "min_amount"),
        ({"min_amount": " 10.00"}, "min_amount"),
        ({"max_amount": "19.99"}, "max_amount"),
        ({"recovery_buffer": "0.00"}, "recovery_buffer"),
        ({"preset_amounts": []}, "preset_amounts"),
        (
            {"preset_amounts": ["20", "30", "40", "50", "60", "70", "80", "90", "100"]},
            "preset_amounts",
        ),
        ({"preset_amounts": ["100.00", "100"]}, "preset_amounts"),
        ({"preset_amounts": ["100.00", "50.00"]}, "preset_amounts"),
        ({"preset_amounts": ["19.99"]}, "preset_amounts"),
        ({"preset_amounts": ["4322.00"]}, "preset_amounts"),
        ({"reuse_window_seconds": -1}, "reuse_window_seconds"),
        ({"reuse_window_seconds": 301}, "reuse_window_seconds"),
        ({"pending_timeout_seconds": 2399}, "pending_timeout_seconds"),
        ({"pending_timeout_seconds": 86401}, "pending_timeout_seconds"),
        # 多个字段同时不合规：点名设计顺序里的第一个。
        (
            {"recovery_buffer": "0", "reuse_window_seconds": 999, "max_amount": "1"},
            "max_amount",
        ),
    ],
    ids=[
        "min-below-floor",
        "min-three-decimals",
        "min-whitespace",
        "max-below-min",
        "buffer-zero",
        "presets-empty",
        "presets-nine",
        "presets-duplicate",
        "presets-descending",
        "preset-below-min",
        "preset-above-max",
        "window-negative",
        "window-above",
        "timeout-below",
        "timeout-above",
        "first-field-named",
    ],
)
def test_rule_failures_are_422_naming_the_field_and_change_nothing(
    world, client, overrides, field
) -> None:
    before, audits = stored(world), audit_count(world)

    response = client.put(SETTINGS, json=body(**overrides))

    assert error_of(response) == (422, "PAYMENT_SETTINGS_INVALID")
    assert field in response.json()["error"]["message"]
    assert stored(world) == before
    assert audit_count(world) == audits


# --- PUT：请求层的类型 ---------------------------------------------------


@pytest.mark.parametrize("field", list(NEW_BODY))
def test_a_missing_field_is_a_validation_error(world, client, field) -> None:
    before = stored(world)

    response = client.put(SETTINGS, json=without(field))

    assert error_of(response) == (422, "VALIDATION_ERROR")
    assert field in response.json()["error"]["message"]
    assert stored(world) == before


@pytest.mark.parametrize(
    "payload",
    [
        body(updated_by="someone@example.com"),
        body(updated_at="2026-11-09T00:00:00"),
        body(currency="MYR"),
        [],
        "not-an-object",
    ],
    ids=["extra-updated-by", "extra-updated-at", "extra-currency", "list", "string"],
)
def test_extra_fields_or_a_non_object_are_validation_errors(world, client, payload) -> None:
    before = stored(world)

    assert error_of(client.put(SETTINGS, json=payload)) == (422, "VALIDATION_ERROR")
    assert stored(world) == before


@pytest.mark.parametrize(
    "overrides",
    [
        # 布尔值不是整数。
        {"reuse_window_seconds": True},
        {"pending_timeout_seconds": False},
        {"version": True},
        # 字符串形式的数字不是整数。
        {"reuse_window_seconds": "60"},
        {"pending_timeout_seconds": "3600"},
        {"version": "1"},
        # 带小数点的数不是整数。
        {"reuse_window_seconds": 60.0},
        {"version": 1.0},
        {"pending_timeout_seconds": None},
        {"version": None},
    ],
    ids=[
        "bool-window",
        "bool-timeout",
        "bool-version",
        "string-window",
        "string-timeout",
        "string-version",
        "float-window",
        "float-version",
        "null-timeout",
        "null-version",
    ],
)
def test_non_integer_seconds_or_version_are_validation_errors(world, client, overrides) -> None:
    before = stored(world)

    assert error_of(client.put(SETTINGS, json=body(**overrides))) == (422, "VALIDATION_ERROR")
    assert stored(world) == before


@pytest.mark.parametrize(
    "overrides",
    [
        {"min_amount": 20},
        {"min_amount": 20.5},
        {"max_amount": 4321.9},
        {"recovery_buffer": 1},
        {"preset_amounts": [20, 100.5]},
        {"preset_amounts": ["20", 100]},
        {"preset_amounts": "20,100"},
        {"min_amount": None},
        {"recovery_buffer": True},
    ],
    ids=[
        "int-min",
        "float-min",
        "float-max",
        "int-buffer",
        "number-presets",
        "mixed-presets",
        "string-presets",
        "null-min",
        "bool-buffer",
    ],
)
def test_numeric_amounts_are_validation_errors(world, client, overrides) -> None:
    before = stored(world)

    assert error_of(client.put(SETTINGS, json=body(**overrides))) == (422, "VALIDATION_ERROR")
    assert stored(world) == before


# --- PUT：版本冲突 -------------------------------------------------------


def test_a_stale_version_is_409_and_the_other_change_is_kept(world, client) -> None:
    """两个管理员都读到版本 1：先提交的成功，后提交的 409，不静默覆盖。"""
    first = data_of(client.put(SETTINGS, json=body()))
    after_first, audits = stored(world), audit_count(world)

    late = client.put(SETTINGS, json=body(min_amount="30.00", preset_amounts=["30.00"]))
    future = client.put(SETTINGS, json=body(version=99))

    assert error_of(late) == (409, "PAYMENT_SETTINGS_VERSION_CONFLICT")
    assert error_of(future) == (409, "PAYMENT_SETTINGS_VERSION_CONFLICT")
    assert stored(world) == after_first
    assert audit_count(world) == audits
    assert data_of(client.get(SETTINGS)) == first


def test_rules_are_judged_before_the_version(world, client) -> None:
    response = client.put(SETTINGS, json=body(recovery_buffer="0.00", version=99))

    assert error_of(response) == (422, "PAYMENT_SETTINGS_INVALID")


# --- 行缺失 -------------------------------------------------------------


def test_without_the_settings_row_both_are_503(world, client) -> None:
    remove_payment_settings(world.factory)

    read = client.get(SETTINGS)
    written = client.put(SETTINGS, json=body())

    assert error_of(read) == (503, "PAYMENTS_MISCONFIGURED")
    # 行缺失不当作版本冲突。
    assert error_of(written) == (503, "PAYMENTS_MISCONFIGURED")
    assert stored(world) is None
    assert settings_audits(world) == []

"""Payment settings service and the recovery minimum (design gate #587 v3, AIH-TASK-189).

设计 §7 标 189 的各行（「设置生效」「挂起租户建单」两行在 test_payment_create.py）：

- **设置校验**：规则的字段顺序（多个字段同时不合规时点名第一个）；§2 的每条规则与边界；
- **改设置写审计与版本**：正常修改；与旧值相同的修改（也 +1、也写审计）；版本不符（409 且数据
  不变）；行缺失（503）；规则不过不连库；
- **并发改设置**（真 MySQL）：两个连接带同一版本同时提交，一个成功、一个 409；
- **最小恢复额**：缓冲 10.00、最低额 10.00 时的各组余额；缓冲 0.01、余额 −50.00；非 PREPAID；
  恢复额超过最高额、最高额为空而恢复额超过 999999.99；
- **配置项已删除**：设置了旧的四个环境变量，不影响任何行为。

`create_all` 建的库没有迁移的种子行：用例先经 `payment_settings_seeder` 夹具（conftest.py）写入。
MySQL 那部分需要 `BILLING_TEST_DATABASE_URL` 指向一个**可以被清空的**库，没设时 skip ——
**不要把 skipped 读成 passed**，CI 设了它并把 skipped 判成失败。Stripe 一律是注入了假传输的
`StripeGateway`，不联网；时间一律注入。
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import threading
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from typing import Any

import pytest
from payment_world import (
    SEED_PAYMENT_SETTINGS,
    SEED_SETTINGS_AT,
    World,
    audit_state,
    build_mysql_world,
    build_sqlite_world,
    remove_payment_settings,
)
from sqlalchemy import func, select
from test_payment_create import NOW as TOPUP_NOW
from test_payment_create import _seeded
from test_payment_create import _sqlite_world as topup_sqlite_world

from app.core.config import Settings
from app.models.auth import AuditAction, AuditLog, User, UserRole
from app.models.payment_settings import PaymentSettings as PaymentSettingsRow
from app.models.tenancy import BillingMode
from app.services.audit_query import PUBLIC_ENTITY_TYPES
from app.services.auth import RequestContext
from app.services.payment_settings import (
    ENTITY_PAYMENT_SETTINGS,
    PaymentSettings,
    PaymentSettingsInvalid,
    PaymentSettingsMissing,
    PaymentSettingsValues,
    PaymentSettingsVersionConflict,
    effective_minimum,
    get_payment_settings,
    recovery_exceeds_cap,
    recovery_minimum,
    topup_cap,
    update_payment_settings,
    validate_payment_settings,
)

# 截到整秒：小数秒不进 `updated_at` 与审计。
NOW = dt.datetime(2026, 11, 9, 9, 30, 0, 750_000)
STAMP = NOW.replace(microsecond=0)
CONTEXT = RequestContext(ip_address="203.0.113.7", user_agent="payment-settings-test")

SEED_PRESETS = ["50.00", "100.00", "300.00", "500.00", "1000.00"]
SEED_VALUES = PaymentSettingsValues(
    min_amount="10.00",
    max_amount=None,
    recovery_buffer="10.00",
    preset_amounts=SEED_PRESETS,
    reuse_window_seconds=60,
    pending_timeout_seconds=3600,
)
NEW_VALUES = PaymentSettingsValues(
    min_amount="20",
    max_amount="5000.5",
    recovery_buffer="0.01",
    preset_amounts=["20", "100.5", "5000.50"],
    reuse_window_seconds=0,
    pending_timeout_seconds=2400,
)
SEED_AUDIT = {
    "min_amount": "10.00",
    "max_amount": None,
    "recovery_buffer": "10.00",
    "preset_amounts": SEED_PRESETS,
    "reuse_window_seconds": 60,
    "pending_timeout_seconds": 3600,
    "version": 1,
}

# 设计 §2 的字段顺序。
FIELD_ORDER = [
    "min_amount",
    "max_amount",
    "recovery_buffer",
    "preset_amounts",
    "reuse_window_seconds",
    "pending_timeout_seconds",
]


def values(**overrides: Any) -> PaymentSettingsValues:
    return dataclasses.replace(SEED_VALUES, **overrides)


def rules(
    *,
    min_amount: str = "10.00",
    max_amount: str | None = None,
    recovery_buffer: str = "10.00",
) -> PaymentSettings:
    """A settings snapshot for the pure functions (no database)."""
    return PaymentSettings(
        min_amount=Decimal(min_amount),
        max_amount=None if max_amount is None else Decimal(max_amount),
        recovery_buffer=Decimal(recovery_buffer),
        preset_amounts=(Decimal("50.00"),),
        reuse_window_seconds=60,
        pending_timeout_seconds=3600,
        version=1,
        updated_at=SEED_SETTINGS_AT,
        updated_by_user_id=None,
    )


# --- 夹具 -----------------------------------------------------------------------------


@pytest.fixture(params=["sqlite", "mysql"])
def world(request, monkeypatch, payment_settings_seeder) -> Iterator[World]:
    worlds = build_mysql_world(monkeypatch) if request.param == "mysql" else build_sqlite_world()
    yield from _with_settings(worlds, payment_settings_seeder)


@pytest.fixture
def mysql_world_with_settings(monkeypatch, payment_settings_seeder) -> Iterator[World]:
    yield from _with_settings(build_mysql_world(monkeypatch), payment_settings_seeder)


def _with_settings(worlds: Iterator[World], seed: Callable[..., None]) -> Iterator[World]:
    """payment_world 的世界，建好后先写入种子设置行。"""
    for world in worlds:
        seed(world.factory)
        yield world


def admin(world: World) -> User:
    with world.factory() as session:
        user = session.get(User, world.admin_id)
        assert user is not None and user.role is UserRole.ADMIN
        session.expunge(user)
        return user


def update(world: World, settings: PaymentSettingsValues, version: int) -> PaymentSettings:
    return update_payment_settings(
        world.factory,
        actor=admin(world),
        values=settings,
        expected_version=version,
        context=CONTEXT,
        now=NOW,
    )


def current(world: World) -> PaymentSettings:
    with world.factory() as session:
        return get_payment_settings(session)


def stored_presets(world: World) -> list[str]:
    with world.factory() as session:
        return list(session.execute(select(PaymentSettingsRow.preset_amounts)).scalar_one())


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


# --- 设置校验 ---------------------------------------------------------------------------


def test_the_seed_values_pass_the_rules() -> None:
    checked = validate_payment_settings(SEED_VALUES)

    assert checked.min_amount == Decimal("10.00")
    assert checked.preset_amounts == tuple(Decimal(text) for text in SEED_PRESETS)


VALID_CASES: list[tuple[str, dict[str, Any]]] = [
    ("min-2.00", {"min_amount": "2.00", "preset_amounts": ["2"]}),
    ("min-999999.99", {"min_amount": "999999.99", "preset_amounts": ["999999.99"]}),
    ("max-equals-min", {"max_amount": "10.00", "preset_amounts": ["10"]}),
    ("max-999999.99", {"max_amount": "999999.99"}),
    ("max-null", {"max_amount": None}),
    ("buffer-0.01", {"recovery_buffer": "0.01"}),
    ("one-preset", {"preset_amounts": ["10.00"]}),
    ("eight-presets", {"preset_amounts": [str(10 * n) for n in range(1, 9)]}),
    ("presets-at-max", {"max_amount": "1000.00"}),
    ("window-0", {"reuse_window_seconds": 0}),
    ("window-300", {"reuse_window_seconds": 300}),
    ("timeout-2400", {"pending_timeout_seconds": 2400}),
    ("timeout-86400", {"pending_timeout_seconds": 86_400}),
]


@pytest.mark.parametrize(
    "overrides", [case for _, case in VALID_CASES], ids=[name for name, _ in VALID_CASES]
)
def test_values_within_the_rules_pass(overrides: dict[str, Any]) -> None:
    validate_payment_settings(values(**overrides))


INVALID_CASES: list[tuple[str, dict[str, Any], str]] = [
    ("min-1.99", {"min_amount": "1.99"}, "min_amount"),
    ("min-three-decimals", {"min_amount": "10.001"}, "min_amount"),
    ("min-seven-digits", {"min_amount": "1000000"}, "min_amount"),
    ("min-not-digits", {"min_amount": "ten"}, "min_amount"),
    ("min-negative", {"min_amount": "-10"}, "min_amount"),
    ("min-blank", {"min_amount": ""}, "min_amount"),
    ("min-not-a-string", {"min_amount": Decimal("10")}, "min_amount"),
    ("max-below-min", {"max_amount": "9.99"}, "max_amount"),
    ("max-seven-digits", {"max_amount": "1000000.00"}, "max_amount"),
    ("max-three-decimals", {"max_amount": "100.001"}, "max_amount"),
    ("max-blank", {"max_amount": ""}, "max_amount"),
    ("buffer-0.00", {"recovery_buffer": "0.00"}, "recovery_buffer"),
    ("buffer-0", {"recovery_buffer": "0"}, "recovery_buffer"),
    ("buffer-three-decimals", {"recovery_buffer": "0.005"}, "recovery_buffer"),
    ("no-presets", {"preset_amounts": []}, "preset_amounts"),
    ("nine-presets", {"preset_amounts": [str(10 * n) for n in range(1, 10)]}, "preset_amounts"),
    ("duplicate-preset", {"preset_amounts": ["50.00", "50.00"]}, "preset_amounts"),
    ("same-value-twice", {"preset_amounts": ["50", "50.00"]}, "preset_amounts"),
    ("descending-presets", {"preset_amounts": ["100.00", "50.00"]}, "preset_amounts"),
    ("preset-below-min", {"preset_amounts": ["9.99", "50.00"]}, "preset_amounts"),
    ("preset-above-max", {"max_amount": "500.00"}, "preset_amounts"),
    ("preset-bad-format", {"preset_amounts": ["50.001"]}, "preset_amounts"),
    ("preset-not-a-string", {"preset_amounts": [Decimal("50")]}, "preset_amounts"),
    ("presets-not-a-list", {"preset_amounts": "50.00"}, "preset_amounts"),
    ("window-minus-1", {"reuse_window_seconds": -1}, "reuse_window_seconds"),
    ("window-301", {"reuse_window_seconds": 301}, "reuse_window_seconds"),
    ("window-boolean", {"reuse_window_seconds": True}, "reuse_window_seconds"),
    ("timeout-2399", {"pending_timeout_seconds": 2399}, "pending_timeout_seconds"),
    ("timeout-86401", {"pending_timeout_seconds": 86_401}, "pending_timeout_seconds"),
]


@pytest.mark.parametrize(
    ("overrides", "field"),
    [(case, field) for _, case, field in INVALID_CASES],
    ids=[name for name, _, _ in INVALID_CASES],
)
def test_each_rule_names_its_field(overrides: dict[str, Any], field: str) -> None:
    with pytest.raises(PaymentSettingsInvalid) as raised:
        validate_payment_settings(values(**overrides))

    error = raised.value
    assert (error.code, error.http_status) == ("PAYMENT_SETTINGS_INVALID", 422)
    assert error.field == field
    assert field in error.message


ALL_WRONG: dict[str, Any] = {
    "min_amount": "1.00",
    "max_amount": "0.50",
    "recovery_buffer": "0.00",
    "preset_amounts": [],
    "reuse_window_seconds": 301,
    "pending_timeout_seconds": 100,
}
ALL_RIGHT: dict[str, Any] = {
    "min_amount": "10.00",
    "max_amount": "1000.00",
    "recovery_buffer": "10.00",
    "preset_amounts": ["50.00"],
    "reuse_window_seconds": 60,
    "pending_timeout_seconds": 3600,
}


@pytest.mark.parametrize("fixed", range(len(FIELD_ORDER)))
def test_the_first_failing_field_in_the_design_order_is_named(fixed: int) -> None:
    """前 `fixed` 个字段合规、其余全部不合规：点名的是第 `fixed` 个（设计的字段顺序）。"""
    given = {
        name: ALL_RIGHT[name] if position < fixed else ALL_WRONG[name]
        for position, name in enumerate(FIELD_ORDER)
    }

    with pytest.raises(PaymentSettingsInvalid) as raised:
        validate_payment_settings(PaymentSettingsValues(**given))

    assert raised.value.field == FIELD_ORDER[fixed]


# --- 读 -------------------------------------------------------------------------------


def test_the_seed_row_reads_as_two_decimal_amounts(world: World) -> None:
    settings = current(world)

    assert settings == PaymentSettings(
        min_amount=Decimal("10.00"),
        max_amount=None,
        recovery_buffer=Decimal("10.00"),
        preset_amounts=tuple(Decimal(text) for text in SEED_PRESETS),
        reuse_window_seconds=60,
        pending_timeout_seconds=3600,
        version=1,
        updated_at=SEED_SETTINGS_AT,
        updated_by_user_id=None,
    )
    for amount in (settings.min_amount, settings.recovery_buffer, *settings.preset_amounts):
        assert amount.as_tuple().exponent == -2
    assert SEED_PAYMENT_SETTINGS["preset_amounts"] == SEED_PRESETS


def test_a_missing_row_is_payments_misconfigured(world: World) -> None:
    remove_payment_settings(world.factory)

    with pytest.raises(PaymentSettingsMissing) as raised:
        current(world)

    assert (raised.value.code, raised.value.http_status) == ("PAYMENTS_MISCONFIGURED", 503)


# --- 改设置写审计与版本 --------------------------------------------------------------------


def test_an_update_replaces_every_field_bumps_the_version_and_writes_the_audit(
    world: World,
) -> None:
    result = update(world, NEW_VALUES, 1)

    expected = PaymentSettings(
        min_amount=Decimal("20.00"),
        max_amount=Decimal("5000.50"),
        recovery_buffer=Decimal("0.01"),
        preset_amounts=(Decimal("20.00"), Decimal("100.50"), Decimal("5000.50")),
        reuse_window_seconds=0,
        pending_timeout_seconds=2400,
        version=2,
        updated_at=STAMP,
        updated_by_user_id=world.admin_id,
    )
    assert result == expected
    assert current(world) == expected
    # JSON 里存定点字符串，不存 JSON 数字。
    assert stored_presets(world) == ["20.00", "100.50", "5000.50"]

    [audit] = settings_audits(world)
    assert audit.actor_user_id == world.admin_id
    assert audit.actor_role == UserRole.ADMIN.value
    assert (audit.entity_type, audit.entity_id) == (ENTITY_PAYMENT_SETTINGS, "1")
    assert (audit.ip_address, audit.user_agent) == (CONTEXT.ip_address, CONTEXT.user_agent)
    assert audit.created_at == STAMP
    assert audit_state(audit.before_state) == SEED_AUDIT
    assert audit_state(audit.after_state) == {
        "min_amount": "20.00",
        "max_amount": "5000.50",
        "recovery_buffer": "0.01",
        "preset_amounts": ["20.00", "100.50", "5000.50"],
        "reuse_window_seconds": 0,
        "pending_timeout_seconds": 2400,
        "version": 2,
    }


def test_the_audit_entity_is_returned_as_is_by_the_audit_query() -> None:
    assert ENTITY_PAYMENT_SETTINGS in PUBLIC_ENTITY_TYPES


def test_an_update_with_the_same_values_still_bumps_the_version_and_audits(world: World) -> None:
    """设计 §2 第 3 步：管理员的确认动作本身要留痕。"""
    first = update(world, SEED_VALUES, 1)
    second = update(world, SEED_VALUES, 2)

    assert (first.version, second.version) == (2, 3)
    assert current(world).version == 3
    assert current(world).min_amount == Decimal("10.00")
    audits = settings_audits(world)
    assert len(audits) == 2
    for audit, version in zip(audits, (1, 2), strict=True):
        before, after = audit_state(audit.before_state), audit_state(audit.after_state)
        assert before == {**SEED_AUDIT, "version": version}
        assert after == {**SEED_AUDIT, "version": version + 1}


@pytest.mark.parametrize("stale", [0, 2, 99])
def test_a_version_conflict_is_409_and_changes_nothing(world: World, stale: int) -> None:
    before = current(world)
    audits = audit_count(world)

    with pytest.raises(PaymentSettingsVersionConflict) as raised:
        update(world, NEW_VALUES, stale)

    assert (raised.value.code, raised.value.http_status) == (
        "PAYMENT_SETTINGS_VERSION_CONFLICT",
        409,
    )
    assert current(world) == before
    assert audit_count(world) == audits


def test_the_older_of_two_edits_is_refused(world: World) -> None:
    """两个管理员都读到版本 1：先提交的成功，后提交的 409，先提交的修改保留。"""
    update(world, NEW_VALUES, 1)

    with pytest.raises(PaymentSettingsVersionConflict):
        update(world, SEED_VALUES, 1)

    assert current(world).min_amount == Decimal("20.00")
    assert current(world).version == 2
    assert len(settings_audits(world)) == 1


def test_an_update_of_a_missing_row_is_misconfigured_not_a_conflict(world: World) -> None:
    remove_payment_settings(world.factory)

    with pytest.raises(PaymentSettingsMissing) as raised:
        update(world, NEW_VALUES, 1)

    assert raised.value.code == "PAYMENTS_MISCONFIGURED"
    assert settings_audits(world) == []


def test_invalid_values_change_nothing(world: World) -> None:
    before = current(world)
    audits = audit_count(world)

    with pytest.raises(PaymentSettingsInvalid):
        update(world, values(recovery_buffer="0.00"), 1)

    assert current(world) == before
    assert audit_count(world) == audits


# --- 并发改设置（真 MySQL） -------------------------------------------------------------


def test_two_concurrent_updates_with_the_same_version_one_wins(
    mysql_world_with_settings: World,
) -> None:
    world = mysql_world_with_settings
    actor = admin(world)
    start = threading.Barrier(2)
    candidates = [NEW_VALUES, values(min_amount="30.00", preset_amounts=["30.00"])]

    def attempt(index: int) -> PaymentSettings | PaymentSettingsVersionConflict:
        start.wait(timeout=30)
        try:
            return update_payment_settings(
                world.factory,
                actor=actor,
                values=candidates[index],
                expected_version=1,
                context=CONTEXT,
                now=NOW,
            )
        except PaymentSettingsVersionConflict as error:
            return error

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(attempt, range(2)))

    won = [o for o in outcomes if isinstance(o, PaymentSettings)]
    lost = [o for o in outcomes if isinstance(o, PaymentSettingsVersionConflict)]
    assert (len(won), len(lost)) == (1, 1)
    assert current(world) == won[0]
    assert won[0].version == 2
    [audit] = settings_audits(world)
    assert audit_state(audit.after_state)["min_amount"] == format(won[0].min_amount, "f")


# --- 最小恢复额 --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("balance", "expected"),
    [
        ("5.00", None),
        ("0", "10.00"),
        ("-0.00000001", "10.01"),
        ("-12.345", "22.35"),
        ("-50.00", "60.00"),
    ],
)
def test_the_recovery_minimum_is_debt_plus_buffer_rounded_up(
    balance: str, expected: str | None
) -> None:
    """缓冲 10.00、最低额 10.00。"""
    settings = rules()

    found = recovery_minimum(Decimal(balance), BillingMode.PREPAID, settings)

    if expected is None:
        assert found is None
        assert effective_minimum(Decimal(balance), BillingMode.PREPAID, settings) == Decimal(
            "10.00"
        )
        return
    assert isinstance(found, Decimal)
    assert found == Decimal(expected)
    assert found.as_tuple().exponent == -2
    assert effective_minimum(Decimal(balance), BillingMode.PREPAID, settings) == Decimal(expected)


def test_a_one_cent_buffer_leaves_one_cent_after_the_top_up() -> None:
    """缓冲 0.01、余额 −50.00：恢复额 50.01，按它充值后余额 0.01（> 0，spec §42）。"""
    settings = rules(recovery_buffer="0.01")
    balance = Decimal("-50.00")

    found = recovery_minimum(balance, BillingMode.PREPAID, settings)
    minimum = effective_minimum(balance, BillingMode.PREPAID, settings)

    assert found == minimum == Decimal("50.01")
    assert balance + minimum == Decimal("0.01")
    assert balance + minimum > 0


def test_the_minimum_amount_wins_when_it_is_larger() -> None:
    settings = rules(min_amount="100.00")

    assert recovery_minimum(Decimal("-5"), BillingMode.PREPAID, settings) == Decimal("15.00")
    assert effective_minimum(Decimal("-5"), BillingMode.PREPAID, settings) == Decimal("100.00")


def test_a_tenant_that_is_not_prepaid_has_no_recovery_minimum() -> None:
    settings = rules()

    for balance in ("-50.00", "0"):
        assert (
            recovery_minimum(Decimal(balance), BillingMode.INTERNAL_METERED_ONLY, settings) is None
        )
        assert effective_minimum(
            Decimal(balance), BillingMode.INTERNAL_METERED_ONLY, settings
        ) == Decimal("10.00")


def test_a_recovery_minimum_above_the_maximum_exceeds_the_cap() -> None:
    """余额 −200、缓冲 10 → 恢复额 210 > 最高额 100：`RECOVERY_EXCEEDS_MAXIMUM`。"""
    settings = rules(max_amount="100.00")
    minimum = effective_minimum(Decimal("-200.00"), BillingMode.PREPAID, settings)

    assert topup_cap(settings) == Decimal("100.00")
    assert minimum == Decimal("210.00")
    assert recovery_exceeds_cap(minimum, settings) is True


def test_without_a_maximum_the_cap_is_999999_99() -> None:
    """最高额为空而恢复额超过 999999.99：`RECOVERY_EXCEEDS_MAXIMUM`。"""
    settings = rules()
    minimum = effective_minimum(Decimal("-999990.00"), BillingMode.PREPAID, settings)

    assert topup_cap(settings) == Decimal("999999.99")
    assert minimum == Decimal("1000000.00")
    assert recovery_exceeds_cap(minimum, settings) is True


def test_a_minimum_equal_to_the_cap_does_not_exceed_it() -> None:
    settings = rules(max_amount="60.00")

    minimum = effective_minimum(Decimal("-50.00"), BillingMode.PREPAID, settings)

    assert minimum == Decimal("60.00")
    assert recovery_exceeds_cap(minimum, settings) is False
    assert recovery_exceeds_cap(Decimal("10.00"), rules()) is False


# --- 配置项已删除 -------------------------------------------------------------------------

REMOVED = {
    "payment_topup_min": "500.00",
    "payment_topup_max": "20.00",
    "payment_pending_timeout_seconds": "99999",
    "payment_reuse_window_seconds": "0",
}


def test_the_four_removed_settings_are_not_read(monkeypatch) -> None:
    for name, value in REMOVED.items():
        monkeypatch.setenv(f"BILLING_{name.upper()}", value)

    settings = Settings()

    for name in REMOVED:
        assert name not in Settings.model_fields
        assert not hasattr(settings, name)


def test_the_removed_environment_variables_do_not_change_a_topup(
    monkeypatch, payment_settings_seeder
) -> None:
    """旧的四个环境变量设成会拒绝这笔建单的值：建单照支付设置（种子值）进行。"""
    for name, value in REMOVED.items():
        monkeypatch.setenv(f"BILLING_{name.upper()}", value)
    worlds = _seeded(topup_sqlite_world(), payment_settings_seeder)
    world = next(worlds)
    try:
        world.publish_tax()
        tenant = world.customer()

        first = world.topup(tenant, "100.00")
        again = world.topup(tenant, "100.00", now=TOPUP_NOW + dt.timedelta(seconds=10))

        # 最低额 500 / 最高额 20 没有生效；超时是种子的 3600；窗口是种子的 60（复用了）。
        assert first.expires_at == TOPUP_NOW + dt.timedelta(seconds=3600)
        assert again == first
        assert len(world.payments()) == 1
    finally:
        for _ in worlds:
            pass

"""Provider cost reconciliation service (design gate #755 v1 §2「服务」, §7, AIH-TASK-244).

世界来自 tests/backend/provider_cost_world.py（夹具在 conftest.py 注册）。数值照设计 §7 的
标准世界：12 月范围内已处理事件 E1、E2，估算 2 条 / 10.50 USD / 46.80 MYR，未处理 1、
未投递 1、已丢弃 1；录入 11.70 USD 取 V44（4.4）→ 51.48。

⚠️ 每个用例都把 `FIRST_RECONCILIATION_MONTH` monkeypatch 为 2026-12（只有「早于首月」那一个
另设），不依赖常量的默认值：只改常量的 PR 不会让这里失败。

MySQL 那部分需要 `BILLING_TEST_DATABASE_URL`，没设时 skip —— **不要把 skipped 读成 passed**。
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
from collections.abc import Iterator
from decimal import Context, Decimal, localcontext
from typing import Any

import pytest
from provider_cost_world import (
    CONTEXT,
    P_CRM,
    P_PRE,
    PROVIDER_CODE,
    V44,
    V45,
    World,
    add_wallet_credit,
    build_fx_switch_world,
    build_myr_three_world,
    clean_wallets,
)
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.core.errors import AppError
from app.models.auth import AuditAction, AuditLog
from app.models.fx_rates import FxRateVersion
from app.models.provider_cost_reconciliations import ProviderCostReconciliation
from app.models.statements import MonthlyStatement
from app.models.usage import UsageEvent, UsageEventStatus
from app.models.wallet import Wallet, WalletTransaction
from app.schemas.customers import money_text
from app.services import provider_cost_reconciliation as service

# 12 月已经结束、1 月还没结束。
NOW = dt.datetime(2027, 1, 15, 0, 0, 0)
DECEMBER = "2026-12"
DOCUMENT = "test-statement-2026-12"
ZERO_UUID = "00000000-0000-0000-0000-000000000000"
# 设计 §7 之外另加的 12 月 AI 模块事件（26 字符，与世界里的占位同一写法）。
E7 = "00000000000000000000000E07"


@pytest.fixture(autouse=True)
def first_month(monkeypatch) -> None:
    monkeypatch.setattr(service, "FIRST_RECONCILIATION_MONTH", DECEMBER)


# --- 帮手 -----------------------------------------------------------------------------


def _entry(**overrides: Any) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "provider_code": PROVIDER_CODE,
        "billing_month": DECEMBER,
        "source_currency": "USD",
        "source_total": Decimal("11.70"),
        "source_document_reference": DOCUMENT,
        "supersedes_id": None,
        "reason": None,
    }
    fields.update(overrides)
    return fields


def preview(
    world: World, *, now: dt.datetime = NOW, **overrides: Any
) -> service.ReconciliationView:
    return service.preview(world.factory, now=now, **_entry(**overrides))


def confirm(
    world: World,
    *,
    now: dt.datetime = NOW,
    expected_event_count: int = 2,
    expected_estimated_source_cost: str = "10.50",
    **overrides: Any,
) -> service.ReconciliationView:
    return service.confirm(
        world.factory,
        actor=world.admin,
        context=CONTEXT,
        expected_event_count=expected_event_count,
        expected_estimated_source_cost=Decimal(expected_estimated_source_cost),
        now=now,
        **_entry(**overrides),
    )


def rows(world: World) -> list[ProviderCostReconciliation]:
    with world.factory() as session:
        statement = select(ProviderCostReconciliation).order_by(ProviderCostReconciliation.id)
        return list(session.execute(statement).scalars())


def audits(world: World) -> list[AuditLog]:
    with world.factory() as session:
        statement = (
            select(AuditLog)
            .where(AuditLog.action == AuditAction.PROVIDER_COST_RECONCILIATION_CREATE)
            .order_by(AuditLog.id)
        )
        return list(session.execute(statement).scalars())


def fx_public_id(world: World, name: str) -> str:
    with world.factory() as session:
        statement = select(FxRateVersion.public_id).where(FxRateVersion.id == world.fx[name].id)
        return session.execute(statement).scalar_one()


def add_ai_module_event(
    world: World,
    *,
    status: UsageEventStatus = UsageEventStatus.PROCESSED,
    source_cost: str | None = "1.00",
    source_currency: str = "USD",
) -> None:
    """One more December AI-module event of `T_int / P_crm`, with a `DELIVERED` outbox row."""
    occurred_at = dt.datetime(2026, 12, 20, 0, 0, 0)
    world.add_event(
        event_id=E7,
        project=P_CRM,
        occurred_at=occurred_at,
        status=status,
        source_cost=source_cost if status is UsageEventStatus.PROCESSED else None,
        source_currency=source_currency,
    )
    world.add_outbox(event_id=E7, project=P_CRM, status="DELIVERED", created_at=occurred_at)


def assert_refused(raised: pytest.ExceptionInfo[AppError], status: int, code: str) -> None:
    assert (raised.value.http_status, raised.value.code) == (status, code)


# --- 期间与纯函数 -----------------------------------------------------------------------


def test_the_period_is_the_utc_calendar_month() -> None:
    assert service.reconciliation_period("2026-12") == (
        dt.datetime(2026, 12, 1),
        dt.datetime(2027, 1, 1),
    )
    assert service.reconciliation_period("2027-02") == (
        dt.datetime(2027, 2, 1),
        dt.datetime(2027, 3, 1),
    )


@pytest.mark.parametrize(
    "label", ["2026-13", "2026-00", "1999-12", "3000-01", "2026-1", "2026/12", "abcd-ef", ""]
)
def test_a_malformed_or_out_of_range_month_is_a_value_error(label: str) -> None:
    with pytest.raises(ValueError):
        service.reconciliation_period(label)


def test_the_constants_are_the_designed_ones() -> None:
    assert service.SCOPE_AI_MODULE == "AI_MODULE"
    assert service.COVERAGE_PERIOD_AGGREGATE == "PERIOD_AGGREGATE"
    assert service.VARIANCE_THRESHOLD == Decimal("0.05")
    assert service.MONTHS_SHOWN == 24


def test_allocate_is_exact_and_unrounded() -> None:
    allocated = service.allocate(Decimal("27.00"), Decimal("51.48"), Decimal("46.80"))

    assert allocated == Decimal("29.70")
    # R ÷ E 不是有限小数时也不舍入到 8 位。
    third = service.allocate(Decimal("1"), Decimal("1"), Decimal("3"))
    assert third is not None and third.as_tuple().exponent < -8


def test_allocate_has_nothing_to_allocate_from_a_zero_estimate() -> None:
    assert service.allocate(Decimal("1"), Decimal("1"), Decimal("0")) is None


# --- 正常确认与换算 ------------------------------------------------------------------------


def test_a_confirmation_converts_at_the_month_end_rate(provider_cost_world: World) -> None:
    world = provider_cost_world
    v44 = fx_public_id(world, V44)

    view = confirm(world)

    assert view.reconciliation_id is not None
    assert (view.provider_code, view.scope, view.billing_month) == (
        PROVIDER_CODE,
        "AI_MODULE",
        DECEMBER,
    )
    assert (view.period_start, view.period_end) == (
        dt.datetime(2026, 12, 1),
        dt.datetime(2027, 1, 1),
    )
    assert view.fx_rate_version_id == v44
    assert view.fx_rate_applied == Decimal("4.4")
    assert view.fx_rate_moment == dt.datetime(2026, 12, 31, 23, 59, 59, 999999)
    assert money_text(view.source_total) == "11.70000000"
    assert money_text(view.reconciled_cost_myr) == "51.48000000"
    assert view.estimated_event_count == 2
    assert money_text(view.estimated_source_cost) == "10.50000000"
    assert money_text(view.estimated_cost_myr) == "46.80000000"
    assert money_text(view.source_variance) == "1.20000000"
    assert str(view.source_variance_rate) == "0.1142857142857142857142857143"
    assert money_text(view.myr_variance) == "4.68000000"
    assert view.myr_variance_rate == Decimal("0.1")
    assert view.exceeds_threshold is True
    assert (
        view.unprocessed_event_count,
        view.outbox_unsettled_count,
        view.outbox_discarded_count,
    ) == (1, 1, 1)
    assert view.coverage_method == "PERIOD_AGGREGATE"
    assert (view.supersedes_id, view.reason) == (None, None)
    assert view.source_document_reference == DOCUMENT
    assert view.is_current is True
    assert view.created_at == NOW
    assert view.created_by_email == world.admin.email

    [row] = rows(world)
    assert row.public_id == view.reconciliation_id
    assert (row.provider_id, row.scope, row.billing_month) == (
        world.provider_id,
        "AI_MODULE",
        DECEMBER,
    )
    assert (row.period_start, row.period_end) == (dt.datetime(2026, 12, 1), dt.datetime(2027, 1, 1))
    assert row.source_document_reference == DOCUMENT
    assert row.source_currency == "USD"
    # 逐位：DECIMAL(20,8) 与 DECIMAL(24,10) 读回来带满位小数（MySQL 与 SQLite 一样）。
    assert str(row.source_total) == "11.70000000"
    assert row.fx_rate_version_id == world.fx[V44].id
    assert str(row.fx_rate_applied) == "4.4000000000"
    assert str(row.reconciled_cost_myr) == "51.48000000"
    assert row.coverage_method == "PERIOD_AGGREGATE"
    assert (
        row.estimated_event_count,
        row.unprocessed_event_count,
        row.outbox_unsettled_count,
        row.outbox_discarded_count,
    ) == (2, 1, 1, 1)
    assert str(row.estimated_source_cost) == "10.50000000"
    assert str(row.estimated_cost_myr) == "46.80000000"
    assert (row.supersedes_id, row.reason) == (None, None)
    assert row.created_by == world.admin_id
    assert row.created_at == NOW


def test_a_confirmation_writes_exactly_one_audit(provider_cost_world: World) -> None:
    world = provider_cost_world

    view = confirm(world)

    [audit] = audits(world)
    assert audit.actor_user_id == world.admin_id
    assert audit.entity_type == "provider_cost_reconciliation"
    assert audit.entity_id == view.reconciliation_id
    assert audit.before_state is None
    assert audit.reason is None
    assert audit.created_at == NOW
    assert audit.after_state is not None
    assert json.loads(audit.after_state) == {
        "public_id": view.reconciliation_id,
        "provider_code": PROVIDER_CODE,
        "scope": "AI_MODULE",
        "billing_month": DECEMBER,
        "source_currency": "USD",
        "source_total": "11.70000000",
        "fx_rate_version_id": fx_public_id(world, V44),
        "fx_rate_applied": "4.4",
        "reconciled_cost_myr": "51.48000000",
        "estimated_event_count": 2,
        "estimated_source_cost": "10.50000000",
        "estimated_cost_myr": "46.80000000",
        "supersedes_id": None,
    }


def test_the_rate_is_the_one_in_effect_at_the_last_moment_of_the_month(
    provider_cost_db: sessionmaker[Session],
) -> None:
    """`fx_switch`：V45 到 2027-01-01 为止，月末最后一刻仍是 V45。"""
    world = build_fx_switch_world(provider_cost_db)

    view = confirm(world)

    assert view.fx_rate_version_id == fx_public_id(world, V45)
    assert view.fx_rate_applied == Decimal("4.5")
    assert money_text(view.reconciled_cost_myr) == "52.65000000"
    [row] = rows(world)
    assert row.fx_rate_version_id == world.fx[V45].id
    assert str(row.reconciled_cost_myr) == "52.65000000"


def test_myr_entered_against_usd_events_is_a_currency_mismatch(provider_cost_world: World) -> None:
    world = provider_cost_world

    with pytest.raises(AppError) as raised:
        confirm(world, source_currency="MYR", source_total=Decimal("51.48"))

    assert_refused(raised, 422, "SOURCE_CURRENCY_MISMATCH")
    assert rows(world) == []
    assert audits(world) == []


def test_myr_entered_against_myr_events_needs_no_rate(
    provider_cost_db: sessionmaker[Session],
) -> None:
    world = build_myr_three_world(provider_cost_db)

    view = confirm(
        world,
        source_currency="MYR",
        source_total=Decimal("1.00"),
        expected_event_count=3,
        expected_estimated_source_cost="3.00",
    )

    assert money_text(view.reconciled_cost_myr) == "1.00000000"
    assert (view.fx_rate_version_id, view.fx_rate_applied, view.fx_rate_moment) == (
        None,
        None,
        None,
    )
    assert money_text(view.estimated_source_cost) == "3.00000000"
    assert money_text(view.estimated_cost_myr) == "3.00000000"
    [row] = rows(world)
    assert (row.fx_rate_version_id, row.fx_rate_applied) == (None, None)
    assert str(row.reconciled_cost_myr) == "1.00000000"
    [audit] = audits(world)
    assert audit.after_state is not None
    after = json.loads(audit.after_state)
    assert (after["fx_rate_version_id"], after["fx_rate_applied"]) == (None, None)
    assert after["reconciled_cost_myr"] == "1.00000000"


def test_the_amounts_are_rounded_once_and_the_rates_are_exact_decimals(
    provider_cost_world: World,
) -> None:
    """0.00000001 × 4.4 = 0.000000044 → 0.00000004（只舍入一次，ROUND_HALF_UP）。"""
    world = provider_cost_world
    source_total = Decimal("0.00000001")

    view = preview(world, source_total=source_total)

    assert money_text(view.reconciled_cost_myr) == "0.00000004"
    estimated_source_cost = Decimal("10.50000000")
    estimated_cost_myr = Decimal("46.80000000")
    source_variance = source_total - estimated_source_cost
    myr_variance = Decimal("0.00000004") - estimated_cost_myr
    assert str(view.source_variance) == str(source_variance) == "-10.49999999"
    assert str(view.myr_variance) == str(myr_variance) == "-46.79999996"
    with localcontext(Context(prec=28)):
        assert str(view.source_variance_rate) == str(source_variance / estimated_source_cost)
        assert str(view.myr_variance_rate) == str(myr_variance / estimated_cost_myr)
    assert view.exceeds_threshold is True


# --- 检查顺序与理由 -----------------------------------------------------------------------


def test_the_reason_is_checked_before_the_month(provider_cost_world: World) -> None:
    """① 不带 supersedes_id 却带理由，且月份未结束。"""
    with pytest.raises(AppError) as raised:
        confirm(provider_cost_world, billing_month="2027-01", reason="A correction")

    assert_refused(raised, 422, "RECONCILIATION_REASON_INVALID")


def test_the_month_is_checked_before_the_provider(provider_cost_world: World) -> None:
    """② 月份未结束，且供应商不存在：月份先于 404。"""
    with pytest.raises(AppError) as raised:
        confirm(provider_cost_world, billing_month="2027-01", provider_code="openai")

    assert_refused(raised, 422, "RECONCILIATION_MONTH_INVALID")


def test_a_month_before_the_first_one_is_invalid(provider_cost_world: World, monkeypatch) -> None:
    """③ 早于首月：本用例把首月设为 2027-01，12 月本来合法。"""
    monkeypatch.setattr(service, "FIRST_RECONCILIATION_MONTH", "2027-01")

    with pytest.raises(AppError) as raised:
        confirm(provider_cost_world, now=dt.datetime(2027, 2, 2, 0, 0, 0))

    assert_refused(raised, 422, "RECONCILIATION_MONTH_INVALID")


def test_a_currency_without_a_published_rate_is_unavailable(provider_cost_world: World) -> None:
    """④ 标准世界录 12 月 EUR 11.70（没有 EUR 版本）。"""
    with pytest.raises(AppError) as raised:
        confirm(provider_cost_world, source_currency="EUR")

    assert_refused(raised, 422, "FX_RATE_UNAVAILABLE")
    assert rows(provider_cost_world) == []


def test_a_correction_of_another_month_is_a_mismatch(provider_cost_world: World) -> None:
    """⑤ 确认 12 月 a 后，2027-02-02 请求 2027-01 并取代 a。"""
    world = provider_cost_world
    a = confirm(world)

    with pytest.raises(AppError) as raised:
        confirm(
            world,
            now=dt.datetime(2027, 2, 2, 0, 0, 0),
            billing_month="2027-01",
            supersedes_id=a.reconciliation_id,
            reason="Wrong month",
            expected_event_count=1,
            expected_estimated_source_cost="2.00",
        )

    assert_refused(raised, 422, "RECONCILIATION_SUPERSEDES_MISMATCH")
    assert len(rows(world)) == 1
    assert len(audits(world)) == 1


@pytest.mark.parametrize(
    "error_code, http_status, overrides",
    [
        ("RECONCILIATION_MONTH_INVALID", 422, {"billing_month": "2026-13"}),
        ("RECONCILIATION_MONTH_INVALID", 422, {"billing_month": "2026-12-01"}),
        ("AI_PROVIDER_NOT_FOUND", 404, {"provider_code": "openai"}),
        (
            "PROVIDER_COST_RECONCILIATION_NOT_FOUND",
            404,
            {"supersedes_id": ZERO_UUID, "reason": "No such record"},
        ),
        (
            "PROVIDER_COST_RECONCILIATION_NOT_FOUND",
            404,
            {"supersedes_id": "not-a-uuid", "reason": "No such record"},
        ),
    ],
    ids=["bad-month", "long-month", "unknown-provider", "absent-uuid", "not-a-uuid"],
)
def test_the_other_refusals_of_the_check_order(
    provider_cost_world: World, error_code: str, http_status: int, overrides: dict[str, Any]
) -> None:
    for attempt in (preview, confirm):
        with pytest.raises(AppError) as raised:
            attempt(provider_cost_world, **overrides)
        assert_refused(raised, http_status, error_code)
    assert rows(provider_cost_world) == []


@pytest.mark.parametrize(
    "supersedes_id, reason",
    [(ZERO_UUID, None), (ZERO_UUID, "   "), (ZERO_UUID, "x" * 256)],
    ids=["missing", "blank", "too-long"],
)
def test_an_invalid_reason_is_refused_by_preview_and_confirm(
    provider_cost_world: World, supersedes_id: str, reason: str | None
) -> None:
    for attempt in (preview, confirm):
        with pytest.raises(AppError) as raised:
            attempt(provider_cost_world, supersedes_id=supersedes_id, reason=reason)
        assert_refused(raised, 422, "RECONCILIATION_REASON_INVALID")
    assert rows(provider_cost_world) == []
    assert audits(provider_cost_world) == []


def test_a_reason_of_255_characters_after_trimming_is_accepted(provider_cost_world: World) -> None:
    world = provider_cost_world
    a = confirm(world)

    view = preview(world, supersedes_id=a.reconciliation_id, reason=f"  {'x' * 255}  ")

    assert view.reason == "x" * 255


def test_a_converted_amount_that_does_not_fit_is_out_of_range(provider_cost_world: World) -> None:
    """第 5a 步：10^12 ÷ 4.4 向上取整到 8 位，乘回来 ≥ 10^12。"""
    world = provider_cost_world
    source_total = Decimal("227272727272.72727273")

    for attempt in (preview, confirm):
        with pytest.raises(AppError) as raised:
            attempt(world, source_total=source_total)
        assert_refused(raised, 422, "RECONCILIATION_AMOUNT_OUT_OF_RANGE")
    assert rows(world) == []


# --- 预览、估算变化、已有根、更正 ----------------------------------------------------------


def test_a_preview_writes_nothing_and_matches_the_confirmation(provider_cost_world: World) -> None:
    world = provider_cost_world
    a = confirm(world)
    entry = {
        "source_total": Decimal("12.00"),
        "source_document_reference": "  test-statement-2026-12 revised  ",
        "supersedes_id": a.reconciliation_id,
        "reason": "  Console figure revised  ",
    }

    previewed = preview(world, **entry)

    assert len(rows(world)) == 1
    assert len(audits(world)) == 1
    assert (previewed.reconciliation_id, previewed.is_current) == (None, None)
    assert (previewed.created_at, previewed.created_by_email) == (None, None)
    assert previewed.source_document_reference == "test-statement-2026-12 revised"
    assert previewed.supersedes_id == a.reconciliation_id
    assert previewed.reason == "Console figure revised"

    confirmed = confirm(world, **entry)

    assert previewed == dataclasses.replace(
        confirmed,
        reconciliation_id=None,
        is_current=None,
        created_at=None,
        created_by_email=None,
    )


def test_a_changed_estimate_since_the_preview_is_a_conflict(provider_cost_world: World) -> None:
    world = provider_cost_world
    previewed = preview(world)
    add_ai_module_event(world)

    with pytest.raises(AppError) as raised:
        confirm(
            world,
            expected_event_count=previewed.estimated_event_count,
            expected_estimated_source_cost=str(previewed.estimated_source_cost),
        )

    assert_refused(raised, 409, "RECONCILIATION_ESTIMATE_CHANGED")
    assert rows(world) == []
    assert audits(world) == []


def test_a_second_root_for_the_month_is_refused(provider_cost_world: World) -> None:
    world = provider_cost_world
    confirm(world)

    with pytest.raises(AppError) as raised:
        confirm(world)

    assert_refused(raised, 409, "RECONCILIATION_EXISTS")
    assert len(rows(world)) == 1
    assert len(audits(world)) == 1


def test_a_correction_becomes_current_and_a_superseded_row_cannot_be_corrected_again(
    provider_cost_world: World,
) -> None:
    world = provider_cost_world
    a = confirm(world)

    b = confirm(
        world,
        source_total=Decimal("12.00"),
        supersedes_id=a.reconciliation_id,
        reason="Console figure revised",
    )

    assert (b.supersedes_id, b.reason, b.is_current) == (
        a.reconciliation_id,
        "Console figure revised",
        True,
    )
    assert money_text(b.reconciled_cost_myr) == "52.80000000"
    detail = service.get_detail(world.factory, str(b.reconciliation_id), now=NOW)
    assert [(link.reconciliation_id, link.is_current) for link in detail.chain] == [
        (a.reconciliation_id, False),
        (b.reconciliation_id, True),
    ]
    [_, audit] = audits(world)
    assert audit.entity_id == b.reconciliation_id
    assert audit.reason == "Console figure revised"
    assert audit.before_state is not None and audit.after_state is not None
    assert json.loads(audit.before_state) == {
        "public_id": a.reconciliation_id,
        "source_currency": "USD",
        "source_total": "11.70000000",
        "reconciled_cost_myr": "51.48000000",
    }
    after = json.loads(audit.after_state)
    assert (after["supersedes_id"], after["source_total"], after["reconciled_cost_myr"]) == (
        a.reconciliation_id,
        "12.00000000",
        "52.80000000",
    )

    with pytest.raises(AppError) as raised:
        confirm(world, supersedes_id=a.reconciliation_id, reason="Second correction of a")

    assert_refused(raised, 409, "RECONCILIATION_NOT_CURRENT")
    assert len(rows(world)) == 2
    assert len(audits(world)) == 2


def test_a_failure_while_auditing_leaves_neither_record_nor_audit(
    provider_cost_world: World, monkeypatch
) -> None:
    world = provider_cost_world

    def failing_audit(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("injected audit failure")

    monkeypatch.setattr(service, "record_audit", failing_audit)

    with pytest.raises(RuntimeError, match="injected audit failure"):
        confirm(world)

    assert rows(world) == []
    assert audits(world) == []


# --- 快照不变（只在 MySQL） -----------------------------------------------------------------


@pytest.fixture
def wallet_world(provider_cost_mysql_world: World) -> Iterator[World]:
    """The standard world on MySQL plus `T_pre`'s wallet with one manual credit."""
    world = provider_cost_mysql_world
    try:
        add_wallet_credit(world, project=P_PRE, amount="100.00")
        yield world
    finally:
        clean_wallets(world.factory.kw["bind"])


def _financial_snapshot(world: World) -> dict[str, Any]:
    with world.factory() as session:
        return {
            "usage_events": [
                tuple(row)
                for row in session.execute(
                    select(UsageEvent.__table__).order_by(UsageEvent.__table__.c.id)
                )
            ],
            "wallets": [
                tuple(row)
                for row in session.execute(
                    select(Wallet.tenant_id, Wallet.balance, Wallet.version).order_by(Wallet.id)
                )
            ],
            "ledger_rows": session.execute(
                select(func.count()).select_from(WalletTransaction)
            ).scalar_one(),
            "statement_rows": session.execute(
                select(func.count()).select_from(MonthlyStatement)
            ).scalar_one(),
            "statements": [
                tuple(row)
                for row in session.execute(
                    select(MonthlyStatement.__table__).order_by(MonthlyStatement.__table__.c.id)
                )
            ],
        }


def test_a_confirmation_leaves_events_wallets_ledger_and_statements_alone(
    wallet_world: World,
) -> None:
    world = wallet_world
    before = _financial_snapshot(world)
    assert before["ledger_rows"] >= 1
    assert [str(balance) for _, balance, _ in before["wallets"]] == ["100.00000000"]

    confirm(world)

    assert _financial_snapshot(world) == before


# --- 详情与总览 -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "event, changed, live",
    [
        ({}, True, (3, "11.50000000", "51.30000000", 0, 1)),
        ({"source_currency": "MYR"}, True, (3, "10.50000000", "47.80000000", 1, 1)),
        (
            {"status": UsageEventStatus.PRICING_ERROR},
            False,
            (2, "10.50000000", "46.80000000", 0, 2),
        ),
    ],
    ids=["processed", "other-currency", "pricing-error"],
)
def test_the_detail_tells_whether_the_estimate_changed_since_confirmation(
    provider_cost_world: World,
    event: dict[str, Any],
    changed: bool,
    live: tuple[int, str, str, int, int],
) -> None:
    world = provider_cost_world
    view = confirm(world)
    add_ai_module_event(world, **event)

    detail = service.get_detail(world.factory, str(view.reconciliation_id), now=NOW)

    assert detail.estimate_changed_since_confirmation is changed
    estimate = detail.live_estimate
    assert (
        estimate.estimated_event_count,
        money_text(estimate.estimated_source_cost),
        money_text(estimate.estimated_cost_myr),
        estimate.currency_mismatch_count,
        estimate.unprocessed_event_count,
    ) == live
    # 行上的快照与确认时一样，差额不重算。
    assert detail.reconciliation == view
    assert detail.chain == (view,)


def test_an_unknown_reconciliation_has_no_detail(provider_cost_world: World) -> None:
    for public_id in (ZERO_UUID, "not-a-uuid"):
        with pytest.raises(AppError) as raised:
            service.get_detail(provider_cost_world.factory, public_id, now=NOW)
        assert_refused(raised, 404, "PROVIDER_COST_RECONCILIATION_NOT_FOUND")


def test_the_months_overview_lists_ended_months_newest_first(provider_cost_world: World) -> None:
    world = provider_cost_world
    view = confirm(world)

    months = service.months_overview(
        world.factory, provider_code=PROVIDER_CODE, now=dt.datetime(2027, 2, 15, 0, 0, 0)
    )

    assert [(month.billing_month, month.status) for month in months] == [
        ("2027-01", "NOT_RECONCILED"),
        ("2026-12", "RECONCILED"),
    ]
    assert months[0].current is None
    assert (months[0].period_start, months[0].period_end) == (
        dt.datetime(2027, 1, 1),
        dt.datetime(2027, 2, 1),
    )
    assert months[1].current == view


def test_the_months_overview_is_empty_until_the_first_month_ends(
    provider_cost_world: World,
) -> None:
    world = provider_cost_world

    assert (
        service.months_overview(
            world.factory, provider_code=PROVIDER_CODE, now=dt.datetime(2026, 12, 20)
        )
        == []
    )
    # 恰好在月末那一刻，12 月已结束。
    [month] = service.months_overview(
        world.factory, provider_code=PROVIDER_CODE, now=dt.datetime(2027, 1, 1)
    )
    assert month.billing_month == DECEMBER


def test_the_months_overview_shows_at_most_24_months(provider_cost_world: World) -> None:
    months = service.months_overview(
        provider_cost_world.factory, provider_code=PROVIDER_CODE, now=dt.datetime(2029, 6, 1)
    )

    assert len(months) == 24
    assert (months[0].billing_month, months[-1].billing_month) == ("2029-05", "2027-06")


def test_the_months_overview_of_an_unknown_provider_is_not_found(
    provider_cost_world: World,
) -> None:
    with pytest.raises(AppError) as raised:
        service.months_overview(provider_cost_world.factory, provider_code="openai", now=NOW)

    assert_refused(raised, 404, "AI_PROVIDER_NOT_FOUND")

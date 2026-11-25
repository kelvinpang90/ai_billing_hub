"""Rebill preview and confirm (design gate #830 v12 §2「服务」, §4, §5, §7, AIH-TASK-255).

世界见 tests/backend/rebill_world.py（SQLite 与真 MySQL 各一遍；MySQL 那一半在
`BILLING_TEST_DATABASE_URL` 未设置时 skip —— **不要把 skipped 读成 passed**，CI 设了它）。
时间一律注入，不 sleep；uuid 是全零占位。同一幂等键的真并发、读视图与五个变异在 AIH-TASK-262。

金额都选二进制精确的值（rebill_world 的说明）：默认数量按 `V_BASE`、`R_GLOBAL` 计费 4.5，
`R_LOW`（×1.5）是 3.375，`V_HIGH`（单价翻倍）是 9。
"""

from __future__ import annotations

import datetime as dt
import json
from decimal import Decimal
from typing import Any

import pytest
from provider_cost_world import CONTEXT, zero_uuid
from rebill_world import (
    A1,
    A2,
    B1,
    DEFAULT_TOKENS,
    INTERNAL,
    R_GLOBAL,
    R_HUGE,
    R_LOW,
    V_BASE,
    V_HIGH,
    ZERO_TOKENS,
    A,
    B,
    EventRef,
    LineSpec,
    RebillWorld,
)
from sqlalchemy import func, inspect, select, update
from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.models.auth import AuditAction, AuditLog, DomainOutbox
from app.models.statements import MonthlyStatement
from app.models.tenancy import AccountStatus, BillingStatus, Tenant
from app.models.usage import UsageEvent
from app.models.usage_rebills import UsageRebill, UsageRebillLine
from app.models.wallet import ReferenceType, TransactionType, Wallet, WalletTransaction
from app.repositories import usage_rebills as rebill_rows
from app.repositories.wallet import EVENT_BILLING_STATUS_CHANGED, post_transaction
from app.services import usage_rebill
from app.services.statements import ledger_section, statement_period
from app.services.usage_rebill import (
    RebillPreview,
    RebillRequest,
    RebillResult,
    confirm_rebill,
    preview_rebill,
)

START = dt.datetime(2026, 11, 10, 0, 0, 0)
END = dt.datetime(2026, 11, 20, 0, 0, 0)
HOUR = dt.timedelta(hours=1)
FUNDED_AT = dt.datetime(2026, 10, 2, 0, 0, 0)
# 确认的时钟：带小数秒，验证截到整秒。
NOW = dt.datetime(2026, 11, 25, 3, 0, 0, 500000)
LATER = dt.datetime(2026, 11, 26, 3, 0, 0)
REASON = "  Fictional price correction  "

# 只有输出 token：0.25 USD → 1.125 MYR → ×2 = 2.25；只有输入 token：0.0625 USD → ×2 = 0.5625。
OUTPUT_ONLY = (0, 1000, 0, 0)
INPUT_ONLY = (1000, 0, 0, 0)


def _clock() -> dt.datetime:
    return NOW


def _later() -> dt.datetime:
    return LATER


def _key(number: int) -> str:
    return f"00000000-0000-4000-8000-{number:012d}"


def _criteria(**extra: Any) -> dict[str, Any]:
    return {"occurred_from": "2026-11-10T00:00:00Z", "occurred_to": "2026-11-20T00:00:00Z", **extra}


def _request(
    w: RebillWorld,
    customer: str = A,
    *,
    selection: dict[str, Any] | None = None,
    events: list[EventRef] | None = None,
    rule: str | None = None,
    version: str | None = None,
) -> RebillRequest:
    if selection is None:
        selection = (
            {"usage_event_ids": [item.public_id for item in events]}
            if events is not None
            else _criteria()
        )
    return RebillRequest(
        customer_id=w.customers[customer].public_id,
        selection=selection,
        corrective_pricing_rule_id=w.public_rule(rule) if rule else None,
        corrective_provider_price_version_id=w.public_version(version) if version else None,
    )


def _confirm(
    w: RebillWorld,
    request: RebillRequest,
    token: str,
    *,
    key: int = 1,
    reason: str = REASON,
    clock: Any = _clock,
) -> RebillResult:
    return confirm_rebill(
        w.factory,
        actor=w.admin,
        request=request,
        preview_token=token,
        reason=reason,
        idempotency_key=_key(key),
        context=CONTEXT,
        clock=clock,
    )


def _fund(w: RebillWorld, customer: str, amount: str) -> None:
    with w.factory() as session:
        post_transaction(
            session,
            tenant_id=w.customers[customer].tenant_id,
            transaction_type=TransactionType.ADJUSTMENT_CREDIT,
            amount=Decimal(amount),
            reference_type=ReferenceType.ADMIN_ADJUSTMENT,
            reference_id=zero_uuid(w.number()),
            now=FUNDED_AT,
            created_by=w.admin_id,
            description="Fictional funding",
        )
        session.commit()


def _columns(row: Any) -> dict[str, Any]:
    return {attr.key: getattr(row, attr.key) for attr in inspect(type(row)).column_attrs}


def _row(w: RebillWorld, model: Any, row_id: int) -> dict[str, Any]:
    with w.factory() as session:
        row = session.get(model, row_id)
        assert row is not None
        return _columns(row)


def _balance(w: RebillWorld, customer: str) -> Decimal:
    with w.factory() as session:
        return session.execute(
            select(Wallet.balance).where(Wallet.tenant_id == w.customers[customer].tenant_id)
        ).scalar_one()


def _tenant(w: RebillWorld, customer: str) -> Tenant:
    with w.factory() as session:
        row = session.get(Tenant, w.customers[customer].tenant_id)
        assert row is not None
        return row


def _count(w: RebillWorld, model: Any, *conditions: Any) -> int:
    with w.factory() as session:
        return int(
            session.execute(select(func.count()).select_from(model).where(*conditions)).scalar_one()
        )


def _rebill_ledger_rows(w: RebillWorld) -> list[WalletTransaction]:
    with w.factory() as session:
        return list(
            session.execute(
                select(WalletTransaction)
                .where(WalletTransaction.reference_type == ReferenceType.REBILL)
                .order_by(WalletTransaction.id)
            ).scalars()
        )


def _footprint(w: RebillWorld) -> tuple[int, ...]:
    """Every table a confirm writes: nothing may change when it fails."""
    return (
        _count(w, WalletTransaction),
        _count(w, UsageRebill),
        _count(w, UsageRebillLine),
        _count(w, AuditLog),
        _count(w, DomainOutbox),
    )


def _assert_equations(w: RebillWorld, result: RebillResult) -> None:
    """设计 v9：数据库不建等式 CHECK，服务的每条写入路径都在这里核对。"""
    with w.factory() as session:
        rebill = session.execute(
            select(UsageRebill).where(UsageRebill.public_id == result.id)
        ).scalar_one()
        lines = list(
            session.execute(
                select(UsageRebillLine).where(UsageRebillLine.rebill_id == rebill.id)
            ).scalars()
        )
        ledger = session.get(WalletTransaction, rebill.wallet_transaction_id)
        assert ledger is not None
    assert rebill.difference == rebill.corrected_total - rebill.previous_total
    assert rebill.event_count == len(lines)
    assert rebill.previous_total == sum((line.previous_charge for line in lines), Decimal(0))
    assert rebill.corrected_total == sum((line.corrected_charge for line in lines), Decimal(0))
    for line in lines:
        assert line.difference == line.corrected_charge - line.previous_charge
        assert line.difference != 0
        assert line.created_at == rebill.created_at
    assert ledger.amount == -sum((line.difference for line in lines), Decimal(0))
    assert ledger.reference_type == ReferenceType.REBILL
    assert ledger.reference_id == rebill.public_id
    assert result.difference == rebill.difference
    assert result.ledger_amount == ledger.amount


def _error(excinfo: pytest.ExceptionInfo[AppError]) -> tuple[str, int]:
    return excinfo.value.code, excinfo.value.http_status


# --- 正常路径 ------------------------------------------------------------------------------


def test_credit_writes_one_ledger_row_lines_and_audit_and_leaves_history_alone(
    rebill_world: RebillWorld,
) -> None:
    w = rebill_world
    _fund(w, A, "100")
    events = [
        w.add_event(project=A1, occurred_at=START),
        w.add_event(project=A1, occurred_at=START + HOUR, tokens=OUTPUT_ONLY),
        w.add_event(project=A2, occurred_at=START + 2 * HOUR, tokens=INPUT_ONLY),
    ]
    zero = w.add_event(project=A1, occurred_at=START + 3 * HOUR, tokens=ZERO_TOKENS)
    before_events = {item.id: _row(w, UsageEvent, item.id) for item in [*events, zero]}
    before_ledger = {
        item.id: _row(w, WalletTransaction, item.wallet_transaction_id)
        for item in events
        if item.wallet_transaction_id is not None
    }
    balance_before = _balance(w, A)
    request = _request(w, rule=R_LOW)

    preview = preview_rebill(w.factory, request=request)

    assert [line.usage_event_id for line in preview.lines] == [item.public_id for item in events]
    assert [line.previous_charge for line in preview.lines] == [
        Decimal("4.5"),
        Decimal("2.25"),
        Decimal("0.5625"),
    ]
    assert [line.corrected_charge for line in preview.lines] == [
        Decimal("3.375"),
        Decimal("1.6875"),
        Decimal("0.421875"),
    ]
    # 计费额为 0 的事件按 R_LOW 仍是 0：不进明细，也不进合计（设计 v9）。
    assert preview.unchanged_count == 1
    assert preview.event_count == 3
    assert preview.previous_total == Decimal("7.3125")
    assert preview.corrected_total == Decimal("5.484375")
    assert preview.difference == Decimal("-1.828125")
    assert preview.ledger_type is TransactionType.REBILL_CREDIT
    assert preview.ledger_amount == Decimal("1.828125")
    assert preview.balance == balance_before
    assert preview.balance_after == balance_before + Decimal("1.828125")
    assert len(preview.preview_token) == 64
    first = preview.lines[0]
    assert first.project == w.projects[A1].public_id
    assert preview.lines[2].project == w.projects[A2].public_id
    assert (first.provider, first.model) == ("anthropic", "claude-test")
    assert first.previous_pricing_rule == w.public_rule(R_GLOBAL)
    assert first.corrected_pricing_rule == w.public_rule(R_LOW)
    assert first.previous_provider_price_version == w.public_version(V_BASE)
    assert first.corrected_provider_price_version == w.public_version(V_BASE)
    assert first.corrected_estimated_provider_cost_myr == Decimal("2.25")
    assert first.original_period == "2026-11"
    assert first.original_statement_id is None
    # 预览不写任何东西。
    assert _count(w, UsageRebill) == 0
    assert _count(w, AuditLog, AuditLog.action == AuditAction.REBILL) == 0

    result = _confirm(w, request, preview.preview_token)

    assert result.replayed is False
    assert result.customer_id == w.customers[A].public_id
    assert result.event_count == 3
    assert result.ledger_type is TransactionType.REBILL_CREDIT
    assert result.ledger_amount == Decimal("1.828125")
    assert result.reason == REASON.strip()
    assert result.created_at == NOW.replace(microsecond=0)
    assert result.corrective_pricing_rule_id == w.public_rule(R_LOW)
    assert result.corrective_provider_price_version_id is None
    assert result.created_by_email == w.admin.email
    assert result.lines == preview.lines
    _assert_equations(w, result)

    [ledger] = _rebill_ledger_rows(w)
    assert ledger.public_id == result.wallet_transaction_id
    assert ledger.transaction_type == TransactionType.REBILL_CREDIT
    assert ledger.reference_id == result.id
    assert ledger.description == "Rebill correction (3 usage events)"
    assert ledger.metadata_json == {"rebill": result.id, "events": 3}
    assert ledger.created_by == w.admin_id
    assert ledger.created_at == NOW.replace(microsecond=0)
    # INV-4：余额变化 = 账本金额。
    assert _balance(w, A) - balance_before == ledger.amount

    with w.factory() as session:
        rebill = session.execute(select(UsageRebill)).scalar_one()
        assert rebill.idempotency_key == _key(1)
        assert rebill.reason == REASON.strip()
        assert rebill.corrective_pricing_rule_id == w.rules[R_LOW]
        lines = list(
            session.execute(select(UsageRebillLine).order_by(UsageRebillLine.id)).scalars()
        )
        assert [line.usage_event_id for line in lines] == [item.id for item in events]
        assert [line.original_wallet_transaction_id for line in lines] == [
            item.wallet_transaction_id for item in events
        ]
        assert {line.previous_pricing_rule_id for line in lines} == {w.rules[R_GLOBAL]}
        assert {line.corrected_pricing_rule_id for line in lines} == {w.rules[R_LOW]}
        [audit] = session.execute(
            select(AuditLog).where(AuditLog.action == AuditAction.REBILL)
        ).scalars()
    assert audit.entity_type == "usage_rebill"
    assert audit.entity_id == result.id
    assert audit.actor_user_id == w.admin_id
    assert audit.reason == REASON.strip()
    assert audit.ip_address == CONTEXT.ip_address
    assert json.loads(audit.before_state or "") == {
        "previous_total": "7.31250000",
        "event_count": 3,
    }
    assert json.loads(audit.after_state or "") == {
        "corrected_total": "5.48437500",
        "difference": "-1.82812500",
        "wallet_transaction": ledger.public_id,
        "corrective_pricing_rule": w.public_rule(R_LOW),
        "corrective_provider_price_version": None,
        "selection": {
            "occurred_from": "2026-11-10T00:00:00.000000Z",
            "occurred_to": "2026-11-20T00:00:00.000000Z",
            "project_id": None,
            "provider": None,
            "model": None,
        },
    }
    assert "cost" not in (audit.after_state or "")

    # INV-5 / INV-6：事件行与原 AI_USAGE 行逐列不变。
    for item in [*events, zero]:
        assert _row(w, UsageEvent, item.id) == before_events[item.id]
    for row_id, columns in before_ledger.items():
        assert _row(w, WalletTransaction, row_id) == columns


def test_debit_takes_the_difference_from_the_balance(rebill_world: RebillWorld) -> None:
    w = rebill_world
    _fund(w, A, "100")
    event = w.add_event(project=A1, occurred_at=START)
    balance_before = _balance(w, A)
    request = _request(w, events=[event], version=V_HIGH)

    preview = preview_rebill(w.factory, request=request)
    assert preview.ledger_type is TransactionType.REBILL_DEBIT
    assert preview.ledger_amount == Decimal("-4.5")
    assert preview.balance_after == balance_before - Decimal("4.5")
    [line] = preview.lines
    assert line.corrected_provider_price_version == w.public_version(V_HIGH)
    # 更正价格版本算出的估算成本：只在管理端可见。
    assert line.corrected_estimated_provider_cost_myr == Decimal("4.5")

    result = _confirm(w, request, preview.preview_token)

    assert result.ledger_type is TransactionType.REBILL_DEBIT
    assert result.ledger_amount == Decimal("-4.5")
    assert result.corrective_provider_price_version_id == w.public_version(V_HIGH)
    _assert_equations(w, result)
    assert _balance(w, A) == balance_before - Decimal("4.5")


def test_two_corrections_keep_the_ledger_equal_to_the_effective_charge(
    rebill_world: RebillWorld,
) -> None:
    w = rebill_world
    _fund(w, A, "100")
    event = w.add_event(project=A1, occurred_at=START)

    lower = _request(w, events=[event], rule=R_LOW)
    first = _confirm(w, lower, preview_rebill(w.factory, request=lower).preview_token, key=1)
    # 「按当前有效版本重算」：refs(e) 已是 R_LOW，没有差额。
    again = _request(w, events=[event])
    unchanged = preview_rebill(w.factory, request=again)
    assert (unchanged.lines, unchanged.unchanged_count) == ((), 1)
    higher = _request(w, events=[event], version=V_HIGH)
    preview = preview_rebill(w.factory, request=higher)
    [line] = preview.lines
    # 第二次以第一次的 corrected_charge 为基准，规则沿用第一次的 R_LOW。
    assert line.previous_charge == Decimal("3.375")
    assert line.previous_pricing_rule == w.public_rule(R_LOW)
    assert line.corrected_charge == Decimal("6.75")
    second = _confirm(w, higher, preview.preview_token, key=2)
    _assert_equations(w, first)
    _assert_equations(w, second)

    with w.factory() as session:
        effective = rebill_rows.latest_lines(session, [event.id])[event.id].corrected_charge
        differences = list(
            session.execute(
                select(UsageRebillLine.difference).where(UsageRebillLine.usage_event_id == event.id)
            ).scalars()
        )
        usage = session.get(WalletTransaction, event.wallet_transaction_id)
        assert usage is not None
    assert event.billable_cost is not None
    assert effective == event.billable_cost + sum(differences, Decimal(0))
    shares = [row.amount for row in _rebill_ledger_rows(w)]
    # INV-11：每次 Rebill 只有这一个事件，整行就是它的份额。
    assert -(usage.amount + sum(shares, Decimal(0))) == effective == Decimal("6.75")


# --- 没有可入账的差额 ------------------------------------------------------------------------


def test_corrections_that_cancel_out_preview_fine_and_refuse_to_confirm(
    rebill_world: RebillWorld,
) -> None:
    w = rebill_world
    _fund(w, A, "100")
    up = w.add_event(project=A1, occurred_at=START)
    down = w.add_event(project=A1, occurred_at=START + HOUR)
    # 把两者的当前有效计费分别放到 4.75 与 4.25（版本不变），按当前有效版本重算回 4.5。
    # 分两次写：一次 Rebill 的合计差额不许为 0。
    w.add_rebill(A, [LineSpec(up, "4.75")], created_at=END)
    w.add_rebill(A, [LineSpec(down, "4.25")], created_at=END)
    request = _request(w, events=[up, down])

    preview = preview_rebill(w.factory, request=request)

    assert [line.difference for line in preview.lines] == [Decimal("-0.25"), Decimal("0.25")]
    assert preview.event_count == 2
    assert preview.difference == 0
    assert (preview.ledger_type, preview.ledger_amount) == (None, None)
    assert preview.balance_after == preview.balance

    before = _footprint(w)
    with pytest.raises(AppError) as excinfo:
        _confirm(w, request, preview.preview_token)
    assert _error(excinfo) == ("REBILL_NET_ZERO", 409)
    assert _footprint(w) == before


def test_nothing_to_rebill_previews_empty_and_refuses_to_confirm(rebill_world: RebillWorld) -> None:
    w = rebill_world
    event = w.add_event(project=A1, occurred_at=START)
    request = _request(w, events=[event])

    preview = preview_rebill(w.factory, request=request)
    assert (preview.lines, preview.unchanged_count, preview.event_count) == ((), 1, 0)
    assert (preview.ledger_type, preview.ledger_amount) == (None, None)
    assert preview.balance_after == preview.balance

    before = _footprint(w)
    with pytest.raises(AppError) as excinfo:
        _confirm(w, request, preview.preview_token)
    assert _error(excinfo) == ("NOTHING_TO_REBILL", 409)
    assert _footprint(w) == before


def test_criteria_matching_nothing_is_409_in_preview_and_confirm(
    rebill_world: RebillWorld,
) -> None:
    w = rebill_world
    request = _request(w, rule=R_LOW)
    with pytest.raises(AppError) as excinfo:
        preview_rebill(w.factory, request=request)
    assert _error(excinfo) == ("NOTHING_TO_REBILL", 409)
    with pytest.raises(AppError) as excinfo:
        _confirm(w, request, "0" * 64)
    assert _error(excinfo) == ("NOTHING_TO_REBILL", 409)


# --- 指纹 ------------------------------------------------------------------------------------


def test_another_rebill_of_the_same_event_makes_the_preview_stale(
    rebill_world: RebillWorld,
) -> None:
    w = rebill_world
    _fund(w, A, "100")
    event = w.add_event(project=A1, occurred_at=START)
    request = _request(w, rule=R_LOW)
    preview = preview_rebill(w.factory, request=request)

    other = _request(w, events=[event], version=V_HIGH)
    _confirm(w, other, preview_rebill(w.factory, request=other).preview_token, key=2)
    before = _footprint(w)

    with pytest.raises(AppError) as excinfo:
        _confirm(w, request, preview.preview_token, key=1)
    assert _error(excinfo) == ("REBILL_PREVIEW_STALE", 409)
    assert _footprint(w) == before


def test_changed_parameters_make_the_preview_stale(rebill_world: RebillWorld) -> None:
    w = rebill_world
    _fund(w, A, "100")
    w.add_event(project=A1, occurred_at=START)
    preview = preview_rebill(w.factory, request=_request(w, rule=R_LOW))
    before = _footprint(w)

    with pytest.raises(AppError) as excinfo:
        _confirm(w, _request(w, rule=R_LOW, version=V_HIGH), preview.preview_token)
    assert _error(excinfo) == ("REBILL_PREVIEW_STALE", 409)
    with pytest.raises(AppError) as excinfo:
        _confirm(
            w,
            _request(w, rule=R_LOW, selection=_criteria(project_id=w.projects[A1].public_id)),
            preview.preview_token,
        )
    assert _error(excinfo) == ("REBILL_PREVIEW_STALE", 409)
    assert _footprint(w) == before


def test_a_new_event_in_the_range_makes_the_preview_stale(rebill_world: RebillWorld) -> None:
    w = rebill_world
    _fund(w, A, "100")
    w.add_event(project=A1, occurred_at=START)
    request = _request(w, rule=R_LOW)
    preview = preview_rebill(w.factory, request=request)
    w.add_event(project=A1, occurred_at=START + HOUR)
    before = _footprint(w)

    with pytest.raises(AppError) as excinfo:
        _confirm(w, request, preview.preview_token)
    assert _error(excinfo) == ("REBILL_PREVIEW_STALE", 409)
    assert _footprint(w) == before


# --- 幂等 ------------------------------------------------------------------------------------


def test_same_key_same_request_replays_and_another_request_conflicts(
    rebill_world: RebillWorld,
) -> None:
    w = rebill_world
    _fund(w, A, "100")
    w.add_event(project=A1, occurred_at=START)
    request = _request(w, rule=R_LOW)
    preview = preview_rebill(w.factory, request=request)
    first = _confirm(w, request, preview.preview_token)

    # 原因只按去首尾空白后的值参与指纹。
    again = _confirm(w, request, preview.preview_token, reason=REASON.strip(), clock=_later)

    assert again.replayed is True
    assert again == RebillResult(**{**first.__dict__, "replayed": True})
    assert len(_rebill_ledger_rows(w)) == 1
    before = _footprint(w)
    with pytest.raises(AppError) as excinfo:
        _confirm(w, request, preview.preview_token, reason="A different reason")
    assert _error(excinfo) == ("REBILL_CONFLICT", 409)
    assert _footprint(w) == before


def test_one_key_used_by_two_customers_conflicts_after_the_unique_key_catches_it(
    rebill_world: RebillWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """设计 v8 审查建议：第 3 步没查到（打桩），插入时撞唯一约束，第 8 步重查后 409。"""
    w = rebill_world
    _fund(w, A, "100")
    _fund(w, B, "100")
    w.add_event(project=A1, occurred_at=START)
    w.add_event(project=B1, occurred_at=START)
    for_a = _request(w, A, rule=R_LOW)
    created = _confirm(w, for_a, preview_rebill(w.factory, request=for_a).preview_token)
    assert created.replayed is False
    for_b = _request(w, B, rule=R_LOW)
    token_b = preview_rebill(w.factory, request=for_b).preview_token

    original = rebill_rows.get_by_idempotency_key
    calls: list[str] = []

    def first_misses(session: Session, idempotency_key: str) -> UsageRebill | None:
        calls.append(idempotency_key)
        if len(calls) == 1:
            return None
        return original(session, idempotency_key)

    monkeypatch.setattr(rebill_rows, "get_by_idempotency_key", first_misses)
    before = _footprint(w)
    balance_b = _balance(w, B)

    with pytest.raises(AppError) as excinfo:
        _confirm(w, for_b, token_b)

    assert _error(excinfo) == ("REBILL_CONFLICT", 409)
    assert calls == [_key(1), _key(1)]
    assert _footprint(w) == before
    assert _balance(w, B) == balance_b


# --- 失败与恢复 ------------------------------------------------------------------------------


def test_a_failure_while_writing_lines_rolls_everything_back(
    rebill_world: RebillWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = rebill_world
    # 余额 4 − 4.5 = −0.5（停机）；补退 1.125 会让它复机：outbox 行也在回滚范围里。
    _fund(w, A, "4")
    w.add_event(project=A1, occurred_at=START)
    request = _request(w, rule=R_LOW)
    token = preview_rebill(w.factory, request=request).preview_token
    before = _footprint(w)
    balance_before = _balance(w, A)
    tenant_before = _columns(_tenant(w, A))

    def broken(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("injected failure")

    with monkeypatch.context() as patch:
        patch.setattr(usage_rebill, "_write_lines", broken)
        with pytest.raises(RuntimeError):
            _confirm(w, request, token)

    assert _footprint(w) == before
    assert _balance(w, A) == balance_before
    assert _columns(_tenant(w, A)) == tenant_before

    result = _confirm(w, request, token)
    assert result.replayed is False
    assert len(_rebill_ledger_rows(w)) == 1
    _assert_equations(w, result)
    assert _tenant(w, A).billing_status is BillingStatus.ACTIVE


def test_a_lost_response_is_replayed_even_after_the_state_moved_on(
    rebill_world: RebillWorld,
) -> None:
    w = rebill_world
    _fund(w, A, "100")
    event = w.add_event(project=A1, occurred_at=START)
    request = _request(w, events=[event], rule=R_LOW)
    token = preview_rebill(w.factory, request=request).preview_token
    # 提交了，响应丢了。
    lost = _confirm(w, request, token, key=1)
    # 之后另一次 Rebill 又改了同一个事件。
    other = _request(w, events=[event], version=V_HIGH)
    _confirm(w, other, preview_rebill(w.factory, request=other).preview_token, key=2)

    resent = _confirm(w, request, token, key=1, clock=_later)

    assert resent.replayed is True
    assert resent.id == lost.id
    assert resent.wallet_transaction_id == lost.wallet_transaction_id
    assert resent.created_at == lost.created_at
    assert [row.reference_id for row in _rebill_ledger_rows(w)].count(lost.id) == 1
    assert len(_rebill_ledger_rows(w)) == 2


# --- 停复机与关户 ----------------------------------------------------------------------------


def _status_events(w: RebillWorld, customer: str) -> list[dict[str, Any]]:
    with w.factory() as session:
        rows = session.execute(
            select(DomainOutbox.payload_json)
            .where(
                DomainOutbox.event_type == EVENT_BILLING_STATUS_CHANGED,
                DomainOutbox.aggregate_id == w.customers[customer].public_id,
            )
            .order_by(DomainOutbox.id)
        ).scalars()
        return [json.loads(row) for row in rows]


@pytest.mark.parametrize(("funding", "after"), [("9", "0"), ("6", "-3")])
def test_a_debit_to_zero_or_below_suspends_in_the_same_transaction(
    rebill_world: RebillWorld, funding: str, after: str
) -> None:
    w = rebill_world
    _fund(w, A, funding)
    event = w.add_event(project=A1, occurred_at=START)
    assert _tenant(w, A).billing_status is BillingStatus.ACTIVE
    version_before = _tenant(w, A).status_version
    events_before = len(_status_events(w, A))
    request = _request(w, events=[event], version=V_HIGH)

    result = _confirm(w, request, preview_rebill(w.factory, request=request).preview_token)

    tenant = _tenant(w, A)
    assert _balance(w, A) == Decimal(after)
    assert tenant.billing_status is BillingStatus.SUSPENDED
    assert tenant.status_version == version_before + 1
    published = _status_events(w, A)
    assert len(published) == events_before + 1
    assert published[-1]["billing_status"] == BillingStatus.SUSPENDED.value
    assert published[-1]["wallet_transaction"] == result.wallet_transaction_id
    assert published[-1]["status_version"] == tenant.status_version


def test_a_credit_above_zero_reactivates_in_the_same_transaction(
    rebill_world: RebillWorld,
) -> None:
    w = rebill_world
    _fund(w, A, "4")
    event = w.add_event(project=A1, occurred_at=START)
    assert _tenant(w, A).billing_status is BillingStatus.SUSPENDED
    version_before = _tenant(w, A).status_version
    request = _request(w, events=[event], rule=R_LOW)

    result = _confirm(w, request, preview_rebill(w.factory, request=request).preview_token)

    tenant = _tenant(w, A)
    assert _balance(w, A) == Decimal("0.625")
    assert tenant.billing_status is BillingStatus.ACTIVE
    assert tenant.status_version == version_before + 1
    published = _status_events(w, A)[-1]
    assert published["billing_status"] == BillingStatus.ACTIVE.value
    assert published["wallet_transaction"] == result.wallet_transaction_id


def test_a_closed_account_is_409_and_nothing_is_written(rebill_world: RebillWorld) -> None:
    w = rebill_world
    _fund(w, A, "100")
    w.add_event(project=A1, occurred_at=START)
    request = _request(w, rule=R_LOW)
    token = preview_rebill(w.factory, request=request).preview_token
    with w.factory() as session:
        session.execute(
            update(Tenant)
            .where(Tenant.id == w.customers[A].tenant_id)
            .values(account_status=AccountStatus.CLOSED)
        )
        session.commit()
    before = _footprint(w)
    balance_before = _balance(w, A)

    with pytest.raises(AppError) as excinfo:
        _confirm(w, request, token)

    assert _error(excinfo) == ("ACCOUNT_CLOSED", 409)
    assert _footprint(w) == before
    assert _balance(w, A) == balance_before


# --- 对账单 ----------------------------------------------------------------------------------


def test_a_finalized_statement_is_referenced_unchanged_and_the_rebill_lands_in_the_next(
    rebill_world: RebillWorld,
) -> None:
    w = rebill_world
    _fund(w, A, "100")
    october = w.add_event(project=A1, occurred_at=dt.datetime(2026, 10, 15, 0, 0, 0))
    statement_id = w.add_statement(A, "2026-10")
    statement_before = _row(w, MonthlyStatement, statement_id)
    request = _request(w, events=[october], rule=R_LOW)

    preview = preview_rebill(w.factory, request=request)
    [line] = preview.lines
    assert line.original_period == "2026-10"
    assert line.original_statement_id == statement_before["public_id"]

    result = _confirm(w, request, preview.preview_token)

    # INV-12：已定稿的原账期对账单逐列不变，明细指向它。
    assert _row(w, MonthlyStatement, statement_id) == statement_before
    with w.factory() as session:
        stored = session.execute(select(UsageRebillLine)).scalar_one()
        assert stored.original_statement_id == statement_id
        assert stored.original_period == "2026-10"
        tenant_id = w.customers[A].tenant_id
        wallet = session.execute(select(Wallet).where(Wallet.tenant_id == tenant_id)).scalar_one()
        # 11 月（Rebill 入账月）的账本段：用对账单生成用的同一个函数。
        section = ledger_section(
            session,
            tenant_id,
            statement_period("2026-11"),
            tenant_public_id=w.customers[A].public_id,
            high_sequence=wallet.version,
            balance=wallet.balance,
            previous=None,
        )
    assert result.ledger_amount == Decimal("1.125")
    assert section.prior_rebill == result.ledger_amount


# --- 错误 ------------------------------------------------------------------------------------


def test_an_internal_metering_customer_is_not_prepaid(rebill_world: RebillWorld) -> None:
    w = rebill_world
    request = _request(w, INTERNAL, rule=R_LOW)
    with pytest.raises(AppError) as excinfo:
        preview_rebill(w.factory, request=request)
    assert _error(excinfo) == ("REBILL_NOT_PREPAID", 409)
    with pytest.raises(AppError) as excinfo:
        _confirm(w, request, "0" * 64)
    assert _error(excinfo) == ("REBILL_NOT_PREPAID", 409)


def test_unknown_customer_rule_and_price_version_are_404(rebill_world: RebillWorld) -> None:
    w = rebill_world
    w.add_event(project=A1, occurred_at=START)
    unknown = zero_uuid(999_999)
    cases = [
        (RebillRequest(customer_id=unknown, selection=_criteria()), "CUSTOMER_NOT_FOUND"),
        (
            RebillRequest(
                customer_id=w.customers[A].public_id,
                selection=_criteria(),
                corrective_pricing_rule_id=unknown,
            ),
            "PRICING_RULE_NOT_FOUND",
        ),
        (
            RebillRequest(
                customer_id=w.customers[A].public_id,
                selection=_criteria(),
                corrective_provider_price_version_id=unknown,
            ),
            "PRICE_VERSION_NOT_FOUND",
        ),
    ]
    before = _footprint(w)
    for request, code in cases:
        with pytest.raises(AppError) as excinfo:
            preview_rebill(w.factory, request=request)
        assert _error(excinfo) == (code, 404)
        with pytest.raises(AppError) as excinfo:
            _confirm(w, request, "0" * 64)
        assert _error(excinfo) == (code, 404)
    assert _footprint(w) == before


def test_a_charge_that_overflows_is_amount_out_of_range(rebill_world: RebillWorld) -> None:
    w = rebill_world
    w.add_event(project=A1, occurred_at=START)
    request = _request(w, rule=R_HUGE)
    with pytest.raises(AppError) as excinfo:
        preview_rebill(w.factory, request=request)
    assert _error(excinfo) == ("AMOUNT_OUT_OF_RANGE", 422)
    before = _footprint(w)
    with pytest.raises(AppError) as excinfo:
        _confirm(w, request, "0" * 64)
    assert _error(excinfo) == ("AMOUNT_OUT_OF_RANGE", 422)
    assert _footprint(w) == before


def test_a_balance_that_would_overflow_is_amount_out_of_range(rebill_world: RebillWorld) -> None:
    w = rebill_world
    event = w.add_event(project=A1, occurred_at=START)
    # −4.5 + 500000000000 + 500000000004 = 999999999999.5；补退 1.125 后超出 DECIMAL(20,8)。
    _fund(w, A, "500000000000")
    _fund(w, A, "500000000004")
    request = _request(w, events=[event], rule=R_LOW)
    token = preview_rebill(w.factory, request=request).preview_token
    before = _footprint(w)

    with pytest.raises(AppError) as excinfo:
        _confirm(w, request, token)

    assert _error(excinfo) == ("AMOUNT_OUT_OF_RANGE", 422)
    assert _footprint(w) == before
    assert _balance(w, A) == Decimal("999999999999.5")


def test_too_many_matches_and_unrebillable_events_name_the_count_and_the_event(
    rebill_world: RebillWorld,
) -> None:
    w = rebill_world
    w.add_zero_events(project=A1, moments=[START + dt.timedelta(seconds=n) for n in range(1001)])
    with pytest.raises(AppError) as excinfo:
        preview_rebill(w.factory, request=_request(w))
    assert _error(excinfo) == ("REBILL_SELECTION_TOO_LARGE", 422)
    assert excinfo.value.message == "Selection matches 1001 usage events; at most 1000."

    foreign = w.add_event(project=B1, occurred_at=START)
    with pytest.raises(AppError) as excinfo:
        preview_rebill(w.factory, request=_request(w, events=[foreign]))
    assert _error(excinfo) == ("USAGE_EVENT_NOT_REBILLABLE", 409)
    assert foreign.public_id in excinfo.value.message

    with pytest.raises(AppError) as excinfo:
        preview_rebill(
            w.factory,
            request=_request(w, selection={"usage_event_ids": [zero_uuid(999_998)]}),
        )
    assert _error(excinfo) == ("USAGE_EVENT_NOT_FOUND", 404)

    mine = w.add_event(project=A1, occurred_at=END)
    with pytest.raises(AppError) as excinfo:
        preview_rebill(
            w.factory,
            request=_request(w, selection={"usage_event_ids": [mine.public_id] * 2}),
        )
    assert _error(excinfo) == ("VALIDATION_ERROR", 422)
    with pytest.raises(AppError) as excinfo:
        preview_rebill(
            w.factory,
            request=_request(
                w,
                selection={
                    "occurred_from": "2026-11-20T00:00:00Z",
                    "occurred_to": "2026-11-10T00:00:00Z",
                },
            ),
        )
    assert _error(excinfo) == ("VALIDATION_ERROR", 422)
    with pytest.raises(AppError) as excinfo:
        preview_rebill(
            w.factory,
            request=_request(w, selection=_criteria(project_id=w.projects[B1].public_id)),
        )
    assert _error(excinfo) == ("PROJECT_NOT_FOUND", 404)


def test_confirm_rejects_a_blank_reason_before_touching_the_database(
    rebill_world: RebillWorld,
) -> None:
    w = rebill_world
    request = _request(w, rule=R_LOW)
    for reason in ("", "   ", "\t\n"):
        with pytest.raises(AppError) as excinfo:
            _confirm(w, request, "0" * 64, reason=reason)
        assert _error(excinfo) == ("VALIDATION_ERROR", 422)


def test_the_preview_is_a_plain_value(rebill_world: RebillWorld) -> None:
    w = rebill_world
    w.add_event(project=A1, occurred_at=START, tokens=DEFAULT_TOKENS)
    preview = preview_rebill(w.factory, request=_request(w, rule=R_LOW))
    assert isinstance(preview, RebillPreview)
    # 同一输入两次预览得到同一个指纹。
    assert preview_rebill(w.factory, request=_request(w, rule=R_LOW)) == preview

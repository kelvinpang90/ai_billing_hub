"""Rebill pricing (design gate #830 v12, AIH-TASK-254).

设计 §2「口径」「适用性」「预览指纹」「金额与精度」与 §7。

用库的用例用 tests/backend/rebill_world.py 的世界（SQLite 与真 MySQL 各一遍；MySQL 那一半在
`BILLING_TEST_DATABASE_URL` 未设置时 skip —— **不要把 skipped 读成 passed**，CI 设了它）。
预览指纹与 selection 规范化是纯函数。时间一律注入，不 sleep；uuid 是全零占位。
"""

from __future__ import annotations

import ast
import datetime as dt
import hashlib
import pathlib
from collections.abc import Iterator
from contextlib import contextmanager
from decimal import Decimal
from typing import Any

import pytest
from provider_cost_world import zero_uuid
from rebill_world import (
    A1,
    A2,
    B1,
    R_DISCARDED,
    R_DRAFT,
    R_FIXED,
    R_FIXED_MISSING,
    R_GLOBAL,
    R_HUGE,
    R_LOW,
    R_OTHER_CUSTOMER,
    R_OTHER_MODEL,
    R_OTHER_PROJECT,
    R_RETIRED,
    V_BASE,
    V_DISCARDED,
    V_DRAFT,
    V_EMBED,
    V_HIGH,
    V_MYR,
    V_OTHER_MODEL,
    V_RETIRED,
    A,
    LineSpec,
    RebillWorld,
)
from sqlalchemy import event, select
from sqlalchemy.orm import Session, sessionmaker

from app.models.fx_rates import FxRateVersion
from app.models.pricing_rules import PricingRuleStatus, PricingStrategy
from app.models.provider_prices import PriceVersionStatus
from app.models.usage_rebills import UsageRebillLine
from app.repositories import pricing_rules as rules_repository
from app.repositories import provider_prices as prices_repository
from app.schemas.customers import money_text
from app.services.pricing_engine import (
    ChargeSnapshot,
    RuleTerms,
    UnitPrice,
    price_event,
    reprice_from_snapshot,
)
from app.services.rebill_pricing import (
    EffectiveState,
    RebillAmountOutOfRange,
    RebillPriceNotApplicable,
    RebillRuleNotApplicable,
    RepricingInputs,
    TokenEvent,
    canonical_json,
    check_corrections,
    corrected_charge,
    effective_state,
    normalize_selection,
    preview_token,
)
from app.services.usage_billing import _quantities

NOVEMBER = dt.datetime(2026, 11, 10, 3, 0, 0)
HOUR = dt.timedelta(hours=1)
# 默认数量按 V_BASE、汇率 4.5、R_GLOBAL ×2：0.2203125 USD → 0.99140625 MYR → 1.9828125。
DEFAULT_CHARGE = Decimal("1.98281250")
NEW_MODULES = (
    pathlib.Path("app/services/rebill_pricing.py"),
    pathlib.Path("app/repositories/usage_rebills.py"),
)


@contextmanager
def captured(factory: sessionmaker[Session]) -> Iterator[list[str]]:
    """Every statement sent on the world's engine while the block runs."""
    engine = factory.kw["bind"]
    seen: list[str] = []

    def listener(_conn, _cursor, statement, _parameters, _context, _executemany) -> None:
        seen.append(statement)

    event.listen(engine, "before_cursor_execute", listener)
    try:
        yield seen
    finally:
        event.remove(engine, "before_cursor_execute", listener)


def _snapshot(event_row: Any, rule_id: int, version_id: int) -> ChargeSnapshot:
    return ChargeSnapshot(
        usage_meter_type_id=event_row.usage_meter_type_id,
        provider_price_version_id=version_id,
        fx_rate_version_id=event_row.fx_rate_version_id,
        pricing_rule_id=rule_id,
    )


# --- 当前有效状态与原版本重算 ---------------------------------------------------------------


def test_recompute_without_corrections_is_bit_identical_to_reprice_from_snapshot(
    rebill_world: RebillWorld,
) -> None:
    w = rebill_world
    charged = w.add_event(project=A1, occurred_at=NOVEMBER)
    assert charged.billable_cost == DEFAULT_CHARGE

    with w.factory() as session:
        row = w.event(session, charged)
        state = effective_state(session, [row])[row.id]
        assert state == EffectiveState(
            eff=DEFAULT_CHARGE,
            pricing_rule_id=w.rules[R_GLOBAL],
            provider_price_version_id=w.versions[V_BASE],
            latest_line_id=None,
        )
        result = corrected_charge(session, row, state, None, None)
        expected = reprice_from_snapshot(
            session,
            ChargeSnapshot(
                usage_meter_type_id=row.usage_meter_type_id,
                provider_price_version_id=row.provider_price_version_id,
                fx_rate_version_id=row.fx_rate_version_id,
                pricing_rule_id=row.pricing_rule_id,
            ),
            _quantities(row),
        )
    # 逐位相同：repr 连 Decimal 的指数一起比。
    assert repr(result) == repr(expected)
    # 原版本重算：差额为 0。
    assert result.billable_cost - state.eff == 0


def test_eff_is_billable_cost_plus_every_line_difference(rebill_world: RebillWorld) -> None:
    w = rebill_world
    corrected = w.add_event(project=A1, occurred_at=NOVEMBER)
    untouched = w.add_event(project=A1, occurred_at=NOVEMBER + HOUR)
    w.add_rebill(A, [LineSpec(corrected, "1.50000000", rule=R_LOW)])
    second = w.add_rebill(
        A, [LineSpec(corrected, "2.25000000", version=V_HIGH)], created_at=dt.datetime(2026, 12, 21)
    )

    with w.factory() as session:
        rows = [w.event(session, corrected), w.event(session, untouched)]
        states = effective_state(session, rows)
        lines = (
            session.execute(
                select(UsageRebillLine).where(UsageRebillLine.usage_event_id == corrected.id)
            )
            .scalars()
            .all()
        )
    assert len(lines) == 2
    assert states[corrected.id].eff == DEFAULT_CHARGE + sum(
        (line.difference for line in lines), Decimal(0)
    )
    assert states[corrected.id] == EffectiveState(
        eff=Decimal("2.25000000"),
        # 第二次只换了价格版本：规则沿用第一次的更正规则。
        pricing_rule_id=w.rules[R_LOW],
        provider_price_version_id=w.versions[V_HIGH],
        latest_line_id=second.line_ids[0],
    )
    assert states[untouched.id] == EffectiveState(
        DEFAULT_CHARGE, w.rules[R_GLOBAL], w.versions[V_BASE], None
    )


def test_recompute_after_a_correction_uses_the_corrected_versions(
    rebill_world: RebillWorld,
) -> None:
    w = rebill_world
    charged = w.add_event(project=A1, occurred_at=NOVEMBER)
    with w.factory() as session:
        row = w.event(session, charged)
        state = effective_state(session, [row])[row.id]
        lower = corrected_charge(session, row, state, None, w.rule(session, R_LOW))
    # 0.99140625 × 1.6；差额是 8 位小数的精确值。
    assert repr(lower.billable_cost) == repr(Decimal("1.58625000"))
    assert lower.billable_cost - state.eff == Decimal("-0.39656250")
    w.add_rebill(A, [LineSpec(charged, money_text(lower.billable_cost), rule=R_LOW)])

    with w.factory() as session:
        row = w.event(session, charged)
        state = effective_state(session, [row])[row.id]
        again = corrected_charge(session, row, state, None, None)
        expected = reprice_from_snapshot(
            session, _snapshot(row, w.rules[R_LOW], w.versions[V_BASE]), _quantities(row)
        )
    assert state.eff == lower.billable_cost
    assert repr(again) == repr(expected)
    # 以第一次的更正计费为基准：原版本重算差额为 0。
    assert again.billable_cost == state.eff


def test_a_zero_charge_event_can_be_rebilled_upwards(rebill_world: RebillWorld) -> None:
    w = rebill_world
    # 1 个缓存读 token 按 R_FIXED 是 0.000000004，舍成 0：计费额 0、没有账本行。
    zero = w.add_event(project=A1, occurred_at=NOVEMBER, tokens=(0, 0, 0, 1), rule=R_FIXED)
    assert zero.billable_cost == 0
    assert zero.wallet_transaction_id is None

    with w.factory() as session:
        row = w.event(session, zero)
        state = effective_state(session, [row])[row.id]
        assert state.eff == 0
        check_corrections(session, [row], None, w.rule(session, R_LOW))
        result = corrected_charge(session, row, state, None, w.rule(session, R_LOW))
    # 0.0000078125 USD × 4.5 × 1.6。
    assert result.billable_cost == Decimal("0.00005625")


# --- 适用性 ----------------------------------------------------------------------------


def test_corrective_price_versions_that_do_not_apply(rebill_world: RebillWorld) -> None:
    w = rebill_world
    first = w.add_event(project=A1, occurred_at=NOVEMBER)
    second = w.add_event(project=A1, occurred_at=NOVEMBER + HOUR)

    with w.factory() as session:
        rows = [w.event(session, first), w.event(session, second)]
        assert w.version(session, V_DRAFT).status is PriceVersionStatus.DRAFT
        assert w.version(session, V_DISCARDED).status is PriceVersionStatus.DISCARDED
        assert w.version(session, V_MYR).source_currency == "MYR"
        # 别的模型、别的币种、DRAFT、DISCARDED、缺分量。
        for name in (V_OTHER_MODEL, V_MYR, V_DRAFT, V_DISCARDED, V_EMBED):
            with pytest.raises(RebillPriceNotApplicable) as raised:
                check_corrections(session, rows, w.version(session, name), None)
            assert raised.value.code == "REBILL_PRICE_NOT_APPLICABLE"
            assert raised.value.http_status == 422
            assert first.public_id in raised.value.message
            assert second.public_id not in raised.value.message
            assert "1.98" not in raised.value.message


def test_corrective_price_versions_ignore_the_effective_period(rebill_world: RebillWorld) -> None:
    w = rebill_world
    charged = w.add_event(project=A1, occurred_at=NOVEMBER)

    with w.factory() as session:
        row = w.event(session, charged)
        high = w.version(session, V_HIGH)
        retired = w.version(session, V_RETIRED)
        # V_HIGH 从 2027-03-01 起，不覆盖事件时刻；V_RETIRED 发布后在生效前停用，区间为空。
        assert high.status is PriceVersionStatus.PUBLISHED
        assert high.effective_from is not None and high.effective_from > row.occurred_at
        assert retired.status is PriceVersionStatus.RETIRED
        assert retired.effective_from is not None
        assert retired.effective_from == retired.effective_to
        state = effective_state(session, [row])[row.id]
        for version in (high, retired):
            check_corrections(session, [row], version, None)
        doubled = corrected_charge(session, row, state, high, None)
        from_retired = corrected_charge(session, row, state, retired, None)
        expected = reprice_from_snapshot(
            session, _snapshot(row, w.rules[R_GLOBAL], retired.id), _quantities(row)
        )
    # 单价翻倍，计费额翻倍。
    assert doubled.billable_cost == DEFAULT_CHARGE * 2
    assert repr(from_retired) == repr(expected)


def test_corrective_rules_that_do_not_apply(rebill_world: RebillWorld) -> None:
    w = rebill_world
    first = w.add_event(project=A1, occurred_at=NOVEMBER)
    second = w.add_event(project=A1, occurred_at=NOVEMBER + HOUR)

    with w.factory() as session:
        rows = [w.event(session, first), w.event(session, second)]
        assert w.rule(session, R_DRAFT).status is PricingRuleStatus.DRAFT
        assert w.rule(session, R_DISCARDED).status is PricingRuleStatus.DISCARDED
        assert w.rule(session, R_FIXED_MISSING).strategy is PricingStrategy.FIXED_RATE
        # 别的客户、别的项目、别的模型、DRAFT、DISCARDED、FIXED_RATE 缺分量。
        for name in (
            R_OTHER_CUSTOMER,
            R_OTHER_PROJECT,
            R_OTHER_MODEL,
            R_DRAFT,
            R_DISCARDED,
            R_FIXED_MISSING,
        ):
            with pytest.raises(RebillRuleNotApplicable) as raised:
                check_corrections(session, rows, None, w.rule(session, name))
            assert raised.value.code == "REBILL_RULE_NOT_APPLICABLE"
            assert raised.value.http_status == 422
            assert first.public_id in raised.value.message
            assert second.public_id not in raised.value.message


def test_corrective_rules_that_apply_whatever_their_period(rebill_world: RebillWorld) -> None:
    w = rebill_world
    charged = w.add_event(project=A1, occurred_at=NOVEMBER)

    with w.factory() as session:
        row = w.event(session, charged)
        retired = w.rule(session, R_RETIRED)
        assert retired.status is PricingRuleStatus.RETIRED
        assert retired.effective_from is not None
        assert retired.effective_from == retired.effective_to
        for name in (R_RETIRED, R_LOW, R_FIXED, R_GLOBAL, R_HUGE):
            check_corrections(session, [row], None, w.rule(session, name))
        state = effective_state(session, [row])[row.id]
        result = corrected_charge(session, row, state, None, retired)
    # 0.99140625 × 2.5。
    assert result.billable_cost == Decimal("2.47851563")


def test_the_first_event_a_correction_does_not_apply_to_is_named(
    rebill_world: RebillWorld,
) -> None:
    w = rebill_world
    in_a2 = w.add_event(project=A2, occurred_at=NOVEMBER)
    first_in_a1 = w.add_event(project=A1, occurred_at=NOVEMBER + HOUR)
    second_in_a1 = w.add_event(project=A1, occurred_at=NOVEMBER + 2 * HOUR)
    foreign = w.add_event(project=B1, occurred_at=NOVEMBER)

    with w.factory() as session:
        rows = [w.event(session, ref) for ref in (in_a2, first_in_a1, second_in_a1)]
        # 项目 A2 的规则：A2 的事件适用，A1 的第一个不适用。
        with pytest.raises(RebillRuleNotApplicable) as raised:
            check_corrections(session, rows, None, w.rule(session, R_OTHER_PROJECT))
        assert first_in_a1.public_id in raised.value.message
        assert second_in_a1.public_id not in raised.value.message
        # 先看价格版本再看规则（同一个事件）。
        with pytest.raises(RebillPriceNotApplicable) as raised_price:
            check_corrections(
                session, rows, w.version(session, V_DRAFT), w.rule(session, R_OTHER_PROJECT)
            )
        assert in_a2.public_id in raised_price.value.message
        # 客户 A 的规则对客户 B 的事件不适用（INV-8）。
        for name in (R_FIXED, R_RETIRED, R_HUGE):
            with pytest.raises(RebillRuleNotApplicable) as raised_foreign:
                check_corrections(session, [w.event(session, foreign)], None, w.rule(session, name))
            assert foreign.public_id in raised_foreign.value.message
        # 什么都不更正：没有可判断的，照常返回。
        check_corrections(session, rows, None, None)


# --- 金额与精度 ---------------------------------------------------------------------------


def test_an_overflowing_recomputation_is_amount_out_of_range(rebill_world: RebillWorld) -> None:
    w = rebill_world
    charged = w.add_event(project=A1, occurred_at=NOVEMBER)

    with w.factory() as session:
        row = w.event(session, charged)
        state = effective_state(session, [row])[row.id]
        with pytest.raises(RebillAmountOutOfRange) as raised:
            corrected_charge(session, row, state, None, w.rule(session, R_HUGE))
    assert raised.value.code == "AMOUNT_OUT_OF_RANGE"
    assert raised.value.http_status == 422
    assert not any(character.isdigit() for character in raised.value.message)


def test_the_new_charge_rounds_half_up_once_as_price_event_does(
    rebill_world: RebillWorld,
) -> None:
    w = rebill_world
    # 1 个输入 token 按 R_FIXED：0.000005 / 1000 = 0.000000005，第 9 位小数的 5。
    single = w.add_event(project=A1, occurred_at=NOVEMBER, tokens=(1, 0, 0, 0))

    with w.factory() as session:
        row = w.event(session, single)
        state = effective_state(session, [row])[row.id]
        fixed = w.rule(session, R_FIXED)
        result = corrected_charge(session, row, state, None, fixed)

        version_id = w.versions[V_BASE]
        priced = prices_repository.components_for(session, [version_id])[version_id]
        stored = rules_repository.components_for(session, [fixed.id])[fixed.id]
        fx = session.get(FxRateVersion, w.fx_version_id)
        assert fx is not None
        direct = price_event(
            quantities=_quantities(row),
            source_currency="USD",
            provider_components=[
                UnitPrice(
                    item.component.component_code,
                    item.component.quantity_field,
                    item.row.unit_quantity,
                    item.row.rate_amount,
                )
                for item in priced
            ],
            fx_rate=fx.rate,
            rule=RuleTerms(
                PricingStrategy.FIXED_RATE,
                None,
                tuple(
                    UnitPrice(
                        item.component.component_code,
                        item.component.quantity_field,
                        item.row.unit_quantity,
                        item.row.rate_amount,
                    )
                    for item in stored
                ),
            ),
        )
    assert result.billable_cost_unrounded == Decimal("0.000000005")
    # ROUND_HALF_UP（不是 HALF_EVEN 的 0）。
    assert repr(result.billable_cost) == repr(Decimal("0.00000001"))
    assert repr(result) == repr(direct)


# --- 不加锁 ----------------------------------------------------------------------------


def test_rebill_pricing_reads_take_no_locks(rebill_world: RebillWorld) -> None:
    w = rebill_world
    corrected = w.add_event(project=A1, occurred_at=NOVEMBER)
    plain = w.add_event(project=A1, occurred_at=NOVEMBER + HOUR)
    w.add_rebill(A, [LineSpec(corrected, "1.00000000", rule=R_LOW)])

    with captured(w.factory) as seen, w.factory() as session:
        rows = [w.event(session, corrected), w.event(session, plain)]
        states = effective_state(session, rows)
        version = w.version(session, V_HIGH)
        rule = w.rule(session, R_FIXED)
        inputs = RepricingInputs(session)
        check_corrections(session, rows, version, rule, inputs=inputs)
        for row in rows:
            corrected_charge(session, row, states[row.id], version, rule, inputs=inputs)
            corrected_charge(session, row, states[row.id], None, None)

    assert seen
    if w.factory.kw["bind"].dialect.name == "mysql":
        for statement in seen:
            text = statement.upper()
            assert "FOR SHARE" not in text
            assert "FOR UPDATE" not in text
            assert "LOCK IN SHARE MODE" not in text


# --- 静态检查 ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", NEW_MODULES, ids=lambda path: path.name)
def test_the_new_modules_never_use_float_log_or_audit(path: pathlib.Path) -> None:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)

    assert "float(" not in source
    assert "ROUND_HALF_EVEN" not in source
    assert ".quantize(" not in source
    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    assert "float" not in names
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported.add(node.module)
            imported.update(f"{node.module}.{alias.name}" for alias in node.names)
    assert "logging" not in imported
    assert not any("audit" in name for name in imported)
    assert "record_audit" not in source
    assert "logger" not in names


# --- 预览指纹（纯函数） ------------------------------------------------------------------------

CUSTOMER = zero_uuid(1)
RULE = zero_uuid(2)
VERSION = zero_uuid(3)
FIRST = zero_uuid(11)
SECOND = zero_uuid(12)
CRITERIA: dict[str, object] = {
    "occurred_from": "2026-11-01T00:00:00Z",
    "occurred_to": "2026-12-01T00:00:00Z",
    "project_id": None,
    "provider": "anthropic",
    "model": None,
}


def _events() -> list[TokenEvent]:
    return [
        TokenEvent(1, FIRST, Decimal("1.98281250"), Decimal("1.58625000"), None),
        TokenEvent(2, SECOND, Decimal("0.50000000"), Decimal("0.50000000"), 7),
    ]


def _token(**changes: Any) -> str:
    arguments: dict[str, Any] = {
        "customer_id": CUSTOMER,
        "corrective_pricing_rule_id": RULE,
        "corrective_provider_price_version_id": None,
        "selection": CRITERIA,
        "events": _events(),
    }
    arguments.update(changes)
    return preview_token(**arguments)


def test_preview_token_is_the_sha256_of_the_canonical_json() -> None:
    expected_json = (
        '{"corrective_pricing_rule_id":"00000000-0000-0000-0000-000000000002",'
        '"corrective_provider_price_version_id":null,'
        '"customer_id":"00000000-0000-0000-0000-000000000001",'
        '"events":[["00000000-0000-0000-0000-000000000011","1.98281250","1.58625000",0],'
        '["00000000-0000-0000-0000-000000000012","0.50000000","0.50000000",7]],'
        '"selection":{"model":null,"occurred_from":"2026-11-01T00:00:00.000000Z",'
        '"occurred_to":"2026-12-01T00:00:00.000000Z","project_id":null,'
        '"provider":"anthropic"}}'
    )
    token = _token()
    assert token == hashlib.sha256(expected_json.encode("utf-8")).hexdigest()
    assert len(token) == 64
    assert token == token.lower()
    assert canonical_json({"b": 1, "a": [1, None]}) == '{"a":[1,null],"b":1}'


def test_preview_token_is_stable_under_order_formats_and_omitted_optionals() -> None:
    token = _token()
    # 键的顺序。
    assert _token(selection=dict(reversed(list(CRITERIA.items())))) == token
    # 事件按内部 id 排序，与传入顺序无关。
    assert _token(events=list(reversed(_events()))) == token
    # 金额格式：同一个值，不同的写法。
    reformatted = [
        TokenEvent(1, FIRST, Decimal("1.9828125"), Decimal("1.58625"), 0),
        TokenEvent(2, SECOND, Decimal("0.5"), Decimal("0.500000000"), 7),
    ]
    assert _token(events=reformatted) == token
    # 时区写法、datetime 与已规范化的形式。
    for moment in (
        "2026-11-01T08:00:00+08:00",
        "2026-10-31T19:00:00-05:00",
        "2026-11-01T00:00:00.000000Z",
        "2026-11-01T00:00:00+00:00",
        dt.datetime(2026, 11, 1, 8, 0, tzinfo=dt.timezone(dt.timedelta(hours=8))),
        dt.datetime(2026, 11, 1, 0, 0, tzinfo=dt.UTC),
    ):
        assert _token(selection={**CRITERIA, "occurred_from": moment}) == token
    # 可选键缺省与 null 一样。
    omitted = {key: value for key, value in CRITERIA.items() if value is not None}
    assert _token(selection=omitted) == token
    # 显式事件的顺序。
    explicit = {"usage_event_ids": [SECOND, FIRST]}
    assert _token(selection=explicit) == _token(selection={"usage_event_ids": [FIRST, SECOND]})


def test_preview_token_changes_with_every_material_input() -> None:
    token = _token()
    changed = [
        _token(customer_id=zero_uuid(99)),
        _token(corrective_pricing_rule_id=None),
        _token(corrective_provider_price_version_id=VERSION),
        _token(selection={**CRITERIA, "occurred_from": "2026-11-01T00:00:00.000001Z"}),
        _token(selection={**CRITERIA, "occurred_to": "2026-12-02T00:00:00Z"}),
        _token(selection={**CRITERIA, "project_id": zero_uuid(21)}),
        _token(selection={**CRITERIA, "provider": None}),
        _token(selection={**CRITERIA, "model": "claude-test"}),
        _token(selection={"usage_event_ids": [FIRST, SECOND]}),
        _token(events=_events()[:1]),
        _token(
            events=[
                *_events(),
                TokenEvent(3, zero_uuid(13), Decimal("1"), Decimal("1"), None),
            ]
        ),
    ]
    first, second = _events()
    for replacement in (
        TokenEvent(1, zero_uuid(13), first.eff, first.new, None),
        TokenEvent(1, FIRST, Decimal("1.98281251"), first.new, None),
        TokenEvent(1, FIRST, first.eff, Decimal("1.58625001"), None),
        TokenEvent(1, FIRST, first.eff, first.new, 1),
        # 排序变了：SECOND 排到 FIRST 前面。
        TokenEvent(3, FIRST, first.eff, first.new, None),
    ):
        changed.append(_token(events=[replacement, second]))
    assert token not in changed
    assert len(set(changed)) == len(changed)


def test_normalize_selection_gives_the_canonical_shapes() -> None:
    assert normalize_selection({"usage_event_ids": (SECOND, FIRST)}) == {
        "usage_event_ids": [FIRST, SECOND]
    }
    assert normalize_selection(
        {
            "occurred_from": "2026-11-01T08:00:00.500+08:00",
            "occurred_to": dt.datetime(2026, 12, 1),
            "provider": "anthropic",
        }
    ) == {
        "occurred_from": "2026-11-01T00:00:00.500000Z",
        "occurred_to": "2026-12-01T00:00:00.000000Z",
        "project_id": None,
        "provider": "anthropic",
        "model": None,
    }
    normalized = normalize_selection(CRITERIA)
    assert normalize_selection(normalized) == normalized

    for invalid in (
        {},
        {"usage_event_ids": [FIRST], "occurred_from": "2026-11-01T00:00:00Z"},
        {"usage_event_ids": FIRST},
        {**CRITERIA, "customer_id": CUSTOMER},
        {**CRITERIA, "occurred_to": None},
        {**CRITERIA, "occurred_from": "yesterday"},
        {**CRITERIA, "provider": 7},
    ):
        with pytest.raises(ValueError):
            normalize_selection(invalid)

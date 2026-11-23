"""The provider cost reconciliation repository (design gate #755 v1 §7 rows of AIH-TASK-243).

覆盖设计 §7 标 AIH-TASK-243 的五行，世界都是 tests/backend/provider_cost_world.py 的标准世界：

- **范围判定**（SQLite 与真 MySQL）：12 月 USD 为事件 2、10.50、46.80、币种不一 0；
  E5（集成方）、E3（11 月）、E4（1 月）不计，E2（马来西亚已是 1 月）计入 12 月；
- **边界的 event_id**（只在 MySQL）：非 ASCII 与 64 字符的集成方事件都不计入、查询不报错；
- **诊断计数**：未处理 1、`outbox_counts` 为 (1, 1)；
- **范围内供应商**：12 月与 2027-01 为 anthropic，2027-02 为空；
- **链头**：根 a 被 b 取代（直接 `insert_record`），当前为 b、链为 [a, b]，
  `current_records_overlapping` 只返回 b；唯一冲突的 `IntegrityError` 原样抛出。

另外各跑一遍三个变体世界的构建函数（`fx_switch`、`myr_three`、`unallocatable`）。

MySQL 那部分需要 `BILLING_TEST_DATABASE_URL`；没设时 skip —— **不要把 skipped 读成
passed**，CI 设了它并把 skipped 判成失败。MySQL 分支逐位断言金额文本。uuid 是全零占位，
金额都是虚构的。
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from provider_cost_world import (
    E1,
    P_CHAT,
    PROVIDER_CODE,
    SCOPE,
    UNALLOCATABLE_RECORDS,
    World,
    build_fx_switch_world,
    build_myr_three_world,
    build_unallocatable_world,
    period,
    reconciliation_fields,
    zero_uuid,
)
from sqlalchemy.exc import IntegrityError

from app.repositories.provider_cost_reconciliations import (
    ScopeEstimate,
    chain,
    current_record,
    current_records_overlapping,
    get_by_public_id,
    has_successor,
    insert_record,
    outbox_counts,
    scope_estimate,
    scope_estimated_myr,
    scope_providers,
    unprocessed_count,
)

DECEMBER = "2026-12"


def dialect(world: World) -> str:
    with world.factory() as session:
        return session.get_bind().dialect.name


def estimate(world: World, label: str, currency: str = "USD") -> ScopeEstimate:
    start, end = period(label)
    with world.factory() as session:
        return scope_estimate(
            session,
            provider_id=world.provider_id,
            period_start=start,
            period_end=end,
            source_currency=currency,
        )


def december_root(world: World, public_id: str) -> dict[str, object]:
    """What a confirmation of 12 月 USD 11.70 would write (V44, 51.48, 标准世界的快照)."""
    return reconciliation_fields(
        world,
        public_id=public_id,
        billing_month=DECEMBER,
        source_currency="USD",
        source_total="11.70",
        fx="V44",
        reconciled_cost_myr="51.48",
        estimated_event_count=2,
        estimated_source_cost="10.50",
        estimated_cost_myr="46.80",
        unprocessed_event_count=1,
        outbox_unsettled_count=1,
        outbox_discarded_count=1,
    )


def correction_of(world: World, root_id: int, public_id: str) -> dict[str, object]:
    """12 月 USD 12.00 (× 4.4 = 52.80) superseding `root_id`."""
    fields = december_root(world, public_id)
    fields.update(
        source_total=Decimal("12.00"),
        reconciled_cost_myr=Decimal("52.80"),
        supersedes_id=root_id,
        reason="Fictional corrected statement",
    )
    return fields


# --- 范围判定 -------------------------------------------------------------------------


def test_scope_estimate_counts_the_ai_module_events_of_the_utc_month(provider_cost_world) -> None:
    world = provider_cost_world
    december = estimate(world, DECEMBER)
    start, end = period(DECEMBER)
    with world.factory() as session:
        estimated_myr = scope_estimated_myr(
            session, provider_id=world.provider_id, period_start=start, period_end=end
        )

    # E1（6.00 / 27.00）与 E2（4.50 / V44 19.80）；E5 的 100.00 / 450.00 不在范围内。
    assert december == ScopeEstimate(2, Decimal("10.50"), Decimal("46.80"), 0)
    assert estimated_myr == Decimal("46.80")
    assert type(december.event_count) is int
    assert type(december.currency_mismatch_count) is int
    assert isinstance(december.estimated_source_cost, Decimal)
    assert isinstance(december.estimated_cost_myr, Decimal)
    assert isinstance(estimated_myr, Decimal)
    # E3 归 11 月、E4 归 1 月（各自的 UTC 月），不进 12 月。
    assert estimate(world, "2026-11") == ScopeEstimate(1, Decimal("1.00"), Decimal("4.50"), 0)
    assert estimate(world, "2027-01") == ScopeEstimate(1, Decimal("2.00"), Decimal("8.80"), 0)
    # 换成别的录入币种：来源成本只加同币种的，事件都算币种不一，MYR 估算不变。
    assert estimate(world, DECEMBER, "MYR") == ScopeEstimate(2, Decimal(0), Decimal("46.80"), 2)
    if dialect(world) == "mysql":
        assert str(december.estimated_source_cost) == "10.50000000"
        assert str(december.estimated_cost_myr) == "46.80000000"
        assert str(estimated_myr) == "46.80000000"


def test_boundary_event_ids_stay_out_of_scope(provider_cost_mysql_world) -> None:
    """MySQL only: the two `event_id` columns differ in character set and length.

    两条 12 月的集成方事件（T_pre / P_chat，1.00 USD / V45 / 4.50，无 outbox 行）。
    """
    world = provider_cost_mysql_world
    # 26 字符、含一个非 ASCII 字符；64 字符、前 26 字符就是 E1 的。
    non_ascii = "0000000000000000000000éE01"
    long_id = E1 + "0" * 38
    assert (len(non_ascii), len(long_id)) == (26, 64)
    start, end = period(DECEMBER)
    for event_id, day in ((non_ascii, 11), (long_id, 12)):
        world.add_event(
            event_id=event_id,
            project=P_CHAT,
            occurred_at=start.replace(day=day),
            source_cost="1.00",
        )

    december = estimate(world, DECEMBER)
    with world.factory() as session:
        unprocessed = unprocessed_count(session, period_start=start, period_end=end)
        providers = scope_providers(session, period_start=start, period_end=end)

    assert december == ScopeEstimate(2, Decimal("10.50"), Decimal("46.80"), 0)
    assert str(december.estimated_source_cost) == "10.50000000"
    assert str(december.estimated_cost_myr) == "46.80000000"
    assert unprocessed == 1
    assert providers == [(world.provider_id, PROVIDER_CODE)]


# --- 诊断计数 -------------------------------------------------------------------------


def test_diagnostic_counts_of_december(provider_cost_world) -> None:
    world = provider_cost_world
    start, end = period(DECEMBER)
    with world.factory() as session:
        unprocessed = unprocessed_count(session, period_start=start, period_end=end)
        counts = outbox_counts(session, period_start=start, period_end=end)
        november = period("2026-11")
        november_counts = (
            unprocessed_count(session, period_start=november[0], period_end=november[1]),
            outbox_counts(session, period_start=november[0], period_end=november[1]),
        )

    # E6（PRICING_ERROR）；O_pending 与 O_discarded。
    assert unprocessed == 1
    assert counts == (1, 1)
    assert type(unprocessed) is int
    assert all(type(count) is int for count in counts)
    assert november_counts == (0, (0, 0))


# --- 范围内供应商 ---------------------------------------------------------------------


def test_scope_providers_by_month(provider_cost_world) -> None:
    world = provider_cost_world
    found = {}
    with world.factory() as session:
        for label in (DECEMBER, "2027-01", "2027-02"):
            start, end = period(label)
            found[label] = scope_providers(session, period_start=start, period_end=end)

    anthropic = [(world.provider_id, PROVIDER_CODE)]
    # 2027-01 只有 E4。
    assert found == {DECEMBER: anthropic, "2027-01": anthropic, "2027-02": []}


# --- 链头 -----------------------------------------------------------------------------


def test_the_superseding_record_is_the_head_of_the_chain(provider_cost_world) -> None:
    world = provider_cost_world
    with world.factory() as session:
        a = insert_record(session, **december_root(world, zero_uuid(0)))
        b = insert_record(session, **correction_of(world, a.id, zero_uuid(1)))
        session.commit()
        a_id, b_id = a.id, b.id

    key = {"provider_id": world.provider_id, "scope": SCOPE, "billing_month": DECEMBER}
    start, end = period(DECEMBER)
    with world.factory() as session:
        head = current_record(session, **key)
        rows = chain(session, **key)
        overlapping = current_records_overlapping(session, start=start, end=end)
        wide = current_records_overlapping(session, start=start, end=period("2027-04")[1])
        later = current_records_overlapping(session, start=end, end=period("2027-01")[1])
        successors = (has_successor(session, a_id), has_successor(session, b_id))
        by_public_id = get_by_public_id(session, zero_uuid(0))
        missing = get_by_public_id(session, zero_uuid(9))
        january = current_record(session, **{**key, "billing_month": "2027-01"})

    assert head is not None and head.id == b_id
    assert [row.id for row in rows] == [a_id, b_id]
    assert [(row.id, code) for row, code in overlapping] == [(b_id, PROVIDER_CODE)]
    assert [(row.id, code) for row, code in wide] == [(b_id, PROVIDER_CODE)]
    assert later == []
    assert successors == (True, False)
    assert by_public_id is not None and by_public_id.id == a_id
    assert missing is None
    assert january is None
    assert head.supersedes_id == a_id
    assert head.reconciled_cost_myr == Decimal("52.80")
    if dialect(world) == "mysql":
        assert str(head.reconciled_cost_myr) == "52.80000000"
        assert str(head.fx_rate_applied) == "4.4000000000"


def test_insert_record_raises_unique_conflicts_as_they_are(provider_cost_world) -> None:
    world = provider_cost_world
    with world.factory() as session:
        a = insert_record(session, **december_root(world, zero_uuid(0)))
        insert_record(session, **correction_of(world, a.id, zero_uuid(1)))
        session.commit()
        a_id = a.id

    # 同月第二个根（`ux_…_root`）；a 第二次被取代（`uq_…_supersedes`）。
    for fields in (
        december_root(world, zero_uuid(2)),
        correction_of(world, a_id, zero_uuid(3)),
    ):
        with world.factory() as session:
            with pytest.raises(IntegrityError):
                insert_record(session, **fields)
            session.rollback()

    with world.factory() as session:
        key = {"provider_id": world.provider_id, "scope": SCOPE, "billing_month": DECEMBER}
        assert [row.public_id for row in chain(session, **key)] == [zero_uuid(0), zero_uuid(1)]


# --- 变体世界 -------------------------------------------------------------------------


def test_fx_switch_snapshots_e2_at_v45(provider_cost_db) -> None:
    world = build_fx_switch_world(provider_cost_db)
    # E1 27.00 + E2 4.50 × 4.5 = 20.25。
    assert estimate(world, DECEMBER) == ScopeEstimate(2, Decimal("10.50"), Decimal("47.25"), 0)
    assert estimate(world, "2027-01") == ScopeEstimate(1, Decimal("2.00"), Decimal("8.80"), 0)
    assert world.fx["V45"].effective_to == period("2027-01")[0]


def test_myr_three_has_three_myr_events(provider_cost_db) -> None:
    world = build_myr_three_world(provider_cost_db)
    assert estimate(world, DECEMBER, "MYR") == ScopeEstimate(3, Decimal("3.00"), Decimal("3.00"), 0)
    assert world.fx == {}


def test_unallocatable_writes_three_current_roots(provider_cost_db) -> None:
    world = build_unallocatable_world(provider_cost_db)
    start, _ = period(DECEMBER)
    with world.factory() as session:
        rows = current_records_overlapping(session, start=start, end=period("2027-03")[1])

    assert [(row.public_id, row.billing_month, code) for row, code in rows] == [
        (spec.public_id, spec.billing_month, PROVIDER_CODE) for spec in UNALLOCATABLE_RECORDS
    ]
    assert [row.reconciled_cost_myr for row, _ in rows] == [
        Decimal("51.48"),
        Decimal("5.00"),
        Decimal("0.00"),
    ]
    assert [row.supersedes_id for row, _ in rows] == [None, None, None]
    assert [row.created_by for row, _ in rows] == [world.admin_id] * 3

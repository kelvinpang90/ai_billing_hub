"""The read-only rebill repository (design gate #830 v12 §2「选事件」, §7, AIH-TASK-254).

世界见 tests/backend/rebill_world.py（SQLite 与真 MySQL 各一遍；MySQL 那一半在
`BILLING_TEST_DATABASE_URL` 未设置时 skip —— **不要把 skipped 读成 passed**，CI 设了它）。
时间一律注入，不 sleep；uuid 是全零占位。
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator
from contextlib import contextmanager
from decimal import Decimal

import pytest
from provider_cost_world import zero_uuid
from rebill_world import (
    A1,
    A2,
    B1,
    I1,
    INTERNAL,
    MODEL_CODE,
    OTHER_MODEL_CODE,
    PROVIDER_CODE,
    R_GLOBAL,
    R_LOW,
    V_BASE,
    V_HIGH,
    ZERO_TOKENS,
    A,
    B,
    LineSpec,
    RebillWorld,
)
from sqlalchemy import event, select
from sqlalchemy.orm import Session, sessionmaker

from app.models.statements import MonthlyStatement
from app.models.usage import UsageEventStatus
from app.models.wallet import TransactionType, WalletTransaction
from app.repositories.usage_rebills import (
    event_history,
    find_explicit_events,
    get_by_idempotency_key,
    get_rebill,
    get_rebill_row,
    latest_lines,
    list_rebills,
    original_periods,
    select_events,
    statement_ids,
)

START = dt.datetime(2026, 11, 10, 0, 0, 0)
END = dt.datetime(2026, 11, 20, 0, 0, 0)
HOUR = dt.timedelta(hours=1)
MICROSECOND = dt.timedelta(microseconds=1)


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


def _tenant(world: RebillWorld, name: str) -> int:
    return world.customers[name].tenant_id


# --- 选事件 ---------------------------------------------------------------------------


def test_criteria_selection_is_half_open_and_only_the_customers_processed_prepaid_events(
    rebill_world: RebillWorld,
) -> None:
    w = rebill_world
    inside_last = w.add_event(project=A1, occurred_at=END - MICROSECOND)
    at_start = w.add_event(project=A1, occurred_at=START)
    w.add_event(project=A1, occurred_at=START - MICROSECOND)
    w.add_event(project=A1, occurred_at=END)
    other_project = w.add_event(project=A2, occurred_at=START + HOUR)
    zero = w.add_event(project=A1, occurred_at=START + 2 * HOUR, tokens=ZERO_TOKENS)
    w.add_event(project=A1, occurred_at=START + 3 * HOUR, status=UsageEventStatus.RECEIVED)
    w.add_event(project=A1, occurred_at=START + 3 * HOUR, status=UsageEventStatus.PRICING_ERROR)
    w.add_event(project=I1, occurred_at=START + 3 * HOUR)
    foreign = w.add_event(project=B1, occurred_at=START + 3 * HOUR)
    expected = sorted([inside_last.id, at_start.id, other_project.id, zero.id])

    with w.factory() as session:
        found = select_events(session, _tenant(w, A), occurred_from=START, occurred_to=END)
        assert found.match_count == 4
        assert [row.id for row in found.events] == expected

        by_project = select_events(
            session,
            _tenant(w, A),
            occurred_from=START,
            occurred_to=END,
            project_id=w.projects[A2].project_id,
        )
        assert [row.id for row in by_project.events] == [other_project.id]

        by_catalog = select_events(
            session,
            _tenant(w, A),
            occurred_from=START,
            occurred_to=END,
            provider_code=PROVIDER_CODE,
            model_code=MODEL_CODE,
        )
        assert [row.id for row in by_catalog.events] == expected
        for provider, model in (("someone-else", None), (None, OTHER_MODEL_CODE)):
            none = select_events(
                session,
                _tenant(w, A),
                occurred_from=START,
                occurred_to=END,
                provider_code=provider,
                model_code=model,
            )
            assert (none.match_count, none.events) == (0, [])

        # 别的客户只选到它自己的事件（INV-8）。
        theirs = select_events(session, _tenant(w, B), occurred_from=START, occurred_to=END)
        assert [row.id for row in theirs.events] == [foreign.id]
        # 内部计量的客户一个都选不到。
        internal = select_events(
            session, _tenant(w, INTERNAL), occurred_from=START, occurred_to=END
        )
        assert internal.match_count == 0

        with pytest.raises(ValueError):
            select_events(
                session,
                _tenant(w, A),
                occurred_from=START.replace(tzinfo=dt.UTC),
                occurred_to=END,
            )


def test_criteria_selection_takes_1000_matches_and_only_counts_1001(
    rebill_world: RebillWorld,
) -> None:
    w = rebill_world
    w.add_zero_events(project=A1, moments=[START + dt.timedelta(seconds=n) for n in range(1000)])

    with w.factory() as session:
        found = select_events(session, _tenant(w, A), occurred_from=START, occurred_to=END)
    assert found.match_count == 1000
    assert len(found.events) == 1000
    ids = [row.id for row in found.events]
    assert ids == sorted(ids)

    w.add_zero_events(project=A1, moments=[START + dt.timedelta(seconds=1000)])
    with captured(w.factory) as seen, w.factory() as session:
        over = select_events(session, _tenant(w, A), occurred_from=START, occurred_to=END)
    assert over.match_count == 1001
    assert over.events == []
    # 超过上限时只发了 COUNT，不取行。
    queries = [statement for statement in seen if "usage_events" in statement]
    assert len(queries) == 1
    assert "count(" in queries[0].lower()


def test_explicit_events_are_the_customers_and_the_ids_no_event_has(
    rebill_world: RebillWorld,
) -> None:
    w = rebill_world
    own = w.add_event(project=A1, occurred_at=START)
    unprocessed = w.add_event(project=A2, occurred_at=START, status=UsageEventStatus.RECEIVED)
    foreign = w.add_event(project=B1, occurred_at=START)
    internal = w.add_event(project=I1, occurred_at=START)
    missing = zero_uuid(999_999)

    with w.factory() as session:
        found = find_explicit_events(
            session,
            _tenant(w, A),
            [unprocessed.public_id, foreign.public_id, missing, internal.public_id, own.public_id],
        )
        # 状态原样给出：未处理的事件由服务判 409。
        assert [row.id for row in found.events] == [own.id, unprocessed.id]
        assert found.events[1].status is UsageEventStatus.RECEIVED
        # 别的客户的事件既不在 events 也不算缺失（409，不是 404）。
        assert found.missing == [missing]

        theirs = find_explicit_events(session, _tenant(w, B), [own.public_id, foreign.public_id])
        assert [row.id for row in theirs.events] == [foreign.id]
        assert theirs.missing == []


# --- 明细 -----------------------------------------------------------------------------


def test_latest_lines_and_the_history_of_an_event(rebill_world: RebillWorld) -> None:
    w = rebill_world
    first, second, untouched = (
        w.add_event(project=A1, occurred_at=START + n * HOUR) for n in range(3)
    )
    december_1 = dt.datetime(2026, 12, 1, 0, 0, 0)
    december_2 = dt.datetime(2026, 12, 2, 0, 0, 0)
    earlier = w.add_rebill(
        A,
        [LineSpec(first, "1.50000000", rule=R_LOW), LineSpec(second, "1.00000000")],
        created_at=december_1,
    )
    later = w.add_rebill(A, [LineSpec(first, "2.00000000", version=V_HIGH)], created_at=december_2)

    with w.factory() as session:
        latest = latest_lines(session, [first.id, second.id, untouched.id])
        assert set(latest) == {first.id, second.id}
        assert latest[first.id].id == later.line_ids[0]
        assert latest[first.id].corrected_charge == Decimal("2.00000000")
        assert latest[second.id].id == earlier.line_ids[1]

        history = event_history(session, first.id)
        assert [(item.line.id, item.rebill_id, item.rebill_created_at) for item in history] == [
            (earlier.line_ids[0], earlier.public_id, december_1),
            (later.line_ids[0], later.public_id, december_2),
        ]
        assert [item.line.previous_charge for item in history] == [
            first.billable_cost,
            Decimal("1.50000000"),
        ]
        assert event_history(session, untouched.id) == []


def test_idempotency_key_lookup(rebill_world: RebillWorld) -> None:
    w = rebill_world
    charged = w.add_event(project=A1, occurred_at=START)
    rebill = w.add_rebill(A, [LineSpec(charged, "1.00000000")])

    with w.factory() as session:
        found = get_by_idempotency_key(session, rebill.idempotency_key)
        assert found is not None and found.public_id == rebill.public_id
        assert get_by_idempotency_key(session, zero_uuid(999_999)) is None
        row = get_rebill_row(session, rebill.public_id)
        assert row is not None and row.id == rebill.id
        assert get_rebill_row(session, zero_uuid(999_999)) is None


# --- 列表与详情 ---------------------------------------------------------------------------


def test_rebill_list_is_id_descending_with_a_cursor_and_an_optional_customer(
    rebill_world: RebillWorld,
) -> None:
    w = rebill_world
    a_events = [w.add_event(project=A1, occurred_at=START + n * HOUR) for n in range(3)]
    b_event = w.add_event(project=B1, occurred_at=START)
    a_first = w.add_rebill(A, [LineSpec(a_events[0], "1.00000000")])
    a_second = w.add_rebill(A, [LineSpec(a_events[1], "3.00000000")])
    b_only = w.add_rebill(B, [LineSpec(b_event, "1.00000000")])
    a_third = w.add_rebill(A, [LineSpec(a_events[2], "1.00000000")])

    with w.factory() as session:
        page = list_rebills(session, tenant_id=None, before_id=None, limit=2)
        assert [item.rebill.id for item in page.items] == [a_third.id, b_only.id]
        assert page.has_more
        rest = list_rebills(session, tenant_id=None, before_id=b_only.id, limit=2)
        assert [item.rebill.id for item in rest.items] == [a_second.id, a_first.id]
        assert not rest.has_more

        # 恰好 limit 行：没有下一页。
        mine = list_rebills(session, tenant_id=_tenant(w, A), before_id=None, limit=3)
        assert [item.rebill.id for item in mine.items] == [a_third.id, a_second.id, a_first.id]
        assert not mine.has_more
        cursor = get_rebill_row(session, a_second.public_id)
        assert cursor is not None
        older = list_rebills(session, tenant_id=_tenant(w, A), before_id=cursor.id, limit=1)
        assert [item.rebill.id for item in older.items] == [a_first.id]
        assert not older.has_more

        by_id = {item.rebill.id: item for item in mine.items}
        debit = by_id[a_second.id]
        assert debit.customer_id == w.customers[A].public_id
        assert debit.created_by_email == w.admin.email
        assert debit.ledger_type is TransactionType.REBILL_DEBIT
        assert debit.ledger_amount == -debit.rebill.difference
        credit = by_id[a_first.id]
        assert credit.ledger_type is TransactionType.REBILL_CREDIT
        assert credit.ledger_amount == -credit.rebill.difference > 0

        with pytest.raises(ValueError):
            list_rebills(session, tenant_id=None, before_id=None, limit=0)


def test_rebill_detail_names_every_reference_by_public_id(rebill_world: RebillWorld) -> None:
    w = rebill_world
    statement_pk = w.add_statement(A, "2026-11")
    charged = w.add_event(project=A1, occurred_at=START)
    zero = w.add_event(project=A2, occurred_at=START + HOUR, tokens=ZERO_TOKENS)
    rebill = w.add_rebill(
        A,
        [
            LineSpec(
                charged,
                "1.50000000",
                rule=R_LOW,
                version=V_HIGH,
                original_statement_id=statement_pk,
            ),
            LineSpec(zero, "0.10000000", rule=R_LOW),
        ],
        corrective_rule=R_LOW,
    )

    with w.factory() as session:
        detail = get_rebill(session, rebill.public_id)
        assert detail is not None
        statement_public_id = session.execute(
            select(MonthlyStatement.public_id).where(MonthlyStatement.id == statement_pk)
        ).scalar_one()
        charged_transaction = session.execute(
            select(WalletTransaction.public_id).where(
                WalletTransaction.id == charged.wallet_transaction_id
            )
        ).scalar_one()
        ledger = session.execute(
            select(WalletTransaction).where(WalletTransaction.id == rebill.wallet_transaction_id)
        ).scalar_one()
        assert get_rebill(session, zero_uuid(999_999)) is None

    summary = detail.summary
    assert summary.rebill.public_id == rebill.public_id
    assert summary.customer_id == w.customers[A].public_id
    assert summary.created_by_email == w.admin.email
    assert summary.wallet_transaction_id == ledger.public_id
    # (1.5 − 1.9828125) + (0.1 − 0) = −0.3828125：多扣了，补退。
    assert summary.rebill.difference == Decimal("-0.38281250")
    assert summary.ledger_type is TransactionType.REBILL_CREDIT
    assert summary.ledger_amount == Decimal("0.38281250")
    assert detail.corrective_pricing_rule_id == w.public_rule(R_LOW)
    assert detail.corrective_provider_price_version_id is None

    billed_line, zero_line = detail.lines
    assert billed_line.line.id == rebill.line_ids[0]
    assert billed_line.usage_event_id == charged.public_id
    assert billed_line.occurred_at == START
    assert billed_line.project_id == w.projects[A1].public_id
    assert billed_line.project_name == A1
    assert (billed_line.provider, billed_line.model) == (PROVIDER_CODE, MODEL_CODE)
    assert billed_line.previous_pricing_rule_id == w.public_rule(R_GLOBAL)
    assert billed_line.corrected_pricing_rule_id == w.public_rule(R_LOW)
    assert billed_line.previous_provider_price_version_id == w.public_version(V_BASE)
    assert billed_line.corrected_provider_price_version_id == w.public_version(V_HIGH)
    assert billed_line.original_wallet_transaction_id == charged_transaction
    assert billed_line.original_statement_id == statement_public_id

    assert zero_line.usage_event_id == zero.public_id
    assert zero_line.project_id == w.projects[A2].public_id
    assert zero_line.corrected_provider_price_version_id == w.public_version(V_BASE)
    assert zero_line.original_wallet_transaction_id is None
    assert zero_line.original_statement_id is None


# --- 原账期与原对账单 ---------------------------------------------------------------------


def test_original_period_follows_the_statement_attribution_rule(
    rebill_world: RebillWorld,
) -> None:
    w = rebill_world
    # 马来西亚 11-30 23:00。11 月的 cut-off 是马来西亚 12-02 00:00 = UTC 12-01 16:00。
    occurred = dt.datetime(2026, 11, 30, 15, 0, 0)
    on_time = w.add_event(project=A1, occurred_at=occurred)
    in_grace = w.add_event(
        project=A1, occurred_at=occurred, ledger_at=dt.datetime(2026, 12, 1, 15, 59, 59)
    )
    late = w.add_event(project=A1, occurred_at=occurred, ledger_at=dt.datetime(2026, 12, 1, 16))
    # 计费额为 0、没有账本行：UTC 仍在 11 月，马来西亚已是 12-01 01:00。
    zero = w.add_event(
        project=A1, occurred_at=dt.datetime(2026, 11, 30, 17, 0, 0), tokens=ZERO_TOKENS
    )
    assert zero.billable_cost == 0 and zero.wallet_transaction_id is None

    with w.factory() as session:
        events = [w.event(session, ref) for ref in (on_time, in_grace, late, zero)]
        periods = original_periods(session, events)
    assert periods == {
        on_time.id: "2026-11",
        in_grace.id: "2026-11",
        late.id: "2026-12",
        zero.id: "2026-12",
    }


def test_statement_ids_are_the_customers_existing_statements(rebill_world: RebillWorld) -> None:
    w = rebill_world
    november = w.add_statement(A, "2026-11")
    theirs = w.add_statement(B, "2026-12")

    with w.factory() as session:
        assert statement_ids(session, _tenant(w, A), ["2026-11", "2026-12", "2026-11"]) == {
            "2026-11": november
        }
        assert statement_ids(session, _tenant(w, B), ["2026-11", "2026-12"]) == {"2026-12": theirs}
        assert statement_ids(session, _tenant(w, A), []) == {}


# --- 不加锁 ----------------------------------------------------------------------------


def test_every_read_is_a_plain_read(rebill_world: RebillWorld) -> None:
    w = rebill_world
    statement_pk = w.add_statement(A, "2026-11")
    charged = w.add_event(project=A1, occurred_at=START)
    rebill = w.add_rebill(A, [LineSpec(charged, "1.00000000", original_statement_id=statement_pk)])

    with captured(w.factory) as seen, w.factory() as session:
        tenant_id = _tenant(w, A)
        select_events(session, tenant_id, occurred_from=START, occurred_to=END)
        found = find_explicit_events(session, tenant_id, [charged.public_id])
        latest_lines(session, [charged.id])
        event_history(session, charged.id)
        get_by_idempotency_key(session, rebill.idempotency_key)
        get_rebill_row(session, rebill.public_id)
        list_rebills(session, tenant_id=tenant_id, before_id=None, limit=10)
        get_rebill(session, rebill.public_id)
        periods = original_periods(session, found.events)
        statement_ids(session, tenant_id, periods.values())

    assert seen
    if w.factory.kw["bind"].dialect.name == "mysql":
        for statement in seen:
            text = statement.upper()
            assert "FOR SHARE" not in text
            assert "FOR UPDATE" not in text
            assert "LOCK IN SHARE MODE" not in text

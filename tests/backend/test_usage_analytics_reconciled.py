"""The allocation inputs of reconciled provider cost (design gate #756 v2 §7 rows of AIH-TASK-247).

世界是 tests/backend/provider_cost_world.py 的标准世界，加一条经 `insert_record` 写入的
12 月记录（USD 11.70、V44、R = 51.48、快照取 L1 §7 标准世界的值，即 `UNALLOCATABLE_RECORDS`
的第一条）；区间 `[2026-11-01, 2027-02-01)`：

- **分摊输入**（SQLite 与真 MySQL）：`records` 恰好一条（E = 46.80）；按客户、马来西亚月、
  供应商分组时 E1 27.00 归 internal、E2 19.80 归 prepaid，E3（11 月）、E4（1 月）不在 12 月
  记录的期间里，E5（集成方，无 outbox）不在任何覆盖和里；
- **查询次数**（SQLite）：不带筛选 3、`customer_id = T_int` 4、`customer_id` 不存在 1、
  `customer_id = T_int` 且 `project_id` 不存在 2、`customer_id` 不存在且带 `project_id` 1，
  后三种返回空输入。

MySQL 那部分需要 `BILLING_TEST_DATABASE_URL`；没设时 skip —— **不要把 skipped 读成
passed**，CI 设了它并把 skipped 判成失败。MySQL 分支逐位断言金额文本。uuid 是全零占位，
金额都是虚构的。
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any

import pytest
from provider_cost_world import (
    P_CRM,
    P_PRE,
    PROVIDER_CODE,
    UNALLOCATABLE_RECORDS,
    World,
    build_standard_world,
    reconciliation_fields,
)
from sqlalchemy import event, select

from app.models.tenancy import Project, Tenant
from app.repositories.provider_cost_reconciliations import insert_record
from app.repositories.usage_analytics import (
    AllocationInputs,
    CoveringRecord,
    reconciled_allocation_inputs,
)

WINDOW_FROM = dt.datetime(2026, 11, 1, 0, 0, 0)
WINDOW_TO = dt.datetime(2027, 2, 1, 0, 0, 0)
UNKNOWN_ID = "00000000-0000-0000-0000-000000000000"
INTERNAL = "INTERNAL_METERED_ONLY"
PREPAID = "PREPAID"
NO_FILTERS: dict[str, Any] = {
    "customer_id": None,
    "project_id": None,
    "provider": None,
    "model": None,
    "feature": None,
}
DECEMBER = UNALLOCATABLE_RECORDS[0]
EMPTY = AllocationInputs(records=(), group_covered={}, total_covered={})


def dialect(world: World) -> str:
    with world.factory() as session:
        return session.get_bind().dialect.name


def add_december_record(world: World) -> str:
    """The December root a confirmation of USD 11.70 would write (V44, 51.48)."""
    with world.factory() as session:
        insert_record(
            session,
            **reconciliation_fields(
                world,
                public_id=DECEMBER.public_id,
                billing_month=DECEMBER.billing_month,
                source_currency=DECEMBER.source_currency,
                source_total=DECEMBER.source_total,
                fx=DECEMBER.fx,
                reconciled_cost_myr=DECEMBER.reconciled_cost_myr,
                estimated_event_count=DECEMBER.estimated_event_count,
                estimated_source_cost=DECEMBER.estimated_source_cost,
                estimated_cost_myr=DECEMBER.estimated_cost_myr,
                unprocessed_event_count=DECEMBER.unprocessed_event_count,
                outbox_unsettled_count=DECEMBER.outbox_unsettled_count,
                outbox_discarded_count=DECEMBER.outbox_discarded_count,
            ),
        )
        session.commit()
    return DECEMBER.public_id


def tenant_public_id(world: World, project: str) -> str:
    with world.factory() as session:
        return session.execute(
            select(Tenant.public_id).where(Tenant.id == world.projects[project].tenant_id)
        ).scalar_one()


def project_public_id(world: World, project: str) -> str:
    with world.factory() as session:
        return session.execute(
            select(Project.public_id).where(Project.id == world.projects[project].project_id)
        ).scalar_one()


def inputs(world: World, group_by: str, **filters: Any) -> AllocationInputs:
    with world.factory() as session:
        return reconciled_allocation_inputs(
            session,
            group_by=group_by,
            occurred_from=WINDOW_FROM,
            occurred_to=WINDOW_TO,
            **{**NO_FILTERS, **filters},
        )


# --- 分摊输入 -------------------------------------------------------------------------


@pytest.mark.parametrize("group_by", ["customer", "month", "provider"])
def test_allocation_inputs_of_the_december_record(provider_cost_world, group_by) -> None:
    world = provider_cost_world
    record = add_december_record(world)
    keys = {
        "customer": (tenant_public_id(world, P_CRM), tenant_public_id(world, P_PRE)),
        # 马来西亚月：E1 在 12 月，E2（UTC 12-31 20:00）已是 2027-01。
        "month": ("2026-12", "2027-01"),
        # E5 同属 anthropic，但没有 outbox 行：不在任何覆盖和里。
        "provider": (PROVIDER_CODE, PROVIDER_CODE),
    }
    internal_key, prepaid_key = keys[group_by]

    result = inputs(world, group_by)

    assert result.records == (
        CoveringRecord(
            public_id=record,
            provider_code=PROVIDER_CODE,
            billing_month="2026-12",
            period_start=dt.datetime(2026, 12, 1, 0, 0, 0),
            period_end=dt.datetime(2027, 1, 1, 0, 0, 0),
            reconciled_cost_myr=Decimal("51.48"),
            scope_estimated_myr=Decimal("46.80"),
        ),
    )
    assert result.group_covered == {
        (internal_key, record, INTERNAL): Decimal("27.00"),
        (prepaid_key, record, PREPAID): Decimal("19.80"),
    }
    assert result.total_covered == {
        (record, INTERNAL): Decimal("27.00"),
        (record, PREPAID): Decimal("19.80"),
    }
    covering = result.records[0]
    assert isinstance(covering.reconciled_cost_myr, Decimal)
    assert isinstance(covering.scope_estimated_myr, Decimal)
    for amount in (*result.group_covered.values(), *result.total_covered.values()):
        assert isinstance(amount, Decimal)
    for key, _, _ in result.group_covered:
        assert type(key) is str
    if dialect(world) == "mysql":
        assert str(covering.reconciled_cost_myr) == "51.48000000"
        assert str(covering.scope_estimated_myr) == "46.80000000"
        assert str(result.group_covered[(internal_key, record, INTERNAL)]) == "27.00000000"
        assert str(result.group_covered[(prepaid_key, record, PREPAID)]) == "19.80000000"
        assert str(result.total_covered[(record, INTERNAL)]) == "27.00000000"
        assert str(result.total_covered[(record, PREPAID)]) == "19.80000000"


def test_unknown_customer_gives_the_empty_inputs(provider_cost_world) -> None:
    world = provider_cost_world
    add_december_record(world)

    assert inputs(world, "customer", customer_id=UNKNOWN_ID) == EMPTY


# --- 查询次数 -------------------------------------------------------------------------


def test_query_counts(provider_cost_sqlite) -> None:
    world = build_standard_world(provider_cost_sqlite)
    add_december_record(world)
    t_int = tenant_public_id(world, P_CRM)
    p_crm = project_public_id(world, P_CRM)
    engine = world.factory.kw["bind"]
    statements: list[str] = []

    def record(conn, cursor, statement, parameters, context, executemany) -> None:
        statements.append(statement)

    cases: list[tuple[dict[str, Any], int, bool]] = [
        ({}, 3, False),
        ({"customer_id": t_int}, 4, False),
        ({"customer_id": UNKNOWN_ID}, 1, True),
        ({"customer_id": t_int, "project_id": UNKNOWN_ID}, 2, True),
        ({"customer_id": UNKNOWN_ID, "project_id": p_crm}, 1, True),
    ]
    for filters, expected, empty in cases:
        with world.factory() as session:
            statements.clear()
            event.listen(engine, "before_cursor_execute", record)
            try:
                result = reconciled_allocation_inputs(
                    session,
                    group_by="customer",
                    occurred_from=WINDOW_FROM,
                    occurred_to=WINDOW_TO,
                    **{**NO_FILTERS, **filters},
                )
            finally:
                event.remove(engine, "before_cursor_execute", record)
        assert len(statements) == expected, (filters, statements)
        if empty:
            assert result == EMPTY, filters
        else:
            assert len(result.records) == 1, filters

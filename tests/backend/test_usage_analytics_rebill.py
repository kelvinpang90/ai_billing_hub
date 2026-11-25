"""Usage analytics revenue includes rebill differences (design gate #830 v12, AIH-TASK-258).

设计 §2「用量分析」与 §7「用量分析」一行：预付收入 = 每个事件的 `billable_cost` + 该事件全部明细
`difference` 之和，按原事件的维度与 `occurred_at` 归属；事件数、token、估算成本与对账后成本的
分摊不变，毛利随收入变；两张新表为空时每个分组、合计与导出与改动前逐位相同
（§8「上线顺序与财务口径」）。

管理端还没有写入 Rebill 的接口：明细由 tests/backend/rebill_world.py 的 `add_rebill` 直接写。
接口与导出经现有的 `GET /api/v1/admin/usage-analytics` 与 `/export`（CSV、xlsx）。

**世界**（全部时刻是不带时区的 UTC；默认数量下每个事件计费 4.5、估算成本 2.25）：

| 事件 | 客户 / 项目 | occurred_at（UTC） | 马来西亚月 |
| --- | --- | --- | --- |
| `e1` | A / A1 | 2026-11-10 04:00 | 2026-11 |
| `e2` | A / A2 | 2026-11-20 04:00 | 2026-11 |
| `i1` | I / I1（内部计量） | 2026-11-15 04:00 | 2026-11 |
| `e3` | A / A1 | 2026-12-05 04:00 | 2026-12 |
| `e4` | B / B1 | 2026-12-10 04:00 | 2026-12 |

第一次 Rebill（A）：`e1` 4.5 → 3.25、`e3` 4.5 → 5.5；第二次（A）：`e1` 3.25 → 4；
B 的一次：`e4` 4.5 → 2.5。
`e1` 有两条明细（差额 −1.25、+0.75）。金额都是二进制精确的（SQLite 账本 CHECK 在 REAL 上算）。

MySQL 那一半需要 `BILLING_TEST_DATABASE_URL`，没设时 skip ——
**不要把 skipped 读成 passed**，CI 设了它。
SQL 文本不做断言；uuid 是全零占位；时间一律注入（令牌按现有测试的写法取当前时刻签发）。
"""

from __future__ import annotations

import base64
import csv
import datetime as dt
import io
import os
from collections.abc import Iterator
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Final

import openpyxl
import pytest
from fastapi.testclient import TestClient
from rebill_world import (
    A1,
    A2,
    B1,
    I1,
    INTERNAL,
    MODEL_CODE,
    PROVIDER_CODE,
    A,
    B,
    EventRef,
    LineSpec,
    RebillWorld,
    build_world,
    count_rows,
    mysql_factory,
    sqlite_shared_factory,
)
from sqlalchemy import ColumnElement, case, func
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.core.csv_export import export_lease_held
from app.core.tokens import issue_access_token
from app.main import create_app
from app.models.ai_catalog import PayloadShape
from app.models.tenancy import BillingMode
from app.models.usage import UsageEvent
from app.models.usage_rebills import UsageRebill, UsageRebillLine
from app.repositories import usage_analytics as analytics_repository
from app.schemas.usage_analytics import USAGE_ANALYTICS_EXPORT_COLUMNS
from app.services.auth import utc_now

PATH: Final = "/api/v1/admin/usage-analytics"
EXPORT: Final = "/api/v1/admin/usage-analytics/export"
SHEET: Final = "usage-analytics"
BOM: Final = b"\xef\xbb\xbf"
# 马来西亚 2026-11-01 00:00 到 2027-01-01 00:00。
WINDOW: Final = {"occurred_from": "2026-10-31T16:00:00Z", "occurred_to": "2026-12-31T16:00:00Z"}
WINDOW_FROM: Final = dt.datetime(2026, 10, 31, 16, 0, 0)
WINDOW_TO: Final = dt.datetime(2026, 12, 31, 16, 0, 0)
GROUPINGS: Final = ("provider", "model", "customer", "project", "feature", "day", "month")
# 本文件按这三种分组核对收入（设计 §7「用量分析」一行：按客户、按月；另加按模型）。
CHECKED: Final = ("customer", "month", "model")

# 默认数量下按快照算出的计费额与估算成本（rebill_world 的注释）。
ORIGINAL_CHARGE: Final = Decimal("4.5")
ESTIMATED: Final = Decimal("2.25")


def money(value: Decimal) -> str:
    return f"{value:.8f}"


# --- 夹具 -----------------------------------------------------------------------------


def settings_for(tmp_path) -> Settings:
    jwt, master = tmp_path / "jwt.key", tmp_path / "master.key"
    jwt.write_text("a-test-signing-key-that-is-long-enough", encoding="utf-8")
    master.write_text(f"1:{base64.b64encode(os.urandom(32)).decode()}\n", encoding="utf-8")
    return Settings(jwt_secret_file=str(jwt), master_key_file=str(master), database_url="")


@pytest.fixture(autouse=True)
def lease_is_free() -> Iterator[None]:
    # 进程级导出租约：每条用例开始与结束时都必须空闲。
    assert not export_lease_held()
    yield
    assert not export_lease_held()


@pytest.fixture(params=["sqlite", "mysql"])
def analytics_db(request, monkeypatch) -> Iterator[sessionmaker[Session]]:
    """The empty rebill database: shared-connection SQLite, and a real MySQL."""
    if request.param == "mysql":
        yield from mysql_factory(monkeypatch)
        return
    yield from sqlite_shared_factory()


@dataclass(frozen=True)
class Placed:
    ref: EventRef
    customer: str
    # 马来西亚月 `YYYY-MM`。
    month: str


@dataclass
class Scene:
    world: RebillWorld
    client: TestClient
    headers: dict[str, str]
    events: dict[str, Placed]

    def charges(self, **overrides: str) -> dict[str, Decimal]:
        """Each prepaid event's current effective charge: the original unless overridden."""
        return {
            name: Decimal(overrides.get(name, ORIGINAL_CHARGE))
            for name, placed in self.events.items()
            if placed.customer != INTERNAL
        }


@pytest.fixture
def scene(tmp_path, analytics_db) -> Scene:
    world = build_world(analytics_db)
    events = {
        "e1": Placed(
            world.add_event(project=A1, occurred_at=dt.datetime(2026, 11, 10, 4)), A, "2026-11"
        ),
        "e2": Placed(
            world.add_event(project=A2, occurred_at=dt.datetime(2026, 11, 20, 4)), A, "2026-11"
        ),
        "i1": Placed(
            world.add_event(project=I1, occurred_at=dt.datetime(2026, 11, 15, 4)),
            INTERNAL,
            "2026-11",
        ),
        "e3": Placed(
            world.add_event(project=A1, occurred_at=dt.datetime(2026, 12, 5, 4)), A, "2026-12"
        ),
        "e4": Placed(
            world.add_event(project=B1, occurred_at=dt.datetime(2026, 12, 10, 4)), B, "2026-12"
        ),
    }
    for name, placed in events.items():
        expected = Decimal(0) if placed.customer == INTERNAL else ORIGINAL_CHARGE
        assert placed.ref.billable_cost == expected, name
    application = create_app(settings_for(tmp_path))
    application.state.engine = analytics_db.kw["bind"]
    application.state.session_factory = analytics_db
    token = issue_access_token(
        application.state.settings,
        user_id=world.admin_id,
        role="ADMIN",
        session_id="0" * 32,
        now=utc_now(),
    )
    return Scene(world, TestClient(application), {"Authorization": f"Bearer {token}"}, events)


# --- 读 -------------------------------------------------------------------------------


def analytics(scene: Scene, group_by: str) -> dict[str, Any]:
    response = scene.client.get(
        PATH, params={"group_by": group_by, **WINDOW}, headers=scene.headers
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]


def export(scene: Scene, group_by: str, export_format: str = "csv") -> bytes:
    response = scene.client.get(
        EXPORT,
        params={"group_by": group_by, "format": export_format, **WINDOW},
        headers=scene.headers,
    )
    assert response.status_code == 200, response.text
    return response.content


def csv_rows(body: bytes) -> list[dict[str, str]]:
    assert body.startswith(BOM)
    table = list(csv.reader(io.StringIO(body[len(BOM) :].decode("utf-8"), newline="")))
    assert table[0] == list(USAGE_ANALYTICS_EXPORT_COLUMNS)
    return [dict(zip(USAGE_ANALYTICS_EXPORT_COLUMNS, row, strict=True)) for row in table[1:]]


def xlsx_values(body: bytes) -> list[tuple[Any, ...]]:
    workbook = openpyxl.load_workbook(io.BytesIO(body))
    try:
        return [tuple(row) for row in workbook[SHEET].iter_rows(values_only=True)]
    finally:
        workbook.close()


def xlsx_rows(body: bytes) -> list[dict[str, Any]]:
    values = xlsx_values(body)
    assert list(values[0]) == list(USAGE_ANALYTICS_EXPORT_COLUMNS)
    return [dict(zip(USAGE_ANALYTICS_EXPORT_COLUMNS, row, strict=True)) for row in values[1:]]


def repository_rows(factory: sessionmaker[Session], group_by: str) -> tuple[str, ...]:
    """The repository's grouped rows and totals as `str` of every field (Decimal 的指数也比)."""
    filters: dict[str, Any] = {
        "customer_id": None,
        "project_id": None,
        "provider": None,
        "model": None,
        "feature": None,
        "occurred_from": WINDOW_FROM,
        "occurred_to": WINDOW_TO,
    }
    with factory() as session:
        rows, total = analytics_repository.list_usage_analytics(
            session, group_by=group_by, offset=0, limit=100, **filters
        )
        totals = analytics_repository.usage_analytics_totals(session, **filters)
    return (
        str(total),
        *(repr(tuple(str(value) for value in row)) for row in rows),
        repr(tuple(str(value) for value in totals)),
    )


# --- 期望值（手算） ----------------------------------------------------------------------


@dataclass(frozen=True)
class Expected:
    event_count: int
    prepaid_event_count: int
    revenue: Decimal
    estimated: Decimal


def group_key_of(scene: Scene, group_by: str, placed: Placed) -> str:
    if group_by == "customer":
        return scene.world.customers[placed.customer].public_id
    if group_by == "month":
        return placed.month
    assert group_by == "model"
    return f"{PROVIDER_CODE}:{MODEL_CODE}"


def expected_groups(
    scene: Scene, group_by: str, charges: dict[str, Decimal]
) -> dict[str, Expected]:
    """收入 = Σ 当前有效计费（= 原计费 + 差额之和）；估算成本只算预付事件。"""
    groups: dict[str, Expected] = {}
    for name, placed in scene.events.items():
        key = group_key_of(scene, group_by, placed)
        prepaid = placed.customer != INTERNAL
        before = groups.get(key, Expected(0, 0, Decimal(0), Decimal(0)))
        groups[key] = Expected(
            before.event_count + 1,
            before.prepaid_event_count + (1 if prepaid else 0),
            before.revenue + (charges[name] if prepaid else Decimal(0)),
            before.estimated + (ESTIMATED if prepaid else Decimal(0)),
        )
    return groups


def expected_totals(scene: Scene, charges: dict[str, Decimal]) -> Expected:
    # 只有一个供应商、一个模型：按模型分组恰好一组，就是合计。
    (only,) = expected_groups(scene, "model", charges).values()
    return only


def assert_view(view: dict[str, Any], expected: Expected, baseline: dict[str, Any]) -> None:
    """One item or `totals` against the hand-computed values and its no-rebill baseline."""
    prepaid = view["prepaid"]
    margin = expected.revenue - expected.estimated
    assert view["event_count"] == expected.event_count
    assert prepaid["event_count"] == expected.prepaid_event_count
    assert prepaid["billable_cost"] == money(expected.revenue)
    assert prepaid["estimated_provider_cost_myr"] == money(expected.estimated)
    assert prepaid["gross_margin"] == money(margin)
    # 没有对账记录：供应商成本就是估算，基于它的毛利同样随收入变。
    assert prepaid["provider_cost_myr"] == money(expected.estimated)
    assert prepaid["gross_margin_on_provider_cost"] == money(margin)
    # 事件数、token、估算成本、对账后成本与内部计量块与没有 Rebill 时逐字相同。
    for key in (
        "event_count",
        "token_event_count",
        "input_tokens",
        "output_tokens",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
        "average_input_tokens",
        "average_output_tokens",
        "internal_metered_only",
    ):
        assert view[key] == baseline[key], key
    for key in (
        "event_count",
        "estimated_provider_cost_myr",
        "covered_estimated_cost_myr",
        "reconciled_provider_cost_myr",
        "provider_cost_myr",
        "provider_cost_basis",
    ):
        assert prepaid[key] == baseline["prepaid"][key], key


def assert_analytics(
    scene: Scene, charges: dict[str, Decimal], baselines: dict[str, dict[str, Any]]
) -> None:
    for group_by in CHECKED:
        data = analytics(scene, group_by)
        baseline = baselines[group_by]
        expected = expected_groups(scene, group_by, charges)
        items = {item["group_key"]: item for item in data["items"]}
        baseline_items = {item["group_key"]: item for item in baseline["items"]}
        assert set(items) == set(expected) == set(baseline_items), group_by
        assert data["total"] == baseline["total"] == len(expected)
        for key, item in items.items():
            assert_view(item, expected[key], baseline_items[key])
        totals = expected_totals(scene, charges)
        assert_view(data["totals"], totals, baseline["totals"])
        # 没有支付：手续费为 0，扣手续费后的毛利 = 用量毛利。
        assert data["totals"]["gross_profit"] == money(totals.revenue - totals.estimated)
        assert data["totals"]["gross_profit_on_provider_cost"] == money(
            totals.revenue - totals.estimated
        )


# --- 用例 -----------------------------------------------------------------------------


def test_one_and_two_rebills_move_revenue_and_margin_but_not_counts_or_costs(scene) -> None:
    world, events = scene.world, scene.events
    baselines = {group_by: analytics(scene, group_by) for group_by in CHECKED}
    assert_analytics(scene, scene.charges(), baselines)
    assert expected_totals(scene, scene.charges()).revenue == Decimal("18")

    # 第一次：e1 −1.25、e3 +1；客户 A 13.5 → 13.25，11 月 13.5 → 12.25，12 月 9 → 10。
    world.add_rebill(
        A,
        [
            LineSpec(events["e1"].ref, "3.25"),
            LineSpec(events["e3"].ref, "5.5"),
        ],
        created_at=dt.datetime(2026, 12, 20, 0, 0, 0),
    )
    once = scene.charges(e1="3.25", e3="5.5")
    assert_analytics(scene, once, baselines)
    assert analytics(scene, "customer")["totals"]["prepaid"]["billable_cost"] == "17.75000000"

    # 第二次：e1 再 +0.75（它现在有两条明细）；B 的 e4 −2。
    world.add_rebill(
        A, [LineSpec(events["e1"].ref, "4")], created_at=dt.datetime(2026, 12, 21, 0, 0, 0)
    )
    world.add_rebill(
        B, [LineSpec(events["e4"].ref, "2.5")], created_at=dt.datetime(2026, 12, 22, 0, 0, 0)
    )
    assert count_rows(world.factory, UsageRebill) == 3
    assert count_rows(world.factory, UsageRebillLine) == 4
    twice = scene.charges(e1="4", e3="5.5", e4="2.5")
    assert_analytics(scene, twice, baselines)

    # 手写的锚点：差额按原事件的客户与 occurred_at 所在的马来西亚月归属，
    # 不按 Rebill 的入账时刻（12 月 20–22 日）。
    by_customer = {item["group_key"]: item for item in analytics(scene, "customer")["items"]}
    a = by_customer[world.customers[A].public_id]
    assert (a["event_count"], a["prepaid"]["billable_cost"], a["prepaid"]["gross_margin"]) == (
        3,
        "14.00000000",
        "7.25000000",
    )
    b = by_customer[world.customers[B].public_id]
    assert (b["event_count"], b["prepaid"]["billable_cost"], b["prepaid"]["gross_margin"]) == (
        1,
        "2.50000000",
        "0.25000000",
    )
    by_month = {item["group_key"]: item for item in analytics(scene, "month")["items"]}
    assert {key: item["prepaid"]["billable_cost"] for key, item in by_month.items()} == {
        "2026-11": "8.50000000",
        "2026-12": "8.00000000",
    }
    assert {key: item["event_count"] for key, item in by_month.items()} == {
        "2026-11": 3,
        "2026-12": 2,
    }
    totals = analytics(scene, "model")["totals"]
    assert (
        totals["event_count"],
        totals["prepaid"]["billable_cost"],
        totals["prepaid"]["estimated_provider_cost_myr"],
        totals["prepaid"]["gross_margin"],
    ) == (5, "16.50000000", "9.00000000", "7.50000000")


def test_an_event_with_several_lines_is_counted_once(scene) -> None:
    world, events = scene.world, scene.events
    before = analytics(scene, "project")
    # 同一事件三条明细：4.5 → 3.25 → 4 → 1.5，差额之和 −3。
    for moment, charge in (
        (dt.datetime(2026, 12, 20), "3.25"),
        (dt.datetime(2026, 12, 21), "4"),
        (dt.datetime(2026, 12, 22), "1.5"),
    ):
        world.add_rebill(A, [LineSpec(events["e1"].ref, charge)], created_at=moment)
    assert count_rows(world.factory, UsageRebillLine) == 3
    after = analytics(scene, "project")
    items = {item["group_key"]: item for item in after["items"]}
    baseline = {item["group_key"]: item for item in before["items"]}
    a1 = world.projects[A1].public_id
    # A1 有 e1、e3：事件数与 token 不因三条明细变成三倍。
    assert_view(items[a1], Expected(2, 2, Decimal("6.0"), Decimal("4.5")), baseline[a1])
    assert items[a1]["input_tokens"] == baseline[a1]["input_tokens"] == 2000
    for key in set(items) - {a1}:
        assert items[key] == baseline[key], key
    assert_view(after["totals"], Expected(5, 4, Decimal("15"), Decimal("9")), before["totals"])


def test_internal_metered_events_are_not_affected(scene) -> None:
    world, events = scene.world, scene.events
    before = analytics(scene, "customer")
    world.add_rebill(A, [LineSpec(events["e2"].ref, "5")])
    after = analytics(scene, "customer")
    internal_key = world.customers[INTERNAL].public_id
    items = {item["group_key"]: item for item in after["items"]}
    baseline = {item["group_key"]: item for item in before["items"]}
    # 内部计量客户：预付块恒为 0、内部块（估算成本与参考价）不变。
    assert items[internal_key] == baseline[internal_key]
    assert items[internal_key]["prepaid"]["billable_cost"] == "0.00000000"
    assert items[internal_key]["internal_metered_only"]["reference_customer_price"] == "4.50000000"
    assert after["totals"]["internal_metered_only"] == before["totals"]["internal_metered_only"]
    assert after["totals"]["prepaid"]["billable_cost"] == "18.50000000"


@pytest.mark.parametrize("export_format", ["csv", "xlsx"])
def test_the_exports_include_the_differences_in_the_revenue_column(scene, export_format) -> None:
    world, events = scene.world, scene.events
    world.add_rebill(
        A,
        [LineSpec(events["e1"].ref, "3.25"), LineSpec(events["e3"].ref, "5.5")],
        created_at=dt.datetime(2026, 12, 20),
    )
    world.add_rebill(A, [LineSpec(events["e1"].ref, "4")], created_at=dt.datetime(2026, 12, 21))
    world.add_rebill(B, [LineSpec(events["e4"].ref, "2.5")], created_at=dt.datetime(2026, 12, 22))
    charges = scene.charges(e1="4", e3="5.5", e4="2.5")
    for group_by in CHECKED:
        body = export(scene, group_by, export_format)
        rows = csv_rows(body) if export_format == "csv" else xlsx_rows(body)
        *groups, total = rows
        assert total["row_type"] == "TOTAL"
        expected = expected_groups(scene, group_by, charges)
        by_key = {row["group_key"]: row for row in groups}
        assert set(by_key) == set(expected), group_by
        for key, row in by_key.items():
            margin = expected[key].revenue - expected[key].estimated
            assert row["row_type"] == "GROUP"
            assert int(row["event_count"]) == expected[key].event_count
            assert row["prepaid_billable_cost"] == money(expected[key].revenue)
            assert row["prepaid_estimated_provider_cost_myr"] == money(expected[key].estimated)
            assert row["prepaid_gross_margin"] == money(margin)
            assert row["prepaid_gross_margin_on_provider_cost"] == money(margin)
        assert total["prepaid_billable_cost"] == "16.50000000"
        assert total["prepaid_estimated_provider_cost_myr"] == "9.00000000"
        assert total["prepaid_gross_margin"] == "7.50000000"
        assert total["gross_profit"] == "7.50000000"
        # 与 JSON 接口同一口径。
        data = analytics(scene, group_by)
        assert total["prepaid_billable_cost"] == data["totals"]["prepaid"]["billable_cost"]
        for item in data["items"]:
            assert (
                by_key[item["group_key"]]["prepaid_billable_cost"]
                == (item["prepaid"]["billable_cost"])
            )


# --- 两张新表为空：与改动前逐位相同 ----------------------------------------------------------


def _legacy_aggregates(_differences: Any) -> list[ColumnElement[Any]]:
    """`_aggregates` exactly as it was before AIH-TASK-258 (收入只是 `billable_cost`)."""
    _sum = analytics_repository._sum
    token = UsageEvent.payload_shape == PayloadShape.LLM_TOKEN_FIELDS
    prepaid = UsageEvent.billing_mode_snapshot == BillingMode.PREPAID.value
    internal = UsageEvent.billing_mode_snapshot == BillingMode.INTERNAL_METERED_ONLY.value
    return [
        func.count(UsageEvent.id).label("event_count"),
        _sum(case((token, 1), else_=0)).label("token_event_count"),
        _sum(case((token, UsageEvent.input_tokens), else_=0)).label("input_tokens"),
        _sum(case((token, UsageEvent.output_tokens), else_=0)).label("output_tokens"),
        _sum(case((token, UsageEvent.cache_creation_input_tokens), else_=0)).label(
            "cache_creation_input_tokens"
        ),
        _sum(case((token, UsageEvent.cache_read_input_tokens), else_=0)).label(
            "cache_read_input_tokens"
        ),
        _sum(case((prepaid, 1), else_=0)).label("prepaid_event_count"),
        _sum(case((prepaid, UsageEvent.billable_cost), else_=0)).label("prepaid_billable_cost"),
        _sum(case((prepaid, UsageEvent.estimated_provider_cost_myr), else_=0)).label(
            "prepaid_estimated_cost"
        ),
        _sum(case((internal, 1), else_=0)).label("internal_event_count"),
        _sum(case((internal, UsageEvent.estimated_provider_cost_myr), else_=0)).label(
            "internal_estimated_cost"
        ),
        _sum(case((internal, UsageEvent.reference_customer_price), else_=0)).label(
            "internal_reference_price"
        ),
    ]


def _snapshot(scene: Scene) -> dict[str, Any]:
    """Every grouping's repository rows, JSON data, CSV bytes and xlsx cell values."""
    return {
        group_by: (
            repository_rows(scene.world.factory, group_by),
            analytics(scene, group_by),
            export(scene, group_by, "csv"),
            xlsx_values(export(scene, group_by, "xlsx")),
        )
        for group_by in GROUPINGS
    }


def test_with_both_rebill_tables_empty_everything_matches_the_query_before_the_change(
    scene, monkeypatch
) -> None:
    assert count_rows(scene.world.factory, UsageRebill) == 0
    assert count_rows(scene.world.factory, UsageRebillLine) == 0
    after = _snapshot(scene)
    with monkeypatch.context() as patch:
        # 改动前的查询写法：不连明细子查询，收入只是 `billable_cost`。
        patch.setattr(analytics_repository, "_aggregates", _legacy_aggregates)
        patch.setattr(
            analytics_repository,
            "_with_rebill_differences",
            lambda statement, _differences: statement,
        )
        before = _snapshot(scene)
    assert after.keys() == before.keys()
    for group_by in GROUPINGS:
        repository_after, data_after, csv_after, xlsx_after = after[group_by]
        repository_before, data_before, csv_before, xlsx_before = before[group_by]
        assert repository_after == repository_before, group_by
        assert data_after == data_before, group_by
        assert csv_after == csv_before, group_by
        assert xlsx_after == xlsx_before, group_by
    # 也与不含明细的手算结果相同。
    assert_analytics(scene, scene.charges(), {group_by: after[group_by][1] for group_by in CHECKED})

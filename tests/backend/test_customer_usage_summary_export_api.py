"""Customer monthly usage summary CSV export end to end (AIH-TASK-119, design gate #457 v1 §7).

`GET /api/v1/customer/usage-summary/export`：`month` 必填，复用 AIH-TASK-107 的
`summarize_processed_events_for_customer` 与 5 键视图，用 AIH-TASK-114 的 `write_csv` 与
`csv_response`；不占导出租约。§7 后端表里「路由守卫」在 test_customer_api.py（显式清单里加了
新路由），不在这里。

数据照 test_customer_usage_summary_api 的写法直接写库（`UsageEventPortal.put`）：
- SQLite：`create_all` 建表；
- MySQL：表与触发器都走迁移，需要 `BILLING_TEST_DATABASE_URL`，没设时 MySQL 那一半 skip ——
  **不要把 skipped 读成 passed**，CI 里不许 skip。

⚠️ 导出租约是进程级的：每条用例开始与结束时都必须空闲（`lease_is_free`）；本接口从不获取它。

⚠️ SQL 文本断言不写死方言字面量；只有 MySQL 才有的断言放在方言分支里。示例 uuid 用全零占位值。
"""

from __future__ import annotations

import csv
import datetime as dt
import functools
import io
import json
from collections.abc import Iterator
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker
from test_customer_usage_api import (
    UNBILLED,
    ZERO_UUID,
    UsageEventPortal,
    _usage_portal,
    record_statements,
    usage_statements,
)
from test_customer_usage_summary_api import (
    MINUTE,
    MONTH,
    OCT_1,
    OCT_15,
    SENTINEL_COSTS,
    SENTINEL_REFERENCE_PRICE,
    SENTINEL_TEXTS,
    ZERO,
    capture_server_log,
    is_mysql,
    month_dates,
    timeout_lines,
)
from test_customer_wallet_api import CONTEXT, _sqlite_factory, error_of
from test_usage_billing import _mysql_factory

import app.api.customer as customer_api
from app.core.csv_export import (
    export_lease_held,
    release_export_lease,
    try_acquire_export_lease,
)
from app.models.auth import UserRole
from app.models.tenancy import AccountStatus, BillingMode
from app.models.usage import UsageEvent
from app.repositories import usage_events as usage_repository
from app.repositories.usage_events import summarize_processed_events_for_customer
from app.schemas.customer_usage import (
    USAGE_SUMMARY_EXPORT_COLUMNS,
    USAGE_SUMMARY_EXPORT_TEXT_COLUMNS,
    USAGE_SUMMARY_EXPORT_TOTAL_LABEL,
    CustomerUsageSummaryDay,
    CustomerUsageSummaryView,
    customer_usage_summary_export_cells,
)
from app.services.portal_accounts import disable_portal_account

EXPORT = "/api/v1/customer/usage-summary/export"
SUMMARY = "/api/v1/customer/usage-summary"
BOM = b"\xef\xbb\xbf"

# 设计 §2「文件」：表头恰好这 4 列，按这个顺序。多一列、少一列、换顺序都红。
COLUMNS = ["date_myt", "request_count", "charged_amount", "conversation_count"]
HEADER_LINE = BOM + b"date_myt,request_count,charged_amount,conversation_count\r\n"


@pytest.fixture(autouse=True)
def lease_is_free() -> Iterator[None]:
    # 进程级租约：每条用例开始与结束时都必须空闲。
    assert not export_lease_held()
    yield
    assert not export_lease_held()


@pytest.fixture
def portal(tmp_path) -> Iterator[UsageEventPortal]:
    yield from _usage_portal(_sqlite_factory(), tmp_path)


@pytest.fixture(params=["sqlite", "mysql"])
def both(request, monkeypatch, tmp_path) -> Iterator[UsageEventPortal]:
    """The same cases on SQLite and on a real MySQL (design §7「SQLite 与 MySQL 参数化」)."""
    factories: Iterator[sessionmaker[Session]] = (
        _mysql_factory(monkeypatch) if request.param == "mysql" else _sqlite_factory()
    )
    yield from _usage_portal(factories, tmp_path)


@pytest.fixture
def mysql_portal(monkeypatch, tmp_path) -> Iterator[UsageEventPortal]:
    yield from _usage_portal(_mysql_factory(monkeypatch), tmp_path)


# --- 辅助 -------------------------------------------------------------------------------


def export(portal: UsageEventPortal, headers: dict[str, str], **params):
    return portal.client.get(EXPORT, params=params, headers=headers)


def read_csv(body: bytes) -> list[list[str]]:
    assert body.startswith(BOM)
    return list(csv.reader(io.StringIO(body[len(BOM) :].decode("utf-8"), newline="")))


def exported(portal: UsageEventPortal, headers: dict[str, str], **params) -> list[list[str]]:
    """The rows after the header of a 200 export (days, then TOTAL); the header is checked here."""
    response = export(portal, headers, **params)
    assert response.status_code == 200, response.text
    assert response.content.startswith(HEADER_LINE)
    table = read_csv(response.content)
    assert table[0] == COLUMNS
    return table[1:]


def busy_rows(rows: list[list[str]]) -> dict[str, list[str]]:
    """`{date_myt: row}` of the day rows that have events."""
    return {row[0]: row for row in rows[:-1] if row[1] != "0"}


def json_summary(portal: UsageEventPortal, headers: dict[str, str], month: str) -> dict:
    response = portal.client.get(SUMMARY, params={"month": month}, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["data"]


def assert_json_envelope(response) -> None:
    """Errors are the JSON envelope, never a CSV."""
    assert response.headers["content-type"].startswith("application/json")
    assert not response.content.startswith(BOM)
    assert "content-disposition" not in response.headers
    body = response.json()
    assert body["data"] is None
    assert body["request_id"]


# --- 正常路径 ---------------------------------------------------------------------------


def test_one_month_with_two_busy_days(both: UsageEventPortal) -> None:
    """§7「正常路径」：1 日 3 条、15 日 2 条，3 条带两个不同的 `conversation_id`、2 条没有。"""
    tenant = both.tenant()
    both.put(tenant, occurred_at=OCT_1, billable="0.0625", conversation_id="conv-1")
    both.put(tenant, occurred_at=OCT_1 + MINUTE, billable="0.125", conversation_id="conv-1")
    both.put(tenant, occurred_at=OCT_1 + 2 * MINUTE, billable="0.25")
    both.put(tenant, occurred_at=OCT_15, billable="0.5", conversation_id="conv-2")
    both.put(tenant, occurred_at=OCT_15 + MINUTE, billable="0.0625")
    headers = both.customer(tenant)
    before = both.tables()

    response = export(both, headers, month=MONTH)

    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "text/csv; charset=utf-8"
    assert (
        response.headers["content-disposition"]
        == 'attachment; filename="usage-summary_2026-10.csv"'
    )
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-accel-buffering"] == "no"
    assert response.headers["content-length"] == str(len(response.content))
    assert response.content.startswith(HEADER_LINE)
    assert response.content.endswith(b"\r\n")
    table = read_csv(response.content)
    assert table[0] == COLUMNS
    days, total = table[1:-1], table[-1]
    assert [row[0] for row in days] == month_dates(dt.date(2026, 10, 1), 31)
    assert busy_rows(table[1:]) == {
        "2026-10-01": ["2026-10-01", "3", "0.43750000", ""],
        "2026-10-15": ["2026-10-15", "2", "0.56250000", ""],
    }
    for row in days:
        assert row[3] == ""
        if row[0] not in ("2026-10-01", "2026-10-15"):
            assert row == [row[0], "0", ZERO, ""]
    assert total == ["TOTAL", "5", "1.00000000", "2"]
    assert both.tables() == before


def test_the_constants_are_the_designed_ones() -> None:
    """§7「常量」：恰好 §2 的 4 列、文本列只有 `date_myt`、合计标签为 `TOTAL`。"""
    assert USAGE_SUMMARY_EXPORT_COLUMNS == tuple(COLUMNS)
    assert USAGE_SUMMARY_EXPORT_TEXT_COLUMNS == frozenset({"date_myt"})
    assert USAGE_SUMMARY_EXPORT_TOTAL_LABEL == "TOTAL"


def test_every_row_matches_the_json_endpoint_for_the_same_month(both: UsageEventPortal) -> None:
    """§7「与 JSON 接口逐字一致」：每个日行与 JSON `days` 同序同名值逐字相等，合计行同理。"""
    tenant = both.tenant()
    amounts = ("0.0625", "0.125", "0.25", "0.5", "0.03125", "0")
    moments = (
        dt.datetime(2026, 9, 30, 16, 0, 0),
        OCT_1,
        OCT_15,
        OCT_15 + MINUTE,
        dt.datetime(2026, 10, 20, 9, 0, 0),
        dt.datetime(2026, 10, 31, 15, 59, 59, 999999),
    )
    for number, (moment, amount) in enumerate(zip(moments, amounts, strict=True)):
        both.put(
            tenant,
            occurred_at=moment,
            billable=amount,
            conversation_id=f"conv-{number % 3}" if number % 2 else None,
        )
    headers = both.customer(tenant)

    data = json_summary(both, headers, MONTH)
    rows = exported(both, headers, month=MONTH)

    days, total = rows[:-1], rows[-1]
    assert len(days) == len(data["days"])
    for row, day in zip(days, data["days"], strict=True):
        assert row[0] == day["date"]
        assert row[1] == str(day["request_count"])
        assert row[2] == day["charged_amount"]
        assert row[3] == ""
    assert total == [
        "TOTAL",
        str(data["request_count"]),
        data["charged_amount"],
        str(data["conversation_count"]),
    ]
    assert total[2] == "0.96875000"


@pytest.mark.parametrize(
    ("month", "days"), [("2026-02", 28), ("2028-02", 29), ("2026-04", 30), ("2026-12", 31)]
)
def test_one_row_per_day_of_the_month(portal: UsageEventPortal, month: str, days: int) -> None:
    """§7「天数」：日行 28 / 29（闰年）/ 30 / 31 个，总行数 = 天数 + 2。"""
    tenant = portal.tenant()
    headers = portal.customer(tenant)

    response = export(portal, headers, month=month)

    assert response.status_code == 200
    table = read_csv(response.content)
    assert len(table) == days + 2
    first = dt.date(int(month[:4]), int(month[5:]), 1)
    assert [row[0] for row in table[1:-1]] == month_dates(first, days)
    assert table[-1][0] == "TOTAL"
    assert (
        response.headers["content-disposition"]
        == f'attachment; filename="usage-summary_{month}.csv"'
    )


def test_the_month_is_utc_16_00_to_16_00(both: UsageEventPortal) -> None:
    """§7「月份边界」：只计中间两条，分别在 `2026-10-01` 与 `2026-10-31` 行。"""
    tenant = both.tenant()
    for moment in (
        dt.datetime(2026, 9, 30, 15, 59, 59, 999999),
        dt.datetime(2026, 9, 30, 16, 0, 0),
        dt.datetime(2026, 10, 31, 15, 59, 59, 999999),
        dt.datetime(2026, 10, 31, 16, 0, 0),
    ):
        both.put(tenant, occurred_at=moment)
    headers = both.customer(tenant)

    rows = exported(both, headers, month=MONTH)

    assert busy_rows(rows) == {
        "2026-10-01": ["2026-10-01", "1", "0.06250000", ""],
        "2026-10-31": ["2026-10-31", "1", "0.06250000", ""],
    }
    assert rows[-1] == ["TOTAL", "2", "0.12500000", "0"]


def test_only_billed_events_are_counted(both: UsageEventPortal) -> None:
    """§7「只算已计费」：同月的未计费 / 错误状态事件不计入任何一行。"""
    tenant = both.tenant()
    both.put(tenant, occurred_at=OCT_1, conversation_id="conv-billed")
    for number, status in enumerate(UNBILLED, start=1):
        both.put(
            tenant,
            status=status,
            occurred_at=OCT_15 + number * MINUTE,
            conversation_id=f"conv-unbilled-{number}",
        )
    headers = both.customer(tenant)

    rows = exported(both, headers, month=MONTH)

    assert busy_rows(rows) == {"2026-10-01": ["2026-10-01", "1", "0.06250000", ""]}
    assert rows[-1] == ["TOTAL", "1", "0.06250000", "1"]


def test_a_conversation_is_counted_once_and_only_on_the_total_row(
    both: UsageEventPortal,
) -> None:
    """§7「对话计数」：同一 `conversation_id` 在 10 月 3 天各有事件 → 合计 1，日行该列都为空。"""
    tenant = both.tenant()
    for number in range(3):
        both.put(
            tenant, occurred_at=OCT_1 + number * dt.timedelta(days=5), conversation_id="conv-1"
        )
    headers = both.customer(tenant)

    rows = exported(both, headers, month=MONTH)

    assert len(busy_rows(rows)) == 3
    assert all(row[3] == "" for row in rows[:-1])
    assert rows[-1] == ["TOTAL", "3", "0.18750000", "1"]


# --- 参数规则与空月 -------------------------------------------------------------------


@pytest.mark.parametrize(
    "month", [None, "2026-13", "2026-00", "2026-1", "2019-12", "2100-01", "abc"]
)
def test_a_missing_or_bad_month_is_422_without_echoing_the_value(
    portal: UsageEventPortal, month: str | None
) -> None:
    """§7「参数规则」：缺失、形状错、月份数或越界 → 422 `VALIDATION_ERROR`。

    只列字段名，JSON 信封。
    """
    tenant = portal.tenant()
    portal.put(tenant, occurred_at=OCT_1)
    headers = portal.customer(tenant)
    before = portal.tables()
    params = {} if month is None else {"month": month}

    response = export(portal, headers, **params)

    assert error_of(response) == (422, "VALIDATION_ERROR")
    assert_json_envelope(response)
    message = response.json()["error"]["message"]
    assert message == "Invalid request fields: query.month"
    if month is not None:
        assert month not in message
    assert portal.tables() == before


@pytest.mark.parametrize(("month", "days"), [("2020-01", 31), ("2099-12", 31), ("2027-06", 30)])
def test_the_edges_and_future_months_are_200_and_zero(
    portal: UsageEventPortal, month: str, days: int
) -> None:
    """§7「参数规则」：`2020-01`、`2099-12` 与未来月份为 200，全为 0。"""
    tenant = portal.tenant()
    portal.put(tenant, occurred_at=OCT_1, conversation_id="conv-1")
    headers = portal.customer(tenant)

    rows = exported(portal, headers, month=month)

    first = dt.date(int(month[:4]), int(month[5:]), 1)
    assert rows == [[date, "0", ZERO, ""] for date in month_dates(first, days)] + [
        ["TOTAL", "0", ZERO, "0"]
    ]


def test_an_empty_month(both: UsageEventPortal) -> None:
    """§7「空月」：日行全为 `0` / `0.00000000`，合计 `TOTAL,0,0.00000000,0`。"""
    tenant = both.tenant()
    both.put(tenant, occurred_at=dt.datetime(2026, 9, 10, 2, 0, 0), conversation_id="conv-1")
    headers = both.customer(tenant)

    response = export(both, headers, month=MONTH)

    assert response.status_code == 200
    rows = read_csv(response.content)[1:]
    assert len(rows) == 32
    assert all(row[1:] == ["0", ZERO, ""] for row in rows[:-1])
    assert rows[-1] == ["TOTAL", "0", ZERO, "0"]
    assert response.content.endswith(b"\r\nTOTAL,0,0.00000000,0\r\n")


# --- 金额精度 ---------------------------------------------------------------------------


def test_tiny_amounts_add_up_exactly(both: UsageEventPortal) -> None:
    """§7「金额精度」（INV-10）：三条 `0.00000001` 合计 `0.00000003`。

    SQLite 把 NUMERIC 存成浮点、钱包连扣三笔过不了算术约束：那里先按精确的二进制小数记账，再把
    事件的计费额直接改成 `0.00000001`（汇总只读 `usage_events`）；MySQL 上照常记账。
    """
    tenant = both.tenant()
    for number in range(3):
        occurred_at = OCT_1 + number * MINUTE
        if is_mysql(both):
            both.put(tenant, occurred_at=occurred_at, billable="0.00000001")
            continue
        stored = both.put(tenant, occurred_at=occurred_at)
        with both.factory() as session:
            session.execute(
                update(UsageEvent)
                .where(UsageEvent.id == stored.row_id)
                .values(billable_cost=Decimal("0.00000001"))
            )
            session.commit()
    headers = both.customer(tenant)

    response = export(both, headers, month=MONTH)

    assert response.status_code == 200
    rows = read_csv(response.content)[1:]
    assert busy_rows(rows) == {"2026-10-01": ["2026-10-01", "3", "0.00000003", ""]}
    assert rows[-1] == ["TOTAL", "3", "0.00000003", "0"]
    assert b",0.00000003," in response.content


def test_the_largest_amount_plus_the_smallest_does_not_overflow(both: UsageEventPortal) -> None:
    """§7「金额精度」：同日 `999999999999.99999999` 与 `0.00000001` 得 `1000000000000.00000000`。

    先给钱包记 1：两笔扣费加起来会超出余额列的下界。
    """
    tenant = both.tenant()
    both.world.fund(tenant, "1")
    both.put(tenant, occurred_at=OCT_1, billable="999999999999.99999999")
    both.put(tenant, occurred_at=OCT_1 + MINUTE, billable="0.00000001")
    headers = both.customer(tenant)

    response = export(both, headers, month=MONTH)

    assert response.status_code == 200
    rows = read_csv(response.content)[1:]
    assert busy_rows(rows) == {"2026-10-01": ["2026-10-01", "2", "1000000000000.00000000", ""]}
    assert rows[-1] == ["TOTAL", "2", "1000000000000.00000000", "0"]
    assert b"2026-10-01,2,1000000000000.00000000,\r\n" in response.content
    assert b"TOTAL,2,1000000000000.00000000,0\r\n" in response.content


# --- 扫描上限与超时映射 ---------------------------------------------------------------


def test_a_scan_past_the_budget_is_query_too_broad(
    mysql_portal: UsageEventPortal, monkeypatch
) -> None:
    """§7「扫描上限」（MySQL）：当月 30 000 条已计费事件、测试专用 1 毫秒超时。

    422 `QUERY_TOO_BROAD`、JSON 信封；一条 `customer_usage_query_timeout` 日志，不带 `month` 的值。
    默认 2 秒预算下照常导出。
    """
    portal = mysql_portal
    tenant = portal.tenant()
    # 马来西亚 10 月 1 日 00:00 起每秒一条：全部落在 10 月 1 日。
    portal.bulk(tenant, 30_000, dt.datetime(2026, 9, 30, 16, 0, 0))
    headers = portal.customer(tenant)

    rows = exported(portal, headers, month=MONTH)
    assert busy_rows(rows) == {"2026-10-01": ["2026-10-01", "30000", ZERO, ""]}
    assert rows[-1] == ["TOTAL", "30000", ZERO, "0"]

    monkeypatch.setattr(
        usage_repository,
        "summarize_processed_events_for_customer",
        functools.partial(summarize_processed_events_for_customer, timeout_ms=1),
    )
    response, lines = capture_server_log(lambda: export(portal, headers, month=MONTH))

    assert error_of(response) == (422, "QUERY_TOO_BROAD")
    assert_json_envelope(response)
    [timeout] = timeout_lines(lines)
    assert timeout["request_id"] == response.json()["request_id"]
    assert timeout["customer_id"] == tenant.public_id
    assert MONTH not in json.dumps(timeout)


def test_query_too_broad_is_a_422_with_one_warning_and_no_parameter_values(
    portal: UsageEventPortal, monkeypatch
) -> None:
    """超时映射与 `GET /usage-summary` 相同：一条日志，日志里不带 `month` 的值，不写任何表。"""
    tenant = portal.tenant()
    portal.put(tenant, occurred_at=OCT_1)
    headers = portal.customer(tenant)
    before = portal.tables()

    def too_broad(*_args, **_kwargs):
        raise usage_repository.QueryTooBroad

    monkeypatch.setattr(usage_repository, "summarize_processed_events_for_customer", too_broad)
    response, lines = capture_server_log(lambda: export(portal, headers, month="2026-07"))

    assert error_of(response) == (422, "QUERY_TOO_BROAD")
    assert_json_envelope(response)
    [timeout] = timeout_lines(lines)
    assert timeout["level"] == "WARNING"
    assert timeout["request_id"] == response.json()["request_id"]
    assert timeout["customer_id"] == tenant.public_id
    assert "2026-07" not in json.dumps(lines)
    assert portal.tables() == before


def test_the_executed_queries_are_the_two_summary_selects(both: UsageEventPortal) -> None:
    """§2「查询」：不另写 SQL，发出的就是汇总函数的两条 SELECT；MySQL 上都带 2 秒提示。"""
    tenant = both.tenant()
    both.put(tenant, occurred_at=OCT_1)
    headers = both.customer(tenant)
    engine = both.app.state.engine
    statements, record = record_statements(engine)
    try:
        assert exported(both, headers, month=MONTH)[-1][1] == "1"
    finally:
        event.remove(engine, "before_cursor_execute", record)

    queries = usage_statements(statements)
    assert len(queries) == 2
    for query in queries:
        assert "FOR UPDATE" not in query.upper()
        if engine.dialect.name == "mysql":
            assert "MAX_EXECUTION_TIME(2000)" in query
        else:
            assert "MAX_EXECUTION_TIME" not in query


# --- 不占导出租约 -----------------------------------------------------------------------


def test_the_export_lease_is_neither_taken_nor_needed(portal: UsageEventPortal) -> None:
    """§7「不占导出租约」：测试先占住租约，本接口照常 200；租约仍由测试持有、未被释放或替换。"""
    tenant = portal.tenant()
    portal.put(tenant, occurred_at=OCT_1, conversation_id="conv-1")
    headers = portal.customer(tenant)
    holder = try_acquire_export_lease()
    assert holder is not None
    try:
        rows = exported(portal, headers, month=MONTH)
        assert rows[-1] == ["TOTAL", "1", "0.06250000", "1"]
        assert export_lease_held()
        assert try_acquire_export_lease() is None
    finally:
        release_export_lease(holder)
    # 用测试自己的 token 释放后就空闲了：说明期间没有被别的 token 替换。
    assert not export_lease_held()

    assert exported(portal, headers, month=MONTH)[-1] == ["TOTAL", "1", "0.06250000", "1"]
    assert not export_lease_held()


def test_the_handler_never_asks_for_the_lease(portal: UsageEventPortal, monkeypatch) -> None:
    """§2「不占导出租约」：处理函数根本不调 `try_acquire_export_lease`。"""
    tenant = portal.tenant()
    portal.put(tenant, occurred_at=OCT_1)
    headers = portal.customer(tenant)
    calls: list[object] = []

    def spy():
        calls.append(None)
        return try_acquire_export_lease()

    monkeypatch.setattr(customer_api, "try_acquire_export_lease", spy)

    assert exported(portal, headers, month=MONTH)[-1][1] == "1"
    assert calls == []


# --- 内部计量、无成本毛利、租户越权 ------------------------------------------------------


def test_an_internal_metered_tenant_is_counted_at_zero(both: UsageEventPortal) -> None:
    """§7「内部计量租户」（INV-7）：合计计费额 `0.00000000`，请求数照常；文件里没有参考价。"""
    tenant = both.tenant()
    both.set_billing_mode(tenant, BillingMode.INTERNAL_METERED_ONLY)
    for number in range(2):
        both.put(
            tenant,
            occurred_at=OCT_1 + number * MINUTE,
            billable="0",
            mode=BillingMode.INTERNAL_METERED_ONLY,
            reference=SENTINEL_REFERENCE_PRICE,
            conversation_id="conv-internal",
        )
    headers = both.customer(tenant)

    response = export(both, headers, month=MONTH)

    assert response.status_code == 200
    rows = read_csv(response.content)[1:]
    assert busy_rows(rows) == {"2026-10-01": ["2026-10-01", "2", ZERO, ""]}
    assert rows[-1] == ["TOTAL", "2", ZERO, "1"]
    for value in SENTINEL_TEXTS:
        assert value.encode() not in response.content


def test_no_cost_rate_or_reference_price_leaks(both: UsageEventPortal) -> None:
    """§7「无成本毛利」（INV-7、INV-9）：哨兵值、`conversation_id` 值与内部 id 都不在原始字节里。"""
    tenant = both.tenant()
    both.put(tenant, occurred_at=OCT_1, costs=SENTINEL_COSTS, conversation_id="conv-secret-0000")
    both.put(tenant, occurred_at=OCT_15, costs=SENTINEL_COSTS, request_id="req-secret-0000")
    headers = both.customer(tenant)

    response = export(both, headers, month=MONTH)

    assert response.status_code == 200
    forbidden = (
        *SENTINEL_TEXTS,
        "conv-secret-0000",
        "req-secret-0000",
        "cost",
        "fx_rate",
        "reference",
        "margin",
        "tokens",
        "tenant_id",
        "public_id",
        "event_id",
        "conversation_id",
        "status",
        tenant.public_id,
    )
    for value in forbidden:
        assert value.encode() not in response.content, value


def test_a_customer_only_ever_exports_its_own_summary(both: UsageEventPortal) -> None:
    """§7「租户越权」（INV-8）：A、B 同月都有事件、共用同一个 `conversation_id` 值。

    A 带 B 的标识导出只得到 A 的数字；A 的对话数不因 B 的同名对话变化；B 的值不在文件里。
    """
    mine = both.tenant()
    theirs = both.tenant()
    both.put(mine, occurred_at=OCT_1, conversation_id="conv-shared")
    both.put(mine, occurred_at=OCT_15, conversation_id="conv-shared", billable="0.125")
    for number in range(3):
        both.put(
            theirs,
            occurred_at=OCT_1 + number * MINUTE,
            conversation_id="conv-shared" if number else "conv-theirs",
            billable="0.5",
        )
    headers = both.customer(mine)
    forged = {"customer_id": theirs.public_id, "tenant_id": str(theirs.id), "project_id": ZERO_UUID}

    plain = export(both, headers, month=MONTH)
    response = export(both, headers, month=MONTH, **forged)

    assert response.status_code == 200
    assert response.content == plain.content
    rows = read_csv(response.content)[1:]
    assert busy_rows(rows) == {
        "2026-10-01": ["2026-10-01", "1", "0.06250000", ""],
        "2026-10-15": ["2026-10-15", "1", "0.12500000", ""],
    }
    assert rows[-1] == ["TOTAL", "2", "0.18750000", "1"]
    theirs_rows = exported(both, both.customer(theirs), month=MONTH)
    assert theirs_rows[-1] == ["TOTAL", "3", "1.50000000", "2"]
    for value in (theirs.public_id, "conv-shared", "conv-theirs", "1.50000000", "0.50000000"):
        assert value.encode() not in response.content, value


# --- 鉴权、只读契约、失败 ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("caller", "expected"),
    [
        ("anonymous", (401, "TOKEN_INVALID")),
        ("bad", (401, "TOKEN_INVALID")),
        ("admin", (403, "CUSTOMER_REQUIRED")),
        # 契约勘误：非 ACTIVE 用户由 `require_current_user` 拒绝，是 401。
        ("disabled", (401, "TOKEN_INVALID")),
        ("no_tenant", (403, "CUSTOMER_REQUIRED")),
    ],
)
def test_non_customers_are_refused_before_any_summary_query(
    portal: UsageEventPortal, caller: str, expected: tuple[int, str]
) -> None:
    """§7「鉴权」：拒绝的同时一条汇总查询都没发。"""
    tenant = portal.tenant()
    portal.put(tenant, occurred_at=OCT_1)
    if caller == "anonymous":
        headers: dict[str, str] = {}
    elif caller == "bad":
        headers = {"Authorization": "Bearer not-a-token"}
    elif caller == "admin":
        headers = portal.admin_caller()
    elif caller == "disabled":
        headers = portal.customer(tenant)
        disable_portal_account(
            portal.factory,
            actor=portal.world.admin,
            customer_id=tenant.public_id,
            context=CONTEXT,
        )
    else:
        orphan = portal.make_customer_user(None)
        headers = portal.token(orphan.id, UserRole.CUSTOMER)
    engine = portal.app.state.engine
    statements, record = record_statements(engine)
    try:
        response = export(portal, headers, month=MONTH)
    finally:
        event.remove(engine, "before_cursor_execute", record)

    assert error_of(response) == expected
    assert_json_envelope(response)
    assert usage_statements(statements) == []


def test_a_missing_month_is_judged_after_authentication(portal: UsageEventPortal) -> None:
    """§2：`month` 必填在鉴权之后判：没有令牌又没有 `month` 是 401，不是 422。"""
    response = portal.client.get(EXPORT)

    assert error_of(response) == (401, "TOKEN_INVALID")


@pytest.mark.parametrize(
    "status",
    [
        AccountStatus.PENDING_ACTIVATION,
        AccountStatus.ENABLED,
        AccountStatus.DISABLED,
        AccountStatus.CLOSED,
    ],
)
def test_the_read_only_contract(both: UsageEventPortal, status: AccountStatus) -> None:
    """§7「只读契约」「无写入」：四种账户状态下都 200，且不写任何表。"""
    tenant = both.tenant()
    both.put(tenant, occurred_at=OCT_1)
    both.set_account_status(tenant, status)
    headers = both.customer(tenant)
    before = both.tables()

    rows = exported(both, headers, month=MONTH)

    assert rows[-1] == ["TOTAL", "1", "0.06250000", "0"]
    assert both.tables() == before


def test_the_same_request_twice_returns_the_same_bytes(portal: UsageEventPortal) -> None:
    """§5「重复请求」：期间没有新计费的事件时文件逐字节相同。"""
    tenant = portal.tenant()
    portal.put(tenant, occurred_at=OCT_1, conversation_id="conv-1")
    headers = portal.customer(tenant)

    first = export(portal, headers, month=MONTH)
    second = export(portal, headers, month=MONTH)

    assert first.status_code == second.status_code == 200
    assert first.content == second.content


def test_a_database_failure_is_the_generic_500(portal: UsageEventPortal, monkeypatch) -> None:
    """§7「数据库断连」：汇总函数抛 `OperationalError` → 500 `INTERNAL_ERROR`，与 JSON 接口同形。

    不经 `CustomerExportFailed`，由通用 500 处理；之后正常请求 200。
    """
    tenant = portal.tenant()
    portal.put(tenant, occurred_at=OCT_1)
    headers = portal.customer(tenant)
    before = portal.tables()
    client = TestClient(portal.app, raise_server_exceptions=False)

    def broken(*_args, **_kwargs):
        raise OperationalError("SELECT", {}, Exception("fictional database failure"))

    with monkeypatch.context() as patch:
        patch.setattr(usage_repository, "summarize_processed_events_for_customer", broken)
        response = client.get(EXPORT, params={"month": MONTH}, headers=headers)
        summary = client.get(SUMMARY, params={"month": MONTH}, headers=headers)

    assert error_of(response) == (500, "INTERNAL_ERROR")
    assert_json_envelope(response)
    assert "fictional database failure" not in response.text
    assert error_of(summary) == (500, "INTERNAL_ERROR")
    assert response.json()["error"] == summary.json()["error"]
    assert set(response.json()) == set(summary.json())
    assert portal.tables() == before

    assert exported(portal, headers, month=MONTH)[-1][1] == "1"


def test_the_json_summary_is_unchanged(portal: UsageEventPortal) -> None:
    """`GET /usage-summary` 照旧：`month` 仍可省略（默认当月），新接口不改它的行为。"""
    tenant = portal.tenant()
    portal.put(tenant, occurred_at=OCT_1)
    headers = portal.customer(tenant)

    response = portal.client.get(SUMMARY, headers=headers)

    assert response.status_code == 200
    assert set(response.json()["data"]) == {
        "month",
        "charged_amount",
        "request_count",
        "conversation_count",
        "days",
    }
    assert json_summary(portal, headers, MONTH)["request_count"] == 1


# --- 单元格转换（unit） ---------------------------------------------------------------


def _view(first: dt.date, count: int) -> CustomerUsageSummaryView:
    """A view whose totals are deliberately NOT the days' sums: the TOTAL row must copy them."""
    return CustomerUsageSummaryView(
        month=f"{first:%Y-%m}",
        charged_amount="123.45678901",
        request_count=999,
        conversation_count=7,
        days=[
            CustomerUsageSummaryDay(
                date=first + dt.timedelta(days=n),
                request_count=n,
                charged_amount=f"{n}.00000001",
            )
            for n in range(count)
        ],
    )


@pytest.mark.parametrize(
    ("first", "count"), [(dt.date(2026, 2, 1), 28), (dt.date(2026, 10, 1), 31)]
)
def test_the_cells_come_from_the_view_only(first: dt.date, count: int) -> None:
    """§7「单元格转换」：行数 = 天数 + 1；只有 `str` / `None`；末行取的是视图的合计字段。"""
    view = _view(first, count)

    rows = customer_usage_summary_export_cells(view)

    assert len(rows) == count + 1
    for row in rows:
        assert len(row) == len(COLUMNS)
        assert all(cell is None or type(cell) is str for cell in row)
    for n, row in enumerate(rows[:-1]):
        assert row == [(first + dt.timedelta(days=n)).isoformat(), str(n), f"{n}.00000001", None]
    # 各日之和分别是 count*(count-1)/2 与另一个金额；合计行不是它们，而是视图里的值。
    assert rows[-1] == ["TOTAL", "999", "123.45678901", "7"]
    assert str(sum(range(count))) != rows[-1][1]

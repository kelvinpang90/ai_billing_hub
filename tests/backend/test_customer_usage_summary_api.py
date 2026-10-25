"""Customer monthly usage summary end to end (AIH-TASK-107, design gate #403 v1 §7).

`GET /api/v1/customer/usage-summary`：只读、租户只取自会话。§7 后端表格里「路由守卫」在
test_customer_api.py（显式清单里加了新路由），不在这里。

用量事件照 test_customer_usage_api 的 `UsageEventPortal.put` 直接按库里的约束写成各种状态；
计费额都是精确的二进制小数（SQLite 把 NUMERIC 存成浮点）。
- SQLite：`create_all` 建表；
- MySQL：表与触发器都走迁移，需要 `BILLING_TEST_DATABASE_URL`，没设时 MySQL 那一半 skip ——
  **不要把 skipped 读成 passed**，CI 里不许 skip。

⚠️ 鉴权一行沿用 AIH-TASK-104 的勘误：门户账号被停用后的旧令牌是 401 `TOKEN_INVALID`。

⚠️ SQL 文本断言不写死方言字面量；只有 MySQL 才有的断言放在方言分支里（或只对 MySQL 方言编译）。
示例 uuid 用全零占位值。
"""

from __future__ import annotations

import datetime as dt
import functools
import io
import json
import logging
import re
from collections.abc import Iterator
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.dialects import mysql, sqlite
from sqlalchemy.exc import OperationalError
from test_customer_usage_api import (
    UNBILLED,
    UsageEventPortal,
    _usage_portal,
    record_statements,
    usage_statements,
)
from test_customer_wallet_api import CONTEXT, _sqlite_factory, error_of
from test_usage_billing import _mysql_factory

import app.api.customer as customer_api
from app.core.logging import JsonFormatter
from app.models.auth import UserRole
from app.models.tenancy import AccountStatus, BillingMode
from app.repositories import usage_events as usage_repository
from app.repositories.usage_events import (
    CUSTOMER_USAGE_QUERY_TIMEOUT_MS,
    CustomerUsageDay,
    QueryTooBroad,
    customer_usage_conversation_statement,
    customer_usage_daily_statement,
    summarize_processed_events_for_customer,
)
from app.services.portal_accounts import disable_portal_account

SUMMARY = "/api/v1/customer/usage-summary"
USAGE = "/api/v1/customer/usage-events"

# 设计 §2：`data` 恰好 5 个键，`days` 每项恰好 3 个键。多一个就红。
SUMMARY_FIELDS = {"month", "charged_amount", "request_count", "conversation_count", "days"}
DAY_FIELDS = {"date", "request_count", "charged_amount"}

MONTH = "2026-10"
# 2026-10（MYT）= UTC [2026-09-30 16:00, 2026-10-31 16:00)。
OCTOBER = {
    "occurred_from": dt.datetime(2026, 9, 30, 16, 0, 0),
    "occurred_to": dt.datetime(2026, 10, 31, 16, 0, 0),
}
# 马来西亚 10 月 1 日、15 日的上午。
OCT_1 = dt.datetime(2026, 10, 1, 2, 0, 0)
OCT_15 = dt.datetime(2026, 10, 15, 2, 0, 0)
MINUTE = dt.timedelta(minutes=1)
ZERO = "0.00000000"
ZERO_UUID = "00000000-0000-0000-0000-000000000000"

# INV-7 的哨兵：写进事件，断言它们不出现在原始响应文本里。
SENTINEL_COSTS = {
    "provider_source_cost": Decimal("0.13579246"),
    "estimated_provider_cost_myr": Decimal("0.24681357"),
    "fx_rate_applied": Decimal("0.97531864"),
}
SENTINEL_REFERENCE_PRICE = Decimal("0.86420975")
SENTINEL_TEXTS = ("0.13579246", "0.24681357", "0.97531864", "0.86420975")
COST_COLUMNS = (
    "provider_source_cost",
    "estimated_provider_cost_myr",
    "fx_rate_applied",
    "reference_customer_price",
    "provider_source_currency",
)


@pytest.fixture
def portal(tmp_path) -> Iterator[UsageEventPortal]:
    yield from _usage_portal(_sqlite_factory(), tmp_path)


@pytest.fixture(params=["sqlite", "mysql"])
def both(request, monkeypatch, tmp_path) -> Iterator[UsageEventPortal]:
    """The same cases on SQLite and on a real MySQL (design §7「SQLite + MySQL 参数化」)."""
    factories = _mysql_factory(monkeypatch) if request.param == "mysql" else _sqlite_factory()
    yield from _usage_portal(factories, tmp_path)


@pytest.fixture
def mysql_portal(monkeypatch, tmp_path) -> Iterator[UsageEventPortal]:
    yield from _usage_portal(_mysql_factory(monkeypatch), tmp_path)


def get_summary(portal: UsageEventPortal, headers: dict[str, str], **params):
    return portal.client.get(SUMMARY, params=params, headers=headers)


def summary(portal: UsageEventPortal, headers: dict[str, str], **params) -> dict:
    response = get_summary(portal, headers, **params)
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert set(data) == SUMMARY_FIELDS
    assert all(set(day) == DAY_FIELDS for day in data["days"])
    return data


def busy_days(data: dict) -> dict[str, tuple[int, str]]:
    """`{date: (request_count, charged_amount)}` of the days that have events."""
    return {
        day["date"]: (day["request_count"], day["charged_amount"])
        for day in data["days"]
        if day["request_count"]
    }


def month_dates(first: dt.date, count: int) -> list[str]:
    return [(first + dt.timedelta(days=n)).isoformat() for n in range(count)]


def is_mysql(portal: UsageEventPortal) -> bool:
    return portal.factory.kw["bind"].dialect.name == "mysql"


def capture_server_log(run) -> tuple[object, list[dict]]:
    """Run `run()` with a JSON handler on the root logger; the `app.*` lines it produced."""
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        result = run()
    finally:
        root.removeHandler(handler)
    # 只看服务端（`app.*`）的日志：测试客户端 httpx 自己会把完整 URL 记在 INFO 上。
    lines = [
        line
        for line in (json.loads(raw) for raw in stream.getvalue().splitlines() if raw.strip())
        if str(line["logger"]).startswith("app.")
    ]
    return result, lines


def timeout_lines(lines: list[dict]) -> list[dict]:
    return [line for line in lines if line["message"] == "customer_usage_query_timeout"]


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

    data = summary(both, headers, month=MONTH)

    assert data["month"] == MONTH
    assert data["request_count"] == 5
    assert data["conversation_count"] == 2
    assert data["charged_amount"] == "1.00000000"
    assert [day["date"] for day in data["days"]] == month_dates(dt.date(2026, 10, 1), 31)
    assert busy_days(data) == {
        "2026-10-01": (3, "0.43750000"),
        "2026-10-15": (2, "0.56250000"),
    }
    for day in data["days"]:
        if day["date"] not in ("2026-10-01", "2026-10-15"):
            assert (day["request_count"], day["charged_amount"]) == (0, ZERO)
    assert sum(day["request_count"] for day in data["days"]) == data["request_count"]
    assert sum(Decimal(day["charged_amount"]) for day in data["days"]) == Decimal(
        data["charged_amount"]
    )
    assert both.tables() == before


def test_the_month_is_utc_16_00_to_16_00(both: UsageEventPortal) -> None:
    """§7「月份边界」：只计中间两条，分别归到 10 月 1 日与 10 月 31 日。"""
    tenant = both.tenant()
    for moment in (
        dt.datetime(2026, 9, 30, 15, 59, 59, 999999),
        dt.datetime(2026, 9, 30, 16, 0, 0),
        dt.datetime(2026, 10, 31, 15, 59, 59, 999999),
        dt.datetime(2026, 10, 31, 16, 0, 0),
    ):
        both.put(tenant, occurred_at=moment)
    headers = both.customer(tenant)

    october = summary(both, headers, month=MONTH)
    september = summary(both, headers, month="2026-09")
    november = summary(both, headers, month="2026-11")

    assert october["request_count"] == 2
    assert busy_days(october) == {
        "2026-10-01": (1, "0.06250000"),
        "2026-10-31": (1, "0.06250000"),
    }
    assert busy_days(september) == {"2026-09-30": (1, "0.06250000")}
    assert len(september["days"]) == 30
    assert busy_days(november) == {"2026-11-01": (1, "0.06250000")}
    assert len(november["days"]) == 30


def test_the_malaysian_day_is_the_same_on_both_dialects(both: UsageEventPortal) -> None:
    """§7「日期归属两边一致」：UTC 10-01 15:59:59 → 马来西亚 10 月 1 日；16:00:00 → 10 月 2 日。"""
    tenant = both.tenant()
    both.put(tenant, occurred_at=dt.datetime(2026, 10, 1, 15, 59, 59))
    both.put(tenant, occurred_at=dt.datetime(2026, 10, 1, 16, 0, 0), billable="0.125")
    headers = both.customer(tenant)

    data = summary(both, headers, month=MONTH)
    with both.factory() as session:
        found = summarize_processed_events_for_customer(session, tenant.id, **OCTOBER)

    assert busy_days(data) == {
        "2026-10-01": (1, "0.06250000"),
        "2026-10-02": (1, "0.12500000"),
    }
    # 两种方言都读成 `datetime.date`、按日期升序。
    assert [(row.day, row.request_count) for row in found.days] == [
        (dt.date(2026, 10, 1), 1),
        (dt.date(2026, 10, 2), 1),
    ]
    assert all(type(row.day) is dt.date for row in found.days)


@pytest.mark.parametrize(
    ("now", "month", "days"),
    [
        (dt.datetime(2026, 10, 31, 15, 59, 59), "2026-10", 31),
        (dt.datetime(2026, 10, 31, 16, 0, 0), "2026-11", 30),
    ],
)
def test_the_default_month_is_the_malaysian_month_of_now(
    both: UsageEventPortal, monkeypatch, now: dt.datetime, month: str, days: int
) -> None:
    """§7「默认月份」：不带 `month`，「现在」固定在月末那一秒的两侧；同一请求只取一次「现在」。"""
    tenant = both.tenant()
    both.put(tenant, occurred_at=dt.datetime(2026, 10, 31, 15, 0, 0))
    both.put(tenant, occurred_at=dt.datetime(2026, 10, 31, 16, 30, 0), billable="0.125")
    headers = both.customer(tenant)
    calls: list[dt.datetime] = []

    def frozen() -> dt.datetime:
        calls.append(now)
        return now

    monkeypatch.setattr(customer_api, "utc_now", frozen)

    data = summary(both, headers)

    assert len(calls) == 1
    assert data["month"] == month
    assert len(data["days"]) == days
    assert data["request_count"] == 1
    expected = "0.06250000" if month == "2026-10" else "0.12500000"
    assert data["charged_amount"] == expected


def test_only_billed_events_are_counted(both: UsageEventPortal) -> None:
    """§7「只算已计费」：同月的未计费 / 错误状态事件不计入三个数字与 `days`。"""
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

    data = summary(both, headers, month=MONTH)

    assert data["request_count"] == 1
    assert data["conversation_count"] == 1
    assert data["charged_amount"] == "0.06250000"
    assert busy_days(data) == {"2026-10-01": (1, "0.06250000")}


def test_the_summary_matches_the_usage_page_for_the_same_month(both: UsageEventPortal) -> None:
    """§7「与用量页一致」：用量接口以同一个月的首末日翻完，条数与计费额之和都一致。"""
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
    for moment, amount in zip(moments, amounts, strict=True):
        both.put(tenant, occurred_at=moment, billable=amount)
    # 区间外的不该出现在任何一边。
    both.put(tenant, occurred_at=dt.datetime(2026, 9, 30, 15, 59, 59), billable="1")
    both.put(tenant, occurred_at=dt.datetime(2026, 10, 31, 16, 0, 0), billable="1")
    headers = both.customer(tenant)

    data = summary(both, headers, month=MONTH)
    items = both.walk(headers, from_date="2026-10-01", to_date="2026-10-31", page_size=100)

    assert data["request_count"] == len(items) == len(moments)
    assert Decimal(data["charged_amount"]) == sum(
        (Decimal(item["charged_amount"]) for item in items), Decimal(0)
    )
    assert data["charged_amount"] == "0.96875000"


def test_conversations_are_counted_once_per_month(both: UsageEventPortal) -> None:
    """§7「对话计数」：同一对话 9 月、10 月各有事件，10 月内 5 条 → 9 月、10 月各计 1。"""
    tenant = both.tenant()
    both.put(tenant, occurred_at=dt.datetime(2026, 9, 10, 2, 0, 0), conversation_id="conv-1")
    for number in range(5):
        both.put(
            tenant,
            occurred_at=OCT_1 + number * dt.timedelta(days=3),
            conversation_id="conv-1",
        )
    # 没有 `conversation_id` 的事件（AI 模块的调用）不计入。
    both.put(tenant, occurred_at=OCT_15)
    headers = both.customer(tenant)

    october = summary(both, headers, month=MONTH)
    september = summary(both, headers, month="2026-09")

    assert october["conversation_count"] == 1
    assert october["request_count"] == 6
    assert september["conversation_count"] == 1
    assert september["request_count"] == 1


# --- 参数规则与空月 -------------------------------------------------------------------


@pytest.mark.parametrize("month", ["2026-13", "2026-00", "2026-1", "2019-12", "2100-01", "abc"])
def test_bad_months_are_422_without_echoing_the_value(portal: UsageEventPortal, month: str) -> None:
    """§7「参数规则」：格式错或越界 → 422 `VALIDATION_ERROR`，文案只列字段名。"""
    tenant = portal.tenant()
    portal.put(tenant, occurred_at=OCT_1)
    headers = portal.customer(tenant)
    before = portal.tables()

    response = get_summary(portal, headers, month=month)

    assert error_of(response) == (422, "VALIDATION_ERROR")
    assert response.json()["error"]["message"] == "Invalid request fields: query.month"
    assert response.json()["data"] is None
    assert month not in response.json()["error"]["message"]
    assert portal.tables() == before


@pytest.mark.parametrize(
    ("month", "days"), [("2020-01", 31), ("2099-12", 31), ("2028-02", 29), ("2027-02", 28)]
)
def test_the_edges_and_future_months_are_200_and_zero(
    portal: UsageEventPortal, month: str, days: int
) -> None:
    """§7「参数规则」「空月」：`2020-01`、`2099-12` 与未来月份为 200，全为 0、`days` 照常补齐。"""
    tenant = portal.tenant()
    portal.put(tenant, occurred_at=OCT_1, conversation_id="conv-1")
    headers = portal.customer(tenant)

    data = summary(portal, headers, month=month)

    first = dt.date(int(month[:4]), int(month[5:]), 1)
    assert data == {
        "month": month,
        "charged_amount": ZERO,
        "request_count": 0,
        "conversation_count": 0,
        "days": [
            {"date": date, "request_count": 0, "charged_amount": ZERO}
            for date in month_dates(first, days)
        ],
    }


def test_an_empty_month(both: UsageEventPortal) -> None:
    """§7「空月」：该月没有任何事件 → 三个数字为 0，`days` 补齐 31 项。"""
    tenant = both.tenant()
    both.put(tenant, occurred_at=dt.datetime(2026, 9, 10, 2, 0, 0), conversation_id="conv-1")
    headers = both.customer(tenant)

    data = summary(both, headers, month=MONTH)

    assert (data["request_count"], data["conversation_count"], data["charged_amount"]) == (
        0,
        0,
        ZERO,
    )
    assert len(data["days"]) == 31
    assert busy_days(data) == {}


def test_unknown_parameters_are_ignored(portal: UsageEventPortal) -> None:
    tenant = portal.tenant()
    portal.put(tenant, occurred_at=OCT_1)
    headers = portal.customer(tenant)

    data = summary(portal, headers, month=MONTH, project_id=ZERO_UUID, sort="asc", days="7")

    assert data["request_count"] == 1


# --- 扫描上限、执行时间提示与超时映射 ---------------------------------------------------


def compiled(statement, dialect) -> str:
    return " ".join(str(statement.compile(dialect=dialect)).split())


def test_the_hint_and_the_day_expression_are_rendered_per_dialect() -> None:
    """§7「扫描上限」的编译断言：两条都在 MySQL 上带提示、SQLite 上不带；日期表达式按方言生成。"""
    assert CUSTOMER_USAGE_QUERY_TIMEOUT_MS == 2000
    for build in (customer_usage_daily_statement, customer_usage_conversation_statement):
        default = build(1, **OCTOBER)
        short = build(1, timeout_ms=1, **OCTOBER)
        assert "MAX_EXECUTION_TIME" not in compiled(default, sqlite.dialect())
        assert "MAX_EXECUTION_TIME" not in compiled(short, sqlite.dialect())
        # 只对 MySQL 方言编译的断言：提示紧跟 SELECT 关键字。
        assert compiled(default, mysql.dialect()).startswith(
            "SELECT /*+ MAX_EXECUTION_TIME(2000) */ "
        )
        assert compiled(short, mysql.dialect()).startswith("SELECT /*+ MAX_EXECUTION_TIME(1) */ ")
        for dialect in (sqlite.dialect(), mysql.dialect()):
            text = compiled(default, dialect)
            for column in COST_COLUMNS:
                assert column not in text
            assert "tenant_id" in text and "status" in text and "occurred_at" in text
    daily = customer_usage_daily_statement(1, **OCTOBER)
    assert "date(usage_events.occurred_at, '+8 hours')" in compiled(daily, sqlite.dialect())
    assert "DATE(DATE_ADD(usage_events.occurred_at, INTERVAL 8 HOUR))" in compiled(
        daily, mysql.dialect()
    )
    conversations = customer_usage_conversation_statement(1, **OCTOBER)
    assert "DISTINCT" in compiled(conversations, sqlite.dialect()).upper()


def test_the_executed_queries_carry_the_hint_on_mysql_only(both: UsageEventPortal) -> None:
    """真正发出的两条汇总查询：MySQL 上都带 2 秒提示，SQLite 上都不带；都不加锁。"""
    tenant = both.tenant()
    both.put(tenant, occurred_at=OCT_1)
    headers = both.customer(tenant)
    engine = both.app.state.engine
    statements, record = record_statements(engine)
    try:
        assert summary(both, headers, month=MONTH)["request_count"] == 1
    finally:
        event.remove(engine, "before_cursor_execute", record)

    queries = usage_statements(statements)
    assert len(queries) == 2
    for query in queries:
        assert "FOR UPDATE" not in query.upper()
        assert "FOR SHARE" not in query.upper()
        assert "LOCK IN SHARE MODE" not in query.upper()
        for column in COST_COLUMNS:
            assert column not in query
        if engine.dialect.name == "mysql":
            assert "MAX_EXECUTION_TIME(2000)" in query
        else:
            assert "MAX_EXECUTION_TIME" not in query


def test_a_scan_past_the_budget_is_query_too_broad(
    mysql_portal: UsageEventPortal, monkeypatch
) -> None:
    """§7「扫描上限」（MySQL）：当月 30 000 条已计费事件。

    ① 测试专用 1 毫秒超时：repository 抛 `QueryTooBroad`，接口层 422 `QUERY_TOO_BROAD` 并有一条
    `customer_usage_query_timeout` 日志；② 默认 2 秒：正常返回且数字正确。
    """
    portal = mysql_portal
    tenant = portal.tenant()
    # 马来西亚 10 月 1 日 00:00 起每秒一条：全部落在 10 月 1 日（8 小时 20 分钟）。
    portal.bulk(tenant, 30_000, dt.datetime(2026, 9, 30, 16, 0, 0))
    headers = portal.customer(tenant)

    with portal.factory() as session, pytest.raises(QueryTooBroad):
        summarize_processed_events_for_customer(session, tenant.id, timeout_ms=1, **OCTOBER)
    with portal.factory() as session:
        found = summarize_processed_events_for_customer(session, tenant.id, **OCTOBER)
    assert found.days == [CustomerUsageDay(dt.date(2026, 10, 1), 30_000, Decimal(0))]
    assert found.conversation_count == 0
    data = summary(portal, headers, month=MONTH)
    assert data["request_count"] == 30_000
    assert busy_days(data) == {"2026-10-01": (30_000, ZERO)}

    monkeypatch.setattr(
        usage_repository,
        "summarize_processed_events_for_customer",
        functools.partial(summarize_processed_events_for_customer, timeout_ms=1),
    )
    response, lines = capture_server_log(lambda: get_summary(portal, headers, month=MONTH))

    assert error_of(response) == (422, "QUERY_TOO_BROAD")
    assert response.json()["data"] is None
    assert response.json()["request_id"]
    [timeout] = timeout_lines(lines)
    assert timeout["request_id"] == response.json()["request_id"]
    assert timeout["customer_id"] == tenant.public_id


def test_query_too_broad_is_a_422_with_one_warning_and_no_parameter_values(
    portal: UsageEventPortal, monkeypatch
) -> None:
    """超时映射：repository 抛 `QueryTooBroad` → 422 `QUERY_TOO_BROAD`，一条日志，不带 `month`。"""
    tenant = portal.tenant()
    portal.put(tenant, occurred_at=OCT_1)
    headers = portal.customer(tenant)
    before = portal.tables()

    def too_broad(*_args, **_kwargs):
        raise QueryTooBroad

    monkeypatch.setattr(usage_repository, "summarize_processed_events_for_customer", too_broad)
    response, lines = capture_server_log(lambda: get_summary(portal, headers, month="2026-07"))

    assert error_of(response) == (422, "QUERY_TOO_BROAD")
    body = response.json()
    assert body["data"] is None
    assert body["request_id"]
    [timeout] = timeout_lines(lines)
    assert timeout["level"] == "WARNING"
    assert timeout["request_id"] == body["request_id"]
    assert timeout["customer_id"] == tenant.public_id
    assert "2026-07" not in json.dumps(lines)
    assert portal.tables() == before


# --- 同一快照 ---------------------------------------------------------------------------


def test_both_selects_run_on_one_connection_in_one_transaction(both: UsageEventPortal) -> None:
    """§7「同一快照」（INV-13）：两条 SELECT 在同一个连接、同一个事务里发出，期间不提交。"""
    tenant = both.tenant()
    both.put(tenant, occurred_at=OCT_1, conversation_id="conv-1")
    engine = both.factory.kw["bind"]
    seen: list[tuple[object, object, str]] = []
    commits: list[object] = []

    def record(conn, _cursor, statement, *_args) -> None:
        if re.search(r"\busage_events\b", statement, re.IGNORECASE):
            seen.append((conn, conn.get_transaction(), statement))

    def committed(conn) -> None:
        commits.append(conn)

    event.listen(engine, "before_cursor_execute", record)
    event.listen(engine, "commit", committed)
    try:
        with both.factory() as session:
            found = summarize_processed_events_for_customer(session, tenant.id, **OCTOBER)
            assert len(seen) == 2
            (first_conn, first_tx, _), (second_conn, second_tx, _) = seen
            assert first_tx is not None and first_tx.is_active
            assert first_conn is second_conn
            assert first_tx is second_tx
            assert commits == []
    finally:
        event.remove(engine, "before_cursor_execute", record)
        event.remove(engine, "commit", committed)

    assert commits == []
    assert found.conversation_count == 1
    assert [row.request_count for row in found.days] == [1]


# --- 金额精度 ---------------------------------------------------------------------------


def test_tiny_amounts_add_up_exactly(both: UsageEventPortal) -> None:
    """§7「金额精度」（INV-10）：三条 `0.00000001` 合计 `"0.00000003"`。"""
    tenant = both.tenant()
    for number in range(3):
        both.put(tenant, occurred_at=OCT_1 + number * MINUTE, billable="0.00000001")
    headers = both.customer(tenant)

    data = summary(both, headers, month=MONTH)

    assert data["charged_amount"] == "0.00000003"
    assert busy_days(data) == {"2026-10-01": (3, "0.00000003")}


def test_the_largest_amount_plus_the_smallest_does_not_overflow(both: UsageEventPortal) -> None:
    """§7「金额精度」：同日 `999999999999.99999999` 与 `0.00000001` 得 `"1000000000000.00000000"`。

    先给钱包记 1：两笔扣费加起来会超出余额列的下界（`DECIMAL(20,8)`）。SQLite 把 NUMERIC 存成浮点，
    在那里这条只是不失败；真正判别 float 的是下面只在 MySQL 上跑的那条。
    """
    tenant = both.tenant()
    both.world.fund(tenant, "1")
    both.put(tenant, occurred_at=OCT_1, billable="999999999999.99999999")
    both.put(tenant, occurred_at=OCT_1 + MINUTE, billable="0.00000001")
    headers = both.customer(tenant)

    data = summary(both, headers, month=MONTH)

    assert data["charged_amount"] == "1000000000000.00000000"
    assert busy_days(data) == {"2026-10-01": (2, "1000000000000.00000000")}


def test_a_float_round_trip_would_change_the_sum(mysql_portal: UsageEventPortal) -> None:
    """设计闸门 #403 审查的建议（只在 MySQL 上跑）：同日 `1234567890.12345678` 与 `0.00000001`。

    当日与合计都恰好是 `1234567890.12345679`；经 float 往返会得到别的值。
    """
    portal = mysql_portal
    tenant = portal.tenant()
    portal.put(tenant, occurred_at=OCT_1, billable="1234567890.12345678")
    portal.put(tenant, occurred_at=OCT_1 + MINUTE, billable="0.00000001")
    headers = portal.customer(tenant)
    expected = "1234567890.12345679"
    # 前提：float 判别得出来。
    assert Decimal(float(Decimal(expected))).quantize(Decimal("0.00000001")) != Decimal(expected)

    data = summary(portal, headers, month=MONTH)

    assert data["charged_amount"] == expected
    assert busy_days(data) == {"2026-10-01": (2, expected)}


# --- 内部计量、无成本毛利、租户越权 ------------------------------------------------------


def test_an_internal_metered_tenant_is_counted_at_zero(both: UsageEventPortal) -> None:
    """§7「内部计量租户」（INV-7）：计费额 `"0.00000000"`，请求数照常；参考价不出现。"""
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

    response = get_summary(both, headers, month=MONTH)

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["charged_amount"] == ZERO
    assert data["request_count"] == 2
    assert data["conversation_count"] == 1
    assert busy_days(data) == {"2026-10-01": (2, ZERO)}
    for value in SENTINEL_TEXTS:
        assert value not in response.text


def test_no_cost_rate_or_reference_price_leaks(both: UsageEventPortal) -> None:
    """§7「无成本毛利」（INV-7）：成本、原币成本、汇率写成哨兵值，原始响应文本里都没有。"""
    tenant = both.tenant()
    both.put(tenant, occurred_at=OCT_1, costs=SENTINEL_COSTS, conversation_id="conv-secret-0000")
    both.put(tenant, occurred_at=OCT_15, costs=SENTINEL_COSTS, request_id="req-secret-0000")
    headers = both.customer(tenant)

    response = get_summary(both, headers, month=MONTH)

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
        assert value not in response.text, value


def test_a_customer_only_ever_sees_its_own_summary(both: UsageEventPortal) -> None:
    """§7「租户越权」（INV-8）：A、B 同月都有事件、共用同一个 `conversation_id` 值。

    A 带 B 的标识只得到 A 的数字；A 的 `conversation_count` 不因 B 的同名对话变化。
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
    forged = {"customer_id": theirs.public_id, "tenant_id": str(theirs.id)}

    plain = summary(both, headers, month=MONTH)
    response = get_summary(both, headers, month=MONTH, **forged)

    assert response.status_code == 200
    data = response.json()["data"]
    assert data == plain
    assert data["request_count"] == 2
    assert data["conversation_count"] == 1
    assert data["charged_amount"] == "0.18750000"
    assert busy_days(data) == {
        "2026-10-01": (1, "0.06250000"),
        "2026-10-15": (1, "0.12500000"),
    }
    theirs_data = summary(both, both.customer(theirs), month=MONTH)
    assert theirs_data["request_count"] == 3
    assert theirs_data["conversation_count"] == 2
    assert theirs.public_id not in response.text


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
        response = get_summary(portal, headers, month=MONTH)
    finally:
        event.remove(engine, "before_cursor_execute", record)

    assert error_of(response) == expected
    assert response.json()["data"] is None
    assert usage_statements(statements) == []


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
    """§7「只读契约」：四种账户状态下都 200，且不写任何表。"""
    tenant = both.tenant()
    both.put(tenant, occurred_at=OCT_1)
    both.set_account_status(tenant, status)
    headers = both.customer(tenant)
    before = both.tables()

    data = summary(both, headers, month=MONTH)

    assert data["request_count"] == 1
    assert both.tables() == before


def test_the_same_request_twice_returns_the_same_response(portal: UsageEventPortal) -> None:
    """§5「重复请求」：期间没有新计费的事件时两次的 data 相同。"""
    tenant = portal.tenant()
    portal.put(tenant, occurred_at=OCT_1, conversation_id="conv-1")
    headers = portal.customer(tenant)

    assert summary(portal, headers, month=MONTH) == summary(portal, headers, month=MONTH)


def test_a_failing_query_is_a_500_with_a_request_id_and_writes_nothing(
    portal: UsageEventPortal, monkeypatch
) -> None:
    """§7「事务中途失败」：repository 抛数据库异常 → 500 带 request_id，各表不变。"""
    tenant = portal.tenant()
    portal.put(tenant, occurred_at=OCT_1)
    headers = portal.customer(tenant)
    before = portal.tables()

    def broken(*_args, **_kwargs):
        raise OperationalError("SELECT", {}, Exception("fictional database failure"))

    monkeypatch.setattr(usage_repository, "summarize_processed_events_for_customer", broken)
    client = TestClient(portal.app, raise_server_exceptions=False)

    response = client.get(SUMMARY, params={"month": MONTH}, headers=headers)

    assert error_of(response) == (500, "INTERNAL_ERROR")
    body = response.json()
    assert body["data"] is None
    assert body["request_id"]
    assert "fictional database failure" not in response.text
    assert portal.tables() == before


def test_the_usage_page_is_unchanged(portal: UsageEventPortal) -> None:
    """AIH-TASK-104 的用量接口照旧：新接口不改它的行为（抽出的超时日志函数是同一条日志）。"""
    tenant = portal.tenant()
    stored = portal.put(tenant, occurred_at=OCT_1)
    headers = portal.customer(tenant)

    response = portal.client.get(
        USAGE, params={"from_date": "2026-10-01", "to_date": "2026-10-01"}, headers=headers
    )

    assert response.status_code == 200
    assert [item["id"] for item in response.json()["data"]["items"]] == [stored.public_id]

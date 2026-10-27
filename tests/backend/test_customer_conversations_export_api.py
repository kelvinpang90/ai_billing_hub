"""Customer conversation CSV export end to end (AIH-TASK-122, design gate #460 v2 §7).

`GET /api/v1/customer/conversations/export`：与 `GET /conversations` 同一个 repository 函数
（`list_conversations_for_customer`）与同一个 9 键白名单视图，用 AIH-TASK-114 的 CSV 模块、
导出租约与 `ExportCsvResponse`；租约与错误处理与用量导出逐步相同。

这里是 §7 后端表里属于接口二的行（导出正常路径、导出行数上限、导出租约、公式注入、只有表头、
内存上界、常量），以及表里写明「接口二」「导出同样」「CSV」的行（日期与参数规则、扫描上限、内部
计量租户、无成本毛利、金额精度、租户越权、鉴权、只读契约、无写入、数据库断连）。「路由守卫」在
test_customer_api.py（显式清单里加了新路由）。

数据照 test_customer_conversations_api 的写法直接写库（`UsageEventPortal.put`）；SQLite 上的金额
只用二进制可精确表示的值，能判别 float 的精度用例只在 MySQL 上跑（设计 §2「查询」）。
- SQLite：`create_all` 建表；
- MySQL：表与触发器都走迁移，需要 `BILLING_TEST_DATABASE_URL`，没设时 MySQL 那一半 skip ——
  **不要把 skipped 读成 passed**，CI 里不许 skip。

⚠️ 导出租约是进程级的：每条用例开始与结束时都必须空闲（`lease_is_free`）。

⚠️ SQL 文本断言不写死方言字面量；只有 MySQL 才有的断言放在方言分支里。
示例 uuid 用全零占位值。
"""

from __future__ import annotations

import csv
import datetime as dt
import functools
import io
import json
import re
import tracemalloc
from collections.abc import Iterator
from decimal import Decimal
from typing import Any
from urllib.parse import urlencode

import pytest
from sqlalchemy import event, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker
from starlette.requests import Request
from test_customer_conversations_api import (
    DAY,
    MINUTE,
    ON_DAY,
    SENTINEL_COSTS,
    SENTINEL_DESCRIPTION,
    SENTINEL_REFERENCE_PRICE,
    SENTINEL_TEXTS,
    SENTINEL_WEBHOOK,
    ZERO,
    ZERO_UUID,
    bulk_conversations,
    conversations,
    seed_normal,
)
from test_customer_usage_api import (
    UsageEventPortal,
    _usage_portal,
    record_statements,
    usage_statements,
)
from test_customer_usage_summary_api import capture_server_log, timeout_lines
from test_customer_wallet_api import CONTEXT, _sqlite_factory, error_of
from test_usage_billing import TOKENS, _mysql_factory

from app.api import customer as customer_api
from app.core.csv_export import (
    EXPORT_LEASE_SCOPE_KEY,
    EXPORT_MAX_ROWS,
    CustomerExportFailed,
    ExportLeaseSlot,
    export_lease_held,
    release_export_lease,
    try_acquire_export_lease,
    write_csv,
)
from app.models.auth import UserRole
from app.models.tenancy import AccountStatus, BillingMode, Project
from app.models.usage import REQUEST_ID_LENGTH
from app.repositories import usage_events as usage_repository
from app.repositories.usage_events import (
    CustomerConversationRow,
    QueryTooBroad,
    list_conversations_for_customer,
)
from app.schemas.customer_conversations import (
    CONVERSATION_EXPORT_COLUMNS,
    CONVERSATION_EXPORT_TEXT_COLUMNS,
    customer_conversation_export_cells,
)
from app.services.portal_accounts import disable_portal_account

EXPORT = "/api/v1/customer/conversations/export"
BOM = b"\xef\xbb\xbf"
MIB = 1024 * 1024

# 设计 §2「接口二」：表头恰好这 9 列，按这个顺序。多一列、少一列、换顺序都红。
COLUMNS = [
    "conversation_id",
    "project_id",
    "project_name",
    "first_occurred_at_utc",
    "last_occurred_at_utc",
    "request_count",
    "input_tokens",
    "output_tokens",
    "charged_amount",
]
TEXT_COLUMNS = {"conversation_id", "project_id", "project_name"}
PERIOD = {"from_date": DAY, "to_date": DAY}
HEADER_ONLY = BOM + (",".join(COLUMNS) + "\r\n").encode("ascii")


@pytest.fixture(autouse=True)
def lease_is_free() -> Iterator[None]:
    # 进程级租约：每条用例开始与结束时都必须空闲，用例之间不互相挡住。
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
    """The data rows of a 200 export; the header row is checked here."""
    response = export(portal, headers, **params)
    assert response.status_code == 200, response.text
    table = read_csv(response.content)
    assert table[0] == COLUMNS
    return table[1:]


def cells_of(item: dict) -> list[str]:
    """What one JSON item becomes in the file (for values that need no formula prefix)."""
    values = {
        **item,
        "first_occurred_at_utc": item["first_occurred_at"],
        "last_occurred_at_utc": item["last_occurred_at"],
    }
    return [str(values[name]) for name in COLUMNS]


def cell(row: list[str], name: str) -> str:
    return row[COLUMNS.index(name)]


def column(rows: list[list[str]], name: str) -> list[str]:
    return [cell(row, name) for row in rows]


def failures_of(lines: list[dict]) -> list[dict]:
    return [line for line in lines if line["message"] == "customer_export_failed"]


def assert_generic_500(status: int, body: dict) -> None:
    """The same envelope as `handle_unexpected_error`'s, field for field."""
    assert status == 500
    assert set(body) == {"success", "data", "error", "request_id"}
    assert body["success"] is False
    assert body["data"] is None
    assert body["error"] == {"code": "INTERNAL_ERROR", "message": "An internal error occurred."}
    assert body["request_id"]


def three_conversations(portal: UsageEventPortal) -> dict[str, str]:
    """A customer with three conversations on `DAY`; its signed-in headers."""
    tenant = portal.tenant()
    for number in range(3):
        portal.put(tenant, occurred_at=ON_DAY + number * MINUTE, conversation_id=f"conv-{number}")
    return portal.customer(tenant)


def fake_row(index: int) -> CustomerConversationRow:
    return CustomerConversationRow(
        conversation_id=f"conv-{index:05d}",
        project_public_id=ZERO_UUID,
        project_name="Chatbot",
        first_occurred_at=ON_DAY,
        last_occurred_at=ON_DAY,
        request_count=1,
        input_tokens=2,
        output_tokens=3,
        charged_amount=Decimal("0.0625"),
    )


def export_scope(app: Any, headers: dict[str, str], params: dict[str, str]) -> dict[str, Any]:
    """An ASGI scope for calling the handler directly, with a lease slot of its own."""
    raw = [(b"host", b"testserver")]
    raw += [
        (name.lower().encode("latin-1"), value.encode("latin-1")) for name, value in headers.items()
    ]
    return {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": EXPORT,
        "raw_path": EXPORT.encode("ascii"),
        "root_path": "",
        "query_string": urlencode(params).encode("ascii"),
        "headers": raw,
        "client": ("127.0.0.1", 50000),
        "server": ("testserver", 80),
        "app": app,
        EXPORT_LEASE_SCOPE_KEY: ExportLeaseSlot(),
    }


# --- 导出正常路径 -----------------------------------------------------------------------


def test_the_export_matches_the_list_cell_for_cell(both: UsageEventPortal) -> None:
    """§7「导出正常路径」：与接口一「正常路径」同一批数据。

    200、`text/csv; charset=utf-8`、文件名、`no-store`、`X-Accel-Buffering: no`、BOM；
    表头逐字为 9 列；行序与接口一相同；每个单元格与接口一同一行的对应值逐字相等
    （时间列对应去掉 `_utc` 的键）。
    """
    tenant = both.tenant()
    main, other = seed_normal(both, tenant)
    headers = both.customer(tenant)
    period = {"from_date": "2026-09-15", "to_date": DAY}
    before = both.tables()

    response = export(both, headers, **period)

    assert response.status_code == 200
    assert response.headers["content-type"] == "text/csv; charset=utf-8"
    assert response.headers["content-disposition"] == (
        f'attachment; filename="conversations_2026-09-15_{DAY}.csv"'
    )
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-accel-buffering"] == "no"
    assert response.headers["content-length"] == str(len(response.content))
    assert response.content.startswith(BOM)
    assert response.content.endswith(b"\r\n")
    table = read_csv(response.content)
    assert table[0] == COLUMNS
    rows = table[1:]
    items = conversations(both, headers, **period, page_size=100)["items"]
    assert len(items) == 3
    assert rows == [cells_of(item) for item in items]
    assert column(rows, "conversation_id") == ["conv-3", "conv-2", "conv-1"]
    assert column(rows, "project_id") == [main.public_id, other.public_id, main.public_id]
    assert column(rows, "project_name") == ["Chatbot", "Support desk", "Chatbot"]
    assert column(rows, "request_count") == ["2", "1", "4"]
    assert column(rows, "charged_amount") == ["0.18750000", "0.50000000", "0.50000000"]
    # `QUANTITY` 那条计入请求数与金额、不计 token。
    assert cell(rows[2], "input_tokens") == str(3 * TOKENS["input_tokens"])
    assert cell(rows[2], "first_occurred_at_utc") == "2026-10-01T02:00:00.123456"
    assert cell(rows[2], "last_occurred_at_utc") == "2026-10-01T02:03:00.123456"
    assert both.tables() == before


def test_the_project_filter_gives_the_same_rows_as_the_list(both: UsageEventPortal) -> None:
    """§2「接口二」：`project_id` 与接口一同一语义。"""
    tenant = both.tenant()
    main, other = seed_normal(both, tenant)
    headers = both.customer(tenant)

    for project in (main, other):
        items = conversations(both, headers, **PERIOD, project_id=project.public_id)["items"]
        assert items
        assert exported(both, headers, **PERIOD, project_id=project.public_id) == [
            cells_of(item) for item in items
        ]


def test_the_column_lists_are_the_designed_ones() -> None:
    """§7「常量」：恰好 §2 的 9 列与 3 个文本列（INV-7：列清单固定）。"""
    assert list(CONVERSATION_EXPORT_COLUMNS) == COLUMNS
    assert set(CONVERSATION_EXPORT_TEXT_COLUMNS) == TEXT_COLUMNS


# --- 日期与参数规则 ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "params",
    [
        {"to_date": DAY},
        {"from_date": DAY},
        {},
        {"from_date": "2026-10-02", "to_date": DAY},
        {"from_date": "2026-07-01", "to_date": "2026-10-01"},
        {"from_date": "2026-02-30", "to_date": "2026-03-01"},
        {"from_date": "2026/10/01", "to_date": DAY},
        {"from_date": "0001-01-01", "to_date": "0001-01-02"},
        {"from_date": "2099-12-31", "to_date": "2100-01-01"},
    ],
)
def test_date_rules_are_the_lists(portal: UsageEventPortal, params: dict) -> None:
    """§7「日期与参数规则」：与接口一相同的 422、相同的文案，不回显值，不占租约。"""
    headers = portal.customer(portal.tenant())

    response = export(portal, headers, **params)
    listed = portal.client.get("/api/v1/customer/conversations", params=params, headers=headers)

    assert error_of(response) == (422, "VALIDATION_ERROR")
    body = response.json()
    assert body == {**listed.json(), "request_id": body["request_id"]}
    assert body["data"] is None
    assert body["error"]["message"].startswith("Invalid request fields: ")
    for value in params.values():
        assert value not in body["error"]["message"]


def test_paging_and_unknown_parameters_are_ignored(portal: UsageEventPortal) -> None:
    """§7「日期与参数规则」：接口二带 `page_size`、`cursor`（被忽略）照常 200。"""
    headers = three_conversations(portal)

    rows = exported(
        portal,
        headers,
        **PERIOD,
        page_size="0",
        cursor="not base64!",
        sort="asc",
        conversation_id="conv-1",
        provider="openai",
    )

    assert column(rows, "conversation_id") == ["conv-2", "conv-1", "conv-0"]


def test_a_92_day_period_is_200(portal: UsageEventPortal) -> None:
    """§7「日期与参数规则」：92 天（含两端）200，首尾两天的会话都在。"""
    tenant = portal.tenant()
    portal.put(tenant, occurred_at=dt.datetime(2026, 6, 30, 16, 0, 0), conversation_id="conv-first")
    portal.put(
        tenant,
        occurred_at=dt.datetime(2026, 9, 30, 15, 59, 59, 999999),
        conversation_id="conv-last",
    )
    headers = portal.customer(tenant)

    rows = exported(portal, headers, from_date="2026-07-01", to_date="2026-09-30")

    assert column(rows, "conversation_id") == ["conv-last", "conv-first"]


# --- 导出行数上限 -----------------------------------------------------------------------


def test_the_query_asks_for_one_row_more_than_the_cap_without_a_cursor(
    portal: UsageEventPortal, monkeypatch
) -> None:
    """§7「导出行数上限」：`EXPORT_MAX_ROWS` 是 20 000，查询的 `limit` 是它 + 1，不带游标。"""
    headers = three_conversations(portal)
    calls: list[dict] = []
    original = usage_repository.list_conversations_for_customer

    def spy(*args, **kwargs):
        calls.append(kwargs)
        return original(*args, **kwargs)

    monkeypatch.setattr(usage_repository, "list_conversations_for_customer", spy)

    assert len(exported(portal, headers, **PERIOD)) == 3
    assert EXPORT_MAX_ROWS == 20_000
    assert [call["limit"] for call in calls] == [EXPORT_MAX_ROWS + 1]
    assert calls[0].get("before") is None


def test_exactly_the_cap_is_200_and_one_more_is_export_too_large(
    both: UsageEventPortal, monkeypatch
) -> None:
    """§7「导出行数上限」：repository 返回 20 000 行与 20 001 行（monkeypatch）。

    前者 200、恰好 20 000 个数据行；后者 422 `EXPORT_TOO_LARGE`、JSON 信封、没有任何 CSV 行。
    """
    headers = three_conversations(both)
    rows = [fake_row(index) for index in range(EXPORT_MAX_ROWS + 1)]
    returned: list[list[CustomerConversationRow]] = [rows[:EXPORT_MAX_ROWS], rows]

    def fixed(*_args, **_kwargs):
        return returned.pop(0)

    monkeypatch.setattr(usage_repository, "list_conversations_for_customer", fixed)

    fits = export(both, headers, **PERIOD)
    too_large = export(both, headers, **PERIOD)

    assert fits.status_code == 200
    table = read_csv(fits.content)
    assert table[0] == COLUMNS
    assert len(table) == EXPORT_MAX_ROWS + 1
    assert table[1] == customer_conversation_export_cells(rows[0])
    assert error_of(too_large) == (422, "EXPORT_TOO_LARGE")
    assert too_large.headers["content-type"].startswith("application/json")
    assert "content-disposition" not in too_large.headers
    assert not too_large.content.startswith(BOM)
    body = too_large.json()
    assert body["data"] is None
    assert body["request_id"]
    assert "20,000" in body["error"]["message"]
    assert "conv-00000" not in too_large.text
    assert returned == []


# --- 导出租约 ---------------------------------------------------------------------------


def test_a_held_lease_is_export_busy_without_any_query(both: UsageEventPortal, monkeypatch) -> None:
    """§7「导出租约」：测试先占住租约再调接口二 → 503 `EXPORT_BUSY`、带 `Retry-After`。

    不发查询（记录执行的语句，且 repository 没被调用）；释放后正常导出 200，之后租约已释放。
    """
    headers = three_conversations(both)
    called: list[object] = []
    original = usage_repository.list_conversations_for_customer

    def spy(*args, **kwargs):
        called.append(args)
        return original(*args, **kwargs)

    monkeypatch.setattr(usage_repository, "list_conversations_for_customer", spy)
    engine = both.app.state.engine
    holder = try_acquire_export_lease()
    assert holder is not None
    statements, record = record_statements(engine)
    try:
        busy = export(both, headers, **PERIOD)
    finally:
        event.remove(engine, "before_cursor_execute", record)
        release_export_lease(holder)

    assert error_of(busy) == (503, "EXPORT_BUSY")
    assert busy.headers["retry-after"] == "5"
    assert busy.json()["data"] is None
    assert busy.json()["request_id"]
    assert called == []
    assert statements, "监听落空"
    assert usage_statements(statements) == []
    projects = re.compile(r"\bprojects\b", re.IGNORECASE)
    assert not [statement for statement in statements if projects.search(statement)]
    assert len(exported(both, headers, **PERIOD)) == 3
    # 正常导出之后租约已由最外层中间件释放。
    assert not export_lease_held()


def test_the_handler_only_holds_the_lease(portal: UsageEventPortal) -> None:
    """§2「接口二」：处理函数只 `hold` 进 `request.scope` 的租约槽，自己不释放。"""
    headers = three_conversations(portal)
    scope = export_scope(portal.app, headers, PERIOD)
    slot = scope[EXPORT_LEASE_SCOPE_KEY]

    try:
        response = customer_api.customer_conversations_export(
            Request(scope), from_date=DAY, to_date=DAY
        )
        # 处理函数返回后租约仍在槽里，等最外层中间件。
        assert export_lease_held()
    finally:
        slot.release()

    assert response.status_code == 200
    assert len(read_csv(response.body)) == 4
    assert not export_lease_held()


# --- 公式注入、只有表头 -----------------------------------------------------------------


def test_formula_prefixes_are_quoted_in_text_columns_only(both: UsageEventPortal) -> None:
    """§7「公式注入」：项目名 `=SUM(A1)`、`+1`、`-1`、`@x`（直接写库）。

    文件里这些单元格按 `write_csv` 的规则加了单引号；数字列与时间列不加。`conversation_id`
    合法地以 `-` 开头时同样加。
    """
    names = ["=SUM(A1)", "+1", "-1", "@x"]
    tenant = both.tenant()
    for number, name in enumerate(names):
        project = both.project(tenant, f"Formula project {number}")
        with both.factory() as session:
            session.execute(update(Project).where(Project.id == project.id).values(name=name))
            session.commit()
        both.put(
            tenant,
            project=project,
            occurred_at=ON_DAY + number * MINUTE,
            conversation_id=f"conv-{number}",
        )
    both.put(tenant, occurred_at=ON_DAY + 9 * MINUTE, conversation_id="-conv")
    headers = both.customer(tenant)

    rows = exported(both, headers, **PERIOD)
    items = conversations(both, headers, **PERIOD)["items"]

    assert column(rows, "project_name") == ["Chatbot", *("'" + name for name in reversed(names))]
    assert [item["project_name"] for item in items] == ["Chatbot", *reversed(names)]
    assert cell(rows[0], "conversation_id") == "'-conv"
    for row in rows:
        assert cell(row, "charged_amount") == "0.06250000"
        for name in set(COLUMNS) - TEXT_COLUMNS:
            assert not cell(row, name).startswith("'"), name


def test_no_conversation_or_a_foreign_project_is_the_header_only(
    both: UsageEventPortal,
) -> None:
    """§7「只有表头」：区间内没有会话；项目不属于本客户；不存在的项目。

    都是 200，只有 BOM 与表头；后两种不发聚合查询。没有 `conversation_id` 的请求不进文件。
    """
    mine = both.tenant()
    theirs = both.tenant()
    both.put(mine, conversation_id="conv-mine")
    both.put(mine, occurred_at=ON_DAY + MINUTE)
    both.put(theirs, conversation_id="conv-theirs")
    headers = both.customer(mine)
    engine = both.app.state.engine

    empty = export(both, headers, from_date="2026-10-02", to_date="2026-10-02")
    statements, record = record_statements(engine)
    try:
        foreign = export(both, headers, **PERIOD, project_id=both.main_project(theirs).public_id)
        unknown = export(both, headers, **PERIOD, project_id=ZERO_UUID)
    finally:
        event.remove(engine, "before_cursor_execute", record)

    for response in (empty, foreign, unknown):
        assert response.status_code == 200
        assert response.content == HEADER_ONLY
    assert statements, "监听落空"
    assert usage_statements(statements) == []
    assert column(exported(both, headers, **PERIOD), "conversation_id") == ["conv-mine"]


# --- 内存上界 ---------------------------------------------------------------------------


def longest_row(index: int) -> CustomerConversationRow:
    """Every string at its column width, each cell its own object (design §7「内存上界」)."""

    def text(prefix: str, width: int) -> str:
        return f"{prefix}{index:0{width - len(prefix)}d}"

    # 微秒恒非 0：`isoformat` 才总带 `.ffffff`，时间列取最长形态。
    moment = dt.datetime(2026, 10, 26, 12, 34, 56, index % 999_999 + 1)
    return CustomerConversationRow(
        conversation_id=text("cnv", REQUEST_ID_LENGTH),
        project_public_id=f"1{index:07d}{ZERO_UUID[8:]}",
        project_name=text("prj", 255),
        first_occurred_at=moment,
        last_occurred_at=moment,
        request_count=int("999999999999999999"),
        input_tokens=int("999999999999999999"),
        output_tokens=int("999999999999999999"),
        charged_amount=Decimal("999999999999.99999999"),
    )


def test_the_peak_memory_of_one_export_is_bounded() -> None:
    """§7「内存上界」（unit）：20 000 行最长形态会话行，峰值 ≤ 128 MiB。

    走完视图、单元格转换、`write_csv` 与 `encode`。超了是设计 §10 假设 4 不成立：停下说明，
    不在这里调上限。
    """
    tracemalloc.start()
    try:
        rows = [longest_row(index) for index in range(EXPORT_MAX_ROWS)]
        body = write_csv(
            CONVERSATION_EXPORT_COLUMNS,
            (customer_conversation_export_cells(row) for row in rows),
            CONVERSATION_EXPORT_TEXT_COLUMNS,
        )
        del rows
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert body.startswith(BOM)
    assert body.count(b"\r\n") == EXPORT_MAX_ROWS + 1
    [_, first] = read_csv(body)[:2]
    assert len(cell(first, "conversation_id")) == REQUEST_ID_LENGTH
    assert len(cell(first, "project_name")) == 255
    assert cell(first, "charged_amount") == "999999999999.99999999"
    assert cell(first, "input_tokens") == "999999999999999999"
    assert peak <= 128 * MIB, f"export peak {peak / MIB:.1f} MiB exceeds 128 MiB"


# --- 扫描上限 ---------------------------------------------------------------------------


def test_a_scan_past_the_budget_is_query_too_broad(
    mysql_portal: UsageEventPortal, monkeypatch
) -> None:
    """§7「扫描上限」（MySQL）：30 000 条已计费事件分布在 3 000 个会话里，1 毫秒超时。

    接口二 422 `QUERY_TOO_BROAD`，一条 `customer_usage_query_timeout` 日志、不带任何参数值；
    没有 `customer_export_failed`；之后租约空闲。
    """
    portal = mysql_portal
    tenant = portal.tenant()
    bulk_conversations(portal, tenant, 30_000, 3_000, dt.datetime(2026, 9, 30, 16, 0, 0))
    headers = portal.customer(tenant)
    monkeypatch.setattr(
        usage_repository,
        "list_conversations_for_customer",
        functools.partial(list_conversations_for_customer, timeout_ms=1),
    )
    params = {"from_date": DAY, "to_date": DAY, "project_id": portal.main_project(tenant).public_id}

    response, lines = capture_server_log(lambda: export(portal, headers, **params))

    assert error_of(response) == (422, "QUERY_TOO_BROAD")
    body = response.json()
    assert body["data"] is None
    assert body["request_id"]
    [timeout] = timeout_lines(lines)
    assert timeout["request_id"] == body["request_id"]
    assert timeout["customer_id"] == tenant.public_id
    assert not failures_of(lines)
    server_log = json.dumps(lines)
    for value in params.values():
        assert value not in server_log
    assert not export_lease_held()


def test_query_too_broad_keeps_its_422_and_its_warning(
    portal: UsageEventPortal, monkeypatch
) -> None:
    """`QueryTooBroad` 记 `customer_usage_query_timeout` 后 422，不被当成意外错误变成 500。"""
    tenant = portal.tenant()
    portal.put(tenant, conversation_id="conv-secret-0000")
    headers = portal.customer(tenant)

    def too_broad(*_args, **_kwargs):
        raise QueryTooBroad

    monkeypatch.setattr(usage_repository, "list_conversations_for_customer", too_broad)
    response, lines = capture_server_log(
        lambda: export(portal, headers, from_date="2026-09-17", to_date="2026-10-03")
    )

    assert error_of(response) == (422, "QUERY_TOO_BROAD")
    assert len(timeout_lines(lines)) == 1
    assert not failures_of(lines)
    server_log = json.dumps(lines)
    for value in ("2026-09-17", "2026-10-03", "conv-secret-0000"):
        assert value not in server_log
        assert value not in response.text


# --- 内部计量、无成本毛利、金额精度 -----------------------------------------------------


def test_no_cost_rate_reference_price_or_project_secret_reaches_the_file(
    both: UsageEventPortal,
) -> None:
    """§7「内部计量租户」「无成本毛利」（INV-7）：哨兵值都不在 CSV 原始字节里。

    内部计量租户 `charged_amount` 为 `0.00000000`，请求数与 token 照常。
    """
    tenant = both.tenant()
    secret = both.project(
        tenant, "Support desk", description=SENTINEL_DESCRIPTION, webhook=SENTINEL_WEBHOOK
    )
    both.put(tenant, project=secret, costs=SENTINEL_COSTS, conversation_id="conv-1")
    internal = both.tenant()
    both.set_billing_mode(internal, BillingMode.INTERNAL_METERED_ONLY)
    for number in range(2):
        both.put(
            internal,
            occurred_at=ON_DAY + number * MINUTE,
            billable="0",
            mode=BillingMode.INTERNAL_METERED_ONLY,
            reference=SENTINEL_REFERENCE_PRICE,
            costs=SENTINEL_COSTS,
            conversation_id="conv-internal",
        )

    files = [
        export(both, both.customer(tenant), **PERIOD),
        export(both, both.customer(internal), **PERIOD),
    ]

    for response in files:
        assert response.status_code == 200
        assert len(read_csv(response.content)) == 2
        for value in (
            *SENTINEL_TEXTS,
            SENTINEL_DESCRIPTION,
            SENTINEL_WEBHOOK,
            "PREPAID",
            "INTERNAL_METERED_ONLY",
            "PROCESSED",
            "USD",
        ):
            assert value.encode() not in response.content, value
    [internal_row] = read_csv(files[1].content)[1:]
    assert cell(internal_row, "charged_amount") == ZERO
    assert cell(internal_row, "request_count") == "2"
    assert cell(internal_row, "input_tokens") == str(2 * TOKENS["input_tokens"])
    assert cell(internal_row, "output_tokens") == str(2 * TOKENS["output_tokens"])


def test_sums_are_exact_decimals_in_the_file(mysql_portal: UsageEventPortal) -> None:
    """§7「金额精度」（只在 MySQL；SQLite 把 NUMERIC 存成浮点，判别不出）。

    ① 三条 `0.00000001`；② `999999999999.00000000 + 0.00000001`；
    ③ `1234567890.12345678 + 0.00000001`；④ `999999999999.99999999 + 0.00000001`。
    CSV 单元格与接口一的 `charged_amount` 都逐字等于期望值。两笔大额扣费加起来会超出余额列的
    范围，所以 ② 与 ④ 各用一个先记了 1 的客户。
    """
    portal = mysql_portal
    cases = {
        "conv-tiny": (("0.00000001",) * 3, "0.00000003"),
        "conv-whole": (("999999999999.00000000", "0.00000001"), "999999999999.00000001"),
        "conv-mixed": (("1234567890.12345678", "0.00000001"), "1234567890.12345679"),
        "conv-largest": (("999999999999.99999999", "0.00000001"), "1000000000000.00000000"),
    }
    # 前提：②③ 的期望值经 float 往返后与期望值不同。
    for name in ("conv-whole", "conv-mixed"):
        expected = Decimal(cases[name][1])
        assert Decimal(float(expected)).quantize(Decimal("0.00000001")) != expected
    shared = portal.tenant()
    owners = {"conv-tiny": shared, "conv-mixed": shared}
    for name in ("conv-whole", "conv-largest"):
        owners[name] = portal.tenant()
        portal.world.fund(owners[name], "1")
    for name, (amounts, _expected) in cases.items():
        for number, amount in enumerate(amounts):
            portal.put(
                owners[name],
                occurred_at=ON_DAY + number * MINUTE,
                billable=amount,
                conversation_id=name,
            )

    in_file: dict[str, str] = {}
    in_list: dict[str, str] = {}
    for tenant in {id(owner): owner for owner in owners.values()}.values():
        headers = portal.customer(tenant)
        for row in exported(portal, headers, **PERIOD):
            in_file[cell(row, "conversation_id")] = cell(row, "charged_amount")
        for item in conversations(portal, headers, **PERIOD)["items"]:
            in_list[item["conversation_id"]] = item["charged_amount"]

    expected_amounts = {name: expected for name, (_amounts, expected) in cases.items()}
    assert in_file == expected_amounts
    assert in_list == expected_amounts


# --- 租户越权 ---------------------------------------------------------------------------


def test_a_customer_only_ever_exports_its_own_conversations(both: UsageEventPortal) -> None:
    """§7「租户越权」（INV-8）：A、B 共用同一个 `conversation_id` 值。

    A 用 B 的项目 id、B 的游标（导出忽略），并带 B 的标识：
    只得到 A 的行或只有表头的文件；A 的合计不含 B 的事件；
    A 的任何文件里不出现 B 的项目名与金额。
    """
    mine = both.tenant()
    theirs = both.tenant()
    their_project = both.project(theirs, "Theirs secret project")
    both.put(mine, occurred_at=ON_DAY, conversation_id="conv-shared")
    both.put(mine, occurred_at=ON_DAY + MINUTE, conversation_id="conv-shared")
    both.put(mine, occurred_at=ON_DAY + 2 * MINUTE, conversation_id="conv-mine")
    both.put(theirs, occurred_at=ON_DAY + 5 * MINUTE, conversation_id="conv-shared", billable="0.5")
    both.put(
        theirs,
        project=their_project,
        occurred_at=ON_DAY + 6 * MINUTE,
        conversation_id="conv-theirs",
        billable="0.5",
    )
    headers = both.customer(mine)
    their_cursor = conversations(both, both.customer(theirs), **PERIOD, page_size=1)["next_cursor"]
    assert their_cursor is not None
    their_main = both.main_project(theirs)
    forged = {"customer_id": theirs.public_id, "tenant_id": str(theirs.id), "cursor": their_cursor}

    responses = [
        export(both, headers, **PERIOD, **forged),
        export(both, headers, **PERIOD, project_id=their_project.public_id, **forged),
        export(both, headers, **PERIOD, project_id=their_main.public_id, **forged),
    ]

    for response in responses:
        assert response.status_code == 200
    rows = read_csv(responses[0].content)[1:]
    mine_items = conversations(both, headers, **PERIOD)["items"]
    assert rows == [cells_of(item) for item in mine_items]
    assert column(rows, "conversation_id") == ["conv-mine", "conv-shared"]
    assert cell(rows[1], "request_count") == "2"
    assert cell(rows[1], "charged_amount") == "0.12500000"
    assert responses[1].content == HEADER_ONLY
    assert responses[2].content == HEADER_ONLY
    for response in responses:
        for leaked in (
            theirs.public_id,
            their_project.public_id,
            their_main.public_id,
            "Theirs secret project",
            "conv-theirs",
            "0.50000000",
        ):
            assert leaked.encode() not in response.content


# --- 鉴权、只读契约、数据库断连 ---------------------------------------------------------


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
def test_non_customers_are_refused_before_any_query_or_lease(
    portal: UsageEventPortal, monkeypatch, caller: str, expected: tuple[int, str]
) -> None:
    """§7「鉴权」：拒绝的同时不发聚合查询、不取租约。"""
    tenant = portal.tenant()
    portal.put(tenant, conversation_id="conv-1")
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
    acquired: list[object] = []

    def counting_acquire():
        acquired.append(None)
        return try_acquire_export_lease()

    monkeypatch.setattr(customer_api, "try_acquire_export_lease", counting_acquire)
    engine = portal.app.state.engine
    statements, record = record_statements(engine)
    try:
        response = export(portal, headers, **PERIOD)
    finally:
        event.remove(engine, "before_cursor_execute", record)

    assert error_of(response) == expected
    assert response.json()["data"] is None
    assert usage_statements(statements) == []
    assert acquired == []


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
    both.put(tenant, conversation_id="conv-1")
    both.set_account_status(tenant, status)
    headers = both.customer(tenant)
    before = both.tables()

    rows = exported(both, headers, **PERIOD)

    assert column(rows, "request_count") == ["1"]
    assert both.tables() == before


def test_a_lost_database_connection_is_a_500_sent_inside_the_lease(
    both: UsageEventPortal, monkeypatch
) -> None:
    """§7「数据库断连」（接口二）：拿到租约后 repository 抛 `OperationalError`。

    500（`CustomerExportFailed`，信封与通用 500 逐字相同）、
    一条 `customer_export_failed` 日志、没有 `Unhandled exception`；
    请求结束后租约已释放；之后正常请求 200。各表不变。
    """
    headers = three_conversations(both)
    before = both.tables()

    def disconnected(*_args, **_kwargs):
        assert export_lease_held()
        raise OperationalError("SELECT", {}, Exception("fictional lost connection"))

    with monkeypatch.context() as patch:
        patch.setattr(usage_repository, "list_conversations_for_customer", disconnected)
        response, lines = capture_server_log(
            lambda: export(both, headers, **PERIOD, project_id=ZERO_UUID)
        )

    body = response.json()
    assert_generic_500(response.status_code, body)
    assert "fictional lost connection" not in response.text
    [failure] = failures_of(lines)
    assert failure["level"] == "ERROR"
    assert failure["request_id"] == body["request_id"]
    assert "OperationalError" in failure["exception"]
    assert not [line for line in lines if line["message"] == "Unhandled exception"]
    assert ZERO_UUID not in json.dumps(lines)
    assert not export_lease_held()
    assert both.tables() == before
    assert len(exported(both, headers, **PERIOD)) == 3


def test_the_raised_error_does_not_chain_to_the_original(
    portal: UsageEventPortal, monkeypatch
) -> None:
    """其他 `Exception` 在 `except` 块之外抛 `CustomerExportFailed`，不链回原异常。

    直接调用处理函数、让单元格转换抛错：`__context__` 与 `__cause__` 都是 `None`。
    """
    headers = three_conversations(portal)

    def failing(_row):
        raise RuntimeError("fictional projection failure")

    monkeypatch.setattr(customer_api, "customer_conversation_export_cells", failing)
    scope = export_scope(portal.app, headers, PERIOD)
    slot = scope[EXPORT_LEASE_SCOPE_KEY]

    try:
        with pytest.raises(CustomerExportFailed) as raised:
            customer_api.customer_conversations_export(Request(scope), from_date=DAY, to_date=DAY)
        # 处理函数自己不释放：租约还在槽里，等最外层中间件。
        assert export_lease_held()
    finally:
        slot.release()

    assert raised.value.__context__ is None
    assert raised.value.__cause__ is None
    assert (raised.value.code, raised.value.http_status) == ("INTERNAL_ERROR", 500)

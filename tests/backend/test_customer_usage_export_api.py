"""Customer usage CSV export end to end (AIH-TASK-115, design gate #435 v10 §7).

`GET /api/v1/customer/usage-events/export`：筛选与用量页相同、日期恒必填，
复用 `list_processed_events_for_customer` 与 #398 的 18 键白名单，
用 AIH-TASK-114 的 CSV 模块、导出租约与 `ExportCsvResponse`。

§7 用量导出表里 unit 层的行（CSV 转义、公式注入六种前缀、分段发送、内存上界、
中间件顺序、不接管、槽只释放自己的 token、代理配置）在 test_csv_export.py；
「鉴权」的路由守卫在 test_customer_api.py。这里是 integration 层的行，外加
「上限常量」里「查询的 `limit` 是 `EXPORT_MAX_ROWS + 1`」与「抛出的错误不链回原异常」。

数据照 test_customer_usage_api 的写法直接写库（`UsageEventPortal.put`）：
- SQLite：`create_all` 建表，装上代替触发器推进钱包的测试触发器；
- MySQL：表与触发器都走迁移，需要 `BILLING_TEST_DATABASE_URL`，没设时 MySQL 那一半
  skip —— **不要把 skipped 读成 passed**，CI 里不许 skip。

「发送阶段持有租约」「断开与取消」「意外错误的 500 在租约内发出」用 `create_app`
建的真实应用（含 `RequestContextMiddleware` 与最外层的 `ExportLeaseMiddleware`），
以 ASGI 方式直接调用 `app(scope, receive, send)`，暂停或打断传进去的最外层 `send`。
「慢读连接持有租约」在测试线程里起真实的 `uvicorn.Server`，绑本机随机端口，
用原始 socket 慢读，用例结束时关闭服务。

⚠️ 导出租约是进程级的：每条用例开始与结束时都必须空闲（`lease_is_free`）。

⚠️ SQL 文本断言不写死方言字面量；只有 MySQL 才有的断言放在方言分支里。
示例 uuid 用全零占位值。
"""

from __future__ import annotations

import asyncio
import contextlib
import csv
import datetime as dt
import functools
import io
import json
import logging
import re
import socket
import threading
import time
from collections.abc import Iterator
from typing import Any
from urllib.parse import urlencode

import pytest
import uvicorn
from sqlalchemy import event, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker
from starlette.requests import Request
from test_customer_usage_api import (
    DAY,
    MINUTE,
    ON_DAY,
    SENTINEL_ESTIMATED_MYR,
    SENTINEL_FX_RATE,
    SENTINEL_REFERENCE_PRICE,
    SENTINEL_SOURCE_COST,
    ZERO_UUID,
    UsageEventPortal,
    _usage_portal,
    record_statements,
    usage_statements,
)
from test_customer_wallet_api import _sqlite_factory, error_of
from test_usage_billing import _mysql_factory

from app.api import customer as customer_api
from app.core.csv_export import (
    EXPORT_LEASE_SCOPE_KEY,
    EXPORT_MAX_ROWS,
    CustomerExportFailed,
    ExportLeaseSlot,
    export_lease_held,
    release_export_lease,
    try_acquire_export_lease,
)
from app.core.logging import JsonFormatter
from app.models.tenancy import AccountStatus, BillingMode, Project
from app.models.usage import UsageEventStatus
from app.repositories import usage_events as usage_repository
from app.repositories.usage_events import list_processed_events_for_customer
from app.schemas.customer_usage import USAGE_EXPORT_COLUMNS, USAGE_EXPORT_TEXT_COLUMNS

EXPORT = "/api/v1/customer/usage-events/export"
BOM = b"\xef\xbb\xbf"
MIB = 1024 * 1024

# 设计 §2「接口一」：表头恰好这 18 列，按这个顺序。多一列、少一列、换顺序都红。
COLUMNS = [
    "id",
    "event_id",
    "request_id",
    "conversation_id",
    "feature",
    "project_id",
    "project_name",
    "provider",
    "model",
    "usage_type",
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
    "quantity",
    "unit",
    "charged_amount",
    "occurred_at_utc",
]
TEXT_COLUMNS = {
    "id",
    "event_id",
    "request_id",
    "conversation_id",
    "feature",
    "project_id",
    "project_name",
    "provider",
    "model",
    "usage_type",
    "unit",
}
PERIOD = {"from_date": DAY, "to_date": DAY}
FILENAME = f'attachment; filename="usage-events_{DAY}_{DAY}.csv"'


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
    values = {**item, "occurred_at_utc": item["occurred_at"]}
    return ["" if values[name] is None else str(values[name]) for name in COLUMNS]


def column(rows: list[list[str]], name: str) -> list[str]:
    index = COLUMNS.index(name)
    return [row[index] for row in rows]


@contextlib.contextmanager
def server_log() -> Iterator[list[dict]]:
    """Every `app.*` log line written inside the block, parsed from the JSON formatter."""
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.addHandler(handler)
    lines: list[dict] = []
    try:
        yield lines
    finally:
        root.removeHandler(handler)
        # 只看服务端（`app.*`）的日志：测试客户端自己会把完整 URL 记在 INFO 上。
        parsed = (json.loads(raw) for raw in stream.getvalue().splitlines() if raw.strip())
        lines.extend(line for line in parsed if str(line["logger"]).startswith("app."))


def messages(lines: list[dict], message: str) -> list[dict]:
    return [line for line in lines if line["message"] == message]


def assert_generic_500(status: int, body: dict) -> None:
    """The same envelope as `handle_unexpected_error`'s, field for field."""
    assert status == 500
    assert set(body) == {"success", "data", "error", "request_id"}
    assert body["success"] is False
    assert body["data"] is None
    assert body["error"] == {"code": "INTERNAL_ERROR", "message": "An internal error occurred."}
    assert body["request_id"]


def failing_projection(monkeypatch: pytest.MonkeyPatch, *, at_row: int) -> None:
    """Make the whitelist projection raise `RuntimeError` on row `at_row` (1-based)."""
    original = customer_api.customer_usage_export_cells
    seen = [0]

    def projection(row):
        seen[0] += 1
        if seen[0] == at_row:
            raise RuntimeError("fictional projection failure")
        return original(row)

    monkeypatch.setattr(customer_api, "customer_usage_export_cells", projection)


def three_rows(portal: UsageEventPortal) -> dict[str, str]:
    """A customer with three billed events on `DAY`; its signed-in headers."""
    tenant = portal.tenant()
    for number in range(3):
        portal.put(tenant, occurred_at=ON_DAY + number * MINUTE)
    return portal.customer(tenant)


# --- 真实应用的 ASGI 调用 ---------------------------------------------------------------


def export_scope(headers: dict[str, str], params: dict[str, str]) -> dict[str, Any]:
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
    }


async def call(app: Any, scope: dict[str, Any], send: Any) -> None:
    delivered = False

    async def receive() -> dict[str, Any]:
        nonlocal delivered
        if not delivered:
            delivered = True
            return {"type": "http.request", "body": b"", "more_body": False}
        # 没有更多请求体：挂起到响应结束时被取消（连接仍开着时真实服务器也是这样）。
        await asyncio.Event().wait()
        return {"type": "http.disconnect"}

    await app(scope, receive, send)


class Recorded:
    """Every message the outermost `send` received."""

    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []

    async def send(self, message: dict[str, Any]) -> None:
        self.messages.append(message)

    @property
    def status(self) -> int:
        return self.messages[0]["status"]

    @property
    def headers(self) -> dict[str, str]:
        return {
            name.decode("latin-1").lower(): value.decode("latin-1")
            for name, value in self.messages[0]["headers"]
        }

    @property
    def body(self) -> bytes:
        return b"".join(bytes(message.get("body", b"")) for message in self.messages[1:])


class Pausing(Recorded):
    """Pauses the first time a message of type `on` arrives, until `go` is set."""

    def __init__(self, on: str) -> None:
        super().__init__()
        self.on = on
        self.paused = asyncio.Event()
        self.go = asyncio.Event()

    async def send(self, message: dict[str, Any]) -> None:
        self.messages.append(message)
        if message["type"] == self.on and not self.paused.is_set():
            self.paused.set()
            await self.go.wait()


# --- 正常路径 ---------------------------------------------------------------------------


def test_the_export_matches_the_json_endpoint_row_for_row(both: UsageEventPortal) -> None:
    """§7「正常路径」：三条已计费（两个项目）+ 一条 RECEIVED、一条 FAILED。

    3 行、最新在前；每行每列与 JSON 接口同一区间的同名键逐字相等（时间列对 `occurred_at`）。
    """
    tenant = both.tenant()
    other = both.project(tenant, "Support desk")
    stored = [
        both.put(tenant, occurred_at=ON_DAY, conversation_id="conv-1", feature="crm.summary"),
        both.put(tenant, project=other, occurred_at=ON_DAY + MINUTE, seconds="12.5"),
        both.put(tenant, occurred_at=ON_DAY + 2 * MINUTE, billable="0.125"),
    ]
    both.put(tenant, status=UsageEventStatus.RECEIVED, occurred_at=ON_DAY + 3 * MINUTE)
    both.put(tenant, status=UsageEventStatus.FAILED_FINAL, occurred_at=ON_DAY + 4 * MINUTE)
    headers = both.customer(tenant)
    before = both.tables()

    response = export(both, headers, **PERIOD)

    assert response.status_code == 200
    assert response.headers["content-type"] == "text/csv; charset=utf-8"
    assert response.headers["content-disposition"] == FILENAME
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-accel-buffering"] == "no"
    assert response.headers["content-length"] == str(len(response.content))
    assert response.content.startswith(BOM)
    assert response.content.endswith(b"\r\n")
    table = read_csv(response.content)
    assert table[0] == COLUMNS
    rows = table[1:]
    assert column(rows, "id") == [row.public_id for row in reversed(stored)]
    items = both.usage(headers, **PERIOD, page_size=100)["items"]
    assert rows == [cells_of(item) for item in items]
    assert rows[2][COLUMNS.index("occurred_at_utc")] == "2026-10-01T02:00:00.123456"
    assert rows[2][COLUMNS.index("conversation_id")] == "conv-1"
    assert rows[1][COLUMNS.index("project_name")] == "Support desk"
    assert rows[1][COLUMNS.index("quantity")] == "12.50000000"
    assert rows[1][COLUMNS.index("input_tokens")] == ""
    assert rows[0][COLUMNS.index("charged_amount")] == "0.12500000"
    assert both.tables() == before


def test_the_column_lists_are_the_designed_ones() -> None:
    """§2「接口一」：列清单与文本列就是设计写的那些（INV-7：列清单固定）。"""
    assert list(USAGE_EXPORT_COLUMNS) == COLUMNS
    assert set(USAGE_EXPORT_TEXT_COLUMNS) == TEXT_COLUMNS


def test_every_filter_gives_the_same_rows_as_the_json_endpoint(both: UsageEventPortal) -> None:
    """§7「筛选一致」：各筛选的行集合与 JSON 接口同筛选翻完全部页的结果相同。"""
    tenant = both.tenant()
    other = both.project(tenant, "Support desk")
    a = both.put(tenant, occurred_at=ON_DAY, conversation_id="conv-a")
    both.put(tenant, project=other, occurred_at=ON_DAY + MINUTE, provider="openai")
    both.put(
        tenant,
        project=other,
        occurred_at=ON_DAY + 2 * MINUTE,
        model="gpt-x",
        conversation_id="conv-a",
    )
    both.put(tenant, occurred_at=ON_DAY + 3 * MINUTE, provider="openai", model="gpt-x")
    headers = both.customer(tenant)

    for filters in (
        {"project_id": other.public_id},
        {"provider": "openai"},
        {"model": "gpt-x"},
        {"conversation_id": "conv-a"},
        {"event_id": a.event_id},
        {"provider": "openai", "model": "gpt-x"},
    ):
        expected = [cells_of(item) for item in both.walk(headers, **PERIOD, page_size=1, **filters)]
        assert expected, filters
        assert exported(both, headers, **PERIOD, **filters) == expected, filters


def test_the_dates_are_required_even_with_an_event_id(both: UsageEventPortal) -> None:
    """§7「日期必填」：只带 `event_id` → 422（JSON 接口同样的参数是 200）。"""
    tenant = both.tenant()
    stored = both.put(tenant)
    headers = both.customer(tenant)

    response = export(both, headers, event_id=stored.event_id)

    assert error_of(response) == (422, "VALIDATION_ERROR")
    assert response.json()["error"]["message"] == (
        "Invalid request fields: query.from_date, query.to_date"
    )
    assert stored.event_id not in response.text
    page = both.usage(headers, event_id=stored.event_id)
    assert [item["id"] for item in page["items"]] == [stored.public_id]
    for params, fields in (
        ({"event_id": stored.event_id, "from_date": DAY}, "query.to_date"),
        ({"event_id": stored.event_id, "to_date": DAY}, "query.from_date"),
        ({}, "query.from_date, query.to_date"),
    ):
        missing = export(both, headers, **params)
        assert error_of(missing) == (422, "VALIDATION_ERROR")
        assert missing.json()["error"]["message"] == f"Invalid request fields: {fields}"


@pytest.mark.parametrize(
    "params",
    [
        {"from_date": "2026-07-01", "to_date": "2026-10-01"},
        {"from_date": "2026-10-02", "to_date": DAY},
        {"from_date": "0001-01-01", "to_date": "0001-01-02"},
        {"from_date": "2099-12-31", "to_date": "2100-01-01"},
        {"from_date": "2026-02-30", "to_date": "2026-03-01"},
        {"from_date": "2026/10/01", "to_date": DAY},
        {**PERIOD, "provider": "Bad Provider"},
        {**PERIOD, "model": "claude x"},
        {**PERIOD, "conversation_id": "has space"},
        {**PERIOD, "event_id": "not-an-event-id"},
    ],
)
def test_validation_is_the_json_endpoints(portal: UsageEventPortal, params: dict) -> None:
    """§7「校验复用」：与 JSON 接口相同的 422、相同的文案，不回显值。"""
    headers = portal.customer(portal.tenant())

    response = export(portal, headers, **params)
    page = portal.get_usage(headers, **params)

    assert error_of(response) == (422, "VALIDATION_ERROR")
    body = response.json()
    assert body == {**page.json(), "request_id": body["request_id"]}
    assert body["data"] is None
    for value in params.values():
        if value != DAY:
            assert value not in body["error"]["message"]


def test_paging_and_unknown_parameters_are_ignored(portal: UsageEventPortal) -> None:
    """§2「接口一」：没有 `page_size` 与 `cursor`，给了按未知参数忽略。"""
    headers = three_rows(portal)

    rows = exported(
        portal, headers, **PERIOD, page_size="0", cursor="not base64!", sort="asc", total="yes"
    )

    assert len(rows) == 3


# --- 上限 -------------------------------------------------------------------------------


def test_more_rows_than_the_cap_is_export_too_large_without_any_row(
    both: UsageEventPortal, monkeypatch
) -> None:
    """§7「上限」：上限 monkeypatch 成 3。

    4 条 → 422 `EXPORT_TOO_LARGE`，响应是 JSON 信封、没有 CSV 行；恰好 3 条 → 200、3 行。
    """
    monkeypatch.setattr(customer_api, "EXPORT_MAX_ROWS", 3)
    four = both.tenant()
    for number in range(4):
        both.put(four, occurred_at=ON_DAY + number * MINUTE, request_id=f"req-four-{number}")
    three = both.tenant()
    for number in range(3):
        both.put(three, occurred_at=ON_DAY + number * MINUTE)
    four_headers = both.customer(four)
    three_headers = both.customer(three)

    too_large = export(both, four_headers, **PERIOD)
    fits = exported(both, three_headers, **PERIOD)

    assert error_of(too_large) == (422, "EXPORT_TOO_LARGE")
    assert too_large.headers["content-type"].startswith("application/json")
    assert "content-disposition" not in too_large.headers
    assert not too_large.content.startswith(BOM)
    body = too_large.json()
    assert body["data"] is None
    assert body["request_id"]
    assert "20,000" in body["error"]["message"]
    for number in range(4):
        assert f"req-four-{number}" not in too_large.text
    assert len(fits) == 3


def test_the_query_asks_for_one_row_more_than_the_cap(
    portal: UsageEventPortal, monkeypatch
) -> None:
    """§7「上限常量」：`EXPORT_MAX_ROWS` 是 20 000，查询的 `limit` 是它 + 1。"""
    headers = three_rows(portal)
    limits: list[int] = []
    original = usage_repository.list_processed_events_for_customer

    def spy(*args, **kwargs):
        limits.append(kwargs["limit"])
        return original(*args, **kwargs)

    monkeypatch.setattr(usage_repository, "list_processed_events_for_customer", spy)

    assert len(exported(portal, headers, **PERIOD)) == 3
    assert EXPORT_MAX_ROWS == 20_000
    assert limits == [EXPORT_MAX_ROWS + 1]


def test_a_scan_past_the_budget_is_query_too_broad(
    mysql_portal: UsageEventPortal, monkeypatch
) -> None:
    """§7「扫描上限」（MySQL）：30 000 条当月事件，测试专用 1 毫秒超时。

    422 `QUERY_TOO_BROAD`；一条 `customer_usage_query_timeout` 日志，只带 `request_id`
    与客户 id。
    """
    portal = mysql_portal
    tenant = portal.tenant()
    portal.bulk(tenant, 30_000, dt.datetime(2026, 9, 30, 16, 0, 0))
    headers = portal.customer(tenant)
    monkeypatch.setattr(
        usage_repository,
        "list_processed_events_for_customer",
        functools.partial(list_processed_events_for_customer, timeout_ms=1),
    )

    with server_log() as lines:
        response = export(portal, headers, from_date="2026-10-01", to_date="2026-10-31")

    assert error_of(response) == (422, "QUERY_TOO_BROAD")
    body = response.json()
    assert body["data"] is None
    assert body["request_id"]
    timeouts = messages(lines, "customer_usage_query_timeout")
    assert len(timeouts) == 1
    assert timeouts[0]["request_id"] == body["request_id"]
    assert timeouts[0]["customer_id"] == tenant.public_id
    assert "2026-10-31" not in json.dumps(lines)
    assert not messages(lines, "customer_export_failed")


def test_query_too_broad_keeps_its_422_and_its_warning(
    portal: UsageEventPortal, monkeypatch
) -> None:
    """契约勘误：`QueryTooBroad` 照旧转换，不被当成意外错误变成 500。"""
    tenant = portal.tenant()
    portal.put(tenant)
    headers = portal.customer(tenant)

    def too_broad(*_args, **_kwargs):
        raise usage_repository.QueryTooBroad

    monkeypatch.setattr(usage_repository, "list_processed_events_for_customer", too_broad)
    with server_log() as lines:
        response = export(portal, headers, **PERIOD, conversation_id="conv-secret-0000")

    assert error_of(response) == (422, "QUERY_TOO_BROAD")
    assert len(messages(lines, "customer_usage_query_timeout")) == 1
    assert not messages(lines, "customer_export_failed")
    assert "conv-secret-0000" not in json.dumps(lines)


# --- 金额、公式注入、空结果 -------------------------------------------------------------


def test_amounts_and_quantities_are_written_exactly(both: UsageEventPortal) -> None:
    """§7「金额精度」（INV-10）：上界、`0.00000001` 与 `quantity` 最小值原样出现在文件里。

    ⚠️ 上界只在 MySQL 上断言：SQLite 把 NUMERIC 存成浮点，20 位有效数字在库里就变了。
    """
    if both.factory.kw["bind"].dialect.name == "mysql":
        top = "999999999999.99999999"
    else:
        top = "1234567.12345678"
    smallest = both.tenant()
    largest = both.tenant()
    both.put(smallest, billable="0.00000001")
    both.put(smallest, seconds="0.00000001", billable="0", occurred_at=ON_DAY - MINUTE)
    both.put(largest, billable=top)

    small = export(both, both.customer(smallest), **PERIOD)
    large = export(both, both.customer(largest), **PERIOD)

    small_rows = read_csv(small.content)[1:]
    [large_row] = read_csv(large.content)[1:]
    assert column(small_rows, "charged_amount") == ["0.00000001", "0.00000000"]
    assert column(small_rows, "quantity") == ["", "0.00000001"]
    assert large_row[COLUMNS.index("charged_amount")] == top
    assert b",0.00000001," in small.content
    assert top.encode() in large.content


def test_formula_prefixes_are_quoted_in_text_columns_only(both: UsageEventPortal) -> None:
    """§7「公式注入」：`=1+1`、`-2`、`@x`、项目名 `+cmd`（直接写库）。

    这些单元格以 `'` 开头；`charged_amount` 与其他数值、时间列不加。
    """
    tenant = both.tenant()
    project = both.project(tenant, "Formula project")
    with both.factory() as session:
        session.execute(update(Project).where(Project.id == project.id).values(name="+cmd"))
        session.commit()
    both.put(tenant, project=project, request_id="=1+1", conversation_id="-2", feature="@x")
    headers = both.customer(tenant)

    [row] = exported(both, headers, **PERIOD)

    assert row[COLUMNS.index("request_id")] == "'=1+1"
    assert row[COLUMNS.index("conversation_id")] == "'-2"
    assert row[COLUMNS.index("feature")] == "'@x"
    assert row[COLUMNS.index("project_name")] == "'+cmd"
    assert row[COLUMNS.index("charged_amount")] == "0.06250000"
    for name in set(COLUMNS) - TEXT_COLUMNS:
        assert not row[COLUMNS.index(name)].startswith("'"), name


def test_an_empty_period_or_a_foreign_project_is_the_header_only(both: UsageEventPortal) -> None:
    """§7「空结果」：区间内没有事件、别人的项目 id、不存在的项目 → 200，只有 BOM 与表头。"""
    mine = both.tenant()
    theirs = both.tenant()
    both.put(mine)
    both.put(theirs)
    headers = both.customer(mine)
    header_only = BOM + (",".join(COLUMNS) + "\r\n").encode("ascii")

    for params in (
        {"from_date": "2026-10-02", "to_date": "2026-10-02"},
        {**PERIOD, "project_id": both.main_project(theirs).public_id},
        {**PERIOD, "project_id": ZERO_UUID},
    ):
        response = export(both, headers, **params)
        assert response.status_code == 200
        assert response.content == header_only


# --- 租户越权、泄漏、只读 ---------------------------------------------------------------


def test_a_customer_only_ever_exports_its_own_usage(both: UsageEventPortal) -> None:
    """§7「租户越权」（INV-8）：A 用 B 的项目 id、`event_id`、`conversation_id` 导出。

    只得到表头；B 的任何值都不出现在文件里。
    """
    mine = both.tenant()
    theirs = both.tenant()
    their_project = both.project(theirs, "Theirs secret project")
    mine_rows = [both.put(mine, occurred_at=ON_DAY + n * MINUTE) for n in range(2)]
    their_rows = [
        both.put(
            theirs,
            project=their_project,
            occurred_at=ON_DAY + n * MINUTE,
            conversation_id="conv-theirs-0000",
            request_id=f"req-theirs-{n}",
        )
        for n in range(2)
    ]
    headers = both.customer(mine)
    forged = {"customer_id": theirs.public_id, "tenant_id": str(theirs.id)}

    responses = [
        export(both, headers, **PERIOD, project_id=their_project.public_id, **forged),
        export(both, headers, **PERIOD, event_id=their_rows[0].event_id, **forged),
        export(both, headers, **PERIOD, conversation_id="conv-theirs-0000", **forged),
        export(both, headers, **PERIOD, **forged),
    ]

    for response in responses[:3]:
        assert response.status_code == 200
        assert read_csv(response.content) == [COLUMNS]
    assert column(read_csv(responses[3].content)[1:], "id") == [
        row.public_id for row in reversed(mine_rows)
    ]
    for response in responses:
        for leaked in (
            theirs.public_id,
            their_project.public_id,
            "Theirs secret project",
            "conv-theirs-0000",
            *(row.public_id for row in their_rows),
            *(row.event_id for row in their_rows),
            *(f"req-theirs-{n}" for n in range(2)),
        ):
            assert leaked.encode() not in response.content


def test_no_cost_rate_or_reference_price_reaches_the_file(both: UsageEventPortal) -> None:
    """§7「泄漏」（INV-7）：事件带成本、汇率、参考价的哨兵值，文件原始字节里都没有。"""
    sentinels = {
        "provider_source_cost": SENTINEL_SOURCE_COST,
        "estimated_provider_cost_myr": SENTINEL_ESTIMATED_MYR,
        "fx_rate_applied": SENTINEL_FX_RATE,
    }
    tenant = both.tenant()
    both.put(tenant, costs=sentinels)
    internal = both.tenant()
    both.set_billing_mode(internal, BillingMode.INTERNAL_METERED_ONLY)
    both.put(
        internal,
        billable="0",
        mode=BillingMode.INTERNAL_METERED_ONLY,
        reference=SENTINEL_REFERENCE_PRICE,
        costs=sentinels,
    )

    files = [
        export(both, both.customer(tenant), **PERIOD),
        export(both, both.customer(internal), **PERIOD),
    ]

    for response in files:
        assert response.status_code == 200
        assert len(read_csv(response.content)) == 2
        for value in (
            "0.13579246",
            "0.24681357",
            "0.97531864",
            "0.86420975",
            "PREPAID",
            "INTERNAL_METERED_ONLY",
            "PROCESSED",
            "USD",
        ):
            assert value.encode() not in response.content, value


@pytest.mark.parametrize("status", [AccountStatus.DISABLED, AccountStatus.CLOSED])
def test_a_read_only_customer_can_export(both: UsageEventPortal, status: AccountStatus) -> None:
    """§6「鉴权主体」：只读客户可以导出，导出是读。"""
    tenant = both.tenant()
    stored = both.put(tenant)
    both.set_account_status(tenant, status)
    headers = both.customer(tenant)

    assert column(exported(both, headers, **PERIOD), "id") == [stored.public_id]


# --- 并发上限、意外错误、数据库断连 -----------------------------------------------------


def test_a_held_lease_is_export_busy_without_any_usage_query(both: UsageEventPortal) -> None:
    """§7「并发上限」（接口部分）：占住租约时 503 `EXPORT_BUSY`、`Retry-After: 5`。

    没有发出用量查询（记录执行的语句）；释放后照常 200。
    """
    headers = three_rows(both)
    engine = both.factory.kw["bind"]
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
    assert statements, "监听落空"
    assert usage_statements(statements) == []
    projects = re.compile(r"\bprojects\b", re.IGNORECASE)
    assert not [statement for statement in statements if projects.search(statement)]
    assert len(exported(both, headers, **PERIOD)) == 3


def test_a_failure_while_projecting_frees_the_lease_for_the_next_export(
    both: UsageEventPortal, monkeypatch
) -> None:
    """§7「并发上限」：处理中抛错（投影函数抛异常）之后租约已释放、下一次导出 200。"""
    headers = three_rows(both)

    with monkeypatch.context() as patch:
        failing_projection(patch, at_row=2)
        failed = export(both, headers, **PERIOD)

    assert_generic_500(failed.status_code, failed.json())
    assert "fictional projection failure" not in failed.text
    assert not export_lease_held()
    assert len(exported(both, headers, **PERIOD)) == 3


def test_a_lost_database_connection_is_a_500_sent_inside_the_lease(
    both: UsageEventPortal, monkeypatch
) -> None:
    """§7「数据库断连（用量导出）」：拿到租约后查询抛 `OperationalError` → 500，不是 503。

    由内层处理器在租约内发出：有 `customer_export_failed`，没有 `Unhandled exception`
    （默认的 `TestClient` 遇到未处理的异常会直接抛出）；之后租约空闲，下一次导出 200。
    """
    headers = three_rows(both)

    def disconnected(*_args, **_kwargs):
        assert export_lease_held()
        raise OperationalError("SELECT", {}, Exception("fictional lost connection"))

    with monkeypatch.context() as patch, server_log() as lines:
        patch.setattr(usage_repository, "list_processed_events_for_customer", disconnected)
        response = export(both, headers, **PERIOD, conversation_id="conv-secret-0000")

    body = response.json()
    assert_generic_500(response.status_code, body)
    assert "fictional lost connection" not in response.text
    failures = messages(lines, "customer_export_failed")
    assert len(failures) == 1
    assert failures[0]["level"] == "ERROR"
    assert failures[0]["request_id"] == body["request_id"]
    assert "OperationalError" in failures[0]["exception"]
    assert not messages(lines, "Unhandled exception")
    assert "conv-secret-0000" not in json.dumps(lines)
    assert not export_lease_held()
    assert len(exported(both, headers, **PERIOD)) == 3


def test_the_raised_error_does_not_chain_to_the_original(
    portal: UsageEventPortal, monkeypatch
) -> None:
    """§7「抛出的错误不链回原异常」：直接调用处理函数、让投影抛错。

    抛出的 `CustomerExportFailed` 的 `__context__` 与 `__cause__` 都是 `None`。
    """
    headers = three_rows(portal)
    failing_projection(monkeypatch, at_row=1)
    slot = ExportLeaseSlot()
    scope = {**export_scope(headers, PERIOD), "app": portal.app, EXPORT_LEASE_SCOPE_KEY: slot}

    try:
        with pytest.raises(CustomerExportFailed) as raised:
            customer_api.customer_usage_events_export(Request(scope), from_date=DAY, to_date=DAY)
        # 处理函数自己不释放：租约还在槽里，等最外层中间件。
        assert export_lease_held()
    finally:
        slot.release()

    assert raised.value.__context__ is None
    assert raised.value.__cause__ is None
    assert (raised.value.code, raised.value.http_status) == ("INTERNAL_ERROR", 500)


# --- 经真实中间件栈 ---------------------------------------------------------------------


def test_the_lease_is_held_while_the_outermost_send_is_paused(both: UsageEventPortal) -> None:
    """§7「发送阶段持有租约（经真实中间件栈）」：外层 `send` 停在 body 上。

    暂停期间第二个导出 503 `EXPORT_BUSY`；第一个的 `app(...)` 返回后租约空闲，第三个 200。
    """
    headers = three_rows(both)
    app = both.app

    async def scenario() -> tuple[Pausing, Recorded]:
        first = Pausing("http.response.body")
        task = asyncio.create_task(call(app, export_scope(headers, PERIOD), first.send))
        await first.paused.wait()
        assert export_lease_held()
        second = Recorded()
        await call(app, export_scope(headers, PERIOD), second.send)
        # 第二个已经发完，第一个仍停着：租约仍被它持有。
        assert export_lease_held()
        first.go.set()
        await task
        return first, second

    first, second = asyncio.run(scenario())

    assert second.status == 503
    assert second.headers["retry-after"] == "5"
    assert json.loads(second.body)["error"]["code"] == "EXPORT_BUSY"
    assert first.status == 200
    assert len(read_csv(first.body)) == 4
    assert not export_lease_held()
    assert len(exported(both, headers, **PERIOD)) == 3


def test_a_send_that_fails_on_the_body_frees_the_lease(both: UsageEventPortal) -> None:
    """§7「断开与取消」：最外层 `send` 在 body 消息上抛 `OSError`；结束后租约空闲。"""
    headers = three_rows(both)
    app = both.app
    seen: list[str] = []

    async def broken(message: dict[str, Any]) -> None:
        seen.append(message["type"])
        if message["type"] == "http.response.body":
            assert export_lease_held()
            raise OSError("fictional connection reset")

    async def scenario() -> None:
        # 异常往外穿过各层；外层的 ServerErrorMiddleware 在响应已开始后原样重新抛出。
        with contextlib.suppress(Exception):
            await call(app, export_scope(headers, PERIOD), broken)

    asyncio.run(scenario())

    assert seen == ["http.response.start", "http.response.body"]
    assert not export_lease_held()
    assert len(exported(both, headers, **PERIOD)) == 3


def test_cancelling_a_paused_export_frees_the_lease(both: UsageEventPortal) -> None:
    """§7「断开与取消」：在最外层 `send` 暂停时取消调用 `app(...)` 的任务；之后租约空闲。"""
    headers = three_rows(both)
    app = both.app

    async def scenario() -> None:
        first = Pausing("http.response.body")
        task = asyncio.create_task(call(app, export_scope(headers, PERIOD), first.send))
        await first.paused.wait()
        assert export_lease_held()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())

    assert not export_lease_held()
    assert len(exported(both, headers, **PERIOD)) == 3


def test_an_unexpected_error_is_a_500_sent_inside_the_lease(
    both: UsageEventPortal, monkeypatch
) -> None:
    """§7「意外错误的 500 在租约内发出」：投影在第 2 行抛 `RuntimeError`。

    最外层 `send` 停在这个 500 的 `http.response.start` 上时第二个导出 503；放行后租约空闲。
    500 与通用 500 的信封相同、带 `request_id`；日志里有 `customer_export_failed`，
    没有 `handle_unexpected_error` 的 `Unhandled exception`。
    """
    headers = three_rows(both)
    app = both.app

    async def scenario() -> tuple[Pausing, Recorded]:
        first = Pausing("http.response.start")
        task = asyncio.create_task(call(app, export_scope(headers, PERIOD), first.send))
        await first.paused.wait()
        assert export_lease_held()
        second = Recorded()
        await call(app, export_scope(headers, PERIOD), second.send)
        assert export_lease_held()
        first.go.set()
        await task
        return first, second

    with monkeypatch.context() as patch, server_log() as lines:
        failing_projection(patch, at_row=2)
        first, second = asyncio.run(scenario())

    body = json.loads(first.body)
    assert_generic_500(first.status, body)
    assert first.headers["content-type"].startswith("application/json")
    assert second.status == 503
    assert json.loads(second.body)["error"]["code"] == "EXPORT_BUSY"
    assert not export_lease_held()
    failures = messages(lines, "customer_export_failed")
    assert len(failures) == 1
    assert failures[0]["request_id"] == body["request_id"]
    assert "RuntimeError" in failures[0]["exception"]
    assert not messages(lines, "Unhandled exception")
    assert len(exported(both, headers, **PERIOD)) == 3


# --- 真实 uvicorn -----------------------------------------------------------------------


def raw_request(
    port: int, target: str, headers: dict[str, str], *, rcvbuf: int | None = None
) -> socket.socket:
    connection = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if rcvbuf is not None:
        # 连接之前设：接收窗口在握手时就定了。
        connection.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, rcvbuf)
    connection.settimeout(30)
    connection.connect(("127.0.0.1", port))
    lines = [
        f"GET {target} HTTP/1.1",
        f"Host: 127.0.0.1:{port}",
        "Connection: close",
        *(f"{name}: {value}" for name, value in headers.items()),
        "",
        "",
    ]
    connection.sendall("\r\n".join(lines).encode("latin-1"))
    return connection


def read_head(connection: socket.socket) -> tuple[int, dict[str, str], bytes]:
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = connection.recv(65_536)
        assert chunk, "connection closed before the response head"
        data += chunk
    head, _, rest = data.partition(b"\r\n\r\n")
    status_line, *header_lines = head.decode("latin-1").split("\r\n")
    headers = {}
    for line in header_lines:
        name, _, value = line.partition(":")
        headers[name.strip().lower()] = value.strip()
    return int(status_line.split()[1]), headers, rest


def read_until(connection: socket.socket, data: bytes, count: int) -> bytes:
    """Read until `count` bytes are in hand or the server closes."""
    buffer = bytearray(data)
    while len(buffer) < count:
        chunk = connection.recv(65_536)
        if not chunk:
            break
        buffer += chunk
    return bytes(buffer)


def wait_for(condition, *, seconds: float = 10) -> None:
    deadline = time.monotonic() + seconds
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.01)


def test_a_slow_reader_holds_the_lease_over_real_uvicorn(
    both: UsageEventPortal, monkeypatch
) -> None:
    """§7「慢读连接持有租约（真实 uvicorn）」：导出生成 monkeypatch 成 8 MiB。

    第一条连接（`SO_RCVBUF` 设小）读完响应头与前 256 KiB 后停住，期间第二个导出 503；
    第一条读到的总字节数等于 `Content-Length`；第三个 200；用例结束时关闭服务。
    """
    headers = three_rows(both)
    payload = b"0123456789abcdef" * (8 * MIB // 16)
    monkeypatch.setattr(customer_api, "write_csv", lambda *_args, **_kwargs: payload)
    target = f"{EXPORT}?{urlencode(PERIOD)}"
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    # `log_config=None`：不让 uvicorn 改写测试进程的日志配置。
    config = uvicorn.Config(both.app, lifespan="off", log_config=None, access_log=False)
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    try:
        wait_for(lambda: server.started)

        first = raw_request(port, target, headers, rcvbuf=4096)
        try:
            status, first_headers, rest = read_head(first)
            assert status == 200
            length = int(first_headers["content-length"])
            assert length == len(payload)
            assert first_headers["x-accel-buffering"] == "no"
            partial = read_until(first, rest, 256 * 1024)
            assert len(partial) < length
            # 停住不读：服务端的分段发送卡在流控上，租约仍被持有。
            assert export_lease_held()

            second = raw_request(port, target, headers)
            try:
                busy_status, busy_headers, busy_rest = read_head(second)
                busy_body = read_until(second, busy_rest, int(busy_headers["content-length"]))
            finally:
                second.close()
            assert busy_status == 503
            assert busy_headers["retry-after"] == "5"
            assert json.loads(busy_body)["error"]["code"] == "EXPORT_BUSY"
            assert export_lease_held()

            whole = read_until(first, partial, length)
        finally:
            first.close()
        assert len(whole) == length
        assert whole == payload

        # 最后一段交给传输之后，最外层中间件的 `finally` 才释放。
        wait_for(lambda: not export_lease_held())
        third = raw_request(port, target, headers)
        try:
            third_status, third_headers, third_rest = read_head(third)
            third_body = read_until(third, third_rest, int(third_headers["content-length"]))
        finally:
            third.close()
        assert third_status == 200
        assert third_body == payload
        wait_for(lambda: not export_lease_held())
    finally:
        server.should_exit = True
        thread.join(timeout=30)
        listener.close()
    assert not thread.is_alive()

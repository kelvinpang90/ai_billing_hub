"""The four customer exports with `format=xlsx` end to end (AIH-TASK-236, design gate #753 v1 §7).

用量、钱包流水、月度汇总、会话摘要四个导出接口加了查询参数 `format`（缺省 `csv`，只认 `csv` /
`xlsx`）。这里是 §7 标 AIH-TASK-236 的各行：正常路径与逐格一致、`format` 校验、错误一致、跨租户、
INV-7 列与成员、恶意与异常文本。xlsx 写出模块本身（单元格类型、字符清洗、临时文件、内存）的 unit
行在 test_xlsx_export.py；CSV 本身的行在四个 CSV 导出测试文件，这里不重复。

⚠️ **只跑 SQLite**（设计 §7 末段）：查询代码路径与 CSV 是同一段、已由现有 CSV 导出测试的 MySQL 一半
覆盖；xlsx 只改会话关闭之后的编码。

数据照 test_customer_usage_api 的写法直接写库（`UsageEventPortal.put`），钱包流水照
test_customer_wallet_export_api 的 `post_at` 经真实的 `post_transaction` 写入。这里只从现有测试模块
导入普通函数与常量，不转引用夹具。

⚠️ 导出租约是进程级的：每条用例开始与结束时都必须空闲（`lease_is_free`）。

⚠️ 不断言 SQL 文本；示例 uuid 用全零占位值；时间一律注入。
"""

from __future__ import annotations

import io
import zipfile
from collections.abc import Iterator
from typing import Any, NamedTuple

import openpyxl
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete
from test_customer_usage_api import (
    DAY,
    MINUTE,
    ON_DAY,
    ZERO_UUID,
    UsageEventPortal,
    _usage_portal,
)
from test_customer_usage_export_api import (
    BOM,
    assert_generic_500,
    messages,
    read_csv,
    server_log,
)
from test_customer_wallet_api import _sqlite_factory, error_of
from test_customer_wallet_export_api import post_at
from test_internal_cost_visibility import FORBIDDEN
from test_usage_billing import Tenancy
from test_xlsx_export import ZIP_MEMBERS, assert_matches_csv, read_values

from app.api import customer as customer_api
from app.core.config import Settings
from app.core.csv_export import (
    export_lease_held,
    release_export_lease,
    try_acquire_export_lease,
)
from app.core.xlsx_export import XLSX_MEDIA_TYPE
from app.main import create_app
from app.models.wallet import TransactionType, Wallet
from app.repositories import usage_events as usage_repository
from app.repositories import wallet as wallet_repository
from app.schemas.customer_conversations import (
    CONVERSATION_EXPORT_COLUMNS,
    CONVERSATION_EXPORT_INTEGER_COLUMNS,
    CONVERSATION_EXPORT_TEXT_COLUMNS,
)
from app.schemas.customer_usage import (
    USAGE_EXPORT_COLUMNS,
    USAGE_EXPORT_INTEGER_COLUMNS,
    USAGE_EXPORT_TEXT_COLUMNS,
    USAGE_SUMMARY_EXPORT_COLUMNS,
    USAGE_SUMMARY_EXPORT_INTEGER_COLUMNS,
    USAGE_SUMMARY_EXPORT_TEXT_COLUMNS,
)
from app.schemas.customer_wallet import (
    WALLET_EXPORT_COLUMNS,
    WALLET_EXPORT_INTEGER_COLUMNS,
    WALLET_EXPORT_TEXT_COLUMNS,
)

PERIOD = {"from_date": DAY, "to_date": DAY}
# `DAY` 所在的马来西亚月。
MONTH = "2026-10"

# §6 逐列对照：成本、汇率、参考价、毛利（AIH-TASK-222 守卫的禁用名单）与内部字段、内部 id。
INTERNAL_NAMES = FORBIDDEN | {
    "tenant_id",
    "customer_id",
    "wallet_id",
    "wallet_transaction_id",
    "pricing_rule_id",
    "provider_price_version_id",
    "fx_rate_version_id",
    "billing_mode_snapshot",
    "balance_before",
    "reference_type",
    "reference_id",
    "description",
    "metadata_json",
    "created_by",
}


class Export(NamedTuple):
    path: str
    params: dict[str, str]
    filename: str
    sheet: str
    columns: tuple[str, ...]
    text_columns: frozenset[str]
    integer_columns: frozenset[str]
    # 查询这一步的 repository 函数（`QueryTooBroad` 注入点）。
    repository: Any
    query: str
    # 占导出租约、有行数上限的三个大导出。
    leased: bool


EXPORTS = {
    "usage": Export(
        "/api/v1/customer/usage-events/export",
        PERIOD,
        f"usage-events_{DAY}_{DAY}.xlsx",
        "usage-events",
        USAGE_EXPORT_COLUMNS,
        USAGE_EXPORT_TEXT_COLUMNS,
        USAGE_EXPORT_INTEGER_COLUMNS,
        usage_repository,
        "list_processed_events_for_customer",
        True,
    ),
    "wallet": Export(
        "/api/v1/customer/wallet/transactions/export",
        PERIOD,
        f"wallet-transactions_{DAY}_{DAY}.xlsx",
        "wallet-transactions",
        WALLET_EXPORT_COLUMNS,
        WALLET_EXPORT_TEXT_COLUMNS,
        WALLET_EXPORT_INTEGER_COLUMNS,
        wallet_repository,
        "list_transactions_for_customer_export",
        True,
    ),
    "summary": Export(
        "/api/v1/customer/usage-summary/export",
        {"month": MONTH},
        f"usage-summary_{MONTH}.xlsx",
        "usage-summary",
        USAGE_SUMMARY_EXPORT_COLUMNS,
        USAGE_SUMMARY_EXPORT_TEXT_COLUMNS,
        USAGE_SUMMARY_EXPORT_INTEGER_COLUMNS,
        usage_repository,
        "summarize_processed_events_for_customer",
        False,
    ),
    "conversations": Export(
        "/api/v1/customer/conversations/export",
        PERIOD,
        f"conversations_{DAY}_{DAY}.xlsx",
        "conversations",
        CONVERSATION_EXPORT_COLUMNS,
        CONVERSATION_EXPORT_TEXT_COLUMNS,
        CONVERSATION_EXPORT_INTEGER_COLUMNS,
        usage_repository,
        "list_conversations_for_customer",
        True,
    ),
}
LEASED = [name for name, spec in EXPORTS.items() if spec.leased]


@pytest.fixture(autouse=True)
def lease_is_free() -> Iterator[None]:
    # 进程级租约：每条用例开始与结束时都必须空闲，用例之间不互相挡住。
    assert not export_lease_held()
    yield
    assert not export_lease_held()


@pytest.fixture
def portal(tmp_path) -> Iterator[UsageEventPortal]:
    yield from _usage_portal(_sqlite_factory(), tmp_path)


# --- 辅助 -------------------------------------------------------------------------------


def get(
    portal: UsageEventPortal,
    spec: Export,
    headers: dict[str, str],
    export_format: str | None = None,
    *,
    client: TestClient | None = None,
    **extra: str,
):
    params = {**spec.params, **extra}
    if export_format is not None:
        params["format"] = export_format
    return (client or portal.client).get(spec.path, params=params, headers=headers)


def seed(portal: UsageEventPortal) -> tuple[Tenancy, dict[str, str]]:
    """Three billed events (three conversations, two projects) and three ledger rows on `DAY`.

    用量、会话、钱包三个导出各有恰好 3 行；月度汇总的 10 月 1 日有 3 条。
    """
    tenant = portal.tenant()
    second = portal.project(tenant, 'Second, "quoted" project')
    portal.put(tenant, occurred_at=ON_DAY, conversation_id="conv-1", feature="chat")
    portal.put(
        tenant,
        project=second,
        occurred_at=ON_DAY + MINUTE,
        conversation_id="conv-2",
        # 15 位：Excel 能精确表示的最大位数，仍是数字单元格。
        tokens={
            "input_tokens": 123456789012345,
            "output_tokens": 7,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        },
        billable="0.125",
    )
    portal.put(
        tenant,
        occurred_at=ON_DAY + 2 * MINUTE,
        conversation_id="conv-3",
        request_id="=1+1",
    )
    post_at(portal, tenant, TransactionType.ADJUSTMENT_CREDIT, "100", at=ON_DAY)
    post_at(portal, tenant, TransactionType.AI_USAGE, "-1.5", at=ON_DAY + MINUTE)
    post_at(portal, tenant, TransactionType.ADJUSTMENT_DEBIT, "-2", at=ON_DAY + 2 * MINUTE)
    return tenant, portal.customer(tenant)


def sheet_names(body: bytes) -> list[str]:
    workbook = openpyxl.load_workbook(io.BytesIO(body), read_only=True)
    try:
        return list(workbook.sheetnames)
    finally:
        workbook.close()


def zip_members(body: bytes) -> set[str]:
    with zipfile.ZipFile(io.BytesIO(body)) as archive:
        return set(archive.namelist())


def zip_bytes(body: bytes) -> bytes:
    """Every member of the archive, uncompressed and concatenated."""
    with zipfile.ZipFile(io.BytesIO(body)) as archive:
        return b"".join(archive.read(name) for name in sorted(archive.namelist()))


def assert_same_table(xlsx_body: bytes, csv_body: bytes, spec: Export) -> list[tuple[Any, ...]]:
    """§1 完成标准 2：行数、行序与每个单元格都与 CSV 相同；返回 xlsx 读出的各行。"""
    values = read_values(xlsx_body, len(spec.columns))
    table = read_csv(csv_body)
    assert values[0] == spec.columns
    assert len(values) == len(table)
    for xlsx_row, csv_row in zip(values, table, strict=True):
        assert len(xlsx_row) == len(csv_row) == len(spec.columns)
        for xlsx_value, csv_field in zip(xlsx_row, csv_row, strict=True):
            assert_matches_csv(xlsx_value, csv_field)
    return values


def assert_json_error(response) -> None:
    """Errors are the JSON envelope, never a file."""
    assert response.headers["content-type"].startswith("application/json")
    assert "content-disposition" not in response.headers
    body = response.json()
    assert body["data"] is None
    assert body["request_id"]


def assert_same_error(csv_response, xlsx_response, expected: tuple[int, str]) -> None:
    """§7「错误一致」：两种格式同一状态码、同一错误码、同一消息。"""
    assert error_of(csv_response) == expected
    assert error_of(xlsx_response) == expected
    assert_json_error(csv_response)
    assert_json_error(xlsx_response)
    assert csv_response.json()["error"] == xlsx_response.json()["error"]


# --- 正常路径 ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", list(EXPORTS))
def test_csv_is_unchanged_and_xlsx_matches_it_cell_for_cell(
    portal: UsageEventPortal, name: str
) -> None:
    """§7「四个客户接口正常路径」。

    不带 `format` 与 `format=csv` 逐字节相同；xlsx 200、媒体类型、文件名、`no-store`、
    `X-Accel-Buffering: no`、工作表名、第一行等于列常量、与同参数 CSV 逐格一致、整数列是数字。
    """
    spec = EXPORTS[name]
    _, headers = seed(portal)

    plain = get(portal, spec, headers)
    as_csv = get(portal, spec, headers, "csv")
    as_xlsx = get(portal, spec, headers, "xlsx")

    assert plain.status_code == 200, plain.text
    assert plain.content.startswith(BOM)
    assert as_csv.status_code == 200
    assert as_csv.content == plain.content
    assert as_csv.headers["content-disposition"] == plain.headers["content-disposition"]
    assert plain.headers["content-disposition"].endswith('.csv"')

    assert as_xlsx.status_code == 200, as_xlsx.text
    assert as_xlsx.headers["content-type"] == XLSX_MEDIA_TYPE
    assert as_xlsx.headers["content-disposition"] == f'attachment; filename="{spec.filename}"'
    assert as_xlsx.headers["cache-control"] == "no-store"
    assert as_xlsx.headers["x-accel-buffering"] == "no"
    assert as_xlsx.headers["content-length"] == str(len(as_xlsx.content))
    assert as_xlsx.content.startswith(b"PK")
    assert sheet_names(as_xlsx.content) == [spec.sheet]

    values = assert_same_table(as_xlsx.content, plain.content, spec)
    data = values[1:]
    assert len(data) >= 3
    for row in data:
        for column, value in zip(spec.columns, row, strict=True):
            if column in spec.integer_columns:
                # 汇总的日行没有对话数，是空单元格；其余整数都读回 `int`。
                assert value is None or type(value) is int, (column, value)
            elif value is not None:
                assert isinstance(value, str), (column, value)
    integers = [
        value
        for row in data
        for column, value in zip(spec.columns, row, strict=True)
        if column in spec.integer_columns and value is not None
    ]
    assert integers, "整数列全空：没有验到数字单元格"


def test_a_15_digit_token_count_is_a_number_and_amounts_are_text(
    portal: UsageEventPortal,
) -> None:
    """§2「单元格类型」：`input_tokens` 123456789012345 读回 `int`；金额读回同一字符串。"""
    spec = EXPORTS["usage"]
    _, headers = seed(portal)

    values = read_values(get(portal, spec, headers, "xlsx").content, len(spec.columns))
    rows = [dict(zip(spec.columns, row, strict=True)) for row in values[1:]]

    assert 123456789012345 in [row["input_tokens"] for row in rows]
    assert sorted(row["charged_amount"] for row in rows) == [
        "0.06250000",
        "0.06250000",
        "0.12500000",
    ]
    for row in rows:
        assert type(row["input_tokens"]) is int
        assert isinstance(row["occurred_at_utc"], str)
    assert "'=1+1" in [row["request_id"] for row in rows]


def test_the_integer_column_constants_are_the_designed_ones() -> None:
    """§2「各导出的整数列」：恰好这些列，都在各自列常量里、与文本列不相交。"""
    assert USAGE_EXPORT_INTEGER_COLUMNS == frozenset(
        {"input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"}
    )
    assert USAGE_SUMMARY_EXPORT_INTEGER_COLUMNS == frozenset(
        {"request_count", "conversation_count"}
    )
    assert WALLET_EXPORT_INTEGER_COLUMNS == frozenset({"wallet_sequence"})
    assert CONVERSATION_EXPORT_INTEGER_COLUMNS == frozenset(
        {"request_count", "input_tokens", "output_tokens"}
    )
    for spec in EXPORTS.values():
        assert spec.integer_columns <= set(spec.columns)
        assert not spec.integer_columns & spec.text_columns


def test_openapi_lists_both_media_types_and_the_format_parameter() -> None:
    """§2「各接口的改法」：200 列出 `text/csv` 与 xlsx 媒体类型；`format` 只认 `csv` / `xlsx`。"""
    paths = create_app(Settings(database_url="", jwt_secret_file="")).openapi()["paths"]
    for spec in EXPORTS.values():
        operation = paths[spec.path]["get"]
        assert set(operation["responses"]["200"]["content"]) == {"text/csv", XLSX_MEDIA_TYPE}
        [parameter] = [item for item in operation["parameters"] if item["name"] == "format"]
        assert parameter["in"] == "query"
        assert not parameter.get("required", False)
        schema = parameter["schema"]
        assert schema["enum"] == ["csv", "xlsx"]
        assert schema["default"] == "csv"


# --- format 校验 ------------------------------------------------------------------------


@pytest.mark.parametrize("name", list(EXPORTS))
@pytest.mark.parametrize("value", ["XLSX", "xls", ""])
def test_an_unknown_format_is_422(portal: UsageEventPortal, name: str, value: str) -> None:
    """§7「format 校验」：`XLSX`、`xls`、空串 → 422 `VALIDATION_ERROR`、消息含 `query.format`。"""
    spec = EXPORTS[name]
    _, headers = seed(portal)

    response = get(portal, spec, headers, value)

    assert error_of(response) == (422, "VALIDATION_ERROR")
    assert_json_error(response)
    assert "query.format" in response.json()["error"]["message"]


@pytest.mark.parametrize("name", list(EXPORTS))
def test_an_unknown_format_is_422_even_without_a_token(portal: UsageEventPortal, name: str) -> None:
    """§2「参数 format」：与其他形状类参数同一时序，FastAPI 在处理函数（鉴权）之前判。"""
    spec = EXPORTS[name]

    response = get(portal, spec, {}, "xls")
    anonymous = get(portal, spec, {}, "xlsx")

    assert error_of(response) == (422, "VALIDATION_ERROR")
    assert "query.format" in response.json()["error"]["message"]
    assert error_of(anonymous) == (401, "TOKEN_INVALID")


# --- 错误一致 ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", LEASED)
def test_more_rows_than_the_cap_is_export_too_large_in_both_formats(
    portal: UsageEventPortal, monkeypatch, name: str
) -> None:
    """§7「错误一致」：上限 monkeypatch 成 2、3 行 → 两种格式都是 422 `EXPORT_TOO_LARGE`。"""
    spec = EXPORTS[name]
    _, headers = seed(portal)
    monkeypatch.setattr(customer_api, "EXPORT_MAX_ROWS", 2)

    as_csv = get(portal, spec, headers, "csv")
    as_xlsx = get(portal, spec, headers, "xlsx")

    assert_same_error(as_csv, as_xlsx, (422, "EXPORT_TOO_LARGE"))
    assert not as_xlsx.content.startswith(b"PK")


@pytest.mark.parametrize("name", list(EXPORTS))
def test_query_too_broad_is_the_same_422_in_both_formats(
    portal: UsageEventPortal, monkeypatch, name: str
) -> None:
    """§7「错误一致」：注入 `QueryTooBroad` → 两种格式都是 422 `QUERY_TOO_BROAD`。"""
    spec = EXPORTS[name]
    _, headers = seed(portal)

    def too_broad(*_args, **_kwargs):
        raise usage_repository.QueryTooBroad

    monkeypatch.setattr(spec.repository, spec.query, too_broad)
    with server_log() as lines:
        as_csv = get(portal, spec, headers, "csv")
        as_xlsx = get(portal, spec, headers, "xlsx")

    assert_same_error(as_csv, as_xlsx, (422, "QUERY_TOO_BROAD"))
    assert len(messages(lines, "customer_usage_query_timeout")) == 2
    assert not messages(lines, "customer_export_failed")


@pytest.mark.parametrize("name", LEASED)
def test_a_held_lease_is_export_busy_in_both_formats(portal: UsageEventPortal, name: str) -> None:
    """§7「错误一致」：预先占住租约 → 两种格式都是 503 `EXPORT_BUSY`、`Retry-After: 5`。"""
    spec = EXPORTS[name]
    _, headers = seed(portal)
    holder = try_acquire_export_lease()
    assert holder is not None
    try:
        as_csv = get(portal, spec, headers, "csv")
        as_xlsx = get(portal, spec, headers, "xlsx")
    finally:
        release_export_lease(holder)

    assert_same_error(as_csv, as_xlsx, (503, "EXPORT_BUSY"))
    assert as_csv.headers["retry-after"] == as_xlsx.headers["retry-after"] == "5"


def test_the_monthly_summary_needs_no_lease_in_either_format(portal: UsageEventPortal) -> None:
    """§7「错误一致」：月度汇总两种格式都不占租约，租约被占时照常 200。"""
    spec = EXPORTS["summary"]
    _, headers = seed(portal)
    holder = try_acquire_export_lease()
    assert holder is not None
    try:
        as_csv = get(portal, spec, headers, "csv")
        as_xlsx = get(portal, spec, headers, "xlsx")
        assert export_lease_held()
    finally:
        release_export_lease(holder)

    assert as_csv.status_code == 200
    assert as_xlsx.status_code == 200
    assert as_xlsx.headers["content-type"] == XLSX_MEDIA_TYPE
    assert_same_table(as_xlsx.content, as_csv.content, spec)


@pytest.mark.parametrize("name", list(EXPORTS))
def test_a_failing_xlsx_writer_is_a_500_and_frees_the_lease(
    portal: UsageEventPortal, monkeypatch, name: str
) -> None:
    """§7「错误一致」：`write_xlsx` 抛 `RuntimeError` → 500 `INTERNAL_ERROR`，请求结束后租约空闲。

    大导出经 `customer_export_failed` 在租约内发出；月度汇总由通用 500 处理。CSV 不经 `write_xlsx`，
    照常 200。
    """
    spec = EXPORTS[name]
    _, headers = seed(portal)
    client = TestClient(portal.app, raise_server_exceptions=False)

    def broken(*_args, **_kwargs):
        raise RuntimeError("fictional xlsx failure")

    monkeypatch.setattr(customer_api, "write_xlsx", broken)
    with server_log() as lines:
        failed = get(portal, spec, headers, "xlsx", client=client)
        held_after = export_lease_held()
        as_csv = get(portal, spec, headers, "csv", client=client)

    assert_generic_500(failed.status_code, failed.json())
    assert "fictional xlsx failure" not in failed.text
    assert not held_after
    if spec.leased:
        assert len(messages(lines, "customer_export_failed")) == 1
        assert not messages(lines, "Unhandled exception")
    else:
        assert not messages(lines, "customer_export_failed")
        assert messages(lines, "Unhandled exception")
    assert as_csv.status_code == 200


@pytest.mark.parametrize("export_format", ["csv", "xlsx"])
def test_a_missing_wallet_is_a_500_without_the_lease(
    portal: UsageEventPortal, monkeypatch, export_format: str
) -> None:
    """§7「错误一致」：钱包缺失 → 500 `INTERNAL_ERROR`，没有获取租约；两种格式相同。"""
    spec = EXPORTS["wallet"]
    tenant, headers = seed(portal)
    with portal.factory() as session:
        session.execute(delete(Wallet).where(Wallet.tenant_id == tenant.id))
        session.commit()
    acquired: list[object] = []

    def spy():
        acquired.append(True)
        return try_acquire_export_lease()

    monkeypatch.setattr(customer_api, "try_acquire_export_lease", spy)
    client = TestClient(portal.app, raise_server_exceptions=False)

    with server_log() as lines:
        response = get(portal, spec, headers, export_format, client=client)

    assert_generic_500(response.status_code, response.json())
    assert acquired == []
    assert not messages(lines, "customer_export_failed")


def test_a_missing_month_is_the_same_422_in_both_formats(portal: UsageEventPortal) -> None:
    """§7「错误一致」：月度汇总不带 `month` → 两种格式都是 422 `query.month`。"""
    _, headers = seed(portal)
    path = EXPORTS["summary"].path

    as_csv = portal.client.get(path, params={"format": "csv"}, headers=headers)
    as_xlsx = portal.client.get(path, params={"format": "xlsx"}, headers=headers)

    assert_same_error(as_csv, as_xlsx, (422, "VALIDATION_ERROR"))
    assert as_xlsx.json()["error"]["message"] == "Invalid request fields: query.month"


# --- 跨租户 -----------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["usage", "conversations"])
def test_another_customers_project_is_the_header_only(portal: UsageEventPortal, name: str) -> None:
    """§7「跨租户」（INV-8）：租户 A 的令牌带租户 B 的 `project_id` → 200，只有表头一行。"""
    spec = EXPORTS[name]
    _, headers = seed(portal)
    theirs, _ = seed(portal)
    their_project = portal.main_project(theirs).public_id

    response = get(portal, spec, headers, "xlsx", project_id=their_project)
    unknown = get(portal, spec, headers, "xlsx", project_id=ZERO_UUID)

    for result in (response, unknown):
        assert result.status_code == 200, result.text
        assert read_values(result.content, len(spec.columns)) == [spec.columns]
    assert their_project.encode() not in zip_bytes(response.content)


# --- INV-7 列与成员 ---------------------------------------------------------------------


@pytest.mark.parametrize("name", list(EXPORTS))
def test_the_xlsx_carries_only_the_whitelisted_columns(portal: UsageEventPortal, name: str) -> None:
    """§7「INV-7 列与成员」：表头恰好等于列常量、与禁用名单与内部字段无交集。

    恰好一个工作表、ZIP 成员集合固定；文件任何成员里都没有客户自己的 id。
    """
    spec = EXPORTS[name]
    tenant, headers = seed(portal)

    response = get(portal, spec, headers, "xlsx")

    assert response.status_code == 200
    header = read_values(response.content, len(spec.columns) + 1)[0]
    # 多读一列：表头之后没有多出来的列。
    assert header == (*spec.columns, None)
    assert not set(spec.columns) & INTERNAL_NAMES
    assert sheet_names(response.content) == [spec.sheet]
    assert zip_members(response.content) == ZIP_MEMBERS
    assert tenant.public_id.encode() not in zip_bytes(response.content)


# --- 恶意与异常文本 ---------------------------------------------------------------------


@pytest.mark.parametrize("name", ["usage", "conversations"])
def test_hostile_project_names(portal: UsageEventPortal, name: str) -> None:
    """§7「恶意与异常文本」：公式前加单引号、`#N/A` 读回字符串、U+0001 在 xlsx 里为 U+FFFD。

    CSV 不受影响：同样的前缀，控制字符原样。
    """
    spec = EXPORTS[name]
    tenant = portal.tenant()
    names = {
        '=HYPERLINK("x","y")': ('\'=HYPERLINK("x","y")', '\'=HYPERLINK("x","y")'),
        "#N/A": ("#N/A", "#N/A"),
        "a\x01b": ("a�b", "a\x01b"),
    }
    expected_xlsx: dict[str, str] = {}
    expected_csv: dict[str, str] = {}
    for number, (project_name, (in_xlsx, in_csv)) in enumerate(names.items()):
        project = portal.project(tenant, project_name)
        portal.put(
            tenant,
            project=project,
            occurred_at=ON_DAY + number * MINUTE,
            conversation_id=f"conv-{number}",
        )
        expected_xlsx[project.public_id] = in_xlsx
        expected_csv[project.public_id] = in_csv
    headers = portal.customer(tenant)

    as_xlsx = get(portal, spec, headers, "xlsx")
    as_csv = get(portal, spec, headers, "csv")

    assert as_xlsx.status_code == as_csv.status_code == 200
    project_at = spec.columns.index("project_id")
    name_at = spec.columns.index("project_name")
    xlsx_rows = read_values(as_xlsx.content, len(spec.columns))[1:]
    csv_rows = read_csv(as_csv.content)[1:]
    assert {row[project_at]: row[name_at] for row in xlsx_rows} == expected_xlsx
    assert {row[project_at]: row[name_at] for row in csv_rows} == expected_csv
    for row in xlsx_rows:
        assert isinstance(row[name_at], str)
    with zipfile.ZipFile(io.BytesIO(as_xlsx.content)) as archive:
        sheet = archive.read("xl/worksheets/sheet1.xml")
    assert b't="e"' not in sheet
    assert b"<f>" not in sheet

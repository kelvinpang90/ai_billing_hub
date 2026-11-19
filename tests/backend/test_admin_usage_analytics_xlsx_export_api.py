"""Admin usage analytics export with `format=xlsx` (AIH-TASK-237, design gate #753 v1 §7).

`GET /api/v1/admin/usage-analytics/export` 加了查询参数 `format`
（缺省 `csv`，只认 `csv` / `xlsx`，在 `require_admin` 与 `parse_analytics_query` 之后判）。
这里是 §7 标 AIH-TASK-237 的三行：管理端 xlsx、管理端校验与鉴权、管理端错误一致。
CSV 本身的行在 test_admin_usage_analytics_export_api.py，
xlsx 写出模块本身的行在 test_xlsx_export.py，这里不重复。

⚠️ **只跑 SQLite**（设计 §7 末段）：查询代码路径与 CSV 是同一段、
已由 AIH-TASK-223 导出测试的 MySQL 一半覆盖；xlsx 只改会话关闭之后的编码。

事件由真实计费 worker 记账（test_usage_billing.World），
支付行由本文件的 `paid` 夹具直接写入；其余用例的事件照 AIH-TASK-223 的写法直接插入。
这里只从现有测试模块导入普通函数与常量，不转引用夹具。

⚠️ 导出租约是进程级的：每条用例开始与结束时都必须空闲（`lease_is_free`）。
⚠️ SQL 文本不做断言；示例 uuid 用全零占位值；时间一律注入。
"""

from __future__ import annotations

import io
import itertools
import logging
import zipfile
from collections.abc import Iterator
from decimal import Decimal
from typing import Any

import openpyxl
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, delete
from sqlalchemy.pool import StaticPool
from test_admin_usage_analytics_export_api import (
    BOM,
    COLUMNS,
    DAY_QUERY,
    EXPORT,
    PAID_AT,
    T0,
    TEXT_COLUMNS,
    WINDOW,
    _sqlite_static_factory,
    customers_with_events,
    error_of,
    extras_of,
    read_csv,
    records,
    settings_for,
    sign_in,
)
from test_usage_billing import TEST_EMAIL_DOMAIN, Tenancy
from test_usage_billing import World as BillingWorld
from test_xlsx_export import ZIP_MEMBERS, assert_matches_csv, read_values

from app.api import admin_usage_analytics as admin_api
from app.core.config import Settings
from app.core.csv_export import (
    export_lease_held,
    release_export_lease,
    try_acquire_export_lease,
)
from app.core.database import create_session_factory
from app.core.xlsx_export import XLSX_MEDIA_TYPE
from app.main import create_app
from app.models.auth import UserRole
from app.models.base import Base
from app.models.payments import Payment, PaymentGateway, PaymentStatus
from app.models.tax_policy import TaxJurisdiction, TaxPolicyStatus, TaxPolicyType, TaxPolicyVersion
from app.repositories import usage_analytics as analytics_repository
from app.schemas.usage_analytics import (
    USAGE_ANALYTICS_EXPORT_COLUMNS,
    USAGE_ANALYTICS_EXPORT_INTEGER_COLUMNS,
    USAGE_ANALYTICS_EXPORT_TEXT_COLUMNS,
)

SHEET = "usage-analytics"
# 设计 §2「各导出的整数列」管理端一行：恰好这 10 列。
INTEGER_COLUMNS = frozenset(
    {
        "event_count",
        "token_event_count",
        "input_tokens",
        "output_tokens",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
        "prepaid_event_count",
        "internal_event_count",
        "paid_payment_count",
        "gateway_fee_unknown_count",
    }
)
# GROUP 行里恒为空的两个整数列（手续费只在 TOTAL 行）。
FEE_INTEGER_COLUMNS = frozenset({"paid_payment_count", "gateway_fee_unknown_count"})
# 金额、比率与平均值：xlsx 里是文本，与 CSV 逐字相同。
DECIMAL_COLUMNS = (
    "average_input_tokens",
    "average_output_tokens",
    "prepaid_billable_cost",
    "prepaid_estimated_provider_cost_myr",
    "prepaid_gross_margin",
    "prepaid_gross_margin_rate",
    "internal_estimated_provider_cost_myr",
    "internal_reference_customer_price",
    "topup_amount",
    "gateway_fee",
    "gross_profit",
    "gross_profit_rate",
)
FILENAME_STEM = "usage-analytics_customer_20260929T170000MYT_20260929T180000MYT"

_NUMBERS = itertools.count(1)


@pytest.fixture(autouse=True)
def lease_is_free() -> Iterator[None]:
    # 进程级租约：每条用例开始与结束时都必须空闲，用例之间不互相挡住。
    assert not export_lease_held()
    yield
    assert not export_lease_held()


@pytest.fixture
def app(tmp_path):
    application = create_app(settings_for(tmp_path))
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    application.state.engine, application.state.session_factory = (
        engine,
        create_session_factory(engine),
    )
    yield application
    engine.dispose()


@pytest.fixture
def billed(tmp_path):
    """Events priced and posted by the real billing worker on SQLite, read by the export app."""
    factories = _sqlite_static_factory()
    factory = next(factories)
    try:
        application = create_app(settings_for(tmp_path))
        application.state.engine = factory.kw["bind"]
        application.state.session_factory = factory
        yield application, BillingWorld(factory)
    finally:
        for _ in factories:
            pass


@pytest.fixture
def paid(billed):
    """Writes `PAID` MYR payments into the billed world; removes them before `billed` tears down.

    同 AIH-TASK-222、223 的写法：先删本用例写的支付行，再清空税务政策版本
    （MySQL 上关掉外键检查后 TRUNCATE；本文件只跑 SQLite，那里直接删）。
    """
    _app, world = billed
    written: list[str] = []
    versions: list[int] = []

    def pay(tenant: Tenancy, fee: str | None, *, amount: str = "100.00") -> None:
        with world.factory() as session:
            if not versions:
                version = TaxPolicyVersion(
                    public_id=f"00000000-0000-4009-8000-{next(_NUMBERS):012d}",
                    jurisdiction=TaxJurisdiction.MY,
                    policy_type=TaxPolicyType.SST,
                    rate=None,
                    treatment_json={
                        "schema_version": 1,
                        "sst_registered": False,
                        "tax_charged": False,
                    },
                    status=TaxPolicyStatus.DRAFT,
                    effective_from=None,
                    effective_to=None,
                    source_reference="Fictional policy for a margin xlsx export test",
                    created_by=world.admin.id,
                    approved_by=None,
                    approved_at=None,
                    created_at=T0,
                    updated_at=T0,
                )
                session.add(version)
                session.flush()
                versions.append(int(version.id))
            public_id = f"00000000-0000-4008-8000-{next(_NUMBERS):012d}"
            session.add(
                Payment(
                    public_id=public_id,
                    tenant_id=tenant.id,
                    gateway=PaymentGateway.STRIPE,
                    expected_amount=Decimal(amount),
                    expected_currency="MYR",
                    wallet_credit_amount=Decimal(amount),
                    tax_policy_version_id=versions[0],
                    tax_amount=None,
                    confirmed_amount=Decimal(amount),
                    confirmed_currency="MYR",
                    gateway_fee=None if fee is None else Decimal(fee),
                    status=PaymentStatus.PAID,
                    expires_at=PAID_AT,
                    paid_at=PAID_AT,
                    created_at=PAID_AT,
                    updated_at=PAID_AT,
                )
            )
            session.commit()
            written.append(public_id)

    try:
        yield pay
    finally:
        with world.factory.begin() as session:
            if written:
                session.execute(delete(Payment).where(Payment.public_id.in_(written)))
        engine = world.factory.kw["bind"]
        if engine.dialect.name == "mysql":
            with engine.begin() as connection:
                connection.exec_driver_sql("SET FOREIGN_KEY_CHECKS = 0")
                try:
                    connection.exec_driver_sql("TRUNCATE TABLE tax_policy_versions")
                finally:
                    connection.exec_driver_sql("SET FOREIGN_KEY_CHECKS = 1")
        elif versions:
            with world.factory.begin() as session:
                session.execute(delete(TaxPolicyVersion).where(TaxPolicyVersion.id.in_(versions)))


# --- 辅助 -------------------------------------------------------------------------------


def export(app, headers: dict[str, str], export_format: str | None = None, **params):
    if export_format is not None:
        params["format"] = export_format
    return TestClient(app).get(EXPORT, params=params, headers=headers)


def sheet_names(body: bytes) -> list[str]:
    workbook = openpyxl.load_workbook(io.BytesIO(body), read_only=True)
    try:
        return list(workbook.sheetnames)
    finally:
        workbook.close()


def zip_members(body: bytes) -> set[str]:
    with zipfile.ZipFile(io.BytesIO(body)) as archive:
        return set(archive.namelist())


def assert_json_error(response) -> None:
    """Errors are the JSON envelope, never a file."""
    assert response.headers["content-type"].startswith("application/json")
    assert "content-disposition" not in response.headers
    body = response.json()
    assert body["data"] is None
    assert body["request_id"]


def assert_same_error(csv_response, xlsx_response, expected: tuple[int, str]) -> None:
    """§7「管理端错误一致」：两种格式同一状态码、同一错误码、同一消息。"""
    assert error_of(csv_response) == expected
    assert error_of(xlsx_response) == expected
    assert_json_error(csv_response)
    assert_json_error(xlsx_response)
    assert csv_response.json()["error"] == xlsx_response.json()["error"]


# --- 管理端 xlsx ------------------------------------------------------------------------


def test_csv_is_unchanged_and_xlsx_matches_it_cell_for_cell(billed, paid) -> None:
    """§7「管理端 xlsx」：`group_by=customer` 两个客户加手续费。

    不带 `format` 与 `format=csv` 逐字节相同；xlsx 31 列逐格等于 CSV；
    10 个整数列是数字；金额、比率、平均值是文本；GROUP 行在前、TOTAL 行恒在末尾；
    文件名 `.xlsx`、工作表名 `usage-analytics`。
    """
    app, world = billed
    world.publish_markup_usd()
    first, second = world.tenant("first"), world.tenant("second")
    for tenant in (first, second):
        world.fund(tenant, "100")
    world.event(first)
    world.event(first)
    world.event(second)
    assert world.bill().claimed == 3
    paid(first, "1.50")
    paid(second, None, amount="50.00")
    admin, _ = sign_in(app, domain=TEST_EMAIL_DOMAIN)
    params = {"group_by": "customer", **WINDOW}

    plain = export(app, admin, **params)
    as_csv = export(app, admin, "csv", **params)
    as_xlsx = export(app, admin, "xlsx", **params)

    assert plain.status_code == 200, plain.text
    assert plain.content.startswith(BOM)
    assert as_csv.status_code == 200, as_csv.text
    assert as_csv.content == plain.content
    assert as_csv.headers["content-type"] == plain.headers["content-type"]
    assert as_csv.headers["content-disposition"] == plain.headers["content-disposition"]
    assert plain.headers["content-disposition"] == f'attachment; filename="{FILENAME_STEM}.csv"'

    assert as_xlsx.status_code == 200, as_xlsx.text
    assert as_xlsx.headers["content-type"] == XLSX_MEDIA_TYPE
    assert as_xlsx.headers["content-disposition"] == (
        f'attachment; filename="{FILENAME_STEM}.xlsx"'
    )
    assert as_xlsx.headers["cache-control"] == "no-store"
    assert as_xlsx.headers["x-accel-buffering"] == "no"
    assert as_xlsx.content.startswith(b"PK")
    assert sheet_names(as_xlsx.content) == [SHEET]
    assert zip_members(as_xlsx.content) == ZIP_MEMBERS

    values = read_values(as_xlsx.content, len(COLUMNS))
    table = read_csv(plain.content)
    assert values[0] == tuple(COLUMNS)
    assert len(values) == len(table) == 4
    for xlsx_row, csv_row in zip(values, table, strict=True):
        assert len(xlsx_row) == len(csv_row) == 31
        for xlsx_value, csv_field in zip(xlsx_row, csv_row, strict=True):
            assert_matches_csv(xlsx_value, csv_field)

    rows = [dict(zip(COLUMNS, row, strict=True)) for row in values[1:]]
    assert [row["row_type"] for row in rows] == ["GROUP", "GROUP", "TOTAL"]
    assert [row["group_key"] for row in rows[:2]] == [first.public_id, second.public_id]
    for row in rows:
        for name, value in row.items():
            if name in INTEGER_COLUMNS:
                if row["row_type"] == "GROUP" and name in FEE_INTEGER_COLUMNS:
                    assert value is None, name
                else:
                    assert type(value) is int, (name, value)
            elif value is not None:
                assert isinstance(value, str), (name, value)
    total = rows[-1]
    assert (total["event_count"], total["paid_payment_count"]) == (3, 2)
    assert total["gateway_fee_unknown_count"] == 1
    for name in DECIMAL_COLUMNS:
        assert isinstance(total[name], str), name
    assert (total["topup_amount"], total["gateway_fee"]) == ("150.00000000", "1.50000000")
    profit = Decimal(total["prepaid_gross_margin"]) - Decimal("1.5")
    assert total["gross_profit"] == f"{profit:.8f}"


def test_the_integer_column_constant_is_the_designed_one() -> None:
    """§2「各导出的整数列」管理端一行：恰好 10 列，都在 31 列里、与文本列不相交。"""
    assert USAGE_ANALYTICS_EXPORT_INTEGER_COLUMNS == INTEGER_COLUMNS
    assert USAGE_ANALYTICS_EXPORT_INTEGER_COLUMNS <= set(USAGE_ANALYTICS_EXPORT_COLUMNS)
    assert not USAGE_ANALYTICS_EXPORT_INTEGER_COLUMNS & USAGE_ANALYTICS_EXPORT_TEXT_COLUMNS
    assert set(USAGE_ANALYTICS_EXPORT_TEXT_COLUMNS) == TEXT_COLUMNS


def test_openapi_lists_both_media_types_and_the_format_parameter() -> None:
    """§2「各接口的改法」：200 列出 `text/csv` 与 xlsx 媒体类型；`format` 可选。"""
    paths = create_app(Settings(database_url="", jwt_secret_file="")).openapi()["paths"]
    operation = paths[EXPORT]["get"]
    assert set(operation["responses"]["200"]["content"]) == {"text/csv", XLSX_MEDIA_TYPE}
    [parameter] = [item for item in operation["parameters"] if item["name"] == "format"]
    assert parameter["in"] == "query"
    assert not parameter.get("required", False)


# --- 管理端校验与鉴权 -------------------------------------------------------------------


@pytest.mark.parametrize("value", ["XLSX", "xls", ""])
def test_an_unknown_format_is_422_without_the_lease_or_a_query(app, monkeypatch, value) -> None:
    """§7「管理端校验与鉴权」：非法 `format` → 422 `query.format`；不拿租约、不查询。"""
    customers_with_events(app, 1)
    admin, _ = sign_in(app)
    acquired: list[object] = []
    queried: list[object] = []

    def acquire():
        acquired.append(True)
        return try_acquire_export_lease()

    def spy(*_args, **kwargs):
        queried.append(kwargs)
        raise AssertionError("no query for an invalid format")

    monkeypatch.setattr(admin_api, "try_acquire_export_lease", acquire)
    monkeypatch.setattr(analytics_repository, "list_usage_analytics", spy)
    response = export(app, admin, value, group_by="customer", **DAY_QUERY)

    assert error_of(response) == (422, "VALIDATION_ERROR")
    assert_json_error(response)
    assert "query.format" in response.json()["error"]["message"]
    assert acquired == []
    assert queried == []


def test_format_is_judged_after_the_other_parameters(app) -> None:
    """§2「参数 format」：在 `parse_analytics_query` 之后判，其他参数先报。"""
    admin, _ = sign_in(app)
    response = export(app, admin, "xls", group_by="week", **DAY_QUERY)

    assert error_of(response) == (422, "VALIDATION_ERROR")
    message = response.json()["error"]["message"]
    assert "query.group_by" in message
    assert "query.format" not in message


def test_anonymous_is_401_and_a_customer_is_403_before_the_format_check(app) -> None:
    """§7「管理端校验与鉴权」：匿名与客户令牌带 `format=xls` 分别先得 401 / 403。"""
    customer, _ = sign_in(app, UserRole.CUSTOMER)

    anonymous = export(app, {}, "xls", group_by="customer", **DAY_QUERY)
    refused = export(app, customer, "xls", group_by="customer", **DAY_QUERY)

    assert error_of(anonymous) == (401, "TOKEN_INVALID")
    assert error_of(refused) == (403, "ADMIN_REQUIRED")
    assert_json_error(refused)


# --- 管理端错误一致 ---------------------------------------------------------------------


def test_more_groups_than_the_cap_is_export_too_large_in_both_formats(app, monkeypatch) -> None:
    """§7「管理端错误一致」：上限设为 2、3 个分组 → 两种格式都是 422 `EXPORT_TOO_LARGE`。"""
    monkeypatch.setattr(admin_api, "EXPORT_MAX_ROWS", 2)
    customers_with_events(app, 3)
    admin, _ = sign_in(app)

    as_csv = export(app, admin, "csv", group_by="customer", **DAY_QUERY)
    as_xlsx = export(app, admin, "xlsx", group_by="customer", **DAY_QUERY)

    assert_same_error(as_csv, as_xlsx, (422, "EXPORT_TOO_LARGE"))
    assert not as_xlsx.content.startswith(b"PK")


def test_a_held_lease_is_export_busy_in_both_formats(app) -> None:
    """§7「管理端错误一致」：租约被占 → 两种格式都是 503 `EXPORT_BUSY`、`Retry-After: 5`。"""
    customers_with_events(app, 1)
    admin, _ = sign_in(app)
    holder = try_acquire_export_lease()
    assert holder is not None
    try:
        as_csv = export(app, admin, "csv", group_by="customer", **DAY_QUERY)
        as_xlsx = export(app, admin, "xlsx", group_by="customer", **DAY_QUERY)
    finally:
        release_export_lease(holder)

    assert_same_error(as_csv, as_xlsx, (503, "EXPORT_BUSY"))
    assert as_csv.headers["retry-after"] == as_xlsx.headers["retry-after"] == "5"


@pytest.mark.parametrize(
    ("export_format", "writer"), [("csv", "write_csv"), ("xlsx", "write_xlsx")]
)
def test_a_failing_writer_is_a_500_and_frees_the_lease(
    app, monkeypatch, caplog, export_format: str, writer: str
) -> None:
    """§7「管理端错误一致」：写出函数抛错 → 500 `INTERNAL_ERROR`，请求结束后租约空闲。

    两种格式相同；失败日志仍是 `admin_usage_analytics_export_failed`（只带路径），没有成功日志。
    """
    customers_with_events(app, 1)
    admin, _ = sign_in(app)

    def broken(*_args, **_kwargs):
        raise RuntimeError("fictional writer failure")

    caplog.set_level(logging.INFO, logger=admin_api.__name__)
    with monkeypatch.context() as patch:
        patch.setattr(admin_api, writer, broken)
        response = export(app, admin, export_format, group_by="customer", **DAY_QUERY)
        held_after = export_lease_held()

    assert not held_after
    assert response.status_code == 500
    body = response.json()
    assert body["error"] == {"code": "INTERNAL_ERROR", "message": "An internal error occurred."}
    assert body["data"] is None
    assert body["request_id"]
    assert "fictional writer failure" not in response.text
    assert "content-disposition" not in response.headers
    failures = records(caplog, "admin_usage_analytics_export_failed")
    assert len(failures) == 1
    assert failures[0].levelno == logging.ERROR
    assert failures[0].exc_info is not None
    assert extras_of(failures[0]) == {"path": EXPORT}
    assert records(caplog, "admin_usage_analytics_exported") == []


def test_a_failing_xlsx_writer_leaves_the_csv_export_working(app, monkeypatch) -> None:
    """CSV 不经 `write_xlsx`：它坏了时 CSV 照常 200。"""
    customers_with_events(app, 1)
    admin, _ = sign_in(app)

    def broken(*_args, **_kwargs):
        raise RuntimeError("fictional xlsx failure")

    monkeypatch.setattr(admin_api, "write_xlsx", broken)
    response = export(app, admin, "csv", group_by="customer", **DAY_QUERY)

    assert response.status_code == 200, response.text
    assert read_csv(response.content)[-1][0] == "TOTAL"


@pytest.mark.parametrize("export_format", [None, "csv", "xlsx"])
def test_a_successful_export_logs_exactly_three_keys(
    app, caplog, export_format: str | None
) -> None:
    """§2「各接口的改法」：成功日志 `extra` 恰好三个键，不加格式。"""
    customers_with_events(app, 2)
    admin, admin_id = sign_in(app)
    caplog.set_level(logging.INFO, logger=admin_api.__name__)

    response = export(app, admin, export_format, group_by="customer", **DAY_QUERY)

    assert response.status_code == 200, response.text
    lines = records(caplog, "admin_usage_analytics_exported")
    assert len(lines) == 1
    assert lines[0].levelno == logging.INFO
    extras: dict[str, Any] = extras_of(lines[0])
    assert extras == {"user_id": admin_id, "group_by": "customer", "row_count": 2}
    assert records(caplog, "admin_usage_analytics_export_failed") == []

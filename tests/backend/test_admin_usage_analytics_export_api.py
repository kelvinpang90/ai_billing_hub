"""Admin usage analytics CSV export end to end (AIH-TASK-223, design gate #709 v2 §2「接口二」, §7).

`GET /api/v1/admin/usage-analytics/export`：参数与接口一相同、没有分页；每个分组一行 GROUP（顺序同
接口一），末尾恒有一行 TOTAL；31 列；复用客户导出的 CSV 模块、租约与 `ExportCsvResponse`。

- 正常路径在 SQLite 与 MySQL 上各跑一遍：事件由真实计费 worker 记账（test_usage_billing.World），
  支付行由本文件的 `paid` 夹具直接写入。MySQL 那一半需要 `BILLING_TEST_DATABASE_URL`，没设时
  skip —— **不要把 skipped 读成 passed**，CI 里不许 skip。
- 其余用例用 SQLite 内存库，事件直接插入。

⚠️ 导出租约是进程级的：每条用例开始与结束时都必须空闲（`lease_is_free`）。
⚠️ SQL 文本不做断言；示例 uuid 用全零占位值；时间一律注入。
"""

from __future__ import annotations

import base64
import csv
import datetime as dt
import io
import itertools
import logging
import os
from collections.abc import Iterator
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, delete
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool
from test_usage_billing import (
    _SQLITE_LEDGER_TRIGGER,
    EVENT_AT,
    TEST_EMAIL_DOMAIN,
    Tenancy,
    _mysql_factory,
    seed_meter_types,
)
from test_usage_billing import World as BillingWorld

from app.api import admin_usage_analytics as admin_api
from app.api import auth as auth_api
from app.core.config import Settings
from app.core.csv_export import (
    export_lease_held,
    release_export_lease,
    try_acquire_export_lease,
)
from app.core.database import create_session_factory
from app.core.tokens import issue_access_token
from app.main import create_app
from app.models.ai_catalog import AiModel, AiProvider, PayloadShape, QuantityKind, UsageMeterType
from app.models.auth import User, UserRole, UserStatus
from app.models.base import Base
from app.models.payments import Payment, PaymentGateway, PaymentStatus
from app.models.tax_policy import TaxJurisdiction, TaxPolicyStatus, TaxPolicyType, TaxPolicyVersion
from app.models.tenancy import BillingMode, Project, Tenant
from app.models.usage import UsageEvent, UsageEventStatus
from app.repositories import usage_analytics as analytics_repository
from app.schemas.usage_analytics import (
    USAGE_ANALYTICS_EXPORT_COLUMNS,
    USAGE_ANALYTICS_EXPORT_TEXT_COLUMNS,
    UsageAnalyticsInternalMeteredOnly,
    UsageAnalyticsItem,
    UsageAnalyticsPayments,
    UsageAnalyticsPrepaid,
    UsageAnalyticsTotalsView,
    rfc3339_utc,
    usage_analytics_export_group_cells,
    usage_analytics_export_total_cells,
)
from app.services.auth import utc_now

PATH = "/api/v1/admin/usage-analytics"
EXPORT = PATH + "/export"
BOM = b"\xef\xbb\xbf"
T0 = dt.datetime(2026, 1, 1)
DAY_QUERY = {"occurred_from": "2026-01-01T00:00:00Z", "occurred_to": "2026-01-02T00:00:00Z"}

# 设计 §2「接口二」：表头恰好这 31 列，按这个顺序。多一列、少一列、换顺序都红。
COLUMNS = [
    "row_type",
    "group_by",
    "group_key",
    "group_name",
    "event_count",
    "token_event_count",
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
    "average_input_tokens",
    "average_output_tokens",
    "prepaid_event_count",
    "prepaid_billable_cost",
    "prepaid_estimated_provider_cost_myr",
    "prepaid_gross_margin",
    "prepaid_gross_margin_rate",
    "internal_event_count",
    "internal_estimated_provider_cost_myr",
    "internal_reference_customer_price",
    "paid_payment_count",
    "topup_amount",
    "gateway_fee",
    "gateway_fee_unknown_count",
    "gross_profit",
    "gross_profit_rate",
    "cost_basis",
    "usage_period_basis",
    "fee_period_basis",
    "occurred_from",
    "occurred_to",
]
TEXT_COLUMNS = {"group_key", "group_name"}
# TOTAL 行在手续费不可归属时为空的列：第 21–26 与第 29 列。
FEE_COLUMNS = [
    "paid_payment_count",
    "topup_amount",
    "gateway_fee",
    "gateway_fee_unknown_count",
    "gross_profit",
    "gross_profit_rate",
    "fee_period_basis",
]
USAGE_KEYS = (
    "event_count",
    "token_event_count",
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
    "average_input_tokens",
    "average_output_tokens",
)

_NUMBERS = itertools.count(1)


@pytest.fixture(autouse=True)
def lease_is_free() -> Iterator[None]:
    # 进程级租约：每条用例开始与结束时都必须空闲，用例之间不互相挡住。
    assert not export_lease_held()
    yield
    assert not export_lease_held()


# --- 辅助 -------------------------------------------------------------------------------


def settings_for(tmp_path) -> Settings:
    jwt, master = tmp_path / "jwt.key", tmp_path / "master.key"
    jwt.write_text("a-test-signing-key-that-is-long-enough", encoding="utf-8")
    master.write_text(f"1:{base64.b64encode(os.urandom(32)).decode()}\n", encoding="utf-8")
    return Settings(jwt_secret_file=str(jwt), master_key_file=str(master), database_url="")


def sign_in(
    app, role: UserRole = UserRole.ADMIN, domain: str = "@analytics-export-test.example.com"
) -> tuple[dict[str, str], int]:
    """A new active user of `role`; its bearer headers and internal id."""
    with app.state.session_factory() as session:
        user = User(
            # 不与 test_usage_billing.World 的 `admin-<n>` 撞名（共用 MySQL 时邮箱唯一）。
            email=f"{role.value.lower()}-export-{next(_NUMBERS)}{domain}",
            password_hash="not-a-real-hash",
            role=role,
            status=UserStatus.ACTIVE,
            created_at=T0,
            updated_at=T0,
        )
        session.add(user)
        session.commit()
        token = issue_access_token(
            app.state.settings, user_id=user.id, role=role.value, session_id="0" * 32, now=utc_now()
        )
        return {"Authorization": f"Bearer {token}"}, int(user.id)


def read_csv(body: bytes) -> list[list[str]]:
    assert body.startswith(BOM)
    return list(csv.reader(io.StringIO(body[len(BOM) :].decode("utf-8"), newline="")))


def by_name(row: list[str]) -> dict[str, str]:
    return dict(zip(COLUMNS, row, strict=True))


def text(value: Any) -> str:
    return "" if value is None else str(value)


def usage_values(view: dict[str, Any]) -> dict[str, Any]:
    """Columns 5–20 from a JSON group item or `totals`."""
    return {
        **{key: view[key] for key in USAGE_KEYS},
        **{f"prepaid_{key}": value for key, value in view["prepaid"].items()},
        **{f"internal_{key}": value for key, value in view["internal_metered_only"].items()},
    }


def expected_group(item: dict[str, Any], group_by: str, start: str, end: str) -> list[str]:
    """What one interface-one item becomes (for values that need no formula prefix)."""
    values = {
        "row_type": "GROUP",
        "group_by": group_by,
        "group_key": item["group_key"],
        "group_name": item["group_name"],
        **usage_values(item),
        "cost_basis": "estimated",
        "usage_period_basis": "occurred_at",
        "occurred_from": start,
        "occurred_to": end,
    }
    return [text(values.get(name)) for name in COLUMNS]


def expected_total(totals: dict[str, Any], group_by: str, start: str, end: str) -> list[str]:
    """What interface one's `totals` becomes, column for column."""
    payments = totals["payments"] or {}
    values = {
        "row_type": "TOTAL",
        "group_by": group_by,
        **usage_values(totals),
        "paid_payment_count": payments.get("paid_count"),
        "topup_amount": payments.get("topup_amount"),
        "gateway_fee": payments.get("gateway_fee"),
        "gateway_fee_unknown_count": payments.get("gateway_fee_unknown_count"),
        "gross_profit": totals["gross_profit"],
        "gross_profit_rate": totals["gross_profit_rate"],
        "cost_basis": "estimated",
        "usage_period_basis": "occurred_at",
        "fee_period_basis": payments.get("period_basis"),
        "occurred_from": start,
        "occurred_to": end,
    }
    return [text(values.get(name)) for name in COLUMNS]


def export(app, headers: dict[str, str], **params):
    return TestClient(app).get(EXPORT, params=params, headers=headers)


def exported(app, headers: dict[str, str], **params) -> list[list[str]]:
    """The data rows (GROUP rows then TOTAL) of a 200 export; the header row is checked here."""
    response = export(app, headers, **params)
    assert response.status_code == 200, response.text
    table = read_csv(response.content)
    assert table[0] == COLUMNS
    assert table[-1][0] == "TOTAL"
    return table[1:]


def error_of(response) -> tuple[int, str]:
    return response.status_code, response.json()["error"]["code"]


def extras_of(record: logging.LogRecord) -> dict[str, Any]:
    baseline = set(logging.LogRecord("x", logging.ERROR, "x", 0, "x", None, None).__dict__)
    baseline |= {"message", "asctime", "taskName"}
    return {key: value for key, value in record.__dict__.items() if key not in baseline}


def records(caplog: pytest.LogCaptureFixture, message: str) -> list[logging.LogRecord]:
    return [
        record
        for record in caplog.records
        if record.name == admin_api.__name__ and record.getMessage() == message
    ]


# --- SQLite：直接插入的事件 ---------------------------------------------------------------


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


def seed_scope(
    session: Session, number: int, company_name: str | None = None
) -> tuple[Tenant, Project, AiProvider, AiModel]:
    tenant = Tenant(
        public_id=f"00000000-0000-4000-8000-{number:012d}",
        company_name=company_name or f"Customer {number}",
        email=f"customer-{number}@analytics-export-test.example.com",
        billing_mode=BillingMode.PREPAID,
        created_at=T0,
        updated_at=T0,
    )
    provider = AiProvider(
        public_id=f"00000000-0000-4001-8000-{number:012d}",
        code=f"provider-{number}",
        display_name=f"Provider {number}",
        created_at=T0,
        updated_at=T0,
    )
    session.add_all((tenant, provider))
    session.flush()
    project = Project(
        public_id=f"00000000-0000-4002-8000-{number:012d}",
        tenant_id=tenant.id,
        name="Project",
        created_at=T0,
        updated_at=T0,
    )
    model = AiModel(
        public_id=f"00000000-0000-4003-8000-{number:012d}",
        provider_id=provider.id,
        code=f"model-{number}",
        display_name=f"Model {number}",
        created_at=T0,
        updated_at=T0,
    )
    session.add_all((project, model))
    session.flush()
    return tenant, project, provider, model


def add_event(
    session: Session,
    tenant: Tenant,
    project: Project,
    provider: AiProvider,
    model: AiModel,
    *,
    feature: str | None = "crm.chat",
) -> None:
    """A PROCESSED PREPAID token event at T0: billed 0, estimated 0.125 (a negative margin)."""
    meter = session.get(UsageMeterType, 1)
    if meter is None:
        meter = UsageMeterType(
            public_id="00000000-0000-4004-8000-000000000001",
            code="LLM_TOKEN",
            display_name="LLM token",
            payload_shape=PayloadShape.LLM_TOKEN_FIELDS,
            unit="TOKEN",
            quantity_kind=QuantityKind.INTEGER,
            created_at=T0,
            updated_at=T0,
        )
        session.add(meter)
        session.flush()
    number = next(_NUMBERS)
    session.add(
        UsageEvent(
            public_id=f"00000000-0000-4005-8000-{number:012d}",
            event_id=f"00000000-0000-7000-8000-{number:012d}",
            schema_version="1.0",
            tenant_id=tenant.id,
            project_id=project.id,
            integration_credential_id=1,
            request_id=f"request-{number}",
            provider_code_raw=provider.code,
            model_code_raw=model.code,
            provider_id=provider.id,
            model_id=model.id,
            usage_meter_type_id=meter.id,
            payload_shape=PayloadShape.LLM_TOKEN_FIELDS,
            quantity_kind=QuantityKind.INTEGER,
            unit="TOKEN",
            input_tokens=8,
            output_tokens=4,
            cache_creation_input_tokens=2,
            cache_read_input_tokens=1,
            quantity=None,
            payload_fingerprint=f"{number:064d}",
            status=UsageEventStatus.PROCESSED,
            feature=feature,
            occurred_at=T0,
            received_at=T0,
            processed_at=T0,
            created_at=T0,
            estimated_provider_cost_myr=Decimal("0.125"),
            billable_cost=Decimal("0"),
            billing_mode_snapshot=BillingMode.PREPAID.value,
            reference_customer_price=None,
        )
    )


def customers_with_events(app, count: int, *, first_number: int = 1) -> None:
    """`count` customers, the `n`-th with `count − n + 1` events: distinct event counts."""
    with app.state.session_factory() as session:
        for offset in range(count):
            scope = seed_scope(session, first_number + offset)
            for _ in range(count - offset):
                add_event(session, *scope)
        session.commit()


# --- SQLite 与 MySQL：真实计费 worker 记账的事件与直接写入的支付 ---------------------------

# 整秒的窗口，包住 test_usage_billing 的 EVENT_AT（09:30:00.25）。
WINDOW = {"occurred_from": "2026-09-29T09:00:00Z", "occurred_to": "2026-09-29T10:00:00Z"}
WINDOW_FROM_TEXT, WINDOW_TO_TEXT = WINDOW["occurred_from"], WINDOW["occurred_to"]
PAID_AT = dt.datetime(2026, 9, 29, 9, 45, 0)
assert dt.datetime(2026, 9, 29, 9) <= EVENT_AT < dt.datetime(2026, 9, 29, 10)


def _sqlite_static_factory():
    """test_usage_billing 的 SQLite 世界，但用 StaticPool：TestClient 在另一个线程里跑端点。"""
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.exec_driver_sql(_SQLITE_LEDGER_TRIGGER)
    factory = create_session_factory(engine)
    seed_meter_types(factory)
    yield factory
    engine.dispose()


@pytest.fixture(params=["sqlite", "mysql"])
def billed(request, monkeypatch, tmp_path):
    """Events priced and posted by the real billing worker on both databases, with the export app
    reading the same database."""
    factories = (
        _mysql_factory(monkeypatch) if request.param == "mysql" else _sqlite_static_factory()
    )
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

    `payments.tax_policy_version_id` 非空且外键 RESTRICT，billed 的清场不删支付与税务政策版本：
    先删本用例写的支付行，在 MySQL 上再关掉外键检查后 TRUNCATE 税务政策版本（它拒绝 DELETE，
    而且以外键引用 billed 世界的管理员）。
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
                    source_reference="Fictional policy for a margin export test",
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


# --- 导出正常路径（SQLite + MySQL） --------------------------------------------------------


def test_a_customer_export_lists_the_groups_in_order_and_a_total_equal_to_the_json(
    billed, paid
) -> None:
    """§7「导出正常路径」：两个客户加手续费。"""
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
    client = TestClient(app)
    reply = client.get(PATH, params={"group_by": "customer", **WINDOW}, headers=admin)
    assert reply.status_code == 200, reply.text
    data = reply.json()["data"]
    assert data["total"] == len(data["items"]) == 2

    response = client.get(EXPORT, params={"group_by": "customer", **WINDOW}, headers=admin)

    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "text/csv; charset=utf-8"
    assert response.headers["content-disposition"] == (
        'attachment; filename="usage-analytics_customer_20260929T170000MYT_20260929T180000MYT.csv"'
    )
    assert response.headers["cache-control"] == "no-store"
    assert response.content.startswith(BOM)
    # 每条记录以 `\r\n` 结尾，没有单独的 `\n`。
    assert response.content.endswith(b"\r\n")
    assert response.content.count(b"\n") == response.content.count(b"\r\n") == 4
    table = read_csv(response.content)
    assert table[0] == COLUMNS
    assert len(table[0]) == 31
    groups, total = table[1:-1], table[-1]
    assert [row[0] for row in table[1:]] == ["GROUP", "GROUP", "TOTAL"]
    # 顺序同接口一：事件数倒序。
    assert [row[2] for row in groups] == [item["group_key"] for item in data["items"]]
    assert [row[2] for row in groups] == [first.public_id, second.public_id]
    for row, item in zip(groups, data["items"], strict=True):
        assert row == expected_group(item, "customer", WINDOW_FROM_TEXT, WINDOW_TO_TEXT)
    assert total == expected_total(data["totals"], "customer", WINDOW_FROM_TEXT, WINDOW_TO_TEXT)
    summary = by_name(total)
    assert (
        summary["event_count"],
        summary["paid_payment_count"],
        summary["topup_amount"],
        summary["gateway_fee"],
        summary["gateway_fee_unknown_count"],
        summary["fee_period_basis"],
    ) == ("3", "2", "150.00000000", "1.50000000", "1", "paid_at")
    profit = Decimal(summary["prepaid_gross_margin"]) - Decimal("1.5")
    assert summary["gross_profit"] == f"{profit:.8f}"
    assert all(by_name(row)["gateway_fee"] == "" for row in groups)


# --- 导出不可归属与空集 ----------------------------------------------------------------


def test_a_model_filter_leaves_the_fee_columns_of_the_total_empty(app) -> None:
    """§7「导出不可归属与空集」：带 `model` 时 TOTAL 行第 21–26、29 列为空。"""
    customers_with_events(app, 2)
    admin, _ = sign_in(app)
    rows = exported(app, admin, group_by="provider", model="model-1", **DAY_QUERY)

    assert [row[0] for row in rows] == ["GROUP", "TOTAL"]
    total = by_name(rows[-1])
    assert [total[name] for name in FEE_COLUMNS] == [""] * 7
    assert [COLUMNS.index(name) + 1 for name in FEE_COLUMNS] == [21, 22, 23, 24, 25, 26, 29]
    assert (total["event_count"], total["prepaid_gross_margin"]) == ("2", "-0.25000000")
    assert (total["cost_basis"], total["usage_period_basis"]) == ("estimated", "occurred_at")


def test_an_empty_period_is_the_header_and_a_total_row_that_still_reports_fees(app) -> None:
    """§7「导出不可归属与空集」：无事件时文件 = 表头 + TOTAL 行；文件名按马来西亚时间。"""
    admin, _ = sign_in(app)
    params = {
        "group_by": "month",
        "occurred_from": "2025-12-31T16:00:00Z",
        "occurred_to": "2026-12-31T16:00:00Z",
    }
    response = export(app, admin, **params)

    assert response.status_code == 200, response.text
    assert response.headers["content-disposition"] == (
        'attachment; filename="usage-analytics_month_20260101T000000MYT_20270101T000000MYT.csv"'
    )
    table = read_csv(response.content)
    assert len(table) == 2
    assert table[0] == COLUMNS
    total = by_name(table[1])
    assert total == {
        **dict.fromkeys(COLUMNS, ""),
        "row_type": "TOTAL",
        "group_by": "month",
        "event_count": "0",
        "token_event_count": "0",
        "input_tokens": "0",
        "output_tokens": "0",
        "cache_creation_input_tokens": "0",
        "cache_read_input_tokens": "0",
        "prepaid_event_count": "0",
        "prepaid_billable_cost": "0.00000000",
        "prepaid_estimated_provider_cost_myr": "0.00000000",
        "prepaid_gross_margin": "0.00000000",
        "internal_event_count": "0",
        "internal_estimated_provider_cost_myr": "0.00000000",
        "internal_reference_customer_price": "0.00000000",
        "paid_payment_count": "0",
        "topup_amount": "0.00000000",
        "gateway_fee": "0.00000000",
        "gateway_fee_unknown_count": "0",
        "gross_profit": "0.00000000",
        "cost_basis": "estimated",
        "usage_period_basis": "occurred_at",
        "fee_period_basis": "paid_at",
        "occurred_from": "2025-12-31T16:00:00Z",
        "occurred_to": "2026-12-31T16:00:00Z",
    }


def test_the_period_columns_are_the_parsed_utc_moments(app) -> None:
    """第 30–31 列是解析后的 UTC 时刻：带 +08:00 的参数写成 Z。"""
    customers_with_events(app, 1)
    admin, _ = sign_in(app)
    rows = exported(
        app,
        admin,
        group_by="customer",
        occurred_from="2026-01-01T08:00:00+08:00",
        occurred_to="2026-01-02T08:00:00+08:00",
    )
    for row in rows:
        assert (by_name(row)["occurred_from"], by_name(row)["occurred_to"]) == (
            "2026-01-01T00:00:00Z",
            "2026-01-02T00:00:00Z",
        )


def test_the_period_text_is_whole_seconds_or_exactly_six_places() -> None:
    """设计 v2 第 30–31 列：微秒为 0 时不带小数，否则恰好 6 位。"""
    assert rfc3339_utc(dt.datetime(2026, 10, 1)) == "2026-10-01T00:00:00Z"
    assert rfc3339_utc(dt.datetime(2026, 10, 31, 15, 59, 59, 999999)) == (
        "2026-10-31T15:59:59.999999Z"
    )
    assert rfc3339_utc(dt.datetime(2026, 10, 31, 16, 0, 0, 250)) == "2026-10-31T16:00:00.000250Z"
    prepaid = UsageAnalyticsPrepaid(
        event_count=0,
        billable_cost="0.00000000",
        estimated_provider_cost_myr="0.00000000",
        gross_margin="0.00000000",
        gross_margin_rate=None,
    )
    internal = UsageAnalyticsInternalMeteredOnly(
        event_count=0,
        estimated_provider_cost_myr="0.00000000",
        reference_customer_price="0.00000000",
    )
    usage = {
        **dict.fromkeys(USAGE_KEYS[:6], 0),
        "average_input_tokens": None,
        "average_output_tokens": None,
        "prepaid": prepaid,
        "internal_metered_only": internal,
    }
    start = dt.datetime(2026, 10, 1, 0, 0, 0, 1)
    end = dt.datetime(2026, 11, 1)
    group = usage_analytics_export_group_cells(
        UsageAnalyticsItem(group_key="2026-10", group_name=None, **usage),
        group_by="month",
        occurred_from=start,
        occurred_to=end,
    )
    total = usage_analytics_export_total_cells(
        UsageAnalyticsTotalsView(
            **usage,
            payments=UsageAnalyticsPayments(
                paid_count=0,
                topup_amount="0.00000000",
                gateway_fee="0.00000000",
                gateway_fee_unknown_count=0,
            ),
            gross_profit="0.00000000",
            gross_profit_rate=None,
        ),
        group_by="month",
        occurred_from=start,
        occurred_to=end,
    )
    assert tuple(USAGE_ANALYTICS_EXPORT_COLUMNS) == tuple(COLUMNS)
    assert set(USAGE_ANALYTICS_EXPORT_TEXT_COLUMNS) == TEXT_COLUMNS
    for cells in (group, total):
        assert len(cells) == 31
        assert all(cell is None or type(cell) is str for cell in cells)
        assert cells[-2:] == ["2026-10-01T00:00:00.000001Z", "2026-11-01T00:00:00Z"]
    assert group[COLUMNS.index("fee_period_basis")] is None
    assert total[COLUMNS.index("fee_period_basis")] == "paid_at"


# --- 导出上限 ---------------------------------------------------------------------------


def test_more_groups_than_the_cap_is_too_large_and_the_total_row_does_not_count(
    app, monkeypatch
) -> None:
    """§7「导出上限」：上限设为 2；恰好 2 个分组 200（TOTAL 不计入），3 个 422。"""
    monkeypatch.setattr(admin_api, "EXPORT_MAX_ROWS", 2)
    customers_with_events(app, 2)
    admin, _ = sign_in(app)
    rows = exported(app, admin, group_by="customer", **DAY_QUERY)
    assert [row[0] for row in rows] == ["GROUP", "GROUP", "TOTAL"]

    customers_with_events(app, 1, first_number=3)
    response = export(app, admin, group_by="customer", **DAY_QUERY)

    assert error_of(response) == (422, "EXPORT_TOO_LARGE")
    assert response.json()["data"] is None
    assert "content-disposition" not in response.headers


# --- 导出租约 ---------------------------------------------------------------------------


def test_a_held_lease_is_export_busy_without_any_analytics_query(app, monkeypatch) -> None:
    """§7「导出租约」：预先占住租约 → 503 `EXPORT_BUSY`、`Retry-After: 5`，不查分组。"""
    customers_with_events(app, 1)
    admin, _ = sign_in(app)
    queried: list[object] = []
    original = analytics_repository.list_usage_analytics

    def spy(*args, **kwargs):
        queried.append(kwargs)
        return original(*args, **kwargs)

    monkeypatch.setattr(analytics_repository, "list_usage_analytics", spy)
    holder = try_acquire_export_lease()
    assert holder is not None
    try:
        busy = export(app, admin, group_by="customer", **DAY_QUERY)
    finally:
        release_export_lease(holder)

    assert error_of(busy) == (503, "EXPORT_BUSY")
    assert busy.headers["retry-after"] == "5"
    assert busy.json()["data"] is None
    assert queried == []
    # 释放后照常导出；`limit` 多取一行，只用来判「超了」。
    assert [row[0] for row in exported(app, admin, group_by="customer", **DAY_QUERY)] == [
        "GROUP",
        "TOTAL",
    ]
    assert [(call["offset"], call["limit"]) for call in queried] == [
        (0, admin_api.EXPORT_MAX_ROWS + 1)
    ]


def test_a_failure_during_the_queries_is_a_generic_500_and_frees_the_lease(
    app, monkeypatch, caplog
) -> None:
    """§7「导出租约」：查询中途注入异常 → 500 `INTERNAL_ERROR`；请求结束后租约空闲。"""
    customers_with_events(app, 1)
    admin, _ = sign_in(app)
    held_during_query: list[bool] = []

    def failing(*_args, **_kwargs):
        held_during_query.append(export_lease_held())
        raise RuntimeError("fictional query failure")

    caplog.set_level(logging.INFO, logger=admin_api.__name__)
    with monkeypatch.context() as patch:
        patch.setattr(analytics_repository, "usage_analytics_totals", failing)
        response = export(app, admin, group_by="customer", **DAY_QUERY)

    assert held_during_query == [True]
    assert not export_lease_held()
    assert response.status_code == 500
    body = response.json()
    assert body["error"] == {"code": "INTERNAL_ERROR", "message": "An internal error occurred."}
    assert body["data"] is None
    assert body["request_id"]
    assert "fictional query failure" not in response.text
    failures = records(caplog, "admin_usage_analytics_export_failed")
    assert len(failures) == 1
    assert failures[0].levelno == logging.ERROR
    assert failures[0].exc_info is not None
    assert extras_of(failures[0]) == {"path": EXPORT}
    assert records(caplog, "admin_usage_analytics_exported") == []
    # 租约已释放：下一次导出照常。
    assert exported(app, admin, group_by="customer", **DAY_QUERY)[-1][0] == "TOTAL"


def test_an_unconfigured_database_is_a_503_before_the_lease(app, monkeypatch) -> None:
    """§7「导出租约」：数据库未配置 → 503，且没有获取租约。

    `require_admin` 自己也要读库，所以只让处理函数里的 `require_session_factory` 看到一个没有
    数据库的应用：用真实的函数与真实的错误。
    """
    admin, _ = sign_in(app)
    unconfigured = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))
    acquired: list[object] = []

    def acquire():
        acquired.append(True)
        return try_acquire_export_lease()

    monkeypatch.setattr(
        admin_api,
        "require_session_factory",
        lambda _request: auth_api.require_session_factory(unconfigured),
    )
    monkeypatch.setattr(admin_api, "try_acquire_export_lease", acquire)
    response = export(app, admin, group_by="customer", **DAY_QUERY)

    assert error_of(response) == (503, "DATABASE_NOT_CONFIGURED")
    assert acquired == []
    assert not export_lease_held()


# --- 公式注入 ---------------------------------------------------------------------------


def test_text_columns_get_a_quote_prefix_and_negative_amounts_do_not(app) -> None:
    """§7「公式注入」：公司名以 `=`、功能标签以 `-` 开头时加单引号；负毛利的金额列不加。"""
    with app.state.session_factory() as session:
        scope = seed_scope(session, 1, company_name="=HYPERLINK(1)")
        add_event(session, *scope, feature="-crm.chat")
        session.commit()
        public_id = scope[0].public_id
    admin, _ = sign_in(app)

    customers = exported(app, admin, group_by="customer", **DAY_QUERY)
    group, total = by_name(customers[0]), by_name(customers[-1])
    assert (group["group_key"], group["group_name"]) == (public_id, "'=HYPERLINK(1)")
    assert group["prepaid_gross_margin"] == "-0.12500000"
    assert total["prepaid_gross_margin"] == "-0.12500000"
    assert total["gross_profit"] == "-0.12500000"

    features = exported(app, admin, group_by="feature", **DAY_QUERY)
    assert by_name(features[0])["group_key"] == "'-crm.chat"
    for row in (*customers, *features):
        for name, value in by_name(row).items():
            if name not in TEXT_COLUMNS:
                assert not value.startswith("'"), name


# --- 导出日志 ---------------------------------------------------------------------------


def test_a_successful_export_logs_one_line_with_only_the_admin_grouping_and_row_count(
    app, caplog
) -> None:
    """§7「导出日志」：恰好一条 INFO，`extra` 只有三个键，不含公司名与金额。"""
    customers_with_events(app, 2)
    admin, admin_id = sign_in(app)
    caplog.set_level(logging.INFO, logger=admin_api.__name__)
    rows = exported(app, admin, group_by="customer", **DAY_QUERY)

    lines = records(caplog, "admin_usage_analytics_exported")
    assert len(lines) == 1
    assert lines[0].levelno == logging.INFO
    extras = extras_of(lines[0])
    assert extras == {"user_id": admin_id, "group_by": "customer", "row_count": 2}
    logged = repr(extras) + lines[0].getMessage()
    for leaked in ("Customer 1", "Customer 2", by_name(rows[-1])["prepaid_gross_margin"]):
        assert leaked not in logged
    assert records(caplog, "admin_usage_analytics_export_failed") == []


# --- 鉴权与校验 -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "params",
    [
        {"group_by": "provider", **DAY_QUERY},
        {"group_by": "week", **DAY_QUERY},
        {"group_by": "day", "occurred_from": "2026-01-01T00:00:00Z"},
    ],
)
def test_anonymous_is_401_and_a_customer_is_403_before_validation(app, params) -> None:
    """§7「鉴权」：匿名 401、客户令牌 403，都先于参数校验；不占租约。"""
    client = TestClient(app)
    anonymous = client.get(EXPORT, params=params)
    customer, _ = sign_in(app, UserRole.CUSTOMER)
    refused = client.get(EXPORT, params=params, headers=customer)

    assert error_of(anonymous) == (401, "TOKEN_INVALID")
    assert error_of(refused) == (403, "ADMIN_REQUIRED")
    assert "content-disposition" not in refused.headers


@pytest.mark.parametrize(
    "params",
    [
        {"group_by": "week", **DAY_QUERY},
        {"group_by": "day", "occurred_from": "2026-01-01T00:00:00Z"},
        # 367 天：比上限多一天。
        {
            "group_by": "day",
            "occurred_from": "2026-01-01T00:00:00Z",
            "occurred_to": "2027-01-03T00:00:00Z",
        },
        {"group_by": "feature", "feature": " ", **DAY_QUERY},
    ],
)
def test_invalid_admin_queries_are_the_interface_one_validation_error(app, params) -> None:
    admin, _ = sign_in(app)
    response = export(app, admin, **params)
    assert error_of(response) == (422, "VALIDATION_ERROR")


def test_paging_parameters_are_ignored(app) -> None:
    """没有 `page` / `page_size`：传了按未知参数忽略。"""
    customers_with_events(app, 2)
    admin, _ = sign_in(app)
    rows = exported(app, admin, group_by="customer", page=0, page_size=1, **DAY_QUERY)
    assert [row[0] for row in rows] == ["GROUP", "GROUP", "TOTAL"]

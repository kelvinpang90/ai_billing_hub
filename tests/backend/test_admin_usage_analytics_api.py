"""End-to-end coverage for the read-only admin usage analytics endpoint."""

from __future__ import annotations

import base64
import datetime as dt
import itertools
import os
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from provider_cost_world import (
    MYR_THREE_DECEMBER,
    P_CHAT,
    P_CRM,
    P_PRE,
    PROVIDER_CODE,
    Q1,
    Q2,
    Q3,
    UNALLOCATABLE_RECORDS,
    add_record,
    build_myr_three_world,
    build_standard_world,
    build_unallocatable_world,
    mysql_factory,
    sqlite_shared_factory,
)
from provider_cost_world import World as ProviderCostWorld
from sqlalchemy import create_engine, delete, event, insert, select, update
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool
from test_usage_billing import (
    _SQLITE_LEDGER_TRIGGER,
    ESTIMATED_MYR,
    EVENT_AT,
    MARKUP_BILLABLE,
    TEST_EMAIL_DOMAIN,
    TOKENS,
    Tenancy,
    _mysql_factory,
    seed_meter_types,
)
from test_usage_billing import NOW as BILLED_AT
from test_usage_billing import World as BillingWorld

from app.core.config import Settings
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
from app.models.wallet import ReferenceType, TransactionType
from app.repositories import usage_events as event_repository
from app.repositories import wallet as wallet_repository
from app.repositories.usage_analytics import (
    MalaysiaMonth,
    UsageAnalyticsTotals,
    list_usage_analytics,
    usage_analytics_totals,
)
from app.services.auth import utc_now

PATH = "/api/v1/admin/usage-analytics"
T0 = dt.datetime(2026, 1, 1)
T1 = dt.datetime(2026, 1, 2)


def settings_for(tmp_path) -> Settings:
    jwt, master = tmp_path / "jwt.key", tmp_path / "master.key"
    jwt.write_text("a-test-signing-key-that-is-long-enough", encoding="utf-8")
    master.write_text(f"1:{base64.b64encode(os.urandom(32)).decode()}\n", encoding="utf-8")
    return Settings(jwt_secret_file=str(jwt), master_key_file=str(master), database_url="")


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
def client(app) -> TestClient:
    return TestClient(app)


def headers(
    app, role: UserRole = UserRole.ADMIN, domain: str = "@analytics-test.example.com"
) -> dict[str, str]:
    with app.state.session_factory() as session:
        user = User(
            email=f"{role.value.lower()}{domain}",
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
    return {"Authorization": f"Bearer {token}"}


def seed_scope(
    session: Session, number: int, name: str = "Project"
) -> tuple[Tenant, Project, AiProvider, AiModel]:
    tenant = Tenant(
        public_id=f"00000000-0000-4000-8000-{number:012d}",
        company_name=f"Customer {number}",
        email=f"customer-{number}@analytics-test.example.com",
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
        name=name,
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
    number: int,
    tenant: Tenant,
    project: Project,
    provider: AiProvider | None,
    model: AiModel | None,
    *,
    occurred_at: dt.datetime = T0,
    status: UsageEventStatus = UsageEventStatus.PROCESSED,
    feature: str | None = "crm.chat",
    token: bool = True,
    billing_mode: BillingMode = BillingMode.PREPAID,
) -> None:
    meter = session.get(UsageMeterType, 1 if token else 2)
    if meter is None:
        meter = UsageMeterType(
            public_id=f"00000000-0000-4004-8000-{(1 if token else 2):012d}",
            code="LLM_TOKEN" if token else "QUANTITY",
            display_name="LLM token" if token else "Quantity",
            payload_shape=PayloadShape.LLM_TOKEN_FIELDS if token else PayloadShape.QUANTITY,
            unit="TOKEN" if token else "UNIT",
            quantity_kind=QuantityKind.INTEGER,
            created_at=T0,
            updated_at=T0,
        )
        session.add(meter)
        session.flush()
    session.add(
        UsageEvent(
            public_id=f"00000000-0000-4005-8000-{number:012d}",
            event_id=f"00000000-0000-7000-8000-{number:012d}",
            schema_version="1.0",
            tenant_id=tenant.id,
            project_id=project.id,
            integration_credential_id=1,
            request_id=f"request-{number}",
            provider_code_raw=provider.code if provider else "unresolved",
            model_code_raw=model.code if model else "unresolved",
            provider_id=provider.id if provider else None,
            model_id=model.id if model else None,
            usage_meter_type_id=meter.id,
            payload_shape=PayloadShape.LLM_TOKEN_FIELDS if token else PayloadShape.QUANTITY,
            quantity_kind=QuantityKind.INTEGER,
            unit="TOKEN" if token else "UNIT",
            input_tokens=8 if token else None,
            output_tokens=4 if token else None,
            cache_creation_input_tokens=2 if token else None,
            cache_read_input_tokens=1 if token else None,
            quantity=None if token else Decimal("2"),
            payload_fingerprint=f"{number:064d}",
            status=status,
            feature=feature,
            occurred_at=occurred_at,
            received_at=T0,
            processed_at=T0 if status is UsageEventStatus.PROCESSED else None,
            created_at=T0,
            estimated_provider_cost_myr=Decimal("0.125"),
            billable_cost=Decimal("0"),
            billing_mode_snapshot=billing_mode.value
            if status is UsageEventStatus.PROCESSED
            else None,
            reference_customer_price=Decimal("0.5")
            if billing_mode is BillingMode.INTERNAL_METERED_ONLY
            else None,
        )
    )


def query(client, admin, **params) -> dict:
    response = client.get(
        PATH,
        params={
            "group_by": "provider",
            "occurred_from": "2026-01-01T00:00:00Z",
            "occurred_to": "2026-01-02T00:00:00Z",
            **params,
        },
        headers=admin,
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]


@pytest.mark.parametrize("group_by", ["provider", "model", "customer", "project", "feature"])
def test_each_grouping_uses_resolved_keys_and_keeps_null_separate(
    app, client, group_by: str
) -> None:
    with app.state.session_factory() as session:
        tenant, project, provider, model = seed_scope(session, 1, "Alpha")
        add_event(session, 1, tenant, project, provider, model)
        add_event(session, 2, tenant, project, None, None, feature=None)
        session.commit()
    items = query(client, headers(app), group_by=group_by)["items"]
    if group_by in {"provider", "model", "feature"}:
        assert len(items) == 2 and any(item["group_key"] is None for item in items)
    else:
        assert len(items) == 1
    resolved = next(item for item in items if item["group_key"] is not None)
    assert (
        resolved["group_key"]
        == {
            "provider": "provider-1",
            "model": "provider-1:model-1",
            "customer": tenant.public_id,
            "project": project.public_id,
            "feature": "crm.chat",
        }[group_by]
    )
    assert resolved["group_name"] == ({"customer": "Customer 1", "project": "Alpha"}.get(group_by))


def test_period_status_modes_tokens_filters_order_paging_and_money(app, client) -> None:
    with app.state.session_factory() as session:
        first, first_project, provider, model = seed_scope(session, 1)
        second, second_project, provider_two, model_two = seed_scope(session, 2)
        add_event(session, 1, first, first_project, provider, model)
        add_event(
            session, 2, first, first_project, provider, model, feature="crm.other", token=False
        )
        add_event(
            session,
            3,
            first,
            first_project,
            provider,
            model,
            billing_mode=BillingMode.INTERNAL_METERED_ONLY,
        )
        add_event(session, 4, first, first_project, provider, model, occurred_at=T1)
        add_event(
            session, 5, first, first_project, provider, model, status=UsageEventStatus.FAILED_FINAL
        )
        add_event(session, 6, second, second_project, provider_two, model_two)
        session.commit()
    admin = headers(app)
    data = query(client, admin, group_by="customer", page_size=1)
    assert (data["cost_basis"], data["total"], data["page"], len(data["items"])) == (
        "estimated",
        2,
        1,
        1,
    )
    item = data["items"][0]
    assert item["group_key"] == first.public_id
    assert (
        item["event_count"],
        item["token_event_count"],
        item["average_input_tokens"],
        item["average_output_tokens"],
    ) == (3, 2, "8", "4")
    assert item["prepaid"] == {
        "event_count": 2,
        "billable_cost": "0.00000000",
        "estimated_provider_cost_myr": "0.25000000",
        "gross_margin": "-0.25000000",
        "gross_margin_rate": None,
        # AIH-TASK-248：没有对账记录，供应商成本就是估算。
        "covered_estimated_cost_myr": "0.00000000",
        "reconciled_provider_cost_myr": "0.00000000",
        "provider_cost_myr": "0.25000000",
        "provider_cost_basis": "estimated",
        "gross_margin_on_provider_cost": "-0.25000000",
        "gross_margin_on_provider_cost_rate": None,
    }
    assert item["internal_metered_only"] == {
        "event_count": 1,
        "estimated_provider_cost_myr": "0.12500000",
        "reference_customer_price": "0.50000000",
        "covered_estimated_cost_myr": "0.00000000",
        "reconciled_provider_cost_myr": "0.00000000",
        "provider_cost_myr": "0.12500000",
        "provider_cost_basis": "estimated",
    }
    if app.state.engine.dialect.name == "mysql":
        assert item["prepaid"]["estimated_provider_cost_myr"] == "0.25000000"
        assert item["internal_metered_only"]["reference_customer_price"] == "0.50000000"
    filtered = query(
        client,
        admin,
        customer_id=first.public_id,
        project_id=first_project.public_id,
        feature="crm.other",
    )
    assert (
        filtered["total"],
        filtered["items"][0]["token_event_count"],
        filtered["items"][0]["average_input_tokens"],
    ) == (1, 0, None)


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"group_by": "bad"},
        {"occurred_from": "2026-01-01T00:00:00Z"},
        {"occurred_to": "2026-01-02T00:00:00Z"},
        {"occurred_from": "2026-01-01T00:00:00Z", "occurred_to": "2027-01-03T00:00:01Z"},
        {"feature": " "},
    ],
)
def test_authentication_precedes_validation_and_invalid_admin_queries_use_envelope(
    app, client, params
) -> None:
    assert client.get(PATH, params=params).status_code == 401
    assert (
        client.get(PATH, params=params, headers=headers(app, UserRole.CUSTOMER)).status_code == 403
    )
    response = client.get(PATH, params=params, headers=headers(app))
    assert (response.status_code, response.json()["error"]["code"]) == (422, "VALIDATION_ERROR")


# 整秒的窗口，包住 test_usage_billing 的 EVENT_AT（09:30:00.25）。
WINDOW_FROM = dt.datetime(2026, 9, 29, 9, 0, 0)
WINDOW_TO = dt.datetime(2026, 9, 29, 10, 0, 0)
assert WINDOW_FROM <= EVENT_AT < WINDOW_TO


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
    """Events priced and posted by the real billing worker (test_usage_billing.World), on both
    databases, with the analytics app reading the same database."""
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


def test_billed_sums_are_exact_on_both_databases_and_ties_sort_by_group_key(billed) -> None:
    app, world = billed
    world.publish_markup_usd()
    first, second = world.tenant("first"), world.tenant("second")
    for tenant in (first, second):
        world.fund(tenant, "100")
    events = [world.event(first), world.event(first), world.event(second), world.event(second)]
    assert world.bill().claimed == 4
    rows = [world.row(event_id) for event_id in events]
    assert {row.status for row in rows} == {UsageEventStatus.PROCESSED}
    assert all(row.billable_cost > 0 for row in rows)

    def expected(tenant) -> tuple[Decimal, Decimal]:
        mine = [row for row in rows if row.tenant_id == tenant.id]
        return (
            sum((row.billable_cost for row in mine), Decimal(0)),
            sum((row.estimated_provider_cost_myr for row in mine), Decimal(0)),
        )

    with world.factory() as session:
        analytics, total = list_usage_analytics(
            session,
            group_by="customer",
            offset=0,
            limit=10,
            customer_id=None,
            project_id=None,
            provider=None,
            model=None,
            feature=None,
            occurred_from=WINDOW_FROM,
            occurred_to=WINDOW_TO,
        )
    assert total == 2

    def analytics_for(group_by: str, **filters):
        arguments = {"customer_id": None, "project_id": None, "provider": None, "model": None}
        arguments.update(filters)
        with world.factory() as session:
            return list_usage_analytics(
                session,
                group_by=group_by,
                offset=0,
                limit=10,
                feature=None,
                occurred_from=WINDOW_FROM,
                occurred_to=WINDOW_TO,
                **arguments,
            )

    # model 分组的键是方言编译的字符串拼接（SQLite `||`、MySQL `concat()`），
    # GROUP BY 与 ORDER BY 用同一个表达式：在两种库上都要能跑、结果相同。
    model_rows, model_total = analytics_for("model")
    assert (model_total, [(row.group_key, row.event_count) for row in model_rows]) == (
        1,
        [("anthropic:claude-x", 4)],
    )
    # provider / model 筛选按上报的原始代码，与列表接口同一口径。
    for filters, expected_events in (
        ({"provider": "anthropic"}, 4),
        ({"model": "claude-x"}, 4),
        ({"provider": "anthropic", "model": "claude-x"}, 4),
        ({"provider": "no-such-provider"}, 0),
        ({"model": "no-such-model"}, 0),
    ):
        filtered_rows, _ = analytics_for("provider", **filters)
        assert sum(row.event_count for row in filtered_rows) == expected_events, filters
    # MySQL 的 SUM 对整数 CASE 返回 DECIMAL：仓储层必须交出 int；金额交出逐位精确的 Decimal。
    for row in analytics:
        assert all(
            type(value) is int
            for value in (
                row.event_count,
                row.token_event_count,
                row.input_tokens,
                row.output_tokens,
                row.prepaid_event_count,
                row.internal_event_count,
            )
        )
        assert isinstance(row.prepaid_billable_cost, Decimal)
        assert isinstance(row.prepaid_estimated_cost, Decimal)
    by_key = {row.group_key: row for row in analytics}
    for tenant in (first, second):
        billable, estimated = expected(tenant)
        assert (
            by_key[tenant.public_id].prepaid_billable_cost,
            by_key[tenant.public_id].prepaid_estimated_cost,
        ) == (billable, estimated)

    data = query(
        client=TestClient(app),
        admin=headers(app, domain=TEST_EMAIL_DOMAIN),
        group_by="customer",
        occurred_from="2026-09-29T09:00:00Z",
        occurred_to="2026-09-29T10:00:00Z",
    )
    # 两组事件数相同：按分组键（public_id）升序。
    assert [item["group_key"] for item in data["items"]] == sorted(
        (first.public_id, second.public_id)
    )
    for item in data["items"]:
        tenant = first if item["group_key"] == first.public_id else second
        billable, estimated = expected(tenant)
        margin = billable - estimated
        assert item["prepaid"] == {
            "event_count": 2,
            "billable_cost": f"{billable:.8f}",
            "estimated_provider_cost_myr": f"{estimated:.8f}",
            "gross_margin": f"{margin:.8f}",
            "gross_margin_rate": format((margin / billable).normalize(), "f"),
        }


# --- AIH-TASK-221：按马来西亚日 / 月分组与全量合计（设计闸门 #709 v2 §7） -----------------

UNKNOWN_ID = "00000000-0000-0000-0000-000000000000"
NO_FILTERS: dict[str, str | None] = {
    "customer_id": None,
    "project_id": None,
    "provider": None,
    "model": None,
    "feature": None,
}
PERIOD_FROM = dt.datetime(2026, 10, 1)
PERIOD_TO = dt.datetime(2027, 2, 1)
GROUPINGS = ("provider", "model", "customer", "project", "feature")
ZERO_TOTALS = UsageAnalyticsTotals(
    0, 0, 0, 0, 0, 0, 0, Decimal(0), Decimal(0), 0, Decimal(0), Decimal(0)
)
_INSERTED = itertools.count(1)


def insert_processed(
    world: BillingWorld,
    template_id: int,
    tenant: Tenancy,
    *,
    occurred_at: dt.datetime,
    feature: str | None = "crm.chat",
    quantity: bool = False,
    billable: Decimal = Decimal(0),
    estimated: Decimal | None = None,
    internal: bool = False,
) -> None:
    """A PROCESSED event inserted directly, with the billed template's snapshot but 0 billed (so
    it needs no ledger row); usage_events has only BEFORE UPDATE triggers. `quantity` makes it an
    AUDIO_SECOND (QUANTITY) event.

    `billable` 大于 0 时先在同一事务里记一条 `AI_USAGE` 账本行并链接它（计费额大于 0 的已处理
    事件必有账本行）；`estimated` 替换估算成本；
    `internal` 改成内部计量快照（计费额 0、有参考售价）。
    """
    template = world.row(template_id)
    skipped = {"id", "public_id", "event_id", "request_id", "wallet_transaction_id"}
    values = {
        column.key: getattr(template, column.key)
        for column in UsageEvent.__table__.columns
        if column.key not in skipped
    }
    number = next(_INSERTED)
    values.update(
        public_id=f"00000000-0000-4006-8000-{number:012d}",
        event_id=f"00000000-0000-7006-8000-{number:012d}",
        request_id=f"inserted-{number}",
        tenant_id=tenant.id,
        project_id=tenant.project_id,
        integration_credential_id=tenant.credential_id,
        occurred_at=occurred_at,
        feature=feature,
        billable_cost=Decimal(0),
        wallet_transaction_id=None,
    )
    if estimated is not None:
        values["estimated_provider_cost_myr"] = estimated
    if internal:
        values.update(
            billing_mode_snapshot=BillingMode.INTERNAL_METERED_ONLY.value,
            reference_customer_price=Decimal(1),
        )
    with world.factory() as session:
        if billable:
            posted = wallet_repository.post_transaction(
                session,
                tenant_id=tenant.id,
                transaction_type=TransactionType.AI_USAGE,
                amount=-billable,
                reference_type=ReferenceType.USAGE_EVENT,
                reference_id=values["event_id"],
                now=BILLED_AT,
            )
            values.update(billable_cost=billable, wallet_transaction_id=posted.transaction.id)
        if quantity:
            meter = event_repository.find_meter_type(session, "AUDIO_SECOND")
            assert meter is not None
            values.update(
                usage_meter_type_id=meter.id,
                payload_shape=meter.payload_shape,
                unit=meter.unit,
                quantity_kind=meter.quantity_kind,
                input_tokens=None,
                output_tokens=None,
                cache_creation_input_tokens=None,
                cache_read_input_tokens=None,
                quantity=Decimal(2),
            )
        session.execute(insert(UsageEvent).values(**values))
        session.commit()


def billed_template(world: BillingWorld) -> tuple[Tenancy, int]:
    """One tenant and one event billed by the real worker (at EVENT_AT, outside PERIOD_*)."""
    world.publish_markup_usd()
    tenant = world.tenant("periods")
    world.fund(tenant, "100")
    template = world.event(tenant)
    assert world.bill().claimed == 1
    return tenant, template


def grouped(
    world: BillingWorld,
    group_by: str,
    *,
    offset: int = 0,
    limit: int = 10,
    occurred_from: dt.datetime = PERIOD_FROM,
    occurred_to: dt.datetime = PERIOD_TO,
):
    with world.factory() as session:
        return list_usage_analytics(
            session,
            group_by=group_by,
            offset=offset,
            limit=limit,
            occurred_from=occurred_from,
            occurred_to=occurred_to,
            **NO_FILTERS,
        )


def test_days_and_months_follow_the_malaysian_calendar_on_both_databases(billed) -> None:
    _app, world = billed
    tenant, template = billed_template(world)
    for moment in (
        dt.datetime(2026, 10, 31, 15, 59, 59, 999999),
        dt.datetime(2026, 10, 31, 16, 0, 0),
        dt.datetime(2026, 12, 31, 15, 59, 59),
        dt.datetime(2026, 12, 31, 16, 0, 0),
    ):
        insert_processed(world, template, tenant, occurred_at=moment)
    for group_by, keys in (
        ("day", ["2026-10-31", "2026-11-01", "2026-12-31", "2027-01-01"]),
        ("month", ["2026-10", "2026-11", "2026-12", "2027-01"]),
    ):
        rows, total = grouped(world, group_by)
        assert total == 4, group_by
        assert [(row.group_key, row.group_name, row.event_count) for row in rows] == [
            (key, None, 1) for key in keys
        ], group_by


def test_days_and_months_sort_by_key_not_by_event_count_and_page(billed) -> None:
    _app, world = billed
    tenant, template = billed_template(world)
    for moment, count in (
        (dt.datetime(2026, 10, 10, 4, 0, 0), 1),
        (dt.datetime(2026, 11, 10, 4, 0, 0), 3),
        (dt.datetime(2026, 12, 10, 4, 0, 0), 2),
    ):
        for _ in range(count):
            insert_processed(world, template, tenant, occurred_at=moment)
    for group_by, keys in (
        ("day", ["2026-10-10", "2026-11-10", "2026-12-10"]),
        ("month", ["2026-10", "2026-11", "2026-12"]),
    ):
        first, first_total = grouped(world, group_by, limit=2)
        second, second_total = grouped(world, group_by, offset=2, limit=2)
        assert (first_total, second_total) == (3, 3), group_by
        assert [(row.group_key, row.event_count) for row in first] == [
            (keys[0], 1),
            (keys[1], 3),
        ], group_by
        assert [(row.group_key, row.event_count) for row in second] == [(keys[2], 2)], group_by


def test_the_month_expression_compiles_per_dialect_and_runs_with_bound_parameters(
    billed,
) -> None:
    _app, world = billed
    tenant, template = billed_template(world)
    insert_processed(world, template, tenant, occurred_at=dt.datetime(2026, 10, 31, 16, 0, 0))
    engine = world.factory.kw["bind"]
    month = MalaysiaMonth(UsageEvent.occurred_at)
    compiled = str(month.compile(dialect=engine.dialect))
    if engine.dialect.name == "mysql":
        # 带绑定参数的语句经驱动的 format 参数风格展开：表达式里不能有 `%`（设计 §9 备选 E）。
        assert "EXTRACT(YEAR_MONTH" in compiled and "%" not in compiled
    else:
        assert "strftime" in compiled
    statement = select(month).where(
        UsageEvent.occurred_at >= PERIOD_FROM, UsageEvent.occurred_at < PERIOD_TO
    )
    with world.factory() as session:
        assert list(session.execute(statement).scalars()) == [202611]


def test_totals_equal_the_sum_of_every_grouping(billed) -> None:
    _app, world = billed
    world.publish_markup_usd()
    first, second, internal = (
        world.tenant("first"),
        world.tenant("second"),
        world.tenant("internal"),
    )
    for tenant in (first, second):
        world.fund(tenant, "100")
    with world.factory.begin() as session:
        session.execute(
            update(Tenant)
            .where(Tenant.id == internal.id)
            .values(billing_mode=BillingMode.INTERNAL_METERED_ONLY)
        )
    template = world.event(first, feature="crm.chat")
    world.event(first)  # 功能为 null。
    world.event(second, feature="crm.other")
    world.event(internal, feature="crm.chat")
    assert world.bill().claimed == 4
    insert_processed(
        world, template, second, occurred_at=EVENT_AT, feature="crm.audio", quantity=True
    )
    world.event(first, feature="crm.chat")  # 留在 RECEIVED：不计。

    with world.factory() as session:
        totals = usage_analytics_totals(
            session, occurred_from=WINDOW_FROM, occurred_to=WINDOW_TO, **NO_FILTERS
        )
    assert (
        totals.event_count,
        totals.token_event_count,
        totals.prepaid_event_count,
        totals.internal_event_count,
    ) == (5, 4, 4, 1)
    # QUANTITY 事件不进 token 合计。
    assert (
        totals.input_tokens,
        totals.output_tokens,
        totals.cache_creation_input_tokens,
        totals.cache_read_input_tokens,
    ) == tuple(4 * TOKENS[name] for name in TOKENS)
    expected_money = (
        3 * MARKUP_BILLABLE,
        4 * ESTIMATED_MYR,
        ESTIMATED_MYR,
        MARKUP_BILLABLE,
    )
    money = (
        totals.prepaid_billable_cost,
        totals.prepaid_estimated_cost,
        totals.internal_estimated_cost,
        totals.internal_reference_price,
    )
    assert money == expected_money
    assert all(isinstance(value, Decimal) for value in money)
    if world.factory.kw["bind"].dialect.name == "mysql":
        assert [str(value) for value in money] == [f"{value:.8f}" for value in expected_money]

    for group_by in GROUPINGS:
        rows, offset = [], 0
        while True:
            page, total = grouped(
                world,
                group_by,
                offset=offset,
                limit=2,
                occurred_from=WINDOW_FROM,
                occurred_to=WINDOW_TO,
            )
            rows.extend(page)
            offset += 2
            if offset >= total:
                break
        assert len(rows) == total, group_by
        for field in UsageAnalyticsTotals._fields:
            value = getattr(totals, field)
            start = Decimal(0) if isinstance(value, Decimal) else 0
            assert sum((getattr(row, field) for row in rows), start) == value, (group_by, field)


def test_totals_are_zero_for_an_unknown_customer_or_project_and_an_empty_period(billed) -> None:
    _app, world = billed
    tenant, _template = billed_template(world)
    engine = world.factory.kw["bind"]
    statements: list[str] = []

    def record(conn, cursor, statement, parameters, context, executemany) -> None:
        statements.append(statement)

    for filters in ({"customer_id": UNKNOWN_ID}, {"project_id": UNKNOWN_ID}):
        statements.clear()
        event.listen(engine, "before_cursor_execute", record)
        try:
            with world.factory() as session:
                totals = usage_analytics_totals(
                    session,
                    occurred_from=WINDOW_FROM,
                    occurred_to=WINDOW_TO,
                    **{**NO_FILTERS, **filters},
                )
        finally:
            event.remove(engine, "before_cursor_execute", record)
        assert totals == ZERO_TOTALS, filters
        # 只查了客户或项目，没有发出聚合查询。
        assert statements and not any("usage_events" in text for text in statements), filters

    with world.factory() as session:
        mine = usage_analytics_totals(
            session,
            occurred_from=WINDOW_FROM,
            occurred_to=WINDOW_TO,
            **{
                **NO_FILTERS,
                "customer_id": tenant.public_id,
                "project_id": tenant.project_public_id,
            },
        )
        empty = usage_analytics_totals(session, occurred_from=T0, occurred_to=T1, **NO_FILTERS)
    assert mine.event_count == 1
    assert empty == ZERO_TOTALS
    assert all(type(getattr(empty, field)) is int for field in UsageAnalyticsTotals._fields[:7])
    assert all(isinstance(value, Decimal) for value in empty[7:9] + empty[10:])


# --- AIH-TASK-222：接口的 totals、手续费块与毛利（设计闸门 #709 v2 §2「接口一」、§7） ---------

# AIH-TASK-073 的 data 键；本任务只新增 `usage_period_basis` 与 `totals`。
TASK_073_KEYS = {"cost_basis", "items", "page", "page_size", "total"}
ITEM_KEYS = {
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
    "prepaid",
    "internal_metered_only",
}
TOTALS_KEYS = (ITEM_KEYS - {"group_key", "group_name"}) | {
    "payments",
    "gross_profit",
    "gross_profit_rate",
    # AIH-TASK-248（设计闸门 #756 v2 §2「接口与导出」）。
    "gross_profit_on_provider_cost",
    "gross_profit_on_provider_cost_rate",
    "reconciliation_coverage",
    "unallocated_reconciled_months",
    "unallocated_reconciled_cost_myr",
}
# AIH-TASK-248 在 `data` 顶层新增的两个口径键。
PROVIDER_COST_DATA_KEYS = {"provider_cost_allocation", "reconciliation_period_basis"}
PAYMENTS_KEYS = {
    "period_basis",
    "paid_count",
    "topup_amount",
    "gateway_fee",
    "gateway_fee_unknown_count",
}
# 分组项与合计里可以逐项相加的键。
ADDITIVE_KEYS = (
    "event_count",
    "token_event_count",
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)
PERIOD_QUERY = {"occurred_from": "2026-10-01T00:00:00Z", "occurred_to": "2027-02-01T00:00:00Z"}
IN_PERIOD = dt.datetime(2026, 11, 10, 4, 0, 0)
FEE_AT = dt.datetime(2026, 11, 15, 4, 0, 0)
ZERO_MONEY = "0.00000000"
_PAYMENTS = itertools.count(1)


def fixed(value: str) -> str:
    """The 8-place text of an exactly representable amount."""
    return f"{Decimal(value):.8f}"


def ratio(value: Decimal) -> str:
    """What the endpoint's `ratio_text` gives: normalised, fixed-point."""
    return format(value.normalize(), "f")


@pytest.fixture
def fees(billed):
    """Writes `PAID` MYR payments into the billed world; removes them before `billed` tears down.

    `payments.tax_policy_version_id` 非空且外键 RESTRICT，billed 的清场（test_usage_billing.clean）
    不删支付与税务政策版本，删租户会被 `fk_payments_tenant_id` 拦下：本夹具先删本用例写的支付行，
    在 MySQL 上再照 payment_world._clean 关掉外键检查后 TRUNCATE 税务政策版本（它拒绝 DELETE，
    而且以外键引用 billed 世界的管理员）。
    """
    _app, world = billed
    written: list[str] = []
    versions: list[int] = []

    def pay(
        tenant: Tenancy,
        fee: str | None,
        *,
        amount: str = "100.00",
        paid_at: dt.datetime = FEE_AT,
    ) -> None:
        with world.factory() as session:
            if not versions:
                version = TaxPolicyVersion(
                    public_id=f"00000000-0000-4009-8000-{next(_PAYMENTS):012d}",
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
                    source_reference="Fictional policy for a margin analytics test",
                    created_by=world.admin.id,
                    approved_by=None,
                    approved_at=None,
                    created_at=T0,
                    updated_at=T0,
                )
                session.add(version)
                session.flush()
                versions.append(int(version.id))
            public_id = f"00000000-0000-4008-8000-{next(_PAYMENTS):012d}"
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
                    expires_at=paid_at,
                    paid_at=paid_at,
                    created_at=paid_at,
                    updated_at=paid_at,
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


def priced_template(world: BillingWorld) -> tuple[Tenancy, int]:
    """`billed_template` with enough in the wallet for the directly posted `AI_USAGE` rows."""
    tenant, template = billed_template(world)
    world.fund(tenant, "1000")
    return tenant, template


def analytics(app, admin, **params) -> dict:
    return query(TestClient(app), admin, **{**PERIOD_QUERY, **params})


def test_the_data_keeps_task_073_keys_and_adds_the_period_basis_and_totals(billed, fees) -> None:
    app, world = billed
    tenant, template = priced_template(world)
    for moment in (IN_PERIOD, dt.datetime(2026, 12, 10, 4, 0, 0)):
        insert_processed(
            world,
            template,
            tenant,
            occurred_at=moment,
            billable=Decimal("4"),
            estimated=Decimal("1"),
        )
    insert_processed(world, template, tenant, occurred_at=IN_PERIOD, feature=None, internal=True)
    fees(tenant, "1.50")
    admin = headers(app, domain=TEST_EMAIL_DOMAIN)
    for group_by in (*GROUPINGS, "day", "month"):
        data = analytics(app, admin, group_by=group_by)
        assert set(data) == TASK_073_KEYS | {"usage_period_basis", "totals"} | (
            PROVIDER_COST_DATA_KEYS
        ), group_by
        assert (data["cost_basis"], data["usage_period_basis"]) == ("estimated", "occurred_at")
        assert data["items"] and all(set(item) == ITEM_KEYS for item in data["items"]), group_by
        totals = data["totals"]
        assert set(totals) == TOTALS_KEYS, group_by
        assert set(totals["payments"]) == PAYMENTS_KEYS
        assert set(totals["prepaid"]) == set(data["items"][0]["prepaid"])
        assert set(totals["internal_metered_only"]) == set(
            data["items"][0]["internal_metered_only"]
        )
        assert totals["payments"]["period_basis"] == "paid_at"
        # 一页装下全部分组：可加字段等于各组之和（含 null 组）。
        assert data["total"] == len(data["items"]), group_by
        for key in ADDITIVE_KEYS:
            assert totals[key] == sum(item[key] for item in data["items"]), (group_by, key)
        assert totals["prepaid"]["billable_cost"] == fixed("8")
        assert totals["prepaid"]["gross_margin"] == fixed("6")
        assert totals["internal_metered_only"]["reference_customer_price"] == fixed("1")
        # 平均值按合计重新算。
        assert totals["average_input_tokens"] == ratio(
            Decimal(totals["input_tokens"]) / Decimal(totals["token_event_count"])
        )
    days = analytics(app, admin, group_by="day")["items"]
    months = analytics(app, admin, group_by="month")["items"]
    assert [(item["group_key"], item["group_name"]) for item in days] == [
        ("2026-11-10", None),
        ("2026-12-10", None),
    ]
    assert [item["group_key"] for item in months] == ["2026-11", "2026-12"]


def test_the_totals_do_not_change_with_the_page(billed, fees) -> None:
    app, world = billed
    tenant, template = priced_template(world)
    for month in (10, 11, 12):
        insert_processed(
            world,
            template,
            tenant,
            occurred_at=dt.datetime(2026, month, 10, 4, 0, 0),
            billable=Decimal("2"),
            estimated=Decimal("1"),
        )
    fees(tenant, "0.75")
    admin = headers(app, domain=TEST_EMAIL_DOMAIN)
    first = analytics(app, admin, group_by="month", page=1, page_size=2)
    second = analytics(app, admin, group_by="month", page=2, page_size=2)
    assert [item["group_key"] for item in first["items"]] == ["2026-10", "2026-11"]
    assert [item["group_key"] for item in second["items"]] == ["2026-12"]
    assert first["totals"] == second["totals"]
    assert first["totals"]["event_count"] == 3
    assert first["totals"]["prepaid"]["billable_cost"] == fixed("6")


def test_the_gross_profit_deducts_the_known_gateway_fees(billed, fees) -> None:
    app, world = billed
    tenant, template = priced_template(world)
    insert_processed(
        world,
        template,
        tenant,
        occurred_at=IN_PERIOD,
        billable=Decimal("10"),
        estimated=Decimal("6"),
    )
    fees(tenant, "1.50")
    fees(tenant, None)
    totals = analytics(app, headers(app, domain=TEST_EMAIL_DOMAIN))["totals"]
    assert totals["prepaid"]["billable_cost"] == fixed("10")
    assert totals["prepaid"]["estimated_provider_cost_myr"] == fixed("6")
    assert totals["prepaid"]["gross_margin"] == fixed("4")
    assert totals["payments"] == {
        "period_basis": "paid_at",
        "paid_count": 2,
        "topup_amount": "200.00000000",
        "gateway_fee": "1.50000000",
        "gateway_fee_unknown_count": 1,
    }
    assert (totals["gross_profit"], totals["gross_profit_rate"]) == ("2.50000000", "0.25")


def test_fees_cannot_be_attributed_to_project_provider_model_or_feature(billed, fees) -> None:
    app, world = billed
    mine, template = priced_template(world)
    other = world.tenant("other")
    world.fund(other, "1000")
    insert_processed(
        world, template, mine, occurred_at=IN_PERIOD, billable=Decimal("10"), estimated=Decimal("6")
    )
    insert_processed(
        world, template, other, occurred_at=IN_PERIOD, billable=Decimal("4"), estimated=Decimal("1")
    )
    fees(mine, "1.50")
    fees(other, "0.25", amount="50.00")
    admin = headers(app, domain=TEST_EMAIL_DOMAIN)
    for filters, events, billable in (
        ({"project_id": mine.project_public_id}, 1, "10"),
        ({"provider": "anthropic"}, 2, "14"),
        ({"model": "claude-x"}, 2, "14"),
        ({"feature": "crm.chat"}, 2, "14"),
        # 参数出现即算：空串不匹配任何事件，手续费同样不可归属。
        ({"provider": ""}, 0, "0"),
    ):
        totals = analytics(app, admin, **filters)["totals"]
        assert (totals["payments"], totals["gross_profit"], totals["gross_profit_rate"]) == (
            None,
            None,
            None,
        ), filters
        # 用量合计照常。
        assert totals["event_count"] == events, filters
        assert totals["prepaid"]["billable_cost"] == fixed(billable), filters

    everyone = analytics(app, admin)["totals"]
    assert (everyone["payments"]["paid_count"], everyone["payments"]["gateway_fee"]) == (
        2,
        "1.75000000",
    )
    assert everyone["gross_profit"] == fixed("5.25")
    only_mine = analytics(app, admin, customer_id=mine.public_id)["totals"]
    assert only_mine["event_count"] == 1
    assert only_mine["payments"] == {
        "period_basis": "paid_at",
        "paid_count": 1,
        "topup_amount": "100.00000000",
        "gateway_fee": "1.50000000",
        "gateway_fee_unknown_count": 0,
    }
    assert (only_mine["gross_profit"], only_mine["gross_profit_rate"]) == ("2.50000000", "0.25")
    nobody = analytics(app, admin, customer_id=UNKNOWN_ID)
    assert (nobody["total"], nobody["items"], nobody["totals"]["event_count"]) == (0, [], 0)
    assert nobody["totals"]["payments"] == {
        "period_basis": "paid_at",
        "paid_count": 0,
        "topup_amount": ZERO_MONEY,
        "gateway_fee": ZERO_MONEY,
        "gateway_fee_unknown_count": 0,
    }
    assert (nobody["totals"]["gross_profit"], nobody["totals"]["gross_profit_rate"]) == (
        ZERO_MONEY,
        None,
    )


def test_without_usage_revenue_the_gross_profit_is_minus_the_fees_and_has_no_rate(
    billed, fees
) -> None:
    app, world = billed
    tenant, template = priced_template(world)
    insert_processed(world, template, tenant, occurred_at=IN_PERIOD, internal=True)
    fees(tenant, "0.40")
    totals = analytics(app, headers(app, domain=TEST_EMAIL_DOMAIN))["totals"]
    assert (totals["prepaid"]["event_count"], totals["internal_metered_only"]["event_count"]) == (
        0,
        1,
    )
    assert totals["prepaid"]["billable_cost"] == ZERO_MONEY
    assert totals["prepaid"]["gross_margin_rate"] is None
    assert (totals["gross_profit"], totals["gross_profit_rate"]) == ("-0.40000000", None)


def test_amounts_keep_eight_places_and_the_rate_is_the_decimal_quotient(billed, fees) -> None:
    """SQLite 把 NUMERIC 存成浮点：余额在千位时连扣 `0.00000001` 过不了
    `ck_wallet_transactions_arithmetic`。照 test_customer_usage_summary_api.py 的做法，那里先按
    精确的二进制小数记账，再把事件的计费额直接改成 `0.00000001`（合计只读 `usage_events`）；
    MySQL 上照常记账。
    """
    app, world = billed
    tenant, template = priced_template(world)
    one = Decimal("0.00000001")
    exact = Decimal("0.5")
    on_mysql = world.factory.kw["bind"].dialect.name == "mysql"
    for _ in range(3):
        insert_processed(
            world,
            template,
            tenant,
            occurred_at=IN_PERIOD,
            billable=one if on_mysql else exact,
            estimated=Decimal(0),
        )
        fees(tenant, "0.01", amount="10.00")
    if not on_mysql:
        with world.factory() as session:
            changed = session.execute(
                update(UsageEvent)
                .where(
                    UsageEvent.tenant_id == tenant.id,
                    UsageEvent.occurred_at == IN_PERIOD,
                    UsageEvent.billable_cost == exact,
                )
                .values(billable_cost=one)
            )
            assert changed.rowcount == 3
            session.commit()
    totals = analytics(app, headers(app, domain=TEST_EMAIL_DOMAIN))["totals"]
    assert totals["prepaid"]["billable_cost"] == "0.00000003"
    assert totals["payments"]["gateway_fee"] == "0.03000000"
    assert totals["payments"]["topup_amount"] == "30.00000000"
    profit = 3 * one - Decimal("0.03")
    assert totals["gross_profit"] == "-0.02999997" == f"{profit:.8f}"
    assert totals["gross_profit_rate"] == ratio(profit / (3 * one))


@pytest.mark.parametrize(
    "params",
    [
        {"group_by": "week", **PERIOD_QUERY},
        # 367 天：比上限多一天。
        {
            "group_by": "day",
            "occurred_from": "2026-01-01T00:00:00Z",
            "occurred_to": "2027-01-03T00:00:00Z",
        },
    ],
)
def test_a_week_grouping_or_a_367_day_span_is_refused_after_authentication(
    app, client, params
) -> None:
    assert client.get(PATH, params=params).status_code == 401
    assert (
        client.get(PATH, params=params, headers=headers(app, UserRole.CUSTOMER)).status_code == 403
    )
    response = client.get(PATH, params=params, headers=headers(app))
    assert (response.status_code, response.json()["error"]["code"]) == (422, "VALIDATION_ERROR")


@pytest.mark.parametrize("group_by", ["day", "month"])
def test_days_and_months_are_valid_groupings(app, client, group_by: str) -> None:
    data = query(client, headers(app), group_by=group_by)
    assert (data["total"], data["items"], data["totals"]["event_count"]) == (0, [], 0)


# --- AIH-TASK-248：按比例分摊的对账后成本（设计闸门 #756 v2 §2、§7） ---
#
# 世界来自 tests/backend/provider_cost_world.py：
# 标准世界（E1–E6）加 `UNALLOCATABLE_RECORDS` 的 12 月记录
# （R = 51.48、实时 E = 46.80、R ÷ E = 1.1），`myr_three`，`unallocatable`。
# SQLite 与真 MySQL 各跑一遍；MySQL 那一半没设 `BILLING_TEST_DATABASE_URL` 时 skip，
# **skipped 不是 passed**，CI 设了它。金额都能精确表示，两边都逐位比较字符串。

DECEMBER_RECORD = UNALLOCATABLE_RECORDS[0]
DECEMBER = {"occurred_from": "2026-12-01T00:00:00Z", "occurred_to": "2027-01-01T00:00:00Z"}
THREE_MONTHS = {"occurred_from": "2026-11-01T00:00:00Z", "occurred_to": "2027-02-01T00:00:00Z"}
# 两块共有的四个新键与 prepaid 块独有的两个。
PROVIDER_COST_KEYS = (
    "covered_estimated_cost_myr",
    "reconciled_provider_cost_myr",
    "provider_cost_myr",
    "provider_cost_basis",
)
PREPAID_PROVIDER_COST_KEYS = (
    *PROVIDER_COST_KEYS,
    "gross_margin_on_provider_cost",
    "gross_margin_on_provider_cost_rate",
)
TOTALS_PROVIDER_COST_KEYS = (
    "gross_profit_on_provider_cost",
    "gross_profit_on_provider_cost_rate",
    "reconciliation_coverage",
    "unallocated_reconciled_months",
    "unallocated_reconciled_cost_myr",
)
COVERAGE_KEYS = {
    "reconciliation_id",
    "provider_code",
    "billing_month",
    "period_start",
    "period_end",
    "fully_in_range",
    "allocatable",
}


@pytest.fixture(params=["sqlite", "mysql"])
def cost_db(request, monkeypatch):
    """An empty database for a provider cost world: shared-connection SQLite, and a real MySQL."""
    if request.param == "mysql":
        yield from mysql_factory(monkeypatch)
        return
    yield from sqlite_shared_factory()


@pytest.fixture
def cost_app(tmp_path, cost_db):
    application = create_app(settings_for(tmp_path))
    application.state.engine = cost_db.kw["bind"]
    application.state.session_factory = cost_db
    return application


def cost_analytics(app, world: ProviderCostWorld, **params) -> dict:
    """Interface one as the world's administrator."""
    token = issue_access_token(
        app.state.settings,
        user_id=world.admin_id,
        role="ADMIN",
        session_id="0" * 32,
        now=utc_now(),
    )
    return query(TestClient(app), {"Authorization": f"Bearer {token}"}, **params)


def block(view: dict, name: str, keys: tuple[str, ...]) -> tuple:
    return tuple(view[name][key] for key in keys)


def by_group_name(data: dict) -> dict[str, dict]:
    return {item["group_name"]: item for item in data["items"]}


def by_group_key(data: dict) -> dict[str, dict]:
    return {item["group_key"]: item for item in data["items"]}


def coverage_of(spec, *, fully_in_range: bool = True, allocatable: bool = True) -> dict:
    """What `totals.reconciliation_coverage` lists for one record (times without `Z`)."""
    start = dt.datetime.strptime(spec.billing_month, "%Y-%m")
    end = dt.datetime(start.year + start.month // 12, start.month % 12 + 1, 1)
    return {
        "reconciliation_id": spec.public_id,
        "provider_code": PROVIDER_CODE,
        "billing_month": spec.billing_month,
        "period_start": start.isoformat(),
        "period_end": end.isoformat(),
        "fully_in_range": fully_in_range,
        "allocatable": allocatable,
    }


def without_provider_cost(data: dict) -> dict:
    """`data` with only the AIH-TASK-222 keys: every key AIH-TASK-248 added is dropped."""

    def strip(view: dict) -> dict:
        kept = {key: value for key, value in view.items() if key not in TOTALS_PROVIDER_COST_KEYS}
        kept["prepaid"] = {
            key: value
            for key, value in view["prepaid"].items()
            if key not in PREPAID_PROVIDER_COST_KEYS
        }
        kept["internal_metered_only"] = {
            key: value
            for key, value in view["internal_metered_only"].items()
            if key not in PROVIDER_COST_KEYS
        }
        return kept

    return {
        **{key: value for key, value in data.items() if key not in PROVIDER_COST_DATA_KEYS},
        "items": [strip(item) for item in data["items"]],
        "totals": strip(data["totals"]),
    }


def test_the_december_record_is_allocated_pro_rata_by_project_and_by_provider(cost_app, cost_db):
    """§7「分摊结果」：区间 [2026-12-01, 2027-01-01)。"""
    world = build_standard_world(cost_db)
    add_record(world, DECEMBER_RECORD)

    data = cost_analytics(cost_app, world, group_by="project", **DECEMBER)
    assert (
        data["cost_basis"],
        data["provider_cost_allocation"],
        data["reconciliation_period_basis"],
    ) == ("estimated", "pro_rata_estimated_cost", "utc_calendar_month")
    projects = by_group_name(data)
    assert set(projects) == {P_CRM, P_PRE, P_CHAT}
    crm, pre, chat = projects[P_CRM], projects[P_PRE], projects[P_CHAT]
    assert block(crm, "internal_metered_only", PROVIDER_COST_KEYS) == (
        "27.00000000",
        "29.70000000",
        "29.70000000",
        "reconciled",
    )
    assert block(crm, "prepaid", PREPAID_PROVIDER_COST_KEYS) == (
        "0.00000000",
        "0.00000000",
        "0.00000000",
        "estimated",
        "0.00000000",
        None,
    )
    assert block(pre, "prepaid", PREPAID_PROVIDER_COST_KEYS) == (
        "19.80000000",
        "21.78000000",
        "21.78000000",
        "reconciled",
        "-21.78000000",
        None,
    )
    assert block(chat, "prepaid", PREPAID_PROVIDER_COST_KEYS) == (
        "0.00000000",
        "0.00000000",
        "450.00000000",
        "estimated",
        "-450.00000000",
        None,
    )
    # 两行分摊之和 = R。
    allocated = Decimal(crm["internal_metered_only"]["reconciled_provider_cost_myr"]) + Decimal(
        pre["prepaid"]["reconciled_provider_cost_myr"]
    )
    assert allocated == Decimal(DECEMBER_RECORD.reconciled_cost_myr)

    totals = data["totals"]
    assert set(totals) == TOTALS_KEYS
    assert block(totals, "prepaid", PREPAID_PROVIDER_COST_KEYS) == (
        "19.80000000",
        "21.78000000",
        "471.78000000",
        "mixed",
        "-471.78000000",
        None,
    )
    assert block(totals, "internal_metered_only", PROVIDER_COST_KEYS) == (
        "27.00000000",
        "29.70000000",
        "29.70000000",
        "reconciled",
    )
    # 没有支付：手续费块全零，基于供应商成本的毛利 = 基于它的用量毛利。
    assert totals["payments"]["gateway_fee"] == ZERO_MONEY
    assert (
        totals["gross_profit_on_provider_cost"],
        totals["gross_profit_on_provider_cost_rate"],
    ) == (
        "-471.78000000",
        None,
    )
    assert totals["reconciliation_coverage"] == [coverage_of(DECEMBER_RECORD)]
    assert all(set(item) == COVERAGE_KEYS for item in totals["reconciliation_coverage"])
    assert (totals["unallocated_reconciled_months"], totals["unallocated_reconciled_cost_myr"]) == (
        [],
        None,
    )

    providers = cost_analytics(cost_app, world, group_by="provider", **DECEMBER)
    [anthropic] = providers["items"]
    assert anthropic["group_key"] == PROVIDER_CODE
    assert block(anthropic, "prepaid", PROVIDER_COST_KEYS) == (
        "19.80000000",
        "21.78000000",
        "471.78000000",
        "mixed",
    )
    assert block(anthropic, "internal_metered_only", PROVIDER_COST_KEYS) == (
        "27.00000000",
        "29.70000000",
        "29.70000000",
        "reconciled",
    )
    assert providers["totals"]["prepaid"]["provider_cost_myr"] == "471.78000000"
    assert providers["totals"]["internal_metered_only"]["provider_cost_myr"] == "29.70000000"


def test_malaysian_months_take_each_event_by_its_utc_month(cost_app, cost_db) -> None:
    """§7「马来西亚月分组」：区间 [2026-11-01, 2027-02-01)、`group_by=month`。"""
    world = build_standard_world(cost_db)
    add_record(world, DECEMBER_RECORD)

    months = by_group_key(cost_analytics(cost_app, world, group_by="month", **THREE_MONTHS))
    assert set(months) == {"2026-12", "2027-01"}
    january, december = months["2027-01"], months["2026-12"]
    # E2：UTC 12 月、马来西亚 1 月。
    assert block(january, "prepaid", PROVIDER_COST_KEYS) == (
        "19.80000000",
        "21.78000000",
        "21.78000000",
        "reconciled",
    )
    # E4 8.80 未覆盖。
    assert block(january, "internal_metered_only", PROVIDER_COST_KEYS) == (
        "0.00000000",
        "0.00000000",
        "8.80000000",
        "estimated",
    )
    # E1 覆盖（27.00 → 29.70）+ E3 未覆盖（UTC 11 月、马来西亚 12 月，4.50）。
    assert block(december, "internal_metered_only", PROVIDER_COST_KEYS) == (
        "27.00000000",
        "29.70000000",
        "34.20000000",
        "mixed",
    )
    # E5：集成方上报，不在范围内。
    assert block(december, "prepaid", PROVIDER_COST_KEYS) == (
        "0.00000000",
        "0.00000000",
        "450.00000000",
        "estimated",
    )


def test_a_partly_covered_record_allocates_only_the_events_in_the_period(cost_app, cost_db):
    """§7「部分覆盖」：区间 [2026-12-15, 2027-01-01)。

    只有 E2 在区间内；E 仍是整月的实时值。
    """
    world = build_standard_world(cost_db)
    add_record(world, DECEMBER_RECORD)

    totals = cost_analytics(
        cost_app,
        world,
        group_by="provider",
        occurred_from="2026-12-15T00:00:00Z",
        occurred_to="2027-01-01T00:00:00Z",
    )["totals"]
    assert totals["reconciliation_coverage"] == [coverage_of(DECEMBER_RECORD, fully_in_range=False)]
    assert block(totals, "prepaid", PROVIDER_COST_KEYS) == (
        "19.80000000",
        "21.78000000",
        "21.78000000",
        "reconciled",
    )
    assert block(totals, "internal_metered_only", PROVIDER_COST_KEYS) == (
        "0.00000000",
        "0.00000000",
        "0.00000000",
        "estimated",
    )


def test_each_group_is_rounded_once_and_the_totals_on_their_own(cost_app, cost_db) -> None:
    """§7「分摊舍入」：`myr_three` 加 12 月 MYR 1.00 记录；每组 1/3，合计单独算为 1。"""
    world = build_myr_three_world(cost_db)
    add_record(world, MYR_THREE_DECEMBER)

    data = cost_analytics(cost_app, world, group_by="project", **DECEMBER)
    projects = by_group_name(data)
    assert set(projects) == {Q1, Q2, Q3}
    for name, item in projects.items():
        assert block(item, "internal_metered_only", PROVIDER_COST_KEYS) == (
            "1.00000000",
            "0.33333333",
            "0.33333333",
            "reconciled",
        ), name
    assert block(data["totals"], "internal_metered_only", PROVIDER_COST_KEYS) == (
        "3.00000000",
        "1.00000000",
        "1.00000000",
        "reconciled",
    )
    # 文档写明的舍入误差：至多组数 × 0.5×10⁻⁸。
    grouped_sum = sum(
        (
            Decimal(item["internal_metered_only"]["reconciled_provider_cost_myr"])
            for item in projects.values()
        ),
        Decimal(0),
    )
    assert Decimal("1.00000000") - grouped_sum == Decimal("0.00000001")


def test_a_record_without_estimate_is_disclosed_but_never_allocated(cost_app, cost_db) -> None:
    """§7「不可分摊」：区间 [2026-11-01, 2027-04-01)。

    12 月可分摊；2 月 E = 0 而 R = 5.00，不可分摊；3 月 E = 0 且 R = 0，可分摊。
    """
    world = build_unallocatable_world(cost_db)
    december, february, march = UNALLOCATABLE_RECORDS

    data = cost_analytics(
        cost_app,
        world,
        group_by="provider",
        occurred_from="2026-11-01T00:00:00Z",
        occurred_to="2027-04-01T00:00:00Z",
    )
    totals = data["totals"]
    assert totals["reconciliation_coverage"] == [
        coverage_of(december),
        coverage_of(february, allocatable=False),
        coverage_of(march),
    ]
    assert (totals["unallocated_reconciled_months"], totals["unallocated_reconciled_cost_myr"]) == (
        [f"{PROVIDER_CODE}:2027-02"],
        "5.00000000",
    )
    # 2 月的 5.00 不进任何行：分摊额只有 12 月的 51.48。
    [anthropic] = data["items"]
    for view in (anthropic, totals):
        assert block(view, "prepaid", PREPAID_PROVIDER_COST_KEYS) == (
            "19.80000000",
            "21.78000000",
            "471.78000000",
            "mixed",
            "-471.78000000",
            None,
        )
        # E1 覆盖 27.00 → 29.70，E3 4.50 与 E4 8.80 未覆盖。
        assert block(view, "internal_metered_only", PROVIDER_COST_KEYS) == (
            "27.00000000",
            "29.70000000",
            "43.00000000",
            "mixed",
        )
    assert totals["gross_profit_on_provider_cost"] == "-471.78000000"


def test_without_records_the_provider_cost_is_the_estimate_and_old_keys_keep_their_values(
    cost_app, cost_db
) -> None:
    """§7「未覆盖与旧键不变」：没有对账记录；之后加上 12 月记录。

    加记录前后，AIH-TASK-222 的键与值不变。
    """
    world = build_standard_world(cost_db)

    before = {
        group_by: cost_analytics(cost_app, world, group_by=group_by, **THREE_MONTHS)
        for group_by in ("project", "month")
    }
    for data in before.values():
        totals = data["totals"]
        for view in (*data["items"], totals):
            prepaid, internal = view["prepaid"], view["internal_metered_only"]
            for part in (prepaid, internal):
                assert (
                    part["covered_estimated_cost_myr"],
                    part["reconciled_provider_cost_myr"],
                    part["provider_cost_basis"],
                ) == (ZERO_MONEY, ZERO_MONEY, "estimated")
                assert part["provider_cost_myr"] == part["estimated_provider_cost_myr"]
            assert prepaid["gross_margin_on_provider_cost"] == prepaid["gross_margin"]
            assert prepaid["gross_margin_on_provider_cost_rate"] == prepaid["gross_margin_rate"]
        assert (
            totals["reconciliation_coverage"],
            totals["unallocated_reconciled_months"],
            totals["unallocated_reconciled_cost_myr"],
        ) == ([], [], None)
        assert totals["gross_profit_on_provider_cost"] == totals["gross_profit"]
        assert totals["gross_profit_on_provider_cost_rate"] == totals["gross_profit_rate"]
    # AIH-TASK-222 的值：估算照标准世界。
    projects = by_group_name(before["project"])
    assert projects[P_CRM]["internal_metered_only"]["estimated_provider_cost_myr"] == (
        "40.30000000"
    )
    assert projects[P_CRM]["internal_metered_only"]["reference_customer_price"] == "80.60000000"
    assert projects[P_PRE]["prepaid"]["estimated_provider_cost_myr"] == "19.80000000"
    assert projects[P_CHAT]["prepaid"]["estimated_provider_cost_myr"] == "450.00000000"

    add_record(world, DECEMBER_RECORD)
    for group_by, data in before.items():
        after = cost_analytics(cost_app, world, group_by=group_by, **THREE_MONTHS)
        assert after["totals"]["reconciliation_coverage"] == [coverage_of(DECEMBER_RECORD)]
        assert without_provider_cost(after) == without_provider_cost(data), group_by
        assert set(without_provider_cost(after)) == TASK_073_KEYS | {"usage_period_basis", "totals"}

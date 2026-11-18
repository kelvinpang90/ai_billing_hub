"""End-to-end coverage for the read-only admin usage analytics endpoint."""

from __future__ import annotations

import base64
import datetime as dt
import itertools
import os
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, insert, select, update
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
from test_usage_billing import World as BillingWorld

from app.core.config import Settings
from app.core.database import create_session_factory
from app.core.tokens import issue_access_token
from app.main import create_app
from app.models.ai_catalog import AiModel, AiProvider, PayloadShape, QuantityKind, UsageMeterType
from app.models.auth import User, UserRole, UserStatus
from app.models.base import Base
from app.models.tenancy import BillingMode, Project, Tenant
from app.models.usage import UsageEvent, UsageEventStatus
from app.repositories import usage_events as event_repository
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
    }
    assert item["internal_metered_only"] == {
        "event_count": 1,
        "estimated_provider_cost_myr": "0.12500000",
        "reference_customer_price": "0.50000000",
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
) -> None:
    """A PROCESSED event inserted directly, with the billed template's snapshot but 0 billed (so
    it needs no ledger row); usage_events has only BEFORE UPDATE triggers. `quantity` makes it an
    AUDIO_SECOND (QUANTITY) event."""
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
    with world.factory() as session:
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

"""End-to-end coverage for the read-only admin usage analytics endpoint."""

from __future__ import annotations

import base64
import datetime as dt
import os
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool
from test_usage_billing import (
    _SQLITE_LEDGER_TRIGGER,
    EVENT_AT,
    TEST_EMAIL_DOMAIN,
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
from app.repositories.usage_analytics import list_usage_analytics
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

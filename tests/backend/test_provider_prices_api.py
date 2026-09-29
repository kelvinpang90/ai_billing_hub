"""Provider price endpoints end to end on SQLite (design gate #177 v3 §7).

这里测接口契约：正常路径与每一步的审计、完整性、目录停用、精度、生效时刻的格式与不许回溯、
状态机的 409、404、封闭的请求体、列表的筛选与排序、响应里没有内部 id，以及「每个处理函数
第一条语句是 `require_admin`」（AST）。

依赖冻结时钟的区间语义、触发器、事务中途失败在 test_provider_prices_service.py；取价、性质用例
与并发在 test_provider_prices_resolve.py。匿名 401 / CUSTOMER 403 且不写库在
test_admin_customers_api.py（从路由表枚举全部管理端路由）。

价格一律是明显的虚构值（公开仓库，不写任何真实的供应商价格）。
"""

from __future__ import annotations

import ast
import base64
import datetime as dt
import json
import os
import pathlib
import uuid
from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.config import Settings
from app.core.database import create_session_factory
from app.core.tokens import issue_access_token
from app.main import create_app
from app.models.ai_catalog import SEED_METER_TYPES, UsageMeterComponent, UsageMeterType
from app.models.auth import AuditAction, AuditLog, User, UserRole, UserStatus
from app.models.base import Base
from app.models.provider_prices import ProviderPriceComponent, ProviderPriceVersion
from app.services.auth import utc_now

PRICES = "/api/v1/admin/provider-prices"
PROVIDERS = "/api/v1/admin/ai-providers"
METER_TYPES = "/api/v1/admin/usage-meter-types"
ROUTER_FILE = pathlib.Path("app/api/admin_provider_prices.py")

# 响应字段白名单（设计 §2）。断言「恰好等于」：多一个键（内部 id）就红。
VERSION_FIELDS = {
    "id",
    "provider_id",
    "provider_code",
    "model_id",
    "model_code",
    "source_currency",
    "source_type",
    "source_reference",
    "status",
    "effective_from",
    "effective_to",
    "components",
    "created_by_email",
    "approved_by_email",
    "created_at",
    "updated_at",
    "approved_at",
}
COMPONENT_FIELDS = {
    "component_code",
    "meter_type_code",
    "unit",
    "unit_quantity",
    "rate_amount",
    "metadata",
    "created_at",
}
PAGE_FIELDS = {"items", "page", "page_size", "total"}
ENVELOPE_FIELDS = {"success", "data", "error", "request_id"}

# 全零占位值（secret-scan）。
ZERO_ID = "00000000-0000-4000-8000-000000000000"

# 明显的虚构价格：每 1 000 000 个 token。
FICTIONAL_TOKEN_RATES = {
    "LLM_INPUT_TOKEN": "1.11111111",
    "LLM_OUTPUT_TOKEN": "2.22222222",
    "LLM_CACHE_WRITE_TOKEN": "3.33333333",
    "LLM_CACHE_READ_TOKEN": "4.44444444",
}


# --- 夹具与帮手 -----------------------------------------------------------------


def settings_for(tmp_path) -> Settings:
    key = tmp_path / "jwt.key"
    key.write_text("a-test-signing-key-that-is-long-enough", encoding="utf-8")
    master = tmp_path / "master.key"
    master.write_text(f"1:{base64.b64encode(os.urandom(32)).decode()}\n", encoding="utf-8")
    return Settings(jwt_secret_file=str(key), master_key_file=str(master), database_url="")


def seed_meter_types(factory: sessionmaker[Session]) -> None:
    """What migration 0012 seeds, for the SQLite tables `create_all` builds empty."""
    now = utc_now().replace(microsecond=0)
    with factory() as session:
        for seed in SEED_METER_TYPES:
            kind = UsageMeterType(
                public_id=str(uuid.uuid4()),
                code=seed.code,
                display_name=seed.display_name,
                payload_shape=seed.payload_shape,
                unit=seed.unit,
                quantity_kind=seed.quantity_kind,
                created_at=now,
                updated_at=now,
            )
            session.add(kind)
            session.flush()
            for part in seed.components:
                session.add(
                    UsageMeterComponent(
                        meter_type_id=kind.id,
                        payload_shape=seed.payload_shape,
                        component_code=part.component_code,
                        quantity_field=part.quantity_field,
                        created_at=now,
                    )
                )
        session.commit()


@pytest.fixture
def app(tmp_path) -> Iterator[FastAPI]:
    application = create_app(settings_for(tmp_path))
    # StaticPool：TestClient 在线程池里跑同步端点，内存库必须所有连接共用一个。
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    application.state.engine = engine
    application.state.session_factory = create_session_factory(engine)
    seed_meter_types(application.state.session_factory)
    yield application
    engine.dispose()


@pytest.fixture
def client(app) -> TestClient:
    return TestClient(app)


@pytest.fixture
def admin_user(app) -> tuple[int, str]:
    """(internal id, email) of a committed ADMIN."""
    now = utc_now()
    email = f"{uuid.uuid4().hex}@example.com"
    with app.state.session_factory() as session:
        user = User(
            email=email,
            password_hash="not-a-real-hash",
            role=UserRole.ADMIN,
            status=UserStatus.ACTIVE,
            created_at=now,
            updated_at=now,
        )
        session.add(user)
        session.commit()
        return int(user.id), email


@pytest.fixture
def admin(app, admin_user) -> dict[str, str]:
    token = issue_access_token(
        app.state.settings,
        user_id=admin_user[0],
        role="ADMIN",
        session_id=uuid.uuid4().hex,
        now=utc_now(),
    )
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def catalog(client, admin) -> dict[str, str]:
    """A provider with two models, and another provider with one (public ids)."""
    provider = new_provider(client, admin, "anthropic")
    other = new_provider(client, admin, "openai")
    return {
        "provider": provider["id"],
        "model": new_model(client, admin, provider["id"], "claude-x")["id"],
        "second_model": new_model(client, admin, provider["id"], "claude-y")["id"],
        "other_provider": other["id"],
        "other_model": new_model(client, admin, other["id"], "gpt-x")["id"],
    }


def factory_of(application: FastAPI) -> sessionmaker[Session]:
    return application.state.session_factory


def error_code(response) -> str:
    body = response.json()
    assert set(body) == ENVELOPE_FIELDS
    assert body["success"] is False
    assert body["data"] is None
    return body["error"]["code"]


def outcomes(responses) -> list[tuple[int, str]]:
    return [(response.status_code, error_code(response)) for response in responses]


def snapshot(application: FastAPI) -> dict[str, object]:
    """Every price row a write may change, as committed, plus the audit count."""
    versions = select(
        ProviderPriceVersion.public_id,
        ProviderPriceVersion.status,
        ProviderPriceVersion.source_currency,
        ProviderPriceVersion.source_reference,
        ProviderPriceVersion.effective_from,
        ProviderPriceVersion.effective_to,
        ProviderPriceVersion.updated_at,
    ).order_by(ProviderPriceVersion.id)
    components = select(
        ProviderPriceComponent.provider_price_version_id,
        ProviderPriceComponent.usage_meter_component_id,
        ProviderPriceComponent.unit_quantity,
        ProviderPriceComponent.rate_amount,
        ProviderPriceComponent.metadata_json,
    ).order_by(ProviderPriceComponent.id)
    with factory_of(application)() as session:
        return {
            "versions": [tuple(row) for row in session.execute(versions)],
            "components": [tuple(row) for row in session.execute(components)],
            "audits": session.execute(select(func.count()).select_from(AuditLog)).scalar_one(),
        }


def audits(application: FastAPI, action: AuditAction) -> list[AuditLog]:
    statement = select(AuditLog).where(AuditLog.action == action).order_by(AuditLog.id)
    with factory_of(application)() as session:
        return list(session.execute(statement).scalars())


def states(audit: AuditLog) -> tuple[object, object]:
    before = json.loads(audit.before_state) if audit.before_state else None
    after = json.loads(audit.after_state) if audit.after_state else None
    return before, after


def new_provider(client: TestClient, headers: dict[str, str], code: str) -> dict:
    response = client.post(PROVIDERS, json={"code": code, "display_name": code}, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()["data"]


def new_model(client: TestClient, headers: dict[str, str], provider_id: str, code: str) -> dict:
    response = client.post(
        f"{PROVIDERS}/{provider_id}/models",
        json={"code": code, "display_name": code},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]


def token_components(**overrides: str) -> list[dict[str, object]]:
    rates = {**FICTIONAL_TOKEN_RATES, **overrides}
    return [
        {"component_code": code, "unit_quantity": "1000000", "rate_amount": rate}
        for code, rate in rates.items()
    ]


def draft_body(catalog: dict[str, str], **fields: object) -> dict[str, object]:
    return {
        "provider_id": catalog["provider"],
        "model_id": catalog["model"],
        "source_currency": "USD",
        "source_reference": "Fictional price sheet, viewed 2026-09-29",
        "components": token_components(),
        **fields,
    }


def create(client: TestClient, headers: dict[str, str], body: dict[str, object]):
    return client.post(PRICES, json=body, headers=headers)


def new_draft(client: TestClient, headers: dict[str, str], catalog: dict, **fields) -> dict:
    response = create(client, headers, draft_body(catalog, **fields))
    assert response.status_code == 201, response.text
    return response.json()["data"]


def act(client: TestClient, headers: dict[str, str], version_id: str, action: str, body=None):
    """POST …/{id}/publish | retire | discard."""
    payload = {} if body is None else body
    return client.post(f"{PRICES}/{version_id}/{action}", json=payload, headers=headers)


def published(client: TestClient, headers: dict[str, str], catalog: dict, **fields) -> dict:
    draft = new_draft(client, headers, catalog, **fields)
    response = act(client, headers, draft["id"], "publish")
    assert response.status_code == 200, response.text
    return response.json()["data"]


def moment(text: str | None) -> dt.datetime | None:
    return dt.datetime.fromisoformat(text) if text is not None else None


# --- 正常路径（设计 §7「正常路径」） -------------------------------------------------


def test_draft_edit_publish_supersede_and_retire(client, app, admin, admin_user, catalog) -> None:
    """建草稿 → 改 → 发布 → 再建草稿 → 发布（截断前一个）→ 退役；各步状态、区间、审计正确。"""
    headers = {**admin, "User-Agent": "admin-console-test"}
    _, email = admin_user
    started = utc_now()

    created = create(client, headers, draft_body(catalog))
    assert created.status_code == 201
    first = created.json()["data"]
    assert set(first) == VERSION_FIELDS
    assert str(uuid.UUID(first["id"])) == first["id"]
    assert (first["status"], first["source_type"], first["source_currency"]) == (
        "DRAFT",
        "MANUAL",
        "USD",
    )
    assert (first["effective_from"], first["effective_to"], first["approved_at"]) == (
        None,
        None,
        None,
    )
    assert (first["created_by_email"], first["approved_by_email"]) == (email, None)
    assert (first["provider_code"], first["model_code"]) == ("anthropic", "claude-x")
    codes = [item["component_code"] for item in first["components"]]
    assert codes == sorted(FICTIONAL_TOKEN_RATES)
    for item in first["components"]:
        assert set(item) == COMPONENT_FIELDS
        assert (item["meter_type_code"], item["unit"]) == ("LLM_TOKEN", "TOKEN")
        assert item["unit_quantity"] == "1000000.00000000"
        assert item["rate_amount"] == FICTIONAL_TOKEN_RATES[item["component_code"]]

    edited = client.patch(
        f"{PRICES}/{first['id']}",
        json={
            "source_reference": "  Corrected fictional sheet  ",
            "components": token_components(LLM_INPUT_TOKEN="1.5"),
        },
        headers=headers,
    )
    assert edited.status_code == 200, edited.text
    edited_view = edited.json()["data"]
    assert edited_view["source_reference"] == "Corrected fictional sheet"
    rates = {item["component_code"]: item["rate_amount"] for item in edited_view["components"]}
    assert rates["LLM_INPUT_TOKEN"] == "1.50000000"

    publishing = act(client, headers, first["id"], "publish")
    assert publishing.status_code == 200
    first = publishing.json()["data"]
    # 第一个版本、未指定时刻：一直以来。
    assert (first["status"], first["effective_from"], first["effective_to"]) == (
        "PUBLISHED",
        None,
        None,
    )
    assert first["approved_by_email"] == email
    assert moment(first["approved_at"]) is not None

    second = published(client, headers, catalog, source_currency="EUR")
    boundary = moment(second["effective_from"])
    assert boundary is not None
    assert boundary > started
    assert boundary.microsecond == 0
    first = client.get(f"{PRICES}/{first['id']}", headers=headers).json()["data"]
    assert first["effective_to"] == second["effective_from"]
    assert first["status"] == "PUBLISHED"

    retiring = act(client, headers, second["id"], "retire", {"reason": "  Fictional reason  "})
    assert retiring.status_code == 200, retiring.text
    retired = retiring.json()["data"]
    assert retired["status"] == "RETIRED"
    ended = moment(retired["effective_to"])
    assert ended is not None
    assert ended >= boundary

    [create_first, create_second] = audits(app, AuditAction.PROVIDER_PRICE_CREATE)
    assert create_first.entity_id == first["id"]
    assert states(create_first) == (
        None,
        {
            "provider_code": "anthropic",
            "model_code": "claude-x",
            "source_currency": "USD",
            "source_type": "MANUAL",
            "source_reference": "Fictional price sheet, viewed 2026-09-29",
            "status": "DRAFT",
            "components": [
                {
                    "component_code": code,
                    "unit_quantity": "1000000.00000000",
                    "rate_amount": FICTIONAL_TOKEN_RATES[code],
                }
                for code in sorted(FICTIONAL_TOKEN_RATES)
            ],
        },
    )
    assert states(create_second)[1]["source_currency"] == "EUR"
    [update] = audits(app, AuditAction.PROVIDER_PRICE_UPDATE)
    before, after = states(update)
    assert before["source_reference"] == "Fictional price sheet, viewed 2026-09-29"
    assert after["source_reference"] == "Corrected fictional sheet"
    rates_before = {row["component_code"]: row["rate_amount"] for row in before["components"]}
    rates_after = {row["component_code"]: row["rate_amount"] for row in after["components"]}
    assert rates_before == FICTIONAL_TOKEN_RATES
    assert rates_after == {**FICTIONAL_TOKEN_RATES, "LLM_INPUT_TOKEN": "1.50000000"}
    [publish_first, publish_second] = audits(app, AuditAction.PROVIDER_PRICE_PUBLISH)
    assert states(publish_first) == (
        {"status": "DRAFT"},
        {
            "status": "PUBLISHED",
            "effective_from": None,
            "effective_to": None,
            "approved_by_email": email,
            "approved_at": first["approved_at"],
        },
    )
    before, after = states(publish_second)
    assert before == {
        "status": "DRAFT",
        "truncated_version": {"id": first["id"], "effective_to": None},
    }
    assert after["truncated_version"] == {"id": first["id"], "effective_to": boundary.isoformat()}
    [retire] = audits(app, AuditAction.PROVIDER_PRICE_RETIRE)
    assert retire.reason == "Fictional reason"
    before, after = states(retire)
    assert (before["status"], after["status"]) == ("PUBLISHED", "RETIRED")
    assert after["effective_to"] == ended.isoformat()
    for audit in [create_first, create_second, update, publish_first, publish_second, retire]:
        assert (audit.actor_role, audit.user_agent) == ("ADMIN", "admin-console-test")
        assert audit.entity_type == "provider_price_version"


def test_discarding_a_draft_keeps_the_row(client, app, admin, catalog) -> None:
    draft = new_draft(client, admin, catalog)

    response = act(client, admin, draft["id"], "discard")

    assert response.status_code == 200
    assert response.json()["data"]["status"] == "DISCARDED"
    assert client.get(f"{PRICES}/{draft['id']}", headers=admin).status_code == 200
    [audit] = audits(app, AuditAction.PROVIDER_PRICE_DISCARD)
    assert audit.entity_id == draft["id"]
    assert states(audit) == ({"status": "DRAFT"}, {"status": "DISCARDED"})


def test_publishing_again_writes_nothing(client, app, admin, catalog) -> None:
    """设计 §2：已发布再发布 200，不写（幂等）。"""
    version = published(client, admin, catalog)
    before = snapshot(app)
    reserve = {"effective_from": "2999-01-01T00:00:00Z"}

    again = act(client, admin, version["id"], "publish")
    later = act(client, admin, version["id"], "publish", reserve)

    assert (again.status_code, later.status_code) == (200, 200)
    assert again.json()["data"] == version
    assert later.json()["data"] == version
    assert snapshot(app) == before


def test_an_edit_that_changes_nothing_writes_nothing(client, app, admin, catalog) -> None:
    draft = new_draft(client, admin, catalog)
    before = snapshot(app)
    # 同一组分量：换个顺序，数量换个等值的写法。
    same_set = [{**row, "unit_quantity": "1000000.0"} for row in reversed(token_components())]

    same = client.patch(
        f"{PRICES}/{draft['id']}",
        json={
            "source_currency": "USD",
            "source_reference": draft["source_reference"],
            "components": same_set,
        },
        headers=admin,
    )

    assert same.status_code == 200, same.text
    assert same.json()["data"] == draft
    assert snapshot(app) == before


# --- 生效时刻 -----------------------------------------------------------------------


def test_a_reservation_is_taken_in_any_zone_and_stored_in_utc(client, admin, catalog) -> None:
    """RFC 3339 带时区；换算成 UTC 存。前一个版本截断于它。"""
    first = published(client, admin, catalog)
    draft = new_draft(client, admin, catalog)
    in_kuala_lumpur = {"effective_from": "2999-01-01T08:00:00+08:00"}

    response = act(client, admin, draft["id"], "publish", in_kuala_lumpur)

    assert response.status_code == 200, response.text
    assert response.json()["data"]["effective_from"] == "2999-01-01T00:00:00"
    stored = client.get(f"{PRICES}/{first['id']}", headers=admin).json()["data"]
    assert stored["effective_to"] == "2999-01-01T00:00:00"


@pytest.mark.parametrize(
    ("effective_from", "expected"),
    [
        ("2999-01-01T00:00:00", (422, "VALIDATION_ERROR")),
        ("2999-01-01T00:00:00.5Z", (422, "VALIDATION_ERROR")),
        ("2999-01-01T00:00:00.000001Z", (422, "VALIDATION_ERROR")),
        ("2999-01-01 00:00:00Z", (422, "VALIDATION_ERROR")),
        ("2999-02-30T00:00:00Z", (422, "VALIDATION_ERROR")),
        ("2999-01-01", (422, "VALIDATION_ERROR")),
        (1_000_000_000, (422, "VALIDATION_ERROR")),
        ("2000-01-01T00:00:00Z", (422, "EFFECTIVE_FROM_IN_PAST")),
        ("2026-01-01T00:00:00+08:00", (422, "EFFECTIVE_FROM_IN_PAST")),
    ],
    ids=[
        "no-zone",
        "fraction",
        "microsecond",
        "space",
        "no-such-day",
        "date-only",
        "number",
        "long-ago",
        "past-with-offset",
    ],
)
def test_effective_from_rules(client, app, admin, catalog, effective_from, expected) -> None:
    """格式、整秒、带时区（422）；早于 `t` 是 422 `EFFECTIVE_FROM_IN_PAST`。都不写。"""
    draft = new_draft(client, admin, catalog)
    before = snapshot(app)

    response = act(client, admin, draft["id"], "publish", {"effective_from": effective_from})

    assert (response.status_code, error_code(response)) == expected
    assert snapshot(app) == before


def test_whole_seconds_with_zero_fractions_are_accepted(client, admin, catalog) -> None:
    draft = new_draft(client, admin, catalog)
    zero_fraction = {"effective_from": "2999-01-01T00:00:00.000Z"}

    response = act(client, admin, draft["id"], "publish", zero_fraction)

    assert response.status_code == 200, response.text
    assert response.json()["data"]["effective_from"] == "2999-01-01T00:00:00"


def test_a_start_not_after_the_latest_is_a_conflict(client, app, admin, catalog) -> None:
    """设计 §7「不许回溯」：请求的 F ≤ 末尾版本的起点是 409 `EFFECTIVE_FROM_CONFLICT`。

    不指定时刻、末尾是尚未开始的预约那一格，设计 v3 没有合法结果（500），在
    test_provider_prices_service.py 的缺口用例里。
    """
    published(client, admin, catalog)
    reserved = new_draft(client, admin, catalog)
    act(client, admin, reserved["id"], "publish", {"effective_from": "2999-01-02T00:00:00Z"})
    draft = new_draft(client, admin, catalog)
    before = snapshot(app)

    responses = [
        act(client, admin, draft["id"], "publish", {"effective_from": "2999-01-02T00:00:00Z"}),
        act(client, admin, draft["id"], "publish", {"effective_from": "2999-01-01T00:00:00Z"}),
    ]

    assert outcomes(responses) == [(409, "EFFECTIVE_FROM_CONFLICT")] * 2
    assert snapshot(app) == before


# --- 完整性、精度与分量 ---------------------------------------------------------------


def test_a_meter_type_must_be_priced_in_full(client, app, admin, catalog) -> None:
    """设计 §7「完整性」：只给 LLM_TOKEN 的三个分量，发布 409 并列出缺的那个；不写。"""
    three = token_components()[:3]
    draft = new_draft(client, admin, catalog, components=three)
    before = snapshot(app)

    response = act(client, admin, draft["id"], "publish")

    assert (response.status_code, error_code(response)) == (409, "PRICE_VERSION_INCOMPLETE")
    assert "LLM_CACHE_READ_TOKEN" in response.json()["error"]["message"]
    assert snapshot(app) == before


def test_meter_types_left_out_entirely_are_fine(client, admin, catalog) -> None:
    """整体没出现的计量类型不要求（那一类用量在计价时判 PRICING_ERROR）。"""
    embedding = {"component_code": "EMBEDDING_TOKEN", "unit_quantity": "1000", "rate_amount": "5"}
    only_embedding = published(client, admin, catalog, components=[embedding])
    everything = [*token_components(), embedding]
    both = published(
        client, admin, catalog, model_id=catalog["second_model"], components=everything
    )

    assert [item["component_code"] for item in only_embedding["components"]] == ["EMBEDDING_TOKEN"]
    assert only_embedding["components"][0]["meter_type_code"] == "EMBEDDING_TOKEN"
    assert len(both["components"]) == 5


@pytest.mark.parametrize(
    ("components", "expected"),
    [
        ([], (422, "VALIDATION_ERROR")),
        ([*token_components(), token_components()[0]], (422, "VALIDATION_ERROR")),
        (
            [{"component_code": "NO_SUCH_COMPONENT", "unit_quantity": "1", "rate_amount": "1"}],
            (404, "USAGE_METER_COMPONENT_NOT_FOUND"),
        ),
        (
            [{"component_code": "llm_input_token", "unit_quantity": "1", "rate_amount": "1"}],
            (404, "USAGE_METER_COMPONENT_NOT_FOUND"),
        ),
    ],
    ids=["none", "duplicate", "unknown", "wrong-case"],
)
def test_component_list_rules(client, app, admin, catalog, components, expected) -> None:
    """设计 §7「完整性」：零个分量、重复分量 422；不存在的分量 404。都不写。"""
    before = snapshot(app)

    created = create(client, admin, draft_body(catalog, components=components))

    assert (created.status_code, error_code(created)) == expected
    assert snapshot(app) == before
    draft = new_draft(client, admin, catalog)
    before = snapshot(app)
    edited = client.patch(f"{PRICES}/{draft['id']}", json={"components": components}, headers=admin)
    assert (edited.status_code, error_code(edited)) == expected
    assert snapshot(app) == before


@pytest.mark.parametrize(
    ("value", "stored"),
    [
        ("0.00000001", "0.00000001"),
        ("12.34567891", "12.34567891"),
        ("1234.5678", "1234.56780000"),
        ("7", "7.00000000"),
        ("1.123456789", None),
        ("0", None),
        ("0.00000000", None),
        ("-1", None),
        ("+1", None),
        ("1e-3", None),
        ("1E2", None),
        (" 1", None),
        ("1,5", None),
        ("1.", None),
        (".5", None),
        ("1000000000000", None),
        ("", None),
        ("NaN", None),
        ("١", None),
        (1, None),
        (0.5, None),
        (None, None),
    ],
)
@pytest.mark.parametrize("field", ["rate_amount", "unit_quantity"])
def test_amount_precision(client, app, admin, catalog, field: str, value, stored) -> None:
    """设计 §7「精度」：8 位小数通过、9 位 422；0、负数、指数写法、JSON 数字都是 422。"""
    components = token_components()
    components[0] = {**components[0], field: value}
    before = snapshot(app)

    response = create(client, admin, draft_body(catalog, components=components))

    if stored is None:
        assert (response.status_code, error_code(response)) == (422, "VALIDATION_ERROR")
        assert snapshot(app) == before
    else:
        assert response.status_code == 201, response.text
        by_code = {item["component_code"]: item for item in response.json()["data"]["components"]}
        assert by_code[components[0]["component_code"]][field] == stored


def test_metadata_is_kept_as_a_note(client, admin, catalog) -> None:
    components = token_components()
    components[0] = {**components[0], "metadata": {"tier": "fictional-tier"}}

    draft = new_draft(client, admin, catalog, components=components)

    by_code = {item["component_code"]: item for item in draft["components"]}
    assert by_code[components[0]["component_code"]]["metadata"] == {"tier": "fictional-tier"}
    assert by_code["LLM_OUTPUT_TOKEN"]["metadata"] is None


# --- 目录停用（设计 §7「目录停用」） --------------------------------------------------------


def _retire_catalog_item(client: TestClient, headers, catalog: dict, item: str) -> None:
    if item == "provider":
        url = f"{PROVIDERS}/{catalog['provider']}"
    elif item == "model":
        url = f"{PROVIDERS}/{catalog['provider']}/models/{catalog['model']}"
    else:
        listing = client.get(METER_TYPES, params={"page_size": 100}, headers=headers)
        [llm] = [row for row in listing.json()["data"]["items"] if row["code"] == "LLM_TOKEN"]
        url = f"{METER_TYPES}/{llm['id']}"
    response = client.patch(url, json={"status": "RETIRED"}, headers=headers)
    assert response.status_code == 200, response.text


@pytest.mark.parametrize("item", ["provider", "model", "meter_type"])
def test_retired_catalog_items_take_no_new_prices(client, app, admin, catalog, item: str) -> None:
    """供应商、模型或分量所属计量类型停用后：建草稿、改草稿、发布都是 409；不写。"""
    draft = new_draft(client, admin, catalog)
    _retire_catalog_item(client, admin, catalog, item)
    before = snapshot(app)

    responses = [
        create(client, admin, draft_body(catalog)),
        client.patch(f"{PRICES}/{draft['id']}", json={"source_reference": "Other"}, headers=admin),
        act(client, admin, draft["id"], "publish"),
    ]

    assert outcomes(responses) == [(409, "CATALOG_ITEM_RETIRED")] * 3
    assert snapshot(app) == before
    # 丢弃不看目录状态。
    assert act(client, admin, draft["id"], "discard").status_code == 200


# --- 状态机的 409 ------------------------------------------------------------------


def test_state_machine_refusals(client, app, admin, catalog) -> None:
    """已发布：编辑、丢弃 409 NOT_DRAFT；已退役 / 已丢弃：一切 409 FINAL；草稿、历史版本不能
    退役。都不写。
    """
    first = published(client, admin, catalog)
    current = published(client, admin, catalog)
    discarded = new_draft(client, admin, catalog)
    act(client, admin, discarded["id"], "discard")
    retired = published(client, admin, catalog, model_id=catalog["second_model"])
    act(client, admin, retired["id"], "retire", {"reason": "Fictional"})
    draft = new_draft(client, admin, catalog)
    before = snapshot(app)
    patch = {"source_reference": "Edited"}

    def patched(version_id: str):
        return client.patch(f"{PRICES}/{version_id}", json=patch, headers=admin)

    reason = {"reason": "Fictional"}
    expected = [
        (patched(current["id"]), (409, "PRICE_VERSION_NOT_DRAFT")),
        (act(client, admin, current["id"], "discard"), (409, "PRICE_VERSION_NOT_DRAFT")),
        (act(client, admin, first["id"], "retire", reason), (409, "PRICE_VERSION_NOT_RETIRABLE")),
        (act(client, admin, draft["id"], "retire", reason), (409, "PRICE_VERSION_NOT_RETIRABLE")),
    ]
    for final in (discarded, retired):
        expected += [
            (patched(final["id"]), (409, "PRICE_VERSION_FINAL")),
            (act(client, admin, final["id"], "discard"), (409, "PRICE_VERSION_FINAL")),
            (act(client, admin, final["id"], "publish"), (409, "PRICE_VERSION_FINAL")),
            (act(client, admin, final["id"], "retire", reason), (409, "PRICE_VERSION_FINAL")),
        ]

    actual = [(response.status_code, error_code(response)) for response, _ in expected]
    assert actual == [outcome for _, outcome in expected]
    assert snapshot(app) == before


# --- 404 --------------------------------------------------------------------------


@pytest.mark.parametrize("unknown", [ZERO_ID, "1", "claude-x"])
def test_an_unknown_version_is_404_everywhere(client, app, admin, catalog, unknown: str) -> None:
    """不存在的 public_id、内部自增 id、随手编的串：同一个 404，不写。"""
    new_draft(client, admin, catalog)
    before = snapshot(app)
    url = f"{PRICES}/{unknown}"

    responses = [
        client.get(url, headers=admin),
        client.patch(url, json={"source_reference": "Edited"}, headers=admin),
        act(client, admin, unknown, "publish"),
        act(client, admin, unknown, "retire", {"reason": "Fictional"}),
        act(client, admin, unknown, "discard"),
    ]

    assert set(outcomes(responses)) == {(404, "PRICE_VERSION_NOT_FOUND")}
    assert snapshot(app) == before


def test_the_model_must_belong_to_the_provider(client, app, admin, catalog) -> None:
    before = snapshot(app)

    responses = [
        create(client, admin, draft_body(catalog, provider_id=ZERO_ID)),
        create(client, admin, draft_body(catalog, model_id=ZERO_ID)),
        create(client, admin, draft_body(catalog, model_id=catalog["other_model"])),
        create(client, admin, draft_body(catalog, provider_id=catalog["other_provider"])),
    ]

    assert outcomes(responses) == [
        (404, "AI_PROVIDER_NOT_FOUND"),
        (404, "AI_MODEL_NOT_FOUND"),
        (404, "AI_MODEL_NOT_FOUND"),
        (404, "AI_MODEL_NOT_FOUND"),
    ]
    assert snapshot(app) == before


# --- 封闭的请求体 --------------------------------------------------------------------


@pytest.mark.parametrize(
    "overrides",
    [
        {"status": "PUBLISHED"},
        {"effective_from": "2999-01-01T00:00:00Z"},
        {"source_type": "SYNC"},
        {"id": ZERO_ID},
        {"approved_by": 1},
        {"source_currency": "usd"},
        {"source_currency": "US"},
        {"source_currency": "USDX"},
        {"source_currency": 840},
        {"source_reference": "   "},
        {"source_reference": "x" * 256},
        {"source_reference": None},
        {"components": None},
        {"components": [{**token_components()[0], "meter_type_code": "LLM_TOKEN"}]},
        {"components": [{"component_code": "LLM_INPUT_TOKEN", "rate_amount": "1"}]},
    ],
    ids=[
        "status",
        "effective_from",
        "source_type",
        "id",
        "approved_by",
        "currency-lowercase",
        "currency-short",
        "currency-long",
        "currency-number",
        "reference-blank",
        "reference-long",
        "reference-null",
        "components-null",
        "component-extra-field",
        "component-no-unit-quantity",
    ],
)
def test_the_create_body_is_closed(client, app, admin, catalog, overrides: dict) -> None:
    before = snapshot(app)

    response = create(client, admin, draft_body(catalog, **overrides))

    assert (response.status_code, error_code(response)) == (422, "VALIDATION_ERROR")
    assert snapshot(app) == before


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"provider_id": ZERO_ID},
        {"model_id": ZERO_ID},
        {"status": "DISCARDED"},
        {"source_currency": None},
        {"source_reference": None},
        {"components": None},
        {"source_reference": "Fine", "effective_from": "2999-01-01T00:00:00Z"},
    ],
    ids=[
        "empty",
        "provider_id",
        "model_id",
        "status",
        "currency-null",
        "reference-null",
        "components-null",
        "valid+effective_from",
    ],
)
def test_the_patch_body_is_closed(client, app, admin, catalog, body: dict) -> None:
    """一个版本挂在哪个模型上建后不可改；状态、区间只由发布与退役改。"""
    draft = new_draft(client, admin, catalog)
    before = snapshot(app)

    response = client.patch(f"{PRICES}/{draft['id']}", json=body, headers=admin)

    assert (response.status_code, error_code(response)) == (422, "VALIDATION_ERROR")
    assert snapshot(app) == before


@pytest.mark.parametrize(
    ("action", "body"),
    [
        ("publish", {"effective_to": "2999-01-01T00:00:00Z"}),
        ("publish", {"approved_by": 1}),
        ("retire", {}),
        ("retire", {"reason": "   "}),
        ("retire", {"reason": "x" * 256}),
        ("retire", {"reason": "Fine", "effective_to": "2999-01-01T00:00:00Z"}),
        ("discard", {"reason": "Fine"}),
    ],
    ids=[
        "publish-effective_to",
        "publish-approved_by",
        "retire-no-reason",
        "retire-blank-reason",
        "retire-long-reason",
        "retire-effective_to",
        "discard-reason",
    ],
)
def test_the_action_bodies_are_closed(client, app, admin, catalog, action: str, body: dict) -> None:
    version = published(client, admin, catalog)
    target = version if action == "retire" else new_draft(client, admin, catalog)
    before = snapshot(app)

    response = act(client, admin, target["id"], action, body)

    assert (response.status_code, error_code(response)) == (422, "VALIDATION_ERROR")
    assert snapshot(app) == before


def test_there_is_no_way_to_delete_a_version(client, app, admin, catalog) -> None:
    draft = new_draft(client, admin, catalog)
    before = snapshot(app)

    assert client.delete(f"{PRICES}/{draft['id']}", headers=admin).status_code == 405
    assert client.put(f"{PRICES}/{draft['id']}", json={}, headers=admin).status_code == 405
    assert snapshot(app) == before


# --- 列表 -----------------------------------------------------------------------


def test_listing_is_filtered_and_ordered(client, admin, catalog) -> None:
    """按供应商、模型、`effective_from` 排序；可按 `provider_id`、`model_id`、`status` 筛。"""
    first = published(client, admin, catalog)
    second = published(client, admin, catalog)
    draft = new_draft(client, admin, catalog)
    other_model = published(client, admin, catalog, model_id=catalog["second_model"])
    theirs = published(
        client,
        admin,
        catalog,
        provider_id=catalog["other_provider"],
        model_id=catalog["other_model"],
    )

    def listed(**params: object) -> list[str]:
        response = client.get(PRICES, params=params, headers=admin)
        assert response.status_code == 200, response.text
        page = response.json()["data"]
        assert set(page) == PAGE_FIELDS
        assert all(set(item) == VERSION_FIELDS for item in page["items"])
        return [item["id"] for item in page["items"]]

    # anthropic / claude-x：草稿与第一个版本的起点都是 NULL（按 id），再是第二个；然后 claude-y；
    # 最后 openai。
    assert listed() == [first["id"], draft["id"], second["id"], other_model["id"], theirs["id"]]
    assert listed(provider_id=catalog["other_provider"]) == [theirs["id"]]
    assert listed(model_id=catalog["second_model"]) == [other_model["id"]]
    both = {"provider_id": catalog["provider"], "model_id": catalog["model"]}
    assert listed(**both) == [first["id"], draft["id"], second["id"]]
    assert listed(status="DRAFT") == [draft["id"]]
    assert listed(status="PUBLISHED", page=2, page_size=2) == [other_model["id"], theirs["id"]]


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("status=BOGUS", (422, "VALIDATION_ERROR")),
        ("status=draft", (422, "VALIDATION_ERROR")),
        ("page=0", (422, "VALIDATION_ERROR")),
        ("page_size=101", (422, "VALIDATION_ERROR")),
        ("provider_id=" + "a" * 65, (422, "VALIDATION_ERROR")),
        (f"provider_id={ZERO_ID}", (404, "AI_PROVIDER_NOT_FOUND")),
        (f"model_id={ZERO_ID}", (404, "AI_MODEL_NOT_FOUND")),
    ],
)
def test_listing_bounds_and_unknown_filters(client, admin, catalog, query: str, expected) -> None:
    response = client.get(f"{PRICES}?{query}", headers=admin)

    assert (response.status_code, error_code(response)) == expected


def test_a_model_filter_of_another_provider_is_404(client, admin, catalog) -> None:
    params = {"provider_id": catalog["provider"], "model_id": catalog["other_model"]}

    response = client.get(PRICES, params=params, headers=admin)

    assert (response.status_code, error_code(response)) == (404, "AI_MODEL_NOT_FOUND")


# --- 响应字段与鉴权写法 ---------------------------------------------------------------


def test_no_response_carries_an_internal_id(client, admin, catalog) -> None:
    """设计 §6：对外的 id 都是 public_id；没有自增 id，也没有 `open_slot`、内部用户 id。"""
    version = published(client, admin, catalog)
    draft = new_draft(client, admin, catalog)
    bodies = [
        version,
        draft,
        client.get(f"{PRICES}/{version['id']}", headers=admin).json()["data"],
        *client.get(PRICES, headers=admin).json()["data"]["items"],
    ]

    for body in bodies:
        assert set(body) == VERSION_FIELDS
        for key in ("id", "provider_id", "model_id"):
            assert str(uuid.UUID(body[key])) == body[key], key
        text = json.dumps(body)
        for internal in ("open_slot", 'created_by"', 'approved_by"', "usage_meter_component_id"):
            assert internal not in text


def _handlers() -> list[ast.FunctionDef]:
    tree = ast.parse(ROUTER_FILE.read_text(encoding="utf-8"))
    return [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and any(
            isinstance(decorator, ast.Call)
            and isinstance(decorator.func, ast.Attribute)
            and isinstance(decorator.func.value, ast.Name)
            and decorator.func.value.id == "router"
            for decorator in node.decorator_list
        )
    ]


def _calls_require_admin(statement: ast.stmt) -> bool:
    value = statement.value if isinstance(statement, ast.Assign | ast.Expr) else None
    return (
        isinstance(value, ast.Call)
        and isinstance(value.func, ast.Name)
        and value.func.id == "require_admin"
        and [ast.unparse(arg) for arg in value.args] == ["request"]
    )


def test_every_handler_starts_with_require_admin() -> None:
    """设计 §2「接口」：第一条语句（文档字符串之后）就是 `require_admin(request)`。"""
    handlers = _handlers()
    # 七个接口；少了说明 AST 扫描落空了。
    assert len(handlers) == 7

    for handler in handlers:
        body = handler.body
        first = body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            body = body[1:]
        assert body, handler.name
        assert _calls_require_admin(body[0]), handler.name

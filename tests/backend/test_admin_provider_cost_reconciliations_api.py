"""Admin provider cost reconciliation end to end (AIH-TASK-245, gate #755 v1 §2「管理端接口」).

Rows of design §7 marked AIH-TASK-245 covered here (SQLite and a real MySQL):

- **接口契约** — preview, confirm, months and detail on the standard world and on `myr_three`:
  the keys exactly as design §2, amounts as 8-place strings, time fields as `FxRateView` writes
  them (no `Z`), the three FX keys null on a MYR record; the six bad `expected_*` values, a
  number for `source_total` and `expected_*` on a preview are 422 `VALIDATION_ERROR`; `/months`
  and `/preview` are not taken for an id;
- **总览** — now 2027-02-15 with December reconciled: 2027-01 `NOT_RECONCILED` then 2026-12
  `RECONCILED`; now 2026-12-20: an empty list; `openai` 404 `AI_PROVIDER_NOT_FOUND`; `Bad!` 422;
- **详情与 id** — after a December AI-module event lands: 200 with the five `live_estimate`
  keys and 3 events; an absent uuid, `not-a-uuid` and a confirmation superseding `not-a-uuid`
  are all 404 `PROVIDER_COST_RECONCILIATION_NOT_FOUND`, not 422;
- **鉴权** — anonymous 401 and CUSTOMER 403 on all four, probes with valid input, nothing written
  (the order of 401 / 403 against 422 is not asserted: FastAPI validates input first).

Every case pins `FIRST_RECONCILIATION_MONTH` to 2026-12 on the service module and the clock
through `app.api.admin_provider_cost_reconciliations.utc_now`. The MySQL half needs
`BILLING_TEST_DATABASE_URL`; it skips without it — **a skip is not a pass**, CI sets it.
"""

from __future__ import annotations

import datetime as dt
import re
import uuid
from collections.abc import Iterator
from typing import Any, Final

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from provider_cost_world import (
    P_CRM,
    TEST_EMAIL_DOMAIN,
    V44,
    World,
    build_myr_three_world,
    build_standard_world,
    mysql_factory,
    sqlite_shared_factory,
)
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.api import admin_provider_cost_reconciliations as api
from app.core.config import Settings
from app.core.tokens import issue_access_token
from app.main import create_app
from app.models.auth import AuditAction, AuditLog, User, UserRole, UserStatus
from app.models.fx_rates import FxRateVersion
from app.models.provider_cost_reconciliations import ProviderCostReconciliation
from app.services import provider_cost_reconciliation as reconciliation
from app.services.auth import utc_now

RECONCILIATIONS: Final = "/api/v1/admin/provider-cost-reconciliations"
MONTHS: Final = RECONCILIATIONS + "/months"
PREVIEW: Final = RECONCILIATIONS + "/preview"
USER_AGENT: Final = "admin-provider-costs-test"
ZERO_UUID: Final = "00000000-0000-0000-0000-000000000000"
# 一个已经结束了 12 月与 2027-01 的时刻。
NOW: Final = dt.datetime(2027, 2, 15, 0, 0, 0)
# 设计 §7「详情的估算变化」①：确认之后才落地的一条 12 月 AI 模块事件（1.00 USD / V45 / 4.50）。
LATE_EVENT: Final = "00000000000000000000000E07"

# 设计 §2 响应视图的键：断言「恰好等于」。
VIEW_KEYS: Final = {
    "reconciliation_id",
    "provider_code",
    "scope",
    "billing_month",
    "period_basis",
    "period_start",
    "period_end",
    "source_document_reference",
    "source_currency",
    "source_total",
    "fx_rate_version_id",
    "fx_rate_applied",
    "fx_rate_moment",
    "reconciled_cost_myr",
    "estimated_event_count",
    "estimated_source_cost",
    "estimated_cost_myr",
    "source_variance",
    "source_variance_rate",
    "myr_variance",
    "myr_variance_rate",
    "exceeds_threshold",
    "variance_threshold",
    "unprocessed_event_count",
    "outbox_unsettled_count",
    "outbox_discarded_count",
    "coverage_method",
    "supersedes_id",
    "reason",
    "is_current",
    "created_at",
    "created_by_email",
}
MONEY_KEYS: Final = (
    "source_total",
    "reconciled_cost_myr",
    "estimated_source_cost",
    "estimated_cost_myr",
    "source_variance",
    "myr_variance",
)
COUNT_KEYS: Final = (
    "estimated_event_count",
    "unprocessed_event_count",
    "outbox_unsettled_count",
    "outbox_discarded_count",
)
MONTH_KEYS: Final = {"billing_month", "period_start", "period_end", "status", "current"}
DETAIL_KEYS: Final = {
    "reconciliation",
    "chain",
    "live_estimate",
    "estimate_changed_since_confirmation",
}
LIVE_KEYS: Final = {
    "estimated_event_count",
    "estimated_source_cost",
    "estimated_cost_myr",
    "currency_mismatch_count",
    "unprocessed_event_count",
}
ERROR_KEYS: Final = {"code", "message"}
MONEY: Final = re.compile(r"-?[0-9]+\.[0-9]{8}")

# 设计 §2 请求体示例（标准世界的 12 月）。来源说明是虚构的。
PREVIEW_BODY: Final = {
    "provider_code": "anthropic",
    "billing_month": "2026-12",
    "source_currency": "USD",
    "source_total": "11.70",
    "source_document_reference": "Fictional statement, test workspace, 2026-12",
}
CONFIRM_BODY: Final = {
    **PREVIEW_BODY,
    "expected_event_count": 2,
    "expected_estimated_source_cost": "10.50000000",
}
# `myr_three`：三个 MYR 事件，各 1.00。
MYR_PREVIEW_BODY: Final = {
    **PREVIEW_BODY,
    "source_currency": "MYR",
    "source_total": "3.00",
}
MYR_CONFIRM_BODY: Final = {
    **MYR_PREVIEW_BODY,
    "expected_event_count": 3,
    "expected_estimated_source_cost": "3.00000000",
}


# --- fixtures ---------------------------------------------------------------------------


@pytest.fixture(params=["sqlite", "mysql"])
def db(request, monkeypatch) -> Iterator[sessionmaker[Session]]:
    """An empty database: SQLite shared across the TestClient's threads, and a real MySQL."""
    if request.param == "mysql":
        yield from mysql_factory(monkeypatch)
        return
    yield from sqlite_shared_factory()


@pytest.fixture(autouse=True)
def first_month(monkeypatch) -> None:
    """设计 §7：不依赖常量的默认值，只改常量的 PR 不会让这里失败。"""
    monkeypatch.setattr(reconciliation, "FIRST_RECONCILIATION_MONTH", "2026-12")


@pytest.fixture(autouse=True)
def clock(monkeypatch) -> list[dt.datetime]:
    """The endpoints' `now`; tests set `clock[0]`."""
    moment = [NOW]
    monkeypatch.setattr(api, "utc_now", lambda: moment[0])
    return moment


@pytest.fixture
def app(tmp_path, db) -> FastAPI:
    key = tmp_path / "jwt.key"
    key.write_text("a-test-signing-key-that-is-long-enough", encoding="utf-8")
    application = create_app(Settings(jwt_secret_file=str(key), database_url=""))
    application.state.session_factory = db
    return application


@pytest.fixture
def client(app) -> TestClient:
    # 500 要以统一错误体返回，而不是在测试里抛出来。
    return TestClient(app, raise_server_exceptions=False, headers={"User-Agent": USER_AGENT})


@pytest.fixture
def world(db) -> World:
    return build_standard_world(db)


@pytest.fixture
def myr_world(db) -> World:
    return build_myr_three_world(db)


# --- helpers ----------------------------------------------------------------------------


def token_for(application: FastAPI, user_id: int, role: str) -> dict[str, str]:
    """A signed access token. `role` is only the claim; the server reads the db."""
    token = issue_access_token(
        application.state.settings,
        user_id=user_id,
        role=role,
        session_id=uuid.uuid4().hex,
        now=utc_now(),
    )
    return {"Authorization": f"Bearer {token}"}


def admin_of(application: FastAPI, world: World) -> dict[str, str]:
    return token_for(application, world.admin_id, "ADMIN")


def customer_headers(application: FastAPI, world: World) -> dict[str, str]:
    with world.factory() as session:
        user = User(
            email=f"portal-{uuid.uuid4().hex}{TEST_EMAIL_DOMAIN}",
            password_hash="not-a-real-hash",
            role=UserRole.CUSTOMER,
            status=UserStatus.ACTIVE,
            created_at=NOW,
            updated_at=NOW,
        )
        session.add(user)
        session.commit()
        return token_for(application, int(user.id), "CUSTOMER")


def error_code(response) -> str | None:
    body = response.json()
    return (body.get("error") or {}).get("code")


def data(response, expected_status: int = 200) -> Any:
    assert response.status_code == expected_status, response.text
    return response.json()["data"]


def assert_error(response, status_code: int, code: str) -> None:
    assert (response.status_code, error_code(response)) == (status_code, code), response.text
    # spec §107 的信封：错误只有 code 与 message，不带 details。
    assert set(response.json()["error"]) == ERROR_KEYS


def written(world: World) -> tuple[int, int]:
    """(对账记录行数, 对账审计行数)；建世界时目录与汇率服务写的审计不算。"""
    with world.factory() as session:
        records = session.execute(
            select(func.count()).select_from(ProviderCostReconciliation)
        ).scalar_one()
        audits = session.execute(
            select(func.count())
            .select_from(AuditLog)
            .where(AuditLog.action == AuditAction.PROVIDER_COST_RECONCILIATION_CREATE)
        ).scalar_one()
        return int(records), int(audits)


def fx_public_id(world: World, name: str) -> str:
    with world.factory() as session:
        return str(
            session.execute(
                select(FxRateVersion.public_id).where(FxRateVersion.id == world.fx[name].id)
            ).scalar_one()
        )


def assert_view_shape(view: dict[str, Any]) -> None:
    """Keys exactly as design §2; amounts 8-place strings, counts integers (not booleans)."""
    assert set(view) == VIEW_KEYS
    for key in MONEY_KEYS:
        assert isinstance(view[key], str) and MONEY.fullmatch(view[key]), key
    for key in COUNT_KEYS:
        assert type(view[key]) is int, key
    assert type(view["exceeds_threshold"]) is bool
    for key in ("source_variance_rate", "myr_variance_rate"):
        assert view[key] is None or isinstance(view[key], str), key
    assert view["period_basis"] == "utc_calendar_month"
    assert view["variance_threshold"] == "0.05"
    assert view["scope"] == "AI_MODULE"
    assert view["coverage_method"] == "PERIOD_AGGREGATE"
    for key in ("period_start", "period_end", "fx_rate_moment", "created_at"):
        assert view[key] is None or not view[key].endswith("Z"), key


def expected_standard_view(world: World) -> dict[str, Any]:
    """The December preview of the standard world (design §7: V44, 51.48), fields only."""
    return {
        "reconciliation_id": None,
        "provider_code": "anthropic",
        "scope": "AI_MODULE",
        "billing_month": "2026-12",
        "period_basis": "utc_calendar_month",
        "period_start": "2026-12-01T00:00:00",
        "period_end": "2027-01-01T00:00:00",
        "source_document_reference": PREVIEW_BODY["source_document_reference"],
        "source_currency": "USD",
        "source_total": "11.70000000",
        "fx_rate_version_id": fx_public_id(world, V44),
        "fx_rate_applied": "4.4",
        "fx_rate_moment": "2026-12-31T23:59:59.999999",
        "reconciled_cost_myr": "51.48000000",
        "estimated_event_count": 2,
        "estimated_source_cost": "10.50000000",
        "estimated_cost_myr": "46.80000000",
        "source_variance": "1.20000000",
        "source_variance_rate": "0.1142857142857142857142857143",
        "myr_variance": "4.68000000",
        "myr_variance_rate": "0.1",
        "exceeds_threshold": True,
        "variance_threshold": "0.05",
        "unprocessed_event_count": 1,
        "outbox_unsettled_count": 1,
        "outbox_discarded_count": 1,
        "coverage_method": "PERIOD_AGGREGATE",
        "supersedes_id": None,
        "reason": None,
        "is_current": None,
        "created_at": None,
        "created_by_email": None,
    }


def confirm_december(client: TestClient, headers: dict[str, str]) -> dict[str, Any]:
    return data(client.post(RECONCILIATIONS, json=CONFIRM_BODY, headers=headers), 201)


# --- 接口契约 -----------------------------------------------------------------------------


def test_preview_and_confirm_on_the_standard_world(app, client, world) -> None:
    admin = admin_of(app, world)
    expected = expected_standard_view(world)

    previewed = data(client.post(PREVIEW, json=PREVIEW_BODY, headers=admin))

    assert_view_shape(previewed)
    assert previewed == expected
    assert written(world) == (0, 0)

    confirmed = confirm_december(client, admin)

    assert_view_shape(confirmed)
    assert isinstance(confirmed["reconciliation_id"], str)
    assert confirmed == {
        **expected,
        "reconciliation_id": confirmed["reconciliation_id"],
        "is_current": True,
        "created_at": "2027-02-15T00:00:00",
        "created_by_email": world.admin.email,
    }
    assert written(world) == (1, 1)


def test_months_and_detail_on_the_standard_world(app, client, world) -> None:
    admin = admin_of(app, world)
    confirmed = confirm_december(client, admin)

    months = data(client.get(MONTHS, params={"provider_code": "anthropic"}, headers=admin))
    assert set(months) == {"items"}
    for item in months["items"]:
        assert set(item) == MONTH_KEYS
    assert months["items"][1]["current"] == confirmed

    detail = data(client.get(f"{RECONCILIATIONS}/{confirmed['reconciliation_id']}", headers=admin))
    assert set(detail) == DETAIL_KEYS
    assert detail["reconciliation"] == confirmed
    assert detail["chain"] == [confirmed]
    assert detail["live_estimate"] == {
        "estimated_event_count": 2,
        "estimated_source_cost": "10.50000000",
        "estimated_cost_myr": "46.80000000",
        "currency_mismatch_count": 0,
        "unprocessed_event_count": 1,
    }
    assert detail["estimate_changed_since_confirmation"] is False


def test_a_myr_record_has_no_fx_keys(app, client, myr_world) -> None:
    admin = admin_of(app, myr_world)
    fx_keys = ("fx_rate_version_id", "fx_rate_applied", "fx_rate_moment")

    previewed = data(client.post(PREVIEW, json=MYR_PREVIEW_BODY, headers=admin))
    confirmed = data(client.post(RECONCILIATIONS, json=MYR_CONFIRM_BODY, headers=admin), 201)

    for view in (previewed, confirmed):
        assert_view_shape(view)
        assert {key: view[key] for key in fx_keys} == dict.fromkeys(fx_keys)
        assert view["source_currency"] == "MYR"
        assert view["source_total"] == "3.00000000"
        assert view["reconciled_cost_myr"] == "3.00000000"
        assert view["estimated_event_count"] == 3
        assert view["estimated_source_cost"] == "3.00000000"
        assert view["estimated_cost_myr"] == "3.00000000"
        assert view["source_variance"] == "0.00000000"
        assert view["source_variance_rate"] == "0"
        assert view["exceeds_threshold"] is False
    assert previewed["reconciliation_id"] is None
    assert previewed["is_current"] is None

    months = data(client.get(MONTHS, params={"provider_code": "anthropic"}, headers=admin))
    december = months["items"][1]
    assert set(december) == MONTH_KEYS
    assert december["current"] == confirmed

    detail = data(client.get(f"{RECONCILIATIONS}/{confirmed['reconciliation_id']}", headers=admin))
    assert set(detail) == DETAIL_KEYS
    assert detail["reconciliation"] == confirmed
    assert {key: detail["reconciliation"][key] for key in fx_keys} == dict.fromkeys(fx_keys)
    assert set(detail["live_estimate"]) == LIVE_KEYS


@pytest.mark.parametrize("value", [11.7, 11, None])
def test_source_total_must_be_a_string(app, client, world, value) -> None:
    admin = admin_of(app, world)
    for url, body in ((PREVIEW, PREVIEW_BODY), (RECONCILIATIONS, CONFIRM_BODY)):
        response = client.post(url, json={**body, "source_total": value}, headers=admin)
        assert_error(response, 422, "VALIDATION_ERROR")
        assert "source_total" in response.json()["error"]["message"]
    assert written(world) == (0, 0)


@pytest.mark.parametrize(
    # 末一个是全角的 1：`[0-9]` 不认它。
    "value",
    ["-1", "1.123456789", "1234567890", "1e2", "+1", " 1", "１"],
)
def test_source_total_outside_the_pattern_is_refused(app, client, world, value) -> None:
    admin = admin_of(app, world)
    response = client.post(PREVIEW, json={**PREVIEW_BODY, "source_total": value}, headers=admin)
    assert_error(response, 422, "VALIDATION_ERROR")


def test_a_zero_source_total_is_allowed(app, client, world) -> None:
    admin = admin_of(app, world)
    previewed = data(
        client.post(PREVIEW, json={**PREVIEW_BODY, "source_total": "0"}, headers=admin)
    )
    assert previewed["source_total"] == "0.00000000"
    assert previewed["reconciled_cost_myr"] == "0.00000000"


def test_a_validation_error_does_not_echo_the_value(app, client, world) -> None:
    admin = admin_of(app, world)
    response = client.post(PREVIEW, json={**PREVIEW_BODY, "source_total": 31415.926}, headers=admin)
    assert_error(response, 422, "VALIDATION_ERROR")
    assert "31415" not in response.text


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("expected_event_count", True),
        ("expected_event_count", "2"),
        ("expected_event_count", -1),
        ("expected_estimated_source_cost", 10.5),
        ("expected_estimated_source_cost", "-1"),
        ("expected_estimated_source_cost", "1.123456789"),
    ],
)
def test_bad_expected_values_are_refused(app, client, world, field, value) -> None:
    admin = admin_of(app, world)
    response = client.post(RECONCILIATIONS, json={**CONFIRM_BODY, field: value}, headers=admin)
    assert_error(response, 422, "VALIDATION_ERROR")
    assert field in response.json()["error"]["message"]
    assert written(world) == (0, 0)


@pytest.mark.parametrize("field", ["expected_event_count", "expected_estimated_source_cost"])
def test_the_confirmation_needs_both_expected_values(app, client, world, field) -> None:
    admin = admin_of(app, world)
    body = {key: value for key, value in CONFIRM_BODY.items() if key != field}
    assert_error(client.post(RECONCILIATIONS, json=body, headers=admin), 422, "VALIDATION_ERROR")


@pytest.mark.parametrize(
    "extra",
    [
        {"expected_event_count": 2},
        {"expected_estimated_source_cost": "10.50000000"},
        {"created_by": 1},
    ],
)
def test_a_preview_takes_exactly_seven_fields(app, client, world, extra) -> None:
    admin = admin_of(app, world)
    response = client.post(PREVIEW, json={**PREVIEW_BODY, **extra}, headers=admin)
    assert_error(response, 422, "VALIDATION_ERROR")


@pytest.mark.parametrize(
    "pairing",
    [
        {"supersedes_id": None, "reason": "A reason without a correction"},
        {"supersedes_id": ZERO_UUID, "reason": None},
        {"supersedes_id": ZERO_UUID},
        {"supersedes_id": ZERO_UUID, "reason": "   "},
    ],
)
def test_supersedes_id_and_reason_go_together(app, client, world, pairing) -> None:
    admin = admin_of(app, world)
    for url, body in ((PREVIEW, PREVIEW_BODY), (RECONCILIATIONS, CONFIRM_BODY)):
        response = client.post(url, json={**body, **pairing}, headers=admin)
        assert_error(response, 422, "VALIDATION_ERROR")
    assert written(world) == (0, 0)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("source_currency", "usd"),
        ("source_currency", "US"),
        ("source_document_reference", "   "),
        ("source_document_reference", "x" * 256),
        ("provider_code", ""),
        ("provider_code", "x" * 65),
        ("billing_month", "2026-012"),
        ("billing_month", 202612),
    ],
)
def test_field_formats_are_checked(app, client, world, field, value) -> None:
    admin = admin_of(app, world)
    response = client.post(PREVIEW, json={**PREVIEW_BODY, field: value}, headers=admin)
    assert_error(response, 422, "VALIDATION_ERROR")


@pytest.mark.parametrize(
    ("field", "value", "status_code", "code"),
    [
        # 只限长度：格式与取值交给服务层。
        ("billing_month", "2026-13", 422, "RECONCILIATION_MONTH_INVALID"),
        ("billing_month", "Dec", 422, "RECONCILIATION_MONTH_INVALID"),
        ("provider_code", "Bad!", 404, "AI_PROVIDER_NOT_FOUND"),
        ("provider_code", "openai", 404, "AI_PROVIDER_NOT_FOUND"),
    ],
)
def test_month_and_provider_are_judged_by_the_service(
    app, client, world, field, value, status_code, code
) -> None:
    admin = admin_of(app, world)
    response = client.post(PREVIEW, json={**PREVIEW_BODY, field: value}, headers=admin)
    assert_error(response, status_code, code)


def test_months_and_preview_are_not_taken_for_an_id(app, client, world) -> None:
    admin = admin_of(app, world)
    months = data(client.get(MONTHS, params={"provider_code": "anthropic"}, headers=admin))
    assert set(months) == {"items"}
    # 不带 provider_code 是 422，而不是把 `months` 当成记录 id 的 404。
    assert_error(client.get(MONTHS, headers=admin), 422, "VALIDATION_ERROR")
    previewed = data(client.post(PREVIEW, json=PREVIEW_BODY, headers=admin))
    assert previewed["reconciliation_id"] is None


# --- 总览 -------------------------------------------------------------------------------


def test_the_months_overview_lists_ended_months_latest_first(app, client, world) -> None:
    admin = admin_of(app, world)
    confirmed = confirm_december(client, admin)

    items = data(client.get(MONTHS, params={"provider_code": "anthropic"}, headers=admin))["items"]

    assert [(item["billing_month"], item["status"]) for item in items] == [
        ("2027-01", "NOT_RECONCILED"),
        ("2026-12", "RECONCILED"),
    ]
    january, december = items
    assert january == {
        "billing_month": "2027-01",
        "period_start": "2027-01-01T00:00:00",
        "period_end": "2027-02-01T00:00:00",
        "status": "NOT_RECONCILED",
        "current": None,
    }
    assert december["period_start"] == "2026-12-01T00:00:00"
    assert december["period_end"] == "2027-01-01T00:00:00"
    assert_view_shape(december["current"])
    assert december["current"]["reconciliation_id"] == confirmed["reconciliation_id"]
    assert december["current"]["is_current"] is True


def test_the_months_overview_is_empty_before_the_first_month_ends(
    app, client, world, clock
) -> None:
    clock[0] = dt.datetime(2026, 12, 20, 0, 0, 0)
    admin = admin_of(app, world)
    response = client.get(MONTHS, params={"provider_code": "anthropic"}, headers=admin)
    assert data(response) == {"items": []}


def test_an_unknown_provider_is_404_not_a_list_of_unreconciled_months(app, client, world) -> None:
    admin = admin_of(app, world)
    response = client.get(MONTHS, params={"provider_code": "openai"}, headers=admin)
    assert_error(response, 404, "AI_PROVIDER_NOT_FOUND")


@pytest.mark.parametrize("code", ["Bad!", "Anthropic", "", "x" * 65])
def test_a_malformed_provider_code_is_422(app, client, world, code) -> None:
    admin = admin_of(app, world)
    response = client.get(MONTHS, params={"provider_code": code}, headers=admin)
    assert_error(response, 422, "VALIDATION_ERROR")
    assert "provider_code" in response.json()["error"]["message"]


# --- 详情与 id ----------------------------------------------------------------------------


def test_the_detail_shows_the_live_estimate_after_a_late_event(app, client, world) -> None:
    admin = admin_of(app, world)
    confirmed = confirm_december(client, admin)
    late = dt.datetime(2026, 12, 16, 0, 0, 0)
    world.add_event(event_id=LATE_EVENT, project=P_CRM, occurred_at=late, source_cost="1.00")
    world.add_outbox(event_id=LATE_EVENT, project=P_CRM, status="DELIVERED", created_at=late)

    response = client.get(f"{RECONCILIATIONS}/{confirmed['reconciliation_id']}", headers=admin)

    detail = data(response)
    assert set(detail) == DETAIL_KEYS
    assert set(detail["live_estimate"]) == LIVE_KEYS
    assert detail["live_estimate"] == {
        "estimated_event_count": 3,
        "estimated_source_cost": "11.50000000",
        "estimated_cost_myr": "51.30000000",
        "currency_mismatch_count": 0,
        "unprocessed_event_count": 1,
    }
    assert detail["estimate_changed_since_confirmation"] is True
    # 记录本身是确认时的快照，不随实时估算变。
    assert detail["reconciliation"] == confirmed
    assert detail["chain"] == [confirmed]


@pytest.mark.parametrize("reconciliation_id", [ZERO_UUID, "not-a-uuid"])
def test_an_unknown_or_malformed_id_is_404(app, client, world, reconciliation_id) -> None:
    admin = admin_of(app, world)
    confirm_december(client, admin)
    response = client.get(f"{RECONCILIATIONS}/{reconciliation_id}", headers=admin)
    assert_error(response, 404, "PROVIDER_COST_RECONCILIATION_NOT_FOUND")


@pytest.mark.parametrize("url", [PREVIEW, RECONCILIATIONS])
def test_superseding_a_malformed_id_is_404_not_422(app, client, world, url) -> None:
    admin = admin_of(app, world)
    confirm_december(client, admin)
    body = PREVIEW_BODY if url == PREVIEW else CONFIRM_BODY
    correction = {**body, "supersedes_id": "not-a-uuid", "reason": "Fictional correction"}

    response = client.post(url, json=correction, headers=admin)

    assert_error(response, 404, "PROVIDER_COST_RECONCILIATION_NOT_FOUND")
    assert written(world) == (1, 1)


# --- 鉴权 -------------------------------------------------------------------------------


PROBES: Final = (
    ("GET", MONTHS + "?provider_code=anthropic", None),
    ("POST", PREVIEW, PREVIEW_BODY),
    ("POST", RECONCILIATIONS, CONFIRM_BODY),
    ("GET", f"{RECONCILIATIONS}/{ZERO_UUID}", None),
)


@pytest.mark.parametrize("caller", ["anonymous", "customer"])
@pytest.mark.parametrize(
    ("method", "url", "body"), PROBES, ids=["months", "preview", "confirm", "detail"]
)
def test_non_admins_are_refused_and_nothing_is_written(
    app, client, world, caller, method, url, body
) -> None:
    """INV-7、INV-8：探针一律带合法参数与请求体（不断言与 422 的先后）。"""
    headers = {} if caller == "anonymous" else customer_headers(app, world)
    before = written(world)

    response = client.request(method, url, json=body, headers=headers)

    expected = (401, "TOKEN_INVALID") if caller == "anonymous" else (403, "ADMIN_REQUIRED")
    assert (response.status_code, error_code(response)) == expected
    assert written(world) == before

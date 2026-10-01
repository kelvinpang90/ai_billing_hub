"""FX rate endpoints end to end on SQLite (design gate #183 v3 §7, AIH-TASK-041).

这里测接口契约：设计 §7「手工录入与编辑」「精度」「鉴权」三行；每个错误码至少一个经 HTTP 的
用例；一次经 HTTP 的发布与撤销预约；`observed_at` / `effective_from` 的格式；请求校验先于状态
判断；列表与拉取记录的形状、排序、筛选与分页；读接口不拿 FX 锁；响应里没有内部 id；以及
「每个处理函数第一条语句是 `require_admin`」（AST）。

依赖冻结时钟的 E1–E16、复查与事务中途失败在 test_fx_rates_service.py；并发与性质用例在
test_fx_rates_resolve.py。匿名 401 / CUSTOMER 403 且不写库，除了这里的八个接口，还在
test_admin_customers_api.py 从路由表枚举全部管理端路由。

汇率一律是明显的虚构值（公开仓库）。
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
from decimal import Decimal

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
from app.models.auth import AuditAction, AuditLog, User, UserRole, UserStatus
from app.models.base import Base
from app.models.fx_rates import (
    FxFetchAttempt,
    FxFetchOutcome,
    FxRateSourceType,
    FxRateStatus,
    FxRateVersion,
)
from app.repositories import fx_rates as fx_repository
from app.services.auth import utc_now

RATES = "/api/v1/admin/fx-rates"
ATTEMPTS = RATES + "/fetch-attempts"
ROUTER_FILE = pathlib.Path("app/api/admin_fx_rates.py")

# 响应字段白名单（设计 §2「接口」）。断言「恰好等于」：多一个键（内部 id）就红。
VERSION_FIELDS = {
    "id",
    "base_currency",
    "quote_currency",
    "rate",
    "source",
    "source_reference",
    "source_quote_date",
    "observed_at",
    "status",
    "effective_from",
    "effective_to",
    "created_by_email",
    "approved_by_email",
    "approved_at",
    "created_at",
    "updated_at",
}
# 设计 v3：拉取记录没有 id。
ATTEMPT_FIELDS = {
    "base_currency",
    "source",
    "requested_date",
    "outcome",
    "quote_date",
    "error_code",
    "fx_rate_id",
    "attempted_at",
}
PAGE_FIELDS = {"items", "page", "page_size", "total"}
ENVELOPE_FIELDS = {"success", "data", "error", "request_id"}

# 全零占位值（secret-scan）。
ZERO_ID = "00000000-0000-4000-8000-000000000000"

# 明显的虚构汇率（1 单位外币 = 多少 MYR）。
FICTIONAL_RATE = "1.2345678901"
OTHER_FICTIONAL_RATE = "9.8765432109"

# 远在将来的预约时刻：测试的真实时钟永远早于它。
FAR_FUTURE = "2999-01-02T00:00:00"


# --- 夹具与帮手 -----------------------------------------------------------------


def settings_for(tmp_path) -> Settings:
    key = tmp_path / "jwt.key"
    key.write_text("a-test-signing-key-that-is-long-enough", encoding="utf-8")
    master = tmp_path / "master.key"
    master.write_text(f"1:{base64.b64encode(os.urandom(32)).decode()}\n", encoding="utf-8")
    # BNM 自动拉取只有 USD：手工录入不受它限制（设计 v3）。
    return Settings(
        jwt_secret_file=str(key),
        master_key_file=str(master),
        database_url="",
        fx_currencies="USD",
    )


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
    yield application
    engine.dispose()


@pytest.fixture
def client(app) -> TestClient:
    return TestClient(app)


def make_user(application: FastAPI, role: UserRole) -> tuple[int, str]:
    """(internal id, email) of a committed active user."""
    now = utc_now()
    email = f"{uuid.uuid4().hex}@example.com"
    with application.state.session_factory() as session:
        user = User(
            email=email,
            password_hash="not-a-real-hash",
            role=role,
            status=UserStatus.ACTIVE,
            created_at=now,
            updated_at=now,
        )
        session.add(user)
        session.commit()
        return int(user.id), email


def token_for(application: FastAPI, user_id: int, role: str) -> dict[str, str]:
    token = issue_access_token(
        application.state.settings,
        user_id=user_id,
        role=role,
        session_id=uuid.uuid4().hex,
        now=utc_now(),
    )
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def admin_user(app) -> tuple[int, str]:
    return make_user(app, UserRole.ADMIN)


@pytest.fixture
def admin(app, admin_user) -> dict[str, str]:
    return token_for(app, admin_user[0], "ADMIN")


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


def count(session: Session, model: type) -> int:
    return session.execute(select(func.count()).select_from(model)).scalar_one()


def snapshot(application: FastAPI) -> dict[str, object]:
    """Every FX row a write may change, as committed, plus the audit and attempt counts."""
    versions = select(
        FxRateVersion.public_id,
        FxRateVersion.base_currency,
        FxRateVersion.rate,
        FxRateVersion.source_reference,
        FxRateVersion.observed_at,
        FxRateVersion.status,
        FxRateVersion.effective_from,
        FxRateVersion.effective_to,
        FxRateVersion.approved_at,
        FxRateVersion.updated_at,
    ).order_by(FxRateVersion.id)
    with factory_of(application)() as session:
        return {
            "versions": [tuple(row) for row in session.execute(versions)],
            "audits": count(session, AuditLog),
            "attempts": count(session, FxFetchAttempt),
        }


def audits(application: FastAPI, action: AuditAction) -> list[AuditLog]:
    statement = select(AuditLog).where(AuditLog.action == action).order_by(AuditLog.id)
    with factory_of(application)() as session:
        return list(session.execute(statement).scalars())


def states(audit: AuditLog) -> tuple[object, object]:
    before = json.loads(audit.before_state) if audit.before_state else None
    after = json.loads(audit.after_state) if audit.after_state else None
    return before, after


def draft_body(**fields: object) -> dict[str, object]:
    return {
        "base_currency": "USD",
        "rate": FICTIONAL_RATE,
        "observed_at": "2026-09-29T12:00:00+08:00",
        "source_reference": "Fictional bank notice, viewed 2026-09-29",
        **fields,
    }


def create(client: TestClient, headers: dict[str, str], body: dict[str, object]):
    return client.post(RATES, json=body, headers=headers)


def new_draft(client: TestClient, headers: dict[str, str], **fields: object) -> dict:
    response = create(client, headers, draft_body(**fields))
    assert response.status_code == 201, response.text
    return response.json()["data"]


def act(client: TestClient, headers: dict[str, str], version_id: str, action: str, body=None):
    """POST …/{id}/publish | retire | discard."""
    payload = {} if body is None else body
    return client.post(f"{RATES}/{version_id}/{action}", json=payload, headers=headers)


def published(client: TestClient, headers: dict[str, str], body=None, **fields) -> dict:
    draft = new_draft(client, headers, **fields)
    response = act(client, headers, draft["id"], "publish", body)
    assert response.status_code == 200, response.text
    return response.json()["data"]


def fetched(client: TestClient, headers: dict[str, str], version_id: str) -> dict:
    response = client.get(f"{RATES}/{version_id}", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["data"]


def bnm_draft(application: FastAPI) -> str:
    """A BNM draft as the fetch task (AIH-TASK-040) writes it: no creator, no audit."""
    quote_date = dt.date(2026, 9, 29)
    now = utc_now().replace(microsecond=0)
    public_id = str(uuid.uuid4())
    reference = f"bnm:exchange-rate:USD:{quote_date}:session=1200:middle_rate:unit=1"
    with factory_of(application)() as session:
        session.add(
            FxRateVersion(
                public_id=public_id,
                base_currency="USD",
                quote_currency="MYR",
                rate=Decimal("3.3333"),
                source=FxRateSourceType.BNM,
                source_reference=reference,
                source_quote_date=quote_date,
                observed_at=dt.datetime.combine(quote_date, dt.time(4)),
                status=FxRateStatus.DRAFT,
                created_by=None,
                created_at=now,
                updated_at=now,
            )
        )
        session.commit()
    return public_id


# --- 手工录入与编辑（设计 §7） ------------------------------------------------------


def test_manual_draft_create_edit_and_discard(client, app, admin, admin_user) -> None:
    """建 → 改 → 丢弃；`observed_at` 换成 UTC 存；创建人用登录邮箱；三条审计。"""
    headers = {**admin, "User-Agent": "admin-console-test"}
    _, email = admin_user

    created = create(client, headers, draft_body())

    assert created.status_code == 201, created.text
    draft = created.json()["data"]
    assert set(draft) == VERSION_FIELDS
    assert str(uuid.UUID(draft["id"])) == draft["id"]
    assert (draft["base_currency"], draft["quote_currency"], draft["rate"]) == (
        "USD",
        "MYR",
        FICTIONAL_RATE,
    )
    assert (draft["source"], draft["source_quote_date"], draft["status"]) == (
        "MANUAL",
        None,
        "DRAFT",
    )
    # 吉隆坡 12:00 = UTC 04:00。
    assert draft["observed_at"] == "2026-09-29T04:00:00"
    assert (draft["effective_from"], draft["effective_to"]) == (None, None)
    assert (draft["created_by_email"], draft["approved_by_email"], draft["approved_at"]) == (
        email,
        None,
        None,
    )
    assert fetched(client, headers, draft["id"]) == draft

    edited = client.patch(
        f"{RATES}/{draft['id']}",
        json={
            "rate": OTHER_FICTIONAL_RATE,
            "observed_at": "2026-09-30T04:00:00Z",
            "source_reference": "  Corrected fictional notice  ",
        },
        headers=headers,
    )

    assert edited.status_code == 200, edited.text
    edited_view = edited.json()["data"]
    assert (edited_view["rate"], edited_view["observed_at"], edited_view["source_reference"]) == (
        OTHER_FICTIONAL_RATE,
        "2026-09-30T04:00:00",
        "Corrected fictional notice",
    )
    assert edited_view["status"] == "DRAFT"

    discarding = act(client, headers, draft["id"], "discard")

    assert discarding.status_code == 200, discarding.text
    assert discarding.json()["data"]["status"] == "DISCARDED"
    # 行留着（不删除）。
    assert fetched(client, headers, draft["id"])["status"] == "DISCARDED"

    [create_audit] = audits(app, AuditAction.FX_RATE_CREATE)
    assert states(create_audit) == (
        None,
        {
            "base_currency": "USD",
            "rate": FICTIONAL_RATE,
            "observed_at": "2026-09-29T04:00:00",
            "source_reference": "Fictional bank notice, viewed 2026-09-29",
            "status": "DRAFT",
        },
    )
    [update_audit] = audits(app, AuditAction.FX_RATE_UPDATE)
    assert states(update_audit) == (
        {
            "rate": FICTIONAL_RATE,
            "observed_at": "2026-09-29T04:00:00",
            "source_reference": "Fictional bank notice, viewed 2026-09-29",
        },
        {
            "rate": OTHER_FICTIONAL_RATE,
            "observed_at": "2026-09-30T04:00:00",
            "source_reference": "Corrected fictional notice",
        },
    )
    [discard_audit] = audits(app, AuditAction.FX_RATE_DISCARD)
    assert states(discard_audit) == ({"status": "DRAFT"}, {"status": "DISCARDED"})
    for audit in (create_audit, update_audit, discard_audit):
        assert (audit.entity_type, audit.entity_id) == ("fx_rate_version", draft["id"])
        assert (audit.actor_role, audit.user_agent) == ("ADMIN", "admin-console-test")


def test_a_manual_currency_is_not_limited_to_the_fetched_ones(client, app, admin) -> None:
    """设计 v3：`BILLING_FX_CURRENCIES` 只是 BNM 拉取的范围；MYR 本身不是外币（422）。"""
    assert app.state.settings.fx_currencies == "USD"

    accepted = [new_draft(client, admin, base_currency=code) for code in ("SGD", "XAU", "ZZZ")]

    assert [draft["base_currency"] for draft in accepted] == ["SGD", "XAU", "ZZZ"]
    before = snapshot(app)
    refused = create(client, admin, draft_body(base_currency="MYR"))
    assert (refused.status_code, error_code(refused)) == (422, "VALIDATION_ERROR")
    assert snapshot(app) == before


def test_an_edit_that_changes_nothing_writes_nothing(client, app, admin) -> None:
    """`rate` 按数值比较（`2.5` 与 `2.5000000000` 相同）；同一时刻换个时区写也一样。"""
    draft = new_draft(client, admin, rate="2.5")
    before = snapshot(app)

    same = client.patch(
        f"{RATES}/{draft['id']}",
        json={
            "rate": "2.5000000000",
            "observed_at": "2026-09-29T04:00:00Z",
            "source_reference": draft["source_reference"],
        },
        headers=admin,
    )

    assert same.status_code == 200, same.text
    assert same.json()["data"] == draft
    assert snapshot(app) == before
    assert audits(app, AuditAction.FX_RATE_UPDATE) == []


def test_a_bnm_draft_cannot_be_edited_but_can_be_discarded(client, app, admin) -> None:
    """设计 §2「状态」：编辑 BNM 草稿 409 `FX_RATE_NOT_EDITABLE`。

    出处不同：要改就丢弃后手工录入，所以丢弃照常。
    """
    version_id = bnm_draft(app)
    before = snapshot(app)

    edited = client.patch(f"{RATES}/{version_id}", json={"rate": "4.4444"}, headers=admin)

    assert (edited.status_code, error_code(edited)) == (409, "FX_RATE_NOT_EDITABLE")
    assert snapshot(app) == before
    shown = fetched(client, admin, version_id)
    assert (shown["source"], shown["source_quote_date"], shown["created_by_email"]) == (
        "BNM",
        "2026-09-29",
        None,
    )
    discarded = act(client, admin, version_id, "discard")
    assert discarded.status_code == 200, discarded.text
    assert discarded.json()["data"]["status"] == "DISCARDED"


def test_retiring_a_draft_is_not_retirable(client, app, admin) -> None:
    """设计 §2「退役」R4：草稿用丢弃，不是退役。"""
    draft = new_draft(client, admin)
    before = snapshot(app)

    response = act(client, admin, draft["id"], "retire", {"reason": "Fictional"})

    assert (response.status_code, error_code(response)) == (409, "FX_RATE_NOT_RETIRABLE")
    assert snapshot(app) == before


# --- 发布与撤销预约 -------------------------------------------------------------------


def test_publish_reserve_and_withdraw_the_reservation(client, app, admin, admin_user) -> None:
    """经 HTTP：P1 → P2（预约，F 带 +08:00）→ P3 不给时刻 409 → R3 409 → R2 撤销预约 → P2。"""
    _, email = admin_user
    first = published(client, admin)
    # P1：时间线为空、不给时刻 =「一直以来」。
    assert (first["status"], first["effective_from"], first["effective_to"]) == (
        "PUBLISHED",
        None,
        None,
    )
    assert first["approved_by_email"] == email
    assert first["approved_at"] is not None

    in_kuala_lumpur = {"effective_from": "2999-01-02T08:00:00+08:00"}
    reserved = published(client, admin, in_kuala_lumpur, rate=OTHER_FICTIONAL_RATE)

    assert (reserved["effective_from"], reserved["effective_to"]) == (FAR_FUTURE, None)
    assert fetched(client, admin, first["id"])["effective_to"] == FAR_FUTURE

    # P3：末尾版本尚未开始，不给时刻 409，什么都不写。
    third = new_draft(client, admin, rate="2.5")
    before = snapshot(app)
    conflict = act(client, admin, third["id"], "publish")
    assert (conflict.status_code, error_code(conflict)) == (409, "EFFECTIVE_FROM_CONFLICT")
    # R3：被截断的历史版本不能退役。
    truncated = act(client, admin, first["id"], "retire", {"reason": "Fictional"})
    assert (truncated.status_code, error_code(truncated)) == (409, "FX_RATE_NOT_RETIRABLE")
    assert snapshot(app) == before

    # R2：撤销预约 —— 空区间的 RETIRED；被它截断的前一个恢复为未截断。
    withdrawing = act(client, admin, reserved["id"], "retire", {"reason": "  Fictional undo  "})

    assert withdrawing.status_code == 200, withdrawing.text
    withdrawn = withdrawing.json()["data"]
    assert (withdrawn["status"], withdrawn["effective_from"], withdrawn["effective_to"]) == (
        "RETIRED",
        FAR_FUTURE,
        FAR_FUTURE,
    )
    restored = fetched(client, admin, first["id"])
    assert (restored["status"], restored["effective_to"]) == ("PUBLISHED", None)
    [retire_audit] = audits(app, AuditAction.FX_RATE_RETIRE)
    assert retire_audit.reason == "Fictional undo"
    before_state, after_state = states(retire_audit)
    assert before_state == {
        "status": "PUBLISHED",
        "effective_from": FAR_FUTURE,
        "effective_to": None,
    }
    assert after_state == {
        "status": "RETIRED",
        "effective_from": FAR_FUTURE,
        "restored_version": {"id": first["id"]},
        "effective_to": FAR_FUTURE,
        "reason": "Fictional undo",
    }

    # 撤销之后空区间不在时间线上：再不给时刻发布就是 P2，截断恢复了的那个。
    now_published = act(client, admin, third["id"], "publish")
    assert now_published.status_code == 200, now_published.text
    boundary = now_published.json()["data"]["effective_from"]
    assert boundary is not None
    assert fetched(client, admin, first["id"])["effective_to"] == boundary
    [publish_first, publish_reserved, publish_third] = audits(app, AuditAction.FX_RATE_PUBLISH)
    assert states(publish_first)[1]["approved_by_email"] == email
    assert states(publish_reserved)[0] == {
        "status": "DRAFT",
        "truncated_version": {"id": first["id"], "effective_to": None},
    }
    assert states(publish_third)[1]["truncated_version"] == {
        "id": first["id"],
        "effective_to": boundary,
    }


def test_publishing_again_writes_nothing_and_ignores_effective_from(client, app, admin) -> None:
    """设计 v3：已发布再发布 200 不写；合法的 F 被忽略（即使早于 `t` 也不报 422）。"""
    version = published(client, admin)
    before = snapshot(app)

    responses = [
        act(client, admin, version["id"], "publish"),
        act(client, admin, version["id"], "publish", {"effective_from": "2000-01-01T00:00:00Z"}),
        act(client, admin, version["id"], "publish", {"effective_from": "2999-01-01T00:00:00Z"}),
    ]

    assert [response.status_code for response in responses] == [200, 200, 200]
    assert all(response.json()["data"] == version for response in responses)
    assert snapshot(app) == before


@pytest.mark.parametrize(
    "effective_from",
    ["2999-01-01T00:00:00", "2999-01-01T00:00:00.5Z", "2999-02-30T00:00:00Z", 1_000_000_000],
    ids=["no-zone", "fraction", "no-such-day", "number"],
)
def test_request_validation_comes_before_the_state(client, app, admin, effective_from) -> None:
    """设计 v3：格式不对的 `effective_from` 一律 422，与版本当前状态无关。

    已发布的（否则 200 不写）、不存在的（否则 404）都是 422。
    """
    version = published(client, admin)
    before = snapshot(app)
    body = {"effective_from": effective_from}

    responses = [
        act(client, admin, version["id"], "publish", body),
        act(client, admin, ZERO_ID, "publish", body),
    ]

    assert outcomes(responses) == [(422, "VALIDATION_ERROR")] * 2
    assert snapshot(app) == before


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
def test_effective_from_rules(client, app, admin, effective_from, expected) -> None:
    """格式、整秒、带时区：422 `VALIDATION_ERROR`。

    早于 `t`：422 `EFFECTIVE_FROM_IN_PAST`。都不写。
    """
    draft = new_draft(client, admin)
    before = snapshot(app)

    response = act(client, admin, draft["id"], "publish", {"effective_from": effective_from})

    assert (response.status_code, error_code(response)) == expected
    assert snapshot(app) == before


@pytest.mark.parametrize(
    ("observed_at", "stored"),
    [
        ("2026-09-29T04:00:00Z", "2026-09-29T04:00:00"),
        ("2026-09-29T12:00:00+08:00", "2026-09-29T04:00:00"),
        ("2026-09-29T04:00:00.000Z", "2026-09-29T04:00:00"),
        ("2026-09-29T04:00:00", None),
        ("2026-09-29T04:00:00.5Z", None),
        ("2026-09-29 04:00:00Z", None),
        ("2026-02-30T04:00:00Z", None),
        ("2026-09-29", None),
        (1_000_000_000, None),
        (None, None),
    ],
    ids=[
        "utc",
        "offset",
        "zero-fraction",
        "no-zone",
        "fraction",
        "space",
        "no-such-day",
        "date-only",
        "number",
        "null",
    ],
)
def test_observed_at_rules(client, app, admin, observed_at, stored) -> None:
    """设计 v3：`observed_at` 与 `effective_from` 同一规则；建草稿与改草稿都一样。"""
    draft = new_draft(client, admin)
    before = snapshot(app)

    created = create(client, admin, draft_body(observed_at=observed_at))
    if stored is None:
        assert (created.status_code, error_code(created)) == (422, "VALIDATION_ERROR")
        assert snapshot(app) == before
    else:
        assert created.status_code == 201, created.text
        assert created.json()["data"]["observed_at"] == stored

    before = snapshot(app)
    url = f"{RATES}/{draft['id']}"
    edited = client.patch(url, json={"observed_at": observed_at}, headers=admin)
    if stored is None:
        assert (edited.status_code, error_code(edited)) == (422, "VALIDATION_ERROR")
        assert snapshot(app) == before
    else:
        assert edited.status_code == 200, edited.text
        assert edited.json()["data"]["observed_at"] == stored


# --- 精度（设计 §7） ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "shown"),
    [
        ("1.2345678901", "1.2345678901"),
        ("0.0000000001", "0.0000000001"),
        ("2.5000", "2.5"),
        ("7", "7"),
        ("12345678901234", "12345678901234"),
        ("1.23456789012", None),
        ("0", None),
        ("0.0000000000", None),
        ("-1", None),
        ("+1", None),
        ("1e-3", None),
        ("1E2", None),
        (" 1", None),
        ("1,5", None),
        ("1.", None),
        (".5", None),
        ("123456789012345", None),
        ("", None),
        ("NaN", None),
        ("١", None),
        (1, None),
        (2.5, None),
        (None, None),
    ],
)
def test_rate_precision(client, app, admin, value, shown) -> None:
    """设计 §7「精度」：10 位小数通过、11 位 422。

    0、负数、JSON 数字（不是字符串）都是 422；建草稿与改草稿一样。
    """
    draft = new_draft(client, admin)
    before = snapshot(app)

    created = create(client, admin, draft_body(rate=value))
    if shown is None:
        assert (created.status_code, error_code(created)) == (422, "VALIDATION_ERROR")
        assert snapshot(app) == before
    else:
        assert created.status_code == 201, created.text
        assert created.json()["data"]["rate"] == shown
        assert fetched(client, admin, created.json()["data"]["id"])["rate"] == shown

    before = snapshot(app)
    edited = client.patch(f"{RATES}/{draft['id']}", json={"rate": value}, headers=admin)
    if shown is None:
        assert (edited.status_code, error_code(edited)) == (422, "VALIDATION_ERROR")
        assert snapshot(app) == before
    else:
        assert edited.status_code == 200, edited.text
        assert edited.json()["data"]["rate"] == shown


# --- 状态机的 409 与 404 ------------------------------------------------------------


def test_state_machine_refusals(client, app, admin) -> None:
    """已发布：编辑、丢弃 409 `FX_RATE_NOT_DRAFT`。

    已退役 / 已丢弃：一切写操作 409 `FX_RATE_FINAL`。都不写。
    """
    current = published(client, admin)
    discarded = new_draft(client, admin, base_currency="EUR")
    act(client, admin, discarded["id"], "discard")
    retired = published(client, admin, base_currency="GBP")
    retiring = act(client, admin, retired["id"], "retire", {"reason": "Fictional"})
    assert retiring.status_code == 200, retiring.text
    before = snapshot(app)
    reason = {"reason": "Fictional"}

    def patched(version_id: str):
        return client.patch(f"{RATES}/{version_id}", json={"rate": "2.5"}, headers=admin)

    expected = [
        (patched(current["id"]), (409, "FX_RATE_NOT_DRAFT")),
        (act(client, admin, current["id"], "discard"), (409, "FX_RATE_NOT_DRAFT")),
    ]
    for final in (discarded, retired):
        expected += [
            (patched(final["id"]), (409, "FX_RATE_FINAL")),
            (act(client, admin, final["id"], "discard"), (409, "FX_RATE_FINAL")),
            (act(client, admin, final["id"], "publish"), (409, "FX_RATE_FINAL")),
            (act(client, admin, final["id"], "retire", reason), (409, "FX_RATE_FINAL")),
        ]

    actual = [(response.status_code, error_code(response)) for response, _ in expected]
    assert actual == [outcome for _, outcome in expected]
    assert snapshot(app) == before


@pytest.mark.parametrize("unknown", [ZERO_ID, "1", "fetch-attempt"])
def test_an_unknown_version_is_404_everywhere(client, app, admin, unknown: str) -> None:
    """不存在的 public_id、内部自增 id、随手编的串：同一个 404，不写。"""
    new_draft(client, admin)
    before = snapshot(app)
    url = f"{RATES}/{unknown}"

    responses = [
        client.get(url, headers=admin),
        client.patch(url, json={"source_reference": "Edited"}, headers=admin),
        act(client, admin, unknown, "publish"),
        act(client, admin, unknown, "retire", {"reason": "Fictional"}),
        act(client, admin, unknown, "discard"),
    ]

    assert set(outcomes(responses)) == {(404, "FX_RATE_NOT_FOUND")}
    assert snapshot(app) == before


# --- 封闭的请求体 --------------------------------------------------------------------


@pytest.mark.parametrize(
    "overrides",
    [
        {"status": "PUBLISHED"},
        {"source": "BNM"},
        {"source_quote_date": "2026-09-29"},
        {"effective_from": "2999-01-01T00:00:00Z"},
        {"quote_currency": "MYR"},
        {"id": ZERO_ID},
        {"approved_by": 1},
        {"base_currency": "usd"},
        {"base_currency": "US"},
        {"base_currency": "USDX"},
        {"base_currency": 840},
        {"base_currency": None},
        {"source_reference": "   "},
        {"source_reference": "x" * 256},
        {"source_reference": None},
    ],
    ids=[
        "status",
        "source",
        "source_quote_date",
        "effective_from",
        "quote_currency",
        "id",
        "approved_by",
        "currency-lowercase",
        "currency-short",
        "currency-long",
        "currency-number",
        "currency-null",
        "reference-blank",
        "reference-long",
        "reference-null",
    ],
)
def test_the_create_body_is_closed(client, app, admin, overrides: dict) -> None:
    before = snapshot(app)

    response = create(client, admin, draft_body(**overrides))

    assert (response.status_code, error_code(response)) == (422, "VALIDATION_ERROR")
    assert snapshot(app) == before


@pytest.mark.parametrize("missing", ["base_currency", "rate", "observed_at", "source_reference"])
def test_every_create_field_is_required(client, app, admin, missing: str) -> None:
    body = draft_body()
    del body[missing]
    before = snapshot(app)

    response = create(client, admin, body)

    assert (response.status_code, error_code(response)) == (422, "VALIDATION_ERROR")
    assert snapshot(app) == before


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"base_currency": "EUR"},
        {"status": "DISCARDED"},
        {"source": "BNM"},
        {"rate": None},
        {"observed_at": None},
        {"source_reference": None},
        {"source_reference": "Fine", "effective_from": "2999-01-01T00:00:00Z"},
    ],
    ids=[
        "empty",
        "base_currency",
        "status",
        "source",
        "rate-null",
        "observed_at-null",
        "reference-null",
        "valid+effective_from",
    ],
)
def test_the_patch_body_is_closed(client, app, admin, body: dict) -> None:
    """币种建后不可改；状态、区间只由发布与退役改。"""
    draft = new_draft(client, admin)
    before = snapshot(app)

    response = client.patch(f"{RATES}/{draft['id']}", json=body, headers=admin)

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
def test_the_action_bodies_are_closed(client, app, admin, action: str, body: dict) -> None:
    version = published(client, admin)
    target = version if action == "retire" else new_draft(client, admin)
    before = snapshot(app)

    response = act(client, admin, target["id"], action, body)

    assert (response.status_code, error_code(response)) == (422, "VALIDATION_ERROR")
    assert snapshot(app) == before


def test_there_is_no_way_to_delete_a_version(client, app, admin) -> None:
    draft = new_draft(client, admin)
    before = snapshot(app)

    assert client.delete(f"{RATES}/{draft['id']}", headers=admin).status_code == 405
    assert client.put(f"{RATES}/{draft['id']}", json={}, headers=admin).status_code == 405
    assert snapshot(app) == before


# --- 列表 -----------------------------------------------------------------------


def listed(client: TestClient, headers: dict[str, str], url: str, **params: object) -> dict:
    response = client.get(url, params=params, headers=headers)
    assert response.status_code == 200, response.text
    page = response.json()["data"]
    assert set(page) == PAGE_FIELDS
    return page


def test_listing_is_newest_first_and_filtered(client, app, admin) -> None:
    """设计 v3：按 `id` 倒序；可按 `base_currency`、`status`、`source` 筛；分页 §108。"""
    first = published(client, admin)
    euro = new_draft(client, admin, base_currency="EUR")
    bnm = bnm_draft(app)
    discarded = new_draft(client, admin)
    act(client, admin, discarded["id"], "discard")

    def ids(**params: object) -> list[str]:
        page = listed(client, admin, RATES, **params)
        assert all(set(item) == VERSION_FIELDS for item in page["items"])
        return [item["id"] for item in page["items"]]

    assert ids() == [discarded["id"], bnm, euro["id"], first["id"]]
    assert ids(base_currency="EUR") == [euro["id"]]
    assert ids(status="DRAFT") == [bnm, euro["id"]]
    assert ids(status="PUBLISHED") == [first["id"]]
    assert ids(source="BNM") == [bnm]
    assert ids(source="MANUAL", base_currency="USD") == [discarded["id"], first["id"]]
    assert ids(page=2, page_size=3) == [first["id"]]
    page = listed(client, admin, RATES, page=2, page_size=3)
    assert (page["page"], page["page_size"], page["total"]) == (2, 3, 4)


@pytest.mark.parametrize(
    "query",
    [
        "status=BOGUS",
        "status=draft",
        "source=bnm",
        "source=SYNC",
        "base_currency=usd",
        "base_currency=USDX",
        "page=0",
        "page=10001",
        "page_size=0",
        "page_size=101",
    ],
)
def test_listing_bounds(client, admin, query: str) -> None:
    """列表的 `status` / `source` 不是合法取值 → 422（设计 §2「接口」）。"""
    response = client.get(f"{RATES}?{query}", headers=admin)

    assert (response.status_code, error_code(response)) == (422, "VALIDATION_ERROR")


# --- 拉取记录 ---------------------------------------------------------------------


def add_attempts(application: FastAPI, version_id: str) -> None:
    """Attempts as the fetch task (AIH-TASK-040) writes them; out of time order on purpose."""
    day = dt.date(2026, 9, 29)
    at = dt.datetime(2026, 9, 29, 4, 30)
    later = at + dt.timedelta(hours=2)
    weekend = at - dt.timedelta(days=3)
    draft = select(FxRateVersion.id).where(FxRateVersion.public_id == version_id)
    with factory_of(application)() as session:
        version_pk = session.execute(draft).scalar_one()
        rows = [
            # 同一时刻两条：按 id 倒序。
            (at, "USD", FxFetchOutcome.NEW_DRAFT, day, None, version_pk),
            (at, "EUR", FxFetchOutcome.FAILED, None, "TIMEOUT", None),
            (later, "USD", FxFetchOutcome.NO_NEW_QUOTE, day, None, None),
            (weekend, "USD", FxFetchOutcome.NO_QUOTE_FOR_DATE, None, None, None),
        ]
        for attempted_at, currency, outcome, quote_date, error, pointer in rows:
            session.add(
                FxFetchAttempt(
                    base_currency=currency,
                    source=FxRateSourceType.BNM,
                    requested_date=attempted_at.date(),
                    outcome=outcome,
                    quote_date=quote_date,
                    error_code=error,
                    fx_rate_version_id=pointer,
                    attempted_at=attempted_at,
                )
            )
        session.commit()


def test_fetch_attempts_shape_order_and_filters(client, app, admin) -> None:
    """设计 v3：`attempted_at` 倒序、同一时刻 `id` 倒序；可筛币种与 `outcome`。

    每行没有 id；`fx_rate_id` 只在 `NEW_DRAFT` 时是草稿的 public_id。路由先于
    `{fx_rate_id}` 注册：否则这里是 404 `FX_RATE_NOT_FOUND`。
    """
    version_id = bnm_draft(app)
    add_attempts(app, version_id)

    page = listed(client, admin, ATTEMPTS)

    assert page["total"] == 4
    items = page["items"]
    assert all(set(item) == ATTEMPT_FIELDS for item in items)
    assert [(item["attempted_at"], item["outcome"]) for item in items] == [
        ("2026-09-29T06:30:00", "NO_NEW_QUOTE"),
        ("2026-09-29T04:30:00", "FAILED"),
        ("2026-09-29T04:30:00", "NEW_DRAFT"),
        ("2026-09-26T04:30:00", "NO_QUOTE_FOR_DATE"),
    ]
    assert items[2] == {
        "base_currency": "USD",
        "source": "BNM",
        "requested_date": "2026-09-29",
        "outcome": "NEW_DRAFT",
        "quote_date": "2026-09-29",
        "error_code": None,
        "fx_rate_id": version_id,
        "attempted_at": "2026-09-29T04:30:00",
    }
    assert (items[1]["error_code"], items[1]["fx_rate_id"]) == ("TIMEOUT", None)
    assert [item["fx_rate_id"] for item in (items[0], items[3])] == [None, None]

    euro = listed(client, admin, ATTEMPTS, base_currency="EUR")["items"]
    assert [item["outcome"] for item in euro] == ["FAILED"]
    failed = listed(client, admin, ATTEMPTS, outcome="NO_QUOTE_FOR_DATE")["items"]
    assert [item["attempted_at"] for item in failed] == ["2026-09-26T04:30:00"]
    second = listed(client, admin, ATTEMPTS, base_currency="USD", page=2, page_size=2)
    assert [item["outcome"] for item in second["items"]] == ["NO_QUOTE_FOR_DATE"]
    assert second["total"] == 3


@pytest.mark.parametrize(
    "query",
    ["outcome=BOGUS", "outcome=failed", "base_currency=eur", "page=0", "page_size=101"],
)
def test_fetch_attempt_bounds(client, admin, query: str) -> None:
    response = client.get(f"{ATTEMPTS}?{query}", headers=admin)

    assert (response.status_code, error_code(response)) == (422, "VALIDATION_ERROR")


# --- 读接口不拿全局锁、响应字段与鉴权 ----------------------------------------------------


def test_reads_never_take_the_fx_lock(client, app, admin, monkeypatch) -> None:
    """设计 v3：列表、详情、拉取记录用普通读，不拿 `fx_rate_locks`（不让发布等它们）。"""
    version = published(client, admin)
    add_attempts(app, bnm_draft(app))

    def refuse(session) -> None:
        raise AssertionError("a read endpoint took the FX lock")

    monkeypatch.setattr(fx_repository, "lock_fx_rates", refuse)
    monkeypatch.setattr(fx_repository, "lock_fx_rates_shared", refuse)

    assert listed(client, admin, RATES)["total"] == 2
    assert fetched(client, admin, version["id"]) == version
    assert listed(client, admin, ATTEMPTS)["total"] == 4


def test_no_response_carries_an_internal_id(client, app, admin) -> None:
    """设计 §6：对外的 id 都是 public_id；没有自增 id、`open_slot`、内部用户 id。"""
    version = published(client, admin)
    draft = new_draft(client, admin, base_currency="EUR")
    add_attempts(app, bnm_draft(app))
    bodies = [
        version,
        draft,
        fetched(client, admin, version["id"]),
        *client.get(RATES, headers=admin).json()["data"]["items"],
    ]

    for body in bodies:
        assert set(body) == VERSION_FIELDS
        assert str(uuid.UUID(body["id"])) == body["id"]
        text = json.dumps(body)
        for internal in ("open_slot", 'created_by"', 'approved_by"'):
            assert internal not in text
    for item in client.get(ATTEMPTS, headers=admin).json()["data"]["items"]:
        assert set(item) == ATTEMPT_FIELDS
        assert "fx_rate_version_id" not in json.dumps(item)


ROUTES = [
    ("GET", RATES, None),
    ("POST", RATES, draft_body()),
    ("GET", ATTEMPTS, None),
    ("GET", f"{RATES}/{{id}}", None),
    ("PATCH", f"{RATES}/{{id}}", {"rate": "2.5"}),
    ("POST", f"{RATES}/{{id}}/publish", {}),
    ("POST", f"{RATES}/{{id}}/retire", {"reason": "Fictional"}),
    ("POST", f"{RATES}/{{id}}/discard", {}),
]
ROUTE_IDS = [f"{method} {path}" for method, path, _ in ROUTES]


@pytest.mark.parametrize("caller", ["anonymous", "customer"])
@pytest.mark.parametrize(("method", "path", "body"), ROUTES, ids=ROUTE_IDS)
def test_non_admins_are_refused(client, app, admin, method, path, body, caller) -> None:
    """设计 §7「鉴权」：匿名 401、CUSTOMER 403，且不写库。

    路径填真实的草稿：漏了鉴权的处理函数会真的成功，而不是碰巧 404。
    """
    draft = new_draft(client, admin)
    url = path.replace("{id}", draft["id"])
    if caller == "anonymous":
        headers: dict[str, str] = {}
    else:
        headers = token_for(app, make_user(app, UserRole.CUSTOMER)[0], "CUSTOMER")
    before = snapshot(app)

    response = client.request(method, url, json=body, headers=headers)

    expected = (401, "TOKEN_INVALID") if caller == "anonymous" else (403, "ADMIN_REQUIRED")
    assert (response.status_code, error_code(response)) == expected
    assert snapshot(app) == before


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
    # 八个接口；少了说明 AST 扫描落空了。
    assert len(handlers) == 8

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

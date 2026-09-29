"""AI providers, models and model aliases end to end on SQLite (design gate #163 v4 §7).

这里测接口契约：正常路径与每一步的审计、字段规则与边界值、不可改的代码、无变化不写、
映射的三种情形与撤销、代码与别名不撞、跨供应商 404、分页与筛选、响应里没有内部 id，
以及「每个处理函数第一条语句是 `require_admin`」（AST）。

依赖时钟的分段语义（边界时刻、改指向不影响过去、撤销后重新映射、性质用例）、事务中途失败的
回滚、数据库约束与并发都在 test_ai_catalog_resolve.py。匿名 401 / CUSTOMER 403 且不写库在
test_admin_customers_api.py（从路由表枚举全部管理端路由）。
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
from app.models.ai_catalog import AiModel, AiModelAlias, AiProvider
from app.models.auth import AuditAction, AuditLog, User, UserRole, UserStatus
from app.models.base import Base
from app.services.auth import utc_now

PROVIDERS = "/api/v1/admin/ai-providers"
ROUTER_FILE = pathlib.Path("app/api/admin_ai_catalog.py")

# 响应字段白名单（设计 §2）。断言「恰好等于」：多一个键（内部 id）就红。
PROVIDER_FIELDS = {"id", "code", "display_name", "status", "created_at", "updated_at"}
MODEL_FIELDS = {
    "id",
    "provider_id",
    "provider_code",
    "code",
    "display_name",
    "status",
    "created_at",
    "updated_at",
}
MODEL_DETAIL_FIELDS = MODEL_FIELDS | {"aliases"}
SEGMENT_FIELDS = {
    "id",
    "alias",
    "model_id",
    "model_code",
    "effective_from",
    "effective_to",
    "created_at",
    "closed_at",
}
PAGE_FIELDS = {"items", "page", "page_size", "total"}
ENVELOPE_FIELDS = {"success", "data", "error", "request_id"}

# 全零占位值（secret-scan）。
ZERO_ID = "00000000-0000-4000-8000-000000000000"

TIME_FORMAT = "%Y-%m-%dT%H:%M:%S"

# 快照里每张表比哪几列。
SNAPSHOT_COLUMNS = {
    AiProvider: (
        AiProvider.code,
        AiProvider.display_name,
        AiProvider.status,
        AiProvider.updated_at,
    ),
    AiModel: (
        AiModel.provider_id,
        AiModel.code,
        AiModel.display_name,
        AiModel.status,
        AiModel.updated_at,
    ),
    AiModelAlias: (
        AiModelAlias.provider_id,
        AiModelAlias.model_id,
        AiModelAlias.alias,
        AiModelAlias.effective_from,
        AiModelAlias.effective_to,
        AiModelAlias.closed_at,
    ),
}


# --- 夹具与帮手 -----------------------------------------------------------------


def settings_for(tmp_path) -> Settings:
    key = tmp_path / "jwt.key"
    key.write_text("a-test-signing-key-that-is-long-enough", encoding="utf-8")
    master = tmp_path / "master.key"
    master.write_text(f"1:{base64.b64encode(os.urandom(32)).decode()}\n", encoding="utf-8")
    return Settings(jwt_secret_file=str(key), master_key_file=str(master), database_url="")


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


@pytest.fixture
def admin_id(app) -> int:
    now = utc_now()
    with app.state.session_factory() as session:
        user = User(
            email=f"{uuid.uuid4().hex}@example.com",
            password_hash="not-a-real-hash",
            role=UserRole.ADMIN,
            status=UserStatus.ACTIVE,
            created_at=now,
            updated_at=now,
        )
        session.add(user)
        session.commit()
        return int(user.id)


@pytest.fixture
def admin(app, admin_id) -> dict[str, str]:
    token = issue_access_token(
        app.state.settings,
        user_id=admin_id,
        role="ADMIN",
        session_id=uuid.uuid4().hex,
        now=utc_now(),
    )
    return {"Authorization": f"Bearer {token}"}


def factory_of(application: FastAPI) -> sessionmaker[Session]:
    return application.state.session_factory


def error_code(response) -> str:
    body = response.json()
    assert set(body) == ENVELOPE_FIELDS
    assert body["success"] is False
    assert body["data"] is None
    return body["error"]["code"]


def snapshot(application: FastAPI) -> dict[str, object]:
    """Everything a catalog write may change, as committed, plus the audit count."""
    stored: dict[str, object] = {}
    with factory_of(application)() as session:
        for model, columns in SNAPSHOT_COLUMNS.items():
            statement = select(*columns).order_by(model.id)
            stored[model.__tablename__] = [tuple(row) for row in session.execute(statement)]
        count = select(func.count()).select_from(AuditLog)
        stored["audits"] = session.execute(count).scalar_one()
    return stored


def audits(application: FastAPI, action: AuditAction) -> list[AuditLog]:
    statement = select(AuditLog).where(AuditLog.action == action).order_by(AuditLog.id)
    with factory_of(application)() as session:
        return list(session.execute(statement).scalars())


def states(audit: AuditLog) -> tuple[object, object]:
    before = json.loads(audit.before_state) if audit.before_state else None
    after = json.loads(audit.after_state) if audit.after_state else None
    return before, after


def new_provider(client: TestClient, headers: dict[str, str], code: str = "anthropic") -> dict:
    response = client.post(PROVIDERS, json={"code": code, "display_name": code}, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()["data"]


def new_model(
    client: TestClient, headers: dict[str, str], provider_id: str, code: str = "claude-x"
) -> dict:
    response = client.post(
        f"{PROVIDERS}/{provider_id}/models",
        json={"code": code, "display_name": code},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]


def map_alias(
    client: TestClient, headers: dict[str, str], provider_id: str, alias: str, model_id: str
):
    return client.post(
        f"{PROVIDERS}/{provider_id}/model-aliases",
        json={"alias": alias, "model_id": model_id},
        headers=headers,
    )


def retire(client: TestClient, headers: dict[str, str], provider_id: str, alias_id: str):
    return client.post(
        f"{PROVIDERS}/{provider_id}/model-aliases/{alias_id}/retire", json={}, headers=headers
    )


def moment(text: str | None) -> dt.datetime | None:
    return dt.datetime.fromisoformat(text) if text is not None else None


def outcomes(responses) -> list[tuple[int, str]]:
    return [(response.status_code, error_code(response)) for response in responses]


# --- 正常路径（设计 §7「正常路径」） -------------------------------------------------


def test_provider_model_alias_rename_retire_and_reuse(client, app, admin, admin_id) -> None:
    """建供应商 → 建模型 → 映射别名 → 改名 → 停用 → 复用；每次真实变化一条审计。"""
    headers = {**admin, "User-Agent": "admin-console-test"}
    provider_response = client.post(
        PROVIDERS, json={"code": "anthropic", "display_name": "  Anthropic  "}, headers=headers
    )
    assert provider_response.status_code == 201
    provider = provider_response.json()["data"]
    assert set(provider) == PROVIDER_FIELDS
    assert (provider["code"], provider["display_name"], provider["status"]) == (
        "anthropic",
        "Anthropic",
        "ACTIVE",
    )
    assert str(uuid.UUID(provider["id"])) == provider["id"]

    model = new_model(client, headers, provider["id"], "claude-sonnet-4-5")
    assert set(model) == MODEL_FIELDS
    assert (model["provider_id"], model["provider_code"], model["status"]) == (
        provider["id"],
        "anthropic",
        "ACTIVE",
    )

    mapped = map_alias(client, headers, provider["id"], "claude-sonnet-4-5-20250929", model["id"])
    assert mapped.status_code == 201
    segment = mapped.json()["data"]
    assert set(segment) == SEGMENT_FIELDS
    # 第一次映射：`[NULL, NULL)`，对过去全部生效。
    assert (segment["effective_from"], segment["effective_to"], segment["closed_at"]) == (
        None,
        None,
        None,
    )
    assert (segment["model_id"], segment["model_code"]) == (model["id"], "claude-sonnet-4-5")

    provider_url = f"{PROVIDERS}/{provider['id']}"
    model_url = f"{provider_url}/models/{model['id']}"
    edits = [
        (provider_url, {"display_name": "Anthropic PBC"}),
        (model_url, {"display_name": "Claude Sonnet 4.5"}),
        (provider_url, {"status": "RETIRED"}),
        (model_url, {"status": "RETIRED"}),
        (provider_url, {"status": "ACTIVE"}),
        (model_url, {"status": "ACTIVE"}),
    ]
    for url, body in edits:
        response = client.patch(url, json=body, headers=headers)
        assert response.status_code == 200, response.text
        for name, value in body.items():
            assert response.json()["data"][name] == value

    [provider_create] = audits(app, AuditAction.AI_PROVIDER_CREATE)
    assert provider_create.entity_type == "ai_provider"
    assert provider_create.entity_id == provider["id"]
    assert states(provider_create) == (
        None,
        {"code": "anthropic", "display_name": "Anthropic", "status": "ACTIVE"},
    )
    [model_create] = audits(app, AuditAction.AI_MODEL_CREATE)
    assert (model_create.entity_type, model_create.entity_id) == ("ai_model", model["id"])
    assert states(model_create) == (
        None,
        {
            "provider_code": "anthropic",
            "code": "claude-sonnet-4-5",
            "display_name": "claude-sonnet-4-5",
            "status": "ACTIVE",
        },
    )
    [alias_map] = audits(app, AuditAction.AI_MODEL_ALIAS_MAP)
    assert (alias_map.entity_type, alias_map.entity_id) == ("ai_model_alias", segment["id"])
    # 指向一律用模型 code，不用内部 id（设计 §2「审计」）。
    assert states(alias_map) == (
        None,
        {
            "provider_code": "anthropic",
            "alias": "claude-sonnet-4-5-20250929",
            "model_code": "claude-sonnet-4-5",
            "effective_from": None,
        },
    )
    provider_updates = audits(app, AuditAction.AI_PROVIDER_UPDATE)
    assert [states(audit) for audit in provider_updates] == [
        ({"display_name": "Anthropic"}, {"display_name": "Anthropic PBC"}),
        ({"status": "ACTIVE"}, {"status": "RETIRED"}),
        ({"status": "RETIRED"}, {"status": "ACTIVE"}),
    ]
    model_updates = audits(app, AuditAction.AI_MODEL_UPDATE)
    assert [states(audit) for audit in model_updates] == [
        ({"display_name": "claude-sonnet-4-5"}, {"display_name": "Claude Sonnet 4.5"}),
        ({"status": "ACTIVE"}, {"status": "RETIRED"}),
        ({"status": "RETIRED"}, {"status": "ACTIVE"}),
    ]
    for audit in [provider_create, model_create, alias_map, *provider_updates, *model_updates]:
        assert (audit.actor_user_id, audit.actor_role) == (admin_id, "ADMIN")
        assert audit.user_agent == "admin-console-test"

    # 模型详情带着当前指向它的别名段。
    detail = client.get(model_url, headers=admin).json()["data"]
    assert set(detail) == MODEL_DETAIL_FIELDS
    assert detail["aliases"] == [segment]
    assert detail["display_name"] == "Claude Sonnet 4.5"


def test_an_edit_that_changes_nothing_writes_nothing(client, app, admin) -> None:
    """设计 §7「幂等」：PATCH 无变化 200，不写审计，`updated_at` 不变。"""
    provider = new_provider(client, admin)
    model = new_model(client, admin, provider["id"])
    before = snapshot(app)

    same_provider = client.patch(
        f"{PROVIDERS}/{provider['id']}",
        json={"display_name": " anthropic ", "status": "ACTIVE"},
        headers=admin,
    )
    same_model = client.patch(
        f"{PROVIDERS}/{provider['id']}/models/{model['id']}",
        json={"display_name": "claude-x"},
        headers=admin,
    )

    assert (same_provider.status_code, same_model.status_code) == (200, 200)
    assert same_provider.json()["data"] == provider
    assert same_model.json()["data"] == model
    assert snapshot(app) == before


# --- 映射与撤销 ---------------------------------------------------------------------


def test_mapping_to_the_same_model_again_writes_nothing(client, app, admin) -> None:
    """设计 §7「指向相同」：200，不写段、不写审计。"""
    provider = new_provider(client, admin)
    model = new_model(client, admin, provider["id"])
    first = map_alias(client, admin, provider["id"], "claude-x-latest", model["id"])
    before = snapshot(app)

    again = map_alias(client, admin, provider["id"], "claude-x-latest", model["id"])

    assert again.status_code == 200
    assert again.json()["data"] == first.json()["data"]
    assert snapshot(app) == before


def test_remapping_truncates_the_current_segment_from_a_later_second(client, app, admin) -> None:
    """改指向：当前段截断于 `t`（晚于此刻的下一个整秒），新段从 `t` 起；旧段指向不变。"""
    provider = new_provider(client, admin)
    old = new_model(client, admin, provider["id"], "claude-x")
    new = new_model(client, admin, provider["id"], "claude-y")
    first = map_alias(client, admin, provider["id"], "claude-latest", old["id"]).json()["data"]
    started = utc_now()

    response = map_alias(client, admin, provider["id"], "claude-latest", new["id"])

    assert response.status_code == 201
    current = response.json()["data"]
    boundary = moment(current["effective_from"])
    assert boundary is not None
    assert boundary > started
    assert boundary.microsecond == 0
    assert current["effective_to"] is None
    assert (current["model_id"], current["model_code"]) == (new["id"], "claude-y")

    listing = client.get(f"{PROVIDERS}/{provider['id']}/model-aliases", headers=admin)
    truncated, reopened = listing.json()["data"]["items"]
    assert reopened == current
    assert truncated["id"] == first["id"]
    assert (truncated["model_id"], truncated["effective_from"]) == (old["id"], None)
    assert truncated["effective_to"] == current["effective_from"]
    assert truncated["closed_at"] is not None

    [_, remap] = audits(app, AuditAction.AI_MODEL_ALIAS_MAP)
    assert remap.entity_id == current["id"]
    assert states(remap) == (
        {"model_code": "claude-x", "effective_to": None},
        {
            "provider_code": "anthropic",
            "alias": "claude-latest",
            "model_code": "claude-y",
            "effective_from": boundary.strftime(TIME_FORMAT),
        },
    )


def test_retiring_truncates_and_a_later_map_fills_the_gap(client, app, admin) -> None:
    """撤销：截断于 `t`；再映射从撤销那一刻接上（设计 §2「三个操作」）。"""
    provider = new_provider(client, admin)
    model = new_model(client, admin, provider["id"])
    segment = map_alias(client, admin, provider["id"], "claude-x-latest", model["id"]).json()
    alias_id = segment["data"]["id"]

    response = retire(client, admin, provider["id"], alias_id)

    assert response.status_code == 200
    retired = response.json()["data"]
    assert set(retired) == SEGMENT_FIELDS
    assert retired["id"] == alias_id
    ended = moment(retired["effective_to"])
    assert ended is not None
    assert ended.microsecond == 0
    assert retired["closed_at"] is not None
    [audit] = audits(app, AuditAction.AI_MODEL_ALIAS_RETIRE)
    assert (audit.entity_type, audit.entity_id) == ("ai_model_alias", alias_id)
    assert states(audit) == ({"effective_to": None}, {"effective_to": ended.strftime(TIME_FORMAT)})
    # 模型详情里不再有这一段（只列未截断的）。
    detail = client.get(f"{PROVIDERS}/{provider['id']}/models/{model['id']}", headers=admin)
    assert detail.json()["data"]["aliases"] == []

    remapped = map_alias(client, admin, provider["id"], "claude-x-latest", model["id"])

    assert remapped.status_code == 201
    assert remapped.json()["data"]["effective_from"] == retired["effective_to"]
    [_, fill] = audits(app, AuditAction.AI_MODEL_ALIAS_MAP)
    # 没有被截断的段：前状态为空。
    assert states(fill)[0] is None


def test_retiring_anything_but_the_current_segment_is_404(client, app, admin) -> None:
    """设计 §7「撤销不存在的段」：已截断的段、别家的段、不存在的 id，都是 404，不写。"""
    provider = new_provider(client, admin)
    other = new_provider(client, admin, "openai")
    model = new_model(client, admin, provider["id"])
    first = map_alias(client, admin, provider["id"], "claude-x-latest", model["id"]).json()
    theirs_model = new_model(client, admin, other["id"], "gpt-x")
    theirs = map_alias(client, admin, other["id"], "gpt-x-latest", theirs_model["id"]).json()
    retire(client, admin, provider["id"], first["data"]["id"])
    before = snapshot(app)

    responses = [
        retire(client, admin, provider["id"], first["data"]["id"]),
        retire(client, admin, provider["id"], theirs["data"]["id"]),
        retire(client, admin, provider["id"], ZERO_ID),
        retire(client, admin, provider["id"], "1"),
    ]

    assert outcomes(responses) == [(404, "AI_MODEL_ALIAS_NOT_FOUND")] * len(responses)
    assert snapshot(app) == before


def test_there_is_no_way_to_edit_a_segment(client, app, admin) -> None:
    """设计 §2：别名段没有 PATCH，改映射只能再 POST 一次。"""
    provider = new_provider(client, admin)
    model = new_model(client, admin, provider["id"])
    segment = map_alias(client, admin, provider["id"], "claude-x-latest", model["id"]).json()
    before = snapshot(app)
    collection = f"{PROVIDERS}/{provider['id']}/model-aliases"
    body = {"model_id": model["id"]}

    for method in ("PATCH", "PUT", "DELETE"):
        # 段本身没有任何路由；集合上只有 GET 与 POST。
        item = client.request(method, f"{collection}/{segment['data']['id']}", json=body)
        assert item.status_code == 404, method
        assert client.request(method, collection, json=body, headers=admin).status_code == 405

    assert snapshot(app) == before


# --- 代码与别名不撞（设计 §7） -------------------------------------------------------


def test_a_model_code_cannot_be_a_string_that_was_ever_an_alias(client, app, admin) -> None:
    provider = new_provider(client, admin)
    model = new_model(client, admin, provider["id"])
    current = map_alias(client, admin, provider["id"], "claude-current", model["id"]).json()
    old = map_alias(client, admin, provider["id"], "claude-old", model["id"]).json()
    retire(client, admin, provider["id"], old["data"]["id"])
    assert current["data"]["effective_to"] is None
    before = snapshot(app)

    for code in ("claude-current", "claude-old"):
        response = client.post(
            f"{PROVIDERS}/{provider['id']}/models",
            json={"code": code, "display_name": "Clash"},
            headers=admin,
        )
        assert (response.status_code, error_code(response)) == (409, "AI_MODEL_CODE_TAKEN")

    assert snapshot(app) == before


def test_an_alias_cannot_be_a_model_code(client, app, admin) -> None:
    provider = new_provider(client, admin)
    target = new_model(client, admin, provider["id"], "claude-x")
    new_model(client, admin, provider["id"], "claude-y")
    before = snapshot(app)

    response = map_alias(client, admin, provider["id"], "claude-y", target["id"])

    assert (response.status_code, error_code(response)) == (409, "AI_MODEL_ALIAS_TAKEN")
    assert snapshot(app) == before


def test_the_collision_rules_are_per_provider(client, admin) -> None:
    """另一个供应商下同名的模型与别名互不相干。"""
    first = new_provider(client, admin, "anthropic")
    second = new_provider(client, admin, "openai")
    model = new_model(client, admin, first["id"], "shared-name")
    theirs = new_model(client, admin, second["id"], "other")

    assert map_alias(client, admin, second["id"], "shared-name", theirs["id"]).status_code == 201
    assert map_alias(client, admin, first["id"], "other", model["id"]).status_code == 201


# --- 跨供应商与不存在的 id -------------------------------------------------------------


def test_another_providers_model_is_not_found(client, app, admin) -> None:
    """设计 §7「别名跨供应商」：映射到别家的模型 404，不泄露它存在；路径里的也一样。"""
    mine = new_provider(client, admin, "anthropic")
    theirs = new_provider(client, admin, "openai")
    their_model = new_model(client, admin, theirs["id"], "gpt-x")
    before = snapshot(app)
    model_url = f"{PROVIDERS}/{mine['id']}/models/{their_model['id']}"

    responses = [
        map_alias(client, admin, mine["id"], "gpt-x-latest", their_model["id"]),
        map_alias(client, admin, mine["id"], "gpt-x-latest", ZERO_ID),
        client.get(model_url, headers=admin),
        client.patch(model_url, json={"status": "RETIRED"}, headers=admin),
    ]

    assert outcomes(responses) == [(404, "AI_MODEL_NOT_FOUND")] * len(responses)
    assert snapshot(app) == before


@pytest.mark.parametrize("unknown", [ZERO_ID, "1", "anthropic"])
def test_an_unknown_provider_is_404_everywhere(client, app, admin, unknown: str) -> None:
    """不存在的 public_id、内部自增 id、供应商代码：同一个 404，不写。"""
    provider = new_provider(client, admin)
    model = new_model(client, admin, provider["id"])
    before = snapshot(app)
    base = f"{PROVIDERS}/{unknown}"

    responses = [
        client.get(base, headers=admin),
        client.patch(base, json={"status": "RETIRED"}, headers=admin),
        client.get(f"{base}/models", headers=admin),
        client.post(f"{base}/models", json={"code": "m", "display_name": "m"}, headers=admin),
        client.get(f"{base}/models/{model['id']}", headers=admin),
        client.patch(f"{base}/models/{model['id']}", json={"status": "RETIRED"}, headers=admin),
        client.get(f"{base}/model-aliases", headers=admin),
        map_alias(client, admin, unknown, "claude-x-latest", model["id"]),
        retire(client, admin, unknown, ZERO_ID),
    ]

    assert set(outcomes(responses)) == {(404, "AI_PROVIDER_NOT_FOUND")}
    assert snapshot(app) == before


@pytest.mark.parametrize("unknown", [ZERO_ID, "1", "claude-x"])
def test_an_unknown_model_is_404(client, app, admin, unknown: str) -> None:
    provider = new_provider(client, admin)
    new_model(client, admin, provider["id"])
    before = snapshot(app)
    url = f"{PROVIDERS}/{provider['id']}/models/{unknown}"

    responses = [
        client.get(url, headers=admin),
        client.patch(url, json={"status": "RETIRED"}, headers=admin),
    ]

    assert set(outcomes(responses)) == {(404, "AI_MODEL_NOT_FOUND")}
    assert snapshot(app) == before


# --- 代码唯一 -----------------------------------------------------------------------


def test_taken_codes_are_409_and_write_nothing(client, app, admin) -> None:
    provider = new_provider(client, admin, "anthropic")
    new_model(client, admin, provider["id"], "claude-x")
    before = snapshot(app)

    same_provider = client.post(
        PROVIDERS, json={"code": "anthropic", "display_name": "Again"}, headers=admin
    )
    same_model = client.post(
        f"{PROVIDERS}/{provider['id']}/models",
        json={"code": "claude-x", "display_name": "Again"},
        headers=admin,
    )

    assert outcomes([same_provider, same_model]) == [
        (409, "AI_PROVIDER_CODE_TAKEN"),
        (409, "AI_MODEL_CODE_TAKEN"),
    ]
    assert snapshot(app) == before


def test_codes_are_compared_exactly(client, admin) -> None:
    """不做大小写或其他规范化：只差大小写的两个模型代码是两个模型（设计 §9）。"""
    provider = new_provider(client, admin, "openai")

    lower = new_model(client, admin, provider["id"], "gpt-4o")
    upper = new_model(client, admin, provider["id"], "GPT-4o")

    assert lower["id"] != upper["id"]
    listing = client.get(f"{PROVIDERS}/{provider['id']}/models", headers=admin).json()["data"]
    assert sorted(item["code"] for item in listing["items"]) == ["GPT-4o", "gpt-4o"]


# --- 字段规则与边界值（设计 §7「边界值」） ------------------------------------------------


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("anthropic", 201),
        ("open-ai_2", 201),
        ("a", 201),
        ("0", 201),
        ("a" * 64, 201),
        ("a" * 65, 422),
        ("Anthropic", 422),
        ("-anthropic", 422),
        ("_anthropic", 422),
        ("anthropic ai", 422),
        (" anthropic", 422),
        ("anthropic\n", 422),
        ("anthropic.ai", 422),
        ("", 422),
    ],
)
def test_provider_code_rules(client, app, admin, code: str, expected: int) -> None:
    """`^[a-z0-9][a-z0-9_-]{0,63}$`：64 字符以内、小写、不含空白。"""
    before = snapshot(app)

    response = client.post(PROVIDERS, json={"code": code, "display_name": "P"}, headers=admin)

    assert response.status_code == expected, response.text
    if expected == 422:
        assert error_code(response) == "VALIDATION_ERROR"
        assert snapshot(app) == before
    else:
        assert response.json()["data"]["code"] == code


MODEL_CODE_CASES = [
    ("claude-sonnet-4-5-20250929", 201),
    ("gpt-4o-mini-transcribe", 201),
    ("models/gemini-x", 201),
    ("org:model@v1.2", 201),
    ("GPT-4o", 201),
    ("m" * 128, 201),
    ("m" * 129, 422),
    ("claude x", 422),
    ("claude\tx", 422),
    ("claude-x\n", 422),
    (" claude-x", 422),
    ("-claude", 422),
    (".claude", 422),
    ("/models", 422),
    ("claude#x", 422),
    ("", 422),
]


@pytest.mark.parametrize(("code", "expected"), MODEL_CODE_CASES)
def test_model_code_rules(client, app, admin, code: str, expected: int) -> None:
    """`^[A-Za-z0-9][A-Za-z0-9._:/@-]{0,127}$`：128 字符以内、不含空白。"""
    provider = new_provider(client, admin)
    before = snapshot(app)

    response = client.post(
        f"{PROVIDERS}/{provider['id']}/models",
        json={"code": code, "display_name": "M"},
        headers=admin,
    )

    assert response.status_code == expected, response.text
    if expected == 422:
        assert error_code(response) == "VALIDATION_ERROR"
        assert snapshot(app) == before
    else:
        assert response.json()["data"]["code"] == code


@pytest.mark.parametrize(("alias", "expected"), MODEL_CODE_CASES)
def test_alias_rules(client, app, admin, alias: str, expected: int) -> None:
    """别名与模型代码同一条规则。"""
    provider = new_provider(client, admin)
    model = new_model(client, admin, provider["id"], "target-model")
    before = snapshot(app)

    response = map_alias(client, admin, provider["id"], alias, model["id"])

    assert response.status_code == expected, response.text
    if expected == 422:
        assert error_code(response) == "VALIDATION_ERROR"
        assert snapshot(app) == before
    else:
        assert response.json()["data"]["alias"] == alias


@pytest.mark.parametrize(
    ("display_name", "expected"),
    [("", 422), ("   ", 422), ("x" * 256, 422), (None, 422), ("x" * 255, 201), (" x ", 201)],
)
def test_display_name_rules(client, app, admin, display_name, expected: int) -> None:
    """去首尾空白后 1–255。"""
    before = snapshot(app)

    response = client.post(
        PROVIDERS, json={"code": "anthropic", "display_name": display_name}, headers=admin
    )

    assert response.status_code == expected
    if expected == 422:
        assert snapshot(app) == before
    else:
        assert response.json()["data"]["display_name"] == display_name.strip()


@pytest.mark.parametrize(
    "body",
    [
        {"code": "anthropic", "display_name": "A", "status": "RETIRED"},
        {"code": "anthropic", "display_name": "A", "id": ZERO_ID},
        {"code": "anthropic", "display_name": "A", "public_id": ZERO_ID},
        {"code": "anthropic"},
        {"display_name": "A"},
    ],
    ids=["status", "id", "public_id", "no-name", "no-code"],
)
def test_the_provider_body_is_closed(client, app, admin, body: dict) -> None:
    before = snapshot(app)

    response = client.post(PROVIDERS, json=body, headers=admin)

    assert (response.status_code, error_code(response)) == (422, "VALIDATION_ERROR")
    assert snapshot(app) == before


@pytest.mark.parametrize(
    "body",
    [
        {"code": "m", "display_name": "M", "provider_id": ZERO_ID},
        {"code": "m", "display_name": "M", "status": "RETIRED"},
        {"alias": "m-latest", "model_id": ZERO_ID, "effective_from": "2026-01-01T00:00:00"},
        {"alias": "m-latest", "model_id": ZERO_ID, "provider_id": ZERO_ID},
        {"alias": "m-latest"},
        {"model_id": ZERO_ID},
    ],
    ids=[
        "model-provider_id",
        "model-status",
        "alias-effective_from",
        "alias-provider_id",
        "alias-no-model",
        "alias-no-alias",
    ],
)
def test_model_and_alias_bodies_are_closed(client, app, admin, body: dict) -> None:
    """供应商只来自路径；段的起点只由服务端定。"""
    provider = new_provider(client, admin)
    before = snapshot(app)
    path = "models" if "code" in body else "model-aliases"

    response = client.post(f"{PROVIDERS}/{provider['id']}/{path}", json=body, headers=admin)

    assert (response.status_code, error_code(response)) == (422, "VALIDATION_ERROR")
    assert snapshot(app) == before


def test_retire_takes_an_empty_body_only(client, app, admin) -> None:
    provider = new_provider(client, admin)
    model = new_model(client, admin, provider["id"])
    segment = map_alias(client, admin, provider["id"], "claude-x-latest", model["id"]).json()
    url = f"{PROVIDERS}/{provider['id']}/model-aliases/{segment['data']['id']}/retire"
    before = snapshot(app)

    response = client.post(url, json={"effective_to": "2026-01-01T00:00:00"}, headers=admin)

    assert (response.status_code, error_code(response)) == (422, "VALIDATION_ERROR")
    assert snapshot(app) == before


@pytest.mark.parametrize("target", ["provider", "model"])
@pytest.mark.parametrize(
    "body",
    [
        {"code": "renamed"},
        {"provider_id": ZERO_ID},
        {"alias": "renamed"},
        {"display_name": "Fine", "code": "renamed"},
        {"status": "RETIRED", "provider_id": ZERO_ID},
        {"unit": "TOKEN"},
        {},
        {"display_name": None},
        {"status": None},
        {"status": "DISABLED"},
        {"display_name": "  "},
    ],
    ids=[
        "code",
        "provider_id",
        "alias",
        "valid+code",
        "valid+provider_id",
        "unit",
        "empty",
        "name-null",
        "status-null",
        "status-unknown",
        "name-blank",
    ],
)
def test_patch_refuses_immutable_and_bad_fields(
    client, app, admin, target: str, body: dict
) -> None:
    """设计 §7「不可改代码」：PATCH 带 `code` / `alias` / `provider_id` 是 422，不写。"""
    provider = new_provider(client, admin)
    model = new_model(client, admin, provider["id"])
    url = f"{PROVIDERS}/{provider['id']}"
    if target == "model":
        url += f"/models/{model['id']}"
    before = snapshot(app)

    response = client.patch(url, json=body, headers=admin)

    assert (response.status_code, error_code(response)) == (422, "VALIDATION_ERROR")
    assert snapshot(app) == before


# --- 列表 -----------------------------------------------------------------------


def test_providers_and_models_are_listed_by_code_and_filtered_by_status(client, admin) -> None:
    codes = ["openai", "anthropic", "google", "azure"]
    providers = {code: new_provider(client, admin, code) for code in codes}
    retired = {"status": "RETIRED"}
    client.patch(f"{PROVIDERS}/{providers['google']['id']}", json=retired, headers=admin)
    anthropic = providers["anthropic"]["id"]
    for code in ("claude-z", "claude-a", "claude-m"):
        new_model(client, admin, anthropic, code)
    new_model(client, admin, providers["openai"]["id"], "gpt-x")
    models_url = f"{PROVIDERS}/{anthropic}/models"
    old = new_model(client, admin, anthropic, "claude-old")
    client.patch(f"{models_url}/{old['id']}", json=retired, headers=admin)

    def codes_of(url: str, **params: object) -> list[str]:
        response = client.get(url, params=params, headers=admin)
        assert response.status_code == 200
        page = response.json()["data"]
        assert set(page) == PAGE_FIELDS
        return [item["code"] for item in page["items"]]

    assert codes_of(PROVIDERS) == sorted(codes)
    assert codes_of(PROVIDERS, status="RETIRED") == ["google"]
    assert codes_of(PROVIDERS, status="ACTIVE") == ["anthropic", "azure", "openai"]
    assert codes_of(PROVIDERS, page=2, page_size=3) == ["openai"]
    # 只有这个供应商的模型。
    assert codes_of(models_url) == ["claude-a", "claude-m", "claude-old", "claude-z"]
    assert codes_of(models_url, status="RETIRED") == ["claude-old"]
    assert codes_of(models_url, status="ACTIVE", page_size=2) == ["claude-a", "claude-m"]


def test_alias_history_is_listed_and_filtered(client, admin) -> None:
    """全部历史，按 `alias`、`effective_from` 排序；可按字符串筛、可只看未截断的段。"""
    provider = new_provider(client, admin)
    first = new_model(client, admin, provider["id"], "claude-x")
    second = new_model(client, admin, provider["id"], "claude-y")
    pid = provider["id"]
    map_alias(client, admin, pid, "zeta", first["id"])
    map_alias(client, admin, pid, "alpha", first["id"])
    map_alias(client, admin, pid, "alpha", second["id"])
    beta = map_alias(client, admin, pid, "beta", second["id"]).json()["data"]
    retire(client, admin, pid, beta["id"])
    url = f"{PROVIDERS}/{pid}/model-aliases"

    def listed(**params: object) -> list[tuple[str, str, bool]]:
        response = client.get(url, params=params, headers=admin)
        assert response.status_code == 200
        items = response.json()["data"]["items"]
        assert all(set(item) == SEGMENT_FIELDS for item in items)
        return [(i["alias"], i["model_code"], i["effective_to"] is None) for i in items]

    assert listed() == [
        ("alpha", "claude-x", False),
        ("alpha", "claude-y", True),
        ("beta", "claude-y", False),
        ("zeta", "claude-x", True),
    ]
    assert listed(alias="alpha") == [("alpha", "claude-x", False), ("alpha", "claude-y", True)]
    assert listed(current="true") == [("alpha", "claude-y", True), ("zeta", "claude-x", True)]
    assert listed(alias="beta", current="true") == []
    assert listed(alias="ALPHA") == []
    assert listed(page=2, page_size=3) == [("zeta", "claude-x", True)]


@pytest.mark.parametrize(
    ("path", "query"),
    [
        ("", "status=BOGUS"),
        ("", "status=active"),
        ("", "page=0"),
        ("", "page_size=101"),
        ("/{provider}/models", "status=RETIRED_NOW"),
        ("/{provider}/models", "page=10001"),
        ("/{provider}/model-aliases", "current=maybe"),
        ("/{provider}/model-aliases", "page_size=0"),
        ("/{provider}/model-aliases", "alias=" + "a" * 129),
    ],
)
def test_listing_bounds_are_422(client, admin, path: str, query: str) -> None:
    provider = new_provider(client, admin)
    url = PROVIDERS + path.replace("{provider}", provider["id"])

    response = client.get(f"{url}?{query}", headers=admin)

    assert (response.status_code, error_code(response)) == (422, "VALIDATION_ERROR")


# --- 响应字段与鉴权写法 ---------------------------------------------------------------


def test_no_response_carries_an_internal_id(client, app, admin) -> None:
    """设计 §7「响应字段」：对外的 id 都是 public_id，没有自增 id，也没有 `open_slot`。"""
    provider = new_provider(client, admin)
    model = new_model(client, admin, provider["id"])
    segment = map_alias(client, admin, provider["id"], "claude-x-latest", model["id"]).json()
    base = f"{PROVIDERS}/{provider['id']}"
    detail = client.get(f"{base}/models/{model['id']}", headers=admin).json()["data"]
    retired = retire(client, admin, provider["id"], segment["data"]["id"]).json()["data"]
    bodies = [
        (PROVIDER_FIELDS, provider),
        (PROVIDER_FIELDS, client.get(base, headers=admin).json()["data"]),
        (MODEL_FIELDS, model),
        (MODEL_DETAIL_FIELDS, detail),
        (SEGMENT_FIELDS, segment["data"]),
        (SEGMENT_FIELDS, retired),
    ]
    listings = [
        (PROVIDER_FIELDS, PROVIDERS),
        (MODEL_FIELDS, f"{base}/models"),
        (SEGMENT_FIELDS, f"{base}/model-aliases"),
    ]
    for fields, url in listings:
        items = client.get(url, headers=admin).json()["data"]["items"]
        bodies += [(fields, item) for item in items]

    for fields, body in bodies:
        assert set(body) == fields
        for key in ("id", "provider_id", "model_id"):
            if key in body:
                assert str(uuid.UUID(body[key])) == body[key], key
        assert "open_slot" not in json.dumps(body)


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
    # 十五个接口；少了说明 AST 扫描落空了。
    assert len(handlers) == 15

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

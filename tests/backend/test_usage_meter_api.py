"""Usage meter types and components (design gate #163 v4 §7, AIH-TASK-025).

三类用例：

- **接口**（SQLite，TestClient）：种子列得出来；新建 `QUANTITY` 类型 → 详情 → 改名 → 停用，
  每步审计；带 `payload_shape` / `quantity_field`、代码重复、格式不合法的 422 / 409 都不写库；
  PATCH 带不可改字段 422、无变化 200 不写审计；响应不含内部 id；
- **事务中途失败**（分量写入、审计写入、提交抛错）：`factory` 夹具的两个参数，在 SQLite 与
  真 MySQL 上各跑一次，类型行一并回滚（设计 §7「新建事务中途失败」「事务中途失败」）；
- **形态由数据库保证**（真 MySQL）：直接插入形态不符的分量、`QUANTITY` 类型的第二个分量、
  取数字段与形态不配的分量、删有分量的类型，都被外键 / CHECK / 唯一约束拒绝。

MySQL 那部分需要 `BILLING_TEST_DATABASE_URL` 指向一个**可以被清空的**库，没设时 skip ——
**不要把 skipped 读成 passed**，CI 设了它并把 skipped 判成失败。
"""

from __future__ import annotations

import base64
import json
import os
import uuid
from collections.abc import Iterator

import pytest
from alembic.config import Config
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, delete, func, insert, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from alembic import command
from app.core.config import Settings
from app.core.database import create_session_factory
from app.core.tokens import issue_access_token
from app.main import create_app
from app.models.ai_catalog import (
    SEED_METER_TYPES,
    AiModel,
    AiModelAlias,
    AiProvider,
    QuantityKind,
    UsageMeterComponent,
    UsageMeterType,
)
from app.models.auth import AuditAction, AuditLog, User, UserRole, UserStatus
from app.models.base import Base
from app.repositories import ai_catalog as catalog_repository
from app.services import ai_catalog
from app.services.auth import RequestContext, utc_now

TEST_DATABASE_URL = os.environ.get("BILLING_TEST_DATABASE_URL", "")
TEST_EMAIL_DOMAIN = "@usage-meter-test.example.com"

METER_TYPES = "/api/v1/admin/usage-meter-types"
CONTEXT = RequestContext(ip_address="203.0.113.9", user_agent="usage-meter-test")

# 响应字段白名单（设计 §2）。断言「恰好等于」：多一个键（内部 id）就红。
METER_TYPE_FIELDS = {
    "id",
    "code",
    "display_name",
    "payload_shape",
    "unit",
    "quantity_kind",
    "status",
    "components",
    "created_at",
    "updated_at",
}
COMPONENT_FIELDS = {"component_code", "quantity_field", "created_at"}
PAGE_FIELDS = {"items", "page", "page_size", "total"}
ENVELOPE_FIELDS = {"success", "data", "error", "request_id"}

# 全零占位值（secret-scan）。
ZERO_ID = "00000000-0000-4000-8000-000000000000"

VIDEO = {
    "code": "VIDEO_SECOND",
    "display_name": "  Video seconds  ",
    "unit": "SECOND",
    "quantity_kind": "DECIMAL",
    "component_code": "VIDEO_SECOND",
}

SEED_CODES = {seed.code for seed in SEED_METER_TYPES}
SEED_COMPONENT_CODES = {
    component.component_code for seed in SEED_METER_TYPES for component in seed.components
}


# --- 夹具与帮手 -----------------------------------------------------------------


def seed_meter_types(factory: sessionmaker[Session]) -> None:
    """What migration 0012 writes, for a database built by `create_all` (SQLite)."""
    now = utc_now().replace(microsecond=0)
    with factory() as session:
        for seed in SEED_METER_TYPES:
            row = UsageMeterType(
                public_id=str(uuid.uuid4()),
                code=seed.code,
                display_name=seed.display_name,
                payload_shape=seed.payload_shape,
                unit=seed.unit,
                quantity_kind=seed.quantity_kind,
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            session.flush()
            for component in seed.components:
                session.add(
                    UsageMeterComponent(
                        meter_type_id=row.id,
                        payload_shape=seed.payload_shape,
                        component_code=component.component_code,
                        quantity_field=component.quantity_field,
                        created_at=now,
                    )
                )
        session.commit()


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
    seed_meter_types(application.state.session_factory)
    yield application
    engine.dispose()


@pytest.fixture
def client(app) -> TestClient:
    return TestClient(app)


def new_user(factory: sessionmaker[Session], role: UserRole = UserRole.ADMIN) -> User:
    now = utc_now()
    with factory() as session:
        user = User(
            email=f"{uuid.uuid4().hex}{TEST_EMAIL_DOMAIN}",
            password_hash="not-a-real-hash",
            role=role,
            status=UserStatus.ACTIVE,
            created_at=now,
            updated_at=now,
        )
        session.add(user)
        session.commit()
        return user


@pytest.fixture
def admin_user(app) -> User:
    return new_user(app.state.session_factory)


@pytest.fixture
def admin(app, admin_user) -> dict[str, str]:
    token = issue_access_token(
        app.state.settings,
        user_id=admin_user.id,
        role="ADMIN",
        session_id=uuid.uuid4().hex,
        now=utc_now(),
    )
    return {"Authorization": f"Bearer {token}"}


def error_code(response) -> str:
    body = response.json()
    assert set(body) == ENVELOPE_FIELDS
    assert body["success"] is False
    return body["error"]["code"]


def snapshot(factory: sessionmaker[Session]) -> dict[str, object]:
    """Every meter type and component as committed, plus the audit count."""
    types = select(
        UsageMeterType.code,
        UsageMeterType.display_name,
        UsageMeterType.status,
        UsageMeterType.updated_at,
    ).order_by(UsageMeterType.id)
    components = select(
        UsageMeterComponent.meter_type_id,
        UsageMeterComponent.component_code,
        UsageMeterComponent.quantity_field,
    ).order_by(UsageMeterComponent.id)
    with factory() as session:
        return {
            "types": [tuple(row) for row in session.execute(types)],
            "components": [tuple(row) for row in session.execute(components)],
            "audits": session.execute(select(func.count()).select_from(AuditLog)).scalar_one(),
        }


def audits(factory: sessionmaker[Session], action: AuditAction) -> list[AuditLog]:
    statement = select(AuditLog).where(AuditLog.action == action).order_by(AuditLog.id)
    with factory() as session:
        return list(session.execute(statement).scalars())


def create(client: TestClient, headers: dict[str, str], **fields: object):
    return client.post(METER_TYPES, json={**VIDEO, **fields}, headers=headers)


def seeded_id(client: TestClient, headers: dict[str, str], code: str) -> str:
    listing = client.get(METER_TYPES, params={"page_size": 100}, headers=headers)
    [found] = [item for item in listing.json()["data"]["items"] if item["code"] == code]
    return found["id"]


# --- 种子 -----------------------------------------------------------------------


def test_the_seeded_types_are_listed_with_their_components(client, admin) -> None:
    response = client.get(METER_TYPES, params={"page_size": 100}, headers=admin)

    assert response.status_code == 200
    page = response.json()["data"]
    assert set(page) == PAGE_FIELDS
    assert page["total"] == 9
    items = page["items"]
    # `code` 升序。
    assert [item["code"] for item in items] == sorted(SEED_CODES)
    expected = {
        seed.code: (
            seed.payload_shape.value,
            seed.unit,
            seed.quantity_kind.value,
            sorted((c.component_code, c.quantity_field) for c in seed.components),
        )
        for seed in SEED_METER_TYPES
    }
    for item in items:
        assert set(item) == METER_TYPE_FIELDS
        assert all(set(component) == COMPONENT_FIELDS for component in item["components"])
        pairs = [(c["component_code"], c["quantity_field"]) for c in item["components"]]
        found = (item["payload_shape"], item["unit"], item["quantity_kind"], pairs)
        assert found == expected[item["code"]]
        assert item["status"] == "ACTIVE"
        assert str(uuid.UUID(item["id"])) == item["id"]


# --- 新建 → 详情 → 改名 → 停用 -----------------------------------------------------


def test_an_admin_creates_a_quantity_type_and_edits_it(client, app, admin, admin_user) -> None:
    """设计 §7「新建计量类型」：类型为 QUANTITY、恰好一个分量取 quantity；每步审计。"""
    factory = app.state.session_factory
    response = create(client, {**admin, "User-Agent": "admin-console-test"})

    assert response.status_code == 201, response.text
    created = response.json()["data"]
    assert set(created) == METER_TYPE_FIELDS
    assert (created["code"], created["display_name"]) == ("VIDEO_SECOND", "Video seconds")
    assert (created["payload_shape"], created["unit"], created["quantity_kind"]) == (
        "QUANTITY",
        "SECOND",
        "DECIMAL",
    )
    assert created["status"] == "ACTIVE"
    [component] = created["components"]
    assert (component["component_code"], component["quantity_field"]) == (
        "VIDEO_SECOND",
        "quantity",
    )
    [audit] = audits(factory, AuditAction.USAGE_METER_TYPE_CREATE)
    assert (audit.actor_user_id, audit.actor_role) == (admin_user.id, "ADMIN")
    assert (audit.entity_type, audit.entity_id) == ("usage_meter_type", created["id"])
    assert audit.user_agent == "admin-console-test"
    assert audit.before_state is None
    assert json.loads(audit.after_state or "{}") == {
        "code": "VIDEO_SECOND",
        "display_name": "Video seconds",
        "payload_shape": "QUANTITY",
        "unit": "SECOND",
        "quantity_kind": "DECIMAL",
        "status": "ACTIVE",
        "component_code": "VIDEO_SECOND",
    }

    url = f"{METER_TYPES}/{created['id']}"
    assert client.get(url, headers=admin).json()["data"] == created

    renamed = client.patch(url, json={"display_name": " Video "}, headers=admin)
    assert renamed.status_code == 200
    assert renamed.json()["data"]["display_name"] == "Video"
    retired = client.patch(url, json={"status": "RETIRED"}, headers=admin)
    assert retired.json()["data"]["status"] == "RETIRED"
    both = client.patch(url, json={"display_name": "Clips", "status": "ACTIVE"}, headers=admin)
    assert (both.json()["data"]["display_name"], both.json()["data"]["status"]) == (
        "Clips",
        "ACTIVE",
    )
    # 不可改的字段原样。
    assert both.json()["data"]["components"] == created["components"]
    assert both.json()["data"]["unit"] == "SECOND"

    updates = audits(factory, AuditAction.USAGE_METER_TYPE_UPDATE)
    # 只记变化的字段的前后值。
    assert [json.loads(a.before_state or "{}") for a in updates] == [
        {"display_name": "Video seconds"},
        {"status": "ACTIVE"},
        {"display_name": "Video", "status": "RETIRED"},
    ]
    assert [json.loads(a.after_state or "{}") for a in updates] == [
        {"display_name": "Video"},
        {"status": "RETIRED"},
        {"display_name": "Clips", "status": "ACTIVE"},
    ]
    for audit in updates:
        assert (audit.entity_type, audit.entity_id) == ("usage_meter_type", created["id"])


def test_a_seeded_type_can_be_renamed_and_retired(client, app, admin) -> None:
    """种子与新建的类型一样可改名、可停用；停用不影响摄取与计价（那是 T-E / T-G 的契约）。"""
    url = f"{METER_TYPES}/{seeded_id(client, admin, 'LLM_TOKEN')}"

    response = client.patch(url, json={"status": "RETIRED"}, headers=admin)

    assert response.status_code == 200
    data = response.json()["data"]
    assert (data["code"], data["status"], data["payload_shape"]) == (
        "LLM_TOKEN",
        "RETIRED",
        "LLM_TOKEN_FIELDS",
    )
    assert len(data["components"]) == 4
    listed = client.get(METER_TYPES, params={"status": "RETIRED"}, headers=admin).json()["data"]
    assert [item["code"] for item in listed["items"]] == ["LLM_TOKEN"]
    active = client.get(METER_TYPES, params={"status": "ACTIVE"}, headers=admin).json()["data"]
    assert active["total"] == 8


def test_an_edit_that_changes_nothing_writes_nothing(client, app, admin) -> None:
    """设计 §7「幂等」：PATCH 无变化 200，不写审计，`updated_at` 不变。"""
    created = create(client, admin).json()["data"]
    before = snapshot(app.state.session_factory)

    response = client.patch(
        f"{METER_TYPES}/{created['id']}",
        json={"display_name": "  Video seconds ", "status": "ACTIVE"},
        headers=admin,
    )

    assert response.status_code == 200
    assert response.json()["data"] == created
    assert snapshot(app.state.session_factory) == before


# --- 新建的边界 -------------------------------------------------------------------


@pytest.mark.parametrize(
    "extra",
    [
        {"payload_shape": "QUANTITY"},
        {"payload_shape": "LLM_TOKEN_FIELDS"},
        {"quantity_field": "quantity"},
        {"quantity_field": "input_tokens"},
        {"status": "RETIRED"},
        {"id": ZERO_ID},
        {"public_id": ZERO_ID},
        {"components": []},
    ],
    ids=[
        "shape-quantity",
        "shape-token",
        "field-quantity",
        "field-token",
        "status",
        "id",
        "public_id",
        "components",
    ],
)
def test_create_refuses_extra_fields(client, app, admin, extra: dict) -> None:
    """新建固定为 QUANTITY、取 quantity：带 `payload_shape` 或 `quantity_field` 就是 422。"""
    before = snapshot(app.state.session_factory)

    response = create(client, admin, **extra)

    assert (response.status_code, error_code(response)) == (422, "VALIDATION_ERROR")
    assert snapshot(app.state.session_factory) == before


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        ({"code": "video_second"}, 422),
        ({"code": "Video_Second"}, 422),
        ({"code": "V"}, 422),
        ({"code": "VS"}, 201),
        ({"code": "V" * 32}, 201),
        ({"code": "V" * 33}, 422),
        ({"code": "1VIDEO"}, 422),
        ({"code": "_VIDEO"}, 422),
        ({"code": "VIDEO SECOND"}, 422),
        ({"code": "VIDEO-SECOND"}, 422),
        ({"code": "VIDEO_SECOND\n"}, 422),
        ({"component_code": "video"}, 422),
        ({"component_code": "C" * 33}, 422),
        ({"component_code": "C" * 32}, 201),
        ({"unit": "second"}, 422),
        ({"unit": "S"}, 201),
        ({"unit": "S" * 16}, 201),
        ({"unit": "S" * 17}, 422),
        ({"unit": "SEC ONDS"}, 422),
        ({"quantity_kind": "FLOAT"}, 422),
        ({"quantity_kind": "decimal"}, 422),
        ({"quantity_kind": "INTEGER"}, 201),
        ({"display_name": ""}, 422),
        ({"display_name": "   "}, 422),
        ({"display_name": "x" * 256}, 422),
        ({"display_name": "x" * 255}, 201),
        ({"code": None}, 422),
    ],
)
def test_create_field_rules(client, app, admin, fields: dict, expected: int) -> None:
    """设计 §2「字段规则」：代码 `^[A-Z][A-Z0-9_]{1,31}$`、单位 `^[A-Z][A-Z0-9_]{0,15}$`。"""
    before = snapshot(app.state.session_factory)

    response = create(client, admin, **fields)

    assert response.status_code == expected, response.text
    if expected == 422:
        assert error_code(response) == "VALIDATION_ERROR"
        assert snapshot(app.state.session_factory) == before


@pytest.mark.parametrize(
    ("fields", "code"),
    [
        ({"code": "EMBEDDING_TOKEN"}, "USAGE_METER_TYPE_CODE_TAKEN"),
        ({"code": "LLM_TOKEN", "component_code": "LLM_TOKEN_NEW"}, "USAGE_METER_TYPE_CODE_TAKEN"),
        ({"component_code": "LLM_INPUT_TOKEN"}, "USAGE_METER_COMPONENT_CODE_TAKEN"),
        ({"component_code": "CUSTOM"}, "USAGE_METER_COMPONENT_CODE_TAKEN"),
    ],
    ids=["type-seed", "type-seed-new-component", "component-seed", "component-seed-single"],
)
def test_a_taken_code_is_409_and_writes_neither_row(
    client, app, admin, fields: dict, code: str
) -> None:
    """设计 §7「新建计量类型的边界」：409 时类型行与分量行都没写。"""
    before = snapshot(app.state.session_factory)

    response = create(client, admin, **fields)

    assert (response.status_code, error_code(response)) == (409, code)
    assert snapshot(app.state.session_factory) == before


def test_codes_taken_by_an_admin_created_type_are_409_too(client, app, admin) -> None:
    create(client, admin)
    before = snapshot(app.state.session_factory)

    same_code = create(client, admin, component_code="OTHER_COMPONENT")
    same_component = create(client, admin, code="OTHER_TYPE")

    assert error_code(same_code) == "USAGE_METER_TYPE_CODE_TAKEN"
    assert error_code(same_component) == "USAGE_METER_COMPONENT_CODE_TAKEN"
    assert snapshot(app.state.session_factory) == before


# --- PATCH 的边界 -----------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        {"code": "RENAMED"},
        {"unit": "MINUTE"},
        {"quantity_kind": "INTEGER"},
        {"payload_shape": "LLM_TOKEN_FIELDS"},
        {"component_code": "RENAMED"},
        {"quantity_field": "input_tokens"},
        {"alias": "x"},
        {"provider_id": ZERO_ID},
        {"display_name": "Fine", "unit": "MINUTE"},
        {"status": "RETIRED", "code": "RENAMED"},
        {},
        {"display_name": None},
        {"status": None},
        {"status": "DISABLED"},
        {"status": "retired"},
        {"display_name": "   "},
        {"display_name": "x" * 256},
    ],
    ids=[
        "code",
        "unit",
        "quantity_kind",
        "payload_shape",
        "component_code",
        "quantity_field",
        "alias",
        "provider_id",
        "valid+unit",
        "valid+code",
        "empty",
        "name-null",
        "status-null",
        "status-unknown",
        "status-lowercase",
        "name-blank",
        "name-256",
    ],
)
def test_patch_refuses_immutable_and_bad_fields(client, app, admin, body: dict) -> None:
    """设计 §7「不可改代码」：代码、单位、数量类型、形态、分量代码都不能经 PATCH 改。"""
    created = create(client, admin).json()["data"]
    before = snapshot(app.state.session_factory)

    response = client.patch(f"{METER_TYPES}/{created['id']}", json=body, headers=admin)

    assert (response.status_code, error_code(response)) == (422, "VALIDATION_ERROR")
    assert snapshot(app.state.session_factory) == before


@pytest.mark.parametrize("unknown", [ZERO_ID, "1", "VIDEO_SECOND"])
def test_an_unknown_meter_type_is_404_and_writes_nothing(client, app, admin, unknown) -> None:
    """不存在的 public_id、内部自增 id、代码：同一个 404。"""
    create(client, admin)
    before = snapshot(app.state.session_factory)

    responses = [
        client.get(f"{METER_TYPES}/{unknown}", headers=admin),
        client.patch(f"{METER_TYPES}/{unknown}", json={"status": "RETIRED"}, headers=admin),
    ]

    expected = [(404, "USAGE_METER_TYPE_NOT_FOUND")] * 2
    assert [(r.status_code, error_code(r)) for r in responses] == expected
    assert snapshot(app.state.session_factory) == before


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("status=BOGUS", 422),
        ("status=retired", 422),
        ("page_size=0", 422),
        ("page_size=101", 422),
        ("page=0", 422),
        ("page=2&page_size=5", 200),
    ],
)
def test_listing_bounds(client, admin, query: str, expected: int) -> None:
    response = client.get(f"{METER_TYPES}?{query}", headers=admin)

    assert response.status_code == expected
    if expected == 200:
        page = response.json()["data"]
        assert (page["page"], page["page_size"], page["total"]) == (2, 5, 9)
        assert [item["code"] for item in page["items"]] == sorted(SEED_CODES)[5:]


def test_no_response_carries_an_internal_id(client, admin) -> None:
    """设计 §7「响应字段」：只有 public_id；没有 `meter_type_id` 或自增 id。"""
    created = create(client, admin).json()["data"]
    url = f"{METER_TYPES}/{created['id']}"
    bodies = [
        created,
        client.get(url, headers=admin).json()["data"],
        client.patch(url, json={"display_name": "Clips"}, headers=admin).json()["data"],
        *client.get(METER_TYPES, headers=admin).json()["data"]["items"],
    ]

    for body in bodies:
        assert set(body) == METER_TYPE_FIELDS
        assert all(set(component) == COMPONENT_FIELDS for component in body["components"])
        assert "meter_type_id" not in json.dumps(body)


# --- 事务中途失败：SQLite 与 MySQL 各一次（INV-13） -----------------------------------


@pytest.fixture(params=["sqlite", "mysql"])
def factory(request, monkeypatch) -> Iterator[sessionmaker[Session]]:
    if request.param == "mysql":
        yield from _mysql_factory(monkeypatch)
        return
    yield from _sqlite_factory()


@pytest.fixture
def mysql_factory(monkeypatch) -> Iterator[sessionmaker[Session]]:
    yield from _mysql_factory(monkeypatch)


def _sqlite_factory() -> Iterator[sessionmaker[Session]]:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = create_session_factory(engine)
    seed_meter_types(session_factory)
    yield session_factory
    engine.dispose()


def _mysql_factory(monkeypatch) -> Iterator[sessionmaker[Session]]:
    if not TEST_DATABASE_URL:
        pytest.skip("BILLING_TEST_DATABASE_URL is not set; the MySQL half needs a real MySQL")
    # 建表与种子走 alembic：外键、CHECK、排序规则与种子都以迁移 0012 建出来的为准。
    from app.core.config import get_settings

    monkeypatch.setenv("BILLING_DATABASE_URL", TEST_DATABASE_URL)
    get_settings.cache_clear()
    command.upgrade(Config("alembic.ini"), "head")
    get_settings.cache_clear()

    engine = create_engine(TEST_DATABASE_URL, pool_size=4, max_overflow=2)
    _clean(engine)
    try:
        yield create_session_factory(engine)
    finally:
        _clean(engine)
        engine.dispose()


def _clean(engine: Engine) -> None:
    """⚠️ 库是共享的：只删本文件建的行，种子留着。

    目录行没有删除接口（设计 §1）；这里是测试清场，直接删表行。审计表只能 TRUNCATE：0010 的
    触发器拒绝 DELETE，TRUNCATE 是 DDL、不经触发器。
    """
    ours = select(UsageMeterType.id).where(UsageMeterType.code.not_in(sorted(SEED_CODES)))
    with engine.begin() as connection:
        connection.exec_driver_sql("TRUNCATE TABLE audit_logs")
    with engine.begin() as connection:
        connection.execute(delete(AiModelAlias))
        connection.execute(delete(AiModel))
        connection.execute(delete(AiProvider))
        connection.execute(
            delete(UsageMeterComponent).where(UsageMeterComponent.meter_type_id.in_(ours))
        )
        connection.execute(
            delete(UsageMeterType).where(UsageMeterType.code.not_in(sorted(SEED_CODES)))
        )
        # 种子行被改名、停用过的，改回原样。
        for seed in SEED_METER_TYPES:
            connection.execute(
                UsageMeterType.__table__.update()
                .where(UsageMeterType.code == seed.code)
                .values(display_name=seed.display_name, status="ACTIVE")
            )
        connection.execute(delete(User).where(User.email.like(f"%{TEST_EMAIL_DOMAIN}")))


def create_video(factory: sessionmaker[Session], actor: User):
    return ai_catalog.create_meter_type(
        factory,
        actor=actor,
        code="VIDEO_SECOND",
        display_name="Video seconds",
        unit="SECOND",
        quantity_kind=QuantityKind.DECIMAL,
        component_code="VIDEO_SECOND",
        context=CONTEXT,
    )


def break_the_audit(monkeypatch) -> None:
    """The audit row fails at flush: `created_at` is NULL (NOT NULL → IntegrityError)."""
    real = ai_catalog.record_audit

    def broken(session: Session, **options: object) -> None:
        real(session, **{**options, "now": None})  # type: ignore[arg-type]

    monkeypatch.setattr(ai_catalog, "record_audit", broken)


def break_the_component(monkeypatch) -> None:
    def failing(*_args: object, **_options: object) -> None:
        raise RuntimeError("injected component failure")

    monkeypatch.setattr(catalog_repository, "insert_quantity_component", failing)


def break_the_commit(monkeypatch) -> None:
    def failing_commit(self: Session) -> None:
        raise RuntimeError("injected commit failure")

    monkeypatch.setattr(Session, "commit", failing_commit)


@pytest.mark.parametrize("failure", ["component", "audit", "commit"])
def test_a_failed_create_leaves_neither_row(factory, monkeypatch, failure: str) -> None:
    """设计 §7「新建事务中途失败」：分量或审计写入抛错、提交抛错，类型行也回滚。"""
    actor = new_user(factory)
    before = snapshot(factory)
    breakers = {
        "component": break_the_component,
        "audit": break_the_audit,
        "commit": break_the_commit,
    }
    breakers[failure](monkeypatch)

    with pytest.raises(Exception):  # noqa: B017 - 三种注入各抛各的，这里只关心回滚
        create_video(factory, actor)

    # 下面的核对只读、不提交，所以不必先撤掉提交的替身。
    assert snapshot(factory) == before


@pytest.mark.parametrize("failure", ["audit", "commit"])
def test_a_failed_update_leaves_the_row_as_it_was(factory, monkeypatch, failure: str) -> None:
    """设计 §7「事务中途失败」：审计写入或提交抛错，改名与停用都不留下。"""
    actor = new_user(factory)
    created = create_video(factory, actor)
    before = snapshot(factory)
    breakers = {"audit": break_the_audit, "commit": break_the_commit}
    breakers[failure](monkeypatch)

    with pytest.raises(Exception):  # noqa: B017
        ai_catalog.update_meter_type(
            factory,
            actor=actor,
            meter_type_id=created.id,
            changes={"display_name": "Clips", "status": "RETIRED"},
            context=CONTEXT,
        )

    assert snapshot(factory) == before


# --- 形态由数据库保证（真 MySQL） ----------------------------------------------------

# MySQL 的错误号。
_ER_DUP_ENTRY = 1062
_ER_ROW_IS_REFERENCED_2 = 1451
_ER_NO_REFERENCED_ROW_2 = 1452
_ER_CHECK_CONSTRAINT_VIOLATED = 3819


def _type_id(session: Session, code: str) -> int:
    return int(
        session.execute(select(UsageMeterType.id).where(UsageMeterType.code == code)).scalar_one()
    )


@pytest.mark.parametrize(
    ("owner", "shape", "field", "errno"),
    [
        # QUANTITY 类型下取 token 字段的分量。
        ("EMBEDDING_TOKEN", "QUANTITY", "input_tokens", _ER_CHECK_CONSTRAINT_VIOLATED),
        # QUANTITY 类型的第二个分量（同一字段）。
        ("EMBEDDING_TOKEN", "QUANTITY", "quantity", _ER_DUP_ENTRY),
        # LLM_TOKEN_FIELDS 类型下取 quantity 的分量。
        ("LLM_TOKEN", "LLM_TOKEN_FIELDS", "quantity", _ER_CHECK_CONSTRAINT_VIOLATED),
        # LLM_TOKEN_FIELDS 类型下同一个 token 字段的第二个分量。
        ("LLM_TOKEN", "LLM_TOKEN_FIELDS", "input_tokens", _ER_DUP_ENTRY),
        # 形态与所属类型不符（取数字段本身与声称的形态相配，所以只有外键拦得住）。
        ("LLM_TOKEN", "QUANTITY", "quantity", _ER_NO_REFERENCED_ROW_2),
        ("EMBEDDING_TOKEN", "LLM_TOKEN_FIELDS", "output_tokens", _ER_NO_REFERENCED_ROW_2),
        # 载荷里不存在的字段。
        ("CUSTOM", "QUANTITY", "amount", _ER_CHECK_CONSTRAINT_VIOLATED),
    ],
    ids=[
        "quantity-type-token-field",
        "quantity-type-second-component",
        "token-type-quantity-field",
        "token-type-same-field-twice",
        "shape-mismatch-quantity",
        "shape-mismatch-token",
        "unknown-field",
    ],
)
def test_the_database_keeps_components_in_shape(
    mysql_factory, owner: str, shape: str, field: str, errno: int
) -> None:
    """设计 §3「本任务自己的不变量」①：绕过服务层直接插，被 CHECK / 外键 / 唯一约束拒绝。"""
    before = snapshot(mysql_factory)
    with mysql_factory() as session:
        values = {
            "meter_type_id": _type_id(session, owner),
            "payload_shape": shape,
            "component_code": "DIRECT_INSERT",
            "quantity_field": field,
            "created_at": utc_now().replace(microsecond=0),
        }
        with pytest.raises(DBAPIError) as raised:
            session.execute(insert(UsageMeterComponent).values(**values))
        session.rollback()

    assert int(raised.value.orig.args[0]) == errno
    assert snapshot(mysql_factory) == before


def test_a_quantity_type_takes_at_most_one_component(mysql_factory) -> None:
    """管理员新建的类型也一样：它唯一的分量之外，再插一个就撞唯一约束。"""
    actor = new_user(mysql_factory)
    create_video(mysql_factory, actor)
    before = snapshot(mysql_factory)

    with mysql_factory() as session:
        values = {
            "meter_type_id": _type_id(session, "VIDEO_SECOND"),
            "payload_shape": "QUANTITY",
            "component_code": "VIDEO_SECOND_AGAIN",
            "quantity_field": "quantity",
            "created_at": utc_now().replace(microsecond=0),
        }
        with pytest.raises(DBAPIError) as raised:
            session.execute(insert(UsageMeterComponent).values(**values))
        session.rollback()

    assert int(raised.value.orig.args[0]) == _ER_DUP_ENTRY
    assert snapshot(mysql_factory) == before


def test_a_meter_type_with_components_cannot_be_deleted(mysql_factory) -> None:
    """设计 §3 INV-6：外键 RESTRICT，类型删不掉。"""
    before = snapshot(mysql_factory)
    with mysql_factory() as session:
        with pytest.raises(DBAPIError) as raised:
            session.execute(delete(UsageMeterType).where(UsageMeterType.code == "LLM_TOKEN"))
        session.rollback()

    assert int(raised.value.orig.args[0]) == _ER_ROW_IS_REFERENCED_2
    assert snapshot(mysql_factory) == before


def test_a_new_type_via_the_service_is_what_mysql_stores(mysql_factory) -> None:
    """MySQL 上走一遍服务：列宽、排序规则、CHECK 与外键都放行一个合法的新类型。"""
    actor = new_user(mysql_factory)

    created = create_video(mysql_factory, actor)

    with mysql_factory() as session:
        row = session.execute(
            select(UsageMeterType).where(UsageMeterType.public_id == created.id)
        ).scalar_one()
        [component] = session.execute(
            select(UsageMeterComponent).where(UsageMeterComponent.meter_type_id == row.id)
        ).scalars()
    assert (row.code, row.payload_shape.value, row.unit, row.quantity_kind.value) == (
        "VIDEO_SECOND",
        "QUANTITY",
        "SECOND",
        "DECIMAL",
    )
    assert (component.component_code, component.quantity_field) == ("VIDEO_SECOND", "quantity")
    assert len(audits(mysql_factory, AuditAction.USAGE_METER_TYPE_CREATE)) == 1

"""Integration API credential endpoints on SQLite (design gate #118 v1 §2, §7).

这里测接口契约：字段白名单、secret 只出现一次、`Cache-Control: no-store`、校验边界、
跨客户与跨项目的 404、轮换冲突与吊销的幂等、主密钥未配置、日志里没有 secret。
匿名 401 / CUSTOMER 403 由 test_admin_customers_api.py 的路由枚举用例覆盖；锁、并发、
复合外键与回滚在 test_integration_access_service.py 与 test_migrations.py。

⚠️ secret 在这里都是运行时随机生成的；文件里只出现全零占位值（设计 §2「格式」）。
"""

from __future__ import annotations

import ast
import base64
import inspect
import io
import json
import logging
import os
import re
import textwrap
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.api import admin_customers
from app.core.config import Settings
from app.core.database import create_session_factory
from app.core.logging import JsonFormatter
from app.core.tokens import issue_access_token
from app.main import create_app
from app.models.auth import AuditAction, AuditLog, User, UserRole, UserStatus
from app.models.base import Base
from app.models.integration import CredentialStatus, IntegrationCredential
from app.models.tenancy import Project
from app.repositories.integration_access import insert_credential
from app.services import integration_access
from app.services.auth import utc_now

CUSTOMERS = "/api/v1/admin/customers"

# 响应字段白名单（设计 §2）。断言「恰好等于」：内部 id、tenant_id、project_id、密文、
# 主密钥版本谁多出来都红。
CREDENTIAL_FIELDS = {
    "api_key",
    "key_version",
    "status",
    "valid_from",
    "valid_until",
    "last_used_at",
    "created_at",
    "revoked_at",
    "verifiable",
}
ISSUED_FIELDS = CREDENTIAL_FIELDS | {"secret"}
PAGE_FIELDS = {"items", "page", "page_size", "total"}
ENVELOPE_FIELDS = {"success", "data", "error", "request_id"}

API_KEY_PATTERN = re.compile(r"ak_[0-9a-f]{32}")
SECRET_PATTERN = re.compile(r"sk_[0-9a-f]{64}")

# 全零占位值：只用来探「请求体不许带这些字段」，从不参与签名。
ZERO_API_KEY = "ak_" + "0" * 32
ZERO_SECRET = "sk_" + "0" * 64
REASON = "Pilot finished"

HANDLERS = [
    admin_customers.create_credential,
    admin_customers.list_credentials,
    admin_customers.rotate_credential,
    admin_customers.revoke_credential_version,
    admin_customers.revoke_credential,
]


# --- 夹具与帮手 -----------------------------------------------------------------


def settings_for(tmp_path, database_url: str = "", *, master_key: bool = True) -> Settings:
    key = tmp_path / "jwt.key"
    key.write_text("a-test-signing-key-that-is-long-enough", encoding="utf-8")
    master = ""
    if master_key:
        path = tmp_path / "master.key"
        path.write_text(f"1:{base64.b64encode(os.urandom(32)).decode()}\n", encoding="utf-8")
        master = str(path)
    return Settings(jwt_secret_file=str(key), master_key_file=master, database_url=database_url)


def in_memory_app(settings: Settings) -> FastAPI:
    application = create_app(settings)
    # StaticPool：理由见 test_admin_customers_api.py 的同名夹具。
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    application.state.engine = engine
    application.state.session_factory = create_session_factory(engine)
    return application


@pytest.fixture
def app(tmp_path):
    application = in_memory_app(settings_for(tmp_path))
    yield application
    application.state.engine.dispose()


@pytest.fixture
def client(app) -> TestClient:
    return TestClient(app)


def make_user(application: FastAPI) -> int:
    now = utc_now()
    with application.state.session_factory() as session:
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


def token_for(application: FastAPI, user_id: int) -> dict[str, str]:
    token = issue_access_token(
        application.state.settings,
        user_id=user_id,
        role="ADMIN",
        session_id=uuid.uuid4().hex,
        now=utc_now(),
    )
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def admin_id(app) -> int:
    return make_user(app)


@pytest.fixture
def admin(app, admin_id) -> dict[str, str]:
    return token_for(app, admin_id)


def error_code(response) -> str:
    body = response.json()
    assert set(body) == ENVELOPE_FIELDS
    assert body["success"] is False
    assert body["data"] is None
    return body["error"]["code"]


def new_customer(client: TestClient, headers: dict[str, str]) -> str:
    body = {"company_name": "Acme Sdn Bhd", "email": "ops@example.com"}
    response = client.post(CUSTOMERS, json=body, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()["data"]["id"]


def new_project(client: TestClient, headers: dict[str, str], customer_id: str) -> str:
    url = f"{CUSTOMERS}/{customer_id}/projects"
    response = client.post(url, json={"name": "Chatbot"}, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()["data"]["id"]


class Place:
    """One customer's project, and the URLs of its credentials."""

    def __init__(self, client: TestClient, headers: dict[str, str]) -> None:
        self.client = client
        self.headers = headers
        self.customer_id = new_customer(client, headers)
        self.project_id = new_project(client, headers, self.customer_id)

    @property
    def base(self) -> str:
        return f"{CUSTOMERS}/{self.customer_id}/projects/{self.project_id}/credentials"

    def create(self):
        return self.client.post(self.base, json={}, headers=self.headers)

    def created(self) -> dict:
        response = self.create()
        assert response.status_code == 201, response.text
        return response.json()["data"]

    def listing(self, **params: int):
        return self.client.get(self.base, params=params, headers=self.headers)

    def rotate(self, api_key: str, current: object):
        body = {"current_key_version": current}
        return self.client.post(f"{self.base}/{api_key}/rotate", json=body, headers=self.headers)

    def revoke_version(self, api_key: str, version: object, reason: str = REASON):
        url = f"{self.base}/{api_key}/versions/{version}/revoke"
        return self.client.post(url, json={"reason": reason}, headers=self.headers)

    def revoke_key(self, api_key: str, reason: str = REASON):
        url = f"{self.base}/{api_key}/revoke"
        return self.client.post(url, json={"reason": reason}, headers=self.headers)


@pytest.fixture
def place(client, admin) -> Place:
    return Place(client, admin)


def count_rows(session: Session, model) -> int:
    return session.execute(select(func.count()).select_from(model)).scalar_one()


def row_counts(application: FastAPI) -> dict[str, int]:
    models = (IntegrationCredential, AuditLog, Project)
    with application.state.session_factory() as session:
        return {model.__tablename__: count_rows(session, model) for model in models}


def stored(application: FastAPI) -> list[tuple]:
    statement = select(
        IntegrationCredential.public_api_key,
        IntegrationCredential.key_version,
        IntegrationCredential.status,
        IntegrationCredential.valid_until,
        IntegrationCredential.revoked_at,
        IntegrationCredential.encrypted_secret,
    ).order_by(IntegrationCredential.id)
    with application.state.session_factory() as session:
        return [tuple(row) for row in session.execute(statement)]


def audits(application: FastAPI, action: AuditAction) -> list[AuditLog]:
    statement = select(AuditLog).where(AuditLog.action == action).order_by(AuditLog.id)
    with application.state.session_factory() as session:
        return list(session.execute(statement).scalars())


def insert_probe_credential(application: FastAPI, project_id: str) -> None:
    """An ACTIVE version 1 of `ZERO_API_KEY`, written directly (no master key needed)."""
    with application.state.session_factory() as session:
        project = session.execute(
            select(Project).where(Project.public_id == project_id)
        ).scalar_one()
        insert_credential(
            session,
            tenant_id=project.tenant_id,
            project_id=project.id,
            api_key=ZERO_API_KEY,
            key_version=1,
            encrypted_secret="not-a-real-ciphertext",
            encryption_key_version=1,
            now=utc_now().replace(microsecond=0),
        )
        session.commit()


# --- 建凭据 ---------------------------------------------------------------------


def test_an_admin_creates_a_credential(app, place, admin_id) -> None:
    before = row_counts(app)

    response = place.create()

    assert response.status_code == 201, response.text
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert set(body) == ENVELOPE_FIELDS
    assert (body["success"], body["error"]) == (True, None)
    data = body["data"]
    assert set(data) == ISSUED_FIELDS
    assert API_KEY_PATTERN.fullmatch(data["api_key"])
    assert SECRET_PATTERN.fullmatch(data["secret"])
    assert (data["key_version"], data["status"], data["valid_until"]) == (1, "ACTIVE", None)
    assert (data["last_used_at"], data["revoked_at"], data["verifiable"]) == (None, None, True)
    assert data["valid_from"] == data["created_at"]

    assert row_counts(app) == {
        **before,
        "integration_credentials": before["integration_credentials"] + 1,
        "audit_logs": before["audit_logs"] + 1,
    }
    [row] = stored(app)
    assert row[0] == data["api_key"]
    assert data["secret"] not in row[5]
    [audit] = audits(app, AuditAction.API_KEY_CREATE)
    assert (audit.actor_user_id, audit.actor_role) == (admin_id, "ADMIN")
    assert (audit.entity_type, audit.entity_id) == ("integration_credential", data["api_key"])


def test_the_secret_is_returned_exactly_once(app, place) -> None:
    """设计 §7：建完再列表、轮换、吊销，任何其他响应体里都搜不到那个 secret。"""
    created = place.created()
    secret = created["secret"]

    later = [
        place.listing(),
        place.rotate(created["api_key"], 1),
        place.listing(),
        place.revoke_version(created["api_key"], 1),
        place.revoke_key(created["api_key"]),
        place.listing(),
    ]

    assert [response.status_code for response in later] == [200, 201, 200, 200, 200, 200]
    for response in later:
        assert secret not in response.text
    rotated_secret = later[1].json()["data"]["secret"]
    assert rotated_secret != secret
    for response in later[2:]:
        assert rotated_secret not in response.text
    # 列表项没有 secret，也没有密文。
    for item in later[0].json()["data"]["items"] + later[5].json()["data"]["items"]:
        assert set(item) == CREDENTIAL_FIELDS
    for ciphertext in (row[5] for row in stored(app)):
        for response in later:
            assert ciphertext not in response.text


def test_every_credential_response_is_no_store(place) -> None:
    created = place.created()
    responses = [
        place.listing(),
        place.rotate(created["api_key"], 1),
        place.revoke_version(created["api_key"], 1),
        place.revoke_key(created["api_key"]),
    ]

    for response in responses:
        assert response.status_code in (200, 201), response.text
        assert response.headers["cache-control"] == "no-store"


# --- 列表 -----------------------------------------------------------------------


def test_versions_are_listed_by_key_then_version(place) -> None:
    first = place.created()["api_key"]
    second = place.created()["api_key"]
    assert place.rotate(first, 1).status_code == 201

    page_one = place.listing(page=1, page_size=2).json()["data"]
    page_two = place.listing(page=2, page_size=2).json()["data"]

    assert set(page_one) == PAGE_FIELDS
    assert [(item["api_key"], item["key_version"]) for item in page_one["items"]] == [
        (first, 1),
        (first, 2),
    ]
    assert [(item["api_key"], item["key_version"]) for item in page_two["items"]] == [(second, 1)]
    assert page_one["total"] == page_two["total"] == 3
    # 重叠期（默认 7 天）内旧版本仍可用。
    assert all(item["verifiable"] for item in page_one["items"] + page_two["items"])
    old = page_one["items"][0]
    assert old["valid_until"] is not None

    assert place.revoke_key(second).status_code == 200
    [revoked] = place.listing(page=2, page_size=2).json()["data"]["items"]
    assert (revoked["status"], revoked["verifiable"]) == ("REVOKED", False)


@pytest.mark.parametrize(
    ("query", "expected"),
    [("page_size=0", 422), ("page_size=101", 422), ("page=0", 422), ("page=10001", 422)],
)
def test_paging_bounds(place, query: str, expected: int) -> None:
    response = place.client.get(f"{place.base}?{query}", headers=place.headers)

    assert (response.status_code, error_code(response)) == (expected, "VALIDATION_ERROR")


# --- 轮换 -----------------------------------------------------------------------


def test_rotation_keeps_the_key_and_adds_a_version(app, place) -> None:
    created = place.created()

    response = place.rotate(created["api_key"], 1)

    assert response.status_code == 201, response.text
    data = response.json()["data"]
    assert set(data) == ISSUED_FIELDS
    assert (data["api_key"], data["key_version"], data["valid_until"]) == (
        created["api_key"],
        2,
        None,
    )
    assert SECRET_PATTERN.fullmatch(data["secret"])
    assert len(audits(app, AuditAction.API_KEY_ROTATE)) == 1


def test_rotating_twice_from_the_same_version_is_a_conflict(app, place) -> None:
    created = place.created()
    assert place.rotate(created["api_key"], 1).status_code == 201
    before = stored(app)

    response = place.rotate(created["api_key"], 1)

    assert (response.status_code, error_code(response)) == (409, "CREDENTIAL_VERSION_CONFLICT")
    assert stored(app) == before
    assert [row[1] for row in before] == [1, 2]


def test_rotating_a_revoked_key_is_refused(app, place) -> None:
    created = place.created()
    assert place.revoke_key(created["api_key"]).status_code == 200
    before, counts = stored(app), row_counts(app)

    response = place.rotate(created["api_key"], 1)

    assert (response.status_code, error_code(response)) == (409, "CREDENTIAL_REVOKED")
    assert (stored(app), row_counts(app)) == (before, counts)


# --- 吊销 -----------------------------------------------------------------------


def test_revoking_a_version_twice(app, place) -> None:
    created = place.created()

    first = place.revoke_version(created["api_key"], 1)
    after_first = stored(app)
    second = place.revoke_version(created["api_key"], 1)

    assert first.status_code == second.status_code == 200
    data = first.json()["data"]
    assert set(data) == CREDENTIAL_FIELDS
    assert (data["status"], data["verifiable"]) == ("REVOKED", False)
    assert data["revoked_at"] is not None
    assert second.json()["data"] == data
    assert stored(app) == after_first
    [audit] = audits(app, AuditAction.API_KEY_REVOKE)
    assert audit.reason == REASON


def test_revoking_a_key_revokes_all_its_versions(app, place) -> None:
    created = place.created()
    assert place.rotate(created["api_key"], 1).status_code == 201

    response = place.revoke_key(created["api_key"])

    assert response.status_code == 200
    items = response.json()["data"]
    assert [(item["key_version"], item["status"]) for item in items] == [
        (1, "REVOKED"),
        (2, "REVOKED"),
    ]
    assert all(set(item) == CREDENTIAL_FIELDS for item in items)
    assert {row[2] for row in stored(app)} == {CredentialStatus.REVOKED}
    [audit] = audits(app, AuditAction.API_KEY_REVOKE)
    listed = json.loads(audit.after_state or "{}")["versions"]
    assert [entry["key_version"] for entry in listed] == [1, 2]

    again = place.revoke_key(created["api_key"])
    assert (again.status_code, again.json()["data"]) == (200, items)
    assert len(audits(app, AuditAction.API_KEY_REVOKE)) == 1


# --- 校验边界 -------------------------------------------------------------------


@pytest.mark.parametrize(
    "extra",
    [
        {"api_key": ZERO_API_KEY},
        {"secret": ZERO_SECRET},
        {"key_version": 1},
        {"valid_until": "2026-10-01T00:00:00"},
        {"tenant_id": 1},
        {"project_id": "00000000-0000-4000-8000-000000000000"},
    ],
    ids=["api_key", "secret", "key_version", "valid_until", "tenant_id", "project_id"],
)
@pytest.mark.parametrize("endpoint", ["create", "rotate", "revoke-version", "revoke-key"])
def test_bodies_refuse_extra_fields(app, place, endpoint: str, extra: dict) -> None:
    """extra="forbid"：请求体不能指定 key、secret、版本、截止时间或任何 id（设计 §2）。"""
    insert_probe_credential(app, place.project_id)
    url, body = {
        "create": (place.base, {}),
        "rotate": (f"{place.base}/{ZERO_API_KEY}/rotate", {"current_key_version": 1}),
        "revoke-version": (f"{place.base}/{ZERO_API_KEY}/versions/1/revoke", {"reason": REASON}),
        "revoke-key": (f"{place.base}/{ZERO_API_KEY}/revoke", {"reason": REASON}),
    }[endpoint]
    before, counts = stored(app), row_counts(app)

    response = place.client.post(url, json={**body, **extra}, headers=place.headers)

    assert (response.status_code, error_code(response)) == (422, "VALIDATION_ERROR")
    assert (stored(app), row_counts(app)) == (before, counts)
    # 422 只列字段名，不回显值。
    assert ZERO_SECRET not in response.text


@pytest.mark.parametrize(
    "current",
    ["1", 0, -1, 1.5, True, None, 2**31],
    ids=["string", "zero", "negative", "float", "bool", "null", "beyond-int"],
)
def test_current_key_version_must_be_a_positive_integer(app, place, current: object) -> None:
    created = place.created()
    before = stored(app)

    response = place.rotate(created["api_key"], current)

    assert (response.status_code, error_code(response)) == (422, "VALIDATION_ERROR")
    assert stored(app) == before


def test_rotation_requires_current_key_version(app, place) -> None:
    created = place.created()
    url = f"{place.base}/{created['api_key']}/rotate"

    response = place.client.post(url, json={}, headers=place.headers)

    assert (response.status_code, error_code(response)) == (422, "VALIDATION_ERROR")


@pytest.mark.parametrize(
    ("reason", "expected"),
    [("", 422), ("   ", 422), ("x" * 256, 422), ("x" * 255, 200), ("  " + "x" * 255 + "  ", 200)],
    ids=["empty", "blank", "256", "255", "255-padded"],
)
def test_reason_bounds(app, place, reason: str, expected: int) -> None:
    created = place.created()

    response = place.revoke_version(created["api_key"], 1, reason=reason)

    assert response.status_code == expected
    if expected == 422:
        assert error_code(response) == "VALIDATION_ERROR"
        assert audits(app, AuditAction.API_KEY_REVOKE) == []
    else:
        [audit] = audits(app, AuditAction.API_KEY_REVOKE)
        assert audit.reason == "x" * 255


# --- 跨客户、跨项目 -------------------------------------------------------------


def test_another_customers_project_is_an_indistinguishable_404(app, client, admin) -> None:
    mine = Place(client, admin)
    theirs = Place(client, admin)
    their_key = theirs.created()["api_key"]
    before, counts = stored(app), row_counts(app)
    # 我的客户路径 + 别人的项目；我的客户路径 + 不存在的项目。
    probes = []
    for project_id in (theirs.project_id, str(uuid.uuid4())):
        mine.project_id = project_id
        probes += [
            mine.create(),
            mine.listing(),
            mine.rotate(their_key, 1),
            mine.revoke_version(their_key, 1),
            mine.revoke_key(their_key),
        ]

    assert {response.status_code for response in probes} == {404}
    assert {error_code(response) for response in probes} == {"PROJECT_NOT_FOUND"}
    assert len({json.dumps(response.json()["error"]) for response in probes}) == 1
    assert (stored(app), row_counts(app)) == (before, counts)


def test_another_projects_key_is_an_indistinguishable_404(app, client, admin) -> None:
    mine = Place(client, admin)
    theirs = Place(client, admin)
    my_key = mine.created()["api_key"]
    their_key = theirs.created()["api_key"]
    before, counts = stored(app), row_counts(app)

    probes = [
        mine.rotate(their_key, 1),
        mine.revoke_version(their_key, 1),
        mine.revoke_key(their_key),
        mine.rotate(ZERO_API_KEY, 1),
        mine.revoke_key(ZERO_API_KEY),
        # 我的 key，不存在的版本。
        mine.revoke_version(my_key, 2),
        mine.revoke_version(my_key, 0),
        mine.revoke_version(my_key, -1),
    ]

    assert {response.status_code for response in probes} == {404}
    assert {error_code(response) for response in probes} == {"CREDENTIAL_NOT_FOUND"}
    assert len({json.dumps(response.json()["error"]) for response in probes}) == 1
    assert (stored(app), row_counts(app)) == (before, counts)


def test_an_unknown_customer_is_customer_not_found(app, place) -> None:
    place.customer_id = str(uuid.uuid4())
    before = row_counts(app)

    responses = [place.create(), place.listing(), place.revoke_key(ZERO_API_KEY)]

    assert {error_code(response) for response in responses} == {"CUSTOMER_NOT_FOUND"}
    assert row_counts(app) == before


# --- 主密钥未配置 ---------------------------------------------------------------


def test_without_a_master_key_create_and_rotate_say_so(tmp_path) -> None:
    """503 `ENCRYPTION_NOT_CONFIGURED`，不写库；列表与吊销不需要主密钥，照常工作。"""
    application = in_memory_app(settings_for(tmp_path, master_key=False))
    try:
        client = TestClient(application)
        place = Place(client, token_for(application, make_user(application)))
        insert_probe_credential(application, place.project_id)
        before, counts = stored(application), row_counts(application)

        refused = [place.create(), place.rotate(ZERO_API_KEY, 1)]

        for response in refused:
            assert (response.status_code, error_code(response)) == (
                503,
                "ENCRYPTION_NOT_CONFIGURED",
            )
        assert (stored(application), row_counts(application)) == (before, counts)

        listed = place.listing()
        assert listed.status_code == 200
        assert [item["api_key"] for item in listed.json()["data"]["items"]] == [ZERO_API_KEY]
        revoked = place.revoke_version(ZERO_API_KEY, 1)
        assert (revoked.status_code, revoked.json()["data"]["status"]) == (200, "REVOKED")
    finally:
        application.state.engine.dispose()


# --- 处理函数顺序 ---------------------------------------------------------------


@pytest.mark.parametrize("handler", HANDLERS, ids=lambda handler: handler.__name__)
def test_the_handler_calls_require_admin_first(handler) -> None:
    """设计 §7：函数体第一条语句（跳过 docstring）是对 `require_admin` 的调用。"""
    source = textwrap.dedent(inspect.getsource(handler))
    function = ast.parse(source).body[0]
    assert isinstance(function, ast.FunctionDef)
    body = function.body
    if ast.get_docstring(function) is not None:
        body = body[1:]
    first = body[0]
    call = first.value if isinstance(first, ast.Assign | ast.Expr) else None
    assert isinstance(call, ast.Call)
    assert isinstance(call.func, ast.Name) and call.func.id == "require_admin"


# --- 日志里没有 secret ----------------------------------------------------------


def test_no_secret_ever_reaches_the_logs(tmp_path, monkeypatch) -> None:
    """建凭据成功一次、轮换一次，再让一次轮换在写审计时失败：日志里没有任何一个 secret。

    引擎由 `create_database_engine` 建（`create_app` 用的就是它），与生产同一个工厂。
    """
    database = tmp_path / "billing.db"
    url = f"sqlite+pysqlite:///{database.as_posix()}"
    application = create_app(settings_for(tmp_path, database_url=url))
    engine = application.state.engine
    assert engine is not None
    Base.metadata.create_all(engine)
    http = TestClient(application, raise_server_exceptions=False)
    generated: list[str] = []
    real_secret = integration_access._new_secret

    def recording() -> str:
        value = real_secret()
        generated.append(value)
        return value

    monkeypatch.setattr(integration_access, "_new_secret", recording)

    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        place = Place(http, token_for(application, make_user(application)))
        created = place.create()
        rotated = place.rotate(created.json()["data"]["api_key"], 1)
        real_audit = integration_access.record_audit

        def broken_audit(session: Session, **options: object) -> None:
            # created_at 为 NULL：提交时 NOT NULL 失败，整个事务回滚，按 500 返回。
            real_audit(session, **{**options, "now": None})

        monkeypatch.setattr(integration_access, "record_audit", broken_audit)
        failed = place.rotate(created.json()["data"]["api_key"], 2)
    finally:
        root.removeHandler(handler)

    try:
        assert created.status_code == 201, created.text
        assert rotated.status_code == 201, rotated.text
        assert (failed.status_code, error_code(failed)) == (500, "INTERNAL_ERROR")
        assert len(generated) == 3
        logged = stream.getvalue()
        # 这次的异常确实进了日志 —— 否则下面那条是在空字符串上空转。
        assert "NOT NULL constraint failed: audit_logs.created_at" in logged
        assert "SQL parameters hidden" in logged
        for secret in generated:
            assert secret not in logged
            assert secret not in failed.text
        # 失败那次什么都没留下：还是两个版本，轮换审计只有成功的那一条。
        with application.state.session_factory() as session:
            assert count_rows(session, IntegrationCredential) == 2
        assert len(audits(application, AuditAction.API_KEY_ROTATE)) == 1
    finally:
        engine.dispose()

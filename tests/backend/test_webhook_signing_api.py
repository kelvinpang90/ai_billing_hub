"""Outbound webhook signing-secret endpoints on SQLite (design gate #135 v1 §2, §7).

这里测接口契约：正常路径、字段白名单、secret 只出现一次、`Cache-Control: no-store`、校验
边界、跨客户与跨项目的 404、状态机的 409 与幂等、主密钥未配置、日志里没有 secret、处理函数
第一条语句是 `require_admin`。匿名 401 / CUSTOMER 403 由 test_admin_customers_api.py 的路由
枚举用例覆盖；锁、并发、生成列唯一索引、复合外键与回滚在 test_webhook_signing_service.py
与 test_migrations.py。

⚠️ secret 在这里都是运行时随机生成的；文件里只出现全零占位值（设计 §2「secret 格式」）。
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
from app.models.integration import ProjectWebhookSecret
from app.models.tenancy import Project
from app.repositories.webhook_signing import insert_pending
from app.services import webhook_signing
from app.services.auth import utc_now

CUSTOMERS = "/api/v1/admin/customers"

# 响应字段白名单（设计 §2「版本对象」）。断言「恰好等于」：内部 id、tenant_id、project_id、
# 密文、主密钥版本、生成列谁多出来都红。
VERSION_FIELDS = {"key_version", "status", "created_at", "activated_at", "retired_at"}
ISSUED_FIELDS = VERSION_FIELDS | {"secret"}
PAGE_FIELDS = {"items", "page", "page_size", "total"}
ENVELOPE_FIELDS = {"success", "data", "error", "request_id"}

SECRET_PATTERN = re.compile(r"whs_[0-9a-f]{64}")

# 全零占位值：只用来探「请求体不许带这些字段」，从不参与签名。
ZERO_SECRET = "whs_" + "0" * 64
REASON = "Integration backend rotated"

HANDLERS = [
    admin_customers.issue_webhook_secret,
    admin_customers.list_webhook_secrets,
    admin_customers.activate_webhook_secret,
    admin_customers.retire_webhook_secret,
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
    """One customer's project, and the URLs of its webhook secrets."""

    def __init__(self, client: TestClient, headers: dict[str, str]) -> None:
        self.client = client
        self.headers = headers
        self.customer_id = new_customer(client, headers)
        self.project_id = new_project(client, headers, self.customer_id)

    @property
    def base(self) -> str:
        return f"{CUSTOMERS}/{self.customer_id}/projects/{self.project_id}/webhook-secrets"

    def issue(self):
        return self.client.post(self.base, json={}, headers=self.headers)

    def issued(self) -> dict:
        response = self.issue()
        assert response.status_code == 201, response.text
        return response.json()["data"]

    def listing(self, **params: int):
        return self.client.get(self.base, params=params, headers=self.headers)

    def activate(self, version: object):
        url = f"{self.base}/{version}/activate"
        return self.client.post(url, json={}, headers=self.headers)

    def retire(self, version: object, reason: str = REASON):
        url = f"{self.base}/{version}/retire"
        return self.client.post(url, json={"reason": reason}, headers=self.headers)


@pytest.fixture
def place(client, admin) -> Place:
    return Place(client, admin)


def count_rows(session: Session, model) -> int:
    return session.execute(select(func.count()).select_from(model)).scalar_one()


def row_counts(application: FastAPI) -> dict[str, int]:
    models = (ProjectWebhookSecret, AuditLog, Project)
    with application.state.session_factory() as session:
        return {model.__tablename__: count_rows(session, model) for model in models}


def stored(application: FastAPI) -> list[tuple]:
    statement = select(
        ProjectWebhookSecret.project_id,
        ProjectWebhookSecret.key_version,
        ProjectWebhookSecret.status,
        ProjectWebhookSecret.activated_at,
        ProjectWebhookSecret.retired_at,
        ProjectWebhookSecret.encrypted_secret,
    ).order_by(ProjectWebhookSecret.id)
    with application.state.session_factory() as session:
        return [tuple(row) for row in session.execute(statement)]


def audits(application: FastAPI, action: AuditAction) -> list[AuditLog]:
    statement = select(AuditLog).where(AuditLog.action == action).order_by(AuditLog.id)
    with application.state.session_factory() as session:
        return list(session.execute(statement).scalars())


def insert_probe_pending(application: FastAPI, project_id: str) -> None:
    """A PENDING version 1, written directly (no master key needed)."""
    with application.state.session_factory() as session:
        project = session.execute(
            select(Project).where(Project.public_id == project_id)
        ).scalar_one()
        insert_pending(
            session,
            tenant_id=project.tenant_id,
            project_id=project.id,
            key_version=1,
            encrypted_secret="not-a-real-ciphertext",
            encryption_key_version=1,
            now=utc_now().replace(microsecond=0),
        )
        session.commit()


def versions(response) -> list[tuple[int, str]]:
    return [(item["key_version"], item["status"]) for item in response.json()["data"]]


# --- 正常路径 -------------------------------------------------------------------


def test_the_normal_rotation_path(app, place, admin_id) -> None:
    """设计 §7「正常路径」：签发 → 列表 → 启用 → 再签发 → 启用；每步一条对应审计。"""
    first = place.issue()
    assert first.status_code == 201, first.text
    body = first.json()
    assert set(body) == ENVELOPE_FIELDS
    assert (body["success"], body["error"]) == (True, None)
    data = body["data"]
    assert set(data) == ISSUED_FIELDS
    assert SECRET_PATTERN.fullmatch(data["secret"])
    assert (data["key_version"], data["status"]) == (1, "PENDING")
    assert (data["activated_at"], data["retired_at"]) == (None, None)
    assert len(audits(app, AuditAction.WEBHOOK_SECRET_ISSUE)) == 1

    listed = place.listing()
    assert listed.status_code == 200
    assert [(i["key_version"], i["status"]) for i in listed.json()["data"]["items"]] == [
        (1, "PENDING")
    ]

    activated = place.activate(1)
    assert activated.status_code == 200, activated.text
    assert versions(activated) == [(1, "ACTIVE")]
    assert activated.json()["data"][0]["activated_at"] is not None
    assert len(audits(app, AuditAction.WEBHOOK_SECRET_ACTIVATE)) == 1

    second = place.issued()
    assert (second["key_version"], second["status"]) == (2, "PENDING")
    assert second["secret"] != data["secret"]
    assert len(audits(app, AuditAction.WEBHOOK_SECRET_ISSUE)) == 2

    switched = place.activate(2)
    assert switched.status_code == 200
    assert versions(switched) == [(1, "RETIRED"), (2, "ACTIVE")]
    old = switched.json()["data"][0]
    assert old["retired_at"] is not None
    assert len(audits(app, AuditAction.WEBHOOK_SECRET_ACTIVATE)) == 2

    for audit in audits(app, AuditAction.WEBHOOK_SECRET_ACTIVATE):
        assert (audit.actor_user_id, audit.actor_role) == (admin_id, "ADMIN")
        assert (audit.entity_type, audit.entity_id) == ("project_webhook_secret", place.project_id)


def test_the_secret_is_returned_exactly_once(app, place) -> None:
    """设计 §7：签发之后，列表、启用、退役的响应体里都搜不到那个 secret，也没有密文。"""
    issued = place.issued()
    secret = issued["secret"]

    later = [
        place.listing(),
        place.activate(1),
        place.listing(),
        place.retire(1),
        place.listing(),
    ]

    assert [response.status_code for response in later] == [200] * 5
    for response in later:
        assert secret not in response.text
    # 列表项、启用返回的各版本、退役返回的版本：都是版本对象，没有 secret 字段。
    for item in later[0].json()["data"]["items"] + later[4].json()["data"]["items"]:
        assert set(item) == VERSION_FIELDS
    assert all(set(item) == VERSION_FIELDS for item in later[1].json()["data"])
    assert set(later[3].json()["data"]) == VERSION_FIELDS
    for ciphertext in (row[5] for row in stored(app)):
        for response in later:
            assert ciphertext not in response.text


def test_every_response_is_no_store(place) -> None:
    responses = [place.issue(), place.listing(), place.activate(1), place.retire(1)]

    for response in responses:
        assert response.status_code in (200, 201), response.text
        assert response.headers["cache-control"] == "no-store"


def test_the_stored_secret_is_ciphertext(app, place) -> None:
    secret = place.issued()["secret"]

    [row] = stored(app)

    assert secret not in row[5]
    assert secret[4:] not in row[5]


# --- 列表 -----------------------------------------------------------------------


def test_versions_are_listed_oldest_first_and_paged(place) -> None:
    for version in (1, 2, 3):
        assert place.issued()["key_version"] == version
        assert place.retire(version).status_code == 200

    page_one = place.listing(page=1, page_size=2).json()["data"]
    page_two = place.listing(page=2, page_size=2).json()["data"]

    assert set(page_one) == PAGE_FIELDS
    assert [item["key_version"] for item in page_one["items"]] == [1, 2]
    assert [item["key_version"] for item in page_two["items"]] == [3]
    assert page_one["total"] == page_two["total"] == 3


@pytest.mark.parametrize("query", ["page_size=0", "page_size=101", "page=0", "page=10001"])
def test_paging_bounds(place, query: str) -> None:
    response = place.client.get(f"{place.base}?{query}", headers=place.headers)

    assert (response.status_code, error_code(response)) == (422, "VALIDATION_ERROR")


# --- 状态机 ---------------------------------------------------------------------


def test_issuing_while_pending_is_refused(app, place) -> None:
    place.issued()
    before, counts = stored(app), row_counts(app)

    response = place.issue()

    assert (response.status_code, error_code(response)) == (409, "WEBHOOK_SECRET_PENDING_EXISTS")
    assert (stored(app), row_counts(app)) == (before, counts)


def test_activating_a_retired_version_is_refused(app, place) -> None:
    place.issued()
    assert place.retire(1).status_code == 200
    before, counts = stored(app), row_counts(app)

    response = place.activate(1)

    assert (response.status_code, error_code(response)) == (409, "WEBHOOK_SECRET_NOT_PENDING")
    assert (stored(app), row_counts(app)) == (before, counts)


def test_activating_twice_is_200_and_writes_nothing(app, place) -> None:
    place.issued()
    first = place.activate(1)
    before, counts = stored(app), row_counts(app)

    second = place.activate(1)

    assert first.status_code == second.status_code == 200
    assert second.json()["data"] == first.json()["data"]
    assert (stored(app), row_counts(app)) == (before, counts)
    assert len(audits(app, AuditAction.WEBHOOK_SECRET_ACTIVATE)) == 1


@pytest.mark.parametrize("state", ["PENDING", "ACTIVE"])
def test_retiring_twice_is_200_and_writes_nothing(app, place, state: str) -> None:
    place.issued()
    if state == "ACTIVE":
        assert place.activate(1).status_code == 200

    first = place.retire(1)
    before, counts = stored(app), row_counts(app)
    second = place.retire(1)

    assert first.status_code == second.status_code == 200
    data = first.json()["data"]
    assert set(data) == VERSION_FIELDS
    assert data["status"] == "RETIRED"
    assert data["retired_at"] is not None
    assert (data["activated_at"] is not None) is (state == "ACTIVE")
    assert second.json()["data"] == data
    assert (stored(app), row_counts(app)) == (before, counts)
    [audit] = audits(app, AuditAction.WEBHOOK_SECRET_RETIRE)
    assert audit.reason == REASON


def test_retiring_the_active_leaves_the_project_unsigned(app, place) -> None:
    """退役 ACTIVE 后项目没有签名密钥，要等新版本启用（设计 §4）。"""
    place.issued()
    assert place.activate(1).status_code == 200
    assert place.retire(1).status_code == 200

    items = place.listing().json()["data"]["items"]

    assert [item["status"] for item in items] == ["RETIRED"]
    # 版本号只增不减，退役的不复用。
    assert place.issued()["key_version"] == 2


# --- 校验边界 -------------------------------------------------------------------


@pytest.mark.parametrize(
    "extra",
    [
        {"secret": ZERO_SECRET},
        {"key_version": 1},
        {"status": "ACTIVE"},
        {"tenant_id": 1},
        {"project_id": "00000000-0000-4000-8000-000000000000"},
    ],
    ids=["secret", "key_version", "status", "tenant_id", "project_id"],
)
@pytest.mark.parametrize("endpoint", ["issue", "activate", "retire"])
def test_bodies_refuse_extra_fields(app, place, endpoint: str, extra: dict) -> None:
    """extra="forbid"：请求体不能指定 secret、版本、状态或任何 id（设计 §2）。"""
    insert_probe_pending(app, place.project_id)
    url, body = {
        "issue": (place.base, {}),
        "activate": (f"{place.base}/1/activate", {}),
        "retire": (f"{place.base}/1/retire", {"reason": REASON}),
    }[endpoint]
    before, counts = stored(app), row_counts(app)

    response = place.client.post(url, json={**body, **extra}, headers=place.headers)

    assert (response.status_code, error_code(response)) == (422, "VALIDATION_ERROR")
    assert (stored(app), row_counts(app)) == (before, counts)
    # 422 只列字段名，不回显值。
    assert ZERO_SECRET not in response.text


@pytest.mark.parametrize("version", ["0", "-1", "abc", "1.5", str(2**31)])
@pytest.mark.parametrize("endpoint", ["activate", "retire"])
def test_key_version_must_be_a_positive_integer(app, place, endpoint: str, version: str) -> None:
    insert_probe_pending(app, place.project_id)
    before, counts = stored(app), row_counts(app)

    if endpoint == "activate":
        response = place.activate(version)
    else:
        response = place.retire(version)

    assert (response.status_code, error_code(response)) == (422, "VALIDATION_ERROR")
    assert (stored(app), row_counts(app)) == (before, counts)


@pytest.mark.parametrize(
    ("reason", "expected"),
    [("", 422), ("   ", 422), ("x" * 256, 422), ("x" * 255, 200), ("  " + "x" * 255 + "  ", 200)],
    ids=["empty", "blank", "256", "255", "255-padded"],
)
def test_reason_bounds(app, place, reason: str, expected: int) -> None:
    place.issued()
    before = stored(app)

    response = place.retire(1, reason=reason)

    assert response.status_code == expected
    if expected == 422:
        assert error_code(response) == "VALIDATION_ERROR"
        assert audits(app, AuditAction.WEBHOOK_SECRET_RETIRE) == []
        assert stored(app) == before
    else:
        [audit] = audits(app, AuditAction.WEBHOOK_SECRET_RETIRE)
        assert audit.reason == "x" * 255


def test_retire_requires_a_reason(app, place) -> None:
    place.issued()

    response = place.client.post(f"{place.base}/1/retire", json={}, headers=place.headers)

    assert (response.status_code, error_code(response)) == (422, "VALIDATION_ERROR")


# --- 跨客户、跨项目 -------------------------------------------------------------


def test_another_customers_project_is_an_indistinguishable_404(app, client, admin) -> None:
    mine = Place(client, admin)
    theirs = Place(client, admin)
    theirs.issued()
    before, counts = stored(app), row_counts(app)
    # 我的客户路径 + 别人的项目；我的客户路径 + 不存在的项目。
    probes = []
    for project_id in (theirs.project_id, str(uuid.uuid4())):
        mine.project_id = project_id
        probes += [mine.issue(), mine.listing(), mine.activate(1), mine.retire(1)]

    assert {response.status_code for response in probes} == {404}
    assert {error_code(response) for response in probes} == {"PROJECT_NOT_FOUND"}
    assert len({json.dumps(response.json()["error"]) for response in probes}) == 1
    assert (stored(app), row_counts(app)) == (before, counts)


def test_another_projects_version_is_an_indistinguishable_404(app, client, admin) -> None:
    mine = Place(client, admin)
    theirs = Place(client, admin)
    mine.issued()
    theirs.issued()
    assert theirs.retire(1).status_code == 200
    theirs.issued()
    before, counts = stored(app), row_counts(app)

    # 版本 2 只在别人的项目里有；版本 3 谁都没有。
    probes = [mine.activate(2), mine.retire(2), mine.activate(3), mine.retire(3)]

    assert {response.status_code for response in probes} == {404}
    assert {error_code(response) for response in probes} == {"WEBHOOK_SECRET_NOT_FOUND"}
    assert len({json.dumps(response.json()["error"]) for response in probes}) == 1
    assert (stored(app), row_counts(app)) == (before, counts)


def test_an_unknown_customer_is_customer_not_found(app, place) -> None:
    place.customer_id = str(uuid.uuid4())
    before = row_counts(app)

    responses = [place.issue(), place.listing(), place.activate(1), place.retire(1)]

    assert {error_code(response) for response in responses} == {"CUSTOMER_NOT_FOUND"}
    assert {response.status_code for response in responses} == {404}
    assert row_counts(app) == before


# --- 主密钥未配置 ---------------------------------------------------------------


def test_without_a_master_key_only_issue_is_refused(tmp_path) -> None:
    """503 `ENCRYPTION_NOT_CONFIGURED`，不写库；列表、启用、退役不需要主密钥，照常工作。"""
    application = in_memory_app(settings_for(tmp_path, master_key=False))
    try:
        client = TestClient(application)
        place = Place(client, token_for(application, make_user(application)))
        before, counts = stored(application), row_counts(application)

        refused = place.issue()

        assert (refused.status_code, error_code(refused)) == (503, "ENCRYPTION_NOT_CONFIGURED")
        assert (stored(application), row_counts(application)) == (before, counts)

        insert_probe_pending(application, place.project_id)
        listed = place.listing()
        assert listed.status_code == 200
        assert [item["key_version"] for item in listed.json()["data"]["items"]] == [1]
        activated = place.activate(1)
        assert (activated.status_code, versions(activated)) == (200, [(1, "ACTIVE")])
        retired = place.retire(1)
        assert (retired.status_code, retired.json()["data"]["status"]) == (200, "RETIRED")
    finally:
        application.state.engine.dispose()


# --- 处理函数顺序 ---------------------------------------------------------------


@pytest.mark.parametrize("handler", HANDLERS, ids=lambda handler: handler.__name__)
def test_the_handler_calls_require_admin_first(handler) -> None:
    """设计 §7「鉴权」：函数体第一条语句（跳过 docstring）是对 `require_admin` 的调用。"""
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
    """签发、启用各成功一次，再让一次签发在写审计时失败：日志里没有任何一个 secret。

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
    real_secret = webhook_signing._new_secret

    def recording() -> str:
        value = real_secret()
        generated.append(value)
        return value

    monkeypatch.setattr(webhook_signing, "_new_secret", recording)

    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        place = Place(http, token_for(application, make_user(application)))
        issued = place.issue()
        activated = place.activate(1)
        real_audit = webhook_signing.record_audit

        def broken_audit(session: Session, **options: object) -> None:
            # created_at 为 NULL：提交时 NOT NULL 失败，整个事务回滚，按 500 返回。
            real_audit(session, **{**options, "now": None})

        monkeypatch.setattr(webhook_signing, "record_audit", broken_audit)
        failed = place.issue()
    finally:
        root.removeHandler(handler)

    try:
        assert issued.status_code == 201, issued.text
        assert activated.status_code == 200, activated.text
        assert (failed.status_code, error_code(failed)) == (500, "INTERNAL_ERROR")
        assert len(generated) == 2
        logged = stream.getvalue()
        # 这次的异常确实进了日志 —— 否则下面那条是在空字符串上空转。
        assert "NOT NULL constraint failed: audit_logs.created_at" in logged
        assert "SQL parameters hidden" in logged
        for secret in generated:
            assert secret not in logged
            assert secret not in failed.text
        # 失败那次什么都没留下：还是一个版本，签发审计只有成功的那一条。
        with application.state.session_factory() as session:
            assert count_rows(session, ProjectWebhookSecret) == 1
        assert len(audits(application, AuditAction.WEBHOOK_SECRET_ISSUE)) == 1
    finally:
        engine.dispose()

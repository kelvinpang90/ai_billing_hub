"""Administrative AI client-key routes (design gate #281 v23 §2)."""

from __future__ import annotations

import base64
import os
import re
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from app.core.config import Settings
from app.core.database import create_session_factory
from app.core.tokens import issue_access_token
from app.main import create_app
from app.models.auth import User, UserRole, UserStatus
from app.models.base import Base
from app.services import customers
from app.services.auth import RequestContext, utc_now

ROOT = "/api/v1/admin/customers/{customer_id}/projects/{project_id}/ai-access"
CONTEXT = RequestContext(ip_address="203.0.113.12", user_agent="ai-access-api-test")


def settings_for(tmp_path) -> Settings:
    jwt_key = tmp_path / "jwt.key"
    jwt_key.write_text("a-test-signing-key-that-is-long-enough", encoding="utf-8")
    master_key = tmp_path / "master.key"
    master_key.write_text(f"1:{base64.b64encode(os.urandom(32)).decode()}\n", encoding="utf-8")
    return Settings(jwt_secret_file=str(jwt_key), master_key_file=str(master_key), database_url="")


@pytest.fixture
def app(tmp_path):
    application = create_app(settings_for(tmp_path))
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


def user_headers(app, role: UserRole = UserRole.ADMIN) -> dict[str, str]:
    now = utc_now()
    with app.state.session_factory() as session:
        user = User(
            email=f"{uuid.uuid4().hex}@example.com",
            password_hash="not-a-real-hash",
            role=role,
            status=UserStatus.ACTIVE,
            created_at=now,
            updated_at=now,
        )
        session.add(user)
        session.commit()
        token = issue_access_token(
            app.state.settings,
            user_id=int(user.id),
            role=role.value,
            session_id=uuid.uuid4().hex,
            now=now,
        )
    return {"Authorization": f"Bearer {token}"}


def project(app, headers: dict[str, str]) -> tuple[str, str]:
    with app.state.session_factory() as session:
        actor = session.query(User).filter(User.role == UserRole.ADMIN).first()
    assert actor is not None
    customer = customers.create_customer(
        app.state.session_factory,
        actor=actor,
        company_name="AI access customer",
        email="ops@example.com",
        context=CONTEXT,
    )
    created = customers.create_project(
        app.state.session_factory,
        actor=actor,
        customer_id=customer.id,
        name="AI access project",
        description=None,
        context=CONTEXT,
    )
    return customer.id, created.id


def endpoint(customer_id: str, project_id: str) -> str:
    return ROOT.format(customer_id=customer_id, project_id=project_id)


def error_code(response) -> str | None:
    return (response.json().get("error") or {}).get("code")


def test_issue_list_revoke_and_order(client, app) -> None:
    admin = user_headers(app)
    customer_id, project_id = project(app, admin)
    url = endpoint(customer_id, project_id)

    first = client.post(url + "/client-keys", headers=admin)
    second = client.post(url + "/client-keys", headers=admin)

    assert first.status_code == second.status_code == 201
    assert first.headers["Cache-Control"] == second.headers["Cache-Control"] == "no-store"
    first_data, second_data = first.json()["data"], second.json()["data"]
    assert set(first_data) == {"client_id", "token", "status", "created_at"}
    assert re.fullmatch(r"aic_[0-9a-f]{32}", first_data["client_id"])
    assert re.fullmatch(r"ait_[0-9a-f]{64}", first_data["token"])

    listed = client.get(url, headers=admin)
    assert listed.status_code == 200
    assert [item["client_id"] for item in listed.json()["data"]] == [
        second_data["client_id"],
        first_data["client_id"],
    ]
    assert all("token" not in item for item in listed.json()["data"])

    revoked = client.post(url + f"/client-keys/{first_data['client_id']}/revoke", headers=admin)
    assert (revoked.status_code, revoked.json()["data"]["status"]) == (200, "REVOKED")
    repeated = client.post(url + f"/client-keys/{first_data['client_id']}/revoke", headers=admin)
    assert repeated.status_code == 409
    third = client.post(url + "/client-keys", headers=admin)
    assert third.status_code == 201
    assert client.post(url + "/client-keys", headers=admin).status_code == 409
    limit = client.post(url + "/client-keys", headers=admin)
    assert error_code(limit) == "AI_CLIENT_KEY_LIMIT"


def test_not_found_and_non_admin_paths(client, app) -> None:
    admin = user_headers(app)
    customer_id, project_id = project(app, admin)
    url = endpoint(customer_id, project_id)
    unknown = str(uuid.uuid4())

    assert client.get(url).status_code == 401
    assert client.post(url + "/client-keys").status_code == 401
    assert client.get(url, headers=user_headers(app, UserRole.CUSTOMER)).status_code == 403
    missing_customer = client.get(endpoint(unknown, project_id), headers=admin)
    assert error_code(missing_customer) == "CUSTOMER_NOT_FOUND"
    missing_project = client.get(endpoint(customer_id, unknown), headers=admin)
    assert error_code(missing_project) == "PROJECT_NOT_FOUND"
    assert (
        error_code(client.post(url + "/client-keys/aic_missing/revoke", headers=admin))
        == "AI_CLIENT_KEY_NOT_FOUND"
    )

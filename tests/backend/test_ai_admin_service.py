"""AI client-key administration (design gate #281 v23 §2)."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import os
import re
import threading
import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor

import pytest
from alembic.config import Config
from sqlalchemy import Engine, create_engine, delete, func, select
from sqlalchemy.orm import Session, sessionmaker

from alembic import command
from app.ai_api import admin
from app.ai_api.models import AiClientKey, AiClientKeyStatus
from app.core.database import create_session_factory
from app.models.auth import AuditAction, AuditLog, User, UserRole, UserStatus
from app.models.base import Base
from app.models.tenancy import Project, Tenant
from app.repositories import tenancy
from app.services.auth import RequestContext

TEST_DATABASE_URL = os.environ.get("BILLING_TEST_DATABASE_URL", "")
NOW = dt.datetime(2026, 10, 14, 8, 30, 0)
CONTEXT = RequestContext(ip_address="203.0.113.10", user_agent="ai-admin-service-test")
CLIENT_ID_PATTERN = re.compile(r"aic_[0-9a-f]{32}")
TOKEN_PATTERN = re.compile(r"ait_[0-9a-f]{64}")


@pytest.fixture
def factory() -> Iterator[sessionmaker[Session]]:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    try:
        yield create_session_factory(engine)
    finally:
        engine.dispose()


@pytest.fixture
def mysql_factory(monkeypatch) -> Iterator[sessionmaker[Session]]:
    if not TEST_DATABASE_URL:
        pytest.skip("BILLING_TEST_DATABASE_URL is not set; MySQL tests need a real database")
    from app.core.config import get_settings

    monkeypatch.setenv("BILLING_DATABASE_URL", TEST_DATABASE_URL)
    get_settings.cache_clear()
    command.upgrade(Config("alembic.ini"), "head")
    get_settings.cache_clear()
    engine = create_engine(TEST_DATABASE_URL, pool_size=4, max_overflow=2)
    _clean_mysql(engine)
    try:
        yield create_session_factory(engine)
    finally:
        _clean_mysql(engine)
        engine.dispose()


def _clean_mysql(engine: Engine) -> None:
    ours = select(Tenant.id).where(Tenant.company_name.like("AI admin %"))
    with engine.begin() as connection:
        connection.exec_driver_sql("TRUNCATE TABLE audit_logs")
    with engine.begin() as connection:
        connection.execute(delete(AiClientKey).where(AiClientKey.tenant_id.in_(ours)))
        connection.execute(delete(Project).where(Project.tenant_id.in_(ours)))
        connection.execute(delete(Tenant).where(Tenant.company_name.like("AI admin %")))
        connection.execute(
            delete(User).where(User.email.like("%@ai-admin-service-test.example.com"))
        )


def make_admin(factory: sessionmaker[Session]) -> User:
    with factory() as session:
        user = User(
            email=f"{uuid.uuid4().hex}@ai-admin-service-test.example.com",
            password_hash="not-a-real-hash",
            role=UserRole.ADMIN,
            status=UserStatus.ACTIVE,
            created_at=NOW,
            updated_at=NOW,
        )
        session.add(user)
        session.commit()
        return user


def make_project(factory: sessionmaker[Session]) -> tuple[str, str]:
    with factory() as session:
        tenant = tenancy.create_tenant(
            session,
            company_name=f"AI admin {uuid.uuid4().hex}",
            email="ops@example.com",
            now=NOW,
        )
        project = tenancy.create_project(session, tenant_id=tenant.id, name="AI", now=NOW)
        session.commit()
        return tenant.public_id, project.public_id


def issue(factory, actor: User, customer_id: str, project_id: str, now: dt.datetime = NOW):
    return admin.issue_client_key(
        factory,
        actor=actor,
        customer_id=customer_id,
        project_id=project_id,
        context=CONTEXT,
        now=now,
    )


def revoke(factory, actor: User, customer_id: str, project_id: str, client_id: str):
    return admin.revoke_client_key(
        factory,
        actor=actor,
        customer_id=customer_id,
        project_id=project_id,
        client_id=client_id,
        context=CONTEXT,
        now=NOW,
    )


def add_project(factory: sessionmaker[Session], customer_id: str) -> str:
    with factory() as session:
        tenant = session.execute(select(Tenant).where(Tenant.public_id == customer_id)).scalar_one()
        project = tenancy.create_project(session, tenant_id=tenant.id, name="AI 2", now=NOW)
        session.commit()
        return project.public_id


def key_rows(factory, project_id: str) -> list[AiClientKey]:
    with factory() as session:
        project = session.execute(
            select(Project).where(Project.public_id == project_id)
        ).scalar_one()
        return list(
            session.execute(
                select(AiClientKey).where(AiClientKey.project_id == project.id)
            ).scalars()
        )


def test_issue_stores_only_digest_and_returns_token_once(factory) -> None:
    actor = make_admin(factory)
    customer_id, project_id = make_project(factory)
    issued = issue(factory, actor, customer_id, project_id)

    assert CLIENT_ID_PATTERN.fullmatch(issued.client_id)
    assert TOKEN_PATTERN.fullmatch(issued.token)
    [row] = key_rows(factory, project_id)
    assert row.token_sha256 == hashlib.sha256(issued.token.encode()).hexdigest()
    assert issued.token not in row.token_sha256
    assert admin.list_client_keys(factory, customer_id=customer_id, project_id=project_id) == [
        admin.AiClientKeyView(issued.client_id, "ACTIVE", NOW, None)
    ]


def test_list_order_limit_revoke_and_audit(factory) -> None:
    actor = make_admin(factory)
    customer_id, project_id = make_project(factory)
    first = issue(factory, actor, customer_id, project_id)
    second = issue(factory, actor, customer_id, project_id)
    with pytest.raises(admin.AiClientKeyLimit):
        issue(factory, actor, customer_id, project_id)

    revoked = admin.revoke_client_key(
        factory,
        actor=actor,
        customer_id=customer_id,
        project_id=project_id,
        client_id=first.client_id,
        context=CONTEXT,
        now=NOW,
    )
    assert revoked.status == "REVOKED"
    assert [
        view.client_id
        for view in admin.list_client_keys(factory, customer_id=customer_id, project_id=project_id)
    ] == [second.client_id, first.client_id]
    with pytest.raises(admin.AiClientKeyRevoked):
        admin.revoke_client_key(
            factory,
            actor=actor,
            customer_id=customer_id,
            project_id=project_id,
            client_id=first.client_id,
            context=CONTEXT,
            now=NOW,
        )

    with factory() as session:
        audits = list(session.execute(select(AuditLog).order_by(AuditLog.id)).scalars())
    assert [row.action for row in audits] == [
        AuditAction.AI_CLIENT_KEY_CREATE,
        AuditAction.AI_CLIENT_KEY_CREATE,
        AuditAction.AI_CLIENT_KEY_REVOKE,
    ]
    for row in audits:
        payload = f"{row.before_state or ''}{row.after_state or ''}"
        assert "token" not in payload
        assert "token_sha256" not in payload


def test_not_found_and_audit_failure_roll_back(factory, monkeypatch) -> None:
    actor = make_admin(factory)
    customer_id, project_id = make_project(factory)
    with pytest.raises(admin.CustomerNotFound):
        admin.list_client_keys(factory, customer_id="missing", project_id=project_id)
    with pytest.raises(admin.ProjectNotFound):
        admin.list_client_keys(factory, customer_id=customer_id, project_id="missing")
    with pytest.raises(admin.AiClientKeyNotFound):
        admin.revoke_client_key(
            factory,
            actor=actor,
            customer_id=customer_id,
            project_id=project_id,
            client_id="aic_missing",
            context=CONTEXT,
            now=NOW,
        )

    def fail_audit(*_args, **_kwargs) -> None:
        raise RuntimeError("injected audit failure")

    monkeypatch.setattr(admin, "record_audit", fail_audit)
    with pytest.raises(RuntimeError, match="injected audit failure"):
        issue(factory, actor, customer_id, project_id)
    assert key_rows(factory, project_id) == []


def test_list_is_newest_first_with_same_second_ties_by_id(factory) -> None:
    actor = make_admin(factory)
    customer_id, project_id = make_project(factory)
    first = issue(factory, actor, customer_id, project_id)
    later = issue(factory, actor, customer_id, project_id, now=NOW + dt.timedelta(minutes=1))
    revoke(factory, actor, customer_id, project_id, first.client_id)
    same_second = issue(factory, actor, customer_id, project_id)

    views = admin.list_client_keys(factory, customer_id=customer_id, project_id=project_id)
    assert [view.client_id for view in views] == [
        later.client_id,
        same_second.client_id,
        first.client_id,
    ]
    assert [view.status for view in views] == ["ACTIVE", "ACTIVE", "REVOKED"]
    assert views[2].revoked_at == NOW
    assert set(vars(views[0])) == {"client_id", "status", "created_at", "revoked_at"}


def test_lookups_never_cross_customers_or_projects(factory) -> None:
    actor = make_admin(factory)
    customer_id, project_id = make_project(factory)
    sibling_project_id = add_project(factory, customer_id)
    other_customer_id, other_project_id = make_project(factory)
    key = issue(factory, actor, customer_id, project_id)

    with pytest.raises(admin.ProjectNotFound):
        admin.list_client_keys(factory, customer_id=customer_id, project_id=other_project_id)
    with pytest.raises(admin.ProjectNotFound):
        issue(factory, actor, other_customer_id, project_id)
    with pytest.raises(admin.ProjectNotFound):
        revoke(factory, actor, other_customer_id, project_id, key.client_id)
    with pytest.raises(admin.CustomerNotFound):
        issue(factory, actor, "missing", project_id)
    with pytest.raises(admin.AiClientKeyNotFound):
        revoke(factory, actor, customer_id, sibling_project_id, key.client_id)

    [row] = key_rows(factory, project_id)
    assert row.status is AiClientKeyStatus.ACTIVE
    assert row.revoked_at is None


def test_revoke_rolls_back_when_audit_write_fails(factory, monkeypatch) -> None:
    actor = make_admin(factory)
    customer_id, project_id = make_project(factory)
    key = issue(factory, actor, customer_id, project_id)

    def fail_audit(*_args, **_kwargs) -> None:
        raise RuntimeError("injected audit failure")

    monkeypatch.setattr(admin, "record_audit", fail_audit)
    with pytest.raises(RuntimeError, match="injected audit failure"):
        revoke(factory, actor, customer_id, project_id, key.client_id)
    [row] = key_rows(factory, project_id)
    assert row.status is AiClientKeyStatus.ACTIVE
    assert row.revoked_at is None


def test_token_stays_out_of_audit_logs_and_errors(factory, caplog) -> None:
    caplog.set_level(logging.DEBUG)
    actor = make_admin(factory)
    customer_id, project_id = make_project(factory)
    first = issue(factory, actor, customer_id, project_id)
    second = issue(factory, actor, customer_id, project_id)
    with pytest.raises(admin.AiClientKeyLimit) as limit:
        issue(factory, actor, customer_id, project_id)
    revoke(factory, actor, customer_id, project_id, first.client_id)
    with pytest.raises(admin.AiClientKeyRevoked) as revoked:
        revoke(factory, actor, customer_id, project_id, first.client_id)

    with factory() as session:
        audits = list(session.execute(select(AuditLog).order_by(AuditLog.id)).scalars())
    assert [(row.entity_type, row.entity_id) for row in audits] == [
        ("ai_client_key", first.client_id),
        ("ai_client_key", second.client_id),
        ("ai_client_key", first.client_id),
    ]
    assert json.loads(audits[0].after_state) == {"client_id": first.client_id, "status": "ACTIVE"}
    assert audits[0].before_state is None
    assert json.loads(audits[2].before_state) == {"client_id": first.client_id, "status": "ACTIVE"}
    assert json.loads(audits[2].after_state) == {
        "client_id": first.client_id,
        "status": "REVOKED",
    }

    digests = [row.token_sha256 for row in key_rows(factory, project_id)]
    surfaces = [
        caplog.text,
        str(limit.value),
        repr(limit.value),
        str(revoked.value),
        repr(revoked.value),
        *(f"{row.before_state}{row.after_state}{row.reason}" for row in audits),
    ]
    for secret in (first.token, second.token, *digests):
        for surface in surfaces:
            assert secret not in surface


def test_mysql_concurrent_issue_allows_exactly_one_second_key(mysql_factory, monkeypatch) -> None:
    actor = make_admin(mysql_factory)
    customer_id, project_id = make_project(mysql_factory)
    issue(mysql_factory, actor, customer_id, project_id)
    with mysql_factory() as session:
        before_creates = session.execute(
            select(func.count())
            .select_from(AuditLog)
            .where(AuditLog.action == AuditAction.AI_CLIENT_KEY_CREATE)
        ).scalar_one()
    barrier = threading.Barrier(2)
    original = admin._locked_project

    def synchronized_lock(*args, **kwargs):
        barrier.wait(timeout=5)
        return original(*args, **kwargs)

    monkeypatch.setattr(admin, "_locked_project", synchronized_lock)
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(issue, mysql_factory, actor, customer_id, project_id) for _ in range(2)
        ]
    outcomes = []
    for future in futures:
        try:
            outcomes.append(future.result())
        except admin.AiClientKeyLimit:
            outcomes.append("limit")
    assert sum(item == "limit" for item in outcomes) == 1
    assert sum(item != "limit" for item in outcomes) == 1
    with mysql_factory() as session:
        project = session.execute(
            select(Project).where(Project.public_id == project_id)
        ).scalar_one()
        active = session.execute(
            select(func.count())
            .select_from(AiClientKey)
            .where(
                AiClientKey.project_id == project.id,
                AiClientKey.status == AiClientKeyStatus.ACTIVE,
            )
        ).scalar_one()
        creates_after = session.execute(
            select(func.count())
            .select_from(AuditLog)
            .where(AuditLog.action == AuditAction.AI_CLIENT_KEY_CREATE)
        ).scalar_one()
    assert active == 2
    assert creates_after == before_creates + 1

"""The AI-module MySQL world shared by the delivery and ops tests (registered in conftest.py).

不以 `test_` 开头，pytest 不会把它当用例收集。conftest.py 导入它时还没执行
`pytest_configure`（那里给每个 xdist worker 换库名），所以库地址必须在 fixture 里读，
不能在模块顶层读。
"""

from __future__ import annotations

import datetime as dt
import os
import uuid
from collections.abc import Iterator
from dataclasses import dataclass

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, delete
from sqlalchemy.orm import Session, sessionmaker

from alembic import command
from app.ai_api.database import create_ai_session_factory
from app.ai_api.outbox import AiUsageOutbox
from app.core.config import get_settings
from app.models.integration import IntegrationCredential
from app.models.tenancy import Project, Tenant
from app.models.usage import UsageEvent


@dataclass(frozen=True)
class World:
    factory: sessionmaker[Session]
    tenant_id: int
    project_id: int
    tenant_public_id: str
    project_public_id: str


@pytest.fixture
def mysql_world(monkeypatch) -> Iterator[World]:
    url = os.environ.get("BILLING_TEST_DATABASE_URL", "")
    if not url:
        pytest.skip("BILLING_TEST_DATABASE_URL is not set; these cases need real MySQL")
    monkeypatch.setenv("BILLING_DATABASE_URL", url)
    get_settings.cache_clear()
    command.upgrade(Config("alembic.ini"), "head")
    get_settings.cache_clear()
    engine = create_engine(url, pool_size=6, max_overflow=2)
    factory = create_ai_session_factory(engine)
    now = dt.datetime.now(dt.UTC).replace(tzinfo=None)
    with factory.begin() as session:
        tenant_public_id = str(uuid.uuid4())
        project_public_id = str(uuid.uuid4())
        tenant = Tenant(
            public_id=tenant_public_id,
            company_name="AI delivery",
            email="delivery@example.com",
            created_at=now,
            updated_at=now,
        )
        session.add(tenant)
        session.flush()
        project = Project(
            public_id=project_public_id,
            tenant_id=tenant.id,
            name="AI delivery",
            created_at=now,
            updated_at=now,
        )
        session.add(project)
        session.flush()
        world = World(factory, tenant.id, project.id, tenant_public_id, project_public_id)
    try:
        yield world
    finally:
        with engine.begin() as connection:
            connection.execute(
                delete(AiUsageOutbox).where(AiUsageOutbox.tenant_id == world.tenant_id)
            )
            connection.execute(delete(UsageEvent).where(UsageEvent.tenant_id == world.tenant_id))
            connection.execute(
                delete(IntegrationCredential).where(
                    IntegrationCredential.tenant_id == world.tenant_id
                )
            )
            connection.execute(delete(Project).where(Project.id == world.project_id))
            connection.execute(delete(Tenant).where(Tenant.id == world.tenant_id))
        engine.dispose()

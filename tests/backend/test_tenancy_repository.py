"""Tenant / project repository on in-memory SQLite (spec §75, §76, §97, §115).

⚠️ 这里的表是 `create_all` 按**模型**建的，不是迁移建的；而且 SQLite 默认不强制
外键、不强制 VARCHAR 长度。所以迁移建出来的外键 RESTRICT、`public_id` 唯一约束与
列形状只能在真 MySQL 上验，那几条在 `test_migrations.py`（CI 里跑）。
这里只测 repository 的行为：往返读写、`public_id` 的生成、跨租户隔离、只 flush 不 commit。
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from sqlalchemy import create_engine

from app.core.database import create_session_factory
from app.models.base import Base
from app.repositories.tenancy import (
    count_projects_for_tenant,
    create_project,
    create_tenant,
    get_project_for_tenant,
    get_tenant_by_public_id,
    list_projects_for_tenant,
    list_tenants,
)

# 固定值而不是 utc_now()：断言「存进去的就是调用方给的那个时刻」要能逐字比对。
NOW = dt.datetime(2026, 9, 19, 8, 30, 0)


@pytest.fixture
def session_factory():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    yield create_session_factory(engine)
    engine.dispose()


def make_tenant(session, company_name: str = "Acme Sdn Bhd"):
    return create_tenant(session, company_name=company_name, email="ops@example.com", now=NOW)


def test_tenant_round_trips(session_factory) -> None:
    with session_factory() as session:
        created = create_tenant(
            session,
            company_name="Acme Sdn Bhd",
            email="ops@example.com",
            contact_name="Contact Person",
            phone="+60 3-0000 0000",
            now=NOW,
        )
        assert created.id is not None, "flush 之后就应该有自增 id"
        public_id = created.public_id
        session.commit()

    with session_factory() as session:
        loaded = get_tenant_by_public_id(session, public_id)

        assert loaded is not None
        assert loaded.company_name == "Acme Sdn Bhd"
        assert loaded.email == "ops@example.com"
        assert loaded.contact_name == "Contact Person"
        assert loaded.phone == "+60 3-0000 0000"
        assert loaded.created_at == NOW
        assert loaded.updated_at == NOW


def test_optional_tenant_fields_default_to_null(session_factory) -> None:
    with session_factory() as session:
        tenant = make_tenant(session)
        session.commit()
        public_id = tenant.public_id

    with session_factory() as session:
        loaded = get_tenant_by_public_id(session, public_id)

        assert loaded is not None
        assert loaded.contact_name is None
        assert loaded.phone is None


def test_tenant_email_is_not_unique(session_factory) -> None:
    """联系邮箱不是登录账号：同一联系人可以对应多家公司。"""
    with session_factory() as session:
        first = make_tenant(session, "First Sdn Bhd")
        second = make_tenant(session, "Second Sdn Bhd")
        session.commit()

        assert first.email == second.email
        assert first.id != second.id


def test_unknown_tenant_public_id_returns_none(session_factory) -> None:
    with session_factory() as session:
        make_tenant(session)
        session.commit()

        assert get_tenant_by_public_id(session, str(uuid.uuid4())) is None


def test_project_round_trips(session_factory) -> None:
    with session_factory() as session:
        tenant = make_tenant(session)
        created = create_project(
            session, tenant_id=tenant.id, name="Chatbot", description="Pilot app", now=NOW
        )
        tenant_id, public_id = tenant.id, created.public_id
        session.commit()

    with session_factory() as session:
        loaded = get_project_for_tenant(session, tenant_id, public_id)

        assert loaded is not None
        assert loaded.tenant_id == tenant_id
        assert loaded.name == "Chatbot"
        assert loaded.description == "Pilot app"
        assert loaded.created_at == NOW
        assert loaded.updated_at == NOW


def test_project_description_is_optional(session_factory) -> None:
    with session_factory() as session:
        tenant = make_tenant(session)
        project = create_project(session, tenant_id=tenant.id, name="Chatbot", now=NOW)
        session.commit()

        loaded = get_project_for_tenant(session, tenant.id, project.public_id)
        assert loaded is not None
        assert loaded.description is None


def test_public_ids_are_distinct_uuid4_strings(session_factory) -> None:
    with session_factory() as session:
        tenants = [make_tenant(session, f"Company {n}") for n in range(3)]
        projects = [
            create_project(session, tenant_id=tenants[0].id, name=f"Project {n}", now=NOW)
            for n in range(3)
        ]
        session.commit()

        public_ids = [row.public_id for row in (*tenants, *projects)]

    assert len(set(public_ids)) == len(public_ids)
    for public_id in public_ids:
        parsed = uuid.UUID(public_id)
        assert parsed.version == 4
        # 规范的 36 字符形式，恰好装进 CHAR(36)。
        assert str(parsed) == public_id


def test_project_of_another_tenant_is_not_found(session_factory) -> None:
    """⚠️ 跨租户与不存在**必须不可区分**（spec §97、§115）：都返回 None。"""
    with session_factory() as session:
        owner = make_tenant(session, "Owner Sdn Bhd")
        other = make_tenant(session, "Other Sdn Bhd")
        project = create_project(session, tenant_id=owner.id, name="Private", now=NOW)
        session.commit()

        assert get_project_for_tenant(session, owner.id, project.public_id) is not None
        assert get_project_for_tenant(session, other.id, project.public_id) is None
        assert get_project_for_tenant(session, owner.id, str(uuid.uuid4())) is None


def test_project_list_is_scoped_to_the_tenant(session_factory) -> None:
    with session_factory() as session:
        owner = make_tenant(session, "Owner Sdn Bhd")
        other = make_tenant(session, "Other Sdn Bhd")
        first = create_project(session, tenant_id=owner.id, name="First", now=NOW)
        create_project(session, tenant_id=other.id, name="Theirs", now=NOW)
        second = create_project(session, tenant_id=owner.id, name="Second", now=NOW)
        empty = make_tenant(session, "Empty Sdn Bhd")
        session.commit()

        owned = list_projects_for_tenant(session, owner.id)

        assert [p.public_id for p in owned] == [first.public_id, second.public_id]
        assert all(p.tenant_id == owner.id for p in owned)
        assert [p.name for p in list_projects_for_tenant(session, other.id)] == ["Theirs"]
        assert list_projects_for_tenant(session, empty.id) == []


def test_tenants_are_paged_newest_first_with_a_total(session_factory) -> None:
    """AIH-TASK-006（设计闸门 #96 §2）：客户列表最新在前，`total` 是全部条数。"""
    with session_factory() as session:
        created = [make_tenant(session, f"Company {n}") for n in range(5)]
        session.commit()
        newest_first = [tenant.public_id for tenant in reversed(created)]

        pages = [list_tenants(session, offset=offset, limit=2) for offset in (0, 2, 4, 10)]

    assert [[tenant.public_id for tenant in rows] for rows, _ in pages] == [
        newest_first[0:2],
        newest_first[2:4],
        newest_first[4:],
        [],
    ]
    # 超出末页也照报总数：客户端靠它判断「没有更多了」而不是「出错了」。
    assert [total for _, total in pages] == [5, 5, 5, 5]


def test_an_empty_tenant_table_lists_nothing(session_factory) -> None:
    with session_factory() as session:
        assert list_tenants(session, offset=0, limit=20) == ([], 0)


def test_projects_can_be_paged_and_counted_per_tenant(session_factory) -> None:
    with session_factory() as session:
        owner = make_tenant(session, "Owner Sdn Bhd")
        other = make_tenant(session, "Other Sdn Bhd")
        owned = [
            create_project(session, tenant_id=owner.id, name=f"Project {n}", now=NOW)
            for n in range(5)
        ]
        create_project(session, tenant_id=other.id, name="Theirs", now=NOW)
        session.commit()
        ids = [project.public_id for project in owned]

        def page(offset: int, limit: int) -> list[str]:
            rows = list_projects_for_tenant(session, owner.id, offset=offset, limit=limit)
            return [project.public_id for project in rows]

        # 最早在前，只含本租户的项目。
        assert page(0, 2) == ids[0:2]
        assert page(2, 2) == ids[2:4]
        assert page(4, 2) == ids[4:]
        assert page(6, 2) == []
        assert count_projects_for_tenant(session, owner.id) == 5
        assert count_projects_for_tenant(session, other.id) == 1
        # 不传分页参数时行为不变：全部项目。
        assert [p.public_id for p in list_projects_for_tenant(session, owner.id)] == ids


@pytest.mark.parametrize(("offset", "limit"), [(-1, 20), (0, 0)])
def test_paging_arguments_are_checked(session_factory, offset: int, limit: int) -> None:
    with session_factory() as session:
        tenant = make_tenant(session)
        with pytest.raises(ValueError):
            list_tenants(session, offset=offset, limit=limit)
        with pytest.raises(ValueError):
            list_projects_for_tenant(session, tenant.id, offset=offset, limit=limit)


def test_repository_does_not_commit(session_factory) -> None:
    """只 flush 不 commit：调用方回滚，什么都不该留下。"""
    with session_factory() as session:
        tenant = make_tenant(session)
        project = create_project(session, tenant_id=tenant.id, name="Chatbot", now=NOW)
        tenant_id, tenant_public_id = tenant.id, tenant.public_id
        project_public_id = project.public_id
        session.rollback()

    with session_factory() as session:
        assert get_tenant_by_public_id(session, tenant_public_id) is None
        assert get_project_for_tenant(session, tenant_id, project_public_id) is None
        assert list_projects_for_tenant(session, tenant_id) == []

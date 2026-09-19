"""Customer service transactions (design gate #96 v3 §3 INV-13, §5, §7).

⚠️ **每条用例在 SQLite 与真 MySQL 上各跑一次**（`factory` 夹具的两个参数）。MySQL
那一半需要 `BILLING_TEST_DATABASE_URL` 指向一个**可以被清空的**库，没设时 skip ——
**不要把 skipped 读成 passed**，CI 设了它并把 skipped 判成失败。钱包的触发器只有迁移
0006 会建，所以「钱包插入被真实触发器拒绝」那一条只在 MySQL 上跑。
"""

from __future__ import annotations

import datetime as dt
import json
import os
import uuid
from collections.abc import Iterator
from decimal import Decimal

import pytest
from alembic.config import Config
from sqlalchemy import Engine, create_engine, delete, func, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from alembic import command
from app.core.database import create_session_factory
from app.models.auth import AuditAction, AuditLog, User, UserRole, UserStatus
from app.models.base import Base
from app.models.tenancy import BillingStatus, Project, Tenant
from app.models.wallet import Wallet
from app.repositories import tenancy
from app.repositories.wallet import get_wallet_for_tenant, verify_wallet
from app.services import customers
from app.services.auth import RequestContext, record_audit
from app.services.customers import CustomerNotFound

TEST_DATABASE_URL = os.environ.get("BILLING_TEST_DATABASE_URL", "")
TEST_EMAIL_DOMAIN = "@customer-service-test.example.com"

# 固定值而不是 utc_now()：断言「存进去的就是调用方给的那个时刻」要能逐字比对。
NOW = dt.datetime(2026, 9, 20, 8, 30, 0)
CONTEXT = RequestContext(ip_address="203.0.113.7", user_agent="customer-service-test")

# 有辨识度的个人数据：断言它们不进审计。
EMAIL = "pii-owner@example.com"
CONTACT = "Contact Person Probe"
PHONE = "+60 3-7788 9911"

# MySQL：SIGNAL SQLSTATE '45000' 报 1644（见 test_wallet_repository.py）。
SIGNALLED = 1644


# --- 夹具 -----------------------------------------------------------------------


@pytest.fixture(params=["sqlite", "mysql"])
def factory(request, monkeypatch) -> Iterator[sessionmaker[Session]]:
    if request.param == "mysql":
        yield from _mysql_factory(monkeypatch)
        return
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    yield create_session_factory(engine)
    engine.dispose()


@pytest.fixture
def mysql_factory(monkeypatch) -> Iterator[sessionmaker[Session]]:
    yield from _mysql_factory(monkeypatch)


def _mysql_factory(monkeypatch) -> Iterator[sessionmaker[Session]]:
    if not TEST_DATABASE_URL:
        pytest.skip("BILLING_TEST_DATABASE_URL is not set; the MySQL half needs a real MySQL")
    # ⚠️ 建表必须走 alembic：钱包的触发器只有迁移会建（同 test_wallet_repository.py）。
    from app.core.config import get_settings

    monkeypatch.setenv("BILLING_DATABASE_URL", TEST_DATABASE_URL)
    get_settings.cache_clear()
    command.upgrade(Config("alembic.ini"), "head")
    get_settings.cache_clear()

    engine = create_engine(TEST_DATABASE_URL)
    _clean(engine)
    try:
        yield create_session_factory(engine)
    finally:
        _clean(engine)
        engine.dispose()


def _clean(engine: Engine) -> None:
    """⚠️ 库是共享的，每个用例前后都清场（与 test_wallet_repository.py 同一做法）。"""
    with engine.begin() as connection:
        # 账本拒绝 DELETE，只能 TRUNCATE；有账本行的钱包删不掉。
        connection.execute(text("TRUNCATE TABLE wallet_transactions"))
    actions = [AuditAction.CUSTOMER_CREATE, AuditAction.PROJECT_CREATE]
    with engine.begin() as connection:
        connection.execute(delete(Wallet))
        connection.execute(delete(Project))
        connection.execute(delete(Tenant))
        connection.execute(delete(AuditLog).where(AuditLog.action.in_(actions)))
        connection.execute(delete(User).where(User.email.like(f"%{TEST_EMAIL_DOMAIN}")))


# --- 帮手 -----------------------------------------------------------------------


def make_admin(factory) -> User:
    """A committed ADMIN, detached with its columns loaded — what `require_admin` returns."""
    with factory() as session:
        user = User(
            email=f"{uuid.uuid4().hex}{TEST_EMAIL_DOMAIN}",
            password_hash="not-a-real-hash",
            role=UserRole.ADMIN,
            status=UserStatus.ACTIVE,
            created_at=NOW,
            updated_at=NOW,
        )
        session.add(user)
        session.commit()
        return user


def create(factory, admin: User, company_name: str = "Acme Sdn Bhd"):
    return customers.create_customer(
        factory,
        actor=admin,
        company_name=company_name,
        email=EMAIL,
        contact_name=CONTACT,
        phone=PHONE,
        context=CONTEXT,
        now=NOW,
    )


def add_project(factory, admin: User, customer_id: str, name: str = "Chatbot"):
    return customers.create_project(
        factory,
        actor=admin,
        customer_id=customer_id,
        name=name,
        context=CONTEXT,
        now=NOW,
    )


def counts(factory) -> dict[str, int]:
    """Rows this service writes. Audits: only its two actions, the MySQL db is shared."""
    ours = AuditLog.action.in_([AuditAction.CUSTOMER_CREATE, AuditAction.PROJECT_CREATE])
    statements = {
        "tenants": select(func.count()).select_from(Tenant),
        "wallets": select(func.count()).select_from(Wallet),
        "projects": select(func.count()).select_from(Project),
        "audits": select(func.count()).select_from(AuditLog).where(ours),
    }
    with factory() as session:
        return {name: session.execute(query).scalar_one() for name, query in statements.items()}


def audits(factory, action: AuditAction) -> list[AuditLog]:
    statement = select(AuditLog).where(AuditLog.action == action).order_by(AuditLog.id)
    with factory() as session:
        return list(session.execute(statement).scalars())


NOTHING = {"tenants": 0, "wallets": 0, "projects": 0, "audits": 0}


def _boom(*_args: object, **_kwargs: object) -> None:
    raise RuntimeError("injected failure")


def _record_invalid_audit(session: Session, **options: object) -> None:
    """The real audit write with a NULL `created_at`: it fails only at commit (NOT NULL)."""
    record_audit(session, **{**options, "now": None})


# --- 建客户 ---------------------------------------------------------------------


def test_create_customer_commits_tenant_wallet_and_audit_together(factory) -> None:
    admin = make_admin(factory)

    detail = create(factory, admin)

    assert counts(factory) == {"tenants": 1, "wallets": 1, "projects": 0, "audits": 1}
    assert detail.billing_status == "SUSPENDED"
    assert detail.status_version == 0
    view = detail.wallet
    assert (view.currency, view.balance, view.version) == ("MYR", "0.00000000", 0)
    with factory() as session:
        tenant = tenancy.get_tenant_by_public_id(session, detail.id)
        assert tenant is not None
        assert tenant.billing_status is BillingStatus.SUSPENDED
        assert tenant.status_version == 0
        assert (tenant.email, tenant.contact_name, tenant.phone) == (EMAIL, CONTACT, PHONE)
        wallet = get_wallet_for_tenant(session, tenant.id)
        assert wallet is not None
        assert (wallet.currency, wallet.balance, wallet.version) == ("MYR", Decimal("0"), 0)
        # 客户、钱包、审计用同一个 now（设计 §2）。
        assert tenant.created_at == wallet.created_at == NOW
        # MySQL 上这里核对的是迁移 0006 的真实库：余额 0、版本 0、没有账本行。
        assert verify_wallet(session, tenant.id) == []

    [audit] = audits(factory, AuditAction.CUSTOMER_CREATE)
    assert (audit.actor_user_id, audit.actor_role) == (admin.id, "ADMIN")
    assert (audit.entity_type, audit.entity_id) == ("tenant", detail.id)
    assert (audit.ip_address, audit.user_agent) == (CONTEXT.ip_address, CONTEXT.user_agent)
    assert audit.created_at == NOW
    assert json.loads(audit.after_state or "{}") == {
        "public_id": detail.id,
        "company_name": "Acme Sdn Bhd",
        "billing_status": "SUSPENDED",
        "wallet_currency": "MYR",
    }
    # 个人数据不进长期保留的审计表（设计 §6）。
    for personal in (EMAIL, CONTACT, PHONE):
        assert personal not in (audit.after_state or "")
        assert personal not in (audit.before_state or "")


@pytest.mark.parametrize("failing_step", ["create_wallet", "record_audit"])
def test_a_failed_step_leaves_no_customer_wallet_or_audit(
    factory, monkeypatch, failing_step: str
) -> None:
    """INV-13：钱包或审计任何一步失败，整个事务回滚。"""
    admin = make_admin(factory)
    monkeypatch.setattr(customers, failing_step, _boom)

    with pytest.raises(RuntimeError, match="injected failure"):
        create(factory, admin)

    assert counts(factory) == NOTHING


def test_a_failed_commit_leaves_no_customer_wallet_or_audit(factory, monkeypatch) -> None:
    """提交那一刻才失败（审计行违反 NOT NULL）：数据库保证不留半截。"""
    admin = make_admin(factory)
    monkeypatch.setattr(customers, "record_audit", _record_invalid_audit)

    with pytest.raises(IntegrityError):
        create(factory, admin)

    assert counts(factory) == NOTHING


def test_the_real_trigger_refusing_the_wallet_rolls_back_the_customer(
    mysql_factory, monkeypatch
) -> None:
    """MySQL 专有：让钱包插入带上非零余额，迁移 0006 的 BEFORE INSERT 触发器拒绝它。"""
    admin = make_admin(mysql_factory)

    def wallet_with_a_balance(session: Session, *, tenant_id: int, now: dt.datetime) -> Wallet:
        wallet = Wallet(
            tenant_id=tenant_id,
            currency="MYR",
            balance=Decimal("1"),
            version=0,
            created_at=now,
            updated_at=now,
        )
        session.add(wallet)
        session.flush()
        return wallet

    monkeypatch.setattr(customers, "create_wallet", wallet_with_a_balance)

    with pytest.raises(DBAPIError) as raised:
        create(mysql_factory, admin)

    assert int(raised.value.orig.args[0]) == SIGNALLED
    assert counts(mysql_factory) == NOTHING


# --- 建项目 ---------------------------------------------------------------------


def test_create_project_commits_the_project_and_its_audit(factory) -> None:
    admin = make_admin(factory)
    customer = create(factory, admin)

    project = add_project(factory, admin, customer.id)

    assert counts(factory) == {"tenants": 1, "wallets": 1, "projects": 1, "audits": 2}
    with factory() as session:
        tenant = tenancy.get_tenant_by_public_id(session, customer.id)
        assert tenant is not None
        stored = tenancy.get_project_for_tenant(session, tenant.id, project.id)
        assert stored is not None
        assert (stored.name, stored.description) == ("Chatbot", None)

    [audit] = audits(factory, AuditAction.PROJECT_CREATE)
    assert (audit.actor_user_id, audit.actor_role) == (admin.id, "ADMIN")
    assert (audit.entity_type, audit.entity_id) == ("project", project.id)
    assert json.loads(audit.after_state or "{}") == {
        "public_id": project.id,
        "name": "Chatbot",
        "tenant_public_id": customer.id,
    }


def test_a_failed_project_audit_leaves_no_project(factory, monkeypatch) -> None:
    """设计审查 v3 的建议：建项目时写入失败，项目与 PROJECT_CREATE 审计都不留下。"""
    admin = make_admin(factory)
    customer = create(factory, admin)
    before = counts(factory)
    monkeypatch.setattr(customers, "record_audit", _boom)

    with pytest.raises(RuntimeError, match="injected failure"):
        add_project(factory, admin, customer.id)

    assert counts(factory) == before
    assert audits(factory, AuditAction.PROJECT_CREATE) == []


def test_a_failed_project_commit_leaves_no_project(factory, monkeypatch) -> None:
    admin = make_admin(factory)
    customer = create(factory, admin)
    before = counts(factory)
    monkeypatch.setattr(customers, "record_audit", _record_invalid_audit)

    with pytest.raises(IntegrityError):
        add_project(factory, admin, customer.id)

    assert counts(factory) == before
    assert audits(factory, AuditAction.PROJECT_CREATE) == []


def test_a_project_for_an_unknown_customer_is_not_found_and_writes_nothing(factory) -> None:
    """设计审查 v3 的建议：对不存在的客户建项目是 404，不写库。"""
    admin = make_admin(factory)
    create(factory, admin)
    before = counts(factory)

    with pytest.raises(CustomerNotFound) as raised:
        add_project(factory, admin, str(uuid.uuid4()))

    assert (raised.value.code, raised.value.http_status) == ("CUSTOMER_NOT_FOUND", 404)
    assert counts(factory) == before


# --- 读 -------------------------------------------------------------------------


def test_reads_write_nothing(factory) -> None:
    """读接口不写审计（设计 §2）。"""
    admin = make_admin(factory)
    customer = create(factory, admin)
    add_project(factory, admin, customer.id)
    before = counts(factory)

    assert customers.get_customer(factory, customer.id) == customer
    listing = customers.list_customers(factory, page=1, page_size=20)
    assert [item.id for item in listing.items] == [customer.id]
    assert listing.total == 1
    projects = customers.list_projects(factory, customer_id=customer.id, page=1, page_size=20)
    assert projects.total == 1

    assert counts(factory) == before

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
import threading
import time
import uuid
from collections.abc import Iterator
from decimal import Decimal

import pytest
from alembic.config import Config
from sqlalchemy import Engine, create_engine, delete, event, func, select, text, update
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from alembic import command
from app.core.database import create_session_factory
from app.models.auth import AuditAction, AuditLog, User, UserRole, UserStatus
from app.models.base import Base
from app.models.integration import ProjectStatusDelivery
from app.models.tenancy import AccountStatus, BillingStatus, IntegrationStatus, Project, Tenant
from app.models.wallet import Wallet
from app.repositories import tenancy
from app.repositories.wallet import get_wallet_for_tenant, verify_wallet
from app.services import customers
from app.services.auth import RequestContext, record_audit
from app.services.customers import CustomerNotFound
from app.services.project_integration import AccountClosed

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

# 这个服务写的审计动作。MySQL 库是共享的，清场与计数都只碰这几种。
OUR_ACTIONS = [
    AuditAction.CUSTOMER_CREATE,
    AuditAction.PROJECT_CREATE,
    AuditAction.CUSTOMER_UPDATE,
]


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
        # 0018 起 usage_events 有外键引用账本：被引用的表要关掉外键检查才能 TRUNCATE。
        connection.execute(text("SET FOREIGN_KEY_CHECKS = 0"))
        try:
            connection.execute(text("TRUNCATE TABLE wallet_transactions"))
        finally:
            connection.execute(text("SET FOREIGN_KEY_CHECKS = 1"))
        # 审计表同理：0010 的触发器拒绝 DELETE，TRUNCATE 是 DDL、不经触发器。
        connection.execute(text("TRUNCATE TABLE audit_logs"))
    with engine.begin() as connection:
        connection.execute(delete(Wallet))
        # 投递行引用项目（RESTRICT），先删它（迁移 0020）。
        connection.execute(delete(ProjectStatusDelivery))
        connection.execute(delete(Project))
        connection.execute(delete(Tenant))
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
    """Rows this service writes. Audits: only its own actions, the MySQL db is shared."""
    ours = AuditLog.action.in_(OUR_ACTIONS)
    statements = {
        "tenants": select(func.count()).select_from(Tenant),
        "wallets": select(func.count()).select_from(Wallet),
        "projects": select(func.count()).select_from(Project),
        "deliveries": select(func.count()).select_from(ProjectStatusDelivery),
        "audits": select(func.count()).select_from(AuditLog).where(ours),
    }
    with factory() as session:
        return {name: session.execute(query).scalar_one() for name, query in statements.items()}


def deliveries(factory) -> list[ProjectStatusDelivery]:
    statement = select(ProjectStatusDelivery).order_by(ProjectStatusDelivery.project_id)
    with factory() as session:
        return list(session.execute(statement).scalars())


def audits(factory, action: AuditAction) -> list[AuditLog]:
    statement = select(AuditLog).where(AuditLog.action == action).order_by(AuditLog.id)
    with factory() as session:
        return list(session.execute(statement).scalars())


NOTHING = {"tenants": 0, "wallets": 0, "projects": 0, "deliveries": 0, "audits": 0}


def _boom(*_args: object, **_kwargs: object) -> None:
    raise RuntimeError("injected failure")


def _record_invalid_audit(session: Session, **options: object) -> None:
    """The real audit write with a NULL `created_at`: it fails only at commit (NOT NULL)."""
    record_audit(session, **{**options, "now": None})


# --- 建客户 ---------------------------------------------------------------------


def test_create_customer_commits_tenant_wallet_and_audit_together(factory) -> None:
    admin = make_admin(factory)

    detail = create(factory, admin)

    assert counts(factory) == {**NOTHING, "tenants": 1, "wallets": 1, "audits": 1}
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

    assert counts(factory) == {
        "tenants": 1,
        "wallets": 1,
        "projects": 1,
        "deliveries": 1,
        "audits": 2,
    }
    with factory() as session:
        tenant = tenancy.get_tenant_by_public_id(session, customer.id)
        assert tenant is not None
        stored = tenancy.get_project_for_tenant(session, tenant.id, project.id)
        assert stored is not None
        assert (stored.name, stored.description) == ("Chatbot", None)
        # 迁移 0020 的两列：新项目 ENABLED、没有地址（设计闸门 #241 §2）。
        assert (stored.integration_status, stored.status_webhook_url) == (
            IntegrationStatus.ENABLED,
            None,
        )
        owner = (stored.id, tenant.id)

    # 同一事务插入的投递行：从未送达、没有失败、`next_attempt_at` 就是建项目的 now。
    [delivery] = deliveries(factory)
    assert (delivery.project_id, delivery.tenant_id) == owner
    assert (delivery.delivered_version, delivery.attempted_version) == (None, None)
    assert delivery.attempt_count == 0
    assert delivery.next_attempt_at == NOW
    assert (delivery.lease_token, delivery.lease_expires_at) == (None, None)
    assert (delivery.last_attempt_at, delivery.delivered_at) == (None, None)
    assert (delivery.last_http_status, delivery.last_error_code) == (None, None)

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


def test_a_failed_delivery_row_leaves_no_project(factory, monkeypatch) -> None:
    """设计闸门 #241 §7：项目与投递行同一事务，投递行写不进去，项目与审计也不留。"""
    admin = make_admin(factory)
    customer = create(factory, admin)
    before = counts(factory)
    monkeypatch.setattr(customers, "add_status_delivery", _boom)

    with pytest.raises(RuntimeError, match="injected failure"):
        add_project(factory, admin, customer.id)

    assert counts(factory) == before
    assert deliveries(factory) == []
    assert audits(factory, AuditAction.PROJECT_CREATE) == []


def test_a_delivery_row_the_database_refuses_leaves_no_project(factory, monkeypatch) -> None:
    """The real row with a negative attempt count: the CHECK of migration 0020 / the model
    refuses it at flush, and the project goes with it."""
    admin = make_admin(factory)
    customer = create(factory, admin)
    before = counts(factory)
    real = customers.add_status_delivery

    def refused_delivery(
        session: Session, project: Project, *, now: dt.datetime
    ) -> ProjectStatusDelivery:
        delivery = real(session, project, now=now)
        delivery.attempt_count = -1
        session.flush()
        return delivery

    monkeypatch.setattr(customers, "add_status_delivery", refused_delivery)

    with pytest.raises(DBAPIError):
        add_project(factory, admin, customer.id)

    assert counts(factory) == before
    assert deliveries(factory) == []


def test_every_project_gets_exactly_one_delivery_row(factory) -> None:
    """设计闸门 #241 §7「每个项目恰好一行」：每建一个项目多一行，归属它自己的项目与租户。"""
    admin = make_admin(factory)
    first = create(factory, admin, company_name="First Sdn Bhd")
    second = create(factory, admin, company_name="Second Sdn Bhd")
    add_project(factory, admin, first.id, name="One")
    add_project(factory, admin, first.id, name="Two")
    add_project(factory, admin, second.id, name="Three")

    with factory() as session:
        owners = session.execute(select(Project.id, Project.tenant_id).order_by(Project.id)).all()

    assert len(owners) == 3
    rows = [(row.project_id, row.tenant_id) for row in deliveries(factory)]
    assert rows == [(row.id, row.tenant_id) for row in owners]


def test_migration_0020_then_a_new_project_leaves_exactly_one_row_each(mysql_factory) -> None:
    """设计闸门 #241 §7：迁移 0020 升级已有两个项目的库，之后经服务新建一个项目 —— 三个项目
    各有一行投递行。MySQL 专有：回填是迁移里的 `INSERT … SELECT`。"""
    from app.core.config import get_settings

    config = Config("alembic.ini")
    get_settings.cache_clear()
    command.downgrade(config, "0019_internal_metering")
    try:
        with mysql_factory() as session:
            # 0019 的表形状：模型已经多了新列，这里只能写裸 SQL。
            for n in (1, 2):
                tenant_public_id = str(uuid.uuid4())
                session.execute(
                    text(
                        "INSERT INTO tenants (public_id, company_name, email, created_at,"
                        " updated_at) VALUES (:public_id, :name, :email, :now, :now)"
                    ),
                    {
                        "public_id": tenant_public_id,
                        "name": f"Existing {n} Sdn Bhd",
                        "email": EMAIL,
                        "now": NOW,
                    },
                )
                session.execute(
                    text(
                        "INSERT INTO projects (public_id, tenant_id, name, created_at, updated_at)"
                        " SELECT :public_id, id, 'Existing', :now, :now"
                        " FROM tenants WHERE public_id = :tenant"
                    ),
                    {"public_id": str(uuid.uuid4()), "tenant": tenant_public_id, "now": NOW},
                )
            session.commit()

        command.upgrade(config, "head")
        get_settings.cache_clear()
        admin = make_admin(mysql_factory)
        customer = create(mysql_factory, admin)
        created = add_project(mysql_factory, admin, customer.id)
    finally:
        command.upgrade(config, "head")
        get_settings.cache_clear()

    with mysql_factory() as session:
        owners = session.execute(
            select(Project.id, Project.tenant_id, Project.public_id).order_by(Project.id)
        ).all()
    rows = deliveries(mysql_factory)

    assert len(owners) == 3
    expected = [(row.id, row.tenant_id) for row in owners]
    assert [(row.project_id, row.tenant_id) for row in rows] == expected
    for row in rows:
        assert (row.delivered_version, row.attempt_count, row.lease_token) == (None, 0, None)
    # 新项目的行由建项目写入，取建项目的 now；回填的两行取迁移时刻（迁移测试里核对）。
    new_id = next(row.id for row in owners if row.public_id == created.id)
    assert [row.next_attempt_at for row in rows if row.project_id == new_id] == [NOW]


def test_a_project_for_an_unknown_customer_is_not_found_and_writes_nothing(factory) -> None:
    """设计审查 v3 的建议：对不存在的客户建项目是 404，不写库。"""
    admin = make_admin(factory)
    create(factory, admin)
    before = counts(factory)

    with pytest.raises(CustomerNotFound) as raised:
        add_project(factory, admin, str(uuid.uuid4()))

    assert (raised.value.code, raised.value.http_status) == ("CUSTOMER_NOT_FOUND", 404)
    assert counts(factory) == before


# --- 关户后建项目（AIH-TASK-210，设计闸门 #707 v4 §2「契约二」） ------------------


def force_status(factory, customer_id: str, value: AccountStatus) -> None:
    """造数据：直接改库（关户接口归 AIH-TASK-213；写法同 test_account_status_api.py）。"""
    with factory() as session:
        session.execute(
            update(Tenant).where(Tenant.public_id == customer_id).values(account_status=value)
        )
        session.commit()


def every_audit(factory) -> int:
    """All audit rows, whatever the action: a refused write leaves none at all."""
    with factory() as session:
        return session.execute(select(func.count()).select_from(AuditLog)).scalar_one()


def test_a_project_for_a_closed_customer_is_409_and_writes_nothing(factory) -> None:
    """§7「管理写入口」：CLOSED 客户建项目 409 ACCOUNT_CLOSED；项目、投递行、审计都不写。"""
    admin = make_admin(factory)
    customer = create(factory, admin)
    force_status(factory, customer.id, AccountStatus.CLOSED)
    before, audits_before = counts(factory), every_audit(factory)

    with pytest.raises(AccountClosed) as raised:
        add_project(factory, admin, customer.id)

    error = raised.value
    assert (error.code, error.http_status, error.message) == (
        "ACCOUNT_CLOSED",
        409,
        "The customer account is closed.",
    )
    assert counts(factory) == before
    assert every_audit(factory) == audits_before
    assert deliveries(factory) == []


def test_a_project_for_a_disabled_customer_is_created(factory) -> None:
    """§7「管理写入口」：DISABLED 不是 CLOSED，建项目照常。"""
    admin = make_admin(factory)
    customer = create(factory, admin)
    force_status(factory, customer.id, AccountStatus.DISABLED)

    project = add_project(factory, admin, customer.id)

    assert counts(factory)["projects"] == 1
    assert [row.entity_id for row in audits(factory, AuditAction.PROJECT_CREATE)] == [project.id]


def test_an_unknown_customer_is_404_even_beside_a_closed_one(factory) -> None:
    """§7「入口判定顺序」：404 先于 409；不存在的客户不会因为库里有 CLOSED 客户而变成 409。"""
    admin = make_admin(factory)
    customer = create(factory, admin)
    force_status(factory, customer.id, AccountStatus.CLOSED)
    before = counts(factory)

    with pytest.raises(CustomerNotFound):
        add_project(factory, admin, str(uuid.uuid4()))

    assert counts(factory) == before


def test_editing_a_closed_customer_still_works(factory) -> None:
    """§2「契约二」不改的写入口：编辑客户资料对 CLOSED 照常，写一条 CUSTOMER_UPDATE。"""
    admin = make_admin(factory)
    customer = create(factory, admin, company_name="Old Name")
    force_status(factory, customer.id, AccountStatus.CLOSED)

    detail = edit(factory, admin, customer.id, company_name="New Name")

    assert detail.company_name == "New Name"
    assert len(audits(factory, AuditAction.CUSTOMER_UPDATE)) == 1


# 入口与关户并发（只在真 MySQL 上；SQLite 忽略 `FOR UPDATE`）。
# 「B 确实在等租户行锁」查 performance_schema.data_lock_waits，写法照
# test_session_revocation_concurrency.py 的 `_WAITING_ON_TABLE`；不查 INNODB_TRX（缓存不准）。
_WAITING_ON_TABLE = text(
    "SELECT COUNT(*) FROM performance_schema.data_lock_waits AS w"
    " JOIN performance_schema.threads AS t ON t.THREAD_ID = w.REQUESTING_THREAD_ID"
    " JOIN performance_schema.data_locks AS l"
    " ON l.ENGINE = w.ENGINE AND l.ENGINE_LOCK_ID = w.REQUESTING_ENGINE_LOCK_ID"
    " WHERE t.PROCESSLIST_ID = :connection_id"
    " AND l.OBJECT_SCHEMA = DATABASE() AND l.OBJECT_NAME = :table_name"
)
_LOCK_REPORT = text(
    "SELECT THREAD_ID, OBJECT_SCHEMA, OBJECT_NAME, INDEX_NAME, LOCK_TYPE, LOCK_MODE,"
    " LOCK_STATUS, LOCK_DATA FROM performance_schema.data_locks"
)
# 「对方在等行锁」的轮询：间隔 50 毫秒、最多 10 秒。
LOCK_POLL_INTERVAL_SECONDS = 0.05
LOCK_POLL_LIMIT_SECONDS = 10.0
# B 的连接把 innodb_lock_wait_timeout 调到这个值：真卡住时以 1205 收场，不等默认的 50 秒。
LOCK_WAIT_TIMEOUT_SECONDS = 20
JOIN_TIMEOUT_SECONDS = 40.0


def test_create_project_waits_for_closure_and_is_refused(mysql_factory) -> None:
    """§7「入口与关户并发」（MySQL）。

    连接 A 锁住租户行（`FOR UPDATE`，模拟关户事务）并改为 CLOSED、暂不提交；连接 B 建项目。
    经 data_lock_waits 确认 B 在等租户行后 A 提交：B 得到 409 ACCOUNT_CLOSED，没有项目行、
    投递行与 PROJECT_CREATE 审计。
    """
    admin = make_admin(mysql_factory)
    customer = create(mysql_factory, admin)
    before, audits_before = counts(mysql_factory), every_audit(mysql_factory)

    # B 用自己的引擎、恰好一条连接：它的 CONNECTION_ID() 就是要观察的那一个。
    connection_ids: list[int] = []
    engine_b = create_engine(TEST_DATABASE_URL, pool_size=1, max_overflow=0)

    def prepare(dbapi_connection, _record) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("SELECT CONNECTION_ID()")
            connection_ids.append(int(cursor.fetchone()[0]))
            cursor.execute(f"SET SESSION innodb_lock_wait_timeout = {LOCK_WAIT_TIMEOUT_SECONDS}")
        finally:
            cursor.close()
        dbapi_connection.rollback()

    event.listen(engine_b, "connect", prepare)
    factory_b = create_session_factory(engine_b)
    observer = create_engine(TEST_DATABASE_URL, isolation_level="AUTOCOMMIT", pool_size=1)
    outcome: dict[str, BaseException | object] = {}
    finished = threading.Event()

    def run_b() -> None:
        try:
            outcome["result"] = add_project(factory_b, admin, customer.id)
        except BaseException as exc:
            outcome["error"] = exc
        finally:
            finished.set()

    thread = threading.Thread(target=run_b, name="create project", daemon=True)
    try:
        with mysql_factory() as closer:
            tenant = closer.execute(
                select(Tenant).where(Tenant.public_id == customer.id).with_for_update()
            ).scalar_one()
            tenant.account_status = AccountStatus.CLOSED
            closer.flush()

            thread.start()
            _wait_until_waiting_for_tenants(observer, connection_ids, finished)
            closer.commit()

        thread.join(JOIN_TIMEOUT_SECONDS)
        assert not thread.is_alive(), "create project did not finish"
    finally:
        thread.join(5)
        engine_b.dispose()
        observer.dispose()

    error = outcome.get("error")
    assert isinstance(error, AccountClosed), outcome
    assert (error.code, error.http_status) == ("ACCOUNT_CLOSED", 409)
    assert counts(mysql_factory) == before
    assert every_audit(mysql_factory) == audits_before
    assert deliveries(mysql_factory) == []
    assert audits(mysql_factory, AuditAction.PROJECT_CREATE) == []


def _wait_until_waiting_for_tenants(
    observer: Engine, connection_ids: list[int], finished: threading.Event
) -> None:
    """Poll until B's connection waits for a row lock on `tenants`; fail if B ends first."""
    deadline = time.monotonic() + LOCK_POLL_LIMIT_SECONDS
    with observer.connect() as connection:
        while time.monotonic() < deadline:
            if connection_ids:
                waiting = connection.execute(
                    _WAITING_ON_TABLE,
                    {"connection_id": connection_ids[-1], "table_name": "tenants"},
                ).scalar_one()
                if waiting:
                    return
            # B 结束了就不可能还在等锁：立即失败，不等到超时。
            if finished.wait(LOCK_POLL_INTERVAL_SECONDS):
                pytest.fail("create project finished before it was seen waiting for the tenant")
        report = [tuple(row) for row in connection.execute(_LOCK_REPORT).all()]
    pytest.fail(
        f"create project (connections {connection_ids}) was not seen waiting for a tenants"
        f" row lock within {LOCK_POLL_LIMIT_SECONDS}s\ndata_locks: {report}"
    )


# --- 编辑客户（AIH-TASK-009） ---------------------------------------------------

LATER = NOW + dt.timedelta(hours=1)


def edit(factory, admin: User, customer_id: str, **changes: str | None):
    return customers.update_customer(
        factory,
        actor=admin,
        customer_id=customer_id,
        changes=changes,
        context=CONTEXT,
        now=LATER,
    )


def stored(factory, customer_id: str) -> dict[str, object]:
    """Every column an edit could touch, plus the ones it must never touch."""
    with factory() as session:
        tenant = tenancy.get_tenant_by_public_id(session, customer_id)
        assert tenant is not None
        wallet = get_wallet_for_tenant(session, tenant.id)
        assert wallet is not None
        return {
            "company_name": tenant.company_name,
            "contact_name": tenant.contact_name,
            "email": tenant.email,
            "phone": tenant.phone,
            "billing_status": tenant.billing_status,
            "status_version": tenant.status_version,
            "low_balance_threshold": tenant.low_balance_threshold,
            "updated_at": tenant.updated_at,
            "wallet": (wallet.balance, wallet.version, wallet.updated_at),
        }


def test_update_customer_commits_the_row_and_its_audit(factory) -> None:
    admin = make_admin(factory)
    customer = create(factory, admin, company_name="Old Name")
    original = stored(factory, customer.id)
    before = counts(factory)

    detail = edit(
        factory, admin, customer.id, company_name="New Name", email="new-" + EMAIL, phone=None
    )

    assert (detail.company_name, detail.email, detail.contact_name, detail.phone) == (
        "New Name",
        "new-" + EMAIL,
        CONTACT,
        None,
    )
    assert detail.updated_at == LATER
    assert counts(factory) == {**before, "audits": before["audits"] + 1}
    assert stored(factory, customer.id) == {
        **original,
        "company_name": "New Name",
        "email": "new-" + EMAIL,
        "phone": None,
        "updated_at": LATER,
    }

    [audit] = audits(factory, AuditAction.CUSTOMER_UPDATE)
    assert (audit.actor_user_id, audit.actor_role) == (admin.id, "ADMIN")
    assert (audit.entity_type, audit.entity_id) == ("tenant", customer.id)
    assert (audit.ip_address, audit.user_agent) == (CONTEXT.ip_address, CONTEXT.user_agent)
    assert audit.created_at == LATER
    assert json.loads(audit.before_state or "{}") == {
        "public_id": customer.id,
        "company_name": "Old Name",
    }
    assert json.loads(audit.after_state or "{}") == {
        "public_id": customer.id,
        "company_name": "New Name",
        "changed_fields": ["company_name", "email", "phone"],
    }
    # 个人数据的旧值、新值都不进审计。
    for personal in (EMAIL, "new-" + EMAIL, CONTACT, PHONE):
        assert personal not in (audit.before_state or "")
        assert personal not in (audit.after_state or "")


def test_a_failed_update_audit_leaves_the_customer_unchanged(factory, monkeypatch) -> None:
    """INV-13：审计写入失败，客户行回滚到改之前。"""
    admin = make_admin(factory)
    customer = create(factory, admin)
    original, before = stored(factory, customer.id), counts(factory)
    monkeypatch.setattr(customers, "record_audit", _boom)

    with pytest.raises(RuntimeError, match="injected failure"):
        edit(factory, admin, customer.id, company_name="Renamed", phone=None)

    assert stored(factory, customer.id) == original
    assert counts(factory) == before
    assert audits(factory, AuditAction.CUSTOMER_UPDATE) == []


def test_a_failed_update_commit_leaves_the_customer_unchanged(factory, monkeypatch) -> None:
    """提交那一刻才失败（审计行违反 NOT NULL）：客户行的 UPDATE 已经 flush，也一起回滚。"""
    admin = make_admin(factory)
    customer = create(factory, admin)
    original, before = stored(factory, customer.id), counts(factory)
    monkeypatch.setattr(customers, "record_audit", _record_invalid_audit)

    with pytest.raises(IntegrityError):
        edit(factory, admin, customer.id, company_name="Renamed", email="new-" + EMAIL)

    assert stored(factory, customer.id) == original
    assert counts(factory) == before
    assert audits(factory, AuditAction.CUSTOMER_UPDATE) == []


def test_an_update_that_changes_nothing_writes_nothing(factory) -> None:
    admin = make_admin(factory)
    customer = create(factory, admin)
    original, before = stored(factory, customer.id), counts(factory)

    detail = edit(factory, admin, customer.id, company_name="Acme Sdn Bhd", phone=PHONE)

    assert detail == customer
    assert stored(factory, customer.id) == original
    assert counts(factory) == before


def test_an_update_for_an_unknown_customer_is_not_found_and_writes_nothing(factory) -> None:
    admin = make_admin(factory)
    customer = create(factory, admin)
    original, before = stored(factory, customer.id), counts(factory)

    with pytest.raises(CustomerNotFound):
        edit(factory, admin, str(uuid.uuid4()), company_name="Renamed")

    assert stored(factory, customer.id) == original
    assert counts(factory) == before


@pytest.mark.parametrize(
    "column", ["billing_status", "status_version", "public_id", "low_balance_threshold"]
)
def test_the_repository_refuses_non_profile_columns(factory, column: str) -> None:
    """请求体之外的第二道防线：计费状态等列不能经资料更新改动。"""
    admin = make_admin(factory)
    customer = create(factory, admin)
    original, before = stored(factory, customer.id), counts(factory)

    with pytest.raises(ValueError, match=column):
        edit(factory, admin, customer.id, **{column: "ACTIVE"})

    assert stored(factory, customer.id) == original
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

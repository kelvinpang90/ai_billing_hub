"""Tenant account status service (design gate #136 v2 §3 INV-4/8/13/14, §4, §5, §7).

四类用例：

- **跃迁表**：`transition` 是纯函数，表里每一格都不连库测一次；`PENDING_ACTIVATION` 与
  `CLOSED` 出发的格再经服务层、用直接改库造的数据各测一次；
- **回滚**（审计写入失败、outbox 写入失败、提交失败）与**与计费状态独立**：`factory` 夹具的
  两个参数，在 SQLite 与真 MySQL 上各跑一次；
- **并发版本**：两个线程同时改账户状态、另一线程调账跨零，只在真 MySQL 上（SQLite 忽略
  `FOR UPDATE`，也没有推进钱包的触发器）；
- 其余（只锁租户行、不碰钱包、不写日志、outbox 为 PENDING）在 SQLite 上，锁语句在 MySQL 上。

MySQL 那部分需要 `BILLING_TEST_DATABASE_URL` 指向一个**可以被清空的**库，没设时 skip ——
**不要把 skipped 读成 passed**，CI 设了它并把 skipped 判成失败。
"""

from __future__ import annotations

import datetime as dt
import inspect
import json
import os
import threading
import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import pytest
from alembic.config import Config
from sqlalchemy import Engine, create_engine, delete, event, func, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from alembic import command
from app.core.database import create_session_factory
from app.models.auth import (
    AuditAction,
    AuditLog,
    DomainOutbox,
    OutboxStatus,
    User,
    UserRole,
    UserStatus,
)
from app.models.base import Base
from app.models.tenancy import AccountStatus, BillingStatus, Tenant
from app.models.wallet import TransactionType, Wallet
from app.repositories import tenancy
from app.repositories.wallet import create_wallet, get_wallet_for_tenant
from app.services import account_status
from app.services.account_status import (
    EVENT_ACCOUNT_STATUS_CHANGED,
    TRANSITIONS,
    AccountStatusTransitionInvalid,
    change_account_status,
    transition,
)
from app.services.auth import RequestContext
from app.services.customers import CustomerNotFound
from app.services.wallet_adjustments import post_adjustment

TEST_DATABASE_URL = os.environ.get("BILLING_TEST_DATABASE_URL", "")
TEST_EMAIL_DOMAIN = "@account-status-test.example.com"
COMPANY = "Account Status Test Sdn Bhd"

NOW = dt.datetime(2026, 9, 28, 8, 30, 0)
CONTEXT = RequestContext(ip_address="203.0.113.11", user_agent="account-status-service-test")
REASON = "Contract terminated pending settlement"

OUR_ACTIONS = [
    AuditAction.TENANT_ACCOUNT_STATUS_CHANGED,
    AuditAction.TENANT_BILLING_STATUS_CHANGED,
    AuditAction.WALLET_ADJUSTMENT_POSTED,
]

PENDING = AccountStatus.PENDING_ACTIVATION
ENABLED = AccountStatus.ENABLED
DISABLED = AccountStatus.DISABLED
CLOSED = AccountStatus.CLOSED


# --- 夹具 -----------------------------------------------------------------------


@pytest.fixture(params=["sqlite", "mysql"])
def factory(request, monkeypatch) -> Iterator[sessionmaker[Session]]:
    if request.param == "mysql":
        yield from _mysql_factory(monkeypatch)
        return
    yield from _sqlite_factory()


@pytest.fixture
def sqlite_factory() -> Iterator[sessionmaker[Session]]:
    yield from _sqlite_factory()


@pytest.fixture
def mysql_factory(monkeypatch) -> Iterator[sessionmaker[Session]]:
    yield from _mysql_factory(monkeypatch)


def _sqlite_factory() -> Iterator[sessionmaker[Session]]:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    yield create_session_factory(engine)
    engine.dispose()


def _mysql_factory(monkeypatch) -> Iterator[sessionmaker[Session]]:
    if not TEST_DATABASE_URL:
        pytest.skip("BILLING_TEST_DATABASE_URL is not set; the MySQL half needs a real MySQL")
    # 建表走 alembic：列、CHECK 以迁移 0009 建出来的为准，钱包触发器只有迁移会建。
    from app.core.config import get_settings

    monkeypatch.setenv("BILLING_DATABASE_URL", TEST_DATABASE_URL)
    get_settings.cache_clear()
    command.upgrade(Config("alembic.ini"), "head")
    get_settings.cache_clear()

    engine = create_engine(TEST_DATABASE_URL, pool_size=10, max_overflow=4)
    _clean(engine)
    try:
        yield create_session_factory(engine)
    finally:
        _clean(engine)
        engine.dispose()


def _clean(engine: Engine) -> None:
    """⚠️ 库是共享的，每个用例前后都清场（与 test_wallet_adjustment_service.py 同一做法）。"""
    ours = select(Tenant.id).where(Tenant.company_name == COMPANY)
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
        connection.execute(delete(Wallet).where(Wallet.tenant_id.in_(ours)))
        connection.execute(delete(Tenant).where(Tenant.company_name == COMPANY))
        connection.execute(delete(DomainOutbox).where(DomainOutbox.aggregate_type == "tenant"))
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


def make_customer(factory, status: AccountStatus | None = None) -> tuple[int, str]:
    """A tenant with an empty wallet; `status` overwrites the default directly in the db."""
    with factory() as session:
        tenant = tenancy.create_tenant(
            session, company_name=COMPANY, email="ops@example.com", now=NOW
        )
        create_wallet(session, tenant_id=tenant.id, now=NOW)
        session.commit()
        tenant_id, public_id = int(tenant.id), tenant.public_id
    if status is not None:
        with factory() as session:
            statement = update(Tenant).where(Tenant.id == tenant_id)
            session.execute(statement.values(account_status=status))
            session.commit()
    return tenant_id, public_id


def change(
    factory,
    admin: User,
    customer_id: str,
    target: AccountStatus,
    *,
    now: dt.datetime = NOW,
    reason: str = REASON,
):
    return change_account_status(
        factory,
        actor=admin,
        customer_id=customer_id,
        account_status=target,
        reason=reason,
        context=CONTEXT,
        now=now,
    )


def adjust(factory, admin: User, customer_id: str, amount: str) -> None:
    kind = TransactionType.ADJUSTMENT_CREDIT
    if Decimal(amount) < 0:
        kind = TransactionType.ADJUSTMENT_DEBIT
    post_adjustment(
        factory,
        actor=admin,
        customer_id=customer_id,
        transaction_type=kind,
        amount=Decimal(amount),
        reason="Settlement adjustment",
        idempotency_key=str(uuid.uuid4()),
        context=CONTEXT,
        now=NOW,
    )


def audits(factory, action: AuditAction, entity_id: str) -> list[AuditLog]:
    statement = (
        select(AuditLog)
        .where(AuditLog.action == action, AuditLog.entity_id == entity_id)
        .order_by(AuditLog.id)
    )
    with factory() as session:
        return list(session.execute(statement).scalars())


def events(factory, event_type: str, aggregate_id: str) -> list[DomainOutbox]:
    statement = (
        select(DomainOutbox)
        .where(DomainOutbox.event_type == event_type, DomainOutbox.aggregate_id == aggregate_id)
        .order_by(DomainOutbox.id)
    )
    with factory() as session:
        return list(session.execute(statement).scalars())


def snapshot(factory, tenant_id: int, public_id: str) -> dict[str, object]:
    """Everything a transition may write, and the wallet it must not touch, as committed."""
    with factory() as session:
        tenant = session.get(Tenant, tenant_id)
        wallet = get_wallet_for_tenant(session, tenant_id)
        assert tenant is not None and wallet is not None
        audit_count = select(func.count()).select_from(AuditLog)
        audit_count = audit_count.where(AuditLog.entity_id == public_id)
        outbox_count = select(func.count()).select_from(DomainOutbox)
        outbox_count = outbox_count.where(DomainOutbox.aggregate_id == public_id)
        return {
            "tenant": (
                tenant.account_status,
                tenant.billing_status,
                tenant.status_version,
                tenant.updated_at,
            ),
            "wallet": (wallet.balance, wallet.version),
            "audits": session.execute(audit_count).scalar_one(),
            "outbox": session.execute(outbox_count).scalar_one(),
        }


def break_the_audit(monkeypatch) -> None:
    """The audit row fails at flush: `created_at` is NULL (NOT NULL → IntegrityError)."""
    real = account_status.record_audit

    def broken(session: Session, **options: object) -> None:
        real(session, **{**options, "now": None})  # type: ignore[arg-type]

    monkeypatch.setattr(account_status, "record_audit", broken)


def break_the_outbox(monkeypatch) -> None:
    """The outbox row fails at flush: `created_at` is NULL (NOT NULL → IntegrityError)."""
    real = account_status._status_event

    def broken(tenant: Tenant, payload: dict[str, object], now: dt.datetime) -> DomainOutbox:
        row = real(tenant, payload, now)
        row.created_at = None  # type: ignore[assignment]
        return row

    monkeypatch.setattr(account_status, "_status_event", broken)


def break_the_commit(monkeypatch) -> None:
    def failing_commit(self: Session) -> None:
        raise RuntimeError("injected commit failure")

    monkeypatch.setattr(Session, "commit", failing_commit)


# --- 跃迁表：纯函数 --------------------------------------------------------------


@pytest.mark.parametrize(
    ("current", "target", "expected"),
    [
        (PENDING, ENABLED, ENABLED),
        (PENDING, DISABLED, DISABLED),
        (ENABLED, DISABLED, DISABLED),
        (DISABLED, ENABLED, ENABLED),
        # 目标等于当前：None，什么都不写。
        (PENDING, PENDING, None),
        (ENABLED, ENABLED, None),
        (DISABLED, DISABLED, None),
        (CLOSED, CLOSED, None),
    ],
)
def test_the_legal_cells(current, target, expected) -> None:
    assert transition(current, target) is expected


@pytest.mark.parametrize(
    ("current", "target"),
    [
        # CLOSED 是终态（设计 §4）。
        (CLOSED, ENABLED),
        (CLOSED, DISABLED),
        (CLOSED, PENDING),
        # 请求校验层就挡住的两个目标，表里同样没有格。
        (ENABLED, PENDING),
        (DISABLED, PENDING),
        (ENABLED, CLOSED),
        (DISABLED, CLOSED),
        (PENDING, CLOSED),
    ],
)
def test_the_illegal_cells(current, target) -> None:
    with pytest.raises(AccountStatusTransitionInvalid) as raised:
        transition(current, target)
    assert (raised.value.code, raised.value.http_status) == (
        "ACCOUNT_STATUS_TRANSITION_INVALID",
        409,
    )


def test_the_table_covers_every_status_and_never_reaches_closed() -> None:
    """本任务不产生 CLOSED，也不产生 PENDING_ACTIVATION（设计 §1「明确不做什么」）。"""
    assert set(TRANSITIONS) == set(AccountStatus)
    reachable = set().union(*TRANSITIONS.values())
    assert reachable == {ENABLED, DISABLED}
    assert TRANSITIONS[CLOSED] == frozenset()


# --- 跃迁表：经服务层，直接改库造数据 -----------------------------------------------


@pytest.mark.parametrize("target", [ENABLED, DISABLED])
def test_pending_activation_goes_either_way(sqlite_factory, target) -> None:
    admin = make_admin(sqlite_factory)
    _tenant_id, public_id = make_customer(sqlite_factory, PENDING)

    detail = change(sqlite_factory, admin, public_id, target)

    assert (detail.account_status, detail.status_version) == (target.value, 1)
    [audit] = audits(sqlite_factory, AuditAction.TENANT_ACCOUNT_STATUS_CHANGED, public_id)
    assert json.loads(audit.before_state or "{}") == {
        "account_status": "PENDING_ACTIVATION",
        "status_version": 0,
    }


@pytest.mark.parametrize("target", [ENABLED, DISABLED])
def test_closed_refuses_and_writes_nothing(sqlite_factory, target) -> None:
    admin = make_admin(sqlite_factory)
    tenant_id, public_id = make_customer(sqlite_factory, CLOSED)
    before = snapshot(sqlite_factory, tenant_id, public_id)

    with pytest.raises(AccountStatusTransitionInvalid):
        change(sqlite_factory, admin, public_id, target)

    assert snapshot(sqlite_factory, tenant_id, public_id) == before


def test_an_unknown_customer_writes_nothing(sqlite_factory) -> None:
    admin = make_admin(sqlite_factory)
    tenant_id, public_id = make_customer(sqlite_factory)
    before = snapshot(sqlite_factory, tenant_id, public_id)

    with pytest.raises(CustomerNotFound):
        change(sqlite_factory, admin, str(uuid.uuid4()), DISABLED)

    assert snapshot(sqlite_factory, tenant_id, public_id) == before


# --- 正常路径的写入 ---------------------------------------------------------------


def test_a_transition_writes_state_version_audit_and_a_pending_event(sqlite_factory) -> None:
    admin = make_admin(sqlite_factory)
    tenant_id, public_id = make_customer(sqlite_factory)
    later = NOW + dt.timedelta(minutes=5)

    detail = change(sqlite_factory, admin, public_id, DISABLED, now=later)

    assert (detail.account_status, detail.billing_status, detail.status_version) == (
        "DISABLED",
        "SUSPENDED",
        1,
    )
    assert detail.updated_at == later
    [audit] = audits(sqlite_factory, AuditAction.TENANT_ACCOUNT_STATUS_CHANGED, public_id)
    assert (audit.actor_user_id, audit.actor_role) == (admin.id, "ADMIN")
    assert (audit.entity_type, audit.entity_id) == ("tenant", public_id)
    assert (audit.ip_address, audit.user_agent) == (CONTEXT.ip_address, CONTEXT.user_agent)
    assert (audit.reason, audit.created_at) == (REASON, later)
    assert json.loads(audit.before_state or "{}") == {
        "account_status": "ENABLED",
        "status_version": 0,
    }
    assert json.loads(audit.after_state or "{}") == {
        "account_status": "DISABLED",
        "status_version": 1,
    }
    # INV-14：只进数据库 outbox，以 PENDING 持久等待。
    [row] = events(sqlite_factory, EVENT_ACCOUNT_STATUS_CHANGED, public_id)
    assert (row.aggregate_type, row.status) == ("tenant", OutboxStatus.PENDING)
    assert row.attempt_count == 0
    assert (row.created_at, row.next_retry_at, row.processed_at) == (later, later, None)
    assert json.loads(row.payload_json or "{}") == {
        "account_status": "DISABLED",
        "previous_account_status": "ENABLED",
        "billing_status": "SUSPENDED",
        "status_version": 1,
        "changed_at": "2026-09-28T08:35:00",
    }
    assert REASON not in (row.payload_json or "")
    assert snapshot(sqlite_factory, tenant_id, public_id)["tenant"] == (
        DISABLED,
        BillingStatus.SUSPENDED,
        1,
        later,
    )


def test_the_same_target_writes_nothing(sqlite_factory) -> None:
    admin = make_admin(sqlite_factory)
    tenant_id, public_id = make_customer(sqlite_factory)
    first = change(sqlite_factory, admin, public_id, DISABLED)
    before = snapshot(sqlite_factory, tenant_id, public_id)

    again = change(
        sqlite_factory,
        admin,
        public_id,
        DISABLED,
        now=NOW + dt.timedelta(minutes=5),
        reason="Another reason",
    )

    assert again == first
    assert snapshot(sqlite_factory, tenant_id, public_id) == before


# --- 回滚：SQLite 与 MySQL 各一次（INV-13） ---------------------------------------


@pytest.mark.parametrize("failure", ["audit", "outbox", "commit"])
def test_a_failed_transition_leaves_nothing(factory, monkeypatch, failure: str) -> None:
    """设计 §7「事务中途失败」：状态、版本、updated_at 不变；无审计、无 outbox。"""
    admin = make_admin(factory)
    tenant_id, public_id = make_customer(factory)
    before = snapshot(factory, tenant_id, public_id)
    expected: type[Exception] = IntegrityError
    if failure == "audit":
        break_the_audit(monkeypatch)
    elif failure == "outbox":
        break_the_outbox(monkeypatch)
    else:
        break_the_commit(monkeypatch)
        expected = RuntimeError

    with pytest.raises(expected):
        change(factory, admin, public_id, DISABLED, now=NOW + dt.timedelta(minutes=5))

    # 下面的核对只读、不提交，所以不必先撤掉提交的替身。
    assert snapshot(factory, tenant_id, public_id) == before
    assert snapshot(factory, tenant_id, public_id)["tenant"][:3] == (
        ENABLED,
        BillingStatus.SUSPENDED,
        0,
    )


# --- 与计费状态独立：SQLite 与 MySQL 各一次 -----------------------------------------


def test_billing_and_account_status_move_independently(factory) -> None:
    """设计 §7「与计费状态独立」：DISABLED 下调账跨零，计费照常跃迁、账户状态不变，版本各 +1；
    账户状态跃迁不改钱包 `version`（INV-4）。MySQL 上钱包由触发器推进，SQLite 上不动。
    """
    admin = make_admin(factory)
    tenant_id, public_id = make_customer(factory)

    change(factory, admin, public_id, DISABLED)
    adjust(factory, admin, public_id, "20")

    after_adjust = snapshot(factory, tenant_id, public_id)
    assert after_adjust["tenant"][:3] == (DISABLED, BillingStatus.ACTIVE, 2)
    [billing] = audits(factory, AuditAction.TENANT_BILLING_STATUS_CHANGED, public_id)
    assert json.loads(billing.before_state or "{}")["status_version"] == 1
    assert json.loads(billing.after_state or "{}")["status_version"] == 2

    detail = change(factory, admin, public_id, ENABLED)

    after_enable = snapshot(factory, tenant_id, public_id)
    assert after_enable["tenant"][:3] == (ENABLED, BillingStatus.ACTIVE, 3)
    # 账户状态跃迁前后钱包余额与 version 都不变。
    assert after_enable["wallet"] == after_adjust["wallet"]
    assert detail.wallet.version == after_adjust["wallet"][1]
    first, second = events(factory, EVENT_ACCOUNT_STATUS_CHANGED, public_id)
    assert json.loads(first.payload_json or "{}")["billing_status"] == "SUSPENDED"
    assert json.loads(second.payload_json or "{}")["billing_status"] == "ACTIVE"


# --- 只锁租户行、不碰钱包 -----------------------------------------------------------


def test_only_the_tenant_row_is_locked(sqlite_factory, monkeypatch) -> None:
    """设计 §2「事务边界」：客户按路径 public_id 取，带行锁。"""
    admin = make_admin(sqlite_factory)
    _tenant_id, public_id = make_customer(sqlite_factory)
    real = tenancy.get_tenant_by_public_id
    calls: list[tuple[str, bool]] = []

    def spy(session: Session, customer_id: str, *, for_update: bool = False):
        calls.append((customer_id, for_update))
        return real(session, customer_id, for_update=for_update)

    monkeypatch.setattr(tenancy, "get_tenant_by_public_id", spy)

    change(sqlite_factory, admin, public_id, DISABLED)

    # 第一次是事务里的加锁读；第二次是提交之后读详情，不加锁。
    assert calls == [(public_id, True), (public_id, False)]


def test_the_transaction_never_touches_the_wallet(factory, monkeypatch) -> None:
    """INV-4 与设计 §2：事务里没有一条语句碰 `wallets`；MySQL 上租户行加锁读。"""
    admin = make_admin(factory)
    _tenant_id, public_id = make_customer(factory)
    engine = factory.kw["bind"]
    statements: list[str] = []

    def record(_conn, _cursor, statement, _parameters, _context, _executemany) -> None:
        statements.append(" ".join(statement.split()).upper())

    # 详情在提交之后另读，读钱包是那一次的事；这里只看事务里发了什么。
    monkeypatch.setattr(account_status.customers, "get_customer", lambda *_a, **_k: None)
    event.listen(engine, "before_cursor_execute", record)
    try:
        change(factory, admin, public_id, DISABLED)
    finally:
        event.remove(engine, "before_cursor_execute", record)

    assert statements, "没有抓到任何语句，下面的断言在空列表上空转"
    assert not [statement for statement in statements if "WALLET" in statement]
    tenant_reads = [s for s in statements if s.startswith("SELECT") and "FROM TENANTS" in s]
    assert len(tenant_reads) == 1
    if engine.dialect.name == "mysql":
        assert tenant_reads[0].endswith("FOR UPDATE")


def test_the_service_does_not_import_the_wallet_or_log() -> None:
    """INV-4：服务层不导入钱包模型；原因不进应用日志（设计 §6）。"""
    source = inspect.getsource(account_status)
    assert "wallet" not in source.lower()
    assert "logging" not in source
    assert "logger" not in source


# --- 并发版本：只在真 MySQL 上 ------------------------------------------------------


def test_status_version_stays_strictly_increasing_under_concurrency(mysql_factory) -> None:
    """设计 §7「并发版本」：两个线程反复改账户状态、另一线程反复调账跨零。

    `status_version` 严格递增、无重复，等于两类审计条数之和；每条审计与每个事件的版本
    都是它那一刻的前一版本 + 1。
    """
    factory = mysql_factory
    admin = make_admin(factory)
    tenant_id, public_id = make_customer(factory)
    rounds = 8
    start = threading.Barrier(3)

    def toggler(_index: int) -> None:
        start.wait(timeout=30)
        for number in range(rounds):
            change(factory, admin, public_id, DISABLED if number % 2 == 0 else ENABLED)

    def adjuster() -> None:
        start.wait(timeout=30)
        # 0 → 20 → 0 → 20 …：每一笔都跨零，每一笔都是一次计费跃迁。
        for number in range(rounds):
            adjust(factory, admin, public_id, "20" if number % 2 == 0 else "-20")

    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(toggler, 0), pool.submit(toggler, 1), pool.submit(adjuster)]
        for future in futures:
            future.result(timeout=120)

    account = audits(factory, AuditAction.TENANT_ACCOUNT_STATUS_CHANGED, public_id)
    billing = audits(factory, AuditAction.TENANT_BILLING_STATUS_CHANGED, public_id)
    assert len(billing) == rounds
    assert account, "两个线程至少各有一次真实跃迁"

    versions = []
    for audit in account + billing:
        before = json.loads(audit.before_state or "{}")["status_version"]
        after = json.loads(audit.after_state or "{}")["status_version"]
        assert after == before + 1
        versions.append(after)
    total = len(account) + len(billing)
    assert sorted(versions) == list(range(1, total + 1))

    final = snapshot(factory, tenant_id, public_id)["tenant"]
    assert final[2] == total
    # 八笔调账之后余额回到 0：SUSPENDED。
    assert final[1] is BillingStatus.SUSPENDED

    account_events = events(factory, EVENT_ACCOUNT_STATUS_CHANGED, public_id)
    billing_events = events(factory, "tenant.billing_status_changed", public_id)
    event_versions = [
        json.loads(row.payload_json or "{}")["status_version"]
        for row in account_events + billing_events
    ]
    assert sorted(event_versions) == list(range(1, total + 1))
    assert len(account_events) == len(account)
    # 账户状态事件里的前后状态首尾相接：没有两次跃迁读到同一个「当前状态」。
    ordered = sorted(
        (json.loads(row.payload_json or "{}") for row in account_events),
        key=lambda payload: payload["status_version"],
    )
    for earlier, later in zip(ordered, ordered[1:], strict=False):
        assert later["previous_account_status"] == earlier["account_status"]

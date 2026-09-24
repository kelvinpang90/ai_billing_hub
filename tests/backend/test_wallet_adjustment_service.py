"""Admin wallet adjustment service (design gate #111 v1 §3 INV-13, §4, §5, §7).

三类用例：

- **回滚**（审计写入失败、提交失败）：`factory` 夹具的两个参数，在 SQLite 与真 MySQL 上
  各跑一次；
- **依赖记账前余额的场景**（真实触发器、跨零恢复、跨零暂停、余额超出范围、并发同键）：
  只在真 MySQL 上。SQLite 没有钱包触发器，余额不会随账本推进；
- **重试与错误映射**：只在 SQLite 上，把 `post_transaction` 换成按剧本抛异常的替身。

MySQL 那部分需要 `BILLING_TEST_DATABASE_URL` 指向一个**可以被清空的**库，没设时 skip ——
**不要把 skipped 读成 passed**，CI 设了它并把 skipped 判成失败。
"""

from __future__ import annotations

import datetime as dt
import inspect
import json
import os
import re
import threading
import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import pytest
from alembic.config import Config
from sqlalchemy import Engine, create_engine, delete, func, select, text
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.orm.attributes import set_committed_value

from alembic import command
from app.core.database import create_session_factory
from app.models.auth import AuditAction, AuditLog, DomainOutbox, User, UserRole, UserStatus
from app.models.base import Base
from app.models.tenancy import BillingStatus, Project, Tenant
from app.models.wallet import TransactionType, Wallet, WalletTransaction
from app.repositories import tenancy
from app.repositories import wallet as wallet_repository
from app.repositories.wallet import (
    EVENT_BILLING_STATUS_CHANGED,
    REASON_BALANCE_NON_POSITIVE,
    REASON_BALANCE_POSITIVE,
    InvalidAmount,
    InvalidTransaction,
    LedgerConflict,
    create_wallet,
    get_wallet_for_tenant,
    verify_wallet,
)
from app.schemas.wallet_adjustments import AdjustmentView
from app.services import wallet_adjustments
from app.services.auth import RequestContext
from app.services.customers import CustomerNotFound
from app.services.wallet_adjustments import (
    AdjustmentConflict,
    AdjustmentRejected,
    BalanceOutOfRange,
    post_adjustment,
)

TEST_DATABASE_URL = os.environ.get("BILLING_TEST_DATABASE_URL", "")
TEST_EMAIL_DOMAIN = "@wallet-adjustment-test.example.com"

# 固定值而不是 utc_now()：断言「存进去的就是调用方给的那个时刻」要能逐字比对。
NOW = dt.datetime(2026, 9, 24, 8, 30, 0)
CONTEXT = RequestContext(ip_address="203.0.113.7", user_agent="wallet-adjustment-service-test")
REASON = "Goodwill credit for outage 2026-09-20"

CREDIT = TransactionType.ADJUSTMENT_CREDIT
DEBIT = TransactionType.ADJUSTMENT_DEBIT

OUR_ACTIONS = [
    AuditAction.WALLET_ADJUSTMENT_POSTED,
    AuditAction.TENANT_BILLING_STATUS_CHANGED,
]


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
    # ⚠️ 建表必须走 alembic：钱包的触发器只有迁移会建（同 test_wallet_repository.py）。
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
    """⚠️ 库是共享的，每个用例前后都清场（与 test_wallet_repository.py 同一做法）。"""
    with engine.begin() as connection:
        # 账本拒绝 DELETE，只能 TRUNCATE；有账本行的钱包删不掉。
        connection.execute(text("TRUNCATE TABLE wallet_transactions"))
    with engine.begin() as connection:
        connection.execute(delete(Wallet))
        connection.execute(delete(Project))
        connection.execute(delete(Tenant))
        connection.execute(delete(AuditLog).where(AuditLog.action.in_(OUR_ACTIONS)))
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


def make_customer(factory) -> tuple[int, str]:
    """A tenant with an empty wallet: balance 0, SUSPENDED, status_version 0."""
    with factory() as session:
        tenant = tenancy.create_tenant(
            session, company_name="Adjustment Test Sdn Bhd", email="ops@example.com", now=NOW
        )
        create_wallet(session, tenant_id=tenant.id, now=NOW)
        session.commit()
        return int(tenant.id), tenant.public_id


def adjust(
    factory,
    admin: User,
    customer_id: str,
    kind: TransactionType,
    amount: str,
    *,
    key: str | None = None,
    reason: str = REASON,
) -> AdjustmentView:
    return post_adjustment(
        factory,
        actor=admin,
        customer_id=customer_id,
        transaction_type=kind,
        amount=Decimal(amount),
        reason=reason,
        idempotency_key=key or str(uuid.uuid4()),
        context=CONTEXT,
        now=NOW,
    )


def audits(factory, action: AuditAction, entity_id: str | None = None) -> list[AuditLog]:
    statement = select(AuditLog).where(AuditLog.action == action)
    if entity_id is not None:
        statement = statement.where(AuditLog.entity_id == entity_id)
    with factory() as session:
        return list(session.execute(statement.order_by(AuditLog.id)).scalars())


def status_events(factory, public_id: str) -> list[dict[str, object]]:
    statement = (
        select(DomainOutbox)
        .where(DomainOutbox.event_type == EVENT_BILLING_STATUS_CHANGED)
        .where(DomainOutbox.aggregate_id == public_id)
        .order_by(DomainOutbox.id)
    )
    with factory() as session:
        rows = list(session.execute(statement).scalars())
    return [json.loads(row.payload_json or "{}") for row in rows]


def snapshot(factory, tenant_id: int, public_id: str) -> dict[str, object]:
    """Everything one adjustment may write, as committed."""
    ours = AuditLog.action.in_(OUR_ACTIONS)
    theirs = DomainOutbox.aggregate_id == public_id
    queries = {
        "ledger": select(func.count()).select_from(WalletTransaction),
        "audits": select(func.count()).select_from(AuditLog).where(ours),
        "outbox": select(func.count()).select_from(DomainOutbox).where(theirs),
    }
    with factory() as session:
        wallet = get_wallet_for_tenant(session, tenant_id)
        tenant = session.get(Tenant, tenant_id)
        assert wallet is not None and tenant is not None
        counts = {name: session.execute(query).scalar_one() for name, query in queries.items()}
        return {
            "wallet": (wallet.balance, wallet.version),
            "billing": (tenant.billing_status, tenant.status_version),
            **counts,
        }


def wallet_state(factory, tenant_id: int) -> tuple[Decimal, int, BillingStatus, int]:
    """(balance, wallet version, billing_status, status_version), as committed."""
    with factory() as session:
        wallet = get_wallet_for_tenant(session, tenant_id)
        tenant = session.get(Tenant, tenant_id)
        assert wallet is not None and tenant is not None
        return wallet.balance, wallet.version, tenant.billing_status, tenant.status_version


# --- 回滚：SQLite 与 MySQL 各一次（INV-13） ---------------------------------------


def test_a_failed_adjustment_audit_leaves_nothing(factory, monkeypatch) -> None:
    """调账审计在 flush 时失败（`created_at` 为 NULL）：什么都不留下。

    这一笔 0 → 20 跨零，所以回滚的范围里也有计费状态跃迁。`IntegrityError` 触发服务层
    那一次重试，第二次同样失败，原样抛出。
    """
    admin = make_admin(factory)
    tenant_id, public_id = make_customer(factory)
    before = snapshot(factory, tenant_id, public_id)
    real = wallet_repository._adjustment_audit
    calls = []

    def broken_audit(row, **options):
        calls.append(row.public_id)
        audit = real(row, **options)
        audit.created_at = None
        return audit

    monkeypatch.setattr(wallet_repository, "_adjustment_audit", broken_audit)

    with pytest.raises(IntegrityError):
        adjust(factory, admin, public_id, CREDIT, "20")

    assert len(calls) == 2
    assert snapshot(factory, tenant_id, public_id) == before
    assert before["billing"] == (BillingStatus.SUSPENDED, 0)


def test_a_failed_commit_leaves_nothing(factory, monkeypatch) -> None:
    """跨零调账在提交那一刻失败：什么都不留下。不是 `IntegrityError`，所以不重试。"""
    admin = make_admin(factory)
    tenant_id, public_id = make_customer(factory)
    before = snapshot(factory, tenant_id, public_id)
    calls = []

    def failing_commit(self: Session) -> None:
        calls.append(self)
        raise RuntimeError("injected commit failure")

    monkeypatch.setattr(Session, "commit", failing_commit)

    with pytest.raises(RuntimeError, match="injected commit failure"):
        adjust(factory, admin, public_id, CREDIT, "20")

    # 下面的核对只读、不提交，所以不必先撤掉这个替身。
    assert len(calls) == 1
    assert snapshot(factory, tenant_id, public_id) == before


def test_an_unknown_customer_is_not_found_and_writes_nothing(factory) -> None:
    admin = make_admin(factory)
    tenant_id, public_id = make_customer(factory)
    before = snapshot(factory, tenant_id, public_id)

    with pytest.raises(CustomerNotFound):
        adjust(factory, admin, str(uuid.uuid4()), CREDIT, "20")

    assert snapshot(factory, tenant_id, public_id) == before


# --- 真实触发器与记账前余额（MySQL） ----------------------------------------------


def test_consecutive_adjustments_move_the_real_wallet(mysql_factory) -> None:
    """设计 §7「正常路径与真实触发器」：+20 再 -5.12345678。"""
    admin = make_admin(mysql_factory)
    tenant_id, public_id = make_customer(mysql_factory)

    first = adjust(mysql_factory, admin, public_id, CREDIT, "20")
    second = adjust(mysql_factory, admin, public_id, DEBIT, "-5.12345678")

    assert (first.balance_before, first.balance_after, first.wallet_sequence) == (
        "0.00000000",
        "20.00000000",
        1,
    )
    assert (second.balance_before, second.balance_after, second.wallet_sequence) == (
        "20.00000000",
        "14.87654322",
        2,
    )
    assert (second.billing_status, second.status_version) == ("ACTIVE", 1)
    assert wallet_state(mysql_factory, tenant_id) == (
        Decimal("14.87654322"),
        2,
        BillingStatus.ACTIVE,
        1,
    )
    with mysql_factory() as session:
        assert verify_wallet(session, tenant_id) == []

    trail = audits(mysql_factory, AuditAction.WALLET_ADJUSTMENT_POSTED)
    assert [audit.entity_id for audit in trail] == [first.id, second.id]
    for audit in trail:
        assert (audit.actor_user_id, audit.actor_role) == (admin.id, "ADMIN")
        assert (audit.ip_address, audit.user_agent) == (CONTEXT.ip_address, CONTEXT.user_agent)
        assert audit.reason == REASON
        assert audit.created_at == NOW
    assert json.loads(trail[1].before_state or "{}") == {"balance": "20.00000000"}
    assert json.loads(trail[1].after_state or "{}") == {"balance": "14.87654322"}


def test_a_credit_across_zero_resumes_billing_in_the_same_transaction(mysql_factory) -> None:
    """设计 §7「跨零恢复」：-5（仍 SUSPENDED）之后 +20 → 15、ACTIVE、status_version +1。"""
    admin = make_admin(mysql_factory)
    tenant_id, public_id = make_customer(mysql_factory)
    adjust(mysql_factory, admin, public_id, DEBIT, "-5")
    assert wallet_state(mysql_factory, tenant_id) == (
        Decimal("-5"),
        1,
        BillingStatus.SUSPENDED,
        0,
    )

    resumed = adjust(mysql_factory, admin, public_id, CREDIT, "20")

    assert (resumed.balance_before, resumed.balance_after) == ("-5.00000000", "15.00000000")
    assert (resumed.billing_status, resumed.status_version) == ("ACTIVE", 1)
    assert wallet_state(mysql_factory, tenant_id) == (
        Decimal("15"),
        2,
        BillingStatus.ACTIVE,
        1,
    )
    [transition] = audits(mysql_factory, AuditAction.TENANT_BILLING_STATUS_CHANGED, public_id)
    assert transition.reason == REASON_BALANCE_POSITIVE
    assert (transition.actor_user_id, transition.actor_role) == (None, "SYSTEM")
    # 跃迁的操作者是系统，不带这次请求的 ip 与 user agent（设计 §2）。
    assert (transition.ip_address, transition.user_agent) == (None, None)
    assert json.loads(transition.after_state or "{}")["wallet_transaction"] == resumed.id
    [event] = status_events(mysql_factory, public_id)
    assert event == {
        "billing_status": "ACTIVE",
        "status_version": 1,
        "reason": REASON_BALANCE_POSITIVE,
        "balance": "15.00000000",
        "wallet_transaction": resumed.id,
    }
    with mysql_factory() as session:
        assert verify_wallet(session, tenant_id) == []


def test_a_debit_down_to_zero_suspends_billing(mysql_factory) -> None:
    """设计 §7「跨零暂停」：余额 15、ACTIVE，记 -15 → 0、SUSPENDED，一次跃迁。"""
    admin = make_admin(mysql_factory)
    tenant_id, public_id = make_customer(mysql_factory)
    adjust(mysql_factory, admin, public_id, CREDIT, "15")
    assert wallet_state(mysql_factory, tenant_id)[2:] == (BillingStatus.ACTIVE, 1)

    suspended = adjust(mysql_factory, admin, public_id, DEBIT, "-15")

    assert suspended.balance_after == "0.00000000"
    assert (suspended.billing_status, suspended.status_version) == ("SUSPENDED", 2)
    assert wallet_state(mysql_factory, tenant_id) == (
        Decimal("0"),
        2,
        BillingStatus.SUSPENDED,
        2,
    )
    trail = audits(mysql_factory, AuditAction.TENANT_BILLING_STATUS_CHANGED, public_id)
    assert [audit.reason for audit in trail] == [
        REASON_BALANCE_POSITIVE,
        REASON_BALANCE_NON_POSITIVE,
    ]
    versions = [event["status_version"] for event in status_events(mysql_factory, public_id)]
    assert versions == [1, 2]
    with mysql_factory() as session:
        assert verify_wallet(session, tenant_id) == []


def test_a_balance_beyond_decimal_20_8_is_refused_and_writes_nothing(mysql_factory) -> None:
    """设计 §7「余额超出范围」：422 `BALANCE_OUT_OF_RANGE`，不写库。"""
    admin = make_admin(mysql_factory)
    tenant_id, public_id = make_customer(mysql_factory)
    adjust(mysql_factory, admin, public_id, CREDIT, "999999999999")
    before = snapshot(mysql_factory, tenant_id, public_id)

    with pytest.raises(BalanceOutOfRange) as raised:
        adjust(mysql_factory, admin, public_id, CREDIT, "1")

    assert (raised.value.code, raised.value.http_status) == ("BALANCE_OUT_OF_RANGE", 422)
    assert snapshot(mysql_factory, tenant_id, public_id) == before
    assert before["wallet"] == (Decimal("999999999999"), 1)


def test_the_same_key_from_eight_threads_posts_once(mysql_factory) -> None:
    """设计 §4 的漏网路径：后到的请求撞唯一约束，服务层换新事务重试一次走重放分支。

    恰好一行账本、一条调账审计；恰好一个 `replayed = false`；没有任何异常（没有 500）。
    """
    admin = make_admin(mysql_factory)
    tenant_id, public_id = make_customer(mysql_factory)
    key = str(uuid.uuid4())
    start = threading.Barrier(8)

    def attempt(_index: int) -> AdjustmentView:
        start.wait(timeout=30)
        return adjust(mysql_factory, admin, public_id, CREDIT, "20", key=key)

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(attempt, range(8)))

    assert sorted(result.replayed for result in results) == [False] + [True] * 7
    assert len({result.id for result in results}) == 1
    assert {result.balance_after for result in results} == {"20.00000000"}
    assert snapshot(mysql_factory, tenant_id, public_id)["ledger"] == 1
    assert len(audits(mysql_factory, AuditAction.WALLET_ADJUSTMENT_POSTED)) == 1
    assert wallet_state(mysql_factory, tenant_id) == (
        Decimal("20"),
        1,
        BillingStatus.ACTIVE,
        1,
    )
    # 重放的响应也要带提交后的状态，不能是读租户那一刻的 SUSPENDED / 0。
    assert {(result.billing_status, result.status_version) for result in results} == {
        (BillingStatus.ACTIVE.value, 1)
    }


def test_a_replay_reports_the_status_committed_after_the_tenant_was_read(
    factory, monkeypatch
) -> None:
    """重放分支不经过 `post_transaction` 的租户锁，响应里的状态要另外重读（PR #115 评审）。

    确定性地复现并发重放的时序：服务开头读到的租户早于第一笔的提交（还是 SUSPENDED / 0），
    重放时库里已经是 ACTIVE / 1。响应必须报后者。
    """
    admin = make_admin(factory)
    _tenant_id, public_id = make_customer(factory)
    key = str(uuid.uuid4())
    first = adjust(factory, admin, public_id, CREDIT, "20", key=key)
    assert (first.billing_status, first.status_version) == ("ACTIVE", 1)
    real = tenancy.get_tenant_by_public_id

    def read_before_the_first_commit(
        session: Session, customer_id: str, *, for_update: bool = False
    ):
        tenant = real(session, customer_id, for_update=for_update)
        set_committed_value(tenant, "billing_status", BillingStatus.SUSPENDED)
        set_committed_value(tenant, "status_version", 0)
        return tenant

    monkeypatch.setattr(tenancy, "get_tenant_by_public_id", read_before_the_first_commit)

    replay = adjust(factory, admin, public_id, CREDIT, "20", key=key)

    assert replay.replayed is True
    assert replay.id == first.id
    assert (replay.billing_status, replay.status_version) == ("ACTIVE", 1)


# --- 重试与错误映射（SQLite，替身） -----------------------------------------------


def _integrity_error() -> IntegrityError:
    return IntegrityError("INSERT INTO wallet_transactions", {}, Exception("duplicate"))


def _scripted(monkeypatch, *failures: BaseException | None) -> list[int]:
    """Replace `post_transaction`: the n-th call raises `failures[n]`, or posts for real."""
    real = wallet_adjustments.post_transaction
    calls: list[int] = []

    def scripted(session: Session, **options: object):
        calls.append(len(calls))
        failure = failures[len(calls) - 1] if len(calls) <= len(failures) else None
        if failure is not None:
            raise failure
        return real(session, **options)

    monkeypatch.setattr(wallet_adjustments, "post_transaction", scripted)
    return calls


def test_one_integrity_error_is_retried_in_a_new_transaction(sqlite_factory, monkeypatch) -> None:
    admin = make_admin(sqlite_factory)
    tenant_id, public_id = make_customer(sqlite_factory)
    calls = _scripted(monkeypatch, _integrity_error())

    view = adjust(sqlite_factory, admin, public_id, CREDIT, "20")

    assert len(calls) == 2
    assert view.replayed is False
    assert snapshot(sqlite_factory, tenant_id, public_id)["ledger"] == 1


def test_a_second_integrity_error_is_raised_as_is(sqlite_factory, monkeypatch) -> None:
    admin = make_admin(sqlite_factory)
    tenant_id, public_id = make_customer(sqlite_factory)
    before = snapshot(sqlite_factory, tenant_id, public_id)
    second = _integrity_error()
    calls = _scripted(monkeypatch, _integrity_error(), second)

    with pytest.raises(IntegrityError) as raised:
        adjust(sqlite_factory, admin, public_id, CREDIT, "20")

    assert raised.value is second
    assert len(calls) == 2
    assert snapshot(sqlite_factory, tenant_id, public_id) == before


def test_an_operational_error_is_not_retried(sqlite_factory, monkeypatch) -> None:
    """锁等待超时与死锁回滚后按 500 返回，由管理员带同一个键重发（设计 §4）。"""
    admin = make_admin(sqlite_factory)
    _tenant_id, public_id = make_customer(sqlite_factory)
    timeout = OperationalError("SELECT ... FOR UPDATE", {}, Exception("lock wait timeout"))
    calls = _scripted(monkeypatch, timeout)

    with pytest.raises(OperationalError):
        adjust(sqlite_factory, admin, public_id, CREDIT, "20")

    assert len(calls) == 1


@pytest.mark.parametrize(
    ("failure", "error", "code", "status"),
    [
        (LedgerConflict("X"), AdjustmentConflict, "ADJUSTMENT_CONFLICT", 409),
        (InvalidAmount("BALANCE_OUT_OF_RANGE"), BalanceOutOfRange, "BALANCE_OUT_OF_RANGE", 422),
        (InvalidAmount("AMOUNT_ZERO"), AdjustmentRejected, "VALIDATION_ERROR", 422),
        (InvalidTransaction("REASON_REQUIRED"), AdjustmentRejected, "VALIDATION_ERROR", 422),
    ],
    ids=["conflict", "balance-out-of-range", "invalid-amount", "invalid-transaction"],
)
def test_ledger_errors_map_to_their_http_errors(
    sqlite_factory, monkeypatch, failure, error, code: str, status: int
) -> None:
    admin = make_admin(sqlite_factory)
    tenant_id, public_id = make_customer(sqlite_factory)
    before = snapshot(sqlite_factory, tenant_id, public_id)
    calls = _scripted(monkeypatch, failure)

    with pytest.raises(error) as raised:
        adjust(sqlite_factory, admin, public_id, CREDIT, "20", reason="Probe reason 41d7")

    assert (raised.value.code, raised.value.http_status) == (code, status)
    # 消息里只有问题码，不含原因或金额。
    assert "Probe reason 41d7" not in raised.value.message
    assert len(calls) == 1
    assert snapshot(sqlite_factory, tenant_id, public_id) == before


def test_the_tenant_is_read_without_a_lock(sqlite_factory, monkeypatch) -> None:
    """设计 §2：先锁租户就与 `post_transaction` 的钱包 → 租户加锁顺序相反，会死锁。"""
    admin = make_admin(sqlite_factory)
    _tenant_id, public_id = make_customer(sqlite_factory)
    real = tenancy.get_tenant_by_public_id
    locks: list[bool] = []

    def spy(session: Session, customer_id: str, *, for_update: bool = False):
        locks.append(for_update)
        return real(session, customer_id, for_update=for_update)

    monkeypatch.setattr(tenancy, "get_tenant_by_public_id", spy)

    adjust(sqlite_factory, admin, public_id, CREDIT, "20")

    assert locks == [False]


def test_the_service_neither_logs_nor_touches_wallets_directly() -> None:
    """设计 §6 与 INV-4：这一层不写日志；余额只经 `post_transaction` 的账本行变动。"""
    source = inspect.getsource(wallet_adjustments)

    assert "logging" not in source
    assert "logger" not in source
    assert "update wallets" not in source.lower()
    assert "update(" not in source
    # 连 `Wallet` 模型都不引入：没有引用，就不可能直接改它。
    assert re.search(r"\bWallet\b", source) is None

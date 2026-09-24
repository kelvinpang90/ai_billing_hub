"""Admin wallet adjustment service (design gate #111 v1 §3 INV-13, §4, §5, §7).

⚠️ **依赖记账前余额的场景只在真 MySQL 上跑**：跨零恢复、跨零暂停、连续记账、余额超出
范围、并发同键。SQLite 上没有迁移 0006 的钱包触发器，钱包余额根本不随账本推进（设计
§7）。审计失败与提交失败两个回滚用例在 SQLite 与 MySQL 上各跑一次（`factory` 夹具的
两个参数）；唯一约束兜底的重试语义用注入的异常在 SQLite 上测。

MySQL 那一半需要 `BILLING_TEST_DATABASE_URL` 指向一个**可以被清空的**库，没设时 skip ——
**不要把 skipped 读成 passed**，CI 设了它并把 skipped 判成失败。
"""

from __future__ import annotations

import ast
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
from sqlalchemy import Engine, create_engine, delete, func, select, text
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session, sessionmaker

from alembic import command
from app.core.database import create_session_factory, session_scope
from app.models.auth import AuditAction, AuditLog, DomainOutbox, User, UserRole, UserStatus
from app.models.base import Base
from app.models.tenancy import BillingStatus, Project, Tenant
from app.models.wallet import Wallet, WalletTransaction
from app.repositories import tenancy
from app.repositories import wallet as wallet_repository
from app.repositories.wallet import (
    EVENT_BILLING_STATUS_CHANGED,
    REASON_BALANCE_NON_POSITIVE,
    REASON_BALANCE_POSITIVE,
    WalletNotFound,
    get_wallet_for_tenant,
    verify_wallet,
)
from app.schemas.wallet_adjustments import AdjustmentView
from app.services import customers, wallet_adjustments
from app.services.auth import RequestContext
from app.services.customers import CustomerNotFound
from app.services.wallet_adjustments import (
    AdjustmentConflict,
    AdjustmentRejected,
    BalanceOutOfRange,
)

TEST_DATABASE_URL = os.environ.get("BILLING_TEST_DATABASE_URL", "")
TEST_EMAIL_DOMAIN = "@wallet-adjustment-test.example.com"

# 固定值而不是 utc_now()：断言「存进去的就是调用方给的那个时刻」要能逐字比对。
NOW = dt.datetime(2026, 9, 24, 8, 30, 0)
CONTEXT = RequestContext(ip_address="203.0.113.7", user_agent="wallet-adjustment-test")
REASON = "Goodwill credit for outage 2026-09-20"

# 这个服务（与建客户）写的审计动作。MySQL 库是共享的，清场只碰这几种。
OUR_ACTIONS = [
    AuditAction.CUSTOMER_CREATE,
    AuditAction.WALLET_ADJUSTMENT_POSTED,
    AuditAction.TENANT_BILLING_STATUS_CHANGED,
]


# --- 夹具 -----------------------------------------------------------------------


@pytest.fixture(params=["sqlite", "mysql"])
def factory(request, monkeypatch, tmp_path) -> Iterator[sessionmaker[Session]]:
    if request.param == "mysql":
        yield from _mysql_factory(monkeypatch)
        return
    yield from _sqlite_factory(tmp_path)


@pytest.fixture
def sqlite_factory(tmp_path) -> Iterator[sessionmaker[Session]]:
    yield from _sqlite_factory(tmp_path)


@pytest.fixture
def mysql_factory(monkeypatch) -> Iterator[sessionmaker[Session]]:
    yield from _mysql_factory(monkeypatch)


def _sqlite_factory(tmp_path) -> Iterator[sessionmaker[Session]]:
    # 文件库而不是内存库：重试用例要两个会话各用各的连接，像真实的两个请求。
    database = tmp_path / "billing.db"
    engine = create_engine(f"sqlite+pysqlite:///{database.as_posix()}")
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

    # 并发用例 8 个线程，每个线程最多先后用两个连接（重试）。
    engine = create_engine(TEST_DATABASE_URL, pool_size=16, max_overflow=8)
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


def make_customer(factory, admin: User) -> str:
    """A new customer: empty MYR wallet, SUSPENDED, status_version 0."""
    detail = customers.create_customer(
        factory,
        actor=admin,
        company_name="Adjustment Test Sdn Bhd",
        email="ops@example.com",
        context=CONTEXT,
        now=NOW,
    )
    return detail.id


def adjust(
    factory,
    admin: User,
    customer_id: str,
    kind: str,
    amount: str,
    *,
    key: str | None = None,
    reason: str = REASON,
) -> AdjustmentView:
    return wallet_adjustments.post_adjustment(
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


def _count(session: Session, model, *conditions) -> int:
    statement = select(func.count()).select_from(model)
    for condition in conditions:
        statement = statement.where(condition)
    return session.execute(statement).scalar_one()


def snapshot(factory, customer_id: str) -> dict[str, object]:
    """Everything one adjustment may write for this customer, as committed."""
    with factory() as session:
        tenant = tenancy.get_tenant_by_public_id(session, customer_id)
        assert tenant is not None
        wallet = get_wallet_for_tenant(session, tenant.id)
        assert wallet is not None
        return {
            "billing_status": tenant.billing_status,
            "status_version": tenant.status_version,
            "wallet": (wallet.balance, wallet.version),
            "ledger": _count(session, WalletTransaction, WalletTransaction.tenant_id == tenant.id),
            "adjustment_audits": _count(
                session, AuditLog, AuditLog.action == AuditAction.WALLET_ADJUSTMENT_POSTED
            ),
            "status_audits": _count(
                session,
                AuditLog,
                AuditLog.action == AuditAction.TENANT_BILLING_STATUS_CHANGED,
                AuditLog.entity_id == customer_id,
            ),
            "outbox": _count(session, DomainOutbox, DomainOutbox.aggregate_id == customer_id),
        }


def audits(factory, action: AuditAction, entity_id: str | None = None) -> list[AuditLog]:
    statement = select(AuditLog).where(AuditLog.action == action)
    if entity_id is not None:
        statement = statement.where(AuditLog.entity_id == entity_id)
    with factory() as session:
        return list(session.execute(statement.order_by(AuditLog.id)).scalars())


def status_events(factory, customer_id: str) -> list[dict[str, object]]:
    statement = (
        select(DomainOutbox)
        .where(DomainOutbox.event_type == EVENT_BILLING_STATUS_CHANGED)
        .where(DomainOutbox.aggregate_id == customer_id)
        .order_by(DomainOutbox.id)
    )
    with factory() as session:
        rows = list(session.execute(statement).scalars())
    return [json.loads(row.payload_json or "{}") for row in rows]


def verified(factory, customer_id: str) -> list[str]:
    with factory() as session:
        tenant = tenancy.get_tenant_by_public_id(session, customer_id)
        assert tenant is not None
        return verify_wallet(session, tenant.id)


def _collision() -> IntegrityError:
    return IntegrityError("INSERT INTO wallet_transactions", {}, Exception("duplicate key"))


# --- 回滚（INV-13）：SQLite 与 MySQL 各一次 --------------------------------------


@pytest.mark.parametrize("failure", ["flush", "raise"])
def test_a_failed_adjustment_audit_leaves_no_trace(factory, monkeypatch, failure: str) -> None:
    """写调账审计失败：账本行、审计、租户状态与版本、出站事件都与调用前相同。

    `flush`：审计行的 `created_at` 为 NULL，flush 时违反 NOT NULL（`IntegrityError`），
    服务层按唯一约束兜底重试一次，第二次仍失败就原样抛出。`raise`：直接抛异常，不重试。
    """
    admin = make_admin(factory)
    customer_id = make_customer(factory, admin)
    before = snapshot(factory, customer_id)
    real = wallet_repository._adjustment_audit
    calls: list[str] = []

    def failing_audit(row, **options):
        calls.append(row.public_id)
        if failure == "raise":
            raise RuntimeError("injected failure")
        audit = real(row, **options)
        audit.created_at = None
        return audit

    monkeypatch.setattr(wallet_repository, "_adjustment_audit", failing_audit)

    expected = IntegrityError if failure == "flush" else RuntimeError
    with pytest.raises(expected):
        # 余额 0 → 20：跨零，成功的话会恢复 ACTIVE、写跃迁审计与出站事件。
        adjust(factory, admin, customer_id, "ADJUSTMENT_CREDIT", "20")

    assert len(calls) == (2 if failure == "flush" else 1)
    assert snapshot(factory, customer_id) == before
    assert before["billing_status"] is BillingStatus.SUSPENDED


def test_a_failed_commit_leaves_no_trace(factory, monkeypatch) -> None:
    """跨零的一笔在提交那一刻失败：什么都不留下；同一个键之后照常入账。"""
    admin = make_admin(factory)
    customer_id = make_customer(factory, admin)
    before = snapshot(factory, customer_id)
    real = wallet_adjustments.post_transaction
    written = []

    def spy(session: Session, **options):
        result = real(session, **options)
        # 先确认这些东西确实写进了事务 —— 否则「回滚后什么都没有」证明不了任何事。
        tenant = session.get(Tenant, options["tenant_id"])
        assert tenant is not None
        written.append((result.replayed, tenant.billing_status))
        return result

    def failing_commit(self: Session) -> None:
        raise RuntimeError("injected commit failure")

    monkeypatch.setattr(wallet_adjustments, "post_transaction", spy)
    monkeypatch.setattr(Session, "commit", failing_commit)
    key = str(uuid.uuid4())

    with pytest.raises(RuntimeError, match="injected commit failure"):
        adjust(factory, admin, customer_id, "ADJUSTMENT_CREDIT", "20", key=key)

    monkeypatch.undo()
    assert written == [(False, BillingStatus.ACTIVE)]
    assert snapshot(factory, customer_id) == before

    # 幂等键没有被那次失败占掉：带同一个键重发，第一次真正入账。
    retried = adjust(factory, admin, customer_id, "ADJUSTMENT_CREDIT", "20", key=key)
    assert retried.replayed is False
    assert (retried.billing_status, retried.status_version) == ("ACTIVE", 1)


# --- 唯一约束兜底（设计 §4）：SQLite 上注入 ---------------------------------------


def test_a_unique_collision_is_retried_once_in_a_new_transaction(
    sqlite_factory, monkeypatch
) -> None:
    """模拟 REPEATABLE READ 的漏网路径：另一个请求抢先提交了同一个键，这一次撞唯一约束。

    重试在一个新会话、新事务里进行，看得到那一行，走重放分支。
    """
    admin = make_admin(sqlite_factory)
    customer_id = make_customer(sqlite_factory, admin)
    real = wallet_adjustments.post_transaction
    sessions: list[Session] = []
    winner: list[str] = []

    def racing(session: Session, **options):
        sessions.append(session)
        if len(sessions) == 1:
            with session_scope(sqlite_factory) as other:
                winner.append(real(other, **options).transaction.public_id)
            raise _collision()
        return real(session, **options)

    monkeypatch.setattr(wallet_adjustments, "post_transaction", racing)

    result = adjust(sqlite_factory, admin, customer_id, "ADJUSTMENT_CREDIT", "20")

    assert len(sessions) == 2
    assert sessions[0] is not sessions[1]
    assert result.replayed is True
    assert result.id == winner[0]
    assert snapshot(sqlite_factory, customer_id)["ledger"] == 1


def test_a_second_collision_is_raised_as_is(sqlite_factory, monkeypatch) -> None:
    admin = make_admin(sqlite_factory)
    customer_id = make_customer(sqlite_factory, admin)
    before = snapshot(sqlite_factory, customer_id)
    calls = []

    def colliding(session: Session, **options):
        calls.append(session)
        raise _collision()

    monkeypatch.setattr(wallet_adjustments, "post_transaction", colliding)

    with pytest.raises(IntegrityError):
        adjust(sqlite_factory, admin, customer_id, "ADJUSTMENT_CREDIT", "20")

    assert len(calls) == 2
    assert snapshot(sqlite_factory, customer_id) == before


def test_lock_timeouts_and_deadlocks_are_not_retried(sqlite_factory, monkeypatch) -> None:
    """`OperationalError` 回滚后原样抛出（500），由管理员带同一个键重发。"""
    admin = make_admin(sqlite_factory)
    customer_id = make_customer(sqlite_factory, admin)
    calls = []

    def timing_out(session: Session, **options):
        calls.append(session)
        raise OperationalError("SELECT ... FOR UPDATE", {}, Exception("lock wait timeout"))

    monkeypatch.setattr(wallet_adjustments, "post_transaction", timing_out)

    with pytest.raises(OperationalError):
        adjust(sqlite_factory, admin, customer_id, "ADJUSTMENT_CREDIT", "20")

    assert len(calls) == 1


# --- 余额只经账本（INV-4） ------------------------------------------------------


def test_the_service_never_writes_the_wallet_directly() -> None:
    """设计 §3：服务层只调 `post_transaction`，没有任何 UPDATE wallets。"""
    source = inspect.getsource(wallet_adjustments)
    imported = {
        alias.asname or alias.name
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }

    assert "update wallets" not in source.lower()
    assert {"update", "Wallet", "text"}.isdisjoint(imported)
    assert "post_transaction" in imported


# --- 错误映射 -------------------------------------------------------------------


def test_ledger_errors_become_contract_errors_and_write_nothing(sqlite_factory) -> None:
    admin = make_admin(sqlite_factory)
    customer_id = make_customer(sqlite_factory, admin)
    key = str(uuid.uuid4())
    adjust(sqlite_factory, admin, customer_id, "ADJUSTMENT_CREDIT", "20", key=key)
    before = snapshot(sqlite_factory, customer_id)

    with pytest.raises(AdjustmentConflict) as conflict:
        adjust(sqlite_factory, admin, customer_id, "ADJUSTMENT_CREDIT", "21", key=key)
    assert (conflict.value.code, conflict.value.http_status) == ("ADJUSTMENT_CONFLICT", 409)

    # 请求校验已经挡住的东西，万一漏到 repository（规则漂移），也是 422、只有问题码。
    drifted = [
        ("ADJUSTMENT_CREDIT", "0.000000001", REASON, "AMOUNT_TOO_MANY_DECIMAL_PLACES"),
        ("ADJUSTMENT_CREDIT", "-5", REASON, "CREDIT_MUST_BE_POSITIVE"),
        ("TOPUP", "5", REASON, "REFERENCE_TYPE_MISMATCH"),
        ("ADJUSTMENT_CREDIT", "5", "   ", "REASON_REQUIRED"),
    ]
    for kind, amount, reason, problem in drifted:
        with pytest.raises(AdjustmentRejected) as rejected:
            adjust(sqlite_factory, admin, customer_id, kind, amount, reason=reason)
        error = rejected.value
        assert (error.code, error.http_status) == ("VALIDATION_ERROR", 422)
        assert error.message == f"Invalid adjustment: {problem}"

    with pytest.raises(CustomerNotFound):
        adjust(sqlite_factory, admin, str(uuid.uuid4()), "ADJUSTMENT_CREDIT", "20")

    assert snapshot(sqlite_factory, customer_id) == before


def test_a_customer_without_a_wallet_is_an_unexpected_error(sqlite_factory) -> None:
    """数据不一致：`WalletNotFound` 原样抛出，由全局处理器按 500 返回。"""
    admin = make_admin(sqlite_factory)
    customer_id = make_customer(sqlite_factory, admin)
    with sqlite_factory() as session:
        session.execute(delete(Wallet))
        session.commit()

    with pytest.raises(WalletNotFound):
        adjust(sqlite_factory, admin, customer_id, "ADJUSTMENT_CREDIT", "20")

    with sqlite_factory() as session:
        assert _count(session, WalletTransaction) == 0
        assert _count(session, DomainOutbox) == 0


# --- 真 MySQL：触发器、跨零、余额范围、并发 --------------------------------------


def test_postings_move_the_real_wallet_and_it_verifies(mysql_factory) -> None:
    """正常路径与真实触发器：+20 再 -5.12345678，钱包 14.87654322、版本 2。"""
    admin = make_admin(mysql_factory)
    customer_id = make_customer(mysql_factory, admin)

    credit = adjust(mysql_factory, admin, customer_id, "ADJUSTMENT_CREDIT", "20")
    debit = adjust(mysql_factory, admin, customer_id, "ADJUSTMENT_DEBIT", "-5.12345678")

    assert (credit.balance_before, credit.balance_after, credit.wallet_sequence) == (
        "0.00000000",
        "20.00000000",
        1,
    )
    assert (debit.amount, debit.balance_before, debit.balance_after, debit.wallet_sequence) == (
        "-5.12345678",
        "20.00000000",
        "14.87654322",
        2,
    )
    state = snapshot(mysql_factory, customer_id)
    assert state["wallet"] == (Decimal("14.87654322"), 2)
    assert (state["billing_status"], state["status_version"]) == (BillingStatus.ACTIVE, 1)
    assert verified(mysql_factory, customer_id) == []

    trail = audits(mysql_factory, AuditAction.WALLET_ADJUSTMENT_POSTED)
    assert [audit.entity_id for audit in trail] == [credit.id, debit.id]
    for audit in trail:
        assert (audit.actor_user_id, audit.actor_role) == (admin.id, "ADMIN")
        assert (audit.ip_address, audit.user_agent) == (CONTEXT.ip_address, CONTEXT.user_agent)
        assert audit.reason == REASON
        assert audit.created_at == NOW
    assert json.loads(trail[1].before_state or "{}") == {"balance": "20.00000000"}
    assert json.loads(trail[1].after_state or "{}") == {"balance": "14.87654322"}


def test_a_credit_across_zero_resumes_the_customer(mysql_factory) -> None:
    """余额 -5、SUSPENDED，记 +20：余额 15、ACTIVE、版本 +1，一条跃迁审计、一条事件。"""
    admin = make_admin(mysql_factory)
    customer_id = make_customer(mysql_factory, admin)
    debit = adjust(mysql_factory, admin, customer_id, "ADJUSTMENT_DEBIT", "-5")
    # 0 → -5 仍是 SUSPENDED，不是跃迁。
    assert (debit.billing_status, debit.status_version) == ("SUSPENDED", 0)
    assert audits(mysql_factory, AuditAction.TENANT_BILLING_STATUS_CHANGED) == []

    credit = adjust(mysql_factory, admin, customer_id, "ADJUSTMENT_CREDIT", "20")

    assert (credit.balance_after, credit.billing_status, credit.status_version) == (
        "15.00000000",
        "ACTIVE",
        1,
    )
    state = snapshot(mysql_factory, customer_id)
    assert state["wallet"] == (Decimal("15"), 2)
    assert (state["billing_status"], state["status_version"]) == (BillingStatus.ACTIVE, 1)
    [transition] = audits(mysql_factory, AuditAction.TENANT_BILLING_STATUS_CHANGED, customer_id)
    assert (transition.actor_user_id, transition.actor_role) == (None, "SYSTEM")
    # 跃迁的操作者是系统，不带请求的 ip 与 user agent。
    assert (transition.ip_address, transition.user_agent) == (None, None)
    assert transition.reason == REASON_BALANCE_POSITIVE
    assert transition.created_at == NOW
    assert json.loads(transition.before_state or "{}") == {
        "billing_status": "SUSPENDED",
        "status_version": 0,
    }
    assert json.loads(transition.after_state or "{}")["wallet_transaction"] == credit.id
    [event] = status_events(mysql_factory, customer_id)
    assert event == {
        "billing_status": "ACTIVE",
        "status_version": 1,
        "reason": REASON_BALANCE_POSITIVE,
        "balance": "15.00000000",
        "wallet_transaction": credit.id,
    }
    assert verified(mysql_factory, customer_id) == []


def test_a_debit_to_exactly_zero_suspends_the_customer(mysql_factory) -> None:
    """余额 15、ACTIVE，记 -15：余额 0、SUSPENDED，一次跃迁。"""
    admin = make_admin(mysql_factory)
    customer_id = make_customer(mysql_factory, admin)
    adjust(mysql_factory, admin, customer_id, "ADJUSTMENT_CREDIT", "15")
    assert snapshot(mysql_factory, customer_id)["billing_status"] is BillingStatus.ACTIVE

    debit = adjust(mysql_factory, admin, customer_id, "ADJUSTMENT_DEBIT", "-15")

    assert (debit.balance_after, debit.billing_status, debit.status_version) == (
        "0.00000000",
        "SUSPENDED",
        2,
    )
    state = snapshot(mysql_factory, customer_id)
    assert state["wallet"] == (Decimal("0"), 2)
    assert (state["billing_status"], state["status_version"]) == (BillingStatus.SUSPENDED, 2)
    trail = audits(mysql_factory, AuditAction.TENANT_BILLING_STATUS_CHANGED, customer_id)
    assert [audit.reason for audit in trail] == [
        REASON_BALANCE_POSITIVE,
        REASON_BALANCE_NON_POSITIVE,
    ]
    versions = [event["status_version"] for event in status_events(mysql_factory, customer_id)]
    assert versions == [1, 2]
    assert verified(mysql_factory, customer_id) == []


def test_a_balance_beyond_decimal_20_8_is_refused_and_writes_nothing(mysql_factory) -> None:
    admin = make_admin(mysql_factory)
    customer_id = make_customer(mysql_factory, admin)
    adjust(mysql_factory, admin, customer_id, "ADJUSTMENT_CREDIT", "999999999999")
    before = snapshot(mysql_factory, customer_id)

    with pytest.raises(BalanceOutOfRange) as raised:
        adjust(mysql_factory, admin, customer_id, "BONUS", "1")

    assert (raised.value.code, raised.value.http_status) == ("BALANCE_OUT_OF_RANGE", 422)
    assert snapshot(mysql_factory, customer_id) == before
    assert before["wallet"] == (Decimal("999999999999"), 1)


def test_eight_concurrent_requests_with_one_key_post_once(mysql_factory) -> None:
    """INV-11：8 个线程、各自的会话，同一个键同一载荷同时调账。

    恰好一个入账，其余都是重放；撞唯一约束的那几个由服务层重试一次，没有 500。
    """
    admin = make_admin(mysql_factory)
    customer_id = make_customer(mysql_factory, admin)
    key = str(uuid.uuid4())
    start = threading.Barrier(8)

    def attempt(_index: int) -> AdjustmentView:
        start.wait(timeout=30)
        return adjust(mysql_factory, admin, customer_id, "ADJUSTMENT_CREDIT", "20", key=key)

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(attempt, range(8)))

    assert sorted(result.replayed for result in results) == [False] + [True] * 7
    assert len({result.id for result in results}) == 1
    state = snapshot(mysql_factory, customer_id)
    assert state["ledger"] == 1
    assert state["adjustment_audits"] == 1
    assert state["status_audits"] == 1
    assert state["wallet"] == (Decimal("20"), 1)
    assert verified(mysql_factory, customer_id) == []

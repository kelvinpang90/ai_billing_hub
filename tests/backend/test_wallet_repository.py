"""Wallet repository against a real MySQL (design gate #88 v6 §7).

⚠️ **这个文件只能对着真 MySQL 跑。**钱包的余额与版本由迁移 0006 的触发器推进，
SQLite 上根本没有那些触发器 —— 在内存库上记一笔账，钱包连动都不会动。所以设计
§7 规定：凡调用 `post_transaction` / `create_wallet` / `verify_wallet` 的场景一律在
这里跑；纯函数在 `test_wallet_rules.py`。

需要 `BILLING_TEST_DATABASE_URL` 指向一个**可以被清空的**库。CI 的 backend job 设了
它、先打开 `log_bin_trust_function_creators`，并把任何 skipped 判成失败。本地没设时
整个文件 skip —— **不要把 skipped 读成 passed**。
"""

from __future__ import annotations

import datetime as dt
import json
import os
import threading
import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from itertools import pairwise

import pytest
from alembic.config import Config
from sqlalchemy import Engine, create_engine, delete, insert, select, text, update
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from alembic import command
from app.core.database import create_session_factory, session_scope
from app.models.auth import (
    AuditAction,
    AuditLog,
    DomainOutbox,
    OutboxStatus,
    User,
    UserRole,
    UserStatus,
)
from app.models.tenancy import BillingStatus, Project, Tenant
from app.models.wallet import (
    REFERENCE_TYPE_FOR,
    ReferenceType,
    TransactionType,
    Wallet,
    WalletTransaction,
)
from app.repositories.tenancy import create_tenant, get_tenant_by_public_id
from app.repositories.wallet import (
    BALANCE_NOT_LEDGER_SUM,
    BILLING_STATUS_MISMATCH,
    EVENT_BILLING_STATUS_CHANGED,
    EVENT_LOW_BALANCE,
    LEDGER_MISSING,
    MAX_PAGE_SIZE,
    REASON_BALANCE_NON_POSITIVE,
    REASON_BALANCE_POSITIVE,
    WALLET_MISSING,
    InvalidAmount,
    InvalidTransaction,
    LedgerConflict,
    PostResult,
    WalletNotFound,
    billing_status_for,
    create_wallet,
    get_wallet_for_tenant,
    list_transactions_for_tenant,
    post_transaction,
    verify_wallet,
)

TEST_DATABASE_URL = os.environ.get("BILLING_TEST_DATABASE_URL", "")

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="BILLING_TEST_DATABASE_URL is not set; the wallet ledger needs a real MySQL",
)

# 固定值而不是 utc_now()：断言「存进去的就是调用方给的那个时刻」要能逐字比对。
NOW = dt.datetime(2026, 9, 19, 8, 30, 0)
TEST_EMAIL_DOMAIN = "@wallet-test.example.com"

TOPUP = TransactionType.TOPUP
AI_USAGE = TransactionType.AI_USAGE

# MySQL 错误号：SIGNAL SQLSTATE '45000' 报 1644，CHECK 违反报 3819，缺父行报 1452。
SIGNALLED = 1644
CHECK_VIOLATED = 3819
NO_PARENT_ROW = 1452


@pytest.fixture
def engine(monkeypatch) -> Iterator[Engine]:
    # ⚠️ 建表必须走 alembic（理由见 test_password_reset_concurrency.py 的同一段）。
    # 何况触发器只有迁移会建：create_all 建出来的表上根本没有它们。
    from app.core.config import get_settings

    monkeypatch.setenv("BILLING_DATABASE_URL", TEST_DATABASE_URL)
    get_settings.cache_clear()
    command.upgrade(Config("alembic.ini"), "head")
    get_settings.cache_clear()

    database = create_engine(TEST_DATABASE_URL, pool_size=24, max_overflow=8)
    _clean(database)
    yield database
    _clean(database)
    database.dispose()


@pytest.fixture
def factory(engine: Engine) -> sessionmaker[Session]:
    return create_session_factory(engine)


def _clean(engine: Engine) -> None:
    """⚠️ 库是共享的，每个用例前后都清场。

    账本拒绝 DELETE（那正是触发器要保证的），所以只能 TRUNCATE —— 它是 DDL、不经
    触发器，也就是设计 §10 记的残余风险。测试库能这样清场，本身就说明了为什么生产上
    要拆分迁移账号与运行账号。
    """
    with engine.begin() as connection:
        connection.execute(text("TRUNCATE TABLE wallet_transactions"))
    actions = [
        AuditAction.WALLET_ADJUSTMENT_POSTED,
        AuditAction.TENANT_BILLING_STATUS_CHANGED,
    ]
    with engine.begin() as connection:
        connection.execute(delete(Wallet))
        connection.execute(delete(Project))
        connection.execute(delete(Tenant))
        connection.execute(delete(AuditLog).where(AuditLog.action.in_(actions)))
        connection.execute(delete(DomainOutbox).where(DomainOutbox.aggregate_type == "tenant"))
        connection.execute(delete(User).where(User.email.like(f"%{TEST_EMAIL_DOMAIN}")))


# --- 帮手 -----------------------------------------------------------------------


def make_tenant(factory, *, threshold: str | None = None, wallet: bool = True) -> int:
    with factory() as session:
        tenant = create_tenant(
            session,
            company_name="Wallet Test Sdn Bhd",
            email="ops@example.com",
            now=NOW,
        )
        if threshold is not None:
            tenant.low_balance_threshold = Decimal(threshold)
        if wallet:
            create_wallet(session, tenant_id=tenant.id, now=NOW)
        session.commit()
        return int(tenant.id)


def make_admin(factory) -> int:
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
        return int(user.id)


def post_in(
    session: Session,
    tenant_id: int,
    kind: TransactionType,
    amount: str,
    **options: object,
) -> PostResult:
    """One posting in the caller's transaction; the source type follows the type."""
    options.setdefault("reference_type", REFERENCE_TYPE_FOR[kind])
    options.setdefault("reference_id", str(uuid.uuid4()))
    return post_transaction(
        session,
        tenant_id=tenant_id,
        transaction_type=kind,
        amount=Decimal(amount),
        now=NOW,
        **options,
    )


def post(factory, tenant_id: int, kind: TransactionType, amount: str, **options) -> PostResult:
    """One posting in its own transaction, committed."""
    with factory() as session:
        result = post_in(session, tenant_id, kind, amount, **options)
        session.commit()
        return result


def state(factory, tenant_id: int) -> tuple[Decimal, int, BillingStatus, int]:
    """(balance, wallet version, billing_status, status_version), as committed."""
    with factory() as session:
        wallet = get_wallet_for_tenant(session, tenant_id)
        tenant = session.get(Tenant, tenant_id)
        assert wallet is not None and tenant is not None
        return wallet.balance, wallet.version, tenant.billing_status, tenant.status_version


def public_id_of(factory, tenant_id: int) -> str:
    with factory() as session:
        tenant = session.get(Tenant, tenant_id)
        assert tenant is not None
        return tenant.public_id


def wallet_id_of(factory, tenant_id: int) -> int:
    with factory() as session:
        wallet = get_wallet_for_tenant(session, tenant_id)
        assert wallet is not None
        return int(wallet.id)


def ledger_rows(factory, tenant_id: int) -> list[WalletTransaction]:
    """Oldest first."""
    statement = (
        select(WalletTransaction)
        .where(WalletTransaction.tenant_id == tenant_id)
        .order_by(WalletTransaction.wallet_sequence)
    )
    with factory() as session:
        return list(session.execute(statement).scalars())


def audits(factory, action: AuditAction, entity_id: str | None = None) -> list[AuditLog]:
    statement = select(AuditLog).where(AuditLog.action == action)
    if entity_id is not None:
        statement = statement.where(AuditLog.entity_id == entity_id)
    with factory() as session:
        return list(session.execute(statement.order_by(AuditLog.id)).scalars())


def status_audits(factory, public_id: str) -> list[AuditLog]:
    return audits(factory, AuditAction.TENANT_BILLING_STATUS_CHANGED, public_id)


def events(factory, event_type: str, public_id: str) -> list[dict[str, object]]:
    """Payloads of one tenant's outbox rows of one type, oldest first."""
    statement = (
        select(DomainOutbox)
        .where(DomainOutbox.event_type == event_type)
        .where(DomainOutbox.aggregate_id == public_id)
        .order_by(DomainOutbox.id)
    )
    with factory() as session:
        rows = list(session.execute(statement).scalars())
    for row in rows:
        # 持久化的意图：PENDING、立刻可投、一次都没试过（INV-14）。
        assert row.status is OutboxStatus.PENDING
        assert row.attempt_count == 0
        assert row.next_retry_at == NOW
        assert row.aggregate_type == "tenant"
    return [json.loads(row.payload_json or "{}") for row in rows]


def status_events(factory, public_id: str) -> list[dict[str, object]]:
    return events(factory, EVENT_BILLING_STATUS_CHANGED, public_id)


def raw_row(wallet_id: int, tenant_id: int, **overrides: object) -> dict[str, object]:
    """Values for a direct INSERT that extends an empty wallet by +10, unless overridden."""
    values: dict[str, object] = {
        "public_id": str(uuid.uuid4()),
        "wallet_id": wallet_id,
        "tenant_id": tenant_id,
        "wallet_sequence": 1,
        "transaction_type": "TOPUP",
        "amount": Decimal("10"),
        "balance_before": Decimal("0"),
        "balance_after": Decimal("10"),
        "reference_type": "PAYMENT",
        "reference_id": str(uuid.uuid4()),
        "created_at": NOW,
    }
    values.update(overrides)
    return values


def raw_wallet(tenant_id: int, **overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "tenant_id": tenant_id,
        "currency": "MYR",
        "balance": Decimal("0"),
        "version": 0,
        "created_at": NOW,
        "updated_at": NOW,
    }
    values.update(overrides)
    return values


def errno_of(error: DBAPIError) -> int:
    return int(error.orig.args[0])


def refused(engine: Engine, statement) -> int:
    """Run `statement` around the repository; return the MySQL error number it got."""
    with pytest.raises(DBAPIError) as raised, engine.begin() as connection:
        connection.execute(statement)
    return errno_of(raised.value)


def refused_insert(engine: Engine, values: dict[str, object]) -> int:
    return refused(engine, insert(WalletTransaction).values(**values))


# --- 建钱包与正常路径 ------------------------------------------------------------


def test_a_new_tenant_starts_suspended_with_an_empty_wallet(factory) -> None:
    """余额为 0 按 spec §7 第 10 条就是暂停；充值使余额 > 0 之后才变 ACTIVE。"""
    tenant_id = make_tenant(factory)

    assert state(factory, tenant_id) == (Decimal("0"), 0, BillingStatus.SUSPENDED, 0)
    with factory() as session:
        wallet = get_wallet_for_tenant(session, tenant_id)
        assert wallet is not None
        assert wallet.currency == "MYR"
        assert verify_wallet(session, tenant_id) == []


def test_a_tenant_gets_one_wallet_only(factory) -> None:
    tenant_id = make_tenant(factory)

    with factory() as session, pytest.raises(IntegrityError):
        create_wallet(session, tenant_id=tenant_id, now=NOW)


def test_posting_needs_a_wallet(factory) -> None:
    tenant_id = make_tenant(factory, wallet=False)

    with pytest.raises(WalletNotFound):
        post(factory, tenant_id, TOPUP, "10")

    with factory() as session:
        assert get_wallet_for_tenant(session, tenant_id) is None
        assert verify_wallet(session, tenant_id) == [WALLET_MISSING]


def test_the_happy_path_chains_rows_and_advances_the_wallet(factory) -> None:
    tenant_id = make_tenant(factory)
    admin_id = make_admin(factory)

    post(factory, tenant_id, TOPUP, "100")
    post(factory, tenant_id, AI_USAGE, "-0.12345678")
    post(
        factory,
        tenant_id,
        TransactionType.ADJUSTMENT_DEBIT,
        "-50",
        created_by=admin_id,
        description="Refund paid outside the system",
    )

    balance, version, _, _ = state(factory, tenant_id)
    assert balance == Decimal("49.87654322")
    # DECIMAL(20,8) 往返：写进去再读出来，值与小数位都不变（T0.4 记下的那条要求）。
    assert balance.as_tuple().exponent == -8
    assert version == 3
    rows = ledger_rows(factory, tenant_id)
    assert [row.wallet_sequence for row in rows] == [1, 2, 3]
    assert rows[0].balance_before == 0
    for previous, current in pairwise(rows):
        assert current.balance_before == previous.balance_after
    for row in rows:
        assert row.balance_after == row.balance_before + row.amount
    assert rows[-1].balance_after == balance
    with factory() as session:
        assert verify_wallet(session, tenant_id) == []


def test_a_debit_may_take_the_balance_negative(factory) -> None:
    """spec §7 第 7 条：异步处理可能把余额扣成负数，这是接受的对账结果。"""
    tenant_id = make_tenant(factory)
    post(factory, tenant_id, TOPUP, "10")

    result = post(factory, tenant_id, AI_USAGE, "-15")

    assert result.balance == Decimal("-5")
    assert state(factory, tenant_id)[0] == Decimal("-5")


# --- 金额与类型：在写入之前被拒 ----------------------------------------------------


def test_the_smallest_amounts_post_and_bad_ones_write_nothing(factory) -> None:
    tenant_id = make_tenant(factory)

    post(factory, tenant_id, TOPUP, "0.00000001")
    post(factory, tenant_id, AI_USAGE, "-0.00000001")

    bad = [
        Decimal("0.000000001"),
        Decimal("0"),
        Decimal("NaN"),
        Decimal("Infinity"),
        Decimal("1000000000000"),
        0.5,
        1,
        "1",
    ]
    for amount in bad:
        with factory() as session, pytest.raises(InvalidAmount):
            post_transaction(
                session,
                tenant_id=tenant_id,
                transaction_type=TOPUP,
                amount=amount,
                reference_type=ReferenceType.PAYMENT,
                reference_id=str(uuid.uuid4()),
                now=NOW,
            )

    amounts = [row.amount for row in ledger_rows(factory, tenant_id)]
    assert amounts == [Decimal("0.00000001"), Decimal("-0.00000001")]
    assert state(factory, tenant_id)[:2] == (Decimal("0"), 2)


def test_a_balance_beyond_decimal_20_8_is_refused_before_writing(factory) -> None:
    tenant_id = make_tenant(factory)
    post(factory, tenant_id, TOPUP, "999999999999")

    with pytest.raises(InvalidAmount):
        post(factory, tenant_id, TOPUP, "1")

    assert state(factory, tenant_id)[:2] == (Decimal("999999999999"), 1)


@pytest.mark.parametrize(
    ("kind", "amount", "source"),
    [
        (TOPUP, "-10", ReferenceType.PAYMENT),
        (AI_USAGE, "10", ReferenceType.USAGE_EVENT),
        (AI_USAGE, "-10", ReferenceType.PAYMENT),
        (TransactionType.REFUND_ADJUSTMENT, "10", ReferenceType.ADMIN_ADJUSTMENT),
    ],
    ids=str,
)
def test_the_repository_refuses_mismatched_type_sign_or_source(
    factory, kind: TransactionType, amount: str, source: ReferenceType
) -> None:
    tenant_id = make_tenant(factory)
    admin_id = make_admin(factory)

    with pytest.raises(InvalidTransaction):
        post(
            factory,
            tenant_id,
            kind,
            amount,
            reference_type=source,
            created_by=admin_id,
            description="Manual refund",
        )

    assert ledger_rows(factory, tenant_id) == []


# 用例里换成真实的管理员 id（外键）。
_ADMIN = "<admin>"
_DEBIT_10 = {"amount": Decimal("-10"), "balance_after": Decimal("-10")}
_ADJUSTMENT = {"transaction_type": "ADJUSTMENT_CREDIT", "reference_type": "ADMIN_ADJUSTMENT"}


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param(
            _DEBIT_10,
            id="TOPUP below zero",
        ),
        pytest.param(
            {"transaction_type": "AI_USAGE", "reference_type": "USAGE_EVENT"},
            id="AI_USAGE above zero",
        ),
        pytest.param(
            {**_DEBIT_10, "transaction_type": "AI_USAGE"},
            id="AI_USAGE on a PAYMENT",
        ),
        pytest.param(
            {
                **_ADJUSTMENT,
                "transaction_type": "REFUND_ADJUSTMENT",
                "description": "Manual refund",
                "created_by": _ADMIN,
            },
            id="REFUND_ADJUSTMENT above zero",
        ),
        pytest.param(
            {"balance_after": Decimal("11")},
            id="balance_after is not balance_before + amount",
        ),
        pytest.param(
            {**_ADJUSTMENT, "created_by": _ADMIN},
            id="adjustment without a reason",
        ),
        pytest.param(
            {**_ADJUSTMENT, "created_by": _ADMIN, "description": "   "},
            id="adjustment with a blank reason",
        ),
        pytest.param(
            {**_ADJUSTMENT, "description": "Goodwill credit"},
            id="adjustment without an actor",
        ),
        pytest.param(
            {"transaction_type": "SYSTEM_CORRECTION", "reference_type": "SYSTEM"},
            id="system correction without a reason",
        ),
    ],
)
def test_the_database_refuses_rows_that_break_a_check(factory, engine, overrides) -> None:
    """设计 §2 的 CHECK：绕过 repository 直接插入，照样被数据库拒绝。

    每一行都「接在钱包之后」（前余额 0、序号 1、租户一致），过得了触发器，
    所以拒绝它的只能是 CHECK。
    """
    tenant_id = make_tenant(factory)
    wallet_id = wallet_id_of(factory, tenant_id)
    values = raw_row(wallet_id, tenant_id, **overrides)
    if values.get("created_by") == _ADMIN:
        values["created_by"] = make_admin(factory)

    assert refused_insert(engine, values) == CHECK_VIOLATED
    assert ledger_rows(factory, tenant_id) == []
    assert state(factory, tenant_id)[:2] == (Decimal("0"), 0)


def test_the_database_refuses_a_wallet_in_another_currency(factory, engine) -> None:
    """spec §5：V1 只有 MYR。"""
    tenant_id = make_tenant(factory, wallet=False)
    statement = insert(Wallet).values(**raw_wallet(tenant_id, currency="USD"))

    assert refused(engine, statement) == CHECK_VIOLATED


# --- 并发与幂等 ----------------------------------------------------------------------


def test_twenty_writers_lose_no_update(factory) -> None:
    """设计 §7「并发」：20 个线程、各自的会话，对同一钱包各记 10 笔不同来源的借贷。"""
    tenant_id = make_tenant(factory)

    def writer(index: int) -> Decimal:
        total = Decimal("0")
        for step in range(10):
            if step % 2 == 0:
                kind, amount = TOPUP, f"{index + 1}.5"
            else:
                kind, amount = AI_USAGE, "-0.12345678"
            post(factory, tenant_id, kind, amount)
            total += Decimal(amount)
        return total

    with ThreadPoolExecutor(max_workers=20) as pool:
        totals = list(pool.map(writer, range(20)))

    balance, version, _, _ = state(factory, tenant_id)
    assert balance == sum(totals, Decimal("0"))
    assert version == 200
    rows = ledger_rows(factory, tenant_id)
    assert [row.wallet_sequence for row in rows] == list(range(1, 201))
    with factory() as session:
        assert verify_wallet(session, tenant_id) == []


def test_the_same_source_twice_posts_once(factory) -> None:
    """INV-2：同一 `(USAGE_EVENT, e1)` 记两次，只有一行，余额只变一次。"""
    tenant_id = make_tenant(factory)

    first = post(factory, tenant_id, AI_USAGE, "-5", reference_id="usage-e1")
    second = post(factory, tenant_id, AI_USAGE, "-5", reference_id="usage-e1")

    assert (first.replayed, second.replayed) == (False, True)
    assert second.transaction.public_id == first.transaction.public_id
    assert second.balance == Decimal("-5")
    assert len(ledger_rows(factory, tenant_id)) == 1
    assert state(factory, tenant_id)[0] == Decimal("-5")


def test_the_same_source_from_eight_threads_posts_once(factory) -> None:
    tenant_id = make_tenant(factory)

    def attempt(_index: int) -> PostResult:
        # 锁被绕过时由唯一约束兜底：调用方回滚后重试，走重放分支（设计 §5）。
        for _retry in range(3):
            try:
                return post(factory, tenant_id, AI_USAGE, "-5", reference_id="usage-e2")
            except IntegrityError:
                continue
        raise AssertionError("still colliding on the unique constraint after retries")

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(attempt, range(8)))

    assert sorted(result.replayed for result in results) == [False] + [True] * 7
    assert len({result.transaction.public_id for result in results}) == 1
    assert len(ledger_rows(factory, tenant_id)) == 1
    assert state(factory, tenant_id)[0] == Decimal("-5")


def test_the_same_source_with_a_different_payload_conflicts(factory) -> None:
    """INV-11：同一来源、金额或类型不同 → LedgerConflict，不写入。"""
    tenant_id = make_tenant(factory)
    admin_id = make_admin(factory)
    adjustment = {
        "created_by": admin_id,
        "description": "Goodwill credit",
        "reference_id": "adjustment-1",
    }
    post(factory, tenant_id, AI_USAGE, "-5", reference_id="usage-e3")
    post(factory, tenant_id, TransactionType.ADJUSTMENT_CREDIT, "5", **adjustment)

    with pytest.raises(LedgerConflict):
        post(factory, tenant_id, AI_USAGE, "-6", reference_id="usage-e3")
    with pytest.raises(LedgerConflict):
        post(factory, tenant_id, TransactionType.ADJUSTMENT_DEBIT, "-5", **adjustment)

    assert len(ledger_rows(factory, tenant_id)) == 2
    assert state(factory, tenant_id)[:2] == (Decimal("0"), 2)


def test_another_tenant_cannot_reuse_a_source(factory) -> None:
    """INV-8 + INV-11：别的租户用同一来源是冲突而不是重放，也拿不到对方的行。"""
    first = make_tenant(factory)
    second = make_tenant(factory)
    post(factory, first, AI_USAGE, "-5", reference_id="usage-shared")

    with pytest.raises(LedgerConflict):
        post(factory, second, AI_USAGE, "-5", reference_id="usage-shared")

    assert state(factory, first)[0] == Decimal("-5")
    assert state(factory, second)[:2] == (Decimal("0"), 0)
    assert ledger_rows(factory, second) == []


def test_sources_that_differ_only_in_case_or_trailing_space_are_distinct(factory) -> None:
    """INV-2：网关的支付 ID 区分大小写。按库默认排序规则比较，第二笔会被当重放吞掉。"""
    tenant_id = make_tenant(factory)

    results = [
        post(factory, tenant_id, TOPUP, "10", reference_id=reference_id)
        for reference_id in ("pi_3AbC", "pi_3abc", "pi_3AbC ")
    ]

    assert [result.replayed for result in results] == [False, False, False]
    assert len(ledger_rows(factory, tenant_id)) == 3
    assert state(factory, tenant_id)[0] == Decimal("30")


# --- 原子性（INV-13） ----------------------------------------------------------------


def _funded_and_watched(factory) -> tuple[int, str]:
    """A tenant at 30 with a low-balance threshold of 20: ACTIVE, status_version 1."""
    tenant_id = make_tenant(factory, threshold="20")
    post(factory, tenant_id, TOPUP, "30")
    return tenant_id, public_id_of(factory, tenant_id)


def _assert_untouched(factory, tenant_id: int, public_id: str) -> None:
    assert state(factory, tenant_id) == (Decimal("30"), 1, BillingStatus.ACTIVE, 1)
    assert len(ledger_rows(factory, tenant_id)) == 1
    assert len(status_audits(factory, public_id)) == 1
    assert len(status_events(factory, public_id)) == 1
    assert events(factory, EVENT_LOW_BALANCE, public_id) == []


def test_a_caller_rollback_leaves_no_trace(factory) -> None:
    """跨零、也跨低余额阈值的一笔：调用方回滚之后，账本、余额、状态、版本、审计、
    出站事件全部回到记账前。"""
    tenant_id, public_id = _funded_and_watched(factory)

    with factory() as session:
        result = post_in(session, tenant_id, AI_USAGE, "-35")
        # 先确认这些东西确实写进了事务 —— 否则「回滚后什么都没有」证明不了任何事。
        tenant = session.get(Tenant, tenant_id)
        assert tenant is not None
        assert tenant.billing_status is BillingStatus.SUSPENDED
        assert result.balance == Decimal("-5")
        session.rollback()

    _assert_untouched(factory, tenant_id, public_id)


def test_an_exception_after_the_flush_leaves_no_trace(factory) -> None:
    tenant_id, public_id = _funded_and_watched(factory)

    with pytest.raises(RuntimeError), session_scope(factory) as session:
        post_in(session, tenant_id, AI_USAGE, "-35")
        raise RuntimeError("the caller failed after the flush, before the commit")

    _assert_untouched(factory, tenant_id, public_id)


# --- 读取与核对 ------------------------------------------------------------------------


def test_reads_are_scoped_to_the_tenant(factory) -> None:
    """INV-8：用 A 的 tenant_id 列不出、核对不到 B 的流水。"""
    first = make_tenant(factory)
    second = make_tenant(factory)
    for amount in ("10", "20", "30"):
        post(factory, first, TOPUP, amount)
    post(factory, second, TOPUP, "99")

    with factory() as session:
        page = list_transactions_for_tenant(session, first, limit=2)
        assert [row.wallet_sequence for row in page] == [3, 2]
        rest = list_transactions_for_tenant(session, first, limit=2, before_sequence=2)
        assert [row.wallet_sequence for row in rest] == [1]
        assert all(row.tenant_id == first for row in page + rest)
        theirs = list_transactions_for_tenant(session, second, limit=50)
        assert [row.amount for row in theirs] == [Decimal("99")]
        assert verify_wallet(session, first) == []
        assert verify_wallet(session, second) == []


def test_a_page_is_capped_and_a_limit_below_one_is_refused(factory) -> None:
    tenant_id = make_tenant(factory)
    with factory() as session:
        for _index in range(MAX_PAGE_SIZE + 1):
            post_in(session, tenant_id, TOPUP, "1")
        session.commit()

    with factory() as session:
        page = list_transactions_for_tenant(session, tenant_id, limit=MAX_PAGE_SIZE + 50)
        assert len(page) == MAX_PAGE_SIZE
        assert page[0].wallet_sequence == MAX_PAGE_SIZE + 1
        for limit in (0, -1):
            with pytest.raises(ValueError):
                list_transactions_for_tenant(session, tenant_id, limit=limit)


def test_a_thousand_smallest_postings_add_up_exactly(factory) -> None:
    """INV-10：1,000 笔 0.00000001 的借贷之后，余额精确等于流水之和。

    全在同一个事务里记，顺带证明同一会话可以连续记账（设计 v5）。
    """
    tenant_id = make_tenant(factory)

    with factory() as session:
        for step in range(1000):
            if step % 5 in (0, 1, 3):
                post_in(session, tenant_id, TOPUP, "0.00000001")
            else:
                post_in(session, tenant_id, AI_USAGE, "-0.00000001")
        session.commit()

    balance, version, status, _ = state(factory, tenant_id)
    assert balance == Decimal("0.00000200")
    assert version == 1000
    assert status is BillingStatus.ACTIVE
    with factory() as session:
        assert verify_wallet(session, tenant_id) == []


def test_a_truncated_ledger_is_reported(factory, engine) -> None:
    """`TRUNCATE` 是 DDL，不经触发器（设计 §10 的残余风险）；核对必须发现它。"""
    tenant_id = make_tenant(factory)
    for amount in ("10", "20", "30"):
        post(factory, tenant_id, TOPUP, amount)

    with engine.begin() as connection:
        connection.execute(text("TRUNCATE TABLE wallet_transactions"))

    with factory() as session:
        problems = verify_wallet(session, tenant_id)
    assert LEDGER_MISSING in problems
    assert BALANCE_NOT_LEDGER_SUM in problems


def test_a_billing_status_out_of_step_with_the_balance_is_reported(factory, engine) -> None:
    tenant_id = make_tenant(factory)
    wrong = update(Tenant).where(Tenant.id == tenant_id).values(billing_status="ACTIVE")
    with engine.begin() as connection:
        connection.execute(wrong)

    with factory() as session:
        assert verify_wallet(session, tenant_id) == [BILLING_STATUS_MISMATCH]


# --- 数据库层的强制（触发器） ------------------------------------------------------------


def test_the_database_refuses_to_change_or_delete_a_ledger_row(factory, engine) -> None:
    """INV-5：已提交的账本行，UPDATE 与 DELETE 一律 SQLSTATE 45000。"""
    tenant_id = make_tenant(factory)
    post(factory, tenant_id, TOPUP, "10")
    target = post(factory, tenant_id, AI_USAGE, "-3", description="usage batch 1").transaction
    before = [(row.amount, row.description) for row in ledger_rows(factory, tenant_id)]

    this_row = WalletTransaction.id == target.id
    statements = [
        update(WalletTransaction).where(this_row).values(amount=Decimal("-1")),
        update(WalletTransaction).where(this_row).values(description="rewritten"),
        delete(WalletTransaction).where(this_row),
    ]
    for statement in statements:
        assert refused(engine, statement) == SIGNALLED

    after = [(row.amount, row.description) for row in ledger_rows(factory, tenant_id)]
    assert after == before
    with factory() as session:
        assert verify_wallet(session, tenant_id) == []


def test_the_database_refuses_rows_that_do_not_extend_the_wallet(factory, engine) -> None:
    """INV-4 的另一个方向：没有「占住幂等键却不影响余额」的孤立账本。"""
    tenant_id = make_tenant(factory)
    other_id = make_tenant(factory)
    wallet_id = wallet_id_of(factory, tenant_id)
    post(factory, tenant_id, TOPUP, "10")
    extends = {
        "wallet_sequence": 2,
        "balance_before": Decimal("10"),
        "amount": Decimal("5"),
        "balance_after": Decimal("15"),
    }
    stale = {**extends, "balance_before": Decimal("5"), "balance_after": Decimal("10")}
    skipped = {**extends, "wallet_sequence": 3}

    assert refused_insert(engine, raw_row(wallet_id, tenant_id, **stale)) == SIGNALLED
    assert refused_insert(engine, raw_row(wallet_id, tenant_id, **skipped)) == SIGNALLED
    # 触发器先于外键执行，所以是 45000；复合外键是第二道（1452）。
    wrong_tenant = refused_insert(engine, raw_row(wallet_id, other_id, **extends))
    assert wrong_tenant in {SIGNALLED, NO_PARENT_ROW}

    assert state(factory, tenant_id)[:2] == (Decimal("10"), 1)
    assert len(ledger_rows(factory, tenant_id)) == 1
    assert ledger_rows(factory, other_id) == []


def test_a_direct_row_that_extends_the_wallet_is_honoured(factory, engine) -> None:
    """不经 repository、但正确接续的一行：触发器照样推进钱包，余额与账本仍然一致；
    之后 repository 用同一来源记账，走重放或冲突分支。"""
    tenant_id = make_tenant(factory)
    wallet_id = wallet_id_of(factory, tenant_id)
    post(factory, tenant_id, TOPUP, "10")
    direct = raw_row(
        wallet_id,
        tenant_id,
        wallet_sequence=2,
        balance_before=Decimal("10"),
        amount=Decimal("5"),
        balance_after=Decimal("15"),
        reference_id="payment-direct",
    )

    with engine.begin() as connection:
        connection.execute(insert(WalletTransaction).values(**direct))

    assert state(factory, tenant_id)[:2] == (Decimal("15"), 2)
    with factory() as session:
        assert verify_wallet(session, tenant_id) == []
    replay = post(factory, tenant_id, TOPUP, "5", reference_id="payment-direct")
    assert replay.replayed
    assert replay.transaction.public_id == direct["public_id"]
    with pytest.raises(LedgerConflict):
        post(factory, tenant_id, TOPUP, "6", reference_id="payment-direct")
    assert state(factory, tenant_id)[:2] == (Decimal("15"), 2)


def test_two_direct_inserts_racing_for_one_slot_land_once(factory, engine) -> None:
    """后到的在触发器的行锁上等待；前一个提交后，它因前余额过时被 45000 拒绝。"""
    tenant_id = make_tenant(factory)
    wallet_id = wallet_id_of(factory, tenant_id)
    # 两行一模一样地「接在空钱包之后」：前余额 0、序号 1，只是来源不同。
    first_row = raw_row(wallet_id, tenant_id)
    second_row = raw_row(wallet_id, tenant_id)
    outcome: dict[str, BaseException | None] = {}

    def second_writer() -> None:
        try:
            with engine.begin() as connection:
                connection.execute(insert(WalletTransaction).values(**second_row))
            outcome["error"] = None
        except DBAPIError as error:
            outcome["error"] = error

    with engine.connect() as first:
        transaction = first.begin()
        first.execute(insert(WalletTransaction).values(**first_row))
        worker = threading.Thread(target=second_writer)
        worker.start()
        worker.join(1.5)
        assert worker.is_alive(), "the second insert should be waiting on the wallet row lock"
        transaction.commit()
    worker.join(30)

    assert not worker.is_alive()
    error = outcome["error"]
    assert isinstance(error, DBAPIError)
    assert errno_of(error) == SIGNALLED
    assert state(factory, tenant_id)[:2] == (Decimal("10"), 1)
    assert len(ledger_rows(factory, tenant_id)) == 1


def test_the_database_refuses_to_move_a_wallet_without_the_ledger(factory, engine) -> None:
    """INV-4：直接改余额、版本、租户、币种一律 45000；经 repository 记账照常。"""
    tenant_id = make_tenant(factory)
    other_id = make_tenant(factory)
    wallet_id = wallet_id_of(factory, tenant_id)
    post(factory, tenant_id, TOPUP, "10")

    this_wallet = update(Wallet).where(Wallet.id == wallet_id)
    statements = [
        this_wallet.values(balance=Wallet.balance + 1),
        this_wallet.values(version=Wallet.version + 1),
        this_wallet.values(tenant_id=other_id),
        this_wallet.values(currency="USD"),
    ]
    for statement in statements:
        assert refused(engine, statement) == SIGNALLED

    assert state(factory, tenant_id)[:2] == (Decimal("10"), 1)
    assert post(factory, tenant_id, AI_USAGE, "-4").balance == Decimal("6")
    with factory() as session:
        assert verify_wallet(session, tenant_id) == []


def test_a_wallet_can_only_start_empty(factory, engine) -> None:
    """设计 v6：余额只能经账本出现 —— 也挡住「删空钱包再带余额插回」。"""
    bare = make_tenant(factory, wallet=False)
    with_balance = insert(Wallet).values(**raw_wallet(bare, balance=Decimal("500")))
    with_version = insert(Wallet).values(**raw_wallet(bare, version=3))
    assert refused(engine, with_balance) == SIGNALLED
    assert refused(engine, with_version) == SIGNALLED

    # 没有账本的钱包可以删（回填与测试清理要用），删掉之后带余额插回照样被拒。
    emptied = make_tenant(factory)
    with engine.begin() as connection:
        connection.execute(delete(Wallet).where(Wallet.tenant_id == emptied))
    comeback = insert(Wallet).values(**raw_wallet(emptied, balance=Decimal("500")))
    assert refused(engine, comeback) == SIGNALLED

    # 有账本的钱包删不掉：外键 RESTRICT。
    funded = make_tenant(factory)
    post(factory, funded, TOPUP, "1")
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(delete(Wallet).where(Wallet.tenant_id == funded))

    # 余额与版本都是 0 的插入（create_wallet 与迁移回填走的就是它）照常成功。
    with engine.begin() as connection:
        connection.execute(insert(Wallet).values(**raw_wallet(bare)))
    assert state(factory, bare)[:2] == (Decimal("0"), 0)


# --- 调账（spec §8、§60） --------------------------------------------------------------


def test_adjustments_need_a_reason_and_an_actor(factory) -> None:
    tenant_id = make_tenant(factory)
    admin_id = make_admin(factory)
    credit = TransactionType.ADJUSTMENT_CREDIT
    attempts = [
        (credit, {"created_by": admin_id}),
        (credit, {"created_by": admin_id, "description": " \t "}),
        (credit, {"description": "Goodwill credit"}),
        (TransactionType.SYSTEM_CORRECTION, {}),
    ]

    for kind, options in attempts:
        with pytest.raises(InvalidTransaction):
            post(factory, tenant_id, kind, "5", **options)

    assert ledger_rows(factory, tenant_id) == []
    assert audits(factory, AuditAction.WALLET_ADJUSTMENT_POSTED) == []


def test_an_adjustment_and_its_audit_commit_or_vanish_together(factory) -> None:
    """调账与它的审计在同一个事务里；重放不再写审计。"""
    tenant_id = make_tenant(factory)
    admin_id = make_admin(factory)
    credit = TransactionType.ADJUSTMENT_CREDIT
    options = {
        "created_by": admin_id,
        "description": "Goodwill credit after outage",
        "actor_role": "ADMIN",
        "reference_id": "adjustment-request-1",
    }

    with factory() as session:
        post_in(session, tenant_id, credit, "25", **options)
        session.rollback()

    assert ledger_rows(factory, tenant_id) == []
    assert audits(factory, AuditAction.WALLET_ADJUSTMENT_POSTED) == []
    assert state(factory, tenant_id)[0] == Decimal("0")

    posted = post(factory, tenant_id, credit, "25", **options)
    replayed = post(factory, tenant_id, credit, "25", **options)

    assert replayed.replayed
    [audit] = audits(factory, AuditAction.WALLET_ADJUSTMENT_POSTED)
    assert audit.actor_user_id == admin_id
    assert audit.actor_role == "ADMIN"
    assert audit.entity_type == "wallet_transaction"
    assert audit.entity_id == posted.transaction.public_id
    assert audit.reason == "Goodwill credit after outage"
    assert json.loads(audit.before_state or "{}") == {"balance": "0.00000000"}
    assert json.loads(audit.after_state or "{}") == {"balance": "25.00000000"}
    assert audit.created_at == NOW


def test_a_system_correction_writes_its_audit_without_an_actor(factory) -> None:
    """系统更正：`created_by` 与审计的 `actor_user_id` 都为空，原因必填并进审计。"""
    tenant_id = make_tenant(factory)
    post(factory, tenant_id, TOPUP, "10")

    posted = post(
        factory,
        tenant_id,
        TransactionType.SYSTEM_CORRECTION,
        "-3",
        description="Reconciliation found a duplicated top-up",
        actor_role="SYSTEM",
    )

    assert posted.transaction.created_by is None
    [audit] = audits(factory, AuditAction.WALLET_ADJUSTMENT_POSTED)
    assert audit.actor_user_id is None
    assert audit.actor_role == "SYSTEM"
    assert audit.entity_type == "wallet_transaction"
    assert audit.entity_id == posted.transaction.public_id
    assert audit.reason == "Reconciliation found a duplicated top-up"
    assert json.loads(audit.before_state or "{}") == {"balance": "10.00000000"}
    assert json.loads(audit.after_state or "{}") == {"balance": "7.00000000"}
    assert state(factory, tenant_id)[0] == Decimal("7")


def test_the_adjustment_audit_carries_the_callers_ip_and_user_agent(factory) -> None:
    """AIH-TASK-011：ip 与 user agent 只进调账审计（user agent 截到 512 字符）；
    同一笔引起的计费状态跃迁，操作者是系统，它的审计不带这两项。"""
    tenant_id = make_tenant(factory)
    admin_id = make_admin(factory)
    public_id = public_id_of(factory, tenant_id)

    posted = post(
        factory,
        tenant_id,
        TransactionType.ADJUSTMENT_CREDIT,
        "25",
        created_by=admin_id,
        description="Goodwill credit after outage",
        actor_role="ADMIN",
        ip_address="203.0.113.7",
        user_agent="x" * 600,
    )

    [audit] = audits(factory, AuditAction.WALLET_ADJUSTMENT_POSTED)
    assert audit.entity_id == posted.transaction.public_id
    assert (audit.ip_address, audit.user_agent) == ("203.0.113.7", "x" * 512)
    # 余额 0 → 25 跨零：同一笔还写了一条跃迁审计。
    [transition] = status_audits(factory, public_id)
    assert transition.reason == REASON_BALANCE_POSITIVE
    assert (transition.ip_address, transition.user_agent) == (None, None)


def test_without_ip_and_user_agent_the_adjustment_audit_leaves_them_empty(factory) -> None:
    """两个参数默认 None：不传它们的调用方行为不变。"""
    tenant_id = make_tenant(factory)
    admin_id = make_admin(factory)

    post(
        factory,
        tenant_id,
        TransactionType.ADJUSTMENT_CREDIT,
        "25",
        created_by=admin_id,
        description="Goodwill credit after outage",
        actor_role="ADMIN",
    )

    [audit] = audits(factory, AuditAction.WALLET_ADJUSTMENT_POSTED)
    assert (audit.ip_address, audit.user_agent) == (None, None)


# --- 计费状态（spec §7 第 8–11 条） --------------------------------------------------


def test_crossing_zero_suspends_with_one_audit_and_one_event(factory) -> None:
    """余额 10、ACTIVE，记 -15：余额 -5、SUSPENDED、版本 +1、一条审计、一条事件。"""
    tenant_id = make_tenant(factory)
    public_id = public_id_of(factory, tenant_id)
    post(factory, tenant_id, TOPUP, "10")
    assert state(factory, tenant_id) == (Decimal("10"), 1, BillingStatus.ACTIVE, 1)

    result = post(factory, tenant_id, AI_USAGE, "-15")

    assert state(factory, tenant_id) == (Decimal("-5"), 2, BillingStatus.SUSPENDED, 2)
    trail = status_audits(factory, public_id)
    assert len(trail) == 2
    latest = trail[-1]
    assert latest.actor_user_id is None
    assert latest.actor_role == "SYSTEM"
    assert latest.reason == REASON_BALANCE_NON_POSITIVE
    before = json.loads(latest.before_state or "{}")
    assert before == {"billing_status": "ACTIVE", "status_version": 1}
    payloads = status_events(factory, public_id)
    assert len(payloads) == 2
    assert payloads[-1] == {
        "billing_status": "SUSPENDED",
        "status_version": 2,
        "reason": REASON_BALANCE_NON_POSITIVE,
        "balance": "-5.00000000",
        "wallet_transaction": result.transaction.public_id,
    }


def test_exactly_zero_stays_suspended(factory) -> None:
    """spec §7 第 10 条：余额正好为 0 仍是 SUSPENDED；再多 0.00000001 才恢复。"""
    tenant_id = make_tenant(factory)
    public_id = public_id_of(factory, tenant_id)
    post(factory, tenant_id, TOPUP, "10")

    post(factory, tenant_id, AI_USAGE, "-10")
    assert state(factory, tenant_id)[2:] == (BillingStatus.SUSPENDED, 2)
    post(factory, tenant_id, TOPUP, "0.00000001")
    assert state(factory, tenant_id)[2:] == (BillingStatus.ACTIVE, 3)

    changes = [(p["billing_status"], p["reason"]) for p in status_events(factory, public_id)]
    assert changes == [
        ("ACTIVE", REASON_BALANCE_POSITIVE),
        ("SUSPENDED", REASON_BALANCE_NON_POSITIVE),
        ("ACTIVE", REASON_BALANCE_POSITIVE),
    ]


def test_a_top_up_resumes_only_once_the_balance_is_above_zero(factory) -> None:
    """SUSPENDED、余额 -5：+3 之后仍暂停、没有新事件；再 +10 才恢复。"""
    tenant_id = make_tenant(factory)
    public_id = public_id_of(factory, tenant_id)
    post(factory, tenant_id, TOPUP, "10")
    post(factory, tenant_id, AI_USAGE, "-15")

    post(factory, tenant_id, TOPUP, "3")
    assert state(factory, tenant_id) == (Decimal("-2"), 3, BillingStatus.SUSPENDED, 2)
    assert len(status_events(factory, public_id)) == 2

    post(factory, tenant_id, TOPUP, "10")
    assert state(factory, tenant_id) == (Decimal("8"), 4, BillingStatus.ACTIVE, 3)
    payloads = status_events(factory, public_id)
    assert len(payloads) == 3
    assert payloads[-1]["billing_status"] == "ACTIVE"
    assert payloads[-1]["reason"] == REASON_BALANCE_POSITIVE


def test_debits_while_suspended_add_no_transition(factory) -> None:
    """spec §7 第 11 条：只在真正跃迁时发，暂停中继续扣费不产生任何新东西。"""
    tenant_id = make_tenant(factory)
    public_id = public_id_of(factory, tenant_id)
    post(factory, tenant_id, TOPUP, "10")
    post(factory, tenant_id, AI_USAGE, "-15")

    for _ in range(3):
        post(factory, tenant_id, AI_USAGE, "-1")

    assert state(factory, tenant_id) == (Decimal("-8"), 5, BillingStatus.SUSPENDED, 2)
    assert len(status_audits(factory, public_id)) == 2
    assert len(status_events(factory, public_id)) == 2


def test_low_balance_fires_only_on_the_way_down(factory) -> None:
    """阈值 20：50 → 25 → 15 → 10 → 30 → 18，恰好两条，对应 15 与 18；阈值为 NULL 时没有。"""
    watched = make_tenant(factory, threshold="20")
    unwatched = make_tenant(factory)
    steps = [
        (TOPUP, "50"),
        (AI_USAGE, "-25"),
        (AI_USAGE, "-10"),
        (AI_USAGE, "-5"),
        (TOPUP, "20"),
        (AI_USAGE, "-12"),
    ]

    results = [post(factory, watched, kind, amount) for kind, amount in steps]
    for kind, amount in steps:
        post(factory, unwatched, kind, amount)

    expected = [
        {
            "balance": balance,
            "threshold": "20.00000000",
            "wallet_transaction": results[index].transaction.public_id,
        }
        for index, balance in ((2, "15.00000000"), (5, "18.00000000"))
    ]
    assert events(factory, EVENT_LOW_BALANCE, public_id_of(factory, watched)) == expected
    assert events(factory, EVENT_LOW_BALANCE, public_id_of(factory, unwatched)) == []


def test_status_version_counts_every_transition_under_concurrency(factory) -> None:
    """多线程交替记借贷，让余额反复跨零：版本号等于按账本序号重算出的跃迁次数。"""
    tenant_id = make_tenant(factory)
    public_id = public_id_of(factory, tenant_id)

    def writer(index: int) -> None:
        for step in range(10):
            if (index + step) % 2 == 0:
                post(factory, tenant_id, TOPUP, "1")
            else:
                post(factory, tenant_id, AI_USAGE, "-1")

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(writer, range(8)))

    status = BillingStatus.SUSPENDED
    transitions = 0
    for row in ledger_rows(factory, tenant_id):
        wanted = billing_status_for(row.balance_after)
        if wanted is not status:
            transitions += 1
            status = wanted

    assert transitions > 0, "余额从没跨过零的话，这条用例什么也没测"
    assert state(factory, tenant_id)[2:] == (status, transitions)
    assert len(status_audits(factory, public_id)) == transitions
    versions = [p["status_version"] for p in status_events(factory, public_id)]
    assert versions == list(range(1, transitions + 1))


def test_a_stale_tenant_in_the_session_does_not_mislead_the_transition(factory) -> None:
    """设计 v5：会话 A 先读到 ACTIVE、余额 3；B 记 -8 并提交；A 再记 +10 并提交。

    ⚠️ 没有 `populate_existing=True` 的话，A 会把会话里那份 ACTIVE 当真：判定「不用
    跃迁」，于是余额 5 而状态停在 SUSPENDED。
    """
    tenant_id = make_tenant(factory)
    public_id = public_id_of(factory, tenant_id)
    post(factory, tenant_id, TOPUP, "3")

    with factory() as session:
        stale_tenant = get_tenant_by_public_id(session, public_id)
        stale_wallet = get_wallet_for_tenant(session, tenant_id)
        assert stale_tenant is not None and stale_wallet is not None
        assert stale_tenant.billing_status is BillingStatus.ACTIVE
        assert stale_wallet.balance == Decimal("3")

        post(factory, tenant_id, AI_USAGE, "-8")  # 另一个会话，已提交
        result = post_in(session, tenant_id, TOPUP, "10")
        session.commit()

    assert result.balance == Decimal("5")
    assert state(factory, tenant_id) == (Decimal("5"), 3, BillingStatus.ACTIVE, 3)
    versions = [p["status_version"] for p in status_events(factory, public_id)]
    assert versions == [1, 2, 3], "版本号在 B 的基础上 +1，没有重复"
    with factory() as session:
        assert verify_wallet(session, tenant_id) == []


def test_several_postings_in_one_transaction(factory) -> None:
    """设计 v5：同一会话、同一事务连记三笔（跨一次零线），最后一次提交，没有 45000。"""
    tenant_id = make_tenant(factory)
    public_id = public_id_of(factory, tenant_id)

    with factory() as session:
        results = [
            post_in(session, tenant_id, TOPUP, "5"),
            post_in(session, tenant_id, AI_USAGE, "-2"),
            post_in(session, tenant_id, AI_USAGE, "-1"),
        ]
        session.commit()

    assert [r.transaction.wallet_sequence for r in results] == [1, 2, 3]
    assert [r.balance for r in results] == [Decimal("5"), Decimal("3"), Decimal("2")]
    assert state(factory, tenant_id) == (Decimal("2"), 3, BillingStatus.ACTIVE, 1)
    assert len(status_audits(factory, public_id)) == 1
    with factory() as session:
        assert verify_wallet(session, tenant_id) == []

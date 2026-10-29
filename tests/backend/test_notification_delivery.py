"""Low balance and billing status events turned into portal notifications (design gate #471 v7).

设计 §7 里属于规则与投递的每一行：正常路径三种、同一笔两条、
内部计量租户、payload 无效、幂等重放、并发（仅 MySQL）、
旧领取者与新领取交错两种、事务中途失败、Redis/Celery 丢失、
金额精度（仅 MySQL）、生产积压与旧 payload。
规则本身（纯函数）另有不碰数据库的用例。

集成用例在 SQLite 与 MySQL 上参数化；MySQL 用 `BILLING_TEST_DATABASE_URL`，
没设时 skip —— **不要把 skipped 读成 passed**，CI 设了它并把 skip 判成失败。
SQLite 上没有钱包触发器，这里装一个与 test_usage_billing.py 同样的测试触发器，
让 `post_transaction` 推进钱包。

示例 uuid 全零；金额与公司名是虚构的。
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import re
import threading
import time
import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import pytest
from alembic.config import Config
from sqlalchemy import Engine, create_engine, delete, select, update
from sqlalchemy.orm import Session, sessionmaker

from alembic import command
from app.core.config import Settings
from app.core.database import create_session_factory
from app.models.auth import DomainOutbox, OutboxStatus
from app.models.base import Base
from app.models.notifications import Notification, NotificationType
from app.models.tenancy import BillingMode, Tenant
from app.models.wallet import REFERENCE_TYPE_FOR, TransactionType, Wallet, WalletTransaction
from app.repositories.tenancy import create_tenant
from app.repositories.wallet import (
    AGGREGATE_TENANT,
    EVENT_BILLING_STATUS_CHANGED,
    EVENT_LOW_BALANCE,
    REASON_BALANCE_NON_POSITIVE,
    REASON_BALANCE_POSITIVE,
    create_wallet,
    post_transaction,
)
from app.services.auth import utc_now
from app.services.notifications import (
    InvalidPayload,
    NoNotification,
    NotificationDraft,
    notification_for,
)
from app.tasks import outbox as outbox_task

TEST_DATABASE_URL = os.environ.get("BILLING_TEST_DATABASE_URL", "")
TEST_EMAIL_DOMAIN = "@notification-delivery-test.example.com"

# 固定的记账时刻：断言 `occurred_at` 取的是它，而不是处理时刻。
NOW = dt.datetime(2026, 10, 27, 5, 12, 9)
# 生产积压那两行的写入日期（设计 §1）。
BACKLOG_AT = dt.datetime(2026, 9, 24, 3, 15, 42)
ZERO_UUID = "00000000-0000-0000-0000-000000000000"
MAX_ATTEMPTS = 3

TOPUP = TransactionType.TOPUP
AI_USAGE = TransactionType.AI_USAGE

_UUID4 = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")

_SQLITE_LEDGER_TRIGGER = """
CREATE TRIGGER test_wallet_follows_ledger
AFTER INSERT ON wallet_transactions
FOR EACH ROW
BEGIN
    UPDATE wallets
       SET balance = NEW.balance_after, version = NEW.wallet_sequence,
           updated_at = NEW.created_at
     WHERE id = NEW.wallet_id;
END
"""

# 写入方 `post_transaction` 的两种 payload 形状（app/repositories/wallet.py）。
LOW = {"balance": "45.00000000", "threshold": "50.00000000", "wallet_transaction": ZERO_UUID}
STATUS = {
    "billing_status": "SUSPENDED",
    "billing_mode": "PREPAID",
    "effective_status": "SUSPENDED",
    "status_version": 2,
    "reason": REASON_BALANCE_NON_POSITIVE,
    "balance": "0.00000000",
    "wallet_transaction": ZERO_UUID,
}
INTERNAL = {**STATUS, "billing_mode": "INTERNAL_METERED_ONLY"}
# 生产积压的形状：迁移 0019 之前写的行，没有 `billing_mode` 与 `effective_status`。
LEGACY = {
    "balance": "0.00000000",
    "billing_status": "SUSPENDED",
    "reason": REASON_BALANCE_NON_POSITIVE,
    "status_version": 1,
    "wallet_transaction": ZERO_UUID,
}


def _without(payload: dict[str, object], key: str) -> dict[str, object]:
    return {name: value for name, value in payload.items() if name != key}


# 设计 §7「payload 无效」的全部列举值，另加契约要求的两种内部计量组合与几种边界写法。
# 每项：(event_type, payload)；payload 是 dict / list 时按 JSON 写入，是 str 时原样写入。
INVALID_PAYLOADS = {
    "low-missing-balance": (EVENT_LOW_BALANCE, _without(LOW, "balance")),
    "low-missing-threshold": (EVENT_LOW_BALANCE, _without(LOW, "threshold")),
    "exponent": (EVENT_LOW_BALANCE, {**LOW, "balance": "1e3"}),
    "nine-decimals": (EVENT_LOW_BALANCE, {**LOW, "balance": "45.123456789"}),
    "thirteen-digits": (EVENT_LOW_BALANCE, {**LOW, "balance": "1234567890123.00"}),
    "nan": (EVENT_LOW_BALANCE, {**LOW, "balance": "NaN"}),
    "infinity": (EVENT_LOW_BALANCE, {**LOW, "threshold": "Infinity"}),
    "number-not-string": (EVENT_LOW_BALANCE, {**LOW, "balance": 45}),
    "negative-threshold": (EVENT_LOW_BALANCE, {**LOW, "threshold": "-1.00"}),
    "padded": (EVENT_LOW_BALANCE, {**LOW, "balance": " 45.00"}),
    "trailing-newline": (EVENT_LOW_BALANCE, {**LOW, "balance": "45.00\n"}),
    "status-missing-balance": (EVENT_BILLING_STATUS_CHANGED, _without(STATUS, "balance")),
    "status-missing-status": (EVENT_BILLING_STATUS_CHANGED, _without(STATUS, "billing_status")),
    "unknown-billing-status": (
        EVENT_BILLING_STATUS_CHANGED,
        {**STATUS, "billing_status": "CLOSED"},
    ),
    "unknown-billing-mode": (EVENT_BILLING_STATUS_CHANGED, {**STATUS, "billing_mode": "POSTPAID"}),
    "null-billing-mode": (EVENT_BILLING_STATUS_CHANGED, {**STATUS, "billing_mode": None}),
    "number-billing-mode": (EVENT_BILLING_STATUS_CHANGED, {**STATUS, "billing_mode": 1}),
    "internal-missing-balance": (EVENT_BILLING_STATUS_CHANGED, _without(INTERNAL, "balance")),
    "internal-unknown-status": (EVENT_BILLING_STATUS_CHANGED, {**INTERNAL, "billing_status": "X"}),
    "array": (EVENT_BILLING_STATUS_CHANGED, [STATUS]),
    "not-json": (EVENT_LOW_BALANCE, "{not json"),
}


def _raw(payload: object) -> str | None:
    if payload is None or isinstance(payload, str):
        return payload
    return json.dumps(payload, sort_keys=True)


# --- 规则：纯函数，不碰数据库（unit） ----------------------------------------------------------


def rule(event_type: str, payload: object, *, aggregate_type: str = AGGREGATE_TENANT):
    return notification_for(
        event_type=event_type,
        aggregate_type=aggregate_type,
        payload_json=_raw(payload),
        created_at=NOW,
    )


def test_the_rule_turns_a_low_balance_event_into_a_draft() -> None:
    assert rule(EVENT_LOW_BALANCE, LOW) == NotificationDraft(
        NotificationType.LOW_BALANCE, Decimal("45.00000000"), Decimal("50.00000000"), NOW
    )


@pytest.mark.parametrize(
    ("billing_status", "expected", "balance"),
    [
        ("SUSPENDED", NotificationType.SUSPENDED, "-3.50000000"),
        ("ACTIVE", NotificationType.REACTIVATED, "5.00000000"),
    ],
)
def test_the_rule_turns_a_prepaid_transition_into_a_draft(
    billing_status: str, expected: NotificationType, balance: str
) -> None:
    payload = {**STATUS, "billing_status": billing_status, "balance": balance}

    assert rule(EVENT_BILLING_STATUS_CHANGED, payload) == NotificationDraft(
        expected, Decimal(balance), None, NOW
    )


def test_a_payload_without_a_billing_mode_counts_as_prepaid() -> None:
    """设计 §2：没有这个键的行写于迁移 0019 之前，那时所有租户都是预付。"""
    result = rule(EVENT_BILLING_STATUS_CHANGED, LEGACY)

    assert result == NotificationDraft(NotificationType.SUSPENDED, Decimal(0), None, NOW)


def test_an_internal_metered_transition_makes_no_notification() -> None:
    assert rule(EVENT_BILLING_STATUS_CHANGED, INTERNAL) == NoNotification()
    active = {**INTERNAL, "billing_status": "ACTIVE", "reason": REASON_BALANCE_POSITIVE}
    assert rule(EVENT_BILLING_STATUS_CHANGED, active) == NoNotification()


@pytest.mark.parametrize("case", list(INVALID_PAYLOADS))
def test_the_rule_rejects_an_invalid_payload(case: str) -> None:
    event_type, payload = INVALID_PAYLOADS[case]

    assert isinstance(rule(event_type, payload), InvalidPayload)


def test_the_rule_rejects_a_missing_payload_and_an_unknown_event() -> None:
    assert isinstance(rule(EVENT_LOW_BALANCE, None), InvalidPayload)
    assert isinstance(rule("tenant.account_status_changed", STATUS), InvalidPayload)


@pytest.mark.parametrize("event_type", [EVENT_LOW_BALANCE, EVENT_BILLING_STATUS_CHANGED])
def test_the_rule_rejects_an_aggregate_that_is_not_a_tenant(event_type: str) -> None:
    payload = LOW if event_type == EVENT_LOW_BALANCE else STATUS

    assert isinstance(rule(event_type, payload, aggregate_type="users"), InvalidPayload)


def test_the_rule_keeps_every_digit_without_float() -> None:
    payload = {**LOW, "balance": "999999999999.99999999", "threshold": "0.00000001"}

    result = rule(EVENT_LOW_BALANCE, payload)

    assert isinstance(result, NotificationDraft)
    assert format(result.balance, "f") == "999999999999.99999999"
    assert result.threshold is not None and format(result.threshold, "f") == "0.00000001"


# --- 数据库 -------------------------------------------------------------------------------


def _sqlite_factory() -> Iterator[sessionmaker[Session]]:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.exec_driver_sql(_SQLITE_LEDGER_TRIGGER)
    yield create_session_factory(engine)
    engine.dispose()


def _clean(engine: Engine) -> None:
    """⚠️ 库是共享的：每个用例前后清场。
    账本与审计拒绝 DELETE（触发器），所以 TRUNCATE。"""
    with engine.begin() as connection:
        connection.exec_driver_sql("SET FOREIGN_KEY_CHECKS = 0")
        try:
            for table in ("notifications", "wallet_transactions", "domain_outbox", "audit_logs"):
                connection.exec_driver_sql(f"TRUNCATE TABLE {table}")
        finally:
            connection.exec_driver_sql("SET FOREIGN_KEY_CHECKS = 1")
    with engine.begin() as connection:
        ours = select(Tenant.id).where(Tenant.email.like(f"%{TEST_EMAIL_DOMAIN}"))
        connection.execute(delete(Wallet).where(Wallet.tenant_id.in_(ours)))
        connection.execute(delete(Tenant).where(Tenant.email.like(f"%{TEST_EMAIL_DOMAIN}")))


def _mysql_factory(monkeypatch: pytest.MonkeyPatch) -> Iterator[sessionmaker[Session]]:
    if not TEST_DATABASE_URL:
        pytest.skip("BILLING_TEST_DATABASE_URL is not set; the MySQL half needs a real MySQL")
    # 建表走 alembic：钱包触发器只有迁移会建。
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


def _wire(monkeypatch: pytest.MonkeyPatch, factory: sessionmaker[Session]) -> None:
    """Point the task at this database; any call to `build_transport` fails the test.

    ⚠️ 前端基址留空：通知类事件不需要它（设计 §2「投递」）。
    """

    def no_transport(_settings: Settings) -> object:
        raise AssertionError("notification events must not build an email transport")

    settings = Settings(
        frontend_base_url="", outbox_retry_base_seconds=60, outbox_max_attempts=MAX_ATTEMPTS
    )
    monkeypatch.setattr(outbox_task, "_session_factory", lambda: factory)
    monkeypatch.setattr(outbox_task, "get_settings", lambda: settings)
    monkeypatch.setattr(outbox_task, "build_transport", no_transport)


@pytest.fixture(params=["sqlite", "mysql"])
def factory(request, monkeypatch) -> Iterator[sessionmaker[Session]]:
    """The task wired to SQLite, then to a real MySQL."""
    if request.param == "mysql":
        source = _mysql_factory(monkeypatch)
    else:
        source = _sqlite_factory()
    made = next(source)
    _wire(monkeypatch, made)
    yield made
    for _ in source:
        pass


@pytest.fixture
def mysql(monkeypatch) -> Iterator[sessionmaker[Session]]:
    """MySQL only: row locks and exact DECIMAL(20,8) round trips."""
    source = _mysql_factory(monkeypatch)
    made = next(source)
    _wire(monkeypatch, made)
    yield made
    for _ in source:
        pass


# --- 帮手 ---------------------------------------------------------------------------------


def make_tenant(
    factory: sessionmaker[Session],
    *,
    threshold: str | None = None,
    mode: BillingMode = BillingMode.PREPAID,
) -> tuple[int, str]:
    """A tenant with an empty wallet: (id, public_id)."""
    with factory() as session:
        tenant = create_tenant(
            session,
            company_name="Notification Test Sdn Bhd",
            email=f"{uuid.uuid4().hex}{TEST_EMAIL_DOMAIN}",
            now=NOW,
        )
        tenant.billing_mode = mode
        if threshold is not None:
            tenant.low_balance_threshold = Decimal(threshold)
        create_wallet(session, tenant_id=tenant.id, now=NOW)
        session.commit()
        return int(tenant.id), tenant.public_id


def post(
    factory: sessionmaker[Session], tenant_id: int, kind: TransactionType, amount: str
) -> None:
    """One committed posting at `NOW` through the real `post_transaction`."""
    with factory() as session:
        post_transaction(
            session,
            tenant_id=tenant_id,
            transaction_type=kind,
            amount=Decimal(amount),
            reference_type=REFERENCE_TYPE_FOR[kind],
            reference_id=str(uuid.uuid4()),
            now=NOW,
        )
        session.commit()


def forget_events(factory: sessionmaker[Session], public_id: str) -> None:
    """Drop the events the setup postings wrote, so each case sees only its own."""
    with factory() as session:
        session.execute(delete(DomainOutbox).where(DomainOutbox.aggregate_id == public_id))
        session.commit()


def add_event(
    factory: sessionmaker[Session],
    event_type: str,
    payload: object,
    *,
    aggregate_id: str,
    aggregate_type: str = AGGREGATE_TENANT,
    created_at: dt.datetime = NOW,
    attempt_count: int = 0,
    next_retry_at: dt.datetime | None = None,
) -> int:
    with factory() as session:
        row = DomainOutbox(
            event_type=event_type,
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            payload_json=_raw(payload),
            status=OutboxStatus.PENDING,
            attempt_count=attempt_count,
            next_retry_at=created_at if next_retry_at is None else next_retry_at,
            created_at=created_at,
        )
        session.add(row)
        session.commit()
        return int(row.id)


def read(factory: sessionmaker[Session], outbox_id: int) -> DomainOutbox:
    with factory() as session:
        row = session.get(DomainOutbox, outbox_id)
        assert row is not None
        session.expunge(row)
        return row


def outbox_rows(factory: sessionmaker[Session], public_id: str) -> list[DomainOutbox]:
    with factory() as session:
        statement = (
            select(DomainOutbox)
            .where(DomainOutbox.aggregate_id == public_id)
            .order_by(DomainOutbox.id)
        )
        rows = list(session.execute(statement).scalars())
        session.expunge_all()
        return rows


def notifications(factory: sessionmaker[Session]) -> list[Notification]:
    with factory() as session:
        rows = list(session.execute(select(Notification).order_by(Notification.id)).scalars())
        session.expunge_all()
        return rows


def make_due(factory: sessionmaker[Session], outbox_id: int) -> None:
    """Move `next_retry_at` into the past, as if the backoff had run out."""
    with factory() as session:
        session.execute(
            update(DomainOutbox)
            .where(DomainOutbox.id == outbox_id)
            .values(next_retry_at=utc_now() - dt.timedelta(minutes=1))
        )
        session.commit()


def claim(factory: sessionmaker[Session], outbox_id: int):
    """Run the claim UPDATE of `deliver` directly (another worker taking the row)."""
    make_due(factory, outbox_id)
    row = read(factory, outbox_id)
    attempt = row.attempt_count + 1
    with factory() as session:
        claimed = session.execute(
            update(DomainOutbox)
            .where(
                DomainOutbox.id == outbox_id,
                DomainOutbox.status == OutboxStatus.PENDING,
                DomainOutbox.next_retry_at <= utc_now(),
                DomainOutbox.attempt_count == row.attempt_count,
            )
            .values(attempt_count=attempt, next_retry_at=utc_now() + dt.timedelta(minutes=5))
        )
        assert int(claimed.rowcount) == 1
        session.commit()
    return outbox_task._Claim(
        outbox_id=outbox_id,
        attempt=attempt,
        event_type=row.event_type,
        aggregate_type=row.aggregate_type,
        aggregate_id=row.aggregate_id,
        payload_json=row.payload_json,
        created_at=row.created_at,
    )


def run_recovery(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """`recover` with the broker gone: no `delay` reaches a queue; deliver what it re-queues."""
    queued: list[int] = []
    monkeypatch.setattr(outbox_task.deliver, "delay", lambda outbox_id: queued.append(outbox_id))
    outbox_task.recover()
    return [outbox_task.deliver(outbox_id) for outbox_id in queued]


def money_state(factory: sessionmaker[Session], tenant_id: int) -> tuple[object, ...]:
    """The wallet and every ledger row of the tenant: INV-4 / INV-5 say delivery leaves them."""
    with factory() as session:
        wallet = session.execute(
            select(Wallet.balance, Wallet.version).where(Wallet.tenant_id == tenant_id)
        ).one()
        ledger = session.execute(
            select(WalletTransaction.id, WalletTransaction.amount, WalletTransaction.balance_after)
            .where(WalletTransaction.tenant_id == tenant_id)
            .order_by(WalletTransaction.id)
        ).all()
        return tuple(wallet), tuple(tuple(line) for line in ledger)


def outbox_logs(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [record for record in caplog.records if record.name == outbox_task.__name__]


def ledger_created_at(factory: sessionmaker[Session], tenant_id: int) -> dt.datetime:
    with factory() as session:
        statement = (
            select(WalletTransaction.created_at)
            .where(WalletTransaction.tenant_id == tenant_id)
            .order_by(WalletTransaction.wallet_sequence.desc())
            .limit(1)
        )
        return session.execute(statement).scalar_one()


# --- 正常路径 -----------------------------------------------------------------------------


def test_a_low_balance_crossing_becomes_one_notification(factory, monkeypatch) -> None:
    """设计 §7「正常路径：低余额」：阈值 50，余额 60 → 记一笔 -15。"""
    tenant_id, public_id = make_tenant(factory, threshold="50")
    post(factory, tenant_id, TOPUP, "60")
    forget_events(factory, public_id)
    post(factory, tenant_id, AI_USAGE, "-15")
    [event] = outbox_rows(factory, public_id)
    assert event.event_type == EVENT_LOW_BALANCE
    before = money_state(factory, tenant_id)
    started = utc_now().replace(microsecond=0)

    assert run_recovery(monkeypatch) == ["notified"]

    [notice] = notifications(factory)
    assert notice.notification_type is NotificationType.LOW_BALANCE
    assert notice.tenant_id == tenant_id
    assert notice.source_outbox_id == event.id
    assert (notice.balance, notice.threshold) == (Decimal("45"), Decimal("50"))
    assert notice.occurred_at == ledger_created_at(factory, tenant_id) == NOW
    assert _UUID4.fullmatch(notice.public_id)
    assert notice.read_at is None
    # 写入时刻是处理时刻，取整到秒。
    assert notice.created_at.microsecond == 0
    assert notice.created_at >= started
    row = read(factory, event.id)
    assert row.status is OutboxStatus.SENT
    assert row.processed_at is not None
    assert row.payload_json == event.payload_json, "通知类事件的 payload 不清空"
    assert money_state(factory, tenant_id) == before


def test_suspension_and_reactivation_become_two_notifications(factory, monkeypatch) -> None:
    """设计 §7「正常路径：停机与复机」：余额 10 → 记 -10 → 记 +5，依次投递。"""
    tenant_id, public_id = make_tenant(factory)
    post(factory, tenant_id, TOPUP, "10")
    forget_events(factory, public_id)

    post(factory, tenant_id, AI_USAGE, "-10")
    before = money_state(factory, tenant_id)
    assert run_recovery(monkeypatch) == ["notified"]
    assert money_state(factory, tenant_id) == before
    post(factory, tenant_id, TOPUP, "5")
    before = money_state(factory, tenant_id)
    assert run_recovery(monkeypatch) == ["notified"]
    assert money_state(factory, tenant_id) == before

    suspended, reactivated = notifications(factory)
    assert suspended.notification_type is NotificationType.SUSPENDED
    assert (suspended.balance, suspended.threshold) == (Decimal(0), None)
    assert reactivated.notification_type is NotificationType.REACTIVATED
    assert (reactivated.balance, reactivated.threshold) == (Decimal(5), None)
    events = outbox_rows(factory, public_id)
    assert [event.event_type for event in events] == [EVENT_BILLING_STATUS_CHANGED] * 2
    assert [suspended.source_outbox_id, reactivated.source_outbox_id] == [e.id for e in events]
    assert all(event.status is OutboxStatus.SENT for event in events)


def test_one_posting_with_two_events_becomes_two_notifications(factory, monkeypatch) -> None:
    """设计 §7「同一笔两条」：阈值 10、余额 15 → 记 -20。"""
    tenant_id, public_id = make_tenant(factory, threshold="10")
    post(factory, tenant_id, TOPUP, "15")
    forget_events(factory, public_id)
    post(factory, tenant_id, AI_USAGE, "-20")
    events = outbox_rows(factory, public_id)
    assert {event.event_type for event in events} == set(outbox_task.NOTIFICATION_EVENTS)
    before = money_state(factory, tenant_id)

    assert run_recovery(monkeypatch) == ["notified", "notified"]

    found = {notice.notification_type: notice for notice in notifications(factory)}
    assert set(found) == {NotificationType.LOW_BALANCE, NotificationType.SUSPENDED}
    low = found[NotificationType.LOW_BALANCE]
    assert (low.balance, low.threshold) == (Decimal(-5), Decimal(10))
    suspended = found[NotificationType.SUSPENDED]
    assert (suspended.balance, suspended.threshold) == (Decimal(-5), None)
    by_type = {event.event_type: event.id for event in events}
    assert low.source_outbox_id == by_type[EVENT_LOW_BALANCE]
    assert suspended.source_outbox_id == by_type[EVENT_BILLING_STATUS_CHANGED]
    assert money_state(factory, tenant_id) == before


def test_an_internal_metered_transition_is_settled_without_a_notification(factory) -> None:
    """设计 §7「内部计量租户」：outbox `SENT`，没有通知行。"""
    _tenant_id, public_id = make_tenant(factory, mode=BillingMode.INTERNAL_METERED_ONLY)
    outbox_id = add_event(factory, EVENT_BILLING_STATUS_CHANGED, INTERNAL, aggregate_id=public_id)

    assert outbox_task.deliver(outbox_id) == "no-notification"

    row = read(factory, outbox_id)
    assert row.status is OutboxStatus.SENT
    assert row.payload_json == _raw(INTERNAL)
    assert notifications(factory) == []


# --- payload 无效 -------------------------------------------------------------------------


def _exhaust(factory: sessionmaker[Session], outbox_id: int) -> str:
    """Deliver the last allowed attempt."""
    with factory() as session:
        session.execute(
            update(DomainOutbox)
            .where(DomainOutbox.id == outbox_id)
            .values(attempt_count=MAX_ATTEMPTS - 1)
        )
        session.commit()
    make_due(factory, outbox_id)
    return outbox_task.deliver(outbox_id)


INVALID_ROWS = {
    **{
        case: (event, payload, AGGREGATE_TENANT, True)
        for case, (event, payload) in INVALID_PAYLOADS.items()
    },
    "not-a-tenant": (EVENT_LOW_BALANCE, LOW, "users", True),
    "unknown-tenant": (EVENT_LOW_BALANCE, LOW, AGGREGATE_TENANT, False),
    "unknown-tenant-status": (EVENT_BILLING_STATUS_CHANGED, STATUS, AGGREGATE_TENANT, False),
}


@pytest.mark.parametrize("case", list(INVALID_ROWS))
def test_an_invalid_row_is_retried_then_dead_lettered_with_its_payload(
    factory, caplog, case: str
) -> None:
    """设计 §7「payload 无效」：未用尽时仍 `PENDING`；
    用尽时 `FAILED`、payload 保留；都没有通知行。"""
    event_type, payload, aggregate_type, known = INVALID_ROWS[case]
    _tenant_id, public_id = make_tenant(factory)
    outbox_id = add_event(
        factory,
        event_type,
        payload,
        aggregate_id=public_id if known else ZERO_UUID,
        aggregate_type=aggregate_type,
    )

    assert outbox_task.deliver(outbox_id) == "retry-later"
    row = read(factory, outbox_id)
    assert (row.status, row.attempt_count, row.processed_at) == (OutboxStatus.PENDING, 1, None)
    assert row.next_retry_at > utc_now()
    assert notifications(factory) == []

    with caplog.at_level(logging.WARNING, logger=outbox_task.__name__):
        assert _exhaust(factory, outbox_id) == "dead-lettered"

    row = read(factory, outbox_id)
    assert row.status is OutboxStatus.FAILED
    assert row.attempt_count == MAX_ATTEMPTS
    assert row.processed_at is not None
    assert row.payload_json == _raw(payload), "死信不清空 payload：留着供排查"
    assert notifications(factory) == []
    assert any(record.levelno == logging.ERROR for record in outbox_logs(caplog))


def test_the_failure_logs_carry_no_amount_and_no_tenant(factory, caplog) -> None:
    """设计 §6 日志：只带 `outbox_id`、`event_type` 与尝试次数。"""
    _tenant_id, public_id = make_tenant(factory)
    payload = {**STATUS, "billing_mode": "POSTPAID", "balance": "12.34567890"}
    outbox_id = add_event(factory, EVENT_BILLING_STATUS_CHANGED, payload, aggregate_id=public_id)

    with caplog.at_level(logging.WARNING, logger=outbox_task.__name__):
        assert outbox_task.deliver(outbox_id) == "retry-later"
        assert _exhaust(factory, outbox_id) == "dead-lettered"

    records = outbox_logs(caplog)
    assert [record.levelno for record in records] == [logging.WARNING, logging.ERROR]
    for record in records:
        assert record.outbox_id == outbox_id
        text = str(vars(record))
        assert "12.34567890" not in text
        assert public_id not in text
        assert record.exc_info is None


# --- 幂等与交错 ---------------------------------------------------------------------------


def test_delivering_the_same_row_twice_makes_one_notification(factory) -> None:
    """设计 §7「幂等重放」：同一行 `deliver` 两次；已 `SENT` 的行再 `deliver`。"""
    _tenant_id, public_id = make_tenant(factory)
    outbox_id = add_event(factory, EVENT_LOW_BALANCE, LOW, aggregate_id=public_id)

    assert outbox_task.deliver(outbox_id) == "notified"
    make_due(factory, outbox_id)
    assert outbox_task.deliver(outbox_id) == "already-settled"

    assert len(notifications(factory)) == 1


def test_settling_one_claim_twice_makes_one_notification(factory) -> None:
    """结算一步自己的幂等：
    同一次领取结算第二遍，UPDATE 受影响 0 行、不插入。"""
    _tenant_id, public_id = make_tenant(factory)
    outbox_id = add_event(factory, EVENT_LOW_BALANCE, LOW, aggregate_id=public_id)
    current = claim(factory, outbox_id)

    assert outbox_task._settle_notification(factory, current) == "notified"
    assert outbox_task._settle_notification(factory, current) == "already-settled"

    assert len(notifications(factory)) == 1


def test_an_old_claimer_failing_its_last_attempt_does_not_dead_letter_a_newer_claim(
    factory, monkeypatch, caplog
) -> None:
    """设计 §7「旧领取者最后一次失败与新领取交错」。

    上限 2；A 以 `attempt_count` 2 领取、规则判无效；
    B 以 3 领取（直接执行领取 UPDATE）后，A 走死信结算：
    受影响 0 行、`superseded`、不记 error。
    之后 B 结算为 `SENT`，恰好一条通知。
    """
    monkeypatch.setattr(
        outbox_task,
        "get_settings",
        lambda: Settings(frontend_base_url="", outbox_max_attempts=2),
    )
    _tenant_id, public_id = make_tenant(factory)
    outbox_id = add_event(factory, EVENT_LOW_BALANCE, LOW, aggregate_id=public_id, attempt_count=1)

    real_rule = outbox_task.notification_for
    calls: list[int] = []

    def invalid_for_a(**kwargs):
        calls.append(1)
        if len(calls) == 1:
            return InvalidPayload("A sees an invalid payload")
        return real_rule(**kwargs)

    monkeypatch.setattr(outbox_task, "notification_for", invalid_for_a)
    real_dead_letter = outbox_task._dead_letter_notification
    newer: list[object] = []

    def b_claims_first(factory_, claim_a):
        assert claim_a.attempt == 2
        newer.append(claim(factory, outbox_id))
        return real_dead_letter(factory_, claim_a)

    monkeypatch.setattr(outbox_task, "_dead_letter_notification", b_claims_first)

    with caplog.at_level(logging.WARNING, logger=outbox_task.__name__):
        assert outbox_task.deliver(outbox_id) == "superseded"

    assert not any(record.levelno >= logging.ERROR for record in outbox_logs(caplog))
    [claim_b] = newer
    assert claim_b.attempt == 3
    row = read(factory, outbox_id)
    assert (row.status, row.attempt_count) == (OutboxStatus.PENDING, 3)
    assert notifications(factory) == []

    assert outbox_task._settle_notification(factory, claim_b) == "notified"

    assert read(factory, outbox_id).status is OutboxStatus.SENT
    [notice] = notifications(factory)
    assert notice.source_outbox_id == outbox_id


def test_an_old_claimer_succeeding_late_does_not_insert(factory, monkeypatch) -> None:
    """设计 §7「旧领取者成功与新领取交错」：
    A 受影响 0 行、回滚、不插入；B 结算并插入一条。"""
    monkeypatch.setattr(
        outbox_task,
        "get_settings",
        lambda: Settings(frontend_base_url="", outbox_max_attempts=2),
    )
    _tenant_id, public_id = make_tenant(factory)
    outbox_id = add_event(factory, EVENT_LOW_BALANCE, LOW, aggregate_id=public_id, attempt_count=1)

    real_settle = outbox_task._settle_notification
    newer: list[object] = []

    def b_claims_first(factory_, claim_a):
        assert claim_a.attempt == 2
        newer.append(claim(factory, outbox_id))
        return real_settle(factory_, claim_a)

    monkeypatch.setattr(outbox_task, "_settle_notification", b_claims_first)

    assert outbox_task.deliver(outbox_id) == "already-settled"

    row = read(factory, outbox_id)
    assert (row.status, row.attempt_count) == (OutboxStatus.PENDING, 3)
    assert notifications(factory) == []

    [claim_b] = newer
    assert real_settle(factory, claim_b) == "notified"

    assert read(factory, outbox_id).status is OutboxStatus.SENT
    assert len(notifications(factory)) == 1


@pytest.mark.parametrize("other", ["stale-claim", "same-claim"])
def test_two_concurrent_settlements_make_one_notification(mysql, monkeypatch, other: str) -> None:
    """设计 §7「并发」（MySQL）：同一行领取后让 `next_retry_at` 到期再被领取，
    两个会话同时走结算分支，一个在结算 UPDATE 后、提交前暂停。
    另一方等行锁，之后受影响 0 行、回滚。

    `stale-claim`：另一方拿的是过期的那次领取；
    `same-claim`：两方拿同一次领取，只剩行锁与 `status = PENDING` 挡住第二条通知。
    """
    _tenant_id, public_id = make_tenant(mysql)
    outbox_id = add_event(mysql, EVENT_BILLING_STATUS_CHANGED, STATUS, aggregate_id=public_id)
    first = claim(mysql, outbox_id)
    second = claim(mysql, outbox_id)
    paused_claim, waiting_claim = second, (first if other == "stale-claim" else second)

    real_rule = outbox_task.notification_for
    paused, release = threading.Event(), threading.Event()
    lock = threading.Lock()
    calls: list[int] = []

    def pause_once(**kwargs):
        with lock:
            calls.append(1)
            mine = len(calls)
        if mine == 1:
            # 结算 UPDATE 已执行、事务未提交：行锁在手。
            paused.set()
            assert release.wait(30)
        return real_rule(**kwargs)

    monkeypatch.setattr(outbox_task, "notification_for", pause_once)

    with ThreadPoolExecutor(max_workers=2) as pool:
        holder = pool.submit(outbox_task._settle_notification, mysql, paused_claim)
        assert paused.wait(30)
        waiter = pool.submit(outbox_task._settle_notification, mysql, waiting_claim)
        time.sleep(1)
        assert not waiter.done(), "另一方的结算 UPDATE 应在行锁上等待"
        release.set()
        assert holder.result(timeout=60) == "notified"
        assert waiter.result(timeout=60) == "already-settled"

    assert len(calls) == 1, "受影响 0 行的一方不调规则、不插入"
    assert read(mysql, outbox_id).status is OutboxStatus.SENT
    assert len(notifications(mysql)) == 1


# --- 失败与恢复 ---------------------------------------------------------------------------


def test_a_failure_inside_the_transaction_rolls_back_the_settlement(factory, monkeypatch) -> None:
    """设计 §7「事务中途失败」：
    通知 INSERT 已 flush 后抛错 → 结算 UPDATE 一并回滚。"""
    _tenant_id, public_id = make_tenant(factory)
    outbox_id = add_event(factory, EVENT_LOW_BALANCE, LOW, aggregate_id=public_id)
    real_insert = outbox_task._insert_notification

    def insert_then_fail(session, **kwargs):
        real_insert(session, **kwargs)
        raise RuntimeError("the connection dropped here")

    monkeypatch.setattr(outbox_task, "_insert_notification", insert_then_fail)

    assert outbox_task.deliver(outbox_id) == "retry-later"

    row = read(factory, outbox_id)
    assert (row.status, row.attempt_count, row.processed_at) == (OutboxStatus.PENDING, 1, None)
    assert row.payload_json == _raw(LOW)
    assert notifications(factory) == []

    # 故障过去之后，同一行照常结算，恰好一条。
    monkeypatch.setattr(outbox_task, "_insert_notification", real_insert)
    make_due(factory, outbox_id)
    assert outbox_task.deliver(outbox_id) == "notified"
    assert len(notifications(factory)) == 1


def test_recovery_alone_settles_due_rows_after_the_broker_is_lost(factory, monkeypatch) -> None:
    """设计 §7「Redis/Celery 丢失」：不调 `delay`，只调 `recover`。
    到期的两类行被重新触发并结算；未到期的不触发。"""
    _tenant_id, public_id = make_tenant(factory)
    low = add_event(factory, EVENT_LOW_BALANCE, LOW, aggregate_id=public_id)
    status = add_event(factory, EVENT_BILLING_STATUS_CHANGED, STATUS, aggregate_id=public_id)
    later = add_event(
        factory,
        EVENT_LOW_BALANCE,
        LOW,
        aggregate_id=public_id,
        next_retry_at=utc_now() + dt.timedelta(minutes=5),
    )

    assert run_recovery(monkeypatch) == ["notified", "notified"]

    assert read(factory, low).status is OutboxStatus.SENT
    assert read(factory, status).status is OutboxStatus.SENT
    waiting = read(factory, later)
    assert (waiting.status, waiting.attempt_count) == (OutboxStatus.PENDING, 0)
    assert sorted(notice.source_outbox_id for notice in notifications(factory)) == [low, status]


def test_the_production_backlog_and_old_payloads(factory, monkeypatch) -> None:
    """设计 §7「生产积压与旧 payload」：
    没有 `billing_mode` 与 `effective_status` 的两行照常转成
    `REACTIVATED` 与 `SUSPENDED`，`occurred_at` 为原时刻；
    `billing_mode` 为 null 或 `"POSTPAID"` 的两行按无效处理。"""
    _tenant_id, public_id = make_tenant(factory)
    active = {
        **LEGACY,
        "billing_status": "ACTIVE",
        "balance": "20.00000000",
        "reason": REASON_BALANCE_POSITIVE,
    }
    reactivated = add_event(
        factory, EVENT_BILLING_STATUS_CHANGED, active, aggregate_id=public_id, created_at=BACKLOG_AT
    )
    suspended = add_event(
        factory,
        EVENT_BILLING_STATUS_CHANGED,
        LEGACY,
        aggregate_id=public_id,
        created_at=BACKLOG_AT + dt.timedelta(seconds=7),
    )
    broken = [
        add_event(
            factory,
            EVENT_BILLING_STATUS_CHANGED,
            {**STATUS, "billing_mode": mode},
            aggregate_id=public_id,
        )
        for mode in (None, "POSTPAID")
    ]

    assert run_recovery(monkeypatch) == ["notified", "notified", "retry-later", "retry-later"]

    first, second = notifications(factory)
    assert (first.source_outbox_id, first.notification_type) == (
        reactivated,
        NotificationType.REACTIVATED,
    )
    assert (first.balance, first.occurred_at) == (Decimal(20), BACKLOG_AT)
    assert (second.source_outbox_id, second.notification_type) == (
        suspended,
        NotificationType.SUSPENDED,
    )
    assert (second.balance, second.occurred_at) == (
        Decimal(0),
        BACKLOG_AT + dt.timedelta(seconds=7),
    )
    for outbox_id in broken:
        row = read(factory, outbox_id)
        assert (row.status, row.attempt_count) == (OutboxStatus.PENDING, 1)


# --- 金额精度（仅 MySQL） -------------------------------------------------------------------


def test_amounts_round_trip_exactly(mysql) -> None:
    """设计 §7「金额精度」：SQLite 上 NUMERIC 按浮点存，
    只在 MySQL 上保证与验证。"""
    balance, threshold = "999999999999.99999999", "0.00000001"
    # 用例有区分力：这两个值经 float 往返会变。
    assert Decimal(float(balance)) != Decimal(balance)
    assert Decimal(float(threshold)) != Decimal(threshold)
    _tenant_id, public_id = make_tenant(mysql)
    payload = {**LOW, "balance": balance, "threshold": threshold}
    outbox_id = add_event(mysql, EVENT_LOW_BALANCE, payload, aggregate_id=public_id)

    assert outbox_task.deliver(outbox_id) == "notified"

    [notice] = notifications(mysql)
    assert format(notice.balance, "f") == balance
    assert notice.threshold is not None and format(notice.threshold, "f") == threshold
    assert read(mysql, outbox_id).payload_json == _raw(payload)

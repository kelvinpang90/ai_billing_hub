"""Issuing payment receipts (design gate #589 v2, AIH-TASK-197).

设计 §2「编号」「快照」「开具」「付款邮件」（outbox 行部分）；§7 标 197 的四行：

- **开具**：编号、快照键集合与每个值、列与快照一致、`tenant_id` 与 `tax_policy_version_id`
  取自支付；`send_email` 真与假；同一支付第二次调用；状态不是 `PAID`；
- **开具检查不过**：三条检查各自不过（没有 TOPUP 行；税务政策读不出；当年没有系列行；
  `last_sequence = 999999`）与先后顺序；
- **编号并发与年份**：12-31 15:59:59 UTC 与 16:00:00 UTC，另加带微秒的
  15:59:59.700000（设计闸门 v2 审查的建议项）；两个连接同时开具同一年份只在真 MySQL 上跑；
- **快照与税务说明**：开具后改公司名、发布新的税务政策版本。

另加 `gateway_payment_intent_id` 为空时快照的 `gateway_reference` 取 `gateway_payment_id`
（设计闸门 v1 审查的建议项）。

支付、租户与造 PAID 支付和 TOPUP 账本行的构件用共用的 payment_world.py（`paid_payment_maker`
在 conftest.py 注册）；本文件其余夹具只在这里用，定义在这里。时间一律注入，不 sleep。

MySQL 那部分需要 `BILLING_TEST_DATABASE_URL`，没设时 skip ——
**不要把 skipped 读成 passed**，CI 设了它并把 skipped 判成失败。
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import threading
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from typing import Any

import pytest
from payment_world import (
    CREATED,
    RECEIPT_NOTICE,
    Customer,
    PaymentRef,
    World,
    build_mysql_world,
    build_sqlite_world,
    insert_tax_version,
    mysql_url,
    seed_receipt_series,
)
from sqlalchemy import Engine, create_engine, event, func, select, text, update
from sqlalchemy.orm import Session, sessionmaker

from app.core.database import create_session_factory
from app.models.auth import DomainOutbox, OutboxStatus
from app.models.payments import Payment, PaymentStatus
from app.models.receipts import PaymentReceipt, ReceiptNumberSeries
from app.models.tenancy import Tenant
from app.services.receipts import (
    AGGREGATE_PAYMENT_RECEIPT,
    EVENT_RECEIPT_EMAIL,
    RECEIPT_ISSUER_NAME,
    RECEIPT_LEDGER_MISSING,
    RECEIPT_SERIES_UNAVAILABLE,
    RECEIPT_TAX_POLICY_UNREADABLE,
    IssuedReceipt,
    issue_receipt,
)

# 固定值而不是 utc_now()：断言「存进去的就是调用方给的那个时刻」要能逐字比对。
ISSUE_NOW = dt.datetime(2026, 11, 10, 6, 5, 9)
COMPANY = "Payment Confirm Test Sdn Bhd"
EMAIL = "ops@example.com"
# 足够独特，日志里出现就认得出来。
ODD_AMOUNT = "4321.09"

SNAPSHOT_KEYS = {
    "schema_version",
    "receipt_number",
    "issued_at",
    "issuer",
    "customer",
    "payment",
    "tax_notice",
}
PAYMENT_KEYS = {
    "payment_id",
    "paid_at",
    "amount",
    "currency",
    "status",
    "gateway",
    "gateway_reference",
    "payment_method",
}
PAYLOAD_KEYS = {"to", "company_name", "receipt_number", "amount", "paid_at", "payment_id"}


# --- 夹具 ---------------------------------------------------------------


def _with_series(worlds: Iterator[World]) -> Iterator[World]:
    # `create_all` 建的 SQLite 库没有系列行；MySQL 上迁移 0039 已写过（这里什么都不做）。
    for world in worlds:
        seed_receipt_series(world.factory)
        yield world


@pytest.fixture(params=["sqlite", "mysql"])
def receipt_world(request, monkeypatch) -> Iterator[World]:
    worlds = build_mysql_world(monkeypatch) if request.param == "mysql" else build_sqlite_world()
    yield from _with_series(worlds)


@pytest.fixture
def mysql_receipt_world(monkeypatch) -> Iterator[World]:
    yield from _with_series(build_mysql_world(monkeypatch))


# --- 构件 ---------------------------------------------------------------


def issue(
    world: World,
    payment: PaymentRef,
    *,
    method: str | None = "fpx",
    now: dt.datetime = ISSUE_NOW,
    send_email: bool = True,
    factory: sessionmaker[Session] | None = None,
    on_start: Callable[[Session], None] | None = None,
) -> IssuedReceipt | None:
    """What a caller does: lock the payment row, issue, commit."""
    with (factory or world.factory)() as session:
        if on_start is not None:
            on_start(session)
        row = session.execute(
            select(Payment).where(Payment.public_id == payment.public_id).with_for_update()
        ).scalar_one()
        issued = issue_receipt(
            session, payment=row, payment_method=method, now=now, send_email=send_email
        )
        session.commit()
        return issued


def receipts(world: World) -> list[PaymentReceipt]:
    with world.factory() as session:
        rows = list(session.execute(select(PaymentReceipt).order_by(PaymentReceipt.id)).scalars())
        for row in rows:
            session.expunge(row)
        return rows


def receipt_of(world: World, payment: PaymentRef) -> PaymentReceipt:
    with world.factory() as session:
        row = session.execute(
            select(PaymentReceipt)
            .join(Payment, Payment.id == PaymentReceipt.payment_id)
            .where(Payment.public_id == payment.public_id)
        ).scalar_one()
        session.expunge(row)
        return row


def series(world: World) -> dict[int, int]:
    with world.factory() as session:
        rows = session.execute(
            select(ReceiptNumberSeries.series_year, ReceiptNumberSeries.last_sequence)
        ).all()
        return {int(year): int(last) for year, last in rows}


def set_series(world: World, year: int, last_sequence: int) -> None:
    with world.factory() as session:
        session.execute(
            update(ReceiptNumberSeries)
            .where(ReceiptNumberSeries.series_year == year)
            .values(last_sequence=last_sequence)
        )
        session.commit()


def remove_series(world: World, year: int) -> None:
    """SQLite only: the year has no series row (MySQL 上系列行由迁移预建，测试不删)."""
    with world.factory() as session:
        row = session.get(ReceiptNumberSeries, year)
        assert row is not None
        session.delete(row)
        session.commit()


def receipt_emails(world: World) -> list[DomainOutbox]:
    with world.factory() as session:
        rows = list(
            session.execute(
                select(DomainOutbox)
                .where(DomainOutbox.aggregate_type == AGGREGATE_PAYMENT_RECEIPT)
                .order_by(DomainOutbox.id)
            ).scalars()
        )
        for row in rows:
            session.expunge(row)
        return rows


def outbox_count(world: World) -> int:
    with world.factory() as session:
        return int(session.execute(select(func.count()).select_from(DomainOutbox)).scalar_one())


def payment_row(world: World, payment: PaymentRef) -> Payment:
    with world.factory() as session:
        row = session.execute(
            select(Payment).where(Payment.public_id == payment.public_id)
        ).scalar_one()
        session.expunge(row)
        return row


def everything(world: World) -> dict[str, Any]:
    """Every row `issue_receipt` may write, as committed."""
    return {
        "receipts": [(row.id, row.receipt_number) for row in receipts(world)],
        "series": series(world),
        "outbox": outbox_count(world),
    }


def ready(
    world: World,
    maker: Callable[..., PaymentRef],
    tenant: Customer | None = None,
    **options: Any,
) -> PaymentRef:
    """A `PAID` payment with its `TOPUP` row and a readable tax policy version."""
    if "tax_version_id" not in options:
        options["tax_version_id"] = insert_tax_version(world)
    return maker(world, tenant or world.customer(), **options)


# --- 开具 ---------------------------------------------------------------


def test_issuing_freezes_the_snapshot_the_design_describes(
    receipt_world: World, paid_payment_maker: Callable[..., PaymentRef]
) -> None:
    world = receipt_world
    version = insert_tax_version(world)
    tenant = world.customer()
    payment = paid_payment_maker(world, tenant, tax_version_id=version, amount="1234.50")
    paid = payment_row(world, payment)

    issued = issue(world, payment, method="fpx")

    assert issued is not None
    assert issued.created is True
    assert issued.receipt_number == "ACV-AIR-2026-000001"
    row = receipt_of(world, payment)
    assert row.public_id == issued.public_id
    assert row.payment_id == paid.id
    assert row.tenant_id == paid.tenant_id == tenant.id
    assert row.tax_policy_version_id == paid.tax_policy_version_id == version
    assert row.receipt_number == "ACV-AIR-2026-000001"
    assert (row.series_year, row.sequence) == (2026, 1)
    assert row.amount == Decimal("1234.50")
    assert row.currency == "MYR"
    assert row.issued_at == ISSUE_NOW
    assert paid.gateway_payment_intent_id is not None
    assert row.snapshot_json == {
        "schema_version": 1,
        "receipt_number": "ACV-AIR-2026-000001",
        "issued_at": "2026-11-10T06:05:09",
        "issuer": {"name": "Acuven Technology Sdn Bhd"},
        "customer": {"company_name": COMPANY},
        "payment": {
            "payment_id": payment.public_id,
            "paid_at": CREATED.isoformat(timespec="seconds"),
            "amount": "1234.50",
            "currency": "MYR",
            "status": "PAID",
            "gateway": "STRIPE",
            "gateway_reference": paid.gateway_payment_intent_id,
            "payment_method": "fpx",
        },
        "tax_notice": dict(RECEIPT_NOTICE),
    }
    assert set(row.snapshot_json) == SNAPSHOT_KEYS
    assert set(row.snapshot_json["payment"]) == PAYMENT_KEYS
    # 列与快照一致。
    assert Decimal(row.snapshot_json["payment"]["amount"]) == row.amount
    assert row.snapshot_json["payment"]["currency"] == row.currency
    assert row.snapshot_json["issued_at"] == row.issued_at.isoformat(timespec="seconds")
    assert row.snapshot_json["receipt_number"] == row.receipt_number
    assert RECEIPT_ISSUER_NAME == "Acuven Technology Sdn Bhd"
    assert series(world)[2026] == 1


def test_the_next_receipt_of_the_year_takes_the_next_number(
    receipt_world: World, paid_payment_maker: Callable[..., PaymentRef]
) -> None:
    world = receipt_world
    version = insert_tax_version(world)
    first = ready(world, paid_payment_maker, tax_version_id=version)
    second = ready(world, paid_payment_maker, tax_version_id=version)

    one = issue(world, first)
    two = issue(world, second, now=ISSUE_NOW + dt.timedelta(minutes=1))

    assert one is not None and two is not None
    assert (one.receipt_number, two.receipt_number) == (
        "ACV-AIR-2026-000001",
        "ACV-AIR-2026-000002",
    )
    assert [(row.series_year, row.sequence) for row in receipts(world)] == [(2026, 1), (2026, 2)]
    assert series(world)[2026] == 2


def test_send_email_writes_one_outbox_row_with_exactly_six_keys(
    receipt_world: World, paid_payment_maker: Callable[..., PaymentRef]
) -> None:
    world = receipt_world
    payment = ready(world, paid_payment_maker, amount="1234.50")

    issued = issue(world, payment, send_email=True)

    assert issued is not None
    [email] = receipt_emails(world)
    assert email.event_type == EVENT_RECEIPT_EMAIL == "PAYMENT_RECEIPT_EMAIL_REQUESTED"
    assert email.aggregate_type == AGGREGATE_PAYMENT_RECEIPT == "payment_receipt"
    assert email.aggregate_id == issued.public_id
    assert email.status is OutboxStatus.PENDING
    assert email.attempt_count == 0
    assert email.next_retry_at == email.created_at == ISSUE_NOW
    assert email.processed_at is None
    expected = {
        "to": EMAIL,
        "company_name": COMPANY,
        "receipt_number": "ACV-AIR-2026-000001",
        "amount": "1234.50",
        "paid_at": CREATED.isoformat(timespec="seconds"),
        "payment_id": payment.public_id,
    }
    assert email.payload_json == json.dumps(expected, sort_keys=True)
    assert set(json.loads(email.payload_json or "{}")) == PAYLOAD_KEYS
    # 金额与付款时刻与快照相同。
    snapshot = receipt_of(world, payment).snapshot_json
    assert expected["amount"] == snapshot["payment"]["amount"]
    assert expected["paid_at"] == snapshot["payment"]["paid_at"]


def test_without_send_email_there_is_no_outbox_row(
    receipt_world: World, paid_payment_maker: Callable[..., PaymentRef]
) -> None:
    world = receipt_world
    payment = ready(world, paid_payment_maker)
    before = outbox_count(world)

    issued = issue(world, payment, send_email=False, method=None)

    assert issued is not None and issued.created is True
    assert receipt_emails(world) == []
    assert outbox_count(world) == before
    assert receipt_of(world, payment).snapshot_json["payment"]["payment_method"] is None


def test_a_second_call_returns_the_same_receipt_and_writes_nothing(
    receipt_world: World, paid_payment_maker: Callable[..., PaymentRef]
) -> None:
    world = receipt_world
    payment = ready(world, paid_payment_maker)
    first = issue(world, payment)
    assert first is not None
    before = everything(world)
    snapshot = receipt_of(world, payment).snapshot_json

    again = issue(world, payment, now=ISSUE_NOW + dt.timedelta(days=400), method="card")

    assert again == IssuedReceipt(first.public_id, first.receipt_number, created=False)
    assert everything(world) == before
    assert len(receipt_emails(world)) == 1
    assert receipt_of(world, payment).snapshot_json == snapshot


@pytest.mark.parametrize(
    "status",
    [
        PaymentStatus.PENDING,
        PaymentStatus.FAILED,
        PaymentStatus.EXPIRED,
        PaymentStatus.CANCELLED,
        PaymentStatus.REVIEW_REQUIRED,
    ],
)
def test_a_payment_that_is_not_paid_is_a_value_error(
    receipt_world: World, status: PaymentStatus
) -> None:
    world = receipt_world
    payment = world.payment(
        world.customer(), status=status, tax_version_id=insert_tax_version(world)
    )
    before = everything(world)

    with pytest.raises(ValueError):
        issue(world, payment)

    assert everything(world) == before


def test_the_gateway_reference_falls_back_to_the_session_id(
    receipt_world: World, paid_payment_maker: Callable[..., PaymentRef]
) -> None:
    """设计闸门 v1 审查的建议项：`gateway_payment_intent_id` 为空时取 `gateway_payment_id`。"""
    world = receipt_world
    payment = ready(world, paid_payment_maker, intent_id=None)
    assert payment_row(world, payment).gateway_payment_intent_id is None
    assert payment.session_id is not None

    issue(world, payment)

    snapshot = receipt_of(world, payment).snapshot_json
    assert snapshot["payment"]["gateway_reference"] == payment.session_id


def test_the_amount_is_the_ledger_row_not_the_expected_amount(
    receipt_world: World, paid_payment_maker: Callable[..., PaymentRef]
) -> None:
    """收据金额 = `TOPUP` 账本行金额（B 的按实付入账可能不等于 `expected_amount`）。"""
    world = receipt_world
    payment = ready(world, paid_payment_maker, amount="99.99")
    with world.factory() as session:
        # 只改应收：账本行仍是 99.99。
        session.execute(
            update(Payment)
            .where(Payment.public_id == payment.public_id)
            .values(expected_amount=Decimal("100.00"), wallet_credit_amount=Decimal("100.00"))
        )
        session.commit()

    issue(world, payment)

    row = receipt_of(world, payment)
    assert row.amount == Decimal("99.99")
    assert row.snapshot_json["payment"]["amount"] == "99.99"


# --- 开具检查不过 ---------------------------------------------------------


def _assert_refused(
    world: World,
    caplog: pytest.LogCaptureFixture,
    payment: PaymentRef,
    code: str,
    before: dict[str, Any],
    **options: Any,
) -> None:
    caplog.clear()
    with caplog.at_level(logging.ERROR, logger="app.services.receipts"):
        issued = issue(world, payment, **options)

    assert issued is None
    assert everything(world) == before
    assert receipt_emails(world) == []
    records = [r for r in caplog.records if r.name == "app.services.receipts"]
    [record] = records
    assert record.levelno == logging.ERROR
    assert record.getMessage() == "Receipt not issued; a pre-issue check failed"
    assert record.__dict__["reason"] == code
    assert record.__dict__["payment_id"] == payment.public_id
    standard = set(logging.LogRecord("x", logging.ERROR, "x", 0, "x", None, None).__dict__)
    extra = {key: value for key, value in record.__dict__.items() if key not in standard}
    assert set(extra) >= {"reason", "payment_id"}
    rendered = record.getMessage() + repr(extra)
    for secret in (COMPANY, EMAIL, ODD_AMOUNT):
        assert secret not in rendered


def test_no_topup_row_is_receipt_ledger_missing(
    receipt_world: World,
    paid_payment_maker: Callable[..., PaymentRef],
    caplog: pytest.LogCaptureFixture,
) -> None:
    world = receipt_world
    payment = ready(world, paid_payment_maker, amount=ODD_AMOUNT, ledger=False)

    _assert_refused(world, caplog, payment, RECEIPT_LEDGER_MISSING, everything(world))


def test_an_unreadable_tax_policy_is_receipt_tax_policy_unreadable(
    receipt_world: World,
    paid_payment_maker: Callable[..., PaymentRef],
    caplog: pytest.LogCaptureFixture,
) -> None:
    world = receipt_world
    # world 自带的那一版没有 `document_notice`：`parse_treatment` 读不出。
    payment = ready(
        world, paid_payment_maker, amount=ODD_AMOUNT, tax_version_id=world.tax_version_id
    )

    _assert_refused(world, caplog, payment, RECEIPT_TAX_POLICY_UNREADABLE, everything(world))


@pytest.mark.parametrize(
    "now",
    [
        # 马来西亚 2025-12-31 23:59:59：2025 年没有系列行。
        dt.datetime(2025, 12, 31, 15, 59, 59),
        # 马来西亚 2100-01-01 00:00:00：2100 年没有系列行。
        dt.datetime(2099, 12, 31, 16, 0, 0),
    ],
    ids=["2025", "2100"],
)
def test_a_year_without_a_series_row_is_receipt_series_unavailable(
    receipt_world: World,
    paid_payment_maker: Callable[..., PaymentRef],
    caplog: pytest.LogCaptureFixture,
    now: dt.datetime,
) -> None:
    world = receipt_world
    payment = ready(world, paid_payment_maker, amount=ODD_AMOUNT)

    _assert_refused(world, caplog, payment, RECEIPT_SERIES_UNAVAILABLE, everything(world), now=now)


def test_a_missing_series_row_of_the_current_year_is_receipt_series_unavailable(
    paid_payment_maker: Callable[..., PaymentRef], caplog: pytest.LogCaptureFixture
) -> None:
    """当年（2026）的系列行不在：只在 SQLite 上删得掉（MySQL 上由迁移预建，不删）。"""
    for world in _with_series(build_sqlite_world()):
        remove_series(world, 2026)
        payment = ready(world, paid_payment_maker, amount=ODD_AMOUNT)

        _assert_refused(world, caplog, payment, RECEIPT_SERIES_UNAVAILABLE, everything(world))


def test_an_exhausted_series_is_receipt_series_unavailable(
    receipt_world: World,
    paid_payment_maker: Callable[..., PaymentRef],
    caplog: pytest.LogCaptureFixture,
) -> None:
    world = receipt_world
    set_series(world, 2026, 999999)
    payment = ready(world, paid_payment_maker, amount=ODD_AMOUNT)

    _assert_refused(world, caplog, payment, RECEIPT_SERIES_UNAVAILABLE, everything(world))
    assert series(world)[2026] == 999999


def test_the_last_number_of_a_series_is_still_issued(
    receipt_world: World, paid_payment_maker: Callable[..., PaymentRef]
) -> None:
    world = receipt_world
    set_series(world, 2026, 999998)
    payment = ready(world, paid_payment_maker)

    issued = issue(world, payment)

    assert issued is not None and issued.receipt_number == "ACV-AIR-2026-999999"
    assert series(world)[2026] == 999999


def test_the_checks_run_in_order(
    receipt_world: World,
    paid_payment_maker: Callable[..., PaymentRef],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """第一条不过就停：账本行与税务政策都不过 → 账本；税务政策与系列都不过 → 税务政策。"""
    world = receipt_world
    set_series(world, 2026, 999999)
    both = ready(
        world,
        paid_payment_maker,
        amount=ODD_AMOUNT,
        tax_version_id=world.tax_version_id,
        ledger=False,
    )
    _assert_refused(world, caplog, both, RECEIPT_LEDGER_MISSING, everything(world))

    later = ready(world, paid_payment_maker, amount=ODD_AMOUNT, tax_version_id=world.tax_version_id)
    _assert_refused(world, caplog, later, RECEIPT_TAX_POLICY_UNREADABLE, everything(world))


def test_a_refused_check_leaves_the_callers_own_writes_to_commit(
    receipt_world: World, paid_payment_maker: Callable[..., PaymentRef]
) -> None:
    """检查不过时不写任何行，调用方事务里自己的写入照常提交（入账不被挡住）。"""
    world = receipt_world
    tenant = world.customer()
    payment = ready(world, paid_payment_maker, tenant, ledger=False)

    with world.factory() as session:
        row = session.execute(
            select(Payment).where(Payment.public_id == payment.public_id).with_for_update()
        ).scalar_one()
        session.execute(
            update(Tenant).where(Tenant.id == tenant.id).values(company_name="Caller Wrote This")
        )
        issued = issue_receipt(
            session, payment=row, payment_method=None, now=ISSUE_NOW, send_email=True
        )
        session.commit()

    assert issued is None
    assert receipts(world) == []
    with world.factory() as session:
        committed = session.get(Tenant, tenant.id)
        assert committed is not None and committed.company_name == "Caller Wrote This"


# --- 编号的年份 -----------------------------------------------------------


@pytest.mark.parametrize(
    ("now", "issued_at", "year"),
    [
        (dt.datetime(2026, 12, 31, 15, 59, 59), dt.datetime(2026, 12, 31, 15, 59, 59), 2026),
        (dt.datetime(2026, 12, 31, 16, 0, 0), dt.datetime(2026, 12, 31, 16, 0, 0), 2027),
        # 设计闸门 v2 审查的建议项：带微秒的时刻先截掉微秒，三处都是 15:59:59 那一秒与当年。
        (
            dt.datetime(2026, 12, 31, 15, 59, 59, 700000),
            dt.datetime(2026, 12, 31, 15, 59, 59),
            2026,
        ),
    ],
    ids=["last-second-of-the-year", "first-second-of-the-next-year", "with-microseconds"],
)
def test_the_series_year_is_the_malaysian_year_of_the_truncated_moment(
    receipt_world: World,
    paid_payment_maker: Callable[..., PaymentRef],
    now: dt.datetime,
    issued_at: dt.datetime,
    year: int,
) -> None:
    world = receipt_world
    payment = ready(world, paid_payment_maker)

    issued = issue(world, payment, now=now, send_email=False)

    assert issued is not None
    assert issued.receipt_number == f"ACV-AIR-{year}-000001"
    row = receipt_of(world, payment)
    assert row.issued_at == issued_at
    assert row.snapshot_json["issued_at"] == issued_at.isoformat(timespec="seconds")
    assert row.series_year == year
    assert row.snapshot_json["receipt_number"] == f"ACV-AIR-{year}-000001"
    after = series(world)
    assert after[year] == 1
    assert {other: last for other, last in after.items() if other != year} == {
        other: 0 for other in after if other != year
    }


def test_the_two_sides_of_new_year_take_their_own_series(
    receipt_world: World, paid_payment_maker: Callable[..., PaymentRef]
) -> None:
    world = receipt_world
    version = insert_tax_version(world)
    late = ready(world, paid_payment_maker, tax_version_id=version)
    early = ready(world, paid_payment_maker, tax_version_id=version)
    again = ready(world, paid_payment_maker, tax_version_id=version)

    issued = [
        issue(world, late, now=dt.datetime(2026, 12, 31, 15, 59, 59)),
        issue(world, early, now=dt.datetime(2026, 12, 31, 16, 0, 0)),
        issue(world, again, now=dt.datetime(2027, 1, 2, 0, 0, 0)),
    ]

    assert [receipt.receipt_number for receipt in issued if receipt is not None] == [
        "ACV-AIR-2026-000001",
        "ACV-AIR-2027-000001",
        "ACV-AIR-2027-000002",
    ]
    assert (series(world)[2026], series(world)[2027]) == (1, 2)


# --- 快照与税务说明 -------------------------------------------------------

OTHER_NOTICE = {
    "zh": "另一版虚构的税务说明。",
    "en": "Another fictional tax notice.",
}


def test_the_snapshot_does_not_follow_later_changes(
    receipt_world: World, paid_payment_maker: Callable[..., PaymentRef]
) -> None:
    """开具后改公司名、发布新的税务政策版本：快照不变，`tax_notice` 是建单时那一版的原文。"""
    world = receipt_world
    tenant = world.customer()
    payment = ready(world, paid_payment_maker, tenant)
    issue(world, payment)
    before = receipt_of(world, payment)

    with world.factory() as session:
        session.execute(
            update(Tenant).where(Tenant.id == tenant.id).values(company_name="Renamed Sdn Bhd")
        )
        session.commit()
    insert_tax_version(world, OTHER_NOTICE, published_from=dt.datetime(2026, 11, 1))
    again = issue(world, payment)

    after = receipt_of(world, payment)
    assert again is not None and again.created is False
    assert after.snapshot_json == before.snapshot_json
    assert after.snapshot_json["customer"] == {"company_name": COMPANY}
    assert after.snapshot_json["tax_notice"] == RECEIPT_NOTICE
    assert after.tax_policy_version_id == before.tax_policy_version_id


def test_the_tax_notice_is_the_payments_own_version_not_the_one_in_effect(
    receipt_world: World, paid_payment_maker: Callable[..., PaymentRef]
) -> None:
    world = receipt_world
    own = insert_tax_version(world)
    insert_tax_version(world, OTHER_NOTICE, published_from=dt.datetime(2026, 11, 1))
    payment = ready(world, paid_payment_maker, tax_version_id=own)

    issue(world, payment)

    row = receipt_of(world, payment)
    assert row.tax_policy_version_id == own
    assert row.snapshot_json["tax_notice"] == RECEIPT_NOTICE


def test_the_company_name_is_the_one_at_issue_time(
    receipt_world: World, paid_payment_maker: Callable[..., PaymentRef]
) -> None:
    world = receipt_world
    tenant = world.customer()
    payment = ready(world, paid_payment_maker, tenant)
    with world.factory() as session:
        session.execute(
            update(Tenant).where(Tenant.id == tenant.id).values(company_name="Renamed Sdn Bhd")
        )
        session.commit()

    issue(world, payment)

    row = receipt_of(world, payment)
    assert row.snapshot_json["customer"] == {"company_name": "Renamed Sdn Bhd"}
    [email] = receipt_emails(world)
    assert json.loads(email.payload_json or "{}")["company_name"] == "Renamed Sdn Bhd"


# --- 真 MySQL：编号并发 ---------------------------------------------------

GATE_TIMEOUT_SECONDS = 15.0
LOCK_POLL_LIMIT_SECONDS = 10.0
LOCK_WAIT_TIMEOUT_SECONDS = 20
JOIN_TIMEOUT_SECONDS = 40.0

# ⚠️ 查 performance_schema.data_lock_waits，不查 INNODB_TRX（CLAUDE.md「写后端测试的约定」）。
_WAITING_ON_TABLE = text(
    "SELECT COUNT(*) FROM performance_schema.data_lock_waits AS w"
    " JOIN performance_schema.threads AS t ON t.THREAD_ID = w.REQUESTING_THREAD_ID"
    " JOIN performance_schema.data_locks AS l"
    " ON l.ENGINE = w.ENGINE AND l.ENGINE_LOCK_ID = w.REQUESTING_ENGINE_LOCK_ID"
    " WHERE t.PROCESSLIST_ID = :connection_id"
    " AND l.OBJECT_SCHEMA = DATABASE() AND l.OBJECT_NAME = :table_name"
)


def _locks_series(statement: str) -> bool:
    """`SELECT … FROM receipt_number_series … FOR UPDATE` (整个用例只在 MySQL 上跑)."""
    sql = " ".join(statement.replace("`", "").lower().split())
    return (
        sql.startswith("select ")
        and " from receipt_number_series " in sql
        and sql.endswith(" for update")
    )


class Gate:
    """The first matching statement on `engine` waits here, after it ran, until `release()`."""

    def __init__(self, engine: Engine, matches: Callable[[str], bool]) -> None:
        self.reached = threading.Event()
        self.released = threading.Event()
        self._fired = False
        self._lock = threading.Lock()

        def listener(conn, cursor, statement, parameters, context, executemany) -> None:
            if not matches(statement):
                return
            with self._lock:
                if self._fired:
                    return
                self._fired = True
            self.reached.set()
            if not self.released.wait(GATE_TIMEOUT_SECONDS):
                raise AssertionError("nobody released the gate")

        event.listen(engine, "after_cursor_execute", listener)

    def wait_reached(self) -> None:
        if not self.reached.wait(GATE_TIMEOUT_SECONDS):
            pytest.fail(f"the gate was not reached within {GATE_TIMEOUT_SECONDS}s")

    def release(self) -> None:
        self.released.set()


def _limit_lock_waits(dbapi_connection: Any, _record: Any) -> None:
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute(f"SET SESSION innodb_lock_wait_timeout = {LOCK_WAIT_TIMEOUT_SECONDS}")
    finally:
        cursor.close()
    dbapi_connection.rollback()


@pytest.fixture
def engines() -> Iterator[Callable[..., Engine]]:
    """Engines of their own, one per side of a race, so a gate only stops that side."""
    url = mysql_url()
    made: list[Engine] = []

    def make(*, observer: bool = False) -> Engine:
        if observer:
            engine = create_engine(url, isolation_level="AUTOCOMMIT", pool_size=1)
        else:
            engine = create_engine(url, pool_size=2, max_overflow=2)
            event.listen(engine, "connect", _limit_lock_waits)
        made.append(engine)
        return engine

    try:
        yield make
    finally:
        for engine in made:
            engine.dispose()


def _wait_until_waiting(observer: Engine, connection_ids: list[int], table: str) -> None:
    """Poll until the second issuer waits on a `table` row lock (每轮一次查询，不 sleep)."""
    deadline = time.monotonic() + LOCK_POLL_LIMIT_SECONDS
    with observer.connect() as connection:
        while time.monotonic() < deadline:
            if connection_ids:
                waiting = connection.execute(
                    _WAITING_ON_TABLE, {"connection_id": connection_ids[-1], "table_name": table}
                ).scalar_one()
                if waiting:
                    return
            else:
                connection.execute(text("SELECT 1"))
    pytest.fail(f"the second issuer was not seen waiting for a {table} row lock")


def test_a_second_issuer_waits_for_the_series_row_and_takes_the_next_number(
    mysql_receipt_world: World,
    paid_payment_maker: Callable[..., PaymentRef],
    engines: Callable[..., Engine],
) -> None:
    """设计 §4「两笔支付同时入账」：都在当年系列行上排队，编号连续、不重复。"""
    world = mysql_receipt_world
    version = insert_tax_version(world)
    first = ready(world, paid_payment_maker, tax_version_id=version)
    second = ready(world, paid_payment_maker, tax_version_id=version)
    first_engine, second_engine = engines(), engines()
    observer = engines(observer=True)
    gate = Gate(first_engine, _locks_series)
    connection_ids: list[int] = []

    def remember(session: Session) -> None:
        connection_ids.append(int(session.execute(text("SELECT CONNECTION_ID()")).scalar_one()))

    with ThreadPoolExecutor(max_workers=2) as pool:
        try:
            one = pool.submit(issue, world, first, factory=create_session_factory(first_engine))
            # 第一个已锁住 2026 年的系列行，还没提交。
            gate.wait_reached()
            two = pool.submit(
                issue,
                world,
                second,
                factory=create_session_factory(second_engine),
                on_start=remember,
            )
            _wait_until_waiting(observer, connection_ids, "receipt_number_series")
        finally:
            gate.release()
        first_issued = one.result(timeout=JOIN_TIMEOUT_SECONDS)
        second_issued = two.result(timeout=JOIN_TIMEOUT_SECONDS)

    assert first_issued is not None and second_issued is not None
    assert first_issued.receipt_number == "ACV-AIR-2026-000001"
    assert second_issued.receipt_number == "ACV-AIR-2026-000002"
    assert series(world)[2026] == 2


def test_many_concurrent_issuers_get_consecutive_unique_numbers(
    mysql_receipt_world: World,
    paid_payment_maker: Callable[..., PaymentRef],
    engines: Callable[..., Engine],
) -> None:
    world = mysql_receipt_world
    version = insert_tax_version(world)
    payments = [ready(world, paid_payment_maker, tax_version_id=version) for _ in range(6)]
    factory = create_session_factory(engines())
    start = threading.Barrier(len(payments))

    def run(payment: PaymentRef) -> IssuedReceipt | None:
        start.wait(GATE_TIMEOUT_SECONDS)
        return issue(world, payment, factory=factory)

    with ThreadPoolExecutor(max_workers=len(payments)) as pool:
        results = [
            future.result(timeout=JOIN_TIMEOUT_SECONDS)
            for future in [pool.submit(run, payment) for payment in payments]
        ]

    numbers = sorted(result.receipt_number for result in results if result is not None)
    assert numbers == [f"ACV-AIR-2026-{n:06d}" for n in range(1, len(payments) + 1)]
    assert sorted(row.sequence for row in receipts(world)) == list(range(1, len(payments) + 1))
    assert series(world)[2026] == len(payments)

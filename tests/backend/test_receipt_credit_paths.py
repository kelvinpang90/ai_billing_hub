"""Receipts issued in the posting transaction of A and B (design gate #589 v2, AIH-TASK-200).

设计 §2「对 A 的 confirm_payment 与 B 的 resolve_review 的修改」；§7 标 200 的两行：

- **同事务（自动确认）**：`confirm_payment` 判定通过 → 支付 `PAID`、TOPUP、收据与
  付款邮件行同时提交，付款方式取 Stripe 返回值；进入 `REVIEW_REQUIRED`、其他状态、
  已是 `PAID`、已结案时没有收据；开具检查不过时入账照常、没有收据与邮件行；开具写入时
  注入数据库错误 → 异常传出 `confirm_payment`，支付、账本与收据都不变；
- **同事务（人工入账）**：B 的 CREDIT 成功时收据金额等于 Stripe 实收（不等于
  `expected_amount`）、有邮件行；CLOSE 没有收据；CREDIT 时开具检查不过 → 入账照常；
  注入数据库错误 → 异常传出 `resolve_review`，一切不变；
- **锁顺序**（真 MySQL）：「支付 → 钱包 → 租户 → 当年系列行」。

数据库错误的替身：会话 flush 时只要有待插入的收据就抛 `OperationalError`（开具第 3 步的写入），其他
flush 照常。不断言事件状态（A 的事件处理对这类异常的行为不变）。

以上除锁顺序外都用 `receipt_world` 夹具在 SQLite 与真 MySQL 上各跑一次。世界来自 payment_world.py；
`create_all` 建的 SQLite 库先补上迁移 0039 的系列行。Stripe 一律是注入了假传输的 `StripeGateway`，
不联网；时间一律注入，不 sleep。

MySQL 那部分需要 `BILLING_TEST_DATABASE_URL` 指向一个**可以被清空的**库，没设时 skip ——
**不要把 skipped 读成 passed**，CI 设了它并把 skipped 判成失败。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from decimal import Decimal
from typing import Any

import pytest
from payment_world import (
    NOW,
    Customer,
    PaymentRef,
    World,
    build_mysql_world,
    build_sqlite_world,
    close_by_review,
    insert_tax_version,
    make_paid_payment,
    seed_receipt_series,
)
from sqlalchemy import Engine, event, select, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session
from test_payment_review import RESOLVED_AT, resolve, serve_paid

from app.models.auth import DomainOutbox
from app.models.payments import Payment, PaymentResolution, PaymentStatus
from app.models.receipts import PaymentReceipt, ReceiptNumberSeries
from app.models.tenancy import Tenant
from app.models.wallet import Wallet
from app.payments.base import GatewayState
from app.services.payment_review import ReviewAction
from app.services.payments import AMOUNT_MISMATCH, ConfirmResult
from app.services.receipts import (
    EVENT_RECEIPT_EMAIL,
    MAX_SEQUENCE,
    RECEIPT_SERIES_UNAVAILABLE,
    RECEIPT_TAX_POLICY_UNREADABLE,
)

RECEIPTS_LOGGER = "app.services.receipts"
# NOW 与 RESOLVED_AT 的马来西亚年份。
YEAR = 2026


# --- 夹具 -----------------------------------------------------------------------------


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


# --- 构件 -----------------------------------------------------------------------------


def pending(world: World, tenant: Customer | None = None) -> PaymentRef:
    """A `PENDING` payment whose tax policy version can be read (开具检查 2 通过)."""
    version = insert_tax_version(world)
    return world.payment(tenant or world.customer(), tax_version_id=version)


def in_review(world: World, tenant: Customer | None = None) -> PaymentRef:
    """A `REVIEW_REQUIRED` payment (应收 100.00，`AMOUNT_MISMATCH`) with a readable tax policy."""
    version = insert_tax_version(world)
    payment = world.payment(
        tenant or world.customer(),
        status=PaymentStatus.REVIEW_REQUIRED,
        reason=AMOUNT_MISMATCH,
        tax_version_id=version,
    )
    with world.factory() as session:
        session.execute(
            update(Payment)
            .where(Payment.public_id == payment.public_id)
            .values(confirmed_amount=Decimal("99.99"), confirmed_currency="MYR")
        )
        session.commit()
    return payment


def with_payment_method(body: dict[str, Any], method: str | None) -> dict[str, Any]:
    """Stripe's `latest_charge.payment_method_details.type`; `None` leaves it absent."""
    if method is not None:
        body["payment_intent"]["latest_charge"]["payment_method_details"] = {"type": method}
    return body


def receipts(world: World) -> list[PaymentReceipt]:
    with world.factory() as session:
        rows = list(session.execute(select(PaymentReceipt).order_by(PaymentReceipt.id)).scalars())
        for row in rows:
            session.expunge(row)
        return rows


def receipt_emails(world: World) -> list[DomainOutbox]:
    with world.factory() as session:
        rows = list(
            session.execute(
                select(DomainOutbox)
                .where(DomainOutbox.event_type == EVENT_RECEIPT_EMAIL)
                .order_by(DomainOutbox.id)
            ).scalars()
        )
        for row in rows:
            session.expunge(row)
        return rows


def last_sequence(world: World, year: int = YEAR) -> int:
    with world.factory() as session:
        return int(
            session.execute(
                select(ReceiptNumberSeries.last_sequence).where(
                    ReceiptNumberSeries.series_year == year
                )
            ).scalar_one()
        )


def exhaust_series(world: World, year: int = YEAR) -> None:
    with world.factory() as session:
        session.execute(
            update(ReceiptNumberSeries)
            .where(ReceiptNumberSeries.series_year == year)
            .values(last_sequence=MAX_SEQUENCE)
        )
        session.commit()


def fail_receipt_writes(world: World) -> list[int]:
    """The stand-in database error: a flush that would insert a receipt raises `OperationalError`.

    返回每次触发时待插入的收据数，用来确认替身确实在开具的写入时触发。
    """
    fired: list[int] = []

    def before_flush(session: Session, _context: object, _instances: object) -> None:
        pending_receipts = [row for row in session.new if isinstance(row, PaymentReceipt)]
        if pending_receipts:
            fired.append(len(pending_receipts))
            raise OperationalError(
                "INSERT payment_receipts", {}, Exception("injected receipt write failure")
            )

    event.listen(world.factory, "before_flush", before_flush)
    return fired


def assert_no_receipt(world: World) -> None:
    assert receipts(world) == []
    assert receipt_emails(world) == []
    assert last_sequence(world) == 0


def assert_refused_once(caplog: pytest.LogCaptureFixture, payment: PaymentRef, code: str) -> None:
    errors = [
        record
        for record in caplog.records
        if record.name == RECEIPTS_LOGGER and record.levelno == logging.ERROR
    ]
    assert [(r.reason, r.payment_id) for r in errors] == [  # type: ignore[attr-defined]
        (code, payment.public_id)
    ]


# --- 自动确认：判定通过 ------------------------------------------------------------------


@pytest.mark.parametrize("method", ["fpx", "card", None], ids=["fpx", "card", "absent"])
def test_a_confirmed_payment_gets_its_receipt_and_email_in_the_same_commit(
    receipt_world: World, method: str | None
) -> None:
    """判定通过：PAID、TOPUP、收据与付款邮件行都提交；付款方式是 Stripe 这次返回的值。"""
    world = receipt_world
    tenant = world.customer()
    payment = pending(world, tenant)
    with_payment_method(world.serve(payment, GatewayState.PAID), method)

    result = world.confirm(payment)

    # 收据不改变确认的结果。
    assert result == ConfirmResult(
        payment_id=payment.public_id,
        old_status=PaymentStatus.PENDING,
        new_status=PaymentStatus.PAID,
        reason=None,
        posted=True,
        state=GatewayState.PAID,
    )
    row = world.row(payment)
    [topup] = world.topups(payment)
    [receipt] = receipts(world)
    assert receipt.payment_id == row.id
    assert receipt.tenant_id == tenant.id
    assert receipt.tax_policy_version_id == row.tax_policy_version_id
    assert receipt.receipt_number == f"ACV-AIR-{YEAR}-000001"
    assert (receipt.series_year, receipt.sequence) == (YEAR, 1)
    assert receipt.amount == topup.amount == Decimal("100")
    assert receipt.issued_at == NOW
    snapshot = receipt.snapshot_json
    assert snapshot["payment"]["payment_id"] == payment.public_id
    assert snapshot["payment"]["payment_method"] == method
    assert snapshot["payment"]["amount"] == "100.00"
    assert snapshot["payment"]["gateway_reference"] == row.gateway_payment_intent_id
    assert last_sequence(world) == 1

    [email] = receipt_emails(world)
    assert email.aggregate_id == receipt.public_id
    payload = json.loads(email.payload_json or "{}")
    assert (payload["receipt_number"], payload["payment_id"]) == (
        receipt.receipt_number,
        payment.public_id,
    )
    assert email.created_at == NOW


def test_the_next_confirmed_payment_takes_the_next_number(receipt_world: World) -> None:
    world = receipt_world
    first, second = pending(world), pending(world)
    world.serve(first, GatewayState.PAID)
    world.serve(second, GatewayState.PAID)

    world.confirm(first)
    world.confirm(second)

    assert [r.receipt_number for r in receipts(world)] == [
        f"ACV-AIR-{YEAR}-000001",
        f"ACV-AIR-{YEAR}-000002",
    ]
    assert len(receipt_emails(world)) == 2


# --- 自动确认：不开具的分支 --------------------------------------------------------------


def test_a_payment_sent_to_review_gets_no_receipt(receipt_world: World) -> None:
    """进入 `REVIEW_REQUIRED`（少一分）：不入账，也不开具。"""
    world = receipt_world
    payment = pending(world)
    world.serve(payment, GatewayState.PAID, amount_total=9999)

    result = world.confirm(payment)

    assert (result.new_status, result.reason) == (PaymentStatus.REVIEW_REQUIRED, AMOUNT_MISMATCH)
    assert world.topups() == []
    assert_no_receipt(world)


@pytest.mark.parametrize(
    "state",
    [GatewayState.INCONSISTENT, GatewayState.FAILED, GatewayState.EXPIRED, GatewayState.OPEN],
    ids=lambda s: s.value,
)
def test_other_states_get_no_receipt(receipt_world: World, state: GatewayState) -> None:
    world = receipt_world
    payment = pending(world)
    world.serve(payment, state)

    world.confirm(payment)

    assert world.row(payment).status is not PaymentStatus.PAID
    assert_no_receipt(world)


def test_an_already_paid_payment_is_not_given_a_receipt_by_confirmation(
    receipt_world: World,
) -> None:
    """已是 `PAID`（开具检查本来都过得了）：确认只更新 last_reconciled_at，不调用开具。

    没有收据的 `PAID` 由补发扫描收口（AIH-TASK-201）。
    """
    world = receipt_world
    version = insert_tax_version(world)
    payment = make_paid_payment(world, world.customer(), tax_version_id=version)
    world.serve(payment, GatewayState.PAID)

    result = world.confirm(payment)

    assert (result.old_status, result.new_status, result.posted) == (
        PaymentStatus.PAID,
        PaymentStatus.PAID,
        False,
    )
    assert_no_receipt(world)


def test_a_payment_closed_by_review_is_not_given_a_receipt(receipt_world: World) -> None:
    world = receipt_world
    payment = in_review(world)
    close_by_review(world, payment)
    world.serve(payment, GatewayState.PAID)

    result = world.confirm(payment)

    assert (result.new_status, result.posted) == (PaymentStatus.FAILED, False)
    assert_no_receipt(world)


# --- 自动确认：开具检查不过 --------------------------------------------------------------


def test_an_exhausted_series_does_not_stop_the_confirmation(
    receipt_world: World, caplog: pytest.LogCaptureFixture
) -> None:
    """检查不过（`RECEIPT_SERIES_UNAVAILABLE`）：入账照常提交，没有收据与邮件行，结果不变。"""
    world = receipt_world
    payment = pending(world)
    world.serve(payment, GatewayState.PAID)
    exhaust_series(world)

    with caplog.at_level(logging.INFO, logger=RECEIPTS_LOGGER):
        result = world.confirm(payment)

    assert (result.new_status, result.reason, result.posted) == (PaymentStatus.PAID, None, True)
    assert world.row(payment).status is PaymentStatus.PAID
    assert len(world.topups(payment)) == 1
    assert len(world.audits(payment)) == 1
    assert receipts(world) == [] and receipt_emails(world) == []
    assert last_sequence(world) == MAX_SEQUENCE
    assert_refused_once(caplog, payment, RECEIPT_SERIES_UNAVAILABLE)


def test_an_unreadable_tax_policy_does_not_stop_the_confirmation(
    receipt_world: World, caplog: pytest.LogCaptureFixture
) -> None:
    """检查不过（`RECEIPT_TAX_POLICY_UNREADABLE`：世界自带的那一版没有说明文字）：入账照常。"""
    world = receipt_world
    payment = world.payment(world.customer())
    world.serve(payment, GatewayState.PAID)

    with caplog.at_level(logging.INFO, logger=RECEIPTS_LOGGER):
        result = world.confirm(payment)

    assert (result.new_status, result.posted) == (PaymentStatus.PAID, True)
    assert len(world.topups(payment)) == 1
    assert_no_receipt(world)
    assert_refused_once(caplog, payment, RECEIPT_TAX_POLICY_UNREADABLE)


# --- 自动确认：开具时的数据库错误 --------------------------------------------------------


def test_a_database_error_while_issuing_rolls_the_confirmation_back(receipt_world: World) -> None:
    """INV-13：异常传出 `confirm_payment`（不归为 `ConfirmConflict`），支付、账本、审计、出站事件与
    收据都不变，系列行不前进。"""
    world = receipt_world
    payment = pending(world)
    world.serve(payment, GatewayState.PAID)
    before = world.snapshot(payment)
    fired = fail_receipt_writes(world)

    with pytest.raises(OperationalError, match="injected receipt write failure"):
        world.confirm(payment)

    assert fired == [1]
    assert world.snapshot(payment) == before
    assert world.row(payment).status is PaymentStatus.PENDING
    assert world.topups() == []
    assert_no_receipt(world)


# --- 人工入账 ---------------------------------------------------------------------------


def test_a_manual_credit_gets_a_receipt_for_what_stripe_received(receipt_world: World) -> None:
    """CREDIT：应收 100.00、实收 99.99 → 收据金额 99.99（等于 TOPUP），有付款邮件行；
    付款方式取第 2 步那次查询的值；返回值不变。"""
    world = receipt_world
    tenant = world.customer()
    payment = in_review(world, tenant)
    with_payment_method(serve_paid(world, payment, amount_total=9999), "card")

    returned = resolve(world, payment, ReviewAction.CREDIT)

    assert returned == payment.public_id
    row = world.row(payment)
    assert (row.status, row.resolution) == (PaymentStatus.PAID, PaymentResolution.CREDITED)
    assert row.expected_amount == Decimal("100")
    [topup] = world.topups(payment)
    [receipt] = receipts(world)
    assert receipt.payment_id == row.id
    assert receipt.tenant_id == tenant.id
    assert receipt.amount == topup.amount == Decimal("99.99")
    assert receipt.snapshot_json["payment"]["amount"] == "99.99"
    assert receipt.snapshot_json["payment"]["payment_method"] == "card"
    assert receipt.receipt_number == f"ACV-AIR-{YEAR}-000001"
    assert receipt.issued_at == RESOLVED_AT
    [email] = receipt_emails(world)
    assert email.aggregate_id == receipt.public_id
    assert json.loads(email.payload_json or "{}")["amount"] == "99.99"


def test_a_manual_close_gets_no_receipt(receipt_world: World) -> None:
    world = receipt_world
    payment = in_review(world)
    serve_paid(world, payment)

    resolve(world, payment, ReviewAction.CLOSE)

    assert world.row(payment).resolution is PaymentResolution.CLOSED
    assert_no_receipt(world)


def test_a_failed_receipt_check_does_not_stop_the_manual_credit(
    receipt_world: World, caplog: pytest.LogCaptureFixture
) -> None:
    """CREDIT 时开具检查不过：入账照常提交，返回值不变，没有收据与邮件行。"""
    world = receipt_world
    payment = in_review(world)
    serve_paid(world, payment)
    exhaust_series(world)

    with caplog.at_level(logging.INFO, logger=RECEIPTS_LOGGER):
        returned = resolve(world, payment, ReviewAction.CREDIT)

    assert returned == payment.public_id
    row = world.row(payment)
    assert (row.status, row.resolution) == (PaymentStatus.PAID, PaymentResolution.CREDITED)
    assert len(world.topups(payment)) == 1
    assert receipts(world) == [] and receipt_emails(world) == []
    assert last_sequence(world) == MAX_SEQUENCE
    assert_refused_once(caplog, payment, RECEIPT_SERIES_UNAVAILABLE)


def test_a_database_error_while_issuing_rolls_the_manual_credit_back(
    receipt_world: World,
) -> None:
    """第 6 步的回滚规则不变：异常原样传出 `resolve_review`，支付仍在复核、无账本行与收据。"""
    world = receipt_world
    payment = in_review(world)
    serve_paid(world, payment)
    before = world.snapshot(payment)
    fired = fail_receipt_writes(world)

    with pytest.raises(OperationalError, match="injected receipt write failure"):
        resolve(world, payment, ReviewAction.CREDIT)

    assert fired == [1]
    assert world.snapshot(payment) == before
    row = world.row(payment)
    assert (row.status, row.resolution) == (PaymentStatus.REVIEW_REQUIRED, None)
    assert world.topups() == []
    assert_no_receipt(world)


# --- 真 MySQL：锁顺序 -------------------------------------------------------------------

_LOCKED_TABLES = [
    Payment.__tablename__,
    Wallet.__tablename__,
    Tenant.__tablename__,
    ReceiptNumberSeries.__tablename__,
]


def _record_locks(engine: Engine) -> list[str]:
    """The tables of `SELECT … FOR UPDATE` statements on `engine`, first time each, in order.

    整个用例只在 MySQL 上跑（SQLite 不编译 FOR UPDATE），匹配的是 MySQL 编译出的文本。
    """
    seen: list[str] = []

    def listener(conn, cursor, statement, parameters, context, executemany) -> None:
        sql = " ".join(statement.replace("`", "").lower().split())
        if not (sql.startswith("select ") and sql.endswith(" for update")):
            return
        for table in _LOCKED_TABLES:
            if f" from {table} " in sql and table not in seen:
                seen.append(table)

    event.listen(engine, "before_cursor_execute", listener)
    return seen


@pytest.mark.parametrize("path", ["confirm", "credit"])
def test_the_series_row_is_locked_last(mysql_receipt_world: World, path: str) -> None:
    """锁顺序「支付 → 钱包 → 租户 → 当年系列行」：只在末尾多一把系列行的锁。"""
    world = mysql_receipt_world
    engine = world.factory.kw["bind"]
    if path == "confirm":
        payment = pending(world)
        world.serve(payment, GatewayState.PAID)
        locked = _record_locks(engine)
        world.confirm(payment)
    else:
        payment = in_review(world)
        serve_paid(world, payment)
        locked = _record_locks(engine)
        resolve(world, payment, ReviewAction.CREDIT)

    assert locked == _LOCKED_TABLES
    assert len(receipts(world)) == 1

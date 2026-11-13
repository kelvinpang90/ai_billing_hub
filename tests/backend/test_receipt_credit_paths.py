"""Receipts issued in the crediting transaction (design gate #589 v2, AIH-TASK-200).

设计 §2「对 A 的 confirm_payment 与 B 的 resolve_review 的修改」；§7 标 200 的两行：

- **同事务（自动确认）**：`confirm_payment` 判定通过（支付 `PAID`、TOPUP、收据、邮件行同时提交，
  付款方式取 Stripe 返回值）；进入 `REVIEW_REQUIRED`、其他状态、已是 `PAID`、已结案时不开具；
  开具检查不过时入账照常、没有收据与邮件行；开具写入时注入数据库错误 → 异常传出
  `confirm_payment`，支付、账本与收据都不变；
- **同事务（人工入账）**：B 的 CREDIT 成功（收据金额等于 Stripe 实收，可能不等于
  `expected_amount`，有邮件行）；CLOSE 没有收据；CREDIT 时开具检查不过入账照常；开具写入时注入
  数据库错误 → 异常传出 `resolve_review`，什么都不变。

注入数据库错误不断言事件状态：A 的事件处理对这类异常的行为不变（设计 §2「开具」）。

支付、租户、假 Stripe 与税务政策版本用共用的 payment_world.py；本文件的夹具只在这里用，定义在
这里。Stripe 一律是注入了假传输的 `StripeGateway`，不联网；时间一律注入，不 sleep。

MySQL 那部分需要 `BILLING_TEST_DATABASE_URL`，没设时 skip ——
**不要把 skipped 读成 passed**，CI 设了它并把 skipped 判成失败。
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from decimal import Decimal
from typing import Any

import pytest
from payment_world import (
    NOW,
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

from app.models.auth import DomainOutbox, User
from app.models.payments import PaymentResolution, PaymentStatus
from app.models.receipts import PaymentReceipt, ReceiptNumberSeries
from app.payments.base import GatewayState
from app.services.auth import RequestContext
from app.services.payment_review import ReviewAction, resolve_review
from app.services.payments import AMOUNT_MISMATCH, ConfirmResult
from app.services.receipts import (
    AGGREGATE_PAYMENT_RECEIPT,
    EVENT_RECEIPT_EMAIL,
    RECEIPT_SERIES_UNAVAILABLE,
    RECEIPT_TAX_POLICY_UNREADABLE,
)

RECEIPTS_LOGGER = "app.services.receipts"
# 固定值而不是 utc_now()：断言「存进去的就是调用方给的那个时刻」要能逐字比对。
RESOLVED_AT = dt.datetime(2026, 11, 9, 10, 0, 0)
CONTEXT = RequestContext(ip_address="203.0.113.9", user_agent="receipt-credit-path-test")
REASON = "Fictional review note for a receipt test"


# --- 夹具 ---------------------------------------------------------------


@pytest.fixture(params=["sqlite", "mysql"])
def receipt_world(request, monkeypatch) -> Iterator[World]:
    worlds = build_mysql_world(monkeypatch) if request.param == "mysql" else build_sqlite_world()
    for world in worlds:
        # `create_all` 建的 SQLite 库没有系列行；MySQL 上迁移 0039 已写过（这里什么都不做）。
        seed_receipt_series(world.factory)
        yield world


# --- 构件 ---------------------------------------------------------------


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
                .where(DomainOutbox.aggregate_type == AGGREGATE_PAYMENT_RECEIPT)
                .order_by(DomainOutbox.id)
            ).scalars()
        )
        for row in rows:
            session.expunge(row)
        return rows


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


def everything(world: World, payment: PaymentRef) -> dict[str, Any]:
    """The payment, the counts of ledger / audit / outbox rows, the receipts and the series."""
    return {
        **world.snapshot(payment),
        "topups": [(row.id, row.amount) for row in world.topups(payment)],
        "receipts": [(row.id, row.receipt_number) for row in receipts(world)],
        "series": series(world),
    }


def payable(world: World, **options: Any) -> PaymentRef:
    """A payment whose tax policy version `parse_treatment` reads (so a receipt can be issued)."""
    return world.payment(world.customer(), tax_version_id=insert_tax_version(world), **options)


def _inserts_a_receipt(statement: str) -> bool:
    # 只比两种库相同的片段：去掉标识符引号、统一大小写与空白。
    sql = " ".join(statement.replace("`", "").replace('"', "").lower().split())
    return sql.startswith("insert into payment_receipts ")


@contextmanager
def receipt_insert_fails(world: World) -> Iterator[list[str]]:
    """A stand-in database error: inserting the receipt raises `OperationalError`.

    返回的列表记下被拦下的语句（证明错误确实发生在开具的写入上）。
    """
    engine = world.factory.kw["bind"]
    assert isinstance(engine, Engine)
    stopped: list[str] = []

    def listener(conn, cursor, statement, parameters, context, executemany) -> None:
        if _inserts_a_receipt(statement):
            stopped.append(statement)
            raise OperationalError(statement, parameters, Exception("injected receipt failure"))

    event.listen(engine, "before_cursor_execute", listener)
    try:
        yield stopped
    finally:
        event.remove(engine, "before_cursor_execute", listener)


# --- 同事务（自动确认） --------------------------------------------------


def test_a_confirmed_payment_gets_its_receipt_and_email_in_the_same_commit(
    receipt_world: World,
) -> None:
    world = receipt_world
    payment = payable(world)
    world.serve(payment, GatewayState.PAID, payment_method="fpx")

    result = world.confirm(payment)

    assert result == ConfirmResult(
        payment_id=payment.public_id,
        old_status=PaymentStatus.PENDING,
        new_status=PaymentStatus.PAID,
        reason=None,
        posted=True,
        state=GatewayState.PAID,
    )
    row = world.row(payment)
    assert row.status is PaymentStatus.PAID
    [topup] = world.topups(payment)
    [receipt] = receipts(world)
    assert receipt.payment_id == row.id
    assert receipt.receipt_number == "ACV-AIR-2026-000001"
    assert receipt.amount == topup.amount == Decimal("100.00")
    assert receipt.issued_at == NOW
    snapshot = receipt.snapshot_json
    assert snapshot["payment"]["payment_method"] == "fpx"
    assert snapshot["payment"]["paid_at"] == NOW.isoformat(timespec="seconds")
    assert snapshot["payment"]["gateway_reference"] == row.gateway_payment_intent_id
    assert series(world)[2026] == 1
    [email] = receipt_emails(world)
    assert email.event_type == EVENT_RECEIPT_EMAIL
    assert email.aggregate_id == receipt.public_id
    payload = json.loads(email.payload_json or "{}")
    assert (payload["receipt_number"], payload["amount"]) == ("ACV-AIR-2026-000001", "100.00")
    assert payload["payment_id"] == payment.public_id


def test_without_a_payment_method_from_stripe_the_receipt_has_none(receipt_world: World) -> None:
    world = receipt_world
    payment = payable(world)
    world.serve(payment, GatewayState.PAID)

    world.confirm(payment)

    [receipt] = receipts(world)
    assert receipt.snapshot_json["payment"]["payment_method"] is None
    assert len(receipt_emails(world)) == 1


def test_a_payment_sent_to_review_gets_no_receipt(receipt_world: World) -> None:
    world = receipt_world
    payment = payable(world)
    world.serve(payment, GatewayState.PAID, amount_total=9999, payment_method="card")
    before = series(world)

    result = world.confirm(payment)

    assert (result.new_status, result.reason) == (PaymentStatus.REVIEW_REQUIRED, AMOUNT_MISMATCH)
    assert receipts(world) == []
    assert receipt_emails(world) == []
    assert series(world) == before


@pytest.mark.parametrize(
    "state",
    [GatewayState.FAILED, GatewayState.EXPIRED, GatewayState.OPEN, GatewayState.INCONSISTENT],
    ids=lambda s: s.value,
)
def test_other_gateway_states_issue_no_receipt(receipt_world: World, state: GatewayState) -> None:
    world = receipt_world
    payment = payable(world)
    world.serve(payment, state)
    before = series(world)

    result = world.confirm(payment)

    assert result.new_status is not PaymentStatus.PAID
    assert receipts(world) == []
    assert receipt_emails(world) == []
    assert series(world) == before


def test_an_already_paid_payment_is_not_given_a_receipt_by_confirmation(
    receipt_world: World,
) -> None:
    """已是 `PAID`（例如本批上线前入账、还没有收据）：确认只更新 `last_reconciled_at`，不开具；
    这样的支付归补发扫描（AIH-TASK-201）。"""
    world = receipt_world
    payment = make_paid_payment(world, world.customer(), tax_version_id=insert_tax_version(world))
    world.serve(payment, GatewayState.PAID, payment_method="card")

    result = world.confirm(payment)

    assert (result.old_status, result.new_status, result.posted) == (
        PaymentStatus.PAID,
        PaymentStatus.PAID,
        False,
    )
    assert receipts(world) == []
    assert receipt_emails(world) == []


def test_a_payment_closed_by_review_is_not_given_a_receipt(receipt_world: World) -> None:
    world = receipt_world
    payment = payable(world)
    close_by_review(world, payment)
    world.serve(payment, GatewayState.PAID)

    result = world.confirm(payment)

    assert result.new_status is PaymentStatus.FAILED
    assert receipts(world) == []
    assert receipt_emails(world) == []


def _assert_credited_without_receipt(world: World, payment: PaymentRef) -> None:
    row = world.row(payment)
    assert row.status is PaymentStatus.PAID
    [topup] = world.topups(payment)
    assert topup.amount == Decimal("100.00")
    assert receipts(world) == []
    assert receipt_emails(world) == []


def test_a_failed_issue_check_does_not_stop_the_confirmation(
    receipt_world: World, caplog: pytest.LogCaptureFixture
) -> None:
    """开具检查不过（税务政策读不出）：入账照常提交、结果不变，没有收据与邮件行。"""
    world = receipt_world
    # world 自带的那一版没有 `document_notice`：`parse_treatment` 读不出。
    payment = world.payment(world.customer())
    world.serve(payment, GatewayState.PAID, payment_method="fpx")
    before = series(world)

    with caplog.at_level(logging.ERROR, logger=RECEIPTS_LOGGER):
        result = world.confirm(payment)

    assert result == ConfirmResult(
        payment_id=payment.public_id,
        old_status=PaymentStatus.PENDING,
        new_status=PaymentStatus.PAID,
        reason=None,
        posted=True,
        state=GatewayState.PAID,
    )
    _assert_credited_without_receipt(world, payment)
    assert series(world) == before
    [record] = [r for r in caplog.records if r.name == RECEIPTS_LOGGER]
    assert record.__dict__["reason"] == RECEIPT_TAX_POLICY_UNREADABLE


def test_an_exhausted_series_does_not_stop_the_confirmation(
    receipt_world: World, caplog: pytest.LogCaptureFixture
) -> None:
    world = receipt_world
    set_series(world, 2026, 999999)
    payment = payable(world)
    world.serve(payment, GatewayState.PAID)

    with caplog.at_level(logging.ERROR, logger=RECEIPTS_LOGGER):
        result = world.confirm(payment)

    assert (result.new_status, result.posted) == (PaymentStatus.PAID, True)
    _assert_credited_without_receipt(world, payment)
    assert series(world)[2026] == 999999
    [record] = [r for r in caplog.records if r.name == RECEIPTS_LOGGER]
    assert record.__dict__["reason"] == RECEIPT_SERIES_UNAVAILABLE


def test_a_database_error_while_issuing_rolls_the_confirmation_back(receipt_world: World) -> None:
    """开具写入时数据库出错：异常传出 `confirm_payment`，支付、账本、收据、系列与邮件行都不变。"""
    world = receipt_world
    payment = payable(world)
    world.serve(payment, GatewayState.PAID, payment_method="fpx")
    before = everything(world, payment)

    with receipt_insert_fails(world) as stopped, pytest.raises(OperationalError):
        world.confirm(payment)

    assert len(stopped) == 1
    assert everything(world, payment) == before
    assert world.row(payment).status is PaymentStatus.PENDING
    assert world.topups(payment) == []
    assert receipts(world) == []


# --- 同事务（人工入账） --------------------------------------------------


def admin_of(world: World) -> User:
    with world.factory() as session:
        user = session.get(User, world.admin_id)
        assert user is not None
        session.expunge(user)
        return user


def in_review(world: World, *, tax_version_id: int | None = None) -> PaymentRef:
    """A `REVIEW_REQUIRED` payment (应收 100.00) as A's confirmation would have left it."""
    return world.payment(
        world.customer(),
        status=PaymentStatus.REVIEW_REQUIRED,
        reason=AMOUNT_MISMATCH,
        tax_version_id=tax_version_id,
    )


def serve_received(world: World, payment: PaymentRef, **overrides: Any) -> None:
    """Stripe reports the payment paid, nothing refunded, no dispute (B 的核对都通过)."""
    body = world.serve(payment, GatewayState.PAID, **overrides)
    latest = body["payment_intent"]["latest_charge"]
    latest["amount_refunded"] = 0
    latest["disputed"] = False


def resolve(world: World, payment: PaymentRef, action: ReviewAction, *, gateway: Any) -> str:
    return resolve_review(
        world.factory,
        gateway,
        actor=admin_of(world),
        payment_id=payment.public_id,
        action=action,
        reason=REASON,
        context=CONTEXT,
        now=RESOLVED_AT,
    )


def test_credit_issues_a_receipt_for_what_stripe_received(receipt_world: World) -> None:
    """应收 100.00、实收 99.99：收据金额 99.99（等于 TOPUP），付款方式取第 2 步那次查询。"""
    world = receipt_world
    payment = in_review(world, tax_version_id=insert_tax_version(world))
    serve_received(world, payment, amount_total=9999, payment_method="card")

    returned = resolve(world, payment, ReviewAction.CREDIT, gateway=world.stripe.gateway())

    assert returned == payment.public_id
    assert world.stripe.asked == [payment.session_id]
    row = world.row(payment)
    assert (row.status, row.resolution) == (PaymentStatus.PAID, PaymentResolution.CREDITED)
    assert row.expected_amount == Decimal("100.00")
    [topup] = world.topups(payment)
    [receipt] = receipts(world)
    assert receipt.payment_id == row.id
    assert receipt.amount == topup.amount == Decimal("99.99")
    assert receipt.issued_at == RESOLVED_AT
    assert receipt.receipt_number == "ACV-AIR-2026-000001"
    assert receipt.snapshot_json["payment"]["amount"] == "99.99"
    assert receipt.snapshot_json["payment"]["payment_method"] == "card"
    assert receipt.snapshot_json["payment"]["paid_at"] == RESOLVED_AT.isoformat(timespec="seconds")
    assert series(world)[2026] == 1
    [email] = receipt_emails(world)
    assert email.aggregate_id == receipt.public_id
    assert json.loads(email.payload_json or "{}")["amount"] == "99.99"


def test_close_issues_no_receipt(receipt_world: World) -> None:
    world = receipt_world
    payment = in_review(world, tax_version_id=insert_tax_version(world))
    before = series(world)

    resolve(world, payment, ReviewAction.CLOSE, gateway=None)

    assert world.row(payment).status is PaymentStatus.FAILED
    assert receipts(world) == []
    assert receipt_emails(world) == []
    assert series(world) == before


def test_a_failed_issue_check_does_not_stop_the_credit(
    receipt_world: World, caplog: pytest.LogCaptureFixture
) -> None:
    """开具检查不过（税务政策读不出）：入账照常、返回值不变，没有收据与邮件行。"""
    world = receipt_world
    payment = in_review(world)
    serve_received(world, payment, payment_method="fpx")

    with caplog.at_level(logging.ERROR, logger=RECEIPTS_LOGGER):
        returned = resolve(world, payment, ReviewAction.CREDIT, gateway=world.stripe.gateway())

    assert returned == payment.public_id
    row = world.row(payment)
    assert (row.status, row.resolution) == (PaymentStatus.PAID, PaymentResolution.CREDITED)
    [topup] = world.topups(payment)
    assert topup.amount == Decimal("100.00")
    assert receipts(world) == []
    assert receipt_emails(world) == []
    [record] = [r for r in caplog.records if r.name == RECEIPTS_LOGGER]
    assert record.__dict__["reason"] == RECEIPT_TAX_POLICY_UNREADABLE


def test_a_database_error_while_issuing_rolls_the_credit_back(receipt_world: World) -> None:
    """开具写入时数据库出错：异常传出 `resolve_review`（第 6 步不归类），整个事务回滚。"""
    world = receipt_world
    payment = in_review(world, tax_version_id=insert_tax_version(world))
    serve_received(world, payment, payment_method="card")
    before = everything(world, payment)

    with receipt_insert_fails(world) as stopped, pytest.raises(OperationalError):
        resolve(world, payment, ReviewAction.CREDIT, gateway=world.stripe.gateway())

    assert len(stopped) == 1
    assert everything(world, payment) == before
    row = world.row(payment)
    assert (row.status, row.resolution) == (PaymentStatus.REVIEW_REQUIRED, None)
    assert world.topups(payment) == []
    assert receipts(world) == []

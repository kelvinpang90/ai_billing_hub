"""Receipts issued in the crediting transaction (design gate #589 v2, AIH-TASK-200).

设计 §2「对 A 的 confirm_payment 与 B 的 resolve_review 的修改」；§7 标 200 的两行：

- **同事务（自动确认）**：`confirm_payment` 判定通过（支付 `PAID`、TOPUP、收据、邮件行同时提交，
  付款方式取 Stripe 返回值）；进入 `REVIEW_REQUIRED` 与其他不入账的分支不开具；开具检查不过时
  入账照常；开具写入时注入数据库错误 → 异常传出、支付与账本与收据都不变；
- **同事务（人工入账）**：B 的 CREDIT 成功（收据金额等于 Stripe 实收、有邮件行）；CLOSE 没有
  收据；CREDIT 时开具检查不过入账照常；注入数据库错误同上。

数据库错误的替身：在开具写收据行（`INSERT INTO payment_receipts`）时抛 `OperationalError`。
不断言事件状态：A 的事件处理对这类异常的行为不变。

支付、租户与假 Stripe 用共用的 payment_world.py；本文件的夹具只在这里用，定义在这里。
Stripe 一律是注入了假传输的 `StripeGateway`，不联网；时间一律注入，不 sleep。

MySQL 那部分需要 `BILLING_TEST_DATABASE_URL`，没设时 skip ——
**不要把 skipped 读成 passed**，CI 设了它并把 skipped 判成失败。
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Callable, Iterator
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
    seed_receipt_series,
)
from sqlalchemy import Engine, event, select, update
from sqlalchemy.exc import OperationalError

from app.models.auth import DomainOutbox, User
from app.models.payments import Payment, PaymentResolution, PaymentStatus
from app.models.receipts import PaymentReceipt, ReceiptNumberSeries
from app.payments.base import GatewayState
from app.services import payment_review as review_service
from app.services import payments as payments_service
from app.services.auth import RequestContext
from app.services.payment_review import ReviewAction, resolve_review
from app.services.payments import AMOUNT_MISMATCH, GATEWAY_INCONSISTENT
from app.services.receipts import AGGREGATE_PAYMENT_RECEIPT, EVENT_RECEIPT_EMAIL

RESOLVED_AT = dt.datetime(2026, 11, 9, 10, 0, 0)
CONTEXT = RequestContext(ip_address="203.0.113.9", user_agent="receipt-credit-paths-test")
REASON = "Fictional review note for a receipt test"


# --- 夹具 ---------------------------------------------------------------


def _with_series(worlds: Iterator[World]) -> Iterator[World]:
    # `create_all` 建的 SQLite 库没有系列行；MySQL 上迁移 0039 已写过（这里什么都不做）。
    for world in worlds:
        seed_receipt_series(world.factory)
        yield world


@pytest.fixture(params=["sqlite", "mysql"])
def credit_world(request, monkeypatch) -> Iterator[World]:
    worlds = build_mysql_world(monkeypatch) if request.param == "mysql" else build_sqlite_world()
    yield from _with_series(worlds)


# --- 构件 ---------------------------------------------------------------


def readable_payment(
    world: World,
    tenant: Customer | None = None,
    *,
    status: PaymentStatus = PaymentStatus.PENDING,
    reason: str | None = None,
) -> PaymentRef:
    """A payment whose tax policy version `parse_treatment` reads (world 自带的那一版读不出)."""
    return world.payment(
        tenant or world.customer(),
        status=status,
        reason=reason,
        tax_version_id=insert_tax_version(world),
    )


def serve_paid(
    world: World, payment: PaymentRef, *, method: str | None = "fpx", **overrides: Any
) -> dict[str, Any]:
    """A paid session whose latest charge carries the refund, dispute and payment method fields."""
    body = world.serve(payment, GatewayState.PAID, **overrides)
    charge = body["payment_intent"]["latest_charge"]
    charge["amount_refunded"] = 0
    charge["disputed"] = False
    if method is not None:
        charge["payment_method_details"] = {"type": method}
    return body


def in_review(world: World, *, readable: bool = True) -> PaymentRef:
    """A `REVIEW_REQUIRED` payment (应收 100.00, Stripe 报 99.99) as A would have left it."""
    tenant = world.customer()
    if readable:
        payment = readable_payment(
            world, tenant, status=PaymentStatus.REVIEW_REQUIRED, reason=AMOUNT_MISMATCH
        )
    else:
        payment = world.payment(
            tenant, status=PaymentStatus.REVIEW_REQUIRED, reason=AMOUNT_MISMATCH
        )
    with world.factory() as session:
        session.execute(
            update(Payment)
            .where(Payment.public_id == payment.public_id)
            .values(confirmed_amount=Decimal("99.99"), confirmed_currency="MYR")
        )
        session.commit()
    return payment


def resolve(world: World, payment: PaymentRef, action: ReviewAction) -> str:
    with world.factory() as session:
        admin = session.get(User, world.admin_id)
        assert admin is not None
        session.expunge(admin)
    return resolve_review(
        world.factory,
        world.stripe.gateway(),
        actor=admin,
        payment_id=payment.public_id,
        action=action,
        reason=REASON,
        context=CONTEXT,
        now=RESOLVED_AT,
    )


def receipt_of(world: World, payment: PaymentRef) -> PaymentReceipt | None:
    with world.factory() as session:
        row = session.execute(
            select(PaymentReceipt)
            .join(Payment, Payment.id == PaymentReceipt.payment_id)
            .where(Payment.public_id == payment.public_id)
        ).scalar_one_or_none()
        if row is not None:
            session.expunge(row)
        return row


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


def receipt_state(world: World) -> dict[str, Any]:
    """Every receipt row and series counter, as committed."""
    with world.factory() as session:
        receipts = session.execute(
            select(PaymentReceipt.id, PaymentReceipt.receipt_number).order_by(PaymentReceipt.id)
        ).all()
        series = session.execute(
            select(ReceiptNumberSeries.series_year, ReceiptNumberSeries.last_sequence)
        ).all()
    return {
        "receipts": [tuple(row) for row in receipts],
        "series": {int(year): int(last) for year, last in series},
    }


def exhaust_series(world: World) -> None:
    """2026 年的系列已用完（MySQL 上清场时 `clean_receipts` 把它归零）."""
    with world.factory() as session:
        session.execute(
            update(ReceiptNumberSeries)
            .where(ReceiptNumberSeries.series_year == 2026)
            .values(last_sequence=999999)
        )
        session.commit()


class FailingReceiptWrite:
    """A stand-in for the database failing the receipt insert: raises `OperationalError`.

    挂在 world 的引擎上：语句真正发到库之前抛出，什么都没写；调用方的事务照常回滚。
    """

    def __init__(self, world: World) -> None:
        self.engine: Engine = world.factory.kw["bind"]
        self.raised = 0

    def _listener(self, conn, cursor, statement, parameters, context, executemany) -> None:
        normalized = " ".join(statement.replace("`", "").replace('"', "").lower().split())
        if normalized.startswith("insert into payment_receipts "):
            self.raised += 1
            raise OperationalError(statement, parameters, Exception("injected receipt failure"))

    def __enter__(self) -> FailingReceiptWrite:
        event.listen(self.engine, "before_cursor_execute", self._listener)
        return self

    def __exit__(self, *exc: object) -> None:
        event.remove(self.engine, "before_cursor_execute", self._listener)


def spy_on(monkeypatch: pytest.MonkeyPatch, module: Any) -> list[str]:
    """Record the payments `module.issue_receipt` is called for; the real one still runs."""
    calls: list[str] = []
    real = module.issue_receipt

    def spy(session, *, payment, **options):
        calls.append(payment.public_id)
        return real(session, payment=payment, **options)

    monkeypatch.setattr(module, "issue_receipt", spy)
    return calls


# --- 同事务（自动确认） -----------------------------------------------------


@pytest.mark.parametrize("method", ["fpx", "card", None], ids=["fpx", "card", "none"])
def test_a_confirmation_commits_the_receipt_and_its_email_with_the_topup(
    credit_world: World, monkeypatch, method: str | None
) -> None:
    """判定通过：`PAID`、TOPUP、收据与邮件行同时提交；付款方式取这次 Stripe 查询的值。"""
    world = credit_world
    payment = readable_payment(world)
    serve_paid(world, payment, method=method)
    calls = spy_on(monkeypatch, payments_service)

    result = world.confirm(payment)

    assert (result.new_status, result.posted) == (PaymentStatus.PAID, True)
    assert calls == [payment.public_id]
    row = world.row(payment)
    [topup] = world.topups(payment)
    receipt = receipt_of(world, payment)
    assert receipt is not None
    assert receipt.receipt_number == "ACV-AIR-2026-000001"
    assert receipt.payment_id == row.id
    assert receipt.tenant_id == row.tenant_id
    assert receipt.amount == topup.amount == Decimal("100.00")
    assert receipt.issued_at == NOW
    assert receipt.snapshot_json["payment"]["payment_method"] == method
    assert receipt.snapshot_json["payment"]["paid_at"] == NOW.isoformat(timespec="seconds")
    assert receipt.snapshot_json["payment"]["gateway_reference"] == row.gateway_payment_intent_id
    [email] = receipt_emails(world)
    assert (email.event_type, email.aggregate_id) == (EVENT_RECEIPT_EMAIL, receipt.public_id)
    payload = json.loads(email.payload_json or "{}")
    assert (payload["receipt_number"], payload["payment_id"]) == (
        receipt.receipt_number,
        payment.public_id,
    )
    assert receipt_state(world)["series"][2026] == 1


def _to_review(world: World, payment: PaymentRef) -> None:
    serve_paid(world, payment, amount_total=9999)


def _to_failed(world: World, payment: PaymentRef) -> None:
    world.serve(payment, GatewayState.FAILED)


def _to_inconsistent(world: World, payment: PaymentRef) -> None:
    world.serve(payment, GatewayState.INCONSISTENT)


def _still_open(world: World, payment: PaymentRef) -> None:
    world.serve(payment, GatewayState.OPEN)


@pytest.mark.parametrize(
    ("serve", "status"),
    [
        (_to_review, PaymentStatus.REVIEW_REQUIRED),
        (_to_inconsistent, PaymentStatus.REVIEW_REQUIRED),
        (_to_failed, PaymentStatus.FAILED),
        (_still_open, PaymentStatus.PENDING),
    ],
    ids=["amount-mismatch", "inconsistent", "failed", "open"],
)
def test_a_confirmation_that_does_not_credit_issues_no_receipt(
    credit_world: World,
    monkeypatch,
    serve: Callable[[World, PaymentRef], None],
    status: PaymentStatus,
) -> None:
    """进入 `REVIEW_REQUIRED` 或其他状态：不调用开具，没有收据与邮件行。"""
    world = credit_world
    payment = readable_payment(world)
    serve(world, payment)
    before = receipt_state(world)
    calls = spy_on(monkeypatch, payments_service)

    result = world.confirm(payment)

    assert (result.new_status, result.posted) == (status, False)
    assert calls == []
    assert receipt_of(world, payment) is None
    assert receipt_emails(world) == []
    assert receipt_state(world) == before


@pytest.mark.parametrize("closed", [False, True], ids=["already-paid", "closed-by-review"])
def test_a_paid_or_resolved_payment_is_not_issued_again_by_a_confirmation(
    credit_world: World, monkeypatch, closed: bool
) -> None:
    """已是 `PAID`（上线前没有收据的那种由补发扫描收口）或已结案：不调用开具。"""
    world = credit_world
    if closed:
        payment = readable_payment(
            world, status=PaymentStatus.REVIEW_REQUIRED, reason=GATEWAY_INCONSISTENT
        )
        close_by_review(world, payment)
    else:
        payment = readable_payment(world, status=PaymentStatus.PAID)
    serve_paid(world, payment)
    before = receipt_state(world)
    calls = spy_on(monkeypatch, payments_service)

    result = world.confirm(payment)

    assert (result.changed, result.posted) == (False, False)
    assert calls == []
    assert receipt_state(world) == before
    assert receipt_emails(world) == []


def test_a_second_confirmation_keeps_the_one_receipt(credit_world: World) -> None:
    world = credit_world
    payment = readable_payment(world)
    serve_paid(world, payment)
    world.confirm(payment)
    before = receipt_state(world)

    again = world.confirm(payment, now=NOW + dt.timedelta(minutes=5))

    assert again.posted is False
    assert receipt_state(world) == before
    assert len(receipt_emails(world)) == 1


@pytest.mark.parametrize("failing", ["tax-policy", "series"])
def test_a_failed_receipt_check_still_commits_the_confirmation(
    credit_world: World, failing: str
) -> None:
    """开具检查不过（返回 None）：入账照常提交，结果不变；没有收据与邮件行。"""
    world = credit_world
    if failing == "series":
        exhaust_series(world)
        payment = readable_payment(world)
    else:
        # world 自带的那一版没有 `document_notice`。
        payment = world.payment(world.customer())
    serve_paid(world, payment)
    before = receipt_state(world)

    result = world.confirm(payment)

    assert (result.new_status, result.reason, result.posted) == (PaymentStatus.PAID, None, True)
    assert world.row(payment).status is PaymentStatus.PAID
    assert len(world.topups(payment)) == 1
    assert len(world.audits(payment)) == 1
    assert receipt_of(world, payment) is None
    assert receipt_emails(world) == []
    assert receipt_state(world) == before


def test_a_database_error_while_issuing_rolls_the_confirmation_back(credit_world: World) -> None:
    """开具写入时数据库错误：异常传出 `confirm_payment`，支付、账本与收据都不变。"""
    world = credit_world
    payment = readable_payment(world)
    serve_paid(world, payment)
    before = world.snapshot(payment)
    receipts_before = receipt_state(world)

    with FailingReceiptWrite(world) as failing, pytest.raises(OperationalError):
        world.confirm(payment)

    assert failing.raised == 1
    assert world.snapshot(payment) == before
    assert world.row(payment).status is PaymentStatus.PENDING
    assert world.topups() == []
    assert receipt_state(world) == receipts_before
    assert receipt_emails(world) == []


# --- 同事务（人工入账） -----------------------------------------------------


def test_a_review_credit_commits_a_receipt_for_what_stripe_received(
    credit_world: World, monkeypatch
) -> None:
    """CREDIT：收据金额等于 Stripe 实收（99.99，不等于应收 100.00）、付款方式取第 2 步的查询、
    有邮件行。"""
    world = credit_world
    payment = in_review(world)
    serve_paid(world, payment, amount_total=9999, method="card")
    calls = spy_on(monkeypatch, review_service)

    assert resolve(world, payment, ReviewAction.CREDIT) == payment.public_id

    assert calls == [payment.public_id]
    row = world.row(payment)
    assert (row.status, row.resolution) == (PaymentStatus.PAID, PaymentResolution.CREDITED)
    assert row.expected_amount == Decimal("100.00")
    [topup] = world.topups(payment)
    receipt = receipt_of(world, payment)
    assert receipt is not None
    assert receipt.amount == topup.amount == Decimal("99.99")
    assert receipt.snapshot_json["payment"]["amount"] == "99.99"
    assert receipt.snapshot_json["payment"]["payment_method"] == "card"
    assert receipt.issued_at == RESOLVED_AT
    [email] = receipt_emails(world)
    assert email.aggregate_id == receipt.public_id
    assert json.loads(email.payload_json or "{}")["amount"] == "99.99"


def test_a_review_close_issues_no_receipt(credit_world: World, monkeypatch) -> None:
    world = credit_world
    payment = in_review(world)
    before = receipt_state(world)
    calls = spy_on(monkeypatch, review_service)

    resolve(world, payment, ReviewAction.CLOSE)

    assert calls == []
    assert world.row(payment).resolution is PaymentResolution.CLOSED
    assert receipt_state(world) == before
    assert receipt_emails(world) == []


def test_a_failed_receipt_check_still_commits_the_review_credit(credit_world: World) -> None:
    """CREDIT 时开具检查不过（税务政策读不出）：入账与结案照常提交，返回值不变。"""
    world = credit_world
    payment = in_review(world, readable=False)
    serve_paid(world, payment, amount_total=9999)
    before = receipt_state(world)

    assert resolve(world, payment, ReviewAction.CREDIT) == payment.public_id

    row = world.row(payment)
    assert (row.status, row.resolution) == (PaymentStatus.PAID, PaymentResolution.CREDITED)
    assert len(world.topups(payment)) == 1
    assert receipt_of(world, payment) is None
    assert receipt_emails(world) == []
    assert receipt_state(world) == before


def test_a_database_error_while_issuing_rolls_the_review_credit_back(credit_world: World) -> None:
    """第 6 步的回滚规则不变：开具时的数据库错误原样传出 `resolve_review`，什么都不变。"""
    world = credit_world
    payment = in_review(world)
    serve_paid(world, payment, amount_total=9999)
    before = world.snapshot(payment)
    receipts_before = receipt_state(world)

    with FailingReceiptWrite(world) as failing, pytest.raises(OperationalError):
        resolve(world, payment, ReviewAction.CREDIT)

    assert failing.raised == 1
    assert world.snapshot(payment) == before
    row = world.row(payment)
    assert (row.status, row.resolution) == (PaymentStatus.REVIEW_REQUIRED, None)
    assert world.topups() == []
    assert receipt_state(world) == receipts_before
    assert receipt_emails(world) == []

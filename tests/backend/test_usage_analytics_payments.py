"""The paid payment totals of the margin analytics (AIH-TASK-221).

设计闸门 #709 v2 §7「手续费汇总」。`paid_payment_totals` 只算 `paid_at` 落在
`[paid_from, paid_to)` 的 `PAID`、MYR 支付：已知手续费只加非空的，为空的单独计笔数；
带 `customer_id` 时只算该租户，查不到客户时全零、不发聚合查询。

支付与租户用共用的 payment_world.py；本文件的夹具只在这里用，定义在这里。payment_world 造的 `PAID`
支付 `paid_at` 恒为 `CREATED`、`gateway_fee` 恒为空：本文件用 UPDATE 改它们（以及一笔的币种）。
测试金额都能精确表示；MySQL 分支逐位断言。时间一律注入，不 sleep。

MySQL 那部分需要 `BILLING_TEST_DATABASE_URL`，没设时 skip ——
**不要把 skipped 读成 passed**，CI 设了它并把 skipped 判成失败。
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable, Iterator
from decimal import Decimal
from typing import Any

import pytest
from payment_world import (
    Customer,
    PaymentRef,
    World,
    build_mysql_world,
    build_sqlite_world,
    close_by_review,
)
from sqlalchemy import event, update

from app.models.payments import Payment, PaymentStatus
from app.repositories.usage_analytics import PaidPaymentTotals, paid_payment_totals

PAID_FROM = dt.datetime(2026, 11, 1, 16, 0, 0)
PAID_TO = dt.datetime(2026, 12, 1, 16, 0, 0)
MIDDLE = dt.datetime(2026, 11, 15, 4, 0, 0)
UNKNOWN_CUSTOMER = "00000000-0000-0000-0000-000000000000"


@pytest.fixture(params=["sqlite", "mysql"])
def fee_world(request, monkeypatch) -> Iterator[World]:
    if request.param == "mysql":
        yield from build_mysql_world(monkeypatch)
        return
    yield from build_sqlite_world()


def set_fields(world: World, payment: PaymentRef, **values: Any) -> None:
    with world.factory() as session:
        session.execute(
            update(Payment).where(Payment.public_id == payment.public_id).values(**values)
        )
        session.commit()


def paid(
    world: World,
    maker: Callable[..., PaymentRef],
    tenant: Customer,
    amount: str,
    *,
    paid_at: dt.datetime,
    fee: str | None,
    currency: str = "MYR",
) -> PaymentRef:
    """A `PAID` payment confirmed at `paid_at` with `fee` (None: unknown) in `currency`."""
    payment = maker(world, tenant, tax_version_id=world.tax_version_id, amount=amount, ledger=False)
    set_fields(
        world,
        payment,
        paid_at=paid_at,
        gateway_fee=None if fee is None else Decimal(fee),
        confirmed_currency=currency,
    )
    return payment


def totals_of(world: World, customer_id: str | None) -> PaidPaymentTotals:
    with world.factory() as session:
        return paid_payment_totals(
            session, customer_id=customer_id, paid_from=PAID_FROM, paid_to=PAID_TO
        )


def test_paid_payment_totals_count_only_paid_myr_payments_in_the_period(
    fee_world: World, paid_payment_maker
) -> None:
    world = fee_world
    mine, other = world.customer(), world.customer()
    # 本租户区间内三笔：左端点（计）、中间、中间且手续费未知。
    paid(world, paid_payment_maker, mine, "100.00", paid_at=PAID_FROM, fee="1.50")
    paid(world, paid_payment_maker, mine, "50.00", paid_at=MIDDLE, fee="0.75")
    paid(world, paid_payment_maker, mine, "300.00", paid_at=MIDDLE, fee=None)
    # 右端点不计；USD 不计。
    paid(world, paid_payment_maker, mine, "10.00", paid_at=PAID_TO, fee="9.99")
    paid(world, paid_payment_maker, mine, "20.00", paid_at=MIDDLE, fee="5.00", currency="USD")
    # 非 PAID 的即使带手续费、`paid_at` 落在区间内也不计。
    pending = world.payment(mine, status=PaymentStatus.PENDING)
    review = world.payment(mine, status=PaymentStatus.REVIEW_REQUIRED, reason="AMOUNT_MISMATCH")
    closed = world.payment(mine, status=PaymentStatus.REVIEW_REQUIRED, reason="AMOUNT_MISMATCH")
    close_by_review(world, closed)
    for payment in (pending, review, closed):
        set_fields(world, payment, paid_at=MIDDLE, gateway_fee=Decimal("2.00"))
    # 另一个租户区间内一笔。
    paid(world, paid_payment_maker, other, "200.00", paid_at=MIDDLE, fee="0.25")

    everyone = totals_of(world, None)
    assert everyone == (4, Decimal("650.00"), Decimal("2.50"), 1)
    only_mine = totals_of(world, mine.public_id)
    assert only_mine == (3, Decimal("450.00"), Decimal("2.25"), 1)
    for totals in (everyone, only_mine):
        assert type(totals.paid_count) is int and type(totals.gateway_fee_unknown_count) is int
        assert isinstance(totals.topup_amount, Decimal)
        assert isinstance(totals.gateway_fee, Decimal)
    if world.factory.kw["bind"].dialect.name == "mysql":
        assert (str(everyone.topup_amount), str(everyone.gateway_fee)) == (
            "650.00000000",
            "2.50000000",
        )
        assert (str(only_mine.topup_amount), str(only_mine.gateway_fee)) == (
            "450.00000000",
            "2.25000000",
        )


def test_an_unknown_customer_is_all_zero_without_an_aggregate_query(
    fee_world: World, paid_payment_maker
) -> None:
    world = fee_world
    paid(world, paid_payment_maker, world.customer(), "100.00", paid_at=MIDDLE, fee="1.50")
    engine = world.factory.kw["bind"]
    statements: list[str] = []

    def record(conn, cursor, statement, parameters, context, executemany) -> None:
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        totals = totals_of(world, UNKNOWN_CUSTOMER)
    finally:
        event.remove(engine, "before_cursor_execute", record)
    assert totals == (0, Decimal(0), Decimal(0), 0)
    assert statements and not any("payments" in statement for statement in statements)

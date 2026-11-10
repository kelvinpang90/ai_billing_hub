"""Payment reconciliation, stage 2: the gateway fee backfill (design gate #585 v3, AIH-TASK-187).

设计 §7 标 187 的各行：

- **手续费补抓**：拿到 MYR 费用写入并记审计；没有费用；非 MYR；会话 id 对不上；超过 8 天不选；
  并发已写影响 0 行；拿不到费用的老单不挡住新单；Stripe 不可用时结束本轮；Stripe 拒绝查询只更新
  `last_reconciled_at`；
- **结案后终态**的 187 部分：`CREDITED` 的行只写 `gateway_fee` 与 `last_reconciled_at`，
  状态与结案字段不变；

另有候选查询的每个条件与边界、与第 1 段共用查询次数、单笔意外错误结束本轮。全部用
`fee_world` 夹具的两个参数，在 SQLite 与真 MySQL 上各跑一次。

支付、租户与假 Stripe 用共用的 payment_world.py；本文件的夹具只在这里用，定义在这里。Stripe 一律是
注入了假传输的 `StripeGateway`，不联网；时间一律注入（`now` 与单调时钟），不 sleep。

MySQL 那部分需要 `BILLING_TEST_DATABASE_URL`，没设时 skip ——
**不要把 skipped 读成 passed**，CI 设了它并把 skipped 判成失败。
"""

from __future__ import annotations

import datetime as dt
import logging
import types
from collections.abc import Iterator
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import pytest
from payment_world import (
    CREATED,
    Customer,
    PaymentRef,
    World,
    audit_state,
    build_mysql_world,
    build_sqlite_world,
    payment_columns,
)
from sqlalchemy import select, update
from sqlalchemy.exc import OperationalError

from app.core.config import Settings
from app.models.auth import AuditAction, AuditLog
from app.models.payments import Payment, PaymentResolution, PaymentStatus
from app.payments.base import GatewayState
from app.services import payment_reconcile as service
from app.services.payment_reconcile import ReconcileRun, StopReason
from app.services.payments import ENTITY_PAYMENT

PAID = PaymentStatus.PAID
SERVICE_LOGGER = "app.services.payment_reconcile"

# 本轮时刻：建单一天之后；各行的时刻都相对它。
RUN_AT = CREATED + dt.timedelta(days=1)
EIGHT_DAYS = 8 * 86_400


def at(seconds: int) -> dt.datetime:
    return RUN_AT + dt.timedelta(seconds=seconds)


# --- 夹具 -------------------------------------------------------------------------------


@pytest.fixture(params=["sqlite", "mysql"])
def fee_world(request, monkeypatch) -> Iterator[World]:
    if request.param == "mysql":
        yield from build_mysql_world(monkeypatch)
        return
    yield from build_sqlite_world()


@pytest.fixture(autouse=True)
def default_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """`run_due` reads grace and maximum from `get_settings()`: the design's defaults here."""
    monkeypatch.setattr(service, "get_settings", lambda: Settings())


# --- 构件 -------------------------------------------------------------------------------


def set_fields(world: World, payment: PaymentRef, **values: Any) -> None:
    with world.factory() as session:
        session.execute(
            update(Payment).where(Payment.public_id == payment.public_id).values(**values)
        )
        session.commit()


def paid(
    world: World,
    tenant: Customer,
    *,
    created: int = -86_400,
    paid_at: int = -86_000,
    reconciled: int | None = None,
    with_session: bool = True,
) -> PaymentRef:
    """A `PAID` payment without a fee; its times are seconds relative to `RUN_AT`."""
    payment = world.payment(tenant, status=PAID, with_session=with_session)
    set_fields(
        world,
        payment,
        created_at=at(created),
        paid_at=at(paid_at),
        last_reconciled_at=None if reconciled is None else at(reconciled),
    )
    return payment


def credit_by_review(world: World, payment: PaymentRef) -> None:
    """What the manual CREDIT (AIH-TASK-182) leaves on a review: `PAID` at the received amount,
    the review reason kept, resolved, no fee yet. One update: `ck_payments_resolution_fields`."""
    set_fields(
        world,
        payment,
        status=PAID,
        confirmed_amount=Decimal("99.99"),
        confirmed_currency="MYR",
        paid_at=CREATED,
        status_reason="AMOUNT_MISMATCH",
        review_required_at=CREATED,
        resolution=PaymentResolution.CREDITED,
        resolved_at=CREATED,
        resolved_by_user_id=world.admin_id,
        resolution_reason="Fictional review credited at the received amount",
    )


def reconcile(world: World, *, now: dt.datetime = RUN_AT, **options: Any) -> ReconcileRun:
    return service.run_due(world.factory, world.stripe.gateway(), now=now, **options)


def candidates(world: World, *, now: dt.datetime = RUN_AT) -> list[str]:
    with world.factory() as session:
        return [row.public_id for row in session.execute(service.fee_candidates(now=now))]


def fee_audits(world: World, payment: PaymentRef) -> list[AuditLog]:
    statement = (
        select(AuditLog).where(AuditLog.entity_id == payment.public_id).order_by(AuditLog.id)
    )
    with world.factory() as session:
        rows = list(session.execute(statement).scalars())
        for row in rows:
            session.expunge(row)
        return rows


def service_records(caplog: pytest.LogCaptureFixture, level: int) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == SERVICE_LOGGER and r.levelno == level]


# --- 候选查询：每个条件与边界 --------------------------------------------------------------


@dataclass(frozen=True)
class Case:
    """One payment, its times relative to `RUN_AT` in seconds, and whether stage 2 picks it."""

    due: bool
    created: int = -86_400
    paid_at: int = -86_000
    reconciled: int | None = None
    with_session: bool = True
    fee: str | None = None
    status: PaymentStatus = PAID
    credited: bool = False


CASES = {
    "never-reconciled": Case(due=True),
    # 创建 8 天内（含 8 天整）
    "created-8-days-ago": Case(created=-EIGHT_DAYS, paid_at=-EIGHT_DAYS + 600, due=True),
    "created-8-days-and-1-second-ago": Case(
        created=-EIGHT_DAYS - 1, paid_at=-EIGHT_DAYS + 600, due=False
    ),
    # 付款满 600 秒
    "paid-599-ago": Case(created=-3600, paid_at=-599, due=False),
    "paid-600-ago": Case(created=-3600, paid_at=-600, due=True),
    # 每 3600 秒最多查一次
    "reconciled-3599-ago": Case(reconciled=-3599, due=False),
    "reconciled-3600-ago": Case(reconciled=-3600, due=True),
    # 已有手续费、没有网关 id、不是 PAID 的不选
    "fee-already-recorded": Case(fee="2.90", due=False),
    "no-gateway-session": Case(with_session=False, due=False),
    "expired": Case(status=PaymentStatus.EXPIRED, due=False),
    "review-required": Case(status=PaymentStatus.REVIEW_REQUIRED, due=False),
    # 人工按实付入账的也是 PAID，照样补
    "credited-by-review": Case(status=PaymentStatus.REVIEW_REQUIRED, credited=True, due=True),
}


def test_the_candidate_query_follows_every_condition_and_boundary(fee_world: World) -> None:
    world = fee_world
    tenant = world.customer()
    names: dict[str, str] = {}
    for name, case in CASES.items():
        payment = world.payment(tenant, status=case.status, with_session=case.with_session)
        if case.credited:
            credit_by_review(world, payment)
        set_fields(
            world,
            payment,
            created_at=at(case.created),
            paid_at=at(case.paid_at),
            last_reconciled_at=None if case.reconciled is None else at(case.reconciled),
            gateway_fee=None if case.fee is None else Decimal(case.fee),
        )
        names[payment.public_id] = name

    selected = {names[public_id] for public_id in candidates(world)}

    assert selected == {name for name, case in CASES.items() if case.due}


def test_the_candidate_query_binds_times_instead_of_database_date_functions(
    fee_world: World,
) -> None:
    """时刻参数由应用按 now 算好再绑定；不写死方言的写法，只查都不该出现的词。"""
    engine = fee_world.factory.kw["bind"]

    sql = str(service.fee_candidates(now=RUN_AT).compile(dialect=engine.dialect)).lower()

    for function in ("now(", "current_timestamp", "date_sub", "date_add", "datetime(", "interval"):
        assert function not in sql
    assert "coalesce" in sql


def test_the_least_recently_checked_payment_comes_first(fee_world: World) -> None:
    """ORDER BY COALESCE(last_reconciled_at, paid_at), id。"""
    world = fee_world
    tenant = world.customer()
    checked_recently = paid(world, tenant, paid_at=-80_000, reconciled=-3600)
    never_checked_first = paid(world, tenant, paid_at=-50_000)
    never_checked_second = paid(world, tenant, paid_at=-50_000)
    checked_earliest = paid(world, tenant, paid_at=-86_000, reconciled=-60_000)

    assert candidates(world) == [
        checked_earliest.public_id,
        never_checked_first.public_id,
        never_checked_second.public_id,
        checked_recently.public_id,
    ]


def test_old_payments_without_a_fee_do_not_hold_back_a_new_one(fee_world: World) -> None:
    """拿不到费用的老单：查过之后一小时内不再入选，新单照样被查到并写入。"""
    world = fee_world
    tenant = world.customer()
    old = [
        paid(world, tenant, created=-500_000, paid_at=-499_000)
        for _ in range(service.FEE_CANDIDATE_LIMIT)
    ]
    for payment in old:
        world.serve(payment, GatewayState.PAID, fee=None)

    first = reconcile(world, now=RUN_AT)

    assert (first.queries, first.fees_recorded) == (service.FEE_CANDIDATE_LIMIT, 0)
    assert all(world.row(payment).last_reconciled_at == RUN_AT for payment in old)

    new = paid(world, tenant, created=-1200, paid_at=-600)
    world.serve(new, GatewayState.PAID, fee=290)
    world.stripe.asked.clear()
    later = RUN_AT + dt.timedelta(minutes=1)

    assert candidates(world, now=later) == [new.public_id]
    second = reconcile(world, now=later)

    assert (second.queries, second.fees_recorded, second.stop_reason) == (1, 1, StopReason.DONE)
    assert world.stripe.asked == [new.session_id]
    assert world.row(new).gateway_fee == Decimal("2.9")


def test_a_payment_created_more_than_8_days_ago_is_not_queried(fee_world: World) -> None:
    world = fee_world
    payment = paid(world, world.customer(), created=-EIGHT_DAYS - 1, paid_at=-EIGHT_DAYS + 600)
    world.serve(payment, GatewayState.PAID, fee=290)
    before = world.snapshot(payment)

    run = reconcile(world)

    assert (run.queries, run.fees_recorded, run.stop_reason) == (0, 0, StopReason.DONE)
    assert world.stripe.asked == []
    assert world.snapshot(payment) == before


# --- 逐笔：拿到 MYR 费用、拿不到、不能写 -----------------------------------------------------


def test_a_myr_fee_is_recorded_with_its_audit(fee_world: World) -> None:
    world = fee_world
    payment = paid(world, world.customer())
    world.serve(payment, GatewayState.PAID, fee=150, fee_currency="myr")
    before = payment_columns(world.row(payment))

    run = reconcile(world)

    assert run == ReconcileRun(
        queries=1,
        orphans_expired=0,
        timeouts_expired=0,
        fees_recorded=1,
        stop_reason=StopReason.DONE,
    )
    assert world.stripe.asked == [payment.session_id]
    after = payment_columns(world.row(payment))
    assert after["gateway_fee"] == Decimal("1.5")
    assert after == {**before, "gateway_fee": after["gateway_fee"], "last_reconciled_at": RUN_AT}
    [audit] = fee_audits(world, payment)
    assert audit.action is AuditAction.PAYMENT_FEE_RECORDED
    assert (audit.actor_user_id, audit.actor_role) == (None, "SYSTEM")
    assert (audit.entity_type, audit.entity_id) == (ENTITY_PAYMENT, payment.public_id)
    assert (audit.reason, audit.created_at) == (None, RUN_AT)
    assert audit_state(audit.before_state) == {"gateway_fee": None}
    assert audit_state(audit.after_state) == {"gateway_fee": "1.5"}
    assert world.topups() == []


@pytest.mark.parametrize(
    ("case", "level"),
    [
        ("no-fee", None),
        ("not-myr", logging.WARNING),
        ("session-mismatch", logging.ERROR),
        ("rejected", logging.ERROR),
    ],
    ids=lambda value: value if isinstance(value, str) else None,
)
def test_without_a_usable_fee_only_last_reconciled_at_is_written(
    fee_world: World, caplog: pytest.LogCaptureFixture, case: str, level: int | None
) -> None:
    """没有费用、非 MYR、会话 id 对不上、Stripe 拒绝查询：只更新 last_reconciled_at，继续下一笔。"""
    world = fee_world
    tenant = world.customer()
    payment = paid(world, tenant, paid_at=-86_000)
    following = paid(world, tenant, paid_at=-80_000)
    world.serve(following, GatewayState.PAID, fee=290)
    if case == "no-fee":
        world.serve(payment, GatewayState.PAID, fee=None)
    elif case == "not-myr":
        world.serve(payment, GatewayState.PAID, fee=290, fee_currency="usd")
    elif case == "session-mismatch":
        world.serve(payment, GatewayState.PAID, fee=290, session_id="cs_test_someone_else")
    # "rejected"：Stripe 不认识这个会话（404 resource_missing）。
    before = payment_columns(world.row(payment))

    with caplog.at_level(logging.INFO, logger=SERVICE_LOGGER):
        run = reconcile(world)

    assert (run.queries, run.fees_recorded, run.stop_reason) == (2, 1, StopReason.DONE)
    assert world.stripe.asked == [payment.session_id, following.session_id]
    assert payment_columns(world.row(payment)) == {**before, "last_reconciled_at": RUN_AT}
    assert fee_audits(world, payment) == []
    assert world.row(following).gateway_fee == Decimal("2.9")

    errors = service_records(caplog, logging.ERROR)
    warnings = service_records(caplog, logging.WARNING)
    if level is None:
        assert errors == [] and warnings == []
        return
    [record] = errors if level == logging.ERROR else warnings
    assert record.payment_id == payment.public_id
    if case == "rejected":
        assert record.error_code == "resource_missing"
    if case == "not-myr":
        assert record.fee_currency == "USD"
        assert errors == []
    else:
        assert warnings == []


def test_a_fee_written_meanwhile_is_left_alone(
    fee_world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """并发已写：条件更新影响 0 行，什么也不做。"""
    world = fee_world
    payment = paid(world, world.customer())
    world.serve(payment, GatewayState.PAID, fee=150)
    real_handle = world.stripe.handle

    def written_meanwhile(request: Any) -> Any:
        # Stripe 正在回答时，另一个写入者先记下了手续费。
        set_fields(world, payment, gateway_fee=Decimal("3.10"))
        return real_handle(request)

    monkeypatch.setattr(world.stripe, "handle", written_meanwhile)

    run = reconcile(world)

    assert (run.queries, run.fees_recorded, run.stop_reason) == (1, 0, StopReason.DONE)
    row = world.row(payment)
    assert (row.gateway_fee, row.last_reconciled_at) == (Decimal("3.10"), None)
    assert fee_audits(world, payment) == []


def test_a_credited_review_only_gets_its_fee_and_last_reconciled_at(fee_world: World) -> None:
    """人工按实付入账的行：只写 gateway_fee 与 last_reconciled_at，状态与结案字段不变。"""
    world = fee_world
    payment = world.payment(world.customer(), status=PaymentStatus.REVIEW_REQUIRED)
    credit_by_review(world, payment)
    world.serve(payment, GatewayState.PAID, fee=290)
    before = payment_columns(world.row(payment))

    run = reconcile(world)

    assert (run.queries, run.fees_recorded) == (1, 1)
    row = world.row(payment)
    after = payment_columns(row)
    assert after["gateway_fee"] == Decimal("2.9")
    assert after == {**before, "gateway_fee": after["gateway_fee"], "last_reconciled_at": RUN_AT}
    assert (row.status, row.resolution) == (PAID, PaymentResolution.CREDITED)
    [audit] = fee_audits(world, payment)
    assert audit.action is AuditAction.PAYMENT_FEE_RECORDED
    assert audit_state(audit.after_state) == {"gateway_fee": "2.9"}


# --- 结束本轮：Stripe 不可用、单笔意外错误、预算与查询次数 ---------------------------------------


def three_due(world: World) -> list[PaymentRef]:
    """Three fee candidates, picked in this order, each with a MYR fee waiting at Stripe."""
    tenant = world.customer()
    payments = [paid(world, tenant, paid_at=-86_000 + index) for index in range(3)]
    for payment in payments:
        world.serve(payment, GatewayState.PAID, fee=290)
    return payments


def test_an_unavailable_gateway_ends_the_run(fee_world: World) -> None:
    world = fee_world
    payments = three_due(world)
    world.stripe.status_code = 503

    run = reconcile(world)

    assert (run.queries, run.fees_recorded, run.stop_reason) == (
        1,
        0,
        StopReason.GATEWAY_UNAVAILABLE,
    )
    assert world.stripe.asked == [payments[0].session_id]
    for payment in payments:
        row = world.row(payment)
        assert (row.gateway_fee, row.last_reconciled_at) == (None, None)


def test_an_unexpected_error_rolls_back_that_payment_and_ends_the_run(
    fee_world: World, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """第一笔的手续费事务抛数据库错误：这一笔不变、ERROR 日志、后两笔本轮没有被查询。"""
    world = fee_world
    payments = three_due(world)
    before = world.snapshot(payments[0])

    def broken_json(*args: Any, **options: Any) -> str:
        raise OperationalError("INSERT INTO audit_logs", {}, Exception("injected"))

    monkeypatch.setattr(service, "_json", broken_json)

    with caplog.at_level(logging.INFO, logger=SERVICE_LOGGER):
        run = reconcile(world)

    assert (run.queries, run.fees_recorded, run.stop_reason) == (
        1,
        0,
        StopReason.UNEXPECTED_ERROR,
    )
    assert world.snapshot(payments[0]) == before
    assert world.stripe.asked == [payments[0].session_id]
    for payment in payments[1:]:
        row = world.row(payment)
        assert (row.gateway_fee, row.last_reconciled_at) == (None, None)
    [error] = service_records(caplog, logging.ERROR)
    assert (error.payment_id, error.error_type) == (payments[0].public_id, "OperationalError")
    assert "injected" not in error.getMessage()


def test_stage_2_shares_the_query_count_with_stage_1(fee_world: World) -> None:
    """一笔到期的 PENDING 用掉唯一的一次查询：第 2 段本轮不查。"""
    world = fee_world
    tenant = world.customer()
    pending = world.payment(tenant)
    set_fields(world, pending, created_at=at(-600), expires_at=at(3000))
    world.serve(pending, GatewayState.OPEN)
    fee_due = paid(world, tenant)
    world.serve(fee_due, GatewayState.PAID, fee=290)

    limited = reconcile(world, max_queries=1)

    assert (limited.queries, limited.fees_recorded, limited.stop_reason) == (
        1,
        0,
        StopReason.QUERY_LIMIT,
    )
    assert world.stripe.asked == [pending.session_id]
    assert world.row(fee_due).gateway_fee is None

    later = RUN_AT + dt.timedelta(minutes=1)
    run = reconcile(world, now=later)

    assert (run.queries, run.fees_recorded, run.stop_reason) == (1, 1, StopReason.DONE)
    assert world.stripe.asked[-1] == fee_due.session_id


def test_a_spent_budget_stops_before_the_next_fee_query(
    fee_world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """注入的单调时钟在第一笔之后超过 45 秒：那一笔做完，不再开始新的一笔。"""
    world = fee_world
    payments = three_due(world)
    clock = {"now": 1000.0}
    monkeypatch.setattr(service, "time", types.SimpleNamespace(monotonic=lambda: clock["now"]))
    real_record = service._record_fee

    def slow_record(*args: Any, **options: Any) -> Any:
        result = real_record(*args, **options)
        clock["now"] += service.RUN_BUDGET_SECONDS + 1
        return result

    monkeypatch.setattr(service, "_record_fee", slow_record)

    run = reconcile(world)

    assert (run.queries, run.fees_recorded, run.stop_reason) == (1, 1, StopReason.BUDGET_SPENT)
    assert world.stripe.asked == [payments[0].session_id]
    assert world.row(payments[1]).gateway_fee is None

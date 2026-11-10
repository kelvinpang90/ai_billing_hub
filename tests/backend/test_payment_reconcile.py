"""Payment reconciliation, stage 1 (design gate #585 v3, AIH-TASK-180).

设计 §7 标 180 的各行：

- **结案后终态**：已不入账结案的支付经事件路径收到 Stripe 已付不改状态；第 1 段不选出结案行；
- **丢失回调补回**、**候选查询每个分支与边界**（老单不挡新单、排序）、**孤儿过期与之后的认回入账**、
  **超时过期四组**、**Stripe 不可用时本轮结束**、**单笔意外错误**、**预算与查询上限**、
  **未满上限时 Stripe 拒绝查询**、**直接调用 `run_due`**：`reconcile_world` 夹具的两个参数，
  在 SQLite 与真 MySQL 上各跑一次；
- **补对账与回调并发只入账一次**、**命名锁被占时直接返回**：只在真 MySQL 上。

支付与租户沿用 test_payment_confirm.py 的世界，事件沿用 test_payment_events.py 的写法；
夹具只在本文件里用，定义在这里。Stripe 一律是注入了假传输的 `StripeGateway`，不联网；时间一律注入
（`now` 与单调时钟），不 sleep。

MySQL 那部分需要 `BILLING_TEST_DATABASE_URL`，没设时 skip ——
**不要把 skipped 读成 passed**，CI 设了它并把 skipped 判成失败。
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import logging
import threading
import types
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy import Engine, text, update
from sqlalchemy.exc import OperationalError
from test_payment_confirm import (
    AUDIT_FIELDS,
    CREATED,
    EXPIRES,
    Customer,
    PaymentRef,
    World,
    _mysql_world,
    _sqlite_world,
    _state,
    close_by_review,
)
from test_payment_events import event_row, payment_event, wire

from app.core.celery_app import create_celery_app
from app.core.config import Settings
from app.models.auth import AuditAction
from app.models.payments import (
    Payment,
    PaymentEventProcessingStatus,
    PaymentResolution,
    PaymentStatus,
)
from app.payments.base import GatewayState
from app.services import payment_reconcile as service
from app.services.payment_reconcile import ReconcileRun, StopReason
from app.services.payments import (
    ENTITY_PAYMENT,
    NO_GATEWAY_SESSION,
    RECONCILE_TIMEOUT,
    ConfirmConflict,
)
from app.tasks import payment_events as events_task
from app.tasks import payment_reconcile as task

PENDING = PaymentStatus.PENDING
SERVICE_LOGGER = "app.services.payment_reconcile"

# 设计 §2「配置」的缺省值。
GRACE = dt.timedelta(seconds=300)
MAXIMUM = dt.timedelta(seconds=604_800)

# 候选查询用例的「本轮时刻」；各行的时刻都相对它。
RUN_AT = dt.datetime(2026, 11, 20, 12, 0, 0)


def at(seconds: int) -> dt.datetime:
    return RUN_AT + dt.timedelta(seconds=seconds)


def after_created(seconds: int) -> dt.datetime:
    return CREATED + dt.timedelta(seconds=seconds)


# --- 夹具 -------------------------------------------------------------------------------


@pytest.fixture(params=["sqlite", "mysql"])
def reconcile_world(request, monkeypatch) -> Iterator[World]:
    if request.param == "mysql":
        yield from _mysql_world(monkeypatch)
        return
    yield from _sqlite_world()


@pytest.fixture
def mysql_reconcile_world(monkeypatch) -> Iterator[World]:
    yield from _mysql_world(monkeypatch)


@pytest.fixture(autouse=True)
def default_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """The service reads grace and maximum from `get_settings()`: the design's defaults here."""
    monkeypatch.setattr(service, "get_settings", lambda: Settings())


def wire_task(
    monkeypatch: pytest.MonkeyPatch,
    world: World,
    *,
    enabled: bool = True,
    configured: bool = True,
) -> None:
    """Point the reconciliation task at this world's database and fake Stripe."""
    monkeypatch.setattr(task, "_session_factory", lambda: world.factory)
    monkeypatch.setattr(task, "get_settings", lambda: Settings(payments_enabled=enabled))
    monkeypatch.setattr(
        task,
        "get_payment_gateway",
        lambda settings: world.stripe.gateway() if configured else None,
    )


def reconcile(world: World, *, now: dt.datetime, **options: Any) -> ReconcileRun:
    return service.run_due(world.factory, world.stripe.gateway(), now=now, **options)


def set_times(
    world: World,
    payment: PaymentRef,
    *,
    created: dt.datetime,
    expires: dt.datetime,
    reconciled: dt.datetime | None = None,
) -> None:
    with world.factory() as session:
        session.execute(
            update(Payment)
            .where(Payment.public_id == payment.public_id)
            .values(created_at=created, expires_at=expires, last_reconciled_at=reconciled)
        )
        session.commit()


def place(
    world: World,
    tenant: Customer,
    *,
    created: dt.datetime,
    expires: dt.datetime,
    reconciled: dt.datetime | None = None,
    with_session: bool = True,
) -> PaymentRef:
    payment = world.payment(tenant, with_session=with_session)
    set_times(world, payment, created=created, expires=expires, reconciled=reconciled)
    return payment


def candidates(world: World, *, now: dt.datetime = RUN_AT) -> list[str]:
    statement = service.pending_candidates(now=now, grace=GRACE, maximum=MAXIMUM)
    with world.factory() as session:
        return [row.public_id for row in session.execute(statement)]


def assert_expired_by_reconciliation(
    world: World, payment: PaymentRef, reason: str, *, now: dt.datetime
) -> None:
    """`EXPIRED` with `reason`, and its system `PAYMENT_STATUS_CHANGE` audit written like A's."""
    row = world.row(payment)
    assert (row.status, row.status_reason, row.updated_at) == (PaymentStatus.EXPIRED, reason, now)
    assert row.paid_at is None and row.resolution is None
    assert world.topups() == []
    [audit] = world.audits(payment)
    assert audit.action is AuditAction.PAYMENT_STATUS_CHANGE
    assert (audit.actor_user_id, audit.actor_role) == (None, "SYSTEM")
    assert (audit.entity_type, audit.entity_id) == (ENTITY_PAYMENT, payment.public_id)
    assert (audit.reason, audit.created_at) == (reason, now)
    before, after = _state(audit.before_state), _state(audit.after_state)
    assert set(before) == set(after) == AUDIT_FIELDS
    assert (before["status"], before["status_reason"]) == ("PENDING", None)
    assert (after["status"], after["status_reason"]) == ("EXPIRED", reason)
    assert after["expected_amount"] == before["expected_amount"]


# --- 候选查询：每个分支与边界（设计 §7「补对账节奏」） -------------------------------------------


@dataclass(frozen=True)
class Case:
    """One payment, its times relative to `RUN_AT` in seconds, and whether stage 1 picks it."""

    created: int
    expires: int
    due: bool
    reconciled: int | None = None
    with_session: bool = True
    status: PaymentStatus = PENDING
    closed: bool = False


CASES = {
    # 创建后 299 / 300 秒（grace）
    "grace-299": Case(created=-299, expires=3301, due=False),
    "grace-300": Case(created=-300, expires=3300, due=True),
    # 未过期、建单 2 小时内：间隔 300 秒
    "young-reconciled-299-ago": Case(created=-3600, expires=1800, reconciled=-299, due=False),
    "young-reconciled-300-ago": Case(created=-3600, expires=1800, reconciled=-300, due=True),
    # 建单 2 小时的边界（同样 300 秒前查过）
    "created-7199-ago": Case(created=-7199, expires=1000, reconciled=-300, due=True),
    "created-7200-ago": Case(created=-7200, expires=1000, reconciled=-300, due=False),
    # 未过期、建单超过 2 小时：间隔 1800 秒
    "old-reconciled-1799-ago": Case(created=-10000, expires=1000, reconciled=-1799, due=False),
    "old-reconciled-1800-ago": Case(created=-10000, expires=1000, reconciled=-1800, due=True),
    # 过期时刻前后（同样 300 秒前查过）
    "one-second-before-expiry": Case(created=-10000, expires=1, reconciled=-300, due=False),
    "at-expiry": Case(created=-10000, expires=0, reconciled=-300, due=True),
    # 过期后 1 小时内：间隔 300 秒
    "just-expired-reconciled-299-ago": Case(
        created=-20000, expires=-1800, reconciled=-299, due=False
    ),
    "just-expired-reconciled-300-ago": Case(
        created=-20000, expires=-1800, reconciled=-300, due=True
    ),
    # 过期后 3600 秒的边界
    "expired-3599-ago": Case(created=-20000, expires=-3599, reconciled=-300, due=True),
    "expired-3600-ago": Case(created=-20000, expires=-3600, reconciled=-300, due=False),
    # 过期 1 小时以后：间隔 21600 秒
    "long-expired-reconciled-21599-ago": Case(
        created=-40000, expires=-7200, reconciled=-21599, due=False
    ),
    "long-expired-reconciled-21600-ago": Case(
        created=-40000, expires=-7200, reconciled=-21600, due=True
    ),
    "long-expired-never-reconciled": Case(created=-40000, expires=-7200, due=True),
    # 满上限：刚查过也入选
    "one-second-short-of-maximum": Case(
        created=-604_799, expires=-600_000, reconciled=0, due=False
    ),
    "at-maximum": Case(created=-604_800, expires=-600_000, reconciled=0, due=True),
    # 孤儿：expires_at + 599 / + 600 秒
    "orphan-expired-599-ago": Case(created=-10000, expires=-599, with_session=False, due=False),
    "orphan-expired-600-ago": Case(created=-10000, expires=-600, with_session=False, due=True),
    "orphan-not-expired": Case(created=-10000, expires=100, with_session=False, due=False),
    # 不是 PENDING 的一律不选（含已不入账结案的行）
    "expired": Case(created=-40000, expires=-7200, status=PaymentStatus.EXPIRED, due=False),
    "paid": Case(created=-40000, expires=-7200, status=PaymentStatus.PAID, due=False),
    "review-required": Case(
        created=-40000, expires=-7200, status=PaymentStatus.REVIEW_REQUIRED, due=False
    ),
    "closed-by-review": Case(
        created=-40000,
        expires=-7200,
        status=PaymentStatus.REVIEW_REQUIRED,
        closed=True,
        due=False,
    ),
}


def test_the_candidate_query_follows_every_branch_and_boundary(reconcile_world: World) -> None:
    world = reconcile_world
    tenant = world.customer()
    names: dict[str, str] = {}
    for name, case in CASES.items():
        payment = world.payment(tenant, status=case.status, with_session=case.with_session)
        if case.closed:
            close_by_review(world, payment)
        set_times(
            world,
            payment,
            created=at(case.created),
            expires=at(case.expires),
            reconciled=None if case.reconciled is None else at(case.reconciled),
        )
        names[payment.public_id] = name

    selected = {names[public_id] for public_id in candidates(world)}

    assert selected == {name for name, case in CASES.items() if case.due}


def test_the_candidate_query_binds_times_instead_of_database_date_functions(
    reconcile_world: World,
) -> None:
    """时刻参数由应用按 now 算好再绑定，不用数据库的日期函数。

    不写死方言的写法，只查都不该出现的词。
    """
    engine = reconcile_world.factory.kw["bind"]
    statement = service.pending_candidates(now=RUN_AT, grace=GRACE, maximum=MAXIMUM)

    sql = str(statement.compile(dialect=engine.dialect)).lower()

    for function in ("now(", "current_timestamp", "date_sub", "date_add", "datetime(", "interval"):
        assert function not in sql
    assert "coalesce" in sql


def test_old_payments_that_are_not_due_do_not_hold_back_a_due_new_one(
    reconcile_world: World,
) -> None:
    """到期条件写在查询里：200 笔未到期的老单（建单更早）不挡住到期的新单。"""
    world = reconcile_world
    tenant = world.customer()
    old = [world.payment(tenant) for _ in range(service.CANDIDATE_LIMIT)]
    with world.factory() as session:
        session.execute(
            update(Payment)
            .where(Payment.public_id.in_([payment.public_id for payment in old]))
            .values(created_at=at(-500_000), expires_at=at(-490_000), last_reconciled_at=at(-60))
        )
        session.commit()
    new = place(world, tenant, created=at(-600), expires=at(3000))

    assert candidates(world) == [new.public_id]


def test_the_least_recently_reconciled_payment_comes_first(reconcile_world: World) -> None:
    """ORDER BY COALESCE(last_reconciled_at, created_at), id。"""
    world = reconcile_world
    tenant = world.customer()
    checked_long_ago = place(
        world, tenant, created=at(-10000), expires=at(1000), reconciled=at(-4000)
    )
    never_checked_first = place(world, tenant, created=at(-5000), expires=at(1000))
    never_checked_second = place(world, tenant, created=at(-5000), expires=at(1000))
    checked_earliest = place(
        world, tenant, created=at(-9000), expires=at(1000), reconciled=at(-6000)
    )

    assert candidates(world) == [
        checked_earliest.public_id,
        never_checked_first.public_id,
        never_checked_second.public_id,
        checked_long_ago.public_id,
    ]


def test_grace_and_maximum_come_from_the_settings(
    reconcile_world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = reconcile_world
    monkeypatch.setattr(
        service,
        "get_settings",
        lambda: Settings(payment_reconcile_grace_seconds=600, payment_reconcile_max_seconds=86_400),
    )
    tenant = world.customer()
    within_grace = place(world, tenant, created=at(-599), expires=at(3000))
    at_maximum = place(world, tenant, created=at(-86_400), expires=at(-82_800), reconciled=at(0))
    world.serve(within_grace, GatewayState.OPEN)
    world.serve(at_maximum, GatewayState.OPEN)

    run = reconcile(world, now=RUN_AT)

    assert world.stripe.asked == [at_maximum.session_id]
    assert (run.queries, run.timeouts_expired) == (1, 1)
    assert world.row(at_maximum).status_reason == RECONCILE_TIMEOUT
    assert world.row(within_grace).status is PENDING


# --- 结案后终态（设计 §7「结案后终态」的 180 部分） ---------------------------------------------


def test_a_closed_review_stays_closed_on_the_event_path_and_is_never_a_candidate(
    reconcile_world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """不入账结案后 Stripe 报已付：事件处理确认后状态不变、无账本行、只更新 last_reconciled_at；
    补对账第 1 段不碰它。"""
    world = reconcile_world
    wire(monkeypatch, world)
    payment = world.payment(world.customer(), status=PaymentStatus.REVIEW_REQUIRED)
    close_by_review(world, payment)
    world.serve(payment, GatewayState.PAID)
    when = after_created(3 * 86_400)

    run = reconcile(world, now=when)

    assert (run.queries, run.stop_reason) == (0, StopReason.DONE)
    assert world.stripe.asked == []

    body = payment_event(world, payment)
    assert events_task.process_due(now=when) == 1

    assert event_row(world, body).processing_status is PaymentEventProcessingStatus.PROCESSED
    row = world.row(payment)
    assert (row.status, row.resolution) == (PaymentStatus.FAILED, PaymentResolution.CLOSED)
    assert (row.last_reconciled_at, row.paid_at) == (when, None)
    assert world.topups() == []
    assert world.audits(payment) == []


# --- 丢失回调补回 -----------------------------------------------------------------------------


def test_a_lost_webhook_is_recovered_and_a_later_delivery_does_not_credit_twice(
    reconcile_world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = reconcile_world
    wire(monkeypatch, world)
    payment = world.payment(world.customer())
    world.serve(payment, GatewayState.PAID)
    when = after_created(600)

    run = reconcile(world, now=when)

    assert run == ReconcileRun(
        queries=1,
        orphans_expired=0,
        timeouts_expired=0,
        fees_recorded=0,
        stop_reason=StopReason.DONE,
    )
    row = world.row(payment)
    assert (row.status, row.paid_at, row.last_reconciled_at) == (PaymentStatus.PAID, when, when)
    assert len(world.topups(payment)) == 1

    body = payment_event(world, payment)
    assert events_task.process_due(now=when + dt.timedelta(minutes=1)) == 1

    assert event_row(world, body).processing_status is PaymentEventProcessingStatus.PROCESSED
    assert len(world.topups(payment)) == 1
    assert world.row(payment).paid_at == when


# --- 孤儿过期与之后的认回入账 -------------------------------------------------------------------


def test_an_orphan_expires_600_seconds_after_its_expiry_and_a_late_payment_still_credits(
    reconcile_world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = reconcile_world
    wire(monkeypatch, world)
    payment = world.payment(world.customer(), with_session=False)
    before = world.snapshot(payment)

    early = reconcile(world, now=EXPIRES + dt.timedelta(seconds=599))

    assert (early.orphans_expired, early.queries) == (0, 0)
    assert world.snapshot(payment) == before

    when = EXPIRES + dt.timedelta(seconds=600)
    run = reconcile(world, now=when)

    assert (run.orphans_expired, run.queries, run.stop_reason) == (1, 0, StopReason.DONE)
    assert world.stripe.asked == []
    assert_expired_by_reconciliation(world, payment, NO_GATEWAY_SESSION, now=when)
    assert world.row(payment).last_reconciled_at is None

    # 过期后收到带会话 id 的付款成功事件：A 的事件处理认回会话、确认、入账（ADR-0007）。
    recovered = dataclasses.replace(payment, session_id="cs_test_orphan_recovered")
    world.serve(recovered, GatewayState.PAID)
    later = when + dt.timedelta(minutes=5)
    payment_event(world, recovered)
    assert events_task.process_due(now=later) == 1

    row = world.row(payment)
    assert row.gateway_payment_id == recovered.session_id
    assert (row.status, row.status_reason, row.paid_at) == (PaymentStatus.PAID, None, later)
    assert len(world.topups(payment)) == 1


def test_an_orphan_that_got_its_session_back_is_not_expired(
    reconcile_world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """过期事务重查：gateway_payment_id 已不为空（并发认回）→ 什么也不做。"""
    world = reconcile_world
    payment = world.payment(world.customer(), with_session=False)
    real_one = service._reconcile_one

    def recovered_meanwhile(*args: Any, **options: Any) -> Any:
        # 候选查询已按旧值选出它之后、过期事务之前，事件处理认回了会话。
        with world.factory() as session:
            session.execute(
                update(Payment)
                .where(Payment.public_id == payment.public_id)
                .values(gateway_payment_id="cs_test_recovered_meanwhile")
            )
            session.commit()
        return real_one(*args, **options)

    monkeypatch.setattr(service, "_reconcile_one", recovered_meanwhile)

    run = reconcile(world, now=EXPIRES + dt.timedelta(seconds=600))

    assert (run.orphans_expired, run.queries, run.stop_reason) == (0, 0, StopReason.DONE)
    row = world.row(payment)
    assert (row.status, row.status_reason) == (PENDING, None)
    assert row.gateway_payment_id == "cs_test_recovered_meanwhile"
    assert world.audits(payment) == []
    assert world.stripe.asked == []


# --- 超时过期四组 ---------------------------------------------------------------------------


AT_MAXIMUM = CREATED + MAXIMUM


@pytest.mark.parametrize(
    "case", ["open", "unavailable", "rejected", "conflict"], ids=lambda case: case
)
def test_a_payment_still_pending_at_the_maximum_age(
    reconcile_world: World, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    """满 7 天：Stripe 报 OPEN、拒绝查询 → EXPIRED（RECONCILE_TIMEOUT）；Stripe 不可用、
    ConfirmConflict → 不变。"""
    world = reconcile_world
    payment = world.payment(world.customer())
    if case == "open":
        world.serve(payment, GatewayState.OPEN)
    elif case == "unavailable":
        world.serve(payment, GatewayState.PAID)
        world.stripe.status_code = 503
    elif case == "conflict":
        world.serve(payment, GatewayState.PAID)

        def conflicting(*args: Any, **options: Any) -> Any:
            raise ConfirmConflict("CONFIRM_CONFLICT")

        monkeypatch.setattr(service, "confirm_payment", conflicting)
    # "rejected"：Stripe 不认识这个会话（404 resource_missing）。
    before = world.snapshot(payment)

    run = reconcile(world, now=AT_MAXIMUM)

    if case in ("open", "rejected"):
        assert (run.queries, run.timeouts_expired, run.stop_reason) == (1, 1, StopReason.DONE)
        assert_expired_by_reconciliation(world, payment, RECONCILE_TIMEOUT, now=AT_MAXIMUM)
        assert world.row(payment).last_reconciled_at == AT_MAXIMUM
        assert world.stripe.asked == [payment.session_id]
    else:
        expected_stop = StopReason.GATEWAY_UNAVAILABLE if case == "unavailable" else StopReason.DONE
        assert (run.queries, run.timeouts_expired, run.stop_reason) == (1, 0, expected_stop)
        assert world.snapshot(payment) == before


def test_a_payment_one_second_short_of_the_maximum_age_is_not_timed_out(
    reconcile_world: World,
) -> None:
    world = reconcile_world
    payment = world.payment(world.customer())
    world.serve(payment, GatewayState.OPEN)
    when = AT_MAXIMUM - dt.timedelta(seconds=1)

    run = reconcile(world, now=when)

    assert (run.queries, run.timeouts_expired) == (1, 0)
    row = world.row(payment)
    assert (row.status, row.last_reconciled_at) == (PENDING, when)


# --- Stripe 不可用、拒绝查询、单笔意外错误 -----------------------------------------------------


def three_due(world: World) -> list[PaymentRef]:
    """Three payments with sessions, due at `RUN_AT` and picked in this order."""
    tenant = world.customer()
    return [place(world, tenant, created=at(-5000 + index), expires=at(1000)) for index in range(3)]


def test_an_unavailable_gateway_ends_the_run(reconcile_world: World) -> None:
    world = reconcile_world
    payments = three_due(world)
    for payment in payments:
        world.serve(payment, GatewayState.PAID)
    world.stripe.status_code = 503

    run = reconcile(world, now=RUN_AT)

    assert (run.queries, run.stop_reason) == (1, StopReason.GATEWAY_UNAVAILABLE)
    assert world.stripe.asked == [payments[0].session_id]
    for payment in payments:
        row = world.row(payment)
        assert (row.status, row.last_reconciled_at) == (PENDING, None)


def test_a_rejected_query_short_of_the_maximum_only_marks_the_payment_reconciled(
    reconcile_world: World, caplog: pytest.LogCaptureFixture
) -> None:
    """创建 1 天、Stripe 拒绝查询：状态不变、last_reconciled_at 更新、记 ERROR，继续下一笔。"""
    world = reconcile_world
    tenant = world.customer()
    rejected = world.payment(tenant)
    following = world.payment(tenant)
    world.serve(following, GatewayState.OPEN)
    when = after_created(86_400)

    with caplog.at_level(logging.INFO, logger=SERVICE_LOGGER):
        run = reconcile(world, now=when)

    assert (run.queries, run.timeouts_expired, run.stop_reason) == (2, 0, StopReason.DONE)
    assert world.stripe.asked == [rejected.session_id, following.session_id]
    row = world.row(rejected)
    assert (row.status, row.status_reason, row.last_reconciled_at) == (PENDING, None, when)
    assert row.updated_at == CREATED
    assert world.audits(rejected) == []
    assert world.row(following).last_reconciled_at == when
    [error] = [r for r in caplog.records if r.name == SERVICE_LOGGER and r.levelno == logging.ERROR]
    assert (error.payment_id, error.error_code) == (rejected.public_id, "resource_missing")


def test_an_unexpected_error_rolls_back_that_payment_and_ends_the_run(
    reconcile_world: World, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """三笔到期，第一笔（孤儿）的过期事务抛数据库错误。

    这一笔不变、ERROR 日志、后两笔本轮没有被处理。
    """
    world = reconcile_world
    tenant = world.customer()
    orphan = place(world, tenant, created=at(-9000), expires=at(-7200), with_session=False)
    others = [place(world, tenant, created=at(-5000), expires=at(1000)) for _ in range(2)]
    for payment in others:
        world.serve(payment, GatewayState.PAID)
    before = world.snapshot(orphan)

    def broken_audit(*args: Any, **options: Any) -> None:
        raise OperationalError("INSERT INTO audit_logs", {}, Exception("injected"))

    monkeypatch.setattr(service, "_audit", broken_audit)

    with caplog.at_level(logging.INFO, logger=SERVICE_LOGGER):
        run = reconcile(world, now=RUN_AT)

    assert (run.queries, run.orphans_expired, run.stop_reason) == (
        0,
        0,
        StopReason.UNEXPECTED_ERROR,
    )
    assert world.snapshot(orphan) == before
    assert world.stripe.asked == []
    for payment in others:
        row = world.row(payment)
        assert (row.status, row.last_reconciled_at) == (PENDING, None)
    [error] = [r for r in caplog.records if r.name == SERVICE_LOGGER and r.levelno == logging.ERROR]
    assert (error.payment_id, error.error_type) == (orphan.public_id, "OperationalError")
    assert "injected" not in error.getMessage()


# --- 预算与查询上限 -----------------------------------------------------------------------------


def test_one_run_queries_stripe_at_most_fifty_times(reconcile_world: World) -> None:
    world = reconcile_world
    tenant = world.customer()
    payments = [world.payment(tenant) for _ in range(service.MAX_QUERIES + 1)]
    for payment in payments:
        world.serve(payment, GatewayState.OPEN)
    when = after_created(600)

    run = reconcile(world, now=when)

    assert (run.queries, run.stop_reason) == (service.MAX_QUERIES, StopReason.QUERY_LIMIT)
    assert len(world.stripe.asked) == service.MAX_QUERIES
    assert world.row(payments[-1]).last_reconciled_at is None


def test_a_spent_budget_stops_before_the_next_payment(
    reconcile_world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """注入的单调时钟在第二笔之后超过 45 秒：正在处理的那一笔做完，不再开始新的一笔。"""
    world = reconcile_world
    payments = three_due(world)
    for payment in payments:
        world.serve(payment, GatewayState.OPEN)
    clock = {"now": 1000.0}
    monkeypatch.setattr(service, "time", types.SimpleNamespace(monotonic=lambda: clock["now"]))
    real_confirm = service.confirm_payment
    calls: list[str] = []

    def slow_confirm(*args: Any, **options: Any) -> Any:
        calls.append(args[2])
        if len(calls) == 2:
            clock["now"] += service.RUN_BUDGET_SECONDS + 1
        return real_confirm(*args, **options)

    monkeypatch.setattr(service, "confirm_payment", slow_confirm)

    run = reconcile(world, now=RUN_AT)

    assert (run.queries, run.stop_reason) == (2, StopReason.BUDGET_SPENT)
    assert calls == [payments[0].public_id, payments[1].public_id]
    assert world.row(payments[1]).last_reconciled_at == RUN_AT
    assert world.row(payments[2]).last_reconciled_at is None


# --- 任务：开关、直接调用、Beat 登记 -------------------------------------------------------------


@pytest.mark.parametrize(
    ("enabled", "configured"), [(False, True), (True, False)], ids=["switched-off", "no-gateway"]
)
def test_the_task_does_nothing_when_payments_are_off_or_unconfigured(
    reconcile_world: World, monkeypatch: pytest.MonkeyPatch, enabled: bool, configured: bool
) -> None:
    world = reconcile_world
    wire_task(monkeypatch, world, enabled=enabled, configured=configured)
    payment = world.payment(world.customer())
    world.serve(payment, GatewayState.PAID)
    before = world.snapshot(payment)

    assert task.run_due(now=after_created(600)) is None

    assert world.snapshot(payment) == before
    assert world.stripe.asked == []


def test_the_run_works_without_celery_on_the_current_clock(
    reconcile_world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """INV-14：不经 Celery 直接调用 run_due（now 为空，本轮读一次当前 UTC 时间）。"""
    world = reconcile_world
    wire_task(monkeypatch, world)
    payment = world.payment(world.customer())
    world.serve(payment, GatewayState.PAID)

    run = task.run_due()

    assert run is not None and run.queries == 1
    row = world.row(payment)
    assert row.status is PaymentStatus.PAID
    assert row.paid_at is not None and row.paid_at > CREATED
    assert row.paid_at.microsecond == 0
    assert row.last_reconciled_at == row.paid_at
    assert len(world.topups(payment)) == 1


@pytest.mark.parametrize(("every", "expires"), [(60, 59), (30, 29), (600, 599)])
def test_beat_runs_the_reconciliation_every_poll_interval(every: int, expires: int) -> None:
    app = create_celery_app(Settings(redis_url="memory://", payment_reconcile_poll_seconds=every))

    entry = app.conf.beat_schedule["payment-reconcile-due"]

    assert entry == {
        "task": "app.tasks.payment_reconcile.run_due",
        "schedule": every,
        "options": {"expires": expires},
    }
    assert "app.tasks.payment_reconcile" in app.conf.include
    assert "app.tasks.payment_reconcile.run_due" in app.tasks


def test_the_three_settings_have_the_designed_defaults_and_ranges() -> None:
    settings = Settings()
    assert settings.payment_reconcile_poll_seconds == 60
    assert settings.payment_reconcile_grace_seconds == 300
    assert settings.payment_reconcile_max_seconds == 604_800
    for name, low, high in (
        ("payment_reconcile_poll_seconds", 30, 600),
        ("payment_reconcile_grace_seconds", 60, 3600),
        ("payment_reconcile_max_seconds", 86_400, 2_592_000),
    ):
        assert getattr(Settings(**{name: low}), name) == low
        assert getattr(Settings(**{name: high}), name) == high
        with pytest.raises(ValidationError):
            Settings(**{name: low - 1})
        with pytest.raises(ValidationError):
            Settings(**{name: high + 1})


# --- 真 MySQL：补对账与回调并发、命名锁 -----------------------------------------------------------


def test_reconciliation_and_event_processing_at_once_credit_once(
    mysql_reconcile_world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """INV-3：补对账与事件处理同时确认同一笔 → 只有一条 TOPUP。"""
    world = mysql_reconcile_world
    wire(monkeypatch, world)
    payment = world.payment(world.customer())
    world.serve(payment, GatewayState.PAID)
    payment_event(world, payment)
    when = after_created(600)
    start = threading.Barrier(2)

    def reconciling() -> object:
        gateway = world.stripe.gateway()
        start.wait(timeout=30)
        return service.run_due(world.factory, gateway, now=when)

    def processing() -> object:
        start.wait(timeout=30)
        return events_task.process_due(now=when)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(reconciling), pool.submit(processing)]
        for future in futures:
            future.result(timeout=60)

    assert world.row(payment).status is PaymentStatus.PAID
    assert len(world.topups(payment)) == 1
    assert len(world.audits(payment)) == 1


def test_a_second_run_returns_while_the_named_lock_is_held(
    mysql_reconcile_world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """同一时刻只跑一份：另一个连接持有 `payment_reconcile:<库名>` 时这一轮直接返回。"""
    world = mysql_reconcile_world
    wire_task(monkeypatch, world)
    payment = world.payment(world.customer())
    world.serve(payment, GatewayState.PAID)
    engine = world.factory.kw["bind"]
    assert isinstance(engine, Engine)
    when = after_created(600)

    with engine.connect() as holder:
        assert (
            holder.scalar(text("SELECT GET_LOCK(CONCAT('payment_reconcile:', DATABASE()), 0)")) == 1
        )
        try:
            assert task.run_due(now=when) is None
            assert world.row(payment).status is PENDING
            assert world.stripe.asked == []
        finally:
            holder.execute(text("SELECT RELEASE_LOCK(CONCAT('payment_reconcile:', DATABASE()))"))

    run = task.run_due(now=when)
    assert run is not None and run.queries == 1
    assert world.row(payment).status is PaymentStatus.PAID
    with engine.connect() as probe:
        assert (
            probe.scalar(text("SELECT IS_FREE_LOCK(CONCAT('payment_reconcile:', DATABASE()))")) == 1
        )

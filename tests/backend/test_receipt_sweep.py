"""The receipt back-fill sweep and its Beat entry (design gate #589 v2, AIH-TASK-201).

设计 §2「补发扫描」、§4「并发」；§7 标 201 的「补发」「补发与入账并发」「Beat 登记」三行
（「告警」一行在 test_payment_alerts.py）：

- **补发**：本批上线前的两笔 `PAID`（没有收据）得到收据、`payment_method = null`、没有邮件行；
  检查不过的那笔不挡后面的；`PENDING` 与 `REVIEW_REQUIRED` 不选；每轮至多 500 笔、按 `paid_at`
  升序（同一时刻按 `id`）；预算用完即停（注入时钟）；重查已有收据的跳过、不计数；抛异常的那一笔
  回滚、记只带 `public_id` 的 ERROR、计入失败；结束时一条只带计数的 INFO；
- **补发与入账并发**（只在真 MySQL 上）：补发锁住支付行时另一个连接对同一笔开具；两轮补发同时跑。
  都只有一张收据；
- **Beat 登记**：条目名、任务、间隔与 `expires`。

支付、租户与造 PAID 支付和 TOPUP 账本行的构件用共用的 payment_world.py（`paid_payment_maker`
在 conftest.py 注册）；开具与读回的构件沿用 test_receipts.py；本文件其余夹具只在这里用，定义在
这里。时间一律注入，不 sleep。

MySQL 那部分需要 `BILLING_TEST_DATABASE_URL`，没设时 skip ——
**不要把 skipped 读成 passed**，CI 设了它并把 skipped 判成失败。
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from typing import Any

import pytest
from payment_world import (
    CREATED,
    EXPIRES,
    Customer,
    PaymentRef,
    World,
    build_mysql_world,
    build_sqlite_world,
    insert_tax_version,
    mysql_url,
    seed_receipt_series,
)
from sqlalchemy import Engine, create_engine, event, insert, select, update
from sqlalchemy.orm import Session, sessionmaker
from test_receipts import (
    JOIN_TIMEOUT_SECONDS,
    Gate,
    _limit_lock_waits,
    _wait_until_waiting,
    issue,
    payment_row,
    receipt_emails,
    receipt_of,
    receipts,
    series,
)

from app.core.celery_app import create_celery_app
from app.core.config import Settings
from app.core.database import create_session_factory
from app.models.payments import Payment, PaymentGateway, PaymentStatus
from app.models.receipts import PaymentReceipt
from app.services.receipts import RECEIPT_LEDGER_MISSING
from app.tasks import receipts as sweep_task

# 马来西亚 2026-11-10 14:05:09；晚于 payment_world 的 `CREATED`（支付的 `paid_at`）。
SWEEP_NOW = dt.datetime(2026, 11, 10, 6, 5, 9)
MINUTE = dt.timedelta(minutes=1)


# --- 夹具 ---------------------------------------------------------------


def _with_series(worlds: Iterator[World]) -> Iterator[World]:
    # `create_all` 建的 SQLite 库没有系列行；MySQL 上迁移 0039 已写过（这里什么都不做）。
    for world in worlds:
        seed_receipt_series(world.factory)
        yield world


@pytest.fixture(params=["sqlite", "mysql"])
def sweep_world(request, monkeypatch) -> Iterator[World]:
    worlds = build_mysql_world(monkeypatch) if request.param == "mysql" else build_sqlite_world()
    yield from _with_series(worlds)


@pytest.fixture
def mysql_sweep_world(monkeypatch) -> Iterator[World]:
    yield from _with_series(build_mysql_world(monkeypatch))


# --- 构件 ---------------------------------------------------------------


def fixed(moment: dt.datetime = SWEEP_NOW) -> Callable[[], dt.datetime]:
    return lambda: moment


class StepClock:
    """Each read returns the previous one plus `step`, starting at `start`."""

    def __init__(self, start: dt.datetime, step: dt.timedelta) -> None:
        self.reads: list[dt.datetime] = []
        self._next = start
        self._step = step

    def __call__(self) -> dt.datetime:
        moment = self._next
        self._next += self._step
        self.reads.append(moment)
        return moment


def ready(
    world: World,
    maker: Callable[..., PaymentRef],
    *,
    tenant: Customer | None = None,
    paid: dt.datetime | None = None,
    ledger: bool = True,
) -> PaymentRef:
    """A `PAID` payment as before this batch: no receipt; with its `TOPUP` row unless `ledger`."""
    payment = maker(
        world,
        tenant or world.customer(),
        tax_version_id=insert_tax_version(world),
        ledger=ledger,
    )
    if paid is not None:
        set_paid_at(world, [payment.public_id], paid)
    return payment


def set_paid_at(world: World, public_ids: list[str], moment: dt.datetime) -> None:
    with world.factory() as session:
        session.execute(
            update(Payment).where(Payment.public_id.in_(public_ids)).values(paid_at=moment)
        )
        session.commit()


def bulk_paid_without_ledger(
    world: World, tenant: Customer, count: int, paid: dt.datetime
) -> list[str]:
    """`count` `PAID` payments with no `TOPUP` row: the first pre-issue check fails for each."""
    rows: list[dict[str, Any]] = [
        {
            "public_id": str(uuid.uuid4()),
            "tenant_id": tenant.id,
            "gateway": PaymentGateway.STRIPE,
            "expected_amount": Decimal("100.00"),
            "expected_currency": "MYR",
            "wallet_credit_amount": Decimal("100.00"),
            "tax_policy_version_id": world.tax_version_id,
            "tax_amount": None,
            "confirmed_amount": Decimal("100.00"),
            "confirmed_currency": "MYR",
            "gateway_payment_id": f"cs_test_{uuid.uuid4().hex}",
            "gateway_payment_intent_id": f"pi_test_{uuid.uuid4().hex}",
            "gateway_fee": None,
            "status": PaymentStatus.PAID,
            "status_reason": None,
            "review_required_at": None,
            "expires_at": EXPIRES,
            "paid_at": paid,
            "last_reconciled_at": None,
            "created_at": CREATED,
            "updated_at": CREATED,
        }
        for _ in range(count)
    ]
    with world.factory() as session:
        session.execute(insert(Payment), rows)
        session.commit()
    return [row["public_id"] for row in rows]


def has_receipt(world: World, payment: PaymentRef | str) -> bool:
    public_id = payment if isinstance(payment, str) else payment.public_id
    with world.factory() as session:
        found = session.execute(
            select(PaymentReceipt.id)
            .join(Payment, Payment.id == PaymentReceipt.payment_id)
            .where(Payment.public_id == public_id)
        ).first()
        return found is not None


def run(
    world: World,
    clock: Callable[[], dt.datetime] | None = None,
    factory: sessionmaker[Session] | None = None,
) -> tuple[int, int]:
    return sweep_task.sweep(factory or world.factory, clock=clock or fixed())


def _records(caplog: pytest.LogCaptureFixture, level: int) -> list[logging.LogRecord]:
    return [
        record
        for record in caplog.records
        if record.name == sweep_task.__name__ and record.levelno == level
    ]


# --- 补发 ---------------------------------------------------------------


def test_paid_payments_from_before_the_batch_get_a_receipt_and_no_email(
    sweep_world: World,
    paid_payment_maker: Callable[..., PaymentRef],
    caplog: pytest.LogCaptureFixture,
) -> None:
    world = sweep_world
    older = ready(world, paid_payment_maker, paid=CREATED - MINUTE)
    newer = ready(world, paid_payment_maker, paid=CREATED)

    with caplog.at_level(logging.INFO, logger=sweep_task.__name__):
        result = run(world)

    assert result == (2, 0)
    first, second = receipt_of(world, older), receipt_of(world, newer)
    # 按 paid_at 升序开具：编号也按这个顺序。
    assert first.receipt_number == "ACV-AIR-2026-000001"
    assert second.receipt_number == "ACV-AIR-2026-000002"
    for row in (first, second):
        assert row.snapshot_json["payment"]["payment_method"] is None
        assert row.issued_at == SWEEP_NOW
    assert receipt_emails(world) == []
    [finished] = _records(caplog, logging.INFO)
    assert finished.issued == 2  # type: ignore[attr-defined]
    assert finished.failed == 0  # type: ignore[attr-defined]


def test_the_sweep_does_nothing_more_once_every_paid_payment_has_a_receipt(
    sweep_world: World, paid_payment_maker: Callable[..., PaymentRef]
) -> None:
    world = sweep_world
    ready(world, paid_payment_maker)

    assert run(world) == (1, 0)
    before = [(row.id, row.receipt_number) for row in receipts(world)]

    assert run(world) == (0, 0)
    assert [(row.id, row.receipt_number) for row in receipts(world)] == before
    assert series(world)[2026] == 1


def test_a_payment_failing_the_checks_does_not_block_the_ones_after_it(
    sweep_world: World,
    paid_payment_maker: Callable[..., PaymentRef],
    caplog: pytest.LogCaptureFixture,
) -> None:
    world = sweep_world
    # 没有 TOPUP 行，且 paid_at 更早：先被处理。
    broken = ready(world, paid_payment_maker, paid=CREATED - MINUTE, ledger=False)
    fine = ready(world, paid_payment_maker, paid=CREATED)

    with caplog.at_level(logging.INFO, logger=sweep_task.__name__):
        result = run(world)

    assert result == (1, 1)
    assert not has_receipt(world, broken)
    assert receipt_of(world, fine).receipt_number == "ACV-AIR-2026-000001"
    # 原因码由 issue_receipt 记；扫描结束的 INFO 只有计数。
    assert any(
        getattr(record, "reason", None) == RECEIPT_LEDGER_MISSING
        and getattr(record, "payment_id", None) == broken.public_id
        for record in caplog.records
    )
    [finished] = _records(caplog, logging.INFO)
    assert (finished.issued, finished.failed) == (1, 1)  # type: ignore[attr-defined]

    # 下一轮照样再试：仍是失败，正常的那笔不再计数。
    assert run(world) == (0, 1)


@pytest.mark.parametrize(
    "status",
    [
        PaymentStatus.PENDING,
        PaymentStatus.REVIEW_REQUIRED,
        PaymentStatus.FAILED,
        PaymentStatus.EXPIRED,
        PaymentStatus.CANCELLED,
    ],
    ids=lambda s: s.value,
)
def test_only_paid_payments_are_candidates(
    sweep_world: World, paid_payment_maker: Callable[..., PaymentRef], status: PaymentStatus
) -> None:
    world = sweep_world
    other = world.payment(world.customer(), status=status)
    paid = ready(world, paid_payment_maker)

    assert run(world) == (1, 0)
    assert has_receipt(world, paid)
    assert not has_receipt(world, other)


def test_one_run_takes_at_most_500_payments_oldest_paid_at_first(
    sweep_world: World,
    paid_payment_maker: Callable[..., PaymentRef],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """500 笔更早的、开具检查不过的支付占满一轮：更新的那笔这一轮不处理（设计 §10 已知限制 3）。"""
    world = sweep_world
    assert sweep_task.SWEEP_BATCH_SIZE == 500
    tenant = world.customer()
    stuck = bulk_paid_without_ledger(world, tenant, 500, CREATED - dt.timedelta(hours=1))
    newest = ready(world, paid_payment_maker, tenant=tenant, paid=CREATED)

    with caplog.at_level(logging.ERROR, logger="app.services.receipts"):
        result = run(world)

    assert result == (0, 500)
    assert not has_receipt(world, newest)
    refused = {
        getattr(record, "payment_id", None)
        for record in caplog.records
        if getattr(record, "reason", None) == RECEIPT_LEDGER_MISSING
    }
    assert refused == set(stuck)

    # 有一笔收口之后，下一轮就轮得到最新的那笔。
    set_paid_at(world, [stuck[0]], CREATED + MINUTE)
    assert run(world) == (1, 499)
    assert has_receipt(world, newest)


def test_candidates_are_ordered_by_paid_at_then_id(
    sweep_world: World,
    paid_payment_maker: Callable[..., PaymentRef],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """批量上限换成 2，看清楚取的是哪两笔：`paid_at` 最早的，同一时刻按 `id`。"""
    world = sweep_world
    monkeypatch.setattr(sweep_task, "SWEEP_BATCH_SIZE", 2)
    latest = ready(world, paid_payment_maker, paid=CREATED + MINUTE)
    tied_first = ready(world, paid_payment_maker, paid=CREATED)
    tied_second = ready(world, paid_payment_maker, paid=CREATED)
    assert payment_row(world, tied_first).id < payment_row(world, tied_second).id

    assert run(world) == (2, 0)

    assert receipt_of(world, tied_first).receipt_number == "ACV-AIR-2026-000001"
    assert receipt_of(world, tied_second).receipt_number == "ACV-AIR-2026-000002"
    assert not has_receipt(world, latest)


def test_the_run_stops_when_the_240_second_budget_is_spent(
    sweep_world: World, paid_payment_maker: Callable[..., PaymentRef]
) -> None:
    """每次读时钟前进 100 秒：开始 t、第一笔前 t+100、第一笔的 now t+200、
    第二笔前 t+300 ≥ 240 → 停。"""
    world = sweep_world
    first = ready(world, paid_payment_maker, paid=CREATED - MINUTE)
    second = ready(world, paid_payment_maker, paid=CREATED)
    clock = StepClock(SWEEP_NOW, dt.timedelta(seconds=100))

    assert run(world, clock) == (1, 0)

    assert len(clock.reads) == 4
    # `now` 在这一笔开始时读一次，就是开具时刻。
    assert receipt_of(world, first).issued_at == SWEEP_NOW + dt.timedelta(seconds=200)
    assert not has_receipt(world, second)


def test_the_budget_is_checked_before_each_payment(
    sweep_world: World, paid_payment_maker: Callable[..., PaymentRef]
) -> None:
    """预算还剩时每一笔都处理，每笔各读一次 `now`。"""
    world = sweep_world
    first = ready(world, paid_payment_maker, paid=CREATED - MINUTE)
    second = ready(world, paid_payment_maker, paid=CREATED)
    clock = StepClock(SWEEP_NOW, dt.timedelta(seconds=1))

    assert run(world, clock) == (2, 0)

    assert receipt_of(world, first).issued_at == SWEEP_NOW + dt.timedelta(seconds=2)
    assert receipt_of(world, second).issued_at == SWEEP_NOW + dt.timedelta(seconds=4)


def test_a_payment_that_got_its_receipt_meanwhile_is_skipped_and_not_counted(
    sweep_world: World,
    paid_payment_maker: Callable[..., PaymentRef],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """候选查询之后、锁住支付行之前，入账路径已给它开了收据：重查后跳过。"""
    world = sweep_world
    raced = ready(world, paid_payment_maker, paid=CREATED - MINUTE)
    other = ready(world, paid_payment_maker, paid=CREATED)
    candidates = sweep_task._candidates

    def candidates_then_credit(factory: sessionmaker[Session]) -> list[tuple[int, str]]:
        found = candidates(factory)
        assert issue(world, raced, method="card") is not None
        return found

    monkeypatch.setattr(sweep_task, "_candidates", candidates_then_credit)

    assert run(world) == (1, 0)

    assert receipt_of(world, raced).snapshot_json["payment"]["payment_method"] == "card"
    assert receipt_of(world, other).receipt_number == "ACV-AIR-2026-000002"
    assert len(receipts(world)) == 2


def test_an_exception_rolls_back_that_payment_logs_only_its_id_and_the_run_goes_on(
    sweep_world: World,
    paid_payment_maker: Callable[..., PaymentRef],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    world = sweep_world
    failing = ready(world, paid_payment_maker, paid=CREATED - MINUTE)
    fine = ready(world, paid_payment_maker, paid=CREATED)
    real_issue = sweep_task.issue_receipt
    secret = "Payment Confirm Test Sdn Bhd"

    def issue_then_fail(session: Session, *, payment: Payment, **options: Any) -> Any:
        issued = real_issue(session, payment=payment, **options)
        if payment.public_id == failing.public_id:
            # 写入已 flush：异常之后这一笔整个回滚。异常文本带着不该进日志的值。
            raise RuntimeError(f"fictional database error for {secret}")
        return issued

    monkeypatch.setattr(sweep_task, "issue_receipt", issue_then_fail)

    with caplog.at_level(logging.INFO, logger=sweep_task.__name__):
        result = run(world)

    assert result == (1, 1)
    assert not has_receipt(world, failing)
    # 回滚的那一笔没有占掉编号。
    assert receipt_of(world, fine).receipt_number == "ACV-AIR-2026-000001"
    assert series(world)[2026] == 1
    [error] = _records(caplog, logging.ERROR)
    assert error.payment_id == failing.public_id  # type: ignore[attr-defined]
    assert error.exc_info is None
    assert secret not in error.getMessage()
    assert "fictional database error" not in caplog.text
    [finished] = _records(caplog, logging.INFO)
    assert (finished.issued, finished.failed) == (1, 1)  # type: ignore[attr-defined]


def test_the_task_issues_even_while_payments_are_switched_off(
    sweep_world: World,
    paid_payment_maker: Callable[..., PaymentRef],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """不看 `payments_enabled`：开关关闭时也补发（不调 Stripe）。"""
    world = sweep_world
    payment = ready(world, paid_payment_maker)
    monkeypatch.setattr(sweep_task, "get_settings", lambda: Settings(payments_enabled=False))
    monkeypatch.setattr(sweep_task, "_session_factory", lambda: world.factory)
    monkeypatch.setattr(sweep_task, "utc_now", fixed())

    assert sweep_task.issue_missing() is None

    assert receipt_of(world, payment).issued_at == SWEEP_NOW
    assert receipt_emails(world) == []


# --- Beat 登记 ----------------------------------------------------------


def test_the_sweep_runs_every_300_seconds() -> None:
    entry = create_celery_app(Settings(redis_url="memory://")).conf.beat_schedule[
        "receipts-issue-missing"
    ]

    assert entry["task"] == "app.tasks.receipts.issue_missing"
    assert entry["schedule"] == 300
    assert entry["options"] == {"expires": 299}


# --- 真 MySQL：补发与入账并发 -------------------------------------------


def _locks_payment(statement: str) -> bool:
    """`SELECT … FROM payments … FOR UPDATE` (整个用例只在 MySQL 上跑)."""
    sql = " ".join(statement.replace("`", "").lower().split())
    return sql.startswith("select ") and " from payments " in sql and sql.endswith(" for update")


@pytest.fixture
def engines() -> Iterator[Callable[..., Engine]]:
    """Engines of their own, one per side of a race, so a gate only stops that side.

    `connection_ids` 收集到的是这个引擎唯一那条连接的 id（单连接池）。
    """
    url = mysql_url()
    made: list[Engine] = []

    def make(*, observer: bool = False, connection_ids: list[int] | None = None) -> Engine:
        if observer:
            engine = create_engine(url, isolation_level="AUTOCOMMIT", pool_size=1)
        else:
            engine = create_engine(url, pool_size=1, max_overflow=0)
            event.listen(engine, "connect", _limit_lock_waits)
            if connection_ids is not None:

                def remember(dbapi_connection: Any, _record: Any) -> None:
                    cursor = dbapi_connection.cursor()
                    try:
                        cursor.execute("SELECT CONNECTION_ID()")
                        connection_ids.append(int(cursor.fetchone()[0]))
                    finally:
                        cursor.close()
                    dbapi_connection.rollback()

                event.listen(engine, "connect", remember)
        made.append(engine)
        return engine

    try:
        yield make
    finally:
        for engine in made:
            engine.dispose()


def test_a_credit_waiting_on_the_locked_payment_finds_the_sweeps_receipt(
    mysql_sweep_world: World,
    paid_payment_maker: Callable[..., PaymentRef],
    engines: Callable[..., Engine],
) -> None:
    """设计 §4「补发与入账」：补发锁住支付行时另一个连接对同一笔开具，等锁后幂等返回同一张。"""
    world = mysql_sweep_world
    payment = ready(world, paid_payment_maker)
    sweep_engine = engines()
    other_ids: list[int] = []
    other_engine = engines(connection_ids=other_ids)
    observer = engines(observer=True)
    gate = Gate(sweep_engine, _locks_payment)

    with ThreadPoolExecutor(max_workers=2) as pool:
        try:
            swept = pool.submit(run, world, None, create_session_factory(sweep_engine))
            # 补发已锁住支付行，还没开具。
            gate.wait_reached()
            other = pool.submit(issue, world, payment, factory=create_session_factory(other_engine))
            _wait_until_waiting(observer, other_ids, "payments")
        finally:
            gate.release()
        sweep_result = swept.result(timeout=JOIN_TIMEOUT_SECONDS)
        other_result = other.result(timeout=JOIN_TIMEOUT_SECONDS)

    assert sweep_result == (1, 0)
    [row] = receipts(world)
    assert other_result is not None
    assert other_result.created is False
    assert other_result.public_id == row.public_id
    assert row.snapshot_json["payment"]["payment_method"] is None
    assert receipt_emails(world) == []
    assert series(world)[2026] == 1


def test_two_overlapping_sweeps_issue_one_receipt(
    mysql_sweep_world: World,
    paid_payment_maker: Callable[..., PaymentRef],
    engines: Callable[..., Engine],
) -> None:
    """设计 §4「两轮补发重叠」：第二轮在支付行锁上等待，拿到后重查已有收据 → 跳过、不计数。"""
    world = mysql_sweep_world
    payment = ready(world, paid_payment_maker)
    first_engine = engines()
    second_ids: list[int] = []
    second_engine = engines(connection_ids=second_ids)
    observer = engines(observer=True)
    gate = Gate(first_engine, _locks_payment)

    with ThreadPoolExecutor(max_workers=2) as pool:
        try:
            first = pool.submit(run, world, None, create_session_factory(first_engine))
            gate.wait_reached()
            # 第一轮还没提交：第二轮的候选查询里仍有这笔，随后在支付行锁上等待。
            second = pool.submit(run, world, None, create_session_factory(second_engine))
            _wait_until_waiting(observer, second_ids, "payments")
        finally:
            gate.release()
        first_result = first.result(timeout=JOIN_TIMEOUT_SECONDS)
        second_result = second.result(timeout=JOIN_TIMEOUT_SECONDS)

    assert first_result == (1, 0)
    assert second_result == (0, 0)
    [row] = receipts(world)
    assert row.payment_id == payment_row(world, payment).id
    assert series(world)[2026] == 1

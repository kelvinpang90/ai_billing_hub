"""Payment webhook events: recording and the processing task (design gate #580 v3, AIH-TASK-175).

设计 §7 标 175 的行（告警那一行在 test_payment_alerts.py）：

- **落库**：四种处理类型 `RECEIVED`、其余 `IGNORED`、`livemode` 不一致、重复投递、
  指纹冲突（再次冲突更新时刻）；
- **处理**：一次领一个与租约、重试退避区间、Stripe 不可用、会话 404（`REJECTED` 带错误码）、
  未知与缺失的 `client_reference_id`（`UNMATCHED`）、状态未知与确认冲突、成功后清空错误码、
  认回会话四组、租约过期重领后不重复入账（旧租约的结果被丢弃）、开关关闭或凭据读不到时不领取、
  每轮上限与预算、不经 Celery 直接调用扫描函数；
- **MySQL 命名锁**：另一个连接持锁时这一轮直接返回。

支付与租户沿用 test_payment_confirm.py 的世界（SQLite 与真 MySQL 各跑一次）；
它在每个用例前后删掉支付与事件行（MySQL 的库是共用的，告警用例不应看到支付段落）。
Stripe 一律是注入了假传输（`httpx2.MockTransport`）的 `StripeGateway`，不联网；地址是虚构的。

MySQL 那部分需要 `BILLING_TEST_DATABASE_URL`，没设时 skip ——
**不要把 skipped 读成 passed**，CI 设了它并把 skipped 判成失败。
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import json
import logging
import random
import types
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import Engine, func, select, text, update
from test_payment_confirm import (
    CREATED,
    EXPIRES,
    NOW,
    SECRET_KEY,
    WEBHOOK_SECRET,
    PaymentRef,
    World,
    _mysql_world,
    _sqlite_world,
)

from app.core.celery_app import create_celery_app
from app.core.config import Settings
from app.models.payments import (
    PaymentEventProcessingStatus,
    PaymentEventVerificationStatus,
    PaymentGateway,
    PaymentGatewayEvent,
    PaymentStatus,
)
from app.payments.base import GatewayState
from app.services import payment_events as recording
from app.services.payment_events import (
    HANDLED_EVENT_TYPES,
    LIVEMODE_MISMATCH,
    RecordOutcome,
    record_event,
)
from app.services.payments import ConfirmConflict
from app.tasks import payment_events as task

RECEIVED = PaymentEventProcessingStatus.RECEIVED
PROCESSING = PaymentEventProcessingStatus.PROCESSING
RETRY = PaymentEventProcessingStatus.RETRY
PROCESSED = PaymentEventProcessingStatus.PROCESSED
IGNORED = PaymentEventProcessingStatus.IGNORED
UNMATCHED = PaymentEventProcessingStatus.UNMATCHED
REJECTED = PaymentEventProcessingStatus.REJECTED

COMPLETED = "checkout.session.completed"
EXPIRED_EVENT = "checkout.session.expired"
SECOND = dt.timedelta(seconds=1)
LATER = NOW + task.LEASE + SECOND
AFTER_EXPIRY = EXPIRES + dt.timedelta(minutes=1)

TASK_LOGGER = "app.tasks.payment_events"
SERVICE_LOGGER = "app.services.payment_events"


# --- 事件 -------------------------------------------------------------------------------


def event_body(
    event_type: str = COMPLETED,
    *,
    event_id: str | None = None,
    reference: str | None = None,
    session_id: str | None = None,
    livemode: bool = False,
    amount_total: int = 10000,
) -> bytes:
    """A Stripe event body around a Checkout Session, with personal data that must not be stored."""
    session: dict[str, Any] = {
        "id": session_id,
        "object": "checkout.session",
        "client_reference_id": reference,
        "payment_status": "paid",
        "status": "complete",
        "amount_total": amount_total,
        "currency": "myr",
        "payment_intent": f"pi_test_{uuid.uuid4().hex}",
        "customer_details": {"email": "payer@example.com", "name": "Fictional Payer"},
        "metadata": {"payment_id": reference},
    }
    event = {
        "id": event_id or f"evt_test_{uuid.uuid4().hex}",
        "object": "event",
        "type": event_type,
        "livemode": livemode,
        "created": 1762588800,
        "data": {"object": session},
    }
    return json.dumps(event, sort_keys=True).encode("utf-8")


def record(
    world: World, body: bytes, *, now: dt.datetime = NOW, livemode: bool = False
) -> RecordOutcome:
    parsed = world.stripe.gateway().parse_webhook(body)
    return record_event(world.factory, parsed, body, now=now, livemode=livemode)


def event_id_of(body: bytes) -> str:
    return str(json.loads(body)["id"])


def event_row(world: World, body_or_id: bytes | str) -> PaymentGatewayEvent:
    gateway_event_id = body_or_id if isinstance(body_or_id, str) else event_id_of(body_or_id)
    with world.factory() as session:
        row = session.execute(
            select(PaymentGatewayEvent).where(
                PaymentGatewayEvent.gateway_event_id == gateway_event_id
            )
        ).scalar_one()
        session.expunge(row)
        return row


def event_count(world: World) -> int:
    with world.factory() as session:
        return int(
            session.execute(select(func.count()).select_from(PaymentGatewayEvent)).scalar_one()
        )


def set_event(world: World, body: bytes, **values: Any) -> None:
    with world.factory() as session:
        session.execute(
            update(PaymentGatewayEvent)
            .where(PaymentGatewayEvent.gateway_event_id == event_id_of(body))
            .values(**values)
        )
        session.commit()


def columns(row: PaymentGatewayEvent) -> dict[str, object]:
    return {
        column.key: getattr(row, column.key) for column in PaymentGatewayEvent.__table__.columns
    }


def payment_event(world: World, payment: PaymentRef, event_type: str = COMPLETED) -> bytes:
    """Record an event for `payment` (carrying its session id) and return its body."""
    body = event_body(event_type, reference=payment.public_id, session_id=payment.session_id)
    assert record(world, body) is RecordOutcome.RECORDED
    return body


class FixedRandom(random.Random):
    """`uniform(0.5, 1.0)` always answers `factor`."""

    def __init__(self, factor: float) -> None:
        super().__init__(0)
        self.factor = factor

    def uniform(self, a: float, b: float) -> float:
        assert (a, b) == (0.5, 1.0)
        return self.factor


# --- 夹具 -------------------------------------------------------------------------------


@pytest.fixture(params=["sqlite", "mysql"])
def events_world(request, monkeypatch) -> Iterator[World]:
    if request.param == "mysql":
        yield from _mysql_world(monkeypatch)
        return
    yield from _sqlite_world()


@pytest.fixture
def mysql_events_world(monkeypatch) -> Iterator[World]:
    yield from _mysql_world(monkeypatch)


def wire(
    monkeypatch: pytest.MonkeyPatch,
    world: World,
    *,
    enabled: bool = True,
    configured: bool = True,
) -> None:
    """Point the task at this world's database and fake Stripe, as a worker would see them."""
    monkeypatch.setattr(task, "_session_factory", lambda: world.factory)
    monkeypatch.setattr(task, "get_settings", lambda: Settings(payments_enabled=enabled))
    monkeypatch.setattr(
        task,
        "get_payment_gateway",
        lambda settings: world.stripe.gateway() if configured else None,
    )


# --- 落库 -------------------------------------------------------------------------------


@pytest.mark.parametrize("event_type", sorted(HANDLED_EVENT_TYPES))
def test_the_four_handled_types_are_recorded_as_received(
    events_world: World, event_type: str
) -> None:
    world = events_world
    reference = str(uuid.uuid4())
    body = event_body(event_type, reference=reference, session_id="cs_test_recorded")
    parsed = world.stripe.gateway().parse_webhook(body)

    outcome = record_event(world.factory, parsed, body, now=NOW, livemode=False)

    assert outcome is RecordOutcome.RECORDED
    row = event_row(world, body)
    assert row.gateway is PaymentGateway.STRIPE
    assert (row.gateway_event_id, row.event_type) == (parsed.gateway_event_id, event_type)
    assert row.verification_status is PaymentEventVerificationStatus.VERIFIED
    assert row.processing_status is RECEIVED
    assert row.payload_fingerprint == hashlib.sha256(body).hexdigest()
    assert row.metadata_json == dict(parsed.fields)
    assert (row.received_at, row.next_attempt_at) == (NOW, NOW)
    assert (row.attempts, row.lease_until, row.payment_id) == (0, None, None)
    assert (row.last_error_code, row.processed_at, row.fingerprint_conflict_at) == (
        None,
        None,
        None,
    )
    # 白名单：不存原始请求体，不存客户的邮箱与姓名，也不存 Stripe 对象自己的 metadata。
    stored = json.dumps(row.metadata_json)
    assert "payer@example.com" not in stored and "Fictional Payer" not in stored
    assert "customer_details" not in row.metadata_json and "metadata" not in row.metadata_json


# `charge.refunded` 自 AIH-TASK-181 起落为 RECEIVED（见 test_payment_disputes.py），
# 这里换成仍不处理的类型。
@pytest.mark.parametrize("event_type", ["payment_intent.succeeded", "charge.succeeded"])
def test_other_types_are_recorded_as_ignored(events_world: World, event_type: str) -> None:
    world = events_world
    body = event_body(event_type, reference=str(uuid.uuid4()))

    assert record(world, body) is RecordOutcome.RECORDED

    row = event_row(world, body)
    assert row.processing_status is IGNORED
    assert (row.last_error_code, row.processed_at) == (None, NOW)
    assert (row.received_at, row.next_attempt_at) == (NOW, NOW)


@pytest.mark.parametrize(("event_livemode", "configured"), [(True, False), (False, True)])
@pytest.mark.parametrize("event_type", [COMPLETED, "payment_intent.succeeded"])
def test_a_livemode_mismatch_is_ignored_with_its_reason(
    events_world: World, event_type: str, event_livemode: bool, configured: bool
) -> None:
    world = events_world
    body = event_body(event_type, reference=str(uuid.uuid4()), livemode=event_livemode)

    assert record(world, body, livemode=configured) is RecordOutcome.RECORDED

    row = event_row(world, body)
    assert row.processing_status is IGNORED
    assert (row.last_error_code, row.processed_at) == (LIVEMODE_MISMATCH, NOW)
    assert row.next_attempt_at == row.received_at == NOW


def test_a_redelivery_of_the_same_body_is_a_duplicate(events_world: World) -> None:
    world = events_world
    body = event_body(reference=str(uuid.uuid4()))
    assert record(world, body) is RecordOutcome.RECORDED
    before = columns(event_row(world, body))

    assert record(world, body, now=LATER) is RecordOutcome.DUPLICATE

    assert event_count(world) == 1
    assert columns(event_row(world, body)) == before


def test_a_different_body_under_the_same_id_is_a_conflict_every_time(
    events_world: World, caplog: pytest.LogCaptureFixture
) -> None:
    """不覆盖原行；`fingerprint_conflict_at` 每次冲突都更新为本次的 now；记 ERROR。"""
    world = events_world
    event_id = f"evt_test_{uuid.uuid4().hex}"
    original = event_body(event_id=event_id, reference=str(uuid.uuid4()))
    assert record(world, original) is RecordOutcome.RECORDED
    before = columns(event_row(world, event_id))
    tampered = event_body(event_id=event_id, reference=str(uuid.uuid4()), amount_total=1)

    with caplog.at_level(logging.INFO, logger=SERVICE_LOGGER):
        first = record(world, tampered, now=NOW + SECOND)
    assert first is RecordOutcome.CONFLICT
    after = columns(event_row(world, event_id))
    assert after.pop("fingerprint_conflict_at") == NOW + SECOND
    before.pop("fingerprint_conflict_at")
    assert after == before
    assert after["payload_fingerprint"] == hashlib.sha256(original).hexdigest()
    errors = [r for r in caplog.records if r.name == SERVICE_LOGGER and r.levelno == logging.ERROR]
    assert len(errors) == 1
    assert "payer@example.com" not in errors[0].getMessage()

    again = record(world, tampered, now=LATER)

    assert again is RecordOutcome.CONFLICT
    assert event_row(world, event_id).fingerprint_conflict_at == LATER
    # 原请求体再来一次仍是重复投递，冲突时刻不变。
    assert record(world, original, now=LATER + SECOND) is RecordOutcome.DUPLICATE
    assert event_row(world, event_id).fingerprint_conflict_at == LATER
    assert event_count(world) == 1


def test_the_fingerprint_is_the_sha256_of_the_raw_body() -> None:
    assert recording.payload_fingerprint(b"{}") == hashlib.sha256(b"{}").hexdigest()
    assert len(recording.payload_fingerprint(b"x")) == 64


# --- 领取 -------------------------------------------------------------------------------


def test_events_are_claimed_one_at_a_time_under_a_lease(events_world: World) -> None:
    """按 next_attempt_at 升序一次领一个；置 PROCESSING、租约 60 秒、attempts + 1。

    租约过期才可重领。"""
    world = events_world
    early = event_body(reference=str(uuid.uuid4()))
    late = event_body(reference=str(uuid.uuid4()))
    waiting = event_body(reference=str(uuid.uuid4()))
    ignored = event_body("payment_intent.succeeded", reference=str(uuid.uuid4()))
    record(world, late, now=NOW - SECOND)
    record(world, early, now=NOW - 2 * SECOND)
    record(world, waiting, now=NOW - 3 * SECOND)
    record(world, ignored, now=NOW - 4 * SECOND)
    set_event(world, waiting, processing_status=RETRY, next_attempt_at=NOW + dt.timedelta(hours=2))

    first = task.claim_one(world.factory, now=NOW)
    assert first is not None
    assert first.gateway_event_id == event_id_of(early)
    assert (first.attempts, first.lease_until) == (1, NOW + task.LEASE)
    row = event_row(world, early)
    assert (row.processing_status, row.lease_until, row.attempts) == (
        PROCESSING,
        NOW + task.LEASE,
        1,
    )
    assert event_row(world, late).processing_status is RECEIVED

    second = task.claim_one(world.factory, now=NOW)
    assert second is not None and second.gateway_event_id == event_id_of(late)
    # 租约未过期、RETRY 未到期、IGNORED：都领不到。
    assert task.claim_one(world.factory, now=NOW) is None
    assert task.claim_one(world.factory, now=NOW + task.LEASE) is None

    reclaimed = task.claim_one(world.factory, now=LATER)
    assert reclaimed is not None
    # 租约过期的两个里 next_attempt_at 最早的那个。
    assert reclaimed.gateway_event_id == event_id_of(early)
    assert (reclaimed.attempts, reclaimed.lease_until) == (2, LATER + task.LEASE)
    assert event_row(world, ignored).attempts == 0


# --- 处理 -------------------------------------------------------------------------------


def test_a_paid_session_is_processed_and_credited_once(
    events_world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = events_world
    wire(monkeypatch, world)
    payment = world.payment(world.customer())
    world.serve(payment, GatewayState.PAID)
    body = payment_event(world, payment)

    assert task.process_due(now=NOW) == 1

    row = event_row(world, body)
    assert row.processing_status is PROCESSED
    assert (row.last_error_code, row.processed_at, row.lease_until) == (None, NOW, None)
    assert row.payment_id == world.row(payment).id
    assert row.attempts == 1
    assert world.row(payment).status is PaymentStatus.PAID
    assert len(world.topups(payment)) == 1
    assert world.stripe.asked == [payment.session_id]


def test_success_after_a_retry_clears_the_error_code(
    events_world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stripe 不可用 → RETRY（GATEWAY_UNAVAILABLE），支付不变；恢复后 → PROCESSED、错误码清空。"""
    world = events_world
    wire(monkeypatch, world)
    payment = world.payment(world.customer())
    world.serve(payment, GatewayState.PAID)
    body = payment_event(world, payment)
    world.stripe.status_code = 503

    assert task.process_due(now=NOW, rng=FixedRandom(1.0)) == 1

    row = event_row(world, body)
    assert row.processing_status is RETRY
    assert row.last_error_code == task.GATEWAY_UNAVAILABLE
    assert (row.processed_at, row.lease_until) == (None, None)
    assert row.next_attempt_at == NOW + dt.timedelta(seconds=2)
    assert row.payment_id == world.row(payment).id
    assert world.row(payment).status is PaymentStatus.PENDING
    assert world.topups() == []
    # 未到期：这一轮领不到。
    assert task.process_due(now=NOW + SECOND) == 0

    world.stripe.status_code = 200
    assert task.process_due(now=NOW + 2 * SECOND) == 1

    row = event_row(world, body)
    assert (row.processing_status, row.last_error_code) == (PROCESSED, None)
    assert (row.processed_at, row.attempts) == (NOW + 2 * SECOND, 2)
    assert len(world.topups(payment)) == 1


@pytest.mark.parametrize(
    ("attempts", "base"),
    [(1, 2), (2, 4), (5, 32), (11, 2048), (12, 3600), (13, 3600), (40, 3600)],
)
def test_retries_back_off_within_half_to_full_of_the_capped_power_of_two(
    events_world: World, monkeypatch: pytest.MonkeyPatch, attempts: int, base: int
) -> None:
    """`min(3600, 2^min(attempts, 12)) × uniform(0.5, 1.0)` 秒（attempts 是领取后的值），不放弃。"""
    world = events_world
    wire(monkeypatch, world)
    payment = world.payment(world.customer())
    world.serve(payment, GatewayState.PAID)
    world.stripe.status_code = 503
    highest = payment_event(world, payment)
    lowest = payment_event(world, payment)
    set_event(world, highest, attempts=attempts - 1)
    set_event(world, lowest, attempts=attempts - 1, next_attempt_at=NOW + SECOND)

    # 先处理 highest（因子 1.0）：它的下一次在 NOW + base ≥ NOW + 2 秒，不会被下一轮领到。
    assert task.process_due(now=NOW, rng=FixedRandom(1.0)) == 1
    assert task.process_due(now=NOW + SECOND, rng=FixedRandom(0.5)) == 1

    low, high = event_row(world, lowest), event_row(world, highest)
    assert (low.processing_status, high.processing_status) == (RETRY, RETRY)
    assert (low.attempts, high.attempts) == (attempts, attempts)
    assert (low.processed_at, high.processed_at) == (None, None)
    assert high.next_attempt_at == NOW + dt.timedelta(seconds=base)
    assert low.next_attempt_at == NOW + SECOND + dt.timedelta(seconds=base // 2)


@pytest.mark.parametrize("attempts", [1, 3, 8, 12, 30])
def test_the_retry_delay_stays_in_its_interval(attempts: int) -> None:
    base = min(3600, 2 ** min(attempts, 12))
    rng = random.Random(attempts)
    delays = [task.retry_delay_seconds(attempts, rng) for _ in range(200)]
    assert all(base / 2 <= delay <= base for delay in delays)
    assert all(isinstance(delay, int) for delay in delays)


@pytest.mark.parametrize(
    ("reference", "code"),
    [(str(uuid.uuid4()), task.PAYMENT_NOT_FOUND), (None, task.NO_CLIENT_REFERENCE)],
    ids=["unknown", "missing"],
)
def test_an_event_without_a_matching_payment_is_unmatched(
    events_world: World, monkeypatch: pytest.MonkeyPatch, reference: str | None, code: str
) -> None:
    world = events_world
    wire(monkeypatch, world)
    body = event_body(reference=reference, session_id="cs_test_unknown")
    record(world, body)

    assert task.process_due(now=NOW) == 1

    row = event_row(world, body)
    assert (row.processing_status, row.last_error_code) == (UNMATCHED, code)
    assert (row.processed_at, row.payment_id, row.lease_until) == (NOW, None, None)
    assert world.stripe.asked == []


def test_a_session_stripe_does_not_know_is_rejected_with_its_error_code(
    events_world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """会话 404：REJECTED、last_error_code 记 Stripe 的错误码，支付不变。"""
    world = events_world
    wire(monkeypatch, world)
    payment = world.payment(world.customer())
    body = payment_event(world, payment)  # 假 Stripe 没有这个会话 → 404 resource_missing
    before = world.snapshot(payment)

    assert task.process_due(now=NOW) == 1

    row = event_row(world, body)
    assert (row.processing_status, row.last_error_code) == (REJECTED, "resource_missing")
    assert (row.processed_at, row.payment_id) == (NOW, world.row(payment).id)
    assert world.snapshot(payment) == before


def test_an_unknown_gateway_state_is_retried(
    events_world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = events_world
    wire(monkeypatch, world)
    payment = world.payment(world.customer())
    world.serve(payment, GatewayState.UNKNOWN)
    body = payment_event(world, payment)

    assert task.process_due(now=NOW, rng=FixedRandom(0.5)) == 1

    row = event_row(world, body)
    assert (row.processing_status, row.last_error_code) == (RETRY, task.GATEWAY_STATE_UNKNOWN)
    assert row.processed_at is None
    assert row.next_attempt_at == NOW + SECOND


def test_a_confirmation_conflict_is_retried(
    events_world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = events_world
    wire(monkeypatch, world)
    payment = world.payment(world.customer())
    body = payment_event(world, payment)

    def conflicting(*args: object, **options: object) -> None:
        raise ConfirmConflict("CONFIRM_CONFLICT")

    monkeypatch.setattr(task, "confirm_payment", conflicting)

    assert task.process_due(now=NOW, rng=FixedRandom(1.0)) == 1

    row = event_row(world, body)
    assert (row.processing_status, row.last_error_code) == (RETRY, task.CONFIRM_CONFLICT)
    assert row.next_attempt_at == NOW + dt.timedelta(seconds=2)


# --- 认回会话（设计 §7 四组） ---------------------------------------------------------------


def test_a_completed_event_recovers_the_session_id_and_credits(
    events_world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """第一组：支付无网关 id，事件带会话 id → 认回、PAID 入账，事件 PROCESSED。"""
    world = events_world
    wire(monkeypatch, world)
    payment = world.payment(world.customer(), with_session=False)
    recovered = dataclasses.replace(payment, session_id=f"cs_test_{uuid.uuid4().hex}")
    world.serve(recovered, GatewayState.PAID)
    body = payment_event(world, recovered)

    assert task.process_due(now=NOW) == 1

    row = world.row(payment)
    assert row.gateway_payment_id == recovered.session_id
    assert (row.status, row.paid_at) == (PaymentStatus.PAID, NOW)
    assert len(world.topups(payment)) == 1
    event = event_row(world, body)
    assert (event.processing_status, event.last_error_code) == (PROCESSED, None)
    assert event.payment_id == row.id


def test_an_expired_event_recovers_the_session_id_and_expires(
    events_world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """第二组：checkout.session.expired → 认回、支付 EXPIRED，事件 PROCESSED。"""
    world = events_world
    wire(monkeypatch, world)
    payment = world.payment(world.customer(), with_session=False)
    recovered = dataclasses.replace(payment, session_id=f"cs_test_{uuid.uuid4().hex}")
    world.serve(recovered, GatewayState.EXPIRED)
    body = payment_event(world, recovered, EXPIRED_EVENT)

    assert task.process_due(now=NOW) == 1

    row = world.row(payment)
    assert row.gateway_payment_id == recovered.session_id
    assert row.status is PaymentStatus.EXPIRED
    assert world.topups() == []
    assert event_row(world, body).processing_status is PROCESSED


@pytest.mark.parametrize(
    ("now", "status"),
    [(AFTER_EXPIRY, REJECTED), (EXPIRES, REJECTED), (NOW, RETRY)],
    ids=["expired", "at-expiry", "not-yet-expired"],
)
def test_without_any_session_id_the_event_waits_until_the_payment_expires(
    events_world: World, monkeypatch: pytest.MonkeyPatch, now: dt.datetime, status: Any
) -> None:
    """第三组：事件不带会话 id、支付也没有 → 已过期 REJECTED，否则 RETRY。

    原因都是 NO_GATEWAY_REFERENCE。"""
    world = events_world
    wire(monkeypatch, world)
    payment = world.payment(world.customer(), with_session=False)
    body = payment_event(world, payment)
    before = world.snapshot(payment)

    assert task.process_due(now=now, rng=FixedRandom(1.0)) == 1

    row = event_row(world, body)
    assert (row.processing_status, row.last_error_code) == (status, task.NO_GATEWAY_REFERENCE)
    assert row.processed_at == (now if status is REJECTED else None)
    assert row.payment_id == world.row(payment).id
    assert world.snapshot(payment) == before
    assert world.stripe.asked == []


def test_a_session_id_owned_by_another_payment_is_rejected(
    events_world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """第四组：会话 id 已属于另一笔支付 → REJECTED（GATEWAY_REFERENCE_TAKEN）。

    支付不变、不查 Stripe。"""
    world = events_world
    wire(monkeypatch, world)
    owner = world.payment(world.customer())
    payment = world.payment(world.customer(), with_session=False)
    claimed = dataclasses.replace(payment, session_id=owner.session_id)
    before = world.snapshot(payment)
    body = payment_event(world, claimed)

    assert task.process_due(now=NOW) == 1

    row = event_row(world, body)
    assert (row.processing_status, row.last_error_code) == (REJECTED, task.GATEWAY_REFERENCE_TAKEN)
    assert (row.processed_at, row.payment_id) == (NOW, world.row(payment).id)
    assert world.snapshot(payment) == before
    assert world.row(payment).gateway_payment_id is None
    assert world.stripe.asked == []


# --- 租约过期重领（INV-3、INV-11） -------------------------------------------------------------


def test_a_reclaimed_event_does_not_credit_twice_and_the_stale_result_is_dropped(
    events_world: World, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """确认已提交、落事件结果之前 worker 挂了：租约过期后被重领，再确认是空操作。

    旧租约的结果被丢弃。"""
    world = events_world
    wire(monkeypatch, world)
    payment = world.payment(world.customer())
    world.serve(payment, GatewayState.PAID)
    body = payment_event(world, payment)
    gateway = world.stripe.gateway()

    stale = task.claim_one(world.factory, now=NOW)
    assert stale is not None
    stale_outcome = task.decide(world.factory, gateway, stale, now=NOW)
    assert stale_outcome.status is PROCESSED
    assert len(world.topups(payment)) == 1

    assert task.process_due(now=LATER) == 1

    row = event_row(world, body)
    assert (row.processing_status, row.processed_at, row.attempts) == (PROCESSED, LATER, 2)
    assert len(world.topups(payment)) == 1
    assert world.stripe.asked == [payment.session_id, payment.session_id]
    settled = columns(row)
    caplog.clear()

    with caplog.at_level(logging.INFO, logger=TASK_LOGGER):
        assert task.settle(world.factory, stale, stale_outcome, now=NOW) is False

    assert columns(event_row(world, body)) == settled
    warnings = [r for r in caplog.records if r.name == TASK_LOGGER and r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "lease" in warnings[0].getMessage().lower()
    for record_ in caplog.records:
        assert SECRET_KEY not in record_.getMessage()
        assert WEBHOOK_SECRET not in record_.getMessage()


# --- 开关、上限、预算、不经 Celery ----------------------------------------------------------


@pytest.mark.parametrize(
    ("enabled", "configured"), [(False, True), (True, False)], ids=["switched-off", "no-gateway"]
)
def test_nothing_is_claimed_when_payments_are_off_or_unconfigured(
    events_world: World, monkeypatch: pytest.MonkeyPatch, enabled: bool, configured: bool
) -> None:
    world = events_world
    wire(monkeypatch, world, enabled=enabled, configured=configured)
    payment = world.payment(world.customer())
    world.serve(payment, GatewayState.PAID)
    body = payment_event(world, payment)
    before = columns(event_row(world, body))

    assert task.process_due(now=NOW) == 0

    assert columns(event_row(world, body)) == before
    assert world.stripe.asked == []


def test_one_run_handles_at_most_fifty_events(
    events_world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = events_world
    wire(monkeypatch, world)
    for _ in range(task.BATCH_LIMIT + 1):
        record(world, event_body(reference=None))

    assert task.process_due(now=NOW) == task.BATCH_LIMIT

    with world.factory() as session:
        statuses = dict(
            session.execute(
                select(PaymentGatewayEvent.processing_status, func.count()).group_by(
                    PaymentGatewayEvent.processing_status
                )
            ).all()
        )
    assert statuses == {UNMATCHED: task.BATCH_LIMIT, RECEIVED: 1}
    assert task.process_due(now=NOW) == 1


def test_a_spent_budget_stops_before_claiming_the_next_event(
    events_world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """预算用 time.monotonic 计、与注入的 now 无关；用完就停，不留下已领未处理的事件。"""
    world = events_world
    wire(monkeypatch, world)
    for _ in range(3):
        record(world, event_body(reference=None))
    clock = {"now": 1000.0}
    monkeypatch.setattr(task, "time", types.SimpleNamespace(monotonic=lambda: clock["now"]))
    real_decide = task.decide

    def slow_decide(*args: Any, **options: Any) -> task.Outcome:
        clock["now"] += task.RUN_BUDGET_SECONDS
        return real_decide(*args, **options)

    monkeypatch.setattr(task, "decide", slow_decide)

    assert task.process_due(now=NOW) == 1

    with world.factory() as session:
        statuses = sorted(
            session.execute(select(PaymentGatewayEvent.processing_status)).scalars().all()
        )
    assert statuses == sorted([UNMATCHED, RECEIVED, RECEIVED])


def test_the_scan_runs_without_celery_on_the_current_clock(
    events_world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """INV-14：Redis / Celery 丢了，事件仍在库里。

    直接调用扫描函数（now 为空，读当前 UTC）即处理。"""
    world = events_world
    wire(monkeypatch, world)
    payment = world.payment(world.customer())
    world.serve(payment, GatewayState.PAID)
    body = payment_event(world, payment)

    assert task.process_due() == 1

    row = event_row(world, body)
    assert row.processing_status is PROCESSED
    assert row.processed_at is not None and row.processed_at > CREATED
    assert row.processed_at.microsecond == 0
    assert world.row(payment).status is PaymentStatus.PAID
    assert len(world.topups(payment)) == 1


# --- MySQL 命名锁 ----------------------------------------------------------------------------


def test_a_second_run_returns_while_the_named_lock_is_held(
    mysql_events_world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """同一时刻只跑一份：另一个连接持有 `payment_events:<库名>` 时这一轮不领取任何事件。"""
    world = mysql_events_world
    wire(monkeypatch, world)
    payment = world.payment(world.customer())
    world.serve(payment, GatewayState.PAID)
    body = payment_event(world, payment)
    engine = world.factory.kw["bind"]
    assert isinstance(engine, Engine)

    with engine.connect() as holder:
        assert holder.scalar(text("SELECT GET_LOCK(CONCAT('payment_events:', DATABASE()), 0)")) == 1
        try:
            assert task.process_due(now=NOW) == 0
            assert event_row(world, body).processing_status is RECEIVED
        finally:
            holder.execute(text("SELECT RELEASE_LOCK(CONCAT('payment_events:', DATABASE()))"))

    assert task.process_due(now=NOW) == 1
    assert event_row(world, body).processing_status is PROCESSED
    # 一轮结束后锁已释放：再来一轮能取到锁（没有事件可领）。
    assert task.process_due(now=NOW) == 0
    with engine.connect() as probe:
        assert probe.scalar(text("SELECT IS_FREE_LOCK(CONCAT('payment_events:', DATABASE()))")) == 1


# --- Beat 登记 -------------------------------------------------------------------------------


@pytest.mark.parametrize(("every", "expires"), [(15, 14), (5, 4), (300, 299)])
def test_beat_runs_the_scan_every_poll_interval(every: int, expires: int) -> None:
    """条目名 payment-events-process-due。

    间隔取 payment_event_poll_seconds，expires 为间隔减 1 秒。"""
    app = create_celery_app(Settings(redis_url="memory://", payment_event_poll_seconds=every))

    entry = app.conf.beat_schedule["payment-events-process-due"]

    assert entry == {
        "task": "app.tasks.payment_events.process_due",
        "schedule": every,
        "options": {"expires": expires},
    }
    assert "app.tasks.payment_events" in app.conf.include
    assert "app.tasks.payment_events.process_due" in app.tasks
    assert Settings().payment_event_poll_seconds == 15

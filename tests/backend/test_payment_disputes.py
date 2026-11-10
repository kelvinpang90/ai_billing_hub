"""Disputes and refunds: alert only, never touch the books (design gate #585 v3, AIH-TASK-181).

设计 §2「争议与退款」、§3 INV-4 / INV-11、§7「争议与退款事件」一行：

- **落库**：`charge.dispute.created`、`charge.refunded` 落为 `RECEIVED`；
  `livemode` 不一致仍 `IGNORED`；`metadata_json` 是白名单（公共字段 + Charge / Dispute
  的字段），不含个人数据；
- **处理**：按 `metadata_json.payment_intent` 找 `payments.gateway_payment_intent_id`：
  找到 → 写 `payment_id`、`PROCESSED`、`last_error_code` 清空；没有 PI 或找不到 →
  `UNMATCHED`（`PAYMENT_NOT_FOUND`）；不查 Stripe、不调 `confirm_payment`；支付、账本、
  审计、outbox 与钱包都不变；同一事件重投不产生第二次效果。

支付与租户用 payment_world.py 的世界（SQLite 与真 MySQL 各跑一次）。Stripe 一律是注入了假传输的
`StripeGateway`，不联网；地址是虚构的。

MySQL 那部分需要 `BILLING_TEST_DATABASE_URL`，没设时 skip ——
**不要把 skipped 读成 passed**，CI 设了它并把 skipped 判成失败。
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from payment_world import (
    NOW,
    PaymentRef,
    World,
    build_mysql_world,
    build_sqlite_world,
    close_by_review,
    wire_event_task,
)
from sqlalchemy import select, update

from app.models.payments import (
    PaymentEventProcessingStatus,
    PaymentGatewayEvent,
    PaymentResolution,
    PaymentStatus,
)
from app.services.payment_events import (
    ALERT_ONLY_EVENT_TYPES,
    LIVEMODE_MISMATCH,
    RecordOutcome,
    record_event,
)
from app.tasks import payment_events as task

RECEIVED = PaymentEventProcessingStatus.RECEIVED
RETRY = PaymentEventProcessingStatus.RETRY
PROCESSED = PaymentEventProcessingStatus.PROCESSED
IGNORED = PaymentEventProcessingStatus.IGNORED
UNMATCHED = PaymentEventProcessingStatus.UNMATCHED

REFUNDED = "charge.refunded"
DISPUTED = "charge.dispute.created"
CHARGE_TYPES = [DISPUTED, REFUNDED]

PAYER_EMAIL = "payer@example.com"
PAYER_NAME = "Fictional Payer"
PAYER_ADDRESS = "1 Fictional Street"


# --- 事件 -------------------------------------------------------------------------------


def charge_object(
    event_type: str, intent: object, *, charge_id: str | None = None
) -> dict[str, Any]:
    """The Charge (refund) or the Dispute, with personal data that must not be stored."""
    charge_id = charge_id or f"ch_test_{uuid.uuid4().hex}"
    if event_type == REFUNDED:
        return {
            "id": charge_id,
            "object": "charge",
            "amount": 10000,
            "amount_refunded": 2500,
            "currency": "myr",
            "payment_intent": intent,
            "status": "succeeded",
            "refunded": False,
            "billing_details": {
                "email": PAYER_EMAIL,
                "name": PAYER_NAME,
                "address": {"line1": PAYER_ADDRESS},
            },
            "receipt_email": PAYER_EMAIL,
            "metadata": {"note": "private"},
        }
    return {
        "id": f"dp_test_{uuid.uuid4().hex}",
        "object": "dispute",
        "amount": 10000,
        "currency": "myr",
        "status": "needs_response",
        "reason": "fraudulent",
        "charge": charge_id,
        "payment_intent": intent,
        "evidence": {"customer_email_address": PAYER_EMAIL, "customer_name": PAYER_NAME},
        "metadata": {"note": "private"},
    }


def charge_event_body(
    event_type: str, intent: object, *, livemode: bool = False, without_intent: bool = False
) -> bytes:
    obj = charge_object(event_type, intent)
    if without_intent:
        del obj["payment_intent"]
    event = {
        "id": f"evt_test_{uuid.uuid4().hex}",
        "object": "event",
        "type": event_type,
        "livemode": livemode,
        "created": 1762588800,
        "data": {"object": obj},
    }
    return json.dumps(event, sort_keys=True).encode("utf-8")


def record(
    world: World, body: bytes, *, now: dt.datetime = NOW, livemode: bool = False
) -> RecordOutcome:
    parsed = world.stripe.gateway().parse_webhook(body)
    return record_event(world.factory, parsed, body, now=now, livemode=livemode)


def event_row(world: World, body: bytes) -> PaymentGatewayEvent:
    gateway_event_id = str(json.loads(body)["id"])
    with world.factory() as session:
        row = session.execute(
            select(PaymentGatewayEvent).where(
                PaymentGatewayEvent.gateway_event_id == gateway_event_id
            )
        ).scalar_one()
        session.expunge(row)
        return row


def paid_payment(world: World) -> tuple[PaymentRef, str]:
    """A credited payment whose PaymentIntent is known: what a dispute or a refund points at."""
    intent = f"pi_test_{uuid.uuid4().hex}"
    return world.payment(world.customer(), status=PaymentStatus.PAID, intent_id=intent), intent


def everything(world: World, payment: PaymentRef) -> tuple[dict[str, object], object]:
    """The payment, the ledger, audit and outbox counts, and the wallet, as committed."""
    return world.snapshot(payment), world.wallet(payment.tenant)


def refuse_stripe_and_confirmation(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: object, **options: object) -> None:
        raise AssertionError("disputes and refunds never confirm a payment")

    monkeypatch.setattr(task, "confirm_payment", forbidden)


# --- 夹具 -------------------------------------------------------------------------------


@pytest.fixture(params=["sqlite", "mysql"])
def dispute_world(request, monkeypatch: pytest.MonkeyPatch) -> Iterator[World]:
    if request.param == "mysql":
        yield from build_mysql_world(monkeypatch)
        return
    yield from build_sqlite_world()


# --- 落库 -------------------------------------------------------------------------------


def test_the_two_types_are_the_alert_only_types() -> None:
    assert ALERT_ONLY_EVENT_TYPES == frozenset(CHARGE_TYPES)


@pytest.mark.parametrize("event_type", CHARGE_TYPES)
def test_disputes_and_refunds_are_recorded_as_received_with_the_whitelist(
    dispute_world: World, event_type: str
) -> None:
    world = dispute_world
    intent = f"pi_test_{uuid.uuid4().hex}"
    body = charge_event_body(event_type, intent)

    assert record(world, body) is RecordOutcome.RECORDED

    row = event_row(world, body)
    assert row.event_type == event_type
    assert row.processing_status is RECEIVED
    assert (row.last_error_code, row.processed_at, row.payment_id) == (None, None, None)
    assert (row.received_at, row.next_attempt_at, row.attempts) == (NOW, NOW, 0)
    obj = json.loads(body)["data"]["object"]
    expected: dict[str, object] = {
        "type": event_type,
        "livemode": False,
        "created": 1762588800,
        "object_id": obj["id"],
        "payment_intent": intent,
        "amount": 10000,
        "currency": "myr",
    }
    if event_type == REFUNDED:
        expected["amount_refunded"] = 2500
    else:
        expected.update(status="needs_response", charge=obj["charge"])
    assert row.metadata_json == expected
    # 白名单：不存原始请求体，不存付款人的邮箱、姓名、地址，也不存 Stripe 对象自己的 metadata。
    stored = json.dumps(row.metadata_json)
    for personal in (PAYER_EMAIL, PAYER_NAME, PAYER_ADDRESS, "private", "fraudulent"):
        assert personal not in stored
    for key in ("billing_details", "receipt_email", "evidence", "metadata", "reason"):
        assert key not in row.metadata_json


@pytest.mark.parametrize("event_type", CHARGE_TYPES)
def test_a_dispute_or_refund_from_the_other_mode_is_ignored(
    dispute_world: World, event_type: str
) -> None:
    world = dispute_world
    body = charge_event_body(event_type, f"pi_test_{uuid.uuid4().hex}", livemode=True)

    assert record(world, body, livemode=False) is RecordOutcome.RECORDED

    row = event_row(world, body)
    assert row.processing_status is IGNORED
    assert (row.last_error_code, row.processed_at) == (LIVEMODE_MISMATCH, NOW)


# --- 处理 -------------------------------------------------------------------------------


@pytest.mark.parametrize("event_type", CHARGE_TYPES)
def test_an_event_on_a_known_payment_intent_is_linked_and_changes_nothing(
    dispute_world: World, monkeypatch: pytest.MonkeyPatch, event_type: str
) -> None:
    """INV-4、INV-11：关联到支付、PROCESSED；不查 Stripe、不确认；支付、账本、审计与钱包都不变。"""
    world = dispute_world
    wire_event_task(monkeypatch, world)
    refuse_stripe_and_confirmation(monkeypatch)
    payment, intent = paid_payment(world)
    world.payment(
        world.customer(), status=PaymentStatus.PAID, intent_id=f"pi_test_{uuid.uuid4().hex}"
    )
    before = everything(world, payment)
    body = charge_event_body(event_type, intent)
    record(world, body)

    assert task.process_due(now=NOW) == 1

    row = event_row(world, body)
    assert (row.processing_status, row.last_error_code) == (PROCESSED, None)
    assert (row.payment_id, row.processed_at, row.lease_until) == (world.row(payment).id, NOW, None)
    assert row.attempts == 1
    assert everything(world, payment) == before
    assert world.stripe.asked == []


def test_an_expanded_payment_intent_links_the_same_way(
    dispute_world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = dispute_world
    wire_event_task(monkeypatch, world)
    refuse_stripe_and_confirmation(monkeypatch)
    payment, intent = paid_payment(world)
    body = charge_event_body(DISPUTED, {"id": intent, "object": "payment_intent"})
    record(world, body)

    assert task.process_due(now=NOW) == 1

    row = event_row(world, body)
    assert (row.processing_status, row.payment_id) == (PROCESSED, world.row(payment).id)


@pytest.mark.parametrize("event_type", CHARGE_TYPES)
@pytest.mark.parametrize("case", ["unknown-intent", "no-intent", "null-intent"])
def test_an_event_without_a_matching_payment_is_unmatched(
    dispute_world: World, monkeypatch: pytest.MonkeyPatch, event_type: str, case: str
) -> None:
    world = dispute_world
    wire_event_task(monkeypatch, world)
    refuse_stripe_and_confirmation(monkeypatch)
    payment, _ = paid_payment(world)
    before = everything(world, payment)
    if case == "unknown-intent":
        body = charge_event_body(event_type, f"pi_test_{uuid.uuid4().hex}")
    elif case == "no-intent":
        body = charge_event_body(event_type, None, without_intent=True)
    else:
        body = charge_event_body(event_type, None)
    record(world, body)

    assert task.process_due(now=NOW) == 1

    row = event_row(world, body)
    assert (row.processing_status, row.last_error_code) == (UNMATCHED, task.PAYMENT_NOT_FOUND)
    assert (row.payment_id, row.processed_at, row.lease_until) == (None, NOW, None)
    assert everything(world, payment) == before
    assert world.stripe.asked == []


def test_linking_clears_an_earlier_error_code(
    dispute_world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = dispute_world
    wire_event_task(monkeypatch, world)
    refuse_stripe_and_confirmation(monkeypatch)
    payment, intent = paid_payment(world)
    body = charge_event_body(REFUNDED, intent)
    record(world, body)
    with world.factory() as session:
        session.execute(
            update(PaymentGatewayEvent)
            .where(PaymentGatewayEvent.gateway_event_id == json.loads(body)["id"])
            .values(processing_status=RETRY, last_error_code="GATEWAY_UNAVAILABLE", attempts=1)
        )
        session.commit()

    assert task.process_due(now=NOW) == 1

    row = event_row(world, body)
    assert (row.processing_status, row.last_error_code, row.attempts) == (PROCESSED, None, 2)
    assert row.payment_id == world.row(payment).id


@pytest.mark.parametrize("event_type", CHARGE_TYPES)
@pytest.mark.parametrize(
    "status", [PaymentStatus.REVIEW_REQUIRED, PaymentStatus.PENDING], ids=lambda s: s.value
)
def test_an_unfinished_payment_is_linked_but_never_confirmed(
    dispute_world: World,
    monkeypatch: pytest.MonkeyPatch,
    event_type: str,
    status: PaymentStatus,
) -> None:
    """争议或退款落在复核单或待付支付上：只关联，支付状态不变（复核单走人工出口）。"""
    world = dispute_world
    wire_event_task(monkeypatch, world)
    refuse_stripe_and_confirmation(monkeypatch)
    intent = f"pi_test_{uuid.uuid4().hex}"
    payment = world.payment(world.customer(), status=status, intent_id=intent)
    before = everything(world, payment)
    body = charge_event_body(event_type, intent)
    record(world, body)

    assert task.process_due(now=NOW) == 1

    row = event_row(world, body)
    assert (row.processing_status, row.payment_id) == (PROCESSED, world.row(payment).id)
    assert everything(world, payment) == before
    assert world.row(payment).status is status


@pytest.mark.parametrize("event_type", CHARGE_TYPES)
def test_a_payment_closed_without_credit_stays_closed(
    dispute_world: World, monkeypatch: pytest.MonkeyPatch, event_type: str
) -> None:
    world = dispute_world
    wire_event_task(monkeypatch, world)
    refuse_stripe_and_confirmation(monkeypatch)
    intent = f"pi_test_{uuid.uuid4().hex}"
    payment = world.payment(
        world.customer(), status=PaymentStatus.REVIEW_REQUIRED, intent_id=intent
    )
    close_by_review(world, payment)
    before = everything(world, payment)
    body = charge_event_body(event_type, intent)
    record(world, body)

    assert task.process_due(now=NOW) == 1

    row = event_row(world, body)
    assert (row.processing_status, row.payment_id) == (PROCESSED, world.row(payment).id)
    assert everything(world, payment) == before
    closed = world.row(payment)
    assert (closed.status, closed.resolution) == (PaymentStatus.FAILED, PaymentResolution.CLOSED)


@pytest.mark.parametrize("event_type", CHARGE_TYPES)
def test_a_redelivery_has_no_second_effect(
    dispute_world: World, monkeypatch: pytest.MonkeyPatch, event_type: str
) -> None:
    """INV-11：同一事件 id 只落一行，重投是 DUPLICATE，没有可再领取的事件。"""
    world = dispute_world
    wire_event_task(monkeypatch, world)
    refuse_stripe_and_confirmation(monkeypatch)
    payment, intent = paid_payment(world)
    body = charge_event_body(event_type, intent)
    record(world, body)
    assert task.process_due(now=NOW) == 1
    settled = event_row(world, body)
    before = everything(world, payment)

    assert record(world, body, now=NOW + dt.timedelta(minutes=1)) is RecordOutcome.DUPLICATE
    assert task.process_due(now=NOW + dt.timedelta(minutes=1)) == 0

    again = event_row(world, body)
    assert (again.processing_status, again.processed_at, again.attempts) == (
        settled.processing_status,
        settled.processed_at,
        settled.attempts,
    )
    assert everything(world, payment) == before

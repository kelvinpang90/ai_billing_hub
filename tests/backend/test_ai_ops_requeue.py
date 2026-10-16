"""MySQL coverage for AI usage dead-letter operational commands."""

from __future__ import annotations

import datetime as dt

import pytest
from test_ai_delivery import add_row, read

from app.ai_api.ops import REQUEUABLE_DEAD_ERRORS, dead_ack, requeue
from app.ai_api.outbox import AiUsageOutbox, AiUsageOutboxStatus


def test_requeue_error_allowlist_and_delivered(mysql_world) -> None:
    allowed, refused, delivered = (
        add_row(mysql_world, 1, status=AiUsageOutboxStatus.DEAD),
        add_row(mysql_world, 2, status=AiUsageOutboxStatus.DEAD),
        add_row(mysql_world, 3, status=AiUsageOutboxStatus.DELIVERED),
    )
    with mysql_world.factory.begin() as session:
        session.get(AiUsageOutbox, allowed).last_error = next(iter(REQUEUABLE_DEAD_ERRORS))
        session.get(AiUsageOutbox, refused).last_error = "PAYLOAD_TOO_LARGE"

    rows = [read(mysql_world, row_id) for row_id in (allowed, refused, delivered)]
    result = requeue(mysql_world.factory, [row.event_id for row in rows], reason="operator")

    assert [item.operation for item in result] == ["requeued", "failed", "failed"]
    assert read(mysql_world, allowed).status is AiUsageOutboxStatus.PENDING
    assert read(mysql_world, refused).status is AiUsageOutboxStatus.DEAD
    assert read(mysql_world, delivered).status is AiUsageOutboxStatus.DELIVERED
    assert (
        requeue(mysql_world.factory, [rows[2].event_id], reason="operator", include_delivered=True)[
            0
        ].operation
        == "requeued"
    )
    assert read(mysql_world, delivered).delivered_at is None


def test_requeue_preserves_review_columns_and_resets_delivery_columns(mysql_world) -> None:
    row_id = add_row(mysql_world, 4, status=AiUsageOutboxStatus.DEAD)
    moment = dt.datetime(2026, 10, 16, 1, 2, 3)
    with mysql_world.factory.begin() as session:
        row = session.get(AiUsageOutbox, row_id)
        assert row is not None
        row.last_error = "INVALID_BILLING_RESPONSE"
        row.claim_id = "01J00000000000000000000000"
        row.attempts = 9
        row.first_attempted_at = moment
        row.review_required_at = moment
        row.reviewed_at = moment
        row.dead_acked_at = moment

    event_id = read(mysql_world, row_id).event_id
    assert requeue(mysql_world.factory, [event_id], reason="operator")[0].operation == "requeued"
    row = read(mysql_world, row_id)
    assert (row.claim_id, row.last_error, row.attempts, row.dead_acked_at) == (None, None, 0, None)
    assert (row.first_attempted_at, row.review_required_at, row.reviewed_at) == (
        moment,
        moment,
        moment,
    )


def test_dead_ack_only_once_and_reason_is_all_or_nothing(mysql_world) -> None:
    row_id = add_row(mysql_world, 5, status=AiUsageOutboxStatus.DEAD)
    event_id = read(mysql_world, row_id).event_id
    assert (
        dead_ack(mysql_world.factory, [event_id], reason="operator")[0].operation == "acknowledged"
    )
    assert read(mysql_world, row_id).dead_acked_at is not None
    assert dead_ack(mysql_world.factory, [event_id], reason="operator")[0].operation == "failed"
    with pytest.raises(ValueError):
        dead_ack(mysql_world.factory, [event_id], reason=" " * 256)


def test_audit_failure_rolls_back_only_its_event(mysql_world, monkeypatch) -> None:
    first, second = (
        add_row(mysql_world, 6, status=AiUsageOutboxStatus.DEAD),
        add_row(mysql_world, 7, status=AiUsageOutboxStatus.DEAD),
    )
    with mysql_world.factory.begin() as session:
        for row_id in (first, second):
            row = session.get(AiUsageOutbox, row_id)
            assert row is not None
            row.last_error = "INVALID_BILLING_RESPONSE"
    import app.ai_api.ops as ops

    original = ops.record_audit
    calls = 0

    def fail_first(*args, **kwargs) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("audit unavailable")
        original(*args, **kwargs)

    monkeypatch.setattr(ops, "record_audit", fail_first)
    ids = [read(mysql_world, row_id).event_id for row_id in (first, second)]
    assert [item.operation for item in requeue(mysql_world.factory, ids, reason="operator")] == [
        "failed",
        "requeued",
    ]
    assert read(mysql_world, first).status is AiUsageOutboxStatus.DEAD
    assert read(mysql_world, second).status is AiUsageOutboxStatus.PENDING

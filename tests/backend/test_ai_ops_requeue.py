"""AI usage dead-letter commands (design gate #281 v23 §2 死信重投, the §7 rows of task 067).

MySQL only: the commands lock each row with `SELECT … FOR UPDATE` and the alert dimension
reads the same table, so they run against the real database like the delivery tests.
"""

from __future__ import annotations

import datetime as dt
import json
import re

import pytest
from sqlalchemy import func, select, update
from test_ai_delivery import add_row, read, settings, wire_delivery

from app import cli
from app.ai_api import delivery, ops
from app.ai_api.ops import ENTITY_AI_USAGE_OUTBOX, dead_ack, requeue
from app.ai_api.outbox import AiUsageOutbox, AiUsageOutboxStatus
from app.models.auth import AuditAction, AuditLog
from app.services import alerts
from app.services.alerts import AlertContext, AlertLevel

# The delivery tests' MySQL world (tenant, project, cleanup), registered here as a fixture.

MOMENT = dt.datetime(2026, 10, 16, 1, 2, 3, 456789)
NOW = dt.datetime(2026, 10, 16, 2, 0, 0, 123456)
REFUSED_ERRORS = (
    "CREDENTIAL_SCOPE_MISMATCH",
    "IDEMPOTENCY_CONFLICT",
    "VALIDATION_ERROR",
    "PAYLOAD_TOO_LARGE",
    "INVALID_LOCAL_EVENT",
    "SOMETHING_UNKNOWN",
)


def set_row(world, row_id: int, **values: object) -> None:
    with world.factory.begin() as session:
        session.execute(update(AiUsageOutbox).where(AiUsageOutbox.id == row_id).values(**values))


def dead_row(world, number: int, last_error: str = "INVALID_BILLING_RESPONSE", **values) -> int:
    row_id = add_row(world, number, status=AiUsageOutboxStatus.DEAD)
    set_row(world, row_id, **{"last_error": last_error, "attempts": 3, **values})
    return row_id


def snapshot(world, row_id: int) -> dict[str, object]:
    row = read(world, row_id)
    return {column.key: getattr(row, column.key) for column in AiUsageOutbox.__table__.columns}


def last_audit_id(world) -> int:
    with world.factory() as session:
        return session.execute(select(func.coalesce(func.max(AuditLog.id), 0))).scalar_one()


def audits_since(world, audit_id: int) -> list[AuditLog]:
    with world.factory() as session:
        return list(
            session.execute(
                select(AuditLog)
                .where(AuditLog.id > audit_id, AuditLog.entity_type == ENTITY_AI_USAGE_OUTBOX)
                .order_by(AuditLog.id)
            ).scalars()
        )


def dead_alert(world) -> tuple[AlertLevel, int]:
    with world.factory() as session:
        result = alerts.ai_usage_outbox(session, AlertContext(now=NOW, currencies=("USD",)))
    match = re.match(r"dead (\d+),", result.summary)
    assert match is not None, result.summary
    return result.level, int(match.group(1))


def die(world, monkeypatch, row_id: int) -> None:
    """Let the real delivery claim the row and kill it on a non-envelope 422."""
    wire_delivery(monkeypatch)
    set_row(world, row_id, next_attempt_at=dt.datetime(2000, 1, 1))
    delivery.deliver_due(world.factory, settings(), send=lambda *_args: (422, b"not json"))
    row = read(world, row_id)
    assert (row.status, row.last_error) == (AiUsageOutboxStatus.DEAD, "INVALID_BILLING_RESPONSE")


@pytest.mark.parametrize("error", sorted(ops.REQUEUABLE_DEAD_ERRORS))
def test_requeue_resets_delivery_columns_and_keeps_the_review_columns(mysql_world, error) -> None:
    row_id = dead_row(
        mysql_world,
        1,
        last_error=error,
        attempts=9,
        claim_id="01J0000000000000000000000C",
        first_attempted_at=MOMENT,
        review_required_at=MOMENT,
        reviewed_at=MOMENT,
        dead_acked_at=MOMENT,
        delivered_at=None,
    )
    before, audit_id = snapshot(mysql_world, row_id), last_audit_id(mysql_world)

    result = requeue(
        mysql_world.factory, [before["event_id"]], reason="  operator  ", clock=lambda: NOW
    )

    assert result == (ops.OperationResult(before["event_id"], "requeued"),)
    after = snapshot(mysql_world, row_id)
    assert {key: value for key, value in after.items() if after[key] != before[key]} == {
        "status": AiUsageOutboxStatus.PENDING,
        "next_attempt_at": NOW,
        "claim_id": None,
        "last_error": None,
        "dead_acked_at": None,
        "attempts": 0,
    }
    assert (after["first_attempted_at"], after["review_required_at"], after["reviewed_at"]) == (
        MOMENT,
        MOMENT,
        MOMENT,
    )
    assert after["payload_json"] == before["payload_json"]
    [audit] = audits_since(mysql_world, audit_id)
    assert (audit.action, audit.entity_id, audit.reason) == (
        AuditAction.AI_USAGE_REQUEUE,
        before["event_id"],
        "operator",
    )
    assert (audit.actor_user_id, audit.actor_role, audit.user_agent) == (None, None, "cli")
    assert json.loads(audit.before_state) == {
        "status": "DEAD",
        "last_error": error,
        "attempts": 9,
        "dead_acked_at": MOMENT.isoformat(),
    }
    assert json.loads(audit.after_state) == {
        "status": "PENDING",
        "last_error": None,
        "attempts": 0,
        "dead_acked_at": None,
    }


@pytest.mark.parametrize("include_delivered", [False, True])
@pytest.mark.parametrize("error", REFUSED_ERRORS)
def test_requeue_refuses_dead_rows_outside_the_allowlist(
    mysql_world, error, include_delivered
) -> None:
    row_id = dead_row(mysql_world, 2, last_error=error)
    before, audit_id = snapshot(mysql_world, row_id), last_audit_id(mysql_world)

    result = requeue(
        mysql_world.factory,
        [before["event_id"]],
        reason="operator",
        include_delivered=include_delivered,
    )

    assert result == (ops.OperationResult(before["event_id"], "failed"),)
    assert snapshot(mysql_world, row_id) == before
    assert audits_since(mysql_world, audit_id) == []


@pytest.mark.parametrize(
    "status",
    [
        AiUsageOutboxStatus.PENDING,
        AiUsageOutboxStatus.SENDING,
        AiUsageOutboxStatus.HELD,
        AiUsageOutboxStatus.DISCARDED,
        AiUsageOutboxStatus.DELIVERED,
    ],
)
def test_requeue_refuses_rows_that_are_not_dead(mysql_world, status) -> None:
    row_id = add_row(mysql_world, 3, status=status)
    set_row(mysql_world, row_id, last_error="INVALID_BILLING_RESPONSE")
    before, audit_id = snapshot(mysql_world, row_id), last_audit_id(mysql_world)

    result = requeue(
        mysql_world.factory, [before["event_id"], "01J00000000000000000000000"], reason="r"
    )

    assert [item.operation for item in result] == ["failed", "failed"]
    assert snapshot(mysql_world, row_id) == before
    assert audits_since(mysql_world, audit_id) == []


def test_include_delivered_requeues_a_delivered_row_and_clears_delivered_at(mysql_world) -> None:
    row_id = add_row(mysql_world, 4, status=AiUsageOutboxStatus.DELIVERED)
    set_row(mysql_world, row_id, attempts=2, first_attempted_at=MOMENT, delivered_at=MOMENT)
    before, audit_id = snapshot(mysql_world, row_id), last_audit_id(mysql_world)

    result = requeue(
        mysql_world.factory,
        [before["event_id"]],
        reason="billing lost it",
        include_delivered=True,
        clock=lambda: NOW,
    )

    assert result[0].operation == "requeued"
    after = snapshot(mysql_world, row_id)
    assert {key: value for key, value in after.items() if after[key] != before[key]} == {
        "status": AiUsageOutboxStatus.PENDING,
        "next_attempt_at": NOW,
        "attempts": 0,
        "delivered_at": None,
    }
    [audit] = audits_since(mysql_world, audit_id)
    assert json.loads(audit.before_state) == {
        "status": "DELIVERED",
        "last_error": None,
        "attempts": 2,
        "dead_acked_at": None,
        "delivered_at": MOMENT.isoformat(),
    }
    assert json.loads(audit.after_state) == {
        "status": "PENDING",
        "last_error": None,
        "attempts": 0,
        "dead_acked_at": None,
        "delivered_at": None,
    }


def test_dead_ack_writes_once_and_only_on_dead_rows(mysql_world) -> None:
    dead = dead_row(mysql_world, 5, last_error="PAYLOAD_TOO_LARGE")
    pending = add_row(mysql_world, 6)
    dead_id, pending_id = read(mysql_world, dead).event_id, read(mysql_world, pending).event_id
    pending_before, audit_id = snapshot(mysql_world, pending), last_audit_id(mysql_world)

    first = dead_ack(
        mysql_world.factory, [dead_id, pending_id], reason=" handled ", clock=lambda: NOW
    )
    acked = snapshot(mysql_world, dead)
    second = dead_ack(mysql_world.factory, [dead_id], reason="again", clock=lambda: MOMENT)

    assert [item.operation for item in first] == ["acknowledged", "failed"]
    assert second[0].operation == "failed"
    assert (acked["status"], acked["dead_acked_at"]) == (AiUsageOutboxStatus.DEAD, NOW)
    assert snapshot(mysql_world, dead) == acked
    assert snapshot(mysql_world, pending) == pending_before
    [audit] = audits_since(mysql_world, audit_id)
    assert (audit.action, audit.entity_id, audit.reason) == (
        AuditAction.AI_USAGE_DEAD_ACK,
        dead_id,
        "handled",
    )
    assert json.loads(audit.before_state) == {"status": "DEAD", "dead_acked_at": None}
    assert json.loads(audit.after_state) == {"status": "DEAD", "dead_acked_at": NOW.isoformat()}


def test_dead_letter_alert_clears_on_ack_and_returns_when_a_requeued_row_dies_again(
    mysql_world, monkeypatch
) -> None:
    """§7「死信告警可复核」."""
    _level, baseline = dead_alert(mysql_world)
    row_id = add_row(mysql_world, 7)
    reviewed = add_row(mysql_world, 8)
    set_row(mysql_world, reviewed, review_required_at=MOMENT, reviewed_at=MOMENT)
    event_id, reviewed_id = read(mysql_world, row_id).event_id, read(mysql_world, reviewed).event_id

    die(mysql_world, monkeypatch, row_id)
    die(mysql_world, monkeypatch, reviewed)
    assert dead_alert(mysql_world) == (AlertLevel.P1, baseline + 2)

    assert [item.operation for item in dead_ack(mysql_world.factory, [event_id], reason="r")] == [
        "acknowledged"
    ]
    assert dead_alert(mysql_world)[1] == baseline + 1

    result = requeue(mysql_world.factory, [event_id, reviewed_id], reason="credential fixed")
    assert [item.operation for item in result] == ["requeued", "requeued"]
    assert dead_alert(mysql_world)[1] == baseline
    assert (
        read(mysql_world, reviewed).review_required_at,
        read(mysql_world, reviewed).reviewed_at,
    ) == (
        MOMENT,
        MOMENT,
    )

    set_row(mysql_world, reviewed, next_attempt_at=dt.datetime(2100, 1, 1))
    die(mysql_world, monkeypatch, row_id)
    assert dead_alert(mysql_world) == (AlertLevel.P1, baseline + 1)
    assert read(mysql_world, row_id).dead_acked_at is None


@pytest.mark.parametrize("command", ["ai-usage-requeue", "ai-usage-dead-ack"])
def test_cli_prints_each_event_and_exits_one_when_any_fails(mysql_world, capsys, command) -> None:
    good = dead_row(mysql_world, 9)
    bad = add_row(mysql_world, 10)
    good_id, bad_id = read(mysql_world, good).event_id, read(mysql_world, bad).event_id
    done = "requeued" if command == "ai-usage-requeue" else "acknowledged"

    assert cli.main([command, good_id, bad_id, "--reason", "operator"]) == 1
    assert capsys.readouterr().out.splitlines() == [
        f"{good_id} {done}",
        f"{bad_id} failed",
        "Changed 1 / 2",
    ]

    other = dead_row(mysql_world, 11)
    other_id = read(mysql_world, other).event_id
    assert cli.main([command, other_id, "--reason", " " + "x" * 255 + " "]) == 0
    assert capsys.readouterr().out.splitlines() == [f"{other_id} {done}", "Changed 1 / 1"]


@pytest.mark.parametrize("command", ["ai-usage-requeue", "ai-usage-dead-ack"])
@pytest.mark.parametrize("reason", ["", "   ", "x" * 256, " " + "x" * 256])
def test_cli_rejects_a_bad_reason_before_touching_any_row(
    mysql_world, capsys, command, reason
) -> None:
    rows = [dead_row(mysql_world, 12), dead_row(mysql_world, 13)]
    before, audit_id = [snapshot(mysql_world, row) for row in rows], last_audit_id(mysql_world)
    event_ids = [entry["event_id"] for entry in before]

    assert cli.main([command, *event_ids, "--reason", reason]) == 2

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "--reason" in captured.err
    assert [snapshot(mysql_world, row) for row in rows] == before
    assert audits_since(mysql_world, audit_id) == []
    function = requeue if command == "ai-usage-requeue" else dead_ack
    with pytest.raises(ValueError):
        function(mysql_world.factory, event_ids, reason=reason)


@pytest.mark.parametrize("command", ["ai-usage-requeue", "ai-usage-dead-ack"])
def test_an_audit_failure_rolls_back_only_that_event(
    mysql_world, monkeypatch, capsys, command
) -> None:
    """§7「事务中途失败」: the row stays as it was, no audit, and the next event still runs."""
    first, second = dead_row(mysql_world, 14), dead_row(mysql_world, 15)
    before, audit_id = snapshot(mysql_world, first), last_audit_id(mysql_world)
    original, calls = ops.record_audit, []

    def fail_first(*args, **kwargs) -> None:
        calls.append(kwargs["entity_id"])
        if len(calls) == 1:
            raise RuntimeError("audit unavailable")
        original(*args, **kwargs)

    monkeypatch.setattr(ops, "record_audit", fail_first)
    second_id = read(mysql_world, second).event_id
    done = "requeued" if command == "ai-usage-requeue" else "acknowledged"

    assert cli.main([command, before["event_id"], second_id, "--reason", "operator"]) == 1

    assert capsys.readouterr().out.splitlines() == [
        f"{before['event_id']} failed",
        f"{second_id} {done}",
        "Changed 1 / 2",
    ]
    assert snapshot(mysql_world, first) == before
    assert [audit.entity_id for audit in audits_since(mysql_world, audit_id)] == [second_id]
    changed = read(mysql_world, second)
    if command == "ai-usage-requeue":
        assert changed.status is AiUsageOutboxStatus.PENDING
    else:
        assert changed.dead_acked_at is not None

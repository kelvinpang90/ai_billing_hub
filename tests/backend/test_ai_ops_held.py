"""Held AI-usage operations (design gate #281 v23 §2 and §7)."""

from __future__ import annotations

import datetime as dt
import json

import test_ai_delivery
from sqlalchemy import select
from test_ai_delivery import add_row, read

from app.ai_api import ops
from app.ai_api.ops import held_discard, held_import, held_release, review_ack
from app.ai_api.outbox import AiUsageOutbox, AiUsageOutboxStatus
from app.models.auth import AuditAction, AuditLog

mysql_world = test_ai_delivery.mysql_world
NOW = dt.datetime(2026, 10, 16, 2, 0, 0, 123456)


def audits(world) -> list[AuditLog]:
    with world.factory() as session:
        return list(session.scalars(select(AuditLog).order_by(AuditLog.id)))


def log_line(world, event_id: str, /, **overrides: object) -> str:
    with world.factory() as session:
        row = session.scalar(select(AiUsageOutbox).where(AiUsageOutbox.event_id == event_id))
        tenant = row.tenant_id
        project = row.project_id
    with world.factory() as session:
        from app.models.tenancy import Project, Tenant

        project_row = session.get(Project, project)
        tenant_row = session.get(Tenant, tenant)
    fields: dict[str, object] = {
        "message": "AI usage not persisted",
        "event_id": event_id,
        "ai_request_id": "01J0000000000000000000000A",
        "feature": "crm.contact_summary",
        "tenant_public_id": tenant_row.public_id,
        "project_public_id": project_row.public_id,
        "model": "claude-sonnet-5-5",
        "usage_input": 1,
        "usage_output": 2,
        "usage_cache_write": 0,
        "usage_cache_read": 0,
        "occurred_at": "2026-10-16T02:00:00.000000Z",
        "commit_outcome": "unknown",
    }
    fields.update(overrides)
    return json.dumps(fields)


def test_release_and_discard_only_resolve_held_rows(mysql_world) -> None:
    held = add_row(mysql_world, 1, status=AiUsageOutboxStatus.HELD)
    other = add_row(mysql_world, 2)
    discarded = add_row(mysql_world, 7, status=AiUsageOutboxStatus.HELD)
    held_id, other_id, discarded_id = (
        read(mysql_world, held).event_id,
        read(mysql_world, other).event_id,
        read(mysql_world, discarded).event_id,
    )

    assert (
        held_release(mysql_world.factory, [held_id], reason="release", clock=lambda: NOW)[
            0
        ].operation
        == "released"
    )
    assert read(mysql_world, held).status is AiUsageOutboxStatus.PENDING
    assert read(mysql_world, held).resolved_at == NOW
    assert (
        held_discard(mysql_world.factory, [other_id], reason="discard", clock=lambda: NOW)[
            0
        ].operation
        == "failed"
    )
    assert (
        held_discard(mysql_world.factory, [held_id], reason="discard", clock=lambda: NOW)[
            0
        ].operation
        == "failed"
    )
    assert (
        held_discard(mysql_world.factory, [discarded_id], reason="discard", clock=lambda: NOW)[
            0
        ].operation
        == "discarded"
    )
    assert read(mysql_world, discarded).status is AiUsageOutboxStatus.DISCARDED
    assert read(mysql_world, discarded).resolved_at == NOW
    assert [audit.action for audit in audits(mysql_world)[-2:]] == [
        AuditAction.AI_USAGE_HELD_RELEASE,
        AuditAction.AI_USAGE_HELD_DISCARD,
    ]


def test_import_creates_held_and_applies_collision_rules(mysql_world) -> None:
    new_id = "01J0000000000000000000000B"
    existing = add_row(mysql_world, 3)
    existing_id = read(mysql_world, existing).event_id
    created_line = json.loads(log_line(mysql_world, existing_id))
    created_line["event_id"] = new_id
    result = held_import(
        mysql_world.factory,
        [json.dumps(created_line), log_line(mysql_world, existing_id)],
        reason="recover logs",
        clock=lambda: NOW,
    )
    assert [item.operation for item in result] == ["created", "held"]
    with mysql_world.factory() as session:
        created = session.scalar(select(AiUsageOutbox).where(AiUsageOutbox.event_id == new_id))
    assert created is not None and created.status is AiUsageOutboxStatus.HELD
    assert read(mysql_world, existing).status is AiUsageOutboxStatus.HELD
    assert [audit.action for audit in audits(mysql_world)[-2:]] == [
        AuditAction.AI_USAGE_HELD_IMPORT,
        AuditAction.AI_USAGE_HELD_IMPORT,
    ]


def test_import_rejects_each_invalid_field_without_audit(mysql_world) -> None:
    row = add_row(mysql_world, 4)
    event_id = read(mysql_world, row).event_id
    bad_lines = [
        log_line(mysql_world, event_id, **{key: value})
        for key, value in {
            "message": "wrong",
            "event_id": "wrong",
            "ai_request_id": "wrong",
            "feature": "wrong",
            "model": " bad",
            "tenant_public_id": "wrong",
            "usage_input": True,
            "usage_output": -1,
            "usage_cache_write": 10**12 + 1,
            "usage_cache_read": "1",
            "occurred_at": "2026-10-16",
            "commit_outcome": "written",
        }.items()
    ]
    before = len(audits(mysql_world))
    result = held_import(mysql_world.factory, bad_lines, reason="recover", clock=lambda: NOW)
    assert all(item.operation.startswith("rejected") for item in result)
    assert len(audits(mysql_world)) == before


def test_import_identity_mismatch_is_rejected_and_review_ack_is_once(mysql_world) -> None:
    row = add_row(mysql_world, 5)
    event_id = read(mysql_world, row).event_id
    result = held_import(
        mysql_world.factory,
        [log_line(mysql_world, event_id, tenant_public_id="00000000-0000-4000-8000-000000000000")],
        reason="recover",
        clock=lambda: NOW,
    )
    assert result[0].operation.startswith("rejected")
    with mysql_world.factory.begin() as session:
        row = session.get(AiUsageOutbox, row)
        row.review_required_at = NOW
    assert (
        review_ack(mysql_world.factory, [event_id], reason="reviewed", clock=lambda: NOW)[
            0
        ].operation
        == "acknowledged"
    )
    assert (
        review_ack(mysql_world.factory, [event_id], reason="again", clock=lambda: NOW)[0].operation
        == "failed"
    )


def test_audit_failure_rolls_back_held_release(mysql_world, monkeypatch) -> None:
    row = add_row(mysql_world, 6, status=AiUsageOutboxStatus.HELD)
    event_id = read(mysql_world, row).event_id
    monkeypatch.setattr(
        ops, "record_audit", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError())
    )
    assert (
        held_release(mysql_world.factory, [event_id], reason="release", clock=lambda: NOW)[
            0
        ].operation
        == "failed"
    )
    assert read(mysql_world, row).status is AiUsageOutboxStatus.HELD

from __future__ import annotations

import logging

from app.ai_api.held import HeldBuffer, HeldUsage


def _held(event_id: str) -> HeldUsage:
    return HeldUsage(
        payload={
            "event_id": event_id,
            "request_id": "01J00000000000000000000000",
            "feature": "crm.contact_summary",
            "tenant_id": "tenant-public-id",
            "project_id": "project-public-id",
            "model": "claude-sonnet-4-6",
            "input_tokens": 1,
            "output_tokens": 2,
            "cache_creation_input_tokens": 3,
            "cache_read_input_tokens": 4,
            "occurred_at": "2026-10-14T00:00:00.000000Z",
        },
        internal_tenant_id=1,
        internal_project_id=2,
        ai_request_id="01J00000000000000000000000",
        commit_outcome="unknown",
    )


def test_held_buffer_discards_oldest_and_records_its_metadata(caplog) -> None:
    buffer = HeldBuffer(1)
    first = _held("01J00000000000000000000001")
    second = _held("01J00000000000000000000002")
    with caplog.at_level(logging.CRITICAL):
        buffer.put(first)
        buffer.put(second)
    assert buffer.take() == second
    record = next(
        record for record in caplog.records if record.message == "AI usage held buffer overflow"
    )
    assert record.event_id == first.payload["event_id"]
    assert record.usage_input == 1
    assert record.usage_output == 2
    assert record.usage_cache_write == 3
    assert record.usage_cache_read == 4

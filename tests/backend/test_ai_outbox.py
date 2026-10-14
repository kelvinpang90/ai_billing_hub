"""MySQL coverage for AI usage outbox uncertainty and held-item recovery."""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import os
import threading
import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, delete, insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from alembic import command
from app.ai_api.database import AiDatabaseExecutor
from app.ai_api.held import HeldBuffer, HeldUsage, drain_held_once, persist_held_once
from app.ai_api.outbox import AiUsageOutbox, AiUsageOutboxStatus, persist_usage, write_pending
from app.core.config import get_settings
from app.core.database import create_session_factory
from app.core.logging import JsonFormatter
from app.models.tenancy import Project, Tenant

TEST_DATABASE_URL = os.environ.get("BILLING_TEST_DATABASE_URL", "")


@dataclass(frozen=True)
class World:
    factory: sessionmaker[Session]
    tenant_id: int
    project_id: int


def _held(number: int) -> HeldUsage:
    return HeldUsage(
        payload={
            "event_id": f"01J000000000000000000000{number:02d}",
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
        internal_tenant_id=0,
        internal_project_id=0,
        ai_request_id="01J00000000000000000000000",
        commit_outcome="unknown",
    )


@pytest.fixture
def mysql_world(monkeypatch) -> Iterator[World]:
    if not TEST_DATABASE_URL:
        pytest.skip("BILLING_TEST_DATABASE_URL is not set; these cases need real MySQL")
    monkeypatch.setenv("BILLING_DATABASE_URL", TEST_DATABASE_URL)
    get_settings.cache_clear()
    command.upgrade(Config("alembic.ini"), "head")
    get_settings.cache_clear()
    engine = create_engine(TEST_DATABASE_URL, pool_size=4, max_overflow=2)
    factory = create_session_factory(engine)
    now = dt.datetime.now(dt.UTC).replace(tzinfo=None)
    with factory() as session:
        tenant = Tenant(
            public_id=str(uuid.uuid4()),
            company_name="AI outbox test",
            email="ai-outbox-test@example.com",
            created_at=now,
            updated_at=now,
        )
        session.add(tenant)
        session.flush()
        project = Project(
            public_id=str(uuid.uuid4()),
            tenant_id=tenant.id,
            name="AI outbox test",
            created_at=now,
            updated_at=now,
        )
        session.add(project)
        session.commit()
        world = World(factory, tenant.id, project.id)
    try:
        yield world
    finally:
        with engine.begin() as connection:
            connection.execute(
                delete(AiUsageOutbox).where(AiUsageOutbox.tenant_id == world.tenant_id)
            )
            connection.execute(delete(Project).where(Project.id == world.project_id))
            connection.execute(delete(Tenant).where(Tenant.id == world.tenant_id))
        engine.dispose()


def _for(world: World, number: int) -> HeldUsage:
    item = _held(number)
    return HeldUsage(
        item.payload, world.tenant_id, world.project_id, item.ai_request_id, item.commit_outcome
    )


def _insert_existing(
    world: World,
    item: HeldUsage,
    *,
    status: AiUsageOutboxStatus,
    first_attempted: bool = False,
    attempts: int | None = None,
    resolved: bool = False,
    review_required: bool = False,
) -> None:
    now = dt.datetime.now(dt.UTC).replace(tzinfo=None)
    p = item.payload
    with world.factory() as session:
        session.execute(
            insert(AiUsageOutbox).values(
                event_id=p["event_id"],
                request_id=p["request_id"],
                claim_id=None,
                tenant_id=world.tenant_id,
                project_id=world.project_id,
                feature=p["feature"],
                payload_json=json.dumps(p, separators=(",", ":")),
                status=status,
                attempts=int(first_attempted) if attempts is None else attempts,
                next_attempt_at=now,
                last_error=None,
                first_attempted_at=now if first_attempted else None,
                created_at=now,
                delivered_at=now if status is AiUsageOutboxStatus.DELIVERED else None,
                resolved_at=now if resolved else None,
                review_required_at=now if review_required else None,
                reviewed_at=None,
                dead_acked_at=None,
            )
        )
        session.commit()


def _outbox(world: World, item: HeldUsage) -> AiUsageOutbox:
    with world.factory() as session:
        return session.scalar(
            select(AiUsageOutbox).where(AiUsageOutbox.event_id == item.payload["event_id"])
        )


def test_commit_acknowledgement_error_is_checked_on_mysql(mysql_world, monkeypatch) -> None:
    item, original_factory, calls = _for(mysql_world, 1), mysql_world.factory, 0

    def factory() -> Session:
        nonlocal calls
        calls += 1
        session = original_factory()
        if calls == 1:
            commit = session.commit

            def commit_then_disconnect() -> None:
                commit()
                raise ConnectionError("acknowledgement lost")

            monkeypatch.setattr(session, "commit", commit_then_disconnect)
            monkeypatch.setattr(
                session, "rollback", lambda: (_ for _ in ()).throw(ConnectionError("rollback lost"))
            )
        return session

    assert (
        write_pending(
            factory,
            payload=item.payload,
            internal_tenant_id=item.internal_tenant_id,
            internal_project_id=item.internal_project_id,
        )
        == "written_after_error"
    )
    assert _outbox(mysql_world, item).status is AiUsageOutboxStatus.PENDING


def test_commit_check_failure_returns_unknown_on_mysql(mysql_world, monkeypatch) -> None:
    item, original_factory, calls = _for(mysql_world, 2), mysql_world.factory, 0

    def factory() -> Session:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise ConnectionError("database unavailable")
        session, commit = original_factory(), None
        commit = session.commit

        def commit_then_disconnect() -> None:
            commit()
            raise ConnectionError("acknowledgement lost")

        monkeypatch.setattr(session, "commit", commit_then_disconnect)
        monkeypatch.setattr(
            session, "rollback", lambda: (_ for _ in ()).throw(ConnectionError("rollback lost"))
        )
        return session

    assert (
        write_pending(
            factory,
            payload=item.payload,
            internal_tenant_id=item.internal_tenant_id,
            internal_project_id=item.internal_project_id,
        )
        == "unknown"
    )


REVIEW_MESSAGE = "AI usage entered billing although the request returned 503"
P, H = AiUsageOutboxStatus.PENDING, AiUsageOutboxStatus.HELD


# Design §7 "撞行规则": each existing row state, in the design's order, plus the
# other two delivery states (SENDING, DEAD) that the second UPDATE also covers.
@pytest.mark.parametrize(
    "number,status,first_attempted,attempts,resolved,review_required,expected,flags",
    [
        (10, P, False, 0, False, False, H, False),  # never claimed
        (11, P, True, 1, False, False, P, True),  # returned after a failed send
        (12, P, True, 0, False, False, P, True),  # redelivered: attempts reset to 0
        (13, P, False, 0, True, False, P, False),  # released by a person
        (14, H, False, 0, False, False, H, False),  # the same item held twice
        (15, AiUsageOutboxStatus.DISCARDED, False, 0, True, False, None, False),
        (16, AiUsageOutboxStatus.DELIVERED, True, 1, False, False, None, True),
        (17, AiUsageOutboxStatus.DELIVERED, True, 1, False, True, None, False),  # flagged
        (18, AiUsageOutboxStatus.SENDING, True, 1, False, False, None, True),
        (19, AiUsageOutboxStatus.DEAD, True, 1, False, False, None, True),
    ],
)
def test_held_collision_follows_each_row_state(
    mysql_world,
    caplog,
    number,
    status,
    first_attempted,
    attempts,
    resolved,
    review_required,
    expected,
    flags,
) -> None:
    item = _for(mysql_world, number)
    _insert_existing(
        mysql_world,
        item,
        status=status,
        first_attempted=first_attempted,
        attempts=attempts,
        resolved=resolved,
        review_required=review_required,
    )
    with caplog.at_level(logging.CRITICAL):
        persist_held_once(mysql_world.factory, item)
    row = _outbox(mysql_world, item)
    assert row.status is (status if expected is None else expected)
    assert (row.review_required_at is not None) is (flags or review_required)
    review_logs = [r for r in caplog.records if r.message == REVIEW_MESSAGE]
    # A new flag logs once; an existing unreviewed flag is logged again (design §2).
    if flags or review_required:
        assert len(review_logs) == 1
        assert review_logs[0].event_id == item.payload["event_id"]
        assert review_logs[0].ai_request_id == item.ai_request_id
        assert review_logs[0].outbox_status == status.value
    else:
        assert not review_logs


def test_non_duplicate_integrity_error_is_not_a_collision(mysql_world, caplog) -> None:
    item = _for(mysql_world, 20)
    orphan = HeldUsage(
        item.payload,
        mysql_world.tenant_id,
        mysql_world.project_id + 1_000_000,
        item.ai_request_id,
        item.commit_outcome,
    )
    with caplog.at_level(logging.CRITICAL), pytest.raises(IntegrityError):
        persist_held_once(mysql_world.factory, orphan)
    assert _outbox(mysql_world, item) is None
    assert not [r for r in caplog.records if r.message == REVIEW_MESSAGE]


def test_held_round_requeues_other_errors_at_tail_and_continues(mysql_world, caplog) -> None:
    good = _for(mysql_world, 21)
    bad_source = _for(mysql_world, 22)
    bad = HeldUsage(
        bad_source.payload,
        mysql_world.tenant_id,
        mysql_world.project_id + 1_000_000,
        bad_source.ai_request_id,
        bad_source.commit_outcome,
    )
    buffer, executor = HeldBuffer(3), AiDatabaseExecutor(threads=1, queue_max=0)
    buffer.put(bad)
    buffer.put(good)
    try:
        with caplog.at_level(logging.ERROR):
            asyncio.run(drain_held_once(buffer, executor, mysql_world.factory, timeout_seconds=5))
    finally:
        executor.shutdown()
    assert _outbox(mysql_world, good).status is AiUsageOutboxStatus.HELD
    assert _outbox(mysql_world, bad) is None
    assert (len(buffer), buffer.take()) == (1, bad)
    assert any(
        r.levelno == logging.ERROR and r.message == "AI usage held persistence failed"
        for r in caplog.records
    )


def test_pending_to_held_and_delivery_claim_have_one_winner(mysql_world, caplog) -> None:
    item = _for(mysql_world, 30)
    _insert_existing(mysql_world, item, status=AiUsageOutboxStatus.PENDING)
    barrier = threading.Barrier(2)

    def claim() -> int:
        with mysql_world.factory() as session:
            barrier.wait()
            result = session.execute(
                update(AiUsageOutbox)
                .where(
                    AiUsageOutbox.event_id == item.payload["event_id"],
                    AiUsageOutbox.status == AiUsageOutboxStatus.PENDING,
                    AiUsageOutbox.first_attempted_at.is_(None),
                )
                .values(
                    status=AiUsageOutboxStatus.SENDING, first_attempted_at=dt.datetime.now(dt.UTC)
                )
            )
            session.commit()
            return result.rowcount

    with caplog.at_level(logging.CRITICAL), ThreadPoolExecutor(max_workers=2) as pool:
        held = pool.submit(lambda: (barrier.wait(), persist_held_once(mysql_world.factory, item)))
        claimed = pool.submit(claim)
        held.result()
        claim_count = claimed.result()

    row = _outbox(mysql_world, item)
    review_logs = [
        record
        for record in caplog.records
        if record.message == "AI usage entered billing although the request returned 503"
    ]
    if claim_count == 1:
        assert row.status is AiUsageOutboxStatus.SENDING
        assert row.first_attempted_at is not None
        assert row.review_required_at is not None
        assert len(review_logs) == 1
    else:
        assert claim_count == 0
        assert row.status is AiUsageOutboxStatus.HELD
        assert row.first_attempted_at is None
        assert row.review_required_at is None
        assert not review_logs


def test_same_held_item_writes_once(mysql_world) -> None:
    item = _for(mysql_world, 31)
    persist_held_once(mysql_world.factory, item)
    persist_held_once(mysql_world.factory, item)
    with mysql_world.factory() as session:
        assert (
            session.query(AiUsageOutbox).filter_by(event_id=item.payload["event_id"]).count() == 1
        )


def test_held_buffer_discards_oldest_and_records_its_metadata(caplog) -> None:
    buffer, first, second = HeldBuffer(1), _held(40), _held(41)
    with caplog.at_level(logging.CRITICAL):
        buffer.put(first)
        buffer.put(second)
    assert buffer.take() == second
    assert (
        next(r for r in caplog.records if r.message == "AI usage held buffer overflow").event_id
        == first.payload["event_id"]
    )


def test_cancelled_persistence_is_logged_and_held(mysql_world, caplog, monkeypatch) -> None:
    item, buffer, executor = (
        _for(mysql_world, 50),
        HeldBuffer(2),
        AiDatabaseExecutor(threads=1, queue_max=0),
    )

    async def cancelled(*args, **kwargs):
        raise asyncio.CancelledError

    monkeypatch.setattr(executor, "run_with_timeout", cancelled)
    with caplog.at_level(logging.CRITICAL), pytest.raises(asyncio.CancelledError):
        asyncio.run(persist_usage(executor, mysql_world.factory, buffer, item, timeout_seconds=1))
    assert buffer.take().commit_outcome == "unknown"
    assert any(r.message == "AI usage not persisted" for r in caplog.records)
    executor.shutdown()


def test_held_round_stops_and_restores_head_on_connection_error(mysql_world, monkeypatch) -> None:
    first, second, buffer, executor = (
        _for(mysql_world, 60),
        _for(mysql_world, 61),
        HeldBuffer(3),
        AiDatabaseExecutor(threads=1, queue_max=0),
    )
    buffer.put(first)
    buffer.put(second)

    async def unavailable(*args, **kwargs):
        from sqlalchemy.exc import TimeoutError as SATimeoutError

        raise SATimeoutError("pool unavailable")

    monkeypatch.setattr(executor, "run_with_timeout", unavailable)
    asyncio.run(drain_held_once(buffer, executor, mysql_world.factory, timeout_seconds=1))
    assert (buffer.take(), buffer.take()) == (first, second)
    executor.shutdown()


def test_cancelled_held_round_restores_inflight_item(mysql_world, monkeypatch) -> None:
    item, buffer, executor = (
        _for(mysql_world, 62),
        HeldBuffer(2),
        AiDatabaseExecutor(threads=1, queue_max=0),
    )
    buffer.put(item)

    async def cancelled(*args, **kwargs):
        raise asyncio.CancelledError

    monkeypatch.setattr(executor, "run_with_timeout", cancelled)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(drain_held_once(buffer, executor, mysql_world.factory, timeout_seconds=1))
    assert buffer.take() == item
    executor.shutdown()


def test_usage_fields_survive_json_formatter(caplog, monkeypatch) -> None:
    buffer, item, executor = HeldBuffer(2), _held(70), AiDatabaseExecutor(threads=1, queue_max=0)

    async def unavailable(*args, **kwargs):
        raise RuntimeError("executor stopped")

    monkeypatch.setattr(executor, "run_with_timeout", unavailable)
    with caplog.at_level(logging.CRITICAL):
        assert (
            asyncio.run(persist_usage(executor, None, buffer, item, timeout_seconds=1)) == "unknown"
        )
    record = next(r for r in caplog.records if r.message == "AI usage not persisted")
    rendered = json.loads(JsonFormatter().format(record))
    assert {key: rendered[key] for key in rendered if key.startswith("usage_")} == {
        "usage_input": 1,
        "usage_output": 2,
        "usage_cache_write": 3,
        "usage_cache_read": 4,
    }
    executor.shutdown()

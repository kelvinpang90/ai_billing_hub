"""Audited operational commands for AI usage outbox incidents."""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
import sys
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.ai_api.database import create_ai_database_engine, create_ai_session_factory
from app.ai_api.messages import MAX_FEATURE_LENGTH
from app.ai_api.outbox import AiUsageOutbox, AiUsageOutboxStatus
from app.core.config import get_settings
from app.models.auth import AuditAction
from app.models.tenancy import Project, Tenant
from app.services.auth import RequestContext, record_audit, utc_now

ENTITY_AI_USAGE_OUTBOX: Final = "ai_usage_outbox"
REQUEUABLE_DEAD_ERRORS: Final = frozenset({"INTEGRATION_AUTH_FAILED", "INVALID_BILLING_RESPONSE"})
_CLI_CONTEXT: Final = RequestContext(ip_address=None, user_agent="cli")
_ULID = re.compile(r"^[0-7][0-9A-HJKMNP-TV-Z]{25}$")
_FEATURE = re.compile(r"^[a-z][a-z0-9_]{0,31}(?:\.[a-z][a-z0-9_]{0,31}){1,3}$")
_MODEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@-]{0,127}$")
_MAX_TOKENS: Final = 10**12
logger = logging.getLogger(__name__)

type Clock = Callable[[], dt.datetime]
type Operation = str


@dataclass(frozen=True)
class OperationResult:
    event_id: str
    operation: Operation


@dataclass(frozen=True)
class ImportItem:
    event_id: str
    request_id: str
    tenant_public_id: str
    project_public_id: str
    feature: str
    model: str
    input_tokens: int
    output_tokens: int
    cache_creation_input_tokens: int
    cache_read_input_tokens: int
    occurred_at: str
    commit_outcome: str

    @property
    def payload(self) -> dict[str, Any]:
        return {
            "schema_version": "1.0",
            "event_id": self.event_id,
            "request_id": self.request_id,
            "feature": self.feature,
            "tenant_id": self.tenant_public_id,
            "project_id": self.project_public_id,
            "provider": "anthropic",
            "model": self.model,
            "usage_type": "LLM_TOKEN",
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_creation_input_tokens": self.cache_creation_input_tokens,
            "cache_read_input_tokens": self.cache_read_input_tokens,
            "occurred_at": self.occurred_at,
        }


def _reason(reason: str) -> str | None:
    cleaned = reason.strip()
    return cleaned if 1 <= len(cleaned) <= 255 else None


def _lock(session: Session, event_id: str) -> AiUsageOutbox | None:
    statement = (
        select(AiUsageOutbox)
        .where(AiUsageOutbox.event_id == event_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return session.execute(statement).scalar_one_or_none()


def _requeue_one(
    session: Session,
    row: AiUsageOutbox,
    *,
    reason: str,
    include_delivered: bool,
    now: dt.datetime,
) -> bool:
    is_dead = row.status is AiUsageOutboxStatus.DEAD
    is_delivered = include_delivered and row.status is AiUsageOutboxStatus.DELIVERED
    if not is_delivered and (not is_dead or row.last_error not in REQUEUABLE_DEAD_ERRORS):
        return False

    before = {
        "status": row.status.value,
        "last_error": row.last_error,
        "attempts": row.attempts,
        "dead_acked_at": row.dead_acked_at.isoformat() if row.dead_acked_at else None,
    }
    if is_delivered:
        before["delivered_at"] = row.delivered_at.isoformat() if row.delivered_at else None
        row.delivered_at = None
    row.status = AiUsageOutboxStatus.PENDING
    row.next_attempt_at = now
    row.claim_id = None
    row.last_error = None
    row.dead_acked_at = None
    row.attempts = 0
    after: dict[str, object] = {
        "status": AiUsageOutboxStatus.PENDING.value,
        "last_error": None,
        "attempts": 0,
        "dead_acked_at": None,
    }
    if is_delivered:
        after["delivered_at"] = None
    record_audit(
        session,
        action=AuditAction.AI_USAGE_REQUEUE,
        context=_CLI_CONTEXT,
        now=now,
        entity_type=ENTITY_AI_USAGE_OUTBOX,
        entity_id=row.event_id,
        before_state=before,
        after_state=after,
        reason=reason,
    )
    return True


def _ack_one(session: Session, row: AiUsageOutbox, *, reason: str, now: dt.datetime) -> bool:
    if row.status is not AiUsageOutboxStatus.DEAD or row.dead_acked_at is not None:
        return False
    before = {"status": AiUsageOutboxStatus.DEAD.value, "dead_acked_at": None}
    row.dead_acked_at = now
    record_audit(
        session,
        action=AuditAction.AI_USAGE_DEAD_ACK,
        context=_CLI_CONTEXT,
        now=now,
        entity_type=ENTITY_AI_USAGE_OUTBOX,
        entity_id=row.event_id,
        before_state=before,
        after_state={"status": AiUsageOutboxStatus.DEAD.value, "dead_acked_at": now.isoformat()},
        reason=reason,
    )
    return True


def _held_one(
    session: Session, row: AiUsageOutbox, *, action: str, reason: str, now: dt.datetime
) -> bool:
    if row.status is not AiUsageOutboxStatus.HELD:
        return False
    status = AiUsageOutboxStatus.PENDING if action == "released" else AiUsageOutboxStatus.DISCARDED
    audit_action = (
        AuditAction.AI_USAGE_HELD_RELEASE
        if action == "released"
        else AuditAction.AI_USAGE_HELD_DISCARD
    )
    before = {"status": AiUsageOutboxStatus.HELD.value, "resolved_at": None}
    row.status = status
    row.resolved_at = now
    if action == "released":
        row.next_attempt_at = now
    after = {"status": status.value, "resolved_at": now.isoformat()}
    record_audit(
        session,
        action=audit_action,
        context=_CLI_CONTEXT,
        now=now,
        entity_type=ENTITY_AI_USAGE_OUTBOX,
        entity_id=row.event_id,
        before_state=before,
        after_state=after,
        reason=reason,
    )
    return True


def _review_ack_one(session: Session, row: AiUsageOutbox, *, reason: str, now: dt.datetime) -> bool:
    if row.review_required_at is None or row.reviewed_at is not None:
        return False
    before = {"status": row.status.value, "reviewed_at": None}
    row.reviewed_at = now
    record_audit(
        session,
        action=AuditAction.AI_USAGE_REVIEW_ACK,
        context=_CLI_CONTEXT,
        now=now,
        entity_type=ENTITY_AI_USAGE_OUTBOX,
        entity_id=row.event_id,
        before_state=before,
        after_state={"status": row.status.value, "reviewed_at": now.isoformat()},
        reason=reason,
    )
    return True


def _operate(
    session_factory: sessionmaker[Session],
    event_ids: Sequence[str],
    *,
    reason: str,
    include_delivered: bool = False,
    acknowledge: bool = False,
    clock: Clock = utc_now,
) -> tuple[OperationResult, ...]:
    """Apply one independently committed, locked transaction per event id."""
    cleaned = _reason(reason)
    if cleaned is None:
        raise ValueError("reason must be 1 to 255 characters after trimming")
    results: list[OperationResult] = []
    for event_id in event_ids:
        try:
            with session_factory.begin() as session:
                row = _lock(session, event_id)
                if row is None:
                    changed = False
                elif acknowledge:
                    changed = _ack_one(session, row, reason=cleaned, now=clock())
                else:
                    changed = _requeue_one(
                        session,
                        row,
                        reason=cleaned,
                        include_delivered=include_delivered,
                        now=clock(),
                    )
        except Exception:
            changed = False
        done: Operation = "acknowledged" if acknowledge else "requeued"
        results.append(OperationResult(event_id, done if changed else "failed"))
    return tuple(results)


def requeue(
    session_factory: sessionmaker[Session],
    event_ids: Sequence[str],
    *,
    reason: str,
    include_delivered: bool = False,
    clock: Clock = utc_now,
) -> tuple[OperationResult, ...]:
    return _operate(
        session_factory,
        event_ids,
        reason=reason,
        include_delivered=include_delivered,
        clock=clock,
    )


def dead_ack(
    session_factory: sessionmaker[Session],
    event_ids: Sequence[str],
    *,
    reason: str,
    clock: Clock = utc_now,
) -> tuple[OperationResult, ...]:
    return _operate(session_factory, event_ids, reason=reason, acknowledge=True, clock=clock)


def _held_operate(
    session_factory: sessionmaker[Session],
    event_ids: Sequence[str],
    *,
    action: str,
    reason: str,
    clock: Clock = utc_now,
) -> tuple[OperationResult, ...]:
    cleaned = _reason(reason)
    if cleaned is None:
        raise ValueError("reason must be 1 to 255 characters after trimming")
    results: list[OperationResult] = []
    for event_id in event_ids:
        try:
            with session_factory.begin() as session:
                row = _lock(session, event_id)
                changed = row is not None and _held_one(
                    session, row, action=action, reason=cleaned, now=clock()
                )
        except Exception:
            changed = False
        results.append(OperationResult(event_id, action if changed else "failed"))
    return tuple(results)


def held_release(
    session_factory: sessionmaker[Session],
    event_ids: Sequence[str],
    *,
    reason: str,
    clock: Clock = utc_now,
) -> tuple[OperationResult, ...]:
    return _held_operate(session_factory, event_ids, action="released", reason=reason, clock=clock)


def held_discard(
    session_factory: sessionmaker[Session],
    event_ids: Sequence[str],
    *,
    reason: str,
    clock: Clock = utc_now,
) -> tuple[OperationResult, ...]:
    return _held_operate(session_factory, event_ids, action="discarded", reason=reason, clock=clock)


def review_ack(
    session_factory: sessionmaker[Session],
    event_ids: Sequence[str],
    *,
    reason: str,
    clock: Clock = utc_now,
) -> tuple[OperationResult, ...]:
    cleaned = _reason(reason)
    if cleaned is None:
        raise ValueError("reason must be 1 to 255 characters after trimming")
    results: list[OperationResult] = []
    for event_id in event_ids:
        try:
            with session_factory.begin() as session:
                row = _lock(session, event_id)
                changed = row is not None and _review_ack_one(
                    session, row, reason=cleaned, now=clock()
                )
        except Exception:
            changed = False
        results.append(OperationResult(event_id, "acknowledged" if changed else "failed"))
    return tuple(results)


def _parse_import(line: str) -> tuple[ImportItem | None, str, str | None]:
    try:
        document = json.loads(line)
    except (TypeError, json.JSONDecodeError):
        return None, "invalid JSON", None
    if not isinstance(document, dict):
        return None, "log line is not an object", None
    event_id = document.get("event_id")
    label = event_id if isinstance(event_id, str) else None
    if document.get("message") != "AI usage not persisted":
        return None, "unexpected message", label
    values = (
        "event_id",
        "ai_request_id",
        "feature",
        "tenant_public_id",
        "project_public_id",
        "model",
        "usage_input",
        "usage_output",
        "usage_cache_write",
        "usage_cache_read",
        "occurred_at",
        "commit_outcome",
    )
    if any(key not in document for key in values):
        return None, "missing required field", label
    if not isinstance(event_id, str) or _ULID.fullmatch(event_id) is None:
        return None, "invalid event_id", label
    request_id = document["ai_request_id"]
    if not isinstance(request_id, str) or _ULID.fullmatch(request_id) is None:
        return None, "invalid ai_request_id", event_id
    feature, model = document["feature"], document["model"]
    if (
        not isinstance(feature, str)
        or len(feature) > MAX_FEATURE_LENGTH
        or _FEATURE.fullmatch(feature) is None
    ):
        return None, "invalid feature", event_id
    if not isinstance(model, str) or _MODEL.fullmatch(model) is None:
        return None, "invalid model", event_id
    tenant_id, project_id = document["tenant_public_id"], document["project_public_id"]
    try:
        valid_scope = (
            all(
                str(uuid.UUID(value)) == value
                for value in (tenant_id, project_id)
                if isinstance(value, str)
            )
            and isinstance(tenant_id, str)
            and isinstance(project_id, str)
        )
    except ValueError:
        valid_scope = False
    if not valid_scope:
        return None, "invalid public identity", event_id
    usages = tuple(document[key] for key in values[6:10])
    if any(type(value) is not int or not 0 <= value <= _MAX_TOKENS for value in usages):
        return None, "invalid usage", event_id
    occurred_at = document["occurred_at"]
    try:
        parsed_time = dt.datetime.fromisoformat(occurred_at.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        parsed_time = None
    if parsed_time is None or parsed_time.tzinfo is None:
        return None, "invalid occurred_at", event_id
    if not isinstance(document["commit_outcome"], str) or document["commit_outcome"] not in {
        "absent",
        "unknown",
    }:
        return None, "invalid commit_outcome", event_id
    return (
        ImportItem(
            event_id,
            request_id,
            tenant_id,
            project_id,
            feature,
            model,
            *usages,
            occurred_at,
            document["commit_outcome"],
        ),
        "",
        event_id,
    )


def _review_log(item: ImportItem, status: AiUsageOutboxStatus) -> None:
    logger.critical(
        "AI usage entered billing although the request returned 503",
        extra={
            "event_id": item.event_id,
            "ai_request_id": item.request_id,
            "feature": item.feature,
            "tenant_public_id": item.tenant_public_id,
            "project_public_id": item.project_public_id,
            "model": item.model,
            "outbox_status": status.value,
        },
    )


def _import_one(session: Session, item: ImportItem, *, reason: str, now: dt.datetime) -> str:
    identity = session.execute(
        select(Project, Tenant)
        .join(Tenant, Project.tenant_id == Tenant.id)
        .where(
            Project.public_id == item.project_public_id, Tenant.public_id == item.tenant_public_id
        )
    ).one_or_none()
    if identity is None:
        raise ValueError("tenant and project do not match")
    project, tenant = identity
    row = _lock(session, item.event_id)
    if row is not None and (row.tenant_id != tenant.id or row.project_id != project.id):
        raise ValueError("existing event identity does not match")
    if row is None:
        row = AiUsageOutbox(
            event_id=item.event_id,
            request_id=item.request_id,
            claim_id=None,
            tenant_id=tenant.id,
            project_id=project.id,
            feature=item.feature,
            payload_json=json.dumps(item.payload, separators=(",", ":")),
            status=AiUsageOutboxStatus.HELD,
            attempts=0,
            next_attempt_at=now,
            last_error=None,
            first_attempted_at=None,
            created_at=now,
            delivered_at=None,
            resolved_at=None,
            review_required_at=None,
            reviewed_at=None,
            dead_acked_at=None,
        )
        session.add(row)
        result, before, after = "created", None, {"status": AiUsageOutboxStatus.HELD.value}
    elif (
        row.status is AiUsageOutboxStatus.PENDING
        and row.first_attempted_at is None
        and row.resolved_at is None
    ):
        before = {"status": AiUsageOutboxStatus.PENDING.value}
        row.status = AiUsageOutboxStatus.HELD
        result, after = "held", {"status": AiUsageOutboxStatus.HELD.value}
    elif (
        row.resolved_at is None
        and row.review_required_at is None
        and (
            row.status
            in {
                AiUsageOutboxStatus.SENDING,
                AiUsageOutboxStatus.DELIVERED,
                AiUsageOutboxStatus.DEAD,
            }
            or (row.status is AiUsageOutboxStatus.PENDING and row.first_attempted_at is not None)
        )
    ):
        before = {
            "status": row.status.value,
            "reviewed_at": row.reviewed_at.isoformat() if row.reviewed_at else None,
        }
        row.review_required_at = now
        result, after = (
            "review_marked",
            {
                "status": row.status.value,
                "reviewed_at": row.reviewed_at.isoformat() if row.reviewed_at else None,
            },
        )
        _review_log(item, row.status)
    else:
        before = {"status": row.status.value}
        after = before.copy()
        result = f"unchanged:{row.status.value}"
        if row.review_required_at is not None and row.reviewed_at is None:
            _review_log(item, row.status)
    after["result"] = result
    record_audit(
        session,
        action=AuditAction.AI_USAGE_HELD_IMPORT,
        context=_CLI_CONTEXT,
        now=now,
        entity_type=ENTITY_AI_USAGE_OUTBOX,
        entity_id=item.event_id,
        before_state=before,
        after_state=after,
        reason=reason,
    )
    return result


def held_import(
    session_factory: sessionmaker[Session],
    lines: Sequence[str],
    *,
    reason: str,
    clock: Clock = utc_now,
) -> tuple[OperationResult, ...]:
    cleaned = _reason(reason)
    if cleaned is None:
        raise ValueError("reason must be 1 to 255 characters after trimming")
    results: list[OperationResult] = []
    for line in lines:
        item, error, label = _parse_import(line)
        if item is None:
            results.append(OperationResult(label or "-", f"rejected {error}"))
            continue
        try:
            with session_factory.begin() as session:
                operation = _import_one(session, item, reason=cleaned, now=clock())
        except Exception as error:
            operation = f"rejected {error}"
        results.append(OperationResult(item.event_id, operation))
    return tuple(results)


def _run(
    event_ids: Sequence[str],
    *,
    reason: str,
    include_delivered: bool = False,
    acknowledge: bool = False,
) -> int:
    if _reason(reason) is None:
        print("--reason must be 1 to 255 characters after trimming.", file=sys.stderr)
        return 2
    settings = get_settings()
    # 设计 §2「API 进程之外的库访问」：每次执行建一个自己的 engine，结束时 dispose。
    engine = create_ai_database_engine(
        settings.database_url,
        threads=1,
        pool_timeout_seconds=settings.ai_db_pool_timeout_seconds,
        connect_timeout_seconds=settings.ai_db_connect_timeout_seconds,
        rw_timeout_seconds=settings.ai_db_rw_timeout_seconds,
    )
    try:
        factory = create_ai_session_factory(engine)
        results = (
            dead_ack(factory, event_ids, reason=reason)
            if acknowledge
            else requeue(factory, event_ids, reason=reason, include_delivered=include_delivered)
        )
    finally:
        engine.dispose()
    for result in results:
        print(f"{result.event_id} {result.operation}")
    changed = sum(result.operation != "failed" for result in results)
    print(f"Changed {changed} / {len(results)}")
    return 0 if changed == len(results) else 1


def run_requeue(event_ids: Sequence[str], *, reason: str, include_delivered: bool = False) -> int:
    return _run(event_ids, reason=reason, include_delivered=include_delivered)


def run_dead_ack(event_ids: Sequence[str], *, reason: str) -> int:
    return _run(event_ids, reason=reason, acknowledge=True)


def run_held(command: str, values: Sequence[str], *, reason: str) -> int:
    if _reason(reason) is None:
        print("--reason must be 1 to 255 characters after trimming.", file=sys.stderr)
        return 2
    settings = get_settings()
    engine = create_ai_database_engine(
        settings.database_url,
        threads=1,
        pool_timeout_seconds=settings.ai_db_pool_timeout_seconds,
        connect_timeout_seconds=settings.ai_db_connect_timeout_seconds,
        rw_timeout_seconds=settings.ai_db_rw_timeout_seconds,
    )
    try:
        factory = create_ai_session_factory(engine)
        if command == "release":
            results = held_release(factory, values, reason=reason)
        elif command == "discard":
            results = held_discard(factory, values, reason=reason)
        elif command == "ack":
            results = review_ack(factory, values, reason=reason)
        else:
            results = held_import(factory, values, reason=reason)
    finally:
        engine.dispose()
    for result in results:
        print(f"{result.event_id} {result.operation}")
    changed = sum(
        not result.operation.startswith("failed") and not result.operation.startswith("rejected")
        for result in results
    )
    print(f"Changed {changed} / {len(results)}")
    return 0 if changed == len(results) else 1


__all__ = [
    "ENTITY_AI_USAGE_OUTBOX",
    "REQUEUABLE_DEAD_ERRORS",
    "OperationResult",
    "dead_ack",
    "held_discard",
    "held_import",
    "held_release",
    "requeue",
    "review_ack",
    "run_dead_ack",
    "run_held",
    "run_requeue",
]

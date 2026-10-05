"""Usage event data access (design gate #176 v8 §2, AIH-TASK-029).

⚠️ **只 flush，不 commit。**事件行、冲突行与审计的事务边界归调用方的 `session_scope()`
（app/services/usage_ingest.py）。

⚠️ **插入优先**：`insert_event` 直接 INSERT，不先查。撞 `uq_usage_events_event_id` 时 flush
抛 `IntegrityError`，由调用方回滚后用 `find_event` 在新事务里读已有行（§23「read-before-insert
alone is insufficient」）。

⚠️ 这里没有更新与删除：本任务只写 `RECEIVED`，冲突表只增（MySQL 上由触发器强制）。

⚠️ 这一层不写日志。
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.ai_catalog import PayloadShape, QuantityKind, UsageMeterType
from app.models.integration import IntegrationCredential
from app.models.tenancy import Project, Tenant
from app.models.usage import (
    ConflictMismatch,
    UsageEvent,
    UsageEventConflict,
    UsageEventStatus,
)


def find_meter_type(session: Session, code: str) -> UsageMeterType | None:
    """The meter type whose `code` equals `usage_type` byte for byte, whatever its status.

    停用（`RETIRED`）的类型照收：停用只影响「以后选不选它」，不影响计费（AIH-TASK-025）。
    """
    statement = select(UsageMeterType).where(UsageMeterType.code == code)
    return session.execute(statement).scalar_one_or_none()


def scope_public_ids(session: Session, *, tenant_id: int, project_id: int) -> tuple[str, str]:
    """`(tenant public_id, project public_id)` of a credential's owner."""
    statement = (
        select(Tenant.public_id, Project.public_id)
        .join(Project, Project.tenant_id == Tenant.id)
        .where(Tenant.id == tenant_id, Project.id == project_id)
    )
    tenant_public_id, project_public_id = session.execute(statement).one()
    return tenant_public_id, project_public_id


def insert_event(
    session: Session,
    *,
    event_id: str,
    schema_version: str,
    tenant_id: int,
    project_id: int,
    integration_credential_id: int,
    request_id: str,
    conversation_id: str | None,
    provider_code_raw: str,
    model_code_raw: str,
    usage_meter_type_id: int,
    payload_shape: PayloadShape,
    quantity_kind: QuantityKind,
    unit: str,
    tokens: dict[str, int] | None,
    quantity: Decimal | None,
    payload_fingerprint: str,
    occurred_at: dt.datetime,
    now: dt.datetime,
) -> UsageEvent:
    """Insert and flush one `RECEIVED` event. `now` is naive UTC, whole seconds.

    `tokens` 是 `LLM_TOKEN_FIELDS` 形态的四个字段（键即列名），`QUANTITY` 形态为 `None`。
    `provider_id` / `model_id` 留空（T-H 写）。
    """
    token_columns = tokens or {}
    row = UsageEvent(
        public_id=str(uuid.uuid4()),
        event_id=event_id,
        schema_version=schema_version,
        tenant_id=tenant_id,
        project_id=project_id,
        integration_credential_id=integration_credential_id,
        request_id=request_id,
        conversation_id=conversation_id,
        provider_code_raw=provider_code_raw,
        model_code_raw=model_code_raw,
        provider_id=None,
        model_id=None,
        usage_meter_type_id=usage_meter_type_id,
        payload_shape=payload_shape,
        quantity_kind=quantity_kind,
        input_tokens=token_columns.get("input_tokens"),
        output_tokens=token_columns.get("output_tokens"),
        cache_creation_input_tokens=token_columns.get("cache_creation_input_tokens"),
        cache_read_input_tokens=token_columns.get("cache_read_input_tokens"),
        quantity=quantity,
        unit=unit,
        payload_fingerprint=payload_fingerprint,
        status=UsageEventStatus.RECEIVED,
        error_code=None,
        error_message=None,
        occurred_at=occurred_at,
        received_at=now,
        processed_at=None,
        created_at=now,
    )
    session.add(row)
    session.flush()
    return row


def find_event(session: Session, event_id: str) -> tuple[UsageEvent, str] | None:
    """The event with this `event_id` and the `public_api_key` it was received with. No lock.

    MySQL 上撞唯一约束的那次 INSERT 会等先到的事务提交或回滚，所以这里读到的一定是
    已提交的行（设计 §4「串行化」）。
    """
    statement = (
        select(UsageEvent, IntegrationCredential.public_api_key)
        .join(
            IntegrationCredential,
            IntegrationCredential.id == UsageEvent.integration_credential_id,
        )
        .where(UsageEvent.event_id == event_id)
    )
    found = session.execute(statement).one_or_none()
    if found is None:
        return None
    event, api_key = found
    return event, api_key


def conflict_recorded(
    session: Session, *, event_id: str, api_key: str, payload_fingerprint: str
) -> bool:
    """Whether this `(event_id, api_key, fingerprint)` conflict is already in the table."""
    statement = (
        select(func.count())
        .select_from(UsageEventConflict)
        .where(
            UsageEventConflict.event_id == event_id,
            UsageEventConflict.api_key == api_key,
            UsageEventConflict.payload_fingerprint == payload_fingerprint,
        )
    )
    return bool(session.execute(statement).scalar_one())


def insert_conflict(
    session: Session,
    *,
    usage_event_id: int,
    event_id: str,
    integration_credential_id: int,
    api_key: str,
    payload_fingerprint: str,
    mismatch: ConflictMismatch,
    now: dt.datetime,
) -> UsageEventConflict:
    """Insert and flush one conflict row.

    并发的同一冲突撞 `uq_usage_event_conflicts_dedupe` 时 flush 抛 `IntegrityError`，
    由调用方回滚（连同审计）。
    """
    row = UsageEventConflict(
        usage_event_id=usage_event_id,
        event_id=event_id,
        integration_credential_id=integration_credential_id,
        api_key=api_key,
        payload_fingerprint=payload_fingerprint,
        mismatch=mismatch,
        received_at=now,
    )
    session.add(row)
    session.flush()
    return row


def count_conflicts_since(session: Session, since: dt.datetime) -> tuple[int, dt.datetime | None]:
    """`(rows, earliest received_at)` of the conflicts received at or after `since`."""
    statement = select(func.count(), func.min(UsageEventConflict.received_at)).where(
        UsageEventConflict.received_at >= since
    )
    count, earliest = session.execute(statement).one()
    return int(count), earliest


__all__ = [
    "conflict_recorded",
    "count_conflicts_since",
    "find_event",
    "find_meter_type",
    "insert_conflict",
    "insert_event",
    "scope_public_ids",
]

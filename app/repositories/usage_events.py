"""Usage event data access (design gate #176 v8 §2, AIH-TASK-029).

⚠️ **只 flush，不 commit。**事件行、冲突行与审计的事务边界归调用方的 `session_scope()`
（app/services/usage_ingest.py）。

⚠️ **插入优先**：`insert_event` 直接 INSERT，不先查。撞 `uq_usage_events_event_id` 时 flush
抛 `IntegrityError`，由调用方回滚后用 `find_event` 在新事务里读已有行（§23「read-before-insert
alone is insufficient」）。

⚠️ 这里没有更新与删除：本任务只写 `RECEIVED`，冲突表只增（MySQL 上由触发器强制）。

管理端查询（AIH-TASK-034）：`list_events` / `get_event_detail` 只读，不加锁、不 flush。
筛选条件里的客户与项目先换成内部 id（两次按唯一列的点查），之后的条件都是字面常量，
每个筛选都落在 AIH-TASK-029 / 032 建的索引上（见 `list_events` 的说明）。

⚠️ 这一层不写日志。
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal
from typing import NamedTuple

from sqlalchemy import ColumnElement, Select, func, select
from sqlalchemy.orm import Session

from app.models.ai_catalog import (
    AiModel,
    AiProvider,
    PayloadShape,
    QuantityKind,
    UsageMeterType,
)
from app.models.fx_rates import FxRateVersion
from app.models.integration import IntegrationCredential
from app.models.pricing_rules import PricingRule
from app.models.provider_prices import ProviderPriceVersion
from app.models.tenancy import Project, Tenant
from app.models.usage import (
    ConflictMismatch,
    UsageEvent,
    UsageEventConflict,
    UsageEventStatus,
)
from app.models.wallet import WalletTransaction


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


# --- 管理端查询（AIH-TASK-034，只读） ---------------------------------------------------


class EventListRow(NamedTuple):
    """One event with the public names of its owner and meter type."""

    event: UsageEvent
    customer_public_id: str
    company_name: str
    project_public_id: str
    usage_type: str


class EventDetailRow(NamedTuple):
    """An event, its owner, and the `public_id` of every row its snapshot points at."""

    listed: EventListRow
    # 目录引用（T-H 解析出来的；模型未知时为空）。
    provider_public_id: str | None
    model_public_id: str | None
    price_version_public_id: str | None
    pricing_rule_public_id: str | None
    fx_rate_version_public_id: str | None
    # 那一行 `AI_USAGE` 账本；0 元事件与未计费事件为空。
    ledger_public_id: str | None
    ledger_amount: Decimal | None
    # 撞了这个 `event_id` 的冲突请求，按写入顺序。
    conflicts: list[UsageEventConflict]


def _listing_columns() -> Select[tuple[UsageEvent, str, str, str, str]]:
    return (
        select(
            UsageEvent,
            Tenant.public_id,
            Tenant.company_name,
            Project.public_id,
            UsageMeterType.code,
        )
        .join(Tenant, Tenant.id == UsageEvent.tenant_id)
        .join(Project, Project.id == UsageEvent.project_id)
        .join(UsageMeterType, UsageMeterType.id == UsageEvent.usage_meter_type_id)
    )


def list_events(
    session: Session,
    *,
    offset: int,
    limit: int,
    customer_id: str | None = None,
    project_id: str | None = None,
    conversation_id: str | None = None,
    request_id: str | None = None,
    provider: str | None = None,
    model: str | None = None,
    occurred_from: dt.datetime | None = None,
    occurred_to: dt.datetime | None = None,
    status: UsageEventStatus | None = None,
    error_code: str | None = None,
) -> tuple[list[EventListRow], int]:
    """One page of events, newest first by row id, and the total. Filters combine with AND.

    `customer_id` / `project_id` 是 `public_id`，不存在就是空页（与「存在但没有事件」一样）；
    `provider` / `model` 与上报的原始字符串逐字节比较；时间段按 `occurred_at` 取
    `[occurred_from, occurred_to)`（不带时区的 UTC）。

    索引（迁移 0017 / 0018）：客户 → `ix_usage_events_tenant_id`；项目 →
    `ix_usage_events_project_id`；会话 → `ix_usage_events_conversation_id`；请求 →
    `ix_usage_events_request_id`；状态（可带时间段）→ `ix_usage_events_status_occurred`；
    只有时间段 → `ix_usage_events_occurred_at`。`provider` / `model` / `error_code` 没有专门的
    索引，与上面任一条件组合时是残余过滤；单独使用时按主键倒序扫（`ORDER BY id DESC LIMIT`）。
    三表连接都是主键点查（内连接不丢行：三个外键列都 `NOT NULL`），`COUNT` 不连接。
    """
    conditions: list[ColumnElement[bool]] = []
    if customer_id is not None:
        tenant_id = session.execute(
            select(Tenant.id).where(Tenant.public_id == customer_id)
        ).scalar_one_or_none()
        if tenant_id is None:
            return [], 0
        conditions.append(UsageEvent.tenant_id == tenant_id)
    if project_id is not None:
        project_row_id = session.execute(
            select(Project.id).where(Project.public_id == project_id)
        ).scalar_one_or_none()
        if project_row_id is None:
            return [], 0
        conditions.append(UsageEvent.project_id == project_row_id)
    if conversation_id is not None:
        conditions.append(UsageEvent.conversation_id == conversation_id)
    if request_id is not None:
        conditions.append(UsageEvent.request_id == request_id)
    if provider is not None:
        conditions.append(UsageEvent.provider_code_raw == provider)
    if model is not None:
        conditions.append(UsageEvent.model_code_raw == model)
    if occurred_from is not None:
        conditions.append(UsageEvent.occurred_at >= occurred_from)
    if occurred_to is not None:
        conditions.append(UsageEvent.occurred_at < occurred_to)
    if status is not None:
        conditions.append(UsageEvent.status == status)
    if error_code is not None:
        conditions.append(UsageEvent.error_code == error_code)

    listing = (
        _listing_columns()
        .where(*conditions)
        .order_by(UsageEvent.id.desc())
        .offset(offset)
        .limit(limit)
    )
    counting = select(func.count()).select_from(UsageEvent).where(*conditions)
    rows = [EventListRow(*found) for found in session.execute(listing).all()]
    total = int(session.execute(counting).scalar_one())
    return rows, total


def get_event_detail(session: Session, public_id: str) -> EventDetailRow | None:
    """The event with this `public_id`, every snapshot reference by `public_id`, its conflicts."""
    statement = (
        _listing_columns()
        .add_columns(
            AiProvider.public_id,
            AiModel.public_id,
            ProviderPriceVersion.public_id,
            PricingRule.public_id,
            FxRateVersion.public_id,
            WalletTransaction.public_id,
            WalletTransaction.amount,
        )
        .outerjoin(AiProvider, AiProvider.id == UsageEvent.provider_id)
        .outerjoin(AiModel, AiModel.id == UsageEvent.model_id)
        .outerjoin(
            ProviderPriceVersion,
            ProviderPriceVersion.id == UsageEvent.provider_price_version_id,
        )
        .outerjoin(PricingRule, PricingRule.id == UsageEvent.pricing_rule_id)
        .outerjoin(FxRateVersion, FxRateVersion.id == UsageEvent.fx_rate_version_id)
        .outerjoin(WalletTransaction, WalletTransaction.id == UsageEvent.wallet_transaction_id)
        .where(UsageEvent.public_id == public_id)
    )
    found = session.execute(statement).one_or_none()
    if found is None:
        return None
    values = tuple(found)
    listed = EventListRow(*values[:5])
    # 冲突表的去重唯一约束以 `event_id` 开头，这里按它查。
    conflicts = (
        select(UsageEventConflict)
        .where(UsageEventConflict.event_id == listed.event.event_id)
        .order_by(UsageEventConflict.id)
    )
    return EventDetailRow(
        listed,
        *values[5:],
        conflicts=list(session.execute(conflicts).scalars().all()),
    )


__all__ = [
    "EventDetailRow",
    "EventListRow",
    "conflict_recorded",
    "count_conflicts_since",
    "find_event",
    "find_meter_type",
    "get_event_detail",
    "insert_conflict",
    "insert_event",
    "list_events",
    "scope_public_ids",
]

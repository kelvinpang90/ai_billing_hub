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

客户用量查询（AIH-TASK-104，设计闸门 #398 v4 §2「查询」）：`list_processed_events_for_customer`
只读、不加锁，只 SELECT 设计列出的列（不复用管理端的 `EventListRow`：它读出成本列，INV-7）；
MySQL 上每条语句带 `MAX_EXECUTION_TIME` 提示，超时（错误 3024）转成 `QueryTooBroad`。

客户本月用量（AIH-TASK-107，设计闸门 #403 v1 §2「查询」）：`summarize_processed_events_for_customer`
在同一个会话里发两条 SELECT（按马来西亚日分组、对话去重），同样的租户、`PROCESSED`、区间条件与
执行时间预算；只读 `billable_cost`、`conversation_id`、`occurred_at`、`status`，
不读成本列（INV-7）。

⚠️ 这一层不写日志。
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal
from typing import Any, Final, NamedTuple

from sqlalchemy import ColumnElement, Date, Row, Select, and_, func, or_, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session
from sqlalchemy.sql.compiler import SQLCompiler
from sqlalchemy.sql.functions import FunctionElement

from app.core.fx_source import KUALA_LUMPUR_OFFSET
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
from app.repositories.tenancy import get_project_for_tenant


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
    feature: str | None = None,
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
        feature=feature,
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
    feature: str | None = None,
    occurred_from: dt.datetime | None = None,
    occurred_to: dt.datetime | None = None,
    status: UsageEventStatus | None = None,
    error_code: str | None = None,
) -> tuple[list[EventListRow], int]:
    """One page of events, newest first by row id, and the total. Filters combine with AND.

    `customer_id` / `project_id` 是 `public_id`，不存在就是空页（与「存在但没有事件」一样）；
    `provider` / `model` / `feature` 与上报的原始字符串逐字节比较；时间段按 `occurred_at` 取
    `[occurred_from, occurred_to)`（不带时区的 UTC）。

    索引（迁移 0017 / 0018）：客户 → `ix_usage_events_tenant_id`；项目 →
    `ix_usage_events_project_id`；会话 → `ix_usage_events_conversation_id`；请求 →
    `ix_usage_events_request_id`；状态（可带时间段）→ `ix_usage_events_status_occurred`；
    只有时间段 → `ix_usage_events_occurred_at`。`provider` / `model` / `feature` /
    `error_code` 没有专门的索引，与上面任一条件组合时是残余过滤；单独使用时按主键倒序扫
    （`ORDER BY id DESC LIMIT`）。
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
    if feature is not None:
        conditions.append(UsageEvent.feature == feature)
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


# --- 客户用量查询（AIH-TASK-104，只读） -------------------------------------------------

# 每条客户用量查询在 MySQL 上最多执行这么久（设计 §2「查询」）。不做成配置项。
CUSTOMER_USAGE_QUERY_TIMEOUT_MS: Final = 2000
# MySQL 的 ER_QUERY_TIMEOUT：语句超过 MAX_EXECUTION_TIME 被中止，已读的行不会部分返回。
_ER_QUERY_TIMEOUT: Final = 3024


class QueryTooBroad(Exception):
    """MySQL stopped the customer usage query at its execution-time budget (error 3024)."""


class CustomerUsageRow(NamedTuple):
    """Exactly the columns design §2「查询」 selects; no cost, rate or internal id."""

    public_id: str
    event_id: str
    request_id: str
    conversation_id: str | None
    feature: str | None
    project_public_id: str
    project_name: str
    provider: str
    model: str
    usage_type: str
    input_tokens: int | None
    output_tokens: int | None
    cache_creation_input_tokens: int | None
    cache_read_input_tokens: int | None
    quantity: Decimal | None
    unit: str
    billable_cost: Decimal
    occurred_at: dt.datetime


def customer_usage_statement(
    tenant_id: int,
    *,
    limit: int,
    project_row_id: int | None = None,
    provider: str | None = None,
    model: str | None = None,
    conversation_id: str | None = None,
    event_id: str | None = None,
    occurred_from: dt.datetime | None = None,
    occurred_to: dt.datetime | None = None,
    before: tuple[dt.datetime, str] | None = None,
    timeout_ms: int = CUSTOMER_USAGE_QUERY_TIMEOUT_MS,
) -> Select[tuple[object, ...]]:
    """The one SELECT of a customer usage page; the hint is rendered on MySQL only.

    `project_row_id` 是已在本租户内查出的项目内部 id；`before` 是游标 `(occurred_at, public_id)`，
    只取严格更旧的行。租户与 `PROCESSED` 两个条件恒在，其余筛选都是附加条件（INV-8）。
    """
    conditions: list[ColumnElement[bool]] = [
        UsageEvent.tenant_id == tenant_id,
        UsageEvent.status == UsageEventStatus.PROCESSED,
    ]
    if occurred_from is not None:
        conditions.append(UsageEvent.occurred_at >= occurred_from)
    if occurred_to is not None:
        conditions.append(UsageEvent.occurred_at < occurred_to)
    if project_row_id is not None:
        conditions.append(UsageEvent.project_id == project_row_id)
    if provider is not None:
        conditions.append(UsageEvent.provider_code_raw == provider)
    if model is not None:
        conditions.append(UsageEvent.model_code_raw == model)
    if conversation_id is not None:
        conditions.append(UsageEvent.conversation_id == conversation_id)
    if event_id is not None:
        conditions.append(UsageEvent.event_id == event_id)
    if before is not None:
        before_at, before_public_id = before
        conditions.append(
            or_(
                UsageEvent.occurred_at < before_at,
                and_(
                    UsageEvent.occurred_at == before_at,
                    UsageEvent.public_id < before_public_id,
                ),
            )
        )
    return (
        select(
            UsageEvent.public_id,
            UsageEvent.event_id,
            UsageEvent.request_id,
            UsageEvent.conversation_id,
            UsageEvent.feature,
            Project.public_id,
            Project.name,
            UsageEvent.provider_code_raw,
            UsageEvent.model_code_raw,
            UsageMeterType.code,
            UsageEvent.input_tokens,
            UsageEvent.output_tokens,
            UsageEvent.cache_creation_input_tokens,
            UsageEvent.cache_read_input_tokens,
            UsageEvent.quantity,
            UsageEvent.unit,
            UsageEvent.billable_cost,
            UsageEvent.occurred_at,
        )
        .join(Project, Project.id == UsageEvent.project_id)
        .join(UsageMeterType, UsageMeterType.id == UsageEvent.usage_meter_type_id)
        .where(*conditions)
        .order_by(UsageEvent.occurred_at.desc(), UsageEvent.public_id.desc())
        .limit(limit)
        # 优化器提示紧跟 SELECT 关键字；SQLite（只在测试里用）不渲染。
        .prefix_with(f"/*+ MAX_EXECUTION_TIME({int(timeout_ms)}) */", dialect="mysql")
    )


def list_processed_events_for_customer(
    session: Session,
    tenant_id: int,
    *,
    limit: int,
    project_id: str | None = None,
    provider: str | None = None,
    model: str | None = None,
    conversation_id: str | None = None,
    event_id: str | None = None,
    occurred_from: dt.datetime | None = None,
    occurred_to: dt.datetime | None = None,
    before: tuple[dt.datetime, str] | None = None,
    timeout_ms: int = CUSTOMER_USAGE_QUERY_TIMEOUT_MS,
) -> list[CustomerUsageRow]:
    """Up to `limit` billed events of the tenant, newest first by `(occurred_at, public_id)`.

    `project_id` 是项目 `public_id`：先在本租户内查出内部 id，查不到（不存在或属于别的客户）
    直接返回空列表，不发用量查询。`timeout_ms` 只给测试用。MySQL 超时抛 `QueryTooBroad`；
    其他数据库错误原样抛出。
    """
    project_row_id: int | None = None
    if project_id is not None:
        project = get_project_for_tenant(session, tenant_id, project_id)
        if project is None:
            return []
        project_row_id = project.id
    statement = customer_usage_statement(
        tenant_id,
        limit=limit,
        project_row_id=project_row_id,
        provider=provider,
        model=model,
        conversation_id=conversation_id,
        event_id=event_id,
        occurred_from=occurred_from,
        occurred_to=occurred_to,
        before=before,
        timeout_ms=timeout_ms,
    )
    return [CustomerUsageRow(*row) for row in _run_bounded(session, statement)]


def _run_bounded(session: Session, statement: Select[tuple[object, ...]]) -> list[Row[Any]]:
    """Execute a customer usage statement; MySQL's timeout (3024) becomes `QueryTooBroad`."""
    try:
        return list(session.execute(statement).all())
    except DBAPIError as error:
        arguments = getattr(error.orig, "args", ()) if error.orig is not None else ()
        if arguments and arguments[0] == _ER_QUERY_TIMEOUT:
            raise QueryTooBroad from error
        raise


# --- 客户本月用量（AIH-TASK-107，设计闸门 #403 v1 §2「查询」，只读） ---------------------

# 马来西亚日期表达式里的小时数，与日期换算同一个偏移（固定 UTC+8）。
_KUALA_LUMPUR_HOURS: Final = int(KUALA_LUMPUR_OFFSET.total_seconds()) // 3600


class MalaysiaDay(FunctionElement[dt.date]):
    """The Malaysian calendar day of a naive-UTC datetime column, rendered per dialect.

    MySQL：`DATE(DATE_ADD(col, INTERVAL 8 HOUR))`；SQLite（只在测试里用）：
    `date(col, '+8 hours')`。结果类型是 `Date`：SQLite 返回的文本也读成 `datetime.date`。
    """

    type = Date()
    name = "malaysia_day"
    inherit_cache = True


@compiles(MalaysiaDay)
def _malaysia_day_sqlite(element: MalaysiaDay, compiler: SQLCompiler, **kw: Any) -> str:
    column = compiler.process(element.clauses, **kw)
    return f"date({column}, '+{_KUALA_LUMPUR_HOURS} hours')"


@compiles(MalaysiaDay, "mysql")
def _malaysia_day_mysql(element: MalaysiaDay, compiler: SQLCompiler, **kw: Any) -> str:
    column = compiler.process(element.clauses, **kw)
    return f"DATE(DATE_ADD({column}, INTERVAL {_KUALA_LUMPUR_HOURS} HOUR))"


class CustomerUsageDay(NamedTuple):
    """One Malaysian day that has billed events: how many, and their `billable_cost` sum."""

    day: dt.date
    request_count: int
    charged_amount: Decimal


class CustomerUsageSummary(NamedTuple):
    """Days that have events, ascending (the others are absent), and distinct conversations."""

    days: list[CustomerUsageDay]
    conversation_count: int


def _summary_conditions(
    tenant_id: int, occurred_from: dt.datetime, occurred_to: dt.datetime
) -> list[ColumnElement[bool]]:
    # 租户、`PROCESSED` 与区间三个条件恒在（INV-8）。
    return [
        UsageEvent.tenant_id == tenant_id,
        UsageEvent.status == UsageEventStatus.PROCESSED,
        UsageEvent.occurred_at >= occurred_from,
        UsageEvent.occurred_at < occurred_to,
    ]


def customer_usage_daily_statement(
    tenant_id: int,
    *,
    occurred_from: dt.datetime,
    occurred_to: dt.datetime,
    timeout_ms: int = CUSTOMER_USAGE_QUERY_TIMEOUT_MS,
) -> Select[tuple[object, ...]]:
    """① Per Malaysian day: `COUNT(*)` and `SUM(billable_cost)`. No cost column is read."""
    day = MalaysiaDay(UsageEvent.occurred_at)
    return (
        select(
            day.label("day"),
            func.count().label("request_count"),
            func.coalesce(func.sum(UsageEvent.billable_cost), 0).label("charged_amount"),
        )
        .where(*_summary_conditions(tenant_id, occurred_from, occurred_to))
        .group_by(day)
        .order_by(day)
        .prefix_with(f"/*+ MAX_EXECUTION_TIME({int(timeout_ms)}) */", dialect="mysql")
    )


def customer_usage_conversation_statement(
    tenant_id: int,
    *,
    occurred_from: dt.datetime,
    occurred_to: dt.datetime,
    timeout_ms: int = CUSTOMER_USAGE_QUERY_TIMEOUT_MS,
) -> Select[tuple[object, ...]]:
    """② Distinct non-null `conversation_id` over the whole period (NULL is not counted)."""
    return (
        select(func.count(UsageEvent.conversation_id.distinct()))
        .where(*_summary_conditions(tenant_id, occurred_from, occurred_to))
        .prefix_with(f"/*+ MAX_EXECUTION_TIME({int(timeout_ms)}) */", dialect="mysql")
    )


def summarize_processed_events_for_customer(
    session: Session,
    tenant_id: int,
    *,
    occurred_from: dt.datetime,
    occurred_to: dt.datetime,
    timeout_ms: int = CUSTOMER_USAGE_QUERY_TIMEOUT_MS,
) -> CustomerUsageSummary:
    """Billed events of the tenant in `[occurred_from, occurred_to)`, per day and conversations.

    两条 SELECT 用同一个会话、不提交：同一个连接、同一个事务（MySQL `REPEATABLE READ` 下同一个
    快照），不加锁、不写。`timeout_ms` 只给测试用。任一条在 MySQL 上超时抛 `QueryTooBroad`；
    其他数据库错误原样抛出。
    """
    period = {"occurred_from": occurred_from, "occurred_to": occurred_to, "timeout_ms": timeout_ms}
    daily = _run_bounded(session, customer_usage_daily_statement(tenant_id, **period))
    [(conversations,)] = _run_bounded(
        session, customer_usage_conversation_statement(tenant_id, **period)
    )
    return CustomerUsageSummary(
        days=[CustomerUsageDay(day, int(count), Decimal(amount)) for day, count, amount in daily],
        conversation_count=int(conversations),
    )


__all__ = [
    "CUSTOMER_USAGE_QUERY_TIMEOUT_MS",
    "CustomerUsageDay",
    "CustomerUsageRow",
    "CustomerUsageSummary",
    "EventDetailRow",
    "EventListRow",
    "MalaysiaDay",
    "QueryTooBroad",
    "conflict_recorded",
    "count_conflicts_since",
    "customer_usage_conversation_statement",
    "customer_usage_daily_statement",
    "customer_usage_statement",
    "find_event",
    "find_meter_type",
    "get_event_detail",
    "insert_conflict",
    "insert_event",
    "list_events",
    "list_processed_events_for_customer",
    "scope_public_ids",
    "summarize_processed_events_for_customer",
]

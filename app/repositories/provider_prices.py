"""Provider price data access and price resolution (design gate #177 v3, AIH-TASK-026).

⚠️ **只 flush，不 commit。**版本、分量与它们的审计同一事务，事务边界归调用方的
`session_scope()`（app/services/provider_prices.py）。`resolve_provider_price` 更是如此：
它必须在调用方（T-H）写事件快照、扣费的那个事务里、在 `resolve_model` 之后调用，读到的
版本与分量的共享锁持有到那个事务提交（设计 §2「对下游任务的契约」T-H）。

⚠️ **发布与退役里的读、以及取价都是加锁读**（`FOR UPDATE` / `FOR SHARE`）：MySQL 默认的
REPEATABLE READ 下，普通 SELECT 读的是本事务第一次一致性读时的快照 —— 锁拿到了、数据却
可能是旧的。加锁读总是读最新提交的版本。`populate_existing`：会话里已有的对象按锁内读到的
值刷新。SQLite 忽略这两种锁。

⚠️ **写入顺序有讲究**（设计 §2，数据库的区间触发器与 `open_slot` 唯一索引都按行检查）：
每个改区间的函数都立刻 flush，调用方按「先截断前一个、再发布本行」「先把被撤销的一行改成
空区间、再恢复前一个」的顺序调用。版本行没有删除；分量只在草稿「整体替换」时删掉旧的。
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, NamedTuple

from sqlalchemy import Select, func, or_, select
from sqlalchemy.orm import Session

from app.models.ai_catalog import AiModel, AiProvider, UsageMeterComponent, UsageMeterType
from app.models.auth import User
from app.models.provider_prices import (
    PERIOD_STATUSES,
    PriceSourceType,
    PriceVersionStatus,
    ProviderPriceComponent,
    ProviderPriceVersion,
)


class NewComponent(NamedTuple):
    """One component to write into a draft."""

    usage_meter_component_id: int
    unit_quantity: Decimal
    rate_amount: Decimal
    metadata: dict[str, Any] | None


class PricedComponent(NamedTuple):
    """A price component with the catalog rows that give it its code, field and unit."""

    row: ProviderPriceComponent
    component: UsageMeterComponent
    meter_type: UsageMeterType


@dataclass(frozen=True)
class ResolvedPriceComponent:
    """One price of a resolved version. T-G takes the quantity from `quantity_field`."""

    component_code: str
    quantity_field: str
    meter_type_id: int
    unit_quantity: Decimal
    rate_amount: Decimal


@dataclass(frozen=True)
class ResolvedProviderPrice:
    """The version in effect at `occurred_at` and its components (`component_code` ascending).

    `version_id` 是内部 id：T-H 把它写进事件快照（`provider_price_version_id`），不对外。
    """

    version_id: int
    public_id: str
    provider_id: int
    model_id: int
    source_currency: str
    effective_from: dt.datetime | None
    effective_to: dt.datetime | None
    components: tuple[ResolvedPriceComponent, ...]


def _new_public_id() -> str:
    return str(uuid.uuid4())


def _page[T](
    session: Session, statement: Select[tuple[T]], *, offset: int, limit: int
) -> tuple[list[T], int]:
    # 边界由接口层校验（422）；这里只挡住调用方写错。
    if offset < 0:
        raise ValueError("offset must not be negative")
    if limit < 1:
        raise ValueError("limit must be at least 1")
    total = session.execute(select(func.count()).select_from(statement.subquery())).scalar_one()
    rows = session.execute(statement.offset(offset).limit(limit)).scalars().all()
    return list(rows), int(total)


def _shared[T](statement: Select[T]) -> Select[T]:
    """Locking read in share mode (`FOR SHARE`); refresh objects already in the session."""
    return statement.with_for_update(read=True).execution_options(populate_existing=True)


# --- 读 ---------------------------------------------------------------------------


def get_version(
    session: Session, public_id: str, *, for_update: bool = False
) -> ProviderPriceVersion | None:
    statement = select(ProviderPriceVersion).where(ProviderPriceVersion.public_id == public_id)
    if for_update:
        statement = statement.with_for_update().execution_options(populate_existing=True)
    return session.execute(statement).scalar_one_or_none()


def lock_provider(session: Session, provider_id: int) -> AiProvider:
    """`SELECT … FOR UPDATE` on the provider row (MySQL; SQLite ignores it).

    与 025 改映射同一把锁：发布、退役彼此串行，也与 `resolve_model` 的共享锁互斥
    （设计 §2「为什么与计费不会交错」）。边界时刻 `t` 必须在它返回**之后**才取。
    """
    statement = (
        select(AiProvider)
        .where(AiProvider.id == provider_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return session.execute(statement).scalar_one()


def get_model(session: Session, model_id: int) -> AiModel:
    return session.execute(select(AiModel).where(AiModel.id == model_id)).scalar_one()


def get_provider(session: Session, provider_id: int) -> AiProvider:
    return session.execute(select(AiProvider).where(AiProvider.id == provider_id)).scalar_one()


def find_model(session: Session, public_id: str) -> AiModel | None:
    """A model by its public id, whichever provider it belongs to (list filter only)."""
    statement = select(AiModel).where(AiModel.public_id == public_id)
    return session.execute(statement).scalar_one_or_none()


def meter_components_by_code(
    session: Session, component_codes: Iterable[str]
) -> dict[str, tuple[UsageMeterComponent, UsageMeterType]]:
    """`component_code` → (the component, its meter type). Unknown codes are simply absent."""
    codes = sorted(set(component_codes))
    if not codes:
        return {}
    statement = (
        select(UsageMeterComponent, UsageMeterType)
        .join(UsageMeterType, UsageMeterType.id == UsageMeterComponent.meter_type_id)
        .where(UsageMeterComponent.component_code.in_(codes))
    )
    found = session.execute(statement).tuples()
    return {component.component_code: (component, kind) for component, kind in found}


def components_for(
    session: Session, version_ids: Iterable[int]
) -> dict[int, list[PricedComponent]]:
    """Version id → its components, `component_code` ascending."""
    ids = sorted(set(version_ids))
    found: dict[int, list[PricedComponent]] = {version_id: [] for version_id in ids}
    if not ids:
        return found
    statement = (
        select(ProviderPriceComponent, UsageMeterComponent, UsageMeterType)
        .join(
            UsageMeterComponent,
            UsageMeterComponent.id == ProviderPriceComponent.usage_meter_component_id,
        )
        .join(UsageMeterType, UsageMeterType.id == UsageMeterComponent.meter_type_id)
        .where(ProviderPriceComponent.provider_price_version_id.in_(ids))
        .order_by(UsageMeterComponent.component_code)
    )
    for row, component, kind in session.execute(statement):
        found[row.provider_price_version_id].append(PricedComponent(row, component, kind))
    return found


def providers_and_models(
    session: Session, rows: Sequence[ProviderPriceVersion]
) -> tuple[dict[int, AiProvider], dict[int, AiModel]]:
    """The providers and models the versions hang on, by internal id."""
    provider_ids = sorted({row.provider_id for row in rows})
    model_ids = sorted({row.model_id for row in rows})
    if not rows:
        return {}, {}
    providers = session.execute(select(AiProvider).where(AiProvider.id.in_(provider_ids)))
    models = session.execute(select(AiModel).where(AiModel.id.in_(model_ids)))
    return (
        {provider.id: provider for provider in providers.scalars()},
        {model.id: model for model in models.scalars()},
    )


def user_emails(session: Session, user_ids: Iterable[int | None]) -> dict[int, str]:
    """Internal user id → login email. Admin views name people by email, never by id."""
    ids = sorted({user_id for user_id in user_ids if user_id is not None})
    if not ids:
        return {}
    statement = select(User.id, User.email).where(User.id.in_(ids))
    return {row.id: row.email for row in session.execute(statement)}


def list_versions(
    session: Session,
    *,
    provider_id: int | None,
    model_id: int | None,
    status: PriceVersionStatus | None,
    offset: int,
    limit: int,
) -> tuple[list[ProviderPriceVersion], int]:
    """One page by provider code, model code and `effective_from` (NULL first); the total."""
    statement = (
        select(ProviderPriceVersion)
        .join(AiProvider, AiProvider.id == ProviderPriceVersion.provider_id)
        .join(AiModel, AiModel.id == ProviderPriceVersion.model_id)
    )
    if provider_id is not None:
        statement = statement.where(ProviderPriceVersion.provider_id == provider_id)
    if model_id is not None:
        statement = statement.where(ProviderPriceVersion.model_id == model_id)
    if status is not None:
        statement = statement.where(ProviderPriceVersion.status == status)
    # 两种方言的升序都把 NULL 排最前（草稿的起点也是 NULL）；再按 id 定住顺序。
    statement = statement.order_by(
        AiProvider.code,
        AiModel.code,
        ProviderPriceVersion.effective_from.is_not(None),
        ProviderPriceVersion.effective_from,
        ProviderPriceVersion.id,
    )
    return _page(session, statement, offset=offset, limit=limit)


# --- 区间 -------------------------------------------------------------------------


def version_order(row: ProviderPriceVersion) -> tuple[bool, dt.datetime, int]:
    """Time order: the one without a start first (排序在 Python 里做，不依赖方言的 NULL 约定)."""
    return (row.effective_from is not None, row.effective_from or dt.datetime.min, row.id)


def is_empty_period(row: ProviderPriceVersion) -> bool:
    """`[F, F)`: a retired reservation. It never matches and takes no part in the timeline."""
    return row.effective_from is not None and row.effective_from == row.effective_to


def period_versions(
    session: Session, provider_id: int, model_id: int
) -> list[ProviderPriceVersion]:
    """Every PUBLISHED / RETIRED version of the (provider, model), in time order. Locking read."""
    statement = (
        select(ProviderPriceVersion)
        .where(
            ProviderPriceVersion.provider_id == provider_id,
            ProviderPriceVersion.model_id == model_id,
            ProviderPriceVersion.status.in_(PERIOD_STATUSES),
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return sorted(session.execute(statement).scalars().all(), key=version_order)


# --- 写 ---------------------------------------------------------------------------


def insert_draft(
    session: Session,
    *,
    provider_id: int,
    model_id: int,
    source_currency: str,
    source_reference: str,
    created_by: int,
    now: dt.datetime,
) -> ProviderPriceVersion:
    """Insert and flush a `MANUAL` draft: no period, no approver (设计 §2)."""
    row = ProviderPriceVersion(
        public_id=_new_public_id(),
        provider_id=provider_id,
        model_id=model_id,
        source_currency=source_currency,
        source_type=PriceSourceType.MANUAL,
        source_reference=source_reference,
        status=PriceVersionStatus.DRAFT,
        effective_from=None,
        effective_to=None,
        created_by=created_by,
        approved_by=None,
        created_at=now,
        updated_at=now,
        approved_at=None,
    )
    session.add(row)
    session.flush()
    return row


def insert_components(
    session: Session,
    version: ProviderPriceVersion,
    components: Sequence[NewComponent],
    *,
    now: dt.datetime,
) -> None:
    """Add the components to a draft and flush (the database refuses a non-draft)."""
    if version.status is not PriceVersionStatus.DRAFT:
        raise ValueError("components are written only on a draft")
    session.add_all(
        ProviderPriceComponent(
            provider_price_version_id=version.id,
            usage_meter_component_id=component.usage_meter_component_id,
            unit_quantity=component.unit_quantity,
            rate_amount=component.rate_amount,
            metadata_json=component.metadata,
            created_at=now,
        )
        for component in components
    )
    session.flush()


def drop_draft_components(session: Session, version: ProviderPriceVersion) -> None:
    """Remove every component of a draft, before its replacement set is inserted.

    ⚠️ 先 flush 掉删除再插入：同一次 flush 里 SQLAlchemy 先插后删，会撞
    `(provider_price_version_id, usage_meter_component_id)` 的唯一约束。
    """
    if version.status is not PriceVersionStatus.DRAFT:
        raise ValueError("components are replaced only on a draft")
    statement = select(ProviderPriceComponent).where(
        ProviderPriceComponent.provider_price_version_id == version.id
    )
    for row in session.execute(statement).scalars().all():
        session.delete(row)
    session.flush()


def save_draft(
    session: Session,
    row: ProviderPriceVersion,
    *,
    source_currency: str,
    source_reference: str,
    now: dt.datetime,
) -> None:
    if row.status is not PriceVersionStatus.DRAFT:
        raise ValueError("only a draft is edited")
    row.source_currency = source_currency
    row.source_reference = source_reference
    row.updated_at = now
    session.flush()


def mark_discarded(session: Session, row: ProviderPriceVersion, *, now: dt.datetime) -> None:
    if row.status is not PriceVersionStatus.DRAFT:
        raise ValueError("only a draft is discarded")
    row.status = PriceVersionStatus.DISCARDED
    row.updated_at = now
    session.flush()


def truncate(
    session: Session, row: ProviderPriceVersion, *, at: dt.datetime, now: dt.datetime
) -> None:
    """`effective_to`: NULL → `at` on the open PUBLISHED version; a successor takes over."""
    if row.status is not PriceVersionStatus.PUBLISHED or row.effective_to is not None:
        raise ValueError("only the untruncated published version is truncated")
    row.effective_to = at
    row.updated_at = now
    session.flush()


def restore_open(session: Session, row: ProviderPriceVersion, *, now: dt.datetime) -> None:
    """`effective_to` → NULL again: the reservation that truncated it was withdrawn."""
    if row.status is not PriceVersionStatus.PUBLISHED or row.effective_to is None:
        raise ValueError("only a truncated published version is restored")
    row.effective_to = None
    row.updated_at = now
    session.flush()


def mark_published(
    session: Session,
    row: ProviderPriceVersion,
    *,
    effective_from: dt.datetime | None,
    approved_by: int,
    now: dt.datetime,
) -> None:
    """DRAFT → PUBLISHED with `[effective_from, NULL)`; the approver is the publisher."""
    if row.status is not PriceVersionStatus.DRAFT:
        raise ValueError("only a draft is published")
    row.status = PriceVersionStatus.PUBLISHED
    row.effective_from = effective_from
    row.effective_to = None
    row.approved_by = approved_by
    row.approved_at = now
    row.updated_at = now
    session.flush()


def mark_retired(
    session: Session, row: ProviderPriceVersion, *, effective_to: dt.datetime, now: dt.datetime
) -> None:
    """PUBLISHED → RETIRED, ending at `effective_to` (= its start for a withdrawn reservation)."""
    if row.status is not PriceVersionStatus.PUBLISHED or row.effective_to is not None:
        raise ValueError("only the untruncated published version is retired")
    row.status = PriceVersionStatus.RETIRED
    row.effective_to = effective_to
    row.updated_at = now
    session.flush()


# --- 取价（T-G / T-H 调用；本任务不接到任何计费路径） ------------------------------------


def resolve_provider_price(
    session: Session, provider_id: int, model_id: int, occurred_at: dt.datetime
) -> ResolvedProviderPrice | None:
    """The provider price in effect for the model at `occurred_at`, or `None`.

    取 `PUBLISHED` / `RETIRED` 且 `effective_from ≤ occurred_at < effective_to`（NULL 端不比）
    的版本及其分量；没有就是 `None`（T-G 判 `PRICING_ERROR`，缺价不按 0 算）。空区间（撤销的
    预约）永不匹配。`provider_id` / `model_id` 是内部 id（取自 `resolve_model` 返回的模型），
    `occurred_at` 是不带时区的 UTC。

    ⚠️ 调用方契约（设计 §2 T-H）：在计费事务里**先** `resolve_model`（它对供应商行加共享锁，
    与发布、退役的排他锁互斥），**再**调用这里；两者都是加锁读（`FOR SHARE`），锁持有到计费
    事务提交。于是一个事件一旦取到某个版本，以后永远取到同一个版本。

    各版本的非空区间不重叠（服务层在供应商锁内保证并复查，数据库触发器兜底），所以至多一个
    命中；多于一个是数据损坏，`scalar_one_or_none` 抛错，不静默挑一个。
    """
    started = or_(
        ProviderPriceVersion.effective_from.is_(None),
        ProviderPriceVersion.effective_from <= occurred_at,
    )
    not_ended = or_(
        ProviderPriceVersion.effective_to.is_(None),
        ProviderPriceVersion.effective_to > occurred_at,
    )
    in_effect = select(ProviderPriceVersion).where(
        ProviderPriceVersion.provider_id == provider_id,
        ProviderPriceVersion.model_id == model_id,
        ProviderPriceVersion.status.in_(PERIOD_STATUSES),
        started,
        not_ended,
    )
    version = session.execute(_shared(in_effect)).scalar_one_or_none()
    if version is None:
        return None

    priced = (
        select(ProviderPriceComponent, UsageMeterComponent)
        .join(
            UsageMeterComponent,
            UsageMeterComponent.id == ProviderPriceComponent.usage_meter_component_id,
        )
        .where(ProviderPriceComponent.provider_price_version_id == version.id)
        .order_by(UsageMeterComponent.component_code)
    )
    components = tuple(
        ResolvedPriceComponent(
            component_code=component.component_code,
            quantity_field=component.quantity_field,
            meter_type_id=component.meter_type_id,
            unit_quantity=row.unit_quantity,
            rate_amount=row.rate_amount,
        )
        for row, component in session.execute(_shared(priced))
    )
    return ResolvedProviderPrice(
        version_id=version.id,
        public_id=version.public_id,
        provider_id=version.provider_id,
        model_id=version.model_id,
        source_currency=version.source_currency,
        effective_from=version.effective_from,
        effective_to=version.effective_to,
        components=components,
    )


__all__ = [
    "NewComponent",
    "PricedComponent",
    "ResolvedPriceComponent",
    "ResolvedProviderPrice",
    "components_for",
    "drop_draft_components",
    "find_model",
    "get_model",
    "get_provider",
    "get_version",
    "insert_components",
    "insert_draft",
    "is_empty_period",
    "list_versions",
    "lock_provider",
    "mark_discarded",
    "mark_published",
    "mark_retired",
    "meter_components_by_code",
    "period_versions",
    "providers_and_models",
    "resolve_provider_price",
    "restore_open",
    "save_draft",
    "truncate",
    "user_emails",
    "version_order",
]

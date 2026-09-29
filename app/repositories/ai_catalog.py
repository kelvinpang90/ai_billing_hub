"""AI catalog data access and model resolution (design gate #163 v4, AIH-TASK-025).

⚠️ **只 flush，不 commit。**目录行与它的审计同一事务，事务边界归调用方的
`session_scope()`（app/services/ai_catalog.py）。`resolve_model` 更是如此：它必须在
调用方（T-H）写事件快照、扣费的那个事务里调用，加的供应商行共享锁持有到那个事务
提交（设计 §2「对下游任务的契约」T-H ①）。

⚠️ **按供应商的读取一律带 `provider_id`。**模型与别名段只在自己的供应商下查得到；
别家的与不存在的一样是 `None`。

⚠️ **写路径与解析里的读都是加锁读**（`FOR UPDATE` / `FOR SHARE`）：MySQL 默认的
REPEATABLE READ 下，普通 SELECT 读的是本事务第一次一致性读时的快照 —— 锁拿到了、
数据却可能是旧的。加锁读总是读最新提交的版本（设计 §2「时间」）。
`populate_existing`：会话里已有的对象按锁内读到的值刷新。SQLite 忽略这两种锁。

⚠️ **这里没有删除。**停用只改状态；别名段只会被截断（设计 §1）。
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Iterable, Mapping
from typing import Final

from sqlalchemy import Select, func, or_, select
from sqlalchemy.orm import Session

from app.models.ai_catalog import (
    QUANTITY_FIELD,
    AiModel,
    AiModelAlias,
    AiProvider,
    CatalogStatus,
    PayloadShape,
    QuantityKind,
    UsageMeterComponent,
    UsageMeterType,
)

# 管理员能改的列（设计 §2「接口」）：其余一律建后不可改。
EDITABLE_FIELDS: Final = ("display_name", "status")

type Editable = UsageMeterType | AiProvider | AiModel


def _new_public_id() -> str:
    return str(uuid.uuid4())


def _check_page(offset: int, limit: int) -> None:
    # 边界由接口层校验（422）；这里只挡住调用方写错。
    if offset < 0:
        raise ValueError("offset must not be negative")
    if limit < 1:
        raise ValueError("limit must be at least 1")


def _page[T](
    session: Session, statement: Select[tuple[T]], *, offset: int, limit: int
) -> tuple[list[T], int]:
    _check_page(offset, limit)
    total = session.execute(select(func.count()).select_from(statement.subquery())).scalar_one()
    rows = session.execute(statement.offset(offset).limit(limit)).scalars().all()
    return list(rows), int(total)


def apply_changes(
    row: Editable, *, changes: Mapping[str, object], now: dt.datetime, session: Session
) -> dict[str, tuple[object, object]]:
    """Apply `display_name` / `status`; return `{field: (before, after)}` for real changes.

    值与现有的相同就不算改动；一个都没变时不碰 `updated_at`、不 flush。
    """
    if not set(changes) <= set(EDITABLE_FIELDS):
        raise ValueError(f"not editable: {sorted(set(changes) - set(EDITABLE_FIELDS))}")
    changed: dict[str, tuple[object, object]] = {}
    for name in EDITABLE_FIELDS:
        if name not in changes:
            continue
        before, after = getattr(row, name), changes[name]
        if name == "status":
            before, after = CatalogStatus(before), CatalogStatus(str(after))
        if before != after:
            changed[name] = (before, after)
            setattr(row, name, after)
    if changed:
        row.updated_at = now
        session.flush()
    return changed


# --- 计量类型与分量 ---------------------------------------------------------------


def get_meter_type(
    session: Session, public_id: str, *, for_update: bool = False
) -> UsageMeterType | None:
    statement = select(UsageMeterType).where(UsageMeterType.public_id == public_id)
    if for_update:
        statement = statement.with_for_update().execution_options(populate_existing=True)
    return session.execute(statement).scalar_one_or_none()


def meter_type_code_exists(session: Session, code: str) -> bool:
    statement = select(UsageMeterType.id).where(UsageMeterType.code == code)
    return session.execute(statement).first() is not None


def component_code_exists(session: Session, component_code: str) -> bool:
    statement = select(UsageMeterComponent.id).where(
        UsageMeterComponent.component_code == component_code
    )
    return session.execute(statement).first() is not None


def insert_quantity_meter_type(
    session: Session,
    *,
    code: str,
    display_name: str,
    unit: str,
    quantity_kind: QuantityKind,
    now: dt.datetime,
) -> UsageMeterType:
    """Insert and flush a `QUANTITY` type. Admins can create no other shape (design §2).

    撞上 `code` 的唯一约束就在 flush 时抛 `IntegrityError`，由调用方映射。
    """
    row = UsageMeterType(
        public_id=_new_public_id(),
        code=code,
        display_name=display_name,
        payload_shape=PayloadShape.QUANTITY,
        unit=unit,
        quantity_kind=quantity_kind,
        status=CatalogStatus.ACTIVE,
        created_at=now,
        updated_at=now,
    )
    session.add(row)
    session.flush()
    return row


def insert_quantity_component(
    session: Session, *, meter_type: UsageMeterType, component_code: str, now: dt.datetime
) -> UsageMeterComponent:
    """The one component of a `QUANTITY` type; it takes its amount from `quantity`."""
    row = UsageMeterComponent(
        meter_type_id=meter_type.id,
        payload_shape=meter_type.payload_shape,
        component_code=component_code,
        quantity_field=QUANTITY_FIELD,
        created_at=now,
    )
    session.add(row)
    session.flush()
    return row


def components_for(
    session: Session, meter_type_ids: Iterable[int]
) -> dict[int, list[UsageMeterComponent]]:
    """`meter_type_id` → its components, `component_code` ascending."""
    ids = sorted(set(meter_type_ids))
    found: dict[int, list[UsageMeterComponent]] = {meter_type_id: [] for meter_type_id in ids}
    if not ids:
        return found
    statement = (
        select(UsageMeterComponent)
        .where(UsageMeterComponent.meter_type_id.in_(ids))
        .order_by(UsageMeterComponent.component_code)
    )
    for row in session.execute(statement).scalars():
        found[row.meter_type_id].append(row)
    return found


def list_meter_types(
    session: Session, *, status: CatalogStatus | None, offset: int, limit: int
) -> tuple[list[UsageMeterType], int]:
    """One page, `code` ascending, and the total."""
    statement = select(UsageMeterType).order_by(UsageMeterType.code)
    if status is not None:
        statement = statement.where(UsageMeterType.status == status)
    return _page(session, statement, offset=offset, limit=limit)


# --- 供应商 -----------------------------------------------------------------------


def insert_provider(
    session: Session, *, code: str, display_name: str, now: dt.datetime
) -> AiProvider:
    row = AiProvider(
        public_id=_new_public_id(),
        code=code,
        display_name=display_name,
        status=CatalogStatus.ACTIVE,
        created_at=now,
        updated_at=now,
    )
    session.add(row)
    session.flush()
    return row


def provider_code_exists(session: Session, code: str) -> bool:
    statement = select(AiProvider.id).where(AiProvider.code == code)
    return session.execute(statement).first() is not None


def get_provider(session: Session, public_id: str) -> AiProvider | None:
    statement = select(AiProvider).where(AiProvider.public_id == public_id)
    return session.execute(statement).scalar_one_or_none()


def lock_provider(session: Session, public_id: str) -> AiProvider | None:
    """`SELECT … FOR UPDATE` on the provider row (MySQL; SQLite ignores it).

    建模型、映射别名、撤销别名都先拿这把锁：三者彼此串行，也与 `resolve_model` 的
    共享锁互斥（设计 §2「时间」、§4「串行化」）。改映射的边界时刻 `t` 必须在它返回
    **之后**才取。
    """
    statement = (
        select(AiProvider)
        .where(AiProvider.public_id == public_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return session.execute(statement).scalar_one_or_none()


def list_providers(
    session: Session, *, status: CatalogStatus | None, offset: int, limit: int
) -> tuple[list[AiProvider], int]:
    statement = select(AiProvider).order_by(AiProvider.code)
    if status is not None:
        statement = statement.where(AiProvider.status == status)
    return _page(session, statement, offset=offset, limit=limit)


# --- 模型 -------------------------------------------------------------------------


def get_model(
    session: Session, provider_id: int, public_id: str, *, for_update: bool = False
) -> AiModel | None:
    """None when absent **or** another provider's — deliberately indistinguishable."""
    statement = select(AiModel).where(
        AiModel.provider_id == provider_id, AiModel.public_id == public_id
    )
    if for_update:
        statement = statement.with_for_update().execution_options(populate_existing=True)
    return session.execute(statement).scalar_one_or_none()


def model_code_exists(session: Session, provider_id: int, code: str) -> bool:
    """Locking read; call it under the provider lock (design §2「模型代码与别名不能撞」)."""
    statement = (
        select(AiModel.id)
        .where(AiModel.provider_id == provider_id, AiModel.code == code)
        .with_for_update()
    )
    return session.execute(statement).first() is not None


def insert_model(
    session: Session, *, provider_id: int, code: str, display_name: str, now: dt.datetime
) -> AiModel:
    row = AiModel(
        public_id=_new_public_id(),
        provider_id=provider_id,
        code=code,
        display_name=display_name,
        status=CatalogStatus.ACTIVE,
        created_at=now,
        updated_at=now,
    )
    session.add(row)
    session.flush()
    return row


def list_models(
    session: Session,
    provider_id: int,
    *,
    status: CatalogStatus | None,
    offset: int,
    limit: int,
) -> tuple[list[AiModel], int]:
    statement = select(AiModel).where(AiModel.provider_id == provider_id).order_by(AiModel.code)
    if status is not None:
        statement = statement.where(AiModel.status == status)
    return _page(session, statement, offset=offset, limit=limit)


def model_codes(session: Session, model_ids: Iterable[int]) -> dict[int, tuple[str, str]]:
    """Internal model id → (public id, code). Aliases point at models this way in views."""
    ids = sorted(set(model_ids))
    if not ids:
        return {}
    statement = select(AiModel.id, AiModel.public_id, AiModel.code).where(AiModel.id.in_(ids))
    return {row.id: (row.public_id, row.code) for row in session.execute(statement)}


# --- 别名段 -----------------------------------------------------------------------


def alias_ever_used(session: Session, provider_id: int, alias: str) -> bool:
    """Whether the string appears in any segment, truncated or not. Locking read."""
    statement = (
        select(AiModelAlias.id)
        .where(AiModelAlias.provider_id == provider_id, AiModelAlias.alias == alias)
        .with_for_update()
    )
    return session.execute(statement).first() is not None


def segments_for(session: Session, provider_id: int, alias: str) -> list[AiModelAlias]:
    """Every segment of the string under the provider, in time order. Locking read.

    `effective_from` 为 NULL 的第一段排最前（排序在 Python 里做，不依赖方言对 NULL
    的排序约定）。
    """
    statement = (
        select(AiModelAlias)
        .where(AiModelAlias.provider_id == provider_id, AiModelAlias.alias == alias)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    rows = list(session.execute(statement).scalars().all())
    return sorted(rows, key=segment_order)


def segment_order(row: AiModelAlias) -> tuple[bool, dt.datetime, int]:
    return (row.effective_from is not None, row.effective_from or dt.datetime.min, row.id)


def get_segment(
    session: Session, provider_id: int, public_id: str, *, for_update: bool = False
) -> AiModelAlias | None:
    statement = select(AiModelAlias).where(
        AiModelAlias.provider_id == provider_id, AiModelAlias.public_id == public_id
    )
    if for_update:
        statement = statement.with_for_update().execution_options(populate_existing=True)
    return session.execute(statement).scalar_one_or_none()


def insert_segment(
    session: Session,
    *,
    provider_id: int,
    model_id: int,
    alias: str,
    effective_from: dt.datetime | None,
    now: dt.datetime,
) -> AiModelAlias:
    """Insert and flush an open segment `[effective_from, NULL) → model_id`.

    已有未截断的段时撞上 `open_slot` 的唯一索引；指向别家模型时撞上复合外键
    （MySQL）。都在 flush 时抛，由调用方处理。
    """
    row = AiModelAlias(
        public_id=_new_public_id(),
        provider_id=provider_id,
        model_id=model_id,
        alias=alias,
        effective_from=effective_from,
        effective_to=None,
        created_at=now,
        closed_at=None,
    )
    session.add(row)
    session.flush()
    return row


def truncate_segment(
    session: Session, row: AiModelAlias, *, at: dt.datetime, now: dt.datetime
) -> None:
    """`effective_to`: NULL → `at`, once and for all; `closed_at` is written with it."""
    if row.effective_to is not None:
        raise ValueError("the segment is already truncated")
    row.effective_to = at
    row.closed_at = now
    session.flush()


def list_segments(
    session: Session,
    provider_id: int,
    *,
    alias: str | None,
    open_only: bool,
    offset: int,
    limit: int,
) -> tuple[list[AiModelAlias], int]:
    """One page of segments, by `alias` then `effective_from` (the NULL one first)."""
    statement = select(AiModelAlias).where(AiModelAlias.provider_id == provider_id)
    if alias is not None:
        statement = statement.where(AiModelAlias.alias == alias)
    if open_only:
        statement = statement.where(AiModelAlias.effective_to.is_(None))
    # MySQL 与 SQLite 的升序都把 NULL 排最前；再按 id 定住同值的顺序。
    statement = statement.order_by(
        AiModelAlias.alias,
        AiModelAlias.effective_from.is_not(None),
        AiModelAlias.effective_from,
        AiModelAlias.id,
    )
    return _page(session, statement, offset=offset, limit=limit)


def open_segments_for_model(session: Session, model: AiModel) -> list[AiModelAlias]:
    """The untruncated segments that point at the model, `alias` ascending."""
    statement = (
        select(AiModelAlias)
        .where(
            AiModelAlias.provider_id == model.provider_id,
            AiModelAlias.model_id == model.id,
            AiModelAlias.effective_to.is_(None),
        )
        .order_by(AiModelAlias.alias)
    )
    return list(session.execute(statement).scalars().all())


# --- 解析（T-H 调用；本任务不接到任何计费路径） -------------------------------------


def resolve_model(
    session: Session, provider_code: str, model_code: str, occurred_at: dt.datetime
) -> AiModel | None:
    """The model a reported `(provider, model)` string meant at `occurred_at`, or `None`.

    按「模型代码精确匹配 → 在 `occurred_at` 那一刻生效的别名段」解析，都不中返回
    `None`（「未知」，T-H 进 `MODEL_UNKNOWN`）。不做大小写或任何规范化；不看供应商与
    模型的状态 —— 停用的模型照样解析（设计 §2「停用的含义」）。`occurred_at` 是不带
    时区的 UTC，段按半开区间 `[effective_from, effective_to)` 比较。

    ⚠️ 调用方契约（设计 §2 T-H）：
    - 在写事件快照、扣费的**同一个事务**里调用。这里对供应商行加的共享锁（`FOR SHARE`）
      持有到那个事务提交，改映射（排他锁）因此与它严格先后；
    - 拿到锁之后取当前时间，只处理 `is_due(occurred_at, now)` 的事件（见下）；
    - 锁顺序：供应商（共享）→ 钱包 → 租户。

    模型与别名都用加锁读（`FOR SHARE`）：调用方在此之前很可能已经读过别的表、建立了
    一致性读快照，普通 SELECT 会读到改映射提交之前的旧段（设计 §2「时间」）。
    """
    by_provider_code = select(AiProvider).where(AiProvider.code == provider_code)
    provider = session.execute(_shared(by_provider_code)).scalar_one_or_none()
    if provider is None:
        return None

    by_code = select(AiModel).where(AiModel.provider_id == provider.id, AiModel.code == model_code)
    model = session.execute(_shared(by_code)).scalar_one_or_none()
    if model is not None:
        return model

    # 各段不重叠（服务层在供应商锁内保证并复查），所以至多一段命中；多于一段是数据
    # 损坏，`scalar_one_or_none` 抛错，不静默挑一个。
    started = or_(
        AiModelAlias.effective_from.is_(None),
        AiModelAlias.effective_from <= occurred_at,
    )
    not_ended = or_(
        AiModelAlias.effective_to.is_(None),
        AiModelAlias.effective_to > occurred_at,
    )
    in_effect = select(AiModelAlias).where(
        AiModelAlias.provider_id == provider.id,
        AiModelAlias.alias == model_code,
        started,
        not_ended,
    )
    segment = session.execute(_shared(in_effect)).scalar_one_or_none()
    if segment is None:
        return None

    target = select(AiModel).where(
        AiModel.id == segment.model_id, AiModel.provider_id == provider.id
    )
    return session.execute(_shared(target)).scalar_one()


def _shared[T](statement: Select[tuple[T]]) -> Select[tuple[T]]:
    """Locking read in share mode (`FOR SHARE`); refresh objects already in the session."""
    return statement.with_for_update(read=True).execution_options(populate_existing=True)


def is_due(occurred_at: dt.datetime, now: dt.datetime) -> bool:
    """Whether an event may be resolved now: `occurred_at` is not after `now`.

    `now` 必须是 `resolve_model` 拿到供应商行锁**之后**取的当前时间（不带时区的 UTC）。
    更晚发生的事件本轮不处理、留在 `RECEIVED` 下一轮再取（设计 §2 T-H ②）：否则它会在
    改映射取边界之前就被解析，落在将来才会被截断的旧段上。本任务只提供判定，不接线。
    """
    return occurred_at <= now


__all__ = [
    "EDITABLE_FIELDS",
    "alias_ever_used",
    "apply_changes",
    "component_code_exists",
    "components_for",
    "get_meter_type",
    "get_model",
    "get_provider",
    "get_segment",
    "insert_model",
    "insert_provider",
    "insert_quantity_component",
    "insert_quantity_meter_type",
    "insert_segment",
    "is_due",
    "list_meter_types",
    "list_models",
    "list_providers",
    "list_segments",
    "lock_provider",
    "meter_type_code_exists",
    "model_code_exists",
    "model_codes",
    "open_segments_for_model",
    "provider_code_exists",
    "resolve_model",
    "segment_order",
    "segments_for",
    "truncate_segment",
]

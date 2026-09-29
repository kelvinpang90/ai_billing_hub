"""AI provider, model and usage meter catalog (design gate #163 v4, AIH-TASK-025).

⚠️ **每个写操作一个 `session_scope()`**（INV-13）：目录行与它的审计同一事务，要么都在、
要么都不在；新建计量类型时类型行、分量行与审计三者同一事务。repository 只 flush。
不写 outbox：目录变化没有下游订阅者（设计 §2「外部系统与异步边界」）。

⚠️ **建模型、映射别名、撤销别名都先 `SELECT … FOR UPDATE` 锁供应商行**，之后的检查与
写入都在锁内：三者彼此串行，也与计费解析（`resolve_model` 的共享锁）严格先后。
改映射的边界时刻 `t` 在**拿到锁之后**才取当前时间，向上取整到下一个整秒
（设计 §2「时间」）。建供应商、建计量类型只靠唯一约束兜底。

⚠️ 别名段只截断、不改指向、不往过去延伸。每次写别名之后，在同一事务里复查该字符串的
全部段首尾相接、只有第一段 `effective_from` 为空；不满足就抛错、整体回滚（设计 §2）。

⚠️ 审计的前后状态里，指向一律用模型 `code` 表示，不用内部 id（设计 §2「审计」）。
这一层不写日志。
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from itertools import pairwise
from typing import Final

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.core.database import session_scope
from app.core.errors import AppError
from app.models.ai_catalog import (
    AiModel,
    AiModelAlias,
    AiProvider,
    CatalogStatus,
    PayloadShape,
    QuantityKind,
    UsageMeterType,
)
from app.models.auth import AuditAction, User
from app.repositories import ai_catalog as catalog
from app.schemas.ai_catalog import (
    AliasSegmentView,
    MeterTypeView,
    ModelDetail,
    ModelView,
    ProviderView,
    alias_segment_view,
    meter_type_view,
    model_detail,
    model_view,
    provider_view,
)
from app.schemas.customers import Page
from app.services.auth import RequestContext, record_audit, utc_now

# 审计的 entity_type；entity_id 一律是那一行的 public_id。
ENTITY_METER_TYPE: Final = "usage_meter_type"
ENTITY_PROVIDER: Final = "ai_provider"
ENTITY_MODEL: Final = "ai_model"
ENTITY_ALIAS: Final = "ai_model_alias"

_ONE_SECOND = dt.timedelta(seconds=1)

# 取当前时间（不带时区的 UTC）。可注入，测试据此冻结时钟。
type Clock = Callable[[], dt.datetime]


class MeterTypeNotFound(AppError):
    def __init__(self) -> None:
        super().__init__(
            "Usage meter type not found.",
            code="USAGE_METER_TYPE_NOT_FOUND",
            http_status=404,
        )


class ProviderNotFound(AppError):
    def __init__(self) -> None:
        super().__init__(
            "AI provider not found.",
            code="AI_PROVIDER_NOT_FOUND",
            http_status=404,
        )


class ModelNotFound(AppError):
    """Absent, or another provider's model — the same 404 (design §2「字段规则」)."""

    def __init__(self) -> None:
        super().__init__(
            "AI model not found.",
            code="AI_MODEL_NOT_FOUND",
            http_status=404,
        )


class AliasNotFound(AppError):
    """Absent, another provider's, or not the untruncated segment."""

    def __init__(self) -> None:
        super().__init__(
            "AI model alias not found.",
            code="AI_MODEL_ALIAS_NOT_FOUND",
            http_status=404,
        )


class MeterTypeCodeTaken(AppError):
    def __init__(self) -> None:
        super().__init__(
            "The usage meter type code is taken.",
            code="USAGE_METER_TYPE_CODE_TAKEN",
            http_status=409,
        )


class ComponentCodeTaken(AppError):
    """The component code exists under any meter type."""

    def __init__(self) -> None:
        super().__init__(
            "The usage meter component code is taken.",
            code="USAGE_METER_COMPONENT_CODE_TAKEN",
            http_status=409,
        )


class ProviderCodeTaken(AppError):
    def __init__(self) -> None:
        super().__init__(
            "The AI provider code is taken.",
            code="AI_PROVIDER_CODE_TAKEN",
            http_status=409,
        )


class ModelCodeTaken(AppError):
    """A model of the provider has the code, or the string has ever been an alias there."""

    def __init__(self) -> None:
        super().__init__(
            "The AI model code is taken, or it has been used as an alias.",
            code="AI_MODEL_CODE_TAKEN",
            http_status=409,
        )


class AliasTaken(AppError):
    """The string is the code of a model of the same provider."""

    def __init__(self) -> None:
        super().__init__(
            "The alias is the code of a model.",
            code="AI_MODEL_ALIAS_TAKEN",
            http_status=409,
        )


class AliasSegmentsBroken(RuntimeError):
    """The in-transaction recheck failed: a bug, never a user error. 500, rolled back."""


@dataclass(frozen=True)
class MappedAlias:
    """The untruncated segment after a map, and whether this call wrote it."""

    segment: AliasSegmentView
    created: bool


def _seconds(moment: dt.datetime) -> dt.datetime:
    # MySQL 的 DATETIME 不存小数秒：与 app/services/customers.py 的 `_now` 同一理由。
    return moment.replace(microsecond=0)


def boundary_after(now: dt.datetime) -> dt.datetime:
    """`now` rounded **up** to the next whole second; a whole-second `now` gives `now + 1s`.

    `t` 因此严格晚于 `now`：此刻之前（含此刻）发生的事件一律落在旧段（设计 §2「时间」）。
    """
    return _seconds(now) + _ONE_SECOND


def _cut_point(now: dt.datetime, current: AiModelAlias) -> dt.datetime:
    """Where to truncate the untruncated segment `current`.

    一般就是 `boundary_after(now)`。例外：同一秒内连续两次改映射时，`current` 自己的
    `effective_from` 可能已经 ≥ 这个 `t`（它是上一次在将来取的边界），照 `t` 截断会得到
    一段空区间、撞 CHECK。这时取 `effective_from + 1s`：仍是整秒、仍严格晚于 `now`，
    且 `current` 起点之后还没有任何事件能被解析（T-H 只处理 `occurred_at ≤ now`），
    所以不改变任何已经解析到模型的结果。
    """
    at = boundary_after(now)
    if current.effective_from is not None and at <= current.effective_from:
        at = current.effective_from + _ONE_SECOND
    return at


def _text(moment: dt.datetime | None) -> str | None:
    return moment.isoformat() if moment is not None else None


def _plain(value: object) -> object:
    return value.value if isinstance(value, CatalogStatus) else value


def _offset(page: int, page_size: int) -> int:
    return (page - 1) * page_size


def _change_states(
    changed: Mapping[str, tuple[object, object]],
) -> tuple[dict[str, object], dict[str, object]]:
    """Audit before / after: only the fields that changed (design §2「审计」)."""
    before = {name: _plain(old) for name, (old, _) in changed.items()}
    after = {name: _plain(new) for name, (_, new) in changed.items()}
    return before, after


# --- 计量类型 ---------------------------------------------------------------------


def _meter_type_view(session: Session, row: UsageMeterType) -> MeterTypeView:
    return meter_type_view(row, catalog.components_for(session, [row.id])[row.id])


def create_meter_type(
    session_factory: sessionmaker[Session],
    *,
    actor: User,
    code: str,
    display_name: str,
    unit: str,
    quantity_kind: QuantityKind,
    component_code: str,
    context: RequestContext,
    clock: Clock = utc_now,
) -> MeterTypeView:
    """A `QUANTITY` type, its one component and `USAGE_METER_TYPE_CREATE` in one commit.

    `code` 或 `component_code` 已存在（含与种子重复、并发下撞唯一约束）：409，类型行与
    分量行都不写。
    """
    with session_scope(session_factory) as session:
        now = _seconds(clock())
        if catalog.meter_type_code_exists(session, code):
            raise MeterTypeCodeTaken
        if catalog.component_code_exists(session, component_code):
            raise ComponentCodeTaken
        try:
            row = catalog.insert_quantity_meter_type(
                session,
                code=code,
                display_name=display_name,
                unit=unit,
                quantity_kind=quantity_kind,
                now=now,
            )
        except IntegrityError:
            raise MeterTypeCodeTaken from None
        try:
            component = catalog.insert_quantity_component(
                session, meter_type=row, component_code=component_code, now=now
            )
        except IntegrityError:
            raise ComponentCodeTaken from None
        record_audit(
            session,
            action=AuditAction.USAGE_METER_TYPE_CREATE,
            context=context,
            now=now,
            actor=actor,
            entity_type=ENTITY_METER_TYPE,
            entity_id=row.public_id,
            after_state={
                "code": row.code,
                "display_name": row.display_name,
                "payload_shape": PayloadShape(row.payload_shape).value,
                "unit": row.unit,
                "quantity_kind": QuantityKind(row.quantity_kind).value,
                "status": CatalogStatus(row.status).value,
                "component_code": component.component_code,
            },
        )
        view = meter_type_view(row, [component])
    return view


def get_meter_type(session_factory: sessionmaker[Session], meter_type_id: str) -> MeterTypeView:
    with session_factory() as session:
        row = catalog.get_meter_type(session, meter_type_id)
        if row is None:
            raise MeterTypeNotFound
        return _meter_type_view(session, row)


def list_meter_types(
    session_factory: sessionmaker[Session],
    *,
    status: CatalogStatus | None,
    page: int,
    page_size: int,
) -> Page[MeterTypeView]:
    """`code` ascending, each with its components."""
    with session_factory() as session:
        rows, total = catalog.list_meter_types(
            session, status=status, offset=_offset(page, page_size), limit=page_size
        )
        components = catalog.components_for(session, [row.id for row in rows])
        return Page[MeterTypeView](
            items=[meter_type_view(row, components[row.id]) for row in rows],
            page=page,
            page_size=page_size,
            total=total,
        )


def update_meter_type(
    session_factory: sessionmaker[Session],
    *,
    actor: User,
    meter_type_id: str,
    changes: Mapping[str, object],
    context: RequestContext,
    clock: Clock = utc_now,
) -> MeterTypeView:
    """Rename or change the status. Nothing really changed: 200, no audit, no write."""
    with session_scope(session_factory) as session:
        row = catalog.get_meter_type(session, meter_type_id, for_update=True)
        if row is None:
            raise MeterTypeNotFound
        now = _seconds(clock())
        changed = catalog.apply_changes(row, changes=changes, now=now, session=session)
        if changed:
            before, after = _change_states(changed)
            record_audit(
                session,
                action=AuditAction.USAGE_METER_TYPE_UPDATE,
                context=context,
                now=now,
                actor=actor,
                entity_type=ENTITY_METER_TYPE,
                entity_id=row.public_id,
                before_state=before,
                after_state=after,
            )
        view = _meter_type_view(session, row)
    return view


# --- 供应商 -----------------------------------------------------------------------


def _require_provider(session: Session, provider_id: str) -> AiProvider:
    provider = catalog.get_provider(session, provider_id)
    if provider is None:
        raise ProviderNotFound
    return provider


def _lock_provider(session: Session, provider_id: str) -> AiProvider:
    provider = catalog.lock_provider(session, provider_id)
    if provider is None:
        raise ProviderNotFound
    return provider


def create_provider(
    session_factory: sessionmaker[Session],
    *,
    actor: User,
    code: str,
    display_name: str,
    context: RequestContext,
    clock: Clock = utc_now,
) -> ProviderView:
    """Provider and `AI_PROVIDER_CREATE` in one commit. Taken code: 409, no writes."""
    with session_scope(session_factory) as session:
        now = _seconds(clock())
        if catalog.provider_code_exists(session, code):
            raise ProviderCodeTaken
        try:
            row = catalog.insert_provider(session, code=code, display_name=display_name, now=now)
        except IntegrityError:
            raise ProviderCodeTaken from None
        record_audit(
            session,
            action=AuditAction.AI_PROVIDER_CREATE,
            context=context,
            now=now,
            actor=actor,
            entity_type=ENTITY_PROVIDER,
            entity_id=row.public_id,
            after_state={
                "code": row.code,
                "display_name": row.display_name,
                "status": CatalogStatus(row.status).value,
            },
        )
        view = provider_view(row)
    return view


def get_provider(session_factory: sessionmaker[Session], provider_id: str) -> ProviderView:
    with session_factory() as session:
        return provider_view(_require_provider(session, provider_id))


def list_providers(
    session_factory: sessionmaker[Session],
    *,
    status: CatalogStatus | None,
    page: int,
    page_size: int,
) -> Page[ProviderView]:
    """`code` ascending."""
    with session_factory() as session:
        rows, total = catalog.list_providers(
            session, status=status, offset=_offset(page, page_size), limit=page_size
        )
        return Page[ProviderView](
            items=[provider_view(row) for row in rows],
            page=page,
            page_size=page_size,
            total=total,
        )


def update_provider(
    session_factory: sessionmaker[Session],
    *,
    actor: User,
    provider_id: str,
    changes: Mapping[str, object],
    context: RequestContext,
    clock: Clock = utc_now,
) -> ProviderView:
    """Rename or change the status. Nothing really changed: 200, no audit, no write.

    停用不影响解析与计费（设计 §2「停用的含义」）。
    """
    with session_scope(session_factory) as session:
        row = _lock_provider(session, provider_id)
        now = _seconds(clock())
        changed = catalog.apply_changes(row, changes=changes, now=now, session=session)
        if changed:
            before, after = _change_states(changed)
            record_audit(
                session,
                action=AuditAction.AI_PROVIDER_UPDATE,
                context=context,
                now=now,
                actor=actor,
                entity_type=ENTITY_PROVIDER,
                entity_id=row.public_id,
                before_state=before,
                after_state=after,
            )
        view = provider_view(row)
    return view


# --- 模型 -------------------------------------------------------------------------


def _require_model(
    session: Session, provider: AiProvider, model_id: str, *, for_update: bool = False
) -> AiModel:
    model = catalog.get_model(session, provider.id, model_id, for_update=for_update)
    if model is None:
        raise ModelNotFound
    return model


def create_model(
    session_factory: sessionmaker[Session],
    *,
    actor: User,
    provider_id: str,
    code: str,
    display_name: str,
    context: RequestContext,
    clock: Clock = utc_now,
) -> ModelView:
    """Model and `AI_MODEL_CREATE` in one commit, inside the provider row lock.

    同供应商下已有同名模型，**或该字符串在别名表里出现过**（不论哪一段、是否已截断）：
    409 `AI_MODEL_CODE_TAKEN`，不写。否则解析顺序会让一个曾经解析到别名目标的字符串
    改解析到同名模型（设计 §2「模型代码与别名不能撞」）。
    """
    with session_scope(session_factory) as session:
        provider = _lock_provider(session, provider_id)
        now = _seconds(clock())
        if catalog.model_code_exists(session, provider.id, code):
            raise ModelCodeTaken
        if catalog.alias_ever_used(session, provider.id, code):
            raise ModelCodeTaken
        try:
            row = catalog.insert_model(
                session, provider_id=provider.id, code=code, display_name=display_name, now=now
            )
        except IntegrityError:
            raise ModelCodeTaken from None
        record_audit(
            session,
            action=AuditAction.AI_MODEL_CREATE,
            context=context,
            now=now,
            actor=actor,
            entity_type=ENTITY_MODEL,
            entity_id=row.public_id,
            after_state={
                "provider_code": provider.code,
                "code": row.code,
                "display_name": row.display_name,
                "status": CatalogStatus(row.status).value,
            },
        )
        view = model_view(row, provider)
    return view


def get_model(
    session_factory: sessionmaker[Session], *, provider_id: str, model_id: str
) -> ModelDetail:
    """The model and the untruncated segments that point at it."""
    with session_factory() as session:
        provider = _require_provider(session, provider_id)
        model = _require_model(session, provider, model_id)
        segments = [
            alias_segment_view(segment, model_id=model.public_id, model_code=model.code)
            for segment in catalog.open_segments_for_model(session, model)
        ]
        return model_detail(model, provider, segments)


def list_models(
    session_factory: sessionmaker[Session],
    *,
    provider_id: str,
    status: CatalogStatus | None,
    page: int,
    page_size: int,
) -> Page[ModelView]:
    """Only this provider's models, `code` ascending."""
    with session_factory() as session:
        provider = _require_provider(session, provider_id)
        rows, total = catalog.list_models(
            session, provider.id, status=status, offset=_offset(page, page_size), limit=page_size
        )
        return Page[ModelView](
            items=[model_view(row, provider) for row in rows],
            page=page,
            page_size=page_size,
            total=total,
        )


def update_model(
    session_factory: sessionmaker[Session],
    *,
    actor: User,
    provider_id: str,
    model_id: str,
    changes: Mapping[str, object],
    context: RequestContext,
    clock: Clock = utc_now,
) -> ModelView:
    """Rename or change the status. Nothing really changed: 200, no audit, no write.

    停用的模型照样被解析（设计 §2「停用的含义」）。
    """
    with session_scope(session_factory) as session:
        provider = _require_provider(session, provider_id)
        row = _require_model(session, provider, model_id, for_update=True)
        now = _seconds(clock())
        changed = catalog.apply_changes(row, changes=changes, now=now, session=session)
        if changed:
            before, after = _change_states(changed)
            record_audit(
                session,
                action=AuditAction.AI_MODEL_UPDATE,
                context=context,
                now=now,
                actor=actor,
                entity_type=ENTITY_MODEL,
                entity_id=row.public_id,
                before_state=before,
                after_state=after,
            )
        view = model_view(row, provider)
    return view


# --- 别名段 -----------------------------------------------------------------------


def _segment_views(session: Session, rows: list[AiModelAlias]) -> list[AliasSegmentView]:
    targets = catalog.model_codes(session, [row.model_id for row in rows])
    return [
        alias_segment_view(
            row, model_id=targets[row.model_id][0], model_code=targets[row.model_id][1]
        )
        for row in rows
    ]


def verify_segments(rows: list[AiModelAlias]) -> None:
    """Raise `AliasSegmentsBroken` unless the segments of one string chain end to end.

    排序后：只有第一段 `effective_from` 为空；相邻两段前一段的 `effective_to` 等于后一段
    的 `effective_from`（于是只有最后一段可以未截断）；每段起点早于终点。
    """
    ordered = sorted(rows, key=catalog.segment_order)
    if not ordered:
        return
    if ordered[0].effective_from is not None:
        raise AliasSegmentsBroken("the first segment must start at the beginning of time")
    for earlier, later in pairwise(ordered):
        if later.effective_from is None:
            raise AliasSegmentsBroken("only the first segment may start at the beginning")
        if earlier.effective_to != later.effective_from:
            raise AliasSegmentsBroken("adjacent segments must meet end to end")
    for row in ordered:
        start, end = row.effective_from, row.effective_to
        if start is not None and end is not None and not start < end:
            raise AliasSegmentsBroken("a segment must start before it ends")


def _recheck(session: Session, provider_id: int, alias: str) -> None:
    # 重新加锁读一遍库里的段（刚 flush 的也在），不信内存里的推算。
    verify_segments(catalog.segments_for(session, provider_id, alias))


def map_alias(
    session_factory: sessionmaker[Session],
    *,
    actor: User,
    provider_id: str,
    alias: str,
    model_id: str,
    context: RequestContext,
    clock: Clock = utc_now,
) -> MappedAlias:
    """Map the string to a model of the same provider, from now on (design §2「三个操作」).

    - 从没有过段：插入 `[NULL, NULL) → M`，对过去全部生效（过去一律是「未知」）；
    - 未截断的段已指向 M：什么都不写，`created=False`；
    - 未截断的段指向别的模型：截断于 `t`，插入 `[t, NULL) → M`；
    - 所有段都已截断（撤销过）：插入 `[最后一段的 effective_to, NULL) → M`，补上空档。

    `model_id` 不是这个供应商的模型：404 `AI_MODEL_NOT_FOUND`。字符串是同供应商下某个
    模型的 `code`：409 `AI_MODEL_ALIAS_TAKEN`。都不写。
    """
    with session_scope(session_factory) as session:
        provider = _lock_provider(session, provider_id)
        # ⚠️ 拿到锁之后才取时间：`t` 必须晚于任何先于它持锁的解析（设计 §2「时间」）。
        now = clock()
        model = _require_model(session, provider, model_id, for_update=True)
        if catalog.model_code_exists(session, provider.id, alias):
            raise AliasTaken
        segments = catalog.segments_for(session, provider.id, alias)
        current = next((row for row in segments if row.effective_to is None), None)
        if current is not None and current.model_id == model.id:
            # 已指向 M：200，不写段、不写审计。
            view = alias_segment_view(current, model_id=model.public_id, model_code=model.code)
            return MappedAlias(segment=view, created=False)

        stamp = _seconds(now)
        before: dict[str, object] | None = None
        effective_from: dt.datetime | None
        if current is not None:
            [(_, previous_code)] = catalog.model_codes(session, [current.model_id]).values()
            effective_from = _cut_point(now, current)
            catalog.truncate_segment(session, current, at=effective_from, now=stamp)
            before = {"model_code": previous_code, "effective_to": None}
        elif segments:
            # 撤销过：从最后一段的终点接上，空档里的事件一直是「未知」、没扣过钱。
            effective_from = segments[-1].effective_to
        else:
            effective_from = None
        row = catalog.insert_segment(
            session,
            provider_id=provider.id,
            model_id=model.id,
            alias=alias,
            effective_from=effective_from,
            now=stamp,
        )
        _recheck(session, provider.id, alias)
        record_audit(
            session,
            action=AuditAction.AI_MODEL_ALIAS_MAP,
            context=context,
            now=stamp,
            actor=actor,
            entity_type=ENTITY_ALIAS,
            entity_id=row.public_id,
            before_state=before,
            after_state={
                "provider_code": provider.code,
                "alias": row.alias,
                "model_code": model.code,
                "effective_from": _text(row.effective_from),
            },
        )
        view = alias_segment_view(row, model_id=model.public_id, model_code=model.code)
    return MappedAlias(segment=view, created=True)


def retire_alias(
    session_factory: sessionmaker[Session],
    *,
    actor: User,
    provider_id: str,
    alias_id: str,
    context: RequestContext,
    clock: Clock = utc_now,
) -> AliasSegmentView:
    """Truncate the untruncated segment at `t`; from `t` on the string is unknown again.

    `alias_id` 必须是这个供应商下当前未截断的那一段，否则 404
    `AI_MODEL_ALIAS_NOT_FOUND`，不写。
    """
    with session_scope(session_factory) as session:
        provider = _lock_provider(session, provider_id)
        # ⚠️ 拿到锁之后才取时间（同 `map_alias`）。
        now = clock()
        row = catalog.get_segment(session, provider.id, alias_id, for_update=True)
        if row is None or row.effective_to is not None:
            raise AliasNotFound
        at = _cut_point(now, row)
        stamp = _seconds(now)
        catalog.truncate_segment(session, row, at=at, now=stamp)
        _recheck(session, provider.id, row.alias)
        record_audit(
            session,
            action=AuditAction.AI_MODEL_ALIAS_RETIRE,
            context=context,
            now=stamp,
            actor=actor,
            entity_type=ENTITY_ALIAS,
            entity_id=row.public_id,
            before_state={"effective_to": None},
            after_state={"effective_to": _text(at)},
        )
        [view] = _segment_views(session, [row])
    return view


def list_aliases(
    session_factory: sessionmaker[Session],
    *,
    provider_id: str,
    alias: str | None,
    current: bool,
    page: int,
    page_size: int,
) -> Page[AliasSegmentView]:
    """Every segment of the provider (the whole history), by `alias` then `effective_from`.

    `current=True` 只列未截断的段。
    """
    with session_factory() as session:
        provider = _require_provider(session, provider_id)
        rows, total = catalog.list_segments(
            session,
            provider.id,
            alias=alias,
            open_only=current,
            offset=_offset(page, page_size),
            limit=page_size,
        )
        return Page[AliasSegmentView](
            items=_segment_views(session, rows),
            page=page,
            page_size=page_size,
            total=total,
        )


__all__ = [
    "ENTITY_ALIAS",
    "ENTITY_METER_TYPE",
    "ENTITY_MODEL",
    "ENTITY_PROVIDER",
    "AliasNotFound",
    "AliasSegmentsBroken",
    "AliasTaken",
    "Clock",
    "ComponentCodeTaken",
    "MappedAlias",
    "MeterTypeCodeTaken",
    "MeterTypeNotFound",
    "ModelCodeTaken",
    "ModelNotFound",
    "ProviderCodeTaken",
    "ProviderNotFound",
    "boundary_after",
    "create_meter_type",
    "create_model",
    "create_provider",
    "get_meter_type",
    "get_model",
    "get_provider",
    "list_aliases",
    "list_meter_types",
    "list_models",
    "list_providers",
    "map_alias",
    "retire_alias",
    "update_meter_type",
    "update_model",
    "update_provider",
    "verify_segments",
]

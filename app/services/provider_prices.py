"""Provider price versions: drafts, publishing and retiring (design gate #177 v3, AIH-TASK-026).

⚠️ **每个写操作一个 `session_scope()`**（INV-13）：版本、分量、被截断或恢复的前一个版本、
复查与审计同一事务，要么都在、要么都不在。repository 只 flush。不写 outbox（设计 §2
「外部系统与异步边界」）。

⚠️ **发布与退役先 `SELECT … FOR UPDATE` 锁供应商行**（与 025 改映射同一把锁），之后的读、
校验与写入都在锁内；边界时刻 `t` 在**拿到锁之后**才取当前时间，向上取整到下一个整秒
（`boundary_after`）。计费侧在同一行上持共享锁、只处理 `occurred_at ≤` 持锁后当前时间的事件，
所以一个事件一旦取到某个版本，以后永远取到同一个版本（设计 §2「为什么与计费不会交错」）。
建草稿、改草稿、丢弃不锁供应商行（草稿不参与计费），只锁版本行。

⚠️ **只在末尾追加**：新版本的 `effective_from` 永远 ≥ `t`（该（供应商, 模型）第一个版本未指定
时刻时的「一直以来」除外）；退役留下的空档不回填。每次发布、退役写完后，在同一事务里加锁
重读该（供应商, 模型）的全部已发布版本复查区间，不满足就抛错、整体回滚。

⚠️ 审计的前后状态里，供应商、模型、分量一律用 `code`，版本用 `public_id`，人用邮箱；不用
内部 id（设计 §2「审计」）。这一层不写日志：应用日志不打印分量明细（设计 §6）。
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Callable, Sequence
from decimal import Decimal
from itertools import pairwise
from typing import Final

from sqlalchemy.orm import Session, sessionmaker

from app.core.database import session_scope
from app.core.errors import AppError
from app.models.ai_catalog import (
    AiModel,
    AiProvider,
    CatalogStatus,
    UsageMeterComponent,
    UsageMeterType,
)
from app.models.auth import AuditAction, User
from app.models.provider_prices import PriceSourceType, PriceVersionStatus, ProviderPriceVersion
from app.repositories import ai_catalog as catalog
from app.repositories import provider_prices as prices
from app.repositories.provider_prices import NewComponent, PricedComponent
from app.schemas.customers import Page, money_text
from app.schemas.provider_prices import ComponentInput, PriceVersionView, price_version_view
from app.services.ai_catalog import ModelNotFound, ProviderNotFound, boundary_after
from app.services.auth import RequestContext, record_audit, utc_now

# 审计的 entity_type；entity_id 是版本的 public_id。
ENTITY_PRICE_VERSION: Final = "provider_price_version"

# 取当前时间（不带时区的 UTC）。可注入，测试据此冻结时钟。
type Clock = Callable[[], dt.datetime]

_FINAL_STATUSES: Final = (PriceVersionStatus.RETIRED, PriceVersionStatus.DISCARDED)


class PriceVersionNotFound(AppError):
    def __init__(self) -> None:
        super().__init__(
            "Provider price version not found.",
            code="PRICE_VERSION_NOT_FOUND",
            http_status=404,
        )


class MeterComponentNotFound(AppError):
    """A `component_code` that is not a priced component of the catalog."""

    def __init__(self) -> None:
        super().__init__(
            "Usage meter component not found.",
            code="USAGE_METER_COMPONENT_NOT_FOUND",
            http_status=404,
        )


class PriceVersionNotDraft(AppError):
    """Editing or discarding a published version."""

    def __init__(self) -> None:
        super().__init__(
            "Only a draft provider price version can be changed or discarded.",
            code="PRICE_VERSION_NOT_DRAFT",
            http_status=409,
        )


class PriceVersionIncomplete(AppError):
    """Publishing without components, or without every component of a meter type it prices."""

    def __init__(self, missing: Sequence[str]) -> None:
        # 缺的是目录里的分量代码，不是秘密，照列（设计 §2「错误」：列出缺的分量）。
        if missing:
            message = "The provider price version misses components: " + ", ".join(missing) + "."
        else:
            message = "The provider price version has no components."
        super().__init__(message, code="PRICE_VERSION_INCOMPLETE", http_status=409)
        self.missing = tuple(missing)


class CatalogItemRetired(AppError):
    """The provider, the model, or a meter type of a component is RETIRED (025 contract)."""

    def __init__(self) -> None:
        super().__init__(
            "The provider, the model or a meter type of a component is retired.",
            code="CATALOG_ITEM_RETIRED",
            http_status=409,
        )


class PriceVersionNotRetirable(AppError):
    """Retiring a draft, or a version whose period a successor has already ended."""

    def __init__(self) -> None:
        super().__init__(
            "Only the current, untruncated published provider price version can be retired.",
            code="PRICE_VERSION_NOT_RETIRABLE",
            http_status=409,
        )


class PriceVersionFinal(AppError):
    """Anything on a retired or discarded version."""

    def __init__(self) -> None:
        super().__init__(
            "The provider price version is retired or discarded.",
            code="PRICE_VERSION_FINAL",
            http_status=409,
        )


class EffectiveFromConflict(AppError):
    """The start is not after the last version's start, or before its end."""

    def __init__(self) -> None:
        super().__init__(
            "The effective time is before the start or the end of the latest version.",
            code="EFFECTIVE_FROM_CONFLICT",
            http_status=409,
        )


class EffectiveFromInPast(AppError):
    """No backdating: a requested start must not be before `t`."""

    def __init__(self) -> None:
        super().__init__(
            "The effective time is in the past.",
            code="EFFECTIVE_FROM_IN_PAST",
            http_status=422,
        )


class PricePeriodsBroken(RuntimeError):
    """The in-transaction recheck failed: a bug, never a user error. 500, rolled back."""


def _seconds(moment: dt.datetime) -> dt.datetime:
    # MySQL 的 DATETIME 不存小数秒：与 app/services/ai_catalog.py 同一理由。
    return moment.replace(microsecond=0)


def _text(moment: dt.datetime | None) -> str | None:
    return moment.isoformat() if moment is not None else None


def _offset(page: int, page_size: int) -> int:
    return (page - 1) * page_size


def _status(row: ProviderPriceVersion) -> PriceVersionStatus:
    return PriceVersionStatus(row.status)


# --- 视图与审计内容 -------------------------------------------------------------------


def _views(session: Session, rows: Sequence[ProviderPriceVersion]) -> list[PriceVersionView]:
    providers, models = prices.providers_and_models(session, rows)
    components = prices.components_for(session, [row.id for row in rows])
    emails = prices.user_emails(
        session, [user for row in rows for user in (row.created_by, row.approved_by)]
    )
    views = []
    for row in rows:
        provider, model = providers[row.provider_id], models[row.model_id]
        views.append(
            price_version_view(
                row,
                provider=(provider.public_id, provider.code),
                model=(model.public_id, model.code),
                components=components[row.id],
                emails=emails,
            )
        )
    return views


def _view(session: Session, row: ProviderPriceVersion) -> PriceVersionView:
    [view] = _views(session, [row])
    return view


def _stored_components(priced: Sequence[PricedComponent]) -> list[dict[str, str]]:
    """Audit form of the stored components: code and the two amounts, `component_code` order."""
    return [
        {
            "component_code": item.component.component_code,
            "unit_quantity": money_text(item.row.unit_quantity),
            "rate_amount": money_text(item.row.rate_amount),
        }
        for item in priced
    ]


def _requested_components(components: Sequence[ComponentInput]) -> list[dict[str, str]]:
    """Audit form of the requested components, in the same order and format as the stored."""
    return [
        {
            "component_code": item.component_code,
            "unit_quantity": money_text(item.unit_quantity),
            "rate_amount": money_text(item.rate_amount),
        }
        for item in sorted(components, key=lambda item: item.component_code)
    ]


def _signature(
    code: str, unit_quantity: Decimal, rate_amount: Decimal, metadata: object
) -> tuple[str, Decimal, Decimal, str]:
    # Decimal 按数值比较：`1.5` 与 `1.50000000` 相同。备注按规范化的 JSON 文本比较。
    return code, unit_quantity, rate_amount, json.dumps(metadata, sort_keys=True)


def _same_components(
    stored: Sequence[PricedComponent], requested: Sequence[ComponentInput]
) -> bool:
    """Whether a replacement would change nothing: same codes, amounts and metadata."""
    before = sorted(
        _signature(
            item.component.component_code,
            item.row.unit_quantity,
            item.row.rate_amount,
            item.row.metadata_json,
        )
        for item in stored
    )
    after = sorted(
        _signature(item.component_code, item.unit_quantity, item.rate_amount, item.metadata)
        for item in requested
    )
    return before == after


# --- 目录校验 ---------------------------------------------------------------------


def _catalog_components(
    session: Session, components: Sequence[ComponentInput]
) -> list[tuple[ComponentInput, UsageMeterComponent, UsageMeterType]]:
    """Each requested component with its catalog component and meter type; unknown: 404."""
    found = prices.meter_components_by_code(session, [item.component_code for item in components])
    resolved = []
    for item in components:
        if item.component_code not in found:
            raise MeterComponentNotFound
        component, kind = found[item.component_code]
        resolved.append((item, component, kind))
    return resolved


def _require_active(
    provider: AiProvider, model: AiModel, meter_types: Sequence[UsageMeterType]
) -> None:
    """025 contract: no new or published price on a RETIRED provider, model or meter type."""
    statuses = [provider.status, model.status, *(kind.status for kind in meter_types)]
    if any(CatalogStatus(status) is CatalogStatus.RETIRED for status in statuses):
        raise CatalogItemRetired


def missing_components(session: Session, priced: Sequence[PricedComponent]) -> list[str]:
    """The catalog components the version lacks for the meter types it prices, sorted.

    凡是出现的计量类型，它在 025 里的全部分量都必须出现（§15.1：缺价不按 0 算）；整体没出现
    的计量类型不要求。
    """
    present = {item.component.id for item in priced}
    required = catalog.components_for(session, [item.component.meter_type_id for item in priced])
    return sorted(
        component.component_code
        for components in required.values()
        for component in components
        if component.id not in present
    )


def _new_components(
    resolved: Sequence[tuple[ComponentInput, UsageMeterComponent, UsageMeterType]],
) -> list[NewComponent]:
    return [
        NewComponent(
            usage_meter_component_id=component.id,
            unit_quantity=item.unit_quantity,
            rate_amount=item.rate_amount,
            metadata=item.metadata,
        )
        for item, component, _ in resolved
    ]


# --- 区间 -------------------------------------------------------------------------


def verify_periods(rows: Sequence[ProviderPriceVersion]) -> None:
    """Raise `PricePeriodsBroken` unless the published versions chain in time.

    设计 §2「事务内复查」：按 `effective_from` 排序，只有第一个的起点可为空；相邻两个，前一个
    未截断时它必须是最后一个，截断时终点 ≤ 后一个的起点（等号 = 首尾相接，小于 = 退役留下的
    空档）；每个区间起点早于终点。

    ⚠️ 空区间 `[F, F)`（撤销的预约）不参与排序与相邻比较：它永不匹配，也不参与数据库的重叠
    判定。不排除的话，撤销预约之后恢复为未截断的前一个版本排在它前面，会被误判成「未截断却
    不是最后一个」。空区间只允许出现在 `RETIRED` 的行上（与 CHECK 一致）。
    """
    for row in rows:
        if prices.is_empty_period(row) and _status(row) is not PriceVersionStatus.RETIRED:
            raise PricePeriodsBroken("only a withdrawn reservation may have an empty period")
        if _status(row) is PriceVersionStatus.RETIRED and row.effective_to is None:
            raise PricePeriodsBroken("a retired version must have an end")
    ordered = sorted(
        (row for row in rows if not prices.is_empty_period(row)), key=prices.version_order
    )
    for row in ordered:
        start, end = row.effective_from, row.effective_to
        if start is not None and end is not None and not start < end:
            raise PricePeriodsBroken("a period must start before it ends")
    for earlier, later in pairwise(ordered):
        if later.effective_from is None:
            raise PricePeriodsBroken("only the first version may start at the beginning")
        if earlier.effective_to is None:
            raise PricePeriodsBroken("only the last version may be open")
        if earlier.effective_to > later.effective_from:
            raise PricePeriodsBroken("periods overlap")


def _recheck(session: Session, provider_id: int, model_id: int) -> None:
    # 重新加锁读一遍库里的版本（刚 flush 的也在），不信内存里的推算。
    verify_periods(prices.period_versions(session, provider_id, model_id))


def _timeline(periods: Sequence[ProviderPriceVersion]) -> list[ProviderPriceVersion]:
    """The non-empty published versions in time order: the ones events can ever match."""
    return [row for row in periods if not prices.is_empty_period(row)]


def _placement(
    tail: ProviderPriceVersion | None, *, requested: dt.datetime | None, t: dt.datetime
) -> tuple[dt.datetime | None, bool]:
    """Where a new version starts, and whether the tail must be truncated there (设计 §2「发布」).

    `tail` 是末尾版本 L：非空的已发布版本里 `effective_from` 最晚的一个。
    """
    if tail is None:
        # 第一个版本：未指定 = 一直以来（此前的事件全是 PRICING_ERROR，只会把无价变有价）。
        return requested, False
    if tail.effective_to is None:
        start = requested if requested is not None else t
        # 只在末尾追加：起点必须晚于 L 的起点。L 是尚未开始的预约时，不指定时刻的发布（从 `t`
        # 起）也落在它前面，同样拒绝。
        if tail.effective_from is not None and start <= tail.effective_from:
            raise EffectiveFromConflict
        return start, True
    # L 已被截断或已退役，尽头 E：从 max(E, 所请求或 t) 起，空档保持无价（不回填）。
    end = tail.effective_to
    if requested is not None and requested < end:
        raise EffectiveFromConflict
    return max(end, requested if requested is not None else t), False


# --- 草稿 -------------------------------------------------------------------------


def _require_version(
    session: Session, price_version_id: str, *, for_update: bool = False
) -> ProviderPriceVersion:
    row = prices.get_version(session, price_version_id, for_update=for_update)
    if row is None:
        raise PriceVersionNotFound
    return row


def _require_draft(row: ProviderPriceVersion) -> None:
    status = _status(row)
    if status in _FINAL_STATUSES:
        raise PriceVersionFinal
    if status is not PriceVersionStatus.DRAFT:
        raise PriceVersionNotDraft


def create_draft(
    session_factory: sessionmaker[Session],
    *,
    actor: User,
    provider_id: str,
    model_id: str,
    source_currency: str,
    source_reference: str,
    components: Sequence[ComponentInput],
    context: RequestContext,
    clock: Clock = utc_now,
) -> PriceVersionView:
    """A draft, its components and `PROVIDER_PRICE_CREATE` in one commit.

    供应商不存在 404 `AI_PROVIDER_NOT_FOUND`；模型不存在或不属于它 404 `AI_MODEL_NOT_FOUND`；
    分量代码不在目录里 404 `USAGE_METER_COMPONENT_NOT_FOUND`；供应商、模型或分量所属计量类型
    已停用 409 `CATALOG_ITEM_RETIRED`。都不写。草稿可以不完整：完整性在发布时校验。
    """
    with session_scope(session_factory) as session:
        now = _seconds(clock())
        provider = catalog.get_provider(session, provider_id)
        if provider is None:
            raise ProviderNotFound
        model = catalog.get_model(session, provider.id, model_id)
        if model is None:
            raise ModelNotFound
        resolved = _catalog_components(session, components)
        _require_active(provider, model, [kind for _, _, kind in resolved])
        row = prices.insert_draft(
            session,
            provider_id=provider.id,
            model_id=model.id,
            source_currency=source_currency,
            source_reference=source_reference,
            created_by=actor.id,
            now=now,
        )
        prices.insert_components(session, row, _new_components(resolved), now=now)
        record_audit(
            session,
            action=AuditAction.PROVIDER_PRICE_CREATE,
            context=context,
            now=now,
            actor=actor,
            entity_type=ENTITY_PRICE_VERSION,
            entity_id=row.public_id,
            after_state={
                "provider_code": provider.code,
                "model_code": model.code,
                "source_currency": row.source_currency,
                "source_type": PriceSourceType(row.source_type).value,
                "source_reference": row.source_reference,
                "status": _status(row).value,
                "components": _requested_components(components),
            },
        )
        view = _view(session, row)
    return view


def update_draft(
    session_factory: sessionmaker[Session],
    *,
    actor: User,
    price_version_id: str,
    source_currency: str | None,
    source_reference: str | None,
    components: Sequence[ComponentInput] | None,
    context: RequestContext,
    clock: Clock = utc_now,
) -> PriceVersionView:
    """Change a draft; `None` = not in the request. `components` replaces the whole set.

    非草稿：已发布 409 `PRICE_VERSION_NOT_DRAFT`，已退役 / 已丢弃 409 `PRICE_VERSION_FINAL`。
    目录校验同建草稿。**没有实际变化**：200，什么都不写（`updated_at` 不变、不写审计）。
    """
    with session_scope(session_factory) as session:
        row = _require_version(session, price_version_id, for_update=True)
        _require_draft(row)
        now = _seconds(clock())
        provider = prices.get_provider(session, row.provider_id)
        model = prices.get_model(session, row.model_id)
        stored = prices.components_for(session, [row.id])[row.id]
        resolved = None if components is None else _catalog_components(session, components)
        if resolved is None:
            meter_types = [item.meter_type for item in stored]
        else:
            meter_types = [kind for _, _, kind in resolved]
        _require_active(provider, model, meter_types)

        before: dict[str, object] = {}
        after: dict[str, object] = {}
        for name, value in (
            ("source_currency", source_currency),
            ("source_reference", source_reference),
        ):
            if value is not None and value != getattr(row, name):
                before[name], after[name] = getattr(row, name), value
        replace = components is not None and not _same_components(stored, components)
        if replace and components is not None:
            before["components"] = _stored_components(stored)
            after["components"] = _requested_components(components)
        if not after:
            return _view(session, row)

        prices.save_draft(
            session,
            row,
            source_currency=source_currency or row.source_currency,
            source_reference=source_reference or row.source_reference,
            now=now,
        )
        if replace and resolved is not None:
            prices.drop_draft_components(session, row)
            prices.insert_components(session, row, _new_components(resolved), now=now)
        record_audit(
            session,
            action=AuditAction.PROVIDER_PRICE_UPDATE,
            context=context,
            now=now,
            actor=actor,
            entity_type=ENTITY_PRICE_VERSION,
            entity_id=row.public_id,
            before_state=before,
            after_state=after,
        )
        view = _view(session, row)
    return view


def discard_draft(
    session_factory: sessionmaker[Session],
    *,
    actor: User,
    price_version_id: str,
    context: RequestContext,
    clock: Clock = utc_now,
) -> PriceVersionView:
    """DRAFT → DISCARDED and `PROVIDER_PRICE_DISCARD` in one commit. The row stays."""
    with session_scope(session_factory) as session:
        row = _require_version(session, price_version_id, for_update=True)
        _require_draft(row)
        now = _seconds(clock())
        prices.mark_discarded(session, row, now=now)
        record_audit(
            session,
            action=AuditAction.PROVIDER_PRICE_DISCARD,
            context=context,
            now=now,
            actor=actor,
            entity_type=ENTITY_PRICE_VERSION,
            entity_id=row.public_id,
            before_state={"status": PriceVersionStatus.DRAFT.value},
            after_state={"status": PriceVersionStatus.DISCARDED.value},
        )
        view = _view(session, row)
    return view


# --- 发布与退役 ---------------------------------------------------------------------


def publish(
    session_factory: sessionmaker[Session],
    *,
    actor: User,
    price_version_id: str,
    effective_from: dt.datetime | None,
    context: RequestContext,
    clock: Clock = utc_now,
) -> PriceVersionView:
    """DRAFT → PUBLISHED inside the provider row lock (设计 §2「发布：区间怎么定」).

    `effective_from` 是不带时区的 UTC 整秒，或 `None`。已发布：200，什么都不写（幂等）。
    已退役 / 已丢弃：409 `PRICE_VERSION_FINAL`。目录已停用：409 `CATALOG_ITEM_RETIRED`。
    不完整：409 `PRICE_VERSION_INCOMPLETE`。请求的时刻早于 `t`：422 `EFFECTIVE_FROM_IN_PAST`；
    不晚于末尾版本的起点、或早于它的尽头：409 `EFFECTIVE_FROM_CONFLICT`。都不写。
    """
    with session_scope(session_factory) as session:
        # 版本挂在哪个供应商上建后不可改，所以先用普通读找到它、再锁供应商行是安全的。
        found = _require_version(session, price_version_id)
        provider = prices.lock_provider(session, found.provider_id)
        # ⚠️ 拿到锁之后才取时间：`t` 必须晚于任何先于它持锁的计费（设计 §2）。
        now = clock()
        row = _require_version(session, price_version_id, for_update=True)
        status = _status(row)
        if status is PriceVersionStatus.PUBLISHED:
            return _view(session, row)
        if status in _FINAL_STATUSES:
            raise PriceVersionFinal
        model = prices.get_model(session, row.model_id)
        priced = prices.components_for(session, [row.id])[row.id]
        _require_active(provider, model, [item.meter_type for item in priced])
        missing = missing_components(session, priced)
        if not priced or missing:
            raise PriceVersionIncomplete(missing)
        t = boundary_after(now)
        if effective_from is not None and effective_from < t:
            raise EffectiveFromInPast

        timeline = _timeline(prices.period_versions(session, row.provider_id, row.model_id))
        tail = timeline[-1] if timeline else None
        start, cut = _placement(tail, requested=effective_from, t=t)
        stamp = _seconds(now)
        before: dict[str, object] = {"status": PriceVersionStatus.DRAFT.value}
        after: dict[str, object] = {
            "status": PriceVersionStatus.PUBLISHED.value,
            "effective_from": _text(start),
            "effective_to": None,
            "approved_by_email": actor.email,
            "approved_at": _text(stamp),
        }
        # ⚠️ 写入顺序（设计 §2）：先截断前一个，再把本行从草稿改为已发布。
        if cut and tail is not None and start is not None:
            prices.truncate(session, tail, at=start, now=stamp)
            before["truncated_version"] = {"id": tail.public_id, "effective_to": None}
            after["truncated_version"] = {"id": tail.public_id, "effective_to": _text(start)}
        prices.mark_published(session, row, effective_from=start, approved_by=actor.id, now=stamp)
        _recheck(session, row.provider_id, row.model_id)
        record_audit(
            session,
            action=AuditAction.PROVIDER_PRICE_PUBLISH,
            context=context,
            now=stamp,
            actor=actor,
            entity_type=ENTITY_PRICE_VERSION,
            entity_id=row.public_id,
            before_state=before,
            after_state=after,
        )
        view = _view(session, row)
    return view


def retire(
    session_factory: sessionmaker[Session],
    *,
    actor: User,
    price_version_id: str,
    reason: str,
    context: RequestContext,
    clock: Clock = utc_now,
) -> PriceVersionView:
    """PUBLISHED → RETIRED inside the provider row lock (设计 §2「退役」).

    - 已开始生效（起点为空或 ≤ `t`）、未截断：终点写 `t`，`t` 起的事件无价；
    - 尚未开始的预约（起点 > `t`）：终点写成起点（空区间，永不匹配）；前一个版本若正是被它
      截断的（仍 `PUBLISHED`、终点等于它的起点），恢复为未截断；
    - 已被后继截断的历史版本、草稿：409 `PRICE_VERSION_NOT_RETIRABLE`；
    - 已退役 / 已丢弃：409 `PRICE_VERSION_FINAL`。

    恢复前一个版本只影响 `occurred_at ≥ 本版本起点 > t` 的时刻：它们都还没有被计费。
    前一个版本若是**退役**结束于那一刻（撤销前它就不是被截断的），不恢复：退役不回填。
    """
    with session_scope(session_factory) as session:
        found = _require_version(session, price_version_id)
        prices.lock_provider(session, found.provider_id)
        # ⚠️ 拿到锁之后才取时间（同 `publish`）。
        now = clock()
        row = _require_version(session, price_version_id, for_update=True)
        status = _status(row)
        if status in _FINAL_STATUSES:
            raise PriceVersionFinal
        if status is not PriceVersionStatus.PUBLISHED or row.effective_to is not None:
            raise PriceVersionNotRetirable
        t = boundary_after(now)
        stamp = _seconds(now)
        timeline = _timeline(prices.period_versions(session, row.provider_id, row.model_id))
        start = row.effective_from
        before: dict[str, object] = {
            "status": PriceVersionStatus.PUBLISHED.value,
            "effective_from": _text(start),
            "effective_to": None,
        }
        after: dict[str, object] = {
            "status": PriceVersionStatus.RETIRED.value,
            "effective_from": _text(start),
        }
        if start is None or start <= t:
            end = t
            prices.mark_retired(session, row, effective_to=end, now=stamp)
        else:
            end = start
            key = prices.version_order(row)
            earlier = [item for item in timeline if prices.version_order(item) < key]
            previous = earlier[-1] if earlier else None
            # ⚠️ 写入顺序（设计 §2）：先把被撤销的一行改成空区间，再恢复前一个。
            prices.mark_retired(session, row, effective_to=end, now=stamp)
            if (
                previous is not None
                and _status(previous) is PriceVersionStatus.PUBLISHED
                and previous.effective_to == start
            ):
                prices.restore_open(session, previous, now=stamp)
                restored = previous.public_id
                before["restored_version"] = {"id": restored, "effective_to": _text(start)}
                after["restored_version"] = {"id": restored, "effective_to": None}
        after["effective_to"] = _text(end)
        _recheck(session, row.provider_id, row.model_id)
        record_audit(
            session,
            action=AuditAction.PROVIDER_PRICE_RETIRE,
            context=context,
            now=stamp,
            actor=actor,
            entity_type=ENTITY_PRICE_VERSION,
            entity_id=row.public_id,
            before_state=before,
            after_state=after,
            reason=reason,
        )
        view = _view(session, row)
    return view


# --- 查询 -------------------------------------------------------------------------


def get_price_version(
    session_factory: sessionmaker[Session], price_version_id: str
) -> PriceVersionView:
    with session_factory() as session:
        return _view(session, _require_version(session, price_version_id))


def list_price_versions(
    session_factory: sessionmaker[Session],
    *,
    provider_id: str | None,
    model_id: str | None,
    status: PriceVersionStatus | None,
    page: int,
    page_size: int,
) -> Page[PriceVersionView]:
    """By provider code, model code, then `effective_from`; each with its components.

    筛选里的供应商不存在 404 `AI_PROVIDER_NOT_FOUND`；模型不存在（或给了供应商、模型却不属于它）
    404 `AI_MODEL_NOT_FOUND`。
    """
    with session_factory() as session:
        provider_pk: int | None = None
        model_pk: int | None = None
        if provider_id is not None:
            provider = catalog.get_provider(session, provider_id)
            if provider is None:
                raise ProviderNotFound
            provider_pk = provider.id
        if model_id is not None:
            if provider_pk is not None:
                model = catalog.get_model(session, provider_pk, model_id)
            else:
                model = prices.find_model(session, model_id)
            if model is None:
                raise ModelNotFound
            model_pk = model.id
        rows, total = prices.list_versions(
            session,
            provider_id=provider_pk,
            model_id=model_pk,
            status=status,
            offset=_offset(page, page_size),
            limit=page_size,
        )
        return Page[PriceVersionView](
            items=_views(session, rows),
            page=page,
            page_size=page_size,
            total=total,
        )


__all__ = [
    "ENTITY_PRICE_VERSION",
    "CatalogItemRetired",
    "Clock",
    "EffectiveFromConflict",
    "EffectiveFromInPast",
    "MeterComponentNotFound",
    "PricePeriodsBroken",
    "PriceVersionFinal",
    "PriceVersionIncomplete",
    "PriceVersionNotDraft",
    "PriceVersionNotFound",
    "PriceVersionNotRetirable",
    "create_draft",
    "discard_draft",
    "get_price_version",
    "list_price_versions",
    "missing_components",
    "publish",
    "retire",
    "update_draft",
    "verify_periods",
]

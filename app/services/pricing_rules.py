"""Customer pricing rules: drafts, publishing and retiring (design gate #178 v3, AIH-TASK-027).

⚠️ **价格是 MYR 含税价**（ADR-0008）：FIXED_RATE 分量的 `rate_amount` 是含税单价，MARKUP 的
倍数乘出来的是含税计费额。本任务不做计费额计算、试算与税额（T-G / Phase 4）。

⚠️ **每个写操作一个 `session_scope()`**（INV-13）：规则、分量、被截断或恢复的前一条规则、
复查与审计同一事务，要么都在、要么都不在。repository 只 flush。不写 outbox（设计 §2
「外部系统与异步边界」）。

⚠️ **锁在前**：发布与停用的第一条语句是 `SELECT … FROM pricing_rule_locks WHERE id = 1 FOR
UPDATE`，之后的读全是加锁读、校验与写入都在锁内 —— 不读 REPEATABLE READ 在锁前建立的快照。
边界时刻 `t` 在**拿到锁之后**才取当前时间，向上取整到下一个整秒（`boundary_after`）。计费侧在
同一行上持共享锁、只处理 `occurred_at ≤` 持锁后当前时间的事件，所以一个事件一旦选到某条规则，
以后永远选到同一条（设计 §2「为什么与计费不会交错」）。建草稿、改草稿、丢弃
不拿这把锁（草稿不参与计费），只锁规则行。

⚠️ **按范围只在末尾追加**：新规则的 `effective_from` 永远 ≥ `t`，**唯一例外**是 `GLOBAL` 范围
从没有已发布规则、且未指定时刻时的「一直以来」（设计 §2「回溯生效只允许全局默认的第一条」：
其他任何一级补一条对过去生效的规则，都会让原本在更低一级选中规则、已经计过费的
时刻改选这一级）。停用留下的空档不回填。空区间 `[F, F)` 只在 `RETIRED` 行上、
不参与时间线。每次发布、停用写完后，在同一事务里加锁重读该范围的全部已发布规则
复查区间，不满足就抛错、整体回滚。

⚠️ 审计的前后状态里，客户用租户 `public_id`，供应商、模型、分量一律用 `code`，规则用
`public_id`，人用邮箱；不用内部 id（设计 §2「审计」）。这一层不写日志：应用日志不打印分量明细
（设计 §6）。
"""

from __future__ import annotations

import datetime as dt
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
from app.models.pricing_rules import PricingRule, PricingRuleStatus, PricingScope, PricingStrategy
from app.repositories import ai_catalog as catalog
from app.repositories import pricing_rules as rules
from app.repositories import tenancy
from app.repositories.pricing_rules import NewComponent, RuleComponent, Scope, ScopeRows
from app.schemas.customers import Page, money_text
from app.schemas.pricing_rules import (
    PricingRuleView,
    RuleComponentInput,
    pricing_rule_view,
    required_scope_fields,
)
from app.services.ai_catalog import ModelNotFound, ProviderNotFound, boundary_after
from app.services.auth import RequestContext, record_audit, utc_now
from app.services.customers import CustomerNotFound
from app.services.provider_prices import CatalogItemRetired, MeterComponentNotFound

# 审计的 entity_type；entity_id 是规则的 public_id。
ENTITY_PRICING_RULE: Final = "pricing_rule"

# 取当前时间（不带时区的 UTC）。可注入，测试据此冻结时钟。
type Clock = Callable[[], dt.datetime]

_FINAL_STATUSES: Final = (PricingRuleStatus.RETIRED, PricingRuleStatus.DISCARDED)


class PricingRuleNotFound(AppError):
    def __init__(self) -> None:
        super().__init__(
            "Pricing rule not found.",
            code="PRICING_RULE_NOT_FOUND",
            http_status=404,
        )


class PricingRuleShapeInvalid(AppError):
    """Scope fields that do not match the scope, or a strategy mixed with the other one's fields.

    请求本身合法、与规则现状合起来才矛盾时（例如给 FIXED_RATE 草稿只带倍数）也是它；与接口层的
    校验失败同一个 422 `VALIDATION_ERROR`。
    """

    def __init__(self, message: str) -> None:
        super().__init__(message, code="VALIDATION_ERROR", http_status=422)


class PricingRuleNotDraft(AppError):
    """Editing or discarding a published rule."""

    def __init__(self) -> None:
        super().__init__(
            "Only a draft pricing rule can be changed or discarded.",
            code="PRICING_RULE_NOT_DRAFT",
            http_status=409,
        )


class PricingRuleIncomplete(AppError):
    """Publishing a FIXED_RATE rule without components, or without every component of a
    meter type it prices."""

    def __init__(self, missing: Sequence[str]) -> None:
        # 缺的是目录里的分量代码，不是秘密，照列（设计 §2「完整性」：给出缺失清单）。
        if missing:
            message = "The pricing rule misses components: " + ", ".join(missing) + "."
        else:
            message = "The fixed rate pricing rule has no components."
        super().__init__(message, code="PRICING_RULE_INCOMPLETE", http_status=409)
        self.missing = tuple(missing)


class PricingRuleNotRetirable(AppError):
    """Retiring a draft, or a rule whose period a successor has already ended."""

    def __init__(self) -> None:
        super().__init__(
            "Only the current, untruncated published pricing rule can be retired.",
            code="PRICING_RULE_NOT_RETIRABLE",
            http_status=409,
        )


class PricingRuleFinal(AppError):
    """Anything on a retired or discarded rule."""

    def __init__(self) -> None:
        super().__init__(
            "The pricing rule is retired or discarded.",
            code="PRICING_RULE_FINAL",
            http_status=409,
        )


class EffectiveFromConflict(AppError):
    """The start is not after the scope's last rule's start, or before its end."""

    def __init__(self) -> None:
        super().__init__(
            "The effective time is before the start or the end of the latest rule of the scope.",
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


class PricingPeriodsBroken(RuntimeError):
    """The in-transaction recheck failed: a bug, never a user error. 500, rolled back."""


def _seconds(moment: dt.datetime) -> dt.datetime:
    # MySQL 的 DATETIME 不存小数秒：与 app/services/ai_catalog.py 同一理由。
    return moment.replace(microsecond=0)


def _text(moment: dt.datetime | None) -> str | None:
    return moment.isoformat() if moment is not None else None


def _amount(value: Decimal | None) -> str | None:
    return money_text(value) if value is not None else None


def _offset(page: int, page_size: int) -> int:
    return (page - 1) * page_size


def _status(row: PricingRule) -> PricingRuleStatus:
    return PricingRuleStatus(row.status)


def _strategy(row: PricingRule) -> PricingStrategy:
    return PricingStrategy(row.strategy)


# --- 视图与审计内容 -------------------------------------------------------------------


def _views(
    session: Session, rows: Sequence[PricingRule], *, lock: bool = False
) -> list[PricingRuleView]:
    # `lock`：发布、停用在规则锁之后的读一律加锁读，不读事务快照（见 `publish`）。
    found = rules.scope_rows(session, rows, lock=lock)
    components = rules.components_for(session, [row.id for row in rows], lock=lock)
    emails = rules.user_emails(
        session, [user for row in rows for user in (row.created_by, row.approved_by)], lock=lock
    )
    views = []
    for row in rows:
        tenant = found.tenants[row.tenant_id] if row.tenant_id is not None else None
        provider = found.providers[row.provider_id] if row.provider_id is not None else None
        model = found.models[row.model_id] if row.model_id is not None else None
        views.append(
            pricing_rule_view(
                row,
                customer_id=tenant.public_id if tenant is not None else None,
                provider=(provider.public_id, provider.code) if provider is not None else None,
                model=(model.public_id, model.code) if model is not None else None,
                components=components[row.id],
                emails=emails,
            )
        )
    return views


def _view(session: Session, row: PricingRule, *, lock: bool = False) -> PricingRuleView:
    [view] = _views(session, [row], lock=lock)
    return view


def _scope_state(row: PricingRule, found: ScopeRows) -> dict[str, object]:
    """Audit form of the scope: tenant public id, provider and model codes (设计 §2「审计」)."""
    tenant = found.tenants.get(row.tenant_id) if row.tenant_id is not None else None
    provider = found.providers.get(row.provider_id) if row.provider_id is not None else None
    model = found.models.get(row.model_id) if row.model_id is not None else None
    return {
        "priority_scope": PricingScope(row.priority_scope).value,
        "customer_id": tenant.public_id if tenant is not None else None,
        "provider_code": provider.code if provider is not None else None,
        "model_code": model.code if model is not None else None,
    }


def _stored_components(stored: Sequence[RuleComponent]) -> list[dict[str, str]]:
    """Audit form of the stored components: code and the two amounts, `component_code` order."""
    return [
        {
            "component_code": item.component.component_code,
            "unit_quantity": money_text(item.row.unit_quantity),
            "rate_amount": money_text(item.row.rate_amount),
        }
        for item in stored
    ]


def _requested_components(components: Sequence[RuleComponentInput]) -> list[dict[str, str]]:
    """Audit form of the requested components, in the same order and format as the stored."""
    return [
        {
            "component_code": item.component_code,
            "unit_quantity": money_text(item.unit_quantity),
            "rate_amount": money_text(item.rate_amount),
        }
        for item in sorted(components, key=lambda item: item.component_code)
    ]


def _same_components(
    stored: Sequence[RuleComponent], requested: Sequence[RuleComponentInput]
) -> bool:
    """Whether a replacement would change nothing (amounts compare by value: 1.5 == 1.50000000)."""
    before = sorted(
        (item.component.component_code, item.row.unit_quantity, item.row.rate_amount)
        for item in stored
    )
    after = sorted(
        (item.component_code, item.unit_quantity, item.rate_amount) for item in requested
    )
    return before == after


def _rule_content(
    row: PricingRule, found: ScopeRows, stored: Sequence[RuleComponent]
) -> dict[str, object]:
    """Scope, strategy, multiplier and components: what a publish makes binding."""
    return {
        **_scope_state(row, found),
        "strategy": _strategy(row).value,
        "markup_multiplier": _amount(row.markup_multiplier),
        "components": _stored_components(stored),
    }


# --- 校验 -------------------------------------------------------------------------


def _require_scope_fields(
    priority_scope: PricingScope,
    customer_id: str | None,
    provider_id: str | None,
    model_id: str | None,
) -> None:
    """设计 §2「接口」：范围字段与 `priority_scope` 不匹配（多给或少给）→ 422。"""
    given = {"customer_id": customer_id, "provider_id": provider_id, "model_id": model_id}
    present = frozenset(name for name, value in given.items() if value is not None)
    if present != required_scope_fields(priority_scope):
        raise PricingRuleShapeInvalid("The scope fields do not match priority_scope.")


def _require_strategy_fields(
    strategy: PricingStrategy,
    markup_multiplier: Decimal | None,
    components: Sequence[RuleComponentInput] | None,
) -> None:
    """MARKUP：必有倍数、没有分量；FIXED_RATE：没有倍数（混合的规则一律 422）。"""
    if strategy is PricingStrategy.MARKUP:
        if markup_multiplier is None:
            raise PricingRuleShapeInvalid("A MARKUP rule needs markup_multiplier.")
        if components is not None:
            raise PricingRuleShapeInvalid("A MARKUP rule has no components.")
    elif markup_multiplier is not None:
        raise PricingRuleShapeInvalid("A FIXED_RATE rule has no markup_multiplier.")


def _catalog_components(
    session: Session, components: Sequence[RuleComponentInput]
) -> list[tuple[RuleComponentInput, UsageMeterComponent, UsageMeterType]]:
    """Each requested component with its catalog component and meter type; unknown: 404."""
    found = rules.meter_components_by_code(session, [item.component_code for item in components])
    resolved = []
    for item in components:
        if item.component_code not in found:
            raise MeterComponentNotFound
        component, kind = found[item.component_code]
        resolved.append((item, component, kind))
    return resolved


def _require_active(
    provider: AiProvider | None, model: AiModel | None, meter_types: Sequence[UsageMeterType]
) -> None:
    """025 contract: no new or published rule on a RETIRED provider, model or meter type.

    租户只要存在（外键保证），账户状态不限：停用的客户也可以预先配置价格（设计 §2「完整性」）。
    """
    statuses = [kind.status for kind in meter_types]
    statuses += [item.status for item in (provider, model) if item is not None]
    if any(CatalogStatus(status) is CatalogStatus.RETIRED for status in statuses):
        raise CatalogItemRetired


def _catalog_of(row: PricingRule, found: ScopeRows) -> tuple[AiProvider | None, AiModel | None]:
    provider = found.providers[row.provider_id] if row.provider_id is not None else None
    model = found.models[row.model_id] if row.model_id is not None else None
    return provider, model


def missing_components(
    session: Session, stored: Sequence[RuleComponent], *, lock: bool = False
) -> list[str]:
    """The catalog components the rule lacks for the meter types it prices, sorted.

    凡是出现的计量类型，它在 025 里的全部分量都必须出现（与价格版本同一条规则）；整体没出现的
    计量类型不要求。`lock`：加锁读目录的分量（发布时）。
    """
    present = {item.component.id for item in stored}
    required = rules.meter_components_of(
        session, [item.component.meter_type_id for item in stored], lock=lock
    )
    return sorted(
        component.component_code
        for components in required.values()
        for component in components
        if component.id not in present
    )


def _new_components(
    resolved: Sequence[tuple[RuleComponentInput, UsageMeterComponent, UsageMeterType]],
) -> list[NewComponent]:
    return [
        NewComponent(
            usage_meter_component_id=component.id,
            unit_quantity=item.unit_quantity,
            rate_amount=item.rate_amount,
        )
        for item, component, _ in resolved
    ]


# --- 区间 -------------------------------------------------------------------------


def is_empty_period(row: PricingRule) -> bool:
    """`[F, F)`: never matches any moment and takes no part in the timeline."""
    return row.effective_from is not None and row.effective_from == row.effective_to


def _timeline(rows: Sequence[PricingRule]) -> list[PricingRule]:
    """The rules with a non-empty period, earliest start first (a `NULL` start is earliest)."""
    return sorted((row for row in rows if not is_empty_period(row)), key=rules.rule_order)


def verify_periods(rows: Sequence[PricingRule]) -> None:
    """Raise `PricingPeriodsBroken` unless the published rules of one scope chain in time.

    设计 §2「发布：区间怎么定」的复查：① 空区间的行都是 `RETIRED`；② 其余（区间非空的）按
    `effective_from` 排序，只有第一条的 `effective_from` 可为空；相邻两条，前一条的
    `effective_to` 为空时它必须是最后一条，非空时 ≤ 后一条的 `effective_from`（等号 = 首尾
    相接，小于 = 停用留下的空档）。区间自身的合法性归数据库的 CHECK。
    """
    for row in rows:
        if is_empty_period(row) and _status(row) is not PricingRuleStatus.RETIRED:
            raise PricingPeriodsBroken("only a retired rule may have an empty period")
    for earlier, later in pairwise(_timeline(rows)):
        if later.effective_from is None:
            raise PricingPeriodsBroken("only the first rule may start at the beginning")
        if earlier.effective_to is None:
            raise PricingPeriodsBroken("only the last rule may be open")
        if earlier.effective_to > later.effective_from:
            raise PricingPeriodsBroken("periods overlap")


def _recheck(session: Session, scope: Scope) -> None:
    # 重新加锁读一遍库里的规则（刚 flush 的也在），不信内存里的推算。
    verify_periods(rules.period_rules(session, scope))


def _placement(
    tail: PricingRule | None,
    *,
    requested: dt.datetime | None,
    t: dt.datetime,
    backdate: bool,
) -> tuple[dt.datetime | None, bool]:
    """Where a new rule starts, and whether the tail must be truncated there.

    `tail` 是末尾规则 L：区间非空的已发布（`PUBLISHED` / `RETIRED`）规则中 `effective_from`
    最晚的一条。`backdate`：`GLOBAL` 范围、从没有已发布规则 —— 只有这时未指定时刻才是
    「一直以来」（设计 §2「回溯生效只允许全局默认的第一条」）。
    """
    if tail is None:
        if requested is not None:
            return requested, False
        return (None if backdate else t), False
    if tail.effective_to is None:
        # 「F 必须 > L 的起点」；不指定时刻而 L 尚未开始（起点 ≥ `t`）同样冲突：截断会得到
        # 倒置或空的已发布区间，也不自动推后管理员没有给的生效时刻。
        start = t if requested is None else requested
        if tail.effective_from is not None and start <= tail.effective_from:
            raise EffectiveFromConflict
        return start, True
    # L 有尽头 E：从 max(E, 所请求或 t) 起，空档不回填；F < E 冲突。
    end = tail.effective_to
    if requested is not None and requested < end:
        raise EffectiveFromConflict
    return max(end, requested if requested is not None else t), False


# --- 草稿 -------------------------------------------------------------------------


def _require_rule(session: Session, rule_id: str, *, for_update: bool = False) -> PricingRule:
    row = rules.get_rule(session, rule_id, for_update=for_update)
    if row is None:
        raise PricingRuleNotFound
    return row


def _require_draft(row: PricingRule) -> None:
    status = _status(row)
    if status in _FINAL_STATUSES:
        raise PricingRuleFinal
    if status is not PricingRuleStatus.DRAFT:
        raise PricingRuleNotDraft


def create_draft(
    session_factory: sessionmaker[Session],
    *,
    actor: User,
    priority_scope: PricingScope,
    customer_id: str | None,
    provider_id: str | None,
    model_id: str | None,
    strategy: PricingStrategy,
    markup_multiplier: Decimal | None,
    components: Sequence[RuleComponentInput] | None,
    context: RequestContext,
    clock: Clock = utc_now,
) -> PricingRuleView:
    """A draft, its components and `PRICING_CREATE` in one commit.

    范围字段与 `priority_scope` 不匹配、策略与字段混合：422 `VALIDATION_ERROR`。客户不存在
    404 `CUSTOMER_NOT_FOUND`；供应商不存在 404 `AI_PROVIDER_NOT_FOUND`；模型不存在或不属于它
    404 `AI_MODEL_NOT_FOUND`；分量代码不在目录里 404 `USAGE_METER_COMPONENT_NOT_FOUND`；
    供应商、模型或分量所属计量类型已停用 409 `CATALOG_ITEM_RETIRED`。都不写。草稿可以
    不完整：完整性在发布时校验。
    """
    _require_scope_fields(priority_scope, customer_id, provider_id, model_id)
    _require_strategy_fields(strategy, markup_multiplier, components)
    requested = list(components or [])
    with session_scope(session_factory) as session:
        now = _seconds(clock())
        tenant_pk: int | None = None
        provider: AiProvider | None = None
        model: AiModel | None = None
        if customer_id is not None:
            tenant = tenancy.get_tenant_by_public_id(session, customer_id)
            if tenant is None:
                raise CustomerNotFound
            tenant_pk = tenant.id
        if provider_id is not None:
            provider = catalog.get_provider(session, provider_id)
            if provider is None:
                raise ProviderNotFound
        if model_id is not None and provider is not None:
            model = catalog.get_model(session, provider.id, model_id)
            if model is None:
                raise ModelNotFound
        resolved = _catalog_components(session, requested)
        _require_active(provider, model, [kind for _, _, kind in resolved])
        scope = Scope(
            priority_scope,
            tenant_pk,
            provider.id if provider is not None else None,
            model.id if model is not None else None,
        )
        row = rules.insert_draft(
            session,
            scope=scope,
            strategy=strategy,
            markup_multiplier=markup_multiplier,
            created_by=actor.id,
            now=now,
        )
        if resolved:
            rules.insert_components(session, row, _new_components(resolved), now=now)
        found = rules.scope_rows(session, [row])
        record_audit(
            session,
            action=AuditAction.PRICING_CREATE,
            context=context,
            now=now,
            actor=actor,
            entity_type=ENTITY_PRICING_RULE,
            entity_id=row.public_id,
            after_state={
                **_scope_state(row, found),
                "strategy": strategy.value,
                "markup_multiplier": _amount(markup_multiplier),
                "components": _requested_components(requested),
                "status": PricingRuleStatus.DRAFT.value,
            },
        )
        view = _view(session, row)
    return view


def update_draft(
    session_factory: sessionmaker[Session],
    *,
    actor: User,
    rule_id: str,
    strategy: PricingStrategy | None,
    markup_multiplier: Decimal | None,
    components: Sequence[RuleComponentInput] | None,
    context: RequestContext,
    clock: Clock = utc_now,
) -> PricingRuleView:
    """Change a draft; `None` = not in the request. `components` replaces the whole set.

    改成（或仍是）MARKUP：倍数取请求里的，没给就沿用原来的（原本是 FIXED_RATE 则 422）；带
    `components` 422；原有分量全部删掉。改成（或仍是）FIXED_RATE：带倍数 422；倍数清空；分量取
    请求里的，没给就沿用原来的（原本是 MARKUP 则为空）。范围不可改。

    非草稿：已发布 409 `PRICING_RULE_NOT_DRAFT`，已停用 / 已丢弃 409 `PRICING_RULE_FINAL`。
    目录校验同建草稿。**没有实际变化**：200，什么都不写（`updated_at` 不变、不写审计）。
    """
    with session_scope(session_factory) as session:
        row = _require_rule(session, rule_id, for_update=True)
        _require_draft(row)
        now = _seconds(clock())
        current = _strategy(row)
        target = strategy or current
        if target is PricingStrategy.MARKUP:
            if components is not None:
                raise PricingRuleShapeInvalid("A MARKUP rule has no components.")
            multiplier = markup_multiplier
            if multiplier is None and current is PricingStrategy.MARKUP:
                multiplier = row.markup_multiplier
            if multiplier is None:
                raise PricingRuleShapeInvalid("A MARKUP rule needs markup_multiplier.")
        else:
            if markup_multiplier is not None:
                raise PricingRuleShapeInvalid("A FIXED_RATE rule has no markup_multiplier.")
            multiplier = None

        found = rules.scope_rows(session, [row])
        stored = rules.components_for(session, [row.id])[row.id]
        resolved = None if components is None else _catalog_components(session, components)
        if target is PricingStrategy.MARKUP:
            meter_types: list[UsageMeterType] = []
        elif resolved is not None:
            meter_types = [kind for _, _, kind in resolved]
        else:
            meter_types = [item.meter_type for item in stored]
        _require_active(*_catalog_of(row, found), meter_types)

        before: dict[str, object] = {}
        after: dict[str, object] = {}
        if target is not current:
            before["strategy"], after["strategy"] = current.value, target.value
        if multiplier != row.markup_multiplier:
            before["markup_multiplier"] = _amount(row.markup_multiplier)
            after["markup_multiplier"] = _amount(multiplier)
        if target is PricingStrategy.MARKUP:
            replace = bool(stored)
            replacement: list[RuleComponentInput] = []
        else:
            replace = components is not None and not _same_components(stored, components)
            replacement = list(components or [])
        if replace:
            before["components"] = _stored_components(stored)
            after["components"] = _requested_components(replacement)
        if not after:
            return _view(session, row)

        # ⚠️ 写入顺序：分量先删（改成 MARKUP 时复合外键要求父行已没有分量），再改策略与倍数
        # （同一条 UPDATE，CHECK 要求两者一致），最后插入新分量（父行已是 FIXED_RATE）。
        if replace:
            rules.drop_draft_components(session, row)
        rules.save_draft(session, row, strategy=target, markup_multiplier=multiplier, now=now)
        if replace and resolved:
            rules.insert_components(session, row, _new_components(resolved), now=now)
        record_audit(
            session,
            action=AuditAction.PRICING_UPDATE,
            context=context,
            now=now,
            actor=actor,
            entity_type=ENTITY_PRICING_RULE,
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
    rule_id: str,
    context: RequestContext,
    clock: Clock = utc_now,
) -> PricingRuleView:
    """DRAFT → DISCARDED and `PRICING_DISCARD` in one commit. The row stays."""
    with session_scope(session_factory) as session:
        row = _require_rule(session, rule_id, for_update=True)
        _require_draft(row)
        now = _seconds(clock())
        rules.mark_discarded(session, row, now=now)
        record_audit(
            session,
            action=AuditAction.PRICING_DISCARD,
            context=context,
            now=now,
            actor=actor,
            entity_type=ENTITY_PRICING_RULE,
            entity_id=row.public_id,
            before_state={"status": PricingRuleStatus.DRAFT.value},
            after_state={"status": PricingRuleStatus.DISCARDED.value},
        )
        view = _view(session, row)
    return view


# --- 发布与停用 ---------------------------------------------------------------------


def publish(
    session_factory: sessionmaker[Session],
    *,
    actor: User,
    rule_id: str,
    effective_from: dt.datetime | None,
    context: RequestContext,
    clock: Clock = utc_now,
) -> PricingRuleView:
    """DRAFT → PUBLISHED inside the pricing rule lock (设计 §2「发布：区间怎么定」).

    `effective_from` 是不带时区的 UTC 整秒，或 `None`。已发布：200，什么都不写（幂等）。
    已停用 / 已丢弃：409 `PRICING_RULE_FINAL`。目录已停用：409 `CATALOG_ITEM_RETIRED`。
    FIXED_RATE 不完整：409 `PRICING_RULE_INCOMPLETE`。请求的时刻早于 `t`：422
    `EFFECTIVE_FROM_IN_PAST`；不晚于该范围末尾规则的起点、或早于它的尽头，以及不指定时刻而末尾
    规则尚未开始（起点 ≥ `t`）：409 `EFFECTIVE_FROM_CONFLICT`。都不写。
    """
    with session_scope(session_factory) as session:
        # ⚠️ 本事务的第一条语句就是这把锁；之后的读全是加锁读。
        rules.lock_pricing_rules(session)
        # ⚠️ 拿到锁之后才取时间：`t` 必须晚于任何先于它持锁的计费（设计 §2）。
        now = clock()
        # 与改草稿（同样 `FOR UPDATE` 规则行）串行：它先提交，这里读到的就是它写的分量。
        row = _require_rule(session, rule_id, for_update=True)
        status = _status(row)
        if status is PricingRuleStatus.PUBLISHED:
            return _view(session, row, lock=True)
        if status in _FINAL_STATUSES:
            raise PricingRuleFinal
        found = rules.scope_rows(session, [row], lock=True)
        stored = rules.components_for(session, [row.id], lock=True)[row.id]
        _require_active(*_catalog_of(row, found), [item.meter_type for item in stored])
        if _strategy(row) is PricingStrategy.FIXED_RATE:
            missing = missing_components(session, stored, lock=True)
            if not stored or missing:
                raise PricingRuleIncomplete(missing)
        t = boundary_after(now)
        if effective_from is not None and effective_from < t:
            raise EffectiveFromInPast

        scope = rules.scope_of(row)
        history = rules.period_rules(session, scope)
        timeline = _timeline(history)
        tail = timeline[-1] if timeline else None
        backdate = scope.priority_scope is PricingScope.GLOBAL and not history
        start, cut = _placement(tail, requested=effective_from, t=t, backdate=backdate)
        stamp = _seconds(now)
        before: dict[str, object] = {"status": PricingRuleStatus.DRAFT.value}
        after: dict[str, object] = {
            **_rule_content(row, found, stored),
            "status": PricingRuleStatus.PUBLISHED.value,
            "effective_from": _text(start),
            "effective_to": None,
            "approved_by_email": actor.email,
            "approved_at": _text(stamp),
        }
        # ⚠️ 写入顺序（设计 §2）：先截断前一条，再把本行从草稿改为已发布。
        if cut and tail is not None and start is not None:
            rules.truncate(session, tail, at=start, now=stamp)
            before["truncated_rule"] = {"id": tail.public_id, "effective_to": None}
            after["truncated_rule"] = {"id": tail.public_id, "effective_to": _text(start)}
        rules.mark_published(session, row, effective_from=start, approved_by=actor.id, now=stamp)
        _recheck(session, scope)
        record_audit(
            session,
            action=AuditAction.PRICING_PUBLISH,
            context=context,
            now=stamp,
            actor=actor,
            entity_type=ENTITY_PRICING_RULE,
            entity_id=row.public_id,
            before_state=before,
            after_state=after,
        )
        view = _view(session, row, lock=True)
    return view


def retire(
    session_factory: sessionmaker[Session],
    *,
    actor: User,
    rule_id: str,
    reason: str,
    context: RequestContext,
    clock: Clock = utc_now,
) -> PricingRuleView:
    """PUBLISHED → RETIRED inside the pricing rule lock (设计 §2「停用」, §59 disable rule).

    - 当前末尾、已开始生效（起点为空或 ≤ `t`）：终点写 `t`，`t` 起该范围不再命中，事件下落到
      更低一级（或无规则 → `PRICING_ERROR`）；
    - 当前末尾、尚未开始的预约（起点 > `t`）：终点写成起点（空区间，永不命中）；
      前一条若正是被它截断的（终点等于它的起点、仍是 PUBLISHED），恢复为未截断；
    - 已被后继截断的历史规则、草稿：409 `PRICING_RULE_NOT_RETIRABLE`；
    - 已停用 / 已丢弃：409 `PRICING_RULE_FINAL`。

    恢复前一条只影响 `occurred_at ≥ 本规则起点 > t` 的时刻：它们都还没有被计费。
    停用后的空档不回填。
    """
    with session_scope(session_factory) as session:
        # ⚠️ 本事务的第一条语句就是这把锁；之后的读全是加锁读（同 `publish`）。
        rules.lock_pricing_rules(session)
        # ⚠️ 拿到锁之后才取时间（同 `publish`）。
        now = clock()
        row = _require_rule(session, rule_id, for_update=True)
        status = _status(row)
        if status in _FINAL_STATUSES:
            raise PricingRuleFinal
        if status is not PricingRuleStatus.PUBLISHED or row.effective_to is not None:
            raise PricingRuleNotRetirable
        t = boundary_after(now)
        stamp = _seconds(now)
        scope = rules.scope_of(row)
        found = rules.scope_rows(session, [row], lock=True)
        timeline = _timeline(rules.period_rules(session, scope))
        start = row.effective_from
        before: dict[str, object] = {
            "status": PricingRuleStatus.PUBLISHED.value,
            "effective_from": _text(start),
            "effective_to": None,
        }
        after: dict[str, object] = {
            **_scope_state(row, found),
            "status": PricingRuleStatus.RETIRED.value,
            "effective_from": _text(start),
        }
        if start is None or start <= t:
            end = t
            rules.mark_retired(session, row, effective_to=end, now=stamp)
        else:
            end = start
            key = rules.rule_order(row)
            earlier = [item for item in timeline if rules.rule_order(item) < key]
            previous = earlier[-1] if earlier else None
            # ⚠️ 写入顺序（设计 §2）：先把被撤销的一行改成空区间，再恢复前一条。
            rules.mark_retired(session, row, effective_to=end, now=stamp)
            # 前一条仍是 PUBLISHED、终点等于本规则的起点（被本规则截断的）才恢复；
            # 以停用结束于那一刻的不恢复（停用不回填）。
            if (
                previous is not None
                and _status(previous) is PricingRuleStatus.PUBLISHED
                and previous.effective_to == start
            ):
                rules.restore_open(session, previous, now=stamp)
                restored = previous.public_id
                before["restored_rule"] = {"id": restored, "effective_to": _text(start)}
                after["restored_rule"] = {"id": restored, "effective_to": None}
        after["effective_to"] = _text(end)
        _recheck(session, scope)
        record_audit(
            session,
            action=AuditAction.PRICING_RETIRE,
            context=context,
            now=stamp,
            actor=actor,
            entity_type=ENTITY_PRICING_RULE,
            entity_id=row.public_id,
            before_state=before,
            after_state=after,
            reason=reason,
        )
        view = _view(session, row, lock=True)
    return view


# --- 查询 -------------------------------------------------------------------------


def get_pricing_rule(session_factory: sessionmaker[Session], rule_id: str) -> PricingRuleView:
    with session_factory() as session:
        return _view(session, _require_rule(session, rule_id))


def list_pricing_rules(
    session_factory: sessionmaker[Session],
    *,
    priority_scope: PricingScope | None,
    customer_id: str | None,
    provider_id: str | None,
    model_id: str | None,
    status: PricingRuleStatus | None,
    page: int,
    page_size: int,
) -> Page[PricingRuleView]:
    """By scope (§16 order), then `effective_from`; each with its components (§59 price history).

    筛选里的客户不存在 404 `CUSTOMER_NOT_FOUND`；供应商不存在 404 `AI_PROVIDER_NOT_FOUND`；模型
    不存在（或给了供应商、模型却不属于它）404 `AI_MODEL_NOT_FOUND`。
    """
    with session_factory() as session:
        tenant_pk: int | None = None
        provider_pk: int | None = None
        model_pk: int | None = None
        if customer_id is not None:
            tenant = tenancy.get_tenant_by_public_id(session, customer_id)
            if tenant is None:
                raise CustomerNotFound
            tenant_pk = tenant.id
        if provider_id is not None:
            provider = catalog.get_provider(session, provider_id)
            if provider is None:
                raise ProviderNotFound
            provider_pk = provider.id
        if model_id is not None:
            if provider_pk is not None:
                model = catalog.get_model(session, provider_pk, model_id)
            else:
                model = rules.find_model(session, model_id)
            if model is None:
                raise ModelNotFound
            model_pk = model.id
        rows, total = rules.list_rules(
            session,
            priority_scope=priority_scope,
            tenant_id=tenant_pk,
            provider_id=provider_pk,
            model_id=model_pk,
            status=status,
            offset=_offset(page, page_size),
            limit=page_size,
        )
        return Page[PricingRuleView](
            items=_views(session, rows),
            page=page,
            page_size=page_size,
            total=total,
        )


__all__ = [
    "ENTITY_PRICING_RULE",
    "Clock",
    "EffectiveFromConflict",
    "EffectiveFromInPast",
    "PricingPeriodsBroken",
    "PricingRuleFinal",
    "PricingRuleIncomplete",
    "PricingRuleNotDraft",
    "PricingRuleNotFound",
    "PricingRuleNotRetirable",
    "PricingRuleShapeInvalid",
    "create_draft",
    "discard_draft",
    "get_pricing_rule",
    "is_empty_period",
    "list_pricing_rules",
    "missing_components",
    "publish",
    "retire",
    "update_draft",
    "verify_periods",
]

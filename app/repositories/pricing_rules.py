"""Pricing rule data access and rule resolution (design gate #178 v3, AIH-TASK-027).

⚠️ **只 flush，不 commit。**规则、分量与它们的审计同一事务，事务边界归调用方的
`session_scope()`（app/services/pricing_rules.py）。`lock_pricing_rules_shared` 与
`resolve_pricing_rule` 更是如此：它们必须在调用方（T-H）写事件快照、扣费的那个事务里调用，
锁持有到那个事务提交（设计 §2「对下游任务的契约」T-H）。

⚠️ **发布与停用里的读、以及解析都是加锁读**（`FOR UPDATE` / `FOR SHARE`）：MySQL 默认的
REPEATABLE READ 下，普通 SELECT 读的是本事务第一次一致性读时的快照 —— 锁拿到了、数据却
可能是旧的。加锁读总是读最新提交的版本。`populate_existing`：会话里已有的对象按锁内读到的
值刷新。SQLite 忽略这两种锁。

⚠️ **写入顺序有讲究**（设计 §2，数据库的区间触发器与 `open_slot` 唯一索引都按行检查）：
每个改区间的函数都立刻 flush，调用方按「先截断前一条、再发布本行」「先把被撤销的一行改成
空区间、再恢复前一条」的顺序调用。规则行没有删除；分量只在草稿「整体替换」或改成 MARKUP 时
删掉。
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import NamedTuple

from sqlalchemy import Select, case, func, or_, select
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from app.models.ai_catalog import AiModel, AiProvider, UsageMeterComponent, UsageMeterType
from app.models.auth import User
from app.models.pricing_rules import (
    PERIOD_STATUSES,
    PRICING_RULE_LOCK_ID,
    RULE_CURRENCY,
    SCOPE_ORDER,
    PricingRule,
    PricingRuleComponent,
    PricingRuleLock,
    PricingRuleStatus,
    PricingScope,
    PricingStrategy,
)
from app.models.tenancy import Tenant


class NewComponent(NamedTuple):
    """One component to write into a FIXED_RATE draft."""

    usage_meter_component_id: int
    unit_quantity: Decimal
    rate_amount: Decimal


class RuleComponent(NamedTuple):
    """A rule component with the catalog rows that give it its code, field and unit."""

    row: PricingRuleComponent
    component: UsageMeterComponent
    meter_type: UsageMeterType


class Scope(NamedTuple):
    """A rule's scope: the level and its three columns (internal ids; `None` where unused)."""

    priority_scope: PricingScope
    tenant_id: int | None
    provider_id: int | None
    model_id: int | None


@dataclass(frozen=True)
class ResolvedRuleComponent:
    """One component price of a resolved FIXED_RATE rule. T-G takes the quantity from
    `quantity_field`; `rate_amount` is the MYR tax-inclusive price per `unit_quantity`."""

    component_code: str
    quantity_field: str
    meter_type_id: int
    unit_quantity: Decimal
    rate_amount: Decimal
    currency: str


@dataclass(frozen=True)
class ResolvedPricingRule:
    """The rule in effect at `occurred_at`: the first level of §16 that has one.

    `rule_id` 是内部 id：T-H 把它写进事件快照（`pricing_rule_id`），不对外。MARKUP 规则
    `components` 为空、`markup_multiplier` 非空；FIXED_RATE 反之（`component_code` 升序）。
    """

    rule_id: int
    public_id: str
    priority_scope: PricingScope
    strategy: PricingStrategy
    markup_multiplier: Decimal | None
    tenant_id: int | None
    provider_id: int | None
    model_id: int | None
    effective_from: dt.datetime | None
    effective_to: dt.datetime | None
    components: tuple[ResolvedRuleComponent, ...]


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


def _locked[T](statement: Select[T], lock: bool) -> Select[T]:
    """`FOR SHARE` when `lock`: reads made under the rule lock in a publish or retire."""
    return _shared(statement) if lock else statement


def scope_of(row: PricingRule) -> Scope:
    return Scope(PricingScope(row.priority_scope), row.tenant_id, row.provider_id, row.model_id)


def _same(column: ColumnElement[int | None], value: int | None) -> ColumnElement[bool]:
    return column.is_(None) if value is None else column == value


def _in_scope(scope: Scope) -> list[ColumnElement[bool]]:
    """The rows of exactly this scope (NULL-safe on the three columns)."""
    return [
        PricingRule.priority_scope == scope.priority_scope,
        _same(PricingRule.tenant_id, scope.tenant_id),
        _same(PricingRule.provider_id, scope.provider_id),
        _same(PricingRule.model_id, scope.model_id),
    ]


# --- 锁 ---------------------------------------------------------------------------


def lock_pricing_rules(session: Session) -> PricingRuleLock:
    """`SELECT … FROM pricing_rule_locks WHERE id = 1 FOR UPDATE` (MySQL; SQLite ignores it).

    发布与停用的第一条语句：两者彼此串行，也与计费的共享锁互斥（设计 §2「为什么与计费不会
    交错」）。边界时刻 `t` 必须在它返回**之后**才取。
    """
    statement = (
        select(PricingRuleLock)
        .where(PricingRuleLock.id == PRICING_RULE_LOCK_ID)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return session.execute(statement).scalar_one()


def lock_pricing_rules_shared(session: Session) -> PricingRuleLock:
    """`SELECT … FROM pricing_rule_locks WHERE id = 1 FOR SHARE`, for billing (T-H).

    ⚠️ 调用方契约（设计 §2 T-H）：在计费事务里 `resolve_model`（供应商行 S）之后、锁钱包之前
    调用，持有到计费事务提交；锁顺序是 **供应商（S）→ 定价规则锁（S）→ 钱包 → 租户**。共享锁
    之间不冲突，计费事务彼此不阻塞；只有发布 / 停用（排他锁）会让计费等一下。本任务不接到任何
    计费路径。
    """
    statement = select(PricingRuleLock).where(PricingRuleLock.id == PRICING_RULE_LOCK_ID)
    return session.execute(_shared(statement)).scalar_one()


# --- 读 ---------------------------------------------------------------------------


def get_rule(session: Session, public_id: str, *, for_update: bool = False) -> PricingRule | None:
    statement = select(PricingRule).where(PricingRule.public_id == public_id)
    if for_update:
        statement = statement.with_for_update().execution_options(populate_existing=True)
    return session.execute(statement).scalar_one_or_none()


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


def meter_components_of(
    session: Session, meter_type_ids: Iterable[int], *, lock: bool = False
) -> dict[int, list[UsageMeterComponent]]:
    """`meter_type_id` → its catalog components, `component_code` ascending."""
    ids = sorted(set(meter_type_ids))
    found: dict[int, list[UsageMeterComponent]] = {meter_type_id: [] for meter_type_id in ids}
    if not ids:
        return found
    statement = (
        select(UsageMeterComponent)
        .where(UsageMeterComponent.meter_type_id.in_(ids))
        .order_by(UsageMeterComponent.component_code)
    )
    for row in session.execute(_locked(statement, lock)).scalars():
        found[row.meter_type_id].append(row)
    return found


def components_for(
    session: Session, rule_ids: Iterable[int], *, lock: bool = False
) -> dict[int, list[RuleComponent]]:
    """Rule id → its components, `component_code` ascending. `lock`: `FOR SHARE` on all three."""
    ids = sorted(set(rule_ids))
    found: dict[int, list[RuleComponent]] = {rule_id: [] for rule_id in ids}
    if not ids:
        return found
    statement = (
        select(PricingRuleComponent, UsageMeterComponent, UsageMeterType)
        .join(
            UsageMeterComponent,
            UsageMeterComponent.id == PricingRuleComponent.usage_meter_component_id,
        )
        .join(UsageMeterType, UsageMeterType.id == UsageMeterComponent.meter_type_id)
        .where(PricingRuleComponent.pricing_rule_id.in_(ids))
        .order_by(UsageMeterComponent.component_code)
    )
    for row, component, kind in session.execute(_locked(statement, lock)):
        found[row.pricing_rule_id].append(RuleComponent(row, component, kind))
    return found


@dataclass(frozen=True)
class ScopeRows:
    """The tenants, providers and models some rules hang on, by internal id."""

    tenants: dict[int, Tenant]
    providers: dict[int, AiProvider]
    models: dict[int, AiModel]


def scope_rows(session: Session, rows: Sequence[PricingRule], *, lock: bool = False) -> ScopeRows:
    tenant_ids = sorted({row.tenant_id for row in rows if row.tenant_id is not None})
    provider_ids = sorted({row.provider_id for row in rows if row.provider_id is not None})
    model_ids = sorted({row.model_id for row in rows if row.model_id is not None})
    tenants: dict[int, Tenant] = {}
    providers: dict[int, AiProvider] = {}
    models: dict[int, AiModel] = {}
    if tenant_ids:
        found = session.execute(_locked(select(Tenant).where(Tenant.id.in_(tenant_ids)), lock))
        tenants = {tenant.id: tenant for tenant in found.scalars()}
    if provider_ids:
        statement = select(AiProvider).where(AiProvider.id.in_(provider_ids))
        providers = {item.id: item for item in session.execute(_locked(statement, lock)).scalars()}
    if model_ids:
        statement = select(AiModel).where(AiModel.id.in_(model_ids))
        models = {item.id: item for item in session.execute(_locked(statement, lock)).scalars()}
    return ScopeRows(tenants, providers, models)


def user_emails(
    session: Session, user_ids: Iterable[int | None], *, lock: bool = False
) -> dict[int, str]:
    """Internal user id → login email. Admin views name people by email, never by id."""
    ids = sorted({user_id for user_id in user_ids if user_id is not None})
    if not ids:
        return {}
    statement = select(User.id, User.email).where(User.id.in_(ids))
    return {row.id: row.email for row in session.execute(_locked(statement, lock))}


def list_rules(
    session: Session,
    *,
    priority_scope: PricingScope | None,
    tenant_id: int | None,
    provider_id: int | None,
    model_id: int | None,
    status: PricingRuleStatus | None,
    offset: int,
    limit: int,
) -> tuple[list[PricingRule], int]:
    """One page by scope (§16 order, then the scope's own rows) and `effective_from`; the total."""
    statement = select(PricingRule)
    if priority_scope is not None:
        statement = statement.where(PricingRule.priority_scope == priority_scope)
    if tenant_id is not None:
        statement = statement.where(PricingRule.tenant_id == tenant_id)
    if provider_id is not None:
        statement = statement.where(PricingRule.provider_id == provider_id)
    if model_id is not None:
        statement = statement.where(PricingRule.model_id == model_id)
    if status is not None:
        statement = statement.where(PricingRule.status == status)
    rank = case(
        {scope.value: index for index, scope in enumerate(SCOPE_ORDER)},
        value=PricingRule.priority_scope,
    )
    # 同一级里按范围聚在一起（内部 id 只用来排序、不出库）；两种方言的升序都把 NULL 排最前
    # （草稿与「一直以来」的起点都是 NULL）；再按 id 定住顺序。
    statement = statement.order_by(
        rank,
        PricingRule.tenant_id,
        PricingRule.provider_id,
        PricingRule.model_id,
        PricingRule.effective_from.is_not(None),
        PricingRule.effective_from,
        PricingRule.id,
    )
    return _page(session, statement, offset=offset, limit=limit)


# --- 区间 -------------------------------------------------------------------------


def rule_order(row: PricingRule) -> tuple[bool, dt.datetime, int]:
    """Time order: the one without a start first (排序在 Python 里做，不依赖方言的 NULL 约定)."""
    return (row.effective_from is not None, row.effective_from or dt.datetime.min, row.id)


def period_rules(session: Session, scope: Scope) -> list[PricingRule]:
    """Every PUBLISHED / RETIRED rule of the scope, in time order. Locking read."""
    statement = (
        select(PricingRule)
        .where(*_in_scope(scope), PricingRule.status.in_(PERIOD_STATUSES))
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return sorted(session.execute(statement).scalars().all(), key=rule_order)


# --- 写 ---------------------------------------------------------------------------


def insert_draft(
    session: Session,
    *,
    scope: Scope,
    strategy: PricingStrategy,
    markup_multiplier: Decimal | None,
    created_by: int,
    now: dt.datetime,
) -> PricingRule:
    """Insert and flush a draft: no period, no approver (设计 §2)."""
    row = PricingRule(
        public_id=_new_public_id(),
        priority_scope=scope.priority_scope,
        tenant_id=scope.tenant_id,
        provider_id=scope.provider_id,
        model_id=scope.model_id,
        strategy=strategy,
        markup_multiplier=markup_multiplier,
        status=PricingRuleStatus.DRAFT,
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
    rule: PricingRule,
    components: Sequence[NewComponent],
    *,
    now: dt.datetime,
) -> None:
    """Add MYR components to a FIXED_RATE draft and flush (the database refuses anything else)."""
    if rule.status is not PricingRuleStatus.DRAFT:
        raise ValueError("components are written only on a draft")
    if rule.strategy is not PricingStrategy.FIXED_RATE:
        raise ValueError("only a FIXED_RATE rule has components")
    session.add_all(
        PricingRuleComponent(
            pricing_rule_id=rule.id,
            strategy=PricingStrategy.FIXED_RATE,
            usage_meter_component_id=component.usage_meter_component_id,
            unit_quantity=component.unit_quantity,
            rate_amount=component.rate_amount,
            currency=RULE_CURRENCY,
            created_at=now,
        )
        for component in components
    )
    session.flush()


def drop_draft_components(session: Session, rule: PricingRule) -> None:
    """Remove every component of a draft: before a replacement set, or before becoming MARKUP.

    ⚠️ 先 flush 掉删除再插入 / 再改策略：同一次 flush 里 SQLAlchemy 先插后删，会撞
    `(pricing_rule_id, usage_meter_component_id)` 的唯一约束；改成 MARKUP 时复合外键
    （RESTRICT）也要求分量先没了。
    """
    if rule.status is not PricingRuleStatus.DRAFT:
        raise ValueError("components are removed only on a draft")
    statement = select(PricingRuleComponent).where(PricingRuleComponent.pricing_rule_id == rule.id)
    for row in session.execute(statement).scalars().all():
        session.delete(row)
    session.flush()


def save_draft(
    session: Session,
    row: PricingRule,
    *,
    strategy: PricingStrategy,
    markup_multiplier: Decimal | None,
    now: dt.datetime,
) -> None:
    """Strategy and multiplier of a draft, together (CHECK: they must agree after every update)."""
    if row.status is not PricingRuleStatus.DRAFT:
        raise ValueError("only a draft is edited")
    row.strategy = strategy
    row.markup_multiplier = markup_multiplier
    row.updated_at = now
    session.flush()


def mark_discarded(session: Session, row: PricingRule, *, now: dt.datetime) -> None:
    if row.status is not PricingRuleStatus.DRAFT:
        raise ValueError("only a draft is discarded")
    row.status = PricingRuleStatus.DISCARDED
    row.updated_at = now
    session.flush()


def truncate(session: Session, row: PricingRule, *, at: dt.datetime, now: dt.datetime) -> None:
    """`effective_to`: NULL → `at` on the open PUBLISHED rule; a successor takes over."""
    if row.status is not PricingRuleStatus.PUBLISHED or row.effective_to is not None:
        raise ValueError("only the untruncated published rule is truncated")
    row.effective_to = at
    row.updated_at = now
    session.flush()


def restore_open(session: Session, row: PricingRule, *, now: dt.datetime) -> None:
    """`effective_to` → NULL again: the reservation that truncated it was withdrawn."""
    if row.status is not PricingRuleStatus.PUBLISHED or row.effective_to is None:
        raise ValueError("only a truncated published rule is restored")
    row.effective_to = None
    row.updated_at = now
    session.flush()


def mark_published(
    session: Session,
    row: PricingRule,
    *,
    effective_from: dt.datetime | None,
    approved_by: int,
    now: dt.datetime,
) -> None:
    """DRAFT → PUBLISHED with `[effective_from, NULL)`; the approver is the publisher."""
    if row.status is not PricingRuleStatus.DRAFT:
        raise ValueError("only a draft is published")
    row.status = PricingRuleStatus.PUBLISHED
    row.effective_from = effective_from
    row.effective_to = None
    row.approved_by = approved_by
    row.approved_at = now
    row.updated_at = now
    session.flush()


def mark_retired(
    session: Session, row: PricingRule, *, effective_to: dt.datetime, now: dt.datetime
) -> None:
    """PUBLISHED → RETIRED, ending at `effective_to` (= its start for a withdrawn reservation)."""
    if row.status is not PricingRuleStatus.PUBLISHED or row.effective_to is not None:
        raise ValueError("only the untruncated published rule is retired")
    row.status = PricingRuleStatus.RETIRED
    row.effective_to = effective_to
    row.updated_at = now
    session.flush()


# --- 解析（T-G / T-H 调用；本任务不接到任何计费路径） -------------------------------------


def _level_scope(level: PricingScope, tenant_id: int, provider_id: int, model_id: int) -> Scope:
    """The one scope of `level` that an event of (tenant, provider, model) falls into."""
    return Scope(
        level,
        tenant_id if level.has_customer else None,
        provider_id if level.has_provider else None,
        model_id if level.has_model else None,
    )


def resolve_pricing_rule(
    session: Session,
    tenant_id: int,
    provider_id: int,
    model_id: int,
    occurred_at: dt.datetime,
) -> ResolvedPricingRule | None:
    """The pricing rule in effect for the event at `occurred_at`, or `None` (设计 §2「解析」).

    按 §16 的顺序逐级查：`CUSTOMER_PROVIDER_MODEL` → `CUSTOMER_PROVIDER` → `CUSTOMER` →
    `GLOBAL_PROVIDER_MODEL` → `GLOBAL`。每一级找范围匹配、`PUBLISHED` / `RETIRED`、
    `effective_from ≤ occurred_at < effective_to`（NULL 端不比）的规则；**第一级命中即返回**
    （规则 + 分量）。一旦命中，就不因该 FIXED_RATE 规则缺事件所需的分量而下落到更低一级（T-G
    判 `PRICING_ERROR`）。五级都没有 → `None`（T-G 判 `PRICING_ERROR`）。空区间（撤销的预约）
    永不匹配。三个 id 都是内部 id：租户取自事件，供应商与模型取自 `resolve_model`；
    `occurred_at` 是不带时区的 UTC。

    ⚠️ 调用方契约（设计 §2 T-H）：在计费事务里 `resolve_model` 之后、先
    `lock_pricing_rules_shared`，再调用这里；所有读都是加锁读（`FOR SHARE`），锁持有到计费事务
    提交。于是一个事件一旦选到某条规则，以后永远选到同一条。

    同一范围的非空区间不重叠（服务层在规则锁内保证并复查，数据库触发器兜底），所以每一级至多
    一条命中；多于一条是数据损坏，`scalar_one_or_none` 抛错，不静默挑一条。
    """
    started = or_(PricingRule.effective_from.is_(None), PricingRule.effective_from <= occurred_at)
    not_ended = or_(PricingRule.effective_to.is_(None), PricingRule.effective_to > occurred_at)
    for level in SCOPE_ORDER:
        scope = _level_scope(level, tenant_id, provider_id, model_id)
        in_effect = select(PricingRule).where(
            *_in_scope(scope),
            PricingRule.status.in_(PERIOD_STATUSES),
            started,
            not_ended,
        )
        rule = session.execute(_shared(in_effect)).scalar_one_or_none()
        if rule is not None:
            return _resolved(session, rule)
    return None


def _resolved(session: Session, rule: PricingRule) -> ResolvedPricingRule:
    priced = (
        select(PricingRuleComponent, UsageMeterComponent)
        .join(
            UsageMeterComponent,
            UsageMeterComponent.id == PricingRuleComponent.usage_meter_component_id,
        )
        .where(PricingRuleComponent.pricing_rule_id == rule.id)
        .order_by(UsageMeterComponent.component_code)
    )
    components = tuple(
        ResolvedRuleComponent(
            component_code=component.component_code,
            quantity_field=component.quantity_field,
            meter_type_id=component.meter_type_id,
            unit_quantity=row.unit_quantity,
            rate_amount=row.rate_amount,
            currency=row.currency,
        )
        for row, component in session.execute(_shared(priced))
    )
    return ResolvedPricingRule(
        rule_id=rule.id,
        public_id=rule.public_id,
        priority_scope=PricingScope(rule.priority_scope),
        strategy=PricingStrategy(rule.strategy),
        markup_multiplier=rule.markup_multiplier,
        tenant_id=rule.tenant_id,
        provider_id=rule.provider_id,
        model_id=rule.model_id,
        effective_from=rule.effective_from,
        effective_to=rule.effective_to,
        components=components,
    )


__all__ = [
    "NewComponent",
    "ResolvedPricingRule",
    "ResolvedRuleComponent",
    "RuleComponent",
    "Scope",
    "ScopeRows",
    "components_for",
    "drop_draft_components",
    "find_model",
    "get_rule",
    "insert_components",
    "insert_draft",
    "list_rules",
    "lock_pricing_rules",
    "lock_pricing_rules_shared",
    "mark_discarded",
    "mark_published",
    "mark_retired",
    "meter_components_by_code",
    "meter_components_of",
    "period_rules",
    "resolve_pricing_rule",
    "restore_open",
    "rule_order",
    "save_draft",
    "scope_of",
    "scope_rows",
    "truncate",
    "user_emails",
]

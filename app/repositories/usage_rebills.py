"""Usage rebills: the read-only queries (design gate #830 v12, AIH-TASK-254).

spec §19「Rebill / Reprocess」；Invariant 6、8、10。预览与确认（AIH-TASK-255）、管理端接口
（AIH-TASK-256）共用这些读：

- 选事件：按客户与显式 `public_id`（`find_explicit_events`），或按客户与条件
  （`select_events`，只选 `PROCESSED` 的预付事件，`occurred_at ∈ [from, to)`，按 `id` 升序；
  另用一条 COUNT 得到准确的匹配数，超过上限时不再取行）；
- 当前有效状态的输入：每个事件最近一条明细（`latest_lines`，明细 `id` 最大）；
- 用量事件详情的更正历史（`event_history`）；
- 幂等查重（`get_by_idempotency_key`）、Rebill 列表（游标分页，按 `id` 倒序）与详情；
- 原账期（`original_periods`）与当时已存在的原对账单（`statement_ids`）。

⚠️ **只读、不提交、全部是普通读，不加任何锁**（设计 v7 去掉了 `locking` 参数）：确认事务在
MySQL 上用 `READ COMMITTED`、先锁钱包，由 AIH-TASK-255 设置；锁后的普通读每条都看到最新已提交的
数据。不写 `usage_rebills` / `usage_rebill_lines`、不写账本。

⚠️ 每个函数都带 `tenant_id`，或只按主键 / 唯一键取（INV-8）。时间一律不带时区的 UTC。

原账期照对账单「账本归属规则」（docs/design/AIH-TASK-154-monthly-statements.md），直接用
app/services/statements.py 的 `attributed_month`，不另写一份。
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Final

from sqlalchemy import ColumnElement, Select, func, select
from sqlalchemy.orm import Session, aliased

from app.models.ai_catalog import AiModel, AiProvider
from app.models.auth import User
from app.models.pricing_rules import PricingRule
from app.models.provider_prices import ProviderPriceVersion
from app.models.statements import MonthlyStatement
from app.models.tenancy import BillingMode, Project, Tenant
from app.models.usage import UsageEvent, UsageEventStatus
from app.models.usage_rebills import MAX_EVENT_COUNT, UsageRebill, UsageRebillLine
from app.models.wallet import TransactionType, WalletTransaction
from app.services.statements import attributed_month, period_containing, statement_period

# 一条 IN 列表至多这么多个值：老版本 SQLite 的绑定参数上限是 999。
_CHUNK: Final = 500


def _chunks[T](values: Sequence[T]) -> Iterator[Sequence[T]]:
    for start in range(0, len(values), _CHUNK):
        yield values[start : start + _CHUNK]


def _naive(moment: dt.datetime, name: str) -> dt.datetime:
    if moment.tzinfo is not None:
        raise ValueError(f"{name} must be a naive UTC datetime")
    return moment


# --- 选事件 ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExplicitEvents:
    """The customer's events among the requested public ids, and the ids no event has.

    `events` 按 `id` 升序，状态与计费模式原样（是否可更正由服务判定：409）。
    既不在 `events` 也不在 `missing` 的 id 属于别的客户（409，不是 404）。
    """

    events: list[UsageEvent]
    # 没有任何事件的 public_id，升序（404）。
    missing: list[str]


def find_explicit_events(
    session: Session, tenant_id: int, public_ids: Iterable[str]
) -> ExplicitEvents:
    """Events of `tenant_id` by public id, plus which ids exist nowhere. Plain reads.

    别的客户的事件只查是否存在（按唯一键 `public_id`），不取行。
    """
    wanted = sorted(set(public_ids))
    events: list[UsageEvent] = []
    existing: set[str] = set()
    for chunk in _chunks(wanted):
        events.extend(
            session.execute(
                select(UsageEvent).where(
                    UsageEvent.tenant_id == tenant_id, UsageEvent.public_id.in_(chunk)
                )
            ).scalars()
        )
        existing.update(
            session.execute(
                select(UsageEvent.public_id).where(UsageEvent.public_id.in_(chunk))
            ).scalars()
        )
    events.sort(key=lambda row: row.id)
    return ExplicitEvents(events, [item for item in wanted if item not in existing])


@dataclass(frozen=True)
class CriteriaSelection:
    """The exact number of matching events and, when it is within the limit, the events.

    `match_count > limit` 时 `events` 为空（不取行），匹配数原样交给服务写进 422 的消息。
    """

    match_count: int
    # 按 `id` 升序。
    events: list[UsageEvent]


def _criteria(
    tenant_id: int,
    occurred_from: dt.datetime,
    occurred_to: dt.datetime,
    project_id: int | None,
    provider_code: str | None,
    model_code: str | None,
) -> list[ColumnElement[bool]]:
    conditions = [
        UsageEvent.tenant_id == tenant_id,
        UsageEvent.status == UsageEventStatus.PROCESSED,
        UsageEvent.billing_mode_snapshot == BillingMode.PREPAID.value,
        # 左闭右开。
        UsageEvent.occurred_at >= _naive(occurred_from, "occurred_from"),
        UsageEvent.occurred_at < _naive(occurred_to, "occurred_to"),
    ]
    if project_id is not None:
        conditions.append(UsageEvent.project_id == project_id)
    # 已计费事件都有目录引用：比目录的 code，不比上报的原始字符串。
    if provider_code is not None:
        conditions.append(
            UsageEvent.provider_id.in_(
                select(AiProvider.id).where(AiProvider.code == provider_code)
            )
        )
    if model_code is not None:
        conditions.append(
            UsageEvent.model_id.in_(select(AiModel.id).where(AiModel.code == model_code))
        )
    return conditions


def select_events(
    session: Session,
    tenant_id: int,
    *,
    occurred_from: dt.datetime,
    occurred_to: dt.datetime,
    project_id: int | None = None,
    provider_code: str | None = None,
    model_code: str | None = None,
    limit: int = MAX_EVENT_COUNT,
) -> CriteriaSelection:
    """The customer's PROCESSED prepaid events in `[occurred_from, occurred_to)`. Plain reads.

    `project_id` 是项目的内部 id（服务已按客户校验归属）；`provider_code` / `model_code` 比目录
    的 `code`。先 COUNT，匹配数不超过 `limit` 才取行。
    """
    conditions = _criteria(
        tenant_id, occurred_from, occurred_to, project_id, provider_code, model_code
    )
    count = session.execute(
        select(func.count()).select_from(UsageEvent).where(*conditions)
    ).scalar_one()
    if count > limit:
        return CriteriaSelection(int(count), [])
    events = list(
        session.execute(select(UsageEvent).where(*conditions).order_by(UsageEvent.id)).scalars()
    )
    return CriteriaSelection(int(count), events)


# --- 明细 -----------------------------------------------------------------------------


def latest_lines(session: Session, event_ids: Iterable[int]) -> dict[int, UsageRebillLine]:
    """Event id → its latest rebill line (largest line `id`); events without lines are absent."""
    wanted = sorted(set(event_ids))
    found: dict[int, UsageRebillLine] = {}
    for chunk in _chunks(wanted):
        latest = (
            select(func.max(UsageRebillLine.id))
            .where(UsageRebillLine.usage_event_id.in_(chunk))
            .group_by(UsageRebillLine.usage_event_id)
        )
        for line in session.execute(
            select(UsageRebillLine).where(UsageRebillLine.id.in_(latest))
        ).scalars():
            found[line.usage_event_id] = line
    return found


@dataclass(frozen=True)
class EventRebillLine:
    """One line of an event's correction history, with the rebill it belongs to."""

    line: UsageRebillLine
    rebill_id: str
    rebill_created_at: dt.datetime


def event_history(session: Session, event_id: int) -> list[EventRebillLine]:
    """Every rebill line of the event, line `id` ascending (用量事件详情的更正历史)."""
    statement = (
        select(UsageRebillLine, UsageRebill.public_id, UsageRebill.created_at)
        .join(UsageRebill, UsageRebill.id == UsageRebillLine.rebill_id)
        .where(UsageRebillLine.usage_event_id == event_id)
        .order_by(UsageRebillLine.id)
    )
    return [
        EventRebillLine(line, public_id, created_at)
        for line, public_id, created_at in session.execute(statement).all()
    ]


# --- Rebill ---------------------------------------------------------------------------


def get_by_idempotency_key(session: Session, idempotency_key: str) -> UsageRebill | None:
    """The rebill written under this key, if any (plain read; the unique key backs it)."""
    statement = select(UsageRebill).where(UsageRebill.idempotency_key == idempotency_key)
    return session.execute(statement).scalar_one_or_none()


def get_rebill_row(session: Session, public_id: str) -> UsageRebill | None:
    """The rebill by its public id: the list cursor and the detail start from it."""
    statement = select(UsageRebill).where(UsageRebill.public_id == public_id)
    return session.execute(statement).scalar_one_or_none()


@dataclass(frozen=True)
class RebillSummary:
    """A rebill with what its list row shows: customer, author and its ledger row."""

    rebill: UsageRebill
    customer_id: str
    created_by_email: str
    ledger_type: TransactionType
    ledger_amount: Decimal
    wallet_transaction_id: str


_SummaryRow = tuple[UsageRebill, str, str, TransactionType, Decimal, str]


def _summaries() -> Select[_SummaryRow]:
    return (
        select(
            UsageRebill,
            Tenant.public_id,
            User.email,
            WalletTransaction.transaction_type,
            WalletTransaction.amount,
            WalletTransaction.public_id,
        )
        .join(Tenant, Tenant.id == UsageRebill.tenant_id)
        .join(User, User.id == UsageRebill.created_by)
        .join(WalletTransaction, WalletTransaction.id == UsageRebill.wallet_transaction_id)
    )


def _summary(row: _SummaryRow) -> RebillSummary:
    rebill, customer_id, email, kind, amount, transaction_id = row
    return RebillSummary(rebill, customer_id, email, TransactionType(kind), amount, transaction_id)


@dataclass(frozen=True)
class RebillPage:
    """One page, `id` descending; `has_more` = a next page exists (no total is counted)."""

    items: list[RebillSummary]
    has_more: bool


def list_rebills(
    session: Session, *, tenant_id: int | None, before_id: int | None, limit: int
) -> RebillPage:
    """Rebills newest first, optionally of one customer. Cursor paging, no offset, no total.

    `before_id` 是游标 Rebill 的内部 id：给了就只取 `id` 小于它的行。取 `limit + 1` 行判断是否
    还有下一页（设计 §2「管理端接口」）。
    """
    if limit < 1:
        raise ValueError("limit must be at least 1")
    statement = _summaries()
    if tenant_id is not None:
        statement = statement.where(UsageRebill.tenant_id == tenant_id)
    if before_id is not None:
        statement = statement.where(UsageRebill.id < before_id)
    rows = session.execute(statement.order_by(UsageRebill.id.desc()).limit(limit + 1)).all()
    items = [_summary(tuple(row)) for row in rows[:limit]]
    return RebillPage(items, len(rows) > limit)


@dataclass(frozen=True)
class RebillLineDetail:
    """One line of a rebill with its references as public ids (规则、价格版本、对账单)."""

    line: UsageRebillLine
    usage_event_id: str
    occurred_at: dt.datetime
    project_id: str
    project_name: str
    provider: str
    model: str
    previous_pricing_rule_id: str
    corrected_pricing_rule_id: str
    previous_provider_price_version_id: str
    corrected_provider_price_version_id: str
    original_wallet_transaction_id: str | None
    original_statement_id: str | None


@dataclass(frozen=True)
class RebillDetail:
    summary: RebillSummary
    # 请求里的更正版本；没有为空。
    corrective_pricing_rule_id: str | None
    corrective_provider_price_version_id: str | None
    # 明细 `id` 升序。
    lines: list[RebillLineDetail]


def _rule_public_id(session: Session, rule_id: int | None) -> str | None:
    if rule_id is None:
        return None
    return session.execute(
        select(PricingRule.public_id).where(PricingRule.id == rule_id)
    ).scalar_one()


def _version_public_id(session: Session, version_id: int | None) -> str | None:
    if version_id is None:
        return None
    return session.execute(
        select(ProviderPriceVersion.public_id).where(ProviderPriceVersion.id == version_id)
    ).scalar_one()


def get_rebill(session: Session, public_id: str) -> RebillDetail | None:
    """The rebill, its two corrective references and every line (line `id` ascending)."""
    found = session.execute(_summaries().where(UsageRebill.public_id == public_id)).one_or_none()
    if found is None:
        return None
    summary = _summary(tuple(found))
    rebill = summary.rebill
    previous_rule = aliased(PricingRule)
    corrected_rule = aliased(PricingRule)
    previous_version = aliased(ProviderPriceVersion)
    corrected_version = aliased(ProviderPriceVersion)
    statement = (
        select(
            UsageRebillLine,
            UsageEvent.public_id,
            UsageEvent.occurred_at,
            Project.public_id,
            Project.name,
            AiProvider.code,
            AiModel.code,
            previous_rule.public_id,
            corrected_rule.public_id,
            previous_version.public_id,
            corrected_version.public_id,
            WalletTransaction.public_id,
            MonthlyStatement.public_id,
        )
        .join(UsageEvent, UsageEvent.id == UsageRebillLine.usage_event_id)
        .join(Project, Project.id == UsageEvent.project_id)
        .join(AiProvider, AiProvider.id == UsageEvent.provider_id)
        .join(AiModel, AiModel.id == UsageEvent.model_id)
        .join(previous_rule, previous_rule.id == UsageRebillLine.previous_pricing_rule_id)
        .join(corrected_rule, corrected_rule.id == UsageRebillLine.corrected_pricing_rule_id)
        .join(
            previous_version,
            previous_version.id == UsageRebillLine.previous_provider_price_version_id,
        )
        .join(
            corrected_version,
            corrected_version.id == UsageRebillLine.corrected_provider_price_version_id,
        )
        .outerjoin(
            WalletTransaction,
            WalletTransaction.id == UsageRebillLine.original_wallet_transaction_id,
        )
        .outerjoin(MonthlyStatement, MonthlyStatement.id == UsageRebillLine.original_statement_id)
        .where(UsageRebillLine.rebill_id == rebill.id)
        .order_by(UsageRebillLine.id)
    )
    lines = [RebillLineDetail(*row) for row in session.execute(statement).all()]
    return RebillDetail(
        summary=summary,
        corrective_pricing_rule_id=_rule_public_id(session, rebill.corrective_pricing_rule_id),
        corrective_provider_price_version_id=_version_public_id(
            session, rebill.corrective_provider_price_version_id
        ),
        lines=lines,
    )


# --- 原账期与原对账单 ---------------------------------------------------------------------


def original_periods(session: Session, events: Sequence[UsageEvent]) -> dict[int, str]:
    """Event id → `YYYY-MM`: the attributed month `t(r)` of its `AI_USAGE` row.

    照对账单「账本归属规则」：`occurred_at` 所在马来西亚月 `U`，账本行晚于 `cutoff(U)` 才入账的
    取它 `created_at` 所在马来西亚月。计费额为 0、没有账本行的事件取 `occurred_at` 所在马来西亚月。
    """
    transaction_ids = sorted(
        {event.wallet_transaction_id for event in events if event.wallet_transaction_id is not None}
    )
    created: dict[int, dt.datetime] = {}
    for chunk in _chunks(transaction_ids):
        rows = session.execute(
            select(WalletTransaction.id, WalletTransaction.created_at).where(
                WalletTransaction.id.in_(chunk)
            )
        ).all()
        created.update({row_id: created_at for row_id, created_at in rows})
    periods: dict[int, str] = {}
    for event in events:
        if event.wallet_transaction_id is None:
            periods[event.id] = period_containing(event.occurred_at).label
        else:
            periods[event.id] = attributed_month(
                TransactionType.AI_USAGE, created[event.wallet_transaction_id], event.occurred_at
            )
    return periods


def statement_ids(session: Session, tenant_id: int, periods: Iterable[str]) -> dict[str, int]:
    """`YYYY-MM` → the id of the customer's `monthly_statements` row for it, where one exists.

    行存在就是已定稿（生成即定稿）；没有的账期不在结果里。
    """
    starts = {statement_period(label).start: label for label in set(periods)}
    found: dict[str, int] = {}
    for chunk in _chunks(sorted(starts)):
        rows = session.execute(
            select(MonthlyStatement.id, MonthlyStatement.period_start).where(
                MonthlyStatement.tenant_id == tenant_id, MonthlyStatement.period_start.in_(chunk)
            )
        ).all()
        found.update({starts[period_start]: int(row_id) for row_id, period_start in rows})
    return found


__all__ = [
    "CriteriaSelection",
    "EventRebillLine",
    "ExplicitEvents",
    "RebillDetail",
    "RebillLineDetail",
    "RebillPage",
    "RebillSummary",
    "event_history",
    "find_explicit_events",
    "get_by_idempotency_key",
    "get_rebill",
    "get_rebill_row",
    "latest_lines",
    "list_rebills",
    "original_periods",
    "select_events",
    "statement_ids",
]

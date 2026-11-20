"""Provider cost reconciliation repository (design gate #755 v1 §2「仓储」, AIH-TASK-243).

范围内估算聚合、诊断计数与对账记录的读写。口径见设计 §2「口径」：

- 期间是 UTC 自然月 `[period_start, period_end)`；
  事件只看 `occurred_at`，outbox 只看 `created_at`；
- 范围 `AI_MODULE`：事件的 `event_id` 出现在 `ai_usage_outbox` 里
  （`ai_module_origin`），集成方用自己的密钥上报的同供应商用量不在范围内；
- 「当前版本」（链头）：没有任何行的 `supersedes_id` 指向它。

⚠️ 全部只用可移植 SQL（`COUNT`、`SUM`、`CASE`、`EXISTS`、比较）。
只读函数不加锁、不提交（一致读，不阻塞计费 worker，设计 §4）。
`insert_record` 只 `add` + `flush`：提交与唯一冲突的映射归服务层（AIH-TASK-244）。
计数交出 `int`、金额交出 `Decimal`（INV-10）。

⚠️ 对用量事件只读（INV-6）。对 outbox 只读 `event_id`、`status`、`created_at`
三列，不读 `payload_json`（INV-9）。
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any, Final, NamedTuple

from sqlalchemy import (
    ColumnElement,
    Numeric,
    case,
    column,
    exists,
    func,
    select,
    table,
    type_coerce,
)
from sqlalchemy.orm import Session, aliased

from app.models.ai_catalog import AiProvider
from app.models.base import MONEY_PRECISION, MONEY_SCALE
from app.models.provider_cost_reconciliations import ProviderCostReconciliation
from app.models.usage import UsageEvent, UsageEventStatus

# ⚠️ AI 模块的表只按表名与三列引用，不 import `app.ai_api`：
# AI 模块边界（设计闸门 #281 v23 §2，tests/backend/test_ai_module_boundary.py）
# 只许列出的几个模块反向 import 它（先例 app/services/account_closure.py）。
_AI_USAGE_OUTBOX = table(
    "ai_usage_outbox", column("event_id"), column("status"), column("created_at")
)

# 已被供应商计费、还没进估算的 outbox 行（`outbox_unsettled_count`）。
OUTBOX_UNSETTLED_STATUSES: Final = ("PENDING", "SENDING", "HELD", "DEAD")
# 永远不会有估算的 outbox 行（`outbox_discarded_count`）。
OUTBOX_DISCARDED_STATUS: Final = "DISCARDED"

# SQLite 把 DECIMAL 存成浮点：聚合结果按金额列的精度转回 Decimal。
# MySQL 上本来就是 DECIMAL，原样交出。
_MONEY = Numeric(MONEY_PRECISION, MONEY_SCALE, asdecimal=True)


class ScopeEstimate(NamedTuple):
    """The processed in-scope events of one provider and period (设计 §2「口径」)."""

    event_count: int
    # 来源币种等于录入币种的事件的 `provider_source_cost` 之和。
    estimated_source_cost: Decimal
    # 全部范围内已处理事件的 `estimated_provider_cost_myr` 之和。
    estimated_cost_myr: Decimal
    # 来源币种与录入币种不同的事件数（大于 0 时服务层 422）。
    currency_mismatch_count: int


def _total(expression: Any) -> ColumnElement[Any]:
    return func.coalesce(func.sum(expression), 0)


def _money_total(expression: Any) -> ColumnElement[Decimal]:
    return type_coerce(_total(expression), _MONEY)


def _successor() -> ColumnElement[bool]:
    """Some row supersedes the outer `ProviderCostReconciliation` row."""
    later = aliased(ProviderCostReconciliation)
    return exists().where(later.supersedes_id == ProviderCostReconciliation.id)


def _in_period(period_start: dt.datetime, period_end: dt.datetime) -> list[ColumnElement[bool]]:
    return [UsageEvent.occurred_at >= period_start, UsageEvent.occurred_at < period_end]


def ai_module_origin() -> ColumnElement[bool]:
    """The usage event was sent through the AI module: its `event_id` is in `ai_usage_outbox`.

    ⚠️ 直接等值比较，不加字符集转换。outbox 的 `event_id` 是 `ascii_bin`、
    事件的是 `utf8mb4_0900_bin`，MySQL 可能因此不能用 `uq_ai_usage_outbox_event_id`
    逐行探测（设计 §10 已知限制 1，计划见 PR 里的 `EXPLAIN`）；
    结果的正确性由 MySQL 用例钉住。依赖 outbox 行永不删除（设计 §10 假设 2）。
    """
    return exists().where(_AI_USAGE_OUTBOX.c.event_id == UsageEvent.event_id)


def _scope_conditions(
    *, provider_id: int, period_start: dt.datetime, period_end: dt.datetime
) -> list[ColumnElement[bool]]:
    return [
        UsageEvent.status == UsageEventStatus.PROCESSED,
        UsageEvent.provider_id == provider_id,
        *_in_period(period_start, period_end),
        ai_module_origin(),
    ]


def scope_estimate(
    session: Session,
    *,
    provider_id: int,
    period_start: dt.datetime,
    period_end: dt.datetime,
    source_currency: str,
) -> ScopeEstimate:
    """Event count, source-currency and MYR estimates and currency mismatches, in one SELECT."""
    statement = select(
        func.coalesce(func.count(), 0).label("event_count"),
        _money_total(
            case(
                (
                    UsageEvent.provider_source_currency == source_currency,
                    UsageEvent.provider_source_cost,
                ),
                else_=0,
            )
        ).label("estimated_source_cost"),
        _money_total(UsageEvent.estimated_provider_cost_myr).label("estimated_cost_myr"),
        _total(case((UsageEvent.provider_source_currency != source_currency, 1), else_=0)).label(
            "currency_mismatch_count"
        ),
    ).where(
        *_scope_conditions(
            provider_id=provider_id, period_start=period_start, period_end=period_end
        )
    )
    row = session.execute(statement).one()
    return ScopeEstimate(
        event_count=int(row.event_count),
        estimated_source_cost=Decimal(row.estimated_source_cost),
        estimated_cost_myr=Decimal(row.estimated_cost_myr),
        currency_mismatch_count=int(row.currency_mismatch_count),
    )


def scope_estimated_myr(
    session: Session, *, provider_id: int, period_start: dt.datetime, period_end: dt.datetime
) -> Decimal:
    """The MYR estimate of `scope_estimate` alone, for any source currency (分摊设计用)."""
    statement = select(_money_total(UsageEvent.estimated_provider_cost_myr)).where(
        *_scope_conditions(
            provider_id=provider_id, period_start=period_start, period_end=period_end
        )
    )
    return Decimal(session.execute(statement).scalar_one())


def unprocessed_count(
    session: Session, *, period_start: dt.datetime, period_end: dt.datetime
) -> int:
    """AI-module events of the period that are not `PROCESSED`, of any provider.

    不限供应商：未知模型的事件可能没有 `provider_id`（设计 §2「口径」）。
    """
    statement = (
        select(func.count())
        .select_from(UsageEvent)
        .where(
            UsageEvent.status != UsageEventStatus.PROCESSED,
            *_in_period(period_start, period_end),
            ai_module_origin(),
        )
    )
    return int(session.execute(statement).scalar_one())


def outbox_counts(
    session: Session, *, period_start: dt.datetime, period_end: dt.datetime
) -> tuple[int, int]:
    """`(unsettled, discarded)` outbox rows whose `created_at` falls in the period.

    按 `created_at` 归期是近似（设计 §10 假设 5）。
    """
    outbox = _AI_USAGE_OUTBOX.c
    statement = select(
        _total(case((outbox.status.in_(OUTBOX_UNSETTLED_STATUSES), 1), else_=0)).label("unsettled"),
        _total(case((outbox.status == OUTBOX_DISCARDED_STATUS, 1), else_=0)).label("discarded"),
    ).where(outbox.created_at >= period_start, outbox.created_at < period_end)
    row = session.execute(statement).one()
    return int(row.unsettled), int(row.discarded)


def scope_providers(
    session: Session, *, period_start: dt.datetime, period_end: dt.datetime
) -> list[tuple[int, str]]:
    """`(provider id, code)` of each provider with processed in-scope events, by code.

    告警用（AIH-TASK-250）：只要供应商，不要币种与金额。
    """
    statement = (
        select(UsageEvent.provider_id, AiProvider.code)
        .distinct()
        .join(AiProvider, AiProvider.id == UsageEvent.provider_id)
        .where(
            UsageEvent.status == UsageEventStatus.PROCESSED,
            *_in_period(period_start, period_end),
            ai_module_origin(),
        )
        .order_by(AiProvider.code)
    )
    return [(int(provider_id), code) for provider_id, code in session.execute(statement)]


# --- 对账记录 ------------------------------------------------------------------------


def current_record(
    session: Session, *, provider_id: int, scope: str, billing_month: str
) -> ProviderCostReconciliation | None:
    """The head of the month's chain (no row supersedes it); None when nothing is recorded."""
    statement = select(ProviderCostReconciliation).where(
        ProviderCostReconciliation.provider_id == provider_id,
        ProviderCostReconciliation.scope == scope,
        ProviderCostReconciliation.billing_month == billing_month,
        ~_successor(),
    )
    return session.execute(statement).scalar_one_or_none()


def get_by_public_id(session: Session, public_id: str) -> ProviderCostReconciliation | None:
    statement = select(ProviderCostReconciliation).where(
        ProviderCostReconciliation.public_id == public_id
    )
    return session.execute(statement).scalar_one_or_none()


def chain(
    session: Session, *, provider_id: int, scope: str, billing_month: str
) -> list[ProviderCostReconciliation]:
    """Every row of the month by `id`: the root first, then each correction."""
    statement = (
        select(ProviderCostReconciliation)
        .where(
            ProviderCostReconciliation.provider_id == provider_id,
            ProviderCostReconciliation.scope == scope,
            ProviderCostReconciliation.billing_month == billing_month,
        )
        .order_by(ProviderCostReconciliation.id)
    )
    return list(session.execute(statement).scalars())


def has_successor(session: Session, record_id: int) -> bool:
    statement = select(exists().where(ProviderCostReconciliation.supersedes_id == record_id))
    return bool(session.execute(statement).scalar_one())


def current_records_overlapping(
    session: Session, *, start: dt.datetime, end: dt.datetime
) -> list[tuple[ProviderCostReconciliation, str]]:
    """`(chain head, provider code)` of each period overlapping `[start, end)`, in one SELECT.

    按 `provider_id`、`period_start` 升序。⚠️ 供应商代码就从这里来、不另查：
    分摊设计的查询预算依赖这一点（AIH-TASK-247）。
    """
    statement = (
        select(ProviderCostReconciliation, AiProvider.code)
        .join(AiProvider, AiProvider.id == ProviderCostReconciliation.provider_id)
        .where(
            ProviderCostReconciliation.period_start < end,
            ProviderCostReconciliation.period_end > start,
            ~_successor(),
        )
        .order_by(ProviderCostReconciliation.provider_id, ProviderCostReconciliation.period_start)
    )
    return [(record, code) for record, code in session.execute(statement).tuples()]


def insert_record(session: Session, **fields: Any) -> ProviderCostReconciliation:
    """Add and flush one row; nothing is committed. `root_slot` is generated: never pass it.

    ⚠️ 唯一约束冲突的 `IntegrityError` 原样抛出：同月第二个根撞 `ux_…_root`，
    同一行第二次被取代撞 `uq_…_supersedes`。按请求形状映射为 409 是服务层的事
    （设计 §2「服务」第 8 步）。
    """
    record = ProviderCostReconciliation(**fields)
    session.add(record)
    session.flush()
    return record


__all__ = [
    "OUTBOX_DISCARDED_STATUS",
    "OUTBOX_UNSETTLED_STATUSES",
    "ScopeEstimate",
    "ai_module_origin",
    "chain",
    "current_record",
    "current_records_overlapping",
    "get_by_public_id",
    "has_successor",
    "insert_record",
    "outbox_counts",
    "scope_estimate",
    "scope_estimated_myr",
    "scope_providers",
    "unprocessed_count",
]

"""Read-only grouped usage analytics queries.

AIH-TASK-221（设计闸门 #709 v2 §2「仓储」）：按马来西亚日 / 月分组、与分组同列同条件的全量合计、
`paid_at` 落在区间内的 `PAID` 支付汇总。
AIH-TASK-247（设计闸门 #756 v2 §2「仓储」）：按与区间重叠的当前对账记录汇总覆盖范围内的估算
（`reconciled_allocation_inputs`），分摊与舍入归接口层。公开函数都只读、不加锁、不提交。
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Final, NamedTuple

from sqlalchemy import (
    ColumnElement,
    Integer,
    Numeric,
    String,
    case,
    cast,
    func,
    select,
    type_coerce,
)
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session
from sqlalchemy.sql.compiler import SQLCompiler
from sqlalchemy.sql.functions import FunctionElement

from app.core.fx_source import KUALA_LUMPUR_OFFSET
from app.models.ai_catalog import AiModel, AiProvider, PayloadShape
from app.models.base import MONEY_PRECISION, MONEY_SCALE
from app.models.payments import Payment, PaymentStatus
from app.models.tenancy import BillingMode, Project, Tenant
from app.models.usage import UsageEvent, UsageEventStatus
from app.repositories.provider_cost_reconciliations import (
    ai_module_origin,
    current_records_overlapping,
    scope_estimated_myr,
)
from app.repositories.usage_events import MalaysiaDay

# 马来西亚月表达式里的小时数，与日期换算同一个偏移（固定 UTC+8）。
_KUALA_LUMPUR_HOURS: Final = int(KUALA_LUMPUR_OFFSET.total_seconds()) // 3600
# 只汇总 MYR 的支付：数据库对 `confirmed_currency` 没有 CHECK（设计 §2「仓储」）。
_PAYMENT_CURRENCY: Final = "MYR"
# SQLite 把 DECIMAL 存成浮点：覆盖估算之和按金额列的精度转回 Decimal（同对账仓储）。
_MONEY: Final = Numeric(MONEY_PRECISION, MONEY_SCALE, asdecimal=True)


class MalaysiaMonth(FunctionElement[int]):
    """The Malaysian calendar month of a naive-UTC datetime column as a `YYYYMM` integer.

    MySQL：`EXTRACT(YEAR_MONTH FROM DATE_ADD(col, INTERVAL 8 HOUR))`（没有 `%`，见设计 §9 备选 E）；
    SQLite（只在测试里用）：`CAST(strftime('%Y%m', substr(col, 1, 19), '+8 hours') AS INTEGER)`。
    """

    type = Integer()
    name = "malaysia_month"
    inherit_cache = True


@compiles(MalaysiaMonth)
def _malaysia_month_sqlite(element: MalaysiaMonth, compiler: SQLCompiler, **kw: Any) -> str:
    column = compiler.process(element.clauses, **kw)
    # 同 MalaysiaDay：先截到整秒，避免 SQLite 按毫秒取整把月末最后一刻进到下个月。
    return (
        f"CAST(strftime('%Y%m', substr({column}, 1, 19), '+{_KUALA_LUMPUR_HOURS} hours')"
        " AS INTEGER)"
    )


@compiles(MalaysiaMonth, "mysql")
def _malaysia_month_mysql(element: MalaysiaMonth, compiler: SQLCompiler, **kw: Any) -> str:
    column = compiler.process(element.clauses, **kw)
    return f"EXTRACT(YEAR_MONTH FROM DATE_ADD({column}, INTERVAL {_KUALA_LUMPUR_HOURS} HOUR))"


class UsageAnalyticsRow(NamedTuple):
    group_key: str | None
    group_name: str | None
    event_count: int
    token_event_count: int
    input_tokens: int
    output_tokens: int
    cache_creation_input_tokens: int
    cache_read_input_tokens: int
    prepaid_event_count: int
    prepaid_billable_cost: Decimal
    prepaid_estimated_cost: Decimal
    internal_event_count: int
    internal_estimated_cost: Decimal
    internal_reference_price: Decimal


class UsageAnalyticsTotals(NamedTuple):
    """The same 12 aggregates as a group row, over every group (no grouping, no paging)."""

    event_count: int
    token_event_count: int
    input_tokens: int
    output_tokens: int
    cache_creation_input_tokens: int
    cache_read_input_tokens: int
    prepaid_event_count: int
    prepaid_billable_cost: Decimal
    prepaid_estimated_cost: Decimal
    internal_event_count: int
    internal_estimated_cost: Decimal
    internal_reference_price: Decimal


class PaidPaymentTotals(NamedTuple):
    """`PAID` MYR payments whose `paid_at` falls in the period (cash period, not revenue)."""

    paid_count: int
    topup_amount: Decimal
    gateway_fee: Decimal
    gateway_fee_unknown_count: int


@dataclass(frozen=True)
class CoveringRecord:
    """One current reconciliation overlapping the period (设计 #756 §2「仓储」)."""

    # 对账记录的 public_id，也是 `AllocationInputs` 两个字典里的记录键。
    public_id: str
    provider_code: str
    # "YYYY-MM"
    billing_month: str
    # 不带时区的 UTC。
    period_start: dt.datetime
    period_end: dt.datetime
    # R
    reconciled_cost_myr: Decimal
    # E：实时、整个期间、不受报表筛选影响。
    scope_estimated_myr: Decimal


@dataclass(frozen=True)
class AllocationInputs:
    """What the allocation needs; keys absent from the dicts count as 0.

    模式键是 `billing_mode_snapshot` 的原值（`"PREPAID"` / `"INTERNAL_METERED_ONLY"`）；
    `total_covered[(r, m)]` 恰好等于 `group_covered` 里同一 `(r, m)` 各项之和。
    """

    # 按 provider_id、period_start 升序。
    records: tuple[CoveringRecord, ...]
    # (group_key, 记录 public_id, 模式) -> covered
    group_covered: dict[tuple[str | None, str, str], Decimal]
    # (记录 public_id, 模式) -> covered
    total_covered: dict[tuple[str, str], Decimal]


_ZERO_TOTALS: Final = UsageAnalyticsTotals(
    0, 0, 0, 0, 0, 0, 0, Decimal(0), Decimal(0), 0, Decimal(0), Decimal(0)
)
_ZERO_PAYMENTS: Final = PaidPaymentTotals(0, Decimal(0), Decimal(0), 0)


def _sum(expression: ColumnElement[object]) -> ColumnElement[object]:
    return func.coalesce(func.sum(expression), 0)


def _conditions(
    session: Session,
    *,
    customer_id: str | None,
    project_id: str | None,
    provider: str | None,
    model: str | None,
    feature: str | None,
    occurred_from: dt.datetime,
    occurred_to: dt.datetime,
) -> list[ColumnElement[bool]] | None:
    """Only processed events; filters use the list endpoint's raw-value semantics.

    `None`：`customer_id` 或 `project_id` 查不到，结果必为空，调用方不再查库。
    """
    conditions: list[ColumnElement[bool]] = [
        UsageEvent.status == UsageEventStatus.PROCESSED,
        UsageEvent.occurred_at >= occurred_from,
        UsageEvent.occurred_at < occurred_to,
    ]
    if customer_id is not None:
        tenant_id = session.execute(
            select(Tenant.id).where(Tenant.public_id == customer_id)
        ).scalar_one_or_none()
        if tenant_id is None:
            return None
        conditions.append(UsageEvent.tenant_id == tenant_id)
    if project_id is not None:
        project_row_id = session.execute(
            select(Project.id).where(Project.public_id == project_id)
        ).scalar_one_or_none()
        if project_row_id is None:
            return None
        conditions.append(UsageEvent.project_id == project_row_id)
    if provider is not None:
        conditions.append(UsageEvent.provider_code_raw == provider)
    if model is not None:
        conditions.append(UsageEvent.model_code_raw == model)
    if feature is not None:
        conditions.append(UsageEvent.feature == feature)
    return conditions


def _aggregates() -> list[ColumnElement[Any]]:
    """The 12 aggregate columns shared by the grouped query and the totals, in row order."""
    token = UsageEvent.payload_shape == PayloadShape.LLM_TOKEN_FIELDS
    prepaid = UsageEvent.billing_mode_snapshot == BillingMode.PREPAID.value
    internal = UsageEvent.billing_mode_snapshot == BillingMode.INTERNAL_METERED_ONLY.value
    return [
        func.count(UsageEvent.id).label("event_count"),
        _sum(case((token, 1), else_=0)).label("token_event_count"),
        _sum(case((token, UsageEvent.input_tokens), else_=0)).label("input_tokens"),
        _sum(case((token, UsageEvent.output_tokens), else_=0)).label("output_tokens"),
        _sum(case((token, UsageEvent.cache_creation_input_tokens), else_=0)).label(
            "cache_creation_input_tokens"
        ),
        _sum(case((token, UsageEvent.cache_read_input_tokens), else_=0)).label(
            "cache_read_input_tokens"
        ),
        _sum(case((prepaid, 1), else_=0)).label("prepaid_event_count"),
        _sum(case((prepaid, UsageEvent.billable_cost), else_=0)).label("prepaid_billable_cost"),
        _sum(case((prepaid, UsageEvent.estimated_provider_cost_myr), else_=0)).label(
            "prepaid_estimated_cost"
        ),
        _sum(case((internal, 1), else_=0)).label("internal_event_count"),
        _sum(case((internal, UsageEvent.estimated_provider_cost_myr), else_=0)).label(
            "internal_estimated_cost"
        ),
        _sum(case((internal, UsageEvent.reference_customer_price), else_=0)).label(
            "internal_reference_price"
        ),
    ]


def _aggregate_values(row: Any) -> dict[str, Any]:
    # MySQL 的 SUM 对整数 CASE 返回 DECIMAL：计数交出 int，金额交出 Decimal。
    return {
        "event_count": int(row.event_count),
        "token_event_count": int(row.token_event_count),
        "input_tokens": int(row.input_tokens),
        "output_tokens": int(row.output_tokens),
        "cache_creation_input_tokens": int(row.cache_creation_input_tokens),
        "cache_read_input_tokens": int(row.cache_read_input_tokens),
        "prepaid_event_count": int(row.prepaid_event_count),
        "prepaid_billable_cost": Decimal(row.prepaid_billable_cost),
        "prepaid_estimated_cost": Decimal(row.prepaid_estimated_cost),
        "internal_event_count": int(row.internal_event_count),
        "internal_estimated_cost": Decimal(row.internal_estimated_cost),
        "internal_reference_price": Decimal(row.internal_reference_price),
    }


def _period_key(group_by: str, value: Any) -> str | None:
    """日键 `YYYY-MM-DD`、月键 `YYYY-MM`；其他分组原样。"""
    if value is None:
        return None
    if group_by == "day":
        return value.isoformat()
    if group_by == "month":
        number = int(value)
        return f"{number // 100:04d}-{number % 100:02d}"
    return value


def list_usage_analytics(
    session: Session,
    *,
    group_by: str,
    offset: int,
    limit: int,
    customer_id: str | None,
    project_id: str | None,
    provider: str | None,
    model: str | None,
    feature: str | None,
    occurred_from: dt.datetime,
    occurred_to: dt.datetime,
) -> tuple[list[UsageAnalyticsRow], int]:
    """Aggregate only processed events; filters use the list endpoint's raw-value semantics."""
    conditions = _conditions(
        session,
        customer_id=customer_id,
        project_id=project_id,
        provider=provider,
        model=model,
        feature=feature,
        occurred_from=occurred_from,
        occurred_to=occurred_to,
    )
    if conditions is None:
        return [], 0

    group_key, group_name, joins = _grouping(group_by)
    by_period = group_by in {"day", "month"}

    # 排序用未加标签的表达式：ORDER BY 文本与 AIH-TASK-073 相同。
    event_count = func.count(UsageEvent.id)
    statement = select(
        group_key.label("group_key"),
        group_name.label("group_name")
        if group_name is not None
        else cast(None, String).label("group_name"),
        *_aggregates(),
    ).select_from(UsageEvent)
    for table, onclause in joins:
        statement = statement.outerjoin(table, onclause)
    group_columns = (group_key,) if group_name is None else (group_key, group_name)
    statement = statement.where(*conditions).group_by(*group_columns)
    count_statement = select(func.count()).select_from(statement.subquery())
    # 日 / 月按时间顺序（分组键升序），不按事件数；其他维度事件数倒序、分组键升序。
    ordering = (group_key.asc(),) if by_period else (event_count.desc(), group_key.asc())
    statement = statement.order_by(*ordering).offset(offset).limit(limit)
    rows = [
        UsageAnalyticsRow(
            group_key=_period_key(group_by, row.group_key),
            group_name=row.group_name,
            **_aggregate_values(row),
        )
        for row in session.execute(statement).all()
    ]
    return rows, int(session.execute(count_statement).scalar_one())


def _grouping(group_by: str) -> tuple[Any, Any, list[Any]]:
    """`(group key, group name or None, outer joins)` of one dimension.

    `list_usage_analytics` 与 `reconciled_allocation_inputs` 共用（设计 #756 §2「仓储」第 3 步）。
    """
    joins: list[Any] = []
    group_key: Any
    group_name: Any
    if group_by == "day":
        group_key, group_name = MalaysiaDay(UsageEvent.occurred_at), None
    elif group_by == "month":
        group_key, group_name = MalaysiaMonth(UsageEvent.occurred_at), None
    elif group_by == "provider":
        group_key, group_name = AiProvider.code, None
        joins.append((AiProvider, AiProvider.id == UsageEvent.provider_id))
    elif group_by == "model":
        group_key = AiProvider.code + ":" + AiModel.code
        group_name = None
        joins.extend(
            (
                (AiProvider, AiProvider.id == UsageEvent.provider_id),
                (AiModel, AiModel.id == UsageEvent.model_id),
            )
        )
    elif group_by == "customer":
        group_key, group_name = Tenant.public_id, Tenant.company_name
        joins.append((Tenant, Tenant.id == UsageEvent.tenant_id))
    elif group_by == "project":
        group_key, group_name = Project.public_id, Project.name
        joins.append((Project, Project.id == UsageEvent.project_id))
    else:
        group_key, group_name = UsageEvent.feature, None
    return group_key, group_name, joins


def usage_analytics_totals(
    session: Session,
    *,
    customer_id: str | None,
    project_id: str | None,
    provider: str | None,
    model: str | None,
    feature: str | None,
    occurred_from: dt.datetime,
    occurred_to: dt.datetime,
) -> UsageAnalyticsTotals:
    """The grouped query's 12 aggregates and conditions over all groups: exactly one row."""
    conditions = _conditions(
        session,
        customer_id=customer_id,
        project_id=project_id,
        provider=provider,
        model=model,
        feature=feature,
        occurred_from=occurred_from,
        occurred_to=occurred_to,
    )
    if conditions is None:
        return _ZERO_TOTALS
    statement = select(*_aggregates()).select_from(UsageEvent).where(*conditions)
    return UsageAnalyticsTotals(**_aggregate_values(session.execute(statement).one()))


def reconciled_allocation_inputs(
    session: Session,
    *,
    group_by: str,
    customer_id: str | None,
    project_id: str | None,
    provider: str | None,
    model: str | None,
    feature: str | None,
    occurred_from: dt.datetime,
    occurred_to: dt.datetime,
) -> AllocationInputs:
    """The covered estimates of each current record overlapping the period (设计 #756 §2「仓储」).

    `_conditions` 的查找 L 次；为 None 时就此返回空输入，否则再发 1 + 2n 条
    （n 为重叠记录数）：每条记录一次实时 E、一次按分组与计费模式的聚合。
    合计在 Python 里由分组项加出，没有不分组的合计查询。不分摊、不舍入。
    """
    conditions = _conditions(
        session,
        customer_id=customer_id,
        project_id=project_id,
        provider=provider,
        model=model,
        feature=feature,
        occurred_from=occurred_from,
        occurred_to=occurred_to,
    )
    if conditions is None:
        return AllocationInputs(records=(), group_covered={}, total_covered={})

    group_key, _, joins = _grouping(group_by)
    records: list[CoveringRecord] = []
    group_covered: dict[tuple[str | None, str, str], Decimal] = {}
    total_covered: dict[tuple[str, str], Decimal] = {}
    for record, provider_code in current_records_overlapping(
        session, start=occurred_from, end=occurred_to
    ):
        period_start, period_end = record.period_start, record.period_end
        records.append(
            CoveringRecord(
                public_id=record.public_id,
                provider_code=provider_code,
                billing_month=record.billing_month,
                period_start=period_start,
                period_end=period_end,
                reconciled_cost_myr=Decimal(record.reconciled_cost_myr),
                scope_estimated_myr=scope_estimated_myr(
                    session,
                    provider_id=record.provider_id,
                    period_start=period_start,
                    period_end=period_end,
                ),
            )
        )
        statement = select(
            group_key.label("group_key"),
            UsageEvent.billing_mode_snapshot.label("billing_mode"),
            type_coerce(func.sum(UsageEvent.estimated_provider_cost_myr), _MONEY).label("covered"),
        ).select_from(UsageEvent)
        for table, onclause in joins:
            statement = statement.outerjoin(table, onclause)
        statement = statement.where(
            *conditions,
            UsageEvent.occurred_at >= max(occurred_from, period_start),
            UsageEvent.occurred_at < min(occurred_to, period_end),
            UsageEvent.provider_id == record.provider_id,
            ai_module_origin(),
        ).group_by(group_key, UsageEvent.billing_mode_snapshot)
        for row in session.execute(statement).all():
            mode = str(row.billing_mode)
            covered = Decimal(row.covered)
            group_covered[(_period_key(group_by, row.group_key), record.public_id, mode)] = covered
            total = (record.public_id, mode)
            total_covered[total] = total_covered.get(total, Decimal(0)) + covered
    return AllocationInputs(
        records=tuple(records), group_covered=group_covered, total_covered=total_covered
    )


def paid_payment_totals(
    session: Session,
    *,
    customer_id: str | None,
    paid_from: dt.datetime,
    paid_to: dt.datetime,
) -> PaidPaymentTotals:
    """`PAID` MYR payments with `paid_from <= paid_at < paid_to`, optionally of one customer.

    手续费只加非空的；为空的笔数单独计。只用可移植的 SQL（COUNT、SUM、CASE、比较）。
    """
    conditions: list[ColumnElement[bool]] = [
        Payment.status == PaymentStatus.PAID,
        Payment.confirmed_currency == _PAYMENT_CURRENCY,
        Payment.paid_at >= paid_from,
        Payment.paid_at < paid_to,
    ]
    if customer_id is not None:
        tenant_id = session.execute(
            select(Tenant.id).where(Tenant.public_id == customer_id)
        ).scalar_one_or_none()
        if tenant_id is None:
            return _ZERO_PAYMENTS
        conditions.append(Payment.tenant_id == tenant_id)
    statement = (
        select(
            func.count().label("paid_count"),
            _sum(Payment.confirmed_amount).label("topup_amount"),
            _sum(Payment.gateway_fee).label("gateway_fee"),
            _sum(case((Payment.gateway_fee.is_(None), 1), else_=0)).label(
                "gateway_fee_unknown_count"
            ),
        )
        .select_from(Payment)
        .where(*conditions)
    )
    row = session.execute(statement).one()
    return PaidPaymentTotals(
        paid_count=int(row.paid_count),
        topup_amount=Decimal(row.topup_amount),
        gateway_fee=Decimal(row.gateway_fee),
        gateway_fee_unknown_count=int(row.gateway_fee_unknown_count),
    )

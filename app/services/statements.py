"""Monthly statements: periods, ledger, usage, snapshot and generation (gate #565 v6 §2).

AIH-TASK-155 这一层：账期边界与到期判定、账本行的归属月 `t(r)`
（Python 参考函数与只含常量的 SQL 谓词）、给定高水位下本期账本行 `S(M)`
的分类汇总与期初期末，以及自检第 1 条。
AIH-TASK-156 这一层：本期用量统计 `E(M)`、自检第 2、3 条、固定键集合的快照、
合格性判定与 `generate_statement`。
AIH-TASK-157 这一层：一个租户的 catch-up `catch_up_tenant` 与每小时的 `generate_due`
（每个 (租户, 账期) 一个事务）；Celery 任务在 app/tasks/statements.py，审计属于 AIH-TASK-159。

⚠️ **不写账本与钱包**（INV-4、INV-5）：账本与事件只读、不加锁；唯一的写是
`generate_statement` 的一条 `monthly_statements` INSERT，只 flush、不提交；
提交只在 `catch_up_tenant`。
唯一的锁是 `resolve_tax_policy` 对税务政策的共享读（设计 §4）。
每条查询都带 `tenant_id`（INV-8）。金额在 SQL 里用 `SUM` 在 `DECIMAL` 上求和，
Python 侧只有 `Decimal` 加减，不经 float（INV-10）。
快照只来自账本金额与 `billable_cost`，不读成本、汇率、加价、价格版本与规则列（INV-7）。

⚠️ 库里所有时刻都是 naive UTC；账期是马来西亚日历月（固定 UTC+8）。
账期标签先判形状与范围（2000-01 到 2999-12），再做任何日期运算：
极值得到 `ValueError`，而不是换算溢出的 `OverflowError`。

⚠️ **不假设 `created_at` 与 `occurred_at` 的先后**，也不引用任何时间容差
（设计 §2「候选扫描」）：候选范围只是上界，每一行再用完整谓词判定归属。

⚠️ 连不上事件的 `AI_USAGE` 行的 `occurred_at` 为 NULL，归属谓词对它为 NULL，
会被 WHERE 悄悄滤掉；所以在从账本侧出发的每段候选范围上另数一次这种行，
有就是数据缺陷（`missing_event`）。
"""

from __future__ import annotations

import datetime as dt
import logging
import re
import secrets
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Final, NoReturn

from sqlalchemy import ColumnElement, Select, and_, func, not_, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.core.fx_source import KUALA_LUMPUR_OFFSET
from app.models.ai_catalog import AiModel, AiProvider
from app.models.statements import MonthlyStatement, StatementStatus
from app.models.tax_policy import TaxJurisdiction, TaxPolicyType
from app.models.tenancy import BillingMode, Project, Tenant
from app.models.usage import UsageEvent, UsageEventStatus
from app.models.wallet import TransactionType, Wallet, WalletTransaction
from app.repositories.tax_policy import DocumentNotice, ResolvedTaxPolicy, resolve_tax_policy
from app.repositories.usage_events import MalaysiaDay
from app.schemas.customer_usage import malaysia_day_start_utc
from app.schemas.customers import money_text

logger = logging.getLogger(__name__)

# 设计 §2：到期 = `now ≥ cutoff(M) + 1 小时`。
# 余量只为让宽限日最后几秒处理的事件提交完，金额的正确性不依赖它。
GENERATION_DELAY: Final = dt.timedelta(hours=1)
# 设计 §2：早于它的账期一律不生成（不回补）。不读环境变量、不设覆盖参数。
FIRST_STATEMENT_PERIOD: Final = "2026-12"

# 账期标签的形状与年份范围（两端都含）。
_PERIOD_LABEL: Final = re.compile(r"([0-9]{4})-([0-9]{2})")
_MIN_PERIOD_YEAR: Final = 2000
_MAX_PERIOD_YEAR: Final = 2999

# 设计 §2「发生额分类」。
_ADJUSTMENT_TYPES: Final = frozenset(
    {
        TransactionType.ADJUSTMENT_CREDIT,
        TransactionType.ADJUSTMENT_DEBIT,
        TransactionType.BONUS,
        TransactionType.REFUND_ADJUSTMENT,
        TransactionType.SYSTEM_CORRECTION,
    }
)
_REBILL_TYPES: Final = frozenset({TransactionType.REBILL_CREDIT, TransactionType.REBILL_DEBIT})

# 自检失败时的 `check`（设计 §2「自检」）。
CHECK_BALANCE: Final = "1"
CHECK_CHARGES: Final = "2"
CHECK_CHARGED_EVENTS: Final = "3"
CHECK_MISSING_EVENT: Final = "missing_event"

# 设计 §2「快照」。
SNAPSHOT_SCHEMA_VERSION: Final = 1
STATEMENT_CURRENCY: Final = "MYR"
# 设计 §2「生成函数」第 6 步：`ST-<YYYYMM>-` 加 8 个 Crockford base32 随机字符。
_CROCKFORD_BASE32: Final = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_STATEMENT_NUMBER_RANDOM_LENGTH: Final = 8


class StatementIntegrityError(Exception):
    """A self-check failed; `check` names it (`"1"`, `"2"`, `"3"` or `"missing_event"`).
    Carries no amount."""

    def __init__(self, check: str) -> None:
        super().__init__(f"statement integrity check failed: {check}")
        self.check = check


class StatementNotDue(Exception):
    """The period is before `FIRST_STATEMENT_PERIOD` or not yet due."""


class StatementNotEligible(Exception):
    """The tenant gets no statement for the period (design §2「合格租户」)."""


class PreviousStatementMissing(Exception):
    """The previous period is eligible but has no statement: the `A` chain would break."""


class StatementTaxPolicyUnavailable(Exception):
    """No tax policy in effect at the cutoff, or the one found fails its check on read.

    不默认「无税」（设计 §2「生成函数」第 4 步）。"""


# --- 账期与时间 -------------------------------------------------------------------------


@dataclass(frozen=True)
class StatementPeriod:
    """A Malaysian calendar month `YYYY-MM`; `start`, `end` and `cutoff` are naive UTC."""

    label: str
    start: dt.datetime
    end: dt.datetime
    cutoff: dt.datetime


def _year_month(label: str) -> tuple[int, int]:
    found = _PERIOD_LABEL.fullmatch(label) if isinstance(label, str) else None
    if found is None:
        raise ValueError("statement period must be YYYY-MM")
    year, month = int(found[1]), int(found[2])
    if not _MIN_PERIOD_YEAR <= year <= _MAX_PERIOD_YEAR or not 1 <= month <= 12:
        raise ValueError("statement period out of range")
    return year, month


def _label(year: int, month: int) -> str:
    return f"{year:04d}-{month:02d}"


def statement_period(label: str) -> StatementPeriod:
    """`start` = the 1st, `end` = the next month's 1st, `cutoff` = its 2nd (00:00 in Malaysia).

    范围在任何日期运算之前判：不是 `YYYY-MM`、年份不在 2000–2999、
    月份不在 1–12，都是 `ValueError`。
    """
    year, month = _year_month(label)
    next_first = dt.date(year + 1, 1, 1) if month == 12 else dt.date(year, month + 1, 1)
    return StatementPeriod(
        label=_label(year, month),
        start=malaysia_day_start_utc(dt.date(year, month, 1)),
        end=malaysia_day_start_utc(next_first),
        cutoff=malaysia_day_start_utc(next_first + dt.timedelta(days=1)),
    )


def previous_period(period: StatementPeriod) -> StatementPeriod:
    """The month before; `ValueError` for `2000-01`."""
    year, month = _year_month(period.label)
    return statement_period(_label(year - 1, 12) if month == 1 else _label(year, month - 1))


def _next_period(period: StatementPeriod) -> StatementPeriod:
    year, month = _year_month(period.label)
    return statement_period(_label(year + 1, 1) if month == 12 else _label(year, month + 1))


def period_containing(moment: dt.datetime) -> StatementPeriod:
    """The Malaysian month a naive-UTC moment falls in; `ValueError` outside 2000–2999."""
    try:
        local = moment + KUALA_LUMPUR_OFFSET
    except OverflowError:
        raise ValueError("statement period out of range") from None
    return statement_period(_label(local.year, local.month))


def is_due(period: StatementPeriod, now: dt.datetime) -> bool:
    """Exactly `now >= cutoff + GENERATION_DELAY`."""
    return now >= period.cutoff + GENERATION_DELAY


def due_periods(now: dt.datetime, *, first: str = FIRST_STATEMENT_PERIOD) -> list[StatementPeriod]:
    """From `first` to the latest due period, ascending; empty when `first` is not due."""
    periods: list[StatementPeriod] = []
    period = statement_period(first)
    while is_due(period, now):
        periods.append(period)
        try:
            period = _next_period(period)
        except ValueError:
            break
    return periods


# --- 账本归属规则 -----------------------------------------------------------------------


def attributed_month(
    transaction_type: TransactionType | str,
    created_at: dt.datetime,
    occurred_at: dt.datetime | None,
) -> str:
    """`t(r)` of design §2 straight from its definition, as `YYYY-MM`.

    `AI_USAGE`：设事件 `occurred_at` 所在月为 `U`，`created_at < cutoff(U)`
    时是 `U`，否则是 `created_at` 所在月（晚到）。其他类型：`created_at`
    所在月。`AI_USAGE` 没有 `occurred_at`、或其他类型带了，都是 `ValueError`。
    """
    kind = TransactionType(transaction_type)
    if kind is TransactionType.AI_USAGE:
        if occurred_at is None:
            raise ValueError("an AI_USAGE row needs the occurred_at of its event")
        usage = period_containing(occurred_at)
        if created_at < usage.cutoff:
            return usage.label
        return period_containing(created_at).label
    if occurred_at is not None:
        raise ValueError("only an AI_USAGE row has an occurred_at")
    return period_containing(created_at).label


def attributed_through(period: StatementPeriod) -> ColumnElement[bool]:
    """`t(r) <= K` with constants only, over `wallet_transactions LEFT JOIN usage_events`.

    非 AI_USAGE：`created_at < end(K)`；AI_USAGE：`occ < end(K) AND
    (created_at < end(K) OR (occ >= start(K) AND created_at < cutoff(K)))`。
    连不上事件的 `AI_USAGE` 行（`occ` 为 NULL）结果为 NULL，取反也是 NULL。
    """
    created = WalletTransaction.created_at
    occurred = UsageEvent.occurred_at
    return or_(
        and_(WalletTransaction.transaction_type != TransactionType.AI_USAGE, created < period.end),
        and_(
            WalletTransaction.transaction_type == TransactionType.AI_USAGE,
            occurred < period.end,
            or_(
                created < period.end,
                and_(occurred >= period.start, created < period.cutoff),
            ),
        ),
    )


# --- 候选扫描 ---------------------------------------------------------------------------


def _row_columns() -> Select[tuple[object, ...]]:
    return select(
        WalletTransaction.id.label("id"),
        WalletTransaction.transaction_type.label("transaction_type"),
        WalletTransaction.amount.label("amount"),
        WalletTransaction.created_at.label("created_at"),
        UsageEvent.occurred_at.label("occurred_at"),
    )


def _from_ledger(conditions: Iterable[ColumnElement[bool]]) -> Select[tuple[object, ...]]:
    """Ledger rows in a range, each with its event's `occurred_at` (NULL when it has none)."""
    return (
        _row_columns()
        .select_from(WalletTransaction)
        .outerjoin(UsageEvent, UsageEvent.wallet_transaction_id == WalletTransaction.id)
        .where(*conditions)
    )


def _c1_range(
    tenant_id: int, period: StatementPeriod, high_sequence: int
) -> list[ColumnElement[bool]]:
    """`C1(K)` up to `h`: `created_at >= end(K)` (`ix_wallet_transactions_tenant_created`)."""
    return [
        WalletTransaction.tenant_id == tenant_id,
        WalletTransaction.created_at >= period.end,
        WalletTransaction.wallet_sequence <= high_sequence,
    ]


def _segment_one_range(
    tenant_id: int, high_sequence: int, previous_high_sequence: int
) -> list[ColumnElement[bool]]:
    """`hw_prev < seq <= hw` (`ix_wallet_transactions_tenant_sequence`)."""
    return [
        WalletTransaction.tenant_id == tenant_id,
        WalletTransaction.wallet_sequence > previous_high_sequence,
        WalletTransaction.wallet_sequence <= high_sequence,
    ]


def later_rows(
    tenant_id: int, period: StatementPeriod, *, high_sequence: int
) -> list[Select[tuple[object, ...]]]:
    """`L(K; h)` = rows with `seq <= h` and `t(r) > K`: two SELECTs disjoint by `created_at`.

    C1 从账本侧取 `created_at >= end(K)` 的行；C2 从事件侧按 `occ >= end(K)`
    （`ix_usage_events_tenant_occurred`）取 `created_at < end(K)` 的 `AI_USAGE` 行，
    按 `wallet_transaction_id` 连账本主键。两段的每一行都再用
    `NOT attributed_through(K)` 判定。
    """
    outside = not_(attributed_through(period))
    c1 = _from_ledger([*_c1_range(tenant_id, period, high_sequence), outside])
    c2 = (
        _row_columns()
        .select_from(UsageEvent)
        .join(WalletTransaction, WalletTransaction.id == UsageEvent.wallet_transaction_id)
        .where(
            UsageEvent.tenant_id == tenant_id,
            UsageEvent.occurred_at >= period.end,
            WalletTransaction.tenant_id == tenant_id,
            WalletTransaction.transaction_type == TransactionType.AI_USAGE,
            WalletTransaction.created_at < period.end,
            WalletTransaction.wallet_sequence <= high_sequence,
            outside,
        )
    )
    return [c1, c2]


def statement_rows(
    tenant_id: int,
    period: StatementPeriod,
    *,
    high_sequence: int,
    previous_high_sequence: int,
) -> list[Select[tuple[object, ...]]]:
    """`S(M)`: segment one, then segment two (`L(M-1; hw_prev)` rows with `t(r) <= M`).

    段一与段二按 `seq` 不相交，段二的两段按 `created_at` 不相交；
    每段一条查询，不写成一个 `OR`。
    """
    through = attributed_through(period)
    one = _from_ledger(
        [*_segment_one_range(tenant_id, high_sequence, previous_high_sequence), through]
    )
    two = [
        candidate.where(through)
        for candidate in later_rows(
            tenant_id, previous_period(period), high_sequence=previous_high_sequence
        )
    ]
    return [one, *two]


def _ids(session: Session, statements: Iterable[Select[tuple[object, ...]]]) -> set[int]:
    return {int(row.id) for statement in statements for row in session.execute(statement)}


def later_ids(
    session: Session, tenant_id: int, period: StatementPeriod, *, high_sequence: int
) -> set[int]:
    """The ledger row ids `later_rows` selects: the same SELECTs, executed."""
    return _ids(session, later_rows(tenant_id, period, high_sequence=high_sequence))


def statement_ids(
    session: Session,
    tenant_id: int,
    period: StatementPeriod,
    *,
    high_sequence: int,
    previous_high_sequence: int,
) -> set[int]:
    """The ledger row ids `statement_rows` selects: the same SELECTs, executed."""
    return _ids(
        session,
        statement_rows(
            tenant_id,
            period,
            high_sequence=high_sequence,
            previous_high_sequence=previous_high_sequence,
        ),
    )


def _sum(session: Session, statements: Iterable[Select[tuple[object, ...]]]) -> Decimal:
    total = Decimal(0)
    for statement in statements:
        rows = statement.subquery()
        found = session.execute(
            select(func.coalesce(func.sum(rows.c.amount), 0)).select_from(rows)
        ).scalar_one()
        total += Decimal(found)
    return total


def _rows_without_event(session: Session, conditions: Iterable[ColumnElement[bool]]) -> int:
    """`AI_USAGE` rows in the range the LEFT JOIN finds no event for; no attribution test."""
    statement = (
        select(func.count())
        .select_from(WalletTransaction)
        .outerjoin(UsageEvent, UsageEvent.wallet_transaction_id == WalletTransaction.id)
        .where(
            *conditions,
            WalletTransaction.transaction_type == TransactionType.AI_USAGE,
            UsageEvent.id.is_(None),
        )
    )
    return int(session.execute(statement).scalar_one())


# --- 本期账本段 -------------------------------------------------------------------------


@dataclass(frozen=True)
class PriorUsage:
    """Late `AI_USAGE` from one earlier month; `usage_period` is its events' month."""

    usage_period: str
    request_count: int
    amount: Decimal


@dataclass(frozen=True)
class LedgerSection:
    """The ledger half of a statement (design §2「发生额分类」), signed `Decimal` amounts."""

    opening_balance: Decimal
    closing_balance: Decimal
    total_topup: Decimal
    total_ai_usage: Decimal
    total_adjustments: Decimal
    # 按 `usage_period` 升序。
    prior_usage: tuple[PriorUsage, ...]
    prior_rebill: Decimal
    prior_total: Decimal
    # 本期 `AI_USAGE` 行数（`occ ∈ [start(M), end(M))`）。
    current_usage_rows: int


def _integrity_failure(tenant_public_id: str, period: StatementPeriod, check: str) -> NoReturn:
    # 只带租户 public_id、账期与检查编号，不带金额（设计 §2「自检」、§6）。
    logger.error(
        "Statement integrity check failed",
        extra={"customer_id": tenant_public_id, "period": period.label, "check": check},
    )
    raise StatementIntegrityError(check)


def ledger_section(
    session: Session,
    tenant_id: int,
    period: StatementPeriod,
    *,
    tenant_public_id: str,
    high_sequence: int,
    balance: Decimal,
    previous: tuple[int, Decimal] | None,
) -> LedgerSection:
    """`S(M)` by kind, the opening and closing balances, and self-check 1.

    `high_sequence` / `balance` 是同一次读到的 `wallets.version` / `balance`；
    `previous` 是上期对账单的 `(ledger_high_sequence, closing_balance)`，
    上期不合格时为 `None`：此时 `hw_prev = high_sequence`，
    期初 = `balance − Σ L(M-1; high_sequence)`。
    `tenant_public_id` 只用于错误日志。

    期初加各项发生额不等于期末，或任一段账本侧候选里有连不上事件的
    `AI_USAGE` 行：记一条 error 日志，抛 `StatementIntegrityError`。
    只读、不加锁、不提交。
    """
    previous_high = high_sequence if previous is None else previous[0]
    before = previous_period(period)

    # 从账本侧出发的候选范围：段一、期末的 C1、段二的 C1、首期期初的 C1。
    # 连不上事件的 AI_USAGE 行会被归属谓词悄悄滤掉，所以先单独数。
    ranges = [
        _segment_one_range(tenant_id, high_sequence, previous_high),
        _c1_range(tenant_id, period, high_sequence),
        _c1_range(tenant_id, before, previous_high),
    ]
    if previous is None:
        ranges.append(_c1_range(tenant_id, before, high_sequence))
    if any(_rows_without_event(session, conditions) for conditions in ranges):
        _integrity_failure(tenant_public_id, period, CHECK_MISSING_EVENT)

    closing = balance - _sum(session, later_rows(tenant_id, period, high_sequence=high_sequence))
    if previous is None:
        opening = balance - _sum(
            session, later_rows(tenant_id, before, high_sequence=high_sequence)
        )
    else:
        opening = previous[1]

    totals: dict[TransactionType, Decimal] = {}
    current_amount = Decimal(0)
    current_rows = 0
    prior: dict[str, tuple[int, Decimal]] = {}
    first_day = (period.start + KUALA_LUMPUR_OFFSET).date()
    next_first_day = (period.end + KUALA_LUMPUR_OFFSET).date()
    for statement in statement_rows(
        tenant_id, period, high_sequence=high_sequence, previous_high_sequence=previous_high
    ):
        rows = statement.subquery()
        day = MalaysiaDay(rows.c.occurred_at)
        grouped = select(
            rows.c.transaction_type,
            day.label("day"),
            func.count().label("row_count"),
            func.coalesce(func.sum(rows.c.amount), 0).label("amount"),
        ).group_by(rows.c.transaction_type, day)
        for found_type, usage_day, count, amount in session.execute(grouped):
            kind = TransactionType(found_type)
            value = Decimal(amount)
            if kind is not TransactionType.AI_USAGE:
                totals[kind] = totals.get(kind, Decimal(0)) + value
            elif usage_day >= next_first_day:
                # 设计 §2：`S(M)` 里不可能有 `occ >= end(M)` 的用量行。
                raise ValueError(f"usage after {period.label} attributed to it")
            elif usage_day >= first_day:
                current_amount += value
                current_rows += int(count)
            else:
                # 按马来西亚日分组后在这里折成月。
                month = _label(usage_day.year, usage_day.month)
                rows_so_far, amount_so_far = prior.get(month, (0, Decimal(0)))
                prior[month] = (rows_so_far + int(count), amount_so_far + value)

    def total_of(kinds: Iterable[TransactionType]) -> Decimal:
        return sum((totals.get(kind, Decimal(0)) for kind in kinds), Decimal(0))

    prior_usage = tuple(
        PriorUsage(month, count, amount) for month, (count, amount) in sorted(prior.items())
    )
    prior_rebill = total_of(_REBILL_TYPES)
    prior_total = sum((item.amount for item in prior_usage), Decimal(0)) + prior_rebill
    section = LedgerSection(
        opening_balance=opening,
        closing_balance=closing,
        total_topup=total_of([TransactionType.TOPUP]),
        total_ai_usage=current_amount,
        total_adjustments=total_of(_ADJUSTMENT_TYPES),
        prior_usage=prior_usage,
        prior_rebill=prior_rebill,
        prior_total=prior_total,
        current_usage_rows=current_rows,
    )
    movement = section.total_topup + section.total_ai_usage + section.total_adjustments
    if opening + movement + prior_total != closing:
        _integrity_failure(tenant_public_id, period, CHECK_BALANCE)
    return section


# --- 用量统计 ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProviderUsage:
    """One `by_provider` line."""

    provider_code: str
    request_count: int
    input_tokens: int
    output_tokens: int
    charges: Decimal


@dataclass(frozen=True)
class ModelUsage:
    """One `by_model` line."""

    provider_code: str
    model_code: str
    request_count: int
    input_tokens: int
    output_tokens: int
    charges: Decimal


@dataclass(frozen=True)
class ProjectUsage:
    """One `by_project` line; `project_name` is the name at generation."""

    project_public_id: str
    project_name: str
    request_count: int
    input_tokens: int
    output_tokens: int
    charges: Decimal


@dataclass(frozen=True)
class UsageSection:
    """The usage half of a statement (design §2「用量统计」), all from `E(M)`."""

    request_count: int
    conversation_count: int
    input_tokens: int
    output_tokens: int
    # `billable_cost` 之和（≥ 0）。
    total_customer_charges: Decimal
    # `E(M)` 里收费事件（`wallet_transaction_id` 非空）的条数：自检第 3 条。
    charged_event_count: int
    # 按 `provider_code`、`(provider_code, model_code)`、`(project_name, project_public_id)`
    # 码点升序。
    by_provider: tuple[ProviderUsage, ...]
    by_model: tuple[ModelUsage, ...]
    by_project: tuple[ProjectUsage, ...]


# (request_count, input_tokens, output_tokens, charges) of one group.
_Counts = tuple[int, int, int, Decimal]


def _current_events(
    tenant_id: int, period: StatementPeriod, high_sequence: int
) -> list[ColumnElement[bool]]:
    """`E(M)` over `usage_events LEFT JOIN wallet_transactions`, from the event side.

    收费事件跟随账本行：`seq <= hw` 且 `created_at < cutoff(M)`（`occ` 在本月时
    `S(M)` 的条件化简为此）；零收费事件按 `processed_at < cutoff(M)`。
    """
    charged = and_(
        UsageEvent.wallet_transaction_id.is_not(None),
        WalletTransaction.id.is_not(None),
        WalletTransaction.wallet_sequence <= high_sequence,
        WalletTransaction.created_at < period.cutoff,
    )
    free = and_(
        UsageEvent.wallet_transaction_id.is_(None),
        UsageEvent.processed_at < period.cutoff,
    )
    return [
        UsageEvent.tenant_id == tenant_id,
        UsageEvent.status == UsageEventStatus.PROCESSED,
        UsageEvent.billing_mode_snapshot == BillingMode.PREPAID.value,
        UsageEvent.occurred_at >= period.start,
        UsageEvent.occurred_at < period.end,
        or_(charged, free),
    ]


def _join_ledger(statement: Select[Any], tenant_id: int) -> Select[Any]:
    # 连账本时也带 `tenant_id`：别的租户的账本行连上来就是 NULL，事件不进 `E(M)`。
    return statement.outerjoin(
        WalletTransaction,
        and_(
            WalletTransaction.id == UsageEvent.wallet_transaction_id,
            WalletTransaction.tenant_id == tenant_id,
        ),
    )


def usage_section(
    session: Session, tenant_id: int, period: StatementPeriod, *, high_sequence: int
) -> UsageSection:
    """Request, conversation and token counts and charges of `E(M)`, in total and by group.

    `input_tokens` / `output_tokens` 为 NULL 的按 0；会话数是非空
    `conversation_id` 的去重计数。明细在 Python 里按码点排序，不依赖数据库排序规则。
    只读、不加锁。
    """
    conditions = _current_events(tenant_id, period, high_sequence)
    input_tokens = func.coalesce(UsageEvent.input_tokens, 0)
    output_tokens = func.coalesce(UsageEvent.output_tokens, 0)

    totals = _join_ledger(
        select(
            func.count().label("request_count"),
            func.count(UsageEvent.conversation_id.distinct()).label("conversation_count"),
            func.coalesce(func.sum(input_tokens), 0).label("input_tokens"),
            func.coalesce(func.sum(output_tokens), 0).label("output_tokens"),
            func.coalesce(func.sum(UsageEvent.billable_cost), 0).label("charges"),
            func.count(UsageEvent.wallet_transaction_id).label("charged_event_count"),
        ).select_from(UsageEvent),
        tenant_id,
    ).where(*conditions)
    total = session.execute(totals).one()

    grouped = (
        _join_ledger(
            select(
                AiProvider.code,
                AiModel.code,
                Project.public_id,
                Project.name,
                func.count(),
                func.coalesce(func.sum(input_tokens), 0),
                func.coalesce(func.sum(output_tokens), 0),
                func.coalesce(func.sum(UsageEvent.billable_cost), 0),
            ).select_from(UsageEvent),
            tenant_id,
        )
        .join(AiProvider, AiProvider.id == UsageEvent.provider_id)
        .join(AiModel, AiModel.id == UsageEvent.model_id)
        .join(Project, Project.id == UsageEvent.project_id)
        .where(*conditions)
        .group_by(
            AiProvider.id,
            AiProvider.code,
            AiModel.id,
            AiModel.code,
            Project.id,
            Project.public_id,
            Project.name,
        )
    )
    providers: dict[str, _Counts] = {}
    models: dict[tuple[str, str], _Counts] = {}
    projects: dict[tuple[str, str], _Counts] = {}

    def add(groups: dict[Any, _Counts], key: Any, counts: _Counts) -> None:
        found = groups.get(key, (0, 0, 0, Decimal(0)))
        groups[key] = (
            found[0] + counts[0],
            found[1] + counts[1],
            found[2] + counts[2],
            found[3] + counts[3],
        )

    for provider, model, project_id, project_name, count, ins, outs, charges in session.execute(
        grouped
    ).tuples():
        counts: _Counts = (int(count), int(ins), int(outs), Decimal(charges))
        add(providers, str(provider), counts)
        add(models, (str(provider), str(model)), counts)
        add(projects, (str(project_name), str(project_id)), counts)

    return UsageSection(
        request_count=int(total.request_count),
        conversation_count=int(total.conversation_count),
        input_tokens=int(total.input_tokens),
        output_tokens=int(total.output_tokens),
        total_customer_charges=Decimal(total.charges),
        charged_event_count=int(total.charged_event_count),
        by_provider=tuple(
            ProviderUsage(code, *counts) for code, counts in sorted(providers.items())
        ),
        by_model=tuple(
            ModelUsage(provider, model, *counts)
            for (provider, model), counts in sorted(models.items())
        ),
        by_project=tuple(
            ProjectUsage(public_id, name, *counts)
            for (name, public_id), counts in sorted(projects.items())
        ),
    )


def check_usage(
    tenant_public_id: str, period: StatementPeriod, ledger: LedgerSection, usage: UsageSection
) -> None:
    """Self-checks 2 and 3: charges against the ledger's usage amount, charged events against
    its usage rows. Either fails: one error log, `StatementIntegrityError`."""
    if usage.total_customer_charges != -ledger.total_ai_usage:
        _integrity_failure(tenant_public_id, period, CHECK_CHARGES)
    if usage.charged_event_count != ledger.current_usage_rows:
        _integrity_failure(tenant_public_id, period, CHECK_CHARGED_EVENTS)


# --- 快照 -------------------------------------------------------------------------------


def _usage_counts(
    request_count: int, input_tokens: int, output_tokens: int, charges: Decimal
) -> dict[str, Any]:
    return {
        "request_count": request_count,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "charges": money_text(charges),
    }


def build_snapshot(
    period: StatementPeriod,
    *,
    company_name: str,
    tax_notice: DocumentNotice,
    ledger: LedgerSection,
    usage: UsageSection,
) -> dict[str, Any]:
    """`snapshot_json` with exactly the keys of design §2「快照」 (`schema_version = 1`).

    金额经 `money_text` 写成 8 位小数字符串，计数是 `int`。
    `tax_notice` 是税务政策版本 `document_notice` 的原文。
    ⚠️ 不加成本、汇率、加价、毛利、版本、规则或内部 id 的键（INV-7）。
    """
    return {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "period": period.label,
        "currency": STATEMENT_CURRENCY,
        "company_name": company_name,
        "tax_notice": {"zh": tax_notice.zh, "en": tax_notice.en},
        "opening_balance": money_text(ledger.opening_balance),
        "total_topup": money_text(ledger.total_topup),
        "total_ai_usage": money_text(ledger.total_ai_usage),
        "total_adjustments": money_text(ledger.total_adjustments),
        "prior_period": {
            "usage": [
                {
                    "usage_period": item.usage_period,
                    "request_count": item.request_count,
                    "amount": money_text(item.amount),
                }
                for item in ledger.prior_usage
            ],
            "rebill": money_text(ledger.prior_rebill),
            "total": money_text(ledger.prior_total),
        },
        "closing_balance": money_text(ledger.closing_balance),
        "request_count": usage.request_count,
        "conversation_count": usage.conversation_count,
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "total_customer_charges": money_text(usage.total_customer_charges),
        "by_provider": [
            {
                "provider_code": line.provider_code,
                **_usage_counts(
                    line.request_count, line.input_tokens, line.output_tokens, line.charges
                ),
            }
            for line in usage.by_provider
        ],
        "by_model": [
            {
                "provider_code": line.provider_code,
                "model_code": line.model_code,
                **_usage_counts(
                    line.request_count, line.input_tokens, line.output_tokens, line.charges
                ),
            }
            for line in usage.by_model
        ],
        "by_project": [
            {
                "project_public_id": line.project_public_id,
                "project_name": line.project_name,
                **_usage_counts(
                    line.request_count, line.input_tokens, line.output_tokens, line.charges
                ),
            }
            for line in usage.by_project
        ],
    }


# --- 生成 -------------------------------------------------------------------------------


@dataclass(frozen=True)
class GenerateResult:
    """The statement of `(tenant, period)`; `created` is False when it already existed."""

    statement: MonthlyStatement
    created: bool


def is_eligible(tenant: Tenant, wallet: Wallet | None, period: StatementPeriod) -> bool:
    """`PREPAID` now, with a wallet created before the period ended.

    账户状态不看；首个账期之前的账期由调用方另判（设计 §2「合格租户」）。
    """
    return (
        tenant.billing_mode == BillingMode.PREPAID
        and wallet is not None
        and wallet.created_at < period.end
    )


def new_statement_number(period: StatementPeriod) -> str:
    """`ST-<YYYYMM of the period>-` and 8 random Crockford base32 characters (`secrets`)."""
    year, month = _year_month(period.label)
    suffix = "".join(
        secrets.choice(_CROCKFORD_BASE32) for _ in range(_STATEMENT_NUMBER_RANDOM_LENGTH)
    )
    return f"ST-{year:04d}{month:02d}-{suffix}"


def _statement_of(
    session: Session, tenant_id: int, period: StatementPeriod
) -> MonthlyStatement | None:
    statement = select(MonthlyStatement).where(
        MonthlyStatement.tenant_id == tenant_id,
        MonthlyStatement.period_start == period.start,
    )
    return session.execute(statement).scalar_one_or_none()


def _tax_policy(
    session: Session, tenant_public_id: str, period: StatementPeriod
) -> ResolvedTaxPolicy:
    """The tax policy in effect at `cutoff(M)`, not `now`: every run of a period gets the same.

    `None` 或读出校验失败（`ValueError`）都是 `StatementTaxPolicyUnavailable`；
    日志只带租户 public_id 与账期，不带政策内容。
    """
    try:
        policy = resolve_tax_policy(session, TaxJurisdiction.MY, TaxPolicyType.SST, period.cutoff)
    except ValueError as error:
        _tax_policy_unavailable(tenant_public_id, period, error)
    if policy is None:
        _tax_policy_unavailable(tenant_public_id, period, None)
    return policy


def _tax_policy_unavailable(
    tenant_public_id: str, period: StatementPeriod, cause: ValueError | None
) -> NoReturn:
    logger.error(
        "Statement tax policy unavailable",
        extra={"customer_id": tenant_public_id, "period": period.label},
    )
    raise StatementTaxPolicyUnavailable(period.label) from cause


def generate_statement(
    session: Session,
    tenant_id: int,
    period: StatementPeriod,
    *,
    now: dt.datetime,
    generated_by: int | None = None,
) -> GenerateResult:
    """Generate and finalise one statement (design §2「生成函数」1–6); flush, never commit.

    1. 早于首个账期或未到期：`StatementNotDue`；租户不合格：`StatementNotEligible`。
    2. 已有 `(tenant_id, period_start)` 的行：原样返回（`created=False`），不读账本、不写。
    3. 上期合格却没有对账单：`PreviousStatementMissing`。
    4. `cutoff(M)` 时生效的税务政策；取不到或校验失败：`StatementTaxPolicyUnavailable`。
    5. 读钱包（普通读、不加锁）与上期，算账本段与用量，三条自检。
    6. INSERT 一行 `FINALIZED` 并 flush。不写审计、钱包与账本。

    唯一键冲突的 `IntegrityError` 原样抛出：并发与编号碰撞由调用方处理（AIH-TASK-157）。
    """
    if period.label < FIRST_STATEMENT_PERIOD or not is_due(period, now):
        raise StatementNotDue(period.label)
    tenant = session.get(Tenant, tenant_id)
    wallet = session.execute(
        select(Wallet).where(Wallet.tenant_id == tenant_id)
    ).scalar_one_or_none()
    if tenant is None or not is_eligible(tenant, wallet, period):
        raise StatementNotEligible(period.label)

    existing = _statement_of(session, tenant_id, period)
    if existing is not None:
        return GenerateResult(statement=existing, created=False)

    before = previous_period(period)
    previous: tuple[int, Decimal] | None = None
    if before.label >= FIRST_STATEMENT_PERIOD and is_eligible(tenant, wallet, before):
        prior_statement = _statement_of(session, tenant_id, before)
        if prior_statement is None:
            raise PreviousStatementMissing(period.label)
        previous = (
            int(prior_statement.ledger_high_sequence),
            Decimal(prior_statement.snapshot_json["closing_balance"]),
        )

    policy = _tax_policy(session, tenant.public_id, period)

    # 同一行、同一次读：`balance` 就是序号 `version` 那一行的 `balance_after`
    # （设计 §2「高水位」）。
    version, balance = session.execute(
        select(Wallet.version, Wallet.balance).where(Wallet.tenant_id == tenant_id)
    ).one()
    high_sequence = int(version)
    ledger = ledger_section(
        session,
        tenant_id,
        period,
        tenant_public_id=tenant.public_id,
        high_sequence=high_sequence,
        balance=Decimal(balance),
        previous=previous,
    )
    usage = usage_section(session, tenant_id, period, high_sequence=high_sequence)
    check_usage(tenant.public_id, period, ledger, usage)

    statement = MonthlyStatement(
        public_id=str(uuid.uuid4()),
        tenant_id=tenant_id,
        statement_number=new_statement_number(period),
        period_start=period.start,
        period_end=period.end,
        cutoff_at=period.cutoff,
        status=StatementStatus.FINALIZED,
        ledger_high_sequence=high_sequence,
        snapshot_json=build_snapshot(
            period,
            company_name=tenant.company_name,
            tax_notice=policy.treatment.document_notice,
            ledger=ledger,
            usage=usage,
        ),
        generated_by=generated_by,
        tax_policy_version_id=policy.version_id,
        finalized_at=now,
        created_at=now,
    )
    session.add(statement)
    session.flush()
    return GenerateResult(statement=statement, created=True)


# --- catch-up ---------------------------------------------------------------------------

# 提交前钩子：管理端补生成在对账单的同一事务里写审计（设计 §2「管理端接口」）。
BeforeCommit = Callable[[Session, MonthlyStatement], None]


def catch_up_tenant(
    factory: sessionmaker[Session],
    tenant_id: int,
    now: dt.datetime,
    *,
    generated_by: int | None = None,
    before_commit: BeforeCommit | None = None,
    raise_errors: bool = False,
) -> list[MonthlyStatement]:
    """Generate every due statement of one tenant the database lacks (design §2「Celery 任务」).

    按 `due_periods(now)` 升序，每期一个事务：
    - 已有行、未到期、不合格 → 下一期；
    - `generate_statement` 本身抛 `IntegrityError` → 回滚，新事务里查
      `(tenant_id, period_start)`：已存在当作成功（不进返回值）、继续下一期；
      不存在是编号碰撞：warning，停止这个租户的后续账期，不算错误；
    - 新生成的一份：`before_commit(session, statement)`、`session.flush()`、提交；
      这三步抛的任何异常（包括 `IntegrityError`）都是失败；
    - 失败（上一条，或 `generate_statement` 抛的其他任何异常）：回滚，一条 error 日志，
      停止这个租户的后续账期；`raise_errors` 时原样抛出，否则返回已生成的部分。

    租户 public_id 在开头的独立只读事务里读一次（租户不存在返回空列表），之后的日志都用它，
    出错后不再查库。日志只带 public_id、账期与异常类型名，不带金额与公司名（设计 §6）。
    返回本次新生成的对账单，按账期升序。
    """
    with factory() as session:
        tenant_public_id = session.execute(
            select(Tenant.public_id).where(Tenant.id == tenant_id)
        ).scalar_one_or_none()
        session.rollback()
    if tenant_public_id is None:
        return []

    generated: list[MonthlyStatement] = []
    for period in due_periods(now):
        with factory() as session:
            try:
                result = generate_statement(
                    session, tenant_id, period, now=now, generated_by=generated_by
                )
            except (StatementNotDue, StatementNotEligible):
                session.rollback()
                continue
            except IntegrityError:
                session.rollback()
                # 新事务：回滚之后的第一条读开启它，看得到并发生成者已提交的行。
                found = _statement_of(session, tenant_id, period)
                session.rollback()
                if found is not None:
                    continue
                logger.warning(
                    "Statement number collided; the next run retries",
                    extra={"customer_id": tenant_public_id, "period": period.label},
                )
                return generated
            except Exception as error:
                session.rollback()
                _catch_up_failure(tenant_public_id, period, error)
                if raise_errors:
                    raise
                return generated
            if not result.created:
                session.rollback()
                continue
            try:
                if before_commit is not None:
                    before_commit(session, result.statement)
                session.flush()
                session.commit()
            except Exception as error:
                session.rollback()
                _catch_up_failure(tenant_public_id, period, error)
                if raise_errors:
                    raise
                return generated
            generated.append(result.statement)
    return generated


def _catch_up_failure(tenant_public_id: str, period: StatementPeriod, error: Exception) -> None:
    # 固定文本；只带租户 public_id、账期与异常类型名（设计 §2「Celery 任务」、§6）。
    logger.error(
        "Statement generation failed",
        extra={
            "customer_id": tenant_public_id,
            "period": period.label,
            "error_type": type(error).__name__,
        },
    )


def generate_due(factory: sessionmaker[Session], now: dt.datetime) -> int:
    """`catch_up_tenant` for every tenant `PREPAID` now, by id; returns how many were generated.

    一个租户失败不影响其他租户：`catch_up_tenant` 每期之外的步骤抛错也在这里接住，
    记一条 error 日志（租户 public_id 与异常类型名；public_id 在列租户时一并取出）后
    继续下一个租户。这时该租户本次已提交的份数拿不到，不计入返回值：只少计，
    已提交的对账单不受影响。
    """
    with factory() as session:
        tenants = session.execute(
            select(Tenant.id, Tenant.public_id)
            .where(Tenant.billing_mode == BillingMode.PREPAID)
            .order_by(Tenant.id)
        ).all()
        session.rollback()

    total = 0
    for tenant_id, tenant_public_id in tenants:
        try:
            total += len(catch_up_tenant(factory, int(tenant_id), now))
        except Exception as error:
            logger.error(
                "Statement catch-up failed for a tenant",
                extra={"customer_id": tenant_public_id, "error_type": type(error).__name__},
            )
    return total


__all__ = [
    "CHECK_BALANCE",
    "CHECK_CHARGED_EVENTS",
    "CHECK_CHARGES",
    "CHECK_MISSING_EVENT",
    "FIRST_STATEMENT_PERIOD",
    "GENERATION_DELAY",
    "SNAPSHOT_SCHEMA_VERSION",
    "STATEMENT_CURRENCY",
    "BeforeCommit",
    "GenerateResult",
    "LedgerSection",
    "ModelUsage",
    "PreviousStatementMissing",
    "PriorUsage",
    "ProjectUsage",
    "ProviderUsage",
    "StatementIntegrityError",
    "StatementNotDue",
    "StatementNotEligible",
    "StatementPeriod",
    "StatementTaxPolicyUnavailable",
    "UsageSection",
    "attributed_month",
    "attributed_through",
    "build_snapshot",
    "catch_up_tenant",
    "check_usage",
    "due_periods",
    "generate_due",
    "generate_statement",
    "is_due",
    "is_eligible",
    "later_ids",
    "later_rows",
    "ledger_section",
    "new_statement_number",
    "period_containing",
    "previous_period",
    "statement_ids",
    "statement_period",
    "usage_section",
]

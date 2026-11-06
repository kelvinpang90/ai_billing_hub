"""Monthly statements: periods, ledger attribution and the ledger section (gate #565 v6 §2).

AIH-TASK-155 这一层：账期边界与到期判定、账本行的归属月 `t(r)`
（Python 参考函数与只含常量的 SQL 谓词）、给定高水位下本期账本行 `S(M)`
的分类汇总与期初期末，以及自检第 1 条。用量统计、快照与生成属于 AIH-TASK-156。

⚠️ **只读**：不加锁、不写、不 flush、不提交（INV-4、INV-5）。
每条查询都带 `tenant_id`（INV-8）。金额在 SQL 里用 `SUM` 在 `DECIMAL` 上求和，
Python 侧只有 `Decimal` 加减，不经 float（INV-10）。

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
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal
from typing import Final, NoReturn

from sqlalchemy import ColumnElement, Select, and_, func, not_, or_, select
from sqlalchemy.orm import Session

from app.core.fx_source import KUALA_LUMPUR_OFFSET
from app.models.usage import UsageEvent
from app.models.wallet import TransactionType, WalletTransaction
from app.repositories.usage_events import MalaysiaDay
from app.schemas.customer_usage import malaysia_day_start_utc

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
CHECK_MISSING_EVENT: Final = "missing_event"


class StatementIntegrityError(Exception):
    """A self-check failed; `check` names it (`"1"` or `"missing_event"`). Carries no amount."""

    def __init__(self, check: str) -> None:
        super().__init__(f"statement integrity check failed: {check}")
        self.check = check


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


__all__ = [
    "CHECK_BALANCE",
    "CHECK_MISSING_EVENT",
    "FIRST_STATEMENT_PERIOD",
    "GENERATION_DELAY",
    "LedgerSection",
    "PriorUsage",
    "StatementIntegrityError",
    "StatementPeriod",
    "attributed_month",
    "attributed_through",
    "due_periods",
    "is_due",
    "later_ids",
    "later_rows",
    "ledger_section",
    "period_containing",
    "previous_period",
    "statement_ids",
    "statement_period",
]

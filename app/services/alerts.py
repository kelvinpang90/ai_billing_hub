"""Alert dimensions behind `GET /internal/alerts` (design gate #183 v3 §2, AIH-TASK-042).

设计 §2「内部告警接口」「两个告警维度」。每个维度一行：
`<维度> <OK|P1|P2> <一句摘要>`。`deploy/monitor.sh` 每 5 分钟读一次，
按维度各推一个 Healthchecks 检查。

**维度注册表**是下面的 `DIMENSIONS`：一个有序列表，每个维度一个只读查询函数。
T-E（幂等冲突）、T-H（定价错误、汇率错误、未知模型、失败、处理积压、负余额、
outbox 积压，设计闸门 #181 v2 §2「告警维度」，AIH-TASK-032）往里加，不改接口形状。

⚠️ **维度查询全是普通读，不拿 `fx_rate_locks`**（设计 v3）：告警只是提示，
不参与计费，不应让发布等它。整次请求一个只读事务，从不提交。

⚠️ **摘要只有计数、日期与币种**（设计 §2、§6、INV-7）：
不含租户、金额（汇率本身也不写）或任何个人数据。

多币种汇总进一行（设计 v3）：级别取各配置币种里最严重的；
有不 OK 的币种时只列它们各一小段，都 OK 时列各币种最近的日期；
币种按 `BILLING_FX_CURRENCIES` 的顺序。
"""

from __future__ import annotations

import datetime as dt
import enum
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Final

from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.orm import Session, sessionmaker

from app.core.fx_source import kuala_lumpur_date
from app.models.auth import DomainOutbox, OutboxStatus
from app.models.fx_rates import (
    PERIOD_STATUSES,
    QUOTE_CURRENCY,
    FxFetchAttempt,
    FxFetchOutcome,
    FxRateVersion,
)
from app.models.usage import UsageEvent, UsageEventStatus
from app.models.wallet import Wallet
from app.repositories.usage_events import count_conflicts_since
from app.services.auth import utc_now
from app.services.usage_billing import AMOUNT_OUT_OF_RANGE, LEDGER_CONFLICT
from app.tasks import outbox as outbox_tasks

type Clock = Callable[[], dt.datetime]

# `fx_fetch`：72 小时内没有任何成功、且至少一条 FAILED = 连续 3 天拉取失败。
FX_FETCH_WINDOW: Final = dt.timedelta(hours=72)
# `fx_stale`：报价日距今（吉隆坡日期相减）**超过** 5 个日历日。
# Kelvin 2026-09-29 选定（设计 §10 第 2 条）。
FX_STALE_DAYS: Final = 5
# `usage_event_conflicts`：最近 24 小时里有任何一条幂等冲突即 P2
# （设计闸门 #176 v8 §2「告警」；spec §23 要求的数据完整性告警）。
CONFLICT_WINDOW: Final = dt.timedelta(hours=24)

# 用量计费的维度（设计闸门 #181 v2 §2「告警维度」）。
# `usage_failed`：FAILED_RETRYABLE 已重试这么多次即告警。
RETRYING_ALERT_ATTEMPTS: Final = 3
# 这两个错误码的 FAILED_FINAL 是缺陷或数据异常，P1；其余 P2。
FAILED_P1_CODES: Final = (LEDGER_CONFLICT, AMOUNT_OUT_OF_RANGE)
# `usage_processing_backlog`：最早的可处理事件等待超过 5 分钟（§119 p99）P2，超过 30 分钟 P1；
# 租约已过期仍未回收的 PROCESSING（回收任务停了）P2，过期超过 10 分钟 P1。
BACKLOG_P2_WAIT: Final = dt.timedelta(minutes=5)
BACKLOG_P1_WAIT: Final = dt.timedelta(minutes=30)
STALE_LEASE_P1_AGE: Final = dt.timedelta(minutes=10)
# `outbox_backlog`：有处理器的事件类型超过 10 分钟仍 PENDING（§120）。
OUTBOX_BACKLOG_AGE: Final = dt.timedelta(minutes=10)
# 每行摘要最多列几个细分码（设计 §2）。
MAX_CODES: Final = 3

# 三种都是成功：BNM 答「无记录」不是失败（设计 §2 `fx_fetch_attempts`）。
_SUCCESSFUL_OUTCOMES: Final = (
    FxFetchOutcome.NEW_DRAFT,
    FxFetchOutcome.NO_NEW_QUOTE,
    FxFetchOutcome.NO_QUOTE_FOR_DATE,
)


class AlertLevel(enum.StrEnum):
    """Severity of one dimension. Members are in increasing severity."""

    OK = "OK"
    P1 = "P1"
    P2 = "P2"


_SEVERITY: Final = {level: rank for rank, level in enumerate(AlertLevel)}


@dataclass(frozen=True)
class AlertContext:
    """What a dimension reads besides the database: the clock and the configured currencies."""

    # 不带时区的 UTC。
    now: dt.datetime
    currencies: tuple[str, ...]


@dataclass(frozen=True)
class AlertResult:
    level: AlertLevel
    summary: str


@dataclass(frozen=True)
class Dimension:
    """One line of the alerts endpoint: a name and a read-only query."""

    name: str
    check: Callable[[Session, AlertContext], AlertResult]


@dataclass(frozen=True)
class _CurrencyResult:
    level: AlertLevel
    text: str


def _combine(results: Sequence[_CurrencyResult]) -> AlertResult:
    """The worst level; the pieces that are not OK if any, otherwise every piece."""
    if not results:
        return AlertResult(AlertLevel.OK, "no currencies configured")
    level = max((result.level for result in results), key=_SEVERITY.__getitem__)
    shown = results
    if level is not AlertLevel.OK:
        shown = [result for result in results if result.level is not AlertLevel.OK]
    return AlertResult(level, "; ".join(result.text for result in shown))


def _days(count: int) -> str:
    return f"{count} calendar day" if count == 1 else f"{count} calendar days"


# --- fx_fetch ---------------------------------------------------------------------


def _fx_fetch_currency(session: Session, currency: str, since: dt.datetime) -> _CurrencyResult:
    recent = (FxFetchAttempt.base_currency == currency, FxFetchAttempt.attempted_at >= since)
    successes = (
        select(func.max(FxFetchAttempt.attempted_at))
        .select_from(FxFetchAttempt)
        .where(*recent, FxFetchAttempt.outcome.in_(_SUCCESSFUL_OUTCOMES))
    )
    last_success = session.execute(successes).scalar_one()
    if last_success is not None:
        day = kuala_lumpur_date(last_success).isoformat()
        return _CurrencyResult(AlertLevel.OK, f"{currency} last success {day}")
    failed = (
        select(func.count())
        .select_from(FxFetchAttempt)
        .where(*recent, FxFetchAttempt.outcome == FxFetchOutcome.FAILED)
    )
    failures = session.execute(failed).scalar_one()
    if failures:
        text = f"{currency} no successful fetch in 72 hours ({failures} failed)"
        return _CurrencyResult(AlertLevel.P2, text)
    # 72 小时内一条记录都没有：从未运行或 Beat 停了，由 `fx_stale` 负责（设计 §2）。
    return _CurrencyResult(AlertLevel.OK, f"{currency} no fetch in 72 hours")


def fx_fetch(session: Session, context: AlertContext) -> AlertResult:
    """P2 when a configured currency has failed fetches and no success in the last 72 hours."""
    since = context.now - FX_FETCH_WINDOW
    results = [_fx_fetch_currency(session, code, since) for code in context.currencies]
    return _combine(results)


# --- fx_stale ---------------------------------------------------------------------


def _in_effect(session: Session, currency: str, moment: dt.datetime) -> FxRateVersion | None:
    """The version covering `moment`: the condition of `resolve_fx_rate`, without a lock."""
    statement = select(FxRateVersion).where(
        FxRateVersion.base_currency == currency,
        FxRateVersion.quote_currency == QUOTE_CURRENCY,
        FxRateVersion.status.in_(PERIOD_STATUSES),
        or_(FxRateVersion.effective_from.is_(None), FxRateVersion.effective_from <= moment),
        or_(FxRateVersion.effective_to.is_(None), FxRateVersion.effective_to > moment),
    )
    return session.execute(statement).scalar_one_or_none()


def quote_date_of(version: FxRateVersion) -> dt.date:
    """BNM: the quote date; manual entry: the Kuala Lumpur date of `observed_at`."""
    if version.source_quote_date is not None:
        return version.source_quote_date
    return kuala_lumpur_date(version.observed_at)


def _fx_stale_currency(session: Session, currency: str, now: dt.datetime) -> _CurrencyResult:
    version = _in_effect(session, currency, now)
    if version is None:
        # 包括「只有未来的预约」：此刻取不到汇率。
        return _CurrencyResult(AlertLevel.P2, f"{currency} no rate in effect")
    quoted = quote_date_of(version)
    age = (kuala_lumpur_date(now) - quoted).days
    level = AlertLevel.P2 if age > FX_STALE_DAYS else AlertLevel.OK
    return _CurrencyResult(level, f"{currency} quoted {quoted.isoformat()} ({_days(age)})")


def fx_stale(session: Session, context: AlertContext) -> AlertResult:
    """P2 when the rate in effect was quoted over 5 calendar days ago, or there is none."""
    results = [_fx_stale_currency(session, code, context.now) for code in context.currencies]
    return _combine(results)


# --- usage_event_conflicts ----------------------------------------------------------


def usage_event_conflicts(session: Session, context: AlertContext) -> AlertResult:
    """P2 when an idempotency conflict was recorded in the last 24 hours (the count and the
    earliest one). 冲突表只增，同一冲突请求重发不加行，所以计数就是不同冲突的条数。"""
    count, earliest = count_conflicts_since(session, context.now - CONFLICT_WINDOW)
    if count == 0 or earliest is None:
        return AlertResult(AlertLevel.OK, "no conflicts in 24 hours")
    noun = "conflict" if count == 1 else "conflicts"
    since = earliest.strftime("%Y-%m-%dT%H:%M:%SZ")
    return AlertResult(AlertLevel.P2, f"{count} {noun} in 24 hours, earliest {since}")


# --- 用量计费（AIH-TASK-032）：摘要只有计数、最早时刻与至多 3 个细分码 --------------------


def _stamp(moment: dt.datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _events(count: int) -> str:
    return "1 event" if count == 1 else f"{count} events"


def _codes(session: Session, *conditions: object) -> list[str]:
    """At most `MAX_CODES` distinct error codes of the matching events, in alphabetical order."""
    statement = (
        select(UsageEvent.error_code)
        .where(*conditions, UsageEvent.error_code.is_not(None))
        .distinct()
        .order_by(UsageEvent.error_code)
        .limit(MAX_CODES)
    )
    return [str(code) for code in session.execute(statement).scalars().all()]


def _with_codes(summary: str, codes: Sequence[str]) -> str:
    return f"{summary}, codes {', '.join(codes)}" if codes else summary


def _error_state(session: Session, status: UsageEventStatus) -> AlertResult:
    """P2 when any event is in `status`: the count, the earliest `received_at`, the codes."""
    in_state = UsageEvent.status == status
    statement = select(func.count(), func.min(UsageEvent.received_at)).where(in_state)
    count, earliest = session.execute(statement).one()
    if not count or earliest is None:
        return AlertResult(AlertLevel.OK, f"no {status.value} events")
    summary = f"{_events(int(count))} {status.value}, earliest received {_stamp(earliest)}"
    return AlertResult(AlertLevel.P2, _with_codes(summary, _codes(session, in_state)))


def usage_pricing_error(session: Session, context: AlertContext) -> AlertResult:
    """P2 when any event is in PRICING_ERROR (no price, no rule, or one lacks a component)."""
    return _error_state(session, UsageEventStatus.PRICING_ERROR)


def usage_fx_rate_error(session: Session, context: AlertContext) -> AlertResult:
    """P2 when any event is in FX_RATE_ERROR (a non-MYR price without a rate in effect)."""
    return _error_state(session, UsageEventStatus.FX_RATE_ERROR)


def usage_model_unknown(session: Session, context: AlertContext) -> AlertResult:
    """P2 when any event is in MODEL_UNKNOWN (§84: the reported model resolves to nothing)."""
    return _error_state(session, UsageEventStatus.MODEL_UNKNOWN)


def usage_failed(session: Session, context: AlertContext) -> AlertResult:
    """FAILED_FINAL events, or FAILED_RETRYABLE ones retried 3 times or more.

    P1 when a FAILED_FINAL carries `LEDGER_CONFLICT` or `AMOUNT_OUT_OF_RANGE`, otherwise P2.
    """
    final = UsageEvent.status == UsageEventStatus.FAILED_FINAL
    retrying = and_(
        UsageEvent.status == UsageEventStatus.FAILED_RETRYABLE,
        UsageEvent.attempt_count >= RETRYING_ALERT_ATTEMPTS,
    )
    counted = select(func.count(), func.min(UsageEvent.received_at))
    finals, final_since = session.execute(counted.where(final)).one()
    retries, retry_since = session.execute(counted.where(retrying)).one()
    if not finals and not retries:
        return AlertResult(AlertLevel.OK, "no failed events")
    severe = select(func.count()).where(final, UsageEvent.error_code.in_(FAILED_P1_CODES))
    level = AlertLevel.P1 if session.execute(severe).scalar_one() else AlertLevel.P2
    earliest = min(moment for moment in (final_since, retry_since) if moment is not None)
    summary = (
        f"{int(finals)} FAILED_FINAL, {int(retries)} FAILED_RETRYABLE with "
        f"{RETRYING_ALERT_ATTEMPTS}+ attempts, earliest received {_stamp(earliest)}"
    )
    return AlertResult(level, _with_codes(summary, _codes(session, or_(final, retrying))))


def usage_processing_backlog(session: Session, context: AlertContext) -> AlertResult:
    """The oldest claimable event waiting, and PROCESSING whose lease expired unrecovered.

    可处理：`RECEIVED`，或到期的 `FAILED_RETRYABLE`，且 `occurred_at ≤ now`。等待的起点是它
    变得可处理的时刻：`RECEIVED` 取接收与发生时刻中较晚的那个（未来偏差的事件到点才可认领），
    `FAILED_RETRYABLE` 取 `next_attempt_at`。等待超过 5 分钟 P2、超过 30 分钟 P1；存在租约已
    过期的 `PROCESSING`（回收任务本身停了）P2，过期超过 10 分钟 P1。
    """
    now = context.now
    happened = UsageEvent.occurred_at <= now
    received_since = case(
        (UsageEvent.occurred_at > UsageEvent.received_at, UsageEvent.occurred_at),
        else_=UsageEvent.received_at,
    )
    received = select(func.count(), func.min(received_since)).where(
        UsageEvent.status == UsageEventStatus.RECEIVED, happened
    )
    retry_since = func.coalesce(UsageEvent.next_attempt_at, UsageEvent.received_at)
    retry_due = or_(UsageEvent.next_attempt_at.is_(None), UsageEvent.next_attempt_at <= now)
    retrying = select(func.count(), func.min(retry_since)).where(
        UsageEvent.status == UsageEventStatus.FAILED_RETRYABLE, retry_due, happened
    )
    processing = UsageEvent.status == UsageEventStatus.PROCESSING
    claimed = select(func.count(), func.min(UsageEvent.claimed_at)).where(processing)
    expired = select(func.count(), func.min(UsageEvent.lease_expires_at)).where(
        processing, UsageEvent.lease_expires_at < now
    )

    due_received, received_oldest = session.execute(received).one()
    due_retrying, retrying_oldest = session.execute(retrying).one()
    in_progress, earliest_claim = session.execute(claimed).one()
    stale, earliest_expiry = session.execute(expired).one()
    due = int(due_received) + int(due_retrying)
    waiting = [moment for moment in (received_oldest, retrying_oldest) if moment is not None]
    oldest = min(waiting) if waiting else None

    waited = now - oldest if oldest is not None else dt.timedelta(0)
    expired_for = now - earliest_expiry if earliest_expiry is not None else dt.timedelta(0)
    level = AlertLevel.OK
    if waited > BACKLOG_P2_WAIT or stale:
        level = AlertLevel.P2
    if waited > BACKLOG_P1_WAIT or expired_for > STALE_LEASE_P1_AGE:
        level = AlertLevel.P1

    since = f", oldest since {_stamp(oldest)}" if oldest is not None else ""
    first = f", earliest claimed {_stamp(earliest_claim)}" if earliest_claim is not None else ""
    summary = f"{due} due{since}; {int(in_progress)} PROCESSING{first}; {int(stale)} lease expired"
    return AlertResult(level, summary)


def wallet_negative_balance(session: Session, context: AlertContext) -> AlertResult:
    """P2 when any wallet is below zero (§120). The count only: no tenant, no amount."""
    statement = select(func.count()).select_from(Wallet).where(Wallet.balance < 0)
    count = int(session.execute(statement).scalar_one())
    if not count:
        return AlertResult(AlertLevel.OK, "no wallet below zero")
    noun = "1 wallet" if count == 1 else f"{count} wallets"
    return AlertResult(AlertLevel.P2, f"{noun} below zero")


def outbox_backlog(session: Session, context: AlertContext) -> AlertResult:
    """P2 when an outbox row of a type with a handler is still PENDING after 10 minutes (§120).

    没有处理器的类型（`tenant.billing_status_changed`、`tenant.low_balance`，等以后的投递任务）
    按设计持久等待，不计入（app/tasks/outbox.py 的 `_RENDERERS`，与 `recover` 同一份名单）。
    """
    handled = list(outbox_tasks._RENDERERS)
    statement = select(func.count(), func.min(DomainOutbox.created_at)).where(
        DomainOutbox.status == OutboxStatus.PENDING,
        DomainOutbox.event_type.in_(handled),
        DomainOutbox.created_at < context.now - OUTBOX_BACKLOG_AGE,
    )
    count, oldest = session.execute(statement).one()
    if not count or oldest is None:
        return AlertResult(AlertLevel.OK, "no handled outbox row pending over 10 minutes")
    noun = "1 row" if count == 1 else f"{int(count)} rows"
    return AlertResult(
        AlertLevel.P2, f"{noun} pending over 10 minutes, oldest created {_stamp(oldest)}"
    )


# --- 注册表 -------------------------------------------------------------------------

# ⚠️ 有序：接口按这个顺序逐行输出。新维度追加在末尾，名字用小写与下划线
# （monitor.sh 按它拼 `BILLING_HEALTHCHECK_ALERT_<维度大写>_URL`）。
DIMENSIONS: Final[list[Dimension]] = [
    Dimension("fx_fetch", fx_fetch),
    Dimension("fx_stale", fx_stale),
    Dimension("usage_event_conflicts", usage_event_conflicts),
    # AIH-TASK-032（设计闸门 #181 v2 §2「告警维度」），按设计表的顺序。
    Dimension("usage_pricing_error", usage_pricing_error),
    Dimension("usage_fx_rate_error", usage_fx_rate_error),
    Dimension("usage_model_unknown", usage_model_unknown),
    Dimension("usage_failed", usage_failed),
    Dimension("usage_processing_backlog", usage_processing_backlog),
    Dimension("wallet_negative_balance", wallet_negative_balance),
    Dimension("outbox_backlog", outbox_backlog),
]


def evaluate(session: Session, context: AlertContext) -> list[tuple[str, AlertResult]]:
    """Every registered dimension, in order."""
    return [(dimension.name, dimension.check(session, context)) for dimension in DIMENSIONS]


def render(results: Sequence[tuple[str, AlertResult]]) -> str:
    """`<dimension> <OK|P1|P2> <summary>`, one line each, newline-terminated."""
    return "".join(f"{name} {result.level.value} {result.summary}\n" for name, result in results)


def alert_lines(
    factory: sessionmaker[Session], currencies: tuple[str, ...], *, clock: Clock = utc_now
) -> str:
    """The body of `GET /internal/alerts`: one read-only transaction, never committed."""
    context = AlertContext(now=clock(), currencies=currencies)
    with factory() as session:
        try:
            return render(evaluate(session, context))
        finally:
            session.rollback()


__all__ = [
    "BACKLOG_P1_WAIT",
    "BACKLOG_P2_WAIT",
    "CONFLICT_WINDOW",
    "DIMENSIONS",
    "FAILED_P1_CODES",
    "FX_FETCH_WINDOW",
    "FX_STALE_DAYS",
    "MAX_CODES",
    "OUTBOX_BACKLOG_AGE",
    "RETRYING_ALERT_ATTEMPTS",
    "STALE_LEASE_P1_AGE",
    "AlertContext",
    "AlertLevel",
    "AlertResult",
    "Dimension",
    "alert_lines",
    "evaluate",
    "fx_fetch",
    "fx_stale",
    "outbox_backlog",
    "quote_date_of",
    "render",
    "usage_event_conflicts",
    "usage_failed",
    "usage_fx_rate_error",
    "usage_model_unknown",
    "usage_pricing_error",
    "usage_processing_backlog",
    "wallet_negative_balance",
]

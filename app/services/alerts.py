"""Alert dimensions behind `GET /internal/alerts` (design gate #183 v3 §2, AIH-TASK-042).

设计 §2「内部告警接口」「两个告警维度」。每个维度一行：
`<维度> <OK|P1|P2> <一句摘要>`。`deploy/monitor.sh` 每 5 分钟读一次，
按维度各推一个 Healthchecks 检查。

**维度注册表**是下面的 `DIMENSIONS`：一个有序列表，每个维度一个只读查询函数。
T-E（幂等冲突）、T-H（定价错误、汇率错误、未知模型、负余额、outbox 积压）
往里加，不改接口形状。

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

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, sessionmaker

from app.core.fx_source import kuala_lumpur_date
from app.models.fx_rates import (
    PERIOD_STATUSES,
    QUOTE_CURRENCY,
    FxFetchAttempt,
    FxFetchOutcome,
    FxRateVersion,
)
from app.services.auth import utc_now

type Clock = Callable[[], dt.datetime]

# `fx_fetch`：72 小时内没有任何成功、且至少一条 FAILED = 连续 3 天拉取失败。
FX_FETCH_WINDOW: Final = dt.timedelta(hours=72)
# `fx_stale`：报价日距今（吉隆坡日期相减）**超过** 5 个日历日。
# Kelvin 2026-09-29 选定（设计 §10 第 2 条）。
FX_STALE_DAYS: Final = 5

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


# --- 注册表 -------------------------------------------------------------------------

# ⚠️ 有序：接口按这个顺序逐行输出。新维度追加在末尾，名字用小写与下划线
# （monitor.sh 按它拼 `BILLING_HEALTHCHECK_ALERT_<维度大写>_URL`）。
DIMENSIONS: Final[list[Dimension]] = [
    Dimension("fx_fetch", fx_fetch),
    Dimension("fx_stale", fx_stale),
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
    "DIMENSIONS",
    "FX_FETCH_WINDOW",
    "FX_STALE_DAYS",
    "AlertContext",
    "AlertLevel",
    "AlertResult",
    "Dimension",
    "alert_lines",
    "evaluate",
    "fx_fetch",
    "fx_stale",
    "quote_date_of",
    "render",
]

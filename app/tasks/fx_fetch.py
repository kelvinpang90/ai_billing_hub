"""Daily BNM fetch: quotes become drafts, every fetch leaves an attempt (AIH-TASK-040).

设计闸门 #183 v3 §2「BNM 适配器」、`fx_fetch_attempts`、§4；spec §17.1；ADR-0005。
Beat 每天 UTC 04:30 / 06:30 / 09:30（= 吉隆坡 12:30 / 14:30 / 17:30）跑一次 `fetch`，
也可以手工再触发（参数相同，任务幂等）：

    D = 吉隆坡今天的日期（UTC + 8 小时后取日期）
    对每个配置币种：source.fetch(currency, D)        ← 事务外
      → 有报价：一个事务里 INSERT 草稿 + 拉取记录 NEW_DRAFT；
                该报价日已有 BNM 版本 → 只写拉取记录 NO_NEW_QUOTE
                （报价变了：error_code = QUOTE_CHANGED）
      → BNM 答「无记录」：拉取记录 NO_QUOTE_FOR_DATE，不写草稿
      → 失败：拉取记录 FAILED + 错误码，不写草稿

⚠️ **自动拉取绝不发布**（§17.1、ADR-0005 §3）：这里只写 `DRAFT`，发布只有管理员能做
（app/services/fx_rates.py）。BNM 草稿**不写审计**：拉取记录就是它的来源证明，
操作者是系统。

⚠️ **出站调用在事务外**：BNM 的往返（最长一个超时）不占数据库连接与锁。
草稿与拉取记录同一事务（INV-13）：要么都在、要么都不在。

⚠️ **每个币种各自一次拉取、各自一个事务**：一个币种写库失败只记日志、继续其余币种；
全部处理完后任务以失败结束。写库失败的那个币种这一次什么都没留下，下一次调度重新拉取，
报价日唯一约束保证不重复。失败不在任务内重试（设计 §4「重试层」）。

**并发拉取**（手工重触发与定时同时跑）：报价日唯一约束 `(base_currency, quote_currency,
source, source_quote_date)` 决出一条草稿。输家的 INSERT 在 MySQL 上等赢家提交后撞唯一
约束，整个事务回滚；随后在一个**新事务**里读到赢家的版本，记 `NO_NEW_QUOTE`。
新事务是必要的：REPEATABLE READ 下，旧事务的普通读看的是它自己的快照，
看不到赢家刚提交的那一行。
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from functools import lru_cache
from typing import Final

from celery import shared_task
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings
from app.core.database import create_database_engine, create_session_factory, session_scope
from app.core.fx_source import (
    FxQuote,
    FxRateSource,
    FxSourceError,
    build_fx_source,
    kuala_lumpur_date,
    noon_session_observed_at,
)
from app.models.fx_rates import (
    QUOTE_CURRENCY,
    FxFetchAttempt,
    FxFetchOutcome,
    FxRateSourceType,
    FxRateStatus,
    FxRateVersion,
)
from app.services.auth import utc_now
from app.services.fx_rates import Clock

logger = logging.getLogger(__name__)

# 同一报价日 BNM 返回了不同的价（BNM 更正）：已有版本不变，
# 拉取记录带上这个错误码（设计 §4「相同 ID、不同载荷」）。
QUOTE_CHANGED: Final = "QUOTE_CHANGED"


class FxFetchIncomplete(RuntimeError):
    """At least one currency could not be written; the others were. The task ends as failed."""


@lru_cache(maxsize=1)
def _session_factory() -> sessionmaker[Session]:
    """One engine per worker process (同 app/tasks/outbox.py)."""
    return create_session_factory(create_database_engine(get_settings()))


def _seconds(moment: dt.datetime) -> dt.datetime:
    # MySQL 的 DATETIME 不存小数秒：`created_at`、`attempted_at` 截到整秒（设计 §2）。
    return moment.replace(microsecond=0)


# --- 写库 -------------------------------------------------------------------------


def existing_bnm_version(
    session: Session, currency: str, quote_date: dt.date
) -> FxRateVersion | None:
    """The BNM version of this quote date, whatever its status (唯一约束保证至多一条)."""
    statement = select(FxRateVersion).where(
        FxRateVersion.base_currency == currency,
        FxRateVersion.quote_currency == QUOTE_CURRENCY,
        FxRateVersion.source == FxRateSourceType.BNM,
        FxRateVersion.source_quote_date == quote_date,
    )
    return session.execute(statement).scalar_one_or_none()


def insert_bnm_draft(session: Session, currency: str, quote: FxQuote, now: dt.datetime) -> int:
    """Insert and flush a BNM draft; return its internal id. No creator, no period, no audit."""
    row = FxRateVersion(
        public_id=str(uuid.uuid4()),
        base_currency=currency,
        quote_currency=QUOTE_CURRENCY,
        rate=quote.rate,
        source=FxRateSourceType.BNM,
        source_reference=quote.reference,
        source_quote_date=quote.quote_date,
        # 报价日 12:00 吉隆坡 = 当日 04:00 UTC（固定 +8）。
        observed_at=noon_session_observed_at(quote.quote_date),
        status=FxRateStatus.DRAFT,
        effective_from=None,
        effective_to=None,
        created_by=None,
        approved_by=None,
        approved_at=None,
        created_at=now,
        updated_at=now,
    )
    session.add(row)
    session.flush()
    return row.id


def add_attempt(
    session: Session,
    *,
    currency: str,
    requested_date: dt.date,
    outcome: FxFetchOutcome,
    now: dt.datetime,
    quote_date: dt.date | None = None,
    error_code: str | None = None,
    fx_rate_version_id: int | None = None,
) -> None:
    """Insert and flush one fetch attempt (append-only)."""
    session.add(
        FxFetchAttempt(
            base_currency=currency,
            source=FxRateSourceType.BNM,
            requested_date=requested_date,
            outcome=outcome,
            quote_date=quote_date,
            error_code=error_code,
            fx_rate_version_id=fx_rate_version_id,
            attempted_at=now,
        )
    )
    session.flush()


def _no_new_quote(
    session: Session,
    existing: FxRateVersion,
    *,
    currency: str,
    requested_date: dt.date,
    quote: FxQuote,
    now: dt.datetime,
) -> FxFetchOutcome:
    # 比较的是还原后的 `rate`，按数值（`Decimal` 相等）：字面量不同、还原后相同不算更正。
    changed = existing.rate != quote.rate
    add_attempt(
        session,
        currency=currency,
        requested_date=requested_date,
        outcome=FxFetchOutcome.NO_NEW_QUOTE,
        now=now,
        quote_date=quote.quote_date,
        error_code=QUOTE_CHANGED if changed else None,
    )
    if changed:
        logger.warning(
            "BNM returned a different rate for a quote date that already has a version",
            extra={"base_currency": currency, "quote_date": quote.quote_date.isoformat()},
        )
    return FxFetchOutcome.NO_NEW_QUOTE


def _record_quote(
    factory: sessionmaker[Session],
    *,
    currency: str,
    requested_date: dt.date,
    quote: FxQuote,
    now: dt.datetime,
) -> FxFetchOutcome:
    """NEW_DRAFT, or NO_NEW_QUOTE when the quote date already has a BNM version."""
    try:
        with session_scope(factory) as session:
            existing = existing_bnm_version(session, currency, quote.quote_date)
            if existing is not None:
                return _no_new_quote(
                    session,
                    existing,
                    currency=currency,
                    requested_date=requested_date,
                    quote=quote,
                    now=now,
                )
            version_id = insert_bnm_draft(session, currency, quote, now)
            add_attempt(
                session,
                currency=currency,
                requested_date=requested_date,
                outcome=FxFetchOutcome.NEW_DRAFT,
                now=now,
                quote_date=quote.quote_date,
                fx_rate_version_id=version_id,
            )
            return FxFetchOutcome.NEW_DRAFT
    except IntegrityError as error:
        # 并发拉取的输家：整个事务已回滚，在新事务里读赢家的版本。
        # 读不到就不是这个原因，原样抛出。
        with session_scope(factory) as session:
            existing = existing_bnm_version(session, currency, quote.quote_date)
            if existing is None:
                raise error
            return _no_new_quote(
                session,
                existing,
                currency=currency,
                requested_date=requested_date,
                quote=quote,
                now=now,
            )


def _record_without_quote(
    factory: sessionmaker[Session],
    *,
    currency: str,
    requested_date: dt.date,
    outcome: FxFetchOutcome,
    now: dt.datetime,
    error_code: str | None = None,
) -> FxFetchOutcome:
    with session_scope(factory) as session:
        add_attempt(
            session,
            currency=currency,
            requested_date=requested_date,
            outcome=outcome,
            now=now,
            error_code=error_code,
        )
    return outcome


# --- 拉取 -------------------------------------------------------------------------


def fetch_currency(
    factory: sessionmaker[Session],
    source: FxRateSource,
    currency: str,
    requested_date: dt.date,
    *,
    clock: Clock = utc_now,
) -> FxFetchOutcome:
    """Fetch one currency for the requested date and record the outcome.

    返回结果；写库失败时抛出（这一次什么都没留下）。
    """
    # ⚠️ 出站调用在任何事务之外。
    try:
        quote = source.fetch(currency, requested_date)
    except FxSourceError as error:
        logger.warning(
            "Fetching an FX rate failed",
            extra={"base_currency": currency, "error_code": error.code},
        )
        return _record_without_quote(
            factory,
            currency=currency,
            requested_date=requested_date,
            outcome=FxFetchOutcome.FAILED,
            now=_seconds(clock()),
            error_code=error.code,
        )
    if quote is None:
        return _record_without_quote(
            factory,
            currency=currency,
            requested_date=requested_date,
            outcome=FxFetchOutcome.NO_QUOTE_FOR_DATE,
            now=_seconds(clock()),
        )
    return _record_quote(
        factory,
        currency=currency,
        requested_date=requested_date,
        quote=quote,
        now=_seconds(clock()),
    )


def fetch_all(
    factory: sessionmaker[Session],
    source: FxRateSource,
    currencies: tuple[str, ...],
    *,
    clock: Clock = utc_now,
) -> dict[str, str]:
    """Fetch every currency for Kuala Lumpur's today. Returns currency → outcome (for the logs).

    任何一个币种写库失败：记日志、继续其余币种，全部处理完后抛 `FxFetchIncomplete`。
    """
    requested_date = kuala_lumpur_date(clock())
    outcomes: dict[str, str] = {}
    unwritten: list[str] = []
    for currency in currencies:
        try:
            outcome = fetch_currency(factory, source, currency, requested_date, clock=clock)
        except Exception:
            # ⚠️ 只记日志、继续其余币种：一个币种的写库失败不该让别的币种也拿不到草稿。
            logger.exception(
                "Recording an FX fetch failed; the next run fetches again",
                extra={"base_currency": currency},
            )
            unwritten.append(currency)
            continue
        outcomes[currency] = outcome.value
    logger.info(
        "FX fetch finished",
        extra={
            "requested_date": requested_date.isoformat(),
            "outcomes": ",".join(f"{code}={outcome}" for code, outcome in outcomes.items()),
        },
    )
    if unwritten:
        raise FxFetchIncomplete(f"FX fetch could not be recorded for: {', '.join(unwritten)}")
    return outcomes


@shared_task(name="app.tasks.fx_fetch.fetch")
def fetch() -> dict[str, str]:
    """Beat entry point (three times a day). Safe to run again by hand: it is idempotent."""
    settings = get_settings()
    return fetch_all(_session_factory(), build_fx_source(settings), settings.fx_currency_codes)


__all__ = [
    "QUOTE_CHANGED",
    "FxFetchIncomplete",
    "add_attempt",
    "existing_bnm_version",
    "fetch",
    "fetch_all",
    "fetch_currency",
    "insert_bnm_draft",
]

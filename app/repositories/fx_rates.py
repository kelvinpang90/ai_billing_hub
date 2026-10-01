"""FX rate version data access and rate resolution (design gate #183 v3, AIH-TASK-039).

⚠️ **只 flush，不 commit。**版本、被截断或恢复的前一个版本与审计同一事务，事务边界归调用方的
`session_scope()`（app/services/fx_rates.py）。`lock_fx_rates_shared` 与 `resolve_fx_rate`
更是如此：它们必须在调用方（T-H）写事件快照、扣费的那个事务里调用，锁持有到那个事务提交
（设计 §2「对下游任务的契约」T-H）。

⚠️ **发布与退役里的读、以及取汇率都是加锁读**（`FOR UPDATE` / `FOR SHARE`）：MySQL 默认的
REPEATABLE READ 下，普通 SELECT 读的是本事务第一次一致性读时的快照 —— 锁拿到了、数据却
可能是旧的。加锁读总是读最新提交的版本。`populate_existing`：会话里已有的对象按锁内读到的
值刷新。SQLite 忽略这两种锁。

⚠️ **写入顺序有讲究**（设计 §2，数据库的区间触发器与 `open_slot` 唯一索引都按行检查）：
每个改区间的函数都立刻 flush，调用方按「先截断前一个、再发布本行」「先把被撤销的一行改成
空区间、再恢复前一个」的顺序调用。版本行没有删除。
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import Select, func, or_, select
from sqlalchemy.orm import Session

from app.models.auth import User
from app.models.fx_rates import (
    FX_RATE_LOCK_ID,
    PERIOD_STATUSES,
    QUOTE_CURRENCY,
    FxFetchAttempt,
    FxFetchOutcome,
    FxRateLock,
    FxRateSourceType,
    FxRateStatus,
    FxRateVersion,
)


@dataclass(frozen=True)
class ResolvedFxRate:
    """The FX rate version in effect at `occurred_at`.

    `version_id` 是内部 id：T-H 把它写进事件快照（`fx_rate_version_id`），不对外；`rate` 是
    1 单位 `base_currency` 等于多少 MYR，T-G 原值存进 `fx_rate_applied`（同精度）。
    """

    version_id: int
    public_id: str
    base_currency: str
    quote_currency: str
    rate: Decimal
    effective_from: dt.datetime | None
    effective_to: dt.datetime | None


def _new_public_id() -> str:
    return str(uuid.uuid4())


def _shared[T](statement: Select[T]) -> Select[T]:
    """Locking read in share mode (`FOR SHARE`); refresh objects already in the session."""
    return statement.with_for_update(read=True).execution_options(populate_existing=True)


def _locked[T](statement: Select[T], lock: bool) -> Select[T]:
    """`FOR SHARE` when `lock`: reads made under the FX lock in a publish or retire."""
    return _shared(statement) if lock else statement


# --- 锁 ---------------------------------------------------------------------------


def lock_fx_rates(session: Session) -> FxRateLock:
    """`SELECT … FROM fx_rate_locks WHERE id = 1 FOR UPDATE` (MySQL; SQLite ignores it).

    发布与退役的第一条语句：两者彼此串行，也与计费的共享锁互斥（设计 §2「与计费不会交错」）。
    边界时刻 `t` 必须在它返回**之后**才取。
    """
    statement = (
        select(FxRateLock)
        .where(FxRateLock.id == FX_RATE_LOCK_ID)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return session.execute(statement).scalar_one()


def lock_fx_rates_shared(session: Session) -> FxRateLock:
    """`SELECT … FROM fx_rate_locks WHERE id = 1 FOR SHARE`, for billing (T-H).

    ⚠️ 调用方契约（设计 §2 T-H）：在计费事务里 `resolve_model` 之后调用、持有到计费事务提交；
    锁顺序是 **供应商（S）→ FX（S）→ 定价规则（S）→ 钱包 → 租户**。共享锁之间不冲突，计费事务
    彼此不阻塞；只有发布 / 退役（排他锁）会让计费等一下。本任务不接到任何计费路径。
    """
    statement = select(FxRateLock).where(FxRateLock.id == FX_RATE_LOCK_ID)
    return session.execute(_shared(statement)).scalar_one()


# --- 读 ---------------------------------------------------------------------------


def get_version(
    session: Session, public_id: str, *, for_update: bool = False
) -> FxRateVersion | None:
    statement = select(FxRateVersion).where(FxRateVersion.public_id == public_id)
    if for_update:
        statement = statement.with_for_update().execution_options(populate_existing=True)
    return session.execute(statement).scalar_one_or_none()


def user_emails(
    session: Session, user_ids: Iterable[int | None], *, lock: bool = False
) -> dict[int, str]:
    """Internal user id → login email. Admin views name people by email, never by id."""
    ids = sorted({user_id for user_id in user_ids if user_id is not None})
    if not ids:
        return {}
    statement = select(User.id, User.email).where(User.id.in_(ids))
    return {row.id: row.email for row in session.execute(_locked(statement, lock))}


# --- 管理端只读查询（AIH-TASK-041） -------------------------------------------------
# 普通读，不拿 FX 锁：只展示，不参与区间计算（设计 §2「接口」）。


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


def list_versions(
    session: Session,
    *,
    base_currency: str | None,
    status: FxRateStatus | None,
    source: FxRateSourceType | None,
    offset: int,
    limit: int,
) -> tuple[list[FxRateVersion], int]:
    """One page, newest first (`id` descending; 设计 v3); the total."""
    statement = select(FxRateVersion)
    if base_currency is not None:
        statement = statement.where(FxRateVersion.base_currency == base_currency)
    if status is not None:
        statement = statement.where(FxRateVersion.status == status)
    if source is not None:
        statement = statement.where(FxRateVersion.source == source)
    statement = statement.order_by(FxRateVersion.id.desc())
    return _page(session, statement, offset=offset, limit=limit)


@dataclass(frozen=True)
class FetchAttemptRow:
    """A fetch attempt with the `public_id` of the draft it wrote (`NEW_DRAFT` only)."""

    attempt: FxFetchAttempt
    fx_rate_public_id: str | None


def list_fetch_attempts(
    session: Session,
    *,
    base_currency: str | None,
    outcome: FxFetchOutcome | None,
    offset: int,
    limit: int,
) -> tuple[list[FetchAttemptRow], int]:
    """One page by `attempted_at`, then `id`, both descending (设计 v3); the total."""
    statement = select(FxFetchAttempt)
    if base_currency is not None:
        statement = statement.where(FxFetchAttempt.base_currency == base_currency)
    if outcome is not None:
        statement = statement.where(FxFetchAttempt.outcome == outcome)
    statement = statement.order_by(FxFetchAttempt.attempted_at.desc(), FxFetchAttempt.id.desc())
    attempts, total = _page(session, statement, offset=offset, limit=limit)
    version_ids = sorted(
        {row.fx_rate_version_id for row in attempts if row.fx_rate_version_id is not None}
    )
    public_ids: dict[int, str] = {}
    if version_ids:
        found = select(FxRateVersion.id, FxRateVersion.public_id).where(
            FxRateVersion.id.in_(version_ids)
        )
        public_ids = {row.id: row.public_id for row in session.execute(found)}
    # 只有 `NEW_DRAFT` 指向草稿；其余的 `fx_rate_version_id` 为空，取到 None。
    rows = [
        FetchAttemptRow(attempt=row, fx_rate_public_id=public_ids.get(row.fx_rate_version_id))
        for row in attempts
    ]
    return rows, total


# --- 区间 -------------------------------------------------------------------------


def version_order(row: FxRateVersion) -> tuple[bool, dt.datetime, int]:
    """Time order: the one without a start first (排序在 Python 里做，不依赖方言的 NULL 约定)."""
    return (row.effective_from is not None, row.effective_from or dt.datetime.min, row.id)


def period_versions(session: Session, base_currency: str) -> list[FxRateVersion]:
    """Every PUBLISHED / RETIRED version of `base_currency` → MYR, in time order. `FOR UPDATE`."""
    statement = (
        select(FxRateVersion)
        .where(
            FxRateVersion.base_currency == base_currency,
            FxRateVersion.quote_currency == QUOTE_CURRENCY,
            FxRateVersion.status.in_(PERIOD_STATUSES),
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return sorted(session.execute(statement).scalars().all(), key=version_order)


# --- 写 ---------------------------------------------------------------------------


def insert_manual_draft(
    session: Session,
    *,
    base_currency: str,
    rate: Decimal,
    observed_at: dt.datetime,
    source_reference: str,
    created_by: int,
    now: dt.datetime,
) -> FxRateVersion:
    """Insert and flush a `MANUAL` draft: no quote date, no period, no approver (设计 §2)."""
    row = FxRateVersion(
        public_id=_new_public_id(),
        base_currency=base_currency,
        quote_currency=QUOTE_CURRENCY,
        rate=rate,
        source=FxRateSourceType.MANUAL,
        source_reference=source_reference,
        source_quote_date=None,
        observed_at=observed_at,
        status=FxRateStatus.DRAFT,
        effective_from=None,
        effective_to=None,
        created_by=created_by,
        approved_by=None,
        approved_at=None,
        created_at=now,
        updated_at=now,
    )
    session.add(row)
    session.flush()
    return row


def save_draft(
    session: Session,
    row: FxRateVersion,
    *,
    rate: Decimal,
    observed_at: dt.datetime,
    source_reference: str,
    now: dt.datetime,
) -> None:
    if row.status is not FxRateStatus.DRAFT or row.source is not FxRateSourceType.MANUAL:
        raise ValueError("only a manual draft is edited")
    row.rate = rate
    row.observed_at = observed_at
    row.source_reference = source_reference
    row.updated_at = now
    session.flush()


def mark_discarded(session: Session, row: FxRateVersion, *, now: dt.datetime) -> None:
    if row.status is not FxRateStatus.DRAFT:
        raise ValueError("only a draft is discarded")
    row.status = FxRateStatus.DISCARDED
    row.updated_at = now
    session.flush()


def truncate(session: Session, row: FxRateVersion, *, at: dt.datetime, now: dt.datetime) -> None:
    """`effective_to`: NULL → `at` on the open PUBLISHED version; a successor takes over."""
    if row.status is not FxRateStatus.PUBLISHED or row.effective_to is not None:
        raise ValueError("only the untruncated published version is truncated")
    row.effective_to = at
    row.updated_at = now
    session.flush()


def restore_open(session: Session, row: FxRateVersion, *, now: dt.datetime) -> None:
    """`effective_to` → NULL again: the reservation that truncated it was withdrawn."""
    if row.status is not FxRateStatus.PUBLISHED or row.effective_to is None:
        raise ValueError("only a truncated published version is restored")
    row.effective_to = None
    row.updated_at = now
    session.flush()


def mark_published(
    session: Session,
    row: FxRateVersion,
    *,
    effective_from: dt.datetime | None,
    approved_by: int,
    now: dt.datetime,
) -> None:
    """DRAFT → PUBLISHED with `[effective_from, NULL)`; the approver is the publisher."""
    if row.status is not FxRateStatus.DRAFT:
        raise ValueError("only a draft is published")
    row.status = FxRateStatus.PUBLISHED
    row.effective_from = effective_from
    row.effective_to = None
    row.approved_by = approved_by
    row.approved_at = now
    row.updated_at = now
    session.flush()


def mark_retired(
    session: Session, row: FxRateVersion, *, effective_to: dt.datetime, now: dt.datetime
) -> None:
    """PUBLISHED → RETIRED, ending at `effective_to` (= its start for a withdrawn reservation)."""
    if row.status is not FxRateStatus.PUBLISHED or row.effective_to is not None:
        raise ValueError("only the untruncated published version is retired")
    row.status = FxRateStatus.RETIRED
    row.effective_to = effective_to
    row.updated_at = now
    session.flush()


# --- 取汇率（T-G / T-H 调用；本任务不接到任何计费路径） -----------------------------------


def resolve_fx_rate(
    session: Session, base_currency: str, occurred_at: dt.datetime
) -> ResolvedFxRate | None:
    """The `base_currency` → MYR rate in effect at `occurred_at`, or `None`.

    取 `PUBLISHED` / `RETIRED` 且 `effective_from ≤ occurred_at < effective_to`（NULL 端不比）
    的版本；没有就是 `None`（T-G 判 `FX_RATE_ERROR`，不扣费）。空区间（撤销的预约、同一秒里
    发布又退役）永不匹配。`occurred_at` 是不带时区的 UTC。只读库，绝不调用 BNM（ADR-0005 §4）。
    `base_currency = MYR` 不查汇率，是调用方（T-G）的事。

    ⚠️ 调用方契约（设计 §2 T-H）：在计费事务里**先** `lock_fx_rates_shared`（与发布、退役的
    排他锁互斥），**再**调用这里；这里是加锁读（`FOR SHARE`），锁持有到计费事务提交。于是一个
    （币种, 时刻）一旦取到某个版本，以后永远取到同一个版本。

    同一币种对的非空区间不重叠（服务层在锁内保证并复查，数据库触发器兜底），所以至多一个
    命中；多于一个是数据损坏，`scalar_one_or_none` 抛错，不静默挑一个。
    """
    started = or_(
        FxRateVersion.effective_from.is_(None),
        FxRateVersion.effective_from <= occurred_at,
    )
    not_ended = or_(
        FxRateVersion.effective_to.is_(None),
        FxRateVersion.effective_to > occurred_at,
    )
    in_effect = select(FxRateVersion).where(
        FxRateVersion.base_currency == base_currency,
        FxRateVersion.quote_currency == QUOTE_CURRENCY,
        FxRateVersion.status.in_(PERIOD_STATUSES),
        started,
        not_ended,
    )
    version = session.execute(_shared(in_effect)).scalar_one_or_none()
    if version is None:
        return None
    return ResolvedFxRate(
        version_id=version.id,
        public_id=version.public_id,
        base_currency=version.base_currency,
        quote_currency=version.quote_currency,
        rate=version.rate,
        effective_from=version.effective_from,
        effective_to=version.effective_to,
    )


__all__ = [
    "FetchAttemptRow",
    "ResolvedFxRate",
    "get_version",
    "insert_manual_draft",
    "list_fetch_attempts",
    "list_versions",
    "lock_fx_rates",
    "lock_fx_rates_shared",
    "mark_discarded",
    "mark_published",
    "mark_retired",
    "period_versions",
    "resolve_fx_rate",
    "restore_open",
    "save_draft",
    "truncate",
    "user_emails",
    "version_order",
]

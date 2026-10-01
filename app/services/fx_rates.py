"""FX rate versions: manual drafts, publishing and retiring (design gate #183 v3, AIH-TASK-039).

spec §17.1、§74.2；ADR-0005。`rate` 是 1 单位 `base_currency` 等于多少 MYR。本任务只有规则层：
管理端接口（HTTP 状态码的映射、请求格式）归 AIH-TASK-041，BNM 拉取归 AIH-TASK-040；本任务
不接到任何计费路径。

⚠️ **每个写操作一个 `session_scope()`**（INV-13）：版本、被截断或恢复的前一个版本、复查与审计
同一事务，要么都在、要么都不在。repository 只 flush。

⚠️ **锁在前**（设计 §2「锁在前」）：发布与退役的第一条语句是 `SELECT … FROM fx_rate_locks
WHERE id = 1 FOR UPDATE`，之后的读全是加锁读（版本行 `FOR UPDATE`、同一币种对的已发布 / 已退役
行 `FOR UPDATE`、响应用到的用户 `FOR SHARE`）—— 不读 REPEATABLE READ 在锁前建立的快照。边界
时刻 `t` 在**拿到锁之后**才取当前时间，截到整秒再加 1 秒（`boundary_after`，整秒也加）。计费侧
在同一行上持共享锁、只处理 `occurred_at ≤` 持锁后当前时间的事件，所以一个（币种, 时刻）一旦
取到某个版本，以后永远取到同一个版本（设计 §2「与计费不会交错」）。建草稿、改草稿、丢弃不拿
这把锁（草稿不参与计费），只锁版本行。

⚠️ **只在末尾追加，从发布时刻起生效**（不从报价日起算）：新版本的 `effective_from` 永远 ≥ `t`，
例外只有两处，覆盖的时刻此前一律取不到汇率 —— P1（时间线为空、未给时刻：一直以来）与 P4
（末尾版本有尽头 E、未给时刻：从 E 起补上退役留下的空档）。空区间 `[F, F)` 只在 `RETIRED` 行上、
不在时间线上。每次发布、退役写完后，在同一事务里加锁重读该币种对的全部已发布 / 已退役版本
复查区间，不满足就抛错、整体回滚。

⚠️ 审计的前后状态里，版本用 `public_id`，人用登录邮箱，时刻是 ISO 8601 UTC，`rate` 是字符串；
不用内部 id（设计 §2「审计内容」）。
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from itertools import pairwise
from typing import Final

from sqlalchemy.orm import Session, sessionmaker

from app.core.database import session_scope
from app.core.errors import AppError
from app.models.auth import AuditAction, User
from app.models.fx_rates import (
    QUOTE_CURRENCY,
    RATE_PRECISION,
    RATE_SCALE,
    SOURCE_REFERENCE_LENGTH,
    FxFetchOutcome,
    FxRateSourceType,
    FxRateStatus,
    FxRateVersion,
)
from app.repositories import fx_rates as rates
from app.services.ai_catalog import boundary_after
from app.services.auth import RequestContext, record_audit, utc_now

# 审计的 entity_type；entity_id 是版本的 public_id。
ENTITY_FX_RATE_VERSION: Final = "fx_rate_version"

# 取当前时间（不带时区的 UTC）。可注入，测试据此冻结时钟。
type Clock = Callable[[], dt.datetime]

_FINAL_STATUSES: Final = (FxRateStatus.RETIRED, FxRateStatus.DISCARDED)

# 币种代码的格式只在应用层校验（设计 v3：数据库不做正则 CHECK）。
_CURRENCY_PATTERN: Final = re.compile(r"[A-Z]{3}")
# DECIMAL(24,10)：最多 10 位小数，整数部分最多 14 位。超出就拒绝，不舍入。
_RATE_QUANTUM: Final = Decimal(1).scaleb(-RATE_SCALE)
_RATE_LIMIT: Final = Decimal(10) ** (RATE_PRECISION - RATE_SCALE)


class FxRateNotFound(AppError):
    def __init__(self) -> None:
        super().__init__("FX rate version not found.", code="FX_RATE_NOT_FOUND", http_status=404)


class FxRateInvalid(AppError):
    """A manual entry or a publish time that breaks the design's format rules (422)."""

    def __init__(self, message: str) -> None:
        super().__init__(message, code="VALIDATION_ERROR", http_status=422)


class FxRateNotDraft(AppError):
    """Editing or discarding a published version."""

    def __init__(self) -> None:
        super().__init__(
            "Only a draft FX rate version can be changed or discarded.",
            code="FX_RATE_NOT_DRAFT",
            http_status=409,
        )


class FxRateNotEditable(AppError):
    """Editing a BNM draft: discard it and enter a manual one instead (the source differs)."""

    def __init__(self) -> None:
        super().__init__(
            "A BNM draft cannot be edited; discard it and enter a manual draft.",
            code="FX_RATE_NOT_EDITABLE",
            http_status=409,
        )


class FxRateNotRetirable(AppError):
    """Retiring a draft, or a published version whose period has already ended (R3, R4)."""

    def __init__(self) -> None:
        super().__init__(
            "Only the current, untruncated published FX rate version can be retired.",
            code="FX_RATE_NOT_RETIRABLE",
            http_status=409,
        )


class FxRateFinal(AppError):
    """Anything on a retired or discarded version (R5)."""

    def __init__(self) -> None:
        super().__init__(
            "The FX rate version is retired or discarded.",
            code="FX_RATE_FINAL",
            http_status=409,
        )


class EffectiveFromConflict(AppError):
    """P3: the last version has not started yet and the start is not after its start."""

    def __init__(self) -> None:
        super().__init__(
            "The effective time is not after the start of the latest FX rate version.",
            code="EFFECTIVE_FROM_CONFLICT",
            http_status=409,
        )


class EffectiveFromInPast(AppError):
    """A requested start before `t`: checked before any rule of the publish table."""

    def __init__(self) -> None:
        super().__init__(
            "The effective time is in the past.",
            code="EFFECTIVE_FROM_IN_PAST",
            http_status=422,
        )


class FxPeriodsBroken(RuntimeError):
    """The in-transaction recheck failed: a bug, never a user error. 500, rolled back."""


@dataclass(frozen=True)
class FxRateVersionView:
    """One version as the admin side sees it: `public_id` as `id`, people by email."""

    id: str
    base_currency: str
    quote_currency: str
    rate: Decimal
    source: FxRateSourceType
    source_reference: str
    source_quote_date: dt.date | None
    observed_at: dt.datetime
    status: FxRateStatus
    effective_from: dt.datetime | None
    effective_to: dt.datetime | None
    created_by_email: str | None
    approved_by_email: str | None
    approved_at: dt.datetime | None
    created_at: dt.datetime
    updated_at: dt.datetime


def _seconds(moment: dt.datetime) -> dt.datetime:
    # MySQL 的 DATETIME 不存小数秒：与 app/services/ai_catalog.py 同一理由。
    return moment.replace(microsecond=0)


def _text(moment: dt.datetime | None) -> str | None:
    return moment.isoformat() if moment is not None else None


def rate_text(value: Decimal) -> str:
    """The rate as an exact decimal string without trailing zeros (`4.0830000000` → `4.083`)."""
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def _status(row: FxRateVersion) -> FxRateStatus:
    return FxRateStatus(row.status)


def _source(row: FxRateVersion) -> FxRateSourceType:
    return FxRateSourceType(row.source)


# --- 校验（设计 §2「金额精度」「时间语义」手工录入部分） ------------------------------------


def _require_base_currency(base_currency: str) -> str:
    if not _CURRENCY_PATTERN.fullmatch(base_currency) or base_currency == QUOTE_CURRENCY:
        raise FxRateInvalid("base_currency must be three upper-case letters other than MYR.")
    return base_currency


def _require_rate(rate: Decimal) -> Decimal:
    """`> 0`, at most 10 decimal places, fits DECIMAL(24,10). Never rounded (INV-10)."""
    if not isinstance(rate, Decimal) or not rate.is_finite():
        raise FxRateInvalid("rate must be an exact decimal.")
    if rate <= 0:
        raise FxRateInvalid("rate must be greater than zero.")
    if rate >= _RATE_LIMIT or rate.quantize(_RATE_QUANTUM) != rate:
        raise FxRateInvalid("rate must have at most 10 decimal places and 14 integer digits.")
    return rate


def _require_moment(moment: dt.datetime, name: str) -> dt.datetime:
    """Naive UTC on a whole second (the API layer converts RFC 3339 with an offset to this)."""
    if moment.tzinfo is not None or moment.microsecond != 0:
        raise FxRateInvalid(f"{name} must be a whole second in UTC.")
    return moment


def _require_reference(source_reference: str) -> str:
    if not source_reference.strip() or len(source_reference) > SOURCE_REFERENCE_LENGTH:
        raise FxRateInvalid("source_reference is required and at most 255 characters.")
    return source_reference


# --- 视图 -------------------------------------------------------------------------


def _view(session: Session, row: FxRateVersion, *, lock: bool = False) -> FxRateVersionView:
    # `lock`：发布、退役在 FX 锁之后的读一律加锁读，不读事务快照（设计 §2「锁在前」第 2 条）。
    emails = rates.user_emails(session, [row.created_by, row.approved_by], lock=lock)
    return FxRateVersionView(
        id=row.public_id,
        base_currency=row.base_currency,
        quote_currency=row.quote_currency,
        rate=row.rate,
        source=_source(row),
        source_reference=row.source_reference,
        source_quote_date=row.source_quote_date,
        observed_at=row.observed_at,
        status=_status(row),
        effective_from=row.effective_from,
        effective_to=row.effective_to,
        created_by_email=emails.get(row.created_by) if row.created_by is not None else None,
        approved_by_email=emails.get(row.approved_by) if row.approved_by is not None else None,
        approved_at=row.approved_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


# --- 区间 -------------------------------------------------------------------------


def is_empty_period(row: FxRateVersion) -> bool:
    """`[F, F)`: never matches any moment and is not on the timeline (设计 §2「术语」)."""
    return row.effective_from is not None and row.effective_from == row.effective_to


def _timeline(rows: Sequence[FxRateVersion]) -> list[FxRateVersion]:
    """The versions with a non-empty period, earliest start first (a `NULL` start is earliest)."""
    return sorted((row for row in rows if not is_empty_period(row)), key=rates.version_order)


def verify_periods(rows: Sequence[FxRateVersion]) -> None:
    """Raise `FxPeriodsBroken` unless the PUBLISHED / RETIRED versions of one pair chain in time.

    设计 §2「事务内复查」：① 空区间的行都是 `RETIRED`；② 其余（时间线上的）按 `effective_from`
    排序（为空的最早），只有第一个的 `effective_from` 可为空；相邻两个，前一个的 `effective_to`
    为空时它必须是最后一个，非空时 ≤ 后一个的 `effective_from`（等号 = 首尾相接，小于 = 退役
    留下的空档）；③ 至多一个 `effective_to` 为空。区间自身的合法性归数据库的 CHECK。
    """
    for row in rows:
        if is_empty_period(row) and _status(row) is not FxRateStatus.RETIRED:
            raise FxPeriodsBroken("only a retired version may have an empty period")
    for earlier, later in pairwise(_timeline(rows)):
        if later.effective_from is None:
            raise FxPeriodsBroken("only the first version may start at the beginning")
        if earlier.effective_to is None:
            raise FxPeriodsBroken("only the last version may be open")
        if earlier.effective_to > later.effective_from:
            raise FxPeriodsBroken("periods overlap")
    if sum(1 for row in rows if row.effective_to is None) > 1:
        raise FxPeriodsBroken("more than one version is open")


def _recheck(session: Session, base_currency: str) -> None:
    # 重新加锁读一遍库里的版本（刚 flush 的也在），不信内存里的推算。
    verify_periods(rates.period_versions(session, base_currency))


def _placement(
    tail: FxRateVersion | None, *, requested: dt.datetime | None, t: dt.datetime
) -> tuple[dt.datetime | None, bool]:
    """Where a new version starts, and whether the tail is truncated there (设计 §2 P1–P4).

    `tail` 是末尾版本 L：时间线（区间非空的 `PUBLISHED` / `RETIRED`）里 `effective_from` 最晚的
    一个。调用方已保证 `requested` 为空或 ≥ `t`。
    """
    if tail is None:
        # P1：时间线为空，此前的时刻一律取不到汇率。未给 F = 一直以来，只把无汇率变有汇率。
        return requested, False
    if tail.effective_to is None:
        # L 未截断，必然是 PUBLISHED（CHECK 4：已退役必有尽头）。
        if tail.effective_from is None or tail.effective_from < t:
            # P2：L 已开始。F ≥ `t` > L 的起点，截断恒合法。
            return (t if requested is None else requested), True
        # P3：L 尚未开始（预约的，或同一秒里刚发布的）。未给 F 不自动推后；F 必须晚于 L 的起点。
        if requested is None or requested <= tail.effective_from:
            raise EffectiveFromConflict
        return requested, True
    # P4：L 有尽头 E（E ≤ `t` 恒成立）。未给 F：从 E 起补上空档；给了 F：`[E, F)` 保持无汇率。
    return (tail.effective_to if requested is None else requested), False


# --- 草稿 -------------------------------------------------------------------------


def _require_version(
    session: Session, fx_rate_id: str, *, for_update: bool = False
) -> FxRateVersion:
    row = rates.get_version(session, fx_rate_id, for_update=for_update)
    if row is None:
        raise FxRateNotFound
    return row


def _require_draft(row: FxRateVersion) -> None:
    status = _status(row)
    if status in _FINAL_STATUSES:
        raise FxRateFinal
    if status is not FxRateStatus.DRAFT:
        raise FxRateNotDraft


def create_draft(
    session_factory: sessionmaker[Session],
    *,
    actor: User,
    base_currency: str,
    rate: Decimal,
    observed_at: dt.datetime,
    source_reference: str,
    context: RequestContext,
    clock: Clock = utc_now,
) -> FxRateVersionView:
    """A `MANUAL` draft and `FX_RATE_CREATE` in one commit. No FX lock (drafts are not billed).

    `base_currency` 是任何 `^[A-Z]{3}$` 且 ≠ MYR 的代码，不限于 BNM 拉取的配置币种；`rate`
    > 0、最多 10 位小数；`observed_at` 是不带时区的 UTC 整秒；`source_reference` 必填。任何一项
    不合格：422 `VALIDATION_ERROR`，不写。
    """
    _require_base_currency(base_currency)
    _require_rate(rate)
    _require_moment(observed_at, "observed_at")
    _require_reference(source_reference)
    with session_scope(session_factory) as session:
        now = _seconds(clock())
        row = rates.insert_manual_draft(
            session,
            base_currency=base_currency,
            rate=rate,
            observed_at=observed_at,
            source_reference=source_reference,
            created_by=actor.id,
            now=now,
        )
        record_audit(
            session,
            action=AuditAction.FX_RATE_CREATE,
            context=context,
            now=now,
            actor=actor,
            entity_type=ENTITY_FX_RATE_VERSION,
            entity_id=row.public_id,
            after_state={
                "base_currency": row.base_currency,
                "rate": rate_text(rate),
                "observed_at": _text(observed_at),
                "source_reference": source_reference,
                "status": FxRateStatus.DRAFT.value,
            },
        )
        view = _view(session, row)
    return view


def update_draft(
    session_factory: sessionmaker[Session],
    *,
    actor: User,
    fx_rate_id: str,
    rate: Decimal | None,
    observed_at: dt.datetime | None,
    source_reference: str | None,
    context: RequestContext,
    clock: Clock = utc_now,
) -> FxRateVersionView:
    """Change a manual draft; `None` = not in the request.

    不存在 404 `FX_RATE_NOT_FOUND`；已退役 / 已丢弃 409 `FX_RATE_FINAL`；已发布 409
    `FX_RATE_NOT_DRAFT`；BNM 草稿 409 `FX_RATE_NOT_EDITABLE`。**没有实际变化**（`rate` 按数值
    比较）：200，什么都不写（`updated_at` 不变、不写审计）。
    """
    if rate is not None:
        _require_rate(rate)
    if observed_at is not None:
        _require_moment(observed_at, "observed_at")
    if source_reference is not None:
        _require_reference(source_reference)
    with session_scope(session_factory) as session:
        row = _require_version(session, fx_rate_id, for_update=True)
        _require_draft(row)
        if _source(row) is not FxRateSourceType.MANUAL:
            raise FxRateNotEditable
        now = _seconds(clock())

        before: dict[str, object] = {}
        after: dict[str, object] = {}
        if rate is not None and rate != row.rate:
            before["rate"], after["rate"] = rate_text(row.rate), rate_text(rate)
        if observed_at is not None and observed_at != row.observed_at:
            before["observed_at"] = _text(row.observed_at)
            after["observed_at"] = _text(observed_at)
        if source_reference is not None and source_reference != row.source_reference:
            before["source_reference"] = row.source_reference
            after["source_reference"] = source_reference
        if not after:
            return _view(session, row)

        rates.save_draft(
            session,
            row,
            rate=rate or row.rate,
            observed_at=observed_at or row.observed_at,
            source_reference=source_reference or row.source_reference,
            now=now,
        )
        record_audit(
            session,
            action=AuditAction.FX_RATE_UPDATE,
            context=context,
            now=now,
            actor=actor,
            entity_type=ENTITY_FX_RATE_VERSION,
            entity_id=row.public_id,
            before_state=before,
            after_state=after,
        )
        view = _view(session, row)
    return view


def discard_draft(
    session_factory: sessionmaker[Session],
    *,
    actor: User,
    fx_rate_id: str,
    context: RequestContext,
    clock: Clock = utc_now,
) -> FxRateVersionView:
    """DRAFT → DISCARDED and `FX_RATE_DISCARD` in one commit. BNM drafts too. The row stays."""
    with session_scope(session_factory) as session:
        row = _require_version(session, fx_rate_id, for_update=True)
        _require_draft(row)
        now = _seconds(clock())
        rates.mark_discarded(session, row, now=now)
        record_audit(
            session,
            action=AuditAction.FX_RATE_DISCARD,
            context=context,
            now=now,
            actor=actor,
            entity_type=ENTITY_FX_RATE_VERSION,
            entity_id=row.public_id,
            before_state={"status": FxRateStatus.DRAFT.value},
            after_state={"status": FxRateStatus.DISCARDED.value},
        )
        view = _view(session, row)
    return view


# --- 发布与退役 ---------------------------------------------------------------------


def publish(
    session_factory: sessionmaker[Session],
    *,
    actor: User,
    fx_rate_id: str,
    effective_from: dt.datetime | None,
    context: RequestContext,
    clock: Clock = utc_now,
) -> FxRateVersionView:
    """DRAFT → PUBLISHED inside the FX lock (设计 §2「发布：区间怎么定」).

    `effective_from` 是不带时区的 UTC 整秒（否则 422 `VALIDATION_ERROR`，与状态无关），或
    `None`。已发布：200，什么都不写，`effective_from` 被忽略。已退役 / 已丢弃：409
    `FX_RATE_FINAL`。F < `t`：422 `EFFECTIVE_FROM_IN_PAST`，先于 P1–P4 的任何判断；P3 里未给 F
    或 F 不晚于末尾版本的起点：409 `EFFECTIVE_FROM_CONFLICT`。都不写。
    """
    if effective_from is not None:
        _require_moment(effective_from, "effective_from")
    with session_scope(session_factory) as session:
        # ⚠️ 本事务的第一条语句就是这把锁；之后的读全是加锁读。
        rates.lock_fx_rates(session)
        # ⚠️ 拿到锁之后才取时间：`t` 必须晚于任何先于它持锁的计费（设计 §2）。
        now = clock()
        # 与改草稿、丢弃（同样 `FOR UPDATE` 版本行）串行：它先提交，这里读到的就是它写的内容。
        row = _require_version(session, fx_rate_id, for_update=True)
        status = _status(row)
        if status is FxRateStatus.PUBLISHED:
            return _view(session, row, lock=True)
        if status in _FINAL_STATUSES:
            raise FxRateFinal
        t = boundary_after(now)
        if effective_from is not None and effective_from < t:
            raise EffectiveFromInPast

        timeline = _timeline(rates.period_versions(session, row.base_currency))
        tail = timeline[-1] if timeline else None
        start, cut = _placement(tail, requested=effective_from, t=t)
        stamp = _seconds(now)
        before: dict[str, object] = {"status": FxRateStatus.DRAFT.value}
        after: dict[str, object] = {
            "status": FxRateStatus.PUBLISHED.value,
            "effective_from": _text(start),
            "effective_to": None,
            "approved_by_email": actor.email,
            "approved_at": _text(stamp),
        }
        # ⚠️ 写入顺序（设计 §2）：先截断前一个，再把本行从草稿改为已发布。
        if cut and tail is not None and start is not None:
            rates.truncate(session, tail, at=start, now=stamp)
            before["truncated_version"] = {"id": tail.public_id, "effective_to": None}
            after["truncated_version"] = {"id": tail.public_id, "effective_to": _text(start)}
        rates.mark_published(session, row, effective_from=start, approved_by=actor.id, now=stamp)
        _recheck(session, row.base_currency)
        record_audit(
            session,
            action=AuditAction.FX_RATE_PUBLISH,
            context=context,
            now=stamp,
            actor=actor,
            entity_type=ENTITY_FX_RATE_VERSION,
            entity_id=row.public_id,
            before_state=before,
            after_state=after,
        )
        view = _view(session, row, lock=True)
    return view


def retire(
    session_factory: sessionmaker[Session],
    *,
    actor: User,
    fx_rate_id: str,
    reason: str,
    context: RequestContext,
    clock: Clock = utc_now,
) -> FxRateVersionView:
    """PUBLISHED → RETIRED inside the FX lock (设计 §2「退役」R1–R5).

    - R1 未截断、已开始（起点为空或 ≤ `t`）：终点写 `t`，`t` 起该币种取不到汇率，直到下一个
      版本按 P4 从 `t` 起补上。起点恰好等于 `t`（同一秒里发布又退役）得到空区间 `[t, t)`，
      不恢复任何版本；
    - R2 未截断、尚未开始（起点 > `t`：撤销预约）：终点写成起点（空区间）；时间线里起点早于它的
      最后一个版本 P，只有仍是 PUBLISHED、终点等于它的起点（被它截断的）时恢复为未截断；
    - R3 已截断的 PUBLISHED、R4 草稿：409 `FX_RATE_NOT_RETIRABLE`；
    - R5 已退役 / 已丢弃：409 `FX_RATE_FINAL`。

    恢复 P 只影响 `occurred_at ≥ 本版本起点 > t` 的时刻：它们都还没有被计费。
    """
    with session_scope(session_factory) as session:
        # ⚠️ 本事务的第一条语句就是这把锁；之后的读全是加锁读（同 `publish`）。
        rates.lock_fx_rates(session)
        # ⚠️ 拿到锁之后才取时间（同 `publish`）。
        now = clock()
        row = _require_version(session, fx_rate_id, for_update=True)
        status = _status(row)
        if status in _FINAL_STATUSES:
            raise FxRateFinal
        if status is not FxRateStatus.PUBLISHED or row.effective_to is not None:
            raise FxRateNotRetirable
        t = boundary_after(now)
        stamp = _seconds(now)
        timeline = _timeline(rates.period_versions(session, row.base_currency))
        start = row.effective_from
        before: dict[str, object] = {
            "status": FxRateStatus.PUBLISHED.value,
            "effective_from": _text(start),
            "effective_to": None,
        }
        after: dict[str, object] = {
            "status": FxRateStatus.RETIRED.value,
            "effective_from": _text(start),
        }
        if start is None or start <= t:
            # R1
            end = t
            rates.mark_retired(session, row, effective_to=end, now=stamp)
        else:
            # R2
            end = start
            key = rates.version_order(row)
            earlier = [item for item in timeline if rates.version_order(item) < key]
            previous = earlier[-1] if earlier else None
            # ⚠️ 写入顺序（设计 §2）：先把被撤销的一行改成空区间，再恢复前一个。
            rates.mark_retired(session, row, effective_to=end, now=stamp)
            # 设计 §2「恢复 P 的条件」：P 以退役结束于那一刻的不恢复（退役不改）。
            if (
                previous is not None
                and _status(previous) is FxRateStatus.PUBLISHED
                and previous.effective_to == start
            ):
                rates.restore_open(session, previous, now=stamp)
                after["restored_version"] = {"id": previous.public_id}
        after["effective_to"] = _text(end)
        after["reason"] = reason
        _recheck(session, row.base_currency)
        record_audit(
            session,
            action=AuditAction.FX_RATE_RETIRE,
            context=context,
            now=stamp,
            actor=actor,
            entity_type=ENTITY_FX_RATE_VERSION,
            entity_id=row.public_id,
            before_state=before,
            after_state=after,
            reason=reason,
        )
        view = _view(session, row, lock=True)
    return view


# --- 查询（AIH-TASK-041）：普通读，不拿 FX 锁 -------------------------------------------


@dataclass(frozen=True)
class FxFetchAttemptView:
    """One BNM fetch attempt. No id of its own: read-only, never referenced singly (设计 v3)."""

    base_currency: str
    source: FxRateSourceType
    requested_date: dt.date
    outcome: FxFetchOutcome
    quote_date: dt.date | None
    error_code: str | None
    # `NEW_DRAFT` 时是那条草稿的 public_id，否则为空。
    fx_rate_id: str | None
    attempted_at: dt.datetime


def _offset(page: int, page_size: int) -> int:
    return (page - 1) * page_size


def get_fx_rate(session_factory: sessionmaker[Session], fx_rate_id: str) -> FxRateVersionView:
    """Unknown: 404 `FX_RATE_NOT_FOUND`. A plain read: no FX lock (设计 §2「接口」)."""
    with session_factory() as session:
        return _view(session, _require_version(session, fx_rate_id))


def list_fx_rates(
    session_factory: sessionmaker[Session],
    *,
    base_currency: str | None,
    status: FxRateStatus | None,
    source: FxRateSourceType | None,
    page: int,
    page_size: int,
) -> tuple[list[FxRateVersionView], int]:
    """Versions newest first (`id` descending), one page, and the total. No FX lock."""
    with session_factory() as session:
        rows, total = rates.list_versions(
            session,
            base_currency=base_currency,
            status=status,
            source=source,
            offset=_offset(page, page_size),
            limit=page_size,
        )
        return [_view(session, row) for row in rows], total


def list_fetch_attempts(
    session_factory: sessionmaker[Session],
    *,
    base_currency: str | None,
    outcome: FxFetchOutcome | None,
    page: int,
    page_size: int,
) -> tuple[list[FxFetchAttemptView], int]:
    """Attempts by `attempted_at`, then `id`, both descending; one page and the total."""
    with session_factory() as session:
        rows, total = rates.list_fetch_attempts(
            session,
            base_currency=base_currency,
            outcome=outcome,
            offset=_offset(page, page_size),
            limit=page_size,
        )
        views = [
            FxFetchAttemptView(
                base_currency=row.attempt.base_currency,
                source=FxRateSourceType(row.attempt.source),
                requested_date=row.attempt.requested_date,
                outcome=FxFetchOutcome(row.attempt.outcome),
                quote_date=row.attempt.quote_date,
                error_code=row.attempt.error_code,
                fx_rate_id=row.fx_rate_public_id,
                attempted_at=row.attempt.attempted_at,
            )
            for row in rows
        ]
        return views, total


__all__ = [
    "ENTITY_FX_RATE_VERSION",
    "Clock",
    "EffectiveFromConflict",
    "EffectiveFromInPast",
    "FxFetchAttemptView",
    "FxPeriodsBroken",
    "FxRateFinal",
    "FxRateInvalid",
    "FxRateNotDraft",
    "FxRateNotEditable",
    "FxRateNotFound",
    "FxRateNotRetirable",
    "FxRateVersionView",
    "create_draft",
    "discard_draft",
    "get_fx_rate",
    "is_empty_period",
    "list_fetch_attempts",
    "list_fx_rates",
    "publish",
    "rate_text",
    "retire",
    "update_draft",
    "verify_periods",
]

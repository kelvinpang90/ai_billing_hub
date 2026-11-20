"""Provider cost reconciliation: preview, confirm, correct (design gate #755 v1, AIH-TASK-244).

spec §14、§17 / §17.1、§19、§66、§74.5；Invariant 4、5、6、10、12、13。设计 §2「服务」：
管理员按供应商账单或 Console 成本页录入某个供应商 × UTC 自然月的实际成本（来源币种原值，不含税），
先预览（不写库）再确认（写一行不可变记录与一条审计）；更正 = 追加一行取代行，必填理由。

⚠️ **检查顺序**（`preview` 与 `confirm` 共用，第一个不过的就抛出）：
1a 理由 → 2 月份 → 3 供应商 → 4 取代目标 / 已有根 → 5 月末汇率（非 MYR：先 FX 共享锁）
→ 5a 换算后金额的范围 → 6 聚合与币种一致 →（仅 `confirm`）7 期望值 → 8 插入记录、审计、提交。
服务层不重复校验 `source_total`、`source_currency`、`source_document_reference` 的格式（接口层
schema 保证），自己的校验只有第 1a、5a 步。所有错误都只有 `code` 与 `message`，不带 details。

⚠️ **事务边界**（设计 §2「事务边界」、INV-13）：预览一个事务、最后回滚，不写任何行；确认一个事务：
FX 共享锁 → 取汇率（加锁读）→ 聚合（一致读）→ INSERT 记录 → INSERT 审计 → 提交。第 4 步是普通读，
并发仲裁靠唯一约束（设计 §4）：插入撞唯一约束时回滚，按请求形状映射为 409，回滚后不再读库，
不在这里重试。详情与总览只读、不加锁、不提交。

⚠️ **金额**（设计 §2「金额与精度」、INV-10）：全程 Decimal、不经 float。换算在 `PRICING_CONTEXT`
里乘、只舍入一次；差额是 Decimal 相减；差额率是 28 位上下文的 Decimal 除法、分母为 0 时 None；
阈值用精确 Decimal 比较。视图只放类型化的值，字符串格式化都在接口层 schema 里做。

⚠️ 不写钱包、账本、对账单与用量事件（INV-4、INV-5、INV-6、INV-12），不 import 这些模块；
不写日志（金额与来源说明只进记录与审计；审计里也不放来源说明，设计 §6）。

⚠️ `FIRST_RECONCILIATION_MONTH` 的前提：本平台专用的供应商密钥须在 2026-12-01 00:00 UTC 之前换好
（Kelvin 2026-11-16 定，设计 §10「已定」第 4 条）；晚了就在写入任何记录之前用只改这个常量的
小 PR 推到换钥后的第一个完整 UTC 月。读它的代码一律经本模块属性读取（测试会 monkeypatch）。
"""

from __future__ import annotations

import datetime as dt
import re
import uuid
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext
from typing import Final

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.core.errors import AppError
from app.models.ai_catalog import AiProvider
from app.models.auth import AuditAction, User
from app.models.base import MONEY_PRECISION, MONEY_SCALE, quantize_money
from app.models.fx_rates import QUOTE_CURRENCY, FxRateVersion
from app.models.provider_cost_reconciliations import REASON_LENGTH, ProviderCostReconciliation
from app.repositories import fx_rates as rates
from app.repositories import provider_cost_reconciliations as reconciliations
from app.repositories.fx_rates import ResolvedFxRate
from app.repositories.provider_cost_reconciliations import ScopeEstimate
from app.schemas.customers import money_text
from app.services.ai_catalog import ProviderNotFound
from app.services.auth import RequestContext, record_audit
from app.services.fx_rates import rate_text
from app.services.pricing_engine import PRICING_CONTEXT

# 设计 §2：早于它的月份一律不能对账。不读环境变量、不设覆盖参数（写法同
# app/services/statements.py 的 `FIRST_STATEMENT_PERIOD`）。前提见模块说明。
FIRST_RECONCILIATION_MONTH: Final = "2026-12"
# 范围只有一种：经 AI 模块发出的调用（`event_id` 出现在 `ai_usage_outbox` 里）。
SCOPE_AI_MODULE: Final = "AI_MODULE"
# 覆盖方式只有一种：供应商 × 期间的一个合计数。
COVERAGE_PERIOD_AGGREGATE: Final = "PERIOD_AGGREGATE"
# 来源币种差额率的高亮阈值（只用于页面，不告警）。
VARIANCE_THRESHOLD: Final = Decimal("0.05")
# 月份总览至多列这么多个月。
MONTHS_SHOWN: Final = 24

# 审计的 entity_type；entity_id 是记录的 public_id。
ENTITY_PROVIDER_COST_RECONCILIATION: Final = "provider_cost_reconciliation"

STATUS_RECONCILED: Final = "RECONCILED"
STATUS_NOT_RECONCILED: Final = "NOT_RECONCILED"

# 月份标签的形状与年份范围（两端都含）。
_MONTH_LABEL: Final = re.compile(r"([0-9]{4})-([0-9]{2})")
_MIN_YEAR: Final = 2000
_MAX_YEAR: Final = 2999

# 汇率取期间最后一刻生效的版本（设计 §2「时间语义」）。
_ONE_MICROSECOND: Final = dt.timedelta(microseconds=1)
# DECIMAL(20,8) 放得下的上界（不含）：整数部分最多 12 位（第 5a 步）。
_MONEY_LIMIT: Final = Decimal(10) ** (MONEY_PRECISION - MONEY_SCALE)
# 差额率：28 位有效数字的 Decimal 除法（设计 §2「金额与精度」），不依赖线程的当前上下文。
_RATIO_CONTEXT: Final = Context(prec=28, rounding=ROUND_HALF_EVEN, traps=[], flags=[])

# MySQL 的 ER_DUP_ENTRY；SQLite 的唯一约束错误只能从消息认出（同 app/services/payments.py）。
_ER_DUP_ENTRY: Final = 1062
_SQLITE_UNIQUE: Final = "UNIQUE constraint failed"


# --- 错误（设计 §2「检查顺序」；都只有 code 与 message） ------------------------------------


class ReconciliationReasonInvalid(AppError):
    def __init__(self) -> None:
        super().__init__(
            "A correction needs a reason of 1-255 characters; a first record has none.",
            code="RECONCILIATION_REASON_INVALID",
            http_status=422,
        )


class ReconciliationMonthInvalid(AppError):
    def __init__(self) -> None:
        super().__init__(
            "The billing month is not a completed month that can be reconciled.",
            code="RECONCILIATION_MONTH_INVALID",
            http_status=422,
        )


class ReconciliationNotFound(AppError):
    def __init__(self) -> None:
        super().__init__(
            "Provider cost reconciliation not found.",
            code="PROVIDER_COST_RECONCILIATION_NOT_FOUND",
            http_status=404,
        )


class ReconciliationSupersedesMismatch(AppError):
    def __init__(self) -> None:
        super().__init__(
            "The superseded reconciliation is for another provider, scope or month.",
            code="RECONCILIATION_SUPERSEDES_MISMATCH",
            http_status=422,
        )


class ReconciliationNotCurrent(AppError):
    def __init__(self) -> None:
        super().__init__(
            "The reconciliation has already been superseded.",
            code="RECONCILIATION_NOT_CURRENT",
            http_status=409,
        )


class ReconciliationExists(AppError):
    def __init__(self) -> None:
        super().__init__(
            "The month has already been reconciled; correct the current record instead.",
            code="RECONCILIATION_EXISTS",
            http_status=409,
        )


class FxRateUnavailable(AppError):
    def __init__(self) -> None:
        super().__init__(
            "No published FX rate is in effect at the end of the month.",
            code="FX_RATE_UNAVAILABLE",
            http_status=422,
        )


class ReconciliationAmountOutOfRange(AppError):
    def __init__(self) -> None:
        super().__init__(
            "The converted MYR cost is out of range.",
            code="RECONCILIATION_AMOUNT_OUT_OF_RANGE",
            http_status=422,
        )


class SourceCurrencyMismatch(AppError):
    def __init__(self) -> None:
        super().__init__(
            "Some in-scope events are in another source currency.",
            code="SOURCE_CURRENCY_MISMATCH",
            http_status=422,
        )


class ReconciliationEstimateChanged(AppError):
    def __init__(self) -> None:
        super().__init__(
            "The estimate changed since the preview; preview again.",
            code="RECONCILIATION_ESTIMATE_CHANGED",
            http_status=409,
        )


# --- 视图（设计 §2「服务返回的视图」，逐字照抄） ---------------------------------------------


@dataclass(frozen=True)
class ReconciliationView:
    reconciliation_id: str | None  # public_id；预览为 None
    provider_code: str
    scope: str
    billing_month: str  # "YYYY-MM"
    period_start: dt.datetime  # 不带时区的 UTC
    period_end: dt.datetime
    source_document_reference: str
    source_currency: str
    source_total: Decimal
    fx_rate_version_id: str | None  # 汇率版本的 public_id；MYR 为 None
    fx_rate_applied: Decimal | None  # MYR 为 None
    fx_rate_moment: dt.datetime | None  # period_end − 1µs；MYR 为 None
    reconciled_cost_myr: Decimal
    estimated_event_count: int
    estimated_source_cost: Decimal
    estimated_cost_myr: Decimal
    source_variance: Decimal
    source_variance_rate: Decimal | None
    myr_variance: Decimal
    myr_variance_rate: Decimal | None
    exceeds_threshold: bool
    unprocessed_event_count: int
    outbox_unsettled_count: int
    outbox_discarded_count: int
    coverage_method: str
    supersedes_id: str | None  # 被取代行的 public_id
    reason: str | None
    is_current: bool | None  # 预览为 None
    created_at: dt.datetime | None  # 预览为 None
    created_by_email: str | None  # 预览为 None


@dataclass(frozen=True)
class MonthView:
    billing_month: str
    period_start: dt.datetime
    period_end: dt.datetime
    status: str  # "RECONCILED" | "NOT_RECONCILED"
    current: ReconciliationView | None


@dataclass(frozen=True)
class LiveEstimate:
    estimated_event_count: int
    estimated_source_cost: Decimal
    estimated_cost_myr: Decimal
    currency_mismatch_count: int
    unprocessed_event_count: int


@dataclass(frozen=True)
class ReconciliationDetail:
    reconciliation: ReconciliationView
    chain: tuple[ReconciliationView, ...]  # id 升序
    live_estimate: LiveEstimate
    estimate_changed_since_confirmation: bool


# --- 期间与纯计算 ------------------------------------------------------------------------


def reconciliation_period(label: str) -> tuple[dt.datetime, dt.datetime]:
    """`YYYY-MM` → `(月首 00:00, 下月首 00:00)`, naive UTC.

    范围在任何日期运算之前判：不是 `YYYY-MM`、年份不在 2000–2999、月份不在 1–12，
    都是 `ValueError`。
    """
    found = _MONTH_LABEL.fullmatch(label) if isinstance(label, str) else None
    if found is None:
        raise ValueError("billing month must be YYYY-MM")
    year, month = int(found[1]), int(found[2])
    if not _MIN_YEAR <= year <= _MAX_YEAR or not 1 <= month <= 12:
        raise ValueError("billing month out of range")
    start = dt.datetime(year, month, 1)
    end = dt.datetime(year + 1, 1, 1) if month == 12 else dt.datetime(year, month + 1, 1)
    return start, end


def _month_label(moment: dt.datetime) -> str:
    return f"{moment.year:04d}-{moment.month:02d}"


def _previous_month(label: str) -> str:
    start, _ = reconciliation_period(label)
    return _month_label(start - _ONE_MICROSECOND)


def allocate(
    covered_estimate: Decimal, reconciled_cost_myr: Decimal, scope_estimated_myr: Decimal
) -> Decimal | None:
    """`covered × R ÷ E` in `PRICING_CONTEXT`, **not rounded**; None when `E = 0`.

    纯函数（分摊设计的 AIH-TASK-248 用）。
    """
    if scope_estimated_myr == 0:
        return None
    with localcontext(PRICING_CONTEXT):
        return covered_estimate * reconciled_cost_myr / scope_estimated_myr


def _ratio(numerator: Decimal, denominator: Decimal) -> Decimal | None:
    if denominator == 0:
        return None
    with localcontext(_RATIO_CONTEXT):
        return numerator / denominator


@dataclass(frozen=True)
class _Variance:
    source_variance: Decimal
    source_variance_rate: Decimal | None
    myr_variance: Decimal
    myr_variance_rate: Decimal | None
    exceeds_threshold: bool


def _variance(
    *,
    source_total: Decimal,
    reconciled_cost_myr: Decimal,
    estimated_source_cost: Decimal,
    estimated_cost_myr: Decimal,
) -> _Variance:
    """设计 §2「口径」的两种差额、差额率与超阈值。差额是精确的 Decimal 相减。"""
    with localcontext(PRICING_CONTEXT):
        source_variance = source_total - estimated_source_cost
        myr_variance = reconciled_cost_myr - estimated_cost_myr
    source_rate = _ratio(source_variance, estimated_source_cost)
    exceeds = (source_rate is not None and abs(source_rate) >= VARIANCE_THRESHOLD) or (
        estimated_source_cost == 0 and source_total > 0
    )
    return _Variance(
        source_variance=source_variance,
        source_variance_rate=source_rate,
        myr_variance=myr_variance,
        myr_variance_rate=_ratio(myr_variance, estimated_cost_myr),
        exceeds_threshold=exceeds,
    )


def _reconciled_cost(source_total: Decimal, fx: ResolvedFxRate | None) -> Decimal:
    """MYR 录入时就是原值；否则 `PRICING_CONTEXT` 内乘、`quantize_money` 只舍入一次。"""
    if fx is None:
        return source_total
    with localcontext(PRICING_CONTEXT):
        product = source_total * fx.rate
    return quantize_money(product)


# --- 测试钩子 -----------------------------------------------------------------------------


def _after_audit_before_commit(session: Session) -> None:
    """Does nothing. ⚠️ 只供并发用例 monkeypatch 来阻塞：确认写完记录与审计、提交之前调用。"""


# --- 检查与聚合（preview 与 confirm 共用） ---------------------------------------------------


@dataclass(frozen=True)
class _Request:
    provider_code: str
    billing_month: str
    source_currency: str
    source_total: Decimal
    source_document_reference: str
    supersedes_id: str | None
    reason: str | None


@dataclass(frozen=True)
class _Outcome:
    """Everything steps 2–6 worked out; the preview and the confirmation share it."""

    request: _Request
    provider: AiProvider
    period_start: dt.datetime
    period_end: dt.datetime
    superseded: ProviderCostReconciliation | None
    fx: ResolvedFxRate | None
    reconciled_cost_myr: Decimal
    estimate: ScopeEstimate
    unprocessed_event_count: int
    outbox_unsettled_count: int
    outbox_discarded_count: int


def _normalize(
    *,
    provider_code: str,
    billing_month: str,
    source_currency: str,
    source_total: Decimal,
    source_document_reference: str,
    supersedes_id: str | None,
    reason: str | None,
) -> _Request:
    """Step 1a: the reason, trimmed, is 1–255 characters and comes with `supersedes_id` only."""
    trimmed = reason.strip() if reason is not None else None
    if trimmed is not None and not 1 <= len(trimmed) <= REASON_LENGTH:
        raise ReconciliationReasonInvalid
    if (supersedes_id is None) != (trimmed is None):
        raise ReconciliationReasonInvalid
    return _Request(
        provider_code=provider_code,
        billing_month=billing_month,
        source_currency=source_currency,
        source_total=source_total,
        source_document_reference=source_document_reference.strip(),
        supersedes_id=supersedes_id,
        reason=trimmed,
    )


def _completed_period(billing_month: str, now: dt.datetime) -> tuple[dt.datetime, dt.datetime]:
    """Step 2: a valid month, not before the first one, already ended at `now`."""
    try:
        start, end = reconciliation_period(billing_month)
    except ValueError:
        raise ReconciliationMonthInvalid from None
    if billing_month < FIRST_RECONCILIATION_MONTH or end > now:
        raise ReconciliationMonthInvalid
    return start, end


def _provider(session: Session, provider_code: str) -> AiProvider:
    statement = select(AiProvider).where(AiProvider.code == provider_code)
    provider = session.execute(statement).scalar_one_or_none()
    if provider is None:
        raise ProviderNotFound
    return provider


def _check_chain(
    session: Session, request: _Request, provider: AiProvider
) -> ProviderCostReconciliation | None:
    """Step 4, a plain read: the superseded row is current and of the same month; else no root.

    并发由第 8 步的唯一约束仲裁（设计 §4），这里只给出更早的错误。
    """
    if request.supersedes_id is None:
        existing = reconciliations.current_record(
            session,
            provider_id=provider.id,
            scope=SCOPE_AI_MODULE,
            billing_month=request.billing_month,
        )
        if existing is not None:
            raise ReconciliationExists
        return None
    superseded = reconciliations.get_by_public_id(session, request.supersedes_id)
    if superseded is None:
        raise ReconciliationNotFound
    if (superseded.provider_id, superseded.scope, superseded.billing_month) != (
        provider.id,
        SCOPE_AI_MODULE,
        request.billing_month,
    ):
        raise ReconciliationSupersedesMismatch
    if reconciliations.has_successor(session, superseded.id):
        raise ReconciliationNotCurrent
    return superseded


def _month_end_rate(
    session: Session, source_currency: str, period_end: dt.datetime
) -> ResolvedFxRate | None:
    """Step 5: the version in effect at `period_end − 1µs`, under the shared FX lock."""
    if source_currency == QUOTE_CURRENCY:
        return None
    rates.lock_fx_rates_shared(session)
    resolved = rates.resolve_fx_rate(session, source_currency, period_end - _ONE_MICROSECOND)
    if resolved is None:
        raise FxRateUnavailable
    return resolved


def _evaluate(session: Session, request: _Request, now: dt.datetime) -> _Outcome:
    """Steps 2–6 in the caller's transaction (step 1a is `_normalize`)."""
    period_start, period_end = _completed_period(request.billing_month, now)
    provider = _provider(session, request.provider_code)
    superseded = _check_chain(session, request, provider)
    fx = _month_end_rate(session, request.source_currency, period_end)
    reconciled = _reconciled_cost(request.source_total, fx)
    if reconciled >= _MONEY_LIMIT:
        raise ReconciliationAmountOutOfRange
    estimate = reconciliations.scope_estimate(
        session,
        provider_id=provider.id,
        period_start=period_start,
        period_end=period_end,
        source_currency=request.source_currency,
    )
    unprocessed = reconciliations.unprocessed_count(
        session, period_start=period_start, period_end=period_end
    )
    unsettled, discarded = reconciliations.outbox_counts(
        session, period_start=period_start, period_end=period_end
    )
    if estimate.currency_mismatch_count > 0:
        raise SourceCurrencyMismatch
    return _Outcome(
        request=request,
        provider=provider,
        period_start=period_start,
        period_end=period_end,
        superseded=superseded,
        fx=fx,
        reconciled_cost_myr=reconciled,
        estimate=estimate,
        unprocessed_event_count=unprocessed,
        outbox_unsettled_count=unsettled,
        outbox_discarded_count=discarded,
    )


def _outcome_view(
    outcome: _Outcome,
    *,
    reconciliation_id: str | None = None,
    is_current: bool | None = None,
    created_at: dt.datetime | None = None,
    created_by_email: str | None = None,
) -> ReconciliationView:
    request = outcome.request
    estimate = outcome.estimate
    variance = _variance(
        source_total=request.source_total,
        reconciled_cost_myr=outcome.reconciled_cost_myr,
        estimated_source_cost=estimate.estimated_source_cost,
        estimated_cost_myr=estimate.estimated_cost_myr,
    )
    fx = outcome.fx
    return ReconciliationView(
        reconciliation_id=reconciliation_id,
        provider_code=outcome.provider.code,
        scope=SCOPE_AI_MODULE,
        billing_month=request.billing_month,
        period_start=outcome.period_start,
        period_end=outcome.period_end,
        source_document_reference=request.source_document_reference,
        source_currency=request.source_currency,
        source_total=request.source_total,
        fx_rate_version_id=fx.public_id if fx is not None else None,
        fx_rate_applied=fx.rate if fx is not None else None,
        fx_rate_moment=outcome.period_end - _ONE_MICROSECOND if fx is not None else None,
        reconciled_cost_myr=outcome.reconciled_cost_myr,
        estimated_event_count=estimate.event_count,
        estimated_source_cost=estimate.estimated_source_cost,
        estimated_cost_myr=estimate.estimated_cost_myr,
        source_variance=variance.source_variance,
        source_variance_rate=variance.source_variance_rate,
        myr_variance=variance.myr_variance,
        myr_variance_rate=variance.myr_variance_rate,
        exceeds_threshold=variance.exceeds_threshold,
        unprocessed_event_count=outcome.unprocessed_event_count,
        outbox_unsettled_count=outcome.outbox_unsettled_count,
        outbox_discarded_count=outcome.outbox_discarded_count,
        coverage_method=COVERAGE_PERIOD_AGGREGATE,
        supersedes_id=outcome.superseded.public_id if outcome.superseded is not None else None,
        reason=request.reason,
        is_current=is_current,
        created_at=created_at,
        created_by_email=created_by_email,
    )


# --- 预览与确认 ---------------------------------------------------------------------------


def preview(
    factory: sessionmaker[Session],
    *,
    provider_code: str,
    billing_month: str,
    source_currency: str,
    source_total: Decimal,
    source_document_reference: str,
    supersedes_id: str | None,
    reason: str | None,
    now: dt.datetime,
) -> ReconciliationView:
    """Steps 1a–6 in one transaction that is rolled back: nothing is written."""
    request = _normalize(
        provider_code=provider_code,
        billing_month=billing_month,
        source_currency=source_currency,
        source_total=source_total,
        source_document_reference=source_document_reference,
        supersedes_id=supersedes_id,
        reason=reason,
    )
    with factory() as session:
        try:
            return _outcome_view(_evaluate(session, request, now))
        finally:
            session.rollback()


def _is_unique_violation(error: IntegrityError) -> bool:
    original = error.orig
    args = getattr(original, "args", ())
    if args and args[0] == _ER_DUP_ENTRY:
        return True
    return str(original).startswith(_SQLITE_UNIQUE)


def _audit_states(
    outcome: _Outcome, public_id: str
) -> tuple[dict[str, object] | None, dict[str, object]]:
    """设计 §2「审计」：金额用 `money_text`、汇率用 `rate_text`，版本与行用 public_id。

    不放来源说明（可能含工作区名称）。
    """
    request = outcome.request
    superseded = outcome.superseded
    before: dict[str, object] | None = None
    if superseded is not None:
        before = {
            "public_id": superseded.public_id,
            "source_currency": superseded.source_currency,
            "source_total": money_text(superseded.source_total),
            "reconciled_cost_myr": money_text(superseded.reconciled_cost_myr),
        }
    fx = outcome.fx
    after: dict[str, object] = {
        "public_id": public_id,
        "provider_code": outcome.provider.code,
        "scope": SCOPE_AI_MODULE,
        "billing_month": request.billing_month,
        "source_currency": request.source_currency,
        "source_total": money_text(request.source_total),
        "fx_rate_version_id": fx.public_id if fx is not None else None,
        "fx_rate_applied": rate_text(fx.rate) if fx is not None else None,
        "reconciled_cost_myr": money_text(outcome.reconciled_cost_myr),
        "estimated_event_count": outcome.estimate.event_count,
        "estimated_source_cost": money_text(outcome.estimate.estimated_source_cost),
        "estimated_cost_myr": money_text(outcome.estimate.estimated_cost_myr),
        "supersedes_id": superseded.public_id if superseded is not None else None,
    }
    return before, after


def confirm(
    factory: sessionmaker[Session],
    *,
    actor: User,
    context: RequestContext,
    provider_code: str,
    billing_month: str,
    source_currency: str,
    source_total: Decimal,
    source_document_reference: str,
    supersedes_id: str | None,
    reason: str | None,
    expected_event_count: int,
    expected_estimated_source_cost: Decimal,
    now: dt.datetime,
) -> ReconciliationView:
    """Steps 1a–8: one immutable row and one audit row in one transaction (INV-13).

    插入撞唯一约束：回滚，不带 `supersedes_id` → `ReconciliationExists`，带 →
    `ReconciliationNotCurrent`（只按请求形状判，不看约束名：SQLite 的错误文本里没有索引名）。
    其他 `IntegrityError` 与任何异常都整体回滚、原样抛出。
    """
    request = _normalize(
        provider_code=provider_code,
        billing_month=billing_month,
        source_currency=source_currency,
        source_total=source_total,
        source_document_reference=source_document_reference,
        supersedes_id=supersedes_id,
        reason=reason,
    )
    created_at = now.replace(microsecond=0)
    public_id = str(uuid.uuid4())
    session = factory()
    try:
        outcome = _evaluate(session, request, now)
        estimate = outcome.estimate
        if (
            estimate.event_count != expected_event_count
            or estimate.estimated_source_cost != expected_estimated_source_cost
        ):
            raise ReconciliationEstimateChanged
        fx = outcome.fx
        try:
            reconciliations.insert_record(
                session,
                public_id=public_id,
                provider_id=outcome.provider.id,
                scope=SCOPE_AI_MODULE,
                billing_month=request.billing_month,
                period_start=outcome.period_start,
                period_end=outcome.period_end,
                source_document_reference=request.source_document_reference,
                source_currency=request.source_currency,
                source_total=request.source_total,
                fx_rate_version_id=fx.version_id if fx is not None else None,
                fx_rate_applied=fx.rate if fx is not None else None,
                reconciled_cost_myr=outcome.reconciled_cost_myr,
                coverage_method=COVERAGE_PERIOD_AGGREGATE,
                estimated_event_count=estimate.event_count,
                unprocessed_event_count=outcome.unprocessed_event_count,
                outbox_unsettled_count=outcome.outbox_unsettled_count,
                outbox_discarded_count=outcome.outbox_discarded_count,
                estimated_source_cost=estimate.estimated_source_cost,
                estimated_cost_myr=estimate.estimated_cost_myr,
                supersedes_id=outcome.superseded.id if outcome.superseded is not None else None,
                reason=request.reason,
                created_by=actor.id,
                created_at=created_at,
            )
        except IntegrityError as error:
            # 回滚后不再读库：映射只看请求形状（设计 §2 第 8 步）。
            session.rollback()
            if not _is_unique_violation(error):
                raise
            if request.supersedes_id is None:
                raise ReconciliationExists from None
            raise ReconciliationNotCurrent from None
        before, after = _audit_states(outcome, public_id)
        record_audit(
            session,
            action=AuditAction.PROVIDER_COST_RECONCILIATION_CREATE,
            context=context,
            now=created_at,
            actor=actor,
            entity_type=ENTITY_PROVIDER_COST_RECONCILIATION,
            entity_id=public_id,
            before_state=before,
            after_state=after,
            reason=request.reason,
        )
        session.flush()
        _after_audit_before_commit(session)
        session.commit()
    except BaseException:
        session.rollback()
        raise
    finally:
        session.close()
    return _outcome_view(
        outcome,
        reconciliation_id=public_id,
        is_current=True,
        created_at=created_at,
        created_by_email=actor.email,
    )


# --- 读：总览与详情（只读会话，不加锁、不提交） --------------------------------------------


def _record_view(
    session: Session, row: ProviderCostReconciliation, *, provider_code: str
) -> ReconciliationView:
    """A stored row as the admin side sees it: the snapshot, never recomputed from live data."""
    fx_public_id: str | None = None
    if row.fx_rate_version_id is not None:
        fx_public_id = session.execute(
            select(FxRateVersion.public_id).where(FxRateVersion.id == row.fx_rate_version_id)
        ).scalar_one()
    superseded_public_id: str | None = None
    if row.supersedes_id is not None:
        superseded_public_id = session.execute(
            select(ProviderCostReconciliation.public_id).where(
                ProviderCostReconciliation.id == row.supersedes_id
            )
        ).scalar_one()
    email = session.execute(select(User.email).where(User.id == row.created_by)).scalar_one()
    variance = _variance(
        source_total=row.source_total,
        reconciled_cost_myr=row.reconciled_cost_myr,
        estimated_source_cost=row.estimated_source_cost,
        estimated_cost_myr=row.estimated_cost_myr,
    )
    return ReconciliationView(
        reconciliation_id=row.public_id,
        provider_code=provider_code,
        scope=row.scope,
        billing_month=row.billing_month,
        period_start=row.period_start,
        period_end=row.period_end,
        source_document_reference=row.source_document_reference,
        source_currency=row.source_currency,
        source_total=row.source_total,
        fx_rate_version_id=fx_public_id,
        fx_rate_applied=row.fx_rate_applied,
        fx_rate_moment=row.period_end - _ONE_MICROSECOND if fx_public_id is not None else None,
        reconciled_cost_myr=row.reconciled_cost_myr,
        estimated_event_count=int(row.estimated_event_count),
        estimated_source_cost=row.estimated_source_cost,
        estimated_cost_myr=row.estimated_cost_myr,
        source_variance=variance.source_variance,
        source_variance_rate=variance.source_variance_rate,
        myr_variance=variance.myr_variance,
        myr_variance_rate=variance.myr_variance_rate,
        exceeds_threshold=variance.exceeds_threshold,
        unprocessed_event_count=int(row.unprocessed_event_count),
        outbox_unsettled_count=int(row.outbox_unsettled_count),
        outbox_discarded_count=int(row.outbox_discarded_count),
        coverage_method=row.coverage_method,
        supersedes_id=superseded_public_id,
        reason=row.reason,
        is_current=not reconciliations.has_successor(session, row.id),
        created_at=row.created_at,
        created_by_email=email,
    )


def months_overview(
    factory: sessionmaker[Session], *, provider_code: str, now: dt.datetime
) -> list[MonthView]:
    """From the latest ended UTC month back to `FIRST_RECONCILIATION_MONTH`, at most 24.

    供应商代码查不到 → `ProviderNotFound`，先于任何别的查询。首月还没结束时为空列表。
    """
    with factory() as session:
        provider = _provider(session, provider_code)
        # `now` 所在的月还没结束；它的上一个月在 `now` 之前（或恰好在 `now`）结束。
        label = _previous_month(_month_label(now))
        items: list[MonthView] = []
        while label >= FIRST_RECONCILIATION_MONTH and len(items) < MONTHS_SHOWN:
            start, end = reconciliation_period(label)
            head = reconciliations.current_record(
                session, provider_id=provider.id, scope=SCOPE_AI_MODULE, billing_month=label
            )
            current = (
                _record_view(session, head, provider_code=provider.code)
                if head is not None
                else None
            )
            items.append(
                MonthView(
                    billing_month=label,
                    period_start=start,
                    period_end=end,
                    status=STATUS_RECONCILED if current is not None else STATUS_NOT_RECONCILED,
                    current=current,
                )
            )
            label = _previous_month(label)
        return items


def get_detail(
    factory: sessionmaker[Session], public_id: str, *, now: dt.datetime
) -> ReconciliationDetail:
    """The row, its whole chain by `id`, and the live estimate of its provider, period, currency.

    `estimate_changed_since_confirmation`：实时的事件数、来源成本、MYR 估算任一与行上快照不等，
    或实时 `currency_mismatch_count > 0`；未处理数不参与判定。只读、不加锁、不取汇率、
    不重算差额（差额来自行上的快照）。
    """
    with factory() as session:
        row = reconciliations.get_by_public_id(session, public_id)
        if row is None:
            raise ReconciliationNotFound
        provider_code = session.execute(
            select(AiProvider.code).where(AiProvider.id == row.provider_id)
        ).scalar_one()
        chain = tuple(
            _record_view(session, link, provider_code=provider_code)
            for link in reconciliations.chain(
                session,
                provider_id=row.provider_id,
                scope=row.scope,
                billing_month=row.billing_month,
            )
        )
        [view] = [link for link in chain if link.reconciliation_id == row.public_id]
        estimate = reconciliations.scope_estimate(
            session,
            provider_id=row.provider_id,
            period_start=row.period_start,
            period_end=row.period_end,
            source_currency=row.source_currency,
        )
        unprocessed = reconciliations.unprocessed_count(
            session, period_start=row.period_start, period_end=row.period_end
        )
        changed = (
            estimate.event_count != row.estimated_event_count
            or estimate.estimated_source_cost != row.estimated_source_cost
            or estimate.estimated_cost_myr != row.estimated_cost_myr
            or estimate.currency_mismatch_count > 0
        )
        return ReconciliationDetail(
            reconciliation=view,
            chain=chain,
            live_estimate=LiveEstimate(
                estimated_event_count=estimate.event_count,
                estimated_source_cost=estimate.estimated_source_cost,
                estimated_cost_myr=estimate.estimated_cost_myr,
                currency_mismatch_count=estimate.currency_mismatch_count,
                unprocessed_event_count=unprocessed,
            ),
            estimate_changed_since_confirmation=changed,
        )


__all__ = [
    "COVERAGE_PERIOD_AGGREGATE",
    "ENTITY_PROVIDER_COST_RECONCILIATION",
    "FIRST_RECONCILIATION_MONTH",
    "MONTHS_SHOWN",
    "SCOPE_AI_MODULE",
    "STATUS_NOT_RECONCILED",
    "STATUS_RECONCILED",
    "VARIANCE_THRESHOLD",
    "FxRateUnavailable",
    "LiveEstimate",
    "MonthView",
    "ReconciliationAmountOutOfRange",
    "ReconciliationDetail",
    "ReconciliationEstimateChanged",
    "ReconciliationExists",
    "ReconciliationMonthInvalid",
    "ReconciliationNotCurrent",
    "ReconciliationNotFound",
    "ReconciliationReasonInvalid",
    "ReconciliationSupersedesMismatch",
    "ReconciliationView",
    "SourceCurrencyMismatch",
    "allocate",
    "confirm",
    "get_detail",
    "months_overview",
    "preview",
    "reconciliation_period",
]

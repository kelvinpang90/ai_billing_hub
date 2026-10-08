"""Asynchronous billing of usage events (design gate #181 v2, AIH-TASK-032).

spec §7、§8、§25、§78、§80–§84、§110；REQ-INGEST-002；ADR-0003、ADR-0010、ADR-0011。
四条路径，各自的事务边界照设计 §2「事务边界」：

- **认领**（`claim_events`）：短事务。`SELECT … FOR UPDATE SKIP LOCKED` 取至多 20 条可处理的
  事件（`RECEIVED`，或到期的 `FAILED_RETRYABLE`，且 `occurred_at ≤ now_c`），改成 `PROCESSING`、
  每条一个新的防护令牌、租约、`attempt_count + 1`，提交。多个 worker 并行认领互不阻塞、不会
  认领同一行；
- **处理**（`process_event`）：一个事件一个事务，①–⑨ 全部在里面（含 `post_transaction` 内的钱包、
  账本、计费状态跃迁、审计与 outbox）。第一步锁住事件行核对「仍是 `PROCESSING` 且令牌是自己的」，
  否则放弃；
- **失败记录**（`record_failure`）：处理事务因意外异常、锁超时或死锁回滚之后另开的短事务，令牌
  仍匹配才写 `FAILED_RETRYABLE` + 退避（2^n 秒，封顶 300），达到上限写 `FAILED_FINAL`；
- **卡住回收**（`recover_stale_processing`）：短事务，`SKIP LOCKED` 取租约已过期的 `PROCESSING`
  —— 仍被某个处理事务持有行锁的事件不会被回收，只回收持有者已崩溃的孤儿。

⚠️ **锁顺序全局唯一**（设计 §2，`docs/database-schema.md`）：事件行（X）→ 供应商行（S）→
`fx_rate_locks`（S）→ `pricing_rule_locks`（S）→ 钱包（X）→ 租户（X）。② 读租户是普通读
（只用于 `CLOSED` 判断）。**三把共享锁都拿到之后**才取 `now_r` 并核对 `occurred_at ≤ now_r`
（025 / T-B / T-C / T-D 共同依赖的契约）；模型未知时 `resolve_model` 拿不到供应商锁，FX 与规则锁
照样先拿，⑤ 照样在它们之后。

⚠️ **只经 `post_transaction` 写账本与改余额**（INV-4）：`AI_USAGE`、金额 = −计费额（`ChargeResult`
的存储值原样取负，不再舍入）、`reference = USAGE_EVENT / event_id`。计费额为 0 不写账本（账本
CHECK 要求 `AI_USAGE < 0`），事件照样 `PROCESSED`、快照齐全。`replayed` 记 WARNING 后照常
`PROCESSED`（指向已有行）；`LedgerConflict` 判 `FAILED_FINAL`（`LEDGER_CONFLICT`），原账本行不动。

⚠️ 本模块的事务把 `innodb_lock_wait_timeout` 设为会话级 10 秒（MySQL）：发布方持排他锁时，
计费等待超过 10 秒就回滚、按可重试记一次，不长时间占着 worker。

⚠️ 日志绑定 `event_id`、`tenant_id`、`project_id`（§94），不打印金额与载荷；异常只记类型与
错误码（设计 §6）。
"""

from __future__ import annotations

import datetime as dt
import enum
import logging
import time
import uuid
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Final

from sqlalchemy import or_, select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker

from app.core.database import session_scope
from app.models.ai_catalog import QUANTITY_FIELD, TOKEN_FIELDS, PayloadShape
from app.models.tenancy import AccountStatus, BillingMode, Tenant
from app.models.usage import ERROR_MESSAGE_LENGTH, UsageEvent, UsageEventStatus
from app.models.wallet import ReferenceType, TransactionType
from app.repositories import ai_catalog as catalog
from app.repositories import fx_rates as rates
from app.repositories import pricing_rules as rules
from app.repositories.wallet import LedgerConflict, post_transaction
from app.services.auth import utc_now
from app.services.pricing_engine import (
    ChargeOverflow,
    ChargeResolution,
    ChargeResult,
    ChargeStatus,
    price_resolved,
    resolve_charge_inputs,
)

logger = logging.getLogger(__name__)

# 取当前时间（不带时区的 UTC）。可注入，测试据此冻结时钟。
type Clock = Callable[[], dt.datetime]
# 单调时钟（秒），只用来量一轮扫描的预算。
type Ticker = Callable[[], float]

# 设计 §2 写定的数值。
CLAIM_BATCH: Final = 20
# 一轮扫描的预算：Beat 每 10 秒一次、expires 9 秒，留出余量。
SWEEP_BUDGET_SECONDS: Final = 8.0
RECOVERY_BATCH: Final = 500
LOCK_WAIT_TIMEOUT_SECONDS: Final = 10
BACKOFF_CAP_SECONDS: Final = 300

# `error_code`。计价错误的细分码来自 T-G（`PricingErrorCode`），溢出来自 `ChargeOverflow.code`。
ACCOUNT_CLOSED: Final = "ACCOUNT_CLOSED"
LEDGER_CONFLICT: Final = "LEDGER_CONFLICT"
AMOUNT_OUT_OF_RANGE: Final = ChargeOverflow.code
STALE_PROCESSING: Final = "STALE_PROCESSING"
STALE_PROCESSING_EXHAUSTED: Final = "STALE_PROCESSING_EXHAUSTED"
# 意外异常的三种：锁等待超时、死锁、其余一切。
LOCK_WAIT_TIMEOUT: Final = "LOCK_WAIT_TIMEOUT"
DEADLOCK: Final = "DEADLOCK"
UNEXPECTED_ERROR: Final = "UNEXPECTED_ERROR"

# MySQL 错误号。
_ER_LOCK_WAIT_TIMEOUT: Final = 1205
_ER_LOCK_DEADLOCK: Final = 1213
# 会话级（默认 50 秒）。⚠️ 计费 worker 用自己的引擎（app/tasks/usage_billing.py），连接回池后
# 带着这个值也只影响计费自己的事务。
_SET_LOCK_WAIT: Final = text(f"SET SESSION innodb_lock_wait_timeout = {LOCK_WAIT_TIMEOUT_SECONDS}")

# 可认领的状态（`FAILED_RETRYABLE` 还要到期）。
CLAIMABLE_STATUSES: Final = (UsageEventStatus.RECEIVED, UsageEventStatus.FAILED_RETRYABLE)


class Outcome(enum.StrEnum):
    """What one processing attempt ended in (for the task's log line and the tests)."""

    PROCESSED = "PROCESSED"
    MODEL_UNKNOWN = "MODEL_UNKNOWN"
    PRICING_ERROR = "PRICING_ERROR"
    FX_RATE_ERROR = "FX_RATE_ERROR"
    FAILED_FINAL = "FAILED_FINAL"
    # ⑤：持锁后 `occurred_at > now_r`，改回 RECEIVED（防御性，不应发生）。
    NOT_DUE = "NOT_DUE"
    # ①：不再是 PROCESSING，或令牌不是自己的（已被回收、另有人处理）。
    ABANDONED = "ABANDONED"
    # 意外异常：主事务回滚，失败另行记录（FAILED_RETRYABLE 或达到上限的 FAILED_FINAL）。
    FAILED = "FAILED"


@dataclass(frozen=True)
class Claim:
    """One claimed event: its internal id and the fencing token this claim wrote."""

    id: int
    token: str


@dataclass(frozen=True)
class SweepResult:
    claimed: int
    outcomes: dict[Outcome, int] = field(default_factory=dict)


def backoff_seconds(attempt_count: int) -> int:
    """`min(2^n, 300)` seconds, `n` the attempts so far (counted at claim time)."""
    # 指数先夹到 16：2^16 早已超过封顶，不让一个异常大的计数算出巨大的整数。
    exponent = min(max(attempt_count, 0), 16)
    return min(2**exponent, BACKOFF_CAP_SECONDS)


def failure_code(error: BaseException) -> str:
    """`LOCK_WAIT_TIMEOUT` / `DEADLOCK` from MySQL's error number, `UNEXPECTED_ERROR` otherwise."""
    if isinstance(error, DBAPIError) and error.orig is not None:
        arguments = getattr(error.orig, "args", ())
        number = arguments[0] if arguments else None
        if number == _ER_LOCK_WAIT_TIMEOUT:
            return LOCK_WAIT_TIMEOUT
        if number == _ER_LOCK_DEADLOCK:
            return DEADLOCK
    return UNEXPECTED_ERROR


def _seconds(moment: dt.datetime) -> dt.datetime:
    # MySQL 的 DATETIME 不存小数秒（插入时还会四舍五入）：服务端时间一律截到整秒。
    return moment.replace(microsecond=0)


def _limit_lock_wait(session: Session) -> None:
    """Session-level `innodb_lock_wait_timeout = 10` (MySQL; SQLite has no such wait)."""
    if session.get_bind().dialect.name == "mysql":
        session.execute(_SET_LOCK_WAIT)


def _lock_event(session: Session, event_id: int) -> UsageEvent | None:
    # ⚠️ `populate_existing=True`：锁拿到了，读到的也必须是库里的当前值。
    statement = (
        select(UsageEvent)
        .where(UsageEvent.id == event_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return session.execute(statement).scalar_one_or_none()


def _holds(event: UsageEvent | None, claim: Claim) -> bool:
    """The fencing check: still PROCESSING under this claim's token."""
    return (
        event is not None
        and event.status is UsageEventStatus.PROCESSING
        and event.claim_token == claim.token
    )


def _clear_claim(event: UsageEvent) -> None:
    # 所有离开 PROCESSING 的跃迁都同时清空三列认领字段（认领 CHECK 要求）。
    event.claim_token = None
    event.claimed_at = None
    event.lease_expires_at = None


def _context(event: UsageEvent) -> dict[str, object]:
    """What a log line about an event carries (§94): ids only, never amounts or the payload."""
    return {
        "event_id": event.event_id,
        "tenant_id": event.tenant_id,
        "project_id": event.project_id,
    }


# --- 认领 -------------------------------------------------------------------------------


def claim_events(
    factory: sessionmaker[Session],
    *,
    lease_seconds: int,
    clock: Clock = utc_now,
    limit: int = CLAIM_BATCH,
) -> list[Claim]:
    """Claim up to `limit` due events in one short transaction (design §2 Ⓐ–Ⓒ).

    提交之后 `PROCESSING` 对外可见（§82「safely claim RECEIVED → PROCESSING」）。`occurred_at`
    晚于当前时间的事件（摄取允许最多 300 秒的未来偏差）不认领、尝试次数不变，到时再说。
    """
    with session_scope(factory) as session:
        _limit_lock_wait(session)
        now = clock()
        due = or_(UsageEvent.next_attempt_at.is_(None), UsageEvent.next_attempt_at <= now)
        statement = (
            select(UsageEvent.id)
            .where(
                UsageEvent.status.in_(CLAIMABLE_STATUSES),
                due,
                UsageEvent.occurred_at <= now,
            )
            .order_by(UsageEvent.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        ids = list(session.execute(statement).scalars().all())
        claimed_at = _seconds(now)
        lease_expires_at = claimed_at + dt.timedelta(seconds=lease_seconds)
        claims = []
        for event_id in ids:
            # 每次认领一个新的 uuid4：被回收后再认领的事件，原处理者在 ① 看到令牌不同而放弃。
            token = str(uuid.uuid4())
            session.execute(
                update(UsageEvent)
                .where(UsageEvent.id == event_id)
                .values(
                    status=UsageEventStatus.PROCESSING,
                    claim_token=token,
                    claimed_at=claimed_at,
                    lease_expires_at=lease_expires_at,
                    attempt_count=UsageEvent.attempt_count + 1,
                )
                .execution_options(synchronize_session=False)
            )
            claims.append(Claim(id=event_id, token=token))
    return claims


# --- 处理 -------------------------------------------------------------------------------


def _quantities(event: UsageEvent) -> dict[str, int | Decimal]:
    """The event's quantities by 025's `quantity_field` (the four token fields, or `quantity`)."""
    if event.payload_shape is PayloadShape.LLM_TOKEN_FIELDS:
        return {name: getattr(event, name) for name in TOKEN_FIELDS}
    assert event.quantity is not None
    return {QUANTITY_FIELD: event.quantity}


def _settle(
    event: UsageEvent,
    status: UsageEventStatus,
    code: str | None,
    resolution: ChargeResolution | None = None,
) -> None:
    """Leave PROCESSING for an error state: no financial effect, the claim cleared.

    已解析到的模型照样写进 `provider_id` / `model_id`（模型未知时为空），给管理员看缺什么。
    """
    model = resolution.model if resolution is not None else None
    event.provider_id = model.provider_id if model is not None else None
    event.model_id = model.model_id if model is not None else None
    event.status = status
    event.error_code = code
    event.error_message = None
    event.next_attempt_at = None
    _clear_claim(event)


def _write_snapshot(
    event: UsageEvent,
    resolution: ChargeResolution,
    charge: ChargeResult,
    billing_mode: BillingMode,
    wallet_transaction_id: int | None,
    processed_at: dt.datetime,
) -> None:
    """⑨: the §14 / §79 snapshot, stored values exactly as T-G returned them; PROCESSED."""
    assert resolution.model is not None and resolution.provider_price is not None
    assert resolution.pricing_rule is not None
    event.provider_id = resolution.model.provider_id
    event.model_id = resolution.model.model_id
    event.provider_price_version_id = resolution.provider_price.version_id
    event.pricing_rule_id = resolution.pricing_rule.rule_id
    event.fx_rate_version_id = (
        resolution.fx_rate.version_id if resolution.fx_rate is not None else None
    )
    event.provider_source_currency = charge.provider_source_currency
    event.provider_source_cost = charge.provider_source_cost
    event.fx_rate_applied = charge.fx_rate_applied
    event.estimated_provider_cost_myr = charge.estimated_provider_cost_myr
    event.billing_mode_snapshot = billing_mode.value
    if billing_mode is BillingMode.INTERNAL_METERED_ONLY:
        event.billable_cost = Decimal(0)
        event.reference_customer_price = charge.billable_cost
    else:
        event.billable_cost = charge.billable_cost
        event.reference_customer_price = None
    event.wallet_transaction_id = wallet_transaction_id
    event.status = UsageEventStatus.PROCESSED
    # ADR-0003：入账期间按 `processed_at`（本事务的服务端时刻，整秒）。
    event.processed_at = processed_at
    event.error_code = None
    event.error_message = None
    event.next_attempt_at = None
    _clear_claim(event)


def _process(session: Session, claim: Claim, clock: Clock) -> Outcome:
    """①–⑨ in the caller's transaction. Writes go through the event row and `post_transaction`."""
    _limit_lock_wait(session)
    # ① 事件行（X），核对防护令牌。
    event = _lock_event(session, claim.id)
    if not _holds(event, claim):
        return Outcome.ABANDONED
    assert event is not None

    # ② 租户：普通读。只用于 CLOSED 判断（终态，读到旧值的窗口由关户前置条件挡住）。
    tenant = session.get(Tenant, event.tenant_id)
    assert tenant is not None
    if tenant.account_status is AccountStatus.CLOSED:
        # AIH-TASK-020 的契约：关户后不入账，进人工复核。DISABLED 照常计费（§112.1）。
        _settle(event, UsageEventStatus.FAILED_FINAL, ACCOUNT_CLOSED)
        return Outcome.FAILED_FINAL

    # ③ 供应商行（S）：`resolve_model` 里加锁。供应商代码未知时没有锁可拿，
    # 下面照样判 MODEL_UNKNOWN。
    catalog.resolve_model(session, event.provider_code_raw, event.model_code_raw, event.occurred_at)
    # ④ fx_rate_locks（S）→ pricing_rule_locks（S）。
    rates.lock_fx_rates_shared(session)
    rules.lock_pricing_rules_shared(session)

    # ⑤ 三把共享锁都拿到之后才取 now_r。认领时已过滤 occurred_at ≤ now_c，这里不应成立。
    now = clock()
    if not catalog.is_due(event.occurred_at, now):
        event.status = UsageEventStatus.RECEIVED
        event.attempt_count = event.attempt_count - 1
        _clear_claim(event)
        return Outcome.NOT_DUE

    # ⑥ 价格版本、汇率、规则（T-G；锁已持有，再取一次是同一事务里的重入）。
    resolution = resolve_charge_inputs(
        session,
        tenant_id=event.tenant_id,
        provider_code=event.provider_code_raw,
        model_code=event.model_code_raw,
        meter_type_id=event.usage_meter_type_id,
        occurred_at=event.occurred_at,
        locking=True,
    )
    if resolution.status is not ChargeStatus.PRICED:
        code = resolution.error_code.value if resolution.error_code is not None else None
        _settle(event, UsageEventStatus(resolution.status.value), code, resolution)
        return Outcome(resolution.status.value)

    # ⑦ 计价（纯函数）。
    try:
        charge = price_resolved(resolution, _quantities(event))
    except ChargeOverflow:
        _settle(event, UsageEventStatus.FAILED_FINAL, AMOUNT_OUT_OF_RANGE, resolution)
        return Outcome.FAILED_FINAL

    # ⑧ 扣费：钱包（X）→ 租户（X）。跃迁、status_version、审计、outbox、低余额事件都在里面。
    processed_at = _seconds(now)
    wallet_transaction_id: int | None = None
    if tenant.billing_mode is BillingMode.PREPAID and charge.billable_cost > 0:
        try:
            posted = post_transaction(
                session,
                tenant_id=event.tenant_id,
                transaction_type=TransactionType.AI_USAGE,
                amount=-charge.billable_cost,
                reference_type=ReferenceType.USAGE_EVENT,
                reference_id=event.event_id,
                now=processed_at,
            )
        except LedgerConflict:
            # 同一 event_id 已有金额不同的账本行：只可能是缺陷。原账本行不动，P1 告警。
            logger.error(
                "A usage event's ledger reference was already posted with another amount",
                extra={**_context(event), "error_code": LEDGER_CONFLICT},
            )
            _settle(event, UsageEventStatus.FAILED_FINAL, LEDGER_CONFLICT, resolution)
            return Outcome.FAILED_FINAL
        if posted.replayed:
            # 已有同额账本行：前一次提交了账本却没提交事件 —— 二者同事务，所以同样是缺陷信号。
            logger.warning(
                "A usage event's ledger row already existed; linking it instead of posting",
                extra=_context(event),
            )
        wallet_transaction_id = posted.transaction.id

    # ⑨ 快照、PROCESSED、processed_at、清认领。提交由调用方的 session_scope 做。
    _write_snapshot(
        event, resolution, charge, tenant.billing_mode, wallet_transaction_id, processed_at
    )
    return Outcome.PROCESSED


def record_failure(
    factory: sessionmaker[Session],
    claim: Claim,
    *,
    code: str,
    error_type: str,
    max_attempts: int,
    clock: Clock = utc_now,
) -> UsageEventStatus | None:
    """After the processing transaction rolled back: one short transaction (design §2).

    仍是 PROCESSING 且令牌是自己的才写：`FAILED_RETRYABLE`，`next_attempt_at = now + min(2^n, 300)`
    （`n` 即认领时已 +1 的 `attempt_count`）；达到 `max_attempts` 写 `FAILED_FINAL`（保留这次的
    错误码）。返回写入的状态；令牌不匹配时什么都不写，返回 `None`。
    """
    with session_scope(factory) as session:
        _limit_lock_wait(session)
        event = _lock_event(session, claim.id)
        if not _holds(event, claim):
            return None
        assert event is not None
        now = _seconds(clock())
        event.error_code = code
        event.error_message = error_type[:ERROR_MESSAGE_LENGTH]
        if event.attempt_count >= max_attempts:
            event.status = UsageEventStatus.FAILED_FINAL
            event.next_attempt_at = None
        else:
            event.status = UsageEventStatus.FAILED_RETRYABLE
            wait = dt.timedelta(seconds=backoff_seconds(event.attempt_count))
            event.next_attempt_at = now + wait
        _clear_claim(event)
        return event.status


def process_event(
    factory: sessionmaker[Session],
    claim: Claim,
    *,
    max_attempts: int,
    clock: Clock = utc_now,
) -> Outcome:
    """One event, one transaction (①–⑨). An unexpected error rolls it back and is recorded apart.

    记失败的短事务本身也失败时（例如数据库此刻不可用），事件停在 PROCESSING，租约过期后由
    卡住回收处理（设计 §5）。
    """
    try:
        with session_scope(factory) as session:
            return _process(session, claim, clock)
    except Exception as error:
        code = failure_code(error)
        error_type = type(error).__name__
        logger.warning(
            "Billing a usage event failed; the attempt is recorded",
            extra={"usage_event": claim.id, "error_type": error_type, "error_code": code},
        )
        try:
            record_failure(
                factory,
                claim,
                code=code,
                error_type=error_type,
                max_attempts=max_attempts,
                clock=clock,
            )
        except Exception as again:
            logger.error(
                "Recording a billing failure failed; stale recovery takes the event over",
                extra={"usage_event": claim.id, "error_type": type(again).__name__},
            )
        return Outcome.FAILED


def bill_pending_events(
    factory: sessionmaker[Session],
    *,
    lease_seconds: int,
    max_attempts: int,
    clock: Clock = utc_now,
    ticker: Ticker = time.monotonic,
    budget_seconds: float = SWEEP_BUDGET_SECONDS,
) -> SweepResult:
    """One Beat run: claim and process until nothing is due or the 8-second budget is spent.

    预算只在认领前检查：已认领的一批全部处理完，不留给租约。
    """
    started = ticker()
    claimed = 0
    outcomes: Counter[Outcome] = Counter()
    while ticker() - started < budget_seconds:
        claims = claim_events(factory, lease_seconds=lease_seconds, clock=clock)
        if not claims:
            break
        claimed += len(claims)
        for claim in claims:
            outcomes[process_event(factory, claim, max_attempts=max_attempts, clock=clock)] += 1
    return SweepResult(claimed=claimed, outcomes=dict(outcomes))


# --- 卡住回收（§110） ------------------------------------------------------------------


def recover_stale_processing(
    factory: sessionmaker[Session],
    *,
    max_attempts: int,
    clock: Clock = utc_now,
    limit: int = RECOVERY_BATCH,
) -> int:
    """Release events whose lease expired while still PROCESSING (design §2). Returns how many.

    ⚠️ `FOR UPDATE SKIP LOCKED`：仍被某个处理事务持有行锁的事件（即使已过租约，例如在等钱包锁）
    不会被回收，所以回收不与正在进行的处理竞争；只有持有者已崩溃、连接已断、行锁已释放的孤儿
    才会被回收。未达上限：`FAILED_RETRYABLE`（`STALE_PROCESSING`，立即可再认领）；达到上限：
    `FAILED_FINAL`（`STALE_PROCESSING_EXHAUSTED`）。
    """
    with session_scope(factory) as session:
        _limit_lock_wait(session)
        now = clock()
        statement = (
            select(UsageEvent)
            .where(
                UsageEvent.status == UsageEventStatus.PROCESSING,
                UsageEvent.lease_expires_at < now,
            )
            .order_by(UsageEvent.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
            .execution_options(populate_existing=True)
        )
        stale = list(session.execute(statement).scalars().all())
        stamp = _seconds(now)
        for event in stale:
            claimed_at = event.claimed_at
            if event.attempt_count >= max_attempts:
                event.status = UsageEventStatus.FAILED_FINAL
                event.error_code = STALE_PROCESSING_EXHAUSTED
                event.next_attempt_at = None
            else:
                event.status = UsageEventStatus.FAILED_RETRYABLE
                event.error_code = STALE_PROCESSING
                event.next_attempt_at = stamp
            event.error_message = None
            _clear_claim(event)
            logger.warning(
                "Recovered a usage event stuck in PROCESSING past its lease",
                extra={
                    **_context(event),
                    "claimed_at": claimed_at.isoformat() if claimed_at is not None else None,
                    "attempt_count": event.attempt_count,
                    "error_code": event.error_code,
                },
            )
        return len(stale)


__all__ = [
    "ACCOUNT_CLOSED",
    "AMOUNT_OUT_OF_RANGE",
    "BACKOFF_CAP_SECONDS",
    "CLAIMABLE_STATUSES",
    "CLAIM_BATCH",
    "DEADLOCK",
    "LEDGER_CONFLICT",
    "LOCK_WAIT_TIMEOUT",
    "LOCK_WAIT_TIMEOUT_SECONDS",
    "RECOVERY_BATCH",
    "STALE_PROCESSING",
    "STALE_PROCESSING_EXHAUSTED",
    "SWEEP_BUDGET_SECONDS",
    "UNEXPECTED_ERROR",
    "Claim",
    "Outcome",
    "SweepResult",
    "backoff_seconds",
    "bill_pending_events",
    "claim_events",
    "failure_code",
    "process_event",
    "record_failure",
    "recover_stale_processing",
]

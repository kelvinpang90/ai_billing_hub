"""Closing a tenant account: `DISABLED → CLOSED` (spec §112.1; design gate #707 v4 §2「契约三」).

⚠️ **两个事务**（设计 §2「事务边界」）：事务 0 只读、只按 `public_id` 取 `tenants.id`；事务 1
锁钱包（X）→ 锁租户（X）→ 判定 → 写。事务 0 单独开，是为了不让事务 1 的一致性读快照在拿锁
之前建立（InnoDB 的快照在事务里第一次普通读时建立，锁定读不建立）：`closure_blockers` 是事务 1
的第一次普通读，快照在两把锁都拿到之后才有。

⚠️ **锁顺序钱包 → 租户**，与 `post_transaction`、支付确认、内部计量开通一致（设计 §4「并发」）。
入账先拿到钱包锁就先提交，关户随后锁内读到新余额；关户先提交，入账锁到租户时读到 `CLOSED`。

⚠️ **判定顺序**：已是 `CLOSED` → 什么都不写、返回；不是 `DISABLED` → 409；
余额不恰好为 0 → 409；四类未结按表序第一个 → 409。写入复用
`account_status._record_transition`（状态、版本、`status_changed_at`、outbox、投递重排、
`updated_at`、审计），与 `change_account_status` 写的同样。

⚠️ **只计数**：不读用量、支付、网关事件与 AI outbox 的金额与载荷；网关事件只读归属所需的
`payment_id` 与 `metadata_json` 里的三个引用。钱包只读余额，不写（INV-4）。

⚠️ **日志**：除网关事件扫描到上限的那一条 WARNING（固定文本 + 上限）外不写日志。原因是管理员的
自由文本，只进审计的 `reason`，不进日志、outbox payload 与异常消息（设计 §6）。
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Final

from sqlalchemy import Select, and_, column, func, or_, select, table
from sqlalchemy.orm import Session, sessionmaker

from app.core.database import session_scope
from app.core.errors import AppError
from app.models.auth import User
from app.models.payments import (
    Payment,
    PaymentEventProcessingStatus,
    PaymentGatewayEvent,
    PaymentStatus,
)
from app.models.tenancy import AccountStatus, Tenant
from app.models.usage import UsageEvent, UsageEventStatus
from app.models.wallet import Wallet
from app.repositories.tenancy import get_tenant_by_public_id
from app.repositories.wallet import WALLET_MISSING, get_wallet_for_tenant
from app.schemas.customers import CustomerDetail
from app.services import account_status, customers
from app.services.account_status import AccountStatusTransitionInvalid
from app.services.auth import RequestContext, utc_now
from app.services.customers import CustomerNotFound

logger = logging.getLogger(__name__)

# 创建超过这么多天的 `EXPIRED` 支付视为终结、不挡关户（Kelvin 决定 2）。
# 边界：`created_at` 恰好等于 `now - 7 天` 不挡，晚一秒挡。
EXPIRED_PAYMENT_SETTLE_DAYS: Final = 7

# 全库未结网关事件至多扫这么多行（设计 §2「网关事件的归属」第 1 步）。
# ⚠️ `closure_blockers` 在调用时读这个模块属性（不做成参数缺省值、不复制进局部常量）：
# 测试以 monkeypatch 注入。
PAYMENT_EVENT_SCAN_LIMIT: Final = 10000

# 类别 1：除两种终结状态以外的全部取值，由枚举推导（新增的状态默认算未结）；写成正向 IN，
# 以便走 `(status, occurred_at)` 索引。
UNRESOLVED_USAGE_STATUSES: Final = tuple(
    status
    for status in UsageEventStatus
    if status not in (UsageEventStatus.PROCESSED, UsageEventStatus.FAILED_FINAL)
)
# 类别 2：AI 模块 outbox 里尚未投递、也未处置的行。
UNRESOLVED_AI_USAGE_STATUSES: Final = ("PENDING", "SENDING", "HELD")
# 类别 3：不论创建时刻都算未结的支付状态（另加 7 天内的 `EXPIRED`）。
UNRESOLVED_PAYMENT_STATUSES: Final = (PaymentStatus.PENDING, PaymentStatus.REVIEW_REQUIRED)
# 类别 4：尚未处理完的网关事件（`RETRY` 不放弃）。
UNRESOLVED_EVENT_STATUSES: Final = (
    PaymentEventProcessingStatus.RECEIVED,
    PaymentEventProcessingStatus.PROCESSING,
    PaymentEventProcessingStatus.RETRY,
)

# ⚠️ AI 模块的表只按表名与两列引用，不 import `app.ai_api`：
# AI 模块边界（设计闸门 #281 v23 §2，tests/backend/test_ai_module_boundary.py）
# 只许列出的几个模块反向 import 它。
_AI_USAGE_OUTBOX = table("ai_usage_outbox", column("tenant_id"), column("status"))

# 错误码（设计 §2「接口」的表）。
USAGE_PENDING: Final = "ACCOUNT_CLOSURE_USAGE_PENDING"
AI_USAGE_PENDING: Final = "ACCOUNT_CLOSURE_AI_USAGE_PENDING"
PAYMENTS_PENDING: Final = "ACCOUNT_CLOSURE_PAYMENTS_PENDING"
PAYMENT_EVENTS_PENDING: Final = "ACCOUNT_CLOSURE_PAYMENT_EVENTS_PENDING"
BALANCE_NOT_ZERO: Final = "ACCOUNT_CLOSURE_BALANCE_NOT_ZERO"
TRANSITION_INVALID: Final = "ACCOUNT_STATUS_TRANSITION_INVALID"


class AccountClosureBalanceNotZero(AppError):
    def __init__(self) -> None:
        super().__init__(
            "The wallet balance must be exactly zero before the account can be closed.",
            code=BALANCE_NOT_ZERO,
            http_status=409,
        )


class AccountClosureUsagePending(AppError):
    def __init__(self) -> None:
        super().__init__(
            "The account has usage events that are not yet final.",
            code=USAGE_PENDING,
            http_status=409,
        )


class AccountClosureAiUsagePending(AppError):
    def __init__(self) -> None:
        super().__init__(
            "The account has AI usage that has not been delivered yet.",
            code=AI_USAGE_PENDING,
            http_status=409,
        )


class AccountClosurePaymentsPending(AppError):
    def __init__(self) -> None:
        super().__init__(
            "The account has payments that are not yet final.",
            code=PAYMENTS_PENDING,
            http_status=409,
        )


class AccountClosurePaymentEventsPending(AppError):
    def __init__(self) -> None:
        super().__init__(
            "The account has payment gateway events that are not yet processed.",
            code=PAYMENT_EVENTS_PENDING,
            http_status=409,
        )


# 每个未结类别的错误码对应的异常（按表序）。
_BLOCKER_ERRORS: Final[dict[str, Callable[[], AppError]]] = {
    USAGE_PENDING: AccountClosureUsagePending,
    AI_USAGE_PENDING: AccountClosureAiUsagePending,
    PAYMENTS_PENDING: AccountClosurePaymentsPending,
    PAYMENT_EVENTS_PENDING: AccountClosurePaymentEventsPending,
}


@dataclass(frozen=True)
class ClosureBlockers:
    """The four unresolved counts of one tenant (design §2「契约三」)."""

    usage_events: int
    ai_usage: int
    payments: int
    payment_events: int
    payment_events_truncated: bool

    def codes(self) -> tuple[str, ...]:
        """The error codes of the non-zero categories, in the table's order.

        网关事件扫描到上限时该类别按未结处理，计数为 0 也报。
        """
        found: list[str] = []
        if self.usage_events:
            found.append(USAGE_PENDING)
        if self.ai_usage:
            found.append(AI_USAGE_PENDING)
        if self.payments:
            found.append(PAYMENTS_PENDING)
        if self.payment_events or self.payment_events_truncated:
            found.append(PAYMENT_EVENTS_PENDING)
        return tuple(found)


@dataclass(frozen=True)
class ClosureCheck:
    """What `closure-check` shows (design §2): the status, the raw balance, the counts, the codes.

    `codes`：此刻关户会被拒绝的全部原因码，顺序同 `close_account` 的判定顺序；
    `CLOSED` 时为空。
    """

    account_status: AccountStatus
    balance: Decimal
    blockers: ClosureBlockers
    codes: tuple[str, ...]


def _now() -> dt.datetime:
    """`utc_now()` truncated to whole seconds, as in app/services/customers.py."""
    return utc_now().replace(microsecond=0)


def _count(session: Session, statement: Select[tuple[int]]) -> int:
    return int(session.execute(statement).scalar_one())


def closure_blockers(session: Session, *, tenant_id: int, now: dt.datetime) -> ClosureBlockers:
    """Count the tenant's unresolved usage, AI usage, payments and gateway events.

    全部普通读（不加锁）、只计数、都带本租户条件。不提交、不回滚，事务归调用方。
    """
    usage_events = _count(
        session,
        select(func.count())
        .select_from(UsageEvent)
        .where(
            UsageEvent.tenant_id == tenant_id,
            UsageEvent.status.in_(UNRESOLVED_USAGE_STATUSES),
        ),
    )
    ai_usage = _count(
        session,
        select(func.count())
        .select_from(_AI_USAGE_OUTBOX)
        .where(
            _AI_USAGE_OUTBOX.c.tenant_id == tenant_id,
            _AI_USAGE_OUTBOX.c.status.in_(UNRESOLVED_AI_USAGE_STATUSES),
        ),
    )
    settled_before = now - dt.timedelta(days=EXPIRED_PAYMENT_SETTLE_DAYS)
    payments = _count(
        session,
        select(func.count())
        .select_from(Payment)
        .where(
            Payment.tenant_id == tenant_id,
            or_(
                Payment.status.in_(UNRESOLVED_PAYMENT_STATUSES),
                and_(
                    Payment.status == PaymentStatus.EXPIRED,
                    Payment.created_at > settled_before,
                ),
            ),
        ),
    )
    payment_events, truncated = _payment_events(session, tenant_id)
    return ClosureBlockers(
        usage_events=usage_events,
        ai_usage=ai_usage,
        payments=payments,
        payment_events=payment_events,
        payment_events_truncated=truncated,
    )


def _payment_events(session: Session, tenant_id: int) -> tuple[int, bool]:
    """The tenant's unresolved gateway events, and whether the scan was full.

    设计 §2「网关事件的归属」。回调落库时 `payment_id` 为空，归属靠 `metadata_json`
    的引用；方言无关地在 Python 里比。
    """
    limit = PAYMENT_EVENT_SCAN_LIMIT
    events = session.execute(
        select(
            PaymentGatewayEvent.id,
            PaymentGatewayEvent.payment_id,
            PaymentGatewayEvent.metadata_json,
        )
        .where(PaymentGatewayEvent.processing_status.in_(UNRESOLVED_EVENT_STATUSES))
        .order_by(PaymentGatewayEvent.id)
        .limit(limit)
    ).all()
    # 恰好取满上限：可能还有更多，该类别按未结处理。
    truncated = len(events) == limit
    if truncated:
        # 不带租户或事件的任何数据：只说扫到了上限。
        logger.warning(
            "Account closure stopped scanning unresolved payment gateway events at %d rows",
            limit,
        )
    if not events:
        return 0, truncated
    ids: set[int] = set()
    public_ids: set[str] = set()
    session_ids: set[str] = set()
    intent_ids: set[str] = set()
    for payment_id, public_id, session_id, intent_id in session.execute(
        select(
            Payment.id,
            Payment.public_id,
            Payment.gateway_payment_id,
            Payment.gateway_payment_intent_id,
        ).where(Payment.tenant_id == tenant_id)
    ):
        ids.add(int(payment_id))
        public_ids.add(public_id)
        if session_id is not None:
            session_ids.add(session_id)
        if intent_id is not None:
            intent_ids.add(intent_id)
    if not ids:
        return 0, truncated
    owned = sum(
        1
        for _event_id, payment_id, metadata in events
        if _belongs(payment_id, metadata, ids, public_ids, session_ids, intent_ids)
    )
    return owned, truncated


def _belongs(
    payment_id: int | None,
    metadata: object,
    ids: set[int],
    public_ids: set[str],
    session_ids: set[str],
    intent_ids: set[str],
) -> bool:
    """One of the four references names one of the tenant's payments (strings only)."""
    if payment_id is not None and int(payment_id) in ids:
        return True
    if not isinstance(metadata, dict):
        return False
    for key, wanted in (
        ("client_reference_id", public_ids),
        ("object_id", session_ids),
        ("payment_intent", intent_ids),
    ):
        value = metadata.get(key)
        if isinstance(value, str) and value in wanted:
            return True
    return False


def get_closure_check(
    factory: sessionmaker[Session], *, customer_id: str, now: dt.datetime | None = None
) -> ClosureCheck:
    """What closing the account would be refused for right now. Read only: no lock, no audit.

    `codes` 为空不保证随后的关户成功（之间可能有新事件）：`close_account` 在锁内重查。
    """
    moment = now or _now()
    with factory() as session:
        tenant = get_tenant_by_public_id(session, customer_id)
        if tenant is None:
            raise CustomerNotFound
        wallet = get_wallet_for_tenant(session, tenant.id)
        if wallet is None:
            # 每个客户都有钱包（建客户同事务）：缺了是数据缺陷，按意外异常走 500。
            raise RuntimeError(WALLET_MISSING)
        blockers = closure_blockers(session, tenant_id=tenant.id, now=moment)
        status = tenant.account_status
        balance = wallet.balance
    codes: list[str] = []
    if status is not AccountStatus.CLOSED:
        if status is not AccountStatus.DISABLED:
            codes.append(TRANSITION_INVALID)
        if balance != Decimal(0):
            codes.append(BALANCE_NOT_ZERO)
        codes.extend(blockers.codes())
    return ClosureCheck(
        account_status=status, balance=balance, blockers=blockers, codes=tuple(codes)
    )


def close_account(
    factory: sessionmaker[Session],
    *,
    actor: User,
    customer_id: str,
    reason: str,
    context: RequestContext,
    now: dt.datetime | None = None,
) -> CustomerDetail:
    """Close a `DISABLED` customer whose balance is exactly 0 and who has nothing unresolved.

    已是 `CLOSED`：什么都不写（不写审计、不加版本、不写 outbox），返回当前详情。
    """
    moment = now or _now()
    # 事务 0（只读、短）：只取内部 id。
    with session_scope(factory) as session:
        tenant_id = session.execute(
            select(Tenant.id).where(Tenant.public_id == customer_id)
        ).scalar_one_or_none()
    if tenant_id is None:
        raise CustomerNotFound
    # 事务 1：锁钱包 → 锁租户 → 判定 → 写，一次提交（INV-13）。
    with session_scope(factory) as session:
        wallet = session.execute(
            select(Wallet)
            .where(Wallet.tenant_id == tenant_id)
            .with_for_update()
            # 锁读之前本事务可能已读过这一行：不加它，identity map 会把旧值原样还回来。
            .execution_options(populate_existing=True)
        ).scalar_one_or_none()
        if wallet is None:
            raise RuntimeError(WALLET_MISSING)
        tenant = session.execute(
            select(Tenant)
            .where(Tenant.id == tenant_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).scalar_one()
        _close_locked(
            session,
            tenant,
            wallet,
            reason=reason,
            actor=actor,
            context=context,
            now=moment,
        )
    # 事务之外、提交之后再读详情（与 `change_account_status` 相同写法）。
    return customers.get_customer(factory, customer_id)


def _close_locked(
    session: Session,
    tenant: Tenant,
    wallet: Wallet,
    *,
    reason: str,
    actor: User,
    context: RequestContext,
    now: dt.datetime,
) -> None:
    """Design §2「契约三」steps 3–7, with the wallet and the tenant locked by the caller."""
    if tenant.account_status is AccountStatus.CLOSED:
        return
    if tenant.account_status is not AccountStatus.DISABLED:
        raise AccountStatusTransitionInvalid
    # 锁内刷新的值；Decimal 精确比较，不经 float（INV-10）。
    if wallet.balance != Decimal(0):
        raise AccountClosureBalanceNotZero
    # 事务 1 的第一次普通读：快照在两把锁都拿到之后建立。
    blockers = closure_blockers(session, tenant_id=tenant.id, now=now)
    codes = blockers.codes()
    if codes:
        raise _BLOCKER_ERRORS[codes[0]]()
    account_status._record_transition(
        session,
        tenant,
        AccountStatus.DISABLED,
        AccountStatus.CLOSED,
        reason=reason,
        actor=actor,
        context=context,
        now=now,
    )


__all__ = [
    "EXPIRED_PAYMENT_SETTLE_DAYS",
    "PAYMENT_EVENT_SCAN_LIMIT",
    "AccountClosureAiUsagePending",
    "AccountClosureBalanceNotZero",
    "AccountClosurePaymentEventsPending",
    "AccountClosurePaymentsPending",
    "AccountClosureUsagePending",
    "ClosureBlockers",
    "ClosureCheck",
    "close_account",
    "closure_blockers",
    "get_closure_check",
]

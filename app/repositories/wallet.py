"""Wallet and ledger data access (spec §7, §8, §77, §78, §81; design gate #88 v6).

⚠️ **只 flush，不 commit**（与 app/repositories/tenancy.py 同一约定）。一次记账的
账本行、调账审计、计费状态跃迁与出站事件由调用方在同一个事务里一次提交
（Invariant 13）。

⚠️ **这里没有任何 UPDATE wallets。**账本行由 `post_transaction` 插入，钱包的
余额与版本由迁移 0006 的 AFTER INSERT 触发器在同一条语句里推进；直接改钱包会被
BEFORE UPDATE 触发器拒绝。也没有任何更新或删除账本的函数（Invariant 5）。

⚠️ **加锁顺序固定为钱包 → 租户**，同一租户的记账与状态跃迁因此串行，不会互相
死锁。这一层不重试：锁等待超时与死锁以 `OperationalError` 抛给调用方，由它回滚
后决定是否重试 —— 幂等键保证重试只有一次效果。

⚠️ 这里不写日志。异常消息只含问题码，不含 `description` 或 `metadata_json`。
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.auth import AuditAction, AuditLog, DomainOutbox, OutboxStatus
from app.models.base import MONEY_PRECISION, MONEY_SCALE
from app.models.tenancy import BillingStatus, Tenant
from app.models.wallet import (
    ADJUSTMENT_REFERENCE_TYPES,
    CREDIT_TYPES,
    DEBIT_TYPES,
    REFERENCE_TYPE_FOR,
    WALLET_CURRENCY,
    ReferenceType,
    TransactionType,
    Wallet,
    WalletTransaction,
)

# 出站事件（设计 §2）。⚠️ 这两个字符串是将来投递任务选处理器的键，改名等于让
# 已经躺在 domain_outbox 里的行认不出自己。
EVENT_BILLING_STATUS_CHANGED: Final = "tenant.billing_status_changed"
EVENT_LOW_BALANCE: Final = "tenant.low_balance"
AGGREGATE_TENANT: Final = "tenant"
ENTITY_WALLET_TRANSACTION: Final = "wallet_transaction"
# 计费状态跃迁的操作者是系统本身，不是任何用户。
SYSTEM_ACTOR_ROLE: Final = "SYSTEM"
REASON_BALANCE_POSITIVE: Final = "BALANCE_POSITIVE"
REASON_BALANCE_NON_POSITIVE: Final = "BALANCE_NON_POSITIVE"

# `list_transactions_for_tenant` 一页的上限。
MAX_PAGE_SIZE: Final = 200

# INV-7（成本、毛利）与 INV-9（对话内容）：metadata_json 里不许出现的键，
# 任意嵌套层级、不分大小写。
FORBIDDEN_METADATA_KEYS: Final = frozenset(
    {
        "cost",
        "provider_cost",
        "margin",
        "estimated_provider_cost_myr",
        "prompt",
        "response",
        "messages",
        "content",
    }
)

# `verify_wallet` 的问题码。空列表 = 一致。
WALLET_MISSING: Final = "WALLET_MISSING"
LEDGER_MISSING: Final = "LEDGER_MISSING"
BALANCE_NOT_LEDGER_SUM: Final = "BALANCE_NOT_LEDGER_SUM"
BALANCE_NOT_LAST_BALANCE_AFTER: Final = "BALANCE_NOT_LAST_BALANCE_AFTER"
VERSION_NOT_LAST_SEQUENCE: Final = "VERSION_NOT_LAST_SEQUENCE"
SEQUENCE_GAP: Final = "SEQUENCE_GAP"
CHAIN_BROKEN: Final = "CHAIN_BROKEN"
ROW_ARITHMETIC_BROKEN: Final = "ROW_ARITHMETIC_BROKEN"
BILLING_STATUS_MISMATCH: Final = "BILLING_STATUS_MISMATCH"

_QUANTUM = Decimal(1).scaleb(-MONEY_SCALE)
# DECIMAL(20,8) 装得下的绝对值上限（不含）：整数部分 12 位。
_MONEY_LIMIT = Decimal(10) ** (MONEY_PRECISION - MONEY_SCALE)
# 与列宽一致：wallet_transactions.reference_id / description、audit_logs.actor_role。
_REFERENCE_ID_LENGTH = 64
_DESCRIPTION_LENGTH = 255
_ACTOR_ROLE_LENGTH = 32


class LedgerError(Exception):
    """Base of every ledger error. The message is a problem code, never business data."""


class InvalidAmount(LedgerError):
    """Not a finite, non-zero Decimal that fits DECIMAL(20,8) without rounding."""


class InvalidTransaction(LedgerError):
    """Type, sign, source, reason, actor, reference or metadata is not acceptable."""


class WalletNotFound(LedgerError):
    """The tenant has no wallet."""


class LedgerConflict(LedgerError):
    """The same financial source was already posted with a different effect."""


@dataclass(frozen=True)
class PostResult:
    transaction: WalletTransaction
    # True = 同一来源已记过且完全一致：返回既有行，什么都没写。
    replayed: bool

    @property
    def balance(self) -> Decimal:
        """以账本行为准：钱包对象在记账后已过期（触发器在库里改写了它）。"""
        return self.transaction.balance_after


@dataclass(frozen=True)
class LedgerLine:
    """What `ledger_problems` needs from one ledger row."""

    wallet_sequence: int
    amount: Decimal
    balance_before: Decimal
    balance_after: Decimal


# --- 纯函数：不碰数据库，test_wallet_rules.py 直接测 ---------------------------


def fits_money(value: Decimal) -> bool:
    return abs(value) < _MONEY_LIMIT


def validate_amount(amount: object) -> Decimal:
    """Return `amount` unchanged if it may be posted; raise `InvalidAmount` otherwise.

    ⚠️ **不舍入。**超过 8 位小数直接拒绝：spec §80 那唯一一次 `ROUND_HALF_UP`
    发生在定价层，账本层再舍入一次就成了第二次。判据是「存进 DECIMAL(20,8) 会不会
    改变这个值」，所以 `Decimal("1.500000000")` 可以，`Decimal("0.000000001")` 不行。
    """
    # float 存不下十进制小数；int 与 str 让「调用方以为自己传的是什么」变得含糊。
    if not isinstance(amount, Decimal):
        raise InvalidAmount("AMOUNT_NOT_DECIMAL")
    if not amount.is_finite():
        raise InvalidAmount("AMOUNT_NOT_FINITE")
    if amount.is_zero():
        raise InvalidAmount("AMOUNT_ZERO")
    # 先判范围再 quantize：超大的值 quantize 会超出 Decimal 上下文精度。
    if not fits_money(amount):
        raise InvalidAmount("AMOUNT_OUT_OF_RANGE")
    if amount.quantize(_QUANTUM) != amount:
        raise InvalidAmount("AMOUNT_TOO_MANY_DECIMAL_PLACES")
    return amount


def coerce_transaction_type(value: object) -> TransactionType:
    try:
        return TransactionType(value)
    except ValueError:
        raise InvalidTransaction("UNKNOWN_TRANSACTION_TYPE") from None


def coerce_reference_type(value: object) -> ReferenceType:
    try:
        return ReferenceType(value)
    except ValueError:
        raise InvalidTransaction("UNKNOWN_REFERENCE_TYPE") from None


def check_transaction_shape(
    transaction_type: TransactionType,
    amount: Decimal,
    reference_type: ReferenceType,
) -> None:
    """Type ↔ sign ↔ source, the same rules as the database CHECKs."""
    if transaction_type in CREDIT_TYPES and amount <= 0:
        raise InvalidTransaction("CREDIT_MUST_BE_POSITIVE")
    if transaction_type in DEBIT_TYPES and amount >= 0:
        raise InvalidTransaction("DEBIT_MUST_BE_NEGATIVE")
    if REFERENCE_TYPE_FOR[transaction_type] is not reference_type:
        raise InvalidTransaction("REFERENCE_TYPE_MISMATCH")


def check_reference_id(reference_id: object) -> None:
    if not isinstance(reference_id, str) or not reference_id.strip():
        raise InvalidTransaction("REFERENCE_ID_REQUIRED")
    if len(reference_id) > _REFERENCE_ID_LENGTH:
        raise InvalidTransaction("REFERENCE_ID_TOO_LONG")


def check_reason_and_actor(
    reference_type: ReferenceType,
    description: str | None,
    created_by: int | None,
    actor_role: str | None = None,
) -> None:
    """spec §8、§60：调账与系统更正必须有原因，调账还必须有操作者。"""
    if description is not None and len(description) > _DESCRIPTION_LENGTH:
        raise InvalidTransaction("DESCRIPTION_TOO_LONG")
    if actor_role is not None and len(actor_role) > _ACTOR_ROLE_LENGTH:
        raise InvalidTransaction("ACTOR_ROLE_TOO_LONG")
    if reference_type in ADJUSTMENT_REFERENCE_TYPES and not (description or "").strip():
        raise InvalidTransaction("REASON_REQUIRED")
    if reference_type is ReferenceType.ADMIN_ADJUSTMENT and created_by is None:
        raise InvalidTransaction("ACTOR_REQUIRED")


def _has_forbidden_key(value: object) -> bool:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if str(key).lower() in FORBIDDEN_METADATA_KEYS or _has_forbidden_key(item):
                return True
        return False
    if isinstance(value, list | tuple):
        return any(_has_forbidden_key(item) for item in value)
    return False


def check_metadata(metadata: object) -> None:
    """INV-7 / INV-9: no cost, margin or conversation content, at any depth."""
    if metadata is None:
        return
    if not isinstance(metadata, Mapping):
        raise InvalidTransaction("METADATA_NOT_AN_OBJECT")
    if _has_forbidden_key(metadata):
        raise InvalidTransaction("METADATA_FORBIDDEN_KEY")
    try:
        # 在写入之前就发现存不进 JSON 列的值（Decimal、NaN、任意对象）。
        json.dumps(metadata, allow_nan=False)
    except (TypeError, ValueError):
        raise InvalidTransaction("METADATA_NOT_JSON") from None


def billing_status_for(balance: Decimal) -> BillingStatus:
    """spec §7 第 8–10 条：> 0 为 ACTIVE，≤ 0 为 SUSPENDED（正好为 0 也暂停）。"""
    return BillingStatus.ACTIVE if balance > 0 else BillingStatus.SUSPENDED


def billing_transition(current: BillingStatus, balance: Decimal) -> BillingStatus | None:
    """The status to move to, or None when the balance does not change it."""
    wanted = billing_status_for(balance)
    return None if wanted == current else wanted


def crosses_low_balance(threshold: Decimal | None, before: Decimal, after: Decimal) -> bool:
    """spec §7 第 11 条：只在从阈值之上跌到阈值及以下的那一笔发。"""
    return threshold is not None and before > threshold >= after


def ledger_problems(
    *,
    balance: Decimal,
    version: int,
    billing_status: BillingStatus,
    lines: Sequence[LedgerLine],
) -> list[str]:
    """Compare a wallet with its ledger. An empty list means they agree."""
    ordered = sorted(lines, key=lambda line: line.wallet_sequence)
    problems = []
    if version > 0 and not ordered:
        # `TRUNCATE` 不经触发器（设计 §10 的残余风险），会表现成这样。
        problems.append(LEDGER_MISSING)
    if balance != sum((line.amount for line in ordered), Decimal(0)):
        problems.append(BALANCE_NOT_LEDGER_SUM)
    if ordered and balance != ordered[-1].balance_after:
        problems.append(BALANCE_NOT_LAST_BALANCE_AFTER)
    if version != (ordered[-1].wallet_sequence if ordered else 0):
        problems.append(VERSION_NOT_LAST_SEQUENCE)
    if [line.wallet_sequence for line in ordered] != list(range(1, len(ordered) + 1)):
        problems.append(SEQUENCE_GAP)
    # 钱包从空开始，所以第一行的前余额必须是 0。
    previous = Decimal(0)
    chained = arithmetic = True
    for line in ordered:
        chained = chained and line.balance_before == previous
        arithmetic = arithmetic and line.balance_after == line.balance_before + line.amount
        previous = line.balance_after
    if not chained:
        problems.append(CHAIN_BROKEN)
    if not arithmetic:
        problems.append(ROW_ARITHMETIC_BROKEN)
    if billing_status != billing_status_for(balance):
        problems.append(BILLING_STATUS_MISMATCH)
    return problems


# --- 数据库入口 -----------------------------------------------------------------


def create_wallet(session: Session, *, tenant_id: int, now: dt.datetime) -> Wallet:
    """Insert and flush an empty MYR wallet. A second one for the tenant is refused.

    由之后的客户管理服务在「建客户」的同一个事务里调用。重复创建由
    `uq_wallets_tenant_id` 拒绝（`IntegrityError`）。
    """
    wallet = Wallet(
        tenant_id=tenant_id,
        currency=WALLET_CURRENCY,
        balance=Decimal(0),
        version=0,
        created_at=now,
        updated_at=now,
    )
    session.add(wallet)
    session.flush()
    return wallet


def get_wallet_for_tenant(session: Session, tenant_id: int) -> Wallet | None:
    statement = select(Wallet).where(Wallet.tenant_id == tenant_id)
    return session.execute(statement).scalar_one_or_none()


def post_transaction(
    session: Session,
    *,
    tenant_id: int,
    transaction_type: TransactionType | str,
    amount: Decimal,
    reference_type: ReferenceType | str,
    reference_id: str,
    now: dt.datetime,
    created_by: int | None = None,
    description: str | None = None,
    metadata: Mapping[str, Any] | None = None,
    actor_role: str | None = None,
) -> PostResult:
    """Append one ledger row and everything that must commit with it. Flushes, never commits.

    同一次 flush 里：账本行（触发器随之推进钱包）；调账与系统更正的审计；计费状态
    跃迁（租户、审计、`tenant.billing_status_changed`）；低余额事件。

    同一来源 `(reference_type, reference_id)` 已记过时：钱包、类型、金额都一致就
    返回既有行（`replayed=True`，什么都不写），否则 `LedgerConflict`。
    所有校验错误都在任何写入之前抛出。
    """
    kind = coerce_transaction_type(transaction_type)
    source = coerce_reference_type(reference_type)
    value = validate_amount(amount)
    check_transaction_shape(kind, value, source)
    check_reference_id(reference_id)
    check_reason_and_actor(source, description, created_by, actor_role)
    check_metadata(metadata)

    wallet = _lock_wallet(session, tenant_id)
    if wallet is None:
        raise WalletNotFound("WALLET_NOT_FOUND")

    # 在钱包锁内查重。别的租户用过同一来源也是冲突：`wallet_id` 不同。
    existing = _find_by_reference(session, source, reference_id)
    if existing is not None:
        same_effect = (
            existing.wallet_id == wallet.id
            and existing.transaction_type == kind
            and existing.amount == value
        )
        if not same_effect:
            raise LedgerConflict("REFERENCE_ALREADY_POSTED_WITH_DIFFERENT_PAYLOAD")
        return PostResult(transaction=existing, replayed=True)

    tenant = _lock_tenant(session, tenant_id)
    # `balance_before` 与序号取自锁内刚刷新的钱包行。
    before = wallet.balance
    after = before + value
    if not fits_money(after):
        raise InvalidAmount("BALANCE_OUT_OF_RANGE")

    row = WalletTransaction(
        public_id=str(uuid.uuid4()),
        wallet_id=wallet.id,
        tenant_id=tenant_id,
        wallet_sequence=wallet.version + 1,
        transaction_type=kind,
        amount=value,
        balance_before=before,
        balance_after=after,
        reference_type=source,
        reference_id=reference_id,
        description=description,
        metadata_json=dict(metadata) if metadata is not None else None,
        created_by=created_by,
        created_at=now,
    )
    session.add(row)
    if source in ADJUSTMENT_REFERENCE_TYPES:
        session.add(_adjustment_audit(row, actor_role=actor_role))
    _apply_billing_status(session, tenant, row)
    if crosses_low_balance(tenant.low_balance_threshold, before, after):
        payload = {
            "balance": _money_text(after),
            "threshold": _money_text(tenant.low_balance_threshold),
            "wallet_transaction": row.public_id,
        }
        session.add(_outbox_event(EVENT_LOW_BALANCE, tenant, payload, now))

    session.flush()
    # ⚠️ 钱包已被 AFTER INSERT 触发器在库里改写，内存里这份必然过期。
    session.expire(wallet)
    return PostResult(transaction=row, replayed=False)


def list_transactions_for_tenant(
    session: Session,
    tenant_id: int,
    *,
    limit: int,
    before_sequence: int | None = None,
) -> list[WalletTransaction]:
    """Newest first. Keyset paging: pass the last sequence seen as `before_sequence`."""
    if limit < 1:
        raise ValueError("limit must be at least 1")
    statement = select(WalletTransaction).where(WalletTransaction.tenant_id == tenant_id)
    if before_sequence is not None:
        statement = statement.where(WalletTransaction.wallet_sequence < before_sequence)
    newest_first = WalletTransaction.wallet_sequence.desc()
    statement = statement.order_by(newest_first).limit(min(limit, MAX_PAGE_SIZE))
    return list(session.execute(statement).scalars().all())


def verify_wallet(session: Session, tenant_id: int) -> list[str]:
    """Problem codes for this tenant's wallet; an empty list means it is consistent.

    只报告，不修复：不一致由人工核查，用 `SYSTEM_CORRECTION` 记补偿行。
    """
    # 刷新会话里可能已有的旧对象，核对的是库里的值。
    wallet_query = select(Wallet).where(Wallet.tenant_id == tenant_id)
    wallet = session.execute(
        wallet_query.execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if wallet is None:
        return [WALLET_MISSING]
    tenant_query = select(Tenant).where(Tenant.id == tenant_id)
    tenant = session.execute(tenant_query.execution_options(populate_existing=True)).scalar_one()
    # 列的顺序即 LedgerLine 的字段顺序。
    columns = (
        WalletTransaction.wallet_sequence,
        WalletTransaction.amount,
        WalletTransaction.balance_before,
        WalletTransaction.balance_after,
    )
    rows_query = (
        select(*columns)
        .where(WalletTransaction.wallet_id == wallet.id)
        .where(WalletTransaction.tenant_id == tenant_id)
        .order_by(WalletTransaction.wallet_sequence)
    )
    lines = [LedgerLine(*row) for row in session.execute(rows_query).all()]
    return ledger_problems(
        balance=wallet.balance,
        version=wallet.version,
        billing_status=tenant.billing_status,
        lines=lines,
    )


# --- 内部 -------------------------------------------------------------------------


def _lock_wallet(session: Session, tenant_id: int) -> Wallet | None:
    # ⚠️ `populate_existing=True` 不能少：没有它，会话里已有的 Wallet 对象会被原样
    # 返回 —— 锁拿到了，读到的却是旧值（app/services/password_reset.py 踩过）。
    statement = (
        select(Wallet)
        .where(Wallet.tenant_id == tenant_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return session.execute(statement).scalar_one_or_none()


def _lock_tenant(session: Session, tenant_id: int) -> Tenant:
    # 钱包锁之后才取：加锁顺序固定为钱包 → 租户。同上，必须刷新会话里的旧对象，
    # 否则跃迁判定与 `status_version` 都会基于旧值。
    statement = (
        select(Tenant)
        .where(Tenant.id == tenant_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return session.execute(statement).scalar_one()


def _find_by_reference(
    session: Session,
    reference_type: ReferenceType,
    reference_id: str,
) -> WalletTransaction | None:
    # ⚠️ 刻意不加锁：对不存在的键加锁读会取间隙锁，不同租户相邻的来源 ID 会
    # 互相阻塞甚至死锁。同一钱包已由钱包锁串行；漏网的并发插入由唯一约束兜底
    # （IntegrityError，调用方重试即走重放或冲突分支）。
    statement = select(WalletTransaction).where(
        WalletTransaction.reference_type == reference_type,
        WalletTransaction.reference_id == reference_id,
    )
    return session.execute(statement).scalar_one_or_none()


def _apply_billing_status(session: Session, tenant: Tenant, row: WalletTransaction) -> None:
    """Move the tenant's billing status if the new balance requires it (design §2 table)."""
    wanted = billing_transition(tenant.billing_status, row.balance_after)
    if wanted is None:
        # 在 SUSPENDED 下继续扣费不产生新的跃迁、审计或事件（spec §7 第 11 条）。
        return
    before_state = {
        "billing_status": tenant.billing_status.value,
        "status_version": tenant.status_version,
    }
    # 锁内刚读到的值 + 1。
    tenant.billing_status = wanted
    tenant.status_version = tenant.status_version + 1
    if wanted is BillingStatus.ACTIVE:
        reason = REASON_BALANCE_POSITIVE
    else:
        reason = REASON_BALANCE_NON_POSITIVE
    after_state = {
        "billing_status": wanted.value,
        "status_version": tenant.status_version,
        "balance": _money_text(row.balance_after),
        "wallet_transaction": row.public_id,
    }
    session.add(
        AuditLog(
            actor_user_id=None,
            actor_role=SYSTEM_ACTOR_ROLE,
            action=AuditAction.TENANT_BILLING_STATUS_CHANGED,
            entity_type=AGGREGATE_TENANT,
            entity_id=tenant.public_id,
            before_state=_json(before_state),
            after_state=_json(after_state),
            reason=reason,
            created_at=row.created_at,
        )
    )
    payload = {
        "billing_status": wanted.value,
        "status_version": tenant.status_version,
        "reason": reason,
        "balance": _money_text(row.balance_after),
        "wallet_transaction": row.public_id,
    }
    session.add(_outbox_event(EVENT_BILLING_STATUS_CHANGED, tenant, payload, row.created_at))


def _adjustment_audit(row: WalletTransaction, *, actor_role: str | None) -> AuditLog:
    # spec §60：操作者、前后余额、原因、账本行。⚠️ 不写 metadata_json。
    return AuditLog(
        actor_user_id=row.created_by,
        actor_role=actor_role,
        action=AuditAction.WALLET_ADJUSTMENT_POSTED,
        entity_type=ENTITY_WALLET_TRANSACTION,
        entity_id=row.public_id,
        before_state=_json({"balance": _money_text(row.balance_before)}),
        after_state=_json({"balance": _money_text(row.balance_after)}),
        reason=row.description,
        created_at=row.created_at,
    )


def _outbox_event(
    event_type: str,
    tenant: Tenant,
    payload: dict[str, object],
    now: dt.datetime,
) -> DomainOutbox:
    # ⚠️ payload 里没有联系人等个人数据，也没有成本或毛利（设计 §2）。
    # 以 PENDING 持久保存：app/tasks/outbox.py 的 recover 只重投有处理器的类型，
    # 这两类在接上投递之前不会被重试成死信（INV-14）。
    return DomainOutbox(
        event_type=event_type,
        aggregate_type=AGGREGATE_TENANT,
        aggregate_id=tenant.public_id,
        payload_json=_json(payload),
        status=OutboxStatus.PENDING,
        attempt_count=0,
        next_retry_at=now,
        created_at=now,
    )


def _money_text(value: Decimal | None) -> str | None:
    # 定点格式：`str(Decimal("0E-8"))` 是 "0E-8"，读的人与下游都不该收到这种写法。
    return None if value is None else format(value, "f")


def _json(value: Mapping[str, object]) -> str:
    return json.dumps(value, sort_keys=True)

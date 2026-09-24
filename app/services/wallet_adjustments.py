"""Admin wallet adjustments (spec §8, §60, §124; design gate #111 v1).

⚠️ **一次调账一个 `session_scope()`**（INV-13）。账本行、`WALLET_ADJUSTMENT_POSTED`
审计、（余额跨零时）租户状态 + `TENANT_BILLING_STATUS_CHANGED` 审计 + 出站事件，全部由
`post_transaction` 在同一次 flush 里写入，在这里一次提交；任何异常整个回滚。

⚠️ **租户用不加锁的读取**，只为拿内部 id。加锁全部交给 `post_transaction`，顺序固定为
钱包 → 租户；这里先锁租户就与记账路径的加锁顺序相反，会死锁（设计 §2「事务边界」）。

⚠️ **只在 `IntegrityError` 时换新事务重试一次**（设计 §4）。MySQL 的 REPEATABLE READ
下，同一个幂等键的并发请求里后到的那个，锁内查重读的是旧快照，会撞上
`UNIQUE (reference_type, reference_id)`；新事务的快照看得到那一行，于是走重放或冲突
分支。第二次还是 `IntegrityError` 就原样抛出（500）。锁等待超时与死锁
（`OperationalError`）不重试：管理员带同一个键重发，幂等键保证只有一次效果。

⚠️ 这里**不写日志**，不记请求体，也不记原因文本：原因是管理员的自由文本，可能碰巧
写进个人数据，所以只进账本与审计（设计 §6）。这里也没有任何直接改钱包的语句：
余额只经 `post_transaction` 插入的账本行变动（INV-4）。
"""

from __future__ import annotations

import datetime as dt
import functools
from decimal import Decimal

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.core.database import session_scope
from app.core.errors import AppError
from app.models.auth import User
from app.models.wallet import ReferenceType, TransactionType
from app.repositories import tenancy
from app.repositories.wallet import (
    InvalidAmount,
    InvalidTransaction,
    LedgerConflict,
    post_transaction,
)
from app.schemas.wallet_adjustments import AdjustmentView, adjustment_view
from app.services.auth import RequestContext, utc_now
from app.services.customers import CustomerNotFound

# `post_transaction` 的 `InvalidAmount` 问题码里，唯一一个请求校验层挡不住的。
BALANCE_OUT_OF_RANGE = "BALANCE_OUT_OF_RANGE"


class AdjustmentConflict(AppError):
    """This idempotency key was already posted with another customer, type or amount."""

    def __init__(self) -> None:
        super().__init__(
            "This idempotency key was already used for a different adjustment.",
            code="ADJUSTMENT_CONFLICT",
            http_status=409,
        )


class BalanceOutOfRange(AppError):
    """The balance after this adjustment would not fit DECIMAL(20,8)."""

    def __init__(self) -> None:
        super().__init__(
            "The balance after this adjustment is out of range.",
            code=BALANCE_OUT_OF_RANGE,
            http_status=422,
        )


class AdjustmentRejected(AppError):
    """The ledger refused a request the schema let through (the two rule sets drifted).

    理论上到不了这里（设计 §2「错误」）。消息里只有问题码，不含金额或原因。
    """

    def __init__(self, problem: str) -> None:
        super().__init__(
            f"The adjustment was rejected: {problem}.",
            code="VALIDATION_ERROR",
            http_status=422,
        )


def _now() -> dt.datetime:
    """`utc_now()` truncated to whole seconds, as in app/services/customers.py.

    MySQL 的 `DATETIME` 不存小数秒。账本行、两条审计与出站事件都用这一个时刻。
    """
    return utc_now().replace(microsecond=0)


def post_adjustment(
    session_factory: sessionmaker[Session],
    *,
    actor: User,
    customer_id: str,
    transaction_type: TransactionType,
    amount: Decimal,
    reason: str,
    idempotency_key: str,
    context: RequestContext,
    now: dt.datetime | None = None,
) -> AdjustmentView:
    """Post one admin adjustment, or return the one already posted under this key.

    返回值的 `replayed` 标明是不是重放。同一个键配不同的客户、类型或金额：
    `AdjustmentConflict`，什么都不写。
    """
    attempt = functools.partial(
        _post_once,
        session_factory,
        actor=actor,
        customer_id=customer_id,
        transaction_type=transaction_type,
        amount=amount,
        reason=reason,
        idempotency_key=idempotency_key,
        context=context,
        # 重试用同一个时刻。
        now=now or _now(),
    )
    try:
        return attempt()
    except IntegrityError:
        # 上一个事务已由 session_scope 整个回滚。只重试这一次（设计 §4）。
        return attempt()


def _post_once(
    session_factory: sessionmaker[Session],
    *,
    actor: User,
    customer_id: str,
    transaction_type: TransactionType,
    amount: Decimal,
    reason: str,
    idempotency_key: str,
    context: RequestContext,
    now: dt.datetime,
) -> AdjustmentView:
    try:
        with session_scope(session_factory) as session:
            tenant = tenancy.get_tenant_by_public_id(session, customer_id)
            if tenant is None:
                raise CustomerNotFound
            result = post_transaction(
                session,
                tenant_id=tenant.id,
                transaction_type=transaction_type,
                amount=amount,
                reference_type=ReferenceType.ADMIN_ADJUSTMENT,
                reference_id=idempotency_key,
                now=now,
                created_by=actor.id,
                description=reason,
                actor_role=actor.role.value,
                ip_address=context.ip_address,
                user_agent=context.user_agent,
            )
            # 记账时 `post_transaction` 加锁重读了同一个租户对象（populate_existing），
            # 跃迁也写在它上面，所以这里读到的就是这个事务提交时的值。
            # 重放分支在锁租户之前就返回了，`tenant` 还是开头那次不加锁的读：它可能早于
            # 别的事务的提交（READ COMMITTED 下的并发同键，或 REPEATABLE READ 的旧快照）。
            # 带共享锁重读一次，拿最新提交的值；钱包锁已在手，顺序仍是钱包 → 租户。
            if result.replayed:
                session.refresh(tenant, with_for_update={"read": True})
            view = adjustment_view(
                result.transaction,
                customer_id=tenant.public_id,
                replayed=result.replayed,
                billing_status=tenant.billing_status,
                status_version=tenant.status_version,
            )
    except LedgerConflict:
        raise AdjustmentConflict from None
    except InvalidAmount as error:
        if str(error) == BALANCE_OUT_OF_RANGE:
            raise BalanceOutOfRange from None
        raise AdjustmentRejected(str(error)) from None
    except InvalidTransaction as error:
        raise AdjustmentRejected(str(error)) from None
    return view


__all__ = [
    "BALANCE_OUT_OF_RANGE",
    "AdjustmentConflict",
    "AdjustmentRejected",
    "BalanceOutOfRange",
    "post_adjustment",
]

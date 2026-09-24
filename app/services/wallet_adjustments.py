"""Admin wallet adjustments (spec §8, §60, §124; design gate #111 v1).

⚠️ **一次调账一个 `session_scope()`**（INV-13）。账本行、`WALLET_ADJUSTMENT_POSTED` 审计、
余额跨零时的租户状态、`TENANT_BILLING_STATUS_CHANGED` 审计与出站事件都由
`post_transaction` 在同一次 flush 里写，这里提交或整体回滚。余额只经账本变动
（INV-4）：这里没有任何直接改钱包的语句。

⚠️ **租户用不加锁的读取。**加锁全部交给 `post_transaction`，顺序固定为钱包 → 租户；
这里要是先锁租户，就与记账路径的加锁顺序相反，会死锁（设计 §2「事务边界」）。

⚠️ **唯一约束兜底只重试一次**（设计 §4）。MySQL 的 REPEATABLE READ 下，同一个键的两个
请求并发时，后到的锁内查重读的是旧快照，会撞 `UNIQUE (reference_type, reference_id)`。
捕获 `IntegrityError` 后整个事务已回滚，换一个新事务再来一次，新快照看得到那一行，
走重放或冲突分支。第二次仍失败就原样抛出（500）。锁等待超时与死锁
（`OperationalError`）不重试，由管理员带同一个键重发。

⚠️ 这里**不写日志**，也不记请求体与原因文本：原因是管理员的自由文本，可能碰巧写进
个人数据，只进账本与审计（设计 §6）。
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from functools import partial
from typing import Final

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.core.database import session_scope
from app.core.errors import AppError
from app.models.auth import User
from app.models.wallet import ReferenceType
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

# `InvalidAmount` 里唯一一个请求校验层挡不住的问题码：取决于记账前的余额。
BALANCE_OUT_OF_RANGE: Final = "BALANCE_OUT_OF_RANGE"


class AdjustmentConflict(AppError):
    """This idempotency key was already posted for another customer, type or amount."""

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
    """The ledger refused something the request validation should already have refused.

    只在请求校验与 repository 的规则漂移时出现（设计 §2）。消息里只有问题码。
    """

    def __init__(self, problem: str) -> None:
        super().__init__(
            f"Invalid adjustment: {problem}",
            code="VALIDATION_ERROR",
            http_status=422,
        )


def _now() -> dt.datetime:
    """`utc_now()` truncated to whole seconds, for the same reason as `services/customers.py`.

    MySQL 的 `DATETIME` 不存小数秒。账本行、两条审计与出站事件都用这一个时刻。
    """
    return utc_now().replace(microsecond=0)


def post_adjustment(
    session_factory: sessionmaker[Session],
    *,
    actor: User,
    customer_id: str,
    transaction_type: str,
    amount: Decimal,
    reason: str,
    idempotency_key: str,
    context: RequestContext,
    now: dt.datetime | None = None,
) -> AdjustmentView:
    """Post one ADMIN_ADJUSTMENT row for the customer in the path. Replays return the first row.

    `amount` 带符号，就是记进账本的数。`idempotency_key` 存成账本的 `reference_id`。
    """
    attempt = partial(
        _post_once,
        session_factory,
        actor=actor,
        customer_id=customer_id,
        transaction_type=transaction_type,
        amount=amount,
        reason=reason,
        idempotency_key=idempotency_key,
        context=context,
        now=now or _now(),
    )
    try:
        return attempt()
    except IntegrityError:
        # `session_scope` 已经回滚了整个事务。换新事务只重试这一次（设计 §4）。
        return attempt()


def _post_once(
    session_factory: sessionmaker[Session],
    *,
    actor: User,
    customer_id: str,
    transaction_type: str,
    amount: Decimal,
    reason: str,
    idempotency_key: str,
    context: RequestContext,
    now: dt.datetime,
) -> AdjustmentView:
    try:
        with session_scope(session_factory) as session:
            # ⚠️ 不加锁：只拿内部 id。加锁顺序交给 `post_transaction`（钱包 → 租户）。
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
            # 记账时 `post_transaction` 锁租户并刷新了这个对象，跃迁也写在它上面，
            # 所以这里读到的就是将要提交的状态与版本。
            view = adjustment_view(result.transaction, tenant, replayed=result.replayed)
    except LedgerConflict:
        raise AdjustmentConflict from None
    except InvalidAmount as error:
        if str(error) == BALANCE_OUT_OF_RANGE:
            raise BalanceOutOfRange from None
        raise AdjustmentRejected(str(error)) from None
    except InvalidTransaction as error:
        raise AdjustmentRejected(str(error)) from None
    # `WalletNotFound`（客户没有钱包，数据不一致）与数据库错误原样抛出：500。
    return view


__all__ = [
    "BALANCE_OUT_OF_RANGE",
    "AdjustmentConflict",
    "AdjustmentRejected",
    "BalanceOutOfRange",
    "post_adjustment",
]

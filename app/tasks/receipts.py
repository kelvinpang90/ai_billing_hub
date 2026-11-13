"""Receipt back-fill sweep (design gate #589 v2 §2「补发扫描」, AIH-TASK-201).

**数据库是事实来源，Beat 只是触发**（INV-14）：Beat 每 `RECEIPT_SWEEP_SECONDS`（300 秒）调
`issue_missing`，扫库找 `PAID` 而没有收据的支付补开。本批上线前已 `PAID` 的支付与开具检查曾经
不过的支付都由它收口；补开的收据 `payment_method = None`（PDF 上印 `Stripe`），**不发邮件**。

不看 `payments_enabled`：不调 Stripe，开关关闭时也要能补发。

一轮：

1. 候选查询在它自己的事务里做完并结束：`PAID` 且没有收据，按 `paid_at` 升序、`id` 升序，至多
   500 笔。先结束这个事务，MySQL 可重复读下每笔的重查才读得到最新数据；
2. 每笔一个事务：`SELECT … FOR UPDATE` 锁支付行（锁顺序「支付 → 当年系列行」，与入账路径一致），
   重查仍是 `PAID` 且没有收据（否则跳过、不计数），`now` 在这一笔开始时读一次时钟，
   `issue_receipt(send_email=False, payment_method=None)`，提交；
3. 返回 `None`（检查不过，`issue_receipt` 已记原因码）或抛异常（回滚这一笔、记 ERROR）都计入
   失败数，继续下一笔；预算 240 秒（按注入的时钟），每笔开始前看一次，用完就停。

并发（设计 §4）：入账事务里支付行被锁、尚未提交为 `PAID`，候选查询看不到它；入账已提交的，锁住
支付行后重查到已有收据 → 跳过。两轮重叠时第二轮在支付行锁上等待，拿到后同样跳过。

⚠️ 日志只带支付的 `public_id` 与计数（设计 §6）：异常的 ERROR 不带异常文本与堆栈——数据库错误
的文本里可能有快照里的公司名。
"""

from __future__ import annotations

import datetime as dt
import enum
import logging
from functools import lru_cache
from typing import Final

from celery import shared_task
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings
from app.core.database import create_database_engine, create_session_factory, session_scope
from app.models.payments import Payment, PaymentStatus
from app.models.receipts import PaymentReceipt
from app.services.auth import utc_now
from app.services.fx_rates import Clock
from app.services.receipts import issue_receipt

logger = logging.getLogger(__name__)

# 每轮至多这么多笔；预算用完就停（设计 §2「补发扫描」）。周期见 app/core/celery_app.py 的
# `RECEIPT_SWEEP_SECONDS`。
SWEEP_BATCH_SIZE: Final = 500
SWEEP_BUDGET: Final = dt.timedelta(seconds=240)


class _Outcome(enum.Enum):
    ISSUED = "ISSUED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


@lru_cache(maxsize=1)
def _session_factory() -> sessionmaker[Session]:
    """One engine per worker process (同 app/tasks/outbox.py)."""
    return create_session_factory(create_database_engine(get_settings()))


def _candidates(factory: sessionmaker[Session]) -> list[tuple[int, str]]:
    """`(id, public_id)` of `PAID` payments without a receipt, oldest first; its own transaction."""
    statement = (
        select(Payment.id, Payment.public_id)
        .outerjoin(PaymentReceipt, PaymentReceipt.payment_id == Payment.id)
        .where(Payment.status == PaymentStatus.PAID, PaymentReceipt.id.is_(None))
        .order_by(Payment.paid_at, Payment.id)
        .limit(SWEEP_BATCH_SIZE)
    )
    with session_scope(factory) as session:
        return [(row.id, row.public_id) for row in session.execute(statement)]


def _issue_one(factory: sessionmaker[Session], payment_id: int, clock: Clock) -> _Outcome:
    """Lock, re-check and issue one payment's receipt in one transaction."""
    with session_scope(factory) as session:
        payment = session.execute(
            select(Payment)
            .where(Payment.id == payment_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).scalar_one_or_none()
        if payment is None or payment.status is not PaymentStatus.PAID:
            return _Outcome.SKIPPED
        existing = session.execute(
            select(PaymentReceipt.id).where(PaymentReceipt.payment_id == payment.id)
        ).first()
        if existing is not None:
            return _Outcome.SKIPPED
        now = clock()
        issued = issue_receipt(
            session, payment=payment, payment_method=None, now=now, send_email=False
        )
        return _Outcome.FAILED if issued is None else _Outcome.ISSUED


def sweep(factory: sessionmaker[Session], *, clock: Clock = utc_now) -> tuple[int, int]:
    """One back-fill run. Returns `(issued, failed)`."""
    started = clock()
    issued = failed = 0
    for payment_id, public_id in _candidates(factory):
        if clock() - started >= SWEEP_BUDGET:
            break
        try:
            outcome = _issue_one(factory, payment_id, clock)
        except Exception:
            # session_scope 已回滚这一笔；继续下一笔。
            logger.error("Receipt back-fill failed for a payment", extra={"payment_id": public_id})
            failed += 1
            continue
        if outcome is _Outcome.ISSUED:
            issued += 1
        elif outcome is _Outcome.FAILED:
            failed += 1
    logger.info("Receipt back-fill finished", extra={"issued": issued, "failed": failed})
    return issued, failed


@shared_task(name="app.tasks.receipts.issue_missing")
def issue_missing() -> None:
    """Beat entry point (every 300 seconds). Safe to overlap: the payment row lock decides."""
    sweep(_session_factory(), clock=utc_now)


__all__ = ["SWEEP_BATCH_SIZE", "SWEEP_BUDGET", "issue_missing", "sweep"]

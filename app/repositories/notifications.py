"""Portal notification data access for the customer endpoints (design gate #471 v7 §2「客户接口」).

⚠️ **每条 SQL 都带 `tenant_id`**（INV-8，设计 §6）：`tenant_id` 必须来自已认证的会话
（`CustomerPrincipal.tenant_id`），不能来自客户端输入。按 `(tenant_id, public_id)` 查不到就是
`None`，不区分「不存在」与「属于别的租户」。

⚠️ **列表只按 `source_outbox_id` 倒序**（记账顺序），不按 `occurred_at`、也不按通知行自己的
`id`：通知 `id` 是投递顺序，重试会打乱它；`occurred_at` 在取钱包锁之前取，可能与记账顺序相反
（设计 §2「客户接口」排序）。

⚠️ **不提交**：写事务归调用方，调用方先 `lock_tenant_for_customer_write` 再调写函数。
这里不读、不写 `domain_outbox`；通知行只由 app/tasks/outbox.py 插入，这里只改 `read_at`。
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.models.notifications import Notification


def list_for_tenant(
    session: Session,
    tenant_id: int,
    *,
    limit: int,
    before_source_outbox_id: int | None = None,
) -> list[Notification]:
    """Up to `limit` notifications of the tenant, newest booking first.

    `before_source_outbox_id` 是上一页最后一条的 `source_outbox_id`：只取比它小的行。
    `source_outbox_id` 唯一，所以是全序，翻页不重复、不遗漏。
    """
    statement = select(Notification).where(Notification.tenant_id == tenant_id)
    if before_source_outbox_id is not None:
        statement = statement.where(Notification.source_outbox_id < before_source_outbox_id)
    statement = statement.order_by(Notification.source_outbox_id.desc()).limit(limit)
    return list(session.execute(statement).scalars())


def source_outbox_id_for(session: Session, tenant_id: int, public_id: str) -> int | None:
    """The cursor's `source_outbox_id`, looked up in this tenant only; `None` when not found."""
    statement = select(Notification.source_outbox_id).where(
        Notification.tenant_id == tenant_id, Notification.public_id == public_id
    )
    return session.execute(statement).scalar_one_or_none()


def count_unread(session: Session, tenant_id: int) -> int:
    """Rows of the tenant with `read_at IS NULL`; paging does not change it."""
    statement = (
        select(func.count())
        .select_from(Notification)
        .where(Notification.tenant_id == tenant_id, Notification.read_at.is_(None))
    )
    return int(session.execute(statement).scalar_one())


def mark_read(
    session: Session, tenant_id: int, public_id: str, *, now: dt.datetime
) -> Notification | None:
    """Mark one notification read and read it back; `None` when the tenant has no such row.

    只在 `read_at IS NULL` 时写：已读的再标一次不改首次时刻（设计 §4，没有「已读 → 未读」）。
    """
    session.execute(
        update(Notification)
        .where(
            Notification.tenant_id == tenant_id,
            Notification.public_id == public_id,
            Notification.read_at.is_(None),
        )
        .values(read_at=now)
        .execution_options(synchronize_session=False)
    )
    statement = (
        select(Notification)
        .where(Notification.tenant_id == tenant_id, Notification.public_id == public_id)
        # 本会话若已载入过这一行，不加它 identity map 会还回 UPDATE 之前的值。
        .execution_options(populate_existing=True)
    )
    return session.execute(statement).scalar_one_or_none()


def mark_all_read(session: Session, tenant_id: int, *, now: dt.datetime) -> int:
    """Mark every unread notification of the tenant read; the number of rows marked.

    只标记此刻已提交的未读行：之后才提交的通知仍是未读（设计 §4、§10）。
    """
    result = session.execute(
        update(Notification)
        .where(Notification.tenant_id == tenant_id, Notification.read_at.is_(None))
        .values(read_at=now)
        .execution_options(synchronize_session=False)
    )
    return int(result.rowcount)


__all__ = [
    "count_unread",
    "list_for_tenant",
    "mark_all_read",
    "mark_read",
    "source_outbox_id_for",
]

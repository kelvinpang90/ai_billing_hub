"""Outbound webhook signing-secret data access (design gate #135 v1 §2, AIH-TASK-019).

⚠️ **只 flush，不 commit。**密钥行与它的审计同一事务，事务边界归调用方的
`session_scope()`（app/services/webhook_signing.py）。

⚠️ **按项目的读取一律带 `project_id`。**`project_id` 必须来自按「客户 + 项目 public_id」
解析出的内部 id；别的项目的版本与不存在的一样查不到（INV-8）。

⚠️ **这里没有删除。**退役只改状态、不清密文（设计 §6）。

⚠️ 这一层不写日志。明文 secret 从不经过这里，这里只见到密文。
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.integration import ProjectWebhookSecret, WebhookSecretStatus
from app.models.tenancy import Project


def lock_project(session: Session, project_id: int) -> None:
    """`SELECT … FOR UPDATE` on the project row (MySQL; SQLite ignores it).

    同一项目的签发、启用、退役因此排队执行：后到的读到的是先到的提交之后的版本
    （设计 §4「串行化」）。钱包路径不锁项目，不引入新的死锁顺序。
    """
    statement = select(Project.id).where(Project.id == project_id).with_for_update()
    session.execute(statement).scalar_one()


def versions_for_project(session: Session, project_id: int) -> list[ProjectWebhookSecret]:
    """Every version of the project, oldest first, row-locked. Call it after `lock_project`.

    ⚠️ 这里也要 `FOR UPDATE`（加锁读）：MySQL 默认的 REPEATABLE READ 下，普通 SELECT 读的是
    本事务第一次读时的快照 —— 在项目锁上排队的那一个会看不见先到者刚提交的版本，接着撞
    唯一索引。加锁读总是读最新提交的行。`populate_existing`：会话里已有的对象按锁内读到的
    值刷新，不沿用锁之前的旧值。SQLite 忽略 `FOR UPDATE`。
    """
    statement = (
        select(ProjectWebhookSecret)
        .where(ProjectWebhookSecret.project_id == project_id)
        .order_by(ProjectWebhookSecret.key_version)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return list(session.execute(statement).scalars().all())


def insert_pending(
    session: Session,
    *,
    tenant_id: int,
    project_id: int,
    key_version: int,
    encrypted_secret: str,
    encryption_key_version: int,
    now: dt.datetime,
) -> ProjectWebhookSecret:
    """Insert and flush one PENDING version. `now` is naive UTC.

    撞上 `(project_id, key_version)` 或 `pending_slot` 的唯一索引就在 flush 时抛
    `IntegrityError`，由调用方映射。
    """
    row = ProjectWebhookSecret(
        tenant_id=tenant_id,
        project_id=project_id,
        key_version=key_version,
        status=WebhookSecretStatus.PENDING,
        encrypted_secret=encrypted_secret,
        encryption_key_version=encryption_key_version,
        created_at=now,
        activated_at=None,
        retired_at=None,
    )
    session.add(row)
    session.flush()
    return row


def activate(session: Session, row: ProjectWebhookSecret, *, now: dt.datetime) -> None:
    """PENDING → ACTIVE. The caller retires the old ACTIVE (and flushes) first.

    先退旧、再启新：`active_slot` 的唯一索引逐行检查，顺序反了就在 flush 时撞上。
    """
    if row.status is not WebhookSecretStatus.PENDING:
        # 调用方先判断；到这里说明判断漏了。消息里没有任何密钥。
        raise ValueError("only a PENDING version can be activated")
    row.status = WebhookSecretStatus.ACTIVE
    row.activated_at = now
    session.flush()


def retire(session: Session, row: ProjectWebhookSecret, *, now: dt.datetime) -> None:
    """PENDING or ACTIVE → RETIRED, terminal. The row and its ciphertext stay (design §6)."""
    if row.status is WebhookSecretStatus.RETIRED:
        raise ValueError("the version is already RETIRED")
    row.status = WebhookSecretStatus.RETIRED
    row.retired_at = now
    session.flush()


def list_for_project(
    session: Session, project_id: int, *, offset: int, limit: int
) -> list[ProjectWebhookSecret]:
    """One page of versions, `key_version` ascending."""
    if offset < 0:
        raise ValueError("offset must not be negative")
    if limit < 1:
        raise ValueError("limit must be at least 1")
    statement = (
        select(ProjectWebhookSecret)
        .where(ProjectWebhookSecret.project_id == project_id)
        .order_by(ProjectWebhookSecret.key_version)
        .offset(offset)
        .limit(limit)
    )
    return list(session.execute(statement).scalars().all())


def count_for_project(session: Session, project_id: int) -> int:
    statement = (
        select(func.count())
        .select_from(ProjectWebhookSecret)
        .where(ProjectWebhookSecret.project_id == project_id)
    )
    return int(session.execute(statement).scalar_one())


def get_active(session: Session, project_id: int) -> tuple[ProjectWebhookSecret, str] | None:
    """The project's ACTIVE version and the project's `public_id` (for the AAD), or `None`."""
    statement = (
        select(ProjectWebhookSecret, Project.public_id)
        .join(
            Project,
            (Project.id == ProjectWebhookSecret.project_id)
            & (Project.tenant_id == ProjectWebhookSecret.tenant_id),
        )
        .where(
            ProjectWebhookSecret.project_id == project_id,
            ProjectWebhookSecret.status == WebhookSecretStatus.ACTIVE,
        )
    )
    found = session.execute(statement).one_or_none()
    if found is None:
        return None
    row, public_id = found
    return row, public_id


__all__ = [
    "activate",
    "count_for_project",
    "get_active",
    "insert_pending",
    "list_for_project",
    "lock_project",
    "retire",
    "versions_for_project",
]

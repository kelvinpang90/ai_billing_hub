"""Integration credential data access (design gate #118 v1 §2, AIH-TASK-012).

⚠️ **只 flush，不 commit。**凭据行与它的审计同一事务，事务边界归调用方的
`session_scope()`（app/services/integration_access.py）。

⚠️ **按项目的读取一律带 `project_id`。**别的项目的 `api_key` 与不存在的一样查不到
（INV-8）。`project_id` 必须来自按「客户 + 项目 public_id」解析出的内部 id。

⚠️ **这里没有删除。**凭据行永久保留：以后的用量事件要引用它（INV-6），吊销只改状态。

⚠️ 这一层不写日志。明文 secret 从不经过这里，这里只见到密文。
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.integration import CredentialStatus, IntegrationCredential


def insert_credential(
    session: Session,
    *,
    tenant_id: int,
    project_id: int,
    api_key: str,
    key_version: int,
    encrypted_secret: str,
    encryption_key_version: int,
    now: dt.datetime,
) -> IntegrationCredential:
    """Insert and flush one ACTIVE version without an end. `now` is naive UTC.

    `(public_api_key, key_version)` 撞了唯一约束就在 flush 时抛 `IntegrityError`，由调用方映射。
    """
    row = IntegrationCredential(
        tenant_id=tenant_id,
        project_id=project_id,
        public_api_key=api_key,
        key_version=key_version,
        encrypted_secret=encrypted_secret,
        encryption_key_version=encryption_key_version,
        status=CredentialStatus.ACTIVE,
        valid_from=now,
        valid_until=None,
        last_used_at=None,
        created_at=now,
        revoked_at=None,
    )
    session.add(row)
    session.flush()
    return row


def lock_key_versions(
    session: Session, *, project_id: int, api_key: str
) -> list[IntegrationCredential]:
    """Every version of `api_key` under this project, oldest first, row-locked (MySQL).

    轮换与吊销的判断都在这把锁之后做：同一个 key 的并发轮换 / 吊销因此串行，后到的
    读到的是先到的提交之后的版本（设计 §2「事务边界」）。`populate_existing`：会话里
    已有的对象按锁内读到的值刷新，不沿用锁之前的旧值。SQLite 忽略 `FOR UPDATE`。
    """
    statement = (
        select(IntegrationCredential)
        .where(
            IntegrationCredential.public_api_key == api_key,
            IntegrationCredential.project_id == project_id,
        )
        .order_by(IntegrationCredential.key_version)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return list(session.execute(statement).scalars().all())


def get_credential(
    session: Session, *, api_key: str, key_version: int
) -> IntegrationCredential | None:
    """The one row for `(api_key, key_version)`, whatever its status. No lock."""
    statement = select(IntegrationCredential).where(
        IntegrationCredential.public_api_key == api_key,
        IntegrationCredential.key_version == key_version,
    )
    return session.execute(statement).scalar_one_or_none()


def list_credentials_for_project(
    session: Session, project_id: int, *, offset: int, limit: int
) -> list[IntegrationCredential]:
    """One page of versions: by when each `api_key` was first created, then by version.

    自增 id 就是创建顺序；一个 key 的「创建先后」取它最早那一行的 id。
    """
    if offset < 0:
        raise ValueError("offset must not be negative")
    if limit < 1:
        raise ValueError("limit must be at least 1")
    first_seen = (
        select(
            IntegrationCredential.public_api_key.label("api_key"),
            func.min(IntegrationCredential.id).label("first_id"),
        )
        .where(IntegrationCredential.project_id == project_id)
        .group_by(IntegrationCredential.public_api_key)
        .subquery()
    )
    statement = (
        select(IntegrationCredential)
        .join(first_seen, first_seen.c.api_key == IntegrationCredential.public_api_key)
        .where(IntegrationCredential.project_id == project_id)
        .order_by(first_seen.c.first_id, IntegrationCredential.key_version)
        .offset(offset)
        .limit(limit)
    )
    return list(session.execute(statement).scalars().all())


def count_credentials_for_project(session: Session, project_id: int) -> int:
    statement = (
        select(func.count())
        .select_from(IntegrationCredential)
        .where(IntegrationCredential.project_id == project_id)
    )
    return int(session.execute(statement).scalar_one())


def end_version_at(
    session: Session, row: IntegrationCredential, *, valid_until: dt.datetime
) -> None:
    """Set the end of one version's validity (rotation overlap). The ciphertext is untouched."""
    row.valid_until = valid_until
    session.flush()


def revoke_version(session: Session, row: IntegrationCredential, *, now: dt.datetime) -> None:
    """ACTIVE → REVOKED, terminal. The row and its ciphertext stay (design §6)."""
    if row.status is not CredentialStatus.ACTIVE:
        # 调用方先判断；到这里说明判断漏了。消息里没有任何 key。
        raise ValueError("only an ACTIVE version can be revoked")
    row.status = CredentialStatus.REVOKED
    row.revoked_at = now
    session.flush()


__all__ = [
    "count_credentials_for_project",
    "end_version_at",
    "get_credential",
    "insert_credential",
    "list_credentials_for_project",
    "lock_key_versions",
    "revoke_version",
]

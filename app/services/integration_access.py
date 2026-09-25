"""Admin management of integration API credentials (spec §36, §66, §74.4; design gate #118 v1).

四个写动作（建凭据、轮换、吊销版本、吊销 key）各一个 `session_scope()`：凭据行与
`API_KEY_CREATE` / `API_KEY_ROTATE` / `API_KEY_REVOKE` 审计同一事务，要么都在，要么都
不在（INV-13）。repository 只 flush。

⚠️ **归属只来自路径**：客户按 `public_id` 读、项目按「客户 + 项目 public_id」读（都不加锁，
只为拿内部 id）；凭据按「项目 + api_key」查。别人的一律 404，与不存在的一模一样（INV-8）。

⚠️ **轮换与吊销先锁住这个 key 的全部版本行**（按 `key_version` 排序），判断与写入都在
锁之后：同一个 key 的并发轮换 / 吊销串行执行，后到的读到先到的结果（设计 §2、§4）。

⚠️ **secret 只活在一次请求的内存里**：生成 → `encrypt_secret`（带行 AAD）→ 响应模型的
`SecretStr`。它不进审计、不进异常消息、不进日志；这一层根本不写日志。提交失败时它随
事务一起丢弃，从未返回给调用方（设计 §5）。

⚠️ 这里没有删除凭据行的函数：凭据永久保留（INV-6），吊销只改状态。
"""

from __future__ import annotations

import datetime as dt
import secrets
from typing import Final

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.core.crypto import MasterKeyring, encrypt_secret, load_keyring
from app.core.database import session_scope
from app.core.errors import AppError
from app.models.auth import AuditAction, User
from app.models.integration import CredentialStatus, IntegrationCredential
from app.models.tenancy import Project, Tenant
from app.repositories import integration_access as credentials
from app.repositories import tenancy
from app.schemas.customers import Page
from app.schemas.integration_access import (
    CredentialView,
    IssuedCredentialView,
    credential_view,
    issued_credential_view,
)
from app.services.auth import RequestContext, record_audit, utc_now
from app.services.customers import CustomerNotFound
from app.services.integration_auth import credential_aad, is_verifiable

# 审计的 entity_type；entity_id 是 api_key。
ENTITY_INTEGRATION_CREDENTIAL: Final = "integration_credential"

# 前缀让密钥扫描工具与人都认得出它是什么（设计 §2「格式」）。
API_KEY_PREFIX: Final = "ak_"
SECRET_PREFIX: Final = "sk_"
FIRST_KEY_VERSION: Final = 1


class ProjectNotFound(AppError):
    """The project does not exist, or it belongs to another customer — the same 404."""

    def __init__(self) -> None:
        super().__init__("Project not found.", code="PROJECT_NOT_FOUND", http_status=404)


class CredentialNotFound(AppError):
    """No such `api_key` / `key_version` under this project — or it is another project's."""

    def __init__(self) -> None:
        super().__init__("Credential not found.", code="CREDENTIAL_NOT_FOUND", http_status=404)


class CredentialVersionConflict(AppError):
    """`current_key_version` is not the newest version any more (double click, two admins)."""

    def __init__(self) -> None:
        super().__init__(
            "The credential has changed; reload it and decide again.",
            code="CREDENTIAL_VERSION_CONFLICT",
            http_status=409,
        )


class CredentialRevoked(AppError):
    """Every version of this `api_key` is revoked; revoked is terminal."""

    def __init__(self) -> None:
        super().__init__("The credential is revoked.", code="CREDENTIAL_REVOKED", http_status=409)


def _now() -> dt.datetime:
    """`utc_now()` truncated to whole seconds, as in app/services/customers.py."""
    return utc_now().replace(microsecond=0)


def _new_api_key() -> str:
    return API_KEY_PREFIX + secrets.token_hex(16)


def _new_secret() -> str:
    # 256 位。HMAC 的密钥就是这整个字符串的 UTF-8 字节。
    return SECRET_PREFIX + secrets.token_hex(32)


def _iso(moment: dt.datetime | None) -> str | None:
    return moment.isoformat() if moment is not None else None


def _encrypt(
    keyring: MasterKeyring, secret: str, api_key: str, key_version: int
) -> tuple[str, int]:
    """(密文, 主密钥版本)。AAD 把密文绑定到这一行的 key 与版本。"""
    return encrypt_secret(keyring, secret, associated_data=credential_aad(api_key, key_version))


def _resolve_project(session: Session, customer_id: str, project_id: str) -> tuple[Tenant, Project]:
    tenant = tenancy.get_tenant_by_public_id(session, customer_id)
    if tenant is None:
        raise CustomerNotFound
    project = tenancy.get_project_for_tenant(session, tenant.id, project_id)
    if project is None:
        raise ProjectNotFound
    return tenant, project


def _locked_versions(
    session: Session, project: Project, api_key: str
) -> list[IntegrationCredential]:
    rows = credentials.lock_key_versions(session, project_id=project.id, api_key=api_key)
    if not rows:
        raise CredentialNotFound
    return rows


def _versions(rows: list[IntegrationCredential], *fields: str) -> list[dict[str, object]]:
    """审计里的版本清单：只有 `key_version` 与点名的字段，永远没有密文或主密钥版本。"""
    listed: list[dict[str, object]] = []
    for row in rows:
        entry: dict[str, object] = {"key_version": row.key_version}
        for name in fields:
            value = getattr(row, name)
            if isinstance(value, dt.datetime):
                entry[name] = value.isoformat()
            elif isinstance(value, CredentialStatus):
                entry[name] = value.value
            else:
                entry[name] = value
        listed.append(entry)
    return listed


def _view(row: IntegrationCredential, moment: dt.datetime) -> CredentialView:
    return credential_view(row, verifiable=is_verifiable(row, moment))


def create_credential(
    session_factory: sessionmaker[Session],
    settings: Settings,
    *,
    actor: User,
    customer_id: str,
    project_id: str,
    context: RequestContext,
    now: dt.datetime | None = None,
) -> IssuedCredentialView:
    """A new `api_key`, version 1, and `API_KEY_CREATE` audit in one commit.

    建凭据不加锁：`api_key` 是新随机值，唯一约束兜底；撞上的概率可忽略，撞上就是 500、
    不重试（设计 §2）。
    """
    # ⚠️ 主密钥先于一切：没配置就 503，什么都不生成、不写（设计 §5）。
    keyring = load_keyring(settings)
    moment = now or _now()
    with session_scope(session_factory) as session:
        tenant, project = _resolve_project(session, customer_id, project_id)
        api_key = _new_api_key()
        secret = _new_secret()
        encrypted, master_version = _encrypt(keyring, secret, api_key, FIRST_KEY_VERSION)
        row = credentials.insert_credential(
            session,
            tenant_id=project.tenant_id,
            project_id=project.id,
            api_key=api_key,
            key_version=FIRST_KEY_VERSION,
            encrypted_secret=encrypted,
            encryption_key_version=master_version,
            now=moment,
        )
        record_audit(
            session,
            action=AuditAction.API_KEY_CREATE,
            context=context,
            now=moment,
            actor=actor,
            entity_type=ENTITY_INTEGRATION_CREDENTIAL,
            entity_id=api_key,
            # ⚠️ 字段白名单（设计 §2「审计」）：没有 secret、密文、主密钥版本。
            after_state={
                "api_key": api_key,
                "key_version": FIRST_KEY_VERSION,
                "project_public_id": project.public_id,
                "tenant_public_id": tenant.public_id,
                "valid_from": _iso(moment),
            },
        )
        view = issued_credential_view(row, secret=secret, verifiable=is_verifiable(row, moment))
    return view


def rotate_credential(
    session_factory: sessionmaker[Session],
    settings: Settings,
    *,
    actor: User,
    customer_id: str,
    project_id: str,
    api_key: str,
    current_key_version: int,
    context: RequestContext,
    now: dt.datetime | None = None,
) -> IssuedCredentialView:
    """Add version N+1 of the same `api_key`; older live versions end after the overlap.

    旧的未吊销版本中，`valid_until` 为 NULL 或晚于 `now + 重叠期` 的改成 `now + 重叠期`；
    新版本没有截止。整个 key 都已吊销：409 `CREDENTIAL_REVOKED`；`current_key_version`
    不是锁内读到的最大版本：409 `CREDENTIAL_VERSION_CONFLICT`。都不写库。
    """
    keyring = load_keyring(settings)
    moment = now or _now()
    overlap_until = moment + dt.timedelta(seconds=settings.credential_rotation_overlap_seconds)
    with session_scope(session_factory) as session:
        _tenant, project = _resolve_project(session, customer_id, project_id)
        rows = _locked_versions(session, project, api_key)
        live = [row for row in rows if row.status is CredentialStatus.ACTIVE]
        if not live:
            raise CredentialRevoked
        newest = rows[-1].key_version
        if current_key_version != newest:
            raise CredentialVersionConflict

        before = _versions(live, "valid_until")
        for row in live:
            if row.valid_until is None or row.valid_until > overlap_until:
                credentials.end_version_at(session, row, valid_until=overlap_until)

        new_version = newest + 1
        secret = _new_secret()
        encrypted, master_version = _encrypt(keyring, secret, api_key, new_version)
        # ⚠️ 新行的归属取自锁住的旧行，不取自别处：同一个 api_key 的所有版本属于同一个项目。
        owner = rows[0]
        try:
            row = credentials.insert_credential(
                session,
                tenant_id=owner.tenant_id,
                project_id=owner.project_id,
                api_key=owner.public_api_key,
                key_version=new_version,
                encrypted_secret=encrypted,
                encryption_key_version=master_version,
                now=moment,
            )
        except IntegrityError:
            # 锁之外的最后兜底：`(public_api_key, key_version)` 唯一约束（设计 §2）。
            raise CredentialVersionConflict from None
        record_audit(
            session,
            action=AuditAction.API_KEY_ROTATE,
            context=context,
            now=moment,
            actor=actor,
            entity_type=ENTITY_INTEGRATION_CREDENTIAL,
            entity_id=owner.public_api_key,
            before_state={"versions": before},
            after_state={"key_version": new_version, "versions": _versions(live, "valid_until")},
        )
        view = issued_credential_view(row, secret=secret, verifiable=is_verifiable(row, moment))
    return view


def revoke_version(
    session_factory: sessionmaker[Session],
    *,
    actor: User,
    customer_id: str,
    project_id: str,
    api_key: str,
    key_version: int,
    reason: str,
    context: RequestContext,
    now: dt.datetime | None = None,
) -> CredentialView:
    """Revoke one version at once. Already revoked: the current state, nothing written.

    不需要主密钥。吊销最新版本、而旧版本还在重叠期内也允许：那是管理员的明确操作，
    审计里看得见（设计 §4）。
    """
    moment = now or _now()
    with session_scope(session_factory) as session:
        _tenant, project = _resolve_project(session, customer_id, project_id)
        rows = _locked_versions(session, project, api_key)
        target = next((row for row in rows if row.key_version == key_version), None)
        if target is None:
            raise CredentialNotFound
        if target.status is CredentialStatus.ACTIVE:
            before = _versions([target], "status")
            credentials.revoke_version(session, target, now=moment)
            record_audit(
                session,
                action=AuditAction.API_KEY_REVOKE,
                context=context,
                now=moment,
                actor=actor,
                entity_type=ENTITY_INTEGRATION_CREDENTIAL,
                entity_id=target.public_api_key,
                before_state={"versions": before},
                after_state={"versions": _versions([target], "status")},
                reason=reason,
            )
        view = _view(target, moment)
    return view


def revoke_key(
    session_factory: sessionmaker[Session],
    *,
    actor: User,
    customer_id: str,
    project_id: str,
    api_key: str,
    reason: str,
    context: RequestContext,
    now: dt.datetime | None = None,
) -> list[CredentialView]:
    """Revoke every ACTIVE version of `api_key` under one audit row; return all versions.

    全部已吊销时返回当前状态，什么都不写（幂等）。
    """
    moment = now or _now()
    with session_scope(session_factory) as session:
        _tenant, project = _resolve_project(session, customer_id, project_id)
        rows = _locked_versions(session, project, api_key)
        live = [row for row in rows if row.status is CredentialStatus.ACTIVE]
        if live:
            before = _versions(live, "status")
            for row in live:
                credentials.revoke_version(session, row, now=moment)
            record_audit(
                session,
                action=AuditAction.API_KEY_REVOKE,
                context=context,
                now=moment,
                actor=actor,
                entity_type=ENTITY_INTEGRATION_CREDENTIAL,
                entity_id=rows[0].public_api_key,
                before_state={"versions": before},
                after_state={"versions": _versions(live, "status")},
                reason=reason,
            )
        views = [_view(row, moment) for row in rows]
    return views


def list_credentials(
    session_factory: sessionmaker[Session],
    *,
    customer_id: str,
    project_id: str,
    page: int,
    page_size: int,
    now: dt.datetime | None = None,
) -> Page[CredentialView]:
    """Every version of every key of the project; `verifiable` is computed at `now`."""
    moment = now or _now()
    with session_factory() as session:
        _tenant, project = _resolve_project(session, customer_id, project_id)
        rows = credentials.list_credentials_for_project(
            session, project.id, offset=(page - 1) * page_size, limit=page_size
        )
        return Page[CredentialView](
            items=[_view(row, moment) for row in rows],
            page=page,
            page_size=page_size,
            total=credentials.count_credentials_for_project(session, project.id),
        )


__all__ = [
    "API_KEY_PREFIX",
    "ENTITY_INTEGRATION_CREDENTIAL",
    "SECRET_PREFIX",
    "CredentialNotFound",
    "CredentialRevoked",
    "CredentialVersionConflict",
    "ProjectNotFound",
    "create_credential",
    "list_credentials",
    "revoke_key",
    "revoke_version",
    "rotate_credential",
]

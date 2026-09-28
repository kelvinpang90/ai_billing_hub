"""Outbound status-webhook signing secrets (spec §28, §36, §37, §66; design gate #135 v1).

两部分：

- **管理端操作**（签发、启用、退役、列表）：写动作各一个 `session_scope()`，密钥行与
  `WEBHOOK_SECRET_ISSUE` / `WEBHOOK_SECRET_ACTIVATE` / `WEBHOOK_SECRET_RETIRE` 审计同一事务，
  要么都在，要么都不在（INV-13）。repository 只 flush。
- **签名库**（不接任何发送方）：`signing_material` 取出项目此刻 `ACTIVE` 的明文与版本号；
  `sign_status_webhook` 按 §37 算出四个请求头。规范串复用 app/services/integration_auth.py
  的 `canonical_request` 与 `sign`，不另写一套。

⚠️ **归属只来自路径**：客户按 `public_id` 读、项目按「客户 + 项目 public_id」读；别人的
一律 404，与不存在的一模一样（INV-8）。

⚠️ **每个写操作先 `SELECT … FOR UPDATE` 锁项目行**，读版本、判断与写入都在锁之后：同一
项目的密钥操作串行执行（设计 §4）。锁之外的最后兜底是唯一索引，撞上映射为 409
`WEBHOOK_SECRET_CONFLICT`。

⚠️ **secret 只活在一次请求的内存里**：生成 → `encrypt_secret`（带 AAD）→ 响应模型的
`SecretStr`。它不进审计、不进异常消息、不进日志；这一层根本不写日志。提交失败时它随
事务一起丢弃，从未返回给调用方（设计 §5）。
"""

from __future__ import annotations

import datetime as dt
import secrets
from dataclasses import dataclass, field
from typing import Final

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.core.crypto import MasterKeyring, decrypt_secret, encrypt_secret, load_keyring
from app.core.database import session_scope
from app.core.errors import AppError
from app.models.auth import AuditAction, User
from app.models.integration import ProjectWebhookSecret, WebhookSecretStatus
from app.models.tenancy import Project
from app.repositories import tenancy
from app.repositories import webhook_signing as webhook_secrets
from app.schemas.customers import Page
from app.schemas.webhook_signing import (
    IssuedWebhookSecretView,
    WebhookSecretView,
    issued_webhook_secret_view,
    webhook_secret_view,
)
from app.services.auth import RequestContext, record_audit, utc_now
from app.services.customers import CustomerNotFound
from app.services.integration_access import ProjectNotFound
from app.services.integration_auth import canonical_request, sign

# 审计的 entity_type；entity_id 是项目的 public_id。
ENTITY_WEBHOOK_SECRET: Final = "project_webhook_secret"

# 前缀让密钥扫描工具与人都认得出它是什么（设计 §2「secret 格式」）。
SECRET_PREFIX: Final = "whs_"
FIRST_KEY_VERSION: Final = 1

# 出站请求头（spec §28）。
TIMESTAMP_HEADER: Final = "X-Acuven-Timestamp"
SIGNATURE_HEADER: Final = "X-Acuven-Signature"
EVENT_ID_HEADER: Final = "X-Acuven-Event-ID"
KEY_VERSION_HEADER: Final = "X-Acuven-Key-Version"


class WebhookSecretNotFound(AppError):
    """The project has no such `key_version` — or the version is another project's."""

    def __init__(self) -> None:
        super().__init__(
            "Webhook secret not found.", code="WEBHOOK_SECRET_NOT_FOUND", http_status=404
        )


class WebhookSecretPendingExists(AppError):
    """Issuing while a PENDING version waits: activate or retire it first."""

    def __init__(self) -> None:
        super().__init__(
            "A pending webhook secret already exists; activate or retire it first.",
            code="WEBHOOK_SECRET_PENDING_EXISTS",
            http_status=409,
        )


class WebhookSecretNotPending(AppError):
    """Activating a RETIRED version; RETIRED is terminal."""

    def __init__(self) -> None:
        super().__init__(
            "Only a pending webhook secret can be activated.",
            code="WEBHOOK_SECRET_NOT_PENDING",
            http_status=409,
        )


class WebhookSecretConflict(AppError):
    """A unique index fired: the last guard behind the project lock (design §4)."""

    def __init__(self) -> None:
        super().__init__(
            "The webhook secrets changed; reload them and decide again.",
            code="WEBHOOK_SECRET_CONFLICT",
            http_status=409,
        )


@dataclass(frozen=True)
class SigningMaterial:
    """The ACTIVE secret of a project and its version. `repr` never shows the secret."""

    secret: str = field(repr=False)
    key_version: int


def _now() -> dt.datetime:
    """`utc_now()` truncated to whole seconds, as in app/services/customers.py."""
    return utc_now().replace(microsecond=0)


def _new_secret() -> str:
    # 256 位。HMAC 的密钥就是这整个字符串的 UTF-8 字节。
    return SECRET_PREFIX + secrets.token_hex(32)


def webhook_secret_aad(project_public_id: str, key_version: int) -> bytes:
    """The AES-GCM associated data that binds a ciphertext to its project and version.

    把一行的密文拷到别的项目或别的版本上就解不开，`DecryptionFailed`（设计 §2）。
    """
    return (
        b"project_webhook_secrets|"
        + project_public_id.encode("ascii")
        + b"|"
        + str(key_version).encode("ascii")
    )


def _resolve_project(session: Session, customer_id: str, project_id: str) -> Project:
    tenant = tenancy.get_tenant_by_public_id(session, customer_id)
    if tenant is None:
        raise CustomerNotFound
    project = tenancy.get_project_for_tenant(session, tenant.id, project_id)
    if project is None:
        raise ProjectNotFound
    return project


def _locked_versions(session: Session, project: Project) -> list[ProjectWebhookSecret]:
    webhook_secrets.lock_project(session, project.id)
    return webhook_secrets.versions_for_project(session, project.id)


def _target(rows: list[ProjectWebhookSecret], key_version: int) -> ProjectWebhookSecret:
    target = next((row for row in rows if row.key_version == key_version), None)
    if target is None:
        raise WebhookSecretNotFound
    return target


def _state(row: ProjectWebhookSecret) -> dict[str, object]:
    """审计里的一个版本：只有 `key_version` 与 `status`，永远没有密文或主密钥版本。"""
    return {"key_version": row.key_version, "status": WebhookSecretStatus(row.status).value}


def issue_secret(
    session_factory: sessionmaker[Session],
    settings: Settings,
    *,
    actor: User,
    customer_id: str,
    project_id: str,
    context: RequestContext,
    now: dt.datetime | None = None,
) -> IssuedWebhookSecretView:
    """A new PENDING version (newest + 1, or 1) and `WEBHOOK_SECRET_ISSUE` audit in one commit.

    已有 PENDING：409 `WEBHOOK_SECRET_PENDING_EXISTS`，不写库。
    """
    # ⚠️ 主密钥先于一切：没配置就 503，什么都不生成、不写（设计 §2「事务边界」）。
    keyring = load_keyring(settings)
    moment = now or _now()
    with session_scope(session_factory) as session:
        project = _resolve_project(session, customer_id, project_id)
        rows = _locked_versions(session, project)
        if any(row.status is WebhookSecretStatus.PENDING for row in rows):
            raise WebhookSecretPendingExists
        key_version = rows[-1].key_version + 1 if rows else FIRST_KEY_VERSION
        secret = _new_secret()
        encrypted, master_version = encrypt_secret(
            keyring, secret, associated_data=webhook_secret_aad(project.public_id, key_version)
        )
        try:
            row = webhook_secrets.insert_pending(
                session,
                tenant_id=project.tenant_id,
                project_id=project.id,
                key_version=key_version,
                encrypted_secret=encrypted,
                encryption_key_version=master_version,
                now=moment,
            )
        except IntegrityError:
            # 锁之外的最后兜底：`(project_id, key_version)` 与 `pending_slot` 唯一（设计 §4）。
            raise WebhookSecretConflict from None
        record_audit(
            session,
            action=AuditAction.WEBHOOK_SECRET_ISSUE,
            context=context,
            now=moment,
            actor=actor,
            entity_type=ENTITY_WEBHOOK_SECRET,
            entity_id=project.public_id,
            # ⚠️ 字段白名单（设计 §2「审计」）：没有 secret、密文、主密钥版本。
            after_state=_state(row),
        )
        view = issued_webhook_secret_view(row, secret=secret)
    return view


def activate_secret(
    session_factory: sessionmaker[Session],
    *,
    actor: User,
    customer_id: str,
    project_id: str,
    key_version: int,
    context: RequestContext,
    now: dt.datetime | None = None,
) -> list[WebhookSecretView]:
    """PENDING → ACTIVE; the old ACTIVE (if any) → RETIRED in the same transaction.

    已是 ACTIVE：返回当前各版本，什么都不写（幂等）。RETIRED：409
    `WEBHOOK_SECRET_NOT_PENDING`。不需要主密钥。返回全部版本，`key_version` 从小到大。
    """
    moment = now or _now()
    with session_scope(session_factory) as session:
        project = _resolve_project(session, customer_id, project_id)
        rows = _locked_versions(session, project)
        target = _target(rows, key_version)
        if target.status is WebhookSecretStatus.RETIRED:
            raise WebhookSecretNotPending
        if target.status is WebhookSecretStatus.PENDING:
            previous = [row for row in rows if row.status is WebhookSecretStatus.ACTIVE]
            changed = [*previous, target]
            before = [_state(row) for row in changed]
            try:
                # ⚠️ 先退旧、再启新：`active_slot` 的唯一索引逐行检查。
                for row in previous:
                    webhook_secrets.retire(session, row, now=moment)
                webhook_secrets.activate(session, target, now=moment)
            except IntegrityError:
                raise WebhookSecretConflict from None
            record_audit(
                session,
                action=AuditAction.WEBHOOK_SECRET_ACTIVATE,
                context=context,
                now=moment,
                actor=actor,
                entity_type=ENTITY_WEBHOOK_SECRET,
                entity_id=project.public_id,
                before_state={"versions": before},
                after_state={"versions": [_state(row) for row in changed]},
            )
        views = [webhook_secret_view(row) for row in rows]
    return views


def retire_secret(
    session_factory: sessionmaker[Session],
    *,
    actor: User,
    customer_id: str,
    project_id: str,
    key_version: int,
    reason: str,
    context: RequestContext,
    now: dt.datetime | None = None,
) -> WebhookSecretView:
    """PENDING or ACTIVE → RETIRED. Already RETIRED: the current state, nothing written.

    退役 ACTIVE 之后项目没有签名密钥，`signing_material` 返回 `None`（设计 §4）。
    不需要主密钥。
    """
    moment = now or _now()
    with session_scope(session_factory) as session:
        project = _resolve_project(session, customer_id, project_id)
        rows = _locked_versions(session, project)
        target = _target(rows, key_version)
        if target.status is not WebhookSecretStatus.RETIRED:
            before = _state(target)
            webhook_secrets.retire(session, target, now=moment)
            record_audit(
                session,
                action=AuditAction.WEBHOOK_SECRET_RETIRE,
                context=context,
                now=moment,
                actor=actor,
                entity_type=ENTITY_WEBHOOK_SECRET,
                entity_id=project.public_id,
                before_state=before,
                after_state=_state(target),
                reason=reason,
            )
        view = webhook_secret_view(target)
    return view


def list_secrets(
    session_factory: sessionmaker[Session],
    *,
    customer_id: str,
    project_id: str,
    page: int,
    page_size: int,
) -> Page[WebhookSecretView]:
    """Every version of the project, `key_version` ascending. Never a secret or ciphertext."""
    with session_factory() as session:
        project = _resolve_project(session, customer_id, project_id)
        rows = webhook_secrets.list_for_project(
            session, project.id, offset=(page - 1) * page_size, limit=page_size
        )
        return Page[WebhookSecretView](
            items=[webhook_secret_view(row) for row in rows],
            page=page,
            page_size=page_size,
            total=webhook_secrets.count_for_project(session, project.id),
        )


# --- 签名库（不接发送方） -------------------------------------------------------------


def signing_material(
    session: Session, keyring: MasterKeyring, project_id: int
) -> SigningMaterial | None:
    """The ACTIVE secret of the project (internal id) and its version, or `None`.

    没有 ACTIVE 时返回 `None`：调用方据此让投递等待，而不是不签名就发（设计 §2）。
    解密失败抛 `DecryptionFailed`，不返回任何明文（设计 §5）。
    """
    found = webhook_secrets.get_active(session, project_id)
    if found is None:
        return None
    row, project_public_id = found
    secret = decrypt_secret(
        keyring,
        row.encrypted_secret,
        associated_data=webhook_secret_aad(project_public_id, row.key_version),
    )
    return SigningMaterial(secret=secret, key_version=row.key_version)


def _epoch_seconds(moment: dt.datetime) -> int:
    # 本仓库的时刻是不带时区的 UTC（spec §109）。
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.UTC)
    return int(moment.timestamp())


def sign_status_webhook(
    secret: str,
    key_version: int,
    *,
    url_path_and_query: str,
    event_id: str,
    body: bytes,
    now: dt.datetime,
) -> dict[str, str]:
    """The four §28 headers of one outbound status webhook.

    `X-Acuven-Timestamp` 是 `now` 的 Unix 纪元秒整数，每次发送重新生成（§37）。规范串是
    `canonical_request("POST", url_path_and_query, timestamp, event_id, body)`：
    ⚠️ **§37 规范串的第四行在出站 webhook 上填 `X-Acuven-Event-ID`**（§28 的出站请求头里
    没有 request id；设计 §2，docs/api.md「状态 webhook 签名」）。
    """
    timestamp = str(_epoch_seconds(now))
    canonical = canonical_request("POST", url_path_and_query, timestamp, event_id, body)
    return {
        TIMESTAMP_HEADER: timestamp,
        SIGNATURE_HEADER: sign(secret, canonical),
        EVENT_ID_HEADER: event_id,
        KEY_VERSION_HEADER: str(key_version),
    }


__all__ = [
    "ENTITY_WEBHOOK_SECRET",
    "EVENT_ID_HEADER",
    "KEY_VERSION_HEADER",
    "SECRET_PREFIX",
    "SIGNATURE_HEADER",
    "TIMESTAMP_HEADER",
    "SigningMaterial",
    "WebhookSecretConflict",
    "WebhookSecretNotFound",
    "WebhookSecretNotPending",
    "WebhookSecretPendingExists",
    "activate_secret",
    "issue_secret",
    "list_secrets",
    "retire_secret",
    "sign_status_webhook",
    "signing_material",
    "webhook_secret_aad",
]

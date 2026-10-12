"""Project integration status and status webhook address (spec §25, §28; design gate #241 v6).

设计 §8 拆分第 3 项：只有服务函数。接口归 AIH-TASK-050，投递归 AIH-TASK-051。

⚠️ **项目集成状态跃迁一个 `session_scope()`**（INV-13）：
锁租户行（`FOR UPDATE`）→ 锁项目行（`FOR UPDATE`）→ 判定 → 改 `integration_status`
→ `bump_status_version`（版本 +1、`status_changed_at`、outbox 事件、作废投递退避）
→ `PROJECT_INTEGRATION_STATUS_CHANGED` 审计 → 提交。任何一步失败整体回滚。

⚠️ **设置 / 清除地址一个 `session_scope()`**：
锁租户行（`FOR SHARE`，读 `account_status`）→ 锁项目行 → 判定 → 改地址
→ 按主键 UPDATE 该项目的投递行（游标、租约、失败计数清空，`next_attempt_at = now`）
→ `PROJECT_STATUS_WEBHOOK_SET` 审计 → 提交。
清空 `lease_token` 是为了作废发往旧地址的在途租约：之后的结算令牌对不上，
什么都不写（设计 §2「事务边界」）。

⚠️ **加锁顺序**（设计 §4）：跃迁是租户 → 项目 → 投递行；
改地址是租户（共享）→ 项目 → 投递行。没有任何路径先锁项目或投递行、再锁租户，
与记账（钱包 → 租户 → 投递行）、账户状态（租户 → 投递行）、
启用 webhook 密钥（项目 → 投递行）都不成环。这里不读、不锁、不写钱包。

⚠️ **归属只来自路径**：客户按 `public_id`、项目按「客户 + 项目 public_id」取；
别人的项目与不存在的一样 404（INV-8）。

⚠️ 这里**不写日志**。地址的路径里可能带集成方的令牌：
完整地址只存在 `projects.status_webhook_url`，审计只记主机名，
异常消息里一个字符都不带（设计 §6）。管理员的原因只进审计的 `reason`。
"""

from __future__ import annotations

import datetime as dt
import ipaddress
import json
import re
from dataclasses import dataclass
from typing import Final
from urllib.parse import urlsplit

from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from app.core.database import session_scope
from app.core.errors import AppError
from app.models.auth import AuditAction, DomainOutbox, OutboxStatus, User
from app.models.integration import ProjectStatusDelivery
from app.models.tenancy import (
    STATUS_WEBHOOK_URL_LENGTH,
    AccountStatus,
    IntegrationStatus,
    Project,
    Tenant,
    compose_effective_status,
)
from app.repositories import tenancy
from app.schemas.customers import ProjectView, project_view
from app.services.auth import RequestContext, record_audit, utc_now
from app.services.customers import CustomerNotFound
from app.services.integration_access import ProjectNotFound

# 出站事件（设计 §2「`status_version` 递增点收拢」）。
# ⚠️ 这个字符串是将来投递任务选处理器的键，改名等于让已经躺在 outbox 里的行认不出自己。
EVENT_INTEGRATION_STATUS_CHANGED: Final = "project.integration_status_changed"
AGGREGATE_PROJECT: Final = "project"
# 审计的 entity_type 定义在本模块：审计归类的扫描测试只认本模块的字符串常量。
ENTITY_PROJECT: Final = "project"

# 原因：去首尾空白后 1–255，与审计 `reason` 列同宽（设计 §2「接口」）。
REASON_MAX_LENGTH: Final = 255

# 地址规范化（设计 §2「URL 规范化与校验」）。
_HTTPS: Final = "https"
_HTTPS_PORT: Final = "443"
_HOST_MAX_LENGTH: Final = 253
# IDNA 编码之后每个点分段的 LDH 形式：小写字母、数字、连字符，
# 不以连字符开头或结尾，长 1–63。
_LDH_LABEL: Final = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
# 每段都是十进制数字或 `0x` 十六进制：`127.1`、`2130706433`、`0x7f.1` 这类简写。
_NUMERIC_LABEL: Final = re.compile(r"[0-9]+|0x[0-9a-f]*")


class IntegrationReasonInvalid(AppError):
    """The reason is empty after trimming, or longer than 255 characters."""

    def __init__(self) -> None:
        super().__init__(
            "The reason must be 1-255 characters after trimming.",
            code="VALIDATION_ERROR",
            http_status=422,
        )


class StatusWebhookUrlInvalid(AppError):
    """The address fails normalization. The message never echoes the input (design §6)."""

    def __init__(self) -> None:
        super().__init__(
            "The status webhook URL is not a valid https URL.",
            code="VALIDATION_ERROR",
            http_status=422,
        )


class AccountClosed(AppError):
    """The customer is CLOSED: no integration change, no new webhook address (design §4)."""

    def __init__(self) -> None:
        super().__init__("The customer account is closed.", code="ACCOUNT_CLOSED", http_status=409)


@dataclass(frozen=True)
class StatusWebhookView:
    """The `data` of PUT / GET status-webhook: the normalized address, or `None`."""

    status_webhook_url: str | None
    status_webhook_configured: bool


def _now() -> dt.datetime:
    """`utc_now()` truncated to whole seconds, as in app/services/customers.py."""
    return utc_now().replace(microsecond=0)


# --- 地址规范化 ---------------------------------------------------------------------


def _normalize(raw: str) -> tuple[str, str]:
    """(规范化后的地址, 规范化主机名)；任何一步不合规都是 `StatusWebhookUrlInvalid`。"""
    # 0. 拆分之前查原始输入：`urlsplit` 会悄悄去掉制表符与换行。
    if any(ord(char) <= 0x20 or ord(char) == 0x7F for char in raw):
        raise StatusWebhookUrlInvalid
    # 用户信息与 fragment（含空的）一律拒绝。
    if "@" in raw or "#" in raw:
        raise StatusWebhookUrlInvalid
    try:
        parts = urlsplit(raw)
    except ValueError:
        raise StatusWebhookUrlInvalid from None
    # 1. scheme（`urlsplit` 已转小写）。
    if parts.scheme.lower() != _HTTPS:
        raise StatusWebhookUrlInvalid
    netloc = parts.netloc
    # 3 的第一种：带方括号的 IPv6 字面量。
    if "[" in netloc or "]" in netloc:
        raise StatusWebhookUrlInvalid
    host = netloc
    if ":" in netloc:
        host, _colon, port = netloc.rpartition(":")
        # 冒号后面没有端口（`https://h:/x`）与非 443 端口都拒绝。
        if port != _HTTPS_PORT:
            raise StatusWebhookUrlInvalid
    # 2. 主机名：去掉末尾一个点、转小写、IDNA 编码。
    if host.endswith("."):
        host = host[:-1]
    try:
        ascii_host = host.lower().encode("idna").decode("ascii").lower()
    except UnicodeError:
        raise StatusWebhookUrlInvalid from None
    labels = ascii_host.split(".")
    if not ascii_host or len(ascii_host) > _HOST_MAX_LENGTH:
        raise StatusWebhookUrlInvalid
    if not all(_LDH_LABEL.fullmatch(label) for label in labels):
        raise StatusWebhookUrlInvalid
    # 3. IP 字面量（含各种简写）一律拒绝。
    if _is_ip_literal(ascii_host, labels):
        raise StatusWebhookUrlInvalid
    # 4. 路径与查询串：只允许 0x21–0x7E，不做百分号编码的规范化。
    path = parts.path or "/"
    query = parts.query
    if not all(0x21 <= ord(char) <= 0x7E for char in path + query):
        raise StatusWebhookUrlInvalid
    # 5. 重新拼出（端口 443 省略），总长 ≤ 2048。
    url = f"{_HTTPS}://{ascii_host}{path}"
    if query:
        url = f"{url}?{query}"
    if len(url) > STATUS_WEBHOOK_URL_LENGTH:
        raise StatusWebhookUrlInvalid
    return url, ascii_host


def _is_ip_literal(host: str, labels: list[str]) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return all(_NUMERIC_LABEL.fullmatch(label) for label in labels)
    return True


def normalize_status_webhook_url(raw: str) -> str:
    """The stored form of a status webhook address (design §2「URL 规范化与校验」).

    `https://<规范化主机名><path>[?query]`：scheme 与主机名小写，主机名去掉末尾一个点
    并做 IDNA 编码，端口 443 省略，空路径取 `/`，空查询串去掉；路径与查询串按原样保留。
    不合规：`StatusWebhookUrlInvalid`（422），消息里没有输入的任何部分。
    """
    url, _host = _normalize(raw)
    return url


def _host_of(url: str | None) -> str | None:
    """The host of a stored (already normalized) address: the only part the audit keeps."""
    if url is None:
        return None
    return urlsplit(url).hostname


def _view(project: Project) -> StatusWebhookView:
    url = project.status_webhook_url
    return StatusWebhookView(status_webhook_url=url, status_webhook_configured=url is not None)


# --- 查找与加锁 ---------------------------------------------------------------------


def _lock_tenant_shared(session: Session, customer_id: str) -> Tenant:
    """`SELECT … FOR SHARE` on the tenant row (MySQL; SQLite ignores it)."""
    statement = (
        select(Tenant)
        .where(Tenant.public_id == customer_id)
        .with_for_update(read=True)
        .execution_options(populate_existing=True)
    )
    tenant = session.execute(statement).scalar_one_or_none()
    if tenant is None:
        raise CustomerNotFound
    return tenant


def _lock_tenant(session: Session, customer_id: str) -> Tenant:
    tenant = tenancy.get_tenant_by_public_id(session, customer_id, for_update=True)
    if tenant is None:
        raise CustomerNotFound
    return tenant


def _lock_project(session: Session, tenant: Tenant, project_id: str) -> Project:
    """`SELECT … FOR UPDATE` on the project row, only under this tenant (INV-8)."""
    statement = (
        select(Project)
        .where(Project.tenant_id == tenant.id, Project.public_id == project_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    project = session.execute(statement).scalar_one_or_none()
    if project is None:
        raise ProjectNotFound
    return project


def _resolve_project(session: Session, customer_id: str, project_id: str) -> Project:
    tenant = tenancy.get_tenant_by_public_id(session, customer_id)
    if tenant is None:
        raise CustomerNotFound
    project = tenancy.get_project_for_tenant(session, tenant.id, project_id)
    if project is None:
        raise ProjectNotFound
    return project


# --- 项目集成状态 -------------------------------------------------------------------


def _checked_reason(reason: str) -> str:
    trimmed = reason.strip()
    if not 1 <= len(trimmed) <= REASON_MAX_LENGTH:
        raise IntegrationReasonInvalid
    return trimmed


def change_integration_status(
    session_factory: sessionmaker[Session],
    *,
    actor: User,
    customer_id: str,
    project_id: str,
    integration_status: IntegrationStatus,
    reason: str,
    context: RequestContext,
    now: dt.datetime | None = None,
) -> ProjectView:
    """Move the project's integration status; return the project after the commit.

    判定顺序（设计 §4）：原因（422）→ 客户与项目（404）
    → 租户 `CLOSED`（409，即使目标等于当前）
    → 目标等于当前（什么都不写：不加版本、不写 outbox 与审计）→ 跃迁。
    """
    trimmed = _checked_reason(reason)
    moment = now or _now()
    with session_scope(session_factory) as session:
        # ⚠️ 先租户、后项目（设计 §2「事务边界」）。
        tenant = _lock_tenant(session, customer_id)
        project = _lock_project(session, tenant, project_id)
        if tenant.account_status is AccountStatus.CLOSED:
            raise AccountClosed
        if integration_status != project.integration_status:
            _apply(
                session,
                tenant,
                project,
                integration_status,
                reason=trimmed,
                actor=actor,
                context=context,
                now=moment,
            )
        view = project_view(project)
    return view


def _apply(
    session: Session,
    tenant: Tenant,
    project: Project,
    target: IntegrationStatus,
    *,
    reason: str,
    actor: User,
    context: RequestContext,
    now: dt.datetime,
) -> None:
    previous = project.integration_status
    before_state = {
        "integration_status": previous.value,
        "status_version": tenant.status_version,
    }

    def make_event(locked: Tenant) -> DomainOutbox:
        # bump 在版本 +1 之后才调用：payload 里一定是新版本。
        # 按名字取 `_integration_event`，测试对它的替换照常生效。
        effective, reason_code = compose_effective_status(
            locked.account_status,
            locked.billing_status,
            locked.billing_mode,
            target,
        )
        payload = {
            "project_id": project.public_id,
            "integration_status": target.value,
            "previous_integration_status": previous.value,
            "effective_status": effective.value,
            "reason_code": reason_code.value if reason_code is not None else None,
            "status_version": locked.status_version,
            "changed_at": now.isoformat(),
        }
        return _integration_event(project, payload, now)

    project.integration_status = target
    project.updated_at = now
    # 锁内刚读到的值 + 1（调用方已锁租户行）。
    tenancy.bump_status_version(session, tenant, now, make_event=make_event)
    record_audit(
        session,
        action=AuditAction.PROJECT_INTEGRATION_STATUS_CHANGED,
        context=context,
        now=now,
        actor=actor,
        entity_type=ENTITY_PROJECT,
        entity_id=project.public_id,
        # ⚠️ 字段白名单（设计 §2「事务边界」）：只有这两项。
        before_state=before_state,
        after_state={
            "integration_status": target.value,
            "status_version": tenant.status_version,
        },
        reason=reason,
    )


def _integration_event(
    project: Project, payload: dict[str, object], now: dt.datetime
) -> DomainOutbox:
    # ⚠️ payload 里没有原因（设计 §2）。以 PENDING 持久保存：app/tasks/outbox.py 的
    # recover 只重投有处理器的类型，这一类接上处理器之前不会被重试成死信（INV-14）。
    return DomainOutbox(
        event_type=EVENT_INTEGRATION_STATUS_CHANGED,
        aggregate_type=AGGREGATE_PROJECT,
        aggregate_id=project.public_id,
        payload_json=json.dumps(payload, sort_keys=True),
        status=OutboxStatus.PENDING,
        attempt_count=0,
        next_retry_at=now,
        created_at=now,
    )


# --- 状态 webhook 地址 --------------------------------------------------------------


def set_status_webhook(
    session_factory: sessionmaker[Session],
    *,
    actor: User,
    customer_id: str,
    project_id: str,
    url: str | None,
    context: RequestContext,
    now: dt.datetime | None = None,
) -> StatusWebhookView:
    """Set (`url` is a string) or clear (`None`) the project's status webhook address.

    判定顺序（设计 §2「接口」）：规范化与校验（422）→ 客户与项目（404）
    → 设为地址而租户 `CLOSED`（409；清除不受此限，关户的客户也能停止推送）
    → 规范化后与当前值逐字节相同（含两次都是 `None`）：什么都不写，
    不重置游标、不写审计 → 写入。
    """
    normalized: str | None = None
    if url is not None:
        normalized = normalize_status_webhook_url(url)
    moment = now or _now()
    with session_scope(session_factory) as session:
        # ⚠️ 租户共享锁 → 项目行锁 → 投递行（设计 §2「事务边界」）。
        tenant = _lock_tenant_shared(session, customer_id)
        project = _lock_project(session, tenant, project_id)
        if normalized is not None and tenant.account_status is AccountStatus.CLOSED:
            raise AccountClosed
        previous = project.status_webhook_url
        if normalized != previous:
            project.status_webhook_url = normalized
            project.updated_at = moment
            session.flush()
            # 每个项目恰好一行投递行（AIH-TASK-047）：按主键 UPDATE，不插入。
            session.execute(
                update(ProjectStatusDelivery)
                .where(ProjectStatusDelivery.project_id == project.id)
                .values(
                    delivered_version=None,
                    attempted_version=None,
                    lease_token=None,
                    lease_expires_at=None,
                    attempt_count=0,
                    next_attempt_at=moment,
                )
            )
            record_audit(
                session,
                action=AuditAction.PROJECT_STATUS_WEBHOOK_SET,
                context=context,
                now=moment,
                actor=actor,
                entity_type=ENTITY_PROJECT,
                entity_id=project.public_id,
                # ⚠️ 只有主机名：路径里可能带集成方的令牌（设计 §2「接口」、§6）。
                before_state={"status_webhook_host": _host_of(previous)},
                after_state={"status_webhook_host": _host_of(normalized)},
            )
        view = _view(project)
    return view


def get_status_webhook(
    session_factory: sessionmaker[Session], *, customer_id: str, project_id: str
) -> StatusWebhookView:
    """The project's address as stored, for GET status-webhook. Read-only, no audit."""
    with session_factory() as session:
        project = _resolve_project(session, customer_id, project_id)
        return _view(project)


__all__ = [
    "EVENT_INTEGRATION_STATUS_CHANGED",
    "AccountClosed",
    "IntegrationReasonInvalid",
    "StatusWebhookUrlInvalid",
    "StatusWebhookView",
    "change_integration_status",
    "get_status_webhook",
    "normalize_status_webhook_url",
    "set_status_webhook",
]

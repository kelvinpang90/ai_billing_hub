"""Admin-controlled tenant account status (spec §24, §25, §56, §66; design gate #136 v2).

⚠️ **一次跃迁一个 `session_scope()`**（INV-13）：锁租户行 → 判定 → 改 `account_status`、
`status_version` +1、`updated_at` → `TENANT_ACCOUNT_STATUS_CHANGED` 审计 →
`tenant.account_status_changed` outbox → 提交。任何一步失败整体回滚。

⚠️ **只锁租户行**（`SELECT … FOR UPDATE`），不读、不锁、不写钱包（设计 §2「事务边界」）。
记账路径的加锁顺序是钱包 → 租户；这里只拿租户一把锁，不会与它成环。两条路径都在
租户行锁内给 `status_version` +1，所以两维的跃迁交错时版本号仍严格递增、不重复。

⚠️ **客户只来自路径里的 `public_id`**（INV-8），查不到就是 `CUSTOMER_NOT_FOUND`。

⚠️ 这里**不写日志**。原因是管理员的自由文本，只进审计的 `reason`，不进 outbox payload
（payload 以后会被转成对外的 webhook），也不进异常消息（设计 §6）。
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Final

from sqlalchemy.orm import Session, sessionmaker

from app.core.database import session_scope
from app.core.errors import AppError
from app.models.auth import AuditAction, DomainOutbox, OutboxStatus, User
from app.models.tenancy import AccountStatus, Tenant, ai_service_enabled
from app.repositories import tenancy
from app.schemas.customers import CustomerDetail
from app.services import customers
from app.services.auth import RequestContext, record_audit, utc_now
from app.services.customers import CustomerNotFound

# 出站事件（设计 §2「外部系统与异步边界」）。⚠️ 这个字符串是将来投递任务选处理器的键，
# 改名等于让已经躺在 domain_outbox 里的行认不出自己。
EVENT_ACCOUNT_STATUS_CHANGED: Final = "tenant.account_status_changed"
AGGREGATE_TENANT: Final = "tenant"
ENTITY_TENANT: Final = "tenant"

# 跃迁表（设计 §4）：当前状态 → 合法的目标。目标等于当前状态不在表里查，200 且不写。
# ⚠️ `CLOSED` 是终态，一格都没有：本任务不产生它，这一行保证关户任务上线后本接口
# 也不会把已关的户重开。`→ PENDING_ACTIVATION` 与 `→ CLOSED` 在请求校验层就是 422。
TRANSITIONS: Final[dict[AccountStatus, frozenset[AccountStatus]]] = {
    AccountStatus.PENDING_ACTIVATION: frozenset({AccountStatus.ENABLED, AccountStatus.DISABLED}),
    AccountStatus.ENABLED: frozenset({AccountStatus.DISABLED}),
    AccountStatus.DISABLED: frozenset({AccountStatus.ENABLED}),
    AccountStatus.CLOSED: frozenset(),
}


class AccountStatusTransitionInvalid(AppError):
    """The transition table has no cell for current → target (design §4)."""

    def __init__(self) -> None:
        super().__init__(
            "The account status cannot change from its current value to this one.",
            code="ACCOUNT_STATUS_TRANSITION_INVALID",
            http_status=409,
        )


def _now() -> dt.datetime:
    """`utc_now()` truncated to whole seconds, as in app/services/customers.py."""
    return utc_now().replace(microsecond=0)


def transition(current: AccountStatus, target: AccountStatus) -> AccountStatus | None:
    """The new status, or `None` when the target is the current status (nothing to write).

    表里没有这一格：`AccountStatusTransitionInvalid`。
    """
    if target == current:
        return None
    if target not in TRANSITIONS[current]:
        raise AccountStatusTransitionInvalid
    return target


def change_account_status(
    session_factory: sessionmaker[Session],
    *,
    actor: User,
    customer_id: str,
    account_status: AccountStatus,
    reason: str,
    context: RequestContext,
    now: dt.datetime | None = None,
) -> CustomerDetail:
    """Move the customer's account status; return the customer detail after the commit.

    目标等于当前状态：什么都不写（不写审计、不加版本、不写 outbox），返回当前详情。
    计费状态不受影响。
    """
    moment = now or _now()
    with session_scope(session_factory) as session:
        tenant = tenancy.get_tenant_by_public_id(session, customer_id, for_update=True)
        if tenant is None:
            raise CustomerNotFound
        _apply(
            session,
            tenant,
            account_status,
            reason=reason,
            actor=actor,
            context=context,
            now=moment,
        )
    # 事务之外、提交之后再读详情：本事务不碰钱包（设计 §2），详情里的钱包由这次只读取得。
    return customers.get_customer(session_factory, customer_id)


def _apply(
    session: Session,
    tenant: Tenant,
    target: AccountStatus,
    *,
    reason: str,
    actor: User,
    context: RequestContext,
    now: dt.datetime,
) -> None:
    previous = tenant.account_status
    wanted = transition(previous, target)
    if wanted is None:
        return
    before_state = {
        "account_status": previous.value,
        "status_version": tenant.status_version,
    }
    # 锁内刚读到的值 + 1。
    tenant.account_status = wanted
    tenant.status_version = tenant.status_version + 1
    tenant.updated_at = now
    record_audit(
        session,
        action=AuditAction.TENANT_ACCOUNT_STATUS_CHANGED,
        context=context,
        now=now,
        actor=actor,
        entity_type=ENTITY_TENANT,
        entity_id=tenant.public_id,
        # ⚠️ 字段白名单（设计 §2「审计」）：只有这两项，没有联系人、邮箱、电话。
        before_state=before_state,
        after_state={
            "account_status": wanted.value,
            "status_version": tenant.status_version,
        },
        reason=reason,
    )
    payload = {
        "account_status": wanted.value,
        "previous_account_status": previous.value,
        "billing_status": tenant.billing_status.value,
        "billing_mode": tenant.billing_mode.value,
        "effective_status": "ALLOW_AI" if ai_service_enabled(tenant) else "BLOCK_AI",
        "status_version": tenant.status_version,
        "changed_at": now.isoformat(),
    }
    session.add(_status_event(tenant, payload, now))


def _status_event(tenant: Tenant, payload: dict[str, object], now: dt.datetime) -> DomainOutbox:
    # ⚠️ payload 里没有原因、没有个人数据（设计 §2、§6）。以 PENDING 持久保存：
    # app/tasks/outbox.py 的 recover 只重投有处理器的类型，这一类在接上投递之前
    # 不会被重试成死信（INV-14）。
    return DomainOutbox(
        event_type=EVENT_ACCOUNT_STATUS_CHANGED,
        aggregate_type=AGGREGATE_TENANT,
        aggregate_id=tenant.public_id,
        payload_json=json.dumps(payload, sort_keys=True),
        status=OutboxStatus.PENDING,
        attempt_count=0,
        next_retry_at=now,
        created_at=now,
    )


__all__ = [
    "EVENT_ACCOUNT_STATUS_CHANGED",
    "TRANSITIONS",
    "AccountStatusTransitionInvalid",
    "change_account_status",
    "transition",
]

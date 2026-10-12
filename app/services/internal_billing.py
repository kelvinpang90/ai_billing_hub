"""Audited, one-way provisioning of an internal metered-only tenant.

⚠️ 计费模式的跃迁与 `status_version` +1、`status_changed_at`、`tenant.billing_mode_changed`
outbox、审计同一事务（INV-13；设计闸门 #241 v6 §2「`status_version` 递增点收拢」）。
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Final

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.core.database import session_scope
from app.core.errors import AppError
from app.models.auth import AuditAction, DomainOutbox, OutboxStatus, User, UserRole
from app.models.integration import IntegrationCredential
from app.models.tenancy import BillingMode, IntegrationStatus, Tenant, compose_effective_status
from app.models.usage import UsageEvent
from app.models.wallet import Wallet
from app.repositories.tenancy import bump_status_version, get_tenant_by_public_id
from app.schemas.customers import CustomerDetail, customer_detail
from app.services.auth import RequestContext, record_audit, utc_now
from app.services.customers import CustomerNotFound

# 出站事件（设计 #241 §2）。⚠️ 这个字符串是将来投递任务选处理器的键，改名等于让
# 已经躺在 domain_outbox 里的行认不出自己。
EVENT_BILLING_MODE_CHANGED: Final = "tenant.billing_mode_changed"
AGGREGATE_TENANT: Final = "tenant"


class InternalMeteringNotEligible(AppError):
    def __init__(self) -> None:
        super().__init__(
            "Internal metering must be enabled before wallet or usage activity.",
            code="INTERNAL_METERING_NOT_ELIGIBLE",
            http_status=409,
        )


def enable_internal_metering(
    factory: sessionmaker[Session],
    *,
    actor: User,
    customer_id: str,
    reason: str,
    context: RequestContext,
    now: dt.datetime | None = None,
) -> CustomerDetail:
    """Convert one newly provisioned tenant before any billable history exists."""
    if actor.role is not UserRole.ADMIN:
        raise PermissionError("admin required")
    moment = (now or utc_now()).replace(microsecond=0)
    with session_scope(factory) as session:
        found = get_tenant_by_public_id(session, customer_id)
        if found is None:
            raise CustomerNotFound
        # Match post_transaction's lock order: wallet, then tenant. A concurrent
        # top-up cannot slip between our eligibility check and the mode change.
        wallet = session.execute(
            select(Wallet)
            .where(Wallet.tenant_id == found.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).scalar_one_or_none()
        if wallet is None:
            raise RuntimeError("WALLET_MISSING")
        tenant = session.execute(
            select(Tenant)
            .where(Tenant.id == found.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).scalar_one()
        if tenant.billing_mode is BillingMode.INTERNAL_METERED_ONLY:
            return customer_detail(tenant, wallet)
        existing_usage = session.execute(
            select(UsageEvent.id)
            .where(UsageEvent.tenant_id == tenant.id)
            .limit(1)
            .with_for_update()
        ).first()
        existing_credential = session.execute(
            select(IntegrationCredential.id)
            .where(IntegrationCredential.tenant_id == tenant.id)
            .limit(1)
            .with_for_update()
        ).first()
        if (
            existing_usage is not None
            or existing_credential is not None
            or wallet.version != 0
            or wallet.balance != 0
        ):
            raise InternalMeteringNotEligible
        previous = tenant.billing_mode

        def make_event(locked: Tenant) -> DomainOutbox:
            # bump 在版本 +1 之后才调用：payload 里一定是新版本。按名字取 `_mode_event`。
            effective, _reason = compose_effective_status(
                locked.account_status,
                locked.billing_status,
                locked.billing_mode,
                IntegrationStatus.ENABLED,
            )
            payload = {
                "billing_mode": locked.billing_mode.value,
                "previous_billing_mode": previous.value,
                "effective_status": effective.value,
                "status_version": locked.status_version,
                "changed_at": moment.isoformat(),
            }
            return _mode_event(locked, payload, moment)

        # 锁内刚读到的值 + 1（钱包 → 租户的锁都已拿到）。
        tenant.billing_mode = BillingMode.INTERNAL_METERED_ONLY
        bump_status_version(session, tenant, moment, make_event=make_event)
        tenant.updated_at = moment
        record_audit(
            session,
            action=AuditAction.INTERNAL_BILLING_MODE_SET,
            context=context,
            now=moment,
            actor=actor,
            entity_type="tenant",
            entity_id=tenant.public_id,
            before_state={"billing_mode": BillingMode.PREPAID.value},
            after_state={"billing_mode": BillingMode.INTERNAL_METERED_ONLY.value},
            reason=reason,
        )
        return customer_detail(tenant, wallet)


def _mode_event(tenant: Tenant, payload: dict[str, object], now: dt.datetime) -> DomainOutbox:
    # ⚠️ payload 里没有原因、没有个人数据（设计 #241 §2）。以 PENDING 持久保存：
    # app/tasks/outbox.py 的 recover 只重投有处理器的类型，这一类在接上投递之前
    # 不会被重试成死信（INV-14）。
    return DomainOutbox(
        event_type=EVENT_BILLING_MODE_CHANGED,
        aggregate_type=AGGREGATE_TENANT,
        aggregate_id=tenant.public_id,
        payload_json=json.dumps(payload, sort_keys=True),
        status=OutboxStatus.PENDING,
        attempt_count=0,
        next_retry_at=now,
        created_at=now,
    )

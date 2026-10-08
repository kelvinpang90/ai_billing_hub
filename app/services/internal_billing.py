"""Audited, one-way provisioning of an internal metered-only tenant."""

from __future__ import annotations

import datetime as dt

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.core.database import session_scope
from app.core.errors import AppError
from app.models.auth import AuditAction, User, UserRole
from app.models.integration import IntegrationCredential
from app.models.tenancy import BillingMode, Tenant
from app.models.usage import UsageEvent
from app.models.wallet import Wallet
from app.repositories.tenancy import get_tenant_by_public_id
from app.schemas.customers import CustomerDetail, customer_detail
from app.services.auth import RequestContext, record_audit, utc_now
from app.services.customers import CustomerNotFound


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
        tenant.billing_mode = BillingMode.INTERNAL_METERED_ONLY
        tenant.status_version += 1
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

"""Internal metered-only tenants and immutable reference-price snapshots.

Existing tenants remain PREPAID. Existing processed events are backfilled as PREPAID;
their financial snapshots and ledger rows are unchanged. No tenant is privileged by
this migration. Assigning the internal mode is a separate audited operation.

Revision ID: 0019_internal_metering
Revises: 0018_usage_billing
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0019_internal_metering"
down_revision: str | None = "0018_usage_billing"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_EVENT_CHECKS = {
    "ck_usage_events_mode_values": (
        "billing_mode_snapshot IS NULL OR billing_mode_snapshot IN"
        " ('PREPAID', 'INTERNAL_METERED_ONLY')"
    ),
    "ck_usage_events_reference_price": (
        "reference_customer_price IS NULL OR reference_customer_price >= 0"
    ),
    "ck_usage_events_mode_snapshot": ("status <> 'PROCESSED' OR billing_mode_snapshot IS NOT NULL"),
    "ck_usage_events_internal_mode": (
        "billing_mode_snapshot <> 'INTERNAL_METERED_ONLY' OR status <> 'PROCESSED'"
        " OR (billable_cost = 0 AND wallet_transaction_id IS NULL"
        " AND reference_customer_price IS NOT NULL)"
    ),
    "ck_usage_events_prepaid_reference": (
        "billing_mode_snapshot <> 'PREPAID' OR reference_customer_price IS NULL"
    ),
}


def upgrade() -> None:
    op.add_column(
        "tenants",
        sa.Column("billing_mode", sa.String(32), nullable=False, server_default="PREPAID"),
    )
    op.create_check_constraint(
        "ck_tenants_billing_mode", "tenants", "billing_mode IN ('PREPAID', 'INTERNAL_METERED_ONLY')"
    )
    # Existing PROCESSED rows cannot be UPDATEd: migration 0018 installed an immutable
    # row trigger. A temporary column default backfills them without firing that trigger;
    # reset the default so future RECEIVED rows get a snapshot only when processed.
    # ⚠️ SET DEFAULT NULL, not DROP DEFAULT: under strict mode a column with no default
    # makes every ingest INSERT that omits it fail with 1364.
    op.add_column(
        "usage_events",
        sa.Column("billing_mode_snapshot", sa.String(32), nullable=True, server_default="PREPAID"),
    )
    op.alter_column(
        "usage_events",
        "billing_mode_snapshot",
        server_default=sa.text("NULL"),
        existing_type=sa.String(32),
        existing_nullable=True,
    )
    op.add_column(
        "usage_events", sa.Column("reference_customer_price", sa.Numeric(20, 8), nullable=True)
    )
    # All historical events belonged to PREPAID tenants; a future processing attempt
    # overwrites the provisional value with the actual mode observed in its transaction.
    for name, condition in _EVENT_CHECKS.items():
        op.create_check_constraint(name, "usage_events", condition)


def downgrade() -> None:
    bind = op.get_bind()
    if not op.get_context().as_sql:
        internal_tenant = bind.execute(
            sa.text("SELECT 1 FROM tenants WHERE billing_mode = 'INTERNAL_METERED_ONLY' LIMIT 1")
        ).first()
        internal_event = bind.execute(
            sa.text(
                "SELECT 1 FROM usage_events"
                " WHERE billing_mode_snapshot = 'INTERNAL_METERED_ONLY' LIMIT 1"
            )
        ).first()
        if internal_tenant or internal_event:
            raise RuntimeError(
                "Cannot downgrade internal metering after an internal tenant or event exists"
            )
    for name in reversed(_EVENT_CHECKS):
        op.drop_constraint(name, "usage_events", type_="check")
    op.drop_column("usage_events", "reference_customer_price")
    op.drop_column("usage_events", "billing_mode_snapshot")
    op.drop_constraint("ck_tenants_billing_mode", "tenants", type_="check")
    op.drop_column("tenants", "billing_mode")

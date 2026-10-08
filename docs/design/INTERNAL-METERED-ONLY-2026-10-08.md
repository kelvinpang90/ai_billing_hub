# Internal metered-only tenant addendum

Approved by the product owner on 2026-10-08. This addendum narrows the prepaid-only rules in spec §§7 and 24 and the usage billing design for **Acuven's own tenant only**. Existing and newly created tenants remain `PREPAID` unless an administrator explicitly provisions the internal mode.

## Mode and accounting

`tenants.billing_mode` is `PREPAID` or `INTERNAL_METERED_ONLY`. The latter uses the same catalog, price versions, FX and pricing engine, then records `estimated_provider_cost_myr` and `reference_customer_price` on each processed event. `billable_cost` is the actual wallet charge and is zero for this mode. No `AI_USAGE` ledger row is created. The reference price is a hypothetical customer charge, not revenue, receivables or gross margin. Costs remain **estimates** until separately reconciled against a supplier invoice.

Each processed event snapshots `billing_mode_snapshot`. A pricing failure retains its normal error status and no zero-cost success snapshot. Existing historical events are assigned `PREPAID` by migration 0019; the migration does not update immutable processed rows. The processing transaction observes the current tenant mode and overwrites any provisional snapshot value for newly processed events.

## Access and provisioning

Wallet balance still determines `billing_status`. Effective AI access is allowed when `account_status = ENABLED` and (`billing_mode = INTERNAL_METERED_ONLY` or `billing_status = ACTIVE`). A disabled or closed account remains blocked. Active integration credentials, capability authorization and other safety controls still apply.

`POST /api/v1/admin/customers/{customer_id}/internal-metering` requires ADMIN and a nonblank audit reason. It is one-way and only succeeds before credentials, usage or wallet history exist; it increments `status_version` and writes `INTERNAL_BILLING_MODE_SET`. It does not infer an owner from email, name or fixed ID. The target public ID must be chosen explicitly. The endpoint is not exposed as a customer self-service control. Existing tenants are never migrated to this mode automatically.

The HMAC-authenticated `GET /api/v1/integration/effective-status` returns a live effective decision, billing mode and `status_version` for the credential's tenant/project. The AI API checks it before each model call. A validated `BLOCK_AI` blocks the call; if the status cannot be read or validated, the call is allowed for every tenant (invariant 1: a central billing failure must not interrupt customer AI service), and usage still queues in the AI API outbox for later delivery. The existing account/billing status outbox payloads also include mode and effective decision; event delivery remains a separate integration task.

## Scope and validation

Migration 0019 adds the mode and reference price fields. Tests cover prepaid debit, internal no-debit pricing, reference price display without a margin, status with zero balance and admin disable, and provisioning restrictions. Provisioning a production owner tenant, issuing its credential and publishing actual model prices require explicit configuration after this change is deployed; no production tenant or secret is embedded here.

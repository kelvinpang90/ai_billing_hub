# Acuven Central AI Billing Platform
## Product Requirements & Technical Implementation Specification

**Version:** 1.2  
**Status:** V1 Development Specification — Revised after architecture and financial review  
**Owner:** Acuven Technology Sdn Bhd  
**Primary Market:** Malaysia  
**Currency:** MYR only  
**System Type:** Multi-tenant AI Usage Metering, Prepaid Wallet, Billing & Payment Platform

## Revision History

| Version | Date | Summary |
| --- | --- | --- |
| 1.0 | 2026-09-09 | Initial V1 product and implementation specification. |
| 1.1 | 2026-09-10 | Clarified currency conversion, pricing data, asynchronous ingestion, idempotency, financial periods, status handling, security, recovery, compliance gates, and production operations. |
| 1.2 | 2026-09-10 | §99 repository visibility changed from private to public to obtain branch protection under GitHub Free. See ADR-0001. No other normative change. |

## Document Navigation

- Product, architecture, and technology: §1–§4
- Wallet, usage, pricing, and financial correctness: §5–§23
- Status propagation and integrated application behavior: §24–§39
- Payments, receipts, statements, and notifications: §40–§50
- Authentication, portals, and administration: §51–§73
- Data model and transaction processing: §74–§87
- APIs, security, operations, and deployment: §88–§100
- Repository structure, integration flow, testing, and delivery: §101–§140

The section number is the stable requirement reference for V1. Critical normative requirements additionally use `REQ-*` identifiers where implementation and test traceability are required.

---

# 1. Project Objective

Build a completely independent Central AI Billing Platform for Acuven Technology Sdn Bhd.

Acuven currently operates multiple tenant-facing AI applications. Each application has an independently deployed backend based on:

- Python
- FastAPI
- MySQL

These Integrated Application Backends currently call AI providers directly.

Current primary provider:

- Anthropic Claude

Other present/future providers include:

- OpenAI
- Google Gemini
- Other LLM providers
- Speech-to-Text
- Text-to-Speech
- Image Generation
- Embedding
- OCR / Document AI
- Other usage-based AI services

The Central Billing Platform must provide one centralized system for:

- AI usage tracking
- Request-level token tracking
- Conversation-level aggregation
- Estimated provider cost calculation and provider invoice reconciliation
- Customer selling price calculation
- Customer prepaid wallet
- Payment Gateway top-up
- Automatic wallet deduction
- Customer suspension/reactivation
- Payment receipts
- Monthly statements
- Admin financial analytics
- Customer usage transparency
- Audit logging
- Pricing management
- Provider price synchronization
- Usage exports

The billing system must remain operationally independent from each tenant-facing AI runtime.

**Critical architecture requirement:**

> Failure of the Central Billing Platform MUST NOT interrupt a customer's AI service.

`REQ-AVAIL-001`: Central Billing network, API, worker, Redis, or database unavailability must not become a synchronous dependency in the end-user AI request path.

Usage reporting and billing must therefore be asynchronous.

---

# 2. Core Architecture Principle

Do NOT embed billing logic separately inside each customer system.

Build one centralized billing platform.

## 2.1 Terminology and Trust Boundary

This specification uses **Integrated Application Backend** for an Acuven-managed application backend that serves one or more tenant projects and calls AI providers. It does not mean software operated by the paying customer.

Integrated Application Backends and the Central Billing Platform are separate trust and failure domains even when both are operated by Acuven. They communicate only through authenticated REST APIs and signed Webhooks. They never share direct database access. Per-project credentials, tenant authorization, HMAC signing, and least-privilege isolation remain mandatory.

```text
                     ACUVEN CENTRAL AI BILLING PLATFORM

┌─────────────────────────────────────────────────────────────┐
│                                                             │
│ Authentication / Tenant Management                          │
│                                                             │
│ AI Usage Metering                                           │
│                                                             │
│ Pricing Engine                                              │
│                                                             │
│ Provider Cost Management                                    │
│                                                             │
│ Wallet & Immutable Ledger                                   │
│                                                             │
│ Payment Gateway                                             │
│                                                             │
│ Customer Status Management                                  │
│                                                             │
│ Notification Service                                        │
│                                                             │
│ Statements / Receipts / Reports                             │
│                                                             │
│ Audit Logs                                                  │
│                                                             │
└──────────────────────────▲──────────────────────────────────┘
                           │
                    Async Billing API
                           │
            ┌──────────────┼──────────────┐
            │              │              │
            ▼              ▼              ▼
     Customer A       Customer B       Customer C
      Backend          Backend          Backend
      FastAPI          FastAPI          FastAPI
      MySQL            MySQL            MySQL
```

Each Integrated Application Backend continues to call the AI provider directly.

Example:

```text
WhatsApp / Website
        ↓
Integrated Application Backend
        ↓
Claude / OpenAI / Gemini
        ↓
AI Response returned to user
        ↓
Usage Event saved into LOCAL OUTBOX
        ↓
Background Worker
        ↓
Central Billing API
        ↓
Pricing + Wallet Deduction
```

The AI response must NOT wait for Billing API completion.

---

# 3. Technology Stack

## 3.1 Central Billing Backend

Use:

- Python
- FastAPI
- SQLAlchemy
- Alembic
- Pydantic
- MySQL
- Redis
- Celery
- Docker
- Docker Compose
- Nginx

All monetary calculations MUST use Python `Decimal`.

Never use binary `float` for monetary calculation.

---

## 3.2 Frontend

Use:

- React
- TypeScript
- Vite
- React Router
- TanStack Query
- Axios
- Ant Design

Frontend must be i18n-ready.

V1 language:

- English only

Future:

- Chinese
- Bahasa Malaysia
- other languages

Do not hardcode UI text directly throughout components.

---

## 3.3 Existing Integrated Application Backend

Current stack:

- Python
- FastAPI
- MySQL
- Anthropic official Python SDK
- thin existing facade at:

```text
app/services/llm.py
```

OpenAI is currently used for speech transcription without the official SDK.

The first pilot is `E:\projects\ai_chatbot_demo`. It already has request-level Anthropic usage observations and a basic `conversation_id`, but those records are not a durable billing outbox and the conversation lifecycle is incomplete.

The pilot currently has:

- no provider-neutral billing usage contract
- no durable billing outbox or Celery delivery worker
- no Central Billing integration credentials or request signing
- no local effective billing status or status reconciliation
- no complete conversation lifecycle with close reasons and configurable idle timeout
- no billable OpenAI transcription usage record

Phase 3 must extend the existing narrow interfaces rather than recreate working usage and conversation behavior from zero.

---

# 4. Multi-Tenant Model

Each business customer is a `Tenant`.

Example:

```text
TENANT_000001
TENANT_000002
TENANT_000003
```

Each tenant can have multiple AI projects.

Example:

```text
TENANT_000001

├── WhatsApp AI
├── Website AI
├── CRM AI
└── ERP AI
```

However:

> One customer uses ONE shared wallet.

All projects under the same tenant deduct from the same MYR wallet.

Example:

```text
Customer A Wallet = RM500

WhatsApp AI usage → deduct wallet
Website AI usage  → deduct same wallet
CRM AI usage      → deduct same wallet
```

---

# 5. Currency

V1 customer-facing currency and wallet currency are:

```text
MYR only
```

Do not implement customer wallets, top-ups, statements, or selling prices in multiple currencies.

Provider source prices may be denominated in currencies other than MYR. V1 must convert provider source cost into MYR through the versioned FX process in §17.1. This provider-cost normalization is not customer-facing multi-currency support.

Application logic must enforce:

```text
currency = MYR
```

`REQ-FIN-001`: Every processed Usage Event must preserve the provider source amount and currency, applied FX rate/version, and resulting MYR estimated provider cost. Historical MYR cost must not change when a later FX rate is published.

---

# 6. Wallet Model

Each customer has one prepaid wallet.

Example:

```text
Customer: ABC Sdn Bhd
Wallet Balance: RM235.728412
```

Internally retain high precision.

Required V1 mechanism:

```sql
DECIMAL(18,6)
```

or greater precision where appropriate.

Customer-facing screens normally display:

```text
RM235.73
```

Request-level detailed usage may show additional decimal places when necessary.

---

# 7. Wallet Rules

V1 wallet rules:

1. Prepaid model.
2. Customer tops up wallet before usage.
3. Credit never expires.
4. Payment Gateway fees are paid by Acuven.
5. If customer pays RM100:
   - Wallet receives RM100.
   - This wallet-credit promise remains unchanged by gateway fees. Any SST treatment, checkout total, and tax snapshot must follow the approved §45.1 policy and must be shown explicitly rather than silently reducing wallet credit.
6. V1 performs no synchronous per-request credit reservation. New AI calls are blocked only by the locally cached effective service status described in §24–§30.
7. Processing and status-propagation delay MAY cause balance to become negative. This is accepted eventual-consistency behavior, not an authorization guarantee.
8. When asynchronous processing causes balance <= 0:
   - process all legitimate pending charges
   - negative balance is allowed as reconciliation result
   - tenant becomes `SUSPENDED`
   - customer must top up
9. After wallet returns above RM0:
   - tenant automatically returns to `ACTIVE`
   - Integrated Application Backend receives reactivation webhook
10. A balance of exactly RM0 remains `SUSPENDED`. The top-up API and UI must calculate and display a minimum recovery amount that both clears negative balance and leaves a positive balance.
11. Low-balance, suspension, and reactivation notifications are emitted only on an actual state transition, not on every balance mutation.

---

# 8. Wallet Ledger

Wallet accounting must use an immutable ledger.

DO NOT directly edit the wallet balance as an administrative operation.

Every wallet balance movement must create a transaction.

Supported transaction types:

```text
TOPUP
AI_USAGE
ADJUSTMENT_CREDIT
ADJUSTMENT_DEBIT
REBILL_CREDIT
REBILL_DEBIT
REFUND_ADJUSTMENT
BONUS
SYSTEM_CORRECTION
```

V1 does not implement Payment Gateway refund automation.

If a refund is manually performed outside the system:

- Admin records an adjustment
- reason is mandatory
- audit log is mandatory

---

# 9. AI Usage Granularity

The source-of-truth billing record is:

> AI Request Level

One `AI Request` means one call to an AI provider.

It is NOT the same as one conversation.

Example:

```text
Conversation CONV_123

Request 1
Request 2
Request 3
Request 4
...
```

For each request record:

- tenant
- project
- conversation
- request
- provider
- model
- usage type
- usage quantity
- input tokens
- output tokens
- cached tokens where applicable
- timestamp
- estimated/reconciled provider cost
- billable cost
- pricing rule
- provider price version
- processing status

---

# 10. Conversation Model

Integrated Application Backends are responsible for generating `conversation_id`.

Central Billing must NOT determine conversation boundaries.

Recommended ID:

```text
conv_<uuid>
```

Conversation lifecycle uses hybrid mode.

A conversation may end through:

### Business event

Examples:

- order completed
- human agent takeover
- user explicitly finishes session
- workflow finished

OR:

### Idle timeout

If no activity occurs for configured duration, close the conversation.

Timeout must be configurable.

Do not hardcode 30 minutes.

Suggested default:

```text
30 minutes
```

The Integrated Application Backend owns conversation session lifecycle.

Central Billing only stores and aggregates the supplied `conversation_id`.

---

# 11. AI Usage Event

Define one generalized `UsageEvent`.

Example:

```json
{
  "schema_version": "1.0",
  "event_id": "evt_01J...",
  "request_id": "req_01J...",
  "conversation_id": "conv_01J...",
  "tenant_id": "tenant_001",
  "project_id": "project_whatsapp",
  "provider": "anthropic",
  "model": "claude-...",
  "usage_type": "LLM_TOKEN",
  "input_tokens": 1850,
  "output_tokens": 420,
  "cache_creation_input_tokens": 0,
  "cache_read_input_tokens": 300,
  "occurred_at": "2026-09-09T08:30:11Z"
}
```

Non-token example:

```json
{
  "schema_version": "1.0",
  "event_id": "evt_01J...",
  "request_id": "req_01J...",
  "conversation_id": "conv_01J...",
  "tenant_id": "tenant_001",
  "project_id": "project_whatsapp",
  "provider": "openai",
  "model": "whisper-1",
  "usage_type": "AUDIO_SECOND",
  "quantity": "37.420",
  "unit": "SECOND",
  "occurred_at": "2026-09-09T08:30:11Z"
}
```

The API contract must define required, optional, and mutually exclusive fields per `usage_type`, including numeric ranges, maximum identifier lengths, timestamp format, and an acceptable future-time skew. Decimal quantities are serialized as JSON strings to prevent binary floating-point ambiguity.

For `LLM_TOKEN`, token counts are non-negative integers. For non-token usage, `quantity` and `unit` are required and token fields are omitted unless that usage type explicitly defines them.

Tenant and project identity are derived from the integration credential and must match any payload values supplied for diagnostics. A mismatch is rejected and audited.

Do NOT send calculated customer cost as authoritative input.

The Integrated Application Backend reports provider usage metadata.

Central Billing calculates money.

---

# 12. Generalized Metering Model

Do not design Billing Engine exclusively around LLM token usage.

Support usage categories such as:

```text
LLM_TOKEN
EMBEDDING_TOKEN
AUDIO_SECOND
AUDIO_MINUTE
TTS_CHARACTER
IMAGE_GENERATION
OCR_PAGE
DOCUMENT_PAGE
CUSTOM
```

V1 UI focuses primarily on LLM billing.

But database and engine must support generalized units from the beginning.

---

# 13. Privacy Requirement

Central Billing MUST NOT store:

- customer prompt
- AI response
- complete WhatsApp message
- complete website conversation content
- sensitive user conversation content

Central Billing stores metadata only.

Allowed examples:

```text
conversation_id
request_id
tenant_id
project_id
provider
model
token counts
usage quantities
costs
timestamps
status
```

This system is a metering and billing service, not a conversation archive.

---

# 14. Provider Cost vs Customer Charge

Always keep the following values separate.

## Estimated Provider Cost

The request-level provider cost estimated from the published provider price version and published FX rate version effective at `occurred_at`.

For every Usage Event preserve:

```text
provider_source_cost
provider_source_currency
fx_rate_version_id
fx_rate_applied
estimated_provider_cost_myr
```

## Reconciled Provider Cost

The cost confirmed from a provider invoice or other authoritative provider billing record. Provider discounts, credits, taxes, contractual rates, or invoice rounding may cause it to differ from the request-level estimate.

Reconciliation must append an auditable record. It must not silently overwrite the original provider price, FX, or estimated-cost snapshot and must not automatically rebill the customer.

If authoritative invoice data can be allocated deterministically to individual requests, store the allocated reconciled amount without replacing the estimate. Otherwise retain reconciliation at provider/billing-period level and report the aggregate variance; never invent per-request precision that the provider invoice does not support.

Until reconciliation is available, dashboards must label the value as estimated. When reconciled data is available, financial reporting uses reconciled cost and discloses the reconciliation coverage period.

## Billable Customer Cost

Amount deducted from the customer's wallet.

Example:

```text
RM0.041200
```

## Gross Margin

Admin-only:

```text
Billable Cost - Provider Cost
```

Provider Cost means reconciled provider cost where available and estimated provider cost otherwise. Reports must expose which basis was used.

Customer MUST NOT see:

- estimated or reconciled provider cost
- markup multiplier
- gross margin
- internal Acuven cost structure

---

# 15. Pricing Engine

Pricing is configurable at:

```text
Customer
+
Provider
+
Model
```

Pricing strategies required in V1:

## MARKUP

Example:

```text
Provider Cost × 2.0
```

For MARKUP, `Provider Cost` means the unrounded MYR estimated provider cost after applying all provider price components and the selected FX rate. Apply the multiplier once, then perform the event-level rounding defined in §80.

Example:

```text
Estimated Provider Cost = RM0.020
Markup = 2.0
Billable = RM0.040
```

## FIXED_RATE

Acuven defines its own selling token rate.

Example:

```text
Input:
RM15 / 1M tokens

Output:
RM60 / 1M tokens
```

Customer price does not directly depend on the provider's current cost.

For FIXED_RATE, calculate each applicable MYR selling-price component, sum the unrounded components, then perform the event-level rounding defined in §80.

## 15.1 Generalized Price Components

Provider prices and FIXED_RATE customer prices must be represented as versioned price components rather than hard-coded model formulas. A component includes:

```text
meter_type
component_code
unit
unit_quantity
rate_amount
rate_currency
```

Required V1 component examples include:

```text
LLM_INPUT_TOKEN
LLM_OUTPUT_TOKEN
LLM_CACHE_WRITE_TOKEN
LLM_CACHE_READ_TOKEN
EMBEDDING_TOKEN
AUDIO_SECOND
AUDIO_MINUTE
TTS_CHARACTER
IMAGE_GENERATION
OCR_PAGE
DOCUMENT_PAGE
CUSTOM
```

Provider-specific cache duration, batch, regional, or other price dimensions must be separate component codes or explicit component attributes. Multipliers such as cache-write or cache-read discounts must never be global constants in the billing engine.

All required components for an event must resolve to a published price. A missing component produces `PRICING_ERROR`; the engine must never silently treat it as zero cost.

---

# 16. Pricing Rule Resolution

Required V1 precedence:

```text
Customer + Provider + Model specific rule
        ↓
Customer + Provider default rule
        ↓
Customer default rule
        ↓
Global Provider + Model rule
        ↓
Global default
```

Do not silently bill if no valid price is resolvable.

If usage cannot be priced:

```text
usage_event.status = PRICING_ERROR
```

Raise Admin alert.

Do NOT lose event.

Pricing rule selection uses the Usage Event `occurred_at`, not ingestion or processing time. Effective intervals are half-open `[effective_from, effective_to)` and published intervals of the same scope and priority must not overlap.

The selected `pricing_rule_id` is permanently retained. Retrying the same event must not select a newly published rule. A deliberate reprocess may select a corrective rule but must retain both old and new references through the rebill audit trail.

---

# 17. Provider Cost Price Management

Provider cost prices must be versioned.

Examples:

```text
Claude Model X Price Version 1
Effective:
2026-01-01 → 2026-08-31

Claude Model X Price Version 2
Effective:
2026-09-01 →
```

Each processed Usage Event must reference:

```text
provider_price_version_id
```

Historical cost must never change merely because current AI provider price changes.

Provider price selection uses `occurred_at`. Each published version must define source currency, effective interval, source/reference, approval metadata, and all required price components. Published versions are immutable; corrections create new versions and use reprocessing where necessary.

## 17.1 Foreign Exchange Rate Management

V1 must implement a pluggable FX-rate provider adapter. The concrete external provider is selected during Phase 0 through documented evaluation of API reliability, licensing, historical coverage, rate semantics, and operational limits.

Flow:

```text
AUTOMATIC FX FETCH
        ↓
DRAFT FX RATE
        ↓
ADMIN REVIEW / APPROVE
        ↓
PUBLISHED FX RATE VERSION
```

Manual entry is mandatory when the external API is unavailable or a historical rate is missing. Automatic retrieval must never silently publish a financial rate without the configured approval workflow.

Required FX version data:

```text
base_currency
quote_currency = MYR
rate
source
source_reference
observed_at
effective_from
effective_to
status
approved_by
approved_at
```

Provider cost conversion uses the published rate version effective at the Usage Event `occurred_at`. Calculation preserves high-precision intermediate values and snapshots the applied rate and final MYR amount on the event.

If no published FX rate is resolvable, preserve the event as `FX_RATE_ERROR`, alert Admin, and do not charge the wallet until reprocessing succeeds.

The FX adapter must support source replacement without changing historical calculation behavior.

---

# 18. Provider Price Synchronization

Architecture must support:

```text
AUTO DISCOVERY / SYNC
        ↓
DRAFT PRICE UPDATE
        ↓
ADMIN REVIEW
        ↓
ADMIN APPROVE
        ↓
PUBLISHED
```

Provider price updates MUST NOT automatically become active without Admin approval.

If fully automated price retrieval is unavailable for a provider, support manual price entry.

---

# 19. Rebill / Reprocess

Admin must be able to reprocess Usage Events.

Use cases:

- wrong provider price
- incorrect pricing rule
- failed billing event
- previously unsupported model
- processing bug

Never modify or delete historical wallet ledger entries.

Correct using compensating ledger transactions.

Example:

Original customer charge:

```text
RM1.50
```

Correct charge:

```text
RM1.20
```

Create:

```text
REBILL_CREDIT +RM0.30
```

Audit trail must store:

- original transaction
- original calculated amount
- corrected amount
- pricing rule
- operator
- reason
- timestamp

For a correction after a monthly statement has been finalized, never mutate the finalized statement. Post the compensating transaction in the current open period as `PRIOR_PERIOD_ADJUSTMENT`, reference the original Usage Event, wallet transaction, and statement period, and display it separately on the next statement.

Reprocessing that changes only estimated/reconciled provider cost does not change the customer's wallet unless the customer pricing rule itself requires a corrected charge and an authorized rebill is performed.

---

# 20. Asynchronous Billing Requirement

This is a mandatory non-functional requirement.

Billing failure MUST NOT block AI response.

There are two distinct asynchronous boundaries:

1. The end-user AI request never waits for Central Billing.
2. Central Billing ingestion durably accepts an event before pricing and wallet mutation occur asynchronously.

Bad architecture:

```text
Customer message
→ Billing API
→ Wait
→ Claude
```

DO NOT implement this.

Correct architecture:

```text
Customer message
        ↓
Integrated Application Backend
        ↓
AI Provider
        ↓
AI Response
        ↓
Persist Usage Event to local Outbox
        ↓
Return response to customer
        ↓
Async delivery to Central Billing
        ↓
Central API authenticates, validates, and stores RECEIVED
        ↓
HTTP 202 Accepted
        ↓
Central worker prices and posts financial effect
```

If the AI Provider has already returned successfully but the Integrated Application Backend cannot persist the local Outbox record, it must still return the AI response. It must emit a highest-severity operational alert containing only allowed billing metadata and create an auditable manual-reconciliation incident when persistence becomes available. V1 explicitly accepts that such a rare local persistence failure may not be automatically recoverable. Central Billing unavailability itself must never cause this condition.

`REQ-INGEST-001`: A `202 Accepted` response means Central Billing has durably taken responsibility for the event. It does not mean pricing or wallet deduction has completed.

`REQ-INGEST-002`: Queue publication alone is never sufficient acknowledgment. The `RECEIVED` database record is the durable source of truth, and workers must be recoverable by scanning database state.

---

# 21. Local Transactional Outbox

Every Integrated Application Backend must implement a local billing outbox.

Recommended table:

```text
billing_outbox
```

Required fields:

```text
id
event_id
request_id
conversation_id
event_type
payload_json
payload_fingerprint
status
attempt_count
next_retry_at
last_attempt_at
last_error
created_at
sent_at
```

Possible statuses:

```text
PENDING
PROCESSING
SENT
FAILED_RETRYABLE
DEAD_LETTER
```

Usage must first be persisted locally.

Only after persistence can background transmission begin.

The local Outbox must define configurable retention for `SENT` events. Retention must be at least as long as the Central Billing disaster-recovery replay window. Deletion or archival must not occur until backup and replay requirements in §98.1 are satisfied.

---

# 22. Outbox Delivery

Use a background Celery worker.

Flow:

```text
AI request completes
        ↓
Usage extracted
        ↓
Outbox INSERT committed
        ↓
AI response unaffected
        ↓
Celery worker reads PENDING event
        ↓
POST Central Billing API
        ↓
HTTP 2xx
        ↓
mark SENT
```

Failure:

```text
POST failed
        ↓
increment attempt_count
        ↓
calculate retry
        ↓
FAILED_RETRYABLE
        ↓
retry later
```

Use exponential backoff + jitter.

Example:

```text
1 min
2 min
5 min
10 min
30 min
1 hr
...
```

Do not aggressively hammer Central Billing.

---

# 23. Exactly-Once Financial Effect

Network delivery is fundamentally at-least-once.

Therefore Central Billing must provide idempotent processing.

Every Usage Event has a globally unique, immutable identifier generated by the Integrated Application Backend:

```text
event_id
```

Central Billing must enforce unique DB index:

```sql
UNIQUE(event_id)
```

Use UUIDv7 or ULID semantics. Database uniqueness is the final concurrency authority; a read-before-insert check alone is insufficient.

On first receipt, Central Billing stores an immutable canonical payload fingerprint together with credential, tenant, and project ownership. A later request using the same `event_id` is a valid retry only when all ownership fields and the canonical payload fingerprint match.

If the same `event_id` is presented for a different tenant/project or with different payload content:

```text
status = IDEMPOTENCY_CONFLICT
HTTP 409
financial effect = none
Admin security/data-integrity alert = required
```

The implementation must use insert-first/upsert semantics or catch the unique constraint conflict, then compare the persisted record. Concurrent duplicate requests must never both create a financial effect.

If an Integrated Application Backend retries the same event 10 times:

> Wallet MUST only be charged once.

Central Billing response for already processed event should be successful/idempotent.

Example:

```json
{
  "status": "already_received",
  "event_id": "evt_..."
}
```

The response must also return the event's current processing status. `already_processed` may be returned only when the persisted event is actually `PROCESSED`.

Do not return an error that causes infinite retries.

---

# 24. Customer Status Model

Status is split into independent dimensions.

Tenant account lifecycle:

```text
PENDING_ACTIVATION
ENABLED
DISABLED
CLOSED
```

Tenant billing status:

```text
ACTIVE
SUSPENDED
```

Project integration status:

```text
ENABLED
DISABLED
```

The effective service status is computed by Central Billing:

```text
ALLOW_AI only when:
tenant.account_status = ENABLED
AND tenant.billing_status = ACTIVE
AND project.integration_status = ENABLED
```

Otherwise the effective status is `BLOCK_AI` with a machine-readable reason code. Top-up may change only `billing_status`; it must never reactivate an administratively disabled or closed tenant/project.

Every effective status transition increments a tenant-scoped monotonic `status_version`. Webhooks and reconciliation responses must carry that version.

---

# 25. Suspension Behavior

Because billing is asynchronous:

Billing may process pending events and discover:

```text
Wallet = -RM12.45
```

Then:

```text
tenant.billing_status = SUSPENDED
```

Within the same database transaction that commits the wallet mutation, Central Billing must:

1. save the billing status and increment `status_version`
2. create the audit record
3. create durable notification and status-webhook Outbox records

Workers deliver notifications and Webhooks after commit. A periodic recovery task must recreate missing delivery tasks from database Outbox state. Only projects whose integration status is `ENABLED` receive delivery.

---

# 26. Integrated Application Backend Suspension Behavior

The Integrated Application Backend keeps the last accepted effective service status and `status_version` locally.

Example:

```text
effective_status = ALLOW_AI
```

When effective status is:

```text
BLOCK_AI
```

new AI requests MUST NOT call any AI provider.

Return generic user-facing message.

Example concept:

```text
The service is temporarily unavailable.
Please try again later.
```

Do NOT expose:

- unpaid wallet
- insufficient credit
- customer debt
- internal billing status

to the tenant's end consumer.

---

# 27. Reactivation

When a suspended customer completes a successful top-up and wallet becomes positive:

```text
SUSPENDED
→ ACTIVE
```

Automatically:

1. update tenant billing status and increment `status_version`
2. create audit log
3. create durable Reactivation Webhook delivery
4. create durable customer notification
5. Integrated Application Backend resumes AI only if the resulting effective status is `ALLOW_AI`

No Acuven manual approval required.

---

# 28. Status Webhook

Central Billing stores per-project:

```text
backend_base_url
status_webhook_url
encrypted_webhook_secret
webhook_key_version
```

Example:

```text
POST https://customer.example.com/internal/billing/status
```

Payload:

```json
{
  "event_id": "status_evt_123",
  "tenant_id": "tenant_123",
  "project_id": "project_123",
  "tenant_account_status": "ENABLED",
  "tenant_billing_status": "SUSPENDED",
  "project_integration_status": "ENABLED",
  "effective_status": "BLOCK_AI",
  "reason_code": "BALANCE_NOT_POSITIVE",
  "status_version": 42,
  "effective_at": "2026-09-09T10:10:00Z"
}
```

Webhook must be signed.

Required:

```text
HMAC-SHA256
```

Headers:

```text
X-Acuven-Timestamp
X-Acuven-Signature
X-Acuven-Event-ID
X-Acuven-Key-Version
```

Status Webhooks use the same canonical HMAC rules, clock-skew checks, encryption, versioning, and constant-time comparison principles as §36–§37, with a separate per-project outbound signing secret.

The Integrated Application Backend validates timestamp and signature, then atomically applies only a `status_version` greater than the local version. The same version is idempotent; an older version is acknowledged and ignored.

---

# 29. Webhook Retry

Webhook failure must retry automatically.

Store delivery record.

Example statuses:

```text
PENDING
DELIVERED
RETRYING
FAILED
```

Use exponential backoff.

Do not permanently lose status transitions.

---

# 30. Periodic Status Reconciliation

Webhook alone is insufficient.

Each Integrated Application Backend must periodically query Central Billing asynchronously.

Example:

```text
every 5 minutes
```

Endpoint:

```http
GET /api/v1/integration/account-status
```

Response:

```json
{
  "tenant_id": "tenant_123",
  "project_id": "project_123",
  "tenant_account_status": "ENABLED",
  "tenant_billing_status": "ACTIVE",
  "project_integration_status": "ENABLED",
  "effective_status": "ALLOW_AI",
  "reason_code": null,
  "status_version": 43,
  "updated_at": "..."
}
```

The backend atomically applies the response only when `status_version` is greater than or equal to the local version. Reconciliation and Webhook processing use the same comparison rule.

This protects against missed Webhooks.

This reconciliation runs in background and MUST NOT block AI traffic.

---

# 31. Outbox Health Monitoring

Monitor billing delivery health.

Metrics include:

```text
pending event count
oldest pending event age
failed delivery count
dead-letter count
last successful delivery
billing API connectivity
```

Examples:

```text
Pending Events: 18,420
Oldest Pending Event: 31 hours
Billing Connectivity: DOWN
```

If thresholds exceeded:

- notify Acuven Admin
- DO NOT suspend customer merely because Billing infrastructure is down
- DO NOT interrupt customer's AI

Thresholds, notification recipients, and escalation timing are configured and documented before production. `DEAD_LETTER` events require an Admin-visible resolution queue. Replay must reuse the original immutable `event_id` and payload; operators cannot create a new ID to bypass a conflict. Every replay, discard decision, or manual reconciliation requires a reason and audit record.

---

# 32. Provider Usage Extraction Layer

The Integrated Application Backend currently has:

```text
app/services/llm.py
```

Thin facade over Anthropic SDK.

Refactor carefully.

Do NOT perform unnecessary large-scale abstraction.

Introduce:

```text
app/services/ai/
```

Suggested structure:

```text
app/services/ai/
├── base.py
├── models.py
├── anthropic_provider.py
├── openai_transcription_provider.py
└── usage.py
```

Define normalized internal response.

Example:

```python
AIUsage(
    provider="anthropic",
    model="...",
    usage_type="LLM_TOKEN",
    input_tokens=...,
    output_tokens=...,
    metadata={}
)
```

Application code does not calculate billing price.

It only extracts provider usage.

---

# 33. Anthropic Integration

Use Anthropic official SDK response usage metadata.

Capture where provided:

```text
input_tokens
output_tokens
cache_creation_input_tokens
cache_read_input_tokens
model
```

Do not assume all models expose identical usage metadata.

Adapter must handle optional provider-specific fields.

Raw provider usage metadata can be preserved inside structured JSON metadata if needed.

---

# 34. OpenAI Speech Transcription

Current OpenAI usage is speech transcription without SDK.

Refactor into provider adapter.

Track suitable metering unit according to the API billing structure.

The generalized usage event must support:

```text
AUDIO_SECOND
AUDIO_MINUTE
```

Do not force speech usage into `input_tokens/output_tokens`.

---

# 35. Integrated Application Billing Client

Create reusable internal Python package/module.

Suggested name:

```text
acuven_billing_client
```

Responsibilities:

- create normalized Usage Event
- save to local Outbox
- async delivery
- API authentication
- retry
- status reconciliation
- webhook helpers if appropriate
- health metrics

The AI application layer should not know Billing API implementation details.

---

# 36. Integration Authentication

Each Project gets unique credentials bound to exactly one tenant and project.

Use:

```text
API Key + Secret
```

Example:

```text
ACUVEN_BILLING_API_KEY
ACUVEN_BILLING_API_SECRET
```

Support Admin actions:

- Create
- Revoke
- Rotate
- Last Used At
- Status
- Project ownership

Never expose secret again after initial creation.

API keys are non-secret identifiers and may be stored in lookup form. Integration request-signing secrets and outbound status-webhook signing secrets must be stored using authenticated reversible encryption or a documented secure key-derivation design because HMAC requires signing key material. Password-style one-way hashing is not valid for these secrets.

The encryption master key must be stored outside the database, injected through production secret management, access-controlled, backed up separately, and covered by the recovery procedure. Logs, audit before/after state, API responses, and exception traces must never expose decrypted secrets.

Rotation must support a bounded overlap period with explicit key versions so in-flight requests can complete. Revoked versions must stop verifying after the overlap expires.

Every API key must be independently revocable.

---

# 37. API Request Signing

Required integration authentication:

```text
API Key
+
HMAC request signature
+
timestamp
```

Headers example:

```text
X-Acuven-Api-Key
X-Acuven-Timestamp
X-Acuven-Signature
X-Acuven-Request-Id
X-Acuven-Key-Version
```

Reject requests with excessive timestamp skew.

Suggested:

```text
±5 minutes
```

The signature timestamp is the current transmission attempt time and is regenerated on every retry. It is not the Usage Event `occurred_at`.

Canonical signature input must be specified identically in the client and server:

```text
HTTP_METHOD + "\n" +
NORMALIZED_PATH_AND_QUERY + "\n" +
X_ACUVEN_TIMESTAMP + "\n" +
X_ACUVEN_REQUEST_ID + "\n" +
SHA256(RAW_REQUEST_BODY)
```

Use UTF-8, lowercase hexadecimal SHA-256 body digest, HMAC-SHA256, and constant-time signature comparison. Reject invalid key versions, malformed timestamps, or signatures outside the allowed window. Production hosts must use NTP time synchronization and alert on excessive clock drift.

Financially mutating requests must combine request-signing replay protection with domain idempotency (`event_id` or payment identifier). Do not hard-code Redis as the only replay store; the persistence choice must survive the required failure model.

---

# 38. Integration API

Minimum V1 endpoints:

```http
POST /api/v1/integration/usage-events
GET  /api/v1/integration/account-status
GET  /api/v1/integration/health
```

Required batch endpoint:

```http
POST /api/v1/integration/usage-events/batch
```

This improves backlog recovery efficiency.

Example:

```json
{
  "events": [
    {...},
    {...},
    {...}
  ]
}
```

Each event receives an independent result and financial outcome.

---

# 39. Batch Ingestion

During Billing outage an Integrated Application Backend may accumulate thousands of events.

Do not require one HTTP call per old event during recovery.

Support controlled batch delivery.

Example:

```text
Batch size: 100
```

Configurable.

Response:

```json
{
  "accepted": 98,
  "duplicates": 2,
  "rejected": 0,
  "results": [
    {
      "event_id": "evt_...",
      "status": "accepted",
      "processing_status": "RECEIVED",
      "error_code": null,
      "retryable": false
    }
  ]
}
```

Batch acceptance permits partial success. Each event is independently validated and durably inserted or rejected; one invalid event must not roll back accepted events. The client marks only explicitly accepted or matching-duplicate local Outbox rows as `SENT`.

If the HTTP request times out or the response is lost, the client safely retries the entire batch using the same immutable `event_id` and payload. `IDEMPOTENCY_CONFLICT` and validation errors are non-retryable; infrastructure failures may be retryable as explicitly indicated per result.

---

# 40. Payment Gateway Architecture

Payment system must use Adapter Pattern.

Do not tightly couple wallet to one gateway.

Interface concept:

```python
class PaymentGateway:
    create_payment(...)
    verify_payment(...)
    parse_webhook(...)
    get_payment_status(...)
```

Future functions may include:

```text
refund
tokenized payment
auto top-up
```

V1 does not require automated refund.

---

# 41. Payment Gateway Selection

Gateway must support Malaysian payment requirements.

V1 implementation should select one gateway during implementation after validating:

- FPX as the priority V1 payment method
- DuitNow where applicable
- card support if required
- webhook reliability
- transaction fees
- API quality
- payment status query
- reconciliation capabilities

Architecture must allow future adapters such as:

```text
Billplz
ToyyibPay
Stripe
SenangPay
others
```

Wallet logic must never directly depend on specific gateway code.

The selected gateway must provide sandbox access, authenticated Webhooks, authoritative payment-status query, immutable gateway references, amount/currency verification, and reconciliation support before implementation proceeds.

---

# 42. Top-Up

Customer Portal provides recommended amounts.

Example:

```text
RM50
RM100
RM300
RM500
RM1,000
```

Also:

```text
Custom Amount
```

Minimum amount must be configurable through Admin settings.

The backend enforces minimum and optional maximum amounts. For a suspended tenant with negative or zero balance, the backend also calculates a minimum recovery amount that leaves the resulting wallet balance strictly above RM0.

Do not hardcode minimum value in frontend.

---

# 43. Payment Flow

```text
Customer chooses amount
        ↓
Create payment record
        ↓
Payment Gateway
        ↓
Customer pays
        ↓
Gateway Webhook
        ↓
Validate signature
        ↓
Verify payment status
        ↓
Idempotent payment processing
        ↓
Create TOPUP ledger transaction
        ↓
Wallet + amount
        ↓
Generate Receipt
        ↓
If previously suspended and balance > 0:
        ACTIVE
        ↓
Reactivation webhook
```

IMPORTANT:

> Never credit wallet merely because frontend redirects to a "payment successful" URL.

Wallet credit occurs only after trusted server-side confirmation.

Trusted confirmation requires all of the following:

- valid gateway authentication/signature
- known internal payment and matching tenant
- gateway status confirmed as paid
- gateway amount exactly matches the internal order amount
- gateway currency is MYR
- gateway payment identifier has not credited any wallet before

If amount or currency differs, do not credit the wallet. Persist the raw gateway event, mark the payment `REVIEW_REQUIRED`, create an Admin alert, and require audited manual resolution.

Webhook delivery is not assumed reliable. Celery Beat must periodically scan stale `PENDING` payments and call `get_payment_status` using controlled backoff. Define the pending timeout, terminal-state mapping, maximum reconciliation duration, provider-unavailable behavior, and `EXPIRED` transition for the selected gateway.

---

# 44. Payment Gateway Fees

Acuven absorbs Payment Gateway fees.

Example:

```text
Customer pays: RM100
Wallet credit: RM100
Gateway fee: RM1.50
```

Store:

```text
payment_amount = RM100
wallet_credit = RM100
gateway_fee = RM1.50
```

Gateway fee contributes to internal profit analytics.

Customer does not lose wallet value because of gateway fee.

---

# 45. Payment Receipt

After successful payment generate Payment Receipt.

Issuer:

```text
Acuven Technology Sdn Bhd
```

Receipt numbering example:

```text
ACV-AIR-2026-000001
```

Receipt and statement number allocation must be concurrency-safe, unique, monotonic within the configured series, and never silently reused. Voided numbers remain auditable.

Receipt should contain:

- Receipt Number
- Acuven company name
- Customer company/name
- Payment date
- Top-up amount
- Payment method
- Gateway reference
- Internal payment transaction ID
- Currency MYR
- Status
- Generated date
- immutable issuer/customer/tax-policy snapshot fields required by the approved accounting decision

Customer can download receipt from Portal.

V1 receipt is NOT LHDN e-Invoice.

Future e-Invoice integration should be possible without replacing payment architecture.

## 45.1 Tax and Compliance Decision Gate

LHDN e-Invoice being out of scope does not determine SST treatment. Before Phase 4 implementation, Acuven must obtain and document accounting/tax advice covering:

- whether Acuven is registered or required to register for SST
- classification of the AI service and applicable exemptions
- whether a top-up is stored value, deposit, or service prepayment
- tax recognition point and effective tax rate/version
- whether wallet credit is tax-inclusive or tax-exclusive
- mandatory receipt and statement fields

Until this decision is approved, production top-up, receipt, and statement schema is blocked. The system must support versioned tax-policy snapshots, but this specification must not invent a tax rate.

---

# 46. Monthly Statement

Every month automatically generate and finalize the previous month's statement using a T+1 cut-off.

Example generation:

```text
Usage period ends at 23:59:59.999999 on the final calendar day in Asia/Kuala_Lumpur.
T+1 grace period = the complete first calendar day of the new month.
Statement generation and finalization = 2nd day of the new month.
```

Each statement has `DRAFT`, `FINALIZED`, or `VOID` status. Generation is idempotent per tenant and billing period. A `FINALIZED` statement and its financial snapshot are immutable.

Usage period is determined by `occurred_at`; ledger posting period is determined by `processed_at`. An event from a prior usage period that is processed after the T+1 cut-off is posted in the current open period and shown separately as `PRIOR_PERIOD_ADJUSTMENT`. It must reference its original usage period.

Statement includes:

- statement number
- billing period
- opening wallet balance
- total top-up
- total AI usage deduction
- adjustments
- prior-period usage and rebill adjustments
- closing balance
- request count
- conversation count
- input tokens
- output tokens
- usage by provider
- usage by model
- usage by project
- total customer charges

Customer downloads PDF.

Do NOT display:

- Estimated or Reconciled Provider Cost
- Gross Profit
- Internal Markup

---

# 47. Customer Notifications

V1 notification channels:

- Portal
- Email
- WhatsApp

Reuse existing Acuven:

- Email sending service
- WhatsApp API service

Central Billing must implement Notification Adapter abstraction.

Do not rebuild WhatsApp infrastructure.

---

# 48. Low Balance Alert

Each Customer can have configurable threshold.

Example:

```text
RM50
```

When wallet crosses threshold:

- create notification
- send email
- send WhatsApp
- display Portal warning

Avoid duplicate notification spam.

Track threshold notification state.

Trigger only when the committed balance crosses from above the configured threshold to at-or-below it. Rearm only after a later committed balance returns above the threshold. Retry of the same notification event must not create a second logical notification.

---

# 49. Suspension Alert

When balance reaches or falls below threshold for suspension:

```text
balance <= 0
```

Notify customer:

- Portal
- Email
- WhatsApp

Do not send billing information to the Integrated Application Backend's end users.

---

# 50. Reactivation Alert

Successful top-up causing:

```text
SUSPENDED → ACTIVE
```

Send:

- Portal notification
- Email
- WhatsApp

---

# 51. Authentication

One authentication platform serves:

```text
ADMIN
CUSTOMER
```

Same React frontend.

Role determines available routes/features.

Backend MUST independently enforce authorization.

Never rely only on frontend menu hiding.

---

# 52. Customer User Model

V1:

```text
One Customer = One Customer Portal admin account
```

Do not implement customer multi-user RBAC in V1.

Schema may allow future expansion.

---

# 53. Login Security

Implement:

- Email + Password
- JWT Access Token
- Refresh Token
- Secure refresh token rotation
- Logout
- Forgot Password
- Reset Password
- Password strength requirements
- Login attempt rate limiting
- session/token revocation

---

# 54. Two-Factor Authentication

ADMIN:

```text
2FA mandatory
```

CUSTOMER:

```text
2FA supported
```

Whether Customer 2FA is mandatory can be configuration driven.

Recommended mechanism:

```text
TOTP
```

Compatible with common authenticator apps.

Provide recovery codes.

Store recovery codes securely hashed.

---

# 55. Admin Portal

Admin Portal must include at minimum:

## Dashboard

Show:

- Total Customer Revenue
- Actual AI Provider Cost
- Payment Gateway Fees
- Gross Profit
- Gross Margin
- AI Requests
- Conversations
- Active Customers
- Suspended Customers
- Total Wallet Balances
- Recent Top-Ups
- Billing Processing Errors
- Outbox/Integration Health Alerts

Filters:

- date range
- customer
- project
- provider
- model

---

# 56. Customer Management

Admin can:

- Create customer
- Edit customer
- Change tenant account lifecycle independently from billing status
- View wallet
- View projects
- Configure low balance threshold
- View payment history
- View usage
- View statements
- View receipts
- View status
- View integration health

---

# 57. Project Management

Each customer may have multiple Projects.

Fields:

```text
project_id
tenant_id
name
description
backend_base_url
status_webhook_url
encrypted_webhook_secret
webhook_key_version
integration_status
created_at
updated_at
```

Admin can:

- create project
- edit project
- disable project
- rotate webhook secret
- manage Billing API credentials

---

# 58. AI Provider Management

Admin can manage:

- Provider
- Model
- Metering unit
- Provider source price and currency
- Draft price changes
- Published price versions
- Effective date
- synchronization source/status
- FX source, draft, approval, and published versions
- provider invoice reconciliation coverage

---

# 59. Pricing Management

Admin can:

- assign pricing rule to customer
- assign specific model rule
- choose MARKUP
- choose FIXED_RATE
- set effective date
- disable rule
- view price history
- preview calculation

Pricing changes must support future-effective pricing and immutable published history. Preview must accept an explicit `occurred_at` and show every resolved component and version.

---

# 60. Wallet Management

Admin sees:

- Current balance
- Ledger
- Top-ups
- Usage charges
- Adjustments
- Rebill corrections

Admin can add adjustment.

Mandatory fields:

```text
amount
type
reason
```

System automatically records:

```text
operator
timestamp
balance_before
balance_after
```

---

# 61. Usage Management

Admin can search by:

- customer
- project
- conversation_id
- request_id
- provider
- model
- date range
- usage status

Request table:

```text
Time
Customer
Project
Conversation
Request ID
Provider
Model
Input Tokens
Output Tokens
Estimated/Reconciled Provider Cost
Billable Cost
Margin
Status
```

Admin can drill into event metadata.

No conversation content.

---

# 62. Reprocessing UI

Admin can select:

- failed Usage Event
- individual processed event
- date-range batch where permitted

Require:

```text
reason
```

Show preview before correction:

```text
Previous charge
New charge
Difference
```

All corrections generate ledger adjustments.

---

# 63. Payment Management

Admin sees:

- payment ID
- customer
- amount
- gateway
- gateway reference
- gateway fee
- status
- created date
- paid date
- receipt

Possible statuses:

```text
PENDING
PAID
FAILED
EXPIRED
CANCELLED
REVIEW_REQUIRED
```

The gateway adapter must define which provider states map to each internal state. `PAID` is terminal and may be reached only after trusted confirmation. `REVIEW_REQUIRED` never credits the wallet automatically.

---

# 64. Receipt Management

Admin can:

- View
- Download
- regenerate rendering if document file missing

Do not regenerate financial values from current pricing.

Receipt data must use immutable payment snapshot.

---

# 65. Statement Management

Admin can:

- View statements
- Download PDF
- regenerate file rendering from statement snapshot
- manually trigger statement generation if scheduled job failed

Do not recalculate historical statement using today's pricing.

---

# 66. Audit Log

Audit Log is mandatory.

Record sensitive activities such as:

```text
LOGIN
LOGIN_FAILED
CUSTOMER_CREATE
CUSTOMER_UPDATE
PRICING_CREATE
PRICING_UPDATE
PRICING_PUBLISH
WALLET_ADJUSTMENT
REBILL
API_KEY_CREATE
API_KEY_ROTATE
API_KEY_REVOKE
PROJECT_UPDATE
PROVIDER_PRICE_PUBLISH
PAYMENT_STATUS_CHANGE
TENANT_SUSPEND
TENANT_REACTIVATE
ADMIN_SETTING_CHANGE
```

Store:

```text
actor
actor_role
action
entity_type
entity_id
before_state
after_state
ip_address
user_agent
timestamp
reason where applicable
```

Audit records must not be editable through normal application APIs.

---

# 67. Customer Portal

Customer Dashboard shows:

- Wallet Balance
- This Month Usage
- Top-up button
- Request count
- Conversation count
- Current account status
- Low balance warning
- recent transactions
- usage chart

Do NOT show internal Acuven costs.

---

# 68. Customer Usage Page

Customer can filter:

- date
- project
- provider
- model
- conversation

Conversation-level table:

```text
Conversation ID
Started At
Last Activity
Requests
Input Tokens
Output Tokens
Customer Charge
```

Click conversation to view request-level details.

---

# 69. Customer Request Detail

Show:

- Request ID
- Date/time
- Project
- Provider
- Model
- Input Tokens
- Output Tokens
- other usage quantities where applicable
- Customer Charged Amount

Do NOT show:

- estimated or reconciled provider cost
- markup
- margin
- internal provider pricing

---

# 70. Customer Wallet Page

Show:

```text
Current Balance
Top Up
Transaction History
```

Ledger visible to Customer includes relevant customer-facing transaction descriptions.

Example:

```text
TOPUP +RM300
AI_USAGE -RM0.041200
ADJUSTMENT +RM20
```

---

# 71. Customer Payment Page

Customer can:

- Top up
- choose preset amount
- enter custom amount
- see payment history
- open/download receipt

---

# 72. Customer Statements Page

Customer can:

- view monthly statements
- download PDF

---

# 73. Export Functionality

V1 must support:

```text
CSV
Excel
```

Exports include at minimum:

- Request Level Usage
- Conversation Summary
- Wallet Transactions
- Monthly Usage Summary

Admin export may include internal costs.

Customer export MUST exclude internal costs.

Large exports should be generated asynchronously when needed.

---

# 74. Core Database Tables

Use migrations.

Required domain tables:

```text
users
refresh_tokens
two_factor_settings

tenants
projects
integration_credentials

wallets
wallet_transactions

ai_providers
ai_models
usage_meter_types

provider_price_versions
provider_price_components
fx_rate_versions
tax_policy_versions
pricing_rules
pricing_rule_components
provider_cost_reconciliations

usage_events
conversation_usage_summaries

payments
payment_gateway_events

receipts
monthly_statements

webhook_deliveries
notifications
domain_outbox

audit_logs

system_settings
```

The following table definitions are minimum V1 requirements, not optional examples. Exact SQL names may follow repository conventions, but field semantics and constraints must remain traceable.

## 74.1 provider_price_versions and provider_price_components

```text
provider_price_versions
  id
  public_id
  provider_id
  model_id
  source_currency
  source_type
  source_reference
  status: DRAFT | PUBLISHED | RETIRED
  effective_from
  effective_to
  approved_by
  approved_at
  created_at

provider_price_components
  id
  provider_price_version_id
  meter_type_id
  component_code
  unit
  unit_quantity
  rate_amount
  metadata_json
  created_at
```

Published effective intervals for the same provider/model/component dimensions must not overlap. `unit_quantity` and `rate_amount` must be positive Decimal values. Published versions and components are immutable, and component codes must cover all usage fields that contribute to provider cost.

## 74.2 fx_rate_versions

```text
id
public_id
base_currency
quote_currency
rate
source
source_reference
observed_at
effective_from
effective_to
status: DRAFT | PUBLISHED | RETIRED
approved_by
approved_at
created_at
```

Only published rates may be used for calculation. Published effective intervals for the same currency pair must not overlap.

## 74.3 pricing_rules and pricing_rule_components

```text
pricing_rules
  id
  public_id
  tenant_id nullable
  provider_id nullable
  model_id nullable
  strategy: MARKUP | FIXED_RATE
  markup_multiplier nullable
  priority_scope
  status: DRAFT | PUBLISHED | RETIRED
  effective_from
  effective_to
  approved_by
  approved_at
  created_at

pricing_rule_components
  id
  pricing_rule_id
  meter_type_id
  component_code
  unit
  unit_quantity
  rate_amount
  currency = MYR
  created_at
```

For `MARKUP`, `markup_multiplier` is required and FIXED_RATE components are absent. For `FIXED_RATE`, the multiplier is absent and all applicable price components are required. Database and service validation must reject mixed or incomplete strategy data.

Published effective intervals of the same pricing scope must not overlap. Scope fields and `priority_scope` must encode the mandatory resolution order in §16 without ambiguous nullable combinations.

## 74.4 integration_credentials

```text
id
public_api_key
tenant_id
project_id
encrypted_secret
key_version
status
valid_from
valid_until
last_used_at
created_at
revoked_at
```

The unique API key is a lookup identifier. Secret ciphertext must use authenticated encryption and must never be returned again after creation.

## 74.5 provider_cost_reconciliations

```text
id
provider_id
billing_period
source_document_reference
source_currency
source_total
reconciled_cost_myr
coverage_method
created_by
created_at
metadata_json
```

Reconciliation records are append-only and must retain their source reference and coverage method.

## 74.6 domain_outbox

```text
id
event_type
aggregate_type
aggregate_id
payload_json
status
attempt_count
next_retry_at
created_at
processed_at
```

The domain Outbox is written in the same transaction as wallet/status/payment changes. Redis/Celery carries delivery triggers only; database Outbox state remains the recoverable source of truth.

## 74.7 payments and payment_gateway_events

```text
payments
  id
  public_id
  tenant_id
  gateway
  expected_amount
  expected_currency = MYR
  wallet_credit_amount
  tax_policy_version_id nullable
  tax_amount nullable
  confirmed_amount nullable
  confirmed_currency nullable
  gateway_payment_id nullable
  gateway_fee nullable
  status
  expires_at
  paid_at nullable
  last_reconciled_at nullable
  created_at

payment_gateway_events
  id
  gateway
  gateway_event_id
  payment_id nullable
  payload_fingerprint
  verification_status
  processing_status
  received_at
  processed_at nullable
  metadata_json
```

`(gateway, gateway_event_id)` is unique. One gateway payment identifier may create at most one TOPUP ledger credit. Raw events must be retained according to the approved security/data policy and must never expose secrets in logs.

## 74.8 monthly_statements

```text
id
public_id
tenant_id
statement_number
period_start
period_end
cutoff_at
status: DRAFT | FINALIZED | VOID
snapshot_json
finalized_at nullable
created_at
```

`(tenant_id, period_start, period_end)` is unique for the active statement version. If corrected-statement versioning is introduced later, it must use explicit version and supersession fields; V1 uses current-period prior-period adjustments instead.

## 74.9 tax_policy_versions

```text
id
public_id
jurisdiction
policy_type
rate nullable
treatment_json
status: DRAFT | PUBLISHED | RETIRED
effective_from
effective_to
approved_by
approved_at
source_reference
created_at
```

No tax policy may be published until the §45.1 decision gate is approved. Payments, receipts, and statements snapshot the applied policy and calculated values so later policy changes do not rewrite history.

---

# 75. tenants

Minimum fields:

```text
id
public_id
company_name
contact_name
email
phone
account_status
billing_status
status_version
low_balance_threshold
currency
created_at
updated_at
```

---

# 76. projects

```text
id
public_id
tenant_id
name
backend_base_url
status_webhook_url
encrypted_webhook_secret
webhook_key_version
integration_status
created_at
updated_at
```

---

# 77. wallets

```text
id
tenant_id
currency
balance
version
created_at
updated_at
```

Use row locking / concurrency-safe mutation.

---

# 78. wallet_transactions

```text
id
public_id
wallet_id
tenant_id
transaction_type
amount
balance_before
balance_after
reference_type
reference_id
description
metadata_json
created_by
created_at
```

Do not update historical ledger values.

`amount` uses a signed convention: credits are positive and debits are negative. For every committed row, `balance_after = balance_before + amount`.

Each financial source must have a unique effect constraint, including one AI_USAGE debit per Usage Event and one TOPUP credit per gateway payment. The system must be able to verify `wallet.balance` against the ordered ledger and alert on any mismatch.

---

# 79. usage_events

Minimum fields:

```text
id
schema_version
event_id UNIQUE
request_id
conversation_id
tenant_id
project_id
integration_credential_id
payload_fingerprint

provider_id
model_id
usage_type

input_tokens
output_tokens
cache_creation_tokens
cache_read_tokens

quantity
unit

provider_price_version_id
pricing_rule_id
fx_rate_version_id

provider_source_cost
provider_source_currency
fx_rate_applied
estimated_provider_cost_myr
reconciled_provider_cost_myr nullable
billable_cost

status
error_code
error_message

occurred_at
received_at
processed_at
created_at
```

Index heavily used filters:

```text
tenant_id
project_id
conversation_id
request_id
provider_id
model_id
occurred_at
status
```

The `event_id` unique constraint is global. Only a matching credential/tenant/project/payload fingerprint is a duplicate; all other collisions are `IDEMPOTENCY_CONFLICT`.

---

# 80. Pricing Precision

Persisted monetary precision:

```text
DECIMAL(20,8)
```

Wallet may use:

```text
DECIMAL(20,8)
```

Never use:

```text
FLOAT
DOUBLE
```

for finance.

Use a higher-precision `Decimal` context for intermediate multiplication and division. Sum all applicable price components for one Usage Event first, then perform exactly one `ROUND_HALF_UP` operation to MYR 8 decimal places. Persist that rounded event amount to the immutable ledger and wallet.

At display boundary:

```text
MYR normal display → 2 decimals
```

Detailed usage may display up to 6 decimals.

Do not round each token component early. Display rounding never changes persisted financial values.

---

# 81. Wallet Concurrency

Multiple events from same tenant can be processed concurrently.

Prevent lost updates.

Recommended:

```text
SELECT ... FOR UPDATE
```

inside DB transaction for wallet mutation.

Flow:

```text
BEGIN
lock wallet
check current balance
create ledger
update wallet balance
commit
```

Financial idempotency must be enforced by database uniqueness within the transaction, not only by an earlier application-level check.

---

# 82. Processing Usage Event

Central API acceptance flow:

```text
receive event
        ↓
authenticate integration
        ↓
validate tenant/project ownership
        ↓
canonicalize and fingerprint payload
        ↓
insert Usage Event as RECEIVED
        ↓
on global event_id conflict:
    compare owner + fingerprint
    matching → return existing status
    mismatch → IDEMPOTENCY_CONFLICT + alert
        ↓
commit durable record
        ↓
return HTTP 202
```

Central asynchronous worker flow:

```text
claim RECEIVED event safely
        ↓
set PROCESSING
        ↓
resolve provider/model
        ↓
resolve published provider price at occurred_at
        ↓
resolve published FX rate at occurred_at
        ↓
calculate estimated provider cost in MYR
        ↓
resolve published customer pricing rule at occurred_at
        ↓
calculate billable cost and round once
        ↓
DB transaction:
    lock wallet
    enforce one financial effect for event_id
    create immutable AI_USAGE ledger row
    update wallet
    update event to PROCESSED with all version snapshots
    if balance <= 0 and billing status changes:
        update billing status + status_version
        create audit + durable domain Outbox rows
        ↓
commit
        ↓
rebuild/update derived aggregates asynchronously
```

Wallet mutation remains serialized per tenant wallet even though processing is asynchronous. Capacity tests must measure the hottest-tenant rate and backlog recovery; Celery does not remove this financial serialization requirement.

---

# 83. Handling Billing Processing Failure

An event may enter:

```text
RECEIVED
PROCESSING
PROCESSED
PRICING_ERROR
FX_RATE_ERROR
MODEL_UNKNOWN
IDEMPOTENCY_CONFLICT
FAILED_RETRYABLE
FAILED_FINAL
```

Never silently discard a usage event.

Admin Dashboard must surface unresolved billing events.

---

# 84. Unknown Model Handling

If an Integrated Application Backend sends:

```text
provider = anthropic
model = new-model-name
```

and model is unknown:

1. preserve Usage Event
2. mark `MODEL_UNKNOWN`
3. do not incorrectly price
4. notify Admin
5. Admin maps/adds model
6. reprocess event

---

# 85. Integration Availability Philosophy

Customer AI uptime is more important than real-time billing consistency.

Therefore use:

```text
eventual billing consistency
```

rather than synchronous authorization.

The platform accepts temporary over-consumption caused by Billing outage.

After recovery:

- process pending events
- wallet may become negative
- suspend customer
- require top-up

This is expected system behavior.

---

# 86. Financial Dashboard Metrics

Admin Dashboard calculates:

```text
Customer Revenue
Estimated Provider Cost
Reconciled Provider Cost
Provider Cost Basis / Reconciliation Coverage
Payment Gateway Fee
Gross Profit
Gross Margin
```

Definition:

```text
Gross Profit
=
Billable AI Revenue
-
Estimated/Reconciled Provider Cost
-
Payment Gateway Fees
```

Clearly distinguish:

```text
Wallet Top-up
```

from:

```text
Recognized AI Usage Revenue
```

Top-up is not the same as usage revenue.

Do NOT report all wallet top-ups as AI revenue.

Financial reports must define the period basis for gateway fees and provider reconciliation adjustments. They must not mix cash-receipt periods with usage-revenue periods without labeling the basis.

---

# 87. Analytics Dimensions

Support filtering/grouping by:

```text
Customer
Project
Provider
Model
Date
Day
Month
```

---

# 88. Authentication API

Recommended endpoints:

```http
POST /api/v1/auth/login
POST /api/v1/auth/refresh
POST /api/v1/auth/logout

POST /api/v1/auth/forgot-password
POST /api/v1/auth/reset-password

POST /api/v1/auth/2fa/setup
POST /api/v1/auth/2fa/verify
POST /api/v1/auth/2fa/disable
```

Admin 2FA cannot be disabled without appropriate security workflow.

---

# 89. Admin APIs

Representative endpoints:

```http
GET    /api/v1/admin/dashboard

GET    /api/v1/admin/customers
POST   /api/v1/admin/customers
GET    /api/v1/admin/customers/{id}
PATCH  /api/v1/admin/customers/{id}

POST   /api/v1/admin/customers/{id}/wallet/adjustments
GET    /api/v1/admin/customers/{id}/wallet/transactions

GET    /api/v1/admin/usage-events
GET    /api/v1/admin/usage-events/{id}
POST   /api/v1/admin/usage-events/{id}/reprocess

GET    /api/v1/admin/pricing-rules
POST   /api/v1/admin/pricing-rules
PATCH  /api/v1/admin/pricing-rules/{id}

GET    /api/v1/admin/provider-prices
POST   /api/v1/admin/provider-prices
POST   /api/v1/admin/provider-prices/{id}/publish

GET    /api/v1/admin/payments
GET    /api/v1/admin/statements
GET    /api/v1/admin/audit-logs
```

Exact endpoint naming may be adjusted if consistent.

---

# 90. Customer APIs

Representative:

```http
GET  /api/v1/customer/dashboard

GET  /api/v1/customer/wallet
GET  /api/v1/customer/wallet/transactions

POST /api/v1/customer/topups

GET  /api/v1/customer/payments
GET  /api/v1/customer/receipts
GET  /api/v1/customer/receipts/{id}/download

GET  /api/v1/customer/conversations
GET  /api/v1/customer/conversations/{id}
GET  /api/v1/customer/usage-events

GET  /api/v1/customer/statements
GET  /api/v1/customer/statements/{id}/download
```

Customer tenant identity MUST come from authenticated session.

Never accept arbitrary `tenant_id` from Customer UI and trust it for authorization.

---

# 91. Payment Webhook API

Example:

```http
POST /api/v1/webhooks/payments/{gateway}
```

Requirements:

- verify gateway signature
- store webhook event
- idempotency
- return appropriate status rapidly
- process heavy work asynchronously where possible

---

# 92. Central → Customer Status Webhook

Webhook event types:

```text
customer.billing.suspended
customer.billing.reactivated
```

Future:

```text
customer.billing.low_balance
```

---

# 93. Document Storage

Receipts and monthly statements may be stored in:

V1:

```text
local persistent Docker volume
```

Design storage abstraction so future migration to:

```text
S3-compatible object storage
```

does not require rewriting business logic.

Store immutable statement/receipt snapshot data in database separately from rendered PDF.

---

# 94. Logging

Use structured logging.

Each request should include correlation IDs such as:

```text
request_id
event_id
tenant_id
project_id
```

Never log:

- passwords
- TOTP secret
- raw API secrets
- full payment secrets
- customer AI prompts/responses

Production logging must define rotation, retention, maximum disk usage, secure deletion, and remote/off-host retention where required for incident investigation. Exhausting local disk through logs must alert before it threatens MySQL or document storage.

---

# 95. Monitoring

At minimum expose/monitor:

```text
HTTP error rate
Celery queue length
usage ingestion rate
usage processing error rate
unknown models
pricing errors
webhook delivery failures
payment webhook failures
statement generation failures
notification failures
stale payment age/count
MySQL and Redis health
database and document-volume capacity
backup age and backup failure
TLS certificate expiry
host clock drift
log volume growth
```

Before production, each metric must have a threshold, severity, notification recipient/channel, suppression rule, and runbook link. Alerts must distinguish customer-impacting failure from internal degraded operation.

---

# 96. Security

Required:

- HTTPS only in production
- Secure password hashing
- HMAC integration signatures
- HMAC Webhook signatures
- JWT expiry
- Refresh token rotation
- API rate limiting
- login rate limiting
- input validation
- SQLAlchemy parameterization
- tenant-level authorization
- secrets in environment / secret management
- no secrets committed to repository
- audit logs
- secure HTTP headers
- CORS restriction
- CSRF consideration depending on auth design
- webhook replay protection using timestamp/event ID

---

# 97. Data Isolation

Customer A must never retrieve:

- Customer B wallet
- Customer B usage
- Customer B payments
- Customer B statements
- Customer B project information

Tenant isolation tests are mandatory.

---

# 98. Central Database Isolation

Central Billing Platform has its own MySQL database.

Integrated Application Backend must NEVER connect directly to this database.

Communication only through:

```text
Billing REST API
+
Webhooks
```

On the single production VPS, Central Billing uses dedicated MySQL and Redis containers, credentials, databases, persistent volumes, resource limits, and backup jobs. It does not share a database or Redis instance with existing applications. Shared host networking or an external reverse proxy is permitted only through documented, least-privilege interfaces.

## 98.1 Production Recovery and Durability

Production recovery objectives:

```text
RPO <= 5 minutes
RTO <= 4 hours
```

The implementation must derive backup frequency and architecture from these objectives. Minimum controls:

- MySQL point-in-time recovery using binary logs plus regular full backups
- encrypted off-VPS backup storage with documented retention
- backup of immutable document files, deployment configuration, and encryption master keys using separate access controls
- automated backup success/freshness monitoring
- quarterly restore drill that verifies wallet, ledger, Usage Event, payment, statement, and document integrity
- documented disaster-recovery order and post-restore reconciliation

Redis and Celery are not the authoritative store for financial or delivery state. Usage processing, payment reconciliation, notification, Webhook, statement, and other durable jobs must be reconstructible by scanning MySQL state and domain Outbox records after queue loss.

Integrated Application Backend `SENT` Outbox retention and Central Billing recovery must be coordinated. After a Central restore, replay uses immutable event IDs and remains financially idempotent.

Database schema changes require compatibility analysis, expected locking/downtime, pre-deployment backup where risk warrants, migration ordering, failure handling, and a tested roll-forward or rollback strategy. Never improvise a destructive production migration.

---

# 99. Deployment

V1:

```text
Single VPS
Docker Compose
```

Environments:

```text
Local Docker test environment
Production VPS
No separate staging environment in V1
```

Services:

```text
nginx
frontend
api
celery-worker
celery-beat
redis
mysql
```

Optional:

```text
document-worker
```

Production source and delivery:

- public GitHub repository — see `docs/adr/ADR-0001-repository-visibility.md`
  - GitHub Free does not support branch protection on private repositories, so the protected `main` and mandatory-CI requirements below could not be met while staying private.
  - Because the repository is public, credentials, secrets, `.env` files, real hostnames/IP addresses, customer data, and supplier contract pricing must never be committed. The `secret-scan` CI job is a backstop, not a licence.
- protected `main` branch
- CI must pass before merge/deployment
- merge to `main` triggers GitHub Actions deployment to the VPS
- immutable image tags identify the exact commit
- production secrets are provided through GitHub environment/host secret management and never committed
- workflow applies database migrations in a documented safe order
- deployment waits for health checks, runs post-deploy smoke tests, and uses a tested rollback or roll-forward procedure on failure
- concurrent production deployments are serialized

Because there is no staging environment, payment, Email, WhatsApp, migration, and destructive recovery changes require sandbox/local verification plus an explicit production change checklist before release.

---

# 100. Future Scalability Requirement

Although V1 runs on one VPS:

- FastAPI must remain stateless
- no process-local persistent state
- use Redis/database for shared state
- document storage through abstraction
- workers horizontally scalable
- API horizontally scalable
- avoid sticky session dependency

Future architecture should support:

```text
Load Balancer
Multiple API Instances
Multiple Workers
Managed MySQL
Managed Redis
Object Storage
```

without changing Billing domain logic.

---

# 101. Suggested Repository Structure

```text
ai_billing_hub/
│
├── backend/
│   ├── app/
│   │   ├── api/
│   │   │   ├── auth/
│   │   │   ├── admin/
│   │   │   ├── customer/
│   │   │   ├── integration/
│   │   │   └── webhooks/
│   │   │
│   │   ├── core/
│   │   │   ├── config.py
│   │   │   ├── security.py
│   │   │   ├── logging.py
│   │   │   └── exceptions.py
│   │   │
│   │   ├── models/
│   │   ├── schemas/
│   │   ├── repositories/
│   │   ├── services/
│   │   │   ├── billing/
│   │   │   ├── pricing/
│   │   │   ├── wallet/
│   │   │   ├── payment/
│   │   │   ├── provider_pricing/
│   │   │   ├── webhook/
│   │   │   ├── notification/
│   │   │   ├── statement/
│   │   │   └── receipt/
│   │   │
│   │   ├── tasks/
│   │   └── main.py
│   │
│   ├── alembic/
│   └── tests/
│
├── frontend/
│   ├── src/
│   │   ├── api/
│   │   ├── components/
│   │   ├── features/
│   │   │   ├── auth/
│   │   │   ├── dashboard/
│   │   │   ├── customers/
│   │   │   ├── usage/
│   │   │   ├── wallet/
│   │   │   ├── payments/
│   │   │   ├── pricing/
│   │   │   ├── statements/
│   │   │   └── audit/
│   │   ├── layouts/
│   │   ├── routes/
│   │   ├── i18n/
│   │   └── main.tsx
│
├── integration-client/
│   └── acuven_billing_client/
│
├── docs/
│   ├── architecture.md
│   ├── integration.md
│   ├── api.md
│   └── billing-rules.md
│
├── docker-compose.yml
├── .env.example
└── README.md
```

---

# 102. Integrated Application Backend Structure

Existing customer repository should receive modules similar to:

```text
app/
├── services/
│   ├── ai/
│   │   ├── anthropic_provider.py
│   │   ├── openai_transcription_provider.py
│   │   └── usage.py
│   │
│   └── billing/
│       ├── client.py
│       ├── outbox.py
│       ├── events.py
│       ├── status.py
│       └── webhook.py
│
├── models/
│   ├── billing_outbox.py
│   └── conversation_session.py
│
├── tasks/
│   ├── billing_delivery.py
│   └── billing_status_sync.py
│
└── api/
    └── internal/
        └── billing_webhook.py
```

Adapt to existing project conventions rather than blindly forcing this exact layout.

---

# 103. Integrated Application Backend Request Flow

Target implementation:

```text
Incoming WhatsApp / Web Message
              ↓
Read local billing status
              ↓
SUSPENDED?
      ┌────YES────┐
      │           ↓
      │     return generic unavailable
      │
      NO
      ↓
Resolve/create conversation_id
      ↓
Call AI through provider adapter
      ↓
Receive AI response + normalized usage
      ↓
Create immutable Usage Event
      ↓
Persist Usage Event to Local Outbox
      ↓
Return AI Response
      ↓
Background worker delivers event
```

Writing to local Outbox should be reliable but should not introduce dependency on Central Billing availability.

---

# 104. Conversation Session Table

An existing Integrated Application Backend should add a table similar to:

```text
conversation_sessions
```

Suggested:

```text
id
conversation_id
channel
end_user_reference
status
started_at
last_activity_at
ended_at
end_reason
created_at
updated_at
```

Do not send unnecessary `end_user_reference` to Central Billing.

Billing only needs conversation_id.

---

# 105. Conversation Lifecycle

States:

```text
ACTIVE
CLOSED
```

Close reasons:

```text
IDLE_TIMEOUT
ORDER_COMPLETED
HUMAN_HANDOVER
WORKFLOW_COMPLETED
USER_ENDED
SYSTEM_ENDED
```

When closed:

next relevant message creates new conversation_id.

---

# 106. Receipt and Statement PDF

PDF generation must happen server-side.

Templates should use stable snapshot data.

Receipts/statements must be reproducible even if customer or pricing configuration changes later.

Store enough snapshot data to reproduce original document.

---

# 107. API Response Standard

Use consistent envelope.

Success example:

```json
{
  "success": true,
  "data": {...},
  "error": null,
  "request_id": "..."
}
```

Error:

```json
{
  "success": false,
  "data": null,
  "error": {
    "code": "PRICING_RULE_NOT_FOUND",
    "message": "..."
  },
  "request_id": "..."
}
```

Do not expose internal stack traces.

---

# 108. Pagination

All potentially large list endpoints must paginate.

Prefer:

```text
cursor pagination
```

for very large request-level usage tables.

Traditional pagination can be used for smaller administrative datasets.

Never return hundreds of thousands of usage rows in one HTTP response.

---

# 109. Time

Store timestamps in:

```text
UTC
```

UI converts based on configured timezone.

Default business display timezone:

```text
Asia/Kuala_Lumpur
```

Monthly statement periods must consistently use defined business timezone.

---

# 110. Scheduled Jobs

Celery Beat tasks should include:

```text
monthly statement generation
provider price synchronization
FX rate synchronization
stale payment reconciliation
notification retries
status webhook retries
stale processing recovery
domain Outbox recovery
usage aggregation
health checks
cleanup of expired auth sessions
```

Integrated Application Backend includes:

```text
billing outbox delivery
billing status reconciliation
conversation idle timeout cleanup
```

---

# 111. Aggregation

Request-level events remain source of truth.

Create derived aggregation for:

```text
conversation
daily usage
monthly usage
```

Do not replace request data with aggregates.

Aggregates may be rebuilt.

---

# 112. Data Retention

Financial and personal data must not share one blanket retention rule.

Before production launch, Acuven must approve a data-governance policy based on Malaysian legal, accounting, contractual, and PDPA advice. It must classify at minimum:

- immutable wallet/payment/statement/audit records and their required legal retention
- provider usage metadata required for financial traceability
- tenant contact name, email, phone, authentication data, and other personal data
- operational logs, delivery attempts, raw Webhook events, and exported files

Do not automatically purge wallet ledger or completed billing records before the approved financial retention period expires. Do not interpret financial retention as permission to retain tenant contact data indefinitely. Personal data must be deleted or irreversibly anonymized when no longer required, subject to documented legal obligations.

Operational logs and exports require explicit retention, secure deletion, and storage-capacity limits.

## 112.1 Account Closure and Balance Settlement

Closing a tenant follows this stateful process:

1. set account status to `DISABLED`, blocking new AI calls and new top-ups
2. revoke or disable integration credentials and wait for legitimate pending Usage Events and payments to reach a defined terminal state
3. resolve negative balance or refund positive balance through an audited manual payment/refund plus immutable ledger adjustment referencing the original payment where applicable
4. resolve receipts/statements and preserve the required financial snapshot
5. set account status to `CLOSED` only when wallet balance is zero and no unresolved financial events remain

Automated gateway refunds remain out of scope. A manual external refund must never directly edit wallet balance and must be limited to a verified refundable amount. Late events discovered after closure enter manual review and must not silently reopen the account.

PDPA and other mandatory legal obligations are not out of scope. Only a self-service customer privacy-request UI may be deferred from V1; Acuven still requires an operational process before launch.

---

# 113. Testing Strategy

Use automated tests.

Minimum:

## Unit Tests

Pricing:

```text
MARKUP calculation
FIXED_RATE calculation
token calculations
cache write/read component calculations
audio and other non-token component calculations
price version selection
pricing rule selection by occurred_at
FX version selection and conversion
rounding
missing price component and FX rate errors
```

Wallet:

```text
top-up
deduction
negative reconciliation
exactly zero remains suspended
minimum recovery top-up
adjustment
concurrent deduction
ledger sign and cached-balance reconciliation
```

Usage:

```text
idempotency
concurrent duplicate ingestion
same event_id with different payload/tenant
unknown model
pricing error
FX rate error
```

---

# 114. Integration Tests

Test:

```text
Usage Event → Billing → Ledger → Wallet
Payment Webhook → Wallet Top-up
Top-up → Reactivation
Negative Balance → Suspension
Webhook Retry
Duplicate Usage Event
Idempotency Conflict
Duplicate Payment Webhook
Payment Webhook loss → active reconciliation
Payment amount/currency mismatch → REVIEW_REQUIRED
Rebill
Late prior-period event
Cross-period rebill adjustment
Finalized statement immutability
Status Webhook out-of-order delivery
Redis/Celery queue loss → database-state recovery
```

---

# 115. Tenant Isolation Tests

Mandatory examples:

```text
Customer A cannot fetch Customer B wallet
Customer A cannot fetch Customer B usage
Customer A cannot fetch Customer B receipt
Customer A cannot guess another tenant ID
```

---

# 116. Failure Tests

Test Central Billing unavailable.

Expected:

```text
Customer AI continues working
Usage enters Local Outbox
Events retry
Central Billing restores
Backlog delivered
Events billed once
Wallet reconciled
Possible suspension occurs
```

Also test local Integrated Application Outbox persistence failure after a successful provider response. The AI response must still be returned, a critical privacy-safe alert must be emitted, and the manual-reconciliation incident path must be exercised.

---

# 117. Concurrency Tests

Test multiple Usage Events charging same wallet.

Expected:

- no lost updates
- correct balance
- one immutable ledger row per usage
- duplicate events not charged twice

Measure hottest-tenant serialization and demonstrate that event state, ledger rows, and wallet balance remain consistent when workers retry or crash around transaction boundaries.

---

# 118. Payment Security Tests

Test:

```text
fake success redirect does not credit wallet
invalid webhook signature rejected
duplicate payment webhook does not double credit
wrong amount rejected
unknown payment rejected
same gateway payment cannot credit different internal payments
stale PENDING payment reconciliation is idempotent
```

---

# 119. Performance Target

V1 does not require massive-scale distributed architecture.

However design to comfortably support at least:

```text
1,000,000+ Usage Events
```

without architectural redesign.

Indexes and pagination are mandatory.

Request-level event volume is expected and acceptable.

Initial V1 engineering acceptance targets, to be validated and adjusted through a documented Phase 0 capacity baseline:

```text
Stored Usage Events: >= 1,000,000
Sustained ingestion: >= 20 events/second
Five-minute burst ingestion: >= 100 events/second
Single-event or batch durable-acceptance API latency: p95 <= 500 ms under sustained target load
Healthy event-to-wallet processing latency: p95 <= 60 seconds, p99 <= 5 minutes
Backlog recovery throughput: >= 5x documented peak normal ingestion rate
Status Webhook enqueue after committed transition: p95 <= 60 seconds
Status reconciliation safety bound: <= 5 minutes
```

Measurements must include the hottest-tenant workload because wallet mutation is serialized per tenant. Report hardware, dataset size, concurrency, test duration, and observed p50/p95/p99. A short synthetic burst alone does not satisfy the acceptance test.

The single-VPS V1 deployment targets 99.5% monthly Central Billing availability excluding approved maintenance. This target does not weaken `REQ-AVAIL-001`: an outage still must not interrupt Integrated Application AI responses.

---

# 120. Operational Admin Alerts

Admin should be alerted for:

```text
Billing ingestion outage
Outbox backlog
Pricing Error
FX Rate Error
Unknown Model
Provider price synchronization failure
FX rate synchronization failure
Payment Webhook failure
Stale pending payment
Webhook delivery failure
Statement generation failure
Unusually high customer usage
Negative wallet balance
Backup failure or stale backup
Database/document volume capacity
TLS certificate expiry
Clock drift
```

---

# 121. V1 Out of Scope

Explicitly NOT required in V1:

- Customer multi-user RBAC
- automated payment refunds
- LHDN e-Invoice
- multi-currency
- automatic credit card auto-top-up
- central AI Gateway
- storing AI conversation content
- customer BYOK
- complex postpaid invoicing
- Kubernetes
- full distributed microservices architecture

Avoid scope creep.

---

# 122. Future Architecture Compatibility

Design V1 so later phases may support:

```text
BYOK
Auto Top-Up
Postpaid Billing
Credit Limit
Multiple Currencies
LHDN e-Invoice
Multiple Customer Users
Central AI Gateway
More Payment Gateways
More AI Providers
Enterprise SSO
```

Do not implement these now.

---

# 123. Development Phases

## Phase 0 — Foundation

Build:

- repository
- Docker Compose
- FastAPI structure
- React structure
- MySQL
- Redis
- Celery
- Alembic
- authentication base
- logging
- error handling
- CI test structure
- private GitHub repository and protected `main` CI/CD
- dedicated production MySQL/Redis topology
- backup, recovery, and encryption-key design
- initial performance/SLO baseline
- FX provider evaluation

Acceptance:

```text
All services boot
DB migrations execute
Admin can login
2FA works
CI gates merge and deploys an immutable commit image
RPO/RTO recovery design is approved
```

---

# 124. Phase 1 — Tenant, Project & Wallet Core

Implement:

- Tenant
- Project
- Admin customer management
- Wallet
- Immutable Wallet Ledger
- Admin adjustment
- API Credentials
- Audit Log

Acceptance:

```text
Admin creates customer
Customer gets wallet
Admin creates project
API credential created
Wallet adjustment works
All actions audited
```

---

# 125. Phase 2 — AI Usage Billing Engine

Implement:

- Provider
- Model
- Usage Meter
- Provider price versions
- generalized provider price components
- automatic FX adapter, approval, and version history
- Pricing Rules
- MARKUP
- FIXED_RATE
- durable 202 Usage ingestion and asynchronous processing
- global event idempotency and conflict detection
- estimated provider cost calculation
- wallet deduction
- negative balance handling
- suspension

Acceptance:

```text
Usage Event billed once
Estimated provider cost and MYR conversion calculated
Customer cost calculated
Wallet deducted
Duplicate event no double charge
Conflicting event ID rejected without charge
Queue loss recoverable from database state
```

---

# 126. Phase 3 — Existing Integrated Application Backend

Modify `E:\projects\ai_chatbot_demo` as the first pilot.

Implement:

- Provider Usage Extraction
- Anthropic usage
- OpenAI transcription usage
- conversation_id
- complete conversation lifecycle
- Local Outbox
- Celery delivery
- Billing Client
- Integration authentication
- Webhook receiver
- local versioned effective service status
- periodic reconciliation
- backlog monitoring

Acceptance:

```text
Claude response still works if Billing is offline
Usage stored locally
Billing recovery sends backlog
No duplicate charge
Suspension stops new AI calls
Reactivation resumes service
```

Only after successful pilot should integration be rolled into additional customers.

---

# 127. Phase 4 — Payment & Customer Portal

Implement:

- Customer authentication
- Customer dashboard
- Wallet
- Top-up UI
- Payment Gateway Adapter
- one selected Malaysian Gateway
- FPX priority
- Payment Webhook
- active stale-payment reconciliation
- payment mismatch review
- Wallet top-up
- Payment Receipt
- Receipt download

Acceptance:

```text
Customer pays RM100
Gateway confirms payment
Wallet increases exactly RM100 once
Receipt generated
Duplicate webhook does not double-credit
Lost Webhook is recovered by status reconciliation
SST/accounting gate is approved before production
```

---

# 128. Phase 5 — Usage Portal

Implement:

- Conversation summary
- Request-level details
- usage filters
- customer usage exports
- admin internal cost/margin views
- provider/model analytics

Acceptance:

Customer sees:

```text
Provider
Model
Tokens
Customer Charge
```

Customer cannot see:

```text
Estimated/Reconciled Provider Cost
Markup
Margin
```

---

# 129. Phase 6 — Notifications & Status Automation

Implement:

- Low Balance
- Suspension notification
- Reactivation notification
- Portal notifications
- existing Email adapter
- existing WhatsApp adapter
- Webhook retry
- health alerts

---

# 130. Phase 7 — Statements & Financial Analytics

Implement:

- monthly statements
- T+1 cut-off and prior-period adjustments
- PDF download
- Revenue
- Estimated/Reconciled Provider Cost
- Payment Gateway Fee
- Gross Profit
- Gross Margin
- filters
- CSV/Excel exports

---

# 131. Phase 8 — Provider Price Synchronization & Rebill

Implement:

- Provider price sync adapter
- FX rate sync adapter
- Draft
- Admin approval
- Publish
- Historical versioning
- provider invoice cost reconciliation
- Reprocess
- Rebill adjustments

---

# 132. Definition of Done

A feature is not complete unless:

1. database migration exists
2. backend validation exists
3. authorization exists
4. audit logging exists where required
5. tests exist
6. error handling exists
7. API contract documented
8. frontend loading/error states exist
9. tenant isolation verified
10. financial idempotency verified where applicable
11. monetary precision, version selection, and accounting-period tests exist where applicable
12. durable job state can recover from Redis/Celery loss where applicable
13. migrations include production locking, backup, and failure-handling analysis
14. monitoring, alert ownership, and runbook coverage exist
15. performance and latency targets are verified for volume-sensitive features

Before production launch, the following cross-feature gates must also pass:

- SST/accounting treatment approved and reflected in immutable financial snapshots
- PDPA and data-retention policy approved
- RPO/RTO restore drill passed
- payment, Email, and WhatsApp real-account integration verified
- protected `main` CI/CD deployment and recovery procedure verified

---

# 133. Critical Invariants

Codex MUST preserve these invariants throughout development.

## Invariant 1

Central Billing failure must NOT stop customer AI service.

## Invariant 2

Same Usage Event must never financially charge twice.

## Invariant 3

Same Payment must never credit Wallet twice.

## Invariant 4

Wallet balance changes only through ledger transactions.

## Invariant 5

Historical ledger entries are immutable.

## Invariant 6

Historical Usage Event retains pricing/version references.

This includes provider price, customer pricing rule, FX rate, source currency/amount, and the MYR calculation snapshot.

## Invariant 7

Customer cannot see Acuven Estimated/Reconciled Provider Cost or Margin.

## Invariant 8

Customer cannot access another tenant's data.

## Invariant 9

AI conversation content is not stored in Billing Platform.

## Invariant 10

All monetary calculations use Decimal, not float.

## Invariant 11

A globally unique Usage Event ID can produce at most one financial effect; mismatched reuse is a conflict, never a duplicate success.

## Invariant 12

Finalized statements are immutable. Late usage and cross-period rebills are posted as explicit prior-period adjustments in an open period.

## Invariant 13

Wallet mutation, billing-status transition, audit record, and durable outbound domain events are committed atomically where the transition is caused by that mutation.

## Invariant 14

Redis/Celery loss must not destroy durable financial, payment, notification, Webhook, or document-generation work.

---

# 134. First Pilot Integration

Do NOT immediately modify every existing Integrated Application Backend.

Select one Integrated Application Backend as pilot.

Implementation order:

```text
1. Identify all current calls through app/services/llm.py
2. Introduce normalized Usage model
3. Extract Anthropic usage
4. Add conversation session mechanism
5. Add billing_outbox
6. Persist Usage Event
7. Implement async delivery
8. Connect sandbox Central Billing API
9. Verify end-to-end usage
10. Test Billing outage
11. Test duplicate events
12. Test suspension
13. Test reactivation
14. Only then create reusable integration package
15. Roll out to remaining customer systems
```

---

# 135. Codex Development Rules

Codex must follow these rules.

### Rule 1

Do not implement the entire application in one uncontrolled change.

Work phase-by-phase.

### Rule 2

Before each phase:

- inspect existing repository
- identify affected modules
- produce implementation plan
- identify migrations
- identify tests

Then implement.

### Rule 3

Never remove working customer AI functionality merely to introduce Billing.

### Rule 4

Do not add synchronous Central Billing dependency to AI request path.

### Rule 5

Do not calculate customer selling price inside an Integrated Application Backend.

### Rule 6

Do not trust monetary amount submitted by an Integrated Application Backend.

### Rule 7

Do not put Provider API secrets in frontend.

### Rule 8

Do not create a central AI Gateway in V1.

### Rule 9

Prefer simple modular monolith architecture for Central Billing V1 rather than unnecessary microservices.

### Rule 10

All significant architecture decisions that differ from this specification must be documented before implementation.

---

# 136. Required Documentation Produced by Codex

Maintain:

```text
docs/architecture.md
docs/database-schema.md
docs/api.md
docs/integrated-application-backend.md
docs/payment-flow.md
docs/pricing-engine.md
docs/currency-and-fx.md
docs/data-governance.md
docs/deployment.md
docs/runbook.md
docs/adr/
```

Architecture decisions for central ingestion semantics, financial period/cut-off, credential encryption, production database isolation, and external provider selections must be recorded as ADRs before their implementation phase.

`runbook.md` should cover:

- Billing API down
- MySQL down
- Redis down
- Celery backlog
- Usage backlog
- payment webhook failure
- stale pending payment and payment reconciliation failure
- pricing error
- FX rate error and synchronization failure
- unknown model
- negative wallet
- status webhook failure
- out-of-order status delivery
- backup failure and full restore
- encryption master-key recovery
- document-volume recovery
- late prior-period usage and cross-period rebill
- account closure with unresolved financial events

---

# 137. Recommended Initial Codex Instruction

Start implementation by reading this specification completely.

Do not start by generating frontend pages.

First establish the domain model and financial invariants.

The first implementation milestone is:

```text
Phase 0
+
Phase 1
```

Before writing code:

1. inspect current repository state
2. propose exact directory structure
3. propose initial database schema
4. identify all V1 domain entities
5. identify financial transaction boundaries
6. identify idempotency boundaries
7. identify asynchronous task boundaries
8. produce implementation checklist

Then begin implementation.

Do not continue to Phase 2 until Phase 0 and Phase 1 tests pass.

---

# 138. Final Target User Experience

## Customer

Customer logs in and sees:

```text
AI Credit Balance
RM 238.62

This Month Usage
RM 61.38

AI Requests
4,732

Conversations
1,286

[ Top Up ]
[ Usage Details ]
[ Transactions ]
[ Statements ]
```

Customer can drill down:

```text
Conversation
    ↓
Requests
    ↓
Provider / Model
Input Tokens
Output Tokens
Charged Amount
```

---

# 139. Final Admin Experience

Acuven Admin sees:

```text
Revenue              RM 8,420.50
Provider Cost Basis    RM 3,186.20
Gateway Fees         RM   126.30
Gross Profit         RM 5,108.00
Gross Margin             60.66%
```

Admin can drill down:

```text
Customer
→ Project
→ Conversation
→ Request
→ Provider
→ Model
→ Token
→ Estimated/Reconciled Provider Cost
→ Customer Charge
→ Margin
```

The complete financial trail must be auditable from:

```text
AI Request
        ↓
Usage Event
        ↓
Provider Price Version
        ↓
FX Rate Version
        ↓
Estimated Provider Cost Snapshot
        ↓
Customer Pricing Rule
        ↓
Wallet Transaction
        ↓
Wallet Balance
```

And for top-up:

```text
Payment
        ↓
Gateway Confirmation
        ↓
Wallet Transaction
        ↓
Receipt
        ↓
Wallet Balance
```

---

# 139.1 Critical Requirement Traceability

| Requirement | Normative rule | Primary sections | Required test evidence |
| --- | --- | --- | --- |
| `REQ-AVAIL-001` | Central Billing outage never blocks end-user AI service. | §1, §20–§22, §85 | Failure and pilot E2E tests |
| `REQ-INGEST-001` | `202` follows durable `RECEIVED` persistence; pricing is asynchronous. | §20, §38–§39, §82 | API, crash, and queue-loss tests |
| `REQ-IDEMP-001` | Global `event_id` produces at most one financial effect; mismatched reuse conflicts. | §23, §79, §82 | Duplicate, conflict, and concurrency tests |
| `REQ-FIN-001` | Provider source cost, currency, FX, MYR estimate, and versions are historical snapshots. | §5, §14, §17 | FX/version/rounding tests |
| `REQ-FIN-002` | Wallet changes only through signed immutable ledger rows using Decimal and one event-level rounding. | §8, §77–§81 | Ledger reconciliation and concurrency tests |
| `REQ-PRICE-001` | Provider/customer pricing resolves every required component by `occurred_at`. | §15–§17, §74 | Component, gap, overlap, and late-event tests |
| `REQ-AUTH-001` | Per-project HMAC keys are encrypted, versioned, rotated, and never logged. | §36–§37, §74.4, §96 | Signature, rotation, replay, and secret-leak tests |
| `REQ-STATUS-001` | Account, billing, and project status compose deterministically and propagate monotonically. | §24–§30 | Transition, ordering, and reconciliation tests |
| `REQ-PAY-001` | A trusted MYR payment credits once; mismatch never auto-credits; lost Webhook is reconciled. | §40–§45, §74.7, §110 | Payment security and reconciliation tests |
| `REQ-PERIOD-001` | T+1 final statements are immutable; later activity is a prior-period adjustment. | §19, §46, §74.8 | Month-boundary and rebill tests |
| `REQ-OPS-001` | Dedicated stores meet RPO/RTO and durable work is reconstructible without Redis. | §95, §98–§99, §132 | Restore drill and queue-loss tests |
| `REQ-PRIV-001` | Billing stores metadata only and applies approved retention by data class. | §13, §94, §112 | Privacy, retention, and tenant-isolation tests |

Implementation plans, migrations, APIs, and tests must reference applicable IDs. A requirement is not complete without the listed evidence or an explicitly approved deviation.

---

# 140. Overall Architectural Decision

V1 uses:

```text
Independent Integrated Application Backend
+
Local Transactional Outbox
+
Durable Asynchronous Billing Ingestion
+
Central Acuven Billing Platform
+
Shared Customer Wallet
+
Request-Level Metering
+
Immutable Financial Ledger
+
Versioned Pricing
+
Versioned Foreign Exchange
+
Prepaid MYR Credit
```

This is the authoritative architecture for V1.

The system prioritizes:

1. Customer AI availability
2. Financial correctness
3. Idempotency
4. Auditability
5. Data isolation
6. Transparent customer usage
7. Maintainability
8. Future scalability

Any implementation that violates these priorities should be treated as architecturally incorrect.

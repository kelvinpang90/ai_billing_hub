# AIH-TASK-172..178 设计：支付第一批（Stripe 适配器、支付 Webhook、可信确认与入账、客户建单）（已批准 v3）

> **来源**：设计闸门 Issue #580。本文件是 `APPROVED: design v3` 那一版正文的**逐字副本**（送审时状态行为 `READY_FOR_REVIEW`，批准后 Issue 上只把状态行改成了 `APPROVED: design v3`）。审查：Codex 额度不可用期间由 Claude Code 审查（审查者由 Kelvin 2026-11-06 选定）。送审前按登记前预审（`worker.design_precheck`）两轮修订；v1 审查阻断「建单时 Stripe 超时留下的会话，其事件会无限重试」，v2 改为从事件认回会话 id，批准；v2 批准后登记前预审第三轮又报出设计缺陷 4 条（金额上限、Stripe 地址不写进仓库、建单校验顺序、事件任务防重叠），升 v3 修订后再次批准（v2 的批准随之作废）。
> 放进仓库，是因为 OpenClaw Worker 在沙箱里不联网、读不到 GitHub Issue。
> **实现以本文件为准**；与 Issue 不一致时，以 Issue #580 上被批准的 v3 为准。设计要改，就回到 Issue 升版本、重新过闸门，不要直接改本文件。
> 产品决定（拆批、建单接口与开关、Stripe 托管付款页、只存实际手续费、停用入账与关户复核、内部租户不能充值）由 Kelvin 2026-11-06 拍板，见 §1。

## 0. 准入判定

| 任务涉及 | 要填的范围 |
| --- | --- |
| 钱包 / 账本 / 定价 / 汇率 / 支付 / 幂等 / 状态机 | **全部章节** |
| 用量摄取 / 认证与会话（集成侧与平台侧）/ Webhook | §1–§7（**§6 不可省** —— 密钥存储与轮换、鉴权、租户过滤、日志脱敏都在那一节） |
| 前端 / 文档 / CI / 脚本 | **不走本闸门**，直接开 PR |
| 既不碰钱、也不碰用量摄取 / 认证与会话 / Webhook 的后端改动 | **不走本闸门**，直接开 PR |

本任务档位：`全部`。理由：支付、钱包入账（TOPUP）、幂等、支付状态机、支付 Webhook 全部在内。

---

状态：`READY_FOR_REVIEW`
负责人：Claude
**设计版本**：`v3`
对应需求：spec §7、§8、§24、§40–§44、§63、§66、§74.6、§74.7、§89、§91、§112.1、§114、§118、§127；`REQ-PAY-001`、`REQ-TXN-001`；Invariant 3；ADR-0004、ADR-0006、ADR-0007、ADR-0008、ADR-0010、ADR-0015
目标 PR：待开（拆成 `AIH-TASK-172`..`178`，见 §11）

## 1. 目标与边界

### 要解决的问题

Phase 4 要让客户用 Stripe（FPX 优先）给预付钱包充值。本批（支付第一批，下称「A」）交付从「建单」到「可信确认后入账」的完整后端链路：两张表、Stripe 适配器、支付 Webhook、可信确认与 TOPUP 入账、客户发起充值的后端接口。spec §43 的六条可信确认与 Invariant 3（同一笔支付永不重复入账）必须在同一条路径上成立，所以这几样同批。

### Kelvin 2026-11-06 的决定

| 问题 | 决定 |
| --- | --- |
| 拆批 | A：表、适配器、回调、入账与客户建单接口（本设计）；B：长时间待付的单子的补对账、`EXPIRED`、`REVIEW_REQUIRED` 的管理端处理（在对客户开放充值之前上线）；C：充值界面、管理端设置（最低额、ADR-0010 缓冲、60 秒复用与 60 分钟过期）、最小恢复额；D：收据 |
| A 是否含客户发起充值的接口 | 含，**只做后端**；由配置开关控制，**生产默认关**；最低额先用配置项；界面与管理端设置留给 C。为了在 B 上线前做生产端点复验，开关分成两个：`payments_enabled`（回调与后台处理）与 `payment_topups_open`（客户建单），另有建单允许名单（见「配置」） |
| 付款方式 | **Stripe 托管付款页（Checkout Session）**：后端建单得到 Stripe 付款页地址，客户跳过去付、付完跳回门户。卡与 FPX 都支持，门户不加载 Stripe 脚本 |
| 手续费（spec §44，ADR-0015 要求本闸门写明） | 只存**逐笔实际手续费**（Stripe 余额流水里的 `fee`）为 `gateway_fee`；第一版不拿合同费率作预期值比对 |
| 停用或关户的租户收到支付成功 | `DISABLED`：照常入账（钱已收到；spec §24 充值只改计费状态，不会解除停用）；`CLOSED`：不入账，置 `REVIEW_REQUIRED` 并告警（spec §112.1） |
| 内部计量租户（`INTERNAL_METERED_ONLY`）充值 | 不能：建单接口拒绝；万一收到它的支付成功，置 `REVIEW_REQUIRED`，不入账 |

### 可观察的完成标准

1. 客户（`PREPAID`、账户 `ENABLED` 或 `PENDING_ACTIVATION`）调 `POST /api/v1/customer/topups` 带金额，得到一笔 `PENDING` 支付与 Stripe 付款页地址；`payments_enabled` 关闭时该接口不存在（404）；`payment_topups_open` 关闭时只有允许名单里的租户能建单，其他租户 403 `TOPUPS_NOT_OPEN`。
2. 客户付款后，Stripe 回调 `POST /api/v1/webhooks/payments/stripe`：验签通过才落库、立即返回 200；后台任务对每个相关事件**向 Stripe 查询权威状态**，六条可信确认全部满足才把支付置 `PAID`，并在同一事务里写一条 `TOPUP` 账本行（金额等于建单金额，手续费不影响入账额），复机、审计、状态 webhook 由现有账本逻辑自动完成。
3. 同一笔支付不论收到多少个不同 id 的事件、重复投递多少次、并发处理多少次，钱包只增加一次（三层唯一约束，见 §2「幂等的三层」）。
4. 金额或币种不符、引用对不上、租户已关户或是内部租户：不入账，置 `REVIEW_REQUIRED`，写审计、触发告警。
5. 伪造的「支付成功」跳转、错误签名、未知支付、同一个网关支付号对应两笔内部支付，都不会入账（spec §118 前六条）。
6. `PAID` 后记下 Stripe 的逐笔实际手续费 `gateway_fee`（拿得到时）。

### 明确不做什么

- **不做长时间待付的单子的补对账、`EXPIRED` 的定时转换、`REVIEW_REQUIRED` 的管理端处理界面与接口**：属于 B。A 只提供 B 要复用的「确认一笔支付」函数，并处理 Stripe 主动推送的「已过期」事件。丢失的 webhook 由 B 的补对账补回（spec §127 那一条验收由 B 完成）。
- **不做充值界面、支付历史、管理端设置、最小恢复额、收据**：属于 C、D。
- **不做退款与拒付（chargeback / dispute）**：spec 只写 V1 不做自动退款、人工退款走管理员调账（§8）；拒付 spec 没有规定。A 不订阅拒付事件，留给后续设计闸门。
- **不对客户发「支付成功」通知**：复机通知已由现有账本逻辑发出；支付成功的单独通知随收据（D）再定。
- **不按合同费率核对手续费**（Kelvin 决定，见上表）。
- **不在生产开启支付**：开关默认关；开启要管理员补 Docker secret、配置 Stripe Dashboard 回调端点，并完成 ADR-0015「收口条件」里的生产端点复验（§8）。
- **不改账本、钱包、复机的现有逻辑**：只调用 `post_transaction`。

### 现有行为与证据

- 支付相关的表、模型、配置、接口都还不存在；账本已有 `TOPUP` 交易类型与 `PAYMENT` 引用类型（`app/models/wallet.py`），`uq_wallet_transactions_reference (reference_type, reference_id)` 唯一。
- `post_transaction`（`app/repositories/wallet.py`）在同一次 flush 里写账本行、推进钱包、按余额改计费状态（>0 为 `ACTIVE`）、写 `TENANT_BILLING_STATUS_CHANGED` 审计与 `tenant.billing_status_changed` outbox；后者驱动门户复机通知、邮件与状态 webhook。所以 TOPUP 走它即可完成 spec §27 的复机要求。
- ADR-0015 实测：Stripe 事件 id 在重复投递时不变；同一笔支付会发出多个**不同 id** 的事件；回调可按 `t.body` 的 HMAC-SHA256 验签；`create` 必须带 `Idempotency-Key`；MYR 最低 RM2；卡与 FPX 的余额流水类型不同。
- ADR-0007：同一租户允许多笔 `PENDING` 并存、都付都入账；60 秒内同额请求复用同一笔；`EXPIRED` 不是终态，过期后到账照样入账。
- ADR-0008：`expected_amount` 是含税毛额，`wallet_credit_amount = expected_amount`；未注册 SST，`tax_amount` 恒为空。
- ADR-0004：支付 webhook 的去重落库，不靠 Redis，以 `(gateway, gateway_event_id)` 唯一约束兜底。

## 2. 设计概要

### 端到端数据流

```text
客户 POST /api/v1/customer/topups {amount}
  → 校验（开关、计费模式、账户状态、金额范围、税务政策）→ 60 秒同额复用
  → 事务一：INSERT payments(PENDING) → COMMIT
  → Stripe：创建 Checkout Session（Idempotency-Key、client_reference_id = payments.public_id、过期时刻）
  → 事务二：写 gateway_payment_id → COMMIT → 返回 {payment_id, redirect_url, expires_at}
客户在 Stripe 付款页付款 → 跳回门户（跳回本身不改任何状态）
Stripe POST /api/v1/webhooks/payments/stripe
  → 读原始字节 → 验签（时间窗 300 秒）→ 解析事件 → INSERT payment_gateway_events(RECEIVED) → COMMIT → 200
后台任务（Beat 每 15 秒）
  → 领取到期的 RECEIVED / RETRY 事件（SKIP LOCKED + 租约）
  → 按 client_reference_id 找到 payments 行（找不到：UNMATCHED）
  → confirm_payment(payment)：
       向 Stripe 查询 Checkout Session 的权威状态（不在事务里）
       → 事务：锁 payments 行 → 按决策表落状态
            → PAID：post_transaction(TOPUP) + 审计（同一事务）
            → REVIEW_REQUIRED / FAILED / EXPIRED：只改状态 + 审计
       → COMMIT
  → 事件置 PROCESSED（Stripe 不可用：RETRY，退避，不放弃）
```

### 数据库（迁移 0036；0035 已被 AIH-TASK-154 预定）

表 `payments`（spec §74.7 的字段，另加本设计需要的列）：

| 列 | 类型 | 说明 |
| --- | --- | --- |
| `id` | BIGINT PK | |
| `public_id` | CHAR(36) NOT NULL UNIQUE | `uuid4`，对外 id，也是账本引用与 Stripe `client_reference_id` |
| `tenant_id` | BIGINT NOT NULL，FK `tenants.id` RESTRICT | |
| `gateway` | VARCHAR(16) NOT NULL，CHECK = `STRIPE`（`ck_payments_gateway`） | |
| `expected_amount` | DECIMAL(20,8) NOT NULL，CHECK > 0 且 `expected_amount = ROUND(expected_amount, 2)` | 客户要付的含税毛额，两位小数 |
| `expected_currency` | CHAR(3) NOT NULL，CHECK = `MYR` | |
| `wallet_credit_amount` | DECIMAL(20,8) NOT NULL，CHECK = `expected_amount` | ADR-0008 |
| `tax_policy_version_id` | BIGINT NOT NULL，FK `tax_policy_versions.id` RESTRICT | 建单时生效的税务政策版本 |
| `tax_amount` | DECIMAL(20,8) NULL，CHECK 为空 | ADR-0008：未注册 SST |
| `confirmed_amount` | DECIMAL(20,8) NULL | 可信确认时 Stripe 报的金额 |
| `confirmed_currency` | CHAR(3) NULL | |
| `gateway_payment_id` | VARCHAR(255) NULL UNIQUE | Checkout Session id（`cs_…`）；建单成功后写入，之后不变 |
| `gateway_payment_intent_id` | VARCHAR(255) NULL UNIQUE | 付款后才产生的 PaymentIntent id（`pi_…`） |
| `gateway_fee` | DECIMAL(20,8) NULL | Stripe 余额流水里的实际手续费（MYR） |
| `status` | VARCHAR(20) NOT NULL，CHECK 六种 | `PENDING`、`PAID`、`FAILED`、`EXPIRED`、`CANCELLED`、`REVIEW_REQUIRED` |
| `status_reason` | VARCHAR(64) NULL | 见「状态原因」 |
| `review_required_at` | DATETIME NULL | 进入 `REVIEW_REQUIRED` 的时刻（告警的 24 小时窗口用它） |
| `expires_at` | DATETIME NOT NULL | 建单时刻 + 待付超时 |
| `paid_at` | DATETIME NULL | |
| `last_reconciled_at` | DATETIME NULL | 每次向 Stripe 查询后更新 |
| `created_at` / `updated_at` | DATETIME NOT NULL | |

CHECK：`ck_payments_paid_fields`：`status <> 'PAID' OR (paid_at IS NOT NULL AND confirmed_amount IS NOT NULL AND confirmed_currency IS NOT NULL)`；`ck_payments_review_at`：`status <> 'REVIEW_REQUIRED' OR review_required_at IS NOT NULL`。索引：`ix_payments_tenant_created (tenant_id, created_at)`、`ix_payments_status_created (status, created_at)`、`ix_payments_reuse (tenant_id, status, expected_amount, created_at)`（60 秒复用的查找）、`ix_payments_tax_policy_version_id (tax_policy_version_id)`（外键列的显式索引，免得 MySQL 自建一个模型里没有的索引，与 0035 同一理由）、`ix_payments_review_required_at (review_required_at)`。其余约束名为 `uq_payments_public_id`、`uq_payments_gateway_payment_id`、`uq_payments_gateway_payment_intent_id`、`fk_payments_tenant_id`、`fk_payments_tax_policy_version_id` 与各 `ck_payments_<列>`。

表 `payment_gateway_events`：

| 列 | 类型 | 说明 |
| --- | --- | --- |
| `id` | BIGINT PK | |
| `gateway` | VARCHAR(16) NOT NULL，CHECK = `STRIPE` | |
| `gateway_event_id` | VARCHAR(255) NOT NULL | Stripe 事件 `evt_…` |
| `event_type` | VARCHAR(64) NOT NULL | |
| `payment_id` | BIGINT NULL，FK `payments.id` RESTRICT | 匹配到支付后写入 |
| `payload_fingerprint` | CHAR(64) NOT NULL | 原始请求体的 SHA-256，小写十六进制 |
| `verification_status` | VARCHAR(16) NOT NULL，CHECK = `VERIFIED` | 只保存验签通过的事件（见 §6）；模型里与其他状态列一样用枚举（只有 `VERIFIED` 一个值） |
| `processing_status` | VARCHAR(16) NOT NULL，CHECK | `RECEIVED`、`PROCESSING`、`RETRY`、`PROCESSED`、`IGNORED`、`UNMATCHED`、`REJECTED`（Stripe 拒绝了权威查询，例如会话不存在） |
| `attempts` | INT NOT NULL DEFAULT 0 | |
| `next_attempt_at` | DATETIME NOT NULL | 落库时等于 `received_at`（`IGNORED` 也一样） |
| `lease_until` | DATETIME NULL | |
| `last_error_code` | VARCHAR(64) NULL | `LIVEMODE_MISMATCH`、Stripe 的错误码等 |
| `fingerprint_conflict_at` | DATETIME NULL | 同一事件 id 后来收到内容不同的请求体时记下 |
| `received_at` | DATETIME NOT NULL | |
| `processed_at` | DATETIME NULL | |
| `metadata_json` | JSON NOT NULL | 只存白名单字段（见 §6），不存原始请求体 |

唯一：`uq_payment_gateway_events_event (gateway, gateway_event_id)`。索引：`ix_payment_gateway_events_due (processing_status, next_attempt_at)`、`ix_payment_gateway_events_payment_id (payment_id)`、`ix_payment_gateway_events_received (received_at)`。外键 `fk_payment_gateway_events_payment_id`；CHECK `ck_payment_gateway_events_<列>`。`processed_at` 在事件进入 `PROCESSED`、`IGNORED`、`UNMATCHED`、`REJECTED` 时写入；进入 `PROCESSED` 时清空 `last_error_code`。

### 幂等的三层

1. `(gateway, gateway_event_id)` 唯一：同一事件重复投递只落一行（ADR-0004、ADR-0006）。**它只去重投递，不足以挡重复入账**：Stripe 同一笔支付会发多个不同 id 的事件（ADR-0015），B 的补对账也没有事件 id。ADR-0007 原文「靠 event_id 唯一挡重复入账」在 Stripe 下不够，由下面两层补足。
2. `payments.gateway_payment_id`、`payments.gateway_payment_intent_id` 各自唯一：一个 Stripe 付款对象只能对应一笔内部支付（spec §118「同一网关支付号不能给两笔内部支付入账」）。
3. 账本 `(PAYMENT, payments.public_id)` 唯一：一笔内部支付至多一条 `TOPUP`（spec §74.7 第 2917 行）。`confirm_payment` 在锁住 `payments` 行后再判状态，已是 `PAID` 就什么也不做；并发情况下第二个到达者看到 `PAID` 直接返回，即使绕过也会被账本唯一约束挡住。

### Stripe 适配器（`app/payments/`）

接口照 ADR-0006（`app/payments/base.py` 定义 `PaymentGateway` 协议与数据类），本批实现 Stripe（`app/payments/stripe_gateway.py` 的 `StripeGateway(secret_key, webhook_secret, *, timeout_seconds, webhook_tolerance_seconds, transport=None)`），**同步**实现（调用方是同步的服务与 Celery 任务），HTTP 用仓库已有的 `httpx2` 的同步客户端，超时 10 秒（可配置）。`app/payments/__init__.py` 提供工厂 `get_payment_gateway(settings) -> PaymentGateway | None`：凭据读不到时返回 `None`；路由、建单与事件任务都经它取得网关，测试用 monkeypatch 替换它注入假传输：

| 方法 | Stripe 调用 | 返回 |
| --- | --- | --- |
| `create_payment(payment, *, success_url, cancel_url)` | `POST /v1/checkout/sessions`：`mode=payment`、一个价格项（MYR、`unit_amount` = 金额 × 100 的整数分、名称「Acuven AI 预付充值」）、`client_reference_id = payments.public_id`、`metadata[payment_id] = payments.public_id`、`expires_at = payments.expires_at`、`success_url` / `cancel_url`；头 `Idempotency-Key: topup-<public_id>`；**不传** `payment_method_types`（ADR-0015：由 Dashboard 开关决定） | `CreatedPayment(gateway_payment_id, redirect_url)` |
| `get_payment_status(gateway_payment_id)` | `GET /v1/checkout/sessions/{id}?expand[]=payment_intent.latest_charge.balance_transaction` | `GatewayPaymentStatus`（见下） |
| `verify_webhook(raw_body, headers, *, now)` | 本地计算：`Stripe-Signature` 的 `t` 与全部 `v1`，`HMAC-SHA256(webhook_secret, t + "." + 原始请求体)`，`hmac.compare_digest`，`|now − t| ≤ 300` 秒 | 通过或 `WebhookSignatureError` |
| `parse_webhook(raw_body)` | 本地解析 JSON | `ParsedEvent(gateway_event_id, event_type, livemode, fields)`，`fields` 是 §6 白名单字段组成的字典（直接写进 `metadata_json`；不含 Stripe 对象自己的 `metadata`） |
| `list_settlements` | 本批不实现 | B / 对账另定 |

`GatewayPaymentStatus` 的字段：`state`（下表）、`session_id`（Stripe 返回的会话 id）、`amount_minor`（`amount_total`，整数分）、`currency`（小写转大写）、`client_reference_id`、`payment_intent_id`、`payment_intent_status`、`amount_received_minor`、`fee_minor`（余额流水的 `fee`，没有就为空）、`fee_currency`、`redirect_url`（会话还开着时的 `url`）。

Checkout Session 到 `state` 的映射（ADR-0006 要求适配器定义，spec §63）：

| Session `status` | `payment_status` | PaymentIntent | `state` |
| --- | --- | --- | --- |
| `complete` | `paid` | `succeeded` 且 `amount_received = amount_total` | `PAID`（费用币种比较前先转大写） |
| `complete` | `paid` | 其他或缺失 | `INCONSISTENT`（按不可信处理，走 `REVIEW_REQUIRED`） |
| `complete` | `no_payment_required` | 任意 | `INCONSISTENT`（本系统的金额都大于 0，不该出现） |
| `complete` | `unpaid` | `requires_payment_method` 或 `canceled` | `FAILED` |
| `complete` | `unpaid` | 其他（例如 `processing`） | `OPEN` |
| `open` | 任意 | 任意 | `OPEN` |
| `expired` | 任意 | 任意 | `EXPIRED` |
| 其他取值 | | | `UNKNOWN`（不改状态，记 WARNING，事件按可重试处理） |

适配器的错误：网络错误、超时、5xx、429 → `GatewayUnavailable`（可重试）；4xx（除 429）→ `GatewayRequestRejected`（不可重试，带 Stripe 的错误码，不带请求体与密钥）。

### 可信确认：`confirm_payment(payment_public_id, *, now)`

供本批的事件处理调用，B 的补对账复用。

1. 不加锁读 `payments`；`gateway_payment_id` 为空（建单时 Stripe 调用失败）就返回「无可查询」，不改状态。
2. **事务外**调用 `get_payment_status`（不在持锁时发网络请求）。
3. 新事务：`SELECT … FOR UPDATE` 依次锁 `payments` 行、该租户的钱包行、租户行（顺序「支付 → 钱包 → 租户」，与现有「钱包 → 租户」一致；改账户状态只锁租户行，不冲突），再读一次支付状态与租户的 `account_status`、`billing_mode`。已是 `PAID` 或 `REVIEW_REQUIRED` → 只更新 `last_reconciled_at`，返回（幂等）。在锁内读租户状态，保证与并发的关户互斥：关户先提交，这里读到 `CLOSED`；这里先拿到锁，关户等这笔入账提交后再进行。
4. 按 Stripe 的 `state` 落状态（下表）；每次状态改变写一条 `PAYMENT_STATUS_CHANGE` 审计（同一事务；操作者为系统，`before` / `after` 只有 `status`、`status_reason`、`expected_amount`、`confirmed_amount`、`gateway_fee`）；不论是否改变都更新 `last_reconciled_at`。
5. 提交。`post_transaction` 在同一事务里再锁钱包、租户时拿到的是本事务已持有的锁。没有任何路径先锁钱包或租户再锁 `payments` 行（建单在租户锁内只插入新行、不锁已有支付行；写网关 id 的事务不持租户锁）。

`state = PAID` 时的判定（spec §43 的六条：签名已在落库时验过；其余五条在这里逐一判定），**全部在写入之前、按表的顺序判定，第一条不满足的决定原因**：

| 条件 | 不满足时 |
| --- | --- |
| `client_reference_id` 等于 `payments.public_id`，且 `session_id` 等于 `gateway_payment_id` | `REVIEW_REQUIRED`，原因 `REFERENCE_MISMATCH` |
| 币种为 `MYR` | `REVIEW_REQUIRED`，`CURRENCY_MISMATCH` |
| `amount_minor` 等于 `expected_amount × 100`（精确整数比较） | `REVIEW_REQUIRED`，`AMOUNT_MISMATCH` |
| `payment_intent_id` 没有被另一笔内部支付占用：在锁内查 `payments.gateway_payment_intent_id = 该值 AND id <> 本行`（唯一索引），查到即占用 | `REVIEW_REQUIRED`，`GATEWAY_PAYMENT_REUSED` |
| 租户 `account_status` 不是 `CLOSED` | `REVIEW_REQUIRED`，`TENANT_CLOSED` |
| 租户 `billing_mode` 是 `PREPAID` | `REVIEW_REQUIRED`，`INTERNAL_TENANT` |
| 全部满足 | `PAID`：写 `confirmed_amount`、`confirmed_currency`、`paid_at = now`、`gateway_payment_intent_id`、`gateway_fee`（拿得到且币种为 MYR 时，`fee_minor / 100`；拿不到为空、不记日志；币种不是 MYR 时为空并记 WARNING）；调用 `post_transaction(transaction_type=TOPUP, amount=wallet_credit_amount, reference_type=PAYMENT, reference_id=public_id, …)`。`DISABLED` 租户照常入账 |

第四条的占用查询**不论前面几条结果如何都先做**（`pi_taken`）。进入 `REVIEW_REQUIRED` 时写 `review_required_at = now`，并照样记下 Stripe 报的 `confirmed_amount`、`confirmed_currency`（供人工复核）；`gateway_payment_intent_id` **只在 `pi_taken` 为假时写入**（不论原因是哪一条），所以复核分支不会因写 PI id 撞唯一约束。进入 `PAID` 时清空 `status_reason`（例如原来的 `GATEWAY_EXPIRED`）。若仍有并发者先占了同一个 PI，写入撞唯一约束：整个事务回滚、支付与账本都不变，调用方按「可重试」处理（事件进入 `RETRY`），下一次判定时第四条查到占用而进入 `REVIEW_REQUIRED`。

其他 `state`：

| 当前状态 | `state` | 新状态 |
| --- | --- | --- |
| `PENDING` / `EXPIRED` / `FAILED` / `CANCELLED` | `PAID`（判定通过） | `PAID`（ADR-0007：过期或失败后到账照样入账） |
| `PENDING` | `FAILED` | `FAILED`，原因 `GATEWAY_FAILED` |
| `PENDING` | `EXPIRED` | `EXPIRED`，原因 `GATEWAY_EXPIRED` |
| `EXPIRED` / `FAILED` / `CANCELLED` | `FAILED` / `EXPIRED` | 不变 |
| 任意未终结 | `INCONSISTENT` | `REVIEW_REQUIRED`，原因 `GATEWAY_INCONSISTENT` |
| 任意 | `OPEN` / `UNKNOWN` | 不变 |
| `PAID` / `REVIEW_REQUIRED` | 任意 | 不变（见第 3 步） |

`CANCELLED` 本批没有产生路径（预留给以后客户主动取消）。`PAID` 与 `REVIEW_REQUIRED` 都不会被本函数改变；`REVIEW_REQUIRED` 永不自动入账（spec §63），由 B 的人工处理出口改变。

`confirm_payment` 的返回值带 `state`（Stripe 的权威状态或「无可查询」），调用方据此决定事件的结果。

**状态原因** `status_reason` 的取值：`REFERENCE_MISMATCH`、`CURRENCY_MISMATCH`、`AMOUNT_MISMATCH`、`GATEWAY_PAYMENT_REUSED`、`TENANT_CLOSED`、`INTERNAL_TENANT`、`GATEWAY_INCONSISTENT`、`GATEWAY_FAILED`、`GATEWAY_EXPIRED`、`GATEWAY_CREATE_FAILED`（见建单）。

### 建单：`POST /api/v1/customer/topups`

- 开关关闭（`BILLING_PAYMENTS_ENABLED` 为假）时路由不注册，返回 404。
- 鉴权：客户门户的访问令牌，租户来自令牌主体；请求体 `{"amount": "100.00"}`（多余字段 422）。
- 校验，**按以下顺序**，任何一项不过都不建单：
  1. 账户可写：沿用现有的客户写规则——路由用 `require_customer_writable` 提前拒绝，服务在写事务里用 `lock_tenant_for_customer_write` 锁租户行并重查；`DISABLED` / `CLOSED` → 403 `ACCOUNT_READ_ONLY`（spec §112.1 拒绝新充值）。
  1a. 开放：`payment_topups_open` 为假且租户的 `public_id` 不在 `payment_topup_tenant_allowlist` 里 → 403 `TOPUPS_NOT_OPEN`。
  2. 租户 `billing_mode` 为 `PREPAID` → 否则 403 `TOPUP_NOT_ALLOWED`。
  3. 金额是字符串，匹配 `^[0-9]{1,6}(\.[0-9]{1,2})?$`（Stripe 单笔上限 999,999.99），不小于最低额（配置，缺省 `10.00`，不得小于 `2.00`，ADR-0015 的 MYR 下限）、不大于最高额（配置，缺省不设；设了时不得小于最低额、不得大于 `999999.99`）→ 否则 422 `TOPUP_AMOUNT_INVALID`；缺字段、不是字符串也是这个错误码。
  4. 按建单时刻解析税务政策（`resolve_tax_policy`，`SST` / 马来西亚）：没有生效版本或解析抛错 → 503 `TAX_POLICY_UNAVAILABLE`；生效版本要求收税（与 ADR-0008 的「未注册、不收税」不符）→ 503 `TAX_POLICY_UNSUPPORTED`（收税的处理另过设计闸门）。
- **60 秒同额复用**（ADR-0007），分两步：
  1. 锁外预查：查同租户、同金额、`PENDING`、`created_at ≥ now − 60 秒`、且 `expires_at − now ≥ 31 分钟` 的最新一笔（候选）。候选已有 `gateway_payment_id` 时，在事务外 `get_payment_status`：`OPEN` 且有 `redirect_url` → 直接返回它（复用完成）；其他状态或 `GatewayRequestRejected` → 记下候选 id 为「排除」；`GatewayUnavailable` → 503 `PAYMENT_GATEWAY_UNAVAILABLE`。
  2. 锁内：`lock_tenant_for_customer_write` 锁租户行，重跑同一个查询并排除第 1 步记下的 id；找到 → 复用这一笔（它没有网关 id 时走下面的「调 Stripe」，有网关 id 时视为第 1 步漏掉的并发结果，同样走事务外查询）；找不到 → 新建。并发的两个请求在租户锁上排队，后到者会找到先到者刚插入的一笔而复用它。
- 新建：事务一插入 `PENDING`（`expires_at = now + 待付超时`，缺省 3600 秒、可配置 2400–86400），提交。Stripe 要求会话过期时刻在创建时刻之后至少 30 分钟、至多 24 小时；下限取 2400 秒、复用要求剩余 ≥ 31 分钟，保证从插入到调用 Stripe（含 60 秒内的重试）仍在区间内。
- 调 `create_payment`（在事务外，不持任何锁）：成功 → 事务二用条件更新 `gateway_payment_id IS NULL` 写入网关 id，并清空 `status_reason`；影响 0 行说明并发的复用请求已写，读出比较两者相同即可；返回 201 `{"payment_id", "redirect_url", "expires_at"}`。条件更新影响 0 行而库里已有的网关 id 与这次 Stripe 返回的不同（同一幂等键正常不会出现）→ 记 ERROR、返回 503 `PAYMENT_GATEWAY_UNAVAILABLE`，客户重试时复用库里那一笔。
- Stripe 不可用或拒绝：用条件更新（`gateway_payment_id IS NULL`）写 `status_reason = GATEWAY_CREATE_FAILED`，支付保持 `PENDING`；Stripe 因同一幂等键正被并发使用而拒绝（`idempotency_key_in_use`）时不写原因；返回 503 `PAYMENT_GATEWAY_UNAVAILABLE`（可重试）；60 秒内的重试会复用这一笔、用同一个 `Idempotency-Key` 再调一次 Stripe，不会在 Stripe 那边产生第二个会话。没有网关 id 的 `PENDING` 由 B 置为过期。
- `success_url` / `cancel_url` 由配置的 `frontend_base_url` 拼出：`{frontend_base_url}/portal/payments/{public_id}?result=success`（或 `cancelled`）。开关打开而 `frontend_base_url` 为空时，建单返回 503 `PAYMENTS_MISCONFIGURED`。跳回门户本身不改任何状态（spec §43 第 1822 行）。

### 支付 Webhook：`POST /api/v1/webhooks/payments/{gateway}`

- 开关关闭时 404；`gateway` 不是 `stripe` 时 404 `NOT_FOUND`；开关打开但凭据读不到时 503 `PAYMENTS_MISCONFIGURED`（Stripe 会重试，告警见下）。错误码：400 `WEBHOOK_SIGNATURE_INVALID` / `WEBHOOK_PAYLOAD_INVALID`、413 `PAYLOAD_TOO_LARGE`、503 `PAYMENTS_MISCONFIGURED` / `SERVICE_UNAVAILABLE`，响应体用仓库现有的错误信封。
- 自己读原始字节，不让框架先解析 JSON；请求体上限 64 KiB（超过 413，不落库）。
- 验签失败或请求头缺失、格式不对、时间窗外：400，**不落库**，记一条 WARNING（不带请求体、请求头原文与密钥）。
- 解析失败（不是 JSON、缺 `id` / `type`）：400，不落库。
- `livemode` 与配置的密钥模式（`sk_live_` / `sk_test_` 前缀）不一致：落库为 `IGNORED`、原因 `LIVEMODE_MISMATCH`，返回 200。
- 落库：`processing_status`——四种处理的事件类型为 `RECEIVED`，其余类型为 `IGNORED`；`metadata_json` 只存白名单（§6）。同一 `(gateway, gateway_event_id)` 已存在：指纹相同 → 200；指纹不同 → 记 `fingerprint_conflict_at`、ERROR 日志、告警，返回 200，不覆盖原行。
- 数据库不可用：503，Stripe 会重试。
- 订阅并处理的事件类型：`checkout.session.completed`、`checkout.session.async_payment_succeeded`、`checkout.session.async_payment_failed`、`checkout.session.expired`。Stripe Dashboard 的回调端点只订阅这四种（ADR-0015「收口条件」）。

### 事件处理任务

- Beat 每 15 秒（可配置；条目名 `payment-events-process-due`，`expires` 为间隔减 1 秒）执行 `app.tasks.payment_events.process_due`（开关为假或 `get_payment_gateway` 返回 `None` 时直接返回、不领取事件，配置告警见下）。**同一时刻只跑一份**：开始时用 MySQL 的 `GET_LOCK(CONCAT('payment_events:', DATABASE()), 0)` 取命名锁（照 `app/ai_api/delivery.py` 的写法；SQLite 下跳过），取不到就直接返回，避免每轮预算 45 秒、间隔 15 秒造成多份并跑占满 worker。每轮最多处理 50 个事件，预算 45 秒；**一次领取一个**（每次一个短事务：按 `next_attempt_at` 升序用 `FOR UPDATE SKIP LOCKED` 取一个到期的 `RECEIVED` / `RETRY` 或租约过期的 `PROCESSING`，置 `PROCESSING`、租约 60 秒、`attempts + 1`，提交），处理完再领下一个；预算用完就停，不留下已领未处理的事件。
- 处理一个事件：
  1. 按 `metadata_json.client_reference_id` 找 `payments`（`public_id`）；没有 `client_reference_id` 或找不到 → `UNMATCHED`（不改任何支付，计入告警）。
  2. 写 `payment_id`。**认回会话**：支付的 `gateway_payment_id` 为空（建单时 Stripe 超时或写库失败，但会话其实已建好），而事件带会话 id（`metadata_json.object_id`）时，用条件更新（`gateway_payment_id IS NULL`）把它写入支付行；撞唯一约束（该会话 id 已属于另一笔支付）→ 事件 `REJECTED`、原因 `GATEWAY_REFERENCE_TAKEN`。事件带签名，`client_reference_id` 已对上本行，认回是安全的；之后的权威查询还会再核对 `client_reference_id` 与 `session_id`。
  3. 调 `confirm_payment`。
  4. 成功 → `PROCESSED`（清空 `last_error_code`）；仍是「无可查询」（事件没带会话 id）：支付已过 `expires_at` → `REJECTED`、原因 `NO_GATEWAY_REFERENCE`，否则 → `RETRY`；`GatewayUnavailable`、`state = UNKNOWN` 或确认事务撞唯一约束 → `RETRY`，`next_attempt_at = now + min(3600, 2^min(attempts, 12)) × uniform(0.5, 1.0)` 秒，**不放弃**；`GatewayRequestRejected`（例如会话不存在）→ `REJECTED`，`last_error_code` 记 Stripe 错误码（不改支付状态，计入告警）。
- 同一笔支付的多个事件被并发处理时，第三层幂等保证只入账一次。

### 告警（并进已有维度）

Healthchecks 已用满 20 个检查（Kelvin 2026-10-17 定：新信号并进已有维度）。支付的信号并进 `wallet_negative_balance` 维度（都是「钱包入账需要人看」的问题），作为它的几个新判据：

| 判据 | 级别 |
| --- | --- |
| `review_required_at` 在最近 24 小时内的支付 | P1 |
| `fingerprint_conflict_at` 在最近 24 小时内的事件 | P1 |
| `RECEIVED` / `RETRY` / `PROCESSING` 事件里最早的 `received_at` 已超过 15 分钟 | P2 |
| `processed_at` 在最近 24 小时内、状态为 `UNMATCHED` 或 `REJECTED` 的事件 | P2 |
| `payments_enabled` 打开但两个密钥文件有一个读不到、Stripe 密钥前缀不是 `sk_live_` / `sk_test_`、webhook 签名密钥前缀不是 `whsec_`、`stripe_api_base_url` 为空、或 `frontend_base_url` 为空 | P1 |

窗口一律是 `now − 24 小时 < 时刻 ≤ now`（晚于 now 的不计）。判据从 `get_settings()` 读开关与配置，不改 `AlertContext`。追加的摘要段落逐字为 `; payments: review 24h <a>, event conflicts 24h <b>, oldest open event <c>s, unmatched or rejected 24h <d>, config <ok|missing>`（`<c>` 没有未处理事件时为 0）。支付相关测试在每个用例后清理自己写的支付与事件行，免得 MySQL 上共用库的其他告警用例看到支付段落。**摘要只在支付的任一计数大于 0 或配置判据触发时追加支付一段**，原有判据、级别与摘要文字不变（现有测试比较整段摘要）。`docs/runbook.md` 与 `docs/api.md` 里这个维度的说明同步补上支付判据。

`REVIEW_REQUIRED` 只报进入后的 24 小时（不是一直报）：v1 没有处理出口（属于 B），一直报会让这个维度长期处于 P1，掩盖之后的新问题（与月度对账单 #565 定的同一规则）。

### 配置（`app/core/config.py`）

| 键 | 缺省 | 说明 |
| --- | --- | --- |
| `payments_enabled` | `False` | 回调与后台处理；生产默认关 |
| `payment_topups_open` | `False` | 客户建单对所有合格租户开放；B 上线前保持关 |
| `payment_topup_tenant_allowlist` | 空 | 逗号分隔的租户 `public_id`；`payment_topups_open` 为假时只有它们能建单（生产端点复验用） |
| `stripe_secret_key_file` | `""` | Docker secret 文件路径；只在开关打开时读取 |
| `stripe_webhook_secret_file` | `""` | 同上；这是**生产端点自己的**签名密钥（ADR-0015） |
| `stripe_api_base_url` | 空 | Stripe API 的 https 根地址，**仓库里不写默认值**（ADR-0001：公开仓库不写第三方主机名，除非单独批准登记）；开启支付时由管理员在环境变量里设置；格式校验与 `fx_bnm_base_url` 相同；开关打开而它为空视为未配置 |
| `stripe_api_timeout_seconds` | 10 | 1–60 |
| `payment_topup_min` | `"10.00"`（Decimal） | 不得小于 `2.00` |
| `payment_topup_max` | 空（Decimal 或空） | 设了时不得小于最低额 |
| `payment_pending_timeout_seconds` | 3600 | 2400–86400 |
| `payment_reuse_window_seconds` | 60 | ADR-0007；0–300 |
| `payment_webhook_tolerance_seconds` | 300 | 60–600 |
| `payment_event_poll_seconds` | 15 | 5–300 |

密钥读取：开关打开时读两个文件，去掉首尾空白；任一文件读不到或为空、Stripe 密钥前缀不是 `sk_live_` / `sk_test_`、webhook 签名密钥前缀不是 `whsec_`、或 `stripe_api_base_url` 为空，视为未配置（建单与回调返回 503、告警 P1）；`livemode` 由密钥前缀决定。

本批**不改** docker-compose 文件：新增 secret 时宿主机文件不存在会让生产部署失败。开启支付时由管理员先放宿主机文件、再提交加 secret 的 PR（§8）。`tests/backend/test_compose.py` 要求每个 `*_file` 配置都接进 compose；本批把两个 Stripe 密钥登记为「开关默认关、开启时由管理员接入」的例外，用例同时断言它们缺省为空、只在开关打开时读取。

### 事务边界

- 建单：事务一（插入 `PENDING`）→ 事务外调 Stripe → 事务二（写网关 id）。
- 回调：一个事务插入事件后提交，再返回。
- 确认：事务外查 Stripe → 一个事务锁支付行、改状态、入账、审计、提交。
- 事件处理：领取一个事务；处理（含确认）；落事件结果一个事务（与确认的事务分开：确认已提交而落事件结果失败时，事件租约到期后被重领、再确认一次，第 3 步的幂等保证不重复入账）。

### 时间语义

- 全部时刻是不带时区的 UTC、精确到秒（与仓库一致）。
- 回调验签的时间窗用 Stripe 签名头里的 `t`，与服务端当前时刻比较。
- `paid_at` 是本系统做出可信确认的时刻，不是 Stripe 的付款时刻（Stripe 的时刻进 `metadata_json`）。

### 金额与精度

- 金额一律 `Decimal`；`expected_amount` 两位小数；与 Stripe 比较时换算成整数分（`expected_amount × 100`，精确为整数），不经 `float`。
- 入账额 = `wallet_credit_amount` = `expected_amount`；手续费不影响入账额（spec §7、§44）。
- `gateway_fee` = `fee_minor / 100`（Decimal）；Stripe 报的手续费币种不是 MYR 时不写、记 WARNING。

## 3. 不变量影响矩阵

| 不变量 | 是否触碰 | 可能怎样被破坏 | 设计控制 | 验证方式 |
| --- | --- | --- | --- | --- |
| INV-1 中心故障不中断 AI | 否 | 支付不在 AI 调用路径上 | — | — |
| INV-2 事件不重复扣费 | 否 | 不碰用量扣费 | — | — |
| INV-3 支付不重复入账 | **是** | 同一支付的多个事件、重复投递、并发处理、补对账与回调同时到达 | 三层唯一（事件 id、网关支付号与 PI id、账本引用）；锁支付行后再判；`PAID` 不再变 | §7「重复回调」「多事件并发」「同一网关支付号两笔内部支付」 |
| INV-4 余额只经账本变动 | 是 | 直接改钱包 | 只调 `post_transaction` | §7「入账走账本」 |
| INV-5 历史账本不可变 | 是 | 修正入账去改旧行 | 本批只新增 TOPUP；更正走 B 的人工出口与调账 | 现有账本测试 |
| INV-6 事件保留版本引用 | 是（税务） | 支付不记当时的税务政策 | `tax_policy_version_id` NOT NULL，建单时解析 | §7「建单记税务版本」 |
| INV-7 客户不可见成本毛利 | 是 | 客户接口返回手续费 | 客户接口只返回 `payment_id`、`redirect_url`、`expires_at` | §7「客户接口字段」 |
| INV-8 租户不可互访 | 是 | 用别的租户的支付入账 | 租户来自令牌主体；确认时以 `payments.tenant_id` 入账；`client_reference_id` 必须等于本行 `public_id` | §7「跨租户」 |
| INV-9 对话内容不入库 | 否 | — | — | — |
| INV-10 金额用 Decimal | 是 | 浮点比较金额 | 整数分比较，Decimal 存储 | §7「金额精度」 |
| INV-11 event_id 至多一次财务效果 | 是 | 同一事件处理两次入账两次 | 事件唯一 + 第三层幂等 | §7「事件重领」 |
| INV-12 定稿对账单不可变 | 否 | 入账进下一期对账单由对账单设计处理 | — | — |
| INV-13 状态与事件原子提交 | 是 | 支付置 `PAID` 而账本未写，或反过来 | 改状态、入账、审计同一事务 | §7「事务中途失败」 |
| INV-14 队列丢失不毁持久工作 | 是 | Celery 丢任务导致事件不处理 | 事件落库后由 Beat 扫描，不靠入队；租约过期重领 | §7「租约过期重领」 |

## 4. 状态与并发

### 支付状态跃迁

见 §2「可信确认」的两张表与建单。合法跃迁汇总：

| 当前状态 | 事件 | 新状态 | 副作用 |
| --- | --- | --- | --- |
| （无） | 建单校验通过 | `PENDING` | — |
| `PENDING` | 建单时 Stripe 失败 | `PENDING`（`status_reason = GATEWAY_CREATE_FAILED`） | — |
| `PENDING` / `EXPIRED` / `FAILED` / `CANCELLED` | 确认：`PAID` 且六条满足 | `PAID` | TOPUP、审计 |
| 任意非终结 | 确认：`PAID` 但任一条不满足，或 `INCONSISTENT` | `REVIEW_REQUIRED` | 审计、告警 |
| `PENDING` | 确认：`FAILED` | `FAILED` | 审计 |
| `PENDING` | 确认：`EXPIRED` | `EXPIRED` | 审计 |
| `PAID` / `REVIEW_REQUIRED` | 任意确认 | 不变 | — |

「终结」只指 `PAID` 与 `REVIEW_REQUIRED`（后者由 B 的人工出口改变）。

### 并发

- **串行化**：同一支付的确认靠 `payments` 行的 `FOR UPDATE`；建单的 60 秒复用靠锁租户行；事件领取靠 `SKIP LOCKED` 与租约。
- **数据库负责的唯一性**：`payments.public_id`、`gateway_payment_id`、`gateway_payment_intent_id`；事件 `(gateway, gateway_event_id)`；账本 `(reference_type, reference_id)`。
- **幂等键**：Stripe 建单 `Idempotency-Key = topup-<public_id>`；入账 `(PAYMENT, public_id)`。
- **相同 id、不同载荷**：事件指纹不同 → 不覆盖、告警。
- **重试在哪一层**：事件处理任务（退避、不放弃）；建单由客户重试（复用同一笔）。

## 5. 失败与恢复矩阵

| 失败点 | 对外结果 | 数据最终状态 | 是否可重试 | 恢复来源 | 告警 |
| --- | --- | --- | --- | --- | --- |
| 建单校验失败 | 4xx | 不建单 | 否 | — | — |
| 建单时 Stripe 不可用 | 503 | `PENDING`、无网关 id | 是（60 秒内复用重试） | 客户重试；B 过期处理 | — |
| 回调验签失败 | 400 | 不落库 | Stripe 自己重试 | 修配置的签名密钥 | WARNING 日志 |
| 回调落库时数据库不可用 | 503 | 不落库 | 是 | Stripe 重试 | — |
| 事件处理时 Stripe 不可用 | — | 事件 `RETRY` | 是，不放弃 | 事件任务 | 超过 15 分钟 P2 |
| 确认的事务中途失败 | — | 支付与账本都不变 | 是 | 事件租约到期重领 | — |
| 确认已提交、落事件结果失败 | — | 支付已 `PAID`，事件 `PROCESSING` | 是 | 重领后确认是空操作 | — |
| 金额 / 币种 / 引用不符 | — | `REVIEW_REQUIRED`，不入账 | 否 | B 的人工出口 | P1（24 小时） |
| 租户已关户或是内部租户 | — | `REVIEW_REQUIRED`，不入账 | 否 | B 的人工出口 | P1（24 小时） |
| 未知支付（找不到 `client_reference_id`） | — | 事件 `UNMATCHED` | 否 | 人工排查 | P2 |
| 建单时 Stripe 超时或写网关 id 失败，但会话已建好 | 建单 503 | 支付 `PENDING`、无网关 id | 是 | 该会话的任一事件把会话 id 认回支付行，再走权威查询；事件不带会话 id 且支付已过期 → 事件 `REJECTED`（`NO_GATEWAY_REFERENCE`） | 后者 P2 |
| 同一事件 id 内容不同 | 200 | 原行不变 | — | 人工排查 | P1 |
| 丢失回调 | — | 支付一直 `PENDING` | 是 | **B 的补对账**（本批不覆盖） | B 定 |
| Redis/Celery 丢失 | — | 事件仍在库里 | 是 | Beat 扫库 | 积压 P2 |
| 重复请求（建单） | 201，同一笔 | 一行 | — | — | — |

## 6. 数据与安全边界

- **租户过滤**：建单的租户来自客户令牌主体，请求体不接受租户；回调不带租户，以 `payments.tenant_id` 为准；确认时核对 `client_reference_id` 等于本行 `public_id`。
- **鉴权主体**：建单是客户门户令牌；回调靠 Stripe 签名（端点不要求登录）。
- **客户侧禁止返回的字段**：手续费、状态原因与 Stripe 对象 id 不作为字段返回给客户；`redirect_url` 是 Stripe 托管页的地址，其中自带会话 id，这是付款所必需的，不算泄露。
- **密钥**：`stripe_secret_key_file`、`stripe_webhook_secret_file` 由 Docker secret 挂成文件，开关打开时读取；不进环境变量、不进日志与异常消息、不进审计；轮换 = 在 Stripe 生成新密钥、替换宿主机文件、重启（webhook 签名密钥在 Stripe 可设过渡期，过渡期内新旧并存，本批只配一个：轮换时先换文件再在 Stripe 让旧密钥失效）。
- **日志**：允许 `payments.public_id`、事件 id、事件类型、状态、原因码、Stripe 错误码；禁止请求体、签名头原文、密钥、客户邮箱与姓名。
- **事件的白名单字段**（`metadata_json`）：`type`、`livemode`、`created`、`object_id`（会话 id）、`client_reference_id`、`payment_status`、`status`、`amount_total`、`currency`、`payment_intent`（id）。**不存原始请求体**：Stripe 的 Checkout Session 里有客户邮箱、姓名等个人数据（PDPA），金额不符时人工复核需要的信息都在白名单里，原始对象可以按会话 id 向 Stripe 查。这是对 spec §43「保存原始事件」的有意偏离，理由是个人数据最小化。
- **审计**：新增 `PAYMENT_STATUS_CHANGE`；`before` / `after` 只有状态、原因、金额、`gateway_fee`。
- **数据保留**：`payments` 与事件长期保留（财务记录）。

## 7. 测试证据计划

| 风险/需求 | 测试层级 | 场景 | 预期结果 | 任务 |
| --- | --- | --- | --- | --- |
| 迁移与约束 | integration（MySQL） | 建表、唯一、CHECK（状态集合、币种、`wallet_credit_amount = expected_amount`、`tax_amount` 为空、`PAID` 必填字段） | 非法插入被拒；降级可回 | 172 |
| 适配器建单 | unit（假传输） | 请求体字段、`Idempotency-Key`、不带 `payment_method_types`、整数分 | 与 §2 一致 | 173 |
| 状态映射全表 | unit | §2 映射表每一行 | 对应 `state` | 173 |
| 验签 | unit | 正确签名；改一个字节；多个 `v1` 其中一个对；时间窗外 301 秒；恰好 300 秒；缺头 | 只有合规的通过 | 173 |
| 适配器错误分类 | unit | 超时、5xx、429、404 | 可重试与不可重试正确 | 173 |
| 可信确认决策表 | integration | §2 两张表每一行（含 `DISABLED` 入账、`CLOSED` 与内部租户复核、`EXPIRED` 后到账入账） | 状态、原因、是否入账与表一致 | 174 |
| 金额精度 | unit | `100.00` 对 10000 分；`99.99` 对 9999；`100.01` 对 10000 | 前两组通过，第三组 `AMOUNT_MISMATCH` | 174 |
| 入账走账本、复机 | integration | 余额为负的暂停租户付款成功 | 一条 TOPUP、余额正确、计费状态 `ACTIVE`、复机 outbox 与审计 | 174 |
| 多事件并发 | integration（MySQL） | 同一支付的两个事件由两个线程同时确认 | 只有一条 TOPUP | 174 |
| 同一网关支付号两笔内部支付 | integration | 两笔支付的 Stripe 状态指向同一个 PI | 第二笔 `REVIEW_REQUIRED`，不入账 | 174 |
| 事务中途失败 | integration | 入账时抛错 | 支付仍是原状态、无账本行 | 174 |
| 回调验签与落库 | integration（API） | 合规、错签名、超大、非 JSON、`livemode` 不一致、重复相同、重复不同 | 200/400/413 与落库结果一致；指纹冲突记下 | 177 |
| 伪造成功跳转 | integration（API） | 只访问跳回地址、不发回调 | 支付仍 `PENDING`，无账本行 | 177 |
| 认回会话 | integration | 支付无网关 id，收到带该会话 id 的 `checkout.session.completed`；另一组 `checkout.session.expired`；另一组事件不带会话 id、支付已过期；另一组会话 id 已属于另一笔支付 | 前两组认回 id 后分别 `PAID` 入账与 `EXPIRED`，事件 `PROCESSED`；第三组事件 `REJECTED`（`NO_GATEWAY_REFERENCE`）；第四组事件 `REJECTED`（`GATEWAY_REFERENCE_TAKEN`），支付不变 | 175 |
| 事件处理与重试 | integration | Stripe 不可用、会话 404、未知 `client_reference_id`、租约过期重领、凭据读不到 | `RETRY` 退避、`REJECTED` 带错误码、`UNMATCHED`、重领后不重复入账、凭据读不到时不领取 | 175 |
| 端到端 | integration（API + 假 Stripe） | 建单 → 回调 → 处理 → 再收一个同支付的不同事件 → 再重复投递 | 钱包只 +金额一次；支付 `PAID`；`gateway_fee` 写入 | 177 |
| 建单校验 | integration（API） | `payments_enabled` 关闭 404；`payment_topups_open` 关闭时非名单租户 403 `TOPUPS_NOT_OPEN`、名单租户可建单；金额格式与上下限；内部租户 403；`DISABLED` / `CLOSED` 403 `ACCOUNT_READ_ONLY`；无税务政策 503；60 秒复用；Stripe 失败 503 后重试复用同一笔并用同一个幂等键 | 与 §2 一致 | 176、177 |
| 客户接口字段 | integration | 建单响应 | 只有三个字段 | 177 |
| 跨租户 | integration | 租户 A 的令牌建单只为 A 建单（请求体带别的租户字段被 422 拒绝）；回调的 `client_reference_id` 指向 A 的支付时只给 A 入账、B 的钱包不变 | 互不影响 | 177 |
| 关户与入账互斥 | integration（MySQL） | 确认持锁期间另一个连接把租户改成 `CLOSED` | 关户等入账提交；若关户先提交，确认读到 `CLOSED` 而进入 `REVIEW_REQUIRED` | 174 |
| 复核记录 | integration | 金额不符进入 `REVIEW_REQUIRED`；另一组：PI 已被另一笔占用且币种也不符 | `review_required_at`、`confirmed_amount`、`confirmed_currency` 写入；第二组原因为 `CURRENCY_MISMATCH`、不写 PI id、不撞唯一约束、不进入重试 | 174 |
| 过期后到账清原因 | integration | `EXPIRED`（原因 `GATEWAY_EXPIRED`）的一笔收到 `PAID` | `PAID`、`status_reason` 为空 | 174 |
| 复用排除 | integration | 60 秒内的候选会话已不是 `OPEN`；两个并发请求 | 新建一笔且只新建一笔，后到者复用它 | 176 |
| 待付超时与 Stripe 区间 | integration | 超时配成 2400 秒；复用一笔剩余不足 31 分钟的单 | 新建一笔；发给 Stripe 的过期时刻在创建后 30 分钟到 24 小时之间 | 176 |
| 并发复用同一幂等键 | integration | 两个请求在 60 秒内复用同一笔，Stripe 对后一个回 `idempotency_key_in_use` | 后一个 503、不写 `GATEWAY_CREATE_FAILED`；先一个写入网关 id 并清空原因 | 176 |
| compose 用例的例外 | unit | `test_compose.py` | 两个 Stripe 密钥不要求接 compose，但缺省为空、只在开关打开时读取 | 173 |
| 告警判据 | unit | §2 告警表每一行与 24 小时窗口边界；没有任何支付数据时 | 级别正确；没有支付数据时摘要与原来逐字相同 | 175 |
| 密钥不泄露 | unit | 适配器异常、日志、审计 | 不含密钥与请求体 | 173、175 |
| Redis/Celery 丢失 | integration | 不经 Celery，直接调用扫描函数 | 事件被处理 | 175 |

## 8. 迁移与上线

- **迁移 0036**（`down_revision` 为 `0035_monthly_statements`）：只建两张新表，不改已有表、不回填。两张都是全新的表，upgrade 只有两种情形：都不存在 → 建表；任一已存在 → 抛错、不发 DDL，消息点名那张表（不做逐项形状比较，生产上只会执行一次）。离线 `--sql` 输出建表语句。downgrade：离线模式抛错；表不存在跳过；任一表有行抛错且不删行；都为空时先删事件表再删 `payments`。
- **部署顺序**：合并即部署；开关默认关，生产上不注册两个路由、Beat 任务空跑（没有事件）。
- **开启支付（不在本批，管理员按 `docs/payment-flow.md` 执行）**：
  1. 在 Stripe 生产账户（ADR-0015「收口条件」）确认提现为自动、FPX 已开启。
  2. 在 Stripe Dashboard 建回调端点，只订阅 §2 的四种事件，得到这个端点自己的签名密钥。
  3. 把 Stripe 密钥与端点签名密钥放到宿主机 secret 文件，再提交加 `secrets:` 的 PR（先放文件、后合并，避免部署失败）。
  4. 设置 `BILLING_PAYMENTS_ENABLED=true`、`BILLING_FRONTEND_BASE_URL`、`BILLING_STRIPE_API_BASE_URL`（Stripe API 的 https 根地址），并把生产验收租户的 `public_id` 放进 `BILLING_PAYMENT_TOPUP_TENANT_ALLOWLIST`（`payment_topups_open` 保持为假，其他客户仍不能建单）；在生产端点复验 ADR-0015 的两项：先把宿主机上的 webhook 签名密钥文件临时换成另一个格式正确（`whsec_` 开头）但不是这个端点的值并重启（回调一律 400），用生产验收租户做一笔 RM10 的真实充值（最低额）；在 Stripe Dashboard 看到投递失败后换回正确的密钥并重启，等 Stripe 自动重试成功；核对重试投递的事件 id 与第一次相同、支付已 `PAID`、钱包只加一次。这笔 RM10 留在验收租户的钱包里，不冲回。由 Kelvin 执行。
  5. 复验完成后从允许名单里移除验收租户。对客户开放（`BILLING_PAYMENT_TOPUPS_OPEN=true`）之前，B（补对账与人工复核）必须已上线。
- **回滚**：关开关即可；已有支付与账本行保留。

## 9. 方案取舍

| 方案 | 优点 | 风险 | 未采用原因 |
| --- | --- | --- | --- |
| 采用：Checkout Session（托管付款页） | 卡与 FPX 都由 Stripe 页面处理；门户不加载第三方脚本；符合 ADR-0006 的「返回跳转地址」 | PaymentIntent 付款后才产生，权威查询要查会话再展开 PI | — |
| 备选 A：PaymentIntent + 门户内嵌 | 体验一体 | 前端加载 Stripe 脚本、改内容安全策略；工作量大 | Kelvin 2026-11-06 选托管页 |
| 备选 B：凭回调里的状态直接入账 | 少一次网络请求 | 回调只是提示（spec §43、ADR-0015）；回调内容可能过时 | 违反 spec §43 |
| 备选 C：以事件 id 作入账幂等键 | 简单 | Stripe 同一支付多个事件 id，会重复入账 | 违反 Invariant 3 |
| 备选 D：保存原始请求体 | 复核信息最全 | 含客户个人数据 | 白名单字段已够复核，原始对象可向 Stripe 查 |
| 备选 E：新增独立的告警维度 | 语义清楚 | Healthchecks 已满 20 个 | Kelvin 2026-10-17 定不新增检查 |

## 10. 未决问题与假设

- **未决问题**：无。
- **假设**：
  1. Stripe 在 Checkout 会话付款成功时发 `checkout.session.completed`（卡）或先 `completed` 再 `async_payment_succeeded`（异步方式）；FPX 在 Checkout 中是同步完成的。即使假设不成立，确认以权威查询为准，事件只是触发。
  2. Checkout Session 展开 `payment_intent.latest_charge.balance_transaction` 在付款成功后即可得到手续费；拿不到时 `gateway_fee` 为空，由 B 补。
  3. 生产与 CI 的 Stripe 调用在测试中全部替换为假传输；真实沙箱的端到端验证在开启前由管理员按 §8 完成。
- **如果假设错误**：1 → 只是确认时机变化，不影响正确性；2 → `gateway_fee` 暂缺，不影响入账。

## 11. 审查与版本绑定

审查方要回答的五个问题常驻在 [scripts/review_checklist.md](../../scripts/review_checklist.md) 的「设计审查」一节，此处不重复。

**版本绑定规则**（规则的出处是 [WORKFLOW §3](../../docs/WORKFLOW.md)；这里保留一份是因为审查材料里只有本 Issue 正文，两处改一起改）：

- 批准必须写成 `APPROVED: design v<N>`，`<N>` 取本 Issue 顶部的「设计版本」
- **设计版本一变，之前的 APPROVE 自动作废**，必须重新过闸门
- 什么算「实质修改」：改了契约、表结构、事务边界、状态机、失败语义、不变量控制。改错别字不算
- 实质修改时：把顶部版本 +1，在下方追加一条变更说明，状态退回 `READY_FOR_REVIEW`

### 实现拆分

| 任务 | 内容 |
| --- | --- |
| `AIH-TASK-172` | 迁移 0036、模型 `app/models/payments.py`、`AuditAction.PAYMENT_STATUS_CHANGE`（含前端审计动作清单）、`docs/database-schema.md` |
| `AIH-TASK-173` | 适配器 `app/payments/`（协议、数据类、Stripe 实现、状态映射、验签）、配置项，与测试 |
| `AIH-TASK-174` | `confirm_payment` 与入账（`app/services/payments.py`），与测试 |
| `AIH-TASK-175` | 事件落库与处理任务（`app/services/payment_events.py`、`app/tasks/payment_events.py`、Beat 登记）、告警判据并进 `wallet_negative_balance` 及 `docs/runbook.md`、`docs/api.md` 里该维度的说明，与测试 |
| `AIH-TASK-176` | 建单服务 `create_topup`（加进 `app/services/payments.py`），与测试 |
| `AIH-TASK-177` | 两个路由（回调、客户建单）、schema、`app/main.py` 注册、`docs/api.md` 两节，与端到端测试 |
| `AIH-TASK-178` | `docs/payment-flow.md`（开启步骤、复核排查）、`docs/deployment.md` 配置项、`docs/ARCHITECTURE.md` 里「payment-flow 待建」一句、`docs/TODO.md` 勾选 |

### 设计闸门判定

- [ ] 需求、非目标和验收标准明确
- [ ] 关键契约与事务边界明确
- [ ] 触碰的不变量都有控制措施
- [ ] 失败路径都有确定的最终状态
- [ ] 高风险控制都有测试场景
- [ ] 没有未解决的阻断假设
- [ ] 审批绑定到明确的设计版本

## 12. 版本变更记录

| 版本 | 日期 | 改了什么 | 触发原因 |
| --- | --- | --- | --- |
| v1 | 2026-11-06 | 初稿；送审前按登记前预审第一轮修订：显式索引与约束名、`review_required_at` 与 `REJECTED`、锁内读租户状态、PI 占用改为写入前查询、待付超时下限 2400 秒、建单沿用客户写规则与校验顺序、条件更新网关 id、事件一次领一个、告警摘要只在有支付计数时追加、compose 用例的例外、生产端点复验方法、拆成七个任务；第二轮预审后：PI 占用先查且只在未占用时写 PI id、进入 `PAID` 清原因、复用分锁外预查与锁内排除两步、开关拆成回调与建单两个并加建单允许名单、webhook 签名密钥前缀 `whsec_`、错误码、告警窗口上界与摘要逐字写法、网关工厂与同步实现、迁移简化为「已存在即报错」、§7 与 §2 不一致的两处 | — |
| v2 | 2026-11-06 | 事件处理在支付没有网关 id 时从事件认回会话 id（条件更新，撞唯一约束为 `REJECTED`）；事件不带会话 id 且支付已过期时事件 `REJECTED`（`NO_GATEWAY_REFERENCE`），不再无限重试；§5、§7 补对应行 | v1 审查阻断项 1 条 |
| v3 | 2026-11-06 | 金额上限 999,999.99（格式与最高额配置）；`stripe_api_base_url` 配置项不写默认值（ADR-0001）；建单校验顺序改为账户可写在前、开放检查在后；事件任务用命名锁保证同一时刻只跑一份并给出 Beat 条目名与过期；生产复验金额改为 RM10；配置项补取值范围 | 登记前预审第三轮（批准后）报出的设计缺陷 4 条 |

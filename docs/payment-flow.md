# 支付链路（Stripe 充值）

**首次编写：2026-11-10（AIH-TASK-178）。依据：设计闸门 #580 v3（[AIH-TASK-172-payments-gateway.md](design/AIH-TASK-172-payments-gateway.md)
§1、§2、§5、§6、§8）；ADR-0006、ADR-0007、ADR-0008、ADR-0015。**

本文件覆盖支付第一批（下称「A」）：从客户建单到可信确认后入账的后端链路、生产开启步骤，以及 B 上线前的只读排查方法。
接口契约见 [api.md](api.md)，配置项与 secret 文件见 [deployment.md「支付配置与 secret 文件」](deployment.md#支付配置与-secret-文件)，
告警的值班处置见 [runbook.md「支付」一节](runbook.md#支付复核事件冲突事件积压与配置wallet_negative_balance-告警的支付部分)。

⚠️ **生产上支付默认关**（`BILLING_PAYMENTS_ENABLED=false`）：两个路由不注册（404），事件任务每一轮直接返回。

## 1. A 不做什么

- 长时间待付的单子的补对账、`EXPIRED` 的定时转换、`REVIEW_REQUIRED` 的管理端处理：属于 B。**丢失的回调在 B 上线前不会被补回**，
  支付会一直停在 `PENDING`。
- 充值界面、支付历史、管理端设置、最小恢复额（C），收据（D）。
- 退款与拒付：A 不订阅拒付事件；人工退款走管理员调账。
- 对客户单独发「支付成功」通知：复机通知由现有账本逻辑发出。
- 按合同费率核对手续费：只记 Stripe 余额流水里的逐笔实际手续费 `gateway_fee`。

## 2. 端到端链路

```text
客户 POST /api/v1/customer/topups {"amount": "100.00"}
  → 校验 → 60 秒同额复用
  → 事务一：插入 payments（PENDING）→ 提交
  → 事务外调 Stripe：创建 Checkout Session（Idempotency-Key = topup-<payments.public_id>，
    client_reference_id = payments.public_id）
  → 事务二：写 gateway_payment_id → 提交 → 201 {payment_id, redirect_url, expires_at}
客户在 Stripe 托管付款页付款（卡或 FPX）→ 跳回门户（跳回本身不改任何状态）
Stripe POST /api/v1/webhooks/payments/stripe
  → 读原始字节 → 验签（时间窗 300 秒）→ 解析 → 插入 payment_gateway_events（RECEIVED）→ 提交 → 200
celery-beat 每 15 秒：payment-events-process-due
  → 一次领一个到期事件（SKIP LOCKED + 60 秒租约）
  → 按 client_reference_id 找支付（找不到：UNMATCHED）
  → confirm_payment：事务外向 Stripe 查询会话的权威状态
       → 事务：锁支付行 → 钱包行 → 租户行 → 按决策表落状态
            PAID：post_transaction(TOPUP) + PAYMENT_STATUS_CHANGE 审计（同一事务）
            REVIEW_REQUIRED / FAILED / EXPIRED：只改状态 + 审计
  → 事件置 PROCESSED（Stripe 不可用：RETRY，退避，不放弃）
```

### 2.1 建单

`POST /api/v1/customer/topups`，客户门户访问令牌，租户来自令牌主体，请求体只有 `amount`（字符串）。校验按顺序，任何一项不过都不建单：

| 顺序 | 校验 | 不过时 |
| --- | --- | --- |
| 1 | 账户可写（`DISABLED` / `CLOSED` 拒绝新充值） | 403 `ACCOUNT_READ_ONLY` |
| 1a | `payment_topups_open` 为真，或租户 `public_id` 在建单允许名单里 | 403 `TOPUPS_NOT_OPEN` |
| 2 | 计费模式为 `PREPAID`（内部计量租户不能充值） | 403 `TOPUP_NOT_ALLOWED` |
| 3 | 金额格式 `^[0-9]{1,6}(\.[0-9]{1,2})?$`，在最低额与最高额之间 | 422 `TOPUP_AMOUNT_INVALID` |
| 4 | 建单时刻有生效的 SST 税务政策，且不要求收税（ADR-0008） | 503 `TAX_POLICY_UNAVAILABLE` / `TAX_POLICY_UNSUPPORTED` |

- 60 秒内同租户、同金额、仍 `PENDING` 且剩余有效期不少于 31 分钟的一笔会被复用（ADR-0007），返回同一个付款页地址。
- Stripe 不可用或拒绝：支付保持 `PENDING`（`status_reason = GATEWAY_CREATE_FAILED`），返回 503 `PAYMENT_GATEWAY_UNAVAILABLE`；
  客户 60 秒内重试会复用这一笔、用同一个 `Idempotency-Key`，Stripe 那边不会出现第二个会话。
- 开关打开而 `BILLING_FRONTEND_BASE_URL` 为空，或凭据不可用：503 `PAYMENTS_MISCONFIGURED`。
- 跳回地址是 `{frontend_base_url}/portal/payments/{payment_id}?result=success`（或 `cancelled`）；**伪造的「支付成功」跳转不会入账**。
- 客户只拿到 `payment_id`、`redirect_url`、`expires_at` 三个字段；手续费、状态原因、Stripe 对象 id 不返回给客户。

### 2.2 付款页

Stripe 托管的 Checkout Session：一个 MYR 价格项、`unit_amount` 为整数分、会话过期时刻 = `payments.expires_at`（缺省建单后 3600 秒）。
付款方式（卡、FPX）由 Stripe Dashboard 的开关决定，请求里不传 `payment_method_types`；门户不加载 Stripe 脚本。

### 2.3 回调

`POST /api/v1/webhooks/payments/stripe`，不要求登录，靠签名鉴权：

| 情况 | 响应 | 落库 |
| --- | --- | --- |
| 验签失败、签名头缺失或格式不对、时间窗外 | 400 `WEBHOOK_SIGNATURE_INVALID` | 不落库，记 WARNING（不带请求体、签名头与密钥） |
| 不是 JSON、缺 `id` / `type` | 400 `WEBHOOK_PAYLOAD_INVALID` | 不落库 |
| 请求体超过 64 KiB | 413 `PAYLOAD_TOO_LARGE` | 不落库 |
| `livemode` 与密钥模式不一致 | 200 | `IGNORED`，`last_error_code = LIVEMODE_MISMATCH` |
| 四种处理的事件类型 | 200 | `RECEIVED` |
| 其他事件类型 | 200 | `IGNORED` |
| 同一事件 id 已存在、内容相同 | 200 | 不变 |
| 同一事件 id 已存在、内容不同 | 200 | 原行不变，记 `fingerprint_conflict_at`、ERROR 日志、告警 |
| 开关打开但凭据不可用；数据库不可用 | 503 | 不落库，Stripe 自动重试 |

处理的四种事件：`checkout.session.completed`、`checkout.session.async_payment_succeeded`、`checkout.session.async_payment_failed`、
`checkout.session.expired`。事件只是触发：入账以向 Stripe 查询到的权威状态为准。

`metadata_json` 只存白名单字段（`type`、`livemode`、`created`、`object_id`、`client_reference_id`、`payment_status`、`status`、
`amount_total`、`currency`、`payment_intent`），**不存原始请求体**（会话里有客户邮箱与姓名，PDPA）；需要原始对象时按会话 id 去 Stripe 查。

### 2.4 后台确认

事件任务（Beat 条目 `payment-events-process-due`，任务 `app.tasks.payment_events.process_due`）用 MySQL 命名锁保证同一时刻只跑一份；
每轮最多 50 个事件、预算 45 秒，一次领一个。处理一个事件：

1. 按 `metadata_json.client_reference_id` 找 `payments.public_id`；没有或找不到 → `UNMATCHED`（不改任何支付）。
2. 写事件的 `payment_id`。支付还没有 `gateway_payment_id`（建单时 Stripe 超时，但会话其实已建好）而事件带会话 id 时，把它认回到支付行；
   该会话 id 已属于另一笔支付 → 事件 `REJECTED`（`GATEWAY_REFERENCE_TAKEN`）。
3. `confirm_payment`。
4. 结果：成功 → `PROCESSED`；支付没有会话 id、事件也不带 → 支付未过期时 `RETRY`、已过期时 `REJECTED`（`NO_GATEWAY_REFERENCE`）；
   Stripe 不可用、返回表外状态或确认撞唯一约束 → `RETRY`（退避最长约一小时一次，不放弃）；Stripe 拒绝查询（如会话不存在）→
   `REJECTED`，`last_error_code` 记 Stripe 错误码。

`confirm_payment` 在 Stripe 报告「已付」时，**全部在写入之前、按下表顺序**判定，第一条不满足的决定原因：

| 条件 | 不满足时 |
| --- | --- |
| `client_reference_id` 等于本行 `public_id`，会话 id 等于 `gateway_payment_id` | `REVIEW_REQUIRED`，`REFERENCE_MISMATCH` |
| 币种为 `MYR` | `REVIEW_REQUIRED`，`CURRENCY_MISMATCH` |
| Stripe 金额（整数分）等于 `expected_amount × 100` | `REVIEW_REQUIRED`，`AMOUNT_MISMATCH` |
| PaymentIntent 没有被另一笔内部支付占用 | `REVIEW_REQUIRED`，`GATEWAY_PAYMENT_REUSED` |
| 租户 `account_status` 不是 `CLOSED` | `REVIEW_REQUIRED`，`TENANT_CLOSED` |
| 租户 `billing_mode` 是 `PREPAID` | `REVIEW_REQUIRED`，`INTERNAL_TENANT` |
| 全部满足 | `PAID`，写一条 `TOPUP` 账本行 |

- Stripe 状态不一致（例如会话 `complete` + `paid` 而 PaymentIntent 未成功）→ `REVIEW_REQUIRED`，`GATEWAY_INCONSISTENT`。
- Stripe 报失败 / 过期：`PENDING` → `FAILED`（`GATEWAY_FAILED`）/ `EXPIRED`（`GATEWAY_EXPIRED`）；`EXPIRED` 不是终态，过期后到账照样入账。
- `PAID` 与 `REVIEW_REQUIRED` 不会被 `confirm_payment` 改变；**`REVIEW_REQUIRED` 永不自动入账**。

### 2.5 入账

- 入账额 = `wallet_credit_amount` = `expected_amount`（含税毛额，ADR-0008）；手续费不影响入账额。
- 只调用 `post_transaction(TOPUP, reference_type=PAYMENT, reference_id=payments.public_id)`：账本行、钱包余额、计费状态（余额 > 0 为 `ACTIVE`）、
  `TENANT_BILLING_STATUS_CHANGED` 审计与复机 outbox 在同一事务里完成，复机通知与状态 webhook 由现有逻辑发出。
- `DISABLED` 租户照常入账（钱已收到；充值不会解除停用）；`CLOSED` 与内部租户不入账、进复核。
- `PAID` 后记下 `gateway_fee`（Stripe 余额流水的实际手续费，币种为 MYR 时；拿不到为空）。

### 2.6 三层幂等

| 层 | 唯一约束 | 挡什么 |
| --- | --- | --- |
| 1 | `payment_gateway_events (gateway, gateway_event_id)` | 同一事件重复投递只落一行。⚠️ 它**不足以**挡重复入账：Stripe 同一笔支付会发多个不同 id 的事件 |
| 2 | `payments.gateway_payment_id`、`payments.gateway_payment_intent_id` 各自唯一 | 一个 Stripe 会话 / PaymentIntent 只能对应一笔内部支付 |
| 3 | 账本 `wallet_transactions (reference_type, reference_id)`，即 `(PAYMENT, payments.public_id)` | 一笔内部支付至多一条 `TOPUP`。`confirm_payment` 锁住支付行后再判状态，已 `PAID` 就什么也不做；即使绕过也被这条约束挡住 |

## 3. 开启支付（管理员执行；不在 A 的代码里）

前提：A 的全部任务已合并部署，开关关着。按顺序做，任一步不成功就停下，不要跳步。

**1. 核对 Stripe 生产账户**（ADR-0015「收口条件」）：提现为自动、FPX 已开启。

**2. 建回调端点。**在 Stripe Dashboard 的生产模式下新建回调端点，地址为本平台的 `https://<平台对外域名>/api/v1/webhooks/payments/stripe`，
**只订阅四种事件**：`checkout.session.completed`、`checkout.session.async_payment_succeeded`、`checkout.session.async_payment_failed`、
`checkout.session.expired`。记下**这个端点自己的**签名密钥（`whsec_` 开头）；不要用 Stripe CLI 转发时的密钥。

**3. 先放 secret 文件，再合并加 secret 的 PR。**

1. 在宿主机部署目录下放两个文件：Stripe 密钥（生产为 `sk_live_` 开头）与上一步的端点签名密钥，各一行，只给 api / worker 容器的运行用户只读。
   密钥只放文件，**不写进 `.env`、镜像、仓库、工单或聊天**。
2. 再提交给 compose 加 `secrets:` 的 PR（api、celery-worker 挂载这两个文件，并设 `BILLING_STRIPE_SECRET_KEY_FILE` /
   `BILLING_STRIPE_WEBHOOK_SECRET_FILE` 指向容器内路径）。⚠️ 顺序不能反：compose secret 的宿主机文件不存在时，合并即部署会失败。

**4. 打开回调、只放验收租户、做生产端点复验。**

1. 在宿主机 `.env` 设置：

   ```text
   BILLING_PAYMENTS_ENABLED=true
   BILLING_FRONTEND_BASE_URL=https://<平台对外域名>
   BILLING_STRIPE_API_BASE_URL=https://<Stripe API 根地址>
   BILLING_PAYMENT_TOPUP_TENANT_ALLOWLIST=<生产验收租户的 public_id>
   ```

   `BILLING_PAYMENT_TOPUPS_OPEN` **保持不设（为假）**：其他客户建单仍得到 403 `TOPUPS_NOT_OPEN`。重启 api、celery-worker、celery-beat。
   确认 `wallet_negative_balance` 维度的摘要里没有 `config missing`。
2. 生产端点复验（ADR-0015 的两项：生产端点按自己的密钥验签、投递失败后 Stripe 自动重试且事件 id 不变）。**由 Kelvin 执行**：
   1. 把宿主机上的 webhook 签名密钥文件**临时换成另一个格式正确（`whsec_` 开头）但不属于这个端点的值**，重启。此时回调一律 400。
      格式要正确：前缀不对会被当成「未配置」返回 503，测不到验签。
   2. 用生产验收租户在门户建一笔 **RM10**（最低额）的真实充值并付款。
   3. 在 Stripe Dashboard 的端点详情里看到这次投递失败（400）；库里没有对应事件行，支付仍 `PENDING`。记下事件 id。
   4. 把签名密钥文件**换回正确的值**，重启；等 Stripe 自动重试（不要在 Dashboard 手工重发，那测不到自动重试），直到投递成功。
   5. 核对（只读）：

      ```sql
      -- 重试投递的事件 id 与第一次相同，且只落了一行
      SELECT gateway_event_id, event_type, processing_status, received_at, processed_at
        FROM payment_gateway_events
       WHERE gateway_event_id = '<Dashboard 上记下的事件 id>';

      -- 支付已 PAID
      SELECT public_id, status, status_reason, expected_amount, confirmed_amount, gateway_fee, paid_at
        FROM payments
       WHERE public_id = '<这笔支付的 payment_id>';

      -- 钱包只加了一次：恰好一条 TOPUP
      SELECT transaction_type, amount, balance_before, balance_after, created_at
        FROM wallet_transactions
       WHERE reference_type = 'PAYMENT' AND reference_id = '<这笔支付的 payment_id>';
      ```

   这笔 RM10 留在验收租户的钱包里，**不冲回**。

**5. 收尾。**从 `BILLING_PAYMENT_TOPUP_TENANT_ALLOWLIST` 里移除验收租户，重启。**B（补对账与人工复核）上线之前不要设
`BILLING_PAYMENT_TOPUPS_OPEN=true`**：A 没有丢失回调的补回与复核单的处理出口。

**回滚**：设 `BILLING_PAYMENTS_ENABLED=false` 并重启即可；已有支付、事件与账本行保留。关着期间回调得到 404、不落库。

**密钥轮换**：在 Stripe 生成新密钥 → 替换宿主机文件 → 重启。webhook 签名密钥：Stripe 可设过渡期，但本平台只配一个，
所以先换文件、重启，再在 Stripe 让旧密钥失效。

## 4. 告警：`wallet_negative_balance` 维度的支付判据

Healthchecks 已满 20 个检查，支付信号并进 `wallet_negative_balance` 维度。摘要在有支付计数大于 0 或配置判据触发时追加：

```text
; payments: review 24h <a>, event conflicts 24h <b>, oldest open event <c>s, unmatched or rejected 24h <d>, config <ok|missing>
```

24 小时窗口一律是 `now − 24 小时 < 时刻 ≤ now`。值班处置的完整步骤（查询语句、原因码对照、绝不能做什么）见
[runbook.md「支付」一节](runbook.md#支付复核事件冲突事件积压与配置wallet_negative_balance-告警的支付部分)。

| 判据 | 级别 | 含义 | 排查 |
| --- | --- | --- | --- |
| `review 24h <a>`：`payments.review_required_at` 在最近 24 小时内 | P1 | 有支付进入 `REVIEW_REQUIRED`：钱已在 Stripe，钱包**没有**加。只报进入后的 24 小时，之后复核单仍在库里 | 按 §5.1 查 `status_reason`，在 Stripe Dashboard 核对 |
| `event conflicts 24h <b>`：`payment_gateway_events.fingerprint_conflict_at` 在最近 24 小时内 | P1 | 同一事件 id 收到了内容不同的请求体；原行未被覆盖。可能是重放或伪造，或签名密钥泄露 | 按事件 id 在 Stripe Dashboard 看原始事件；怀疑泄露时按 §3 轮换签名密钥 |
| `oldest open event <c>s`：`RECEIVED` / `RETRY` / `PROCESSING` 里最早的 `received_at` 超过 15 分钟 | P2 | 事件积压：Stripe 不可用、beat / worker 停了，或事件一直重试 | 看 `last_error_code` 与 `attempts`（§5.3）；积压不降时按 runbook 的「celery-beat 停止调度」「Redis / Celery broker 不可用」排查 |
| `unmatched or rejected 24h <d>`：`processed_at` 在最近 24 小时内、状态为 `UNMATCHED` 或 `REJECTED` | P2 | 有事件对不上本系统的支付，或 Stripe 拒绝了权威查询 | 按 §5.2、§5.3 查 |
| `config missing` | P1 | 开关打开，但两个密钥文件有一个读不到或为空、Stripe 密钥前缀不是 `sk_live_` / `sk_test_`、签名密钥前缀不是 `whsec_`、`BILLING_STRIPE_API_BASE_URL` 为空，或 `BILLING_FRONTEND_BASE_URL` 为空。回调与建单 503，事件任务不领取事件 | 看 api / worker 日志的 `payments credential unavailable: …`（只带文件路径与原因），修好后重启 api 与 worker |

## 5. B 上线前的只读排查

B 上线之前没有处理出口。下面的查询**只读**：

- **不要**改 `payments.status`、补 `TOPUP` 账本行或直接改钱包（INV-3、INV-4）；
- **不要**改事件的 `processing_status`、`attempts`、`next_attempt_at`，也不要删 `payment_gateway_events` 的行（它是去重依据）；
- 结论记进工单，等 B 的人工出口处理。

查询在 mysql 容器里执行（`docker compose exec mysql sh -c 'mysql -u"$MYSQL_USER" -p"$MYSQL_PASSWORD" "$MYSQL_DATABASE"'`），
`metadata_json` 只有白名单字段，没有个人数据。

### 5.1 `REVIEW_REQUIRED`

用 `payments` 的 `public_id`、`tenant_id`、`status_reason`、`expected_amount`、`expected_currency`、`confirmed_amount`、`confirmed_currency`、
`gateway_payment_id`、`gateway_payment_intent_id`、`review_required_at`、`last_reconciled_at`，连 `tenants` 看客户：

```sql
SELECT p.public_id, t.public_id AS customer, t.account_status, t.billing_mode,
       p.status_reason, p.expected_amount, p.expected_currency, p.confirmed_amount, p.confirmed_currency,
       p.gateway_payment_id, p.gateway_payment_intent_id, p.review_required_at, p.last_reconciled_at
  FROM payments p JOIN tenants t ON t.id = p.tenant_id
 WHERE p.status = 'REVIEW_REQUIRED'
 ORDER BY p.review_required_at DESC
 LIMIT 50;
```

| `status_reason` | 看什么 |
| --- | --- |
| `REFERENCE_MISMATCH` | Stripe 会话的 `client_reference_id` 或会话 id 与本行不符：在 Stripe Dashboard 按 `gateway_payment_id` 打开会话核对 |
| `CURRENCY_MISMATCH` / `AMOUNT_MISMATCH` | `confirmed_currency` / `confirmed_amount` 与 `expected_*` 的差异；客户实付了多少以 Stripe 为准 |
| `GATEWAY_PAYMENT_REUSED` | 同一 PaymentIntent 已属于另一笔支付：`gateway_payment_intent_id` 在本行为空，按下面的事件查询找到同一会话的事件，在 Stripe 查 PaymentIntent，再用 `SELECT public_id, status FROM payments WHERE gateway_payment_intent_id = '<pi id>'` 找到占用它的那一笔 |
| `TENANT_CLOSED` / `INTERNAL_TENANT` | `account_status` / `billing_mode`；钱已收，处理（退款或调账）等 B 与人工决定 |
| `GATEWAY_INCONSISTENT` | Stripe 会话与 PaymentIntent 状态互相矛盾：在 Stripe Dashboard 看 PaymentIntent 的最终状态 |

同一笔支付的事件与审计：

```sql
SELECT gateway_event_id, event_type, processing_status, last_error_code, received_at, processed_at, metadata_json
  FROM payment_gateway_events
 WHERE payment_id = (SELECT id FROM payments WHERE public_id = '<payment_id>')
 ORDER BY received_at;
```

`PAYMENT_STATUS_CHANGE` 审计的 `before` / `after` 记着每次状态变化（状态、原因、金额、`gateway_fee`）。

### 5.2 `UNMATCHED`

事件里的 `client_reference_id` 缺失或找不到对应支付，`payment_id` 为空。用 `payment_gateway_events` 的 `gateway_event_id`、`event_type`、
`last_error_code`（`NO_CLIENT_REFERENCE` / `PAYMENT_NOT_FOUND`）、`received_at`、`processed_at` 与 `metadata_json` 的
`client_reference_id`、`object_id`、`livemode`、`amount_total`、`currency`：

```sql
SELECT gateway_event_id, event_type, last_error_code, received_at, processed_at,
       JSON_UNQUOTE(JSON_EXTRACT(metadata_json, '$.client_reference_id')) AS client_reference_id,
       JSON_UNQUOTE(JSON_EXTRACT(metadata_json, '$.object_id')) AS session_id,
       JSON_EXTRACT(metadata_json, '$.livemode') AS livemode,
       JSON_EXTRACT(metadata_json, '$.amount_total') AS amount_total,
       JSON_UNQUOTE(JSON_EXTRACT(metadata_json, '$.currency')) AS currency
  FROM payment_gateway_events
 WHERE processing_status = 'UNMATCHED'
 ORDER BY processed_at DESC
 LIMIT 50;
```

多半是：不是本平台建的会话（例如 Dashboard 手工建的付款链接），或测试与生产的端点配串了。核对 Stripe Dashboard 的回调端点只订阅那四种事件、
指向正确的环境。`client_reference_id` 看起来是本平台的 `public_id` 却找不到时，按数据事故上报。

### 5.3 `REJECTED`

`payment_id` 通常已写入。用 `last_error_code` 区分：

```sql
SELECT e.gateway_event_id, e.event_type, e.last_error_code, e.attempts, e.received_at, e.processed_at,
       p.public_id AS payment, p.status, p.gateway_payment_id, p.expires_at,
       JSON_UNQUOTE(JSON_EXTRACT(e.metadata_json, '$.object_id')) AS session_id
  FROM payment_gateway_events e
  LEFT JOIN payments p ON p.id = e.payment_id
 WHERE e.processing_status = 'REJECTED'
 ORDER BY e.processed_at DESC
 LIMIT 50;
```

| `last_error_code` | 含义 | 看什么 |
| --- | --- | --- |
| Stripe 错误码（如 `resource_missing`） | Stripe 拒绝了权威查询，多半是会话不存在：密钥与回调端点不属于同一账户或模式 | 核对密钥文件、`BILLING_STRIPE_API_BASE_URL` 与端点所在账户 |
| `GATEWAY_REFERENCE_TAKEN` | 事件的会话 id 已属于另一笔支付；本支付不变、不入账 | 用 `SELECT public_id, status FROM payments WHERE gateway_payment_id = '<session_id>'` 找到另一笔，在 Stripe 核对会话的 `client_reference_id` |
| `NO_GATEWAY_REFERENCE` | 建单时 Stripe 超时、支付没有会话 id，事件也不带会话 id，支付已过 `expires_at` | 按支付 `public_id` 在 Stripe Dashboard 搜索是否真有付款；有付款时等 B 的补对账处理 |

## 6. 示例的写法

本文件与 PR、工单里的示例一律用占位符（`<平台对外域名>`、`<Stripe API 根地址>`、`<payment_id>`），**不写真实主机名、地址、租户 id 与密钥**（ADR-0001）。

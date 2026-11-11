# 支付链路（Stripe 充值）

**首次编写：2026-11-10（AIH-TASK-178）。依据：设计闸门 #580 v3（[AIH-TASK-172-payments-gateway.md](design/AIH-TASK-172-payments-gateway.md)
§1、§2、§5、§6、§8）；ADR-0006、ADR-0007、ADR-0008、ADR-0015。
更新：AIH-TASK-186，设计闸门 #585 v3（[AIH-TASK-179-payments-review.md](design/AIH-TASK-179-payments-review.md)
§1、§2「补对账」「人工复核出口」「争议与退款」「告警」「配置」、§8）：补对账、复核处理、争议与退款、对客户开放充值前的检查。
更新：AIH-TASK-195，设计闸门 #587 v3（[AIH-TASK-188-payment-settings-portal.md](design/AIH-TASK-188-payment-settings-portal.md)
§1、§2「支付设置服务」「最小恢复额」「门户页面」、§8）：支付设置、最小恢复额、对客户开放充值。**

本文件覆盖支付第一批（下称「A」）：从客户建单到可信确认后入账的后端链路与生产开启步骤；支付第二批（下称「B」）：
补对账（§5）、`REVIEW_REQUIRED` 的人工复核处理（§6）、争议与退款（§7）、对客户开放充值前的检查（§8）；以及支付第三批（下称「C」）：
支付设置（§11）、最小恢复额（§12）、对客户开放充值与回滚（§13）。
接口契约见 [api.md](api.md)，配置项与 secret 文件见 [deployment.md「支付配置与 secret 文件」](deployment.md#支付配置与-secret-文件)，
告警的值班处置见 [runbook.md「支付」一节](runbook.md#支付复核事件冲突事件积压与配置wallet_negative_balance-告警的支付部分)。

⚠️ **生产上支付默认关**（`BILLING_PAYMENTS_ENABLED=false`）：两个路由不注册（404），事件任务与补对账每一轮直接返回。

## 1. A、B、C 不做什么

- A 留下的长时间待付的单子的补对账、`EXPIRED` 的定时转换、`REVIEW_REQUIRED` 的管理端处理：已由 B 交付，见 §5、§6。
- 充值界面、支付历史、管理端支付设置、最小恢复额：已由 C 交付，见 §11、§12。收据属于 D，支付历史不显示收据列。
- 不让客户取消或继续一笔待付的支付：复用窗口内重复点同一金额会复用同一笔（§2.1）。
- 不改停机阈值（ADR-0010 方案 A：停机规则仍是余额 ≤ 0），不改可信确认与入账：最小恢复额只是建单时的校验（§12）。
- 不把 `BILLING_PAYMENTS_ENABLED`、`BILLING_PAYMENT_TOPUPS_OPEN`、建单允许名单搬进支付设置：它们是运维开关，仍是配置项（§13）。
- 不做自动退款，不对争议自动扣回（spec §112.1）：B 订阅争议与退款事件，只落库、关联与告警，扣回走现有人工调账（§7）。
- 不对 `PENDING`、`EXPIRED`、`FAILED`、`CANCELLED`、`PAID` 提供人工操作：人工出口只针对 `REVIEW_REQUIRED`。
- 对客户单独发「支付成功」通知：复机通知由现有账本逻辑发出。
- 按合同费率核对手续费：只记 Stripe 余额流水里的逐笔实际手续费 `gateway_fee`。
- 不新增 Healthchecks 检查：支付信号并进已有的 `wallet_negative_balance` 维度。

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
celery-beat 每 60 秒：payment-reconcile-due（B，§5）
  → 到期的 PENDING：confirm_payment / 孤儿过期 / 超时过期
  → PAID 而 gateway_fee 为空：补抓手续费
管理员 → 管理端「支付」页 → 「按实付入账」/「不入账结案」（B，§6）
Stripe → charge.dispute.created / charge.refunded → 落库、关联支付、P1 告警，不改账（B，§7）
```

### 2.1 建单

`POST /api/v1/customer/topups`，客户门户访问令牌，租户来自令牌主体，请求体只有 `amount`（字符串）。校验按顺序，任何一项不过都不建单：

| 顺序 | 校验 | 不过时 |
| --- | --- | --- |
| 1 | 账户可写（`DISABLED` / `CLOSED` 拒绝新充值） | 403 `ACCOUNT_READ_ONLY` |
| 1a | `payment_topups_open` 为真，或租户 `public_id` 在建单允许名单里 | 403 `TOPUPS_NOT_OPEN` |
| 2 | 计费模式为 `PREPAID`（内部计量租户不能充值） | 403 `TOPUP_NOT_ALLOWED` |
| 3 | 金额按顺序：格式 `^[0-9]{1,6}(\.[0-9]{1,2})?$` → 不小于最低额 → 不小于生效的最低额（挂起租户的最小恢复额，§12）→ 不大于最高额（设了时）；最低额与最高额来自支付设置（§11） | 422 `TOPUP_AMOUNT_INVALID`；只有第三条不过时为 422 `TOPUP_BELOW_RECOVERY_MINIMUM` |
| 4 | 建单时刻有生效的 SST 税务政策，且不要求收税（ADR-0008） | 503 `TAX_POLICY_UNAVAILABLE` / `TAX_POLICY_UNSUPPORTED` |

- 金额规则、复用窗口与待付超时在快速校验开始时从支付设置读一次，整个建单过程用这一份；读不到（设置行缺失）→ 503 `PAYMENTS_MISCONFIGURED`。
- 复用窗口内（支付设置的 `reuse_window_seconds`，缺省 60 秒，§11）同租户、同金额、仍 `PENDING` 且剩余有效期不少于 31 分钟的一笔会被复用（ADR-0007），返回同一个付款页地址。
- Stripe 不可用或拒绝：支付保持 `PENDING`（`status_reason = GATEWAY_CREATE_FAILED`），返回 503 `PAYMENT_GATEWAY_UNAVAILABLE`；
  客户 60 秒内重试会复用这一笔、用同一个 `Idempotency-Key`，Stripe 那边不会出现第二个会话。
  没有会话 id 的 `PENDING` 由补对账在 `expires_at` 之后 10 分钟置为 `EXPIRED`（§5.2）。
- 开关打开而 `BILLING_FRONTEND_BASE_URL` 为空，或凭据不可用：503 `PAYMENTS_MISCONFIGURED`。
- 跳回地址是 `{frontend_base_url}/portal/payments/{payment_id}?result=success`（或 `cancelled`）；**伪造的「支付成功」跳转不会入账**。
- 客户只拿到 `payment_id`、`redirect_url`、`expires_at` 三个字段；手续费、状态原因、Stripe 对象 id 不返回给客户。

### 2.2 付款页

Stripe 托管的 Checkout Session：一个 MYR 价格项、`unit_amount` 为整数分、会话过期时刻 = `payments.expires_at`（建单时刻加支付设置的 `pending_timeout_seconds`，缺省 3600 秒，§11）。
付款方式（卡、FPX）由 Stripe Dashboard 的开关决定，请求里不传 `payment_method_types`；门户不加载 Stripe 脚本。

### 2.3 回调

`POST /api/v1/webhooks/payments/stripe`，不要求登录，靠签名鉴权：

| 情况 | 响应 | 落库 |
| --- | --- | --- |
| 验签失败、签名头缺失或格式不对、时间窗外 | 400 `WEBHOOK_SIGNATURE_INVALID` | 不落库，记 WARNING（不带请求体、签名头与密钥） |
| 不是 JSON、缺 `id` / `type` | 400 `WEBHOOK_PAYLOAD_INVALID` | 不落库 |
| 请求体超过 64 KiB | 413 `PAYLOAD_TOO_LARGE` | 不落库 |
| `livemode` 与密钥模式不一致 | 200 | `IGNORED`，`last_error_code = LIVEMODE_MISMATCH` |
| 六种处理的事件类型 | 200 | `RECEIVED` |
| 其他事件类型 | 200 | `IGNORED` |
| 同一事件 id 已存在、内容相同 | 200 | 不变 |
| 同一事件 id 已存在、内容不同 | 200 | 原行不变，记 `fingerprint_conflict_at`、ERROR 日志、告警 |
| 开关打开但凭据不可用；数据库不可用 | 503 | 不落库，Stripe 自动重试 |

A 处理的四种事件：`checkout.session.completed`、`checkout.session.async_payment_succeeded`、`checkout.session.async_payment_failed`、
`checkout.session.expired`。事件只是触发：入账以向 Stripe 查询到的权威状态为准。B 另加两种：`charge.dispute.created`、`charge.refunded`，
只关联与告警，不改账（§7）。

`metadata_json` 只存白名单字段（`type`、`livemode`、`created`、`object_id`、`client_reference_id`、`payment_status`、`status`、
`amount_total`、`currency`、`payment_intent`；争议与退款事件另有 `amount`、`amount_refunded`、`charge`，见 §7），
**不存原始请求体**（会话里有客户邮箱与姓名，PDPA）；需要原始对象时按会话 id 去 Stripe 查。

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
- `PAID`、`REVIEW_REQUIRED` 与**已人工结案**（`resolution` 不为空，B）的支付不会被 `confirm_payment` 改变，只更新 `last_reconciled_at`；
  **`REVIEW_REQUIRED` 永不自动入账**，只能经 §6 的人工出口处理。

### 2.5 入账

- 入账额 = `wallet_credit_amount` = `expected_amount`（含税毛额，ADR-0008）；手续费不影响入账额。
  人工「按实付入账」的入账额以 Stripe 实收为准（§6.3），**实际入账额一律以账本行为准**。
- 只调用 `post_transaction(TOPUP, reference_type=PAYMENT, reference_id=payments.public_id)`：账本行、钱包余额、计费状态（余额 > 0 为 `ACTIVE`）、
  `TENANT_BILLING_STATUS_CHANGED` 审计与复机 outbox 在同一事务里完成，复机通知与状态 webhook 由现有逻辑发出。
- `DISABLED` 租户照常入账（钱已收到；充值不会解除停用）；`CLOSED` 与内部租户不入账、进复核。
- `PAID` 后记下 `gateway_fee`（Stripe 余额流水的实际手续费，币种为 MYR 时；拿不到为空，由补对账在 8 天内补抓，§5.4）。

### 2.6 三层幂等

| 层 | 唯一约束 | 挡什么 |
| --- | --- | --- |
| 1 | `payment_gateway_events (gateway, gateway_event_id)` | 同一事件重复投递只落一行。⚠️ 它**不足以**挡重复入账：Stripe 同一笔支付会发多个不同 id 的事件 |
| 2 | `payments.gateway_payment_id`、`payments.gateway_payment_intent_id` 各自唯一 | 一个 Stripe 会话 / PaymentIntent 只能对应一笔内部支付 |
| 3 | 账本 `wallet_transactions (reference_type, reference_id)`，即 `(PAYMENT, payments.public_id)` | 一笔内部支付至多一条 `TOPUP`。`confirm_payment` 锁住支付行后再判状态，已 `PAID` 就什么也不做；即使绕过也被这条约束挡住 |

B 另加 CHECK `ck_payments_resolution_fields`：人工结案的支付（`CREDITED` 只能配 `PAID`、`CLOSED` 只能配 `FAILED`）在数据库层面是终态，
任何路径把它改成别的状态都会被拒。

## 3. 开启支付（管理员执行；不在 A 的代码里）

前提：A 的全部任务已合并部署，开关关着。按顺序做，任一步不成功就停下，不要跳步。

**1. 核对 Stripe 生产账户**（ADR-0015「收口条件」）：提现为自动、FPX 已开启。

**2. 建回调端点。**在 Stripe Dashboard 的生产模式下新建回调端点，地址为本平台的 `https://<平台对外域名>/api/v1/webhooks/payments/stripe`，
订阅 A 的四种事件：`checkout.session.completed`、`checkout.session.async_payment_succeeded`、`checkout.session.async_payment_failed`、
`checkout.session.expired`（B 上线后再加两种，共六种，见 §8 第 1 步）。记下**这个端点自己的**签名密钥（`whsec_` 开头）；
不要用 Stripe CLI 转发时的密钥。

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

   ⚠️ B 已部署时，签名密钥换错期间补对账仍在跑：建单 5 分钟后它可能先于 Stripe 的自动重试把这笔置 `PAID`。这不影响本步要验的两项
   （重试投递的事件 id 不变、只落一行；钱包只加一次），核对方法不变。

**5. 收尾。**从 `BILLING_PAYMENT_TOPUP_TENANT_ALLOWLIST` 里移除验收租户，重启。**不要在这一步设 `BILLING_PAYMENT_TOPUPS_OPEN=true`**：
先完成 §8 的检查，充值界面（C）上线后按 §13 打开。

**回滚**：设 `BILLING_PAYMENTS_ENABLED=false` 并重启即可；已有支付、事件与账本行保留，补对账随之停止，已结案的数据保留。
关着期间回调得到 404、不落库。

**密钥轮换**：在 Stripe 生成新密钥 → 替换宿主机文件 → 重启。webhook 签名密钥：Stripe 可设过渡期，但本平台只配一个，
所以先换文件、重启，再在 Stripe 让旧密钥失效。

## 4. 告警：`wallet_negative_balance` 维度的支付判据

Healthchecks 已满 20 个检查，支付信号并进 `wallet_negative_balance` 维度。摘要段落为：

```text
; payments: review open <a>, event conflicts 24h <b>, oldest open event <c>s, unmatched or rejected 24h <d>, stale pending <e>, disputes or refunds 24h <f>, config <ok|missing>
```

只在 `<a>`、`<b>`、`<d>`、`<e>`、`<f>` 任一大于 0、`<c>` 超过 900 秒（严格大于）、或配置判据触发时追加。
24 小时窗口一律是 `now − 24 小时 < 时刻 ≤ now`。值班处置的完整步骤（查询语句、原因码对照、绝不能做什么）见
[runbook.md「支付」一节](runbook.md#支付复核事件冲突事件积压与配置wallet_negative_balance-告警的支付部分)。

| 判据 | 级别 | 含义 | 排查 |
| --- | --- | --- | --- |
| `review open <a>`：`status = 'REVIEW_REQUIRED'` 的支付（不论进入多久） | P1 | 钱已在 Stripe，钱包**没有**加。一直报到结案，结案即消失（B 替换了 A 的「进入后 24 小时内」） | 按 §6 处理 |
| `event conflicts 24h <b>`：`payment_gateway_events.fingerprint_conflict_at` 在最近 24 小时内 | P1 | 同一事件 id 收到了内容不同的请求体；原行未被覆盖。可能是重放或伪造，或签名密钥泄露 | 按事件 id 在 Stripe Dashboard 看原始事件；怀疑泄露时按 §3 轮换签名密钥 |
| `oldest open event <c>s`：`RECEIVED` / `RETRY` / `PROCESSING` 里最早的 `received_at` 超过 15 分钟 | P2 | 事件积压：Stripe 不可用、beat / worker 停了，或事件一直重试 | 看 `last_error_code` 与 `attempts`（§9.3）；积压不降时按 runbook 的「celery-beat 停止调度」「Redis / Celery broker 不可用」排查 |
| `unmatched or rejected 24h <d>`：`processed_at` 在最近 24 小时内、状态为 `UNMATCHED` 或 `REJECTED` | P2 | 有事件对不上本系统的支付，或 Stripe 拒绝了权威查询 | 按 §9.2、§9.3 查；争议与退款事件的 `UNMATCHED` 按 §7 |
| `stale pending <e>`：`status = 'PENDING'` 且 `expires_at <= now − 3600 秒` | P2 | 过期一小时仍未收口：补对账没在工作或 Stripe 持续不可用。会话已 `complete` 而异步付款仍在处理时会一直报到付款有结果或 7 天超时，这是有意的 | 按 §5.3 |
| `disputes or refunds 24h <f>`：`charge.dispute.created` / `charge.refunded`、`processing_status` 不是 `IGNORED`、`received_at` 在最近 24 小时内；不含「`charge.refunded` 且关联的支付 `resolution = CLOSED`」 | P1 | Stripe 侧有争议（拒付）或后台退款；支付与钱包都没有被改动 | 按 §7 |
| `config missing` | P1 | 开关打开，但两个密钥文件有一个读不到或为空、Stripe 密钥前缀不是 `sk_live_` / `sk_test_`、签名密钥前缀不是 `whsec_`、`BILLING_STRIPE_API_BASE_URL` 为空，或 `BILLING_FRONTEND_BASE_URL` 为空。回调与建单 503，事件任务不领取事件，补对账不运行 | 看 api / worker 日志的 `payments credential unavailable: …`（只带文件路径与原因），修好后重启 api 与 worker |

## 5. 补对账

补对账把丢失回调的支付补回来、把收不了口的 `PENDING` 置为过期，并补抓手续费。它**扫库、不靠入队**（INV-14）：Celery 丢了任务，下一轮照样会选出来。
配置项见 [deployment.md「支付配置与 secret 文件」](deployment.md#支付配置与-secret-文件)。

### 5.1 候选条件与节奏

- celery-beat 每 `BILLING_PAYMENT_RECONCILE_POLL_SECONDS`（缺省 60 秒）执行一次 `app.tasks.payment_reconcile.run_due`（条目 `payment-reconcile-due`，
  `expires` 为间隔减 1 秒）。开关为假或凭据不可用时直接返回。
- **同一时刻只跑一份**：开始时取 MySQL 命名锁 `payment_reconcile:<库名>`，取不到直接返回。
- 每轮预算 45 秒、最多 50 次 Stripe 查询（第 1 段与第 2 段共用）；整轮共用本轮开始时读到的一个当前时刻。
- **第 1 段：`PENDING` 补对账。**候选必须是 `status = 'PENDING'` 且建单已满 `BILLING_PAYMENT_RECONCILE_GRACE_SECONDS`（缺省 300 秒，
  给回调先到的机会），再满足下表任一行。到期条件全部写在查询里，未到期的老单不会挡住到期的新单。

  | 情形 | 条件 | 距上次查询（`last_reconciled_at`）至少 |
  | --- | --- | --- |
  | 孤儿 | 没有网关 id，且 `now ≥ expires_at + 600 秒` | —（不查 Stripe，直接过期，§5.2） |
  | 已满上限 | 有网关 id，建单已满 `BILLING_PAYMENT_RECONCILE_MAX_SECONDS`（缺省 7 天） | 每轮 |
  | 会话未过期、建单 2 小时内 | 有网关 id，`expires_at > now` | 5 分钟 |
  | 会话未过期、建单超过 2 小时 | 有网关 id，`expires_at > now` | 30 分钟 |
  | 过期后 1 小时内 | 有网关 id，`now − 3600 秒 < expires_at ≤ now` | 5 分钟 |
  | 过期 1 小时以后 | 有网关 id，`expires_at ≤ now − 3600 秒` | 6 小时 |

  从没查过（`last_reconciled_at` 为空）的算作到期。排序为「最久没查过的先查」（`COALESCE(last_reconciled_at, created_at)`、`id`），
  每轮最多选 200 行，查不完的下一轮继续。节奏以 `expires_at` 为准：会话开着时每 5 或 30 分钟查一次，**过期后的第一个小时每 5 分钟查一次**，
  Stripe 侧会话一结束就能收口（与 `stale pending` 的「过期 1 小时」P2 对上）。
- 有网关 id 的支付逐笔调 A 的 `confirm_payment`，结果按 A 的决策表落状态（`PAID` 入账 / `EXPIRED` / `FAILED` / `REVIEW_REQUIRED` / 不变），
  并更新 `last_reconciled_at`。补对账与事件任务同时确认同一笔时，「锁支付行后再判、`PAID` 不再变、账本引用唯一」保证只入账一次；
  补回之后再收到迟到的回调也不会重复入账。
- 已人工结案的支付不是 `PENDING`，不会被第 1 段选出。
- 600 秒、2 小时、5 / 30 分钟与 6 小时的间隔、过期后 1 小时，以及手续费补抓的 8 天、600 秒与 1 小时，是写在服务模块里的常量，**改它们要过设计闸门**。

### 5.2 孤儿与超时过期

| 过期 | 什么时候 | 结果 |
| --- | --- | --- |
| 孤儿过期 | 没有网关 id（建单时 Stripe 调用失败留下的单子），`now ≥ expires_at + 600 秒`。不查 Stripe：没有可查的 id，Stripe 侧即使有会话，它的过期时刻也等于 `expires_at`，600 秒后必定已过期 | `EXPIRED`，`status_reason = NO_GATEWAY_SESSION` |
| 超时过期 | 建单已满 `BILLING_PAYMENT_RECONCILE_MAX_SECONDS`（缺省 7 天），本轮向 Stripe 查询后仍是 `PENDING`（或 Stripe 拒绝查询） | `EXPIRED`，`status_reason = RECONCILE_TIMEOUT` |

- 两种过期各在一个**只锁这一笔支付行**的事务里做：重查仍是 `PENDING`、`resolution` 为空（孤儿过期还要求 `gateway_payment_id` 仍为空）才改，
  同一事务写 `PAYMENT_STATUS_CHANGE` 审计（操作者为系统）；重查不满足就什么也不做（例如事件刚认回了会话 id）。
- `EXPIRED` 不是终态（ADR-0007）：之后收到这笔的付款成功事件，事件任务照常认回会话 id、确认、入账（`EXPIRED` → `PAID`）；已人工结案的除外（§6.5）。
- Stripe 不可用或确认撞唯一约束时**不做**超时过期，下一轮再来。

### 5.3 Stripe 不可用时的表现

| 情况 | 本轮怎么做 | 支付 | 之后 |
| --- | --- | --- | --- |
| Stripe 不可用（`GatewayUnavailable`） | **结束本轮**，后面的支付本轮不再查询（不连续冲击 Stripe） | 不变，`last_reconciled_at` 不变，不做超时过期 | 下一轮重来；持续不可用时过期一小时的 `PENDING` 触发 `stale pending` P2 |
| Stripe 拒绝查询（`GatewayRequestRejected`，例如会话不存在） | 记 ERROR（带 Stripe 错误码与 `public_id`），继续下一笔 | 状态不变，`last_reconciled_at` 更新为本轮时刻；已满上限 → 超时过期 | 按间隔再查，满 7 天 `EXPIRED`（`RECONCILE_TIMEOUT`）；需要人工排查（多半是密钥与会话不属于同一账户或模式，§9.3） |
| 确认事务撞唯一约束（`ConfirmConflict`） | 跳过这一笔，本轮继续 | 不变，不做超时过期 | 下一轮再来 |
| 单笔意外错误（例如过期或手续费事务里的数据库错误） | 回滚这一笔，记 ERROR（只带 `public_id` 与异常类型），**结束本轮** | 不变 | 下一轮按候选查询重来 |
| celery-beat / Redis 停了 | 补对账不跑 | 仍在库里 | 恢复后扫库补上；期间 `stale pending` P2 |

补对账只在开关打开且凭据可用时运行：`config missing` 期间它也不跑。

### 5.4 手续费补抓的期限

第 1 段结束后、预算与查询次数还有剩余时运行第 2 段：

- 候选：`status = 'PAID'`、`gateway_fee` 为空、有网关 id、**建单 8 天以内**、`paid_at` 已过 600 秒、距上次查询至少 1 小时（即**按小时补查**）；
  按「最久没查过的先查」（`COALESCE(last_reconciled_at, paid_at)`、`id`），每轮最多 50 行。
- 拿到手续费且币种为 MYR → 写 `gateway_fee`（只在它仍为空时写），同一事务写 `PAYMENT_FEE_RECORDED` 审计（操作者为系统，
  `before` 为 `{"gateway_fee": null}`、`after` 为 `{"gateway_fee": "<定点字符串>"}`）。
- 没有手续费 → 只更新 `last_reconciled_at`；币种不是 MYR → 只更新 `last_reconciled_at`，记 WARNING（与 A 相同，不写）；
  Stripe 返回的会话 id 对不上 → 只更新 `last_reconciled_at`，记 ERROR。Stripe 不可用 → 结束本轮；Stripe 拒绝查询 → 只更新 `last_reconciled_at`，记 ERROR。
- **建单超过 8 天仍拿不到的，`gateway_fee` 保持为空**，不再补查。它只影响内部毛利分析，不影响入账。
- 人工「按实付入账」（`resolution = CREDITED`）的支付也是 `PAID`，同样会被补手续费：只写 `gateway_fee` 与 `last_reconciled_at`，不改状态与结案字段。

## 6. 复核处理

`REVIEW_REQUIRED` 的支付**钱已在 Stripe、钱包没有加**，`review open` 一直报 P1 到结案为止。它永不自动入账，只能由管理员经下面的两个出口之一结案。

### 6.1 入口：管理端支付页的两个出口

- 管理端「支付」页（`/payments`）按状态 `REVIEW_REQUIRED` 筛选，打开详情页（`/payments/:paymentId`）。详情页列出基本字段与这笔支付的回调事件；
  对应接口为 `GET /api/v1/admin/payments`、`GET /api/v1/admin/payments/{payment_id}`、`POST /api/v1/admin/payments/{payment_id}/resolve`，
  只有 ADMIN 能调用（登录必过 2FA），契约见 [api.md](api.md)。
- 支付是 `REVIEW_REQUIRED` 时详情页显示复核面板，两个出口：

  | 出口 | 动作 | 结果 |
  | --- | --- | --- |
  | 「按实付入账」 | `CREDIT` | 向 Stripe 重新查询并核对，按 Stripe **实收**金额写一条 `TOPUP`，支付转 `PAID`（`resolution = CREDITED`）。只限 MYR、租户不是关户也不是内部租户 |
  | 「不入账结案」 | `CLOSE` | 支付转 `FAILED`（`resolution = CLOSED`），不写账本；需要退款的，**在 Stripe 后台手工退款**（§6.4） |

- 两个出口都要填原因（去掉首尾空白后 1–255 字）并二次确认；原因进库（`resolution_reason`）与审计，**不进日志**，不要写客户个人数据。
  `status_reason` 保留进入复核时的原因。两者都写 `PAYMENT_REVIEW_RESOLVED` 审计（`entity_type` 为 `payments`、`entity_id` 为 `public_id`）。
- 「按实付入账」需要支付开关打开且凭据可用（否则 503 `PAYMENTS_MISCONFIGURED`）；「不入账结案」不需要 Stripe，开关关着也能结案。
- 先在 Stripe Dashboard 按详情页的 `gateway_reference`（会话 id）与 `gateway_payment_intent_id` 核对这笔付款，再看进入复核的原因：

  | `status_reason` | 看什么 |
  | --- | --- |
  | `REFERENCE_MISMATCH` | Stripe 会话的 `client_reference_id` 或会话 id 与本行不符：按会话 id 打开会话核对这笔钱属于谁 |
  | `CURRENCY_MISMATCH` / `AMOUNT_MISMATCH` | `confirmed_currency` / `confirmed_amount` 与应收的差异；客户实付了多少以 Stripe 为准。金额不符可以按实付入账；币种不是 MYR 不能入账 |
  | `GATEWAY_PAYMENT_REUSED` | 同一 PaymentIntent 已属于另一笔支付：这笔钱已给那一笔入过账，只能不入账结案且**不退款**（§6.4） |
  | `TENANT_CLOSED` / `INTERNAL_TENANT` | 不能入账：不入账结案，并在 Stripe 退款 |
  | `GATEWAY_INCONSISTENT` | Stripe 会话与 PaymentIntent 状态互相矛盾：看 PaymentIntent 的最终状态；只有 Stripe 现在报已付时才能按实付入账 |

  能不能入账不是靠 `status_reason` 判断，而是靠「按实付入账」提交时对 Stripe 与数据库的**重新核对**（§6.3）；核对不过会返回错误码、
  不改任何数据，按 §6.2 处理。

### 6.2 各错误码的处理

「按实付入账」或「不入账结案」被拒时支付、账本与审计都不变（仍是 `REVIEW_REQUIRED`、仍报 P1），页面显示后端的错误信息：

| 错误码 | 含义 | 管理员该怎么做 |
| --- | --- | --- |
| `PAYMENT_NOT_IN_REVIEW` / `RESOLUTION_CONFLICT` | 已被别人处理或并发冲突 | 刷新页面看当前状态 |
| `PAYMENT_GATEWAY_UNAVAILABLE` / `PAYMENTS_MISCONFIGURED` | Stripe 暂时不可用 / 支付未配置 | 稍后重试；不要因为它改用不入账结案 |
| `GATEWAY_QUERY_REJECTED` | Stripe 拒绝查询（例如会话不存在） | 在 Stripe Dashboard 核对；确认没有收到钱 → 不入账结案 |
| `GATEWAY_REFERENCE_MISSING` / `GATEWAY_REFERENCE_MISMATCH` | 网关引用缺失或对不上 | 在 Stripe Dashboard 核对这笔钱属于谁；不能确认 → 不入账结案并在 Stripe 退款 |
| `GATEWAY_NOT_PAID` | Stripe 现在不报已付 | 异步付款可能还在处理，稍后再试；确认不会到账 → 不入账结案 |
| `CURRENCY_NOT_MYR` / `INTERNAL_TENANT` / `TENANT_CLOSED` | 不能入账 | 不入账结案，并在 Stripe 退款 |
| `GATEWAY_PAYMENT_REUSED` | 这笔钱已给另一笔支付入过账 | 不入账结案，**不退款** |
| `GATEWAY_REFUNDED_OR_DISPUTED` | 这笔钱在 Stripe 已退过款（全部或部分）或有争议 | 不入账结案，**不再退款**；部分退款而客户确实该得到剩余部分时，结案后由 Kelvin 用人工调账补记，原因里写支付编号 |
| `BALANCE_OUT_OF_RANGE` | 入账后余额越界 | 不入账结案并退款，或先联系 Kelvin |

### 6.3 「按实付入账」：以 Stripe 实收为准

- 提交时事务外向 Stripe 重新查询，按顺序核对：`client_reference_id` 与会话 id 对得上；Stripe 报已付；币种为 MYR；有 PaymentIntent；
  拿得到 charge 的退款与争议信息，且没有退过款、没有争议。随后在锁住支付、钱包、租户的事务里重查：仍是未结案的 `REVIEW_REQUIRED`；
  PaymentIntent 与本行一致且没有被另一笔支付占用；租户不是 `CLOSED`、计费模式是 `PREPAID`。任一条不过即返回 §6.2 的错误码。
- **入账额 = Stripe 实收金额**（`amount_received`，整数分换成两位小数），可能不等于应收 `expected_amount`（金额不符正是进入复核的常见原因）；
  确认框里显示的 `confirmed_amount` 只是参考，以提交时查到的为准。
- 入账只经 `post_transaction(TOPUP, reference_type=PAYMENT, reference_id=public_id)`，原因写进账本行的 `description`；
  支付转 `PAID`，`confirmed_amount` / `confirmed_currency` 写实收，`paid_at` 为提交时刻。详情页的 `credited_amount` 是账本行的金额：
  `wallet_credit_amount` 仍是建单时的计划入账额，**实际入账额以账本行为准**。
- 复机、计费状态与复机通知由现有账本逻辑在同一事务里完成；`DISABLED` 租户照常入账（与 A 一致）。
- 同一笔不会入账两次：两个管理员同时提交、或与自动确认并发时，后到者得到 409，账本引用 `(PAYMENT, public_id)` 唯一。

### 6.4 「不入账结案」与之后在 Stripe 退款

- 支付转 `FAILED`，不写账本。**钱需要退给客户时，由管理员在 Stripe Dashboard 后台手工退款**（spec §112.1：不做自动退款）。
- **`GATEWAY_PAYMENT_REUSED` 的复核单结案时不退款**：它的 PaymentIntent 记在另一笔已 `PAID` 的支付上，这笔钱已经给那一笔入过账，
  退款等于把已入账的钱退回、要先扣回那一笔。若已误退，按 §7 第一种（`PAID`）处理扣回。
- `GATEWAY_REFUNDED_OR_DISPUTED` 的复核单结案时也**不再退款**（Stripe 上已退过或正在拒付）。
- 结案后在 Stripe 退款会产生 `charge.refunded` 事件。是否告警以设计「告警」表为准：
  - 事件能按 PaymentIntent 关联到这笔 `resolution = CLOSED` 的支付 → **不计入 P1**（这是预期结果，钱从没入过账，不需要扣回）。
  - 关联不上（例如 `GATEWAY_INCONSISTENT` 时 Stripe 没给 PaymentIntent、本行没记下）→ 事件为 `UNMATCHED`，**照样计入 P1**
    （也计入 `unmatched or rejected` 的 P2）：在 Stripe Dashboard 核对它确实是刚做的那笔结案退款即可，不改账，P1 在 24 小时后消失。

### 6.5 结案后是终态

- 结案后（`resolution` 不为空）这笔支付对一切自动路径都是终态：事件任务与补对账调 `confirm_payment` 时只更新 `last_reconciled_at`；
  补对账第 1 段不选它；之后 Stripe 再报付款成功，`CLOSED` 的支付也不会入账或回到 `REVIEW_REQUIRED`。
- 数据库 CHECK `ck_payments_resolution_fields` 兜底：任何路径把已结案的行改成别的状态都会被拒。
- 唯一的例外是手续费：`CREDITED` 的支付会被补对账第 2 段补写 `gateway_fee`（§5.4），状态与结案字段不变。
- 结案不能撤销，管理端对已结案的支付没有任何操作。

## 7. 争议与退款

Stripe 侧的争议（拒付，`charge.dispute.created`）与在 Stripe 后台的退款（`charge.refunded`）：**订阅、落库、关联、P1 告警，不改账**
（spec §8、§112.1：不做自动退款、不对争议自动扣回，外部退款不得直接改余额）。

- **落库**：回调把这两种事件落为 `RECEIVED`（需要回调端点订阅它们，§8 第 1 步）。`metadata_json` 另存 `object_id`（`ch_…` / `dp_…`）、
  `payment_intent`、`amount`、`amount_refunded`（Charge）、`currency`、`status`（Dispute）、`charge`（Dispute 的 charge id）；
  **不存** `billing_details`、`receipt_email` 等个人数据。
- **处理**：事件任务对它们不查 Stripe、不调 `confirm_payment`，只按 `metadata_json.payment_intent` 找 `payments.gateway_payment_intent_id`：
  找到 → 写事件的 `payment_id`，`PROCESSED`；没有 `payment_intent` 或找不到 → `UNMATCHED`（`last_error_code = PAYMENT_NOT_FOUND`，计入 P2）。
  **支付状态与钱包都不变。**
- **告警**：最近 24 小时内、不是 `IGNORED` 的这两种事件计入 `disputes or refunds 24h` P1，**不含**「`charge.refunded` 且关联的支付
  `resolution = CLOSED`」；关联不上的（`UNMATCHED`）照样计入。24 小时后自动不再计入。查询语句见
  [runbook.md「支付」一节](runbook.md#支付复核事件冲突事件积压与配置wallet_negative_balance-告警的支付部分)第 4 步。

按事件关联到的支付逐条处理：

1. **支付是 `PAID`**（自动入账或人工「按实付入账」）：Kelvin 在 Stripe Dashboard 核实争议或退款的金额后，需要扣回时用现有的人工调账
   `REFUND_ADJUSTMENT`（借方）扣回，**原因里写支付编号**（`public_id`）。支付本身保持 `PAID`：扣回是一条新的账本行，不改原来的 `TOPUP`（INV-4、INV-5）。
2. **支付是「不入账结案」（`resolution = CLOSED`）且事件是 `charge.refunded`**：这是结案后在 Stripe 后台退款的预期结果，钱从没入过账，
   不需要扣回；**这种事件不计入 P1**。
3. **`GATEWAY_PAYMENT_REUSED` 的复核单**：它的 PaymentIntent 记在另一笔已 `PAID` 的支付上（A 只在 PaymentIntent 未被占用时写入），
   这笔钱已经给那一笔入过账。所以这种复核单**结案时不在 Stripe 退款**（退款等于把已入账的钱退回，需要先扣回那一笔）；
   若误退，退款事件会关联到那笔 `PAID`，按第 1 条扣回。
4. **不入账结案的支付没记下 PaymentIntent**（例如 `GATEWAY_INCONSISTENT` 时 Stripe 没给 PaymentIntent）：结案后在 Stripe 退款产生的
   `charge.refunded` 关联不上任何支付，事件为 `UNMATCHED`，**照样计入 P1**（系统无法确认它就是那笔结案退款）：管理员在 Stripe Dashboard
   核对它确实是刚做的那笔退款即可，不改账，P1 在 24 小时后消失。
5. **争议落在不入账结案的支付上**：钱从没入过账，不需要扣回；在 Stripe 里应诉或接受即可。这种争议照样计入 P1。
6. **争议或退款落在仍是 `REVIEW_REQUIRED` 的支付上**：先按 §6 结案（钱已被退或被拒付时选「不入账结案」，且不再在 Stripe 退款；
   即使误点「按实付入账」，核对也会以 `GATEWAY_REFUNDED_OR_DISPUTED` 拒绝，事件是否关联得上都一样），不改账。
7. **争议或退款事件为 `UNMATCHED`（以上以外的情况）**：在 Stripe Dashboard 按 PaymentIntent 找到对应的付款与它的 `client_reference_id`，
   再按它对应的支付归入以上各条。

**不要**因为争议或退款去改支付状态、改原 `TOPUP` 行或直接改钱包；扣回只用人工调账新增一行。

## 8. 对客户开放充值前的检查（管理员执行；不在 B 的代码里）

前提：B 的全部任务（AIH-TASK-179..187）已合并部署，§3 已做完（开关打开、`config missing` 不出现、`BILLING_PAYMENT_TOPUPS_OPEN` 仍为假、
建单允许名单已清空）。按顺序做，任一步不成功就停下。**由 Kelvin 执行。**

**1. 回调端点加订阅两种事件。**在 Stripe Dashboard 生产模式下，给 §3 建的回调端点加订阅 `charge.dispute.created`、`charge.refunded`。
加完后端点共订阅**六种**事件：

```text
checkout.session.completed
checkout.session.async_payment_succeeded
checkout.session.async_payment_failed
checkout.session.expired
charge.dispute.created
charge.refunded
```

端点的地址与签名密钥不变，不需要换 secret 文件。

**2. 验证丢失回调能被补回**（spec §127 验收「Lost Webhook is recovered by status reconciliation」）：

1. 把生产验收租户的 `public_id` 放进 `BILLING_PAYMENT_TOPUP_TENANT_ALLOWLIST`，重启 api、celery-worker、celery-beat。
2. 在 Stripe Dashboard 把回调端点**临时停用**。停用期间的事件 Stripe 不会补发，这笔支付只能靠补对账收口。
3. 用验收租户在门户建一笔 **RM10** 的真实充值并付款。
4. 等补对账：建单后 5 分钟（`BILLING_PAYMENT_RECONCILE_GRACE_SECONDS`）起，会话未过期时每 5 分钟查一次（§5.1）。
   在这个节奏内（通常建单后 5–10 分钟）核对（只读）：

   ```sql
   -- 支付已 PAID，last_reconciled_at 有值（由补对账确认）
   SELECT public_id, status, status_reason, expected_amount, confirmed_amount, gateway_fee, paid_at, last_reconciled_at
     FROM payments
    WHERE public_id = '<这笔支付的 payment_id>';

   -- 停用期间没有收到这笔的回调事件
   SELECT gateway_event_id, event_type, processing_status, received_at
     FROM payment_gateway_events
    WHERE payment_id = (SELECT id FROM payments WHERE public_id = '<这笔支付的 payment_id>');

   -- 钱包只加了一次：恰好一条 TOPUP
   SELECT transaction_type, amount, balance_before, balance_after, created_at
     FROM wallet_transactions
    WHERE reference_type = 'PAYMENT' AND reference_id = '<这笔支付的 payment_id>';
   ```

   等过了会话过期时刻一小时仍不是 `PAID`：停下，按 §5.3 与 runbook 排查，不要继续开放。
5. 在 Stripe Dashboard **重新启用**回调端点。
6. 把验收租户**从 `BILLING_PAYMENT_TOPUP_TENANT_ALLOWLIST` 里移除**（与 §3 第 5 步相同），重启。

这笔 RM10 留在验收租户的钱包里，**不冲回**。

**3. 充值界面（C）上线之后再打开 `payment_topups_open`**：先核对支付设置，再设 `BILLING_PAYMENT_TOPUPS_OPEN=true` 并重启，步骤与回滚见 §13。在那之前保持为假。

## 9. 只读排查：`UNMATCHED` 与 `REJECTED`

`REVIEW_REQUIRED` 已有处理出口（§6），不再在这里排查。事件的 `UNMATCHED` 与 `REJECTED` 仍按下面只读排查：

- **不要**改 `payments.status`、补 `TOPUP` 账本行或直接改钱包（INV-3、INV-4）；
- **不要**改事件的 `processing_status`、`attempts`、`next_attempt_at`，也不要删 `payment_gateway_events` 的行（它是去重依据）；
- 结论记进工单。

查询在 mysql 容器里执行（`docker compose exec mysql sh -c 'mysql -u"$MYSQL_USER" -p"$MYSQL_PASSWORD" "$MYSQL_DATABASE"'`），
`metadata_json` 只有白名单字段，没有个人数据。

### 9.1 `REVIEW_REQUIRED`

见 [§6 复核处理](#6-复核处理)：在管理端「支付」页核对并选「按实付入账」或「不入账结案」，错误码按 §6.2 处理。

### 9.2 `UNMATCHED`

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

多半是：不是本平台建的会话（例如 Dashboard 手工建的付款链接），或测试与生产的端点配串了。核对 Stripe Dashboard 的回调端点只订阅 §8 第 1 步的六种事件、
指向正确的环境。`client_reference_id` 看起来是本平台的 `public_id` 却找不到时，按数据事故上报。
`charge.dispute.created` / `charge.refunded` 的 `UNMATCHED` 是按 `payment_intent` 关联不上，按 §7 处理。

### 9.3 `REJECTED`

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
| `NO_GATEWAY_REFERENCE` | 建单时 Stripe 超时、支付没有会话 id，事件也不带会话 id，支付已过 `expires_at` | 补对账对没有会话 id 的支付不查 Stripe，只在 `expires_at` 之后 10 分钟置 `EXPIRED`（`NO_GATEWAY_SESSION`，§5.2）；之后带会话 id 的付款成功事件仍会认回并入账。按支付 `public_id` 在 Stripe Dashboard 搜索是否真有付款，有付款而一直没入账时记进工单交 Kelvin |

## 10. 示例的写法

本文件与 PR、工单里的示例一律用占位符（`<平台对外域名>`、`<Stripe API 根地址>`、`<payment_id>`），**不写真实主机名、地址、租户 id 与密钥**（ADR-0001）。

## 11. 支付设置

建单的金额规则、复用窗口与待付超时由管理端「支付设置」维护（spec §42：最低额「通过管理端设置配置」、前端不许写死），
取代 A 的四个配置项 `payment_topup_min`、`payment_topup_max`、`payment_pending_timeout_seconds`、`payment_reuse_window_seconds`
（已删除，设了也不再读取，见 [deployment.md「支付配置与 secret 文件」](deployment.md#支付配置与-secret-文件)）。
`BILLING_PAYMENTS_ENABLED`、`BILLING_PAYMENT_TOPUPS_OPEN`、建单允许名单是运维开关，**不在**支付设置里。

- 入口：管理端「支付设置」页（`/payment-settings`），对应接口 `GET` / `PUT /api/v1/admin/payment-settings`，只有 ADMIN 能调用（登录必过 2FA），
  契约见 [api.md](api.md)。客户在门户充值页通过 `GET /api/v1/customer/topup-options` 读到这些值，门户不写死任何金额。
- 存储：表 `payment_settings`，恰好一行（迁移 0038 插入种子行，种子值等于 A 的配置缺省，所以部署前后建单行为不变）。

### 11.1 字段

金额一律是两位小数的定点字符串（MYR，格式 `^[0-9]{1,6}(\.[0-9]{1,2})?$`，所以不会超过 `999999.99`）。

| 字段 | 含义 | 范围 | 种子值 |
| --- | --- | --- | --- |
| `min_amount` | 最低充值额 | `≥ 2.00`（ADR-0015 的 MYR 下限） | `10.00` |
| `max_amount` | 最高充值额（单笔上限） | 可为空（空 = 不另设上限，只受格式限制的 `999999.99`）；设了时 `≥ min_amount` | 空 |
| `recovery_buffer` | 最小恢复额的余额缓冲（§12） | `≥ 0.01`，**不能为 0** | `10.00` |
| `preset_amounts` | 充值页的推荐金额 | 1–8 个；每个符合金额格式、互不相同、严格升序、都 `≥ min_amount`，且（`max_amount` 不为空时）`≤ max_amount` | `50.00`、`100.00`、`300.00`、`500.00`、`1000.00` |
| `reuse_window_seconds` | 同额复用窗口（ADR-0007，§2.1） | 0–300 的整数 | `60` |
| `pending_timeout_seconds` | 待付超时，也是 Stripe 会话的过期时刻（`expires_at = 建单时刻 + 它`；Stripe 要求创建后 30 分钟到 24 小时） | 2400–86400 的整数 | `3600` |

- 每次保存都要给全部字段（整体替换）。类型不对（金额不是字符串、推荐金额不是字符串数组、秒数不是整数，布尔值与字符串形式的数字都不算）→
  422 `VALIDATION_ERROR`；规则不过 → 422 `PAYMENT_SETTINGS_INVALID`，按上表的字段顺序点名**第一个**不合规的字段。
- 管理端页面：最高额留空表示不设上限；推荐金额用逗号分隔填写（保存前拆分、去掉首尾空白、丢掉空段）；两个秒数框必须是整数，否则页面提示、不发请求。

### 11.2 乐观锁

- 每次修改 `version` +1。保存时带上读到的 `version`；期间别的管理员已经改过 → 409 `PAYMENT_SETTINGS_VERSION_CONFLICT`，数据不变（别人的修改保留），
  页面提示「设置已被他人修改」并提供刷新按钮：刷新、重新核对后再改。两个管理员同时保存，后提交者得到 409，不会静默覆盖。
- 设置行缺失（只会在迁移没跑时出现）→ 503 `PAYMENTS_MISCONFIGURED`，不当作版本冲突；此时建单与充值选项也都是 503，客户的支付列表与详情不受影响。处理：跑迁移。

### 11.3 改动写审计

- 每次修改在同一事务里写一条 `PAYMENT_SETTINGS_UPDATE` 审计（`entity_type` 为 `payment_settings`、`entity_id` 为 `1`；
  `before` / `after` 为全部业务字段与 `version`）。设置不保留历史版本，**历史值只在审计里**。
- 新值与旧值完全相同时也照常 +1 版本并写审计（管理员的确认动作本身要留痕）。
- 日志只记管理员用户 id 与新版本号，不记请求体。

### 11.4 改动不影响已建好的支付

- 建单在快速校验开始时读一次设置，整个建单过程用这一份（§2.1）；改设置那一刻正在进行的建单可能按旧规则完成，这是可接受的（金额规则是输入校验，不是账本规则）。
- 已建好的支付不受影响：金额（`expected_amount`）与过期时刻（`expires_at`）在建单时已写进支付行，改待付超时不会延长或缩短已有的 Stripe 会话；
  入账仍按 A 与 B 的规则（`wallet_credit_amount`），与设置无关。
- 改复用窗口只影响之后建单时的候选查询；改最低额、最高额只影响之后的建单校验与充值页显示。

## 12. 最小恢复额

挂起（余额 ≤ 0）的 `PREPAID` 租户充值时，后端算出一个最小恢复额，保证按它充值后余额严格大于 RM0（spec §42、ADR-0010 方案 A）。

### 12.1 公式

- 只对 `billing_mode = PREPAID` 且钱包 `balance ≤ 0` 的租户有值，否则为空。
- **最小恢复额** = `(−balance + recovery_buffer)` **向上取到分**（两位小数，`ROUND_CEILING`）。钱包余额是 8 位小数，欠款可能有两位以上小数，
  向上取保证充值后余额 ≥ 缓冲。
- **生效的最低额** = `max(min_amount, 最小恢复额)`（没有最小恢复额时就是 `min_amount`）。建单金额低于它 → 422 `TOPUP_BELOW_RECOVERY_MINIMUM`（§2.1 第 3 步）；
  充值页的金额下限、推荐金额按钮可不可点都按它，并在表单上方提示「账户已暂停，至少充值 RM X 才能恢复服务」（X 为生效的最低额）。
- 计算全部在 `Decimal` 上做，不经 `float`。

例（`min_amount = 10.00`、`recovery_buffer = 10.00`）：

| 余额 | 最小恢复额 | 生效的最低额 |
| --- | --- | --- |
| `5.00` | 空 | `10.00` |
| `0` | `10.00` | `10.00` |
| `−0.00000001` | `10.01` | `10.01` |
| `−12.345` | `22.35` | `22.35` |
| `−50.00` | `60.00` | `60.00` |

缓冲为 `0.01`、余额 `−50.00` 时最小恢复额为 `50.01`，按它充值后余额为 `0.01`（> 0）。

### 12.2 为什么要缓冲（ADR-0010）

- 停机规则仍是余额 ≤ 0（ADR-0010 方案 A，不改停机阈值）。如果最小恢复额只保证余额比 0 多一分钱，客户充 RM0.01 复机、下一个请求就又停机，
  反复循环；缓冲让复机后还有一段可用余额。
- 最小恢复额只在**建单时**校验：从建单到付款之间余额还可能继续变化（在途用量），缓冲就是为这段留的余地。入账时不再校验（按 `wallet_credit_amount` 入账）。
- 缓冲**至少 0.01**：最小恢复额 ≥ 欠款 + 0.01，所以按生效的最低额充值后余额至少 RM0.01。缓冲若为 0，欠款是整分时（例如欠 RM50、充 RM50）
  充值后余额正好为 0、仍然停机，正是 ADR-0010 要消除的边界。
- 建单时先用不加锁读到的余额判一次，再在租户锁的事务里重读余额重判（排在复用查找之前）；若另一笔支付在这之间入账使余额变正，就按普通最低额判。

### 12.3 超过单笔上限时

单笔上限为 `max_amount`，为空时为 `999999.99`。生效的最低额大于单笔上限（欠款很大而上限不够）时：

- 充值选项返回 `available = false`、`unavailable_reason = RECOVERY_EXCEEDS_MAXIMUM`，充值页不显示表单，显示「需充值的金额超过单笔上限，请联系客服」；
- 建单时任何金额都被 422 拒绝：低于生效的最低额的为 `TOPUP_BELOW_RECOVERY_MINIMUM`，其余为 `TOPUP_AMOUNT_INVALID`；
- 处理：管理员在支付设置（§11）里调高最高额（不超过 `999999.99`）；仍不够，或不想调高上限时，用人工调账处理。

## 13. 对客户开放充值（管理员执行）

前提：C 的全部任务（AIH-TASK-188..194）已合并部署；§8 的开放前检查都已完成（开关打开、`config missing` 不出现、
`BILLING_PAYMENT_TOPUPS_OPEN` 仍为假、建单允许名单已清空）。按顺序做，任一步不成功就停下。**由 Kelvin 执行。**

**1. 核对支付设置。**在管理端「支付设置」页（§11）核对最低额、缓冲与推荐金额（顺带看一眼最高额、复用窗口与待付超时）。
要改就在页面上保存（写 `PAYMENT_SETTINGS_UPDATE` 审计）；保存得到 409 时刷新后重新核对。

**2. 打开建单。**宿主机 `.env` 设 `BILLING_PAYMENT_TOPUPS_OPEN=true`，重启 api、celery-worker、celery-beat。
之后所有合格的客户（`PREPAID`、账户可写）都能在门户「充值」页建单。

**回滚**：

1. 宿主机 `.env` 设 `BILLING_PAYMENT_TOPUPS_OPEN=false`，清空 `BILLING_PAYMENT_TOPUP_TENANT_ALLOWLIST`，然后重启（两者都是配置项，重启后生效）。
   `BILLING_PAYMENTS_ENABLED` 不动：回调与补对账要继续运行。
2. 之后原本能充值的客户建单得到 403 `TOPUPS_NOT_OPEN`，充值页显示「未开放」；内部租户也得到 `TOPUPS_NOT_OPEN`（开放检查排在计费模式之前，§2.1）；
   `DISABLED` 与 `CLOSED` 的客户照旧先得到 `ACCOUNT_READ_ONLY`（账户可写检查排在开放检查之前）。
3. 已建好的支付照常由回调与补对账收口；支付历史与付款结果页照常可看。

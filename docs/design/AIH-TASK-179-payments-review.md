# AIH-TASK-179..187 设计：支付第二批（补对账与过期、人工复核出口、争议与退款告警、手续费补抓、管理端支付页）（已批准 v3）

> **来源**：设计闸门 Issue #585。本文件是 `APPROVED: design v3` 那一版正文的**逐字副本**（送审时状态行为 `READY_FOR_REVIEW`，批准后 Issue 上只把状态行改成了 `APPROVED: design v3`）。审查：Codex 额度不可用期间由 Claude Code 审查（审查者由 Kelvin 2026-11-06 选定）。送审前按登记前预审（`worker.design_precheck`）三轮修订。v1 获批准后，登记前预审报出 §5 与 §2 矛盾，升 v2；v2 审查阻断「人工按实付入账没有核对 Stripe 侧的退款与争议」，升 v3（同时按预审把手续费补抓拆成 AIH-TASK-187），批准。各轮审查的测试建议项都补进了对应任务的契约。
> 放进仓库，是因为 OpenClaw Worker 在沙箱里不联网、读不到 GitHub Issue。
> **实现以本文件为准**；与 Issue 不一致时，以 Issue #585 上被批准的 v3 为准。设计要改，就回到 Issue 升版本、重新过闸门，不要直接改本文件。
> 本设计建立在支付第一批（设计闸门 #580 已批准 v3，`docs/design/AIH-TASK-172-payments-gateway.md`）之上。产品决定（管理端含页面、两个复核出口、争议与退款只告警）由 Kelvin 2026-11-06 拍板，见 §1。

## 0. 准入判定

| 任务涉及 | 要填的范围 |
| --- | --- |
| 钱包 / 账本 / 定价 / 汇率 / 支付 / 幂等 / 状态机 | **全部章节** |
| 用量摄取 / 认证与会话（集成侧与平台侧）/ Webhook | §1–§7（**§6 不可省** —— 密钥存储与轮换、鉴权、租户过滤、日志脱敏都在那一节） |
| 前端 / 文档 / CI / 脚本 | **不走本闸门**，直接开 PR |
| 既不碰钱、也不碰用量摄取 / 认证与会话 / Webhook 的后端改动 | **不走本闸门**，直接开 PR |

本任务档位：`全部`。理由：补对账会触发入账、人工复核出口直接入账、支付状态机新增跃迁、支付 Webhook 新增事件类型。

---

状态：`READY_FOR_REVIEW`
负责人：Claude
**设计版本**：`v3`
对应需求：spec §8、§43、§44、§60、§63、§66、§89、§112.1、§113、§118、§120、§127；Invariant 3；ADR-0007、ADR-0015；建立在设计闸门 #580（支付第一批，下称「A」）已批准的 v3 之上
目标 PR：待开（拆成 `AIH-TASK-179`..`187`，见 §11）

## 1. 目标与边界

### 要解决的问题

A 交付了建单、回调、可信确认与入账，但留下四个口子，都必须在对客户开放充值（`payment_topups_open`）之前补上：

1. **丢失回调**：Stripe 的回调没到，支付会一直停在 `PENDING`，客户付了钱却没入账（spec §43 最后一段、§127「Lost Webhook is recovered by status reconciliation」）。
2. **没有网关 id 的 `PENDING`**：建单时 Stripe 调用失败留下的单子，A 写明「由 B 置为过期」。
3. **`REVIEW_REQUIRED` 没有出口**：金额或币种不符、关户、内部租户、网关数据矛盾的单子，A 只置状态、发告警，没有人工处理的途径（spec §43「require audited manual resolution」、§63）。
4. **手续费可能暂缺**：A 的假设 2 写明拿不到时 `gateway_fee` 为空、由 B 补（spec §44）。

另外，管理员需要看到支付列表（spec §63、§89 `GET /api/v1/admin/payments`），并在 Stripe 侧发生争议（拒付）或在 Stripe 后台手工退款时得到告警。

### Kelvin 2026-11-06 的决定

| 问题 | 决定 |
| --- | --- |
| 管理端范围 | 后端接口 **加** 管理端页面（支付列表、筛选、详情、复核处理） |
| `REVIEW_REQUIRED` 的出口 | 两个：「按实付入账」（只限 MYR，租户不是关户、也不是内部租户，按 Stripe 实收金额入账，支付转 `PAID`）；「不入账结案」（支付转 `FAILED`，必须填原因，退款在 Stripe 后台手工做）。两者都写审计；入账仍以支付编号为账本引用，防重复 |
| 争议与 Stripe 后台退款 | 订阅、落库、P1 告警，**不改账**；由 Kelvin 核实后用现有的人工调账（`REFUND_ADJUSTMENT`）扣回（spec §8、§112.1：不做自动退款，外部退款不得直接改余额） |
| 审查者 | Claude（Codex 额度不可用期间） |

A 的决定（拆批、托管付款页、只存实际手续费、停用入账与关户复核、内部租户不能充值）沿用，不重开。

### 可观察的完成标准

1. **丢失回调被补回**：一笔有网关 id 的 `PENDING`，Stripe 侧已付款而本系统没收到任何回调，补对账在 §2「补对账」的节奏内调用 A 的 `confirm_payment` 把它置 `PAID` 并入账一次；之后再收到回调也不重复入账。
2. **过期**：Stripe 报会话已过期的 `PENDING` 转 `EXPIRED`（A 的映射）；没有网关 id 的 `PENDING` 在 `expires_at` 之后 10 分钟转 `EXPIRED`（原因 `NO_GATEWAY_SESSION`）；创建满 7 天仍是 `PENDING` 的转 `EXPIRED`（原因 `RECONCILE_TIMEOUT`）。过期后再收到付款成功照样入账（ADR-0007，A 的决策表），已人工结案的除外（见第 4 条）。
3. **管理端可见**：管理员在 `GET /api/v1/admin/payments` 与管理端「支付」页看到 spec §63 列出的字段（收据除外，属于 D），可按状态、客户、创建日期筛选；详情页列出这笔支付的回调事件。
4. **复核出口**：管理员对 `REVIEW_REQUIRED` 的支付选「按实付入账」或「不入账结案」并填原因；前者向 Stripe 重新查询、核对后按实收金额写一条 `TOPUP`，支付转 `PAID`；后者支付转 `FAILED`。两者都写 `PAYMENT_REVIEW_RESOLVED` 审计，结案后这笔支付对一切自动路径都是终态（数据库 CHECK 兜底）。
5. **争议与退款**：`charge.dispute.created`、`charge.refunded` 事件验签后落库、关联到支付、触发 P1 告警，钱包与支付状态不变。
6. **手续费补抓**：`PAID` 而 `gateway_fee` 为空的支付，在创建后 8 天内由补对账按小时补查，拿到 MYR 手续费后写入并记审计。
7. **告警**：长时间未决的 `PENDING`（P2）、未结案的 `REVIEW_REQUIRED`（P1，直到结案）、24 小时内的争议与退款（P1）并进 `wallet_negative_balance` 维度。

### 明确不做什么

- **不做客户侧的支付历史与充值界面、管理端设置、最小恢复额**：属于 C。
- **不做收据**：属于 D；管理端列表没有收据列。
- **不做自动退款，不对争议自动扣回**：spec §112.1；扣回走现有人工调账。
- **不对 `PENDING`、`EXPIRED`、`FAILED`、`CANCELLED`、`PAID` 提供人工操作**：本批的人工出口只针对 `REVIEW_REQUIRED`。
- **不新增 Healthchecks 检查**：并进已有维度（Kelvin 2026-10-17）。
- **不改账本、钱包、复机的现有逻辑**：只调用 `post_transaction`。
- **不改 A 的建单、回调验签与可信确认的判定**；只在 §2 列出的三处扩展 A：`confirm_payment` 把已结案的支付视为终态；回调与事件处理多认两种事件类型；`GatewayPaymentStatus` 多带两个字段（见「对 A 的适配器的扩展」），A 的状态映射不变。

### 现有行为与证据

- A（设计 #580 v3，`docs/design/AIH-TASK-172-payments-gateway.md`）的 `confirm_payment(payment_public_id, *, now)`：事务外查 Stripe，锁「支付 → 钱包 → 租户」，按两张决策表落状态；`PAID` 与 `REVIEW_REQUIRED` 不被它改变；`PENDING` / `EXPIRED` / `FAILED` / `CANCELLED` 收到通过判定的 `PAID` 都入账。它每次查询成功都更新 `last_reconciled_at`。
- A 的事件处理在支付没有网关 id 时从事件认回会话 id（条件更新 `gateway_payment_id IS NULL`，不看支付状态）。
- A 的告警把支付信号并进 `wallet_negative_balance`，摘要段落逐字为 `; payments: review 24h <a>, event conflicts 24h <b>, oldest open event <c>s, unmatched or rejected 24h <d>, config <ok|missing>`。
- 人工调账（`app/services/wallet_adjustments.py`，设计闸门 #111 v1）已支持 `REFUND_ADJUSTMENT`（借方），原因去掉首尾空白后 1–255 字，进账本 `description` 与审计，不进日志。
- 管理端接口每个处理函数显式调用 `require_admin`（`app/api/auth.py`；ADMIN 登录必过 2FA）；列表用 `page` / `page_size`（`app/api/admin_usage_events.py`）。
- 账本 `uq_wallet_transactions_reference (reference_type, reference_id)` 唯一；`ReferenceType.PAYMENT` 已存在。

## 2. 设计概要

### 数据流

```text
Beat 每 60 秒 → payment_reconcile.run_due（命名锁，预算 45 秒、最多 50 次 Stripe 查询）
  第 1 段：PENDING 补对账（到期条件写在查询里）
    无网关 id 且 now ≥ expires_at + 600 秒 → EXPIRED（NO_GATEWAY_SESSION）+ 审计
    有网关 id 且到期 → confirm_payment（A）→ PAID 入账 / EXPIRED / FAILED / REVIEW_REQUIRED / 不变
    创建满 7 天、查询后仍 PENDING → EXPIRED（RECONCILE_TIMEOUT）+ 审计
  第 2 段：手续费补抓
    PAID、gateway_fee 为空、创建 8 天内、到期 → get_payment_status → 写 gateway_fee + 审计
管理员 → GET /api/v1/admin/payments[/{id}]
管理员 → POST /api/v1/admin/payments/{id}/resolve {action, reason}
  CREDIT：事务外查 Stripe → 核对 → 事务：锁支付 → 钱包 → 租户 → 重查 → PAID + TOPUP + 审计
  CLOSE：事务：锁支付 → 重查 → FAILED + 审计
Stripe → 回调（A 的端点）→ charge.dispute.created / charge.refunded 落库 RECEIVED
  → 事件任务（A）：按 payment_intent 关联支付 → PROCESSED（不查 Stripe、不改支付）→ P1 告警
```

### 数据库（迁移 0037；`down_revision` 为 A 的 0036）

`payments` 加四列（全部可空，已有行全为空）：

| 列 | 类型 | 说明 |
| --- | --- | --- |
| `resolution` | VARCHAR(16) NULL，CHECK 为空或 `CREDITED` / `CLOSED`（`ck_payments_resolution`） | 人工结案的方式 |
| `resolved_at` | DATETIME NULL | |
| `resolved_by_user_id` | BIGINT NULL，FK `users.id` RESTRICT（`fk_payments_resolved_by_user_id`） | 操作的管理员 |
| `resolution_reason` | VARCHAR(255) NULL | 管理员填的原因（去掉首尾空白后 1–255 字） |

CHECK `ck_payments_resolution_fields`：

```text
(resolution IS NULL AND resolved_at IS NULL AND resolved_by_user_id IS NULL AND resolution_reason IS NULL)
OR (resolution IS NOT NULL AND resolved_at IS NOT NULL AND resolved_by_user_id IS NOT NULL
    AND resolution_reason IS NOT NULL AND review_required_at IS NOT NULL
    AND ((resolution = 'CREDITED' AND status = 'PAID') OR (resolution = 'CLOSED' AND status = 'FAILED')))
```

它让「已结案」在数据库层面成为终态：任何路径把已结案的行改成别的状态都违反 CHECK。索引：`ix_payments_resolved_by_user_id (resolved_by_user_id)`（外键列的显式索引，与 A 同一理由）。`payment_gateway_events` 不改表结构。

审计动作新增两个：`PAYMENT_REVIEW_RESOLVED`（人工结案）、`PAYMENT_FEE_RECORDED`（补抓到手续费）；前端审计动作清单同步。补对账引起的状态改变沿用 A 的 `PAYMENT_STATUS_CHANGE`（操作者为系统）。

**状态原因**新增两个取值：`NO_GATEWAY_SESSION`、`RECONCILE_TIMEOUT`。

### 对 A 的 `confirm_payment` 的一处修改

A 第 3 步「已是 `PAID` 或 `REVIEW_REQUIRED` → 只更新 `last_reconciled_at`，返回」改为「已是 `PAID` 或 `REVIEW_REQUIRED`，**或 `resolution` 不为空** → 只更新 `last_reconciled_at`，返回」。理由：「不入账结案」的支付状态是 `FAILED`，按 A 的决策表 `FAILED` 收到 `PAID` 会自动入账或再次进入 `REVIEW_REQUIRED`，等于推翻人工结论。CHECK `ck_payments_resolution_fields` 是兜底：即使漏掉这条规则，写入也会被拒（A 把它当可重试冲突，事件会一直 `RETRY`，由 15 分钟积压告警暴露）。

### 对 A 的适配器的扩展

`GatewayPaymentStatus` 加两个字段，取自 `get_payment_status` 已经展开的 `payment_intent.latest_charge`（A 的请求不变，不多发请求）：

| 字段 | 来源 | 没有 `latest_charge` 时 |
| --- | --- | --- |
| `charge_amount_refunded_minor` | `latest_charge.amount_refunded`（整数分） | `None` |
| `charge_disputed` | `latest_charge.disputed`（布尔） | `None` |

A 的状态映射与可信确认不用这两个字段（自动入账在付款成功后立即发生，之后的退款与争议走「争议与退款」的告警）；只有人工 CREDIT 用它们，防止把已经退回客户或正在拒付的钱记进钱包（spec §112.1：外部退款限于核实过的可退金额）。

### 补对账：`app/services/payment_reconcile.py` 与 `app/tasks/payment_reconcile.py`

- Beat 每 60 秒（配置 `payment_reconcile_poll_seconds`；条目名 `payment-reconcile-due`，`expires` 为间隔减 1 秒）执行 `app.tasks.payment_reconcile.run_due`。开关为假或 `get_payment_gateway` 返回 `None` 时直接返回（配置告警沿用 A）。
- **同一时刻只跑一份**：开始时取 `GET_LOCK(CONCAT('payment_reconcile:', DATABASE()), 0)`（照 A 与 `app/ai_api/delivery.py`；SQLite 下跳过），取不到直接返回。
- 每轮预算 45 秒（`time.monotonic` 计）、最多 50 次 Stripe 查询（两段共用）；`now` 未注入时**本轮开始读一次当前 UTC 时间，整轮共用**（候选查询、满上限的判断、写入的时刻都用它；一轮最长 45 秒，写入时刻最多早 45 秒，可以接受）。
- **第 1 段：`PENDING` 补对账**。**到期条件全部写在查询里**（不先取一批再在应用里过滤，免得一批未到期的老单挡住到期的新单）。时刻参数都由应用按 `now` 算好再绑定，不用数据库的日期函数。候选：

  ```text
  status = 'PENDING' AND created_at <= now − grace
  AND (
    (gateway_payment_id IS NULL AND expires_at <= now − 600)                                  -- 孤儿
    OR (gateway_payment_id IS NOT NULL AND (
         created_at <= now − max                                                               -- 已满上限
      OR (expires_at > now AND created_at > now − 7200 AND (lr IS NULL OR lr <= now − 300))    -- 未过期、建单 2 小时内
      OR (expires_at > now AND created_at <= now − 7200 AND (lr IS NULL OR lr <= now − 1800))  -- 未过期、建单超过 2 小时
      OR (expires_at <= now AND expires_at > now − 3600 AND (lr IS NULL OR lr <= now − 300))   -- 过期后 1 小时内
      OR (expires_at <= now − 3600 AND (lr IS NULL OR lr <= now − 21600))                      -- 过期 1 小时以后
    ))
  )
  ORDER BY COALESCE(last_reconciled_at, created_at), id
  LIMIT 200
  ```

  其中 `grace` 为 `payment_reconcile_grace_seconds`（缺省 300 秒，给回调先到的机会），`max` 为 `payment_reconcile_max_seconds`（缺省 604800 秒，即 7 天），`lr` 为 `last_reconciled_at`。按「最久没查过的先查」排序，同一批里查不完的下一轮继续。节奏以 `expires_at` 为准而不是只看年龄：A 允许待付超时在 2400–86400 秒之间，会话开着时每 5 或 30 分钟查一次，**过期后的第一个小时每 5 分钟查一次**，保证 Stripe 侧会话一结束就能收口（与下面「告警」里过期 1 小时的 P2 对上）。逐笔：
  1. `gateway_payment_id` 为空（只会因孤儿条件入选）→ **孤儿过期**（见下）。不查 Stripe（没有可查的 id；Stripe 侧即使有会话，它的过期时刻也等于 `expires_at`，600 秒后必定已过期）。
  2. 有网关 id → 调 A 的 `confirm_payment`：
     - 正常返回 → A 已按决策表落状态、入账、审计，并更新了 `last_reconciled_at`；若支付已满上限且重读后仍是 `PENDING` → **超时过期**。
     - `GatewayUnavailable` → **结束本轮**，不再查后面的支付（Stripe 不可用时不连续冲击），下一轮再来；`last_reconciled_at` 不变；不做超时过期。
     - `GatewayRequestRejected`（例如会话不存在）→ 用条件更新（`status = 'PENDING'`）把 `last_reconciled_at` 置为 now，记 ERROR（带 Stripe 错误码与 `public_id`），继续下一笔；不改状态；若已满上限 → **超时过期**。
     - `ConfirmConflict`（A 的契约 AIH-TASK-174 定义：确认事务写入撞唯一约束、整体回滚后抛出，调用方按可重试处理）→ 跳过，下一轮再来；不做超时过期。
- **孤儿过期 / 超时过期**：新事务，`SELECT … FOR UPDATE` 锁 `payments` 行（只锁这一行，不锁钱包与租户），重查：仍是 `PENDING`、`resolution` 为空，孤儿过期还要求 `gateway_payment_id` 仍为空 → 置 `EXPIRED`，`status_reason` 为 `NO_GATEWAY_SESSION` / `RECONCILE_TIMEOUT`，写 `PAYMENT_STATUS_CHANGE` 审计（系统），提交；重查不满足 → 什么也不做。之后若收到这笔的付款成功事件，A 的事件处理照常认回会话 id、确认、入账（`EXPIRED` → `PAID`，ADR-0007）。
- **第 2 段：手续费补抓**（第 1 段结束后、预算与查询次数还有剩余时）。到期条件同样写在查询里：`status = 'PAID' AND gateway_fee IS NULL AND gateway_payment_id IS NOT NULL AND created_at >= now − 8 天 AND paid_at <= now − 600 AND (last_reconciled_at IS NULL OR last_reconciled_at <= now − 3600)`，`ORDER BY COALESCE(last_reconciled_at, paid_at), id`，最多 50 行（老单拿不到手续费会轮到后面，不会一直挡在前面）。逐笔：
  1. 事务外 `get_payment_status`。`GatewayUnavailable` → 结束本轮；`GatewayRequestRejected` → 只更新 `last_reconciled_at`（条件更新 `status = 'PAID'`），记 ERROR。
  2. 返回的 `session_id` 不等于 `gateway_payment_id` → 只更新 `last_reconciled_at`，记 ERROR（不写手续费）。
  3. 有 `fee_minor` 且费用币种（转大写）为 `MYR` → 新事务，条件更新 `UPDATE payments SET gateway_fee = fee_minor / 100, last_reconciled_at = now WHERE id = ? AND status = 'PAID' AND gateway_fee IS NULL`，影响 1 行时同一事务写 `PAYMENT_FEE_RECORDED` 审计（系统；`before` 为 `{"gateway_fee": null}`，`after` 为 `{"gateway_fee": "<定点字符串>"}`，写法照 `app/repositories/wallet.py` 的 `_money_text`），提交；影响 0 行（并发已写）→ 什么也不做。
  4. 没有 `fee_minor` → 只更新 `last_reconciled_at`；费用币种不是 MYR → 只更新 `last_reconciled_at`，记 WARNING（不写，与 A 相同）。
  - 创建超过 8 天仍拿不到的，`gateway_fee` 保持为空（只影响内部毛利分析，不影响入账）。
  - 人工「按实付入账」（`resolution = CREDITED`）的支付也是 `PAID`，同样会被补手续费：这只写 `gateway_fee` 与 `last_reconciled_at`，不改状态与结案字段，钱确实收到了，手续费应当记下。
- **单笔的意外错误**（上面没列出的异常，例如过期或手续费事务里的数据库错误）：回滚这一笔的事务，记 ERROR（只带 `public_id` 与异常类型），**结束本轮**（数据库出错通常不是单行问题，不在同一轮里连续冲击），下一轮按候选查询重来；这一笔的数据不变。
- 补对账与事件处理可能同时对同一笔调 `confirm_payment`：A 的「锁支付行后再判、`PAID` 不再变、账本引用唯一」保证只入账一次。

### 人工复核出口：`app/services/payment_review.py`

`resolve_review(session_factory, gateway, *, actor, payment_id, action, reason, context, now) -> str`（返回支付的 `public_id`；接口层提交后再用详情查询组装响应，详情查询属于接口那一层）：`gateway` 是 `PaymentGateway | None`，由接口层传入（`payments_enabled` 为真时取 `get_payment_gateway(settings)`，否则为 `None`）；`action` 为 `CREDIT` 或 `CLOSE`，`reason` 已由请求层去掉首尾空白、长度 1–255。检查顺序：先共同前提（404 / 409），再看网关（只有 CREDIT 需要网关；`gateway` 为 `None` 时 503 `PAYMENTS_MISCONFIGURED`）；CLOSE 不需要网关，开关关闭时也能结案。

共同前提（不加锁读一次，事务内再查一次）：支付存在（否则 404 `PAYMENT_NOT_FOUND`）；`status = 'REVIEW_REQUIRED'` 且 `resolution` 为空（否则 409 `PAYMENT_NOT_IN_REVIEW`）。

**CLOSE（不入账结案）**：一个事务：`SELECT … FOR UPDATE` 锁支付行，重查前提；置 `status = 'FAILED'`（`status_reason` 保留进入复核时的原因）、`resolution = 'CLOSED'`、`resolved_at = now`、`resolved_by_user_id`、`resolution_reason`；写 `PAYMENT_REVIEW_RESOLVED` 审计；提交。不写账本。退款由管理员在 Stripe 后台手工做（spec §112.1）；之后 Stripe 发的 `charge.refunded` 照「争议与退款」一节告警，不改账（这笔从未入账，不需要扣回）。

**CREDIT（按实付入账）**：

1. 不加锁读支付，检查前提；`gateway_payment_id` 为空 → 409 `GATEWAY_REFERENCE_MISSING`（A 的路径不会产生，防御）；`gateway` 为 `None` → 503 `PAYMENTS_MISCONFIGURED`。
2. **事务外**调 `get_payment_status(gateway_payment_id)`：`GatewayUnavailable` → 503 `PAYMENT_GATEWAY_UNAVAILABLE`；`GatewayRequestRejected` → 409 `GATEWAY_QUERY_REJECTED`。
3. 核对 Stripe 的权威状态，**按顺序**，第一条不满足即拒绝、不改任何数据：
   | 条件 | 不满足时 |
   | --- | --- |
   | `client_reference_id` 等于 `public_id` 且 `session_id` 等于 `gateway_payment_id` | 409 `GATEWAY_REFERENCE_MISMATCH` |
   | `state = PAID`（A 的映射：会话 `complete`、`payment_status = paid`、PI `succeeded` 且实收等于应收） | 409 `GATEWAY_NOT_PAID` |
   | 币种为 `MYR` | 409 `CURRENCY_NOT_MYR` |
   | `payment_intent_id` 不为空 | 409 `GATEWAY_NOT_PAID` |
   | `charge_amount_refunded_minor` 与 `charge_disputed` 都不为 `None`（拿得到 charge 的退款与争议信息） | 409 `GATEWAY_NOT_PAID` |
   | `charge_amount_refunded_minor = 0` 且 `charge_disputed` 为假（没有退过款、没有争议） | 409 `GATEWAY_REFUNDED_OR_DISPUTED` |
   入账额 `credit_amount = amount_received_minor / 100`（Decimal，精确两位；等于 `amount_minor / 100`）。
4. 新事务：`SELECT … FOR UPDATE` 依次锁支付行、该租户的钱包行、租户行（与 A 的顺序相同），重查：
   - 前提仍成立（否则 409 `PAYMENT_NOT_IN_REVIEW`）；
   - 支付行的 `gateway_payment_intent_id` 为空或等于 Stripe 返回的 PI（否则 409 `GATEWAY_REFERENCE_MISMATCH`）；
   - 该 PI 没有被另一笔支付占用（`gateway_payment_intent_id = PI AND id <> 本行`；否则 409 `GATEWAY_PAYMENT_REUSED`——这笔钱已经给另一笔支付入过账）；
   - 租户 `account_status` 不是 `CLOSED`（否则 409 `TENANT_CLOSED`，只能结案并在 Stripe 退款）；
   - 租户 `billing_mode` 是 `PREPAID`（否则 409 `INTERNAL_TENANT`）。
5. 写入：`status = 'PAID'`（`status_reason` 保留进入复核时的原因）、`confirmed_amount = credit_amount`、`confirmed_currency = 'MYR'`、`paid_at = now`、`gateway_payment_intent_id = PI`、`gateway_fee`（拿得到且为 MYR 时，写法同 A）、`last_reconciled_at = now`、`resolution = 'CREDITED'`、`resolved_at`、`resolved_by_user_id`、`resolution_reason`；调用 `post_transaction(transaction_type=TOPUP, amount=credit_amount, reference_type=PAYMENT, reference_id=public_id, description=reason, …)`，操作者身份照 `app/services/wallet_adjustments.py` 传管理员；写 `PAYMENT_REVIEW_RESOLVED` 审计；提交。`DISABLED` 租户照常入账（与 A 一致）。
6. `post_transaction` 与写入的各种结果：返回 `replayed=True`（账本里已有这笔支付的 TOPUP，前面的锁与状态检查下不该出现）→ 整个事务回滚，409 `RESOLUTION_CONFLICT`；抛 `LedgerConflict` 或写入撞唯一约束（PI 或账本引用，即并发者先一步）→ 整个事务回滚，409 `RESOLUTION_CONFLICT`；`InvalidAmount` 的问题码为 `BALANCE_OUT_OF_RANGE` → 回滚，沿用人工调账的 422 `BALANCE_OUT_OF_RANGE`；其他 `InvalidAmount` 与 `InvalidTransaction` 是程序错误，回滚后原样抛出（500）。

各错误码的处理（186 写进 `docs/payment-flow.md`）：

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

说明：

- **入账额以 Stripe 实收为准**，可能不等于 `expected_amount`（金额不符正是进入复核的常见原因）。`wallet_credit_amount` 保持 A 的含义（建单时的计划入账额，CHECK 等于 `expected_amount`），**实际入账额以账本行为准**：自动入账时等于 `wallet_credit_amount`，人工入账时等于 `confirmed_amount`。管理端详情返回账本行的金额（`credited_amount`）。
- 不允许入账的原因码不是靠 `status_reason` 判断，而是靠第 3、4 步对 Stripe 与数据库的**重新核对**：例如 `AMOUNT_MISMATCH` 可以入账（按实收），`GATEWAY_PAYMENT_REUSED` 在第 4 步被拒，`CURRENCY_MISMATCH` 在第 3 步被拒，`GATEWAY_INCONSISTENT` 只有 Stripe 现在报 `PAID` 时才能入账。
- 同一笔不会入账两次：账本 `(PAYMENT, public_id)` 唯一；结案后 CHECK 让它成为终态。

审计 `PAYMENT_REVIEW_RESOLVED`：`entity_type` 为 `payments`、`entity_id` 为 `public_id`；`before` 为 `{status, status_reason, resolution: null, confirmed_amount, confirmed_currency}`，`after` 为 `{status, status_reason, resolution, confirmed_amount, confirmed_currency, credited_amount（CLOSE 时为 null）, resolution_reason}`；金额写法同 `_money_text`。

### 管理端接口：`app/api/admin_payments.py`

每个处理函数显式调用 `require_admin`；路由**总是注册**（与开关无关：开关关闭时列表为空，读接口照常可用）。结案接口按「人工复核出口」一节取 `gateway` 传给 `resolve_review`：CREDIT 在开关关闭或凭据读不到时 503 `PAYMENTS_MISCONFIGURED`（在 404 / 409 前提检查之后）；CLOSE 不受开关影响。

| 方法与路径 | 说明 |
| --- | --- |
| `GET /api/v1/admin/payments` | 查询参数 `page`、`page_size`（与 `admin_usage_events` 相同的上下限）、`status`（六种之一，可选）、`customer_id`（租户 `public_id`，可选）、`created_from` / `created_to`（`YYYY-MM-DD`，按**马来西亚时间（UTC+8）**的日历日解释、含两端：`created_from` 当天 00:00 MYT 起，到 `created_to` 次日 00:00 MYT 止，服务端换算成 UTC 比较；严格解析：必须匹配 `^[0-9]{4}-[0-9]{2}-[0-9]{2}$` 且是合法日期，年份在 2000–2099 之间，否则 422 `VALIDATION_ERROR`；`created_from` 晚于 `created_to` 也是 422 `VALIDATION_ERROR`）；未知查询参数忽略（与其他列表一致）。按 `created_at` 降序、`id` 降序 |
| `GET /api/v1/admin/payments/{payment_id}` | `payment_id` 是 `public_id`；不存在 404 `PAYMENT_NOT_FOUND` |
| `POST /api/v1/admin/payments/{payment_id}/resolve` | 请求体 `{"action": "CREDIT" | "CLOSE", "reason": "<1–255>"}`，`extra="forbid"`，原因类型沿用人工调账的 `Reason`；成功 200 返回详情（`resolve_review` 提交后，用与 `GET` 详情相同的查询重新读出） |

列表项字段：`id`、`customer_id`、`customer_name`、`amount`（`expected_amount`）、`currency`、`gateway`（`STRIPE`）、`gateway_reference`（`gateway_payment_id`）、`gateway_fee`、`status`、`status_reason`、`resolution`、`created_at`、`paid_at`、`review_required_at`。详情另加：`confirmed_amount`、`confirmed_currency`、`credited_amount`（这笔支付的 `TOPUP` 账本行金额，没有为 null）、`gateway_payment_intent_id`、`expires_at`、`last_reconciled_at`、`resolved_at`、`resolved_by`（管理员的邮箱，与审计查询的写法一致）、`resolution_reason`、`events`（这笔支付关联的事件，按 `received_at` 升序、同一时刻按 `id` 升序，最多 100 条：`gateway_event_id`、`event_type`、`processing_status`、`last_error_code`、`received_at`、`processed_at`、`fingerprint_conflict_at`）。金额字段的写法照现有管理端（Decimal 序列化为定点字符串）。

错误码汇总：404 `PAYMENT_NOT_FOUND`；409 `PAYMENT_NOT_IN_REVIEW`、`GATEWAY_REFERENCE_MISSING`、`GATEWAY_QUERY_REJECTED`、`GATEWAY_REFERENCE_MISMATCH`、`GATEWAY_NOT_PAID`、`GATEWAY_REFUNDED_OR_DISPUTED`、`CURRENCY_NOT_MYR`、`GATEWAY_PAYMENT_REUSED`、`TENANT_CLOSED`、`INTERNAL_TENANT`、`RESOLUTION_CONFLICT`；422 `VALIDATION_ERROR`、`BALANCE_OUT_OF_RANGE`；503 `PAYMENTS_MISCONFIGURED`、`PAYMENT_GATEWAY_UNAVAILABLE`。响应用仓库现有的错误信封。

### 管理端页面

- 新路由「支付」列表页（路径 `/payments`）与详情页（`/payments/:paymentId`），加进管理端导航。
- 列表：状态、客户、创建日期区间（马来西亚时间的日历日，与接口一致，输入框标 MYT）筛选；表格列为列表项字段（不含收据），时刻按 +8 显示并标 MYT；空列表显示空状态。
- 详情：基本字段、事件表；`REVIEW_REQUIRED` 时显示复核面板：「按实付入账」「不入账结案」两个操作，都要填原因并二次确认；确认框写明「入账金额以提交时向 Stripe 查询到的实收金额为准」，并把当前的 `confirmed_amount` 按 `confirmed_currency` 作为参考显示（`confirmed_amount` 为空时显示「Stripe 未报金额」；金额不为空而 `confirmed_currency` 为空时只显示数字并标「币种未知」；币种不是 MYR 时照样按它的币种显示，提交会得到 409 `CURRENCY_NOT_MYR`）。提交后按返回的详情刷新；409 / 503 显示后端的错误信息。
- 列表页有浏览器验收（只读，见 §11 的 184）。

### 争议与退款（扩展 A 的回调与事件处理）

- **事件类型**：在 A 的四种之外，回调把 `charge.dispute.created`、`charge.refunded` 也落为 `RECEIVED`（其余类型仍 `IGNORED`）。Stripe Dashboard 的回调端点相应加订阅这两种（开启步骤由 186 写进 `docs/payment-flow.md`）。
- **白名单字段**（`metadata_json`，扩展 A 的 §6）：A 的公共字段 `type`、`livemode`、`created` 照存；对象是 Charge / Dispute，另取 `object_id`（`ch_…` / `dp_…`）、`payment_intent`、`amount`、`amount_refunded`（Charge）、`currency`、`status`（Dispute）、`charge`（Dispute 的 charge id）；对象里没有的键不写。**不取** `billing_details`、`receipt_email` 等个人数据。
- **处理**（A 的事件任务按类型分支）：这两种不查 Stripe、不调 `confirm_payment`；按 `metadata_json.payment_intent` 查 `payments.gateway_payment_intent_id`：找到 → 写事件的 `payment_id`，`PROCESSED`；没有 `payment_intent` 或找不到 → `UNMATCHED`（`last_error_code` 为 `PAYMENT_NOT_FOUND`，计入 A 的 P2）。支付与钱包都不变。
- **告警**：见下。处理按关联到的支付分三种（186 写进 `docs/payment-flow.md`）：
  - 支付是 `PAID`（自动入账或人工按实付入账）：Kelvin 核实后，需要扣回时用现有人工调账 `REFUND_ADJUSTMENT`（原因里写支付编号）；支付本身保持 `PAID`（spec §112.1：外部退款不直接改余额，扣回是一条新的账本行）。
  - 支付是「不入账结案」（`resolution = CLOSED`）且事件是 `charge.refunded`：这是结案后在 Stripe 后台退款的预期结果，钱从没入过账，不需要扣回；**这种事件不计入 P1**（见告警表）。
  - `GATEWAY_PAYMENT_REUSED` 的复核单：它的 PI 记在另一笔已 `PAID` 的支付上（A 只在 PI 未被占用时写入），这笔钱已经给那一笔入过账。所以这种复核单**结案时不在 Stripe 退款**（退款等于把已入账的钱退回，需要先扣回那一笔）；若误退，退款事件会关联到那笔 `PAID`，按第一种处理扣回。
  - 不入账结案的支付如果没记下 PI（例如 `GATEWAY_INCONSISTENT` 时 Stripe 没给 PI），结案后在 Stripe 退款产生的 `charge.refunded` 关联不上任何支付，事件为 `UNMATCHED`，**照样计入 P1**（系统无法确认它就是那笔结案退款）：管理员在 Stripe Dashboard 核对它确实是刚做的那笔退款即可，不改账，P1 在 24 小时后消失。
  - 争议落在不入账结案的支付上：钱从没入过账，不需要扣回；在 Stripe 里应诉或接受即可。
  - 争议或退款落在仍是 `REVIEW_REQUIRED` 的支付上：先按「人工复核出口」结案（钱已被退或被拒付时选「不入账结案」，且不再在 Stripe 退款；即使误点「按实付入账」，核对也会以 `GATEWAY_REFUNDED_OR_DISPUTED` 拒绝，事件是否关联得上都一样），不改账。
  - 争议或退款事件为 `UNMATCHED`（上面以外的情况）：在 Stripe Dashboard 按 PI 找到对应的付款与它的 `client_reference_id`，再按它对应的支付归入以上各种。

### 告警（继续并进 `wallet_negative_balance`）

| 判据 | 级别 | 与 A 的关系 |
| --- | --- | --- |
| `status = 'REVIEW_REQUIRED'` 的支付（不论进入多久） | P1 | **替换** A 的「`review_required_at` 在最近 24 小时内」：A 只报 24 小时是因为当时没有出口；现在有了出口，未结案就该一直报，结案即消失 |
| `event_type` 为 `charge.dispute.created` 或 `charge.refunded`、`processing_status` 不是 `IGNORED`（例如 `LIVEMODE_MISMATCH` 的不算）、`received_at` 在最近 24 小时内的事件；不含「`charge.refunded` 且关联的支付 `resolution = CLOSED`」 | P1 | 新增 |
| `status = 'PENDING'` 且 `expires_at <= now − 3600 秒` 的支付 | P2 | 新增（spec §120「Stale pending payment」）：Stripe 会话在 `expires_at` 必定结束，补对账在过期后的第一个小时每 5 分钟查一次，过了一小时还没收口，说明补对账没在工作或 Stripe 持续不可用。少数情况是正常的：会话已 `complete` 而异步付款仍在处理（A 的映射为 `OPEN`），这时 P2 一直报到付款有结果或 7 天超时，这是有意的可见性 |
| A 的其余判据 | 不变 | 不变 |

窗口写法同 A（`now − 24 小时 < 时刻 ≤ now`）。摘要段落逐字改为 `; payments: review open <a>, event conflicts 24h <b>, oldest open event <c>s, unmatched or rejected 24h <d>, stale pending <e>, disputes or refunds 24h <f>, config <ok|missing>`；只在 `<a>`、`<b>`、`<d>`、`<e>`、`<f>` 任一大于 0、`<c>` 超过 900 秒（严格大于）、或配置判据触发时追加，否则与原来逐字相同；`<c>` 超过 900 秒才追加是 A 的契约（AIH-TASK-175）已定的规则，本批不改。`<f>` 按上表的 P1 判据计数（已排除 `IGNORED` 与结案后的退款）。`docs/runbook.md` 与 `docs/api.md` 里这个维度的说明同步。

### 配置（`app/core/config.py`）

| 键 | 缺省 | 范围 |
| --- | --- | --- |
| `payment_reconcile_poll_seconds` | 60 | 30–600 |
| `payment_reconcile_grace_seconds` | 300 | 60–3600 |
| `payment_reconcile_max_seconds` | 604800 | 86400–2592000 |

候选查询里的 600 秒（孤儿）、7200 秒（建单 2 小时）、300 / 1800 / 21600 秒（间隔）、3600 秒（过期后 1 小时），以及手续费补抓的 8 天、600 秒与 3600 秒，是常量，写在服务模块里（改它们要过闸门）。

### 事务边界

- 补对账：每笔一个确认事务（A）；孤儿 / 超时过期各一个只锁支付行的事务；手续费一个条件更新事务。没有事务跨笔。
- 人工 CREDIT：事务外查 Stripe → 一个事务锁支付、钱包、租户，改状态、入账、审计、提交。
- 人工 CLOSE：一个事务。
- 争议与退款事件：A 的落事件结果事务（只写事件行）。

### 时间语义

沿用 A：不带时区的 UTC，精确到秒。`resolved_at`、`paid_at`（人工入账时）为管理员提交的时刻。

### 金额与精度

- 人工入账额 = `amount_received_minor / 100`，Decimal，两位小数；不经 `float`。
- 手续费补抓同 A。

## 3. 不变量影响矩阵

| 不变量 | 是否触碰 | 可能怎样被破坏 | 设计控制 | 验证方式 |
| --- | --- | --- | --- | --- |
| INV-1 中心故障不中断 AI | 否 | — | — | — |
| INV-2 事件不重复扣费 | 否 | — | — | — |
| INV-3 支付不重复入账 | **是** | 补对账与回调同时确认；人工入账与自动确认并发；结案后又被自动入账；两个管理员同时入账 | 复用 A 的三层唯一与锁支付行；`confirm_payment` 把已结案视为终态；CHECK `ck_payments_resolution_fields`；人工入账锁内重查前提，账本引用唯一 | §7「补对账与回调并发」「人工入账并发」「结案后终态」 |
| INV-4 余额只经账本变动 | 是 | 人工入账直接改钱包；争议直接扣余额 | 只调 `post_transaction`；争议只告警 | §7「人工入账走账本」「争议不改账」 |
| INV-5 历史账本不可变 | 是 | 扣回去改原 TOPUP | 扣回走人工调账新增一行 | 现有账本测试 |
| INV-6 事件保留版本引用 | 否 | 支付的税务版本在建单时已定（A） | — | — |
| INV-7 客户不可见成本毛利 | 是 | 手续费出现在客户接口 | 本批只加管理端接口；客户接口不变 | §7「管理端鉴权」 |
| INV-8 租户不可互访 | 是 | 人工入账给错租户 | 入账以 `payments.tenant_id` 为准，Stripe 的 `client_reference_id` 必须等于本行 | §7「人工入账核对」 |
| INV-9 对话内容不入库 | 否 | — | — | — |
| INV-10 金额用 Decimal | 是 | 浮点换算实收 | 整数分 → Decimal | §7「人工入账金额」 |
| INV-11 event_id 至多一次财务效果 | 是 | 争议事件重复处理 | 争议事件没有财务效果；A 的事件唯一 | §7「争议不改账」 |
| INV-12 定稿对账单不可变 | 否 | — | — | — |
| INV-13 状态与事件原子提交 | 是 | 支付置 `PAID` 而账本未写；结案写了一半 | 改状态、入账、审计同一事务 | §7「事务中途失败」 |
| INV-14 队列丢失不毁持久工作 | 是 | Celery 丢任务导致补对账不跑 | Beat 扫库，不靠入队；未收口的 `PENDING` 有 P2 告警 | §7「直接调用扫描函数」 |

## 4. 状态与并发

### 本批新增的支付状态跃迁

| 当前状态 | 触发 | 新状态 | 副作用 |
| --- | --- | --- | --- |
| `PENDING`（无网关 id） | 补对账：`now ≥ expires_at + 600 秒` | `EXPIRED`（`NO_GATEWAY_SESSION`） | 审计 |
| `PENDING`（有网关 id） | 补对账：`confirm_payment`（A 的决策表） | 照 A | 照 A |
| `PENDING` | 补对账：年龄满上限仍 `PENDING` | `EXPIRED`（`RECONCILE_TIMEOUT`） | 审计 |
| `REVIEW_REQUIRED` | 管理员 CREDIT 且核对通过 | `PAID`（`resolution = CREDITED`） | TOPUP（实收金额）、审计 |
| `REVIEW_REQUIRED` | 管理员 CLOSE | `FAILED`（`resolution = CLOSED`） | 审计 |
| 已结案（`resolution` 不为空） | 任何自动路径 | 不变 | — |

A 的跃迁不变，只是「已结案」被加进「不变」的一行。`EXPIRED`（含本批两个新原因）后收到付款成功照 A 入账。

### 并发

- **补对账只跑一份**：命名锁。
- **补对账 vs 事件处理**：都经 `confirm_payment`，A 的锁支付行与幂等保证。
- **过期 vs 事件认回会话**：过期事务锁支付行并要求 `gateway_payment_id` 仍为空；认回是 A 的条件更新 `gateway_payment_id IS NULL`。两者谁先都正确：先过期再认回 → `EXPIRED` 的行写入会话 id，随后确认可入账；先认回再过期 → 过期重查发现已有网关 id，什么也不做。
- **人工 CREDIT vs 自动确认**：CREDIT 锁支付行后重查 `REVIEW_REQUIRED`；自动确认见到 `REVIEW_REQUIRED` 或已结案什么也不做。
- **两个管理员同时处理**：后到者锁到行时 `resolution` 已不为空 → 409 `PAYMENT_NOT_IN_REVIEW`。
- **人工 CREDIT vs 关户**：锁内读租户，与 A 相同的互斥。
- **数据库负责的唯一性与终态**：A 的全部唯一约束；`ck_payments_resolution_fields`。
- **重试在哪一层**：补对账每轮自然重试；人工操作由管理员重试（409 / 503 不留下中间状态）。

## 5. 失败与恢复矩阵

| 失败点 | 对外结果 | 数据最终状态 | 是否可重试 | 恢复来源 | 告警 |
| --- | --- | --- | --- | --- | --- |
| 丢失回调 | — | 补对账确认后 `PAID` 入账 | 是 | 补对账 | 收口前 P2（过期后 1 小时） |
| 补对账时 Stripe 不可用 | — | 不变，本轮结束 | 是 | 下一轮 | 持续则 P2 |
| 补对账时 Stripe 拒绝查询 | — | 不变，按间隔再查，满 7 天 `EXPIRED`（`RECONCILE_TIMEOUT`） | 否 | 人工排查 | P2、ERROR 日志 |
| 补对账的确认事务撞唯一约束（`ConfirmConflict`） | — | 不变，跳过这一笔，本轮继续 | 是 | 下一轮 | — |
| 补对账单笔出现意外错误（其他异常） | — | 这一笔不变，本轮结束 | 是 | 下一轮 | ERROR 日志 |
| 没有网关 id 的 `PENDING` | — | `EXPIRED`（`NO_GATEWAY_SESSION`） | — | 迟到事件仍可认回并入账 | — |
| 7 天仍 `PENDING` | — | `EXPIRED`（`RECONCILE_TIMEOUT`） | — | 迟到事件仍可入账 | 之前 P2 |
| 人工 CREDIT 时 Stripe 不可用 | 503 | 不变（仍 `REVIEW_REQUIRED`） | 是 | 管理员重试 | 仍 P1 |
| 人工 CREDIT 核对不过 | 409 + 码 | 不变 | 否（按码处理，通常改为 CLOSE） | 管理员 | 仍 P1 |
| 人工 CREDIT 事务中途失败 | 500 / 409 | 支付与账本都不变 | 是 | 管理员重试 | 仍 P1 |
| 人工结案后 Stripe 又报付款成功 | — | 不变（终态） | — | — | — |
| 争议 / 退款事件 | 200 | 事件 `PROCESSED`，支付与钱包不变 | — | Kelvin 核实后人工调账 | P1（24 小时） |
| 争议事件找不到支付 | 200 | 事件 `UNMATCHED` | — | 人工排查 | P2（A） |
| 手续费一直拿不到 | — | `gateway_fee` 为空 | 8 天内按小时 | — | — |
| Redis/Celery 丢失 | — | 支付仍在库里 | 是 | Beat 扫库 | P2 |

## 6. 数据与安全边界

- **鉴权**：管理端三个接口都要求 ADMIN（`require_admin`，ADMIN 登录必过 2FA）；客户接口不变，客户看不到手续费、原因与人工结案信息。
- **租户过滤**：管理端跨租户可见（管理员职责）；人工入账的租户以 `payments.tenant_id` 为准，并核对 Stripe 的 `client_reference_id`。
- **日志**：允许 `public_id`、状态、原因码、Stripe 错误码、管理员的用户 id；**禁止** `resolution_reason`（管理员自由文本，可能碰巧含个人数据；只进库与审计，与人工调账同一规则）、请求体、密钥。
- **事件白名单**：扩展字段见「争议与退款」，不含个人数据。
- **密钥**：不新增；沿用 A。
- **审计**：`PAYMENT_REVIEW_RESOLVED`、`PAYMENT_FEE_RECORDED`；补对账的状态改变用 `PAYMENT_STATUS_CHANGE`。
- **数据保留**：同 A，长期保留。

## 7. 测试证据计划

| 风险/需求 | 测试层级 | 场景 | 预期结果 | 任务 |
| --- | --- | --- | --- | --- |
| 迁移与 CHECK | integration（MySQL） | 加列；`ck_payments_resolution_fields` 的各组合（四列半空、`CREDITED` 配 `FAILED`、`CLOSED` 配 `PAID`、缺 `review_required_at`）；降级 | 非法组合被拒；合法组合通过；有结案行时降级报错 | 179 |
| 结案后终态 | integration | 已 CLOSE 的支付收到 Stripe `PAID` 的事件（经 A 的事件处理调 `confirm_payment`）；补对账第 1 段不选出已结案的行；CREDITED 的行手续费为空时被第 2 段补手续费 | 前者状态不变、无账本行、`last_reconciled_at` 更新；第 1 段不碰结案行；CREDITED 行只写 `gateway_fee` 与 `last_reconciled_at`，状态与结案字段不变 | 180、187 |
| 丢失回调补回 | integration | 有网关 id 的 `PENDING`，假 Stripe 报已付款，不发回调，跑补对账 | `PAID`、一条 TOPUP；再投递回调不重复入账 | 180 |
| 补对账节奏 | integration | 候选查询的每个分支与边界（创建后 299 / 300 秒；未过期 2 小时边界；过期时刻前后；过期后 3600 秒边界；`last_reconciled_at` 各间隔边界；满上限）；未到期的老单不挡住到期的新单；排序为最久没查过的先查 | 选出的行与条件一致 | 180 |
| 孤儿过期 | integration | 无网关 id，`expires_at + 599` / `+ 600` 秒；过期后收到带会话 id 的付款成功事件 | 前者不变、后者 `EXPIRED`（`NO_GATEWAY_SESSION`）；事件认回后 `PAID` 入账 | 180 |
| 超时过期 | integration | 满 7 天、Stripe 报 `OPEN`；满 7 天、Stripe 不可用；满 7 天、Stripe 拒绝查询；满 7 天、`ConfirmConflict` | 第一、三组 `EXPIRED`（`RECONCILE_TIMEOUT`）；第二、四组不变 | 180 |
| Stripe 不可用时本轮结束 | integration | 三笔到期，第一笔查询不可用 | 后两笔本轮没有被查询 | 180 |
| 单笔意外错误 | integration | 三笔到期，第一笔的过期事务抛数据库错误 | 这一笔不变、ERROR 日志、后两笔本轮没有被处理 | 180 |
| 预算与查询上限 | integration | 到期的支付多于 50 笔；注入的单调时钟在第二笔之后超过 45 秒 | 本轮查询次数不超过 50；超过预算后不再开始新的一笔 | 180 |
| 未满上限时 Stripe 拒绝查询 | integration | 有网关 id、创建 1 天、Stripe 拒绝查询 | 状态不变、`last_reconciled_at` 更新、继续下一笔 | 180 |
| 补对账与回调并发 | integration（MySQL） | 补对账与事件处理同时确认同一笔 | 只有一条 TOPUP | 180 |
| 只跑一份 | integration（MySQL） | 另一个连接持有命名锁 | 本轮直接返回 | 180 |
| 手续费补抓 | integration | `PAID`、费用为空：拿到 MYR 费用；没有费用；非 MYR；会话 id 对不上；超过 8 天；并发已写；拿不到费用的老单不挡住新单；Stripe 不可用；Stripe 拒绝查询 | 只有第一组写入并记审计；Stripe 不可用时本轮结束；其余只更新 `last_reconciled_at` 或不查 | 187 |
| 直接调用扫描函数 | integration | 不经 Celery 调用 `run_due` | 到期的支付被处理 | 180 |
| 争议与退款事件 | integration（任务） | 两种事件落库为 `RECEIVED`；能关联到支付；找不到 PI；白名单含公共字段、不含个人数据 | `PROCESSED` 并写 `payment_id` / `UNMATCHED`；支付与钱包不变 | 181 |
| 告警判据 | unit | 新表每一行与边界（`expires_at` 前后 3600 秒；24 小时窗口；未结案复核不论多久；`IGNORED` 的争议不计；结案后的退款不计、结案后的争议照计）；没有支付数据时 | 级别正确；摘要逐字；没有数据时与原来逐字相同 | 181 |
| CLOSE | integration | `REVIEW_REQUIRED` 结案；重复结案；非复核状态结案 | `FAILED`、结案字段、审计；后两者 409 `PAYMENT_NOT_IN_REVIEW` | 182 |
| 人工入账核对 | integration | §2 CREDIT 第 2 步（Stripe 不可用 503、拒绝查询 409）、第 3 步每一行（含部分退款、全部退款、有争议、charge 信息缺失）、第 4 步每一行；`gateway` 为 `None` 时 CREDIT 503、CLOSE 照常；第 6 步：`post_transaction` 返回重放或抛 `LedgerConflict` 409、余额越界 422 `BALANCE_OUT_OF_RANGE` | 对应的错误码，支付、账本与审计都不变 | 182 |
| 适配器新字段 | unit（假传输） | `latest_charge` 有退款、有争议、都没有、`latest_charge` 缺失 | 两个字段与表一致；A 的状态映射结果不变 | 182 |
| 人工入账金额 | integration | 应收 100.00、实收 99.99（`AMOUNT_MISMATCH`） | TOPUP 99.99；`confirmed_amount` 99.99；`credited_amount` 99.99 | 182 |
| 人工入账走账本与复机 | integration | 余额为负的暂停租户人工入账 | 一条 TOPUP、计费状态 `ACTIVE`、复机 outbox 与审计 | 182 |
| 人工入账并发 | integration（MySQL） | 两个管理员同时 CREDIT；CREDIT 与自动确认同时 | 只有一条 TOPUP；后到者 409 | 182 |
| 人工入账 vs 关户 | integration（MySQL） | CREDIT 持锁时另一连接关户；关户先提交 | 关户等入账提交；或 409 `TENANT_CLOSED` | 182 |
| 事务中途失败 | integration | 人工入账时 `post_transaction` 抛错 | 支付仍 `REVIEW_REQUIRED`、无账本行、无审计 | 182 |
| 原因不进日志 | unit | 人工操作的日志 | 不含原因文本 | 182 |
| 管理端鉴权 | integration（API） | 未登录、客户令牌调用三个接口 | 401 / 403 `ADMIN_REQUIRED` | 183 |
| 列表与筛选 | integration（API） | 状态、客户、日期区间（马来西亚时间日界前后各一秒）、分页、`created_from` 晚于 `created_to` | 结果与排序正确；422 | 183 |
| 详情 | integration（API） | 带事件（含同一时刻的两条）、人工入账后的 `credited_amount`、不存在 | 字段与顺序正确；404 | 183 |
| 结案接口 | integration（API） | 两种动作、多余字段、原因为空或超长、开关关闭时 CREDIT 与 CLOSE | 200 / 422 / CREDIT 503、CLOSE 200 | 183 |
| 前端列表 | unit（前端） | 列、各筛选的查询串、日期起晚于止不发请求、翻页、三种状态 | 与 §2 一致 | 184 |
| 前端详情与复核面板 | unit（前端） | 字段与事件表、复核面板只在 `REVIEW_REQUIRED` 显示、原因为空时按钮不可用、二次确认（含 `confirmed_amount` 为空与非 MYR）、两种动作、409 与 503 的错误展示 | 与 §2 一致 | 185 |
| 浏览器验收 | acceptance（只读） | 打开支付列表 | 页面与表格或空状态出现 | 184 |

## 8. 迁移与上线

- **迁移 0037**（`down_revision` 为 A 的 0036）：`payments` 加四列、两个 CHECK（`ck_payments_resolution`、`ck_payments_resolution_fields`）、一个外键、一个索引；不回填（已有行四列都为空，满足 CHECK）。upgrade：任一新列已存在 → 抛错、不发 DDL，消息点名那一列；否则用**一条** `ALTER TABLE payments` 同时加四列、两个 CHECK、外键与索引（MySQL 8 的 InnoDB DDL 是原子的：这条语句失败时表结构不变，可以直接重跑，不会留下只加了一半的列）。离线 `--sql` 输出 DDL。downgrade：离线模式抛错；有 `resolution` 不为空的行 → 抛错、不删；否则按「两个 CHECK → 外键 → 索引 → 四列」的顺序删（MySQL 不允许先删外键依赖的索引，错误 1553，与 0002、0005、0018 的写法相同）。
- **部署顺序**：A 的 172..178 全部合并之后。合并即部署；补对账在 `payments_enabled` 为假时空跑。
- **对客户开放充值之前**（不在本批，Kelvin 执行，186 写进 `docs/payment-flow.md`）：
  1. 在 Stripe Dashboard 的回调端点加订阅 `charge.dispute.created`、`charge.refunded`（共六种）。
  2. **验证丢失回调能被补回**（spec §127 验收的「Lost Webhook is recovered by status reconciliation」）：在 Stripe Dashboard 把回调端点临时停用（停用期间的事件 Stripe 不会补发），用生产验收租户（放进建单允许名单）做一笔 RM10 的真实充值，确认在补对账的节奏内（建单后 5 分钟起，每 5 分钟查一次）支付变成 `PAID`、钱包只加一次，然后重新启用端点，并把验收租户**从建单允许名单里移除**（与 A §8 第 5 步相同）。这笔 RM10 留在验收租户的钱包里。由 Kelvin 执行。
  3. 充值界面（C）上线后再打开 `payment_topups_open`。
- **回滚**：关 `payments_enabled` 即停补对账；已结案的数据保留。

## 9. 方案取舍

| 方案 | 优点 | 风险 | 未采用原因 |
| --- | --- | --- | --- |
| 采用：结案用 `resolution` 列 + 沿用六种状态 | 不改 spec §63 的状态集合；CHECK 让结案成为终态 | `FAILED` 有两种来源，要看 `resolution` 区分 | — |
| 备选 A：新增状态 `RESOLVED` | 一眼可辨 | 改 spec 的状态集合，A 的 CHECK 与客户侧映射都要改 | 改动面大 |
| 备选 B：入账走现有人工调账 | 不新写入账路径 | 支付与调账之间没有数据库约束，可能既调账又被自动入账 | Kelvin 选两个出口 |
| 备选 C：争议自动扣回 | 省人工 | spec §112.1 禁止外部退款直接改余额；争议可能撤销 | 违反 spec |
| 备选 D：用 Stripe 的列表接口批量补对账 | 请求少 | 要按时间翻页、带回更多数据（含个人数据） | 逐笔查已够 V1 的量 |
| 备选 E：人工入账按 `expected_amount` | 与自动入账一致 | 客户实付不同时账不平 | 违反 Kelvin「按实付入账」 |

## 10. 未决问题与假设

- **未决问题**：无。
- **假设**：
  1. Stripe Checkout Session 的过期时刻就是建单时传的 `expires_at`，过期后不能再付款；只有已 `complete` 的会话可能在之后变成已付（异步付款方式）。
  2. 在 Stripe 后台手工退款会产生 `charge.refunded`；拒付会产生 `charge.dispute.created`。两者对象里都有 `payment_intent`。
  3. 生产的支付量在 V1 很小（每天几十笔以内），逐笔补对账的查询量远低于 Stripe 的速率限制。
- **如果假设错误**：1 → 7 天上限前补对账仍会查到；2 → 告警缺失，但不影响入账正确性（扣回本来就是人工）；3 → 每轮 50 次的上限保护 Stripe，积压由 P2 告警暴露。

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
| `AIH-TASK-179` | 迁移 0037、模型加列、`AuditAction` 两个新动作（含前端审计动作清单）、`docs/database-schema.md` |
| `AIH-TASK-180` | `confirm_payment` 的终态规则；补对账服务与任务（第 1 段、命名锁、Beat 登记）、孤儿与超时过期、三个配置项，与测试 |
| `AIH-TASK-187` | 补对账第 2 段：手续费补抓，与测试（依赖 180，排在 181 之前；编号放在最后是为了不改其他任务的编号） |
| `AIH-TASK-181` | 争议与退款：回调认两种类型、白名单字段、事件处理分支；告警判据与摘要；`docs/runbook.md`、`docs/api.md` 里该维度的说明，与测试 |
| `AIH-TASK-182` | 适配器 `GatewayPaymentStatus` 的两个新字段；人工复核出口服务 `resolve_review`（CREDIT / CLOSE），与测试 |
| `AIH-TASK-183` | 管理端三个接口、schema、注册路由、`docs/api.md` 一节，与 API 测试 |
| `AIH-TASK-184` | 管理端前端接口封装、「支付」列表页、导航，与前端测试；浏览器验收步骤 |
| `AIH-TASK-185` | 管理端支付详情页与复核面板，与前端测试 |
| `AIH-TASK-186` | `docs/payment-flow.md`（补对账、复核处理、争议与退款的处理步骤、开放充值前的检查）、`docs/deployment.md` 配置项、`docs/TODO.md` 勾选「主动的 stale payment 对账定时任务」与「支付金额不符的人工复核流」两项 |

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
| v1 | 2026-11-06 | 初稿；送审前按登记前预审三轮修订（候选查询的到期条件写进 SQL 与以过期时刻为准的节奏、争议与退款的各种情形、各错误码的处理表、迁移用一条原子 ALTER、单笔意外错误结束本轮、拆出详情页任务） | — |
| v2 | 2026-11-06 | §5 把「确认冲突」与「单笔意外错误」拆成两行（前者跳过这一笔、本轮继续；后者本轮结束），与 §2 一致；`resolve_review` 只返回 `public_id`，结案接口提交后用详情查询重新读出 | 登记前预审（批准后）报出 §5 与 §2 矛盾 1 条 |
| v3 | 2026-11-06 | 人工 CREDIT 核对 Stripe 的退款与争议：`GatewayPaymentStatus` 加 `charge_amount_refunded_minor`、`charge_disputed`（取自已展开的 `latest_charge`），已退款或有争议时 409 `GATEWAY_REFUNDED_OR_DISPUTED`、charge 信息缺失时 409 `GATEWAY_NOT_PAID`；错误码处理表与争议情形同步；§7 补审查建议的测试（CREDIT 第 2、6 步、适配器新字段、单笔意外错误、预算与查询上限、未满上限时拒绝查询、手续费补抓遇到 Stripe 不可用或拒绝）；登记前预审判 180 偏大，把手续费补抓拆成 `AIH-TASK-187` | v2 审查阻断项 1 条（已退款或已拒付的钱会被人工入账） |

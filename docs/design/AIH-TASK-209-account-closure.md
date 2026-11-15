# AIH-TASK-209..215 设计：关户（租户账户 → `CLOSED`）（已批准 v3）

> **来源**：设计闸门 Issue #707。本文件是 `APPROVED: design v3` 那一版正文的**逐字副本**（送审时状态行为 `READY_FOR_REVIEW`）。审查：Codex 额度不可用期间由 Claude Code 审查。v1、v2 各获批准后，登记前预审报出的问题改进了 v2、v3（见 §12），v3 重新审查通过、没有阻断项。
> 放进仓库，是因为 OpenClaw Worker 在沙箱里不联网、读不到 GitHub Issue。
> **实现以本文件为准**；与 Issue 不一致时，以 Issue #707 上被批准的 v3 为准。设计要改，就回到 Issue 升版本、重新过闸门，不要直接改本文件。
> 产品决定（「未结」的范围、关户后另外拒绝的写入口、退款扣回引用支付号、关户确认方式、关户当月一律出最后一份对账单）由 Kelvin 2026-11-14 拍板，见 §1。

状态：`READY_FOR_REVIEW`
负责人：Claude
**设计版本**：`v3`
对应需求：spec §112.1、§8、§24、§65、§66、§83、§133（Invariant 2、3、4、5、8、11、12、13、14）；`docs/TODO.md` Phase 4「关户（`→ CLOSED`）」一条；AIH-TASK-020 设计（`docs/design/AIH-TASK-020-tenant-account-status.md` §2 末尾「对以后功能的契约」，设计闸门 #136 v2）；AIH-TASK-154 设计 §1「明确不做什么」末条（关户后的出单规则留给本设计，设计闸门 #565）；AIH-TASK-032 设计 §2「锁顺序」（关户前置条件挡住计费的旧值窗口，设计闸门 #181）
目标 PR：待开（拆成 `AIH-TASK-209`..`215`，见 §11）

## 0. 准入判定

| 任务涉及 | 要填的范围 |
| --- | --- |
| 钱包 / 账本 / 定价 / 汇率 / 支付 / 幂等 / 状态机 | **全部章节** |
| 用量摄取 / 认证与会话（集成侧与平台侧）/ Webhook | §1–§7（**§6 不可省** —— 密钥存储与轮换、鉴权、租户过滤、日志脱敏都在那一节） |
| 前端 / 文档 / CI / 脚本 | **不走本闸门**，直接开 PR |
| 既不碰钱、也不碰用量摄取 / 认证与会话 / Webhook 的后端改动 | **不走本闸门**，直接开 PR |

本任务档位：`全部`。理由：给账户状态机加上终态跃迁 `DISABLED → CLOSED`；在记账总入口 `post_transaction` 的租户行锁内拒绝给 `CLOSED` 租户入账（钱包 / 账本）；关户的前置条件读余额与用量、支付、网关事件的终结状态；改对账单的出单资格（INV-12 相关）。

---

## 1. 目标与边界

### 要解决的问题

spec §112.1 规定关户是一个有状态的流程（先停用 → 吊销凭据并等在途用量与支付到终态 → 余额人工清零 → 收据与对账单保留快照 → 余额为 0 且无未结财务事件才置 `CLOSED`；关户后发现的迟到事件进人工复核，不得静默重开）。今天：

1. 没有任何路径产生 `CLOSED`：账户状态接口的请求体只收 `ENABLED` / `DISABLED`（`app/schemas/customers.py:141-152`），跃迁表里 `CLOSED` 一格都没有、也没有格通向它（`app/services/account_status.py:51-56`）。
2. 记账总入口 `post_transaction`（`app/repositories/wallet.py:412-509`）锁了租户行却不看账户状态；管理员调账（`app/services/wallet_adjustments.py:130-183`）、建项目（`app/services/customers.py:264-304`）、建 / 轮换集成凭据（`app/services/integration_access.py:155`、`:209`）、AI 客户端密钥签发（`app/ai_api/admin.py:136`）、webhook 签名密钥签发与启用（`app/services/webhook_signing.py:189`、`:244`）、内部计量开通（`app/services/internal_billing.py:43`）、低余额阈值设置（`app/services/customers.py:201`）都不看 `CLOSED`。AIH-TASK-020 写下的契约（关户后拒绝新调账、建项目、建凭据、轮换凭据）还没有实现。
3. 月度对账单不按账户状态筛租户（`app/services/statements.py:859-868` 的 `is_eligible`），关户后的出单规则留空（AIH-TASK-154 设计 §1）。

### Kelvin 2026-11-14 的决定

| 问题 | 决定 |
| --- | --- |
| 关户后的对账单 | 关户当月**一律**出最后一份对账单（没有发生额时是期初 = 期末 = 0 的一份），之后不再生成；落到对账单资格判断里，见 §2「对账单资格」。（Kelvin 先定「若有活动才出」，看过初稿后同日改为一律出，理由见 §9 备选 H） |
| 挡关户的「未结」 | ① 用量事件状态不在 `{PROCESSED, FAILED_FINAL}`；② AI 模块本地用量 outbox（`ai_usage_outbox`）里该租户 `PENDING` / `SENDING` / `HELD` 的行；③ 支付 `PENDING`、`REVIEW_REQUIRED`；④ 该租户支付的网关事件处理状态 `RECEIVED` / `PROCESSING` / `RETRY`。`EXPIRED` 支付创建超过 7 天视为终态、不挡；之后迟到到账按 `CLOSED` 规则进 `REVIEW_REQUIRED`（已有：`app/services/payments.py:344`） |
| 关户后另外拒绝的写操作 | AI 客户端密钥签发、webhook 签名密钥签发与启用、内部计量开通、低余额阈值设置。吊销类照常允许 |
| 退款扣回「引用原支付」 | V1 不加字段，在调账原因里写支付号（runbook 写明操作步骤） |
| 关户的确认 | 关户必须填原因（进审计）；管理端前端要求输入客户名才能确认 |

已有、本设计沿用的决定（Kelvin 2026-09-28，AIH-TASK-020 设计 §2 末尾）：只能从 `DISABLED` 关；钱包余额恰好为 0；在 `post_transaction` 的租户行锁内拒绝给 `CLOSED` 租户入账；`CLOSED` 为终态；关户后拒绝新调账、建项目、建凭据、轮换凭据，吊销凭据、查看、编辑客户仍允许。

我定的（未单独问）：

- 关户用**独立接口** `POST /api/v1/admin/customers/{customer_id}/close`，账户状态接口不变（`CLOSED` 作为目标仍是 422）；另加只读的 `GET …/closure-check` 给管理端弹窗显示还差什么。理由见 §9。
- 前置条件不含「集成凭据已全部吊销」：凭据是否吊销不影响钱（关户后到达的用量一律 `FAILED_FINAL`），而 AI 模块 outbox 的投递要用项目的集成凭据签名（`app/ai_api/delivery.py:350-360`），先吊销会让未投递的行永远停在 `PENDING`、反过来挡住关户。顺序写进 runbook（§2「runbook」）。
- 关户时刻取租户的 `status_changed_at`，不加列、不加迁移（理由与风险见 §2「关户时刻」、§9、§10）。
- 客户门户不改：`CLOSED` 已是只读（`app/services/account_status.py:85`），门户首页已有 `CLOSED` 的显示文案（`frontend/src/features/portal/PortalHomePage.tsx:47`）。

### 可观察的完成标准

1. `DISABLED`、余额恰好为 0、四类未结都为 0 的客户，管理员填原因并在前端输入客户名后关户成功：账户状态 `CLOSED`、`status_version` +1、同一事务写 `TENANT_ACCOUNT_STATUS_CHANGED` 审计（带原因）与 `tenant.account_status_changed` outbox 行，项目的状态投递被重排、集成方随后看到 `BLOCK_AI` / `ACCOUNT_CLOSED`。
2. 不满足任一前置条件时关户返回 409，错误码指出第一条不满足的条件，什么都不写；`GET …/closure-check` 一次列出全部未满足的条件与各类计数。
3. 对 `CLOSED` 租户的任何新入账在 `post_transaction` 的租户行锁内被拒绝，不写任何行：管理员调账 409 `ACCOUNT_CLOSED`；用量计费把事件置 `FAILED_FINAL` / `ACCOUNT_CLOSED`；支付自动确认与人工按实付入账沿用已有的 `TENANT_CLOSED`（锁内先判，走不到 `post_transaction` 的拒绝）。
4. 对 `CLOSED` 租户，建项目、建凭据、轮换凭据、签发 AI 客户端密钥、签发与启用 webhook 签名密钥、开通内部计量、设置或清除低余额阈值都返回 409 `ACCOUNT_CLOSED`、不写库；各类吊销、查看、编辑客户资料照常。
5. 关户当月（马来西亚日历月）的对账单照常生成，之后的账期不再生成，「缺失对账单」告警也不再把它们算作缺失。
6. 关户后不存在任何把账户改回非 `CLOSED` 的路径（两个状态接口对 `CLOSED` 都只有「200 不写」或 409）。

### 明确不做什么

- **不做自动网关退款**（spec §112.1「Automated gateway refunds remain out of scope」）：正余额由 Kelvin 在 Stripe 后台人工退款后，用现有调账 `REFUND_ADJUSTMENT` 扣回，原因里写支付号。
- **不加「引用原支付」的结构化字段**（Kelvin 决定 4）；不校验「退款额 ≤ 该支付可退额」——由 runbook 要求人工核实（§10 已知限制 3）。
- **不加迁移、不加列**：`account_status` 的列与 CHECK 已允许 `CLOSED`（迁移 0009，`docs/database-schema.md:41`）；原因只进审计。
- **不做重开**：`CLOSED` 是终态，没有任何接口离开它。
- **不改客户门户**、不改用量摄取（关户后用量照常落库为 `RECEIVED`，由计费置 `FAILED_FINAL`）、不改 AI 调用的有效状态公式（`compose_effective_status` 已把 `CLOSED` 排第一，`app/models/tenancy.py:130`）。
- **不要求凭据先吊销**、不自动吊销凭据或 AI 客户端密钥（runbook 里人工做）。
- **不做「关户后迟到事件」的新复核界面**：用量的 `FAILED_FINAL` / `ACCOUNT_CLOSED` 走现有的用量事件管理页与 runbook（`docs/runbook.md:981` 已写「人工复核；不要重新入队」）；支付的 `REVIEW_REQUIRED` / `TENANT_CLOSED` 走 B 的复核出口（只能「不入账结案」）。
- **不改管理端各面板的按钮显隐**：`CLOSED` 客户的建项目等按钮照旧显示，点了得到 409 `ACCOUNT_CLOSED` 并由现有错误提示显示（§10 假设 4）。
- **不做 PDPA 个人数据删除**（spec §112.1 末段，另有流程）。

### 现有行为与证据

- **账户状态跃迁**：`change_account_status`（`app/services/account_status.py:130-160`）在 `session_scope` 里 `get_tenant_by_public_id(for_update=True)` 只锁租户行，`_apply`（`:163-221`）做 `transition` 判定 → 改状态 → `bump_status_version`（`app/repositories/tenancy.py:117-154`：版本 +1、`status_changed_at = now`、加 outbox 行、重排该租户全部项目的投递行）→ `updated_at` → 审计 `TENANT_ACCOUNT_STATUS_CHANGED`（白名单：`account_status`、`status_version`，`reason` 进审计的 `reason` 列）。outbox 事件 `tenant.account_status_changed` 没有处理器、以 `PENDING` 持久保存（`app/tasks/outbox.py:348`）。
- **`bump_status_version` 的调用方恰好四处**（`docs/database-schema.md:64`）：账户状态（`account_status.py:204`）、余额驱动的计费状态（`wallet.py:842`，只在 `post_transaction` 内）、内部计量开通（`internal_billing.py:118`）、项目集成状态（`project_integration.py:350`，已对 `CLOSED` 409，`:293`）。
- **记账**：`post_transaction` 校验 → `_lock_wallet`（`wallet.py:766`，`FOR UPDATE` + `populate_existing`）→ 来源查重（重放返回既有行、不锁租户，`:455-464`）→ `_lock_tenant`（`:778`）→ 余额范围 → 写账本行与审计、计费状态跃迁、低余额事件 → flush。锁顺序钱包 → 租户（`wallet.py` 文件头）。四个调用方：`payments.py:266`、`payment_review.py:408`、`usage_billing.py:409`、`wallet_adjustments.py:147`。
- **支付确认**（`payments.py:225-292`）锁「支付 → 钱包 → 租户」后判定，`_paid_check_failure` 在锁内对 `CLOSED` 返回 `TENANT_CLOSED`（`:344`）→ `REVIEW_REQUIRED`，不调 `post_transaction`。人工按实付入账（`payment_review.py:366-436`）同样顺序锁三行，`_locked_check_failure` 对 `CLOSED` 返回 `TENANT_CLOSED`（`:462`）。
- **建单**：`_locked_pass`（`payments.py:834-864`）先 `lock_tenant_for_customer_write`（`account_status.py:88-110`，`DISABLED` / `CLOSED` → 403 `ACCOUNT_READ_ONLY`）再插入 `PENDING`：账户 `DISABLED` 之后不会再有新支付行。
- **用量计费**（`usage_billing.py:344-438`）：① 锁事件行 → ② **普通读**租户，`CLOSED` → `FAILED_FINAL` / `ACCOUNT_CLOSED`（`:353-358`）→ ③④ 共享锁 → ⑧ 仅当 `PREPAID` 且 `billable_cost > 0` 时 `post_transaction`（`:404-432`，只捕获 `LedgerConflict`）→ ⑨ `PROCESSED`。全局锁顺序「事件行 → 供应商 → FX → 规则 → 钱包 → 租户」（AIH-TASK-032 设计 §2）。
- **重新入队**（`app/services/usage_requeue.py:45-50`）：`MODEL_UNKNOWN` / `PRICING_ERROR` / `FX_RATE_ERROR` / `FAILED_FINAL` 且账本里没有该 `event_id` 的行 → 改回 `RECEIVED`。不看账户状态。
- **用量事件状态**九种（`app/models/usage.py:124-132`）；索引 `ix_usage_events_status_occurred (status, occurred_at)`、`ix_usage_events_tenant_id`（`:378-379`）。
- **AI 模块 outbox**（`app/ai_api/outbox.py:51-57`）六种状态 `PENDING` / `SENDING` / `DELIVERED` / `DEAD` / `HELD` / `DISCARDED`，表上有 `tenant_id`（`:81`）与 `ix_ai_usage_outbox_status_next_attempt_at`（`:111`）。投递用项目当前可验证的集成凭据签名，找不到凭据按「可重试」`SIGNING_CREDENTIAL_MISSING` 回到 `PENDING`（`app/ai_api/delivery.py:350-360`、`:135-141`），不会自己变成 `DEAD`。
- **支付状态**（`app/models/payments.py:81-91`）：`PENDING` / `PAID` / `FAILED` / `EXPIRED` / `CANCELLED` / `REVIEW_REQUIRED`；`EXPIRED` 与 `FAILED` 不是终结，迟到的 `PAID` 照样走确认（`payments.py:116` 的 `FINAL_STATUSES` 只有 `PAID`、`REVIEW_REQUIRED`）。索引 `ix_payments_tenant_created (tenant_id, created_at)`（`:226`）。
- **网关事件**（`app/models/payments.py:107-116`、`:265-319`）：回调落库时 `payment_id` 为空（`app/services/payment_events.py:102`），处理结果落库时才写（`app/tasks/payment_events.py` 文件头第 2 步）；归属靠 `metadata_json` 的 `client_reference_id`（= 支付 `public_id`）、`object_id`（会话 id）或争议 / 退款事件的 `payment_intent`（`app/tasks/payment_events.py:258-267`、`:299-313`）。`RETRY` 不放弃。索引 `ix_payment_gateway_events_due (processing_status, next_attempt_at)`（`:315`）。
- **对账单资格**：`is_eligible`（`statements.py:859-868`）= `PREPAID` 且有钱包且钱包早于账期结束；生成（`:917-1003`）与「缺失对账单」告警（`app/services/alerts.py:424-466`）共用它。
- **已有 `ACCOUNT_CLOSED` 错误类**：`AccountClosed`（`app/services/project_integration.py:104-108`，409），`portal_accounts.py:65` 已从那里导入复用。
- **锁租户的既有写法**：项目集成状态「先租户（`FOR UPDATE`）后项目」（`project_integration.py:290-294`），状态 webhook 地址「租户共享锁 → 项目行锁」（`:411-415`，`_lock_tenant_shared` 在 `:215-226`）；内部计量开通「钱包 → 租户」（`internal_billing.py:58-75`）。

## 2. 设计概要

### 数据流

```text
管理员 GET  …/customers/{id}/closure-check
  └─ 只读会话：读租户与钱包 → closure_blockers(session, tenant_id, now)（普通读）→ 视图

管理员 POST …/customers/{id}/close  {reason}
  └─ 事务 0（只读，短）：public_id → tenant_id；没有 → 404
  └─ 事务 1：
       锁钱包（X）→ 锁租户（X）                     ← 与 post_transaction 同序
       已是 CLOSED → 什么都不写，200
       不是 DISABLED → 409 ACCOUNT_STATUS_TRANSITION_INVALID
       余额 ≠ 0 → 409 ACCOUNT_CLOSURE_BALANCE_NOT_ZERO
       closure_blockers（锁后第一次普通读，快照在此建立）→ 第一个非零类别 → 409
       写：account_status = CLOSED → bump_status_version（版本、status_changed_at、
           tenant.account_status_changed outbox、重排投递）→ updated_at → 审计（带 reason）
     提交 → 事务外读客户详情返回

关户之后（防线）：
  新入账 → post_transaction 锁租户后见 CLOSED → TenantClosed（不写任何行）
     ├─ 用量计费 ⑧ 捕获 → 事件 FAILED_FINAL / ACCOUNT_CLOSED
     ├─ 调账 → 409 ACCOUNT_CLOSED
     └─ 支付确认 / 人工入账：锁内已先判 → REVIEW_REQUIRED / 拒绝 TENANT_CLOSED（不到 post_transaction）
  其他管理写入口 → 锁租户（S 或 X）后见 CLOSED → 409 ACCOUNT_CLOSED
  对账单 → is_eligible：账期开始晚于关户时刻 → 不合格
```

### 契约一：`post_transaction` 拒绝 `CLOSED`（AIH-TASK-209，`app/repositories/wallet.py`）

- 新异常 `class TenantClosed(LedgerError)`，消息是问题码 `TENANT_CLOSED`（与 `LedgerError` 族一致：消息只含问题码）。
- 位置：`_lock_tenant` 之后、`BALANCE_OUT_OF_RANGE` 判定之前：`if tenant.account_status is AccountStatus.CLOSED: raise TenantClosed("TENANT_CLOSED")`。此时没有写过任何行（账本行、审计、跃迁都在其后），调用方的事务里只有它自己的改动。
- 检查顺序因此为：参数校验 → 锁钱包 → 钱包缺失 → 来源查重（**重放与冲突在租户锁之前，照旧**）→ 锁租户 → `CLOSED` → 余额范围 → 写。
  - 同一来源已记过且一致：仍返回既有行（`replayed=True`），不论租户是否已关户——重放没有新的财务效果（INV-11），关户前提交的调账用同一幂等键重发得到同一个 200。
  - 同一来源、不同效果：仍是 `LedgerConflict`，先于 `CLOSED`。
- 不读也不改别的调用约定；docstring 的「所有校验错误都在任何写入之前抛出」仍成立。

**各调用方的处理**：

| 调用方 | 会不会遇到 `TenantClosed` | 处理 | 最终状态 |
| --- | --- | --- | --- |
| `usage_billing._process` ⑧（`usage_billing.py:409`） | 会：② 是普通读，可能读到关户提交前的 `DISABLED`；⑧ 的租户锁在关户事务提交后才拿到、读到 `CLOSED` | 与 `LedgerConflict` 并列捕获：`_settle(event, FAILED_FINAL, ACCOUNT_CLOSED, resolution)`，返回 `Outcome.FAILED_FINAL`；记一条 INFO 日志（固定文本 + `_context(event)`，不带金额） | 事件 `FAILED_FINAL` / `ACCOUNT_CLOSED`，没有账本行；与 ② 判出的结果相同。**不是** 500、不进 `record_failure` 的重试 |
| `wallet_adjustments._post_once`（`:147`） | 会 | `except TenantClosed: raise AccountClosed from None`（复用 `project_integration.AccountClosed`，409 `ACCOUNT_CLOSED`） | 什么都没写（事务回滚） |
| `payments._apply`（`:266`） | 不会：同一事务里已按「支付 → 钱包 → 租户」锁住三行，`_paid_check_failure` 在锁内先对 `CLOSED` 返回 `TENANT_CLOSED`（`:344`），走 `REVIEW_REQUIRED`，不调 `post_transaction` | 不捕获（走到即程序缺陷，异常原样传出，事务回滚，事件按 A 的现有规则重领后再判一次，结果是 `REVIEW_REQUIRED`） | 现有行为不变 |
| `payment_review._credit_locked`（`:408`） | 不会：同理，`_locked_check_failure` 锁内先判（`:462`） | 不捕获 | 现有行为不变（`TENANT_CLOSED` 拒绝） |

以后新增的 `post_transaction` 调用方（例如 Phase 8 rebill）自动得到这条兜底。

### 契约二：其他管理写入口拒绝 `CLOSED`（AIH-TASK-210）

一律用已有的 `AccountClosed`（409 `ACCOUNT_CLOSED`），**在锁住租户行之后判**，这样与关户事务（持有租户 X 锁）串行：写入先拿到锁就在关户之前完成；关户先提交，这里锁到后读到 `CLOSED` 而拒绝。所有入口都是「先租户、后其他行」，与现有的租户 → 项目 / 投递行 / 凭据顺序一致，不新增锁环（§4「并发」）。

| 入口 | 文件 | 改法 | `CLOSED` 判定位置 |
| --- | --- | --- | --- |
| 建项目 | `app/services/customers.py:264` | `_require_tenant(session, customer_id)` 改为 `for_update=True` | 锁租户后、`tenancy.create_project` 之前 |
| 设置 / 清除低余额阈值 | `customers.py:201` | 已 `for_update=True`（`:222`） | 锁租户后、读 `before` 之前；**与现值相同也 409**（同 `project_integration` 的「即使目标等于当前」） |
| 建集成凭据 | `app/services/integration_access.py:155` | `_resolve_project` 之前加租户共享锁读（`SELECT … FOR SHARE`，`populate_existing`，同 `project_integration._lock_tenant_shared`） | 共享锁后、生成密钥之前 |
| 轮换集成凭据 | `integration_access.py:209` | 同上，在 `_locked_versions` 之前 | 共享锁后、锁版本行之前 |
| 签发 AI 客户端密钥 | `app/ai_api/admin.py:136` | `_locked_project` 之前加租户共享锁读 | 共享锁后、锁项目行之前 |
| 签发 webhook 签名密钥 | `app/services/webhook_signing.py:189` | `_locked_versions`（锁项目行）之前加租户共享锁读 | 共享锁后 |
| 启用 webhook 签名密钥 | `webhook_signing.py:244` | 同上 | 共享锁后；**已是 `ACTIVE` 的幂等返回也先判 `CLOSED`**（409） |
| 开通内部计量 | `app/services/internal_billing.py:43` | 已锁「钱包 → 租户」（`:58-75`） | 锁租户后、「已是内部计量直接返回」之前 |

不改的写入口（照常允许）：吊销集成凭据版本 / 整个 key、吊销 AI 客户端密钥、退役 webhook 签名密钥、编辑客户资料、清除项目状态 webhook 地址（`project_integration.py:414` 已只拒绝非空地址）、门户账号的停用与重置（`portal_accounts.py` 已有自己的规则）、用量事件重新入队（结果确定，见「契约四」）、支付复核的「不入账结案」。

每个入口的 `CLOSED` 判定排在 404（客户、项目不存在）之后、其他业务 409 之前；主密钥未配置的 503（建凭据、轮换、签发签名密钥在事务外先 `load_keyring`）仍最先。

### 契约三：关户服务（AIH-TASK-212，新文件 `app/services/account_closure.py`）

#### `closure_blockers(session, *, tenant_id, now) -> ClosureBlockers`

只读、全部普通读（不加锁），四类各一次查询；`ClosureBlockers` 是不可变数据类，字段 `usage_events`、`ai_usage`、`payments`、`payment_events`（`int`）与 `payment_events_truncated`（`bool`），方法 `codes()` 按下表顺序给出非零类别的错误码。

| 顺序 | 类别 | 查询（全部带 `tenant_id` 过滤） | 错误码 |
| --- | --- | --- | --- |
| 1 | 用量事件 | `COUNT(*) FROM usage_events WHERE tenant_id = :t AND status IN (<UsageEventStatus 里除 PROCESSED、FAILED_FINAL 以外的全部取值>)`。集合由枚举推导（新增的状态默认算未结），写成正向 `IN` 以便走 `(status, occurred_at)` 索引 | `ACCOUNT_CLOSURE_USAGE_PENDING` |
| 2 | AI 模块 outbox | `COUNT(*) FROM ai_usage_outbox WHERE tenant_id = :t AND status IN ('PENDING', 'SENDING', 'HELD')` | `ACCOUNT_CLOSURE_AI_USAGE_PENDING` |
| 3 | 支付 | `COUNT(*) FROM payments WHERE tenant_id = :t AND (status IN ('PENDING', 'REVIEW_REQUIRED') OR (status = 'EXPIRED' AND created_at > :now - 7 天))` | `ACCOUNT_CLOSURE_PAYMENTS_PENDING` |
| 4 | 网关事件 | 见下 | `ACCOUNT_CLOSURE_PAYMENT_EVENTS_PENDING` |

- 常量 `EXPIRED_PAYMENT_SETTLE_DAYS: Final = 7`（Kelvin 决定 2）。边界：`created_at` 恰好等于 `now - 7 天` **不挡**；晚一秒挡。`FAILED`、`CANCELLED`、`PAID`、`EXPIRED`（超过 7 天）不挡。
- **网关事件的归属**（事件在 `RECEIVED` / `PROCESSING` 时 `payment_id` 为空，SQL 方言无关地在 Python 里做）：
  1. 取全库 `processing_status IN ('RECEIVED', 'PROCESSING', 'RETRY')` 的事件 `(id, payment_id, metadata_json)`，按 `id` 升序，至多 `PAYMENT_EVENT_SCAN_LIMIT` 行（模块级常量，值 10000；走 `ix_payment_gateway_events_due`）。函数**在调用时**读这个模块属性（不做成参数缺省值，也不复制进局部常量），测试用 `monkeypatch.setattr(account_closure, "PAYMENT_EVENT_SCAN_LIMIT", 3)` 注入，经 `close_account` 与 `get_closure_check` 调用时同样生效；签名不加参数；
  2. 取本租户全部支付的 `(id, public_id, gateway_payment_id, gateway_payment_intent_id)`（走 `ix_payments_tenant_created`）；
  3. 一个事件属于本租户，当且仅当：`payment_id` 是本租户某笔支付的 `id`；或 `metadata_json` 的 `client_reference_id` 是本租户某笔支付的 `public_id`；或 `object_id` 是本租户某笔支付的 `gateway_payment_id`；或 `payment_intent` 是本租户某笔支付的 `gateway_payment_intent_id`（只比较字符串值；非字符串忽略）。多归属只会让关户更严，不会放过；
  4. `payment_events` = 归属本租户的条数；第 1 步恰好取满上限（可能还有更多）时 `payment_events_truncated = True`，**该类别按未结处理**（`codes()` 照报 `ACCOUNT_CLOSURE_PAYMENT_EVENTS_PENDING`，即使计数为 0），并记一条 WARNING（固定文本 + 上限值，不带租户以外的数据）。
- 不读用量事件、支付、事件的金额与内容；只计数。

#### `get_closure_check(factory, *, customer_id, now=None) -> ClosureCheck`

一个只读会话：按 `public_id` 读租户（没有 → `CustomerNotFound`）与钱包余额（没有钱包 → `RuntimeError("WALLET_MISSING")`，接口 500，与 `close_account` 同一处理）、`closure_blockers`，返回服务层的不可变数据类 `ClosureCheck`。不加锁、不写、不写审计。

`ClosureCheck`（定义在 `app/services/account_closure.py`，`@dataclass(frozen=True)`）：`account_status: AccountStatus`、`balance: Decimal`（钱包余额原值）、`blockers: ClosureBlockers`、`codes: tuple[str, ...]`（此刻关户会被拒绝的全部原因码，顺序同 `close_account` 的判定顺序：账户不是 `DISABLED` 且不是 `CLOSED` 时首项 `ACCOUNT_STATUS_TRANSITION_INVALID`；余额不为 0 时 `ACCOUNT_CLOSURE_BALANCE_NOT_ZERO`；之后 `blockers.codes()`；`CLOSED` 时为空元组）。服务层不 import 接口 schema；接口层的 `ClosureCheckView`（pydantic，`app/schemas/customers.py`，AIH-TASK-213）由它转换而来，金额在转换时经 `money_text` 写成 8 位小数字符串。

#### `close_account(factory, *, actor, customer_id, reason, context, now=None) -> CustomerDetail`

`now` 缺省为 `utc_now()` 截到整秒。

0. **事务 0**（只读、短）：`SELECT tenants.id WHERE public_id = :customer_id`；没有 → `CustomerNotFound`（404）。单独一个事务，**是为了不让事务 1 的一致性读快照在拿锁之前建立**（InnoDB 的快照在事务里第一次普通读时建立；锁定读不建立快照，`portal_accounts.py` 已有同一说明）。
1. **事务 1**（`session_scope`）：
   1. 锁钱包：`SELECT wallets … WHERE tenant_id = :id FOR UPDATE`（`populate_existing`）；没有钱包 → `RuntimeError("WALLET_MISSING")`（500，同 `internal_billing.py:67`；每个租户建档时都有钱包，属于数据缺陷）。
   2. 锁租户：`SELECT tenants … FOR UPDATE`（`populate_existing`）。顺序与 `post_transaction`、支付确认的「（支付 →）钱包 → 租户」一致。
   3. `account_status is CLOSED` → 返回（不写任何行，不写审计，不加版本），事务提交空事务；接口返回 200 与当前详情。
   4. `account_status is not DISABLED` → `AccountStatusTransitionInvalid`（409 `ACCOUNT_STATUS_TRANSITION_INVALID`，复用 `account_status.py:59`）。
   5. `wallet.balance != Decimal(0)`（锁内刷新的值）→ 409 `ACCOUNT_CLOSURE_BALANCE_NOT_ZERO`。
   6. `closure_blockers(session, tenant_id=…, now=…)`：这是事务 1 的第一次普通读，快照在两把锁都拿到之后建立；第一个非零（或截断）的类别 → 对应 409。
   7. 写：调用从 `account_status._apply` 抽出的 `_record_transition(session, tenant, previous=DISABLED, wanted=CLOSED, reason, actor, context, now)`（见下），与 `change_account_status` 写同样的四样东西：`account_status = CLOSED`、`bump_status_version`（`status_version` +1、`status_changed_at = now`、outbox `tenant.account_status_changed`、重排该租户全部项目投递行）、`updated_at = now`、审计 `TENANT_ACCOUNT_STATUS_CHANGED`（`before_state = {account_status: DISABLED, status_version: v}`、`after_state = {account_status: CLOSED, status_version: v+1}`、`reason`）。
2. 提交之后，事务外 `customers.get_customer` 读详情返回（与 `change_account_status` 相同写法）。

错误类（全部 409，`app/services/account_closure.py`）：`AccountClosureBalanceNotZero`、`AccountClosureUsagePending`、`AccountClosureAiUsagePending`、`AccountClosurePaymentsPending`、`AccountClosurePaymentEventsPending`；消息是固定英文句子，不带计数与金额（计数看 `closure-check`）。

**对 `account_status.py` 的改动（AIH-TASK-212）**：把 `_apply` 第 177–221 行的写入部分原样抽成 `_record_transition`，`_apply` 改为「`transition` 判定 → 目标等于当前就返回 → `_record_transition`」，行为不变。**`TRANSITIONS` 表不变**：`DISABLED` 仍只能到 `ENABLED`，通用接口永远产生不了 `CLOSED`（现有测试 `test_account_status_service.py:327-346` 照旧成立）；`DISABLED → CLOSED` 只由 `close_account` 产生，它自己判前置条件。模块文件头的「只锁租户行」说明补一句：关户不经这里的入口，它锁「钱包 → 租户」。

### 契约四：关户之后各类未结来源的去向

关户事务提交后，下表每一行都有确定的最终状态，没有一行能产生财务效果或把账户改回去：

| 来源 | 关户后还可能怎样出现 | 挡住它的防线 | 最终状态 |
| --- | --- | --- | --- |
| 集成方上报的用量（凭据未吊销） | 摄取不看账户状态，照常落库 `RECEIVED` | 计费 ② 读到 `CLOSED`（`usage_billing.py:356`） | `FAILED_FINAL` / `ACCOUNT_CLOSED`，无账本行 |
| 关户事务快照建立之后、提交之前落库的用量 | 关户的普通读看不到它 | 同上；若 ② 在关户提交前读到 `DISABLED`，⑧ 的 `post_transaction` 要等关户释放钱包锁，拿到租户锁时读到 `CLOSED` → `TenantClosed` → 捕获 | 同上 |
| 同上，但租户是内部计量，或这笔 `billable_cost = 0` | ⑧ 不调 `post_transaction` | 无（没有财务效果可挡） | `PROCESSED`，`wallet_transaction_id` 为空，无账本行（§10 已知限制 2） |
| 管理员把 `FAILED_FINAL` / `ACCOUNT_CLOSED` 的事件重新入队 | 改回 `RECEIVED` | 再次计费 → ② 读到 `CLOSED` | 再次 `FAILED_FINAL` / `ACCOUNT_CLOSED`；只多一条 `USAGE_EVENT_REQUEUE` 审计 |
| AI 模块 outbox 行（停用前开始、关户后才写入的 AI 调用） | 写 `PENDING` → 投递到摄取 | 同第一行 | 同第一行。凭据已吊销时投递一直 `SIGNING_CREDENTIAL_MISSING` 重试、停在 `PENDING`，无财务效果（§5） |
| 迟到到账（`EXPIRED` 超过 7 天、`FAILED` 的支付） | 网关事件 → `confirm_payment` | 锁「支付 → 钱包 → 租户」后 `_paid_check_failure` → `TENANT_CLOSED` | 支付 `REVIEW_REQUIRED`，无账本行；B 的复核里只能「不入账结案」（CREDIT 被 `TENANT_CLOSED` 拒绝），之后在 Stripe 人工退款（`docs/payment-flow.md` §6.4） |
| 新建支付 | — | 关户前提是 `DISABLED`，建单锁租户后 `ACCOUNT_READ_ONLY`（`payments.py:854`） | 不产生 |
| 管理员调账 | — | `post_transaction` 锁内 `TenantClosed` | 409 `ACCOUNT_CLOSED`，不写 |
| 关户后的争议 / 退款事件 | 回调落库 → 只关联、不改账 | 现有（`app/tasks/payment_events.py` `_link_charge_event`） | 事件 `PROCESSED`，P1 告警；**不能再记账扣回**（§10 已知限制 1） |
| 账户状态接口 | — | `TRANSITIONS[CLOSED]` 为空 | 409，账户保持 `CLOSED` |

### 对账单资格（AIH-TASK-211，`app/services/statements.py:859`）

`is_eligible(tenant, wallet, period)` 改为：

```text
PREPAID 且 wallet 不为空 且 wallet.created_at < period.end            （原条件，不变）
且（account_status 不是 CLOSED
    或 status_changed_at 为空
    或 period.start <= status_changed_at）                              （新增）
```

- 关户时刻 `T = status_changed_at`。包含 `T` 的马来西亚月（`period.start <= T < period.end`）与之前的月合格；`period.start > T` 的月不合格。于是关户当月照常出最后一份（在次月第 2 日的整点任务里），之后不再生成。
- 「缺失对账单」告警（`alerts.py:459`）与管理端补生成（`catch_up_tenant`）都调用同一个 `is_eligible`，自动一致：关户之后的账期不算缺失、补生成跳过。
- 上期连续性检查（`statements.py:948-956`）不受影响：关户当月的上一期在关户之前、照旧合格；关户之后的账期本身不合格、不会去查它的上一期。
- 关户当月的对账单完整：关户之后 `post_transaction` 拒绝一切新账本行，钱包高水位在关户时就不再变化。
- `status_changed_at` 为空而 `CLOSED`：正常路径不会出现（关户在同一事务里写它）；只可能来自直接改库。此时**照常合格**（多出零发生额的对账单，不丢任何一期）。
- 关户当月**一律**出单（无发生额时是一份期初 = 期末 = 0 的对账单），见 §1 Kelvin 的决定。

#### 关户时刻为什么可以取 `status_changed_at`

`status_changed_at` 只由 `bump_status_version` 写（`docs/database-schema.md:50`），它的四个调用方在 `CLOSED` 之后都不会再执行：账户状态（`TRANSITIONS[CLOSED]` 为空）、计费状态（只在 `post_transaction` 里，AIH-TASK-209 后对 `CLOSED` 抛 `TenantClosed`、在跃迁之前）、内部计量开通（AIH-TASK-210 后 409）、项目集成状态（已有 409）。AIH-TASK-211 的测试逐一钉住这四条在 `CLOSED` 租户上不改 `status_version` 与 `status_changed_at`。以后新增 `bump_status_version` 的调用方时必须同样拒绝 `CLOSED`，写进 `docs/database-schema.md` 那一节（AIH-TASK-215）。

### 接口（AIH-TASK-213，`app/api/admin_customers.py`、`app/schemas/customers.py`）

两个接口都在 `/api/v1/admin` 下，显式 `require_admin`（ADMIN 登录必过 2FA）；客户只来自路径。

**`GET /api/v1/admin/customers/{customer_id}/closure-check`** → 200 `ApiResponse[ClosureCheckView]`；401 / 403 / 404 `CUSTOMER_NOT_FOUND` / 500（没有钱包，数据缺陷）/ 503。`ClosureCheckView` 定义在 `app/schemas/customers.py`，由服务层 `ClosureCheck` 逐字段转换（`blockers` 取 `ClosureCheck.codes`，计数取 `ClosureCheck.blockers` 的各字段）。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `account_status` | string | 当前账户状态 |
| `balance` | string | 钱包余额，恰好 8 位小数的定点字符串（与 `WalletView.balance` 同一写法） |
| `unresolved_usage_events` | int | 类别 1 的计数 |
| `unresolved_ai_usage` | int | 类别 2 |
| `unresolved_payments` | int | 类别 3 |
| `unresolved_payment_events` | int | 类别 4（归属本租户的条数） |
| `payment_events_truncated` | bool | 类别 4 的扫描到了上限 |
| `blockers` | string[] | 此刻关户会被拒绝的全部原因码，顺序同 POST 的判定顺序：账户不是 `DISABLED` 时首项 `ACCOUNT_STATUS_TRANSITION_INVALID`；余额不为 0 时 `ACCOUNT_CLOSURE_BALANCE_NOT_ZERO`；之后 `closure_blockers.codes()`。`CLOSED` 时为空数组 |

只读、不写审计。`blockers` 为空不保证随后的 POST 成功（之间可能有新事件），POST 在锁内重查。

**`POST /api/v1/admin/customers/{customer_id}/close`**，请求体 `CloseAccountRequest`：`{"reason": string}`，`extra="forbid"`，`reason` 去首尾空白后 1–255（复用 `_Reason`，`customers.py:137`）。成功 200 `ApiResponse[CustomerDetail]`（已是 `CLOSED` 时同样 200、不写）。

| HTTP | `error.code` | 什么时候 |
| --- | --- | --- |
| 404 | `CUSTOMER_NOT_FOUND` | 客户不存在 |
| 409 | `ACCOUNT_STATUS_TRANSITION_INVALID` | 账户不是 `DISABLED`（也不是 `CLOSED`） |
| 409 | `ACCOUNT_CLOSURE_BALANCE_NOT_ZERO` | 余额不恰好为 0 |
| 409 | `ACCOUNT_CLOSURE_USAGE_PENDING` | 有未结用量事件 |
| 409 | `ACCOUNT_CLOSURE_AI_USAGE_PENDING` | AI 模块 outbox 有该租户的 `PENDING` / `SENDING` / `HELD` 行 |
| 409 | `ACCOUNT_CLOSURE_PAYMENTS_PENDING` | 有 `PENDING`、`REVIEW_REQUIRED` 或 7 天内创建的 `EXPIRED` 支付 |
| 409 | `ACCOUNT_CLOSURE_PAYMENT_EVENTS_PENDING` | 有归属该租户的 `RECEIVED` / `PROCESSING` / `RETRY` 网关事件，或扫描到了上限 |
| 422 | `VALIDATION_ERROR` | 原因缺失、为空白或超长；多余字段 |
| 500 | `INTERNAL_ERROR` | 没有钱包（数据缺陷）或数据库错误 |

判定顺序：422（请求校验）→ 404 → `CLOSED` 直接 200 → `ACCOUNT_STATUS_TRANSITION_INVALID` → 余额 → 四类未结（按表序）。

账户状态接口 `POST …/account-status` 不变：请求体仍只收 `ENABLED` / `DISABLED`，`CLOSED` 作目标 422，从 `CLOSED` 出发 409。

AIH-TASK-210 让以下已有接口多一个 409 `ACCOUNT_CLOSED`（写进 `docs/api.md` 对应各节）：建项目、低余额阈值 PUT、建凭据、轮换凭据、签发 AI 客户端密钥、签发与启用 webhook 签名密钥、开通内部计量；AIH-TASK-209 让手工调账多一个 409 `ACCOUNT_CLOSED`。

### 前端（AIH-TASK-214）

- `frontend/src/api/adminCustomers.ts`：类型 `ClosureCheck`；`getClosureCheck(customerId)`；`postCloseAccount(customerId, {reason})`，失败抛 `CloseAccountError`（`ApiError` 子类，保留 HTTP 状态，同 `AccountStatusError` 的写法）。
- 新组件 `frontend/src/features/customers/CloseAccountModal.tsx`：
  - 详情页仅当 `account_status === "DISABLED"` 时显示「Close account」按钮（危险样式）；其他状态（含 `CLOSED`）不显示。`CLOSED` 时账户状态区照常显示状态文本，不给任何状态按钮（`accountStatusTarget` 已对 `CLOSED` 返回 `null`）。
  - 打开弹窗即请求 `closure-check`，显示余额与四类计数；`blockers` 非空时逐条显示原因说明（每个码一句英文文案）与「Refresh」按钮（重新请求 `closure-check`），确认按钮不可点。
  - `closure-check` 请求进行中：显示加载状态，确认按钮不可点；请求失败（4xx、5xx、网络错误）：显示 `error.code`（没有时显示 HTTP 状态）与 `request_id`，确认按钮不可点，给「Refresh」重新请求；只有最近一次请求成功且 `blockers` 为空时，确认按钮才由原因与客户名两项决定是否可点。
  - 原因必填：去首尾空白后 1–255 码点（与 `AccountStatusModal` 同一计数法）。
  - 客户名确认：输入框的值去首尾空白后与详情里的 `company_name` **完全相同**（区分大小写）时，确认按钮才可点；不在前端以外的地方校验（Kelvin 决定 5）。
  - 提交 `POST …/close`；成功关闭弹窗，让客户详情与客户列表过期重读。404 / 409 / 422：显示 `error.code`、信息与 `request_id`，并重新请求 `closure-check`；5xx 与网络错误：按钮变「Try again」，原样重发同一请求体（安全：已关户是 200 不写，未关户则重新判定）。不自动重试。
- `frontend/src/features/customers/CustomerDetailPage.tsx`：接入按钮与弹窗。
- 文案进 `frontend/src/i18n/locales/en.json`。
- 浏览器验收（只读）：`check_account_closure`，见 §11。

### runbook（AIH-TASK-215，`docs/runbook.md` 新增「关户」一节）

固定顺序（每步做完再做下一步；任何一步发现问题停下来，不跳步）：

1. **停用**：管理端客户详情「Disable account」，原因写关户工单号。此后新的 AI 调用与新充值被拒（spec §112.1 第 1 步）。
2. **等 AI 模块 outbox 清空**：在 `closure-check` 看 `unresolved_ai_usage` 归零（停用后不再有新的 AI 调用，在途的会在调用超时后写入并投递）。`HELD` 行按「AI 模块用量 outbox」一节处理（释放或丢弃，带原因）。**这一步之前不要吊销集成凭据**：投递要用它签名，先吊销会让行一直停在 `PENDING`。
3. **吊销凭据**：吊销该客户全部 AI 客户端密钥与全部集成凭据（整个 key）。集成方在此之后上报的用量被 401 拒绝；吊销前与集成方确认已无待上报的用量。
4. **等用量与支付到终态**：`unresolved_usage_events`、`unresolved_payments`、`unresolved_payment_events` 归零。用量停在 `MODEL_UNKNOWN` / `PRICING_ERROR` / `FX_RATE_ERROR`：先修目录或价格再重新入队（§10 已知限制 4）；支付 `REVIEW_REQUIRED`：按 `docs/payment-flow.md` §6 结案；`EXPIRED` 支付在创建满 7 天后不再挡。
5. **余额清零**（只用调账，不直接改钱包；每笔调账的原因写清楚）：
   - **正余额，来自付款**：在 Stripe 后台对相应付款人工退款，退款额不超过该付款在 Stripe 上的可退额；每笔退款对应一条 `REFUND_ADJUSTMENT`，**原因里写支付号**（`payments.public_id`）与 Stripe 退款号。退款会产生 `charge.refunded` 事件（P1「disputes or refunds 24h」，预期内，24 小时后消失）；等它被处理（`unresolved_payment_events` 归零）再继续。
   - **正余额，不来自付款**（赠送、更正）：`ADJUSTMENT_DEBIT`，原因写明来源。
   - **负余额**：客户在系统外付清的，`ADJUSTMENT_CREDIT`，原因写外部付款凭据号；核销的，`ADJUSTMENT_CREDIT`，原因写「坏账核销」与审批人。
   - 核对余额恰好 `0.00000000`。
6. **收据与对账单**：不需要操作。已开具的收据不可变；没有收据的 `PAID` 支付由补发扫描照常补开；关户当月的对账单在次月第 2 日自动生成，之后不再生成。
7. **关户**：客户详情「Close account」，确认 `closure-check` 全部为零，填原因（关户工单号），输入公司名确认。
8. **关户之后**：`FAILED_FINAL` / `ACCOUNT_CLOSED` 的用量事件人工复核、不要重新入队；`REVIEW_REQUIRED` / `TENANT_CLOSED` 的支付只能「不入账结案」并在 Stripe 退款；争议与退款事件不能再记账，在 Stripe 与会计账上处理。账户不能重开；同一家公司要重新使用，新建客户。

### 事务边界

- 关户：事务 0（只读）+ 事务 1（锁、判定、状态、版本、outbox、投递重排、审计一次提交，INV-13）；详情在提交后另读。
- `closure-check`：一个只读会话。
- AIH-TASK-209：拒绝发生在 `post_transaction` 的任何写入之前；调用方各自的事务照旧（用量计费在同一事务里写 `FAILED_FINAL`）。
- AIH-TASK-210：拒绝发生在各入口事务的任何写入之前，整个事务回滚。

### 外部系统与异步边界

- 状态推送：`bump_status_version` 重排投递行，现有的状态投递任务推送 `BLOCK_AI` / `ACCOUNT_CLOSED`（`compose_effective_status` 已有该原因码）。`tenant.account_status_changed` outbox 行照旧无处理器、`PENDING` 持久保存。
- 不调用 Stripe；不发邮件；不新增 Celery 任务或 Beat 条目。

### 时间语义

`now` 为不带时区的 UTC、整秒。`status_changed_at` 即关户时刻。`EXPIRED` 的 7 天窗口按 `payments.created_at` 与 `now` 比较。对账单账期按马来西亚日历月（`StatementPeriod.start` 为 UTC）。

### 金额与精度

只读余额：`Decimal` 与 `Decimal(0)` 比较，不经 float；接口里是 8 位小数的定点字符串。关户不写任何金额。

## 3. 不变量影响矩阵

| 不变量 | 是否触碰 | 可能怎样被破坏 | 设计控制 | 验证方式 |
| --- | --- | --- | --- | --- |
| INV-1 中心故障不中断 AI | 否 | 关户只对已 `DISABLED`（AI 已被拒）的租户生效，不在 AI 调用路径上；新增的锁只在管理写入口与关户事务里 | — | — |
| INV-2 事件不重复扣费 | 是 | 关户后用量被扣费；重新入队的事件被扣费 | ② 判 `CLOSED`；② 读到旧值时 `post_transaction` 锁内 `TenantClosed`、在写之前；捕获后 `FAILED_FINAL`；重新入队后再判一次 | §7「计费旧值窗口」「重新入队」 |
| INV-3 支付不重复入账 | 是 | 关户后迟到到账被入账 | 支付确认与人工入账锁内先判 `TENANT_CLOSED`（不变）；`post_transaction` 兜底 | §7「迟到到账」 |
| INV-4 余额只经账本变动 | 是 | 关户为了「清零」直接改钱包 | 关户不写钱包，只读并要求恰好为 0；清零只经调账（runbook） | §7「关户不写钱包」（钱包 `version` 不变） |
| INV-5 历史账本不可变 | 否 | 只读余额；不改、不删任何账本行 | — | — |
| INV-6 事件保留版本引用 | 否 | `FAILED_FINAL` 的事件本来就没有定价快照（`_settle` 只写解析到的模型），这条路径照旧 | — | — |
| INV-7 客户不可见成本毛利 | 否 | `closure-check` 只给 ADMIN，字段只有余额与计数；客户接口不变 | 响应字段白名单 | §7「接口字段」 |
| INV-8 租户不可互访 | 是 | 关户检查把别的租户的事件算进来或漏掉；改别人的状态 | 客户只来自路径；四类查询都按本租户过滤，网关事件只按本租户支付的四种引用归属 | §7「跨租户」 |
| INV-9 对话内容不入库 | 否 | 不读不写用量内容与 outbox 的 `payload_json` | 只 `COUNT` | — |
| INV-10 金额用 Decimal | 是 | 余额用 float 比较或格式化 | `Decimal(0)` 精确比较；定点字符串 | §7「余额边界」 |
| INV-11 event_id 至多一次财务效果 | 是 | 关户后同一 `event_id` 被记账；`TenantClosed` 之后半写 | 拒绝在任何写之前；重放分支照旧在租户锁前返回既有行（无新效果） | §7「post_transaction 拒绝」「重放」 |
| INV-12 定稿对账单不可变 | 是 | 关户后又出新期对账单、或关户当月对账单漏行 | 只改「哪些账期合格」，不碰已定稿行；关户后不再有账本行，当月对账单完整 | §7「对账单资格」 |
| INV-13 状态与事件原子提交 | 是 | 状态改了而审计 / outbox / 版本没写 | 复用 `_record_transition` 在同一事务写四样；AIH-TASK-209 / AIH-TASK-210 的拒绝都在写之前 | §7「关户正常路径」「事务中途失败」 |
| INV-14 队列丢失不毁持久工作 | 是 | Celery 丢失导致状态推送或对账单不发 | 投递行与 outbox 行在库里（扫库重排）；对账单由 Beat 扫库；关户本身同步完成 | §7「Redis/Celery 丢失」 |

## 4. 状态与并发

### 账户状态跃迁（新增一格，其余不变）

| 当前状态 | 事件 | 前置条件 | 新状态 | 副作用 | 非法时结果 |
| --- | --- | --- | --- | --- | --- |
| `DISABLED` | `close` | 锁内余额恰好 0，四类未结都为 0 | `CLOSED` | 版本 +1、`status_changed_at`、outbox、重排投递、审计（带原因） | 409，按判定顺序给出第一条原因码，不写 |
| `CLOSED` | `close` | — | `CLOSED` | 无 | 200，不写 |
| `ENABLED` / `PENDING_ACTIVATION` | `close` | — | — | — | 409 `ACCOUNT_STATUS_TRANSITION_INVALID` |
| `CLOSED` | 账户状态接口任何目标 | — | — | — | 409（现有，`TRANSITIONS[CLOSED]` 为空）；目标等于 `CLOSED` 在请求校验层 422 |

### 关户后其他实体的状态（已有跃迁，本设计只钉住结果）

| 实体 | 当前状态 | 事件 | 新状态 |
| --- | --- | --- | --- |
| 用量事件 | `PROCESSING` | 计费，租户 `CLOSED`（② 或 ⑧） | `FAILED_FINAL` / `ACCOUNT_CLOSED` |
| 支付 | `PENDING`（不可能，关户前已挡）/ `EXPIRED` / `FAILED` | 网关报 `PAID` | `REVIEW_REQUIRED` / `TENANT_CLOSED` |
| 支付 | `REVIEW_REQUIRED` | 人工 CREDIT | 拒绝 `TENANT_CLOSED`（不变） |
| 支付 | `REVIEW_REQUIRED` | 人工 CLOSE | `FAILED`（不变） |

### 并发

**全局锁顺序（不变，只确认新路径都遵守）**：事件行 → 供应商 → FX → 规则 → **钱包 → 租户** → 项目 / 投递行 / 凭据 / 密钥行；支付确认在最前多一个支付行。

- 关户锁「钱包 X → 租户 X」：与 `post_transaction`、支付确认、内部计量开通同序。
- AIH-TASK-210 的入口锁「租户（S 或 X）→ 项目 / 凭据 / 密钥」，不锁钱包；没有任何路径先锁项目 / 凭据 / 密钥行再锁租户（`project_integration.py:290`、`:411`、`internal_billing.py:58-87` 都是租户在前），不成环。
- 关户与 `post_transaction` 串行在钱包锁上：入账先拿到锁就先提交（关户随后锁内读到新余额或新的未结行）；关户先提交，入账拿到租户锁时读到 `CLOSED`。
- 关户与建单串行在租户锁上；且建单要求非只读，`DISABLED` 期间根本不会插入。
- 关户与账户状态接口（`DISABLED → ENABLED`）串行在租户锁上：先启用则关户锁内读到 `ENABLED` → 409；先关户则启用读到 `CLOSED` → 409。
- **检查与提交之间的竞态**：四类未结是普通读，快照在两把锁之后建立。锁挡住的写（入账、建单、状态跃迁）在快照里都已是最新；锁挡不住的写（用量摄取、AI outbox 写入、网关回调落库）可能在快照之后出现、被关户看漏——它们在关户后的去向见「契约四」，全部没有财务效果。
- **数据库负责的唯一性**：不新增约束；`(reference_type, reference_id)` 唯一照旧兜底入账的幂等。
- **幂等键**：关户没有幂等键；已是 `CLOSED` 时 200 不写，重发安全。
- **相同 ID、不同载荷**：不适用（同一客户重复关户、原因不同：第二次不写，原因丢弃，审计只有第一次的）。
- **重试层**：关户不自动重试；死锁或锁等待超时（`OperationalError`）回滚、500，管理员重发。
- **必须原子提交**：状态、版本、`status_changed_at`、outbox、投递重排、审计（INV-13）。

## 5. 失败与恢复矩阵

| 失败点 | 对外结果 | 数据最终状态 | 是否可重试 | 恢复来源 | 告警 |
| --- | --- | --- | --- | --- | --- |
| 校验失败（原因缺失 / 超长 / 多余字段） | 422 | 不变 | 改请求后可 | — | — |
| 客户不存在 | 404 | 不变 | 否 | — | — |
| 不是 `DISABLED` | 409 | 不变 | 先停用后可 | runbook 第 1 步 | — |
| 余额不为 0 / 有未结 | 409（第一条原因） | 不变 | 是 | runbook 第 2–5 步；`closure-check` 看还差什么 | — |
| 网关事件扫描到上限 | 409 `…PAYMENT_EVENTS_PENDING` | 不变 | 积压消化后可 | 支付事件积压的现有处置 | WARNING 日志；现有支付积压告警 |
| 没有钱包 | 500 | 不变 | 否 | 数据事故处理 | ERROR（未处理异常） |
| DB 提交失败 / 死锁 | 500 | 整个事务回滚：账户仍 `DISABLED`，没有审计、outbox、版本变化 | 是 | 管理员重发 | — |
| Redis / Celery 丢失 | 关户照常（同步，不依赖队列） | `CLOSED` 已提交；投递行已重排、outbox 行在库 | — | Beat 恢复后状态投递任务扫库推送；对账单任务扫库补生成 | 现有 Beat / 投递告警 |
| 外部服务超时 | 不适用：关户不调外部服务 | — | — | — | — |
| 重复请求 | 第二次 200、不写 | `CLOSED` 一次 | — | — | — |
| 关户后计费读到旧值 | — | 事件 `FAILED_FINAL` / `ACCOUNT_CLOSED`，无账本行 | — | 人工复核（runbook 第 8 步） | 现有用量失败告警 |
| 关户后 AI outbox 行投递不出（凭据已吊销） | — | 行停在 `PENDING`，无财务效果 | — | runbook 第 2 步的顺序避免；出现时按 runbook「AI 模块用量 outbox」处理 | 现有 outbox 积压告警 |
| 关户后迟到到账 | — | 支付 `REVIEW_REQUIRED` / `TENANT_CLOSED`，无账本行 | — | 「不入账结案」+ Stripe 退款 | 现有「review open」P1 |
| `post_transaction` 抛 `TenantClosed` 给调账 | 409 `ACCOUNT_CLOSED` | 不变 | 否 | — | — |
| AIH-TASK-210 入口遇 `CLOSED` | 409 `ACCOUNT_CLOSED` | 不变 | 否 | — | — |

## 6. 数据与安全边界

- **鉴权**：两个新接口显式 `require_admin`（ADMIN，登录必过 2FA）；AIH-TASK-210 的入口鉴权不变。
- **租户过滤**：客户只来自路径的 `public_id`；四类未结查询全部带本租户条件；网关事件只按本租户支付的 `id`、`public_id`、会话 id、PaymentIntent 归属，别的租户的事件不计入。
- **客户侧**：不新增客户接口；`closure-check` 只给管理端。门户在 `CLOSED` 下只读（现有）。
- **字段**：`closure-check` 只有账户状态、余额、计数、截断标志与原因码；没有成本、毛利、个人数据。
- **日志**：关户服务不写日志（同 `account_status.py` 的约定），只有网关事件扫描截断时一条 WARNING（固定文本 + 上限）；用量计费的 INFO 只带 `_context(event)`（事件 id、租户与项目 id）。原因是管理员自由文本，**只进审计 `reason`**，不进日志、outbox payload、异常消息。
- **审计**：沿用 `TENANT_ACCOUNT_STATUS_CHANGED`，白名单不变（`account_status`、`status_version`），`reason` 必填。spec §66 的映射在 `docs/TODO.md` 补一句：§66 的动作清单只有 `TENANT_SUSPEND` / `TENANT_REACTIVATE`，没有关户动作，所以 `DISABLED → CLOSED` 没有对应的 §66 动作名，与 `PENDING_ACTIVATION → ENABLED`（首次启用）同一处理，仍记 `TENANT_ACCOUNT_STATUS_CHANGED`。
- **密钥**：不新增、不读取；AIH-TASK-210 的建凭据、签发密钥在 `CLOSED` 时于生成 secret 之前（建凭据：主密钥检查之后、锁内判定之后才生成）拒绝，secret 不会生成。
- **prompt / response**：不处理。
- **数据保留**：关户不删除任何数据；财务记录永久保留；个人数据的删除走 spec §112.1 末段的另一流程，不在本设计。

## 7. 测试证据计划

后端测试照仓库根 `CLAUDE.md`「写后端测试的约定」：断言 SQL 时不写死方言相关的字面量；跨文件共用的 fixture 在 `tests/backend/conftest.py` 注册、定义放在不以 `test_` 开头的辅助模块（任务可改路径不含 conftest 时夹具写在本任务的测试文件里）；确认「对方确实在等行锁」查 `performance_schema.data_lock_waits`（按 `DATABASE()` 与表名过滤，写法照 `tests/backend/test_session_revocation_concurrency.py` 的 `_WAITING_ON_TABLE`），不查 `INNODB_TRX`；需要真 MySQL 的场景在 `BILLING_TEST_DATABASE_URL` 未设置时 skip（CI 必跑）；时间一律注入，不 sleep。造 `CLOSED` 租户用直接改库（现有 `force_status` 写法）或 AIH-TASK-212 之后的 `close_account`。

| 风险/需求 | 测试层级 | 场景 | 预期结果 | 任务 |
| --- | --- | --- | --- | --- |
| post_transaction 拒绝 | integration | `CLOSED` 租户依次用九种类型记账（含 `TOPUP`、`AI_USAGE`、`REFUND_ADJUSTMENT`） | 抛 `TenantClosed`；账本行数、钱包 `version` 与 `balance`、审计、租户 `status_version` 都不变 | AIH-TASK-209 |
| 重放 | integration | 租户 `DISABLED` 时记一条调账 → 改库为 `CLOSED` → 同一来源同效果再记；同一来源不同金额再记 | 第一次返回既有行 `replayed=True`；第二次 `LedgerConflict` | AIH-TASK-209 |
| 检查顺序 | unit/integration | `CLOSED` 且余额会越界 | `TenantClosed`（先于 `BALANCE_OUT_OF_RANGE`） | AIH-TASK-209 |
| 计费旧值窗口 | integration | 认领事件后，让 ② 读到 `DISABLED`（先把租户对象载入本会话的 identity map，再由另一会话提交 `CLOSED`），⑧ 的锁内读到 `CLOSED` | `_process` 返回 `FAILED_FINAL`，事件 `FAILED_FINAL` / `ACCOUNT_CLOSED`，没有账本行，不抛异常、不进 `record_failure` | AIH-TASK-209 |
| 计费 ② 照旧 | integration | 事件认领时租户已 `CLOSED` | 同上（现有行为不变） | AIH-TASK-209 |
| 调账 | integration（API） | `CLOSED` 客户调账；关户前已记的同一幂等键重发 | 409 `ACCOUNT_CLOSED`、什么都不写；重发 200 `replayed` | AIH-TASK-209 |
| 支付路径不变 | integration | `CLOSED` 租户的 `confirm_payment`（其余判定都通过）；人工 CREDIT | `REVIEW_REQUIRED` / `TENANT_CLOSED`；CREDIT 拒绝 `TENANT_CLOSED`（`test_payment_confirm.py`、`test_payment_review.py` 现有用例，AIH-TASK-209 不改它们、照常通过） | AIH-TASK-209 |
| 管理写入口 | integration（服务） | 对 `CLOSED` 租户：建项目、阈值设置与清除（含与现值相同）、建凭据、轮换、签发 AI 密钥、签发与启用签名密钥（含已 `ACTIVE` 的幂等启用）、开通内部计量（含已是内部计量） | 全部 409 `ACCOUNT_CLOSED`、不写（含审计）；`DISABLED` 租户同样操作照常成功；吊销凭据、吊销 AI 密钥、退役签名密钥、编辑资料对 `CLOSED` 照常成功 | AIH-TASK-210 |
| 入口判定顺序 | integration | `CLOSED` 租户 + 不存在的项目；主密钥未配置时建凭据 | 404 先于 409；503 先于 409 | AIH-TASK-210 |
| 入口与关户并发 | integration（MySQL） | 连接 A 持租户 `FOR UPDATE`（模拟关户）并提交 `CLOSED`；连接 B 同时建项目（经 `data_lock_waits` 确认 B 在等租户行） | B 在 A 提交后得到 409、不写 | AIH-TASK-210 |
| status_changed_at 冻结 | integration | `CLOSED` 租户上分别触发四个 `bump_status_version` 调用方 | `status_version`、`status_changed_at` 都不变 | AIH-TASK-211 |
| 对账单资格 | unit | `CLOSED`，`status_changed_at` 在账期 M 内 / 恰好等于 M 的开始 / 为空；`DISABLED` | M 与之前合格、M+1 不合格；恰好等于开始时 M 合格；为空时都合格；`DISABLED` 照旧 | AIH-TASK-211 |
| 对账单生成与告警 | integration | `CLOSED` 租户跨三个到期账期跑 `generate_due`；告警窗口内 | 关户当月及之前生成、之后不生成；`_missing_statements` 不把之后的账期算缺失；关户当月无发生额时生成期初 = 期末 = 0 的一份 | AIH-TASK-211 |
| 关户正常路径 | integration | `DISABLED`、余额 0、四类为 0 | `CLOSED`、版本 +1、`status_changed_at = now`、一行 outbox（payload 键与 `change_account_status` 相同，`account_status = CLOSED`、`effective_status = BLOCK_AI`）、投递行 `next_attempt_at` 提前到 `now`、一行审计（`reason`、前后状态白名单）；钱包 `version` 不变 | AIH-TASK-212 |
| 前置条件逐条 | integration | `ENABLED`；`PENDING_ACTIVATION`；余额 `0.00000001` 与 `-0.00000001`；每类未结各造一条（用量七种非终结状态各一；outbox `PENDING` / `SENDING` / `HELD` 各一；支付 `PENDING`、`REVIEW_REQUIRED`、7 天内与恰好 7 天前与 7 天零 1 秒前的 `EXPIRED`；网关事件 `RECEIVED`（`payment_id` 空、只靠 `client_reference_id`）、`PROCESSING`（只靠 `object_id`）、`RETRY`（带 `payment_id`）、争议事件（只靠 `payment_intent`）） | 各自的 409 码；什么都不写；恰好 7 天前的 `EXPIRED` 不挡；终结状态（`PROCESSED`、`FAILED_FINAL`、`DELIVERED`、`DEAD`、`DISCARDED`、`PAID`、`FAILED`、`CANCELLED`，网关事件 `PROCESSED` / `IGNORED` / `UNMATCHED` / `REJECTED`）都不挡 | AIH-TASK-212 |
| 判定顺序 | integration | 同时不满足多条 | 只报表序里的第一条 | AIH-TASK-212 |
| 网关事件扫描上限 | integration | 把上限注入为 3，造 3 条别的租户的未结事件 | `payment_events = 0`、`truncated = True`、关户 409 `…PAYMENT_EVENTS_PENDING`、一条 WARNING | AIH-TASK-212 |
| 已关户 | integration | 对 `CLOSED` 再关户 | 返回详情、什么都不写（审计、版本、outbox 都不变） | AIH-TASK-212 |
| 跨租户 | integration | 别的租户有各类未结与非零余额 | 本租户关户成功；`closure_blockers` 计数为 0 | AIH-TASK-212 |
| 并发：关户 vs 计费 | integration（MySQL） | 连接 A 关户，停在 `closure_blockers` **之后**、写入之前（注入钩子；此时 A 的四类计数都是 0、快照已建立）；这之后才插入一条该租户的 `RECEIVED` 用量事件，由连接 B 认领并计费：② 读到 `DISABLED`（A 未提交），⑧ 等钱包锁（`data_lock_waits` 确认）；放行 A 提交 | B 得到 `FAILED_FINAL` / `ACCOUNT_CLOSED`，没有账本行；余额仍 0 | AIH-TASK-212 |
| 并发：关户 vs 调账 | integration（MySQL） | 调账先拿到钱包锁、关户等待；反之 | 调账先：关户锁内读到非零余额 → 409；关户先：调账 409 `ACCOUNT_CLOSED` | AIH-TASK-212 |
| 并发：关户 vs 启用 | integration（MySQL） | 同时 `DISABLED → ENABLED` 与关户 | 恰好一个成功，另一个 409；不出现死锁 | AIH-TASK-212 |
| 事务中途失败 | integration | 审计写入时注入数据库错误 | 异常传出；账户仍 `DISABLED`，没有 outbox、版本不变 | AIH-TASK-212 |
| Redis/Celery 丢失 | integration | 关户时 broker 不可用（不注入任何 Celery 依赖） | 关户成功；投递行已重排、outbox 行 `PENDING` | AIH-TASK-212 |
| 接口 | integration（API） | 鉴权（匿名 401、客户令牌 403）；`closure-check` 字段、`blockers` 顺序、`CLOSED` 时为空；`close` 的 200、各 409、404、422（缺原因、空白、256 码点、多余字段 `account_status`）；`account-status` 接口对 `CLOSED` 目标仍 422 | 与 §2「接口」一致；响应只有列出的字段 | AIH-TASK-213 |
| 余额边界 | integration（API） | 余额 0、`0.00000001` | `balance` 为 `"0.00000000"`、`"0.00000001"`；后者 `blockers` 含 `ACCOUNT_CLOSURE_BALANCE_NOT_ZERO` | AIH-TASK-213 |
| 前端 | unit（前端） | 按钮只在 `DISABLED` 出现；弹窗读 `closure-check`、有 blockers 时确认不可点、Refresh 重读；原因 0 / 1 / 255 / 256 码点；客户名不符、大小写不同、首尾空白；成功后详情与列表过期；409 显示码与 request_id 并重读检查；5xx 「Try again」原样重发 | 与 §2「前端」一致 | AIH-TASK-214 |
| 浏览器验收 | acceptance（只读） | 夹具客户详情 | 「Close account」按钮出现当且仅当页面自己读到的详情 `account_status` 为 `DISABLED`；不点 | AIH-TASK-214 |

## 8. 迁移与上线

- **数据迁移**：无。`account_status` 的列与 CHECK 已允许 `CLOSED`（迁移 0009）；关户时刻用已有的 `status_changed_at`（迁移 0020）；原因进已有的审计表。最新迁移仍是 0039。
- **锁表与性能影响**：无 DDL。运行时：AIH-TASK-210 的入口多一次租户行共享 / 排他锁（短事务）；`post_transaction` 多一次已在内存里的属性比较；关户事务持钱包与租户锁的时间 = 四次计数查询（用量与 outbox 走状态索引，网关事件至多读 10000 行）。
- **兼容窗口**：生产里没有 `CLOSED` 租户（今天没有任何产生路径）。AIH-TASK-209–AIH-TASK-212 合并后都是「对 `CLOSED` 的处理」，在 AIH-TASK-213 接口上线之前不会被触发。
- **部署顺序**：AIH-TASK-209 → AIH-TASK-210 → AIH-TASK-211 → AIH-TASK-212 → AIH-TASK-213 → AIH-TASK-214 → AIH-TASK-215，依赖串行。AIH-TASK-213 合并（关户接口上线）时，防线（AIH-TASK-209、AIH-TASK-210）、对账单规则（AIH-TASK-211）与服务（AIH-TASK-212）都已在生产。AIH-TASK-214 之前只能经 API 关户。
- **回滚或前滚**：只回滚应用镜像。若回滚到 AIH-TASK-209 之前而已有 `CLOSED` 租户：计费 ② 与支付锁内判定仍挡住用量与到账，调账与 AIH-TASK-210 的入口失去拒绝——管理员不对 `CLOSED` 客户做这些操作（runbook 写明），前滚修复。
- **部分部署**：任一时刻只有已合并的层生效；AIH-TASK-213 之前没有路径产生 `CLOSED`。
- **监控与告警负责人**：Kelvin。不新增告警维度；关户后的迟到事件由现有的用量失败、支付复核（P1）、争议退款（P1）、outbox 积压告警覆盖。

## 9. 方案取舍

| 方案 | 优点 | 风险 | 未采用原因 |
| --- | --- | --- | --- |
| 采用：独立关户接口 + 只读检查接口；关户锁「钱包 → 租户」后普通读四类未结；`post_transaction` 锁内拒绝作兜底；各管理写入口锁租户后判；对账单按 `status_changed_at` 截止 | 与现有锁顺序一致；检查与提交之间漏看的写都有确定且无财务效果的去向；无迁移 | 关户时刻依赖「`CLOSED` 后不再 bump」这一约束 | — |
| 备选 A：扩展 `POST …/account-status` 接受 `CLOSED` | 少一个接口 | 前置条件与 5 个新错误码混进一个原本只有切换语义的接口；现有测试与文档钉住「`CLOSED` 作目标 422」（`test_account_status_api.py:354`、`docs/api.md` 账户状态一节）；前端确认流程完全不同 | 职责与错误语义不同 |
| 备选 B：未结检查用锁定读（`FOR SHARE`）锁住用量 / outbox / 事件的范围 | 检查后不会再出现新行 | 在 `(status, …)` 索引上加间隙锁，会挡住**所有租户**的摄取、AI outbox 写入与回调落库，直到关户提交 | 影响面远大于收益；漏看的行已有确定去向 |
| 备选 C：新增 `tenants.closed_at` 列（迁移 0040）并加 CHECK | 关户时刻语义明确、不依赖其他调用方的约束 | 一个迁移（含 CHECK 的形状检查、预检、降级）与模型、schema 文档改动；值与 `status_changed_at` 相同 | `status_changed_at` 在 `CLOSED` 后被四个调用方全部冻结且有测试钉住（§2）；若审查认为约束太脆，改用此方案（§10「请审查重点看」第 1 条） |
| 备选 D：计费 ② 改为锁定读租户 | 关户后不会再有 `PROCESSED`（包括零金额事件） | 计费变成「租户 → 钱包」，与 `post_transaction` 的「钱包 → 租户」相反，会死锁；或要改全局锁顺序 | 不安全；零金额事件没有财务效果（§10 已知限制 2） |
| 备选 E：只在各调用方判 `CLOSED`，`post_transaction` 不判 | 改动集中在服务层 | 计费 ② 是普通读，挡不住旧值窗口；以后新增的记账调用方会漏 | AIH-TASK-020 契约要求在记账总入口锁内拒绝 |
| 备选 F：前置条件加「集成凭据与 AI 密钥已全部吊销」 | 关户后不会再有用量落库 | 先吊销会让 AI outbox 投递永远 `SIGNING_CREDENTIAL_MISSING`、反过来挡住关户；关户后落库的用量本来就 `FAILED_FINAL` | 不改变财务结果，反而制造死结；放进 runbook 顺序 |
| 备选 G：后端也校验输入的客户名 | 防误调 API | 请求体多一个字段，与 Kelvin「前端要求」的范围不同；ADMIN + 2FA + `DISABLED` + 余额 0 的前置已足够防误关 | Kelvin 定为前端确认 |
| 备选 H：「若有活动」按活动判断关户当月是否出单 | 字面贴合 | `is_eligible` 是纯函数、被生成与告警共用；按活动判断要在两处查账本与用量，并定义「活动」 | 无发生额的那一份是全零对账单，没有信息损失，也是关户的收尾凭证；Kelvin 2026-11-14 选定一律出 |

## 10. 未决问题与假设

- **已知限制**：
  1. 关户后的争议与退款事件只关联、不能记账扣回（`post_transaction` 拒绝 `CLOSED`）；在 Stripe 与会计账上处理（runbook 第 8 步）。这是「不得静默重开」的直接结果。
  2. 关户事务提交前、计费 ② 已读到旧值的内部计量或零金额事件，会以 `PROCESSED`（无账本行）结束而不是 `FAILED_FINAL`。没有财务效果；只在毫秒级窗口内、且需要该租户恰好有在途的这类事件（关户前提要求用量全部终结，停用后集成方与 AI 调用都已被拒）。
  3. 退款扣回不校验「≤ 该支付的可退额」，也不结构化引用原支付（Kelvin 决定 4）：靠 runbook 要求在 Stripe 核实、原因里写支付号。
  4. 停在 `MODEL_UNKNOWN` / `PRICING_ERROR` / `FX_RATE_ERROR` 的用量事件挡住关户，V1 没有「人工判为终结」的出口：必须修好目录 / 价格 / 汇率后重新入队。若某个事件永远无法定价，这个客户就关不了户，需要另一个设计闸门加出口。请 Kelvin 知悉。
- **请审查重点看**：
  1. 关户时刻取 `status_changed_at`（§2「关户时刻为什么可以取 `status_changed_at`」）的论证是否足够；不足则改用备选 C（迁移 0040 加 `closed_at`），AIH-TASK-211 前插一个数据库层任务。
  2. 网关事件归属在 Python 里做、全库未结事件至多扫 10000 行、超限按未结处理（§2 契约三）。
  3. 关户事务在持有钱包与租户排他锁期间跑四次计数查询（§8「锁表与性能影响」）：同一租户的计费与调账在这段时间里排队。
- **尚未验证的假设**：
  1. InnoDB 在 REPEATABLE READ 下，事务里只做过锁定读时尚未建立一致性读快照，第一次普通读才建立（`portal_accounts.py` 的注释依赖同一事实）。若不成立，后果仅是「检查与提交之间」的窗口变大一点（从拿锁前开始），漏看的行仍走契约四，财务结果不变。
  2. 每个租户都有钱包（建档同事务创建）。不成立时关户 500，不写。
  3. 网关事件的 `metadata_json` 里 `client_reference_id`、`object_id`、`payment_intent` 是字符串（`app/tasks/payment_events.py` 按字符串用）。不是字符串的值被忽略，可能少算——但此时事件本身也会被处理成 `UNMATCHED`，不会影响该租户的钱。
  4. 管理端各面板（项目、凭据、AI 密钥、签名密钥、低余额阈值、内部计量）对 409 用现有的错误提示显示 `error.code` 与 `request_id`（例如 `ProjectsPanel.tsx:177` 的 `CustomerErrorAlert`）。若某面板吞掉 409，AIH-TASK-214 之外另开前端任务。
- **如果假设错误，具体后果**：见各条。

本稿没有未解决的阻断问题。

## 11. 审查与版本绑定

审查方要回答的五个问题常驻在 [scripts/review_checklist.md](../../scripts/review_checklist.md) 的「设计审查」一节，此处不重复。

**版本绑定规则**（规则的出处是 [WORKFLOW §3](../../docs/WORKFLOW.md)；这里保留一份是因为审查材料里只有本 Issue 正文，两处改一起改）：

- 批准必须写成 `APPROVED: design v<N>`，`<N>` 取本 Issue 顶部的「设计版本」
- **设计版本一变，之前的 APPROVE 自动作废**，必须重新过闸门
- 什么算「实质修改」：改了契约、表结构、事务边界、状态机、失败语义、不变量控制。改错别字不算
- 实质修改时：把顶部版本 +1，在下方追加一条变更说明，状态退回 `READY_FOR_REVIEW`

### 实现拆分

依赖串行：AIH-TASK-209 → AIH-TASK-210 → AIH-TASK-211 → AIH-TASK-212 → AIH-TASK-213 → AIH-TASK-214 → AIH-TASK-215。每个任务都不改 `.env*`、`.platform/`，不装依赖，不跑迁移。

**AIH-TASK-209** 关户第一层：记账总入口拒绝给已关户租户入账（层：业务规则·记账；预计 ~350 行）

- 可改路径：`app/repositories/wallet.py`、`app/services/usage_billing.py`、`app/services/wallet_adjustments.py`、`tests/backend/test_wallet_repository.py`、`tests/backend/test_usage_billing.py`、`tests/backend/test_wallet_adjustment_service.py`、`tests/backend/test_wallet_adjustment_api.py`、`docs/api.md`
- 验收要点：
  1. `TenantClosed(LedgerError)` 在 `_lock_tenant` 之后、余额范围判定与任何写入之前抛出；重放与冲突分支的位置不变
  2. 用量计费 ⑧ 捕获它 → `FAILED_FINAL` / `ACCOUNT_CLOSED`，`Outcome.FAILED_FINAL`，不进 `record_failure`
  3. 调账把它映射为 409 `ACCOUNT_CLOSED`（复用 `project_integration.AccountClosed`）
  4. 支付确认与人工入账的代码与测试都不改：`tests/backend/test_payment_confirm.py`、`tests/backend/test_payment_review.py` 里现有的 `TENANT_CLOSED` 用例照常通过，即证明锁内先判、走不到 `TenantClosed`
  5. §7 标 AIH-TASK-209 的各行
  6. `docs/api.md` 手工调账一节加 409 `ACCOUNT_CLOSED`

**AIH-TASK-210** 关户第二层：关户后拒绝建项目、建与轮换凭据、签发密钥、内部计量和阈值设置（层：业务规则·管理写入口；预计 ~450 行）

- 可改路径：`app/services/customers.py`、`app/services/integration_access.py`、`app/ai_api/admin.py`、`app/services/webhook_signing.py`、`app/services/internal_billing.py`、`tests/backend/test_customer_service.py`、`tests/backend/test_low_balance_threshold_api.py`、`tests/backend/test_integration_access_service.py`、`tests/backend/test_ai_admin_service.py`、`tests/backend/test_webhook_signing_service.py`、`tests/backend/test_status_version.py`、`docs/api.md`
- 验收要点：
  1. §2 契约二表中八个入口在锁住租户后判 `CLOSED`，409 `ACCOUNT_CLOSED`，不写（含审计）；锁顺序「租户 → 项目 / 凭据 / 密钥」或「钱包 → 租户」
  2. 判定顺序：主密钥 503 → 404 → `CLOSED` 409 → 其他业务 409；阈值与现值相同、签名密钥已 `ACTIVE` 的幂等路径也先 409
  3. 吊销凭据、吊销 AI 密钥、退役签名密钥、编辑资料对 `CLOSED` 照常
  4. MySQL 并发一条（建项目等关户的租户锁，用 `data_lock_waits` 确认）
  5. `docs/api.md` 各对应接口的错误表加 409 `ACCOUNT_CLOSED`

**AIH-TASK-211** 关户第三层：对账单资格加关户截止，关户后的账期不再生成也不算缺失（层：业务规则·对账单；预计 ~250 行）

- 可改路径：`app/services/statements.py`、`tests/backend/test_statement_generate.py`、`tests/backend/test_statements_task.py`、`tests/backend/test_usage_alerts.py`、`tests/backend/test_status_version.py`
- 验收要点：
  1. `is_eligible` 按 §2「对账单资格」的规则；`status_changed_at` 为空照常合格
  2. 关户当月（含无发生额）生成，之后不生成；`_missing_statements` 不算缺失；管理端补生成跳过
  3. 四个 `bump_status_version` 调用方在 `CLOSED` 租户上不改 `status_version` 与 `status_changed_at`（依赖 AIH-TASK-209、AIH-TASK-210）
  4. 已定稿对账单与生成函数的其他步骤不变

**AIH-TASK-212** 关户第四层：关户服务与锁内前置条件检查（层：业务规则·状态机；预计 ~950 行）

- 可改路径：`app/services/account_closure.py`、`app/services/account_status.py`、`tests/backend/test_account_closure_service.py`、`tests/backend/test_account_closure_concurrency.py`、`tests/backend/payment_world.py`
- 验收要点：
  1. `closure_blockers` 的四类查询、`EXPIRED` 7 天边界、网关事件四种引用的归属与扫描上限（上限可注入）
  2. `close_account` 的事务 0 / 事务 1、锁「钱包 → 租户」、判定顺序与五个新错误类（另复用 `AccountStatusTransitionInvalid`）
  3. `_record_transition` 从 `_apply` 原样抽出，`TRANSITIONS` 不变，`test_account_status_service.py` 不改仍全过
  4. 写入与 `change_account_status` 同样四样（状态、bump、`updated_at`、审计带原因），钱包 `version` 不变
  5. 已 `CLOSED` 时不写
  6. MySQL 并发三条（vs 计费、vs 调账、vs 启用），用 `data_lock_waits` 确认等待
  7. §7 标 AIH-TASK-212 的其余各行

**AIH-TASK-213** 关户第五层：管理端关户接口与关户检查接口（层：接口；预计 ~550 行）

- 可改路径：`app/api/admin_customers.py`、`app/schemas/customers.py`、`tests/backend/test_admin_account_closure_api.py`、`tests/backend/test_admin_customers_api.py`、`docs/api.md`
- 验收要点：
  1. `GET …/closure-check` 与 `POST …/close` 的路径、鉴权、请求体、响应字段、错误表与判定顺序照 §2「接口」
  2. `account-status` 接口不变（`CLOSED` 目标 422）
  3. 管理端路由鉴权集合补两个接口
  4. `docs/api.md` 新增「管理端关户」一节，并改「管理端客户管理」接口表与「明确不做」里关于关户的旧说明（`docs/api.md:1155`、`:1272`）

**AIH-TASK-214** 关户第六层：管理端客户详情的关户按钮与确认弹窗（层：前端；预计 ~850 行）

- 可改路径：`frontend/src/api/adminCustomers.ts`、`frontend/src/api/adminCustomers.test.ts`、`frontend/src/features/customers/CloseAccountModal.tsx`、`frontend/src/features/customers/CloseAccountModal.test.tsx`、`frontend/src/features/customers/CustomerDetailPage.tsx`、`frontend/src/features/customers/CustomerDetailPage.test.tsx`、`frontend/src/i18n/locales/en.json`、`scripts/acceptance/admin_customers.mjs`
- 验收要点：
  1. 接口封装与错误类照 §2「前端」
  2. 按钮只在 `DISABLED`；弹窗读检查接口、blockers 非空不可确认、Refresh
  3. 原因 1–255 码点；客户名去首尾空白后完全相同才可确认
  4. 成功后详情与列表过期；409 显示码与 request_id 并重读；5xx「Try again」原样重发
  5. 文案走 i18n，满足 `no-hardcoded-jsx-text` 与 `keys.test.ts`；不请求 `/api/v1/customer/` 路径
  6. 浏览器验收步骤 `check_account_closure`：在 login 之后打开公司名以 `[TEST] Acceptance Fixture` 开头的夹具客户详情；后端值取页面自己发出的 `GET /api/v1/admin/customers/{id}` 响应（被动观察）；断言「Close account」按钮出现当且仅当 `account_status` 为 `DISABLED`；不点任何按钮、不请求 `closure-check`；文件头步骤说明同步；已有步骤行为不变；沿用验收脚本已有的约束
- 浏览器验收：`acceptance.browser`，`writes: false`，步骤 `login`、`check_account_closure`，`timeout_seconds: 300`

**AIH-TASK-215** 关户第七层：关户 runbook、数据库契约说明与 TODO 勾选（层：文档；预计 ~250 行）

- 可改路径：`docs/runbook.md`、`docs/database-schema.md`、`docs/payment-flow.md`、`docs/TODO.md`
- 验收要点：
  1. `docs/runbook.md` 新增「关户」一节，内容照 §2「runbook」八步
  2. `docs/database-schema.md`：账户状态契约表的 `CLOSED` 列改为已实现的规则；`bump_status_version` 一节加「新调用方必须拒绝 `CLOSED`」；关户时刻 = `status_changed_at`
  3. `docs/payment-flow.md` §7 加一句：关户后的争议与退款不能记账扣回，见 runbook「关户」
  4. `docs/TODO.md` Phase 4 勾选「关户」并写设计闸门号；§66 映射补 `DISABLED → CLOSED`

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
| v1 | 2026-11-14 | 初稿 | — |
| v2 | 2026-11-14 | ① §7「并发：关户 vs 计费」的钩子挪到 `closure_blockers` 之后，事件在 A 的检查之后才插入（v1 的写法下 B 的事件在 A 检查时已是 `PROCESSING`，A 得 409，测不到要测的窗口）；② `get_closure_check` 改为返回服务层的 `ClosureCheck` 数据类，定义它的字段与 `codes` 顺序，接口层 `ClosureCheckView` 由它转换；③ 无钱包时 `closure-check` 也是 500；④ `PAYMENT_EVENT_SCAN_LIMIT` 由函数在调用时读模块属性，测试以 monkeypatch 注入；⑤ 前端写明 `closure-check` 加载中与失败时确认按钮不可点及显示内容 | v1 批准后的登记前预审（AIH-TASK-212 `NOT_READY`：一条 DESIGN_DEFECT、两条 UNDECIDED；213、214 各一条 UNDECIDED） |
| v3 | 2026-11-14 | §6「审计」：§66 没有关户动作，`DISABLED → CLOSED` 写明没有对应的 §66 动作名（v2 写「对应 §66 的租户关停」，§66 并无此项） | v2 批准后的最终预审（AIH-TASK-215 一条 DESIGN_DEFECT） |

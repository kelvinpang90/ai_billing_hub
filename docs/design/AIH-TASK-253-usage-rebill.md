# AIH-TASK-253..259 设计：Phase 8 Rebill —— 已计费用量按更正版本重算，差额以补退 / 补扣账本行入账（已批准 v6）

> **来源**：设计闸门 Issue #830。本文件是 `APPROVED: design v6` 那一版正文的**逐字副本**（送审时状态行为 `READY_FOR_REVIEW`）。审查：Codex，五轮 REQUEST_CHANGES 后 v6 批准（读视图、提交结果不明、零计费事件的范围、降级保护与续跑）。产品决定（9 条）由 Kelvin 2026-11-23 拍板。
> 放进仓库，是因为 OpenClaw Worker 在沙箱里不联网、读不到 GitHub Issue。
> **实现以本文件为准**；与 Issue 不一致时，以 Issue #830 上被批准的 v6 为准。设计要改，就回到 Issue 升版本、重新过闸门，不要直接改本文件。
> **任务划分**（登记时调整，设计内容不变）：§11 把前端整层记为 AIH-TASK-257，按 12 条可改路径的上限拆成 AIH-TASK-257（列表与详情）与 AIH-TASK-260（新建页与用量事件详情）；§8 的降级协议（改名、检查、续跑）从 AIH-TASK-253 拆出为 AIH-TASK-261（253 只做「有行就拒绝」的简单保护，261 合并前没有写入方）。管理端 Rebill 列表的分页照用量事件列表的 page / page_size（§2「管理端接口」写的「游标分页」是笔误，管理端没有游标分页的先例）。

状态：`READY_FOR_REVIEW` · 设计版本：`v6` · 目标 PR：待开（登记 PR 在本设计批准后开）
负责人：Claude
对应需求：spec §19「Rebill / Reprocess」、§62「Reprocessing UI」、§66 审计动作 `REBILL`、§16（更正规则保留新旧引用）、§131「Reprocess」「Rebill adjustments」、§46 / REQ-PERIOD-001（定稿对账单不可变、跨期 rebill 记为上期调整）

## 0. 准入判定

本任务档位：**全部**。改钱包账本（新增 `REBILL_CREDIT` / `REBILL_DEBIT` 的写入方）、计价（按更正版本重算）、幂等（确认请求的幂等键与预览指纹），并新增两张只追加的财务表。

## 1. 目标与边界

### 要解决的问题

已计费（`PROCESSED`）的用量事件发现算错了钱时，今天没有任何纠正途径：

- 事件行被迁移 0018 的触发器 `trg_usage_events_processed_immutable` 锁死，不能改。
- 价格版本与定价规则都不许回溯发布（#177 / #178 的裁定，`docs/design/AIH-TASK-026-provider-prices.md`「不许回溯」、`AIH-TASK-027-pricing-rules.md`「回溯生效只允许全局默认的第一条」），所以「过去用错了价」时，无法补一个覆盖过去的版本，让计费自己选到它。
- `REBILL_CREDIT` / `REBILL_DEBIT` 两种账本类型与 `ReferenceType.REBILL` 已定义（`app/models/wallet.py:58-112`），从没有写入方；对账单的 `prior_period.rebill` 一直是 0（`app/services/statements.py:534`）。
- 管理员唯一的手段是手工调账（AIH-TASK-011），但它不记录原事件、原金额、更正金额与所用规则，满足不了 spec §19 的审计要求。

### Kelvin 2026-11-23 的决定（9 条建议全部采纳）

1. **范围**：只纠正已计费的预付事件（`status = PROCESSED`、`billing_mode_snapshot = 'PREPAID'`）。未计费的错误事件继续走现有的重新入队（`app/services/usage_requeue.py`）。内部计量租户不做 Rebill；已关户的租户照关户规则拒绝。
2. **按什么重算**：三种方式。修 bug 后用原版本重算；指定更正定价规则；指定更正供应商价格版本（只影响 MARKUP 规则）。不许手填金额。更正版本必须已审批发布过（`PUBLISHED` 或 `RETIRED`），不看它的生效区间；汇率沿用原快照。
3. **流程**：先预览（旧计费、新计费、差额），再确认（必填原因、带幂等键）；预览之后数据变了返回 409，要求重新预览。
4. **批量**：按客户加日期范围筛选（项目、供应商、模型可选），一次最多 1000 条，同一组更正参数，差额为 0 的事件跳过。
5. **账本粒度**：一次 Rebill 只写一行 `REBILL_CREDIT` 或 `REBILL_DEBIT`，逐事件的明细写进更正明细表。
6. **重复更正与补扣**：同一事件可以更正多次，每次以「原计费 + 历次差额」为基准；补扣可以让余额变负、照常触发停机（余额 ≤ 0），补退照常触发复机（余额 > 0），确认页写明。
7. **对账单**：快照结构不改；Rebill 照现有规则按入账月计入 `prior_period.rebill`；原事件、原账本行、原账期的引用存在更正明细里，管理端可查；客户侧不发邮件。
8. **毛利分析**：把更正差额算进用量分析的收入（单独一层任务）。
9. **命名**：接口与模块叫 rebill，不用 spec §89 的 `/reprocess`（与重新入队区分；`tests/backend/test_usage_requeue_api.py:713-721` 钉住管理端路径不含 `reprocess`）；新增审计动作 `REBILL`（spec §66）。

### 我定的（未单独问，审查者重点看，§10 也列出）

1. **一次 Rebill 只属于一个客户**：请求必须带 `customer_id`。这样一次 Rebill 正好对应一个钱包、一行账本，`(REBILL, <rebill public_id>)` 天然唯一，锁也只有一个钱包。
2. **超过 1000 条直接拒绝**（422 `REBILL_SELECTION_TOO_LARGE`），不截断：截断会让管理员以为整段日期都更正了。
3. **差额相抵为 0 时拒绝**（409 `REBILL_NET_ZERO`）：账本行金额不能为 0（`app/models/wallet.py` 的符号规则），而让明细存在却没有账本行会破坏「每次 Rebill 恰有一行账本」。需要时管理员把补退与补扣分两次做。
4. **更正价格版本必须与原快照同币种**（`source_currency` 相同）：汇率沿用原快照，换币种就没有可用的汇率。
5. **「原版本重算」用当前有效的版本**：同一事件已被更正过时，用最近一次更正所用的规则与价格版本，而不是事件原快照的那组；没被更正过就是原快照。这样重算的基准与「原计费 + 历次差额」一致。
6. **估算成本不改**：更正价格版本算出的新估算成本存在明细里供管理端查看，但用量分析的成本、对账后成本都仍以事件原快照为准（spec §17 第 631 行：成本快照不得被静默覆盖）。
7. **客户门户的用量页不变**：仍显示事件处理时的计费额；更正只体现在钱包流水（`REBILL_*` 行）与对账单的 Rebill 一栏。
8. **不加开关**：功能只对管理员开放，每次都要预览加确认，合并即上线。

### 可观察的完成标准

1. 管理员选一个客户、一段 `occurred_at` 日期范围（或一组事件），指定更正规则 / 价格版本，或什么都不指定（按当前有效版本重算），预览看到每个事件的原计费、新计费、差额与合计。
2. 确认后客户钱包恰好多一行 `REBILL_CREDIT`（多扣退回）或 `REBILL_DEBIT`（少扣补扣），金额等于合计差额的相反数；事件行与原 `AI_USAGE` 行一个字节不变。
3. 更正明细逐事件保存：原账本行、原金额、更正金额、新旧规则与价格版本、原账期、当时已定稿的原对账单、操作人、原因、时间。
4. 同一幂等键重发返回同一结果；预览之后有别的 Rebill 或参数变化，确认返回 409 `REBILL_PREVIEW_STALE`。
5. 补扣让余额 ≤ 0 时，计费状态照现有规则停机（`SUSPENDED`，正好为 0 也算）；补退让余额 > 0 时照常复机（`ACTIVE`），同一事务。
6. 当月对账单的 `prior_period.rebill` 等于当月入账的 Rebill 行之和（现有代码，不改）。
7. 用量分析的收入与毛利包含更正差额。
8. 管理端用量事件详情显示这一事件的更正历史与当前有效计费。

### 明确不做什么

- 不改事件行、原 `AI_USAGE` 行、已定稿对账单的任何一列。
- 不做未计费事件的重算：那是重新入队。
- 不做内部计量租户的「参考价更正」。
- 不做汇率更正（`FX_RATE_ERROR` 事件走重新入队；已计费事件的汇率沿用原快照）。
- 不做按金额手填的更正（那是手工调账）。
- 不做跨客户的批量；不做定时或自动 Rebill。
- 不改对账单快照结构、PDF、门户用量页与客户通知。
- 不改供应商成本对账（AIH-TASK-242..）：对账仍不触发 Rebill（spec §17 第 631 行）。
- 不做 Rebill 的撤销：撤销 = 再做一次 Rebill，把版本改回去。

### 现有行为与证据

- 事件快照：`usage_events` 的 `provider_price_version_id`、`pricing_rule_id`、`fx_rate_version_id`、`provider_source_currency`、`fx_rate_applied`、`estimated_provider_cost_myr`、`billable_cost`、`billing_mode_snapshot`、`wallet_transaction_id`（`app/models/usage.py:288-316`）；`PROCESSED` 行不可改（迁移 0018 触发器）。计费额为 0 时没有账本行（`PROCESSED_LEDGER_CHECK`）。
- 重算：`app/services/pricing_engine.py:512` 的 `reprice_from_snapshot(session, ChargeSnapshot, quantities)` 用快照引用的版本、汇率与规则调用纯函数 `price_event`，今天没有调用方。数量由 `app/services/usage_billing.py:278` 的 `_quantities(event)` 按计量形态取出。
- 规则范围：八级 `priority_scope`，每级对 `tenant_id` / `project_id` / `provider_id` / `model_id` 的取值由 `SCOPE_COLUMNS_CHECK` 钉住（`app/models/pricing_rules.py`）；MARKUP = 估算成本 × 倍数，FIXED_RATE = 分量价（同文件 :143）。
- 账本：`post_transaction`（`app/repositories/wallet.py:419`）锁钱包 → 按 `(reference_type, reference_id)` 查重（同效果返回 `replayed`）→ 锁租户 → `CLOSED` 抛 `TenantClosed` → 余额范围 → 写行、计费状态跃迁（停机 / 复机）、低余额事件，都在调用方事务里。余额可以为负（只查 `fits_money`）。`metadata_json` 不许出现 `cost` 等键（同文件 :80-91）。`REBILL` 行不会自动写调账审计，原因与操作人也不受 CHECK 约束（同文件 :500-504）。
- 对账单：`REBILL_*` 行按 `created_at` 的马来西亚月归属，计入 `prior_period.rebill`（`docs/design/AIH-TASK-154-monthly-statements.md`「账本归属规则」与快照字段表；`app/services/statements.py:82,534`）。PDF 已有 Rebill 一行（`app/services/statement_pdf.py`）。
- 前端已能显示 `REBILL_CREDIT` / `REBILL_DEBIT` 与 `REBILL` 来源（`frontend/src/features/portal/CustomerTransactionsTable.tsx:44-46`、`frontend/src/features/wallet/WalletTransactionsPanel.tsx:62`）。
- 用量分析的收入是 `prepaid_billable_cost`（`app/repositories/usage_analytics.py:228`），按事件维度与 `occurred_at` 分组。
- 审计动作是非原生枚举（`app/models/auth.py:181`），新增值不需要迁移；实体类型登记在 `app/services/audit_query.py:41` 的 `PUBLIC_ENTITY_TYPES`。
- 最新迁移：`20261117_0041_provider_cost_reconciliations.py`（revision `0041_provider_cost_recon`）。

## 2. 设计概要

### 数据流

```text
管理员 → POST /admin/rebills/preview（只读事务）
       → 选事件（客户 + 条件，≤ 1000）→ 每个事件取当前有效计费与版本
       → 校验更正版本是否适用 → 按更正版本重算 → 差额 ≠ 0 的列成明细 + 预览指纹
管理员 → POST /admin/rebills（同参数 + 指纹 + 原因 + 幂等键）
       → 锁钱包 → 幂等查重 → 在锁内重新选事件、重算 → 指纹比对
       → post_transaction(REBILL_CREDIT / REBILL_DEBIT)（锁租户、停复机、低余额事件）
       → 写 usage_rebills + usage_rebill_lines → 审计 REBILL → 提交
```

### 口径（写进 `docs/api.md` 与页面）

- **当前有效计费** `eff(e)`：事件 `e` 最近一条更正明细（按明细 `id` 最大）的 `corrected_charge`；没有明细时是 `e.billable_cost`。恒有 `eff(e) = e.billable_cost + Σ 该事件明细的 difference`（测试钉住）。
- **当前有效版本** `refs(e)`：最近一条明细的 `corrected_pricing_rule_id`、`corrected_provider_price_version_id`；没有明细时是事件快照的 `pricing_rule_id`、`provider_price_version_id`。汇率版本始终是事件快照的 `fx_rate_version_id`。
- **更正版本**：请求里的 `corrective_pricing_rule_id`、`corrective_provider_price_version_id`，各自可空；空的那一项取 `refs(e)` 里的对应项。两项都空 = 「按当前有效版本重算」。
- **新计费** `new(e)`：`price_event(数量 = _quantities(e), 价格分量 = 更正价格版本, 汇率 = 快照汇率, 规则 = 更正规则)` 的 `billable_cost`（8 位小数，计价引擎已有的舍入，一次）。
- **差额** `d(e) = new(e) − eff(e)`；`d(e) = 0` 的事件不进明细。
- **合计差额** `D = Σ d(e)`；账本金额 `= −D`：`D < 0`（多扣了）写 `REBILL_CREDIT`，金额 `−D > 0`；`D > 0`（少扣了）写 `REBILL_DEBIT`，金额 `−D < 0`。
- **原账期** `original_period`：原 `AI_USAGE` 行按对账单「账本归属规则」的归属月 `t(r)`（`occurred_at` 所在马来西亚月，晚于该月 cut-off 才入账的取 `created_at` 所在马来西亚月）；计费额为 0、没有账本行的事件取 `occurred_at` 所在马来西亚月。格式 `YYYY-MM`。
- **原对账单** `original_statement_id`：确认时这个客户、这个原账期已存在的 `monthly_statements` 行（有就是已定稿，生成即定稿）；没有为空。

### 更正版本的适用性（`app/services/rebill_pricing.py`，AIH-TASK-254）

对每个选中的事件逐一判断，任一事件不适用就整个请求失败（不部分执行），错误带第一个不适用事件的 `public_id`：

- 更正价格版本 `v`：`v.status ∈ {PUBLISHED, RETIRED}`；`v.provider_id = e.provider_id` 且 `v.model_id = e.model_id`；`v.source_currency = e.provider_source_currency`；`v` 的分量覆盖事件计量类型需要的全部分量（`_covering`）。否则 422 `REBILL_PRICE_NOT_APPLICABLE`。
- 更正规则 `r`：`r.status ∈ {PUBLISHED, RETIRED}`；`r.tenant_id`、`r.project_id`、`r.provider_id`、`r.model_id` 各自为空或等于事件的对应值（这正是该规则的范围能覆盖这个事件的条件，八级 `priority_scope` 由 `SCOPE_COLUMNS_CHECK` 保证列的取值）；FIXED_RATE 规则的分量覆盖需要的分量。否则 422 `REBILL_RULE_NOT_APPLICABLE`。
- **不看生效区间**：更正版本的意义就是「过去那一刻本该用它」，而不许回溯发布让它不可能在过去生效。批准流程仍在：版本必须先经草稿 → 发布（`approved_by` / `approved_at` 非空，`APPROVED_CHECK`）。
- 结果超出 `DECIMAL(20,8)`（`ChargeOverflow`）：422 `AMOUNT_OUT_OF_RANGE`。

`rebill_pricing.py` 只读、不加锁，提供：`effective_state(session, events) -> dict[event_id, EffectiveState]`（`eff`、`refs`、最近明细 id）、`check_corrections(session, events, price_version, rule)`、`corrected_charge(session, event, state, price_version, rule) -> ChargeResult`。重算复用 `pricing_engine` 的 `price_event`、`_covering`、`_rule_terms`、`_meter_components`（把这几个以公开名导出，或在 `pricing_engine` 里新增 `reprice_with(session, snapshot, quantities)` 并让 `reprice_from_snapshot` 调用它，二选一由实现决定，行为必须与 `reprice_from_snapshot` 对同一组引用逐位相同）。

### 选事件

请求的 `selection` 二选一：

- `{"usage_event_ids": [...]}`：1 到 1000 个不重复的事件 `public_id`。任一不存在 404 `USAGE_EVENT_NOT_FOUND`；任一不属于该客户、不是 `PROCESSED`、或 `billing_mode_snapshot ≠ 'PREPAID'`，409 `USAGE_EVENT_NOT_REBILLABLE`。
- `{"occurred_from", "occurred_to", "project_id"?, "provider"?, "model"?}`：`occurred_at ∈ [from, to)`（RFC 3339 带时区，换成 UTC，`from < to`，否则 422 `VALIDATION_ERROR`）；`project_id` 是该客户的项目 `public_id`（不属于该客户 404 `PROJECT_NOT_FOUND`）；`provider` / `model` 比目录的 `code`（已计费事件都有目录引用）。只选 `tenant_id = 客户`、`status = PROCESSED`、`billing_mode_snapshot = 'PREPAID'` 的事件，按 `id` 升序；匹配数 > 1000 时 422 `REBILL_SELECTION_TOO_LARGE`（响应带匹配数），= 0 时 409 `NOTHING_TO_REBILL`。

客户不存在：404 `CUSTOMER_NOT_FOUND`；客户 `billing_mode = INTERNAL_METERED_ONLY`：409 `REBILL_NOT_PREPAID`。

### 预览指纹

`preview_token = sha256` 十六进制（64 位），输入是下列内容的规范 JSON（键排序、无空白、金额用 8 位小数的定点字符串）：客户 `public_id`、两个更正版本的 `public_id`（空为 `null`）、`selection` 原样、以及按事件 `id` 升序的 `[事件 public_id, eff(e), new(e), 最近明细 id 或 0]` 列表（包括差额为 0 的事件）。它不是秘密，只用来比较：确认时服务端在钱包锁内用同样的输入重算，不一致就 409 `REBILL_PREVIEW_STALE`。这挡住三种情况：预览之后有别的 Rebill 改了同一事件；匹配集合变了（新事件被计费进来）；请求参数与预览时不同。

**一次 Rebill 的对象 = 预览展示的那组事件**（v3，#830 第二轮审查）。条件选事件（日期范围等）只是帮管理员**找出**这组事件的手段，不是「确认时刻该范围内的全部事件」的承诺。指纹保证确认事务处理的集合与预览展示的集合逐个相同（事件、基准、新计费、最近明细）。在确认事务的读视图建立**之后**才提交为 `PROCESSED` 的事件——计费额为 0 的预付事件不经 `post_transaction`、不拿钱包锁（`app/services/usage_billing.py:408`），可能在 Rebill 持锁期间提交；计费额大于 0 的事件在 Rebill 持锁期间等钱包，在 Rebill 之后提交——都不在本次 Rebill 里，与「预览之后才计费的事件不在本次范围」是同一件事：它们由下一次预览发现，再做一次 Rebill。这不会少扣或多扣：本次只改它明细里列出的事件，没列出的事件保持原计费，管理员看到的预览就是将要入账的全部内容。页面与 runbook 写明：做 Rebill 时 `occurred_to` 应取已过 T+1 cut-off 的时刻，避免与仍在计费的用量交错（只是建议，不强制）。

### 数据库（迁移 0042，AIH-TASK-253）

`alembic/versions/20261124_0042_usage_rebills.py`，revision `0042_usage_rebills`，down_revision `0041_provider_cost_recon`。

**`usage_rebills`**（一次 Rebill 一行）：

| 列 | 类型 | 说明 |
| --- | --- | --- |
| `id` | BIGINT PK | |
| `public_id` | CHAR(36) | 唯一；也是账本行的 `reference_id` |
| `tenant_id` | FK `tenants.id` | NOT NULL |
| `idempotency_key` | CHAR(36) | 唯一，小写 UUID |
| `request_fingerprint` | CHAR(64) | 规范请求（客户、更正版本、selection、`preview_token`、原因）的 sha256，幂等重放时比对 |
| `corrective_pricing_rule_id` | FK `pricing_rules.id` | 可空 |
| `corrective_provider_price_version_id` | FK `provider_price_versions.id` | 可空 |
| `event_count` | INT | 1..1000（明细行数） |
| `previous_total` | DECIMAL(20,8) | Σ eff(e)，≥ 0 |
| `corrected_total` | DECIMAL(20,8) | Σ new(e)，≥ 0 |
| `difference` | DECIMAL(20,8) | `corrected_total − previous_total`，≠ 0 |
| `wallet_transaction_id` | FK `wallet_transactions.id` | NOT NULL，唯一 |
| `reason` | VARCHAR(255) | NOT NULL，去首尾空白后非空 |
| `created_by` | FK `users.id` | NOT NULL |
| `created_at` | DATETIME | 整秒 UTC |

CHECK：`event_count BETWEEN 1 AND 1000`；`previous_total >= 0`；`corrected_total >= 0`；`difference = corrected_total - previous_total`；`difference <> 0`。索引 `(tenant_id, id)`。

**`usage_rebill_lines`**（一次 Rebill 的逐事件明细）：

| 列 | 类型 | 说明 |
| --- | --- | --- |
| `id` | BIGINT PK | |
| `rebill_id` | FK `usage_rebills.id` | NOT NULL |
| `usage_event_id` | FK `usage_events.id` | NOT NULL |
| `original_wallet_transaction_id` | FK `wallet_transactions.id` | 可空（计费额为 0 的事件没有原账本行） |
| `previous_charge` | DECIMAL(20,8) | eff(e)，≥ 0 |
| `corrected_charge` | DECIMAL(20,8) | new(e)，≥ 0 |
| `difference` | DECIMAL(20,8) | `corrected_charge − previous_charge`，≠ 0 |
| `previous_pricing_rule_id` / `previous_provider_price_version_id` | FK | NOT NULL，refs(e) |
| `corrected_pricing_rule_id` / `corrected_provider_price_version_id` | FK | NOT NULL |
| `corrected_estimated_provider_cost_myr` | DECIMAL(20,8) | ≥ 0，只给管理端看 |
| `original_period` | CHAR(7) | `YYYY-MM` |
| `original_statement_id` | FK `monthly_statements.id` | 可空 |
| `created_at` | DATETIME | 与所属 Rebill 相同 |

CHECK：三个金额的非负与等式、`difference <> 0`。`original_period` 的格式由服务层保证（与迁移 0041 的 `billing_month CHAR(7)` 同一做法，不在数据库里校验月份格式）。唯一 `(rebill_id, usage_event_id)`；索引 `(usage_event_id, id)`（取最近明细）。

**只追加**：两张表各一对无条件的 `BEFORE UPDATE` / `BEFORE DELETE` 触发器，`SIGNAL SQLSTATE '45000'`，只在 MySQL 上建，写法照迁移 0041（SQLite 不建，测试里的只追加断言放在 MySQL 分支）。外键都是 `RESTRICT`。不改任何已有表。

### 服务（`app/services/usage_rebill.py`，AIH-TASK-255）

`preview_rebill(session_factory, *, request) -> RebillPreview`：一个只读事务（不加锁），按上面的口径返回明细（只含 `d ≠ 0` 的事件）、`unchanged_count`、四个合计、账本方向（`REBILL_CREDIT` / `REBILL_DEBIT` / 无）、余额预估（当前余额 − D，只作提示）与 `preview_token`。不写库、不写审计。

`confirm_rebill(session_factory, *, actor, request, preview_token, reason, idempotency_key, context, clock) -> RebillResult`，**两个事务，先后进行**：

**读视图的约定**（v2，#830 第一轮审查）：MySQL 默认 `REPEATABLE READ` 下，一个事务的读视图在它的**第一次普通读**时建立，之后不再刷新；拿到行锁不会让它看到在那之后才提交的数据。所以：

- 锁外的校验放在**单独的一个只读事务**里做完并结束，不与确认事务共用读视图。
- 确认事务的**第一条语句**是锁钱包的 `SELECT … FOR UPDATE`，之前不执行任何普通读。所有在它之前提交的 Rebill（它们都持有过同一把钱包锁）都在这之后建立的读视图里。
- 决定金额基准的两次读——幂等查重、取各事件最近一条更正明细——用**加锁读**（`FOR SHARE`）。加锁读总是读最新已提交的版本，与读视图无关，作为第二道保证。

步骤：

0. **校验事务**（只读，结束后才开确认事务）：客户存在（404 `CUSTOMER_NOT_FOUND`）、是预付（409 `REBILL_NOT_PREPAID`），取客户内部 id。客户的 id 与 `billing_mode` 的单向性（`PREPAID → INTERNAL_METERED_ONLY` 要求钱包为空、没有用量，`app/services/internal_billing.py:85-107`）保证这一步的结论在确认事务里仍然成立；确认事务第 2 步再用锁内读到的租户行复核一次 `billing_mode`。
1. **确认事务**开始；第一条语句：按客户内部 id `SELECT … FOR UPDATE` 锁钱包行（与 `post_transaction` 内部的锁是同一行，同事务再锁不阻塞）。**同一客户的所有 Rebill 由此串行**。
2. 读租户行（锁后的普通读），`billing_mode ≠ PREPAID` → 409 `REBILL_NOT_PREPAID`。
3. 按 `idempotency_key` 加锁读（`FOR SHARE`）`usage_rebills`：存在且 `request_fingerprint` 相同 → 返回原结果，`replayed = true`，什么都不写；存在但不同 → 409 `REBILL_CONFLICT`（与手工调账的 `ADJUSTMENT_CONFLICT` 同一叫法）。
4. 在锁内重新选事件、取 `effective_state`（最近明细用 `FOR SHARE` 加锁读）、校验更正版本、重算、算指纹；指纹 ≠ `preview_token` → 409 `REBILL_PREVIEW_STALE`。**幂等判定（第 3 步）在指纹比对之前**：同键重发时状态早已被第一次改变，必须先按幂等键返回原结果，而不是报过期。明细为空 → 409 `NOTHING_TO_REBILL`；`D = 0` → 409 `REBILL_NET_ZERO`。
5. 生成 Rebill 的 `public_id`；`post_transaction(tenant_id, REBILL_CREDIT 或 REBILL_DEBIT, amount = −D, reference_type = REBILL, reference_id = public_id, now, created_by = actor.id, description = "Rebill correction (<N> usage events)", metadata = {"rebill": public_id, "events": N}, actor_role, ip, user_agent)`。`TenantClosed` → 409 `ACCOUNT_CLOSED`（什么都不写）；`InvalidAmount("BALANCE_OUT_OF_RANGE")` → 422 `AMOUNT_OUT_OF_RANGE`。
6. 写 `usage_rebills`（`wallet_transaction_id` = 刚写的账本行）与 N 行 `usage_rebill_lines`。
7. `record_audit(action = REBILL, entity_type = "usage_rebill", entity_id = public_id, before_state = {"previous_total", "event_count"}, after_state = {"corrected_total", "difference", "wallet_transaction": 账本行 public_id, "corrective_pricing_rule", "corrective_provider_price_version", "selection"}, reason)`。审计里有金额（客户计费额，不是成本），没有估算成本。
8. 提交。`IntegrityError` 且是 `idempotency_key` 唯一冲突（另一个事务先提交了同一个键）→ 回滚后开一个新事务重查一次，按第 3 步判定；其他 `IntegrityError` 照常抛出。

`rebill_pricing.effective_state` 因此有一个 `locking: bool` 参数：预览传 `False`（普通读），确认传 `True`（`FOR SHARE`）。

所有时间取 `clock()` 截到整秒，在钱包锁**之后**取（与账本 `created_at` 的既有约定一致：对账单按 `created_at` 归属，锁后取值让 Rebill 行的 `created_at` 不早于它所看到的最新状态）。

### 管理端接口（`app/api/admin_rebills.py`，AIH-TASK-256）

每个处理函数第一条语句是 `require_admin(request)`（与 `app/api/admin_usage_events.py` 同一约定）。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/v1/admin/rebills/preview` | 体：`customer_id`、`selection`、`corrective_pricing_rule_id?`、`corrective_provider_price_version_id?`（都是 `public_id`）。200：`lines[]`（`usage_event_id`、`occurred_at`、`project`、`provider`、`model`、`previous_charge`、`corrected_charge`、`difference`、`previous_pricing_rule`、`corrected_pricing_rule`、`previous_provider_price_version`、`corrected_provider_price_version`、`corrected_estimated_provider_cost_myr`、`original_period`、`original_statement_id`）、`unchanged_count`、`event_count`、`previous_total`、`corrected_total`、`difference`、`ledger_type`、`ledger_amount`、`balance`、`balance_after`、`preview_token` |
| POST | `/api/v1/admin/rebills` | 体：预览的全部字段 + `preview_token`（64 位小写十六进制）+ `reason`（1..255，去首尾空白后非空）+ `idempotency_key`（小写 UUID）。201：Rebill 详情；重放 200，`replayed: true` |
| GET | `/api/v1/admin/rebills` | `customer_id?`、游标分页（照用量事件列表的写法），按 `id` 倒序：`id`、`customer_id`、`event_count`、`difference`、`ledger_type`、`ledger_amount`、`reason`、`created_by_email`、`created_at` |
| GET | `/api/v1/admin/rebills/{id}` | 列表字段 + 两个更正版本 + `wallet_transaction_id` + 全部明细 |

金额一律是定点字符串（`money_text`）。用量事件详情 `GET /api/v1/admin/usage-events/{id}` 增加 `effective_charge` 与 `rebills[]`（`rebill_id`、`created_at`、`previous_charge`、`corrected_charge`、`difference`，按明细 `id` 升序）；不增加处理函数（`tests/backend/test_usage_requeue_api.py:695` 钉住的 4 个处理函数不变）。

错误码汇总：404 `CUSTOMER_NOT_FOUND` / `USAGE_EVENT_NOT_FOUND` / `PROJECT_NOT_FOUND` / `PRICING_RULE_NOT_FOUND` / `PRICE_VERSION_NOT_FOUND`（复用价格版本接口的同名错误码） / `REBILL_NOT_FOUND`；409 `REBILL_NOT_PREPAID` / `USAGE_EVENT_NOT_REBILLABLE` / `NOTHING_TO_REBILL` / `REBILL_NET_ZERO` / `REBILL_PREVIEW_STALE` / `REBILL_CONFLICT` / `ACCOUNT_CLOSED`；422 `VALIDATION_ERROR` / `REBILL_SELECTION_TOO_LARGE` / `REBILL_PRICE_NOT_APPLICABLE` / `REBILL_RULE_NOT_APPLICABLE` / `AMOUNT_OUT_OF_RANGE`。

### 管理端页面（AIH-TASK-257）

- 新路由 `/rebills`（列表，可按客户筛）与 `/rebills/new`（新建），侧栏加「Rebills」入口；`/rebills/:id` 详情。
- 新建页：选客户 → 选「事件列表」或「日期范围」（项目、供应商、模型可选）→ 选更正规则与价格版本（可都不选，提示「按当前有效版本重算」）→「预览」显示明细表与合计、账本方向与余额变化；当前余额 > 0 而 `balance_after ≤ 0` 时显示醒目提示「确认后余额不大于 0，客户将停机」；当前余额 ≤ 0 而 `balance_after > 0` 时提示「确认后客户将复机」→ 填原因 →「确认」。确认返回 409 `REBILL_PREVIEW_STALE` 时提示重新预览并清空确认按钮。幂等键在进入确认步骤时生成一次，重试沿用。
- 用量事件详情页：`PROCESSED` 的预付事件显示「Rebill」按钮，跳到新建页并预填客户与该事件；显示当前有效计费与更正历史。
- 文案进 `frontend/src/i18n/locales/en.json`，不硬编码。

### 用量分析（AIH-TASK-258）

`app/repositories/usage_analytics.py` 的 `_aggregates` 里，`prepaid_billable_cost` 改为 `Σ (billable_cost + 该事件全部明细 difference 之和)`：左连接按 `usage_event_id` 聚合的明细子查询，空为 0。分组、合计、导出（CSV / xlsx 经同一仓储）一起变。对账后成本的分摊（`reconciled_allocation_inputs`）只用估算成本，不变。`docs/api.md` 用量分析一节写明「收入含 Rebill 差额，按原事件的维度与 `occurred_at` 归属」。

### 事务边界

- 预览：一个只读事务，不加锁。
- 确认：一个事务包住锁钱包、查重、重算、账本行（含停复机与低余额 outbox 行）、两张表、审计；任何一步失败整体回滚，什么都不留。

### 时间语义

- 选事件按 `occurred_at`（`[from, to)`，UTC 比较；页面按马来西亚时间输入，换成 UTC 再发）。
- 更正版本不看生效区间（见「适用性」）。
- Rebill 行的 `created_at` = 确认时刻（锁后取值、整秒）；对账单按它的马来西亚月归属。
- `original_period` 按原账本行的归属月（见口径）。

### 金额与精度

- 新计费由计价引擎在 `PRICING_CONTEXT` 里算出并舍入一次（8 位小数，`ROUND_HALF_UP`），与计费同一套代码。
- 差额、合计、账本金额都是 8 位小数定点值的精确加减，不再舍入。
- 全程 `Decimal`，不出现浮点（沿用计价引擎的静态检查）。
- 币种：只有 MYR（钱包币种）；更正价格版本与原快照同源币种，汇率沿用快照。

## 3. 不变量影响矩阵

| 不变量 | 是否触碰 | 可能怎样被破坏 | 设计控制 | 验证方式 |
| --- | --- | --- | --- | --- |
| INV-1 中心故障不中断 AI | 否 | Rebill 只在管理端触发，不在 AI 调用或用量摄取路径上 | 不改 `app/ai_api/`、摄取与计费 | 现有测试不变 |
| INV-2 事件不重复扣费 | **是** | 同一更正被确认两次；两次 Rebill 都以同一个旧基准算差额 | 幂等键唯一 + 请求指纹；钱包锁串行同一客户的 Rebill；锁内重算指纹，含最近明细 id | 并发与重放测试（§7） |
| INV-3 支付不重复入账 | 否 | 不碰支付 | — | — |
| INV-4 余额只经账本变动 | **是** | 绕过 `post_transaction` 改余额 | 唯一的财务效果是 `post_transaction` 写的一行 `REBILL_*`；余额由触发器推进 | 服务测试断言余额变化 = 账本金额 |
| INV-5 历史账本不可变 | **是** | 改原 `AI_USAGE` 行或原明细 | 只追加；新表只追加触发器；原行不动 | 迁移测试：UPDATE / DELETE 被拒；服务测试：原行逐列不变 |
| INV-6 事件保留版本引用 | **是** | 更正让事件的原引用丢失 | 事件行不改；新旧引用都存在明细里（spec §16） | 测试：Rebill 后事件快照逐列不变，明细的 previous / corrected 引用正确 |
| INV-7 客户不可见成本毛利 | **是** | 明细的估算成本、规则倍数漏到客户侧 | 新接口只在 `/admin`；账本 `metadata` 只有 Rebill id 与事件数（`FORBIDDEN_METADATA_KEYS` 兜底）；门户只看到账本行 | `tests/backend/test_internal_cost_visibility.py` 加新路由；门户流水测试断言无成本字段 |
| INV-8 租户不可互访 | **是** | 选到别的客户的事件；更正规则是别的客户的 | 选事件以 `tenant_id = 客户` 过滤；显式事件列表逐个校验归属；规则 `tenant_id` 为空或等于事件的 | 测试：别的客户的事件 409、别的客户的规则 422 |
| INV-9 对话内容不入库 | 否 | 不处理 prompt / response | — | — |
| INV-10 金额用 Decimal | **是** | 浮点进入差额或合计 | 全程 `Decimal`，复用计价引擎 | 静态检查用例覆盖新模块 |
| INV-11 event_id 至多一次财务效果 | **是** | Rebill 让一个事件「多一次扣费」 | INV-11 管的是摄取与计费路径：`(USAGE_EVENT, event_id)` 仍只有一行。Rebill 是管理员显式发起、带审计的补偿，来源是 `(REBILL, rebill public_id)`；同一事件的多次更正以「原计费 + 历次差额」为基准，累计财务效果恒等于 `eff(e)`，不会叠加重复 | 测试：两次更正后 `−(AI_USAGE + Σ REBILL 份额) = eff(e)` |
| INV-12 定稿对账单不可变 | **是** | 更正改写已定稿的那一期 | 不碰 `monthly_statements`；Rebill 行按 `created_at` 进开放期的 `prior_period.rebill`；原对账单只被引用 | 测试：Rebill 后原对账单逐列不变；下一期快照的 `prior_period.rebill` 等于 Rebill 行金额 |
| INV-13 状态与事件原子提交 | **是** | 账本写了、明细没写；停机没随余额变负发生 | 一个事务；停复机与低余额 outbox 在 `post_transaction` 内 | 测试：在写明细时注入失败，账本、余额、审计、outbox 全部回滚 |
| INV-14 队列丢失不毁持久工作 | 否 | 不用 Celery；同步请求 | 停复机产生的状态推送 outbox 行沿用现有周期恢复 | — |

## 4. 状态与并发

### 跃迁表

Rebill 本身没有状态（只追加的事实记录）。它引起的状态变化全部来自 `post_transaction` 的既有规则：

| 当前状态 | 事件 | 前置条件 | 新状态 | 副作用 | 非法时结果 |
| --- | --- | --- | --- | --- | --- |
| 计费状态 `ACTIVE` | `REBILL_DEBIT` 让余额 ≤ 0 | 预付、未关户 | `SUSPENDED` | 状态推送 outbox、低余额事件（越过阈值时） | — |
| 计费状态 `SUSPENDED` | `REBILL_CREDIT` 让余额 > 0 | 同上 | `ACTIVE` | 状态推送 outbox | — |
| 账户状态 `CLOSED` | 任何 Rebill | — | 不变 | 无 | 409 `ACCOUNT_CLOSED`，什么都不写 |
| 事件 `PROCESSED` | Rebill | 预付 | 不变（事件行不改） | 新增明细 | — |
| 事件非 `PROCESSED` | Rebill | — | 不变 | 无 | 409 `USAGE_EVENT_NOT_REBILLABLE`（显式列表）/ 不被选中（条件） |

（余额 > 0 为 `ACTIVE`、≤ 0 为 `SUSPENDED`，由 `app/repositories/wallet.py:817` 的 `_apply_billing_status` 按 `billing_transition` 判定；已在目标状态时不跃迁、不写事件。本设计不改它。）

### 并发

- **同一客户的两次 Rebill**：都先锁钱包行，串行。后一个的确认事务在拿到锁之前没有任何普通读，读视图在锁之后才建立，最近明细又是加锁读，所以必然看到前一个已提交的明细，最近明细 id 变了 → 指纹不符 → 409 `REBILL_PREVIEW_STALE`（见 §2「读视图的约定」）。若两次选中的事件不重叠且匹配集合不变，指纹仍相同，后一个照常成功（两者互不影响，正确）。
- **Rebill 与用量计费**：计费的锁顺序是 事件 → 供应商（S）→ 汇率锁（S）→ 规则锁（S）→ 钱包（X）→ 租户（X）（`usage_billing.py:18-22`）。Rebill 只拿 钱包（X）→ 租户（X），不锁事件与版本行，所以不会成环。计费在 Rebill 持锁期间等钱包；它新计费的事件在 Rebill 之后提交，若落在 Rebill 的条件范围内，下一次预览才能看到。计费额为 0 的预付事件不拿钱包锁，可能在 Rebill 持锁期间提交，同样不在本次 Rebill 里（§2「预览指纹」末段：一次 Rebill 的对象 = 预览展示的那组事件）。Rebill 读的事件都是 `PROCESSED`（不可改），版本与规则的分量不可改，所以不需要计费那几把共享锁。
- **Rebill 与手工调账、支付入账、关户**：都经 `post_transaction` 锁 钱包 → 租户，同一顺序，串行。关户先提交时 Rebill 在 `post_transaction` 里拿到 `TenantClosed`。
- **Rebill 与对账单生成**：对账单按账本高水位划分（`ledger_high_sequence`），Rebill 行是普通账本行，在生成之前提交就进这一期、之后提交就进下一期，与其他行同一规则。
- **同一幂等键并发**：唯一约束让第二个插入失败，回滚后重查走重放或冲突（第 8 步）。由于钱包锁串行，第二个事务通常在第 3 步就查到。
- **数据库保证的唯一性**：`usage_rebills.public_id`、`usage_rebills.idempotency_key`、`usage_rebills.wallet_transaction_id`、账本的 `(reference_type, reference_id)`、`usage_rebill_lines (rebill_id, usage_event_id)`。
- **幂等键**：`idempotency_key`（客户端生成的小写 UUID）。相同键、不同请求 → 409 `REBILL_CONFLICT`。重试只在客户端（同键重发）；服务端不自动重试，除第 8 步的一次重查。

## 5. 失败与恢复矩阵

| 失败点 | 对外结果 | 数据最终状态 | 是否可重试 | 恢复来源 | 告警 |
| --- | --- | --- | --- | --- | --- |
| 校验失败（参数、适用性、超 1000） | 404 / 409 / 422 | 什么都没写 | 改参数后重试 | — | 无 |
| 预览之后状态变了 | 409 `REBILL_PREVIEW_STALE` | 什么都没写 | 重新预览后确认 | — | 无 |
| 客户已关户 | 409 `ACCOUNT_CLOSED` | 什么都没写 | 否 | — | 无 |
| 余额超出 `DECIMAL(20,8)` | 422 `AMOUNT_OUT_OF_RANGE` | 什么都没写 | 否 | — | 无 |
| 提交前失败（语句报错、提交被拒） | 500 | 事务回滚，什么都没写 | 同幂等键重发：照常执行 | 幂等键 | 现有 5xx 日志 |
| 提交时或提交后连接中断、响应丢失 | 500 或客户端超时 | **两种之一，调用方无法区分**：已完整提交（账本行、两张表、审计都在），或已完整回滚（什么都没有）；不会有一半 | 必须用**同一幂等键**重发：已提交 → 200 `replayed: true` 返回原结果（幂等判定先于指纹比对，见 §2 第 4 步）；已回滚 → 照常执行。**不得换新幂等键直接确认**：要换键必须先重新预览，新预览已反映第一次是否提交（页面与 runbook 写明） | 幂等键 | 现有 5xx 日志 |
| 锁等待超时 / 死锁 | 500（`OperationalError` 不重试，与手工调账同一做法，`app/services/wallet_adjustments.py:14`） | 回滚 | 同键重发 | 幂等键 | 现有 5xx 日志 |
| Redis/Celery 丢失 | 无影响（同步请求） | 停复机的状态推送 outbox 行已随事务提交 | — | 现有 outbox 周期恢复（INV-14） | 现有 |
| 外部服务超时 | 不适用（不调外部服务） | — | — | — | — |
| 重复请求（同键同参） | 200 `replayed: true` | 不变 | — | — | 无 |
| 重复请求（同键异参） | 409 `REBILL_CONFLICT` | 不变 | 否 | — | 无 |

## 6. 数据与安全边界

- **鉴权**：只有 `ADMIN` 会话（已强制 2FA）；每个处理函数第一条语句 `require_admin`。客户门户没有任何 Rebill 接口。
- **租户过滤**：选事件、校验显式事件、校验规则 `tenant_id`、校验项目归属都在服务层；仓储函数都带 `tenant_id` 参数。
- **客户侧禁止返回**：估算成本、规则倍数与分量价、明细。客户侧只看到 `REBILL_*` 账本行（类型、金额、余额、`description`、时间）；`metadata` 只有 Rebill id 与事件数。
- **日志**：服务与接口不写含金额的日志；异常只带错误码与事件 `public_id`。
- **审计**：`REBILL` 一条，含客户计费额与差额、更正版本的 `public_id`、selection、原因；不含估算成本。
- **密钥**：不涉及。
- **prompt / response**：不涉及。
- **数据保留**：两张表是财务记录，按 `docs/data-governance.md` 的财务记录口径保留（与账本同）；只追加，不删除。

## 7. 测试证据计划

| 风险/需求 | 测试层级 | 场景 | 预期结果 |
| --- | --- | --- | --- |
| 正常路径：补退 | integration | 3 个事件原计费 1.50 / 0.80 / 0.70，更正规则倍数降低使新计费 1.20 / 0.64 / 0.56 | 一行 `REBILL_CREDIT +0.60`；余额 +0.60；3 行明细；审计 1 条；事件与原账本行逐列不变 |
| 正常路径：补扣 | integration | 更正价格版本单价更高 | 一行 `REBILL_DEBIT`，金额 = −D；余额减少 |
| 原版本重算 | unit + integration | 不给更正版本，修改前后的计价函数一致 | 明细为空 → 409 `NOTHING_TO_REBILL`；（用桩让 `price_event` 返回不同值时）按差额入账 |
| 多次更正 | integration | 同一事件先降后升两次 Rebill | 第二次以第一次的 `corrected_charge` 为基准；`eff(e) = billable_cost + Σ difference`；第二次「原版本重算」用第一次的更正版本 |
| 差额为 0 跳过 | integration | 5 个事件里 2 个差额为 0 | 明细 3 行，`unchanged_count = 2`，指纹仍包含 5 个事件 |
| 相抵为 0 | integration | 一个补退 0.10、一个补扣 0.10 | 409 `REBILL_NET_ZERO`，什么都不写 |
| 计费额为 0 的事件 | integration | 原计费 0、无原账本行，更正后 > 0 | 明细 `original_wallet_transaction_id` 为空，`original_period` = `occurred_at` 的马来西亚月 |
| 适用性 | unit | 价格版本：别的模型、别的币种、DRAFT、DISCARDED、缺分量；规则：别的客户、别的项目、别的模型、DRAFT、FIXED_RATE 缺分量；RETIRED 与生效区间不覆盖事件时刻 | 前五 / 前五种 422 对应错误码并带事件 id；RETIRED 与区间不覆盖照常可用 |
| 选事件边界 | integration | 条件匹配 1000 条成功、1001 条 422 并带匹配数；0 条 409；`[from, to)` 左闭右开；内部计量事件与非 PROCESSED 事件不被选中 | 如述 |
| 显式事件 | integration | 别的客户的事件、未处理事件、内部计量事件、不存在的 id、重复 id | 409 / 409 / 409 / 404 / 422 |
| 指纹 | integration | 预览后另一个 Rebill 改了同一事件；预览后改了请求参数；预览后有新事件被计费进条件范围 | 都是 409 `REBILL_PREVIEW_STALE` |
| 并发（MySQL） | integration | 两个线程对同一客户、同一事件各自确认（不同幂等键、同一预览）：A 拿到钱包锁后暂停；B 开始确认，用 `performance_schema.data_lock_waits` 确认 B 确实在等钱包锁（CLAUDE.md 的约定）；A 提交 | B 得到 409 `REBILL_PREVIEW_STALE`；只有一行账本、一组明细 |
| 并发（MySQL） | integration | Rebill 持钱包锁时，同一客户另一个事件的计费 | 计费等待后成功；最终余额 = 初始 − 计费 + Rebill 份额 |
| 幂等重放 | integration | 同键同参重发；同键异参；两个事务同键并发 | 200 `replayed`；409 `REBILL_CONFLICT`；一个 201 一个 200 |
| 事务中途失败 | integration | 在写明细时注入异常 | 账本行、余额、审计、outbox、Rebill 行全部不存在；随后同幂等键重发照常执行，结果只有一行账本 |
| 提交后响应丢失 | integration | 第一次确认已提交（模拟响应丢失：服务调用返回后丢弃结果）；之后又有别的 Rebill 改了同一客户的状态；再用同一幂等键、同一预览指纹重发 | 200 `replayed: true`，返回第一次的结果；账本仍只有一行（不是 409 `REBILL_PREVIEW_STALE`） |
| 读视图（MySQL） | integration | 捕获确认事务在该连接上执行的语句（SQLAlchemy `before_cursor_execute`） | 事务的第一条语句是钱包的 `FOR UPDATE`；取最近明细与幂等查重的语句是加锁读（只在 `mysql` 分支断言加锁子句的字面量，CLAUDE.md「写后端测试的约定」） |
| 停复机 | integration | 补扣让余额从正变为 ≤ 0（含正好为 0）；补退让停机客户余额变为 > 0 | 同事务里状态变化与状态推送 outbox 行，与 `post_transaction` 既有测试同一断言 |
| 关户 | integration | 客户已 `CLOSED` | 409 `ACCOUNT_CLOSED`，什么都不写 |
| 对账单 | integration | 原事件在 10 月（10 月对账单已定稿），11 月确认 Rebill | 10 月对账单逐列不变；明细 `original_statement_id` 指向它；11 月快照 `prior_period.rebill` = Rebill 行金额 |
| 租户越权 | API | 门户会话调用四个新路由 | 401 / 403；`test_internal_cost_visibility.py` 覆盖新路由 |
| 金额精度 | unit | 新计费在第 9 位小数四舍五入的边界；差额与合计为 8 位小数精确值 | 与 `price_event` 的结果逐位相同 |
| 只追加 | migration | MySQL 上对两张表 UPDATE / DELETE；CHECK 违例（差额 ≠ 新 − 旧、差额 0、事件数 0 与 1001、负金额） | 被拒 |
| 降级保护 | migration | 空表时 `downgrade` 到 0041；插入一组 Rebill 与明细（MySQL 上触发器只拦 UPDATE / DELETE，插入正常）后 `downgrade` | 空表：两张表与触发器都没了；非空：抛出 `RuntimeError`，两张表以原名存在、触发器与行全部仍在、`alembic_version` 仍是 0042 |
| 降级与确认并发（MySQL） | migration | 另一个连接开事务、向两张表各插入一行但不提交；此时在后台线程执行 `downgrade`，用 `performance_schema.metadata_locks`（`LOCK_STATUS = 'PENDING'`、`OBJECT_NAME = 'usage_rebills'`、按 `DATABASE()` 过滤）确认降级确实在等元数据锁；然后提交那个事务 | 降级抛 `RuntimeError`；两张表以原名存在，那两行都在；`alembic_version` 仍是 0042。另一个变体：那个事务回滚而不是提交 → 降级成功、两张表都没了 |
| 降级中断后续跑 | migration | ① 打桩让第 1 步改名之后抛异常（模拟进程退出），再执行一次 `downgrade`，表为空 / 非空各一次；② 打桩让第 3 步删完表之后、`alembic_version` 更新之前抛异常，再执行一次 `downgrade`；③ 手工造出「一张原名、一张 `_downgrading` 名」后执行 `downgrade`；④（MySQL）第 3 步的语句是一条同时列出两张表的 `DROP TABLE`（捕获执行的语句断言），执行后 `information_schema.TRIGGERS` 里没有这两张表的触发器 | ① 空：续跑删完、版本变为 0041；非空：改回原名并抛 `RuntimeError`，行都在、版本仍是 0042；② 续跑什么都不删、版本变为 0041；③ 抛「unexpected state」，两张表原样不动；④ 如述 |
| 降级后的确认 | migration | 降级第 1 步改名之后（用打桩让第 2 步之前暂停）发起一次确认 | 确认在插入时报错并整体回滚：没有账本行、余额不变 |
| 零计费并发（MySQL） | integration | 客户有事件 a（计费额 > 0，已 `PROCESSED`）与事件 b（预付、计费额为 0，`RECEIVED`），都在条件范围内。预览只含 a。Rebill 确认事务拿到钱包锁并建立读视图后暂停；此时计费处理 b 并提交（不等钱包锁）；Rebill 继续并提交 | Rebill 成功，明细只有 a；b 的快照与状态不受影响；再预览同一条件，b 出现在明细里（更正后计费额 > 0 时） |
| 用量分析 | integration | Rebill 后按客户、按月分组与合计、CSV 导出 | 收入 = 原计费 + 差额；成本不变；毛利随之变 |
| Redis/Celery 丢失 | 不适用 | 同步请求不经队列 | — |

四个变异必须变红：删掉确认时的指纹比对（并发测试变红）；把 `eff(e)` 改为总用 `billable_cost`（多次更正测试变红）；在确认事务锁钱包之前加一次普通读、同时把最近明细改为普通读（并发测试变红：B 的读视图建立在 A 提交之前）；把幂等判定挪到指纹比对之后（「提交后响应丢失」测试变红）。

## 8. 迁移与上线

- 迁移 0042 只新建两张表与触发器，不改已有表、不回填。
- **`downgrade` 只在两张表都为空时执行，且检查与删表之间不留并发窗口**（v3 起；v4 按 #830 第三轮审查补上并发）。步骤：
  1. 一条语句同时改名：`RENAME TABLE usage_rebills TO usage_rebills_downgrading, usage_rebill_lines TO usage_rebill_lines_downgrading`。MySQL 的 `RENAME TABLE` 要拿两张表的排他元数据锁：正在写它们的确认事务持有共享元数据锁直到提交或回滚，改名会等它们结束；改名之后，新的确认事务找不到 `usage_rebills`，在插入时报错并整体回滚（账本行也随之回滚，见 §2「事务边界」），不会再有行写进来。触发器随表改名。
  2. 在改名后的两张表上 `SELECT 1 … LIMIT 1`。任一有行：改回原名（同样一条 `RENAME TABLE`），抛 `RuntimeError("usage_rebills is not empty: downgrading would delete rebill records (spec §19); restore from backup or keep this revision")`，什么都不删。
  3. 都为空：**一条**语句同时删两张表 `DROP TABLE usage_rebill_lines_downgrading, usage_rebills_downgrading`（v6，#830 第五轮审查）。MySQL 8.0 起 InnoDB 支持原子 DDL：一条 `DROP TABLE` 列出的多张 InnoDB 表要么全删、要么全不删，表上的触发器随表一并删除（生产与 CI 都是 `mysql:8.4`，`docker-compose.yml:140`、`.github/workflows/ci.yml:108`）。所以不存在「只删了一张」的中间状态，也不需要单独删触发器。
  **可续跑**（v5，#830 第四轮审查）：MySQL 的 DDL 各自隐式提交，三步之间进程退出不会自动回滚。所以 `downgrade` 开头先看当前表名，决定从哪一步开始：
  - 两张表都以原名存在、都没有 `_downgrading` 名：从第 1 步开始。
  - 两张表都以 `_downgrading` 名存在、都没有原名（上次在第 1 步之后中断）：跳过第 1 步，从第 2 步继续。
  - 原名与 `_downgrading` 名都不存在（上次在第 3 步删完表之后、`alembic_version` 更新之前中断）：什么都不用删，直接结束（触发器已随表删除）。
  - 其他任何组合（例如一张原名、一张 `_downgrading` 名，或同名表两种都在）：抛 `RuntimeError("usage_rebills tables are in an unexpected state: …")`，列出实际存在的表名，什么都不做。第 1 步与恢复都是**一条** `RENAME TABLE` 同时改两张表，第 3 步是**一条** `DROP TABLE` 同时删两张表，三者在 MySQL 里都是原子的，所以任何一步中断都只会停在上面三种状态之一，不会产生这种组合；它只可能来自人工改动，此时由人按 runbook 查看并处理，迁移不猜测。
  中断期间（表在 `_downgrading` 名下、`alembic_version` 仍是 0042），管理端 Rebill 接口报 500（表不存在）、不会写入任何东西（见第 1 步）；其他功能不受影响。恢复方式写进 runbook：要么重新执行 `alembic downgrade 0041_provider_cost_recon`（按上面的判断续跑：空表就删完，非空就改回原名并报错）；要么放弃降级，手工执行一条 `RENAME TABLE usage_rebills_downgrading TO usage_rebills, usage_rebill_lines_downgrading TO usage_rebill_lines` 回到原状。
  SQLite（只在测试里用）没有元数据锁，按同样的三步执行（`ALTER TABLE … RENAME TO`），只验证空 / 非空两条分支。有了真实 Rebill 之后，0042 就是不可降级的修订：它保存的逐事件金额、版本与原交易引用是 spec §19 要求的审计链，账本里的 `REBILL_*` 行离开它就无法解释。
- 上线：合并即部署，功能只对管理员可见，不需要开关（§1「我定的」第 8 条）。
- 上线后第一次使用前，按 runbook 新增的「Rebill」一节先在测试专用验收租户上做一次小额补退再补扣还原，核对账本、余额、审计与用量分析。
- 回滚：没有 Rebill 时可降级；有了之后只回滚应用代码、不降级迁移（新表对旧代码无害：旧代码不读它们）。runbook 写明：不得手工删除这两张表的行，也不得为了降级而清表。

## 9. 方案取舍

| 方案 | 优点 | 缺点 | 结论 |
| --- | --- | --- | --- |
| **采用**：只追加的 Rebill 与明细表，一次一行账本，按更正版本重算 | 原记录不动；新旧引用齐全；客户流水简洁；同一客户串行 | 多两张表；事件的「当前计费」要从明细推导 | 采用 |
| 允许回溯发布更正版本，再重新计费 | 计费路径不变 | 推翻 #177 / #178 的裁定；改写历史选择；已计费事件仍不可改 | 否 |
| 逐事件一行 `REBILL_*` 账本 | 账本与事件一一对应 | 1000 条就是 1000 行客户流水；账本 `reference_id` 要逐事件生成 | 否（Kelvin 第 5 条） |
| 直接改事件快照 | 查询简单 | 违反 INV-5 / INV-6，要拆触发器 | 否 |
| 手填更正金额 | 灵活 | 没有规则引用，满足不了 spec §19 的审计；与手工调账重复 | 否（Kelvin 第 2 条） |
| 跨客户批量 | 一次修完一个全局价格错误 | 一次请求要锁多个钱包、写多行账本，失败面大 | 否：按客户逐个做 |

## 10. 未决问题与假设

我定的（§1 已列，请审查者重点看）：

1. 一次 Rebill 只属于一个客户。
2. 超过 1000 条拒绝而不截断。
3. 补退与补扣相抵为 0 时拒绝。
4. 更正价格版本必须与原快照同源币种。
5. 「原版本重算」用当前有效版本（最近一次更正的），不是事件原快照。
6. 估算成本不改，用量分析的成本仍以原快照为准。
7. 门户用量页不变。
8. 不加开关。

假设：

- 对账单 `prior_period.rebill` 把当月入账的所有 Rebill 都算作「上期调整」一栏，包括原事件就在当月的情况。字段名不完全贴切，但 Kelvin 第 7 条定了不改快照结构；PDF 上这一行的标签是「Rebill」。
- 停复机、低余额与状态推送完全沿用 `post_transaction`，本设计不新增通知。

## 11. 审查与版本绑定

### 实现拆分

七层串行，每层合并后都能单独通过检查与 CI：

| 任务 | 内容 | 主要文件 | 预计行数 |
| --- | --- | --- | --- |
| AIH-TASK-253 | 数据库：迁移 0042、两个模型、只追加触发器、`docs/database-schema.md` | `alembic/versions/20261124_0042_usage_rebills.py`、`app/models/usage_rebills.py`、`alembic/env.py`、迁移测试 | ~450 |
| AIH-TASK-254 | 规则与读：当前有效状态、更正版本适用性、按更正版本重算、选事件、列表与详情的只读仓储 | `app/repositories/usage_rebills.py`、`app/services/rebill_pricing.py`、`app/services/pricing_engine.py`（只加导出或 `reprice_with`）、测试 | ~700 |
| AIH-TASK-255 | 服务：预览与确认、钱包锁、幂等、指纹、账本、审计动作 `REBILL`、并发测试 | `app/services/usage_rebill.py`、`app/models/auth.py`、`app/services/audit_query.py`、`frontend/src/api/adminAudit.ts`、测试 | ~900 |
| AIH-TASK-256 | 管理端接口与用量事件详情的更正历史、`docs/api.md` | `app/schemas/usage_rebills.py`、`app/api/admin_rebills.py`、`app/main.py`、`app/api/admin_usage_events.py`、`app/schemas/usage_events.py`、测试 | ~800 |
| AIH-TASK-257 | 管理端页面：列表、新建（预览 + 确认）、详情、用量事件详情的按钮与历史 | `frontend/src/api/adminRebills.ts`、`frontend/src/features/rebills/*`、`frontend/src/features/usage/UsageEventDetailPage.tsx`、路由、侧栏、`en.json` | ~1000 |
| AIH-TASK-258 | 用量分析收入含 Rebill 差额 | `app/repositories/usage_analytics.py`、测试、`docs/api.md` | ~250 |
| AIH-TASK-259 | 收尾：runbook「Rebill」一节、勾选 Phase 8 的 Reprocess 与 Rebill 调整 | `docs/runbook.md`、`docs/TODO.md` | ~200 |

具体的可改路径、验收标准与命令在登记 PR 的任务契约里定；设计与契约冲突时以本设计为准。

### 设计闸门判定

本设计碰钱包账本、计价与幂等，档位「全部」，批准前不写代码。批准后登记 253..259，按控制面的预审规则逐个预审。

## 12. 版本变更记录

| 版本 | 日期 | 变更 |
| --- | --- | --- |
| v1 | 2026-11-23 | 初稿 |
| v2 | 2026-11-23 | #830 第一轮审查（Codex）两个阻断项：① `REPEATABLE READ` 下锁前的普通读会让锁后的读看不到刚提交的 Rebill —— 确认拆成「校验事务 + 确认事务」，确认事务第一条语句是锁钱包，幂等查重与最近明细改为加锁读，新增读视图测试与变异；② 失败矩阵把「提交时连接中断」写成全部回滚 —— 改为「已完整提交或已完整回滚」两种结果，写明必须同键重发、幂等判定先于指纹比对，补「提交后响应丢失」测试 |
| v3 | 2026-11-23 | #830 第二轮审查（Codex）两个阻断项：① 计费额为 0 的预付事件不拿钱包锁，可能在 Rebill 持锁期间提交、不在本次 Rebill 里 —— 写明「一次 Rebill 的对象 = 预览展示的那组事件」、之后才计费的事件由下一次预览发现，§4 并发同步，补零计费并发测试；② 迁移降级会删掉更正明细 —— `downgrade` 只在两张表都为空时执行，否则抛错，补降级保护测试，回滚改为只回滚代码 |
| v4 | 2026-11-23 | #830 第三轮审查（Codex）阻断项：降级的空表检查与删表之间，并发确认可能提交 Rebill，随后被删表 —— 降级改为先用一条 `RENAME TABLE` 改名（等正在写的事务结束、挡住新的写入），再在改名后的表上检查，有行就改回原名并抛错；补「降级与确认并发」「降级后的确认」两条测试 |
| v5 | 2026-11-23 | #830 第四轮审查（Codex）阻断项：降级改名之后进程中断，表停在 `_downgrading` 名下、无法恢复 —— `downgrade` 开头按实际表名判断从哪一步续跑，写明中断期间的表现与 runbook 的两种恢复方式，异常组合直接拒绝；补「降级中断后续跑」测试 |
| v6 | 2026-11-23 | #830 第五轮审查（Codex）阻断项：降级第 3 步若分两条语句删表，中间中断会只剩一张表 —— 改为一条 `DROP TABLE` 同时删两张表（MySQL 8 原子 DDL，触发器随表删除），续跑分支相应简化；测试加④断言一条语句删两表 |

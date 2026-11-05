# AIH-TASK-154..162 设计：月度对账单、T+1 cut-off 与上期调整（已批准 v6）

> **来源**：设计闸门 Issue #565。本文件是 `APPROVED: design v6` 那一版正文的**逐字副本**（六轮均由 Claude 审查：v1 被要求修改，有界扫描假设 `created_at ≥ occurred_at`；v2 被要求修改，数据缺陷下的恢复方式走不通，改为 Kelvin 拍板的已知限制；v3 曾获批准，之后 AIH-TASK-158 预审查出缺失告警会一直 P2、遮住同维度的 P1，改为只报 24 小时（v4，Kelvin 拍板），批准随之作废；v4 被要求修改，快照缺 ADR-0008 §5 的税务说明；v5 曾获批准，之后 AIH-TASK-158 预审查出 §5 把生成后才提交的行误写成由晚到告警兜住，改为 v6，批准随之作废；v6 审查通过，见 §12。送审时状态行为 `READY_FOR_REVIEW`，批准后 Issue 上只把状态行改成了 `APPROVED: design v6`）。
> 放进仓库，是因为 OpenClaw Worker 在沙箱里不联网、读不到 GitHub Issue。
> **实现以本文件为准**；与 Issue 不一致时，以 Issue 上被批准的版本为准。设计要改，就回到 Issue 升版本、重新过闸门，不要直接改本文件。
> **前置**：ADR-0003（财务期间与 cut-off）；账本 `wallet_transactions` 与 `post_transaction`（AIH-TASK-008 起）；用量计费的 `processed_at` 与 `wallet_transaction_id`（AIH-TASK-032）；告警维度 `usage_processing_backlog`（AIH-TASK-046）。
> §11「任务拆分」的九个实现任务登记为 `AIH-TASK-154` → `155` → `156` → `157` → `158` → `159` → `160` → `161` → `162`，按这个顺序依赖。

---
## 0. 准入判定

| 任务涉及 | 要填的范围 |
| --- | --- |
| 钱包 / 账本 / 定价 / 汇率 / 支付 / 幂等 / 状态机 | **全部章节** |
| 用量摄取 / 认证与会话（集成侧与平台侧）/ Webhook | §1–§7（**§6 不可省**） |
| 前端 / 文档 / CI / 脚本 | **不走本闸门**，直接开 PR |
| 既不碰钱、也不碰用量摄取 / 认证与会话 / Webhook 的后端改动 | **不走本闸门**，直接开 PR |

本任务档位：`全部`

理由：对账单是财务快照：读账本算期初、期末余额与各类发生额，按 T+1 cut-off 判定上期调整，定稿后不可变（INV-12）；生成要对每个租户与账期幂等，并带一个小状态机（`DRAFT` / `FINALIZED` / `VOID`）。不写账本、不改 `post_transaction`、不改计费。

---

状态：`READY_FOR_REVIEW`
负责人：Claude
**设计版本**：`v6`
对应需求：spec §46、§65、§74.8、§113、§114、§130「monthly statements」「T+1 cut-off and prior-period adjustments」；ADR-0003；Invariant 12
目标 PR：待开（登记 PR）；实现为 AIH-TASK-154..162

## 1. 目标与边界

- **要解决的问题**：spec §46 要求每月自动生成并定稿上月对账单，按 T+1 cut-off 区分本期用量与上期调整；今天没有对账单表、没有生成任务、客户和管理员都看不到月度对账单。
- **可观察的完成标准**：
  1. 每个预付租户（`billing_mode = PREPAID`）每个账期（马来西亚日历月）恰好一份 `FINALIZED` 对账单，在新月第 2 日 01:00（马来西亚时间）之后的第一次整点任务里生成；任务没跑成，下一次整点补上。
  2. 对账单的期初余额 + 本期各类发生额 = 期末余额，精确到账本精度（8 位小数）；上期对账单的期末余额 = 本期对账单的期初余额。
  3. 每一行账本恰好进一份对账单：在 cut-off 之后才入账的上月用量出现在当前开放期的对账单里，标为上期调整并注明原用量期间；已定稿的对账单不因此改变。
  4. 定稿的对账单在数据库层拒绝任何 UPDATE 与 DELETE。每份对账单引用 cutoff 时刻生效的税务政策版本，并把该版本的税务说明原文冻结进快照（ADR-0008 §5、spec §45.1 `REQ-TAX-001`）：以后改措辞不改已定稿的对账单。
  5. 管理员能按客户与账期查看对账单，能对某个客户手动补生成；客户在门户能看到自己的对账单列表与详情，看不到估算或对账后的供应商成本、加价、毛利。
  6. 某期对账单到期后 6 至 30 小时内仍缺失、或最近 24 小时晚到用量占比达到 1%，`usage_processing_backlog` 维度报 P2。
- **明确不做什么**（Kelvin 2026-11-04 定的范围，见 §10「已拍板」）：
  - 不做 PDF 下载与「从快照重新渲染文件」（Phase 7 后续批）；不做收入、成本、毛利分析与导出（Phase 7 后续批）。
  - 只给 `PREPAID` 租户出对账单；内部计量租户（`INTERNAL_METERED_ONLY`）不出。
  - 不做人工确认的草稿环节：生成即定稿；`VOID` 只作为状态值存在，不做作废流程与更正版本。
  - 不回补：第一个账期是 `2026-12`（部署后第一个完整月），之前的月份不生成。
  - 出单后不发邮件、不写门户通知。
  - 不写账本、不改 `post_transaction`、不改计费与用量摄取、不加 `PRIOR_PERIOD_ADJUSTMENT` 账本类型（上期调整是对账单上的**归类**，不是新的账本行：用量已按事件入账，ADR-0011）。
  - Rebill（Phase 8）尚未实现：`REBILL_CREDIT` / `REBILL_DEBIT` 行今天不存在；本设计把它们计入上期调整一栏，但不做 rebill 的原期间引用（Phase 8 设计再定）。
  - 关户（`CLOSED`）尚未实现：本设计不按账户状态筛租户，关户后的出单规则由关户设计闸门定。
- **现有行为与问题证据**：
  - 代码里没有 `monthly_statements` 表、模型、服务、任务与接口（`grep -rn statement app` 只命中 SQL `statement` 变量名）。
  - 账本：`wallet_transactions` 只追加；`(wallet_id, wallet_sequence)` 唯一、从 1 连续递增；`wallets.version` 等于最后一行的 `wallet_sequence`，`wallets.balance` 等于它的 `balance_after`，两者由触发器随账本行在同一事务推进（`app/models/wallet.py`）。
  - `created_at` 由调用方传入，在钱包锁**之前**取值（`post_transaction(now=...)`），所以 `created_at` 的先后与 `wallet_sequence` 的先后不保证一致：一个 `created_at` 在 cut-off 之前的事务可能在 cut-off 之后才提交。
  - 用量计费：`app/services/usage_billing.py` 的 `_process` 在同一事务里写 `AI_USAGE` 账本行（`created_at = processed_at`）与事件快照（`processed_at`、`wallet_transaction_id`、`billing_mode_snapshot`）；`billable_cost = 0` 时不写账本行，事件的 `wallet_transaction_id` 为 NULL。
  - 租户的 `billing_mode` 只会从 `PREPAID` 改为 `INTERNAL_METERED_ONLY`（`app/services/internal_billing.py`），不会反向。
  - 马来西亚日历换算已有：`app/schemas/customer_usage.py` 的 `malaysia_day_start_utc`、`usage_month`；SQL 侧 `app/repositories/usage_events.py` 的 `MalaysiaDay`。

## 2. 设计概要

```text
beat 每小时 ──► generate_due(now)：对每个到期账期 M（升序）× 每个合格租户
                  └─ generate_statement(session, tenant, M, now)  —— 一个 (租户, 账期) 一个事务
                       ① 读租户、钱包（version = 高水位 hw，balance）、上期对账单
                       ② 账本归属：S(M) = A(M; hw) \ A(M-1; hw_prev) 的分类汇总
                       ③ 用量统计：本期事件（收费事件跟随账本行，零收费事件按 processed_at）
                       ④ 三条自检；任一不等 → 不写，抛错
                       ⑤ INSERT monthly_statements（FINALIZED，快照，hw）—— 提交
管理员 POST …/statements/generate ──► 同一个 catch-up（只对这个租户），每份对账单同事务写一条审计
客户 GET /customer/statements[/{id}] ──► 只读本租户 FINALIZED 行
告警 usage_processing_backlog ──► 现有三部分 + 缺失对账单 + 24 小时晚到用量占比
```

### 账期与时间（AIH-TASK-155，`app/services/statements.py`）

- 账期 `M` 用字符串 `YYYY-MM` 表示，是马来西亚日历月。库里所有时刻都是 naive UTC（与全库一致）。
- `start(M)` = `malaysia_day_start_utc(M 的 1 日)`；`end(M)` = `start(M+1)`；`cutoff(M)` = `malaysia_day_start_utc(M+1 的 2 日)`，即 T+1 宽限日（新月 1 日）结束的时刻（ADR-0003）。
- 到期：`now ≥ cutoff(M) + 1 小时`（`GENERATION_DELAY`）。1 小时的余量只为让宽限日最后几秒处理的事件提交完；**金额的正确性不依赖这个余量**（见下面的高水位）。
- `FIRST_STATEMENT_PERIOD: Final = "2026-12"`：早于它的账期一律不生成（Kelvin 定不回补）。代码里不读环境变量、不设覆盖参数；测试用 `2026-12` 及之后的账期并注入 `now`。
- 合格租户 `eligible(T, M)`：生成时 `tenants.billing_mode = PREPAID`，有钱包，且 `wallets.created_at < end(M)`；账户状态不筛（`PENDING_ACTIVATION`、`ENABLED`、`DISABLED` 都出；没有发生额就是一份全零的对账单）。

### 账本归属规则（AIH-TASK-155）

每行账本 `r` 有一个**归属月** `t(r)`，只由不可变的字段决定：

- `AI_USAGE` 行：设其事件（`usage_events.wallet_transaction_id = r.id`）的 `occurred_at` 所在马来西亚月为 `U`。`r.created_at < cutoff(U)` 时 `t(r) = U`；否则 `t(r)` = `r.created_at` 所在马来西亚月（晚到，进当时的开放期）。
- 其他所有类型（`TOPUP`、`ADJUSTMENT_*`、`BONUS`、`REFUND_ADJUSTMENT`、`SYSTEM_CORRECTION`、`REBILL_*`）：`t(r)` = `r.created_at` 所在马来西亚月。

对固定的月 `K`，`t(r) ≤ K` 等价于下面这个只含常量的谓词（`occ` 为事件的 `occurred_at`）：

```text
非 AI_USAGE：created_at < end(K)
AI_USAGE：  occ < end(K) AND ( created_at < end(K) OR ( occ >= start(K) AND created_at < cutoff(K) ) )
```

推导：`U ≤ K-1` 时 `cutoff(U) ≤ cutoff(K-1) < end(K)`，条件化为 `created_at < end(K)`；`U = K` 时化为 `created_at < cutoff(K)`（`cutoff(K) > end(K)`，晚到的不可能落回 `K`）；`U > K` 时为假。

**高水位**：生成账期 `M` 时读一次钱包，`hw(M) = wallets.version`，`B = wallets.balance`（同一行、同一次读，二者由触发器同事务推进，所以 `B` 就是序号 `hw(M)` 那一行的 `balance_after`；`hw = 0` 时 `B = 0`）。序号 `≤ hw(M)` 的行此刻都已提交、以后不会再有，所以下面的集合一经生成就冻结。`hw(M)` 存进对账单行（`ledger_high_sequence`）。

```text
A(K; h) = { r : r.wallet_sequence ≤ h  且  t(r) ≤ K }
hw_prev = 上期对账单（M-1）的 ledger_high_sequence；上期不合格（钱包在 M-1 结束后才建，或 M-1 早于首个账期）时取 hw(M)
S(M)    = A(M; hw(M)) \ A(M-1; hw_prev)          —— 本期对账单的账本行
期末    = Σ A(M; hw(M))   = B − Σ{ r : seq ≤ hw(M), t(r) > M }
期初    = 上期对账单的期末；上期不合格时 = Σ A(M-1; hw(M)) = B − Σ{ r : seq ≤ hw(M), t(r) ≥ M }
```

- 每行账本恰好进一份对账单：`A` 对 `K` 与 `h` 都单调（`hw` 只增），相邻两期的差集不重不漏。一个 `created_at` 在 cut-off 之前、却在生成之后才提交的行，序号 `> hw(M)`，自然落进下一期的 `S`（归为上期调整），不会丢。
- **候选扫描**（全用现有索引，**不假设 `created_at` 与 `occ` 的先后**：摄取允许 `occurred_at` 比服务端时间晚最多 300 秒（`app/services/usage_ingest.py` 的 `FUTURE_TOLERANCE`），所以 `created_at < occ` 是合法数据；进程内 AI 模块等其他写入路径也不受这个容差的数据库约束，所以设计不依赖任何容差值）。记
  ```text
  L(K; h) = { r : r.wallet_sequence ≤ h  且  t(r) > K }
  ```
  由谓词取反，`t(r) > K` 推出：`r.created_at ≥ end(K)`，或者 `r` 是 `AI_USAGE` 且 `occ ≥ end(K)`（非 `AI_USAGE` 行只有前者；`AI_USAGE` 行若 `occ < end(K)` 且 `created_at < end(K)`，谓词第一个析取项成立，`t(r) ≤ K`）。所以 `L(K; h)` 的候选是两段按 `created_at` 互不相交的范围，每段里的每一行再用完整谓词 `NOT (t(r) ≤ K)` 判定（候选只是上界，判定才决定归属）：
  - `C1(K)`：`created_at ≥ end(K)`，走 `ix_wallet_transactions_tenant_created`；
  - `C2(K)`：`AI_USAGE` 行、`created_at < end(K)`、其事件 `occ ≥ end(K)`，从事件侧走 `ix_usage_events_tenant_occurred`，按 `wallet_transaction_id` 连账本主键。
- 用 `L` 写出三处扫描（`end(M-1) = start(M)`）：
  - 期末 = `B − Σ L(M; hw(M))`；
  - 上期不合格时的期初 = `B − Σ L(M-1; hw(M))`；
  - `S(M)` = 段一 `{ r : hw_prev < seq ≤ hw(M), t(r) ≤ M }`（走 `ix_wallet_transactions_tenant_sequence`）∪ 段二 `{ r ∈ L(M-1; hw_prev) : t(r) ≤ M }`（候选是 `C1(M-1)` 与 `C2(M-1)` 再加 `seq ≤ hw_prev`）。段一与段二按 `seq` 不相交，段二的两段候选按 `created_at` 不相交。
  每段各写一条查询，不写成一个 `OR`；候选里出现连不上事件的 `AI_USAGE` 行，按下面「发生额分类」末句处理（数据缺陷）。
- 生成账期 `M` 时，若 `M-1` 合格却没有对账单，**拒绝**（`PreviousStatementMissing`），不跳过：否则 `hw_prev` 取不到，`A` 链会断。catch-up 按账期升序，前一期失败，本轮跳过这个租户的后续账期。

### 发生额分类（AIH-TASK-155）

对 `S(M)` 按类型汇总（金额是账本的带符号金额，8 位小数，`Decimal`，不经 float）：

| 快照字段 | 内容 |
| --- | --- |
| `total_topup` | `TOPUP` 之和（≥ 0） |
| `total_ai_usage` | `AI_USAGE` 且 `occ ∈ [start(M), end(M))` 之和（≤ 0，本期用量扣费） |
| `total_adjustments` | `ADJUSTMENT_CREDIT`、`ADJUSTMENT_DEBIT`、`BONUS`、`REFUND_ADJUSTMENT`、`SYSTEM_CORRECTION` 之和 |
| `prior_period.usage[]` | `AI_USAGE` 且 `occ < start(M)`：按 `occ` 的马来西亚月分组，每组 `usage_period`（`YYYY-MM`）、`request_count`（行数）、`amount`（之和），按 `usage_period` 升序 |
| `prior_period.rebill` | `REBILL_CREDIT`、`REBILL_DEBIT` 之和 |
| `prior_period.total` | 上两项之和 |

`S(M)` 里不可能有 `occ ≥ end(M)` 的 `AI_USAGE` 行（`t ≥ U`）。一行 `AI_USAGE` 找不到事件（`LEFT JOIN` 为空）视为数据缺陷，生成失败。

### 用量统计（AIH-TASK-156）

本期用量事件 `E(M)`：本租户、`status = PROCESSED`、`billing_mode_snapshot = 'PREPAID'`、`occurred_at ∈ [start(M), end(M))`，并且

- 收费事件（`wallet_transaction_id` 非空）：其账本行在 `S(M)` 里，即 `seq ≤ hw(M)` 且 `created_at < cutoff(M)`（`occ` 在本月时 `S(M)` 的条件化简为此）——**跟随账本行**，与金额同一个归属；
- 零收费事件（`wallet_transaction_id` 为空）：`processed_at < cutoff(M)`。

快照的用量字段（全部只来自 `E(M)`）：`request_count`、`conversation_count`（`COUNT(DISTINCT conversation_id)`，NULL 不计，与 `GET /customer/usage-summary` 同一口径）、`input_tokens`、`output_tokens`（NULL 按 0）、`total_customer_charges`（`billable_cost` 之和），以及三组明细 `by_provider`（`provider_code`）、`by_model`（`provider_code`、`model_code`）、`by_project`（`project_public_id`、`project_name`），每组 `request_count`、`input_tokens`、`output_tokens`、`charges`，按代码 / 名称升序（名称相同再按 `project_public_id`）。代码取目录表 `ai_providers.code`、`ai_models.code`，项目名取生成时的 `projects.name`（快照冻结，之后改名不影响已定稿的对账单）。

上期调整的用量只给金额与条数（来自账本），不进本期的用量统计。

### 自检（第 1 条 AIH-TASK-155，第 2、3 条 AIH-TASK-156）

生成在写入之前做三条等式检查，任一不成立就抛 `StatementIntegrityError`、不写任何行、记一条 error 日志（只带租户 `public_id`、账期与失败的检查 `check`：`1`、`2`、`3`，或 `missing_event` 表示有 `AI_USAGE` 行连不上事件；不带金额），供 runbook 定位缺陷：

1. `opening + Σ S(M) == closing`（`closing` 按上面「期末」公式独立算出）；
2. `total_customer_charges == −total_ai_usage`（本期收费事件的 `billable_cost` 之和与其账本行金额之和互为相反数；零收费事件的 `billable_cost` 为 0）；
3. `E(M)` 中收费事件的条数 == `S(M)` 中本期 `AI_USAGE` 行的条数。

### 快照（AIH-TASK-156）

`snapshot_json` 的键集合固定（`schema_version = 1`），金额都是 8 位小数的字符串，计数是整数：

```text
schema_version, period, currency("MYR"), company_name, tax_notice{zh, en},
opening_balance, total_topup, total_ai_usage, total_adjustments, prior_period{usage[], rebill, total}, closing_balance,
request_count, conversation_count, input_tokens, output_tokens, total_customer_charges,
by_provider[], by_model[], by_project[]
```

`tax_notice` 是生成时取到的税务政策版本的 `treatment_json.document_notice` 原文（`zh`、`en` 两个字符串，逐字照抄、不改写），只放说明文字，不放税率、版本 id 或其他政策字段；版本引用存在列 `tax_policy_version_id` 上（见「数据库」）。不出现任何成本、汇率、加价、毛利、价格版本、规则、内部 id 的键（INV-7）；测试断言键集合逐一相等，而不是「不含某几个键」。

### 生成函数（AIH-TASK-156）

`generate_statement(session, tenant_id, period, *, now, generated_by=None) -> GenerateResult`，不提交、只 flush：

1. `period` 早于 `FIRST_STATEMENT_PERIOD` 或未到期 → `StatementNotDue`；租户不合格 → `StatementNotEligible`。
2. 已有 `(tenant_id, period_start)` 的行 → 返回它（`created=False`），什么都不写（幂等）。
3. `M-1` 合格且没有对账单 → `PreviousStatementMissing`。
4. 取税务政策：`resolve_tax_policy(session, MY, SST, at=cutoff(M))`（`app/repositories/tax_policy.py`，已有；`FOR SHARE` 共享读，只与发布税务政策互斥）。取 `cutoff(M)` 而不取 `now`，是为了同一期不论哪一轮生成，取到的都是同一个版本。返回 `None`（这一时刻没有生效版本）或抛 `ValueError`（读出校验失败）→ `StatementTaxPolicyUnavailable`，不写行；不默认「无税」（照该函数的文档）。
5. 读钱包与上期（普通读，不加锁），按上面算 `S(M)`、用量、自检。
6. INSERT 一行：`status = FINALIZED`、`finalized_at = created_at = now`、`statement_number` = `ST-<YYYYMM>-` 加 8 个 Crockford base32 随机字符（`secrets`）、`generated_by`（系统为 NULL，管理员为其 `users.id`）、`ledger_high_sequence = hw(M)`、`cutoff_at`、`tax_policy_version_id`（第 4 步取到的版本）、快照。本函数不写审计；管理员触发时由 catch-up 的提交前钩子在同一事务写审计（见「Celery 任务」与「管理端接口」）。

并发：同一 `(tenant_id, period_start)` 两个生成者同时到第 6 步，唯一约束让后提交的那个 `IntegrityError`；调用方回滚后再查一次，行已存在就当作成功（`created=False`），不存在（编号撞了）就记 warning、本轮放弃这一项，下一轮重来。

### 数据库（迁移 0035，AIH-TASK-154）

表 `monthly_statements`（spec §74.8，外加 `ledger_high_sequence`、`generated_by` 与 `tax_policy_version_id`）：

| 列 | 类型 | 说明 |
| --- | --- | --- |
| `id` | BIGINT 自增主键 | |
| `public_id` | CHAR(36) | 唯一 |
| `tenant_id` | BIGINT | FK `tenants.id` RESTRICT |
| `statement_number` | VARCHAR(32) | 唯一 |
| `period_start` / `period_end` / `cutoff_at` | DATETIME | naive UTC；CHECK `period_start < period_end AND period_end < cutoff_at` |
| `status` | VARCHAR(16) | CHECK `IN ('DRAFT','FINALIZED','VOID')` |
| `ledger_high_sequence` | BIGINT | CHECK `>= 0` |
| `snapshot_json` | JSON | NOT NULL |
| `generated_by` | BIGINT NULL | FK `users.id` RESTRICT；NULL = 系统 |
| `tax_policy_version_id` | BIGINT | NOT NULL；FK `tax_policy_versions.id` RESTRICT（spec §45.1 `REQ-TAX-001`：不可变的财务快照引用版本化的税务政策） |
| `finalized_at` | DATETIME NULL | CHECK `status <> 'FINALIZED' OR finalized_at IS NOT NULL` |
| `created_at` | DATETIME | |

- 唯一 `uq_monthly_statements_tenant_period (tenant_id, period_start)`（V1 没有更正版本，spec §74.8 末句）；索引 `ix_monthly_statements_period (period_start, tenant_id)` 供管理端按账期列与告警查缺失；索引 `ix_monthly_statements_generated_by (generated_by)` 与 `ix_monthly_statements_tax_policy_version_id (tax_policy_version_id)` 显式声明（否则 MySQL 会为外键自动建一个同名于外键的索引，形状校验对不上）。
- MySQL 触发器（INV-12 的数据库兜底）：`trg_monthly_statements_before_update` 与 `trg_monthly_statements_before_delete`，`OLD.status <> 'DRAFT'` 时 `SIGNAL SQLSTATE '45000'`。SQLite 不建触发器（与 0006 等迁移同一惯例），服务层本来就没有更新与删除路径。
- 迁移形状照 0028：表已存在且形状（列、索引、外键与 `DELETE_RULE`、CHECK 名称）一致时，MySQL 上再逐个看两个触发器：读 `information_schema.TRIGGERS` 的 `ACTION_TIMING`、`EVENT_MANIPULATION`、`EVENT_OBJECT_TABLE` 与 `ACTION_STATEMENT`（去掉首尾空白后与迁移里的主体逐字比较），都在且一致 → 不发 DDL；缺了的只补缺的那个（补完中断的升级：建表与建触发器是分开提交的 DDL）；存在但任一项不一致 → 抛错、不发 DDL。表形状不一致 → 抛错、不发 DDL。降级在离线模式拒绝，表非空拒绝，不删任何行；表为空先删触发器再删表。

### Celery 任务（AIH-TASK-157，`app/tasks/statements.py`）

- `app.tasks.statements.generate_due`，beat 每小时整点一次（`crontab(minute=0)`，与 `FX_FETCH_SCHEDULE` 同一写法），`expires` 3240 秒；所以「第 2 日 01:00 之后的第一个整点」就是 01:00 本身。
- `generate_due(factory, now)`：账期从 `FIRST_STATEMENT_PERIOD` 到最近一个到期的账期，对每个合格租户按账期升序调用 `generate_statement`，每项一个事务；单项失败（任何异常）记一条 error 日志（租户 `public_id`、账期、异常类型名），回滚，跳过这个租户的后续账期，继续下一个租户。返回本轮新生成的份数。
- `catch_up_tenant(factory, tenant_id, now, *, generated_by=None, before_commit=None, raise_errors=False) -> list[MonthlyStatement]`：一个租户的 catch-up，`generate_due` 对每个租户调用它（`raise_errors=False`）；管理端的手动补生成也调用它（`raise_errors=True`）。按到期账期升序，每期一个事务：
  - `generate_statement` 返回已有行 → 下一期；未到期、不合格 → 跳过这一期。
  - `generate_statement` 本身抛 `IntegrityError`（INSERT 撞唯一约束，InnoDB 在 flush 时就报）→ 回滚，新事务里查 `(tenant_id, period_start)`：已存在当作成功、继续下一期；不存在就是编号碰撞，记 warning、停止这个租户的后续账期，不算错误。
  - 新生成的那一份：调用 `before_commit(session, statement)`，紧接着 `session.flush()`，再提交；钩子、这次 flush 或提交抛的**任何**异常（包括 `IntegrityError`）都按失败处理。
  - 失败（上一条，或 `generate_statement` 抛的其他任何异常）：回滚，记一条 error 日志（租户 `public_id`、账期、异常类型名），停止这个租户的后续账期；`raise_errors=True` 时原样抛出，否则返回本次已新生成的部分。
  - 返回本次新生成的对账单，按账期升序。

### 告警（AIH-TASK-158，`app/services/alerts.py` 的 `usage_processing_backlog`）

Healthchecks 已 20/20，按 Kelvin 2026-11-04 的决定并进 `usage_processing_backlog`，不加维度、不改 `deploy/monitor.sh`：

- **缺失对账单**：对每个账期 `M`（`FIRST_STATEMENT_PERIOD` 起，`cutoff(M) + 6 小时 ≤ now < cutoff(M) + 30 小时`，常量 `STATEMENT_MISSING_GRACE = 6h`、`STATEMENT_MISSING_WINDOW = 24h`）与每个合格租户，没有对账单的项数 `> 0` → P2。摘要：有缺失时 `<n> statements missing, earliest period <YYYY-MM>`，没有时 `0 statements missing`。只报 24 小时（Kelvin 2026-11-04 定）：`deploy/monitor.sh` 每个维度一个 Healthchecks 检查，非 OK 就推 `/fail`，而 Healthchecks 只在状态切换时通知一次。数据缺陷导致的永久停出（§5）如果一直报，这个维度会一直是 down，之后同一维度的积压、过期租约（P1）都不会再通知任何人。只报 24 小时则一定通知一次，窗口过后维度恢复；代价是同一维度的其他问题最多被遮住 24 小时，停出之后只剩 error 日志与管理端列表可见。Healthchecks 已 20/20（免费版上限），不单开维度。
- **晚到用量占比**：最近 24 小时内入账的预付租户 `AI_USAGE` 行（按 `created_at`），其中 `t(r) ≠ U` 的为晚到；晚到条数占比或金额占比 `≥ 1%` → P2（用精确的 `Decimal` 比值与 `0.01` 比较；摘要里舍入后的百分比只用于显示，不参与判定）。只统计连得上事件的行（内连接；连不上事件的 `AI_USAGE` 行是数据缺陷，由生成的自检报出）。窗口最多跨两个月，按月边界拆开，每段用该月的常量谓词判定（晚到 ⇔ `occ < start(K) AND NOT (occ >= start(K-1) AND created_at < cutoff(K-1))`，`K` 为该段所在月）。摘要 `late usage 24h <a>/<b> rows (<x>%), <y>% of amount`，百分比 `ROUND_HALF_UP` 到两位小数，分母为 0 时写 `0.00`（如 `late usage 24h 0/0 rows (0.00%), 0.00% of amount`）；只有计数与百分比，不带租户与金额。
- 现有三部分（积压、PROCESSING、过期租约）的判据与摘要不变；新两部分各成一段，用 `; ` 接在后面，维度级别取最严重的一个（照 `wallet_negative_balance` 的合并写法）。

### 管理端接口（AIH-TASK-159，`app/api/admin_statements.py`）

- `GET /api/v1/admin/statements?customer_id=&period=&page=&page_size=`（现有 `Page`，与客户列表同一分页写法）：按 `period_start` 倒序、`id` 倒序；每项 `statement_id`、`statement_number`、`customer_id`、`company_name`（快照里的）、`period`、`status`、`closing_balance`、`finalized_at`。
- `GET /api/v1/admin/statements/{statement_id}`：上面的字段加 `cutoff_at`、`ledger_high_sequence`、`generated_by_email`（生成者管理员的邮箱，系统生成为 null；`users` 没有 `public_id`）与 `snapshot`（完整快照，作为一个嵌套对象，不展开到顶层）。管理端也看不到成本与毛利（快照里本来就没有；内部成本分析是 Phase 7 后续批）。
- `POST /api/v1/admin/customers/{customer_id}/statements/generate`：对这个客户跑 catch-up，200 返回本次新生成的 `[{statement_id, period}]`（可以为空）；客户不存在 404 `CUSTOMER_NOT_FOUND`；非预付或没有钱包 409 `STATEMENT_NOT_ELIGIBLE`（预付但没有到期账期时 200 空列表）；某一期生成失败 500（已生成的前几期照常保留）。同步执行，没有请求体。审计经 `before_commit` 钩子写：每份新生成的对账单一条 `STATEMENT_GENERATE`（`entity_type = monthly_statement`、`entity_id` 为对账单 `public_id`、`after_state` 只有 `period`），与对账单同一事务；`monthly_statement` 登记进 `app/services/audit_query.py` 的 `PUBLIC_ENTITY_TYPES`。
- 三个接口的第一件事都是 `require_admin(request)`；`AuditAction` 新增 `STATEMENT_GENERATE`，前端 `adminAudit.ts` 同步。

### 客户接口（AIH-TASK-160，`app/api/customer.py`）

- `GET /api/v1/customer/statements?page=&page_size=`：本租户 `FINALIZED` 的对账单，按账期倒序；每项 `statement_id`、`statement_number`、`period`、`closing_balance`、`finalized_at`。没有对账单的租户（包括一直是内部计量的租户）返回空页；从预付改为内部计量的租户仍能看到改之前的对账单。
- `GET /api/v1/customer/statements/{statement_id}`：同上字段加 `snapshot`（完整快照，嵌套对象），不返回 `cutoff_at`、`ledger_high_sequence`、生成者；别的租户的、不存在的、格式不对的 id 一律 404 `STATEMENT_NOT_FOUND`（鉴权之后再校验）。
- 门户账号在 `DISABLED` 下也能看（只读，AIH-TASK-020 契约）。

### 前端（AIH-TASK-161、162，不在闸门范围内，写在这里只为说明接口够用）

- 两个页面的详情都显示快照里的 `tax_notice.en`（界面只有英文），原样显示、不改写。
- 管理端 `/statements`：列表（按客户、账期筛）、点一行在下方显示详情；按客户筛选时显示「补生成对账单」按钮（调用手动生成接口），成功或失败之后都刷新列表（失败时前几期可能已生成）。
- 门户 `/portal/statements`：列表（每页 12 条，上一页 / 下一页）、点一行在列表下方显示详情（不开弹窗、不跳子路由），金额按现有门户的显示规则（2 位小数）。

## 3. 不变量影响矩阵

| 不变量 | 是否触碰 | 可能怎样被破坏 | 设计控制 | 验证方式 |
| --- | --- | --- | --- | --- |
| INV-1 中心故障不中断 AI | 否 | 生成任务只读账本与事件、写自己的表，不在 AI 调用或用量摄取路径上 | 不改摄取、计费、AI 模块 | 不新增测试；现有测试照常 |
| INV-2 事件不重复扣费 | 否 | 不写账本 | 对账单只读 | §7「只读」 |
| INV-3 支付不重复入账 | 否 | 同上 | 同上 | 同上 |
| INV-4 余额只经账本变动 | 否 | 同上；期初期末是算出来的，不回写钱包 | 不调用 `post_transaction`，不写 `wallets` | §7「只读」 |
| INV-5 历史账本不可变 | 否 | 只读 | 同上 | 同上 |
| INV-6 事件保留版本引用 | 是 | 对账单不带税务政策版本，以后渲染只能读当前政策，改措辞时历史对账单跟着变（ADR-0008 §5） | 列 `tax_policy_version_id` NOT NULL、FK RESTRICT，指向 `cutoff(M)` 时生效的版本；该版本的 `document_notice` 原文冻结进快照 `tax_notice`；定稿行由触发器拒绝修改。用量事件本身不改 | §7「税务说明」 |
| INV-7 客户不可见成本毛利 | 是 | 快照或接口带出成本、汇率、加价 | 快照只从 `billable_cost` 与账本金额汇总；键集合固定并逐一断言；管理端也用同一快照 | §7「快照键集合」「客户接口」 |
| INV-8 租户不可互访 | 是 | 客户读到别人的对账单；生成时混入别的租户的账本或事件 | 客户接口 `WHERE tenant_id = principal.tenant_id AND public_id = ?`；生成的每条查询都带 `tenant_id` | §7「越权」「两个租户」 |
| INV-9 对话内容不入库 | 否 | 快照只有计数、token 数、金额、代码与项目名 | 不读 prompt / response（库里本来也没有） | 快照键集合 |
| INV-10 金额用 Decimal | 是 | 汇总经 float、舍入 | SQL `SUM` 在 `DECIMAL(20,8)` 上；Python 侧只有 `Decimal` 加减；快照存 8 位小数字符串，不舍入 | §7「金额精度」（MySQL 跑） |
| INV-11 event_id 至多一次财务效果 | 否 | 对账单不产生财务效果 | 只读 | — |
| INV-12 定稿对账单不可变 | 是 | 重新生成覆盖；手工 UPDATE；之后晚到的行改动旧单 | 唯一 `(tenant_id, period_start)` + 已存在就返回；MySQL 触发器拒绝改删非草稿行；晚到行按高水位进下一期 | §7「幂等」「触发器」「晚到」 |
| INV-13 状态与事件原子提交 | 是 | 对账单写了、审计没写，或反之 | 管理员生成时对账单行与审计同一事务 | §7「审计」 |
| INV-14 队列丢失不毁持久工作 | 是 | beat 或 worker 丢了一次任务，某期没生成 | 每小时 catch-up 从首个账期扫到最近到期账期，缺什么补什么；到期后 6–30 小时内仍缺失告警 | §7「catch-up」「告警」 |

## 4. 状态与并发

| 当前状态 | 事件 | 前置条件 | 新状态 | 副作用 | 非法时结果 |
| --- | --- | --- | --- | --- | --- |
| （无行） | 生成 | 到期、合格、上期齐全、自检通过 | `FINALIZED` | 审计（仅管理员触发） | 不写行，抛对应异常 |
| `FINALIZED` | 再次生成 | — | `FINALIZED`（不变） | 无 | 返回已有行 |
| `FINALIZED` / `VOID` | UPDATE / DELETE | — | — | — | MySQL 触发器 SIGNAL |
| `DRAFT` | — | V1 不产生 `DRAFT` 行 | — | — | — |

- 串行化：靠唯一约束 `(tenant_id, period_start)`，不加锁、不读钱包锁（高水位读不需要锁，见 §2）；唯一的锁是税务政策的共享读（§2「生成函数」第 4 步），只与发布税务政策互斥。
- 幂等键：`(tenant_id, period_start)`。没有「相同 ID 不同载荷」：载荷完全由账本与事件决定，已存在就不重算。
- 重试层：beat 每小时一次；管理端手动触发是同一个函数。
- 原子提交：一份对账单行 + （管理员触发时）一条审计。

## 5. 失败与恢复矩阵

| 失败点 | 对外结果 | 数据最终状态 | 是否可重试 | 恢复来源 | 告警 |
| --- | --- | --- | --- | --- | --- |
| 未到期 / 早于首个账期 | 任务跳过；管理端只生成到期的 | 无行 | — | — | 无 |
| `cutoff(M)` 没有生效的税务政策，或读出校验失败 | `StatementTaxPolicyUnavailable`，error 日志；管理端 500 | 无行；这个租户后续账期随之 `PreviousStatementMissing` | 是 | 没有生效版本：按税务政策发布流程补一条覆盖 `cutoff(M)` 的版本，下一个整点自动补上（生产自 2026-10-17 起已有一条不设尽头的 `PUBLISHED` 版本，正常不会触发）；读出校验失败是数据缺陷，按上面「自检不等」一行的处置 | 到期后 6–30 小时内缺失 P2 |
| 不合格（非预付） | 任务跳过；管理端 409 | 无行 | — | — | 无 |
| 上期缺失 | 本轮跳过该租户后续账期 | 无行 | 是 | 下一轮先补上期 | 到期后 6–30 小时内缺失 P2 |
| 自检不等，或有 `AI_USAGE` 行连不上事件 | error 日志（带检查编号）；管理端 500 | 这个租户从账期 `M` 起**永久停出**：`M` 无行、每小时重试都失败；`M+1` 起每期都 `PreviousStatementMissing`、无行。其他租户不受影响 | 否（数据不可变，重试结果相同） | **已知限制，Kelvin 2026-11-04 拍板接受**（§10）。只有数据缺陷（`AI_USAGE` 行连不上事件、收费事件的 `billable_cost` 与账本金额不符）会触发。计费在同一事务里写事件与账本，所以这种缺陷只可能来自代码缺陷或绕过服务层直接写库。各集合由完整谓词判定、不依赖时间先后（§2「候选扫描」），合法数据不会触发。本批不提供放行、不出 `VOID` 占位单。处置写在 runbook（AIH-TASK-157）：按日志里的租户、账期与检查编号查出缺陷行，不改、不删账本、事件与对账单；再开设计闸门定修复（例如作废与更正版本，§10「后续」） | 到期后 6–30 小时内缺失 P2（报一次就停，§2「告警」；之后靠 error 日志与管理端列表） |
| DB 提交失败 | error 日志；管理端 500 | 无行（事务回滚） | 是 | 下一轮 | 到期后 6–30 小时内缺失 P2 |
| 并发重复生成 | 后者 `IntegrityError` → 回滚后查到已有行，当作成功 | 恰好一行 | — | — | 无 |
| 编号碰撞 | warning；本轮放弃这一项 | 无行 | 是 | 下一轮换随机数 | 一般不会触发 |
| Redis/Celery 丢失 | 本小时没跑 | 无行 | 是 | 下一小时 beat；管理端手动 | 到期后 6–30 小时内缺失 P2 |
| 生成之后才提交的旧行 | 不改已定稿对账单 | 进下一期，归为上期调整 | — | 高水位规则 | 无：这类行 `created_at < cutoff(U)`、`t(r) = U`，晚到占比的判定按定义不计它（§10 假设） |

## 6. 数据与安全边界

- 租户过滤：生成的每条查询带 `tenant_id`；客户接口按 `principal.tenant_id` 过滤，id 不属于本租户一律 404（不区分「不存在」与「别人的」）。
- 鉴权：管理端三个接口 `require_admin`；客户接口 `require_customer`；生成任务无外部入口。
- 客户侧禁止返回：成本、汇率、加价、毛利、价格版本与规则、内部 id、`generated_by`、`ledger_high_sequence`。管理端详情可以多返回 `ledger_high_sequence` 与生成者，便于排查。
- 日志：只带租户 `public_id`、账期、异常类型名与计数，不带金额与公司名；审计元数据只有账期。
- 密钥：不涉及。
- prompt / response：不涉及。
- 保留与删除：对账单行 RESTRICT 引用租户与用户，不随删除级联；保留期随上线闸门的数据留存政策定（后续计划已有）。

## 7. 测试证据计划

金额与 SQL 相关的用例都在 SQLite 与 MySQL 两个分片跑；只有 MySQL 才成立的（触发器、`DECIMAL` 精度）放进 `if engine.dialect.name == "mysql":` 或只在 MySQL 参数化下跑。时间一律注入 `now`。

| 风险/需求 | 测试层级 | 场景 | 预期结果 | 任务 |
| --- | --- | --- | --- | --- |
| 账期边界 | unit | `2026-12`：`start` = 2026-11-30 16:00、`end` = 2026-12-31 16:00、`cutoff` = 2027-01-01 16:00（UTC）；`2027-02` 闰年与非闰年；到期判定在 `cutoff + 1h` 前后各一秒 | 时刻逐一相等；到期恰在 `cutoff + 1h` | 155 |
| 归属谓词 | unit + db | 表驱动：`AI_USAGE` 的 `occ` / `created_at` 落在上月末、宽限日内、宽限日后、本月、下月 1 日，非用量行落在月末前后一秒；含 `created_at < occ` 的行（`occ` 在下月初、`created_at` 在本月末） | 与直接按定义算 `t(r)` 的参考函数逐一相等 | 155 |
| `created_at` 早于 `occ` 跨月 | db | 本月末 `end(M) − 90s` 入账、`occ = end(M) + 120s` 的用量（审查 #565 v1 的场景），以及镜像：上月末 `start(M) − 90s` 入账、`occ = start(M) + 120s` 的用量；连续生成 `M-1`、`M`、`M+1` | 自检都通过；前者不进 `M`、进 `M+1` 的本期用量；后者不进 `M-1`、进 `M` 的本期用量；每期期初 = 上期期末 | 155、156 |
| 扫描完整性 | db | 一批账本行，覆盖各类型与 `created_at` 相对 `occ` 早 0 秒至 1 天、晚 0 秒至 40 天，跨三个月；对几个 `(K, h)` 组合 | `L(K; h)`、`S(M)`、期末、首期期初与「全表扫一遍、逐行用 `t(r)` 参考函数判定」的结果逐一相等（集合与金额） | 155 |
| 正常路径 | db | 一个租户：期初有余额；本月充值、用量、调账；生成 | 各字段等于手算值；期初 + 发生额 = 期末 | 155、156 |
| 宽限日 | db | 上月 31 日 23:59 发生、1 日 10:00 入账的用量 | 进上月对账单的本期用量；本月对账单不含 | 155 |
| 晚到 | db | 上月发生、2 日 00:00 之后入账的用量；上上月发生、本月入账的用量 | 进本月 `prior_period.usage`，`usage_period` 分别为原月；上月已定稿的对账单不变 | 155 |
| 生成后才提交 | db | 上月对账单生成后，再插入一行 `created_at` 在 cut-off 之前、序号大于高水位的 `AI_USAGE` 行 | 上月不变；本月对账单含它（上期调整）；两期期末与钱包余额一致 | 155、156 |
| 链式连续 | db | 连续三个月生成 | 每期期初 = 上期期末；最后一期期末 = 截至该期高水位、归属不晚于该月的账本和 | 156 |
| 首期 | db | 租户在首个账期之前就有账本 | 期初 = 截至首期高水位、归属早于首期的账本和 | 155 |
| 自检 | db | 人为插入一行没有事件的 `AI_USAGE` 账本行（155）；插入时就是 `PROCESSED`、`billable_cost` 与账本金额不符的事件（156；MySQL 上触发器不许改已处理的事件，不能事后改） | `StatementIntegrityError`，不写行，日志不含金额 | 155、156 |
| 幂等 | db | 同一 `(租户, 账期)` 生成两次 | 第二次返回已有行、`created=False`，行数 1 | 156 |
| 并发 | db（MySQL） | 两个会话在两个线程里生成同一项：第二个的 INSERT 在唯一键上等第一个提交，之后报 `IntegrityError` | 恰好一行；catch-up 回滚后查到已有行、当作成功（157） | 156、157 |
| 上期缺失 | db | `M-1` 合格但没有对账单时生成 `M` | `PreviousStatementMissing`，不写行 | 156 |
| 合格性 | db | 内部计量租户；钱包在 `end(M)` 之后才建；早于首个账期 | 不生成 | 156、157 |
| 零收费事件 | db | `billable_cost = 0`、无账本行的事件在 cut-off 前后处理 | 前者计入本期请求数与 token，后者不计；金额不受影响 | 156 |
| 用量统计 | db | 两个项目、两个模型、有无 `conversation_id` | 明细与合计相等；会话数为去重的非空 `conversation_id` | 156 |
| 快照键集合 | unit | 生成的快照 | 键集合与 §2 逐一相等（含 `tax_notice` 的 `zh`、`en`），没有成本类键 | 156 |
| 税务说明 | db | `cutoff(M)` 时生效的版本；在 `end(M)` 与 `cutoff(M)` 之间、`cutoff(M)` 之后各换一次版本；`cutoff(M)` 没有生效版本；`treatment_json` 读出校验失败 | 快照 `tax_notice` 与 `cutoff(M)` 时生效版本的 `document_notice` 逐字相等、列指向该版本；之后换版本不影响已定稿的对账单，下一期用下一期 `cutoff` 时的版本；后两种 `StatementTaxPolicyUnavailable`、不写行，补发布后下一轮生成成功 | 156 |
| 金额精度 | db（MySQL） | 金额带 8 位小数，多行求和 | 快照字符串与 `Decimal` 手算逐位相等 | 155、156 |
| 两个租户 | db | 两个租户同月都有发生额 | 各自的对账单只含自己的行 | 156 |
| 触发器 | db（MySQL） | 对 `FINALIZED` 行 UPDATE 任一列、DELETE | 都报 45000；`DRAFT` 行可改 | 154 |
| 迁移 | db | 升级、形状校验（含 `tax_policy_version_id` 的外键与索引）、降级（空表、非空表、离线） | 同 0028 的三种结果 | 154 |
| catch-up | db | 任务停了两个月后运行；某租户中间一期自检失败；编号碰撞 | 其余租户补齐；失败租户只到失败之前；返回新生成份数；碰撞记 warning、停止该租户后续账期、不算错误 | 157 |
| 永久停出（已知限制） | db | 某租户 `M` 期自检失败后，再跑两轮整点（`now` 跨过 `M+1` 的到期时刻） | 每轮 `M` 都失败、日志带检查编号；`M+1` 不生成（`PreviousStatementMissing`）；其他租户的 `M`、`M+1` 照常生成 | 157 |
| beat 登记 | unit | `beat_schedule` | 有 `statements-generate-due` 条目，`crontab(minute=0)`、`expires` 3240 | 157 |
| 告警：缺失 | db | 缺一份时 `cutoff + 6h` 与 `cutoff + 30h` 前后各一秒；两期都缺、只有较新一期在窗口内 | `+6h` 之前 OK、`[+6h, +30h)` P2、`+30h` 起 OK；摘要只数窗口内的项，只有计数与账期 | 158 |
| 告警：晚到 | db | 24 小时内 100 行里 1 行晚到；窗口跨月 | P2；0 行晚到 OK；跨月两段各按各月常量判 | 158 |
| 告警：原有部分 | db | 原有积压、过期租约用例 | 判据与摘要前缀不变 | 158 |
| 管理端 | api | 列表筛选、详情、手动生成（新建、已有、预付但没有到期账期、非预付 409、没有钱包 409、不存在 404、非管理员 403、中间一期失败） | 状态码与载荷；手动生成每份一条审计；没有到期账期 200 空列表；中间一期失败 500，之前几期的对账单与审计保留，失败那一期与之后都没有 | 159 |
| 审计 | db | 手动生成时让审计写入失败 | 对账单也不写 | 159 |
| 客户接口 | api | 列表只含本租户 `FINALIZED`；别人的 id、乱写的 id 404；内部租户空列表；响应里没有成本类键 | 同左 | 160 |
| 只读 | db | 生成前后对比 `wallets` 与 `wallet_transactions` | 完全不变 | 156 |

## 8. 迁移与上线

- 数据迁移：只建新表，不动已有表、不回填。
- 锁表与性能：新表建在空库上，无锁影响。生成查询按 §2「候选扫描」的范围走现有索引；一个大租户一个月的账本与事件在后台任务里聚合，耗时随数据量增长，不在请求路径上（管理端手动触发除外，见 §10）。
- 兼容窗口：迁移先于代码可用（新表无人读写）；代码依赖新表，部署顺序是迁移随部署先跑（现有部署流程）。
- 部署顺序：154（迁移）→ 155、156（纯服务，无入口）→ 157（beat 开始跑；首个账期 `2026-12` 在 2027-01-02 01:00 才到期，之前每次都是空跑）→ 158（告警）→ 159、160（接口）→ 161、162（页面）。
- 回滚：154 之后任何一步都可以只回滚应用镜像；表留着。降级迁移在表非空时拒绝。
- 部分部署：157 上线前没有任何对账单；159、160 上线前页面不存在。都不影响计费。
- 监控与负责人：`usage_processing_backlog` 的两段新判据；负责人 Kelvin。

## 9. 方案取舍

| 方案 | 优点 | 风险 | 未采用原因 |
| --- | --- | --- | --- |
| 采用：按 `t(r)` 与钱包高水位划分账本行 | 每行恰好一期；不依赖任何时间余量；期初期末可独立算出并自检 | 期末不是某一时刻的钱包余额（宽限日里的上月用量算在上月），要在页面上说明 | — |
| 只按 `created_at` 与 cut-off 划分 | 简单 | `created_at` 在钱包锁之前取值，cut-off 前取值、生成后才提交的行会两期都不含，期初期末对不上 | 正确性依赖时间假设，无法证明 |
| 加 `PRIOR_PERIOD_ADJUSTMENT` 账本类型，晚到时冲销再记 | 账本上直接看得见 | 要改 `post_transaction` 与计费、一笔用量两行账本，违背 ADR-0011 的每事件一行 | 上期调整只是展示归类，不需要新的财务效果 |
| 期末取生成时的钱包余额 | 与钱包页一致 | 包含 cut-off 之后、下一期的发生额 | 期初 + 发生额 ≠ 期末 |
| 生成时锁钱包 | 能拿到「此刻」的一致视图 | 与计费抢锁；高水位读本来就一致 | 不需要 |
| 编号用全局序列或租户内部 id | 好读、可排序 | 暴露内部 id 或要额外的序列表 | 随机后缀 + 唯一约束足够，碰撞下一轮重来 |

## 10. 未决问题与假设

- **已拍板（Kelvin 2026-11-04）**：本批范围到「生成并定稿 + 管理端查看与手动补生成 + 门户查看」；只给 `PREPAID` 出；生成即定稿；首个账期是部署后第一个完整月（`2026-12`），不回补；上期调整占比的监控本批做、并进 `usage_processing_backlog`；出单不通知客户。
- **已拍板（Kelvin 2026-11-04，#565 v2 审查后）**：数据缺陷（`AI_USAGE` 行连不上事件、收费事件的 `billable_cost` 与账本金额不符）让自检失败时，这个租户从该期起永久停出，接受为已知限制。本批不做放行登记、不出 `VOID` 占位单。缺失告警在该期到期后 6–30 小时内报 P2，之后不再报（下一条）；发生时按 runbook 定位，再开设计闸门定修复（§5）。
- **已拍板（Kelvin 2026-11-04，#565 v3 预审后）**：缺失对账单的告警只在到期后 `[6 小时, 30 小时)` 的窗口里报，不一直报、不单开维度（理由见 §2「告警」）。
- **不做的假设**：不假设 `created_at ≥ occurred_at`，也不依赖 `FUTURE_TOLERANCE` 的值（§2「候选扫描」）；金额与归属的正确性只依赖不可变字段与高水位。
- **假设**：`created_at` 在 cut-off 之前、却在生成（cut-off 之后至少 1 小时）之后才提交的 `AI_USAGE` 行，金额正确地进下一期的上期调整（高水位规则），但晚到用量告警不计它（它的 `t(r) = U`，不算晚到）。这要求一个计费事务跨过 1 小时以上才提交；处理事务在拿到计费锁之后才取时刻，锁等待有上限、事务以秒计，所以不为它另加告警，§7「生成后才提交」覆盖它的金额去向。
- **假设**：零收费事件在 cut-off 之前取了 `processed_at`、却在生成（cut-off 之后至少 1 小时）之后才提交时，会从对账单的请求数里漏掉（它没有金额，账本侧不受影响）。处理事务在拿到计费锁之后才取 `processed_at`，锁等待有上限，正常情况下事务以秒计；这个漏计只影响计数，记为已知限制。
- **假设**：管理端手动触发是同步执行；一个大租户一个月的数据量下可能耗时较长。目前只有内部租户与验收租户，真实客户的量级出现后再看是否改为异步。
- **后续**：作废与更正版本（`VOID`、唯一约束改为「有效版本唯一」）、PDF、财务分析、出单通知、关户后的出单规则、rebill 的原期间引用，各由对应的设计闸门定。ADR-0003「收口条件」的勾选在本批全部交付后由管理员另开 PR 处理。
- **需要谁拍板**：无。

## 11. 审查与版本绑定

审查方要回答的五个问题常驻在 [scripts/review_checklist.md](../../scripts/review_checklist.md) 的「设计审查」一节，此处不重复。

**版本绑定规则**（规则的出处是 [WORKFLOW §3](../../docs/WORKFLOW.md)）：

- 批准必须写成 `APPROVED: design v<N>`，`<N>` 取本文件顶部的「设计版本」
- **设计版本一变，之前的 APPROVE 自动作废**，必须重新过闸门
- 实质修改：改了契约、表结构、事务边界、状态机、失败语义、不变量控制

### 任务拆分

| 任务 | 层 | 内容 |
| --- | --- | --- |
| AIH-TASK-154 | 数据库 | 迁移 0035、模型、触发器、迁移测试 |
| AIH-TASK-155 | 业务规则 | 账期与时间、归属谓词、高水位、`S(M)` 与期初期末、发生额分类 |
| AIH-TASK-156 | 业务规则 | 用量统计、快照、自检、`generate_statement` |
| AIH-TASK-157 | 任务 | `generate_due` catch-up、Celery 任务与 beat |
| AIH-TASK-158 | 告警 | `usage_processing_backlog` 两段新判据 |
| AIH-TASK-159 | 接口 | 管理端列表、详情、手动生成、审计动作 |
| AIH-TASK-160 | 接口 | 客户列表与详情 |
| AIH-TASK-161 | 前端 | 管理端对账单页与补生成按钮 |
| AIH-TASK-162 | 前端 | 门户对账单页 |

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
| v1 | 2026-11-04 | 初稿 | — |
| v2 | 2026-11-04 | §2「有界扫描」改为「候选扫描」：期末、首期期初与 `S(M)` 段二的候选对 `AI_USAGE` 增加按事件 `occ` 的第二段（`C2`），每行再按完整谓词判定，不再假设 `created_at ≥ occurred_at`；§5「自检不等」写明只由数据或代码缺陷触发与恢复方式；§7 增加 `created_at` 早于 `occ` 跨月与扫描完整性两行；§10 写明不做的假设 | #565 v1 审查阻断项：`occurred_at` 可比服务端时间晚 300 秒，旧下界会漏行、自检永久失败 |
| v3 | 2026-11-04 | §5「自检不等」改为「永久停出」的已知限制：写明最终状态、不可重试与 runbook 处置；§10 记 Kelvin 拍板；§2「自检」日志带检查编号；§7 增加「永久停出」一行，「catch-up」补编号碰撞，「管理端」补没有到期账期、没有钱包与中间一期失败 | #565 v2 审查阻断项：数据缺陷下 v2 写的「修数据后自动补上」走不通；五问第 4 条的测试缺口 |
| v4 | 2026-11-04 | §2「告警」缺失对账单只在 `cutoff(M) + 6h ≤ now < cutoff(M) + 30h` 的窗口内计数并写明理由；§1 完成标准 6、§3 INV-14、§5 各行的告警一栏、§7「告警：缺失」、§10 已拍板随之改 | AIH-TASK-158 预审 DESIGN_DEFECT：永久停出让缺失告警一直 P2，`usage_processing_backlog` 的 Healthchecks 检查一直 down，之后的 P1 无人知晓 |
| v5 | 2026-11-04 | 对账单引用税务政策：表加 `tax_policy_version_id`（NOT NULL、FK RESTRICT、显式索引）；`generate_statement` 第 4 步按 `cutoff(M)` 取生效版本，取不到或校验失败 → `StatementTaxPolicyUnavailable`；快照加 `tax_notice{zh, en}`；§1 完成标准 4、§3 INV-6 改为「是」、§4 锁、§5 新增一行、§7 新增「税务说明」、前端显示说明随之改 | #565 v4 审查阻断项：ADR-0008 §5 要求说明随税务政策版本快照进每张对账单，spec §45.1 `REQ-TAX-001` |
| v6 | 2026-11-04 | §5「生成之后才提交的旧行」的告警一栏改为「无」并在 §10 记为假设（v5 误写成由晚到占比告警兜住）；§2「告警」写明 1% 阈值用精确 `Decimal` 比值判定、舍入后的百分比只用于显示 | AIH-TASK-158 预审 DESIGN_DEFECT：这类行 `t(r) = U`，晚到判定按定义不计它；同一预审的 UNDECIDED |

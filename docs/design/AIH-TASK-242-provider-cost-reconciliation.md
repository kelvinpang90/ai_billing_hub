# AIH-TASK-242..246、250、251 设计：Phase 8 供应商账单成本对账（已批准 v1）

> **来源**：设计闸门 Issue #755。本文件是 `APPROVED: design v1` 那一版正文的**逐字副本**（送审时状态行为 `READY_FOR_REVIEW`）。审查：Codex 额度不可用期间由 Claude Code 审查。原为一份设计，因正文超过 Issue 字数上限拆成两份：本文件是对账本身（AIH-TASK-242..246、250、251），分摊进毛利分析见 `docs/design/AIH-TASK-247-provider-cost-allocation.md`（#756）。v1 一次审查通过、没有阻断项；送审前多轮登记预审报出的问题已改进 v1。产品决定（UTC 自然月、录入不含税、分摊一起登记、首个对账月 2026-12 以 12-01 前换好专用密钥为前提）由 Kelvin 2026-11-16 拍板。
> 放进仓库，是因为 OpenClaw Worker 在沙箱里不联网、读不到 GitHub Issue。
> **实现以本文件为准**；与 Issue 不一致时，以 Issue #755 上被批准的 v1 为准。设计要改，就回到 Issue 升版本、重新过闸门，不要直接改本文件。

状态：`READY_FOR_REVIEW` · 设计版本：`v1` · 目标 PR：待开

## 0. 准入判定

| 任务涉及 | 要填的范围 |
| --- | --- |
| 钱包 / 账本 / 定价 / 汇率 / 支付 / 幂等 / 状态机 | **全部章节** |
| 用量摄取 / 认证与会话（集成侧与平台侧）/ Webhook | §1–§7（**§6 不可省**） |
| 前端 / 文档 / CI / 脚本 | **不走本闸门**，直接开 PR |
| 既不碰钱、也不碰用量摄取 / 认证与会话 / Webhook 的后端改动 | **不走本闸门**，直接开 PR |

本任务档位：`全部`。理由：新建一张**不可变的财务记录表**（供应商实际成本、币种、汇率版本引用、换算后的 MYR、估算快照），引入「当前版本 / 已被取代」的更正链（只进不退的状态机），按已发布汇率版本做币种换算。不动钱包、账本、计费额与对账单，这些「不触碰」在 §3 逐条说明。把对账后成本按比例分摊进毛利分析（AIH-TASK-247..249）是另一份设计 `docs/design/AIH-TASK-247-provider-cost-allocation.md`（下称「分摊设计」），建立在本设计之上。

---

状态：`READY_FOR_REVIEW`
负责人：Claude
**设计版本**：`v1`
对应需求：spec §14（估算成本与对账后成本分开；对账追加可审计记录、不覆盖估算快照、不自动 rebill；不能确定地落到逐请求时只在供应商 / 账期层面对账、报告合计差额）、§17 / §17.1（汇率按已发布版本换算、保留快照）、§19（只改估算 / 对账成本不改客户钱包）、§66（审计）、§74.5（`provider_cost_reconciliations`，只追加）、§109（时间）、§120（运维告警）、§131（Phase 8「provider invoice cost reconciliation」）；Invariant 4、5、6、7、10、12、13；ADR-0003、ADR-0016
目标 PR：待开（拆成 AIH-TASK-242..246、250、251，见 §11；AIH-TASK-247..249 见分摊设计）

## 1. 目标与边界

### 要解决的问题

1. **成本只有估算**：已处理事件冻结了按处理时价格与汇率版本算出的 `provider_source_cost` 与 `estimated_provider_cost_myr`（`app/models/usage.py:288-315`）。折扣、税、调价漏录都不会反映出来（ADR-0016「后果」第 1 条指明对账后成本是纠正途径）。
2. **没有实际成本的入口**：`provider_cost_reconciliations`（spec §74.5）不存在；`docs/TODO.md:381` 未勾；毛利分析设计把对账后成本推到本 Phase（`docs/design/AIH-TASK-221-margin-analytics.md:50`）。
3. **看不出本平台自己的供应商花费**：AI 模块用一把从文件读取的 Anthropic 密钥（`app/core/config.py:327-328`，`docs/runbook.md:103-106`），这把密钥现在多个项目共用，供应商侧的花费分不出本平台那一份。

### 已定（来自任务说明）

- 生产上唯一有 AI 用量的是内部租户（`INTERNAL_METERED_ONLY`）的 crm 项目，经 AI 模块调 Anthropic。
- Kelvin 为本平台单独生成一把 Anthropic 密钥（建议放在 Console 里本平台专用的 workspace），之后本平台的 Anthropic 花费才能单独看到；对账只能从换钥之后的第一个完整自然月开始。
- 沿用 ADR-0016 的思路：V1 **不接**供应商账单或成本 API，由管理员每月按供应商账单或 Console 按 workspace 的成本页**手工录入**实际成本。
- 管理端能录入、查看每月对账结果与差额率；Healthchecks 检查已用满，告警只能并进已有维度（`docs/TODO.md:48`）。
- 客户永远看不到（INV-7）；对账不改任何已计费金额、不动钱包与账本（Rebill 不在本批）。

### Kelvin 2026-11-16 的决定

| 问题 | 决定 |
| --- | --- |
| 对账期间 | **UTC 自然月**（`[YYYY-MM-01 00:00 UTC, 下月 1 日 00:00 UTC)`）。Kelvin 会在 Console 上顺手核实成本页按 UTC 划日；若不是，本设计升版本改 `reconciliation_period` |
| 录入什么数 | **不含税**：录 Console 成本页上本平台专用 workspace 当月的消耗额（来源币种原值） |
| 毛利分析分摊 | 按估算成本比例分摊进毛利分析，**与本批一起登记**、不推迟（规则在分摊设计） |
| 首个对账月 | `2026-12`，前提是 Kelvin 在 **2026-12-01 00:00 UTC（马来西亚 08:00）之前**换好专用密钥 |

### 我定的（未单独问，审查者重点看，§10 也列出）

1. **对账范围 = 经 AI 模块发出的调用**：判据是事件的 `event_id` 出现在 `ai_usage_outbox` 里；集成方用自己密钥上报的同供应商用量不在范围内。
2. **期间 = UTC 自然月**（Kelvin 已定，见上表），与 Console 成本页的日边界对齐；**不是** ADR-0003 的马来西亚月。毛利分析按马来西亚日 / 月分组时怎样取系数见分摊设计。
3. **粒度 = 供应商 × UTC 月 × 范围，一个合计数**，不分模型。
4. **没有草稿态**：先预览（不写）再确认（写一行不可变记录）；更正 = 追加取代行，**必填理由**；行不可改、不可删。
5. **换算汇率 = 该期间最后一刻（`period_end − 1 微秒`）生效的已发布汇率版本**，记录版本引用与汇率值。
6. **差额不进 Healthchecks**（页面高亮）；只把「上月满 9 天未对账」作为 72 小时 P2 并进 `usage_processing_backlog`。

### 可观察的完成标准

1. 管理员在「Provider costs」页选供应商 `anthropic`，看到从首个对账月起每个已结束的 UTC 月一行：已对账（实际成本、换算后 MYR、估算、差额与差额率、超阈值高亮）或未对账。
2. 录入币种、实际金额、来源说明后「预览」看到估算、换算后 MYR、两种差额与差额率、诊断计数，不写库；「确认」写一行不可变记录与一条审计；预览后估算变了则 409。
3. 「更正」填新数据与理由，确认后新行为当前、旧行为「已被取代」，都保留、不可改；并发取代时后提交者 409。
4. 数据库层：MySQL 上对这张表的任何 `UPDATE`、`DELETE` 被触发器拒绝；同一供应商、范围、月份至多一个根记录，每行至多被取代一次。
5. 记录写入后，用量事件的全部快照列、钱包、账本、对账单逐字节不变。
6. 任何非管理端接口与客户侧模块看不到新字段（扩展 AIH-TASK-222 的 INV-7 守卫）。
7. 上一个完整月过了 9 天仍未对账时，`usage_processing_backlog` 在 72 小时内报 P2，摘要只有供应商代码与月份。

### 明确不做什么

- **不做 Rebill / Reprocess**：不改 `usage_events` 的任何列（包括估算快照），不写钱包、账本、对账单，不发补偿交易（spec §19 最后一段）。
- **不接**供应商的账单、用量或成本 API（含 Anthropic Admin API），不抓页面（ADR-0016 的思路）；不保存供应商 Admin 密钥。
- **不分模型、不分项目**录入实际成本；不把分摊额写进任何表（spec §14「不编造逐请求精度」）。毛利分析里的分摊见分摊设计。
- **不做草稿保存、四眼审批**（§10「我定的」第 3 条）。
- **不处理税**：外国数字服务税等供应商代收或另行征收的税不录入、不进对账后成本与毛利（Kelvin 2026-11-16 定：录入额不含税）；要计入需另开任务、另过闸门。
- **不处理**换钥本身：换钥是运维步骤（AIH-TASK-251 写进 runbook），代码只认一个常量「首个对账月」。
- **不做** OpenAI 或其他供应商的对账入口特化：接口按供应商代码通用，但范围只有「AI 模块」一种，AI 模块现在只调 Anthropic（`app/ai_api/client_rules.py:97`）。
- 不改 `app/ai_api/` 下任何文件；不加依赖；不新增 Healthchecks 检查、不新增告警维度。

### 现有行为与证据

- **事件快照**：来源币种、来源成本 DECIMAL(20,8)、汇率版本与汇率 DECIMAL(24,10)、估算 MYR、计费模式（`app/models/usage.py:288-315`）；`PROCESSED` 时齐全（:179-190），MYR 原币时两个汇率列为空。`event_id` 是 `VARCHAR(64)`、`utf8mb4_0900_bin`（:89、:92、:235），唯一 `uq_usage_events_event_id`（:376）；按状态与发生时刻的区间索引 `ix_usage_events_status_occurred`（:378）。
- **AI 模块 outbox**：`ai_usage_outbox.event_id` 是 `CHAR(26)`、`ascii_bin`（`app/ai_api/outbox.py:78`），唯一 `uq_ai_usage_outbox_event_id`（:110）；`created_at` 是微秒 `DATETIME(6)`（:94）；状态 `PENDING / SENDING / DELIVERED / DEAD / HELD / DISCARDED`（:46-57）。`app/ai_api/` 里没有删除 outbox 行的代码（全目录无 `delete(`，§10 假设 2）。outbox 的 `event_id` 就是用量事件的 `event_id`（spec §141.4）。
- **AI 模块边界**：只有 `REVERSE_EXCEPTIONS` 里的模块可以 import `app.ai_api`（`tests/backend/test_ai_module_boundary.py:47-55`）；已批准的先例是只按表名与列名引用（`app/services/account_closure.py:84-87`），本批照做。
- **汇率与舍入**：`resolve_fx_rate`（`app/repositories/fx_rates.py:344-389`，加锁读，调用方先 `lock_fx_rates_shared` :92）；`quantize_money`（`app/models/base.py:33-45`，8 位 `ROUND_HALF_UP`）；`PRICING_CONTEXT`（`app/services/pricing_engine.py:76`，50 位）。
- **先例**：不可变表与迁移写法照 `alembic/versions/20261109_0039_payment_receipts.py`；「同组至多一个」的生成列 + 唯一索引照 `fx_rate_versions.open_slot`（`app/models/fx_rates.py:158`）；首期常量照 `FIRST_STATEMENT_PERIOD`（`app/services/statements.py:65`）。
- **审计**：`record_audit`（`app/services/auth.py:131-165`）、`AuditAction`（`app/models/auth.py:181`，`native_enum=False`）；前端清单 `frontend/src/api/adminAudit.ts:102`。
- **告警**：`DIMENSIONS`（`app/services/alerts.py:958-974`），摘要只许计数、日期与币种（:19-23）；`usage_processing_backlog` 恰好五段（:559-581），对账单缺失那段用有限窗口（:424-466），因为 Healthchecks 只在翻转时通知、持续红会遮住同一检查的后续故障（`docs/TODO.md:113`）。
- **INV-7 守卫**：`tests/backend/test_internal_cost_visibility.py` 由 AIH-TASK-222（毛利分析设计 `docs/design/AIH-TASK-221-margin-analytics.md`）新建，是 AIH-TASK-245 的前提。

## 2. 设计概要

### 数据流

```text
POST …/preview（ADMIN）→ 校验 → 一个事务（回滚）：理由 → 月份 → 供应商 → 取代目标
  → [非 MYR：fx 共享锁 → 月末汇率] → 换算与范围 → 聚合 → 币种一致 → 200（不写库）
POST …（确认）→ 同上 + 期望值比对（409）→ INSERT 记录 + 审计 → 提交 → 201
GET …/months、GET …/{id}（更正链、确认后估算变化）
告警：usage_processing_backlog 追加一段，上月满 9 天未对账 → 72 小时 P2
（不写钱包 / 账本 / 对账单 / 用量事件，无出站事件）
```

### 口径（写进 `docs/api.md`、页面）

| 量 | 定义 |
| --- | --- |
| 期间 `period` | 供应商 × UTC 自然月：`period_start = YYYY-MM-01T00:00:00Z`，`period_end` = 下月 1 日 0 点 UTC，左闭右开 |
| 范围内事件 `scope(p, period)` | `usage_events` 里 `provider_id = p.id`、`occurred_at ∈ [period_start, period_end)`、且 `EXISTS (ai_usage_outbox o WHERE o.event_id = usage_events.event_id)` |
| 估算事件数 `estimated_event_count` | 范围内 `status = 'PROCESSED'` 的条数 |
| 估算来源币种成本 `estimated_source_cost` | 范围内已处理事件里 `provider_source_currency = 录入币种` 的 `provider_source_cost` 之和 |
| 估算 MYR 成本 `estimated_cost_myr` | 范围内已处理事件的 `estimated_provider_cost_myr` 之和 |
| 实际来源币种成本 `source_total` | 管理员按 Console 成本页 / 供应商账单录入（≥ 0，至多 8 位小数） |
| 换算后的实际 MYR 成本 `reconciled_cost_myr` | 录入币种为 MYR 时等于 `source_total`；否则 `quantize_money(source_total × fx_rate_applied)`（`PRICING_CONTEXT` 内乘），`fx_rate_applied` 取自 `resolve_fx_rate(录入币种, period_end − 1µs)` |
| 来源币种差额 / 差额率 | `source_variance = source_total − estimated_source_cost`；`source_variance_rate = source_variance ÷ estimated_source_cost`，分母为 0 时 null |
| MYR 差额 / 差额率 | `myr_variance = reconciled_cost_myr − estimated_cost_myr`；`myr_variance_rate` 同上 |
| 超阈值 `exceeds_threshold` | `|source_variance_rate| ≥ 0.05`；或 `estimated_source_cost = 0` 且 `source_total > 0` |
| 诊断计数（只读，快照进记录） | `unprocessed_event_count`：`occurred_at` 在期间内、属于 AI 模块（同一 `EXISTS`）、`status <> 'PROCESSED'` 的事件数（**不限供应商**：未知模型的事件可能没有 `provider_id`）；`outbox_unsettled_count`：`created_at` 在期间内、状态 `PENDING / SENDING / HELD / DEAD` 的 outbox 行数；`outbox_discarded_count`：同一区间状态 `DISCARDED` 的行数 |

**差额率阈值的依据**取来源币种：它只反映供应商侧的差异（漏录调价、折扣、漏计事件），不混入月内汇率波动。MYR 差额照样显示。

**诊断计数的意义**（页面写明）：未处理与未投递的调用已被供应商计费、估算里还没有，让差额偏正；已丢弃的永远没有估算；outbox 按 `created_at` 归期是近似。

### 数据库（迁移 0041，AIH-TASK-242）

新表 `provider_cost_reconciliations`（spec §74.5；行**只插入**）：

| 列 | 类型 | 约束 / 说明 |
| --- | --- | --- |
| `id` | BIGINT 自增主键 | |
| `public_id` | CHAR(36) | 唯一 `uq_provider_cost_reconciliations_public_id` |
| `provider_id` | BIGINT | 外键 `ai_providers.id` RESTRICT |
| `scope` | VARCHAR(16) | CHECK `scope IN ('AI_MODULE')` |
| `billing_month` | CHAR(7) | `YYYY-MM`（格式应用层校验，同 `base_currency` 不做正则 CHECK 的惯例，`app/models/fx_rates.py:113`） |
| `period_start`、`period_end` | DATETIME | 不带时区的 UTC；CHECK `period_start < period_end` |
| `source_document_reference` | VARCHAR(255) | 非空；来源说明（例：工作区名称 + 月份 + 页面或账单号）；不写合同价 |
| `source_currency` | CHAR(3) | |
| `source_total` | DECIMAL(20,8) | CHECK `>= 0` |
| `fx_rate_version_id` | BIGINT NULL | 外键 `fx_rate_versions.id` RESTRICT |
| `fx_rate_applied` | DECIMAL(24,10) NULL | |
| `reconciled_cost_myr` | DECIMAL(20,8) | CHECK `>= 0` |
| `coverage_method` | VARCHAR(32) | CHECK `coverage_method IN ('PERIOD_AGGREGATE')` |
| `estimated_event_count`、`unprocessed_event_count`、`outbox_unsettled_count`、`outbox_discarded_count` | BIGINT | 各自 CHECK `>= 0` |
| `estimated_source_cost`、`estimated_cost_myr` | DECIMAL(20,8) | 各自 CHECK `>= 0` |
| `supersedes_id` | BIGINT NULL | 被本行取代的那一行 |
| `reason` | VARCHAR(255) NULL | 更正理由 |
| `created_by` | BIGINT | 外键 `users.id` RESTRICT |
| `created_at` | DATETIME | 整秒 UTC |
| `root_slot` | INT，STORED 生成列 | `CASE WHEN supersedes_id IS NULL THEN 1 END` |

约束与索引（名字照此）：

- `ck_provider_cost_reconciliations_fx`：`(source_currency = 'MYR' AND fx_rate_version_id IS NULL AND fx_rate_applied IS NULL AND reconciled_cost_myr = source_total) OR (source_currency <> 'MYR' AND fx_rate_version_id IS NOT NULL AND fx_rate_applied IS NOT NULL AND fx_rate_applied > 0)`。逐列写 `IS NOT NULL`，不用行构造器（同 `app/models/usage.py:178` 注释的理由）。
- `ck_provider_cost_reconciliations_reason`：`(supersedes_id IS NULL AND reason IS NULL) OR (supersedes_id IS NOT NULL AND reason IS NOT NULL)`。
- 其余 11 条 CHECK，名字为 `ck_provider_cost_reconciliations_` 加：`scope`（`IN ('AI_MODULE')`）、`coverage_method`（`IN ('PERIOD_AGGREGATE')`）、`period`（`period_start < period_end`）；以下八条都是对同名列 `>= 0`：`source_total`、`reconciled_cost`（列 `reconciled_cost_myr`）、`estimated_source_cost`、`estimated_cost_myr`、`estimated_event_count`、`unprocessed_event_count`、`outbox_unsettled_count`、`outbox_discarded_count`。连同上面两条共 13 条。
- 唯一 `ux_provider_cost_reconciliations_root (provider_id, scope, billing_month, root_slot)`：同一供应商、范围、月份**至多一个根记录**（`root_slot` 对非根为 NULL，唯一索引不管 NULL）。它也是 `provider_id` 外键用的索引。
- 唯一 `uq_provider_cost_reconciliations_supersedes (supersedes_id, provider_id, scope, billing_month)`：① 复合自引用外键在子表上的索引（前导列同名同序）；② **每行至多被取代一次**：外键保证取代行的后三列等于被取代行的，所以四列唯一等价于 `supersedes_id` 唯一；根（NULL）不受约束。不另建单列索引。于是每月的记录是一条单链，恰好一个链头（当前版本）。
- 唯一 `uq_provider_cost_reconciliations_chain_key (id, provider_id, scope, billing_month)`：复合自引用外键在**父表**一侧引用的键。
- 普通外键（名字写死，都是 `ON DELETE RESTRICT`）：`fk_provider_cost_reconciliations_provider_id`（`provider_id` → `ai_providers.id`）、`fk_provider_cost_reconciliations_fx_rate_version_id`（`fx_rate_version_id` → `fx_rate_versions.id`）、`fk_provider_cost_reconciliations_created_by`（`created_by` → `users.id`）。
- 外键 `fk_provider_cost_reconciliations_supersedes (supersedes_id, provider_id, scope, billing_month)` → `(id, provider_id, scope, billing_month)` RESTRICT：取代者与被取代者**必是同一供应商、范围、月份**；`supersedes_id` 为 NULL 时 MATCH SIMPLE 不检查。
- `ix_provider_cost_reconciliations_fx_rate_version_id`、`ix_provider_cost_reconciliations_created_by`：显式声明，供外键使用（0039 同一理由）。
- **外键在子表上的索引**（MySQL 要求前导列一致，缺了会自建隐藏索引）：`provider_id` → `ux_…_root`；`fx_rate_version_id` → `ix_…_fx_rate_version_id`；`created_by` → `ix_…_created_by`；复合自引用外键 → `uq_…_supersedes`。迁移测试断言 MySQL 上除主键外的索引恰好是 `ux_…_root`、`uq_…_public_id`、`uq_…_supersedes`、`uq_…_chain_key`、`ix_…_fx_rate_version_id`、`ix_…_created_by` 六个（前缀均为 `provider_cost_reconciliations`）。
- MySQL 触发器 `trg_provider_cost_reconciliations_before_update`、`trg_provider_cost_reconciliations_before_delete`：**无条件** `SIGNAL SQLSTATE '45000'`（`provider_cost_reconciliations rows are immutable`）。SQLite 不建。

spec §74.5 的 `billing_period` 落为 `billing_month` + `period_start` / `period_end`（把口径写死在行上）；`metadata_json` **不建**：所有字段都是结构化列，没有要放进去的东西（§10「我定的」第 5 条）。

「当前版本」= 没有任何行的 `supersedes_id` 指向它；「已被取代」= 有。没有状态列，不需要改行。

迁移 `revision = "0041_provider_cost_recon"`（24 字符，≤ 32，见 `alembic/versions/20261018_0023_wallet_ledger_reference_index.py:53`）、`down_revision = "0040_reset_request_index"`（AIH-TASK-231 的迁移）。写法照 0039：存在性与形状逐项检查、`log_bin_trust_function_creators` 预检、一条 `CREATE TABLE`、缺哪个触发器建哪个、`--sql` 离线输出；降级只删空表（有行就抛错，处理方法是只回滚镜像）。

### 仓储（`app/repositories/provider_cost_reconciliations.py`，AIH-TASK-243）

- `_AI_USAGE_OUTBOX = table("ai_usage_outbox", column("event_id"), column("status"), column("created_at"))`，**不 import `app.ai_api`**（`app/services/account_closure.py:84-87` 的先例与注释照抄）。
- `ai_module_origin() -> ColumnElement[bool]`：`exists().where(_AI_USAGE_OUTBOX.c.event_id == UsageEvent.event_id)`。直接等值比较；两列字符集不同（§1「现有行为」），MySQL 可能不能用 outbox 的唯一索引做逐行探测。PR 正文写明 MySQL `EXPLAIN` 的实际计划（照 AIH-TASK-073 / 221 的做法）；结果正确性由 §7 的 MySQL 用例钉住，不在设计里预设计划。
- `scope_estimate(session, *, provider_id, period_start, period_end, source_currency) -> ScopeEstimate`：一条 SELECT，`status = 'PROCESSED'`、`provider_id`、`occurred_at` 左闭右开、`ai_module_origin()`，返回 `event_count`、`estimated_source_cost`（`SUM(CASE WHEN provider_source_currency = :cur THEN provider_source_cost ELSE 0 END)`）、`estimated_cost_myr`、`currency_mismatch_count`（`SUM(CASE WHEN provider_source_currency <> :cur THEN 1 ELSE 0 END)`），全部 `COALESCE(…, 0)`。
- `scope_estimated_myr(session, *, provider_id, period_start, period_end) -> Decimal`：同条件只求 `estimated_provider_cost_myr` 之和（分摊设计用）。
- `unprocessed_count(session, *, period_start, period_end) -> int`：`status <> 'PROCESSED'`、`occurred_at` 区间、`ai_module_origin()`。
- `outbox_counts(session, *, period_start, period_end) -> tuple[int, int]`：`ai_usage_outbox` 按 `created_at` 左闭右开，`(unsettled, discarded)`。
- `scope_providers(session, *, period_start, period_end) -> list[tuple[int, str]]`：一条 `SELECT DISTINCT usage_events.provider_id, ai_providers.code`（连 `ai_providers`），条件 `status = 'PROCESSED'`、`occurred_at` 左闭右开、`ai_module_origin()`，按 `code` 升序（告警用，AIH-TASK-250）。
- `current_record(session, *, provider_id, scope, billing_month) -> ProviderCostReconciliation | None`：链头（`NOT EXISTS` 后继）。
- `get_by_public_id(session, public_id)`、`chain(session, *, provider_id, scope, billing_month) -> list[…]`（按 `id` 升序）、`has_successor(session, record_id) -> bool`。
- `current_records_overlapping(session, *, start, end) -> list[tuple[ProviderCostReconciliation, str]]`：**一条** SELECT，连 `ai_providers` 取 `code`，返回 `(链头行, 供应商代码)`；条件 `period_start < end AND period_end > start` 且没有后继，按 `provider_id`、`period_start` 升序。供应商代码就从这里来，不另查（分摊设计的查询预算依赖这一点）。
- `insert_record(session, **fields) -> ProviderCostReconciliation`：`add` + `flush`；唯一约束冲突的 `IntegrityError` 原样抛给服务层。
- 全部只用可移植 SQL（`COUNT`、`SUM`、`CASE`、`EXISTS`、比较）；只读函数不加锁、不提交。

### 服务（`app/services/provider_cost_reconciliation.py`，AIH-TASK-244）

常量：`FIRST_RECONCILIATION_MONTH: Final = "2026-12"`（Kelvin 已定，前提见 §10「已定」第 4 条）、`SCOPE_AI_MODULE = "AI_MODULE"`、`COVERAGE_PERIOD_AGGREGATE = "PERIOD_AGGREGATE"`、`VARIANCE_THRESHOLD = Decimal("0.05")`、`MONTHS_SHOWN = 24`。

- `reconciliation_period(label) -> (start, end)`：`YYYY-MM`（年份 2000–2999、月 1–12，否则 `ValueError`）→ UTC 月首与下月首。
- `preview(factory, *, provider_code, billing_month, source_currency, source_total, source_document_reference, supersedes_id, reason, now) -> ReconciliationView`：入参与 `confirm` 相同，只少 `actor`、`context` 与两个期望值；视图里的 `source_document_reference`、`supersedes_id`、`reason` 就是入参（规范化之后：去首尾空白）。
- `confirm(factory, *, actor, context, provider_code, billing_month, source_currency, source_total, source_document_reference, supersedes_id, reason, expected_event_count, expected_estimated_source_cost, now) -> ReconciliationView`
- `months_overview(factory, *, provider_code, now) -> list[MonthView]`：供应商代码查不到 → 404 `AI_PROVIDER_NOT_FOUND`（先于任何别的查询）；从 `FIRST_RECONCILIATION_MONTH` 到最近一个已结束的 UTC 月（`period_end ≤ now`），倒序、至多 `MONTHS_SHOWN` 个；每个月附链头视图或 null。首月还没结束时为空列表。
- `get_detail(factory, public_id, *, now) -> ReconciliationDetail`：找不到 → 404 `PROVIDER_COST_RECONCILIATION_NOT_FOUND`。返回该行、整条链（`id` 升序，带 `is_current`）与 `live_estimate`（按该行的供应商、期间与币种重算 `scope_estimate` 的四个值与 `unprocessed_count`）。`estimate_changed_since_confirmation` 为真当且仅当实时的事件数、来源成本、MYR 估算任一与快照不等，**或**实时 `currency_mismatch_count > 0`；未处理数不参与判定。只读、不加锁、不取汇率、不重算差额。
- `allocate(covered_estimate, reconciled_cost_myr, scope_estimated_myr) -> Decimal | None`：纯函数，`PRICING_CONTEXT` 内 `covered × R ÷ E`，**不舍入**；`E = 0` 时 None（分摊设计的 AIH-TASK-248 用）。

**检查顺序**（`preview` 与 `confirm` 共用；第一个不过的就返回）：

1. （接口层）请求体校验 → 422 `VALIDATION_ERROR`，只列字段名。请求体里 `provider_code`（1–64）与 `billing_month`（1–7）只限长度、不加正则，格式与取值由第 2、3 步判定。服务层**不重复**校验 `source_total`、`source_currency`、`source_document_reference` 的格式（由 schema 保证），自己的校验只有第 1a、5a 步。
1a. `preview` 与 `confirm` 都做（服务层再挡一次，不依赖接口层）：`reason` 去首尾空白后为空串或长于 255，或与 `supersedes_id` 不配对（带 `supersedes_id` 而 `reason` 为 None，或不带 `supersedes_id` 而 `reason` 不为 None）→ 422 `RECONCILIATION_REASON_INVALID`。排在第 2 步之前，不会走到插入撞 CHECK。
2. `billing_month` 解析失败、早于 `FIRST_RECONCILIATION_MONTH`、或 `period_end > now`（月份没结束）→ 422 `RECONCILIATION_MONTH_INVALID`。
3. `provider_code` 在 `ai_providers` 里找不到 → 404 `AI_PROVIDER_NOT_FOUND`（复用 `app/services/ai_catalog.py:78` 的 `ProviderNotFound`）。
4. 带 `supersedes_id`：找不到 → 404 `PROVIDER_COST_RECONCILIATION_NOT_FOUND`；供应商、范围或月份与请求不同 → 422 `RECONCILIATION_SUPERSEDES_MISMATCH`；已有后继 → 409 `RECONCILIATION_NOT_CURRENT`。不带：该月已有根记录 → 409 `RECONCILIATION_EXISTS`。**所有错误都不带 `details`**（§107 信封与 `AppError` 只有 `code`、`message`，不扩展 `app/core/errors.py`、`app/schemas/envelope.py`）；前端收到 409 后重拉月份总览。
5. `source_currency ≠ MYR`：`lock_fx_rates_shared` → `resolve_fx_rate(source_currency, period_end − 1µs)`；None → 422 `FX_RATE_UNAVAILABLE`。
5a. `preview` 与 `confirm` 都做：按「金额与精度」算出 `reconciled_cost_myr`，它 ≥ `10^12`（放不进 `DECIMAL(20,8)`）→ 422 `RECONCILIATION_AMOUNT_OUT_OF_RANGE`，不写行（确认不会因溢出 MySQL 1264 落到 500）。
6. 聚合（`scope_estimate`、`unprocessed_count`、`outbox_counts`）；`currency_mismatch_count > 0` → 422 `SOURCE_CURRENCY_MISMATCH`（范围内有事件的来源币种与录入币种不同，比较没有意义）。
7. 仅 `confirm`：`expected_event_count` 或 `expected_estimated_source_cost` 与第 6 步不等 → 409 `RECONCILIATION_ESTIMATE_CHANGED`（预览之后有事件被处理，重新预览）。
8. 仅 `confirm`：算 `reconciled_cost_myr`，`insert_record`，同一事务 `record_audit`，再调模块级测试钩子 `_after_audit_before_commit(session)`（默认什么都不做，只供并发用例 monkeypatch 来阻塞，先例是 `tests/backend/test_payment_create.py:561` 对服务模块属性的 monkeypatch），提交 → 201。插入时的 `IntegrityError` → 回滚，先判是不是唯一冲突：MySQL 为 `orig.args[0] == 1062`，SQLite 为错误文本以 `UNIQUE constraint failed` 开头；不是就原样抛出 → 500。是则**按请求形状映射、不看约束名**（SQLite 文本里没有索引名）：不带 `supersedes_id` 只可能撞 `ux_…_root` → 409 `RECONCILIATION_EXISTS`；带的只可能撞 `uq_…_supersedes` → 409 `RECONCILIATION_NOT_CURRENT`（新 UUID 撞 `public_id` 不予考虑）。回滚后不再读库。

`preview` 跑第 2–6 步，在同一个事务里，结束时回滚（不写任何行）。第 4 步是普通读，第 8 步的唯一约束才是并发仲裁（§4）。

**服务返回的视图**（`app/services/provider_cost_reconciliation.py` 里的 `@dataclass(frozen=True)`；**服务只给类型化的值，所有字符串格式化都在接口层 schema 里做**，照 `FxRateVersionView` 的先例，`app/services/fx_rates.py:155-173`）：

```python
@dataclass(frozen=True)
class ReconciliationView:
    reconciliation_id: str | None        # public_id；预览为 None
    provider_code: str
    scope: str
    billing_month: str                   # "YYYY-MM"
    period_start: dt.datetime            # 不带时区的 UTC
    period_end: dt.datetime
    source_document_reference: str
    source_currency: str
    source_total: Decimal
    fx_rate_version_id: str | None       # 汇率版本的 public_id；MYR 为 None
    fx_rate_applied: Decimal | None      # MYR 为 None
    fx_rate_moment: dt.datetime | None   # period_end − 1µs；MYR 为 None
    reconciled_cost_myr: Decimal
    estimated_event_count: int
    estimated_source_cost: Decimal
    estimated_cost_myr: Decimal
    source_variance: Decimal
    source_variance_rate: Decimal | None
    myr_variance: Decimal
    myr_variance_rate: Decimal | None
    exceeds_threshold: bool
    unprocessed_event_count: int
    outbox_unsettled_count: int
    outbox_discarded_count: int
    coverage_method: str
    supersedes_id: str | None            # 被取代行的 public_id
    reason: str | None
    is_current: bool | None              # 预览为 None
    created_at: dt.datetime | None       # 预览为 None
    created_by_email: str | None         # 预览为 None

@dataclass(frozen=True)
class MonthView:
    billing_month: str
    period_start: dt.datetime
    period_end: dt.datetime
    status: str                          # "RECONCILED" | "NOT_RECONCILED"
    current: ReconciliationView | None

@dataclass(frozen=True)
class LiveEstimate:
    estimated_event_count: int
    estimated_source_cost: Decimal
    estimated_cost_myr: Decimal
    currency_mismatch_count: int
    unprocessed_event_count: int

@dataclass(frozen=True)
class ReconciliationDetail:
    reconciliation: ReconciliationView
    chain: tuple[ReconciliationView, ...]   # id 升序
    live_estimate: LiveEstimate
    estimate_changed_since_confirmation: bool
```

`months_overview` 返回 `list[MonthView]`。`period_basis`（恒 `utc_calendar_month`）与 `variance_threshold`（`VARIANCE_THRESHOLD`）是常量，由接口层 schema 补进响应，不放进视图。

**审计**：新动作 `PROVIDER_COST_RECONCILIATION_CREATE`（按 `PROJECT_CREATE` 的先例补）；`entity_type = provider_cost_reconciliation`、`entity_id = public_id`；`before_state` 为 null（根）或被取代行的 `{public_id, source_currency, source_total, reconciled_cost_myr}`；`after_state` 为 `{public_id, provider_code, scope, billing_month, source_currency, source_total, fx_rate_version_id（汇率版本的 public_id 或 null）, fx_rate_applied, reconciled_cost_myr, estimated_event_count, estimated_source_cost, estimated_cost_myr, supersedes_id（public_id 或 null）}`；前后状态里的金额都用 `money_text`（`record_audit` 的 `json.dumps` 不收 `Decimal`），`fx_rate_applied` 用 `app/services/fx_rates.py:185` 的 `rate_text`（MYR 时 null），计数为整数；`reason` 列写更正理由；不写 `source_document_reference`（可能含工作区名称）。

### 管理端接口（`app/api/admin_provider_cost_reconciliations.py`，AIH-TASK-245）

全部挂在 `/api/v1/admin`，每个处理函数第一条语句 `require_admin(request)`。路径里的 id 是 `public_id`。

| 方法与路径 | 用途 | 成功 |
| --- | --- | --- |
| `GET /provider-cost-reconciliations/months?provider_code=` | 月份总览（必填 `provider_code`，`^[a-z0-9_.-]{1,64}$`，格式不符 422；格式对但 `ai_providers` 里没有 → 404 `AI_PROVIDER_NOT_FOUND`，不返回一串 `NOT_RECONCILED`） | 200，`data.items` |
| `POST /provider-cost-reconciliations/preview` | 预览，不写库 | 200 |
| `POST /provider-cost-reconciliations` | 确认 | 201 |
| `GET /provider-cost-reconciliations/{reconciliation_id}` | 详情 + 更正链 + 实时估算；路径参数不做格式校验，查不到（含不是 UUID 的值）→ 404 `PROVIDER_COST_RECONCILIATION_NOT_FOUND` | 200 |

`/months` 与 `/preview` 先于 `/{reconciliation_id}` 注册（`app/api/admin_fx_rates.py` 文件头记的同一个坑）。

请求体（`extra="forbid"`）：

```json
{
  "provider_code": "anthropic",
  "billing_month": "2026-12",
  "source_currency": "USD",
  "source_total": "11.70",
  "source_document_reference": "Console cost, platform workspace, 2026-12",
  "supersedes_id": null,
  "reason": null,
  "expected_event_count": 2,
  "expected_estimated_source_cost": "10.50000000"
}
```

- `source_total`：JSON **字符串**，`[0-9]{1,9}(\.[0-9]{1,8})?`，不经 float（照 `app/schemas/fx_rates.py:46-62` 的 `_parse_rate`，允许 0）。整数只到 9 位，使积放进 `DECIMAL(20,8)`；第 5a 步兜底。
- `source_currency`：`^[A-Z]{3}$`；`source_document_reference`：去首尾空白后 1–255（复用 `app/schemas/provider_prices.py:98-100` 的 `SourceReference`）；`reason`：同 `Reason`（:101），`supersedes_id` 非空时必填、为空时必须为 null（模型校验，422）。
- **预览与确认的请求体**：预览恰好是上面除 `expected_*` 之外的七个字段，`source_document_reference` 同样必填、`supersedes_id` 与 `reason` 的配对规则同样由模型校验（422 `VALIDATION_ERROR`）；确认恰好九个字段，`expected_*` 必填：`expected_estimated_source_cost` 是 JSON **字符串**，`[0-9]{1,12}(\.[0-9]{1,8})?`（`app/schemas/provider_prices.py:44` 的 `_AMOUNT_PATTERN`，允许 0），不经 float；`expected_event_count` 是 JSON 整数、`>= 0`、严格类型（照 `app/schemas/integration_access.py:34`），拒绝布尔与字符串；不合规 422 `VALIDATION_ERROR`。预览带 `expected_*` 是 422（`extra="forbid"`）。
- `supersedes_id`：字符串，长 1–64（复用 `app/schemas/provider_prices.py:102` 的 `PublicId`），**不校验 UUID 格式**；找不到（含不是 UUID 的值）→ 404 `PROVIDER_COST_RECONCILIATION_NOT_FOUND`。

响应视图 `ReconciliationView`（预览时 `reconciliation_id`、`created_at`、`created_by_email` 为 null，`is_current` 为 null）：

```json
{
  "reconciliation_id": "00000000-0000-0000-0000-000000000000",
  "provider_code": "anthropic", "scope": "AI_MODULE", "billing_month": "2026-12",
  "period_basis": "utc_calendar_month",
  "period_start": "2026-12-01T00:00:00", "period_end": "2027-01-01T00:00:00",
  "source_document_reference": "…",
  "source_currency": "USD", "source_total": "11.70000000",
  "fx_rate_version_id": "00000000-…", "fx_rate_applied": "4.4", "fx_rate_moment": "2026-12-31T23:59:59.999999",
  "reconciled_cost_myr": "51.48000000",
  "estimated_event_count": 2, "estimated_source_cost": "10.50000000", "estimated_cost_myr": "46.80000000",
  "source_variance": "1.20000000", "source_variance_rate": "0.1142857142857142857142857143",
  "myr_variance": "4.68000000", "myr_variance_rate": "0.1",
  "exceeds_threshold": true, "variance_threshold": "0.05",
  "unprocessed_event_count": 1, "outbox_unsettled_count": 1, "outbox_discarded_count": 1,
  "coverage_method": "PERIOD_AGGREGATE",
  "supersedes_id": null, "reason": null, "is_current": true,
  "created_at": "…", "created_by_email": "…"
}
```

- **格式化在 schema 里做**：金额用 `money_text`（8 位，`app/schemas/customers.py:340-342`）；`fx_rate_applied` 用 `rate_text`（`app/services/fx_rates.py:185`）；差额率与 `variance_threshold` 用 `ratio_text`（`app/schemas/usage_analytics.py:54-57`）；时间字段的 schema 类型是 `dt.datetime`，序列化同 `FxRateView`（`app/schemas/fx_rates.py:160-175`）：不带时区、没有 `Z`，如 `2026-12-01T00:00:00`、`2026-12-31T23:59:59.999999`（`tests/backend/test_fx_rates_api.py:320`）；全程 Decimal。
- 汇率三键：`source_currency ≠ MYR` 时 `fx_rate_version_id` 为汇率版本的 `public_id`、`fx_rate_applied` 为汇率、`fx_rate_moment` 为 `period_end − 1µs`（序列化见上一条）；`source_currency = MYR` 时**三个都为 null**；预览同一规则。
- 月份总览项：`{billing_month, period_start, period_end, status: "RECONCILED" | "NOT_RECONCILED", current: ReconciliationView | null}`。
- 详情：`{reconciliation: ReconciliationView, chain: [ReconciliationView…], live_estimate: {estimated_event_count, estimated_source_cost, estimated_cost_myr, currency_mismatch_count, unprocessed_event_count}, estimate_changed_since_confirmation: bool}`（判定规则见 §2「服务」的 `get_detail`）。
- 错误信封照 spec §107；422 不回显值。错误码：422 `VALIDATION_ERROR`、`RECONCILIATION_REASON_INVALID`、`RECONCILIATION_MONTH_INVALID`、`RECONCILIATION_SUPERSEDES_MISMATCH`、`FX_RATE_UNAVAILABLE`、`SOURCE_CURRENCY_MISMATCH`；404 `AI_PROVIDER_NOT_FOUND`、`PROVIDER_COST_RECONCILIATION_NOT_FOUND`；409 `RECONCILIATION_EXISTS`、`RECONCILIATION_NOT_CURRENT`、`RECONCILIATION_ESTIMATE_CHANGED`。

### 管理端页面（AIH-TASK-246）

- 新路由 `ROUTES.providerCosts`（`/provider-costs`），导航放在「FX rates」旁（`frontend/src/layouts/AppLayout.tsx:94` 一带），只对 ADMIN。
- 页面：供应商选择（V1 只列 `anthropic` 一个选项，值来自常量，不调目录接口）；月份表格：`Month (UTC)`（单元格文字恰为 `billing_month`，如 `2026-12`）、`Status`（`RECONCILED` 显示 `Reconciled`、`NOT_RECONCILED` 显示 `Not reconciled`，文案进 `en.json`）、`Actual (source)`、`Actual (MYR)`、`Estimated (MYR)`、`Variance rate (source)`、`Variance rate (MYR)`；表格**关闭分页**（`pagination={false}`）；超阈值行**只在 `Variance rate (source)` 单元格**里值的旁边显示文字标记「Above 5%」，MYR 差额率格不加；已对账行里为 null 的差额率显示「—」；超阈值而原币差额率为 null（估算为 0、实际大于 0）时，`Variance rate (source)` 单元格显示「—」后接文字标记「Above 5%」；未对账行的 `Actual (source)`、`Actual (MYR)`、`Estimated (MYR)`、`Variance rate (source)`、`Variance rate (MYR)` 五列都显示「—」（U+2014；总览对未对账行 `current` 为 null、不另查估算）；`items` 为空时表格的空状态文字恰为 `No completed months to reconcile yet.`（antd `Table` 的 `locale.emptyText`，文案进 `en.json`）。`check_provider_cost_months` 按这三条文字断言。
- 「Reconcile」（未对账行）/「Correct」（已对账行）打开同一个表单：币种（**固定下拉，只有 `USD` 与 `MYR`**）、实际金额、来源说明，更正时多一个必填理由。Reconcile 时表单为空、币种 `USD`；**Correct 时预填当前记录的币种、`source_total`、`source_document_reference`，理由为空**，`supersedes_id` 取当前记录；「Preview」显示预览结果与诊断计数及其说明；「Confirm」只在本次预览成功、且表单在预览后没被改动时可点，提交时带上预览返回的 `estimated_event_count` 与 `estimated_source_cost`；确认成功（201）后：关闭表单、重新拉月份总览、用 antd `message.success` 显示成功提示（不用 `role="alert"` 的组件）；409 `RECONCILIATION_ESTIMATE_CHANGED` 时提示重新预览；409 `RECONCILIATION_EXISTS` / `RECONCILIATION_NOT_CURRENT` 时（**预览与确认都可能返回**，第 4 步两者都做）关闭表单、重新拉月份总览并提示「已有更新的版本」；其他错误（含 422 `RECONCILIATION_AMOUNT_OUT_OF_RANGE`）在表单里显示错误码、信息与 `RequestReference`。
- 月份总览加载失败（含 404 `AI_PROVIDER_NOT_FOUND`）：表格位置显示一段普通文字（`Typography.Text type="danger"`，**不用 `Alert`、不带 `role="alert"`**）：错误码、信息与 `RequestReference`，内容取法同 `frontend/src/features/catalog/ProvidersPage.tsx:419` 的 `CatalogErrorAlert`；不显示空状态文字。
- 点已对账行展开详情：更正链（时间、操作人、理由、各版本金额、当前 / 已被取代）与「确认之后估算已变化」提示。
- 说明文字：期间按 UTC 自然月（与 Console 的日边界一致）、范围只含经 AI 模块的调用、汇率取月末生效的已发布版本、对账不改任何已计费金额。
- 金额、比率原样显示字符串；文案进 `en.json`；`adminAudit.ts` 的动作清单由 AIH-TASK-244 随后端枚举一起加（见 §11）。

### 告警（AIH-TASK-250，见 §10「我定的」第 4 条）

`usage_processing_backlog` 的摘要在现有五段之后追加第六段 `provider cost <OK 文案 | 未对账文案>`：

- 判定月 m = 「上一个已结束的 UTC 月」，且 `m ≥ FIRST_RECONCILIATION_MONTH`；窗口 `period_end(m) + 9 天 ≤ now < period_end(m) + 12 天`。
- 窗口内：`scope_providers(m 的期间)`（AIH-TASK-243，一条查询，按代码升序）列出 m 期间里有范围内已处理事件的供应商；对每个调 `current_record(provider_id, AI_MODULE, m)`，为 None 的收集起来。不调 `scope_estimate`（不需要币种与金额）。收集到的非空 → P2，摘要 `provider cost <code>,<code> not reconciled for <YYYY-MM>`；否则 `provider cost reconciled or not due`。
- 只有供应商代码与月份，没有金额（INV-7、`app/services/alerts.py:19-23`）。窗口外恒 OK，所以它在 72 小时后自动翻回，不长期遮住同一检查的其他故障。
- 差额超阈值**不告警**（§9 备选 L）。

### 事务边界

- 预览：一个事务，只读 + 共享锁，最后回滚。
- 确认：一个事务：fx 共享锁 → 取汇率（加锁读）→ 聚合（一致读）→ INSERT 记录 → INSERT 审计 → 提交（INV-13）。没有 outbox、没有异步任务。
- 详情、总览、告警：只读会话，不提交。

### 时间语义

- `occurred_at`：`DATETIME(6)` 不带时区的 UTC，期间归属只看它。期间端点是整秒 UTC。
- 汇率时刻 `period_end − 1µs`：汇率版本区间是整秒，比较照常；取到的是该期间最后一刻生效的版本（马来西亚时间是次月 1 日 07:59:59.999999）。
- `created_at`：整秒 UTC（审计同一约定，`app/services/auth.py:150-151`）。
- 「月份已结束」用服务器 `now`（UTC）判断 `period_end ≤ now`。

### 金额与精度

- 聚合在数据库里对 DECIMAL 精确求和；差额是 Decimal 相减（8 位，无舍入）；差额率是 Decimal 除法（默认上下文 28 位）后 `ratio_text`。
- `reconciled_cost_myr = quantize_money(source_total × fx_rate_applied)`，乘法在 `PRICING_CONTEXT`（50 位）内，**只舍入一次**。`source_total` 至多 8 位小数、汇率至多 10 位，积至多 18 位小数、精确。
- 阈值比较用精确 Decimal（`abs(rate) >= Decimal("0.05")`），不用格式化后的字符串。

## 3. 不变量影响矩阵

| 不变量 | 是否触碰 | 可能怎样被破坏 | 设计控制 | 验证方式 |
| --- | --- | --- | --- | --- |
| INV-1 中心故障不中断 AI | 是（间接） | 共用数据库（spec §141.5）；预览扫一个月的事件并探测 outbox | 一致读不加锁；不改、不 import `app/ai_api/`；AI 请求路径不读新表 | §7「边界」；PR 里的 `EXPLAIN` |
| INV-2 事件不重复扣费 | 否 | 不改事件状态、不计费 | 对事件只读 | §7「快照不变」 |
| INV-3 支付不重复入账 | 否 | 不读不写支付 | — | — |
| INV-4 余额只经账本变动 | 否 | 对账若顺手调钱包就是越界 | 服务层不**直接** import 钱包与账本模块（只扫直接 import；共用工具的间接依赖不算）；不发补偿交易 | §7「快照不变」断言钱包余额与账本行数不变 |
| INV-5 历史账本不可变 | 否 | 同上 | 同上 | 同上 |
| INV-6 事件保留版本引用 | 是 | 对账覆盖事件上的估算快照（spec §14 明令禁止） | 只插入新表；事件列只读；对账记录自己引用汇率版本与快照 | §7「快照不变」逐列比对 |
| INV-7 客户不可见成本毛利 | **是** | 新字段挂到客户侧；客户侧模块 import 新模块 | 只在 `/api/v1/admin`、首条语句 `require_admin`；INV-7 守卫加新键名与新模块；告警摘要不含金额 | §7「INV-7 守卫」「鉴权」「告警摘要」 |
| INV-8 租户不可互访 | 是 | 客户令牌调用对账接口 | 只有 ADMIN；对账记录不属于租户 | §7「鉴权」 |
| INV-9 对话内容不入库 | 否 | 只读计量列与 outbox 三列，不读 `payload_json` | 表引用只声明三列 | 代码审查 + §7「边界」 |
| INV-10 金额用 Decimal | 是 | 实际金额经 float 解析；换算中途舍入；前端转 number | 字符串入参、DECIMAL 列、`PRICING_CONTEXT`、只舍入一次、字符串输出 | §7「金额精度」「前端不经 number」 |
| INV-11 event_id 至多一次财务效果 | 否 | 对账不对任何事件产生财务效果 | 不写事件、不写账本 | — |
| INV-12 定稿对账单不可变 | 否 | 把对账差额并进客户对账单 | 不读不写 `monthly_statements` | §7「快照不变」断言对账单行不变 |
| INV-13 状态与事件原子提交 | 是 | 记录写入而审计没写，或反之 | 同一事务插入记录与审计；任一失败整体回滚 | §7「事务中途失败」 |
| INV-14 队列丢失不毁持久工作 | 否 | 不用队列；告警由每 5 分钟的巡检读库 | 同步写库；告警无状态 | — |

## 4. 状态与并发

### 跃迁表（「当前 / 已被取代」是派生状态，行本身从不改）

| 当前状态 | 事件 | 前置条件 | 新状态 | 副作用 | 非法时结果 |
| --- | --- | --- | --- | --- | --- |
| 该月无记录 | 确认（不带 `supersedes_id`） | §2 检查第 2–7 步通过 | 新行为根、当前 | 插入一行 + 一条审计 | 已有根 → 409 `RECONCILIATION_EXISTS` |
| 记录 a 为当前 | 确认（`supersedes_id = a`，带理由） | 同上；a 与请求同供应商、范围、月份 | 新行 b 为当前；a 变为已被取代 | 插入 b + 一条审计（`before` 为 a 的金额） | a 已被取代 → 409 `RECONCILIATION_NOT_CURRENT`；月份等不符 → 422 `RECONCILIATION_SUPERSEDES_MISMATCH` |
| 记录已被取代 | 任何确认 | — | — | — | 409 `RECONCILIATION_NOT_CURRENT` |
| 任何 | `UPDATE` / `DELETE`（任何路径，包括手工 SQL） | — | — | — | MySQL 触发器 `SIGNAL 45000`；应用没有更新与删除路径 |

### 并发

- **两个根同时确认同一月**：都过第 4 步的普通读；后插入者在 `ux_…_root` 上等先者的未提交键，先者提交后得重复键 → 回滚 → 409 `RECONCILIATION_EXISTS`。唯一性由数据库负责，第 4 步只给出更早的错误。
- **两个更正同时取代 a**：同理由 `uq_provider_cost_reconciliations_supersedes` 仲裁 → 后者 409 `RECONCILIATION_NOT_CURRENT`。所以链永远是单链、恰好一个链头。
- **确认与汇率发布 / 退役并发**：确认先拿 `fx_rate_locks` 共享锁（与发布、退役的排他锁互斥，`app/repositories/fx_rates.py:92` 的契约），取到的版本在提交前不会变；记录里存版本引用与汇率值，之后再变也不影响已写的行。
- **确认与计费 worker 并发**：聚合是一致读、不阻塞计费；预览后新处理的事件由第 7 步 409 挡住；确认后才处理的在详情里显示「估算已变化」。
- **锁顺序**：`fx_rate_locks`（S）→ `fx_rate_versions` 行（S，`resolve_fx_rate` 的加锁读）→ 插入新行（外键检查对 `ai_providers`、`users`、`fx_rate_versions`、被取代行各取 S）→ 插入审计。汇率发布按 `fx_rate_locks`（X）→ 版本行的顺序，与本顺序一致，不成环；被取代行永不更新，没有人对它取 X。
- **幂等与重试**：没有幂等键；重复提交由唯一约束变成 409，要改只能走更正。服务层不自动重试，断连时整体回滚、管理员重提。

## 5. 失败与恢复矩阵

| 失败点 | 对外结果 | 数据最终状态 | 是否可重试 | 恢复来源 | 告警 |
| --- | --- | --- | --- | --- | --- |
| 未登录 / 非 ADMIN | 401 `TOKEN_INVALID` / 403 `ADMIN_REQUIRED` | 不变 | — | — | — |
| 请求体校验失败 | 422 `VALIDATION_ERROR`（只列字段名） | 不变 | 改参数后 | — | — |
| 月份不合法 / 未结束 / 早于首月 | 422 `RECONCILIATION_MONTH_INVALID` | 不变 | 月份结束后 | — | — |
| 供应商不存在 | 404 `AI_PROVIDER_NOT_FOUND` | 不变 | — | — | — |
| 月末没有已发布汇率 | 422 `FX_RATE_UNAVAILABLE` | 不变 | 照 `docs/currency-and-fx.md` §4 用 P1 / P4 补录后；两者都补不了的空档：本月暂不能对账，记待办，汇率补录另走设计 | 管理端汇率页 | 现有 `fx_stale` / `fx_fetch` |
| 范围内事件币种不一 | 422 `SOURCE_CURRENCY_MISMATCH` | 不变 | 事件都是另一种币种时换成它录入；混杂多种币种时本月无法对账，需另走设计闸门（§10 假设 4） | — | — |
| 预览后估算变化 | 409 `RECONCILIATION_ESTIMATE_CHANGED` | 不变 | 重新预览后确认 | — | — |
| 并发确认 / 已被取代 / 已有根 | 409 `RECONCILIATION_EXISTS` / `RECONCILIATION_NOT_CURRENT`（不带 `details`） | 先提交者的一行；后者什么都没写 | 页面重新拉月份总览得到当前版本，再对它更正 | — | — |
| DB 提交失败（断连、锁等待超时） | 500 `INTERNAL_ERROR` | 整体回滚：既没有记录也没有审计 | 是 | 重新确认 | ERROR 日志 |
| 有人手工 `UPDATE` / `DELETE` | MySQL 报 45000 | 行不变 | — | — | — |
| 迁移中断 | 部署失败 | 见 §8（照 0039 的每个窗口） | 原样重新部署 | — | 部署失败通知 |
| Redis / Celery 丢失 | 不影响（不用队列） | 不变 | — | — | — |
| 外部服务超时 | 不调用外部服务 | — | — | — | — |
| 重复请求 | 409（见上） | 一行 | — | — | — |
| 告警查询出错 | 现有 `/internal/alerts` 的失败处理（整个维度不可读） | 不变 | 下一轮 | — | 现有 |

## 6. 数据与安全边界

- **鉴权**：所有新接口在 `/api/v1/admin`，首条语句 `require_admin`（ADMIN 令牌必过 2FA）；四个路径加进 `tests/backend/test_admin_customers_api.py` 的 `EXPECTED_ADMIN_ROUTES`，401 / 403 探测给 `/months` 补 `provider_code` 查询串。
- **租户过滤**：对账记录按供应商与月份，不属于任何租户。
- **客户侧禁止的字段**：在 AIH-TASK-222 的禁用名单上追加 `reconciled_cost_myr`、`source_total`、`source_variance`、`source_variance_rate`、`myr_variance`、`myr_variance_rate`、`estimated_source_cost`、`estimated_cost_myr`（`fx_rate_applied` 已在名单里；毛利分析的新键名见分摊设计）；import 扫描加 `app.services.provider_cost_reconciliation`、`app.repositories.provider_cost_reconciliations`、`app.schemas.provider_cost_reconciliations`、`app.api.admin_provider_cost_reconciliations`。
- **日志与审计**：确认只写审计（§2，审计页只对 ADMIN 可见）、不另记日志；失败沿用通用 500，不记金额与来源说明；告警摘要只有供应商代码与月份。
- **密钥**：本批**不新增、不读取**任何密钥。专用 Anthropic 密钥仍走现有 secret 文件（`app/core/config.py:327-328`，ADR-0004）；换钥步骤由 AIH-TASK-251 写进 runbook，**不写主机名、地址、密钥值**；不保存供应商 Admin 密钥。
- **prompt / response**：不处理；outbox 只引用 `event_id`、`status`、`created_at` 三列，不读 `payload_json`。
- **公开仓库与保留**：文档与测试不写 Console 地址、工作区真实名称、实际金额；对账记录与审计永久保留、不可删。

## 7. 测试证据计划

遵守仓库根 `CLAUDE.md`「写后端测试的约定」：不写死方言字面量；共用构件放辅助模块 `tests/backend/provider_cost_world.py`，夹具在 `tests/backend/conftest.py` 注册；等行锁查 `performance_schema.data_lock_waits`（照 `tests/backend/test_session_revocation_concurrency.py`）。示例 uuid 全零，MySQL 分支逐位断言。**AIH-TASK-244、245、250 的用例一律在夹具里 monkeypatch `FIRST_RECONCILIATION_MONTH` 为 `2026-12`**，不依赖常量默认值，所以只改常量的 PR 不会让 CI 挂。

**标准世界**（`tests/backend/provider_cost_world.py`，SQLite 与 MySQL 两份；全部时刻是不带时区的 UTC）：

- 供应商 `anthropic`（`ai_providers`），模型 `claude-test`；该模型一个 `PUBLISHED` 价格版本（来源币种 USD）与满足 `PROCESSED_SNAPSHOT_CHECK` 所需的定价规则，只被引用，数值不参与断言。
- 租户与项目：`T_int`（`INTERNAL_METERED_ONLY`）的项目 `P_crm`（AI 模块项目）；`T_pre`（`PREPAID`）的项目 `P_pre`（AI 模块项目）与 `P_chat`（集成方项目，用自己的供应商密钥调用后上报）。
- 汇率（USD→MYR，均 `PUBLISHED`）：`V45` 区间 `[2026-11-01 00:00:00, 2026-12-31 12:00:00)`、汇率 4.5；`V44` 区间 `[2026-12-31 12:00:00, 无上界)`、汇率 4.4。**每个事件快照里的汇率版本就是按它的 `occurred_at` 由 `resolve_fx_rate` 取到的那一版**，估算 MYR = 来源成本 × 该汇率。
- **计费额一律 0**：`PROCESSED_LEDGER_CHECK` 只在计费额 > 0 时要求账本行，取 0 就不建钱包与账本；对账不读计费额（分摊设计的毛利用例据此断言）。`T_int` 的事件 `reference_customer_price` = 估算 MYR × 2（满足 `INTERNAL_MODE_CHECK`）；`T_pre` 的事件 `reference_customer_price` 为 NULL（`PREPAID_REFERENCE_CHECK`）。

| 事件 | 来源 | 租户 / 项目 | `occurred_at` | 状态 | 来源成本（USD） | 汇率版本 | 估算 MYR | `billing_mode_snapshot` |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| E1 | AI 模块 | T_int / P_crm | 2026-12-05 03:00:00 | PROCESSED | 6.00 | V45 | 27.00 | INTERNAL_METERED_ONLY |
| E2 | AI 模块 | T_pre / P_pre | 2026-12-31 20:00:00（马来西亚 2027-01-01 04:00） | PROCESSED | 4.50 | V44 | 19.80 | PREPAID |
| E3 | AI 模块 | T_int / P_crm | 2026-11-30 23:59:59.999999（马来西亚 2026-12-01） | PROCESSED | 1.00 | V45 | 4.50 | INTERNAL_METERED_ONLY |
| E4 | AI 模块 | T_int / P_crm | 2027-01-01 00:00:00 | PROCESSED | 2.00 | V44 | 8.80 | INTERNAL_METERED_ONLY |
| E5 | 集成方（**没有** outbox 行） | T_pre / P_chat | 2026-12-10 00:00:00 | PROCESSED | 100.00 | V45 | 450.00 | PREPAID |
| E6 | AI 模块 | T_int / P_crm | 2026-12-15 00:00:00 | PRICING_ERROR | — | — | — | — |

- **MySQL 清场**：本世界写过的、带 BEFORE DELETE 触发器的表都拒绝 DELETE，照 `tests/backend/payment_world.py:507-512` 关外键检查后 TRUNCATE、再恢复：`provider_cost_reconciliations`、`audit_logs`、`fx_rate_versions`、`provider_price_versions`、`provider_price_components`、`pricing_rules`、`pricing_rule_components`。锁表（`fx_rate_locks`、`pricing_rule_locks`）的种子行是取锁的前提、本世界不写它们，**不清**。

- outbox：E1、E2、E3、E4、E6 各一行 `DELIVERED`（`event_id` 与事件相同，`created_at` 等于事件的 `occurred_at`，租户与项目同事件）；另有 `O_pending`（`PENDING`，`created_at` 2026-12-20 00:00:00）与 `O_discarded`（`DISCARDED`，`created_at` 2026-12-21 00:00:00），无对应事件，租户与项目用 `T_int / P_crm`。占位值写死：`event_id` 是辅助模块里的 26 字符常量（E1–E6 用 `00000000000000000000000E01` 到 `00000000000000000000000E06`，同时就是事件的 `event_id`；`O_pending` 为 `00000000000000000000000P01`、`O_discarded` 为 `00000000000000000000000D01`），`request_id` 等于 `event_id`，`feature` 为 `provider_cost_test`，`payload_json` 为 `{}`，`attempts` 为 0，`next_attempt_at` 等于 `created_at`，其余可空列为 NULL。outbox 行按表名插入，不经 `app.ai_api`。
- 由此：12 月（`[2026-12-01, 2027-01-01)`）的范围内已处理事件是 E1、E2；`scope_estimate(anthropic, 12 月, USD)` = 事件 2、来源成本 `10.50`、估算 MYR `46.80`、币种不一 0；未处理 1（E6）、未投递 1、已丢弃 1。录入 `11.70 USD`：取值时刻 `2026-12-31 23:59:59.999999` 落在 V44 → `reconciled_cost_myr = 51.48`；`R ÷ E = 51.48 ÷ 46.80 = 1.1`（精确）。

**变体世界**（只在用到的用例里建，不改标准世界）：

- `fx_switch`：在标准世界上把 V45 的区间改为 `[2026-11-01, 2027-01-01 00:00:00)`、V44 改为从 `2027-01-01 00:00:00` 起；E2 的快照相应改为 V45、估算 `20.25`（E4 仍是 V44）。录入 `11.70 USD` 取到 V45 → `reconciled_cost_myr = 52.65`。
- `myr_three`：独立世界，`T_int` 的三个 AI 模块项目 `Q1`、`Q2`、`Q3`，各一个 AI 模块事件（`occurred_at` 2026-12-02、12-03、12-04 的 12:00:00，`PROCESSED`、`INTERNAL_METERED_ONLY`），来源币种 **MYR**、来源成本 = 估算 MYR = `1.00`（两个汇率列为空，`reference_customer_price` = `2.00`，计费额 0）；outbox 各一行 `DELIVERED`。
- `unallocatable`（分摊设计用）：标准世界 + 三条记录，**由构建函数用 `insert_record` 直接写入**（不经 `confirm`；金额与汇率列按「金额与精度」算好写死）：2026-12（`USD 11.70`、V44、R = 51.48）、2027-02（`MYR 5.00`，无事件，E = 0）、2027-03（`MYR 0.00`，E = 0）。三条都是根、`scope = AI_MODULE`；`public_id` 依次为 `00000000-0000-0000-0000-000000000101` / `…102` / `…103`（全零规则的例外：同一世界里须互不相同）；`created_by` 为标准世界的管理员用户（标准世界照 `tests/backend/payment_world.py:607-618` 建一个 `ADMIN`、`ACTIVE` 用户，邮箱用其测试域名常量）；估算与诊断快照取该月按标准世界算出的值（2026-12 照上文：事件 2、10.50、46.80、未处理 1、未投递 1、已丢弃 1；另两条全为 0）；`created_at = period_end + 1 天`；`source_document_reference = "test-statement-<YYYY-MM>"`。`now` 注入为 2027-04-02。

| 风险/需求 | 测试层级 | 场景 | 预期结果 | 任务 |
| --- | --- | --- | --- | --- |
| 表结构与触发器 | migration（SQLite + MySQL） | 升级到 0041；比对模型与迁移的列、索引、外键、CHECK；MySQL 上读 `information_schema.STATISTICS`；对一行 `UPDATE` / `DELETE` | 形状一致；MySQL 上除主键外的索引集合**恰好**是 §2 列出的六个（没有为外键自建的隐藏索引）；两条语句都被 45000 拒绝 | AIH-TASK-242 |
| 根唯一与单链 | migration（唯一约束：SQLite + MySQL；外键：只在 MySQL） | 同供应商同月插两个根；两行取代同一行；取代行的月份与被取代者不同 | 前两种唯一约束失败（`ux_…_root`、`uq_…_supersedes`，两种库都测）；第三种 `fk_provider_cost_reconciliations_supersedes` 失败——**只在 MySQL 上测**：后端测试的 SQLite 引擎不开 `PRAGMA foreign_keys`，本任务也不为它开；不同月份可以各有一个根 | AIH-TASK-242 |
| CHECK | migration | MYR 行带汇率；非 MYR 行缺汇率或汇率为 0；根带理由；取代行缺理由；负金额与负计数 | 各被对应 CHECK 拒绝（SQLite 与 MySQL；CHECK 都是可移植表达式） | AIH-TASK-242 |
| 迁移中断与降级 | migration（MySQL） | 照 0039 的窗口：只建了表缺触发器、同名不同形状的表、表里有行时降级 | 重跑只补缺的；形状不对抛错不发 DDL；有行时降级抛错 | AIH-TASK-242 |
| 范围判定 | integration（SQLite + MySQL，标准世界） | `scope_estimate(anthropic, 12 月, USD)` | 事件 2、来源成本 `10.50000000`、MYR `46.80000000`、币种不一 0；E5（集成方）、E3（11 月）、E4（1 月）不计；E2（马来西亚已是 1 月）计入 12 月 | AIH-TASK-243 |
| 边界的 event_id | integration（MySQL） | 在标准世界上另加两条集成方事件（12 月、`T_pre / P_chat`、来源成本 1.00 USD / 估算 4.50、无 outbox 行）：一条 `event_id` 含非 ASCII 字符；一条 `event_id` 是 64 字符、前 26 字符与 E1 的 ULID 相同 | 都不计入范围（`scope_estimate` 仍为事件 2 / `10.50000000`）、查询不报错 | AIH-TASK-243 |
| 诊断计数 | integration | 标准世界，12 月 | `unprocessed_count` 1、`outbox_counts` 为 (1, 1) | AIH-TASK-243 |
| 范围内供应商 | integration | `scope_providers` 取 12 月、2027-01、2027-02 | `[(anthropic 的 id, "anthropic")]`、同上（E4）、`[]` | AIH-TASK-243 |
| 链头 | integration | 根 a、a 被 b 取代（直接 `insert_record`） | `current_record` 为 b；`chain` 为 [a, b]；`current_records_overlapping` 只返回 b | AIH-TASK-243 |
| 正常确认 | service（SQLite + MySQL，标准世界） | 12 月 `USD 11.70` | `reconciled_cost_myr = 51.48000000`（V44）；`source_variance = 1.20000000`、`source_variance_rate = 0.1142857142857142857142857143`；`myr_variance = 4.68000000`、`myr_variance_rate = 0.1`；超阈值；一行一条审计，审计字段恰好如 §2 | AIH-TASK-244 |
| 汇率时刻 | service（`fx_switch`） | 12 月 `USD 11.70` | 取 V45，`reconciled_cost_myr = 52.65000000` | AIH-TASK-244 |
| MYR 录入 | service | 标准世界录 `MYR 51.48`；`myr_three` 录 `MYR 1.00` | 前者 422 `SOURCE_CURRENCY_MISMATCH`；后者 `reconciled_cost_myr = 1.00000000`，`fx_rate_version_id`、`fx_rate_applied` 为空，视图的三个汇率键为 null | AIH-TASK-244 |
| 检查顺序 | service | ① 不带 `supersedes_id` 却带理由且月份未结束；② 月份未结束且供应商不存在；③ 早于首月；④ 录 12 月 `EUR 11.70`（无 EUR 版本，两种库都跑）；⑤ 确认 12 月 a 后，`now` = 2027-02-02 请求 2027-01 带 `supersedes_id = a` | 依次 422 `RECONCILIATION_REASON_INVALID`、`RECONCILIATION_MONTH_INVALID`（先于 404）、`RECONCILIATION_MONTH_INVALID`、`FX_RATE_UNAVAILABLE`、`RECONCILIATION_SUPERSEDES_MISMATCH` | AIH-TASK-244 |
| 理由校验 | service | `preview` 与 `confirm` 各一遍：带 `supersedes_id`、`reason` 为 None；`reason` 为三个空格；`reason` 256 字符 | 都是 422 `RECONCILIATION_REASON_INVALID`，不写行 | AIH-TASK-244 |
| 预览不写库 | service | 用同一组入参（含 `source_document_reference`、`supersedes_id`、`reason`）先预览、再确认；预览后数行数与审计数 | 都不变；预览视图与确认视图除 `reconciliation_id`、`created_at`、`created_by_email`、`is_current` 四个字段（预览里都是 None）外逐字段相等，`source_document_reference` 与 `reason` 也相等 | AIH-TASK-244 |
| 估算变化 | service | 预览后插入一条 12 月 AI 模块 `PROCESSED` 事件（`T_int / P_crm`，1.00 USD / V45 / 4.50），再用预览的期望值确认 | 409 `RECONCILIATION_ESTIMATE_CHANGED`，不写行 | AIH-TASK-244 |
| 已有根 | service | 12 月已确认 a，再不带 `supersedes_id` 确认 12 月 | 409 `RECONCILIATION_EXISTS`（错误只有 `code` 与 `message`） | AIH-TASK-244 |
| 更正 | service | 确认 a 后带理由取代 a；再次取代 a | b 为当前、审计 `before` 为 a 的金额；第二次 409 `RECONCILIATION_NOT_CURRENT` | AIH-TASK-244 |
| 并发确认 | concurrency（MySQL） | 事务 1 跑真实的 `confirm`，被 monkeypatch 的钩子挡在审计之后、提交之前；事务 2 同月确认，用 `performance_schema.data_lock_waits` 确认它在 `provider_cost_reconciliations` 上等锁；事务 1 提交 | 事务 2 得到 409 `RECONCILIATION_EXISTS`；表里一行、审计一条 | AIH-TASK-244 |
| 并发更正 | concurrency（MySQL） | 先经 `confirm` 写入 a（一行、一条审计）；再按「并发确认」的方式让两个事务都取代 a | 一个成功、一个 409 `RECONCILIATION_NOT_CURRENT`；最终表里两行（a 与胜出的更正）、审计两条（a 与胜出的更正各一条），败者不写行与审计 | AIH-TASK-244 |
| 事务中途失败 | service | 审计写入时注入异常 | 500；记录与审计都不存在 | AIH-TASK-244 |
| 快照不变 | service（MySQL） | 确认前后对全部事件的计费快照列、钱包余额、账本行数、对账单行做逐列快照 | 完全相同 | AIH-TASK-244 |
| 详情的估算变化 | service | 12 月确认后分别只插入一条 12 月 AI 模块事件：① `PROCESSED`、1.00 USD / V45 / 4.50；② `PROCESSED`、来源币种 MYR、成本与估算 1.00；③ `PRICING_ERROR`（带 `DELIVERED` 的 outbox 行） | ① `estimate_changed_since_confirmation = true`、`live_estimate.estimated_event_count = 3`；② true、`currency_mismatch_count = 1`；③ false、`unprocessed_event_count = 2` | AIH-TASK-244 |
| 分摊纯函数 | unit | `allocate(27.00, 51.48, 46.80)`；`allocate(1, 1, 0)` | `29.70`（精确，未舍入）；None | AIH-TASK-244 |
| 金额精度 | service | `source_total = 0.00000001`、汇率 4.4 | `reconciled = 0.00000004`（`0.000000044` 半入）；差额率逐位等于 Decimal 计算值 | AIH-TASK-244 |
| 接口契约 | API（SQLite + MySQL） | 预览、确认、总览、详情（标准世界与 `myr_three`）；确认时 `expected_event_count` 为 `true`、`"2"`、`-1`，`expected_estimated_source_cost` 为数字 `10.5`、`"-1"`、`"1.123456789"` | 键与类型恰好如 §2；金额为 8 位字符串；MYR 记录的三个汇率键为 null；时间字段按 `FxRateView` 的写法（不带 `Z`）；`expected_*` 的六个非法值都是 422 `VALIDATION_ERROR`；`source_total` 传数字是 422；预览带 `expected_*` 是 422；路由注册顺序（`/months`、`/preview` 不被当作 id） | AIH-TASK-245 |
| 总览 | API | `now` 在 2027-02-15、首月 2026-12、12 月已对账；`provider_code=openai`（库里没有）；`provider_code=Bad!` | 两项：2027-01 `NOT_RECONCILED`、2026-12 `RECONCILED` 倒序；`now` 在 2026-12-20 时为空列表；没有的供应商 404 `AI_PROVIDER_NOT_FOUND`；格式不符 422 `VALIDATION_ERROR` | AIH-TASK-245 |
| 详情与 id | API | 同「详情的估算变化」①；不存在的 UUID；`not-a-uuid`；确认时 `supersedes_id = "not-a-uuid"` | 200，`live_estimate` 五个键齐全；后三者都是 404 `PROVIDER_COST_RECONCILIATION_NOT_FOUND`（不是 422） | AIH-TASK-245 |
| 鉴权 | API | 匿名、客户令牌调四个接口（**带合法参数与请求体**） | 401 / 403；不写库；不断言与 422 的先后（照 `app/api/admin_fx_rates.py` 文件头的现状，FastAPI 先校验输入），探针一律用合法输入；`EXPECTED_ADMIN_ROUTES` 含四个路径 | AIH-TASK-245 |
| INV-7 守卫 | unit | 扩展后的禁用名单与 import 扫描；自检注入一个新名字 | 非管理端 schema 无交集；客户侧与 AI 模块不 import 新模块；自检会失败 | AIH-TASK-245 |
| 前端 api | unit（vitest） | 四个封装函数的路径、方法、请求体（金额为字符串） | 与 §2 一致 | AIH-TASK-246 |
| 前端页面 | unit（Testing Library） | 一行已对账、一行未对账；25 行；空列表；加载失败；null 差额率；超阈值（含原币差额率 null）；币种下拉；Correct；预览后才能确认、改表单后不可点；确认 201；409 估算变化；409 已存在；详情链 | 状态 `Reconciled` / `Not reconciled`、月份为 `billing_month`；25 行全部渲染；空状态文案；加载失败显示错误码与 `RequestReference`、无空状态；未对账行五列与 null 差额率为「—」；Above 5% 只在原币差额率格；下拉只有 USD / MYR；Correct 预填当前记录、理由为空；201 后关表单、重拉总览、`message.success`；409 已存在后重拉总览；全程没有 `role="alert"`、金额原样显示 | AIH-TASK-246 |
| 浏览器验收 | acceptance（只读） | `open_provider_costs`、`check_provider_cost_months` | 见 §11 AIH-TASK-246 | AIH-TASK-246 |
| 告警窗口 | unit（SQLite，标准世界） | 判定月 2027-01（有 E4）：`now` = 2027-02-01 加 8 天 23:59:59、9 天整、12 天减 1 秒、12 天整；有 / 无 2027-01 的当前记录；判定月 2027-02（没有范围内事件）的窗口内；判定月早于首月（该用例把常量 monkeypatch 为 2027-02） | 只在窗口内、有事件且无记录时 P2，摘要第六段恰为 `provider cost anthropic not reconciled for 2027-01`；没有范围内事件的月份 OK；早于首月不报；其余五段逐字不变 | AIH-TASK-250 |
| Redis / Celery 丢失 | — | 本批不用队列 | 不适用 | — |
| 租户越权 | — | 记录不属于租户；鉴权由「鉴权」行覆盖 | — | — |

## 8. 迁移与上线

- **数据迁移**：迁移 `0041_provider_cost_recon`（AIH-TASK-242，文件 `20261117_0041_provider_cost_reconciliations.py`，`down_revision` 为 AIH-TASK-231 的 `0040_reset_request_index`）只建一张新表与两个触发器；不改任何已有表，不回填。
- **锁表与性能**：建表对三张被引用表各取短暂元数据锁，不扫行。范围查询走 `ix_usage_events_status_occurred`（一个月），outbox 探测的计划由 PR 的 `EXPLAIN` 给出；变慢另开任务（§10 已知限制 1）。
- **备份**：照 0005–0040 的口径要求前置备份。
- **部署顺序**：AIH-TASK-231（迁移 0040）→ AIH-TASK-242 → AIH-TASK-243 → AIH-TASK-244 → AIH-TASK-245（另依赖 AIH-TASK-222）→ AIH-TASK-246 →（分摊设计的 AIH-TASK-247..249）→ AIH-TASK-250 → AIH-TASK-251，按编号串行登记（AIH-TASK-250 实际只依赖 AIH-TASK-244）。每层合并即部署、互不破坏。
- **上线后的人工步骤**（AIH-TASK-251 写进 runbook，AIH-TASK-245 之后才有意义）：① Kelvin 在 Console 为本平台建专用 workspace 与密钥；② 替换宿主机 secret 文件、重建 api 容器、确认 `/healthz/ai` 正常；③ 记下切换时刻；换钥晚于 2026-12-01 时，**每月 10 日之前**（AIH-TASK-250 的告警窗口开始前）开一个只改 `FIRST_RECONCILIATION_MONTH` 的 PR，把它推到「当前月的下一个月」，到下月 10 日前仍未换钥就再推一个月，直到它等于换钥后的第一个完整 UTC 月；写入任何记录之前常量必须已正确；④ 每月初在 Console 成本页确认上月数字已稳定后录入。
- **回滚或前滚**：生产只回滚应用镜像，**不降级 0041**；降级只删空表（有行就抛错）。记录写错 → 用更正（追加取代行），不删行。
- **部分部署与兼容**：新表在 AIH-TASK-242 后无人读写；AIH-TASK-246 依赖 AIH-TASK-245 已在生产。
- **监控**：AIH-TASK-250 的 P2 提醒并进 `usage_processing_backlog`；负责人 Kelvin。

## 9. 方案取舍

| 方案 | 优点 | 风险 | 未采用原因 |
| --- | --- | --- | --- |
| 采用：手工录入供应商 × UTC 月 × 范围的合计；不可变追加行 + 取代链；月末已发布汇率换算 | 不碰任何计费数据；数据库保证不可改与单链 | UTC 月与对账单的马来西亚月不同，必须标注；范围判定依赖 outbox 永久保留 | — |
| 备选 A：草稿表（DRAFT → CONFIRMED） | 先存后确认、可四眼 | 行要可改、触发器要按状态放行；单管理员时预览已够 | 复杂度换不来收益 |
| 备选 B：接供应商成本 API | 免手工 | 新的 Admin 凭据与轮换；格式无契约 | ADR-0016 同一思路 |
| 备选 C：按模型录入 | 看出哪个模型偏差 | 不确定 Console 能否按 workspace 同时按模型给数；录入量成倍 | V1 合计；需要时加子表 |
| 备选 D：逐事件加权汇率（`实际 × Σ估算MYR ÷ Σ估算来源`） | MYR 差额只剩供应商侧 | 无汇率版本引用；Σ估算来源 = 0 时无定义 | 有无定义的情形 |
| 备选 E：录入卡账单实付 MYR | 是真实现金 | 预付额度的购买时点与消耗月份不对应（§10 假设 1） | 口径对不上消耗 |
| 备选 F：按马来西亚月对账 | 与对账单的月一致 | 供应商按 UTC 划日时两端各错开 8 小时（§10 假设 1） | 要和来源单据同口径 |
| 备选 H：范围 = 该供应商的全部事件 | 不碰 outbox | 集成方用自己的密钥上报的同供应商用量被算进来 | 范围必须现在定对 |
| 备选 I：范围 = 有 AI 客户端密钥的项目 | 查询简单 | 依赖 spec §141 的约定、数据库不强制 | outbox 是逐事件判据 |
| 备选 J：给 `usage_events` 加来源列 | 判定最快 | 大表迁移 + 改摄取路径 | 代价远大于收益 |
| 备选 K：import `app.ai_api.outbox` 并加进 `REVERSE_EXCEPTIONS` | 有类型 | 扩大反向依赖面 | 先例是表名引用 |
| 备选 L：差额超阈值进 Healthchecks | 有推送 | 录入时页面已显示；持续红遮住同一检查后来的故障（`docs/TODO.md:113`） | 只做有窗口的「未对账」提醒 |

## 10. 未决问题与假设

**已定（Kelvin 2026-11-16）**：

1–3. 期间 UTC 自然月（Console 不按 UTC 划日就升版本）、录入不含税的消耗额、分摊与本批一起登记：见 §1「Kelvin 2026-11-16 的决定」。
4. **首个对账月**：常量 `2026-12`，前提是 Kelvin 在 2026-12-01 00:00 UTC 之前换好专用密钥；晚了就照 §8 ③ 每月 10 日前用只改常量的 PR 推到下一个月，直到换钥后的第一个完整月（避免 AIH-TASK-250 误报）。

**我定的，请审查者重点看**（Kelvin 已授权按建议推进；审查认为不妥就升版本改）：

1. **汇率**：期间最后一刻（`period_end − 1µs`）生效的已发布 USD→MYR 版本。备选 D（逐事件加权）、E（卡账单实付）见 §9。
2. **粒度**：供应商 × 月一个合计，不分模型。
3. **更正与审批**：追加取代行、必填理由、单人确认，不做草稿与四眼。
4. **告警**：差额不进 Healthchecks；只做「上一个完整月满 9 天仍未对账」在 72 小时窗口内的 P2，并进 `usage_processing_backlog`（AIH-TASK-250）。阈值 5% 只用于页面高亮。
5. spec §74.5 的 `metadata_json` 不建、`billing_period` 落为 `billing_month` + 两个时刻；不改 spec，在 `docs/database-schema.md`（AIH-TASK-242）与 `docs/api.md`（AIH-TASK-245）写明。

**尚未验证的假设**：

1. Console 成本页能按 workspace 给出某个 UTC 月的 USD 消耗合计（不含税），日期按 UTC 划分（Kelvin 会核实），且数字在月结束几天后稳定。**错了的后果**：差额率失去意义；只影响内部报表，不影响客户金额。
2. `ai_usage_outbox` 的行永不删除（`app/ai_api/` 里没有删除路径）。**错了的后果**：被删的调用不再计入范围、差额偏正；将来加 outbox 清理须保留 `event_id` 或另存来源标记（另过闸门）。
3. AI 模块只调 Anthropic（`app/ai_api/client_rules.py:97`）。**错了的后果**：未处理计数混入别家事件，只影响诊断。
4. Anthropic 的价格版本以 USD 计价，范围内事件的 `provider_source_currency` 全是 USD。**错了的后果**：混杂币种时 422 `SOURCE_CURRENCY_MISMATCH`，该月无法对账，需另走设计闸门。
5. outbox 的 `created_at` 与 `occurred_at` 只差请求耗时，月界附近诊断计数可能差一两笔。

**已知限制**：

0. 月末时刻落在 P1、P4 都补不了的汇率空档（`docs/currency-and-fx.md` §4）时该月暂不能对账，补录另走设计。
1. `ai_usage_outbox.event_id`（`ascii_bin`）与 `usage_events.event_id`（`utf8mb4_0900_bin`）字符集不同，`EXISTS` 可能对 outbox 全表物化。现在行数很小；PR 写 `EXPLAIN`，行数上来后另开任务优化。
2. 首个对账月之前的用量永远只有估算（共用密钥的花费分不出来）。
3. 对账单（马来西亚月、计费额）与本对账（UTC 月、成本）口径不同，不互相核对。

**登记 PR 要做的**：删去 `docs/TODO.md` 规划块里「待登记：Phase 8 供应商账单成本对账」一行，并把 Phase 7 待登记行里的「对账后成本口径」去掉（AIH-TASK-251 不碰规划块）。

**跨批次协调**：本批占用迁移编号 `0041`（0040 是另一批认证加固 AIH-TASK-231 的 `0040_reset_request_index`）。AIH-TASK-242 的 `down_revision` 是 `0040_reset_request_index`，**依赖 0040 先合并**：AIH-TASK-242 的 `depends_on` 含 AIH-TASK-231 与前一批的最后一项（登记时填）；若 0040 改号或取消，AIH-TASK-242 的文件名、`revision`、`down_revision` 跟着改。AIH-TASK-245 改 AIH-TASK-222 新建的 `tests/backend/test_internal_cost_visibility.py`，所以排在 221..225 之后。

财务口径（期间、录入额、首月）已由 Kelvin 定；没有阻断性的未决问题，状态为 `READY_FOR_REVIEW`。

## 11. 审查与版本绑定

审查方要回答的五个问题常驻在 [scripts/review_checklist.md](../../scripts/review_checklist.md) 的「设计审查」一节，此处不重复。

**版本绑定规则**（规则的出处是 [WORKFLOW §3](../../docs/WORKFLOW.md)；这里保留一份是因为审查材料里只有本 Issue 正文，两处改一起改）：

- 批准必须写成 `APPROVED: design v<N>`，`<N>` 取本 Issue 顶部的「设计版本」
- **设计版本一变，之前的 APPROVE 自动作废**，必须重新过闸门
- 什么算「实质修改」：改了契约、表结构、事务边界、状态机、失败语义、不变量控制。改错别字不算
- 实质修改时：把顶部版本 +1，在下方追加一条变更说明，状态退回 `READY_FOR_REVIEW`

### 实现拆分

| 任务 | 层 | 依赖 | 预计改动 |
| --- | --- | --- | --- |
| `AIH-TASK-242` | 数据库 | AIH-TASK-231（迁移 0040）、前一批最后一项（登记时填） | 约 1100 行（迁移 500、模型 140、迁移测试 380、迁移总测试 30、文档 60） |
| `AIH-TASK-243` | 仓储 | AIH-TASK-242 | 约 650 行（代码 230、测试世界 70、测试 350） |
| `AIH-TASK-244` | 业务规则 | AIH-TASK-243 | 约 1150 行（服务 420、审计枚举 10、测试 560、并发测试 160） |
| `AIH-TASK-245` | 接口 | AIH-TASK-244、AIH-TASK-222 | 约 950 行（schema 160、接口 200、测试 470、守卫 40、文档 80） |
| `AIH-TASK-246` | 前端 + 验收步骤 | AIH-TASK-245 | 约 1150 行（api 与测试 230、页面 400、页面测试 320、导航与路由 30、文案 40、验收脚本 130） |
| `AIH-TASK-250` | 告警 | AIH-TASK-244（登记顺序排在 AIH-TASK-249 之后） | 约 300 行（代码 80、测试 180、runbook 40） |
| `AIH-TASK-251` | 文档 | AIH-TASK-246、AIH-TASK-249、AIH-TASK-250 | 约 150 行 |

AIH-TASK-247..249（毛利分析分摊的仓储、接口与导出、页面）在分摊设计里，依赖不变：AIH-TASK-247 依赖 AIH-TASK-246 与 AIH-TASK-221。

通用验收（每个任务都适用，下面不重复）：不改 `.platform/`、`.env*`、planning-v1 块，不装依赖；复选框只允许 `[ ]` / `[x]`；allowed_commands 的检查全部零退出，skipped 如实报告且不算 passed；Draft PR 交付，PR 正文「设计闸门」写本闸门编号；不改 `app/ai_api/` 下任何文件。

#### AIH-TASK-242

- **标题**：供应商成本对账第一层：新建不可变的供应商成本对账表与迁移 0041
- **可改路径**：
  - `alembic/versions/20261117_0041_provider_cost_reconciliations.py`（新建；`revision` 为 `0041_provider_cost_recon`，`down_revision` 为 `0040_reset_request_index`）
  - `alembic/env.py`
  - `app/models/provider_cost_reconciliations.py`（新建）
  - `tests/backend/test_provider_cost_reconciliations_migration.py`（新建）
  - `tests/backend/test_migrations.py`（AIH-TASK-231 加的「0040 是 head」断言改为 0041 接在 0040 之后且是 head）
  - `tests/backend/test_payment_receipts_migration.py`（预防：只在 head 变为 0041 让其辅助函数失败时做最小修改）
  - `docs/database-schema.md`
- **验收要点**：
  1. 表、列、CHECK、索引、外键的名字与定义逐条如 §2「数据库」（复合外键的子表索引是 `uq_…_supersedes`）；模型与迁移同名同序，迁移测试比对两边，并断言 MySQL 上除主键外恰好六个索引。
  2. `revision = "0041_provider_cost_recon"`（≤ 32 字符）、`down_revision = "0040_reset_request_index"`。MySQL 两个无条件触发器；SQLite 不建；迁移照 0039 的形状检查、预检、离线输出、只删空表的降级与每个中断窗口的结局（文件头写全 §132 第 13 条分析）。
  3. 13 条 CHECK 与四个外键的名字照 §2「数据库」逐字；测试覆盖 §7 标 AIH-TASK-242 的行（SQLite 与 MySQL 两半；取代行月份不同的外键用例只在 MySQL 上跑，不开 SQLite 的 `PRAGMA foreign_keys`）。
  4. `docs/database-schema.md` 补这张表：口径（UTC 月、范围、只插入、取代链）、与 spec §74.5 字段的对应及 `metadata_json` 不建的理由。
  5. 不加写入方、不加接口；不改任何已有表。

#### AIH-TASK-243

- **标题**：供应商成本对账第二层：范围内估算聚合、诊断计数与对账记录的读写仓储
- **可改路径**：
  - `app/repositories/provider_cost_reconciliations.py`（新建）
  - `tests/backend/provider_cost_world.py`（新建，辅助模块）
  - `tests/backend/conftest.py`
  - `tests/backend/test_provider_cost_reconciliations_repository.py`（新建）
- **验收要点**：
  1. 函数与签名如 §2「仓储」（含 `scope_providers`）；`ai_usage_outbox` 只按表名与三列引用，不 import `app.ai_api`（`test_ai_module_boundary.py` 照常通过、不改它）。
  2. 只用可移植 SQL；只读函数不加锁、不提交；`insert_record` 把 `IntegrityError` 原样抛出；`current_records_overlapping` 一条查询连 `ai_providers`，返回 `(链头行, 供应商代码)` 的列表。
  3. 辅助模块逐条照 §7 建标准世界与 `fx_switch`、`myr_three`、`unallocatable` 三个变体的构建函数，夹具在 conftest 注册，SQLite 与 MySQL 两份。
  4. 测试覆盖 §7 标 AIH-TASK-243 的行，含非 ASCII 与 64 字符 `event_id` 的 MySQL 用例。
  5. PR 正文写明 MySQL `EXPLAIN` 对 `scope_estimate` 的实际计划。

#### AIH-TASK-244

- **标题**：供应商成本对账第三层：预览、确认与更正的业务规则、月末汇率换算和审计
- **可改路径**：
  - `app/services/provider_cost_reconciliation.py`（新建）
  - `app/models/auth.py`
  - `tests/backend/test_provider_cost_reconciliation_service.py`（新建）
  - `tests/backend/test_provider_cost_reconciliation_concurrency.py`（新建）
  - `app/services/audit_query.py`（`provider_cost_reconciliation` 加进 `PUBLIC_ENTITY_TYPES`，:41 起；否则 `tests/backend/test_admin_audit_api.py` 的「每个写入的 entity_type 都已分类」用例失败）
  - `frontend/src/api/adminAudit.ts`、`frontend/src/api/adminAudit.test.ts`（新动作 `PROVIDER_COST_RECONCILIATION_CREATE` 必须与 `AuditAction` 同一个 PR 加：`adminAudit.test.ts:161-163` 按声明顺序比对后端枚举，先例 AIH-TASK-188）
- **验收要点**：
  1. 常量、`reconciliation_period`、`preview`、`confirm`、`months_overview`、`get_detail`（含 `estimate_changed_since_confirmation` 的判定规则）、`allocate` 如 §2「服务」；检查顺序与错误码逐条一致，含第 1a 步 `RECONCILIATION_REASON_INVALID`。
  2. 确认的事务边界与锁顺序如 §2「事务边界」与 §4；唯一冲突先按方言判定（MySQL 1062、SQLite `UNIQUE constraint failed`），再按请求形状（带不带 `supersedes_id`）映射为 409；所有错误都不带 `details`；预览回滚、不写任何行。
  3. 换算、差额、差额率、阈值的计算与舍入如 §2「金额与精度」，全程 Decimal。
  4. 审计动作与字段如 §2「审计」，与记录同一事务；四个视图数据类逐字照 §2，服务不格式化字符串；`preview` 收来源说明与理由，第 1a、5a 步两者都做。
  5. 不 import 钱包、账本、对账单、计费模块；不写 `usage_events`。
  6. 测试覆盖 §7 标 AIH-TASK-244 的行；并发用例的事务 1 跑真实 `confirm`、经 `_after_audit_before_commit(session)` 阻塞，用 `performance_schema.data_lock_waits` 确认事务 2 等锁；`entity_type` 归入 `PUBLIC_ENTITY_TYPES`。

#### AIH-TASK-245

- **标题**：供应商成本对账第四层：管理端对账的月份总览、预览、确认与详情接口
- **可改路径**：
  - `app/schemas/provider_cost_reconciliations.py`（新建）
  - `app/api/admin_provider_cost_reconciliations.py`（新建）
  - `app/main.py`
  - `tests/backend/test_admin_provider_cost_reconciliations_api.py`（新建）
  - `tests/backend/test_admin_customers_api.py`
  - `tests/backend/test_internal_cost_visibility.py`
  - `docs/api.md`
- **验收要点**：
  1. 四个接口的路径、方法、注册顺序、请求体（`extra="forbid"`、金额为字符串）、响应键与错误码如 §2「管理端接口」；首条语句 `require_admin`。
  2. 格式化全在 schema（`money_text`、`rate_text`、`ratio_text`、时间同 `FxRateView`）；预览七个字段、确认九个；未知供应商与非 UUID id 的结果、MYR 记录的汇率三键、`live_estimate` 五个键如 §2。
  3. `EXPECTED_ADMIN_ROUTES` 加四个路径，401 / 403 探测补查询串与请求体。
  4. INV-7 守卫按 §6 追加禁用名与 import 扫描模块，自检照常。
  5. `docs/api.md` 新增「管理端供应商成本对账」一节：口径、四个接口、错误码、不改任何计费金额。
  6. 测试覆盖 §7 标 AIH-TASK-245 的行。

#### AIH-TASK-246

- **标题**：供应商成本对账第五层：管理端供应商成本页的月份总览、录入预览确认与更正
- **可改路径**：
  - `frontend/src/api/adminProviderCosts.ts`（新建）
  - `frontend/src/api/adminProviderCosts.test.ts`（新建）
  - `frontend/src/features/providerCosts/ProviderCostsPage.tsx`（新建）
  - `frontend/src/features/providerCosts/ProviderCostsPage.test.tsx`（新建）
  - `frontend/src/routes/index.tsx`
  - `frontend/src/routes/paths.ts`
  - `frontend/src/layouts/AppLayout.tsx`
  - `frontend/src/i18n/locales/en.json`
  - `scripts/acceptance/admin_customers.mjs`
- **验收要点**：
  1. 语义以 `docs/api.md` 的对账一节为准，交互以 §2「管理端页面」为准；与后端冲突时停下在 PR 里说明；不改后端文件、`client.ts`、`download.ts`。
  2. 确认按钮只在本次预览成功且表单未改动时可用，提交带预览返回的两个期望值；409 估算变化提示重新预览；更正必填理由。
  3. 各列与状态、空状态、「—」、Above 5%、加载失败、409 与 201 后的行为、Correct 预填逐条如 §2「管理端页面」；不用 `Alert`、没有 `role="alert"`；金额原样显示字符串。
  4. 不改 `frontend/src/api/adminAudit.ts`（新动作已由 AIH-TASK-244 同步）；文案进 `en.json`，满足 no-hardcoded-jsx-text 与 keys.test.ts。
  5. 测试覆盖 §7 标 AIH-TASK-246 的行；已有用例照常通过。
  6. `open_provider_costs`：在 `login` 之后经导航打开供应商成本页（href 为 /provider-costs）。`check_provider_cost_months`：须先跑过 `open_provider_costs`（否则 assertion_failed），后端值取页面自己发出的 GET months 响应体（被动观察），断言表格行数等于 `items` 数、每行月份列文字等于 `billing_month`、状态列文字按 `status` 为 `Reconciled` 或 `Not reconciled`；`items` 为空时空状态文字恰为 `No completed months to reconcile yet.` 也算通过；不点任何按钮、不打开表单。两步加进脚本（文件名不变），文件头步骤说明同步；已有步骤行为不变；沿用验收脚本已有的约束；脚本只执行 stdin 里 steps 列出的步骤。
  7. CI 的 frontend job（含 build）全绿。
- **浏览器验收**：`acceptance.browser`，`writes: false`，`timeout_seconds: 300`，steps：`login`、`open_provider_costs`、`check_provider_cost_months`。

#### AIH-TASK-250

- **标题**：供应商成本对账第六层：上月满九天仍未对账时在用量积压告警里提醒三天
- **可改路径**：
  - `app/services/alerts.py`
  - `tests/backend/test_usage_alerts.py`
  - `tests/backend/test_internal_alerts.py`
  - `docs/runbook.md`
  - `docs/api.md`（约第 4740 行逐字写了 `usage_processing_backlog` 的摘要格式，要同步第六段）
- **验收要点**：
  1. `usage_processing_backlog` 追加第六段，判定月、窗口、供应商集合与摘要文案如 §2「告警」；其余五段逐字不变；不新增维度。
  2. 供应商集合用 AIH-TASK-243 的 `scope_providers` 一条查询取得，再逐个调 `current_record`，不调 `scope_estimate`；首月与期间用 AIH-TASK-244 的 `FIRST_RECONCILIATION_MONTH`、`reconciliation_period`；普通读、不加锁。
  3. 摘要只有供应商代码与月份；`app/services/alerts.py:19-23` 文件头「摘要只有计数、日期与币种」的说明同步补上供应商代码；`tests/backend/test_internal_alerts.py:269` 逐字比对的空库摘要随之更新。
  4. runbook 中 `usage_processing_backlog` 那一行补第六段的含义与处置（到供应商成本页录入）。
  5. 测试覆盖 §7 标 AIH-TASK-250 的行。

#### AIH-TASK-251

- **标题**：供应商成本对账收尾：运维手册补换钥与月度对账流程，勾选 Phase 8 对账项
- **可改路径**：
  - `docs/runbook.md`
  - `docs/TODO.md`
  - `docs/currency-and-fx.md`
- **验收要点**：
  1. runbook 新增「供应商成本对账」一节，是操作流程，照 `docs/runbook.md` 中「税务政策：首条政策的发布」一节的流程写法（不套故障场景的五个固定小节），放在「月度对账单」一节之后：换钥步骤（§8 上线后人工步骤 ①–③，不写地址、主机名、工作区真实名称、金额）、每月录入流程、诊断计数的解读、更正方法；`FX_RATE_UNAVAILABLE` 指向 `docs/currency-and-fx.md` §4 的补录（P1 / P4），不写成「发布一个该时刻的汇率」；`SOURCE_CURRENCY_MISMATCH` 在事件币种混杂时写明「本月无法对账，需另走设计闸门」；换钥晚于 2026-12-01 时照 §8 ③ 每月 10 日前推常量；月末时刻落在 P1、P4 都补不了的汇率空档时写「本月暂不能对账，记待办，汇率补录另走设计」。
  2. `docs/currency-and-fx.md` 补一段：对账换算取期间最后一刻生效的已发布版本，与事件逐笔换算的区别。
  3. `docs/TODO.md` **只改 planning-v1 规划块之外的勾选项**：Phase 8「供应商账单成本对账」改 `[x]`，说明同时引用两个闸门：AIH-TASK-242..246、250、251 → 本闸门，AIH-TASK-247..249 → 分摊设计的闸门；Phase 7「收入 / 估算成本 / 对账后成本 / 网关手续费 / 毛利 / 毛利率」在 AIH-TASK-221..225 与 AIH-TASK-247..249（分摊设计）都已合并时改 `[x]`，否则保持 `[ ]` 并在该行写明剩什么。规划块里的「待登记：Phase 8 供应商账单成本对账」与 Phase 7 待登记行**不碰**：它们由本批的登记 PR 删改。
  4. 不改代码与测试。

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
| v1 | 2026-11-17 | 初稿（含 2026-11-16 Kelvin 的决定与五轮登记预审的修订，尚未送审）。原合在一份设计里，因 Issue 正文上限拆出毛利分析分摊（AIH-TASK-247..249）为分摊设计；契约不变 | Kelvin 拍板；登记预审；Issue 正文长度 |

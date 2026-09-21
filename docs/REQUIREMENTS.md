# REQUIREMENTS — 需求索引

> **这不是需求正文。** 正文在 [Acuven_Central_AI_Billing_Platform_Spec.md](Acuven_Central_AI_Billing_Platform_Spec.md)（编号排到 §140，其中三个编号已退役，现存 137 个一级编号章节）。
> 本文件的用途：① 定义「什么算一条硬性要求」并给出覆盖状态；② 按主题快速定位到 spec 的哪一节；③ 提供 `REQ-*` 可追溯性清单。
> 冲突时以 spec 为准。
> 第一、二节由 [tests/test_requirements_coverage.py](../tests/test_requirements_coverage.py) 机械校验，改坏了会红。
> 最后更新：2026-09-21

---

## 一、硬性要求的定义与需求追溯（spec §139.1）

### 1.1 什么算一条「硬性要求」

**定义是一条可枚举的解析规则，不是形容词。** 一条硬性要求 = 下面三类条目中的一条，三类之外的 spec 正文都不算：

1. spec §133 里每一个 `## Invariant N` 小节 —— 正文取该标题之后的第一个非空段落
2. spec §132 那个有序列表里的每一项 Definition of Done
3. spec §132 末尾、有序列表之后那串上线前跨功能闸门里的每一条

按这条规则对当前 spec（v1.7）枚举出 **34 条**：14 条 Invariant + 15 条 DoD + 5 条上线前闸门。
**34 这个数字只是当前快照，不是判定标准** —— 判定标准是上面三条解析规则。spec 增删一条，第 1.3 节的枚举结果就随之变化，校验脚本不需要改（脚本里既没有写死条数，也没有写死任何一条的正文）。

**为什么不能用关键词判定。** spec 不是按 RFC 2119 写的：

- 全文大写 `MUST` / `SHALL` / `NEVER` 合计只有 **14 次**（`MUST` 13 次、`NEVER` 1 次、`SHALL` 0 次），而且散落在 §3、§13、§14、§97 这类章节里，与「哪些条目需要逐条验收」没有对应关系 —— 拿它当硬性要求的全集，会漏掉 §132 和 §133 的绝大多数条目。
- 不分大小写的 `must` 全文出现 **173 次**（其中小写 `must` 160 次），几乎每一节都有 —— 拿它当全集等于没有定义，而且同一句话里的 `must` 有时只是在描述实现细节。
- 也就是说关键词既做不了上界也做不了下界。唯一稳定的抓手是**结构**：`## Invariant N` 是标题，DoD 是有序列表项，上线前闸门是紧随其后的无序列表项。三者都能机械解析，spec 改了会立刻反映出来。

「硬性要求有没有被编号覆盖」的答案在第 1.3 节。**覆盖列只有三种合法写法**：

1. **一个或多个 spec 里真实存在的 `REQ-*` ID** —— 这条硬性要求已经有编号可追溯
2. **固定串「缺口」** —— 它本该有编号，但现有编号都不覆盖它；必须在同一行给出理由
3. **固定串「不适用」** —— 该条是**逐功能的交付流程要求**：它约束的是每个功能交付时要做哪些动作，由 spec §132 的 DoD 与 PR 模板自检逐个功能强制，不构成可独立验收的系统规则，因此不发 `REQ-*` 编号；同样必须在同一行给出理由

「不适用」不是豁免：这些条目照样要在每个功能上被 §132 逐条验收，只是强制手段不是 `REQ-*`。「缺口」也不是失败：它把「本该有编号却没有」点名，而不是让它继续隐形。两种写法都必须在同一行给出理由，[tests/test_requirements_coverage.py](../tests/test_requirements_coverage.py) 会挡住空理由。

### 1.2 关键需求追溯表

实现计划、数据库迁移、API 与测试都必须引用适用的 REQ ID。**没有下表列出的证据（或经明确批准的偏离），该需求不算完成。**

`REQ-*` ID 的唯一来源是 spec 原文。下表与 spec 双向闭合：spec 里出现的每个 `REQ-*` 在表里恰好一行，表里的每个 `REQ-*` 在 spec 里至少出现一次。

| ID | 规范性规则 | 主要章节 | 必需测试证据 |
| --- | --- | --- | --- |
| `REQ-AVAIL-001` | 中心计费故障绝不阻塞终端用户 AI 服务 | §1、§20–§22、§85 | 故障测试 + 试点 E2E |
| `REQ-INGEST-001` | `202` 必须在持久化 `RECEIVED` 之后返回；定价异步 | §20、§38–§39、§82 | API / 崩溃 / 队列丢失测试 |
| `REQ-INGEST-002` | 队列发布本身不构成确认；`RECEIVED` 数据库记录才是持久事实来源，worker 必须能靠扫库恢复 | §20、§82–§83、§110 | 队列丢失 / 崩溃恢复 + 扫库重建测试 |
| `REQ-IDEMP-001` | 全局 `event_id` 至多一次财务效果；ID 复用不匹配即冲突 | §23、§79、§82 | 重复 / 冲突 / 并发测试 |
| `REQ-FIN-001` | 供应商源成本、币种、汇率、MYR 估算与各版本均为历史快照 | §5、§14、§17 | FX / 版本 / 舍入测试 |
| `REQ-FIN-002` | 钱包只能通过不可变账本行变动，用 `Decimal`，每事件仅一次舍入 | §8、§77–§81 | 账本对账 + 并发测试 |
| `REQ-TXN-001` | 钱包变更、由它引起的计费状态跃迁、审计记录与 domain outbox 事件在同一个数据库事务里提交 | §66、§74.6、§81–§82 | 原子提交 + 故障注入回滚 + outbox/账本一致性测试 |
| `REQ-PRICE-001` | 供应商与客户定价按 `occurred_at` 解析出每一个必需分量 | §15–§17、§74 | 分量 / 缺口 / 重叠 / 晚到事件测试 |
| `REQ-AUTH-001` | 按项目的 HMAC 密钥加密存储、版本化、可轮换、永不入日志 | §36–§37、§74.4、§96 | 签名 / 轮换 / 重放 / 密钥泄漏测试 |
| `REQ-STATUS-001` | 账户、计费、项目状态确定性合成，单调传播 | §24–§30 | 状态跃迁 / 乱序 / 对账测试 |
| `REQ-PAY-001` | 可信 MYR 支付只入账一次；金额不符绝不自动入账；丢失 Webhook 由对账补回 | §40–§45、§74.7、§110 | 支付安全 + 对账测试 |
| `REQ-TAX-001` | SST 口径经批准后 Phase 4 方可开工，并落进版本化税务政策快照与引用它的不可变财务快照 | §45.1、§74.9、§127 | 税务政策版本快照 + 收据 / 对账单字段测试 |
| `REQ-PERIOD-001` | T+1 定稿对账单不可变；之后的活动记为上期调整 | §19、§46、§74.8 | 月度边界 + rebill 测试 |
| `REQ-OPS-001` | 专用存储满足 RPO/RTO；持久化工作可在无 Redis 时重建 | §95、§98–§99、§132 | 恢复演练 + 队列丢失测试 |
| `REQ-PRIV-001` | 计费只存元数据，按数据分类执行已批准的留存策略 | §13、§94、§112 | 隐私 / 留存 / 租户隔离测试 |
| `REQ-PRIV-002` | 客户可见面绝不暴露估算 / 对账供应商成本、markup、毛利与内部供应商定价 | §14、§46、§68–§69 | 门户 / 对账单 / 导出字段可见性测试 |
| `REQ-LAUNCH-001` | 上线前用真实账号验证支付、Email、WhatsApp 三条集成 | §40–§41、§47–§50、§99 | 真实账号联调演练记录 + 上线前变更检查单 |

### 1.3 硬性要求 → REQ 覆盖表

按第 1.1 节的规则枚举出的每一条硬性要求在下表恰好一行。**覆盖**列的三种合法写法见第 1.1 节：一个或多个 spec 里真实存在的 `REQ-*` ID、固定串「缺口」、固定串「不适用」；后两种都必须在同一行给出理由。

判定「覆盖」的口径：该 `REQ-*` 的**规范性规则、主要章节或必需测试证据**里明确含有这条硬性要求的内容（第 1.2 节三列都算）。仅仅「主题相邻」不算覆盖 —— 那正是要点名成缺口的东西。

当前 34 条里 **23 条有覆盖、11 条不适用、0 条缺口**。AIH-TASK-008（2026-09-21）按 Kelvin 拍板的分档口径收口了原先的 15 个缺口：Invariant 7、Invariant 13、上线前闸门 1 与 4 这四条是可独立验收的系统规则，已在 spec 里就地新增 `REQ-PRIV-002` / `REQ-TXN-001` / `REQ-TAX-001` / `REQ-LAUNCH-001`；DoD 1–8 与 13–15 共 11 条是逐功能的交付流程要求，按第 1.1 节第 3 种写法标「不适用」。

| 硬性要求 | spec 原文（逐字） | 覆盖 | 缺口 / 不适用理由 |
| --- | --- | --- | --- |
| Invariant 1 | Central Billing failure must NOT stop customer AI service. | `REQ-AVAIL-001` | — |
| Invariant 2 | Same Usage Event must never financially charge twice. | `REQ-IDEMP-001` | — |
| Invariant 3 | Same Payment must never credit Wallet twice. | `REQ-PAY-001` | — |
| Invariant 4 | Wallet balance changes only through ledger transactions. | `REQ-FIN-002` | — |
| Invariant 5 | Historical ledger entries are immutable. | `REQ-FIN-002` | — |
| Invariant 6 | Historical Usage Event retains pricing/version references. | `REQ-FIN-001`、`REQ-PRICE-001` | — |
| Invariant 7 | Customer cannot see Acuven Estimated/Reconciled Provider Cost or Margin. | `REQ-PRIV-002` | — |
| Invariant 8 | Customer cannot access another tenant's data. | `REQ-PRIV-001` | — |
| Invariant 9 | AI conversation content is not stored in Billing Platform. | `REQ-PRIV-001` | — |
| Invariant 10 | All monetary calculations use Decimal, not float. | `REQ-FIN-002` | — |
| Invariant 11 | A globally unique Usage Event ID can produce at most one financial effect; mismatched reuse is a conflict, never a duplicate success. | `REQ-IDEMP-001` | — |
| Invariant 12 | Finalized statements are immutable. Late usage and cross-period rebills are posted as explicit prior-period adjustments in an open period. | `REQ-PERIOD-001` | — |
| Invariant 13 | Wallet mutation, billing-status transition, audit record, and durable outbound domain events are committed atomically where the transition is caused by that mutation. | `REQ-TXN-001` | — |
| Invariant 14 | Redis/Celery loss must not destroy durable financial, payment, notification, Webhook, or document-generation work. | `REQ-OPS-001`、`REQ-INGEST-002` | — |
| DoD 1 | database migration exists | 不适用 | 逐功能的交付流程要求：表结构变化由每个功能自己的迁移承担，由 §132 的 DoD 与 PR 模板自检逐个功能强制。它约束的是交付动作，不是系统在运行时的行为，没有可独立验收的系统规则能写进 REQ |
| DoD 2 | backend validation exists | 不适用 | 逐功能的交付流程要求：入参校验是每个接口自己的事（§107 只统一错误响应格式），由 §132 的 DoD 与 PR 模板自检逐个功能强制。「都要校验」脱离具体接口无从验收 |
| DoD 3 | authorization exists | 不适用 | 逐功能的交付流程要求：管理端与客户门户每个接口各自的授权检查（§55、§67、§89–§90），由 §132 的 DoD 与 PR 模板自检逐个功能强制。集成侧按项目的 HMAC 凭据那部分是可独立验收的系统规则，已由 `REQ-AUTH-001` 覆盖 |
| DoD 4 | audit logging exists where required | 不适用 | 逐功能的交付流程要求：哪些操作「where required」要写审计由功能自己判断（§66 定字段与查询面），由 §132 的 DoD 与 PR 模板自检逐个功能强制。审计与钱包变更同事务提交这条系统规则另由 `REQ-TXN-001` 覆盖 |
| DoD 5 | tests exist | 不适用 | 逐功能的交付流程要求，由 §132 的 DoD 与 PR 模板自检逐个功能强制。**这条最能说明为什么不发编号**：发了之后第 1.2 节的「必需测试证据」列只能填「有测试」，与本节自己的验收口径循环 |
| DoD 6 | error handling exists | 不适用 | 逐功能的交付流程要求：每个功能各自的失败路径（§83 定「绝不静默丢弃」、§107 定响应格式），由 §132 的 DoD 与 PR 模板自检逐个功能强制。「计费故障不阻塞 AI 服务」是其中可独立验收的那条路径，已由 `REQ-AVAIL-001` 覆盖 |
| DoD 7 | API contract documented | 不适用 | 逐功能的交付流程要求：每个接口写进 §136 点名的 `docs/api.md`，由 §132 的 DoD 与 PR 模板自检逐个功能强制。产出文档是交付动作，不是系统规则 |
| DoD 8 | frontend loading/error states exist | 不适用 | 逐功能的交付流程要求：每个前端页面各自的加载态与错误态，由 §132 的 DoD 与 PR 模板自检逐个功能强制。没有对应的后端系统规则可编号 |
| DoD 9 | tenant isolation verified | `REQ-PRIV-001` | — |
| DoD 10 | financial idempotency verified where applicable | `REQ-IDEMP-001` | — |
| DoD 11 | monetary precision, version selection, and accounting-period tests exist where applicable | `REQ-FIN-001`、`REQ-FIN-002`、`REQ-PRICE-001`、`REQ-PERIOD-001` | — |
| DoD 12 | durable job state can recover from Redis/Celery loss where applicable | `REQ-OPS-001`、`REQ-INGEST-002` | — |
| DoD 13 | migrations include production locking, backup, and failure-handling analysis | 不适用 | 逐功能的交付流程要求：**每次迁移**各自给出生产锁表、备份与失败处理分析，由 §132 的 DoD 与 PR 模板自检逐个功能强制。存储级 RPO/RTO 与无 Redis 重建（§98.1）是可独立验收的系统规则，已由 `REQ-OPS-001` 覆盖 |
| DoD 14 | monitoring, alert ownership, and runbook coverage exist | 不适用 | 逐功能的交付流程要求：每个功能各自配齐监控指标、告警归属与 runbook 场景（§95、§120、§136），由 §132 的 DoD 与 PR 模板自检逐个功能强制。RPO/RTO 那部分已由 `REQ-OPS-001` 覆盖 |
| DoD 15 | performance and latency targets are verified for volume-sensitive features | 不适用 | 逐功能的交付流程要求：§119 的量化目标要由每个「量敏感」功能各自验证，由 §132 的 DoD 与 PR 模板自检逐个功能强制。哪个功能算量敏感是逐功能判断，写不成一条能独立验收的系统规则 |
| Gate 1 | SST/accounting treatment approved and reflected in immutable financial snapshots | `REQ-TAX-001` | — |
| Gate 2 | PDPA and data-retention policy approved | `REQ-PRIV-001` | — |
| Gate 3 | RPO/RTO restore drill passed | `REQ-OPS-001` | — |
| Gate 4 | payment, Email, and WhatsApp real-account integration verified | `REQ-LAUNCH-001` | — |
| Gate 5 | protected `main` CI/CD deployment and recovery procedure verified | `REQ-OPS-001` | — |

---

## 二、按主题的章节索引

spec 里每个一级编号章节（`# N.` 形式的标题）在本节**恰好一行**。已退役的编号保留原行并就地标注退役版本 —— spec 的编号是稳定引用，退役后留空、不重用。

> ⚠️ 已退役的编号在本文件里一律写成**纯数字**（表格第一列），不写成 `§` 形式：`scripts/check_docs.py` 把 `§N` 当作 spec 章节引用校验，而这些编号在 spec 里已经没有对应标题，写了会让 docs 检查以 unknown spec section 失败。

### A. 产品与架构基础（§1–§4）

| § | 标题 | 说明 |
| --- | --- | --- |
| 1 | Project Objective | 目标、能力清单、`REQ-AVAIL-001` |
| 2 | Core Architecture Principle | 中心化而非各自内嵌；系统上下文图 |
| 2.1 | Terminology and Trust Boundary | 「Integrated Application Backend」定义与信任边界 |
| 3 | Technology Stack | 后端 / 前端 / 现有应用后端三段技术栈 |
| 4 | Multi-Tenant Model | 租户—项目—**共享单钱包** |

### B. 货币、钱包与账本（§5–§8）

| § | 标题 | 说明 |
| --- | --- | --- |
| 5 | Currency | 仅 MYR，供应商 USD 需换算 |
| 6 | Wallet Model | 预付钱包模型 |
| 7 | Wallet Rules | 余额规则、透支、停机阈值 |
| 8 | Wallet Ledger | 不可变账本，每笔余额变动一行 |

### C. 用量计量与隐私（§9–§13）

| § | 标题 | 说明 |
| --- | --- | --- |
| 9 | AI Usage Granularity | 请求级粒度 |
| 10 | Conversation Model | 会话聚合模型 |
| 11 | AI Usage Event | 用量事件契约 |
| 12 | Generalized Metering Model | 泛化计量单位（token / 音频秒 / OCR 页…） |
| 13 | Privacy Requirement | 不存对话内容 |

### D. 成本与定价引擎（§14–§19）

| § | 标题 | 说明 |
| --- | --- | --- |
| 14 | Provider Cost vs Customer Charge | 估算成本 / 对账成本 / 客户计费额 / 毛利四者区分 |
| 15 | Pricing Engine | MARKUP 与 FIXED_RATE 两种策略 |
| 15.1 | Generalized Price Components | 泛化价格分量（含缓存 token、非 token 单位） |
| 16 | Pricing Rule Resolution | 定价规则解析链 |
| 17 | Provider Cost Price Management | 供应商价格版本化 |
| 17.1 | Foreign Exchange Rate Management | 汇率版本化与管理 |
| 18 | Provider Price Synchronization | 供应商价格同步 |
| 19 | Rebill / Reprocess | 补偿交易而非改历史 |

### E. 异步摄取与幂等（§20–§23）

| § | 标题 | 说明 |
| --- | --- | --- |
| 20 | Asynchronous Billing Requirement | 摄取快返回，定价异步；`REQ-INGEST-001` / `REQ-INGEST-002` |
| 21 | Local Transactional Outbox | 应用侧本地事务性 outbox |
| 22 | Outbox Delivery | 投递重试与退避 |
| 23 | Exactly-Once Financial Effect | 幂等与冲突检测 |

### F. 状态模型与传播（§24–§31）

| § | 标题 | 说明 |
| --- | --- | --- |
| 24 | Customer Status Model | 客户状态机 |
| 25 | Suspension Behavior | 中心侧停机行为 |
| 26 | Integrated Application Backend Suspension Behavior | 应用侧停机行为（唯一的消费拦截点） |
| 27 | Reactivation | 复机 |
| 28 | Status Webhook | 状态推送 payload |
| 29 | Webhook Retry | 重试策略 |
| 30 | Periodic Status Reconciliation | 周期性状态对账兜底 |
| 31 | Outbox Health Monitoring | outbox 积压监控 |

### G. 供应商集成（§32–§34）

| § | 标题 | 说明 |
| --- | --- | --- |
| 32 | Provider Usage Extraction Layer | 供应商用量提取抽象层 |
| 33 | Anthropic Integration | Claude 用量字段（含缓存 token） |
| 34 | OpenAI Speech Transcription | 语音转写按音频秒计量 |

### H. 应用后端集成与接口（§35–§39）

| § | 标题 | 说明 |
| --- | --- | --- |
| 35 | Integrated Application Billing Client | 客户端 SDK |
| 36 | Integration Authentication | 按项目凭据 |
| 37 | API Request Signing | HMAC 签名与时间窗（注意：签名时间戳 ≠ `occurred_at`） |
| 38 | Integration API | 摄取 API 契约 |
| 39 | Batch Ingestion | 批量摄取与逐条结果 |

### I. 支付、收据、对账单、通知（§40–§50）

| § | 标题 | 说明 |
| --- | --- | --- |
| 40 | Payment Gateway Architecture | 网关适配器接口 |
| 41 | Payment Gateway Selection | 马来西亚网关选型（FPX 优先） |
| 42 | Top-Up | 充值规则，金额不许硬编码 |
| 43 | Payment Flow | 支付流程 |
| 44 | Payment Gateway Fees | 网关手续费记账 |
| 45 | Payment Receipt | 收据字段 |
| 45.1 | Tax and Compliance Decision Gate | **SST 税务口径决策闸门（上线前必须过）** |
| 46 | Monthly Statement | 月度对账单、T+1 cut-off、上期调整 |
| 47 | Customer Notifications | 通知总览 |
| 48 | Low Balance Alert | 低余额告警 |
| 49 | Suspension Alert | 停机通知 |
| 50 | Reactivation Alert | 复机通知 |

### J. 认证与门户（§51–§73）

| § | 标题 |
| --- | --- |
| 51 | Authentication |
| 52 | Customer User Model |
| 53 | Login Security |
| 54 | Two-Factor Authentication |
| 55 | Admin Portal（含 Dashboard） |
| 56 | Customer Management |
| 57 | Project Management |
| 58 | AI Provider Management |
| 59 | Pricing Management |
| 60 | Wallet Management |
| 61 | Usage Management |
| 62 | Reprocessing UI |
| 63 | Payment Management |
| 64 | Receipt Management |
| 65 | Statement Management |
| 66 | Audit Log |
| 67 | Customer Portal |
| 68 | Customer Usage Page |
| 69 | Customer Request Detail |
| 70 | Customer Wallet Page |
| 71 | Customer Payment Page |
| 72 | *(已退役 —— spec v1.6 撤销，编号留空不重用；内容已由 §46 与 §90 覆盖)* |
| 73 | Export Functionality |

### K. 数据模型与事务处理（§74–§87）

| § | 标题 | 说明 |
| --- | --- | --- |
| 74 | Core Database Tables | 核心表总览 |
| 74.1 | provider_price_versions / provider_price_components | 供应商价格版本与分量 |
| 74.2 | fx_rate_versions | 汇率版本 |
| 74.3 | pricing_rules / pricing_rule_components | 客户定价规则与分量 |
| 74.4 | integration_credentials | 集成凭据（加密存储） |
| 74.5 | provider_cost_reconciliations | 供应商账单对账 |
| 74.6 | domain_outbox | 中心侧领域事件 outbox |
| 74.7 | payments / payment_gateway_events | 支付与网关事件 |
| 74.8 | monthly_statements | 月度对账单 |
| 74.9 | tax_policy_versions | 税务政策版本 |
| 75 | tenants | 租户表 |
| 76 | projects | 项目表 |
| 77 | wallets | 钱包表 |
| 78 | wallet_transactions | 不可变账本表 |
| 79 | usage_events | 用量事件表 |
| 80 | Pricing Precision | `DECIMAL(20,8)`、单次舍入 |
| 81 | Wallet Concurrency | 钱包并发与行锁 |
| 82 | Processing Usage Event | 计费处理主流程伪代码 |
| 83 | Handling Billing Processing Failure | 失败处理，**绝不静默丢弃** |
| 84 | Unknown Model Handling | 未知模型处理 |
| 85 | Integration Availability Philosophy | 可用性哲学（接受计费延迟，不接受服务中断） |
| 86 | Financial Dashboard Metrics | 财务指标（区分「充值」与「已确认收入」） |
| 87 | Analytics Dimensions | 分析维度 |

### L. API、安全、运维、部署（§88–§100）

| § | 标题 |
| --- | --- |
| 88 | Authentication API |
| 89 | Admin APIs |
| 90 | Customer APIs |
| 91 | Payment Webhook API |
| 92 | Central → Customer Status Webhook |
| 93 | Document Storage |
| 94 | Logging |
| 95 | Monitoring |
| 96 | Security |
| 97 | Data Isolation |
| 98 | Central Database Isolation |
| 98.1 | Production Recovery and Durability（RPO/RTO、备份、恢复演练） |
| 99 | Deployment |
| 100 | Future Scalability Requirement |

### M. 结构、集成流程与通用约定（§101–§112）

| § | 标题 | 说明 |
| --- | --- | --- |
| 101 | Suggested Repository Structure | ⚠️ 已被 ARCHITECTURE.md 第 9 节取代 |
| 102 | *(已退役 —— spec v1.4 撤销，编号留空不重用)* | 目录布局以 [ARCHITECTURE.md](ARCHITECTURE.md) 第 9 节为准 |
| 103 | Integrated Application Backend Request Flow | 应用侧请求流 |
| 104 | Conversation Session Table | 会话表 |
| 105 | Conversation Lifecycle | 会话生命周期与关闭原因 |
| 106 | Receipt and Statement PDF | PDF 生成 |
| 107 | API Response Standard | 统一响应格式 |
| 108 | Pagination | 分页 |
| 109 | Time | 时间与时区 |
| 110 | Scheduled Jobs | Celery Beat 任务清单（含支付对账、stale processing recovery） |
| 111 | Aggregation | 聚合不替代明细 |
| 112 | Data Retention | 数据留存 |
| 112.1 | Account Closure and Balance Settlement | 销户与余额清算 |

### N. 测试与性能（§113–§120）

| § | 标题 |
| --- | --- |
| 113 | Testing Strategy（含单元测试清单） |
| 114 | Integration Tests |
| 115 | Tenant Isolation Tests |
| 116 | Failure Tests |
| 117 | Concurrency Tests |
| 118 | Payment Security Tests |
| 119 | Performance Target |
| 120 | Operational Admin Alerts |

### O. 范围、阶段、验收与不变量（§121–§133）

| § | 标题 | 说明 |
| --- | --- | --- |
| 121 | V1 Out of Scope | 明确不做的 11 项 |
| 122 | Future Architecture Compatibility | 为未来留路但现在不实现 |
| 123 | Development Phases | Phase 0 — Foundation；见 [TODO.md](TODO.md) |
| 124 | Phase 1 — Tenant, Project & Wallet Core | 见 [TODO.md](TODO.md) |
| 125 | Phase 2 — AI Usage Billing Engine | 见 [TODO.md](TODO.md) |
| 126 | Phase 3 — Existing Integrated Application Backend | 试点范围与验收；见 [TODO.md](TODO.md) |
| 127 | Phase 4 — Payment & Customer Portal | 见 [TODO.md](TODO.md) |
| 128 | Phase 5 — Usage Portal | 见 [TODO.md](TODO.md) |
| 129 | Phase 6 — Notifications & Status Automation | 见 [TODO.md](TODO.md) |
| 130 | Phase 7 — Statements & Financial Analytics | 见 [TODO.md](TODO.md) |
| 131 | Phase 8 — Provider Price Synchronization & Rebill | 见 [TODO.md](TODO.md) |
| 132 | Definition of Done | 15 条 DoD + 5 条上线前跨功能闸门；**硬性要求来源之一**，见第 1.1 节 |
| 133 | Critical Invariants | 14 条不变量，见 [ARCHITECTURE.md](ARCHITECTURE.md) 第 6 节；**硬性要求来源之一** |

### P. 试点、开发规则与目标体验（§134–§140）

| § | 标题 | 说明 |
| --- | --- | --- |
| 134 | First Pilot Integration | 试点 = `ai_chatbot_demo` |
| 135 | Implementation Constraints | 约束每个实现阶段的八条（v1.6 由「Codex Development Rules」改名并瘦身） |
| 136 | Required Documentation Produced by Codex | 需维护的文档 + runbook 场景清单 |
| 137 | Recommended Initial Codex Instruction | 起步指令：先域模型与财务不变量，不要先写前端页面 |
| 138 | *(已退役 —— spec v1.4 撤销，编号留空不重用)* | 字段均已由 §67–§69 规范 |
| 139 | Final Admin Experience | 管理端：端到端可审计的财务链路 |
| 139.1 | Critical Requirement Traceability | 见本文件第 1.2 节 |
| 140 | Overall Architectural Decision | 权威架构决策 |

---

## 三、待办的需求编号工作

评审 P2-24 指出 spec 层级偏平、缺可追溯编号。v1.1 已修掉 §3.2 / §3.3 的标题层级错误，并新增了「Document Navigation」导航段和一批 `REQ-*` ID（见本文件第 1.2 节）。

AIH-TASK-007（2026-09-21）收口了其中一条，AIH-TASK-008（2026-09-21）收口了第一条，剩余状态如下：

- [x] `REQ-*` **覆盖状态已逐条落定**：第 1.3 节按第 1.1 节的可枚举定义列出当前全部 34 条硬性要求，**一个「缺口」都不剩** —— 23 条有 `REQ-*` 覆盖（其中四条是 AIH-TASK-008 在 spec v1.7 就地新增的 `REQ-PRIV-002` / `REQ-TXN-001` / `REQ-TAX-001` / `REQ-LAUNCH-001`），11 条是逐功能的交付流程要求，按第 1.1 节第 3 种写法标「不适用」并逐条给了理由
- [x] **一级编号章节的完整索引已补齐**：第二节现在对 spec 里每个 `# N.` 章节各列一行（§123–§131 由一行合并行改为九行逐节列出），退役编号 72 / 102 / 138 就地标注版本；[tests/test_requirements_coverage.py](../tests/test_requirements_coverage.py) 机械校验「缺哪一节、多哪一个编号」
- [ ] §139.1 写成了 `# 139.1`，与 §17.1 / §45.1 用 `##` 的写法不一致（纯瑕疵）。**不在 AIH-TASK-008 范围**：本任务对 [Acuven_Central_AI_Billing_Platform_Spec.md](Acuven_Central_AI_Billing_Platform_Spec.md) 只许新增段落，全文唯一允许被改写的行是文件头的 `**Version:**`（这是 PR 里「删除行恰好一行」那条机械证据的前提）；改标题层级要改写既有行，且它是排版勘误而不是可追溯性标注，留待下一次 spec 勘误一并处理

这些**不阻塞** Phase 0；剩下的一条是纯排版瑕疵，不影响 §132 的可验收性。

## 四、评审意见的落实状态

✅ **逐条核对已完成（2026-09-10）** —— 结果见 [REVIEW_FOLLOWUP_v1.1.md](archive/REVIEW_FOLLOWUP_v1.1.md)。

结论：25 条里 20 条已解决，5 条有残留（R1–R5，已进 [TODO.md](TODO.md)）。**7 条 P0 阻断级全部解决**，Phase 0 / Phase 1 没有被评审意见卡住的地方。

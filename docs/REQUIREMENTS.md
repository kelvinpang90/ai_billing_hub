# REQUIREMENTS — 需求索引

> **这不是需求正文。** 正文在 [Acuven_Central_AI_Billing_Platform_Spec_v1.3.md](Acuven_Central_AI_Billing_Platform_Spec_v1.3.md)（140 节）。
> 本文件的用途：① 按主题快速定位到 spec 的哪一节；② 提供 `REQ-*` 可追溯性清单。
> 冲突时以 spec 为准。
> 最后更新：2026-09-10

---

## 一、关键需求追溯表（spec §139.1）

实现计划、数据库迁移、API 与测试都必须引用适用的 REQ ID。**没有下表列出的证据（或经明确批准的偏离），该需求不算完成。**

| ID | 规范性规则 | 主要章节 | 必需测试证据 |
| --- | --- | --- | --- |
| `REQ-AVAIL-001` | 中心计费故障绝不阻塞终端用户 AI 服务 | §1、§20–§22、§85 | 故障测试 + 试点 E2E |
| `REQ-INGEST-001` | `202` 必须在持久化 `RECEIVED` 之后返回；定价异步 | §20、§38–§39、§82 | API / 崩溃 / 队列丢失测试 |
| `REQ-IDEMP-001` | 全局 `event_id` 至多一次财务效果；ID 复用不匹配即冲突 | §23、§79、§82 | 重复 / 冲突 / 并发测试 |
| `REQ-FIN-001` | 供应商源成本、币种、汇率、MYR 估算与各版本均为历史快照 | §5、§14、§17 | FX / 版本 / 舍入测试 |
| `REQ-FIN-002` | 钱包只能通过不可变账本行变动，用 `Decimal`，每事件仅一次舍入 | §8、§77–§81 | 账本对账 + 并发测试 |
| `REQ-PRICE-001` | 供应商与客户定价按 `occurred_at` 解析出每一个必需分量 | §15–§17、§74 | 分量 / 缺口 / 重叠 / 晚到事件测试 |
| `REQ-AUTH-001` | 按项目的 HMAC 密钥加密存储、版本化、可轮换、永不入日志 | §36–§37、§74.4、§96 | 签名 / 轮换 / 重放 / 密钥泄漏测试 |
| `REQ-STATUS-001` | 账户、计费、项目状态确定性合成，单调传播 | §24–§30 | 状态跃迁 / 乱序 / 对账测试 |
| `REQ-PAY-001` | 可信 MYR 支付只入账一次；金额不符绝不自动入账；丢失 Webhook 由对账补回 | §40–§45、§74.7、§110 | 支付安全 + 对账测试 |
| `REQ-PERIOD-001` | T+1 定稿对账单不可变；之后的活动记为上期调整 | §19、§46、§74.8 | 月度边界 + rebill 测试 |
| `REQ-OPS-001` | 专用存储满足 RPO/RTO；持久化工作可在无 Redis 时重建 | §95、§98–§99、§132 | 恢复演练 + 队列丢失测试 |
| `REQ-PRIV-001` | 计费只存元数据，按数据分类执行已批准的留存策略 | §13、§94、§112 | 隐私 / 留存 / 租户隔离测试 |

---

## 二、按主题的章节索引

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
| 20 | Asynchronous Billing Requirement | 摄取快返回，定价异步 |
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
| 72 | Customer Statements Page |
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
| 102 | Integrated Application Backend Structure | 应用侧模块布局建议 |
| 103 | Integrated Application Backend Request Flow | 应用侧请求流 |
| 104 | Conversation Session Table | 会话表 |
| 105 | Conversation Lifecycle | 会话生命周期与关闭原因 |
| 106 | Receipt and Statement PDF | PDF 生成 |
| 107 | API Response Standard | 统一响应格式 |
| 108 | Pagination | 分页 |
| 109 | Time | 时间与时区 |
| 110 | Scheduled Jobs | Celery Beat 任务清单（含支付对账） |
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
| 123–131 | Development Phases 0–8 | 见 [TODO.md](TODO.md) |
| 132 | Definition of Done | 15 条 DoD + 5 条上线前跨功能闸门 |
| 133 | Critical Invariants | 14 条不变量，见 [ARCHITECTURE.md](ARCHITECTURE.md) 第 6 节 |

### P. 试点、开发规则与目标体验（§134–§140）

| § | 标题 | 说明 |
| --- | --- | --- |
| 134 | First Pilot Integration | 试点 = `ai_chatbot_demo` |
| 135 | Codex Development Rules | 实现方开发规则 |
| 136 | Required Documentation Produced by Codex | 需维护的文档 + runbook 场景清单 |
| 137 | Recommended Initial Codex Instruction | 起步指令：先域模型与财务不变量，不要先写前端页面 |
| 138 | Final Target User Experience | 客户端目标体验 |
| 139 | Final Admin Experience | 管理端目标体验 |
| 139.1 | Critical Requirement Traceability | 见本文件第一节 |
| 140 | Overall Architectural Decision | 权威架构决策 |

---

## 三、待办的需求编号工作

评审 P2-24 指出 spec 层级偏平、缺可追溯编号。v1.1 已修掉 §3.2 / §3.3 的标题层级错误，并新增了「Document Navigation」导航段和 13 个 `REQ-*` ID（见本文件第一节）。

仍有残留：

- [ ] `REQ-*` 只覆盖 13 条关键要求，**不是每条硬性要求**；§132 的 Definition of Done 要求逐条验收，细粒度编号仍缺
- [ ] 140 个一级标题仍无完整 TOC（Document Navigation 是粗粒度区间，不是目录）
- [ ] §139.1 写成了 `# 139.1`，与 §17.1 / §45.1 用 `##` 的写法不一致（纯瑕疵）

这些**不阻塞** Phase 0，但会影响 §132 的可验收性，建议在 Phase 1 之前处理。

## 四、评审意见的落实状态

✅ **逐条核对已完成（2026-09-10）** —— 结果见 [REVIEW_FOLLOWUP_v1.1.md](REVIEW_FOLLOWUP_v1.1.md)。

结论：25 条里 20 条已解决，5 条有残留（R1–R5，已进 [TODO.md](TODO.md)）。**7 条 P0 阻断级全部解决**，Phase 0 / Phase 1 没有被评审意见卡住的地方。

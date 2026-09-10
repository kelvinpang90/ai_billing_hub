# TODO — 开发任务清单

> 任务骨架来自 spec §123–§131（Phase 0–8）。验收标准直接抄自 spec，**不要自行放宽**。
> 最后更新：2026-09-10

---

## 本文件的约定

1. 一个 session 做一个编号任务：读本文件 → 做完 → 验收 → commit，中途不需要逐步审批。
2. **对计划本身有异议时才停下来讨论**，不要在每个任务上重新走一遍「先计划、等确认」。
3. 做完把 `[ ]` 改成 `[x]`，并在该任务下补一行记录：做了什么、偏离了什么、验证到什么程度。
4. 本文件里**没有**的工作（临时需求、探索型任务）才需要先写计划、等确认。
5. 每个 Phase 的每个功能都必须过 spec §132 的 15 条 Definition of Done，否则不算完成。

> ℹ️ `CLAUDE.md` / `AGENTS.md` 的「任务管理」一节已指向本文件（2026-09-10 修正，此前指向不存在的 `tasks/todo.md`）。

---

## P-1. 前置决策（不写代码，但决定表结构 —— 必须先做）

这些是 [SPEC_REVIEW_v1.0.md](SPEC_REVIEW_v1.0.md) 的「建议的处理顺序」，即使 v1.1 已修订，落地前仍需确认结论并落成 ADR。

- [ ] **D1 — 汇率来源与版本化策略**：数据源（人工录入 / API 供应商）、更新频率、按 `occurred_at` 还是结算日取值、`provider_price_versions` 存 USD 原价还是换算后 MYR。→ ADR
- [ ] **D2 — SST 税务口径**：向会计确认。收 RM100 是含税还是不含税？税在收款时确认还是消费时确认？receipt / statement 表要预留哪些字段。（spec §45.1 是上线闸门，但**字段现在就要留**，事后加等于重做所有历史凭证）→ ADR
- [ ] **D3 — 生产数据库隔离**：**结论已由 spec §98 给出 —— 专用 MySQL/Redis 容器、凭据、库、持久卷、资源限制、备份任务，不与现有应用共享实例**（即不接 `vps_infra` 的 `infra_mysql` / `infra_redis`）。本项只剩：落成 ADR 记录理由。→ ADR
- [ ] **D4 — 凭据加密方案**：KMS / 应用层信封加密的具体选型、主密钥保管与恢复流程。→ ADR
- [ ] **D5 — 财务期间与 cut-off**：T+N 的 N 取值、晚到事件的归属规则、Prior Period Adjustment 在对账单上的呈现。→ ADR
- [ ] **D6 — 支付网关选型**：马来西亚网关，FPX 优先。含手续费结构、沙箱可用性、对账 API 能力。→ ADR
- [ ] **D7 — 通知通道**：Email adapter 用什么；WhatsApp 复用 `whatsapp_gateway` 还是自建出站。

---

## P-2. 评审残留项（R1–R5）

来自 [REVIEW_FOLLOWUP_v1.1.md](REVIEW_FOLLOWUP_v1.1.md)：25 条评审意见中 20 条已在 spec v1.1 解决，以下 5 条有残留。

- [ ] **R1 — 停机/复机阈值**：阈值仍硬编码 `balance <= 0`（§49），无可配置 `suspend_at` / `resume_at` 与滞后带。§7.10 的「RM0 保持挂起 + 最小恢复额」已消除死锁，抖动也被 §7.11/§48 的跃迁触发挡住。**决定采纳双阈值，还是明确记为已知取舍。**（Phase 2 前）
- [ ] **R2 — 并发 PENDING payment**：同一租户是否允许并存多笔 `PENDING` 支付，spec 无任何规定。客户连点两次充值 → 两笔都付了怎么办、UI 显示哪一笔。**Phase 4 前必须定。**
- [ ] **R3 — 账本膨胀权衡**：§82 已承认钱包变更按租户串行化并要求测最热租户，§119 给了量化目标，但**没有对「按对话/时间窗聚合成一笔 AI_USAGE ledger、usage_events 保留明细」做权衡分析**。即使决定不做，也要写明理由。（Phase 2 前）
- [ ] **R4 — 重放保护存储与 TTL**：§37 只说「别把 Redis 硬编码成唯一重放存储」，**nonce 的存储介质与 TTL 未定义**（应等于签名时间窗大小）。（Phase 2 前，影响 §37 实现）
- [ ] **R5 — 需求编号覆盖度**：`REQ-*` 只有 13 条，未覆盖每条硬性要求；140 节仍无完整 TOC。影响 §132 逐条验收。（Phase 1 前）

---

## Phase 0 — Foundation（§123）

- [ ] 仓库骨架：`app/`、`tests/`、Alembic、`.env.example`、`README.md`
- [ ] Docker Compose：nginx · frontend · api · celery-worker · celery-beat · redis · mysql
- [ ] FastAPI 结构（`app/api` / `core` / `models` / `schemas` / `repositories` / `services` / `tasks`）
- [ ] React 结构（Vite + TS + Ant Design + i18n 骨架）
- [ ] MySQL + Redis + Celery 接通
- [ ] 认证基座
- [ ] 日志与统一错误处理（§94、§107）
- [ ] CI 测试结构
- [ ] 私有 GitHub 仓库 + 受保护 `main` + CI/CD
- [ ] 专用生产 MySQL/Redis 拓扑（依赖 D3）
- [ ] 备份、恢复、加密密钥方案设计（依赖 D4）
- [ ] 初始性能 / SLO 基线
- [ ] FX 供应商评估（依赖 D1）

**验收**：所有服务能起 · DB 迁移能跑 · 管理员能登录 · 2FA 可用 · CI 拦住合并并能部署不可变镜像 · RPO/RTO 恢复方案已批准

---

## Phase 1 — Tenant, Project & Wallet Core（§124）

- [ ] Tenant
- [ ] Project
- [ ] 管理端客户管理
- [ ] Wallet
- [ ] 不可变钱包账本
- [ ] 管理员手工调账
- [ ] API 凭据（加密存储、版本化、可轮换）
- [ ] 审计日志

**验收**：管理员建客户 → 自动有钱包 · 建 project · 建 API 凭据 · 调账生效 · 所有动作都有审计

> 🔒 Phase 1 测试不通过，不得进入 Phase 2（§137）。

---

## Phase 2 — AI Usage Billing Engine（§125）

- [ ] Provider / Model / Usage Meter
- [ ] `provider_price_versions` + 泛化价格分量（含缓存 token、非 token 单位）
- [ ] FX 适配器 + 审批 + 版本历史
- [ ] Pricing Rules：MARKUP 与 FIXED_RATE
- [ ] 持久化 `202` 摄取 + 异步处理
- [ ] 全局 event 幂等 + 冲突检测
- [ ] 估算供应商成本计算
- [ ] 钱包扣费
- [ ] 负余额处理
- [ ] 停机

**验收**：用量事件只计费一次 · 估算成本与 MYR 换算正确 · 客户计费额正确 · 钱包扣减 · 重复事件不双扣 · ID 冲突事件拒绝且不扣费 · 队列丢失后可从数据库状态恢复

---

## Phase 3 — Integrated Application Backend 试点（§126）

试点目标：`E:\projects\ai_chatbot_demo`。**扩展它现有的窄接口，不要推倒重来**（§3.3）。

- [ ] 供应商用量提取层
- [ ] Anthropic 用量
- [ ] OpenAI 转写用量
- [ ] `conversation_id` + 完整会话生命周期（关闭原因、可配置空闲超时）
- [ ] 本地事务性 Outbox
- [ ] Celery 投递
- [ ] Billing Client
- [ ] 集成认证 + HMAC 签名
- [ ] Webhook 接收端
- [ ] 本地版本化的有效服务状态
- [ ] 周期性状态对账
- [ ] 积压监控

**验收**：计费平台离线时 Claude 仍能回复 · 用量落本地 · 计费恢复后补投积压 · 无重复扣费 · 停机后不再发起 AI 调用 · 复机后恢复服务

> 试点成功之后才能推广到其他应用。

---

## Phase 4 — Payment & Customer Portal（§127）

- [ ] 客户认证
- [ ] 客户仪表盘
- [ ] 钱包页
- [ ] 充值 UI（金额不许硬编码）
- [ ] 支付网关适配器 + 选定的马来西亚网关（FPX 优先，依赖 D6）
- [ ] 支付 Webhook
- [ ] 主动的 stale payment 对账定时任务
- [ ] 支付金额不符的人工复核流
- [ ] 钱包充值入账
- [ ] 支付收据 + 下载

**验收**：客户付 RM100 → 网关确认 → 钱包**恰好**增加 RM100 一次 · 生成收据 · 重复 webhook 不双入账 · 丢失 webhook 由状态对账补回 · **SST / 会计闸门在上生产前已批准**

---

## Phase 5 — Usage Portal（§128）

- [ ] 会话摘要
- [ ] 请求级明细
- [ ] 用量筛选
- [ ] 客户端用量导出
- [ ] 管理端内部成本 / 毛利视图
- [ ] 供应商 / 模型分析

**验收**：客户能看到 Provider / Model / Tokens / 客户计费额；客户**看不到**估算或对账后的供应商成本、加价率、毛利（Invariant 7）

---

## Phase 6 — Notifications & Status Automation（§129）

- [ ] 低余额告警
- [ ] 停机通知
- [ ] 复机通知
- [ ] 门户内通知
- [ ] Email adapter（依赖 D7）
- [ ] WhatsApp adapter（依赖 D7）
- [ ] Webhook 重试
- [ ] 健康告警

---

## Phase 7 — Statements & Financial Analytics（§130）

- [ ] 月度对账单
- [ ] T+1 cut-off + 上期调整（依赖 D5）
- [ ] PDF 下载
- [ ] 收入 / 估算成本 / 对账后成本 / 网关手续费 / 毛利 / 毛利率
- [ ] 筛选
- [ ] CSV / Excel 导出

---

## Phase 8 — Provider Price Sync & Rebill（§131）

- [ ] 供应商价格同步适配器
- [ ] FX 汇率同步适配器
- [ ] 草稿 → 管理员审批 → 发布
- [ ] 历史版本化
- [ ] 供应商账单成本对账
- [ ] Reprocess
- [ ] Rebill 调整

---

## 上线前跨功能闸门（§132）

Phase 全做完还不能上，以下五条必须全过：

- [ ] SST / 会计处理已批准，并已体现在不可变财务快照里
- [ ] PDPA 与数据留存政策已批准
- [ ] RPO/RTO 恢复演练通过
- [ ] 支付、Email、WhatsApp 真实账号集成已验证
- [ ] 受保护 `main` 的 CI/CD 部署与恢复流程已验证

---

## 文档欠账

- [ ] `docs/adr/` 目录与上述 D1–D7 的 ADR
- [ ] `docs/database-schema.md`（Phase 1 起维护）
- [ ] `docs/api.md`（Phase 1 起维护）
- [ ] `docs/pricing-engine.md`、`docs/currency-and-fx.md`（Phase 2）
- [ ] `docs/integrated-application-backend.md`（Phase 3）
- [ ] `docs/payment-flow.md`（Phase 4）
- [ ] `docs/data-governance.md`、`docs/deployment.md`、`docs/runbook.md`（Phase 0 起补，runbook 需覆盖 §136 列的 18 个故障场景）
- [x] 逐条核对 25 条评审意见与 spec v1.1 —— 完成于 2026-09-10，结果见 [REVIEW_FOLLOWUP_v1.1.md](REVIEW_FOLLOWUP_v1.1.md)（20 条已解决 / 5 条残留，已转为上方 R1–R5）
- [ ] 给 spec 的硬性要求补 `REQ-*` 编号（= R5）

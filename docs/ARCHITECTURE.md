# ARCHITECTURE — 架构摘要

> 本文件是**摘要与索引**，不是事实来源。精确定义一律以
> [Acuven_Central_AI_Billing_Platform_Spec.md](Acuven_Central_AI_Billing_Platform_Spec.md) 为准，括号里的 §N 是 spec 节号。
> 最后更新：2026-09-10

---

## 1. 权威架构决策（§140）

```text
独立的 Integrated Application Backend
  + 本地事务性 Outbox
  + 持久化异步计费摄取
  + 中心 Acuven 计费平台
  + 租户共享钱包
  + 请求级计量
  + 不可变财务账本
  + 版本化定价
  + 版本化汇率
  + 预付 MYR 余额
```

优先级顺序（冲突时按此排序取舍）：

1. 客户 AI 可用性
2. 财务正确性
3. 幂等性
4. 可审计性
5. 数据隔离
6. 客户用量透明
7. 可维护性
8. 未来可扩展性

## 2. 系统上下文（§2）

```text
                ACUVEN CENTRAL AI BILLING PLATFORM
┌─────────────────────────────────────────────────────────────┐
│ 认证 / 租户管理    AI 用量计量      定价引擎                 │
│ 供应商成本管理     钱包 & 不可变账本  支付网关               │
│ 客户状态管理       通知服务          对账单 / 收据 / 报表    │
│ 审计日志                                                    │
└──────────────────────────▲──────────────────────────────────┘
                           │  异步计费 API（HMAC 签名 REST）
            ┌──────────────┼──────────────┐
            ▼              ▼              ▼
      Integrated     Integrated     Integrated
      App Backend    App Backend    App Backend
      (FastAPI +     (FastAPI +     (FastAPI +
       MySQL)         MySQL)         MySQL)
```

**信任边界（§2.1）**：Integrated Application Backend 指的是 **Acuven 自己开发运维的应用后端**，不是客户自建系统。但即便两端都是自己的机器，也视为**独立的信任域与故障域**：只通过认证 REST API + 签名 Webhook 通信，**永不共享数据库**。按项目发放凭据、HMAC 签名、最小权限隔离都是强制的。

## 3. 计费链路（§2）

```text
WhatsApp / 网站
      ↓
Integrated Application Backend  ──→ Claude / OpenAI / Gemini
      ↓                                    ↓
AI 响应立即返回给终端用户  ←────────────────┘
      ↓
用量事件写入本地 OUTBOX（与业务同一事务）
      ↓
后台 Celery Worker（可重试、可积压数小时）
      ↓
中心计费 API：鉴权 + 校验 + 落 usage_event(RECEIVED) + 返回 202
      ↓
中心 Celery Worker：定价解析 → 钱包扣费 → 写不可变账本
      ↓
余额触发阈值 → 状态机 → 签名 Webhook 回推各应用后端
```

关键点：**AI 响应绝不等待计费 API**（§1、§20）。摄取端 `202` 之后才做定价，钱包行锁不出现在 HTTP 请求路径上（§20、§82）。

## 4. 技术栈（§3）

| 层 | 选型 |
| --- | --- |
| 后端 | Python · FastAPI · SQLAlchemy · Alembic · Pydantic |
| 存储 | MySQL · Redis |
| 异步 | Celery（worker + beat）|
| 前端 | React · TypeScript · Vite · React Router · TanStack Query · Axios · Ant Design |
| 运行 | Docker · Docker Compose · Nginx |

- 所有金额计算用 `Decimal`，**禁止 `float`**。
- 前端必须 i18n-ready，V1 只出英文，不许把文案硬编码在组件里。

## 5. 关键架构决策

| # | 决策点 | V1 结论 | spec |
| --- | --- | --- | --- |
| 1 | 计费同步还是异步 | 异步。摄取只做鉴权+校验+落库+202，定价在 worker 里 | §20、§82 |
| 2 | 上报可靠性 | 应用侧本地事务性 outbox + 重试投递 | §21、§22 |
| 3 | 幂等边界 | 全局唯一 `event_id`，至多产生一次财务效果；ID 复用但内容不符 = 冲突，不是重复成功 | §23、Invariant 11 |
| 4 | 钱包模型 | 一租户一钱包，预付，余额只能通过不可变账本行变动 | §4、§6–§8 |
| 5 | 定价解析时点 | 供应商成本与客户定价规则**统一按 `occurred_at`** 解析 | §15–§17、§82 |
| 6 | 汇率 | 版本化 FX，`usage_events` 快照 FX 版本、源币种与金额 | §17.1、§74.2 |
| 7 | 成本与售价隔离 | 估算成本 / 对账后成本 / 客户计费额分别记录；客户永不可见成本与毛利 | §14、Invariant 7 |
| 8 | 历史修正 | 不改历史账本，用补偿交易（REBILL_CREDIT）| §19 |
| 9 | 会计期间 | T+1 cut-off，已定稿对账单不可变；晚到用量走下期的 Prior Period Adjustment | §46、Invariant 12 |
| 10 | 状态传播 | 状态单调传播 + Webhook 重试 + 周期性对账兜底轮询 | §24–§30 |
| 11 | 凭据存储 | 按项目 HMAC 密钥，**可逆加密**存储（HMAC 验签需要明文），版本化、可轮换、永不入日志 | §36–§37、§74.4 |
| 12 | 支付可靠性 | Webhook + 主动 stale payment 对账；金额不符**绝不自动入账** | §40–§45、§110 |
| 13 | 隐私 | 只存元数据，AI 对话内容不进本平台 | §13、Invariant 9 |
| 14 | 有状态性 | FastAPI 保持无状态，共享状态放 Redis/DB，为将来水平扩展留路 | §100 |

## 6. 14 条不变量（§133）

任何实现都必须保住这 14 条。**这里是这张清单的唯一出处**，别的文件只许指过来，不许再抄一份。

审查时**怎么查**每一条，见 [review_checklist.md](../scripts/review_checklist.md) A 节——编号与下面一一对应。

1. 中心计费故障不得中断客户 AI 服务
2. 同一用量事件永不重复扣费
3. 同一笔支付永不重复入账
4. 钱包余额只能通过账本交易变动
5. 历史账本条目不可变
6. 历史用量事件保留定价/版本引用（供应商价、客户定价规则、汇率、源币种金额、MYR 换算快照）
7. 客户不可见 Acuven 的成本与毛利
8. 租户之间不可互相访问数据
9. AI 对话内容不存进计费平台
10. 金额计算全用 `Decimal`
11. 全局唯一 `event_id` 至多一次财务效果；ID 复用不匹配 = 冲突
12. 定稿对账单不可变，晚到与跨期 rebill 走开放期间的显式调整
13. 钱包变动、状态跃迁、审计记录、持久化出站领域事件**原子提交**
14. Redis/Celery 丢失不得摧毁持久化的财务、支付、通知、Webhook、文档生成工作

## 7. 部署拓扑（§99）

```text
单 VPS + Docker Compose
services: nginx · frontend · api · celery-worker · celery-beat · redis · mysql
optional: document-worker
环境: 本地 Docker 测试环境 + 生产 VPS（V1 无独立 staging）
```

**落地状态（2026-09-12，T0.6 + T0.7）**：`docker-compose.yml` + `Dockerfile` + `frontend/` + `deploy/nginx/`
已经能实际跑起来，**七个服务到齐**。api / celery-worker / celery-beat 共用同一个镜像、以非 root UID `10001`
运行（[ADR-0004](adr/ADR-0004-credential-encryption.md) 要求 Phase 0 定的就是这个数字）；
`frontend` 是另一个镜像（node 构建 → nginx 发静态文件），只经边缘 nginx 对外。
**生产侧尚未落地**：TLS、真实域名、镜像 tag、GitHub Actions 部署、备份与恢复都归 T0.9。

交付要求：**公开** GitHub 仓库（见第 7.1 节与 [ADR-0001](adr/ADR-0001-repository-visibility.md)）、受保护 `main`、CI 通过才能合并、合并触发 GitHub Actions 部署、镜像用不可变 tag 标识提交、密钥走 GitHub environment secrets 绝不入库、迁移按文档化的安全顺序执行、部署等健康检查 + 冒烟测试、有经过验证的回滚/前滚流程、并发部署串行化。

⚠️ 因为没有 staging，支付 / Email / WhatsApp / 数据库迁移 / 破坏性恢复类改动，必须先在沙箱或本地验证，并走明确的生产变更清单。

## 7.1 仓库可见性：公开（已批准）

| | |
| --- | --- |
| **spec 要求** | §99 自 v1.2 起为「public GitHub repository」，与实际一致 |
| **当前实际** | `github.com/kelvinpang90/ai_billing_hub` 为**公开** |
| **发生时间** | 2026-09-10 |
| **原因** | GitHub Free 的**私有**仓库不支持分支保护（branch protection 与 rulesets 两个 API 均返回 403，提示需 Pro）。为拿到 spec §99 同样要求的「受保护 `main` + CI 通过才能合并」，仓库改为公开 |

**这是用一条 spec 要求换另一条 spec 要求，不是一个干净的决定。** 代价是：spec 全文、数据库表结构、HMAC 与密钥管理方案、部署拓扑、`SPEC_REVIEW_v1.0.md`（一份系统弱点清单）与 `REVIEW_FOLLOWUP_v1.1.md`（5 条至今未解决的残留）全部永久公开。

**状态：已收口（2026-09-10）。** 决策人选择接受公开，决策与完整代价记录在
[ADR-0001](adr/ADR-0001-repository-visibility.md)，spec 已修订至 v1.2 使 §99 与实际一致。

派生的硬约束（ADR-0001「后果」一节）：**绝不可提交**凭据、密钥、`.env`、真实主机名 / IP、
客户数据、供应商合同价。CI 的 `secret-scan` job 是兜底，不是许可。

## 8. 待补的 ADR（§136）

目录与写法约定见 [adr/README.md](adr/README.md)。以下决策**必须在对应实现阶段之前**落成 ADR：

- [ ] 中心摄取语义（同步 vs 异步的最终定稿与边界）
- [x] 财务期间与 cut-off 规则 → [ADR-0003](adr/ADR-0003-financial-period-and-cutoff.md)
- [x] 凭据加密方案（密钥管理、轮换、恢复）→ [ADR-0004](adr/ADR-0004-credential-encryption.md)
- [x] 生产数据库隔离 → [ADR-0002](adr/ADR-0002-production-datastore-isolation.md)
- [x] FX 数据源 → [ADR-0005](adr/ADR-0005-fx-rate-source.md)
- [ ] 支付网关（准入契约已定，供应商未选）→ [ADR-0006](adr/ADR-0006-payment-gateway-contract.md) 定了准入契约与适配器边界；**供应商未选定，硬截止在 Phase 4 开工前**

spec §136 还要求维护 `database-schema.md` / `api.md` / `integrated-application-backend.md` / `payment-flow.md` / `pricing-engine.md` / `currency-and-fx.md` / `data-governance.md` / `deployment.md` / [`runbook.md`](runbook.md)。这些在对应 Phase 落地时再建，现在不预创建空文件。

[`runbook.md`](runbook.md) 已开始（2026-09-12，T0.5）：**按场景逐个补，不预留空标题** —— 空标题会让人以为「已经有预案了」。目前只有「Redis / Celery broker 不可用」一条。

`runbook.md` 需覆盖的故障场景清单见 spec §136（18 项，从「计费 API 挂了」到「销户时存在未结财务事件」）。

## 9. 与 spec §101 的偏差

spec §101 建议的目录结构是 `backend/app/` + `frontend/` + `integration-client/` + 小写文档名。本仓库改用：

```text
ai_billing_hub/
├── docs/          # 大写文档名，见 PROJECT.md 文档地图
├── app/           # 后端主体（Phase 0 创建）
├── alembic/       # 迁移（T0.4 创建；⚠️ 不在 wheel 里，镜像要单独 COPY）
├── deploy/        # 部署期配置，目前只有 nginx/（T0.6 创建）
├── frontend/      # React SPA，自带 Dockerfile 与 nginx.conf（T0.7 创建）
└── tests/         # 后端测试（Phase 0 创建）
```

`frontend/src/` 内部**照 §101 的分法**（`api` / `components` / `features` / `layouts` / `routes` / `i18n`），
只有 `features/` 下的九个业务子目录不预建 —— 空目录会让人以为那块已经开工。
integration-client 的目录在对应 Phase 再定。**§101 的顶层结构视为已被本节取代**，实现时不要再回去照抄。

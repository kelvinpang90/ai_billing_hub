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

这些是 [SPEC_REVIEW_v1.0.md](archive/SPEC_REVIEW_v1.0.md) 的「建议的处理顺序」，即使 v1.1 已修订，落地前仍需确认结论并落成 ADR。

- [x] **D1 — 汇率来源与版本化策略** —— 已收口。BNM openAPI 每日拉取落 `DRAFT`，需显式发布为 `PUBLISHED` 才可用于计算；按用量事件的 `occurred_at` 取版本（不是结算日）；`provider_price_versions` 存供应商原币种原价，MYR 换算结果连同 `fx_rate_version_id` 快照进用量事件。计费热路径不实时调用 BNM。见 [ADR-0005](adr/ADR-0005-fx-rate-source.md)。⚠️ BNM 是中间价且周末无新价，偏差须由 markup 的 FX 缓冲吸收
- [ ] **D2 — SST 税务口径** —— **部分收口**。已定：**对客户展示的金额一律含税**（充值 RM100 → 钱包 +RM100，税嵌在消费价里；spec 第 359 行禁止税静默减少钱包额度）。字段语义因此确定，Phase 0–3 不再被阻塞。见 [ADR-0008](adr/ADR-0008-sst-tax-treatment.md)。⚠️ **spec §45.1 六项里其余五项仍需会计意见**：是否需注册 SST、AI 服务的税务分类与豁免、充值属储值 / 押金 / 服务预付、税的确认时点与税率、receipt / statement 强制字段。**五项齐备前 Phase 4 不得开工**，`tax_policy_versions` 只能有 `DRAFT` 行
- [x] **D3 — 生产数据库隔离** —— 已收口。结论：专用 MySQL/Redis 实例，不接 `vps_infra` 的 `infra_mysql` / `infra_redis`。理由与代价见 [ADR-0002](adr/ADR-0002-production-datastore-isolation.md)
- [ ] **D4 — 凭据加密方案** —— 主体已定，**尚未完全收口**。应用层信封加密（AES-256-GCM）；主密钥走 Docker Compose `secrets:` 文件注入，**不用环境变量**；备份与数据库备份分离、两套访问控制。见 [ADR-0004](adr/ADR-0004-credential-encryption.md)。⚠️ **遗留一项未决**：出站 webhook 密钥的 schema（新建 `project_webhook_secrets` 表 vs `projects` 加暂存列）——`projects` 现在只有一组密钥槽，轮换重叠期无处安放。**Phase 1 前必须二选一，且要走设计闸门**（ADR-0004 第 4a 节）
- [x] **D5 — 财务期间与 cut-off** —— 已收口。结论：用量期按 `occurred_at`（Asia/KL），T+1 宽限，新月第 2 日定稿；晚到走 `PRIOR_PERIOD_ADJUSTMENT`。见 [ADR-0003](adr/ADR-0003-financial-period-and-cutoff.md)。⚠️ T+1（24h）覆盖不了 §31 举例的 31 小时积压，所以上期调整**是预期会出现的**；但 §31 那个数字是**告警阈值示例，不是日常状态**——每一笔都应能追溯到一次具体延迟事件，**不是会计常态**。笔数与金额占比必须进 §95 监控，占比走高要查根因
- [ ] **D6 — 支付网关选型** —— 准入契约已定，**供应商尚未选定**。已定四条硬性准入（H1 回调带稳定唯一标识 / H2 服务端权威状态查询 / H3 回调可验签 / H4 沙箱 + 对账）与适配器接口，Phase 1–3 不再被阻塞。见 [ADR-0006](adr/ADR-0006-payment-gateway-contract.md)。⚠️ **具体供应商仍未选定，硬截止在 Phase 4 开工前**；H1–H4 必须用沙箱实测，不能凭供应商文档
- [ ] **D7 — 通知通道** —— **部分收口**。已定：Email 借 `rs-roof-pms` 的 SMTP 传输层（`aiosmtplib`、465/587 TLS 分支、空 host = 未配置、发送不抛异常），但**投递模型不借**——改走 domain Outbox + worker + 周期恢复（Invariant 13/14；`rs-roof-pms` 是请求内同步发送、零重试、无发送记录）；一切通知走 spec §47 强制的 Notification Adapter 抽象。见 [ADR-0009](adr/ADR-0009-notification-channels.md)。⚠️ **WhatsApp 传输路径待定**：`whatsapp_gateway` 现已停机、出站端点从未被调用过、且缺模板消息能力与 24 小时窗口判定，接不了「我方发起」的业务通知；需在「复活并扩建网关」与「计费平台独立 App / 号码」之间拍板。**Email 与 Portal 不受此阻塞**

---

## P-2. 评审残留项（R1–R5）

来自 [REVIEW_FOLLOWUP_v1.1.md](archive/REVIEW_FOLLOWUP_v1.1.md)：25 条评审意见中 20 条已在 spec v1.1 解决，以下 5 条有残留。

- [ ] **R1 — 停机/复机阈值**：阈值仍硬编码 `balance <= 0`（§49），无可配置 `suspend_at` / `resume_at` 与滞后带。§7.10 的「RM0 保持挂起 + 最小恢复额」已消除死锁，抖动也被 §7.11/§48 的跃迁触发挡住。**决定采纳双阈值，还是明确记为已知取舍。**（Phase 2 前）
- [x] **R2 — 并发 PENDING payment** —— 已收口。允许并存；两笔都付则都入账（钱已收到，拒绝入账等于吞客户的钱，且预付钱包多充仍是客户余额）；60 秒内同额重复请求复用同一笔挡误触；PENDING 60 分钟置 `EXPIRED`，但**过期后收到成功回调仍然入账**。见 [ADR-0007](adr/ADR-0007-concurrent-pending-payments.md)。⚠️ 人工退款流程超出 V1 范围，是明确缺口
- [ ] **R3 — 账本膨胀权衡**：§82 已承认钱包变更按租户串行化并要求测最热租户，§119 给了量化目标，但**没有对「按对话/时间窗聚合成一笔 AI_USAGE ledger、usage_events 保留明细」做权衡分析**。即使决定不做，也要写明理由。（Phase 2 前）
- [x] **R4 — 重放保护存储与过期策略** —— 已收口。支付 Webhook 的 nonce 落数据库，其余四个签名端点走 Redis；**过期时刻 = 请求 timestamp + 5 分钟**（跟着请求自己算，不是「记录时刻 + 固定 TTL」——时间窗是 ±5 分钟，固定 TTL 会留下约 4 分钟重放窗口）。Redis 丢失最长让纵深防御第二层失效约 10 分钟，不产生财务缺口——财务安全网是 `event_id` 与 `(gateway, gateway_event_id)` 的领域幂等。见 [ADR-0004](adr/ADR-0004-credential-encryption.md) 第 5 节
- [ ] **R5 — 需求编号覆盖度**：`REQ-*` 只有 13 条，未覆盖每条硬性要求；140 节仍无完整 TOC。影响 §132 逐条验收。（Phase 1 前）
- [x] **R7 — 对账单 cut-off** —— 已复核（2026-09-10）：**仍选 T+1**。那条「T+3 也挡不住 31 小时积压」的论据是错的（T+3 = 72 小时），已撤回；但剩下两条理由（上期调整机制无论如何都必须存在、客户体验）足以支撑 T+1。代价：上期调整会经常出现，已派生监控要求——**但它是投递链路的延迟信号，不是会计常态**（见 ADR-0003「要读准这个数字的性质」）。见 [ADR-0003](adr/ADR-0003-financial-period-and-cutoff.md)
- [x] **R6 — 仓库可见性偏离** —— 已收口（2026-09-10）。决策人选 B：接受公开，写成 [ADR-0001](adr/ADR-0001-repository-visibility.md)，spec 修订至 v1.2 使 §99 与实际一致。派生硬约束：绝不可提交凭据、密钥、`.env`、真实主机名 / IP、客户数据、供应商合同价。（来源：Codex 审查 PR #2）

---

## Phase 0 — Foundation（§123）

§123 的 13 项是一个大块，**一个 PR 装不下**（[HANDOFF](HANDOFF.md) 已要求拆分）。
下面按「能独立提 PR、能独立验收」拆成 T0.1–T0.10，编号即分支名里的 `<TODO 编号>`
（例：`task/phase0-1-backend-skeleton`）。依赖列写明必须先合并谁，**不要开 stacked PR**。

| 编号 | 任务 | 依赖 |
| --- | --- | --- |
| T0.1 | 后端骨架与配置 | — |
| T0.2 | CI 后端 job（lint + pytest） | T0.1 |
| T0.3 | 日志与统一错误处理 | T0.1 |
| T0.4 | MySQL + SQLAlchemy + Alembic 接通 | T0.1 |
| T0.5 | Redis + Celery 接通 | T0.4 |
| T0.6 | Docker Compose 七服务栈 | T0.5 |
| T0.7 | React 骨架 | — |
| T0.8 | 认证基座（管理员登录 + 2FA） | T0.3、T0.4 |
| T0.9 | 生产拓扑 + 备份 / 恢复 / 密钥方案（RPO/RTO） | — |
| T0.10 | 初始性能 / SLO 基线 | T0.6 |

- [x] **T0.1 — 后端骨架与配置**：`app/` 七层目录（`api` / `core` / `models` / `schemas` / `repositories` / `services` / `tasks`）、`app/main.py` 应用工厂、`/healthz`、`app/core/config.py`、`pyproject.toml`（依赖 + ruff + pytest）、`.env.example`、`tests/backend/` 与首个冒烟测试
- [ ] **T0.2 — CI 后端 job**：`.github/workflows/ci.yml` 加 `backend` job（`ruff check` + `ruff format --check` + `pytest`）；[WORKFLOW §7](WORKFLOW.md) 的命令清单补上 lint 与 pytest；在分支保护里把 `backend` 设为必需状态检查
- [ ] **T0.3 — 日志与统一错误处理**（§94、§107）：结构化日志、request id、统一错误响应体、领域异常层次、日志脱敏（密钥与 AI 内容绝不入日志）
- [ ] **T0.4 — MySQL + SQLAlchemy + Alembic 接通**：engine / session 生命周期、`Decimal` 列约定（Invariant 10）、Alembic 初始化与首个迁移、带依赖的就绪检查
- [ ] **T0.5 — Redis + Celery 接通**：Celery app、worker 与 beat 配置、一个可验证的探活任务
- [ ] **T0.6 — Docker Compose**：nginx · frontend · api · celery-worker · celery-beat · redis · mysql；含 API / Celery 容器的运行 UID 决定，宿主机主密钥文件**属主设为该 UID**、权限 `0400`（Compose 的 `file:` secret 走 bind mount，`uid`/`gid`/`mode` 只在 swarm 生效；**不得为读密钥把容器改回 root**）
- [ ] **T0.7 — React 骨架**：Vite + TS + React Router + TanStack Query + Axios + Ant Design + i18n 骨架（V1 只出英文，文案不许硬编码在组件里）
- [ ] **T0.8 — 认证基座**：管理员登录、密码哈希、会话 / 令牌、2FA
- [ ] **T0.9 — 生产拓扑与恢复方案**：专用生产 MySQL/Redis 拓扑（依赖 D3 / [ADR-0002](adr/ADR-0002-production-datastore-isolation.md)）、备份与恢复、加密密钥方案（依赖 D4 / [ADR-0004](adr/ADR-0004-credential-encryption.md)）、RPO/RTO 设计待批准
- [ ] **T0.10 — 初始性能 / SLO 基线**
- [x] FX 供应商评估（D1 已定：BNM openAPI，见 [ADR-0005](adr/ADR-0005-fx-rate-source.md)）
- [x] ~~GitHub 仓库 + 受保护 `main`~~ 已建、已推送；`main` 保护规则已配（禁 force push / 禁删除 / 强制 PR / 线性历史 / 管理员同样受限），CI 四项 `docs` / `scripts` / `policy` / `secret-scan` 已全部设为必需状态检查

**验收**：所有服务能起 · DB 迁移能跑 · 管理员能登录 · 2FA 可用 · CI 拦住合并并能部署不可变镜像 · RPO/RTO 恢复方案已批准

### T0.1 任务记录（2026-09-11）

**做了什么**

- `app/` 建成 spec §101 的分层（扁平化口径见 [ARCHITECTURE](ARCHITECTURE.md) 第 9 节）：七个包各带一行说明该层放什么，**不预建将来才用的空子目录**（`api/auth`、`services/wallet` 等在对应 Phase 再建）
- `app/main.py` 用 `create_app(settings)` 工厂，不是模块级单例 —— 测试要能拿到互不干扰的实例，且 API 保持无状态（§100）
- `/healthz` **刻意不依赖任何外部组件**：MySQL / Redis 挂掉时它仍须应答，否则编排器会在数据库恢复期间反复重启 API 容器。带依赖的就绪检查留给 T0.4 / T0.5
- `app/core/config.py`：`pydantic-settings`，前缀 `BILLING_`。配置只有这一个入口；默认值里不许出现真实主机名 / 凭据
- `pyproject.toml`：运行依赖只装 FastAPI 这条链，**SQLAlchemy / Alembic / Celery / redis 等到接通它们的任务里再加**
- `tests/backend/` 走 pytest（`pythonpath = ["."]`，不需要安装包），与 `tests/test_*.py` 那套流程脚本 unittest 回归**物理分开**、互不收集

**偏离了什么**

- **ruff 暂不纳入 `scripts/` 与 `tests/test_*.py`**：它们早于 ruff，全量纳入会让本任务变成一次 52 处报错的大范围格式化，属于范围扩张。已记为下方待清理项
- ruff 的 `extend-exclude` 里排除了 `*.md`：ruff 会格式化 Markdown 里的 Python 代码块，而 spec 里那些代码块是**规格原文**，被工具改写等于静默改需求（实测会改 spec 第 1438 行）
- dev 依赖用 `httpx2` 而不是 `httpx`：starlette 的 `TestClient` 已弃用旧 httpx，装 `httpx` 会在每次跑测试时刷弃用警告
- 本任务**没有**做 Alembic（原「仓库骨架」条目里带的）—— 它要和数据库 engine 一起落地才验得了，已移入 T0.4

**验证到什么程度**

- `python -m pytest` → 2 passed（应用能起、`/healthz` 返回 200、注入的配置生效）
- `python -m ruff check .` / `python -m ruff format --check .` → 通过
- [WORKFLOW §7](WORKFLOW.md) 三项本地检查通过；`python -m unittest discover -s tests` 仍是 61 passed，**新增的 `tests/backend/` 没有被它收集**（该目录无 `__init__.py`，不是可导入包）
- **未验证**：容器内启动、生产配置分支 —— 那是 T0.6 的事

**待清理**

- [ ] 把 `scripts/` 与 `tests/test_*.py` 纳入 ruff（约 52 处报错 + 5 个文件需重新格式化），单独开 `chore/` PR

---

## Phase 1 — Tenant, Project & Wallet Core（§124）

- [ ] Tenant
- [ ] Project
- [ ] 管理端客户管理
- [ ] Wallet
- [ ] 不可变钱包账本
- [ ] 管理员手工调账
- [ ] API 凭据（加密存储、版本化、可轮换）
- [ ] **出站 webhook 密钥 schema 二选一**（`project_webhook_secrets` 新表 / `projects` 加暂存列），走设计闸门后再实现——见 [ADR-0004](adr/ADR-0004-credential-encryption.md) 第 4a 节
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

## 流程加固 —— 审查流水线（2026-09-11）

背景：#5 六轮 + #6 两轮共 17 条阻断，**0 条自查发现**；重复的错误类型见 [REVIEW-LOG.md](REVIEW-LOG.md)「反复出现的问题」与「机械化对照」。写下来的规则对开发者无效（写下的当轮就违反过），所以本节只做两类：机器执行的检查，和让错误做不出来的格式约束。

- [x] `scripts/check_repo_policy.py`：复选框白名单（只允许 `[ ]` / `[x]`）、PR 模板小节齐全与 `设计闸门：` 行、`TODO 影响` 声明与 diff 一致、回应证据 `完整 SHA · 文件:行` 在所指提交上真实存在且在 head 历史内。**验收**：`tests/test_check_repo_policy.py` 32 用例，全部取自真实缺陷（`[~]`、假引用、过期 SHA、越界行号、代码块 / 链接 / HTML 注释不误判）；变异测试——禁用复选框逻辑后 5 个用例失败
- [x] `scripts/gh_verified_write.py`：GitHub 写操作以参数数组调 `gh api`，写完按对象 id 回读比对；不一致退出 2 且**不自动重发**。**验收**：`tests/test_gh_verified_write.py` 10 用例，全部 mock，不打真实 PR
- [x] `scripts/codex-review.ps1` 接入：准入（CI 全绿 + 正文策略检查）不过则不调 Codex；复审材料自动带上轮审查、本轮回应、本轮相对上轮的增量（= 两版「PR 相对 base 的 diff」之间的差异，同步 base 不计入）；第三轮起要求 `### 完整影响面`；发布改走回读验证
- [x] CI 新增 `policy` job（策略检查 + Python 回归）；`pull_request` 触发加 `edited`，PR 正文改了会重跑
- [x] 模板与文档同步：PR 模板 `TODO 影响` 节、WORKFLOW §4 / §6 / §7、审查清单准入与 F 节、CLAUDE.md / AGENTS.md 入口。**验收**：`check_docs.py` 通过（约定串规则含署名前缀）
- [x] 合并后把 `policy` 加进 `main` 的必需检查（分支保护是仓库设置，PR 里改不了）。2026-09-11 已加：必需检查现为 `docs` / `scripts` / `policy` / `secret-scan`，`strict: true`；其余设置（enforce_admins、线性历史、禁 force push、禁删除、对话必须解决）逐项回读确认未变动
- [ ] 已开的 #6、#7 按新模板补 `TODO 影响` 节；#6 上旧格式的 `CLAUDE RESPONSE` 要在复审前重发一条新格式的，否则复审取材会被策略检查拒绝
- [ ] 准入查询的瞬态空结果：`gh pr checks --watch` 刚返回时立刻跑 `codex-review.ps1`，`gh pr checks --json` 曾报「no checks reported」（复制延迟），几秒后正常。现在是 fail-closed（退出 2），但紧跟 CI 跑审查是最常见的用法。给这一种报错加有界重试（只读查询，≤3 次），**不是本轮顺手修的范围**，另开 PR
- [ ] `.ps1` 文件 UTF-8 BOM 检查（Codex 建议时看的是无 `.ps1` 的分支才暂缓；现在 main 上有三个）
- [x] 收口 #9 留下的输出格式冲突：重写后的 `AGENTS.md` §18 自带一套「推荐格式」（`### P1 — 问题标题` + 位置 / 问题 / 触发条件 / 后果 / 建议），与 `codex-review.ps1` 提示词强制的 `### 阻断项` / `### 建议项` / `### 清单核对` 是两套。脚本只解析首行前缀与末行判定，退出码不受影响，但 Codex 每次会同时收到两套格式指令。按「一个格式约定只准有一份实现」收口：格式模板只留在提示词里（它每次随材料送达），§18 改为指向它，并把原 P1 格式里那五项作为**内容底线**保留（位置 / 问题 / 触发条件 / 后果 / 建议）。**验收**：`check_docs.py`（含约定串规则）、`check_repo_policy.py`、Python 61 用例、PS 96 用例全过
- [x] 刷新入口文档（2026-09-11）。整理全仓库 25 份文档时发现**入口文档本身在骗人**：`HANDOFF.md` 停在 09-10，写着「未完成：ADR」（实际 0001–0009 全写完）、7 条未决问题里 4 条已收口，最要命的是「已知冲突 #1」还写着 `CLAUDE.md` 与 `AGENTS.md`「是同一内容的副本，改一个记得同步另一个」——#9 已经把两者拆成独立文件，照着做会同步错方向。`PROJECT.md` 的文档地图只覆盖 `docs/` 内部，漏了 `CLAUDE.md` / `AGENTS.md` / `review_checklist.md` 与两个 GitHub 模板。仓库还没有 `README.md`，GitHub 首页是空的。三项一并修：HANDOFF 重写到当前真实状态、文档地图补全为七组 25 份、新增根 `README.md`
- [x] 14 条不变量去重（2026-09-11）。同一张清单原本存在三份：spec §133（英文，权威）、`ARCHITECTURE.md` 第 6 节、`review_checklist.md` A 节。后两份是中文全文，其中 7 条逐字相同，另 7 条在清单侧多一句「怎么查」。本仓库最安全攸关的清单有三份副本，而 REVIEW-LOG 记着的教训正是「一个约定只准有一份实现，分叉时两边都不报错」。改为：**不变量正文只留在 `ARCHITECTURE.md` 第 6 节**（并声明为唯一出处），`review_checklist.md` A 节只留「怎么查」，按编号一一对应，并给原本没有查法的 7 条（1/2/3/6/12/13/14）补上。注意：**清单因此没有变短**（净 +8 行），省掉的是一份副本不是篇幅
- [x] 散文重复收敛到 `WORKFLOW.md`（2026-09-11）。**先更正计数**：原记的「7 份 / 6 份」数的是*提及*，真正复述规则的是——三项检查命令 4 份（WORKFLOW §7 / `CLAUDE.md` / `README.md` / PR 模板自检行）、版本绑定规则 4 份（WORKFLOW §3 / `CLAUDE.md` / 审查清单 / design-gate 模板）、「前端 / 文档 / CI / 脚本」不走闸门清单 4 份。处理：① 命令清单唯一出处 WORKFLOW §7（README 独有的 PS 测试那句搬进去），其余三处改链接；② 版本绑定：人读的留 WORKFLOW §3，**Codex 读的留 design-gate 模板**（审查材料里只有 Issue 正文，Codex 读不到 WORKFLOW），两处互相声明「改一起改」，审查清单改为指向 Issue 正文；③ 不走闸门清单四处各服务不同读者、删不掉，改为 `check_docs` 约定串机械钉住（变异测试：去掉斜杠两侧空格的变体被当场拒绝，本条记录初稿就中招了）。`CLAUDE.md` 第 11–12 行的闸门要点保留——它开头已声明「唯一事实来源是 WORKFLOW，下面只是要点」，一句话 + 链接正是目标形态
- [ ] `ADR-0004`（355 行，是其余 ADR 的 3–4 倍）实为四个决策捆在一起：凭据加密 + 密钥轮换流程 + 备份恢复 + 未决的出站 webhook 密钥 schema。最后一项本来就要走设计闸门、本来就要写新 ADR，可借机拆分。**未做**
- [x] spec 勘误 v1.2 → v1.3（2026-09-11）。为文档瘦身而通读 spec 时发现**两处自相矛盾**，都不是措辞问题：① **§6 写 `DECIMAL(18,6)` 且措辞为「Required V1 mechanism」，与 §80 的 `DECIMAL(20,8)` + 单次 `ROUND_HALF_UP` 到 8 位直接冲突**——先读到 §6 的实现者会按 6 位建表，每笔计费精度差两位，撞 Invariant 10；全仓库只有这一处写 18,6（ADR-0008 / REQUIREMENTS / review_checklist 早已全用 20,8），故 §6 改为引用 §80。② **§123 Phase 0 仍写 `private GitHub repository`**，而 §99 与 ADR-0001 已改 public，且 v1.2 修订说明自称「No other normative change」——**v1.2 漏改了这处，是「改了 A 忘了改 B」第 7 次**。按「文件名跟随 Revision History」约定升 v1.3 并改名，同步 8 个文件 11 处引用
- [x] spec 勘误 v1.4 → v1.5（2026-09-11）。核实瘦身通读时记下的 3 处疑似矛盾：**两处属实，一处不是矛盾但是坑**。① **§28 应用 webhook 只认 `status_version >` 本地，§30 对账却写 `>=`，并断言「二者用同一比较规则」——断言为假**；仓库其余出处（`review_checklist.md` 状态单调条、`REVIEW_FOLLOWUP` §24 条）全站 `>`，`REQ-STATUS-001` 的乱序测试按两节会得出不同预期，故 §30 改为引用 §28 的规则。② **§55 仪表盘字段「Actual AI Provider Cost」全 spec 仅此一处**，其余 20+ 处与 §14 定义一律是 Estimated/Reconciled 且「必须标注口径」；改用已定义术语并要求显示口径。③ §30 的「every 5 minutes」是示例、§119 的 `<= 5 minutes` 是可调的验收目标，**两边都不是硬性的，不构成矛盾**（早先「硬上界」的说法不准确）；但间隔 = 5 分钟时最坏陈旧度必超界，且 §30 是应用后端开发者唯一会读的一节却不提 §119，故把裸数字示例换成交叉引用。三处都在 §24–§30 状态传播，沿 #14 先例判为勘误不走闸门，PR 正文请审查者判。顺带：修订历史里 1.4 行原排在 1.3 上面，排正；`README.md` 链接文字仍写「spec v1.2」，改对
- [x] spec 文件名去版本号（2026-09-11，Kelvin 拍板）。`git mv` → `docs/Acuven_Central_AI_Billing_Platform_Spec.md`，版本只记在文档内 Revision History；同步 9 处引用（含 `check_docs.py` 与 `codex-review.ps1` 的硬编码路径），`HANDOFF.md` 的「文件名跟随版本」约定改写。起因：v1.3 → v1.6 四次改名，每次都是「改几行正文 + 同步 9 处引用」，纯开销。**不动 spec 内容、不升版本**——改名不是修订。`SPEC_REVIEW_v1.0.md` / `REVIEW_FOLLOWUP_v1.1.md` 的文件名照旧不动（它们评审的确实是那两个版本）
- [x] 历史评审记录归档（2026-09-11，Kelvin 拍板）。`SPEC_REVIEW_v1.0.md` 与 `REVIEW_FOLLOWUP_v1.1.md` 移入 `docs/archive/`，加 README 说明「针对当时版本、内容冻结」；`check_docs.py` 对该目录跳过 `§N` 校验（链接与约定串照查）。起因：它们引用的是 v1.0 / v1.1 的章节，却被拿来对现行 spec 校验，结果 §56 / §58 / §59 这种只被历史记录引用的章节退役不了——除非改历史记录来绕检查。修了 7 个文件的入站链接与归档文件自身的 4 处出站相对链接。**变异测试**：同一个引用不存在章节号的文件放 `docs/archive/` 通过、放 `docs/` 被拒。副作用：§56 / §58 / §59 的退役障碍解除，但那三节是否值得退役另议（各约 17 行）
- [x] spec 瘦身（通读已完成，估计可减约 800 行 / 16.4%，剩约 4070 行）。**已完成第一批**（2026-09-11，v1.4）：退役第 102 节（目录树，文档自称非规范）与第 138 节（编造数字的界面草图，字段已在 §67–§69 规范化），共 76 行。**⚠️ 更正一处早先的错判**：原以为第 85、120、137 节也零引用，实测**都不是**——第 85 节被 `REQUIREMENTS.md` 的 `REQ-AVAIL-001` 追溯行与 ADR-0003 引用；第 120 节被 `REQUIREMENTS.md` 的范围标题与 `REVIEW_FOLLOWUP` 的落实证据引用；第 137 节被 `HANDOFF.md` 与本文件当作闸门依据引用。这三节要删必须先改引用方，**不是零风险操作**。**已完成第二批**（2026-09-11，v1.6，−150 行）：§2 的通用流程图改为引用 §20/§103；§55 筛选器清单改为引用 §87；§134 的 15 步块改为引用 §126（内容一一对应）；§135 只保留别处没有的约束（Rule 3/5/6/7/9），其余指向 §20/§121/§123/§136，标题改为 Implementation Constraints，**没有并入 §133、不变量未动**；§139 的模拟仪表盘与下钻层级改为引用 §86/§61/§87，只保留「财务链路必须端到端可审计」一句，§139.1 表原样保留；退役第 72 节（两条都被 §46 与 §90 覆盖）。**更正早先两个说法**：① 「流程图画了 4 遍」不准确——§22 带投递状态与退避、§103 带停用检查与 conversation_id，各有独有步骤，只有 §2 那份是纯复述，所以此项只减 19 行不是 ~100 行；② 「§135 须先并入 §133」——压缩即可，不必动不变量。**有意不做**：§56/§58/§59 的导航清单被历史评审记录 `SPEC_REVIEW_v1.0.md` 引用，而 `check_docs` 对历史记录同样校验 `§N`，整节退役就得改历史记录来绕检查，不干；§71 是 ADR-0007 的影响落点，保留；§60–§70 的字段清单是规范性 UI 要求不是导航，保留；§101 目录树有 10 处引用，保留。**剩余可做**：§22 成功路径前四步与 §20 重复（约 8 行）、§57 字段清单与 §76 表重复（待核）。两批合计 −223 行（4865 → 4640），距通读估算的 ~800 行差得远，原估算把「重复」和「各有独有内容」混在一起了。**收尾判断（2026-09-11）**：§22 前四步与 §20 重复只值 8 行，却要加一行修订记录，净收益≈0，**不做**；§57 与 §76 不是重复而是**不一致**——§57 的 UI 字段有 `project_id` 与 `description`，§76 的表却是 `id` + `public_id`、没有 `description`——这是 Phase 1 定 `projects` 表结构时要裁决的事，已记到下面的 Phase 1 前置项。瘦身到此**停止**，两批合计 −223 行
- [x] `AGENTS.md` §10 / §11 的 `erp_os` 模板残留（2026-09-11）。删掉 §10 阻断清单里的 `Inventory 错误` / `e-Invoice 严重错误` 与 §11 高风险模块里的 `Inventory` / `Order` / `e-Invoice`，共 5 行；只删不加。**顺带发现的缺口，未动**：§11 高风险模块没有列本项目真正会算错钱的地方——定价 / 汇率、Outbox 与 Webhook 投递、幂等键——要不要补由 Kelvin 定，补的话属于改 Codex 的注意力分配，应单独一个 PR
- [x] 第二轮 Codex 审查的三条阻断（审查发现，非自查）：① 复审增量用随机临时文件名，`git diff --no-index` 把路径写进 `--stat` 与 diff 头，审查前后两次取材逐字不等 → **只要增量非空，所有修复复审都发不出判定**，改为固定文件名 + 以临时目录为工作目录用相对名调 git；② `Test-ImpactSection` 的 `\s*\S` 跨空行，空的「完整影响面」后接另一个章节也算写了 → 改成在下一个标题之前找内容行；③ 复选框白名单按「后面跟链接就豁免」，于是 `[~]` 后面接一个 Markdown 链接就能绕过 → 判据改为「方括号里不止一个字符才是链接标签」。**验收**：三条各做变异测试，分别有用例失败；Python 61 用例、PS 96 用例

**仍靠语义审查、机器抓不住的**：ADR 与 TODO 的*内容*是否一致（检查只证明声明了影响）；「已修」是否真修好（SHA · 行号只证明引用存在）；范围扩张是否必要。这三样写进了审查清单 F 节，由 Codex 复审时判断。

---

## 文档欠账

- [ ] D1–D7 的 ADR（`docs/adr/` 目录已建，见 [adr/README.md](adr/README.md)；D1、D3、D4、D5 已完成，D6 部分完成，**剩 D2、D7**）
- [ ] `docs/database-schema.md`（Phase 1 起维护）
- [ ] `projects` 表结构裁决（Phase 1 建表前）：spec §57 的 UI 字段有 `project_id` 与 `description`，§76 的表却是 `id` + `public_id`、没有 `description`。两节不一致，建表前定下来并写进 `docs/database-schema.md`；改 spec 的话走一次勘误
- [ ] `docs/api.md`（Phase 1 起维护）
- [ ] `docs/pricing-engine.md`、`docs/currency-and-fx.md`（Phase 2）
- [ ] `docs/integrated-application-backend.md`（Phase 3）
- [ ] `docs/payment-flow.md`（Phase 4）
- [ ] `docs/data-governance.md`、`docs/deployment.md`、`docs/runbook.md`（Phase 0 起补，runbook 需覆盖 §136 列的 18 个故障场景）
- [x] 逐条核对 25 条评审意见与 spec v1.1 —— 完成于 2026-09-10，结果见 [REVIEW_FOLLOWUP_v1.1.md](archive/REVIEW_FOLLOWUP_v1.1.md)（20 条已解决 / 5 条残留，已转为上方 R1–R5）
- [ ] 给 spec 的硬性要求补 `REQ-*` 编号（= R5）

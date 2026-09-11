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

- [x] **D1 — 汇率来源与版本化策略** —— 已收口。BNM openAPI 每日拉取落 `DRAFT`，需显式发布为 `PUBLISHED` 才可用于计算；按用量事件的 `occurred_at` 取版本（不是结算日）；`provider_price_versions` 存供应商原币种原价，MYR 换算结果连同 `fx_rate_version_id` 快照进用量事件。计费热路径不实时调用 BNM。见 [ADR-0005](adr/ADR-0005-fx-rate-source.md)。⚠️ BNM 是中间价且周末无新价，偏差须由 markup 的 FX 缓冲吸收
- [ ] **D2 — SST 税务口径** —— **部分收口**。已定：**对客户展示的金额一律含税**（充值 RM100 → 钱包 +RM100，税嵌在消费价里；spec 第 359 行禁止税静默减少钱包额度）。字段语义因此确定，Phase 0–3 不再被阻塞。见 [ADR-0008](adr/ADR-0008-sst-tax-treatment.md)。⚠️ **spec §45.1 六项里其余五项仍需会计意见**：是否需注册 SST、AI 服务的税务分类与豁免、充值属储值 / 押金 / 服务预付、税的确认时点与税率、receipt / statement 强制字段。**五项齐备前 Phase 4 不得开工**，`tax_policy_versions` 只能有 `DRAFT` 行
- [x] **D3 — 生产数据库隔离** —— 已收口。结论：专用 MySQL/Redis 实例，不接 `vps_infra` 的 `infra_mysql` / `infra_redis`。理由与代价见 [ADR-0002](adr/ADR-0002-production-datastore-isolation.md)
- [ ] **D4 — 凭据加密方案** —— 主体已定，**尚未完全收口**。应用层信封加密（AES-256-GCM）；主密钥走 Docker Compose `secrets:` 文件注入，**不用环境变量**；备份与数据库备份分离、两套访问控制。见 [ADR-0004](adr/ADR-0004-credential-encryption.md)。⚠️ **遗留一项未决**：出站 webhook 密钥的 schema（新建 `project_webhook_secrets` 表 vs `projects` 加暂存列）——`projects` 现在只有一组密钥槽，轮换重叠期无处安放。**Phase 1 前必须二选一，且要走设计闸门**（ADR-0004 第 4a 节）
- [x] **D5 — 财务期间与 cut-off** —— 已收口。结论：用量期按 `occurred_at`（Asia/KL），T+1 宽限，新月第 2 日定稿；晚到走 `PRIOR_PERIOD_ADJUSTMENT`。见 [ADR-0003](adr/ADR-0003-financial-period-and-cutoff.md)。⚠️ T+1（24h）覆盖不了 §31 举例的 31 小时积压，所以上期调整**是预期会出现的**；但 §31 那个数字是**告警阈值示例，不是日常状态**——每一笔都应能追溯到一次具体延迟事件，**不是会计常态**。笔数与金额占比必须进 §95 监控，占比走高要查根因
- [ ] **D6 — 支付网关选型** —— 准入契约已定，**供应商尚未选定**。已定四条硬性准入（H1 回调带稳定唯一标识 / H2 服务端权威状态查询 / H3 回调可验签 / H4 沙箱 + 对账）与适配器接口，Phase 1–3 不再被阻塞。见 [ADR-0006](adr/ADR-0006-payment-gateway-contract.md)。⚠️ **具体供应商仍未选定，硬截止在 Phase 4 开工前**；H1–H4 必须用沙箱实测，不能凭供应商文档
- [ ] **D7 — 通知通道** —— **部分收口**。已定：Email 借 `rs-roof-pms` 的 SMTP 传输层（`aiosmtplib`、465/587 TLS 分支、空 host = 未配置、发送不抛异常），但**投递模型不借**——改走 domain Outbox + worker + 周期恢复（Invariant 13/14；`rs-roof-pms` 是请求内同步发送、零重试、无发送记录）；一切通知走 spec §47 强制的 Notification Adapter 抽象。见 [ADR-0009](adr/ADR-0009-notification-channels.md)。⚠️ **WhatsApp 传输路径待定**：`whatsapp_gateway` 现已停机、出站端点从未被调用过、且缺模板消息能力与 24 小时窗口判定，接不了「我方发起」的业务通知；需在「复活并扩建网关」与「计费平台独立 App / 号码」之间拍板。**Email 与 Portal 不受此阻塞**

---

## P-2. 评审残留项（R1–R5）

来自 [REVIEW_FOLLOWUP_v1.1.md](REVIEW_FOLLOWUP_v1.1.md)：25 条评审意见中 20 条已在 spec v1.1 解决，以下 5 条有残留。

- [ ] **R1 — 停机/复机阈值**：阈值仍硬编码 `balance <= 0`（§49），无可配置 `suspend_at` / `resume_at` 与滞后带。§7.10 的「RM0 保持挂起 + 最小恢复额」已消除死锁，抖动也被 §7.11/§48 的跃迁触发挡住。**决定采纳双阈值，还是明确记为已知取舍。**（Phase 2 前）
- [x] **R2 — 并发 PENDING payment** —— 已收口。允许并存；两笔都付则都入账（钱已收到，拒绝入账等于吞客户的钱，且预付钱包多充仍是客户余额）；60 秒内同额重复请求复用同一笔挡误触；PENDING 60 分钟置 `EXPIRED`，但**过期后收到成功回调仍然入账**。见 [ADR-0007](adr/ADR-0007-concurrent-pending-payments.md)。⚠️ 人工退款流程超出 V1 范围，是明确缺口
- [ ] **R3 — 账本膨胀权衡**：§82 已承认钱包变更按租户串行化并要求测最热租户，§119 给了量化目标，但**没有对「按对话/时间窗聚合成一笔 AI_USAGE ledger、usage_events 保留明细」做权衡分析**。即使决定不做，也要写明理由。（Phase 2 前）
- [x] **R4 — 重放保护存储与过期策略** —— 已收口。支付 Webhook 的 nonce 落数据库，其余四个签名端点走 Redis；**过期时刻 = 请求 timestamp + 5 分钟**（跟着请求自己算，不是「记录时刻 + 固定 TTL」——时间窗是 ±5 分钟，固定 TTL 会留下约 4 分钟重放窗口）。Redis 丢失最长让纵深防御第二层失效约 10 分钟，不产生财务缺口——财务安全网是 `event_id` 与 `(gateway, gateway_event_id)` 的领域幂等。见 [ADR-0004](adr/ADR-0004-credential-encryption.md) 第 5 节
- [ ] **R5 — 需求编号覆盖度**：`REQ-*` 只有 13 条，未覆盖每条硬性要求；140 节仍无完整 TOC。影响 §132 逐条验收。（Phase 1 前）
- [x] **R7 — 对账单 cut-off** —— 已复核（2026-09-10）：**仍选 T+1**。那条「T+3 也挡不住 31 小时积压」的论据是错的（T+3 = 72 小时），已撤回；但剩下两条理由（上期调整机制无论如何都必须存在、客户体验）足以支撑 T+1。代价：上期调整会经常出现，已派生监控要求——**但它是投递链路的延迟信号，不是会计常态**（见 ADR-0003「要读准这个数字的性质」）。见 [ADR-0003](adr/ADR-0003-financial-period-and-cutoff.md)
- [x] **R6 — 仓库可见性偏离** —— 已收口（2026-09-10）。决策人选 B：接受公开，写成 [ADR-0001](adr/ADR-0001-repository-visibility.md)，spec 修订至 v1.2 使 §99 与实际一致。派生硬约束：绝不可提交凭据、密钥、`.env`、真实主机名 / IP、客户数据、供应商合同价。（来源：Codex 审查 PR #2）

---

## Phase 0 — Foundation（§123）

- [ ] 仓库骨架：`app/`、`tests/`、Alembic、`.env.example`、~~`README.md`~~（README 已于 2026-09-11 单独补上，其余未做）
- [ ] Docker Compose：nginx · frontend · api · celery-worker · celery-beat · redis · mysql
- [ ] FastAPI 结构（`app/api` / `core` / `models` / `schemas` / `repositories` / `services` / `tasks`）
- [ ] React 结构（Vite + TS + Ant Design + i18n 骨架）
- [ ] MySQL + Redis + Celery 接通
- [ ] 认证基座
- [ ] 日志与统一错误处理（§94、§107）
- [ ] CI 测试结构 —— 文档层已有（`.github/workflows/ci.yml` 的 `docs` + `secret-scan` 两个 job）；待补：**后端 lint + pytest job**（等 `app/` 与 `tests/` 建好）
- [ ] ~~GitHub 仓库 + 受保护 `main`~~ ✅ 已建、已推送、`main` 保护规则已配（禁 force push / 禁删除 / 强制 PR / 线性历史 / 管理员同样受限）；CI workflow 已建，待做：**在分支保护里把 `docs` / `secret-scan` 设为必需状态检查**
- [ ] 专用生产 MySQL/Redis 拓扑（依赖 D3）
- [ ] 备份、恢复、加密密钥方案设计（依赖 D4）
- [ ] 确定 API / Celery 容器的运行 UID，宿主机主密钥文件**属主设为该 UID**、权限 `0400`（Compose 的 `file:` secret 走 bind mount，`uid`/`gid`/`mode` 只在 swarm 生效；**不得为读密钥把容器改回 root**）
- [ ] 初始性能 / SLO 基线
- [x] FX 供应商评估（D1 已定：BNM openAPI，见 [ADR-0005](adr/ADR-0005-fx-rate-source.md)）

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
- [ ] `AGENTS.md` §10 / §11 列的 `Inventory` / `Order` / `e-Invoice` 不是本项目的模块（疑似从 `erp_os` 模板带入），会把 Codex 的注意力引到不存在的地方。属噪音不属错误，另开 PR 清
- [x] 第二轮 Codex 审查的三条阻断（审查发现，非自查）：① 复审增量用随机临时文件名，`git diff --no-index` 把路径写进 `--stat` 与 diff 头，审查前后两次取材逐字不等 → **只要增量非空，所有修复复审都发不出判定**，改为固定文件名 + 以临时目录为工作目录用相对名调 git；② `Test-ImpactSection` 的 `\s*\S` 跨空行，空的「完整影响面」后接另一个章节也算写了 → 改成在下一个标题之前找内容行；③ 复选框白名单按「后面跟链接就豁免」，于是 `[~]` 后面接一个 Markdown 链接就能绕过 → 判据改为「方括号里不止一个字符才是链接标签」。**验收**：三条各做变异测试，分别有用例失败；Python 61 用例、PS 96 用例

**仍靠语义审查、机器抓不住的**：ADR 与 TODO 的*内容*是否一致（检查只证明声明了影响）；「已修」是否真修好（SHA · 行号只证明引用存在）；范围扩张是否必要。这三样写进了审查清单 F 节，由 Codex 复审时判断。

---

## 文档欠账

- [ ] D1–D7 的 ADR（`docs/adr/` 目录已建，见 [adr/README.md](adr/README.md)；D1、D3、D4、D5 已完成，D6 部分完成，**剩 D2、D7**）
- [ ] `docs/database-schema.md`（Phase 1 起维护）
- [ ] `docs/api.md`（Phase 1 起维护）
- [ ] `docs/pricing-engine.md`、`docs/currency-and-fx.md`（Phase 2）
- [ ] `docs/integrated-application-backend.md`（Phase 3）
- [ ] `docs/payment-flow.md`（Phase 4）
- [ ] `docs/data-governance.md`、`docs/deployment.md`、`docs/runbook.md`（Phase 0 起补，runbook 需覆盖 §136 列的 18 个故障场景）
- [x] 逐条核对 25 条评审意见与 spec v1.1 —— 完成于 2026-09-10，结果见 [REVIEW_FOLLOWUP_v1.1.md](REVIEW_FOLLOWUP_v1.1.md)（20 条已解决 / 5 条残留，已转为上方 R1–R5）
- [ ] 给 spec 的硬性要求补 `REQ-*` 编号（= R5）

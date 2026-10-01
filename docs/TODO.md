# TODO — 开发任务清单

> 任务骨架来自 spec §123–§131（Phase 0–8）。验收标准直接抄自 spec，**不要自行放宽**。
> 最后更新：2026-09-30

---

## 本文件的约定

1. 一个 session 做一个编号任务：读本文件 → 做完 → 验收 → commit，中途不需要逐步审批。
2. **对计划本身有异议时才停下来讨论**，不要在每个任务上重新走一遍「先计划、等确认」。
3. 做完把 `[ ]` 改成 `[x]`。**任务记录写在 PR 描述里**（做了什么写进「改了什么」，偏离了什么、验证到什么程度写进「如何验证」，留给后续的写进「已知未做 / 留给后续」）；**本文件只改勾选项和规划块**，不再新增记录段。设计文件或 `.platform/tasks.yaml` 里写「记进 `docs/TODO.md`」的内容，同样写进 PR 描述。2026-09-30 之前的记录在 [archive/TODO_RECORDS_2026-09-30.md](archive/TODO_RECORDS_2026-09-30.md)
4. 本文件里**没有**的工作（临时需求、探索型任务）才需要先写计划、等确认。
5. 每个 Phase 的每个功能都必须过 spec §132 的 15 条 Definition of Done，否则不算完成。

> ℹ️ `CLAUDE.md` / `AGENTS.md` 的「任务管理」一节已指向本文件（2026-09-10 修正，此前指向不存在的 `tasks/todo.md`）。

> 📦 历史记录（已完成任务的记录段、Phase 0 的任务记录、已结的决策细节、流程接入过程）2026-09-30 已原样移到 [archive/TODO_RECORDS_2026-09-30.md](archive/TODO_RECORDS_2026-09-30.md)。

---

## 规划（OpenClaw planning-v1）

> 下面两个标记之间的块由 OpenClaw 控制面按固定格式解析，决定下一项任务；只管「做什么、先做什么」，执行权限仍以 `.platform/tasks.yaml` 为准。格式不对整块作废，项目 fail closed。

<!-- openclaw:planning-v1:begin -->
### 当前计划
1. `AIH-TASK-044` Phase 2 FX 汇率 2b/6：规则层的并发与性质测试
2. `AIH-TASK-040` Phase 2 FX 汇率 3/6：BNM 拉取与 Celery Beat
3. `AIH-TASK-041` Phase 2 FX 汇率 4/6：管理端汇率接口
4. `AIH-TASK-042` Phase 2 FX 汇率 5/6：内部告警接口与两个维度
5. `AIH-TASK-043` Phase 2 FX 汇率 6/6：nginx、monitor.sh、配置快照与运维文档
6. `AIH-TASK-029` Phase 2 用量摄取端点
7. `AIH-TASK-030` Phase 2 批量用量摄取端点
8. `AIH-TASK-031` Phase 2 计价引擎与试算预览
9. `AIH-TASK-032` Phase 2 异步计费 worker
10. `AIH-TASK-033` Phase 2 最热租户压测脚本
11. `AIH-TASK-034` Phase 2 管理端用量事件查询接口
12. `AIH-TASK-035` Phase 2 管理端前端：AI 目录
13. `AIH-TASK-036` Phase 2 管理端前端：供应商价格与汇率
14. `AIH-TASK-037` Phase 2 管理端前端：定价规则与试算
15. `AIH-TASK-038` Phase 2 管理端前端：用量事件与重新入队

### 已阻塞
- 待登记：第一个真实客户进生产前处置验收管理员账号（2026-09-28 Kelvin 选 (a)：浏览器验收沿用夹具 ADMIN 账号，期限是第一个真实客户进生产之前，届时必须降权或撤销；仍是待办）｜阻塞：等第一个真实客户进生产前执行
- 待登记：D6 支付网关供应商选定与沙箱实测 H1–H4（Phase 4 支付开工前）｜阻塞：等 Kelvin 选定供应商
- 待登记：D2 SST 其余五项（Phase 4 上生产前）｜阻塞：等会计意见
- 待登记：D7 WhatsApp 通知的传输路径｜阻塞：等 Kelvin 拍板
- 待登记：Phase 4 前重新评估 Email 传输（事务邮件服务、退信与投诉抑制）｜阻塞：等 Kelvin 拍板

### 后续计划
- 待登记：供应商价格版本的数据库兜底：起点为空只许在该（供应商, 模型）没有区间非空的已发布版本时出现、改为 `RETIRED` 必须同时写 `effective_to`（026 记录的后续，新迁移，需设计闸门）
- 待登记：管理端前端：出站 webhook 签名密钥的签发、启用与退役
- 待登记：低余额阈值配置（需设计闸门）
- 待登记：管理端查看钱包流水（需设计闸门）
- 待登记：余额不一致的定时核对与告警（需设计闸门）
- 待登记：管理端前端：账户状态、低余额阈值与钱包流水
- 待登记：关户（§112.1：先停用、余额为 0、无未结用量事件与支付，依赖 Phase 2 与 Phase 4）（需设计闸门）
- 待登记：审计时间戳取整在登录与业务两条路径上统一（需设计闸门，涉及认证路径）
- 待登记：数据库账号权限拆分，迁移账号与运行账号分开（运维，管理员执行）
- 待登记：scripts/ 与 tests/test_*.py 纳入 ruff（chore）
- 待登记：Phase 3 出站服务状态 webhook 投递与重试（需设计闸门，依赖 webhook 密钥表）
- 待登记：Phase 3 服务状态查询接口，供集成方周期对账（需设计闸门）
- 待登记：Phase 3 Billing Client 库 integration-client（需设计闸门）
- 待登记：Phase 3 ai_chatbot_demo 试点接入（在 ai_chatbot_demo 仓库做，OpenClaw 尚未登记该项目）
- 待登记：Phase 4 客户认证与客户门户登录（需设计闸门）
- 待登记：Phase 4 客户仪表盘与钱包页
- 待登记：Phase 4 支付网关适配器与支付 Webhook（需设计闸门，依赖 D6）
- 待登记：Phase 4 stale payment 对账与金额不符人工复核（需设计闸门）
- 待登记：Phase 4 充值 UI 与充值入账（需设计闸门）
- 待登记：Phase 4 支付收据与下载
- 待登记：Phase 5 会话摘要、请求级明细与用量筛选
- 待登记：Phase 5 客户端用量导出
- 待登记：Phase 5 管理端内部成本、毛利与供应商模型分析
- 待登记：Phase 6 Notification Adapter 抽象与 Email adapter
- 待登记：Phase 6 低余额、停机、复机通知与门户内通知（需设计闸门）
- 待登记：Phase 6 WhatsApp adapter（依赖 D7）
- 待登记：Phase 6 健康告警
- 待登记：Phase 7 月度对账单、T+1 cut-off 与上期调整（需设计闸门）
- 待登记：Phase 7 对账单 PDF 下载
- 待登记：Phase 7 收入、成本、手续费、毛利分析与 CSV / Excel 导出
- 待登记：Phase 8 供应商价格同步适配器、草稿审批发布（需设计闸门）
- 待登记：Phase 8 供应商账单成本对账（需设计闸门）
- 待登记：Phase 8 Reprocess 与 Rebill 调整（需设计闸门）
- 待登记：上线闸门：PDPA 与数据留存政策
- 待登记：上线闸门：RPO/RTO 恢复演练
- 待登记：上线闸门：支付、Email、WhatsApp 真实账号集成验证
<!-- openclaw:planning-v1:end -->

---

## P-1. 前置决策（不写代码，但决定表结构 —— 必须先做）

这些是 [SPEC_REVIEW_v1.0.md](archive/SPEC_REVIEW_v1.0.md) 的「建议的处理顺序」，即使 v1.1 已修订，落地前仍需确认结论并落成 ADR。

- [x] **D1 — 汇率来源与版本化策略** —— 已收口。BNM openAPI 每日拉取落 `DRAFT`，需显式发布为 `PUBLISHED` 才可用于计算；按用量事件的 `occurred_at` 取版本（不是结算日）；`provider_price_versions` 存供应商原币种原价，MYR 换算结果连同 `fx_rate_version_id` 快照进用量事件。计费热路径不实时调用 BNM。见 [ADR-0005](adr/ADR-0005-fx-rate-source.md)。⚠️ BNM 是中间价且周末无新价，偏差须由 markup 的 FX 缓冲吸收
- [ ] **D2 — SST 税务口径** —— **部分收口**。已定：**对客户展示的金额一律含税**（充值 RM100 → 钱包 +RM100，税嵌在消费价里；spec 第 359 行禁止税静默减少钱包额度）。字段语义因此确定，Phase 0–3 不再被阻塞。见 [ADR-0008](adr/ADR-0008-sst-tax-treatment.md)。⚠️ **spec §45.1 六项里其余五项仍需会计意见**：是否需注册 SST、AI 服务的税务分类与豁免、充值属储值 / 押金 / 服务预付、税的确认时点与税率、receipt / statement 强制字段。**五项齐备前 Phase 4 不得开工**，`tax_policy_versions` 只能有 `DRAFT` 行
- [x] **D3 — 生产数据库隔离** —— 已收口。结论：专用 MySQL/Redis 实例，不接 `vps_infra` 的 `infra_mysql` / `infra_redis`。理由与代价见 [ADR-0002](adr/ADR-0002-production-datastore-isolation.md)
- [ ] **D4 — 凭据加密方案** —— 主体已定，**尚未完全收口**。应用层信封加密（AES-256-GCM）；主密钥走 Docker Compose `secrets:` 文件注入，**不用环境变量**；备份与数据库备份分离、两套访问控制。见 [ADR-0004](adr/ADR-0004-credential-encryption.md)。⚠️ **遗留一项未决**：出站 webhook 密钥的 schema（新建 `project_webhook_secrets` 表 vs `projects` 加暂存列）——`projects` 现在只有一组密钥槽，轮换重叠期无处安放。**Phase 1 前必须二选一，且要走设计闸门**（ADR-0004 第 4a 节） → **2026-09-25 已选定方案 i**（Kelvin），ADR-0004 第 4a 节已补完；它自己的设计闸门还没开，D4 因此仍不勾 → 2026-09-28：设计闸门 #135 已批准 v1，AIH-TASK-019 实现了表、管理端与签名库，ADR-0004「收口条件」的第 4a 节那一项随合并满足（勾选要改 ADR-0004，不在 019 的可改路径里，留给下一次改 ADR 的任务）。**D4 仍不勾**：ADR-0004「收口条件」还有五项未满足 —— ① Phase 0 的「备份、恢复、加密密钥方案设计」验收（T0.9）；② `docs/runbook.md` 写入主密钥恢复流程（脱敏）与私有附录；③ 季度恢复演练含主密钥恢复；④ §113 的 secret-leak tests 覆盖日志、审计、API 响应、异常栈四条路径（入站凭据与出站签名密钥各自的用例已覆盖这四条，但收口条件指的是整体，要有人对照 ADR 判定）；⑤ D6 选型时确认支付网关回调的幂等标识
- [x] **D5 — 财务期间与 cut-off** —— 已收口。结论：用量期按 `occurred_at`（Asia/KL），T+1 宽限，新月第 2 日定稿；晚到走 `PRIOR_PERIOD_ADJUSTMENT`。见 [ADR-0003](adr/ADR-0003-financial-period-and-cutoff.md)。⚠️ T+1（24h）覆盖不了 §31 举例的 31 小时积压，所以上期调整**是预期会出现的**；但 §31 那个数字是**告警阈值示例，不是日常状态**——每一笔都应能追溯到一次具体延迟事件，**不是会计常态**。笔数与金额占比必须进 §95 监控，占比走高要查根因
- [ ] **D6 — 支付网关选型** —— 准入契约已定，**供应商尚未选定**。已定四条硬性准入（H1 回调带稳定唯一标识 / H2 服务端权威状态查询 / H3 回调可验签 / H4 沙箱 + 对账）与适配器接口，Phase 1–3 不再被阻塞。见 [ADR-0006](adr/ADR-0006-payment-gateway-contract.md)。⚠️ **具体供应商仍未选定，硬截止在 Phase 4 开工前**；H1–H4 必须用沙箱实测，不能凭供应商文档
- [ ] **D7 — 通知通道** —— **部分收口**。已定：Email 借 `rs-roof-pms` 的 SMTP 传输层（`aiosmtplib`、465/587 TLS 分支、空 host = 未配置、发送不抛异常），但**投递模型不借**——改走 domain Outbox + worker + 周期恢复（Invariant 13/14；`rs-roof-pms` 是请求内同步发送、零重试、无发送记录）；一切通知走 spec §47 强制的 Notification Adapter 抽象。见 [ADR-0009](adr/ADR-0009-notification-channels.md)。⚠️ **WhatsApp 传输路径待定**：`whatsapp_gateway` 现已停机、出站端点从未被调用过、且缺模板消息能力与 24 小时窗口判定，接不了「我方发起」的业务通知；需在「复活并扩建网关」与「计费平台独立 App / 号码」之间拍板。**Email 与 Portal 不受此阻塞**

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
- [x] **T0.2 — CI 后端 job**：`.github/workflows/ci.yml` 加 `backend` job（`ruff check` + `ruff format --check` + `pytest`）；[WORKFLOW §7](WORKFLOW.md) 的命令清单补上 lint 与 pytest；在分支保护里把 `backend` 设为必需状态检查。**另加打包冒烟**：装进干净 venv 后 `import app.main` 并起一次 `/healthz` —— `pytest` 跑的是源码树（`pythonpath = ["."]`），发现不了 wheel 少打子包这类问题（PR #22 审查实证）
- [x] **T0.3 — 日志与统一错误处理**（§94、§107）：结构化日志、request id、统一错误响应体、领域异常层次、日志脱敏（密钥与 AI 内容绝不入日志）
- [x] **T0.4 — MySQL + SQLAlchemy + Alembic 接通**：engine / session 生命周期、`Decimal` 列约定（Invariant 10）、Alembic 初始化与首个迁移、带依赖的就绪检查
- [x] **T0.5 — Redis + Celery 接通**：Celery app、worker 与 beat 配置、一个可验证的探活任务
- [x] **T0.6 — Docker Compose**：nginx · frontend · api · celery-worker · celery-beat · redis · mysql；含 API / Celery 容器的运行 UID 决定，宿主机主密钥文件**属主设为该 UID**、权限 `0400`（Compose 的 `file:` secret 走 bind mount，`uid`/`gid`/`mode` 只在 swarm 生效；**不得为读密钥把容器改回 root**）
- [x] **T0.7 — React 骨架**：Vite + TS + React Router + TanStack Query + Axios + Ant Design + i18n 骨架（V1 只出英文，文案不许硬编码在组件里）。**另含 T0.6 欠下的第七个服务**：compose 加 `frontend`、把 `deploy/nginx/billing.conf` 的 `location /` 从 503 占位改成指向它、同步 `tests/backend/test_compose.py` 的 `EXPECTED_SERVICES`（三处漏一处测试就红）
- [x] **T0.8 — 认证基座**：管理员登录、密码哈希、会话 / 令牌、2FA。**设计闸门 [#32](https://github.com/kelvinpang90/ai_billing_hub/issues/32) 已批准 `design v5`**，按下面四个 PR 落地
  - [x] **T0.8a** — `users` / `refresh_tokens` / `audit_logs` 三张表、Argon2id 密码哈希与强度、登录、JWT + 刷新轮换与重放检测、登出与吊销、失败锁定、按来源限流、bootstrap CLI
  - [x] **T0.8b** — 信封加密模块（[ADR-0004](adr/ADR-0004-credential-encryption.md)）、`two_factor_settings` / `recovery_codes`、TOTP 注册 / 确认 / 校验、恢复码、**ADMIN 强制 2FA**
  - [x] **T0.8c** — 前端登录页、路由守卫、令牌持有与刷新
  - [x] **T0.8d** — `password_reset_tokens` + `domain_outbox`、忘记密码 / 重置密码、[ADR-0009](adr/ADR-0009-notification-channels.md) 的 Email 传输与投递任务（补齐 §53 的最后两项）
  - [x] **T0.8f** — 前端「忘记密码 / 重置密码」两个页面（T0.8d 派生）。落地前那条链接会落到守卫的 `*` 兜底上、被当成未登录重定向去登录页 —— 用户点开重置链接看到的是登录表单，而他来这儿正是因为登不进去。另加一条跨端守卫，机械比对「后端拼出来的链接」与「前端登记的路由」
  - [x] **T0.8e** — 注册路径的 pending 2FA 令牌 TTL 改为 600 秒（T0.8c 整栈实测发现 120 秒走不完首次注册；设计闸门 [#37](https://github.com/kelvinpang90/ai_billing_hub/issues/37) `APPROVED: design v2`）
- [ ] **T0.9 — 生产拓扑与恢复方案**（方案见 [deployment.md](deployment.md)，**七件待拍板的事已于 2026-09-13 定案**；⚠️ 还包含部署流水线 CD，见下）：专用生产 MySQL/Redis 拓扑（依赖 D3 / [ADR-0002](adr/ADR-0002-production-datastore-isolation.md)）、备份与恢复、加密密钥方案（依赖 D4 / [ADR-0004](adr/ADR-0004-credential-encryption.md)）、RPO/RTO 设计待批准；**另含 §94 的生产日志要求**（轮转、保留期、磁盘上限、安全删除、异地留存，以及「日志撑爆本地磁盘必须在威胁到 MySQL / 文档存储之前告警」）—— T0.3 只做了应用侧的日志**内容与格式**，这些是部署侧的事
- [x] **T0.10 — 初始性能 / SLO 基线**：可重复跑的测量工具 [`scripts/perf_baseline.py`](../scripts/perf_baseline.py) + 基线本体 [perf-baseline.md](perf-baseline.md)（spec §119 要求的那份 documented Phase 0 capacity baseline）。连接池三参数变成配置项，`pool_timeout` 30s → 5s；前端路由级懒加载

> ⚠️ **T0.9 必须处理的三件边缘代理遗留**（T0.6 派生）：① `deploy/nginx/billing.conf` 对 `/readyz` 的网段限制比的是 `$remote_addr`，生产上若在 nginx 前面再放一层代理，这条限制**形同虚设**，届时要改用 `real_ip_header` + `set_real_ip_from` 或在前一层拦掉；② nginx 仍以官方镜像默认方式运行（master 是 root）；③ TLS / 证书 / 真实域名尚未配置，栈现在只监听 80。
> ⚠️ ~~**生产上没有任何安全响应头**~~ —— **已补**（首次部署后外网实测发现，2026-09-14）：边缘 nginx 在 server 层加 `X-Frame-Options: DENY`、`Content-Security-Policy: frame-ancestors 'none'`、`X-Content-Type-Options: nosniff`、`Referrer-Policy: strict-origin-when-cross-origin`、`Strict-Transport-Security: max-age=31536000`，全部 `always`。本地栈实测页面 / API / 404 / 503 / 限流 429 都带上。守卫钉住「location 里不许写 add_header」（nginx 的继承规则会让它静默丢掉全部安全头）。⚠️ **仍欠完整 CSP**：现在只有 `frame-ancestors`，`script-src` / `style-src` 等要逐条核对前端实际加载的资源（antd 用内联样式）再定，没有归属。
> ⚠️ **主密钥没有重包裹任务**（T0.8b 派生）：`app/core/crypto.py` 已经支持多版本钥匙串（轮换时老行仍能解开），但把老行重新用新密钥包裹的后台任务还没有。没有它，轮换之后老密钥必须**永久保留**，否则历史 TOTP 注册全部作废。
> ✅ **生产巡检落地，§95 被点名的两条告警关闭**（2026-09-16）：新增 `deploy/monitor.sh`，cron 每 5 分钟跑一次，查三样东西 —— 七个容器的 `State` / `Health`（含 celery-beat 那个按调度状态文件 mtime 判活的探针）、宿主机直读 `/readyz` **响应体**（Redis 降级时状态码仍是 200，只能读体）、`df -P` 的磁盘水位（≥ 80% P2、≥ 90% P1，§94 点名要求的那条）。**三个维度各自一个 Healthchecks 检查**，不合成一个：外部服务只在状态翻转时通知，合成之后「磁盘先红、MySQL 再挂」不会有第二条通知。全绿也 ping（dead man's switch）；发现问题先隔 45 秒复核一次，否则**每次正常部署都会误报**（换版本时容器有半分钟不是 healthy）。本地用假 docker + 假心跳服务演练九个场景，守卫用例 9 条、四个变异（删掉 celery-beat、磁盘阈值调到 99%、关掉复核、把两个维度合成一个检查）全部被杀。见 [deployment.md](deployment.md) §8。⚠️ **合并后要做**：在 Healthchecks.io 建三个检查（Simple / 5 分钟 / 宽限 15 分钟）、地址写进 VPS 的 `.env`、部署、重装 cron、手工跑一次验证三个变绿。⚠️ **§95 的 17 项业务指标仍未做**（要指标管道，Phase 1 起随功能补），日志聚合与异地留存同样未做。
> ✅ **部署留下的旧镜像现在会被回收**（2026-09-16，磁盘巡检上线后顺着查出来的）：`deploy.sh` 成功分支里那句 `docker image prune -f` **只清悬空（无标签）镜像**，而每次部署拉进来的是带 commit SHA 标签的 —— 它从来没拦住过真正在长的那一类。生产实测：api + frontend 攒了 **8 个版本**、只有 1 个在跑，镜像总量 11.02 GB，根分区到 **68%**（§94 的告警线是 80%）。新增 `prune_old_images`：按 `BILLING_IMAGE_KEEP`（默认 3）保留最近几个版本，另外三道防线是当前标签、回滚目标、**任何容器（含别的项目）正在引用的镜像**。⚠️ 演练模式（`BILLING_IMAGE_REPO` 未设）与保留数 < 2 / 非数字时一律不删 —— 「清多了」在这里不可逆。守卫用例拿假 docker **真跑一遍这个函数**（本文件其余部署用例都是静态断言，这一组刻意不是：排序排反、整词匹配写错，静态断言一个都看不出来，而后果是删掉在跑的镜像）；写的时候就真踩了一个 —— `docker ps` 的多行输出没 `tr` 成一行，`case " $in_use "` 那道防线形同虚设。⚠️ **合并后要做**：部署一次让新脚本上 VPS；**存量的 7 个旧版本它不会追溯清理**，第一次跑的时候只会把当时窗口外的删掉。⚠️ **别的项目的遗留镜像不在这个脚本的职责里**（`ai_chatbot-backend:latest` 304 MB、`hello-world`、`alpine` 等），另有一个 **209 MB 的悬空卷是 `ai_chatbot` 的 MySQL 数据目录**，看着像垃圾其实不是 —— 这台机器上**绝不能跑 `docker system prune --volumes`**。
> ✅ **可信代理三处一起收窄**（2026-09-16，§10 末尾那条「还差一个事实」到此有答案）：在生产 VPS 上查实 —— `infra_nginx` 与本平台 nginx 在**同一张 `proxy_net` 上直连容器**（都在 `172.19.0.0/16`），不经宿主机发布端口。于是：nginx 的 `set_real_ip_from` 从三段 RFC1918 + 回环收成 **只有 proxy_net**；`/readyz` 的 allow 名单收成 回环 + 本栈网段 + proxy_net；应用的 `BILLING_TRUSTED_PROXIES` 收成 **只有本栈网段**。⚠️ 为了让「本栈网段」是个**稳定**的值，`docker-compose.yml` 把默认网络钉死成 `10.201.0.0/24`（`BILLING_STACK_SUBNET` 可覆盖）—— 不钉的话 docker 每次随手分一个 172.x，这正是原来只能拿三段RFC1918 兜着的原因；选 10.201 是因为它在 docker 默认池（172.17–172.31）之外，且生产上 10.x 一个都没用。守卫 5 条（含「compose 与 nginx 里的本栈网段必须是同一个串」），三个变异（放宽 `set_real_ip_from`、放宽 `BILLING_TRUSTED_PROXIES`、把网段改成没钉死的写法）全部被杀。⚠️ **合并后第一次部署会重建本栈那张网**，容器跟着重建一次（compose v2 自己会做，本地用一个一次性项目实测过；数据在命名卷里不受影响）——**这次部署会有约一分钟不可用**。⚠️ **仍然保留一层信任**：proxy_net 上挂着同机另外八个项目的容器，它们仍能伪造 `X-Forwarded-For`。要再窄一层得让 `infra_nginx` 在 **vps_infra** 仓库里拿一个固定 IP，然后这里改成 `/32` —— 那是另一个仓库的改动。⚠️ 另一条要盯的：**proxy_net 的网段由 vps_infra 定**，它变了而这里没跟着改的后果是**静默失效**（来源又变回 infra_nginx 自己）。
> ⚠️ **后续项：Argon2 的 `parallelism` 与容器 CPU 配额不匹配**（2026-09-17 生产基线发现）：argon2-cffi 默认 `parallelism=4`，四个线程同时烧 CPU，1.0 核的 CFS 配额在墙钟 25 ms 就烧完、其余时间被节流 —— 所以即使调到 1 核，一次登录仍要 279 ms（开发机 35 ms）。两条路：把 api 配额给到接近 `parallelism` 的核数，或把 `parallelism` 调低。**后者是认证参数，要过设计闸门**（已有哈希自带参数，旧哈希仍可验证，但成本曲线会变）。现阶段只有管理员登录，不紧急。
> ⚠️ ~~**前端产物是单个 974 kB 的 chunk**，加到第三、四个路由时必须做路由级懒加载~~ —— **T0.10 已做，但收益远小于这条预期**：实测 `/login` 首屏 974.50 → 933.91 kB（gzip 316.69 → 309.67），**只省 2.2%**。重量是 antd，每条路由都用它，只会被提成共享分片。**真要压首屏得从 antd 本身下手**（按需引入 / 换轻量组件 / 自建主题），那是另一个任务，现在没有归属。详见 [perf-baseline.md](perf-baseline.md) 第 7 节。
- [x] FX 供应商评估（D1 已定：BNM openAPI，见 [ADR-0005](adr/ADR-0005-fx-rate-source.md)）
- [x] ~~GitHub 仓库 + 受保护 `main`~~ 已建、已推送；`main` 保护规则已配（禁 force push / 禁删除 / 强制 PR / 线性历史 / 管理员同样受限），CI 五项 `docs` / `scripts` / `policy` / `backend` / `secret-scan` 已全部设为必需状态检查（`backend` 于 T0.2 合并后追加）

**验收**：所有服务能起 · DB 迁移能跑 · 管理员能登录 · 2FA 可用 · CI 拦住合并并能部署不可变镜像 · RPO/RTO 恢复方案已批准

## Phase 1 — Tenant, Project & Wallet Core（§124）

- [ ] Tenant
- [ ] Project
- [ ] 管理端客户管理
- [ ] Wallet
- [ ] 不可变钱包账本
- [x] 管理员手工调账（AIH-TASK-011，见下面的记录段；随合并生效）
- [x] API 凭据（加密存储、版本化、可轮换）（AIH-TASK-012，见下面的记录段；随合并生效）
- [x] **出站 webhook 密钥 schema 二选一**（`project_webhook_secrets` 新表 / `projects` 加暂存列），走设计闸门后再实现——见 [ADR-0004](adr/ADR-0004-credential-encryption.md) 第 4a 节。2026-09-25 已选定方案 i（新表），实现另过设计闸门 → 设计闸门 #135 `APPROVED: design v1`（2026-09-27），实现登记为 AIH-TASK-019；Kelvin 2026-09-28 同意密钥表与投递拆成两步，投递归 Phase 3。AIH-TASK-019 建了表、四个管理端接口与签名库（见下面的记录段；随合并生效）
- [x] 审计日志（AIH-TASK-021 到 023，按下面的「审计日志差异清单」判断，见 AIH-TASK-023 记录段；随合并生效）

> 未勾的几项**不是漏勾**，是各自还差一块（2026-09-25 汇总自下面各任务记录）：
> - Tenant：身份字段（004）与计费状态（005）已有；账户状态 `account_status` 的列与启用 / 停用接口在 AIH-TASK-020（随合并生效）；缺关户（依赖 Phase 2 与 Phase 4）与有效状态合成（Phase 3）
> - Project：身份字段与管理端建 / 列（004、006）已有，出站 webhook 签名密钥（019，`project_webhook_secrets` 表）已有；缺集成字段（后端地址、状态 webhook 地址、`integration_status`），随状态模型任务与 Phase 3
> - 管理端客户管理：建客户、列表、详情、编辑（006、009）已有，前端的列表、建客户、详情与编辑页（015）已有，账户状态的启用 / 停用接口（020）随合并生效；缺账户状态的前端、低余额阈值配置
> - Wallet：建客户时同事务建钱包（006）、手工调账（011）已有；缺管理端查看流水（011 设计 §10 后移）
> - 不可变钱包账本：数据层与触发器（005）已有；缺余额不一致的定时核对与告警（005 记录的后移项）
>
> 审计日志已勾（AIH-TASK-023）：Kelvin 2026-09-28 定的范围 AIH-TASK-021 到 023 都已落地，下面「审计日志差异清单」里剩下的
> 都不是 §124 的验收项，理由见 AIH-TASK-023 记录段

**验收**：管理员建客户 → 自动有钱包 · 建 project · 建 API 凭据 · 调账生效 · 所有动作都有审计

> 🔒 Phase 1 测试不通过，不得进入 Phase 2（§137）。

### 测试专用验收夹具 —— 生产上固定的验收租户与账号（2026-09-21，Kelvin 要求）

以后「合并部署后在生产上核对一遍」用这套固定夹具，不再每次新建一个客户，也不再用 Kelvin
自己的管理员账号（那个账号的 TOTP 密钥泄露过，重置排在整个项目做完之后）。夹具的公司名以
`[TEST]` 开头、正文写着 `DO NOT BILL`，**任何按客户数做的基线判据都要把它减掉**。

- [x] **专用管理员账号**：`python -m app.cli create-admin` 在生产建了一个只用于验收的
  ADMIN（`users` 第 2 行），再走 `/api/v1/auth/login` → `/api/v1/auth/2fa/enrol`
  → `/api/v1/auth/2fa/confirm` 完成 TOTP 注册，拿到 10 个恢复码。⚠️ **账号标识、密码、TOTP
  密钥与恢复码一概不写进本仓库**（仓库是公开的）——它们只在 VPS 上仓库外的一个 0600 文件里：
  不在部署用的 git 工作副本内、没挂进任何容器、不进对话，连路径也不写在这里
- [x] **验收租户与项目**：用这个账号调 `POST /api/v1/admin/customers` 建
  `[TEST] Acceptance Fixture - DO NOT BILL`（`6e9fa169-…`），再调
  `POST /api/v1/admin/customers/{customer_id}/projects` 建 `acceptance-smoke`（`ef3d4951-…`）。
  读回核对：`billing_status=SUSPENDED`、`status_version=0`、钱包 `MYR` / `"0.00000000"` / 版本 0，
  建客户的响应与 `GET /api/v1/admin/customers/{customer_id}` 的详情逐字一致
- [x] **生产库只读核对**：`tenants` 与 `wallets` 各 2 行、`projects` 2 行；`CUSTOMER_CREATE` 的
  `after_state` 恰好 `public_id`、`company_name`、`billing_status`、`wallet_currency` 四个键，
  `PROJECT_CREATE` 恰好 `public_id`、`name`、`tenant_public_id` 三个键，都不含 email、
  contact_name、phone（REQ-PRIV-001）
- [x] **2026-09-20 那个核对客户改判为废弃**：不删，公司名前面加 `[DEPRECATED] `、同时刷新
  `updated_at`。⚠️ **这一步直接 UPDATE 了生产库的一行，没有留下任何审计** —— 管理端没有编辑
  客户的接口。用 `WHERE public_id = ... AND company_name = ...` 限定，`ROW_COUNT()` 为 1，
  改完再用管理端接口读回确认（`mysql -N` 的默认字符集会把中文显示成 `????`，核对要走接口或
  `--default-character-set=utf8mb4`，别按终端里看到的乱码判断）
- ⚠️ **这个账号拿不到真正的第二因素保障，而它的权限是平台级的**：要让核对能自动跑，密码与第二
  因素必须由同一处保管，于是那个文件一旦泄露，拿到的是**完整的管理员会话** —— 不是「只影响那个
  零余额的夹具租户」。它能列出、读取所有客户，也能给任何客户建项目（管理端就这五个接口）；AIH-TASK-009 之后还能改任何客户的资料。
  现在生产库里除夹具外没有真实客户，暴露面才是有限的；**这个前提会失效**
- [ ] **第一个真实客户进生产之前**，必须重新处置这个账号：降到一个只读 / 只限本租户的角色（现在
  没有这种角色，`UserRole` 只有 ADMIN 与 CUSTOMER），或改成只在核对期间启用、用完停用，或干脆
  撤销、把核对改回人工。三选一之前，不要把它当成长期安排（未决，等 Kelvin 拍板）。
  2026-09-28 Kelvin 就 OpenClaw 浏览器验收选了 (a)：AIH-TASK-024 起的自动验收沿用这个账号，期限同样是第一个真实客户
  进生产之前，届时仍按上面三选一降权或撤销 —— 这条待办不因此关闭
- [x] 管理端缺一条**带审计的**「编辑客户」路径，所以改名这类事现在只能直接动生产库、绕过
  `audit_logs`。**这是一笔明账：spec §66 要求这个动作留审计，这次没有留**，事后也不补 ——
  往 `audit_logs` 里手写一条「像是应用写的」记录，比缺一条更坏。补偿只有本记录：谁、何时、
  哪一行、改前改后各是什么。Phase 4 客户门户之前要把 `CUSTOMER_UPDATE` 这条路径补上
  → **AIH-TASK-009 已补**：`PATCH /api/v1/admin/customers/{customer_id}`，客户行与
  `CUSTOMER_UPDATE` 审计同事务；审计只记 `company_name` 前后值与 `changed_fields` 字段名，
  email / contact_name / phone 的值不进审计。2026-09-21 那一次直改**仍然没有审计，也不补**，
  上面那条明账照旧成立；以后改客户资料走这个接口，不再直接动生产库。契约见 [api.md](api.md)
- [ ] AIH-TASK-009 部署后，生产上第一次用编辑接口时只读核对一次：`CUSTOMER_UPDATE` 的
  `before_state` 恰好 `public_id`、`company_name` 两个键，`after_state` 再加 `changed_fields`，
  都不含 email、contact_name、phone 的值

---

## Phase 2 — AI Usage Billing Engine（§125）

> 拆分、依赖与关键路径见 [PHASE-2-plan.md](design/PHASE-2-plan.md)（Kelvin 2026-09-28 确认）：12 个任务 T-A…T-L，8 个过设计闸门；设计闸门先写，实现任务等 AIH-TASK-020–023 合并部署后再登记。自动 FX 适配器（BNM）归本 Phase，Phase 8 只做供应商价格同步。
>
> 2026-09-29：020–023 已交付，T-A 登记为 `AIH-TASK-025`（设计闸门 #163 `APPROVED: design v4`，设计在 [AIH-TASK-025-ai-catalog.md](design/AIH-TASK-025-ai-catalog.md)）。审查中两处改动值得记住：计量类型由只读改为管理员可新建（Kelvin 选择满足 §58，不写偏离 ADR）；别名按 `occurred_at` 分段、解析与改映射用供应商行锁串行 —— T-B / T-D 的价格与规则发布将面临同一个「发布与计费解析的先后」问题，那两个闸门要照此处理。
>
> **占位名与登记编号、设计闸门对照**（2026-09-29 更新；设计文件正文里用占位名互相引用，以此表为准）：
>
> | 占位 | 任务 | 设计闸门 | 登记 |
> | --- | --- | --- | --- |
> | T-A | 目录 | #163 `APPROVED: design v4` | `AIH-TASK-025` |
> | T-B | 供应商价格版本 | #177 `APPROVED: design v4` | `AIH-TASK-026` |
> | T-D | 定价规则 | #178 `APPROVED: design v4`（Kelvin 2026-09-30：保留全局默认第一条起点为空） | `AIH-TASK-027` |
> | T-C | FX 汇率、BNM 拉取、告警接口 | #183 `APPROVED: design v3`（Kelvin 2026-09-29：从发布时刻起生效；过期阈值 5 个日历日。2026-09-30 v2：登记前预审后修正发布 / 退役规则与数据库兜底，按 BNM 实测改写适配器，按层拆分；v3：补拆分后预审指出的九处） | `AIH-TASK-028`（F1）、`AIH-TASK-039` 到 `AIH-TASK-044`（F2–F6 与 F2b） |
> | —（T-C / T-D 的补充） | 两张锁表禁止 UPDATE | #200 `APPROVED: design v1`（#199 的 Codex 审查引出；Kelvin 2026-09-30：先修再收尾 028，新开一个闸门） | `AIH-TASK-045` |
> | T-E | 用量摄取端点 | #176 `APPROVED: design v8` | `AIH-TASK-029` |
> | T-F | 批量摄取 | #180 `APPROVED: design v3` | `AIH-TASK-030` |
> | T-G | 计价引擎与试算 | #179 `APPROVED: design v1` | `AIH-TASK-031` |
> | T-H | 异步计费 worker | #181 `APPROVED: design v2` | `AIH-TASK-032` |
> | T-I | 最热租户压测脚本（ADR-0011） | 不走闸门 | `AIH-TASK-033` |
> | T-J | 管理端用量查询接口 | 不走闸门（只读） | `AIH-TASK-034` |
> | T-K | 管理端前端：目录；价格与汇率 | 不走闸门 | 拆成 `AIH-TASK-035` / `AIH-TASK-036` |
> | T-L | 管理端前端：规则与试算；用量事件 | 不走闸门 | 拆成 `AIH-TASK-037` / `AIH-TASK-038` |
>
> 计划里的 T-K、T-L 各拆成两个前端任务（2026-09-29）：两者各覆盖三四组页面，单个 Worker run 的改动面会超过 20 个文件（015 的经验：十九个路径已经吃力）。拆分不改变范围与验收，只改变交付粒度；Phase 2 因此是 14 个编号任务。**Phase 2 全部 14 个任务已登记。**
>
> 审查中的教训（写给后面的闸门与实现）：① 凡是「只在末尾追加的版本序列」都要数据库层的三道保护 —— 只能以草稿插入、发布跃迁上校验完整性、锁住同一把锁后检查区间不重叠；只靠服务层会被 Codex 判阻断（T-B、T-C、T-D 都补了）。② 复合外键与 CHECK 只在组成列非空时生效，必需列要逐列写 NOT NULL（T-E）。③ 跑 `codex-review.ps1` 期间**不要改工作区**：脚本检测到改动会作废判定、不发布（本次踩过一次）。

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
  - AIH-TASK-020 契约（设计闸门 #136 v2 §2 末尾）：用量事件摄取与扣费在 `PENDING_ACTIVATION` / `ENABLED` / `DISABLED`
    下**照常**（§112.1：在途的合法事件要处理完，停用不拦用量）；`CLOSED`（关户任务之后才会出现）的事件不入账，进人工复核
  - ADR-0011：用量扣费的设计闸门必须带最热租户压测（单租户 100 事件 / 秒突发、500 / 秒回补、worker 重试与崩溃下事件 / 账本 / 余额一致），以及压测不达标时切到「同租户批事务」的判据；ADR-0010：停机规则保持 `balance <= 0`

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
  - AIH-TASK-020 契约（设计闸门 #136 v2 §2 末尾）：新的 AI 调用在账户状态 `PENDING_ACTIVATION` / `DISABLED` / `CLOSED` 下
    阻断，`ENABLED` 时看计费与集成状态。投递设计把 `tenant.billing_status_changed` 与 `tenant.account_status_changed` 两类
    outbox 事件合成有效状态（`ALLOW_AI` / `BLOCK_AI` 与 `reason_code`）、扇出到各项目；两维共用的 `status_version` 就是
    集成方按版本丢弃旧状态的依据。`projects.integration_status` 也归那个设计闸门
- [ ] 周期性状态对账
- [ ] 积压监控
  - ADR-0010：试点观测项加「每次停机时的余额（透支了多少）」，透支明显时再按新 ADR 评估提前停机

**验收**：计费平台离线时 Claude 仍能回复 · 用量落本地 · 计费恢复后补投积压 · 无重复扣费 · 停机后不再发起 AI 调用 · 复机后恢复服务

> 试点成功之后才能推广到其他应用。

---

## Phase 4 — Payment & Customer Portal（§127）

- [ ] 客户认证
  - AIH-TASK-020 契约（设计闸门 #136 v2 §2 末尾）：客户门户登录在 `ENABLED` 下允许，`DISABLED` 与 `CLOSED` 下只读（看账单
    与收据），`PENDING_ACTIVATION` 由本 Phase 的设计闸门定
- [ ] 客户仪表盘
- [ ] 钱包页
- [ ] 充值 UI（金额不许硬编码）
  - ADR-0010：§42 的最小恢复额 = 欠款 + 可配置缓冲（不是只比 0 多一分钱），设计闸门里要写明缓冲的配置项与计算
- [ ] 支付网关适配器 + 选定的马来西亚网关（FPX 优先，依赖 D6）
- [ ] 支付 Webhook
- [ ] 主动的 stale payment 对账定时任务
- [ ] 支付金额不符的人工复核流
- [ ] 钱包充值入账
  - AIH-TASK-020 契约（设计闸门 #136 v2 §2 末尾）：新充值在 `PENDING_ACTIVATION` / `ENABLED` 下允许，在 `DISABLED`
    下**拒绝**（§112.1 第 1 步），`CLOSED` 下拒绝
- [ ] **关户（`→ CLOSED`）**：Phase 2 的用量事件与本 Phase 的支付落地后另立任务，走设计闸门（已在「后续计划」）。
  前置条件（AIH-TASK-020 设计 §2 末尾，Kelvin 2026-09-28 确认）：只能从 `DISABLED` 关；钱包余额恰好为 0；没有非终态的
  用量事件与支付（§112.1）；在记账总入口 `post_transaction` 的租户行锁内拒绝给 `CLOSED` 租户入账；`CLOSED` 为终态。
  关户后拒绝新调账、建项目、建凭据、轮换凭据（吊销凭据、查看、编辑客户仍允许）。AIH-TASK-020 已让列与 CHECK 允许
  `CLOSED`、账户状态接口从 `CLOSED` 出发一律 409，这个任务不必改列
- [ ] 支付收据 + 下载
- [ ] **开工前重新评估 Email 传输**：Phase 0 用 Google Workspace SMTP 直连，认证全过仍被 Outlook.com 判进垃圾箱（T0.9 2026-09-15 实测）。给客户发收据 / 通知之前，决定是否换事务邮件 API 服务，并补退信与投诉抑制（[ADR-0009](adr/ADR-0009-notification-channels.md) 备选 A 与已知缺口）

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

## 任务记录里仍未结清的后续项

> 2026-09-30 把任务记录移到归档时，从各记录里原样摘出、还没结清的勾选项（归档原位置留一行 ↪ 指向这里），
> 按原来所在的记录分组。**本节只减不增**：做完就勾上；新发现的后续项写进 PR 描述的「已知未做 / 留给后续」，
> 由管理员收进规划块的「后续计划」。

### 来自「AIH-TASK-006 —— 管理端客户管理：建客户（同事务建钱包与审计）、建项目、分页查看（2026-09-20）」

- [ ] 设计文本与已发布契约对齐（仓库审查的建议）：请求体与查询参数由 FastAPI 先于 `require_admin` 校验，所以没带令牌、参数又不合法的请求得到 422 而不是设计 §2 / §5 写的 401。实现与 [api.md](api.md) 已写明；下次改这份设计（或做「改用路由器依赖」的重构）时把 §2 流程与 §5 那一行改成实际顺序（未开始）

### 来自「AIH-TASK-012 —— API 凭据：加密存储、版本化、轮换与吊销，外加签名校验库（2026-09-25）」

- [ ] **后移**：REQ-AUTH-001 的 replay 测试证据与防重放 nonce 存储随摄取端点做（Kelvin 2026-09-25 的第 4 项决定）；
  同时接上 `last_used_at` 写入与解密结果缓存

### 来自「AIH-TASK-015 —— 管理端前端：客户列表、建客户、客户详情与编辑（2026-09-27）」

- [ ] 部署后用验收夹具账号在生产上走一遍：列表里看到夹具客户、详情的余额与后端字符串一致、改一次联系人再改回（留两条
  `CUSTOMER_UPDATE` 审计，`changed_fields` 只有 `contact_name`）。只读部分（登录、列表、详情、余额）已由 AIH-TASK-024
  的自动浏览器验收覆盖（2026-09-28 首次 PASS，见 024 记录段）；「改联系人再改回」在只读版跑稳后再加，这一条因此仍不勾

### 来自「AIH-TASK-017 —— 管理端前端：手工调账（2026-09-28）」

- [ ] 部署后用验收夹具账号在生产上走一遍：给夹具客户记一笔小额贷方再记一笔等额借方（两个键、两行账本、两条
  `WALLET_ADJUSTMENT_POSTED`），余额回到原值

### 来自「AIH-TASK-019 —— 出站 webhook 签名密钥：签发、启用、退役与签名库（2026-09-28）」

- [ ] **后续**：ADR-0004「收口条件」里第 4a 节那一项的勾选（要改 ADR 文件，不在本任务的可改路径里）；管理端前端（已在
  「后续计划」）；Phase 3 的投递设计闸门接上 `signing_material` 与 `sign_status_webhook`，并在 api.md「状态 webhook 签名」
  补时间窗、重试与按事件 id 去重的约定

### 来自「AIH-TASK-022 —— 管理端审计日志查询接口（2026-09-29）」

- [ ] **后续**（受限审查的不阻塞意见）：
  - `refresh_tokens` 归「对外 id」让 ADMIN 看得到令牌家族 id（也是会话 id），见上面「实现定的细节」第一条 —— Kelvin
    2026-09-29 确认不隐藏，保持现状（记入 [REVIEW-LOG](REVIEW-LOG.md)「升级给人的分歧」）
  - `entity_type` 扫描测试对「形参同名」的豁免太宽：包一层 `record_audit` / `AuditLog` 的辅助函数会让它的调用方漏扫；
    应把豁免限定在已知的写审计函数上
  - `_state` 假定库里的 JSON 是对象，非对象会在响应校验时 500；现有写入方都是 `json.dumps(dict)`，暂可接受

### 来自「AIH-TASK-023 —— 管理端前端：审计日志页（2026-09-29）」

- [ ] **后续**：
  - `ResetPasswordPage` 的 5s 超时在 2026-09-28 把逐字输入改成粘贴之后又在 Worker 里连挂两轮（本任务没改它）；本任务新加的
    `AuditLogPage.test.tsx` 又用了逐字的 `user.type`，全量并行时可能拖慢同批用例。要不要把它也改成粘贴、或给 auth 页用例
    放宽超时，另开任务
  - 第 3 张截图里「Entity」列在窄视口下把邮箱逐字折行，可读性差；属于样式，另开任务
  - 脚本新增步骤依赖的前端事实：审计表格的「Action」「Actor」列头、动作下拉的选项带 `title` 与 `aria-selected`（antd Select
    的现状）、「Apply filters」按钮；前端改了它们，验收以 FAIL 结束（不会误报 PASS），届时一起改脚本
  - 实体类型现在是自由文本框；要改成下拉，选项得与 `app/services/audit_query.py` 的两类清单同步

### 来自「AIH-TASK-024 —— AIH-TASK-015 管理端客户页的浏览器验收脚本（OpenClaw P6 试点，2026-09-28）」

- [ ] **后续**：
  - 受限审查的不阻塞意见：`emit()` 不检查 `finished`，285 秒硬停与某一步完成撞在一起时，理论上会在结论行之后多打一行
    `ACCEPTANCE_STEP`，一行守卫即可堵上；改这个脚本要另登记任务
  - 脚本依赖的几条前端事实（分页反映在 `?page=`、表格不拆表头与表体、详情接口与 `hosts[0]` 同源、响应信封是
    `{ success, data: { id, wallet } }`）现在成立；前端改了它们，验收会以 FAIL 结束（不会误报 PASS），届时一起改脚本
  - 「改联系人再改回」的写入版验收，在只读版跑稳后另开任务（AIH-TASK-015 的走查项因此仍不勾）
  - 验收管理员账号在第一个真实客户进生产之前降权或撤销，见「测试专用验收夹具」与「已阻塞」

### 来自「AIH-TASK-025 —— 供应商、模型与计量单位目录（T-A，2026-09-29）」

- [ ] **后续**：
  - Worker 评审的 20 万字符上限：026 / 027 的允许路径与 025 同一量级，很可能同样超限。是按 diff 规模拆着登记，还是
    在控制面调高上限（控制面任务，走它自己的审查），Kelvin 定在 025 落地之后再决定
  - 设计 §7「撤销与重新映射」写「共三段首尾相接」，但「映射 → 撤销 → 重新映射」只产生两段（重新映射从撤销点接上）；用例
    按「映射 → 改指向 → 撤销 → 重新映射」造出三段，逐段核对了设计列出的各个时刻的解析结果
  - 「快照读陷阱」用例同时断言了陷阱本身（同一事务里的普通读仍是旧快照），依赖 MySQL 默认的 REPEATABLE READ；CI 的
    MySQL 若改了隔离级别，那一条断言要跟着调整
  - 前端 `frontend/src/api/adminAudit.ts` 的 `AUDIT_ACTIONS` 是 `AuditAction` 的手抄副本，还没有这八个新动作（本任务不改
    前端）；审计页的动作下拉因此选不到它们，按 `entity_type` 筛选不受影响。随 T-K（AIH-TASK-035）补上

### 来自「AIH-TASK-026 —— 供应商价格版本与泛化价格分量（T-B，2026-09-29）」

- [ ] **后续**：
  - 前端 `frontend/src/api/adminAudit.ts` 的 `AUDIT_ACTIONS` 还没有这五个新动作（本任务不改前端），随 T-K
    （AIH-TASK-036）补上
  - 「快照读陷阱」用例同时断言了陷阱本身（同一事务里的普通读仍是旧快照），与 025 一样依赖 MySQL 默认的
    REPEATABLE READ
  - 数据库兜底的两个缺口，与 027 在 #178 v4 补上的同类（#192 的实现审查发现）：绕过服务可把草稿直接发布为起点为空
    （起点为空只该出现在该（供应商, 模型）没有区间非空的已发布版本时，设计 v4 §2）；可把版本直接改为 `RETIRED` 而不写
    `effective_to`，它仍被取价、并让之后的发布撞上区间不重叠触发器。Kelvin 2026-09-30 决定另开任务补（新迁移，
    碰定价，要先过设计闸门）

### 来自「AIH-TASK-027 —— 客户定价规则 MARKUP 与 FIXED_RATE（T-D，2026-09-29）」

- [ ] **后续**：
  - 前端 `frontend/src/api/adminAudit.ts` 的 `AUDIT_ACTIONS` 还没有这五个新动作（本任务不改前端），随 T-L
    （AIH-TASK-037）补上
  - 「快照读陷阱」用例同时断言了陷阱本身，与 025 / 026 一样依赖 MySQL 默认的 REPEATABLE READ

### 来自「AIH-TASK-045 —— 两张锁表禁止 UPDATE（pricing_rule_locks 与 fx_rate_locks，2026-09-30）」

- [ ] **后续**：残余风险同 0006 / 0014 / 0015：`TRUNCATE` / `DROP TRIGGER` / `ALTER TABLE` 是 DDL，不经触发器，归
  「后续计划」里的数据库账号权限拆分
  - Worker 审查的非阻断观察：`tests/backend/test_fx_rates_db.py` 升降用例上方的注释说 fx 的触发器随表删除、pricing 的
    由 0016 的 downgrade 删除，实际是 0016 的 downgrade 先跑、两个都删；断言本身正确。只是注释，留给下一个改这个文件的
    任务顺手改正

### 来自「流程加固 —— 审查流水线（2026-09-11）」

- [ ] 准入查询的瞬态空结果：`gh pr checks --watch` 刚返回时立刻跑 `codex-review.ps1`，`gh pr checks --json` 曾报「no checks reported」（复制延迟），几秒后正常。现在是 fail-closed（退出 2），但紧跟 CI 跑审查是最常见的用法。给这一种报错加有界重试（只读查询，≤3 次），**不是本轮顺手修的范围**，另开 PR
- [ ] `.ps1` 文件 UTF-8 BOM 检查（Codex 建议时看的是无 `.ps1` 的分支才暂缓；现在 main 上有三个）
- [ ] `ADR-0004`（355 行，是其余 ADR 的 3–4 倍）实为四个决策捆在一起：凭据加密 + 密钥轮换流程 + 备份恢复 + 未决的出站 webhook 密钥 schema。最后一项本来就要走设计闸门、本来就要写新 ADR，可借机拆分。**未做**

### 来自「AIH-TASK-005 的前置：MySQL 允许应用账号建触发器（2026-09-19）」

- [ ] 控制面：Worker 是否在 lint 之前跑 `ruff check --fix` / `ruff format`。现在 Worker 没有自动修复这一步，一条导入顺序问题就让整个 run 失败：`AIH-TASK-004`（run `7671aead`，CI 挂 I001 后补提交）与 `AIH-TASK-005`（run `5cfb3b84`，结算为 `checks_failed`）都是这样。要改的是控制面仓库，不在本仓库（未开始）
- [ ] 控制面：Worker 合并前判断「检查是否通过」时，把同一 head 上**所有**工作流运行的检查都算进去（`statusCheckRollup`）。改 PR 正文会让 CI 重跑、把还在跑的旧一轮取消，留下的 `CANCELLED` 永远算作没通过，GitHub 页面却全绿。`AIH-TASK-006` 因此两次「批准」都被拒，靠 `gh run rerun <旧运行> --failed` 把那一项跑绿才合并。设计闸门任务每次都要改 PR 正文，所以会反复出现；应改成只看每个检查名最新的一次。要改的是控制面仓库（未开始）
- ⚠️ **Worker 里 skipped 不是 passed**：这 17 个用例在 Worker 里不再有信号，只由 CI 覆盖。另：`AIH-TASK-001` 是只跑检查、不开 PR 的任务，而当前 Worker 只接受 `creates_branch` / `creates_pull_request` 为 `true` 且有 `allowed_change_paths` 的开发任务，所以它在这个 Worker 上跑不了（run `3a699c91` 以 `invalid_contract` 失败）。留在契约里会误导，待清理（删掉该任务，或让 Worker 支持只读检查任务）

## 待办：密码重置与通知投递的几项加固（T0.8d 第三轮整体自查）

复审第三轮要求「停止逐项补洞、整体比对」，下面几项是那次自查发现的。**都不是
T0.8d 的阻断项**，spec §53 与设计闸门 #32 v5 都没有要求，所以本轮没有顺手做。

- [ ] **对同一个邮箱的重置申请没有频率限制。** 限流是**按来源**的（`auth_rate_limit_*` + nginx `limit_req`），换 IP 就能绕开 —— 于是对着一个已知邮箱狂发，受害者会收到一串重置邮件（邮件轰炸，也是对我们发信域名声誉的消耗）。常见加固是「每个账号每 N 分钟最多一封」，落点在 `request_reset`：申请前查最近一条未过期令牌的 `created_at`。⚠️ 实现时**不得**因此改变对外响应（那会重新打开用户枚举通道）
- [ ] **`password_reset_tokens` 与 `domain_outbox` 都只增不减，没有清理 / 归档任务。** 前者每次申请一行，后者 Phase 2 的用量摄取接上来之后会长得很快。保留策略与审计日志的保留期一起归 T0.9
- [ ] **重置成功后不发「你的密码刚刚被修改」通知。** 这是账号被盗时受害者唯一可能察觉的信号。§53 没列，Phase 6 通知体系里一并做
- [ ] **邮件正文硬编码在 `app/tasks/outbox.py` 的渲染函数里，没有模板层。** 只有一封信时这样最简单，但 ADR-0009 明确说过 `rs-roof-pms` 没有模板层是缺点 —— **第二类通知出现时必须抽出来**，否则会变成字符串拼接的泥潭。归 Phase 6
- [ ] **`frontend_base_url` 不校验格式。** 配成一个不是 URL 的值时，拼出来的链接是坏的，而症状要到用户点不开才暴露

## 待办：缺失的 secret 文件不会让栈起不来（既有，与 compose 注释所写相反）

- [ ] **`docker-compose.yml` 的 `secrets:` 注释说「文件不存在时 `docker compose up` 直接失败 —— 与密码留空同一种 fail-closed」，实测不成立**（T0.8d 复审时验证）：Docker 会把缺失的 `file:` secret 挂成一个**空目录**，栈照常起来。三把密钥都一样 ——
  - `jwt.key` 缺失 → `load_signing_key` 抛 `AuthNotConfigured`，认证端点 503（**这一条仍是 fail-closed，只是不在 compose 那一层**）
  - `master.key` 缺失 → 2FA 端点报 `ENCRYPTION_NOT_CONFIGURED`
  - `smtp.password` 缺失 → 密码**静默变成空串**，只留一条 error 日志（T0.8d 已在自己那条注释里写明实际行为）

  也就是说「忘了建文件」与「本来就不需要密码」在行为上几乎一样。要真的 fail-closed，得在应用启动时显式校验这几个路径指向的是**可读的文件**而不是目录。**这是既有缺口，不是 T0.8d 引入的**；T0.8d 复审只修了被指出的问题，没有顺手改那条既有注释（范围）。

## 待办：既有配置项缺下界校验

- [ ] **`Settings` 里既有的 TTL / 计数类配置项没有下界约束**（T0.8e 派生）：`access_token_ttl_seconds`、`refresh_token_ttl_seconds`、`refresh_token_idle_seconds`、`login_max_failures`、`login_lockout_seconds`、`auth_rate_limit_*` 设成 0 或负数都不会被拒，各自会带来一种「配置对了一半」的故障（令牌立刻过期、锁定立刻解除、限流桶为空…）。T0.8e 只给自己新增的两个加了 `gt=0`，**刻意没有顺手补旁边的**（那是范围扩张）。补的时候要逐个想清楚每项的合理下界，不是无脑 `gt=0`

## 文档欠账

- [ ] D1–D7 的 ADR（`docs/adr/` 目录已建，见 [adr/README.md](adr/README.md)；D1、D3、D4、D5 已完成，D6 部分完成，**剩 D2、D7**）
- [ ] `docs/database-schema.md`（Phase 1 起维护）
- [x] `projects` 表结构裁决（Phase 1 建表前）：spec §57 的 UI 字段有 `project_id` 与 `description`，§76 的表却是 `id` + `public_id`、没有 `description`。两节不一致，建表前定下来并写进 `docs/database-schema.md`；改 spec 的话走一次勘误 —— **已裁决（Kelvin，2026-09-19）**：§57 的 `project_id` 就是 §76 的 `public_id`；加可空 `description`。§74 说表定义是最低要求，多一列不冲突，**不需要勘误**。见 [database-schema.md](database-schema.md)
- [ ] `docs/api.md`（Phase 1 起维护）—— [已新建](api.md)（AIH-TASK-006），目前只有管理端客户管理的五个接口；认证接口的契约还没补进去
- [ ] `docs/pricing-engine.md`、`docs/currency-and-fx.md`（Phase 2）
- [ ] `docs/integrated-application-backend.md`（Phase 3）
- [ ] `docs/payment-flow.md`（Phase 4）
- [ ] `docs/data-governance.md`、`docs/runbook.md`（Phase 0 起补，runbook 需覆盖 §136 列的 18 个故障场景）；[`docs/deployment.md`](deployment.md) **已起草，七件决策已定案** —— T0.9 的方案文档；落地部分仍待生产主机
- [x] 逐条核对 25 条评审意见与 spec v1.1 —— 完成于 2026-09-10，结果见 [REVIEW_FOLLOWUP_v1.1.md](archive/REVIEW_FOLLOWUP_v1.1.md)（20 条已解决 / 5 条残留，已转为上方 R1–R5）
- [x] 给 spec 的硬性要求补 `REQ-*` 编号（= R5）—— AIH-TASK-008（2026-09-21）收口：spec v1.7 新增 `REQ-PRIV-002` / `REQ-TXN-001` / `REQ-TAX-001` / `REQ-LAUNCH-001` 四个编号，其余 11 条逐功能交付流程要求标「不适用」并逐条给理由；[REQUIREMENTS.md](REQUIREMENTS.md) 第 1.3 节已无「缺口」行

# TODO — 开发任务清单

> 任务骨架来自 spec §123–§131（Phase 0–8）。验收标准直接抄自 spec，**不要自行放宽**。
> 最后更新：2026-09-12

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
- [x] **T0.2 — CI 后端 job**：`.github/workflows/ci.yml` 加 `backend` job（`ruff check` + `ruff format --check` + `pytest`）；[WORKFLOW §7](WORKFLOW.md) 的命令清单补上 lint 与 pytest；在分支保护里把 `backend` 设为必需状态检查。**另加打包冒烟**：装进干净 venv 后 `import app.main` 并起一次 `/healthz` —— `pytest` 跑的是源码树（`pythonpath = ["."]`），发现不了 wheel 少打子包这类问题（PR #22 审查实证）
- [x] **T0.3 — 日志与统一错误处理**（§94、§107）：结构化日志、request id、统一错误响应体、领域异常层次、日志脱敏（密钥与 AI 内容绝不入日志）
- [x] **T0.4 — MySQL + SQLAlchemy + Alembic 接通**：engine / session 生命周期、`Decimal` 列约定（Invariant 10）、Alembic 初始化与首个迁移、带依赖的就绪检查
- [x] **T0.5 — Redis + Celery 接通**：Celery app、worker 与 beat 配置、一个可验证的探活任务
- [x] **T0.6 — Docker Compose**：nginx · frontend · api · celery-worker · celery-beat · redis · mysql；含 API / Celery 容器的运行 UID 决定，宿主机主密钥文件**属主设为该 UID**、权限 `0400`（Compose 的 `file:` secret 走 bind mount，`uid`/`gid`/`mode` 只在 swarm 生效；**不得为读密钥把容器改回 root**）
- [x] **T0.7 — React 骨架**：Vite + TS + React Router + TanStack Query + Axios + Ant Design + i18n 骨架（V1 只出英文，文案不许硬编码在组件里）。**另含 T0.6 欠下的第七个服务**：compose 加 `frontend`、把 `deploy/nginx/billing.conf` 的 `location /` 从 503 占位改成指向它、同步 `tests/backend/test_compose.py` 的 `EXPECTED_SERVICES`（三处漏一处测试就红）
- [ ] **T0.8 — 认证基座**：管理员登录、密码哈希、会话 / 令牌、2FA。**设计闸门 [#32](https://github.com/kelvinpang90/ai_billing_hub/issues/32) 已批准 `design v5`**，按下面四个 PR 落地
  - [x] **T0.8a** — `users` / `refresh_tokens` / `audit_logs` 三张表、Argon2id 密码哈希与强度、登录、JWT + 刷新轮换与重放检测、登出与吊销、失败锁定、按来源限流、bootstrap CLI
  - [x] **T0.8b** — 信封加密模块（[ADR-0004](adr/ADR-0004-credential-encryption.md)）、`two_factor_settings` / `recovery_codes`、TOTP 注册 / 确认 / 校验、恢复码、**ADMIN 强制 2FA**
  - [ ] **T0.8c** — 前端登录页、路由守卫、令牌持有与刷新
  - [ ] **T0.8d** — `password_reset_tokens` + `domain_outbox`、忘记密码 / 重置密码、[ADR-0009](adr/ADR-0009-notification-channels.md) 的 Email 传输与投递任务（补齐 §53 的最后两项）
  - [x] **T0.8e** — 注册路径的 pending 2FA 令牌 TTL 改为 600 秒（T0.8c 整栈实测发现 120 秒走不完首次注册；设计闸门 [#37](https://github.com/kelvinpang90/ai_billing_hub/issues/37) `APPROVED: design v2`）
- [ ] **T0.9 — 生产拓扑与恢复方案**：专用生产 MySQL/Redis 拓扑（依赖 D3 / [ADR-0002](adr/ADR-0002-production-datastore-isolation.md)）、备份与恢复、加密密钥方案（依赖 D4 / [ADR-0004](adr/ADR-0004-credential-encryption.md)）、RPO/RTO 设计待批准；**另含 §94 的生产日志要求**（轮转、保留期、磁盘上限、安全删除、异地留存，以及「日志撑爆本地磁盘必须在威胁到 MySQL / 文档存储之前告警」）—— T0.3 只做了应用侧的日志**内容与格式**，这些是部署侧的事
- [ ] **T0.10 — 初始性能 / SLO 基线**

> ⚠️ **T0.9 必须处理的三件边缘代理遗留**（T0.6 派生）：① `deploy/nginx/billing.conf` 对 `/readyz` 的网段限制比的是 `$remote_addr`，生产上若在 nginx 前面再放一层代理，这条限制**形同虚设**，届时要改用 `real_ip_header` + `set_real_ip_from` 或在前一层拦掉；② nginx 仍以官方镜像默认方式运行（master 是 root）；③ TLS / 证书 / 真实域名尚未配置，栈现在只监听 80。
> ⚠️ **T0.9 的上线前置**（设计闸门 #32 定的）：**T0.8d 合并前没有任何自助密码重置**，只能走 `python -m app.cli create-admin` 那条 CLI。必须在第一次部署之前关掉。（另一条「管理员登录是单因素」已由 T0.8b 关闭。）
> ⚠️ **主密钥的宿主机那一半仍归 T0.9**（ADR-0004）：宿主机主密钥文件要 `chown 10001:10001` + `chmod 0400`。应用侧的加解密与文件读取 T0.8b 已实现，**但在宿主机那一半落地前不能部署到生产**。
> ⚠️ **主密钥没有重包裹任务**（T0.8b 派生）：`app/core/crypto.py` 已经支持多版本钥匙串（轮换时老行仍能解开），但把老行重新用新密钥包裹的后台任务还没有。没有它，轮换之后老密钥必须**永久保留**，否则历史 TOTP 注册全部作废。
> ⚠️ **边缘 nginx 自己的 `$binary_remote_addr` 仍是直连对端**（T0.8a 派生）：应用侧已经会解析 `X-Forwarded-For`（`app/core/clientip.py`，只在可信代理后面采信），但**边缘那层 `limit_req` 与 `/readyz` 的网段限制还没有**。生产上若在 nginx 前面再放一层代理，这两处都会把所有客户端看成同一个来源。T0.9 要配 `real_ip_header` + `set_real_ip_from`，三处一并收口。
> ⚠️ **边缘 nginx 的上游超时没有收紧**（T0.7 实测发现）：api 容器停掉时，边缘 nginx 要 **约 4 秒**才返回 502（DNS 解析不到上游的等待），`proxy_connect_timeout` 更是还挂着 60 秒的默认值。后果是**一次停机在用户侧表现成卡住而不是报错**，而且每个挂起的请求都占着 nginx 的连接。T0.9 要把 `resolver_timeout` 与 `proxy_connect_timeout` 收到秒级。实测数据：`curl` 到 `/healthz` 在 api 停机时耗时 3.96s。
> ⚠️ **前端产物是单个 877 kB 的 chunk**（gzip 283 kB，主要是 antd）。只有一个路由时拆包没有意义，**加到第三、四个路由时必须做路由级懒加载**，否则首屏会越拖越久。归 T0.10（性能 / SLO 基线）一并量。
> ⚠️ **celery-beat 没有存活探针**（T0.6 派生）：`celery inspect ping` 问的是 worker，够不着 beat。现在 beat 的 schedule 是空的，崩了也没有后果；**第一条周期任务落地时这就变成静默故障** —— beat 挂掉 = 对账扫描、状态轮询全部不执行，而 API 一切正常、没有任何报错。加第一条周期任务的那个任务必须同时给出探测手段（例如让 beat 自己周期性打一条心跳日志并挂告警）。
> ⚠️ **T0.9 必须包含的一条具体告警**（T0.5 派生，PR #28 审查指出）：`/readyz` 在 Redis 不可用时**刻意返回 200**，所以负载均衡不会发现这个故障，**它只能靠日志告警发现**。告警名 `billing_readiness_degraded_redis`，条件、分级与升级路径写在 [runbook](runbook.md)。告警落地之前，Redis 静默不可用是一个**已知的、被接受的检测缺口**。
- [x] FX 供应商评估（D1 已定：BNM openAPI，见 [ADR-0005](adr/ADR-0005-fx-rate-source.md)）
- [x] ~~GitHub 仓库 + 受保护 `main`~~ 已建、已推送；`main` 保护规则已配（禁 force push / 禁删除 / 强制 PR / 线性历史 / 管理员同样受限），CI 五项 `docs` / `scripts` / `policy` / `backend` / `secret-scan` 已全部设为必需状态检查（`backend` 于 T0.2 合并后追加）

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
- dev 依赖用 `httpx2` 而不是 `httpx`：starlette 的 `TestClient` 已弃用旧 httpx，装 `httpx` 会在每次跑测试时刷弃用警告。**代价是 `fastapi` 的下界被这条绑住**——下界放低，解析器可能挑到仍要旧 httpx 的 starlette，干净环境里 pytest 会在导入 `TestClient` 时挂掉。两个下界是一对，改一个必须复验另一个（PR #22 审查暴露）
- 本任务**没有**做 Alembic（原「仓库骨架」条目里带的）—— 它要和数据库 engine 一起落地才验得了，已移入 T0.4

**验证到什么程度**

- `python -m pytest` → 2 passed（应用能起、`/healthz` 返回 200、注入的配置生效）
- `python -m ruff check .` / `python -m ruff format --check .` → 通过
- [WORKFLOW §7](WORKFLOW.md) 三项本地检查通过；`python -m unittest discover -s tests` 仍是 61 passed，**新增的 `tests/backend/` 没有被它收集**（该目录无 `__init__.py`，不是可导入包）
- **打包路径单独验过**（PR #22 审查指出 `packages = ["app"]` 会漏掉全部子包）：改成 `[tool.setuptools.packages.find] include = ["app*"]` 后，`pip install .` 进一个干净 venv → `import app.main` 成功、`GET /healthz` 返回 200。这条**自动化留给 T0.2**，因为 `pytest` 跑的是源码树，结构上发现不了打包缺陷
- **干净环境单独验过**：新建 venv → `pip install -e ".[dev]"` → `pytest` 2 passed，且该环境里**根本没有 `httpx` 模块**（`import httpx` 抛 `ModuleNotFoundError`），证明 `TestClient` 走的就是 `httpx2`
- **未验证**：容器内启动、生产配置分支 —— 那是 T0.6 的事

**待清理**

- [ ] 把 `scripts/` 与 `tests/test_*.py` 纳入 ruff（约 52 处报错 + 5 个文件需重新格式化），单独开 `chore/` PR

### T0.2 任务记录（2026-09-11）

**做了什么**

- `.github/workflows/ci.yml` 加 `backend` job：装 `-e ".[dev]"` → `ruff check` + `ruff format --check` → `pytest` → **打包冒烟**
- `scripts/packaging_smoke.py`：新增。装进独立 venv 后验证子包真的在 wheel 里、`/healthz` 真的挂上了。它**放在 `scripts/` 下是刻意的** —— 以脚本方式运行时 `sys.path[0]` 是脚本自己的目录，那里没有 `app`，所以 `import app` 只能解析到已安装的那一份；脚本里再断言一次路径含 `site-packages`，防止有人把它挪到仓库根目录后静默失效
- [WORKFLOW §7](WORKFLOW.md) 命令清单从三项补到六项，并写明 `unittest discover` 与 `pytest` 跑的是两套不相交的测试；打包冒烟单列，说明只在动打包配置时需要本地手跑
- `scripts/codex-review.ps1` 的准入名单加 `backend`。**不加这一条，一个把 `backend` job 删掉的 PR 照样能进审查** —— 脚本注释里记着之前漏 `policy` 就是这么出的事
- CI 项数从四涨到五，四处引用同步：`README.md`、`CLAUDE.md`、[WORKFLOW](WORKFLOW.md) §2 的流程图与 §4 的准入描述、[HANDOFF](HANDOFF.md)

**偏离了什么**

- **分支保护里把 `backend` 设为必需检查这一步没做在 PR 里** —— 分支保护是仓库设置，PR 改不了，而且 `backend` job 要先随 PR #24 合进 `main` 才存在。**2026-09-11 已在 #24 合并后补做**（见下方补记）
- 打包冒烟没做成 `pytest` 用例：它要一个独立 venv 和一次 `pip install`，塞进单元测试会让本地跑测试从秒级变成分钟级。放 CI 每次跑、本地按需跑，是更合适的位置
- `scripts/packaging_smoke.py` **不在 ruff 覆盖范围内**（`pyproject.toml` 排除了 `scripts`）。与 T0.1 留下的待清理项同源，等那条清理时一并纳入

**验证到什么程度**

- 打包冒烟脚本**正反两面都跑过**：干净 venv `pip install .` → exit 0（`site-packages/app serves /healthz`）；指向源码树的 editable 安装 → exit 1 并打印拒绝理由。**只验通过路径不算数**——一个永远返回 0 的检查和没有检查一样
- [WORKFLOW §7](WORKFLOW.md) 六项本地全过：`check_docs` 8 条约定串、`check_repo_policy`、`unittest discover -s tests` 61 passed、`ruff check` / `ruff format --check`、`pytest` 2 passed
- `pwsh scripts/tests/Test-ReviewVerdict.ps1` 96 通过
- ~~**未验证**：`backend` job 在 GitHub Runner 上的实际行为~~ → PR #24 的 CI 已跑：`backend` 通过，25s（Linux + Python 3.12；本地是 Windows + Python 3.14）

**合并后补记（2026-09-11）**

PR #24 合并后执行了 PR 里改不了的那一步：把 `backend` 加进 `main` 的必需状态检查。这属于 [WORKFLOW §2](WORKFLOW.md) 的**唯一例外**（PR 里物理上做不到的仓库设置），按那里的三步走：原 PR 预先写明 → 合并后用追加型 API 执行 → 用只改状态陈述的 `chore/` PR 把逐项回读补回仓库。

- 走 `POST .../protection/required_status_checks/contexts` 这个**纯追加**端点，而不是 PUT 整份 protection —— 后者要重发全部字段，漏一个就是静默降级
- 回读：必需检查现为 `backend` / `docs` / `policy` / `scripts` / `secret-scan`
- 其余设置逐项回读确认未变动：`strict` 真、`enforce_admins` 真、线性历史 真、禁 force push、禁删除、对话必须解决 真、`dismiss_stale_reviews` 真、`required_approving_review_count` 0

### T0.3 任务记录（2026-09-11）

**做了什么**

- `app/core/logging.py`：JSON 行日志（stdlib `logging` + 自写 formatter，**没引 structlog** —— 三十行能解决的事不值得加一个依赖）。关联 ID 走 `ContextVar`，`request_id` 由中间件写入，`tenant_id` / `project_id` / `event_id` 留给后续 Phase 在拿到它们的地方 `bind_log_context()` 补上（§94 点名的四个字段）
- **脱敏做成格式化时的过滤，不是调用点的自觉**，且覆盖**三条**进入日志的路径（缺一条就等于没做）：
  1. `extra=` 的结构化字段 —— 按字段名递归脱敏（`password` / `secret` / `token` / `authorization` / `totp` / `signature` / `prompt` / `completion` 等）
  2. 日志 message 本身 —— 按文本形状脱敏
  3. **异常栈** —— 同样按文本形状脱敏。**初版漏了这条**（PR #26 审查指出）：`logger.exception()` 把异常消息原样写进日志，而抛错的人最常见的写法就是把触发它的那个值一起写进消息。结构化字段守住了、这条路敞着，等于没守
  ⚠️ 这是**兜底**，不是防线 —— 按字段名兜不住换了名字的字段，按文本形状兜不住没有 `key=value` 形状的自由文本。模块顶部写死了两条硬规矩：不把 payload 交给 logger、不把密钥拼进异常消息
- **递归脱敏 fail-closed**：环检测（按对象 id，兄弟节点各拿一份 `seen`，共享引用不误判成环）+ 深度上限 12，**到顶返回占位符而不是原对象**。初版到顶直接 `return value` 是 fail-open（PR #26 第二轮审查指出）—— 没遍历到的那层里若有 `password` / `prompt`，它会原样落进日志。**一个「查不下去」的脱敏必须当作「有东西没查」**
- `configure_logging()` 接管 `uvicorn` / `uvicorn.error` / `uvicorn.access` 三个 logger。它们自带 handler 且 `propagate=False`，不接管的话生产日志会一半 JSON 一半纯文本 —— **那等于没有结构化日志**
- `app/schemas/envelope.py`：§107 的 `{success, data, error, request_id}`。成功时 `error` 为 null、失败时 `data` 为 null，客户端只看 `success` 一个字段就能分支
- `app/core/errors.py`：`AppError` 基类（`code` + `http_status`，后续领域异常从这里派生）+ 四个处理器 —— `AppError` / 框架 `HTTPException` / 请求校验失败 / **未处理异常**。未处理异常一律返回固定文案 `INTERNAL_ERROR`，栈只进日志
- `app/core/middleware.py`：`RequestContextMiddleware`。每个请求从**空**上下文开始，分配或复用 `X-Request-ID`，记一条完成日志（方法、路径、状态码、耗时）
- `/healthz` 改为走同一个信封。给健康检查开形状例外，就得在每个客户端里维护「这个端点不一样」，而一致的代价在这里是零

**偏离了什么**

- **入站 `X-Request-ID` 接受但严格校验**（`^[A-Za-z0-9._-]{1,64}$`，不合规就自己生成）。接受它是为了让计费平台与 Integrated Application Backend 两侧日志能对上同一次调用；校验是因为它**会进日志也会回响应头** —— 换行能伪造日志行，超长值能撑爆日志
- **请求日志不记 query string、不记请求体**。query 里可能有密钥，而按字段名脱敏对一整串原始文本无能为力
- **§94 的生产日志要求（轮转、保留期、磁盘上限、安全删除、异地留存、磁盘告警）不在本任务**：那是部署侧的事，已移进 T0.9 的任务描述。T0.3 只管日志的**内容与格式**
- 校验失败只回字段名、不回收到的值：请求体里可能有密钥

**验证到什么程度**

- `pytest` **38 passed**（T0.1 的 2 条 + 本任务新增 36 条），其中写死的是这几条边界：
  - **异常栈进日志前过脱敏**：用例抛一个消息里带 `postgres://svc:hunter2@db-01/billing` 的异常，断言日志输出里无 `hunter2`、有 `***REDACTED***`、**且仍有 `Traceback`**（脱敏不能把排障信息一起吃掉）
  - 响应守住不等于日志守住 —— 两条路径分别验。日志里的凭据比响应里的更危险：会被永久留存、会转发到集中日志平台，而且没人盯着看
  - 未处理异常的响应里**不出现**异常原文（用例故意抛一个带 `postgres://user:hunter2@...` 的异常，断言响应里既无 `hunter2` 也无 `postgres` 也无 `Traceback`）
  - 框架 404 与领域异常走**同一个信封形状**
  - 校验失败不回显收到的值
  - 敌意 `X-Request-ID`（空格 / 换行 / 65 字符 / 分号 / 空串）一律被换成自己生成的 uuid4
  - 脱敏递归进嵌套结构；自引用结构不会栈溢出
  - 两次请求拿到不同 id（上下文没串）
- **测试与审查抓到四个真 bug**：
  1. 未处理异常的响应由最外层 `ServerErrorMiddleware` 产出，**绕过** `RequestContextMiddleware`，`X-Request-ID` 响应头根本没设上 —— 而 500 恰恰最需要客户端报得出 id。改成在信封生成处设头
  2. 异常栈没过脱敏（PR #26 第一轮审查指出）—— 见上
  4. 递归脱敏到深度上限时 fail-open（PR #26 第二轮审查指出）—— 见上
  3. `Authorization: Bearer abc.def` 只 redact 掉 `Bearer`，token 原样留在后面（值匹配「取到空格为止」）。值那一组补了 `Bearer|Basic|Token|Digest` 分支

**教训：含反斜杠的内容不要经 shell heredoc**

写那两条正则时走了 `python - <<'PY'` 的 heredoc，`\1` 和 `\b` 在普通 Python 字符串里是**八进制转义与退格符**，落盘成了不可见的 `\x01` / `\x03` / `\x08`。后果不是报错，是 **`\x08` 藏在正则里让它永不匹配** —— 脱敏照跑、什么都没换掉，而且 `Edit` 工具按可见文本也匹配不上那几行。

这正是 [REVIEW-LOG](REVIEW-LOG.md)「跨层假设」那条记过的坑（原文：**含正则 / 转义 / 反斜杠的内容只走 Write / Edit 工具，不进 shell**），我没照做。两个派生做法已落进代码：

- 替换用 `lambda` 而不是 `\1` 模板串 —— 反向引用模板经过任何一层转义都会**静默失效**，lambda 没有这个失效模式
- 脱敏测试断言的是「`hunter2` 不在输出里**且** `***REDACTED***` 在输出里」。只断言前者的话，一个永不匹配的正则也能通过
- [WORKFLOW §7](WORKFLOW.md) 六项本地全过；打包冒烟在干净 venv 通过
- **未验证**：真实 uvicorn 进程下的日志输出（测试里是 TestClient，走不到 uvicorn 的 logger 接管路径）。等 T0.6 起容器时看。⚠️ T0.4 的打包冒烟在**独立进程**里跑时已经打出了合格的 JSON 行（`{"timestamp": ..., "level": "WARNING", "logger": "app.main", ...}`），所以「formatter 在真实进程里生效」这半条已验；剩下未验的只是 uvicorn 自己那三个 logger 的接管

### T0.4 任务记录（2026-09-12）

**做了什么**

- `app/models/base.py`：`Base` + **`Money` 注解类型**（`Numeric(20, 8)`，spec §80）+ `quantize_money()`。
  金额列一律用 `Money` 声明，不要每张表各写一遍 `Numeric(...)` —— 写散了就会有人写成 `Numeric(10, 2)`，**而那时候没有任何东西会报错**。`quantize_money()` 是 §80 那「恰好一次 `ROUND_HALF_UP`」的唯一实现：散在各处的话，迟早有一处漏写 `rounding=` 而落回 Python 默认的 `ROUND_HALF_EVEN`，差异只在 `.5` 那类值上出现，对账时才发现
- `app/core/database.py`：engine（`pool_pre_ping` + `pool_recycle=1800`）、`session_factory`、`session_scope()`、`check_database()`。
  **同步 SQLAlchemy，不是 async**：Celery worker 是同步的，两边共用一套仓储代码才不会写两遍；钱包变更要 `SELECT ... FOR UPDATE`（§81），同步写法里事务边界一眼看得出来。FastAPI 会把同步依赖丢进线程池
- `/readyz`（新）与 `/healthz`（既有）**分开**：存活探针刻意不依赖外部组件——MySQL 挂掉时它仍须应答，否则编排器会在数据库恢复期间反复重启容器，把一次可恢复的故障变成滚动重启；就绪探针才查依赖，不通返回 503
- **没配数据库不让进程崩**：`create_app()` 捕获 `DatabaseNotConfigured`，记一条 warning 后照常起。一个配置笔误不该让容器进重启循环，而日志里只有一行没人看得见的堆栈
- Alembic：`alembic.ini` + `alembic/env.py` + 基线迁移。连接串**不在 `alembic.ini` 里**，`env.py` 从 `app.core.config` 读；`compare_type=True`（列类型变更默认不被 autogenerate 检测，对一个金额必须是 `DECIMAL(20,8)` 的系统来说那正是最不能漏的一类）
- CI 的 `backend` job 加 MySQL service，并**显式把「有任何 skipped」判成失败**

**偏离了什么**

- **基线迁移故意不建任何表**。它的作用是让 `alembic upgrade head` 在空库上真的跑起来、建出 `alembic_version`，把 §123 验收里的「DB migrations execute」在 Phase 0 就验掉。业务表从 Phase 1 开始
- **驱动选 `pymysql` 而不是 `mysqlclient`**：后者要编译、镜像里得带编译链。计费的瓶颈在 IO 与锁，不在驱动的解析速度
- **连接池大小没做成配置项**：`pool_pre_ping` 与 `pool_recycle` 是 MySQL 必需的（默认 8 小时掐空闲连接），先硬编码；池大小要等 T0.10 有了并发基线才知道该设多少，现在开个旋钮只是猜
- **`alembic/` 不在 wheel 里**（`packages.find` 只含 `app*`）。迁移是从镜像里的源码树跑的，不是从安装包跑的 —— T0.6 建镜像时要记得把 `alembic/` 与 `alembic.ini` 一起 COPY 进去
- `Decimal` 的**跨进程一致性**（MySQL 端 `DECIMAL(20,8)` 与 Python `Decimal` 的往返）没有端到端用例，因为还没有任何表。Phase 1 建钱包表时必须补一条「写进去再读出来，值与小数位都不变」

**验证到什么程度**

- **迁移对着真 MySQL 8.4 跑通**（本地 Docker 一次性容器）：`upgrade head` 建出 `alembic_version`；`downgrade base` → `upgrade head` 可逆。**能升不能降的迁移链，出事时只能靠恢复备份**
- `pytest` **62 passed、0 skipped**（带 `BILLING_TEST_DATABASE_URL` 时）；不带时 60 passed **2 skipped**，跳的正是那两条迁移用例
- 就绪探针三种状态都有用例：未配置 → `DATABASE_NOT_CONFIGURED`、连不上 → `DATABASE_UNAVAILABLE`、正常 → 200；**且断言连接目标不出现在响应里**（连接错误里带主机名、端口，有时还有用户名，§94 不许外泄）
- 存活探针在数据库不通时仍返回 200，同一用例里断言 `/readyz` 同时是 503
- `session_scope()` 的提交 / 回滚 / 关闭三条路径都有用例。**漏掉 `rollback()` 的连接会带着脏事务回到连接池，下一个请求拿到它才报错，现场已经没了**
- `quantize_money()` 的 `.5` 边界与负数有用例，并显式断言它与 Python 默认的 `ROUND_HALF_EVEN` **结果不同**
- [WORKFLOW §7](WORKFLOW.md) 六项本地全过；打包冒烟在干净 venv 通过

**教训：第三方解析器读的配置文件要保持 ASCII**

`alembic.ini` 初版写了中文注释。Alembic 用 `configparser` 读它，而 `configparser` 用**平台默认编码** —— Windows 上是 cp1252，直接 `UnicodeDecodeError`；Linux 的 CI runner 是 UTF-8，跑得好好的。**这个 bug 只在开发机上炸、CI 看不见**，是最难发现的那一类。

已落成机械检查：`test_alembic_ini_is_ascii_only` 直接按 ASCII 解码那个文件，解不出来就失败。写文件时的判据是：**这个文件由谁解析？** 由 Python 解析的（`env.py`、迁移脚本）随便写中文；由第三方库按平台编码解析的，只写 ASCII，理由写到旁边的 `.py` 里去。

### T0.5 任务记录（2026-09-12）

**做了什么**

- `app/core/celery_app.py`：Celery 工厂。**每一条默认值都是从 spec §74.6 和 Invariant 14 推出来的，不是抄模板**：
  - **不存任务结果**（`task_ignore_result=True`、`result_backend=None`）。§74.6 写死了「Redis/Celery 只承载投递触发，数据库 Outbox 才是可恢复的事实来源」。结果放 Redis 会让人依赖它做判断，而 Redis 是可丢的 —— 任务的产出必须落库，落不了库就是这个任务没做完
  - `task_acks_late` + `task_reject_on_worker_lost`：worker 被 kill 时消息回队列重投，而不是「已 ack 但没做完」。**代价是任务必须幂等**，已写进 `app/tasks/__init__.py` 的硬规矩
  - `worker_prefetch_multiplier=1`：配合 acks_late。预取多了，一个 worker 崩掉会让一批消息同时重投，放大重复
  - **只收 JSON**：pickle 反序列化等于允许 broker 上的任意代码在 worker 里执行
  - UTC（§109）：beat 的调度时刻要和账本时间同一个基准
- `app/worker.py`：`celery -A app.worker worker / beat` 的入口。**worker 没有 broker 就直接起不来**，与 API「没数据库也要能起」相反 —— 这个不对称是有意的：API 要让存活探针应答，worker 没有 broker 则毫无意义
- `app/tasks/ping.py`：探活任务。Celery 配线错了（任务没注册、队列名不对、序列化器不匹配）**不会在启动时报错**，只会在第一个真任务被丢进队列后安静地不执行
- `/readyz` 加 `redis` 字段与 `degraded` 状态（见下）
- CI 的 `backend` job 加 Redis service

**偏离了什么 —— 一个需要论证的设计决定**

- **Redis 不通 = 200 `degraded`，不是 503。** 数据库不通才是 503。这个不对称是刻意的：
  - §74.6：Redis/Celery 只承载投递触发，数据库 Outbox 才是事实来源
  - `REQ-AVAIL-001`：Redis 不可用**不得**成为终端 AI 请求路径上的同步依赖
  - Redis 挂了，API 仍能收用量事件、落库、返回 202，投递触发欠着，等 Redis 回来由周期恢复补上（Invariant 14）
  - **把 Redis 做成就绪阻断项，等于 Redis 一挂就把所有实例摘出轮转 —— 那正是 Invariant 1 要防的中断，而且是我们自己造出来的**
  - 代价：Redis 静默不可用不会被负载均衡发现，**必须由监控告警兜住**。审查（PR #28）判这条为阻断项 —— 我自己选了 200，就必须自己把补偿控制补上。本任务能给的都给了：
    - `/readyz` 判定 degraded 时打一条 **稳定契约**的 WARNING（`message="Readiness degraded"`、`component="redis"`），并有用例钉住「degraded 时恰好一条」「正常时一条都没有」
    - 新建 [runbook.md](runbook.md) 的第一个场景「Redis / Celery broker 不可用」，含告警名、条件、分级、升级路径与「绝不能做什么」
    - T0.9 的任务描述里钉上了这条具体告警
    - **告警设施本身建不了**：还没有任何部署、没有日志聚合。那是 T0.9 的前置，属排序事实，不是取舍
- **beat 的 schedule 留空**，不塞示例条目 —— 示例条目会被真的跑起来
- **任务模块显式列出**（`TASK_MODULES`），不用 `autodiscover_tasks`：后者靠约定扫包，改了包名时只是**安静地少注册一个任务**，调用方拿到 `NotRegistered` 才发现
- **没做 worker 的存活探针**：那要容器编排配合，是 T0.6 的事

**验证到什么程度**

- **起了真 worker 端到端跑通**（本地 Docker 一次性 Redis）：`celery inspect ping` → `pong`；`inspect registered` → `app.tasks.ping`；派发任务 → worker `received` → `succeeded`，返回 UTC 时间戳
- `pytest` **75 passed、0 skipped**（带 `BILLING_TEST_DATABASE_URL` + `BILLING_TEST_REDIS_URL` 时）
- 配置断言逐条钉住：不存结果、acks_late 三件套、只收 JSON、UTC、beat 空、任务模块显式
- 对着真 Redis 验了**入队**（eager 模式跑不到的那一半：序列化、连接、入队）
- `/readyz` 的 Redis 三态都有用例：未配置 / 连不上 / 正常；且断言连接目标不出现在响应里
- [WORKFLOW §7](WORKFLOW.md) 六项本地全过

**实测发现的两个坑（都只有起真 worker 才看得见）**

1. **Celery 默认劫持 root logger**（`worker_hijack_root_logger`），把 T0.3 装的 JSON handler 顶掉。后果不是报错，是 **worker 打纯文本、API 打 JSON**，集中日志里两半对不上 —— 而且 worker 那半**完全不过脱敏**。已关掉，连同 `worker_redirect_stdouts`
2. **Celery 会把任务的 `args` / `kwargs` 写进日志**（`"Task ... received"` 那条的 extra 里）。而 `args` 这个键名不敏感，**只按键名脱敏兜不住**。两道补救：
   - `redact()` 现在对**字符串值**也跑一遍文本脱敏，不只按键名
   - `app/tasks/__init__.py` 写死硬规矩：**任务参数只传标识符，不传值** —— 密钥、token、AI prompt / response、请求体一律在任务里按 id 去库里取
   - 端到端验过：派发 `args=["api_secret=s3cr3t-must-not-appear"]`，该串在 worker 日志里出现 **0 次**，「received」行与异常栈两处都是 `***REDACTED***`

---

### T0.6 任务记录（2026-09-12）

**做了什么**

- `Dockerfile`：api / celery-worker / celery-beat **共用一个镜像**，只换 `command`。分成三个镜像只会让「worker 和 api 版本不一致」变成可能
  - 两阶段：build 造 wheel，runtime 只装 wheel，构建期工具不留在运行镜像里
  - **构建期就地跑一次 `scripts/packaging_smoke.py`**，验「装进 site-packages 的那一份」能起 `/healthz` —— 这一类缺陷（PR #22 的 wheel 少打子包）只有在安装后的环境里才看得见
  - `alembic/` 与 `alembic.ini` **单独 COPY**（wheel 只打 `app*`）。这是 T0.5 记下的欠账，现在由用例钉住
  - WORKDIR 用 `/srv/billing` 而不是 `/app`：cwd 会进 `sys.path`，一个叫 `app` 的目录在那里会让 `import app` 的解析变得要靠运气
- `docker-compose.yml`：六个服务 + 三个命名卷。MySQL 显式 `--default-time-zone=+00:00`（§109 要求一律 UTC 存储，默认的 `SYSTEM` 会让「换一台宿主机」变成一次静默的数据语义变更）
- `deploy/nginx/`：边缘反向代理。转发头单独成 `.inc` 文件被三处 `location` include —— 各抄一遍早晚有一处少 `X-Request-ID`，而少了它那条链路在日志里就断了
- `tests/backend/test_compose.py`：10 条不变量用例
- CI 的 `backend` job 加 `Compose stack` 一步：`docker compose config` + `docker compose build api`

**几个不是随手选的默认**

- **api 的容器健康检查探 `/healthz`，不探 `/readyz`。** `/readyz` 在数据库不可用时返回 503，拿它做健康检查会把一次**可恢复**的数据库故障放大成 API 的滚动重启 —— 正是 `app/api/health.py` 那段注释要防的事。用例钉住
- **`depends_on` 只用启动顺序，不用 `service_healthy`。** API 被设计成依赖不可用时照样启动、由 `/readyz` 如实汇报；让它等 MySQL 健康，等于 MySQL 坏了连 `/healthz` 都拿不到，排障时手上空空
- **迁移不是启动副作用**，是显式的 `docker compose run --rm api alembic upgrade head`。§100 要求 API 可水平扩展，多实例同时启动就会同时迁移同一个库；§98 要求迁移有明确顺序与回滚策略。用例钉住 `Dockerfile` 与所有 `command` 里都没有 `alembic upgrade`
- **只发布 nginx 一个端口。** 公开仓库 + 单 VPS，发布 3306 等于把数据库摆到公网
- **密码空着就起不来**（`${VAR:?...}`）。实测：不设时 compose 直接报错并指名该设哪个变量，而不是悄悄起一个空密码的数据库
- **compose 里没有 `env_file: .env`。** 宿主机 `.env` 里的 `BILLING_DATABASE_URL` 指向 `127.0.0.1`（给直接跑 uvicorn 用），整份灌进容器会让容器连回自己
- **nginx 访问日志记 `$uri` 而不是 `$request`**：`$request` 带查询串，查询串里可能有密钥（§94）
- **`/readyz` 只对私有网段开放**：响应体逐个报出依赖状态，等于公开内部拓扑与故障窗口。⚠️ 这条控制的前提是 nginx 直接面对客户端 —— 前面再放一层代理，`$remote_addr` 全变成那层代理的私有地址，限制就形同虚设（已写进配置注释，归 T0.9）
- **容器运行 UID = `10001`**，[ADR-0004](adr/ADR-0004-credential-encryption.md) 要求 Phase 0 定的就是它。宿主机主密钥文件要按它设属主 —— 那一半要有生产主机才做得了，归 T0.9

**偏离了什么**

- **spec §99 的服务清单是七个，这里只有六个：`frontend` 不存在**（T0.7 才建 React 骨架）。给一个构建不出来的 frontend 占位会让 `up` 直接失败、整个栈验证不了；在 `location /` 放注释掉的 `proxy_pass` 更糟 —— 注释掉的配置看起来像「已经有了」。现在 `/` 返回一句说明用的 503，欠账**钉在 T0.7 的任务描述里**，且 `EXPECTED_SERVICES` 写死六个，T0.7 必须一并改
- **nginx 以官方镜像默认方式运行**（master 是 root、worker 是 `nginx` 用户），没有换成非 root 镜像。它要绑 80，换镜像属于生产加固，归 T0.9
- **没起整个栈进 CI**：慢且不稳。CI 只做「配置可解析 + 镜像能构建」，不变量交给用例
- **没加 §93 的文档存储卷**：那个卷要等有代码用它的时候再加，现在加就是占位

**验证到什么程度**

- **真起了整套栈**（本机 Docker 29.7.2，Linux 容器）：六个服务全 `Up`，四个有健康检查的全部 `healthy`
- 端到端经 nginx：`/healthz` → 200 信封；`/readyz` → `{"status":"ok","database":"ok","redis":"ok"}`；`/` → 503 占位；`/nginx-health` → 200
- **关联 ID 跨进程对上了**：nginx 在客户端没带时生成 `$request_id`，同一个值出现在 nginx 访问日志、应用 `Request completed` 日志和响应头里
- **顺带验到一件事**：故意在 URL 里塞 `?api_secret=must-not-appear-in-logs` —— nginx 日志因为记 `$uri` 完全没有查询串；`uvicorn.access` **确实**带查询串，被 T0.3 的 `scrub_text()` 拦成 `api_secret=***REDACTED***`。两道都成立
- **密钥扫描**：两个密码与那个假密钥在六个服务的日志里各出现 **0 次**
- `docker compose exec` 实测 api / celery-worker / celery-beat 三者都是 `uid=10001(app)`
- **迁移在容器里跑通**：`alembic upgrade head` → MySQL 的 `alembic_version` 落 `0001_baseline`，`alembic current` 确认 —— 证明 `alembic/` 确实进了镜像
- **worker 端到端**：`inspect ping` → pong、`inspect registered` → `app.tasks.ping`、派发 → `received` → `succeeded`；worker 日志是 JSON（在容器里也确认了 T0.5 关掉 root logger 劫持这一条生效）
- **beat 以非 root 写出了调度文件**：`/var/lib/celery/beat-schedule` 属主 `app:app`
- `pytest` **87 passed、0 skipped**（带 `BILLING_TEST_DATABASE_URL` + `BILLING_TEST_REDIS_URL`）；[WORKFLOW §7](WORKFLOW.md) 六项本地全过

**教训：所有基于文本的断言，默认都会匹配到注释里**

写完 10 条用例后做了一轮变异测试（12 种改法，每种都该让测试变红）。两条**存活**了：把 `COPY alembic ./alembic` 和 `proxy_set_header X-Request-ID` 各注释掉，测试照样绿 —— 因为 `"COPY alembic" in text` 在注释行上同样为真。

这是「测试通过但东西是坏的」那一类，和 [REVIEW-LOG](REVIEW-LOG.md) 里正则永不匹配那次是同一个病根：**断言的范围比它自以为的大**。修法是加一个 `instructions()` 只保留非注释行，所有文本断言都走它；补完 12 条变异全部被抓到。

派生的通用规矩：**校验配置文件内容的用例，必须先剥掉注释再断言**，并且要用「把那行注释掉」这一种变异验证过。

---

### T0.7 任务记录（2026-09-12）

**做了什么**

- `frontend/`：spec §3.2 钉死的那一套 —— React 19 + TypeScript + Vite + React Router 7 + TanStack Query 5 + Axios + Ant Design 6，i18n-ready（V1 只出英文）
- 目录照 §101 分（`api` / `layouts` / `routes` / `i18n` / `features`）。**`features/` 下的九个业务子目录不预建** —— 空目录会让人以为那块已经开工，和 runbook 不预留空标题是同一条道理
- `frontend/Dockerfile` + `frontend/nginx.conf`：node 构建 → nginx 发静态文件，运行期没有 node
- compose 补上第七个服务，边缘 nginx 的 `location /` 从 T0.6 的 503 占位改成真转发；`EXPECTED_SERVICES` 同步，并**单独加一条用例**钉住「根路径真的转发到 frontend」—— 光加服务不改边缘是一个能跑通其他所有用例的错误状态
- CI 加 `frontend` job（lint / typecheck / test / build），`Compose stack` 那步改成 `build api frontend`

**三条「失败方式是安静的」，都做成了机械保证**

1. **文案硬编码**：自定义 ESLint 规则 `no-hardcoded-jsx-text`，管 JSX 文本、表达式容器里的字符串、以及 `title` / `label` / `placeholder` / `alt` 这类面向用户的属性；纯空白与标点放行。规则自己有 14 个断言（8 个应放行、6 个应报错）—— **一条永不触发的 lint 规则和没有规则完全一样，而且看起来更让人放心**
2. **i18n key 不存在**：i18next 默认**把 key 原样渲染给用户**，像一句奇怪的英文，不报错不告警。`keys.test.ts` 校验源码用到的每个 key 都在 `en.json` 里、且没有没人用的死 key。另外配了 `parseMissingKeyHandler` 让漏网的显示成 `⟪ missing:key ⟫`
3. **信封没解开**：§107 的成功与失败共用一种形状，漏看 `success` 就会把 `data: null` 当正常值渲染 —— 界面一片空白而不报错。解信封只存在 `api/client.ts` 一处，**不认识的形状当成错误而不是空数据**（代理层出问题时回的是 HTML）

**几个不是随手选的决定**

- **`keySeparator` 与 `nsSeparator` 都关掉，key 一律扁平字符串。** 默认开着时 `"wallet.balance"` 会被拆成嵌套查找，于是「key 不存在」和「key 的父节点是个字符串」两种情况**失败方式一模一样**，都是安静地渲染 key 本身
- **axios 的 `baseURL` 是 `/` 而不是 `/api`。** nginx 转发刻意不改写路径（T0.6），所以浏览器请求的 uri、nginx 日志里的 uri、应用日志里的 path 是**同一个字符串**；在这里偷偷加前缀，三处就对不上
- **浏览器端生成 `X-Request-ID`**，而不是让 nginx 兜底生成 —— 兜底那个前端自己不知道，用户截图里就没有可搜的 id。格式落在后端 `_SAFE_REQUEST_ID` 的字符集内
- **错误界面必须显示 `request_id`**，那是用户能报给支持、支持能在日志里搜到的唯一钥匙（§94 的关联链条到这里才闭合）
- **`RequestReference` 的参数类型是 `Error` 而不是 `ApiError`**，运行时用 `instanceof` 收窄：TanStack Query 把 `error` 标成 `Error`，queryFn 里一个普通 TypeError 也会走到这里。写成 `ApiError` 是在骗类型系统，真出事时读到 `undefined`
- **`retry: 1` 而不是默认的 3**；写操作的重试策略要在引入它的那个任务里单独定，不许靠这里的默认值
- **`sourcemap: false`**：源码映射会把完整前端逻辑摆到公网上，排障靠 `request_id` 关联服务端日志
- **index.html 不缓存、带哈希的产物永久缓存。** 反过来做的话，用户拿着缓存的旧 index.html 去要一个已经不存在的 bundle，结果是白屏，而且强刷之前一直白着
- **用 antd 的 `<App>` 包一层、组件里走 `App.useApp()`**，不用 `message.x()` 这类静态方法 —— 静态方法拿不到 `ConfigProvider` 的上下文，顺带也就不需要 React 19 的兼容补丁包

**版本选型：不按数字最新，按「这套能不能真跑通」**

初装拿到的是 eslint 9（已标停止支持）、vite 6、vitest 2，都落后一个大版本。查了一轮主线版本后实测组装，结论有两条是**查出来的不是猜的**：

- `typescript-eslint@8` 的 peer 是 `typescript >=4.8.4 <6.1.0`，**TypeScript 7 还不能用**，所以留在 5.9
- `eslint-plugin-react@7.37.5` 的 peer 只到 eslint 9.7，与 eslint 10 互斥。**选择去掉这个插件**：与其为一条规则把整个 lint 链钉死在一个不再收安全修复的大版本上，不如自己维护那 60 行规则 —— 何况它是本项目的硬要求，本来就该本项目负责

最终：eslint 10 / vite 8 / vitest 5 / antd 6 / React 19 / TS 5.9，装完零弃用告警。

**验证到什么程度**

- **七个服务实测起来**，五个有健康检查的全部 `healthy`
- **真浏览器**（Chrome）打开 `http://127.0.0.1:8080/`：渲染出布局 + 平台状态卡片，**成功态**显示「The billing platform is responding.」—— 证明 浏览器 → nginx → api → §107 信封 → TanStack Query → 界面 整条链通了
- **关联 ID 闭合**：浏览器生成的 UUID（带连字符）与 nginx 兜底生成的 32 位十六进制**能在日志里区分开**，实测浏览器那次的同一个 id 出现在 nginx 访问日志、应用 `Request completed` 日志两处
- **SPA 兜底**：`/nope/deep/path` 返回 200 + index.html，前端路由匹配到 `*` 渲染 404 页
- **缓存策略实测**：`/` 是 `no-cache, must-revalidate`，`/assets/index-*.js` 是 `public, max-age=31536000, immutable`
- **三态齐全**（DoD 第 8 条）：停掉 api 看到**加载态**（骨架条 + Checking…）；把查询推进错误态看到**错误态** —— 后端安全文案 + `Reference: <request_id>`（可复制）+ 「Try again」按钮
- `npm run lint` / `typecheck` / `test`（11 passed）/ `build` 全过；后端 `pytest` **88 passed、0 skipped**；[WORKFLOW §7](WORKFLOW.md) 六项本地全过
- **`check_docs.py` 顺带修了一个被 `frontend/` 暴露出来的缺口**：它遍历全仓库的 Markdown，把 `node_modules` 里第三方包 README 的相对链接报成了 156 条死链。加了 `VENDOR_DIRS` 跳过依赖与构建产物。**刻意不改成「只查 git 跟踪的文件」**——那样一个刚写好、还没 `git add` 的新文档会被静默跳过，而这个脚本的全部价值就在于不静默。改完做了变异验证：仓库自己的死链接、`docs/` 下的死链接、不存在的 `§N` 引用，三条都照样被抓到

**合并后的那一步（[WORKFLOW §2](WORKFLOW.md) 的受控例外）—— 已执行，2026-09-12**

把新的 `frontend` job 设为 `main` 的必需状态检查。**这一步 PR 里做不到**：新增的 CI job 必须先合进 `main` 才存在，顺序上只能后做。

- 走了 `POST /repos/{owner}/{repo}/branches/main/protection/required_status_checks/contexts` 这个**纯追加**端点，而不是 `PUT` 整份 protection —— 后者要重发全部字段，漏一个就是静默降级，而在保护 `main` 的配置上静默降级不会报错（与 T0.2 加 `backend` 时同一个做法）
- 回读对照：**前** `docs` / `secret-scan` / `scripts` / `policy` / `backend`（五项）→ **后** 同样五项 + `frontend`（六项）
- 其余保护项逐项回读，**一项未变**：`strict=true`、`enforce_admins=true`、线性历史、禁 force push、禁删除、对话必须解决、PR 审查照旧 —— 确认纯追加端点只做了这一件事
- 执行结果由 `chore/frontend-required-check` 这个只改状态陈述的 PR 补回仓库

**教训：先分清「被测系统坏了」和「测量环境坏了」**

停掉 api 之后，界面**永远停在加载态**，重试一次都不发。我一度判定这是真缺陷。查下来不是：

TanStack Query 的 retryer 源码里写着

```js
const canContinue = () => focusManager.isFocused() && (...) && config.canRun();
sleep(delay).then(() => canContinue() ? undefined : pause())
```

**窗口失焦时重试被无限期挂起**，查询状态停在 `fetchStatus: "paused"`。而自动化浏览器的标签页永远是 `hidden`、永远拿不到焦点。真实用户窗口有焦点，重试正常走完。

值得记的不是这条 API 细节，而是排查顺序上的两次浪费：我先后猜了「计时器节流」和「onlineManager 判定离线」，两条都有表面证据（标签页确实 hidden、确实测到 setTimeout 被拖长），**也都是错的**。真正定位靠的是两步：① 写一个一次性复现脚本，证明 `apiGet` 对着 502 确实会 reject —— 把「我的代码」从嫌疑里排除掉；② 直接读 `node_modules` 里库的源码，而不是继续猜它的行为。

派生规矩：**在自动化浏览器里观察到的「卡住」，先证明它在真实环境也卡住，再当缺陷处理**；以及**依赖库的行为有源码可读时不要猜**。

---

### T0.8 设计闸门记录（2026-09-12，Issue [#32](https://github.com/kelvinpang90/ai_billing_hub/issues/32)）

**判定：`APPROVED: design v5`。走了五轮，前四轮都是 `REQUEST_CHANGES`，四条阻断项全部成立。**

⚠️ **分档表里没有「平台认证」这一行 —— 这是表本身的缺口，待 Kelvin 拍板。**
按字面读，[WORKFLOW §3](WORKFLOW.md) 第二行的「集成认证」指的是应用后端的 HMAC 凭据（`REQ-AUTH-001`、§36–§37、§74.4），**不是**管理员登录；那样 T0.8 落进第四行「不走」。但管理员认证是钱包调整、定价发布、退款的**唯一前门**，而闸门 §6 要管的密钥存储、鉴权主体、日志脱敏条条正中本任务。**我按更严的一档走了**，没有等拍板。建议把第二行改成「用量摄取 / **认证与会话**（集成侧与平台侧）/ Webhook」，结论记进 [REVIEW-LOG](REVIEW-LOG.md) —— 那是单独一个 PR。

**四条阻断项，每一条都是真的设计缺陷**

| 轮次 | 阻断项 | 为什么成立 |
| --- | --- | --- |
| 1 | 把 §53 明确要求的「忘记密码 / 重置密码」列进「不做」 | 我的理由（邮件投递能力不存在）是**事实**，但它只能决定**排序**，不能决定**范围**。把一条 spec 硬性要求写进「不做」而不给落地路径，读起来就像这个要求被免掉了 |
| 1 | TOTP 防重放只有应用层比较，没有数据库并发控制 | **真 bug**。我在刷新令牌那里写了条件更新，还专门写了「不靠先读后写 —— 那是经典 TOCTOU」，**然后在 TOTP 和恢复码上原样犯了这个错** |
| 2 | 没规定完整认证成功后清零失败计数 | 历史失败会永久累积，「连续失败才锁定」根本不成立 |
| 3 | 2FA 的 `confirm` / `enrol` 没有数据库级串行化 | 并发两个 `confirm` 各生成 10 个恢复码 = 20 个有效码；`enrol` 与 `confirm` 交错还能确认一个**从未被验证过**的密钥 |
| 4 | 只有按账号的锁定，没有按来源的限流（§53 `Login attempt rate limiting`） | 见下 |

**两个值得单独记的发现**

1. **同一类错误在三轮里换了三个地方出现**（v1 漏 TOTP 与恢复码、v3 漏状态置位本身）。每次我修的都是「被指出的那一处」，下一处照旧。v4 因此改了做法：**先写规则，再列出规则管辖的全部位置**，让「哪里需要条件更新」变成一张能数清的表，而不是每处各想一遍。
2. **第四轮那个洞是我自己造的。** v1 为防时序泄露规定「用户不存在时也跑一次假的 Argon2 verify」—— 于是任意不存在的邮箱都能稳定消耗一次 Argon2，换着邮箱发就是一条 CPU 放大通道，而账号锁定永远不会触发。**一个为了堵信息泄露加的控制，变成了一条资源放大通道。**这类「防御自身成为攻击面」的二阶效应，我在前四版里一次都没有主动检查过。
3. 自查还发现 §53 的 `Password strength requirements` 在 v4 里只有「密码强度」四个字、**没有定义任何规则** —— 那等于没设计。v5 补了 §53/§54 **逐项覆盖表**，让遗漏可数，而不是靠审查方替我数。

⚠️ **`scripts/gh_verified_write.py` 的 `--kind` 里没有 `issue-body`**，只支持 PR 正文与评论。而设计闸门的正文住在 Issue 里，所以本轮的回读比对是手工做的（`gh issue view --json body` 逐字对比，五次全部一致）。补这个 kind 是一个独立的小任务。

---

### T0.8a 任务记录（2026-09-12）

**做了什么**

- `users` / `refresh_tokens` / `audit_logs` 三张表 + 迁移（仓库第一次建真实业务表，见下）
- `app/core/passwords.py`：Argon2id + 强度规则（NIST SP 800-63B：长度下限 12 / 上限 128、**不做组成规则**、拒常见口令与邮箱同名口令）
- `app/core/tokens.py`：JWT 访问令牌（10 分钟）+ 不透明刷新令牌（SHA-256 存储）
- `app/core/ratelimit.py` + nginx `limit_req`：按来源限流，三层
- `app/services/auth.py`：登录、刷新轮换与重放检测、登出、锁定、审计
- `app/api/auth.py`：`/api/v1/auth/{login,refresh,logout}`
- `app/cli.py`：`python -m app.cli create-admin` —— **管理员只能这样创建，没有自助注册端点**
- `app/core/emails.py`：邮箱归一化，登录与 CLI 共用

**几个不是随手选的决定**

- **两种令牌两套机制。** 访问令牌是 JWT（验签不查库，每请求便宜，代价是签发后收不回 → 寿命 10 分钟）；刷新令牌是不透明串落库（能立刻吊销，代价是每次刷新一次查库，而刷新本就不频繁）。**「吊销后最多 10 分钟旧访问令牌仍可用」是明确接受的取舍**，不是没想到
- **刷新令牌用 SHA-256 存，密码用 Argon2id。** 不是不一致：刷新令牌是我们生成的 256 位随机串，无字典可猜，而**查表必须靠等值索引**——Argon2 带盐，同一个令牌两次哈希结果不同，根本查不出来。密码相反，那是人选的，必须慢
- **锁定计数落库，不落 Redis。** Redis 一重启计数全清零，那是又一个 fail-open 的安全控制；而管理员登录量极小，DB 写不是瓶颈
- **限流的进程内那层用内存，也不用 Redis 或数据库。** Redis 丢失会 fail-open；数据库是把 CPU 放大换成写放大；进程内的失效模式是**退化到 nginx 那层**，不是安全控制消失
- **邮箱不存在、密码错误、账号被锁定三者同码同文案。** 锁定**刻意不返回 423**：那个状态码等于告诉对方「这个邮箱存在，而且正在被爆破」
- **失败计数单独提交。** 与请求其余部分共用一个会被回滚的事务时，一次数据库错误就把爆破计数清零了
- **审计表不加 `actor_user_id` 外键。** §66 要求审计长期保留，外键会让「删用户」变成「要么级联删掉他的审计记录、要么删不掉用户」—— 两种都不对，**审计必须比它记录的对象活得更久**

**实测发现的三个坑**

1. **naive UTC 被当成本机时区。** 数据库列按 §109 存 naive UTC，而 `datetime.timestamp()` 对 naive 值的解释是**本机时区**。本机是 UTC+8，于是签出的令牌 `exp` 落在 8 小时前 —— **一签出就是过期的**。最坏的是它**在 UTC 的服务器上完全正常**，开发机与生产机时区不同时只在一边出现，而现象（「用户随机掉登录」）指不回原因。已加 `_epoch()` 与一条回归用例
2. **`BigInteger` 主键在 SQLite 上不自增。** SQLite 只对 `INTEGER PRIMARY KEY` 做自增，BIGINT 主键插入直接撞 NOT NULL。用 `with_variant(Integer, "sqlite")`：生产 MySQL 拿 BIGINT，单元测试拿 INTEGER，**不用在测试里写特例**
3. **内存 SQLite 每连接一个独立的库。** TestClient 在线程池里跑同步端点，换线程就换连接，于是建好的表在请求里「不存在」。报错是 `no such table: users`，看起来像迁移没跑。要 `StaticPool` + `check_same_thread=False`

**迁移：spec §132 第 13 条分析**（T0.4 写的「业务表从 Phase 1 开始」被本任务打破，所以这条落在这里）

- **锁表**：三张全新建，空库上瞬时完成，不锁任何既有表。**这是本项目在锁表上唯一轻松的一次**
- **回滚**：`downgrade` 直接 DROP。⚠️ 一旦有真实管理员账号，它就是数据丢失，且审计记录按 §66 不可重建 —— **生产上不 downgrade 这一版，往前修**
- ⚠️ **回退实测失败过一次，留下了坏状态**：`DROP INDEX ix_refresh_tokens_family` 报错（MySQL 不允许删掉外键依赖的索引），结果 `audit_logs` 已删而 `alembic_version` 仍停在本版 —— **schema 与版本号对不上，再 upgrade 也补不回来**。根因是 `drop_index` 本来就多余（`DROP TABLE` 会一并删索引）。已删掉那两行，并**把迁移里那段写错的失败分析改对**：MySQL 的 DDL 不参与事务，「三条 CREATE TABLE 要么全成要么全不成」是不成立的

**验证到什么程度**

- `pytest` **150 passed、0 skipped**（带真 MySQL + Redis）
- **对着真 MySQL 验迁移往返**：`upgrade head` → `downgrade base`（干净剩 `alembic_version`）→ 再 `upgrade head`
- **整栈端到端**（七服务）：经 nginx 登录 → 200 + httpOnly/SameSite=strict/Path 收窄的 cookie；刷新 → 轮换出新令牌；**重放旧令牌 → 401 `TOKEN_REUSED` 且整个家族被吊销**（新令牌随之失效）
- **nginx 限流实测**：连发 30 次，第 19 次起返回 429（burst 20 用尽）
- **密钥泄漏扫描**：密码、Argon2 哈希、两个刷新令牌在 nginx / api / worker / mysql 四个服务日志里各出现 **0 次**
- **CLI 实测**：弱口令被拒（exit 2）、大小写邮箱归一化、重复账号被拒（exit 1）
- **16 条变异全部被抓到**（见下）

**教训：变异测试抓出了 5 条「看起来在测、其实没测」的用例**

第一轮变异有 5 条存活，每条都是真缺口：

| 存活的变异 | 用例为什么没抓到 |
| --- | --- |
| 锁到期后不清零计数 | 用例断言的是「**成功登录后**计数为 0」，而成功路径本来就会清零 —— 它没测到它声称测的东西 |
| 条件更新退回先读后写 | **根本没有并发用例**。重放那条走的是早期的 `used_at` 判断，绕过了条件更新 |
| 用户不存在时不跑假校验 | 没有任何断言盯着这条控制 |
| 密码下限放宽到 1 | 用例写的是 `MIN_PASSWORD_LENGTH - 1`，**跟着常量一起变** |
| 锁定计数与主事务共用 | 没有用例区分「两个事务」与「一个事务」 |

派生规矩两条：**边界值要有绝对断言**（`assert MIN_PASSWORD_LENGTH >= 12`），相对断言只能证明「边界两侧行为不同」，证不了边界在合理位置；**并发语义必须对着真数据库验** —— 内存 SQLite 要么每连接一个库、要么（StaticPool）所有会话共用一条连接，后者意味着两个「并发」事务其实是同一个事务，模拟不出竞态。第一版并发用例就是这么写的，两边都被判成重放，而那个失败与被测代码无关。

---

### T0.8b 任务记录（2026-09-12）

**做了什么**

- `app/core/crypto.py`：ADR-0004 的信封加密。一条 secret 一把 DEK，DEK 再用主密钥包裹 —— **这正是「换主密钥只需重包 DEK、不碰密文」的原因**，否则轮换要把整张表解密再加密一遍
- `two_factor_settings` / `recovery_codes` 两张表 + 迁移 `0003`
- `app/services/two_factor.py`：注册 / 确认 / 校验 / 重新生成恢复码
- 登录改成两步：`/login` 只发 `pending_token`，`/login/totp` 才发会话
- `/api/v1/auth/2fa/{enrol,confirm,recovery-codes}` 三个端点
- `require_current_user()`：仓库第一个 Bearer 鉴权依赖，主体只从已验签令牌的 `sub` 取

**几个不是随手选的决定**

- **注册端点收 `pending_token` 而不是访问令牌。** ADMIN 的 2FA 是强制的，所以新管理员**第一次登录时还没有访问令牌** —— 他停在 `ENROL_2FA`，手上只有 pending 令牌。注册必须能用它走完，否则新管理员永远进不来
- **`PENDING`（没扫码确认）一律算未启用。** 算成已启用的话，「生成了密钥但没确认」会把人锁在门外 —— 既进不去，也没法重新注册
- **TOTP 密钥可还原、恢复码单向哈希。** 不是不一致：校验 TOTP 需要密钥原文（spec 第 1542 行写死了这一点），而校验恢复码只需要比对
- **钥匙串能同时持有多把主密钥。** 只留一把的话，换密钥那一刻所有历史 TOTP 注册立刻读不出来
- **重新生成恢复码要重新验密码。** 光有访问令牌不够 —— 恢复码等价于第二因子，一张被偷的令牌就能换出十个新的，那等于把 2FA 绕过了
- **失败计数的清零点移到了 `_complete_login()`**（令牌真的发出去那一刻）。留在密码那一步的话，知道密码但不知道验证码的人可以**无限次猜 TOTP**，每猜一次都顺手把计数清掉
- **TOTP 失败与密码失败共用同一个计数器。** 分开计数只会多一个能被分别耗尽的额度
- **错误的验证码与用过的恢复码同码同文案。** 区分它们只对攻击者有价值

**四处「只能发生一次」，全部靠条件更新**（设计闸门在这一块挡了两轮）

| 只能发生一次的事 | 条件 |
| --- | --- |
| 一个 TOTP 码只能用一次 | `last_used_counter IS NULL OR last_used_counter < :counter` |
| 一个恢复码只能用一次 | `used_at IS NULL AND revoked_at IS NULL` |
| 一份注册只能确认一次 | `confirmed_at IS NULL AND secret_version = :version` |
| 已确认的注册不能被覆盖 | `confirmed_at IS NULL` |

`secret_version` 是为第三条专门加的列：校验之后、置位之前若有人重新 `enrol` 换了密钥，确认必须落空 —— 否则会把一个**从未被验证过的密钥**标成已确认，用户的验证器从此对不上。

**验证到什么程度**

- `pytest` **215 passed、0 skipped**（带真 MySQL + Redis）
- **三条并发用例跑在真 MySQL 上**：并发 `confirm` 只生成一批恢复码（不是两批 20 个）、同一个 TOTP 码只过一次、同一个恢复码只消费一次
- **整栈九步实测**（经 nginx）：ADMIN 密码对 → `stage=ENROL_2FA` 且不发访问令牌 → 注册拿密钥 → 确认拿 10 个恢复码 → 重新登录停在 `TOTP_REQUIRED` → 输码登进来 → **同码再用被拒** → 恢复码登录（剩 9）→ **同恢复码再用被拒** → **pending 令牌冒充访问令牌被拒**
- 迁移对真 MySQL 跑通，三个枚举列确认是 `varchar(64)`
- **密钥泄漏扫描**：`otpauth://`、密码、恢复码在四个服务日志里各 **0 次**

**教训：SQLite 对「列宽」这一类缺陷是结构性失明的**

整栈实测时 `/2fa/confirm` 报 500，真 MySQL 说 `Data too long for column 'action'`。

根因：`Enum(native_enum=False)` 生成的 VARCHAR 宽度按**建表那一刻最长的成员**算。`0002` 建 `action` 列时最长是 `LOGIN_FAILED`（12 字符），而本任务新增了 `RECOVERY_CODES_REGENERATED`（26 字符）。

**而 208 条单元测试全绿** —— SQLite 根本不强制 VARCHAR 长度。这不是「用例写少了」，是跑在 SQLite 上的用例对这一类缺陷**看不见**。

两处修：① 三个枚举列的宽度写死成 `_ENUM_LENGTH = 64`，以后加枚举值不必再配 ALTER；② 新增 `tests/backend/test_model_columns.py` —— 它不测行为，直接测**列的形状**（宽度装不装得下所有成员），所以在 SQLite 上照样有效。变异验证过：把宽度调窄，用例立刻变红。

派生规矩：**凡是「数据库会拒绝、而 SQLite 会接受」的约束（列宽、字符集、严格模式），都要有一条直接断言 schema 形状的用例**，不能指望行为用例覆盖到。

**另外两个小坑**

- **同一个 30 秒窗口里的 TOTP 码只能用一次**（防重放，刻意的），所以测试里「连续登录两次」的辅助函数第二次起要改用恢复码 —— 把时钟往前推会让 JWT 的 `iat` 落在未来被 PyJWT 拒掉（T0.8a 踩过）
- **并发用例第二次踩了内存 SQLite 的坑**：`StaticPool` 是单连接共享，两个「并发」会话其实在同一个事务里。已挪到真 MySQL 的独立库


### T0.8e 任务记录（2026-09-12）

**做了什么**

- `app/core/config.py`：新增 `pending_token_ttl_seconds`（120）与 `enrolment_pending_token_ttl_seconds`（600），**两个都带 `gt=0`**
- `app/core/tokens.py`：`issue_pending_2fa_token` 的 `ttl_seconds` **去掉默认值**
- `app/services/auth.py`：两个签发点各自显式传对应的配置值
- `.env.example`：同步两个新配置项

**起因**：T0.8c 整栈实测时，新管理员首次登录走到最后一步吃了 401。从 api 日志还原：
`/login` 07:36:43 → `/2fa/confirm` 07:38:36（**t+113 秒，紧贴 120 秒上限**）→
`/login/totp` 07:39:12（t+149 秒）401。那一段要求用户扫码 + **抄下 10 个恢复码** + 输验证码，
而上面那次还是脚本化操作、恢复码是复制而非手抄。真人必然超时。

超时的后果不只是重来一次：后端回的是「令牌无效或已过期」，而用户此刻正盯着验证码
输入框 —— **现象指向验证码，原因在两步之前**。

**三个不是随手选的决定**

- **只放宽注册那一段。** 日常登录（掏手机输 6 位数）保持 120 秒。一起延长没有任何收益，
  却把「密码已验、第二因子未验」的窗口整体拉长 5 倍
- **`ttl_seconds` 去掉默认值。** 一旦出现两种寿命，默认值就成了静默的错误来源：将来第三个
  签发点漏传会安静地拿到其中一个，症状是「某条路径偶尔提前失效」。同一条道理已经用在
  `decode_token(expected_type=...)` 上。**去掉默认值当场就抓出一个未显式传参的既有调用点**
- **`gt=0` 让非正值直接起不来。** 设成 0 或负数时令牌签发即过期，**全部新管理员被锁在门外**，
  而症状是「密码明明对却一直说令牌无效」。与 compose 里密码留空直接报错停住是同一种 fail-closed

**设计闸门走了两轮，被打回的那条我认**

v1 把「配置下界要不要校验」写成未决问题、请审查方判断，**然后仍然标了 `READY_FOR_REVIEW`**。
模板写得很清楚：存在未解决的安全或可用性问题时不得标记 `READY_FOR_REVIEW`，而「全部新管理员
被锁在门外」正是这一类。**这是流程误用，不是措辞疏忽** —— 我把一个该自己定的决定当成了可以
外包给审查方的选项。v2 在 §2 定死，闸门一次通过。

**测试**：新增 6 条用例（两条路径的寿命各自独立、600 秒的两侧边界、配置非正值被拒、
默认值不同、以及**服务层验证两个签发点没传反**）。最后那条是唯一能抓住「参数传反」的用例 ——
`test_config.py` 只能证明两个默认值不同，证明不了它们接对了地方。

**变异测试 5/5 抓住**：两个签发点传反、注册路径仍用 120、日常登录被一起延长到 600、
下界约束被去掉、`ttl_seconds` 参数被忽略写死 120。

**边界用例的写法**：把**签发时间往过去推**，再用真实当前时间解码。不能把时钟往未来推 ——
PyJWT 2.10 会拒绝 `iat` 落在未来的令牌，那样失败原因看起来像签名问题（T0.8a 踩过）。


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

## 待办：既有配置项缺下界校验

- [ ] **`Settings` 里既有的 TTL / 计数类配置项没有下界约束**（T0.8e 派生）：`access_token_ttl_seconds`、`refresh_token_ttl_seconds`、`refresh_token_idle_seconds`、`login_max_failures`、`login_lockout_seconds`、`auth_rate_limit_*` 设成 0 或负数都不会被拒，各自会带来一种「配置对了一半」的故障（令牌立刻过期、锁定立刻解除、限流桶为空…）。T0.8e 只给自己新增的两个加了 `gt=0`，**刻意没有顺手补旁边的**（那是范围扩张）。补的时候要逐个想清楚每项的合理下界，不是无脑 `gt=0`

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

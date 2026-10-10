# `.platform/` —— 控制契约

这一层是给**未来的 Windows Execution Worker** 看的机器可验证 allowlist。
它现在**没有接任何东西**：没有 Worker、没有定时任务、没有 webhook、没有外部通知。
合并它只是把「允许什么」先写死，好让后面接 Worker 时有一份可审的边界，而不是
接上之后再补规则。

本目录不改动任何业务代码、部署配置、依赖或数据库。

---

## 文件

| 文件 | 管什么 |
| --- | --- |
| [project.yaml](project.yaml) | 项目事实、角色绑定、Worker 总开关 |
| [commands.yaml](commands.yaml) | 可执行命令 allowlist（固定 executable + 固定参数） |
| [tasks.yaml](tasks.yaml) | 可调度任务 allowlist |
| [state.schema.json](state.schema.json) | 单次 run 的状态文档格式 |
| [state.examples.json](state.examples.json) | 一个正例 + 三个反例 |
| [approvals.yaml](approvals.yaml) | 动作分档与批准绑定 |

---

## 控制面与仓库的关系

OpenClaw 是**私有控制面**，它不直接改这个业务仓库。它能做的只有一件事：
按下面的契约把一个已登记的任务交给 Worker 去跑，然后记录状态。

仓库的写入口没有变，还是那条已经跑了半年的路：
Claude Code 实现 → Codex 只读审查 → Kelvin 合并。
流程的唯一事实来源仍然是 [docs/WORKFLOW.md](../docs/WORKFLOW.md)，本目录不取代它、
也不新增第二条入口。

---

## Telegram 只接受这四条

```text
开启 ai_billing_hub [task_id]
状态 [run_id]
取消 [run_id]
批准 [run_id]
```

`[task_id]` 只能是 `tasks.yaml` 里逐个列出的 id，目前是 `AIH-TASK-001`、
`AIH-TASK-002`、`AIH-TASK-003`、`AIH-TASK-004`、`AIH-TASK-005`、`AIH-TASK-006`、`AIH-TASK-007`、`AIH-TASK-008`、`AIH-TASK-011`、`AIH-TASK-012`、`AIH-TASK-013`、`AIH-TASK-014`、`AIH-TASK-015`、`AIH-TASK-016`、`AIH-TASK-017`、`AIH-TASK-018`、`AIH-TASK-019`、`AIH-TASK-020`、`AIH-TASK-021`、`AIH-TASK-022` 到 `AIH-TASK-044`（用途见下面「Worker 的启用状态」）。
`AIH-TASK-009` / `AIH-TASK-010` 不在其中：它们是控制面给「采纳提议」的 run 分配的编号，不写进 `tasks.yaml`。

除此之外一律 **fail closed** ——

- 未知命令：拒绝，不做模糊匹配、不猜意图
- 未知 task id：拒绝（`tasks.yaml` 的 `unknown_task_id`），格式合法**不等于**已授权
- 未授权发送者：拒绝
- 契约文件缺失或解析失败：拒绝，不退化成「按默认值跑」

聊天永远只是**触发器**，不是执行通道：消息正文不会被当成命令、参数或代码执行。
`approvals.yaml` 把 `execute_arbitrary_shell_from_chat` 放进 `prohibited` 一档，
那一档没有「拿到批准就可以」的路径。

---

## 角色

| 角色 | 是谁 | 在本契约里 |
| --- | --- | --- |
| 实现 | Claude Code | 写代码与测试，开 PR，按审查意见修改 |
| 独立审查 | Codex | 只读审查，不改任何文件 |
| 合并 | Kelvin | 唯一的 merge owner，也是唯一的批准角色。例外只有一个：OpenClaw 的自动收尾 PR 在 CI 通过后自动合并（见下面「Worker 的启用状态」） |

Worker 本身不进入这三个角色中的任何一个，它只是执行环境。跑什么取决于任务：
`AIH-TASK-001` 下只跑 `commands.yaml` 里的检查；`AIH-TASK-002` 到 `AIH-TASK-008`、`AIH-TASK-011` 到 `AIH-TASK-044` 下由 Worker
中运行的 Claude Code 担任实现角色（改 `allowed_change_paths` 列出的文件、开 Draft PR），审查仍是
Codex，合并仍是 Kelvin。

---

## Worker 的启用状态

`project.yaml` 的 `worker_enabled` 已置 `true`，但这只是**业务契约侧**的同意：
本仓库登记了 `AIH-TASK-002` 到 `AIH-TASK-008`、`AIH-TASK-011` 到 `AIH-TASK-044`，能否调度以 `tasks.yaml` 里的 `status` 为准（见下）。它本身不会让任何东西执行。每次 run
仍要求下面几样成立：

- 控制面 registry 登记本项目
- Worker 的 host-local 配置就位（不在本仓库）
- Worker 启动前预检通过
- Kelvin 对启用给出独立的明确批准（合并本契约不算）

完整清单见 `project.yaml` 的 `enable_preconditions`。这些 gate 的实际状态由控制面记录，
不在本仓库。把 `worker_enabled` 改回 `false` 仍是**出问题时的回滚方式**：不需要删文件、
不需要改代码。

已登记任务的用途不同：

| 任务 | 用途 | 写仓库吗 |
| --- | --- | --- |
| `AIH-TASK-001` | 校验本控制契约，并跑仓库已有的只读 / 测试检查 | 不建分支、不开 PR |
| `AIH-TASK-002` | 第一次端到端 Pilot：Worker 中的 Claude 更新一份非生产文档，跑文档类检查，以 Draft PR 交付，验证「实现 → Codex 审查 → Kelvin 合并」这条链 | 建分支、开 Draft PR（开 PR 仍需绑定到该 run 的一次性批准）；不合并 |
| `AIH-TASK-003` | 业务仓库适配（非生产功能）：让 `policy.check` / `tests.process` 在 Worker 模式下消费控制面给的只读 Git manifest（见下面「Worker 模式的 Git 输入」），以便之后重试 `AIH-TASK-002` | 同 `AIH-TASK-002` |
| `AIH-TASK-004` | Phase 1 第一刀：`tenants` / `projects` 两张表（只含身份与归属字段）、Alembic 迁移 0005、repository 与测试；不含状态、金额、认证、webhook 与 API。表结构裁决见 [docs/database-schema.md](../docs/database-schema.md) | 同 `AIH-TASK-002`；合并即由自动部署在生产上执行迁移，Worker 自己不对任何数据库跑迁移 |
| `AIH-TASK-005` | Phase 1 第二刀：钱包与不可变账本的数据层，加上由余额驱动的计费状态。**碰钱，已过设计闸门** #88（`APPROVED: design v6`）；批准的设计逐字放在 [docs/design/AIH-TASK-005-wallet-ledger.md](../docs/design/AIH-TASK-005-wallet-ledger.md)，Worker 以它为准 | 同 `AIH-TASK-002`；合并即由自动部署在生产上执行迁移 0006（含触发器）。Worker 生成的 PR 正文固定写「设计闸门：不适用」，由实现方改成 `#88` 再审 |
| `AIH-TASK-006` | Phase 1 第三刀：管理端客户管理。管理员建客户时在同一事务里建钱包并写审计，建项目，分页查看客户与项目；新增管理员鉴权 `require_admin`。**碰钱包创建与鉴权，已过设计闸门** #96（`APPROVED: design v3`）；批准的设计逐字放在 [docs/design/AIH-TASK-006-admin-customers.md](../docs/design/AIH-TASK-006-admin-customers.md)，Worker 以它为准 | 同 `AIH-TASK-002`；没有迁移。Worker 生成的 PR 正文固定写「设计闸门：不适用」，由实现方改成 `#96` 再审 |
| `AIH-TASK-007` | 文档卫生，非生产功能：收口 P-2 的 R5「需求编号覆盖度」—— 在 [docs/REQUIREMENTS.md](../docs/REQUIREMENTS.md) 写下「什么算一条硬性要求」的可枚举定义，补完整章节索引与 `REQ-*` 双向闭合，并新增 `tests/test_requirements_coverage.py` 把这些机械锁死。**不走设计闸门**（文档与脚本，既不碰钱也不碰用量摄取 / 认证与会话 / Webhook） | 改 [docs/REQUIREMENTS.md](../docs/REQUIREMENTS.md)、[docs/TODO.md](../docs/TODO.md) 与新增一个测试文件，开 Draft PR；没有迁移、不碰业务代码。Worker 生成的 PR 正文固定写「设计闸门：不适用」，本任务正好就是不适用，不需要改 |
| `AIH-TASK-008` | R5 的最后一刀：按 2026-09-21 拍板的分档口径补齐覆盖表里剩下的 15 个缺口 —— Invariant 7 / Invariant 13 / Gate 1 / Gate 4 四条在 spec 里新增 `REQ-*` 编号，DoD 1–8 与 13–15 共 11 条改标「不适用」并逐条给理由。**不走设计闸门**：只做可追溯性标注，spec 的规范性语义一个字不改 | **唯一会改 spec 正文的任务**，所以契约把它锁成「只许新增」：spec 的 diff 里删除行必须恰好一行，且就是文件头的 Version 行（1.6 → 1.7）。另改 [docs/REQUIREMENTS.md](../docs/REQUIREMENTS.md)、`tests/test_requirements_coverage.py` 与 [docs/TODO.md](../docs/TODO.md)；没有迁移、不碰业务代码 |
| `AIH-TASK-011` | Phase 1 第四刀：管理端手工调账。管理员按客户记一笔 `ADMIN_ADJUSTMENT`，账本、调账审计与计费状态跃迁同一事务，幂等键即账本 `reference_id`。**碰钱，已过设计闸门** #111（`APPROVED: design v1`）；批准的设计逐字放在 [docs/design/AIH-TASK-010-admin-wallet-adjustment.md](../docs/design/AIH-TASK-010-admin-wallet-adjustment.md)（设计沿用 010 的编号），Worker 以它为准 | 同 `AIH-TASK-002`；没有迁移。Worker 生成的 PR 正文固定写「设计闸门：不适用」，由实现方改成 `#111` 再审 |
| `AIH-TASK-012` | Phase 1 第五刀：集成 API 凭据。管理员为项目建、列、轮换、吊销凭据（`api_key` 不变、版本递增，重叠期默认 7 天），签名密钥用信封加密存储，secret 只在建凭据与轮换时返回一次；另有不接端点的签名校验库。**碰认证与凭据，已过设计闸门** #118（`APPROVED: design v1`）；批准的设计放在 [docs/design/AIH-TASK-012-integration-access.md](../docs/design/AIH-TASK-012-integration-access.md)，Worker 以它为准 | 同 `AIH-TASK-002`；合并即由自动部署在生产上执行迁移 0007。文件名避开 Worker 的敏感路径规则，用 `integration_access`（见契约）。Worker 生成的 PR 正文固定写「设计闸门：不适用」，由实现方改成 `#118` 再审 |
| `AIH-TASK-013` | AIH-TASK-012 留下的文档尾巴：在 [docs/runbook.md](../docs/runbook.md) 补一段 `BILLING_CREDENTIAL_ROTATION_OVERLAP_SECONDS` 的运维说明（含义、默认 7 天、怎么改、怎么生效、对已轮换凭据的影响），事实以 012 的设计 §2「配置」与代码为准。**不走设计闸门**：纯运维文档，不改钱、状态机与认证逻辑。也是「按规划执行」的第一项 | 只改 `docs/runbook.md` 与 [docs/TODO.md](../docs/TODO.md)；不碰代码、数据库与迁移。planning-v1 块由管理员的收尾 PR 改，Worker 不动 |
| `AIH-TASK-014` | 把 [docs/ARCHITECTURE.md](../docs/ARCHITECTURE.md) 第 8 节那句 spec §136 文档清单同步成现状：已建的四份（database-schema / api / deployment / runbook）加链接，未建的五份写明尚未创建。**不走设计闸门**：纯文档。用来验证 OpenClaw P3（就绪预检、状态时间线、批准预检、合并时的部署绑定） | 只改 `docs/ARCHITECTURE.md` 一个文件，检查只有 `docs.check`；不碰代码、迁移、docker/compose、`.platform/` 与 `docs/TODO.md`。planning-v1 块由管理员的收尾 PR 改 |
| `AIH-TASK-015` | 管理端前端第一刀：客户列表、建客户、客户详情（含钱包余额与计费状态）与编辑，调用 006 / 009 已上线的接口；新建 `features/customers/`，加 `MoneyText`（金额只按十进制字符串处理）与 `DateTimeText`（UTC → Asia/Kuala_Lumpur）。**不走设计闸门**：纯前端 | 十九项，全在 `frontend/src/` 与 `docs/TODO.md`（含把 `DashboardPage` 的 `RequestReference` 抽成共享组件）；不装依赖、不碰后端。Worker 跑 `frontend.test` / `frontend.typecheck` / `frontend.lint`（见下面「在 Worker 的 MXC 里跑不起来的检查」的前端一行），`build` 只在 CI 的 frontend job |
| `AIH-TASK-016` | 管理端前端第二刀：客户详情页的项目列表与建项目，并给 018 留挂载点。依赖 015。**不走设计闸门**：纯前端 | 八项；同 015 |
| `AIH-TASK-017` | 管理端前端第三刀：手工调账（接口已过闸门 #111）。幂等键每次打开表单生成、结果未知时锁定表单只许同键重试。依赖 016（两者都改客户详情页）。**不走设计闸门**：纯前端，钱的规则全在后端 | 八项，新建 `features/wallet/`；同 015 |
| `AIH-TASK-018` | 管理端前端第四刀：项目集成 API 凭据的建、列、轮换、吊销（接口已过闸门 #118）。`secret` 只在结果对话框出现一次，不进查询缓存与浏览器存储。依赖 016。**不走设计闸门**：纯前端 | 八项；文件名沿用 `integration_access` 的叫法避开 Worker 的敏感路径规则；同 015 |
| `AIH-TASK-019` | 出站状态 webhook 的签名密钥：`project_webhook_secrets`（待启用 / 签名中 / 已退役）、四个管理端接口、不接发送方的签名库。**碰 Webhook 与密钥，已过设计闸门** #135（`APPROVED: design v1`）；批准的设计逐字放在 [docs/design/AIH-TASK-019-webhook-signing.md](../docs/design/AIH-TASK-019-webhook-signing.md)，Worker 以它为准 | 十五项；合并即由自动部署在生产上执行迁移 0008。文件名用 `webhook_signing` 避开 Worker 的敏感路径规则。PR 正文由实现方改成 `#135` 再审 |
| `AIH-TASK-020` | 租户账户状态的启用与停用：`tenants.account_status`、管理端接口、与计费状态共用 `status_version`，审计与 outbox 同事务；关户移出，等 Phase 2 / 4。**状态机，已过设计闸门** #136（`APPROVED: design v2`）；设计在 [docs/design/AIH-TASK-020-tenant-account-status.md](../docs/design/AIH-TASK-020-tenant-account-status.md)。依赖 019 | 十三项；合并即执行迁移 0009。PR 正文由实现方改成 `#136` 再审 |
| `AIH-TASK-021` | 审计表在数据库层只追加：迁移 0010 加 BEFORE UPDATE / DELETE 触发器（照账本），靠 DELETE 清场的 MySQL 测试改用 TRUNCATE，压测脚本不再删审计。**不走设计闸门**：不碰钱、状态机与认证。依赖 020 | 十三项（`test_password_reset_concurrency.py` 撞 Worker 敏感路径词，由管理员前置 PR 改）；合并即执行迁移 0010 |
| `AIH-TASK-022` | 管理端审计日志查询接口 `GET /api/v1/admin/audit-logs`（spec §89）：筛选、分页、最新在前，内部 id 一律换成用户邮箱；迁移 0011 补查询索引。**不走设计闸门**：只读。依赖 021 | 十二项；合并即执行迁移 0011 |
| `AIH-TASK-023` | 管理端前端审计页（spec §101 `features/audit/`）。任务自己声明 `acceptance`（命令 `acceptance.browser`，只读，三个步骤），审计页的两个新步骤加进 024 的验收脚本。**不走设计闸门**：纯前端。依赖 022 | 十二项（含验收脚本）；Worker 跑前端三项检查，build 只在 CI |
| `AIH-TASK-024` | OpenClaw 浏览器验收（P6）的试点：写 `scripts/acceptance/admin_customers.mjs`，部署后由 Worker 在生产上用验收夹具账号只读地验 015 的页面（登录、列表、夹具详情、余额）。任务自己声明 `acceptance`（命令 `acceptance.browser`，只读，四个步骤），主机见 `project.yaml` 的 `acceptance_hosts`。依赖 015。**不走设计闸门**：只读驱动已上线页面，不碰钱、状态机、认证逻辑与 webhook | 只改那一个脚本，检查只有 `docs.check` / `policy.check`。⚠️ 这个脚本在 MXC 之外、带凭据、网络不受限运行（控制面安全边界 A7），评审按生产代码逐行看；`acceptance.browser` 只能被 `acceptance` 块引用，不得进任何任务的 `allowed_commands` |
| `AIH-TASK-025` | Phase 2 第一刀（[PHASE-2-plan.md](../docs/design/PHASE-2-plan.md) 的 T-A）：计量类型与分量（按上报形态约束取数字段，管理员可新建）、供应商、模型、按 `occurred_at` 分段的模型别名，以及在调用方事务里加共享锁的只读解析函数 `resolve_model`。**碰定价的根，已过设计闸门** #163（`APPROVED: design v4`）；设计在 [docs/design/AIH-TASK-025-ai-catalog.md](../docs/design/AIH-TASK-025-ai-catalog.md)。依赖 022 | 二十项（#185 补了 `app/services/audit_query.py`）；合并即执行迁移 0012。纯后端，不带 `acceptance` 块。PR 正文由实现方改成 `#163` 再审 |
| `AIH-TASK-026` | Phase 2 的 T-B：供应商成本价的版本（草稿 → 发布 → 退役，只在末尾追加、可预约、不许回溯），发布与计费解析用供应商行锁串行，触发器保证不可变、分量完整与区间不重叠；只读查询 `resolve_provider_price`。**碰定价，已过设计闸门** #177（`APPROVED: design v4`）；设计在 [docs/design/AIH-TASK-026-provider-prices.md](../docs/design/AIH-TASK-026-provider-prices.md)。依赖 025 | 十九项（含 `app/services/audit_query.py`，v4 补）；合并即执行迁移 0013。纯后端，不带 `acceptance` 块。PR 正文由实现方改成 `#177` 再审 |
| `AIH-TASK-027` | Phase 2 的 T-D：客户定价规则（五级范围、MARKUP / FIXED_RATE、含税），单行锁表让发布与计费串行，只有全局默认的第一条可回溯，触发器保证不可变、完整与区间不重叠；只读解析 `resolve_pricing_rule`（命中即停）。**碰定价，已过设计闸门** #178（`APPROVED: design v4`）；设计在 [docs/design/AIH-TASK-027-pricing-rules.md](../docs/design/AIH-TASK-027-pricing-rules.md)。依赖 026 | 二十项（含 `app/services/audit_query.py`，#193 补）；合并即执行迁移 0014。纯后端，不带 `acceptance` 块。PR 正文由实现方改成 `#178` 再审 |
| `AIH-TASK-028` | Phase 2 的 T-C 第 1 / 6 刀（设计 §11 的 F1）：汇率的数据库层 —— 迁移 0015 的三张表、CHECK、`open_slot` 与全部触发器。**碰汇率，已过设计闸门** #183（`APPROVED: design v3`）；设计在 [docs/design/AIH-TASK-028-fx-rates.md](../docs/design/AIH-TASK-028-fx-rates.md)。2026-09-30 由 v1 的单个任务（三十九项）按层拆成 028、039–044。依赖 027 | 八项；合并即执行迁移 0015。PR 正文改成 `#183` 再审 |
| `AIH-TASK-045` | 两张单行锁表 `pricing_rule_locks` / `fx_rate_locks` 拒绝 UPDATE：迁移 0016 只加两个无条件拒绝的 BEFORE UPDATE 触发器，不改模型与服务。由 #199（收尾 028）的 Codex 审查引出。**碰定价与汇率的串行化，已过设计闸门** #200（`APPROVED: design v1`）；设计在 [docs/design/AIH-TASK-045-lock-tables-no-update.md](../docs/design/AIH-TASK-045-lock-tables-no-update.md)。依赖 027（迁移 0015 已随 #197 部署） | 六项；合并即执行迁移 0016，029 / 032 的迁移顺延为 0017 / 0018。纯后端，不带 `acceptance` 块。PR 正文改成 `#200` 再审 |
| `AIH-TASK-039` | T-C 第 2 / 6 刀（F2）：发布 P1–P4、退役 R1–R5、锁在前、事务内复查、审计，加锁读的 `resolve_fx_rate`；测试与设计例子 E1–E16 一一对应。#183 v3。依赖 028 | 七项（含 `app/services/audit_query.py`）。PR 正文改成 `#183` 再审 |
| `AIH-TASK-044` | T-C 第 2b 刀（F2b）：只加测试 —— 真 MySQL 上的发布与计费并发、两侧的快照读陷阱、时间稳定性性质用例。v3 从 039 拆出（登记前预审判 039 过大）。#183 v3。依赖 039 | 两项；不改产品代码 |
| `AIH-TASK-040` | T-C 第 3 / 6 刀（F3）：BNM 适配器（按日期取、双精度字面量取最短往返表示）、拉取任务、一天三次的 Beat；测试用设计 §10 的录制响应。#183 v3。依赖 039 | 十项 |
| `AIH-TASK-041` | T-C 第 4 / 6 刀（F4）：管理端八个汇率接口；可在服务与仓储里只新增只读查询。#183 v3。依赖 039 | 九项 |
| `AIH-TASK-042` | T-C 第 5 / 6 刀（F5）：带令牌的 `/internal/alerts`、维度注册表、`fx_fetch` / `fx_stale`；监控令牌 secret。#183 v3。依赖 040 | 九项（含 compose） |
| `AIH-TASK-043` | T-C 第 6 / 6 刀（F6）：nginx 放行与拒绝、`monitor.sh` 按行推 Healthchecks、`config_snapshot.sh` 按键名模式自查、部署与运维文档、ADR-0005 收口。#183 v3。依赖 042 | 九项（含 nginx、monitor.sh、config_snapshot.sh）；合并后有运维步骤（令牌文件、Healthchecks、验证 BNM 可达、发布第一个汇率） |
| `AIH-TASK-029` | Phase 2 的 T-E：`POST /api/v1/integration/usage-events`（验签、nonce、持久化 202、全局幂等与冲突、固定的 `retryable`）；nginx 集成前缀的 JSON 413；冲突告警维度。**碰摄取与幂等，已过设计闸门** #176（`APPROVED: design v8`）；设计在 [docs/design/AIH-TASK-029-usage-ingest.md](../docs/design/AIH-TASK-029-usage-ingest.md)。依赖 043（2026-09-30 前是 028：它往 042 建的告警注册表加维度，并与 043 改同一份 nginx 配置） | 二十八项（`app/services/audit_query.py` 由 #193 补）；合并即执行迁移 0017。PR 正文改成 `#176` 再审 |
| `AIH-TASK-030` | Phase 2 的 T-F：批量摄取端点，逐条复用 029 的单条逻辑。**已过设计闸门** #180（`APPROVED: design v3`）；设计在 [docs/design/AIH-TASK-030-usage-ingest-batch.md](../docs/design/AIH-TASK-030-usage-ingest-batch.md)。依赖 029 | 十项；无迁移。PR 正文改成 `#180` 再审 |
| `AIH-TASK-031` | Phase 2 的 T-G：计价纯函数、解析与取锁、只读试算接口。**碰定价，已过设计闸门** #179（`APPROVED: design v1`）；设计在 [docs/design/AIH-TASK-031-pricing-engine.md](../docs/design/AIH-TASK-031-pricing-engine.md)。依赖 039（2026-09-30 前是 028：计价只用 `resolve_fx_rate` 与共享锁，#183 v2 §11） | 十项；无迁移。PR 正文改成 `#179` 再审 |
| `AIH-TASK-032` | Phase 2 的 T-H：计费 worker（认领为 PROCESSING、防护令牌、扣费、卡住回收）、重新入队、七个告警维度。**碰钱包与状态机，已过设计闸门** #181（`APPROVED: design v2`）；设计在 [docs/design/AIH-TASK-032-usage-billing.md](../docs/design/AIH-TASK-032-usage-billing.md)。依赖 030、031 | 二十五项；合并即执行迁移 0018。PR 正文改成 `#181` 再审 |
| `AIH-TASK-033` | Phase 2 的 T-I：最热租户压测脚本（ADR-0011 的 S1–S4 与方案 C 判据），只对 `billing_perf` 库运行。**不走设计闸门**：测量脚本。依赖 032 | 四项；实测是合并后的运维步骤 |
| `AIH-TASK-034` | Phase 2 的 T-J：管理端用量事件查询（列表与详情）。**不走设计闸门**：只读。依赖 032 | 七项 |
| `AIH-TASK-035` | Phase 2 的 T-K 上半：管理端前端 AI 目录（计量类型、供应商、模型、别名）。带 `acceptance`（`open_catalog`、`check_meter_types`）。**不走设计闸门**：纯前端。依赖 043（2026-09-30 前是 028，只为排在汇率之后） | 十六项（含验收脚本） |
| `AIH-TASK-036` | Phase 2 的 T-K 下半：管理端前端供应商价格与汇率。带 `acceptance`（`open_provider_prices`、`open_fx_rates`）。依赖 035 | 二十项（含验收脚本） |
| `AIH-TASK-037` | Phase 2 的 T-L 上半：管理端前端定价规则与试算。带 `acceptance`（`open_pricing_rules`、`open_pricing_preview`）。依赖 036、031 | 十七项（含验收脚本） |
| `AIH-TASK-038` | Phase 2 的 T-L 下半：管理端前端用量事件与重新入队。带 `acceptance`（`open_usage_events`）。依赖 037、034 | 十四项（含验收脚本） |

`tasks.yaml` 里每个会写仓库的任务都有 `status`：

- `ready`：已登记，可以在 Telegram 发「开启」；
- `done`：已交付（合并并部署）；
- `superseded`：没有交付，由 Kelvin 决定不再做，`tasks.yaml` 里就地写明原因。

Worker 只接受 `ready`，所以另外两种任务都不会被误开。控制面推荐「下一个任务」时只从 `ready` 里挑（控制面
ACVDEV-TASK-019），所以任务交付之后必须改成 `done` 并移出「当前计划」，否则控制面按 `plan_task_delivered`
挡住整个项目。

**这一步由 OpenClaw 自动收尾**（控制面 ACVDEV-TASK-066）。OpenClaw 的 run 仍然改不了 `.platform/`，它自己的
实现 PR 做不到这一步；run 完成（合并、部署观察、声明过的浏览器验收都通过）之后，Worker 另开一个非 Draft 的
收尾 PR，只做两处机械改动：`tasks.yaml` 里这个任务 `status: ready` 改成 `status: done`，从
[docs/TODO.md](../docs/TODO.md) planning-v1 的「当前计划」删掉这一项（其后各项序号减一）。CI 通过后它自动合并；
`.github/workflows/deploy.yml` 的 `paths-ignore` 让只改这两个文件的合并不触发部署。
只有自动收尾失败（Telegram 收到「自动收尾失败」通知，附原因与建议）时，才由人按同样的两处改动手工开收尾 PR。

**交付不再另写仓库记录**：不在本文件追加状态句或交付经过，不在 `docs/TODO.md` 写交付记录，不在
[docs/REVIEW-LOG.md](../docs/REVIEW-LOG.md) 为交付加行（那里只记拍板的分歧与约定）。交付事实以实现 PR、run 的
「状态」时间线和收尾 PR 为准。下面的 `done` 清单与各任务的交付经过截至 `AIH-TASK-040`，是历史记录，今后不再追加。

2026-09-27 的状态：`AIH-TASK-003`（#81）、`AIH-TASK-004`（#85）、`AIH-TASK-005`（#92）、
`AIH-TASK-006`（#98）、`AIH-TASK-007`（#104）、`AIH-TASK-008`（#107）、`AIH-TASK-011`（#115）、
`AIH-TASK-012`（#120）、`AIH-TASK-013`（#126）、`AIH-TASK-014`（#129）、`AIH-TASK-015`（#139）、`AIH-TASK-016`（#141）、`AIH-TASK-017`（#145）、`AIH-TASK-018`（#147）、`AIH-TASK-019`（#152）、`AIH-TASK-024`（#158）、`AIH-TASK-020`（#161）、`AIH-TASK-021`（#167）、`AIH-TASK-022`（#170）、`AIH-TASK-023`（#173）、`AIH-TASK-025`（#186）、`AIH-TASK-026`（#188）、`AIH-TASK-027`（#192）、`AIH-TASK-028`（#197）、`AIH-TASK-045`（#202）、`AIH-TASK-039`（#205）、`AIH-TASK-044`（#207）、`AIH-TASK-040`（#209）是 `done`；
`AIH-TASK-002` 是 `superseded`：它的 Pilot 从未交付，要验证的端到端链已由 004 / 005 的真实 run 验证。
2026-09-27 登记 `AIH-TASK-015` 到 `AIH-TASK-018`（管理端前端四刀）为 `ready`，按 015 → 016 → 017 → 018 排进
[docs/TODO.md](../docs/TODO.md) planning-v1 块的「当前计划」；四个都已交付。2026-09-28 登记 `AIH-TASK-019`（#135）与 `AIH-TASK-020`（#136）为 `ready`，按 019 → 020 排进「当前计划」；同日按「审计日志差异清单」的拍板登记 `AIH-TASK-021` 到 `AIH-TASK-023`，排在 020 之后；019 已交付。同日按 Kelvin 的决定登记 `AIH-TASK-024`（浏览器验收试点）并排在「当前计划」第 1 条，已交付（部署后首次浏览器验收 PASS），020 回到第 1 条，也已交付，021、022、023 随后交付；项目余下的工作按 Phase 排进「后续计划」，要过设计闸门的在标题里注明，闸门批准后再逐个登记。2026-09-29 登记 `AIH-TASK-025`（#163，Phase 2 的 T-A）为 `ready`，排在「当前计划」第 1 条；同日登记 `AIH-TASK-026`（#177，T-B）与 `AIH-TASK-027`（#178，T-D），排在 025 之后；同日登记 Phase 2 其余的 `AIH-TASK-028` 到 `AIH-TASK-038`（028–032 过了设计闸门 #183、#176、#180、#179、#181；033 / 034 与四个前端任务不走闸门），按编号顺序排在 027 之后；025、026 已交付。2026-09-30 登记前预审（控制面 ACVDEV-TASK-059/062）判 028 的 v1 `NOT_READY`，设计回到 #183 升为 v2；对拆分后任务的预审又发现九处，升为 v3（`APPROVED: design v3`）。按 v3 §11 把 028 拆成七个任务：028 改为第一刀（F1，Kelvin 选保留原编号），新登记 `AIH-TASK-039` 到 `AIH-TASK-044`（044 是从 039 拆出的纯测试任务），在「当前计划」里按 028、039、044、040、041、042、043 紧跟 028、排在 029 之前；029 与 035 改为依赖 043，031 改为依赖 039。2026-09-30 #199（收尾 028）的 Codex 审查指出两张锁表可被 UPDATE 移走锁行，Kelvin 选先修再收尾：新开设计闸门 #200（`APPROVED: design v1`），登记 `AIH-TASK-045` 排在「当前计划」第 1 条、028 之前，029 / 032 的迁移顺延一位；按 Kelvin 的裁定 #199 先把 028 改成 `done`，045 随后交付，039 回到第 1 条。

`AIH-TASK-016` 不是由 Worker 的 PR 交付的：run `b287409b` 以 `checks_failed` 结束、没有开 PR —— `frontend.test` 先挂在
jsdom 缺 `ResizeObserver` 上，第二次自动修复在测试里补了替身，替身那行的多余类型断言又被 `frontend.lint` 拦下，修复次数已用完。
Kelvin 选由 Claude 接手：run 留下的 8 个文件原样拷到新分支，只改那一处断言，#141 合并为 `d7d330a` 并部署。

`AIH-TASK-025` 的 Worker PR #186 没有经过 Worker 的受限评审：run `11443341` 的 diff 约 25.6 万字符，超过评审的 20 万字符
上限，以 `review_input_unusable` 结束。Kelvin 选由 Claude 接手这个 PR：修掉 CI 上一条测试的防空转阈值（`ea10ee3`），
Codex 审查通过，合并为 `9b63cfa` 并部署。

`AIH-TASK-026` 的 Worker PR #188 由 Claude 手工完成：run `9abfe47e` 的独立审查 `REQUEST_CHANGES`（实现改了设计 v3
的规则而没有报回闸门、发布 / 退役的快照读陷阱、`audit_query.py` 不在允许路径），修复轮的改动因一条 E501 以
`checks_failed` 结束、没有提交。Kelvin 决定先让设计 v4 过闸门 #177，#190 把契约改到 v4 并补允许路径，再由 Claude
接手 #188（不重发「开启」），Codex 审查通过，合并为 `ef3800e` 并部署。

`AIH-TASK-027` 的 Worker PR #192 由 Claude 手工完成：run `20966ebc` 的独立审查认为实现没有缺陷，唯一的阻断是新审计
实体 `pricing_rule` 要在 `audit_query.py` 归类而该文件不在允许路径，run 以 `review_repair_no_change` 结束；CI 另有
四个测试自身的失败。#193 补允许路径（028、029 一并补上，032 写明沿用 `usage_event`），Claude 接手修完 CI；实现审查
又发现两条 v3 没列的数据库兜底，设计闸门 #178 升 v4（Kelvin 裁定保留全局默认第一条起点为空），#194 同步副本与契约，
#192 按 v4 补上后 Codex 审查通过，合并为 `3e8a950` 并部署。

`AIH-TASK-028` 是 Phase 2 第一个完全由 Worker 交付的任务：run `4df4c6cc` 首个提交的 CI backend 因测试自身的错误假设
失败，Worker 的修复轮改对后 CI 全绿，独立受限审查通过，Kelvin「批准」，合并为 `e8b667b` 并部署（迁移 0015）。没有经过
Codex 审查，PR #197 正文的「设计闸门」一行合并前也没有改成 #183（#203 事后补改）。收尾 PR #199 的 Codex 审查指出两张锁表可被 UPDATE 移走锁行，
修复登记为 `AIH-TASK-045`（设计闸门 #200）；先收尾、后修复的裁定见 [docs/REVIEW-LOG.md](../docs/REVIEW-LOG.md)。
`AIH-TASK-045` 由 Worker 交付：run `376e0811` 一个提交 `1b63af0`，CI 六项全绿，独立受限审查通过，Kelvin「批准」，
合并为 `a7ada92` 并部署（迁移 0016）。PR #202 正文的「设计闸门」一行同样没有改成 #200，也没有经过 Codex 审查。
收尾 PR #203 的 Codex 审查据此判阻断；Kelvin 裁定事后补改 #202 与 #197 的这一行，Worker 交付的任务以 Worker 的独立受限审查加
「批准」代替 Codex 审查，以后「批准」前先改好这一行，见 [docs/REVIEW-LOG.md](../docs/REVIEW-LOG.md)。
`AIH-TASK-039` 由 Worker 交付：run `e00848d9` 首个提交 `26aef08` 的 CI backend 因两个测试自身的错误失败，修复轮
`2fd5d6d` 改对后 CI 全绿、独立受限审查通过。「批准」前「设计闸门」一行已改成 #183；但改正文会在同一 head 上重跑 CI，
重跑未完时「批准」触发的合并因必需检查未过而失败，重跑变绿后合并为 `fb69fda` 并部署。以后改完闸门行要等重跑变绿再「批准」。
`AIH-TASK-044`（纯测试）由 Worker 交付：run `7d708ec4` 首个提交 `48a3d71` 的 Worker 审查判要改（快照读用例没写两种顺序
各自的预期），修复轮 `3933333` 审查通过；闸门行改为 #183 后等 CI 重跑变绿再「批准」，合并为 `a7b3030` 并部署。
`AIH-TASK-040` 由 Worker 交付：run `ad74996b` 只有一个提交 `b350544`，没有修复轮；闸门行改为 #183 后「批准」，
合并为 `e2351dd` 并部署。

`AIH-TASK-015` 是第一个在 Worker 里跑前端检查的任务（#138 登记 `frontend.test` / `frontend.typecheck` / `frontend.lint`，
控制面 ACVDEV-TASK-045 提供只读依赖目录）。前两次 run（`0bf98377` PR #133、`43dfa20c` PR #137）没有这三条检查，
后一次失败在 CI 的 frontend job；第三次 run `d1bb9e18` 三条都在 Worker 里零退出，PR #139 合并为 `cccd91c` 并部署。

`AIH-TASK-014` 用来验证 OpenClaw P3（run `2c4a37ea`，PR #129，合并为 `f247a7c`）。第一次「批准」在 CI 的 `backend`
还没跑完时发出，批准预检按 `checks_not_passed` 拒绝、run 回到等待批准；CI 全绿后再批准，合并与部署正常完成。

`AIH-TASK-013` 是第一个**按规划执行**（ACVDEV-TASK-038）走通的任务：「当前计划」第 1 条 → 开启 → 实现 → 检查 →
评审 → 批准 → 合并 → 部署都在 Telegram 里完成（run `ebbae679`，PR #126，合并为 `ee635cd`）。它的第一次 run
`1bcd3d2e` 实现器主动停下、没改文件（控制面记成 `unexpected_changes`）：契约要它沿用 runbook 里并不存在的配置章节，
而这个配置当时也没转发进容器。先由 #124 补上 compose 转发，#125 修正契约，再重发「开启」。

`AIH-TASK-012` 的 run `8f6c3ae3` 以 `failed:review_changes_requested` 结束（Worker 的独立受限评审要求修改，
原文在 Worker 主机的诊断目录）。修复由管理员作为追加提交推到同一个 PR #120，CI 与 Codex 复审通过后直接合并
（`1e790ba`），没有走 Telegram 批准 —— run 不在 `awaiting_merge`，批准会被拒。

`AIH-TASK-011` 不是由它自己的 Worker PR 合并的：Worker 的 #114 被 secret-scan 拦下（示例 uuid 误报，
gitleaks 逐个扫 PR 里的提交），实现改由只含一个提交的 #115 交付，run `10fe6c87` 在 Telegram 取消。

`AIH-TASK-007` 是第一个**全程由 Telegram 驱动**的任务：开启 → 实现 → 检查 → 评审 → 批准 → 合并 → 部署
都在聊天里完成（run `a7ad65ae`，PR #104，合并为 `a4aa18a`）。

`AIH-TASK-002` 的第一次 Pilot **未通过**。Worker 里刻意没有真实 Git，而 `policy.check` 与
`tests.process` 原本依赖它；重试前须先合并 `AIH-TASK-003`。本文件不记录 `AIH-TASK-003`
的运行结果或 `AIH-TASK-002` 的重试结果，也不声称它们已通过。2026-09-19 起这段只是历史：`AIH-TASK-003`
已由 #81 合并，`AIH-TASK-002` 改为 `superseded`，不再重试（见上面「`status`」一段）。

⚠️ **任务登记是管理员前置条件，不是 Worker 任务的改动。**`AIH-TASK-003` 在
`tasks.yaml` 的登记、`commands.yaml` 里放行 `ACUVEN_GIT_LS_FILES_MANIFEST`、以及本文件的
相应说明，必须由管理员先单独合并，之后才能调度该任务的 Worker run。控制面 Worker 拒绝
`allowed_change_paths` 里的任何 `.platform/` 路径，所以 Worker 任务本身不改这三份文件。

每个会写仓库的任务允许改哪些文件由 `tasks.yaml` 里的 `allowed_change_paths` 声明
（`AIH-TASK-002` 只有 `docs/openclaw-worker-pilot.md` 一项；`AIH-TASK-003` 只有
`scripts/check_repo_policy.py`、`tests/test_check_repo_policy.py`、`tests/test_gh_verified_write.py`、
`docs/TODO.md` 四项；`tests/test_gh_verified_write.py` 是契约修正加入的，理由见下面「Worker 模式的 Git 输入」；
`AIH-TASK-004` 是 `app/models/tenancy.py`、`app/repositories/__init__.py`、`app/repositories/tenancy.py`、
`alembic/env.py`、`alembic/versions/20260919_0005_tenants_projects.py`、`tests/backend/test_migrations.py`、
`tests/backend/test_tenancy_repository.py`、`docs/TODO.md` 八项；`AIH-TASK-005` 与 `AIH-TASK-006` 各是十四项；
`AIH-TASK-007` 是 `docs/REQUIREMENTS.md`、`tests/test_requirements_coverage.py`、`docs/TODO.md` 三项；
`AIH-TASK-008` 在这三项之外多一个 `docs/Acuven_Central_AI_Billing_Platform_Spec.md`，共四项；`AIH-TASK-011` 是十项；`AIH-TASK-012` 是二十一项 ——
逐个列在 `tasks.yaml` 里）。口径：

- 仓库根相对的 POSIX 路径，**逐个精确匹配文件**；不是 glob，也不是目录前缀
- 不得包含任何 `.platform/` 路径（控制面 Worker 会拒绝）
- 列表之外的任何改动都算越界，包括 rename / copy 的**源和目标**两端
- `acceptance_criteria` 里的文件范围描述只是审查依据，不是强制手段
- `status: ready` 的任务，`purpose` 与 `acceptance_criteria` 里点名的 `frontend/`、`app/`、`tests/`、`scripts/`、`docs/`、`alembic/` 下带扩展名的文件，必须在本任务的 `allowed_change_paths` 里、已在仓库里，或在 `depends_on` 传递闭包中某个 `ready` 任务的 `allowed_change_paths` 里（上游已 `done` 却没建这个文件的不算）。`scripts/check_repo_policy.py` 在登记 PR 的 CI 上机械检查；被连带变红的测试这类隐式依赖不在检查范围内

⚠️ 这个字段写在本仓库里**不会让它自动生效**。强制它的是控制面 Worker 经审查的
parser 与 pipeline：必须在跑检查、commit、push、开 Draft PR 之前核对改动集合，
越界即 `failed`（fail closed）。Worker 预检针对该任务校验不通过（读不到、解析不了
`allowed_change_paths`，或没按上面的口径生效）时，该任务**不能运行**。

---

## schema 保证什么、不保证什么

`state.schema.json` 机械保证的是**单个状态文档的形状**：

- `run_id` 是 UUID，`task_id` 匹配 `AIH-TASK-` 加至少三位数字，`project_id` 固定
- 状态只能取枚举里那十一个值之一
- 时间字段是 RFC3339 且必须带 `Z`（只收 UTC，不收偏移量）
- `attempt` 是正整数
- 只存 `requester_id_hash`（小写十六进制 SHA-256），原始发送者标识没有字段可落
- `additionalProperties: false` —— 未声明的字段直接拒绝，不静默吞掉

⚠️ 下面这些是**运行时约束，JSON Schema 单独保证不了**，必须由 Worker 实现，
不要因为 schema 校验通过就认为它们已经成立：

- **状态单向推进**：schema 只看一份文档，看不到上一份，因此挡不住 `completed`
  倒回 `running`。合法迁移见下面的「状态迁移」一节，必须由 Worker 与控制面
  在运行时强制
- **批准单次绑定**：`consumed` 只是一个布尔字段。「同一张批准不被用第二次」
  「run 的状态一变批准即作废」「批准过期」都要由 Worker 在消费时判定
- **`command_id` 真的在 allowlist 里**：schema 只校验它的字符形状，跨文件的
  成员关系是运行时检查
- **`state.examples.json` 目前没有被机器校验**：本仓库没有 JSON Schema validator，
  `jsonschema` 不是已声明依赖，本次任务也不装新依赖。那四个文档现在只做到
  「是合法 JSON」。把它们接进一个 validator 是启用 Worker 的前置条件之一

---

## 状态迁移

终态只有三个：`completed`、`failed`、`cancelled`。
`awaiting_review` 与 `awaiting_merge` **不是终态**，它们是在等人 ——
`tasks.yaml` 里因此把「Worker 干完活的交接态」（`worker_handoff_state`）与
「run 的终态」（`terminal_states`）分成两个字段。写成一个的话，
「Worker 到此为止」会被读成「run 到此为止」，控制面就再也推不动它了。

| 当前状态 | 允许迁移到 | 谁推 |
| --- | --- | --- |
| `received` | `validated` / `failed` / `cancelled` | 控制面 |
| `validated` | `queued` / `approval_required` / `failed` / `cancelled` | 控制面 |
| `approval_required` | `queued` / `failed` / `cancelled` | 控制面（拿到绑定到本 run 的批准之后） |
| `queued` | `running` / `cancel_requested` / `failed` / `cancelled` | 控制面 |
| `running` | `awaiting_review` / `cancel_requested` / `failed` | Worker |
| `cancel_requested` | `cancelled` / `failed` | Worker |
| `awaiting_review` | `awaiting_merge` / `completed` / `failed` / `cancelled` | 人（Codex 审查 + Kelvin） |
| `awaiting_merge` | `completed` / `failed` / `cancelled` | 人（Kelvin 合并） |
| `completed` / `failed` / `cancelled` | 无 | — |

三条读法：

- **Worker 的活到 `awaiting_review` 为止。**它不推 `awaiting_merge`，也不推
  `completed` —— 那两步的依据是审查结论和合并事实，不是命令的退出码
- **表里没有任何一条指回更早的状态**，这就是「单向推进」的全部内容。
  `completed` 倒回 `running` 不是「不推荐」，是拒绝
- **不在表里的迁移一律拒绝**，run 置 `failed`（fail closed），不猜意图

`AIH-TASK-001` 不建分支也不建 PR（`tasks.yaml` 的 `creates_pull_request: false`），
所以它的 run **不会经过 `awaiting_merge`**：停在 `awaiting_review`，由人看完之后
置 `completed` 或 `cancelled`。

`AIH-TASK-002` 到 `AIH-TASK-008` 会开 Draft PR（`creates_pull_request: true`），它们的 run
在审查通过后进 `awaiting_merge`，由 Kelvin 合并后才置 `completed`。

---

## 不记录的东西

以下内容不进状态、不进日志、不进任何通知：

- secrets、token、API key、凭据
- 原始电话号码或其它原始发送者标识（只存加盐哈希）
- 私有地址、主机名、内网 IP、本机绝对路径
- 聊天正文

本目录的配置文件里只写**角色名与稳定标识**。可执行文件的绝对路径属于本机信息，
由 Worker 安装侧固定（`commands.yaml` 的 `executable_resolution`），不写进仓库 ——
这个仓库是公开的。

---

## 命令 allowlist 的口径

`commands.yaml` 里每一条都能在 [docs/WORKFLOW.md](../docs/WORKFLOW.md) 第 7 节
「提 PR 前必须本地跑过」里找到出处，且对应文件在仓库中真实存在。没写进去的命令
不可执行（`default: deny`）。

硬约束：不经 shell、参数是固定字面量数组、不做模板替换、不展开通配符、
不接受调用方给的可执行文件或参数、工作目录只能是仓库根。

### 环境与网络：这两样必须由 Worker 强制

「不注入环境变量」只挡住了注入，**挡不住继承**。子进程按普通方式启动会原样拿到
宿主环境，于是一条声称无凭据的 low risk 命令会看到 `BILLING_TEST_DATABASE_URL`
之类的变量，转头去连真实的数据库或 Redis —— 契约宣称的边界就不成立了。

所以 `commands.yaml` 的 `constraints.environment` 规定：

- `inherit_host_environment: false` —— 环境先**清空**，不是过滤
- 只按**变量名**白名单放行让进程起得来的最小集（`PATH`、`SYSTEMROOT`、`TEMP`、
  `TMP`），值由 Worker 安装侧固定，不写进仓库；另加 Worker 模式的
  `ACUVEN_GIT_LS_FILES_MANIFEST`（只读 manifest 的路径，见下面「Worker 模式的 Git 输入」）
- 另有一份 `denied_name_patterns`（`BILLING_*`、`*DATABASE*`、`*REDIS*`、
  `*SECRET*`、`*TOKEN*`、`*PASSWORD*`、`*CREDENTIAL*`、`*API_KEY*`、`GH_*`、
  `GITHUB_*`、`AWS_*`），**deny 优先于 allow** —— 有人日后往白名单里加错东西也拦得住

同理，每条命令的 `network: false` **不是元数据**。`network_enforcement:
process_boundary_required` 要求它在进程边界上真的被强制（作业对象 / 防火墙规则 /
隔离账户，具体机制由 Worker 安装侧决定）。

⚠️ 这两条和状态迁移一样，都是**运行时约束**：写在 YAML 里不会让它们自动成立。
`project.yaml` 的 `enable_preconditions` 因此要求「已验证环境清理」与
「已验证网络隔离」——**没验证过就不要把 `worker_enabled` 打开。**

### 跑过这些检查不等于 CI 通过

`tests.backend` 在没有 `BILLING_TEST_DATABASE_URL` / `BILLING_TEST_REDIS_URL` 时会把
迁移与 broker 用例安静地 skip，而 Worker 这边环境是清空的、`BILLING_*` 还额外落在
拒绝名单里，所以那些用例**必然** skip；CI 那边起了真 MySQL 与 Redis，并且显式把
「有任何 skipped」判成失败。**不得把 skipped 读成 passed。**
准入判定以 GitHub 上的 CI 为准。

### 在 Worker 的 MXC 里跑不起来的检查

`commands.yaml` 里登记了十条，但当前 Worker 的 MXC（AppContainer）里**只有纯 Python 的那几条能跑**。
2026-09-19 用 Worker 自己的 `build_guarded_argv` 在 MXC 里实测：

| 命令 | MXC 里 | 原因 |
| --- | --- | --- |
| `docs.check` / `policy.check` / `tests.process` | 能跑 | 纯 Python |
| `lint.check` / `format.check` | 默认跑不起来；本机 Worker 按命令放开 Win32k 后能跑 | `ruff.exe` 导入 user32/gdi32，MXC 默认的 Win32k 禁用缓解让它初始化失败（`0xC0000142`）。只关掉这一条缓解、其余不变时两条都通过（实测，含 `-I` 形式） |
| `tests.backend` | 跑不起来 | 默认一 import SQLAlchemy 就走到 `platform.machine()` → WMI 查询，整进程崩溃（`0xC06D007E`）；放开 Win32k 后仍有 5 个 API 测试文件挂在 `socket.socketpair()` 上（MXC 禁 loopback），另有 bash 用例 |
| `scripts.verdict_tests` | 未实测 | 没有任何会写仓库的任务登记它 |
| `frontend.test` / `frontend.typecheck` / `frontend.lint` | 当前 argv 能跑；控制面部署前不可用 | 需要控制面以只读 junction 提供 `frontend/node_modules`、登记 `node`、把 `LOCALAPPDATA` 设成 worktree 之外的逐次临时目录。OpenClaw 在 Worker 的 MXC 里实测（2026-09-27），最早那版 argv 原样都跑不起来：AppContainer 里 lstat 不了 worktree 的上级目录，node 的 realpath 报 `EPERM`；`--configLoader runner` 解析不了 `@vitejs/plugin-react` 的依赖；经 junction 加载时 vitest 被加载两份；jsdom 安装检查误报。改成三条都带 `--preserve-symlinks` / `--preserve-symlinks-main`、test 用 `--configLoader native` 并经 `--execArgv` 传给子进程、`frontend/vite.config.ts` 加 `resolve.preserveSymlinks` 与 `VITEST_SKIP_INSTALL_CHECKS` 之后：test 63/63 用时 24s，typecheck 2.7s，lint 15–30s，工作区无改动 |

**lint / format 的放开（控制面 ACVDEV-TASK-017）**：控制面给本机 Worker 配置加了 `check_win32k`，只对**固定了
完整定义**（executable + 逐字 args，首参数必须是 Python 隔离模式 `-I`）的检查关掉 Win32k 这一条缓解；网络、
loopback、文件系统、capabilities 都不变。所以 `commands.yaml` 里这两条写成 `python -I -m ruff ...`，本机配置
里固定的定义必须与之逐字一致 —— 改其中一边不改另一边，run 在任何检查之前以 `policy_violation` 失败。

⚠️ 这件事**要等下面几样都成立**才能把 `lint.check` / `format.check` 加进会写仓库任务的 `allowed_commands`：
控制面 ACVDEV-TASK-017 已合并、本机 Worker 部署副本已更新到该提交、本机配置已固定这两条定义、并在本机
MXC 里实测过一次两条都零退出。**2026-09-19 这四样都已成立**（控制面 #17 合并、部署副本更新、本机配置固定两条定义、MXC 实测零退出），`AIH-TASK-005` 是第一个带上这两条的任务。

`tests.backend` 与 `scripts.verdict_tests` 仍只交给 CI；**不要**把它们加进任何任务的 `allowed_commands`，
否则 run 必然 `checks_failed`。

### Worker 模式的 Git 输入

Worker 里**刻意没有真实 Git**。控制面（ACVDEV-TASK-005）改为预先生成
`git ls-files -z --cached --others --exclude-standard` 的原始输出，写成只读文件，
把它的绝对路径放进环境变量 `ACUVEN_GIT_LS_FILES_MANIFEST`（已加进
`constraints.environment.allowed_names`）。`scripts/check_repo_policy.py` 的消费契约：

- **没设置该变量**（本地 / CI）：行为不变，照旧调用真实 Git
- **设置了且检查的是仓库根**：读 manifest 字节，不启动 Git；严格 UTF-8，
  每条记录以 NUL 结尾，与上面那条命令的输出逐条对应
- 以下一律 fail closed（退出码 2，不退化成「当作没有文件」）：变量值不是绝对且规范化的路径、
  文件缺失或不可读、是链接 / reparse point / 目录、超过 1,000,000 字节、非法 UTF-8、
  NUL 分帧错误（空文件、缺结尾 NUL、空记录）、记录是绝对路径 / 含 `.` 或 `..` 段 /
  含反斜杠、冒号或控制字符 / 重复
- 单测传入的临时仓库根**不走** manifest；PR 正文与回应检查（`--event-file` /
  `--body-file` / `--response-file`）仍只走真实 Git，属于本地 / CI 操作
- 报错不回显路径或记录内容

`policy.check` 与 `tests.process` 在 Worker 模式下都消费这个变量。`tests.process` 里要建临时
Git 仓库或读提交图的用例**只在设置了该变量时**以固定原因 skip，manifest 消费用例照常运行。

**唯一的非 Git 例外（契约修正）**：一次真实的 Windows MXC run 里，设置了该变量后 `tests.process`
跑到了不依赖 Git 的用例
`tests/test_gh_verified_write.py::VerifiedWriteTests::test_file_with_spaces_unicode_and_relative_path`。
MXC 里 Unicode 临时文件已创建、确实在工作树中，但 `Path.resolve(strict=True)` 报 WinError 5 ——
AppContainer 拒绝最终路径解析。本地与 CI 全量运行时该用例通过。因此：

- `tests/test_gh_verified_write.py` 加进 `AIH-TASK-003` 的 `allowed_change_paths`
- **只有这一个用例**、**只在设置了该变量时**可以 skip，原因固定且明确写出 Windows MXC 最终路径解析
- 不得整模块 / 整类 skip，不得 skip 其它不依赖 Git 的用例；需要真实 Git 的 skip 与 manifest 消费用例保持原样
- CI 不设该变量，必须运行它

这条修正与任务登记一样是管理员前置条件，须先合并再调度。本文件不声称 `AIH-TASK-003` 或其重试已通过。

和 `tests.backend` 一样，**Worker 里的 skipped 不是 passed**：CI 不设该变量、全量运行，
准入以 CI 为准。

---

## 动作分档

`approvals.yaml` 分三档，默认落最严的一档：

- `low` —— 只读状态、校验本契约、跑 allowlist 里那几条仓库检查
- `approval_required` —— 建/改 PR、装依赖、外部通知、部署、生产服务重启、
  数据库迁移、删除、凭据轮换、认证与防火墙配置变更
- `prohibited` —— 直推或合并默认分支、绕过 CI、绕过审查、读取或输出 secrets、
  从聊天执行任意 shell

批准绑定 `run_id` + `action` + `approver` 三者，单次使用，run 的状态一变即作废。
一个动作**没有**在 `low` 里明确列出，就不是 low —— 分档看后果，不看它跑起来
像不像一条命令。

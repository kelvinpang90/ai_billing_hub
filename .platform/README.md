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
`AIH-TASK-002`、`AIH-TASK-003`、`AIH-TASK-004`、`AIH-TASK-005` 与 `AIH-TASK-006`（用途见下面「Worker 的启用状态」）。

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
| 合并 | Kelvin | 唯一的 merge owner，也是唯一的批准角色 |

Worker 本身不进入这三个角色中的任何一个，它只是执行环境。跑什么取决于任务：
`AIH-TASK-001` 下只跑 `commands.yaml` 里的检查；`AIH-TASK-002` 到 `AIH-TASK-006` 下由 Worker
中运行的 Claude Code 担任实现角色（改 `allowed_change_paths` 列出的文件、开 Draft PR），审查仍是
Codex，合并仍是 Kelvin。

---

## Worker 的启用状态

`project.yaml` 的 `worker_enabled` 已置 `true`，但这只是**业务契约侧**的同意：
本仓库登记了 `AIH-TASK-002` 到 `AIH-TASK-006`，能否调度以 `tasks.yaml` 里的 `status` 为准（见下）。它本身不会让任何东西执行。每次 run
仍要求下面几样成立：

- 控制面 registry 登记本项目
- Worker 的 host-local 配置就位（不在本仓库）
- Worker 启动前预检通过
- Kelvin 对启用给出独立的明确批准（合并本契约不算）

完整清单见 `project.yaml` 的 `enable_preconditions`。这些 gate 的实际状态由控制面记录，
不在本仓库。把 `worker_enabled` 改回 `false` 仍是**出问题时的回滚方式**：不需要删文件、
不需要改代码。

六个已登记任务的用途不同：

| 任务 | 用途 | 写仓库吗 |
| --- | --- | --- |
| `AIH-TASK-001` | 校验本控制契约，并跑仓库已有的只读 / 测试检查 | 不建分支、不开 PR |
| `AIH-TASK-002` | 第一次端到端 Pilot：Worker 中的 Claude 更新一份非生产文档，跑文档类检查，以 Draft PR 交付，验证「实现 → Codex 审查 → Kelvin 合并」这条链 | 建分支、开 Draft PR（开 PR 仍需绑定到该 run 的一次性批准）；不合并 |
| `AIH-TASK-003` | 业务仓库适配（非生产功能）：让 `policy.check` / `tests.process` 在 Worker 模式下消费控制面给的只读 Git manifest（见下面「Worker 模式的 Git 输入」），以便之后重试 `AIH-TASK-002` | 同 `AIH-TASK-002` |
| `AIH-TASK-004` | Phase 1 第一刀：`tenants` / `projects` 两张表（只含身份与归属字段）、Alembic 迁移 0005、repository 与测试；不含状态、金额、认证、webhook 与 API。表结构裁决见 [docs/database-schema.md](../docs/database-schema.md) | 同 `AIH-TASK-002`；合并即由自动部署在生产上执行迁移，Worker 自己不对任何数据库跑迁移 |
| `AIH-TASK-005` | Phase 1 第二刀：钱包与不可变账本的数据层，加上由余额驱动的计费状态。**碰钱，已过设计闸门** #88（`APPROVED: design v6`）；批准的设计逐字放在 [docs/design/AIH-TASK-005-wallet-ledger.md](../docs/design/AIH-TASK-005-wallet-ledger.md)，Worker 以它为准 | 同 `AIH-TASK-002`；合并即由自动部署在生产上执行迁移 0006（含触发器）。Worker 生成的 PR 正文固定写「设计闸门：不适用」，由实现方改成 `#88` 再审 |
| `AIH-TASK-006` | Phase 1 第三刀：管理端客户管理。管理员建客户时在同一事务里建钱包并写审计，建项目，分页查看客户与项目；新增管理员鉴权 `require_admin`。**碰钱包创建与鉴权，已过设计闸门** #96（`APPROVED: design v3`）；批准的设计逐字放在 [docs/design/AIH-TASK-006-admin-customers.md](../docs/design/AIH-TASK-006-admin-customers.md)，Worker 以它为准 | 同 `AIH-TASK-002`；没有迁移。Worker 生成的 PR 正文固定写「设计闸门：不适用」，由实现方改成 `#96` 再审 |

`tasks.yaml` 里每个会写仓库的任务都有 `status`：

- `ready`：已登记，可以在 Telegram 发「开启」；
- `done`：已交付（合并并部署）；
- `superseded`：没有交付，由 Kelvin 决定不再做，`tasks.yaml` 里就地写明原因。

Worker 只接受 `ready`，所以另外两种任务都不会被误开。控制面推荐「下一个任务」时只从 `ready` 里挑（控制面
ACVDEV-TASK-019），所以任务合并部署之后，**要由管理员单独开一个收尾 PR 把它改成 `done`**（与 #93 那类
close-out PR 一起做）。Worker 自己的实现 PR 做不到：它不能改 `.platform/`，合并前也还没有部署。漏改的话，
控制面会一直推荐一个已经做完的任务。

2026-09-19 的状态：`AIH-TASK-003`（#81）、`AIH-TASK-004`（#85）、`AIH-TASK-005`（#92）是 `done`；
`AIH-TASK-002` 是 `superseded`：它的 Pilot 从未交付，要验证的端到端链已由 004 / 005 的真实 run 验证。

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
`tests/backend/test_tenancy_repository.py`、`docs/TODO.md` 八项；`AIH-TASK-005` 与 `AIH-TASK-006` 各是十四项，逐个列在 `tasks.yaml` 里）。口径：

- 仓库根相对的 POSIX 路径，**逐个精确匹配文件**；不是 glob，也不是目录前缀
- 不得包含任何 `.platform/` 路径（控制面 Worker 会拒绝）
- 列表之外的任何改动都算越界，包括 rename / copy 的**源和目标**两端
- `acceptance_criteria` 里的文件范围描述只是审查依据，不是强制手段

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

`AIH-TASK-002` 到 `AIH-TASK-006` 会开 Draft PR（`creates_pull_request: true`），它们的 run
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

`commands.yaml` 里登记了七条，但当前 Worker 的 MXC（AppContainer）里**只有纯 Python 的那几条能跑**。
2026-09-19 用 Worker 自己的 `build_guarded_argv` 在 MXC 里实测：

| 命令 | MXC 里 | 原因 |
| --- | --- | --- |
| `docs.check` / `policy.check` / `tests.process` | 能跑 | 纯 Python |
| `lint.check` / `format.check` | 默认跑不起来；本机 Worker 按命令放开 Win32k 后能跑 | `ruff.exe` 导入 user32/gdi32，MXC 默认的 Win32k 禁用缓解让它初始化失败（`0xC0000142`）。只关掉这一条缓解、其余不变时两条都通过（实测，含 `-I` 形式） |
| `tests.backend` | 跑不起来 | 默认一 import SQLAlchemy 就走到 `platform.machine()` → WMI 查询，整进程崩溃（`0xC06D007E`）；放开 Win32k 后仍有 5 个 API 测试文件挂在 `socket.socketpair()` 上（MXC 禁 loopback），另有 bash 用例 |
| `scripts.verdict_tests` | 未实测 | 没有任何会写仓库的任务登记它 |

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

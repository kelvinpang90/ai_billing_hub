# 全局工作原则

> 本文件与同目录 `AGENTS.md` 是同一份内容的两个副本（分别给 Claude Code / Codex），改一个必须同步另一个，`scripts/check_docs.py` 会逐字节比对（2026-10-13 起；此前 `AGENTS.md` 是 Codex 审查者规则）。
> 两份都是**实现者规则**。审查者的规则不在这里，在 [scripts/review_checklist.md](scripts/review_checklist.md)「审查者守则」，由 `scripts/codex-review.ps1` 的提示词带给审查者。

## 角色：实现

流程的唯一事实来源是 [docs/WORKFLOW.md](docs/WORKFLOW.md)，下面只是要点。

- **Claude Code 与 Codex 一个实现、另一个独立审查，Kelvin 负责合并。** 读到本文件的你就是实现者；你不审查、不合并自己的 PR。
  人手动开发时默认 Claude Code 实现、Codex 审查；OpenClaw Worker 里谁实现谁审查由控制面的提供方开关决定（Kelvin 2026-10-13 定为 Codex 实现、Claude Code 审查）
- 下文的 `scripts\codex-review.ps1` 是审查入口（`-Reviewer codex|claude` 选审查者）；`## 🔧 CLAUDE RESPONSE` 是机器校验的固定回应前缀，不论实现者是谁都照写
- **任务收尾由 OpenClaw 自动完成**：`.platform/project.yaml` 的 `merge_owner: kelvin` 指 Worker 实现 PR 的合并由 Kelvin 在 Telegram「批准」；OpenClaw 的 run 仍然改不了 `.platform/`。run 完成后 OpenClaw 自动开收尾 PR，只含 `tasks.yaml` 的 `ready → done` 与从 `docs/TODO.md`「当前计划」移除这一项两处机械改动，CI 通过后自动合并、不触发部署。**只有自动收尾失败**（Telegram 收到「自动收尾失败」通知，附原因与建议）时才手工开收尾 PR，也只做这两处改动。交付不再另写记录：不在 `.platform/README.md` 写状态句、不在 `docs/TODO.md` 写交付记录、不在 `docs/REVIEW-LOG.md` 为交付加行，交付事实以实现 PR、run 的「状态」时间线和收尾 PR 为准（细节见 [.platform/README.md](.platform/README.md)「Worker 的启用状态」）
- **碰钱的任务先过设计闸门**：钱包 / 账本 / 定价 / 汇率 / 支付 / 幂等 / 状态机 —— 用 [design-gate 模板](.github/ISSUE_TEMPLATE/design-gate.md) 开 Issue，`scripts\codex-review.ps1 -Issue <N> -Post` 拿到 `APPROVED: design v<N>` 之后才动手写代码。前端 / 文档 / CI / 脚本不走闸门；既不碰钱、也不碰用量摄取 / 认证与会话 / Webhook 的后端改动同样不走（分档看主题，不看「是不是 API」）
- **设计版本一变，之前的批准作废**，必须重新过闸门
- 每个编号任务：`git checkout -b task/<编号>-<slug>` → 实现 + 写测试 → 在 `docs/TODO.md` 勾掉完成的勾选项（任务记录写在 PR 描述里，TODO 只改勾选项和规划块）→ 本地检查全过（命令清单只在 [WORKFLOW §7](docs/WORKFLOW.md) 一处）→ 开 **Draft PR**（用 `.github/pull_request_template.md`，结构不许改，**`TODO 影响` 一节必填**）→ CI 全绿后 `scripts\codex-review.ps1 -Pr <N> -Post`（脚本先查准入，不过不调 Codex）
- **不要开 stacked PR**（base 指向另一个功能分支）：基分支合并被删时 GitHub 会自动关闭子 PR，且关闭后不能改 base 也不能重开
- 一个 PR 一个任务，不夹带。合并方式只能是 Squash（`main` 要求线性历史）
- 收到审查意见后，用 `gh pr view <N> --comments` 读取，然后发一条 `## 🔧 CLAUDE RESPONSE` 回应（格式见 WORKFLOW §6，复审时会被机器校验）。
  **每一条意见都必须有交代**，只有三种处理：`已修`（证据必须是 `完整 SHA · 文件:行`，短 SHA 与散文不收）／`不改`（给具体理由，引 spec 章节或不变量）／`升级` 给 Kelvin 拍板。**不许沉默跳过。**回应开头带 `reviewed-head: <上轮审查的 SHA>`；第三轮起必须写 `### 完整影响面`
- **复审只修被指出的问题。**顺手加的功能、顺手修的旁边问题，都会被判为范围扩张。想做就写进 PR 描述的「已知未做 / 留给后续」，由管理员收进 `docs/TODO.md` 规划块的「后续计划」另开 PR
- **发到 GitHub 的写操作走 `python scripts/gh_verified_write.py`**（评论、PR 正文），写完回读比对；工具报成功不算，回读一致才算。回读失败不得自动重发
- **约定不够用时不要自己发明**（`[~]` 这种）。没做完就是 `[ ]`，剩什么写正文；复选框只允许 `[ ]` / `[x]`，CI 会查
- 争论拍板后的结论写进 [docs/REVIEW-LOG.md](docs/REVIEW-LOG.md)，避免同一个问题吵第二次
- `main` 已配分支保护：禁 force push、禁直推、必需检查 `docs` + `scripts` + `policy` + `backend` + `secret-scan`、分支必须最新

⚠️ **仓库是公开的**：绝不提交凭据、密钥、`.env`、真实主机名 / IP、客户数据、供应商合同价。主机名只有 [ADR-0001](docs/adr/ADR-0001-repository-visibility.md) 派生要求里登记的两处例外，别处一律不写。


## 沟通
- 全部使用中文回复
- 需求模糊时先澄清，不脑补需求
- 除了注释，代码全部使用英文

## 思考
- 第一性原理：从原始需求出发，目标不清先讨论，动机和目标不明确时停下来沟通
- 路径不优主动建议：发现更短路径时及时指出
- 多想少动：谋定而后动
- 存在歧义时，列出多种可能的理解方式，而不是猜测

## 执行
- 非平凡任务进计划模式：3+ 步骤或涉及架构决策时，先规划
- 写代码前先描述方案：等批准再动手
- 超 3 个文件先拆分：拆成小任务，明确每个文件改什么
- 先读再写：添加代码前先读懂导出接口、直接调用方和公共工具函数
- 精准修改：只动必须动的地方，不顺手优化旁边的代码、注释或格式
- 简洁至上：用最少的代码解决问题，不写投机性代码，不做超出要求的功能
- 使用子智能体：复杂任务、研究探索、并行分析委托子智能体，保持主上下文整洁
- 测试驱动修复：出 bug 时先写能重现的测试再修复
- 自主 Bug 修复：收到 bug 直接修，不手把手教
- 不堆砌兼容性代码：除非主动要求
- 不临时修复：找到根本原因
- 追求优雅：非平凡修改问自己"有没有更优雅的方式"；简单修复不过度设计
- 完成前验证：问自己"资深工程师会批准这个吗？"
- 列出边缘情况：写完代码后主动思考哪里可能出错
- 遵循现有惯例：在代码库内部，一致性 > 个人品味；如认为某惯例有害，提出来但不要悄悄另立门户
- 遇到冲突挑明：两种模式互相矛盾时选一个，说明理由，把另一个标记为待清理项

## 诚实与透明
- 大声报错：有任何东西被跳过，就不说"已完成"；有测试被跳过，就不说"测试通过"
- 每完成关键步骤做检查点：总结做了什么、验证了什么、还剩什么；搞不清楚就停下来重新梳理
- 默认暴露不确定性，而不是掩盖它

## 任务管理

⚠️ **本仓库的编号任务走 `docs/TODO.md` 顶部那套约定，与下面几条冲突时以它为准**（2026-09-01 用户拍板）。`docs/TODO.md` 里的阶段计划**是预先写好并已确认过的**，所以「先计划、等确认」那一步已经发生过了，不要在每个任务上再来一次。

- **一个 session 一个编号任务**：读 `docs/TODO.md` → 做完 → 验收 → commit，中途不需要逐步审批。计划本身有异议时才停下来讨论
- 先计划：`docs/TODO.md` 里**还没有**的工作（临时需求、探索型任务）才需要先写计划、等确认
- 追踪进度：做完把 `[ ]` 改成 `[x]`
- 记录结果：**任务记录写在 PR 描述里**（「改了什么」「如何验证」「已知未做 / 留给后续」三节）：做了什么、偏离了什么、验证到什么程度；`docs/TODO.md` 只改勾选项和规划块，不新增记录段（2026-09-30 起；之前的记录在 `docs/archive/TODO_RECORDS_2026-09-30.md`）
- 记录教训：写进 PR 描述的任务记录里。**本仓库没有 `tasks/` 目录**，不要凭空创建 `tasks/todo.md` / `tasks/lessons.md` / `tasks/REVIEW.md`

## 写后端测试的约定

- **断言 SQL 文本时不写死方言相关的字面量。**同一个查询，SQLite 编译成 `JOIN`，MySQL 编译成 `INNER JOIN`；共享锁（`FOR SHARE` / `LOCK IN SHARE MODE`）、标识符引号、分页写法也不一样。只断言两种库相同的片段，或者比较前先归一（例如 `.replace("INNER JOIN", "JOIN")`）；只有 MySQL 才有的断言放进 `if engine.dialect.name == "mysql":` 分支
- 原因：Worker 的沙箱跑不了 `tests.backend`，参数化成 `[sqlite]` / `[mysql]` 的用例要到 CI 的 MySQL 分片才第一次真跑；AIH-TASK-051 run `7fe4822e` 就挂在这里（评审通过，CI 不过）
- **跨文件共用的 fixture 在 `tests/backend/conftest.py` 里注册**，定义放在不以 `test_` 开头的辅助模块（例如 `tests/backend/ai_mysql_world.py`）。不要在用例模块里写 `mysql_world = test_xxx.mysql_world` 这种转引用：漏写一行就是 `fixture 'mysql_world' not found`，同样只在 CI 才暴露（AIH-TASK-067 run `9e7e38aa`）。辅助模块被 conftest 导入时还没执行 `pytest_configure`（xdist 换库名），环境变量要在 fixture 里读，不能在模块顶层读
- **要确认「对方确实在等行锁」时查 `performance_schema.data_lock_waits`，不查 `information_schema.INNODB_TRX`。**INNODB_TRX 是一份不满 0.1 秒不刷新的缓存，而且不分库（xdist 各 worker 的库在同一台 MySQL 上），50 毫秒轮询可能一直读到旧快照、也可能数到别的 worker 的等待。联 `performance_schema.data_locks`，按 `DATABASE()` 与表名过滤（需要区分等待方时再经 `performance_schema.threads` 对上它的 `CONNECTION_ID()`），写法见 `tests/backend/test_session_revocation_concurrency.py` 的 `_WAITING_ON_TABLE`。AIH-TASK-131 run `07489d13` 的测试用 INNODB_TRX 等锁：Worker 的 PR 上碰巧过了，换一次 CI 就在 10 秒内一直没看到等待而失败（#496）

## 登记任务时的浏览器验收约定

登记任务时的浏览器验收约定（OpenClaw 浏览器验收契约）：
- 有用户可见页面的前端任务，登记时默认带 acceptance 块：沿用项目已有的验收命令，writes: false，按页面写 2–4 个只读步骤
  （小写下划线步骤名），timeout_seconds 不超过 300；验收命令不写进 allowed_commands；allowed_change_paths 加上验收脚本。
- 验收标准里写清每个新步骤验什么，通用约束一句「沿用验收脚本已有的约束」带过；脚本只执行 stdin 里 steps 列出的步骤。
- 纯后端、数据库、文档任务不带；运营者说「这个不验」时不带。
- 任务的标题、目的与验收标准会原样进 PR 正文并过泄漏规则：不写任何 xxx:// 形式的地址、主机名、邮箱地址。

本仓库已有的验收命令是 .platform/commands.yaml 里的 acceptance.browser（脚本 scripts/acceptance/admin_customers.mjs）。

## 登记任务的约定：标题、预审与拆分

完整说明在控制面仓库 `docs/ONBOARD-PROJECT.md` 的「登记任务之前」一节。

- **`title` 用一句中文**说明这一项做什么：80 个字符以内、一行、首尾无空白；不用反引号、尖括号、「」『』【】〖〗，不带 emoji（带变体选择符的会被拒）、链接、路径、主机名。OpenClaw 的推荐行显示成「下一项：<编号>，内容：<标题>」
- **按条件预审**：任务有设计闸门（碰钱、碰个人数据、状态机、改数据库结构或迁移），或 `contract_check` 对它给出拆分 WARN、预计改动接近约 1500 行时，登记为 `ready` 之前必须在控制面仓库运行

  ```bash
  python -m worker.design_precheck --repo <本仓库干净检出或 git archive 导出> --project-id ai_billing_hub --task <任务 id> --design <设计文件相对路径> --claude <claude 程序绝对路径>
  ```

  把完整输出贴进登记 PR；`NOT_READY` 的发现要么修掉，要么在 PR 里逐条写明为什么不成立。不满足条件的任务在登记 PR 里写一行「预审：不适用」并给出理由
- **拆分规则**：一个任务只做一层（数据库 / 业务规则 / 接口 / 前端），用 `depends_on` 串起来，每层合并后都能单独通过检查与 CI；规则多的设计先交付规则和对应测试再接接口；`allowed_change_paths` 超过 12 个、验收标准超过 8 条、或预计改动超过约 1500 行先拆，拆不开在登记 PR 里写明原因；预审报出的 `SCOPE_GAP` 先改契约

## 核心
**多想少动 · 知错即改 · 往正确的方向走**

## 高风险操作 - 必须先询问,禁止自行连续尝试

以下场景，执行一次失败后必须停止并向我汇报，禁止连续重试、更换参数后再试：
- SSH/远程登录尝试（换用户名、换密钥、换端口都算"重试"）
- 数据库连接、密码尝试
- 任何涉及认证/鉴权的连接测试
- 生产环境的删除、重启、清空操作
- 修改防火墙、fail2ban、安全组规则

规则：失败一次 = 停止 + 汇报现象 + 等待我的下一步指示。
不允许"既然A不行，那我试试B"的连续排查逻辑，除非我已经明确说"随便试到通为止"。

诊断任务的默认模式是"观察 + 汇报"，不是"排除故障直到成功"。
除非我说"帮我解决"或"你直接修"，否则默认只做只读检查。

## 回答格式
- 先给结论，一句话说完
- 用清单，一条数句，不写大段落
- 一次只推进一步，说完就停，等我回应——**但编号任务例外**：那些一口气做到「能跑通、能 commit」再回话，不要做一半停下来问
- 一条回复只讲一个主题
- 长内容写进文件，不铺在对话里
- 提问用编号清单，一条一个，方便我逐条回答

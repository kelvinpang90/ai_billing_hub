# WORKFLOW — 双 agent 开发流程

> Claude Code 主力开发 · Codex 独立审查 · Kelvin 最终验收。
> **这份是流程的唯一事实来源**，`CLAUDE.md` 与 `AGENTS.md` 里的角色说明都指回这里。
> 最后更新：2026-09-11

---

## 1. 角色

| 角色 | 是谁 | 能做 | 不能做 |
| --- | --- | --- | --- |
| **开发者** | Claude Code（本机） | 写设计、写代码与测试、开 PR、按审查意见修改 | 自己合并 |
| **审查者** | Codex（`codex exec -s read-only`） | 读设计与 diff、按清单审查、发判定 | **改任何文件、push、合并** |
| **验收者** | Kelvin | 触发审查、裁决分歧、点 Squash and merge | — |

分工的意义在于**审查者没有实现时的思维定势**。所以审查者只读不写是硬规则——一旦 Codex 动手改，它就变成第二个开发者，独立性没了。

> 「只读」现在是**沙箱强制的**，不是靠自觉：`codex exec -s read-only` 从机制上禁止写入。
> 唯一例外是它自己产出的 `.codex-review-*.md`（已被 `.gitignore` 忽略）。

## 2. 两个闸门

```text
                    Kelvin / GitHub Issue（任务与决策）
                              ↓
        ┌───────────── 闸门 A：设计（碰钱的任务必走）
        │   Claude 按 design-gate 模板开 Issue
        │        ↓
        │   Codex 冷读设计 → APPROVED: design vN / REQUEST_CHANGES
        │        ↓
        └── 通过后才开始写代码
                              ↓
              Claude 实现 + 写测试 + 写 TODO 任务记录
                              ↓
                        Draft PR
                              ↓
              确定性 CI：docs / secret-scan（+ 将来的 backend）
                              ↓
        ┌───────────── 闸门 B：实现
        │   Codex 冷读 diff → VERDICT: APPROVE / REQUEST_CHANGES
        │        ↓
        │   REQUEST_CHANGES → Claude 逐条修 → push → 回到 CI
        └── APPROVE → 转正式 PR
                              ↓
                   Kelvin Squash and merge
```

⚠️ **合并之后没有步骤。** `main` 受保护、不能直接推，任何「合并后再补记录」都得另开一个闸门外的 PR。所以**任务记录与审查教训必须在分支上完成**，随 PR 一起进 `main`。这也和 [TODO.md](TODO.md) 顶部「做完把 `[ ]` 改成 `[x]` 并补记录」的约定对齐。

## 3. 闸门 A：设计

### 什么任务要走

| 任务涉及 | 要填的范围 |
| --- | --- |
| 钱包 / 账本 / 定价 / 汇率 / 支付 / 幂等 / 状态机 | **全部章节** |
| 用量摄取 / 集成认证 / Webhook | §1–§7（**§6 不可省**：密钥、鉴权、租户过滤、日志脱敏都在那节） |
| 前端 / 文档 / CI / 脚本 | **不走**，直接开 PR |

分档是刻意的。11 节的模板套在「建一张表」上，填完比实现还久，**结果就是敷衍填——而敷衍的设计文档比没有更糟**，它制造「已经论证过」的假象。

### 流程

1. Claude 用 [design-gate 模板](../.github/ISSUE_TEMPLATE/design-gate.md) 开 Issue，打 `design-gate` 标签
2. 填完、无未决阻断假设 → 状态标 `READY_FOR_REVIEW`
3. 跑 `scripts\codex-review.ps1 -Issue <N> -Post`
4. Codex 回答五个问题 + 核对七条判定 → `APPROVED: design v<N>` 或 `REQUEST_CHANGES`
5. 通过后才开始写代码

### 设计版本绑定 —— 这条最容易被绕过

- 批准写成 **`APPROVED: design v<N>`**，`<N>` 取 Issue 顶部的「设计版本」
- **设计版本一变，之前的批准自动作废**，必须重新过闸门
- 什么算实质修改：改了契约、表结构、事务边界、状态机、失败语义、不变量控制。改错别字不算
- 实质修改时：顶部版本 +1，§12 追加变更说明，状态退回 `READY_FOR_REVIEW`

> 不设这条，就会出现「批准的是 v1、合并的是 v3」——PR #2 上真发生过类似情况：`APPROVE` 在两版改动之前给出，之后又叠了新改动。

## 4. 闸门 B：实现

1. Claude 实现 + 写测试
2. 本地全套检查通过（见 §7）
3. 在同一分支写好 [TODO.md](TODO.md) 的任务记录：勾掉 `[ ]`，写清做了什么、偏离了什么、验证到什么程度
4. push + **开 Draft PR**（用 [PR 模板](../.github/pull_request_template.md)，结构不许改）
   - 模板里的 `设计闸门：#N` 一行是**机器可读**的：`codex-review.ps1` 靠它把设计文档
     与批准记录一并取进审查材料。不走闸门的改动写「不适用」；触及钱包 / 账本 / 定价 /
     汇率 / 支付 / 幂等 / 状态机却写「不适用」的，审查时会被判为阻断项
5. **审查准入**：CI 三项（`docs` / `scripts` / `policy`）与 `secret-scan` 全绿，且 PR 正文过
   `scripts/check_repo_policy.py`（模板小节齐全、`设计闸门：` 一行、`TODO 影响` 声明）。
   `codex-review.ps1` 会先查这两样，**不满足就不调 Codex**——配额不花在注定被打回的材料上
6. 跑 `scripts\codex-review.ps1 -Pr <N> -Post`
7. `REQUEST_CHANGES` → Claude 发 `## 🔧 CLAUDE RESPONSE` 逐条回应（格式见 §6，**已修必须给 `完整 SHA · 文件:行`**）→ 修 → push → 回到 5
8. `VERDICT: APPROVE` → Draft 转正式 PR，等 Kelvin 合并

复审（第二轮起）时脚本会把**上轮审查、本轮回应、`reviewed-head..HEAD` 的增量 diff** 一并放进材料，
Codex 逐条核对回应，并把与修复无关的范围扩张判为阻断。**第三轮起回应里必须有 `### 完整影响面`**——
连续两轮被指出「还漏一项」，说明该停止逐项补洞、改成整体比对了（#5 六轮不收敛的教训）。

两条口径，都是为了让「同步 base」不被误判：

- **上轮 `APPROVE`、之后又推了提交**（含分支保护要求的 merge main）→ 复审只要增量，**不要求回应**——没有意见可回应，强制要一张非空表只能逼人凑数
- **增量 = 两版「PR 相对 base 的 diff」之间的差异**，不是 `reviewed-head..HEAD`。后者会把 merge main 带进来的所有改动算成范围扩张（实测 #7 同步一次 main 就是 13 文件 / +1340 行）

用 Draft PR 的理由：`main` 要求走 PR，草稿状态能防止在审查完成前被误合并。

## 5. 分支与 PR 约定

- 分支名：`task/<TODO 编号>-<短 slug>`，例如 `task/phase1-wallet-ledger`
- 非任务型改动用 `chore/` 或 `fix/` 前缀
- **一个 PR 一个任务**，不夹带
- 合并方式只能是 **Squash**（`main` 要求线性历史）
- ⚠️ **不要开 stacked PR**（base 指向另一个功能分支）。合并时基分支被删，GitHub 会自动关闭子 PR，**且关闭后既不能改 base 也不能重开**。这个坑踩过一次（PR #3）
- ⚠️ **审查开始后不要 rebase / force-push。**审查评论里的 `reviewed-head` 必须留在分支历史里，复审靠它算增量；rebase 会让它消失，脚本从此对这个 PR 永远退出 2，没有重定基线的路。需要同步 base 用 `git merge origin/main`——反正最后是 Squash，分支历史不进 `main`

## 6. 评论格式与判定

⚠️ Claude 与 Codex 目前**共用同一个 GitHub 账号**（`kelvinpang90`），PR 上所有评论都显示同一个头像。署名前缀是强制的，否则分不清谁说的。

| 谁 | 前缀 | 最后一行 |
| --- | --- | --- |
| Codex 审设计 | `## 🔍 CODEX REVIEW — 设计闸门` | `APPROVED: design v<N>` 或 `REQUEST_CHANGES` |
| Codex 审实现 | `## 🔍 CODEX REVIEW` | `VERDICT: APPROVE` 或 `VERDICT: REQUEST_CHANGES` |
| Claude 回应 | `## 🔧 CLAUDE RESPONSE` | — |

**首行必须是上表的署名前缀，判定必须是最后一行**，都别加别的字——`codex-review.ps1` 会解析判定行并据此设置退出码（0 = 通过，1 = 要改，2 = 出错或格式不对）。

发布前脚本对前缀的校验，调用的就是**读取批准记录时用的那个函数**。两边各写一份的话，会发布一条脚本认为合法、而读取方判为「不是审查」的批准：设计闸门显示通过、实现审查却说该设计未批准，两端各说各话且都不报错。

发布前脚本会在判定行**之前**插一行：

```text
reviewed-head: <被审的那个 commit SHA>

VERDICT: APPROVE
```

**一次判定只对它审的那个提交有效。** 之后再 push，这行就和当前 head 对不上，旧的通过判定不再作数，必须重跑。没有这行的话，一次通过会被后续未审查的提交沿用下去。

脚本还会在**审查前后各校验一次**基线：工作区是否干净、本地 `HEAD`、PR head。审查期间有新提交或改动，判定一律不发布——它指向的已经是另一版代码了。（`git status` 查不出「切到另一个干净的提交」，所以本地 `HEAD` 要单独比。）

发布前还会**重新取一次审查材料，逐段比对**：标题、PR 正文、关联设计、diff，逐字不变才允许发布。前面那些是「想到了才查得到」的专项检查——设计正文原地改写而版本不变、PR 正文改成关联另一个设计、base 前进导致 diff 变化，都绕得过。逐段比对是兜底。

材料取好后还会做完整性校验：出现替换字符（编码坏了）、找不到标题原文、或一行 `diff --git` 都没有，都当场失败。**不能让审查方对着乱码给判定。**

Claude 的回应用表格，每条意见都要有交代，且**机器校验**（`check_repo_policy.py --response-file`，复审取材时自动跑）：

```text
## 🔧 CLAUDE RESPONSE

reviewed-head: <上轮审查评论里那个 40 位 SHA>

| 意见 | 处理 | 证据 | 验证 |
| --- | --- | --- | --- |
| 阻断项 1 | 已修 | <40 位 commit SHA> · docs/TODO.md:27 | 本地三项检查通过 |
| 建议项 2 | 不改 | spec §41 已规定 | 引用核对过 |
| 建议项 3 | 升级 | 见 PR 里的两方案取舍 | 等 Kelvin 拍板 |
```

- `处理` 只有三种：`已修` / `不改` / `升级`。别的词（「稍后」「记下了」）= 沉默跳过，校验直接拒绝
- **`已修` 的证据必须是 `完整 SHA · 文件:行`**，脚本会到那个提交上确认该文件该行真的存在、且该提交在当前 head 的历史里。
  这条是冲着「声称已写进 X，实际没写」来的——那类在本仓库出现过 3 次。要填行号就得打开文件看一眼，验证从「记得做」变成「不做就填不出来」
- 短 SHA 不收：仓库一大就歧义

⚠️ **SHA 与行号只证明引用存在，不证明问题修好了。**它们让审查者能一跳到位，「确实修好」「没有夹带」仍然要由复审判断。

### 写后回读

所有发到 GitHub 的写操作（发审查评论、发回应、改 PR 正文）走 `scripts/gh_verified_write.py`：按参数数组调 `gh api`，写完**按对象 id 回读比对**，不一致就退出 2。
起因是一次 `gh pr edit --body-file` 报成功、实际把原文写了回去——工具说成功不算数，**回读一致才算**。

回读失败时**不得自动重发**：POST 失败也可能已经创建了评论，先去远端核对。

### 工具失败 = 未完成审查

`gh` 起不来、JSON 解析失败、策略检查报错（退出码 2）、回读不一致——这些都是「这次审查没有完成」，**不是**「审查要求修改」，更不是「通过」。脚本把它们统一归到退出码 2；自动化不得把 2 当成任何一种判定去处理。

## 7. 提 PR 前必须本地跑过

```bash
python scripts/check_docs.py
python scripts/check_repo_policy.py
python -m unittest discover -s tests
```

三项分别管：链接与 `§N` 引用 + 约定串一致性 / 任务复选框与 PR 字段 / 策略脚本与回读脚本自身的回归。

Phase 0 建好 `app/` 与 `tests/` 之后，这里会补上 lint 与 pytest。**跑不过就不许 push**——让 CI 替你发现本地能发现的问题是浪费一轮。

## 8. 怎么跑审查

```powershell
# 审实现，只看不发
.\scripts\codex-review.ps1 -Pr 5

# 审实现并发到 PR
.\scripts\codex-review.ps1 -Pr 5 -Post

# 审设计
.\scripts\codex-review.ps1 -Issue 12 -Post
```

脚本做三件事：**取材料 → 调 Codex 分析 → 发结果**。

⚠️ **Codex 不碰网络，也不调 `gh`。** `codex exec -s read-only` 的沙箱会挡住 `%APPDATA%\GitHub CLI\config.yml`，`gh` 在里面根本起不来（放宽沙箱试过，`disk-full-read-access` 也不行）。所以由脚本先把 PR 描述与完整 diff 取好、落成 `.codex-input-*.md`，Codex 只做纯分析。

这不只是绕过限制，架构上也更好：

- 审查者的权限面收到最小 —— 只读本仓库，不需要任何网络能力
- **审查输入是一个可检视的文件**，事后能确认它到底看了什么，而不是只能相信它「取到了正确的 diff」
- 少几轮 agent 往返，快

**同一个脚本，谁都能跑。** Kelvin 在自己终端跑就能实时看到全过程；Claude 也能调它。**审查内容不会因为谁按的回车而变**——提示词与清单都在仓库里、受版本控制、在 PR 里可审。这是防止 Claude 临时把提示词写得偏向自己的唯一保障，**不要把提示词挪到脚本外面临时拼**。

## 9. 已知约束

| 约束 | 影响 | 现状 |
| --- | --- | --- |
| `codex exec` 的会话不进 Codex desktop 与 CLI 的会话列表 | 想看过程只能看终端输出 | 在终端跑脚本即可实时看 |
| read-only 沙箱挡住 `%APPDATA%`，`gh` 在沙箱内不可用 | Codex 自己取不了 PR 内容 | 脚本先取材落盘，Codex 只读本地文件（见 §8） |
| 两边共用一个 GitHub 账号 | Codex **无法**给出正式的 GitHub APPROVE（GitHub 不允许 approve 自己的 PR），只能发评论 + 判定行 | 接受。闸门在 Kelvin 手上，功能等价 |
| Codex 跑 PowerShell | 给它的命令不能用 `wc` / `grep` / `head` 这类 unix 工具 | 脚本里已规避 |
| 同账号导致评论难分辨 | 靠 `## 🔍` / `## 🔧` 前缀区分 | §6 已约定 |
| 两个角色是同一个模型 | 换会话只清空记忆，**没换掉先验** | 见下 |

### 关于「同一个模型」的独立性缺口

设计闸门里 Codex 是**冷读**的（它没参与设计），所以这条不影响当前流程。但如果将来引入「Codex 同事」参与方案讨论或测试设计，那部分产物必须在 PR 描述里**标注来源**，审查方要显式声明先验可能重合、请人复核。

**不假装独立性还在，而是把缺口标出来。**

### 可选升级（不急）

给 Codex 单独建一个 GitHub 账号加为 collaborator：能出正式 APPROVE、可把「必须 1 个审批」设成分支保护硬规则、两个身份天然分得清。代价是多管一个账号。

## 10. 分歧怎么办

Claude **不许**沉默跳过任何一条意见。只有三种处理：

1. **改** —— 在回应表里写 commit sha
2. **不改，说明理由** —— 理由要具体（引 spec 章节、引不变量、引已有约定）
3. **升级给 Kelvin** —— 两边都说不服对方时，在 PR 里说清两种方案各自的取舍，等裁决

第 3 种的结论**必须**写进 [REVIEW-LOG.md](REVIEW-LOG.md) 的「升级给人的分歧」，避免同一个争论反复发生。裁决之后不要在后续对话里重提——把理由和代价写进 ADR，让它成为可查的记录，然后按决策执行。

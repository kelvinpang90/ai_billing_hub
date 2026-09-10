# WORKFLOW — 双 agent 开发流程

> Claude Code 主力开发 · Codex 独立审查 · Kelvin 最终验收。
> **这份是流程的唯一事实来源**，`CLAUDE.md` 与 `AGENTS.md` 里的角色说明都指回这里。
> 最后更新：2026-09-10

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
5. CI 跑绿
6. 跑 `scripts\codex-review.ps1 -Pr <N> -Post`
7. `REQUEST_CHANGES` → Claude 发 `## 🔧 CLAUDE RESPONSE` 逐条回应 → 修 → push → 回到 5
8. `VERDICT: APPROVE` → Draft 转正式 PR，等 Kelvin 合并

用 Draft PR 的理由：`main` 要求走 PR，草稿状态能防止在审查完成前被误合并。

## 5. 分支与 PR 约定

- 分支名：`task/<TODO 编号>-<短 slug>`，例如 `task/phase1-wallet-ledger`
- 非任务型改动用 `chore/` 或 `fix/` 前缀
- **一个 PR 一个任务**，不夹带
- 合并方式只能是 **Squash**（`main` 要求线性历史）
- ⚠️ **不要开 stacked PR**（base 指向另一个功能分支）。合并时基分支被删，GitHub 会自动关闭子 PR，**且关闭后既不能改 base 也不能重开**。这个坑踩过一次（PR #3）

## 6. 评论格式与判定

⚠️ Claude 与 Codex 目前**共用同一个 GitHub 账号**（`kelvinpang90`），PR 上所有评论都显示同一个头像。署名前缀是强制的，否则分不清谁说的。

| 谁 | 前缀 | 最后一行 |
| --- | --- | --- |
| Codex 审设计 | `## 🔍 CODEX REVIEW — 设计闸门` | `APPROVED: design v<N>` 或 `REQUEST_CHANGES` |
| Codex 审实现 | `## 🔍 CODEX REVIEW` | `VERDICT: APPROVE` 或 `VERDICT: REQUEST_CHANGES` |
| Claude 回应 | `## 🔧 CLAUDE RESPONSE` | — |

**判定必须是最后一行**，别加别的字——`codex-review.ps1` 会解析它并据此设置退出码（0 = 通过，1 = 要改，2 = 出错或格式不对）。

发布前脚本会在判定行**之前**插一行：

```text
reviewed-head: <被审的那个 commit SHA>

VERDICT: APPROVE
```

**一次判定只对它审的那个提交有效。** 之后再 push，这行就和当前 head 对不上，旧的通过判定不再作数，必须重跑。没有这行的话，一次通过会被后续未审查的提交沿用下去。

脚本还会在**审查前后各校验一次**基线（PR head、工作区是否干净）。审查期间有新提交或改动，判定一律不发布——它指向的已经是另一版代码了。

材料取好后还会做完整性校验：出现替换字符（编码坏了）、找不到标题原文、或一行 `diff --git` 都没有，都当场失败。**不能让审查方对着乱码给判定。**

Claude 的回应用表格，每条意见都要有交代：

```text
## 🔧 CLAUDE RESPONSE

| 意见 | 处理 | 说明 |
| --- | --- | --- |
| 阻断项 1 | 已修 | <commit sha> |
| 建议项 2 | 不改 | <理由> |
```

## 7. 提 PR 前必须本地跑过

```bash
python scripts/check_docs.py
```

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

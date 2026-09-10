# WORKFLOW — 双 agent 开发流程

> Claude Code 主力开发 · Codex 独立审查 · 你最终验收。
> **这份是流程的唯一事实来源**，`CLAUDE.md` 与 `AGENTS.md` 里的角色说明都指回这里。
> 最后更新：2026-09-10

---

## 1. 角色

| 角色 | 是谁 | 能做 | 不能做 |
| --- | --- | --- | --- |
| **开发者** | Claude Code（本机） | 读 TODO 取任务、写代码与测试、开 PR、按审查意见修改 | 自己合并 PR |
| **审查者** | Codex desktop（本机） | 读 diff、按清单审查、把意见发到 PR | **改任何代码、push、合并** |
| **验收者** | 你 | 触发审查、最终判断、点 Squash and merge | — |

分工的意义在于**审查者没有实现时的思维定势**。所以审查者只读不写这条是硬规则——一旦 Codex 开始动手改，它就变成了第二个开发者，独立性没了。

## 2. 闭环

```text
 1  Claude 读 docs/TODO.md，取下一个编号任务
 2  git checkout -b task/<编号>-<slug>
 3  实现 + 写测试
 4  本地全套检查通过（见 §5）
 5  push + gh pr create（用 PR 模板）
 6  CI 跑：docs / secret-scan（+ 将来的 backend）
 ────────────────────────────────────────────
 7  ★ 你在 Codex desktop 粘 §6 那段指令，启动审查
 8  Codex 按 scripts/review_checklist.md 审 diff，
    用 gh pr comment 发结构化意见，末行给 VERDICT
 ────────────────────────────────────────────
 9  Claude 读 gh pr view --comments，
    逐条修 或 逐条说明为什么不改（不许沉默跳过）
10  回到 6，直到 VERDICT: APPROVE
11  你 Squash and merge
12  Claude 回写 docs/TODO.md 的任务记录 + docs/REVIEW-LOG.md 的教训
```

**第 7 步是唯一的人工触发点。** Codex desktop 是本地应用，不会被 GitHub webhook 叫醒，这是这套形态的天花板，绕不过去。所以指令固定成下面那段，你复制粘贴即可。

## 3. 分支与 PR 约定

- 分支名：`task/<TODO 编号>-<短 slug>`，例如 `task/phase1-wallet-ledger`
- 非任务型改动用 `chore/` 或 `fix/` 前缀
- **一个 PR 一个任务**，不要顺手夹带
- PR 描述用 `.github/pull_request_template.md`，**结构不许改**——Codex 靠它定位改动意图
- 合并方式只能是 **Squash**（`main` 要求线性历史）

## 4. 评论格式约定

⚠️ Claude 与 Codex 目前**共用同一个 GitHub 账号**（`kelvinpang90`），PR 上所有评论都显示同一个头像。所以署名前缀是强制的，否则谁也分不清哪句是谁说的。

审查意见（Codex 发）：

```text
## 🔍 CODEX REVIEW

**结论**：<一句话>

### 阻断项
- `文件:行` — 问题 → 后果

### 建议项
- `文件:行` — 问题 → 建议

### 清单核对
- 不变量：<触碰了第几条，是否保住>
- DoD：<哪几条未满足>
- 测试：<改动引入的边界是否被覆盖>

---
VERDICT: REQUEST_CHANGES
```

回应（Claude 发）：

```text
## 🔧 CLAUDE RESPONSE

| 意见 | 处理 | 说明 |
| --- | --- | --- |
| 阻断项 1 | 已修 | <commit sha> |
| 建议项 2 | 不改 | <理由> |
```

**`VERDICT:` 必须是最后一行**，取值只有 `APPROVE` 或 `REQUEST_CHANGES`。这一行是机器可读的，别加别的字。

## 5. 提 PR 前必须本地跑过

```bash
python scripts/check_docs.py
```

Phase 0 建好 `app/` 与 `tests/` 之后，这里会补上 lint 与 pytest。**跑不过就不许 push**——让 CI 替你发现本地能发现的问题是浪费一轮。

## 6. 启动审查：复制这段给 Codex desktop

> Codex desktop 跑的是 **PowerShell**，下面的命令都是 PowerShell 兼容的。
> 不要往里加 `wc` / `grep` / `head` 这类 unix 工具，会直接报 not recognized。

```text
你是本仓库的独立审查者，不是开发者。

硬规则：
- 只读。不修改任何文件，不 git add / commit / push，不合并 PR。
- 只报你能指出具体位置和具体后果的问题。指不出后果的观感问题不要写。
- 不要重复 linter 和 CI 已经能抓的东西（格式、import 顺序、拼写）。

步骤：
1. 执行 gh pr list --state open --json number,title --repo kelvinpang90/ai_billing_hub
   取到待审的 PR 编号，下面记作 N。
2. 读 scripts/review_checklist.md，这是本项目的审查清单，逐条走。
3. 执行 gh pr view N --json title,body 和 gh pr diff N 拿到改动意图与 diff。
4. 需要上下文时读 docs/ARCHITECTURE.md（14 条不变量）与
   docs/Acuven_Central_AI_Billing_Platform_Spec_v1.1.md 的相关章节。
5. 把结果写进 .codex-review-N.md，严格用 docs/WORKFLOW.md 第 4 节的格式，
   最后一行必须是 VERDICT: APPROVE 或 VERDICT: REQUEST_CHANGES。
6. 执行 gh pr comment N --body-file .codex-review-N.md
7. 删除 .codex-review-N.md

只要有一条阻断项，VERDICT 就必须是 REQUEST_CHANGES。
没有阻断项但有建议项时，VERDICT 可以是 APPROVE。
```

## 7. 已知约束

| 约束 | 影响 | 现状 |
| --- | --- | --- |
| Codex desktop 不响应 webhook | 每轮审查要你手动触发一次 | 接受，是形态决定的 |
| 两边共用一个 GitHub 账号 | Codex **无法**给出正式的 GitHub APPROVE（GitHub 不允许 approve 自己的 PR），只能发评论 + `VERDICT:` 行 | 接受。闸门本来就在你手上，功能等价 |
| Codex 跑 PowerShell | 给它的命令不能用 unix 管道工具 | 已在第 6 节的指令里规避 |
| 同账号导致评论难分辨 | 靠 `## 🔍` / `## 🔧` 前缀区分 | 第 4 节已约定 |

### 可选升级（不急）

给 Codex 单独建一个 GitHub 账号，加为本仓库 collaborator。收益：

- Codex 能出**正式的** GitHub APPROVE
- 可以把「必须 1 个审批」设成分支保护的硬规则，审查从「约定」升级成「机制」
- PR 上两个身份天然分得清

代价：多管一个账号、多一次授权。等这套流程跑顺了再考虑。

## 8. 审查意见有分歧怎么办

Claude **不许**沉默跳过任何一条意见。只有三种处理：

1. **改** —— 在回应表里写 commit sha
2. **不改，说明理由** —— 理由要具体（引 spec 章节、引不变量、引已有约定）
3. **升级给你** —— 两边都说不服对方时，在 PR 里 `@` 你，说清楚两种方案各自的取舍，等你拍板

第 3 种情况的结论必须写进 [REVIEW-LOG.md](REVIEW-LOG.md)，避免同一个争论反复发生。

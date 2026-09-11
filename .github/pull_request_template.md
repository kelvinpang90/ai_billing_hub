<!--
结构不要改。Codex 审查时靠这几个小节定位改动意图。
流程见 docs/WORKFLOW.md
-->

## 任务

<!-- docs/TODO.md 里的编号任务，或说明这是临时改动 -->

设计闸门：#N

<!--
上面这一行是**机器可读**的，scripts/codex-review.ps1 靠它把设计文档与
批准记录一并取进审查材料。格式必须是「设计闸门：#编号」。

不走设计闸门的改动（前端 / 文档 / CI / 脚本，以及既不碰钱、也不碰用量摄取 /
集成认证 / Webhook 的后端改动），把 #N 改成「不适用」。

⚠️ 触及钱包 / 账本 / 定价 / 汇率 / 支付 / 幂等 / 状态机却写「不适用」的，
审查时会被判为阻断项。

分档看**主题**，不看「是不是 API」：新增一个端点本身不构成走闸门的理由。
-->

## 改了什么

<!-- 按文件或按模块列，一条一句。不要贴 diff。 -->

## TODO 影响

<!--
机器可读（scripts/check_repo_policy.py 会校验，审查脚本在调 Codex 之前也会校验）。
本仓库最常见的缺陷是「改了 ADR / 结论，没改 docs/TODO.md」，所以每个 PR 都要显式声明。

二选一，多余的行删掉：

  TODO impact: updated
  TODO target: docs/TODO.md:<行号>      ← 本 PR 改动的那一条所在行，按 PR head 计

  TODO impact: none
  TODO reason: <为什么这次改动不影响任何任务状态>   ← 不接受「无」「不适用」
-->

TODO impact: updated
TODO target: docs/TODO.md:<行号>

## 触碰的不变量 / REQ

<!--
列出这次改动碰到的 14 条不变量（docs/ARCHITECTURE.md 第 6 节）
与 REQ-* ID（docs/REQUIREMENTS.md 第一节），并说明怎么保住的。
一条都没碰就写「无」。
-->

## 如何验证

<!--
具体到命令与预期输出。「跑了测试」不算，要写跑了什么、看到什么。
-->

## 自检

- [ ] 本地检查全过（命令清单见 [WORKFLOW §7](../docs/WORKFLOW.md)）
- [ ] 金额相关代码全用 `Decimal`，无 `float`
- [ ] 涉及查询的地方都带 tenant 过滤
- [ ] 新增的失败路径不会静默吞掉事件
- [ ] 没有提交任何凭据、`.env`、真实主机名 / IP、客户数据
- [ ] spec §132 的 DoD 逐条过了一遍（不适用的说明为什么）

## 已知未做 / 留给后续

<!-- 刻意没做的部分，写清楚为什么。没有就写「无」。 -->

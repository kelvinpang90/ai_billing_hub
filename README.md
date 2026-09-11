# Acuven Central AI Billing Platform

给 Acuven 旗下所有 AI 应用做一套独立的中心化计费平台：**请求级用量计量 + 预付 MYR 钱包 + 不可变财务账本 + 支付网关充值 + 自动扣费与停复机**。

> **当前状态：Phase 0 之前，尚无产品代码。** 本仓库目前只有规格、架构决策与开发流程工具。
> 详细进度与下一步见 [docs/HANDOFF.md](docs/HANDOFF.md)。

---

## 从哪里开始读

| 你想做什么 | 读这个 |
| --- | --- |
| 第一次接触这个项目 | [docs/PROJECT.md](docs/PROJECT.md) —— 项目总览与**完整文档地图** |
| 知道现在做到哪、下一步是什么 | [docs/HANDOFF.md](docs/HANDOFF.md) |
| 找具体任务与验收标准 | [docs/TODO.md](docs/TODO.md) |
| 写代码前理解架构与财务不变量 | [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) |
| 需要精确定义 | [spec v1.2](docs/Acuven_Central_AI_Billing_Platform_Spec_v1.4.md) —— **唯一事实来源** |
| 开 PR / 做审查 | [docs/WORKFLOW.md](docs/WORKFLOW.md) |

## 提 PR 前必须本地跑过

```bash
python scripts/check_docs.py
python scripts/check_repo_policy.py
python -m unittest discover -s tests
```

PowerShell 侧另有 `pwsh -NoProfile -File scripts/tests/Test-ReviewVerdict.ps1`。

CI 跑四项：`docs` / `scripts` / `policy` / `secret-scan`，四项都是 `main` 的必需检查。

## 开发流程

Claude Code 主力开发 · Codex 独立审查 · Kelvin 最终验收。碰钱的改动（钱包 / 账本 / 定价 / 汇率 / 支付 / 幂等 / 状态机）要先过**设计闸门**，再过**实现闸门**。完整规则见 [docs/WORKFLOW.md](docs/WORKFLOW.md)。

## ⚠️ 本仓库是公开的

绝不提交凭据、密钥、`.env`、真实主机名 / IP、客户数据、供应商合同价。仓库转公开的决策与代价见 [ADR-0001](docs/adr/ADR-0001-repository-visibility.md)。

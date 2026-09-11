# PROJECT — Acuven Central AI Billing Platform

> 项目总览与文档地图。**新人 / 新 session 从这里开始读。**
> 最后更新：2026-09-11

---

## 一句话

给 Acuven 旗下所有 AI 应用做一套独立的中心化计费平台：请求级用量计量 + 预付 MYR 钱包 + 不可变财务账本 + 支付网关充值 + 自动扣费与停复机。

## 背景

Acuven 目前运营多个面向租户的 AI 应用（`acuven_aichat`、`ai_chatbot_demo`、`rs-roof-pms` 等），每个都是独立部署的 Python + FastAPI + MySQL 后端，各自直接调用 AI 供应商（当前主力 Anthropic Claude，另有 OpenAI 语音转写）。

问题：没有统一的用量计量、成本核算和向客户收费的能力。

做法：**不在每个应用里各写一套计费逻辑**，而是建一个中心平台，各应用通过本地事务性 outbox 异步上报用量。

## 铁律

> **中心计费平台挂掉，绝不能中断客户的 AI 服务。**（`REQ-AVAIL-001`）

这条决定了整个架构：用量上报必须异步，计费 API 绝不能出现在终端用户 AI 请求的同步路径上。任何违反此原则的实现都视为架构错误。

## 当前状态

| 项 | 状态 |
| --- | --- |
| 规格文档 | ✅ v1.1（140 节，已按 v1.0 评审意见修订） |
| 规格评审 | ✅ 已完成（针对 v1.0，25 条意见） |
| 评审落实核对 | ✅ 已完成（20 条已解决 / 5 条残留 R1–R5） |
| 架构决策记录（ADR） | 🔶 ADR-0001～0007 已写（D1、D3、D4、D5、R2 收口，D6 部分收口）；**剩 D2 SST、D7 通知通道** |
| 代码 | ❌ 未开始，Phase 0 尚未启动 |
| 数据库 schema | ❌ 未开始 |
| git 仓库 | ✅ `github.com/kelvinpang90/ai_billing_hub`（**公开**，`main` 已配分支保护） |

**当前所处阶段：Phase 0 之前**。下一步见 [HANDOFF.md](HANDOFF.md)。

## 文档地图

全仓库 Markdown 按**作用**分七组。路径相对仓库根目录。

### A. 入口 —— 迷路时从这里进

| 文件 | 作用 | 什么时候读 |
| --- | --- | --- |
| `docs/PROJECT.md`（本文件） | 项目总览、文档地图 | 第一次接触项目 |
| [docs/HANDOFF.md](HANDOFF.md) | 现在在哪、下一步、未决问题、环境状态 | **每个 session 开始时** |
| [docs/TODO.md](TODO.md) | Phase 0–8 任务清单与验收、前置决策 D1–D7、评审残留 R1–R7 | 每个 session 开始时 |

### B. 唯一事实来源

| 文件 | 作用 | 什么时候读 |
| --- | --- | --- |
| [docs/Acuven_Central_AI_Billing_Platform_Spec_v1.3.md](Acuven_Central_AI_Billing_Platform_Spec_v1.3.md) | 140 节完整规格。**所有冲突以它为准** | 需要精确定义时 |

### C. spec 的导航层（不重复定义需求，只指路）

| 文件 | 作用 | 什么时候读 |
| --- | --- | --- |
| [docs/ARCHITECTURE.md](ARCHITECTURE.md) | 架构摘要、技术栈、**14 条财务不变量**、目录结构（第 9 节） | 动手写任何代码之前 |
| [docs/REQUIREMENTS.md](REQUIREMENTS.md) | `REQ-*` 追溯表 + 140 节按主题分组导航 | 找某条具体需求在 spec 哪一节 |

> 这两份是导航层。与 spec 冲突时，**以 spec 为准**。

### D. spec 的演进史（想知道「为什么长这样」时读）

| 文件 | 作用 | 什么时候读 |
| --- | --- | --- |
| [docs/SPEC_REVIEW_v1.0.md](SPEC_REVIEW_v1.0.md) | 对 v1.0 的评审意见（P0×7 / P1×10 / P2×8） | 想知道某个设计为什么这么定 |
| [docs/REVIEW_FOLLOWUP_v1.1.md](REVIEW_FOLLOWUP_v1.1.md) | 25 条意见在 v1.1 的落实核对（20 解决 / 5 残留） | 想知道某条意见到底改没改 |

### E. 决策记录

| 文件 | 作用 | 什么时候读 |
| --- | --- | --- |
| [docs/adr/README.md](adr/README.md) | ADR 的写法约定、**修订规则**、索引、待写清单 | 要写新 ADR 或改已有 ADR 时 |
| `docs/adr/ADR-0001` … `ADR-0009` | 逐项架构决策。索引与状态见上一行 | 想知道某个决策为什么这么定 |

> ⚠️ 有四份是**部分接受**（0004 / 0006 / 0008 / 0009），各自留了显式待补项，别当成已定案。

### F. 双 agent 流程

| 文件 | 作用 | 什么时候读 |
| --- | --- | --- |
| [docs/WORKFLOW.md](WORKFLOW.md) | **流程的唯一事实来源**：两个闸门、评论格式、回应表、写后回读、退出码契约 | 开 PR、审查、回应意见时 |
| [docs/REVIEW-LOG.md](REVIEW-LOG.md) | 审查记账：反复出现的问题、已达成的约定、机械化对照表 | 想知道某个做法为什么定成这样 |
| [scripts/review_checklist.md](../scripts/review_checklist.md) | Codex 审查时逐条走的清单（A 不变量 / B DoD / C 财务 / D 通用 / E 文档CI脚本 / F 复审） | 审查时；写代码时可当自查表 |
| [CLAUDE.md](../CLAUDE.md) | **Claude（开发者）的指令文件** | 由 Claude Code 自动加载 |
| [AGENTS.md](../AGENTS.md) | **Codex（审查者）的指令文件** | 由 Codex 自动加载 |

> ⚠️ `CLAUDE.md` 与 `AGENTS.md` **不是同一内容的两个副本**（2026-09-11 起各自独立）。两边必须一致的只有评论署名前缀与判定行，由 `check_docs.py` 的约定串规则机械保证。

### G. GitHub 模板（机器可读，结构不要改）

| 文件 | 作用 | 什么时候读 |
| --- | --- | --- |
| [.github/pull_request_template.md](../.github/pull_request_template.md) | PR 正文结构。`设计闸门：` 行与 `TODO 影响` 节会被 `check_repo_policy.py` 校验 | 开 PR 时 |
| [.github/ISSUE_TEMPLATE/design-gate.md](../.github/ISSUE_TEMPLATE/design-gate.md) | 设计闸门 Issue 模板 | 碰钱的任务写代码前 |

## 硬性约束

- **货币**：仅 MYR。供应商价目表是 USD，因此汇率必须版本化（详见 spec §5、§17.1）。
- **金额计算**：全部用 Python `Decimal`，禁止 `float`。
- **隐私**：AI 对话内容**不进**计费平台，只存元数据。
- **部署**：V1 单 VPS + Docker Compose，无独立 staging 环境。
- **仓库是公开的**：⚠️ 绝不可提交任何凭据、密钥、`.env`、真实主机名/IP、客户数据或供应商合同价。生产配置一律走 GitHub Secrets 与主机密钥管理。
- **钱包**：一个租户一个共享钱包，租户下所有 project 从同一钱包扣费。

## V1 范围外

客户端多用户 RBAC、自动退款、LHDN e-Invoice、多币种、信用卡自动充值、中心 AI 网关、存对话内容、客户自带密钥（BYOK）、复杂后付费开票、Kubernetes、微服务拆分。

完整列表见 spec §121，未来兼容性考虑见 §122。

## 与 E:\projects 其他项目的关系

- **首个试点**：`E:\projects\ai_chatbot_demo`（Phase 3）。它已有请求级 Anthropic 用量观测和基础 `conversation_id`，但**没有**持久化 outbox、没有计费凭据与签名、没有本地状态同步。Phase 3 是**扩展现有窄接口**，不是推倒重来。
- **共享基建**：`vps_infra` 提供 `infra_nginx` / `infra_mysql` / `infra_redis`。本平台跑的是钱包余额和财务账本，spec §98 已定案：**使用专用 MySQL / Redis 容器，不与现有应用共享实例**，即不接 `infra_mysql` / `infra_redis`。只剩落成 ADR 记录理由。
- **WhatsApp 通知**：复用 `whatsapp_gateway` 还是自建出站通道，未定（TODO 的 D7）。

## 命名

仓库目录 `ai_billing_hub` 为准。spec §101 里出现过 `acuv-ai-billing` 这个旧名，忽略之。

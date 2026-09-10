# ADR-0001 — 仓库可见性：公开而非私有

| | |
| --- | --- |
| **状态** | 已接受 |
| **日期** | 2026-09-10 |
| **决策人** | Kelvin Peng |
| **影响** | spec §99「Production source and delivery」，[ARCHITECTURE.md](../ARCHITECTURE.md) §7 |

---

## 背景

spec v1.1 §99 要求生产源码与交付使用：

- private GitHub repository
- protected `main` branch
- CI must pass before merge/deployment

2026-09-10 建仓后尝试配置 `main` 分支保护，两个 API 都被拒：

```text
GET /repos/{owner}/{repo}/branches/main/protection
GET /repos/{owner}/{repo}/rulesets
→ 403 Upgrade to GitHub Pro or make this repository public to enable this feature.
```

账号 `kelvinpang90` 无付费计划。**GitHub Free 的私有仓库不支持分支保护**（经典 branch protection 与新版 rulesets 均不支持）。

于是 §99 的两条要求在 GitHub Free 上互斥：保住「private」就拿不到「protected `main` + CI must pass before merge」，反之亦然。这不是能同时满足的取舍。

## 决策

**仓库设为公开**，换取分支保护与必需状态检查。

`main` 实际配置：

| 规则 | 状态 |
| --- | --- |
| 禁止 force push | 启用 |
| 禁止删除分支 | 启用 |
| 合并前必须走 PR | 启用（0 审批，单人开发） |
| 必需状态检查 | `docs`、`secret-scan` |
| 分支必须为最新 | 启用 |
| 线性历史 | 启用 |
| 管理员同样受限 | 启用 |

spec 同步修订至 **v1.2**，§99 的「private GitHub repository」改为公开并说明原因，使唯一事实来源与实际一致。

## 备选方案

### A. 升级 GitHub Pro（约 US$4/月），保持私有

同时满足 §99 两条要求，无偏离，spec 无需修订。

**未采用。** 决策人选择不引入订阅成本。

> 记录在案：本 ADR 的起草者（Claude）曾两次建议选 A，理由见「后果」一节。决策人在了解代价后确认选择公开。此处如实记录分歧，不代表结论有误——这是决策人的判断权限。

### B. 不做分支保护，只跑 CI

零成本，保持私有。但 CI 只能报告结果，无法阻止合并，`main` 可被直接 force push。

**未采用。** 对一个跑真钱、要求可审计的系统，`main` 不设防说不过去，且直接违反 §99 的「CI must pass before merge」。

### C. GitHub Team

单人使用时与 Pro 等价但按人计费，无额外收益。**未采用。**

## 后果

### 正面

- 拿到 §99 要求的受保护 `main` + 必需状态检查，且零成本
- GitHub Actions 在公开仓库不计入分钟额度
- 外部可见性对招聘、技术展示有一定价值

### 负面 —— 这些是已知且已接受的代价

仓库内容**永久公开**。改回私有不能收回已被 clone、fork、搜索引擎与归档站抓取的内容。具体暴露：

| 内容 | 暴露了什么 |
| --- | --- |
| spec 全文（140 节） | 完整数据库表结构与字段、HMAC 规范化签名串定义、密钥加密与轮换方案、钱包并发与幂等机制、部署拓扑、RPO/RTO、监控告警阈值 |
| `SPEC_REVIEW_v1.0.md` | **一份「这个系统在哪里会算错钱」的清单**：跨租户 event_id 投毒、支付金额不符、余额边界死锁、乱序 webhook，写明了成因 |
| `REVIEW_FOLLOWUP_v1.1.md` | **5 条至今未解决的残留**（R1–R5），等于公开当前薄弱点 |
| 导航层文档 | `acuventech.com` 各子域、`vps_infra` 共享基建结构、关联项目名 |
| 商业信息 | 定价策略机制（MARKUP / FIXED_RATE）、毛利计算口径、供应商成本对账方式 |

派生要求：

- **绝不可提交**任何凭据、密钥、`.env`、真实主机名 / IP、客户数据、供应商合同价。CI 的 `secret-scan` job 是兜底，不是许可
- 未来若有客户数据样例、真实成本数据、合同条款，必须放在本仓库之外
- PR、Issue、commit message 同样公开，讨论中不得出现上述内容

### 缓解措施（可选，尚未采纳）

- 把 `SPEC_REVIEW_v1.0.md` 与 `REVIEW_FOLLOWUP_v1.1.md` 移出本仓库历史，转入私有仓库或本地 —— 这两份的敏感度高于 spec 本身
- 生产上线前重新评估是否转回私有（届时可能已有付费计划）

## 收口条件

本 ADR 生效即完成收口。判定标准：

- [x] spec 修订至 v1.2，§99 与实际一致
- [x] `ARCHITECTURE.md` §7.1 从「未决偏离」改为指向本 ADR
- [x] `TODO.md` 的 R6 关闭

若将来转回私有，写新 ADR 取代本篇，并把 spec §99 改回 private。

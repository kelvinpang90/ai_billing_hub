# HANDOFF — 交接状态

> 每个 session 开始先读这里，结束前更新这里。**只写「现在在哪、下一步做什么、什么没定」，不写历史流水账。**
> 最后更新：2026-09-10

---

## 现在在哪

**Phase 0 之前。** 项目只有文档，一行代码都还没有。

已完成：
- spec v1.1（140 节）—— 已按 v1.0 评审意见修订
- spec v1.0 评审意见（25 条：P0×7 / P1×10 / P2×8）
- 文档结构整理：PROJECT / ARCHITECTURE / REQUIREMENTS / TODO / HANDOFF
- 评审意见逐条核对（2026-09-10）：25 条中 20 条已在 v1.1 解决，5 条残留转为 TODO 的 R1–R5
- git 仓库：已初始化并推送到私有远端 `github.com/kelvinpang90/ai_billing_hub`

未完成：ADR、数据库 schema、任何代码。

## 下一步

按 [TODO.md](TODO.md) 的顺序：

1. **先做 P-1 的 D1–D7 前置决策**，尤其是 D1（汇率）、D2（SST）—— 这两个直接决定表结构，事后改代价极高。D3 的结论 spec §98 已给出（专用实例），只需落 ADR。
2. 顺带定 P-2 的 R1 / R3 / R4（Phase 2 前）与 R5（Phase 1 前）；R2 可留到 Phase 4 前。
3. 决策落成 `docs/adr/` 下的 ADR。
4. 再启动 Phase 0。

spec §137 的起步指令说得很清楚：**不要先写前端页面**，先立域模型与财务不变量。第一个里程碑是 Phase 0 + Phase 1。

## 未决问题

| # | 问题 | 影响 | 谁来定 |
| --- | --- | --- | --- |
| 1 | 汇率数据源、更新频率、取值时点 | `usage_events` / `fx_rate_versions` 表结构 | 需拍板（D1） |
| 2 | SST 含税口径、税点确认时机 | receipt / statement 表字段，事后加等于重做所有历史凭证 | 会计（D2） |
| 3 | ~~生产 MySQL 独立实例 vs 复用 `infra_mysql`~~ | **已定案**：spec §98 要求专用实例，不共享。只剩落 ADR | — |
| 4 | 支付网关选哪家 | Phase 4 全部 | 需拍板（D6） |
| 5 | WhatsApp 通知复用 `whatsapp_gateway` 还是自建 | Phase 6 | 需拍板（D7） |
| 6 | ~~25 条评审意见落实了几条~~ | **已核对（2026-09-10）**：20 条已解决 / 5 条残留，见 [REVIEW_FOLLOWUP_v1.1.md](REVIEW_FOLLOWUP_v1.1.md) | — |
| 7 | 同租户是否允许并存多笔 PENDING 支付 | Phase 4，spec 完全没规定 | 需拍板（R2） |

## 已知冲突（待清理）

1. ~~`tasks/todo.md` vs `docs/TODO.md`~~ —— **已解决（2026-09-10）**
   `CLAUDE.md` / `AGENTS.md` 的「任务管理」一节原本指向从未存在过的 `tasks/todo.md`（应是从别的项目抄来的模板）。现已改为指向 `docs/TODO.md`，并明确写死「本仓库没有 `tasks/` 目录」。两份文件是同一内容的副本，**以后改一个记得同步另一个**。

2. **spec §101 的仓库结构 vs 实际结构**
   §101 写的是 `backend/app/` + `frontend/` + `integration-client/` + 小写文档名，实际采用扁平的 `app/` + `tests/` + 大写文档名。
   → §101 视为**已被 [ARCHITECTURE.md](ARCHITECTURE.md) 第 9 节取代**，实现时不要回去照抄。

3. **spec 文件名 vs 内容版本**
   文件已改名为 `..._Spec_v1.1.md`，与文档内 Revision History 对齐。评审文件仍叫 `SPEC_REVIEW_v1.0.md`，因为它评审的确实是 v1.0，**不要改**。

## 环境与凭据

- 尚无任何生产环境、域名、凭据。
- 远端仓库：`github.com/kelvinpang90/ai_billing_hub`（私有，HTTPS remote，默认分支 `main`）。**`main` 的分支保护与 CI 尚未配置**，spec §99 要求受保护 `main` + CI 通过才能合并 —— 属 Phase 0 任务。
- 共享基建 `vps_infra` 提供 `infra_nginx` / `infra_mysql` / `infra_redis`，本项目是否接入取决于 D3。

## 代码目录说明

`app/` 与 `tests/` **本次刻意没有创建**——避免留一堆空目录。Phase 0 启动时按 [ARCHITECTURE.md](ARCHITECTURE.md) 第 9 节创建。

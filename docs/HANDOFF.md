# HANDOFF — 交接状态

> 每个 session 开始先读这里，结束前更新这里。**只写「现在在哪、下一步做什么、什么没定」，不写历史流水账。**
> 最后更新：2026-09-11

---

## 现在在哪

**Phase 0 之前。项目仍然一行产品代码都没有**——`main` 上只有 `.github/` `docs/` `scripts/` `tests/` 和两个 agent 指令文件，没有 `app/`。

但**前置工作基本做完了**，Phase 0 现在**没有任何阻塞**。

已完成：

- **spec v1.2**（140 节）。v1.0 的 25 条评审意见已逐条核对：20 条解决、5 条残留（转为 TODO 的 R1–R5）
- **ADR-0001 ~ ADR-0009 全部写完**。其中 0004 / 0006 / 0008 / 0009 是「部分接受」，各自留了显式的待补项
- **前置决策 D1–D7**：3 项完全收口、3 项部分收口、1 项（D2）等会计
- **评审残留 R1–R7**：4 项收口（R2 / R4 / R6 / R7），3 项未做（R1 / R3 / R5）
- **双 agent 流程与闸门已落地并经受住实战**：设计闸门 + 实现闸门、`codex-review.ps1` 取材与准入、`check_repo_policy.py` 策略检查、`gh_verified_write.py` 写后回读
- **CI 四项**（`docs` / `scripts` / `policy` / `secret-scan`）全部已进 `main` 的必需检查；分支保护：禁 force push、禁直推、线性历史、分支必须最新

未完成：数据库 schema、任何代码。

## 下一步

**开 Phase 0。** D1–D7 里剩下的未决项**全部落在 Phase 1 及以后**，没有一项挡住 Phase 0：

| Phase 0 要做的 | 依赖任何未决项吗 |
| --- | --- |
| 仓库骨架（`app/` `tests/` Alembic `.env.example` `README.md`） | 否 |
| Docker Compose、FastAPI 分层、React 脚手架 | 否 |
| MySQL + Redis + Celery 接通 | 否（D3 已收口：专用实例） |
| 认证基座、日志与统一错误处理 | 否 |
| 专用生产数据库拓扑 | 否（ADR-0002） |
| 备份 / 恢复 / 密钥方案设计 | 部分（D4 主体已定，未决的是出站 webhook 密钥 schema，那是 Phase 1 的事） |

spec §137 的起步指令：**不要先写前端页面**，先立域模型与财务不变量。第一个里程碑是 Phase 0 + Phase 1。

⚠️ Phase 0 是 13 项的大块，**拆成能独立提 PR 的小任务再动手**，不要堆一个巨型 PR（一个 PR 一个任务，见 [WORKFLOW.md](WORKFLOW.md) §5）。

## 未决问题

只列**真正还没定**的。已收口的不再占位——要看决策历史去 [adr/](adr/README.md)。

| # | 问题 | 卡住什么 | 谁来定 | 死线 |
| --- | --- | --- | --- | --- |
| 1 | **SST 六项里的五项**：是否需注册、AI 服务税务分类与豁免、充值属储值/押金/服务预付、税的确认时点与税率、receipt/statement 强制字段 | 生产环境充值、收据、对账单 schema（spec §45.1 闸门） | **会计** | Phase 4 开工前 |
| 2 | **出站 webhook 密钥的 schema**：新建 `project_webhook_secrets` 表，还是 `projects` 加暂存列 | 密钥轮换重叠期无处安放 | 需拍板，**且要走设计闸门** | Phase 1 开工前 |
| 3 | **支付网关选哪家** | Phase 4 全部 | 需拍板（商务） | Phase 4 开工前 |
| 4 | **WhatsApp 传输路径**：复活并扩建 `whatsapp_gateway`，还是计费平台用独立 App / 号码 | Phase 6 的 WhatsApp 通道（Email 与 Portal 不受影响） | 需拍板 | Phase 6 的 WhatsApp 部分 |
| 5 | **R1 停机/复机阈值**：仍硬编码 `balance <= 0`，无可配置阈值 | Phase 2 | 需拍板 | 建议 Phase 2 前 |
| 6 | **R3 账本膨胀权衡**：钱包变更按租户串行化的量级验证 | Phase 1–2 性能 | 需评估 | Phase 2 前 |
| 7 | **R5 `REQ-*` 覆盖度**：只有 13 条，未覆盖每条硬性要求 | 可追溯性 | 待补 | Phase 1 前 |

**D2（第 1 条）与 WhatsApp（第 4 条）的背景已经整理好**，分别见 [ADR-0008](adr/ADR-0008-sst-tax-treatment.md) 与 [ADR-0009](adr/ADR-0009-notification-channels.md)，可以直接拿去问会计 / 做评估。

## 已知冲突（待清理）

1. **spec §101 的仓库结构 vs 实际结构**
   §101 写的是 `backend/app/` + `frontend/` + `integration-client/` + 小写文档名，实际采用扁平的 `app/` + `tests/` + 大写文档名。
   → §101 视为**已被 [ARCHITECTURE.md](ARCHITECTURE.md) 第 9 节取代**，实现时不要回去照抄。

2. **spec 文件名 vs 内容版本**
   约定：spec 文件名跟随文档内 Revision History。现为 `..._Spec_v1.3.md`（2026-09-11 勘误：§6 精度、§123 仓库可见性）。
   `SPEC_REVIEW_v1.0.md` 与 `REVIEW_FOLLOWUP_v1.1.md` 的文件名**不跟随**——它们各自评审/核对的确实是 v1.0 与 v1.1，**不要改**。

3. **`AGENTS.md` §10 / §11 的 `erp_os` 模板残留**
   列了 `Inventory` / `Order` / `e-Invoice`，本项目没有这些模块，会把审查者的注意力引到不存在的地方。属噪音不属错误，已记进 [TODO.md](TODO.md)，另开 PR 清。

> ⚠️ **`CLAUDE.md` 与 `AGENTS.md` 不再是同一内容的两个副本**（2026-09-11 起）。两份是各自独立的指令文件，**不要再互相同步正文**。唯一必须一致的是评论署名前缀与判定行的写法，那由 `check_docs.py` 的约定串规则机械保证。

## 环境与凭据

- **尚无任何生产环境、域名、凭据。**
- 远端仓库：`github.com/kelvinpang90/ai_billing_hub`（**公开**，HTTPS remote，默认分支 `main`）。2026-09-10 由私有转公开，目的是在 GitHub Free 上启用分支保护（私有仓库该功能需 Pro）。见 [ADR-0001](adr/ADR-0001-repository-visibility.md)
- ⚠️ **仓库公开**：绝不提交凭据、密钥、`.env`、真实主机名 / IP、客户数据、供应商合同价
- 共享基建 `vps_infra` 提供 `infra_nginx` / `infra_mysql` / `infra_redis`，**本项目不接入其 MySQL / Redis**——[ADR-0002](adr/ADR-0002-production-datastore-isolation.md) 要求专用实例

## 代码目录说明

`app/` 与 `tests/` 里的产品代码**尚未创建**（`tests/` 目前只放流程脚本自己的回归测试）。Phase 0 启动时按 [ARCHITECTURE.md](ARCHITECTURE.md) 第 9 节创建。

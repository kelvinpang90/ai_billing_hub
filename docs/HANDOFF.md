# HANDOFF — 交接状态

> 每个 session 开始先读这里，结束前更新这里。**只写「现在在哪、下一步做什么、什么没定」，不写历史流水账。**
> 最后更新：2026-09-12

---

## 现在在哪

**Phase 0 进行中。** §123 的 13 项已拆成 T0.1–T0.10（拆法与依赖见 [TODO.md](TODO.md) 的 Phase 0 一节），
**T0.1–T0.7 已做完**：`app/` 七层包、`create_app()` 工厂、`/healthz`、`app/core/config.py`、`pyproject.toml`、
`.env.example`、`tests/backend/`；CI 的 `backend` job（lint + 格式 + pytest + 打包冒烟）；结构化 JSON 日志 +
关联 ID + 脱敏 + §107 统一错误信封 + `AppError` 领域异常基类；MySQL + SQLAlchemy + Alembic 接通
（`Money` = `DECIMAL(20,8)`、会话生命周期、基线迁移、`/readyz` 就绪探针）；Redis + Celery 接通
（worker / beat 入口、探活任务、面向财务不变量的 Celery 默认值）；Docker Compose 栈（`Dockerfile` +
`docker-compose.yml` + `deploy/nginx/`）；React 骨架（`frontend/`，§3.2 那一套 + i18n，**七个**服务实测跑通）。

**业务表一张都还没有**（基线迁移是空的，Phase 1 才开始建）；**周期任务一条都还没有**（beat 的 schedule 是空的）；
**前端只有一个落地页**（一张平台状态卡片，Phase 1 起换成真内容）；**认证还没有**（T0.8）。

已完成：

- **spec v1.2**（140 节）。v1.0 的 25 条评审意见已逐条核对：20 条解决、5 条残留（转为 TODO 的 R1–R5）
- **ADR-0001 ~ ADR-0009 全部写完**。其中 0004 / 0006 / 0008 / 0009 是「部分接受」，各自留了显式的待补项
- **前置决策 D1–D7**：3 项完全收口、3 项部分收口、1 项（D2）等会计
- **评审残留 R1–R7**：4 项收口（R2 / R4 / R6 / R7），3 项未做（R1 / R3 / R5）
- **双 agent 流程与闸门已落地并经受住实战**：设计闸门 + 实现闸门、`codex-review.ps1` 取材与准入、`check_repo_policy.py` 策略检查、`gh_verified_write.py` 写后回读
- **CI 六项**（`docs` / `scripts` / `policy` / `backend` / `frontend` / `secret-scan`）**全部已进 `main` 的必需检查**（`backend` 于 2026-09-11 T0.2 合并后追加，`frontend` 于 2026-09-12 T0.7 合并后追加）。分支保护其余项：禁 force push、禁直推、禁删除、线性历史、分支必须最新、`enforce_admins`、对话必须解决

未完成：数据库 schema、T0.8 之后的全部 Phase 0 任务。

## 下一步

**接着做 T0.8（认证基座：管理员登录 + 密码哈希 + 会话/令牌 + 2FA）**，再按 T0.9 / T0.10 往下走。
⚠️ T0.8 要记着 spec §51：一套认证同时服务 ADMIN 与 CUSTOMER、**同一个 React 前端**，角色只决定可见的路由；
**授权必须由后端独立强制执行，前端藏菜单不构成任何访问控制**。
清单与依赖在 [TODO.md](TODO.md) 的 Phase 0 一节，
**每个 T0.x 一个 PR**，从 `main` 开分支，不要开 stacked PR（[WORKFLOW.md](WORKFLOW.md) §5）。

D1–D7 里剩下的未决项**全部落在 Phase 1 及以后**，没有一项挡住 Phase 0：

| Phase 0 要做的 | 依赖任何未决项吗 |
| --- | --- |
| 后端骨架、CI job、日志与错误处理 | 否 |
| Docker Compose、React 脚手架 | 否 |
| MySQL + Redis + Celery 接通 | 否（D3 已收口：专用实例） |
| 认证基座 | 否 |
| 专用生产数据库拓扑 | 否（ADR-0002） |
| 备份 / 恢复 / 密钥方案设计 | 部分（D4 主体已定，未决的是出站 webhook 密钥 schema，那是 Phase 1 的事） |

spec §137 的起步指令：**不要先写前端页面**，先立域模型与财务不变量。第一个里程碑是 Phase 0 + Phase 1。

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
   约定（2026-09-11 起）：spec 文件名**不带版本号**，固定为 `docs/Acuven_Central_AI_Billing_Platform_Spec.md`；版本只记在文档内的 Revision History。旧约定「文件名跟随版本」让每次改几行正文都要改名并同步 9 处引用，连做三次（v1.3 → v1.6）后取消。
   **章节号是稳定引用**，退役的章节留编号空洞、不重编号。
   ⚠️ 退役之后就**不能再用 `§` 写法引用它**——`check_docs.py` 会判为「unknown spec section」。要点名退役章节，写成「第 N 节」。
   `docs/archive/` 下的 `SPEC_REVIEW_v1.0.md` 与 `REVIEW_FOLLOWUP_v1.1.md` 是对 v1.0 / v1.1 的历史记录，内容冻结、文件名不改；`check_docs.py` 对该目录**不校验 `§N`**（它们引用的是当时版本的章节），链接与约定串照查。要退役一个只被历史记录引用的章节，不再需要改历史记录。

> ⚠️ **`CLAUDE.md` 与 `AGENTS.md` 不再是同一内容的两个副本**（2026-09-11 起）。两份是各自独立的指令文件，**不要再互相同步正文**。唯一必须一致的是评论署名前缀与判定行的写法，那由 `check_docs.py` 的约定串规则机械保证。

## 环境与凭据

- **尚无任何生产环境、域名、凭据。**
- 远端仓库：`github.com/kelvinpang90/ai_billing_hub`（**公开**，HTTPS remote，默认分支 `main`）。2026-09-10 由私有转公开，目的是在 GitHub Free 上启用分支保护（私有仓库该功能需 Pro）。见 [ADR-0001](adr/ADR-0001-repository-visibility.md)
- ⚠️ **仓库公开**：绝不提交凭据、密钥、`.env`、真实主机名 / IP、客户数据、供应商合同价
- 共享基建 `vps_infra` 提供 `infra_nginx` / `infra_mysql` / `infra_redis`，**本项目不接入其 MySQL / Redis**——[ADR-0002](adr/ADR-0002-production-datastore-isolation.md) 要求专用实例

## 代码目录说明

`app/` 已按 [ARCHITECTURE.md](ARCHITECTURE.md) 第 9 节建好七层包（T0.1），里面目前只有应用工厂、配置与 `/healthz`。

`tests/` 下有两套测试，**跑法不同、互不收集**：

| 目录 | 内容 | 怎么跑 |
| --- | --- | --- |
| `tests/backend/` | 后端产品代码的测试 | `python -m pytest` |
| `tests/test_*.py` | 流程脚本（策略检查、写后回读）自己的回归 | `python -m unittest discover -s tests` |

`tests/backend/` 没有 `__init__.py`，所以 unittest 的 discover 不会递归进去——**不要给它加 `__init__.py`**，加了两套测试就会互相收集。

前端是第三套，完全独立：`frontend/` 下 `npm test`（vitest）。它的测试和源码放在一起（`*.test.ts`），
由 `frontend/tsconfig.test.json` 单独类型检查——**应用代码那份（`tsconfig.app.json`）刻意不给 Node 类型**，
免得浏览器代码里出现 `import fs` 这种打包时才炸的写法。

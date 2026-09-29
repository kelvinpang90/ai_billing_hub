# Phase 2 计划 —— AI Usage Billing Engine（spec §125）

> 状态：**已确认**（Kelvin，2026-09-28）：第 1 节五条全部按建议执行 —— 设计闸门先写，实现任务等 020–023 合并部署后再登记为 `ready`。
> 编号在登记实现任务时分配（与 AIH-TASK-020 的先例一致），设计闸门 Issue 用下面的占位名 T-A…T-L。
> 依据：spec §125 及其引用的 §11–§17.1、§20、§23、§36–§39、§58–§59、§74–§84、§89、§113–§120；
> [ADR-0005](../adr/ADR-0005-fx-rate-source.md)、[ADR-0008](../adr/ADR-0008-sst-tax-treatment.md)、
> [ADR-0010](../adr/ADR-0010-suspension-threshold.md)、[ADR-0011](../adr/ADR-0011-per-event-usage-ledger.md)。
> 基线：`origin/main` c8daaed。

---

## 1. 要先拍板的事（编号，逐条回答即可）

1. **Phase 1 闸门（§137）怎么解释。** Phase 1 六项里还有五项没勾；AIH-TASK-020–023 仍是 `ready`。
   **建议**：Phase 2 的**设计闸门**（只写文档）现在就可以和 020–023 并行；Phase 2 的**实现任务**等 020–023
   合并部署之后才登记为 `ready`。「后续计划」里剩下的 Phase 1 项（低余额阈值、流水查看、余额定时核对、关户）
   **不阻塞 Phase 2**，但「余额定时核对与告警」建议排在 Phase 3 试点之前做完 —— 用量扣费一接上，账本行数会快速增长。
2. **自动 FX 适配器放 Phase 2 还是 Phase 8。** §125 写 Phase 2 要 automatic FX adapter，§131 又把 FX rate sync adapter 放 Phase 8。
   **建议**：Phase 2 做 BNM 拉取 → `DRAFT` → 管理员发布，以及手工录入（ADR-0005 已定来源）；Phase 8 只做供应商价格同步。
3. **§120 告警在 Phase 6 通知体系之前走什么通道。** Phase 2 会新产生 Pricing Error、FX Rate Error、Unknown Model、
   FX 拉取失败（连续 3 天）、最新发布汇率超过 3 天、钱包负余额、摄取冲突这几类告警。
   **建议**：沿用现有 `deploy/monitor.sh` + Healthchecks 的做法，加一个管理端只读计数接口（或 `/readyz` 同类的内部端点），
   cron 读计数、按维度各一个检查；不在 Phase 2 做邮件告警。
4. **把 TODO 里两条合成一条闸门**：「用量摄取端点」和「全局 event 幂等与冲突检测」是同一段代码（插入优先、撞唯一约束再比指纹），
   拆开的话第一个 PR 里的端点没有幂等，不能单独上线。**建议合并**为下面的 T-E。
5. **`MODEL_UNKNOWN` 事件要存原始 provider / model 字符串。** §79 的 `usage_events` 只有 `provider_id` / `model_id`，
   而未知模型时两者为空，管理员没法据此建映射（§84）。**建议**加 `provider_code_raw` / `model_code_raw` 两列；
   §74 说表定义是最低要求，多列不冲突，不需要勘误（同 `projects.description` 的先例）。

---

## 2. 任务拆分

编号先用 T-A…T-L 占位，确认后按顺序分配 `AIH-TASK-025` 起的编号（2026-09-29 全部登记，对照表见 `docs/TODO.md` 的 Phase 2 一节；T-K、T-L 登记时各拆成两个前端任务，共 14 个编号）。「闸门」列按 CLAUDE.md 的分档。
迁移接在 022 的 0011 后面，**全部串行**（每个任务最多一个迁移）。

| 占位 | 任务 | 闸门 | 依赖 | 迁移 | 主要内容 |
| --- | --- | --- | --- | --- | --- |
| T-A | Provider / Model / Usage Meter 目录 | 全部 | 022 | 0012 | `ai_providers`、`ai_models`（含别名映射，供 §84 映射未知模型）、`usage_meter_types`；§15.1 的 component_code 目录；管理端建 / 列 / 停用接口与审计。只停用不删除 |
| T-B | 供应商价格版本 | 全部 | T-A | 0013 | `provider_price_versions` + `provider_price_components`；DRAFT → PUBLISHED → RETIRED；同维度已发布区间不重叠；已发布不可变（照账本做触发器）；`GET/POST /admin/provider-prices`、`POST …/{id}/publish`；按 `occurred_at` 取版本的查询函数；审计 `PROVIDER_PRICE_PUBLISH`。**不许回溯发布**（`effective_from` 不早于发布时刻），否则重试会选到新版本，违反 §16 |
| T-C | FX 汇率版本、审批与 BNM 适配器 | 全部 | T-A | 0014 | `fx_rate_versions`；BNM 适配器（Celery Beat，吉隆坡 12:00 后）落 DRAFT；手工录入同一审批；按 `occurred_at`（Asia/KL）取已发布版本；热路径不调 BNM；两条告警（连续 3 天失败、最新发布超 3 天）；产出 `docs/currency-and-fx.md`。可与 T-B 并行设计，实现排在 T-B 之后（迁移串行） |
| T-D | 定价规则 MARKUP 与 FIXED_RATE | 全部 | T-A | 0015 | `pricing_rules` + `pricing_rule_components`；§16 五级优先级编进 `priority_scope`；混合 / 不完整规则在数据库与服务层都拒绝；同 scope 已发布区间不重叠、不许回溯；`GET/POST/PATCH /admin/pricing-rules`；所有价格标注「含税」（ADR-0008） |
| T-E | 用量摄取端点：签名、防重放、持久化 202、全局幂等与冲突 | 全部 | T-A | 0016 | `usage_events`（§79 + 第 1 节第 5 条的两列）；`POST /api/v1/integration/usage-events`；接上 AIH-TASK-012 的 `verify_signature`；Redis nonce（ADR-0004 §5）；tenant / project 以凭据为准，payload 不一致则拒绝并审计；各 usage_type 的字段契约（§11）；`RECEIVED` 提交后才回 202；插入优先 + 指纹比较，一致回 `already_received`，不一致 409 `IDEMPOTENCY_CONFLICT` + 告警；补 REQ-AUTH-001 的重放测试证据。**本任务只落 RECEIVED，不计费** |
| T-F | 批量摄取端点 | §1–§7 | T-E | — | `POST …/usage-events/batch`，默认上限 100 可配置，逐条复用 T-E 的逻辑，部分成功、冲突与校验错误不可重试（§39） |
| T-G | 计价引擎（纯函数） | 全部 | T-B、T-C、T-D | — | 输入事件 + 三个版本 → `provider_source_cost`、`fx_rate_applied`、`estimated_provider_cost_myr`、`billable_cost`；各分量求和后只舍入一次 ROUND_HALF_UP 到 8 位（§80）；缺分量 → PRICING_ERROR、缺汇率 → FX_RATE_ERROR，绝不按 0 算；「同一事件重算结果完全一致」测试（ADR-0005）；产出 `docs/pricing-engine.md`；管理端试算预览接口（§59，显式 `occurred_at`，列出每个分量与版本） |
| T-H | 异步计费 worker：扣费、负余额、停机、恢复 | 全部 | T-E、T-G | 0017（如需） | 认领 RECEIVED → PROCESSING → 计价 → 一个事务里 `post_transaction(AI_USAGE, USAGE_EVENT, event_id)` + 事件置 PROCESSED 并写全部快照；负余额与停机跃迁复用现有 `post_transaction`（ADR-0010 规则不改）；§83 全部错误状态；Beat 扫描 RECEIVED 与卡住的 PROCESSING（队列丢失恢复，REQ-INGEST-002）；未知模型 / 定价 / 汇率错误修好后**重新入队**（这些事件从未产生财务效果，不是 Phase 8 的 rebill）；钱包负余额告警。**设计闸门必须带 ADR-0011 的最热租户压测方案与切换方案 C 的判据** |
| T-I | 最热租户压测与 §119 基线 | 不走（脚本） | T-H | — | 扩 `scripts/perf_baseline.py`：单租户 100 事件 / 秒突发、500 / 秒回补、worker 重试与崩溃下事件 / 账本 / 余额一致；报 p50 / p95 / p99；更新 `docs/perf-baseline.md` 第 5 节。不达标则按 T-H 闸门里的判据另开方案 C 任务 |
| T-J | 管理端用量事件查询接口 | 不走（只读） | T-H | 0018（索引，如需） | `GET /admin/usage-events[/{id}]`（§89、§61 的筛选）；未解决事件列表（§83「管理端要列出未解决事件」）；成本与毛利只在管理端（INV-7） |
| T-K | 管理端前端：供应商、模型、价格与汇率 | 不走 | T-B、T-C | — | §58 的页面；加载态与错误态（§132 DoD） |
| T-L | 管理端前端：定价规则、试算与用量事件 | 不走 | T-D、T-G、T-J | — | §59、§61 的页面 |

**设计闸门共 8 个**（T-A、T-B、T-C、T-D、T-E、T-F、T-G、T-H）。

### 关键路径

```text
020 → 021 → 022 → 023          （Phase 1 收尾，Worker 在跑）
        T-A ─┬─ T-B ─┐
             ├─ T-C ─┼─ T-G ─┐
             ├─ T-D ─┘       ├─ T-H ─┬─ T-I
             └─ T-E ── T-F ──┘       └─ T-J ── T-L
                         T-B + T-C ── T-K
```

设计闸门可以提前：T-A 的设计现在就能写；T-B / T-C / T-D / T-E 在 T-A 批准后并行写。

---

### Phase 2 的告警（第 1 节第 3 条的落实）

通道：平台加一个只读的告警计数接口，`deploy/monitor.sh` 读它，**每个维度一个 Healthchecks 检查**（与现有三个检查同一做法）。
不发邮件，Phase 6 通知体系到位后再接。

| 告警 | 来源 | 由谁加 |
| --- | --- | --- |
| FX 拉取连续 3 天失败、最新发布汇率超 3 天 | ADR-0005、§120 | T-C（**计数接口与它的访问控制在 T-C 闸门里定**，第一个用到的任务建） |
| 摄取中断、`IDEMPOTENCY_CONFLICT` | §120、§23 | T-E |
| Pricing Error、FX Rate Error、Unknown Model、钱包负余额、Outbox 积压 | §120 | T-H |

## 3. 对照 §125 验收

| §125 验收 | 由谁满足 |
| --- | --- |
| Usage Event billed once | T-E（全局唯一 event_id）+ T-H（账本唯一约束，已上线） |
| Estimated provider cost and MYR conversion calculated | T-B、T-C、T-G |
| Customer cost calculated | T-D、T-G |
| Wallet deducted | T-H |
| Duplicate event no double charge | T-E（摄取层）+ T-H（账本层）双重保证 |
| Conflicting event ID rejected without charge | T-E |
| Queue loss recoverable from database state | T-H（Beat 恢复扫描） |

TODO 里的「负余额处理」「停机」两条：规则已由 ADR-0010 定、代码已在 `post_transaction`，Phase 2 的工作是在 T-H 里
**接线并用真实用量路径重新验证**（跨零、正好为 0、挂起中继续扣费），不重写。

## 4. 明确不在 Phase 2

- 供应商价格同步、成本对账、reprocess / rebill（Phase 8，§131、§19、§62）
- 应用侧 Billing Client、本地 outbox、状态 webhook 投递（Phase 3）
- 税额反算与入账（Phase 4，ADR-0008）
- 客户侧用量页与会话摘要（Phase 5）

## 5. 确认之后我做什么

1. 按第 1 节的答复改本文件，分配正式编号
2. 一个收尾 PR：更新 `docs/TODO.md` 的 Phase 2 清单与规划块「后续计划」（替换现有 8 条待登记）
3. 开 T-A 的设计闸门（`docs/design/AIH-TASK-025-*.md` + Issue），走 Codex 审查

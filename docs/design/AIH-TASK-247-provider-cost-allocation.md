# AIH-TASK-247..249 设计：毛利分析按比例分摊对账后成本（已批准 v2）

> **来源**：设计闸门 Issue #756。本文件是 `APPROVED: design v2` 那一版正文的**逐字副本**（送审时状态行为 `READY_FOR_REVIEW`）。审查：Codex 额度不可用期间由 Claude Code 审查。建立在 #755（`docs/design/AIH-TASK-242-provider-cost-reconciliation.md`）之上。v1 审查指出导出里看不到不可分摊的已对账成本，v2 在导出合计行追加两列，v2 审查通过、没有阻断项。
> 放进仓库，是因为 OpenClaw Worker 在沙箱里不联网、读不到 GitHub Issue。
> **实现以本文件为准**；与 Issue 不一致时，以 Issue #756 上被批准的 v2 为准。设计要改，就回到 Issue 升版本、重新过闸门，不要直接改本文件。

状态：`READY_FOR_REVIEW` · 设计版本：`v2` · 目标 PR：待开

## 0. 准入判定

| 任务涉及 | 要填的范围 |
| --- | --- |
| 钱包 / 账本 / 定价 / 汇率 / 支付 / 幂等 / 状态机 | **全部章节** |
| 用量摄取 / 认证与会话（集成侧与平台侧）/ Webhook | §1–§7（**§6 不可省**） |
| 前端 / 文档 / CI / 脚本 | **不走本闸门**，直接开 PR |
| 既不碰钱、也不碰用量摄取 / 认证与会话 / Webhook 的后端改动 | **不走本闸门**，直接开 PR |

本任务档位：`全部`。理由：本批只读、不写任何表，但它第一次把**对账后成本**带进管理端毛利口径（spec §14、§86），并让带对账后成本的数据经 CSV 离开系统（spec §73）；口径错了 Kelvin 会按错误的毛利定价，字段误入客户侧就违反 INV-7。

**建立在**：`docs/design/AIH-TASK-242-provider-cost-reconciliation.md`（下称 **L1**，AIH-TASK-242..246、250、251：对账表、仓储、服务、接口、对账页）与 `docs/design/AIH-TASK-221-margin-analytics.md`（下称 **毛利分析设计**，AIH-TASK-221..225）。本文只写分摊；对账记录、范围、期间、汇率、标准世界都以 L1 为准，不重复。

---

状态：`READY_FOR_REVIEW`
负责人：Claude
**设计版本**：`v2`
对应需求：spec §14（有对账数据时报表用对账后成本并披露覆盖期间；不编造逐请求精度；报表写明成本口径）、§73（管理端导出可含内部成本）、§86（毛利口径、对账调整的期间口径）、§87（分组维度）；Invariant 6、7、10
目标 PR：待开（AIH-TASK-247..249，见 §11）

## 1. 目标与边界

### 要解决的问题

毛利分析设计只有估算成本（`cost_basis` 固定 `estimated`，对账后成本等 Phase 8：`docs/design/AIH-TASK-221-margin-analytics.md:50`、`:71`）。L1 交付了按供应商 × UTC 月的对账记录，但它只是合计数；spec §14 要求有对账数据时报表用对账后成本、并披露覆盖期间。

### Kelvin 2026-11-16 的决定

按估算成本比例分摊进毛利分析，与对账页一起登记、不推迟（L1 §1）。

### 我定的（审查者重点看，§10 也列出）

1. **按估算成本比例分摊**（口径名 `pro_rata_estimated_cost`），**只在查询时算、不落库**；分组行与合计都给，并标明口径与覆盖的对账月份。
2. **用实时的 `E(r)`**（该记录期间范围内全部已处理事件的估算之和，不受报表筛选影响），使同一对账月的分摊之和恒等于实际成本。
3. **逐事件按它所属的 UTC 月取系数**：毛利分析按马来西亚日 / 月分组时不受 UTC 与马来西亚月的口径差影响。
4. 原有 `cost_basis` 与全部旧键不变，新口径以新键给出；导出只在末尾追加列。

### 可观察的完成标准

1. 管理端用量分析（AIH-TASK-222 的接口与 223 的导出）的分组行与合计多出「供应商成本（对账后，未覆盖部分用估算）」、它的口径（`estimated` / `reconciled` / `mixed`）、基于它的毛利；合计另列覆盖的对账月份与是否可分摊。
2. 同一对账月完整落在区间内时，按任何维度分组的分摊额之和等于该月实际成本（舍入误差除外）。
3. 管理端用量分析页显示上述合计项与覆盖说明，表格多出供应商成本列。
4. 任何非管理端接口与客户侧模块看不到新字段（INV-7 守卫追加新键名）。

### 明确不做什么

- 不把分摊额写进任何表、不改 `usage_events`（spec §14、INV-6）。
- 不改 L1 的仓储、服务与对账接口；不改毛利分析已有的口径与键；不改 `app/core/csv_export.py`。
- 不按模型或请求录入实际成本（L1 的粒度是供应商 × 月）。
- 不加迁移、索引、依赖；不写审计。

### 现有行为与证据

- 用量分析的条件构造：`customer_id` / `project_id` 先查 public id，查不到直接返回空（`app/repositories/usage_analytics.py:58-71`）；AIH-TASK-221 把它抽成 `_conditions`（毛利分析设计 §2「仓储」）。
- 分组表达式、按日 / 按月、合计、导出 31 列与 INV-7 守卫：毛利分析设计 §2「接口一」「接口二」「仓储」，AIH-TASK-221..224 实现。
- L1 提供：`ai_module_origin`、`scope_estimated_myr`、`current_records_overlapping`（一条连表查询，带回供应商代码）（L1 §2「仓储」）与纯函数 `allocate`（L1 §2「服务」）。

## 2. 设计概要

### 数据流

```text
GET /admin/usage-analytics 或 /export（ADMIN）→ 毛利分析设计的原有查询不变
  → 同一只读会话：reconciled_allocation_inputs（L + 1 + 2n 条查询）
  → 接口层用 allocate 按比例分摊、每组每块 quantize_money 一次
  → 分组行与合计的新键；导出追加 10 列
（不写任何行、不写审计、无出站事件）
```

### 规则

对一条当前对账记录 r（供应商 p、期间 [s, e)、实际 MYR 成本 R），令 `E(r) = scope_estimated_myr(p, s, e)`（L1 §2「仓储」）（**实时**、整个期间、不受报表筛选影响）。L1 范围（经 AI 模块、该供应商、该 UTC 月）内任一已处理事件 x 的「分摊后成本」是 `est(x) × R ÷ E(r)`；不在任何当前记录范围内的事件（集成方上报的、未对账月份的、首月之前的）仍用 `est(x)`。分摊**只在查询时算、不落库**，口径名 `pro_rata_estimated_cost`。

### 推论

- 对任一分组 g：`covered(g) = Σ_r Σ_{x∈g∩scope(r)∩区间} est(x)`；`reconciled(g) = quantize_money(Σ_r covered(g, r) × R ÷ E(r))`（每个分组只舍入一次）；`provider_cost(g) = reconciled(g) + (estimated(g) − covered(g))`。
- `allocatable(r) = (E(r) > 0) 或 (R = 0)`。`E(r) = 0` 而 `R > 0` 的记录无法分摊：它不进任何行，合计的覆盖清单里标 `allocatable: false`，页面提示；`E(r) = 0` 且 `R = 0` 的记录没有东西要分，标 `allocatable: true`（`allocate` 返回 None 时按 0 处理，不进任何行），照常列进 `reconciled_months`。
- 同一对账月完整落在区间内时，按任何维度分组的 `reconciled` 之和等于 R（每组至多 0.5×10⁻⁸ 的舍入误差，组数越多误差越大，文档写明）；合计层单独按同一公式计算、只舍入一次。
- 用实时的 `E(r)` 而不是记录里的快照：确认之后才处理完的事件也分到成本，分摊之和恒等于 R；快照只用于「确认时比较的是什么」。

### 仓储（`app/repositories/usage_analytics.py`，AIH-TASK-247）

新增 `reconciled_allocation_inputs(session, *, group_by, customer_id, project_id, provider, model, feature, occurred_from, occurred_to) -> AllocationInputs`，返回类型写死为：

```python
@dataclass(frozen=True)
class CoveringRecord:
    public_id: str                      # 对账记录的 public_id，也是下面两个字典里的记录键
    provider_code: str
    billing_month: str                  # "YYYY-MM"
    period_start: dt.datetime           # 不带时区的 UTC
    period_end: dt.datetime
    reconciled_cost_myr: Decimal        # R
    scope_estimated_myr: Decimal        # E：实时、整个期间、不受报表筛选影响

@dataclass(frozen=True)
class AllocationInputs:
    records: tuple[CoveringRecord, ...]                        # 按 provider_id、period_start 升序
    group_covered: dict[tuple[str | None, str, str], Decimal]  # (group_key, 记录 public_id, 模式) -> covered
    total_covered: dict[tuple[str, str], Decimal]              # (记录 public_id, 模式) -> covered
```

- 模式键是普通字符串 `"PREPAID"` 或 `"INTERNAL_METERED_ONLY"`（`billing_mode_snapshot` 的原值；`PROCESSED` 事件恒非空，`MODE_SNAPSHOT_CHECK`）。`group_key` 与 `list_usage_analytics` 返回的 `UsageAnalyticsRow.group_key` **同值同类型**（`str | None`；`day` 为 `YYYY-MM-DD`、`month` 为 `YYYY-MM`，其余维度照原样）。字典只含查询结果里出现的键，缺的键按 0 处理。`total_covered[(r, m)]` 恰好等于 `group_covered` 里同一 `(r, m)` 各项之和（第 4 步在 Python 里加出来，不另发查询）。
- 空输入：`AllocationInputs(records=(), group_covered={}, total_covered={})`。

步骤（只读、不加锁、不提交）：

1. `_conditions(...)`（AIH-TASK-221 抽出的函数）。它自己发出的查找：带 `customer_id` 时 1 次租户查找；带 `project_id` 时 1 次项目查找，但租户先查不到时直接返回 None、不再查项目（现有行为，`app/repositories/usage_analytics.py:58-71`）。为 None → 返回空输入，**不再发任何查询**。
2. L1 的 `current_records_overlapping(occurred_from, occurred_to)`（AIH-TASK-243）：1 次，已连 `ai_providers` 带回 `provider_code`，`CoveringRecord.provider_code` 取自它，不再查供应商表。
3. 对每条记录 r（共 n 条）：`scope_estimated_myr(r 的供应商, s, e)` 1 次（得 E）；分组聚合 1 次：在 `_conditions` 之上加 `occurred_at ∈ [max(from, s), min(to, e))`、`provider_id = r 的供应商`、`ai_module_origin()`，`SELECT <分组表达式>, billing_mode_snapshot, SUM(estimated_provider_cost_myr)`，`GROUP BY` 与 `list_usage_analytics` **同一个分组表达式与连接**（含 `day` / `month`）再加 `billing_mode_snapshot`，**不分页、不排序**（全部分组都要）。
4. 在 Python 里转换分组键、填 `group_covered`，并按 `(r, 模式)` 累加出 `total_covered`。**没有**单独的不分组合计查询。

**精确查询次数**（测试用 `before_cursor_execute` 计数，只数本函数内发出的语句）：设 `L` 为第 1 步的查找次数（都不带为 0；只带 `customer_id` 为 1；只带 `project_id` 为 1；两个都带且租户找到为 2、租户找不到为 1），则 `_conditions` 为 None 时恰好 `L` 次，否则恰好 `L + 1 + 2n` 次。366 天区间、一个供应商时 n ≤ 13，不带筛选时至多 27 次。

### 接口与导出（`app/api/admin_usage_analytics.py`、`app/schemas/usage_analytics.py`，AIH-TASK-248）

AIH-TASK-222 的全部键原样保留，新增：

- 顶层：`provider_cost_allocation: "pro_rata_estimated_cost"`、`reconciliation_period_basis: "utc_calendar_month"`。`cost_basis` 仍为 `"estimated"`，含义不变（指原有的 `estimated_provider_cost_myr` 字段）。
- 每个分组项与 `totals` 的 `prepaid` 块：`covered_estimated_cost_myr`、`reconciled_provider_cost_myr`、`provider_cost_myr`、`provider_cost_basis`（逐块判定：该块 `covered = 0`（含该块没有事件、估算为 0）→ `"estimated"`；`covered = estimated_provider_cost_myr` 且 `> 0` → `"reconciled"`；其余 `"mixed"`）、`gross_margin_on_provider_cost = billable_cost − provider_cost_myr`、`gross_margin_on_provider_cost_rate`（计费额为 0 时 null）。
- `internal_metered_only` 块：前四个同名键（没有毛利）。
- `totals` 的两个块用 `total_covered` 按同一公式单独计算（每块只舍入一次），不由分组行相加。
- `totals`：`gross_profit_on_provider_cost = prepaid.gross_margin_on_provider_cost − payments.gateway_fee`、`gross_profit_on_provider_cost_rate`（`payments` 为 null 时两者都 null，同 AIH-TASK-222 规则）；`reconciliation_coverage: [{reconciliation_id, provider_code, billing_month, period_start, period_end, fully_in_range, allocatable}]`，顺序同 `records`；`period_start` / `period_end` 的 JSON 序列化与 L1 的 `ReconciliationView` 相同（照 `FxRateView`：不带时区、不带 `Z` 的 ISO 8601 UTC），CSV / xlsx 导出不含这两列；`fully_in_range = (occurred_from ≤ period_start 且 period_end ≤ occurred_to)`；`allocatable` 见上面「推论」。
- `totals` 另加两个披露字段（**不可分摊的已对账成本**，不并进任何成本、毛利，只单独披露）：`unallocated_reconciled_months`：`allocatable = false` 的覆盖记录，每条 `provider_code:YYYY-MM`，顺序同 `reconciliation_coverage`，没有时为 `[]`；`unallocated_reconciled_cost_myr`：这些记录 R 之和（按记录全额计，不按区间切分；`money_text`），没有时为 null。
- 导出（AIH-TASK-223 的 31 列之后**追加** 10 列，原有列不变；列常量是 CSV 与 AIH-TASK-237 的 xlsx 导出共用的那一个，所以 **xlsx 同样是 41 列**，与 237「只换编码」一致，不另立 CSV 专用列表）：32 `prepaid_provider_cost_myr`、33 `prepaid_provider_cost_basis`、34 `prepaid_gross_margin_on_provider_cost`、35 `internal_provider_cost_myr`、36 `internal_provider_cost_basis`、37 `gross_profit_on_provider_cost`（TOTAL 行；null 时空）、38 `provider_cost_allocation`（两种行都是 `pro_rata_estimated_cost`）、39 `reconciled_months`（TOTAL 行：可分摊的覆盖记录按 `provider_code:YYYY-MM` 以 `;` 连接；GROUP 行空）、40 `unallocated_reconciled_months`（只 TOTAL 行：`totals.unallocated_reconciled_months` 以 `;` 连接，如 `anthropic:2027-02`；没有时空；GROUP 行空）、41 `unallocated_reconciled_cost_myr`（只 TOTAL 行：`totals.unallocated_reconciled_cost_myr` 的定点字符串；null 时空；GROUP 行空）。第 39、40 列加入公式注入防护的文本列。
- INV-7 守卫（`tests/backend/test_internal_cost_visibility.py`）的禁用名单加上全部新键名。

### 页面（AIH-TASK-249）

- **合计块**（`UsageAnalyticsTotals`，带边框的 `Descriptions`）追加 **8 项**，**每项是独立的一对 `<th>` / `<td>`，`<td>` 里只有那一个值**（后端字符串原样，或口径的显示文案，null 为短横 `—`），不在同一格里附口径、比率或说明：

  | 标签（`<th>`） | 值（`<td>`） |
  | --- | --- |
  | Provider cost (reconciled where available) | `totals.prepaid.provider_cost_myr` |
  | Provider cost basis | `totals.prepaid.provider_cost_basis` 的显示文案 |
  | Internal provider cost (reconciled where available) | `totals.internal_metered_only.provider_cost_myr` |
  | Internal provider cost basis | `totals.internal_metered_only.provider_cost_basis` 的显示文案 |
  | Gross margin on provider cost | `totals.prepaid.gross_margin_on_provider_cost` |
  | Gross margin on provider cost (ratio) | `totals.prepaid.gross_margin_on_provider_cost_rate` |
  | Gross profit on provider cost | `totals.gross_profit_on_provider_cost` |
  | Gross profit on provider cost (ratio) | `totals.gross_profit_on_provider_cost_rate` |

  「Gross margin / Gross profit on provider cost 按 prepaid 块的供应商成本算、口径同 Provider cost basis；手续费按 `paid_at`」写在合计块下方的说明文字里，不进任何 `<td>`。
- **口径的显示文案**（i18n，`en.json`；页面不显示后端原词）：`estimated` → `Estimated`，`reconciled` → `Reconciled`，`mixed` → `Mixed (partly reconciled)`。合计块与表格小字用同一组文案。
- **覆盖说明**（合计块下方的普通文字，至多两行，格式写死）：第一行恒在：`Reconciled months (UTC): ` 后接 `allocatable = true` 的记录，每条写 `<provider_code> <YYYY-MM>`，`fully_in_range = false` 的在其后加一个空格和 `(partial)`，多条之间用 `, ` 连接，顺序同 `reconciliation_coverage`；没有这样的记录时这一行是 `Reconciled months (UTC): none — provider costs are estimated`。第二行只在有 `allocatable = false` 的记录时出现：`Not allocated (no estimated cost in that month, UTC): ` 后接这些记录，每条同样写 `<provider_code> <YYYY-MM>`（同样的 `(partial)` 规则），**全部在这一行里**用 `, ` 连接，行尾再接 ` — reconciled cost not included in provider cost: <totals.unallocated_reconciled_cost_myr> MYR`（金额原样显示后端字符串）。例：`Reconciled months (UTC): anthropic 2026-12, anthropic 2027-03 (partial)`。
- **表格**：在 AIH-TASK-224 的 Prepaid 列组里追加两列「Provider cost」（`item.prepaid.provider_cost_myr`，下一行小字为口径的显示文案）与「Margin on provider cost」（`item.prepaid.gross_margin_on_provider_cost`）；在 Internal 列组里追加一列「Provider cost」（`item.internal_metered_only.provider_cost_myr`，下一行小字为口径的显示文案）。不新增列组，其他列不变。
- 说明文字补：对账后成本按估算成本比例分摊到分组，不是逐请求的实际成本；对账月份按 UTC 划分；Estimated / Reconciled / Mixed 三种口径的含义；Gross margin / Gross profit on provider cost 的口径与手续费期间（见合计块一条）。

### 事务边界

- 与毛利分析设计相同：每个请求一个只读会话、不提交；分摊输入的查询与原有查询在同一会话（MySQL `REPEATABLE READ` 一致读同一快照，请求中途确认的对账记录这次不计）。

### 时间语义

- 归属只看事件的 `occurred_at`（不带时区的 UTC）；对账记录的期间是 L1 的 UTC 自然月；报表区间与对账期间按 `[max(from, s), min(to, e))` 求交。

### 金额与精度

- 分摊：每个分组每块 `Σ_r covered × R ÷ E` 在 `PRICING_CONTEXT` 内累加，最后 `quantize_money` 一次；合计单独同样计算。金额 `money_text`、比率 `ratio_text`，不经 float。

## 3. 不变量影响矩阵

| 不变量 | 是否触碰 | 可能怎样被破坏 | 设计控制 | 验证方式 |
| --- | --- | --- | --- | --- |
| INV-1 中心故障不中断 AI | 是（间接） | 每个重叠的对账记录多两条范围聚合，扫一个月的事件并探测 outbox | 只读一致读、不加锁；366 天区间、一个供应商时至多 27 条查询 | §7「查询次数」 |
| INV-2 事件不重复扣费 | 否 | 只读事件 | — | — |
| INV-3 支付不重复入账 | 否 | 手续费沿用毛利分析设计的只读查询 | — | — |
| INV-4 余额只经账本变动 | 否 | 不读不写钱包 | — | — |
| INV-5 历史账本不可变 | 否 | 不读账本 | — | — |
| INV-6 事件保留版本引用 | 是 | 把分摊额写回事件或另存逐请求的「实际成本」 | 只在查询时算，不落库 | §7 全部行只读；代码审查 |
| INV-7 客户不可见成本毛利 | **是** | 新键挂到客户侧；客户侧模块导入新逻辑 | 只扩展 `/api/v1/admin` 的用量分析接口与导出；INV-7 守卫追加全部新键名 | §7「INV-7 守卫」（毛利分析设计的守卫 + 新名） |
| INV-8 租户不可互访 | 是 | 客户令牌调用 | 沿用 `require_admin` | 毛利分析设计的鉴权用例照常 |
| INV-9 对话内容不入库 | 否 | 只读计量列与 outbox 的 `event_id` | — | — |
| INV-10 金额用 Decimal | 是 | 分摊中途舍入或经 float；前端转 number | `PRICING_CONTEXT`、每组每块只舍入一次、字符串输出、前端原样显示 | §7「分摊舍入」「前端分析页」 |
| INV-11 event_id 至多一次财务效果 | 否 | 无财务效果 | — | — |
| INV-12 定稿对账单不可变 | 否 | 不读不写对账单 | — | — |
| INV-13 状态与事件原子提交 | 否 | 无状态变化 | — | — |
| INV-14 队列丢失不毁持久工作 | 否 | 同步只读 | — | — |

## 4. 状态与并发

- 无状态机，不写任何行。
- 与对账确认并发：同一只读会话一致读；请求开始后提交的对账记录这次不计，下次计入。
- 与计费并发：确认后才处理的事件进入实时 `E(r)`，分摊之和仍等于 R。
- 幂等：只读 GET，没有幂等键与重试层；导出租约照毛利分析设计。

## 5. 失败与恢复矩阵

| 失败点 | 对外结果 | 数据最终状态 | 是否可重试 | 恢复来源 | 告警 |
| --- | --- | --- | --- | --- | --- |
| 校验失败 | 毛利分析设计的 422 | 不变 | 改参数后 | — | — |
| DB 查询失败 | 500；导出租约照 AIH-TASK-223 释放 | 不变 | 是 | 重新请求 | ERROR 日志 |
| Redis/Celery 丢失 | 不影响 | 不变 | — | — | — |
| 外部服务超时 | 不调用外部服务 | — | — | — | — |
| 重复请求 | 同一结果（随数据变化） | 不变 | — | — | — |
| 对账记录无法分摊（E = 0 而 R > 0） | 200；覆盖清单 `allocatable = false`；`totals.unallocated_reconciled_months` / `unallocated_reconciled_cost_myr` 披露月份与 R 之和；导出 TOTAL 行第 40、41 列同样披露；页面「Not allocated…」行带这一合计；该成本不进任何成本与毛利 | 不变 | — | 检查该月是否真无经 AI 模块的用量 | 无 |

## 6. 数据与安全边界

- 鉴权、租户过滤、日志、导出文件的处理：沿用毛利分析设计 §6，不变。
- **客户侧禁止的字段**：INV-7 守卫的禁用名单追加 `covered_estimated_cost_myr`、`reconciled_provider_cost_myr`、`provider_cost_myr`、`provider_cost_basis`、`gross_margin_on_provider_cost`、`gross_margin_on_provider_cost_rate`、`gross_profit_on_provider_cost`、`gross_profit_on_provider_cost_rate`、`reconciliation_coverage`、`unallocated_reconciled_months`、`unallocated_reconciled_cost_myr`。
- 不新增、不读取密钥；不处理 prompt / response；不新增数据。

## 7. 测试证据计划

世界、夹具与约定照 L1 §7：**标准世界**（E1–E6、`T_int / P_crm`、`T_pre / P_pre / P_chat`、汇率 V45 / V44）与变体 `myr_three`、`unallocatable`，都由 `tests/backend/provider_cost_world.py` 提供（AIH-TASK-243 已建，本批只调用、需要时只追加构件）。12 月记录 R = 51.48、E = 46.80、`R ÷ E = 1.1`。

| 风险/需求 | 测试层级 | 场景 | 预期结果 | 任务 |
| --- | --- | --- | --- | --- |
| 分摊输入 | integration（SQLite + MySQL，标准世界 + 经 `insert_record` 写入的 12 月记录：USD 11.70、V44、R = 51.48、快照取 L1 §7 标准世界的值） | 区间 `[2026-11-01, 2027-02-01)`：`group_by=customer`、`month`（马来西亚）、`provider` | `records` 恰好一条（12 月，E = `46.80`）；`customer`：`group_covered` 恰好 `{(T_int 的 public_id, r, "INTERNAL_METERED_ONLY"): 27.00, (T_pre 的 public_id, r, "PREPAID"): 19.80}`；`month`：恰好 `{("2026-12", r, "INTERNAL_METERED_ONLY"): 27.00, ("2027-01", r, "PREPAID"): 19.80}`；`provider`：恰好 `{("anthropic", r, "INTERNAL_METERED_ONLY"): 27.00, ("anthropic", r, "PREPAID"): 19.80}`（E5 不在任何覆盖和里）；三种 `total_covered` 都是 `{(r, "INTERNAL_METERED_ONLY"): 27.00, (r, "PREPAID"): 19.80}` | AIH-TASK-247 |
| 查询次数 | integration（SQLite） | 同上世界与区间：不带筛选；`customer_id = T_int`；`customer_id` 不存在；`customer_id = T_int` 且 `project_id` 不存在；`customer_id` 不存在且带 `project_id` | 3；4；1（返回空输入）；2（空输入）；1（空输入） | AIH-TASK-247 |
| 分摊结果 | API（SQLite + MySQL，同「分摊输入」的世界） | 区间 `[2026-12-01, 2027-01-01)`，`group_by=project` 与 `provider` | `project`：P_crm 的 internal 块 `covered 27.00000000`、`reconciled_provider_cost_myr 29.70000000`、`provider_cost_myr 29.70000000`、`reconciled`，prepaid 块 `0.00000000` 与 `estimated`；P_pre 的 prepaid 块 `reconciled 21.78000000`、`provider_cost 21.78000000`、`reconciled`、`gross_margin_on_provider_cost = -21.78000000`、比率 null；P_chat 的 prepaid 块 `reconciled 0.00000000`、`provider_cost 450.00000000`、`estimated`；两行分摊之和 51.48 = R。`provider`：anthropic 行 prepaid 块 `covered 19.80`、`provider_cost 471.78000000`、`mixed`，internal 块 `29.70000000`、`reconciled`。合计：prepaid `provider_cost 471.78000000`、internal `29.70000000`；`reconciliation_coverage` 一项，`fully_in_range = true`、`allocatable = true`；`unallocated_reconciled_months = []`、`unallocated_reconciled_cost_myr = null` | AIH-TASK-248 |
| 马来西亚月分组 | API | 同世界，区间 `[2026-11-01, 2027-02-01)`，`group_by=month` | `2027-01` 组：prepaid 块 `reconciled 21.78000000`、`reconciled`，internal 块（E4 8.80 未覆盖）`provider_cost 8.80000000`、`estimated`；`2026-12` 组：internal 块（E1 覆盖 + E3 未覆盖）`provider_cost 34.20000000`、`mixed`，prepaid 块（E5）`450.00000000`、`estimated` | AIH-TASK-248 |
| 部分覆盖 | API | 同世界，区间 `[2026-12-15, 2027-01-01)` | 覆盖项 `fully_in_range = false`；合计 prepaid `reconciled 21.78000000`（只有 E2 在区间内），internal `covered 0`、`estimated` | AIH-TASK-248 |
| 分摊舍入 | API（`myr_three` + 12 月 `MYR 1.00` 记录） | `group_by=project`，区间 `[2026-12-01, 2027-01-01)` | 每组 internal `reconciled 0.33333333`；合计 internal `1.00000000`（文档写明的舍入误差） | AIH-TASK-248 |
| 不可分摊 | API（`unallocatable`） | 区间 `[2026-11-01, 2027-04-01)`，`group_by=provider` | 覆盖三项按月升序：12 月 `allocatable = true`、2 月 `false`、3 月 `true`（3 月是 E = 0 且 R = 0 的分支：`allocate` 返回 None 按 0、不进任何行、照常列入）；任何分组行与成本、毛利都不含 2 月的 5.00；`totals.unallocated_reconciled_months = ["anthropic:2027-02"]`、`unallocated_reconciled_cost_myr = "5.00000000"`；导出 TOTAL 行第 39 列 `anthropic:2026-12;anthropic:2027-03`、第 40 列 `anthropic:2027-02`、第 41 列 `5.00000000`，GROUP 行这两列为空 | AIH-TASK-248 |
| 未覆盖与旧键不变 | API | 标准世界、没有任何对账记录 | 新增块的 `covered = 0`、`basis = estimated`、`provider_cost = estimated`；`reconciliation_coverage = []`、`unallocated_reconciled_months = []`、`unallocated_reconciled_cost_myr = null`；AIH-TASK-222 的全部旧键与值不变 | AIH-TASK-248 |
| 导出追加列 | API | 「分摊结果」的条件分别导出 CSV 与 xlsx | 两种文件表头都是 41 列，前 31 列与原来逐字相同；32–41 列按 §2 填写（本条件下 TOTAL 行第 40、41 列为空）；CSV 第 39、40 列有公式注入防护；xlsx 逐格与 CSV 相同（237 的既有断言只改列数为 41 与新列） | AIH-TASK-248 |
| 前端分析页 | unit | mock 报告：合计 8 项（`mixed` 口径、一项 null）；覆盖：`anthropic 2026-12`（完整）、`anthropic 2027-03`（`fully_in_range = false`）与两条 `allocatable = false`（`anthropic 2027-01`、`anthropic 2027-02`；四条记录对应同一个连续区间 `[2026-12-01, 2027-03-15)`，只有末月 2027-03 不完整）；覆盖为空；表格行的三个新列 | 8 个 `<td>` 的文字逐个等于对应值（口径显示 `Mixed (partly reconciled)`，null 为 `—`），`<td>` 里没有别的文字；第一行恰为 `Reconciled months (UTC): anthropic 2026-12, anthropic 2027-03 (partial)`，mock 的 `unallocated_reconciled_cost_myr` 为 `"7.50000000"`，第二行恰为 `Not allocated (no estimated cost in that month, UTC): anthropic 2027-01, anthropic 2027-02 — reconciled cost not included in provider cost: 7.50000000 MYR`；覆盖为空时第一行为 none 文案、没有第二行；表格 Prepaid 组多两列、Internal 组多一列，小字为显示文案；没有 `role="alert"` | AIH-TASK-249 |
| INV-7 守卫 | unit | 追加新键名后的禁用名单；自检注入一个新名字 | 非管理端 schema 无交集；自检会失败 | AIH-TASK-248 |
| 事务中途失败、Redis / Celery、并发 | — | 只读 | 不适用：不写行、不用队列、不加锁 | — |

## 8. 迁移与上线

- **数据迁移**：无。不加表、列、索引。
- **性能**：每个与区间重叠的对账记录多两条范围聚合（§2「精确查询次数」）；走 `ix_usage_events_status_occurred`，outbox 探测的计划见 L1 §10 已知限制 1。
- **兼容窗口**：只新增键、只在导出末尾追加列；AIH-TASK-224 的前端不读新键照常工作。
- **部署顺序**：L1 的 AIH-TASK-246 → AIH-TASK-247（另依赖 AIH-TASK-221）→ AIH-TASK-248（另依赖 AIH-TASK-223）→ AIH-TASK-249（另依赖 AIH-TASK-224）；之后是 L1 的 AIH-TASK-250、251。每层合并即部署、互不破坏。
- **回滚**：只回滚应用镜像，没有数据要回滚。

## 9. 方案取舍

| 方案 | 优点 | 风险 | 未采用原因 |
| --- | --- | --- | --- |
| 采用：查询时按估算成本比例分摊、实时 E、逐事件按 UTC 月取系数 | 分摊之和恒等于实际成本；任何分组维度可用；不落库 | 比例分摊是规则，不是逐请求实际成本，必须标注 | — |
| 备选 G：把分摊额写进 `usage_events` 或新表 | 报表查询简单 | 改 `PROCESSED` 行（INV-6）或造出逐请求「实际成本」，spec §14 禁止 | 不采用 |
| 备选 M：分摊用对账记录里的估算快照 E | 可复现 | 确认后才处理的事件也按快照系数分，分摊之和不等于 R | 用实时 E；快照只做比较依据（L1） |
| 备选 N：只在合计层用对账后成本、分组行仍估算 | 不发明分组精度 | 按客户 / 项目看不到对账后成本；Kelvin 定要分摊 | 采用分摊并标明口径 |
| 备选 O：SQL 里按 `R / E` 直接相乘 | 少一次 Python 计算 | MySQL DECIMAL 除法的小数位有限，丢精度 | Python `Decimal` 计算 |

## 10. 未决问题与假设

- **我定的**：§1 的四条（比例分摊、实时 E、逐事件 UTC 月系数、旧键不变）。
- **已知限制**：分组行的分摊额之和与合计之间有至多「组数 × 0.5×10⁻⁸」的舍入差；比例分摊不是逐请求实际成本（页面与 `docs/api.md` 写明）。
- **假设**：AIH-TASK-221..224 按毛利分析设计实现（`_conditions`、按日 / 月分组、`totals`、导出 31 列、合计块组件）。若实现与设计有出入，本批照已合并的代码对齐、在 PR 里说明，不改那几层。
- **跨批次协调**：不加迁移；依赖 L1（AIH-TASK-243、244）与 AIH-TASK-221、223、224。

## 11. 审查与版本绑定

审查方要回答的五个问题常驻在 [scripts/review_checklist.md](../../scripts/review_checklist.md) 的「设计审查」一节，此处不重复。

**版本绑定规则**（规则的出处是 [WORKFLOW §3](../../docs/WORKFLOW.md)；这里保留一份是因为审查材料里只有本 Issue 正文，两处改一起改）：

- 批准必须写成 `APPROVED: design v<N>`，`<N>` 取本 Issue 顶部的「设计版本」
- **设计版本一变，之前的 APPROVE 自动作废**，必须重新过闸门
- 什么算「实质修改」：改了契约、表结构、事务边界、状态机、失败语义、不变量控制。改错别字不算
- 实质修改时：把顶部版本 +1，在下方追加一条变更说明，状态退回 `READY_FOR_REVIEW`

### 实现拆分

| 任务 | 层 | 依赖 | 预计改动 |
| --- | --- | --- | --- |
| `AIH-TASK-247` | 仓储（毛利分析） | AIH-TASK-246、AIH-TASK-221 | 约 600 行（代码 180、测试 420） |
| `AIH-TASK-248` | 接口与导出 | AIH-TASK-247、AIH-TASK-223 | 约 900 行（代码 260、测试 560、文档 80） |
| `AIH-TASK-249` | 前端 + 验收步骤 | AIH-TASK-248、AIH-TASK-224 | 约 750 行（页面与 api 220、测试 330、文案 40、验收脚本 160） |

通用验收：不改 `.platform/`、`.env*`、planning-v1 块，不装依赖；复选框只允许 `[ ]` / `[x]`；allowed_commands 的检查全部零退出，skipped 如实报告且不算 passed；Draft PR 交付，PR 正文「设计闸门」写本闸门编号；不改 `app/ai_api/` 下任何文件。

#### AIH-TASK-247

- **标题**：对账后成本第一层：用量分析仓储按对账记录汇总覆盖范围内的估算成本
- **可改路径**：
  - `app/repositories/usage_analytics.py`
  - `tests/backend/test_usage_analytics_reconciled.py`（新建）
- **验收要点**：
  1. `reconciled_allocation_inputs` 与 `CoveringRecord`、`AllocationInputs` 两个数据类的字段、键类型（记录键为 `public_id` 字符串，模式键为 `"PREPAID"` / `"INTERNAL_METERED_ONLY"` 字符串）如 §2「仓储」；步骤 1–4 与精确查询次数（`_conditions` 为 None 时恰好 `L` 次，否则 `L + 1 + 2n` 次，没有不分组的合计查询）逐条一致；分组表达式与连接与 `list_usage_analytics` 共用，`day` / `month` 键转换一致。
  2. 范围判定复用 L1 的 AIH-TASK-243 的 `ai_module_origin` 与 `current_records_overlapping`、`scope_estimated_myr`，不复制 SQL；`CoveringRecord.provider_code` 取自 `current_records_overlapping` 已连表带回的代码，不另查 `ai_providers`（否则超出查询预算）。
  3. 原有函数的 SQL 与行为不变，现有用例不改断言照常通过。
  4. 测试覆盖 §7 标 AIH-TASK-247 的行（SQLite 与 MySQL，夹具来自 `provider_cost_world`）。
  5. 不改接口、schema、前端。

#### AIH-TASK-248

- **标题**：对账后成本第二层：用量分析接口与导出加按比例分摊的供应商成本和基于它的毛利
- **可改路径**：
  - `app/schemas/usage_analytics.py`
  - `app/api/admin_usage_analytics.py`
  - `tests/backend/test_admin_usage_analytics_api.py`
  - `tests/backend/test_admin_usage_analytics_export_api.py`
  - `tests/backend/test_admin_usage_analytics_xlsx_export_api.py`（AIH-TASK-237 新建；只调整列数与新列的断言）
  - `tests/backend/test_internal_cost_visibility.py`
  - `docs/api.md`
- **验收要点**：
  1. 新键、逐块的口径判定、毛利与合计公式、`allocatable = (E > 0) 或 (R = 0)`、`fully_in_range` 与覆盖清单如 §2「规则」「推论」「接口与导出」；分摊用 AIH-TASK-244 的 `allocate`（返回 None 按 0），每个分组与每个合计块只 `quantize_money` 一次，合计用 `total_covered` 单独算。
  2. AIH-TASK-222 的全部旧键与值不变；`cost_basis` 仍为 `estimated`。
  3. 导出在 31 列后追加 10 列（含第 40、41 列的不可分摊披露），前 31 列逐字不变；CSV 与 xlsx 共用同一列常量、都是 41 列；第 39、40 列加公式注入防护；`totals` 的两个披露字段如 §2。
  4. INV-7 守卫追加新键名。
  5. `docs/api.md` 用量分析与导出两节补对账后成本口径、分摊方法、UTC 月覆盖与舍入误差说明。
  6. 测试覆盖 §7 标 AIH-TASK-248 的行（SQLite 与 MySQL 两半）。

#### AIH-TASK-249

- **标题**：对账后成本第三层：管理端用量分析页显示对账后供应商成本、口径与覆盖月份
- **可改路径**：
  - `frontend/src/api/adminUsageAnalytics.ts`
  - `frontend/src/api/adminUsageAnalytics.test.ts`
  - `frontend/src/features/usage/UsageAnalyticsPage.tsx`
  - `frontend/src/features/usage/UsageAnalyticsPage.test.tsx`
  - `frontend/src/features/usage/UsageAnalyticsTotals.tsx`
  - `frontend/src/features/usage/UsageAnalyticsTotals.test.tsx`
  - `frontend/src/i18n/locales/en.json`
  - `scripts/acceptance/admin_customers.mjs`
- **验收要点**：
  1. 语义以 `docs/api.md` 为准，页面如 §2「页面」；不改后端文件。
  2. 合计块追加的 8 项各占一对 `<th>` / `<td>`，`<td>` 里只有那一个值（后端字符串原样、口径的显示文案或短横），读的键、口径显示文案（Estimated / Reconciled / Mixed (partly reconciled)）、说明文字的位置、覆盖说明两行的格式与表格新列（Prepaid 组两列、Internal 组一列，小字为口径显示文案）逐字照 §2「页面」；不用 `Alert`、不用 `role="alert"`，合计块仍不是表格。
  3. 说明文字写明按估算成本比例分摊、不是逐请求实际成本、对账月份按 UTC 划分。
  4. 测试覆盖 §7 标 AIH-TASK-249 的行；已有用例照常通过。
  5. `check_analytics_provider_cost`：须先跑过 `open_usage_analytics`；后端值取页面自己发出的 GET usage-analytics 响应体（被动观察，取最近一次成功的）；合计块里标签恰为 `Provider cost (reconciled where available)` 与 `Gross profit on provider cost` 的两项，其 `<td>` 的全部文字分别等于 `totals.prepaid.provider_cost_myr` 与 `totals.gross_profit_on_provider_cost`（null 时短横）；不改筛选、不翻页。加进脚本（文件名不变），文件头同步；沿用验收脚本已有的约束；脚本只执行 stdin 里 steps 列出的步骤。
  6. CI 的 frontend job（含 build）全绿。
- **浏览器验收**：`acceptance.browser`，`writes: false`，`timeout_seconds: 300`，steps：`login`、`open_usage_analytics`、`check_analytics_provider_cost`。

### 设计闸门判定

- [ ] 需求、非目标和验收标准明确
- [ ] 关键契约与事务边界明确
- [ ] 触碰的不变量都有控制措施
- [ ] 失败路径都有确定的最终状态
- [ ] 高风险控制都有测试场景
- [ ] 没有未解决的阻断假设
- [ ] 审批绑定到明确的设计版本

## 12. 版本变更记录

| 版本 | 日期 | 改了什么 | 触发原因 |
| --- | --- | --- | --- |
| v1 | 2026-11-17 | 初稿（从合并设计拆出，内容含五轮登记预审的修订，尚未送审） | Issue 正文长度；Kelvin 拍板；登记预审 |
| v2 | 2026-11-17 | 不可分摊（E = 0 而 R > 0）的已对账成本不再在导出里静默消失：`totals` 加 `unallocated_reconciled_months`、`unallocated_reconciled_cost_myr`，导出追加第 40、41 列（CSV 与 xlsx 共用列常量，共 41 列），页面「Not allocated…」行带合计；§5、§6、§7（不可分摊、分摊结果、未覆盖、导出追加列、前端分析页）同步；§7 点明 3 月记录覆盖 E = 0 且 R = 0 分支 | #756 审查 REQUEST_CHANGES（导出漏披露，spec §14、§73） |

# AIH-TASK-221..225 设计：管理端毛利分析扩展与 CSV 导出（已批准 v2）

> **来源**：设计闸门 Issue #709。本文件是 `APPROVED: design v2` 那一版正文的**逐字副本**（送审时状态行为 `READY_FOR_REVIEW`）。审查：Codex 额度不可用期间由 Claude Code 审查。v1 获批准后，登记前预审（`worker.design_precheck`）报出的问题与审查建议项改进了 v2（见 §12），v2 重新审查通过、没有阻断项。
> 放进仓库，是因为 OpenClaw Worker 在沙箱里不联网、读不到 GitHub Issue。
> **实现以本文件为准**；与 Issue 不一致时，以 Issue #709 上被批准的 v2 为准。设计要改，就回到 Issue 升版本、重新过闸门，不要直接改本文件。
> 产品决定（在现有用量分析接口上扩展、按日按月、手续费按现金期间单独成数不分摊、只做 CSV、成本标 estimated、导出只记日志不写审计）由 Kelvin 2026-11-14 拍板，见 §1。

## 0. 准入判定

| 任务涉及 | 要填的范围 |
| --- | --- |
| 钱包 / 账本 / 定价 / 汇率 / 支付 / 幂等 / 状态机 | **全部章节** |
| 用量摄取 / 认证与会话（集成侧与平台侧）/ Webhook | §1–§7（**§6 不可省** —— 密钥存储与轮换、鉴权、租户过滤、日志脱敏都在那一节） |
| 前端 / 文档 / CI / 脚本 | **不走本闸门**，直接开 PR |
| 既不碰钱、也不碰用量摄取 / 认证与会话 / Webhook 的后端改动 | **不走本闸门**，直接开 PR |

本任务档位：`全部`。理由：本批只读、不写任何表，但它第一次定义**财务口径**——收入、估算成本、网关手续费、毛利与毛利率怎么算、按哪个期间归属（spec §86 要求写明现金期间与用量期间不得混用），并第一次让**带内部成本的数据离开系统**（管理端 CSV 导出，spec §73）。口径错了会让 Kelvin 按错误的毛利做定价决定；导出或接口误入客户侧就违反 INV-7。只读带来的「不触碰」在 §3 逐条说明。

---

状态：`READY_FOR_REVIEW`
负责人：Claude
**设计版本**：`v2`
对应需求：spec §14（估算 / 对账成本分开、未对账标 estimated、报表写明成本口径）、§44（手续费由 Acuven 承担、计入内部利润分析）、§55（管理端财务指标与 §87 筛选）、§73（CSV 与 Excel；管理端导出可含内部成本，客户端必须排除）、§86（毛利 = 计费 AI 收入 − 估算 / 对账成本 − 网关手续费；充值不是收入；手续费与对账调整写明期间口径）、§87（Customer / Project / Provider / Model / Date / Day / Month）、§109（展示时区 `Asia/Kuala_Lumpur`）、§130（Phase 7 收入、成本、手续费、毛利、毛利率、筛选、CSV / Excel 导出）；`REQ-PRIV-002`（Invariant 7）；Invariant 10、12；ADR-0003（马来西亚日历、用量期间按 `occurred_at`）；ADR-0008（`billable_cost` 是含税毛额）；建立在 AIH-TASK-073 / 074（用量分析接口与页面，契约在 `.platform/tasks.yaml` 的这两项与 `docs/api.md`「管理端用量分析」）之上
目标 PR：待开（拆成 `AIH-TASK-221`..`225`，见 §11）

## 1. 目标与边界

### 要解决的问题

AIH-TASK-073 交付了 `GET /api/v1/admin/usage-analytics`（`app/api/admin_usage_analytics.py:27-110`）：按供应商 / 模型 / 客户 / 项目 / 功能分组，给出 PREPAID 的计费额、估算成本、「毛利」与毛利率，内部计量租户单独一块。它的口径明确写着「不含网关手续费，也不是 Phase 7 财务毛利」（`docs/api.md:3344-3345`）。Phase 7（spec §130、`docs/TODO.md:364-366`）还缺：

1. **网关手续费**：`payments.gateway_fee`（`app/models/payments.py:191`）只在管理端支付详情里逐笔显示（`app/schemas/admin_payments.py:67`），没有任何汇总，毛利也没扣它（spec §44、§86）。
2. **按日 / 按月**：spec §87 的 Day、Month 两个维度没有；按时间看毛利只能逐段改时间区间。
3. **全量合计**：接口只返回一页分组，没有覆盖全部分组的合计；「这段时间总共赚了多少」得自己把所有页加起来。
4. **导出**：管理端没有任何导出（客户导出的设计 `docs/design/AIH-TASK-113-customer-export.md:48` 把管理端导出划给 Phase 7）。
5. **期间口径没写明**：spec §86 要求区分充值与已确认用量收入，并写明手续费的期间口径。

### Kelvin 2026-11-14 的决定

| 问题 | 决定 |
| --- | --- |
| 在哪里做 | **在现有 `GET /api/v1/admin/usage-analytics` 上扩展**，不另开财务报表接口 |
| 时间维度 | 加**按日、按月**分组（马来西亚日历，ADR-0003） |
| 网关手续费 | 按 `payments.paid_at` 的**现金期间单独成一个数**，**不分摊**到项目、模型（或任何分组行） |
| 导出格式 | 管理端**只做 CSV**；Excel 推迟，与客户端 Excel 一起做（要加依赖） |
| 成本口径 | 成本列标明 `estimated`；对账后成本等 Phase 8（数据源 `provider_cost_reconciliations` 还不存在） |
| 导出是否写审计（看过初稿后同日补定） | **不写审计**，只记一条 INFO 日志（管理员 id、分组维度、行数） |

我定的（未单独问，审查者重点看，§10 也列出）：

- 手续费在**合计层**扣：`毛利（扣手续费后）= 用量毛利合计 − 区间内手续费合计`；分组行只有用量毛利，不出现手续费。
- 带了 `customer_id` 筛选时，手续费只算这个客户的支付（手续费本来就逐笔属于一个租户，不是分摊）；带了 `project_id` / `provider` / `model` / `feature` 任一筛选时，手续费无法归属，**手续费块与扣手续费后的毛利为 null**，不给一个混了口径的数。
- 同一块里顺带给出区间内的**充值到账额**（`PAID` 支付的 `confirmed_amount` 合计），标明「现金期间、不是收入」，落实 spec §86「充值与用量收入分开」。
- 导出复用客户导出的进程级租约（同一进程同一时刻至多一个导出，管理端与客户端共用）。

### 可观察的完成标准

1. `group_by=day` / `month` 时，每个分组是一个马来西亚日（`YYYY-MM-DD`）或月（`YYYY-MM`），归属按 `occurred_at + 8 小时`；UTC `2026-10-31 15:59:59.999999` 归 `2026-10-31` / `2026-10`，`16:00:00` 归 `2026-11-01` / `2026-11`，SQLite 与 MySQL 结果相同；按日期升序。
2. 响应多一个 `totals`：覆盖全部分组（不随分页变化）的同口径合计，可加字段等于所有分组之和；`totals.payments` 给出 `paid_at` 落在同一区间的 `PAID` 支付的笔数、充值到账额、已知手续费合计与手续费未知的笔数；`totals.gross_profit = 用量毛利 − 已知手续费`，`totals.gross_profit_rate = gross_profit ÷ PREPAID 计费额`。
3. 响应顶层带 `usage_period_basis: "occurred_at"`，`totals.payments.period_basis` 为 `"paid_at"`，`cost_basis` 仍为 `"estimated"`；旧前端（074）不读新字段也照常工作。
4. `GET /api/v1/admin/usage-analytics/export` 用同样的参数返回一个 CSV：每个分组一行（`row_type = GROUP`，顺序同接口），最后一行 `row_type = TOTAL`（含手续费与毛利）；每行带成本口径、期间口径与区间；超过 20 000 个分组 422 `EXPORT_TOO_LARGE`；有导出在跑时 503 `EXPORT_BUSY`。
5. 管理端用量分析页：维度多「Day」「Month」；表格上方一块合计（收入、估算成本、用量毛利、手续费、扣手续费后的毛利与毛利率、充值到账额、内部计量）并写明各数的期间口径；手续费未知时显示「有 N 笔支付的手续费未知，毛利没有扣它们」；一个「Export CSV」按钮用当前已应用的条件下载。
6. 任何非管理端接口（客户门户、`/api/v1/me`、集成接口、支付建单、AI 模块）的响应 schema 里不出现成本、毛利、手续费字段，客户侧模块不导入毛利分析的仓储与 schema —— 由一个新的守卫测试钉住（INV-7）。

### 明确不做什么

- **不做对账后成本**：`cost_basis` 只有 `estimated`；对账后成本、对账覆盖期间等 Phase 8「供应商账单成本对账」。
- **不分摊手续费**到分组行（客户、项目、供应商、模型、功能、日、月）；按日 / 按月分组时也不把手续费放进当天 / 当月那一行（Kelvin 定「单独成一个数」）。
- **不做 Excel**（`.xlsx` 要新增依赖），与客户端 Excel 一起登记为后续计划。
- **不做 spec §55 的管理端仪表盘卡片**（`frontend/src/features/dashboard/DashboardPage.tsx` 仍是平台状态卡）；本批只扩展用量分析页。
- **不扣除账本调整**：`ADJUSTMENT_*`、`REFUND_ADJUSTMENT`、`BONUS`、`SYSTEM_CORRECTION`、`REBILL_*` 不进收入、不进毛利（口径见 §2「V1 口径」）。
- **不改**计费、支付、补对账、对账单的任何写路径；不加迁移、不加索引、不加依赖。
- **不加**手续费的人工录入（`gateway_fee` 永久为空的支付照样计入「未知」，见 §10）。
- **不做**异步导出、导出文件落盘、导出历史。

### 现有行为与证据

- 接口：`app/api/admin_usage_analytics.py:27-110`，第一条语句 `require_admin`（:41），参数校验 `parse_analytics_query`（`app/schemas/usage_analytics.py:17-47`：`group_by` 只认五个值 :30，跨度 ≤ 366 天 :45），毛利与毛利率在接口层算（:68-69，计费额为 0 时毛利率 null），金额 `money_text` 8 位（`app/schemas/customers.py:305-307`），比率 `ratio_text`（`app/schemas/usage_analytics.py:54-57`，`normalize` 后定点）。`UsageAnalyticsView.cost_basis` 固定 `"estimated"`（:89-94）。
- 仓储：`app/repositories/usage_analytics.py:38-159`。条件 `status = PROCESSED` 与 `occurred_at` 左闭右开（:53-57）；`customer_id` / `project_id` 先查 public id，查不到直接返回空（:58-71）；分组表达式（:79-99）；PREPAID / INTERNAL 按 `billing_mode_snapshot` 用 `CASE` 分块（:101-133，内部租户只有估算成本与参考售价）；排序事件数倒序、分组键升序（:139）；总数用子查询 `COUNT(*)`（:138）。
- 马来西亚日的方言表达式已有：`MalaysiaDay`（`app/repositories/usage_events.py:571-597`），MySQL `DATE(DATE_ADD(col, INTERVAL 8 HOUR))`，SQLite `date(substr(col, 1, 19), '+8 hours')`（先截到整秒，避免 SQLite 毫秒取整把 `15:59:59.999999` 进到次日）；客户本月用量按它 `GROUP BY` / `ORDER BY`（:631-647，AIH-TASK-107）。
- 支付：`gateway_fee` 可空，注释写明 MYR（`app/models/payments.py:190-191`）；`paid_at` 是「本系统做出可信确认的时刻，不是 Stripe 的付款时刻」（:201-202）；`PAID` 必有 `paid_at`、`confirmed_amount`、`confirmed_currency`（`PAID_FIELDS_CHECK` :129-132）。自动确认只在费用币种为 MYR 时写 `gateway_fee`，否则为空并记 WARNING（`app/services/payments.py:363-374`）；内部计量租户的付款进复核、不入账（:346-347）；人工按实付入账把 `confirmed_amount` 设为实收额（`app/services/payment_review.py:400-405`）；补对账第 2 段只在建单 8 天内补抓手续费，之后永久为空（`app/services/payment_reconcile.py:30-43`、`docs/payment-flow.md:309-320`，那里写着「它只影响内部毛利分析」）。`payments` 上没有 `paid_at` 索引，只有 `ix_payments_status_created`（:227）与 `ix_payments_tenant_created`（:226）。
- 导出基础设施：`app/core/csv_export.py`——20 000 行上限（:49）、进程级租约 `try_acquire_export_lease`（:155）、`ExportLeaseMiddleware` 在 `app/main.py:186-188` 是最外层、`write_csv`（BOM、公式注入只防声明的文本列，:212-238）、`csv_response`（文件名只许 `[A-Za-z0-9._-]`，:269-280）、`CustomerExportFailed`（信封与通用 500 逐字相同，:94-105）。客户导出的处理函数写法：`app/api/customer.py:261-325`（先 503 再拿租约、只 `hold` 不释放、意外异常在 `except` 外抛）。
- 前端：`frontend/src/api/adminUsageAnalytics.ts`（类型与 `getUsageAnalytics`）、`frontend/src/features/usage/UsageAnalyticsPage.tsx`（437 行；说明文字不用 antd `Alert`，因为验收脚本把任何 `role="alert"` 当错误）、`frontend/src/api/download.ts:112`（`downloadFile`，带令牌取文件、解析错误信封）。
- 路由守卫：`tests/backend/test_admin_customers_api.py:120`（`USAGE_ANALYTICS_ROUTE`）、:197（`EXPECTED_ADMIN_ROUTES`）、:994-997（401 / 403 探测补查询串）。
- 对账单按账本入账期间（含 `PRIOR_PERIOD_ADJUSTMENT`，ADR-0003），本报表按 `occurred_at`：同一个月的两个数可能不同，这是口径差异，不是错误。

## 2. 设计概要

### 数据流

```text
GET /admin/usage-analytics（ADMIN 令牌）
  → require_admin（第一条语句，401 / 403）→ parse_analytics_query（422）
  → 同一个只读会话（不提交；MySQL REPEATABLE READ 下第一次读建立快照，后面四条看同一快照）：
      ① 分组一页（list_usage_analytics）② 分组总数 ③ 全量合计（usage_analytics_totals）
      ④ 手续费块：带 project_id / provider / model / feature 任一 → 不查、为 null；否则 paid_payment_totals
  → 接口层用 Decimal 算毛利、毛利率、扣手续费后的毛利 → 200
GET /admin/usage-analytics/export
  → require_admin → 同一个校验 → 取 session factory（503）→ 拿导出租约（503 EXPORT_BUSY）→ hold
  → 同一个只读会话：①（offset 0，limit 20 001）③ ④ → > 20 000 行 422 EXPORT_TOO_LARGE
  → GROUP 行 + TOTAL 行 → write_csv → 分段发送 → 最外层中间件释放租约
（无状态变化、无财务效果、无出站事件、无审计、不落盘）
```

### V1 口径（写进 `docs/api.md`、页面与 CSV）

| 量 | 定义 | 期间 |
| --- | --- | --- |
| 用量收入（usage revenue） | `status = PROCESSED`、`billing_mode_snapshot = PREPAID` 的事件 `billable_cost` 之和。含税毛额（ADR-0008）；Acuven 未注册 SST、不拆税额，所以它就是收入 | `occurred_at ∈ [occurred_from, occurred_to)` |
| 估算供应商成本 | 同一批事件的 `estimated_provider_cost_myr` 之和；`cost_basis = estimated` | 同上 |
| 用量毛利 | 用量收入 − 估算成本（AIH-TASK-073 的 `gross_margin`，分组行与合计都有） | 同上 |
| 网关手续费 | `status = PAID` 的支付的 `gateway_fee` 之和（只加非空的）；为空的笔数单独报 `gateway_fee_unknown_count` | `paid_at ∈ [occurred_from, occurred_to)`（**现金期间**） |
| 充值到账额 | 同一批支付的 `confirmed_amount` 之和（= 各自 `TOPUP` 账本行金额）；**不是收入** | 同上（现金期间） |
| 毛利（扣手续费后） | 用量毛利合计 − 网关手续费合计，**只在合计层** | 两个期间都写明，见下 |
| 毛利率 | 毛利（扣手续费后）÷ 用量收入；用量收入为 0 时 null | — |
| 内部计量 | `INTERNAL_METERED_ONLY` 事件的估算成本与参考售价；**不是收入、不进任何毛利**（`docs/design/INTERNAL-METERED-ONLY-2026-10-08.md`） | `occurred_at` |

spec §86 要求「不得把现金期间与用量期间混在一起而不标注」：扣手续费后的毛利**确实**把两个期间的数相减（Kelvin 定手续费按 `paid_at` 单独成一个数），所以接口用字段（`usage_period_basis`、`payments.period_basis`）、页面用说明文字、CSV 用列把两个期间都写出来。查询的两个区间是同一对时刻。

V1 明确**不进**这个报表的（页面与 `docs/api.md` 写明）：

- 账本调整：`ADJUSTMENT_CREDIT` / `ADJUSTMENT_DEBIT`、`REFUND_ADJUSTMENT`、`BONUS`、`SYSTEM_CORRECTION`、`REBILL_*` 都不增减收入。推论：客户用 `BONUS` 或调账加的额度消费，仍按 `billable_cost` 计入用量收入。
- Stripe 侧的外部退款与争议（支付第二批只告警、由 Kelvin 人工调账）：不减充值到账额，也不改手续费；争议费不在 `payments` 上，不计。
- 非 `PAID` 的支付（`REVIEW_REQUIRED`、人工 `CLOSED` 后的 `FAILED`）即使 Stripe 收了钱、扣了手续费，也不计。
- 对账单数字：对账单按账本入账期间（含上期调整），本报表按 `occurred_at`，同一个月可能不同（INV-12 不受影响）。

### 接口一：`GET /api/v1/admin/usage-analytics`（扩展，AIH-TASK-222）

**参数**：与 AIH-TASK-073 完全相同，只有 `group_by` 多两个合法值 `day`、`month`（`UsageAnalyticsGroupBy` 扩到七个，`parse_analytics_query` :30 的集合同步）。跨度上限 366 天、校验顺序、错误信封不变。

**分组键**：

| `group_by` | `group_key` | `group_name` | 排序 |
| --- | --- | --- | --- |
| `provider` / `model` / `customer` / `project` / `feature` | 不变 | 不变 | 不变：事件数倒序、分组键升序 |
| `day` | 马来西亚日 `YYYY-MM-DD` | `null` | 分组键升序（时间顺序） |
| `month` | 马来西亚月 `YYYY-MM` | `null` | 分组键升序 |

只返回有事件的日 / 月，不补零行（与其他维度一致）。区间两端不在马来西亚零点时，首尾两个日 / 月桶只含区间内的事件（页面与文档写明「首尾可能不完整」）。

**响应 `data`**：AIH-TASK-073 的全部键原样保留，新增两个键：

```json
{
  "cost_basis": "estimated",
  "usage_period_basis": "occurred_at",
  "items": [ "…AIH-TASK-073 的分组项，形状不变…" ],
  "page": 1, "page_size": 20, "total": 3,
  "totals": {
    "event_count": 120, "token_event_count": 118,
    "input_tokens": 1180, "output_tokens": 590,
    "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
    "average_input_tokens": "10", "average_output_tokens": "5",
    "prepaid": { "event_count": 100, "billable_cost": "10.00000000", "estimated_provider_cost_myr": "6.00000000",
                 "gross_margin": "4.00000000", "gross_margin_rate": "0.4" },
    "internal_metered_only": { "event_count": 20, "estimated_provider_cost_myr": "1.00000000",
                               "reference_customer_price": "2.00000000" },
    "payments": { "period_basis": "paid_at", "paid_count": 3, "topup_amount": "300.00000000",
                  "gateway_fee": "3.00000000", "gateway_fee_unknown_count": 1 },
    "gross_profit": "1.00000000",
    "gross_profit_rate": "0.1"
  }
}
```

- `totals` 前 10 个键与 `prepaid`、`internal_metered_only` 两块的形状、类型、算法与分组项**完全相同**（同一个构造函数），区别只是不分组、不分页：可加字段等于全部分组之和（含 null 分组），平均值按合计重新算。
- `payments`：带了 `project_id`、`provider`、`model`、`feature` 中任一参数（参数出现即算，含空串——与这些参数「按原值比较」的现有语义一致）时为 **`null`**；否则为对象，带 `customer_id` 时只算该客户的支付。`customer_id` 查不到时（现有行为是分组为空）`payments` 为全零对象（`paid_count = 0`、两个金额 `"0.00000000"`、`gateway_fee_unknown_count = 0`）。
- `gross_profit = totals.prepaid.billable_cost − totals.prepaid.estimated_provider_cost_myr − payments.gateway_fee`；`payments` 为 null 时 `gross_profit` 与 `gross_profit_rate` 都为 null。`gross_profit_rate = gross_profit ÷ totals.prepaid.billable_cost`，分母为 0 时 null。**手续费未知的那几笔没有被扣**：`gateway_fee_unknown_count > 0` 时 `gross_profit` 偏高，页面必须提示。
- 金额 `money_text` 8 位；比率与平均值 `ratio_text`；全程 Decimal，不经 float。
- 兼容：只新增键、只扩大 `group_by` 的取值集合；074 的前端不读新键、不发新值，照常工作。

### 接口二：`GET /api/v1/admin/usage-analytics/export`（新增，AIH-TASK-223）

放在同一个路由器（`app/api/admin_usage_analytics.py`），`response_class=ExportCsvResponse`。

**参数**：`group_by`、`occurred_from`、`occurred_to`、`customer_id`、`project_id`、`provider`、`model`、`feature`，语义与接口一完全相同；没有 `page` / `page_size`（传了按未知参数忽略）。

**检查顺序**（照 `app/api/customer.py:261-325`）：

1. `require_admin(request)`（第一条语句）→ 401 / 403。
2. `parse_analytics_query(…, page=1, page_size=1, max_page=1, max_page_size=1)`：复用同一个校验函数，分页那两项恒通过 → 422 `VALIDATION_ERROR`（信封与接口一相同）。
3. `require_session_factory(request)` → 数据库没配置时 503，**早于拿租约**。
4. `try_acquire_export_lease()` 为 `None` → 503 `EXPORT_BUSY`（带 `Retry-After: 5`）；拿到后 `request.scope[EXPORT_LEASE_SCOPE_KEY].hold(token)`，之后不在处理函数里释放。
5. 一个只读会话：`list_usage_analytics(offset=0, limit=EXPORT_MAX_ROWS + 1)`、`usage_analytics_totals`、手续费块（规则同接口一）。分组行数 > 20 000 → 422 `EXPORT_TOO_LARGE`（不返回部分文件）。
6. 拼 CSV、构造 `csv_response`；成功后记一条 INFO `admin_usage_analytics_exported`，`extra` 只有 `user_id`（管理员内部 id，同 `app/api/two_factor.py:124-127` 的写法）、`group_by`、`row_count`（GROUP 行数）。
7. `AppError` 原样抛；其他异常 → `logger.exception("admin_usage_analytics_export_failed", extra={"path": …})`，在 `except` 块之外抛 `CustomerExportFailed`（它的信封就是通用 500 `INTERNAL_ERROR`，名字里的 Customer 不影响对外结果；不改 `csv_export.py`）。

**文件**：`csv_response(filename, body)`，`Content-Disposition: attachment`、`Cache-Control: no-store`。文件名 `usage-analytics_<group_by>_<from>_<to>.csv`，`<from>` / `<to>` 是解析后的 UTC 时刻加 8 小时、格式 `%Y%m%dT%H%M%S` 再接字面 `MYT`，例：`usage-analytics_month_20260101T000000MYT_20270101T000000MYT.csv`（只含合法字符，不拼任何客户输入）。

**列**（恰好 31 列，按此顺序；`write_csv` 的格式、BOM、`\r\n` 不变）：

| # | 列 | GROUP 行 | TOTAL 行 |
| --- | --- | --- | --- |
| 1 | `row_type` | `GROUP` | `TOTAL` |
| 2 | `group_by` | 请求的 `group_by` | 同左 |
| 3 | `group_key` | 分组键（null 组为空） | 空 |
| 4 | `group_name` | 名称或空 | 空 |
| 5–12 | `event_count`、`token_event_count`、`input_tokens`、`output_tokens`、`cache_creation_input_tokens`、`cache_read_input_tokens`、`average_input_tokens`、`average_output_tokens` | 分组项的值 | `totals` 的值 |
| 13–17 | `prepaid_event_count`、`prepaid_billable_cost`、`prepaid_estimated_provider_cost_myr`、`prepaid_gross_margin`、`prepaid_gross_margin_rate` | 分组项 `prepaid` | `totals.prepaid` |
| 18–20 | `internal_event_count`、`internal_estimated_provider_cost_myr`、`internal_reference_customer_price` | 分组项 `internal_metered_only` | `totals.internal_metered_only` |
| 21–24 | `paid_payment_count`、`topup_amount`、`gateway_fee`、`gateway_fee_unknown_count` | 空 | `totals.payments` 的值；`payments` 为 null 时空 |
| 25–26 | `gross_profit`、`gross_profit_rate` | 空 | `totals` 的值；null 时空 |
| 27 | `cost_basis` | `estimated` | `estimated` |
| 28 | `usage_period_basis` | `occurred_at` | `occurred_at` |
| 29 | `fee_period_basis` | 空 | `paid_at`；`payments` 为 null 时空 |
| 30–31 | `occurred_from`、`occurred_to` | 区间的 UTC RFC 3339：微秒为 0 时写 `YYYY-MM-DDTHH:MM:SSZ`，否则写恰好 6 位小数 `YYYY-MM-DDTHH:MM:SS.ffffffZ`（在导出模块里用一个小函数格式化，不新增共用工具） | 同左 |

- 单元格全部由接口层按 JSON 视图的值转成字符串（整数 `str()`、金额与比率沿用接口一的字符串、null → 空），`write_csv` 只收 `str | None`。
- 公式注入防护的文本列：`group_key`、`group_name`（功能标签、公司名、模型代码来自输入）。数值列不加前缀（负毛利 `-0.40000000` 必须保持是数）。
- GROUP 行顺序与接口一相同；TOTAL 行恒在最后、恒存在（没有分组时文件 = 表头 + TOTAL 行，仍报手续费）。TOTAL 行不计入 20 000 上限。

### 仓储（`app/repositories/usage_analytics.py`，AIH-TASK-221）

- **按月表达式** `MalaysiaMonth(FunctionElement[int])`，写法照 `MalaysiaDay`，结果类型 `Integer`，值为 `YYYYMM` 整数：
  - MySQL：`EXTRACT(YEAR_MONTH FROM DATE_ADD(col, INTERVAL 8 HOUR))`
  - SQLite（只在测试里用）：`CAST(strftime('%Y%m', substr(col, 1, 19), '+8 hours') AS INTEGER)`
  - 小时数由 `KUALA_LUMPUR_OFFSET`（`app/core/fx_source.py:55`）算出，不另写字面 8。MySQL 一侧刻意不用 `DATE_FORMAT`：见 §9。
- **按日**直接用 `MalaysiaDay`（从 `app/repositories/usage_events.py` 导入，不复制）。
- `list_usage_analytics` 签名不变；`group_by` 为 `day` / `month` 时：分组表达式即 `group_key`，`group_name` 为 `NULL`，没有 join，`ORDER BY` 只按该表达式升序；行转换时 `day` 的 `datetime.date` → `isoformat()`，`month` 的整数 `v` → `f"{v // 100:04d}-{v % 100:02d}"`，所以返回的 `UsageAnalyticsRow.group_key` 仍是 `str | None`。其余五种分组的 SQL 一字不改。
- 把 :53-77 的条件构造抽成内部函数 `_conditions(session, …) -> list[ColumnElement[bool]] | None`（`None` 表示 `customer_id` / `project_id` 查不到），三个公开函数共用；行为不变。
- `usage_analytics_totals(session, *, customer_id, project_id, provider, model, feature, occurred_from, occurred_to) -> UsageAnalyticsTotals`：与分组查询**相同的 12 个聚合列、相同条件**，不 `GROUP BY`、不 join，恰好一行（`COALESCE(…, 0)`）；条件为 `None` 时不查库，返回全零。
- `paid_payment_totals(session, *, customer_id, paid_from, paid_to) -> PaidPaymentTotals`（`paid_count`、`topup_amount`、`gateway_fee`、`gateway_fee_unknown_count`）：

  ```sql
  SELECT COUNT(*),
         COALESCE(SUM(confirmed_amount), 0),
         COALESCE(SUM(gateway_fee), 0),               -- SUM 本来就跳过 NULL
         COALESCE(SUM(CASE WHEN gateway_fee IS NULL THEN 1 ELSE 0 END), 0)
  FROM payments
  WHERE status = 'PAID' AND confirmed_currency = 'MYR'
    AND paid_at >= :paid_from AND paid_at < :paid_to
    [AND tenant_id = :tenant_id]                      -- 仅当给了 customer_id；查不到客户时返回全零、不查
  ```

  只算 `confirmed_currency = 'MYR'`：现有写入路径只会写 MYR（自动确认的判定条件、人工入账写 `PAYMENT_CURRENCY`），`gateway_fee` 也只在 MYR 时写入（§1「现有行为」）；但数据库对 `confirmed_currency` 没有 CHECK（`PAID_FIELDS_CHECK` 只要求非空），所以查询自己加这个条件，以后若出现写入非 MYR 的路径，非 MYR 的支付不会被当作 MYR 加进合计。不按 `billing_mode` 过滤：内部计量租户的付款不会成为 `PAID`；租户事后改计费模式，它此前的手续费照样是 Acuven 付出的现金成本。
- 三个函数都只读、不加锁、不提交；`paid_payment_totals` 只用可移植的 SQL（`COUNT`、`SUM`、`CASE`、比较），没有方言函数。

### 页面（`frontend/src/features/usage/UsageAnalyticsPage.tsx` 等，AIH-TASK-224）

- `frontend/src/api/adminUsageAnalytics.ts`：`USAGE_ANALYTICS_GROUP_BYS` 加 `day`、`month`；类型加 `usage_period_basis`、`totals`（含 `payments: … | null`、`gross_profit: string | null`、`gross_profit_rate: string | null`）；新增 `usageAnalyticsExportUrl(query)`（参数规则同 `getUsageAnalytics`：省略与空白的可选筛选不进查询串，不带分页）与 `exportUsageAnalytics(query, signal)`，经 `downloadFile`，回退文件名 `usage-analytics.csv`。
- 维度选择加「Day」「Month」。表格的分组单元格（现有 `GroupCell`，名称与键合在一格）在按日 / 按月时只显示日期键（`YYYY-MM-DD` / `YYYY-MM`），不显示名称行；不新增列，其他维度的显示不变。
- 表格**上方**一块「Totals」：只要本次查询成功就显示，**包括 `total` 为 0**（没有任何分组）的时候，此时各值就是后端返回的零值字符串；表格自身的空状态照旧。合计块的写法（`<section role="region" aria-label="Totals">`，用带边框的 antd `Descriptions`：标签在 `<th>`、值在 `<td>`；**不用** `Table`，免得 `check_usage_analytics` 把它数成数据行；**不用** `Alert`）。逐项原样显示后端字符串：Usage revenue、Estimated provider cost、Usage gross margin、Gateway fees、Gross profit、Gross margin（ratio）、Top-ups received（标「cash, not revenue」）、Paid payments、内部计量的事件数 / 估算成本 / 参考售价；null 显示短横。
- 说明文字（替换现有 `usageAnalytics.estimateNote` 的「不含网关手续费」那句）：收入、成本与用量毛利按调用时间（`occurred_at`，所选区间）；手续费与充值按付款确认时间（`paid_at`，同一区间）；成本为估算值；毛利 = 用量毛利 − 手续费；对账单按记账期间，数字可能不同；首尾日 / 月可能不完整。
- `gateway_fee_unknown_count > 0`：在合计块里显示「N paid payment(s) have no recorded gateway fee; gross profit does not deduct them.」；`payments` 为 null：显示「Gateway fees cannot be attributed to project, provider, model or feature filters, so gross profit is not shown.」。都是普通文字，不是 `role="alert"`。
- 「Export CSV」按钮：用**当前已应用**的条件（与表格同一组，不是表单里未提交的草稿），下载中不可再点；失败在按钮下方显示错误码、信息与 `request_id`（`EXPORT_TOO_LARGE` / `EXPORT_BUSY` 沿用 `download.ts` 的错误常量），下一次点导出或条件变化时清除。导出不影响表格与合计的查询。
- 金额、比率不转 `number`；文案进 `en.json`。

### 事务边界

- 接口一与导出各用一个只读会话、不提交：分组页、总数、合计、手续费四条 SELECT 在同一个事务里（MySQL `REPEATABLE READ` 一致读，同一快照）。导出的「生成 CSV」在会话关闭之后做（不持连接拼文件）。
- 不写任何行，不写审计，不写 outbox。

### 时间语义

- `occurred_at`：`DATETIME(6)` 不带时区的 UTC；`paid_at`：不带时区的 UTC，整秒，是本系统可信确认的时刻（人工按实付入账时是管理员结案的时刻，可能比客户付款晚几天）。
- 查询区间是解析后的一对 UTC 时刻，用量与手续费用同一对。
- 马来西亚日 / 月 = UTC 加固定 8 小时（马来西亚不实行夏令时；与 `KUALA_LUMPUR_OFFSET`、ADR-0003、AIH-TASK-107 一致），不依赖数据库时区表。

### 金额与精度

- 各 SUM 在数据库里对 `DECIMAL(20,8)` 精确求和；毛利、扣手续费后的毛利在 Python 用 `Decimal` 相减，8 位小数相减无舍入；`money_text` 只做 8 位定点格式化。
- 比率与平均值：`Decimal` 除法（默认上下文 28 位有效数字）后 `ratio_text`，与 AIH-TASK-073 同一函数、同一结果。
- `gateway_fee`（两位小数）与 `confirmed_amount`（两位小数）的合计以 8 位小数输出（`"3.00000000"`），与报表其余金额同一格式。

## 3. 不变量影响矩阵

| 不变量 | 是否触碰 | 可能怎样被破坏 | 设计控制 | 验证方式 |
| --- | --- | --- | --- | --- |
| INV-1 中心故障不中断 AI | 是（间接） | AI 模块与本平台共用数据库（spec §141.5）；管理员跑跨 366 天的合计与导出占用数据库资源；导出租约是进程级的，管理端导出期间客户导出会拿到 503 | 只读、不加锁（一致读不阻塞计费写入）；跨度上限 366 天不变；同一进程同一时刻至多一个导出；AI 调用路径不读这些表的聚合。没有执行预算是现有接口的已知状态，见 §10 | §7「导出租约」；§8 性能说明 |
| INV-2 事件不重复扣费 | 否 | 只读 `usage_events`，不改状态、不计费 | — | — |
| INV-3 支付不重复入账 | 否 | 只读 `payments`，不改状态、不入账 | — | — |
| INV-4 余额只经账本变动 | 否 | 不读也不写钱包 | — | — |
| INV-5 历史账本不可变 | 否 | 不读账本 | — | — |
| INV-6 事件保留版本引用 | 否 | 只读事件上已冻结的快照列，不重算、不按当前价格重估 | 报表用的是处理时保存的 `estimated_provider_cost_myr` 与 `billable_cost` | 现有用例「金额精确」照跑 |
| INV-7 客户不可见成本毛利 | **是** | 新字段（手续费、毛利）或导出被挂到客户接口；客户侧模块导入毛利 schema；客户令牌调到管理端导出 | 两个接口都在 `/api/v1/admin` 路由器、第一条语句 `require_admin`；新增守卫测试：非管理端路由的响应 schema 不含禁用字段名、客户侧模块不导入毛利分析模块、客户导出列常量不含禁用字段名；路由枚举测试覆盖导出路径 | §7「INV-7 守卫」「鉴权」 |
| INV-8 租户不可互访 | 是 | 客户令牌调用管理端报表看到全部租户的数据 | 只有 ADMIN 可调（401 / 403）；管理端本就跨租户，不按租户过滤是设计 | §7「鉴权」 |
| INV-9 对话内容不入库 | 否 | 只读计量列；导出不含 `conversation_id`、请求内容 | 列白名单（§2） | §7「CSV 列」 |
| INV-10 金额用 Decimal | 是 | 前端或接口把金额转 float；CSV 写 float | 数据库 DECIMAL 求和、Python Decimal 相减与相除、输出字符串；前端原样显示字符串；CSV 单元格只收 `str` | §7「金额精度」「前端不经 number」 |
| INV-11 event_id 至多一次财务效果 | 否 | 无财务效果 | — | — |
| INV-12 定稿对账单不可变 | 否 | 不读也不写对账单；报表与对账单口径不同只在文档说明 | 文档与页面写明「对账单按记账期间，数字可能不同」 | 文档审查 |
| INV-13 状态与事件原子提交 | 否 | 没有状态变化与出站事件 | — | — |
| INV-14 队列丢失不毁持久工作 | 否 | 同步只读、不落盘、不入队 | — | — |

## 4. 状态与并发

### 状态

无状态机：不新增状态、不改任何状态。

### 并发

- **与计费并发**：报表的四条 SELECT 在同一个事务里，看到同一个快照；事务开始后才变成 `PROCESSED` 的事件这次不计，下次计入。一致读不加锁，不阻塞计费 worker。
- **与手续费补抓并发**：补对账第 2 段把 `gateway_fee` 从空写成值（条件更新）。同一个报表请求里看到的是快照，不会出现「既计入未知笔数又计入金额」；两次请求之间数字会变（未知笔数减少、手续费增加），**报表不是定稿数字**（文档写明）。
- **与人工入账并发**：`resolve_review` 提交后同一支付的 `paid_at` 是结案时刻，之后的请求按它归期。
- **两个导出同时到达**：租约 `try_acquire` 在进程锁里判空，后到者立即 503 `EXPORT_BUSY`，不排队（现有机制，`app/core/csv_export.py:117-149`）。
- 幂等：只读 GET，重放结果只随数据变化。没有幂等键、没有重试层。

## 5. 失败与恢复矩阵

| 失败点 | 对外结果 | 数据最终状态 | 是否可重试 | 恢复来源 | 告警 |
| --- | --- | --- | --- | --- | --- |
| 未登录 / 非 ADMIN | 401 `TOKEN_INVALID` / 403 `ADMIN_REQUIRED`（校验之前） | 不变 | — | — | — |
| 校验失败（`group_by`、时间、跨度、`feature`、分页） | 422 `VALIDATION_ERROR`，只列字段名 | 不变 | 改参数后 | — | — |
| 数据库没配置 | 503（导出在拿租约之前） | 不变；导出不占租约 | 是 | 配好数据库 | 现有 |
| 查询时数据库错误（断连、超时） | 500 `INTERNAL_ERROR`；导出记 `admin_usage_analytics_export_failed` 带栈 | 不变；导出租约由最外层中间件在请求结束时释放 | 是 | 重新请求 | ERROR 日志 |
| 导出分组 > 20 000 | 422 `EXPORT_TOO_LARGE`，不给部分文件 | 不变；租约释放 | 缩小区间或加筛选 | — | — |
| 已有导出在跑 | 503 `EXPORT_BUSY`，`Retry-After: 5` | 不变 | 是（几秒后） | — | 持有超 120 秒时现有 `customer_export_lease_held_long` |
| 客户端中途断开 | 无 | 不变；租约在中间件 `finally` 释放 | — | — | — |
| 手续费永久未知（8 天后仍拿不到、或费用非 MYR） | 200，`gateway_fee_unknown_count > 0`，`gross_profit` 偏高，页面提示 | 支付行 `gateway_fee` 保持为空 | 否（V1 无录入路径） | 按 `docs/payment-flow.md` 的处置到 Stripe 后台核对（AIH-TASK-225 写入）；人工录入另过闸门（§10） | 无（不是故障） |
| Redis / Celery 丢失 | 不影响（本批不用队列） | 不变 | — | — | — |
| 外部服务 | 不调用任何外部服务 | — | — | — | — |
| 重复请求 | 同一结果（随数据变化） | 不变 | — | — | — |

## 6. 数据与安全边界

- **鉴权**：两个接口都在 `/api/v1/admin` 路由器、第一条语句显式 `require_admin`（角色以库为准；ADMIN 令牌必过 2FA）。导出路径加进 `EXPECTED_ADMIN_ROUTES`，401 / 403 探测补查询串（`tests/backend/test_admin_customers_api.py:994-997` 的写法）。
- **租户过滤**：管理端跨租户，不按租户过滤；`customer_id` 只是筛选。客户、集成方与 AI 模块的任何接口都不暴露这些字段（INV-7 守卫，§7）。
- **客户侧禁止的字段**（守卫测试的禁用名单）：`estimated_provider_cost_myr`、`provider_source_cost`、`fx_rate_applied`、`reference_customer_price`、`gross_margin`、`gross_margin_rate`、`gross_profit`、`gross_profit_rate`、`gateway_fee`、`gateway_fee_unknown_count`、`cost_basis`、`markup`。现有非管理端 schema 里都没有这些名字（`grep` 只命中 `app/api/admin_*`、`app/schemas/admin_payments.py`、`pricing_*`、`usage_analytics.py`、`usage_events.py`，都是管理端）。
- **日志**：导出成功一条 INFO（`user_id`、`group_by`、`row_count`），失败一条带栈的 ERROR（只带路径）；不记筛选值、公司名、金额、文件内容。页面查询不记日志（现状）。
- **审计**：不写。理由：spec §66 的审计清单是改变状态的敏感操作；导出的内容与管理员在页面上看到的同一份数据、同一权限；在 GET 里写审计会让「审计写失败」变成导出失败的新原因。是否改为记审计请 Kelvin 定（§10）。
- **导出文件**：服务器不保存；文件里有客户公司名（`group_by=customer`）与 Acuven 内部成本、毛利，离开系统后由管理员负责保管（`docs/api.md` 写明「含内部成本，不得转发给客户」）。
- **密钥**：不新增、不读取。
- **prompt / response**：不处理；CSV 不含 `conversation_id`、请求 id 与任何内容字段。
- **数据保留**：不新增数据，不影响保留与删除。

## 7. 测试证据计划

后端用例遵守仓库根 `CLAUDE.md`「写后端测试的约定」：SQL 文本断言不写死方言字面量（`MalaysiaMonth` 的 MySQL 编译文本只在 `if engine.dialect.name == "mysql":` 分支里断言）；跨文件共用的构件从不以 `test_` 开头的辅助模块导入（支付行用 `tests/backend/payment_world.py` 的 `build_sqlite_world` / `build_mysql_world` 由本测试模块自己写夹具，`paid_payment_maker` 已在 conftest 注册）；示例 uuid 全零；测试金额都能精确表示，MySQL 分支逐位断言。

| 风险/需求 | 测试层级 | 场景 | 预期结果 | 任务 |
| --- | --- | --- | --- | --- |
| 按日 / 按月归属 | integration（SQLite + MySQL，现有 `billed` 夹具） | 事件 `occurred_at` 为 UTC `2026-10-31 15:59:59.999999`、`16:00:00`、`2026-12-31 15:59:59`、`16:00:00` | 日：`2026-10-31`、`2026-11-01`、`2026-12-31`、`2027-01-01`；月：`2026-10`、`2026-11`、`2026-12`、`2027-01`；两种库相同；`group_name` 为 null | AIH-TASK-221 |
| 日 / 月排序与分页 | integration | 三个月各有事件，事件数各不相同；`page_size=2` | 按键升序（不按事件数），第 2 页是第三个月；`total = 3` | AIH-TASK-221 |
| 其他五种分组不变 | integration | 现有用例 | 全部照常通过，未改动断言 | AIH-TASK-221 |
| 月表达式的编译 | unit | 对 MySQL 与 SQLite 方言编译含 `MalaysiaMonth` 的语句 | MySQL 文本含 `EXTRACT(YEAR_MONTH` 且不含 `%`；SQLite 文本含 `strftime`；带绑定参数在 MySQL 上真执行不报错（上一行的 MySQL 分片） | AIH-TASK-221 |
| 合计与分组一致 | integration（SQLite + MySQL） | 两个 PREPAID 客户、一个内部客户、一条 null 功能、一条 QUANTITY 事件、一条非 PROCESSED 事件；对五种分组各取全部页 | `usage_analytics_totals` 的 12 个聚合字段等于各分组之和；非 PROCESSED 不计；QUANTITY 不进 token 合计；与 `group_by` 无关 | AIH-TASK-221 |
| 合计的筛选与空集 | integration | `customer_id` 查不到；`project_id` 查不到；区间内无事件 | 全零，查不到时不发聚合查询（计数 SQL 语句数） | AIH-TASK-221 |
| 手续费汇总 | integration（SQLite + MySQL，`payment_world` 世界） | 本租户区间内 `PAID` 三笔：手续费 `1.50` 且 `paid_at` 恰为 `occurred_from`（计）、`0.75` 在区间中间、手续费为空在区间中间；本租户另一笔 `PAID` 手续费 `9.99` 且 `paid_at` 恰为 `occurred_to`（不计）；本租户区间内 `confirmed_currency = 'USD'` 的 `PAID` 一笔、手续费 `5.00`（直接插入，不计）；`PENDING`、`REVIEW_REQUIRED`、`CLOSED`（`FAILED`）各一笔带手续费；另一个租户区间内 `PAID` 一笔、手续费 `0.25` | 不带客户：`paid_count = 4`（本租户 3 + 另一租户 1）、`gateway_fee = 2.50`、未知 1、`topup_amount` = 这 4 笔 `confirmed_amount` 之和；带客户：只算该租户；非 `PAID` 不计；查不到客户全零 | AIH-TASK-221 |
| 接口新字段与兼容 | API（SQLite + MySQL） | 正常请求 | `data` 恰好 AIH-TASK-073 的键加 `usage_period_basis`、`totals`；`totals` 恰好 §2 列出的键；分组项形状不变；`group_by=day` / `month` 合法 | AIH-TASK-222 |
| 合计不随分页 | API | 同一条件 `page=1` 与 `page=2` | 两次 `totals` 完全相同 | AIH-TASK-222 |
| 扣手续费后的毛利 | API | 计费额 `10`、估算成本 `6`、手续费 `1.50` 与空各一笔 | `gross_profit = "2.50000000"`、`gross_profit_rate = "0.25"`、`gateway_fee_unknown_count = 1` | AIH-TASK-222 |
| 手续费不可归属 | API | 分别带 `project_id`、`provider`、`model`、`feature`（含 `provider=` 空串）；只带 `customer_id` | 前四种 `payments`、`gross_profit`、`gross_profit_rate` 为 null，用量合计照常；只带客户时 `payments` 是该客户的 | AIH-TASK-222 |
| 分母为 0 | API | 只有内部计量事件，区间内有手续费 | `prepaid.billable_cost = "0.00000000"`、`gross_profit` 为负的手续费、`gross_profit_rate` 为 null | AIH-TASK-222 |
| 金额精度 | API（SQLite + MySQL） | 三条 `0.00000001` 计费额；手续费 `0.01` 三笔 | `"0.00000003"`；`"0.03000000"`；比率逐位等于 `Decimal` 计算值 | AIH-TASK-222 |
| 校验 | API | `group_by=week`；跨度 367 天；未登录与非 ADMIN 带非法参数 | 422 `VALIDATION_ERROR`；401 / 403 先于 422 | AIH-TASK-222 |
| INV-7 守卫 | unit（新文件） | 对 `create_app(...)` 的 OpenAPI：每个不以 `/api/v1/admin/` 开头的路径的全部响应（含 `$ref` 递归展开的组件）的属性名；AST 扫描 `app/api/customer.py`、`app/api/customer_payments.py`、`app/api/me.py`、`app/api/integration_usage.py`、`app/api/payments.py` 与 `app/ai_api/` 下全部模块的 import；客户四个导出列常量 | 属性名与 §6 禁用名单无交集；没有一个模块导入 `app.repositories.usage_analytics`、`app.schemas.usage_analytics`、`app.api.admin_usage_analytics`；列常量与禁用名单无交集；另有一条自检：把一个禁用名注入假 schema 时守卫会失败（防止守卫空转） | AIH-TASK-222 |
| 导出正常路径 | API（SQLite + MySQL） | `group_by=customer` 两个客户 + 手续费 | 200、`text/csv; charset=utf-8`、`Content-Disposition` 文件名符合 §2、`Cache-Control: no-store`；BOM；表头恰好 31 列按序；两行 GROUP 顺序同接口一、一行 TOTAL 在末尾；TOTAL 行的值逐列等于接口一的 `totals` | AIH-TASK-223 |
| 导出不可归属与空集 | API | 带 `model`；区间内无事件 | TOTAL 行第 21–26、29 列为空；空集时文件 = 表头 + TOTAL 行 | AIH-TASK-223 |
| 导出上限 | API（SQLite） | 把 `EXPORT_MAX_ROWS` monkeypatch 为 2，造 3 个分组；恰好 2 个分组 | 422 `EXPORT_TOO_LARGE`；恰好 2 个时 200（TOTAL 不计入） | AIH-TASK-223 |
| 导出租约 | API | 预先占住租约；导出中途注入异常 | 503 `EXPORT_BUSY` + `Retry-After: 5`；500 `INTERNAL_ERROR` 且请求结束后 `export_lease_held()` 为假；数据库未配置时 503 且没拿租约 | AIH-TASK-223 |
| 公式注入 | API | 功能标签或公司名以 `=`、`-` 开头；负毛利 | 文本列前加单引号；负金额列不加 | AIH-TASK-223 |
| 导出日志 | API | 成功导出 | 恰好一条 `admin_usage_analytics_exported`，`extra` 只有三个键，不含公司名与金额 | AIH-TASK-223 |
| 鉴权 | API | 导出与接口一：匿名、客户令牌（带合法参数） | 401 / 403；`EXPECTED_ADMIN_ROUTES` 含导出路径 | AIH-TASK-222、AIH-TASK-223 |
| 前端 api | unit（vitest） | `getUsageAnalytics` 发 `group_by=month`；`usageAnalyticsExportUrl` 省略空筛选、不带分页；`exportUsageAnalytics` 经 `downloadFile` | 路径与查询串正确 | AIH-TASK-224 |
| 前端合计块 | unit（Testing Library） | `payments` 有值且未知 0；未知 2；`payments` 为 null；`gross_profit_rate` 为 null | 各值原样显示；未知提示含数量；null 提示与短横；没有 `role="alert"`；合计块不是表格 | AIH-TASK-224 |
| 前端维度与导出 | unit | 选 Day / Month 后请求参数；导出用已应用条件（改了表单不提交时仍用旧条件）；下载中不可点；失败显示码、信息与 `request_id`，再点时清除 | 与 §2 一致 | AIH-TASK-224 |
| 前端不经 number | unit | 金额 `"0.10000001"`、比率 `"0.333333333333333333333333333"` | 原样显示 | AIH-TASK-224 |
| 浏览器验收 | acceptance（只读） | `check_analytics_totals`、`check_analytics_export` | 见 §11 AIH-TASK-224 | AIH-TASK-224 |
| 事务中途失败 | — | 只读，没有部分写入 | 不适用：任何失败都不留下状态（导出租约见上） | — |
| Redis / Celery 丢失 | — | 不使用队列 | 不适用 | — |
| 并发 | — | 一致读快照由 InnoDB 保证；两个导出并发由租约用例覆盖 | 不另写行锁用例：没有锁 | — |

## 8. 迁移与上线

- **数据迁移**：无。不加表、列、索引。
- **锁表与性能**：
  - 用量：分组与合计都走 `ix_usage_events_status_occurred`（`app/models/usage.py:378`）的区间扫描，合计比现状多一次同范围扫描（每次请求两次聚合扫描）；按日 / 按月的分组键是表达式，`GROUP BY` 在扫描结果上做。一致读不加锁。
  - 支付：没有 `paid_at` 索引；带 `customer_id` 时走以 `tenant_id` 打头的索引之一（`ix_payments_reuse` 或 `ix_payments_tenant_created`，由优化器选），否则走 `ix_payments_status_created` 的 `status` 前缀，扫全部 `PAID` 行再按 `paid_at` 过滤。支付笔数（充值次数）远小于用量事件，V1 接受；笔数上来后加 `(status, paid_at)` 索引需另一个迁移（§10）。
  - PR 正文写明 MySQL `EXPLAIN` 实际选用的索引（照 AIH-TASK-073），不预设是哪一个。
- **兼容窗口**：接口只新增键与取值；074 的前端在 AIH-TASK-222 合并后照常工作。AIH-TASK-224 之前页面看不到新功能，接口可直接调用。
- **部署顺序**：AIH-TASK-221 → AIH-TASK-222 → AIH-TASK-223 → AIH-TASK-224 → AIH-TASK-225，每层合并即部署、互不破坏：AIH-TASK-221 只加仓储能力（接口仍拒绝 `day` / `month`）；AIH-TASK-222 开放接口；AIH-TASK-223 加导出路径；AIH-TASK-224 前端；AIH-TASK-225 文档。
- **回滚**：只回滚应用镜像；没有数据要回滚。
- **部分部署**：前端与后端同一次部署；AIH-TASK-224 依赖 AIH-TASK-222、AIH-TASK-223 已在生产，登记时用 `depends_on` 串起来。
- **监控**：无新告警；导出失败与租约占用沿用现有 ERROR / WARNING 日志。

## 9. 方案取舍

| 方案 | 优点 | 风险 | 未采用原因 |
| --- | --- | --- | --- |
| 采用：扩展现有接口；合计层扣按 `paid_at` 归期的手续费；分组行只给用量毛利；SQL 表达式按马来西亚日 / 月分组；CSV 末尾一行 TOTAL | 一个报表、一套口径；手续费不发明分摊精度；分组在数据库里完成，内存只与分组数有关 | 扣手续费后的毛利把两个期间相减，必须标注 | — |
| 备选 A：按收入比例把手续费分摊到客户 / 项目 / 模型 | 每行都有「净毛利」 | 分摊比例是发明出来的，spec §14 反对编造精度 | Kelvin 定不分摊 |
| 备选 B：把手续费按用量期间归属（找出这笔充值被哪些用量消耗） | 两个数同一期间 | 预付钱包里充值与消费没有一一对应，做不到确定的匹配 | 不可实现 |
| 备选 C：Python 侧按日 / 月分桶（取逐条事件再聚合） | 不写方言表达式 | 366 天的事件可能有数百万条，全部取进内存；分页与排序要在 Python 重做 | 内存与延迟不可接受；`MalaysiaDay` 已有先例 |
| 备选 D：MySQL `CONVERT_TZ(col, '+00:00', 'Asia/Kuala_Lumpur')` | 时区语义显式 | 命名时区要求服务器装时区表，未装时返回 NULL；SQLite 没有对应函数 | 马来西亚无夏令时，固定 +8 与现有实现一致 |
| 备选 E：MySQL 用 `DATE_FORMAT(…, '%Y-%m')` 出月份字符串 | 直接得到 `YYYY-MM` | 自定义编译函数里的字面 `%` 在 MySQL 驱动的 `format` 参数风格下要写成 `%%`（语句带绑定参数），SQLite 一侧又不能双写，两边写法不同、容易出错 | `EXTRACT(YEAR_MONTH …)` 没有 `%`，格式化放在 Python |
| 备选 F：导出另设管理端专用租约 | 管理端导出不挡客户导出 | 多一套租约与中间件槽；进程内存上界翻倍 | 管理端导出罕见、分组行少、生成快；共用即可 |
| 备选 G：导出写审计 | 留下谁在何时导出了内部成本 | GET 里写库，审计写失败会让导出失败；同样的数据页面查看不审计 | 记 INFO 日志；请 Kelvin 定（§10） |
| 备选 H：现在就做 Excel | 一次满足 spec §73 | 新增依赖（需管理员先加进 `pyproject.toml`）；Worker 不能装依赖 | Kelvin 定推迟，与客户端 Excel 一起 |
| 备选 I：CSV 的合计单独一个文件或放进表头注释 | 每个文件只有一种行 | 两个文件易丢一个；注释行会被表格软件当数据 | 一个文件、`row_type` 区分 |
| 备选 J：带项目 / 供应商 / 模型 / 功能筛选时仍扣全部手续费 | 总有一个毛利数 | 「Anthropic 的毛利」被扣掉全公司的手续费，数字没有意义 | 置 null 并说明 |

## 10. 未决问题与假设

- **我定的口径，请审查者重点看**（Kelvin 已授权按建议推进；审查认为不妥就升版本改）：
  1. 带 `project_id` / `provider` / `model` / `feature` 筛选时手续费块与扣手续费后的毛利为 null；只带 `customer_id` 时按该客户的支付算。
  2. 合计块顺带给出区间内的充值到账额（现金期间，标明不是收入）——spec §86 要求区分充值与收入，但你的决定里没提，不要就删。
  3. V1 口径：只计 `PAID` 支付的手续费（`REVIEW_REQUIRED` 与人工 `CLOSED` 的支付即使 Stripe 扣了手续费也不计）；外部退款、争议费不计；账本调整（含 `BONUS`）不增减收入，用赠送额度的消费仍算用量收入。
- **已知限制**：
  1. 手续费 8 天内补不到（或费用非 MYR）就永久为空，V1 没有人工录入路径；报表持续报「N 笔未知」、毛利偏高。要录入需另过闸门（改 `payments` 的写路径与审计）。
  2. 管理端分析的查询没有执行预算（AIH-TASK-073 起就没有；客户侧有 2 秒）。本批每次请求多一次同范围聚合扫描。数据量上来、跨年查询变慢时，另开任务加 `MAX_EXECUTION_TIME` 与 `QUERY_TOO_BROAD`。
  3. 区间两端不在马来西亚零点时，按日 / 按月的首尾桶不完整（页面与文档说明，不在后端强制对齐）。
  4. 共用导出租约的 WARNING 日志名是 `customer_export_lease_held_long`，管理端导出占用过久时也用这个名字（不改 `csv_export.py`，精准修改）。
  5. `paid_at` 是系统确认时刻：人工按实付入账的支付按结案日归期，可能与客户实际付款跨月。
- **假设**：
  1. `gateway_fee` 只在 MYR 时写入（`app/services/payments.py:351-374`、`app/services/payment_review.py:400-405`）；`confirmed_currency` 由查询条件过滤为 MYR（数据库没有 CHECK）。
  2. 生产里 `billing_mode_snapshot` 为空的 `PROCESSED` 事件（若有）照现状只计入事件数与 token、不进 PREPAID 与内部两块；合计沿用同一规则。
  3. 每个分组查询的结果行数 ≤ 20 000 对管理端足够（7 种维度里最多的是客户、项目、模型；按日最多 367 行）。
- **如果假设错误**：1 → 非 MYR 的支付不进合计（查询条件挡住），不会被误加；§7「手续费汇总」用直接插入的 USD `PAID` 行钉住；2 → 合计与分组同口径，不会自相矛盾，只是这批事件不进收入，与 AIH-TASK-073 现状一致；3 → 422 `EXPORT_TOO_LARGE`，提示缩短区间。
- **跨批次协调**：本批不加迁移，不占用迁移编号。

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
| `AIH-TASK-221` | 仓储 | — | 约 450 行（代码 150、测试 300） |
| `AIH-TASK-222` | 接口 + INV-7 守卫 | AIH-TASK-221 | 约 700 行（代码 180、测试 470、文档 50） |
| `AIH-TASK-223` | 导出接口 | AIH-TASK-222 | 约 550 行（代码 170、测试 340、文档 40） |
| `AIH-TASK-224` | 前端 + 验收步骤 | AIH-TASK-223 | 约 850 行（页面与 api 260、前端测试 330、验收脚本 220、文案 40） |
| `AIH-TASK-225` | 文档 | AIH-TASK-224 | 约 60 行 |

#### AIH-TASK-221

- **标题**：毛利分析第一层：用量分析仓储层加按马来西亚日与月分组、全量合计与已付款手续费汇总
- **可改路径**：
  - `app/repositories/usage_analytics.py`
  - `tests/backend/test_admin_usage_analytics_api.py`
  - `tests/backend/test_usage_analytics_payments.py`（新建）
- **验收要点**：
  1. 按本设计 §2「仓储」：`MalaysiaMonth`（两种方言写法、无 `%` 的 MySQL 文本）、`list_usage_analytics` 支持 `day` / `month`（键格式、`group_name` 为 null、按键升序），其余五种分组的 SQL 不变、现有用例不改断言照常通过。
  2. `_conditions` 抽取后行为不变；`usage_analytics_totals` 与分组查询同列同条件、恰好一行、查不到客户或项目时不查库返回全零。
  3. `paid_payment_totals` 按 §2 的 SQL：`PAID`、`paid_at` 左闭右开、可选租户、未知笔数、`confirmed_amount` 合计；只用可移植 SQL。
  4. 测试覆盖 §7 标 AIH-TASK-221 的行；`billed` 夹具的 SQLite 与 MySQL 两半都跑；支付用例用 `payment_world` 的 `build_sqlite_world` / `build_mysql_world` 在新测试模块里自写参数化夹具（不在用例模块之间转引用夹具）。
  5. SQL 文本断言不写死方言字面量；MySQL 专属断言放进方言分支；示例 uuid 全零。
  6. 不改接口、schema、前端；不加迁移、不装依赖；不改 `.platform/`、planning-v1 块；复选框只允许 `[ ]` / `[x]`。
  7. allowed_commands 的检查全部零退出，skipped 如实报告且不算 passed；Draft PR 交付，PR 正文「设计闸门」写本闸门编号，并写明用到的索引。

#### AIH-TASK-222

- **标题**：毛利分析第二层：用量分析接口加日与月分组、合计、现金期间手续费与毛利，并加客户侧内部成本守卫
- **可改路径**：
  - `app/schemas/usage_analytics.py`
  - `app/api/admin_usage_analytics.py`
  - `tests/backend/test_admin_usage_analytics_api.py`
  - `tests/backend/test_internal_cost_visibility.py`（新建）
  - `docs/api.md`
- **验收要点**：
  1. `group_by` 接受七个值；`data` 新增 `usage_period_basis` 与 `totals`，键与类型恰好如 §2「接口一」；分组项构造与合计共用一个函数。
  2. 手续费块的 null 规则、`customer_id` 规则、查不到客户时的全零；`gross_profit` 与 `gross_profit_rate` 的公式与 null 条件，全程 Decimal。
  3. 四条 SELECT 在同一个会话里、不提交；处理函数第一条语句仍是 `require_admin`。
  4. 新建 INV-7 守卫：OpenAPI 非管理端路径的响应属性名、客户侧与 AI 模块的 import、客户导出列常量三项检查，禁用名单取 §6；含一条防空转自检。
  5. 测试覆盖 §7 标 AIH-TASK-222 的行（SQLite 与 MySQL 两半）。
  6. `docs/api.md`「管理端用量分析」一节改写口径：新维度、`totals`、两个期间口径、手续费的 null 规则与未知笔数、V1 不计的项、与对账单的差异；删去「不含网关手续费，也不是 Phase 7 财务毛利」的旧句。
  7. 不改前端、不改 `app/repositories/`；不加迁移、不装依赖；不改 `.platform/`、planning-v1 块；检查全部零退出、skipped 不算 passed；Draft PR。

#### AIH-TASK-223

- **标题**：毛利分析第三层：管理端用量分析的 CSV 导出接口
- **可改路径**：
  - `app/api/admin_usage_analytics.py`
  - `app/schemas/usage_analytics.py`
  - `tests/backend/test_admin_usage_analytics_export_api.py`（新建）
  - `tests/backend/test_admin_customers_api.py`
  - `docs/api.md`
- **验收要点**：
  1. `GET /api/v1/admin/usage-analytics/export` 的参数、检查顺序（鉴权 → 校验 → 503 → 租约 → 查询 → 上限）与 §2「接口二」逐条一致；复用 `parse_analytics_query`、`try_acquire_export_lease`、`write_csv`、`csv_response`、`ExportTooLarge`、`ExportBusy`、`CustomerExportFailed`，不改 `app/core/csv_export.py`。
  2. 31 列恰好按 §2 的顺序与填法；列常量与单元格函数放在 `app/schemas/usage_analytics.py`；文本列只有 `group_key`、`group_name`。
  3. TOTAL 行恒在末尾、不计入 20 000 上限；文件名按 §2 的 MYT 格式。
  4. 成功一条 INFO、失败一条带栈 ERROR，`extra` 如 §2；不写审计。
  5. `EXPECTED_ADMIN_ROUTES` 加导出路径，401 / 403 探测给导出路径补与接口一相同的查询串。
  6. 测试覆盖 §7 标 AIH-TASK-223 的行；租约用例结束后断言租约已释放。
  7. `docs/api.md` 在用量分析一节后补导出小节（列表、行上限、503、文件名、「含内部成本，不得转发给客户」）。
  8. 不改前端；不加迁移、不装依赖；不改 `.platform/`、planning-v1 块；检查全部零退出、skipped 不算 passed；Draft PR。

#### AIH-TASK-224

- **标题**：毛利分析第四层：管理端用量分析页加日月分组、合计与毛利块和 CSV 导出按钮
- **可改路径**：
  - `frontend/src/api/adminUsageAnalytics.ts`
  - `frontend/src/api/adminUsageAnalytics.test.ts`
  - `frontend/src/features/usage/UsageAnalyticsPage.tsx`
  - `frontend/src/features/usage/UsageAnalyticsPage.test.tsx`
  - `frontend/src/features/usage/UsageAnalyticsTotals.tsx`（新建）
  - `frontend/src/features/usage/UsageAnalyticsTotals.test.tsx`（新建）
  - `frontend/src/i18n/locales/en.json`
  - `scripts/acceptance/admin_customers.mjs`
- **验收要点**：
  1. 语义以 `docs/api.md` 的用量分析与导出两节为准，交互以本设计 §2「页面」为准；与后端冲突时停下在 PR 里说明。不改任何后端文件，不改 `frontend/src/api/client.ts`、`frontend/src/api/download.ts`；不装依赖。
  2. api 封装：两个新维度、新类型、导出 URL 与下载函数（省略空筛选、不带分页、回退文件名）。
  3. 合计块：`role="region"`、`aria-label` 为 Totals，带边框的 Descriptions，原样显示字符串、null 显示短横；两种提示文字；不用 Table、不用 Alert。
  4. 说明文字写明两个期间口径、估算成本、对账单差异、首尾桶不完整；替换旧的「不含网关手续费」说明。
  5. 导出按钮用已应用条件、下载中不可点、失败显示码、信息与 `request_id` 并在下次点击或条件变化时清除。文案进 `en.json`，满足 no-hardcoded-jsx-text 与 keys.test.ts。
  6. 测试覆盖 §7 标 AIH-TASK-224 的行；已有用例照常通过；示例 uuid 全零。
  7. `check_analytics_totals`：须先跑过 `open_usage_analytics`（否则 assertion_failed），不自己导航；后端值取页面自己发出的 GET usage-analytics 响应体（被动观察，取点顶栏之后成功读完的最近一次）；合计块里 Usage revenue、Gross profit、Gateway fees 三项的 `<td>` 文字分别等于 `totals.prepaid.billable_cost`、`totals.gross_profit`、`totals.payments.gateway_fee`（null 时为短横）；不改筛选、不翻页。`check_analytics_export`：同一页上文字为 Export CSV 的按钮恰好一个且可用，**不点**。两步加进脚本（文件名不变），文件头步骤说明同步；已有步骤行为不变；沿用验收脚本已有的约束；脚本只执行 stdin 里 steps 列出的步骤。
  8. CI 的 frontend job（含 build）全绿；检查全部零退出、skipped 不算 passed；Draft PR。
- **浏览器验收**：`acceptance.browser`，`writes: false`，`timeout_seconds: 300`，steps：`login`、`open_usage_analytics`、`check_analytics_totals`、`check_analytics_export`。

#### AIH-TASK-225

- **标题**：毛利分析第五层：支付流程文档补毛利报表口径与手续费未知的处置，勾选 Phase 7 对应项
- **可改路径**：
  - `docs/payment-flow.md`
  - `docs/TODO.md`
- **验收要点**：
  1. `docs/payment-flow.md` §5.4 之后补一小节：手续费在毛利报表里按 `paid_at` 归期、为空的计入未知笔数、8 天后仍空时的处置（到 Stripe 后台核对实际手续费，V1 不录入，毛利偏高的量级按未知笔数估）。
  2. `docs/TODO.md` Phase 7：「筛选」改 `[x]`（注明 AIH-TASK-222 交付的 Day / Month 与原有维度）；「收入 / 估算成本 / 对账后成本 / 网关手续费 / 毛利 / 毛利率」与「CSV / Excel 导出」保持 `[ ]`，正文写明已交付的部分（估算口径与手续费、CSV）与剩下的（对账后成本等 Phase 8、Excel 与客户端一起）。
  3. 不改代码与测试；不改 `.platform/`、planning-v1 块；复选框只允许 `[ ]` / `[x]`；检查全部零退出；Draft PR。

登记 PR 另在 planning-v1 的后续计划里加两项（不属于以上任务）：「管理端与客户端 Excel 导出（需加依赖）」「手续费人工录入（需设计闸门）」（后者等 Kelvin 对 §10 已知限制 1 的意见）。

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
| v1 | 2026-11-14 | 初稿 | — |
| v2 | 2026-11-14 | ① 手续费 / 充值查询加 `confirmed_currency = 'MYR'`，改正 §10 假设 1 的论据（数据库对币种没有 CHECK）；② §7「手续费汇总」的夹具写死各笔的位置与手续费（含恰在区间两端、USD 一笔），期望值随之写明；③ §8 不再预设客户筛选走哪个索引，改为 PR 里写 `EXPLAIN` 实际结果；④ 导出 30–31 列的时间格式写死；⑤ 前端：按日按月时分组单元格只显示日期键；合计块在 `total` 为 0 时也显示 | v1 批准后的审查建议项 1 与登记前预审（AIH-TASK-221 `NOT_READY`：两条 DESIGN_DEFECT、一条 UNDECIDED；223、224 的 UNDECIDED） |

# AIH-TASK-119 / 120 设计：客户月度用量汇总 CSV 导出（已批准 v1）

> **来源**：设计闸门 Issue #457。本文件是 `APPROVED: design v1` 那一版正文的**逐字副本**（审查者为 Codex；送审时状态行为 `READY_FOR_REVIEW`，批准后 Issue 上只把状态行改成了 `APPROVED: design v1`）。
> 放进仓库，是因为 OpenClaw Worker 在沙箱里不联网、读不到 GitHub Issue。
> **实现以本文件为准**；与 Issue 不一致时，以 Issue #457 上被批准的 v1 为准。设计要改，就回到 Issue 升版本、重新过闸门，不要直接改本文件。
> **前置**：设计闸门 #403（[AIH-TASK-107-customer-usage-summary.md](AIH-TASK-107-customer-usage-summary.md)）的 `AIH-TASK-107`（汇总函数与视图）；设计闸门 #435（[AIH-TASK-113-customer-export.md](AIH-TASK-113-customer-export.md)）的 `AIH-TASK-114`（CSV 模块）与 `AIH-TASK-117`（下载辅助函数）。
> §8 的两个实现任务登记为 `AIH-TASK-119`（接口）→ `AIH-TASK-120`（门户用量页的月度汇总导出区），按这个顺序依赖。

---

## 0. 准入判定

| 任务涉及 | 要填的范围 |
| --- | --- |
| 钱包 / 账本 / 定价 / 汇率 / 支付 / 幂等 / 状态机 | **全部章节** |
| 用量摄取 / 认证与会话（集成侧与平台侧）/ Webhook | §1–§7（**§6 不可省**） |
| 前端 / 文档 / CI / 脚本 | 不走本闸门 |
| 既不碰钱、也不碰用量摄取 / 认证与会话 / Webhook 的后端改动 | 不走本闸门 |

本任务档位：`全部`。只读，不写任何表、不改表结构，但把客户按月汇总的计费额（`billable_cost` 合计）以文件形式交给客户（INV-7），金额要与 #403 的本月用量和 #435 的请求级导出逐分对得上（INV-10）。只读带来的「不触碰」在 §3 逐条说明理由。

---

状态：`待审`（`READY_FOR_REVIEW`）
负责人：Claude
**设计版本**：`v1`
对应需求：spec §73「Export Functionality」（至少请求级用量、会话摘要、钱包流水、**月度用量汇总**；「Customer export MUST exclude internal costs」）、§128（Phase 5「customer usage exports」）、§67（本月用量口径）、§80（金额精度）、§87（Date / Month 维度）、§109（展示时区 `Asia/Kuala_Lumpur`）、§112（导出不在服务端保存）、`REQ-PRIV-001`（INV-8）、`REQ-PRIV-002`（INV-7）
前置设计：#403 v1（客户门户本月用量：`GET /api/v1/customer/usage-summary`、两条 SELECT 同一快照、补齐日子、5 键白名单，AIH-TASK-107..108）、#435 v10（客户 CSV 导出：`app/core/csv_export.py` 的 CSV 格式、公式注入防护、`ExportCsvResponse` 与响应头，AIH-TASK-113..118）、#398 v4（2 秒执行预算与 `QUERY_TOO_BROAD`）
目标 PR：待开（批准后按 §8 拆两个任务登记；`docs/TODO.md`「后续计划」里「Phase 5 客户端导出的其余部分」中的「月度用量汇总导出」即本闸门）

## 1. 目标与边界

- **要解决的问题**：spec §73 要求的四种导出里，请求级用量与钱包流水已由 #435 交付，月度用量汇总还没有。#435 §1 把它明确延后到这里，并写定口径复用 #403 的本月用量：一个马来西亚日历月每天一行加合计。门户首页只显示当月、只看不能拿走，客户月初对上个月的账时要一份汇总文件。
- **可观察的完成标准**：
  1. `GET /api/v1/customer/usage-summary/export?month=YYYY-MM` 返回登录客户自己那个马来西亚日历月的 CSV 附件：该月每一天一行（28–31 行），最后一行合计。
  2. 每一行的请求数与计费额、合计行的请求数、计费额与对话数，与同一时刻 `GET /api/v1/customer/usage-summary?month=` 同一个月的同名值**逐字相同**（同一个 repository 函数、同一个白名单视图，§2「投影」）。
  3. 金额原样 8 位小数，不舍入；文件里没有成本、汇率、参考价、毛利、token 合计或内部 id。
  4. 查询沿用 2 秒执行预算，超时 422 `QUERY_TOO_BROAD`；不出现部分文件。
  5. 门户用量页加一个「月度汇总」导出区：选月份（默认当前马来西亚月）后点按钮下载文件。
- **明确不做什么**（Kelvin 2026-10-27 逐项确认）：
  - **合计放在末尾一行**，`date_myt` 列写 `TOTAL`；不另做表头外的元数据行。
  - **对话数只在合计行给**（当月不同的非空 `conversation_id` 个数，与首页卡片同一口径），每天的行留空：各日去重数相加不等于整月去重数（#403 §9 备选 E），给了容易被客户在表格里误加。
  - **入口在用量页**，带月份选择；首页本月用量卡不加按钮。
  - 只做 CSV（`.xlsx` 仍在「后续计划」）；不做会话摘要导出（随会话摘要）；不做按项目、供应商、模型的拆分（与 #403 一致）；不做跨月、多月或日期区间的汇总：一个文件恰好一个月。
  - 不建聚合表、不缓存：与 #403 相同，读时实时算。
  - 不写任何表、不改表结构、不加索引；不写审计。
- **现有行为与问题证据**：
  - `app/repositories/usage_events.py` 的 `summarize_processed_events_for_customer(session, tenant_id, *, occurred_from, occurred_to, timeout_ms)`：两条 SELECT（按日、对话去重）在同一个会话里、不提交，MySQL 上各带 `MAX_EXECUTION_TIME(2000)`，超时转 `QueryTooBroad`。
  - `app/schemas/customer_usage.py`：`usage_month(month, *, now)` 校验 `YYYY-MM` 与 `2020-01`..`2099-12` 并换成 UTC 区间；`customer_usage_summary_view(first_day, summary)` 补齐每一天、Decimal 相加出合计、`money_text` 格式化，并在查询结果里出现本月之外的日子时抛 `ValueError`（不悄悄丢数）。
  - `app/core/csv_export.py`（AIH-TASK-114）：`write_csv(columns, rows, text_columns) -> bytes`（BOM、RFC 4180、单元格只收 `str` / `None`、文本列防公式注入）与 `csv_response(filename, body)`（`Content-Disposition`、`Cache-Control: no-store`、`X-Accel-Buffering: no`、64 KiB 分段发送）。
  - `frontend/src/api/download.ts`（AIH-TASK-117）：带令牌的 blob 下载与失败信封解析。

## 2. 设计概要

```text
GET（客户访问令牌）→ month 形状校验（FastAPI，处理函数之前，422）→ require_customer（第一条语句：401 / 403）
→ month 必填与范围（422）→ tenant_id = CustomerPrincipal.tenant_id → 月份换算成 UTC 区间
→ 同一个只读会话里两条 SELECT（复用 #403 的 repository 函数）→ #403 的白名单视图 → 视图转成 CSV 行 → 200 text/csv（附件）
（无状态变化、无财务效果、无出站事件、无审计、不落盘、不占导出租约）
```

### 接口：`GET /api/v1/customer/usage-summary/export`

| 查询参数 | 规则 |
| --- | --- |
| `month` | **必填**，`YYYY-MM`，马来西亚日历月，`2020-01` 到 `2099-12`（含两端）。形状不对由 FastAPI 在处理函数之前 422（与 `GET /usage-summary` 同一个 `UsageMonthParam`）；缺失、月份不在 1..12、越界在鉴权之后、任何日期运算之前 422 `VALIDATION_ERROR`，只列字段名 `month`、不回显值。允许未来的月份（全为 0，与 JSON 接口一致） |

- **与 `GET /usage-summary` 的差别只有一处**：`month` 必填，不默认当月。文件用于对账，月份由客户明确选择；页面总是带上（§2「前端」）。这样文件名与内容恒为请求里的那个月，不受「现在」跨月的影响。
- 未知查询参数忽略；不接受任何租户、客户标识。
- 处理函数第一条语句 `require_customer(request)`；`month is None` 时抛 `InvalidCustomerUsageQuery("month")`；之后 `usage_month(month, now=utc_now())`（带了 `month` 时不读 `now`）。

### 查询

- 调用现有 `summarize_processed_events_for_customer(session, principal.tenant_id, occurred_from=period.from_utc, occurred_to=period.to_utc)`，**不另写 SQL**：口径（只算 `PROCESSED`、按 `occurred_at` 的马来西亚日归属、`COUNT(DISTINCT conversation_id)` 不计 NULL）、同一快照、执行预算与 `QueryTooBroad` 全部沿用 #403。
- `QueryTooBroad` → 与 JSON 接口相同：`_log_query_timeout(factory, tenant_id)` 记一条 `customer_usage_query_timeout`（只带 `request_id` 与客户 `public_id`），再抛 `CustomerUsageQueryTooBroad`（422 `QUERY_TOO_BROAD`）。

### 投影（白名单只有一份）

- 先用现有 `customer_usage_summary_view(period.first_day, summary)` 得到 #403 的 5 键视图，CSV 行**只从这个视图取值**，不读 repository 的返回值或任何 ORM 行。所以文件与 JSON 接口在同一份白名单、同一套补齐与合计逻辑上。
- `app/schemas/customer_usage.py` 新增：
  - `USAGE_SUMMARY_EXPORT_COLUMNS = ("date_myt", "request_count", "charged_amount", "conversation_count")`
  - `USAGE_SUMMARY_EXPORT_TEXT_COLUMNS = frozenset({"date_myt"})`
  - `USAGE_SUMMARY_EXPORT_TOTAL_LABEL = "TOTAL"`
  - `customer_usage_summary_export_cells(view: CustomerUsageSummaryView) -> list[list[str | None]]`：按 `view.days` 的顺序每天一行 `[day.date.isoformat(), str(day.request_count), day.charged_amount, None]`；最后一行 `["TOTAL", str(view.request_count), view.charged_amount, str(view.conversation_count)]`。整数用十进制字符串，金额是视图里已格式化好的 8 位小数字符串原样（不再经 Decimal 或 float）。
- 合计行的请求数与计费额直接取视图的 `request_count` / `charged_amount`（视图里它们就是各日之和，Decimal 相加，#403 §2），**不在 CSV 层再加一遍**：合计只有一处来源。

### 文件

| 列 | 文本列（防公式注入） | 每天的行 | 合计行 |
| --- | --- | --- | --- |
| `date_myt` | 是 | 马来西亚日历日 `YYYY-MM-DD` | `TOTAL` |
| `request_count` | 否 | 当天已计费的请求数（整数） | 当月请求数 = 各日之和 |
| `charged_amount` | 否 | 当天计费额（含税），恰好 8 位小数；没有事件的日子 `0.00000000` | 当月计费额 = 各日之和 |
| `conversation_count` | 否 | 空 | 当月不同的非空 `conversation_id` 个数（不是各日之和；没有对话标识的请求不计入） |

- 列名 `date_myt`（Malaysia Time）而不是 `date`：同一个客户手里还有 #435 的请求级导出，那边的时间列是 `occurred_at_utc`；日期列名写明是马来西亚日，免得客户按 UTC 日期把两份文件对不上。JSON 接口的键名不变（仍是 `date`）。
- 行数恒为 1（表头）+ 当月天数 + 1（合计），即 30–33 行，约 2 KB。
- 格式完全复用 `write_csv`（UTF-8 带 BOM、RFC 4180、`\r\n`、空值空字段）；`date_myt` 的值只可能是程序生成的日期或常量 `TOTAL`，不会以公式前缀开头，仍声明为文本列，与 #435 的「文本列一律防护」一致。
- 文件名：`usage-summary_<YYYY-MM>.csv`（例 `usage-summary_2026-10.csv`），月份取视图的 `month`（即校验过的请求月份），只含 ASCII 字母、数字、`-`、`_`、`.`，满足 `csv_response` 的文件名检查。
- 响应：`csv_response(filename, body)`：`Content-Type: text/csv; charset=utf-8`、`Content-Disposition: attachment; filename="…"`、`Cache-Control: no-store`、`X-Accel-Buffering: no`，由 `ExportCsvResponse` 分段发送（一个约 2 KB 的文件只有一段）。

### 不占导出租约（与 #435 的两个导出不同）

- #435 的进程级导出租约（`EXPORT_MAX_CONCURRENT = 1`）是为单个导出最多 20 000 行、峰值 ≤ 128 MiB 的内存上界设的（#435 §2「为什么要进程内的并发上限」，INV-1）。月度汇总的文件恒为 30–33 行、约 2 KB，查询结果至多 31 个按日分组的元组，内存与 `GET /usage-summary` 的 JSON 响应同一量级；数据库负载与 JSON 接口**完全相同**（同一个函数、同样的两条语句与 2 秒预算），而 JSON 接口本来就不限并发，首页每次打开都调用它。
- 所以本接口**不获取、不持有**导出租约：不会因为别的客户正在下载一个大文件而得到 503 `EXPORT_BUSY`，也不会挡住别人的导出。
- 因此也不需要 #435 v5 的「拿到租约后在处理函数里把 `Exception` 转成 `CustomerExportFailed`」：那是为了让 500 在租约内发出；这里没有租约，意外错误照常由 `handle_unexpected_error` 生成通用 500（与 `GET /usage-summary` 相同）。
- `docs/api.md`「客户导出」的「客户导出共用这一个名额」一句改为：用量导出与钱包流水导出共用这一个名额，月度汇总导出不占（写明理由：文件恒为一个月、约 2 KB）。

### 错误

401 `TOKEN_INVALID`（无 / 坏 / 过期令牌、非 `ACTIVE` 的门户账号）、403 `CUSTOMER_REQUIRED`（ADMIN 令牌、没有租户的 CUSTOMER）、422 `VALIDATION_ERROR`（`month` 缺失、形状、月份数或越界）、422 `QUERY_TOO_BROAD`、500 `INTERNAL_ERROR`、503 `DATABASE_NOT_CONFIGURED`。没有 `EXPORT_TOO_LARGE`（行数恒有界）、没有 `EXPORT_BUSY`（不占租约）、没有 404、没有 `ACCOUNT_READ_ONLY`。错误响应是 JSON 信封、不是 CSV：文件在内存里拼好才开始发送。

### 时间

- 月份区间与按日归属完全沿用 #403 §2「时间」：`[当月 1 日 00:00 − 8 小时, 次月 1 日 00:00 − 8 小时)`，`date = (occurred_at + 8 小时) 的日期`。
- 晚到或晚计费的事件计入它发生的那一天：同一个月的文件过几天再导出，数字可能变大（spec §111）。这不是对账单：对账单（Phase 7，T+1 cut-off 与上期调整）定稿后不变，本文件随时是「此刻已计费的」。`docs/api.md` 写明这一点。

### 金额

- 原样 8 位小数字符串（视图里的 `money_text` 结果），与 JSON 接口逐字节相同；不舍入、不加千分位、不带币种（恒为 MYR）。与 #435 相同的理由：导出用于对账，舍入后加总会差几分。

### 前端（第二个任务，不在闸门范围内，写在这里只为说明接口够用）

- 用量页（`frontend/src/features/portal/UsagePage.tsx`）在现有用量一块之外加一块「Monthly summary」导出区：一个月份输入（`<Input type="month">`，`min="2020-01"`，`max` = 当前马来西亚月；默认当前马来西亚月，按浏览器时间加 8 小时算，与用量页默认日期区间同一算法）与一个「Export monthly summary」按钮。说明文字写明：整月、全部项目、每天一行加合计，金额 8 位小数。
- 这一块**不受**用量页筛选的影响（汇总没有项目、供应商、模型筛选），与「Export CSV」（请求级导出）分开放、分开处理状态。
- 不支持 `type="month"` 的浏览器会退回文本框：月份不符合 `YYYY-MM`、不在 `2020-01` 到当前月之间时按钮不可用，并提示格式。
- 下载经 `download.ts` 的 `downloadFile`（带令牌、`responseType: "blob"`）；`frontend/src/api/customerUsage.ts` 加地址常量、拼地址与兜底文件名（与后端同一规则 `usage-summary_<YYYY-MM>.csv`）、下载函数。导出中按钮转为加载、不可重复点。失败文案：`QUERY_TOO_BROAD`（这个月的汇总暂时算不出，请稍后重试，或在上面按较短区间导出请求明细）；其余带 `request_id` 的通用失败文案。
- 页面不请求任何 `/api/v1/admin/` 路径。
- 浏览器验收加一个只读步骤 `check_portal_usage_summary_export`：排在 `check_portal_usage_export` 之后，停留在用量页，只核对导出区在、月份输入的值形如 `YYYY-MM`、按钮恰好一个且可用；**不点击**（验收不下载文件）、不改月份。

## 3. 不变量影响矩阵

| 不变量 | 是否触碰 | 可能怎样被破坏 | 设计控制 | 验证方式 |
| --- | --- | --- | --- | --- |
| INV-1 中心故障不中断 AI | 否（间接） | 只读，不在 AI 或摄取路径上；与 AI 模块共用数据库与进程：一个大客户的整月汇总可能长时间扫表；导出若不受租约约束会不会叠加内存 | 两条 SELECT 各 2 秒执行预算（与首页的 JSON 汇总完全相同）；文件恒为 30–33 行、约 2 KB，内存与 JSON 响应同一量级，所以不需要导出租约（§2「不占导出租约」） | 测试：MySQL 上测试专用 1 毫秒超时得 `QUERY_TOO_BROAD`；占住导出租约时本接口照常 200（证明不占也不受租约影响） |
| INV-2 事件不重复扣费 | 否 | 不写 | 无写路径 | 测试：调用前后各表行数与余额不变 |
| INV-3 支付不重复入账 | 否 | 同上 | 同上 | 同上 |
| INV-4 余额只经账本变动 | 否 | 同上 | 同上 | 同上 |
| INV-5 历史账本不可变 | 否 | 不读账本 | — | — |
| INV-6 事件保留版本引用 | 否 | 不读不导出版本引用 | — | — |
| INV-7 客户不可见成本毛利 | **是** | ① CSV 层直接读查询结果或另写查询，顺手带出成本列；② 内部计量租户的 `reference_customer_price` 被当成计费额 | ① 不另写 SQL，复用 #403 只读 `billable_cost` / `conversation_id` / `occurred_at` / `status` 的两条 SELECT；CSV 行只从 #403 的 5 键视图取值；列清单固定 4 列；② 只 SUM `billable_cost`（内部计量恒为 0） | 测试：成本、原币成本、汇率、参考价写成哨兵值，断言文件原始字节里没有；内部计量租户的合计计费额 `0.00000000`、请求数照常；表头逐字断言 |
| INV-8 租户不可互访 | **是** | 从请求里取租户；对话去重跨了租户 | 租户只来自 `CustomerPrincipal.tenant_id`；接口没有任何租户参数；查询沿用 #403 的 `WHERE tenant_id = :tenant_id` | 测试：A、B 同月都有事件且共用一个 `conversation_id` 值；A 带 `?customer_id=<B>&tenant_id=<B 的内部 id>` 导出只得到 A 的数字，A 的对话数不因 B 变化；B 的任何值不出现在文件里 |
| INV-9 对话内容不入库 | 否 | 只计数 `conversation_id`，不导出它 | — | 测试：文件里不出现任何 `conversation_id` 值 |
| INV-10 金额用 Decimal | **是** | CSV 层把金额再经 float 相加或舍入；合计在 CSV 层另算一遍与 JSON 不一致 | 金额单元格是视图里 `money_text` 的结果原样；合计直接取视图的合计（视图内 Decimal 相加），CSV 层不做任何算术 | 测试：三条 `0.00000001` 合计 `0.00000003`；同日 `999999999999.99999999` 与 `0.00000001` 得 `1000000000000.00000000`；每一行与 JSON 接口同月的同名值逐字相等 |
| INV-11 event_id 至多一次财务效果 | 否 | 不写 | — | — |
| INV-12 定稿对账单不可变 | 否 | 对账单还不存在；本文件不是对账单，数字会随晚到事件变化 | `docs/api.md` 写明不是对账单 | — |
| INV-13 状态与事件原子提交 | 否 | 没有状态变化与出站事件；两条 SELECT 在同一快照里（#403） | 复用 #403 的同一会话、不提交 | #403 的同一快照用例已覆盖 repository 函数 |
| INV-14 队列丢失不毁持久工作 | 否 | 不用 Redis / Celery、不落盘 | — | — |

## 4. 状态与并发

- **没有状态变化**，没有跃迁表；没有幂等键：同一请求重放得到当时的数据（可能多出晚到计费的事件），没有副作用。
- **与计费并发**：两条 SELECT 在同一个 `REPEATABLE READ` 事务里看到同一个快照（#403 §4），所以文件里各日的数字、合计与对话数来自同一组 `PROCESSED` 行。
- **不加锁**：不阻塞计费与摄取，也不被它们阻塞。
- **与其他导出并发**：不获取导出租约，与用量导出、钱包导出互不阻塞（§2「不占导出租约」）；多个月度汇总可以同时进行，与多个首页请求同时进行的情况相同。
- 重试：由客户再点一次发起，服务端不重试。

## 5. 失败与恢复矩阵

| 失败点 | 对外结果 | 数据最终状态 | 是否可重试 | 恢复来源 | 告警 |
| --- | --- | --- | --- | --- | --- |
| `month` 缺失、形状错、月份数不在 1..12 或越界 | 422 `VALIDATION_ERROR`，只列 `month`、不回显值 | 不变 | 修正后重试 | — | 无 |
| 未登录 / 令牌过期 / 门户账号非 `ACTIVE` | 401 `TOKEN_INVALID` | 不变 | 刷新后重试（非 `ACTIVE` 不行） | 刷新令牌 / 管理员处理账号 | 无 |
| ADMIN、无租户 CUSTOMER | 403 `CUSTOMER_REQUIRED` | 不变 | 否 | — | 无 |
| 租户 `DISABLED` / `CLOSED` / 只读 | 200 照常（导出是读） | 不变 | — | — | 无 |
| 任一条 SELECT 超过 2 秒 | 422 `QUERY_TOO_BROAD`，不返回任何文件内容；页面提示稍后重试或按较短区间导出请求明细 | 不变（语句被中止） | 是 | — | `customer_usage_query_timeout` 日志；超时常见时按 #403 另开闸门建聚合表 |
| 数据库未配置 | 503 `DATABASE_NOT_CONFIGURED`（`require_session_factory`，早于任何查询） | 不变 | 是 | 运维 | 现有 |
| 查询时数据库不可用 / 连接中断 / 其他查询错误 | 500 `INTERNAL_ERROR`（通用 500，带 `request_id`，与 `GET /usage-summary` 相同） | 不变 | 是 | 数据库恢复 | 现有 `Unhandled exception` 错误日志与 readyz 巡检 |
| 查询结果里出现本月之外的日子（归日与区间不一致的缺陷） | 500 `INTERNAL_ERROR`（`customer_usage_summary_view` 抛 `ValueError`，不出部分文件、不悄悄丢数） | 不变 | 否（是缺陷） | 修代码 | 错误日志 |
| 别的导出正占着导出租约 | 不影响：照常 200 | 不变 | — | — | 无 |
| 客户端下载中断 | 浏览器侧失败 | 不变（服务器不留文件） | 是，重新导出 | — | 无 |
| Redis/Celery 丢失 | 无影响 | 不变 | — | — | 无 |
| 外部服务超时 | 不适用 | — | — | — | — |
| 重复请求 | 期间没有新计费事件时文件逐字节相同 | 不变 | 是 | — | 无 |

## 6. 数据与安全边界

- **租户过滤**：两条 SELECT 都 `WHERE tenant_id = :tenant_id`（#403 的 repository 函数），`tenant_id` 只来自 `require_customer`。
- **鉴权主体**：只有 CUSTOMER（`ACTIVE`、有租户）。处理函数第一条语句显式调用 `require_customer`；新路由进 `tests/backend/test_customer_api.py` 的路由枚举与守卫用例。
- **文件里禁止出现的**：成本（估算与对账）、原币成本、汇率、参考价、毛利、加价、token 合计、价格版本与定价规则引用、`conversation_id` 值、项目 / 供应商 / 模型拆分、任何内部 id。
- **日志、审计、异常**：沿用请求日志；不记录 `month` 的值、不记录金额与计数、不记录文件内容；超时日志只带 `request_id` 与客户 id（现有）。读操作不写审计。
- **留存（spec §112）**：与 #435 相同：服务器不保存文件（内存里生成、请求结束即释放）；`X-Accel-Buffering: no` 让两层代理不缓冲（`billing.conf` 的 `proxy_pass_header` 已由 AIH-TASK-114 交付，对本接口同样生效，因为路径在 `location /api/` 之下）；`Cache-Control: no-store`。
- **公式注入**：唯一的文本列 `date_myt` 的值由程序生成，没有外部输入进入文件的文本单元格；仍按 #435 的规则防护。
- **密钥**：不涉及。**prompt / response**：不涉及。

## 7. 测试证据计划

后端（`tests/backend/test_customer_usage_summary_export_api.py` 新文件，SQLite 与 MySQL 参数化，MySQL 用 `BILLING_TEST_DATABASE_URL`、未设置时 skip、CI 必跑；客户登录夹具照 `tests/backend/test_customer_usage_summary_api.py` 的写法；SQL 文本断言不写死方言字面量；示例 uuid 全零）：

| 风险/需求 | 测试层级 | 场景 | 预期结果 |
| --- | --- | --- | --- |
| 正常路径 | integration | `month=2026-10`：10 月 1 日 3 条、10 月 15 日 2 条 `PROCESSED`，其中 3 条有两个不同的 `conversation_id`、2 条没有 | 200、`text/csv; charset=utf-8`、文件名 `usage-summary_2026-10.csv`、`Cache-Control: no-store`、`X-Accel-Buffering: no`；以 BOM 开头；表头恰好 `date_myt,request_count,charged_amount,conversation_count`；31 个日行按日期升序、1 日与 15 日有值、其余 `0` 与 `0.00000000`、`conversation_count` 列全空；末行 `TOTAL,5,<合计>,2` |
| 与 JSON 接口逐字一致 | integration | 同一批事件，同一个月分别调 `GET /usage-summary?month=` 与导出 | 每个日行的 `date_myt` / `request_count` / `charged_amount` 与 JSON `days` 同序同名值逐字相等；合计行与 JSON 的 `request_count` / `charged_amount` / `conversation_count` 逐字相等 |
| 天数 | integration | `2026-02`、`2028-02`（闰年）、`2026-04`、`2026-12` | 日行分别 28、29、30、31 个，总行数 = 天数 + 2 |
| 月份边界 | integration | 事件在 UTC `2026-09-30 15:59:59.999999`、`16:00:00`、`2026-10-31 15:59:59.999999`、`16:00:00` | `month=2026-10` 只计中间两条，分别在 `2026-10-01` 与 `2026-10-31` 行 |
| 只算已计费 | integration | 同月另有 `RECEIVED`、`PRICING_ERROR`、`MODEL_UNKNOWN`、`FAILED_FINAL` 事件 | 都不计入任何一行 |
| 对话计数 | integration | 同一 `conversation_id` 在 10 月 3 天各有事件 | 合计行 `conversation_count = 1`；日行该列都为空 |
| 参数规则 | integration | 不带 `month`；`month` 为 `2026-13`、`2026-1`、`2019-12`、`2100-01`、`abc` | 422 `VALIDATION_ERROR`，只列 `month`、文案不回显值，响应是 JSON 信封不是 CSV；`2020-01`、`2099-12` 为 200；未来月份 200、全为 0 |
| 空月 | integration | 该月没有任何事件 | 200，日行全为 `0` / `0.00000000`，合计 `TOTAL,0,0.00000000,0` |
| 金额精度 | integration（SQLite + MySQL） | 三条 `0.00000001`；同日 `999999999999.99999999` 与 `0.00000001` | 合计 `0.00000003`；该日与合计 `1000000000000.00000000`，原样出现在文件里 |
| 扫描上限 | integration（MySQL） | 30 000 条当月 `PROCESSED` 事件，测试专用 1 毫秒超时（monkeypatch 传给 repository 的 `timeout_ms`，照 #403 用例的写法） | 422 `QUERY_TOO_BROAD`，JSON 信封；一条 `customer_usage_query_timeout` 日志，不带 `month` 的值 |
| 不占导出租约 | integration | 测试里先占住导出租约（`try_acquire_export_lease`），再调本接口；调完后检查租约 | 200 正常文件；租约仍由测试持有、未被释放或替换；测试释放后照常 |
| 内部计量租户 | integration | `INTERNAL_METERED_ONLY` 的 `PROCESSED` 事件，`reference_customer_price` 为哨兵值 | 合计 `charged_amount = 0.00000000`，请求数照常；文件里没有哨兵值 |
| 无成本毛利 | integration | 成本、原币成本、汇率、参考价写成哨兵值 | 文件原始字节里没有任何哨兵值 |
| 租户越权 | integration | 客户 A、B 同月有事件且共用 `conversation_id` 值；A 带 `?customer_id=<B>&tenant_id=<B 的内部 id>` | 只有 A 的数字；B 的数字与任何 `conversation_id` 值都不在文件里 |
| 鉴权 | integration | 无令牌、坏令牌、ADMIN 令牌、门户账号停用后的旧令牌 | 401 / 401 / 403 `CUSTOMER_REQUIRED` / 401，且不发汇总查询 |
| 只读契约 | integration | 租户四种 `account_status` | 都 200 |
| 路由守卫 | integration | `tests/backend/test_customer_api.py` 的客户路由枚举与守卫用例 | 新路由被枚举到；匿名 401、ADMIN 403 |
| 无写入 | integration | 每个用例前后 | `usage_events`、`wallet_transactions`、`wallets`、`audit_logs`、`domain_outbox` 不变 |
| 数据库断连 | integration | monkeypatch `summarize_processed_events_for_customer` 抛 `sqlalchemy.exc.OperationalError` | 500 `INTERNAL_ERROR`、带 `request_id`、JSON 信封，与 `GET /usage-summary` 同一形状；之后正常请求 200 |
| 单元格转换 | unit | `customer_usage_summary_export_cells`：28 天与 31 天的视图 | 行数 = 天数 + 1；每个单元格是 `str` 或 `None`；日行第 4 格为 `None`；末行第 1 格为 `TOTAL`、其余取自视图的合计（构造一个合计与各日之和不同的视图，断言末行取的是视图的合计字段，即 CSV 层不另算） |
| 常量 | unit | 列清单、文本列、合计标签 | 恰好 §2 的 4 列、文本列只有 `date_myt`、标签为 `TOTAL` |
| 幂等重放 / 事务中途失败 / Redis 丢失 | — | 只读、不入队 | 不适用（重复请求在无新事件时逐字节相同，由「与 JSON 接口逐字一致」间接覆盖） |

前端（Vitest + Testing Library）：导出区默认月份 = 当前马来西亚月（把时钟固定在 UTC `2026-10-31 15:59:59` 与 `16:00:00`，分别得 `2026-10` 与 `2026-11`）；`max` 为当前马来西亚月、`min` 为 `2020-01`；月份为空、格式错、早于 `2020-01` 或晚于当前月时按钮不可用；点击后请求的地址恰好是 `/api/v1/customer/usage-summary/export?month=<月份>`，不带用量页的任何筛选参数；兜底文件名；导出中按钮加载且不可重复点；`QUERY_TOO_BROAD` 专门文案与通用失败（带 `request_id`）；失败不影响用量列表与请求级「Export CSV」；不请求 `/api/v1/admin/` 路径。

## 8. 迁移与上线

- **数据迁移步骤**：无。不改表、不加索引。
- **锁表与性能影响**：无 DDL。每次导出与一次首页 JSON 汇总的数据库代价相同（两条范围扫描，各 2 秒上限）。导出远比首页打开少。
- **兼容窗口**：新增路由，不改任何既有接口的行为与响应；`docs/api.md` 只改「客户导出」里租约那一句的适用范围（用量与钱包两个导出仍然共用名额，行为不变）。
- **部署顺序与拆分**（批准后登记为两个任务，每个合并后都能单独通过 CI 并部署）：
  1. 后端：`app/schemas/customer_usage.py` 加列清单、文本列、合计标签与 `customer_usage_summary_export_cells`；`app/api/customer.py` 一个路由；新测试文件 `tests/backend/test_customer_usage_summary_export_api.py`；`tests/backend/test_customer_api.py` 的路由枚举加一行；`docs/api.md`「客户导出」加本接口一节并改租约那一句、删掉「月度用量汇总导出不在这里」。依赖 AIH-TASK-107（汇总函数与视图）与 AIH-TASK-114（`write_csv` / `csv_response`），两者都已交付。
  2. 前端：`frontend/src/api/customerUsage.ts`（地址、文件名、下载函数）、`UsagePage.tsx` 的月度汇总导出区、`en.json` 文案、`UsagePage.test.tsx` 与 `customerUsage.test.ts`、验收脚本一个只读步骤 `check_portal_usage_summary_export`。依赖 1。
- **回滚或前滚方式**：revert 即可，没有数据与结构变化。
- **部分部署时的行为**：只部署 1 → 接口可用、页面没有入口。
- **监控指标和告警负责人**：不新增；沿用 `customer_usage_query_timeout` 日志。负责人 Kelvin。

## 9. 方案取舍

| 方案 | 优点 | 风险 | 未采用原因 |
| --- | --- | --- | --- |
| **采用**：复用 #403 的查询与视图，视图转成 CSV 行，合计行取视图的合计；`month` 必填；不占导出租约 | 与首页 JSON 逐字一致、白名单只有一份；没有新 SQL；不会被大导出挡住 | 首页 JSON 与文件的口径绑在一起：以后改 #403 的口径，文件跟着变（这正是要的） | — |
| 备选 A：另写一条 SQL 直接出 CSV 需要的行（含 `WITH ROLLUP` 合计） | 一条语句 | 两份口径、两份白名单；SQLite 不支持 `WITH ROLLUP`（#403 §9 备选 C） | 复用已批准、已测的函数更安全 |
| 备选 B：合计放在单独的元数据行（文件开头写「Month: 2026-10, Total: …」） | 表头上方一眼可见 | 表格软件里第一行不再是表头，排序、筛选、透视都要先删行；不是 RFC 4180 意义上的规整表格 | Kelvin 2026-10-27 选末尾一行 `TOTAL` |
| 备选 C：每天也给对话数 | 每天信息更多 | 多一条按日 `COUNT(DISTINCT)` 查询；各日相加大于合计，客户在表格里 SUM 会得到错数 | Kelvin 2026-10-27 选只在合计行给 |
| 备选 D：占导出租约，与 #435 的两个导出一致 | 「所有导出共用一个名额」一句话说清 | 一个约 2 KB 的文件会因为别人慢慢下载 8 MB 而 503 最长约一分钟；要照搬 `CustomerExportFailed` 的整套错误处理，换不来任何内存或数据库上的收益 | 租约的理由是 20 000 行的内存上界，对恒为一个月的文件不成立 |
| 备选 E：`month` 可省略、默认当月（与 JSON 接口一致） | 少一个必填 | 客户在月末最后几分钟打开页面、跨月后才点，得到的是新月份的文件，与页面显示的月份不一致 | 页面总是带上月份，接口必填更明确 |
| 备选 F：支持日期区间或多月（每月一行） | 一次拿多个月 | 多月就要多次或更大的扫描，超出 #403 一次一个月的预算假设；跨月的对话去重语义要另定 | 一个文件一个月，与「后续计划」的写法和首页口径一致；多月可以多次导出 |
| 备选 G：入口放首页本月用量卡 | 离数字最近 | 首页卡只显示当月；对账最常用的是月初导上个月 | Kelvin 2026-10-27 选用量页加月份选择 |
| 备选 H：日期列名用 `date`（与 JSON 键相同） | 与 JSON 一致 | 与请求级导出的 `occurred_at_utc` 放在一起时，客户容易把它当 UTC 日期去对 | 列名写明马来西亚日；JSON 键不变 |

## 10. 未决问题与假设

- **未决问题**：无阻断项。
- **已拍板的取舍**（Kelvin 2026-10-27）：合计放末尾一行 `TOTAL`；对话数只在合计行；入口在用量页并带月份选择。
- **尚未验证的假设**：
  1. AIH-TASK-107 交付的 `summarize_processed_events_for_customer` 与 `customer_usage_summary_view`、AIH-TASK-114 交付的 `write_csv` / `csv_response` 形状如 §1「现有行为」所述（2026-10-27 读过 `main` 上的代码）。若形状不同，后端任务停下说明，不自行补。
  2. 客户当月事件数在 2 秒预算内可汇总完：与 #403 §10 的假设 2 相同，生产上现在只有夹具与内部租户。错了的后果是 422 `QUERY_TOO_BROAD`，不是错数据。
  3. `billing.conf` 的 `proxy_pass_header X-Accel-Buffering;` 在 `location /api/` 之下（AIH-TASK-114 交付），本接口路径在其下，所以同样不被代理缓冲；即使被缓冲，约 2 KB 也在 nginx 的内存缓冲之内，不会写临时文件。
- **需要谁拍板**：无新增。
- **如果假设错误，具体后果**：最坏是导出失败（422 / 500），不会给出与首页不一致或缺行的文件（文件在内存里拼好才发送，行只来自同一个视图）。

## 11. 审查与版本绑定

审查方要回答的五个问题常驻在 [scripts/review_checklist.md](../../scripts/review_checklist.md) 的「设计审查」一节，此处不重复。

**版本绑定规则**（规则的出处是 [WORKFLOW §3](../../docs/WORKFLOW.md)；这里保留一份是因为审查材料里只有本 Issue 正文，两处改一起改）：

- Codex 的批准必须写成 `APPROVED: design v<N>`，`<N>` 取本 Issue 顶部的「设计版本」
- **设计版本一变，之前的 APPROVE 自动作废**，必须重新过闸门
- 什么算「实质修改」：改了契约、表结构、事务边界、状态机、失败语义、不变量控制。改错别字不算
- 实质修改时：把顶部版本 +1，在下方追加一条变更说明，状态退回 `READY_FOR_REVIEW`

### 设计闸门判定

同时满足才算通过：

- [x] 需求、非目标和验收标准明确
- [x] 关键契约与事务边界明确
- [x] 触碰的不变量都有控制措施
- [x] 失败路径都有确定的最终状态
- [x] 高风险控制都有测试场景
- [x] 没有未解决的阻断假设
- [ ] 审批绑定到明确的设计版本

## 12. 版本变更记录

| 版本 | 日期 | 改了什么 | 触发原因 |
| --- | --- | --- | --- |
| v1 | 2026-10-27 | 初稿 | — |

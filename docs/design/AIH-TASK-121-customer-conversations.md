# AIH-TASK-121 / 122 / 123 / 124 设计：客户会话摘要（已批准 v2）

> **来源**：设计闸门 Issue #460。本文件是 `APPROVED: design v2` 那一版正文的**逐字副本**（v1 由 Codex 审查、被要求修改，见 §12；v2 在 Codex 额度用完期间由独立、只读的 Claude Code 会话审查通过；送审时状态行为 `READY_FOR_REVIEW`，批准后 Issue 上只把状态行改成了 `APPROVED: design v2`）。
> 放进仓库，是因为 OpenClaw Worker 在沙箱里不联网、读不到 GitHub Issue。
> **实现以本文件为准**；与 Issue 不一致时，以 Issue #460 上被批准的 v2 为准。设计要改，就回到 Issue 升版本、重新过闸门，不要直接改本文件。
> **前置**：设计闸门 #398（[AIH-TASK-103-customer-usage.md](AIH-TASK-103-customer-usage.md)）的 `AIH-TASK-103..106`；设计闸门 #435（[AIH-TASK-113-customer-export.md](AIH-TASK-113-customer-export.md)）的 `AIH-TASK-114`（CSV 模块与导出租约）与 `AIH-TASK-117`（下载辅助函数）；设计闸门 #457（[AIH-TASK-119-customer-usage-summary-export.md](AIH-TASK-119-customer-usage-summary-export.md)）的 `AIH-TASK-119`、`AIH-TASK-120`（改同几个文件，排在它们之后）。
> §8 的四个实现任务登记为 `AIH-TASK-121`（会话列表接口）→ `AIH-TASK-122`（会话摘要导出接口）→ `AIH-TASK-123`（门户会话页）→ `AIH-TASK-124`（会话页导出按钮），按这个顺序依赖。

---

## 0. 准入判定

| 任务涉及 | 要填的范围 |
| --- | --- |
| 钱包 / 账本 / 定价 / 汇率 / 支付 / 幂等 / 状态机 | **全部章节** |
| 用量摄取 / 认证与会话（集成侧与平台侧）/ Webhook | §1–§7（**§6 不可省**） |
| 前端 / 文档 / CI / 脚本 | 不走本闸门 |
| 既不碰钱、也不碰用量摄取 / 认证与会话 / Webhook 的后端改动 | 不走本闸门 |

本任务档位：`全部`。只读，不写任何表、不改表结构、不加索引，但按会话把客户的计费额（`billable_cost`）加总后展示与导出（INV-7、INV-10），加总结果要与同条件下的请求级明细逐分对得上；`usage_events` 同一行上就有成本、汇率、参考价。只读带来的「不触碰」在 §3 逐条说明理由。

---

状态：`待审`（`READY_FOR_REVIEW`）
负责人：Claude
**设计版本**：`v2`
对应需求：spec §68「Customer Usage Page」（Conversation-level table：Conversation ID、Started At、Last Activity、Requests、Input Tokens、Output Tokens、Customer Charge；「Click conversation to view request-level details」）、§10（`conversation_id` 由集成方生成，Central Billing 只存储与聚合、不判定会话边界）、§73（导出至少包括 Conversation Summary；「Customer export MUST exclude internal costs」）、§90（客户接口，租户只取自会话）、§108（大列表游标分页）、§109（展示时区 `Asia/Kuala_Lumpur`）、§111（聚合由请求数据派生、可重算，不替代请求数据）、§112（导出不在服务端保存）、§128 Phase 5「Conversation summary」「customer usage exports」、`REQ-PRIV-001`（INV-8）、`REQ-PRIV-002`（INV-7）
前置设计：#398 v4（客户用量明细：`GET /api/v1/customer/usage-events`、马来西亚日历日区间与 92 天、2 秒执行预算与 `QUERY_TOO_BROAD`、索引 `ix_usage_events_tenant_occurred` / `ix_usage_events_project_occurred`，AIH-TASK-103..106）、#403 v1（读时实时汇总，不建聚合表）、#435 v10（客户 CSV 导出：`write_csv` / `csv_response`、导出租约、20 000 行上限、128 MiB 内存上界，AIH-TASK-113..118）、#457 v1（月度汇总导出，AIH-TASK-119..120，与本闸门改同几个文件）
目标 PR：待开（批准后按 §8 拆四个任务登记；`docs/TODO.md`「后续计划」里的「Phase 5 会话摘要」与「客户端导出的其余部分」中的「会话摘要导出」即本闸门）

## 1. 目标与边界

- **要解决的问题**：客户门户已有请求级明细（#398）、本月用量与对话数（#403），但看不到「哪一次会话花了多少」。spec §68 要求会话级表格并可点进看请求，§73 要求会话摘要导出，§128 把会话摘要列为 Phase 5 的验收项。#398 §1 把它明确延后到这里。
- **可观察的完成标准**：
  1. `GET /api/v1/customer/conversations?from_date=&to_date=[&project_id=][&page_size=][&cursor=]` 返回登录客户自己在该马来西亚日历日区间内的会话：每个（项目, `conversation_id`）一行，只计 `PROCESSED`、`conversation_id` 非空的事件；每行恰好 §2 的 9 个键。
  2. 每一行的 `request_count`、`input_tokens`、`output_tokens`、`charged_amount` 与同一时刻 `GET /api/v1/customer/usage-events`（同一区间、`project_id` 取该行项目、`conversation_id` 取该行会话）返回的全部行的对应值之和**逐字相等**；`first_occurred_at` / `last_occurred_at` 等于那些行 `occurred_at` 的最小 / 最大值。
  3. 按最后活动时间倒序，不透明游标往旧翻页；翻页全程同一会话不出现两次。
  4. 查询沿用 2 秒执行预算，超时 422 `QUERY_TOO_BROAD`。
  5. `GET /api/v1/customer/conversations/export?from_date=&to_date=[&project_id=]` 返回同样口径的 CSV 附件，至多 20 000 行，与 #435 的用量导出共用导出租约、行数上限与错误语义。
  6. 门户新增会话页 `/portal/conversations`（导航加一项）：日期区间与项目筛选、往旧翻页、点一行跳到用量页并带上该会话、项目与同一日期区间；有导出按钮。
- **明确不做什么**（前三条 Kelvin 2026-10-27 逐项确认）：
  - **读时实时算，不建聚合表**（spec §74 的 `conversation_usage_summaries` 不建）：与 #403 相同，查询有 2 秒预算兜底；`customer_usage_query_timeout` 日志显示会话查询常超时，再另开闸门建可重算的聚合表（spec §111）。
  - **只算所选日期区间内的请求**：跨区间边界的会话只计区间内的部分；`first_occurred_at` 是「区间内的第一条请求」，不是会话真正的开始。这样点进去看到的请求与这一行的合计逐条对得上（完成标准 2）。
  - **没有 `conversation_id` 的请求不进会话表**（AI 模块代报的事件都没有）：与首页「对话数」不计 NULL 同一口径（#403）；页面写一句说明，这类请求在用量页看。
  - 不做会话状态、关闭原因、渠道：会话生命周期归集成方（spec §10、§104），平台只有 `conversation_id`。
  - 不做按供应商、模型、`conversation_id` 筛选会话表：按供应商筛会把一次会话的合计切成「这个供应商的那部分」，与「会话」的含义冲突；要找某一个会话，在用量页按 `conversation_id` 筛请求（#398 已有）。
  - **不新增 `GET /api/v1/customer/conversations/{id}`**（spec §90 列了它）：同一个 `conversation_id` 可以出现在同一客户的两个项目里，单凭 id 定位不到一次会话；「点进会话看请求」由现有 `GET /usage-events?conversation_id=&project_id=&from_date=&to_date=` 完成，白名单、越权测试与分页都已有，不再造第二个请求级视图。§9 备选 C。
  - 不汇总缓存 token 与 `quantity`：spec §68 只列输入 / 输出 token；`QUANTITY` 形态的事件（例如转写按秒）计入请求数与计费额，token 记 0。
  - 只做 CSV（`.xlsx` 仍在「后续计划」）；不做总数（`total`）、不做排序选项。
  - 不写任何表、不改表结构、不加索引；不写审计。
- **现有行为与问题证据**（2026-10-27 读 `main` 上的代码）：
  - `usage_events.conversation_id` 是 `VARCHAR(128)`、可空，MySQL 上排序规则 `utf8mb4_0900_bin`（`app/models/usage.py` 的 `TEXT_COLLATION`）：分组与比较按字节，`conv_A` 与 `conv_a` 是两个会话。格式由摄取限定为 `^[A-Za-z0-9._:-]{1,128}$`（`REQUEST_ID_PATTERN`）。
  - `conversation_id` 只在同一个集成方（项目）内由集成方生成，平台不保证跨项目唯一（spec §10）。
  - 已有索引：`ix_usage_events_tenant_occurred (tenant_id, occurred_at, public_id)`、`ix_usage_events_project_occurred (project_id, occurred_at, public_id)`（迁移 0026）、`ix_usage_events_conversation_id`。
  - `app/repositories/usage_events.py`：`run_bounded`（MySQL 错误 3024 → `QueryTooBroad`）、`CUSTOMER_USAGE_QUERY_TIMEOUT_MS = 2000`、`customer_usage_statement` 的 `prefix_with("/*+ MAX_EXECUTION_TIME(...) */", dialect="mysql")` 写法；`get_project_for_tenant`（`app/repositories/tenancy.py`）。
  - `app/schemas/customer_usage.py`：`usage_period(from_date, to_date, *, has_event_id)`（2020-01-01..2099-12-31、92 天、必填）、`InvalidCustomerUsageQuery`、`CustomerUsageQueryTooBroad`、`USAGE_DATE_PATTERN`；`money_text`（`app/schemas/customers.py`）。
  - `app/api/customer.py`：`customer_usage_events_export` 的租约与错误处理写法（`try_acquire_export_lease`、`EXPORT_LEASE_SCOPE_KEY`、`CustomerExportFailed`、`ExportTooLarge`）、`_log_query_timeout`。
  - `app/core/csv_export.py`：`EXPORT_MAX_ROWS = 20_000`、`write_csv`、`csv_response`；`tests/backend/test_csv_export.py` 用 `tracemalloc` 钉住 20 000 行最长形态用量行的峰值 ≤ 128 MiB。
  - 前端：`frontend/src/features/portal/UsagePage.tsx` 的筛选只放在组件状态里，查询串只认 `event_id`；`frontend/src/components/MoneyText.tsx` 的 `formatAmountRounded`；`frontend/src/api/download.ts` 的 `downloadFile`。

## 2. 设计概要

```text
GET（客户访问令牌）→ 查询参数形状与范围（FastAPI，处理函数之前，422）→ require_customer（第一条语句：401 / 403）
→ tenant_id = CustomerPrincipal.tenant_id → 日期区间（usage_period，必填、92 天，422）→ 游标解码（422）
→ [项目在本租户内查出内部 id，查不到 → 空页 / 只有表头的文件，不发聚合查询]
→ 一条聚合 SELECT（2 秒预算）→ 白名单视图 → 200 JSON / CSV（导出另有租约与 20 000 行上限）
（无状态变化、无财务效果、无出站事件、无审计、不落盘）
```

### 接口一：`GET /api/v1/customer/conversations`

| 查询参数 | 规则 |
| --- | --- |
| `from_date` / `to_date` | **必填**，规则与 `GET /usage-events` 完全相同（`usage_period(..., has_event_id=False)`）：`YYYY-MM-DD` 马来西亚日历日、含两端、各自在 2020-01-01..2099-12-31、`to_date` 不早于 `from_date`、最多 92 天 |
| `project_id` | 可选，项目 `public_id`；不是本客户的项目时空页（与 `GET /usage-events` 同一语义，不构成存在性探测） |
| `page_size` | 1–100，默认 20 |
| `cursor` | 可选，上一页的 `next_cursor` 原样传回；解码失败、内容不合规 422 |

未知参数忽略；不接受任何租户、客户标识。类型与范围错误在鉴权之前 422；日期必填与先后、92 天、游标在鉴权之后 422；都是 `VALIDATION_ERROR`，只列字段名、不回显值。

成功 200，`data`：

```json
{
  "items": [
    {
      "conversation_id": "conv_0001",
      "project_id": "00000000-0000-4000-8000-000000000001",
      "project_name": "Customer service bot",
      "first_occurred_at": "2026-10-18T08:30:00.123456",
      "last_occurred_at": "2026-10-18T08:41:12.000001",
      "request_count": 7,
      "input_tokens": 8400,
      "output_tokens": 2380,
      "charged_amount": "0.28840000"
    }
  ],
  "page_size": 20,
  "next_cursor": null
}
```

| 字段 | 说明 |
| --- | --- |
| `conversation_id` | 集成方上报的原值（标识，不是内容，INV-9） |
| `project_id` / `project_name` | 该会话所属项目的 `public_id` 与 `name`。会话的键是（项目, `conversation_id`）：同一个 `conversation_id` 出现在两个项目里是两行 |
| `first_occurred_at` / `last_occurred_at` | 区间内该会话已计费请求 `occurred_at` 的最小 / 最大值，UTC 不带时区、保留微秒（与 `GET /usage-events` 的 `occurred_at` 同一格式）；前端 +8 小时显示 |
| `request_count` | 区间内该会话已计费的请求数 |
| `input_tokens` / `output_tokens` | 这些请求 `input_tokens` / `output_tokens` 之和，整数；`QUANTITY` 形态的请求（这两列为 NULL）不计入，全是 `QUANTITY` 形态时为 0 |
| `charged_amount` | 这些请求 `billable_cost` 之和（含税），8 位小数字符串（`money_text`），不舍入；内部计量租户恒为 `"0.00000000"` |
| `next_cursor` | 还有更旧的行时为不透明字符串，否则 `null`；多取的第 `page_size + 1` 行只用来判断有没有下一页 |

**刻意不返回**：成本（估算与对账）、原币成本、汇率、参考价、毛利、计费模式、状态、版本与规则引用、供应商 / 模型拆分、缓存 token 与 `quantity` 合计、任何内部 id（包括项目内部 id）、总行数。

### 查询（一条 SELECT）

```sql
SELECT /*+ MAX_EXECUTION_TIME(2000) */
       g.conversation_id, p.public_id AS project_public_id, p.name AS project_name,
       g.first_occurred_at, g.last_occurred_at, g.request_count,
       g.input_tokens, g.output_tokens, g.charged_amount
FROM (
  SELECT ue.project_id, ue.conversation_id,
         MIN(ue.occurred_at) AS first_occurred_at, MAX(ue.occurred_at) AS last_occurred_at,
         COUNT(*) AS request_count,
         COALESCE(SUM(ue.input_tokens), 0) AS input_tokens,
         COALESCE(SUM(ue.output_tokens), 0) AS output_tokens,
         COALESCE(SUM(ue.billable_cost), 0) AS charged_amount
  FROM usage_events ue
  WHERE ue.tenant_id = :tenant_id
    AND ue.status = 'PROCESSED'
    AND ue.occurred_at >= :from_utc AND ue.occurred_at < :to_utc
    AND ue.conversation_id IS NOT NULL
    [AND ue.project_id = :project_id]          -- 先在本租户内由项目 public_id 查出
  GROUP BY ue.project_id, ue.conversation_id
) g
JOIN projects p ON p.id = g.project_id
[WHERE g.last_occurred_at < :c_at
    OR (g.last_occurred_at = :c_at AND (p.public_id < :c_project
        OR (p.public_id = :c_project AND g.conversation_id < :c_conversation)))]
ORDER BY g.last_occurred_at DESC, p.public_id DESC, g.conversation_id DESC
LIMIT :limit
```

- 新函数放在 `app/repositories/usage_events.py`：`customer_conversation_statement(tenant_id, *, occurred_from, occurred_to, limit, project_row_id=None, before=None, timeout_ms=CUSTOMER_USAGE_QUERY_TIMEOUT_MS)` 构造语句，`list_conversations_for_customer(session, tenant_id, *, ...)` 先查项目（`get_project_for_tenant`，查不到返回空列表、不发聚合查询）、再经 `run_bounded` 执行，返回新的 `CustomerConversationRow`（恰好上面 9 列，`before` 是 `(last_occurred_at, project_public_id, conversation_id)`）。
- 内层只读 `project_id`、`conversation_id`、`occurred_at`、`status`、`tenant_id`、`input_tokens`、`output_tokens`、`billable_cost`，不读任何成本列（INV-7）；不复用管理端的 `EventListRow`。
- 分组在派生表里只按 `usage_events` 的两列，项目名与 `public_id` 在外层按主键 JOIN：不依赖 MySQL 对 `ONLY_FULL_GROUP_BY` 的函数依赖推断，SQLite 与 MySQL 同一写法。
- 执行时间提示只在最外层 SELECT（MySQL 只认顶层语句的这个提示），覆盖整条语句；SQLite（只在测试里用）不渲染。
- 索引：没有项目时走 `ix_usage_events_tenant_occurred` 的范围扫描，带项目时走 `ix_usage_events_project_occurred`，回表取其余列。分组要在排序前扫完整个区间（与 #403 的按日汇总同一代价，区间最长 92 天而不是一个月）；每一页都重算一次聚合。最坏情况由 2 秒预算封顶，扫描量与页码无关。
- **金额求和的精确性是 MySQL 上的性质**（生产与 CI 的 MySQL 分片）：`SUM(DECIMAL(20,8))` 是精确的十进制和，结果经 `Decimal(...)` 与 `money_text` 原样输出，不经 float。写法与 #403 的 `customer_usage_daily_statement` 相同（`func.coalesce(func.sum(UsageEvent.billable_cost), 0)`）。
- **SQLite 只在测试里用，且它把每一个 NUMERIC 列都存成浮点**（不只是 SUM：单行的 `billable_cost`、钱包余额同样如此，仓库既有测试 `tests/backend/test_customer_usage_summary_api.py` 的「金额精度」用例写明了这一点）。所以 SQLite 上的金额用例只用二进制可精确表示的金额（如 `0.25`、`0.5`、`1.125`），能判别 float 的精度用例只在 MySQL 上跑（§7「金额精度」）。不为 SQLite 另写一套精确求和：那只修测试库，生产路径不变，反而让两种库走不同的 SQL。

### 游标

- 内容是本页最后一行的 `last_occurred_at`（`%Y-%m-%dT%H:%M:%S.%f`）、项目 `public_id`、`conversation_id`，以 `|` 连接后 base64url 编码（不带填充）。`conversation_id` 的字符集不含 `|`，项目 `public_id` 是小写 uuid，三段可以无歧义地切开。
- 编码前最长约 192 字节，编码后最长 256 字符；解码前长度上限 512，超出、非 base64url、段数不对、时间不合规、uuid 不合规、`conversation_id` 不匹配 `REQUEST_ID_PATTERN` 一律 422 `VALIDATION_ERROR`（字段名 `cursor`）。客户端不得解析，后端以后可以改编码。
- 与 #398 的事件游标是两种格式、两个函数（`encode_conversation_cursor` / `decode_conversation_cursor`），不共用：拿事件游标来翻会话（或反之）得到 422，不会错位。
- 比较按字节（MySQL `utf8mb4_0900_bin`、SQLite 默认 `BINARY`；两段字符串都只含 ASCII），与 Python 的字符串比较一致。

### 接口二：`GET /api/v1/customer/conversations/export`

- 参数：`from_date`、`to_date`（必填，同接口一）、`project_id`（可选）；没有 `page_size` 与 `cursor`。
- 处理流程与 `customer_usage_events_export` 逐步相同：`require_customer` → `usage_period` → `require_session_factory`（未配置 503，早于租约）→ 不阻塞地获取导出租约（拿不到 503 `EXPORT_BUSY`、带 `Retry-After`、不发查询）→ 只 `hold` 进 `request.scope` 的租约槽、由最外层中间件释放 → 在一个 `try` 里查询（`limit = EXPORT_MAX_ROWS + 1`，无游标）、超过 20 000 行抛 `ExportTooLarge`（422 `EXPORT_TOO_LARGE`）、`write_csv`、`csv_response`；`QueryTooBroad` 记 `customer_usage_query_timeout` 后 422 `QUERY_TOO_BROAD`；其他 `Exception` 记 `customer_export_failed` 后在 `except` 块之外抛 `CustomerExportFailed`。
- **占导出租约**（与 #457 的月度汇总不同）：行数随会话数增长、可达 20 000 行，内存与 #435 的用量导出同一量级，租约的理由（单进程内存上界，INV-1）对它成立。`docs/api.md`「客户导出」里共用名额那一句加上会话摘要导出。
- 排序与接口一相同（最后活动时间倒序）；行数上限针对会话行，不是请求行。

| 列（按此顺序） | 文本列（防公式注入） | 值 |
| --- | --- | --- |
| `conversation_id` | 是 | 同接口一 |
| `project_id` | 是 | 同接口一 |
| `project_name` | 是 | 同接口一 |
| `first_occurred_at_utc` | 否 | 接口一的 `first_occurred_at` |
| `last_occurred_at_utc` | 否 | 接口一的 `last_occurred_at` |
| `request_count` | 否 | 十进制整数 |
| `input_tokens` | 否 | 十进制整数 |
| `output_tokens` | 否 | 十进制整数 |
| `charged_amount` | 否 | 8 位小数原样 |

- 每个单元格都取自接口一的白名单视图（`customer_conversation_view(row)` 的 JSON 值），不直接读查询结果：白名单只有一份（与 #435 `customer_usage_export_cells` 同一写法）。两个时间列改名加 `_utc`，与请求级导出的 `occurred_at_utc` 一致。
- 文件名 `conversations_<from_date>_<to_date>.csv`（只含 ASCII 字母数字与 `-_.`，满足 `csv_response` 的检查）。
- 格式、响应头（`Content-Disposition`、`Cache-Control: no-store`、`X-Accel-Buffering: no`）、分段发送、BOM 全部沿用 `write_csv` / `csv_response`。
- 内存：会话行 9 列，比用量行的 18 列短；仍按 #435 的写法用 `tracemalloc` 钉住 20 000 行最长形态会话行（每列取最大长度、每个字符串独立）走完投影、`write_csv` 与 `encode` 的峰值 ≤ 128 MiB。

### 时间

- 日期区间沿用 #398 §2「时间」：`[from_date 00:00 − 8h, (to_date + 1 天) 00:00 − 8h)`，按 `occurred_at` 取。
- 晚到或晚计费的事件计入它发生的时刻：同一区间隔一段时间再看，会话的合计可能变大，甚至多出会话（spec §111）。

### 金额

- 接口：`Decimal` 和 → `money_text` 8 位小数，不舍入。CSV 同样原样 8 位。
- 前端显示：与用量页相同，`formatAmountRounded(amount, 6)`（spec §80「Detailed usage may display up to 6 decimals」），只在显示层、只做字符串运算。

### 错误

接口一：401 `TOKEN_INVALID`、403 `CUSTOMER_REQUIRED`、422 `VALIDATION_ERROR`、422 `QUERY_TOO_BROAD`、500 `INTERNAL_ERROR`、503 `DATABASE_NOT_CONFIGURED`。接口二另有 422 `EXPORT_TOO_LARGE`、503 `EXPORT_BUSY`，其 500 在租约内发出（`CustomerExportFailed`，信封与通用 500 逐字相同）。没有 404（项目不属于本客户 → 空页 / 只有表头的文件）；没有 `ACCOUNT_READ_ONLY`（都是读，用 `require_customer`）。错误响应是 JSON 信封、不是 CSV。

### 前端（第三、四个任务，不在闸门范围内，写在这里只为说明接口够用）

- 会话页 `/portal/conversations`（`frontend/src/routes/paths.ts` 的 `portalConversations`，挂在 `RequireRole`（CUSTOMER）与 `PortalLayout` 之下；门户导航在「Usage」之后加「Conversations」）：
  - 日期区间（默认马来西亚时间本月 1 日到今天，2020-01-01..2099-12-31，超过 92 天不发请求并提示：与用量页同一套规则，复用 `UsagePage.tsx` 已导出的日期函数或把它们原样导出后复用，不另写一份）与项目下拉（`GET /api/v1/customer/projects`）；点「Apply filters」生效。
  - 表格列：会话 ID、项目、区间内首次请求（MYT）、最后活动（MYT）、请求数、输入 / 输出 token、计费额（最多 6 位小数）。「Older」用 `next_cursor`、「Newest」回第一页；换条件游标作废。
  - 表格上方一句说明：只列有会话 ID 的请求；没有会话 ID 的请求（例如 AI API 代报的）在用量页看；合计只算所选日期区间内的请求。
  - 点一行（或会话 ID 链接）跳到 `/portal/usage?conversation_id=…&project_id=…&from_date=…&to_date=…`，日期取会话页**当前已应用**的区间。
  - 加载中、失败（带 `request_id`）、空列表三种状态；`QUERY_TOO_BROAD` 专门文案。
- 用量页（`UsagePage.tsx`）：进页面时，若查询串带 `conversation_id`（且不带 `event_id`），把 `conversation_id`、`project_id` 与 `from_date` / `to_date` 作为初始的已应用筛选与表单值；两个日期缺一或不合规时日期用默认区间、其余照用。之后的筛选仍只在组件状态里、不写回地址栏；`event_id` 点查的行为不变（带 `event_id` 时其余查询参数一概不生效）。
- 导出按钮（第四个任务）：会话页「Export CSV」用已应用的筛选调接口二，经 `downloadFile` 下载；兜底文件名与后端同一规则；导出中按钮加载、不可重复点；`EXPORT_TOO_LARGE`、`QUERY_TOO_BROAD`、`EXPORT_BUSY` 各有专门文案，其余带 `request_id`；导出不影响列表与翻页。
- 页面不请求任何 `/api/v1/admin/` 路径，不显示成本、毛利或内部字段。
- 浏览器验收：第三个任务加只读步骤 `check_portal_conversations`（经门户导航打开会话页、默认区间；被动观察页面发出的 `GET /api/v1/customer/conversations` 的响应体，表格行数与 `items` 条数一致，非空时第一行的计费额等于 `items[0].charged_amount` 按 6 位 `ROUND_HALF_UP` 舍入的值，空表也算通过；`QUERY_TOO_BROAD` 判 FAIL；不点行、不翻页）；第四个任务加只读步骤 `check_portal_conv_export`（停在会话页，只核对「Export CSV」按钮恰好一个且可用，不点击）。

## 3. 不变量影响矩阵

| 不变量 | 是否触碰 | 可能怎样被破坏 | 设计控制 | 验证方式 |
| --- | --- | --- | --- | --- |
| INV-1 中心故障不中断 AI | 否（间接） | 只读，不在 AI 或摄取路径上；与计费、AI 模块共用数据库与进程：92 天的聚合要扫完整个区间、每页重算；导出可达 2 万行 | 一条 SELECT 2 秒执行预算，超时即中止；导出占进程级导出租约、20 000 行上限、内存峰值 ≤ 128 MiB（与 #435 同一机制） | 测试：MySQL 上 1 毫秒预算得 `QUERY_TOO_BROAD`；租约被占时导出 503 且不发查询；`tracemalloc` 峰值上界 |
| INV-2 事件不重复扣费 | 否 | 不写 | 无写路径 | 测试：调用前后 `usage_events`（行数与状态）、`wallet_transactions`、`wallets`、`audit_logs`、`domain_outbox` 不变 |
| INV-3 支付不重复入账 | 否 | 同上 | 同上 | 同上 |
| INV-4 余额只经账本变动 | 否 | 同上 | 同上 | 同上 |
| INV-5 历史账本不可变 | 否 | 不读账本；`billable_cost` 在 `PROCESSED` 后由迁移 0018 的触发器保证不变 | — | — |
| INV-6 事件保留版本引用 | 否 | 返回版本引用等于泄露定价结构 | 不读不返回 | 键集合与表头逐字断言 |
| INV-7 客户不可见成本毛利 | **是** | ① 聚合时顺手 SUM 了 `estimated_provider_cost_myr` 或 `reference_customer_price`；② 复用管理端查询或 schema；③ 内部计量租户的参考价被当成计费额 | ① 内层只读 §2 列出的 8 列、外层只取 9 列，金额只 SUM `billable_cost`；② 新的 9 键白名单模型（新文件 `app/schemas/customer_conversations.py`），不继承管理端模型；CSV 单元格只取自这个视图；③ 内部计量的 `billable_cost` 恒为 0 | 测试：成本、原币成本、汇率、参考价写成哨兵值，断言 JSON 原始响应文本与 CSV 原始字节里都没有；键集合逐字恰好 9 个、表头逐字；内部计量租户 `charged_amount = "0.00000000"` |
| INV-8 租户不可互访 | **是** | ① 从请求取租户；② 漏掉租户过滤；③ 拿别的客户的项目 id 或游标看到别人的会话；④ 两个客户用同一个 `conversation_id` 时被分进同一组 | ① 租户只来自 `CustomerPrincipal.tenant_id`，接口没有租户参数；② 内层 `WHERE tenant_id = :tenant_id` 恒在；③ 项目先在本租户内查出，查不到空页；游标只参与 `<` 比较，不改变租户条件；④ 分组键含 `project_id`，项目属于唯一的租户，且租户条件在分组之前 | 测试：客户 A、B 共用同一个 `conversation_id` 值；A 用 B 的项目 id、B 的游标，带 `?customer_id=<B>&tenant_id=<B 的内部 id>`，都只得到 A 的行或空页；A 的会话合计不含 B 的事件；导出同样 |
| INV-9 对话内容不入库 | 否 | 不新增存储；只返回 `conversation_id` 这个集成方给的标识（摄取时已按格式限制） | — | — |
| INV-10 金额用 Decimal | **是** | 求和经 float；前端显示舍入改了值；CSV 层另算 | MySQL 上数据库内十进制 SUM（精确），结果转 `Decimal`、`money_text` 原样；CSV 单元格取视图的字符串；前端 6 位舍入只在显示层、只做字符串运算。SQLite（只在测试里用）把 NUMERIC 存成浮点，精度判别只在 MySQL 上做（§2「查询」） | 测试（只在 MySQL）：能被 float 判别出来的和，接口一与 CSV 都逐字相等（§7「金额精度」）；完成标准 2 的逐字对账（SQLite 与 MySQL，SQLite 上用二进制可精确表示的金额） |
| INV-11 event_id 至多一次财务效果 | 否 | 不写 | — | — |
| INV-12 定稿对账单不可变 | 否 | 对账单还不存在；会话合计会随晚到事件变化，不是对账单 | `docs/api.md` 写明 | — |
| INV-13 状态与事件原子提交 | 否 | 没有状态变化与出站事件；只读 `PROCESSED` 行，它的计费额与账本行在计费事务里同时提交；一条 SELECT 是一个一致快照 | — | — |
| INV-14 队列丢失不毁持久工作 | 否 | 不用 Redis / Celery、不落盘 | — | — |

## 4. 状态与并发

- **没有状态变化**，没有跃迁表、没有幂等键；GET 天然可重复。
- **与计费并发**：每页是一条 SELECT 的一致快照；计费把事件改成 `PROCESSED` 与写账本同一事务提交，所以一页里每个会话的合计都来自同一组已提交的 `PROCESSED` 行。
- **翻页与新数据**（排序键 `(last_occurred_at, project public_id, conversation_id)`，在一个客户内唯一）：
  - `PROCESSED` 是终态、事件只增不删，所以一个会话的 `last_occurred_at` 只会变大或不变。
  - 已经返回过的会话：之后的键 ≥ 当时的键 ≥ 游标，后面的页（只取严格小于游标的键）不会再返回它——**不重复**。
  - 还没返回的会话在翻页途中有了新活动、键越过游标：后面的页里不会出现它，它出现在「Newest」那一端。这是读时聚合 + 键集分页的已知行为，在 `docs/api.md` 写明；不出现重复、不出现错数。
  - 还没返回的会话收到 `occurred_at` 更旧的晚到事件：键不变，合计在它出现的那一页里已包含该事件（如果事件在那之前已计费）。
- **导出**：一条 SELECT、一个快照、没有分页，以上翻页问题不存在。
- **不加锁**：不阻塞计费与摄取，也不被它们阻塞。重试由客户发起，服务端不重试。

## 5. 失败与恢复矩阵

| 失败点 | 对外结果 | 数据最终状态 | 是否可重试 | 恢复来源 | 告警 |
| --- | --- | --- | --- | --- | --- |
| 日期缺失、格式、越界、倒置、超过 92 天；`page_size` 越界；游标不合规 | 422 `VALIDATION_ERROR`，只列字段名 | 不变 | 修正后重试 | — | 无 |
| 未登录 / 令牌过期 / 门户账号非 `ACTIVE` | 401 `TOKEN_INVALID` | 不变 | 刷新后重试（非 `ACTIVE` 不行） | 刷新令牌 / 管理员 | 无 |
| ADMIN、无租户 CUSTOMER | 403 `CUSTOMER_REQUIRED` | 不变 | 否 | — | 无 |
| 租户 `DISABLED` / `CLOSED` / 只读 | 200 照常（读） | 不变 | — | — | 无 |
| 项目不属于本客户 | 空页 / 只有表头的文件，不发聚合查询 | 不变 | — | — | 无 |
| 聚合 SELECT 超过 2 秒 | 422 `QUERY_TOO_BROAD`；导出不返回任何文件内容 | 不变（语句被中止） | 是，缩小区间或选项目 | — | `customer_usage_query_timeout` 日志（只带 `request_id` 与客户 `public_id`）；常见时另开闸门建聚合表 |
| 导出超过 20 000 个会话 | 422 `EXPORT_TOO_LARGE` | 不变 | 是，缩小区间或选项目 | — | 无 |
| 导出时租约被占 | 503 `EXPORT_BUSY` + `Retry-After`，不发查询 | 不变 | 是，几秒后 | — | 无（沿用 #435 的租约持有过久告警日志） |
| 数据库未配置 | 503 `DATABASE_NOT_CONFIGURED`（早于租约与查询） | 不变 | 是 | 运维 | 现有 |
| 查询时数据库不可用 / 其他错误 | 接口一：通用 500；接口二：租约内的 `CustomerExportFailed`（500，信封相同），记 `customer_export_failed` | 不变 | 是 | 数据库恢复 | 现有错误日志与 readyz 巡检 |
| 客户端下载中断 | 浏览器侧失败 | 不变（服务器不留文件） | 是 | — | 无 |
| 翻页途中会话有新活动 | 该会话不在后面的页里，回到「Newest」可见 | 不变 | — | — | 无（§4，文档写明） |
| Redis/Celery 丢失 | 无影响 | 不变 | — | — | 无 |
| 外部服务超时 | 不适用 | — | — | — | — |
| 重复请求 | 期间没有新计费事件时结果逐字节相同 | 不变 | 是 | — | 无 |

## 6. 数据与安全边界

- **租户过滤**：聚合内层 `WHERE tenant_id = :tenant_id`，`tenant_id` 只来自 `require_customer` 返回的 `CustomerPrincipal.tenant_id`；项目筛选与游标都是附加条件。
- **鉴权主体**：只有 CUSTOMER（`ACTIVE`、有租户）。两个处理函数第一条语句显式调用 `require_customer`；两个新路由进 `tests/backend/test_customer_api.py` 的路由枚举与守卫用例。
- **禁止出现在响应与文件里的**：见 §2「刻意不返回」；项目只出 `public_id` 与 `name`（不出 `description`、`integration_status`、`status_webhook_url`）。
- **日志、审计、异常**：沿用请求日志；不记录日期、项目、游标、`conversation_id` 的值，不记录金额与文件内容；超时与导出失败日志沿用现有两条（只带 `request_id` 与客户 id / 路径）。读操作不写审计。
- **留存（spec §112）**：导出与 #435 相同，内存里生成、请求结束即释放；`Cache-Control: no-store`、`X-Accel-Buffering: no`。
- **公式注入**：三个文本列（`conversation_id`、`project_id`、`project_name`）按 `write_csv` 的规则防护；`project_name` 是管理员起的自由文本，可能以 `=`、`+`、`-`、`@` 开头。
- **密钥**：不涉及。**prompt / response**：不处理（`usage_events` 没有内容列）。

## 7. 测试证据计划

后端（`tests/backend/test_customer_conversations_api.py` 与 `tests/backend/test_customer_conversations_export_api.py` 两个新文件，SQLite 与 MySQL 参数化，MySQL 用 `BILLING_TEST_DATABASE_URL`、未设置时 skip、CI 必跑；客户登录夹具照 `tests/backend/test_customer_usage_api.py` 的写法；SQL 文本断言不写死方言字面量；示例 uuid 全零）：

| 风险/需求 | 测试层级 | 场景 | 预期结果 |
| --- | --- | --- | --- |
| 正常路径 | integration | 区间内两个项目、三个会话（其中一个会话 4 条请求，含 1 条 `QUANTITY` 形态），另有 2 条没有 `conversation_id` 的请求 | 3 行，按 `last_occurred_at` 倒序；每行恰好 9 个键；`request_count`、token 和、`charged_amount`、首末时刻逐字等于按库里的值手算的结果；`QUANTITY` 那条计入请求数与金额、不计 token；没有会话 ID 的请求不出现、不计入任何行 |
| 与请求级明细对账 | integration（SQLite 上金额用二进制可精确表示的值） | 同一批数据，对每一行调 `GET /usage-events?from_date&to_date&project_id=<该行项目>&conversation_id=<该行会话>&page_size=100` | 每行的 `request_count` = items 条数，token 和、`charged_amount`（Decimal 相加后 `money_text`）逐字相等，首末时刻等于 items 的 `occurred_at` 最小 / 最大值 |
| 分组键 | integration | 同一 `conversation_id` 在同一客户的两个项目里；`conv_A` 与 `conv_a` 在同一项目里 | 前者两行（项目不同）；后者两行（按字节区分大小写，SQLite 与 MySQL 结果相同） |
| 区间裁剪 | integration | 一个会话的事件在 UTC `2026-09-30 15:59:59.999999`、`16:00:00`、`2026-10-01 15:59:59.999999`、`16:00:00` | `from_date = to_date = 2026-10-01` 时该会话只计中间两条，`first_occurred_at` / `last_occurred_at` 为这两条的时刻 |
| 只算已计费 | integration | 同一会话另有 `RECEIVED`、`PROCESSING`、`PRICING_ERROR`、`MODEL_UNKNOWN`、`FAILED_RETRYABLE`、`FAILED_FINAL` 各一条 | 都不计入；只有未计费事件的会话不出现 |
| 排序与平局 | integration | 3 个会话的 `last_occurred_at` 完全相同（微秒相同），分属两个项目；`page_size=1` 翻完 | 按（项目 `public_id`、`conversation_id`）倒序逐个出现，不重不漏；最后一页 `next_cursor = null` |
| 翻页 | integration | 5 个会话，`page_size=2` 连翻三页 | 2 / 2 / 1，前两页 `next_cursor` 非空 |
| 翻页中有新活动（并发） | integration（MySQL） | 取第一页（2 个会话）后，另一事务：给第一页里的会话 X 插入一条更新的 `PROCESSED` 事件，给尚未返回的会话 Y 插入一条比所有现有事件都新的 `PROCESSED` 事件；然后用 `next_cursor` 翻完 | 全程没有任何会话出现两次；Y 不在后面的页里；重新取「Newest」第一页时 Y 与 X 都在、合计含新事件 |
| 日期与参数规则 | integration | 缺 `from_date` / `to_date`、倒置、93 天、格式错、`0001-01-01` 与 `2100-01-01`；`page_size` 0 / 101；接口二带 `page_size`、`cursor`（被忽略） | 422 `VALIDATION_ERROR`、只列字段名、不回显值；92 天 200；接口二忽略多余参数照常 200 |
| 游标 | unit + integration | 编码解码往返（含 128 字符的 `conversation_id`）；篡改的游标：非 base64url、缺段、多段、非法时间、非法 uuid、`conversation_id` 含非法字符、超长；把 `GET /usage-events` 的 `next_cursor` 拿来用 | 往返相等；篡改与事件游标都 422（字段名 `cursor`） |
| 项目筛选 | integration | 按本客户的一个项目筛；用不存在的、别的客户的项目 id | 只出现该项目的会话；后两种空页且不发聚合查询（monkeypatch 计数） |
| 扫描上限 | integration（MySQL） | 30 000 条区间内的 `PROCESSED` 事件分布在 3 000 个会话里：① 测试专用 1 毫秒超时；② 默认 2 秒 | ① 接口一与接口二都 422 `QUERY_TOO_BROAD`，各一条 `customer_usage_query_timeout` 日志，不带任何参数值；② 接口一 200。另断言 MySQL 上编译出的 SQL 恰好带一次 `MAX_EXECUTION_TIME` 提示、在最外层，SQLite 上不带（归一后比较） |
| 内部计量租户 | integration | `INTERNAL_METERED_ONLY` 的会话，`reference_customer_price` 为哨兵值 | `charged_amount = "0.00000000"`、请求数与 token 照常；响应与文件里没有哨兵值 |
| 无成本毛利 | integration | `estimated_provider_cost_myr`、`provider_source_cost`、`fx_rate_applied`、`reference_customer_price` 写成哨兵值；项目 `description`、`status_webhook_url` 写成哨兵串 | JSON 原始响应文本与 CSV 原始字节里都没有任何哨兵值 |
| 金额精度 | integration（**只在 MySQL**；照 `tests/backend/test_customer_usage_summary_api.py` 的写法，SQLite 存浮点、判别不出） | 四个会话各自的事件：① 三条 `0.00000001`；② `999999999999.00000000` 与 `0.00000001`；③ `1234567890.12345678` 与 `0.00000001`；④ `999999999999.99999999` 与 `0.00000001`。用例先断言前提：②③ 的期望值经 float 往返后与期望值不同 | ① `0.00000003`；② `999999999999.00000001`；③ `1234567890.12345679`；④ `1000000000000.00000000`（Decimal 不溢出）；接口一的 `charged_amount` 与 CSV 单元格都逐字等于期望值 |
| 租户越权 | integration | 客户 A、B 共用同一个 `conversation_id` 值；A 用 B 的项目 id、B 的最后一页游标，并带 `?customer_id=<B 的 public_id>&tenant_id=<B 的内部 id>` | 只有 A 的行或空页；A 的合计不含 B 的事件；A 的任何响应与文件里不出现 B 的项目名与金额 |
| 鉴权 | integration | 无令牌、坏令牌、ADMIN 令牌、门户账号停用后的旧令牌 | 401 / 401 / 403 `CUSTOMER_REQUIRED` / 401，且不发聚合查询、不取租约 |
| 只读契约 | integration | 租户四种 `account_status` | 两个接口都 200 |
| 路由守卫 | integration | `tests/backend/test_customer_api.py` 的客户路由枚举与守卫用例 | 两个新路由被枚举到；匿名 401、ADMIN 403 |
| 无写入 | integration | 每个用例前后 | `usage_events`（行数与状态）、`wallet_transactions`、`wallets`、`audit_logs`、`domain_outbox` 不变 |
| 数据库断连 | integration | monkeypatch repository 函数抛 `sqlalchemy.exc.OperationalError` | 接口一：500 `INTERNAL_ERROR` 带 `request_id`；接口二：500（`CustomerExportFailed`，信封与通用 500 逐字相同）、一条 `customer_export_failed` 日志、请求结束后租约已释放；之后正常请求 200 |
| 导出正常路径 | integration | 与「正常路径」同一批数据 | 200、`text/csv; charset=utf-8`、文件名 `conversations_<from>_<to>.csv`、`Cache-Control: no-store`、`X-Accel-Buffering: no`、BOM 开头；表头逐字为 §2 的 9 列；行序与接口一相同；每个单元格与接口一同一行的对应值逐字相等（时间列对应去掉 `_utc` 的键） |
| 导出行数上限 | integration | 照 `tests/backend/test_customer_usage_export_api.py` 的写法：断言查询的 `limit` 是 `EXPORT_MAX_ROWS + 1`；repository 返回 `EXPORT_MAX_ROWS` 行与 `EXPORT_MAX_ROWS + 1` 行（monkeypatch） | 前者 200、恰好 20 000 个数据行；后者 422 `EXPORT_TOO_LARGE`、JSON 信封 |
| 导出租约 | integration | 测试先占住导出租约再调接口二；正常导出后检查租约 | 503 `EXPORT_BUSY`、带 `Retry-After`、不发查询；正常导出后租约已由中间件释放 |
| 公式注入 | integration | 项目名为 `=SUM(A1)`、`+1`、`-1`、`@x` | 文件里该单元格按 `write_csv` 的规则加了防护前缀；数字列不加 |
| 只有表头 | integration | 区间内没有会话；项目不属于本客户 | 200，只有 BOM 与表头一行 |
| 内存上界 | unit | `tracemalloc` 下 20 000 行最长形态会话行（`conversation_id` 128 字符、项目名取列的最大长度、金额 `999999999999.99999999`、token 取 18 位整数，每个字符串独立）走完 `customer_conversation_view`、单元格转换、`write_csv` 与 `encode` | 峰值 ≤ 128 MiB |
| 常量 | unit | 列清单、文本列 | 恰好 §2 的 9 列与 3 个文本列 |
| 幂等重放 / 事务中途失败 / Redis 丢失 | — | 只读、不入队 | 不适用（重复请求在无新事件时结果相同，由「翻页」与「导出正常路径」间接覆盖） |

前端（Vitest + Testing Library）：会话页默认日期是马来西亚时间本月 1 日到今天（固定「现在」测 UTC 16:00 前后两侧）；超过 92 天不发请求；项目筛选与翻页的查询串（不带供应商、模型、`conversation_id`）；时间 +8 显示并标 MYT；计费额 6 位舍入显示；点一行跳到的地址恰好带 `conversation_id`、`project_id`、`from_date`、`to_date` 四个参数，日期是已应用的区间；没有会话 ID 的说明文字在；三种状态与 `QUERY_TOO_BROAD` 文案；不请求 `/api/v1/admin/` 路径。用量页：查询串带 `conversation_id` 等四个参数时首个请求就带这四个筛选、表单显示它们；日期缺一或不合规时用默认区间、其余照用；同时带 `event_id` 时只按 `event_id` 查；不带这些参数时行为与现在相同（既有用例不改）。导出按钮：请求地址带已应用的筛选、不带 `page_size` 与 `cursor`；兜底文件名；导出中不可重复点；三种专门文案与通用失败。

## 8. 迁移与上线

- **数据迁移步骤**：无。不改表、不加索引。
- **锁表与性能影响**：无 DDL。每次请求一条范围扫描 + 分组，2 秒上限；会话页每次翻页重算一次聚合，代价与 #403 的首页汇总同类（区间更长）。生产上现在只有夹具与内部租户，内部租户的事件来自 AI 模块、没有 `conversation_id`。
- **兼容窗口**：新增路由，不改任何既有接口的行为与响应；`docs/api.md`「客户导出」只把共用名额那一句扩到会话摘要导出。用量页新增的查询串参数只在带 `conversation_id` 时生效，现有链接（钱包流水的 `event_id`）行为不变。
- **部署顺序与拆分**（批准后登记为四个任务，`depends_on` 串起来，每个合并后都能单独通过 CI 并部署）：
  1. **接口一（会话列表）**：`app/repositories/usage_events.py` 加 `CustomerConversationRow`、`customer_conversation_statement`、`list_conversations_for_customer`；新文件 `app/schemas/customer_conversations.py`（9 键视图、分页、会话游标编解码）；`app/api/customer.py` 一个路由；新测试文件 `tests/backend/test_customer_conversations_api.py`；`tests/backend/test_customer_api.py` 路由枚举加一行；`docs/api.md` 新增「客户会话」一节（含 §4 翻页说明）。依赖 AIH-TASK-119（同改 `app/api/customer.py`、`docs/api.md`、`tests/backend/test_customer_api.py`，排在它之后避免冲突）。
  2. **接口二（会话摘要导出）**：`app/schemas/customer_conversations.py` 加列清单、文本列与单元格转换；`app/api/customer.py` 一个路由；新测试文件 `tests/backend/test_customer_conversations_export_api.py`（含内存上界）；`tests/backend/test_customer_api.py` 路由枚举加一行；`docs/api.md`「客户导出」加本接口一节并改共用名额那一句、删掉「会话摘要导出不在这里」。依赖 1。
  3. **前端会话页**：`frontend/src/api/customerConversations.ts` 与测试、`frontend/src/features/portal/ConversationsPage.tsx` 与测试、`UsagePage.tsx` 读查询串初始筛选与测试、`frontend/src/routes/paths.ts`、`frontend/src/routes/index.tsx`、`frontend/src/layouts/PortalLayout.tsx`、`en.json`、验收脚本步骤 `check_portal_conversations`；勾掉 `docs/TODO.md` Phase 5 的「会话摘要」。依赖 1 与 AIH-TASK-120（同改 `UsagePage.tsx`、`en.json`、验收脚本）。
  4. **前端导出按钮**：`customerConversations.ts` 加导出函数与测试、`ConversationsPage.tsx` 加按钮与测试、`en.json`、验收脚本步骤 `check_portal_conv_export`。依赖 2 与 3。Phase 5「客户端用量导出」仍不勾（Excel 未做）。
- **回滚或前滚方式**：revert 即可，没有数据与结构变化。
- **部分部署时的行为**：只部署 1 → 接口可用、页面没有入口；只部署到 2 → 多一个导出接口；只部署到 3 → 会话页可用、没有导出按钮。都不影响现有功能。
- **监控指标和告警负责人**：不新增；沿用 `customer_usage_query_timeout`、`customer_export_failed` 与导出租约的既有日志。负责人 Kelvin。

## 9. 方案取舍

| 方案 | 优点 | 风险 | 未采用原因 |
| --- | --- | --- | --- |
| **采用**：读时一条聚合 SELECT（派生表分组 + 外层 JOIN 项目）、区间内裁剪、（项目, `conversation_id`）为键、最后活动倒序的键集游标、2 秒预算；导出复用 #435 的租约与上限；点进会话复用 `GET /usage-events` | 不建表、不碰计费事务；会话合计与请求明细逐条对得上；白名单与越权控制沿用已批准的写法 | 每页重算聚合，大客户的长区间可能 `QUERY_TOO_BROAD`；翻页途中有新活动的会话会跳到最新一端（§4） | — |
| 备选 A：`conversation_usage_summaries` 聚合表，由定时任务或计费事务维护、可重建（spec §74、§111） | 读快、分页便宜 | 多一个迁移、一个维护任务与重建命令；在计费事务里维护会加长热租户的钱包行锁持有时间（ADR-0011 的压测前提要重测）；按区间裁剪的合计仍要读时算 | Kelvin 2026-10-27 选读时实时算；超时日志常见时按证据再做 |
| 备选 B：会话全量口径（区间内有活动的会话，合计算它的全部请求） | 「开始时间」是真开始 | 要两步查询（先找会话、再按 `ix_usage_events_conversation_id` 汇总全量），排序键可能落在区间外；与区间内的请求明细对不上 | Kelvin 2026-10-27 选只算区间内 |
| 备选 C：新增 `GET /conversations/{id}` 返回一个会话的请求（spec §90） | 与 spec 的路由清单一字不差 | `conversation_id` 跨项目不唯一，路径里还得带项目；等于把 `GET /usage-events` 的请求级白名单、分页、越权测试再做一遍 | 现有接口按 `conversation_id` + `project_id` + 区间已能精确列出，且与会话行逐条对得上 |
| 备选 D：没有 `conversation_id` 的请求合成一行「无会话」 | 会话表合计 = 区间总用量 | 这一行不是会话，排序与游标要特判；AI 模块客户的会话页只有这一行 | Kelvin 2026-10-27 选不进会话表、页面注明 |
| 备选 E：按 `conversation_id` 单独分组（不含项目） | 键更短 | 同一客户两个项目用同一个 id 时被并成一行，合计跨项目、点进去不知道看哪个项目 | spec §10：id 由集成方生成，只在集成方内有意义 |
| 备选 F：`page` / `total` 偏移分页 | 前端可显示页码 | `total` 要多一次全量聚合；偏移在聚合结果上同样要先算完；新活动会让偏移翻页重复 | spec §108 偏好游标，且与用量页一致 |
| 备选 G：会话表也接受供应商 / 模型 / `conversation_id` 筛选 | 筛选与用量页一致 | 供应商筛选把会话合计切成部分，含义含糊；找单个会话用用量页更直接 | 只留日期与项目 |
| 备选 H：导出不占租约（与 #457 一致） | 不会 503 | 行数可达 2 万行，内存与用量导出同量级，#457 不占租约的理由（文件恒约 2 KB）不成立 | 占租约 |

## 10. 未决问题与假设

- **未决问题**：无阻断项。
- **已拍板的取舍**（Kelvin 2026-10-27）：读时实时算、不建聚合表；只算所选日期区间；没有 `conversation_id` 的请求不进会话表、页面注明；会话摘要导出同批登记。
- **尚未验证的假设**：
  1. AIH-TASK-119 按 #457 v1 交付后，`app/api/customer.py`、`docs/api.md`「客户导出」与 `tests/backend/test_customer_api.py` 的形状如 §1 所述（119 只新增一个路由与一节文档）。若形状不同，接口任务停下说明。
  2. MySQL 对「派生表内按租户（或项目）+ 区间分组」会用 `ix_usage_events_tenant_occurred` / `ix_usage_events_project_occurred` 做范围扫描。依据：等值前缀 + 范围是这两个索引的设计用途（#398），与 #403 的按日汇总同一访问路径。若没选中，后果是慢、最坏 `QUERY_TOO_BROAD`，不是错数据。
  3. 现阶段客户 92 天内的会话聚合在 2 秒内完成：生产上现在只有夹具与内部租户，带 `conversation_id` 的事件很少。错了的后果是 422 `QUERY_TOO_BROAD`，客户可缩小区间或选项目；按超时日志另开闸门建聚合表。
  4. 20 000 行最长形态会话行的内存峰值 ≤ 128 MiB：会话行的列数是用量行的一半、字段更短，而用量行已在该上界内（`tests/backend/test_csv_export.py`）。若测得超过，接口二任务停下说明，由新版本设计降低上限。
- **需要谁拍板**：无新增。
- **如果假设错误，具体后果**：最坏是查询或导出失败（422 / 500），不会给出与请求明细对不上的合计或越权的数据。

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
| v2 | 2026-10-27 | 金额求和的精确性写明是 MySQL 上的性质，SQLite（只在测试里用、所有 NUMERIC 列存浮点）上的金额用例只用二进制可精确表示的金额；§7「金额精度」改为只在 MySQL 上跑、加入 `999999999999.00000000 + 0.00000001` 与 `1234567890.12345678 + 0.00000001` 两个能被 float 判别的和并先断言判别前提；§2「查询」、§3 INV-10、§7「与请求级明细对账」相应修改 | Codex 审查 v1 的阻断项：SQLite 的 `SUM(NUMERIC)` 是浮点运算，v1 的 `999999999999.99999999 + 0.00000001` 恰好得到整数，掩盖失精 |

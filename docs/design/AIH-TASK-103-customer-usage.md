# AIH-TASK-103 / 104 / 105 / 106 设计：客户用量明细（已批准 v4）

> **来源**：设计闸门 Issue #398。本文件是 `APPROVED: design v4` 那一版正文的**逐字副本**（审查者 Codex；v1–v3 被要求修改，见 §12；送审时状态行为 `READY_FOR_REVIEW`，批准后 Issue 上只把状态行改成了 `APPROVED: design v4`）。
> 放进仓库，是因为 OpenClaw Worker 在沙箱里不联网、读不到 GitHub Issue。
> **实现以本文件为准**；与 Issue 不一致时，以 Issue #398 上被批准的 v4 为准。设计要改，就回到 Issue 升版本、重新过闸门，不要直接改本文件。
> **前置**：设计闸门 #368（[AIH-TASK-089-customer-portal-auth.md](AIH-TASK-089-customer-portal-auth.md)）的 `AIH-TASK-093`；设计闸门 #393（[AIH-TASK-099-customer-wallet.md](AIH-TASK-099-customer-wallet.md)）的 `AIH-TASK-099`、`AIH-TASK-100`、`AIH-TASK-101` 与金额显示的舍入函数 `AIH-TASK-102`（§2「金额」说的 `formatAmountRounded` 由 102 交付，用量页只复用）。
> §8 的四个实现任务登记为 `AIH-TASK-103`（迁移加两个索引）→ `AIH-TASK-104`（项目列表与用量两个接口）→ `AIH-TASK-105`（门户用量页）→ `AIH-TASK-106`（钱包流水的 `event_id` 链到用量页），按这个顺序依赖。

---

## 0. 准入判定

| 任务涉及 | 要填的范围 |
| --- | --- |
| 钱包 / 账本 / 定价 / 汇率 / 支付 / 幂等 / 状态机 | **全部章节** |
| 用量摄取 / 认证与会话（集成侧与平台侧）/ Webhook | §1–§7（**§6 不可省**） |
| 前端 / 文档 / CI / 脚本 | 不走本闸门 |
| 既不碰钱、也不碰用量摄取 / 认证与会话 / Webhook 的后端改动 | 不走本闸门 |

本任务档位：`全部`。只读，不写用量事件、不写账本，但对客户展示的是每条请求的计费额（`billable_cost`），旁边就是同一行上的供应商成本、汇率、参考价与毛利来源（INV-7 正面触碰），还要给 `usage_events` 加一个索引（迁移）。所以按全部章节填，只读带来的「不触碰」在 §3 逐条说明理由。

---

状态：`待审`（`READY_FOR_REVIEW`）
负责人：Claude
**设计版本**：`v4`
对应需求：spec §68「Customer Usage Page」（筛选：date / project / provider / model / conversation）、§69「Customer Request Detail」（Request ID、Date/time、Project、Provider、Model、Input/Output Tokens、other usage quantities、Customer Charged Amount；**不显示**估算或对账后的供应商成本、markup、margin、内部供应商定价）、§90（`GET /api/v1/customer/usage-events`；租户只取自会话）、§108（请求级用量表用游标分页，「Never return hundreds of thousands of usage rows」）、§109（存 UTC，默认展示时区 `Asia/Kuala_Lumpur`）、§128 Phase 5「Request-level details」「usage filters」与验收（客户看得到 Provider / Model / Tokens / Customer Charge，看不到成本、加价、毛利）、`REQ-PRIV-001`（INV-8）、`REQ-PRIV-002`（INV-7）
前置设计：#368 v12（客户门户认证：`require_customer`、`CustomerPrincipal`、只读契约）、#393 v1（客户钱包：客户侧白名单写法、流水行的 `event_id`）
目标 PR：待开（批准后按 §8 拆四个任务登记；`docs/TODO.md`「后续计划」里的「Phase 5 会话摘要、请求级明细与用量筛选」中请求级明细与筛选两部分即本闸门）

## 1. 目标与边界

- **要解决的问题**：客户门户（#368）与钱包页（#393）上线后，客户能看到「AI 用量扣了 RM0.0412」，但看不到这笔钱对应哪次调用、哪个项目、哪个模型、多少 token。spec §68、§69 要求请求级明细与筛选；§128 把它列为 Phase 5 的验收项。管理端早有用量事件列表（AIH-TASK-034），但它带成本、毛利与内部状态，不能给客户。
- **可观察的完成标准**：
  1. `GET /api/v1/customer/projects` 返回登录客户自己的全部项目，每项恰好 `id`、`name` 两个键。
  2. `GET /api/v1/customer/usage-events` 返回登录客户自己的、**已计费**（`status = PROCESSED`）的用量事件，最新在前（按 `occurred_at`），游标分页；每项恰好 §2 列出的 18 个键，含计费额 `charged_amount`，不含任何成本、汇率、参考价、毛利、内部状态与内部 id。
  3. 筛选：马来西亚日历日区间（`from_date`、`to_date`，含两端，都在 2020-01-01 到 2099-12-31 之间）、项目、供应商、模型、`conversation_id`、`event_id`。没有 `event_id` 时日期区间必填、最多 92 天。
  3a. 每次查询在数据库里最多执行 2 秒（MySQL `MAX_EXECUTION_TIME`）；超时返回 422 `QUERY_TOO_BROAD`，请客户缩小日期区间或换筛选条件，不会让一个过宽的查询长时间占用与计费、AI 模块共用的数据库。
  4. 门户有一个用量页：默认显示本月（马来西亚时间），可按上述条件筛选、往旧翻页，点一行展开请求详情（spec §69 的字段），时间按马来西亚时间显示，计费额按 spec §80 最多显示 6 位小数；钱包流水的 AI 用量行可点 `event_id` 跳到用量页并只显示那一条。
- **明确不做什么**（Kelvin 2026-10-23 逐项确认）：
  - **不做会话摘要**（spec §68 的会话表、「Click conversation」）：要在读时 GROUP BY 或建聚合表（spec §111），另开闸门定；AI 模块产生的事件没有 `conversation_id`，那时一并定怎么呈现。本设计只提供按 `conversation_id` 筛请求级明细。
  - **只显示已计费的事件**：`RECEIVED`、`PROCESSING`、`FAILED_RETRYABLE` 还没有计费额；`PRICING_ERROR`、`FX_RATE_ERROR`、`MODEL_UNKNOWN`、`FAILED_FINAL` 是内部运维状态，由管理端处理（重新入队）。客户看到的每一行都有确定的计费额，与钱包流水对得上。
  - **不做导出**（spec §73，「Phase 5 客户端用量导出」仍在后续计划）。
  - 不做本月用量、请求数、对话数、用量图（仪表盘其余部分，仍在后续计划；本设计定下的营业时区是它们的先例，见 §2「时间」）。
  - 不做 `total`、不做按 `feature`、`usage_type`、`request_id` 筛选、不做排序选项。
  - 不写任何表；不改用量摄取、计费、账本。
- **现有行为与问题证据**：
  - 客户侧没有任何用量接口；`app/api/customer.py` 由 AIH-TASK-093 新建，钱包两个接口由 AIH-TASK-099 加。
  - 管理端 `GET /api/v1/admin/usage-events`（`app/api/admin_usage_events.py`）是 page / total 分页、按 `id` 倒序，响应 `UsageEventSummary` 含 `estimated_provider_cost_myr`、`reference_customer_price`、`billing_mode_snapshot`、`status`、`error_code`，详情还有 `gross_margin`、`fx_rate_applied`、`provider_source_cost`、`conflicts`（含集成方 `api_key`）。
  - `usage_events` 上没有 `(tenant_id, occurred_at)` 复合索引；按租户取时间段只能走 `ix_usage_events_tenant_id` 再残余过滤，或 `ix_usage_events_tenant_feature_occurred` 的 `tenant_id` 前缀（`app/models/usage.py:379`、`:388`）。
  - 客户现在看不到自己的项目（#393 §10「客户门户现在没有项目视图」）。
  - 代码里没有营业时区配置；吉隆坡时间只有 `app/core/fx_source.py:55` 的 `KUALA_LUMPUR_OFFSET = timedelta(hours=8)`（汇率与告警在用）。

## 2. 设计概要

```text
GET（客户访问令牌）→ 查询参数类型与范围校验（FastAPI，处理函数之前，422）→ require_customer（第一条语句：401 / 403 CUSTOMER_REQUIRED）
→ tenant_id = CustomerPrincipal.tenant_id（取自库里的 user 行，不取自令牌与请求）
→ 组合校验（日期区间、游标解码，422）→ 一条 SELECT → 白名单投影 → 200
（无状态变化、无财务效果、无出站事件、无审计）
```

### 接口一：`GET /api/v1/customer/projects`

无查询参数（未知参数忽略）。成功 200，`data`：

```json
{
  "items": [
    { "id": "00000000-0000-4000-8000-000000000001", "name": "Customer service bot" }
  ]
}
```

- 该客户的**全部**项目，按创建先后（`projects.id` 升序），复用 `list_projects_for_tenant(session, tenant_id)`（不传 offset / limit 即全部）。不分页：项目只由管理员创建，一个客户的项目数是个位到两位数；以后真有上百个项目再加分页（§10）。
- 只有 `id`（项目 `public_id`）与 `name`。**不返回** `description`、`integration_status`、`status_webhook_url`（可能带集成方令牌）、凭据、内部 id。

### 接口二：`GET /api/v1/customer/usage-events`

| 查询参数 | 规则 |
| --- | --- |
| `from_date` / `to_date` | `YYYY-MM-DD`，马来西亚日历日，含两端，**每个都必须在 `2020-01-01` 到 `2099-12-31` 之间**（含两端；在做任何日期运算之前判，所以 `0001-01-01`、`9999-12-31` 这类合法格式的极值得到 422，而不是换算溢出的 500）。没有 `event_id` 时**两个都必填**；`to_date` 不得早于 `from_date`；区间最多 92 天（`to_date − from_date ≤ 91`）。换算见「时间」 |
| `project_id` | 可选，项目 `public_id`。不是本客户的项目（不存在或属于别的客户）时结果为空页，不报错（与管理端同一语义，不构成存在性探测） |
| `provider` / `model` | 可选，与上报的原始字符串（`provider_code_raw` / `model_code_raw`）逐字节比较；格式同用量摄取的 `provider`、`model` 规则，不合规 422 |
| `conversation_id` | 可选，逐字节比较；格式 `^[A-Za-z0-9._:-]{1,128}$` |
| `event_id` | 可选，逐字节比较；格式同用量摄取的 `event_id`（UUIDv7 或 ULID 规范写法）。给出时日期区间可省略（给了仍照常生效）：这是从钱包流水跳过来的点查，走 `uq_usage_events_event_id` |
| `page_size` | 1–100，默认 20 |
| `cursor` | 可选，上一页响应的 `next_cursor` 原样传回；省略 = 从最新开始。解码失败、内容不合规 422 |

未知查询参数忽略；**不接受**任何租户、客户标识（传了也只会被忽略）。类型与范围错误（`page_size`、日期格式）在鉴权之前 422；组合规则（日期必填、先后、92 天、游标）在鉴权之后 422。所有 422 都是 `VALIDATION_ERROR`，文案只列字段名、不回显值。

成功 200，`data`：

```json
{
  "items": [
    {
      "id": "00000000-0000-4000-8000-000000000002",
      "event_id": "00000000-0000-7000-8000-000000000003",
      "request_id": "req_0001",
      "conversation_id": "conv_0001",
      "feature": "crm.contact_summary",
      "project_id": "00000000-0000-4000-8000-000000000001",
      "project_name": "Customer service bot",
      "provider": "anthropic",
      "model": "claude-sonnet-5-5",
      "usage_type": "llm_tokens",
      "input_tokens": 1200,
      "output_tokens": 340,
      "cache_creation_input_tokens": 0,
      "cache_read_input_tokens": 0,
      "quantity": null,
      "unit": "token",
      "charged_amount": "0.04120000",
      "occurred_at": "2026-10-18T08:30:00.123456"
    }
  ],
  "page_size": 20,
  "next_cursor": "MjAyNi0xMC0xOFQwODozMDowMC4xMjM0NTZ8MDAwMDAwMDAtMDAwMC00MDAwLTgwMDAtMDAwMDAwMDAwMDAy"
}
```

| 字段 | 说明 |
| --- | --- |
| `id` | 用量事件 `public_id` |
| `event_id` / `request_id` / `conversation_id` / `feature` | 集成方上报的原值（AI 模块代为上报的事件 `conversation_id` 为 `null`、`feature` 为功能标签）。都是标识，不是内容（INV-9） |
| `project_id` / `project_name` | 该事件所属项目的 `public_id` 与 `name`（JOIN `projects`，按 `project_id` 主键点查） |
| `provider` / `model` | 上报的原始字符串（与管理端列表、筛选同一口径；客户看到的就是自己上报的值） |
| `usage_type` | 计量类型 `code`（JOIN `usage_meter_types`，主键点查） |
| 四个 token 字段 / `quantity` / `unit` | 原值；`LLM_TOKEN_FIELDS` 形态 token 非空、`quantity` 为 `null`，`QUANTITY` 形态相反（模型的 CHECK 保证）。`quantity` 是 8 位小数字符串（`money_text`，与管理端同一格式） |
| `charged_amount` | `billable_cost`：本次请求向客户收的金额，8 位小数字符串，**含税**（ADR-0008：对客户展示的金额一律含税）。`PREPAID` 时等于钱包流水同一 `event_id` 那一行 `amount` 的绝对值；为 0 的事件不写账本、钱包流水里没有它；内部计量租户恒为 `"0.00000000"` |
| `occurred_at` | 调用发生时刻，UTC 不带时区，保留微秒（`docs/api.md`「时间」的既有例外）；前端换成马来西亚时间显示 |
| `next_cursor` | 还有更旧的行时为不透明字符串，否则 `null`。内容是本页最后一行的 `occurred_at` 与 `id`（base64url 编码），只能原样传回；客户端不得解析，后端以后可以改编码。多取的第 `page_size + 1` 行只用来判断有没有下一页 |

**刻意不返回**（白名单之外一律不返回，测试逐条断言不出现）：`status`（恒为 `PROCESSED`）、`error_code`、`error_message`、`received_at`、`processed_at`、`schema_version`、`payload_fingerprint`、`provider_id` / `model_id`、`estimated_provider_cost_myr`、`provider_source_currency`、`provider_source_cost`、`fx_rate_applied`、`reference_customer_price`、`billing_mode_snapshot`、价格版本 / 定价规则 / 汇率版本引用、`wallet_transaction_id`、`integration_credential_id`、认领字段、冲突记录（含 `api_key`）、任何内部 id、毛利。

### 查询

```sql
SELECT ue.public_id, ue.event_id, ue.request_id, ue.conversation_id, ue.feature,
       p.public_id AS project_public_id, p.name AS project_name,
       ue.provider_code_raw, ue.model_code_raw, mt.code AS usage_type,
       ue.input_tokens, ue.output_tokens, ue.cache_creation_input_tokens, ue.cache_read_input_tokens,
       ue.quantity, ue.unit, ue.billable_cost, ue.occurred_at
FROM usage_events ue
JOIN projects p ON p.id = ue.project_id
JOIN usage_meter_types mt ON mt.id = ue.usage_meter_type_id
WHERE ue.tenant_id = :tenant_id
  AND ue.status = 'PROCESSED'
  [AND ue.occurred_at >= :from_utc AND ue.occurred_at < :to_utc]
  [AND ue.project_id = :project_id]          -- 先在本租户内由项目 public_id 查出
  [AND ue.provider_code_raw = :provider] [AND ue.model_code_raw = :model]
  [AND ue.conversation_id = :conversation_id] [AND ue.event_id = :event_id]
  [AND (ue.occurred_at < :c_occurred_at OR (ue.occurred_at = :c_occurred_at AND ue.public_id < :c_public_id))]
ORDER BY ue.occurred_at DESC, ue.public_id DESC
LIMIT :page_size + 1
```

- 新函数放在 `app/repositories/usage_events.py`（例如 `list_processed_events_for_customer`），只选上面这些列；**不**复用管理端的 `EventListRow` 与 `_summary_fields`（它们读出成本列）。
- `JOIN projects` 只按主键点查取名称。项目筛选先用 `get_project_for_tenant(session, tenant_id, public_id)` 在本租户内查出内部 id，查不到（不存在或属于别的客户）直接返回空页；`ue.tenant_id = :tenant_id` 照常保留。
- **两个新索引**，都与 `ORDER BY occurred_at DESC, public_id DESC` 一致，取到 `page_size + 1` 条符合残余条件的行即停：
  - `ix_usage_events_tenant_occurred (tenant_id, occurred_at, public_id)`：只有日期区间（以及只带供应商 / 模型）时用。
  - `ix_usage_events_project_occurred (project_id, occurred_at, public_id)`：带项目时用。项目先在本租户内按 `public_id` 查出内部 id（`get_project_for_tenant`），查不到直接返回空页、不发用量查询；查到后以 `ue.project_id = :project_id AND ue.tenant_id = :tenant_id` 过滤。项目是 spec §68 的主筛选条件，一个客户常有一个新项目只占它总用量的一小部分，没有这个索引就要在租户的全部事件里残余过滤。
- `conversation_id` 筛选：优化器可以选现有 `ix_usage_events_conversation_id`（会话内行数少，取出后按租户与区间过滤再排序）或新索引；两者结果相同，测试不断言选择。
- `event_id` 筛选：`uq_usage_events_event_id` 点查，至多一行，再按租户与 `PROCESSED` 过滤；别的客户的 `event_id` 得到空页。
- **扫描量的硬上限是执行时间，不是 92 天**：供应商、模型（以及与项目组合时的其余条件）是残余过滤。客户按一个在区间内很少出现、甚至不存在的供应商筛选时，即使有索引也要扫过该区间内的全部事件才知道凑不满一页；一个有百万级事件的客户，92 天就可能是百万行。所以：
  - MySQL 上这条 SELECT 带优化器提示 `/*+ MAX_EXECUTION_TIME(2000) */`（常量 `CUSTOMER_USAGE_QUERY_TIMEOUT_MS = 2000`，放在 repository，不做成配置项；repository 函数接受一个可选的超时参数，只给测试用）。MySQL 对只读 SELECT 执行这个提示：超时即中止语句、返回错误 3024（`ER_QUERY_TIMEOUT`），已读的行不会部分返回。
  - repository 把错误 3024 转成 `QueryTooBroad`，接口返回 422 `QUERY_TOO_BROAD`（文案：缩小日期区间或换筛选条件），请求日志另记一条 `WARNING`（`customer_usage_query_timeout`，带 `request_id` 与客户 `public_id`，不带任何查询参数的值）。其他数据库错误照常 500 / 503。
  - SQLite（只在测试里用）不加提示。
  - 于是每个请求对共用数据库的占用至多约 2 秒，与数据量无关；92 天上限只是让常见查询落在这个预算之内，不再被当作扫描量的证明。
  - 2 秒的依据：管理端与门户的其他 GET 都在毫秒级；这个预算足够在新索引上扫过数十万行（§10 假设 3），又不会让一个页面请求卡到前端超时。

### 时间（营业时区，Kelvin 2026-10-23 定）

- 客户侧的日期一律按**马来西亚时间**理解：`Asia/Kuala_Lumpur`，固定 UTC+8（马来西亚没有夏令时），复用 `app/core/fx_source.py` 的 `KUALA_LUMPUR_OFFSET`，不引入 tzdata、不新增配置项。
- 换算：`from_utc = from_date 00:00 − 8 小时`，`to_utc = (to_date + 1 天) 00:00 − 8 小时`，按 `occurred_at` 取 `[from_utc, to_utc)`。例：`from_date = to_date = 2026-10-01` → `[2026-09-30 16:00:00, 2026-10-01 16:00:00)` UTC。
- 接口返回的时间仍是 UTC（`docs/api.md`「时间」不变），前端显示时 +8 小时并标注 `MYT`。
- 这是客户侧第一个按营业日理解的接口；以后「本月用量」、对账单月份边界沿用同一换算（写进 `docs/REVIEW-LOG.md`「已达成的约定」，见 §8）。
- 排序与游标只用 `occurred_at`（集成方报告的调用时刻），不用 `received_at` / `processed_at`：客户关心的是调用何时发生。晚到的事件（调用发生后几天才上报或才计费）会出现在它发生的那一天，所以同一个查询隔一段时间再看可能多出旧日期的行；这是预期行为（§4）。

### 金额

- 接口：只做 `Decimal` → 8 位小数字符串的格式化（`money_text`），不做任何运算、不换算、不舍入；`charged_amount` 是持久化的计费额原值。不在本接口里汇总金额。
- 前端显示（spec §80「Detailed usage may display up to 6 decimals」「Display rounding never changes persisted financial values」）：用量页的 `charged_amount` 显示为**最多 6 位小数**：对十进制字符串做 `ROUND_HALF_UP` 到 6 位，再去掉末尾多余的 0、至少留 2 位、整数部分加千分位。只做字符串运算，不经 `Number` / `parseFloat` / `Intl.NumberFormat`。例：`"0.04123450"` → `0.041235`，`"0.00000049"` → `0.00`，`"12.50000000"` → `12.50`，`"0.00000050"` → `0.000001`。
- 这个舍入函数是 `frontend/src/components/MoneyText.tsx` 里新增的一个导出（例如 `formatAmountRounded(amount, places)`），既有的 `formatAmount` / `MoneyText`（管理端用，不舍入）不改。
- `quantity` 不是金额，按既有的 `DecimalText` 原样显示。
- 详情里不另外显示 8 位原值：spec §80 说明细最多 6 位；原值在接口里，以后的导出会给出。

### 错误（两个接口相同，接口一没有 422）

401 `TOKEN_INVALID`（无 / 坏 / 过期访问令牌，前端照常走刷新）、403 `CUSTOMER_REQUIRED`（ADMIN 令牌、没有 `tenant_id` 的 CUSTOMER、非 `ACTIVE` 用户）、422 `VALIDATION_ERROR`、422 `QUERY_TOO_BROAD`（仅接口二：超过执行时间预算）、500、503（数据库不可用，沿用现有处理）。没有 404（租户来自会话，项目与事件筛不到就是空页）；没有 403 `ACCOUNT_READ_ONLY`（两个接口都是读，用 `require_customer`）。

### 数据库

- 新迁移（`0025_customer_portal_accounts` 之后的下一个编号，登记时定）：只加两个索引 `ix_usage_events_tenant_occurred (tenant_id, occurred_at, public_id)` 与 `ix_usage_events_project_occurred (project_id, occurred_at, public_id)`；downgrade 删它们。不改列、不改约束、不改触发器、不动数据。
- **upgrade 可以安全重跑**（MySQL 的 DDL 不参与事务，`ALTER TABLE` 与写 `alembic_version` 之间有失败窗口，仓库 0023 迁移文件头记录过同一窗口）：
  1. 先用 SQLAlchemy inspector 读 `usage_events` 现有的索引，对两个目标名各判一次：
     - 不存在 → 待建；
     - 存在、非唯一、列恰好按序是设计的三列 → 已建好，跳过；
     - 存在但列或唯一性不同 → 抛错，消息写明哪个索引形状不对、应如何处理，**不发任何 DDL**。
  2. 判完之后，待建的（0、1 或 2 个）在 MySQL 上合成**一条** `ALTER TABLE usage_events ADD INDEX …[, ADD INDEX …]`（`op.execute`）发出；一个都不缺时不发 DDL。MySQL 8 的 InnoDB DDL 是原子的：这一条失败（锁等待超时、进程被杀）时整条回滚，不会只建成其中一个。SQLite（只在测试里用）用 `op.create_index`，DDL 在 Alembic 的迁移事务里。
  3. 于是三个失败窗口都有确定的结局：
     - DDL 失败 → 两个都不存在、版本不变 → 原样重跑；
     - DDL 成功、写版本之前进程退出 → 两个都在、版本不变 → 原样重跑，第 1 步判为「已建好」、不发 DDL，只把版本前进；
     - 有人手工建过同名但形状不同的索引 → 重跑在第 1 步失败、不发 DDL、版本不变 → 按报错信息 `DROP INDEX` 那个索引后重新部署。
  4. downgrade 对存在的那几个发一条 `ALTER TABLE usage_events DROP INDEX …[, DROP INDEX …]`，不存在的跳过，同样可以重跑。
  5. 迁移文件头按仓库惯例写「失败处理」，内容即第 3 条。
- 模型 `UsageEvent.__table_args__` 同步加这两个 `Index`；`tests/backend/test_migrations.py` 的模型与迁移比对照常覆盖。

### 事务边界

只读：每个请求一个会话、一条 SELECT（`require_customer` 读用户行那一次除外），不加锁（不用 `FOR UPDATE` / `FOR SHARE`），不提交任何写。

### 前端（第三、四个任务，不在闸门范围内，写在这里只为说明接口够用）

- 用量页 `/portal/usage`（门户导航加一项）：日期区间（默认本月 1 日到今天，马来西亚时间；日期选择限制在 2020-01-01 到 2099-12-31）、项目下拉（接口一）、供应商、模型、`conversation_id` 输入；表格列 = 时间（MYT）、项目、供应商 / 模型、类型、输入 / 输出 token、计费额（最多 6 位小数，见「金额」）；「Older」用 `next_cursor` 翻页、「Newest」回第一页；点一行展开详情（§69 的全部字段，含 `event_id`、`request_id`、`conversation_id`、缓存 token、`quantity` / `unit`）。查询串带 `event_id` 时只按它查。`QUERY_TOO_BROAD` 显示「请缩小日期区间或换筛选条件」，不是通用错误。
- 钱包页（AIH-TASK-100 的流水表）：AI 用量行的 `event_id` 变成到 `/portal/usage?event_id=…` 的链接。
- 浏览器验收：用验收夹具的客户账号各加一个只读步骤。

## 3. 不变量影响矩阵

| 不变量 | 是否触碰 | 可能怎样被破坏 | 设计控制 | 验证方式 |
| --- | --- | --- | --- | --- |
| INV-1 中心故障不中断 AI | 否（间接） | 只读客户接口，不在 AI 请求或用量摄取路径上；但与计费、AI 模块共用数据库，一个过宽的查询长时间扫表会拖慢它们 | 每页行数有上限、走索引、日期区间封顶，**每条查询最多执行 2 秒**（§2「查询」） | 测试：MySQL 上超时真的中止并返回 `QUERY_TOO_BROAD`（§7「扫描上限」） |
| INV-2 事件不重复扣费 | 否 | 不写账本、不改用量事件 | 无写路径 | 测试：调用前后 `usage_events`（行数与 `status`）、`wallet_transactions`、`wallets`、`audit_logs`、`domain_outbox` 不变 |
| INV-3 支付不重复入账 | 否 | 同上 | 同上 | 同上 |
| INV-4 余额只经账本变动 | 否 | 同上 | 同上 | 同上 |
| INV-5 历史账本不可变 | 否 | 不读账本；`charged_amount` 来自用量事件的 `billable_cost`（`PROCESSED` 后由迁移 0018 触发器保证不变） | — | 测试：`PREPAID` 事件的 `charged_amount` 等于同一 `event_id` 账本行 `amount` 的绝对值 |
| INV-6 事件保留版本引用 | 否（只读侧不暴露） | 把版本引用返回给客户等于泄露定价结构 | 不返回任何版本 / 规则引用 | 键集合逐字断言 |
| INV-7 客户不可见成本毛利 | **是** | ① 响应带出 `estimated_provider_cost_myr`、`provider_source_cost`、`fx_rate_applied`、`reference_customer_price`、毛利；② 复用管理端的查询或 schema，管理端加字段时客户端跟着出现；③ 内部计量租户的 `reference_customer_price`（假想售价）被当成计费额显示 | ① 新查询只 SELECT §2 列出的列；② 客户 schema 是新文件里的 18 键白名单模型，不继承管理端模型、不复用 `EventListRow`；③ `charged_amount` 只取 `billable_cost`（内部计量为 0） | 测试：成本、原币成本、汇率、参考价写成哨兵值，断言**原始响应文本**里都没有出现；键集合逐字断言恰好 18 个；内部计量租户的事件 `charged_amount = "0.00000000"` 且响应里没有参考价哨兵值 |
| INV-8 租户不可互访 | **是** | ① 从请求里取租户；② 漏掉租户过滤；③ 用别的客户的项目 id、`event_id`、`conversation_id` 或游标看到别人的行；④ 项目列表漏过滤 | ① 租户只来自 `CustomerPrincipal.tenant_id`，接口不定义任何租户参数；② 两条查询都 `WHERE tenant_id = :tenant_id`；③ 项目、`event_id`、`conversation_id` 只是附加条件，租户条件始终在；游标只用于 `<` 比较，不改变租户条件；④ `list_projects_for_tenant` 按租户 | 测试：客户 A、B 各有项目与事件，且共用同一个 `conversation_id` 与 `request_id` 值；A 用 B 的项目 id、B 的 `event_id`、B 的 `conversation_id`、B 的最后一页游标，带 `?customer_id=<B>&tenant_id=<B 的内部 id>`，都只得到 A 的行或空页；A 的项目列表不含 B 的项目 |
| INV-9 对话内容不入库 | 否 | 不新增存储；`request_id`、`conversation_id`、`feature` 是集成方给的标识（摄取时已按格式限制，不可能是内容） | — | — |
| INV-10 金额用 Decimal | 是 | 序列化或前端显示经 float 丢精度；显示舍入改了持久值 | 接口 `money_text` 原值；前端的 6 位舍入只做字符串运算、只在显示层 | 测试：`billable_cost` 为 `0.00000001` 与 `999999999999.99999999` 原样往返；前端测试覆盖舍入进位（`"0.99999950"` → `1.00`、`"999999999999.99999950"` → `1,000,000,000,000.00`）、舍去、负号与非十进制字符串原样返回 |
| INV-11 event_id 至多一次财务效果 | 否 | 不写；`event_id` 只读出与筛选 | — | — |
| INV-12 定稿对账单不可变 | 否 | 对账单还不存在 | — | — |
| INV-13 状态与事件原子提交 | 否 | 没有状态变化与出站事件；只读 `PROCESSED` 行，它的计费额与账本行在计费事务里同时提交 | — | — |
| INV-14 队列丢失不毁持久工作 | 否 | 不用 Redis / Celery | — | — |

## 4. 状态与并发

- **没有状态变化**，所以没有跃迁表。只读 `status = 'PROCESSED'` 的行；`PROCESSED` 是终态（迁移 0018 的触发器），一行一旦出现在结果里，它的字段不会再变。
- **与计费并发**：计费 worker 把事件从 `PROCESSING` 改成 `PROCESSED` 与写账本在同一事务提交。每页是一条 SELECT 的一致快照，不会看到「已 `PROCESSED` 但账本还没写」的组合。
- **翻页与新数据**：排序键是 `(occurred_at, public_id)`。翻页之间新变成 `PROCESSED` 的事件：
  - 如果它的 `occurred_at` 比当前游标新：只出现在「最新」那一端，往旧翻**不重不漏**已存在的行。
  - 如果它的 `occurred_at` 比当前游标旧（晚到或晚计费的事件）：会出现在后面的页里，**不会重复**已返回的行（游标严格小于），也不会漏掉已存在的行。
  - 所以往旧翻的结果是「翻页过程中某个时刻的全集」的子序列，不会出现同一行两次。这一点由排序键的唯一性（`public_id` 唯一）保证。
- **不加锁**：不阻塞计费与摄取，计费与摄取也不阻塞本查询（一致性读）。
- 唯一性、幂等键、相同 ID 不同载荷、重放：不适用（只读，GET 天然可重复）。
- 重试：失败由前端重新请求，后端不重试。

## 5. 失败与恢复矩阵

| 失败点 | 对外结果 | 数据最终状态 | 是否可重试 | 恢复来源 | 告警 |
| --- | --- | --- | --- | --- | --- |
| 校验失败（类型、范围、日期必填与先后、92 天、游标、格式） | 422 `VALIDATION_ERROR`，不回显值 | 不变（没有写） | 修正参数后重试 | — | 无 |
| 未登录 / 令牌过期 | 401 `TOKEN_INVALID`，前端走刷新 | 不变 | 是（刷新后） | 刷新令牌 | 无 |
| ADMIN、无租户 CUSTOMER、账号非 `ACTIVE` | 403 `CUSTOMER_REQUIRED` | 不变 | 否 | 管理员修账号 | 无 |
| 租户 `DISABLED` / `CLOSED` | 200 照常返回（只读契约） | 不变 | — | — | 无 |
| 项目 / `event_id` / `conversation_id` 不属于本客户 | 200 空页 | 不变 | — | — | 无 |
| DB 不可用 / 查询失败 | 503 / 500（沿用全局处理，带 `request_id`） | 不变 | 是 | 数据库恢复后重查 | 现有 readyz 巡检 |
| 查询超过 2 秒执行预算（稀少条件、超大客户） | 422 `QUERY_TOO_BROAD` | 不变（语句被数据库中止，没有部分结果） | 是，缩小日期区间或换条件后 | — | `WARNING` 日志 `customer_usage_query_timeout`；不新增告警维度（客户可自行重试） |
| Redis/Celery 丢失 | 无影响 | 不变 | — | — | 无 |
| 外部服务超时 | 不适用（不调外部服务） | — | — | — | — |
| 重复请求 | 同一参数、期间没有新计费的事件时结果相同 | 不变 | 是 | — | 无 |
| 迁移的 `ALTER TABLE` 失败（锁等待超时、进程被杀） | 部署失败，旧容器继续服务（`deploy.sh` 先迁移再换镜像） | 原子 DDL 整条回滚：两个索引都不存在，`alembic_version` 不变 | 是，原样重新部署 | 现有部署流程 | 部署通知 |
| `ALTER TABLE` 成功、写 `alembic_version` 之前进程退出 | 部署失败，旧容器继续服务 | 两个索引都在，`alembic_version` 仍是上一版 | 是，原样重新部署：upgrade 判两个都已建好，不发 DDL，只前进版本 | 现有部署流程 | 部署通知 |
| 库里已有同名但形状不同的索引（手工建过） | 部署失败，报错写明是哪个索引 | 不发任何 DDL；`alembic_version` 不变 | 按报错 `DROP INDEX` 那个索引后重新部署 | 迁移文件头的「失败处理」 | 部署通知 |
| 前端遇到未知 `usage_type` / `unit` | 显示原值，不崩 | 不变 | — | — | 无 |

## 6. 数据与安全边界

- **租户过滤**：在 repository 查询里，`tenant_id` 只来自 `require_customer` 返回的 `CustomerPrincipal.tenant_id`。两个接口都不定义任何租户、客户标识参数（spec §90）。项目、`event_id`、`conversation_id` 筛选都是租户条件之上的附加条件。
- **鉴权主体**：只有 CUSTOMER（`ACTIVE`、`tenant_id` 非空）。处理函数第一条语句显式调用 `require_customer`；AIH-TASK-093 的 `/api/v1/customer/*` 路由枚举用例覆盖两个新路由。ADMIN 不能调（管理员用管理端用量接口）。
- **客户侧禁止返回的字段**：见 §2「刻意不返回」。特别是冲突记录里的集成方 `api_key`、项目的 `status_webhook_url`。
- **日志、审计、异常**：沿用请求日志（方法、路径模板、状态、耗时、`request_id`）；不记录查询参数的值（`event_id`、`conversation_id`、日期）、金额、项目名。读操作不写审计。422 文案只列字段名。
- **密钥**：不涉及。
- **prompt / response 与客户数据**：不处理 prompt / response（`usage_events` 本来就没有内容列，INV-9）。返回的都是该客户自己上报的标识与用量、以及它自己的计费额。项目名是管理员替客户起的，属于客户自己的数据。
- **数据保留**：不新增存储；索引随表保留。

## 7. 测试证据计划

后端（SQLite + MySQL 参数化，沿用 AIH-TASK-093 的客户登录夹具写法；SQL 文本断言不写死方言字面量；示例 uuid 用全零占位值）：

| 风险/需求 | 测试层级 | 场景 | 预期结果 |
| --- | --- | --- | --- |
| 正常路径（项目列表） | API | 客户有 3 个项目，另一客户有 2 个 | 恰好本客户 3 项、按创建先后、每项恰好 `id`、`name` 两个键 |
| 正常路径（用量） | API | 客户 5 条 `PROCESSED` 事件（两个项目、token 形态与 quantity 形态各有、`occurred_at` 各不同），`page_size=2` 连翻三页 | 依次 2 / 2 / 1 条，按 `occurred_at` 倒序；`next_cursor` 前两页非空、最后一页 `null`；每项恰好 18 个键，字段逐字等于库里的值，`project_name`、`usage_type` 正确 |
| 只看已计费 | API | 同一客户另有 `RECEIVED`、`PROCESSING`、`PRICING_ERROR`、`MODEL_UNKNOWN`、`FAILED_RETRYABLE`、`FAILED_FINAL` 事件各一条，日期在区间内 | 都不出现 |
| 同一时刻的平局 | API | 3 条事件 `occurred_at` 完全相同（微秒相同），`page_size=1` 翻页 | 按 `public_id` 倒序逐条出现，不重不漏 |
| 日期换算 | API | 事件在 UTC `2026-09-30 15:59:59.999999`、`16:00:00`、`2026-10-01 15:59:59.999999`、`16:00:00` | `from_date = to_date = 2026-10-01` 只返回中间两条 |
| 日期规则 | API | 没有 `event_id` 时缺 `from_date`、缺 `to_date`；`to_date < from_date`；区间 92 天（`to − from = 91`）与 93 天；日期格式错 | 缺、倒置、93 天、格式错 → 422；92 天 → 200 |
| 日期极值 | API | `from_date` 或 `to_date` 为 `0001-01-01`、`9999-12-31`、`2019-12-31`、`2100-01-01`（各自配成区间不超过 92 天的另一端），以及只带 `event_id` 时附带这些日期 | 一律 422 `VALIDATION_ERROR`，不是 500；`2020-01-01` 起与 `2099-12-31` 止的区间 → 200 |
| 筛选 | API | 按项目、`provider`、`model`、`conversation_id` 各筛一次，及组合 | 只出现匹配的行；不存在的项目 id → 空页 |
| `event_id` 点查 | API | 只带 `event_id`（不带日期） | 恰好那一条；别的客户的 `event_id` → 空页；未计费事件的 `event_id` → 空页；`event_id` 加上不含它的日期区间 → 空页 |
| 游标 | API | 篡改的游标（非 base64、缺字段、非法时间、非法 uuid） | 422 `VALIDATION_ERROR`，文案不回显值 |
| 边界值 | API | `page_size` = 0 / 101 / 非整数；`provider`、`model`、`conversation_id`、`event_id` 格式不合规 | 422；`page_size=100` 为 200 |
| 并发 | MySQL | 取第一页后，另一事务把一条 `occurred_at` 更旧的事件改成 `PROCESSED`（模拟晚计费）、再插入一条更新的 `PROCESSED`，然后用 `next_cursor` 翻完 | 翻页全程没有任何行出现两次；更旧的那条出现在后面的页里 |
| 幂等重放 | API | 同一请求连发两次 | 两次响应相同 |
| 事务中途失败 | API | repository 查询抛数据库异常 | 500 / 503 带 `request_id`；各表行数不变 |
| Redis/Celery 丢失 | — | 不使用 | 不适用 |
| 租户越权 | API | 客户 A、B 共用同一个 `conversation_id` 与 `request_id` 值；A 用 B 的项目 id、B 的 `event_id`、B 的 `conversation_id`、B 的游标，并带 `?customer_id=<B 的 public_id>&tenant_id=<B 的内部 id>` | 只得到 A 的行或空页；A 的任何响应里不出现 B 的事件 `id`、项目名 |
| 鉴权 | API | 无令牌、坏令牌、ADMIN 令牌、门户账号被停用后的旧令牌 | 401 `TOKEN_INVALID` / 401 / 403 `CUSTOMER_REQUIRED` / 403，且不发用量查询 |
| 只读契约 | API | 租户为 `PENDING_ACTIVATION` / `ENABLED` / `DISABLED` / `CLOSED` | 两个接口都 200 |
| 路由守卫 | integration | AIH-TASK-093 的 `/api/v1/customer/*` 枚举用例 | 两个新处理函数被枚举到且显式调用了 `require_customer` |
| 无成本毛利 | API | 事件的 `estimated_provider_cost_myr`、`provider_source_cost`、`fx_rate_applied`、`reference_customer_price` 写成哨兵值；项目的 `status_webhook_url` 与 `description` 写成哨兵串；事件有冲突记录（`api_key` 哨兵串） | 两个接口的**原始响应文本**里不出现任何哨兵值 |
| 内部计量租户 | API | `INTERNAL_METERED_ONLY` 的 `PROCESSED` 事件，`reference_customer_price` 为哨兵值 | `charged_amount = "0.00000000"`；响应里没有哨兵值 |
| 与钱包对得上 | API | `PREPAID` 事件计费 RM0.0412 | `charged_amount` 等于同一 `event_id` 的账本行 `amount` 的绝对值 |
| 金额精度 | API（SQLite + MySQL） | `billable_cost` 为 `0.00000001`、`999999999999.99999999`；`quantity` 为 `0.00000001` | 字符串逐字相等，8 位小数 |
| 无写入 | API | 每个用例前后 | `usage_events`（行数与状态）、`wallet_transactions`、`wallets`、`audit_logs`、`domain_outbox` 不变 |
| 扫描上限 | MySQL | 一个客户写入 30 000 条区间内的 `PROCESSED` 事件，按一个不存在的 `provider` 筛选：① 用测试专用的 1 毫秒超时调 repository；② 用默认 2 秒调 | ① 抛 `QueryTooBroad`，接口层对应 422 `QUERY_TOO_BROAD`；② 返回空页。另断言 MySQL 上编译出的 SQL 带 `MAX_EXECUTION_TIME` 提示、SQLite 上不带（归一后比较） |
| 超时映射 | API | repository 抛 `QueryTooBroad` | 422 `QUERY_TOO_BROAD`，带 `request_id`，文案不含任何查询参数的值；有一条 `customer_usage_query_timeout` 日志 |
| 项目先查 | API | 项目 id 不属于本客户 | 空页，且不发用量查询 |
| 索引存在且可用 | 迁移（MySQL） | 升级到 head 后 `SHOW INDEX`；对只带日期区间、带项目两种查询 `EXPLAIN` | 两个新索引存在、列顺序分别为 `tenant_id, occurred_at, public_id` 与 `project_id, occurred_at, public_id`；各自查询的 `possible_keys` 含对应索引（不断言最终选择） |
| 迁移可重跑 | 迁移（MySQL，另有 SQLite 版的前两条） | 均先升级到上一版，再：① 手工把两个新索引按设计形状建好（模拟「DDL 成功、版本未前进」），upgrade；② 只手工建好第一个，upgrade；③ 手工建一个与第二个同名、列不同的索引，upgrade；④ 正常 upgrade 后 downgrade 两次（第二次前手工删掉其中一个） | ① 成功、不发任何 `ALTER TABLE`（捕获执行的语句断言）、版本前进、两个索引形状不变；② 成功、只发一条只含第二个 `ADD INDEX` 的 `ALTER TABLE`；③ 失败、报错点名第二个索引、第一个新索引**不存在**、版本仍是上一版，删掉手工索引后 upgrade 成功；④ downgrade 只删存在的那个、不报错。正常路径断言 MySQL 上 upgrade 只发一条 `ALTER TABLE usage_events` |
| 迁移往返 | 迁移（SQLite + MySQL） | upgrade → downgrade → upgrade | 两个索引随之建、删、再建；其他表结构不变；模型与迁移比对通过 |

前端（Vitest）：默认日期是马来西亚时间的本月 1 日到今天（用固定的「现在」测 UTC 16:00 前后两侧）；时间按 +8 显示；计费额按 6 位 `ROUND_HALF_UP` 显示（INV-10 一行的用例），`formatAmount` 既有用例不变；`QUERY_TOO_BROAD` 显示专门文案；筛选与翻页的查询串；查询串带 `event_id` 时只按它查；详情展开；未知 `usage_type` 显示原值；页面不请求 `/api/v1/admin/` 路径。

## 8. 迁移与上线

- **数据迁移步骤**：无数据迁移；只在一条原子 `ALTER TABLE` 里加两个二级索引，upgrade 先检查、可以安全重跑（§2「数据库」）。
- **锁表与性能影响**：MySQL 8 的 `ADD INDEX` 默认在线（INPLACE，不阻塞 DML）。生产上 `usage_events` 现在只有夹具与内部租户的少量行，构建瞬时完成。**写入代价**：每条用量事件插入时多维护两个二级索引条目（各含 `occurred_at`、`public_id` CHAR(36)，前缀分别是 `tenant_id` 与 `project_id`，外加隐含的主键）。这些列在插入后都不再更新，所以计费 worker 的状态更新不碰它们。相对 `usage_events` 现有的十几个索引，这是一个增量；`docs/perf-baseline.md` 第 10 节测的是计费吞吐（钱包行锁内逐事件记账），没有单独测插入，登记时不另立性能任务，上线后由现有请求与计费日志观察。
- **兼容窗口**：新接口是新增的，旧前端不调用；前端在后端部署之后才上（同一次合并同时部署 api 与 frontend，且前端任务排在接口任务之后）。
- **部署顺序与拆分**（批准后登记为四个任务，`depends_on` 串起来，每个都能单独过检查与 CI）：
  1. 数据库：迁移加两个索引 + 模型 `Index` + 迁移测试 + `docs/database-schema.md`（不加接口）。
  2. 接口：repository 新查询 + `app/schemas/customer_usage.py` + `app/api/customer.py` 两个路由 + 测试 + `docs/api.md`（依赖 1、AIH-TASK-093；排在 AIH-TASK-099 之后，避免两个任务同时改 `app/api/customer.py` 与 `docs/api.md`）。
  3. 前端用量页：API 封装、用量页、门户导航与路由、文案、测试、验收脚本一个只读步骤；勾 `docs/TODO.md` Phase 5 的「请求级明细」与「用量筛选」（依赖 2 与 AIH-TASK-100）。
  4. 前端钱包页链接：AIH-TASK-100 的流水表里 AI 用量行的 `event_id` 链到用量页（依赖 3 与 AIH-TASK-101）。
- **登记 PR 同时**在 `docs/REVIEW-LOG.md`「已达成的约定」记一行：客户侧日期按马来西亚时间（固定 UTC+8）理解，接口仍返回 UTC（Kelvin 2026-10-23）。
- **回滚或前滚方式**：接口与前端回滚到上一个镜像即可（无数据变化）；迁移 downgrade 只删两个索引。
- **部分部署时的行为**：只部署 1 → 多两个没人用的索引；只部署到 2 → 接口可用、页面没有入口；只部署到 3 → 用量页可用，钱包流水还不能点过去。都不影响现有功能。
- **监控指标和告警负责人**：不新增指标；现有请求日志可以看耗时，`customer_usage_query_timeout` 日志可以看超时次数；Kelvin 负责。

## 9. 方案取舍

| 方案 | 优点 | 风险 | 未采用原因 |
| --- | --- | --- | --- |
| **采用**：按 `(occurred_at, public_id)` 排序 + 不透明游标 + 两个新索引（租户 / 项目前缀）+ 日期区间 92 天上限 + 每条查询 2 秒执行预算 | 客户按调用时间看；日期与项目两种主筛选都是索引范围扫描；翻页不重复；游标不暴露内部 id；最坏情况对共用数据库的占用有硬上限 | 两个索引的写入代价（§8）；游标要编码 / 解码；极端查询得到 `QUERY_TOO_BROAD` 而不是结果 | — |
| 备选 A：沿用管理端的 page / total、按 `id` 倒序 | 前端可复用管理端翻页 | spec §108 对请求级用量表要求游标；`total` 每次 COUNT 全部匹配行；`OFFSET` 要先扫过前面的行；新事件插入时 offset 翻页会重复 | 与 #351、#393 的理由相同 |
| 备选 B：按 `id` 倒序 + `id` 当游标，不加索引 | 不需要迁移（`ix_usage_events_tenant_id` 隐含主键） | `id` 是全局自增，暴露给客户等于暴露全平台的事件量；按 `id` 排序是「入库顺序」，与客户关心的调用时间不一致（批量补报的旧事件排在最前）；日期区间要残余过滤 | 排序语义不对，且泄露平台规模 |
| 备选 C：游标用 `(occurred_at, event_id)`、索引用 `event_id` | `event_id` 本来就返回给客户 | `event_id` 是 `VARCHAR(64)`，比 `public_id` 的 36 字符更长；与列表项的 `id` 不是同一个键，排序次序不直观 | `public_id` 更短，且就是列表项的 `id` |
| 备选 D：两个显式游标参数（`before_occurred_at`、`before_id`） | 透明、好调试 | `occurred_at` 带微秒、不带时区，作为查询参数要另定格式规则，与「时间参数必须带时区」的现有约定冲突 | 不透明游标把格式留在后端内部 |
| 备选 E：再加 `(tenant_id, provider_code_raw, model_code_raw, occurred_at, public_id)`，让供应商 / 模型也走等值前缀 | 稀少的供应商 / 模型也快 | 两个原始字符串最长 64 + 128 字符，索引条目大；每条事件插入多一次维护；组合条件（项目 + 供应商）仍有残余过滤，照样需要时间预算兜底 | 执行时间预算已给最坏情况封顶；`customer_usage_query_timeout` 日志显示供应商 / 模型筛选真的常超时时，再按证据加 |
| 备选 I：不设时间预算，只靠索引与 92 天 | 实现简单，没有 `QUERY_TOO_BROAD` | 稀少或不存在的残余条件会扫满区间；百万级事件的客户一次请求可能占用数据库数秒到更久（Codex 审查 v1 指出） | 无法证明扫描量可控 |
| 备选 J：计费额在前端原样显示 8 位小数 | 与接口、管理端的 `MoneyText` 一致 | 与 spec §80「Detailed usage may display up to 6 decimals」冲突（Codex 审查 v1 指出） | spec 明确上限；显示舍入不改持久值 |
| 备选 F：`provider` / `model` 返回并筛选目录里的规范 code（解析后的 `provider_id` / `model_id`） | 别名统一成一个 code | 要 JOIN 目录表；客户看到的不是自己上报的值；管理端用的是原始字符串 | 与管理端同一口径，客户按自己上报的值筛 |
| 备选 G：日期参数沿用管理端的 RFC 3339 时刻区间 | 不需要时区规则 | 客户要自己换算「10 月 1 日」对应哪两个 UTC 时刻，前端也得换算；以后「本月」边界还是要定 | Kelvin 定马来西亚日历日（spec §109） |
| 备选 H：未计费的事件也显示（带对客状态） | 客户能看到「已上报、待计费」 | 定价失败是内部问题，暴露给客户还要定文案与解释；这些行没有计费额，与钱包对不上 | Kelvin 定只看已计费 |

## 10. 未决问题与假设

- **未决问题**：无阻断项。
- **已拍板的范围取舍**（Kelvin 2026-10-23）：先做请求级明细，会话摘要另开闸门；只看 `PROCESSED`；新增只返回 `id`、`name` 的客户项目列表；客户侧日期按马来西亚时间。
- **尚未验证的假设**：
  1. AIH-TASK-093 按 #368 v12 交付 `require_customer`、`CustomerPrincipal.tenant_id`、`app/api/customer.py` 与路由枚举用例。若形状不同，接口任务停下说明，不自行补依赖。
  2. MySQL 优化器对「租户 + 日期区间 + `ORDER BY occurred_at DESC, public_id DESC LIMIT`」会选新索引。依据：等值前缀 + 有序范围 + 与排序一致的次序列，是最典型的可用形状；测试只断言 `possible_keys`。若生产上没选中，后果是慢，不是错，可加索引提示另修。
  3. 一个客户的项目数小（不分页返回全部）。依据：项目只由管理员创建；生产上现有客户各只有一两个项目（内部租户 2 个）。超过 100 个时再加分页，属于接口兼容的扩展。
  4. 2 秒执行预算对常见查询（只带日期、或带项目）足够：它们在新索引上取到 `page_size + 1` 行即停，扫描行数与页大小同量级。只有稀少的残余条件才会接近预算；那时返回 `QUERY_TOO_BROAD` 是设计行为，不是故障。
- **需要谁拍板**：无新增。
- **如果假设错误，具体后果**：假设 1 错 → 接口任务开工时停下；假设 2 错 → 常见查询变慢，最坏得到 `QUERY_TOO_BROAD`，不影响正确性；假设 3 错 → 项目下拉的响应变大，不影响正确性；假设 4 错 → 常见查询也超时，客户看不到数据（不会看到错数据），按日志证据调预算或加索引，需要改设计。

## 11. 审查与版本绑定

审查方要回答的五个问题常驻在 [scripts/review_checklist.md](../../scripts/review_checklist.md) 的「设计审查」一节，此处不重复。

**版本绑定规则**（规则的出处是 [WORKFLOW §3](../../docs/WORKFLOW.md)；这里保留一份是因为审查材料里只有本 Issue 正文，两处改一起改）：

- Codex 的批准必须写成 `APPROVED: design v<N>`，`<N>` 取本 Issue 顶部的「设计版本」
- **设计版本一变，之前的 APPROVE 自动作废**，必须重新过闸门
- 什么算「实质修改」：改了契约、表结构、事务边界、状态机、失败语义、不变量控制。改错别字不算
- 实质修改时：把顶部版本 +1，在下方追加一条变更说明，状态退回 `READY_FOR_REVIEW`

### 设计闸门判定

同时满足才算通过：

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
| v1 | 2026-10-23 | 初稿 | — |
| v2 | 2026-10-23 | ① 日期参数限定在 2020-01-01 到 2099-12-31，在日期运算之前判，极值 422；② 每条用量查询带 MySQL `MAX_EXECUTION_TIME(2000)`，超时 422 `QUERY_TOO_BROAD` 并记日志；新增项目前缀索引 `ix_usage_events_project_occurred`，项目先在租户内查出内部 id；③ 前端计费额按 spec §80 最多显示 6 位小数（字符串 `ROUND_HALF_UP`），接口仍返回 8 位原值；§3、§5、§7、§8、§9、§10 相应补充 | Codex 审查 v1 的三个阻断项：极端日期会 500；稀少条件的扫描量无上限；8 位显示与 spec §80 冲突 |
| v3 | 2026-10-23 | 两个索引改为 MySQL 上**一条**原子 `ALTER TABLE` 同时加（downgrade 同样一条）；§2「数据库」写明失败后两个都不存在、无需清场，以及手工建过同名索引时的处理；§5 补两行迁移失败；§7 补「迁移原子性」测试 | Codex 审查 v2 的阻断项：逐个 `create_index` 时第二个失败会留下第一个，重跑撞「索引已存在」 |
| v4 | 2026-10-23 | upgrade 先用 inspector 检查两个目标索引（不存在 / 形状正确 / 形状不对），只为缺的发一条原子 `ALTER TABLE`，形状不对时不发 DDL 并报错；downgrade 只删存在的；§2「数据库」列出三个失败窗口各自的结局，§5 拆成三行，§7「迁移原子性」改为「迁移可重跑」四个场景 | Codex 审查 v3 的阻断项：`ALTER TABLE` 成功而 `alembic_version` 未前进时，v3 的「原样重跑」会撞同名索引 |

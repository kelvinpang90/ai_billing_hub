# AIH-TASK-113 .. 118 设计：客户用量与钱包流水 CSV 导出（已批准 v10）

> **来源**：设计闸门 Issue #435。本文件是 `APPROVED: design v10` 那一版正文的**逐字副本**（审查者为 Codex；送审时状态行为 `READY_FOR_REVIEW`，批准后 Issue 上只把状态行改成了 `APPROVED: design v10`）。
> 放进仓库，是因为 OpenClaw Worker 在沙箱里不联网、读不到 GitHub Issue。
> **实现以本文件为准**；与 Issue 不一致时，以 Issue #435 上被批准的 v10 为准。设计要改，就回到 Issue 升版本、重新过闸门，不要直接改本文件。
> **前置**：设计闸门 #398（[AIH-TASK-103-customer-usage.md](AIH-TASK-103-customer-usage.md)）的客户用量查询、18 键白名单、2 秒执行预算与 `QUERY_TOO_BROAD`；设计闸门 #393（[AIH-TASK-099-customer-wallet.md](AIH-TASK-099-customer-wallet.md)）的客户流水 7 键白名单。
> §8 的五步登记为六个实现任务：第 1 步 `AIH-TASK-113`（迁移）；第 2 步按登记前预审的拆分建议（约 2 000 行）拆成 `AIH-TASK-114`（CSV 模块、导出租约中间件、代理不缓冲配置）与 `AIH-TASK-115`（用量导出接口，依赖 114）；第 3 步 `AIH-TASK-116`（钱包导出，依赖 113、115）；第 4 步 `AIH-TASK-117`（用量页按钮，依赖 115）；第 5 步 `AIH-TASK-118`（钱包页导出区，依赖 116、117）。

---

## 0. 准入判定

| 任务涉及 | 要填的范围 |
| --- | --- |
| 钱包 / 账本 / 定价 / 汇率 / 支付 / 幂等 / 状态机 | **全部章节** |
| 用量摄取 / 认证与会话（集成侧与平台侧）/ Webhook | §1–§7（**§6 不可省**） |
| 前端 / 文档 / CI / 脚本 | 不走本闸门 |
| 既不碰钱、也不碰用量摄取 / 认证与会话 / Webhook 的后端改动 | 不走本闸门 |

本任务档位：`全部`。只读、不写任何业务表，但把客户的计费额与钱包流水整段导出成文件交给客户（INV-7 的白名单第一次以文件形式出系统），并给 `wallet_transactions` 加一个索引（改数据库结构）。只读带来的「不触碰」在 §3 逐条说明理由。

---

状态：`待审`（`READY_FOR_REVIEW`，v10）
负责人：Claude
**设计版本**：`v10`
对应需求：spec §73「Export Functionality」（V1 支持 CSV 与 Excel；至少请求级用量、会话摘要、钱包流水、月度用量汇总；「Customer export MUST exclude internal costs」；大导出必要时异步）、§128（Phase 5「customer usage exports」）、§69（请求级字段）、§70（客户流水）、§80（金额精度）、§109（展示时区）、§112（导出需要明确的留存、安全删除与容量限制）、`REQ-PRIV-002`（INV-7）、`REQ-PRIV-001`（INV-8）
前置设计：#398 v4（客户用量明细：18 键白名单、马来西亚日历日、92 天、`ix_usage_events_tenant_occurred` / `ix_usage_events_project_occurred`、2 秒执行预算与 `QUERY_TOO_BROAD`，AIH-TASK-103..106）、#393 v3（客户钱包流水：7 键白名单，AIH-TASK-099..101）
目标 PR：待开（批准后按 §8 拆五个任务登记；`docs/TODO.md`「后续计划」里的「Phase 5 客户端用量导出」即本闸门）

## 1. 目标与边界

- **要解决的问题**：门户能按条件看自己的已计费用量（#398）和钱包流水（#393），但拿不走：spec §73 / §128 要求客户导出。客户对账时需要把整段数据放进表格软件，逐页抄不现实。
- **可观察的完成标准**：
  1. `GET /api/v1/customer/usage-events/export` 按与用量页相同的筛选（日期区间必填）返回一个 CSV 文件，每行一条已计费用量事件，列恰好是 #398 的 18 个键（时间列改名见 §2）。
  2. `GET /api/v1/customer/wallet/transactions/export` 按马来西亚日期区间（必填，最多 92 天）返回一个 CSV 文件，每行一条自己的钱包流水，列恰好是 #393 的 7 个键。
  3. 金额原样 8 位小数，与接口、账本逐分对得上；文件里没有任何成本、汇率、参考价、毛利或内部 id。
  4. 单个文件最多 20 000 行；超过时 422 `EXPORT_TOO_LARGE`，不返回部分文件；数据库查询沿用 2 秒执行预算，超时 422 `QUERY_TOO_BROAD`。
  5. 每个 API 进程同一时刻最多有 1 个导出**在生成或在发送**（从发查询之前，到最外层中间件确认整个响应已交给 uvicorn 之后），第二个立刻得到 503 `EXPORT_BUSY`（带 `Retry-After`），不排队、不占内存；单个导出的内存峰值有测试钉住的上限（§2「上限与资源边界」）。
  6. 门户用量页与钱包页各有一个「导出 CSV」按钮，用当前筛选（钱包页另有导出日期区间）下载文件。
- **明确不做什么**（Kelvin 2026-10-26 逐项确认）：
  - **只做 CSV**，不做 `.xlsx`（要新增依赖；CSV 能直接用 Excel 打开）。spec §73 的 Excel 留在「后续计划」。
  - **只做请求级用量与钱包流水**。spec §73 的另外两项**延后、不替代**：
    - 会话摘要导出：等会话功能（Phase 5「会话摘要」）做了再补；
    - 月度用量汇总导出：另登记为「后续计划」里的一项（口径复用 #403 的本月用量：一个马来西亚日历月每天一行，`date`、`request_count`、`charged_amount`，外加合计），本批不做。请求级导出选一个月**不是**汇总：它是逐条记录，超过 20 000 条时还会被拒绝。本闸门批准后的登记 PR 把这一项写进 `docs/TODO.md`「后续计划」。
  - **同步生成、不落盘**：请求时查询、在内存里拼好文件后一次返回；服务器不保存任何导出文件、不建导出任务表、不异步。所以 spec §112 的「导出文件留存与安全删除」在本设计里没有对象（见 §6）。超过行数上限就拒绝并提示缩短区间，不转异步。
  - **金额原样 8 位小数**，不按页面的 2 / 6 位舍入：导出是对账用的数据，舍入后加总会与账本差几分。页面显示规则（spec §80，#393 / #398）不变。
  - 不做管理端导出（管理端可以含内部成本，归 Phase 7「收入、成本、毛利分析与 CSV / Excel 导出」）。
  - 不加限流；不写审计（客户读自己的数据，与看页面同一性质）。
- **现有行为与问题证据**：
  - `app/repositories/usage_events.py` 的 `list_processed_events_for_customer(session, tenant_id, *, limit, …)` 已能按全部筛选取「最新在前」的已计费事件，一条 SELECT，MySQL 上带 `MAX_EXECUTION_TIME(2000)`，超时转 `QueryTooBroad`；`limit` 没有上限约束（上限 100 在接口参数上）。
  - `app/schemas/customer_usage.py` 的 `usage_period` 做日期校验与马来西亚日到 UTC 的换算（92 天、2020-01-01..2099-12-31）；`CustomerUsageQueryTooBroad` 是 422 `QUERY_TOO_BROAD`。
  - `wallet_transactions` 只有 `(tenant_id, wallet_sequence)` 与 `(tenant_id, reference_type, wallet_sequence)` 两个按序号的索引，**没有按 `created_at` 的索引**（`app/models/wallet.py:246`）：按日期区间取一个租户的流水只能扫这个租户的全部流水。每条计费的用量事件都有一行流水，活跃客户的流水会很多，所以按日期导出前先加索引（§2「数据库」）。

## 2. 设计概要

```text
GET …/export（客户访问令牌）→ 查询参数校验（FastAPI，处理函数之前，422）→ require_customer（第一条语句：401 / 403）
→ tenant_id = CustomerPrincipal.tenant_id → 日期换算成 UTC 区间（422）
→ 一个普通只读会话里一条 SELECT（LIMIT 20 001，2 秒预算）→ 行数 > 20 000：422 EXPORT_TOO_LARGE
→ 白名单投影 → 在内存里写 CSV → 200 text/csv（附件）
（无状态变化、无财务效果、无出站事件、无审计、不落盘）
```

### 接口一：`GET /api/v1/customer/usage-events/export`

- 查询参数与 `GET /api/v1/customer/usage-events` **相同的** `from_date`、`to_date`、`project_id`、`provider`、`model`、`conversation_id`、`event_id`，校验规则、错误与时序完全复用（`usage_period` 等）；**区别**：`from_date` 与 `to_date` 恒为必填（带 `event_id` 也要），没有 `page_size` 与 `cursor`（给了按「未知参数忽略」处理，与现有接口一致）。
- 处理函数第一条语句 `require_customer(request)`；租户只取 `CustomerPrincipal.tenant_id`，不定义任何租户或客户标识参数。
- 查询：调用现有 `list_processed_events_for_customer(..., limit=EXPORT_MAX_ROWS + 1)`，筛选、排序（`occurred_at DESC, public_id DESC`）、执行预算与 `QueryTooBroad` 全部沿用，不另写 SQL。`project_id` 不是自己的项目时照旧得到空结果（只有表头的文件）。
- 行数 > `EXPORT_MAX_ROWS`（20 000）：422 `EXPORT_TOO_LARGE`，不返回任何行。
- 列（表头恰好这 18 个，按这个顺序）：`id`、`event_id`、`request_id`、`conversation_id`、`feature`、`project_id`、`project_name`、`provider`、`model`、`usage_type`、`input_tokens`、`output_tokens`、`cache_creation_input_tokens`、`cache_read_input_tokens`、`quantity`、`unit`、`charged_amount`、`occurred_at_utc`。值与 JSON 接口的同名键逐一相同（`project_id` 是项目 `public_id`，`charged_amount` 是 `billable_cost` 的 8 位小数字符串，`quantity` 是 8 位小数字符串或空）；唯一改名的是时间列：`occurred_at_utc`，值与 JSON 的 `occurred_at` 相同（不带时区的 UTC、保留微秒），列名写明 UTC，免得在表格软件里被当成本地时间。
- 投影复用 #398 的客户用量白名单模型（`app/schemas/customer_usage.py` 里把 `CustomerUsageRow` 转成 18 键视图的那个函数），CSV 层只按上面的列顺序取值，不直接读 ORM 行或查询结果：白名单只有一份。
- 文件名：`usage-events_<from_date>_<to_date>.csv`（例 `usage-events_2026-10-01_2026-10-31.csv`）。

### 接口二：`GET /api/v1/customer/wallet/transactions/export`

- 查询参数：`from_date`、`to_date`，必填，规则与接口一相同（`YYYY-MM-DD` 马来西亚日历日、含两端、2020-01-01..2099-12-31、`to_date` 不早于 `from_date`、最多 92 天），复用 `usage_period(from_date, to_date, has_event_id=False)` 换成 UTC 区间 `[from_utc, to_utc)`，**按流水的 `created_at`（记账时间）取**。
- 处理函数第一条语句 `require_customer(request)`；先用现有 `get_wallet_for_tenant` 确认钱包存在，查不到抛 `RuntimeError(WALLET_MISSING)`（沿用 #393 v3 的写法与 500）。
- 查询：`app/repositories/wallet.py` 新增 `list_transactions_for_customer_export(session, tenant_id, *, created_from, created_to, limit, timeout_ms=CUSTOMER_USAGE_QUERY_TIMEOUT_MS)`：一条 SELECT，`WHERE tenant_id = ? AND created_at >= ? AND created_at < ?`，`ORDER BY wallet_sequence DESC`，`LIMIT limit`；只选客户白名单需要的列（`public_id`、`wallet_sequence`、`transaction_type`、`amount`、`balance_after`、`reference_type`、`reference_id`、`created_at`），**不 JOIN 用户表**（不读操作者邮箱）、不读 `description`、`metadata_json`、`balance_before`；不加锁；MySQL 上带 `/*+ MAX_EXECUTION_TIME(2000) */`，超时 3024 转 `QueryTooBroad`（复用 `usage_events.py` 的常量、异常与转换逻辑，不另写一份），接口 422 `QUERY_TOO_BROAD`，记一条 `customer_usage_query_timeout` 日志（与用量接口同一条，只带 `request_id` 与客户 id）。
- `limit = EXPORT_MAX_ROWS + 1`；多于 20 000 行 422 `EXPORT_TOO_LARGE`。
- 排序按 `wallet_sequence` 倒序（账本的真实顺序，`balance_after` 逐行连得上），不按 `created_at`：`created_at` 是写入时取的时间，不保证与序号同序。MySQL 用新索引做范围扫描取出区间内的行，再按序号排序；区间内行很多时排序可能超过 2 秒预算，结局是 `QUERY_TOO_BROAD`（缩短区间），不是慢查询。
- 列（表头恰好这 7 个，按这个顺序）：`id`、`wallet_sequence`、`transaction_type`、`amount`、`balance_after`、`event_id`、`created_at_utc`。值与 `GET /api/v1/customer/wallet/transactions` 每项的同名键相同：`event_id` 只在 `reference_type = USAGE_EVENT` 时等于 `reference_id`，其余为空（调账、系统更正、rebill 的 `reference_id` 是内部幂等键，不给客户）；`created_at_utc` 与 JSON 的 `created_at` 相同。投影复用 #393 的客户流水白名单函数（`customer_wallet_transaction_view` 或从它抽出的同一份映射），不另写一份。
- 文件名：`wallet-transactions_<from_date>_<to_date>.csv`。

### CSV 格式（两个接口共用，新模块 `app/core/csv_export.py`）

- UTF-8，文件开头带 BOM（`﻿`），Excel 才能正确识别非 ASCII 的项目名；行尾 `\r\n`（RFC 4180，`csv` 标准库默认）；字段按需加双引号（`csv.QUOTE_MINIMAL`），值里的双引号转义成两个双引号。
- 第一行是表头；之后每行一条记录；空值（JSON 里的 `null`）写成空字段；整数原样；金额与 `quantity` 是 8 位小数字符串原样，不经 float、不舍入（INV-10）。
- **公式注入防护**：文本列（接口一的 `id`、`event_id`、`request_id`、`conversation_id`、`feature`、`project_id`、`project_name`、`provider`、`model`、`usage_type`、`unit`；接口二的 `id`、`transaction_type`、`event_id`）的值若以 `=`、`+`、`-`、`@`、制表符或回车开头，前面加一个单引号 `'`。这些值有的来自集成方上报（`request_id`、`conversation_id`、`feature`、`provider`、`model`）或管理员（`project_name`），用 Excel 打开时以这些字符开头的单元格会被当成公式执行。数值列（token、`quantity`、`charged_amount`、`amount`、`balance_after`、`wallet_sequence`）与时间列不加：负金额 `-0.04120000` 必须保持是数。
- 响应头：`Content-Type: text/csv; charset=utf-8`、`Content-Disposition: attachment; filename="<文件名>"`（文件名只由校验过的日期拼出，只含 ASCII 字母、数字、`-`、`_`、`.`）、`Cache-Control: no-store`（导出文件不留在浏览器缓存里）、`X-Accel-Buffering: no`（见下文「代理不缓冲」）。
- 错误响应不变：仍是 JSON 信封（`{success:false, error, request_id}`），HTTP 状态码照常。因为整个文件在内存里拼好才开始发送，不会出现「已经发了 200 和半个文件、查询才失败」的情况。
- 模块只有纯函数与一个响应类：`write_csv(columns, rows, text_columns) -> bytes` 与 `csv_response(filename, body) -> ExportCsvResponse`（分段发送，见 §2「上限与资源边界」）一类，不碰数据库、不打日志。

### 上限与资源边界

- `EXPORT_MAX_ROWS = 20_000`，两个接口共用，定义在 `app/core/csv_export.py`。超出时的 422 `EXPORT_TOO_LARGE` 文案固定：说明超过 20 000 行、请缩短日期区间或加筛选条件，不回显参数值，不报实际行数。
- **为什么要进程内的并发上限**（设计闸门 #435 v1 审查阻断项）：生产的 `api` 容器是**一个** uvicorn 进程、`mem_limit` 默认 768 MB（`docker-compose.yml` 的 `backend` 锚点），同步处理函数在线程池里并发执行；AI 模块（`/ai/v1/*`）、集成用量摄取与管理端都在这个进程里。2 秒执行预算只限制数据库，不限制 Python 侧的投影与拼 CSV；多个大导出同时进来，内存叠加可能让容器被 OOM 杀掉，期间同进程的 AI API 也中断（INV-1 的冲击面）。
- **导出租约（进程内至多 1 个）**：`app/core/csv_export.py` 持有一个进程级的导出租约，两个接口共用：一个 `threading.Lock` 保护的「当前持有者 token 或空」，`EXPORT_MAX_CONCURRENT = 1`。**没有按时间的过期与接管**（v3 的 120 秒接管会把仍在发送的导出当成泄漏，连续接管时并发没有上界；设计闸门 #435 v3 审查阻断项）。
  - **获取**：处理函数在 `require_customer` 与参数校验之后、**发任何查询之前**以不阻塞方式获取（`try_acquire_export_lease() -> token | None`）：空闲就拿到一个新 token；被占用就返回 `None`，处理函数立刻 503 `EXPORT_BUSY`，响应头 `Retry-After: 5`，文案固定（导出正忙，请几秒后重试），不排队、不等待、不占线程。
  - **唯一的释放点在最外层**（v2、v3 审查阻断项：在处理函数里拼好文件时释放、或在内层响应的 `send` 返回时释放，都早于响应体真正离开应用——`RequestContextMiddleware` 是 `BaseHTTPMiddleware`，内层 `send` 返回只表示消息交给了它，外层还在发送）：新增一个**纯 ASGI** 中间件 `ExportLeaseMiddleware`（`app/core/csv_export.py`），在 `app/main.py` 里**最后**一个 `add_middleware`，所以它是所有用户中间件的最外层（Starlette 里后加的在外层；内置的 `ServerErrorMiddleware` 仍在它外面）。它对每个 HTTP 请求：
    1. 调用内层之前，在 `scope` 里放一个空的租约槽（`scope["acuven.export_lease"] = ExportLeaseSlot()`）；
    2. `try: await self.app(scope, receive, send) finally: slot.release()`——槽里有 token 才释放，并且只释放这个 token。
    处理函数拿到 token 后只做一件事：`request.scope["acuven.export_lease"].hold(token)`；之后**不在任何地方释放**。查询、投影、拼 CSV 出错、返回 503 / 422 / 500、正常发完、客户端断开（uvicorn 的 `send` 在连接断开后直接返回、不抛错，分段循环随之很快走完）、请求被取消（`CancelledError`）——这些结局最终都要让最外层的 `await self.app(...)` 返回或抛出，`finally` 在每一种结局都执行。`BaseHTTPMiddleware` 不换 `scope` 对象（它把同一个 `scope` 传给内层），槽在两层之间共享。
  - **所有错误响应都在租约内发出**（设计闸门 #435 v4 审查阻断项）：`register_error_handlers` 把 `Exception` 的处理器 `handle_unexpected_error` 注册在 Starlette 的 `ServerErrorMiddleware` 上，它在 `ExportLeaseMiddleware` **外面**；而 `AppError`、`RequestValidationError`、`HTTPException` 的处理器在最内层的 `ExceptionMiddleware`，在租约中间件**里面**。所以一个未处理的异常会先穿过租约中间件（`finally` 释放租约），再由外层生成并发送 500；异常的回溯还引用着正在投影的查询结果，这时第二个导出已经能开始。为此两个导出处理函数从拿到租约起，把查询、投影、拼 CSV 放进一个 `try`：
    - `QueryTooBroad` 照旧转成 `CustomerUsageQueryTooBroad`（`AppError`）；
    - 其他任何 `Exception`：在 `except` 里 `logger.exception("customer_export_failed")`（栈进日志，与 `handle_unexpected_error` 同一口径，不带参数值与文件内容），只设一个标志；**离开 `except` 块之后**、先把持有查询结果与部分 CSV 的局部变量置空，再抛一个新的 `AppError` 子类 `CustomerExportFailed`（500，`code = "INTERNAL_ERROR"`，固定文案「An internal error occurred.」，与通用 500 的信封逐字相同）。在 `except` 块之外抛出，新异常就没有 `__context__` 指向原异常，原异常与它引用的帧、查询结果在抛出前已经可以回收。
    - `CustomerExportFailed` 由内层 `ExceptionMiddleware` 的 `handle_app_error` 生成 500 响应，在租约中间件**里面**发出；租约要等这个 500 的 `send` 返回才释放。
    - 不捕获 `BaseException`（`CancelledError`、`KeyboardInterrupt`）：取消时没有要发的响应，`finally` 照常释放。
    这样从拿到租约到释放，进程里只有这一个导出的数据，以及（出错时）一个不引用它的小错误信封。
  - **分段发送，释放时只剩一小段在缓冲里**（设计闸门 #435 v7 审查阻断项：uvicorn 0.52 的 `send` 是**先**在写暂停时等排空、**再**把这一条消息整个写进传输缓冲，写完立刻返回；v7 把整份约 8 MB 的 CSV 作为一条 body 消息发出，`send` 一返回租约就释放，而这 8 MB 还在传输缓冲里等慢客户端读，连续几次下载就叠加）：
    - 响应体不放进普通 `Response`，而由 `csv_export.py` 的 `ExportCsvResponse(Response)` 发送：覆写 ASGI `__call__`，先发 `http.response.start`（带 `Content-Length` 与 §2「CSV 格式」的全部响应头），再把拼好的字节按 `EXPORT_SEND_CHUNK_BYTES = 65536` 一段、用 `memoryview` 切片（不复制）逐条发 `http.response.body`，前面各段 `more_body=True`，最后一段 `False`；空文件（只有 BOM 与表头）也走同一条路径。
    - uvicorn 每发一段之前，若传输缓冲已超过高水位就先等它排空（asyncio 传输的默认高水位 64 KiB），所以第 N 段能写进去时，前面的字节基本都已交给内核与 nginx；最后一段的 `send` 返回时，传输缓冲里至多是「高水位 + 一段」，约 128 KiB。租约此时才可能被最外层中间件释放。
    - 因此在租约释放之前，进程里同一时刻至多一份导出的响应体与其生成过程；释放之后残留在某条慢连接上的至多约 128 KiB，与普通 JSON 响应同一量级，不随导出大小增长。
    - 经过 `RequestContextMiddleware`（`BaseHTTPMiddleware`）时，每段是一条独立消息，内外层之间的内存流没有缓冲区（容量 0），外层每取走一段内层才继续，分段不会在中间层重新合并。
  - **代理不缓冲**（设计闸门 #435 v6 审查阻断项：v6 依赖 nginx 默认的代理缓冲让租约很快释放，但导出响应约 8 MB，远大于 nginx 的内存缓冲，超出部分会写进 nginx 的代理临时文件——客户的计费明细以明文落在代理主机的磁盘上，连续的慢下载还会累积占用磁盘，`Cache-Control: no-store` 管不到这一层）：
    - 两个导出接口的响应都带 `X-Accel-Buffering: no`。nginx 收到上游的这个头，就对这一个响应关闭代理缓冲：边读边转发给客户端，不写临时文件。
    - 生产上的链路是：客户端 → 共享的边缘反向代理（`vps_infra` 的 `infra_nginx`）→ 本项目的 `billing_nginx` → API。nginx 默认会消费并**去掉**上游的 `X-Accel-*` 头，所以只靠应用发这个头，外面那一层仍会缓冲。为此 `deploy/nginx/billing.conf` 的 `location /api/` 加 `proxy_pass_header X-Accel-Buffering;`：`billing_nginx` 自己照它关闭缓冲，同时把它原样交给外层，外层也关闭缓冲。其他接口不发这个头，行为不变。
    - 2026-10-26 读过 `vps_infra` 里外层代理对本站的配置：没有 `proxy_ignore_headers`，也没有对本站关闭 `X-Accel-Buffering` 的处理，所以它会照这个头关闭缓冲。外层不在本仓库，这是 §10 的一条假设。
    - 部署：`deploy/deploy.sh` 第 5b 步在每次部署时 `nginx -t` 后 reload `billing_nginx`，配置随 AIH-TASK-114 的部署生效。
    - 结果：导出期间，响应体只在 API 进程里有一份（受租约约束），两层代理都只有少量在途缓冲，任何一层都不写磁盘。
  - **慢客户端**：代理不缓冲，所以租约从生成一直持有到客户端把文件读完（分段发送，每段之前 uvicorn 都按流控等排空；最外层中间件要等最后一段的 `send` 返回才释放）。慢客户端只会挡住其他导出（503 `EXPORT_BUSY`），不叠加内存、不落盘，也不影响非导出请求。客户端停住不读时，nginx 的 `send_timeout`（默认 60 秒，`billing.conf` 没有改）到点关闭连接，上游连接随之关闭，uvicorn 把连接标记为断开，之后的 `send` 直接返回，剩下的分段很快走完，租约在最外层的 `finally` 里释放。
  - **长时间占用的可观测性**：获取失败时若当前租约已持有超过 `EXPORT_LEASE_WARN_SECONDS = 120` 秒，记一条 `customer_export_lease_held_long` 警告日志（只带 `request_id`，每个持有者只记一次），**不接管**；运维据此排查卡住的连接。
  - 一个客户的导出会挡住所有客户的导出几秒，这是有意的：现阶段门户客户很少，换取的是内存上界确定。
- **单个导出的内存上界**：生成路径不保留多余副本：查询结果 → 逐行投影成白名单字典 → 直接写进一个 `io.StringIO` → `getvalue().encode("utf-8")` 之后立刻丢弃结果列表与 `StringIO`。`tests/backend/test_csv_export.py` 用 `tracemalloc` 钉住上界：20 000 个合成的最长形态 `CustomerUsageRow` 先建好并计入（`tracemalloc.start()` 在建行之前），再走完投影、`write_csv`（含 BOM）与 `encode`，**峰值必须 ≤ 128 MiB**（Kelvin 2026-10-26 选择：保留 2 万行、上界放宽到 128 MiB；v5 的 64 MiB 按设计闸门 #435 登记前预审的估算会被突破，约 90–110 MB）。超了测试红，AIH-TASK-114 停下说明，不在实现里自行调上限或行数。「最长形态」的定义：
  - 每个字符串列取模型列宽的 **ASCII** 字符串（`app/models/usage.py` 的 `EVENT_ID_LENGTH`、`REQUEST_ID_LENGTH`、`PROVIDER_CODE_LENGTH`、`MODEL_CODE_LENGTH`、`UNIT_LENGTH`、`FEATURE_LENGTH` 等长度常量；项目名 255；`conversation_id` 与 `request_id` 同宽）；ASCII 是因为生产上这些列都受摄取格式约束（`event_id`、`conversation_id` 等的正则只含 ASCII），只有项目名可能含非 ASCII，它只有一列，按 ASCII 估算的偏差远小于余量；
  - 每一行每一列都是**独立**的字符串对象（用行号拼出不同的值，不让 20 000 行共享同一个对象，否则测得的峰值偏小）；
  - 四个 token 列取 `2^63 − 1`，`quantity` 与 `billable_cost` 取 `Decimal("999999999999.99999999")`，时间取带微秒的 `datetime`；
  - 数据库驱动的缓冲不计（与普通查询相同）。
- **加起来**：导出在进程既有的内存占用（常驻部分加 Argon2 每次哈希 64 MiB 等峰值，`docs/perf-baseline.md` §3.2）之上至多多出一份 ≤ 128 MiB（768 MiB 限额的六分之一），且不随并发数叠加（没有接管，所以没有「短暂两份」）。
- **如果仍被 OOM 杀掉**（例如以后有人调低限额）：容器 `restart: unless-stopped` 与健康检查让它自动重启；导出只读、不加锁、不落盘，重启不留下任何半完成的状态；AI 模块与本平台同进程，进程不可用期间它也不可用——这是 ADR-0012 接受的代价，但不能由导出引起，所以才有上面的并发上限与内存上界；客户重新导出即可。§5 列为一行。

### 数据库

- 新迁移 `0027`：给 `wallet_transactions` 加非唯一索引 `ix_wallet_transactions_tenant_created (tenant_id, created_at, wallet_sequence)`。写法完全照 `0026`（AIH-TASK-103）：inspector 先查同名索引，不存在 → 建；存在且形状正确（非唯一、列恰好按序、没有前缀长度）→ 跳过；形状不对 → 抛错、不发 DDL；MySQL 上一条 `ALTER TABLE wallet_transactions ADD INDEX …`，SQLite 用 `op.create_index`；`downgrade` 只删存在的。模型 `WalletTransaction.__table_args__` 同步声明。
- MySQL 8 InnoDB 加二级索引是在线 DDL（`ALGORITHM=INPLACE, LOCK=NONE`），建索引期间流水照常可写，记账不被阻塞；生产上流水行数很少，瞬时完成。`wallet_transactions` 的触发器（不可变性、钱包推进）不受影响：建索引不改行。
- 不改任何列、约束、触发器与既有索引；用量导出不需要新索引（复用 0026 的两个）。

### 事务边界

- 两个接口：普通只读会话，一条 SELECT（接口二另有一条找钱包的 SELECT，与 #393 流水接口相同），不提交、不加锁、不写。一条 SELECT 就是一个一致的快照：文件里的行彼此一致，不会出现同一条记录出现两次或漏掉中间的行。

### 外部系统与异步边界

- 无。不入队、不调外部服务、不写文件系统。

### 时间

- 用量导出按 `occurred_at`（调用发生时间）的马来西亚日历日取，与用量页一致；钱包导出按 `created_at`（记账时间）的马来西亚日历日取。两者的同一日期区间可能对不上几笔跨日的事件（月末发生、次月计费），与 #403 已写明的口径相同；文档写清。
- 文件里的时间一律是不带时区的 UTC，列名带 `_utc`。

### 金额

- 原样 8 位小数字符串（`money_text`），与 JSON 接口逐字节相同；不舍入、不加千分位、不带币种（币种恒为 MYR，写在文档里）。

### 前端（第四、第五个任务，不在闸门范围内，写在这里只为说明接口够用）

- 下载要带访问令牌，不能用普通链接：经现有 axios 客户端（拦截器负责令牌与刷新）以 `responseType: "blob"` 取文件，成功时用对象 URL 触发下载（文件名取自 `Content-Disposition`，取不到时前端用同一规则拼）；失败时把 blob 按 JSON 信封解析出 `error.code` 与 `request_id` 显示，`EXPORT_TOO_LARGE` 与 `QUERY_TOO_BROAD` 各有专门文案。
- 用量页：「导出 CSV」按钮用页面当前已应用的筛选，日期区间无效时按钮不可用；导出中按钮转为加载状态、不可重复点。
- 钱包页：流水上方加导出区：日期区间（默认本马来西亚月 1 日到今天）与「导出 CSV」按钮；不影响流水列表的翻页。
- 页面不请求任何 `/api/v1/admin/` 路径。部署后浏览器验收各加一个只读步骤：只核对按钮存在且可用，**不点击**（验收不下载文件）。

## 3. 不变量影响矩阵

| 不变量 | 是否触碰 | 可能怎样被破坏 | 设计控制 | 验证方式 |
| --- | --- | --- | --- | --- |
| INV-1 中心故障不中断 AI | 是（守住） | 大导出拖慢共享数据库，影响摄取与计费；并发的大导出耗尽 API 进程内存，同进程的 AI API 随容器重启中断 | 每条 SELECT 2 秒执行预算，超时即中止；行数上限 20 000；不加锁；进程内至多 1 个导出在生成或发送（租约只由最外层纯 ASGI 中间件在整个请求结束时释放，不按时间接管），其余立刻 503 `EXPORT_BUSY`；单个导出内存峰值 ≤ 128 MiB | 测试：MySQL 上 1 毫秒预算触发 `QUERY_TOO_BROAD`；占住租约时第二个导出 503 且不发查询；经过真实中间件栈、外层 `send` 暂停期间第二个导出 503；`tracemalloc` 峰值上界 |
| INV-2 事件不重复扣费 | 否 | 只读 | — | — |
| INV-3 支付不重复入账 | 否 | 只读 | — | — |
| INV-4 余额只经账本变动 | 否 | 只读；迁移只加索引 | 迁移不改行、不碰触发器 | 迁移测试：建索引前后流水行与钱包不变 |
| INV-5 历史账本不可变 | 是（守住） | 迁移误改了流水表的列或触发器 | 只加二级索引，`0027` 先检查形状、不改列与触发器 | 迁移测试：触发器仍在；`test_migrations.py` 模型比对 |
| INV-6 事件保留版本引用 | 否 | 只读，不导出版本引用 | — | — |
| INV-7 客户不可见成本毛利 | 是 | CSV 层直接读查询结果或 ORM 行，把成本、汇率、参考价、`description`、操作者邮箱、内部 id 写进文件；以后表上加列时跟着出现 | 列清单固定（18 / 7 个）；值只取自 #398 / #393 的白名单投影；钱包导出的 SELECT 不 JOIN 用户表、不选 `description` / `metadata_json` | 测试：表头恰好 18 / 7 列；原始文件字节里没有成本、汇率、参考价、调账原因、管理员邮箱、调账幂等键的哨兵值 |
| INV-8 租户不可互访 | 是 | 导出按请求参数取了别人的数据 | 租户只取自会话；两条 SELECT 都带 `tenant_id`；项目、`event_id`、`conversation_id` 只是附加条件 | 测试：A、B 各有数据，A 用 B 的项目 id、`event_id`、`conversation_id` 导出只得到表头 |
| INV-9 对话内容不入库 | 否 | 只导出元数据，库里本来就没有对话内容 | — | — |
| INV-10 金额用 Decimal | 是 | 写 CSV 时金额经 float 或被舍入 | 金额列写的是 `money_text` 字符串原样 | 测试：`999999999999.99999999` 与 `0.00000001` 原样出现在文件里 |
| INV-11 event_id 至多一次财务效果 | 否 | 只读 | — | — |
| INV-12 定稿对账单不可变 | 否 | 对账单尚未实现 | — | — |
| INV-13 状态与事件原子提交 | 否 | 没有状态变化 | 一条 SELECT 即一个快照，文件内部一致 | — |
| INV-14 队列丢失不毁持久工作 | 否 | 不入队、不落盘 | — | — |

## 4. 状态与并发

- 没有状态机，没有写入，没有幂等键：同一请求重放得到当时的数据（可能多出晚到的行），不产生任何副作用。
- 并发：两个接口不加锁；导出与摄取、计费、记账并发时，读的是 SELECT 开始时的快照（InnoDB 一致性读），不阻塞写入方，也不被它们阻塞（不读锁）。
- 迁移与并发：在线 DDL，建索引期间记账照常；只在开始和结束各短暂拿一次元数据锁（等正在进行的、碰过这张表的事务结束），与 0023、0026 相同。
- 唯一性：不涉及。重试由客户端（门户用户再点一次）发起，服务端不重试。

## 5. 失败与恢复矩阵

| 失败点 | 对外结果 | 数据最终状态 | 是否可重试 | 恢复来源 | 告警 |
| --- | --- | --- | --- | --- | --- |
| 校验失败（日期缺失、格式、区间超 92 天、筛选格式） | 422 `VALIDATION_ERROR`，只列字段名 | 不变 | 改正后重试 | — | 无 |
| 超过 20 000 行 | 422 `EXPORT_TOO_LARGE`，不返回任何行 | 不变 | 缩短区间或加筛选后重试 | — | 无 |
| 查询超过 2 秒预算 | 422 `QUERY_TOO_BROAD`；一条 `customer_usage_query_timeout` 日志 | 不变 | 缩短区间后重试 | — | 日志（常见时考虑聚合或异步，§9） |
| 钱包缺失（仅接口二） | 500 `INTERNAL_ERROR`（`RuntimeError(WALLET_MISSING)`，沿用 #393） | 不变 | 否 | 运维 | 现有错误日志 |
| 数据库不可用（查询时连接失败、连接中断） | 500 `INTERNAL_ERROR`（`CustomerExportFailed`），带 `request_id`；与现有客户接口一致：查询时的连接错误不映射成 503，`DatabaseUnavailable`（503）只用于 `/readyz` 的就绪检查；数据库根本没配置时 `require_session_factory` 照旧在获取租约之前给出 503 `DATABASE_NOT_CONFIGURED` | 不变 | 是 | — | `customer_export_failed` 错误日志（带栈）与现有数据库告警 |
| 鉴权失败 | 401 `TOKEN_INVALID` / 403 `CUSTOMER_REQUIRED`（现有） | 不变 | — | — | 无 |
| 查询、投影或生成 CSV 时意外出错 | 500 `INTERNAL_ERROR`（`CustomerExportFailed`，信封与通用 500 相同），带 `request_id`；文件尚未开始发送；这个 500 在租约内由内层异常处理发出，发完后租约才在最外层中间件的 `finally` 里释放 | 不变 | 是 | — | `customer_export_failed` 错误日志（带栈） |
| 发送中客户端断开 / 请求被取消 | 客户端下载失败 | 不变；租约在最外层中间件的 `finally` 里释放 | 是，重新导出 | — | 无 |
| 慢客户端占住租约（代理不缓冲） | 其他客户的导出 503 `EXPORT_BUSY`，直到它读完、断开，或停住不读 60 秒被 nginx `send_timeout` 关闭；超过 120 秒记 `customer_export_lease_held_long` | 不变；两层代理都不写临时文件 | 是 | 连接结束即释放 | 警告日志 |
| 已有一个导出在生成 | 503 `EXPORT_BUSY`，`Retry-After: 5`；不发查询 | 不变 | 是，几秒后 | — | 无 |
| API 进程内存耗尽被杀（不应发生，见 §2「资源边界」） | 进行中的请求失败；容器自动重启期间 API 不可用 | 不变：导出只读、不落盘，没有半完成状态 | 是，重启后重新导出 | `restart: unless-stopped` + 健康检查 | 现有容器重启与 `/healthz` 告警 |
| 客户端下载中断 | 浏览器侧失败 | 不变（服务器不留文件） | 是，重新导出 | — | 无 |
| 迁移 `ALTER TABLE` 失败 / 写版本前进程退出 / 同名索引形状不对 | 部署失败或重跑通过（同 0026 的三个窗口） | 索引要么建好要么没建，版本与之对应 | 是，原样重新部署；形状不对时按报错删掉那个索引后重新部署 | 迁移的检查步骤 | 部署失败通知 |
| Redis/Celery 丢失 | 不影响：不入队 | — | — | — | — |
| 外部服务超时 | 不适用 | — | — | — | — |
| 重复请求 | 200，当时的数据 | 不变 | 是 | — | 无 |

## 6. 数据与安全边界

- **租户过滤**：两条 SELECT 的 `WHERE` 恒带 `tenant_id = CustomerPrincipal.tenant_id`（`require_customer` 从库里的用户行取，#368）。接口不接受任何租户、客户、钱包标识。
- **鉴权主体**：客户访问令牌；ADMIN 令牌 403 `CUSTOMER_REQUIRED`；非 `ACTIVE` 门户账号 401 `TOKEN_INVALID`（现有行为）。只读客户（`read_only`）可以导出：导出是读。两个新路由必须进 `tests/backend/test_customer_api.py` 的路由守卫。
- **客户侧禁止出现在文件里的**：成本（估算与对账）、原币成本、汇率、参考价、毛利、加价、价格版本与定价规则引用、`billing_mode_snapshot`、`status`、错误信息、`received_at` / `processed_at`、`payload_fingerprint`、内部 id（`tenant_id`、`project_id` 内部值、`provider_id` / `model_id`、`wallet_id`、`created_by`）；流水的 `description`、`reference_type`、调账 / 系统更正 / rebill 的 `reference_id`、`balance_before`、`metadata_json`、操作者邮箱。
- **日志 / 异常**：不记文件内容、不记查询参数值、不记行数以外的数据；超时日志只带 `request_id` 与客户 id（现有）。422 文案不回显值。
- **留存（spec §112）**：服务器不保存导出文件（内存里生成，请求结束即释放）；两层 nginx 对导出响应都不缓冲（`X-Accel-Buffering: no` 加 `billing.conf` 的 `proxy_pass_header`，§2「代理不缓冲」），不写代理临时文件；`Cache-Control: no-store` 让浏览器不缓存；文件交给客户之后的保管是客户自己的事。所以没有需要定留存期与安全删除的服务器端导出文件；上线闸门的数据治理政策里把「客户导出：不在服务端保存」写成一行即可（`docs/TODO.md` 上线闸门 PDPA 一项的输入）。
- **密钥**：不涉及。**prompt / response**：不涉及（库里没有）。
- **公式注入**：见 §2「CSV 格式」。这是导出文件特有的风险：门户页面渲染是转义的文本，文件在 Excel 里不是。

## 7. 测试证据计划

后端，用量导出（`tests/backend/test_customer_usage_export_api.py` 新文件，SQLite 与 MySQL 参数化，MySQL 用 `BILLING_TEST_DATABASE_URL`、未设置时 skip、CI 必跑）：

| 风险/需求 | 测试层级 | 场景 | 预期结果 |
| --- | --- | --- | --- |
| 正常路径 | integration | 三条已计费事件（两个项目）+ 一条 `RECEIVED`、一条 `FAILED`；导出覆盖它们的区间 | 200、`text/csv; charset=utf-8`、附件文件名正确、`Cache-Control: no-store`、`X-Accel-Buffering: no`；以 BOM 开头；表头恰好 18 列且顺序正确；3 行，最新在前；每行每列与 `GET /customer/usage-events` 同一区间返回的同名键逐字相等（时间列对 `occurred_at`） |
| 筛选一致 | integration | 分别带 `project_id`、`provider`、`model`、`conversation_id`、`event_id` | 行集合与 JSON 接口同筛选翻完全部页的结果相同 |
| 日期必填 | integration | 只带 `event_id`、不带日期 | 422 `VALIDATION_ERROR`（与 JSON 接口不同：导出恒要日期） |
| 校验复用 | integration | 区间 93 天、`to_date` 早于 `from_date`、`0001-01-01`、坏格式的 `provider` | 与 JSON 接口相同的 422 |
| 上限 | integration | 测试里把上限 monkeypatch 成 3；造 4 条与恰好 3 条 | 4 条 422 `EXPORT_TOO_LARGE`，响应是 JSON 信封、没有 CSV 行；3 条 200、3 行 |
| 上限常量 | unit | `EXPORT_MAX_ROWS` | 等于 20 000；查询的 `limit` 是它 + 1 |
| 扫描上限 | integration（MySQL） | 30 000 条当月事件，测试专用 1 毫秒超时 | 422 `QUERY_TOO_BROAD`；一条 `customer_usage_query_timeout` 日志 |
| 金额精度 | integration | `charged_amount` 为 `999999999999.99999999` 与 `0.00000001`，`quantity` 为 `0.00000001` | 文件里原样是这几个字符串 |
| 公式注入 | integration | `request_id` = `=1+1`、`conversation_id` = `-2`、`feature` = `@x`、项目名 = `+cmd`（直接写库造数据） | 这些单元格以 `'` 开头；`charged_amount` 列不加 |
| 公式注入（六种前缀） | unit | `write_csv` 的文本列分别以 `=`、`+`、`-`、`@`、制表符 `\t`、回车 `\r` 开头；同样的值放在数值列；以这些字符**之外**的字符开头（如 `a=1`）；值中间含这些字符（`a-b`） | 前六种文本列值读回是 `'` + 原值；数值列原样；后两种不加 |
| 并发上限 | unit + integration | 测试里先占住租约，再调用两个导出接口 | 都是 503 `EXPORT_BUSY`、带 `Retry-After: 5`、没有发出用量或流水查询（记录执行的语句）；释放后照常 200；处理中抛错（monkeypatch 投影函数抛异常）之后租约已释放、下一次导出 200 |
| 发送阶段持有租约（经真实中间件栈） | integration | 用 `create_app` 建的真实应用（含 `RequestContextMiddleware` 与 `ExportLeaseMiddleware`），以 ASGI 方式直接调用 `app(scope, receive, send)` 发一个成功的用量导出；传入的**最外层** `send` 在收到 `http.response.body` 时暂停（`asyncio.Event`）；暂停期间在同一事件循环里再发一个导出请求；然后放行 | 暂停期间第二个请求 503 `EXPORT_BUSY`；第一个请求的 `app(...)` 返回之后租约空闲，第三个请求 200 |
| 断开与取消 | integration | 同上的真实应用：一例最外层 `send` 在 body 消息上抛 `OSError`；一例在暂停时取消调用 `app(...)` 的任务 | 两种情况下 `app(...)` 结束后租约都空闲，下一次导出 200 |
| 意外错误的 500 在租约内发出 | integration | 同上的真实应用：monkeypatch 投影函数在第 N 行抛 `RuntimeError`；最外层 `send` 在收到这个请求的 `http.response.start` 时暂停；暂停期间再发一个导出请求；然后放行 | 第一个请求的响应是 500、`error.code = INTERNAL_ERROR`、带 `request_id`，与通用 500 的信封形状相同；暂停期间第二个请求 503 `EXPORT_BUSY`；放行后租约空闲；日志里有一条 `customer_export_failed`，且这条 500 **不是**由 `handle_unexpected_error` 生成（断言那条 `Unhandled exception` 日志没有出现） |
| 抛出的错误不链回原异常 | unit | 直接调用导出处理逻辑、让投影抛错，捕获抛出的 `CustomerExportFailed` | `__context__` 与 `__cause__` 都是 `None` |
| 数据库断连（用量导出） | integration | 拿到租约之后，让用量查询抛 `sqlalchemy.exc.OperationalError`（monkeypatch `list_processed_events_for_customer`，模拟连接中断；SQLite 与 MySQL 都跑） | 500、`error.code = INTERNAL_ERROR`、带 `request_id`，信封形状与通用 500 相同（不是 503）；有一条 `customer_export_failed`，没有 `Unhandled exception`（这个 500 由内层处理器在租约内发出）；之后租约空闲，下一次导出 200 |
| 中间件顺序 | unit | 检查 `create_app()` 的 `user_middleware` | `ExportLeaseMiddleware` 是第一个（最外层），`RequestContextMiddleware` 在它里面 |
| 不接管 | unit | 持有 token A，把时钟（monkeypatch 时间函数）推过 120 秒，再获取两次 | 两次都返回 `None`；只记一条 `customer_export_lease_held_long`；释放 A 后获取成功 |
| 槽只释放自己的 token | unit | 槽里没有 token 时 `release()`；槽 hold A 后释放两次 | 前者无副作用；后者第二次无副作用，不影响之后别的持有者 |
| 分段发送 | unit | 以 ASGI 方式调用 `ExportCsvResponse`，`send` 记录每条消息；body 分别为 0 字节以外的最小文件、恰好 65 536 字节、65 537 字节、约 8 MiB | 一条 `http.response.start`（`Content-Length` 等于总长）；每条 body 消息 ≤ 65 536 字节，只有最后一条 `more_body=False`；拼回来与原字节逐字节相等 |
| 慢读连接持有租约（真实 uvicorn） | integration | 测试里在本机随机端口起一个真实的 uvicorn 服务（线程内运行 `uvicorn.Server`，应用由 `create_app` 建），把导出生成 monkeypatch 成返回 8 MiB 字节；用原始 socket 客户端（`SO_RCVBUF` 设小）发第一个导出请求，只读响应头与前 256 KiB 后停住；在它停住期间用另一条连接发第二个导出请求；然后让第一条读完全部字节；再发第三个 | 第二个请求 503 `EXPORT_BUSY`；第一条读到的总字节数等于 `Content-Length`；第三个请求 200；测试结束时关闭服务 |
| 代理配置 | unit | 读 `deploy/nginx/billing.conf`（照 `tests/backend/test_compose.py` 读配置文件的写法） | `location /api/` 块里有 `proxy_pass_header X-Accel-Buffering;`；整个文件没有 `proxy_ignore_headers`；没有 `proxy_buffering off`（其他接口照旧缓冲） |
| 内存上界 | unit | `tracemalloc` 下 20 000 行最长形态合成用量行走完投影与 `write_csv` | 峰值 ≤ 128 MiB（行的定义见 §2「单个导出的内存上界」） |
| CSV 转义 | unit | 值里含逗号、双引号、换行；空值 | 按 RFC 4180 加引号与转义，读回（`csv.reader`）逐值相等；空值是空字段 |
| 空结果 | integration | 区间内没有事件；别人的项目 id | 200，只有 BOM 与表头一行 |
| 租户越权 | integration | A、B 各有事件；A 用 B 的项目 id、`event_id`、`conversation_id` 导出 | 只有表头；B 的任何值不出现在文件里 |
| 泄漏 | integration | 事件带成本、汇率、参考价的哨兵值 | 文件原始字节里不出现这些哨兵值 |
| 鉴权 | integration | 路由守卫（`tests/backend/test_customer_api.py`） | 匿名 401、ADMIN 403；只读客户 200 |
| 幂等重放 / 并发 / 事务中途失败 / Redis 丢失 | — | 不适用：只读、单条 SELECT、不入队 | — |

后端，钱包导出（`tests/backend/test_customer_wallet_export_api.py` 新文件，同上参数化）：

| 风险/需求 | 测试层级 | 场景 | 预期结果 |
| --- | --- | --- | --- |
| 正常路径 | integration | 充值（调账贷方）、两笔用量扣费、一笔调账借方 | 200、表头恰好 7 列、4 行按 `wallet_sequence` 倒序；每行与 `GET /customer/wallet/transactions` 的同名键逐字相等；用量行 `event_id` 有值，调账行为空 |
| 按记账日期取 | integration | 三笔流水的 `created_at` 分别在马来西亚日 1 日 23:59、2 日 00:00、2 日 23:59（UTC 写库）；导出 2 日..2 日 | 恰好后两笔 |
| 区间边界 | integration | 区间 93 天、缺 `to_date`、`to_date` 早于 `from_date` | 422 `VALIDATION_ERROR` |
| 上限 | integration | 上限 monkeypatch 成 3；4 笔与 3 笔 | 422 `EXPORT_TOO_LARGE` / 200 |
| 扫描上限 | integration（MySQL） | 30 000 笔流水，1 毫秒超时 | 422 `QUERY_TOO_BROAD` |
| 不读敏感列 | unit | 断言导出的 SELECT 语句不含 `users` 表、`description`、`metadata_json`、`balance_before`（只断言方言无关的片段） | 通过 |
| 泄漏 | integration | 调账带 `description` 哨兵串、操作者是管理员、幂等键已知 | 文件里不出现哨兵串、管理员邮箱、幂等键 |
| 金额精度 | integration | 余额到 `999999999999.99999999` 的一笔 | `amount`、`balance_after` 原样 |
| 缺钱包 | integration | 删掉钱包行（测试里绕过触发器的现有写法） | 500 `INTERNAL_ERROR` |
| 数据库断连（钱包导出） | integration | 钱包存在性检查通过、拿到租约之后，让 `list_transactions_for_customer_export` 抛 `sqlalchemy.exc.OperationalError` | 同用量导出的「数据库断连」：500 `INTERNAL_ERROR`、带 `request_id`、有 `customer_export_failed`、没有 `Unhandled exception`、之后租约空闲 |
| 租户越权 | integration | A、B 各有流水 | A 的文件里只有 A 的行 |
| 鉴权 | integration | 路由守卫 | 同上 |

迁移（`tests/backend/test_wallet_created_index_migration.py` 新文件，照 0026 的测试写法：迁移场景只在 MySQL 上跑，SQLite 只用模型 metadata 建表后断言索引存在、列顺序、非唯一）：

| 风险/需求 | 测试层级 | 场景 | 预期结果 |
| --- | --- | --- | --- |
| 迁移可重跑 | integration（MySQL） | ① 从 0026 升到 0027；② 索引已存在（手工建好、形状正确）时 upgrade；③ 同名索引形状不对（列或列顺序不同、唯一）时 upgrade；④ 索引已被手工删除时 downgrade | ① 一条 `ALTER TABLE wallet_transactions ADD INDEX`；② 不发 DDL、版本前进；③ 抛错、消息点名索引与处理方法、不发 DDL、版本不变；④ 不发 DDL、不报错、版本回到 0026 |
| 索引可用 | integration（MySQL） | `SHOW INDEX` 与按租户 + 日期区间查询的 `EXPLAIN` | 列顺序正确；`possible_keys` 含新索引（不断言最终选择） |
| 迁移往返 | integration（MySQL） | upgrade → downgrade → upgrade | 每一步结构与模型一致；触发器仍在；流水与钱包行不变 |

前端（vitest + Testing Library）：下载辅助函数的成功（对象 URL、文件名）与失败（blob 里的 JSON 信封解析出错误码与 `request_id`）；用量页按钮用当前筛选拼参数、日期无效时不可用、两种专门错误文案；钱包页导出区的默认日期、按钮与错误文案；都不请求 `/api/v1/admin/`。

## 8. 迁移与上线

- **数据迁移**：只有 `0027` 加一个二级索引，不改数据。
- **锁表与性能**：在线 DDL，见 §2「数据库」；写入代价是每条流水插入多维护一个二级索引条目，`created_at` 插入后不再更新（流水不可变）。
- **兼容窗口**：两个新接口都是新增路由，不改既有接口的行为与响应。
- **部署顺序与拆分**（每个任务合并后都能单独通过 CI 并部署）：
  1. `AIH-TASK-113`（数据库）：迁移 `0027` 与模型声明，`docs/database-schema.md` 记索引。
  2. `AIH-TASK-114`（后端，另写 `docs/deployment.md` §3.3 的一句资源与代理说明，并给 `deploy/nginx/billing.conf` 的 `location /api/` 加 `proxy_pass_header X-Accel-Buffering;`）：`app/core/csv_export.py`（格式、公式注入防护、行数上限、导出租约与 `ExportLeaseMiddleware`、响应头）、`app/main.py` 挂中间件与接口一，`docs/api.md` 新增「客户导出」一节（含 CSV 格式说明）。不依赖 113。
  3. `AIH-TASK-115`（后端）：接口二与 `list_transactions_for_customer_export`，`docs/api.md` 补钱包导出。依赖 113（索引）与 114（CSV 模块）。
  4. `AIH-TASK-116`（前端）：下载辅助函数与用量页导出按钮，带浏览器只读步骤。依赖 114。
  5. `AIH-TASK-117`（前端）：钱包页导出区，带浏览器只读步骤。依赖 115 与 116（复用下载辅助函数）。
- **回滚或前滚**：接口与前端 revert 即可；`0027` 的 `downgrade` 只删这个索引，没有任何写入方依赖它（旧代码不用它）。
- **部分部署**：113 上线而 115 未上线：多一个没人用的索引。114 上线而 116 未上线：接口可用，页面没有按钮。
- **监控**：沿用 `customer_usage_query_timeout` 日志；无新增告警维度。负责人 Kelvin。

## 9. 方案取舍

| 方案 | 优点 | 风险 | 未采用原因 |
| --- | --- | --- | --- |
| 采用：同步、一条 SELECT（`LIMIT 上限 + 1`）、内存里拼好再发，进程内至多 1 个导出 | 一个快照；错误时状态码准确（文件发出前已知成败）；不落盘，没有留存问题；复用既有查询与白名单；内存上界确定 | 单请求内存峰值 ≤ 128 MiB；超过 2 万行要客户拆区间；多个客户同时导出时后来者要等几秒重试 | — |
| 备选 F：不设并发上限，只靠行数上限 | 没有 `EXPORT_BUSY` | 内存随并发数线性叠加，几个并发的大导出就能让单进程的 API 容器 OOM，AI API 一起中断 | v1 审查阻断项；导出不是高频操作，几秒的排他等待可以接受 |
| 备选 G：阻塞排队（`acquire` 带超时） | 客户少见忙碌错误 | 等待的请求各占一个线程池线程；排队的请求一多会挤占 AI 与摄取的线程 | 立刻失败、前端提示重试更可控 |
| 备选 H：拼好文件即释放（v2） | 实现最简单 | 慢客户端下载期间响应体仍在内存，后来的导出接着生成，多份叠加，内存上界不成立 | v2 审查阻断项 |
| 备选 I：用 Starlette 的 `BackgroundTask` 在发送后释放 | 不用写中间件 | 发送抛错（客户端断开）或被取消时后台任务不运行，租约永久泄漏，之后所有导出都 503 | 最外层中间件的 `finally` 覆盖正常、抛错、取消三种结局 |
| 备选 J：在响应子类的 `__call__` 里释放（v3） | 不改 `app/main.py` | 内层 `send` 返回不等于外层发完（`BaseHTTPMiddleware`），释放早于响应体离开应用 | v3 审查阻断项 |
| 备选 N：整份响应体作为一条消息发送（v7） | 最简单 | uvicorn 的 `send` 先等排空再写，一条 8 MB 消息写进传输缓冲后立刻返回，租约释放时整份文件还在缓冲里，慢连接上可叠加多份 | v7 审查阻断项；分段后残留不随文件大小增长 |
| 备选 M：只在 `billing.conf` 设 `proxy_max_temp_file_size 0`，或只在应用里发 `X-Accel-Buffering: no` | 改动更少 | 前者只管本项目这一层，外层共享代理照样缓冲并写临时文件；后者被 `billing_nginx` 消费掉，外层收不到 | 两层都要不缓冲，`proxy_pass_header` 一行就够 |
| 备选 L：把 `Exception` 的处理也放进租约内（例如在租约中间件里自己生成 500） | 不改处理函数 | 中间件里要复制一份 500 信封与日志逻辑，和 `handle_unexpected_error` 两处维护；且仍要在 `except` 外抛 / 发送才能放掉回溯 | 处理函数内转成 `AppError` 复用现有内层处理器，口径只有一份 |
| 备选 K：持有超过 N 秒就接管（v3） | 防泄漏 | 活跃的慢发送也会被接管，连续接管时并发无上界 | v3 审查阻断项；最外层 `finally` 让泄漏只剩「连接一直不结束」一种，它挡导出但不叠加内存 |
| 备选 A：流式响应（边查边写，按游标分页多次查询） | 内存小，可以不设或设大上限 | 响应头 200 已经发出后，后面某页超时或出错只能截断文件，客户拿到一份看起来正常、实际缺行的对账文件；多次查询不是同一个快照（除非整段放进一个可重复读事务，长事务又拖住清理） | 截断的对账文件比报错更糟。Kelvin 选的「同步 + 行数上限」用一次性生成实现，流式不是必要条件 |
| 备选 B：异步生成文件、通知下载 | 没有行数与时间限制 | 要建导出任务表、存储、留存期与安全删除（spec §112）、下载鉴权，范围大一个量级 | Kelvin 选同步；2 万行对现阶段的客户足够，`EXPORT_TOO_LARGE` 常见时再另开闸门 |
| 备选 C：`.xlsx` | 原生 Excel 类型、无公式注入（单元格按文本写） | 新增依赖，Worker 不能装，要管理员 PR；多一层测试 | Kelvin 选先只做 CSV |
| 备选 D：钱包导出不加索引，用 `(tenant_id, wallet_sequence)` 扫这个租户的全部流水再按日期过滤 | 不改结构 | 每条计费事件都有一行流水，活跃客户的流水量与用量事件同一量级；按日期取一段要扫这个客户从开户起的全部流水，很快就稳定超过 2 秒预算，按钮对老客户等于不可用 | 一个二级索引的代价远小于这个 |
| 备选 E：钱包导出按 `created_at` 排序（顺着索引读、不用排序） | 省掉排序 | `created_at` 不保证与 `wallet_sequence` 同序（时间在锁外取），`balance_after` 逐行可能接不上，客户对账时看到余额跳动 | 账本顺序以序号为准；排序成本受 2 秒预算约束 |

## 10. 未决问题与假设

- **未决问题**：无。Kelvin 2026-10-26 已拍板：只做 CSV；请求级用量与钱包流水；同步 + 行数上限；金额原样 8 位。
- **尚未验证的假设**：
  - 20 000 行在生产 MySQL 上一条 SELECT 能在 2 秒内取完（按索引范围扫描加回表）：0026 的扫描上限测试用 30 000 行配 1 毫秒超时验证的是「超时会被正确转换」，不是正常速度；正常速度在 AIH-TASK-114 部署后用夹具租户实测一次（`docs/perf-baseline.md` 不要求）。如果不成立，结局是确定的 422 `QUERY_TOO_BROAD`（客户缩短区间），不是错误数据。
  - 128 MiB 上界对生产数据成立：测试用的是每列取最大长度的合成行，真实行只会更短。若 `tracemalloc` 测得超过 128 MiB，AIH-TASK-114 停下说明，由新版本设计降低 `EXPORT_MAX_ROWS`，不在实现里自行调。
  - 外层代理照 `X-Accel-Buffering` 关闭缓冲：外层是 `vps_infra` 的共享边缘代理，不在本仓库，2026-10-26 读过它对本站的配置（见 §2「代理不缓冲」）。若以后外层加了 `proxy_ignore_headers X-Accel-Buffering`，或在两层之间插入别的会缓冲的代理，大导出会在那一层写临时文件（nginx 默认单个响应上限 1 GiB、响应结束即删除）；`docs/deployment.md` 的资源说明（AIH-TASK-114 写的那一句）同时写明这条依赖，改外层代理时要核对。本仓库这一层由 §7「代理配置」用例钉住。
  - uvicorn 的发送语义：`send` 在写暂停时先等排空再写、断开后直接返回——以 uvicorn 0.52.4 的 `h11_impl` 为准（2026-10-26 读过源码）；§7「慢读连接持有租约（真实 uvicorn）」实测它，升级 uvicorn 时这条用例会先红。
  - Starlette 中间件语义：`add_middleware` 后加的在外层、`BaseHTTPMiddleware` 把同一个 `scope` 传给内层——§7「中间件顺序」与「发送阶段持有租约（经真实中间件栈）」两条用例实测它们；升级 Starlette 时这两条会先红。
  - 单进程部署：`EXPORT_MAX_CONCURRENT` 是进程级的；以后 API 改成多进程（uvicorn `--workers`）时，总并发 = 进程数 × 1，内存限额要按此重算。AIH-TASK-114 在 `docs/deployment.md` 的资源限额说明（§3.3）里写一句：API 进程里同一时刻至多一个客户导出、峰值 ≤ 128 MiB，改成多进程时按进程数重算。
- **需要谁拍板**：无。
- **如果假设错误**：最坏是导出失败（422 / 503），不会给出错误或缺行的文件，也不影响记账（只读、不加锁、2 秒预算）；进程级导出租约只在整个请求结束时由最外层中间件释放、不接管，保证同一时刻只有一份导出的内存在进程里。

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
| v1 | 2026-10-26 | 初稿 | — |
| v2 | 2026-10-26 | ① 新增「上限与资源边界」：进程级 `BoundedSemaphore(1)`，不阻塞获取，忙时 503 `EXPORT_BUSY` + `Retry-After: 5`、不发查询；生成路径不留多余副本，`tracemalloc` 钉住 20 000 行峰值 ≤ 64 MiB；OOM 时的恢复（容器重启、只读无残留）。§1 完成标准、§3 INV-1、§5 两行、§7 两行、§9 备选 F / G、§10 两条假设同步。② 月度用量汇总导出改为**明确延后**（登记 PR 写进「后续计划」），删去「用请求级导出代替」的说法；会话摘要导出同样延后。③ §7 补公式注入六种前缀（含制表符与回车）与非前缀的单元测试 | Codex 设计审查 v1 的三个阻断项：并发导出无内存边界可使同进程 AI API 中断；月度汇总与 spec §73 不符；公式注入测试未覆盖制表符与回车 |
| v3 | 2026-10-26 | 并发上限从「生成期间持有的信号量」改为「导出租约」：持有到响应体交给服务器——处理函数出错时释放，成功时交给 `ExportResponse`，在其 ASGI `__call__` 的 `finally` 里释放（覆盖正常、发送抛错、取消）；释放只认自己的 token；租约持有超过 120 秒视为泄漏、下次获取时接管并记 `customer_export_lease_expired`；写明 nginx 默认缓冲让发送阶段有界。§1 完成标准第 5 条、§3 INV-1、§5 三行、§7 四行单元测试、§9 备选 H / I、§10 一条假设同步 | Codex 设计审查 v2 的阻断项：拼好文件即释放，慢客户端下载期间响应体仍占内存，后来的导出可继续生成，单进程内存上界不成立 |
| v4 | 2026-10-26 | 租约的唯一释放点改为最外层的纯 ASGI 中间件 `ExportLeaseMiddleware`（`app/main.py` 最后一个 `add_middleware`）：请求开始时在 `scope` 放租约槽，处理函数只 `hold(token)`，`try: await app(...) finally: slot.release()`；去掉 `ExportResponse` 与 120 秒接管，改为只记 `customer_export_lease_held_long` 警告；写明释放时响应体已交给 uvicorn。§1 第 5 条、§3 INV-1、§5 三行、§7 五行（经真实中间件栈暂停外层 `send`、断开 / 取消 / 处理函数异常、中间件顺序、不接管、槽只释放自己的 token）、§9 备选 J / K、§10 一条假设同步 | Codex 设计审查 v3 的两个阻断项：内层 `send` 返回不代表外层发完；按时间接管会接管活跃发送，连续接管无上界 |
| v5 | 2026-10-26 | 两个导出处理函数从拿到租约起捕获查询、投影、拼 CSV 的任何 `Exception`：记 `customer_export_failed`（带栈），离开 `except` 块、置空持有数据的局部变量后抛 `CustomerExportFailed`（`AppError`，500 `INTERNAL_ERROR`，信封与通用 500 相同），由内层 `ExceptionMiddleware` 在租约内发出；不捕获 `BaseException`。§5 一行、§7 两行（经真实中间件栈暂停 500 的发送、抛出的错误不链回原异常）、§9 备选 L 同步 | Codex 设计审查 v4 的阻断项：`Exception` 的处理器挂在 `ServerErrorMiddleware`（租约中间件外层），未处理异常会让租约先于 500 发送释放，回溯仍引用导出数据 |
| v6 | 2026-10-26 | ① 单个导出内存上界由 64 MiB 改为 128 MiB，行数上限仍为 20 000（Kelvin 选择）；`tracemalloc` 测试的「最长形态」写定：模型列宽的 ASCII 字符串、每行每列独立对象、token 取 `2^63 − 1`、金额与 `quantity` 取最大 Decimal、建行在 `tracemalloc.start()` 之后、BOM 与 `encode` 计入。② 公式注入的文本列改为逐个列出，`quantity` 不在其中（它是数值列）。③ §10 的多进程说明由 AIH-TASK-114 写进 `docs/deployment.md` §3.3，§8 第 2 步同步 | 登记前预审（`worker.design_precheck`，导出 `aa43a0a`）对 AIH-TASK-114 的四条发现：64 MiB 按最长形态估算会被突破；`quantity` 同时被算作文本列与数值列；`docs/deployment.md` 无任务负责；最长形态未定义 |
| v7 | 2026-10-26 | 导出响应加 `X-Accel-Buffering: no`，`deploy/nginx/billing.conf` 的 `location /api/` 加 `proxy_pass_header X-Accel-Buffering;`，让本项目与外层共享代理两层都不缓冲、不写临时文件；租约因此持有到下载结束，慢客户端由 nginx `send_timeout` 兜底。§2「代理不缓冲」「慢客户端」、§5 一行、§6「留存」、§7 两处（响应头断言、代理配置用例）、§8 第 2 步、§9 备选 M、§10 一条假设同步 | Codex 设计审查 v6 的阻断项（该轮因审查期间工作区被改动未发布，结论成立）：依赖 nginx 代理缓冲会把约 8 MB 的导出写进代理临时文件，`Cache-Control: no-store` 不阻止，客户明细明文落盘且慢下载累积占用磁盘 |
| v8 | 2026-10-26 | 响应体改由 `ExportCsvResponse` 按 64 KiB 分段（`memoryview`，不复制）发送，每段之前 uvicorn 按流控等排空，最后一段返回时缓冲至多约 128 KiB，租约之后才释放；更正「客户端断开时 `send` 抛错」为「断开后 `send` 直接返回」。§2 三处、§2「CSV 格式」响应类、§7 两行（分段发送、真实 uvicorn 慢读连接）、§9 备选 N、§10 一条假设同步 | Codex 设计审查 v7 的阻断项：uvicorn 的 `send` 先等排空再写，整份 8 MB 作为一条消息写进传输缓冲后立刻返回，租约释放时文件仍在缓冲，慢连接上可叠加 |
| v9 | 2026-10-26 | §5「数据库不可用」一行由「503（现有处理）」改为 500 `INTERNAL_ERROR`（`CustomerExportFailed`）：现有客户接口不把查询时的连接错误映射成 503（`DatabaseUnavailable` 只用于 `/readyz`），导出按 §2「所有错误响应都在租约内发出」也得到 500；数据库未配置的 503 不变。其他章节不变 | 登记前预审（`worker.design_precheck`，导出 `4254116`）对 AIH-TASK-114 的发现；PR #440 Codex 审查阻断项：v8 写 503 而登记契约用勘误改成 500，失败语义的变化须升版重审，不能由契约勘误替代 |
| v10 | 2026-10-26 | §7 两张后端表各加一行「数据库断连」：拿到租约之后查询抛 `OperationalError`，断言 500 `INTERNAL_ERROR`（不是 503）、带 `request_id`、由内层处理器在租约内发出（有 `customer_export_failed`、没有 `Unhandled exception`）、之后租约空闲 | Codex 设计审查 v9 的阻断项：v9 改了数据库断连的对外结果，却没有对应的测试场景 |

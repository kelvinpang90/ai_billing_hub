# AIH-TASK-234..241 设计：客户导出与管理端毛利分析导出的 Excel（.xlsx）版本（已批准 v1）

> **来源**：设计闸门 Issue #753。本文件是 `APPROVED: design v1` 那一版正文的**逐字副本**（送审时状态行为 `READY_FOR_REVIEW`）。审查：Codex 额度不可用期间由 Claude Code 审查。v1 一次审查通过、没有阻断项；送审前的登记预审报出的问题（整数规则、临时文件清理步骤、回车读回口径、月度汇总的失败日志、重启 api 的影响面）已改进 v1。
> 放进仓库，是因为 OpenClaw Worker 在沙箱里不联网、读不到 GitHub Issue。
> **实现以本文件为准**；与 Issue 不一致时，以 Issue #753 上被批准的 v1 为准。设计要改，就回到 Issue 升版本、重新过闸门，不要直接改本文件。
> 前置：依赖 `openpyxl>=3.1,<3.2` 由管理员 PR #748 加入，并已装进 Worker 检查用的 Python 3.12。

状态：`READY_FOR_REVIEW`
负责人：Claude
**设计版本**：`v1`
对应需求：spec §73「Export Functionality」（V1 支持 CSV 与 Excel；请求级用量、会话摘要、钱包流水、月度用量汇总；管理端导出可含内部成本，客户端必须排除）、§128（Phase 5「customer usage exports」）、§130（Phase 7「CSV/Excel exports」）、§80（金额精度）、§109（展示时区）、§112（导出需要明确的留存、安全删除与容量限制）、§141.5（AI 模块与本平台同进程）、`REQ-PRIV-001`（INV-8）、`REQ-PRIV-002`（INV-7）、Invariant 1、7、8、10
前置设计（都已批准、仍然生效，本设计只换编码，不改它们的查询、列、筛选、上限与错误）：#435 v10（`docs/design/AIH-TASK-113-customer-export.md`：请求级用量与钱包流水 CSV、导出租约、20 000 行、分段发送、代理不缓冲）、#457 v1（`docs/design/AIH-TASK-119-customer-usage-summary-export.md`：月度汇总 CSV，不占租约）、#460 v2（`docs/design/AIH-TASK-121-customer-conversations.md`：会话摘要 CSV）、#709 v2（`docs/design/AIH-TASK-221-margin-analytics.md`：管理端毛利分析 CSV，AIH-TASK-223，尚未实现）
目标 PR：待开（拆成 `AIH-TASK-234`..`AIH-TASK-241`，见 §11）

## 0. 准入判定

| 任务涉及 | 要填的范围 |
| --- | --- |
| 钱包 / 账本 / 定价 / 汇率 / 支付 / 幂等 / 状态机 | **全部章节** |
| 用量摄取 / 认证与会话（集成侧与平台侧）/ Webhook | §1–§7（**§6 不可省** —— 密钥存储与轮换、鉴权、租户过滤、日志脱敏都在那一节） |
| 前端 / 文档 / CI / 脚本 | **不走本闸门**，直接开 PR |
| 既不碰钱、也不碰用量摄取 / 认证与会话 / Webhook 的后端改动 | **不走本闸门**，直接开 PR |

本任务档位：`全部`。理由：只读、不写任何业务表，但它让客户的计费额与钱包流水、以及管理端的内部成本与毛利以**一种新的文件格式**离开系统（INV-7 的白名单要在新格式里逐列成立），金额要在一种以双精度存数的表格格式里保持 `Decimal` 精度（INV-10），新增一个第三方依赖，并且这个依赖会在 API 进程里**写临时文件、多占 CPU**（INV-1 的冲击面、spec §112 的「导出文件安全删除」）。前置的两个导出闸门（#435、#709）都按「全部」走，本批沿用。只读带来的「不触碰」在 §3 逐条说明。

---

## 1. 目标与边界

### 要解决的问题

spec §73 要求 V1 导出支持 CSV 与 Excel。现状只有 CSV：

1. 客户门户四个 CSV 导出（请求级用量、钱包流水、月度用量汇总、会话摘要）都已上线（`app/api/customer.py:265-332`、`:390-461`、`:491-522`、`:564-629`），三份前置设计都把 `.xlsx` 明确推到「后续计划」（`docs/design/AIH-TASK-113-customer-export.md:42`、`AIH-TASK-119-customer-usage-summary-export.md:44`、`AIH-TASK-121-customer-conversations.md:49`），`docs/api.md:808` 写着「只做 CSV；`.xlsx` 不在这里」。
2. 管理端毛利分析的 CSV 导出（AIH-TASK-223）同样「Excel 推迟，与客户端 Excel 一起做」（`docs/design/AIH-TASK-221-margin-analytics.md:46`、`:70`、`:389`）。
3. `docs/TODO.md` Phase 5「客户端用量导出」（:343）与 Phase 7「CSV / Excel 导出」（:371）都因缺 Excel 而没勾；planning-v1 的后续计划里两条「待登记」（:45、:49）等的就是本批。

CSV 用 Excel 打开能用，但有已知的毛病：长数字串与日期样的文本会被 Excel 自动改写，分隔符随系统区域设置变化，非 ASCII 依赖 BOM。`.xlsx` 里每个单元格的类型由文件决定，打开即是原值。

### 已定（任务说明，2026-11-16）

| 问题 | 决定 |
| --- | --- |
| 依赖 | `openpyxl`，由**管理员先单独 PR** 加进 `pyproject.toml`（Worker 不装依赖）；本设计按「依赖已在」写，前置见 §8 |
| 范围 | 客户门户现有的**全部** CSV 导出各加一个 `.xlsx` 版本；管理端毛利分析的 CSV 导出（AIH-TASK-223）加 `.xlsx` 版本；本批排在 AIH-TASK-225 之后 |
| 原则 | xlsx 与对应 CSV **同一查询、同一列、同一筛选、同一行数上限与错误码，只换编码** |
| 前端 | 每个「Export CSV」旁边加「导出 Excel」按钮；有浏览器验收 |

### 我定的（未单独问，审查者重点看，§10 也列出）

- **接口形状**：不加新路径，在现有五个导出接口上加一个查询参数 `format`（`csv` 缺省 / `xlsx`）。同一个处理函数、同一次查询、同一个列常量，只在最后一步选编码器 —— 「只换编码」由构造保证，而不是靠两份代码保持一致（§9 备选 A）。
- **金额写文本单元格**（结论与理由见 §2「单元格类型」）；整数列只有规范十进制且不超过 15 位时写数字单元格，其余退回文本；时间写文本。
- **公式注入防护与 CSV 逐字一致**：同一批文本列、同一组前缀、同一个单引号；xlsx 的文本单元格另外显式标为字符串类型。
- **openpyxl 总是先把工作表写进临时文件**（§1「现有行为」第 5 条）：给 `api` 容器的 `/tmp` 挂内存文件系统（tmpfs，上限 128 MiB），导出数据不落宿主机磁盘；失败时按固定步骤删除临时文件（§2「临时文件」）。
- **文件字节不要求可重现**：工作表内容可重现（测试钉住），`docProps/core.xml` 的时间与 ZIP 条目时间是生成时刻。
- **浏览器验收只查按钮在且可用，不点**（不下载文件）；下载接口的 200 由 API 测试覆盖。点一次月度汇总的 Excel 下载作为可选项列在 §10。

### 可观察的完成标准

1. 下面五个接口带 `format=xlsx` 时返回一个 `.xlsx` 附件，带 `format=csv` 或不带时返回的 CSV 与今天**逐字节相同**：
   - `GET /api/v1/customer/usage-events/export`
   - `GET /api/v1/customer/wallet/transactions/export`
   - `GET /api/v1/customer/usage-summary/export`
   - `GET /api/v1/customer/conversations/export`
   - `GET /api/v1/admin/usage-analytics/export`（AIH-TASK-223 交付后）
2. 同一组参数下，xlsx 只有一个工作表，第一行是与 CSV 表头相同的列名，之后的行数、行序与每个单元格的值都与 CSV 相同（规则：CSV 空字段 ↔ xlsx 空单元格；整数列在 xlsx 里是数字、`str()` 后与 CSV 字段逐字相同；不能写进 XML 的控制字符换成 U+FFFD，见 §10 已知限制 1；回车可能读回成换行，见 §10 已知限制 2）。
3. 行数上限、`EXPORT_TOO_LARGE`、`QUERY_TOO_BROAD`、`EXPORT_BUSY`（含 `Retry-After: 5`）、`INTERNAL_ERROR`、钱包缺失的 500、月度汇总缺 `month` 的 422，xlsx 与 CSV 完全相同；xlsx 与 CSV 共用同一个进程级导出租约（月度汇总两种格式都不占）。`format` 只认 `csv`、`xlsx`，其他值 422 `VALIDATION_ERROR`。
4. 金额、`quantity`、比率与平均值在 xlsx 里是**文本**，字符与 CSV、JSON 接口逐字相同（8 位小数，不经浮点）。
5. 客户的四个 xlsx 不含任何成本、汇率、参考价、毛利或内部 id：列与 CSV 恒同一个常量（§6 逐列对照）。
6. 生成 xlsx 时客户数据不写进宿主机磁盘；临时文件在成功与失败后都被删除（唯一例外是不含行数据的一个窗口，§10 已知限制 7）；单个 xlsx 导出的 Python 堆峰值仍 ≤ 128 MiB（测试钉住），临时文件 ≤ 96 MiB（测试钉住，tmpfs 上限 128 MiB）。
7. 门户用量页、月度汇总区、钱包页导出区、会话页，以及管理端用量分析页，每个「Export CSV」旁边各有一个导出 Excel 的按钮，用与 CSV 按钮相同的条件下载。

### 明确不做什么

- **不改**任何导出的查询、筛选、列、列顺序、行序、上限、错误码、租约与分段发送（前置设计原样有效）。
- **不做**样式、列宽、冻结首行、筛选按钮、多工作表、合计公式；不在文件里加说明行或说明表（会改变行数）。
- **不做**异步导出、导出文件落盘或导出历史（与 #435 相同）。
- **不做**对账单、收据、管理端其他页面的 Excel；不做 `.xls`、`.ods`。
- **不加**新路径、不改路由清单（`tests/backend/test_customer_api.py:516` 的路由集合、`tests/backend/test_admin_customers_api.py` 的 `EXPECTED_ADMIN_ROUTES` 都不变）。
- **不写审计**（与 CSV 相同：客户读自己的数据；管理端导出按 Kelvin 2026-11-14 的决定只记 INFO，见 #709）。

### 现有行为与证据

1. **CSV 写出**：`app/core/csv_export.py:212-238` 的 `write_csv(columns, rows, text_columns)`，单元格只收 `str | None`；文本列以 `_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")`（:61）开头时前加 `'`；`csv_response`（:269-280）的文件名只许 `[A-Za-z0-9._-]`，响应头带 `Content-Disposition: attachment`、`Cache-Control: no-store`、`X-Accel-Buffering: no`；`ExportCsvResponse`（:241-266）按 64 KiB 分段发送。上限 `EXPORT_MAX_ROWS = 20_000`（:49）、租约（:117-183）、最外层释放中间件（`app/main.py:187-189`）。
2. **处理函数**：三个大导出（钱包、用量、会话）都是「鉴权 → 校验 → 取会话工厂（503）→ 拿租约（503 `EXPORT_BUSY`）→ `hold` → `try` 里查询、判上限、拼文件、构造响应 → 意外异常在 `except` 外抛 `CustomerExportFailed`」（`app/api/customer.py:265-332` 等）；月度汇总不拿租约、不包 `try`（:491-522）。
3. **列与文本列**：`app/schemas/customer_usage.py:65-100`（18 列，11 个文本列）、`:105-112`（月度 4 列，文本列只有 `date_myt`）；`app/schemas/customer_wallet.py:32-44`（7 列，3 个文本列）；`app/schemas/customer_conversations.py:49-68`（9 列，3 个文本列）；管理端 31 列与文本列（`group_key`、`group_name`）由 AIH-TASK-223 放进 `app/schemas/usage_analytics.py`（#709 §2「接口二」）。单元格都由 `*_export_cells` 从客户白名单视图的 JSON 值转成字符串（例：`app/schemas/customer_usage.py:365-376`）。
4. **数值的取值范围**：摄取与 AI 模块把每条事件的 token 限在 `10**12` 以内（`app/schemas/usage_ingest.py:47`、`app/ai_api/client_rules.py:14`），但数据库只有 `>= 0` 的 CHECK（`app/models/usage.py:162-167`）；`wallet_sequence` 的接口上界是 `2**63 - 1`（`app/schemas/wallet_transactions.py:28`）；金额列是 `DECIMAL(20,8)`，最多 20 位有效数字，现有内存测试的最长形态就用 `Decimal("999999999999.99999999")`（`tests/backend/test_csv_export.py:540`）。
5. **openpyxl 的写出行为**（读的是本机已装的 openpyxl 3.1.2 源码，不是仓库文件）：
   - 不论是否 `write_only`，每个工作表都先写进 `tempfile.NamedTemporaryFile(prefix="openpyxl.", delete=False)` 建的临时文件（`openpyxl/worksheet/_writer.py` 的 `create_temporary_file`：建好文件、**关闭句柄**、把路径记进模块级列表 `ALL_TEMP_FILES`），保存时复制进 ZIP 后才删除（`openpyxl/writer/excel.py` 的 `write_worksheet` → `writer.cleanup()`：`os.remove(out)` 并从 `ALL_TEMP_FILES` 移除）；中途出错不删，只在进程退出时由 `atexit` 兜底。
   - write_only 工作表的写出器是**懒建**的：`create_sheet` 只建 `WriteOnlyWorksheet` 对象、不碰文件；第一次 `append`（或 `close`）时 `_get_writer` 才 `WorksheetWriter(ws)`，它在构造函数里先 `create_temporary_file()` 得到路径 `out`（字符串），再建生成器 `xf = get_stream()` 并 `next(xf)`：生成器里 `with xmlfile(out)` **打开文件句柄**并写出 `<worksheet>` 开始标签；构造函数返回后才赋给 `ws._writer`，随后 `write_top()`。之后各行经 `ws._rows`（`_write_rows` 生成器，持有 `sheetData` 元素）写进 `xf`。`WorksheetWriter.close()` 就是 `xf.close()`（生成器收到 `GeneratorExit`，退出 `with xmlfile`，句柄关闭）。
   - `save_workbook` 把 `properties.modified` 设为当前 UTC 时刻；ZIP 条目的时间也是写入时刻。
   - 字符串写成内联字符串（`t="inlineStr"`），没有共享字符串表。
   - 字符串赋值时：含 `[\x00-\x08\x0B\x0C\x0E-\x1F]` 抛 `IllegalCharacterError`；超过 32 767 字符**静默截断**；以 `=` 开头且长于 1 的推断为**公式**；恰好等于 `#N/A`、`#REF!` 等七个错误码之一的推断为**错误值**（`openpyxl/cell/cell.py` 的 `check_string` 与 `_bind_value`）。
6. **可写进客户导出的自由文本**：项目名与公司名只做首尾去空白、长度 1–255（`app/schemas/customers.py:57` 的 `_Name`），**可以含控制字符**；`request_id` / `conversation_id` 只许 `[A-Za-z0-9._:-]`（`app/schemas/usage_ingest.py:32`）。
7. **容器**：`api` 是一个 uvicorn 进程、PID 1（`Dockerfile:73`），`mem_limit` 768m、`cpus` 1.0（`docker-compose.yml:55-56`），没有 tmpfs 与只读根文件系统；`app/` 里没有任何 `tempfile`、`UploadFile`、`/tmp` 的用法（`grep` 0 命中）。部署用 `docker compose up -d --no-build`（`deploy/deploy.sh:366`），配置变了会重建容器。
8. **前端**：`frontend/src/api/download.ts:112` 的 `downloadFile(url, fallbackFilename, signal)` 带令牌取 blob、按 `Content-Disposition` 存盘、失败按信封解析；四个门户导出各用一个 `useMutation`（例：`frontend/src/features/portal/UsagePage.tsx:393-403`、`:260-329`，`WalletPage.tsx:93-160`，`ConversationsPage.tsx:167-176`、`:347-360`）；按钮文案在 `frontend/src/i18n/locales/en.json`（例：`:1350`、`:1358`、`:1429`、`:1456`）。
9. **验收脚本**：`scripts/acceptance/admin_customers.mjs` 已有 `check_portal_usage_export`、`check_portal_monthly_export`、`check_portal_wallet_export`、`check_portal_conv_export`，都「只查不点、验收不下载文件」（文件头 :114-142）；步骤名上限 32 字符（:20）；脚本「不写任何文件」，CDP 方法有固定清单（:89-94）。

**本机实测**（Windows、Python 3.11、openpyxl 3.1.2、没有 lxml；一次性测量，不是仓库测试）：20 000 行最长形态 × 18 列（用量导出的形状，字符串取列宽上限、token 取 `999999999999`），write_only、文本单元格全部显式字符串类型：生成约 **3.2 秒**（不显式类型约 2.5 秒；同一批行写 CSV 约 0.2 秒）；工作表 XML **34 MB**；压缩后 1.7 MB（合成数据重复多，真实数据压缩率更低）；不计行本身的 Python 堆峰值约 2 MB。开着 `tracemalloc` 时同一段要 23 秒。生产是 Linux 与 Python 3.12，以 AIH-TASK-235 在 CI 上的测量为准（写进 PR）。

## 2. 设计概要

### 数据流

```text
GET …/export?format=xlsx（令牌）
  → 客户接口：format 由 FastAPI 在处理函数之前判（422）；管理端：require_admin 与 parse_analytics_query 之后判（422）
  → 与 CSV 完全相同的：鉴权 → 校验 → 503 → 租约（月度汇总不拿）→ 一次只读查询（LIMIT 20 001）→ 上限
  → 同一个 *_export_cells 投影成 str | None
  → format == csv：write_csv → csv_response（与今天逐字节相同）
    format == xlsx：write_xlsx → xlsx_response
        write_xlsx：openpyxl write_only → 工作表 XML 流式写进 /tmp（tmpfs）→ 压缩进内存里的 ZIP → 删除临时文件 → bytes
  → 分段发送 → 最外层中间件释放租约
（无状态变化、无财务效果、无出站事件、无审计、不落盘）
```

### 参数 `format`

| 接口 | 怎样声明 | 非法值 |
| --- | --- | --- |
| 客户四个导出 | `ExportFormatParam = Annotated[Literal["csv", "xlsx"], Query(alias="format")]`，Python 参数名 `export_format`，缺省 `"csv"`；定义在 `app/api/customer.py` 现有参数别名旁 | FastAPI 在处理函数之前 422 `VALIDATION_ERROR`，字段 `query.format`（与这些接口其他「形状」类参数同一时序：现有的日期、`provider`、`month` 都在鉴权之前判） |
| 管理端毛利分析导出 | `export_format: str \| None = Query(default=None, alias="format")`；在 `require_admin` 与 `parse_analytics_query` **之后**判：`None` 或 `"csv"` 走 CSV，`"xlsx"` 走 xlsx，其他抛 `InvalidUsageEventQuery("format")` | 422 `VALIDATION_ERROR`，`query.format`；未登录 / 非 ADMIN 先 401 / 403（与该接口其他参数一致，#709 §7「校验」） |

- 只认小写的 `csv`、`xlsx`；`XLSX`、`xls`、空串都是 422。
- 不带或 `format=csv`：处理函数走与今天**完全相同**的代码路径，输出逐字节相同。
- 参数只决定编码器与文件扩展名；查询、筛选、上限、租约、错误处理的代码路径对两种格式是同一段。

### 各接口的改法（只改「拼文件 → 构造响应」两行）

三个大导出（`app/api/customer.py` 的钱包、用量、会话）在现有 `try` 块里：

```python
if export_format == "xlsx":
    body = write_xlsx(COLUMNS, cells, TEXT_COLUMNS, INTEGER_COLUMNS, sheet_title=SHEET)
    rows = None
    response = xlsx_response(f"<同一个文件名主干>.xlsx", body)
else:
    …原样的 write_csv / csv_response 两行…
```

- 两个分支都在拿到租约之后的同一个 `try` 里；`write_xlsx` 的任何意外异常走现有的 `customer_export_failed` 日志与 `CustomerExportFailed`（500，在租约内发出）。
- 月度汇总：同一个位置分支，**不拿租约、不包 `try`**（与 CSV 一致，意外错误由通用 500 处理）。
- 管理端：在 AIH-TASK-223 的处理函数里同样分支；成功日志 `admin_usage_analytics_exported` 与失败日志不变（`extra` 仍只有 `user_id`、`group_by`、`row_count`，不加格式）。
- 路由装饰器的 `response_class=ExportCsvResponse` 不变；`responses={200: {"content": {"text/csv": {}, XLSX_MEDIA_TYPE: {}}}}` 让 OpenAPI 列出两种媒体类型。

文件名（主干与 CSV 相同，只换扩展名）：

| 接口 | xlsx 文件名 | 工作表名 |
| --- | --- | --- |
| 用量 | `usage-events_<from_date>_<to_date>.xlsx` | `usage-events` |
| 钱包 | `wallet-transactions_<from_date>_<to_date>.xlsx` | `wallet-transactions` |
| 月度汇总 | `usage-summary_<YYYY-MM>.xlsx` | `usage-summary` |
| 会话 | `conversations_<from_date>_<to_date>.xlsx` | `conversations` |
| 管理端 | `usage-analytics_<group_by>_<from>MYT_<to>MYT.xlsx`（`<from>` / `<to>` 规则同 #709 §2「文件」） | `usage-analytics` |

工作表名都是程序常量（≤ 31 字符、不含 `[]:*?/\`），不拼任何输入。

### 新模块 `app/core/xlsx_export.py`（AIH-TASK-235）

```python
XLSX_MEDIA_TYPE: Final = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
XLSX_MAX_TEXT_LENGTH: Final = 32_767                         # Excel 单元格上限
_NUMERIC_INTEGER: Final = re.compile(r"0|[1-9][0-9]{0,14}")  # 只用 fullmatch；15 位 = Excel 的有效数字上限

def write_xlsx(
    columns: Sequence[str],
    rows: Iterable[Sequence[str | None]],
    text_columns: Collection[str],
    integer_columns: Collection[str],
    *,
    sheet_title: str,
) -> bytes: ...

class ExportXlsxResponse(ExportCsvResponse):
    media_type = XLSX_MEDIA_TYPE          # 非 text/*，Starlette 不追加 charset

def xlsx_response(filename: str, body: bytes) -> ExportXlsxResponse: ...
```

- **输入与 `write_csv` 相同**：同一个列元组、同一个 `rows`（`str | None`）、同一个 `text_columns`，外加 `integer_columns`。`rows` 里出现 `str | None` 以外的类型、或某行长度与列数不等，抛 `TypeError` / `ValueError`（与 `write_csv` 同一约定，`csv_export.py:225-229`：类型不对 `TypeError`，`zip(strict=True)` 长度不等 `ValueError`）。`integer_columns` 与 `text_columns` 有交集或含不在 `columns` 里的名字，抛 `ValueError`。
- **工作簿**：`openpyxl.Workbook(write_only=True)`，恰好一个工作表 `sheet_title`；第一行是 `columns`（文本单元格），之后每行一条。不设样式（整数单元格的数字格式除外）、列宽、冻结、筛选、文档标题；`docProps` 里只有 openpyxl 的缺省值（`creator`、两个时间）。
- **保存**：`wb.save(io.BytesIO())`，ZIP 只在内存里；返回 `getvalue()` 后丢弃缓冲。
- 模块不碰数据库、不打日志（与 `csv_export.py` 相同）。

### 临时文件（AIH-TASK-235 的清理步骤）

openpyxl 在第一次 `append` 时建临时文件（§1「现有行为」第 5 条）；成功时由 openpyxl 在保存时删除。`write_xlsx` 用 `try / except BaseException` 包住从 `Workbook(write_only=True)` 到 `save` 的全过程；进入 `except` 时，对 `wb.worksheets` 里的每个工作表（本模块只建一个）**按下面的顺序**清理，每一步各自包在 `contextlib.suppress(Exception)` 里（清理失败不掩盖原异常），然后原样重抛原异常：

1. `rows = getattr(ws, "_rows", None)`；不为 `None` 时 `rows.close()`（结束 `_write_rows` 生成器，退出它持有的 `sheetData` 元素）。
2. `writer = getattr(ws, "_writer", None)`；为 `None` 时这个工作表没有可达的临时文件，跳过 3–4。
3. `writer.xf.close()`：关闭写 XML 的生成器，`with xmlfile(out)` 随之退出，**文件句柄关闭**。`writer.out` 本身只是路径字符串，没有句柄可关；先关句柄再删，Windows 上才删得掉，Linux 上也避免删掉仍在写的文件。
4. `path = writer.out`：是 `str` 且 `os.path.exists(path)` 时 `os.remove(path)`；`path` 仍在 `openpyxl.worksheet._writer.ALL_TEMP_FILES` 里时把它移除。成功路径的 `cleanup()` 也做这两件事；文件已被 openpyxl 删掉的情形（保存时复制完之后才出错），这一步什么也不做。

**够不到的那一个窗口**：`WorksheetWriter.__init__` 在 `create_temporary_file()` 之后、构造函数返回（赋给 `ws._writer`）之前出错（例如 `next(xf)` 打开文件失败、`MemoryError`），临时文件已建而没有任何对象引用它。这时文件里至多是 `<worksheet>` 开始标签、**没有任何行数据**（表头与数据行都在 `ws._writer` 赋值之后才写）；它留在 `api` 的内存 tmpfs 里，进程退出时由 openpyxl 的 `atexit` 删除、容器重启时随 tmpfs 清空（AIH-TASK-234）。列为 §10 已知限制 7；不为它去扫描临时目录（会误删并发的月度汇总导出正在写的文件，§9 备选 K）。

这些步骤用到 openpyxl 3.1 的私有属性（`_rows`、`_writer`、`xf`、`out`、`ALL_TEMP_FILES`），所以依赖写成 `openpyxl>=3.1,<3.2`（§8）；升级要经过 AIH-TASK-235 的临时文件用例，属性名变了用例会红。

### `app/core/csv_export.py` 的配合改动（AIH-TASK-235，行为与输出字节不变）

- 新增公开函数 `formula_guarded(value: str) -> str`（以 `_FORMULA_PREFIXES` 之一开头时前加 `'`，否则原样），`write_csv` 改为调用它。
- 新增公开函数 `export_attachment_headers(filename: str) -> dict[str, str]`（文件名校验 + 三个响应头），`csv_response` 改为调用它；`xlsx_response` 也用它。
- `test_csv_export.py` 不改任何断言、照常通过。

### 单元格类型（逐列规则）

每个单元格按下表选一种，顺序是先公式防护、再类型、再字符清洗：

| 列 | xlsx 单元格 | 值 |
| --- | --- | --- |
| `text_columns` 里的列 | 文本（显式字符串类型，`t="inlineStr"`） | `formula_guarded(value)` —— 与 CSV 逐字相同，然后做字符清洗 |
| `integer_columns` 里的列，且 `_NUMERIC_INTEGER.fullmatch(value)` 成立（`0`，或不带前导零、至多 15 位的 ASCII 十进制数字串，即 `0 ≤ v ≤ 999 999 999 999 999`） | 数字，数字格式 `0`（整数显示，不变成科学计数法） | `int(value)`；这种写法下 `str(int(value)) == value`，数字单元格读回后的十进制写法与 CSV 字段逐字相同 |
| `integer_columns` 里的列，其余任何值（超过 15 位、带前导零如 `007`、带空白或换行如 `5\n`、负号、小数点、非 ASCII 数字、空串） | 文本（显式字符串类型，经字符清洗） | 原样的字符串，与 CSV 字段逐字相同。不抛错：正常数据只会落到「超过 15 位」这一种；其余形态说明上游视图变了，由 AIH-TASK-236、237 的逐格一致测试发现，而不是让导出 500 |
| 其余列（金额、`quantity`、比率、平均值、时间、日期、`TOTAL` 标签、口径列） | 文本（显式字符串类型） | 原样（与 CSV 相同：这些列在 CSV 里也不加前缀），然后做字符清洗 |
| 任何列的 `None` | 空单元格 | — |

- **必须用 `fullmatch`**：`re.match(r"^[0-9]+$", "5\n")` 会成立（`$` 在末尾换行之前匹配），`"007"` 也会成立，而 `str(int(...))` 与原串不同，写出的数字单元格会与 CSV 字段不一致。`[0-9]` 在 Python 的 `str` 正则里只匹配 ASCII 数字。
- **为什么所有字符串都显式标为字符串类型**：openpyxl 的推断会把 `=` 开头的值写成公式、把恰好等于 `#N/A` 等七个错误码的值写成错误值（§1「现有行为」第 5 条）。公司名、项目名可以就叫 `#N/A`；显式类型保证文件里绝不出现 `<f>`（公式）与 `t="e"`（错误值）。实测显式类型多花约 30% 时间（§1 实测），换来没有推断。
- **字符清洗**（只作用于 xlsx 的文本单元格）：把 XML 1.0 不允许的字符（`U+0000`–`U+0008`、`U+000B`、`U+000C`、`U+000E`–`U+001F`、`U+FFFE`、`U+FFFF`、代理码点）各换成一个 `U+FFFD`。不换的话 openpyxl 抛异常（xlsx 导出 500 而 CSV 成功，违反「同一错误码」），或写出 Excel 打不开的 XML。制表符、换行、回车保留（回车读回见 §10 已知限制 2）。
- **长度**：清洗后长于 32 767 字符抛 `ValueError`，不让 openpyxl 静默截断。现有导出的文本列最长 255 字符（项目名、公司名），到不了这个上限。

**各导出的整数列**（新增常量，放在各自列常量旁边；其余非文本列都是文本单元格）：

| 导出 | `*_EXPORT_INTEGER_COLUMNS` |
| --- | --- |
| 用量（`app/schemas/customer_usage.py`） | `input_tokens`、`output_tokens`、`cache_creation_input_tokens`、`cache_read_input_tokens` |
| 钱包（`app/schemas/customer_wallet.py`） | `wallet_sequence` |
| 月度汇总（`app/schemas/customer_usage.py`） | `request_count`、`conversation_count` |
| 会话（`app/schemas/customer_conversations.py`） | `request_count`、`input_tokens`、`output_tokens` |
| 管理端（`app/schemas/usage_analytics.py`） | `event_count`、`token_event_count`、`input_tokens`、`output_tokens`、`cache_creation_input_tokens`、`cache_read_input_tokens`、`prepaid_event_count`、`internal_event_count`、`paid_payment_count`、`gateway_fee_unknown_count` |

### 金额为什么写文本（结论）

**结论：金额、`quantity`、比率、平均值一律写文本单元格，字符与 CSV、JSON 接口逐字相同。** 理由：

1. **Excel 的数字是双精度，只保 15 位有效数字。** `DECIMAL(20,8)` 最多 20 位：现有内存测试的最长形态 `999999999999.99999999` 在 Excel 里读成 `1000000000000`；余额到千万级（8 位整数 + 8 位小数 = 16 位）就开始丢最后一位。这不是边界情形里的 bug，而是这种格式装不下我们的精度。
2. **导出是对账用的数据**（#435 §1：「舍入后加总会与账本差几分」）。写成数字，文件本身（`<v>` 里的十进制文本）是精确的，但用户在 Excel 里看到和加总的已经是双精度值，与账本、对账单、门户页面对不上时没有办法解释。
3. **INV-10**：我们的代码不经浮点（金额从头到尾是 `Decimal` 生成的定点字符串）；写成数字单元格等于把「转浮点」这一步交给了客户的软件，而且是在客户看不到的地方。
4. **三方一致**：同一笔金额在 JSON、CSV、xlsx 里是同一个字符串，测试可以逐字比对，客户也可以逐字比对。

代价：Excel 在这些格上标「以文本形式存储的数字」，`SUM` 直接求和得 0。`docs/api.md` 写明，并给出求和写法（例如 `=SUMPRODUCT(VALUE(range))`；这一步由客户自己选择转成双精度）。月度汇总与管理端导出本来就有合计行，不需要客户自己加。

**整数为什么可以写数字**：token、序号、计数是整数，规范写法且 `≤ 999 999 999 999 999` 时双精度精确、Excel 显示也精确，客户能直接求和；超过 15 位或写法不规范（带前导零、空白、符号等）时退回文本，不丢一位、与 CSV 逐字相同。每条事件的 token 在应用层限在 `10**12`（13 位），正常数据下不会触发退回；数据库没有上界，所以退回规则必须有。

**时间为什么写文本**：Excel 的日期时间没有时区、只到毫秒；我们的时间列是不带时区的 UTC、保留微秒，列名带 `_utc`。写成 Excel 日期会丢微秒，并让用户误以为是本地时间。

### 公式注入防护

- **与 CSV 一致**：同一组文本列（各导出的 `*_EXPORT_TEXT_COLUMNS`，xlsx 直接读同一个常量）、同一组前缀（`=`、`+`、`-`、`@`、制表符、回车）、同一个单引号，由同一个函数 `formula_guarded` 完成。xlsx 文本单元格的值与 CSV 字段逐字相同（含回车的值读回时回车可能变成换行，§10 已知限制 2，不影响开头的 `'`）。
- **xlsx 多一层**：所有字符串单元格显式标为字符串类型，文件里没有公式单元格。前缀仍然保留：类型化的文本格不会被求值，但用户在 Excel 里编辑这一格、回车之后，Excel 会把 `=HYPERLINK(…)` 这样的文本重新当公式解析；单引号让它在那时仍是文本（不加前缀的取舍见 §9 备选 D）。
- 前缀 `'` 在 xlsx 里是值的一部分，会显示出来（§10 已知限制 5）。
- 数值与时间列不加前缀，与 CSV 相同：负金额 `-0.04120000` 保持原样。

### 可重现性

- 同一份数据导出两次：**工作表 XML（`xl/worksheets/sheet1.xml`）与除 `docProps/core.xml` 之外的每个 ZIP 成员逐字节相同**；`docProps/core.xml` 的 `created` / `modified` 与每个 ZIP 条目的修改时间是生成时刻，所以**整个文件的字节不同**。
- 这些时刻只含生成时间，不含任何业务数据（与 HTTP 响应的 `Date` 头同一信息）。
- 不追求整个文件字节可重现：导出不是财务快照（对比收据 PDF，spec 要求按快照重现），做到要绕开 `save_workbook` 并改写 ZIP 条目，多依赖 openpyxl 内部（§9 备选 I）。

### tmpfs（AIH-TASK-234）

- `docker-compose.yml` 的 `api` 服务加 `tmpfs: ["/tmp:size=128m,mode=1777"]`，只加在 `api`（只有它生成导出）。
- 效果：openpyxl 的临时文件在内存里，**不写宿主机磁盘**；容器重启或重建时清空（uvicorn 是 PID 1，进程崩溃即容器重启）。spec §112 要求导出文件有安全删除：数据从未落盘，删除就是释放内存页，没有要擦除的磁盘块。
- tmpfs 的页计入容器的内存限额：生成期间最多多占一个工作表 XML 的大小（最坏形态约 34 MB，非 ASCII 项目名与 XML 转义最坏可到约 60 MB），128 MiB 是上限而不是预留。大导出由租约串行，月度汇总的文件只有几 KB。
- 失败路径：`write_xlsx` 按上文四步删除；进程被杀则随容器重启清空。
- 本地开发的 compose 同样带 tmpfs；CI 与 Worker 的测试写在各自机器的临时目录（合成数据），不受影响。

**tmpfs 写满时怎么判断、怎么处置（定下来，AIH-TASK-241 照写进 runbook）**：

- **能写满的途径**：单个导出的工作表 XML 有 ≤ 96 MiB 的测试上界、大导出由租约串行，正常路径写不满 128 MiB；能累积的只有 §10 已知限制 7 的残留文件（每个只有开始标签，几百字节）与 `app/` 之外的东西写进 `/tmp`（`app/` 自己不用 `/tmp`，§1「现有行为」第 7 条）。所以「满」是异常，先查证再动手。
- **表现**：xlsx 导出 500（`OSError: No space left on device` 出现在对应失败日志的栈里，日志名见 §5「各导出的失败日志」）；CSV 导出、JSON 接口与 AI 模块不受影响（它们都不写 `/tmp`）。
- **第一步，确认是否真的满**（只读）：`docker compose exec api df -h /tmp`（看 `Use%`）与 `docker compose exec api ls -la --time-style=full-iso /tmp`（看是什么文件、多大、多旧）。没满就不是这个原因，回到失败日志的栈。
- **第二步，不重启的处置**：残留的是 `openpyxl.` 开头、修改时间早于 10 分钟的文件时，`docker compose exec api find /tmp -maxdepth 1 -name 'openpyxl.*' -mmin +10 -delete`。10 分钟远长于任何一次生成（最坏数秒）加发送（客户端不读时 nginx 60 秒断开），不会删到正在写的文件；这一步不影响任何请求。删后再 `df` 确认。
- **第三步，只有第二步之后仍满（占满的不是 openpyxl 的文件）才重启 `api`**：`docker compose restart api`。影响面（以代码为准）：uvicorn 优雅排空最长 85 秒、容器宽限 100 秒（`Dockerfile:73` 的 `--timeout-graceful-shutdown 85`、`docker-compose.yml:234`、`docs/deployment.md:155`），期间新请求失败、进行中的 AI 调用在排空窗口内尽量完成；`app/main.py:110-135` 的停机钩子对 AI 模块内存里的暂存缓冲（`app/ai_api/held.py` 的 `HeldBuffer`）做最后一次落库，限时 5 秒，落不完的条目只留日志，要按 runbook「AI 模块用量 outbox」一节用 `ai-usage-held import --from-log-line` 从消息为 `AI usage not persisted` 的日志行补录（`docs/runbook.md:52-57`）。计费、门户、管理端同进程，同样中断。时机：选低峰；不紧急时（只影响 xlsx 导出，CSV 可替代）先告诉客户用 CSV，等低峰再重启。重启后 tmpfs 清空，`df` 复核。
- **绝不能做**：在 `api` 里删 `/tmp` 下不明来源或 10 分钟内的文件；为腾空间去调 tmpfs 的 `size` 而不改设计；用 `docker compose down` 代替 `restart`（会删网络与容器，扩大影响）。

### 资源边界（INV-1）

- **并发**：与 CSV 共用一个进程级导出租约，xlsx 不增加名额；月度汇总两种格式都不占（文件恒为一个月）。
- **Python 堆**：xlsx 是流式写出，堆峰值不高于 CSV 路径（CSV 在 `StringIO` 里拼整份文本，xlsx 写进临时文件，内存里只有压缩后的 ZIP）。AIH-TASK-235 用与 CSV 相同的 `tracemalloc` 方法钉住 ≤ 128 MiB（20 000 个最长形态行计入峰值）。
- **临时文件**：AIH-TASK-235 钉住最长形态下工作表 XML ≤ 96 MiB，留在 tmpfs 128 MiB 之内。
- **CPU**：xlsx 生成是纯 Python，最坏形态本机约 3 秒（CSV 0.2 秒）。`api` 的配额是 1 核，期间同进程的其他请求（含 AI 模块）分时间片；AI 调用以等待上游为主，表现为延迟抖动，不是失败。租约保证同一时刻至多一个；行数上限按原则与 CSV 相同，不为 xlsx 单独降（§9 备选 H）；AIH-TASK-235 把 CI 上的实测秒数写进 PR，超过 10 秒时停下在 PR 里说明，由 Kelvin 决定是否改设计（§10）。
- **响应**：压缩后的 xlsx 比同样数据的 CSV 小，分段发送与代理不缓冲照旧（`ExportXlsxResponse` 继承 `ExportCsvResponse` 的分段 `__call__`）。

### 事务边界

与 CSV 完全相同：一个普通只读会话、一次查询（钱包另有一次找钱包），不提交、不加锁、不写。编码在会话关闭之后做（不持连接写 xlsx）。

### 时间语义

不变：时间列是不带时区的 UTC 文本（与 JSON 逐字相同）、日期列是马来西亚日 `YYYY-MM-DD` 文本；文件名与工作表名里没有时间。`docProps` 的时刻是生成时刻（UTC），只是文件元数据。

### 金额精度与舍入

不舍入、不转换：金额是视图里的 8 位小数字符串原样写成文本；整数只在 `fullmatch` 成立时用 `int()` 转换（精确，且 `str()` 回去与原串相同）；比率与平均值是 `ratio_text` 的字符串原样。全程没有 `float`。

### 门户与管理端页面

共同规则（AIH-TASK-238、239、240）：

- `frontend/src/api/download.ts`（AIH-TASK-238）新增：`export type ExportFormat = "csv" | "xlsx";` 与 `withExportFormat(url, format)`：`csv` 原样返回（地址与今天逐字相同），`xlsx` 在查询串末尾加 `format=xlsx`。`downloadFile` 不改。
- 各 api 模块的导出查询类型加可选字段 `format?: ExportFormat`（缺省 `csv`）；地址函数经 `withExportFormat`；回退文件名按格式换扩展名（`.csv` / `.xlsx`）。月度汇总的函数加可选参数：`customerUsageSummaryExportUrl(month, format = "csv")`、`customerUsageSummaryExportFilename(month, format = "csv")`、`exportCustomerUsageSummary(month, signal?, format = "csv")`。现有调用与测试不变。
- 每个导出区在「Export CSV」按钮**右边**加一个导出 Excel 按钮；两个按钮共用该区现有的那一个 `useMutation`（变量里带 `format`）：任一在下载中两个都不可点，加载状态只显示在被点的那个按钮上；失败提示、文案与清除规则与今天相同（错误码不分格式，失败文案不提格式）；按钮可用的条件与「Export CSV」相同（日期区间无效、按 `event_id` 点查、月份不合法时都不可用）。
- 文案（`en.json`，新键）：`portal.usage.export.buttonXlsx`、`portal.wallet.export.buttonXlsx`、`portal.conversations.export.buttonXlsx` 都是 `Export Excel`；`portal.usage.monthly.buttonXlsx` 是 `Export monthly summary (Excel)`；管理端用量分析页用与 AIH-TASK-224 的 Export CSV 按钮同一前缀的键，值 `Export Excel`。现有按钮文案不改（验收脚本按它们找按钮）。
- 页面只请求自己一侧的路径（门户不请求 `/api/v1/admin/`，管理端不请求 `/api/v1/customer/`）。

| 任务 | 页面与按钮 |
| --- | --- |
| AIH-TASK-238 | 用量页工具栏（`Export CSV` 右边）；月度汇总区（`Export monthly summary` 右边） |
| AIH-TASK-239 | 钱包页导出区（`Export CSV` 右边）；会话页工具栏（`Export CSV` 右边） |
| AIH-TASK-240 | 管理端用量分析页（AIH-TASK-224 的 `Export CSV` 右边，用同一组已应用条件） |

### 浏览器验收

沿用 `.platform/commands.yaml` 的 `acceptance.browser`（`scripts/acceptance/admin_customers.mjs`），`writes: false`，`timeout_seconds: 300`。新步骤**只查不点**：不下载文件（与四个 CSV 导出步骤同一约束）。为了让每个任务的步骤数在 2–4 个之内，门户的新步骤**自己经门户导航进页**（与 `check_portal_receipts` 同一写法），不依赖别的步骤停在哪个页面。

| 步骤（≤ 32 字符） | 任务 | 断言 |
| --- | --- | --- |
| `check_portal_usage_xlsx` | AIH-TASK-238 | 跑过 `login_customer`（否则 `assertion_failed`）；点门户导航的「Usage」（href `/portal/usage`）；页面落定（门户外壳在、用量一块不再显示加载中，显示加载失败是 `page_error`）；用量一块里文字为 `Export Excel` 的按钮恰好一个且可用；月度汇总区（aria-label `Monthly summary`）里文字为 `Export monthly summary (Excel)` 的按钮恰好一个且可用；两个按钮都不点 |
| `check_portal_wallet_xlsx` | AIH-TASK-239 | 跑过 `login_customer`；点门户导航的「Wallet」（href `/portal/wallet`）；导出区（aria-label `Export transactions`）里 `Export Excel` 恰好一个且可用（默认区间有效）；不点 |
| `check_portal_conv_xlsx` | AIH-TASK-239 | 跑过 `login_customer`；点门户导航的「Conversations」（href `/portal/conversations`）；会话一块不再加载中后，`Export Excel` 恰好一个且可用；不点 |
| `check_analytics_xlsx` | AIH-TASK-240 | 跑过 `open_usage_analytics`（否则 `assertion_failed`），不自己导航；同一页上文字为 `Export Excel` 的按钮恰好一个且可用；不点 |

找不到按钮是 `element_missing`；按钮在但一直不可用或不止一个是 `assertion_failed`。已有步骤行为不变。

## 3. 不变量影响矩阵

| 不变量 | 是否触碰 | 可能怎样被破坏 | 设计控制 | 验证方式 |
| --- | --- | --- | --- | --- |
| INV-1 中心故障不中断 AI | 是 | AI 模块与导出同进程：xlsx 生成多占 CPU（最坏约 3 秒）与内存（tmpfs 的页计入 768m），并发的 xlsx 叠加可能 OOM | 与 CSV 共用进程级租约（至多一个）；Python 堆 ≤ 128 MiB 与工作表 XML ≤ 96 MiB 都由测试钉住；tmpfs 上限 128 MiB；行数上限不变；CI 上测 CPU 秒数写进 PR、超过 10 秒停下 | §7「内存上界」「临时文件大小」「错误一致」；§8 容量说明 |
| INV-2 事件不重复扣费 | 否 | 只读用量事件，不改状态、不计费 | — | — |
| INV-3 支付不重复入账 | 否 | 管理端导出只读支付合计（#709），不改支付 | — | — |
| INV-4 余额只经账本变动 | 否 | 只读账本行，不写钱包 | — | — |
| INV-5 历史账本不可变 | 否 | 只读 | — | — |
| INV-6 事件保留版本引用 | 否 | 只读事件上已冻结的金额，不重算 | — | — |
| INV-7 客户不可见成本毛利 | **是** | xlsx 多出列、多出工作表、文档属性里带内部数据；管理端 xlsx 被客户令牌调到 | 客户 xlsx 的列恒为 CSV 的同一个列常量（同一个处理函数传入）；恰好一个工作表、ZIP 成员集合固定；`docProps` 只有 openpyxl 缺省值；管理端导出仍只在 `/api/v1/admin`、第一条语句 `require_admin`；AIH-TASK-222 的守卫测试仍对同一组列常量生效；§6 逐列对照 | §7「INV-7 列与成员」「管理端校验与鉴权」 |
| INV-8 租户不可互访 | 是 | xlsx 分支绕开租户条件 | 不新增查询：同一个处理函数、同一次以令牌主体租户为条件的查询；分支只在编码那一步 | §7「跨租户」 |
| INV-9 对话内容不入库 | 否 | 列不变，没有请求或回复内容；临时文件在 tmpfs、用完即删 | 列白名单不变 | §7「INV-7 列与成员」 |
| INV-10 金额用 Decimal | **是** | 金额写成数字单元格后被 Excel 读成双精度；整数超过 15 位被截；非规范整数串（`007`、`5\n`）被 `int()` 改写；代码里经 `float` | 金额、比率、平均值写文本（与 CSV 逐字相同）；整数只有 `fullmatch(0\|[1-9][0-9]{0,14})` 时才写数字（`str(int(v)) == v`），其余退回文本；写出模块不接受 `str \| None` 以外的输入 | §7「金额与整数精度」 |
| INV-11 event_id 至多一次财务效果 | 否 | 无财务效果 | — | — |
| INV-12 定稿对账单不可变 | 否 | 不读也不写对账单 | — | — |
| INV-13 状态与事件原子提交 | 否 | 没有状态变化与出站事件 | — | — |
| INV-14 队列丢失不毁持久工作 | 否 | 同步生成，不入队、不落盘 | — | — |

## 4. 状态与并发

### 状态

无状态机：不新增状态、不改任何状态。

### 并发

- **两个导出同时到达**（任意格式、任意接口、客户与管理端混合）：仍由同一个租约串行，后到者立刻 503 `EXPORT_BUSY`（`app/core/csv_export.py:117-149`）。
- **月度汇总与大导出同时**：月度汇总不拿租约，两者各自的临时文件由 `NamedTemporaryFile` 取唯一名字，互不干扰；失败清理只删自己工作表 `writer.out` 那一个路径，不扫目录，不会删到别人的文件。openpyxl 的全局临时文件清单是模块级列表，追加与删除在 GIL 下是原子的。
- **工作簿对象**：每个请求自己的 `Workbook`，不跨线程共享。
- **幂等**：只读 GET，重放只随数据变化；没有幂等键、没有重试层。

## 5. 失败与恢复矩阵

| 失败点 | 对外结果 | 数据最终状态 | 是否可重试 | 恢复来源 | 告警 |
| --- | --- | --- | --- | --- | --- |
| `format` 非法 | 422 `VALIDATION_ERROR`（`query.format`）；客户接口在鉴权之前、管理端在鉴权之后 | 不变；不拿租约、不查询 | 改参数后 | — | — |
| 其他校验、未登录、非本角色 | 与 CSV 相同 | 不变 | — | — | — |
| 数据库没配置 | 503，早于拿租约 | 不变 | 是 | 配好数据库 | 现有 |
| 有导出在跑 | 503 `EXPORT_BUSY`、`Retry-After: 5` | 不变 | 是 | — | 持有超 120 秒时现有 WARNING |
| 行数 > 20 000 | 422 `EXPORT_TOO_LARGE`，不给部分文件 | 不变；租约释放 | 缩短区间 | — | — |
| 查询超过执行预算 | 422 `QUERY_TOO_BROAD` | 不变 | 缩短区间 | — | 现有 `customer_usage_query_timeout` |
| xlsx 生成出错（openpyxl 异常、tmpfs 满、`MemoryError`、超长文本） | 500 `INTERNAL_ERROR`（大导出由 `CustomerExportFailed` 在租约内发出；月度汇总由通用 500） | 这次的临时文件由 `write_xlsx` 按 §2「临时文件」四步删除；租约在请求结束时释放 | 是 | 重新导出；同样的错误反复出现按 runbook 排查（AIH-TASK-241）；tmpfs 满按 §2「tmpfs 写满时怎么判断、怎么处置」 | 带栈的 ERROR，日志名随导出不同，见下表 |
| tmpfs 写满 | xlsx 导出 500；CSV、JSON 接口与 AI 模块不受影响 | 不变 | 处置后是 | §2「tmpfs 写满时怎么判断、怎么处置」：先 `df` 确认，再删 10 分钟前的 `openpyxl.` 残留，仍满才在低峰重启 `api` | 同上 |
| 写出器构造到一半出错（§2「够不到的那一个窗口」） | 同上 500 | 留下一个不含行数据的临时文件在 tmpfs，进程退出或容器重启时清空 | 是 | 同上 | 同上 |
| 进程在生成中被杀（OOM） | 连接中断 | 容器重启，tmpfs 清空，不留文件；没有业务状态 | 是 | `restart: unless-stopped` 与健康检查 | 现有容器告警 |
| 客户端中途断开 | 无 | 不变；租约在最外层 `finally` 释放 | — | — | — |
| 控制字符出现在项目名 / 公司名 | 200；xlsx 里该字符为 U+FFFD | 不变 | — | — | — |
| Redis / Celery 丢失 | 不影响（本批不用队列） | 不变 | — | — | — |
| 外部服务 | 不调用任何外部服务 | — | — | — | — |
| 重复请求 | 同一内容（随数据变化） | 不变 | — | — | — |

### 各导出的失败日志（现有路径，xlsx 不改；CSV 今天就是这样）

| 导出 | 生成或查询中的意外异常 | 日志（logger） | 依据 |
| --- | --- | --- | --- |
| 用量、钱包、会话（占租约） | 在拿到租约之后的 `try` 里捕获，记日志后抛 `CustomerExportFailed` | `customer_export_failed`（`app.api.customer`，`logger.exception`，`extra` 只有 `path`） | `app/api/customer.py` 三个处理函数的 `except Exception` |
| 钱包导出在拿租约**之前**发现钱包缺失 | `RuntimeError(WALLET_MISSING)` 不经 `try`，由通用 500 处理 | `Unhandled exception`（`app.core.errors`，`extra` 只有 `path`） | `app/api/customer.py:283-285`、`app/core/errors.py:145-147` |
| 月度汇总（不占租约、不包 `try`） | 任何意外异常（含 xlsx 生成出错）由通用 500 处理 | `Unhandled exception`（`app.core.errors`，`extra` 只有 `path`，路径是 `/api/v1/customer/usage-summary/export`） | `app/api/customer.py:491-522`、`app/core/errors.py:145-147` |
| 管理端毛利分析 | 在拿到租约之后的 `try` 里捕获，记日志后抛 `CustomerExportFailed` | `admin_usage_analytics_export_failed`（`extra` 只有 `path`） | #709 §2「接口二」第 7 步（AIH-TASK-223） |

四种日志都带栈、不带参数值。查询超过执行预算（`QUERY_TOO_BROAD`）不是失败日志，是现有的 `customer_usage_query_timeout`（只有客户接口）。按日志排查 xlsx 失败时，月度汇总要找的是**路径为 `/api/v1/customer/usage-summary/export` 的 `Unhandled exception`**，而不是 `customer_export_failed`。

## 6. 数据与安全边界

- **鉴权与租户过滤**：不变。客户四个接口第一条语句 `require_customer`，租户只取令牌主体；管理端第一条语句 `require_admin`，跨租户是设计。`format` 不是租户参数，也不改变查询条件。
- **客户侧禁止的字段 —— 逐列对照**（xlsx 与 CSV 用同一个列常量，所以下表同时适用于两种格式）：

| 导出 | 列（按顺序） | 对照结论 |
| --- | --- | --- |
| 用量（18） | `id`（用量事件 `public_id`）、`event_id`、`request_id`、`conversation_id`、`feature`、`project_id`（项目 `public_id`）、`project_name`、`provider`、`model`、`usage_type`、`input_tokens`、`output_tokens`、`cache_creation_input_tokens`、`cache_read_input_tokens`、`quantity`、`unit`、`charged_amount`、`occurred_at_utc` | `charged_amount` 是 `billable_cost`（客户计费额，含税毛额）；没有 `estimated_provider_cost_myr`、`provider_source_cost`、`fx_rate_applied`、`reference_customer_price`、`markup`、定价规则或价格版本 id、自增 id |
| 钱包（7） | `id`（流水 `public_id`）、`wallet_sequence`、`transaction_type`、`amount`、`balance_after`、`event_id`、`created_at_utc` | `event_id` 只在用量流水时等于 `reference_id`；调账、系统更正、rebill 的内部幂等键、`description`、`metadata_json`、操作者、`balance_before` 都不在 |
| 月度汇总（4） | `date_myt`、`request_count`、`charged_amount`、`conversation_count` | 只有计费额与计数 |
| 会话（9） | `conversation_id`、`project_id`、`project_name`、`first_occurred_at_utc`、`last_occurred_at_utc`、`request_count`、`input_tokens`、`output_tokens`、`charged_amount` | 只有计费额、计数与时间 |
| xlsx 独有的部分 | 工作表名（程序常量）、`docProps/core.xml`（`creator` 为 openpyxl 缺省值、`created`、`modified`）、`docProps/app.xml`（应用名与版本）、样式与主题部件 | 不含租户、客户、项目的任何标识与内部字段；ZIP 成员集合固定（测试钉住） |

  管理端 31 列含估算成本、毛利、手续费（#709 §2「接口二」），这是设计：只在 `/api/v1/admin`、只给 ADMIN；`docs/api.md` 写明「含内部成本，不得转发给客户」（AIH-TASK-223 已写，AIH-TASK-237 补 xlsx）。
- **日志**：不新增日志。失败仍是现有两条 ERROR（带栈，只带路径）；管理端成功 INFO 的 `extra` 不变。日志里不带文件内容、筛选值、公司名、金额。
- **临时数据**：工作表 XML 只在 `api` 容器的 tmpfs 里存在几秒，成功或失败都删除（例外见 §10 已知限制 7，不含行数据），进程死亡随容器重启清空；不写宿主机磁盘，不进备份、不进镜像层。
- **文件名与工作表名**：只由校验过的日期、`group_by` 与常量拼出（同 CSV），不拼客户输入。
- **密钥**：不新增、不读取。
- **依赖**：新增 `openpyxl`（MIT），带来 `et_xmlfile`（MIT），都是纯 Python；不新增 C 扩展。
- **prompt / response**：不处理。
- **数据保留**：服务器不保存任何导出文件（与 #435 相同）；浏览器下载后的文件归客户自己管理。

## 7. 测试证据计划

**回车的比较口径**（本节所有涉及回车的读回比较统一照这一条，§10 已知限制 2）：值里含回车（`\r`）时，期望值与读回值两边都先 `replace("\r", "\n")` 再逐字比较；不含回车的值一律逐字比较。这条只放宽回车本身，其余字符（含开头的 `'`、换行、制表符）仍须逐字相同。

| 风险/需求 | 测试层级 | 场景 | 预期结果 | 任务 |
| --- | --- | --- | --- | --- |
| tmpfs 声明 | unit（解析 compose） | 读 `docker-compose.yml` | `api` 的 `tmpfs` 恰好是 `/tmp:size=128m,mode=1777`；其他服务没有挂到 `/tmp` 的 tmpfs | AIH-TASK-234 |
| CSV 重构不改行为 | unit | `test_csv_export.py` 全部用例 | 不改断言照常通过；`formula_guarded` 对六个前缀加 `'`、其余原样；`export_attachment_headers` 与 `csv_response` 的头逐字相同 | AIH-TASK-235 |
| 基本形状 | unit | 3 列（文本、整数、金额）、两行，其中有 `None` | 以 `PK` 开头；ZIP 成员集合恰好是固定的 9 个（`[Content_Types].xml`、`_rels/.rels`、`docProps/app.xml`、`docProps/core.xml`、`xl/workbook.xml`、`xl/_rels/workbook.xml.rels`、`xl/styles.xml`、`xl/theme/theme1.xml`、`xl/worksheets/sheet1.xml`）；`openpyxl.load_workbook(read_only=True)` 读出恰好一个工作表、名称正确；第一行是列名；文本列是 `str`、整数列是 `int`、`None` 读回 `None` | AIH-TASK-235 |
| 与 CSV 逐格一致 | unit | 同一组行分别 `write_csv` 与 `write_xlsx`（含公式前缀、非 ASCII、逗号、引号、换行、回车、空值） | 解析后逐格相等（规则：CSV 空字段 ↔ xlsx `None`；整数列 xlsx 的 `int` 经 `str()` ↔ CSV 的数字串；含回车的值按「回车的比较口径」） | AIH-TASK-235 |
| 公式与错误值不出现 | unit | 文本列值 `=1+1`、`+1`、`-1`、`@A1`、`\t=1`、`\r=1`；文本列与非文本列值 `#N/A`、`#REF!` | 文本列读回 `'=1+1`、`'+1`、`'-1`、`'@A1`、`'\t=1`、`'\r=1`（与 CSV 字段相同；`'\r=1` 按「回车的比较口径」，读回 `'\r=1` 或 `'\n=1` 都通过，关键是开头的 `'`）；`#N/A` 原样是字符串；工作表 XML 里没有 `<f>` 也没有 `t="e"` | AIH-TASK-235 |
| 金额与整数精度 | unit | 金额 `999999999999.99999999`、`0.00000001`、`-0.04120000`；整数列给 `"0"`、`"999999999999999"`、`"1000000000000000"`、`str(2**63 - 1)`、`"007"`、`"5\n"`、`" 5"`、`"1.5"`、`"-1"`、全角零（`chr(0xFF10)`） | 金额读回同一字符串（文本）；`"0"`、`"999999999999999"` 读回 `int`、数字格式为 `0`、`str(读回值)` 等于原串；其余各值都读回**同一个字符串**（文本单元格，不是数字），不抛错（`"5\n"` 只含换行、不含回车，逐字比较） | AIH-TASK-235 |
| 字符清洗与长度 | unit | 文本 `"\x01a"`、`"a\x0bb"`、`chr(0xFFFE)`、`"a\tb\nc"`、`"a\rb"`；32 768 字符的文本 | 不合法字符变 U+FFFD、不抛错；制表符与换行保留；`"a\rb"` 按「回车的比较口径」；超长抛 `ValueError` | AIH-TASK-235 |
| 输入约定 | unit | 单元格是 `int`、`Decimal`；行长度不等；`integer_columns` 与 `text_columns` 相交；`integer_columns` 含未知列 | `TypeError` / `ValueError`，与 `write_csv` 同一约定 | AIH-TASK-235 |
| 临时文件清理 | unit | 每个场景先把 `tempfile.tempdir` monkeypatch 到一个空的 `tmp_path`：①正常写出；②行迭代器在第 3 行抛 `RuntimeError`（写出器已建、已写两行、`xf` 与 `_rows` 都开着）；③行迭代器在第一条数据行之前抛（表头行已 `append`，写出器已建、`xf` 开着，只是还没有数据行）；④monkeypatch `openpyxl.worksheet._writer.WorksheetWriter.write_tail` 抛错（`ws._writer` 已赋值、`xf` 仍开着）；⑤monkeypatch `zipfile.ZipFile.write` 抛 `OSError`（工作表已关闭，复制进 ZIP 时失败，文件仍在） | 五种结局之后 `tmp_path` 里都没有任何文件；②–⑤ 原样传出注入的那个异常对象；每个场景结束后 `openpyxl.worksheet._writer.ALL_TEMP_FILES` 不含 `tmp_path` 下的路径。「够不到的那一个窗口」（§2「临时文件」）不写用例：它只能靠在 openpyxl 构造函数内部注入失败来造，归 §10 已知限制 7 | AIH-TASK-235 |
| 可重现 | unit | 同一组行写两次 | 除 `docProps/core.xml` 外每个 ZIP 成员逐字节相同；`core.xml` 里不含任何单元格的值 | AIH-TASK-235 |
| 内存上界 | unit | `tracemalloc.start()` 后建 20 000 个最长形态行（复用 `test_csv_export.py` 的 `longest_row` 与 `usage_cells` 函数，导入函数而不是夹具），按用量导出的列与文本列、整数列写 xlsx | 峰值 ≤ 128 MiB；读出 20 001 行；token 列（`2**63 - 1`）是文本、金额是文本 | AIH-TASK-235 |
| 临时文件大小 | unit | 同一批最长形态行（不开 `tracemalloc`） | ZIP 里 `xl/worksheets/sheet1.xml` 的未压缩大小 ≤ 96 MiB；用例打印生成秒数，PR 里报告 | AIH-TASK-235 |
| 响应 | unit | `xlsx_response` 的头；非法文件名；按 64 KiB 分段 | `Content-Type` 恰好是 xlsx 媒体类型（没有 charset）、`Content-Disposition`、`Cache-Control: no-store`、`X-Accel-Buffering: no`；非法文件名 `ValueError`；分段与 CSV 相同 | AIH-TASK-235 |
| 打包 | CI（packaging smoke） | 装好的 wheel 里 `import app.core.xlsx_export` | 成功（证明 openpyxl 随依赖装上） | AIH-TASK-235 |
| 四个客户接口正常路径 | API（SQLite） | 每个接口各一组有数据的请求：不带 `format`、`format=csv`、`format=xlsx` | 前两者响应体逐字节相同；xlsx 200、媒体类型、文件名 `.xlsx`、`no-store`、`X-Accel-Buffering: no`；工作表名；第一行等于列常量；与同参数 CSV 逐格一致（含回车的值按「回车的比较口径」）；整数列是数字 | AIH-TASK-236 |
| `format` 校验 | API | `format=XLSX`、`xls`、空串；未登录带 `format=xls` | 422 `VALIDATION_ERROR`、消息含 `query.format`；未登录也是 422（与这些接口其他形状参数同一时序） | AIH-TASK-236 |
| 错误一致 | API | 把 `EXPORT_MAX_ROWS` monkeypatch 为 2 造 3 行；注入 `QueryTooBroad`；预先占住租约；用 monkeypatch 让 `write_xlsx` 抛 `RuntimeError`；钱包缺失；月度汇总不带 `month` | 两种格式逐一相同：422 `EXPORT_TOO_LARGE`、422 `QUERY_TOO_BROAD`、503 `EXPORT_BUSY` + `Retry-After: 5`（月度汇总两种格式在租约被占时都 200）、500 `INTERNAL_ERROR` 且请求结束后 `export_lease_held()` 为假、钱包缺失 500 且没拿租约、月度 422 `query.month` | AIH-TASK-236 |
| 跨租户 | API | 租户 A 的令牌带租户 B 的 `project_id` 导出用量与会话 xlsx | 200，只有表头一行 | AIH-TASK-236 |
| INV-7 列与成员 | API | 四个 xlsx | 表头恰好等于各自列常量；与 AIH-TASK-222 守卫的禁用名单无交集；恰好一个工作表；ZIP 成员集合固定 | AIH-TASK-236 |
| 恶意与异常文本 | API | 项目名 `=HYPERLINK("x","y")`、`#N/A`、含 `\x01` | xlsx 读回 `'=HYPERLINK("x","y")`、`#N/A`（字符串）、`\x01` 处为 U+FFFD；CSV 不受影响 | AIH-TASK-236 |
| 管理端 xlsx | API（SQLite） | `group_by=customer` 两个客户加手续费：不带、`csv`、`xlsx` | 前两者逐字节相同；xlsx 31 列逐格等于 CSV；10 个整数列是数字；金额、比率、平均值是文本；GROUP 行在前、TOTAL 行恒在末尾；文件名 `.xlsx` | AIH-TASK-237 |
| 管理端校验与鉴权 | API | `format=xls`；未登录与客户令牌带 `format=xls` | 422 `query.format`；401 / 403 先于 422 | AIH-TASK-237 |
| 管理端错误一致 | API | 上限（monkeypatch 为 2）、租约被占、`write_xlsx` 抛错 | 与 CSV 相同的 422 / 503 / 500，租约释放；成功日志 `extra` 仍恰好三个键 | AIH-TASK-237 |
| 前端 api | unit（vitest） | 各地址函数带与不带 `format`；回退文件名；`withExportFormat` | CSV 地址与今天逐字相同；xlsx 地址末尾 `format=xlsx`；扩展名随格式 | AIH-TASK-238、239、240 |
| 前端按钮 | unit（Testing Library） | 两个按钮都在；点 Excel 用已应用条件与 `format: "xlsx"` 调导出；下载中两个都不可点、加载状态在被点的按钮上；失败文案与 CSV 相同并按原规则清除；条件无效时两个都不可用 | 与 §2「门户与管理端页面」一致；已有用例照常通过 | AIH-TASK-238、239、240 |
| 浏览器验收 | acceptance（只读） | `check_portal_usage_xlsx`；`check_portal_wallet_xlsx`、`check_portal_conv_xlsx`；`check_analytics_xlsx` | 按钮恰好一个且可用，不点 | AIH-TASK-238、239、240 |
| 事务中途失败 | — | 只读，没有部分写入 | 不适用：失败不留业务状态；临时文件见「临时文件清理」 | — |
| Redis / Celery 丢失 | — | 不使用队列 | 不适用 | — |
| 并发 | — | 租约由现有 `test_csv_export.py` 与 AIH-TASK-236 的租约用例覆盖 | 不另写行锁用例：没有锁 | — |

后端测试照仓库根 `CLAUDE.md`「写后端测试的约定」：不断言方言相关的 SQL 字面量（本批不新增 SQL）；新测试模块不转引用别的模块的夹具（只导入普通函数，现有导出测试已是这种写法，例：`tests/backend/test_customer_usage_summary_export_api.py:33-56`）；示例 uuid 全零。AIH-TASK-236、237 只跑 SQLite：查询代码路径与 CSV 是同一段、已由现有 CSV 导出测试的 MySQL 一半覆盖，xlsx 只改会话关闭之后的编码；这一点写进两份 PR。

## 8. 迁移与上线

- **前置（Worker 开工之前，管理员做）**：
  1. 单独一个 PR 在 `pyproject.toml` 的 `dependencies` 加 `"openpyxl>=3.1,<3.2"`，照该文件惯例写一行注释（Excel 导出，设计闸门编号；上界的理由：`write_xlsx` 失败时的临时文件清理依赖 3.1 的私有属性，升级要过 AIH-TASK-235 的用例）。CI 照常从 `pyproject.toml` 装依赖，合并即生效。
  2. 在 Worker 跑检查用的 Python 3.12 解释器（本地配置 `check_executables.python`）里装上 `openpyxl`：Worker 不装依赖，没装的话 AIH-TASK-235 起的测试在沙箱里 `ImportError`。
- **数据迁移**：无。不加表、列、索引、迁移编号。
- **锁表与性能**：不改任何查询，数据库侧负载与 CSV 相同。应用侧见 §2「资源边界」；`docs/deployment.md` 的容量说明（3.3 节「客户导出」一段）由 AIH-TASK-234 补上 tmpfs 与「xlsx 与 CSV 共用名额」。
- **兼容窗口**：`format` 缺省是 `csv`，旧前端与任何不带它的调用行为逐字节不变。
- **部署顺序**：AIH-TASK-225 → AIH-TASK-234（tmpfs；部署时 `up -d` 因配置变化重建 `api`）→ AIH-TASK-235（模块，无接口使用它）→ AIH-TASK-236（客户接口开放 `format=xlsx`）→ AIH-TASK-237（管理端，需要 AIH-TASK-223 已在生产）→ AIH-TASK-238 → AIH-TASK-239 → AIH-TASK-240（需要 AIH-TASK-224）→ AIH-TASK-241。每层合并即部署、互不破坏。AIH-TASK-234 必须先于 AIH-TASK-236 在生产生效，否则 xlsx 的临时文件会写在容器可写层：登记时 `depends_on` 串成一条链。
- **回滚**：只回滚应用镜像；没有数据要回滚。回滚到 AIH-TASK-236 之前的镜像后，`format` 会被当作未知参数忽略、返回 CSV（文件名仍是 `.csv`，不会出现扩展名与内容不符）；tmpfs 留着无害。
- **部分部署**：前端与后端同一次部署；AIH-TASK-238..240 都排在对应接口之后。
- **监控**：无新告警。导出失败沿用现有 ERROR 日志（各导出的日志名见 §5「各导出的失败日志」：三个大导出 `customer_export_failed`、管理端 `admin_usage_analytics_export_failed`、月度汇总与钱包缺失是通用 500 的 `Unhandled exception`）；AIH-TASK-241 在 runbook 里照 §5 的表写排查步骤，tmpfs 写满照 §2「tmpfs 写满时怎么判断、怎么处置」写。

## 9. 方案取舍

| 方案 | 优点 | 风险 | 未采用原因 |
| --- | --- | --- | --- |
| 采用：现有处理函数加 `format`；openpyxl write_only；字符串显式类型；金额文本、规范整数 ≤ 15 位为数字；公式前缀与 CSV 同一函数；`/tmp` 挂 tmpfs；失败按固定四步清理 | 「只换编码」由同一段代码保证；金额三方逐字一致；不落盘 | 多一个依赖、生成慢一个数量级、依赖 openpyxl 3.1 私有属性做失败清理 | — |
| 备选 A：每个导出另开一个 `…/export.xlsx` 路径与处理函数 | 路由各自独立 | 租约获取、`try`、置空局部变量、在 `except` 外抛错这段微妙的代码再复制五份（今天已有三份），CSV 与 xlsx 的语义会逐渐漂移；路由守卫要改 | 违背「同一查询」原则的最可靠实现方式 |
| 备选 B：金额写数字单元格（`<v>` 写精确十进制文本，设数字格式） | 客户在 Excel 里直接 `SUM` | Excel 读成双精度：超过 15 位有效数字失真（`999999999999.99999999` → `1000000000000`），与账本、CSV、页面对不上 | 导出是对账数据；见 §2「金额为什么写文本」 |
| 备选 C：时间写 Excel 日期 | 可按时间排序、筛选 | Excel 日期无时区、只到毫秒，微秒丢失；用户会当成本地时间 | 文本与 CSV、JSON 一致；字典序就是时间序 |
| 备选 D：xlsx 不加公式前缀（类型化文本本身不会被求值） | 值里没有 `'`，更干净 | xlsx 与 CSV 的值不再逐字相同；用户编辑该格回车后 Excel 会重新按公式解析 | 任务要求与 CSV 一致；保留对编辑场景的防护 |
| 备选 E：不用 openpyxl，自己用 `zipfile` 流式写最小 xlsx | 全在内存、无临时文件、整个文件字节可重现、更快 | 自己保证 OOXML 合规（内容类型、关系、样式），审查与维护成本高 | 已定用 openpyxl；若 Kelvin 不接受 tmpfs，这是退路（§10） |
| 备选 F：不挂 tmpfs，接受临时文件写在容器可写层 | 不改 compose | 客户计费明细明文落宿主机磁盘（删除不擦除磁盘块，spec §112 要求导出安全删除）；进程被杀时残留到容器重建 | 与 #435 拒绝 nginx 临时文件同一理由 |
| 备选 G：设 `TMPDIR=/dev/shm`（容器默认 64 MB 的共享内存） | 不加挂载 | 64 MB 对最坏约 60 MB 的工作表 XML 没有余量；改变全进程的临时目录，语义隐晦 | 显式 tmpfs 更清楚、上限可控 |
| 备选 H：xlsx 用更低的行数上限（因为更耗 CPU） | CPU 上界更小 | 违背「同一行数上限与错误码」；客户同一区间 CSV 能导、xlsx 不能 | 先测量（AIH-TASK-235 报 CI 秒数），超过 10 秒再议（§10） |
| 备选 I：整个文件字节可重现（固定 ZIP 条目时间与 `core.xml` 时间） | 两次导出可以直接比对文件哈希 | 要绕开 `save_workbook`、改写 ZIP 条目，更深地依赖 openpyxl 内部 | 导出不是财务快照；工作表内容已可重现并有测试 |
| 备选 J：前端用一个格式下拉加一个按钮 | 按钮少 | 多一步操作；验收脚本要操作下拉 | 两个按钮直观，现有「Export CSV」完全不动 |
| 备选 K：失败时扫描临时目录删除所有 `openpyxl.` 开头的文件（覆盖「够不到的那一个窗口」） | 连构造函数里的失败也清干净 | 会删掉并发的月度汇总导出正在写的文件，让它失败 | 只删自己引用得到的那个路径；窗口里的文件不含行数据、tmpfs 兜底 |

## 10. 未决问题与假设

- **请 Kelvin 拍板 / 审查者重点看**：
  1. **`api` 容器 `/tmp` 挂 tmpfs（上限 128 MiB）**：openpyxl 无论哪种模式都把工作表写进临时文件，不挂的话客户计费明细会明文落到宿主机磁盘。tmpfs 的页计入 768m 限额，最坏多占约 60 MB、只在生成的几秒里。不接受的话，退路是备选 E（不用 openpyxl、自写最小 xlsx）。
  2. **金额写文本**：客户在 Excel 里直接 `SUM` 金额列会得到 0，要用 `VALUE` 转换（`docs/api.md` 写明）。换来的是与账本逐分一致。若宁可要直接求和而接受 15 位以上失真，改为备选 B，需升版本。
  3. **浏览器验收只查按钮、不点**：任务说明允许「只验按钮存在与下载接口 200」。验 200 需要点按钮（脚本是被动观察，不自己发请求），而点了浏览器就会把文件存到验收机（脚本现有约束「不写任何文件」「验收不下载文件」），要不存文件得给脚本的 CDP 方法清单加一个拒绝下载的方法。我的选择是不点，下载接口的 200 由 AIH-TASK-236、237 的 API 测试覆盖。可选项：AIH-TASK-238 的步骤只点「月度汇总（Excel）」（不占租约、几 KB、夹具客户），被动观察它的 GET 是 200、媒体类型与文件名正确，并在步骤开始时把下载行为设为拒绝 —— 要的话升版本写进 AIH-TASK-238。
  4. **Phase 7「CSV / Excel 导出」是否在 AIH-TASK-241 勾选**：本批交付后，Phase 7 唯一的表格导出（毛利分析）有了 CSV 与 Excel；对账单只有 PDF 下载。我按「勾选，括注写明范围」写进 AIH-TASK-241；如果你认为它还要求对账单的 CSV / Excel，AIH-TASK-241 改为不勾、在行末写明。
  5. **CPU**：最坏 20 000 行的 xlsx 本机约 3 秒（CSV 0.2 秒），`api` 只有 1 核配额。按原则行数上限不变；AIH-TASK-235 报告 CI 实测，超过 10 秒停下。
- **已知限制**：
  1. 项目名、公司名里 XML 不允许的控制字符在 xlsx 里换成 U+FFFD，CSV 保留原字符。只可能来自管理员录入（`_Name` 不拦控制字符）。
  2. 文本里的回车（CR）在 xlsx 里可能读回为换行（XML 解析时的行尾规范化；openpyxl 在没有 lxml 时是否把 CR 写成字符引用未逐一核实）。所有测试按 §7「回车的比较口径」比较。含回车的值只可能来自录入的名称，或经公式前缀规则以回车开头的文本列值。
  3. Excel 对字符串里形如 `_xHHHH_` 的片段有转义约定，可能把它显示成对应字符；`request_id` / `conversation_id` 的格式允许这样的写法。文件里存的是原文，openpyxl、pandas 读取不受影响；Excel 的实际显示**未验证**。V1 接受。
  4. 金额是文本单元格，Excel 标「以文本形式存储的数字」。
  5. 公式前缀 `'` 在 xlsx 里是值的一部分，会显示出来（与 CSV 值逐字相同的代价）。
  6. 整个文件字节不可重现（`core.xml` 与 ZIP 条目的时间）；工作表内容可重现。
  7. openpyxl 写出器构造到一半出错（`create_temporary_file` 之后、赋给 `ws._writer` 之前）时，临时文件没有任何对象引用，`write_xlsx` 删不到它。文件里至多是 `<worksheet>` 开始标签、没有表头与数据行；它留在内存 tmpfs 里，进程退出时由 openpyxl 的 `atexit` 删除、容器重启时清空。不写用例（要在 openpyxl 构造函数内部注入失败）。
- **假设**：
  1. openpyxl 3.1.x 的行为如 §1「现有行为」第 5 条（读的是 3.1.2 源码：写出器懒建、`out` 是路径、句柄在 `xf` 生成器里、`ALL_TEMP_FILES`）；依赖上界 `<3.2`。
  2. 生产镜像与 CI 不装 `lxml`；装了 openpyxl 会改用它写 XML，字节布局可能不同，但不影响正确性与「同一环境两次写出的工作表相同」。
  3. compose 接受 `tmpfs` 短写法里的 `size`、`mode` 选项（compose 规范的示例有这种写法）。
  4. 管理员在 AIH-TASK-235 开工前已把 openpyxl 装进 Worker 的检查解释器。
  5. AIH-TASK-223 按 #709 §2「接口二」实现（处理函数结构、31 列与文本列常量、文件名、日志）。
- **如果假设错误**：1 → AIH-TASK-235 的临时文件与类型用例失败，AIH-TASK-235 停下说明，不在实现里绕；2 → 无影响；3 → AIH-TASK-234 合并后的部署在 `up` 时失败，`deploy.sh` 回滚到上一个镜像与配置，AIH-TASK-234 改写成长写法（`type: tmpfs`）后重来；4 → AIH-TASK-235 的检查在沙箱里失败，补装后重跑；5 → AIH-TASK-237 以 `docs/api.md` 与已合并的代码为准，冲突时停下说明。
- **跨批次协调**：不加迁移，不占编号。本批与 AIH-TASK-221..225 改同一批文件（`app/api/admin_usage_analytics.py`、`app/schemas/usage_analytics.py`、`frontend/src/api/adminUsageAnalytics.ts`、`frontend/src/features/usage/UsageAnalyticsPage.tsx`、`scripts/acceptance/admin_customers.mjs`、`docs/api.md`、`docs/TODO.md`），所以 AIH-TASK-234 依赖 AIH-TASK-225，整批排在它之后。登记 PR 同时把 planning-v1 后续计划里两条「Excel 待登记」（`docs/TODO.md:45`、`:49`）改为已登记。

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
| `AIH-TASK-234` | 部署配置 | AIH-TASK-225 | 约 60 行（compose 6、测试 25、文档 25） |
| `AIH-TASK-235` | 核心模块 | AIH-TASK-234；管理员的依赖 PR | 约 700 行（代码 210、测试 490、打包冒烟 2） |
| `AIH-TASK-236` | 客户接口 | AIH-TASK-235 | 约 750 行（代码 90、测试 560、文档 100） |
| `AIH-TASK-237` | 管理端接口 | AIH-TASK-236、AIH-TASK-223 | 约 350 行（代码 40、测试 270、文档 40） |
| `AIH-TASK-238` | 前端（门户用量页） | AIH-TASK-237 | 约 550 行（api 与页面 110、前端测试 260、验收脚本 170、文案 2） |
| `AIH-TASK-239` | 前端（门户钱包页与会话页） | AIH-TASK-238 | 约 700 行（api 与页面 140、前端测试 330、验收脚本 230、文案 2） |
| `AIH-TASK-240` | 前端（管理端用量分析页） | AIH-TASK-239、AIH-TASK-224 | 约 350 行（api 与页面 60、前端测试 160、验收脚本 130、文案 1） |
| `AIH-TASK-241` | 文档 | AIH-TASK-240 | 约 60 行 |

公共约束（每个任务的验收要点都隐含，不再逐条重复）：实现与本设计逐条一致，与设计冲突时停下在 PR 里说明，不自行改设计；不改 `.platform/`、`.env*`、planning-v1 块；不装依赖、不加迁移、不连生产；复选框只允许 `[ ]` / `[x]`；allowed_commands 全部零退出，skipped 如实报告且不算 passed；Draft PR 交付，PR 正文「设计闸门」写本闸门编号。

#### AIH-TASK-234

- **标题**：Excel 导出第一层：API 容器的临时目录挂载为内存文件系统
- **可改路径**：
  - `docker-compose.yml`
  - `tests/backend/test_compose.py`
  - `docs/deployment.md`
- **验收要点**：
  1. 只在 `api` 服务加 `tmpfs: ["/tmp:size=128m,mode=1777"]`，并写注释：为什么（xlsx 的临时文件不落盘，设计闸门编号）、它计入 `mem_limit`；其他服务、`mem_limit`、`cpus` 不变。
  2. `test_compose.py` 新增一个用例钉住 `api` 的 tmpfs 恰好如上、其他服务没有挂到 `/tmp` 的 tmpfs；已有用例不改。
  3. `docs/deployment.md` 3.3 节「客户导出」一段补：xlsx 与 CSV 共用名额、xlsx 的临时文件在 tmpfs（上限 128 MiB，计入 768m）、单个导出 Python 堆 ≤ 128 MiB。
  4. 不改任何应用代码与前端；不改部署脚本。
- **预计改动**：约 60 行。

#### AIH-TASK-235

- **标题**：Excel 导出第二层：通用 xlsx 写出模块与附件响应，公式注入防护与 CSV 共用
- **可改路径**：
  - `app/core/xlsx_export.py`（新建）
  - `app/core/csv_export.py`
  - `tests/backend/test_xlsx_export.py`（新建）
  - `scripts/packaging_smoke.py`
- **验收要点**：
  1. `write_xlsx`、`ExportXlsxResponse`、`xlsx_response`、`XLSX_MEDIA_TYPE`、`XLSX_MAX_TEXT_LENGTH` 按 §2「新模块」：write_only、恰好一个工作表、只在内存里拼 ZIP、不碰数据库、不打日志。
  2. 单元格类型按 §2「单元格类型」：所有字符串显式字符串类型；整数列只有 `re.fullmatch(r"0|[1-9][0-9]{0,14}", value)` 成立时为数字（数字格式 `0`），其余任何值（超过 15 位、`007`、`5\n`、负号、小数、非 ASCII 数字等）原样为文本、不抛错；其余列为文本；字符清洗与长度上限；输入约定与 `write_csv` 相同。
  3. 失败清理严格按 §2「临时文件」的四步与顺序（关 `_rows` → 取 `_writer` → 关 `xf` → 文件存在才删 `out` 并移出 `ALL_TEMP_FILES`），每步各自吞掉清理异常、最后原样重抛原异常；不扫描临时目录。
  4. `csv_export.py` 新增 `formula_guarded` 与 `export_attachment_headers`，`write_csv` 与 `csv_response` 改为调用它们；CSV 输出与响应头逐字节不变，`test_csv_export.py` 不改断言照常通过。
  5. `test_xlsx_export.py` 覆盖 §7 标 AIH-TASK-235 的各行（含「回车的比较口径」、整数反例、临时文件清理五个场景）；内存用例开着 `tracemalloc` 峰值 ≤ 128 MiB；临时文件大小用例 ≤ 96 MiB 并打印生成秒数。超出任何一条时停下在 PR 里说明，不自行调上限、行数或类型规则。
  6. PR 正文报告 CI 上最长形态 20 000 行的生成秒数；超过 10 秒时停下说明。
  7. `scripts/packaging_smoke.py` 加 `import app.core.xlsx_export`。
  8. 不改任何接口、schema 与前端；`pyproject.toml` 不改（依赖由管理员先加）。
- **预计改动**：约 700 行。

#### AIH-TASK-236

- **标题**：Excel 导出第三层：客户四个导出接口加 xlsx 格式参数
- **可改路径**：
  - `app/api/customer.py`
  - `app/schemas/customer_usage.py`
  - `app/schemas/customer_wallet.py`
  - `app/schemas/customer_conversations.py`
  - `tests/backend/test_customer_xlsx_export_api.py`（新建）
  - `docs/api.md`
- **验收要点**：
  1. 四个导出接口加 `format`（`ExportFormatParam`，缺省 `csv`，只认 `csv` / `xlsx`，FastAPI 在处理函数之前判）；不带或 `csv` 时代码路径与输出逐字节不变。
  2. `xlsx` 时只在「拼文件 → 构造响应」处分支（§2「各接口的改法」）：三个大导出在现有租约与 `try` 之内，月度汇总不拿租约；文件名与工作表名按 §2 的表；OpenAPI 的 200 列出两种媒体类型。
  3. 三个 schema 模块新增 `USAGE_EXPORT_INTEGER_COLUMNS`、`USAGE_SUMMARY_EXPORT_INTEGER_COLUMNS`、`WALLET_EXPORT_INTEGER_COLUMNS`、`CONVERSATION_EXPORT_INTEGER_COLUMNS`，内容如 §2 的表，并加进 `__all__`；列常量、文本列常量与单元格函数不改。
  4. 新测试覆盖 §7 标 AIH-TASK-236 的各行（正常路径与逐格一致、`format` 校验、错误一致、跨租户、INV-7 列与成员、恶意与异常文本），含回车的值按 §7「回车的比较口径」；只跑 SQLite 的理由写进 PR；现有四个 CSV 导出测试文件不改、照常通过。
  5. 路由集合不变（`test_customer_api.py` 的路由守卫不改）。
  6. `docs/api.md`「客户导出」：删去「只做 CSV」一句；概览表与四个接口小节写明 `format` 参数、xlsx 文件名；新增「xlsx 格式」小节：一个工作表、表头同 CSV、单元格类型（金额文本及其理由与求和写法、规范整数 ≤ 15 位为数字、时间文本）、公式前缀与 CSV 相同、控制字符换成 U+FFFD、回车可能读回为换行、与 CSV 共用名额与上限、媒体类型。
  7. 不改前端、`app/core/`、`app/repositories/`。
- **预计改动**：约 750 行。

#### AIH-TASK-237

- **标题**：Excel 导出第四层：管理端毛利分析导出接口加 xlsx 格式参数
- **可改路径**：
  - `app/api/admin_usage_analytics.py`
  - `app/schemas/usage_analytics.py`
  - `tests/backend/test_admin_usage_analytics_xlsx_export_api.py`（新建）
  - `docs/api.md`
- **验收要点**：
  1. `GET /api/v1/admin/usage-analytics/export` 加 `format`（`alias="format"`，在 `require_admin` 与 `parse_analytics_query` 之后判，非法 `InvalidUsageEventQuery("format")`）；不带或 `csv` 时输出逐字节不变。
  2. `xlsx` 时在 AIH-TASK-223 的同一个 `try` 里分支，文件名 `.xlsx`、工作表名 `usage-analytics`；租约、上限、TOTAL 行规则、成功与失败日志（`extra` 仍恰好三个键）都不变。
  3. `app/schemas/usage_analytics.py` 新增 `USAGE_ANALYTICS_EXPORT_INTEGER_COLUMNS`（§2 表里的 10 列），其余列常量与单元格函数不改。
  4. 新测试覆盖 §7 标 AIH-TASK-237 的各行；AIH-TASK-223 的导出测试不改、照常通过；路由清单不变。
  5. `docs/api.md` 导出小节补 `format`、xlsx 的单元格类型（同客户导出「xlsx 格式」小节，指过去即可）与文件名，重申「含内部成本，不得转发给客户」。
  6. 不改前端、客户接口与 `app/core/`。
- **预计改动**：约 350 行。

#### AIH-TASK-238

- **标题**：Excel 导出第五层：门户用量页的用量导出与月度汇总加导出 Excel 按钮
- **可改路径**：
  - `frontend/src/api/download.ts`
  - `frontend/src/api/download.test.ts`
  - `frontend/src/api/customerUsage.ts`
  - `frontend/src/api/customerUsage.test.ts`
  - `frontend/src/features/portal/UsagePage.tsx`
  - `frontend/src/features/portal/UsagePage.test.tsx`
  - `frontend/src/i18n/locales/en.json`
  - `scripts/acceptance/admin_customers.mjs`
- **验收要点**：
  1. 语义以 `docs/api.md`「客户导出」为准（AIH-TASK-236 已写），交互以 §2「门户与管理端页面」为准；与后端冲突时停下说明；不改任何后端文件，不改 `frontend/src/api/client.ts`。
  2. `download.ts` 加 `ExportFormat` 与 `withExportFormat`，`downloadFile` 不改；`customerUsage.ts` 的导出查询加可选 `format`，月度汇总三个函数加可选 `format` 参数；CSV 地址与回退文件名与今天逐字相同。
  3. 用量页工具栏与月度汇总区各加一个 Excel 按钮，与同区的 CSV 按钮共用一个下载状态（§2 的规则）；可用条件、失败文案与清除规则不变。
  4. 新键 `portal.usage.export.buttonXlsx`、`portal.usage.monthly.buttonXlsx`；满足 no-hardcoded-jsx-text 与 keys.test.ts；现有键不改。
  5. 测试覆盖 §7「前端 api」「前端按钮」两行里 AIH-TASK-238 的部分；已有用例照常通过。
  6. 验收脚本加 `check_portal_usage_xlsx`（§2「浏览器验收」：自己经门户导航进用量页，两个按钮各恰好一个且可用，不点）；文件头步骤说明同步；已有步骤行为不变；沿用验收脚本已有的约束；脚本只执行 stdin 里 steps 列出的步骤。
  7. CI 的 frontend job（含 build）全绿；`docs/TODO.md` 不改。
- **浏览器验收**：`acceptance.browser`，`writes: false`，`timeout_seconds: 300`，steps：`login_customer`、`check_portal_usage_xlsx`。
- **预计改动**：约 550 行。

#### AIH-TASK-239

- **标题**：Excel 导出第六层：门户钱包页与会话页加导出 Excel 按钮
- **可改路径**：
  - `frontend/src/api/customerWallet.ts`
  - `frontend/src/api/customerWallet.test.ts`
  - `frontend/src/api/customerConversations.ts`
  - `frontend/src/api/customerConversations.test.ts`
  - `frontend/src/features/portal/WalletPage.tsx`
  - `frontend/src/features/portal/WalletPage.test.tsx`
  - `frontend/src/features/portal/ConversationsPage.tsx`
  - `frontend/src/features/portal/ConversationsPage.test.tsx`
  - `frontend/src/i18n/locales/en.json`
  - `scripts/acceptance/admin_customers.mjs`
- **验收要点**：
  1. 同 AIH-TASK-238 第 1 条；用 AIH-TASK-238 的 `ExportFormat` 与 `withExportFormat`，不改 `download.ts`。
  2. 两个 api 模块的导出查询加可选 `format`；CSV 地址与回退文件名不变。
  3. 钱包页导出区与会话页工具栏各加一个 Excel 按钮，规则同 AIH-TASK-238 第 3 条。
  4. 新键 `portal.wallet.export.buttonXlsx`、`portal.conversations.export.buttonXlsx`；现有键不改。
  5. 测试覆盖 §7「前端 api」「前端按钮」两行里 AIH-TASK-239 的部分；已有用例照常通过。
  6. 验收脚本加 `check_portal_wallet_xlsx`、`check_portal_conv_xlsx`（各自经门户导航进页，按钮恰好一个且可用，不点）；其余同 AIH-TASK-238 第 6 条。
  7. 同 AIH-TASK-238 第 7 条。
- **浏览器验收**：`acceptance.browser`，`writes: false`，`timeout_seconds: 300`，steps：`login_customer`、`check_portal_wallet_xlsx`、`check_portal_conv_xlsx`。
- **预计改动**：约 700 行。

#### AIH-TASK-240

- **标题**：Excel 导出第七层：管理端用量分析页加导出 Excel 按钮
- **可改路径**：
  - `frontend/src/api/adminUsageAnalytics.ts`
  - `frontend/src/api/adminUsageAnalytics.test.ts`
  - `frontend/src/features/usage/UsageAnalyticsPage.tsx`
  - `frontend/src/features/usage/UsageAnalyticsPage.test.tsx`
  - `frontend/src/i18n/locales/en.json`
  - `scripts/acceptance/admin_customers.mjs`
- **验收要点**：
  1. 语义以 `docs/api.md` 管理端用量分析的导出小节为准（AIH-TASK-237 已写）；不改任何后端文件，不改 `download.ts`、`client.ts`。
  2. AIH-TASK-224 的导出地址与下载函数的查询加可选 `format`；CSV 地址与回退文件名（`usage-analytics.csv`）不变，xlsx 回退 `usage-analytics.xlsx`。
  3. AIH-TASK-224 的 Export CSV 按钮右边加 Excel 按钮，用同一组已应用条件，共用一个下载状态；失败显示与清除规则不变（下次点任一导出或已应用条件变化时清除）。
  4. 新键与 AIH-TASK-224 的导出按钮键同一前缀，值 `Export Excel`；不请求任何 `/api/v1/customer/` 路径。
  5. 测试覆盖 §7「前端 api」「前端按钮」两行里 AIH-TASK-240 的部分；AIH-TASK-224 的已有用例照常通过。
  6. 验收脚本加 `check_analytics_xlsx`（须先跑过 `open_usage_analytics`，不自己导航，按钮恰好一个且可用，不点）；其余同 AIH-TASK-238 第 6 条。
  7. 同 AIH-TASK-238 第 7 条。
- **浏览器验收**：`acceptance.browser`，`writes: false`，`timeout_seconds: 300`，steps：`login`、`open_usage_analytics`、`check_analytics_xlsx`。
- **预计改动**：约 350 行。

#### AIH-TASK-241

- **标题**：Excel 导出第八层：运维手册补 xlsx 导出排查，勾选 Phase 5 与 Phase 7 的导出项
- **可改路径**：
  - `docs/runbook.md`
  - `docs/TODO.md`
- **验收要点**：
  1. `docs/runbook.md` 新增「xlsx 导出失败」一节，照该文件规定的五小节写：怎么发现（按 §5「各导出的失败日志」逐个列出：用量、钱包、会话 `customer_export_failed`；管理端 `admin_usage_analytics_export_failed`；月度汇总与钱包缺失是路径对应的 `Unhandled exception`；都带栈）；影响什么（只影响 xlsx 导出，CSV 照常；tmpfs 满时同样只影响 xlsx）；立刻做什么与恢复（照 §2「tmpfs 写满时怎么判断、怎么处置」三步：`df` 确认 → 删 10 分钟前的 `openpyxl.` 残留 → 仍满才在低峰重启 `api`，写明重启的影响面：85 秒优雅排空期间新请求失败、计费与门户同进程中断、AI 暂存缓冲 5 秒最后落库、落不完的按「AI 模块用量 outbox」一节用 `ai-usage-held import` 补录）；绝不能做什么（同 §2 那一段）。另写：升级 openpyxl 要过 AIH-TASK-235 的用例；客户问「金额求和为 0」时的答复（金额是文本，见 `docs/api.md`）。不写「失败只影响那一次导出」这类与重启影响面矛盾的概括。
  2. `docs/TODO.md` Phase 5「客户端用量导出」改 `[x]`，照该章节已勾项的写法括注：CSV（设计闸门 #435、#457、#460，AIH-TASK-113..124）与 Excel（本闸门，AIH-TASK-236、238、239）。
  3. `docs/TODO.md` Phase 7「CSV / Excel 导出」按 §10 第 4 条的决定处理（缺省：改 `[x]`，括注管理端毛利分析的 CSV（AIH-TASK-223、224）与 Excel（本闸门，AIH-TASK-237、240），对账单只有 PDF 下载）；其余勾选不动。
  4. 不改代码与测试；不改 planning-v1 块。
- **预计改动**：约 60 行。

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
| v1 | 2026-11-17 | 初稿（送审前按登记预审修订：§7 统一回车的比较口径；§2 整数改为 `fullmatch` 且不允许前导零、其余退回文本，§7 补反例；§2 写死失败清理的四步与够不到的那一个窗口，§10 加已知限制 7，§7 补五个清理场景；第二轮：§5 加「各导出的失败日志」表（月度汇总与钱包缺失走通用 500 的 `Unhandled exception`）与 tmpfs 写满一行，§2 定下 tmpfs 写满的判断与处置及重启 `api` 的影响面，§8 监控与 AIH-TASK-241 第 1 条同步） | — |

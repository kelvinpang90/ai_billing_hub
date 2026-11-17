# AIH-TASK-228..229 设计：收据 PDF 嵌入中文字体（共用对账单的字体选择规则）（已批准 v1）

> **来源**：设计闸门 Issue #750。本文件是 `APPROVED: design v1` 那一版正文的**逐字副本**（送审时状态行为 `READY_FOR_REVIEW`）。审查：Codex 额度不可用期间由 Claude Code 审查。v1 一次审查通过、没有阻断项；送审前登记预审报出的两处（旧的「印成 ?」用例、对账单 `_Layout` 的类型注解）已改进 v1。
> 放进仓库，是因为 OpenClaw Worker 在沙箱里不联网、读不到 GitHub Issue。
> **实现以本文件为准**；与 Issue 不一致时，以 Issue #750 上被批准的 v1 为准。设计要改，就回到 Issue 升版本、重新过闸门，不要直接改本文件。

## 0. 准入判定

| 任务涉及 | 要填的范围 |
| --- | --- |
| 钱包 / 账本 / 定价 / 汇率 / 支付 / 幂等 / 状态机 | **全部章节** |
| 用量摄取 / 认证与会话（集成侧与平台侧）/ Webhook | §1–§7（**§6 不可省** —— 密钥存储与轮换、鉴权、租户过滤、日志脱敏都在那一节） |
| 前端 / 文档 / CI / 脚本 | **不走本闸门**，直接开 PR |
| 既不碰钱、也不碰用量摄取 / 认证与会话 / Webhook 的后端改动 | **不走本闸门**，直接开 PR |

本任务档位：`全部`（§1–§12）。理由：本批不写钱、不写任何表，按上表本可不走闸门；但它改变的是**客户可见的财务文件**（收据，spec §45、§106）的渲染，而收据的渲染契约是已批准设计闸门 #589 v2 §2「PDF」写死的（「Helvetica 标准字体，不嵌入字体文件」、§10 已知限制 1 写明「需要时另过闸门加字体」）；同时要改动已上线的对账单 PDF 代码（#708 v3）而不得改变它的输出字节。照 #708 的先例走完整模板，不适用的章节写明理由。

---

状态：`READY_FOR_REVIEW`
负责人：Claude
**设计版本**：`v1`
对应需求：spec §45（收据字段、「Customer can download receipt from Portal」）、§93（快照与渲染分离）、§106（服务端生成、只用稳定快照、可重现）；ADR-0008 §5（税务说明随快照冻结）；Invariant 6、7、8、10、12、14；建立在设计闸门 #589 v2（收据，`docs/design/AIH-TASK-196-payment-receipts.md`）与 #708 v3（对账单 PDF，`docs/design/AIH-TASK-216-statement-pdf.md`）之上
目标 PR：待开（拆成 AIH-TASK-228、229，见 §11）

## 1. 目标与边界

### 要解决的问题

1. 收据 PDF 只用 Helvetica：`app/services/receipt_pdf.py:13-14`（模块文档串）、`:58`（`_FONT = "Helvetica"`）、`:137-139`（`_latin1` 把 `ord ≥ 256` 的字符换成 `?`），每个印出的值都先经 `_latin1`（`:194`、`:202`、`:211`、`:217`）。公司名含中文时收据上印成 `??`（现有测试 `tests/backend/test_receipt_pdf.py:113-122` 钉住了这一行为）。这是 #589 v2 §10 已知限制 1，`docs/payment-flow.md:801` 与 `docs/api.md:4368-4369` 也如此告知读者。
2. 同一问题在对账单 PDF 已经解决：`app/services/statement_pdf.py:251-323`「文本与字体」按字符串选 Helvetica 或 Noto Sans SC（规则见 #708 v3 §2「文本与字体」第 1–5 步），字体文件 `app/data/NotoSansSC-Regular.ttf` 已进仓库并随 wheel 分发（`app/data/__init__.py:21-34`，`pyproject.toml:71`），`scripts/packaging_smoke.py:56-75` 在构建期与 CI 核对字体可读、cmap 含 `中`。
3. #708 v3 §1「明确不做什么」第 1 条把收据中文另立后续项；`docs/TODO.md:50` 规划块里有对应的「待登记：收据 PDF 嵌入中文字体」。

### 本批任务说明已定的事项

| 编号 | 事项 | 定为 |
| --- | --- | --- |
| H-a | 字体规则 | **复用对账单的同一套规则**（#708 v3 §2「文本与字体」第 1–5 步，含 v2、v3 补的第 3 步与换行符例外），能抽成共用小模块就抽 |
| H-b | 对账单 | **不得改变对账单 PDF 的输出字节**，要有测试钉住 |
| H-c | 快照 | 收据快照 schema 不变（`schema_version = 1`），只改渲染；同一快照两次渲染字节相同的可重现性保持 |
| H-d | 税务说明 | 照现有收据：只印 `tax_notice.en` 原文，不改文案 |
| H-e | 范围 | 不加迁移、不加依赖（`fonttools>=4.34` 已在 `pyproject.toml:45`，`fpdf2>=2.8` 在 `:41`） |

我定的（未单独问，审查者重点看的列在 §10）：

- **抽出共用模块 `app/services/pdf_text.py`**，对账单与收据都从它取字体选择；对账单的公共名字（`StatementFontUnavailable`、`STATEMENT_FONT_SHA256`）保留为共用对象的别名，所以 `app/api/statement_download.py` 与对账单的接口测试一字不改（§9 备选 A、B）。
- **对账单字节的钉子是「冻结参照」差分测试**，不是钉死 SHA-256（依赖版本没锁、CI 不许 skip，见 §9 备选 E）。
- **收据下载补齐对账单下载的三种失败处置**（快照不合规 / 字体不可用 / 其他渲染异常，日志只带 id），见 §2「下载」与 §6。
- 版面、标签、坐标、字号、行高、页数规则（恰好一页）一律不变；只有「值」的字体按规则选。

### 可观察的完成标准

1. 公司名（或快照里任何会印出的字符串）含中文时，收据 PDF 正确显示中文：嵌入 Noto Sans SC 的子集（只含用到的字形）；字体里也没有的字符印成 `?`，不出现缺字框。
2. 快照里会印出的字符串**全部可用 Latin-1 编码**的收据，渲染出的字节与本批之前**完全相同**（不读字体文件、不嵌入字体）；只含「Latin-1 之外且字体里也没有」的字符的收据同样字节不变（照旧印 `?`）。
3. 同一份收据快照在同一进程两次、在不同 `PYTHONHASHSEED` 与 `SOURCE_DATE_EPOCH` 的两个进程里渲染，字节完全相同（含中文子集时也一样）。
4. 对账单 PDF 的输出字节在本批前后完全相同（任意输入）；由 §7「对账单字节不变」的差分测试钉住。
5. 含中文而字体不可用时，收据下载返回 500 `INTERNAL_ERROR`、记一条只带收据 `public_id` 的 ERROR `Receipt font unavailable; download refused`；其他渲染异常记 `Receipt PDF rendering failed` 并带异常类名；日志里不出现公司名与异常原文。
6. 收据仍是 A4 一页：255 个汉字的公司名换行后，所有文字仍在页内（距底边不少于 20 mm）。

### 明确不做什么

- **不改收据快照与表**：`schema_version` 仍是 1，`issue_receipt`（`app/services/receipts.py`）、模型、迁移 0039 不动；不加迁移、不加依赖、不加二进制文件。
- **不做中文版面**：标签、标题、固定文字仍是英文 Helvetica；不印 `tax_notice.zh`（H-d）。
- **不改对账单的规则、版面与接口**：`app/api/statement_download.py`、`app/api/customer.py`、`app/api/admin_statements.py` 与前端不动。
- **不存 PDF、不存 PDF 的哈希**：沿用即时渲染（#589 v2 §1、spec §93 的「may」）。
- **不加「渲染版本」**：不为旧收据保留「中文印成 `?`」的旧渲染（§9 备选 F）。
- 不改付款邮件、门户与管理端页面（PDF 内容的变化对页面不可见），因而没有浏览器验收。
- 不改 `docs/TODO.md` 规划块（Worker 不改；「待登记」一行由管理员在登记 PR 里删）。

### 现有行为与证据

- **收据渲染**（`app/services/receipt_pdf.py`）：先 `_validate`（`:104-134`），再 `render_receipt_pdf`（`:166-219`）：`set_compression(False)`、`set_auto_page_break(auto=False)`、`set_creation_date(issued_at UTC)`、`set_title(_latin1(receipt_number))`（`:189-194`）；标题 `PAYMENT RECEIPT` Helvetica B 18（`:198-199`）；开具方 Helvetica 12、`multi_cell` 行高 8（`:200-203`）；十行「标签 Helvetica B 11 宽 45 / 值 Helvetica 11 `multi_cell` 行高 7」（`:206-212`）；税务说明 Helvetica 9、行高 5（`:214-218`）。模块不 import 会话、模型与配置（`:5-6`，测试 `tests/backend/test_receipt_pdf.py:267-304`）。
- **对账单的字体选择**（`app/services/statement_pdf.py`）：`_font_resource`（`:254-255`）、`_font_cmap`（`:258-281`，`lru_cache(maxsize=1)`，只认 `glyf`，读不了抛 `StatementFontUnavailable` 且不缓存异常）、`_is_latin1`（`:284-285`）、`_Fonts.prepare`/`_add_noto`（`:288-323`）；家族名常量 `_HELVETICA = "Helvetica"`、`_NOTO = "NotoSansSC"`（`:115-116`）；`STATEMENT_FONT_SHA256`（`:41`）；渲染在 `contextlib.ExitStack` 里、输出前不释放字体文件（`:487-568`）。模块不 import `app.services.receipts`、`app.services.statements`（`:7-9`）。
- **对账单测试对内部名字的引用**（AIH-TASK-228 必须同步改的地方）：`statement_pdf._font_cmap()`（`tests/backend/test_statement_pdf.py:451`、`:463`、`:476`）、`statement_pdf._font_cmap.cache_clear()`（`:495`、`:497`）、`monkeypatch.setattr(statement_pdf, "_font_resource", …)`（`:504`、`:518`）。其余用例只调用 `render_statement_pdf` 与公共名字。
- **对账单下载**按异常类型分三支处理（`app/api/statement_download.py:98-117`），其中 `except StatementFontUnavailable`（`:107`）。
- **收据下载**只处理 `ReceiptSnapshotInvalid`（`app/api/receipt_download.py:97-104`）；其他异常落到通用处理器 `handle_unexpected_error`，它用 `logger.exception`（`app/core/errors.py:145-147`）把异常原文与栈写进日志。#708 v3 §2「下载」第 3 步指出 fpdf2 的异常文案可能含被渲染的文字（公司名），对账单因此加了兜底分支。
- **没有任何地方存收据 PDF 或其哈希**：`payment_receipts` 只有快照与列（`app/models/receipts.py:98-117`，无文件、无哈希列）；`grep -rniE "sha256|digest|etag|checksum" app alembic frontend/src scripts/acceptance` 命中的只有口令、令牌、Webhook 签名、用量指纹、支付事件指纹与字体文件哈希（`app/data/__init__.py:30`、`app/services/statement_pdf.py:41`），没有一处针对收据或对账单 PDF；前端与验收脚本也不比较 PDF 字节。唯一「同一份字节」的断言是测试里「下载正文等于直接渲染同一份快照」（`tests/backend/test_customer_receipts_api.py:439-441`），本批之后仍成立。
- **依赖未锁版本**：`fpdf2>=2.8`、`fonttools>=4.34`（`pyproject.toml:41`、`:45`），CI 用 `pip install -e ".[dev]"`（`.github/workflows/ci.yml:147`）装当时的最新版；CI 把任何 skipped 判为失败（`.github/workflows/ci.yml:158-170`）。
- **本地实测**（fpdf2 2.9.0、fontTools 4.68.0，2026-11-16，不改仓库，只在内存里跑）：
  - 用对账单的 `_Fonts` 按本设计的方式渲染收据：全 ASCII 快照、公司名 `Café த Sdn Bhd` 的快照，字节都与现有 `render_receipt_pdf` **完全相同**；公司名 `Café 中文 Sdn Bhd` 的字节不同（预期）。
  - 公司名为 255 个 `中`：一页，最后一行文字的 y 约 187 mm（A4 高 297 mm），所有文字基线距底边约 111 mm 以上；单次渲染约 0.16 秒。
  - 含中文的对账单（测试夹具 `_chinese_snapshot`）单次渲染约 0.2 秒、峰值内存约 36 MB。

## 2. 设计概要

### 数据流

```text
客户 GET /api/v1/customer/receipts/{id}/download ─┐（不变）
管理员 GET /api/v1/admin/receipts/{id}/download ──┤（不变）
                                                  ▼
             receipt_pdf_response（AIH-TASK-229：补两种失败分支）
               ① 只读会话读收据号与快照，关闭会话（不变）
               ② render_receipt_pdf(snapshot)
                    校验（不变）→ 每个值经 PdfFonts.prepare 选字体（AIH-TASK-228，新）→ 版面（不变）→ bytes
                  ReceiptSnapshotInvalid / ReceiptFontUnavailable / 其他异常 → ERROR（只带 receipt_id）→ 500
               ③ 200 application/pdf，<receipt_number>.pdf，no-store（不变）

render_statement_pdf（只把字体选择换成同一个 PdfFonts，输出字节不变）
```

没有写入、没有状态变化、没有异步任务、没有出站事件。

### 共用模块：`app/services/pdf_text.py`（AIH-TASK-228，新）

只依赖标准库、`fontTools` 与 `fpdf`；**不 import** 会话、模型、配置、`app.services.statement_pdf`、`app.services.receipt_pdf`、`app.services.receipts`、`app.services.statements`。内容是把 `app/services/statement_pdf.py:251-323` 原样搬过来，只改名字（逻辑、顺序、异常的不缓存、`ExitStack` 用法一字不改）：

| 名字 | 来源（AIH-TASK-228 之前） | 说明 |
| --- | --- | --- |
| `HELVETICA: Final = "Helvetica"`、`NOTO: Final = "NotoSansSC"` | `statement_pdf.py:115-116` | 家族名进 PDF 的 `/BaseFont`（子集名形如 `MPDFAA+NotoSansSC`），**不得改** |
| `NOTO_FONT_FILE: Final = "NotoSansSC-Regular.ttf"` | `:117` | |
| `NOTO_FONT_SHA256: Final = "d45f67f0…7d16"` | `:41`（`STATEMENT_FONT_SHA256`） | 值不变 |
| `class PdfFontUnavailable(Exception)` | `:154-155`（`StatementFontUnavailable`） | 字体文件缺失、读不出、不是 TrueType、cmap 为空、`add_font` 失败 |
| `_font_resource() -> Traversable` | `:254-255` | 测试的 monkeypatch 点 |
| `font_cmap() -> frozenset[int]` | `:258-281`（`_font_cmap`） | `functools.lru_cache(maxsize=1)`，进程内两个渲染器共用一份；异常不缓存 |
| `is_latin1(text) -> bool` | `:284-285` | |
| `class PdfFonts`：`__init__(pdf, stack)`、`prepare(text) -> tuple[str, str]` | `:288-323`（`_Fonts`） | 规则见下；`add_font(NOTO, "", path)` 每个 `PdfFonts` 实例（即每次渲染）至多一次，在第一次需要时 |

`prepare(text)` 的规则逐字照 #708 v3 §2「文本与字体」：

1. 每个字符都 `ord(c) < 256` → `(HELVETICA, text)`，原样（含 Latin-1 的控制字符），**不读字体文件**。
2. 否则把「既不在 Latin-1、也不在 `font_cmap()`」的字符换成 `?`；替换后全部 `< 256` → `(HELVETICA, 替换后)`（读了 cmap，但不 `add_font`、不嵌入字体）。
3. 否则凡不在 cmap 里的字符（含 Latin-1 的 C0 / C1 控制字符）换成 `?`，**换行符 `\n` 例外**、保留；`add_font` 后返回 `(NOTO, 替换后)`。
4. 读 cmap 或 `add_font` 失败 → `PdfFontUnavailable`（`raise … from error`）。

`app/data/__init__.py` 的文档串（只改文字，不动字体文件）把「月度对账单 PDF 里……用的字体」改为对账单与收据共用，把「同步对账单 PDF 代码里钉住的哈希」改为「同步 `app/services/pdf_text.py` 的 `NOTO_FONT_SHA256`」，并写明换字体会同时改变历史对账单与历史收据重新渲染出的字形。

### 对账单：`app/services/statement_pdf.py`（AIH-TASK-228，只换来源）

- 删去 `:251-323` 的四个定义与只为它们服务的 import 与常量（`contextlib` 仍要用于 `ExitStack`；`functools`、`resources`、`Traversable`、`TTFont`、`_NOTO`、`_FONT_FILE`、`_LATIN1_END`、`_NEWLINE` 删去），改为 `from app.services.pdf_text import HELVETICA, NOTO_FONT_SHA256, PdfFontUnavailable, PdfFonts`；`_HELVETICA` 取 `HELVETICA` 的值。
- `:488` 的 `_Fonts(pdf, stack)` 换成 `PdfFonts(pdf, stack)`，**按模块全局名 `PdfFonts` 调用**（差分测试要 monkeypatch 它，见 §7）。
- `:388` `_Layout.__init__` 的类型注解 `fonts: _Fonts` 换成 `fonts: PdfFonts`（`_Fonts` 已搬走，不改会报未定义名）；这是版面代码里唯一允许的一处改动，只改注解。
- 公共名字不变：`StatementFontUnavailable = PdfFontUnavailable`（同一个类，`app/api/statement_download.py:107` 的 `except` 照常命中）、`STATEMENT_FONT_SHA256: Final = NOTO_FONT_SHA256`，`__all__` 不变。
- 除上面几处与文档串里「字体」一段指向 `pdf_text` 之外，**校验、显示、版面与 `render_statement_pdf` 的代码一字不改**。

### 收据：`app/services/receipt_pdf.py`（AIH-TASK-228）

- 删去 `_latin1`（`:137-139`）与 `_FONT`（`:58`）；`from app.services.pdf_text import HELVETICA, PdfFontUnavailable, PdfFonts`；导出 `ReceiptFontUnavailable = PdfFontUnavailable`（加进 `__all__`）。
- `render_receipt_pdf` 的步骤（坐标、字号、行高、宽度、顺序与现有逐一相同）：
  1. `_validate(snapshot)`（不变）；任何不合规先抛 `ReceiptSnapshotInvalid`，不读字体。
  2. `FPDF` 的设置与现有相同；`set_title(snapshot["receipt_number"])`（收据号已校验为 ASCII，与原来的 `_latin1(...)` 结果相同）。
  3. 在 `with contextlib.ExitStack() as stack:` 里建 `fonts = PdfFonts(pdf, stack)`，然后 `add_page()`；
  4. 标题：`set_font(HELVETICA, "B", 18)` + `cell(0, 10, "PAYMENT RECEIPT", …)`（不变，固定 ASCII 不经 `prepare`）；
  5. 开具方：`family, text = fonts.prepare(issuer.name)`；`set_font(family, "", 12)`；`multi_cell(0, 8, text, …)`；`ln(8)`；
  6. 十行：`set_font(HELVETICA, "B", 11)` + `cell(45, 7, label)`（标签固定 ASCII）；`family, text = fonts.prepare(value)`；`set_font(family, "", 11)`；`multi_cell(0, 7, text, …)`。**每一个值都经 `prepare`**（含由代码生成的 ASCII 值：时刻、金额、币种、付款方式显示值、状态；它们走第 1 步，结果与原样相同），与对账单 `_Layout.labelled` 同一写法；
  7. `ln(10)`；`family, text = fonts.prepare(tax_notice.en)`；`set_font(family, "", 9)`；`multi_cell(0, 5, text, …)`（原文：不改写、不拼接；含换行时照换行）；
  8. **在 `with` 块内** `return bytes(pdf.output())`（fpdf2 输出时才子集化，字体文件此前一直可读）。
- 只有 `issuer.name`、`customer.company_name`、`payment.payment_id`、`payment.gateway_reference`、`tax_notice.en` 是快照里的自由文本，可能含非 Latin-1 字符；其余值由校验限定为 ASCII（`receipt_pdf.py:117-127`）。
- 文档串：「只用 Helvetica……换成 `?`（设计 §10 已知限制 1）」一段改为「字体照 `app/services/pdf_text.py`（本设计 §2）」；「可重现」一段补「含中文字体子集时也一样」。
- 由此（§1 本地实测已验证）：快照里会印出的字符串全部 `< 256`、或其中非 Latin-1 字符都不在字体里时，调用序列与本批之前逐一相同、不 `add_font`，**字节不变**。

### 下载：`app/api/receipt_download.py`（AIH-TASK-229）

`receipt_pdf_response` 的 `try`（`:97-104`）改为照 `app/api/statement_download.py:98-117` 的三支（收据号列格式检查仍在 `try` 内、仍按快照不合规处理）：

| 异常 | 日志（ERROR，`extra` 只带 `receipt_id` = 收据 `public_id`） | 对外 |
| --- | --- | --- |
| `ReceiptSnapshotInvalid` | `Receipt snapshot invalid; download refused`（现有，不变） | `ReceiptRenderFailed`（500 `INTERNAL_ERROR`，固定文案）`from None` |
| `ReceiptFontUnavailable` | `Receipt font unavailable; download refused`（新） | 同上 |
| 其他任何 `Exception` | `Receipt PDF rendering failed`（新），`extra` 另带 `"error_type": type(exc).__name__` | 同上 |

不用 `logger.exception`、不交给通用处理器；成功下载不记日志（不变）。查询、租户过滤、404、响应头、文件名一律不变。模块文档串第 14–16 行同步写三种失败。

### 文档（AIH-TASK-229）

- `docs/api.md`：客户下载小节（`:4357-4377`）删去「字体只支持 Latin-1，之外的字符（例如中文）印成 `?`」，改为「快照里的公司名等含中文时用嵌入的 Noto Sans SC 字体（子集）印出，字体里没有的字符印成 `?`」；「同一张收据任何时候下载内容都相同」补「（同一代码与依赖版本下字节相同）」；500 的说明与客户错误表那一行（`:4387`）改为「快照不合规、需要的中文字体不可用、或渲染失败」，与对账单小节（`:3707`、`:3858`）同一措辞；管理端下载小节（`:4137-4153`）的 500 一句与管理端错误表的 500 一行（`:4175`）同步。
- `docs/payment-flow.md`：§14.7「下载」（`:756-760`）把一条 500 处置扩成三种 ERROR 日志各自的含义与处置（快照不合规：人工排查、不改收据行；字体不可用：查镜像里的 `app/data/NotoSansSC-Regular.ttf` 与构建期的 packaging smoke，重新部署带字体的镜像；渲染失败：按 `receipt_id` 读快照在本地用同一代码复现）；§14.10（`:799`、`:801`）「不做中文收据」改为「版面只用英文」，已知限制 1 改为「快照里的中文等字符用嵌入的 Noto Sans SC 印出；字体里没有的字符印成 `?`。本批（设计闸门 H）之前开具、印出的字符串含中文的收据，重新下载得到的字节与之前不同（字形代替 `?`，数据不变）」。

### 事务边界

没有写事务。下载仍是一个只读会话、会话关闭后渲染（`receipt_download.py:72-86`，不变）。

### 时间语义

不变：PDF 上的时刻按 MYT 显示；元数据创建时刻 = 快照 `issued_at`（UTC）。

### 金额与精度

不变：金额经 `_display_money`（`receipt_pdf.py:148-157`，`Decimal` + `ROUND_HALF_UP`），不经 float；金额字符串是 ASCII，走 `prepare` 第 1 步，原样印出。

## 3. 不变量影响矩阵

| 不变量 | 是否触碰 | 可能怎样被破坏 | 设计控制 | 验证方式 |
| --- | --- | --- | --- | --- |
| INV-1 中心故障不中断 AI | 否 | 只改两个 PDF 渲染器与收据下载的异常分支，不在 AI 调用、摄取与计费路径上；渲染仍在请求线程、会话关闭之后 | 不加锁、不占连接；字体 cmap 进程内只读一次（两个渲染器共用） | 不新增 |
| INV-2 事件不重复扣费 | 否 | 不读不写用量事件与账本 | — | 不新增 |
| INV-3 支付不重复入账 | 否 | 不碰入账与支付状态；只渲染已开具收据的快照 | — | 不新增 |
| INV-4 余额只经账本变动 | 否 | 不写钱包 | — | 不新增 |
| INV-5 历史账本不可变 | 否 | 不读账本 | — | 不新增 |
| INV-6 事件保留版本引用 | 是 | 渲染时读当前公司名、当前税务政策或别的表，让历史收据随配置变化（spec §106） | 渲染仍只收快照；`pdf_text` 与两个渲染器都不 import 会话、模型、配置；字体是随代码分发、SHA-256 钉住的资产（`NOTO_FONT_SHA256`，换字体要过设计闸门） | §7「渲染只读快照（含中文）」「模块隔离」「字体文件钉住」 |
| INV-7 客户不可见成本毛利 | 是（不变） | 改渲染时顺手多印快照里的字段或放宽键集合 | 校验（`receipt_pdf.py:104-134`）一字不改，键集合恰好相等；版面只印原来的十个值、开具方与 `tax_notice.en` | 现有 `test_invalid_snapshot_is_rejected` 不改照过；§7「收据版面不变」 |
| INV-8 租户不可互访 | 是（不变） | 改下载函数时动了租户过滤 | AIH-TASK-229 只改 `try` 里的异常分支；`_read` 与参数签名不变 | 现有跨租户用例（`test_customer_receipts_api.py:518` 起）不改照过 |
| INV-9 对话内容不入库 | 否 | 收据快照没有对话内容；日志不带快照 | — | 不新增 |
| INV-10 金额用 Decimal | 是（不变） | — | `_display_money` 不变；金额字符串不因字体选择被改写（ASCII 走第 1 步） | 现有金额显示用例不改照过 |
| INV-11 event_id 至多一次财务效果 | 否 | 没有财务效果 | — | 不新增 |
| INV-12 定稿对账单不可变 | 是（只读） | 重构字体选择时改变了历史定稿对账单重渲染出的字节（spec §65「regenerate … from snapshot」、§106） | 规则代码原样搬移、家族名不变、`add_font` 时机不变、版面代码不改；对账单不读任何新东西 | §7「对账单字节不变（冻结参照差分）」；对账单现有用例的断言不改照过 |
| INV-13 状态与事件原子提交 | 否 | 没有状态变化、审计与出站事件 | — | 不新增 |
| INV-14 队列丢失不毁持久工作 | 否 | 文档生成是请求内同步渲染，不经 Celery；Redis 丢失照常下载 | 不加任务 | 不新增 |

## 4. 状态与并发

### 状态

本批没有状态与状态变化。收据与对账单的状态机（#589 v2、#565 v6）不变。

| 当前状态 | 事件 | 前置条件 | 新状态 | 副作用 | 非法时结果 |
| --- | --- | --- | --- | --- | --- |
| 收据已开具 | 下载 | 客户：本租户；管理员：已过 `require_admin` | 不变 | 无 | 404 `RECEIPT_NOT_FOUND`（不变） |

### 并发

- 每次渲染一个新的 `FPDF` 与 `PdfFonts` 实例；`add_font` 作用在本次渲染的 `FPDF` 上。唯一的共享状态是 `font_cmap()` 的进程内缓存：只读的 `frozenset`，并发下首次可能重复计算、结果相同（与对账单现状相同，#708 v3 §4），现在由两个渲染器共用。
- 数据库唯一性、幂等键、相同 ID 不同载荷、重试层：不适用（只读；同一请求重发得到相同字节）。

## 5. 失败与恢复矩阵

| 失败点 | 对外结果 | 数据最终状态 | 是否可重试 | 恢复来源 | 告警 |
| --- | --- | --- | --- | --- | --- |
| 收据 id 不存在 / 别人的 / 不像 uuid | 404 `RECEIPT_NOT_FOUND`（不变） | 不变 | — | — | 无 |
| 快照不合规（只可能来自手工改库） | 500；ERROR `Receipt snapshot invalid; download refused`（不变） | 不变；这张收据永久下载不了（行不可改） | 否 | `docs/payment-flow.md` §14.7：按 `receipt_id` 人工排查，不改收据行 | 仅 ERROR 日志 |
| 印出的字符串含非 Latin-1 字符而字体文件缺失或损坏（打包缺陷） | 500；ERROR `Receipt font unavailable; download refused` | 不变；只影响印出的字符串含非 Latin-1 字符的收据（这类收据本批之前能下载、印 `?`，见 §10 第 4 条） | 修复后是 | 重新部署带字体的镜像；packaging smoke（`scripts/packaging_smoke.py:56-75`）在 Docker 构建期与 CI 已拦，正常到不了生产 | 仅 ERROR 日志 |
| 渲染时其他异常（fpdf2 / fontTools 内部错误） | 500；ERROR `Receipt PDF rendering failed`，带 `error_type` | 不变 | 视原因 | 按 `receipt_id` 读快照，在本地用同一代码复现 | 仅 ERROR 日志 |
| AIH-TASK-228 已部署、AIH-TASK-229 未部署时字体不可用或渲染异常 | 500；落到通用处理器，`logger.exception` 记 `Unhandled exception` 与栈 | 不变 | 同上 | 同上；窗口只到 AIH-TASK-229 部署为止 | 仅 ERROR 日志（栈里是 `PdfFontUnavailable` 的固定文案与底层读文件错误，不含快照内容；fpdf2 内部错误的文案在窗口内仍可能进日志，与本批之前收据下载的现状相同） |
| 数据库不可用 / 查询失败 | 503 / 500（现有通用处理） | 不变 | 是 | 数据库恢复后重试 | 现有数据库告警 |
| DB 提交失败 | 不适用：没有写入 | — | — | — | — |
| Redis / Celery 丢失 | 无影响（不经队列） | 不变 | — | — | — |
| 外部服务超时 | 不适用：不调外部服务 | — | — | — | — |
| 重复请求 | 每次 200、字节相同 | 不变 | — | — | — |
| 对账单：同上各行 | 与 #708 v3 §5 相同（本批不改对账单的下载与失败语义） | — | — | — | — |

## 6. 数据与安全边界

- **鉴权与租户过滤**：不变。客户 `require_customer`、租户只来自令牌主体；管理端 `require_admin`、`tenant_id=None`（`receipt_download.py:89-95`）。
- **客户侧禁止出现的内容**：不变。PDF 只印原来的十个值、开具方与 `tax_notice.en`；快照键集合恰好相等，多一个键即拒绝（INV-7）。客户与管理员下载同一份字节。
- **日志**：收据下载只记三种 ERROR，`extra` 只有 `receipt_id`（第三种另有 `error_type`）；不带公司名、收据号、金额、快照内容与异常原文；不用 `logger.exception`。理由：本批让收据第一次走字体子集化（fontTools），新增的失败路径的异常文案与栈不受我们控制，可能含被渲染的文字（#708 v3 §2「下载」第 3 步的同一判断）；交给 `handle_unexpected_error` 会把它们写进日志（`app/core/errors.py:147`）。
- **字体许可**：Noto Sans SC 为 SIL OFL 1.1，许可证 `app/data/NotoSansSC-OFL.txt` 已随 wheel 与镜像分发（#708 v3 §6）；收据嵌入子集与对账单同一用法，不修改字体文件。
- **个人 / 商业数据**：PDF 含公司名（快照冻结值），与之前相同；不落盘、`Cache-Control: no-store`（不变）。
- **密钥**：不新增、不读取（ADR-0004 不涉及）。
- **prompt / response**：不涉及。
- **数据保留与删除**：不新增数据；PDF 不存储。

## 7. 测试证据计划

渲染测试是纯单元测试（不需要数据库；Worker 沙箱跑不了 `tests.backend`，由 CI 跑）；接口测试用现有的 SQLite / MySQL 参数化夹具，断言不写方言相关的 SQL 字面量；需要改快照的用例沿用现有 `sqlite_world` 直接改行的写法（`test_customer_receipts_api.py:475-490`，MySQL 触发器拒绝改收据行）。PDF 内容流与 ToUnicode 的检索工具函数放进新的辅助模块 `tests/backend/pdf_content.py`（不以 `test_` 开头），由收据与 `pdf_text` 的测试导入；对账单测试保留自己的那份，不改（减少对账单测试的改动面）。示例 uuid 全零。

| 风险/需求 | 测试层级 | 场景 | 预期结果 | 任务 |
| --- | --- | --- | --- | --- |
| 规则矩阵 | unit（`test_pdf_text.py`） | 对一个新 `FPDF` 与 `ExitStack` 逐条调用 `prepare`：`Example Sdn Bhd`；`Café\tA`；`Café த Sdn Bhd`（先断言 U+0BA4 不在 cmap）；`示例科技 Sdn Bhd`；`Café 中 த`；`Customer’s copy\tA\nSecond line B`（先断言 `’` 在 cmap、`\t` 不在） | 依次为 `(Helvetica, 原样)`、`(Helvetica, 原样含 \t)`、`(Helvetica, Café ? Sdn Bhd)`、`(NotoSansSC, 原样)`、`(NotoSansSC, Café 中 ?)`、`(NotoSansSC, Customer’s copy?A\nSecond line B)` | AIH-TASK-228 |
| `add_font` 次数 | unit | 同一 `PdfFonts` 先后 `prepare` 两个含中文的串、一个 ASCII 串；另一个实例只 `prepare` ASCII 与 `Café த` | 前者 `add_font` 恰好一次、家族名 `NotoSansSC`；后者零次（包一层计数） | AIH-TASK-228 |
| 字体不可用不缓存 | unit | monkeypatch `pdf_text._font_resource` 指向不存在的文件、清缓存后 `prepare("中")`；恢复后再 `prepare("中")`；指向非字体文件 | 前者 `PdfFontUnavailable`；恢复后成功；非字体文件 `PdfFontUnavailable`；`prepare("Example")` 在字体缺失时照常（不读字体） | AIH-TASK-228 |
| 模块隔离 | unit | 单独子进程只 `import app.services.pdf_text`、只 `import app.services.receipt_pdf`，检查 `sys.modules` | 都不含 `sqlalchemy`、`pydantic_settings`、`app.models`、`app.repositories`、`app.core.config`、`app.core.database`、`app.services.statements`、`app.services.receipts`；`pdf_text` 不含两个渲染器；`receipt_pdf` 不含 `app.services.statement_pdf`。对账单现有 `_FORBIDDEN_MODULES`（`test_statement_pdf.py:709-718`）加 `app.services.receipt_pdf` | AIH-TASK-228 |
| 对账单字节不变（H-b） | unit（`test_statement_pdf.py`） | 测试文件里放一份「冻结参照」`_FrozenStatementFonts`：AIH-TASK-228 开工时 main 上 `app/services/statement_pdf.py:254-323` 的 `_font_resource`、`_font_cmap`、`_is_latin1`、`_Fonts` 的逐字副本（只改成测试私有名，自带缓存，读同一字体文件）。对 ASCII、中文（`_chinese_snapshot`）、`Café த Sdn Bhd`、`Café 中 த`、税务说明含 `’`/`\t`/`\n`、300 行分页这六份输入，各渲染两次：一次照常，一次 `monkeypatch.setattr(statement_pdf, "PdfFonts", _FrozenStatementFonts)` | 每份两次的字节完全相同 | AIH-TASK-228 |
| 对账单现有用例 | unit | `test_statement_pdf.py` 只做机械替换：`statement_pdf._font_cmap` → `pdf_text.font_cmap`（`:451`、`:463`、`:476`）、缓存夹具与两处 monkeypatch 改指 `pdf_text`（`:495-518`） | 断言一条不改，全部通过；`STATEMENT_FONT_SHA256 == pdf_text.NOTO_FONT_SHA256` | AIH-TASK-228 |
| 收据：全 Latin-1 字节不变 | unit（`test_receipt_pdf.py`） | 现有 `_snapshot()`；公司名 `Café Sdn Bhd`；公司名 `Café த Sdn Bhd`。参照：`monkeypatch.setattr(receipt_pdf, "PdfFonts", 桩)`，桩的 `prepare` 恒返回 `("Helvetica", 把 ord ≥ 256 的字符换成 ?)`（即本批之前的 `_latin1` 规则）、从不 `add_font` | 照常渲染与参照渲染字节相同；内容不含 `/FontFile2`；第三份含 `(Caf… ? Sdn Bhd)` 字面量 | AIH-TASK-228 |
| 收据：中文 | unit | 公司名 `示例科技 Sdn Bhd` | `/FontFile2` 在；`/BaseFont` 含 `NotoSansSC`；ToUnicode 里有这四个字的码点；经 ToUnicode 还原的文字里有 `示例科技 Sdn Bhd`；标签、`(PAYMENT RECEIPT)`、`(RM 1,000.00)`、`(ACV-AIR-2026-000001)` 仍是 Helvetica 字面量；恰好一页 | AIH-TASK-228 |
| 收据：字体里没有的字符 | unit | 公司名 `Café 中 த` | 嵌入字体；还原文字里有 `Café 中 ?`；ToUnicode 不含 `த` | AIH-TASK-228 |
| 收据：其他自由文本 | unit | `tax_notice.en` = `Customer’s copy\tA\nSecond line B`；`issuer.name` 含中文 | 说明在 `\n` 处换行、`\t` 印成 `?`、`’` 正常；开具方的中文经 Noto 印出 | AIH-TASK-228 |
| 收据：一页放得下 | unit | 公司名 255 个 `中` | 恰好一页；内容流里每个 `BT x y Td` 的 y 都不小于 56.69（距底边 20 mm）；公司名的每个字都在 ToUnicode 还原的文字里出现（总数 255） | AIH-TASK-228 |
| 收据：字体缺失 | unit | monkeypatch `pdf_text._font_resource` 指向不存在的文件并清缓存：渲染中文收据；渲染全 ASCII 收据；渲染 `schema_version = 2` 的中文收据 | 中文抛 `ReceiptFontUnavailable`（且不是 `ReceiptSnapshotInvalid`）；ASCII 照常；坏快照仍先抛 `ReceiptSnapshotInvalid`（先校验后读字体） | AIH-TASK-228 |
| 收据：可重现 | unit | 中文收据同进程两次；两个 `sys.executable` 子进程（环境为当前副本加 `PYTHONHASHSEED=1` / `2`、`SOURCE_DATE_EPOCH=0` / `2000000000`，输出经标准输出传回） | 字节相同，且等于同进程的结果 | AIH-TASK-228 |
| 收据：只读快照 | unit | 现有 `test_renders_without_database_or_configuration`（`test_receipt_pdf.py:267-304`）加中文快照的参数；深拷贝比较中文快照 | 照常渲染、字节相同；快照不变 | AIH-TASK-228 |
| 收据：校验不变 | unit | 现有 `_INVALID` 全表（`test_receipt_pdf.py:188-258`） | 不改，照过 | AIH-TASK-228 |
| 收据：旧的「印成 ?」用例 | unit | 现有 `test_characters_outside_latin1_become_question_marks`（`test_receipt_pdf.py:113-122`）钉的正是本批要改掉的行为 | 这是唯一允许改断言的现有收据用例：改名并改成断言中文经 Noto Sans SC 原样印出（经 ToUnicode 还原的文字含原中文、不含 `??`），可与「收据：中文」合并 | AIH-TASK-228 |
| 下载：中文收据 | integration（API，SQLite 与 MySQL） | 租户公司名改成 `示例科技 Sdn Bhd` 后开具收据并下载 | 200；正文等于 `render_receipt_pdf(存下的快照)`；含 `/FontFile2`；连发两次字节相同 | AIH-TASK-229 |
| 下载：字体不可用 | integration（API） | monkeypatch `receipt_download.render_receipt_pdf` 抛 `ReceiptFontUnavailable` | 500 固定文案、无 `Content-Disposition`；该 logger 的 ERROR 恰好一条，文案 `Receipt font unavailable; download refused`，`extra` 只有 `receipt_id`、没有 `error_type` | AIH-TASK-229 |
| 下载：渲染异常 | integration（API） | monkeypatch 让渲染抛文案含公司名的 `RuntimeError` | 500；ERROR 恰好一条 `Receipt PDF rendering failed`，`error_type == "RuntimeError"`；日志记录的消息、`extra` 与 `exc_info`/`exc_text` 都不含该文案（没有交给通用处理器） | AIH-TASK-229 |
| 下载：快照不合规 | integration（API） | 现有 `test_a_corrupted_snapshot_is_500_with_one_error_log` | 不改，照过 | AIH-TASK-229 |
| 租户越权 / 鉴权 / 只读状态 | integration（API） | 现有用例 | 不改，照过 | AIH-TASK-229 |
| 并发 | 不适用 | 只读、无锁；唯一共享状态是只读 cmap 缓存（§4） | — | — |
| 幂等重放 | integration（API） | 已含在「下载：中文收据」 | 字节相同 | AIH-TASK-229 |
| 事务中途失败 | 不适用 | 没有写事务；渲染失败不留任何状态 | — | — |
| Redis/Celery 丢失 | 不适用 | 不经队列 | — | — |
| 金额精度 | unit | 现有 `test_amount_is_shown_with_two_decimals_and_thousands` | 不改，照过 | AIH-TASK-228 |

## 8. 迁移与上线

- **数据迁移**：不适用——不加表、不改列、不回填；收据快照与迁移 0039 不动。
- **依赖**：不新增（H-e）；`fonttools`、`fpdf2` 已在 `dependencies`。不新增二进制文件（字体已由 #706 提交）。
- **打包**：`scripts/packaging_smoke.py` 不改：它已 import `app.services.receipt_pdf` 与 `app.services.statement_pdf`（`:39-40`，两者都会 import `app.services.pdf_text`，漏打包会直接报错），并已核对字体可读、cmap 含 `中`（`:56-75`）。
- **锁表与性能**：不锁表。含中文的收据每次下载解析字体并子集化：本地实测单次约 0.16–0.2 秒、峰值内存约 36 MB（§1 证据）；cmap 进程内缓存一次。
- **部署顺序**：AIH-TASK-228（渲染）→ AIH-TASK-229（下载异常分支与文档）。AIH-TASK-228 合并即随自动部署生效：之后下载含中文的收据得到中文；AIH-TASK-229 合并后失败日志按 §2「下载」分三种。AIH-TASK-228 与 AIH-TASK-229 之间的窗口见 §5 第 5 行。
- **对已开具收据的影响**：AIH-TASK-228 部署后，**印出的字符串含字体覆盖的非 Latin-1 字符**的已开具收据，重新下载得到的字节与之前不同（字形代替 `?`，快照与数据不变）；其余收据字节不变（§1 实测、§7「收据：全 Latin-1 字节不变」）。没有任何地方存收据 PDF 或其哈希（§1 证据），所以没有需要同步的存量。管理员可在部署前只读查一次候选（会列出含任何非 ASCII 字符的收据，是超集；只看印出的五个字段，`tax_notice.zh` 不印、不查）：

  ```sql
  SELECT receipt_number
    FROM payment_receipts
   WHERE CHAR_LENGTH(CONCAT_WS('', snapshot_json->>'$.issuer.name', snapshot_json->>'$.customer.company_name',
                               snapshot_json->>'$.payment.payment_id', snapshot_json->>'$.payment.gateway_reference',
                               snapshot_json->>'$.tax_notice.en'))
      <> LENGTH(CONCAT_WS('', snapshot_json->>'$.issuer.name', snapshot_json->>'$.customer.company_name',
                          snapshot_json->>'$.payment.payment_id', snapshot_json->>'$.payment.gateway_reference',
                          snapshot_json->>'$.tax_notice.en'));
  ```

- **兼容窗口 / 部分部署**：每一步单独上线都不影响接口契约（路径、状态码、响应头不变）；前端不变。
- **回滚或前滚**：只回滚应用镜像，没有数据要回滚。回滚到 AIH-TASK-228 之前，含中文的收据重新印成 `?`（又一次字节变化）；对账单前后字节都不变。
- **监控与负责人**：不加告警维度（Healthchecks 20/20 已满，2026-10-17 Kelvin 定不增检查，#708 v3 §8 同一处理）；下载失败只在 ERROR 日志，按 `docs/payment-flow.md` §14.7 处置；负责人 Kelvin。

## 9. 方案取舍

| 方案 | 优点 | 风险 | 未采用原因 |
| --- | --- | --- | --- |
| 采用：抽出 `pdf_text.py`（原样搬移对账单的规则），收据每个值经 `PdfFonts.prepare`；对账单保留公共别名；对账单字节用冻结参照差分测试钉住；收据下载补齐三种失败分支 | 一份规则、一份 cmap 缓存；对账单接口与接口测试不动；全 Latin-1 收据字节不变；中文正确显示 | 对账单模块与测试要改（搬移与机械替换），靠差分测试兜住 | — |
| 备选 A：收据直接 import 对账单的私有 `_Fonts` / `_font_cmap` | 对账单零改动 | 收据依赖对账单模块的私有实现；收据抛出的异常叫 `StatementFontUnavailable`；以后为对账单改字体代码会无声地改变收据 | 耦合方向错，命名误导 |
| 备选 B：把规则复制一份进 `receipt_pdf.py` | 两个模块互不影响 | 两份规则会漂移（#708 已升过两次版本修这段规则）；两份 cmap 缓存，进程内各读一次约 10 MB 字体的 cmap | 违背 H-a「同一套规则」 |
| 备选 C：收据所有字符串都用 Noto Sans SC | 字形统一 | 每张收据都要读字体并嵌入子集，所有现有收据字节都变；TrueType 文字在内容流里是字形编号，现有按 `(文字)` 检索的测试全部要改 | 成本与可测性（#708 v3 §9 备选 A 同一理由） |
| 备选 D：fpdf2 的回退字体（`set_fallback_fonts`） | 不用自己按字符串选字体 | 主字体是标准字体时回退是否生效未验证；缺字行为由库决定 | 与对账单的显式规则不一致（#708 v3 §9 备选 B） |
| 备选 E：钉死对账单输出的 SHA-256（金样哈希） | 直接钉字节 | `fpdf2>=2.8`、`fonttools>=4.34` 未锁版本（`pyproject.toml:41`、`:45`），CI 每次装最新版（`ci.yml:147`）；上游任何序列化或子集化的改动都会让所有 PR 的 CI 同时变红；CI 不许 skip（`ci.yml:158-170`），无法「版本不符就跳过」 | 钉的是依赖版本而不是本批的改动；冻结参照差分与依赖版本无关，钉的正是「字体选择的输出不变」 |
| 备选 F：给收据加「渲染版本」，本批之前开具的收据继续用只印 Helvetica 的旧规则 | 已开具收据的字节永不变 | 要永久保留两套渲染路径并按开具时刻或新字段分派（新字段要改快照或加列，违背 H-c、H-e）；继续把能印出的数据印成 `?` | spec §106 要求的是「从快照重现内容」，字形更忠实于快照；受影响的存量很少（§10 假设 1） |
| 备选 G：收据下载只加 `ReceiptFontUnavailable` 一支，不加兜底 | 改动更小 | 其他渲染异常仍交给 `handle_unexpected_error`，`logger.exception` 会记下 fpdf2 / fontTools 的异常原文与栈（可能含公司名）；与对账单下载的处置不一致 | §6 日志边界；与 #708 v3 §2「下载」第 3 步一致 |
| 备选 H：字体不可用时退回 Helvetica 印 `?`（降级而不是 500） | 字体丢了也能下载 | 同一份快照在不同部署上得到不同字节，破坏可重现（spec §106）；还会把打包缺陷藏起来 | 与对账单一致：失败要响亮 |

## 10. 未决问题与假设

- **需要 Kelvin 拍板**：
  1. **已开具收据重新下载字节会变**：AIH-TASK-228 部署后，印出的字符串含字体覆盖的非 Latin-1 字符（中文、弯引号 `’`、长破折号 `—` 等）的已开具收据，重新下载与之前字节不同（字形代替 `?`；快照、金额与所有数据不变）；其余收据字节不变。没有任何地方存收据 PDF 或其哈希（§1 证据），#589 v2 §1 完成标准 3「任何时候下载，PDF 的内容相同」按「印出的数据相同」理解时仍成立，按「字节相同」理解则这是一次性的、有意的例外。本稿认为可接受（备选 F 不采用），请确认；需要时管理员先跑 §8 的只读查询看有多少张。
- **请审查者重点看**：
  2. **对账单字节的钉子**：本稿用「冻结参照」差分测试（§7）而不是金样哈希（§9 备选 E）。它钉住的是「共用模块对六份代表性输入给出与搬移前完全相同的字体选择与 `add_font` 行为」，再加上版面代码不改（审查 diff 可见），推出字节不变；它不覆盖「版面代码被改」——那由 AIH-TASK-228 契约的「版面代码一字不改」与代码审查保证。
  3. **公共别名**：`StatementFontUnavailable = PdfFontUnavailable`、`STATEMENT_FONT_SHA256 = NOTO_FONT_SHA256`、`ReceiptFontUnavailable = PdfFontUnavailable`。保留别名是为了让 `app/api/statement_download.py`（接口层）与对账单接口测试在 AIH-TASK-228 里一字不改；备选是在 AIH-TASK-228 里改 `statement_download.py` 的 import，但那会让渲染层任务跨进接口层。
  4. **字体不可用的影响面比「需要中文」略大**：规则第 2 步要读 cmap 才能判断非 Latin-1 字符在不在字体里，所以字体缺失时，印出的字符串里有**任何**非 Latin-1 字符的收据都会 500（包括本批之前能下载、印 `?` 的那些）。这与对账单一致，且字体缺失是打包缺陷、构建期已拦；不为它加降级路径（§9 备选 H）。
  5. **AIH-TASK-229 合并文档**：本稿把 `docs/api.md` 与 `docs/payment-flow.md` 的更新并进接口任务 AIH-TASK-229（共约 200 行），不另设纯文档任务：两处文档描述的正是 AIH-TASK-229 改的下载失败语义（与 AIH-TASK-217 把 `docs/api.md` 并进接口任务同一做法），`docs/TODO.md` 没有要勾的项（Phase 4「支付收据 + 下载」已是 `[x]`，规划块由管理员改）。如审查者认为文档必须单列，拆出第三个任务即可，不影响其他内容。
- **尚未验证的假设**：
  1. 生产里已开具的收据只来自 Kelvin 的测试付款（充值开关在生产关着，#589 v2 §10 假设 3），公司名为拉丁字母；未上库核对（本稿不连生产），§8 的只读查询可核对。
  2. 生产税务政策 `document_notice.en` 是 ASCII：ADR-0008 第 5 节给出的英文原文 `Prices are final and include all charges. No tax is charged separately.` 是 ASCII（`docs/adr/ADR-0008-sst-tax-treatment.md:85`）；若实际发布的文本含弯引号等字符，这些收据的页脚也会改为正确字形（同第 1 条）。
  3. fpdf2 在本仓库的用法下把每段文字写成 `BT x y Td … ET`（§7「一页放得下」的断言依赖它）；本地 fpdf2 2.9.0 的收据输出如此（31 处，最小 y 316.2）。
  4. 对账单 `_Fonts` 自 AIH-TASK-216 合并后没有再改（当前 main 的 `statement_pdf.py:288-323`）；AIH-TASK-228 开工时若已变，冻结参照取开工时 main 上的版本。
- **如果假设错误，具体后果**：1、2 → 受影响的收据更多，处理同第 1 条（数据不变，只是字形）；3 → AIH-TASK-228 的该条断言改用 fpdf2 实际的定位写法，停下在 PR 里说明，不放宽为「只数页数」；4 → 冻结参照照开工时版本，差分测试意义不变。
- **已知限制**：
  1. 「可重现」限于同一代码与依赖版本：升级 fpdf2 / fontTools 可能改变两类 PDF 的字节而不改变内容（与 #708 v3 §10 已知限制 2 相同）。
  2. 换字体文件会同时改变历史对账单与历史收据重新渲染出的字形，要过设计闸门，并同步 `NOTO_FONT_SHA256`。

**本稿没有未解决的财务、安全、租户隔离或不可逆迁移问题**；第 1 条是对「可重现」口径的确认，任何选择都不影响金额、数据与隔离。

## 11. 审查与版本绑定

审查方要回答的五个问题常驻在 [scripts/review_checklist.md](../../scripts/review_checklist.md) 的「设计审查」一节，此处不重复。

**版本绑定规则**（规则的出处是 [WORKFLOW §3](../../docs/WORKFLOW.md)；这里保留一份是因为审查材料里只有本 Issue 正文，两处改一起改）：

- 批准必须写成 `APPROVED: design v<N>`，`<N>` 取本 Issue 顶部的「设计版本」
- **设计版本一变，之前的 APPROVE 自动作废**，必须重新过闸门
- 什么算「实质修改」：改了契约、表结构、事务边界、状态机、失败语义、不变量控制。改错别字不算
- 实质修改时：把顶部版本 +1，在下方追加一条变更说明，状态退回 `READY_FOR_REVIEW`

### 实现拆分

前置：无管理员步骤（字体、许可证、package-data 与 `fonttools` 依赖已由 PR #706 提交）。批准后由管理员在登记 PR 里放入本设计的逐字副本（`docs/design/` 下，Worker 沙箱读不到 Issue）、删去 `docs/TODO.md` 规划块里「待登记：收据 PDF 嵌入中文字体」一行，并按控制面约定跑登记前预审（本批碰个人 / 商业数据的财务文件，预审适用）。

| 任务 | 层 | 标题 | 依赖 | 预计行数 |
| --- | --- | --- | --- | --- |
| `AIH-TASK-228` | 业务规则（渲染） | 收据 PDF 中文字体第一层：抽出共用字体选择模块，收据按对账单同一规则嵌入 Noto Sans SC | AIH-TASK-216（已合并） | ~950 |
| `AIH-TASK-229` | 接口（含文档） | 收据 PDF 中文字体第二层：下载接口补字体不可用与渲染失败的处置，同步接口与支付流程文档 | AIH-TASK-228 | ~200 |

**AIH-TASK-228**

- 可改路径：`app/services/pdf_text.py`、`app/services/statement_pdf.py`、`app/services/receipt_pdf.py`、`app/data/__init__.py`、`tests/backend/pdf_content.py`、`tests/backend/test_pdf_text.py`、`tests/backend/test_statement_pdf.py`、`tests/backend/test_receipt_pdf.py`
- 验收要点：
  1. 新模块 `app/services/pdf_text.py` 照 §2「共用模块」：名字、值与规则第 1–4 步逐条一致；代码由 `statement_pdf.py:251-323` 原样搬移，只改名字；只 import 标准库、`fontTools`、`fpdf`。
  2. `statement_pdf.py` 照 §2「对账单」：只删搬走的定义与 import、`_Fonts(pdf, stack)` 换成按模块全局名调用的 `PdfFonts(pdf, stack)`、`_Layout.__init__` 的注解 `fonts: _Fonts` 换成 `PdfFonts`、加两个公共别名；校验、显示、版面与 `render_statement_pdf` 其余代码一字不改；`__all__` 不变。
  3. `receipt_pdf.py` 照 §2「收据」第 1–8 步：每个值经 `prepare`、标题与标签仍是 Helvetica、坐标字号行高宽度与顺序不变、`ExitStack` 内输出；导出 `ReceiptFontUnavailable`；校验一字不改；文档串同步。
  4. `app/data/__init__.py` 只改文档串（§2「共用模块」末段）；字体与许可证文件不动、不新增二进制文件。
  5. 测试覆盖 §7 标 AIH-TASK-228 的每一行，含「对账单字节不变」的冻结参照差分（六份输入）与「收据：全 Latin-1 字节不变」；`test_receipt_pdf.py` 里只有 `test_characters_outside_latin1_become_question_marks` 一条现有断言可以改（§7「旧的印成 ? 用例」），其余现有收据用例不改；`test_statement_pdf.py` 除新增差分用例与 `_FORBIDDEN_MODULES` 加一项外只做 §7 所列机械替换，现有断言不改；检索工具放进 `tests/backend/pdf_content.py`。
  6. 可重现：中文收据同进程与跨进程（`PYTHONHASHSEED`、`SOURCE_DATE_EPOCH` 不同）字节相同；做不到时停下在 PR 里说明，不得放宽为去掉某些字节再比较。
  7. 不改 `app/api/` 下任何文件、`app/services/receipts.py`、`app/services/statements.py`、模型、迁移、`pyproject.toml`、`scripts/packaging_smoke.py`；不改快照 `schema_version`。
  8. PR 里记一次本地实测：255 个汉字公司名的收据单次渲染耗时与峰值内存。

**AIH-TASK-229**

- 可改路径：`app/api/receipt_download.py`、`tests/backend/test_customer_receipts_api.py`、`docs/api.md`、`docs/payment-flow.md`
- 验收要点：
  1. `receipt_pdf_response` 的异常处理照 §2「下载」三行表：文案、`extra`（只 `receipt_id`，兜底另带 `error_type`）、`raise ReceiptRenderFailed from None`；不用 `logger.exception`；查询、租户过滤、404、响应头与文件名不变；模块文档串同步。
  2. 测试覆盖 §7 标 AIH-TASK-229 的每一行（中文收据下载、字体不可用、渲染异常），SQLite 与 MySQL 两个分片都跑；现有用例不改照过；不写方言相关的 SQL 字面量断言。
  3. `docs/api.md` 照 §2「文档」改客户与管理端两个收据下载小节及错误表的 500 一行，措辞与对账单小节一致；其余内容不改。
  4. `docs/payment-flow.md` 照 §2「文档」改 §14.7 与 §14.10；其余小节不改。
  5. 不改 `app/services/` 下任何文件、`app/api/statement_download.py`、前端与 `docs/TODO.md`。

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
| v1 | 2026-11-16 | 初稿 | — |

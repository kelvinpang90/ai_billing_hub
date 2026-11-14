# AIH-TASK-216..220 设计：月度对账单 PDF 下载（管理端 + 门户）（已批准 v3）

> **来源**：设计闸门 Issue #708。本文件是 `APPROVED: design v3` 那一版正文的**逐字副本**（送审时状态行为 `READY_FOR_REVIEW`）。审查：Codex 额度不可用期间由 Claude Code 审查。v1、v2 各获批准后，登记前预审报出的问题改进了 v2、v3（见 §12），v3 重新审查通过、没有阻断项。
> 放进仓库，是因为 OpenClaw Worker 在沙箱里不联网、读不到 GitHub Issue。
> **实现以本文件为准**；与 Issue 不一致时，以 Issue #708 上被批准的 v3 为准。设计要改，就回到 Issue 升版本、重新过闸门，不要直接改本文件。
> 产品决定（嵌入完整的 Noto Sans SC 字体、税务说明只印英文、开具方名称用常量、快照 v1 不改）由 Kelvin 2026-11-14 拍板，见 §1；字体文件由管理员 PR #706 提交。

## 0. 准入判定

| 任务涉及 | 要填的范围 |
| --- | --- |
| 钱包 / 账本 / 定价 / 汇率 / 支付 / 幂等 / 状态机 | **全部章节** |
| 用量摄取 / 认证与会话（集成侧与平台侧）/ Webhook | §1–§7（**§6 不可省** —— 密钥存储与轮换、鉴权、租户过滤、日志脱敏都在那一节） |
| 前端 / 文档 / CI / 脚本 | **不走本闸门**，直接开 PR |
| 既不碰钱、也不碰用量摄取 / 认证与会话 / Webhook 的后端改动 | **不走本闸门**，直接开 PR |

本任务档位：`全部`（§1–§12）。理由：本批不写钱、不写任何表，按上表本可不走闸门；但它产出的是**客户可见的财务文件**（spec §46「Customer downloads PDF」），内容来自不可变的对账单快照（INV-12），印有客户的公司名与项目名（个人 / 商业数据），并涉及租户隔离（INV-8）与「客户不可见成本毛利」（INV-7）。照收据 PDF（设计闸门 #589 v2，`docs/design/AIH-TASK-196-payment-receipts.md`）的先例走完整模板；不适用的章节写明理由。

---

状态：`READY_FOR_REVIEW`
负责人：Claude
**设计版本**：`v3`
对应需求：spec §46（字段、「Customer downloads PDF」、不得显示成本 / 毛利 / 加价）、§65（管理员下载 PDF、从快照重渲染、不按今天的定价重算）、§80（显示 2 位小数）、§93、§106（服务端生成、只用稳定快照、可重现）、§130「PDF download」；ADR-0003；ADR-0008 §5；Invariant 6、7、8、10、12；建立在设计闸门 #565 v6（月度对账单，`docs/design/AIH-TASK-154-monthly-statements.md`）与 #589 v2（收据 PDF 的写法）之上
目标 PR：待开（前置管理员步骤 `P0` 即 PR #706，实现拆成 `AIH-TASK-216`..`220`，见 §11）

## 1. 目标与边界

### 要解决的问题

1. spec §46 写「Customer downloads PDF」，§65 写管理员「Download PDF」「regenerate file rendering from statement snapshot」，§130 把「PDF download」列入 Phase 7。月度对账单（#565 v6）只交付了生成、定稿与两端的 JSON 查看，并明确「不做 PDF 下载与从快照重新渲染文件（Phase 7 后续批）」（`docs/design/AIH-TASK-154-monthly-statements.md:42`）。今天客户与管理员都拿不到对账单文件。
2. 对账单快照里的公司名与项目名可能含中文；现有唯一的 PDF 渲染（收据，`app/services/receipt_pdf.py:13-14`、`:137-139`）只用 Helvetica，Latin-1 之外的字符印成 `?`。对账单要能正确显示中文。

### Kelvin 2026-11-14 的决定

| 编号 | 问题 | 决定 |
| --- | --- | --- |
| 6 | 中文显示 | **嵌入开源 CJK 字体 Noto Sans SC（SIL OFL 1.1）**：fpdf2 按用到的字子集化嵌入 PDF；字体文件进公开仓库（数 MB）。收据 PDF 里中文印成 `?` 的问题另立后续项，**本批不改收据** |
| 6a | 字体形态（看过初稿后同日补定） | **完整的静态 Regular 字体（约 10 MB），不预先子集化**：避免生僻字印成 `?`；由管理员从 Google Fonts 官方来源下载、经 P0 提交（见 §2「P0」） |
| 6b | 税务说明（同上） | **只印 `tax_notice.en`**：与收据 PDF、两端对账单页面一致 |
| 7 | 开具方名称 | **用常量，与收据一致**（`app/services/receipts.py:43` 的 `RECEIPT_ISSUER_NAME = "Acuven Technology Sdn Bhd"`）；**对账单快照 v1（`schema_version = 1`）不改** |

我定的（未单独问，审查者重点看的列在 §10）：

- 即时渲染、不存文件（与收据同一做法：spec §93 的「may be stored」不做，没有卷、没有存储抽象；每次下载都从快照渲染，所以 §65 的「regenerate file rendering」就是每一次下载本身）。
- 版面只用英文标签；快照里的字符串（公司名、项目名、供应商与模型代码）原样印出，含中文时用 Noto Sans SC。
- 只有 `FINALIZED` 的对账单可以下载（客户与管理员都是）。
- 金额一律按 2 位小数 `ROUND_HALF_UP` 显示（与门户对账单页一致），合计行取快照里的合计值，不由显示值相加。

### 可观察的完成标准

1. 客户在门户对账单详情里点「Download PDF」，得到 `<statement_number>.pdf`；管理员在管理端对账单详情里同样能下载任何客户的已定稿对账单。
2. PDF 含 spec §46 列出的全部字段（对账单号、账期、期初、充值、本期 AI 用量、调整、上期用量与 rebill 调整、期末、请求数、会话数、输入 / 输出 token、按供应商 / 模型 / 项目的用量、客户计费合计），外加开具方名称、客户公司名、定稿时刻与快照里冻结的 `tax_notice.en`；**不含**任何成本、毛利、加价、汇率、税额栏或 SST 注册号（spec §46、ADR-0008 §5）。
3. 公司名或项目名含中文时正确显示中文（字体子集嵌入）；字体里没有的字符印成 `?`。
4. 同一份对账单任何时候、任何进程下载，PDF 字节完全相同（同一代码与依赖版本下）；之后改公司名、改项目名、发布新的税务政策版本，都不改变已定稿对账单的 PDF（spec §106、§65）。
5. 明细行再多也能完整打印：自动分页、每页页脚有「Page n of N」、续页重复表头。
6. 别的租户的、不存在的、非 `FINALIZED` 的、id 形状不对的对账单，客户下载一律 404 `STATEMENT_NOT_FOUND`，与不存在不可区分。

### 明确不做什么

- **不改收据**：`app/services/receipt_pdf.py` 与收据接口一字不动；收据中文印成 `?` 另立后续项（管理员在登记 PR 里往 `docs/TODO.md` 规划块「后续计划」加一行「待登记」，Worker 不改规划块）。
- **不改对账单快照与表**：`schema_version` 仍是 1，`build_snapshot`（`app/services/statements.py:774-845`）与迁移 0035 不动；不加迁移。
- **不存 PDF**、不加卷、不做文件存储抽象（spec §93 的「may」）。
- **不做中文版面**（标签全英文）、不做双语税务说明（Kelvin 决定 6b）。
- **不做作废 / 更正版对账单的 PDF**（V1 没有 `DRAFT` / `VOID` 的写入方，`app/models/statements.py:57-63`），也不给非定稿行加水印。
- **不做邮件附件、出单通知、批量下载、列表页按钮**：只在两端的详情卡片里加下载按钮。
- **不做收入 / 成本 / 毛利分析**（Phase 7 其余部分）。

### 现有行为与证据

- 表与快照：`monthly_statements`（`app/models/statements.py:73-141`），`statement_number` 为 `ST-<YYYYMM>-` 加 8 个 Crockford base32 字符（`app/services/statements.py:871-877`，字母表 `:94`），`finalized_at` 在 `FINALIZED` 时非空（CHECK `:70`），MySQL 触发器拒绝改删非 `DRAFT` 行（迁移 0035，模型文档串 `:11-14`）。快照由 `build_snapshot` 构造（`app/services/statements.py:774-845`）：19 个顶层键；金额经 `money_text` 写成**恰好 8 位小数的带符号字符串**（`app/schemas/customers.py:305-307`）；计数是 `int`；`prior_period.usage[]` 项为 `usage_period`、`request_count`、`amount`；三组明细的键见 `:816-844`；**快照里没有对账单号、定稿时刻与开具方**（对账单号与定稿时刻在不可变的列上）。
- 金额符号：快照是账本的带符号金额（`total_ai_usage ≤ 0`，`docs/design/AIH-TASK-154-monthly-statements.md:129`）；明细的 `charges` 是 `billable_cost` 之和（≥ 0）。门户与管理端页面都原样显示符号（`frontend/src/features/portal/StatementsPage.tsx:246-295`）。
- 管理端接口：`app/api/admin_statements.py`：`GET /statements`（`:67`）、`GET /statements/{statement_id}`（`:112`，先 `require_admin`、再 `is_statement_id`，不按状态过滤）。客户接口：`app/api/customer.py:705`（列表）、`:738`（详情：`require_customer`、`is_statement_id`、`tenant_id = principal.tenant_id`、`status = FINALIZED`，都不满足是同一个 404）。视图与 `StatementNotFound`（404 `STATEMENT_NOT_FOUND`）在 `app/schemas/statements.py:38-42`、`:120-122`。
- 收据 PDF（本批照它的写法）：`app/services/receipt_pdf.py`（先严格校验键集合再渲染 `:104-134`；`_display_money` 先 `quantize(ROUND_HALF_UP)` 再格式化、`-0.00` 去符号 `:148-157`；`set_compression(False)`、`set_creation_date(issued_at UTC)`、`set_title` 让输出可字节级重现 `:187-194`；不 import 会话、模型、配置）。下载：`app/api/receipt_download.py`（会话里只读、会话关闭后渲染 `:72-86`；文件名只来自编号、`Content-Disposition: attachment`、`Cache-Control: no-store` `:105-111`；`tenant_id` 无缺省值、显式传 `:89-95`）；路由写法 `app/api/customer_payments.py:124-130`（`response_class=ReceiptPdfResponse`）。测试 `tests/backend/test_receipt_pdf.py`（在未压缩的内容流里检索 `(文字)` 字面量 `:57-91`；两次渲染字节相同 `:141-142`；元数据 `:145-156`）。
- 依赖与打包：`fpdf2>=2.8` 已在 `pyproject.toml:41`；数据文件靠 `[tool.setuptools.package-data] "app.data" = ["*.txt"]` 进 wheel（`pyproject.toml:63-67`），`app/data/` 是包（`app/data/__init__.py`），读取走 `importlib.resources`（`app/core/passwords.py:10`、`:42`）；Docker 两阶段构建只装 wheel，构建期跑 `scripts/packaging_smoke.py`（`Dockerfile`），CI 也在干净环境装好后跑它（`.github/workflows/ci.yml:157-158`）。
- 前端下载：`frontend/src/api/download.ts` 的 `downloadFile(url, fallbackFilename, signal?)`（带令牌、按 `Content-Disposition` 取文件名、失败按信封解析，超时 120 秒）；收据的封装 `downloadAdminReceipt`（`frontend/src/api/adminPayments.ts:194`）。门户显示规则 `formatAmountRounded`（`frontend/src/components/MoneyText.tsx:59-76`：字符串 HALF_UP、千分位、零不带负号）。
- 门户只读状态：`require_customer` 对 `DISABLED` 也放行（#565 v6 §2「客户接口」末条，AIH-TASK-020 契约）；下载是读，同样放行。
- 浏览器验收：已有 `open_statements`（管理端）与 `check_portal_statements`（门户）两个只读步骤，**都不点行**（`scripts/acceptance/admin_customers.mjs:170-171`、`:530-538`）。生产首个账期 `2026-12` 在 2027-01-02 01:00 才生成（`app/services/statements.py:65`），在那之前生产没有任何对账单。

## 2. 设计概要

### 数据流

```text
客户 GET /api/v1/customer/statements/{id}/download ─┐
管理员 GET /api/v1/admin/statements/{id}/download ──┤
                                                     ▼
            statement_pdf_response(factory, id, tenant_id=…)
              ① id 不是小写规范 uuid → 404 STATEMENT_NOT_FOUND
              ② 只读会话：SELECT public_id, statement_number, finalized_at, snapshot_json
                 WHERE public_id = id AND status = 'FINALIZED' [AND tenant_id = 客户的租户]
                 没有 → 404；会话关闭
              ③ render_statement_pdf(snapshot, statement_number=…, finalized_at=…)
                   校验 → 选字体（Helvetica / Noto Sans SC 子集）→ 版面 → bytes
                 失败 → ERROR 日志（只带对账单 public_id）→ 500 INTERNAL_ERROR
              ④ 200 application/pdf 附件 <statement_number>.pdf，no-store
```

没有写入、没有状态变化、没有异步任务、没有出站事件。

### P0（管理员，Worker 之前）：字体文件进仓库

Worker 的沙箱不联网，取不到字体；字体文件也是二进制，Worker 能否提交二进制文件未经验证（§10）。所以由管理员先开一个 PR（不走 OpenClaw），内容恰好：

| 文件 | 内容 |
| --- | --- |
| `app/data/NotoSansSC-Regular.ttf` | Noto Sans SC Regular 的**静态 TrueType**（`glyf` 轮廓）字体，取自官方发行；不用可变字体（`[wght]`），不用 CFF 轮廓的 `.otf`（fpdf2 对两者的支持未验证，§10） |
| `app/data/NotoSansSC-OFL.txt` | 该字体随附的 SIL Open Font License 1.1 全文（OFL 要求再分发时随附；`*.txt` 已在 package-data 里，wheel 与镜像都会带上） |
| `pyproject.toml` | `[tool.setuptools.package-data] "app.data" = ["*.txt", "*.ttf"]`；`dependencies` 加 `fonttools>=4.34`（fpdf2 2.8 的传递依赖，AIH-TASK-216 会直接 `import fontTools`，不依赖传递关系；版本下界以 fpdf2 2.8 的 install_requires 为准，管理员加时核对） |
| `app/data/__init__.py` | 文档串补一节「NotoSansSC-Regular.ttf」：来源、版本号、许可证（OFL 1.1，文件 `NotoSansSC-OFL.txt`）、文件 SHA-256 |

**P0 已由管理员提交：PR #706**（AIH-TASK-216 依赖它先合并）。实际文件：Google Fonts 的 Noto Sans SC 静态 Regular，字体版本 2.004，`glyf` 轮廓、无 `fvar`、无 `CFF `，cmap 30,890 个码位；10,559,284 字节；SHA-256 `d45f67f0a7c0ca3f256950777ce6a61cc7ce5f9696d02900cbbaac25f8aa7d16`（`STATEMENT_FONT_SHA256` 取这个值）。`fonttools>=4.34` 的下界与 fpdf2 元数据 `Requires-Dist: fonttools>=4.34.0` 一致。PR #706 的本地验证：干净虚拟环境安装后 `importlib.resources` 读得到字体与许可证、哈希一致，`scripts/packaging_smoke.py` 通过；用 fpdf2 2.9.0 `add_font` 该文件写一行中英混排，未压缩的 PDF 只有 8,854 字节（只嵌入用到的字形），`PYTHONHASHSEED` 取 1、2、999 三个进程的输出字节完全相同。

### PDF：`render_statement_pdf`（`app/services/statement_pdf.py`，AIH-TASK-216）

签名：`render_statement_pdf(snapshot: dict[str, Any], *, statement_number: str, finalized_at: datetime) -> bytes`。

`statement_number` 与 `finalized_at` 来自 `monthly_statements` 的列：定稿行上不可变（触发器），快照 v1 里没有它们（Kelvin 决定 7：不改快照）。**只读这三个输入**：不 import 会话、模型、配置与 `app/services/statements.py`（与 `receipt_pdf.py` 同一规则），所以改公司名、改项目名、改定价、改税务政策都不改变重渲染的结果（spec §106、§65，INV-6）。

#### 1. 校验（先校验、再渲染）

任何一条不合规都抛 `StatementSnapshotInvalid`（不抛别的异常），**不修改输入字典**：

- `statement_number` 是字符串且完全匹配 `ST-[0-9]{6}-[0-9A-HJKMNP-TV-Z]{8}`（`fullmatch`），且其中的 `YYYYMM` 等于快照 `period` 去掉连字符；`finalized_at` 是不带时区的 `datetime`。
- 顶层键集合**恰好**是这 19 个：`schema_version, period, currency, company_name, tax_notice, opening_balance, total_topup, total_ai_usage, total_adjustments, prior_period, closing_balance, request_count, conversation_count, input_tokens, output_tokens, total_customer_charges, by_provider, by_model, by_project`。多一个、少一个都不合规——**多出来的键不印也不忽略**，直接拒绝（防止将来误把成本类键加进快照后被静默接受，INV-7）。
- `schema_version` 是 `int`、不是 `bool`、等于 1。**未知版本**（2 及以上、0、非整数）一律不合规：v1 是唯一的写入版本；将来升快照版本必须另过设计闸门，在本模块按版本分派一个新的校验与版面，不在 v1 的版面上「尽量印」。
- `period` 完全匹配 `[0-9]{4}-(0[1-9]|1[0-2])`，年份在 2026–2999（2026 是首个账期 `2026-12` 的年份，2999 是 `app/services/statements.py:70` 的上界）。
- `currency` 等于 `MYR`。
- `company_name` 是非空字符串。
- `tax_notice` 是键集合恰好 `{zh, en}` 的对象，两个值都是非空字符串（只印 `en`，但两者都校验，与收据 `receipt_pdf.py:132-133` 一致）。
- 金额字段（`opening_balance`、`total_topup`、`total_ai_usage`、`total_adjustments`、`closing_balance`、`total_customer_charges`、`prior_period.rebill`、`prior_period.total`、每个 `prior_period.usage[].amount`、每个明细的 `charges`）：字符串且完全匹配 `-?[0-9]{1,12}\.[0-9]{8}`（`DECIMAL(20,8)` 的 `money_text` 输出形状）。
- 计数字段（`request_count`、`conversation_count`、`input_tokens`、`output_tokens`、每个 `usage[].request_count`、每个明细的三个计数）：`int`、不是 `bool`、`≥ 0`。
- `prior_period` 键集合恰好 `{usage, rebill, total}`；`usage` 是列表，每项键集合恰好 `{usage_period, request_count, amount}`，`usage_period` 形状与年份范围（2026–2999）都同 `period`，且字符串比较小于 `period`。
- `by_provider` 每项恰好 `{provider_code, request_count, input_tokens, output_tokens, charges}`；`by_model` 每项恰好 `{provider_code, model_code, request_count, input_tokens, output_tokens, charges}`；`by_project` 每项恰好 `{project_public_id, project_name, request_count, input_tokens, output_tokens, charges}`；其中的字符串字段都是非空字符串。
- **不复核算术**（期初 + 发生额 = 期末等）：生成时的三条自检已保证（#565 v6 §2「自检」），渲染只负责如实印出快照；理由见 §9。

#### 2. 文本与字体

每个要印出的字符串按下面的顺序处理（固定英文标签本来就是 ASCII，走第 1 步即止）：

1. 字符串里每个字符都 `ord(c) < 256`（Latin-1 可编码）→ 用 **Helvetica**（标准字体，不嵌入），原样交给 fpdf2。
2. 否则：把「既不在 Latin-1、也不在 Noto Sans SC 的 cmap 里」的字符换成 `?`；替换后若已全部 Latin-1 可编码 → Helvetica；否则 → **Noto Sans SC**（`add_font` 只在本次渲染第一次需要时调用一次，fpdf2 在输出时只把用到的字形子集化嵌入）。
3. 选定 Noto Sans SC 的字符串里，凡**不在字体 cmap 里**的字符（包括第 2 步保留下来的 Latin-1 字符中字体没有的，例如 C0 / C1 控制字符）也一律换成 `?`，不出现缺字框（`.notdef`）。**换行符 `
` 例外**：它是版面指令、不是要印的字形（第 7 节 `Notes` 第 3 条的 `multi_cell` 要靠它换行），本步与第 2 步都不替换它；`
`、`	` 等其他控制字符照替换。
4. cmap 用 `fontTools.ttLib.TTFont(path, lazy=True).getBestCmap()` 的键集合，进程内缓存一次（`functools.lru_cache`；并发下可能重复计算，结果相同，无害）。字体路径经 `importlib.resources.files("app.data").joinpath("NotoSansSC-Regular.ttf")` 与 `resources.as_file` 取得。
5. 读字体失败（文件不在、读不出、不是 TrueType）→ 抛 `StatementFontUnavailable`。**只有需要中文字体的对账单会因此失败**；全 Latin-1 的对账单不读字体文件、照常渲染。

由此：全 Latin-1 的对账单 PDF 里不嵌入任何字体文件（测试可在未压缩的内容流里检索 `(文字)` 字面量，与收据测试同一方法）；含中文的对账单只嵌入用到的字形。

常量 `STATEMENT_FONT_SHA256`（`statement_pdf.py`）等于 P0 写进 `app/data/__init__.py` 的 SHA-256；测试核对文件哈希。换字体文件会改变历史对账单 PDF 的字形，所以要改这个常量就要过设计闸门。

#### 3. 版面（A4 纵向，单位 mm，可多页）

- 边距 15；**自动分页开**（`set_auto_page_break(auto=True, margin=20)`）；`alias_nb_pages()`。
- **页脚**（每页，Helvetica 8，居中）：`<statement_number> | Page <n> of <N>`。
- **续页页眉**（第 2 页起，Helvetica 9）：`Monthly statement <statement_number> (continued)`。
- 第 1 页抬头：
  - 标题 `MONTHLY STATEMENT`（Helvetica B 18）；下一行开具方 `STATEMENT_ISSUER_NAME`（常量 `"Acuven Technology Sdn Bhd"`，定义在 `statement_pdf.py`；**不 import** `app/services/receipts.py`，那会把模型与 SQLAlchemy 带进渲染模块；测试断言两个常量相等，Kelvin 决定 7）。
  - 标签与值各一行：`Statement No.`（对账单号）、`Billing period`（`2026-12 (1 Dec 2026 - 31 Dec 2026, MYT)`：起止日由 `calendar.monthrange` 算，月份缩写用代码里的固定元组 `Jan`..`Dec`，**不用** `strftime("%b")`——它随进程 locale 变化，破坏可重现）、`Customer`（`company_name`，过长自动换行）、`Currency`（`MYR`）、`Finalized`（`finalized_at` 换算马来西亚时间，`YYYY-MM-DD HH:MM MYT`）。
- 第 1 节 `Balance summary`（两列表）：`Opening balance`、`Top-ups`、`AI usage this period`、`Adjustments`、`Prior-period adjustments`（= `prior_period.total`）、`Closing balance`。
- 第 2 节 `Prior-period adjustments`：先一行固定说明 `Usage from an earlier month that was processed after that month's cut-off (00:00 MYT on the 2nd day of the following month) is shown here under its original usage period.`；表头 `Usage period | Requests | Amount`；每个 `usage[]` 一行（按快照顺序），再一行 `Rebill | - | <rebill>`，再一行 `Total | - | <total>`（与门户页 `frontend/src/features/portal/StatementsPage.tsx:193-212` 的行结构相同）。
- 第 3 节 `Usage summary`：`Requests`、`Conversations`、`Input tokens`、`Output tokens`、`Total customer charges`。
- 第 4–6 节 `Usage by provider`（`Provider | Requests | Input tokens | Output tokens | Charges`）、`Usage by model`（`Provider | Model | …`）、`Usage by project`（`Project | …`，只印 `project_name`，**不印** `project_public_id`，与门户页 `:352-365` 一致）。行序 = 快照里的顺序（生成时已按码点排序），渲染不重排。列表为空时该节只印一行 `No usage in this period.`。
- 第 7 节 `Notes`（Helvetica 9）：
  1. `Amounts are shown in MYR rounded to 2 decimal places. Each total is rounded from its exact value, so it may differ slightly from the sum of the rounded lines.`
  2. `The closing balance is calculated by period attribution (T+1 cut-off) and may differ from the wallet balance at the end of the month.`
  3. `tax_notice.en` 原文（`multi_cell`，保留原文里的换行，不改写、不拼接）。
- 表格：用 fpdf2 的 `FPDF.table()`（2.7 起有，`fpdf2>=2.8` 满足），`repeat_headings=1`（续页重复表头）；单元格按第 2 步各自选字体（`FontFace(family=…)`）；一行内容换行时行高随之增长，**一行不跨页拆开**。数值列右对齐。
- 数字格式：计数与 token 用千分位整数（`f"{n:,}"`，与 locale 无关）；金额见下。

#### 4. 金额显示

- `MYR <值>`：先 `Decimal(text).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)`，结果为 0 时去掉符号（不印 `-0.00`），再 `f"{value:,.2f}"`。与 `receipt_pdf.py:148-157` 同一规则（前缀不同：收据印 `RM`，对账单印 `MYR`，与门户对账单页 `MYR 87.50` 一致），与门户 `formatAmountRounded` 的 HALF_UP（远离零）结果相同。不经 float（INV-10）。
- **符号原样**：`total_ai_usage` 等账本金额是负数就印负数，明细 `charges` 是正数就印正数，与两端页面一致，不翻转。
- **合计不由显示值相加**：每个合计行（`Closing balance`、`Prior-period adjustments`、`Total`、`Total customer charges`）印快照里该合计字段自己的舍入值。舍入后合计与各行显示值之和可能差几分，例：`opening_balance = 0.00500000`、`total_topup = 0.00500000`、其余为 0、`closing_balance = 0.01000000` → 印 `MYR 0.01`、`MYR 0.01`、期末 `MYR 0.01`（显示值相加是 0.02）。这是预期行为，由第 7 节说明 1 告知读者；**不做「把差额摊到某一行」**的调整（那会印出快照里没有的数）。

#### 5. 可重现

- `set_compression(False)`；`set_creation_date(finalized_at.replace(tzinfo=UTC))`；`set_title(statement_number)`；不设任何取自当前时间、随机数、主机名或环境变量的元数据。
- 同一份输入在**同一进程两次**、以及在**两个子进程**（`PYTHONHASHSEED` 不同、`SOURCE_DATE_EPOCH` 不同）中渲染，字节完全相同——含嵌入中文字体子集的情况。后一条专门挡住两类风险：子集化对 `set` 迭代顺序的依赖（哈希随机化），以及 fontTools 保存字体时重算 `head.modified` 时间戳（它读当前时间或 `SOURCE_DATE_EPOCH`）。
- 「可重现」的范围是**同一代码与依赖版本**：fpdf2 在 PDF 里写自己的 Producer 版本串，升级 fpdf2 会改变字节而不改变内容；这与收据一致（§10）。

### 下载：`statement_pdf_response`（`app/api/statement_download.py`，AIH-TASK-217）

`statement_pdf_response(factory, statement_id, *, tenant_id: int | None) -> StatementPdfResponse`，照 `app/api/receipt_download.py` 的结构：

1. `is_statement_id(statement_id)` 不成立 → `StatementNotFound`（现有，404 `STATEMENT_NOT_FOUND`）。（形状判断在路由的鉴权之后，因为路由的第一句就是鉴权，见下。）
2. 一个普通只读会话：`SELECT public_id, statement_number, finalized_at, snapshot_json FROM monthly_statements WHERE public_id = :id AND status = 'FINALIZED'`，`tenant_id` 不为 `None` 时再加 `AND tenant_id = :tenant_id`。不加锁、不提交、不写审计。没有行 → `StatementNotFound`。**会话在此关闭**。
3. 会话关闭之后调用 `render_statement_pdf`。异常处理（日志都只带 `extra={"statement_id": <public_id>}`，不带快照内容、公司名、金额与异常原文）：
   - `StatementSnapshotInvalid` → ERROR `Statement snapshot invalid; download refused`；
   - `StatementFontUnavailable` → ERROR `Statement font unavailable; download refused`；
   - 渲染中的任何其他异常（例如 fpdf2 内部错误）→ ERROR `Statement PDF rendering failed`，另带 `"error_type": type(exc).__name__`。不交给通用处理器：`handle_unexpected_error` 用 `logger.exception`（`app/core/errors.py:147`），会把异常原文写进日志，而 fpdf2 的异常文案可能含被渲染的文字（公司名）。
   - 三者都抛 `StatementRenderFailed`（新类，与 `ReceiptRenderFailed` 同形：500、`INTERNAL_ERROR`、固定文案 `An internal error occurred.`），`raise … from None`。
4. 200：`StatementPdfResponse`（`Response` 子类，`media_type = "application/pdf"`），头 `Content-Disposition: attachment; filename="<statement_number>.pdf"`、`Cache-Control: no-store`。文件名只来自对账单号（第 1 节已校验它是固定 ASCII 形状），不拼任何客户输入。

`tenant_id` 参数没有缺省值（漏传是类型错误，不是「不过滤」），与收据同一写法。

### 客户接口（`app/api/customer.py`，AIH-TASK-217）

`GET /api/v1/customer/statements/{statement_id}/download`，`response_class=StatementPdfResponse`：

```python
principal = require_customer(request)   # 第一条语句；DISABLED 也放行（只读）
return statement_pdf_response(require_session_factory(request), statement_id, tenant_id=principal.tenant_id)
```

| 情形 | 结果 |
| --- | --- |
| 本租户、`FINALIZED` | 200 PDF |
| 不是小写规范 uuid / 不存在 / 别的租户 / 非 `FINALIZED` | 404 `STATEMENT_NOT_FOUND`（同一响应，不区分） |
| 未登录 / 令牌无效 | 401（`require_customer` 现有行为） |
| `require_customer` 拒绝的其他情形 | 403（现有行为，与详情接口相同） |
| 快照不合规 / 字体不可用 / 渲染失败 | 500 `INTERNAL_ERROR` |
| 数据库未配置 | 503（`require_session_factory` 现有行为） |

### 管理端接口（`app/api/admin_statements.py`，AIH-TASK-217）

`GET /api/v1/admin/statements/{statement_id}/download`，`response_class=StatementPdfResponse`；**第一条语句 `require_admin(request)`**（`app/api/admin_statements.py:6-8` 的约定；路由表枚举测试自动覆盖 401 / 403 且不写库）；之后 `statement_pdf_response(…, tenant_id=None)`。错误与客户接口相同：不存在、形状不对、非 `FINALIZED` → 404 `STATEMENT_NOT_FOUND`（非定稿行在 V1 不存在，见 §10 未决问题第 1 条）。不写审计（与收据下载、对账单详情一致：只读）。

### 门户页面（AIH-TASK-218）

- `frontend/src/api/customerStatements.ts`：新增 `downloadCustomerStatement(statementId, statementNumber, signal?)`，经 `downloadFile` 请求 `${CUSTOMER_STATEMENTS_URL}/${statementId}/download`，回退文件名 `${statementNumber}.pdf`；`statementId` 原样进路径。
- `frontend/src/features/portal/StatementsPage.tsx` 的详情卡片（`StatementDetailCard`）：详情读成功后，卡片标题栏右侧（`Card` 的 `extra`）显示按钮 `Download PDF`。下载中按钮 `loading` 且不可再点；失败时在详情正文上方显示一条错误（错误码、信息与 `request_id`，用现有 `RequestReference`），下一次点下载时清除；换一份对账单时卡片按 `key` 重建，错误随之消失。详情加载中或读失败时不显示按钮。列表、翻页、点行行为不变。
- 文案进 `frontend/src/i18n/locales/en.json`（`portal.statements.download.button`、`portal.statements.download.failed`）。

### 管理端页面（AIH-TASK-219）

- `frontend/src/api/adminStatements.ts`：新增 `downloadAdminStatement(statementId, statementNumber, signal?)`，请求 `/api/v1/admin/statements/${statementId}/download`。
- `frontend/src/features/statements/StatementsPage.tsx` 的详情卡片：**仅当 `status === "FINALIZED"`** 时在 `extra` 显示 `Download PDF`；交互同门户（下载中不可再点、失败显示错误码 / 信息 / `request_id`、下一次点下载或换一份时清除）。补生成按钮、筛选与列表不变。
- 文案键 `statements.download.button`、`statements.download.failed`。

### 事务边界

只有一个只读会话（第 2 步），渲染在会话关闭之后（不持连接渲染）。没有写事务。

### 时间语义

库里不带时区的 UTC。PDF 上的定稿时刻按马来西亚时间（UTC+8）显示并标 `MYT`；账期起止日由账期标签按日历算出（马来西亚日历月，#565 v6 §2「账期与时间」）。PDF 元数据的创建时刻 = 列 `finalized_at`（UTC）。

### 金额与精度

快照存 8 位小数字符串，渲染只在显示边界做一次 `ROUND_HALF_UP` 到 2 位（spec §80「MYR normal display → 2 decimals」；「Detailed usage may display up to 6 decimals」是允许不是要求，本批与门户页一致取 2 位，§10 未决问题第 2 条），不改持久值，不经 float。

## 3. 不变量影响矩阵

| 不变量 | 是否触碰 | 可能怎样被破坏 | 设计控制 | 验证方式 |
| --- | --- | --- | --- | --- |
| INV-1 中心故障不中断 AI | 否 | 下载接口不在 AI 调用、摄取与计费路径上；渲染在 api 进程的请求线程里，与 AI 模块共用进程，但只占一次请求 | 只读、无锁；渲染在会话关闭后，不占连接 | 不新增 |
| INV-2 事件不重复扣费 | 否 | 本批不读不写用量事件与账本 | 只读 `monthly_statements` 一行 | 不新增 |
| INV-3 支付不重复入账 | 否 | 不碰支付 | — | 不新增 |
| INV-4 余额只经账本变动 | 否 | 不写钱包；期初期末只是快照里的数 | — | 不新增 |
| INV-5 历史账本不可变 | 否 | 连账本都不读 | — | 不新增 |
| INV-6 事件保留版本引用 | 是 | 渲染时读当前的税务政策、公司名或项目名，改动后历史对账单跟着变（spec §106、§65） | 渲染函数只收快照与两个不可变列；模块不 import 会话、模型、配置；税务说明只取快照 `tax_notice.en` | §7「渲染只读输入」「改名与新税务政策后字节不变」 |
| INV-7 客户不可见成本毛利 | 是 | PDF 印出成本、汇率、加价；将来快照多了成本类键被静默印出或忽略 | 顶层与每层键集合恰好相等，多一个键即拒绝；版面只印列出的字段；不印税额栏与 SST 号（ADR-0008 §5） | §7「键集合」「PDF 不含成本类字样」 |
| INV-8 租户不可互访 | 是 | 客户下载别人的对账单 | 租户只来自令牌主体，SQL 带 `tenant_id`；别人的与不存在的同为 404；管理端 `tenant_id=None` 只在 `require_admin` 之后 | §7「跨租户」 |
| INV-9 对话内容不入库 | 否 | 快照只有计数、token 数、金额、代码与项目名（#565 v6 §3） | 不读事件与会话 | 不新增 |
| INV-10 金额用 Decimal | 是 | 显示经 float 或默认 `ROUND_HALF_EVEN` | `Decimal.quantize(ROUND_HALF_UP)` 后格式化；零去符号 | §7「金额显示」 |
| INV-11 event_id 至多一次财务效果 | 否 | 没有财务效果 | — | 不新增 |
| INV-12 定稿对账单不可变 | 是（只读） | 下载路径改写快照或重算金额（spec §65「Do not recalculate」） | 只 SELECT、不提交；只认 `FINALIZED`；渲染不复算任何金额、不读账本；不改表与触发器 | §7「下载不改行」 |
| INV-13 状态与事件原子提交 | 否 | 没有状态变化、审计与出站事件（下载是读，与详情接口一样不写审计） | — | 不新增 |
| INV-14 队列丢失不毁持久工作 | 否 | 文档生成是请求内同步渲染，不经 Celery、不留持久的生成任务；Redis 丢了也照常下载 | 不加任务 | 不新增 |

## 4. 状态与并发

### 状态

本批没有状态与状态变化。对账单的状态机（#565 v6 §4）不变；下载只接受 `FINALIZED`。

| 当前状态 | 事件 | 前置条件 | 新状态 | 副作用 | 非法时结果 |
| --- | --- | --- | --- | --- | --- |
| `FINALIZED` | 下载 | 客户：本租户；管理员：已过 `require_admin` | `FINALIZED`（不变） | 无 | — |
| `DRAFT` / `VOID` / 无行 | 下载 | — | 不变 | 无 | 404 `STATEMENT_NOT_FOUND` |

### 并发

- 并发下载互不影响：每次下载一个新的 `FPDF` 实例；没有共享可变状态，除了 cmap 的进程内缓存（只读结果，重复计算无害）。
- 下载与生成并发：生成插入的行在提交前对下载不可见，提交后才可下载；定稿行之后不可改，所以同一份对账单的任意两次下载读到同一份数据。
- 数据库唯一性、幂等键、相同 ID 不同载荷、重试层：不适用（只读；同一个请求重发得到相同字节）。

## 5. 失败与恢复矩阵

| 失败点 | 对外结果 | 数据最终状态 | 是否可重试 | 恢复来源 | 告警 |
| --- | --- | --- | --- | --- | --- |
| id 形状不对 / 不存在 / 别人的 / 非 `FINALIZED` | 404 `STATEMENT_NOT_FOUND` | 不变 | — | — | 无 |
| 快照不合规（数据缺陷，正常生成不会产生） | 500 `INTERNAL_ERROR`；ERROR 日志带对账单 `public_id` | 不变；这一份对账单**永久**下载不了（行不可改，INV-12） | 否 | runbook：按日志里的 `public_id` 查行、核对快照哪一处不合规；不改、不删行；修复要另过设计闸门（作废与更正版本，#565 v6 §10「后续」）。客户仍可在门户看 JSON 详情 | 仅 ERROR 日志 |
| 需要中文字体但字体文件缺失或损坏（打包缺陷） | 500；ERROR `Statement font unavailable` | 不变；只影响含非 Latin-1 字符的对账单 | 修复后是 | 重新部署带字体的镜像（packaging smoke 在构建期与 CI 已拦，正常到不了生产） | 仅 ERROR 日志 |
| 渲染时其他异常（fpdf2 内部错误） | 500；ERROR 带异常类型名 | 不变 | 视原因 | runbook：按 `public_id` 在本地用同一快照复现 | 仅 ERROR 日志 |
| 数据库不可用 / 查询失败 | 503（未配置）或 500（现有通用处理） | 不变 | 是 | 数据库恢复后重试 | 现有数据库告警 |
| DB 提交失败 | 不适用：没有写入 | — | — | — | — |
| Redis / Celery 丢失 | 无影响（不经队列） | 不变 | — | — | — |
| 外部服务超时 | 不适用：不调外部服务 | — | — | — | — |
| 前端下载超时（> 120 秒） | 门户 / 管理端显示错误码 `NETWORK_ERROR` 类错误 | 不变 | 是 | 重试；正常渲染远低于此（§10 假设 5） | 无 |
| 重复请求 | 每次 200、字节相同 | 不变 | — | — | — |

## 6. 数据与安全边界

- **鉴权**：客户 `require_customer`（只读：`DISABLED` 也可下载自己的已定稿对账单）；管理端显式 `require_admin`（ADMIN 登录必过 2FA）。两者都是路由的第一条语句。
- **租户过滤**：客户只按令牌主体的 `tenant_id` 查询，SQL 里带条件；别的租户的对账单与不存在的不可区分（404）。管理端不按租户过滤。
- **客户侧禁止出现的内容**：成本、汇率、加价、毛利、价格版本与规则、内部 id（含 `tenant_id`、`generated_by`、`ledger_high_sequence`、`tax_policy_version_id`、`project_public_id`）、生成者邮箱、税额栏、SST 注册号。PDF 只印 §2「版面」列出的字段；快照键集合固定，多键即拒绝。客户与管理员下载的是**同一份 PDF**（管理端不额外印内部字段，便于核对「客户看到的就是这份」）。
- **个人 / 商业数据**：PDF 含公司名与项目名（快照冻结值）。不落盘、不缓存（`Cache-Control: no-store`）；只经带令牌的接口发给本租户或管理员。
- **日志**：下载路径只记三种 ERROR（快照不合规 / 字体不可用 / 渲染失败），只带对账单 `public_id` 与（第三种）异常类型名；不带公司名、项目名、金额、快照内容与异常原文。成功下载不记日志（与收据一致；访问日志由现有中间件记路径）。
- **文件名**：`Content-Disposition` 只用对账单号（`ST-YYYYMM-` + 8 位 Crockford base32，已校验），不拼客户输入，不需要 RFC 5987 编码。
- **密钥**：不新增、不读取（ADR-0004 不涉及）。
- **prompt / response**：不涉及。
- **字体许可**：Noto Sans SC 为 SIL OFL 1.1，允许嵌入 PDF 与随软件再分发，要求随附许可证文本（`app/data/NotoSansSC-OFL.txt`，随 wheel 与镜像分发）、不得单独出售字体本身；不修改字体文件（不改名、不改字形），所以不触发 OFL 的「保留字体名」限制。
- **数据保留与删除**：不新增数据；PDF 不存储，删除影响为零。对账单行本身的保留随 #565 v6 §6。

## 7. 测试证据计划

`render_statement_pdf` 的测试是纯单元测试（不需要数据库，Worker 沙箱能跑）；接口测试用现有的 SQLite / MySQL 参数化夹具，**断言不写方言相关的 SQL 字面量**（仓库 `CLAUDE.md`「写后端测试的约定」）。破坏快照的用例**用 INSERT 直接写一行坏快照**，不 UPDATE 已有行（MySQL 触发器拒绝改定稿行，两种库都要能跑）。示例 uuid 全零。

| 风险/需求 | 测试层级 | 场景 | 预期结果 | 任务 |
| --- | --- | --- | --- | --- |
| 正常路径 | unit | 一份全 ASCII 的快照（期初、充值、本期用量、调整、两条上期用量、rebill、两组明细各两行） | 以 `%PDF-` 开头；内容流里能找到 `(MONTHLY STATEMENT)`、开具方、对账单号、`(2026-12 (1 Dec 2026 - 31 Dec 2026, MYT))`、公司名、`MYT` 定稿时刻、§2 列出的每个标签、每个金额的显示值、每个明细行的代码 / 项目名、`tax_notice.en`；不含 `/FontFile2`（没有嵌入字体） | AIH-TASK-216 |
| 键集合（INV-7） | unit | 顶层多一个键（如 `estimated_cost`）、少一个键；`tax_notice`、`prior_period`、`usage[]` 项、三种明细项各多一个 / 少一个键；明细列表里放非对象 | 都抛 `StatementSnapshotInvalid` | AIH-TASK-216 |
| 版本 | unit | `schema_version` 为 `2`、`0`、`True`、`"1"`、`1.0` | 都抛 `StatementSnapshotInvalid` | AIH-TASK-216 |
| 字段格式 | unit | 金额 `"1.5"`、`"1.123456789"`、`1.5`（数字）、13 位整数；计数 `-1`、`True`、`"3"`；`period` `2026-13`、`2025-12`、`3000-01`；`usage_period` 不小于 `period`；`currency` `USD`；空公司名；`statement_number` 形状错、含 `I` / `L` / `O` / `U`、`YYYYMM` 与 `period` 不符、末尾带换行；`finalized_at` 带时区或为 `None` | 都抛 `StatementSnapshotInvalid` | AIH-TASK-216 |
| PDF 不含成本类字样 | unit | 正常快照渲染结果 | 不含 `cost`、`markup`、`margin`、`profit`、`SST`、`Tax amount`（大小写不敏感，只在内容流文字里查） | AIH-TASK-216 |
| 金额显示（INV-10） | unit | `0.00500000` → `MYR 0.01`；`-0.00500000` → `MYR -0.01`；`-0.00400000` → `MYR 0.00`（不是 `-0.00`）；`1234567.99500000` → `MYR 1,234,568.00`；`0.00000000` → `MYR 0.00`；合计与行之和不等的例子（§2「金额显示」） | 逐一相等；合计行印快照合计的舍入值，说明 1 在页内 | AIH-TASK-216 |
| 数字格式 | unit | `input_tokens = 1234567` | `(1,234,567)` | AIH-TASK-216 |
| 中文 | unit | 公司名 `示例科技 Sdn Bhd`、项目名 `客服项目` | 内容里有 `/FontFile2`，`/BaseFont` 含 `NotoSansSC`；ToUnicode CMap 里出现这几个字的码点（十六进制，大小写不敏感）；ASCII 的标签仍以 `(…)` 字面量出现 | AIH-TASK-216 |
| 字体里没有的字符 | unit | 公司名 `Café த Sdn Bhd`（先断言 U+0BA4 不在字体 cmap 里）；`Café 中 த` | 前者走 Helvetica、印成 `(Caf… ? Sdn Bhd)`、不嵌入字体；后者嵌入字体、`த` 成 `?` | AIH-TASK-216 |
| 字体缺失 | unit | 用 monkeypatch 让字体路径指向不存在的文件、清缓存：渲染含中文的快照；渲染全 ASCII 的快照 | 前者抛 `StatementFontUnavailable`；后者照常 | AIH-TASK-216 |
| 字体文件钉住 | unit | 源码树里的字体文件 SHA-256；许可证文件存在且含 `SIL OPEN FONT LICENSE Version 1.1` | 等于 `STATEMENT_FONT_SHA256`；许可证在 | AIH-TASK-216 |
| 开具方一致 | unit | `STATEMENT_ISSUER_NAME` 与 `app.services.receipts.RECEIPT_ISSUER_NAME` | 相等（测试文件可 import 两边） | AIH-TASK-216 |
| 分页 | unit | 300 行 `by_project`（名字 `Prj00001`…，其中若干 255 字符）；空明细 | 页数 > 1；每页页脚 `Page n of N`（`N` 为实际页数）；续页有 `(continued)` 页眉与重复的表头；每个项目名都出现；空明细印 `No usage in this period.` | AIH-TASK-216 |
| 可重现（同进程） | unit | 同一输入渲染两次（ASCII 与含中文各一份） | 字节相同 | AIH-TASK-216 |
| 可重现（跨进程） | unit | 两个子进程（`sys.executable`，环境为当前环境的副本加 `PYTHONHASHSEED=1` / `2`、`SOURCE_DATE_EPOCH=0` / `2000000000`）各渲染同一份含中文的快照，输出经标准输出传回 | 字节相同 | AIH-TASK-216 |
| 元数据 | unit | 正常渲染 | `/CreationDate` 为 `finalized_at` 的 UTC，`/Title` 为对账单号（照 `test_receipt_pdf.py:145-156` 的两种编码判断） | AIH-TASK-216 |
| 渲染只读输入 | unit | 不建数据库、不读配置时渲染；渲染前后深拷贝比较快照 | 照常渲染；快照不变；模块不 import `app.models`、`app.core.config`、`sqlalchemy`（检查 `sys.modules` 在单独子进程里只 import 本模块后的结果） | AIH-TASK-216 |
| 打包 | packaging smoke | 装好的发行版里 import `app.services.statement_pdf`；`importlib.resources` 能读到字体文件，且其 cmap 含 `中` | 通过（CI 与 Docker 构建期都跑） | AIH-TASK-216 |
| 客户下载 | integration（API） | 本租户 `FINALIZED` | 200；`application/pdf`；`Content-Disposition: attachment; filename="<number>.pdf"`；`Cache-Control: no-store`；正文等于直接调 `render_statement_pdf` 的结果 | AIH-TASK-217 |
| 租户越权（INV-8） | integration（API） | 租户 A 的令牌下载 B 的对账单；不存在的 uuid；大写 uuid；`not-a-uuid`；A 自己的 `VOID` 行与 `DRAFT` 行（直接 INSERT） | 都是 404 `STATEMENT_NOT_FOUND`，响应体除 `request_id` 外相同 | AIH-TASK-217 |
| 只读状态 | integration（API） | 租户 `DISABLED` 时下载 | 200 | AIH-TASK-217 |
| 鉴权 | integration（API） | 客户接口：无令牌；管理端接口：匿名、客户令牌（路由表枚举测试自动覆盖） | 401 / 403，不写库 | AIH-TASK-217 |
| 管理端下载 | integration（API） | 任一租户的 `FINALIZED`；`VOID`；不存在 | 200 PDF（与客户下载同一份字节）；404；404 | AIH-TASK-217 |
| 改名与新税务政策后字节不变（INV-6） | integration（API） | 下载一次；改租户公司名、改项目名、发布一版新的税务政策；再下载 | 两次字节相同 | AIH-TASK-217 |
| 快照不合规 | integration（API） | INSERT 一行 `FINALIZED`、快照多一个键 | 500 `INTERNAL_ERROR`、固定文案；ERROR 日志恰好一条，`statement_id` 为该行 `public_id`，日志记录里不含公司名 | AIH-TASK-217 |
| 渲染异常 | integration（API） | monkeypatch 让渲染抛一个文案含公司名的 `RuntimeError` | 500；ERROR 日志带 `error_type = "RuntimeError"`，日志（含异常信息）里不含该文案 | AIH-TASK-217 |
| 下载不改行（INV-12） | integration（API） | 下载前后读该行全部列 | 完全相同；没有审计行 | AIH-TASK-217 |
| 并发 | 不适用 | 只读、无锁、无共享可变状态（§4） | — | — |
| 幂等重放 | integration（API） | 同一请求连发两次 | 字节相同（已含在「客户下载」） | AIH-TASK-217 |
| 事务中途失败 | 不适用 | 没有写事务；渲染失败不留任何状态（「快照不合规」「渲染异常」两行覆盖失败后的结果） | — | — |
| Redis/Celery 丢失 | 不适用 | 不经队列 | — | — |
| 门户页面 | unit（前端） | 详情读成功后有 `Download PDF`；点下载调用 `downloadCustomerStatement`（路径、回退文件名）；下载中按钮不可再点；失败显示错误码 / 信息 / `request_id`，再点一次时清除；详情加载中与读失败时没有按钮 | 与 §2 一致；不请求任何 `/api/v1/admin/` 路径 | AIH-TASK-218 |
| 管理端页面 | unit（前端） | `FINALIZED` 详情有按钮、`VOID` 详情没有；下载成功、下载中、失败与清除 | 与 §2 一致；不请求任何 `/api/v1/customer/` 路径 | AIH-TASK-219 |
| 浏览器验收 | acceptance（只读） | 见 §11 AIH-TASK-218、AIH-TASK-219 的步骤 | 列表非空时详情卡片里有可点的 `Download PDF`；列表为空同样通过；不点下载 | AIH-TASK-218、AIH-TASK-219 |

## 8. 迁移与上线

- **数据迁移**：不适用——不加表、不改列、不回填；对账单快照与 0035 不动。
- **锁表与性能**：不锁表。下载是单行主键（唯一索引 `uq_monthly_statements_public_id`）读取；渲染在会话外。含中文时要解析约 10 MB 的字体并子集化，单次渲染的耗时与内存见 §10 假设 5。
- **依赖**：`fonttools` 由 P0 显式写进 `dependencies`（它本来就随 fpdf2 装上，CI 与 Worker 环境不需要新装）；不新增其他依赖。
- **部署顺序**：P0（字体与 package-data）→ AIH-TASK-216（渲染，无入口）→ AIH-TASK-217（两个接口与 `docs/api.md`）→ AIH-TASK-218（门户按钮）→ AIH-TASK-219（管理端按钮）→ AIH-TASK-220（文档收尾）。AIH-TASK-216 合并后 packaging smoke 开始核对字体；AIH-TASK-217 合并后接口可用但页面还没有入口；AIH-TASK-218 / AIH-TASK-219 合并后用户可见。
- **兼容窗口 / 部分部署**：任何一步单独上线都不影响已有接口与页面；前端早于后端不会发生（AIH-TASK-218 依赖 AIH-TASK-217）。
- **回滚或前滚**：只回滚应用镜像；没有数据要回滚。回滚到 AIH-TASK-217 之前的镜像后，下载接口消失、页面按钮（若已上线）点了会得到 404 信封并显示错误。
- **监控与负责人**：不加告警维度（Healthchecks 20/20 已满，2026-10-17 Kelvin 定不增检查）；下载失败只在 ERROR 日志，按 runbook 处置（AIH-TASK-220）；负责人 Kelvin。

## 9. 方案取舍

| 方案 | 优点 | 风险 | 未采用原因 |
| --- | --- | --- | --- |
| 采用：即时渲染；Latin-1 用 Helvetica、其他走 Noto Sans SC 子集；严格校验 v1 键集合；合计取快照值 | 与收据同一结构；全 ASCII 的对账单不读字体、可在内容流里检索文字；中文正确显示；多键即拒绝守住 INV-7 | 两种字体混排，同一列里 Latin 与中文字形略有差别 | — |
| 备选 A：所有快照字符串都用 Noto Sans SC | 字形统一 | 每次下载都要解析约 10 MB 字体并嵌入子集；TrueType 文字在内容流里是字形编号，测试检索不到 ASCII 文字，需要额外的 PDF 解析依赖 | 成本与可测性 |
| 备选 B：fpdf2 的回退字体（`set_fallback_fonts`） | 不用自己按字符串选字体 | 主字体是标准字体时回退是否生效未验证；缺字时的行为由库决定，不确定是否抛错 | 行为不确定；本方案的选择规则显式、可测 |
| 备选 C：存 PDF 文件（spec §93 的卷） | 下载不渲染 | 多一个卷、存储抽象与「文件缺失重渲染」路径；文件与快照可能不一致 | 与收据的 Kelvin 决定一致：即时生成 |
| 备选 D：渲染时复核算术（期初 + 发生额 = 期末），不等就拒绝 | 多一道防线 | 生成时的三条自检已保证这一点；一旦不等，这份对账单永久下载不了，而 JSON 详情照样能看，徒增不一致 | 重复控制，失败语义更差 |
| 备选 E：把显示差额摊进某一行，使舍入后各行之和等于合计 | 版面上「加得平」 | 印出快照里没有的数字，与门户 JSON 和页面不一致；违背 spec §65「不重算」的精神 | 以页内说明代替 |
| 备选 F：明细金额显示 6 位小数（spec §80 允许） | 小额明细不显示为 `0.00` | 与门户对账单页（2 位）不一致，客户对照两边看到不同的数 | 与页面一致优先；列在 §10 未决问题第 2 条由审查者 / Kelvin 定 |
| 备选 G：开具方名称 import `RECEIPT_ISSUER_NAME` | 只有一处常量 | `app/services/receipts.py` import 模型与 SQLAlchemy（`:28-38`），会破坏渲染模块「不 import 模型」的隔离 | 另定常量 + 测试断言相等 |
| 备选 H：快照升 v2，把开具方名称与对账单号写进快照 | 完全自包含、开具方名称也冻结 | 改生成逻辑与快照契约，要处理 v1 / v2 两种快照 | Kelvin 决定 7：不改快照 |

## 10. 未决问题与假设

- **未决问题（请审查者 / Kelvin 重点看）**：
  1. **管理员下载非 `FINALIZED` 行**：本稿统一 404 `STATEMENT_NOT_FOUND`（V1 没有这类行；管理端页面只给 `FINALIZED` 显示按钮）。备选是 409 加新错误码，语义更清楚但多一个码。
  2. **明细金额 2 位还是 6 位**（spec §80）：本稿取 2 位，与门户页一致；小额明细会显示 `MYR 0.00`。
- **需要管理员做的**：P0 已提交（PR #706，AIH-TASK-216 之前合并）；登记 PR 里在 `docs/TODO.md` 规划块「后续计划」加「待登记：收据 PDF 嵌入中文字体（收据中文印成 ? 的问题）」并删去「待登记：Phase 7 对账单 PDF 下载」那一行（Worker 不改规划块）。
- **尚未验证的假设**：
  1. **Worker 能否提交二进制文件**：未验证（控制面的改动集合与回读对二进制的处理没有查到明确说明）；Worker 也不联网、取不到字体。所以字体放在 P0 由管理员提交，AIH-TASK-216..220 都不新增二进制文件。
  2. **fpdf2 对静态 TrueType CJK 字体的 `add_font` 与子集化可用**：已在 P0 用 fpdf2 2.9.0 实测可用（见 §2「P0」）。**仍未验证**的是 `FPDF.table()` 的单元格可按 `FontFace(family=…)` 换字体、`repeat_headings` 可用；AIH-TASK-216 的测试会直接暴露。
  3. **含字体子集的输出可字节级重现**：fpdf2 子集名前缀固定、字形顺序确定、保存子集字体时不重算 `head.modified`（或不受当前时间与 `SOURCE_DATE_EPOCH` 影响）。P0 的最小探针已在三个不同 `PYTHONHASHSEED` 的进程里得到相同字节（fpdf2 2.9.0）；完整版面仍由 §7「可重现（跨进程）」验证。
  4. `fonttools` 的版本下界 4.34 与 fpdf2 的 install_requires 一致：P0 时已核对。
  5. **性能**：一份含中文、几百行明细的对账单，单次渲染在 2 秒内、内存增量在 100 MB 内（字体解析是懒加载，子集只含用到的字形）。没有量过；AIH-TASK-216 的 PR 里记一次本地实测。
  6. **浏览器验收在 2027-01-02 之前是空跑**：生产在首个账期生成前没有对账单，AIH-TASK-218、AIH-TASK-219 的新步骤只能走「列表为空即通过」的分支；真正点开详情、看到按钮要等生成之后的部署。
- **已知限制**：
  1. 开具方名称是代码常量、不在快照里（Kelvin 决定 7）：将来改这个常量，历史对账单重渲染时会印新名称。所以改它与改字体文件一样要过设计闸门；金额与客户数据不受影响。
  2. 「可重现」限于同一代码与依赖版本：升级 fpdf2 会改变 Producer 串等字节；版面模板改动也会改变历史 PDF 的外观（不改数字）。与收据相同。
  3. 快照不合规的对账单永久下载不了（行不可改），与 #565 v6 的「永久停出」同一类处理；正常生成不会产生这种行。
- **如果假设错误，具体后果**：1 → 若 Worker 其实能提交二进制，也不改本设计（字体仍需联网取得）；2 → AIH-TASK-216 停下在 PR 里说明，设计升版本改用可行的 API（例如手写表格与 `will_page_break`）；3 → AIH-TASK-216 的跨进程测试失败，实现**停下并在 PR 里说明**，不得放宽为「去掉某些字节再比较」——那等于放弃 spec §106 的可重现，设计升版本处理；4 → P0 时按实际下界写；5 → 记录实测，超出时另开任务（例如进程内缓存解析后的字体对象），不阻塞本批；6 → 部署后的验收结论只证明「页面与接口可达」，生成后由管理员手工下载一次核对（AIH-TASK-220 写进 runbook）。

**本稿没有未解决的财务、安全、租户隔离或不可逆迁移问题**；上面 2 条未决问题都是展示口径，任何选择都不影响正确性与隔离。

## 11. 审查与版本绑定

审查方要回答的五个问题常驻在 [scripts/review_checklist.md](../../scripts/review_checklist.md) 的「设计审查」一节，此处不重复。

**版本绑定规则**（规则的出处是 [WORKFLOW §3](../../docs/WORKFLOW.md)；这里保留一份是因为审查材料里只有本 Issue 正文，两处改一起改）：

- 批准必须写成 `APPROVED: design v<N>`，`<N>` 取本 Issue 顶部的「设计版本」
- **设计版本一变，之前的 APPROVE 自动作废**，必须重新过闸门
- 什么算「实质修改」：改了契约、表结构、事务边界、状态机、失败语义、不变量控制。改错别字不算
- 实质修改时：把顶部版本 +1，在下方追加一条变更说明，状态退回 `READY_FOR_REVIEW`

### 实现拆分

前置：**P0（管理员 PR，不走 OpenClaw）**——字体文件、许可证、package-data、`fonttools` 依赖、`app/data/__init__.py` 文档串（§2「P0」）。AIH-TASK-216 依赖 P0 已合并。

| 任务 | 层 | 标题 | 依赖 | 预计行数 |
| --- | --- | --- | --- | --- |
| `AIH-TASK-216` | 业务规则（渲染） | 月度对账单 PDF 渲染服务：快照校验、中文字体子集、分页版面与可重现 | P0 | ~950 |
| `AIH-TASK-217` | 接口 | 月度对账单 PDF 的客户与管理端下载接口 | AIH-TASK-216 | ~600 |
| `AIH-TASK-218` | 前端 | 门户对账单详情加 PDF 下载按钮 | AIH-TASK-217 | ~450 |
| `AIH-TASK-219` | 前端 | 管理端对账单详情加 PDF 下载按钮 | AIH-TASK-218 | ~450 |
| `AIH-TASK-220` | 文档 | 月度对账单 PDF 下载的运维说明与 TODO 勾选 | AIH-TASK-219 | ~60 |

**AIH-TASK-216**

- 可改路径：`app/services/statement_pdf.py`、`tests/backend/test_statement_pdf.py`、`scripts/packaging_smoke.py`
- 验收要点：
  1. `render_statement_pdf(snapshot, *, statement_number, finalized_at) -> bytes` 与异常 `StatementSnapshotInvalid`、`StatementFontUnavailable`，校验规则与 §2「校验」逐条一致（键集合恰好相等、未知版本拒绝、不修改输入）。
  2. 字体选择与 `?` 替换按 §2「文本与字体」；字体经 `importlib.resources` 读 `app/data/NotoSansSC-Regular.ttf`；`STATEMENT_FONT_SHA256` 等于 `app/data/__init__.py` 文档串里记的值。
  3. 版面、标签与说明文字与 §2「版面」逐字一致；金额显示按 §2「金额显示」；`STATEMENT_ISSUER_NAME` 与收据常量相等。
  4. 可重现：同进程两次、跨进程（不同 `PYTHONHASHSEED` 与 `SOURCE_DATE_EPOCH`）字节相同；失败时停下说明，不放宽比较。
  5. 模块不 import 会话、模型、配置与 `app/services/statements.py`、`app/services/receipts.py`。
  6. `scripts/packaging_smoke.py` 加 `import app.services.statement_pdf` 与字体可读、cmap 含 `中` 的检查。
  7. §7 标 AIH-TASK-216 的各行都有用例；PR 里记一次本地实测的渲染耗时（§10 假设 5）。
  8. 不改收据相关文件；不改 `pyproject.toml`（P0 已改）。

**AIH-TASK-217**

- 可改路径：`app/api/statement_download.py`、`app/api/customer.py`、`app/api/admin_statements.py`、`tests/backend/test_customer_statements_api.py`、`tests/backend/test_admin_statements_api.py`、`docs/api.md`
- 验收要点：
  1. `statement_pdf_response(factory, statement_id, *, tenant_id)` 按 §2「下载」：形状判断、只读会话只取四列且只认 `FINALIZED`、会话关闭后渲染、三种失败的日志与 500、响应头。
  2. 客户 `GET /api/v1/customer/statements/{statement_id}/download`：第一条语句 `require_customer`，`tenant_id` 取令牌主体。
  3. 管理端 `GET /api/v1/admin/statements/{statement_id}/download`：第一条语句 `require_admin`，`tenant_id=None`。
  4. 404 一律 `STATEMENT_NOT_FOUND`（形状不对、不存在、别的租户、非 `FINALIZED`）。
  5. 日志只带 `statement_id`（与第三种的 `error_type`），不带公司名、金额、快照与异常原文。
  6. `docs/api.md` 的「月度对账单（管理端）」「月度对账单（客户）」两节各加下载接口的行与小节（状态码、头、文件名、404 语义）。
  7. §7 标 AIH-TASK-217 的各行都有用例，SQLite 与 MySQL 两个分片都跑；不写方言相关的 SQL 字面量断言。

**AIH-TASK-218**

- 可改路径：`frontend/src/api/customerStatements.ts`、`frontend/src/api/customerStatements.test.ts`、`frontend/src/features/portal/StatementsPage.tsx`、`frontend/src/features/portal/StatementsPage.test.tsx`、`frontend/src/i18n/locales/en.json`、`scripts/acceptance/admin_customers.mjs`
- 验收要点：
  1. `downloadCustomerStatement` 经 `downloadFile`，路径与回退文件名按 §2「门户页面」；不改 `frontend/src/api/download.ts`、`client.ts`、`MoneyText.tsx`。
  2. 详情卡片的按钮、下载中、失败与清除按 §2；列表、翻页与点行行为不变；不请求 `/api/v1/admin/`。
  3. 文案走 i18n，满足 `no-hardcoded-jsx-text` 与 `keys.test.ts`。
  4. 新步骤 `check_portal_statement_pdf`（排在 `login_customer` 之后）：经门户导航进 `/portal/statements`；后端值取页面自己发出的 `GET /api/v1/customer/statements` 响应（被动观察）；`items` 非空时点第一行（只触发详情的 GET），等详情卡片落定，断言卡片里有可点的 `Download PDF` 按钮，**不点它**；`items` 为空时显示空状态即通过。不翻页、不提交表单。
  5. 浏览器验收：`acceptance.browser`，`writes: false`，步骤 `login_customer`、`check_portal_statement_pdf`，`timeout_seconds: 300`；沿用验收脚本已有的约束；已有步骤行为不变；脚本只执行 stdin 里 steps 列出的步骤；文件头步骤说明同步补上。

**AIH-TASK-219**

- 可改路径：`frontend/src/api/adminStatements.ts`、`frontend/src/api/adminStatements.test.ts`、`frontend/src/features/statements/StatementsPage.tsx`、`frontend/src/features/statements/StatementsPage.test.tsx`、`frontend/src/i18n/locales/en.json`、`scripts/acceptance/admin_customers.mjs`
- 验收要点：
  1. `downloadAdminStatement` 经 `downloadFile`，路径与回退文件名按 §2「管理端页面」。
  2. 只有 `FINALIZED` 的详情显示按钮；下载中、失败与清除按 §2；补生成、筛选、列表不变；不请求 `/api/v1/customer/`。
  3. 文案走 i18n，满足 `no-hardcoded-jsx-text` 与 `keys.test.ts`。
  4. 新步骤 `check_admin_statement_pdf`（排在 `login` 之后）：经顶栏进 `/statements`；被动观察 `GET /api/v1/admin/statements`；`items` 非空时点第一行，等详情落定，`status` 为 `FINALIZED` 时断言有可点的 `Download PDF`、不点；为空即通过。不点补生成、不动筛选、不翻页。
  5. 浏览器验收：步骤 `login`、`check_admin_statement_pdf`，其余同 AIH-TASK-218。

**AIH-TASK-220**

- 可改路径：`docs/runbook.md`、`docs/TODO.md`
- 验收要点：
  1. `docs/runbook.md`「月度对账单」一节加「PDF 下载失败」处置：三种 ERROR 日志各自的含义与处置（快照不合规按 `public_id` 查行、不改不删；字体不可用查镜像里的字体文件与 packaging smoke；渲染失败本地复现）。
  2. 同一节加「首个账期生成后，管理员在管理端下载一份对账单核对 PDF 内容（含中文公司名时的显示）」一条（§10 假设 6）。
  3. `docs/TODO.md` Phase 7 的「PDF 下载」勾选；不改 planning-v1 块。

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
| v2 | 2026-11-14 | §2「文本与字体」加第 3 步：选定 Noto Sans SC 的字符串里不在字体 cmap 里的字符（含 Latin-1 控制字符）也换成 `?` | v1 批准后的登记前预审（AIH-TASK-216 一条 UNDECIDED：这类字符原先会印成缺字框） |
| v3 | 2026-11-14 | §2「文本与字体」第 3 步：换行符 `
` 不替换（v2 的写法会把税务说明里的换行换成 `?`，与第 7 节 `Notes` 保留换行矛盾）；§2「校验」：`usage_period` 的年份范围写明同 `period` | v2 批准后的最终预审（AIH-TASK-216 一条 DESIGN_DEFECT、一条 UNDECIDED） |

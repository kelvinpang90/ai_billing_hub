# AIH-TASK-196..206 设计：支付第四批（收据：编号、快照、PDF 下载、付款邮件与补发）（已批准 v1）

> **来源**：设计闸门 Issue #589。本文件是 `APPROVED: design v1` 那一版正文的**逐字副本**（送审时状态行为 `READY_FOR_REVIEW`，批准后 Issue 上只把状态行改成了 `APPROVED: design v1`）。审查：Codex 额度不可用期间由 Claude Code 审查（审查者沿用 Kelvin 为支付第二、三批选定的 Claude）。送审前按登记前预审（`worker.design_precheck`）三轮修订；v1 审查无阻断项，批准。审查的测试建议项（`gateway_reference` 的回退分支）补进了 AIH-TASK-197 的契约。
> 放进仓库，是因为 OpenClaw Worker 在沙箱里不联网、读不到 GitHub Issue。
> **实现以本文件为准**；与 Issue 不一致时，以 Issue #589 上被批准的 v1 为准。设计要改，就回到 Issue 升版本、重新过闸门，不要直接改本文件。
> 本设计建立在支付第一批（设计闸门 #580 已批准 v3）、第二批（#585 已批准 v3）与第三批（#587 已批准 v3）之上。产品决定（即时生成 PDF、按年编号、付款邮件不带附件、上线前的 PAID 补发收据不发邮件）由 Kelvin 2026-11-07 拍板，见 §1。

## 0. 准入判定

| 任务涉及 | 要填的范围 |
| --- | --- |
| 钱包 / 账本 / 定价 / 汇率 / 支付 / 幂等 / 状态机 | **全部章节** |
| 用量摄取 / 认证与会话（集成侧与平台侧）/ Webhook | §1–§7（**§6 不可省** —— 密钥存储与轮换、鉴权、租户过滤、日志脱敏都在那一节） |
| 前端 / 文档 / CI / 脚本 | **不走本闸门**，直接开 PR |
| 既不碰钱、也不碰用量摄取 / 认证与会话 / Webhook 的后端改动 | **不走本闸门**，直接开 PR |

本任务档位：`全部`。理由：收据是支付的不可变财务快照，开具发生在入账的同一事务里（改 A 的 `confirm_payment` 与 B 的人工入账），收据编号的分配要并发安全、单调、不复用（spec §45），并新增财务表与客户可见的财务文件。

---

状态：`READY_FOR_REVIEW`
负责人：Claude
**设计版本**：`v1`
对应需求：spec §43（「Generate Receipt」一步）、§45、§45.1（`REQ-TAX-001`）、§56、§64、§71、§93、§106、§112.1、§129「Payment Receipt」「Receipt download」「Receipt generated」、Invariant 3、6、7、8、10、13、14；ADR-0008 §5；ADR-0014；建立在设计闸门 #580（支付第一批，下称「A」）、#585（第二批，「B」）、#587（第三批，「C」）三份已批准的 v3 之上
目标 PR：待开（拆成 `AIH-TASK-196`..`206`，见 §11）

## 1. 目标与边界

### 要解决的问题

A、B、C 交付了从建单、回调、确认入账、补对账、人工复核到门户充值与支付历史的完整链路，但付款成功之后**没有收据**：

1. spec §43 的流程在入账之后是「Generate Receipt」，§45 规定收据的编号、字段与「客户可在门户下载」，§64 要求管理员能查看与下载，§106 要求服务端生成 PDF、可按快照重现。今天没有收据表、编号、快照与文件。
2. A 的设计写明「支付成功的单独通知随收据（D）再定」：客户付款后只有余额变化与（停机时的）复机通知，没有「已收到付款」的邮件。
3. B 的管理端支付页、C 的门户支付历史都明确「不含收据列，属于 D」。

### Kelvin 2026-11-07 的决定

| 问题 | 决定 |
| --- | --- |
| 收据文件形式 | **按快照即时生成 PDF**：收据数据在开具时冻结进数据库；每次下载由服务端从快照现生成 PDF，不存文件、不加 Docker 卷。新增一个纯 Python 的 PDF 依赖；收据只用英文 |
| 编号规则 | **按年重置**：`ACV-AIR-<马来西亚年份>-<6 位序号>`，每个自然年从 `000001` 开始（与 spec §45 示例一致）；编号在开具的同一事务里分配，回滚不留空号 |
| 付款成功邮件 | **发邮件，不带附件**：发到租户联系邮箱，内容为收据号、金额、付款时刻与门户里这笔支付的页面链接（在那里下载收据）；走现有的邮件 outbox，投递失败不影响收据 |
| 本批上线前已 `PAID` 的支付 | **补发收据，不发邮件**：上线后由后台扫描给没有收据的 `PAID` 支付补开，编号照常分配；当时没有记下的付款方式显示为 `Stripe` |

我定的（未单独问）：

- 收据只给 `PAID` 的支付开，每笔至多一张；V1 不做作废与更正（没有退款与改账会作用在支付上：B 定的争议与外部退款只告警、由 Kelvin 人工调账）。
- 开具与入账同一事务；开具前的检查不过时**不阻断入账**（钱先入账，收据由补发扫描再试，并告警），见 §2「开具」。
- 收据上的客户只印公司名（`company_name`），不印联系人与邮箱。
- 管理端不加独立的收据页：在 B 的支付列表加收据号一列、详情加收据号与下载按钮。门户同样在 C 的支付历史与付款结果页加收据号与下载按钮；客户的收据列表接口照 spec §API 实现。

### 可观察的完成标准

1. 每笔经 A 的自动确认或 B 的人工按实付入账而成为 `PAID` 的支付，在同一事务里得到恰好一张收据：编号形如 `ACV-AIR-2026-000001`，同一年份内连续递增、不重复、不复用。
2. 收据快照含 spec §45 列出的全部字段，金额等于这笔支付的 `TOPUP` 账本行金额；快照引用这笔支付建单时的税务政策版本，并逐字冻结它的 `document_notice`（ADR-0008 §5）；收据行在数据库层拒绝 UPDATE 与 DELETE。
3. 客户在门户的支付历史与付款结果页看到收据号并能下载 PDF；管理员在支付列表与详情看到收据号并能下载。同一张收据任何时候下载，PDF 的内容相同；改公司名、改税务政策都不改变已开具的收据。
4. 收据开具的同一事务写一行付款邮件 outbox 行，租户联系邮箱最终收到一封英文邮件（收据号、金额、付款时刻、门户链接）。
5. 本批上线前已是 `PAID` 的支付，在上线后第一次补发扫描里得到收据，不发邮件。
6. 任何 `PAID` 支付在 `paid_at` 之后 1 小时仍没有收据，`wallet_negative_balance` 维度报 P2。

### 明确不做什么

- **不做作废、更正与红冲收据**：没有任何路径改变已开具的收据；`spec §45` 的「作废的编号仍可审计」在 V1 没有作废路径，编号因为收据行不可删而永不复用。
- **不做 LHDN e-Invoice**（spec §45「V1 receipt is NOT LHDN e-Invoice」）；表与快照不为它预留字段。
- **不存 PDF 文件**、不做文件存储抽象（spec §93 的「may be stored」，Kelvin 定即时生成）；因而也没有 §64 的「文件缺失时重新渲染」接口——每次下载都是从快照渲染。
- **不做中文收据**，不嵌入字体：PDF 用 Helvetica 标准字体，Latin-1 之外的字符印成 `?`（见 §10 已知限制）。
- **不在邮件里附 PDF**；不给门户写「付款成功」通知（门户已有余额与复机通知）；补发的收据不发邮件。
- **不改** A 与 B 的状态机、可信确认的判定、入账金额、事件处理与补对账的调度；不改 C 的建单校验与支付设置。
- **不改** 对账单（INV-12）：收据与对账单互不引用。
- **不做客户侧的「收据」独立页面**；客户收据列表接口照 spec 提供，门户页面不调用它。

### 现有行为与证据

- A 的 `confirm_payment`（`app/services/payments.py`，AIH-TASK-174）：判定通过时在锁「支付 → 钱包 → 租户」的事务里写 `PAID` 并 `post_transaction(TOPUP, reference=(PAYMENT, public_id))`；B（AIH-TASK-180）在第 3 步加了「`resolution` 不为空 → 只更新 `last_reconciled_at`」。
- B 的 `resolve_review`（`app/services/payment_review.py`，AIH-TASK-182）的 CREDIT：事务外查 Stripe，同样顺序锁三行后写 `PAID`、`resolution = CREDITED`，入账额为 Stripe 实收。
- A 的 `get_payment_status` 已经 `expand[]=payment_intent.latest_charge.balance_transaction`；`latest_charge` 里带 `payment_method_details.type`（`card`、`fpx` 等）；B 已从同一个对象取 `amount_refunded` 与 `disputed`，不多发请求。
- 税务政策：`payments.tax_policy_version_id`（A，建单时解析）；`app/repositories/tax_policy.py` 的 `parse_treatment` 校验 `treatment_json` 并给出 `document_notice{zh, en}`；对账单（AIH-TASK-156）用同一字段冻结说明文字。
- 邮件传输：ADR-0014（2026-10-15）定现阶段继续走 SMTP，并把「客户反馈收不到收据、对账单或余额提醒」列为重新评估的触发条件；`docs/TODO.md` Phase 4 那一条「开工前重新评估 Email 传输」的评估结论就是 ADR-0014（`docs/HANDOFF.md` 第 8 项「已定」）。本批沿用，不另行评估。
- 邮件：`app/tasks/outbox.py` 的 `_RENDERERS` 与 `handled_event_types()`；`recover` 只重投有处理器的类型；`_settle` 成功或死信都清空 `payload_json`；`app/core/email_templates.py` 的模板标题固定、不含 payload 值（设计闸门 #559 v2）。
- 下载：`frontend/src/api/download.ts` 的 `downloadFile` 带令牌取文件、按 `Content-Disposition` 存盘，失败时解析错误信封；后端 `app/core/csv_export.py` 的附件响应写法（`Content-Disposition: attachment; filename="…"`、`Cache-Control: no-store`）。
- 不可变表的数据库兜底：迁移 0035 的两个 MySQL 触发器（`trg_monthly_statements_before_update` / `_before_delete`）与 downgrade「只删空表」的写法。
- 告警：A、B 的支付判据都并进 `wallet_negative_balance` 维度（`app/services/alerts.py`）。
- 代码里没有任何 `receipt` 表、模型、服务与接口（`grep -rni receipt app` 只命中用量摄取的「首次收到」语义）。

## 2. 设计概要

### 数据流

```text
A confirm_payment（判定通过）／ B resolve_review CREDIT
  └─ 同一事务：PAID + post_transaction(TOPUP) + 审计
       └─ 新：issue_receipt(session, payment, payment_method, now, send_email=True)
             ① 已有收据 → 返回它（幂等）
             ② 检查（只读）：TOPUP 账本行、税务政策版本、当年编号系列 —— 任一不过 → 记 ERROR、返回 None，不写任何行
             ③ 写：系列 last_sequence + 1 → INSERT payment_receipts（快照）→ INSERT 付款邮件 outbox 行
  └─ 提交（入账、收据、邮件行原子；② 不过时只有入账提交）

Beat 每 300 秒 receipts.issue_missing：PAID 且没有收据的支付（按 paid_at 升序，每轮 ≤ 500、预算 240 秒）
  └─ 每笔一个事务：锁支付行 → 重查 → issue_receipt(send_email=False)

recover ──► 付款邮件：渲染 → Adapter → SMTP（现有 outbox 路径）

客户 GET /customer/receipts、/customer/receipts/{id}/download；C 的支付列表与详情加收据字段
管理员 GET /admin/receipts/{id}/download；B 的支付列表与详情加收据字段
下载 ──► 读快照 ──► render_receipt_pdf(snapshot) ──► application/pdf 附件
```

### 数据库（迁移 0039；`down_revision` 为 C 的 `0038_payment_settings`）

**`receipt_number_series`**（编号计数器，每个年份一行）：

| 列 | 类型 | 说明 |
| --- | --- | --- |
| `series_year` | SMALLINT PK（不自增：模型 `autoincrement=False`，MySQL 上没有 `AUTO_INCREMENT`，形状检查核对这一点），CHECK `BETWEEN 2026 AND 2099`（`ck_receipt_number_series_year`） | 马来西亚日历年 |
| `last_sequence` | INT NOT NULL DEFAULT 0，CHECK `BETWEEN 0 AND 999999`（`ck_receipt_number_series_sequence`） | 这一年已分配的最大序号 |

迁移插入 2026 至 2099 每年一行（74 行，`last_sequence = 0`）。只有 `issue_receipt` 写它。不预建行的方案见 §9。2099 年之后要用新的迁移同时放宽两张表上的年份 CHECK 并插入新的系列行（过设计闸门）。

**`payment_receipts`**：

| 列 | 类型 | 说明 |
| --- | --- | --- |
| `id` | BIGINT PK | |
| `public_id` | CHAR(36) NOT NULL，唯一 `uq_payment_receipts_public_id` | `uuid4`，对外 id |
| `payment_id` | BIGINT NOT NULL，唯一 `uq_payment_receipts_payment_id`，FK `payments.id` RESTRICT（`fk_payment_receipts_payment_id`） | 每笔支付至多一张 |
| `tenant_id` | BIGINT NOT NULL，FK `tenants.id` RESTRICT（`fk_payment_receipts_tenant_id`） | 等于支付的 `tenant_id`（由 `issue_receipt` 写入，测试核对） |
| `receipt_number` | VARCHAR(32) NOT NULL，唯一 `uq_payment_receipts_receipt_number` | `ACV-AIR-YYYY-NNNNNN` |
| `series_year` | SMALLINT NOT NULL，CHECK `BETWEEN 2026 AND 2099`（`ck_payment_receipts_series_year`） | |
| `sequence` | INT NOT NULL，CHECK `BETWEEN 1 AND 999999`（`ck_payment_receipts_sequence`） | |
| `amount` | DECIMAL(20,8) NOT NULL，CHECK `amount > 0 AND amount = ROUND(amount, 2)`（`ck_payment_receipts_amount`） | 这笔支付的 `TOPUP` 账本行金额 |
| `currency` | CHAR(3) NOT NULL，CHECK `= 'MYR'`（`ck_payment_receipts_currency`） | |
| `tax_policy_version_id` | BIGINT NOT NULL，FK `tax_policy_versions.id` RESTRICT（`fk_payment_receipts_tax_policy_version_id`） | 等于支付的 `tax_policy_version_id` |
| `snapshot_json` | JSON NOT NULL | 见「快照」 |
| `issued_at` | DATETIME NOT NULL | 开具时刻 |

索引：唯一 `uq_payment_receipts_series_sequence (series_year, sequence)`；`ix_payment_receipts_tenant_issued (tenant_id, issued_at, id)`（客户列表，也供 `tenant_id` 外键使用）；`ix_payment_receipts_tax_policy_version_id (tax_policy_version_id)`（外键列的显式索引，与 0035 同一理由）。`payment_id` 的外键用它的唯一索引。

MySQL 触发器 `trg_payment_receipts_before_update`（BEFORE UPDATE）与 `trg_payment_receipts_before_delete`（BEFORE DELETE）：无条件 `SIGNAL SQLSTATE '45000'`（消息 `payment_receipts rows are immutable`）。SQLite 不建触发器（与 0035 同一惯例）。

迁移的检查、预检（`log_bin_trust_function_creators`）、离线输出、失败窗口与降级照 0035 的写法（见 §8）。MySQL 的 `CREATE TABLE` 与插入系列行不在同一个事务里，所以系列行的检查分三种：0 行 → 插入 74 行（补完在建表与插入之间中断的升级）；恰好 74 行、年份恰好 2026–2099、`last_sequence` 都在 0–999999 → 不动；其他 → 抛错、不发 DDL、不写行。

### 编号

- `series_year` = `issued_at` 换算成马来西亚时间（UTC+8）后的年份；`receipt_number` = `f"ACV-AIR-{series_year:04d}-{sequence:06d}"`。
- 分配在开具的事务里：`SELECT … FOR UPDATE` 锁这一年的系列行，`sequence = last_sequence + 1`，`UPDATE` 回写，再插入收据。事务回滚时系列行一起回滚，不留空号；并发的开具在系列行上排队，所以同一年份内按分配顺序连续、唯一（spec §45）。
- 年份不在 2026–2099（没有系列行）或 `last_sequence` 已是 999999 → 开具检查不过（`RECEIPT_SERIES_UNAVAILABLE`），见「开具」。

### 快照（`snapshot_json`，`schema_version = 1`）

键集合恰好如下，不多不少：

```json
{
  "schema_version": 1,
  "receipt_number": "ACV-AIR-2026-000001",
  "issued_at": "2026-12-03T06:05:09",
  "issuer": {"name": "Acuven Technology Sdn Bhd"},
  "customer": {"company_name": "Example Sdn Bhd"},
  "payment": {
    "payment_id": "<payments.public_id>",
    "paid_at": "2026-12-03T06:05:09",
    "amount": "100.00",
    "currency": "MYR",
    "status": "PAID",
    "gateway": "STRIPE",
    "gateway_reference": "pi_…",
    "payment_method": "fpx"
  },
  "tax_notice": {"zh": "…", "en": "…"}
}
```

| 键 | 来源 |
| --- | --- |
| `issued_at`、`payment.paid_at` | 列 `issued_at`、`payments.paid_at`，不带时区的 UTC，`isoformat(timespec="seconds")` |
| `issuer.name` | 常量 `RECEIPT_ISSUER_NAME: Final = "Acuven Technology Sdn Bhd"`（spec §45 原文；`app/services/receipts.py`） |
| `customer.company_name` | 开具时 `tenants.company_name` 原文 |
| `payment.amount` | `TOPUP` 账本行金额，`quantize(0.01)` 后的定点字符串（账本行本身就是两位小数，见「开具」检查 2） |
| `payment.currency`、`payment.status`、`payment.gateway` | 常量 `MYR`（`TOPUP` 账本只有 MYR）、常量 `PAID`（只给 `PAID` 开具）、`payments.gateway` 原值（`STRIPE`） |
| `payment.gateway_reference` | `gateway_payment_intent_id`，为空时 `gateway_payment_id` |
| `payment.payment_method` | 见「付款方式」；拿不到为 `null` |
| `tax_notice` | `payments.tax_policy_version_id` 那一版的 `document_notice` 原文（`zh`、`en`，逐字照抄） |

不含手续费、`expected_amount`、状态原因、结案原因、内部 id 与任何成本类字段（INV-7）。收据的 `amount` 与 `currency` 另存成列，供列表与 CHECK 使用，与快照里的值相同。

### 付款方式（扩展 A 的适配器，AIH-TASK-197）

`GatewayPaymentStatus` 加字段 `payment_method_type: str | None`，取自 `get_payment_status` 已经展开的 `payment_intent.latest_charge.payment_method_details.type`（A 的请求不变）：是字符串且匹配 `^[a-z0-9_]{1,32}$` 时原样返回，否则（缺失、类型不对、不匹配）为 `None`。A 的状态映射与可信确认、B 的人工核对都不读它。

### 开具：`issue_receipt`（`app/services/receipts.py`，AIH-TASK-197）

`issue_receipt(session, *, payment, payment_method, now, send_email) -> IssuedReceipt | None`：`payment` 是调用方已在本事务里 `FOR UPDATE` 锁住、状态为 `PAID` 的 `Payment` 行（状态不是 `PAID` 抛 `ValueError`，属于程序错误）；`IssuedReceipt` 带 `public_id`、`receipt_number`、`created`（本次新开为真）。

0. **时刻**：`issued_at = now.replace(microsecond=0)`；系列年份、列 `issued_at` 与快照里的 `issued_at` 都用这个值（MySQL `DATETIME` 会对微秒四舍五入，不截掉会让存下的时刻比快照晚一秒，在年末还会跨年）。
1. **幂等**：查 `payment_receipts.payment_id = payment.id`；已有 → 返回它（`created = False`），什么都不写。
2. **检查**（只读，按顺序，第一条不过即记一条 ERROR 日志、返回 `None`，**不写任何行**）：
   1. 账本里有这笔支付的 `TOPUP` 行（`reference_type = PAYMENT`、`reference_id = payment.public_id`），金额 > 0 且等于 `ROUND(金额, 2)`，否则 `RECEIPT_LEDGER_MISSING`；
   2. 支付的 `tax_policy_version_id` 那一行能读出、`parse_treatment` 通过，否则 `RECEIPT_TAX_POLICY_UNREADABLE`；
   3. `issued_at` 的马来西亚年份的系列行存在（`SELECT … FOR UPDATE`）且 `last_sequence < 999999`，否则 `RECEIPT_SERIES_UNAVAILABLE`。
   日志是固定文本加原因码与支付的 `public_id`，不带公司名、邮箱、金额。
3. **写**：系列行 `last_sequence + 1`；插入收据（`tenant_id`、`tax_policy_version_id` 取自支付行，`issued_at` 取第 0 步的值，快照照上表）；`send_email` 为真时插入付款邮件 outbox 行（见「付款邮件」）；`flush`。不提交——由调用方提交。

检查放在任何写入之前，所以检查不过时调用方的事务里只有它自己的写入（入账照常提交），不需要保存点。第 3 步的写入没有业务上会失败的约束（编号由系列行锁保证唯一，`payment_id` 已在第 1 步查过且支付行被锁住）；数据库层面的错误（死锁、连接断开）照原样抛出，`issue_receipt` 与调用方都不捕获，整个事务回滚，由调用方原有的恢复处理：A 的事件处理对这类异常不归类为可重试，事件停在 `PROCESSING`，租约（60 秒）过期后被重新领取（A 的现有行为）；B 的接口 500，管理员重试。

### 对 A 的 `confirm_payment` 与 B 的 `resolve_review` 的修改（AIH-TASK-200）

- `confirm_payment`：判定通过、写 `PAID` 并 `post_transaction` 之后、提交之前，调用 `issue_receipt(session, payment=<锁住的支付行>, payment_method=status.payment_method_type, now=now, send_email=True)`。进入 `REVIEW_REQUIRED` 或其他状态时不调用。
- `resolve_review` 的 CREDIT：第 5 步入账与审计之后、提交之前，同样调用，`payment_method` 取第 2 步那次 Stripe 查询的 `payment_method_type`。CLOSE 不调用。
- 返回 `None` 时两处都不改变自己的结果与提交（入账照常提交；收据由补发扫描再试，告警见下）。返回值不影响事件的处理结果与接口响应。
- 锁顺序变为「支付 → 钱包 → 租户 → 当年系列行」。补发扫描只锁「支付 → 系列行」，没有任何路径先锁系列行再锁前三者，不形成环。

### 补发扫描：`app/tasks/receipts.py`（AIH-TASK-201）

- Beat 条目 `receipts-issue-missing`，每 300 秒（常量 `RECEIPT_SWEEP_SECONDS`），`expires` 为间隔减 1 秒，执行 `app.tasks.receipts.issue_missing`。不看 `payments_enabled`（不调 Stripe；开关关闭时也要能补发）。
- 候选：`payments.status = 'PAID'` 且没有收据（`LEFT JOIN payment_receipts … IS NULL`），按 `paid_at` 升序、`id` 升序，每轮最多 500 笔、预算 240 秒（用完就停）。检查不过的支付只做几次只读查询，所以失败的行不会让一轮耗尽预算；只有同时存在 500 笔以上持续失败的支付才会挡住更新的那些，那时告警早已在报（见 §10 已知限制）。
- 候选查询在它自己的事务里做完并结束（提交或关闭会话），之后才逐笔处理，保证 MySQL 可重复读下每笔的重查读到最新数据。
- 每笔一个事务：`SELECT … FOR UPDATE` 锁支付行，重查仍是 `PAID` 且没有收据（否则跳过，不计数），`now` 在这一笔开始时从注入的时钟读一次，调用 `issue_receipt(send_email=False, payment_method=None, now=now)`，提交。返回 `None` 或抛异常（回滚这一笔、记 ERROR）都记入本轮的失败数，继续下一笔。
- 结束时记一条 INFO：本轮开具数与失败数（只有计数）。
- 补发的收据付款方式为 `null`（PDF 上印 `Stripe`），不发邮件——所以本批上线前的 `PAID` 支付（Kelvin 的决定）与开具检查曾经不过的支付都由它收口，且不会补发邮件。

### 告警（并进 `wallet_negative_balance`，AIH-TASK-201）

| 判据 | 级别 |
| --- | --- |
| 有 `status = 'PAID'`、`paid_at ≤ now − 3600 秒` 且没有收据的支付 | P2 |

摘要：B 定的段落 `; payments: …, disputes or refunds 24h <f>, config <ok|missing>` 在 `<f>` 之后、`config` 之前插入 `, receipts missing 1h <g>`（`<g>` 为上表判据的笔数），逐字为 `; payments: review open <a>, event conflicts 24h <b>, oldest open event <c>s, unmatched or rejected 24h <d>, stale pending <e>, disputes or refunds 24h <f>, receipts missing 1h <g>, config <ok|missing>`；追加条件在 B 的条件上加「或 `<g>` 大于 0」；都不成立时与原来逐字相同。B 的测试里比对整段摘要的期望值相应补上 `, receipts missing 1h 0`。1 小时覆盖 12 轮补发扫描；正常路径下收据与入账同时提交，所以这条只在开具检查持续不过（数据缺陷、系列耗尽）或扫描停摆时触发。

### 付款邮件（AIH-TASK-198 渲染，AIH-TASK-197 写 outbox 行）

- 事件类型常量 `EVENT_RECEIPT_EMAIL: Final = "PAYMENT_RECEIPT_EMAIL_REQUESTED"`，定义在 `app/services/receipts.py`（照 `EVENT_PASSWORD_RESET` 定义在 `app/services/password_reset.py`、由 `app/tasks/outbox.py` 导入的写法）；`aggregate_type = "payment_receipt"`，`aggregate_id` = 收据的 `public_id`；`status = PENDING`、`attempt_count = 0`、`next_retry_at = created_at = now`；不 `delay()`，只靠 `recover`。
- payload 的键恰好这 6 个（`json.dumps(sort_keys=True)`）：`to`（开具时 `tenants.email`）、`company_name`、`receipt_number`、`amount`（两位小数定点字符串，与快照相同）、`paid_at`（`isoformat(timespec="seconds")`，与快照相同）、`payment_id`（支付的 `public_id`）。
- 渲染 `_render_receipt_email`（`app/tasks/outbox.py`，登记进 `_RENDERERS`）：键集合不等、收件人为空或含换行、公司名为空、收据号不匹配 `^ACV-AIR-[0-9]{4}-[0-9]{6}$`、金额读不出或不大于 0、`paid_at` 读不出、`payment_id` 不匹配 `^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$`、`frontend_base_url` 为空 → 记固定文本的 ERROR、返回 `None`（按现有规则算一次失败，重试后死信）。
- 模板 `TEMPLATE_PAYMENT_RECEIPT`（`app/core/email_templates.py`）：标题固定为 `We have received your Acuven Billing payment`；正文：

  ```text
  We have received your payment of RM $amount for the Acuven Billing account of $company_name, made at $paid_at.
  It has been added to your prepaid balance.

  Receipt number: $receipt_number

  You can view this payment and download the receipt in the customer portal:
  $link
  ```

  `$amount` 用现有 `_display_money`（两位小数、千分位）；`$paid_at` 用现有 `_malaysia_time`；`$link` = `{frontend_base_url}/portal/payments/{payment_id}`（C 的付款结果页，要登录、不带查询参数）。
- 投递、重试、死信与 `outbox_backlog` 告警沿用现有邮件路径；`_settle` 照旧清空 payload。`docs/api.md` 里 `outbox_backlog` 一行列出的已处理事件类型、`docs/runbook.md` 里已处理类型的清单与按类型的死信处置各补上这个类型（与 AIH-TASK-152 补通知邮件的写法相同）。死信时 payload 已被清空、不能重投：收据照常可下载。处置：死信行的 `aggregate_id` 是收据的 `public_id`，按它查 `payment_receipts` 得到收据号与 `payment_id`，再查 `payments.public_id`，在管理端支付详情核对；必要时人工告知客户收据号。

### PDF：`render_receipt_pdf(snapshot) -> bytes`（`app/services/receipt_pdf.py`，AIH-TASK-199）

- 依赖：`pyproject.toml` 的 `dependencies` 加 `fpdf2>=2.8`（纯 Python；它带来的 Pillow、fontTools、defusedxml 在 `python:3.12-slim` 上都有 wheel，不需要编译链）；`scripts/packaging_smoke.py` 加 `import app.services.receipt_pdf`，证明装好的那一份带着它。
- 输入是快照字典：先校验，任何一条不合规都抛 `ReceiptSnapshotInvalid`（不抛别的异常；下载接口转成 500，见「客户接口」）：顶层与 `issuer`、`customer`、`payment`、`tax_notice` 各层的键集合都恰好如「快照」所列；`schema_version` 是 `int`（不是 `bool`）且等于 1；`receipt_number` 匹配 `^ACV-AIR-[0-9]{4}-[0-9]{6}$`；`issued_at`、`paid_at` 是 `datetime.fromisoformat` 读得出、不带时区、年份在 2026–2099 之间的字符串；`amount` 匹配 `^[0-9]{1,6}\.[0-9]{2}$` 且大于 0；`currency` 等于 `MYR`、`status` 等于 `PAID`、`gateway` 等于 `STRIPE`；`payment_method` 为 null 或匹配 `^[a-z0-9_]{1,32}$`；其余值都是非空字符串。
- 版面：A4 纵向一页，Helvetica 标准字体（不嵌入字体文件），只用英文：
  - 标题 `PAYMENT RECEIPT`；下一行 `issuer.name`；
  - 标签与值各一行：`Receipt No.`、`Issued`（`issued_at` 的马来西亚时间，`YYYY-MM-DD HH:MM MYT`）、`Received from`（`customer.company_name`，过长时自动换行）、`Payment date`（`paid_at`，同上格式）、`Amount`（`RM 1,000.00`，规则同 `_display_money`）、`Currency`、`Payment method`（`card` → `Card`，`fpx` → `FPX`，其他非空值原样，`null` → `Stripe`）、`Gateway reference`、`Payment ID`、`Status`（`PAID`）；
  - 页脚：`tax_notice.en` 原文。
- 字符：每个印出的字符串先把 Latin-1 编不了的字符换成 `?`（标准字体只支持这个编码；不换会抛异常），再交给 fpdf2。
- 可重现：PDF 元数据的创建时刻设为快照的 `issued_at`（不带时区的 UTC，交给 fpdf2 前附上 `datetime.timezone.utc`），标题设为收据号；不压缩内容流（一页的文件很小，也便于测试检索文字）。同一份快照两次渲染的字节完全相同。
- 只读快照，不读任何表与当前配置（spec §106：改公司名、改定价、改税务政策都不改变重现的收据）。

### 客户接口（扩展 C 的 `app/api/customer_payments.py`，AIH-TASK-202）

都用 `require_customer`（只读，`DISABLED` / `CLOSED` 也能看），租户只来自令牌主体，总是注册。

- C 的支付列表与详情的每一项加两个字段：`receipt_id`（收据 `public_id`，没有为 null）、`receipt_number`（没有为 null）。其他字段与客户侧状态映射不变。
- `GET /api/v1/customer/receipts`：`page`（≥ 1）、`page_size`（1–100，缺省 20），与 C 的支付列表相同的分页信封；本租户的收据，按 `issued_at` 降序、`id` 降序。每项：`id`（收据 `public_id`）、`receipt_number`、`payment_id`、`amount`（两位小数字符串）、`currency`、`paid_at`、`issued_at`。
- `GET /api/v1/customer/receipts/{receipt_id}/download`：200 `application/pdf` 附件，`Content-Disposition: attachment; filename="<receipt_number>.pdf"`、`Cache-Control: no-store`；收据不存在、属于别的租户、或 id 根本不像 uuid → 一律 404 `RECEIPT_NOT_FOUND`（不做格式校验，与 C 的支付详情同一写法）；快照校验不过 → 记 ERROR（带收据 `public_id`）、500，用现有的错误信封与 `INTERNAL_ERROR` 码。

### 管理端接口（扩展 B 的 `app/api/admin_payments.py`，AIH-TASK-203）

- B 的支付列表项在 B 定的字段之后追加 `receipt_number`（没有为 null）；详情在 B 定的字段之后、`events` 之前追加 `receipt_id`、`receipt_number`、`receipt_issued_at`（都可为 null，按这个顺序）。B 的结案接口返回的是同一个详情，所以它的响应也带这三个字段（CREDIT 成功时就是刚开具的收据）；结案的请求、校验与错误码不变。
- `GET /api/v1/admin/receipts/{receipt_id}/download`：显式 `require_admin`；响应与错误同客户的下载接口（不按租户过滤）。

### 门户页面（AIH-TASK-204）

- `frontend/src/api/customerPayments.ts`：类型加 `receipt_id`、`receipt_number`；新增 `downloadCustomerReceipt(receiptId, receiptNumber)`，经 `downloadFile`，取不到文件名时用 `<receipt_number>.pdf`。
- 支付历史页：表格加 `Receipt` 列：有收据时显示收据号与一个下载按钮，没有时显示短横。下载中只有这一行的按钮不可点，其他行照常可点；下载失败在表格上方显示一条页面级的错误（错误码、信息与 `request_id`）：只显示最近一次失败（后到的覆盖先到的，翻页之后才失败的下载照样显示），下一次点任一下载或翻页时清除。
- 付款结果页：`PAID` 且有收据时显示收据号与下载按钮（下载中不可再点；失败时在按钮下方显示错误码、信息与 `request_id`，下一次点下载时清除）；`PAID` 而还没有收据时显示「收据正在准备，稍后可在支付历史下载」；其他状态不显示收据区域。结果页的轮询规则不变（只在 `PENDING` 时刷新）。
- 文案进 `en.json`；有浏览器验收（只读，见 §11 的 204）。

### 管理端页面（AIH-TASK-205）

- `frontend/src/api/adminPayments.ts`：类型加收据字段；新增 `downloadAdminReceipt(receiptId, receiptNumber)`，经 `downloadFile`。
- 支付列表加 `Receipt` 列（收据号，没有为短横）；详情的基本字段加收据号与开具时刻（MYT），有收据时显示下载按钮（下载中不可再点；失败在按钮下方显示错误码、信息与 `request_id`，下一次点下载或详情重新读取（含结案后的刷新）时清除）。
- 有浏览器验收（只读，见 §11 的 205）。

### 事务边界

- 自动确认与人工入账：原事务里多一次开具（检查只读；写入在检查之后），与入账、审计同一次提交（INV-13）。
- 补发：每笔一个事务。
- 下载：只读，一个只读会话，渲染在会话关闭之后做（不持连接渲染）。

### 时间语义

不带时区的 UTC，精确到秒（与 A 一致）。编号的年份、PDF 与邮件上的时刻按马来西亚时间（UTC+8）换算，只用于显示与编号，不存换算后的值。

### 金额与精度

收据金额 = 账本行金额（`Decimal`，两位小数），JSON 里存定点字符串；不经 float。显示规则同 `_display_money`（HALF_UP、千分位）。

## 3. 不变量影响矩阵

| 不变量 | 是否触碰 | 可能怎样被破坏 | 设计控制 | 验证方式 |
| --- | --- | --- | --- | --- |
| INV-1 中心故障不中断 AI | 否 | 收据不在 AI 调用路径上 | — | — |
| INV-2 事件不重复扣费 | 否 | — | — | — |
| INV-3 支付不重复入账 | 是（间接） | 开具失败让入账回滚后重试，或补发与入账并发重复开具 | 检查在写之前、不过不阻断入账；`payment_id` 唯一 + 锁支付行 + 幂等返回；收据不写账本 | §7「开具检查不过」「补发与入账并发」 |
| INV-4 余额只经账本变动 | 否 | 本批不写钱包 | — | — |
| INV-5 历史账本不可变 | 否 | 只读账本行 | — | — |
| INV-6 事件保留版本引用 | 是（税务） | 收据渲染时读当前政策，改措辞后历史收据跟着变 | 列 `tax_policy_version_id` NOT NULL FK RESTRICT；说明原文冻结进快照；PDF 只读快照 | §7「快照与税务说明」「渲染只读快照」 |
| INV-7 客户不可见成本毛利 | 是 | 收据或客户接口带手续费 | 快照键集合固定、不含手续费；客户接口字段白名单 | §7「快照键集合」「客户接口字段」 |
| INV-8 租户不可互访 | 是 | 客户下载别人的收据 | 租户来自令牌主体；别的租户的收据 404，与不存在不可区分 | §7「跨租户」 |
| INV-9 对话内容不入库 | 否 | — | — | — |
| INV-10 金额用 Decimal | 是 | 浮点格式化金额 | Decimal、定点字符串、HALF_UP 显示 | §7「金额显示」 |
| INV-11 event_id 至多一次财务效果 | 否 | 收据没有财务效果 | — | — |
| INV-12 定稿对账单不可变 | 否 | — | — | — |
| INV-13 状态与事件原子提交 | 是 | 入账提交而邮件行没写，或收据写了一半 | 收据、系列行、邮件行与入账同一事务 | §7「同事务」 |
| INV-14 队列丢失不毁持久工作 | 是 | Celery 丢任务导致补发或邮件不发 | 补发由 Beat 扫库；邮件靠 outbox `recover`；缺收据有 P2 | §7「补发」「告警」 |

## 4. 状态与并发

### 状态

收据没有状态机：开具即终态，之后不可改、不可删（触发器兜底）。支付状态机不变。

### 并发

- **两笔支付同时入账**：都在当年系列行上排队（`FOR UPDATE`），后到者读到先到者提交后的 `last_sequence`，编号连续、不重复。
- **补发与入账**：入账事务里支付行被锁、尚未提交为 `PAID`，补发的候选查询看不到它；补发在锁住支付行后重查「没有收据」，入账已提交的则跳过。
- **两轮补发重叠**（一轮超时、下一轮已开始）：第二个在支付行锁上等待，拿到后重查已有收据 → 跳过。
- **年份交界**：`issued_at` 在 12 月 31 日 23:59:59 MYT 与 1 月 1 日 00:00:00 MYT 的两笔各锁各的系列行，互不阻塞。
- **改公司名与开具**：开具读的是开具时的公司名；之后改名不影响已开具的收据。

## 5. 失败与恢复矩阵

| 失败点 | 对外结果 | 数据最终状态 | 是否可重试 | 恢复来源 | 告警 |
| --- | --- | --- | --- | --- | --- |
| 开具检查不过：系列缺失或耗尽 | 入账照常；客户暂时看不到收据 | 支付 `PAID`、账本行在、没有收据、没有邮件行 | 是 | 补发扫描每 5 分钟重试；新的迁移补系列行后自动收口 | 1 小时后 P2，收口即消失 |
| 开具检查不过：账本行缺失或税务政策读不出 | 同上 | 同上，**永久**（见 §10 已知限制） | 否 | 按数据事故处理（runbook）；不能靠改数据恢复 | P2 一直报 |
| 开具时数据库错误（死锁、断连） | 与入账一起失败：A 的事件停在 `PROCESSING`；B 的接口 500 | 支付、账本、收据都不变 | 是 | A 的租约过期后重领；管理员重试 | A / B 原有 |
| 补发扫描停摆（Beat 停了） | 补发的收据不出现 | 不变 | 是 | Beat 恢复后补齐 | P2 |
| 付款邮件渲染或投递失败 | 客户没收到邮件；收据照常可下载 | 邮件行重试后 `FAILED` | 是（至死信） | 现有 outbox 重试 | `outbox_backlog` |
| 下载时快照校验不过 | 500 | 不变 | 否 | 人工排查（runbook） | ERROR 日志 |
| 下载别人的或不存在的收据 | 404 `RECEIPT_NOT_FOUND` | — | — | — | — |
| Stripe 没给付款方式 | 收据上印 `Stripe` | 快照 `payment_method = null` | 否 | — | — |
| 2099 年之后 | 开具检查不过 | 同第一行 | — | 加系列行（迁移） | P2 |

## 6. 数据与安全边界

- **鉴权**：客户两个收据接口与 C 的两个支付接口用 `require_customer`（只读）；管理端下载显式 `require_admin`（ADMIN 登录必过 2FA）。
- **租户过滤**：客户接口只按令牌主体的租户查询；别的租户的收据返回 404，与不存在不可区分。
- **字段白名单**：快照与客户接口不含手续费、状态原因、结案原因、`expected_amount`、内部 id；PDF 只印快照里的值。
- **日志**：开具、补发、下载的日志只带原因码与 `public_id`、计数；不带公司名、邮箱、金额、快照内容。
- **邮件 payload**：含邮箱与公司名，投递成功或死信后由 `_settle` 清空（现有规则）。
- **文件名**：`Content-Disposition` 只用收据号（格式固定为 ASCII），不拼客户输入。
- **密钥**：不新增。
- **依赖**：新增 `fpdf2`（LGPL-3.0，作为库使用，不修改它的源码）。
- **审计**：开具是系统行为、不是管理员操作，不写审计；支付状态变化的审计照 A / B。
- **数据保留**：收据行永久保留，不可删（触发器）；降级只删空表（§8）。

## 7. 测试证据计划

| 风险/需求 | 测试层级 | 场景 | 预期结果 | 任务 |
| --- | --- | --- | --- | --- |
| 迁移、CHECK 与触发器 | integration（MySQL） | 建两张表与 74 行系列；表已存在且形状一致 / 不一致；缺一个触发器；离线输出；降级（空表、有行、离线）；非法插入（序号 0 与 1000000、年份 2025 与 2100、金额 0 与三位小数、币种 USD、同一支付两张、同一年份同一序号）；MySQL 上 UPDATE 与 DELETE 收据行被拒 | 照 0035 的各分支；非法插入被拒；降级有行时报错不删 | 196 |
| 付款方式字段 | unit（假传输） | `payment_method_details.type` 为 `card`、`fpx`、缺失、非字符串、含大写或超长、没有 `latest_charge` | `card`、`fpx`，其余 `None`；A 的状态映射结果不变 | 197 |
| 开具 | integration | 正常开具：编号、快照键集合与每个值、列与快照一致、`tenant_id` 与 `tax_policy_version_id` 取自支付；`send_email` 真与假；同一支付第二次调用；状态不是 `PAID` | 编号 `ACV-AIR-<年>-000001`、下一张 `000002`；真时一行邮件 outbox 行、payload 恰好 6 个键，假时没有；第二次返回同一张、不写任何行；`ValueError` | 197 |
| 开具检查不过 | integration | 三条检查各自不过（没有 TOPUP 行；税务政策 `treatment_json` 读不出；当年没有系列行；`last_sequence = 999999`） | 返回 `None`，系列行、收据与邮件行都不变，ERROR 日志带原因码、不带公司名与金额 | 197 |
| 编号并发与年份 | integration（MySQL） | 两个连接同时开具同一年份；`now` 为 12-31 15:59:59 UTC 与 16:00:00 UTC | 编号连续、不重复；年份分别为当年与次年 | 197 |
| 快照与税务说明 | integration | 开具后改公司名、发布新的税务政策版本 | 快照不变；`tax_notice` 等于支付建单时那一版的原文 | 197 |
| 付款邮件 | unit | payload 合规；每一条不合规（键多一个、少一个、收件人含换行、收据号格式、金额 0 与读不出、`paid_at` 读不出、`payment_id` 不像 uuid、`frontend_base_url` 为空）；标题与正文；链接路径与前端 `paths.ts` 登记的付款结果页一致 | 合规时标题固定、正文与 §2 逐字一致（金额千分位、MYT 时刻）；不合规返回 `None`、日志不带 payload 值 | 198 |
| 邮件接入 outbox | integration | `handled_event_types()` 含新类型；`recover` 领取并投递一行 | 投递后 `SENT`、payload 清空 | 198 |
| 渲染 | unit | 正常快照；`payment_method` 为 `card` / `fpx` / 其他 / null；含 Latin-1 之外字符的公司名；255 字符的公司名；键多一个、少一个、`schema_version = 2`；同一快照渲染两次 | 以 `%PDF-` 开头、恰好一页；内容流里能找到收据号、公司名、`RM 1,000.00`、付款方式的显示值、MYT 时刻、税务说明；不能编码的字符成 `?`、不抛错；长名换行仍一页；不合规抛 `ReceiptSnapshotInvalid`；两次字节相同 | 199 |
| 渲染只读快照 | unit | 渲染时数据库与配置不可用（不传会话） | 照常渲染 | 199 |
| 同事务（自动确认） | integration | `confirm_payment` 判定通过；进入 `REVIEW_REQUIRED`；开具检查不过；开具写入时注入数据库错误 | 通过时支付 `PAID`、TOPUP、收据、邮件行同时提交，付款方式取 Stripe 返回值；复核时没有收据；检查不过时入账照常、没有收据与邮件行；注入错误时异常传出 `confirm_payment`、支付与账本与收据都不变 | 200 |
| 同事务（人工入账） | integration | B 的 CREDIT 成功；CLOSE；CREDIT 时开具检查不过 | CREDIT 时收据金额等于 Stripe 实收（可能不等于 `expected_amount`）、有邮件行；CLOSE 没有收据；检查不过时入账照常 | 200 |
| 补发 | integration | 两笔本批上线前的 `PAID`（没有收据）；一笔检查不过、一笔正常；`PENDING` 与 `REVIEW_REQUIRED` 的支付；超过 500 笔；预算用完（注入时钟） | 正常的得到收据、`payment_method = null`、没有邮件行；检查不过的那笔不阻挡后面的；非 `PAID` 不选；每轮至多 500 笔、按 `paid_at` 升序；预算用完即停 | 201 |
| 补发与入账并发 | integration（MySQL） | 补发锁住支付行时另一个连接对同一笔开具；两轮补发同时跑 | 只有一张收据 | 201 |
| 告警 | integration | `PAID` 无收据且 `paid_at` 为 59 分钟前 / 61 分钟前；有收据；没有任何支付数据 | 只有 61 分钟那一笔计入、P2、摘要段落与 §2 逐字一致；没有支付数据时摘要与原来逐字相同 | 201 |
| Beat 登记 | unit | Celery 配置 | 条目名、间隔、`expires` 与 §2 一致 | 201 |
| 客户接口 | integration（API） | C 的支付列表与详情的收据字段（有、无）；收据列表分页、排序、`total`；下载的状态码、媒体类型、文件名、`Cache-Control` 与内容；别的租户的收据；不像 uuid 的 id；快照被破坏（SQLite 上直接改行） | 字段正确；200 PDF；404；500 带错误信封 | 202 |
| 客户接口字段 | integration（API） | 响应字段 | 只有 §2 列出的字段 | 202 |
| 跨租户 | integration（API） | 租户 A 的令牌下载 B 的收据、在列表里找 B 的收据 | 404；列表里没有 | 202 |
| 管理端接口 | integration（API） | 鉴权（匿名、客户令牌）；列表与详情的收据字段；下载 | 401 / 403；字段正确；200 PDF；404 | 203 |
| 门户页面 | unit（前端） | 历史页有收据与没有收据的行；下载成功、下载中不可重复点、下载失败；结果页 `PAID` 有收据、`PAID` 无收据、其他状态 | 与 §2 一致 | 204 |
| 管理端页面 | unit（前端） | 列表的收据列；详情有与没有收据；下载成功与失败 | 与 §2 一致 | 205 |
| 浏览器验收 | acceptance（只读） | 门户支付历史的收据列；管理端支付列表的收据列 | 列存在；有收据的行显示的收据号与页面自己读到的接口数据一致；不点下载 | 204、205 |

## 8. 迁移与上线

- **迁移 0039**（`down_revision` 为 `0038_payment_settings`）：建 `receipt_number_series`（并插入 2026–2099 共 74 行）与 `payment_receipts`，MySQL 上建两个触发器。检查（两张表的形状、系列行恰好 74 行且 `last_sequence` 在合法范围、触发器的时机事件与正文）、预检（`log_bin_trust_function_creators`）、离线 `--sql`、每个失败窗口与重跑结局照 0035 的写法逐条写进文件头。**downgrade 只删空表**：离线模式抛错；`payment_receipts` 有任何一行就抛错、不发 DDL（收据是不可变的财务快照，删了编号就会复用）；为空时先删触发器、再删两张表。
- **部署顺序**：A、B、C 全部合并之后。196 合并后只多两张没人读写的表；200 合并后新入账开始开具收据；201 合并后补发扫描为此前的 `PAID` 补开（只有 Kelvin 的测试付款，开关在生产关着）。198（邮件渲染）排在 200 之前：200 写的邮件行一出现就有处理器。
- **对客户开放充值**：仍按 B、C 写进 `docs/payment-flow.md` 的步骤；206 在那里加一条开放前检查：「在管理端支付详情能下载测试付款的收据，PDF 内容正确」。
- **回滚**：只回滚应用镜像，不降级 0039。回滚到 200 之前的镜像后新入账不再开具收据；重新部署后补发扫描补开（不发邮件）。

## 9. 方案取舍

| 方案 | 优点 | 风险 | 未采用原因 |
| --- | --- | --- | --- |
| 采用：入账同一事务开具 + 只读检查在前 + 补发扫描兜底 | 正常路径收据与入账原子；收据问题不挡钱入账；补发同时承担上线前的存量 | 两条开具路径 | — |
| 备选 A：只靠后台扫描开具 | 不改 A / B 的代码 | 收据与付款邮件延迟最多一个扫描周期；邮件要另行判断「是不是上线前的」 | spec §43 流程是入账后即开具；邮件判定变复杂 |
| 备选 B：开具失败就让入账一起回滚 | 不变量最简单（每笔 `PAID` 必有收据） | 系列耗尽或税务政策读不出时**所有充值都入不了账**，客户付了钱不到账 | 钱比收据重要 |
| 备选 C：保存点（`begin_nested`）包住开具 | 写入失败也不挡入账 | 仓库的业务代码没有用过保存点，SQLite 测试驱动对保存点的支持要额外处理；检查前置已经够用 | 复杂度不值 |
| 备选 D：编号用 `MAX(sequence) + 1` | 不要计数表 | 依赖隔离级别下的间隙锁才能并发安全 | 不可靠 |
| 备选 E：计数表按需插入年份行 | 不预建 74 行 | 并发插入同一年份撞主键会让入账事务失败 | 预建简单可靠 |
| 备选 F：存 PDF 文件（spec §93 的卷） | 下载快 | 多一个卷、存储抽象与「文件缺失重新渲染」接口 | Kelvin 定即时生成 |
| 备选 G：reportlab | 许可证更宽松（BSD） | 带 C 扩展、体积大 | fpdf2 纯 Python、够用 |
| 备选 H：嵌入 Unicode 字体 | 中文公司名能正常显示 | 仓库要放字体文件（CJK 字体数 MB）并进 wheel | 马来西亚公司注册名为拉丁字母；作为已知限制 |

## 10. 未决问题与假设

- **未决问题**：无。
- **已知限制**：
  1. PDF 用标准字体，Latin-1 之外的字符（例如中文）印成 `?`。马来西亚注册公司名使用拉丁字母，V1 接受；需要时另过闸门加字体。
  2. 开具检查的「账本行缺失」与「税务政策读不出」是永久状态：账本行只追加、不可改（INV-5），税务政策版本有触发器拒绝改删，所以不能靠修数据让它通过；这笔支付永远没有收据，P2 一直报，直到另一个设计闸门给出人工开具或更正的办法。两者在正常数据下都不会发生：入账路径里账本行与收据同一事务、先写账本；税务政策在发布时已校验、外键 RESTRICT 保证存在。与对账单「永久停出」（设计闸门 #565）同一类处理。
  3. 补发扫描每轮按 `paid_at` 取最旧的 500 笔，同时有 500 笔以上持续失败时更新的支付补不上；那时 P2 已在报。
- **假设**：
  1. A、B 交付后，`PAID` 支付的 `gateway_payment_intent_id` 不为空（A、B 写 `PAID` 时都写入它）；为空时快照退而用 `gateway_payment_id`。
  2. `fpdf2` 在给定创建时刻时输出是确定的（不写随机文件标识）。
  3. 本批上线时生产里的 `PAID` 支付只有 Kelvin 的测试付款（充值开关关着）。
- **如果假设错误**：1 → 快照照样可开；2 → 199 的「两次字节相同」测试会失败，由实现在 fpdf2 的选项里关掉随机部分，关不掉时改为比较去掉文件标识后的字节，并在 PR 里说明；3 → 补发照样开具，只是数量多，不发邮件。

## 11. 审查与版本绑定

审查方要回答的五个问题常驻在 [scripts/review_checklist.md](../../scripts/review_checklist.md) 的「设计审查」一节，此处不重复。

**版本绑定规则**（规则的出处是 [WORKFLOW §3](../../docs/WORKFLOW.md)；这里保留一份是因为审查材料里只有本 Issue 正文，两处改一起改）：

- 批准必须写成 `APPROVED: design v<N>`，`<N>` 取本 Issue 顶部的「设计版本」
- **设计版本一变，之前的 APPROVE 自动作废**，必须重新过闸门
- 什么算「实质修改」：改了契约、表结构、事务边界、状态机、失败语义、不变量控制。改错别字不算
- 实质修改时：把顶部版本 +1，在下方追加一条变更说明，状态退回 `READY_FOR_REVIEW`

### 实现拆分

| 任务 | 内容 |
| --- | --- |
| `AIH-TASK-196` | 迁移 0039（两张表、74 行系列、两个触发器）、模型 `app/models/receipts.py`、`docs/database-schema.md` |
| `AIH-TASK-197` | 适配器 `payment_method_type`；开具服务 `issue_receipt`（编号、快照、检查、邮件 outbox 行），与测试 |
| `AIH-TASK-198` | 付款邮件模板与渲染函数、登记进 outbox，与测试 |
| `AIH-TASK-199` | PDF 依赖与渲染 `render_receipt_pdf`、打包冒烟，与测试 |
| `AIH-TASK-200` | `confirm_payment` 与 `resolve_review` 在入账事务里开具，与测试 |
| `AIH-TASK-201` | 补发扫描任务与 Beat 登记、告警判据并进 `wallet_negative_balance` 及 `docs/runbook.md`、`docs/api.md` 里该维度的说明，与测试 |
| `AIH-TASK-202` | 客户接口：收据列表与下载、C 的支付列表与详情加收据字段、schema、`docs/api.md`，与 API 测试 |
| `AIH-TASK-203` | 管理端接口：收据下载、B 的支付列表与详情加收据字段、schema、`docs/api.md`，与 API 测试 |
| `AIH-TASK-204` | 门户前端：接口封装、支付历史与付款结果页的收据，与前端测试；浏览器验收步骤 |
| `AIH-TASK-205` | 管理端前端：接口封装、支付列表与详情的收据，与前端测试；浏览器验收步骤 |
| `AIH-TASK-206` | `docs/payment-flow.md`（收据、补发、告警处置、开放前检查）、`docs/TODO.md` 勾选「支付收据 + 下载」 |

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
| v1 | 2026-11-07 | 初稿 | — |

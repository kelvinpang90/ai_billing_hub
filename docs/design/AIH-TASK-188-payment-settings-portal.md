# AIH-TASK-188..195 设计：支付第三批（支付设置、最小恢复额、充值页、支付历史与付款回跳页）（已批准 v3）

> **来源**：设计闸门 Issue #587。本文件是 `APPROVED: design v3` 那一版正文的**逐字副本**（送审时状态行为 `READY_FOR_REVIEW`，批准后 Issue 上只把状态行改成了 `APPROVED: design v3`）。审查：Codex 额度不可用期间由 Claude Code 审查（审查者由 Kelvin 2026-11-06 选定）。送审前按登记前预审（`worker.design_precheck`）两轮修订；v1 审查阻断「余额缓冲可以为 0，欠整分的挂起租户按最小恢复额充值后余额正好为 0、仍停机」，v2 把缓冲下限改为 0.01，批准；v2 批准后登记前预审报出 §8 回滚说明与 §2 的校验顺序矛盾，升 v3（同时写明锁内恢复额重判与复用的先后、结果页与历史页的两处细节），批准。各轮审查的测试建议项都补进了 AIH-TASK-189 的契约。
> 放进仓库，是因为 OpenClaw Worker 在沙箱里不联网、读不到 GitHub Issue。
> **实现以本文件为准**；与 Issue 不一致时，以 Issue #587 上被批准的 v3 为准。设计要改，就回到 Issue 升版本、重新过闸门，不要直接改本文件。
> 本设计建立在支付第一批（设计闸门 #580 已批准 v3）与第二批（设计闸门 #585 已批准 v3）之上。产品决定（推荐金额管理端可改、客户看到核对中）由 Kelvin 2026-11-06 拍板，见 §1。

## 0. 准入判定

| 任务涉及 | 要填的范围 |
| --- | --- |
| 钱包 / 账本 / 定价 / 汇率 / 支付 / 幂等 / 状态机 | **全部章节** |
| 用量摄取 / 认证与会话（集成侧与平台侧）/ Webhook | §1–§7（**§6 不可省** —— 密钥存储与轮换、鉴权、租户过滤、日志脱敏都在那一节） |
| 前端 / 文档 / CI / 脚本 | **不走本闸门**，直接开 PR |
| 既不碰钱、也不碰用量摄取 / 认证与会话 / Webhook 的后端改动 | **不走本闸门**，直接开 PR |

本任务档位：`全部`。理由：改变建单的金额校验（最低额、最高额、最小恢复额）、复用窗口与待付超时的来源，新增支付相关的配置表与客户可见的支付数据。

---

状态：`READY_FOR_REVIEW`
负责人：Claude
**设计版本**：`v3`
对应需求：spec §7、§24、§27、§42、§44、§63、§66、§71、§89、§112.1；ADR-0007、ADR-0008、ADR-0010；建立在设计闸门 #580（支付第一批，下称「A」）已批准的 v3 与 #585（支付第二批，下称「B」）已批准的 v3 之上
目标 PR：待开（拆成 `AIH-TASK-188`..`195`，见 §11）

## 1. 目标与边界

### 要解决的问题

A 与 B 交付了后端的建单、回调、确认、补对账与管理端复核，但客户还没有任何充值入口，金额规则也还没落到 spec 要求的位置：

1. **金额规则写在环境变量里**：A 把最低额、最高额、待付超时、复用窗口做成了配置项（`payment_topup_min`、`payment_topup_max`、`payment_pending_timeout_seconds`、`payment_reuse_window_seconds`），A 的拆批表写明「管理端设置」属于 C。spec §42 要求最低额「通过管理端设置配置」、前端不许写死。
2. **没有最小恢复额**：spec §42 要求挂起（余额 ≤ 0）的租户充值时，后端算出一个最小恢复额，保证充值后余额严格大于 RM0；ADR-0010 方案 A 进一步要求它等于「欠款 + 可配置的余额缓冲」，消掉「充 RM0.01 复机、下一个请求又停」的循环。
3. **没有客户界面**：spec §71 要求客户能充值（推荐金额与自定义金额）、看支付历史；A 的建单返回的 `success_url` / `cancel_url` 指向 `/portal/payments/{public_id}`，这个页面还不存在。

### Kelvin 2026-11-06 的决定

| 问题 | 决定 |
| --- | --- |
| 推荐金额 | 放进支付设置、管理端可改；初始值为 spec §42 的 RM50 / 100 / 300 / 500 / 1,000；最多 8 个，每个都要在最低额与最高额之间 |
| 客户看到 `REVIEW_REQUIRED` | 显示「核对中」，不显示原因；结案后按结果显示已入账或未入账 |
| 审查者 | Claude（Codex 额度不可用期间） |

A、B 的决定沿用，不重开。

### 可观察的完成标准

1. 管理员在管理端「支付设置」页看到并修改：最低充值额、最高充值额（可空）、最小恢复额的余额缓冲、推荐金额列表、60 秒同额复用窗口、待付超时；每次修改写 `PAYMENT_SETTINGS_UPDATE` 审计；两个管理员同时改，后提交者得到 409。
2. 建单（A 的 `POST /api/v1/customer/topups`）改为从支付设置读这些值；A 的四个对应配置项删除。
3. 余额 ≤ 0 的 `PREPAID` 租户建单时，金额低于 `max(最低额, 欠款 + 缓冲)` 被拒绝（422 `TOPUP_BELOW_RECOVERY_MINIMUM`）；客户在充值页提前看到这个金额。
4. 客户在门户「充值」页看到推荐金额与自定义金额输入框（所有数值都来自接口，前端不写死），选好后跳到 Stripe 付款页；不能充值时（未开放、只读、内部租户、支付未开启）显示原因而不显示表单。
5. 客户在门户「支付历史」页看到自己的支付（编号、金额、状态、实际入账额、时间），看不到手续费、原因与网关编号；`REVIEW_REQUIRED` 显示「核对中」。
6. 付款后跳回的 `/portal/payments/{id}` 页显示这笔支付的状态，等待中每 3 秒刷新一次、最多 2 分钟；跳回本身不改任何状态（A）。

### 明确不做什么

- **不做收据**：属于 D；支付历史不显示收据列。
- **不让客户取消或继续一笔待付的支付**：spec 没有要求；60 秒内重复点同一金额会复用同一笔（A）。
- **不改停机阈值**：ADR-0010 方案 A，停机规则仍是余额 ≤ 0。
- **不改可信确认与入账**：入账额仍按 A 与 B 的规则；最小恢复额只是建单时的校验。
- **不把 `payments_enabled`、`payment_topups_open`、建单允许名单搬进数据库**：它们是运维开关（与密钥、地址一起在部署时决定），保持配置项。
- **不在门户显示 Stripe 的任何东西**：只跳转到 A 返回的 `redirect_url`，门户不加载 Stripe 脚本（A 的决定）。

### 现有行为与证据

- A 的建单（`app/services/payments.py` 的 `create_topup`，AIH-TASK-176）：校验顺序为账户可写 → 开放 → 计费模式 → 金额 → 税务；金额规则 `^[0-9]{1,6}(\.[0-9]{1,2})?$`、不小于最低额（配置，缺省 10.00，不得小于 2.00）、不大于最高额（配置，可空，不得大于 999999.99）；60 秒同额复用；待付超时 2400–86400 秒。
- 门户已有钱包页 `/portal/wallet`（`CustomerWalletView`：`balance`、`billing_mode`、`billing_status`），客户读接口用 `require_customer`、写接口用 `require_customer_writable`（`app/api/auth.py`）。
- 仓库里没有全局设置表；按租户的设置有先例（低余额阈值，设计闸门 #433）。
- 浏览器验收脚本 `scripts/acceptance/admin_customers.mjs` 已有 `login`（管理员）与 `login_customer`（验收客户）步骤。
- 错误信封没有附加字段（`AppError` 只有 `code`、`message`），所以需要客户知道的金额通过读接口给出，不放在错误里。

## 2. 设计概要

### 数据流

```text
管理员 → GET/PUT /api/v1/admin/payment-settings（乐观锁 version）→ payment_settings（单行）+ 审计
客户门户「充值」页 → GET /api/v1/customer/topup-options
  → {可否充值与原因, 最低额, 最高额, 推荐金额, 最小恢复额, 生效的最低额}
  → 客户选金额 → POST /api/v1/customer/topups（A）
       create_topup 从 payment_settings 读金额规则、复用窗口、待付超时；挂起租户另查最小恢复额
  → 跳转 redirect_url（Stripe 托管页）→ 付款 → 跳回 /portal/payments/{id}?result=success|cancelled
客户门户「付款结果」页 → GET /api/v1/customer/payments/{id}（每 3 秒，最多 2 分钟）
客户门户「支付历史」页 → GET /api/v1/customer/payments
```

### 数据库（迁移 0038；`down_revision` 为 B 的 0037）

新表 `payment_settings`，**恰好一行**：

| 列 | 类型 | 约束 / 说明 |
| --- | --- | --- |
| `id` | INT PK | CHECK `id = 1`（`ck_payment_settings_singleton`） |
| `min_amount` | DECIMAL(20,8) NOT NULL | CHECK `min_amount >= 2 AND min_amount <= 999999.99 AND min_amount = ROUND(min_amount, 2)` |
| `max_amount` | DECIMAL(20,8) NULL | CHECK 为空，或 `max_amount >= min_amount AND max_amount <= 999999.99 AND max_amount = ROUND(max_amount, 2)` |
| `recovery_buffer` | DECIMAL(20,8) NOT NULL | CHECK `recovery_buffer >= 0.01 AND recovery_buffer <= 999999.99 AND recovery_buffer = ROUND(recovery_buffer, 2)`（**不能为 0**：见「最小恢复额」） |
| `preset_amounts` | JSON NOT NULL | 金额字符串的数组；形状由应用校验（见下） |
| `reuse_window_seconds` | INT NOT NULL | CHECK 0–300 |
| `pending_timeout_seconds` | INT NOT NULL | CHECK 2400–86400 |
| `version` | INT NOT NULL | CHECK `>= 1`；每次修改 +1 |
| `updated_at` | DATETIME NOT NULL | |
| `updated_by_user_id` | BIGINT NULL，FK `users.id` RESTRICT（`fk_payment_settings_updated_by_user_id`） | 种子行为空 |

索引：`ix_payment_settings_updated_by_user_id`（外键列的显式索引）。CHECK 名为 `ck_payment_settings_<列>`。

迁移插入种子行：`min_amount = 10.00`、`max_amount = NULL`、`recovery_buffer = 10.00`、`preset_amounts = ["50.00","100.00","300.00","500.00","1000.00"]`、`reuse_window_seconds = 60`、`pending_timeout_seconds = 3600`、`version = 1`、`updated_at` 为迁移时刻、`updated_by_user_id = NULL`。种子值与 A 的配置项缺省一致，所以部署后行为不变。

审计动作新增 `PAYMENT_SETTINGS_UPDATE`（前端审计动作清单同步）。

### 支付设置服务：`app/services/payment_settings.py`

- `get_payment_settings(session) -> PaymentSettings`：普通读（不加锁）那一行；不存在 → 抛 `PaymentSettingsMissing`（接口层 503 `PAYMENTS_MISCONFIGURED`；只会在迁移没跑时出现）。
- `update_payment_settings(session_factory, *, actor, values, expected_version, context, now) -> PaymentSettings`：
  1. 校验 `values`（全部字段都必须给，整体替换）。**类型**在请求层严格校验（金额字段必须是 JSON 字符串、推荐金额必须是字符串数组、两个秒数必须是 JSON 整数，布尔值与字符串形式的数字都不算），不合规为 422 `VALIDATION_ERROR`；**规则**在服务里按下列字段顺序逐条校验，消息点名第一个不合规的字段——`min_amount`、`max_amount`、`recovery_buffer`、`preset_amounts`、`reuse_window_seconds`、`pending_timeout_seconds`：
     - 金额字段是字符串，匹配 `^[0-9]{1,6}(\.[0-9]{1,2})?$`；`min_amount ≥ 2.00`；`max_amount` 为 null 或 `≥ min_amount`；`recovery_buffer ≥ 0.01`；
     - `preset_amounts`：1–8 个，每个匹配同一正则、互不相同、严格升序、都 `≥ min_amount` 且（`max_amount` 不为空时）`≤ max_amount`；
     - `reuse_window_seconds` 0–300、`pending_timeout_seconds` 2400–86400 的整数。
     不过 → 422 `PAYMENT_SETTINGS_INVALID`（消息点名第一个不合规的字段）。
  2. 一个事务：先普通读那一行，不存在 → 503 `PAYMENTS_MISCONFIGURED`（不当作版本冲突）；再 `UPDATE payment_settings SET … , version = version + 1, updated_at = now, updated_by_user_id = actor WHERE id = 1 AND version = expected_version`；影响 0 行 → 回滚，409 `PAYMENT_SETTINGS_VERSION_CONFLICT`。同一事务写 `PAYMENT_SETTINGS_UPDATE` 审计（`entity_type` `payment_settings`、`entity_id` `1`；`before` / `after` 为全部业务字段与 `version`，金额照 `_money_text`）。
  3. 新值与旧值完全相同时也照常 +1 版本并写审计（管理员的确认动作本身要留痕）。
- 金额在服务里一律是两位小数的 `Decimal`；JSON 里存定点字符串（例如 `"50.00"`），读出时转回 `Decimal`。

### 最小恢复额

`recovery_minimum(balance, billing_mode, settings) -> Decimal | None`：

- 只对 `billing_mode = PREPAID` 且钱包 `balance ≤ 0` 的租户有值，否则为 `None`。
- 值 = `(−balance + recovery_buffer)` 向上取到分（`ROUND_CEILING`，两位小数）；`balance` 是 DECIMAL(20,8)，欠款可能有两位以上小数，向上取保证充值后余额 ≥ 缓冲。
- **生效的最低额** `effective_minimum = max(min_amount, recovery_minimum)`（`recovery_minimum` 为 `None` 时就是 `min_amount`）。
- **缓冲至少 0.01**（CHECK 与服务校验）：`recovery_minimum = ceil(欠款 + 缓冲) ≥ 欠款 + 0.01`，所以按生效的最低额（不小于它）充值后，余额至少是 RM0.01，严格大于 0（spec §42）。缓冲若允许为 0，欠款是整分时（例如欠 RM50、充 RM50）充值后余额正好为 0、仍然停机，正是 ADR-0010 要消除的边界死锁。
- **单笔上限** `cap` 为 `max_amount`，为空时为 `999999.99`（A 的金额正则决定的上限）。`effective_minimum` 大于 `cap`（欠款很大而上限不够）时：充值页显示「需充值的金额超过单笔上限，请联系客服」；建单时任何金额都会被 422 拒绝（按下面「金额第 3 步」的顺序：低于 `effective_minimum` 的为 `TOPUP_BELOW_RECOVERY_MINIMUM`，其余为 `TOPUP_AMOUNT_INVALID`）；管理员可以调高最高额，或用人工调账处理。

### 对 A 的建单的修改（`create_topup`）

只改 A §2「建单」里列出的几处，校验顺序不变：

1. 金额规则（第 3 步）、复用窗口、待付超时，改为在**快速校验开始时**读一次 `get_payment_settings`，整个建单过程用这一份（锁内不再读，免得同一次建单里前后两份设置不一致）；读不到 → 503 `PAYMENTS_MISCONFIGURED`。
2. 第 3 步（金额）改为按以下顺序，第一条不过即拒绝：格式（A 的正则；缺字段、不是字符串）→ `≥ min_amount` → `≥ effective_minimum` → `≤ max_amount`（设了时）。第一、二、四条不过为 422 `TOPUP_AMOUNT_INVALID`（与 A 相同），第三条不过为 422 `TOPUP_BELOW_RECOVERY_MINIMUM`。快速校验时用不加锁读到的余额判一次；锁内（`lock_tenant_for_customer_write` 的事务里）再用普通读（不加锁，不改变 A 的加锁顺序）读一次钱包余额重判，**这次重判排在锁内的复用查找之前**（不过就不复用、不新建）。A 的锁外预查若直接返回一笔仍开着的候选（复用完成），不再做锁内重判：候选是 60 秒内同金额建的，建它时已按当时的余额过了恢复额校验，快速校验也刚用当前余额判过一次。
3. 复用的候选查询仍按 A，只是窗口来自设置；`expires_at = now + pending_timeout_seconds` 来自设置。
4. 删除 A 的四个配置项 `payment_topup_min`、`payment_topup_max`、`payment_pending_timeout_seconds`、`payment_reuse_window_seconds` 及其校验；`docs/deployment.md` 同步删掉（195）。不保留兼容读取：生产从未打开过支付，没有设置过这些环境变量。

最小恢复额是**建单时**的校验：从建单到付款之间余额可能继续变化（在途用量），缓冲就是为这段留的余地；入账时不再校验（A：确认通过就按 `wallet_credit_amount` 入账）。

### 客户接口：`app/api/customer_payments.py`

三个接口都用 `require_customer`（只读，`DISABLED` / `CLOSED` 也能看），租户只来自令牌主体；**总是注册**（与 `payments_enabled` 无关）。

**`GET /api/v1/customer/topup-options`**

```json
{
  "currency": "MYR",
  "available": true,
  "unavailable_reason": null,
  "min_amount": "10.00",
  "max_amount": null,
  "preset_amounts": ["50.00", "100.00", "300.00", "500.00", "1000.00"],
  "recovery_minimum": null,
  "effective_minimum": "10.00"
}
```

- `available` 为假时 `unavailable_reason` 按以下顺序取第一个成立的（与 A 的建单校验顺序一致，开关放最前）：`PAYMENTS_DISABLED`（`payments_enabled` 为假）→ `ACCOUNT_READ_ONLY`（`DISABLED` / `CLOSED`）→ `TOPUPS_NOT_OPEN`（`payment_topups_open` 为假且租户不在允许名单）→ `TOPUP_NOT_ALLOWED`（不是 `PREPAID`）→ `RECOVERY_EXCEEDS_MAXIMUM`（`effective_minimum` 大于上面定义的单笔上限 `cap`）。
- 金额字段都是两位小数的定点字符串；`max_amount`、`recovery_minimum` 可为 null。不可充值时金额字段照样返回（读不到设置时整体 503 `PAYMENTS_MISCONFIGURED`）。
- 只读，不查 Stripe、不读密钥文件。
- 只有这个接口读支付设置；下面的支付列表与详情不读设置，设置行缺失时照常返回。

**`GET /api/v1/customer/payments`**：`page`（≥ 1）、`page_size`（1–100，缺省 20），返回与管理端列表相同的分页信封（`items`、`page`、`page_size`、`total`）——门户其他列表用游标，这里用页码是因为支付量小、需要显示总数；本租户的支付，按 `created_at` 降序、`id` 降序。列表项：

| 字段 | 说明 |
| --- | --- |
| `id` | `public_id` |
| `amount` | `expected_amount`（两位小数字符串） |
| `currency` | `MYR` |
| `status` | 客户侧状态，见下表 |
| `credited_amount` | 这笔支付的 `TOPUP` 账本行金额，没有为 null（人工按实付入账时可能不等于 `amount`） |
| `created_at`、`paid_at`、`expires_at` | |

客户侧状态（由支付状态与 `resolution` 映射，不返回原始状态、原因、手续费、网关编号）：

| 支付 | 客户侧 `status` | 门户文案（英文 i18n，下为含义） |
| --- | --- | --- |
| `PENDING` | `PENDING` | 等待付款 |
| `PAID`（含人工按实付入账） | `PAID` | 已入账 |
| `REVIEW_REQUIRED` | `IN_REVIEW` | 核对中，如有疑问请联系客服 |
| `FAILED` 且 `resolution = CLOSED` | `NOT_CREDITED` | 未入账，如有疑问请联系客服 |
| `FAILED`（其他） | `FAILED` | 付款失败 |
| `EXPIRED` | `EXPIRED` | 已过期 |
| `CANCELLED` | `CANCELLED` | 已取消 |

**`GET /api/v1/customer/payments/{payment_id}`**：同一组字段；`payment_id` 不存在、属于别的租户、或格式根本不像 uuid → 一律 404 `PAYMENT_NOT_FOUND`（不做格式校验，不区分几种情况）。

### 管理端接口：`GET` / `PUT /api/v1/admin/payment-settings`

每个处理函数显式调用 `require_admin`；总是注册。

- `GET`：返回全部业务字段、`version`、`updated_at`、`updated_by`（管理员邮箱，种子行为 null），字段顺序如下：

  ```json
  {
    "min_amount": "10.00",
    "max_amount": null,
    "recovery_buffer": "10.00",
    "preset_amounts": ["50.00", "100.00", "300.00", "500.00", "1000.00"],
    "reuse_window_seconds": 60,
    "pending_timeout_seconds": 3600,
    "version": 1,
    "updated_at": "2026-11-08T00:00:00",
    "updated_by": null
  }
  ```

- 时刻照仓库现有接口的写法：不带时区的 UTC、精确到秒、不带 `Z`。
- `PUT`：请求体为上例的前七个字段（全部业务字段加 `version`，`extra="forbid"`）；字段类型错误或缺字段 → 422 `VALIDATION_ERROR`（请求层，严格类型见「支付设置服务」）；行缺失 → 503 `PAYMENTS_MISCONFIGURED`；规则不过 → 422 `PAYMENT_SETTINGS_INVALID`；版本不符 → 409 `PAYMENT_SETTINGS_VERSION_CONFLICT`；成功 200 返回与 `GET` 相同的内容。

### 门户页面

- **充值页** `/portal/topup`（门户导航加「Top up」一项）：
  - 读 `topup-options`；`available` 为假时只显示对应原因的说明，不显示表单。
  - 推荐金额显示成按钮，低于 `effective_minimum` 或高于 `max_amount` 的按钮不可点；**点推荐金额只是把它填进金额输入框**，不提交；自定义金额就在同一个输入框里输入，按同一正则即时提示，下限为 `effective_minimum`（不是 `min_amount`）、上限为 `max_amount`（为空时不设，正则已限住 999999.99）；另有一个「去付款」提交按钮，金额不合规时不可点；有 `recovery_minimum` 时在表单上方说明「账户已暂停，至少充值 RM X 才能恢复服务」（X 为 `effective_minimum`）。
  - 提交 → `POST /api/v1/customer/topups` → 成功后 `window.location.assign(redirect_url)`；按钮在请求中不可再点。失败显示错误码、信息与 `request_id`；`TOPUP_BELOW_RECOVERY_MINIMUM` 时重新读一次 `topup-options` 刷新页面上的金额。
- **支付历史页** `/portal/payments`（门户导航加「Payments」一项）：表格列为编号、金额、状态（上表文案）、实际入账额、创建时间与付款时间（+8 显示并标 MYT；实际入账额与付款时间为空时显示短横）；按页码分页，每页 20 条，页码放在地址的查询串 `page` 里（从付款结果页返回时回到同一页），上一页 / 下一页按 `total` 判断是否可用；点一行进付款结果页。
- **付款结果页** `/portal/payments/:paymentId`：
  - 读详情；`status` 为 `PENDING` 时每 3 秒再读一次，最多 40 次（约 2 分钟），之后停止并显示「付款结果还在确认中，可稍后在支付历史查看」；其他状态停止刷新。
  - 查询串 `result=cancelled` 时在顶部显示「你取消了付款」（状态仍以接口为准）；`result=success` 不改变任何显示逻辑（跳回不代表已付，spec §43）。
  - 404 显示「找不到这笔支付」。
  - 第一次读取失败（非 404）：显示错误与 `request_id` 和一个重试按钮，不自动刷新；重试成功后按正常流程处理，若是 `PENDING` 则从 0 重新计 40 次。刷新过程中某次读取失败（非 404）：计入 40 次、继续按 3 秒刷新，页面保留上一次成功的内容；40 次用完时若最后一次失败，同时显示「付款结果还在确认中」的提示与这次的错误和 `request_id`，并给一个重试按钮（重试成功且为 `PENDING` 时从 0 重新计 40 次）。刷新过程中读到 404：停止刷新，改为显示「找不到这笔支付」。
- 文案走 i18n（`en.json`），满足 `no-hardcoded-jsx-text` 与 `keys.test.ts`；不出现 `/api/v1/admin/` 请求。

### 管理端页面

- **支付设置页** `/payment-settings`（管理端顶栏加一项）：表单字段即接口字段；金额输入框（最低额、最高额、缓冲）去掉首尾空白后原样作为字符串发送，最高额去空白后为空表示 null；两个秒数输入框去空白后必须匹配 `^[0-9]+$` 才转成整数发送，否则在页面上提示、不发请求（不会把空框当 0）；推荐金额用逗号分隔的输入框（保存前按逗号拆分、去掉每段首尾空白、丢掉空段，例如 `50, 100,,300,` 得到三个）；显示当前 `version`、最后修改人与时间；保存时带上读到的 `version`；409 时提示「设置已被他人修改，请刷新后再改」并提供刷新按钮；422 显示后端消息；保存成功用返回值刷新表单。

### 事务边界

- 改设置：一个事务（条件更新 + 审计）。
- 建单：A 的事务划分不变；设置在快速校验开始时读一次（独立的短读）。
- 客户与管理端读接口：只读。

### 时间语义

沿用 A：不带时区的 UTC，精确到秒；门户与管理端显示按 +8 并标 MYT。

### 金额与精度

- 设置里的金额两位小数、`Decimal`；最小恢复额向上取到分；比较全部在 `Decimal` 上做，不经 `float`。
- 推荐金额在 JSON 里存定点字符串，避免 JSON 数字的浮点解析。

## 3. 不变量影响矩阵

| 不变量 | 是否触碰 | 可能怎样被破坏 | 设计控制 | 验证方式 |
| --- | --- | --- | --- | --- |
| INV-1 中心故障不中断 AI | 否 | — | — | — |
| INV-2 事件不重复扣费 | 否 | — | — | — |
| INV-3 支付不重复入账 | 否（间接） | 改复用窗口影响 60 秒复用 | 复用与入账的幂等不变（A）；窗口只是候选查询的参数 | A 的测试；§7「设置生效」 |
| INV-4 余额只经账本变动 | 否 | 本批不写钱包 | — | — |
| INV-6 事件保留版本引用 | 是（审计） | 改设置后看不出某笔建单当时用的规则 | 设置的每次修改写审计（含前后值与版本）；支付行已有金额与过期时刻 | §7「改设置写审计」 |
| INV-7 客户不可见成本毛利 | 是 | 客户接口返回手续费或原因 | 客户接口字段白名单（§2）；状态映射不暴露原因 | §7「客户接口字段」 |
| INV-8 租户不可互访 | 是 | 客户看到别人的支付 | 租户来自令牌主体；详情对别的租户 404 | §7「跨租户」 |
| INV-10 金额用 Decimal | 是 | 浮点比较或 JSON 数字 | Decimal、JSON 存字符串、向上取到分 | §7「最小恢复额」 |
| INV-13 状态与事件原子提交 | 是 | 设置改了而审计没写 | 同一事务 | §7「改设置写审计」 |
| 其余（INV-5、9、11、12、14） | 否 | — | — | — |

## 4. 状态与并发

- 支付状态机不变（A、B）。
- **设置的并发**：乐观锁 `version`；两个管理员同时 `PUT`，后提交者 409，不会静默覆盖。
- **改设置与在途建单**：建单开始时读一次设置并整次使用；改设置不影响已建好的支付（`expires_at`、金额已写入支付行）。改设置与建单之间不需要锁：最坏情况是改设置的那一刻正在进行的建单按旧规则完成，这是可接受的（金额规则是输入校验，不是账本规则）。
- **最小恢复额与并发入账**：锁内重读余额；若另一笔支付在这之间入账使余额变正，`recovery_minimum` 为 `None`，按普通最低额判。
- **数据库负责的**：单行（CHECK `id = 1` 与主键）、各字段的范围与精度 CHECK。

## 5. 失败与恢复矩阵

| 失败点 | 对外结果 | 数据最终状态 | 是否可重试 | 恢复来源 | 告警 |
| --- | --- | --- | --- | --- | --- |
| 设置行缺失（迁移没跑） | 503 `PAYMENTS_MISCONFIGURED` | 不建单 | 是 | 跑迁移 | — |
| 改设置时规则不过 | 422 | 不变 | 改后重试 | 管理员 | — |
| 改设置时版本冲突 | 409 | 不变（别人的修改保留） | 刷新后重试 | 管理员 | — |
| 挂起租户金额不够恢复 | 422 `TOPUP_BELOW_RECOVERY_MINIMUM` | 不建单 | 改金额后重试 | 客户 | — |
| 恢复额超过单笔上限 | 充值页说明；建单 422 | 不建单 | 否 | 管理员调高上限或人工调账 | — |
| 付款结果页 2 分钟仍是 `PENDING` | 页面提示稍后查看 | 支付仍 `PENDING`，由回调或 B 的补对账收口 | 是 | A / B | B 的 P2 |
| 客户看别人的支付 | 404 | — | — | — | — |

## 6. 数据与安全边界

- **鉴权**：客户三个接口 `require_customer`（只读）；建单仍是 A 的 `require_customer_writable`；管理端两个接口 `require_admin`（ADMIN 登录必过 2FA）。
- **租户过滤**：客户接口只按令牌主体的租户查询；详情对别的租户返回 404，与不存在不可区分。
- **客户侧字段白名单**：见 §2，不含手续费、状态原因、结案原因、网关编号、`confirmed_*`、`resolution` 原值。
- **日志**：改设置只记管理员用户 id 与新版本号，不记请求体；客户接口不记金额以外的任何内容。
- **跳转**：门户只跳 A 返回的 `redirect_url`（服务端从 Stripe 拿到的托管页地址），不拼接、不接受查询串里的地址（防开放重定向）。
- **密钥**：不新增。
- **审计**：`PAYMENT_SETTINGS_UPDATE`。
- **数据保留**：设置表长期保留；历史值在审计里。

## 7. 测试证据计划

| 风险/需求 | 测试层级 | 场景 | 预期结果 | 任务 |
| --- | --- | --- | --- | --- |
| 迁移与 CHECK | integration（MySQL） | 建表与种子行；第二行（`id = 2`）；各字段越界与三位小数（含缓冲为 0）；`max_amount < min_amount`；降级 | 非法插入被拒；种子值与 §2 一致；降级删表 | 188 |
| 设置校验 | unit | 规则的字段顺序（多个字段同时不合规时点名第一个）；§2 的每条规则与边界（2.00 / 1.99；缓冲 0.01 / 0.00；推荐金额 0 个、9 个、重复、降序、低于最低额、高于最高额；窗口 −1 / 0 / 300 / 301；超时 2399 / 2400 / 86400 / 86401） | 合规通过，其余 422 并点名字段 | 189 |
| 改设置写审计与版本 | integration | 正常修改；与旧值相同的修改；版本不符 | 版本 +1、审计前后值正确；相同值也 +1；409 且数据不变 | 189 |
| 并发改设置 | integration（MySQL） | 两个连接带同一版本同时提交 | 一个成功、一个 409 | 189 |
| 最小恢复额 | unit | 缓冲 10.00、最低额 10.00 时：余额 5.00；0；−0.00000001；−12.345；−50.00；另一组缓冲 0.01：余额 −50.00；非 PREPAID；`effective_minimum` 大于最高额；最高额为空而 `effective_minimum` 大于 999999.99 | 依次为 `None`；10.00；10.01；22.35；60.00；缓冲 0.01 时 50.01，且按它充值后余额为 0.01（> 0）；非 PREPAID 为 `None`；后两组 `RECOVERY_EXCEEDS_MAXIMUM` | 189 |
| 设置生效 | integration | 改最低额、最高额、复用窗口、待付超时后建单 | 建单按新值校验、复用与 `expires_at` 按新值 | 189 |
| 挂起租户建单 | integration | 余额 −20、缓冲 10：建单 25 / 30；锁内重读时余额变为不大于 0 而 60 秒内有同金额的待付候选（重判在复用查找之前，422 且不复用）；锁内重读时余额已被另一笔入账变正；恢复额超过最高额时分别低于和高于恢复额的两笔 | 25 → 422 `TOPUP_BELOW_RECOVERY_MINIMUM`；30 成功；锁内变正后按普通最低额判；超上限时依次为 `TOPUP_BELOW_RECOVERY_MINIMUM` 与 `TOPUP_AMOUNT_INVALID` | 189 |
| 配置项已删除 | unit | 设置了旧的四个环境变量 | 不影响任何行为（不再被读取） | 189 |
| 充值选项 | integration（API） | 五种不可充值原因的顺序；可充值；挂起租户；读不到设置 | `available`、原因、金额字段与 §2 一致；503 | 190 |
| 客户支付列表与详情 | integration（API） | 七种客户侧状态的映射；人工按实付入账的 `credited_amount`；分页与 `total`；别的租户的支付；格式不像 uuid 的 id | 映射正确；不含禁用字段；404 | 190 |
| 客户接口字段 | integration（API） | 响应字段 | 只有 §2 列出的字段 | 190 |
| 跨租户 | integration（API） | 租户 A 的令牌查 B 的支付 | 404，列表里没有 B 的支付 | 190 |
| 管理端设置接口 | integration（API） | 鉴权（匿名、客户）；GET 的字段与顺序；PUT 合规、规则不过、缺字段、多余字段、布尔值或字符串形式的秒数、数字形式的金额、版本冲突、行缺失 | 401 / 403；200；422 两种码；409；503 | 191 |
| 充值页 | unit（前端） | 五种不可充值原因的说明；推荐金额按钮可点与不可点、点了只填金额不提交；自定义金额低于 `effective_minimum` 时去付款按钮不可点；自定义金额的提示；挂起说明；提交中不可重复点；成功跳转到 `redirect_url`；失败展示；`TOPUP_BELOW_RECOVERY_MINIMUM` 后重新读选项 | 与 §2 一致；不写死任何金额 | 192 |
| 支付历史与付款结果页 | unit（前端） | 七种状态文案；空值显示短横；分页；结果页 `PENDING` 时每 3 秒刷新、40 次后停止并提示；终态停止；`result=cancelled` 提示；404；第一次读取失败后重试成功重新计数；刷新中读取失败；40 次用完且最后一次失败时提示、错误与重试按钮同时出现；历史页的页码在查询串里，从结果页返回回到同一页；刷新中读到 404 | 与 §2 一致 | 193 |
| 管理端设置页 | unit（前端） | 读出与回填；推荐金额的拆分（含空段）；最高额留空为 null；秒数框为空或不是整数时不发请求；保存带版本；409 提示与刷新；422 展示 | 与 §2 一致 | 194 |
| 浏览器验收 | acceptance（只读） | 门户充值页、支付历史页；管理端设置页 | 页面内容与页面自己发出的读请求一致 | 192、193、194 |

## 8. 迁移与上线

- **迁移 0038**（`down_revision` 为 B 的 0037）：建 `payment_settings` 并插入种子行。upgrade：表已存在 → 抛错、不发 DDL；离线 `--sql` 输出建表与插入。downgrade：离线模式抛错；表不存在跳过；否则删表（设置的历史在审计里，删表不丢财务数据）。
- **部署顺序**：A、B 全部合并之后。合并即部署；种子值等于 A 的配置缺省，部署前后建单行为不变（生产的建单开关仍关着）。
- **对客户开放充值**（不在本批，Kelvin 执行，195 写进 `docs/payment-flow.md`）：B 的开放前检查都完成之后，在管理端支付设置页核对最低额、缓冲、推荐金额，再设 `BILLING_PAYMENT_TOPUPS_OPEN=true` 并重启。
- **回滚**：设 `BILLING_PAYMENT_TOPUPS_OPEN=false`、清空 `BILLING_PAYMENT_TOPUP_TENANT_ALLOWLIST`，然后重启（两者都是配置项，重启后生效）；之后原本能充值的客户建单 403 `TOPUPS_NOT_OPEN`、充值页显示「未开放」（按 §2 与 A 的校验顺序：`DISABLED` / `CLOSED` 的客户照旧先得到 `ACCOUNT_READ_ONLY`；内部租户也得到 `TOPUPS_NOT_OPEN`，因为开放检查排在计费模式之前）。已建好的支付照常由回调与补对账收口。

## 9. 方案取舍

| 方案 | 优点 | 风险 | 未采用原因 |
| --- | --- | --- | --- |
| 采用：单行设置表 + 乐观锁 + 审计 | 管理端可改、有历史、不会静默覆盖 | 多一张表 | — |
| 备选 A：继续用环境变量 | 不加表 | 违反 spec §42「管理端设置」；改值要重启 | 违反 spec |
| 备选 B：带版本的设置历史表（照税务政策） | 能按时刻回溯 | 金额规则只是建单时的输入校验，不进账本，不需要按时刻解析 | 复杂度不值 |
| 备选 C：最小恢复额只保证余额 > 0 | 简单 | ADR-0010 的「充 RM0.01 复机又停」循环 | 违反 ADR-0010 |
| 备选 D：最小恢复额放进错误响应 | 一次请求就知道 | 错误信封没有附加字段，要改全局约定 | 通过读接口提前给出 |

## 10. 未决问题与假设

- **未决问题**：无。
- **假设**：
  1. 生产从未设置过 A 的四个金额与时间配置项（支付从未在生产打开）。若设置过，删除后会被忽略，行为回到种子值。
  2. 单笔充值的金额上限与 A 一致，为 999,999.99（Stripe 的上限）。
- **如果假设错误**：1 → 管理员在设置页把值改回想要的；2 → CHECK 与校验要一起改，过闸门。

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
| `AIH-TASK-188` | 迁移 0038、模型 `PaymentSettings`、`AuditAction.PAYMENT_SETTINGS_UPDATE`（含前端审计动作清单）、`docs/database-schema.md` |
| `AIH-TASK-189` | 支付设置服务、最小恢复额、建单改读设置并校验最小恢复额、删除 A 的四个配置项，与测试 |
| `AIH-TASK-190` | 客户三个接口（充值选项、支付列表、支付详情）、schema、注册路由、`docs/api.md` 一节，与 API 测试 |
| `AIH-TASK-191` | 管理端支付设置两个接口、schema、注册路由、`docs/api.md` 一节，与 API 测试 |
| `AIH-TASK-192` | 门户前端接口封装、充值页、门户导航，与前端测试；浏览器验收步骤 |
| `AIH-TASK-193` | 门户支付历史页与付款结果页、门户导航，与前端测试；浏览器验收步骤 |
| `AIH-TASK-194` | 管理端支付设置页与顶栏，与前端测试；浏览器验收步骤 |
| `AIH-TASK-195` | `docs/payment-flow.md`（支付设置、最小恢复额、对客户开放的步骤）、`docs/deployment.md` 删除四个配置项、`docs/TODO.md` 勾选 |

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
| v1 | 2026-11-06 | 初稿；送审前按登记前预审两轮修订（金额校验顺序、单笔上限、设置的严格类型与字段顺序、测试种子夹具、门户导航测试、充值页与结果页与设置页的交互细节、回滚步骤） | — |
| v2 | 2026-11-06 | 余额缓冲最小为 0.01（CHECK 与服务校验），保证按最小恢复额充值后余额严格大于 0；删去「缓冲为 0」的错误论证；§7 最小恢复额一行写明每组的预期值并加缓冲 0.01、余额 −50.00 的一组 | v1 审查阻断项 1 条（缓冲为 0 时欠整分的租户充值后余额为 0、仍停机） |
| v3 | 2026-11-06 | §8 回滚说明更正：内部租户在开关关闭时得到 `TOPUPS_NOT_OPEN`（开放检查在计费模式之前）；写明锁内恢复额重判排在锁内复用查找之前、锁外预查直接复用时不再重判；结果页 40 次用完且最后一次失败时的显示与重试；历史页每页 20 条、页码放在查询串里 | 登记前预审（批准后）报出 §8 与 §2 矛盾 1 条 |

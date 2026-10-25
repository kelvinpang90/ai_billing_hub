# AIH-TASK-109 / 110 / 111 / 112 设计：低余额阈值配置与门户低余额提示（已批准 v2）

> **来源**：设计闸门 Issue #433。本文件是 `APPROVED: design v2` 那一版正文的**逐字副本**（审查者为 Codex；送审时状态行为 `READY_FOR_REVIEW`，批准后 Issue 上只把状态行改成了 `APPROVED: design v2`）。
> 放进仓库，是因为 OpenClaw Worker 在沙箱里不联网、读不到 GitHub Issue。
> **实现以本文件为准**；与 Issue 不一致时，以 Issue #433 上被批准的 v2 为准。设计要改，就回到 Issue 升版本、重新过闸门，不要直接改本文件。
> **前置**：设计闸门 #88（[AIH-TASK-005-wallet-ledger.md](AIH-TASK-005-wallet-ledger.md)）的 `low_balance_threshold` 列与 `tenant.low_balance` 事件；设计闸门 #393（[AIH-TASK-099-customer-wallet.md](AIH-TASK-099-customer-wallet.md)）的客户钱包接口与门户余额卡；`AIH-TASK-102` 的 `formatAmountRounded`。
> §8 的四个实现任务登记为 `AIH-TASK-109`（管理端接口）→ `AIH-TASK-110`（客户钱包两个键）；`AIH-TASK-111`（管理端面板）依赖 109，`AIH-TASK-112`（门户提示）依赖 110。

---

## 0. 准入判定

| 任务涉及 | 要填的范围 |
| --- | --- |
| 钱包 / 账本 / 定价 / 汇率 / 支付 / 幂等 / 状态机 | **全部章节** |
| 用量摄取 / 认证与会话（集成侧与平台侧）/ Webhook | §1–§7（**§6 不可省**） |
| 前端 / 文档 / CI / 脚本 | 不走本闸门 |
| 既不碰钱、也不碰用量摄取 / 认证与会话 / Webhook 的后端改动 | 不走本闸门 |

本任务档位：`全部`。不改余额、不写账本，但 `tenants.low_balance_threshold` 是 `post_transaction` 在钱包与租户锁内读取、决定是否写 `tenant.low_balance` 出站事件的那一列（spec §7 第 11 条、§48），并且第一次把钱包相关的配置值返回给客户。

---

状态：`待审`（`READY_FOR_REVIEW`，v2）
负责人：Claude
**设计版本**：`v2`
对应需求：spec §48「Low Balance Alert」（每个客户可配置阈值；跨越时提醒；「display Portal warning」；只在提交后的余额从阈值之上跌到阈值及以下时触发，回到阈值之上才重新武装）、§7 第 11 条（只在真实跃迁时发）、§56（管理员「Configure low balance threshold」）、§67（客户仪表盘「Low balance warning」）、§75（`tenants.low_balance_threshold`）、§80（MYR 常规显示 2 位）、`REQ-PRIV-002`（INV-7）、`REQ-PRIV-001`（INV-8）
前置设计：#88 v3（钱包账本：`low_balance_threshold` 列、`crosses_low_balance`、`tenant.low_balance` 事件，AIH-TASK-005）、#96 / AIH-TASK-009（客户编辑：`PATCH` 明确不收阈值）、#393 v3（客户钱包接口与门户余额卡：当时刻意不返回阈值，AIH-TASK-099..101）
目标 PR：待开（批准后按 §8 拆四个任务登记；`docs/TODO.md`「后续计划」里的「低余额阈值配置（需设计闸门）」「管理端前端：低余额阈值」与「客户仪表盘的其余部分」中的低余额提示即本闸门）

## 1. 目标与边界

- **要解决的问题**：`tenants.low_balance_threshold` 列、跨越判定与 `tenant.low_balance` 事件在 AIH-TASK-005 就已建好，但没有任何入口能设置这一列（`PATCH` 客户编辑明确 422 拒收它），所以现在所有客户都是 `NULL`、永远不发低余额事件；门户也没有低余额提示（#393 把它推迟到本闸门）。
- **可观察的完成标准**：
  1. 管理员能读取并设置（或清除）某个客户的低余额阈值；每次真实改动与一条审计同一事务提交；与现值相同时什么都不写。
  2. 设置之后，`post_transaction` 的跨越判定用的就是新值：`PREPAID` 客户下一笔把余额从新阈值之上压到其下的记账写出恰好一条 `tenant.low_balance`；`INTERNAL_METERED_ONLY` 客户无论阈值如何都不写这个事件（与客户侧 `low_balance` 恒为 `false` 同一口径）。
  3. 客户的 `GET /api/v1/customer/wallet` 多返回阈值与「是否处于低余额」两个键；门户余额卡（钱包页与首页共用）在处于低余额时显示提示，提示里带阈值金额（2 位小数）。
  4. 管理端客户详情页能看到并修改阈值。
- **明确不做什么**（Kelvin 2026-10-26 逐项确认）：
  - **默认不设**：新客户与现有客户都保持 `NULL`（不提醒），不加迁移、不回填、不加系统级默认值。
  - **改阈值不发事件**：管理员把阈值调到当前余额之上（例如余额 30、阈值改成 50）时不补发 `tenant.low_balance`。spec §48 的事件只由余额跨越触发；门户提示按「余额 ≤ 阈值」实时判断，照样会出现。
  - **只有管理员能改**：客户只读，门户不提供修改入口。
  - 不实现低余额通知的投递（Email / WhatsApp / 门户内通知归 Phase 6；`tenant.low_balance` 仍以 `PENDING` 持久等待处理器，INV-14 行为不变）。
  - 不做最小恢复额（随充值设计闸门）、不做充值入口（门户提示只说请充值，没有按钮）。
  - 不改 `crosses_low_balance` 这个纯函数、不改表结构。`post_transaction` 只在写低余额事件的那一处多一个 `billing_mode` 条件（见下文「低余额事件只对预付客户」），别的不动。
- **现有行为与问题证据**：
  - `app/models/tenancy.py:207`：`low_balance_threshold` 为 `DECIMAL(20,8)`、可空、`CHECK (low_balance_threshold IS NULL OR low_balance_threshold >= 0)`。
  - `app/repositories/wallet.py:266`：`crosses_low_balance(threshold, before, after)` = `threshold is not None and before > threshold >= after`；`post_transaction`（`:450`）在钱包锁 → 租户锁（`_lock_tenant`，`populate_existing`）之后读取 `tenant.low_balance_threshold`，跨越时写 `tenant.low_balance`，载荷为 `balance`、`threshold`、`wallet_transaction`。
  - `app/repositories/tenancy.py:68`：`PROFILE_FIELDS` 刻意不含阈值，「阈值归另一个任务」；`docs/api.md`「编辑客户」写明阈值 422。
  - `app/repositories/wallet.py:350`：`get_wallet_summary` 是一条 `tenants JOIN wallets` 的 SELECT，客户钱包接口只用它。

## 2. 设计概要

```text
管理员 PUT（管理员令牌）→ 请求体校验（FastAPI，处理函数之前，422）→ require_admin（第一条语句）
→ 锁客户行（SELECT … FOR UPDATE，不锁钱包）→ 与现值比较 → 不同：UPDATE 阈值与 updated_at + 一条审计，同一事务提交
→ 200（无余额变化、无账本行、无出站事件、不动 status_version）

之后的任意记账：post_transaction 钱包锁 → 租户锁 → 读到已提交的新阈值与 billing_mode → PREPAID 且跨越时写 tenant.low_balance

客户 GET /customer/wallet（客户令牌）→ require_customer → 同一条 tenants JOIN wallets SELECT 多取阈值列
→ low_balance = (billing_mode = PREPAID 且 阈值非空 且 余额 ≤ 阈值) → 白名单投影 → 200
```

### 接口一：`GET /api/v1/admin/customers/{customer_id}/low-balance-threshold`

- 只有 ADMIN；处理函数第一条语句 `require_admin(request)`。客户不存在 404 `CUSTOMER_NOT_FOUND`。只读，不写审计。
- 200：`data` 恰好一个键 `{"low_balance_threshold": "50.00000000"}`，未设置时为 `null`。金额是恰好 8 位小数的字符串（`money_text`），与管理端其他金额一致。

### 接口二：`PUT /api/v1/admin/customers/{customer_id}/low-balance-threshold`

- 只有 ADMIN；处理函数第一条语句 `require_admin(request)`。
- 请求体 `extra="forbid"`，恰好一个**必填**键 `low_balance_threshold`：
  - JSON 字符串，匹配 `[0-9]{1,12}(\.[0-9]{1,2})?`（用 `[0-9]` 不用 `\d`，理由同调账金额），且大于 0；或者显式 `null`，表示清除（不提醒）。
  - 键缺失、JSON 数字、负数、`0` / `0.00`、超过 2 位小数、超过 12 位整数、空串、前后带空白：一律 422 `VALIDATION_ERROR`，文案只列字段名、不回显值（`app/core/errors.py` 现有行为）。
  - 只收 2 位小数：阈值是给人看的提醒线，MYR 常规显示就是 2 位（spec §80）；不舍入、超出直接 422。存库时 `quantize_money` 到 8 位。
  - 不收 `0`：余额 ≤ 0 已经是 `SUSPENDED`（spec §7 第 8–10 条），阈值为 0 的提醒与暂停重合；不想提醒用 `null`。
  - 「大于 0、最多 2 位」只在接口层校验，库约束仍是 `NULL` 或 `>= 0`（见「数据库」）。本接口是阈值唯一的写入入口；若以后有别的路径直接写进 `50.001` 这样的值，判定用精确值、门户按 2 位显示 `50.00`，显示与判定线会差不到 0.01 —— 任何新增的写入入口都必须复用本接口的校验。
  - 不要求 `reason`：与编辑客户资料一致，前后值都进审计。
- 客户不存在：404 `CUSTOMER_NOT_FOUND`，什么都不写。
- 与现值相同（含两边都是 `null`）：200，返回当前值，不写库、不写审计、`updated_at` 不变。
- 成功：200，`data` 形状同接口一（改后的值）。
- 客户的 `account_status`（含 `DISABLED`）与 `billing_mode`（含 `INTERNAL_METERED_ONLY`）都不拦。内部计量租户仍可能有管理员调账（`app/services/wallet_adjustments.py` 照常调用 `post_transaction`），但下文「低余额事件只对预付客户」保证它不写 `tenant.low_balance`，客户侧 `low_balance` 也恒为 `false`（见接口三），所以设了无害；拦它反而要另处理「先设阈值、后转内部计量」的顺序（`enable_internal_metering` 不看阈值）。

### 接口三：`GET /api/v1/customer/wallet` 多两个键

在 #393 v3 的 6 个键之外加两个，恰好 8 个键：

| 字段 | 说明 |
| --- | --- |
| `low_balance_threshold` | 管理员给这个客户设的提醒线：恰好 8 位小数的字符串，未设置为 `null` |
| `low_balance` | 布尔：`billing_mode = PREPAID`、阈值非空、且 `balance ≤ low_balance_threshold` 时为 `true`，否则 `false` |

- 阈值与余额、三个状态出自**同一条** SELECT（`get_wallet_summary` 多选一列 `tenants.low_balance_threshold`，`WalletSummary` 多一个字段），不会读到「阈值已改、余额是旧的」以外的组合；`low_balance` 在 Python 里用 `Decimal` 比较算出，不经 float、不在 SQL 里算。
- 等号与 spec §48 的「at-or-below」一致：余额恰好等于阈值时为 `true`。
- 余额 ≤ 0（`SUSPENDED`）且设了阈值时也是 `true`：余额确实在提醒线以下；门户余额卡另有计费状态显示，两者不冲突。
- `INTERNAL_METERED_ONLY` 恒为 `false`：它的余额通常为 0、AI 照常可用，显示低余额就是误报（与余额卡只对 `PREPAID` 显示计费状态同一理由）。
- 规则只在后端算一次：前端只看 `low_balance`，不自己比较金额。
- #393 v3 当时「`low_balance_threshold` 刻意不出现在任何响应里」的理由是「没有入口能设置它」；本设计给了入口，客户看到自己的提醒线是 Kelvin 2026-10-26 的选择（「提示加阈值金额」）。阈值不是成本、汇率或毛利，不触 INV-7。钱包 `version`、`status_version`、内部 id 仍然不返回。

### 低余额事件只对预付客户（改 `post_transaction` 的一处条件）

- v1 假设「内部计量租户不记账、不会产生事件」不成立：管理员调账对任何模式的租户都走 `post_transaction`（`app/services/wallet_adjustments.py:147`），而写事件的条件（`app/repositories/wallet.py:450`）只看 `crosses_low_balance(threshold, before, after)`，不看 `billing_mode`。例如内部计量租户先调账到 100，再设阈值 50、扣到 40，就会写出 `tenant.low_balance`，与客户侧 `low_balance = false` 相反，Phase 6 的通知处理器上线后会发误报（设计闸门 #433 v1 Codex 审查阻断项）。
- 改为：`if tenant.billing_mode is BillingMode.PREPAID and crosses_low_balance(tenant.low_balance_threshold, before, after):`。`tenant` 是 `_lock_tenant` 在锁内刷新过的行，`billing_mode` 是已提交的最新值；`crosses_low_balance` 纯函数不改，它的既有单测不变。
- 规则因此只有一条，后端两处同口径：**低余额 = `PREPAID` 且阈值非空且余额 ≤ 阈值**；事件在这个条件从假变真的那一笔记账写出。
- 模式只能 `PREPAID → INTERNAL_METERED_ONLY` 单向、且只在没有任何钱包流水时（`docs/api.md`「初始化内部计量租户」），所以不存在「内部计量期间跌破、转回预付后补发」的情形。
- 用量计费路径本来就只对 `PREPAID` 且计费额 > 0 的事件记账（`app/services/usage_billing.py:385`），行为不变。
- 既有数据：生产上所有租户的阈值都是 `NULL`（没有写入入口），这一改动对已写出的事件没有影响，不需要清理。

### 数据库

- 不改表结构、不加迁移、不加索引。列、可空性与 `CHECK >= 0` 已在（AIH-TASK-005）；本设计的「> 0、最多 2 位小数」只在接口层校验，库约束不收紧（收紧要迁移，且 `NULL` 与 `>= 0` 已足以保证判定不出错）。
- 写入函数放在 `app/repositories/tenancy.py`：`set_low_balance_threshold(session, tenant, *, threshold, now) -> bool`，值相同返回 `False` 且不碰 `updated_at`、不 flush；不同则赋值、改 `updated_at`、flush，返回 `True`。`PROFILE_FIELDS` 与 `update_tenant_profile` 不变（阈值不并进资料编辑）。

### 事务边界

- 接口二：一个事务里「锁客户行 → 比较 → UPDATE → 审计」，`session_scope` 提交，要么都在、要么都不在。**只锁租户行，不锁钱包**。
- 接口一、三：普通只读会话，一条 SELECT（接口一找客户读列；接口三沿用 #393 的那一条），不提交、不加锁。

### 外部系统与异步边界

- 无。不写 `domain_outbox`，不碰 Celery / Redis，不发状态 webhook（阈值不在状态快照里，spec §28；`status_version` 不变）。
- 已有的 `tenant.low_balance` 事件照旧只由 `post_transaction` 写（v2 起只对 `PREPAID`），仍无处理器、持久 `PENDING`（`docs/api.md` 告警维度 `outbox_backlog` 不计入它）。

### 时间

- 审计时间与 `updated_at` 用服务层的同一个 `now`（UTC），与 `update_customer` 一致。没有生效时间：提交即生效，之后的记账按新值判定，之前已提交的记账不回溯。

### 金额

- 入参：2 位小数以内的十进制字符串 → `Decimal` → `quantize_money`（8 位），不经 float（INV-10）。
- 出参：管理端与客户端都是 8 位小数字符串（`money_text`），原样来自 `DECIMAL(20,8)` 列，不舍入。
- 前端显示：管理端与门户都按 2 位显示（AIH-TASK-102 的 `formatAmountRounded(value, 2)`，字符串 `ROUND_HALF_UP`）；因为入参最多 2 位，这里的舍入实际不会改变数值。

### 审计

- 新动作 `TENANT_LOW_BALANCE_THRESHOLD_SET`（`AuditAction` 的列是写死宽度的 VARCHAR、没有 CHECK，加值不需要迁移）。不在 spec §66 的清单里，按 `PROJECT_CREATE` 的先例补上，记在实现 PR 描述里。
- `actor` 为管理员，`entity_type = tenant`（`ENTITY_TENANT`）、`entity_id` 为客户 `public_id`，带请求上下文（IP、UA），与 `CUSTOMER_UPDATE` 同一写法。
- `before_state` / `after_state` 都恰好是 `{"public_id": …, "low_balance_threshold": "50.00000000" 或 null}`（字段白名单，不 dump 整行）。阈值不是个人数据。
- 只在真实改动时写一条；值相同不写。

### 前端（第三、第四个任务，不在闸门范围内，写在这里只为说明接口够用）

- **管理端**：客户详情页钱包卡附近加「低余额阈值」面板：读接口一显示当前值（2 位，未设置显示「未设置」）；「修改」打开表单（字符串输入，客户端按同一正则先校验，服务端为准），「清除」提交 `null`；保存成功后重读面板，失败显示 `request_id`。审计页的动作筛选清单（`frontend/src/api/adminAudit.ts` 的 `AUDIT_ACTIONS`）**不在这个前端任务里**：`adminAudit.test.ts` 直接解析 `app/models/auth.py` 的 `AuditAction` 逐项比对，后端加了动作而前端没跟上，前端测试当场就红，所以这一行由加动作的第一个任务（AIH-TASK-109）一起补。
- **门户**：`WalletSummaryCard`（钱包页与首页共用）在 `low_balance` 为 `true` 时于卡内显示警告：余额已不高于提醒线 `MYR 50.00`，请尽快充值；`false` 时不显示。不显示也不请求任何成本、毛利或内部字段。

## 3. 不变量影响矩阵

| 不变量 | 是否触碰 | 可能怎样被破坏 | 设计控制 | 验证方式 |
| --- | --- | --- | --- | --- |
| INV-1 中心故障不中断 AI | 否 | 阈值不进集成侧的状态快照与 AI 放行判定 | `compose_effective_status` 不读阈值；不改状态 webhook 与 `status_version` | 测试：改阈值前后 `status_version` 不变、不新增投递 |
| INV-2 事件不重复扣费 | 否 | 不处理用量事件 | — | — |
| INV-3 支付不重复入账 | 否 | 不处理支付 | — | — |
| INV-4 余额只经账本变动 | 是（守住） | 设置阈值的路径顺手改了余额或钱包 | 接口二只锁、只写租户行的阈值与 `updated_at`，不读不写 `wallets` | 测试：设置前后钱包余额、`version`、账本行数都不变 |
| INV-5 历史账本不可变 | 否 | 不碰账本 | — | — |
| INV-6 事件保留版本引用 | 否 | 不处理用量事件 | — | — |
| INV-7 客户不可见成本毛利 | 是 | 客户钱包接口多出字段时顺带带出钱包 `version`、`status_version`、内部 id，或以后的列 | 客户侧仍是显式白名单模型，只加两个键；不继承管理端模型 | 测试：`data` 恰好 8 个键 |
| INV-8 租户不可互访 | 是 | 管理员接口按错的客户写；客户接口读到别人的阈值 | 管理端客户只来自路径 `customer_id`（`public_id`），请求体禁止多余字段；客户端租户只取自 `CustomerPrincipal.tenant_id` | 测试：给 A 设阈值，B 的管理端 GET 与 B 的钱包都看不到；请求体带 `tenant_id` / `customer_id` 422 |
| INV-9 对话内容不入库 | 否 | 不处理对话 | — | — |
| INV-10 金额用 Decimal | 是 | JSON 数字经 float 进来；比较或显示走 float | 只收 JSON 字符串，正则校验后 `Decimal`；`low_balance` 用 `Decimal` 比较；前端只用 `formatAmountRounded` 字符串运算 | 测试：JSON 数字 422；阈值 `50.01`、余额 `50.01000000` 时 `low_balance` 为 `true`，余额 `50.01000001` 时为 `false` |
| INV-11 event_id 至多一次财务效果 | 否 | 不处理用量事件 | — | — |
| INV-12 定稿对账单不可变 | 否 | 对账单尚未实现 | — | — |
| INV-13 状态与事件原子提交 | 是 | 阈值改了但审计没写（或反之）；记账读到改了一半的阈值；内部计量租户写出与客户侧判定相反的低余额事件 | 阈值与审计同一事务；记账在租户行锁内读阈值与 `billing_mode`，设置也持租户行锁，二者串行；事件条件与客户侧 `low_balance` 同一规则 | 测试：审计写入失败时阈值回滚；内部计量租户调账跨越不写事件；MySQL 并发用例见 §7 |
| INV-14 队列丢失不毁持久工作 | 否 | 不写 outbox、不入队 | `tenant.low_balance` 仍只由 `post_transaction` 在记账事务里写进 `domain_outbox` | — |

## 4. 状态与并发

阈值不是状态机，没有跃迁表；它是租户行上的一个配置值：`NULL` ↔ 正数，任意互换。

- **串行化**：接口二在事务内 `SELECT … FOR UPDATE` 锁租户行（`_require_tenant(..., for_update=True)`，与 `update_customer` 同一写法）。`post_transaction` 的加锁顺序是钱包 → 租户；接口二只锁租户、不锁钱包，所以两条路径之间没有环，不会死锁。
- **与记账并发**：记账在拿到租户行锁之后（`populate_existing`）才读阈值。设置先拿到锁：记账等它提交，读到新值；记账先拿到锁：设置等记账提交，那一笔按旧值判定。两种顺序下每一笔记账都恰好用了一个已提交的阈值。
- **并发设置**：同一客户的两次设置按锁顺序串行，最后提交的为准；每条审计的前后值对应它自己那一次改动（锁内读取 before）。
- **唯一性 / 幂等**：PUT 是设置语义，天然幂等：同值重放 200、不写；没有幂等键。不同的值就是新的设置。
- **重试**：不在服务端重试；锁等待超时（MySQL 1205）或死锁（1213）落到现有的未预期异常处理（`session_scope` 回滚，500 `INTERNAL_ERROR`，带 `request_id`），由管理员重新提交。
- **必须原子提交**：阈值 UPDATE、`updated_at`、`TENANT_LOW_BALANCE_THRESHOLD_SET` 审计。

## 5. 失败与恢复矩阵

| 失败点 | 对外结果 | 数据最终状态 | 是否可重试 | 恢复来源 | 告警 |
| --- | --- | --- | --- | --- | --- |
| 校验失败（形状、正则、0、负数、JSON 数字、多余字段） | 422 `VALIDATION_ERROR`，不回显值 | 什么都没写 | 改正后重试 | — | 无 |
| 客户不存在 | 404 `CUSTOMER_NOT_FOUND` | 什么都没写 | 否 | — | 无 |
| 非 ADMIN / 未登录 | 401 / 403（现有鉴权） | 什么都没写 | 否 | — | 无 |
| 锁等待超时 / 死锁 | 500 `INTERNAL_ERROR`，带 `request_id` | 整个事务回滚，阈值与审计都不变 | 是，管理员重提 | — | 现有错误日志 |
| DB 提交失败（含审计写入失败） | 500，带 `request_id` | 回滚：阈值不变、无审计 | 是 | — | 现有错误日志 |
| Redis/Celery 丢失 | 不影响：本设计不入队 | 不变 | — | — | — |
| 外部服务超时 | 不适用：无外部调用 | — | — | — | — |
| 重复请求（同值） | 200，当前值 | 不写库、不写审计 | 是 | — | 无 |
| 客户钱包接口：钱包缺失 | 沿用 #393 v3：`RuntimeError(WALLET_MISSING)` → 500 | 不变 | 否 | 运维 | 现有错误日志 |

## 6. 数据与安全边界

- **租户过滤**：管理端按路径 `public_id` 找客户（ADMIN 可见全部客户，与其他管理端客户接口一致）；客户端只用 `CustomerPrincipal.tenant_id`，处理函数第一条语句 `require_customer`（#393 现有）。
- **鉴权主体**：接口一、二仅 ADMIN（`require_admin` 显式调用；`tests/backend/test_admin_customers_api.py` 的路由枚举逼新路由补进期望集合与合法请求体）。接口三沿用客户令牌；只读客户（`read_only`）可读，本来就没有写。
- **客户侧禁止返回的字段**：钱包 `version`、`status_version`、内部 `id` / `tenant_id` / `wallet_id`、任何成本 / 汇率 / 参考价 / 毛利。新增只有 `low_balance_threshold` 与 `low_balance`。
- **日志 / 审计 / 异常**：接口层不记请求体；422 不回显值；审计只有 `public_id` 与阈值前后值。阈值不是个人数据，可进审计。
- **密钥**：不涉及。
- **prompt / response / 客户数据**：不涉及；不读写 `email`、联系人、电话。
- **数据保留**：审计永久保留（现有策略）；清除阈值是写 `NULL`，不删任何行。

## 7. 测试证据计划

后端（`tests/backend/test_low_balance_threshold_api.py` 新文件，SQLite 与 MySQL 参数化，MySQL 用 `BILLING_TEST_DATABASE_URL`、未设置时 skip、CI 必跑）：

| 风险/需求 | 测试层级 | 场景 | 预期结果 |
| --- | --- | --- | --- |
| 正常路径 | integration | 新客户 GET；PUT `"50.00"`；再 GET | 先 `null`；PUT 200 返回 `"50.00000000"`；GET 同值；一条 `TENANT_LOW_BALANCE_THRESHOLD_SET`，前 `null`、后 `"50.00000000"`，actor 为该管理员；`updated_at` 前进 |
| 清除 | integration | 设 `"50"` 后 PUT `null` | 200 `null`；第二条审计前 `"50.00000000"`、后 `null` |
| 同值不写 | integration | 连续两次 PUT `"50.00"`；对未设置的客户 PUT `null` | 第二次 200、审计仍一条、`updated_at` 不变；`null`→`null` 不写审计 |
| 边界值 | integration | `"0.01"`、`"999999999999.99"`、`"7"`、`"7.5"` | 都 200，存为对应 8 位值 |
| 校验 | integration | 缺键 `{}`、`50`（JSON 数字）、`"0"`、`"0.00"`、`"-1"`、`"1.001"`、`"1000000000000"`、`""`、`" 5"`、`"5."`、`".5"`、`"１"`（全角）、多带 `tenant_id` | 全部 422 `VALIDATION_ERROR`，响应文本里不出现提交的值；阈值与审计都不变 |
| 客户不存在 | integration | 全零占位 `public_id` 的 GET 与 PUT | 404 `CUSTOMER_NOT_FOUND`，不写审计 |
| 鉴权 | integration | 路由枚举用例（`test_admin_customers_api.py`）补两条路由与 PUT 的合法请求体 | 匿名 401、客户令牌 403，均不写库 |
| 余额不动（INV-4） | integration | 有余额的客户设阈值前后 | 钱包 `balance`、`version`、账本行数不变；`status_version` 不变；`domain_outbox` 行数不变 |
| 设置后跨越用新值 | integration | 余额 `100`；PUT `"50"`；调账扣 `60`（余额 40） | 恰好一条 `tenant.low_balance`，载荷 `threshold = "50.00000000"`、`balance = "40.00000000"` |
| 改阈值不补发 | integration | 余额 `30`；PUT `"50"` | 没有新的 `tenant.low_balance`；客户钱包 `low_balance = true` |
| 重新武装 | integration | 接上一行：调账加 `30`（余额 60）再扣 `20`（余额 40） | 只在扣到 40 那一笔多出一条事件（共一条） |
| 内部计量不发事件 | integration | 客户转 `INTERNAL_METERED_ONLY`；调账加 `100`；PUT `"50"`；调账扣 `60`（余额 40） | 没有 `tenant.low_balance`；同样的步骤对 `PREPAID` 客户恰好一条（对照） |
| 先设阈值后转内部计量 | integration | 新客户 PUT `"50"`；转 `INTERNAL_METERED_ONLY`；调账加 `100` 再扣 `60` | 没有 `tenant.low_balance` |
| 并发：与记账 | integration（MySQL） | 事务 A 锁住客户行并把阈值改为 `"50"`、暂不提交；线程 B 对余额 `100` 的客户扣 `60`；用 `performance_schema.data_lock_waits` 确认 B 在等租户行锁后提交 A | B 完成后恰好一条 `tenant.low_balance`，载荷阈值为 `"50.00000000"`；无死锁 |
| 并发：两次设置 | integration（MySQL） | 两个线程同时 PUT `"20"` 与 `"30"` | 两条审计，第二条的 `before` 等于第一条的 `after`；最终值等于后提交的那条的 `after` |
| 事务中途失败 | integration | 让审计写入抛错（monkeypatch `record_audit`） | 500；阈值仍是旧值、无审计 |
| 租户越权 | integration | 给 A 设阈值 | B 的管理端 GET 为 `null`；B 的客户钱包 `low_balance_threshold` 为 `null` |
| 幂等重放 | integration | 同「同值不写」 | 同上 |
| Redis/Celery 丢失 | — | 不适用：本设计不入队 | — |

客户钱包（`tests/backend/test_customer_wallet_api.py` 改既有「恰好 6 个键」为 8 个键并补用例）：

| 风险/需求 | 测试层级 | 场景 | 预期结果 |
| --- | --- | --- | --- |
| 字段白名单（INV-7） | integration | 任意客户 GET | `data` 恰好 8 个键 |
| 未设置 | integration | 阈值 `NULL` | `low_balance_threshold = null`、`low_balance = false` |
| 金额精度（INV-10） | integration | 阈值 `50.01`；余额分别为 `50.01000001`、`50.01000000`、`-5` | `false`、`true`、`true` |
| 内部计量 | integration | `INTERNAL_METERED_ONLY`、余额 0、阈值 `50` | `low_balance = false`，阈值照常返回 |
| 同一快照 | unit | `get_wallet_summary` 仍只发一条 SELECT | 断言语句数为 1（只断言方言无关的片段） |

前端（vitest + Testing Library）：管理端面板的显示、修改、清除、客户端校验、失败带 `request_id`；门户余额卡在 `low_balance` 为 `true` / `false` 时显示 / 不显示警告、警告里阈值按 2 位显示、`INTERNAL_METERED_ONLY` 与 `false` 一致；两侧都不请求不该请求的路径。部署后浏览器验收各加一个只读步骤（被动观察页面自己发出的请求，不提交表单）。

## 8. 迁移与上线

- **数据迁移**：无。不改表结构；现有客户保持 `NULL`。
- **锁表与性能**：接口二锁一行租户，持锁时间是一次 UPDATE 加一次审计 INSERT；与记账争同一行锁，与 `update_customer`、改账户状态相同量级。接口三的 SELECT 多一列，执行计划不变。
- **兼容窗口**：客户钱包接口是加字段（6 → 8）；门户前端在 AIH-TASK-112 之前不读新字段，不受影响。
- **部署顺序与拆分**（每个任务合并后都能单独通过 CI 并部署）：
  1. `AIH-TASK-109`（后端，管理端）：接口一、二，审计动作（连同 `frontend/src/api/adminAudit.ts` 的清单），仓库函数，`post_transaction` 的 `PREPAID` 条件，`docs/api.md` 的新小节（并删掉「不在本批接口里」与「编辑客户」里关于阈值「不走这个接口」的过时说法，改为指向新小节），`docs/database-schema.md` 第 80 行的归属说明。
  2. `AIH-TASK-110`（后端，客户端）：接口三的两个键，`docs/api.md` 客户钱包小节。依赖 109（文档与语义相邻，串行更好审）。
  3. `AIH-TASK-111`（前端，管理端）：阈值面板与审计页动作清单，带浏览器只读步骤。依赖 109。
  4. `AIH-TASK-112`（前端，门户）：余额卡低余额警告，带浏览器只读步骤。依赖 110。
- **回滚或前滚**：都是加接口、加字段，回滚即 revert 对应 PR；已写的阈值留在库里，旧代码照样在记账时读它（AIH-TASK-005 起就读），不会出错。
- **部分部署**：109 上线而 110 未上线：管理员能设阈值、事件照常按新值发，门户暂不提示。110 上线而 112 未上线：接口多两个键，门户不显示。
- **监控**：无新增告警维度；`tenant.low_balance` 仍不计入 `outbox_backlog`。负责人 Kelvin。

## 9. 方案取舍

| 方案 | 优点 | 风险 | 未采用原因 |
| --- | --- | --- | --- |
| 采用：独立子资源 `…/low-balance-threshold` 的 GET / PUT | 客户对象白名单不变；`PATCH` 资料编辑的「阈值 422」保留；审计动作单独可筛 | 多两条路由，鉴权枚举要补 | — |
| 备选 A：阈值并进 `PATCH /customers/{id}` 与客户详情 | 少一个接口 | 客户详情的「恰好这些键」在多处测试与文档里钉死，要一起改；资料编辑的审计（`CUSTOMER_UPDATE` 只记字段名）与阈值要记前后值的口径不同，要拆两套 | 改动面更大，审计口径混在一起 |
| 备选 B：门户提示由前端自己比较余额与阈值 | 后端只返回阈值 | 规则（`PREPAID` 才算、等号、Decimal 比较）分散到前端，前端没有 Decimal；与以后 Phase 6 通知的判定可能分叉 | 规则只在后端写一次 |
| 备选 C：改阈值时若余额已 ≤ 新阈值就补发事件 | 客户马上收到通知（Phase 6 上线后） | 违背 spec §48「只由余额跨越触发」；需要在设置路径里锁钱包读余额，引入钱包 → 租户之外的第二种加锁路径 | Kelvin 2026-10-26 选不发 |
| 备选 D：库约束收紧为 `> 0` 且最多 2 位 | 库层兜底 | 要迁移；`NULL` / `>= 0` 已保证跨越判定正确，接口是唯一写入方 | 收益不抵迁移成本 |

## 10. 未决问题与假设

- **未决问题**：无。Kelvin 2026-10-26 已拍板四项：默认不设、改阈值不发事件、门户显示提示加阈值金额、只有管理员能改。
- **尚未验证的假设**：
  - `post_transaction` 读阈值与 `billing_mode` 发生在 `_lock_tenant` 之后（`app/repositories/wallet.py:420`，已读代码确认），所以 §4 的串行化成立；§7 的 MySQL 并发用例实测它。
  - 设置路径不锁钱包就不会与记账形成锁环：记账先钱包后租户，设置只锁租户；InnoDB 下单行锁等待不成环。
- **需要谁拍板**：无。
- **如果假设错误**：若记账在锁外读了阈值，那一笔可能按旧值判定，最坏是多发或漏发一条 `tenant.low_balance`（Phase 6 前没有投递，不触钱）；并发用例会红，在合并前暴露。

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
| v2 | 2026-10-26 | ① 低余额事件只对 `PREPAID` 客户：`post_transaction` 写 `tenant.low_balance` 的条件加 `billing_mode is PREPAID`，与客户侧 `low_balance` 同一规则；删去「内部计量租户不记账」的错误假设。② §7 补「内部计量不发事件」「先设阈值后转内部计量」两行。③ INV-13 一行补内部计量。④ 写明「大于 0、最多 2 位」只在接口层、本接口是唯一写入入口。⑤ 审计页动作清单改由 AIH-TASK-109 补（`adminAudit.test.ts` 逐项比对后端枚举），§8 第 1 步同步 | Codex 设计审查 v1 的阻断项：管理员调账对内部计量租户照样调用 `post_transaction`，跨越判定不看 `billing_mode`，会写出与客户侧相反的低余额事件；第 3 问的非阻断意见；登记前核对发现 `adminAudit.test.ts` 的跨语言比对 |

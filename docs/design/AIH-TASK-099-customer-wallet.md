# AIH-TASK-099 / 100 / 101 设计：客户门户仪表盘与钱包页（已批准 v1）

> **来源**：设计闸门 Issue #393。本文件是 `APPROVED: design v1` 那一版正文的**逐字副本**（审查者 Codex；送审时状态行为 `READY_FOR_REVIEW`，批准后 Issue 上只把状态行改成了 `APPROVED: design v1`）。
> 放进仓库，是因为 OpenClaw Worker 在沙箱里不联网、读不到 GitHub Issue。
> **实现以本文件为准**；与 Issue 不一致时，以 Issue #393 上被批准的 v1 为准。设计要改，就回到 Issue 升版本、重新过闸门，不要直接改本文件。
> **前置**：设计闸门 #368（[AIH-TASK-089-customer-portal-auth.md](AIH-TASK-089-customer-portal-auth.md)）的 `AIH-TASK-093`（`require_customer` 与 `app/api/customer.py`）与 `AIH-TASK-097`（门户首页）；翻页规则沿用 #351（[AIH-TASK-083-wallet-ledger-view.md](AIH-TASK-083-wallet-ledger-view.md)）。
> §8 的三个实现任务登记为 `AIH-TASK-099`（接口）→ `AIH-TASK-100`（门户钱包页）→ `AIH-TASK-101`（门户首页的余额卡与最近流水），按这个顺序依赖。

---

## 0. 准入判定

| 任务涉及 | 要填的范围 |
| --- | --- |
| 钱包 / 账本 / 定价 / 汇率 / 支付 / 幂等 / 状态机 | **全部章节** |
| 用量摄取 / 认证与会话（集成侧与平台侧）/ Webhook | §1–§7（**§6 不可省**） |
| 前端 / 文档 / CI / 脚本 | 不走本闸门 |
| 既不碰钱、也不碰用量摄取 / 认证与会话 / Webhook 的后端改动 | 不走本闸门 |

本任务档位：`全部`。只读，不写账本、不动余额、不改表结构，但主题是钱包账本（第一行），而且第一次把账本给**客户**看（INV-7、INV-8 正面触碰），所以按全部章节填。只读带来的「不触碰」在 §3 逐条说明理由。

---

状态：`待审`（`READY_FOR_REVIEW`）
负责人：Claude
**设计版本**：`v1`
对应需求：spec §67「Customer Dashboard」（Wallet Balance、Current account status、recent transactions；「Do NOT show internal Acuven costs.」）、§70「Customer Wallet Page」（Current Balance、Transaction History；「Ledger visible to Customer includes relevant customer-facing transaction descriptions.」）、§90（`GET /api/v1/customer/wallet`、`/wallet/transactions`；租户只取自会话）、§127 Phase 4「Customer dashboard」「Wallet」、`REQ-PRIV-001`（INV-8）、`REQ-PRIV-002`（客户侧不得暴露成本与毛利）
前置设计：#368 v12（客户门户认证：`require_customer`、`CustomerPrincipal`、只读契约），#351 v2（管理端钱包流水：序号游标与 `list_transactions_for_tenant`）
目标 PR：待开（批准后按 §8 拆三个任务登记；`docs/TODO.md`「后续计划」里的「Phase 4 客户仪表盘与钱包页」即本闸门）

## 1. 目标与边界

- **要解决的问题**：客户门户（#368，AIH-TASK-089..098 已登记）登录后只有 `/customer/me` 的四个账户字段，客户看不到自己的余额，也看不到余额是怎么扣的。spec §67、§70 要求门户首页显示余额、账户状态与最近交易，并有一个钱包页看完整流水。#351 的管理端流水接口明确「不给客户侧看，Phase 4 另定客户可见字段与描述」（#351 §1），#368 也把「门户的业务页面（仪表盘、钱包、充值、收据）」划为另开任务（#368 §1）。本设计就是那次「另定」。
- **可观察的完成标准**：
  1. `GET /api/v1/customer/wallet` 返回登录客户自己租户的币种、余额、计费模式、计费状态，以及 AI 服务现在是否可用及原因（恰好 6 个键）。
  2. `GET /api/v1/customer/wallet/transactions` 返回登录客户自己租户的账本行，最新在前，序号游标分页；每行恰好 7 个键：`id`、`wallet_sequence`、`transaction_type`、`amount`、`balance_after`、`event_id`、`created_at`。
  3. 客户流水**不含**：库里的 `description`（管理员写的调账原因）、调账的幂等键、操作者、用量事件的成本 / 汇率 / 参考价、`metadata_json`、`balance_before`、任何内部 id。AI 用量行带接入方上报的 `event_id`，其余行 `event_id = null`。
  4. 两个接口在账户 `PENDING_ACTIVATION`、`ENABLED`、`DISABLED`、`CLOSED` 下都可读（#368 只读契约：停用后仍能看账）。
  5. 门户有一个钱包页（余额卡 + 流水表，「Older」翻页），门户首页有余额卡与最近 5 条流水、链到钱包页；页面不请求、不显示任何成本、毛利或内部字段。
- **明确不做什么**（Kelvin 2026-10-23 逐项确认）：
  - **不显示库里的 `description`**：调账原因是写给内部看的，可能含内部备注。客户看到的说明是前端按 `transaction_type` 显示的固定文案（spec §70 的「customer-facing transaction descriptions」）。将来要给客户看具体说明，在调账时另加「客户可见说明」字段，另开任务。
  - **不做最小恢复额**（spec §7 第 10 条、§42、ADR-0010：欠款 + 可配置缓冲）：缓冲的配置项属于「充值 UI 与充值入账」的设计闸门（`docs/TODO.md` Phase 4「充值 UI」下已写明），没有充值入口时显示一个算不准的数字没有用。本设计只显示「余额不足、AI 服务已暂停」。
  - **不做低余额提示**（spec §48、§67）：阈值的配置入口是「低余额阈值配置（需设计闸门）」那一项；`tenants.low_balance_threshold` 现在没有任何入口能设置，也刻意不出现在任何响应里。
  - **不做本月用量、请求数、对话数、用量图**（spec §67）：月份边界与时区属于 Phase 5 / 7（对账单的 T+1 cut-off），用量明细是 Phase 5（spec §128）。
  - 不做充值按钮（充值属于后续支付闸门）、不做导出、不做按时间段或类型筛选、不给客户 `total`。
  - 不写账本、不改余额、不改任何状态，不改表结构、不加索引（没有迁移）。
- **现有行为与问题证据**：
  - `app/repositories/wallet.py:452` `list_transactions_for_tenant(session, tenant_id, *, limit, before_sequence, reference_type)` 已按租户、序号游标倒序取行，返回 `LedgerRow`（12 个字段，含 `description`、`created_by_email`、`reference_id`、`usage_event_public_id`）。管理端 `app/api/admin_customers.py:266` 是它唯一的接口调用方，响应 schema `app/schemas/wallet_transactions.py` 是 12 键白名单。
  - 用量扣费行 `amount = -billable_cost`（客户价格，不是成本，`app/services/usage_billing.py:387`），`reference_id = event.event_id`（接入方上报，或 AI 模块用 `new_ulid()` 生成，`app/ai_api/client_rules.py:92`）。
  - `INTERNAL_METERED_ONLY` 租户不产生 `AI_USAGE` 账本行，余额通常为 0、`billing_status = SUSPENDED`，但 AI 服务照常可用（`app/models/tenancy.py` `compose_effective_status`）。只看 `billing_status` 会让这类租户的页面误报「已暂停」。
  - 客户鉴权依赖 `require_customer` 与 `app/api/customer.py` 由 AIH-TASK-093 交付（尚未合并），本设计的接口任务依赖它。

## 2. 设计概要

```text
GET（客户访问令牌）→ 查询参数校验（FastAPI，处理函数之前，422）→ require_customer（第一条语句：401 / 403 CUSTOMER_REQUIRED）
→ tenant_id = CustomerPrincipal.tenant_id（取自库里的 user 行，不取自令牌与请求）
→ 一条 SELECT → 白名单投影 → 200
（无状态变化、无财务效果、无出站事件、无审计）
```

### 接口一：`GET /api/v1/customer/wallet`

无查询参数（未知参数忽略，与现有列表接口一致）。成功 200，`data`：

```json
{
  "currency": "MYR",
  "balance": "12.45880000",
  "billing_mode": "PREPAID",
  "billing_status": "ACTIVE",
  "ai_service": "ALLOW_AI",
  "ai_service_reason": null
}
```

| 字段 | 说明 |
| --- | --- |
| `currency` | 钱包币种，V1 恒为 `MYR` |
| `balance` | 恰好 8 位小数的有符号十进制字符串（`money_text`），原样来自 `wallets.balance`，可为负 |
| `billing_mode` | `PREPAID` / `INTERNAL_METERED_ONLY`。前端只在 `PREPAID` 时显示计费状态 |
| `billing_status` | `ACTIVE` / `SUSPENDED`（余额 > 0 为 `ACTIVE`，含恰好 0 在内的 ≤ 0 为 `SUSPENDED`，spec §7 第 10 条） |
| `ai_service` | `ALLOW_AI` / `BLOCK_AI`：用现有 `compose_effective_status(account_status, billing_status, billing_mode, IntegrationStatus.ENABLED)` 在租户级算出（与 `ai_service_enabled` 同一公式，不另写一份） |
| `ai_service_reason` | `ai_service = BLOCK_AI` 时为 `ACCOUNT_CLOSED` / `ACCOUNT_DISABLED` / `ACCOUNT_PENDING_ACTIVATION` / `BALANCE_NOT_POSITIVE` 之一（判定顺序沿用公式：管理员动作在余额之前）；`ALLOW_AI` 时为 `null`。租户级以 `ENABLED` 代入项目维度，所以不会出现 `PROJECT_INTEGRATION_DISABLED`；单个项目被停用不在本接口体现（客户没有项目视图，见 §10） |

不返回：钱包 `version`、`low_balance_threshold`、`status_version`、任何内部 id。账户状态与只读标志已由 `/customer/me` 给出，不重复。

查询（一条语句，同一个一致性读快照，余额与计费状态不会来自不同时刻）：

```sql
SELECT w.currency, w.balance, t.billing_mode, t.billing_status, t.account_status
FROM tenants t JOIN wallets w ON w.tenant_id = t.id
WHERE t.id = :tenant_id
```

新增 `app/repositories/wallet.py` 的 `get_wallet_summary(session, tenant_id) -> WalletSummary | None`（只读、不加锁）。查不到（每个租户建客户时同事务建钱包、迁移 0006 回填，缺钱包是数据不一致）→ 照管理端客户详情的先例抛 `RuntimeError(WALLET_MISSING)`，走全局 500，消息只有问题码。

### 接口二：`GET /api/v1/customer/wallet/transactions`

| 查询参数 | 规则 |
| --- | --- |
| `page_size` | 1–100，默认 20；超出 422（与 #351 同一范围，约束与管理端的 `PageSize` 相同） |
| `before_sequence` | 可选，整数 1 – 9 223 372 036 854 775 807；只返回 `wallet_sequence < before_sequence` 的行；省略 = 从最新一行开始；超出 422（约束与管理端的 `BeforeSequence` 相同，上界取 `MAX_WALLET_SEQUENCE`） |

**不接受** `reference_type` 或任何其他筛选，也不接受任何租户 / 客户标识；未知参数忽略（传了 `customer_id`、`tenant_id` 也只会被忽略，租户永远取自会话）。

成功 200，`data`：

```json
{
  "items": [
    {
      "id": "00000000-0000-4000-8000-000000000001",
      "wallet_sequence": 42,
      "transaction_type": "AI_USAGE",
      "amount": "-0.04120000",
      "balance_after": "12.45880000",
      "event_id": "00000000-0000-7000-8000-000000000002",
      "created_at": "2026-10-18T08:30:00"
    }
  ],
  "page_size": 20,
  "next_before_sequence": 23
}
```

| 字段 | 说明 |
| --- | --- |
| `id` | 账本行 `public_id` |
| `wallet_sequence` | 本钱包内的序号（1 起连续），也是翻页游标 |
| `transaction_type` | spec §8 九种之一，原值。前端按它显示固定文案（如 `AI_USAGE` → 「AI usage」，`ADJUSTMENT_CREDIT` → 「Credit adjustment」） |
| `amount` / `balance_after` | 恰好 8 位小数的有符号字符串，原样来自 `DECIMAL(20,8)` 列，不经 float、不舍入 |
| `event_id` | 仅当该行 `reference_type = USAGE_EVENT` 时 = `reference_id`（接入方上报的 `event_id`，或 AI 模块代为生成的 ULID），供客户与自己系统对账；其余来源一律 `null`（调账的 `reference_id` 是管理端幂等键，属内部） |
| `created_at` | 记账时刻，UTC 不带时区（`docs/api.md`「时间」） |
| `next_before_sequence` | 与 #351 v2 完全相同的规则：取 `page_size + 1` 行，有更早的行时 = 本页最后一行的序号，否则 `null`；多取的一行不返回、不当游标 |

**没有 `total`**（与 #351 同一理由；不带筛选时总行数就是钱包 `version`，但本接口不返回它，客户不需要）。

查询：复用 `list_transactions_for_tenant(session, principal.tenant_id, limit=page_size + 1, before_sequence=…)`，`reference_type` 恒为 `None`，所以走现有 `ix_wallet_transactions_tenant_sequence` 倒序范围扫描。函数不改。它读出的 `description`、`created_by_email`、`usage_event_public_id` 在客户 schema 的投影函数里**丢弃**：客户 schema 是一个新的 7 键白名单模型（新文件 `app/schemas/customer_wallet.py`），不继承、不复用管理端的 `WalletTransactionView`，避免管理端加字段时客户端跟着出现。

### 错误（两个接口相同）

401 `TOKEN_INVALID`（无 / 坏 / 过期访问令牌，前端照常走刷新；路径不在 `/api/v1/auth/` 下）、403 `CUSTOMER_REQUIRED`（ADMIN 令牌、没有 `tenant_id` 的 CUSTOMER、非 `ACTIVE` 用户）、422 `VALIDATION_ERROR`（仅接口二；文案只列字段名、不回显值）、500（缺钱包等数据不一致，带 `request_id`）、503（数据库不可用，沿用现有处理）。**没有 404**：租户来自会话，不存在「找不到客户」；没有 403 `ACCOUNT_READ_ONLY`：两个接口都是读，用 `require_customer`，不用 `require_customer_writable`。

### 事务边界、时间、金额

- 只读：每个请求一个会话、一条 SELECT（`require_customer` 自己读用户行的那一次除外），不加锁（不用 `FOR UPDATE` / `FOR SHARE`），不提交任何写。
- 时间：只返回 `created_at`（记账时刻），排序只按 `wallet_sequence`；不涉及 `occurred_at` 与版本生效时间。
- 金额：只做 `Decimal` → 8 位小数字符串的格式化（`money_text`），不做任何运算、不换算币种、不舍入到 2 位。spec §70 示例里的 6 位小数只是示例，客户看到的是账本原值；前端按字符串原样显示，不经 `Number` / `parseFloat`。

### 前端（第二、三个任务，不在闸门范围内，写在这里只为说明接口够用）

- 钱包页 `/portal/wallet`（门户外壳 AIH-TASK-096 的导航加一项）：余额卡（余额、`PREPAID` 时的计费状态、AI 服务状态与原因的固定文案）+ 流水表（时间、类型文案、金额、余额（后）、`event_id`）；「Older」用 `next_before_sequence` 翻下一页、「Newest」回第一页。
- 门户首页（AIH-TASK-097 的 `PortalHomePage`）：加同一张余额卡和 `page_size=5` 的最近流水，链到钱包页。
- 九种 `transaction_type` 与四种 `ai_service_reason` 的文案进 `en.json`；未知值显示原值，不报错。
- 浏览器验收：用验收夹具的客户账号（AIH-TASK-094 部署后由管理员建，AIH-TASK-097 已依赖它）各加一个只读步骤。

## 3. 不变量影响矩阵

| 不变量 | 是否触碰 | 可能怎样被破坏 | 设计控制 | 验证方式 |
| --- | --- | --- | --- | --- |
| INV-1 中心故障不中断 AI | 否 | 只读客户接口，不在 AI 请求路径上；`ai_service` 只是显示，AI 模块不读它 | 每页行数有上限、全走索引 | — |
| INV-2 事件不重复扣费 | 否 | 不写账本 | 无写路径 | 测试：调用前后 `wallet_transactions`、`wallets`、`tenants`、`audit_logs`、`domain_outbox` 行数与余额不变 |
| INV-3 支付不重复入账 | 否 | 同上 | 同上 | 同上 |
| INV-4 余额只经账本变动 | 否 | 同上 | 同上 | 同上 |
| INV-5 历史账本不可变 | 是（读侧） | 返回值与库里不一致会让客户以为账本被改过 | `id`、序号、类型、金额、余额逐字取自行，金额只格式化 | 测试：每项字段逐字等于库里的行；MySQL 上 8 位小数逐字相等 |
| INV-6 事件保留版本引用 | 否 | 不读不写定价 / 汇率版本；`event_id` 只是对账用的标识 | 不 JOIN 版本表 | — |
| INV-7 客户不可见成本毛利 | **是** | ① 响应带出用量事件的 `provider_source_cost`、`estimated_provider_cost_myr`、`fx_rate_applied`、`reference_customer_price`、毛利；② 管理端 schema 以后加字段，客户端跟着出现；③ 调账原因里写了内部成本信息，经 `description` 给客户看到 | ① 查询不 JOIN 用量事件的任何成本列（复用的函数只取 `usage_events.public_id`，客户 schema 再丢弃）；② 客户 schema 是独立的 7 键 / 6 键白名单模型，不继承管理端模型；③ 不返回 `description` | 测试：用量事件的成本、汇率、参考价写成哨兵值（如 `"0.13579246"`），调账 `description` 写成哨兵串，断言两个接口的**原始响应文本**里都没有出现；键集合逐字断言恰好 7 / 6 个 |
| INV-8 租户不可互访 | **是** | ① 从请求里取租户；② 漏掉租户过滤；③ 拿别的租户的序号当游标 | ① 租户只来自 `CustomerPrincipal.tenant_id`（库里的 user 行），接口不定义任何租户参数，传了也被忽略；② 两条查询都 `WHERE tenant_id = :tenant_id`（钱包查询按 `t.id`）；③ 游标是本钱包内的序号，只用于 `<` 比较，过滤条件仍是本租户 | 测试：客户 A、B 各有账本与余额；A 的令牌带 `?customer_id=<B>&tenant_id=<B 的内部 id>` 仍只得到 A；A 用 B 的最大序号 + 1 当 `before_sequence` 只得到 A 的行；A 的任何页都不含 B 的 `id` |
| INV-9 对话内容不入库 | 否 | 不新增存储，不读 prompt / response | — | — |
| INV-10 金额用 Decimal | 是 | 序列化或前端显示经 float 丢精度 | 复用 `money_text`；前端按字符串显示 | 测试：`-0.00000001`、`999999999999.99999999`、负余额原样往返；前端测试断言 `"0.00000001"` 原样渲染 |
| INV-11 event_id 至多一次财务效果 | 否 | 不写；`event_id` 只读出显示 | — | — |
| INV-12 定稿对账单不可变 | 否 | 对账单还不存在，不读不写 | — | — |
| INV-13 状态与事件原子提交 | 否 | 没有状态变化与出站事件；余额与计费状态在同一条 SELECT 里读，不会读到「余额已变、状态未变」的组合（记账在同一事务里写两者） | 一条 JOIN 查询 | 测试：单元层断言 `get_wallet_summary` 只发一条 SELECT（照现有 SQL 计数用例的写法） |
| INV-14 队列丢失不毁持久工作 | 否 | 不用 Redis / Celery | — | — |

## 4. 状态与并发

- **没有状态变化**，所以没有跃迁表。`ai_service` 是现有公式在读时的计算结果，不落库、不改 `status_version`。
- **与记账并发**：与 #351 §4 相同——账本只插入、序号在钱包行锁内连续 +1，每页是一条 SELECT 的一致快照；翻页之间新记的行只出现在最新一端，往旧翻**不重不漏**。
- **余额卡与流水表不同步**：两个接口是两个请求，中间可能有新记账，页面上余额可能比流水表第一行的 `balance_after` 更新。这是预期行为（不是同一快照），前端不做「两者必须相等」的校验。
- **与账户状态变更并发**：管理员停用租户与客户读钱包互不阻塞（读不加锁）。读到的 `ai_service` 是那一刻的结果，下次请求就是新状态。
- **与门户账号停用并发**：`require_customer` 每次请求读库里的用户行（#368），账号被停用后的下一个请求即 403，不依赖令牌过期。
- 唯一性、幂等键、相同 ID 不同载荷、重放：不适用（只读，GET 天然可重复）。
- 重试：失败由前端重新请求，后端不重试。

## 5. 失败与恢复矩阵

| 失败点 | 对外结果 | 数据最终状态 | 是否可重试 | 恢复来源 | 告警 |
| --- | --- | --- | --- | --- | --- |
| 校验失败（`page_size`、`before_sequence`） | 422 `VALIDATION_ERROR` | 不变（没有写） | 修正参数后重试 | — | 无 |
| 未登录 / 令牌过期 | 401 `TOKEN_INVALID`，前端走刷新 | 不变 | 是（刷新后） | 刷新令牌 | 无 |
| ADMIN、无租户 CUSTOMER、账号非 `ACTIVE` | 403 `CUSTOMER_REQUIRED` | 不变 | 否 | 管理员修账号 | 无 |
| 租户 `DISABLED` / `CLOSED` | 200 照常返回（只读契约） | 不变 | — | — | 无 |
| 缺钱包（数据不一致） | 500 带 `request_id`，消息只有 `WALLET_MISSING` | 不变 | 否 | 人工修数据 | 现有 500 日志；余额核对告警是另一个闸门 |
| DB 不可用 / 查询失败 | 503 / 500（沿用全局处理） | 不变 | 是 | 数据库恢复后重查 | 现有 readyz 巡检 |
| Redis/Celery 丢失 | 无影响 | 不变 | — | — | 无 |
| 外部服务超时 | 不适用（不调外部服务） | — | — | — | — |
| 重复请求 | 同一参数、期间无新记账时结果相同 | 不变 | 是 | — | 无 |
| 前端遇到未知 `transaction_type` / `ai_service_reason` | 显示原值，不崩 | 不变 | — | 补文案 | 无 |

## 6. 数据与安全边界

- **租户过滤**：在 repository 查询里，`tenant_id` 只来自 `require_customer` 返回的 `CustomerPrincipal.tenant_id`（库里的 user 行）。两个接口都不定义任何租户、客户、钱包标识参数；spec §90「Never accept arbitrary `tenant_id`」。
- **鉴权主体**：只有 CUSTOMER（`ACTIVE`、`tenant_id` 非空）。处理函数第一条语句显式调用 `require_customer`；AIH-TASK-093 的路由枚举用例（枚举 `/api/v1/customer/*` 的处理函数）自动覆盖这两个新路由。ADMIN 不能调（管理员看客户钱包用 #351 的管理端接口）。查询参数由 FastAPI 在处理函数之前校验，非法参数在鉴权之前得到 422，文案不回显值、不泄露数据（与管理端一致）。
- **客户侧禁止返回的字段**（白名单之外一律不返回，这里列出是为了测试逐条断言）：`description`、`reference_type`、调账 / 系统 / rebill 行的 `reference_id`、`created_by` / `created_by_email`、`balance_before`、`metadata_json`、内部 `id` / `tenant_id` / `wallet_id`、用量事件的 `public_id`（管理端用量详情的 id，客户没有对应页面）、成本 / 汇率 / 参考价 / 毛利、钱包 `version`、`low_balance_threshold`、`status_version`。
- **日志、审计、异常**：沿用请求日志（方法、路径模板、状态、耗时、`request_id`）；不记录余额、金额、`event_id`、邮箱。读操作不写审计（与现有全部 GET 一致）。500 的消息只有问题码。
- **密钥**：不涉及。
- **prompt / response 与客户数据**：不处理 prompt / response。返回的都是该客户自己的账务数据；`event_id` 是客户自己系统上报（或 AI 模块代为生成）的标识。不返回任何管理员个人数据（操作者邮箱）。
- **数据保留**：不新增存储。

## 7. 测试证据计划

后端（`tests/backend/test_customer_wallet_api.py`，SQLite + MySQL 参数化，沿用 AIH-TASK-093 的客户登录夹具写法；SQL 文本断言不写死方言字面量）：

| 风险/需求 | 测试层级 | 场景 | 预期结果 |
| --- | --- | --- | --- |
| 正常路径（钱包） | API | `PREPAID` 客户余额 `12.5`、`ENABLED` | 200，恰好 6 个键；`balance = "12.50000000"`、`billing_status = ACTIVE`、`ai_service = ALLOW_AI`、`ai_service_reason = null` |
| 正常路径（流水） | API | 客户有 5 行账本：AI 用量（有事件）、调账加、调账减、系统更正、再一笔 AI 用量；`page_size=2` 连翻三页 | 序号依次 5,4 / 3,2 / 1；`next_before_sequence` 依次 4、2、`null`；每项恰好 7 个键且逐字等于库里的行；AI 用量行 `event_id` = 上报的 `event_id`，其余行 `null` |
| 空账本 | API | 新客户、余额 0、没有账本行 | 钱包：`balance = "0.00000000"`、`SUSPENDED`、`BLOCK_AI`、`BALANCE_NOT_POSITIVE`；流水：`items` 空、`next_before_sequence = null` |
| 边界值 | API | `page_size` = 0 / 101 / 非整数；`before_sequence` = 0 / -1 / 2^63 | 422 `VALIDATION_ERROR`，文案不回显值；`page_size=100`、`before_sequence=1`（空页）为 200 |
| 余额恰好为 0 与负余额 | API | 余额 `0`、`-3.2` | 都是 `SUSPENDED` + `BLOCK_AI` + `BALANCE_NOT_POSITIVE`；`balance` 分别为 `"0.00000000"`、`"-3.20000000"` |
| 原因顺序 | API | `DISABLED` 且余额为负 | `ai_service_reason = ACCOUNT_DISABLED`（管理员动作排在余额之前） |
| 内部计量租户 | API | `INTERNAL_METERED_ONLY`、余额 0、`ENABLED` | `billing_mode = INTERNAL_METERED_ONLY`、`billing_status = SUSPENDED`、`ai_service = ALLOW_AI`、`ai_service_reason = null` |
| 只读契约 | API | 租户依次为 `PENDING_ACTIVATION` / `ENABLED` / `DISABLED` / `CLOSED` | 两个接口都 200；`ai_service_reason` 依次为 `ACCOUNT_PENDING_ACTIVATION` / 按余额 / `ACCOUNT_DISABLED` / `ACCOUNT_CLOSED` |
| 并发（翻页） | MySQL | 取第一页后另一事务记两笔新账，再用 `next_before_sequence` 取第二页 | 第二页与没有新记账时逐字相同（不重不漏） |
| 单条快照 | unit | `get_wallet_summary` | 只发一条 SELECT，且不含 `FOR UPDATE` / `FOR SHARE`（归一后比较） |
| 幂等重放 | API | 同一请求连发两次 | 两次响应相同 |
| 事务中途失败 | API | repository 查询抛数据库异常 | 500 / 503 带 `request_id`；账本、钱包、租户、审计、outbox 行数不变 |
| 缺钱包 | API | 测试里删掉该租户的钱包行（SQLite） | 500，响应不含任何客户数据 |
| Redis/Celery 丢失 | — | 不使用 | 不适用 |
| 租户越权 | API | 客户 A、B 各有余额与账本；A 的令牌带 `?customer_id=<B 的 public_id>&tenant_id=<B 的内部 id>`；A 用 B 的最大序号 + 1 做 `before_sequence` | 钱包与流水都只是 A 的；A 的任何页都不含 B 的 `id`；B 的余额字符串不出现在 A 的响应里 |
| 鉴权 | API | 无令牌、坏令牌、ADMIN 令牌、门户账号被停用后旧令牌 | 401 `TOKEN_INVALID` / 401 / 403 `CUSTOMER_REQUIRED` / 403 `CUSTOMER_REQUIRED`，且不发账本查询 |
| 路由守卫 | integration | AIH-TASK-093 的 `/api/v1/customer/*` 枚举用例 | 两个新处理函数被枚举到且显式调用了 `require_customer` |
| 无成本毛利、无内部说明 | API | 用量事件的成本、汇率、参考价写成哨兵值；调账 `description` 写成哨兵串；`created_by` 指向管理员 | 两个接口的**原始响应文本**里不出现任何哨兵值、管理员邮箱、调账幂等键；键集合逐字断言 |
| 金额精度 | API（SQLite + MySQL） | 金额 `-0.00000001`、`999999999999.99999999`、负余额 | 响应字符串与库值逐字相等，8 位小数 |
| 无写入 | API | 每个用例前后 | `wallet_transactions`、`wallets`、`tenants`、`audit_logs`、`domain_outbox` 行数与余额不变 |

前端（Vitest，钱包页与首页各自的测试文件）：金额字符串原样渲染（`"0.00000001"`、负数）；`INTERNAL_METERED_ONLY` 不显示「已暂停」；`BLOCK_AI` 四种原因各有文案；未知类型显示原值；「Older」带上 `before_sequence`、最后一页没有「Older」；页面不请求 `/api/v1/admin/*`。

## 8. 迁移与上线

- **数据迁移步骤**：无。不改表、不加索引（只按序号倒序取，现有 `ix_wallet_transactions_tenant_sequence` 已覆盖；钱包按 `uq_wallets_tenant_id` 点查）。
- **锁表与性能影响**：无 DDL。每次请求一次主键 / 唯一键点查（钱包）或一次索引范围扫描取 ≤ 101 行（流水），外加 #351 已有的每行两次唯一键点查。
- **兼容窗口**：两个新接口是新增的；前端在后端部署之后才上（同一次合并同时部署 api 与 frontend，且前端任务排在接口任务之后）。
- **部署顺序与拆分**（批准后登记为三个任务，`depends_on` 串起来，每个都能单独过检查与 CI）：
  1. 接口：`get_wallet_summary` + `app/schemas/customer_wallet.py` + `app/api/customer.py` 两个路由 + `tests/backend/test_customer_wallet_api.py` + `docs/api.md`（依赖 AIH-TASK-093：`require_customer` 与 `app/api/customer.py` 由它交付）。
  2. 前端钱包页：API 封装、余额卡组件、钱包页、门户导航与路由、文案、测试、验收脚本一个只读步骤（依赖 1 与 AIH-TASK-097）。
  3. 前端门户首页：余额卡与最近 5 条流水、文案、测试、验收脚本一个只读步骤（依赖 2）。
- **回滚或前滚方式**：回滚到上一个镜像即可（无数据变化、无迁移）。
- **部分部署时的行为**：只部署 1 → 接口可用、页面没有入口；只部署到 2 → 有钱包页、首页还没有余额卡。都不影响现有功能。
- **监控指标和告警负责人**：不新增指标；现有请求日志可以看耗时与 500；Kelvin 负责。

## 9. 方案取舍

| 方案 | 优点 | 风险 | 未采用原因 |
| --- | --- | --- | --- |
| **采用**：两个只读接口、独立的客户白名单 schema、复用 `list_transactions_for_tenant` 与现有公式、不返回 `description` | 不改表、不改账本函数；客户字段集与管理端彻底分开；与 #351 的游标语义完全一致 | 复用的查询多做两次 LEFT JOIN（取了不用的 `description`、邮箱、用量事件 id） | — |
| 备选 A：客户流水另写一个只选 7 列的查询 | 少两次每行点查，读出的列就是返回的列 | 多一份几乎相同的查询要维护；两次唯一键点查每页至多 202 次，代价可忽略 | 白名单在 schema 层已保证不外泄；省下的开销不值得第二份查询（以后流水查询要改时只改一处） |
| 备选 B：返回库里的 `description` | 调账能带具体说明（spec §70「customer-facing descriptions」字面最贴） | `description` 是管理员写给内部看的原因，可能含内部备注甚至成本信息（INV-7）；一旦给客户看，所有既有调账的原因都追溯性地对外了 | Kelvin 2026-10-23 选「只显示类型文案」；需要对客说明时另加专用字段 |
| 备选 C：返回 `reference_type` 并支持按来源筛选 | 与管理端一致，客户能只看调账 | 多一个参数和一组错误；客户现在只有用量和调账两种来源，没有充值时筛选价值小 | 等 Phase 4 充值上线、来源变多时再加（复用现有索引，不需要迁移） |
| 备选 D：一个 `/customer/dashboard` 聚合接口（余额 + 最近流水 + 本月用量） | 首页一次请求 | 「本月」要先定月份边界与时区（Phase 5 / 7）；与钱包接口字段重复 | 首页直接调两个接口（`page_size=5`）；本月用量归 Phase 5 |
| 备选 E：只返回 `billing_status`，不返回 `ai_service` | 字段少 | 内部计量租户余额 0 → `SUSPENDED`，页面会误报「AI 已暂停」；管理员停用时只看余额也解释不了为什么不能用 | 复用 `compose_effective_status` 一处公式，客户看到的「能不能用、为什么」与集成方 `effective-status` 一致 |
| 备选 F：金额按 spec §70 示例舍入到 6 位或 2 位 | 显示更短 | 舍入位置是财务规则，余额与流水各自舍入后会出现「加起来对不上」 | 返回账本原值，显示层原样；要短显示时另议并写进 spec |

## 10. 未决问题与假设

- **未决问题**：无阻断项。
- **已拍板的范围取舍**（Kelvin 2026-10-23）：不显示库里的 `description`、AI 用量行显示 `event_id`、最小恢复额 / 低余额提示 / 本月用量这次都不做。
- **尚未验证的假设**：
  1. AIH-TASK-093 按 #368 v12 交付 `require_customer`、`CustomerPrincipal.tenant_id` 与 `app/api/customer.py`（前缀 `/api/v1/customer`）以及枚举 `/api/v1/customer/*` 的路由守卫用例。若 093 的实际形状不同，接口任务停下并在 PR 里说明，不自行补依赖。
  2. 每个租户恰好一个钱包（`uq_wallets_tenant_id`，建客户同事务建钱包、迁移 0006 回填）——已核对 `app/services/customers.py` 与 `app/models/wallet.py`。
- **项目级停用不在 `ai_service` 里体现**：客户门户现在没有项目视图，单个项目的集成被管理员停用时，钱包卡仍显示 `ALLOW_AI`。项目视图是以后的事；届时按项目调同一公式。这不是财务或安全问题（只是显示不全），不阻断。
- **需要谁拍板**：无新增；上面三条范围取舍已拍板。
- **如果假设错误，具体后果**：假设 1 错 → 接口任务在开工时停下（契约要求先核对依赖），不会出现绕过 `require_customer` 的实现。

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

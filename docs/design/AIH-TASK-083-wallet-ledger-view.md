# AIH-TASK-083 / 084 / 085 设计：管理端查看钱包流水（已批准 v2）

> **来源**：设计闸门 Issue #351。本文件是 `APPROVED: design v2` 那一版正文的**逐字副本**，审查者为 Claude Code（Codex 额度不可用期间临时替代）。
> 放进仓库，是因为 OpenClaw Worker 在沙箱里不联网、读不到 GitHub Issue。
> **实现以本文件为准**；与 Issue 不一致时，以 Issue #351 上被批准的 v2 为准。设计要改，就回到 Issue 升版本、重新过闸门，不要直接改本文件。
> §8 的三个实现任务登记为 `AIH-TASK-083`（迁移加索引）、`AIH-TASK-084`（仓储与接口）、`AIH-TASK-085`（前端面板），按这个顺序依赖。

---

## 0. 准入判定

| 任务涉及 | 要填的范围 |
| --- | --- |
| 钱包 / 账本 / 定价 / 汇率 / 支付 / 幂等 / 状态机 | **全部章节** |
| 用量摄取 / 认证与会话（集成侧与平台侧）/ Webhook | §1–§7（**§6 不可省**） |
| 前端 / 文档 / CI / 脚本 | 不走本闸门 |
| 既不碰钱、也不碰用量摄取 / 认证与会话 / Webhook 的后端改动 | 不走本闸门 |

本任务档位：`全部`。只读，不写账本、不动余额，但主题是钱包账本（第一行），还要给 `wallet_transactions` 加一个索引（迁移），所以按全部章节填。只读带来的「不触碰」在 §3 逐条说明理由。

---

状态：`待审`（`READY_FOR_REVIEW`）
负责人：Claude
**设计版本**：`v2`
对应需求：spec §60「Admin sees: Current balance / Ledger / Top-ups / Usage charges / Adjustments / Rebill corrections」、§78（`wallet_transactions`）、§108（分页：大表优先用游标）、`REQ-PRIV-002`（客户侧不得暴露成本与毛利，本接口只给管理员）
目标 PR：待开（批准后按 §8 拆三个任务登记；`docs/TODO.md`「后续计划」里的「管理端查看钱包流水（需设计闸门）」与「管理端前端：低余额阈值与钱包流水」的流水部分即本闸门）

## 1. 目标与边界

- **要解决的问题**：管理员现在只能看到客户的当前余额（客户详情的 `wallet`），看不到余额是怎么来的。spec §60 要求管理端能看账本，并按充值、用量扣费、调账、rebill 更正分开看。AIH-TASK-010 设计 §1 已把它「另开任务」（设计第 40 行与第 282 行），至今没有归属。排查余额问题（用户问「为什么我的钱少了」、或者以后余额核对告警响了）时，现在只能直接查生产库。
- **可观察的完成标准**：
  1. `GET /api/v1/admin/customers/{customer_id}/wallet/transactions` 返回该客户的账本行，**最新在前**，游标分页；可选按来源（`reference_type`）筛选。
  2. 每行带金额、前后余额、序号、类型、来源与来源 id、原因、操作者邮箱、时间；AI 用量行另带对应用量事件的 `id`，管理端可以跳到用量事件详情。
  3. 管理端客户详情页有一个「钱包流水」面板，翻页、按来源筛选、AI 用量行可点进用量事件详情。
  4. 一个客户有上百万行 AI 用量时，任意一页（含带筛选）的查询都是索引上的范围扫描，不随总行数变慢。
- **明确不做什么**：
  - 不写账本、不改余额、不改任何状态（没有写路径）。
  - 不给客户侧看（spec §70 客户钱包页属 Phase 4，那时另定客户可见字段与描述）。
  - 不做导出（spec §73 归 Phase 5 / 7）、不做按时间段筛选、不做跨客户的流水总览。
  - 不返回 `metadata_json`：现在没有任何写入方会填它（`post_transaction` 的三个调用方都不传 `metadata`），等真有内容时再定哪些键可以给管理员看。
  - 不做余额核对（「余额不一致的定时核对与告警」是另一个闸门）。
- **现有行为与问题证据**：`app/repositories/wallet.py:428` 已有 `list_transactions_for_tenant`（按 `wallet_sequence` 倒序、`before_sequence` 键集分页、上限 200），但没有接口调用它，也不支持筛选；`ix_wallet_transactions_tenant_sequence (tenant_id, wallet_sequence)` 是唯一可用的索引。

## 2. 设计概要

```text
GET（管理员令牌）→ 查询参数校验（FastAPI，鉴权之前，422）→ require_admin（第一条语句，401 / 403）
→ 按 public_id 找租户（不存在 404）→ 一条 SELECT：wallet_transactions 按租户（+ 来源）+ 序号游标倒序取 page_size + 1 行，
  LEFT JOIN usage_events（取用量事件 public_id）、LEFT JOIN users（取操作者邮箱）→ 组装响应
（无状态变化、无财务效果、无出站事件、无审计）
```

### 接口

`GET /api/v1/admin/customers/{customer_id}/wallet/transactions`

| 查询参数 | 规则 |
| --- | --- |
| `page_size` | 1–100，默认 20；超出 422（与 spec §108 / `docs/api.md`「分页」同一范围） |
| `before_sequence` | 可选，整数 1 – 9 223 372 036 854 775 807；只返回 `wallet_sequence < before_sequence` 的行。省略 = 从最新一行开始。超出 422 |
| `reference_type` | 可选，`USAGE_EVENT` / `PAYMENT` / `ADMIN_ADJUSTMENT` / `REBILL` / `SYSTEM` 之一，大小写敏感；其余 422 |

未知查询参数不报错（与现有列表接口一致）。

`reference_type` 与 spec §60 的分类一一对应：`PAYMENT` = 充值，`USAGE_EVENT` = 用量扣费，`ADMIN_ADJUSTMENT` = 调账（含 `BONUS`、`REFUND_ADJUSTMENT`），`REBILL` = rebill 更正，`SYSTEM` = 系统更正。用来源而不是 `transaction_type` 筛选，是因为一个来源可能对应多个类型（调账有四种），按单个类型筛就得支持多值，见 §9。

成功 200，`data`：

```json
{
  "items": [
    {
      "id": "00000000-0000-4000-8000-000000000001",
      "wallet_sequence": 42,
      "transaction_type": "AI_USAGE",
      "amount": "-0.04120000",
      "balance_before": "12.50000000",
      "balance_after": "12.45880000",
      "reference_type": "USAGE_EVENT",
      "reference_id": "00000000-0000-7000-8000-000000000002",
      "usage_event_id": "00000000-0000-4000-8000-000000000003",
      "description": null,
      "created_by_email": null,
      "created_at": "2026-10-18T08:30:00"
    }
  ],
  "page_size": 20,
  "next_before_sequence": 23
}
```

| 字段 | 说明 |
| --- | --- |
| `id` | 账本行的 `public_id`（与调账响应的 `id` 同一个值） |
| `amount` / `balance_before` / `balance_after` | 恰好 8 位小数的十进制字符串，有符号，原样来自 `DECIMAL(20,8)` 列，不经 float |
| `reference_id` | 库里的原值：用量扣费是上报的 `event_id`，调账是幂等键，充值以后是支付的标识 |
| `usage_event_id` | 只有 `USAGE_EVENT` 来源有：经 `usage_events.wallet_transaction_id`（唯一）反查到的用量事件 `public_id`，可直接用于 `GET /api/v1/admin/usage-events/{usage_event_id}`。其他来源 `null` |
| `description` | 库里的原因（调账、系统更正必有，AI 用量为空） |
| `created_by_email` | 操作者（`created_by` → `users.email`）；系统写入的行是 `null`。审计接口的 `actor_email` 已经对管理员给出同一信息 |
| `created_at` | UTC，不带时区（`docs/api.md`「时间」） |
| `next_before_sequence` | 还有更早的行时 = 本页最后一行（`items` 的最后一项）的 `wallet_sequence`，原样作为下一页的 `before_sequence`（下一页只取比它小的行）；没有更多时 `null`。多取的第 `page_size + 1` 行只用来判断有没有下一页，不返回、也不当游标。带筛选时序号不连续，所以游标只能取本页最后一行，不能用「最后一行 − 1」之类推算 |

**没有 `total`**：带筛选时总数要额外 COUNT 扫全部匹配行，正是游标分页要避免的。不带筛选时总行数等于钱包的 `version`（客户详情已返回），前端需要时自己取。

错误：401（无 / 坏令牌）、403 `ADMIN_REQUIRED`、404 `CUSTOMER_NOT_FOUND`、422 `VALIDATION_ERROR`（文案只列字段名、不回显值）、500、503（数据库不可用，沿用现有处理）。

### 查询

```sql
SELECT wt.*, ue.public_id AS usage_event_public_id, u.email AS created_by_email
FROM wallet_transactions wt
LEFT JOIN usage_events ue ON ue.wallet_transaction_id = wt.id
LEFT JOIN users u ON u.id = wt.created_by
WHERE wt.tenant_id = :tenant_id
  [AND wt.reference_type = :reference_type]
  [AND wt.wallet_sequence < :before_sequence]
ORDER BY wt.wallet_sequence DESC
LIMIT :page_size + 1
```

- 不带筛选：走现有 `ix_wallet_transactions_tenant_sequence (tenant_id, wallet_sequence)` 倒序范围扫描。
- 带 `reference_type`：走**新索引** `ix_wallet_transactions_tenant_reference_sequence (tenant_id, reference_type, wallet_sequence)`，同样是等值前缀 + 范围 + 有序，取 `page_size + 1` 行即停。没有它，一个有百万行 AI 用量、只有几笔调账的客户筛「调账」时要倒着扫完几乎全部行才凑满一页。
- 两个 JOIN 都按唯一键（`uq_usage_events_wallet_transaction_id`、`users` 主键）各查一行，每页至多 `2 × (page_size + 1)` 次索引点查。
- `list_transactions_for_tenant` 改为返回带这两个 JOIN 字段的结果并接受 `reference_type`；它现有的调用方只有测试。页上限仍取调用方的 `page_size`（≤ 100），函数内 200 的硬上限保留。

### 数据库

- 新迁移（`0022_tax_policy` 之后的下一个编号，登记时定）：只加索引 `ix_wallet_transactions_tenant_reference_sequence (tenant_id, reference_type, wallet_sequence)`；downgrade 删它。不改列、不改约束、不改触发器、不动数据。
- 模型 `WalletTransaction.__table_args__` 同步加这个 `Index`，`tests/backend/test_migrations.py` 的模型与迁移比对照常覆盖。

### 事务边界、时间、金额

- 只读：一个会话里两条 SELECT（找租户、取一页），不加锁（不用 `FOR UPDATE` / `FOR SHARE`），不提交任何写。
- 时间：`created_at` 是记账时刻（UTC，原样返回库里的值）；本接口不涉及 `occurred_at`，排序只按 `wallet_sequence`。
- 金额：只做 Decimal → 8 位小数字符串的格式化，不做任何运算、不换算币种（钱包币种在客户详情里）。

### 前端（第三个任务，不在闸门范围内，写在这里只为说明接口够用）

客户详情页加「Wallet transactions」面板：表格列 = 时间、序号、类型、金额、余额（后）、来源、原因、操作者；来源下拉筛选；「Older」按钮用 `next_before_sequence` 翻下一页、「Newest」回第一页；AI 用量行链接到用量事件详情；金额按字符串原样显示。

## 3. 不变量影响矩阵

| 不变量 | 是否触碰 | 可能怎样被破坏 | 设计控制 | 验证方式 |
| --- | --- | --- | --- | --- |
| INV-1 中心故障不中断 AI | 否 | 只读管理端接口，不在 AI 请求路径上；查询慢也只占管理端请求 | 每页取行数有上限、全走索引 | — |
| INV-2 事件不重复扣费 | 否 | 不写账本 | 无写路径 | 测试：调用前后 `wallet_transactions`、`wallets`、`tenants`、`audit_logs`、`domain_outbox` 行数与余额不变 |
| INV-3 支付不重复入账 | 否 | 同上 | 同上 | 同上 |
| INV-4 余额只经账本变动 | 否 | 同上 | 同上 | 同上 |
| INV-5 历史账本不可变 | 是（读侧）| 返回的行与库里不一致（例如格式化时改了值）会让管理员以为账本变了 | 金额原样格式化；`id`、序号、类型、来源逐字取自行 | 测试：响应逐字段等于库里的行；MySQL 上金额 8 位小数逐字相等 |
| INV-6 事件保留版本引用 | 否 | 不读不写定价版本；`usage_event_id` 只是指向事件的链接 | — | — |
| INV-7 客户不可见成本毛利 | 是 | 返回成本、毛利，或被客户角色调到 | 只给 ADMIN（`require_admin`）；响应不含 `metadata_json`，也不 JOIN 用量事件的成本列；`usage_event_id` 只是 id | 测试：CUSTOMER 令牌 403；响应键集合恰好是 §2 列出的 12 个 |
| INV-8 租户不可互访 | 是 | 漏掉租户过滤会把别的客户的账本混进来 | `WHERE wt.tenant_id = :tenant_id`，租户只来自路径；游标是序号，不是行 id，不能借它跨租户 | 测试：两个客户各有账本，A 的列表不含 B 的任何行；对 B 的 `before_sequence` 值翻 A 的页只返回 A 的行 |
| INV-9 对话内容不入库 | 否 | 不新增存储；不返回 `metadata_json`；`description` 是管理员写的原因 | — | — |
| INV-10 金额用 Decimal | 是 | 序列化时经 float 丢精度 | schema 字段是 `Decimal` 并按现有调账响应的写法序列化成字符串 | 测试：`-0.00000001`、`999999999999.99999999`（`DECIMAL(20,8)` 的上界）原样往返 |
| INV-11 event_id 至多一次财务效果 | 否 | 不写 | — | — |
| INV-12 定稿对账单不可变 | 否 | 对账单还不存在；本接口不读不写它 | — | — |
| INV-13 状态与事件原子提交 | 否 | 没有状态变化与出站事件 | — | — |
| INV-14 队列丢失不毁持久工作 | 否 | 不用 Redis / Celery | — | — |

## 4. 状态与并发

- **没有状态变化**，所以没有跃迁表。
- **与记账并发**：账本只插入（INV-5，触发器拒绝 UPDATE / DELETE），`wallet_sequence` 在钱包行锁内严格 +1 连续递增，且一个租户恰好一个钱包（`uq_wallets_tenant_id`）。所以：
  - 每页是一条 SELECT 的一致快照（InnoDB 一致性读），页内不会出现半笔记账。
  - 翻页之间新记的行序号都大于第一页的最大序号，只会出现在「最新」那一端；按 `before_sequence` 往旧翻的页**不重不漏**。这是选序号游标而不是 offset 的主要理由之一（offset 在新行插入后会让下一页重复上一页的最后几行）。
- **不加锁**：不阻塞记账；记账也不阻塞本查询（一致性读不等行锁）。
- 唯一性、幂等键、重放：不适用（只读、GET 天然可重复）。
- 重试：失败由调用方（前端）重新请求，后端不重试。

## 5. 失败与恢复矩阵

| 失败点 | 对外结果 | 数据最终状态 | 是否可重试 | 恢复来源 | 告警 |
| --- | --- | --- | --- | --- | --- |
| 校验失败（`page_size`、`before_sequence`、`reference_type`） | 422 `VALIDATION_ERROR` | 不变（没有写） | 修正参数后重试 | — | 无 |
| 未登录 / 非管理员 | 401 / 403 | 不变 | — | — | 无 |
| 客户不存在 | 404 `CUSTOMER_NOT_FOUND` | 不变 | — | — | 无 |
| DB 不可用 / 查询失败 | 503 / 500（沿用全局处理，带 `request_id`） | 不变 | 是 | 数据库恢复后重查 | 现有 readyz 巡检 |
| Redis/Celery 丢失 | 无影响 | 不变 | — | — | 无 |
| 外部服务超时 | 不适用（不调外部服务） | — | — | — | — |
| 重复请求 | 同一参数、期间无新记账时结果相同 | 不变 | 是 | — | 无 |
| 用量事件反查不到（`ue` 为 NULL） | 该行 `usage_event_id = null`，其余照常 | 不变 | — | 这是数据缺陷信号（`USAGE_EVENT` 账本行理应有事件）；本接口只如实显示，不修 | 不新增（余额核对任务另议） |
| 迁移部分失败（加索引中途失败） | 部署失败、按现有流程回滚 | MySQL 加索引是单条 DDL，失败即不存在 | 修正后重新部署 | 现有部署回滚 | 部署通知 |

## 6. 数据与安全边界

- **租户过滤**：在 repository 查询里，`tenant_id` 来自路径客户的 `public_id` 查得的内部 id；查询参数里没有任何 id 能改变租户。
- **鉴权主体**：只有 ADMIN。处理函数第一条语句 `require_admin`；接口加进 `EXPECTED_ADMIN_ROUTES`。查询参数由 FastAPI 在处理函数之前校验，所以非法参数在鉴权之前得到 422（与用量事件列表一致，422 不回显值、不泄露数据）。
- **客户侧禁止的字段**：本接口不是客户侧。仍然不返回 `metadata_json`、不返回内部 `id` / `tenant_id` / `wallet_id` / `created_by`（用户内部 id）；`usage_event_id` 与 `id` 都是 `public_id`。
- **日志、审计、异常**：沿用请求日志（方法、路径模板、状态、耗时、`request_id`）；不记录金额、原因、`reference_id`、邮箱。读操作不写审计（与现有全部管理端 GET 一致）。
- **密钥**：不涉及。
- **prompt / response 与客户数据**：不处理 prompt / response。`created_by_email` 是平台管理员的邮箱，不是客户的个人数据；客户的联系人、电话、邮箱一概不返回。`description` 可能含管理员写的业务说明（调账时已提示不写个人数据，`docs/api.md` 调账一节）。
- **数据保留**：不新增存储；索引随表保留。

## 7. 测试证据计划

| 风险/需求 | 测试层级 | 场景 | 预期结果 |
| --- | --- | --- | --- |
| 正常路径 | API（SQLite + MySQL） | 客户有 5 行账本（充值以外的四种来源各至少一行，用量行有对应事件、调账行有操作者），`page_size=2` 连翻三页 | 依次得到序号 5,4 / 3,2 / 1；`next_before_sequence` 依次 4、2、`null`（每页最后一行的序号）；每行字段逐字等于库里的行；用量行 `usage_event_id` 等于事件 `public_id`，调账行 `created_by_email` 等于管理员邮箱，系统行两者为 `null` |
| 空账本 | API | 新建客户、没有任何账本行 | `items` 为空、`next_before_sequence` 为 `null` |
| 筛选 | API | 同一客户交错写入 `USAGE_EVENT` 与 `ADMIN_ADJUSTMENT`，`reference_type=ADMIN_ADJUSTMENT&page_size=1` 翻到底 | 只出现调账行，按序号倒序、不重不漏 |
| 边界值 | API | `page_size` = 0 / 101 / 非整数；`before_sequence` = 0 / -1 / 2^63；`reference_type` = 小写 / 未知值 | 422 `VALIDATION_ERROR`，文案不回显值；`page_size=100`、`before_sequence=1`（空页）为 200 |
| 并发 | MySQL | 取第一页后，另一事务记两笔新账，再用 `next_before_sequence` 取第二页 | 第二页与没有新记账时的第二页逐字相同（不重不漏）；第一页重取时新行在最前 |
| 幂等重放 | API | 同一请求连发两次 | 两次响应相同 |
| 事务中途失败 | API | repository 查询抛数据库异常 | 500 / 503 带 `request_id`；账本、钱包、审计、outbox 行数不变 |
| Redis/Celery 丢失 | — | 不使用 | 不适用 |
| 租户越权 | API | 客户 A、B 各有账本；以 A 的路径带 B 的序号做 `before_sequence` | 只返回 A 的行；A 的任何页都不含 B 的 `id` |
| 鉴权 | API | 无令牌、CUSTOMER 令牌、坏令牌 | 401 / 403 / 401，且不访问账本（不发查询的断言照现有路由枚举用例的写法） |
| 金额精度 | API（SQLite + MySQL） | 金额 `-0.00000001`、`999999999999.99999999`、余额为负 | 响应字符串与库值逐字相等，8 位小数 |
| 无成本毛利 | API | 用量事件带成本与毛利快照 | 响应每项的键集合恰好是 §2 的 12 个，不含任何成本、毛利、`metadata_json` |
| 索引存在且可用 | 迁移（MySQL） | 升级到 head 后 `SHOW INDEX`；对带 `reference_type` 的查询 `EXPLAIN` | 新索引存在、列顺序为 `tenant_id, reference_type, wallet_sequence`；`EXPLAIN` 的 `possible_keys` 含它（不断言优化器最终选择，避免小表时不稳定） |
| 迁移往返 | 迁移（SQLite + MySQL） | upgrade → downgrade → upgrade | 索引随之建、删、再建；其他表结构不变；模型与迁移比对通过 |

SQL 文本断言遵守仓库约定：不写死方言字面量（`JOIN` / `INNER JOIN`、`LEFT OUTER JOIN` 等先归一）。

## 8. 迁移与上线

- **数据迁移步骤**：无数据迁移；只加一个二级索引。
- **锁表与性能影响**：MySQL 8 的 `ADD INDEX` 默认在线（INPLACE、不阻塞 DML）。生产上 `wallet_transactions` 现在只有夹具与内部租户的少量行，构建瞬时完成；以后行数大了再加同样不锁写，只是更久，所以趁现在加。
- **兼容窗口**：新接口是新增的，旧前端不调用；新前端在后端部署之后才上（同一次合并同时部署 api 与 frontend，且前端任务排在接口任务之后）。
- **部署顺序与拆分**（批准后登记为三个任务，`depends_on` 串起来，每个都能单独过检查与 CI）：
  1. 数据库：迁移加索引 + 模型 `Index` + 迁移测试 + `docs/database-schema.md`（不加接口）。
  2. 接口：repository 查询（筛选、两个 JOIN、游标）+ schema + 路由 + 测试 + `docs/api.md`（依赖 1）。
  3. 前端：客户详情页的流水面板 + 验收脚本一个只读步骤（依赖 2，不走闸门）。
- **回滚或前滚方式**：接口与前端回滚到上一个镜像即可（无数据变化）；迁移 downgrade 只删索引。
- **部分部署时的行为**：只部署了 1 → 多一个没人用的索引；只部署到 2 → 接口可用、页面没有入口。都不影响现有功能。
- **监控指标和告警负责人**：不新增指标。现有请求日志可以看耗时；Kelvin 负责。

## 9. 方案取舍

| 方案 | 优点 | 风险 | 未采用原因 |
| --- | --- | --- | --- |
| **采用**：序号游标（`before_sequence`）+ 按来源单值筛选 + 新索引 `(tenant_id, reference_type, wallet_sequence)` | 任意深度、带不带筛选都是索引范围扫描，取 `page_size + 1` 行即停；翻页不重不漏；复用已有的 `list_transactions_for_tenant` 思路 | 与现有列表接口的 `page` / `total` 形状不同，前端要另写一套翻页；没有总数 | — |
| 备选 A：沿用 `page` / `page_size` / `total` | 与其他列表接口一致，前端现成 | 每个 AI 请求一行账本，大客户的账本是 spec §108 说的「very large」表：`OFFSET` 要先扫过前面所有行（第 10 000 页 × 100 = 跳过近百万行），`total` 每次要 COUNT 全部匹配行；新行插入时 offset 翻页会重复 | spec §108 对大表明确要求优先用游标；序号连续且只增，天然适合当游标 |
| 备选 B：按 `transaction_type` 多值筛选 | 粒度更细 | 多值 `IN` 加 `ORDER BY wallet_sequence` 时 MySQL 不能在 `(tenant_id, transaction_type, wallet_sequence)` 上按序合并多个范围，会对全部匹配行排序；单值又凑不出「调账」这一类（四个类型） | `reference_type` 与 spec §60 的分类一一对应，单值就够，索引能按序取 |
| 备选 C：不加索引，带筛选时靠现有索引残余过滤 | 少一个迁移 | 稀有来源（调账、系统更正）在百万行用量里筛一页要倒着扫几乎全部行 | 这正是要避免的慢查询；现在表小、加索引零代价，以后表大了再加更慢 |
| 备选 D：返回 `metadata_json` | 信息更全 | 现在没有写入方填它，内容契约不存在；一旦有人往里写东西就自动暴露给前端 | 等有内容时再按键白名单加 |

## 10. 未决问题与假设

- **未决问题**：无阻断项。
- **尚未验证的假设**：
  1. MySQL 优化器对带 `reference_type` 的查询会选新索引。依据：等值前缀 `(tenant_id, reference_type)` + 有序列 `wallet_sequence` + `LIMIT`，是最典型的可用形状；测试只断言 `possible_keys`，若生产上真没选中，后果是慢，不是错，可加 `USE INDEX` 提示另修。
  2. `usage_events.wallet_transaction_id` 上的唯一索引存在（`uq_usage_events_wallet_transaction_id`，`docs/database-schema.md`）—— 已核对 `app/models/usage.py:440`。
- **需要谁拍板**：Kelvin 只需确认「不做时间段筛选、不返回 `metadata_json`、没有 `total`」这三条范围取舍。
- **如果假设错误，具体后果**：假设 1 错 → 带筛选的页变慢（读侧性能问题，不影响数据正确性）。

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
| v1 | 2026-10-18 | 初稿 | — |
| v2 | 2026-10-19 | 游标规则统一为 §2 文字与查询的写法（本页最后一行的序号，下一页取 `<`）：§7「正常路径」的 `next_before_sequence` 由 3、1、`null` 改为 4、2、`null`；§2 JSON 示例的 `next_before_sequence` 由 22 改为 23；§2 字段说明写明多取的一行不返回、不当游标，带筛选时不能推算游标 | AIH-TASK-084 run `70b1cfb2` 实现时发现：v1 的 §7 与 JSON 示例按「游标 = 多取的那一行、`≤`」写，与 §2 文字和查询（最后一行、`<`）矛盾，照 §2 实现 §7 过不了，照 §7 的值翻页会漏掉序号 3 |


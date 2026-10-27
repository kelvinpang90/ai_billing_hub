# AIH-TASK-125 / 126 / 127 / 128 设计：门户内通知（已批准 v3）

> **来源**：设计闸门 Issue #471。本文件是 `APPROVED: design v3` 那一版正文的**逐字副本**（v1、v2 由 Codex 审查、被要求修改，见 §12；v3 由 Codex 审查通过；送审时状态行为 `READY_FOR_REVIEW`，批准后 Issue 上只把状态行改成了 `APPROVED: design v3`）。
> 放进仓库，是因为 OpenClaw Worker 在沙箱里不联网、读不到 GitHub Issue。
> **实现以本文件为准**；与 Issue 不一致时，以 Issue #471 上被批准的 v3 为准。设计要改，就回到 Issue 升版本、重新过闸门，不要直接改本文件。
> **前置**：设计闸门 #433（[AIH-TASK-109-low-balance-threshold.md](AIH-TASK-109-low-balance-threshold.md)）写 `tenant.low_balance` 的规则；设计闸门 #368（[AIH-TASK-089-customer-portal-auth.md](AIH-TASK-089-customer-portal-auth.md)）的客户鉴权与 `lock_tenant_for_customer_write`；设计闸门 #460（[AIH-TASK-121-customer-conversations.md](AIH-TASK-121-customer-conversations.md)）的 `AIH-TASK-122..124`（改同几个前端文件与验收脚本，排在它们之后）。
> §11「任务拆分」的四个实现任务登记为 `AIH-TASK-125`（通知表迁移与模型）→ `AIH-TASK-126`（事件转通知）→ `AIH-TASK-127`（客户通知接口）→ `AIH-TASK-128`（门户通知页），按这个顺序依赖。

---
## 0. 准入判定

| 任务涉及 | 要填的范围 |
| --- | --- |
| 钱包 / 账本 / 定价 / 汇率 / 支付 / 幂等 / 状态机 | **全部章节** |
| 用量摄取 / 认证与会话（集成侧与平台侧）/ Webhook | §1–§7（**§6 不可省**） |
| 前端 / 文档 / CI / 脚本 | **不走本闸门**，直接开 PR |
| 既不碰钱、也不碰用量摄取 / 认证与会话 / Webhook 的后端改动 | **不走本闸门**，直接开 PR |

本任务档位：`全部`

理由：本设计给 `domain_outbox` 的两类钱包派生事件（`tenant.billing_status_changed`、`tenant.low_balance`）接上处理器，改动 outbox 的投递与结算（幂等、INV-13 / INV-14），新建一张表并加迁移，还引入一个通知已读状态（小状态机）和第一个客户写接口。不改钱包、账本与 `post_transaction`。

---

状态：`READY_FOR_REVIEW`
负责人：Claude
**设计版本**：`v3`
对应需求：spec §7 第 11 条、§25、§27 第 4 步、§47、§48、§49、§50、§74（`notifications`）、§74.6、§98.1、§129「Portal notifications」；ADR-0009 第 1、3、4 节
目标 PR：待开（登记 PR）；实现为 AIH-TASK-125..128

## 1. 目标与边界

- **要解决的问题**：钱包记账时，余额跨过低余额阈值、或计费状态在 `ACTIVE` / `SUSPENDED` 之间切换，`post_transaction` 已在同一事务里写了 outbox 行（`app/repositories/wallet.py` 的 `EVENT_LOW_BALANCE`、`EVENT_BILLING_STATUS_CHANGED`），但这两类行**没有处理器**，一直以 `PENDING` 躺在库里（`app/tasks/outbox.py` `_RENDERERS` 上方的注释）。客户在门户上看不到「余额已低于阈值」「已停机」「已复机」这类通知。spec §48–§50 要求三类通知都有门户通道，§27 第 4 步要求复机时「create durable customer notification」。
- **可观察的完成标准**：
  1. 余额从阈值之上跌到阈值及以下（预付客户）→ 最多约一个恢复周期（60 秒）后，该客户门户的通知列表里多一条 `LOW_BALANCE`，带当时的余额与阈值。
  2. 预付客户 `ACTIVE → SUSPENDED` → 一条 `SUSPENDED`；`SUSPENDED → ACTIVE` → 一条 `REACTIVATED`，都带当时的余额。
  3. 同一条 outbox 行不论被投递几次，最多产生一条通知（spec §48「Retry … must not create a second logical notification」）。
  4. 门户侧栏显示未读数；客户可把单条或全部通知标为已读。
  5. 内部计量租户（`INTERNAL_METERED_ONLY`）的计费状态跃迁不产生通知（Kelvin 2026-10-27 定）。
- **明确不做什么**：
  - 不发 Email、不发 WhatsApp。`notifications` 行是逻辑通知；以后的 Email / WhatsApp 通道挂在它下面另立任务（Email 要先重新评估投递方案，见 docs/TODO.md Phase 4「开工前重新评估 Email 传输」；WhatsApp 依赖 D7）。
  - 不改 `post_transaction`、钱包、账本、计费状态规则、低余额判定，也不改两类事件的 payload。
  - 不为账户状态（管理员停用 / 启用，`tenant.account_status_changed`）、计费模式切换、项目集成状态变更出通知。
  - 不做通知删除、归档、按类型筛选，不做推送与轮询（门户在进入页面和导航时读一次）。
  - 不做管理端的通知视图。
  - 不改门户已有的低余额提示条（AIH-TASK-112）。
- **现有行为与问题证据**：
  - `app/repositories/wallet.py` `post_transaction`：预付客户跨阈值时写 `tenant.low_balance`，payload `{balance, threshold, wallet_transaction}`；`_apply_billing_status` 在跃迁时经 `bump_status_version` 写 `tenant.billing_status_changed`，payload `{billing_status, billing_mode, effective_status, status_version, reason, balance, wallet_transaction}`。两者的 `aggregate_type = tenant`、`aggregate_id = 租户 public_id`，`created_at` = 账本行的 `created_at`。
  - `app/tasks/outbox.py`：`recover` 只重投 `_RENDERERS` 里的类型；`app/services/alerts.py` `outbox_backlog` 同样只数 `_RENDERERS` 里的类型。
  - 生产只读核对（2026-10-27）：`domain_outbox` 里 `tenant.billing_status_changed` 有 2 行 `PENDING`（2026-09-24，测试租户），`tenant.low_balance` 0 行；租户共 3 个（两个预付、一个内部计量，全部 `SUSPENDED`）。

## 2. 设计概要

```text
post_transaction（不改）
  └─ 同一事务写 domain_outbox：tenant.low_balance / tenant.billing_status_changed（PENDING）
                │
recover（每 60 秒，已有）── 扫到期的 PENDING 行，类型在「有处理器」名单内 ──► deliver(id)
                │
deliver：领取（已有：attempt_count +1、next_retry_at 推后，提交）
         └─ 通知类事件：一个新事务内
               ① UPDATE domain_outbox SET status=SENT, processed_at=now WHERE id=? AND status=PENDING
               ② 受影响行数为 1 时：按 payload 算出通知（或判定「不出通知」），INSERT notifications
               ③ 提交（①② 原子）
         └─ 邮件类事件：不变（渲染 → SMTP → _settle）
                │
客户门户：GET /customer/notifications（列表 + 未读数）
          POST /customer/notifications/{id}/read、POST /customer/notifications/read-all
```

### 数据库：新表 `notifications`（迁移 0028，AIH-TASK-125）

| 列 | 类型 | 空 | 说明 |
| --- | --- | --- | --- |
| `id` | BIGINT 自增主键 | 否 | 与其他表同一 `_PrimaryKey` 写法 |
| `public_id` | CHAR(36) | 否 | uuid4，**唯一**；客户侧唯一可见的标识 |
| `tenant_id` | BIGINT | 否 | 外键 `tenants.id`，`ON DELETE RESTRICT` |
| `notification_type` | VARCHAR(32) | 否 | `LOW_BALANCE` / `SUSPENDED` / `REACTIVATED`；CHECK 约束限定这三个值；模型用 `Enum(native_enum=False, length=32)` |
| `source_outbox_id` | BIGINT | 否 | 产生它的 `domain_outbox.id`，**唯一**（幂等键，见 §4）；不建外键（§9 方案取舍 B） |
| `balance` | DECIMAL(20,8) | 否 | 事件 payload 的 `balance`：触发事件那笔流水之后的余额 |
| `threshold` | DECIMAL(20,8) | 是 | 只有 `LOW_BALANCE` 非空：事件 payload 的 `threshold`。CHECK：`(notification_type = 'LOW_BALANCE') = (threshold IS NOT NULL)`，写成两条分支的 OR，两库同义 |
| `occurred_at` | DATETIME | 否 | 事件发生时刻 = outbox 行的 `created_at`（= 账本行的 `created_at`，不带时区的 UTC） |
| `created_at` | DATETIME | 否 | 通知行写入时刻（UTC，取整到秒） |
| `read_at` | DATETIME | 是 | 首次标已读的时刻；NULL = 未读 |

索引：

- `uq_notifications_public_id (public_id)`
- `uq_notifications_source_outbox (source_outbox_id)`
- `ix_notifications_tenant_occurred (tenant_id, occurred_at, id)`：列表按租户、时间倒序翻页；未读数 `WHERE tenant_id = ? AND read_at IS NULL` 也走它的前缀（每个租户的通知只在状态跃迁时产生，量很小）。

模型放 `app/models/notifications.py`，在 `alembic/env.py` 登记。迁移只建这一张表，不改任何已有表、不回填；降级只在表为空时删表（§8）；表结构写进 `docs/database-schema.md`。

### 通知规则（AIH-TASK-126，`app/services/notifications.py`）

一个纯函数：输入 outbox 行的 `event_type`、`payload_json`、`created_at`，输出「一条通知草稿」「不出通知」或「payload 无效」三者之一。

| `event_type` | payload 条件 | 结果 |
| --- | --- | --- |
| `tenant.low_balance` | `balance`、`threshold` 都能解析为合法金额 | `LOW_BALANCE`，带 `balance`、`threshold` |
| `tenant.billing_status_changed` | 计费模式为预付（见下）且 `billing_status == "SUSPENDED"` | `SUSPENDED`，带 `balance` |
| `tenant.billing_status_changed` | 计费模式为预付（见下）且 `billing_status == "ACTIVE"` | `REACTIVATED`，带 `balance` |
| `tenant.billing_status_changed` | `billing_mode` 是 `"INTERNAL_METERED_ONLY"` | **不出通知**（outbox 行照常结算为 `SENT`） |
| 任一 | payload 不是 JSON 对象、缺字段、金额不能解析或超出 `DECIMAL(20,8)`、`billing_status` / `billing_mode` 不是已知值 | **payload 无效**（按失败处理，见 §5） |

- 金额必须是字符串，且整串匹配 `^-?[0-9]{1,12}(\.[0-9]{1,8})?$`（写入方 `_money_text` 用定点格式，余额可以为负；`threshold` 另要求不为负），再用 `Decimal(str)` 解析，不经 float。不是字符串、指数写法、`NaN` / `Infinity`、超过 8 位小数或 12 位整数都无效。
- **计费模式为预付**：payload 有 `billing_mode` 键时，它必须是 `"PREPAID"`（预付）或 `"INTERNAL_METERED_ONLY"`（不出通知），其他值无效；**没有这个键时按预付处理**。理由：`billing_mode` 列与这个 payload 键是同一个提交加的（#236 `e950f97`，2026-10-08，迁移 0019），此后 `_apply_billing_status` 写的每一行都带它；没有这个键的行只能写于 0019 之前，那时还没有内部计量模式，所有租户都是预付。生产现有的 2 行积压（2026-09-24）正是这种行：键为 `balance`、`billing_status`、`reason`、`status_version`、`wallet_transaction`，租户当前都是预付（2026-10-27 只读核对）。`billing_mode` 键存在但值为 `null` 或非字符串 → 无效。
- `low_balance` 不再检查计费模式：写入方已只对预付客户写这类事件（设计闸门 #433 v2 §2），payload 里也没有计费模式。
- 租户由 outbox 行的 `aggregate_type == "tenant"` 与 `aggregate_id`（租户 `public_id`）查 `tenants.id`。`aggregate_type` 不是 `tenant`、或查不到租户（租户从不删除，不应发生）→ 无效。
- 判定只看 payload（事件发生时的快照），不读租户当前的计费模式或余额：通知说的是当时发生了什么。

### 投递（AIH-TASK-126，`app/tasks/outbox.py`）

- 新增一份「通知类事件」名单：`tenant.low_balance`、`tenant.billing_status_changed`。`recover` 的扫描与 `alerts.outbox_backlog` 的计数都改用「邮件类 ∪ 通知类」的同一个函数（例如 `handled_event_types()`），两处不再各自引用 `_RENDERERS`。
- `deliver(id)` 的领取一步不变（带完整前置条件的 UPDATE，`attempt_count` 当乐观锁，同时推后 `next_retry_at`，先提交）。
- 领取成功后，通知类事件走新分支，**一个事务**内：
  1. `UPDATE domain_outbox SET status = 'SENT', processed_at = :now WHERE id = :id AND status = 'PENDING'`；受影响行数不是 1 → 回滚，返回 `already-settled`（另一个 worker 已结算）。
  2. 调通知规则：草稿 → `INSERT notifications`（`public_id` 新 uuid4，`source_outbox_id = id`，`created_at = now`）；不出通知 → 不插入；无效 → 回滚，走下面的失败分支。
  3. 提交。①② 同一事务：通知行存在 ⇔ outbox 行是 `SENT` 且规则给出了草稿。
- **payload 不清空**：通知类事件的 payload 只有余额、阈值、状态与流水 `public_id`，不含令牌或个人数据（`wallet.py` `_outbox_event` 的注释），留着供排查。邮件类事件结算时仍清空（不变）。
- 失败（payload 无效、事务内任何异常）：回滚这个事务；之后与邮件类同一套逻辑——`attempt < outbox_max_attempts` 记 warning 等下次领取，否则置 `FAILED`（死信，记 error 日志）。死信结算对通知类事件同样**不清空** payload。异常不向外抛（与 `_attempt_send` 一样「never raises」）：任务返回值说明发生了什么。
- 通知类事件不调 SMTP，`build_transport` 不参与；不需要 `BILLING_FRONTEND_BASE_URL`。
- `post_transaction` 不 `delay()` 这两类事件（不改钱包代码）：触发只靠 `recover` 的 60 秒周期，通知最多晚约一个周期出现。

### 客户接口（AIH-TASK-127）

三个接口都在 `/api/v1/customer` 下，租户只取自 `CustomerPrincipal.tenant_id`（INV-8），路径、查询串、请求体里没有租户参数。

**`GET /api/v1/customer/notifications`**（只读，`require_customer`，四种账户状态都能读）

- 查询参数：`page_size`（1–100，缺省 20，同 `app/schemas/customers.py` 的常量）、`cursor`（可选，上一页返回的 `next_cursor`）。
- 排序：`occurred_at` 倒序，同一时刻按 `id` 倒序。
- 游标：就是上一页最后一条的 `notification_id`（`public_id`）。服务端在**本租户**内按 `public_id` 查出它的 `(occurred_at, id)`，再取 `(occurred_at, id) < (c_occ, c_id)` 的行。格式不是 UUID（FastAPI 422），或本租户查不到（包括别的租户的通知）→ 422 `VALIDATION_ERROR`（字段名 `cursor`，与用量页游标无效同一个错误类）。
- 多取一行判断有没有下一页；`next_cursor` 没有下一页时为 `null`；不返回总数。
- 响应：

  ```json
  {
    "items": [
      {
        "notification_id": "uuid",
        "notification_type": "LOW_BALANCE | SUSPENDED | REACTIVATED",
        "occurred_at": "2026-10-27T05:12:09Z",
        "balance": "12.50000000",
        "threshold": "50.00000000 或 null",
        "read_at": "时间或 null"
      }
    ],
    "next_cursor": "uuid 或 null",
    "unread_count": 3
  }
  ```

  时间格式与客户钱包流水的 `created_at` 相同；金额是 8 位小数的定点字符串（与客户钱包视图同一 `money_text`）。条目恰好这 6 个键：不返回内部 `id`、`tenant_id`、`source_outbox_id`、`created_at`，也不返回 outbox payload 里的其他字段（`status_version`、`reason`、`effective_status`、流水 `public_id`）。
- `unread_count` 是本租户 `read_at IS NULL` 的行数，与列表在同一个会话里读，不受分页影响。

**`POST /api/v1/customer/notifications/{notification_id}/read`**（写，`require_customer_writable`）

- 路径参数是 UUID 形状（否则 422）。
- 写事务：先 `lock_tenant_for_customer_write(session, tenant_id)`（`DISABLED` / `CLOSED` → 403 `ACCOUNT_READ_ONLY`）；再 `UPDATE notifications SET read_at = :now WHERE tenant_id = :tenant AND public_id = :id AND read_at IS NULL`；再按 `(tenant_id, public_id)` 读回这一行；读不到 → 404 `NOTIFICATION_NOT_FOUND`（别的租户的通知同样 404，不区分）；提交。
- 幂等：已读的再标一次不改 `read_at`（保留首次时刻），照样 200。
- 响应：这一条（与列表条目同样的 6 个键）。

**`POST /api/v1/customer/notifications/read-all`**（写，`require_customer_writable`）

- 无请求体。同样先锁租户重查状态；`UPDATE notifications SET read_at = :now WHERE tenant_id = :tenant AND read_at IS NULL`；提交。
- 响应：`{"marked": <本次标记的行数>}`；没有未读时为 0，照样 200。

不写审计：已读状态只影响客户自己的界面显示，不改变余额、权限或任何业务状态（§6）。

### 前端（AIH-TASK-128，不在闸门范围内，写在这里只为说明接口够用）

- 新页 `/portal/notifications`：表格列出时间（马来西亚时间）、类型说明、余额（与钱包页同一个 2 位小数显示助手）、阈值（仅低余额）；未读行加粗，每行一个「标为已读」，表头一个「全部标为已读」；按 `next_cursor` 「加载更多」；空列表显示空状态。类型说明的文案放 `en.json`，后端不存文案。
- 门户侧栏加「通知」项，旁边显示未读数（`unread_count`，0 时不显示）；进入门户与在通知页标已读后刷新。
- 浏览器验收：沿用 `scripts/acceptance/admin_customers.mjs`，步骤 `login_customer`、`check_portal_notifications`（只读：按 href 点侧栏的通知项，等到页面标题，再等到表格或空状态；不点「标为已读」）。

### 时间

- `occurred_at` 取 outbox 行的 `created_at`，不取处理时刻：重试晚几分钟不改变通知说的时间。
- `created_at`、`read_at` 是 UTC、取整到秒（与 `account_status._now()` 同一做法）。

### 金额

- 只存与回显，不做任何运算：payload 里的定点字符串 → `Decimal` → `DECIMAL(20,8)` → `money_text`。SQLite 上 NUMERIC 按浮点存（#460 v2 已记录），所以精确往返只在 MySQL 上保证与验证（§7）。

## 3. 不变量影响矩阵

| 不变量 | 是否触碰 | 可能怎样被破坏 | 设计控制 | 验证方式 |
| --- | --- | --- | --- | --- |
| INV-1 中心故障不中断 AI | 否 | 通知投递失败拖慢记账 | 投递在 worker 里、记账提交之后；`post_transaction` 不改、不等通知 | 不改代码路径；§7「事务中途失败」 |
| INV-2 事件不重复扣费 | 否 | — | 不碰用量与记账 | 不改代码 |
| INV-3 支付不重复入账 | 否 | — | 不碰支付 | 不改代码 |
| INV-4 余额只经账本变动 | 否 | 通知逻辑误写钱包 | 新代码只写 `notifications` 与 `domain_outbox` 的状态列 | §7 正常路径断言钱包与账本行不变 |
| INV-5 历史账本不可变 | 否 | — | 不写 `wallet_transactions` | 同上 |
| INV-6 事件保留版本引用 | 否 | — | 不碰用量事件 | 不改代码 |
| INV-7 客户不可见成本毛利 | 是 | 通知接口带出 payload 里的内部字段 | 视图恰好 6 个键；payload 本身没有成本、加价、毛利（`_outbox_event` 注释） | §7 断言键集合 |
| INV-8 租户不可互访 | 是 | 客户读到或标记别的租户的通知；游标借用别的租户的通知 | 租户只取自会话；列表、游标查找、标已读、全部已读的 SQL 都带 `tenant_id = 会话租户`；通知的租户由 outbox 行的 `aggregate_id` 决定 | §7「租户越权」四条 |
| INV-9 对话内容不入库 | 否 | — | 通知不含对话内容 | 表结构没有这类列 |
| INV-10 金额用 Decimal | 是 | payload 金额经 float 解析 | `Decimal(str)` 解析、`DECIMAL(20,8)` 存、`money_text` 出 | §7「金额精度」（MySQL） |
| INV-11 event_id 至多一次财务效果 | 否 | — | 通知没有财务效果 | — |
| INV-12 定稿对账单不可变 | 否 | — | 不碰对账单 | — |
| INV-13 状态与事件原子提交 | 是 | 通知行写了但 outbox 未结算（重复通知），或 outbox 结算了但通知没写（丢通知） | 结算 UPDATE 与通知 INSERT 同一事务；outbox 行本身仍由 `post_transaction` 与跃迁同事务写入（不改） | §7「事务中途失败」「幂等重放」 |
| INV-14 队列丢失不毁持久工作 | 是 | 通知只靠队列消息触发，Redis 丢失后永远不出 | 触发只靠 `recover` 从库里扫 `PENDING`；领取推后 `next_retry_at`，worker 崩溃后行自动重新到期 | §7「Redis/Celery 丢失」 |

## 4. 状态与并发

**outbox 行（通知类事件）**

| 当前状态 | 事件 | 前置条件 | 新状态 | 副作用 | 非法时结果 |
| --- | --- | --- | --- | --- | --- |
| `PENDING` | 领取 | `next_retry_at <= now`、`attempt_count` 未变 | `PENDING`（`attempt_count + 1`，`next_retry_at` 推后） | 无 | 受影响 0 行 → `not-claimed` |
| `PENDING` | 结算成功 | 规则给出草稿或「不出通知」 | `SENT`（`processed_at`） | 草稿时同事务插入一条通知 | 受影响 0 行 → 回滚，`already-settled` |
| `PENDING` | 规则判无效或事务异常，次数未用尽 | — | `PENDING`（不变） | 回滚，warning 日志 | — |
| `PENDING` | 同上，次数用尽 | `attempt >= outbox_max_attempts` | `FAILED` | error 日志；payload 保留 | — |
| `SENT` / `FAILED` | 任何 | — | 不变 | 无 | `already-settled` |

**通知的已读状态**

| 当前状态 | 事件 | 前置条件 | 新状态 | 副作用 | 非法时结果 |
| --- | --- | --- | --- | --- | --- |
| 未读（`read_at` NULL） | 标已读 / 全部已读 | 账户不是 `DISABLED` / `CLOSED` | 已读（`read_at = now`） | 无 | 403 `ACCOUNT_READ_ONLY` |
| 已读 | 标已读 / 全部已读 | 同上 | 已读（`read_at` 不变） | 无 | 同上 |

没有「已读 → 未读」。

- **串行化**：两个 worker 同时投同一行：领取靠 `attempt_count` 乐观锁只有一个成功；领取后崩溃、行重新到期被另一个 worker 领走时，两次结算的 `UPDATE … WHERE status = 'PENDING'` 由 InnoDB 行锁排队，后到的读到 `SENT`、受影响 0 行、回滚，不插入。
- **数据库的唯一性保证**：`source_outbox_id` 唯一，是「一条 outbox 行最多一条通知」的兜底；`public_id` 唯一。
- **幂等键**：`domain_outbox.id`。同一个 id 不会有不同的 payload（outbox 行写入后 payload 只会被邮件类结算清空，通知类不改）。
- **重试层**：只在 outbox 层（`attempt_count`、`next_retry_at`、`recover`）。
- **客户写接口**：先锁租户行（`lock_tenant_for_customer_write`）。它与 `post_transaction` 锁的是同一行，所以记账与标已读排队；标已读的事务只有一条锁读与一条 UPDATE，持锁很短。worker 插入通知不锁租户行，与标已读互不阻塞；「全部已读」之后才提交的通知仍是未读（§10）。
- **必须原子提交**：outbox 结算 + 通知插入（§2「投递」）。

## 5. 失败与恢复矩阵

| 失败点 | 对外结果 | 数据最终状态 | 是否可重试 | 恢复来源 | 告警 |
| --- | --- | --- | --- | --- | --- |
| payload 无效（缺字段、金额不合法、未知状态） | 客户看不到这条通知 | 重试至上限后 outbox 行 `FAILED`，payload 保留；无通知行 | 自动重试到上限；之后人工 | 人工看 `FAILED` 行与 error 日志 | 次数用尽时 error 日志；未用尽前超过 10 分钟仍 `PENDING` → `outbox_backlog` P2 |
| 结算事务提交失败（DB 断开、死锁） | 晚出现 | 回滚：outbox 仍 `PENDING`，无通知行 | 是 | `next_retry_at` 到期后 `recover` 重投 | 同上 |
| worker 在领取之后、结算之前崩溃 | 晚出现 | outbox `PENDING`（已占一次 `attempt_count`），无通知行 | 是 | 同上 | 同上 |
| Redis/Celery 丢失 | 晚出现 | 数据库里的 `PENDING` 行不受影响 | 是 | broker 恢复后 `recover` 每 60 秒扫库重建 | `outbox_backlog` |
| 外部服务超时 | 不适用 | 通知类事件不调任何外部服务 | — | — | — |
| 重复投递（`recover` 与上一次投递重叠） | 不重复 | 一条通知；后到者 `already-settled` 或 `not-claimed` | — | — | 无 |
| 标已读时租户被停用 | 403 `ACCOUNT_READ_ONLY` | `read_at` 不变 | 否 | — | 无 |
| 标已读的通知不存在或属于别的租户 | 404 `NOTIFICATION_NOT_FOUND` | 不变 | 否 | — | 无 |
| 列表游标无效或属于别的租户 | 422 `VALIDATION_ERROR` | 不变 | 否 | — | 无 |
| 表非空时执行 0028 降级 | 迁移失败（抛错） | 表、通知行与已读状态原样保留；`alembic_version` 仍为 0028 | 否（按设计拒绝） | 不需要：数据从未被删 | 部署 / 迁移日志的错误 |
| 滚动更新：旧 worker 镜像遇到这两类行 | 不处理 | 行保持 `PENDING`（旧 `recover` 不扫它们，与今天相同） | — | 新 worker 起来后接手 | 旧 `outbox_backlog` 不数它们 |

## 6. 数据与安全边界

- **租户过滤**：通知的租户在写入时由 outbox 行的 `aggregate_id` 决定（写入方是 `post_transaction`，来自锁内的租户行）；客户接口的租户只来自 `CustomerPrincipal.tenant_id`，每条 SQL 都带 `tenant_id` 条件；标已读与游标查找按 `(tenant_id, public_id)` 查，查不到一律当作不存在。
- **鉴权**：列表 `require_customer`；两个写接口 `require_customer_writable` 加事务内的 `lock_tenant_for_customer_write`。ADMIN 调这三个接口是 403 `CUSTOMER_REQUIRED`（已有依赖的行为）。三个接口登记进 `tests/backend/test_customer_api.py` 的路由守卫。
- **客户侧禁止返回的字段**：内部 `id`、`tenant_id`、`source_outbox_id`、行的 `created_at`、outbox payload 的 `status_version` / `reason` / `effective_status` / 流水 `public_id`；没有任何成本、加价或毛利字段（INV-7）。
- **日志**：worker 日志只带 `outbox_id`、`event_type`、尝试次数，不带余额、阈值或租户标识（与现有 outbox 日志一致）。接口错误日志不带查询参数的值。
- **审计**：不写审计。通知插入是系统对已审计事实（`TENANT_BILLING_STATUS_CHANGED` 审计、账本行）的派生；已读状态只影响客户自己的显示。
- **密钥**：不涉及。
- **prompt / response 与客户数据**：不处理对话内容；通知只含余额与阈值（客户本来就能在钱包页看到）。
- **数据保留**：通知行随租户长期保留，与钱包流水一致；日后 PDPA 与数据留存政策（上线闸门）一并处理。`source_outbox_id` 不建外键，所以日后清理 outbox 不被通知表挡住，但清理方案必须保留「已结算」语义（§9 B）。

## 7. 测试证据计划

| 风险/需求 | 测试层级 | 场景 | 预期结果 |
| --- | --- | --- | --- |
| 迁移形状 | integration（MySQL） | 0027 → 0028 → 0027 → 0028（表为空） | 表、列可空性、列宽、唯一索引、`ix_notifications_tenant_occurred` 的列顺序、两个 CHECK 与外键 RESTRICT 与模型一致；空表降级只删这张表；其他表不变 |
| 重跑：表已存在且形状正确 | integration（MySQL） | 升级到 0028 后把 `alembic_version` 改回 0027（模拟建表后写版本号失败），再升级到 0028 | 不发任何 DDL（捕获执行语句断言）；`alembic_version` 为 0028；表结构与首次升级后的快照相同 |
| 重跑：表已存在但形状不对 | integration（MySQL） | 版本号为 0027，`notifications` 表已存在但分别缺一条 CHECK、缺 `ix_notifications_tenant_occurred`、`tenant_id` 外键不是 RESTRICT、多一列（四个子场景，每个用全新的表） | 每个子场景都抛错（消息点名这张表）；不发任何 DDL；`alembic_version` 仍为 0027；预置的表原样 |
| 带数据降级 | integration（MySQL） | 0028 上插入两条通知（一条已读）后执行降级到 0027 | 抛错；没有发出任何 DDL（捕获执行语句断言）；表、两行与 `read_at` 原样；`alembic_version` 仍为 0028 |
| CHECK | integration（两库） | 插入 `LOW_BALANCE` 不带阈值、`SUSPENDED` 带阈值、未知类型 | 三条都被数据库拒绝 |
| 正常路径：低余额 | integration | 预付租户阈值 50，余额 60 → 记一笔 -15；跑 `recover` 与 `deliver` | 一条 `LOW_BALANCE`，`balance` 45、`threshold` 50、`occurred_at` = 账本行 `created_at`；outbox `SENT`，payload 仍在；钱包与账本行不变 |
| 正常路径：停机与复机 | integration | 预付租户余额 10 → 记 -10 → 记 +5，依次投递 | 先一条 `SUSPENDED`（余额 0），再一条 `REACTIVATED`（余额 5） |
| 同一笔两条 | integration | 阈值 10、余额 15 → 记 -20 | 两条 outbox、两条通知：`LOW_BALANCE` 与 `SUSPENDED` |
| 内部计量租户 | integration | `INTERNAL_METERED_ONLY` 租户的 `billing_status_changed` 行 | outbox `SENT`，没有通知行 |
| payload 无效 | unit + integration | 缺 `balance`、金额 `"1e3"`、9 位小数、13 位整数、`"NaN"`、数字而非字符串、`threshold` 为 `"-1.00"`、未知 `billing_status`、未知 `billing_mode`、payload 是数组、`aggregate_type` 不是 `tenant`、租户查不到 | 规则返回无效；`attempt < max` 时行仍 `PENDING`，用尽时 `FAILED` 且 payload 保留；都没有通知行 |
| 幂等重放 | integration | 同一行 `deliver` 两次；已 `SENT` 的行再 `deliver` | 一条通知；第二次 `already-settled` |
| 并发 | integration（MySQL） | 同一行领取后让 `next_retry_at` 到期，两个会话同时走结算分支（一个在结算 UPDATE 后、提交前暂停） | 只有一条通知；另一方受影响 0 行、回滚 |
| 事务中途失败 | integration | 让通知 INSERT 抛错（例如替换插入函数） | outbox 仍 `PENDING`（结算 UPDATE 被回滚），没有通知行 |
| Redis/Celery 丢失 | integration | 不调 `delay`，只调 `recover` | 到期的两类行被重新触发并结算；未到期的不触发 |
| recover 与告警名单 | unit | 名单函数 | 含两类通知事件与两类邮件事件；`outbox_backlog` 数超过 10 分钟仍 `PENDING` 的 `tenant.billing_status_changed`（改写 `test_usage_alerts.py` 里「不计入」的那条） |
| 邮件类不回归 | integration | 现有 `test_outbox.py` 全部用例 | 照旧通过；邮件类结算仍清空 payload |
| 列表 | integration | 25 条通知、`page_size` 10 | 三页 10/10/5，时间倒序，同一 `occurred_at` 按 `id` 倒序；`next_cursor` 最后为 `null`；每页 `unread_count` 相同；条目恰好 6 个键 |
| 游标 | integration | 非 UUID、不存在的 UUID | 前者 FastAPI 422，后者 422 `VALIDATION_ERROR` |
| 标已读 | integration | 标一条；再标一次；全部已读；再全部已读 | `read_at` 首次写入后不变；`unread_count` 递减；`marked` 依次为 N、0 |
| 只读账户 | integration | 租户 `DISABLED` 后列表、标已读、全部已读 | 列表 200；两个写接口 403 `ACCOUNT_READ_ONLY`，`read_at` 不变 |
| 租户越权 | integration | A 用 B 的通知 id 标已读；A 用 B 的通知 id 当游标；A 全部已读；A 的列表 | 404；422；B 的 `read_at` 不变；A 的列表里没有 B 的行 |
| 鉴权 | integration | ADMIN、未登录 | 403 `CUSTOMER_REQUIRED`、401；路由守卫列出三个新接口 |
| 金额精度 | integration（**仅 MySQL**） | payload `balance` `"999999999999.99999999"`、`threshold` `"0.00000001"` | 库里读回与接口返回的字符串与 payload 完全一致；先断言这两个值经 float 往返会变（用例有区分力） |
| 生产积压与旧 payload | integration | 预置两条与生产积压同形状的 `billing_status_changed` `PENDING` 行（payload 没有 `billing_mode` 与 `effective_status`，一条 `ACTIVE`、一条 `SUSPENDED`）；另一条 `billing_mode` 为 `null`、一条为 `"POSTPAID"` | 前两条照常转成 `REACTIVATED` 与 `SUSPENDED`，`occurred_at` 为原时刻；后两条按 payload 无效处理 |

## 8. 迁移与上线

- **数据迁移步骤**：0028 只 `CREATE TABLE notifications` 及其索引、约束；不改已有表、不回填。MySQL 上建表前用 inspector 判一次：表不存在 → 建；存在且形状与预期一致 → 跳过、不发 DDL；形状不对 → 抛错、不发 DDL（与 0026、0027 同一写法）。「形状」逐项比：恰好这 10 列（名称、类型与宽度、可空性）、两个唯一索引（名称与列）、`ix_notifications_tenant_occurred`（名称、列顺序、非唯一）、`tenant_id` 外键（引用表与列、`ON DELETE RESTRICT`）、两条 CHECK（名称存在）；任何一项缺失、多出或不同都算形状不对。这条分支的用途是「MySQL 上 DDL 自动提交，建表成功后写 `alembic_version` 失败」时的重跑：表已在、版本号还是 0027。**降级只在表为空时删表**：先 `SELECT 1 FROM notifications LIMIT 1`，有任何一行就抛错（消息点名这张表、行数非零、处理方法是保留表只回滚应用镜像），不发任何 DDL、不删任何行；表不存在时什么都不做。
- **锁表与性能影响**：建新表不锁任何已有表。运行时：每条通知类 outbox 行多一个短事务；量只与状态跃迁次数成正比，生产现有积压 2 行。
- **兼容窗口**：`deploy.sh` 先迁移、再换镜像。125 合并后表存在但没人写；126 合并后 worker 开始写；127 合并后才有接口读；128 合并后才有页面。每一层单独部署都安全。
- **部署顺序**：125 → 126 → 127 → 128（`depends_on` 串起来）。126 上线后的第一个恢复周期会把生产上 2 行 2026-09-24 的 `PENDING` 转成通知（Kelvin 2026-10-27 定：照常转，不设时间分界）。
- **回滚或前滚**：生产上的回滚**只回滚应用镜像，不降级 0028**。126 回滚到旧镜像：旧 worker 不认这两类事件，已 `SENT` 的行与已写的通知（含已读状态）原样保留在表里，新产生的行以 `PENDING` 等再次前滚；不会重复通知（`SENT` 的行不再被领取）。127、128 回滚同理：表与数据不动，只是接口或页面暂时没有。0028 的降级只有在表为空（126 从未在这个库上结算过任何通知类行）时才执行；表非空时降级抛错、不发 DDL、不删行，所以「先回滚 126 再降级 125」不会删掉已交付的通知。需要修复时一律前滚。
- **部分部署**：API 新、worker 旧（或反之）见 §5 最后一行；接口只读这张表，不依赖 worker 的版本。
- **监控**：沿用 `outbox_backlog`（P2，超过 10 分钟仍 `PENDING`）；它从此也数这两类事件，`docs/api.md` 与 `docs/runbook.md` 里「现在只有密码重置信」「不计入」的说法随 126 改正。负责人照 runbook 现有分工。

## 9. 方案取舍

| 方案 | 优点 | 风险 | 未采用原因 |
| --- | --- | --- | --- |
| 采用：worker 从已有 outbox 行派生通知，结算与插入同事务 | 不改钱包热路径；复用已有领取、退避、恢复与告警；生产积压自然被消化 | 通知最多晚约 60 秒 | — |
| A：`post_transaction` 内同事务直接写通知行 | 通知与跃迁零延迟、零额外组件 | 改动钱包热路径（每条用量计费都走它）；通知规则（内部租户不出、文案类型）混进记账代码；生产已有的 `PENDING` 行仍要另写处理器才能消化 | 风险落在最不该动的地方，且没有省掉处理器 |
| B：`source_outbox_id` 建外键到 `domain_outbox` | 引用完整性由数据库保证 | outbox 是投递机制表，日后按时间清理会被通知表挡住，需要级联或先删通知（通知是要长期给客户看的） | 唯一约束已足够保证幂等；引用来源只用于排查 |
| C：通知表只存 outbox id，接口读时联查 payload 拼字段 | 少存两列金额 | 邮件类结算会清 payload，将来若改动结算逻辑就会丢通知内容；接口要解析 JSON | 通知应是自足的持久记录（spec §27「durable customer notification」） |
| D：一个 outbox 行按通道拆多行（门户、Email、WhatsApp 各一） | 各通道独立重试 | 现在只有门户一个通道；要改 `post_transaction` 写多行 | 通道是逻辑通知之下的事，等 Email / WhatsApp 任务时在 `notifications` 之下加投递记录 |
| E：标已读用 `read_at` 之外的计数列或单独的已读表 | — | 多一张表或一套一致性问题 | 每条通知一个可空时间列已够；没有多用户（spec §52：一个客户一个门户账号） |

## 10. 未决问题与假设

- **未决问题**：无。三项范围问题已由 Kelvin 2026-10-27 拍板：要已读 / 未读与未读角标；内部计量租户不出停机 / 复机通知；生产积压照常转成通知。
- **接受的行为**：「全部已读」只标记当时已提交的未读行；客户打开页面之后、点「全部已读」之前新出的通知会一起被标成已读（它仍在列表里，只是不加粗）。状态跃迁本身很少，这个窗口只有几秒，接受。
- **尚未验证的假设**：
  1. `recover` 的 60 秒周期在生产 beat 上正常运行（`celery_app.py` `OUTBOX_RECOVERY_SECONDS`，邮件投递已依赖它）。
- **需要谁拍板**：无。
- **如果假设错误，具体后果**：假设 1 不成立 → 邮件投递早已同样失效，`outbox_backlog` 会报警。

## 11. 审查与版本绑定

审查方要回答的五个问题常驻在 [scripts/review_checklist.md](../../scripts/review_checklist.md) 的「设计审查」一节，此处不重复。

**版本绑定规则**（规则的出处是 [WORKFLOW §3](../../docs/WORKFLOW.md)；这里保留一份是因为审查材料里只有本 Issue 正文，两处改一起改）：

- Codex 的批准必须写成 `APPROVED: design v<N>`，`<N>` 取本 Issue 顶部的「设计版本」
- **设计版本一变，之前的 APPROVE 自动作废**，必须重新过闸门
- 什么算「实质修改」：改了契约、表结构、事务边界、状态机、失败语义、不变量控制。改错别字不算
- 实质修改时：把顶部版本 +1，在下方追加一条变更说明，状态退回 `READY_FOR_REVIEW`

**任务拆分**（每层合并后单独通过检查与 CI）：

| 任务 | 层 | 内容 | 依赖 |
| --- | --- | --- | --- |
| AIH-TASK-125 | 数据库 | 迁移 0028、模型、`docs/database-schema.md`、迁移测试 | 124 |
| AIH-TASK-126 | 业务规则 | 通知规则、outbox 通知分支、`recover` 与 `outbox_backlog` 名单、文档改正 | 125 |
| AIH-TASK-127 | 接口 | 三个客户接口、`docs/api.md`、路由守卫 | 126 |
| AIH-TASK-128 | 前端 | 门户通知页、侧栏未读数、浏览器验收步骤 | 127 |

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
| v1 | 2026-10-27 | 初稿 | — |
| v2 | 2026-10-27 | 0028 降级只在表为空时删表，表非空时抛错、不发 DDL；§8 生产回滚只回滚应用镜像、不降级 0028；§5 加「表非空时降级」一行；§7 加「带数据降级」用例 | Codex v1 REQUEST_CHANGES：先回滚 126 再降级 125 会删掉已交付的通知，来源 outbox 已 `SENT`，无法重建 |
| v3 | 2026-10-27 | §8 写明 0028 判「形状」逐项比哪些（10 列、两个唯一索引、租户时间索引、外键 RESTRICT、两条 CHECK）及这条分支的用途；§7 加「表已存在且形状正确」「表已存在但形状不对」（四个子场景）两行 MySQL 用例 | Codex v2 REQUEST_CHANGES：表已存在的两个升级分支没有测试 |

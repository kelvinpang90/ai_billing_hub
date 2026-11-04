# AIH-TASK-152 / 153 设计：低余额、停机、复机通知的 Email 通道

> **来源**：设计闸门 Issue #559。本文件是被批准那一版正文的**逐字副本**。
> 放进仓库，是因为 OpenClaw Worker 在沙箱里不联网、读不到 GitHub Issue。
> **实现以本文件为准**；与 Issue 不一致时，以 Issue 上被批准的版本为准。设计要改，就回到 Issue 升版本、重新过闸门，不要直接改本文件。
> **前置**：设计闸门 #471（[AIH-TASK-125-portal-notifications.md](AIH-TASK-125-portal-notifications.md)）的 `notifications` 表与通知类事件的结算分支；AIH-TASK-151 的 Notification Adapter 与邮件模板层（`app/core/notification_adapter.py`、`app/core/email_templates.py`）。
> §11「任务拆分」的两个实现任务登记为 `AIH-TASK-152`（通知邮件的渲染与投递）→ `AIH-TASK-153`（结算通知时同事务写邮件 outbox 行），按这个顺序依赖。

---
## 0. 准入判定

| 任务涉及 | 要填的范围 |
| --- | --- |
| 钱包 / 账本 / 定价 / 汇率 / 支付 / 幂等 / 状态机 | **全部章节** |
| 用量摄取 / 认证与会话（集成侧与平台侧）/ Webhook | §1–§7（**§6 不可省**） |
| 前端 / 文档 / CI / 脚本 | **不走本闸门**，直接开 PR |
| 既不碰钱、也不碰用量摄取 / 认证与会话 / Webhook 的后端改动 | **不走本闸门**，直接开 PR |

本任务档位：`全部`

理由：本设计在通知类 outbox 行的结算事务里多写一行 outbox（INV-13 的原子提交、「一条逻辑通知最多一封信」的幂等），新增一个邮件类事件类型及其处理器（INV-14 的恢复名单），并改邮件类结算的前置条件（outbox 小状态机的「只有当前领取者能结算」）。不改钱包、账本、`post_transaction`、通知规则，不加表、不加迁移。

---

状态：`READY_FOR_REVIEW`
负责人：Claude
**设计版本**：`v1`
对应需求：spec §47、§48、§49、§50、§74.6、§98.1、§129「Low Balance」「Suspension notification」「Reactivation notification」「existing Email adapter」；ADR-0009 第 1–3 节；ADR-0014
目标 PR：待开（登记 PR）；实现为 AIH-TASK-152..153

## 1. 目标与边界

- **要解决的问题**：spec §48–§50 要求低余额、停机、复机三类通知除门户外还要发 Email。今天只有门户通道：通知类 outbox 行（`tenant.low_balance`、`tenant.billing_status_changed`）结算时插入一条 `notifications` 行（AIH-TASK-126），不发信。AIH-TASK-151 已把邮件改为经 Notification Adapter 与模板层发送，但只有密码重置与门户邀请两个模板。
- **可观察的完成标准**：
  1. 预付客户每产生一条门户通知（`LOW_BALANCE` / `SUSPENDED` / `REACTIVATED`），该租户的联系邮箱（`tenants.email`）最终收到一封对应的英文邮件，带当时的余额（低余额另带阈值）、事件时刻与门户钱包页链接。
  2. 一条门户通知最多对应一行邮件 outbox 行；同一行邮件 outbox 行不论投递几次，仍是那一行（投递本身是至少一次，见 §4）。
  3. 内部计量租户不产生通知，因而也不发信（沿用 #471）。
  4. 本设计上线之前已经存在的门户通知不补发邮件（Kelvin 2026-11-04 定）。
  5. 邮件投递失败不影响门户通知：门户通知照常存在，邮件行按已有的重试、死信与 `outbox_backlog` 告警处理。
- **明确不做什么**：
  - 不做 WhatsApp（依赖 D7 的 Meta 凭据与模板审批，另立任务；Kelvin 2026-11-04 定只做 Email）。
  - 收件人只有租户联系邮箱，不发给门户账号（Kelvin 2026-11-04 定）；不做收件人配置、退订、抄送。
  - 不加表、不加列、不加迁移；不记录「哪条通知发了哪封信」之外的投递回执（邮件 outbox 行的 `aggregate_id` 就是通知的 `public_id`，足以排查）。
  - 不改 `post_transaction`、钱包、账本、低余额与停机判定、通知规则（`app/services/notifications.py` 的判定）、门户通知接口与页面。
  - 不改两封现有邮件（密码重置、门户邀请）的收件人、标题与正文。
  - 模板只有英文（沿用 AIH-TASK-151，Kelvin 2026-11-03 定）。
  - 邮件里不放充值链接：客户自助充值（Phase 4）还没有；正文请客户联系 Acuven 充值，链接只到门户钱包页。
- **现有行为与问题证据**：
  - `app/tasks/outbox.py` `_settle_notification`：一个事务内 ① 带 `attempt_count = :attempt` 的结算 UPDATE；② 按规则得到草稿后查 `tenants.id`、插入 `notifications`；③ 提交。不写别的 outbox 行。
  - `app/tasks/outbox.py` `_settle`（邮件类结算）：`WHERE id = :id AND status = 'PENDING'`，**不带** `attempt_count`；设计闸门 #471 v7 §10 把「旧领取者死信覆盖新领取」记为邮件路径的已知缺口、留给后续。
  - `app/core/email_templates.py`：只有 `password_reset`、`portal_invitation` 两个模板。
  - `tenants.email`：`NOT NULL`、`String(320)`，创建与修改经 `EmailStr` 校验（`app/schemas/customers.py`），是联系邮箱不是登录账号，刻意不唯一。
  - 前端门户钱包页路径登记在 `frontend/src/routes/paths.ts` 的 `portalWallet: "/portal/wallet"`。

## 2. 设计概要

```text
post_transaction（不改）── 同事务写 tenant.low_balance / tenant.billing_status_changed（PENDING）
        │
recover ──► deliver(id)：领取（不变）
        └─ 通知类：一个事务内（AIH-TASK-153 改这里）
              ① 结算 UPDATE（不变，带 attempt_count）
              ② 规则 → 草稿 → 查租户 id、email、company_name → INSERT notifications（不变）
              ③ 新：同一事务 INSERT domain_outbox（NOTIFICATION_EMAIL_REQUESTED，PENDING）
              ④ 提交（①②③ 原子）
        │
recover（下一个周期）──► deliver(email_id)：领取（不变）
        └─ 邮件类：渲染（AIH-TASK-152 新增渲染函数）→ Adapter → SMTP
              → _settle（AIH-TASK-152 改：带 attempt_count；仍清空 payload）
```

### 新事件类型（AIH-TASK-152）

- 常量 `EVENT_NOTIFICATION_EMAIL: Final = "NOTIFICATION_EMAIL_REQUESTED"`，定义在 `app/tasks/outbox.py`（生产者与处理器都在这个文件里，不另开模块）。命名沿用邮件类事件的大写形式（`PASSWORD_RESET_REQUESTED`、`PORTAL_INVITATION_REQUESTED`）。
- `aggregate_type = "notification"`，`aggregate_id` = 那条通知的 `public_id`（36 字符，列宽 64）。
- 加进 `_RENDERERS`，因而自动进入 `handled_event_types()`：`recover` 会扫它、`outbox_backlog` 的积压与死信两部分都会数它（同一份名单，#471 v7 §2「投递」）。
- **payload**（由 153 写、152 读；键恰好这 6 个）：

  | 键 | 值 | 来源 |
  | --- | --- | --- |
  | `to` | 字符串 | 结算事务里读到的 `tenants.email` |
  | `company_name` | 字符串 | 结算事务里读到的 `tenants.company_name` |
  | `notification_type` | `"LOW_BALANCE"` / `"SUSPENDED"` / `"REACTIVATED"` | 草稿的 `notification_type` |
  | `balance` | 恰好 8 位小数的定点字符串 | 草稿的 `balance`：`format(quantize_money(value), "f")`（`quantize_money` 来自 `app/models/base.py`；草稿金额本来最多 8 位小数，量化不改变数值，只把位数补齐） |
  | `threshold` | 同上的定点字符串，或 `null` | 只有 `LOW_BALANCE` 非空，写法同 `balance` |
  | `occurred_at` | `YYYY-MM-DDTHH:MM:SS` | 草稿的 `occurred_at`（outbox 行的 `created_at`，不带时区的 UTC、整秒），`isoformat()` |

  `to` 与 `company_name` 是**结算时**的快照，不是事件发生时的：联系邮箱在事件与结算之间（通常不到 60 秒）被改，信发到新地址，接受。

### 渲染（AIH-TASK-152，`app/tasks/outbox.py`）

新增 `_render_notification_email(settings, payload) -> OutboundMessage | None`，登记为 `_RENDERERS[EVENT_NOTIFICATION_EMAIL]`。返回 `None`（按投递失败处理：重试，次数用尽进死信）的情况，每种记一条固定文本的 error 日志、不带任何 payload 值：

- 前端基址为空（与重置信、邀请信同一判定与理由）。
- `to` 不是非空字符串，或含 `\r` / `\n`（列已经 `EmailStr` 校验，这一条只是不把 payload 原样塞进信头）。
- `company_name` 不是非空字符串。
- `notification_type` 不是三个已知值之一。
- `balance` 不是合法金额；`LOW_BALANCE` 的 `threshold` 不是合法的非负金额；其他两类的 `threshold` 不是 `null`。合法金额的判定**复用** `app/services/notifications.py` 的 `_money`：把它改为公开名 `parse_money`（签名与行为不变，模块内原调用处同步改名），渲染函数调用它。
- `occurred_at` 不能被 `datetime.fromisoformat` 解析。
- payload 的键集合不恰好是上表 6 个。

链接：`f"{base}/portal/wallet"`（与重置信同一个 `base` 取法：`strip().rstrip("/")`）；不带查询参数。路径必须与 `frontend/src/routes/paths.ts` 的 `portalWallet` 相同，由测试从 `paths.ts` 取字面量比对（同 `tests/backend/test_invitation_link.py` 的做法）。钱包页要登录，客户点开后先登录，接受。

模板参数（全部是字符串）：

| 参数 | 值 |
| --- | --- |
| `company_name` | payload 原值 |
| `balance` | 显示金额（见下） |
| `threshold` | 显示金额，**只有低余额模板有这个参数** |
| `occurred_at` | 马来西亚时间到分钟加 UTC，格式与邀请信的到期时刻完全相同：把 `_invitation_deadline` 改名为 `_malaysia_time`（行为不变，邀请信调用处同步），通知邮件也调用它 |
| `link` | 上面的钱包页链接 |

模板键按 `notification_type` 选：`LOW_BALANCE` → `TEMPLATE_NOTIFICATION_LOW_BALANCE = "notification_low_balance"`；`SUSPENDED` → `TEMPLATE_NOTIFICATION_SUSPENDED = "notification_suspended"`；`REACTIVATED` → `TEMPLATE_NOTIFICATION_REACTIVATED = "notification_reactivated"`。

**显示金额**（spec §80「MYR normal display → 2 decimals」；与门户 `formatAmountRounded(amount, 2)` 同一规则）：`Decimal` 先 `quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)`，再按千分位格式化成两位小数；量化结果为 0 时一律显示 `0.00`（不显示 `-0.00`）。⚠️ 不能只用 `format(value, ",.2f")`：`Decimal.__format__` 按上下文舍入，默认 `ROUND_HALF_EVEN`，`"0.125"` 会显示成 `0.12`。示例：`"1234.56500000"` → `1,234.57`；`"-0.00400000"` → `0.00`；`"-12.50000000"` → `-12.50`。只改显示，不改任何持久值。函数放在 `app/tasks/outbox.py`（`_display_money`），不新建模块。

### 模板（AIH-TASK-152，`app/core/email_templates.py`）

三个新模板，标题固定（**不含**公司名或任何 payload 值，信头不受数据影响），正文如下（`$` 占位符由 `string.Template.substitute` 替换；不用 `safe_substitute`，沿用 151）：

`notification_low_balance`，必需参数 `company_name`、`balance`、`threshold`、`occurred_at`、`link`：

```text
Subject: Your Acuven Billing balance is low

The prepaid balance of the Acuven Billing account for $company_name has fallen to RM $balance, at or below your low-balance threshold of RM $threshold.

This was recorded at $occurred_at.

AI services billed to this account are suspended when the balance reaches RM 0.00. To top up, please contact your Acuven contact.

You can check your balance in the customer portal:
$link
```

`notification_suspended`，必需参数 `company_name`、`balance`、`occurred_at`、`link`：

```text
Subject: Your Acuven Billing account is suspended

The prepaid balance of the Acuven Billing account for $company_name reached RM $balance at $occurred_at, so AI services billed to this account are now suspended.

They resume automatically once the balance is above RM 0.00. To top up, please contact your Acuven contact.

You can check your balance in the customer portal:
$link
```

`notification_reactivated`，必需参数 `company_name`、`balance`、`occurred_at`、`link`：

```text
Subject: Your Acuven Billing account is active again

The prepaid balance of the Acuven Billing account for $company_name is RM $balance as of $occurred_at, so AI services billed to this account have resumed.

You can check your balance in the customer portal:
$link
```

正文里 `Subject:` 那一行与其后的空行不属于正文：`Subject:` 后面是模板的 `subject`，空行之后到代码块末尾是 `body`；段落之间恰好一个空行（`\n\n`），正文末尾没有换行。`company_name` 里的 `$` 不会被再次解释（`substitute` 只替换模板里的占位符）。

### 写邮件 outbox 行（AIH-TASK-153，`app/tasks/outbox.py` `_settle_notification`）

- 规则给出草稿时，租户查询从只取 `Tenant.id` 改为取 `Tenant.id`、`Tenant.email`、`Tenant.company_name`（同一条 SELECT，不加锁，与今天一样）；查不到仍按无效处理。
- `_insert_notification` 之后、提交之前，同一个会话 `session.add(DomainOutbox(...))`：`event_type = EVENT_NOTIFICATION_EMAIL`，`aggregate_type = "notification"`，`aggregate_id = 通知的 public_id`，`payload_json = json.dumps(上表 6 键, sort_keys=True)`（与 `app/repositories/wallet.py` `_json` 同一写法），`status = PENDING`，`attempt_count = 0`，`created_at = next_retry_at = now`（就是这个事务的 `now`，与通知行的 `created_at` 相同）。`flush` 失败按事务内异常处理（整个事务回滚）。
- **「不出通知」与「payload 无效」时不写**（只有插入了通知行才写邮件行）。
- 不 `delay()` 新行：与通知类事件一样只靠 `recover` 的 60 秒周期触发（#471 v7 §2「投递」最后一条）。所以一封通知邮件最晚约在事件之后两个恢复周期发出。
- `_settle_notification` 的返回值不变（插入了通知仍返回 `notified`）。
- 写 payload 的金额按上表「`balance`」一行的写法；不从 `app/repositories/wallet.py` 导入它的私有 `_money_text`。

### 邮件类结算带上领取次数（AIH-TASK-152，`app/tasks/outbox.py` `_settle`）

`_settle(factory, outbox_id, *, status)` 改为 `_settle(factory, claim, *, status)`（`claim` 是 `deliver` 已经构造好的 `_Claim`），UPDATE 的条件加上 `attempt_count = claim.attempt`：

```sql
UPDATE domain_outbox SET status = :status, processed_at = :now, payload_json = NULL
WHERE id = :id AND status = 'PENDING' AND attempt_count = :attempt
```

- 受影响 1 行：与今天相同。
- 受影响 0 行（这一行已被更晚的一次领取接手，或已结算）：什么都不做；成功分支 `deliver` 仍返回 `sent`；死信分支**不记** error 日志，返回 `superseded`（与通知类死信结算的返回值一致）。
- 对密码重置与门户邀请同样生效：单个领取者时行为完全不变；有交错时，旧领取者不再能把新领取者正在投的行置为 `FAILED` 或 `SENT`，行由新领取者结算。`_settle` 只有 `deliver` 一个调用方。
- 这是 #471 v7 §10 记下的已知缺口；通知邮件是第一类客户被动收到、且会进死信告警的邮件，与它一并修。

### 时间

- 邮件里的时刻 = `notifications.occurred_at`（outbox 行的 `created_at`），不取发信时刻：重试晚几分钟或几小时，信里的时刻不变。
- 邮件 outbox 行的 `created_at` / `next_retry_at` = 结算事务的 `now`（UTC、整秒，`_now()`）。

### 金额

- 只显示，不运算：payload 定点字符串 → `Decimal(str)` → 量化到 2 位显示。不经 float。

## 3. 不变量影响矩阵

| 不变量 | 是否触碰 | 可能怎样被破坏 | 设计控制 | 验证方式 |
| --- | --- | --- | --- | --- |
| INV-1 中心故障不中断 AI | 否 | 发信失败拖慢记账 | 发信在 worker 里、通知结算提交之后另一行 outbox；`post_transaction` 不改 | 不改代码路径 |
| INV-2 事件不重复扣费 | 否 | — | 不碰用量与记账 | 不改代码 |
| INV-3 支付不重复入账 | 否 | — | 不碰支付 | 不改代码 |
| INV-4 余额只经账本变动 | 否 | 新代码误写钱包 | 新代码只写 `domain_outbox` | §7 正常路径断言钱包与账本行不变 |
| INV-5 历史账本不可变 | 否 | — | 不写 `wallet_transactions` | 同上 |
| INV-6 事件保留版本引用 | 否 | — | 不碰用量事件 | 不改代码 |
| INV-7 客户不可见成本毛利 | 是 | 邮件带出内部字段 | payload 恰好 6 键，只有余额、阈值、时刻、公司名、收件人、类型；模板只用其中 5 个 | §7「payload 形状」「渲染逐字」 |
| INV-8 租户不可互访 | 是 | 信发到别的租户的邮箱，或带别的租户的余额 | 收件人、公司名与金额在同一个结算事务里，由同一条 outbox 行的 `aggregate_id` 决定的租户读出；邮件行之后不再查租户 | §7「两个租户」 |
| INV-9 对话内容不入库 | 否 | — | 邮件不含对话内容 | payload 键集合 |
| INV-10 金额用 Decimal | 是 | 显示金额经 float 或 HALF_EVEN 舍入 | `Decimal(str)` + `quantize(ROUND_HALF_UP)` | §7「显示金额」 |
| INV-11 event_id 至多一次财务效果 | 否 | — | 邮件没有财务效果 | — |
| INV-12 定稿对账单不可变 | 否 | — | 不碰对账单 | — |
| INV-13 状态与事件原子提交 | 是 | 通知写了而邮件行没写（丢信），或邮件行写了而通知没写（重复信） | 结算 UPDATE、通知 INSERT、邮件行 INSERT 同一事务；`notifications.source_outbox_id` 唯一，所以一条通知类 outbox 行最多一条通知、因而最多一行邮件行 | §7「事务中途失败」「并发」「幂等重放」 |
| INV-14 队列丢失不毁持久工作 | 是 | 邮件只靠队列触发，Redis 丢失后不发 | 邮件行在库里，类型进 `handled_event_types()`，`recover` 扫它；领取推后 `next_retry_at` | §7「Redis/Celery 丢失」 |

## 4. 状态与并发

**邮件 outbox 行（`NOTIFICATION_EMAIL_REQUESTED`，与其他邮件类事件同一套）**

| 当前状态 | 事件 | 前置条件 | 新状态 | 副作用 | 非法时结果 |
| --- | --- | --- | --- | --- | --- |
| （无） | 通知结算事务插入通知 | 规则给出草稿 | `PENDING`（`attempt_count` 0，立即到期） | — | 事务回滚则不存在 |
| `PENDING` | 领取 | `next_retry_at <= now`、`attempt_count` 未变 | `PENDING`（`attempt_count + 1`，`next_retry_at` 推后） | 无 | `not-claimed` |
| `PENDING` | 发信成功 | `attempt_count` 仍等于本次领取的值 | `SENT`，payload 置空 | 一封信 | 受影响 0 行 → 不变（由新领取者结算），返回 `sent` |
| `PENDING` | 渲染失败或发信失败，次数未用尽 | — | `PENDING`（不变） | warning 日志 | — |
| `PENDING` | 同上，次数用尽 | `attempt >= outbox_max_attempts`；`attempt_count` 仍等于本次领取的值 | `FAILED`，payload 置空 | error 日志 | 受影响 0 行 → 不变，`superseded`，不记 error |
| `SENT` / `FAILED` | 任何 | — | 不变 | 无 | `already-settled` |

- **串行化**：与 #471 v7 §4 相同：领取靠 `attempt_count` 乐观锁；成功与死信两种结算都带 `attempt_count = :attempt`，只有当前领取者能结算。
- **数据库的唯一性保证**：`notifications.source_outbox_id` 唯一；邮件行与通知行同事务插入，所以「一条通知类 outbox 行 → 最多一条通知 → 最多一行邮件行」。不给 `domain_outbox` 加唯一约束（不加迁移）。
- **幂等键**：通知类 outbox 行的 `id`（经 `source_outbox_id` 唯一）；邮件行自身的 `id`。
- **至少一次**：发信在事务外，「信已发出、结算之前崩溃」或「旧领取者停顿期间新领取者也发了」都会让同一行发第二封，内容逐字相同。这是逻辑通知之下的投递重复，不是第二条逻辑通知（spec §48 的要求针对后者），与密码重置、邀请信同一取舍（`app/tasks/outbox.py` 模块说明）。
- **重试层**：只在 outbox 层。
- **必须原子提交**：通知类 outbox 结算 + 通知插入 + 邮件行插入。

## 5. 失败与恢复矩阵

| 失败点 | 对外结果 | 数据最终状态 | 是否可重试 | 恢复来源 | 告警 |
| --- | --- | --- | --- | --- | --- |
| 结算事务里插入邮件行失败（DB 断开、死锁、flush 异常） | 门户通知与邮件都晚出现 | 整个事务回滚：通知类 outbox 仍 `PENDING`，无通知行，无邮件行 | 是 | 下一次领取 | 次数用尽进死信，`outbox_backlog` P2 |
| payload 不合法（只可能来自代码缺陷） | 不发信 | 邮件行重试至上限后 `FAILED`，payload 置空；门户通知不受影响 | 自动到上限，之后人工 | 人工按 runbook「outbox 死信的处置」 | 死信 P2 |
| 前端基址为空 | 不发信 | 同上 | 同上 | 配好基址后人工 | 积压 P2，之后死信 P2 |
| SMTP 未配置或拒收 / 超时 | 晚发或不发 | 同上（传输层返回 False，不抛） | 是，退避重试 | 配置或网络恢复 | 积压 P2，之后死信 P2 |
| worker 在发信之后、结算之前崩溃 | 可能收到两封相同的信 | 行仍 `PENDING`，下次领取再发一次后 `SENT` | 是 | `recover` | 无 |
| 旧领取者停顿，行被更晚的领取接手 | 可能两封相同的信 | 旧领取者的结算受影响 0 行；行由新领取者结算 | — | — | 旧领取者不记 error |
| Redis/Celery 丢失 | 晚发 | 库里的 `PENDING` 行不受影响 | 是 | broker 恢复后 `recover` | 积压 P2 |
| 重复投递 | 一行邮件行；至少一次发信 | 后到者 `already-settled` 或 `not-claimed` | — | — | 无 |
| 滚动更新：worker 还是 152 之前的镜像 | 不发信 | 邮件行不在旧名单里，以 `PENDING` 持久等待（旧 `recover` 不扫、旧告警不数） | — | 新 worker 起来后接手 | 无 |
| 回滚：153 → 152 镜像 | 新通知不再生成邮件行 | 已生成的邮件行照常由 152 投递 | — | — | — |

## 6. 数据与安全边界

- **租户过滤**：收件人、公司名、金额都在通知结算事务里，由通知类 outbox 行的 `aggregate_id`（租户 `public_id`）查出的同一个租户提供；邮件行的渲染只用自己的 payload，不再查库，不可能混入别的租户。
- **鉴权**：不新增接口。
- **客户侧禁止出现的字段**：邮件只含公司名、余额、阈值、事件时刻与固定链接；不含 `tenant_id`、`public_id`、outbox 或流水标识、`status_version`、`reason`，没有成本、加价或毛利（INV-7）。
- **日志**：渲染失败与投递日志只带 `outbox_id`、`event_type`、尝试次数、模板键与通道（151 的 Adapter 约定）；不带收件人、公司名、金额。
- **个人数据**：`to`（联系邮箱）是个人数据（REQ-PRIV-001）。它在邮件行的 payload 里只活到结算：`_settle` 在 `SENT` 与 `FAILED` 时都把 payload 置空（不变）；死信排查靠通知行与租户行，不靠 payload。runbook「outbox 死信的处置」写明：通知邮件进死信后 payload 已空，按 `aggregate_id` 找到通知、再找到租户。
- **信头**：标题固定，不含任何 payload 值；收件人含 CR / LF 时不发（§2「渲染」）。
- **密钥**：不涉及（SMTP 凭据沿用 ADR-0014 的现有配置）。
- **数据保留**：不新增持久数据（邮件行的 payload 结算即清空，行本身随 outbox 保留）。

## 7. 测试证据计划

| 风险/需求 | 测试层级 | 场景 | 预期结果 |
| --- | --- | --- | --- |
| 渲染逐字（152） | unit | 三类 payload 各一（固定基址、公司名、金额、时刻），经 `_render_notification_email`、真实 `EmailAdapter` 与记录型假 transport | `to`、`subject`、`body` 与测试里的字面量逐字相等（字面量按 §2「模板」写出） |
| 显示金额（152） | unit | `"0.125"` → `0.13`（区分 HALF_UP 与 HALF_EVEN）、`"1234.56500000"` → `1,234.57`、`"-0.00400000"` → `0.00`、`"-12.50000000"` → `-12.50`、`"999999999999.99999999"` → `1,000,000,000,000.00` | 全部如左；先断言 `format(Decimal("0.125"), ".2f") == "0.12"`（用例有区分力） |
| 渲染失败（152） | unit | 基址为空；`to` 为空 / 非字符串 / 含 `\n`；`company_name` 为空；未知类型；`balance` 为 `"1e3"`；`LOW_BALANCE` 缺 `threshold` 或为负；`SUSPENDED` 带 `threshold`；`occurred_at` 不能解析；多一个键；少一个键 | 都返回 `None`；error 日志不含收件人、公司名与金额（caplog） |
| 链接守卫（152） | unit | 从 `paths.ts` 取 `portalWallet` 字面量 | 渲染出的链接路径与它相等；无查询参数 |
| 邮件类型进名单（152） | unit | `handled_event_types()` | 恰好五类：两类通知事件、三类邮件事件（改写 `test_outbox.py` 里的集合断言） |
| 投递正常路径（152） | integration | 手工插入一行合法的 `NOTIFICATION_EMAIL_REQUESTED`，跑 `recover` 与 `deliver` | 一封信；行 `SENT`、payload 为 NULL |
| 投递失败到死信（152） | integration | 假 transport 恒返回 False，`outbox_max_attempts` 2 | 第一次 `retry-later`、第二次 `dead-lettered`；行 `FAILED`、payload 为 NULL |
| 邮件结算带领取次数（152） | integration | `outbox_max_attempts` 2；worker A 以 `attempt_count` 2 领取一行密码重置信后暂停；把 `next_retry_at` 拨到过去，worker B 以 3 领取；A 发信失败走死信；B 发信成功结算 | A 返回 `superseded`、不记 error、行未变；B 结算为 `SENT`；同样的交错让 A 成功、B 随后结算：A 的结算受影响 0 行，行由 B 置为 `SENT` |
| 现有两封信不回归（152） | integration | 现有 `test_outbox.py`、`test_notification_adapter.py`、`test_invitation_link.py`、`test_password_reset_link.py` | 不改断言即通过（`test_password_reset_link.py` 不在 Worker 可改范围内，必须原样通过） |
| 正常路径：三类（153） | integration | 预付租户阈值 50、余额 60 → 记 -15；再记 -45（停机）；再记 +5（复机）；每步跑 `recover` 两轮 | 三条通知；三行邮件行，`aggregate_id` 依次是三条通知的 `public_id`；三封信的收件人是该租户的 `email`、类型与余额对应；钱包与账本行不变 |
| payload 形状（153） | integration | 上一行的邮件行 payload | 恰好 6 键；金额是 8 位小数定点字符串；`occurred_at` 等于通知行的 `occurred_at` |
| 内部计量租户（153） | integration | `INTERNAL_METERED_ONLY` 租户的 `billing_status_changed` | 没有通知行，没有邮件行 |
| payload 无效（153） | integration | 一行无效的通知类 outbox 行 | 没有通知行，没有邮件行（与 #471 的失败分支同一结局） |
| 事务中途失败（153） | integration | 让邮件行的 `flush` 抛错（替换 `session.add` 之后的 flush，或插入一行违反约束的数据） | 通知类 outbox 仍 `PENDING`、无通知行、无邮件行 |
| 并发（153，MySQL） | integration | #471 的「两个会话同时结算」用例 | 一条通知、一行邮件行 |
| 旧领取者成功与新领取交错（153） | integration | #471 的同名用例 | 恰好一条通知、一行邮件行 |
| 幂等重放（153） | integration | 同一通知类行 `deliver` 两次；已 `SENT` 的再 `deliver` | 一条通知、一行邮件行 |
| 两个租户（153） | integration | A、B 两个预付租户同一轮各跨一次阈值 | 两行邮件行，各自的 `to`、`company_name`、金额来自各自的租户 |
| 不补发（153） | integration | 预置一条已存在的通知（对应的通知类 outbox 行已 `SENT`），跑 `recover` | 不产生邮件行 |
| Redis/Celery 丢失（153） | integration | 不调 `delay`，只调 `recover` 两轮 | 通知与邮件都出现 |
| 既有通知测试（153） | integration | `test_notification_delivery.py` 里计数 outbox 行、或让所有到期行结算的用例 | 随邮件行的出现调整断言，意义不变（例如只数通知类行；或断言恰好多出对应的邮件行） |

## 8. 迁移与上线

- **数据迁移步骤**：无。不加表、不加列、不回填。
- **锁表与性能影响**：每条通知多一行 outbox 插入与一次投递；量与状态跃迁次数成正比（每个租户每天至多几条）。
- **兼容窗口与部署顺序**：152 → 153。152 上线后有处理器而没有生产者（库里没有这类行），不发任何信；153 上线后，从下一次结算的通知开始生成邮件行。
- **不补发**：已经存在的通知不会补出邮件行（邮件行只在插入通知的那个事务里产生，迁移与回填都没有）。153 上线时仍 `PENDING` 的通知类行（生产上它们在约 60 秒内就会被结算，正常情况下没有积压）结算时会照常生成邮件行：它们是上线前一刻发生的事件，信里的时刻就是事件时刻。
- **上线前置（管理员，153 部署之前）**：确认生产已配置 SMTP 并能投递（ADR-0014 的落地项：发件邮箱已开通、发件域名的 SPF / DKIM 已配置），且 `BILLING_FRONTEND_BASE_URL` 已配置。否则 153 一上线，每条通知邮件都会重试、进死信并触发 `outbox_backlog` P2。这一步不在任何任务契约里，由管理员在 153 开启之前做并告知 Kelvin。
- **回滚或前滚**：只回滚应用镜像。回滚到 152：不再生成邮件行，已生成的照常投递。回滚到 152 之前：邮件行以 `PENDING` 持久等待，再次前滚后接手（同一封信可能晚到）。
- **部分部署**：只有 worker 的代码变化；API 不读这类行。
- **监控**：沿用 `outbox_backlog`（积压与死信两部分，P2），新类型经 `handled_event_types()` 自动计入；`docs/api.md` 与 `docs/runbook.md` 里列举「有处理器的类型」的句子随 152 补上 `NOTIFICATION_EMAIL_REQUESTED`，runbook 死信处置补一条「通知邮件进死信后 payload 已空，按 `aggregate_id` 找通知」。

## 9. 方案取舍

| 方案 | 优点 | 风险 | 未采用原因 |
| --- | --- | --- | --- |
| 采用：通知结算事务里同事务写一行邮件 outbox，邮件走已有的邮件投递分支 | 复用已有领取、退避、死信、恢复、告警与 payload 清空；邮件失败不牵连门户通知；「一条通知最多一行邮件」由同事务 + 已有唯一约束保证 | 通知邮件最晚约两个恢复周期发出 | — |
| A：通知类行结算时直接发信 | 少一行 outbox | 发信必须在事务外：先发后结算则失败重试会重复插入通知或重发信，先结算后发信则发信失败无处重试；两个通道的失败绑在一起 | 违背 INV-13 的做法，且失败语义更差 |
| B：`post_transaction` 里同时写邮件 outbox 行 | 少一跳 | 改钱包热路径；内部计量租户的「不出通知」判定要搬进记账代码；已存在的通知类 `PENDING` 行要另外处理 | 风险落在最不该动的地方（与 #471 方案 A 同理） |
| C：发给门户账号 | 收件人与登录身份一致 | 没开门户账号的租户收不到；一个客户一个门户账号（spec §52），与联系邮箱多半相同 | Kelvin 2026-11-04 定发租户联系邮箱 |
| D：渲染时再读 `tenants.email` | 收件人永远是最新的 | 邮件分支今天不开数据库会话（`_attempt_send` 只吃 payload）；要为一类邮件改投递结构 | 结算到发信通常不到 60 秒，快照足够 |
| E：加 `notification_deliveries` 表记每个通道的投递 | 将来 WhatsApp 回执有处可放 | 一张新表、一次迁移、一套状态，现在只有 Email 且回执不接收 | WhatsApp 任务需要回执时再加；outbox 行已足够记录 Email 投递 |
| F：给邮件加新鲜度窗口（事件过了 N 小时不再发） | 长时间故障后不发过时提醒 | 故障恢复后客户静默收不到信，违背 INV-14 的精神；N 无依据 | 信里写明事件时刻，迟到可辨认；接受 |

## 10. 未决问题与假设

- **未决问题**：无。四项范围问题已由 Kelvin 2026-11-04 拍板：只做 Email；收件人是租户联系邮箱；不补发已有通知；邮件带余额、阈值与门户链接。
- **接受的行为**：
  1. 投递至少一次：极少数情况下同一封信收到两次（§4）。
  2. 联系邮箱是结算时的快照（§2）。
  3. 长时间故障后信会迟到，信里的时刻是事件时刻（§9 F）。
  4. 账户状态（`DISABLED` / `CLOSED`）不影响发信，与门户通知一致：通知规则本来就不看账户状态。
- **尚未验证的假设**：
  1. 生产 SMTP 已配置且能投递到外部邮箱（§8「上线前置」核对）。
  2. `BILLING_FRONTEND_BASE_URL` 在生产已配置（门户邀请信依赖它）。
- **需要谁拍板**：无。
- **如果假设错误，具体后果**：假设 1 或 2 不成立 → 通知邮件全部进死信、`outbox_backlog` P2；门户通知不受影响；配好之后新通知照常发信，已进死信的不会自动重发。

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
| AIH-TASK-152 | 投递 | 事件类型常量、三个模板、渲染函数与显示金额、`_settle` 带领取次数、`parse_money` 与 `_malaysia_time` 改名、文档、测试 | 151 |
| AIH-TASK-153 | 业务规则 | 通知结算事务里写邮件行、测试 | 152 |

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
| v1 | 2026-11-04 | 初稿 | — |

# API 契约

> 本文件记录**已经实现**的 HTTP 接口的请求、响应与错误契约。接口改了，同一个 PR 里改这里。
> 设计依据写在每一节开头；设计与本文件不一致时以已批准的设计为准，并修正本文件。

目前收录：

- [管理端客户管理](#管理端客户管理)（AIH-TASK-006）
- [管理端手工调账](#管理端手工调账)（AIH-TASK-011）
- [管理端集成 API 凭据](#管理端集成-api-凭据)与[集成请求签名](#集成请求签名)（AIH-TASK-012）
- [管理端出站 webhook 签名密钥](#管理端出站-webhook-签名密钥)与[状态 webhook 签名](#状态-webhook-签名)（AIH-TASK-019）
- [管理端审计日志](#管理端审计日志)（AIH-TASK-022）
- [管理端供应商价格](#管理端供应商价格)（AIH-TASK-026）
- [管理端汇率](#管理端汇率)（AIH-TASK-041）
- [管理端试算预览](#管理端试算预览)（AIH-TASK-031）
- [管理端用量事件：重新入队](#管理端用量事件重新入队)（AIH-TASK-032）
- [内部告警接口](#内部告警接口)（AIH-TASK-042）：机器接口，**不套信封**，是下面通用约定的例外
- [集成：用量事件摄取](#集成用量事件摄取)（AIH-TASK-029）：集成签名认证，错误信封多一个顶层 `retryable`

认证接口（`/api/v1/auth/*`）早于本文件，契约暂时只在代码与设计闸门 #32 里，之后补进来。

---

## 通用约定

### 信封（spec §107）

每个响应都是同一个形状，成功与失败都一样：

```json
{
  "success": true,
  "data": { "...": "..." },
  "error": null,
  "request_id": "7d3c0b8e-..."
}
```

- 成功时 `error` 为 `null`；失败时 `data` 为 `null`，`error` 是 `{code, message}`。
- 客户端按 `error.code` 分支。`message` 给人看，随时可能改，从不含栈、SQL 或内部标识。
- `request_id` 同时出现在响应头 `X-Request-ID` 里，报障时带上它。

### 鉴权

- 访问令牌放在 `Authorization: Bearer <token>` 头里。令牌由 `/api/v1/auth/login`（ADMIN 还要过
  `/api/v1/auth/login/totp`）签发，前端只放在内存里。
- 浏览器不会跨站自动附带这个头，所以这些接口不另加 CSRF 令牌（spec §96）。
- 管理端接口（`/api/v1/admin/*`）只有 ADMIN 能调。**角色以数据库为准**：令牌里的 `role`
  不被信任，把账号降成 CUSTOMER 或停用都立即生效，不等令牌过期。

### 通用错误码

| HTTP | `error.code` | 什么时候 |
| --- | --- | --- |
| 401 | `TOKEN_INVALID` | 没有令牌、令牌格式不对、签名或有效期不对、拿 2FA 的 pending 令牌来调、账号不存在或已停用 |
| 403 | `ADMIN_REQUIRED` | 令牌有效，但调用者在数据库里不是 ADMIN |
| 422 | `VALIDATION_ERROR` | 请求体、查询参数不合法，或请求体里有未声明的字段。`message` 只列字段名，不回显值 |
| 503 | `DATABASE_NOT_CONFIGURED` | 服务没有配置数据库 |
| 500 | `INTERNAL_ERROR` | 意外错误。固定文案，细节只进服务端日志（日志里没有 SQL 参数） |

⚠️ 请求体与查询参数在鉴权**之前**校验：没带令牌、参数又不合法的请求得到 422，不是 401。

### 标识

- 对外的 `id` 一律是 `public_id`（uuid4 的 36 字符字符串）。内部自增 id 不出现在任何请求或响应里。
- 路径里的 id 不存在时是 404；拿内部自增 id、别的实体的 id 或随手编的串来调，得到的 404 一模一样。

### 金额（INV-10）

金额是**字符串**，恰好 8 位小数，例如 `"0.00000000"`、`"12.50000000"`，从不是 JSON 数字。
客户端按十进制解析，不要转成浮点数。

### 时间

`created_at` / `updated_at` 是不带时区的 UTC，ISO 8601，精确到秒，例如 `"2026-09-20T08:30:00"`
（spec §109）。展示层再换成 Asia/Kuala_Lumpur。

### 分页（spec §108）

列表接口收两个查询参数：

| 参数 | 取值 | 默认 |
| --- | --- | --- |
| `page` | 1–10000 | 1 |
| `page_size` | 1–100 | 20 |

超出范围是 422，不静默截断。响应的 `data` 是：

```json
{ "items": [], "page": 1, "page_size": 20, "total": 0 }
```

`total` 是总条数。页码超过末页时 `items` 为空，`total` 照报。

---

## 管理端客户管理

设计依据：设计闸门 #96 `APPROVED: design v3`，全文见 [design/AIH-TASK-006-admin-customers.md](design/AIH-TASK-006-admin-customers.md)（spec §56、§57、§124）。

七个接口都在 `/api/v1/admin` 下，都要 ADMIN。编辑客户是 AIH-TASK-009 加的，改账户状态是
AIH-TASK-020 加的（见[下一节](#管理端账户状态)），其余五个来自设计闸门 #96。

| 方法与路径 | 成功 | 错误 |
| --- | --- | --- |
| `POST /api/v1/admin/customers` | 201，客户详情 | 401 / 403 / 422 / 503 |
| `GET /api/v1/admin/customers` | 200，客户分页 | 401 / 403 / 422 / 503 |
| `GET /api/v1/admin/customers/{customer_id}` | 200，客户详情 | 401 / 403 / 404 / 503 |
| `PATCH /api/v1/admin/customers/{customer_id}` | 200，客户详情 | 401 / 403 / 404 / 422 / 503 |
| `POST /api/v1/admin/customers/{customer_id}/account-status` | 200，客户详情 | 401 / 403 / 404 / 409 / 422 / 500 / 503 |
| `POST /api/v1/admin/customers/{customer_id}/projects` | 201，项目 | 401 / 403 / 404 / 422 / 503 |
| `GET /api/v1/admin/customers/{customer_id}/projects` | 200，项目分页 | 401 / 403 / 404 / 422 / 503 |

`{customer_id}` 是客户的 `id`（`public_id`）。本任务专有的错误码：

| HTTP | `error.code` | 什么时候 |
| --- | --- | --- |
| 404 | `CUSTOMER_NOT_FOUND` | 路径里的客户不存在 |

### 客户对象

**客户详情**（建客户与查单个客户的响应）：

```json
{
  "id": "3f0e6c1a-8d4b-4c6e-9a51-2b7f0d9c4e11",
  "company_name": "Acme Sdn Bhd",
  "contact_name": "Contact Person",
  "email": "ops@example.com",
  "phone": "+60 3-0000 0000",
  "billing_status": "SUSPENDED",
  "account_status": "ENABLED",
  "status_version": 0,
  "created_at": "2026-09-20T08:30:00",
  "updated_at": "2026-09-20T08:30:00",
  "wallet": { "currency": "MYR", "balance": "0.00000000", "version": 0 }
}
```

| 字段 | 说明 |
| --- | --- |
| `id` | 客户的 `public_id` |
| `company_name` | 公司名 |
| `contact_name` / `phone` | 可为 `null` |
| `email` | 联系邮箱，不是登录账号，不要求唯一 |
| `billing_status` | 由余额驱动：`ACTIVE`（余额 > 0）或 `SUSPENDED`（余额 ≤ 0）。新客户余额为 0，所以是 `SUSPENDED` |
| `account_status` | 管理员控制的账户生命周期，与 `billing_status` 互相独立：`PENDING_ACTIVATION`、`ENABLED`、`DISABLED`、`CLOSED` 之一。新客户与迁移前已有的客户都是 `ENABLED`；现在只有 `ENABLED` / `DISABLED` 会出现（AIH-TASK-020） |
| `status_version` | 计费状态或账户状态每真实跃迁一次 +1，两维共用、只增不减。新客户是 0 |
| `wallet.currency` | V1 只有 `MYR` |
| `wallet.balance` | 8 位小数的字符串 |
| `wallet.version` | 钱包的账本序号，新钱包是 0 |

**客户列表项**：客户详情去掉 `wallet`。

响应里只有上面这些字段：不含内部自增 id、低余额阈值，也不含任何成本或毛利。

### 项目对象

```json
{
  "id": "9b1d7e0c-5a2f-4d8e-b6c3-0e4f1a2b3c4d",
  "name": "Chatbot",
  "description": null,
  "created_at": "2026-09-20T08:31:00",
  "updated_at": "2026-09-20T08:31:00"
}
```

`description` 可为 `null`。

### `POST /api/v1/admin/customers` —— 建客户

请求体：

| 字段 | 必填 | 规则 |
| --- | --- | --- |
| `company_name` | 是 | 去掉首尾空白后长度 1–255 |
| `email` | 是 | 合法邮箱，长度不超过 320 |
| `contact_name` | 否 | 不超过 255 |
| `phone` | 否 | 不超过 32 |

- 可选字段去掉首尾空白后是空串的，存成 `null`。
- 其他字段一律 422：请求体不能指定 `id`、`public_id`、`billing_status`、余额或任何内部 id。

成功：**201**，`data` 是客户详情。同一个事务里建好三样东西，要么全在、要么全不在：

- 客户：`billing_status = SUSPENDED`、`account_status = ENABLED`、`status_version = 0`；
- 一个 MYR 钱包：余额 `"0.00000000"`、版本 0；
- 一条 `CUSTOMER_CREATE` 审计。审计的 `after_state` 只有 `public_id`、`company_name`、
  `billing_status`、`wallet_currency`，不含 email、联系人、电话。

⚠️ **不幂等**：同一请求体提交两次得到两个客户，各有各的钱包（设计 §4 明确接受：没有财务效果，
管理员在列表里看得见）。

### `GET /api/v1/admin/customers` —— 客户列表

查询参数见[分页](#分页spec-108)。`data.items` 是客户列表项，**最新在前**。只读，不写审计。

### `GET /api/v1/admin/customers/{customer_id}` —— 客户详情

成功：**200**，`data` 是客户详情。客户不存在：404 `CUSTOMER_NOT_FOUND`。只读，不写审计。

### `PATCH /api/v1/admin/customers/{customer_id}` —— 编辑客户

部分更新：只改请求体里出现的字段，至少带一个。

| 字段 | 规则 |
| --- | --- |
| `company_name` | 去掉首尾空白后长度 1–255；不能是 `null` |
| `email` | 合法邮箱，长度不超过 320；不能是 `null` |
| `contact_name` | 不超过 255；`null` 或空白表示清空 |
| `phone` | 不超过 32；`null` 或空白表示清空 |

- 空请求体 `{}` 返回 422。
- 其他字段一律 422，包括 `billing_status`、`account_status`、`status_version`、`public_id`、`id`、
  `low_balance_threshold` 和余额。计费状态只随余额变化；账户状态只走
  [`POST …/account-status`](#管理端账户状态)；低余额阈值、调账不走这个接口。
- 客户不存在：404 `CUSTOMER_NOT_FOUND`，什么都不写。

成功：**200**，`data` 是改后的客户详情。钱包只读不写。客户行与一条 `CUSTOMER_UPDATE`
审计同一事务提交，要么都在，要么都不在：

- 审计的 `before_state` 是 `public_id`、`company_name`（改前）；
- `after_state` 是 `public_id`、`company_name`（改后）和 `changed_fields`（实际改动的字段名，按
  `company_name`、`contact_name`、`email`、`phone` 排序）；
- email、联系人、电话的**值**不进审计，改前改后都不进，只在 `changed_fields` 里留字段名（REQ-PRIV-001）。

所有字段都与现有值相同时，返回 200 和当前详情，不写库：不写审计，`updated_at` 也不变。
并发编辑按最后写入为准；客户行在事务内加锁，每条审计的前后状态对应它自己那一次改动。

### `POST /api/v1/admin/customers/{customer_id}/projects` —— 建项目

请求体：

| 字段 | 必填 | 规则 |
| --- | --- | --- |
| `name` | 是 | 去掉首尾空白后长度 1–255 |
| `description` | 否 | 不超过 1000；空白存成 `null` |

- 其他字段一律 422。项目归属只来自路径里的客户，请求体里的 `tenant_id` / `customer_id` 之类被拒。
- 客户不存在：404 `CUSTOMER_NOT_FOUND`，什么都不写。

成功：**201**，`data` 是项目。项目与一条 `PROJECT_CREATE` 审计同一事务提交；审计的
`after_state` 是 `public_id`、`name` 与所属客户的 `tenant_public_id`。同样不幂等。

### `GET /api/v1/admin/customers/{customer_id}/projects` —— 项目列表

查询参数见[分页](#分页spec-108)。`data.items` 是这个客户的项目，**最早在前**；别的客户的项目不会出现。
客户不存在：404 `CUSTOMER_NOT_FOUND`。只读，不写审计。

### 不在本批接口里

低余额阈值配置、出站 webhook、删除客户与项目（财务记录永久保留）、关户（`→ CLOSED`，
见下一节末尾）。账户状态、管理员调账与 API 凭据见后面几节。

---

## 管理端账户状态

设计依据：设计闸门 #136 `APPROVED: design v2`，全文见 [design/AIH-TASK-020-tenant-account-status.md](design/AIH-TASK-020-tenant-account-status.md)（spec §24、§25、§56、§66、§112.1）。实现登记为 AIH-TASK-020。

| 方法与路径 | 成功 | 错误 |
| --- | --- | --- |
| `POST /api/v1/admin/customers/{customer_id}/account-status` | 200，客户详情 | 401 / 403 / 404 / 409 / 422 / 500 / 503 |

只有 ADMIN 能调。`{customer_id}` 是客户的 `id`（`public_id`）。账户状态是管理员控制的那一维，
独立于由余额驱动的计费状态：改账户状态不改计费状态，记账也不改账户状态。本接口专有的错误码：

| HTTP | `error.code` | 什么时候 | 写库 |
| --- | --- | --- | --- |
| 404 | `CUSTOMER_NOT_FOUND` | 路径里的客户不存在 | 否 |
| 409 | `ACCOUNT_STATUS_TRANSITION_INVALID` | 跃迁表里没有这一格：从 `CLOSED` 出去（终态） | 否 |
| 500 | `INTERNAL_ERROR` | 意外错误（含与记账争租户行锁超时）；整个事务回滚 | 否 |

### 请求体

| 字段 | 必填 | 规则 |
| --- | --- | --- |
| `account_status` | 是 | `ENABLED` 或 `DISABLED`，大小写敏感。`PENDING_ACTIVATION` 与 `CLOSED` 不能作为目标（422） |
| `reason` | 是 | 去掉首尾空白后长度 1–255。存的是去掉首尾空白后的值 |

- 其他字段一律 422，包括 `status_version`、`billing_status`、余额、`tenant_id`、`customer_id` 与
  任何 id：客户只来自路径。
- ⚠️ **原因只写业务说明，不写客户的联系人、电话、邮箱等个人数据。**原因只进审计（永久保留），
  不进出站事件、不进应用日志。

### 跃迁表

| 当前状态 | 目标 | 结果 |
| --- | --- | --- |
| `PENDING_ACTIVATION` | `ENABLED` / `DISABLED` | 跃迁 |
| `ENABLED` | `DISABLED` | 跃迁 |
| `DISABLED` | `ENABLED` | 跃迁 |
| `CLOSED` | 任何目标 | 409 `ACCOUNT_STATUS_TRANSITION_INVALID` |
| 任何状态 | 与当前相同 | 200，不写 |

现在没有任何路径会产生 `PENDING_ACTIVATION`（V1 没有自助注册）或 `CLOSED`（关户另立任务）；
表按全部取值写全，保证关户上线后本接口也不会把已关的户重开。

### 语义

- **跃迁**：200，`data` 是改后的客户详情。同一个事务里：客户行锁内改 `account_status`、
  `status_version` +1、`updated_at`；一条 `TENANT_ACCOUNT_STATUS_CHANGED` 审计；一条
  `tenant.account_status_changed` 出站事件。任何一步失败（包括提交失败）整体回滚，状态与版本不变。
- **目标等于当前状态**：200，返回当前详情，什么都不写（不写审计、不加版本、不写事件，
  `updated_at` 也不变）。所以超时重发是安全的；原因不同的重发按同一目标处理，保留第一次的审计。
- 只锁客户（租户）行，不读也不改钱包。与记账并发时两者在租户行锁上串行，`status_version`
  严格递增、不重复。
- 审计：操作者是调用的管理员（带 ip 与 user agent），`entity_type = tenant`、`entity_id` 是客户的
  `public_id`；`before_state` / `after_state` 只有 `account_status` 与 `status_version`；`reason`
  是请求里的原因。
- 出站事件：`aggregate_type = tenant`、`aggregate_id` 是客户的 `public_id`，状态 `PENDING`
  （现在没有处理器，持久等待；以后由状态 webhook 投递合成有效状态）。payload 只有下面五项，
  **没有原因**：

```json
{
  "account_status": "DISABLED",
  "previous_account_status": "ENABLED",
  "billing_status": "SUSPENDED",
  "status_version": 1,
  "changed_at": "2026-09-28T08:30:00"
}
```

`billing_status` 是跃迁那一刻的计费状态；`changed_at` 与客户的 `updated_at` 相同（UTC，截到整秒）。

不在本接口里：关户（`→ CLOSED`，要等用量事件与支付落地后另立任务）、有效状态（`ALLOW_AI` /
`BLOCK_AI`）与 `reason_code`、项目的 `integration_status`。各状态下摄取、充值、客户登录的行为
是对以后功能的契约，记在 [TODO 历史记录](archive/TODO_RECORDS_2026-09-30.md) 的 AIH-TASK-020 一节。

---

## 管理端手工调账

设计依据：设计闸门 #111 `APPROVED: design v1`，全文见 [design/AIH-TASK-010-admin-wallet-adjustment.md](design/AIH-TASK-010-admin-wallet-adjustment.md)（spec §8、§60、§124）。实现登记为 AIH-TASK-011。

| 方法与路径 | 成功 | 错误 |
| --- | --- | --- |
| `POST /api/v1/admin/customers/{customer_id}/wallet/adjustments` | 201 首次 / 200 重放，调账对象 | 401 / 403 / 404 / 409 / 422 / 500 / 503 |

只有 ADMIN 能调。`{customer_id}` 是客户的 `id`（`public_id`）。本接口专有的错误码：

| HTTP | `error.code` | 什么时候 | 写库 |
| --- | --- | --- | --- |
| 404 | `CUSTOMER_NOT_FOUND` | 路径里的客户不存在 | 否 |
| 409 | `ADJUSTMENT_CONFLICT` | 这个幂等键已经记过，但客户、类型或金额不同 | 否 |
| 422 | `BALANCE_OUT_OF_RANGE` | 这一笔之后余额超出 `DECIMAL(20,8)`（整数部分超过 12 位） | 否 |
| 500 | `INTERNAL_ERROR` | 意外错误，包括客户没有钱包（数据不一致）；整个事务回滚 | 否 |

### 请求体

| 字段 | 必填 | 规则 |
| --- | --- | --- |
| `transaction_type` | 是 | `ADJUSTMENT_CREDIT`、`BONUS`（贷方），`ADJUSTMENT_DEBIT`、`REFUND_ADJUSTMENT`（借方）之一。`TOPUP`、`AI_USAGE`、`SYSTEM_CORRECTION`、rebill 等其他类型 422 |
| `amount` | 是 | **JSON 字符串**，带符号，就是记进账本的那个数。格式：可选的 `-`，1–12 位整数，可选的 `.` 加 1–8 位小数。不能是 0（`"-0.00000000"` 也算 0）。贷方类型必须为正、借方类型必须为负 |
| `reason` | 是 | 去掉首尾空白后长度 1–255。存的是去掉首尾空白后的值 |
| `idempotency_key` | 是 | 小写 uuid（36 字符，带连字符）。前端每次打开调账表单时生成一个，超时重发时带同一个 |

- `amount` 是 JSON 数字、带指数（`"1e2"`）、带 `+`、超过 8 位小数时一律 422，**不舍入**。
- 请求体不能带 `customer_id`、`tenant_id`、`created_by`、`balance`、`metadata` 或任何 id：
  客户只来自路径，操作者只来自令牌。
- ⚠️ **原因只写业务说明，不写客户的联系人、电话、邮箱等个人数据。**原因进账本与审计，两者都
  永久保留、不能删除或改写；它不进应用日志。
- 撤销调账就是再记一笔反向调账，用新的幂等键。账本只追加，旧行不能改。

### 调账对象

```json
{
  "id": "5e2a9d4c-1b3f-4a6e-8c7d-9f0e1a2b3c4d",
  "customer_id": "3f0e6c1a-8d4b-4c6e-9a51-2b7f0d9c4e11",
  "transaction_type": "ADJUSTMENT_CREDIT",
  "amount": "20.00000000",
  "balance_before": "-5.00000000",
  "balance_after": "15.00000000",
  "wallet_sequence": 4,
  "reason": "Goodwill credit for outage 2026-09-20",
  "idempotency_key": "00000000-0000-4000-8000-000000000000",
  "created_at": "2026-09-23T08:30:00",
  "replayed": false,
  "billing_status": "ACTIVE",
  "status_version": 3
}
```

| 字段 | 说明 |
| --- | --- |
| `id` | 账本行的 `public_id` |
| `customer_id` | 客户的 `public_id` |
| `amount` / `balance_before` / `balance_after` | 8 位小数的字符串 |
| `wallet_sequence` | 这一笔在钱包账本里的序号，从 1 起 |
| `reason` | 库里存的原因 |
| `idempotency_key` | 库里存的幂等键 |
| `replayed` | `true` 表示这次是重放：没有新的入账，返回的是第一次那一行 |
| `billing_status` / `status_version` | **提交时**客户的计费状态与版本 |

`id` 到 `created_at` 取自账本行，重放时与第一次逐字相同；`billing_status` / `status_version`
是当时的值，中间有别的记账时可能与第一次不同。响应里没有内部自增 id、`created_by`、
`metadata`、低余额阈值，也没有任何成本或毛利。

### 语义

- **首次**：201。同一个事务里写一行账本（来源 `ADMIN_ADJUSTMENT`，`created_by` 为调用的管理员）
  和一条 `WALLET_ADJUSTMENT_POSTED` 审计（操作者、前后余额、原因、ip、user agent）。
- **跨零**：这一笔让余额从 ≤ 0 变成 > 0，或反过来时，同一事务里还有：计费状态跃迁、
  `status_version` +1、一条 `TENANT_BILLING_STATUS_CHANGED` 审计（操作者是系统，不带 ip 与
  user agent）、一条 `tenant.billing_status_changed` 出站事件。余额正好为 0 也是 `SUSPENDED`。
- **重放**：同一个幂等键、同一个客户、同一个类型与金额：200、`replayed: true`，什么都不写。
  原因不同也按重放处理，返回的 `reason` 是第一次存下的那一份。
- **冲突**：同一个幂等键配了别的客户、类型或金额：409 `ADJUSTMENT_CONFLICT`，什么都不写，
  响应里没有第一次那一笔的任何字段。
- 任何一步失败（包括提交失败）整个事务回滚，账本、审计、计费状态、出站事件都不留下。
  带同一个幂等键重发即可：若其实已经提交，重发走重放。

---

## 管理端集成 API 凭据

设计依据：设计闸门 #118 `APPROVED: design v1`，全文见 [design/AIH-TASK-012-integration-access.md](design/AIH-TASK-012-integration-access.md)（spec §36、§37、§66、§74.4）。实现登记为 AIH-TASK-012。

集成应用后端（以后的摄取端点、Billing Client）用 `api_key` + `secret` 签名请求。这里是管理员为
客户的项目建、列、轮换、吊销凭据的五个接口；签名怎么算见下一节。

| 方法与路径 | 请求体 | 成功 | 错误 |
| --- | --- | --- | --- |
| `POST /api/v1/admin/customers/{customer_id}/projects/{project_id}/credentials` | `{}` | 201，签发的凭据版本 | 401 / 403 / 404 / 422 / 500 / 503 |
| `GET /api/v1/admin/customers/{customer_id}/projects/{project_id}/credentials` | — | 200，凭据版本分页 | 401 / 403 / 404 / 422 / 503 |
| `POST …/credentials/{api_key}/rotate` | `{"current_key_version": int}` | 201，签发的凭据版本 | 401 / 403 / 404 / 409 / 422 / 500 / 503 |
| `POST …/credentials/{api_key}/versions/{key_version}/revoke` | `{"reason": str}` | 200，该版本 | 401 / 403 / 404 / 422 / 500 / 503 |
| `POST …/credentials/{api_key}/revoke` | `{"reason": str}` | 200，该 key 的全部版本（数组，版本从小到大） | 401 / 403 / 404 / 422 / 500 / 503 |

`…` 是 `/api/v1/admin/customers/{customer_id}/projects/{project_id}`。只有 ADMIN 能调。
`{customer_id}`、`{project_id}` 是 `public_id`；`{api_key}` 是凭据的 `api_key`（非机密的查找标识）；
`{key_version}` 是整数。

⚠️ **`secret` 只出现一次**：只在建凭据与轮换的那一次 201 响应里，之后任何接口都不再返回它
（spec §36）。调用方当场保存。五个接口的成功响应都带 `Cache-Control: no-store`。

本节专有的错误码：

| HTTP | `error.code` | 什么时候 | 写库 |
| --- | --- | --- | --- |
| 404 | `CUSTOMER_NOT_FOUND` | 路径里的客户不存在 | 否 |
| 404 | `PROJECT_NOT_FOUND` | 项目不存在，或不属于路径里的客户（两者一模一样） | 否 |
| 404 | `CREDENTIAL_NOT_FOUND` | `api_key` / `key_version` 不存在，或属于别的项目（一模一样） | 否 |
| 409 | `CREDENTIAL_VERSION_CONFLICT` | 轮换时 `current_key_version` 不是当前最大版本 | 否 |
| 409 | `CREDENTIAL_REVOKED` | 轮换一个所有版本都已吊销的 `api_key` | 否 |
| 503 | `ENCRYPTION_NOT_CONFIGURED` | 主密钥未配置（只影响建凭据与轮换；列表与吊销照常） | 否 |
| 500 | `INTERNAL_ERROR` | 意外错误；整个事务回滚，这次生成的 `secret` 随之作废、从未返回 | 否 |

### 格式

- `api_key` = `ak_` + 32 个小写十六进制字符，例如 `ak_00000000000000000000000000000000`。
- `secret` = `sk_` + 64 个小写十六进制字符（256 位），例如
  `sk_0000000000000000000000000000000000000000000000000000000000000000`。HMAC 的密钥就是这**整个字符串**
  （含 `sk_` 前缀）的 UTF-8 字节。
- 本文件与测试里的示例一律是全零占位值，不是真实凭据。

### 凭据版本对象

```json
{
  "api_key": "ak_00000000000000000000000000000000",
  "key_version": 2,
  "status": "ACTIVE",
  "valid_from": "2026-09-25T08:30:00",
  "valid_until": null,
  "last_used_at": null,
  "created_at": "2026-09-25T08:30:00",
  "revoked_at": null,
  "verifiable": true
}
```

| 字段 | 说明 |
| --- | --- |
| `api_key` | 查找标识。轮换时不变 |
| `key_version` | 签名密钥版本，从 1 起；请求头 `X-Acuven-Key-Version` 里带的就是它 |
| `status` | 只有 `ACTIVE` 与 `REVOKED`。`REVOKED` 是终态 |
| `valid_from` / `valid_until` | 有效期；`valid_until` 为 `null` 表示没有截止。轮换时旧版本得到截止时间 |
| `last_used_at` | 预留，目前总是 `null`（写入随摄取端点做） |
| `revoked_at` | 吊销时刻；未吊销为 `null` |
| `verifiable` | 按响应那一刻算：`ACTIVE`、已到 `valid_from`、且 `valid_until` 为空或还没到。「已过期」不另存状态 |

**签发的凭据版本**（建凭据与轮换的响应）= 凭据版本对象 + `secret`。

响应里没有内部自增 id、`tenant_id`、内部 `project_id`、密文与主密钥版本。

### `POST …/credentials` —— 建凭据

请求体是 `{}`。其他字段一律 422：不能指定 `api_key`、`secret`、`key_version`、`tenant_id`、
`project_id`、`valid_until` 或任何 id —— 客户与项目只来自路径，操作者只来自令牌。

成功：**201**。新的 `api_key`，`key_version = 1`，`ACTIVE`，没有截止时间。同一个事务里写一行凭据
（`secret` 只以 AES-256-GCM 信封加密的密文存储）和一条 `API_KEY_CREATE` 审计（`after_state` 是
`api_key`、`key_version`、`project_public_id`、`tenant_public_id`、`valid_from`）。不幂等：再调一次得到
另一个 `api_key`。

### `GET …/credentials` —— 凭据列表

查询参数见[分页](#分页spec-108)。`data.items` 是这个项目下每个 `api_key` 的每个版本：先按 `api_key`
的创建先后，再按 `key_version` 从小到大。只读，不写审计。没有 `secret`，没有密文。

### `POST …/credentials/{api_key}/rotate` —— 轮换

| 字段 | 必填 | 规则 |
| --- | --- | --- |
| `current_key_version` | 是 | 正整数（JSON 数字，不收字符串、小数或布尔）。填调用方看到的**最新**版本号 |

成功：**201**，`api_key` 不变，新版本 `key_version = N+1`、没有截止时间，响应里带它的 `secret`（只这一次）。
同一事务里：此前未吊销的旧版本中，`valid_until` 为空或晚于「现在 + 重叠期」的，改为「现在 + 重叠期」；
旧版本的密文不动。写一条 `API_KEY_ROTATE` 审计（`before_state` 是各未吊销版本的 `key_version` 与
`valid_until`，`after_state` 是新 `key_version` 与各旧版本改后的 `valid_until`）。

- **重叠期**默认 7 天（604800 秒），由配置项 `BILLING_CREDENTIAL_ROTATION_OVERLAP_SECONDS` 调整（`0` 表示
  轮换即让旧版本立刻失效）。重叠期里新旧版本都能用于签名，集成方在这段时间里换上新 `secret`。
- `current_key_version` 不是当前最大版本（双击、两个管理员同时轮换）：409 `CREDENTIAL_VERSION_CONFLICT`，
  什么都不写。刷新列表后再决定。
- 这个 `api_key` 的所有版本都已吊销：409 `CREDENTIAL_REVOKED`。
- 失败重试：带同一个 `current_key_version` 重发。若提交成功而响应丢了，新版本已入库但拿不到 `secret`：
  再轮换一次（或吊销那个版本），`secret` 不能事后找回。

### `POST …/credentials/{api_key}/versions/{key_version}/revoke` —— 吊销一个版本

| 字段 | 必填 | 规则 |
| --- | --- | --- |
| `reason` | 是 | 去掉首尾空白后长度 1–255。进审计的 `reason`；只写业务说明，不写个人数据 |

成功：**200**，`data` 是该版本。`ACTIVE` → `REVOKED`，`revoked_at` 为现在，立即不可用于校验；同一事务写一条
`API_KEY_REVOKE` 审计（前后状态是该版本的 `key_version` 与 `status`）。**幂等**：已经 `REVOKED` 时返回 200 与
当前状态，什么都不写。吊销最新版本、而旧版本还在重叠期内也允许：此后只剩旧版本可用，直到它过期。

### `POST …/credentials/{api_key}/revoke` —— 吊销整个 key

请求体同上。成功：**200**，`data` 是这个 `api_key` 的全部版本（数组，版本从小到大），所有 `ACTIVE` 版本
都变成 `REVOKED`；一条 `API_KEY_REVOKE` 审计列出受影响的版本。全部已吊销时返回 200 与当前状态，什么都不写。

吊销只改状态，不删行、不清密文；凭据没有删除接口（以后的用量事件要引用它）。

---

## 集成请求签名

设计依据同上（设计 §2「校验库」；spec §37）。校验库在 `app/services/integration_auth.py`；第一个按它校验的
端点是 [用量事件摄取](#集成用量事件摄取)（AIH-TASK-029），防重放与请求头格式的补充规则写在那一节。下面两条是
spec §37 没写死、由设计补的定义，**本节是它们的唯一出处**，Billing Client 与服务端都照这里实现。

### 请求头

| 请求头 | 内容 |
| --- | --- |
| `X-Acuven-Api-Key` | `api_key` |
| `X-Acuven-Key-Version` | `key_version`（十进制整数） |
| `X-Acuven-Timestamp` | **Unix 纪元秒的十进制整数字符串**：不带小数、不带正负号、不补零，例如 `1790325000` |
| `X-Acuven-Request-Id` | 请求 id，原样参与签名 |
| `X-Acuven-Signature` | HMAC-SHA256 输出的**小写**十六进制（64 个字符） |

### 规范化请求串

五行，用 `\n`（LF）连接，末尾没有换行：

```text
METHOD
NORMALIZED_PATH_AND_QUERY
X-Acuven-Timestamp
X-Acuven-Request-Id
SHA256(RAW_REQUEST_BODY)
```

1. `METHOD`：大写，例如 `POST`。
2. `NORMALIZED_PATH_AND_QUERY`：
   - 路径**原样**：不解码百分号编码、不折叠 `.` / `..`、不去掉尾部斜杠；
   - 查询串按 `&` 拆成若干段，每段在第一个 `=` 处分成键与值（没有 `=` 的段，值为空串）；
   - 按键、再按值做**字节序**（UTF-8 字节逐个比较，大写字母排在小写之前）排序，键值都相同时按整段排序；
     排好后用 `&` 原样连接。各段的百分号编码原样保留，空值不丢；
   - 没有查询串（包括只有一个 `?`）时不带 `?`。
   - 例：`/api/v1/usage-events?source=chatbot&batch=7` → `/api/v1/usage-events?batch=7&source=chatbot`
3. 时间戳：请求头 `X-Acuven-Timestamp` 的原值。
4. 请求 id：请求头 `X-Acuven-Request-Id` 的原值。
5. 请求体原始字节的 SHA-256，小写十六进制。空请求体也照算
   （`e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855`）。

签名 = `HMAC-SHA256(key = secret 的 UTF-8 字节, message = 规范化请求串的 UTF-8 字节)`，小写十六进制。

### 服务端怎么判

1. 时间戳不是上面的格式：拒绝（`MALFORMED_TIMESTAMP`）。
2. `|服务端当前时刻 − 时间戳| > 300` 秒：拒绝（`TIMESTAMP_OUT_OF_WINDOW`）。正好 300 秒仍接受。
3. 签名不是 64 个小写十六进制字符，或与服务端算出的不同（常量时间比较）：拒绝（`BAD_SIGNATURE`）。
4. `api_key` + `key_version` 必须对应一个此刻可用的版本：`ACTIVE`、已到 `valid_from`、`valid_until`
   为空或还没到。不存在、已吊销、已过期、版本号不是正整数，一律同样对待。

端点对以上所有拒绝给**同一个** 401，不让调用方区分原因；上面的码只在服务端内部使用。
防重放（记住用过的请求 id）由摄取端点实现，见[用量事件摄取](#集成用量事件摄取)的「防重放」。

---

## 管理端出站 webhook 签名密钥

设计依据：设计闸门 #135 `APPROVED: design v1`，全文见 [design/AIH-TASK-019-webhook-signing.md](design/AIH-TASK-019-webhook-signing.md)（spec §28、§36、§37、§66；ADR-0004 §4a）。实现登记为 AIH-TASK-019。

平台向集成应用后端推送的状态 webhook 用**每个项目一把**的出站签名密钥签名（spec §28）。这里是管理员为
客户的项目签发、列出、启用、退役这把密钥的四个接口；签名怎么算见下一节。**本任务不发送 webhook**：
`status_webhook_url`、投递、重试与 payload 形状归 Phase 3 的投递设计闸门。

| 方法与路径 | 请求体 | 成功 | 错误 |
| --- | --- | --- | --- |
| `POST /api/v1/admin/customers/{customer_id}/projects/{project_id}/webhook-secrets` | `{}` | 201，签发的版本 | 401 / 403 / 404 / 409 / 422 / 500 / 503 |
| `GET /api/v1/admin/customers/{customer_id}/projects/{project_id}/webhook-secrets` | — | 200，版本分页 | 401 / 403 / 404 / 422 / 503 |
| `POST …/webhook-secrets/{key_version}/activate` | `{}` | 200，启用后的全部版本（数组，版本从小到大） | 401 / 403 / 404 / 409 / 422 / 500 / 503 |
| `POST …/webhook-secrets/{key_version}/retire` | `{"reason": str}` | 200，该版本 | 401 / 403 / 404 / 422 / 500 / 503 |

`…` 是 `/api/v1/admin/customers/{customer_id}/projects/{project_id}`。只有 ADMIN 能调。
`{customer_id}`、`{project_id}` 是 `public_id`；`{key_version}` 是正整数（`0`、负数、非整数、超出 INT 上界都是 422）。

⚠️ **`secret` 只出现一次**：只在签发的那一次 201 响应里，之后任何接口都不再返回它（spec §36）。调用方
当场保存并交给集成方。四个接口的成功响应都带 `Cache-Control: no-store`。

本节专有的错误码：

| HTTP | `error.code` | 什么时候 | 写库 |
| --- | --- | --- | --- |
| 404 | `CUSTOMER_NOT_FOUND` | 路径里的客户不存在 | 否 |
| 404 | `PROJECT_NOT_FOUND` | 项目不存在，或不属于路径里的客户（两者一模一样） | 否 |
| 404 | `WEBHOOK_SECRET_NOT_FOUND` | 该项目没有这个 `key_version`（属于别的项目的与不存在的一模一样） | 否 |
| 409 | `WEBHOOK_SECRET_PENDING_EXISTS` | 签发时已有一个 `PENDING` 版本（先启用或退役它） | 否 |
| 409 | `WEBHOOK_SECRET_NOT_PENDING` | 启用一个 `RETIRED` 版本 | 否 |
| 409 | `WEBHOOK_SECRET_CONFLICT` | 数据库唯一约束兜底触发（并发下理论上被行锁挡住） | 否 |
| 503 | `ENCRYPTION_NOT_CONFIGURED` | 主密钥未配置（只影响签发；列表、启用、退役照常） | 否 |
| 500 | `INTERNAL_ERROR` | 意外错误；整个事务回滚，这次生成的 `secret` 随之作废、从未返回 | 否 |

### 格式

- `secret` = `whs_` + 64 个小写十六进制字符（256 位），例如
  `whs_0000000000000000000000000000000000000000000000000000000000000000`。HMAC 的密钥就是这**整个字符串**
  （含 `whs_` 前缀）的 UTF-8 字节，与入站的 `sk_…` 规则一致。
- 本文件与测试里的示例一律是全零占位值，不是真实密钥。

### 版本对象

```json
{
  "key_version": 2,
  "status": "ACTIVE",
  "created_at": "2026-09-28T08:30:00",
  "activated_at": "2026-09-28T09:00:00",
  "retired_at": null
}
```

| 字段 | 说明 |
| --- | --- |
| `key_version` | 签名版本，从 1 起，只增不减，退役的不复用；出站请求头 `X-Acuven-Key-Version` 里带的就是它 |
| `status` | `PENDING`（已签发、平台还没用它签名）/ `ACTIVE`（平台此刻用它签名）/ `RETIRED`（终态） |
| `created_at` / `activated_at` / `retired_at` | 服务端时刻，整秒、UTC；后两者没发生时为 `null`（从 `PENDING` 直接退役的没有 `activated_at`） |

**签发的版本**（签发的响应）= 版本对象 + `secret`。

响应里没有内部自增 id、`tenant_id`、内部 `project_id`、密文与主密钥版本。

每个项目任何时刻**至多一个 `ACTIVE`、至多一个 `PENDING`**，由数据库唯一索引保证。

### `POST …/webhook-secrets` —— 签发

请求体是 `{}`。其他字段一律 422：不能指定 `secret`、`key_version`、`status` 或任何 id —— 客户与项目只来自
路径，操作者只来自令牌。

成功：**201**，新版本 `key_version` = 该项目最大版本 + 1（没有则 1），`PENDING`，响应里带它的 `secret`
（只这一次）。同一事务里写一行密文（AES-256-GCM 信封加密）和一条 `WEBHOOK_SECRET_ISSUE` 审计（`after_state`
是 `key_version` 与 `status`）。

- 已有 `PENDING`：409 `WEBHOOK_SECRET_PENDING_EXISTS`，什么都不写。
- 不幂等。若提交成功而响应丢了，新版本已是 `PENDING` 但拿不到 `secret`：退役它再签发一次，`secret` 不能事后找回。

### `GET …/webhook-secrets` —— 版本列表

查询参数见[分页](#分页spec-108)。`data.items` 是这个项目的全部版本，`key_version` 从小到大。只读，不写审计。
没有 `secret`，没有密文。

### `POST …/webhook-secrets/{key_version}/activate` —— 启用

请求体是 `{}`。成功：**200**，`data` 是这个项目启用后的全部版本（数组，版本从小到大）。

- `PENDING` → `ACTIVE`，`activated_at` 为现在；原来的 `ACTIVE`（若有）**同一事务**变成 `RETIRED`，`retired_at`
  为现在。平台从此用新版本签名，不会出现两把 `ACTIVE` 或一把都没有的中间态。写一条 `WEBHOOK_SECRET_ACTIVATE`
  审计（前后状态是新旧两个版本的 `key_version` 与 `status`）。
- 已是 `ACTIVE`：200 与当前各版本，什么都不写（幂等）。
- `RETIRED`：409 `WEBHOOK_SECRET_NOT_PENDING`。

**正常轮换**（ADR-0004 §4a）：签发新版本 → 把 `secret` 交给集成方，集成方配置成「新旧两个版本都接受」→ 启用
新版本（平台原子切换）→ 集成方确认新签名能通过后自己停止接受旧版本。旧版本在启用那一步已是 `RETIRED`，
平台不会再用它签名。

### `POST …/webhook-secrets/{key_version}/retire` —— 退役

| 字段 | 必填 | 规则 |
| --- | --- | --- |
| `reason` | 是 | 去掉首尾空白后长度 1–255。进审计的 `reason`；只写业务说明，不写个人数据 |

成功：**200**，`data` 是该版本。`PENDING` 或 `ACTIVE` → `RETIRED`，`retired_at` 为现在；同一事务写一条
`WEBHOOK_SECRET_RETIRE` 审计（前后状态是该版本的 `key_version` 与 `status`）。**幂等**：已经 `RETIRED` 时返回
200 与当前状态，什么都不写。

⚠️ 退役 `ACTIVE`（例如泄露时）之后**项目没有签名密钥**：平台不会不签名就发送，以后的投递要等到有新的
`ACTIVE` 为止。退役只改状态，不删行、不清密文；没有删除接口。

---

## 状态 webhook 签名

设计依据同上（设计 §2「签名库」；spec §28、§37）。**本任务只提供签名库**（`app/services/webhook_signing.py`
的 `signing_material` 与 `sign_status_webhook`），还没有任何发送方；投递随 Phase 3 接上。规范串与
[集成请求签名](#集成请求签名)是**同一套规则、同一份实现**，只有第四行不同。**本节是那条差别的唯一出处**，
集成方验签照这里实现。

### 请求头

| 请求头 | 内容 |
| --- | --- |
| `X-Acuven-Timestamp` | **Unix 纪元秒的十进制整数字符串**（同入站：不带小数、不带正负号、不补零）。每次发送重新生成 |
| `X-Acuven-Signature` | HMAC-SHA256 输出的**小写**十六进制（64 个字符） |
| `X-Acuven-Event-ID` | 事件 id，原样参与签名 |
| `X-Acuven-Key-Version` | 签名用的 `key_version`（十进制整数） |

### 规范化请求串

五行，用 `\n`（LF）连接，末尾没有换行：

```text
POST
NORMALIZED_PATH_AND_QUERY
X-Acuven-Timestamp
X-Acuven-Event-ID
SHA256(RAW_REQUEST_BODY)
```

⚠️ **第四行填 `X-Acuven-Event-ID`**。spec §37 的第四行是请求 id，而 spec §28 的出站请求头里没有请求 id；
在出站 webhook 上，事件 id 就是那一行的角色（设计 §2）。其余四行与[集成请求签名](#规范化请求串)完全相同：
方法是 `POST`；路径与查询串是 webhook URL 的路径与查询串，按同样的规则规范化；时间戳是请求头原值；
最后一行是请求体原始字节的 SHA-256 小写十六进制。

签名 = `HMAC-SHA256(key = secret 的 UTF-8 字节, message = 规范化请求串的 UTF-8 字节)`，小写十六进制。

### 集成方怎么验

按 `X-Acuven-Key-Version` 选密钥，照上面的规则算出签名，与 `X-Acuven-Signature` 做常量时间比较。轮换期间
同时接受新旧两个版本，确认新版本能通过后再停用旧版本（ADR-0004 §4a）。时间窗、重试与按事件 id 去重的约定
随 Phase 3 投递设计闸门写进这里。

---

## 管理端审计日志

依据：spec §89 的 `GET /api/v1/admin/audit-logs`、§66 的审计字段。实现登记为 AIH-TASK-022；只读接口，不改钱的行为、
状态机与认证逻辑，没有走设计闸门（见 [TODO 历史记录](archive/TODO_RECORDS_2026-09-30.md) 的「审计日志差异清单」第 1 条）。

| 方法与路径 | 成功 | 错误 |
| --- | --- | --- |
| `GET /api/v1/admin/audit-logs` | 200，审计分页 | 401 / 403 / 422 / 503 |

只有 ADMIN 能调。**只读**：查看审计本身不写审计，也不写任何行。

### 查询参数

全部可省略，给了几个就按几个同时筛（AND）。未知的查询参数不报错，与其他列表接口一致。

| 参数 | 规则 |
| --- | --- |
| `page` / `page_size` | 见[分页](#分页spec-108) |
| `action` | `AuditAction` 的一个值，大小写敏感（如 `CUSTOMER_CREATE`、`LOGIN_FAILED`）；别的值 422 |
| `entity_type` | 精确匹配，如 `tenant`、`project`、`users` |
| `entity_id` | 精确匹配。**只对非用户类型生效**（见下面的替换规则）：`entity_type` 是用户类型时同时带 `entity_id` 是 422；不带 `entity_type` 时只在非用户类型里找 |
| `actor_email` | 去掉首尾空白、转小写后与操作者的登录邮箱精确匹配；没有这个用户就是空列表 |
| `created_from` | 含。格式见[时间](#时间)：不带时区的 UTC，精确到秒，例如 `2026-09-20T08:30:00` |
| `created_to` | 不含。格式同上 |

- 时间只收 `YYYY-MM-DDTHH:MM:SS` 这一种写法：只有日期、带 `Z` 或 `+08:00`、带小数秒、用空格分隔、不补零、
  日历上不存在的日期（`2026-02-30T00:00:00`）都是 422。
- `created_from` 晚于或等于 `created_to` 时 422。
- 422 都是 `VALIDATION_ERROR`，`message` 只列字段名（如 `query.created_from`），不回显值。
- ⚠️ 分页与 `action` 在鉴权**之前**校验（与[通用约定](#鉴权)一致）；时间格式、时间区间与「用户类型带
  `entity_id`」在鉴权**之后**判 —— 没带令牌时这几种先得到 401。

### 审计对象

`data` 是[分页](#分页spec-108)对象，`items` 里每一项：

```json
{
  "created_at": "2026-09-20T08:30:00",
  "action": "CUSTOMER_CREATE",
  "actor_role": "ADMIN",
  "actor_email": "admin@example.com",
  "entity_type": "tenant",
  "entity_id": "3f0e6c1a-8d4b-4c6e-9a51-2b7f0d9c4e11",
  "entity_user_email": null,
  "ip_address": "203.0.113.7",
  "user_agent": "Mozilla/5.0 ...",
  "reason": null,
  "before_state": null,
  "after_state": { "public_id": "3f0e6c1a-8d4b-4c6e-9a51-2b7f0d9c4e11", "company_name": "Acme Sdn Bhd" }
}
```

| 字段 | 说明 |
| --- | --- |
| `created_at` | 写入时刻，不带时区的 UTC，精确到秒 |
| `action` | `AuditAction` 的值 |
| `actor_role` | 写入时操作者的角色；系统动作（计费状态跃迁）是 `SYSTEM`；不知道是谁时 `null` |
| `actor_email` | 操作者的登录邮箱。没有操作者（如 `UNKNOWN_EMAIL` 的登录失败、系统动作）或用户已不存在时 `null` |
| `entity_type` | 审计对象的类型；登录失败等没有对象的审计为 `null` |
| `entity_id` | 对外 id，见下面的替换规则 |
| `entity_user_email` | 只在用户类型上有值：那个用户的登录邮箱；用户已不存在时 `null` |
| `ip_address` / `user_agent` / `reason` | 库里存的原值，可为 `null` |
| `before_state` / `after_state` | 解析好的 JSON **对象**（不是字符串）；没有时 `null`。内容是写入方挑好的字段白名单，不含密码、令牌、密钥与客户的个人数据 |

响应里只有上面这些字段：**没有**审计行的 `id`，也没有 `actor_user_id`。

### 内部 id 的替换规则

响应里不出现任何内部自增 id：

- 操作者：`actor_user_id` 不返回，关联 `users` 换成 `actor_email`。
- **用户类型**：`entity_type` 为 `users`、`two_factor_settings`、`recovery_codes` 的审计，库里的 `entity_id` 存的是
  内部用户 id —— 响应里 `entity_id` 一律为 `null`，改填 `entity_user_email`。按 `entity_id` 筛选也不对它们生效，
  内部用户 id 不能被探测出来。
- 其他类型的 `entity_id` 本来就是对外 id，原样返回、`entity_user_email` 为 `null`：

| `entity_type` | `entity_id` 是 |
| --- | --- |
| `tenant` | 客户的 `id`（`public_id`） |
| `project` | 项目的 `id`（`public_id`） |
| `project_webhook_secret` | 所属项目的 `id`（`public_id`） |
| `integration_credential` | `api_key` |
| `wallet_transaction` | 调账对象的 `id`（账本行的 `public_id`） |
| `refresh_tokens` | 登录会话的令牌家族 id（随机 uuid 的 32 位十六进制，不是自增 id） |
| `usage_meter_type` | 计量类型的 `id`（`public_id`） |
| `ai_provider` | 供应商的 `id`（`public_id`） |
| `ai_model` | 模型的 `id`（`public_id`） |
| `ai_model_alias` | 别名段的 `id`（`public_id`）：映射是新段的，撤销是被截断那一段的 |

两类的清单在 `app/services/audit_query.py`；新增一种写审计的 `entity_type` 而没有归类，
`tests/backend/test_admin_audit_api.py` 的扫描用例会红。

### 排序

**最新在前，按审计行的自增 id 倒序**，不按 `created_at`。登录路径与业务路径写 `created_at` 时的取整不一致
（见 [TODO 历史记录](archive/TODO_RECORDS_2026-09-30.md) 的 AIH-TASK-006 记录段）：登录审计可能比它之后写入的建客户审计晚一秒。自增 id 才是
真实的写入顺序。同一个原因，按 `created_from` / `created_to` 筛选在整秒边界上可能差一秒。

---

## AI 目录：计量类型、供应商、模型与别名

设计依据：设计闸门 #163 `APPROVED: design v4`，全文见 [design/AIH-TASK-025-ai-catalog.md](design/AIH-TASK-025-ai-catalog.md)
（spec §11、§12、§15.1、§58、§74、§84）。实现登记为 AIH-TASK-025。表结构见
[database-schema.md](database-schema.md) 的「AI 目录」。

本节是 Phase 2 价格、定价规则与计费挂靠的目录。**不含**价格、定价规则、FX、用量事件、未知模型的重新入队与前端。

| 方法与路径 | 请求体 | 成功 | 错误 |
| --- | --- | --- | --- |
| `GET /api/v1/admin/usage-meter-types` | — | 200，计量类型分页 | 401 / 403 / 422 / 503 |
| `POST /api/v1/admin/usage-meter-types` | 见下 | 201，计量类型 | 401 / 403 / 409 / 422 / 500 / 503 |
| `GET /api/v1/admin/usage-meter-types/{meter_type_id}` | — | 200，计量类型 | 401 / 403 / 404 / 503 |
| `PATCH /api/v1/admin/usage-meter-types/{meter_type_id}` | 见下 | 200，计量类型 | 401 / 403 / 404 / 422 / 500 / 503 |
| `GET /api/v1/admin/ai-providers` | — | 200，供应商分页 | 401 / 403 / 422 / 503 |
| `POST /api/v1/admin/ai-providers` | 见下 | 201，供应商 | 401 / 403 / 409 / 422 / 500 / 503 |
| `GET /api/v1/admin/ai-providers/{provider_id}` | — | 200，供应商 | 401 / 403 / 404 / 503 |
| `PATCH /api/v1/admin/ai-providers/{provider_id}` | 见下 | 200，供应商 | 401 / 403 / 404 / 422 / 500 / 503 |
| `GET …/{provider_id}/models` | — | 200，模型分页 | 401 / 403 / 404 / 422 / 503 |
| `POST …/{provider_id}/models` | 见下 | 201，模型 | 401 / 403 / 404 / 409 / 422 / 500 / 503 |
| `GET …/{provider_id}/models/{model_id}` | — | 200，模型详情 | 401 / 403 / 404 / 503 |
| `PATCH …/{provider_id}/models/{model_id}` | 见下 | 200，模型 | 401 / 403 / 404 / 422 / 500 / 503 |
| `GET …/{provider_id}/model-aliases` | — | 200，别名段分页 | 401 / 403 / 404 / 422 / 503 |
| `POST …/{provider_id}/model-aliases` | 见下 | 201 或 200，当前未截断的那一段 | 401 / 403 / 404 / 409 / 422 / 500 / 503 |
| `POST …/{provider_id}/model-aliases/{alias_id}/retire` | `{}` | 200，被截断的那一段 | 401 / 403 / 404 / 422 / 500 / 503 |

`…` 是 `/api/v1/admin/ai-providers`。只有 ADMIN 能调（处理函数第一条语句就是鉴权）。路径里的 id 一律是
`public_id`；响应里没有内部自增 id。别名段**没有 PATCH**：段一旦写下就不改指向，改映射只能再 POST 一次。
没有删除接口：目录行只停用、不删除（价格版本与用量事件会永久引用它们）。

列表按[分页](#分页spec-108)，按 `code` 升序；计量类型、供应商、模型的列表可加 `status=ACTIVE|RETIRED` 筛选
（别的值 422），不带时列出全部。

本节专有的错误码：

| HTTP | `error.code` | 什么时候 | 写库 |
| --- | --- | --- | --- |
| 404 | `USAGE_METER_TYPE_NOT_FOUND` | 计量类型不存在 | 否 |
| 404 | `AI_PROVIDER_NOT_FOUND` | 路径里的供应商不存在 | 否 |
| 404 | `AI_MODEL_NOT_FOUND` | 路径或请求体里的模型不存在，或不属于路径里的供应商（两者一模一样，不泄露别家的模型是否存在） | 否 |
| 404 | `AI_MODEL_ALIAS_NOT_FOUND` | 撤销的不是该供应商下当前未截断的那一段（已截断、别家的、不存在的都一样） | 否 |
| 409 | `USAGE_METER_TYPE_CODE_TAKEN` | 计量类型 `code` 已存在（含与种子重复） | 否 |
| 409 | `USAGE_METER_COMPONENT_CODE_TAKEN` | `component_code` 已存在（任何类型下，含种子） | 否 |
| 409 | `AI_PROVIDER_CODE_TAKEN` | 供应商 `code` 已存在 | 否 |
| 409 | `AI_MODEL_CODE_TAKEN` | 同供应商下已有同名模型，**或该字符串在别名表里出现过**（不论哪一段、是否已截断） | 否 |
| 409 | `AI_MODEL_ALIAS_TAKEN` | 映射的字符串与同供应商下某个模型的 `code` 相同 | 否 |
| 500 | `INTERNAL_ERROR` | 意外错误（含别名段首尾相接的复查不通过、锁等待超时）；整个事务回滚 | 否 |

### 字段规则

| 字段 | 规则 |
| --- | --- |
| 计量类型 `code`、`component_code` | `^[A-Z][A-Z0-9_]{1,31}$` |
| `unit` | `^[A-Z][A-Z0-9_]{0,15}$` |
| `quantity_kind` | `INTEGER` / `DECIMAL` |
| 供应商 `code` | `^[a-z0-9][a-z0-9_-]{0,63}$` |
| 模型 `code`、`alias` | `^[A-Za-z0-9][A-Za-z0-9._:/@-]{0,127}$`（不含空白；覆盖 `claude-sonnet-4-5-20250929`、`gpt-4o-mini-transcribe`、`models/gemini-x`） |
| `display_name` | 去首尾空白后 1–255 |
| `status` | `ACTIVE` / `RETIRED` |

代码**不去空白、不改大小写**：带空白就是 422；`GPT-4o` 与 `gpt-4o` 是两个不同的代码（库里按字节比较）。
所有请求体都拒绝多余字段（422）。

### 计量类型对象

```json
{
  "id": "00000000-0000-4000-8000-000000000000",
  "code": "VIDEO_SECOND",
  "display_name": "Video seconds",
  "payload_shape": "QUANTITY",
  "unit": "SECOND",
  "quantity_kind": "DECIMAL",
  "status": "ACTIVE",
  "components": [
    { "component_code": "VIDEO_SECOND", "quantity_field": "quantity", "created_at": "2026-09-29T08:30:00" }
  ],
  "created_at": "2026-09-29T08:30:00",
  "updated_at": "2026-09-29T08:30:00"
}
```

| 字段 | 说明 |
| --- | --- |
| `code` | 即上报事件的 `usage_type`。建后不可改 |
| `payload_shape` | 上报形态：`LLM_TOKEN_FIELDS`（四个 token 字段，每个分量对应一个；只有种子 `LLM_TOKEN`）/ `QUANTITY`（`quantity` + `unit`，唯一一个分量取 `quantity`）。建后不可改 |
| `unit` | 上报的 `unit` 必须与它相等（摄取按它校验）。建后不可改 |
| `quantity_kind` | 数量是整数还是小数。建后不可改 |
| `status` | 停用只影响「以后选不选它」：**不影响**摄取与计价 |
| `components` | 计价分量，`component_code` 升序；`quantity_field` 是这个分量从上报事件的哪个字段取数量 |

迁移预置 9 个类型、12 个分量（spec §12、§15.1，清单见 [database-schema.md](database-schema.md)）。

#### `POST /api/v1/admin/usage-meter-types` —— 新建计量类型

| 字段 | 必填 | 规则 |
| --- | --- | --- |
| `code` | 是 | 见字段规则 |
| `display_name` | 是 | 见字段规则 |
| `unit` | 是 | 见字段规则 |
| `quantity_kind` | 是 | `INTEGER` / `DECIMAL` |
| `component_code` | 是 | 见字段规则；与已有任何分量（任何类型下）都不能重复 |

新建的类型**固定为 `QUANTITY`**，同一事务建出它唯一的分量（`quantity_field` = `quantity`）与一条
`USAGE_METER_TYPE_CREATE` 审计。请求体带 `payload_shape` 或 `quantity_field`（或任何多余字段）是 422。
新建之后，应用按非 token 的写法上报 `usage_type = <code>`、`quantity`、`unit` 即可被接收与计价，不需要改代码；
要新的多字段形态（例如分开上报两档缓存写入）才需要改载荷与代码。

- `code` 或 `component_code` 已存在：409，类型行与分量行都不写。

#### `PATCH /api/v1/admin/usage-meter-types/{meter_type_id}` —— 改名或改状态

只收 `display_name`、`status`，至少一个；两者都不许是 `null`。带 `code`、`unit`、`quantity_kind`、
`payload_shape`、`component_code`、`quantity_field` 或任何多余字段是 422。**没有实际变化**：200，什么都不写
（`updated_at` 不变、不写审计）；有变化时写一条 `USAGE_METER_TYPE_UPDATE`。`ACTIVE ↔ RETIRED` 双向都允许。

### 供应商对象

```json
{
  "id": "00000000-0000-4000-8000-000000000000",
  "code": "anthropic",
  "display_name": "Anthropic",
  "status": "ACTIVE",
  "created_at": "2026-09-29T08:30:00",
  "updated_at": "2026-09-29T08:30:00"
}
```

`code` 与上报事件的 `provider` 精确比较，建后不可改。

- `POST /api/v1/admin/ai-providers`：请求体 `code`、`display_name`。201；同一事务写一条 `AI_PROVIDER_CREATE`。
  `code` 已存在：409 `AI_PROVIDER_CODE_TAKEN`。
- `PATCH /api/v1/admin/ai-providers/{provider_id}`：规则同计量类型的 PATCH（只收 `display_name`、`status`；带
  `code` 等是 422；无变化 200 不写）。审计动作 `AI_PROVIDER_UPDATE`。

### 模型对象

```json
{
  "id": "00000000-0000-4000-8000-000000000000",
  "provider_id": "00000000-0000-4000-8000-000000000000",
  "provider_code": "anthropic",
  "code": "claude-sonnet-4-5",
  "display_name": "Claude Sonnet 4.5",
  "status": "ACTIVE",
  "created_at": "2026-09-29T08:30:00",
  "updated_at": "2026-09-29T08:30:00"
}
```

`provider_id` 是供应商的 `public_id`。`code` 与上报事件的 `model` 精确比较，建后不可改。**模型详情**
（`GET …/models/{model_id}`）多一项 `aliases`：当前（未截断的段）指向它的别名段，`alias` 升序。

- `POST …/{provider_id}/models`：请求体 `code`、`display_name`；供应商只来自路径（带 `provider_id` 是 422）。
  201；同一事务写一条 `AI_MODEL_CREATE`。在供应商行排他锁内检查：同供应商下已有同名模型，**或该字符串在
  别名表里出现过**：409 `AI_MODEL_CODE_TAKEN`。
- `PATCH …/{provider_id}/models/{model_id}`：规则同上。审计动作 `AI_MODEL_UPDATE`。

**停用（`RETIRED`）不影响解析与计费**：用量已经发生，钱照样要算；停用只表示以后建新价格、新规则时不许选它。

### 别名段对象

一段 = 「某个字符串在 `[effective_from, effective_to)` 内指向某个模型」。一个字符串的所有段首尾相接、
不重叠，至多一段未截断。

```json
{
  "id": "00000000-0000-4000-8000-000000000000",
  "alias": "claude-sonnet-4-5-20250929",
  "model_id": "00000000-0000-4000-8000-000000000000",
  "model_code": "claude-sonnet-4-5",
  "effective_from": null,
  "effective_to": null,
  "created_at": "2026-09-29T08:30:00",
  "closed_at": null
}
```

| 字段 | 说明 |
| --- | --- |
| `alias` | 上报事件里出现、但不是模型代码的那个字符串 |
| `model_id` / `model_code` | 这一段指向的模型（`public_id` 与代码）。段写下之后指向不变 |
| `effective_from` | `null` = 「一直以来」；只有一个字符串的第一段是 `null` |
| `effective_to` | `null` = 仍未截断；截断后不再改 |
| `closed_at` | 截断的那一刻（服务端时间，整秒） |

#### `GET …/{provider_id}/model-aliases` —— 别名段列表

全部历史，按 `alias`、`effective_from`（`null` 在前）排序。查询参数：`alias`（精确匹配，最长 128，
不做大小写折叠）、`current=true`（只列未截断的段），以及分页。

#### `POST …/{provider_id}/model-aliases` —— 映射

| 字段 | 必填 | 规则 |
| --- | --- | --- |
| `alias` | 是 | 见字段规则 |
| `model_id` | 是 | **同一供应商下**模型的 `public_id`；别家的与不存在的一样是 404 `AI_MODEL_NOT_FOUND` |

在供应商行排他锁内完成；边界时刻 `t` 在**拿到锁之后**取：服务端当前时间**向上**取整到下一个整秒（恰好是
整秒时取下一秒），所以 `t` 严格晚于此刻。按该字符串的现状：

| 现状 | 做什么 | 响应 |
| --- | --- | --- |
| 从没有过段 | 插入第一段 `[null, null) → M`：对过去全部生效（过去这个字符串一律是「未知」、从未扣过钱） | 201 |
| 有未截断的段，且指向 M | 什么都不写 | 200 |
| 有未截断的段，指向别的模型 N | 截断当前段于 `t`，插入 `[t, null) → M`。`t` 之前发生的事件永远按 N | 201 |
| 所有段都已截断（撤销过），最后一段止于 `t0` | 插入 `[t0, null) → M`，补上撤销以来的空档 | 201 |

- 字符串与同供应商下某个模型的 `code` 相同：409 `AI_MODEL_ALIAS_TAKEN`，不写。
- 写了新段时同一事务写一条 `AI_MODEL_ALIAS_MAP`，并在提交前复查该字符串的全部段首尾相接、只有第一段
  `effective_from` 为空；不满足就整体回滚（500）。
- 同一秒内对同一字符串连续两次改映射（或改映射后立刻撤销）时，当前段的起点可能已经 ≥ 这一次的 `t`；这时
  截断点取「当前段起点 + 1 秒」，仍是整秒、仍严格晚于此刻，段不会变成空区间。

**已经解析到某个模型的 `(字符串, 时刻)` 永远不会改成另一个模型**：映射的每一次变动只可能把某些时刻从「未知」
变成某个模型，或只影响 `t` 及以后发生的事件。

#### `POST …/{provider_id}/model-aliases/{alias_id}/retire` —— 撤销

请求体是 `{}`。`alias_id` 必须是这个供应商下**当前未截断**的那一段，否则 404 `AI_MODEL_ALIAS_NOT_FOUND`、
不写。成功：200，截断于 `t`（取法同映射），同一事务写一条 `AI_MODEL_ALIAS_RETIRE`。`t` 起发生的事件
解析为「未知」（计费侧进 `MODEL_UNKNOWN`，不扣费、可见）。

### 审计

八个动作都与目录写入同一事务，不写 outbox；操作者带 ip 与 user agent。前后状态里指向一律用模型 `code`，
不用内部 id。

| `action` | `entity_type` / `entity_id` | `before_state` | `after_state` |
| --- | --- | --- | --- |
| `USAGE_METER_TYPE_CREATE` | `usage_meter_type` / 类型的 `id` | — | `code`、`display_name`、`payload_shape`、`unit`、`quantity_kind`、`status`、`component_code` |
| `USAGE_METER_TYPE_UPDATE` | 同上 | 变化的字段的旧值 | 变化的字段的新值 |
| `AI_PROVIDER_CREATE` | `ai_provider` / 供应商的 `id` | — | `code`、`display_name`、`status` |
| `AI_PROVIDER_UPDATE` | 同上 | 变化的字段的旧值 | 变化的字段的新值 |
| `AI_MODEL_CREATE` | `ai_model` / 模型的 `id` | — | `provider_code`、`code`、`display_name`、`status` |
| `AI_MODEL_UPDATE` | 同上 | 变化的字段的旧值 | 变化的字段的新值 |
| `AI_MODEL_ALIAS_MAP` | `ai_model_alias` / 新段的 `id` | 被截断那一段的 `model_code` 与 `effective_to`（截断前为 `null`）；没有被截断的段时为空 | `provider_code`、`alias`、`model_code`（新段的目标）、`effective_from` |
| `AI_MODEL_ALIAS_RETIRE` | `ai_model_alias` / 被截断那一段的 `id` | `effective_to`（`null`） | `effective_to` |

「指向相同」的映射与无变化的 PATCH 不写审计。

### 解析（计费侧调用，本任务不接线）

`app/repositories/ai_catalog.py` 的 `resolve_model(session, provider_code, model_code, occurred_at)`：按「模型代码
精确匹配 → 在 `occurred_at` 那一刻生效的别名段」返回模型，都不中返回 `None`。不做大小写或任何规范化；不看
供应商与模型的状态。它对供应商行加共享锁（`FOR SHARE`），模型与别名也用加锁读，锁持有到**调用方**的事务
提交。计费侧（T-H）必须在写事件快照、扣费的同一事务里调用它，并只处理 `is_due(occurred_at, now)` 为真的
事件（`now` 在它拿到锁之后取）；更晚的留到下一轮。契约全文见设计 §2「对下游任务的契约」与
[TODO 历史记录](archive/TODO_RECORDS_2026-09-30.md) 的 AIH-TASK-025 记录段。

---

## 管理端供应商价格

设计依据：设计闸门 #177 `APPROVED: design v4`，全文见
[design/AIH-TASK-026-provider-prices.md](design/AIH-TASK-026-provider-prices.md)（spec §14、§15.1、§17、§66、§74.1、
§80）。实现登记为 AIH-TASK-026。表结构见 [database-schema.md](database-schema.md) 的「供应商成本价」。

一个**价格版本**是某个（供应商, 模型）的一版**成本价**：原币种（通常 USD）、来源说明、若干价格分量。状态
`DRAFT → PUBLISHED → RETIRED`，草稿也可以 `DISCARDED`。已发布的版本在 `[effective_from, effective_to)` 内生效，
同一（供应商, 模型）的区间首尾相接、不重叠，只在末尾追加。**成本价，原币种，客户不可见**：只有这一组管理端
接口。**不含** FX 换算、成本计算、试算、价格同步与前端。

| 方法与路径 | 请求体 | 成功 | 错误 |
| --- | --- | --- | --- |
| `GET /api/v1/admin/provider-prices` | — | 200，版本分页 | 401 / 403 / 404 / 422 / 503 |
| `POST /api/v1/admin/provider-prices` | 见下 | 201，版本（草稿） | 401 / 403 / 404 / 409 / 422 / 500 / 503 |
| `GET /api/v1/admin/provider-prices/{price_version_id}` | — | 200，版本 | 401 / 403 / 404 / 503 |
| `PATCH /api/v1/admin/provider-prices/{price_version_id}` | 见下 | 200，版本 | 401 / 403 / 404 / 409 / 422 / 500 / 503 |
| `POST /api/v1/admin/provider-prices/{price_version_id}/publish` | 见下 | 200，版本 | 401 / 403 / 404 / 409 / 422 / 500 / 503 |
| `POST /api/v1/admin/provider-prices/{price_version_id}/retire` | `{"reason": …}` | 200，版本 | 401 / 403 / 404 / 409 / 422 / 500 / 503 |
| `POST /api/v1/admin/provider-prices/{price_version_id}/discard` | `{}` | 200，版本 | 401 / 403 / 404 / 409 / 422 / 500 / 503 |

只有 ADMIN 能调（处理函数第一条语句就是鉴权）。路径里的 `price_version_id` 是版本的 `public_id`；响应里没有内部
自增 id，人一律用登录邮箱表示。所有请求体都拒绝多余字段（422）。**没有删除接口**：草稿只能丢弃，版本行永不删除。

本节专有的错误码：

| HTTP | `error.code` | 什么时候 | 写库 |
| --- | --- | --- | --- |
| 404 | `PRICE_VERSION_NOT_FOUND` | 路径里的版本不存在 | 否 |
| 404 | `AI_PROVIDER_NOT_FOUND` | 请求体或筛选里的供应商不存在 | 否 |
| 404 | `AI_MODEL_NOT_FOUND` | 模型不存在，或不属于那个供应商（两者一模一样） | 否 |
| 404 | `USAGE_METER_COMPONENT_NOT_FOUND` | `component_code` 不是目录里的计价分量（大小写精确比较） | 否 |
| 409 | `PRICE_VERSION_NOT_DRAFT` | 编辑或丢弃一个已发布的版本 | 否 |
| 409 | `PRICE_VERSION_INCOMPLETE` | 发布时完整性不满足；`message` 列出缺的分量代码 | 否 |
| 409 | `CATALOG_ITEM_RETIRED` | 供应商、模型或某个分量所属的计量类型已停用（建草稿、改草稿、发布时检查） | 否 |
| 409 | `PRICE_VERSION_NOT_RETIRABLE` | 退役一个已被后继截断的历史版本，或退役草稿 | 否 |
| 409 | `PRICE_VERSION_FINAL` | 对已退役 / 已丢弃的版本做任何写操作 | 否 |
| 409 | `EFFECTIVE_FROM_CONFLICT` | 生效时刻不晚于末尾版本的起点，或早于它的尽头；不指定时刻而末尾版本尚未开始（见「发布」） | 否 |
| 422 | `EFFECTIVE_FROM_IN_PAST` | 请求的生效时刻早于 `t`（不许回溯） | 否 |
| 422 | `VALIDATION_ERROR` | 格式、精度、正数、分量个数、多余字段 | 否 |
| 500 | `INTERNAL_ERROR` | 意外错误（含发布 / 退役后区间复查不通过、锁等待超时）；整个事务回滚 | 否 |

### 字段规则

| 字段 | 规则 |
| --- | --- |
| `provider_id` / `model_id` | 供应商与模型的 `public_id`；模型必须属于该供应商 |
| `source_currency` | ISO 4217 大写三字母，`^[A-Z]{3}$`（`usd` 是 422）。可以是 `MYR` |
| `source_reference` | 去首尾空白后 1–255：价格出处（例如「供应商价格页，查看于 2026-09-29」）。不要写合同价 |
| `components` | 1–64 个（超出是 422），同一版本里 `component_code` 不重复（重复是 422） |
| `components[].component_code` | 目录里的计价分量代码，例如 `LLM_INPUT_TOKEN`、`EMBEDDING_TOKEN` |
| `components[].unit_quantity` | 多少个计量单位对应一个 `rate_amount`，例如 `"1000000"` |
| `components[].rate_amount` | 原币种单价 |
| `components[].metadata` | 可选，JSON 对象；只作备注（例如档位名），**计价不读它** |

`unit_quantity` 与 `rate_amount` **只收 JSON 字符串**，正数，`^[0-9]{1,12}(\.[0-9]{1,8})?$`：JSON 数字、`0`、负号、
正号、指数写法、逗号、空白一律 422；**超过 8 位小数是 422，不舍入**。响应里两者都是恰好 8 位小数的字符串。

### 版本对象

```json
{
  "id": "00000000-0000-4000-8000-000000000000",
  "provider_id": "00000000-0000-4000-8000-000000000000",
  "provider_code": "anthropic",
  "model_id": "00000000-0000-4000-8000-000000000000",
  "model_code": "claude-x",
  "source_currency": "USD",
  "source_type": "MANUAL",
  "source_reference": "Fictional price sheet, viewed 2026-09-29",
  "status": "PUBLISHED",
  "effective_from": null,
  "effective_to": null,
  "components": [
    {
      "component_code": "LLM_INPUT_TOKEN",
      "meter_type_code": "LLM_TOKEN",
      "unit": "TOKEN",
      "unit_quantity": "1000000.00000000",
      "rate_amount": "1.11111111",
      "metadata": null,
      "created_at": "2026-09-29T08:30:00"
    }
  ],
  "created_by_email": "admin@example.com",
  "approved_by_email": "admin@example.com",
  "created_at": "2026-09-29T08:30:00",
  "updated_at": "2026-09-29T08:30:00",
  "approved_at": "2026-09-29T08:30:00"
}
```

（示例里的价格是虚构值。）

| 字段 | 说明 |
| --- | --- |
| `source_type` | 本任务只有 `MANUAL`；`SYNC` 留给 Phase 8 的供应商价格同步 |
| `status` | `DRAFT` / `PUBLISHED` / `RETIRED` / `DISCARDED` |
| `effective_from` | 草稿为 `null`；已发布时 `null` 只有一种含义：发布时该（供应商, 模型）没有区间非空的已发布版本、且未指定生效时刻 =「一直以来」 |
| `effective_to` | `null` = 仍生效（或尚未发布）。`effective_from == effective_to` 是空区间（被撤销的预约，或同一秒里发布又退役），永不生效，只出现在 `RETIRED` 上 |
| `components` | `component_code` 升序；`meter_type_code` / `unit` 是分量所属计量类型与它的单位 |
| `created_by_email` / `approved_by_email` | 建草稿人与发布人的登录邮箱；未发布时 `approved_by_email`、`approved_at` 为 `null` |

时间都是不带时区的 UTC，精确到秒。

#### `GET /api/v1/admin/provider-prices` —— 版本列表

按供应商 `code`、模型 `code`、`effective_from`（`null` 在前，含草稿）排序，每项带分量。查询参数：`provider_id`、
`model_id`（`public_id`，最长 64）、`status=DRAFT|PUBLISHED|RETIRED|DISCARDED`，以及[分页](#分页spec-108)。
筛选里的供应商不存在是 404 `AI_PROVIDER_NOT_FOUND`；模型不存在（或同时给了供应商而模型不属于它）是 404
`AI_MODEL_NOT_FOUND`。

#### `POST /api/v1/admin/provider-prices` —— 建草稿

请求体：`provider_id`、`model_id`、`source_currency`、`source_reference`、`components`（都必填）。201；同一事务写
版本、分量与一条 `PROVIDER_PRICE_CREATE`。草稿**可以不完整**：完整性在发布时校验。供应商、模型或分量所属计量类型
已停用：409 `CATALOG_ITEM_RETIRED`。不锁供应商行；重发会建出两个草稿（草稿不影响计费，丢弃多余的即可）。

#### `PATCH /api/v1/admin/provider-prices/{price_version_id}` —— 改草稿

只收 `source_currency`、`source_reference`、`components`，至少一个，都不许是 `null`；`components` 是**整体替换**。
带 `provider_id`、`model_id`、`status`、`effective_from` 或任何多余字段是 422。只对草稿：已发布 409
`PRICE_VERSION_NOT_DRAFT`，已退役 / 已丢弃 409 `PRICE_VERSION_FINAL`。目录检查同建草稿。**没有实际变化**（分量
按代码、数值与备注比较，`1000000` 与 `1000000.0` 相同）：200，什么都不写；有变化时写一条 `PROVIDER_PRICE_UPDATE`。

#### `POST /api/v1/admin/provider-prices/{price_version_id}/publish` —— 发布

请求体：可选 `effective_from`，RFC 3339 且**必须带时区**（`2026-10-01T00:00:00Z`、`2026-10-01T08:00:00+08:00`）；
换算成 UTC 后必须是整秒（`.000` 可以，`.5` 是 422）。不带或 `null` = 不指定。

在供应商行排他锁内完成：拿到锁之后取 `t` = 服务端当前时间**向上**取整到下一个整秒（恰好整秒时取下一秒），所以
`t` 严格晚于此刻。设该（供应商, 模型）**区间非空**的已发布（`PUBLISHED` / `RETIRED`）版本中 `effective_from`
最晚的一个为末尾版本 L（空区间不参与时间线）：

| 情形 | 本版本的 `effective_from` | 对 L 做什么 |
| --- | --- | --- |
| 没有 L，不指定 | `null`（一直以来：此前该模型的事件一律无价、从未扣过钱；撤销了唯一的预约之后也是这样） | — |
| 没有 L，指定 F | F | — |
| L 未截断且已开始（起点为 `null` 或早于 `t`），不指定 | `t` | L 截断于 `t` |
| L 未截断且尚未开始（起点不早于 `t`：预约的，或同一秒里刚发布的），不指定 | 409 `EFFECTIVE_FROM_CONFLICT`，什么都不写：先撤销预约、指定晚于它起点的时刻，或下一秒再发 | — |
| L 未截断，指定 F | F（必须晚于 L 的起点，否则 409 `EFFECTIVE_FROM_CONFLICT`） | L 截断于 F |
| L 已截断或已退役，尽头 E | `max(E, F 或 t)`；F 早于 E 是 409 | —（退役留下的空档不回填） |

- 指定的 F 早于 `t`：422 `EFFECTIVE_FROM_IN_PAST`。F 等于 `t` 可以。
- 完整性：至少一个分量；**出现的计量类型，它的全部分量都必须出现**（例如出现 `LLM_TOKEN` 就必须有输入、输出、
  缓存写入、缓存读取四个价）。不满足：409 `PRICE_VERSION_INCOMPLETE`，`message` 列出缺的分量。
- 目录已停用：409 `CATALOG_ITEM_RETIRED`。已退役 / 已丢弃：409 `PRICE_VERSION_FINAL`。
- **已发布再发布**：200，返回当前版本，什么都不写（幂等；请求体里的 `effective_from` 被忽略）。
- 成功时同一事务写：（需要时）截断 L、本版本改为已发布（`approved_by` / `approved_at` 为发布人与发布时刻）、复查
  区间首尾相接、一条 `PROVIDER_PRICE_PUBLISH`；复查不通过整体回滚（500）。

**一个时刻一旦取到某个版本，以后永远取到同一个版本**：发布只影响 `t` 及以后（或第一个版本把「无价」变成「有价」）。

#### `POST /api/v1/admin/provider-prices/{price_version_id}/retire` —— 退役

请求体：`reason`，去首尾空白后 1–255（记在审计的 `reason` 上）。在供应商行排他锁内完成，`t` 取法同发布：

| 被退役的版本 | 做什么 |
| --- | --- |
| 未截断、已开始生效（起点为 `null` 或 ≤ `t`） | `effective_to = t`、`RETIRED`：`t` 起发生的事件无价（计费侧 `PRICING_ERROR`），`t` 之前照旧。起点恰好是 `t`（同一秒里发布又退役）时得到空区间 `[t, t)`，前一个版本不恢复 |
| 未截断、尚未开始（预约的，起点 > `t`） | `effective_to = effective_from`（空区间，永不生效）、`RETIRED`；前一个版本若仍是 `PUBLISHED` 且正是被它截断的（终点等于它的起点），恢复为未截断（撤销预约）；以退役结束于那一刻的不恢复（退役不回填） |
| 已被后继截断的历史版本、草稿 | 409 `PRICE_VERSION_NOT_RETIRABLE` |
| 已退役 / 已丢弃 | 409 `PRICE_VERSION_FINAL` |

同一事务写版本、（需要时）被恢复的前一个版本、复查与一条 `PROVIDER_PRICE_RETIRE`。退役的版本在它自己的区间里照样
取得到（已经发生的用量照样算钱）。要纠正历史价格走 Phase 8 的 reprocess，不在这里。

#### `POST /api/v1/admin/provider-prices/{price_version_id}/discard` —— 丢弃草稿

请求体是 `{}`。草稿 → `DISCARDED`，行留着（不删除），写一条 `PROVIDER_PRICE_DISCARD`。已发布 409
`PRICE_VERSION_NOT_DRAFT`；已退役 / 已丢弃 409 `PRICE_VERSION_FINAL`。不看目录状态。

### 审计

五个动作都与写入同一事务，不写 outbox；操作者带 ip 与 user agent。`entity_type` = `provider_price_version`，
`entity_id` = 版本的 `id`。供应商、模型、分量一律用 `code`，被截断 / 被恢复的版本用 `public_id`，发布人用邮箱。
审计里有价格：管理员可见的业务数据，不是秘密；应用日志不打印分量明细。

| `action` | `before_state` | `after_state` |
| --- | --- | --- |
| `PROVIDER_PRICE_CREATE` | — | `provider_code`、`model_code`、`source_currency`、`source_type`、`source_reference`、`status`、`components`（每项 `component_code`、`unit_quantity`、`rate_amount`） |
| `PROVIDER_PRICE_UPDATE` | 变化的字段的旧值；分量变了时是整组旧分量 | 变化的字段的新值；分量变了时是整组新分量 |
| `PROVIDER_PRICE_DISCARD` | `status`（`DRAFT`） | `status`（`DISCARDED`） |
| `PROVIDER_PRICE_PUBLISH` | `status`（`DRAFT`）；截断了前一个版本时 `truncated_version`：它的 `id` 与原 `effective_to`（`null`） | `status`、`effective_from`、`effective_to`、`approved_by_email`、`approved_at`；截断了前一个版本时 `truncated_version`：它的 `id` 与新的 `effective_to` |
| `PROVIDER_PRICE_RETIRE` | `status`、`effective_from`、`effective_to`；恢复了前一个版本时 `restored_version`：它的 `id` 与原 `effective_to` | `status`、`effective_from`、`effective_to`；恢复了前一个版本时 `restored_version`：它的 `id` 与 `effective_to`（`null`）。`reason` 记在审计行的 `reason` 上 |

「已发布再发布」与无变化的 PATCH 不写审计。只有 `PROVIDER_PRICE_PUBLISH` 在 spec §66 的清单里，其余四个按先例补上
（见 [TODO 历史记录](archive/TODO_RECORDS_2026-09-30.md) 的 AIH-TASK-026 记录段）。

### 取价（计费侧调用，本任务不接线）

`app/repositories/provider_prices.py` 的 `resolve_provider_price(session, provider_id, model_id, occurred_at)`：
参数是 `resolve_model` 返回的模型的内部 `provider_id` / `id` 与不带时区的 UTC `occurred_at`；返回状态为
`PUBLISHED` 或 `RETIRED`、且 `effective_from ≤ occurred_at < effective_to`（`null` 端不比）的版本及其分量
（`component_code`、`quantity_field`、`unit_quantity`、`rate_amount`，`component_code` 升序），没有就是 `None`
（计费侧 `PRICING_ERROR`，缺价不按 0 算）。版本与分量都是加锁读（`FOR SHARE`），锁持有到**调用方**的事务提交；
计费侧必须在同一个计费事务里**先** `resolve_model`（它对供应商行加共享锁）**再**调用它，并把返回的版本 id 写进事件
快照。契约全文见设计 §2「对下游任务的契约」与 [TODO 历史记录](archive/TODO_RECORDS_2026-09-30.md) 的 AIH-TASK-026 记录段。

---

## 管理端定价规则

设计依据：设计闸门 #178 `APPROVED: design v4`，全文见
[design/AIH-TASK-027-pricing-rules.md](design/AIH-TASK-027-pricing-rules.md)（spec §14、§15、§15.1、§16、§59、§66、
§74.3、§80、§89、§113；ADR-0008）。实现登记为 AIH-TASK-027。表结构见 [database-schema.md](database-schema.md) 的
「客户定价规则」；五级顺序与下落语义见 [pricing-engine.md](pricing-engine.md) 的「规则」。

一条**定价规则**决定客户被扣多少钱：一个**范围**（spec §16 的五级之一）加一种**策略**（spec §15）——

- `MARKUP`：MYR 估算成本 × `markup_multiplier`；
- `FIXED_RATE`：Acuven 自定的 MYR 分量价（`components`），与供应商成本无关。

**价格一律是 MYR 含税价（tax-inclusive，ADR-0008）**：FIXED_RATE 的 `rate_amount` 是含税单价；倍数乘出来的就是含税
计费额（倍数本身没有「含税」与否之分）。本任务不存税额，录错不会报错（ADR-0008 已接受的代价）。

状态 `DRAFT → PUBLISHED → RETIRED`，草稿也可以 `DISCARDED`。已发布的规则在 `[effective_from, effective_to)` 内生效；
同一范围的区间首尾相接、不重叠，只在末尾追加；**只有 `GLOBAL` 范围的第一条可以对过去生效**。倍数（即 markup）只在
管理端可见（INV-7）。**不含**计费额计算、舍入、试算、税额与前端。

| 方法与路径 | 请求体 | 成功 | 错误 |
| --- | --- | --- | --- |
| `GET /api/v1/admin/pricing-rules` | — | 200，规则分页 | 401 / 403 / 404 / 422 / 503 |
| `POST /api/v1/admin/pricing-rules` | 见下 | 201，规则（草稿） | 401 / 403 / 404 / 409 / 422 / 500 / 503 |
| `GET /api/v1/admin/pricing-rules/{rule_id}` | — | 200，规则 | 401 / 403 / 404 / 503 |
| `PATCH /api/v1/admin/pricing-rules/{rule_id}` | 见下 | 200，规则 | 401 / 403 / 404 / 409 / 422 / 500 / 503 |
| `POST /api/v1/admin/pricing-rules/{rule_id}/publish` | 见下 | 200，规则 | 401 / 403 / 404 / 409 / 422 / 500 / 503 |
| `POST /api/v1/admin/pricing-rules/{rule_id}/retire` | `{"reason": …}` | 200，规则 | 401 / 403 / 404 / 409 / 422 / 500 / 503 |
| `POST /api/v1/admin/pricing-rules/{rule_id}/discard` | `{}` | 200，规则 | 401 / 403 / 404 / 409 / 422 / 500 / 503 |

只有 ADMIN 能调（处理函数第一条语句就是鉴权）。路径里的 `rule_id` 是规则的 `public_id`；客户用 `customer_id`（租户
的 `public_id`）；响应里没有内部自增 id，人一律用登录邮箱表示。所有请求体都拒绝多余字段（422）。**没有删除接口**：
草稿只能丢弃，规则行永不删除。spec §89 没有列发布接口（§66 有 `PRICING_PUBLISH`），这里按 §89「允许调整命名」补上。

本节专有的错误码：

| HTTP | `error.code` | 什么时候 | 写库 |
| --- | --- | --- | --- |
| 404 | `PRICING_RULE_NOT_FOUND` | 路径里的规则不存在 | 否 |
| 404 | `CUSTOMER_NOT_FOUND` | 请求体或筛选里的客户不存在 | 否 |
| 404 | `AI_PROVIDER_NOT_FOUND` | 请求体或筛选里的供应商不存在 | 否 |
| 404 | `AI_MODEL_NOT_FOUND` | 模型不存在，或不属于那个供应商（两者一模一样） | 否 |
| 404 | `USAGE_METER_COMPONENT_NOT_FOUND` | `component_code` 不是目录里的计价分量 | 否 |
| 409 | `PRICING_RULE_NOT_DRAFT` | 编辑或丢弃一条已发布的规则 | 否 |
| 409 | `PRICING_RULE_INCOMPLETE` | 发布 FIXED_RATE 规则时完整性不满足；`message` 列出缺的分量代码 | 否 |
| 409 | `CATALOG_ITEM_RETIRED` | 规则引用的供应商、模型或某个分量所属的计量类型已停用（建草稿、改草稿、发布时检查） | 否 |
| 409 | `PRICING_RULE_NOT_RETIRABLE` | 停用一条已被后继截断的历史规则，或停用草稿 | 否 |
| 409 | `PRICING_RULE_FINAL` | 对已停用 / 已丢弃的规则做任何写操作 | 否 |
| 409 | `EFFECTIVE_FROM_CONFLICT` | 生效时刻不晚于该范围末尾规则的起点，或早于它的尽头；不指定时刻而末尾规则尚未开始 | 否 |
| 422 | `EFFECTIVE_FROM_IN_PAST` | 请求的生效时刻早于 `t`（不许回溯） | 否 |
| 422 | `VALIDATION_ERROR` | 范围字段与 `priority_scope` 不匹配、策略与字段混合、格式、精度、正数、多余字段 | 否 |
| 500 | `INTERNAL_ERROR` | 意外错误（含发布 / 停用后区间复查不通过、锁等待超时）；整个事务回滚 | 否 |

### 范围（spec §16，从高到低）

| `priority_scope` | `customer_id` | `provider_id` | `model_id` |
| --- | --- | --- | --- |
| `CUSTOMER_PROVIDER_MODEL` | 必填 | 必填 | 必填 |
| `CUSTOMER_PROVIDER` | 必填 | 必填 | 不许给 |
| `CUSTOMER` | 必填 | 不许给 | 不许给 |
| `GLOBAL_PROVIDER_MODEL` | 不许给 | 必填 | 必填 |
| `GLOBAL` | 不许给 | 不许给 | 不许给 |

多给或少给都是 422 `VALIDATION_ERROR`（`null` 等同不给）；§16 之外的组合（例如全局 + 供应商不带模型）写不进去，数据库
的 CHECK 也拒绝。模型必须属于给出的供应商。客户的账户状态不限：停用的客户也可以预先配置价格。

### 字段规则

| 字段 | 规则 |
| --- | --- |
| `strategy` | `MARKUP` 或 `FIXED_RATE` |
| `markup_multiplier` | 只有 MARKUP 有，且必填；**乘 MYR 估算成本，得到含税计费额** |
| `components` | 只有 FIXED_RATE 有，且必填（草稿可以是空列表：完整性在发布时校验）；0–64 个，`component_code` 不重复 |
| `components[].component_code` | 目录里的计价分量代码，例如 `LLM_INPUT_TOKEN`、`AUDIO_SECOND` |
| `components[].unit_quantity` | 多少个计量单位对应一个 `rate_amount`，例如 `"1000000"` |
| `components[].rate_amount` | **MYR 含税单价**（tax-inclusive）；币种固定为 MYR，请求里不收 `currency` |

MARKUP 带 `components`、FIXED_RATE 带 `markup_multiplier`、MARKUP 不带倍数、FIXED_RATE 不带 `components`：一律 422
（混合的规则数据库也拒绝：CHECK 与复合外键）。`markup_multiplier`、`unit_quantity`、`rate_amount` **只收 JSON 字符串**，
正数，`^[0-9]{1,12}(\.[0-9]{1,8})?$`：JSON 数字、`0`、负号、正号、指数写法、空白一律 422；**超过 8 位小数是 422，
不舍入**。响应里都是恰好 8 位小数的字符串。

### 规则对象

```json
{
  "id": "00000000-0000-4000-8000-000000000000",
  "priority_scope": "CUSTOMER_PROVIDER_MODEL",
  "customer_id": "00000000-0000-4000-8000-000000000000",
  "provider_id": "00000000-0000-4000-8000-000000000000",
  "provider_code": "anthropic",
  "model_id": "00000000-0000-4000-8000-000000000000",
  "model_code": "claude-x",
  "strategy": "FIXED_RATE",
  "markup_multiplier": null,
  "status": "PUBLISHED",
  "effective_from": "2026-09-29T08:30:01",
  "effective_to": null,
  "components": [
    {
      "component_code": "LLM_INPUT_TOKEN",
      "meter_type_code": "LLM_TOKEN",
      "unit": "TOKEN",
      "unit_quantity": "1000000.00000000",
      "rate_amount": "1.11111111",
      "currency": "MYR",
      "created_at": "2026-09-29T08:30:00"
    }
  ],
  "created_by_email": "admin@example.com",
  "approved_by_email": "admin@example.com",
  "created_at": "2026-09-29T08:30:00",
  "updated_at": "2026-09-29T08:30:00",
  "approved_at": "2026-09-29T08:30:00"
}
```

（示例里的价格是虚构值，MYR 含税。）

| 字段 | 说明 |
| --- | --- |
| `customer_id` / `provider_id` / `model_id` | 按 `priority_scope` 有值，其余为 `null`；`provider_code` / `model_code` 同理 |
| `markup_multiplier` | MARKUP 的倍数；FIXED_RATE 为 `null` |
| `status` | `DRAFT` / `PUBLISHED` / `RETIRED` / `DISCARDED` |
| `effective_from` | 草稿为 `null`；已发布时 `null` 只有一种含义：`GLOBAL` 范围的第一条、未指定生效时刻 =「一直以来」 |
| `effective_to` | `null` = 仍生效（或尚未发布）。`effective_from == effective_to` 是空区间（被撤销的预约，或同一秒里发布又停用），永不命中，只出现在 `RETIRED` 上 |
| `components` | FIXED_RATE 的分量，`component_code` 升序；MARKUP 为空列表 |

时间都是不带时区的 UTC，精确到秒。

#### `GET /api/v1/admin/pricing-rules` —— 规则列表（spec §59「查看价格历史」）

按范围（§16 的顺序，同一级里同一范围的聚在一起）、`effective_from`（`null` 在前，含草稿）排序，每项带分量。查询参数：
`priority_scope`、`customer_id`、`provider_id`、`model_id`（`public_id`，最长 64）、
`status=DRAFT|PUBLISHED|RETIRED|DISCARDED`，以及[分页](#分页spec-108)。筛选里的客户不存在 404 `CUSTOMER_NOT_FOUND`；
供应商不存在 404 `AI_PROVIDER_NOT_FOUND`；模型不存在（或同时给了供应商而模型不属于它）404 `AI_MODEL_NOT_FOUND`。

#### `POST /api/v1/admin/pricing-rules` —— 建草稿

请求体：`priority_scope`、按范围给的 `customer_id` / `provider_id` / `model_id`、`strategy`、`markup_multiplier`
（MARKUP）或 `components`（FIXED_RATE）。201；同一事务写规则、分量与一条 `PRICING_CREATE`。草稿**可以不完整**。
供应商、模型或分量所属计量类型已停用：409 `CATALOG_ITEM_RETIRED`。不拿规则锁；重发会建出两个草稿（草稿不影响计费，
丢弃多余的即可）。

#### `PATCH /api/v1/admin/pricing-rules/{rule_id}` —— 改草稿（spec §89）

只收 `strategy`、`markup_multiplier`、`components`，至少一个，都不许是 `null`；`components` 是**整体替换**。**范围
字段不可改**（带 `priority_scope`、`customer_id`、`provider_id`、`model_id` 是 422；改范围请新建）。与规则现状合起来：

- 改成（或仍是）MARKUP：倍数取请求里的，没给就沿用原来的（原本是 FIXED_RATE 而没给倍数：422）；带 `components` 422；
  原有分量全部删掉；
- 改成（或仍是）FIXED_RATE：带 `markup_multiplier` 422；倍数清空；分量取请求里的，没给就沿用原来的（原本是 MARKUP
  则为空）。

只对草稿：已发布 409 `PRICING_RULE_NOT_DRAFT`，已停用 / 已丢弃 409 `PRICING_RULE_FINAL`。目录检查同建草稿。**没有
实际变化**（分量按代码与数值比较，`1.5` 与 `1.50000000` 相同）：200，什么都不写；有变化时写一条 `PRICING_UPDATE`。

#### `POST /api/v1/admin/pricing-rules/{rule_id}/publish` —— 发布

请求体：可选 `effective_from`，RFC 3339 且**必须带时区**；换算成 UTC 后必须是整秒。不带或 `null` = 不指定。

在 `pricing_rule_locks` 那一行的排他锁（`SELECT … FOR UPDATE`）内完成：拿到锁之后取 `t` = 服务端当前时间**向上**取整
到下一个整秒。每个范围（`scope_key`）是一条只在末尾追加的时间线；设该范围**区间非空**的已发布规则中 `effective_from`
最晚的一条为末尾规则 L：

| 情形 | 本规则的 `effective_from` | 对 L 做什么 |
| --- | --- | --- |
| `GLOBAL` 范围、该范围从没有已发布规则，不指定 | `null`（一直以来：此前未被更高级覆盖的时刻都没有规则、从未扣过钱） | — |
| 其他范围（或 `GLOBAL` 已有过已发布规则）、没有 L，不指定 | `t`（**不回溯**：回溯会让已按更低一级计过费的时刻改选这一级） | — |
| 没有 L，指定 F | F | — |
| L 未截断且已开始，不指定 | `t` | L 截断于 `t` |
| L 未截断且尚未开始，不指定 | 409 `EFFECTIVE_FROM_CONFLICT`，什么都不写 | — |
| L 未截断，指定 F | F（必须晚于 L 的起点，否则 409） | L 截断于 F |
| L 已截断或已停用，尽头 E | `max(E, F 或 t)`；F 早于 E 是 409 | —（停用留下的空档不回填） |

- 指定的 F 早于 `t`：422 `EFFECTIVE_FROM_IN_PAST`。F 等于 `t` 可以。
- FIXED_RATE 的完整性：至少一个分量；**出现的计量类型，它的全部分量都必须出现**（出现 `LLM_TOKEN` 就必须有输入、输出、
  缓存写入、缓存读取四个价）。不满足：409 `PRICING_RULE_INCOMPLETE`。MARKUP 只需倍数（数据库已保证 > 0）。
- 目录已停用：409 `CATALOG_ITEM_RETIRED`。已停用 / 已丢弃：409 `PRICING_RULE_FINAL`。
- **已发布再发布**：200，返回当前规则，什么都不写（幂等）。
- 成功时同一事务写：（需要时）截断 L、本规则改为已发布、复查该范围的区间、一条 `PRICING_PUBLISH`；复查不通过整体
  回滚（500）。

**一个（租户, 供应商, 模型, 时刻）一旦选到某条规则，以后永远选到同一条**（原本无规则的时刻除外）：发布只影响 `t` 及
以后，只有全局默认的第一条把「无规则」变成「有规则」。

#### `POST /api/v1/admin/pricing-rules/{rule_id}/retire` —— 停用（spec §59「disable rule」）

请求体：`reason`，去首尾空白后 1–255（记在审计的 `reason` 上）。在规则锁内完成，`t` 取法同发布：

| 被停用的规则 | 做什么 |
| --- | --- |
| 当前末尾、已开始生效（起点为 `null` 或 ≤ `t`） | `effective_to = t`、`RETIRED`：`t` 起该范围不再命中，事件**下落到更低一级**（或无规则 → 计费侧 `PRICING_ERROR`） |
| 当前末尾、尚未开始（预约的，起点 > `t`） | `effective_to = effective_from`（空区间，永不命中）、`RETIRED`；前一条若仍是 `PUBLISHED` 且正是被它截断的，恢复为未截断（撤销预约） |
| 已被后继截断的历史规则、草稿 | 409 `PRICING_RULE_NOT_RETIRABLE` |
| 已停用 / 已丢弃 | 409 `PRICING_RULE_FINAL` |

停用后的空档不回填：停用的意思就是「此后这一级不再适用」。停用的规则在它自己的区间里照样命中。

#### `POST /api/v1/admin/pricing-rules/{rule_id}/discard` —— 丢弃草稿

请求体是 `{}`。草稿 → `DISCARDED`，行留着（不删除），写一条 `PRICING_DISCARD`。已发布 409 `PRICING_RULE_NOT_DRAFT`；
已停用 / 已丢弃 409 `PRICING_RULE_FINAL`。

### 审计

五个动作都与写入同一事务，不写 outbox；操作者带 ip 与 user agent。`entity_type` = `pricing_rule`，`entity_id` = 规则
的 `id`。范围里客户用租户 `public_id`（`customer_id`），供应商、模型、分量一律用 `code`，被截断 / 被恢复的规则用
`public_id`，发布人用邮箱；不含内部 id。审计里有倍数与单价：管理员可见的业务数据；应用日志不打印分量明细。

| `action` | `before_state` | `after_state` |
| --- | --- | --- |
| `PRICING_CREATE` | — | `priority_scope`、`customer_id`、`provider_code`、`model_code`、`strategy`、`markup_multiplier`、`components`（每项 `component_code`、`unit_quantity`、`rate_amount`）、`status` |
| `PRICING_UPDATE` | 变化的字段（`strategy` / `markup_multiplier` / `components`）的旧值 | 变化的字段的新值 |
| `PRICING_DISCARD` | `status`（`DRAFT`） | `status`（`DISCARDED`） |
| `PRICING_PUBLISH` | `status`（`DRAFT`）；截断了前一条时 `truncated_rule`：它的 `id` 与原 `effective_to`（`null`） | 范围四项、`strategy`、`markup_multiplier`、`components`、`status`、`effective_from`、`effective_to`、`approved_by_email`、`approved_at`；截断了前一条时 `truncated_rule`：它的 `id` 与新的 `effective_to` |
| `PRICING_RETIRE` | `status`、`effective_from`、`effective_to`；恢复了前一条时 `restored_rule`：它的 `id` 与原 `effective_to` | 范围四项、`status`、`effective_from`、`effective_to`；恢复了前一条时 `restored_rule`：它的 `id` 与 `effective_to`（`null`）。`reason` 记在审计行的 `reason` 上 |

「已发布再发布」与无变化的 PATCH 不写审计。`PRICING_CREATE` / `PRICING_UPDATE` / `PRICING_PUBLISH` 在 spec §66 的
清单里；`PRICING_RETIRE`、`PRICING_DISCARD` 按先例补上（见 [TODO 历史记录](archive/TODO_RECORDS_2026-09-30.md) 的 AIH-TASK-027 记录段）。

### 解析（计费侧调用，本任务不接线）

`app/repositories/pricing_rules.py`：

- `lock_pricing_rules_shared(session)`：对 `pricing_rule_locks` 那一行取 `FOR SHARE`，持有到调用方的事务提交；
- `resolve_pricing_rule(session, tenant_id, provider_id, model_id, occurred_at)`：参数是事件的内部 `tenant_id`、
  `resolve_model` 返回的模型的内部 `provider_id` / `id`，以及不带时区的 UTC `occurred_at`。按
  `CUSTOMER_PROVIDER_MODEL → CUSTOMER_PROVIDER → CUSTOMER → GLOBAL_PROVIDER_MODEL → GLOBAL` 逐级加锁读（`FOR SHARE`）
  该级范围内状态为 `PUBLISHED` 或 `RETIRED`、`effective_from ≤ occurred_at < effective_to` 的规则，**第一级命中即返回**
  （规则 + 分量；命中的 FIXED_RATE 规则缺事件所需分量也**不下落**），五级都没有返回 `None`（计费侧 `PRICING_ERROR`）。

计费事务里的顺序：`resolve_model`（供应商 S）→ `lock_pricing_rules_shared` → 价格与规则的加锁读 → 钱包 → 租户；返回
的规则内部 id 写进事件快照（`pricing_rule_id`）。契约全文见设计 §2「对下游任务的契约」与 [TODO 历史记录](archive/TODO_RECORDS_2026-09-30.md) 的
AIH-TASK-027 记录段。

---

## 管理端试算预览

设计依据：设计闸门 #179 `APPROVED: design v1`，全文见
[design/AIH-TASK-031-pricing-engine.md](design/AIH-TASK-031-pricing-engine.md)（spec §14、§15、§15.1、§16、§17.1、§59、
§80；ADR-0005、ADR-0008）。实现登记为 AIH-TASK-031。算法、舍入与错误顺序见 [pricing-engine.md](pricing-engine.md) 的
「计算」。

按显式的 `occurred_at` 试算一个用量：列出解析到的模型、价格版本、汇率版本、定价规则，每个分量的数量与未舍入的成本 /
客户分量价，以及未舍入与舍入后的三个金额（spec §59「preview calculation」）。**与计费同一套解析与计算代码**，只是不取
两张单行锁表的共享锁。**只读**：不写库、不写审计。只用已发布（`PUBLISHED` / `RETIRED`）的版本与规则，不能用草稿试算。
响应里有供应商成本、汇率与倍数，只在管理端（INV-7）。

| 方法与路径 | 请求体 | 成功 | 错误 |
| --- | --- | --- | --- |
| `POST /api/v1/admin/pricing-preview` | 见下 | 200，试算结果（含错误状态） | 401 / 403 / 404 / 422 / 500 / 503 |

只有 ADMIN 能调（处理函数第一条语句就是鉴权）。

### 请求体

| 字段 | 规则 |
| --- | --- |
| `customer_id` | 客户（租户）的 `public_id`；不存在 404 `CUSTOMER_NOT_FOUND` |
| `provider` | 供应商代码，`^[a-z0-9][a-z0-9_-]{0,63}$` |
| `model` | 模型代码或别名，`^[A-Za-z0-9][A-Za-z0-9._:/@-]{0,127}$`；按 `occurred_at` 解析（含别名段） |
| `usage_type` | 计量类型 `code`；不存在 404 `USAGE_METER_TYPE_NOT_FOUND` |
| `input_tokens` / `output_tokens` / `cache_creation_input_tokens` / `cache_read_input_tokens` | `LLM_TOKEN_FIELDS` 形态的类型（`LLM_TOKEN`）：四个都必填，JSON 整数，0–10¹²；其余类型不许出现 |
| `quantity` / `unit` | `QUANTITY` 形态的类型：`quantity` 必填，**JSON 字符串** `^[0-9]{1,12}(\.[0-9]{1,8})?$`（整数类型不许有小数部分，`"3.0"` 也拒绝）；`unit` 必须等于该类型的单位；`LLM_TOKEN` 不许出现 |
| `occurred_at` | 必填，RFC 3339 且**必须带时区**，最多 6 位小数秒；**可以是将来**（预约生效的价格与规则也能预览） |

与摄取同一套字段写法（[集成：用量事件摄取](#集成用量事件摄取)）：严格类型（`true` 不是整数，数字不是字符串），多余
字段 422。

### 响应

```json
{
  "status": "PRICED",
  "error_code": null,
  "model": {"provider": "anthropic", "model": "claude-x", "matched_via": "code"},
  "provider_price_version": {
    "id": "00000000-0000-4000-8000-000000000000",
    "source_currency": "USD",
    "effective_from": null,
    "effective_to": null
  },
  "fx_rate_version": {
    "id": "00000000-0000-4000-8000-000000000000",
    "rate": "4.4444444444",
    "observed_at": "2026-09-29T08:30:00"
  },
  "pricing_rule": {
    "id": "00000000-0000-4000-8000-000000000000",
    "priority_scope": "GLOBAL",
    "strategy": "MARKUP",
    "markup_multiplier": "2.00000000"
  },
  "components": [
    {
      "component_code": "LLM_CACHE_READ_TOKEN",
      "quantity": "3000",
      "provider_cost_unrounded": "0.00045",
      "customer_price_unrounded": null
    }
  ],
  "provider_source_cost_unrounded": "0.0177",
  "provider_source_cost": "0.01770000",
  "estimated_provider_cost_myr_unrounded": "0.07866666666588",
  "estimated_provider_cost_myr": "0.07866667",
  "billable_cost_unrounded": "0.15733333333176",
  "billable_cost": "0.15733333",
  "tax_inclusive": true
}
```

（示例里的价格、汇率与倍数是虚构值；`components` 只列了一项。）

| 字段 | 说明 |
| --- | --- |
| `status` | `PRICED` / `MODEL_UNKNOWN` / `PRICING_ERROR` / `FX_RATE_ERROR` |
| `error_code` | `PRICING_ERROR` 的细分码：`NO_PROVIDER_PRICE` / `MISSING_PROVIDER_COMPONENT` / `NO_PRICING_RULE` / `MISSING_RULE_COMPONENT`；其余状态为 `null` |
| `model` | 解析到的模型：`model` 是模型自己的代码（用别名试算时也是），`matched_via` 是 `code` 或 `alias` |
| `provider_price_version` | 生效的价格版本；`effective_from` / `effective_to` 为 `null` 表示「一直以来」/「仍生效」 |
| `fx_rate_version` | 原币不是 MYR 时生效的汇率版本，`rate` 是原值（1 单位原币 = 多少 MYR，不舍入）；原币是 MYR 时为 `null`（不查汇率） |
| `pricing_rule` | 命中的规则；`markup_multiplier` 只有 MARKUP 有 |
| `components` | 该计量类型的全部分量，`component_code` 升序：数量、未舍入的供应商原币成本、未舍入的客户分量价（MYR 含税，只有 FIXED_RATE 有） |
| `*_unrounded` | 未舍入的中间值，十进制字符串（最多 50 位有效数字），从不是 JSON 数字 |
| `provider_source_cost` / `estimated_provider_cost_myr` / `billable_cost` | 存储值：恰好 8 位小数，各自由未舍入值 `ROUND_HALF_UP` 舍入一次。「`estimated_provider_cost_myr` × 倍数」与 `billable_cost` 可以差最后一位（见 [pricing-engine.md](pricing-engine.md)） |
| `tax_inclusive` | 恒为 `true`：`billable_cost` 是含税金额（ADR-0008），不拆税 |

**错误状态也是 200**，给出已解析到的部分，未解析到的为 `null`、`components` 为空、六个金额为 `null`。错误按「模型 →
价格 → 汇率 → 规则」的顺序判定，先遇到哪个报哪个：例如同时缺价格与汇率时报 `PRICING_ERROR` / `NO_PROVIDER_PRICE`；
模型与价格都有、缺汇率时 `model`、`provider_price_version` 有值，`fx_rate_version`、`pricing_rule` 为 `null`。命中的
FIXED_RATE 规则缺该计量类型的分量时是 `MISSING_RULE_COMPONENT`，不下落到更低一级的规则。

本节专有的错误码：

| HTTP | `error.code` | 什么时候 | 写库 |
| --- | --- | --- | --- |
| 404 | `CUSTOMER_NOT_FOUND` | `customer_id` 不存在 | 否 |
| 404 | `USAGE_METER_TYPE_NOT_FOUND` | `usage_type` 不是计量类型 | 否 |
| 422 | `VALIDATION_ERROR` | 格式、类型、多余字段；用量字段与计量类型的形态不符；`occurred_at` 不是日历上存在的时刻 | 否 |
| 422 | `AMOUNT_OUT_OF_RANGE` | 某个存储值超出 DECIMAL(20,8)（计费侧会把这样的事件标 `FAILED_FINAL`） | 否 |

---

## 管理端汇率

设计依据：设计闸门 #183 `APPROVED: design v3`，全文见
[design/AIH-TASK-028-fx-rates.md](design/AIH-TASK-028-fx-rates.md)（spec §17.1、§58、§74.2；ADR-0005）。规则层是
AIH-TASK-039，接口层是 AIH-TASK-041（本节）。规则全文（时间线、P1–P4、R1–R5、锁顺序）见
[currency-and-fx.md](currency-and-fx.md)。

一个**汇率版本**是某个外币（`base_currency`）对 MYR 的一版汇率：`rate` = **1 单位 `base_currency` 等于多少 MYR**。
来源是 BNM 自动拉取（每天三次，只写草稿）或管理员手工录入。状态 `DRAFT → PUBLISHED → RETIRED`，草稿也可以
`DISCARDED`。已发布的版本在 `[effective_from, effective_to)` 内生效，同一币种的区间互不重叠、只在末尾追加；
**汇率从发布时刻起生效，不从报价日起算**。自动拉取绝不发布：发布只经这里，由管理员手工做。**不含**用汇率算钱、
内部告警接口与前端。

| 方法与路径 | 请求体 | 成功 | 错误 |
| --- | --- | --- | --- |
| `GET /api/v1/admin/fx-rates` | — | 200，版本分页 | 401 / 403 / 422 / 503 |
| `POST /api/v1/admin/fx-rates` | 见下 | 201，版本（草稿） | 401 / 403 / 422 / 500 / 503 |
| `GET /api/v1/admin/fx-rates/fetch-attempts` | — | 200，拉取记录分页 | 401 / 403 / 422 / 503 |
| `GET /api/v1/admin/fx-rates/{fx_rate_id}` | — | 200，版本 | 401 / 403 / 404 / 503 |
| `PATCH /api/v1/admin/fx-rates/{fx_rate_id}` | 见下 | 200，版本 | 401 / 403 / 404 / 409 / 422 / 500 / 503 |
| `POST /api/v1/admin/fx-rates/{fx_rate_id}/publish` | 见下 | 200，版本 | 401 / 403 / 404 / 409 / 422 / 500 / 503 |
| `POST /api/v1/admin/fx-rates/{fx_rate_id}/retire` | `{"reason": …}` | 200，版本 | 401 / 403 / 404 / 409 / 422 / 500 / 503 |
| `POST /api/v1/admin/fx-rates/{fx_rate_id}/discard` | `{}` | 200，版本 | 401 / 403 / 404 / 409 / 422 / 500 / 503 |

只有 ADMIN 能调（处理函数第一条语句就是鉴权）。路径里的 `fx_rate_id` 是版本的 `public_id`；响应里没有内部自增 id，
人一律用登录邮箱表示。所有请求体都拒绝多余字段（422）。**没有删除接口**：草稿只能丢弃，版本行永不删除。
`fetch-attempts` 先于 `{fx_rate_id}` 注册，不会被当成版本 id。

**请求校验先于状态判断**：请求体与查询参数在看版本状态之前校验，格式不对一律 422，与版本是草稿、已发布还是不存在
无关。**读接口（列表、详情、拉取记录）是普通读，不拿发布用的全局锁**：它们只展示，不参与区间计算，发布不必等它们。

本节专有的错误码：

| HTTP | `error.code` | 什么时候 | 写库 |
| --- | --- | --- | --- |
| 404 | `FX_RATE_NOT_FOUND` | 路径里的版本不存在 | 否 |
| 409 | `FX_RATE_NOT_DRAFT` | 编辑或丢弃一个已发布的版本 | 否 |
| 409 | `FX_RATE_NOT_EDITABLE` | 编辑 BNM 草稿（出处不同：要改就丢弃后手工录入） | 否 |
| 409 | `FX_RATE_NOT_RETIRABLE` | 退役草稿（草稿用丢弃），或退役一个已被截断的已发布版本 | 否 |
| 409 | `FX_RATE_FINAL` | 对已退役 / 已丢弃的版本做任何写操作 | 否 |
| 409 | `EFFECTIVE_FROM_CONFLICT` | 末尾版本尚未开始时不指定时刻发布，或指定的时刻不晚于它的起点（见「发布」） | 否 |
| 422 | `EFFECTIVE_FROM_IN_PAST` | 请求的生效时刻早于 `t`（不许回溯） | 否 |
| 422 | `VALIDATION_ERROR` | 格式、精度、正数、时区、整秒、币种、多余字段；列表的 `status` / `source` / `outcome` 不是合法取值 | 否 |
| 500 | `INTERNAL_ERROR` | 意外错误（含发布 / 退役后区间复查不通过、锁等待超时）；整个事务回滚 | 否 |

### 字段规则

| 字段 | 规则 |
| --- | --- |
| `base_currency` | 大写三字母 `^[A-Z]{3}$`（`usd` 是 422），不能是 `MYR`。**不限于** `BILLING_FX_CURRENCIES`：那是 BNM 自动拉取的范围，手工录入正是为 BNM 不覆盖的币种与日期准备的。建后不可改 |
| `rate` | **只收 JSON 字符串**，正数，`^[0-9]{1,14}(\.[0-9]{1,10})?$`：JSON 数字、`0`、负号、正号、指数写法、逗号、空白一律 422；**超过 10 位小数是 422，不舍入**；精确解析，不经过浮点数 |
| `observed_at` | 录入者给出的观测时刻。RFC 3339 且**必须带时区**（`2026-09-29T04:00:00Z`、`2026-09-29T12:00:00+08:00`）；换算成 UTC 后必须是整秒（`.000` 可以，`.5` 是 422）；存 UTC |
| `source_reference` | 去首尾空白后 1–255：汇率出处（例如「某银行公告，查看于 2026-09-29」） |

### 版本对象

```json
{
  "id": "00000000-0000-4000-8000-000000000000",
  "base_currency": "USD",
  "quote_currency": "MYR",
  "rate": "1.2345678901",
  "source": "MANUAL",
  "source_reference": "Fictional bank notice, viewed 2026-09-29",
  "source_quote_date": null,
  "observed_at": "2026-09-29T04:00:00",
  "status": "PUBLISHED",
  "effective_from": "2026-09-29T08:30:01",
  "effective_to": null,
  "created_by_email": "admin@example.com",
  "approved_by_email": "admin@example.com",
  "approved_at": "2026-09-29T08:30:00",
  "created_at": "2026-09-29T08:30:00",
  "updated_at": "2026-09-29T08:30:00"
}
```

（示例里的汇率是虚构值。）

| 字段 | 说明 |
| --- | --- |
| `rate` | 精确的十进制字符串，去掉末尾的 0（`"4.083"`，不是 `"4.0830000000"`），从不是 JSON 数字 |
| `quote_currency` | 永远是 `MYR` |
| `source` | `BNM`（自动拉取）或 `MANUAL`（手工录入） |
| `source_reference` | BNM：`bnm:exchange-rate:<币种>:<报价日>:session=1200:middle_rate:unit=<n>`；手工：录入者写的出处 |
| `source_quote_date` | BNM 的报价日（吉隆坡日期）；手工录入为 `null` |
| `observed_at` | BNM：报价日 12:00 吉隆坡（= 04:00 UTC）；手工：录入者给出的时刻 |
| `status` | `DRAFT` / `PUBLISHED` / `RETIRED` / `DISCARDED` |
| `effective_from` | 草稿为 `null`；已发布时 `null` 只有一种含义：发布时该币种没有区间非空的已发布版本、且未指定生效时刻 =「一直以来」 |
| `effective_to` | `null` = 仍生效（或尚未发布）。`effective_from == effective_to` 是空区间（被撤销的预约，或同一秒里发布又退役），永不生效，只出现在 `RETIRED` 上 |
| `created_by_email` | 手工草稿的录入人；BNM 草稿为 `null`（系统写入） |
| `approved_by_email` / `approved_at` | 发布人与发布时刻；未发布时为 `null` |

时间都是不带时区的 UTC，精确到秒。

#### `GET /api/v1/admin/fx-rates` —— 版本列表（spec §58 的草稿、审批与版本历史）

按 `id` 倒序（最新建的在前）。查询参数：`base_currency`（`^[A-Z]{3}$`）、`status=DRAFT|PUBLISHED|RETIRED|DISCARDED`、
`source=BNM|MANUAL`，以及[分页](#分页spec-108)。取值不合法（含小写）是 422。

#### `POST /api/v1/admin/fx-rates` —— 手工建草稿

请求体：`base_currency`、`rate`、`observed_at`、`source_reference`（都必填，都不许 `null`）。201；同一事务写版本
（`source = MANUAL`、`source_quote_date = null`）与一条 `FX_RATE_CREATE`。不拿全局锁（草稿不参与计费）；重发会建出
两个草稿，丢弃多余的即可。

#### `PATCH /api/v1/admin/fx-rates/{fx_rate_id}` —— 改手工草稿

只收 `rate`、`observed_at`、`source_reference`，至少一个，都不许是 `null`。带 `base_currency`、`status`、`source`、
`effective_from` 或任何多余字段是 422。只对**手工来源的草稿**：BNM 草稿 409 `FX_RATE_NOT_EDITABLE`；已发布 409
`FX_RATE_NOT_DRAFT`；已退役 / 已丢弃 409 `FX_RATE_FINAL`。**没有实际变化**（`rate` 按数值比较，`2.5` 与
`2.5000000000` 相同；时刻换算成 UTC 后比较）：200，什么都不写；有变化时写一条 `FX_RATE_UPDATE`。

#### `POST /api/v1/admin/fx-rates/{fx_rate_id}/publish` —— 发布

请求体：可选 `effective_from`（预约时刻 F），格式规则同 `observed_at`。不带或 `null` = 不指定。

在 `fx_rate_locks` 那一行的排他锁内完成：拿到锁之后取 `t` = 服务端当前时间**向上**取整到下一个整秒（恰好整秒时取
下一秒），所以 `t` 严格晚于此刻。设该币种**区间非空**的已发布（`PUBLISHED` / `RETIRED`）版本中 `effective_from`
最晚的一个为末尾版本 L（空区间不在时间线上）：

| 情形 | 不指定时刻 | 指定 F |
| --- | --- | --- |
| P1 没有 L | `[null, null)`（一直以来：此前该币种一律取不到汇率、从未扣过钱） | `[F, null)` |
| P2 L 未截断、已开始（起点为 `null` 或早于 `t`） | `[t, null)`；L 截断于 `t` | `[F, null)`；L 截断于 F |
| P3 L 未截断、尚未开始（预约的，或同一秒里刚发布的） | 409 `EFFECTIVE_FROM_CONFLICT`，什么都不写：先撤销预约、指定晚于它起点的时刻，或下一秒再发 | F 晚于 L 起点：`[F, null)`，L 截断于 F；否则 409 `EFFECTIVE_FROM_CONFLICT` |
| P4 L 有尽头 E（已退役，或它的后继被撤销） | `[E, null)`：补上退役留下的空档（那段时间此前一律取不到汇率） | `[F, null)`，`[E, F)` 保持无汇率 |

- 指定的 F 早于 `t`：422 `EFFECTIVE_FROM_IN_PAST`，先于上表的任何判断。F 等于 `t` 可以。
- 已退役 / 已丢弃：409 `FX_RATE_FINAL`。
- **已发布再发布**：200，返回当前版本，什么都不写；请求体里的 `effective_from` 被忽略（只要格式合法，早于 `t` 也不
  报 422）。格式不合法仍是 422（请求校验先于状态判断）。
- 成功时同一事务写：（需要时）截断 L、本版本改为已发布（发布人与发布时刻）、复查该币种的区间、一条
  `FX_RATE_PUBLISH`；复查不通过整体回滚（500）。

**一个（币种, 时刻）一旦取到某个版本，以后永远取到同一个版本**：发布只影响 `t` 及以后，例外只有 P1 与 P4（不指定
时刻）把「无汇率」变成「有汇率」。

#### `POST /api/v1/admin/fx-rates/{fx_rate_id}/retire` —— 退役

请求体：`reason`，去首尾空白后 1–255（记在审计的 `reason` 上）。在同一把锁内完成，`t` 取法同发布：

| 被退役的版本 | 做什么 |
| --- | --- |
| R1 未截断、已开始生效（起点为 `null` 或 ≤ `t`） | `effective_to = t`、`RETIRED`：`t` 起该币种取不到汇率（计费侧 `FX_RATE_ERROR`），直到下一次发布按 P4 从 `t` 起补上。起点恰好是 `t` 时得到空区间 `[t, t)`，前一个版本不恢复 |
| R2 未截断、尚未开始（预约的，起点 > `t`）：**撤销预约** | `effective_to = effective_from`（空区间，永不生效）、`RETIRED`；前一个版本若仍是 `PUBLISHED` 且正是被它截断的（尽头等于它的起点），恢复为未截断；以退役结束的不恢复 |
| R3 已被截断的已发布版本、R4 草稿 | 409 `FX_RATE_NOT_RETIRABLE` |
| R5 已退役 / 已丢弃 | 409 `FX_RATE_FINAL` |

退役的版本在它自己的区间里照样取得到（已经发生的用量照样按它算）。要纠正已计费事件用的汇率走 Phase 8 的
reprocess，不在这里。

#### `POST /api/v1/admin/fx-rates/{fx_rate_id}/discard` —— 丢弃草稿

请求体是 `{}`。草稿（手工与 BNM 都可以）→ `DISCARDED`，行留着（不删除），写一条 `FX_RATE_DISCARD`。已发布 409
`FX_RATE_NOT_DRAFT`；已退役 / 已丢弃 409 `FX_RATE_FINAL`。

#### `GET /api/v1/admin/fx-rates/fetch-attempts` —— BNM 拉取记录（spec §58「同步状态」）

按 `attempted_at` 倒序，同一时刻按内部顺序倒序。查询参数：`base_currency`（`^[A-Z]{3}$`）、
`outcome=NEW_DRAFT|NO_NEW_QUOTE|NO_QUOTE_FOR_DATE|FAILED`，以及[分页](#分页spec-108)。取值不合法是 422。每行：

```json
{
  "base_currency": "USD",
  "source": "BNM",
  "requested_date": "2026-09-29",
  "outcome": "NEW_DRAFT",
  "quote_date": "2026-09-29",
  "error_code": null,
  "fx_rate_id": "00000000-0000-4000-8000-000000000000",
  "attempted_at": "2026-09-29T04:30:00"
}
```

| 字段 | 说明 |
| --- | --- |
| `requested_date` | 向 BNM 要的报价日（吉隆坡当天） |
| `outcome` | `NEW_DRAFT`：写了新草稿；`NO_NEW_QUOTE`：取到报价，但该报价日已有 BNM 版本；`NO_QUOTE_FOR_DATE`：BNM 答「该日无记录」（周末、公众假期、中午场尚未公布）；`FAILED`：失败。前三种都是成功 |
| `quote_date` | 取到报价时的报价日；否则 `null` |
| `error_code` | `FAILED` 时：`TIMEOUT` / `HTTP_<status>` / `BAD_PAYLOAD` / `UNIT_NOT_EXACT` / `NETWORK`；`NO_NEW_QUOTE` 时可为 `QUOTE_CHANGED`（BNM 更正了同日价格，已有版本不变）；否则 `null` |
| `fx_rate_id` | `NEW_DRAFT` 时是那条草稿的 `id`（`public_id`），否则 `null` |

拉取记录只读、不可单条引用，所以没有 `id`，也不返回内部 id。

### 审计

五个动作都与写入同一事务，不写 outbox；操作者带 ip 与 user agent。`entity_type` = `fx_rate_version`，
`entity_id` = 版本的 `id`。被截断 / 被恢复的版本用 `public_id`，人用邮箱，时刻是 ISO 8601 UTC，`rate` 是字符串；
不含内部 id。BNM 草稿不写审计：拉取记录就是它的来源证明。

| `action` | `before_state` | `after_state` |
| --- | --- | --- |
| `FX_RATE_CREATE` | — | `base_currency`、`rate`、`observed_at`、`source_reference`、`status` |
| `FX_RATE_UPDATE` | 变化的字段的旧值 | 同一组字段的新值 |
| `FX_RATE_DISCARD` | `status`（`DRAFT`） | `status`（`DISCARDED`） |
| `FX_RATE_PUBLISH` | `status`（`DRAFT`）；截断了前一个版本时 `truncated_version`：它的 `id` 与原 `effective_to`（`null`） | `status`、`effective_from`、`effective_to`、`approved_by_email`、`approved_at`；截断了前一个版本时 `truncated_version`：它的 `id` 与新的 `effective_to` |
| `FX_RATE_RETIRE` | `status`、`effective_from`、`effective_to` | `status`、`effective_from`、`effective_to`、`reason`；恢复了前一个版本时 `restored_version`：它的 `id`。`reason` 也记在审计行的 `reason` 上 |

「已发布再发布」与无变化的 PATCH 不写审计。

---

## 管理端用量事件：重新入队

设计依据：设计闸门 #181 `APPROVED: design v2`，全文见
[design/AIH-TASK-032-usage-billing.md](design/AIH-TASK-032-usage-billing.md) 第 2 节「重新入队」（spec §83、§84）。
实现在 `app/api/admin_usage_events.py` 与 `app/services/usage_requeue.py`。

计费 worker 把无法计价的事件停在 `MODEL_UNKNOWN` / `PRICING_ERROR` / `FX_RATE_ERROR`（带细分码），把计算溢出、关户、
账本冲突、重试用尽的停在 `FAILED_FINAL`。管理员修好目录、价格、规则或汇率之后，用这两个接口把**从未产生财务效果**
的错误事件改回 `RECEIVED`，下一轮扫描（每 10 秒）照常计费。运维流程见 [runbook.md](runbook.md)「用量计费」。

| 方法与路径 | 请求体 | 成功 | 错误 |
| --- | --- | --- | --- |
| `POST /api/v1/admin/usage-events/{usage_event_id}/requeue` | `{"reason": …}` | 200，`{"id", "status"}` | 401 / 403 / 404 / 409 / 422 / 500 / 503 |
| `POST /api/v1/admin/usage-events/requeue` | 见下 | 200，`{"requeued", "skipped", "ids"}` | 401 / 403 / 404 / 422 / 500 / 503 |

- 只有 ADMIN 能调（处理函数第一条语句就是鉴权）。路径里的 `usage_event_id` 是事件的 `public_id`。
- 路径名是 `requeue`，**不是** spec §89 的 `reprocess`：Phase 8 的 reprocess 专指「对已计费的事件重算并生成调整行」，
  会改变已计费的金额；这里从不碰已计费的事件。
- 可重新入队：状态是上面四个错误状态之一，**且账本里没有这个事件的行**（`LEDGER_CONFLICT` 的 `FAILED_FINAL` 有，
  所以不行）。MySQL 上迁移 0018 的触发器同样拒绝把有账本行的事件改回 `RECEIVED`。
- 每个事件一个事务：锁事件行 → 改回 `RECEIVED`，清空 `error_code`、`error_message`、`attempt_count`、`next_attempt_at`
  → 写一条审计 `USAGE_EVENT_REQUEUE`（`entity_type` = `usage_event`，`entity_id` = 事件的 `public_id`，`reason`，
  `before_state` 是原状态、原错误码与尝试次数，`after_state` 是 `{"status": "RECEIVED"}`）→ 提交。
- **响应只含数量与事件的 `public_id`**，不含成本、金额或内部 id。没有自动重新入队：原因多样，自动放回可能反复失败。

### 单个

请求体 `{"reason": "…"}`：`reason` 必填，去首尾空白后 1–255；多余字段 422。成功：

```json
{ "id": "00000000-0000-4000-8000-000000000000", "status": "RECEIVED" }
```

### 批量

| 字段 | 规则 |
| --- | --- |
| `status` | **必填**，`MODEL_UNKNOWN` / `PRICING_ERROR` / `FX_RATE_ERROR` / `FAILED_FINAL` 之一，其余取值 422 |
| `error_code` | 可选，1–64；与事件的 `error_code` 逐字比较（`MODEL_UNKNOWN` / `FX_RATE_ERROR` 没有细分码） |
| `customer_id` | 可选，客户的 `public_id`；不存在 404 `CUSTOMER_NOT_FOUND` |
| `provider` / `model` | 可选，1–64 / 1–128；与上报的**原始字符串**逐字节比较（模型未知的事件没有目录引用） |
| `occurred_from` / `occurred_to` | 可选；RFC 3339、必须带时区、整秒；按 `occurred_at` 取 `[occurred_from, occurred_to)`，`occurred_from` 必须早于 `occurred_to`（否则 422） |
| `reason` | **必填**，去首尾空白后 1–255；写进每一条审计 |

按事件的内部 id 升序（≈ 接收顺序）**至多取 1000 条**，逐条一个事务。选出之后状态变了（已被别人放回、已被处理）或
账本里有它的行的事件跳过并计数，不写审计。超过 1000 条时再调一次即可（已放回的不会再被选中）。成功：

```json
{ "requeued": 2, "skipped": 1, "ids": ["00000000-0000-4000-8000-000000000001", "00000000-0000-4000-8000-000000000002"] }
```

`ids` 是放回了的事件的 `public_id`，按内部 id 升序。

本节专有的错误码：

| HTTP | `error.code` | 什么时候 | 写库 |
| --- | --- | --- | --- |
| 404 | `USAGE_EVENT_NOT_FOUND` | 单个：路径里的事件不存在 | 否 |
| 404 | `CUSTOMER_NOT_FOUND` | 批量：`customer_id` 不存在 | 否 |
| 409 | `USAGE_EVENT_NOT_REQUEUABLE` | 单个：事件不在四个错误状态之一（`RECEIVED`、`PROCESSING`、`PROCESSED`、`FAILED_RETRYABLE` 等），或账本里已有它的行 | 否 |
| 422 | `VALIDATION_ERROR` | 缺 `reason`、`reason` 为空白或超长、批量的 `status` 不是四个错误状态之一、时间格式或区间不对、多余字段 | 否 |

---

## 内部告警接口

设计依据：设计闸门 #183（[AIH-TASK-028-fx-rates.md](design/AIH-TASK-028-fx-rates.md) v3 第 2 节
「内部告警接口」「两个告警维度」、第 5 节令牌一行、第 6 节）。实现在 `app/api/internal_alerts.py` 与
`app/services/alerts.py`。

`GET /internal/alerts` 是给宿主机上 `deploy/monitor.sh` 读的**机器接口**，不给浏览器、也不给集成方：

- **不在 `/api/` 下，不套 §107 信封。**`monitor.sh` 是 bash，宿主机未必有 `jq`。所有响应（200、401、404、503）
  都是 `text/plain; charset=utf-8`；响应头照常带 `X-Request-ID`。
- 不在 OpenAPI 文档里。

### 两层访问控制

1. **nginx**：`location = /internal/alerts` 与 `/readyz` 同一份 allow 名单（回环、本栈网段、proxy_net），其余
   deny；另有 `location /internal/ { deny all; }`。⚠️ 这一层归 AIH-TASK-043，**在它合并之前这个路径经 nginx
   落到前端、外面不可达**。
2. **应用**：`Authorization: Bearer <令牌>`，令牌从 `BILLING_MONITOR_TOKEN_FILE` 所指的文件读（compose 里固定为
   `/run/secrets/billing_monitor_token`，宿主机文件 `secrets/monitor.token`），常量时间比较。需要第二层是因为
   proxy_net 上还有同机其他项目的容器。令牌只能读告警行，不是管理员凭据。

令牌文件的每种状态：

| `BILLING_MONITOR_TOKEN_FILE` | 文件 | 响应 |
| --- | --- | --- |
| 空（未配置） | — | 404 `not found`，不记日志（等于未启用） |
| 已配置 | 读不出：不存在、是目录（Docker Desktop 下 compose 缺 `file:` secret 时挂成目录；生产上缺文件则部署直接失败）、无权限 | 404 `not found`，每次请求一条 ERROR 日志 |
| 已配置 | 去首尾空白后少于 32 个字符（含空文件） | 404 `not found`，每次请求一条 ERROR 日志 |
| 已配置 | 可用 | 缺 `Authorization`、不是 `Bearer`、令牌不对 → 401 `unauthorized`（带 `WWW-Authenticate: Bearer`，不说明原因）；对 → 200 |

- 401 / 404 的正文只有 `unauthorized` / `not found` 一行。
- ERROR 日志不含文件路径与内容。
- **每次请求重新读文件**：轮换令牌（改文件内容）不需要重启。生成：`openssl rand -hex 32`（64 个字符）。
- **令牌判定先于任何数据库查询**：401 / 404 不碰数据库。
- 令牌通过、但服务没有配置数据库或查询出错 → 503，正文一行（`database not configured` / `unavailable`）。
  `monitor.sh` 把非 200 当成「接口不通」，不推告警维度，Healthchecks 宽限期后报「未上报」。

### 行格式

200 的正文每个维度一行，以换行结束，顺序固定（维度注册表 `app/services/alerts.py` 的 `DIMENSIONS`）：

```text
<维度> <OK|P1|P2> <一句摘要>
```

例如：

```text
fx_fetch OK USD last success 2026-09-29
fx_stale P2 USD quoted 2026-09-24 (6 calendar days); EUR no rate in effect
```

- 一个维度一行。多个币种汇总进这一行：级别取各配置币种（`BILLING_FX_CURRENCIES`，按它的顺序）里最严重的；
  有不 OK 的币种时摘要只列它们，各一小段、以 `; ` 分隔；都 OK 时列每个币种。
- 摘要只有计数、日期与币种，**不含租户、金额（汇率本身也不写）或任何个人数据**。
- 维度查询是普通读、不拿 `fx_rate_locks`：告警只是提示，不让汇率发布等它。
- 用量计费的七个维度（AIH-TASK-032，设计闸门 #181 v2 §2「告警维度」）追加在注册表末尾，行格式不变；它们的摘要只有
  计数、最早时刻（UTC，`YYYY-MM-DDTHH:MM:SSZ`）与至多 3 个细分码（字母序），不含租户与金额。

| 维度 | 何时告警 | 摘要 |
| --- | --- | --- |
| `fx_fetch` | 某个配置币种在最近 72 小时内**没有任何**成功的拉取记录（`NEW_DRAFT`、`NO_NEW_QUOTE`、`NO_QUOTE_FOR_DATE` 都算成功），且至少有一条 `FAILED` —— 即连续 3 天拉取失败。72 小时内一条记录都没有是 OK（从未运行或 Beat 停了，由 `fx_stale` 报） | `P2`：`USD no successful fetch in 72 hours (3 failed)`；OK：`USD last success <吉隆坡日期>` 或 `USD no fetch in 72 hours` |
| `fx_stale` | 某个配置币种**此刻生效**的版本（与 `resolve_fx_rate` 同一条件）的报价日距今**超过 5 个日历日**（吉隆坡日期相减；BNM 版本用报价日，手工录入用 `observed_at` 的吉隆坡日期）；或此刻没有生效的版本（包括只有未来的预约） | `P2`：`USD quoted 2026-09-24 (6 calendar days)` 或 `USD no rate in effect`；OK：`USD quoted 2026-09-29 (1 calendar day)` |
| `usage_event_conflicts`（AIH-TASK-029） | 最近 24 小时（含恰好 24 小时前）记下了任何一条用量事件幂等冲突（`usage_event_conflicts` 的行；同一冲突请求重发不加行） | `P2`：`2 conflicts in 24 hours, earliest 2026-09-30T04:30:00Z`（条数与其中最早一条的 UTC 时刻）；OK：`no conflicts in 24 hours` |
| `usage_pricing_error`（AIH-TASK-032） | 存在 `PRICING_ERROR` 的事件：`P2` | `P2`：`3 events PRICING_ERROR, earliest received 2026-10-01T04:00:00Z, codes NO_PRICING_RULE, NO_PROVIDER_PRICE`；OK：`no PRICING_ERROR events` |
| `usage_fx_rate_error`（AIH-TASK-032） | 存在 `FX_RATE_ERROR` 的事件：`P2` | `P2`：`1 event FX_RATE_ERROR, earliest received …`；OK：`no FX_RATE_ERROR events` |
| `usage_model_unknown`（AIH-TASK-032） | 存在 `MODEL_UNKNOWN` 的事件：`P2` | `P2`：`2 events MODEL_UNKNOWN, earliest received …`；OK：`no MODEL_UNKNOWN events` |
| `usage_failed`（AIH-TASK-032） | 存在 `FAILED_FINAL` 的事件，或有 `FAILED_RETRYABLE` 的事件已尝试 ≥ 3 次：`P2`；其中有 `FAILED_FINAL` 带 `LEDGER_CONFLICT` 或 `AMOUNT_OUT_OF_RANGE`：`P1` | `1 FAILED_FINAL, 2 FAILED_RETRYABLE with 3+ attempts, earliest received …, codes LEDGER_CONFLICT, LOCK_WAIT_TIMEOUT`；OK：`no failed events` |
| `usage_processing_backlog`（AIH-TASK-032） | 最早的可处理事件（`RECEIVED`，或到期的 `FAILED_RETRYABLE`，且 `occurred_at ≤ now`）等待超过 5 分钟（§119 p99），或存在租约已过期仍未回收的 `PROCESSING`（回收任务本身停了）：`P2`；等待超过 30 分钟，或有 `PROCESSING` 过期超过 10 分钟：`P1`。等待的起点是事件变得可处理的时刻：`RECEIVED` 取接收与发生时刻中较晚的那个，`FAILED_RETRYABLE` 取 `next_attempt_at` | `3 due, oldest since 2026-10-01T04:20:00Z; 1 PROCESSING, earliest claimed 2026-10-01T04:29:50Z; 0 lease expired`；空库：`0 due; 0 PROCESSING; 0 lease expired` |
| `wallet_negative_balance`（AIH-TASK-032） | 存在余额 < 0 的钱包（§120）：`P2` | `P2`：`2 wallets below zero`（只有个数）；OK：`no wallet below zero` |
| `outbox_backlog`（AIH-TASK-032） | `domain_outbox` 里**有处理器**的事件类型（`app/tasks/outbox.py` 的处理器名单，与周期恢复同一份）存在创建超过 10 分钟仍 `PENDING` 的行（§120）：`P2`。没有处理器的类型（`tenant.billing_status_changed`、`tenant.low_balance`）按设计持久等待，不计入 | `P2`：`1 row pending over 10 minutes, oldest created …`；OK：`no handled outbox row pending over 10 minutes` |

---

## 集成：用量事件摄取

设计依据：设计闸门 #176（[AIH-TASK-029-usage-ingest.md](design/AIH-TASK-029-usage-ingest.md) v8 第 2 节）。
实现在 `app/api/integration_usage.py`、`app/services/usage_ingest.py`（单条处理是 `ingest_one`，批量端点复用它）
与 `app/core/nonce.py`。spec §20、§23、§37。

`POST /api/v1/integration/usage-events` 收一个用量事件。**只接受集成签名认证**（[集成请求签名](#集成请求签名)）：不读
`Authorization`，管理员会话在这里等于没带签名。`202` 的意思是事件已经**持久**落库（状态 `RECEIVED`），不是已经计费：
定价与扣费异步进行（REQ-INGEST-001）。本端点不计费、不入队；一次发多条用[批量端点](#批量摄取)。

### 请求头

[集成请求签名](#集成请求签名)的五个头，另加两条格式规则（不符一律 401）：

| 请求头 | 规则 |
| --- | --- |
| `X-Acuven-Request-Id` | `^[A-Za-z0-9._:-]{1,128}$`。⚠️ **每一次 HTTP 尝试都必须换一个新值，重试也要换**（时间戳本来就每次重新生成）：同一个值第二次出现就是重放，401 |
| `X-Acuven-Key-Version` | 十进制正整数，不补零 |
| `X-Acuven-Api-Key` | 1–64 个 `[A-Za-z0-9_]` 字符（签发的 key 是 `ak_` 加 32 个十六进制字符） |

签名覆盖**原始请求体字节**的 SHA-256：服务端先按原始字节验签、再解析 JSON。请求体发出去之后不要再改写
（重新序列化、加空格都会让签名失效）。

### 防重放

验签**通过之后**，服务端在 Redis 里写 `SET nonce:{api_key}:{X-Acuven-Request-Id} 1 NX PXAT (时间戳 + 300) × 1000`：
键已存在 → 401。时间戳出窗的请求在验签时就被拒，不查 Redis。Redis 不可用（连不上、200 ms 超时、没配置）时
**放行**并在服务端记一条 `WARNING`（`nonce_store_unavailable`）：财务上的安全网是 `event_id` 的全局唯一约束，
被重放的请求最多得到一次「合法重复」。

### 请求体

JSON 对象，**不接受未声明的字段**（422）。类型是严格的：JSON 的 `true` 不是整数，`1.0` 不是整数，数字不是字符串。
客户计算的费用、成本以及任何 prompt / 回复内容都不是字段，出现即 422（spec §11）。

| 字段 | 必填 | 规则 |
| --- | --- | --- |
| `schema_version` | 是 | 只接受 `"1.0"`；别的字符串 422 `UNSUPPORTED_SCHEMA_VERSION`，不是字符串 422 `VALIDATION_ERROR` |
| `event_id` | 是 | **UUIDv7 或 ULID 的规范写法**：UUIDv7 = 小写带连字符的 36 字符，`^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$`；ULID = 26 个大写 Crockford base32 字符，`^[0-7][0-9A-HJKMNP-TV-Z]{25}$`。大写的 UUID、小写的 ULID、别的版本的 UUID 都是 422。全局唯一，按字节比较 |
| `request_id` | 是 | AI 调用的 id，`^[A-Za-z0-9._:-]{1,128}$` |
| `conversation_id` | 否 | 同上；可为 `null` |
| `tenant_id` / `project_id` | 否 | 只作诊断：给出时必须等于凭据所属客户 / 项目的 `public_id`，否则 403。最长 64 个字符（更长 422） |
| `provider` | 是 | `^[a-z0-9][a-z0-9_-]{0,63}$` |
| `model` | 是 | `^[A-Za-z0-9][A-Za-z0-9._:/@-]{0,127}$`。目录里没有的模型照收，由计费侧标 `MODEL_UNKNOWN` |
| `usage_type` | 是 | 必须是某个计量类型的 `code`（[AI 目录](#ai-目录计量类型供应商模型与别名)，停用的照收）。不在目录里：409 `UNKNOWN_USAGE_TYPE`，`retryable = true` |
| `input_tokens` / `output_tokens` / `cache_creation_input_tokens` / `cache_read_input_tokens` | 按形态 | `LLM_TOKEN_FIELDS` 类型：四个都必填，JSON 整数，0 ≤ n ≤ 10^12；`QUANTITY` 类型：一个都不许出现（`null` 也不行） |
| `quantity` | 按形态 | `QUANTITY` 类型：必填，JSON **字符串**，`^[0-9]{1,12}(\.[0-9]{1,8})?$`；类型的数量是整数（`INTEGER`）时不许有小数部分（`"3.0"` 也拒绝）。`LLM_TOKEN_FIELDS` 类型：不许出现 |
| `unit` | 按形态 | `QUANTITY` 类型：必填，必须**逐字等于**该类型的 `unit`（区分大小写）。`LLM_TOKEN_FIELDS` 类型：不许出现 |
| `occurred_at` | 是 | RFC 3339，必须带时区（`Z` 或 `±hh:mm`），最多 6 位小数秒，换算成 UTC。晚于服务端当前时刻**超过** 300 秒：422 `OCCURRED_AT_IN_FUTURE`；没有过去的下限 |

**校验顺序**（决定一个请求报哪个码）：① 请求体大小 → ② 请求头、凭据与签名 → ③ nonce → ④ JSON 语法、顶层结构、
字段名与各字段的类型和格式 → ⑤ `schema_version` → ⑥ 查 `usage_type`（不在目录里即 409，不再往下）→ ⑦ 按该类型的
形态校验字段组、`unit`、整数数量 → ⑧ `occurred_at` 的未来上界 → ⑨ `tenant_id` / `project_id` 比对。未知的
`usage_type` 因此永远报 `UNKNOWN_USAGE_TYPE`，不会先因为字段组被判成 `VALIDATION_ERROR`。

### 响应

| 情形 | HTTP | `data` |
| --- | --- | --- |
| 新事件，已持久接收 | 202 | `{"event_id", "status": "accepted", "processing_status": "RECEIVED"}` |
| 同一事件再发，尚未处理完 | 200 | `{"event_id", "status": "already_received", "processing_status": <当前状态>}` |
| 同一事件再发，已处理 | 200 | `{"event_id", "status": "already_processed", "processing_status": "PROCESSED"}` |

「同一事件」= 同一个 `event_id`，归属（客户、项目、`api_key`）相同，指纹也相同。指纹按规范化之后的内容算：
`"37.42"` 与 `"37.420"`、`…Z` 与等价的 `+08:00` 写法是同一个事件；`tenant_id` / `project_id` 不参与。同一个
`api_key` 的新 `key_version`（轮换重叠期内）重发同一事件是合法重复，不是冲突。成功响应不带 `retryable`。

### 错误与 `retryable`

集成端点的**每一个**错误响应都在信封顶层多带一个布尔 `retryable`（包括框架自己的 404 / 405 / 422 与意外的 500）。
**集成方只看 `retryable` 决定是否重试，不按 HTTP 状态码推断**：同一个 409 里既有该重试的、也有不该重试的。
这是对[信封](#信封spec-107)的只增扩展，只用在 `/api/v1/integration/` 下，别的接口不变。

```json
{
  "success": false,
  "data": null,
  "error": { "code": "UNKNOWN_USAGE_TYPE", "message": "usage_type is not a known meter type." },
  "retryable": true,
  "request_id": "7d3c0b8e-..."
}
```

| HTTP | `error.code` | 什么时候 | `retryable` | 客户端该做什么 |
| --- | --- | --- | --- | --- |
| 401 | `INTEGRATION_AUTH_FAILED` | 请求头缺失或格式不对、凭据不存在 / 已吊销 / 已过期、签名错、时间戳出窗、Request-Id 重放 —— **一律同一个码**，原因只进服务端日志 | `false` | 停止重试该事件，进死信（保留）并告警；修好凭据、时钟或 Request-Id 生成后人工重投 |
| 403 | `CREDENTIAL_SCOPE_MISMATCH` | 载荷里的 `tenant_id` / `project_id` 不是凭据所属的（服务端另写一条审计） | `false` | 死信并告警 |
| 409 | `IDEMPOTENCY_CONFLICT` | 同一 `event_id`、归属或指纹不同。原事件不变；响应不透露原事件的任何信息 | `false` | 死信并告警 |
| 409 | `UNKNOWN_USAGE_TYPE` | `usage_type` 不在目录里 | `true` | 保留事件，退避重试；管理员建好类型后自然成功 |
| 413 | `PAYLOAD_TOO_LARGE` | 请求体超过 16 KiB（应用）或 1 MiB（nginx，见下）；批量端点见[批量摄取](#批量摄取) | `false` | 死信并告警 |
| 422 | `VALIDATION_ERROR` / `UNSUPPORTED_SCHEMA_VERSION` / `OCCURRED_AT_IN_FUTURE` | 见上表。`message` 只列字段名，不回显值 | `false` | 死信并告警 |
| 500 | `INTERNAL_ERROR` | 意外错误（含提交失败）；事件没有落库 | `true` | 退避重试 |
| 503 | `SERVICE_UNAVAILABLE` | 数据库不可用或未配置（主密钥未配置同样） | `true` | 退避重试 |

框架自己的 404 / 405（`HTTP_ERROR`）同样带 `retryable`：4xx 为 `false`。

**超大请求在 nginx 就被拒**：`/api/v1/integration/` 前缀的上限是 1 MiB，超过时 nginx 直接返回 413，正文是同一个
信封（`PAYLOAD_TOO_LARGE`、`retryable: false`），其中 **`request_id` 为 `null`** —— 请求没有到达应用，没有应用侧的
请求 id。1 MiB 以内、超过 16 KiB 的由应用返回同一个码，`request_id` 照常有值。

### 写了什么

- 新事件：一行 `usage_events`（`RECEIVED`），归属（客户、项目、凭据行）**只来自凭据**，载荷里的诊断 id 从不写入。
- 冲突：一行 `usage_event_conflicts`（同一 `event_id` + 请求方 `api_key` + 指纹只记一次）与一条审计
  `USAGE_EVENT_IDEMPOTENCY_CONFLICT`（`entity_type = usage_event`，`entity_id` = 原事件的 `public_id`；
  `after_state` 只有 `event_id`、请求方的 `api_key` 与 `key_version`、不符类型）。原事件一个字节都不改。
- 归属不符：只写一条审计 `USAGE_EVENT_SCOPE_MISMATCH`（`entity_type = integration_credential`，`entity_id` =
  `api_key`；`after_state` 只有 `key_version`、`event_id`、载荷给的两个 id 与凭据实际的两个 `public_id`），
  在独立事务里提交。
- 成功（202 / 200）之后，凭据的 `last_used_at` 在独立短事务里节流更新：每个凭据版本每 60 秒至多写一次。

### 批量摄取

设计依据：设计闸门 #180（[AIH-TASK-030-usage-ingest-batch.md](design/AIH-TASK-030-usage-ingest-batch.md) v3 第 2 节）。
spec §38、§39。

`POST /api/v1/integration/usage-events/batch` 一次收多条用量事件。请求层与单条端点相同、**整批只做一次**：
同样的五个签名头，签名覆盖**整个批的原始请求体字节**，整批一个 `X-Acuven-Request-Id`（同一个值再发一次是重放，401）。
之后按数组顺序逐条处理，每一条走的就是单条端点的那一套处理（校验顺序 ④–⑨、指纹、合法重复与冲突），
**结果与把它单独发给单条端点完全相同**（状态、`error_code`、`retryable`、写库效果）。

请求体：

```json
{"events": [ {…单条端点的事件对象…}, {…} ]}
```

- 顶层只有 `events` 一个字段（多了 422），`events` 是**非空**数组。
- 每个元素就是单条端点的请求体（字段规则见本节上面单条端点的「请求体」）。一个元素不合法只让**那一条** `rejected`，
  不让整批 422。
- 批内同一个 `event_id` 出现两次**不会预先去重**：按顺序处理，内容相同的第二条得 `already_received`，内容不同的
  得 `IDEMPOTENCY_CONFLICT`（与先后单独发两次相同）。
- 整批共用一个服务端时刻：签名时间窗、`occurred_at` 的 300 秒未来上界与每条的 `received_at` 口径一致。

上限：

| 上限 | 默认值 | 配置项 | 超出时 |
| --- | --- | --- | --- |
| 条数 | 100（spec §39） | `BILLING_INGEST_BATCH_MAX` | 整批 422 `BATCH_TOO_LARGE`，`retryable = false`，不写任何事件；拆小后重发 |
| 请求体 | 1 MiB（1048576 字节） | `BILLING_INGEST_BATCH_MAX_BYTES` | 整批 413 `PAYLOAD_TOO_LARGE`，`retryable = false`；拆小后重发 |
| 单个元素 | 16 KiB | 不可配（与单条端点相同） | 该条 `rejected` / `PAYLOAD_TOO_LARGE` / `retryable = false`，按元素**紧凑重新序列化**（无空白、UTF-8）后的长度判，其他条照常 |

请求体上限与 nginx 集成前缀块的 `client_max_body_size 1m` 相同，两层给的是同一个信封：nginx 拒的 `request_id` 为
`null`，应用拒的照常有值。两个配置项都必须是正数（请求体上限不小于 16 KiB），否则进程起不来。

**批层面的失败整体拒绝、不写任何事件**，是带 `retryable` 的错误信封（同[错误与 `retryable`](#错误与-retryable)）：

| HTTP | `error.code` | 什么时候 | `retryable` |
| --- | --- | --- | --- |
| 401 | `INTEGRATION_AUTH_FAILED` | 与单条端点相同（含整批的 Request-Id 重放） | `false` |
| 413 | `PAYLOAD_TOO_LARGE` | 请求体超过 `BILLING_INGEST_BATCH_MAX_BYTES` | `false` |
| 422 | `VALIDATION_ERROR` | 不是 JSON 对象、缺 `events`、多了顶层字段、`events` 不是数组或为空 | `false` |
| 422 | `BATCH_TOO_LARGE` | 条数超过 `BILLING_INGEST_BATCH_MAX` | `false` |
| 500 | `INTERNAL_ERROR` | 不属于单条错误表的意外错误；之前已提交的条保留，重发整批是安全的 | `true` |
| 503 | `SERVICE_UNAVAILABLE` | 认证时数据库不可用或未配置 | `true` |

否则一律 **200**，`data` 是汇总与按请求顺序排列的逐条结果：

```json
{
  "success": true,
  "data": {
    "accepted": 1,
    "duplicates": 1,
    "rejected": 1,
    "results": [
      {"index": 0, "event_id": "00000000-0000-7000-8000-000000000001", "status": "accepted", "processing_status": "RECEIVED", "error_code": null, "retryable": false},
      {"index": 1, "event_id": "00000000-0000-7000-8000-000000000002", "status": "already_received", "processing_status": "RECEIVED", "error_code": null, "retryable": false},
      {"index": 2, "event_id": "00000000-0000-7000-8000-000000000003", "status": "rejected", "processing_status": null, "error_code": "IDEMPOTENCY_CONFLICT", "retryable": false}
    ]
  },
  "error": null,
  "request_id": "7d3c0b8e-..."
}
```

| 字段 | 含义 |
| --- | --- |
| `accepted` / `duplicates` / `rejected` | 三者之和等于条数；`duplicates` 数 `already_received` 与 `already_processed` 两种 |
| `results[].index` | 该条在 `events` 里的位置（从 0 起） |
| `results[].event_id` | 该元素里的 `event_id` 字符串，原样返回（格式不合法的也照样返回）；元素不是对象、缺这个字段或它不是字符串时为 `null`，按 `index` 对应 |
| `results[].status` | `accepted`（新接收，行已提交）/ `already_received` / `already_processed`（合法重复，同单条端点）/ `rejected`（其余一切，包括可重试的） |
| `results[].processing_status` | 成功的条是事件的当前状态（同单条端点）；`rejected` 为 `null` |
| `results[].error_code` | `rejected` 的条是单条端点[错误表](#错误与-retryable)里的码（`VALIDATION_ERROR`、`UNSUPPORTED_SCHEMA_VERSION`、`OCCURRED_AT_IN_FUTURE`、`UNKNOWN_USAGE_TYPE`、`CREDENTIAL_SCOPE_MISMATCH`、`IDEMPOTENCY_CONFLICT`、`PAYLOAD_TOO_LARGE`、`SERVICE_UNAVAILABLE`）；成功为 `null` |
| `results[].retryable` | 取该码在单条端点错误表里的固定值（例如 `UNKNOWN_USAGE_TYPE` 与 `SERVICE_UNAVAILABLE` 为 `true`，`IDEMPOTENCY_CONFLICT` 为 `false`）；成功的条为 `false`（不需要重试） |

⚠️ **200 不代表每条都成功**：集成方只按每条的 `retryable` 决定该条是否重投（与单条端点同一规则），只重投需要的条。
冲突条不回显原事件的任何信息。

处理与写库：

- 每条**各自一个事务**，批本身没有外层事务：一条失败不回滚其他条，每条的「已接收」只在它自己的行提交之后成立。
- 处理某条时数据库不可用：该条 `SERVICE_UNAVAILABLE` / `retryable = true`，**后面的条照常尝试**（各自重连）；
  已提交的条不受影响。
- 写的东西与单条端点逐条相同（事件行、冲突行与审计、归属不符的审计）。
- 响应之后，凭据的 `last_used_at` 按单条端点的节流规则更新一次（批被处理即算一次使用，哪怕每条都 `rejected`）。
- 服务端日志只记条数与汇总（`Usage event batch processed`），不记请求体。
- 重发整批（新签名、新 Request-Id）是安全的：已接收的条得到 `already_received`。

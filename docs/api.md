# API 契约

> 本文件记录**已经实现**的 HTTP 接口的请求、响应与错误契约。接口改了，同一个 PR 里改这里。
> 设计依据写在每一节开头；设计与本文件不一致时以已批准的设计为准，并修正本文件。

目前收录：

- [管理端客户管理](#管理端客户管理)（AIH-TASK-006）
- [管理端手工调账](#管理端手工调账)（AIH-TASK-011）
- [管理端集成 API 凭据](#管理端集成-api-凭据)与[集成请求签名](#集成请求签名)（AIH-TASK-012）

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

六个接口都在 `/api/v1/admin` 下，都要 ADMIN。编辑客户是 AIH-TASK-009 加的，其余五个来自设计闸门 #96。

| 方法与路径 | 成功 | 错误 |
| --- | --- | --- |
| `POST /api/v1/admin/customers` | 201，客户详情 | 401 / 403 / 422 / 503 |
| `GET /api/v1/admin/customers` | 200，客户分页 | 401 / 403 / 422 / 503 |
| `GET /api/v1/admin/customers/{customer_id}` | 200，客户详情 | 401 / 403 / 404 / 503 |
| `PATCH /api/v1/admin/customers/{customer_id}` | 200，客户详情 | 401 / 403 / 404 / 422 / 503 |
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
| `status_version` | 计费状态每跃迁一次 +1。新客户是 0 |
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

- 客户：`billing_status = SUSPENDED`、`status_version = 0`；
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
- 其他字段一律 422，包括 `billing_status`、`status_version`、`public_id`、`id`、
  `low_balance_threshold` 和余额。计费状态只随余额变化；账户状态、低余额阈值、调账不走这个接口。
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

账户状态 `account_status`、低余额阈值配置、出站 webhook、删除或停用
客户与项目（财务记录永久保留）。管理员调账与 API 凭据见后面两节。

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

设计依据同上（设计 §2「校验库」；spec §37）。**本任务只提供校验库**（`app/services/integration_auth.py`），
还没有任何端点按它校验；摄取端点随 Phase 2 / 3 接上。下面两条是 spec §37 没写死、由设计补的定义，
**本节是它们的唯一出处**，Billing Client 与服务端都照这里实现。

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

以后的端点对以上所有拒绝给**同一个** 401，不让调用方区分原因；上面的码只在服务端内部使用。
防重放（记住用过的请求 id）随摄取端点实现，本任务不做。

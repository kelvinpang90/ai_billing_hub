# API 契约

> 本文件记录**已经实现**的 HTTP 接口的请求、响应与错误契约。接口改了，同一个 PR 里改这里。
> 设计依据写在每一节开头；设计与本文件不一致时以已批准的设计为准，并修正本文件。

目前收录：

- [管理端客户管理](#管理端客户管理)（AIH-TASK-006）

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

五个接口都在 `/api/v1/admin` 下，都要 ADMIN。

| 方法与路径 | 成功 | 错误 |
| --- | --- | --- |
| `POST /api/v1/admin/customers` | 201，客户详情 | 401 / 403 / 422 / 503 |
| `GET /api/v1/admin/customers` | 200，客户分页 | 401 / 403 / 422 / 503 |
| `GET /api/v1/admin/customers/{customer_id}` | 200，客户详情 | 401 / 403 / 404 / 503 |
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

编辑客户、账户状态 `account_status`、低余额阈值配置、管理员调账、API 凭据、出站 webhook、删除或停用
客户与项目（财务记录永久保留）。

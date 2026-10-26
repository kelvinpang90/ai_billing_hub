# API 契约

> 本文件记录**已经实现**的 HTTP 接口的请求、响应与错误契约。接口改了，同一个 PR 里改这里。
> 设计依据写在每一节开头；设计与本文件不一致时以已批准的设计为准，并修正本文件。

目前收录：

- [客户门户认证](#客户门户认证)（AIH-TASK-093）：接受邀请、当前用户、客户自己开启 2FA、客户端 `/customer/me`
- [客户钱包](#客户钱包)（AIH-TASK-099）：客户自己的余额与钱包流水，流水用序号游标分页
- [管理端客户管理](#管理端客户管理)（AIH-TASK-006）
- [管理端手工调账](#管理端手工调账)（AIH-TASK-011）
- [管理端钱包流水](#管理端钱包流水)（AIH-TASK-084）：序号游标分页，是下面「分页」的例外
- [管理端集成 API 凭据](#管理端集成-api-凭据)与[集成请求签名](#集成请求签名)（AIH-TASK-012）
- [管理端出站 webhook 签名密钥](#管理端出站-webhook-签名密钥)与[状态 webhook 签名](#状态-webhook-签名)（AIH-TASK-019）
- [管理端项目集成状态与状态 webhook 地址](#管理端项目集成状态与状态-webhook-地址)、[状态 webhook 的接收与对账契约](#状态-webhook-签名)与[集成：有效 AI 服务状态](#集成有效-ai-服务状态)的新字段（AIH-TASK-050）
- [管理端 AI 密钥](#管理端-ai-密钥)（AIH-TASK-059）
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

### 改密码使之前的全部会话失效

设计依据：设计闸门 #369 v6（`docs/design/AIH-TASK-086-session-revocation.md`）。

- 每个用户有一个密码版本号 `users.password_version`，从 0 开始，每次改密码在同一事务里加 1，只增不减。
- 签发令牌时把**验证密码时读到的那个版本号**写进去：访问令牌与 2FA 的 pending 令牌带整数 claim `pwv`，
  刷新令牌行带 `password_version`。刷新沿用旧行的版本号，二次验证沿用 pending 令牌的版本号。
- 使用令牌时拿它的版本号与库里当前值比较，不等就是无效：
  - 访问令牌调任何已登录接口：401 `TOKEN_INVALID`
  - pending 令牌调 `/api/v1/auth/login/totp`：401 `INVALID_CREDENTIALS`，不消耗 TOTP 或恢复码
  - pending 令牌调 `/api/v1/auth/2fa/enrol`、`/2fa/confirm`，访问令牌调 `/2fa/recovery-codes`：
    401 `TOKEN_INVALID`，什么都不写。这三个接口与 `/login/totp` 在自己的写事务里锁住用户行后再比较一次，
    所以鉴权之后、写入之前提交的改密码同样拦得住
  - 刷新令牌调 `/api/v1/auth/refresh`：401 `TOKEN_REUSED`
- 没有 `pwv` 的旧令牌按 0 处理：用户第一次改密码之前照常可用。
- 改密码之前已经通过鉴权的普通业务请求可以完成（与停用的语义相同）。
- ⚠️ **以后新增任何设置密码的入口，都必须在写新哈希的同一事务里、锁住用户行之后递增
  `password_version`**（设计 §10 A1）。漏掉的话那个入口改了密码却不加 1，旧会话照旧有效，
  而且不会让任何请求报错。`tests/backend/test_session_revocation.py` 的守卫用例扫描 `app/` 下
  每一处给 `.password_hash` 赋值的地方，要求同一函数里也给 `password_version` 赋值；
  用 `User(..., password_hash=...)` 建新用户的写法不受它管（新用户版本号是 0，没有会话可吊销）。

**刷新令牌被吊销与被重放是两回事**。对外都是 401 `TOKEN_REUSED`，客户端的处置相同（回到登录页），
服务端的处置不同：

| 刷新令牌行 | 判定 | 服务端副作用 |
| --- | --- | --- |
| 已用过（`used_at` 非空） | 重放或并发抢占，按被盗处理 | 吊销整条令牌链、写 `TOKEN_REUSED` 审计、记 `Refresh token reuse detected` warning（盗用告警挂在它上面） |
| 没用过、已吊销（改密码、登出、整条链被吊销） | 已失效 | 无：不写审计、不记 warning、不再吊销 |
| 没用过、没吊销、版本号与库里不等 | 已失效（改过密码） | 无，同上 |
| 没用过、没吊销、已过绝对期限或闲置期限 | 过期 | 无 |

所以正常改密码之后另一台设备的下一次刷新、登出之后同一令牌再来刷新，都不再触发盗用告警。

### 通用错误码

| HTTP | `error.code` | 什么时候 |
| --- | --- | --- |
| 401 | `TOKEN_INVALID` | 没有令牌、令牌格式不对、签名或有效期不对、拿 2FA 的 pending 令牌来调、账号不存在或已停用、令牌签发于最近一次改密码之前 |
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

`created_at` / `updated_at`（以及 `effective_at`）是不带时区的 UTC，ISO 8601，精确到秒，例如 `"2026-09-20T08:30:00"`
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

例外：[管理端钱包流水](#管理端钱包流水)与[客户钱包](#客户钱包)的流水接口用序号游标（`before_sequence`），没有 `page` 与 `total`，理由见那两节；[客户用量](#客户用量)的用量接口用不透明游标（`cursor`），同样没有 `page` 与 `total`。

---

## 客户门户认证

设计依据：设计闸门 #368 `APPROVED: design v12`，全文见 [design/AIH-TASK-089-customer-portal-auth.md](design/AIH-TASK-089-customer-portal-auth.md)（§2「API」「依赖」、§6；spec §51–§54、§90）。

客户（CUSTOMER）与管理员共用 `/api/v1/auth/login`、`/login/totp`、`/refresh`、`/logout` 与忘记密码。本节是客户门户
新增的五个接口；管理端的门户账号接口见[管理端门户账号](#管理端门户账号)。

| 方法与路径 | 主体 | 成功 | 错误 |
| --- | --- | --- | --- |
| `POST /api/v1/auth/invitation/accept` | 匿名，凭邀请令牌 | 200 `{}` | 400 `INVALID_INVITATION_TOKEN` / `WEAK_PASSWORD`；409 `ACCOUNT_CLOSED`；422；429 |
| `POST /api/v1/auth/2fa/self/enrol` | CUSTOMER 的访问令牌 + 重验密码 | 200 `{secret, otpauth_uri}` | 401 `INVALID_CREDENTIALS` / `TOKEN_INVALID`；403 `CUSTOMER_REQUIRED`；409 `ALREADY_ENROLLED`；422；429 |
| `POST /api/v1/auth/2fa/self/confirm` | CUSTOMER 的访问令牌 | 200 `{recovery_codes}` | 401 `INVALID_TOTP` / `TOKEN_INVALID`；403 `CUSTOMER_REQUIRED`；409 `ALREADY_ENROLLED` / `NOT_ENROLLED`；422 |
| `GET /api/v1/me` | 任一已登录用户 | 200 `{role, email, two_factor_enabled}` | 401 `TOKEN_INVALID` |
| `GET /api/v1/customer/me` | CUSTOMER 的访问令牌 | 200 `{customer_id, company_name, account_status, read_only}` | 401 `TOKEN_INVALID`；403 `CUSTOMER_REQUIRED` |

本节专有的错误码：

| HTTP | `error.code` | 什么时候 |
| --- | --- | --- |
| 400 | `INVALID_INVITATION_TOKEN` | 邀请令牌不存在、已用、已过期、用途不对，或账号不在 `INVITED`。**统一一个码、一句文案，不细分** |
| 400 | `WEAK_PASSWORD` | 密码强度不合格，与重置密码同一个错误 |
| 409 | `ACCOUNT_CLOSED` | 接受邀请时客户已关户 |
| 403 | `CUSTOMER_REQUIRED` | 令牌有效，但调用者在数据库里不是 CUSTOMER，或是一个不属于任何客户的 CUSTOMER |
| 403 | `ACCOUNT_READ_ONLY` | 客户写接口：客户的 `account_status` 是 `DISABLED` 或 `CLOSED`。本节没有写接口，以后的客户写接口沿用 |

### 客户请求的约定

- **客户请求不接受任何租户标识**（spec §90）。客户是谁、属于哪个客户，只取自访问令牌对应的、库里的 user 行；
  `/api/v1/customer/*` 的路径、查询串、请求体里都没有租户参数，带了也不读。带别的客户的 `customer_id` 来调，
  返回的仍是自己的。
- 每个 `/api/v1/customer/*` 处理函数的第一条语句是 `require_customer` 或 `require_customer_writable`。ADMIN 调
  `/api/v1/customer/*` 一律 403 `CUSTOMER_REQUIRED`；CUSTOMER 调 `/api/v1/admin/*` 一律 403 `ADMIN_REQUIRED`。
- 客户的四种 `account_status` 都能登录、都能读。`DISABLED` / `CLOSED` 时 `read_only` 为 `true`，客户写接口返回
  403 `ACCOUNT_READ_ONLY`；`PENDING_ACTIVATION` 与 `ENABLED` 一样可以完整使用。开启 2FA 在只读状态下仍然允许。
- ⚠️ **客户写操作必须在自己的写事务里、写入之前先调用 `lock_tenant_for_customer_write`**
  （`app/services/account_status.py`），由它锁客户行并重查 `account_status`。`require_customer_writable` 只是
  **提前拒绝**：它读完状态就结束了，之后管理员仍可能把客户停用。以后每个客户写接口的设计闸门都要引用这一条
  （设计 §2「依赖」）。

### `POST /api/v1/auth/invitation/accept` —— 接受邀请

邀请邮件里的链接是 `{frontend_base_url}/accept-invitation?token=…`，前端把令牌与客户设置的密码提交到这里。

```json
{ "token": "<邀请邮件里的令牌>", "password": "a-customer-perfectly-fine-passphrase" }
```

成功：

```json
{ "success": true, "data": {}, "error": null, "request_id": "00000000-0000-0000-0000-000000000000" }
```

- 账号从 `INVITED` 变为 `ACTIVE`，该账号其余未用的邀请令牌一并作废。**不自动登录**：没有访问令牌、不设刷新
  cookie，前端转登录页。
- 判定顺序：令牌与账号状态（400 `INVALID_INVITATION_TOKEN`）→ 客户是否关户（409 `ACCOUNT_CLOSED`）→ 密码强度
  （400 `WEAK_PASSWORD`）。错误码只取决于「链接还能不能用」与客户状态，不取决于密码的内容。弱密码不消耗令牌，
  换个密码可以用同一个链接重试。
- 同一个令牌提交第二次得到 400 `INVALID_INVITATION_TOKEN`，不覆盖第一次设置的密码。提交确认丢失后重试得到 400 时，
  前端提示「可能已设置，请直接登录」。
- 设置密码使之前的全部会话失效（见[上文](#改密码使之前的全部会话失效)）：接受之前为这个账号签发的令牌一律无效。
- 路径在 `/api/v1/auth/` 下：受 nginx 的认证限流与进程内令牌桶限流（429 `TOO_MANY_REQUESTS`）。

### `POST /api/v1/auth/2fa/self/enrol` 与 `/2fa/self/confirm` —— 客户自己开启 2FA

只给 CUSTOMER；ADMIN 在登录时强制注册（`/2fa/enrol`、`/2fa/confirm`，收 pending 令牌），调这两个接口 403
`CUSTOMER_REQUIRED`。两个接口都收 `Authorization: Bearer <访问令牌>`。

第一步重验密码：

```json
{ "password": "a-customer-perfectly-fine-passphrase" }
```

```json
{
  "success": true,
  "data": { "secret": "<base32 密钥>", "otpauth_uri": "otpauth://totp/..." },
  "error": null,
  "request_id": "00000000-0000-0000-0000-000000000000"
}
```

第二步提交验证器上的六位数：

```json
{ "code": "123456" }
```

```json
{
  "success": true,
  "data": { "recovery_codes": ["XXXX-XXXX-XXXX", "..."] },
  "error": null,
  "request_id": "00000000-0000-0000-0000-000000000000"
}
```

- 密钥与恢复码都**只返回这一次**，之后服务端不再回显。开启之后登录走 `TOTP_REQUIRED`。
- 密码错：401 `INVALID_CREDENTIALS`，不计入登录锁定、不写审计（调用方已持有有效的访问令牌）。验证码错：401
  `INVALID_TOTP`。已开启过：409 `ALREADY_ENROLLED`，不重发密钥。
- 两个接口都在 `/api/v1/auth/` 下，受认证限流；`/self/enrol` 第一步就过进程内令牌桶。前端对这个前缀下的 401
  不触发令牌刷新，所以密码错、验证码错的 401 直接展示给用户。
- 第一步之后账号被停用、改了密码或被重置 2FA，第二步得到 401 `TOKEN_INVALID`，什么都不写。
- 成功开启写 `TWO_FACTOR_ENABLED` 审计。

### `GET /api/v1/me` —— 当前用户

任一已登录用户（ADMIN 或 CUSTOMER），前端据 `role` 分流。

```json
{
  "success": true,
  "data": { "role": "CUSTOMER", "email": "portal@example.com", "two_factor_enabled": false },
  "error": null,
  "request_id": "00000000-0000-0000-0000-000000000000"
}
```

- 恰好这三个键：没有内部用户 id、`tenant_id`、哈希、锁定信息。`two_factor_enabled` 只算已确认的注册。
- 刻意**不**在 `/api/v1/auth/` 下：访问令牌过期时返回 401 `TOKEN_INVALID`，前端照常走刷新；也不进认证限流区，
  每次进页面调它不会撞上限流。

### `GET /api/v1/customer/me` —— 客户自己的摘要

```json
{
  "success": true,
  "data": {
    "customer_id": "00000000-0000-0000-0000-000000000000",
    "company_name": "Example Sdn Bhd",
    "account_status": "ENABLED",
    "read_only": false
  },
  "error": null,
  "request_id": "00000000-0000-0000-0000-000000000000"
}
```

- `customer_id` 是客户的 `public_id`。恰好这四个键：没有任何金额、成本、毛利、内部 id、`billing_mode`（INV-7）。
- `read_only` 在 `account_status` 为 `DISABLED` 或 `CLOSED` 时为 `true`。

---

## 客户钱包

设计依据：设计闸门 #393 `APPROVED: design v3`，全文见 [design/AIH-TASK-099-customer-wallet.md](design/AIH-TASK-099-customer-wallet.md)（§2、§6、§9；spec §67、§70、§90）。实现登记为 AIH-TASK-099。钱包摘要的 `low_balance_threshold` 与 `low_balance` 两个键依据设计闸门 #433 `APPROVED: design v2`（[design/AIH-TASK-109-low-balance-threshold.md](design/AIH-TASK-109-low-balance-threshold.md) §2「接口三」；spec §48、§67），实现登记为 AIH-TASK-110。

| 方法与路径 | 主体 | 成功 | 错误 |
| --- | --- | --- | --- |
| `GET /api/v1/customer/wallet` | CUSTOMER 的访问令牌 | 200，钱包摘要（8 个键） | 401 / 403 / 500 / 503 |
| `GET /api/v1/customer/wallet/transactions` | CUSTOMER 的访问令牌 | 200，一页账本行（每项 7 个键） | 401 / 403 / 422 / 500 / 503 |

- **客户请求不接受任何租户标识**（spec §90，见上文[客户请求的约定](#客户请求的约定)）：租户只取自访问令牌对应的、库里的 user 行。两个接口都不定义租户、客户、钱包标识参数；带了 `customer_id`、`tenant_id` 也只会被当作未知参数忽略，返回的仍是自己的钱包。
- **两个接口在只读状态下照常可读**：`account_status` 为 `PENDING_ACTIVATION`、`ENABLED`、`DISABLED`、`CLOSED` 时都返回 200（停用、关户后仍能看账）。它们用 `require_customer`，不用 `require_customer_writable`，所以没有 403 `ACCOUNT_READ_ONLY`。
- 只读：不写账本、不动余额、不改任何状态、不写审计，不加锁。
- ADMIN 不能调；管理员看客户的流水用[管理端钱包流水](#管理端钱包流水)。

### `GET /api/v1/customer/wallet` —— 客户自己的钱包

没有查询参数（未知参数忽略）。

```json
{
  "success": true,
  "data": {
    "currency": "MYR",
    "balance": "12.45880000",
    "billing_mode": "PREPAID",
    "billing_status": "ACTIVE",
    "ai_service": "ALLOW_AI",
    "ai_service_reason": null,
    "low_balance_threshold": "50.00000000",
    "low_balance": true
  },
  "error": null,
  "request_id": "00000000-0000-0000-0000-000000000000"
}
```

| 字段 | 说明 |
| --- | --- |
| `currency` | 钱包币种，V1 恒为 `MYR` |
| `balance` | 恰好 8 位小数的有符号字符串，原样来自 `wallets.balance`，可为负 |
| `billing_mode` | `PREPAID` / `INTERNAL_METERED_ONLY` |
| `billing_status` | `ACTIVE`（余额 > 0）/ `SUSPENDED`（余额 ≤ 0，恰好为 0 也是 `SUSPENDED`） |
| `ai_service` | `ALLOW_AI` / `BLOCK_AI`：AI 服务现在是否可用。与集成方的[有效 AI 服务状态](#集成有效-ai-服务状态)同一公式，在客户级算（项目维度按 `ENABLED` 代入） |
| `ai_service_reason` | `ai_service` 为 `BLOCK_AI` 时是 `ACCOUNT_CLOSED` / `ACCOUNT_DISABLED` / `ACCOUNT_PENDING_ACTIVATION` / `BALANCE_NOT_POSITIVE` 之一，`ALLOW_AI` 时为 `null`。管理员动作排在余额之前：被停用且余额为负时是 `ACCOUNT_DISABLED`。不会出现 `PROJECT_INTEGRATION_DISABLED`：单个项目被停用不在这里体现 |
| `low_balance_threshold` | 管理员给这个客户设的低余额提醒线：恰好 8 位小数的字符串，原样来自 `tenants.low_balance_threshold`；未设置（不提醒）为 `null` |
| `low_balance` | 布尔：`billing_mode` 为 `PREPAID`、阈值非空、且 `balance ≤ low_balance_threshold` 时为 `true`，否则 `false` |

- 恰好这 8 个键：没有钱包 `version`、`status_version`、任何内部 id。账户状态与只读标志见 `/customer/me`，这里不重复。
- `INTERNAL_METERED_ONLY` 的客户余额通常为 0、`billing_status` 为 `SUSPENDED`，但 `ai_service` 照常是 `ALLOW_AI`：判断 AI 能不能用看 `ai_service`，不要只看 `billing_status`。
- 余额、三个状态与阈值出自同一条查询，不会读到「余额已变、状态未变」的组合。
- `low_balance` 的判定在后端用十进制精确比较算出（不经浮点、不在 SQL 里算），前端只看这个布尔，不自己比较金额：
  - **只有 `PREPAID` 才算**：`INTERNAL_METERED_ONLY` 恒为 `false`（阈值照常返回），与[低余额事件](#低余额事件)只对预付客户同一口径。
  - **等号算低余额**：余额恰好等于阈值时为 `true`（spec §48「at-or-below」）；高出最小单位（`0.00000001`）就是 `false`。
  - 余额 ≤ 0（`SUSPENDED`）且设了阈值时也是 `true`。
  - 按当前余额实时判断：管理员把阈值调到当前余额之上时这里立刻是 `true`，但不会补发低余额事件。
- 阈值只能由管理员设置或清除（见[管理端低余额阈值](#管理端低余额阈值)）；客户只读，没有修改入口。

### `GET /api/v1/customer/wallet/transactions` —— 客户自己的钱包流水

| 参数 | 规则 |
| --- | --- |
| `page_size` | 1–100，默认 20；超出 422 |
| `before_sequence` | 可选，整数 1 – 9 223 372 036 854 775 807；只返回 `wallet_sequence < before_sequence` 的行。省略 = 从最新一行开始。超出 422 |

不接受 `reference_type` 或任何其他筛选；未知参数忽略。参数由框架在鉴权**之前**校验，所以没带令牌、参数又不合法的请求拿到 422；文案只列字段名、不回显值。

```json
{
  "success": true,
  "data": {
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
  },
  "error": null,
  "request_id": "00000000-0000-0000-0000-000000000000"
}
```

（示例只列了一页 20 行里的第一行；这一页最后一行的序号是 23。）

| 字段 | 说明 |
| --- | --- |
| `items` | 最新在前，按 `wallet_sequence` 倒序 |
| `id` | 账本行的 `public_id` |
| `wallet_sequence` | 这一笔在钱包账本里的序号，从 1 起、连续递增，也是翻页游标 |
| `transaction_type` | 九种类型之一（`TOPUP`、`AI_USAGE`、`ADJUSTMENT_CREDIT`、`ADJUSTMENT_DEBIT`、`REBILL_CREDIT`、`REBILL_DEBIT`、`REFUND_ADJUSTMENT`、`BONUS`、`SYSTEM_CORRECTION`），原值。前端按它显示固定文案 |
| `amount` / `balance_after` | 恰好 8 位小数的有符号字符串，原样来自 `DECIMAL(20,8)` 列，不经浮点、不舍入 |
| `event_id` | 只有 AI 用量扣费行有：接入方上报的 `event_id`（或 AI 模块代为生成的 ULID），用于和自己的系统对账。其余行一律 `null` |
| `created_at` | 记账时刻，UTC，不带时区 |
| `page_size` | 本次请求的 `page_size` |
| `next_before_sequence` | 还有更早的行时 = 本页**最后一行**的 `wallet_sequence`，原样作为下一页的 `before_sequence`；没有更多时 `null` |

- 每项恰好上面 7 个键。与[管理端钱包流水](#管理端钱包流水)相比**刻意不返回**：`description`（管理员写的调账原因，可能含内部备注）、`reference_type`、调账的 `reference_id`（管理端幂等键）、`created_by_email`、`balance_before`、`usage_event_id`、`metadata_json`、任何成本 / 汇率 / 参考价 / 毛利与内部 id（INV-7）。
- 翻页规则与管理端钱包流水相同：第一页不带 `before_sequence`；下一页把 `next_before_sequence` 原样传回；为 `null` 就是最后一页；「回到最新」= 不带 `before_sequence` 重取。翻页之间新记的账只出现在最新那一端，往旧翻不重复、不遗漏。拿别的客户的序号当游标，也只会翻到自己的行。
- 余额卡与流水是两次请求，中间可能有新记账：页面上的余额可能比流水第一行的 `balance_after` 更新，这是预期行为。

**为什么没有 `total`**（设计 §2、§9，与管理端钱包流水同一理由）：每个 AI 请求一行账本，大客户的账本是 spec §108 说的大表。`total` 每次要数全部行，`OFFSET` 要先扫过前面所有行，新行插入时按页码翻还会重复上一页的末尾几行；序号游标在任意深度都是索引上的范围扫描。客户不需要总行数，所以本接口不返回它。

### 错误（两个接口相同）

| HTTP | `error.code` | 什么时候 |
| --- | --- | --- |
| 401 | `TOKEN_INVALID` | 没有令牌、令牌无效或过期（前端照常走刷新）；门户账号已停用或不是 `ACTIVE`（停用后的旧令牌下一个请求即失效） |
| 403 | `CUSTOMER_REQUIRED` | 令牌有效，但调用者是 ADMIN，或是一个不属于任何客户的 CUSTOMER |
| 422 | `VALIDATION_ERROR` | 仅流水接口：查询参数超出上面的规则 |
| 500 | `INTERNAL_ERROR` | 意外错误，带 `request_id`；包括客户缺钱包这种数据不一致（流水接口也是 500，不返回空页） |
| 503 | `DATABASE_UNAVAILABLE` 等 | 数据库不可用，沿用通用处理 |

没有 404（客户来自会话，不存在「找不到客户」），也没有 403 `ACCOUNT_READ_ONLY`（两个接口都是读）。

---

## 客户用量

设计依据：设计闸门 #398 `APPROVED: design v4`，全文见 [design/AIH-TASK-103-customer-usage.md](design/AIH-TASK-103-customer-usage.md)（§2、§6；spec §68、§69、§90、§108、§109）。实现登记为 AIH-TASK-104。

| 方法与路径 | 主体 | 成功 | 错误 |
| --- | --- | --- | --- |
| `GET /api/v1/customer/projects` | CUSTOMER 的访问令牌 | 200，自己的全部项目（每项 2 个键） | 401 / 403 / 500 / 503 |
| `GET /api/v1/customer/usage-events` | CUSTOMER 的访问令牌 | 200，一页已计费的用量事件（每项 18 个键） | 401 / 403 / 422 / 500 / 503 |
| `GET /api/v1/customer/usage-summary` | CUSTOMER 的访问令牌 | 200，一个马来西亚日历月的计费额、请求数、对话数与按日用量（每天各一项，见下文） | 401 / 403 / 422 / 500 / 503 |

- **客户请求不接受任何租户标识**（spec §90，见上文[客户请求的约定](#客户请求的约定)）：租户只取自访问令牌对应的、库里的 user 行。这些接口都不定义租户、客户标识参数；带了 `customer_id`、`tenant_id` 也只会被当作未知参数忽略。
- **这些接口在只读状态下照常可读**：四种 `account_status` 都返回 200。它们用 `require_customer`，没有 403 `ACCOUNT_READ_ONLY`。
- 只读：不写任何表、不加锁、不写审计。ADMIN 不能调；管理员看用量用[管理端用量事件：查询](#管理端用量事件查询)。

### `GET /api/v1/customer/projects` —— 客户自己的项目

没有查询参数（未知参数忽略）。返回该客户的**全部**项目，按创建先后，不分页（项目只由管理员创建，数量很少）。

```json
{
  "success": true,
  "data": {
    "items": [
      { "id": "00000000-0000-4000-8000-000000000001", "name": "Customer service bot" }
    ]
  },
  "error": null,
  "request_id": "00000000-0000-0000-0000-000000000000"
}
```

- 每项恰好 `id`（项目 `public_id`，可作用量接口的 `project_id` 筛选）与 `name` 两个键。**不返回** `description`、`integration_status`、`status_webhook_url`（可能带集成方令牌）、凭据与任何内部 id。

### `GET /api/v1/customer/usage-events` —— 客户自己的已计费用量

只列**已计费**（`PROCESSED`）的事件：还在排队、计费中或停在错误状态的事件没有计费额，由管理端处理，这里看不到。每一行都有确定的计费额，与钱包流水对得上。

| 参数 | 规则 |
| --- | --- |
| `from_date` / `to_date` | `YYYY-MM-DD`，**马来西亚日历日**，含两端；每个都必须在 `2020-01-01` 到 `2099-12-31` 之间。没有 `event_id` 时**两个都必填**；`to_date` 不得早于 `from_date`；区间最多 92 天（`to_date − from_date ≤ 91`）。换算见下文「营业时区」 |
| `project_id` | 可选，项目 `public_id`（来自上面的项目列表）。不是自己的项目（不存在或属于别的客户）时得到空页，不报错 |
| `provider` / `model` | 可选，与上报的原始字符串逐字节比较；格式同[用量事件摄取](#集成用量事件摄取)的 `provider`、`model` |
| `conversation_id` | 可选，逐字节比较；格式 `^[A-Za-z0-9._:-]{1,128}$` |
| `event_id` | 可选，逐字节比较；格式同用量摄取的 `event_id`（UUIDv7 或 ULID 规范写法）。给出时日期可省略（给了仍照常生效、照常校验）：用于从钱包流水的 `event_id` 跳过来查那一条 |
| `page_size` | 1–100，默认 20 |
| `cursor` | 可选，上一页响应的 `next_cursor` **原样**传回；省略 = 从最新开始 |

- 各筛选之间是 AND。未知参数忽略。
- 校验分两步，所有 422 都是 `VALIDATION_ERROR`，文案只列字段名（如 `Invalid request fields: query.from_date, query.to_date`），不回显收到的值：
  - 类型、范围与格式（`page_size`、日期的 `YYYY-MM-DD` 形状、`provider`、`model`、`conversation_id`、`event_id`）由框架在鉴权**之前**判；
  - 日期是否真实存在、是否在 2020-01-01 到 2099-12-31 之间、必填、先后、92 天与游标在鉴权**之后**判。日期范围在任何日期运算之前判，所以 `0001-01-01`、`9999-12-31` 这类极值得到 422。

```json
{
  "success": true,
  "data": {
    "items": [
      {
        "id": "00000000-0000-4000-8000-000000000002",
        "event_id": "00000000-0000-7000-8000-000000000003",
        "request_id": "req_0001",
        "conversation_id": "conv_0001",
        "feature": "crm.contact_summary",
        "project_id": "00000000-0000-4000-8000-000000000001",
        "project_name": "Customer service bot",
        "provider": "anthropic",
        "model": "claude-sonnet-5-5",
        "usage_type": "LLM_TOKEN",
        "input_tokens": 1200,
        "output_tokens": 340,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "quantity": null,
        "unit": "TOKEN",
        "charged_amount": "0.04120000",
        "occurred_at": "2026-10-18T08:30:00.123456"
      }
    ],
    "page_size": 20,
    "next_cursor": "MjAyNi0xMC0xOFQwODozMDowMC4xMjM0NTZ8MDAwMDAwMDAtMDAwMC00MDAwLTgwMDAtMDAwMDAwMDAwMDAy"
  },
  "error": null,
  "request_id": "00000000-0000-0000-0000-000000000000"
}
```

（示例只列了一页里的第一行。）

| 字段 | 说明 |
| --- | --- |
| `items` | 最新在前，按 `occurred_at` 倒序；同一时刻的按 `id` 倒序 |
| `id` | 用量事件的 `public_id` |
| `event_id` / `request_id` / `conversation_id` / `feature` | 集成方上报的原值。AI 模块代为上报的事件 `conversation_id` 为 `null`、`feature` 为功能标签 |
| `project_id` / `project_name` | 该事件所属项目的 `public_id` 与名称 |
| `provider` / `model` | 上报的原始字符串（筛选用的也是它） |
| `usage_type` | 计量类型的 `code` |
| `input_tokens` / `output_tokens` / `cache_creation_input_tokens` / `cache_read_input_tokens` / `quantity` / `unit` | 上报的用量原值。token 形态的四个 token 字段非空、`quantity` 为 `null`；数量形态相反。`quantity` 是恰好 8 位小数的字符串 |
| `charged_amount` | 本次请求向客户收的金额（`billable_cost`），恰好 8 位小数的字符串，**含税**，不经浮点、不舍入。预付费客户的它等于钱包流水里同一 `event_id` 那一行 `amount` 的绝对值；为 0 的事件不记账，钱包流水里没有它；内部计量客户恒为 `"0.00000000"` |
| `occurred_at` | 调用发生时刻，不带时区的 UTC，**保留微秒**（[时间](#时间)约定的例外，与管理端用量事件一致） |
| `page_size` | 本次请求的 `page_size` |
| `next_cursor` | 还有更旧的行时是一个不透明字符串，否则 `null` |

- 每项恰好上面 18 个键。与[管理端用量事件](#管理端用量事件查询)相比**刻意不返回**：`status`（恒为 `PROCESSED`）、`error_code`、`error_message`、`received_at`、`processed_at`、`schema_version`、`payload_fingerprint`、`provider_id` / `model_id`、`estimated_provider_cost_myr`、`provider_source_currency`、`provider_source_cost`、`fx_rate_applied`、`reference_customer_price`、`billing_mode_snapshot`、价格版本 / 定价规则 / 汇率版本引用、账本行、凭据、认领字段、冲突记录（含 `api_key`）、毛利与任何内部 id（INV-7）。
- 拿别的客户的项目 id、`event_id`、`conversation_id` 或游标来调，只会得到自己的行或空页（INV-8）。

**营业时区**：客户侧的日期一律按马来西亚时间（`Asia/Kuala_Lumpur`，固定 UTC+8，没有夏令时）理解。`from_date`..`to_date` 换成 UTC 的 `[from_date 00:00 − 8 小时, (to_date + 1 天) 00:00 − 8 小时)`，按 `occurred_at` 取。例：`from_date = to_date = 2026-10-01` → `[2026-09-30 16:00:00, 2026-10-01 16:00:00)` UTC。响应里的时间仍是 UTC，前端显示时 +8 小时。晚到或晚计费的事件出现在它**发生**的那一天，所以同一个查询隔一段时间再看可能多出旧日期的行。

**翻页**：第一页不带 `cursor`；下一页把 `next_cursor` 原样传回；为 `null` 就是最后一页；「回到最新」= 不带 `cursor` 重取。⚠️ **游标是不透明的**：客户端不得解析、拼接或修改它，后端以后可以改它的编码；被改过或解不开的游标得到 422 `VALIDATION_ERROR`（`query.cursor`）。翻页之间新计费的事件不会让已返回的行重复出现：比游标新的只出现在最新那一端，比游标旧的（晚计费）出现在后面的页里。

**`QUERY_TOO_BROAD`**：每次查询在数据库里最多执行 2 秒。按很少出现（甚至不存在）的供应商、模型筛一个很长的区间时，数据库要扫过区间内的全部事件才知道凑不满一页，超过预算就中止并返回 422 `QUERY_TOO_BROAD`，没有部分结果。这不是故障：缩小日期区间，或换（加）一个更具体的条件（项目、`conversation_id`、`event_id`）后重试。服务端为每次超时记一条 `customer_usage_query_timeout` 日志（只带 `request_id` 与客户 id，不带查询参数的值）。

### 错误（两个接口相同，项目列表没有 422）

| HTTP | `error.code` | 什么时候 |
| --- | --- | --- |
| 401 | `TOKEN_INVALID` | 没有令牌、令牌无效或过期（前端照常走刷新）；门户账号已停用或不是 `ACTIVE`（停用后的旧令牌下一个请求即失效） |
| 403 | `CUSTOMER_REQUIRED` | 令牌有效，但调用者是 ADMIN，或是一个不属于任何客户的 CUSTOMER |
| 422 | `VALIDATION_ERROR` | 仅用量接口：查询参数违反上面的规则 |
| 422 | `QUERY_TOO_BROAD` | 仅用量接口：查询超过 2 秒执行预算；缩小日期区间或换筛选条件后重试 |
| 500 | `INTERNAL_ERROR` | 意外错误，带 `request_id` |
| 503 | `DATABASE_UNAVAILABLE` 等 | 数据库不可用，沿用通用处理 |

没有 404（客户来自会话；项目与事件筛不到就是空页），也没有 403 `ACCOUNT_READ_ONLY`（两个接口都是读）。

### `GET /api/v1/customer/usage-summary` —— 客户自己某月的用量汇总

设计依据：设计闸门 #403 `APPROVED: design v1`，全文见 [design/AIH-TASK-107-customer-usage-summary.md](design/AIH-TASK-107-customer-usage-summary.md)（§2、§6；spec §67、§87、§90、§109、§111）。实现登记为 AIH-TASK-107。

口径与上面的用量接口相同：只算**已计费**（`PROCESSED`）的事件，按 `occurred_at` 的马来西亚日历日归属。读时在用量事件上实时汇总，不建聚合表、不缓存。

| 参数 | 规则 |
| --- | --- |
| `month` | 可选，`YYYY-MM`，**马来西亚日历月**；省略 = 请求时刻所在的马来西亚日历月（服务端时刻 UTC 加 8 小时后的年月，同一请求只取一次）。必须在 `2020-01` 到 `2099-12` 之间（含两端）。未来的月份照常 200，全为 0 |

- 未知参数忽略；不接受任何租户、客户标识。
- 校验分两步，所有 422 都是 `VALIDATION_ERROR`，文案只列字段名（`Invalid request fields: query.month`），不回显收到的值：`YYYY-MM` 的形状（如 `2026-1`、`abc`）由框架在鉴权**之前**判；月份是否在 01–12、是否在 `2020-01` 到 `2099-12` 之间（如 `2026-13`、`2019-12`、`2100-01`）在鉴权**之后**、任何日期运算之前判。

```json
{
  "success": true,
  "data": {
    "month": "2026-10",
    "charged_amount": "12.34567800",
    "request_count": 1234,
    "conversation_count": 56,
    "days": [
      { "date": "2026-10-01", "request_count": 10, "charged_amount": "0.41200000" },
      { "date": "2026-10-02", "request_count": 0, "charged_amount": "0.00000000" }
    ]
  },
  "error": null,
  "request_id": "00000000-0000-0000-0000-000000000000"
}
```

（示例只列了 `days` 的前两项；实际是该月的每一天。）

| 字段 | 说明 |
| --- | --- |
| `month` | 实际统计的月份（省略参数时就是当月），`YYYY-MM` |
| `charged_amount` | 该月已计费事件的 `billable_cost` 之和，恰好 8 位小数的字符串，**含税**，不经浮点、不舍入；等于 `days` 里各日 `charged_amount` 之和。内部计量客户恒为 `"0.00000000"` |
| `request_count` | 该月已计费事件的条数（一次供应商调用一条）；等于各日 `request_count` 之和 |
| `conversation_count` | 该月已计费事件中**不同的非空** `conversation_id` 的个数。同一个对话跨月时，在它有事件的每个月各计一次；没有 `conversation_id` 的事件（例如 AI 模块代为上报的调用）不计入。不是各日去重数之和 |
| `days` | 该月的**每一天**各一项（28–31 项），按日期升序；每项恰好 `date`（马来西亚日历日，`YYYY-MM-DD`）、`request_count`、`charged_amount` 三个键。没有事件的日子 `request_count = 0`、`charged_amount = "0.00000000"` |

- `data` 恰好上面 5 个键。**刻意不返回**：成本、原币成本、汇率、参考价、毛利、token 合计、按项目 / 供应商 / 模型的拆分与任何内部 id（INV-7）。`conversation_id` 只参与计数，不返回。
- **月份区间**：`[当月 1 日 00:00 − 8 小时, 次月 1 日 00:00 − 8 小时)` UTC，按 `occurred_at` 取。例：`month = 2026-10` → `[2026-09-30 16:00:00, 2026-10-31 16:00:00)` UTC。与用量接口按同一个月的首末日查到的条数与计费额之和一致。
- **按发生时间归月，不按记账时间**：月末发生、次月才计费的事件计入它发生的月；钱包流水按记账时间。所以本月 `charged_amount` 与本月钱包流水里 `AI_USAGE` 的合计可能差在跨月的几笔，两者不要并列比较。
- **数字会变**：晚到或晚计费的事件计入它**发生**的那一天，同一个月的数字会随时间变大；这不是对账单。
- 两条汇总查询在同一个只读事务里执行，合计、按日与对话数来自同一组行。

**`QUERY_TOO_BROAD`**：每条汇总查询在数据库里最多执行 2 秒（与用量接口同一预算）。该月事件太多、超过预算时中止并返回 422 `QUERY_TOO_BROAD`，没有部分结果；可以在用量接口按较短区间查看。服务端记一条 `customer_usage_query_timeout` 日志（只带 `request_id` 与客户 id，不带参数值）。

| HTTP | `error.code` | 什么时候 |
| --- | --- | --- |
| 401 | `TOKEN_INVALID` | 没有令牌、令牌无效或过期；门户账号已停用或不是 `ACTIVE` |
| 403 | `CUSTOMER_REQUIRED` | 令牌有效，但调用者是 ADMIN，或是一个不属于任何客户的 CUSTOMER |
| 422 | `VALIDATION_ERROR` | `month` 格式错或越界 |
| 422 | `QUERY_TOO_BROAD` | 任一条汇总查询超过 2 秒执行预算 |
| 500 | `INTERNAL_ERROR` | 意外错误，带 `request_id` |
| 503 | `DATABASE_UNAVAILABLE` 等 | 数据库不可用，沿用通用处理 |

没有 404，也没有 403 `ACCOUNT_READ_ONLY`（读接口，只读状态下照常可读）。

---

## 客户导出

设计依据：设计闸门 #435 `APPROVED: design v10`，全文见 [design/AIH-TASK-113-customer-export.md](design/AIH-TASK-113-customer-export.md)（§2、§5、§6；spec §73、§80、§109、§112）。CSV 模块与导出租约登记为 AIH-TASK-114，用量导出接口登记为 AIH-TASK-115。

| 方法与路径 | 主体 | 成功 | 错误 |
| --- | --- | --- | --- |
| `GET /api/v1/customer/usage-events/export` | CUSTOMER 的访问令牌 | 200，一个 CSV 附件：区间内全部已计费用量事件，每行一条、18 列 | 401 / 403 / 422 / 500 / 503 |

- 与其他客户接口一样：租户只取自访问令牌对应的、库里的 user 行，不接受任何租户、客户标识；四种 `account_status` 都能导出（导出是读）；ADMIN 不能调。
- 同步生成、不落盘：请求时查询一次，在内存里拼好整份文件后再开始发送。服务器不保存任何导出文件，没有导出任务、没有异步。只读：不写任何表、不加锁、不写审计。
- 只做 CSV；`.xlsx`、会话摘要导出、月度用量汇总导出不在这里。

### CSV 格式

- **编码**：UTF-8，文件开头带 BOM（`EF BB BF`），Excel 才能正确识别非 ASCII 的项目名。
- **行**：RFC 4180。第一行是表头，之后每行一条记录；行尾 `\r\n`；含逗号、双引号或换行的字段加双引号，值里的双引号写成两个双引号。
- **空值**：JSON 接口里为 `null` 的值写成空字段。
- **整数**原样（十进制）。**金额与 `quantity`** 是恰好 8 位小数的字符串，与 JSON 接口逐字相同，**不按页面的 2 / 6 位舍入**、不经浮点、不加千分位、不带币种（币种恒为 MYR）：导出是对账用的数据，舍入后加总会与账本差几分。
- **时间**一律是不带时区的 UTC，保留微秒，列名带 `_utc`，免得在表格软件里被当成本地时间。
- **公式注入防护**：文本列的值若以 `=`、`+`、`-`、`@`、制表符或回车开头，前面加一个单引号 `'`（例：`=1+1` 写成 `'=1+1`）。这些值有的来自集成方上报或管理员，用 Excel 打开时以这些字符开头的单元格会被当成公式执行。数值列（token、`quantity`、`charged_amount`）与时间列不加：负金额必须保持是数。哪些是文本列见下文的列表。
- **响应头**：`Content-Type: text/csv; charset=utf-8`、`Content-Disposition: attachment; filename="<文件名>"`、`Cache-Control: no-store`（文件不留在浏览器缓存里）、`X-Accel-Buffering: no`（见下文「同一时刻只有一个导出」）。文件名只由校验过的日期拼出，只含 ASCII 字母、数字、`-`、`_`、`.`。
- **错误响应不是 CSV**：仍是 JSON 信封（`{success:false, error, request_id}`），HTTP 状态码照常。整份文件拼好才开始发送，所以不会出现「已经收到 200 和半个文件、之后才失败」。下载前端用 `responseType: "blob"` 时，失败的 blob 按 JSON 信封解析。

### 行数上限与 `EXPORT_TOO_LARGE`

一个文件最多 **20 000 行**。查询多取一行判断是否超出；超出时 422 `EXPORT_TOO_LARGE`，**不返回任何行**（没有部分文件），文案固定（超过 20 000 行，请缩短日期区间或加筛选条件），不回显参数值、不报实际行数。超过上限也不转异步：缩短区间或加筛选条件后重试。

### 同一时刻只有一个导出与 `EXPORT_BUSY`

每个 API 进程同一时刻**最多一个导出在生成或在下载**（客户导出共用这一个名额，所有客户之间也共用）。从发查询之前起，到整个响应交给服务器之后才释放；第二个导出立刻得到 503 `EXPORT_BUSY`，带 `Retry-After: 5`，不排队、不等待、也不发任何查询；几秒后重试即可。

导出响应带 `X-Accel-Buffering: no`，反向代理对它不缓冲（不把文件写进代理的临时文件），边读边转发给客户端。所以名额**一直占到客户端把文件读完**：下载很慢的客户端会让其他导出在这段时间里得到 `EXPORT_BUSY`。客户端停住不读时，代理的发送超时到点关闭连接，名额随之释放。下载中途断开或请求被取消同样释放，不留下任何状态。非导出请求不受影响。

### `GET /api/v1/customer/usage-events/export` —— 导出客户自己的已计费用量

与[用量页](#get-apiv1customerusage-events--客户自己的已计费用量)相同的口径：只含已计费（`PROCESSED`）的事件，按 `occurred_at` 的马来西亚日历日取，最新在前（`occurred_at` 倒序，同一时刻按 `id` 倒序）。一条 SELECT 取出整份文件，文件里的行来自同一个快照。

| 参数 | 规则 |
| --- | --- |
| `from_date` / `to_date` | **恒为必填**（带 `event_id` 也要）。`YYYY-MM-DD`，马来西亚日历日，含两端；其余规则与用量页相同：`2020-01-01` 到 `2099-12-31` 之间、`to_date` 不早于 `from_date`、最多 92 天 |
| `project_id` / `provider` / `model` / `conversation_id` / `event_id` | 可选，规则与用量页相同。不是自己的项目时得到只有表头的文件，不报错 |

- **与用量页的差别**：日期恒必填（用量页带 `event_id` 时可省略）；没有 `page_size` 与 `cursor`（给了按未知参数忽略），一次返回整个区间。其余的校验、文案与时序完全相同：形状由框架在鉴权之前判，日期是否存在、必填、先后与 92 天在鉴权之后判，所有 422 只列字段名、不回显值。
- **文件名**：`usage-events_<from_date>_<to_date>.csv`，例 `usage-events_2026-10-01_2026-10-31.csv`。
- **列**：表头恰好这 18 列，按这个顺序。每列的值与[用量页](#get-apiv1customerusage-events--客户自己的已计费用量)每项的同名键逐字相同；唯一改名的是时间列。

| 列 | 文本列（防公式注入） | 说明 |
| --- | --- | --- |
| `id` | 是 | 用量事件的 `public_id` |
| `event_id` / `request_id` / `conversation_id` / `feature` | 是 | 集成方上报的原值；`conversation_id`、`feature` 可能为空 |
| `project_id` / `project_name` | 是 | 项目的 `public_id` 与名称 |
| `provider` / `model` | 是 | 上报的原始字符串 |
| `usage_type` | 是 | 计量类型的 `code` |
| `input_tokens` / `output_tokens` / `cache_creation_input_tokens` / `cache_read_input_tokens` | 否 | 整数；数量形态的事件为空 |
| `quantity` | 否 | 恰好 8 位小数；token 形态的事件为空 |
| `unit` | 是 | 计量单位 |
| `charged_amount` | 否 | 本次请求向客户收的金额（`billable_cost`），恰好 8 位小数，含税 |
| `occurred_at_utc` | 否 | 值与 JSON 的 `occurred_at` 相同：不带时区的 UTC，保留微秒 |

- 文件里**没有**成本、原币成本、汇率、参考价、毛利、计费模式快照、状态、错误信息、`received_at` / `processed_at`、指纹、价格版本与规则引用与任何内部 id（INV-7），与用量页刻意不返回的那些相同。
- 拿别的客户的项目 id、`event_id`、`conversation_id` 来导出，只得到只有表头的文件（INV-8）。
- **与钱包流水的口径**：用量按 `occurred_at`（调用发生时间）的马来西亚日归属，钱包流水按记账时间；同一个日期区间两边可能差在跨日的几笔（例如月末发生、次月计费）。
- **`QUERY_TOO_BROAD`**：查询沿用用量页的 2 秒执行预算，超过时 422 `QUERY_TOO_BROAD`，没有部分结果；缩短区间或换更具体的筛选条件后重试。服务端记一条 `customer_usage_query_timeout` 日志（只带 `request_id` 与客户 id，不带查询参数的值）。

### 错误

| HTTP | `error.code` | 什么时候 |
| --- | --- | --- |
| 401 | `TOKEN_INVALID` | 没有令牌、令牌无效或过期；门户账号已停用或不是 `ACTIVE` |
| 403 | `CUSTOMER_REQUIRED` | 令牌有效，但调用者是 ADMIN，或是一个不属于任何客户的 CUSTOMER |
| 422 | `VALIDATION_ERROR` | 查询参数违反上面的规则（包括缺 `from_date` 或 `to_date`） |
| 422 | `EXPORT_TOO_LARGE` | 超过 20 000 行 |
| 422 | `QUERY_TOO_BROAD` | 查询超过 2 秒执行预算 |
| 500 | `INTERNAL_ERROR` | 意外错误，**包括查询时数据库连接失败或中断**（与其他客户接口一致：查询时的连接错误不是 503），带 `request_id`；信封与通用 500 相同 |
| 503 | `EXPORT_BUSY` | 已有一个导出在生成或下载；响应头 `Retry-After: 5`，几秒后重试 |
| 503 | `DATABASE_NOT_CONFIGURED` | 服务端根本没有配置数据库 |

所有错误都是 JSON 信封，不是 CSV。没有 404，也没有 403 `ACCOUNT_READ_ONLY`。

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
| `POST /api/v1/admin/customers/{customer_id}/internal-metering` | 200，客户详情 | 401 / 403 / 404 / 409 / 422 / 503 |
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
  "billing_mode": "PREPAID",
  "account_status": "ENABLED",
  "ai_service_enabled": false,
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
| `billing_mode` | 默认为 `PREPAID`；Acuven 自用租户可在首次使用前受控设为 `INTERNAL_METERED_ONLY` |
| `ai_service_enabled` | 按模式、余额和管理员账户状态计算的有效 AI 权限；内部模式免余额停用，但不免管理员禁用 |
| `account_status` | 管理员控制的账户生命周期，与 `billing_status` 互相独立：`PENDING_ACTIVATION`、`ENABLED`、`DISABLED`、`CLOSED` 之一。新客户与迁移前已有的客户都是 `ENABLED`；现在只有 `ENABLED` / `DISABLED` 会出现（AIH-TASK-020） |
| `status_version` | 计费状态、账户状态、计费模式或该客户任一项目的集成状态每真实跃迁一次 +1，各维共用、只增不减。新客户是 0 |
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
  "integration_status": "ENABLED",
  "status_webhook_configured": false,
  "created_at": "2026-09-20T08:31:00",
  "updated_at": "2026-09-20T08:31:00"
}
```

| 字段 | 说明 |
| --- | --- |
| `description` | 可为 `null` |
| `integration_status` | `ENABLED` / `DISABLED`（AIH-TASK-050）。新项目与迁移前已有的项目都是 `ENABLED`；改它见[项目集成状态与状态 webhook 地址](#管理端项目集成状态与状态-webhook-地址) |
| `status_webhook_configured` | 布尔：项目是否设了状态 webhook 地址。⚠️ 项目对象里**没有地址本身**（路径里可能带集成方的令牌），完整地址只出现在 `…/status-webhook` 的 PUT 与 GET 响应里 |

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

### `POST /api/v1/admin/customers/{customer_id}/internal-metering` —— 初始化内部计量租户

仅 ADMIN 可调用；请求体为 `{"reason":"非空审计原因"}`。必须明确提供目标客户的 public ID，
不会根据邮箱或公司名自动判定。只有尚无集成凭据、用量事件及钱包流水且余额为零的租户可启用；
否则 409 `INTERNAL_METERING_NOT_ELIGIBLE`。成功后返回客户详情，模式为
`INTERNAL_METERED_ONLY`，`status_version` 加一并写审计。重复调用幂等；本接口不支持切回预付。
该模式的参考售价不计入收入、应收或钱包，详见
[内部计量模式设计](design/INTERNAL-METERED-ONLY-2026-10-08.md)。

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
  [`POST …/account-status`](#管理端账户状态)；低余额阈值走
  [`PUT …/low-balance-threshold`](#管理端低余额阈值)；调账不走这个接口。
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

删除客户与项目（财务记录永久保留）、关户（`→ CLOSED`，见[管理端账户状态](#管理端账户状态)末尾）。
低余额阈值见[下一节](#管理端低余额阈值)；账户状态、管理员调账、API 凭据、项目集成状态与状态 webhook
地址见后面几节。

---

## 管理端低余额阈值

设计依据：设计闸门 #433 `APPROVED: design v2`，全文见 [design/AIH-TASK-109-low-balance-threshold.md](design/AIH-TASK-109-low-balance-threshold.md)（spec §48、§56、§7 第 11 条）。实现登记为 AIH-TASK-109。

| 方法与路径 | 成功 | 错误 |
| --- | --- | --- |
| `GET /api/v1/admin/customers/{customer_id}/low-balance-threshold` | 200，阈值对象 | 401 / 403 / 404 / 503 |
| `PUT /api/v1/admin/customers/{customer_id}/low-balance-threshold` | 200，阈值对象 | 401 / 403 / 404 / 422 / 500 / 503 |

两个接口都只有 ADMIN 能调；客户只来自路径里的 `{customer_id}`（`public_id`）。客户不存在：404
`CUSTOMER_NOT_FOUND`，什么都不写。客户的账户状态（含 `DISABLED`）与计费模式（含 `INTERNAL_METERED_ONLY`）
都不拦。客户自己只读，门户没有修改入口。

### 阈值对象

`data` 恰好一个键：

```json
{ "low_balance_threshold": "50.00000000" }
```

| 字段 | 说明 |
| --- | --- |
| `low_balance_threshold` | 恰好 8 位小数的字符串；未设置（不提醒）为 `null`。新客户与现有客户默认都是 `null` |

### `GET …/low-balance-threshold` —— 读取

成功：**200**，`data` 是阈值对象。只读，不加锁，不写审计。

### `PUT …/low-balance-threshold` —— 设置或清除

请求体 `{"low_balance_threshold": "50.00"}`，或 `{"low_balance_threshold": null}` 表示清除。

| 字段 | 必填 | 规则 |
| --- | --- | --- |
| `low_balance_threshold` | 是（可为 `null`） | JSON 字符串，匹配 `[0-9]{1,12}(\.[0-9]{1,2})?`（整数最多 12 位、小数最多 2 位），且大于 0；或显式 `null` |

- 键缺失、JSON 数字、负数、`"0"` / `"0.00"`、超过 2 位小数、超过 12 位整数、空串、前后带空白、`"5."`、
  `".5"`、全角数字：一律 422 `VALIDATION_ERROR`，文案只列字段名、不回显值。不舍入，超出直接 422。
- 不收 0：余额 ≤ 0 已经是 `SUSPENDED`；不想提醒用 `null`。
- 其他字段一律 422：请求体不能带 `tenant_id`、`customer_id` 之类。
- 不要求 `reason`：前后值都进审计。

成功：**200**，`data` 是改后的阈值对象。客户行在事务内加锁（只锁客户行，不锁钱包），与现值比较：

- **真实改动**：阈值与 `updated_at` 的 UPDATE 和一条 `TENANT_LOW_BALANCE_THRESHOLD_SET` 审计同一事务提交，
  要么都在、要么都不在（审计写入失败时 500 带 `request_id`，阈值不变）。审计 `entity_type = tenant`、
  `entity_id` 为客户 `public_id`，带 IP 与 UA；`before_state` / `after_state` 都恰好是
  `{"public_id": …, "low_balance_threshold": "50.00000000" 或 null}`。
- **同值不写**：与现值相同（含 `"50"` 与 `"50.00"`、两边都是 `null`）时返回 200 与当前值，不写库、不写审计，
  `updated_at` 不变。PUT 是设置语义，重放天然幂等，没有幂等键。

不改余额、不写账本、不写 `domain_outbox`、不动 `status_version`（阈值不在状态快照里，不发状态 webhook）。

### 低余额事件

- **不补发**：把阈值调到当前余额之上（例如余额 30、阈值改成 50）**不**写 `tenant.low_balance`。这个事件只由
  记账跨越触发：某一笔记账把余额从阈值之上（`>`）压到阈值及以下（`≤`）时写恰好一条；余额回到阈值之上后，
  下一次跌破再写一条（重新武装）。
- **只对 `PREPAID` 客户**：`INTERNAL_METERED_ONLY` 客户无论阈值如何都不写这个事件（管理员调账照样记账，但不触发）。
- 事件仍无处理器，以 `PENDING` 持久等待，不计入告警的 `outbox_backlog`。

### 与记账的先后关系

记账（`post_transaction`）按钱包 → 客户行的顺序加锁，在客户行锁内读阈值与计费模式；设置只锁客户行，二者串行、
不成环。设置先拿到锁：记账等它提交，按新值判定；记账先拿到锁：设置等记账提交，那一笔按旧值判定。提交即生效，
没有生效时间，已提交的记账不回溯。同一客户的两次设置按锁顺序串行，最后提交的为准，每条审计的前后值对应它
自己那一次改动。锁等待超时或死锁：500 `INTERNAL_ERROR` 带 `request_id`，整体回滚，管理员重新提交即可。

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

## 管理端门户账号

设计依据：设计闸门 #368 `APPROVED: design v12`，全文见 [design/AIH-TASK-089-customer-portal-auth.md](design/AIH-TASK-089-customer-portal-auth.md)（§2「API」管理端、「管理员重置客户 2FA」、「邀请邮件与重置的隔离」，§4、§6；spec §52、§53）。实现登记为 AIH-TASK-094。

一个客户至多一个门户账号（spec §52）。六个接口都在 `/api/v1/admin/customers/{customer_id}/portal-account` 下，
都要 ADMIN（每个处理函数的第一条语句是 `require_admin`）；`{customer_id}` 是客户的 `id`（`public_id`）。
运维做法（开通、重发、重置 2FA 前的身份核实）见 [runbook.md「客户门户账号」](runbook.md#客户门户账号)。

| 方法与路径后缀 | 请求体 | 成功 | 专有错误 |
| --- | --- | --- | --- |
| `GET` | — | 200，门户账号 | 404 `CUSTOMER_NOT_FOUND` / `PORTAL_ACCOUNT_NOT_FOUND` |
| `POST` | `{email}` | 201，门户账号 | 409 `PORTAL_ACCOUNT_EXISTS` / `EMAIL_IN_USE` / `ACCOUNT_CLOSED`；422 邮箱格式 |
| `POST /resend-invitation` | `{email?}`，可省略 | 200，门户账号 | 409 `PORTAL_ACCOUNT_NOT_INVITED` / `EMAIL_IN_USE` / `ACCOUNT_CLOSED` |
| `POST /disable` | — | 200，门户账号 | 409 `PORTAL_ACCOUNT_ALREADY_DISABLED` |
| `POST /enable` | — | 200，门户账号 | 409 `PORTAL_ACCOUNT_NOT_DISABLED` / `ACCOUNT_CLOSED` |
| `POST /reset-2fa` | `{reason}` | 200，门户账号（`two_factor_enabled` 为 `false`） | 409 `PORTAL_2FA_NOT_ENABLED`；422 原因为空或过长 |

六个接口另外都可能返回 401 / 403（不是 ADMIN）、404 `CUSTOMER_NOT_FOUND`（客户不存在），除开通外都可能返回
404 `PORTAL_ACCOUNT_NOT_FOUND`（还没开通），以及 500 / 503。

| HTTP | `error.code` | 什么时候 | 写库 |
| --- | --- | --- | --- |
| 404 | `PORTAL_ACCOUNT_NOT_FOUND` | 客户存在，但还没有门户账号 | 否 |
| 409 | `PORTAL_ACCOUNT_EXISTS` | 开通时该客户已有门户账号。响应里不带邮箱，前端据此刷新 GET | 否 |
| 409 | `EMAIL_IN_USE` | 开通或重发改邮箱时，这个地址已被任何用户（包括 ADMIN）占用；按归一后的地址比较 | 否 |
| 409 | `ACCOUNT_CLOSED` | 客户已关户时开通、重发、启用 | 否 |
| 409 | `PORTAL_ACCOUNT_NOT_INVITED` | 重发时账号不在 `INVITED` | 否 |
| 409 | `PORTAL_ACCOUNT_ALREADY_DISABLED` | 停用一个已停用的账号 | 否 |
| 409 | `PORTAL_ACCOUNT_NOT_DISABLED` | 启用一个不在 `DISABLED` 的账号 | 否 |
| 409 | `PORTAL_2FA_NOT_ENABLED` | 重置 2FA 时，账号既没有已确认的 2FA，也没有待确认的注册 | 否 |

### 门户账号对象

六个接口成功时的 `data` 都是这个对象，恰好七个键：

```json
{
  "email": "portal@example.com",
  "status": "INVITED",
  "invited_at": "2026-10-23T08:00:00",
  "invitation_expires_at": "2026-10-26T08:00:00",
  "activated_at": null,
  "last_login_at": null,
  "two_factor_enabled": false
}
```

| 字段 | 说明 |
| --- | --- |
| `email` | 登录邮箱，已归一（去首尾空白、转小写）。与客户对象的联系邮箱是两回事，可以不同 |
| `status` | `INVITED`（已开通、尚未接受邀请）、`ACTIVE`、`DISABLED` 之一 |
| `invited_at` | 最近一次邀请的签发时刻；从未发过邀请时为 `null` |
| `invitation_expires_at` | 账号是 `INVITED` 且最近一次邀请未用、未过期时，为它的到期时刻；否则为 `null`（已接受、已停用、重新启用后旧邀请已作废、已过期，都是 `null`，表示需要重发） |
| `activated_at` | 客户第一次接受邀请的时刻；从未接受时为 `null` |
| `last_login_at` | 最近一次登录的时刻；可为 `null` |
| `two_factor_enabled` | 是否有已确认的 2FA；只有待确认的注册时为 `false` |

没有内部用户 id、`tenant_id`、哈希、锁定信息。时间都是不带时区的 UTC。

### 开通与重发

开通：

```json
{ "email": "portal@example.com" }
```

- 同一事务里建出 `INVITED` 的 CUSTOMER 用户（不可用的随机密码，凭密码登录不了）、一张邀请令牌（有效期由
  `BILLING_CUSTOMER_INVITATION_TTL_SECONDS` 决定，默认 72 小时）、一行 `PORTAL_INVITATION_REQUESTED` 出站事件与
  `PORTAL_ACCOUNT_CREATED` 审计。提交之后立即触发投递；触发失败不影响响应，周期恢复会补投。
- 邮箱先去首尾空白再校验格式（422），存之前转小写。不接受其他字段（422），客户只来自路径。
- 开通不是幂等的：第二次得到 409 `PORTAL_ACCOUNT_EXISTS`。

重发（只在 `INVITED` 时）：

```json
{ "email": "corrected@example.com" }
```

- 请求体可以省略，也可以是 `{}` 或 `{"email": null}`：都表示不改邮箱。给了邮箱、归一后与当前相同，同样不算改。
- 作废该账号全部未用的邀请令牌（旧链接立即失效），签发新令牌、写出站事件与 `PORTAL_INVITATION_RESENT` 审计，
  立即触发投递。
- 改邮箱时新地址被任何用户占用得到 409 `EMAIL_IN_USE`，什么都不写。

### 邀请邮件

- 链接是 `{BILLING_FRONTEND_BASE_URL}/accept-invitation?token=…`，前端的接受页匿名可达（不在登录守卫之内），
  提交到 [`POST /api/v1/auth/invitation/accept`](#post-apiv1authinvitationaccept--接受邀请)。路径与参数名由前端
  `routes/paths.ts` 登记，`tests/backend/test_invitation_link.py` 机械比对。
- 正文写明到期时刻：马来西亚时间（UTC+8）精确到分钟，括号附 UTC。只用出站事件 payload 里的 `expires_at`（与令牌行
  同一个值），不读当前配置、不写相对时长，所以重试或改了有效期配置之后，信里的时刻仍与令牌一致。正文另提示查垃圾箱。
- `BILLING_FRONTEND_BASE_URL` 为空时与重置邮件同样处理：不发一封带半截链接的信，按普通失败重试。
- 出站事件 payload 是 `{to, token, expires_at}`，含令牌原文；投递成功或进死信时置空。
- 邀请令牌与重置令牌互相用不了；`INVITED` 的客户点「忘记密码」得到同一个 200、不发信，没收到邀请由管理员重发。

### 停用与启用

- 停用（`INVITED` / `ACTIVE` → `DISABLED`）：吊销全部刷新令牌，作废全部未用的邀请与重置令牌，使之前签发的全部
  访问令牌与 pending 令牌永久失效（重新启用也不会复活），写 `PORTAL_ACCOUNT_DISABLED` 审计。客户已关户时也允许停用。
- 启用（`DISABLED` →）：从未接受过邀请的回到 `INVITED`，接受过的回到 `ACTIVE`；写 `PORTAL_ACCOUNT_ENABLED` 审计。
  **不自动重发邀请**：停用时旧邀请已作废，需要时再调重发。客户已关户时 409 `ACCOUNT_CLOSED`。
- 停用与客户的忘记密码按同一用户行排队：停用一旦提交，该账号不会再有可用的重置令牌。

### 重置 2FA

```json
{ "reason": "Lost phone; identity confirmed by a call-back to the registered number." }
```

- ⚠️ **调用前必须在线下核实对方确实是该客户**（runbook「客户门户账号」）。原因必填，去掉首尾空白后 1–255 个
  字符（与改账户状态同一条规则），存的是去掉首尾空白后的值；为空或超过 255 个字符 422，什么都不写。
- 删除该账号的 2FA（已确认或待确认）与全部恢复码，使该账号全部会话失效，写 `PORTAL_2FA_RESET` 审计（带原因）。
  账号状态不变，不发通知邮件。客户之后用密码登录，可以重新开启 2FA。

### 审计

`entity_type = users`、`entity_id` 是门户用户的内部 id（审计查询接口按既有规则把它显示为邮箱，只对 ADMIN 可见）。
`after_state` 只放 `tenant_public_id` 与 `status`，重发另加 `email_changed`，重置 2FA 只放 `tenant_public_id`；
**不放邮箱值**（REQ-PRIV-001）。⚠️ 原因只写业务说明，不写客户的联系人、电话、邮箱等个人数据。

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

## 管理端钱包流水

设计依据：设计闸门 #351 `APPROVED: design v2`，全文见 [design/AIH-TASK-083-wallet-ledger-view.md](design/AIH-TASK-083-wallet-ledger-view.md)（spec §60、§78、§108）。实现登记为 AIH-TASK-084。

| 方法与路径 | 成功 | 错误 |
| --- | --- | --- |
| `GET /api/v1/admin/customers/{customer_id}/wallet/transactions` | 200，一页账本行 | 401 / 403 / 404 / 422 / 500 / 503 |

只有 ADMIN 能调。`{customer_id}` 是客户的 `id`（`public_id`），租户只来自路径。只读：不写账本、不动余额、不写审计。

### 查询参数

| 参数 | 规则 |
| --- | --- |
| `page_size` | 1–100，默认 20；超出 422（与「分页」同一范围） |
| `before_sequence` | 可选，整数 1 – 9 223 372 036 854 775 807；只返回 `wallet_sequence < before_sequence` 的行。省略 = 从最新一行开始。超出 422 |
| `reference_type` | 可选，`USAGE_EVENT` / `PAYMENT` / `ADMIN_ADJUSTMENT` / `REBILL` / `SYSTEM` 之一，大小写敏感；其余 422 |

未知查询参数不报错（与其他列表接口一致）。参数由框架在鉴权**之前**校验，所以没带令牌、参数又不合法的请求拿到 422；文案只列字段名、不回显值。

`reference_type` 是来源，对应 spec §60 的分类：`PAYMENT` = 充值，`USAGE_EVENT` = 用量扣费，`ADMIN_ADJUSTMENT` = 调账（`ADJUSTMENT_CREDIT`、`ADJUSTMENT_DEBIT`、`BONUS`、`REFUND_ADJUSTMENT` 四种类型），`REBILL` = rebill 更正，`SYSTEM` = 系统更正。

### 响应

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

（示例只列了一页 20 行里的第一行；这一页最后一行的序号是 23。）

| 字段 | 说明 |
| --- | --- |
| `items` | 最新在前，按 `wallet_sequence` 倒序 |
| `id` | 账本行的 `public_id`（与调账响应的 `id` 同一个值） |
| `wallet_sequence` | 这一笔在钱包账本里的序号，从 1 起、连续递增 |
| `transaction_type` | 九种类型之一（`TOPUP`、`AI_USAGE`、`ADJUSTMENT_CREDIT`、`ADJUSTMENT_DEBIT`、`REBILL_CREDIT`、`REBILL_DEBIT`、`REFUND_ADJUSTMENT`、`BONUS`、`SYSTEM_CORRECTION`） |
| `amount` / `balance_before` / `balance_after` | 恰好 8 位小数的字符串，带符号，原样来自 `DECIMAL(20,8)` 列，不经浮点 |
| `reference_type` | 来源，见上 |
| `reference_id` | 库里的原值：用量扣费是上报的 `event_id`，调账是幂等键 |
| `usage_event_id` | 对应用量事件的 `public_id`，可直接用于 `GET /api/v1/admin/usage-events/{usage_event_id}`。只有 `USAGE_EVENT` 来源有；查不到对应事件（数据缺陷）时为 `null`，该行其余字段照常返回 |
| `description` | 库里的原因（调账、系统更正必有，AI 用量为空） |
| `created_by_email` | 操作者的邮箱；系统写入的行为 `null` |
| `created_at` | 记账时刻，UTC，不带时区 |
| `page_size` | 本次请求的 `page_size` |
| `next_before_sequence` | 还有更早的行时 = 本页**最后一行**的 `wallet_sequence`，原样作为下一页的 `before_sequence`；没有更多时 `null` |

每项恰好上面 12 个键。响应里没有内部自增 id、`tenant_id`、`wallet_id`、`created_by`（内部用户 id）、`metadata_json`，也没有任何成本或毛利。

### 翻页

1. 第一页不带 `before_sequence`。
2. 下一页把上一页的 `next_before_sequence` 原样作为 `before_sequence`；`next_before_sequence` 为 `null` 就是最后一页。
3. 带 `reference_type` 时序号不连续，游标照样只取本页最后一行的序号，不要用「最后一行 − 1」之类自己推算。
4. 「回到最新」= 不带 `before_sequence` 重新取第一页。

翻页之间新记的账序号都更大，只会出现在最新那一端；往旧翻的页不重复、不遗漏。

**为什么不用「分页」的 `page` / `total`**（设计 §9）：每个 AI 请求一行账本，大客户的账本是 spec §108 说的大表。`OFFSET` 要先扫过前面所有行，`total` 每次要数全部匹配行；新行插入时按页码翻还会重复上一页的末尾几行。序号游标在任意深度、带不带筛选都是索引上的范围扫描。不带筛选时总行数等于钱包的 `version`（客户详情已返回），需要时从那里取。

### 错误

| HTTP | `error.code` | 什么时候 |
| --- | --- | --- |
| 401 | `TOKEN_INVALID` | 没有令牌或令牌无效 |
| 403 | `ADMIN_REQUIRED` | 不是管理员 |
| 404 | `CUSTOMER_NOT_FOUND` | 路径里的客户不存在 |
| 422 | `VALIDATION_ERROR` | 查询参数超出上面的规则 |
| 500 | `INTERNAL_ERROR` | 意外错误，带 `request_id` |
| 503 | `DATABASE_UNAVAILABLE` 等 | 数据库不可用，沿用通用处理 |

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

## 管理端项目集成状态与状态 webhook 地址

设计依据：设计闸门 #241 `APPROVED: design v6`，全文见 [design/PHASE-3-status-sync.md](design/PHASE-3-status-sync.md) §2「接口」、§4（spec §24、§25、§28）。
规则在 AIH-TASK-049 的服务函数里，接口登记为 AIH-TASK-050。投递（真正发出 webhook）归 AIH-TASK-051，还没有上线。

| 方法与路径 | 请求体 | 成功 | 错误 |
| --- | --- | --- | --- |
| `POST …/integration-status` | `{"integration_status": str, "reason": str}` | 200，项目对象 | 401 / 403 / 404 / 409 / 422 / 500 / 503 |
| `PUT …/status-webhook` | `{"url": str 或 null}` | 200，地址对象 | 401 / 403 / 404 / 409 / 422 / 500 / 503 |
| `GET …/status-webhook` | — | 200，地址对象 | 401 / 403 / 404 / 503 |

`…` 是 `/api/v1/admin/customers/{customer_id}/projects/{project_id}`。只有 ADMIN 能调。`{customer_id}`、`{project_id}` 是
`public_id`；项目不属于路径里的客户时与不存在一样 404。本节专有的错误码：

| HTTP | `error.code` | 什么时候 | 写库 |
| --- | --- | --- | --- |
| 404 | `CUSTOMER_NOT_FOUND` | 路径里的客户不存在 | 否 |
| 404 | `PROJECT_NOT_FOUND` | 项目不存在，或不属于路径里的客户（两者一模一样） | 否 |
| 409 | `ACCOUNT_CLOSED` | 客户已关户（`CLOSED`）时改集成状态（任何目标，含与当前相同），或设为一个地址（清除不受限） | 否 |
| 422 | `VALIDATION_ERROR` | 请求体不合法、有未声明的字段，或地址不合规。`message` 不回显地址的任何部分 | 否 |

### `POST …/integration-status` —— 改项目集成状态

| 字段 | 必填 | 规则 |
| --- | --- | --- |
| `integration_status` | 是 | `ENABLED` 或 `DISABLED`，大小写敏感 |
| `reason` | 是 | 去掉首尾空白后长度 1–255。只进审计的 `reason`；只写业务说明，不写个人数据 |

其他字段一律 422。判定顺序：请求体（422）→ 客户与项目（404）→ 客户 `CLOSED`（409，**即使目标与当前相同**）→
目标与当前相同（200，不写）→ 跃迁。

- **跃迁**：200，`data` 是改后的[项目对象](#项目对象)。同一事务里：项目的 `integration_status` 与 `updated_at`；客户的
  `status_version` +1 与状态跃迁时刻；一条 `project.integration_status_changed` 领域事件（payload 不带原因）；一条
  `PROJECT_INTEGRATION_STATUS_CHANGED` 审计（`entity_type = project`、`entity_id` 是项目的 `public_id`，`before_state` /
  `after_state` 只有 `integration_status` 与 `status_version`）。任何一步失败整体回滚。
- **目标与当前相同**：200，返回当前项目对象，什么都不写（不加版本、不写事件与审计）。超时重发是安全的。
- 停用项目**不吊销凭据、不拒绝摄取**：在途用量照常入账；`effective-status` 对它返回 `BLOCK_AI` /
  `PROJECT_INTEGRATION_DISABLED`。停用的项目**不推送**状态 webhook（spec §25），使它停用的那一次也不推，集成方经
  [周期对账](#对账)得知；重新启用时版本 +1，之后照常推送当前状态。

### `PUT …/status-webhook` —— 设置或清除状态 webhook 地址

| 字段 | 必填 | 规则 |
| --- | --- | --- |
| `url` | 是（缺失 422） | 字符串：按下面的规则规范化并校验，不合规 422；显式 `null`：清除 |

其他字段一律 422。地址规范化与校验（设计 §2「URL 规范化与校验」）：

- 原始输入里有空格或控制字符（0x00–0x20、0x7F）、`@`、`#`（含空的用户信息与空的 fragment），或主机名后面跟着冒号却没有端口：422。
- scheme 必须是 `https`（不分大小写）；端口省略或 443。
- 主机名去掉末尾一个点、转小写、IDNA 编码成 ASCII；每个点分段是 LDH 形式（小写字母、数字、连字符，不以连字符开头或结尾，
  长 1–63），整个主机名 ≤ 253。
- **IP 字面量一律拒绝**：带方括号的 IPv6、能解析成 IP 的、每段都是十进制数字或 `0x` 十六进制的（`127.1`、`2130706433`、`0x7f.1`）。
- 路径为空取 `/`；查询串为空（含只有一个 `?`）时去掉；路径与查询串只允许 0x21–0x7E，按原样保留、不做百分号编码的规范化。
- 保存的是重新拼出的 `https://<规范化主机名><path>[?query]`（端口 443 省略），总长 ≤ 2048。

判定顺序：请求体与地址规范化（422）→ 客户与项目（404）→ 设为地址而客户 `CLOSED`（409；清除不受限，关户的客户也能停止推送）→
规范化后与当前值逐字节相同（200，不写）→ 写入。

- **规范化后与当前值相同**（例如只差大小写，或两次都是 `null`、包括清除一个从未设过的地址）：200，什么都不写，不写审计。
- **真实变化**：200。同一事务里改地址，并把该项目的投递游标清零（新地址会收到一次当前状态；发往旧地址的在途请求的结果作废）；
  写一条 `PROJECT_STATUS_WEBHOOK_SET` 审计，`before_state` / `after_state` 只有 `status_webhook_host`（规范化主机名或 `null`），
  **不记完整地址**。
- 地址只能由 ADMIN 设置。平台不做 DNS 解析后的内网地址拦截（设计 §10）。

### `GET …/status-webhook` —— 读取地址

只读，不写审计。`data` 与 PUT 的响应相同。

### 地址对象

```json
{
  "status_webhook_url": "https://hooks.example.com/acuven/receive",
  "status_webhook_configured": true
}
```

| 字段 | 说明 |
| --- | --- |
| `status_webhook_url` | 规范化后的地址；没设时为 `null` |
| `status_webhook_configured` | `status_webhook_url` 不为 `null` |

⚠️ 完整地址**只出现在这两个接口的响应里**；项目对象只有 `status_webhook_configured`，审计只有主机名。

---

## 状态 webhook 签名

设计依据同上（设计 §2「签名库」；spec §28、§37）。签名库是 AIH-TASK-019 的（`app/services/webhook_signing.py`
的 `signing_material` 与 `sign_status_webhook`）；请求体、接收规则与对账契约来自设计闸门 #241 v6
（[design/PHASE-3-status-sync.md](design/PHASE-3-status-sync.md) §2「Webhook 请求」「时间」，AIH-TASK-050 写进这里）。
发送方（投递）归 AIH-TASK-051，还没有上线。规范串与
[集成请求签名](#集成请求签名)是**同一套规则、同一份实现**，只有第四行不同。**本节是那条差别的唯一出处**，
集成方验签照这里实现。

`POST <status_webhook_url>`：地址由管理员按项目设置（见[上一节](#管理端项目集成状态与状态-webhook-地址)）。只有
`integration_status = ENABLED` 且设了地址的项目才收到推送，内容永远是该项目**当前**的有效状态。

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
同时接受新旧两个版本，确认新版本能通过后再停用旧版本（ADR-0004 §4a）。

**时间窗**：`X-Acuven-Timestamp` 与集成方本机时间相差**不超过 300 秒**（含正负两个方向，恰好 300 秒算通过），与入站签名的
`MAX_SKEW_SECONDS` 相同。超出就回非 2xx；平台按失败重试，每次重发都重新生成时间戳与签名。

### 请求体

`Content-Type: application/json`，按键排序、无多余空白；签名覆盖的就是这些原始字节。例：

```json
{"account_status":"ENABLED","billing_mode":"PREPAID","billing_status":"SUSPENDED","effective_at":"2026-10-09T10:10:00","effective_status":"BLOCK_AI","event_id":"sevt_9b1d7e0c-5a2f-4d8e-b6c3-0e4f1a2b3c4d_42","project_id":"9b1d7e0c-5a2f-4d8e-b6c3-0e4f1a2b3c4d","project_integration_status":"ENABLED","reason_code":"BALANCE_NOT_POSITIVE","status_version":42,"tenant_id":"3f0e6c1a-8d4b-4c6e-9a51-2b7f0d9c4e11"}
```

恰好这 11 个字段：

| 字段 | 说明 |
| --- | --- |
| `event_id` | `sevt_<项目 public_id>_<status_version>`，见下 |
| `tenant_id` | 租户（客户）的 `public_id` |
| `project_id` | 项目的 `public_id` |
| `account_status` | `PENDING_ACTIVATION` / `ENABLED` / `DISABLED` / `CLOSED` |
| `billing_status` | `ACTIVE` / `SUSPENDED` |
| `billing_mode` | `PREPAID` / `INTERNAL_METERED_ONLY` |
| `project_integration_status` | `ENABLED` / `DISABLED` |
| `effective_status` | `ALLOW_AI` / `BLOCK_AI` |
| `reason_code` | `BLOCK_AI` 的原因，取值见[集成：有效 AI 服务状态](#集成有效-ai-服务状态)；`ALLOW_AI` 时为 `null` |
| `status_version` | 租户的状态版本，整数，只增不减 |
| `effective_at` | 最近一次状态跃迁的时刻，格式见[时间](#时间) |

- 字段名与 [`effective-status`](#集成有效-ai-服务状态) 的响应**完全一致**，集成方只写一份解析。
- 不带余额、阈值、成本、管理员原因，也不带 spec §92 的事件类型名：payload 是完整的状态快照，集成方按
  `effective_status` 与 `reason_code` 处理。
- ⚠️ **`reason_code` 只给集成方后端，不得展示给终端用户**（spec §26）。

**`event_id` 规则**：由（项目, 版本）确定。同一版本重发时 `event_id` 不变；同一版本的状态快照也唯一（任何状态变化都会让
版本 +1）。前缀 `sevt_` 与用量事件 id 区分，它没有任何财务效果。

### 集成方怎么应用

集成方本地按项目保存**最后一次接受的状态与 `status_version`**，按本地状态放行或拦截（spec §26）。收到验签与时间窗都通过的推送后：

| 收到的 `status_version` 与本地相比 | 处理 | 回给平台 |
| --- | --- | --- |
| 更大（或本地还没有状态） | 应用：覆盖本地状态与版本 | 2xx |
| 相同 | 幂等：不改本地状态（即使载荷不同也不覆盖已应用的状态） | 2xx |
| 更小（旧版本） | 忽略 | **2xx**（确认收到，不让平台重试旧版本） |

- 平台只把 2xx 视为送达；3xx（不跟随重定向）、4xx、5xx、超时、连接错误都会按指数退避重试，**不设放弃上限**，最终一定送达
  最新版本。中间版本可以合并：投递停顿期间状态变了几次，恢复后只送最新的那一个。
- 验签失败、时间窗外：回非 2xx，不应用。
- 推送失败或 Billing Hub 不可用时，集成方**保留最后一次接受的状态**；从未拿到过状态就放行（不变量 1：中心计费故障不得
  中断客户 AI 服务）。

### 对账

推送之外，集成方必须周期对账（spec §30），兜底推送的丢失与停用项目（停用项目不推送）。对账调
[`GET /api/v1/integration/effective-status`](#集成有效-ai-服务状态)，用与推送**相同的版本规则**：版本更大才应用，
同版本幂等，旧版本忽略。

- **周期**：后台每 **4 分钟**调一次，不在 AI 调用路径上。
- **单次超时**：**10 秒**。
- **失败重试**：失败（超时、非 200、应答校验不过）后 **30 秒**重试一次，之后回到 4 分钟节奏。
- **最坏滞后**（Billing Hub 可用时）：4 分钟 + 10 秒超时 + 30 秒重试 + 10 秒 = **4 分 50 秒 < 5 分钟**（spec §119；等于上限
  不算满足），可以容忍一次偶发失败。
- **连续失败**（Billing Hub 不可用）：任何设计都给不出 5 分钟上限。此时**保留最后一次接受的状态**，从未拿到过状态就放行；
  最后一次成功对账早于 5 分钟时，集成方发出自己的健康告警。

这些行为由外部接入方实现与测试；本仓库只交付契约与 `effective-status` 本身。

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

## 管理端用量事件：查询

spec §61「Usage Management」与 §89 的管理端用量查询（AIH-TASK-034）。不走设计闸门：只读，不碰钱、状态机、摄取、认证与
webhook。实现在 `app/api/admin_usage_events.py`、`app/repositories/usage_events.py` 与 `app/schemas/usage_events.py`。

| 方法与路径 | 成功 | 错误 |
| --- | --- | --- |
| `GET /api/v1/admin/usage-events` | 200，`Page[事件列表项]` | 401 / 403 / 422 / 500 / 503 |
| `GET /api/v1/admin/usage-events/{usage_event_id}` | 200，事件详情 | 401 / 403 / 404 / 500 / 503 |

- 只有 ADMIN 能调（处理函数第一条语句就是鉴权）。路径里的 `usage_event_id` 是事件的 `public_id`。
- 只读：不写任何行，也不写审计（与审计日志查询一样）。
- **成本、计费额与毛利只出现在这两个管理端接口里**（INV-7）。响应不含内部自增 id、对话内容、签名或凭据 secret；
  对外的引用一律是 `public_id`。

### 列表的查询参数

全部可选，省略即不筛，多个条件取 AND。分页照「分页（spec §108）」；按事件的内部 id **倒序**（≈ 最新接收的在前）。

| 参数 | 规则 |
| --- | --- |
| `customer_id` | 客户的 `public_id`。不存在时是空页（`total` 为 0），不报 404 |
| `project_id` | 项目的 `public_id`。不存在、或与 `customer_id` 不属同一客户时是空页 |
| `conversation_id` / `request_id` | 与上报值逐字节比较。`request_id` 只在客户内有意义：不同客户可以上报同一个值，单独按它筛会把它们都列出来 |
| `provider` / `model` | 与上报的**原始字符串**（`provider_code_raw` / `model_code_raw`）逐字节比较，模型未知的事件也筛得到 |
| `feature` | 精确匹配调用方上报的功能标签；格式与用量摄取的 `feature` 相同 |
| `occurred_from` / `occurred_to` | RFC 3339、**必须带时区**、整秒；换成 UTC 后按 `occurred_at` 取 `[occurred_from, occurred_to)`（含起点、不含终点）；`occurred_from` 必须早于 `occurred_to` |
| `status` | spec §83 的九个取值之一，其余取值 422 |
| `error_code` | 与事件的 `error_code` 逐字比较 |
| `page` / `page_size` | 见「分页」 |

`status` 与分页在鉴权**之前**校验（不合法就 422，与其他列表接口一致）；`feature`、时间格式与区间在鉴权**之后**判，同样是 422
`VALIDATION_ERROR`，文案只列字段名、不回显值。未知的查询参数不报错。

用到的索引（迁移 0017 / 0018，本任务不加迁移）：客户 → `ix_usage_events_tenant_id`，项目 → `ix_usage_events_project_id`，
会话 → `ix_usage_events_conversation_id`，请求 → `ix_usage_events_request_id`，状态（可带时间段）→
`ix_usage_events_status_occurred`，只有时间段 → `ix_usage_events_occurred_at`。`provider` / `model` / `feature` / `error_code` 没有
专门的索引：与上面任一条件同用时是残余过滤；单独使用时按主键倒序扫描取一页，`total` 的 `COUNT` 要扫全表。

### 列表项

```json
{
  "id": "00000000-0000-4000-8000-000000000001",
  "event_id": "00000000-0000-7000-8000-000000000001",
  "customer_id": "00000000-0000-4000-8000-000000000002",
  "customer_company_name": "Fictional Sdn Bhd",
  "project_id": "00000000-0000-4000-8000-000000000003",
  "request_id": "call-1",
  "conversation_id": null,
  "provider": "openai",
  "model": "whisper-x",
  "feature": null,
  "usage_type": "AUDIO_SECOND",
  "input_tokens": null,
  "output_tokens": null,
  "cache_creation_input_tokens": null,
  "cache_read_input_tokens": null,
  "quantity": "2.00000000",
  "unit": "SECOND",
  "status": "PROCESSED",
  "error_code": null,
  "occurred_at": "2026-09-29T09:30:00",
  "received_at": "2026-09-29T08:30:00",
  "processed_at": "2026-09-29T10:30:00",
  "billable_cost": "0.12500000",
  "estimated_provider_cost_myr": "0.06250000",
  "billing_mode_snapshot": "PREPAID",
  "reference_customer_price": null
}
```

- `id` 是事件的 `public_id`；`event_id` 是集成方上报的幂等键；`customer_id` / `project_id` 是 `public_id`。
- `provider` / `model` 是上报的原始字符串；`usage_type` 是计量类型代码。
- `feature` 是调用方的可选功能标签；带值时为 2–4 个小写字母开头、以点分隔的段，每段可含小写字母、数字与下划线，最多 64 个字符。
- `LLM_TOKEN_FIELDS` 形态四个 token 数是整数、`quantity` 为 null；`QUANTITY` 形态相反。`quantity` 与金额一样是 8 位小数的字符串。
- `occurred_at` 保留上报的小数秒（存在时）；其余时间整秒。全部是不带时区的 UTC。
- `billable_cost`（含税、从钱包扣的额）与 `estimated_provider_cost_myr` 是 8 位小数的字符串；未计费的事件为 null。
- `billing_mode_snapshot` 是处理时的计费模式；`reference_customer_price` 仅内部计量事件有值。该参考价是模拟客户售价，不属于应收或收入。内部事件的 `billable_cost` 为零且不生成账本行。

### 详情

列表项的全部字段，再加：

| 字段 | 含义 |
| --- | --- |
| `schema_version` / `payload_shape` / `quantity_kind` / `payload_fingerprint` | 摄取时记下的载荷版本、形态、数量类型与指纹 |
| `error_message` | 计费失败时 worker 记下的异常类型名（只是类型名，不含载荷）；否则 null |
| `created_at` | 行的写入时刻 |
| `provider_ref_id` / `model_ref_id` | 计费 worker 解析出的供应商、模型的 `public_id`（§79 的目录引用）；模型未知或未处理时 null |
| `provider_price_version_id` / `pricing_rule_id` / `fx_rate_version_id` | 计费所用价格版本、定价规则、汇率版本的 `public_id`；MYR 原币时 `fx_rate_version_id` 为 null |
| `fx_rate_applied` | 所用汇率原值（去掉尾零的精确十进制字符串，与汇率版本对象一致）；MYR 原币时 null |
| `provider_source_currency` / `provider_source_cost` | 原币币种与原币成本（8 位小数） |
| `gross_margin` / `gross_margin_basis` | 预付事件毛利 = `billable_cost − estimated_provider_cost_myr`（可为负），`basis` 为 `"estimated"`。内部计量及未计费事件两者都为 null；参考价不得作为正式毛利计算依据 |
| `wallet_transaction` | 那一行 `AI_USAGE` 账本：`{"id": public_id, "amount": "-0.12500000"}`（金额 = −计费额）；0 元事件与未计费事件为 null |
| `attempt_count` / `next_attempt_at` | 认领次数与最早再认领时刻 |
| `claim_token` / `claimed_at` / `lease_expires_at` | 当前认领的防护令牌与租约；只有 `PROCESSING` 时非空 |
| `conflicts` | 撞了这个 `event_id` 的冲突请求，按写入顺序：`[{"api_key", "mismatch", "received_at"}]`（`api_key` 是请求方的公开 key，`mismatch` 是 `OWNERSHIP` / `FINGERPRINT` / `BOTH`）；没有时 `[]` |

错误状态的事件没有快照：价格、规则、汇率、成本、计费额、毛利与账本行全为 null（已解析到的 `provider_ref_id` /
`model_ref_id` 照样给出，方便管理员看缺什么）。

本节专有的错误码：

| HTTP | `error.code` | 什么时候 |
| --- | --- | --- |
| 404 | `USAGE_EVENT_NOT_FOUND` | 详情：路径里的事件不存在 |
| 422 | `VALIDATION_ERROR` | 列表：`status` 不是九个取值之一、`feature` 格式不合法、分页越界、时间不带时区 / 带小数秒 / 不是合法时间、`occurred_from` 不早于 `occurred_to` |

---

## 管理端用量分析

`GET /api/v1/admin/usage-analytics`（AIH-TASK-073）是仅 ADMIN 可用的只读报表。`group_by` 必填，可为
`provider`、`model`、`customer`、`project` 或 `feature`；`occurred_from` 与 `occurred_to` 必填，按
`occurred_at` 取左闭右开的 RFC 3339 区间，最大跨度 366 天。它支持与管理端用量事件列表相同语义的
`customer_id`、`project_id`、`provider`、`model`、`feature` 筛选及 `page` / `page_size` 分页。

- 只统计 `PROCESSED` 事件，按事件数倒序、分组键升序；供应商和模型使用已解析目录代码，未解析值单列为
  `null` 组，客户和项目返回 public id 与名称。
- 响应的 `cost_basis` 固定为 `estimated`。金额是 8 位小数的十进制字符串；平均 token 与毛利率也是十进制
  字符串，全程不经 float。成本为处理时保存的估算供应商成本，**不含网关手续费，也不是 Phase 7 财务毛利**。
- `PREPAID` 块包含计费额、估算成本、毛利和毛利率；`INTERNAL_METERED_ONLY` 只包含事件数、估算成本和参考售价。
  内部计量租户的参考售价不是收入，因此不进入毛利。

查询使用已有的 `ix_usage_events_status_occurred`（状态与时间段）、`ix_usage_events_tenant_id`、
`ix_usage_events_project_id` 与 `ix_usage_events_tenant_feature_occurred`；没有新增迁移或索引。

---

## AI 调用接口

设计依据：设计闸门 #281 `APPROVED: design v23`，全文见 [design/AI-MODULE.md](design/AI-MODULE.md) §2「调用接口 `POST /v1/messages`」。此接口供业务后端调用模型；提示词、输入输出格式、输出解析与质量评测都由调用方负责。AI 密钥须等 AIH-TASK-068 合并后才签发。

### 入口与鉴权

- 同机业务后端经同机的 `billing_nginx` 入口调用 `POST /v1/messages`，不绕行公网。
- 公网业务后端经公网 HTTPS 的 nginx 入口调用同一路径；nginx 按来源限速。
- 每个请求带 `X-Acuven-Client-Id: aic_…` 与 `Authorization: Bearer ait_…`。这是项目级 AI 密钥，不可与管理员会话或集成 HMAC 凭据互用。

### 请求

请求体是严格 JSON 对象：`strict=True`、`extra="forbid"`；未知字段一律 422，JSON 的 `true`、`4000.0` 与 `"4000"` 都不是可接受的 `max_tokens`。应用按原始 UTF-8 字节限制请求体 ≤ `BILLING_AI_MAX_REQUEST_BYTES`（默认 65536）；nginx 的外层上限是 80 KiB。

| 字段 | 必填 | 规则 |
| --- | --- | --- |
| `feature` | 是 | 功能标签，`^[a-z][a-z0-9_]{0,31}(\.[a-z][a-z0-9_]{0,31}){1,3}$`，最多 64 字符，例如 `crm.contact_summary`。调用方自行命名，只用于统计，不影响调用或计价 |
| `model` | 是 | 最多 128 字符；必须是供应商 `anthropic` 下 `ACTIVE` 模型的 `code`，或当前生效且指向 `ACTIVE` 模型的别名 |
| `system` | 否 | 字符串 |
| `messages` | 是 | 1–50 条；每条为 `{"role":"user" \| "assistant","content":非空字符串}`，第一条必须为 `user` |
| `max_tokens` | 是 | 整数，范围为 1 至 `BILLING_AI_MAX_OUTPUT_TOKENS`（默认 4000） |
| `output_config` | 否 | 严格对象；可含 `format`（`{"type":"json_schema","schema":对象}`）与 `effort`（`low` / `medium` / `high`），原样转给供应商 |

模型目录中供应商也必须为 `ACTIVE`。直接给模型 `code` 时转发该 `code`；给生效别名时先解析为目标模型 `code` 再转发；同一个字符串同时为模型 `code` 与别名时按模型 `code` 处理。目录外、已停用或别名已失效的模型均为 422 `MODEL_NOT_ALLOWED`。请求路径不读取供应商价格。

### 成功响应

成功为 200，响应不套通用错误信封：

```json
{
  "request_id": "01J…",
  "model": "供应商返回的实际模型名",
  "text": "按顺序用换行拼接的 text 块",
  "stop_reason": "end_turn",
  "usage": {
    "input_tokens": 0,
    "output_tokens": 0,
    "cache_creation_input_tokens": 0,
    "cache_read_input_tokens": 0
  }
}
```

`request_id` 是 AI 模块生成的 26 位 ULID，也是对应使用量事件的 `request_id`，供调用方对账。它不同于 HTTP 关联 ID：中间件仍会在 `X-Request-ID` 响应头和错误信封中提供关联 ID，两者无关。`usage` 仅含 token 数，不含金额。供应商拒答、截断或输出不符合调用方预期时仍可为 200；调用方应按 `stop_reason` 与自身 schema 处理。

### 错误

除 nginx 在请求到达应用前返回的错误外，错误均使用 §107 信封，且 `/v1/messages` 的信封不含 `retryable`：

| HTTP | 错误码 | 情形 |
| --- | --- | --- |
| 401 | `AI_AUTH_FAILED` | 缺头、未知 client、token 错误或密钥已吊销 |
| 403 | `AI_SERVICE_BLOCKED` | 租户或项目有效状态为 `BLOCK_AI`；不透露原因 |
| 413 | `PAYLOAD_TOO_LARGE` | nginx 拒绝超过 80 KiB 的请求；信封的 `request_id` 为 `null` |
| 422 | `VALIDATION_ERROR` / `REQUEST_TOO_LARGE` / `MODEL_NOT_ALLOWED` / `PROVIDER_REJECTED_REQUEST` | 请求不合规、应用侧原始字节超限、模型不允许，或供应商以 400 / 404 / 413 / 422 拒绝；最后一种只返回供应商错误 `type`，不回显 `message` |
| 429 | `RATE_LIMITED` | nginx 按来源限速；请求未到应用，信封 `request_id` 为 `null` |
| 502 | `AI_PROVIDER_UNAVAILABLE` | 供应商超时、网络错误、429、5xx、401 / 403，或响应没有可读的 `usage` |
| 503 | `AI_STORE_UNAVAILABLE` / `AI_PROVIDER_NOT_CONFIGURED` / `AI_USAGE_NOT_PERSISTED` | 鉴权读库失败或超时、AI 库访问已满、未配置模型 key，或用量无法确认落库 |

每次成功推理会先写本地 `ai_usage_outbox` 的 `PENDING` 行再返回 200；摄取、计费 worker 或 broker 不可用不阻断调用，恢复后由投递扫描补送。

---

## 管理端 AI 密钥

设计依据：设计闸门 #281 `APPROVED: design v23`，全文见 [design/AI-MODULE.md](design/AI-MODULE.md) §2「管理」。实现登记为 AIH-TASK-059。

| 方法与路径 | 请求体 | 成功 | 错误 |
| --- | --- | --- | --- |
| `GET /api/v1/admin/customers/{customer_id}/projects/{project_id}/ai-access` | — | 200，项目的全部 AI 密钥 | 401 / 403 / 404 / 503 |
| `POST …/ai-access/client-keys` | 无 | 201，刚签发的密钥 | 401 / 403 / 404 / 409 / 500 / 503 |
| `POST …/ai-access/client-keys/{client_id}/revoke` | 无 | 200，已吊销的密钥 | 401 / 403 / 404 / 409 / 500 / 503 |

只有 ADMIN 能调。`customer_id`、`project_id` 是公开 ID；客户不存在为 `CUSTOMER_NOT_FOUND`，项目不存在或不属于客户为 `PROJECT_NOT_FOUND`，不存在或不属于项目的 `client_id` 为 `AI_CLIENT_KEY_NOT_FOUND`。所有成功响应走 §107 信封。

列表按 `created_at` 倒序，同一秒按内部创建顺序倒序；每项仅含 `client_id`、`status`（`ACTIVE` 或 `REVOKED`）、`created_at`、`revoked_at`。签发响应仅这一次额外含 256 位随机 `token`；响应带 `Cache-Control: no-store`，列表和吊销响应绝不返回 token。每项目最多两把 `ACTIVE` 密钥，达到上限为 409 `AI_CLIENT_KEY_LIMIT`；吊销已吊销密钥为 409 `AI_CLIENT_KEY_REVOKED`。

AI 密钥须等 AIH-TASK-068 合并后才签发，见设计 §8「部署顺序」。

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
- 状态 webhook 的维度 `status_webhook`（AIH-TASK-052，设计闸门 #241 v6 §2「健康告警维度」）接在它们之后，三类问题合在
  一行、级别取最严重的那类；摘要只有三个十进制计数，不含租户与项目 id。
- AI 用量 outbox 维度 `ai_usage_outbox`（AIH-TASK-066，设计闸门 #281 v23 §2「告警维度」）接在其后；摘要只有计数与秒数，
  不含租户、项目或事件 id。

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
| `status_webhook`（AIH-TASK-052） | 只统计 `integration_status = ENABLED` 且设了[状态 webhook 地址](#管理端项目集成状态与状态-webhook-地址)的项目（停用或没有地址的项目不推送，一律不计）。三个计数：**`failing`** = 投递落后（`delivered_version` 为空或小于客户的 `status_version`）且连续失败 ≥ 6 次（按默认退避约 15 分钟）的项目数；**`missing secret`** = 没有 `ACTIVE` [签名密钥](#管理端出站-webhook-签名密钥)的项目数（由数据推导，不看错误码）；**`signing error`** = 投递落后且最近一次因 `SECRET_DECRYPT_FAILED`（密钥解不开）或 `ENCRYPTION_NOT_CONFIGURED`（主密钥未配置）未发送的项目数。级别：`signing error > 0` 为 `P1`（加密配置坏了）；否则 `failing > 0` 或 `missing secret > 0` 为 `P2`；否则 OK | `P1` / `P2`：`failing 1, missing secret 0, signing error 2`（三个都是十进制整数，不做单复数变化）；OK：`no status webhook problem` |
| `ai_usage_outbox`（AIH-TASK-066） | 未处理的 `DEAD`（`dead_acked_at` 为空）、`HELD`，或待复核（`review_required_at` 非空且 `reviewed_at` 为空）任一计数 > 0：`P1`；否则最老 `PENDING` / `SENDING` 行的 `created_at` 超过 `BILLING_AI_PENDING_ALERT_SECONDS`（默认 900 秒）：`P2`；否则 OK | `P1` / `P2` / OK：`dead 1, held 0, review required 0, oldest pending 901 seconds` |

---

## 集成：有效 AI 服务状态

`GET /api/v1/integration/effective-status` 使用与用量摄取相同的五个 `X-Acuven-*`
HMAC 请求头；签名覆盖 `GET`、完整路径、时间戳、唯一请求 ID 和空请求体的 SHA-256。
请求体必须为空。成功返回 `data`：

```json
{
  "tenant_id": "租户 public_id",
  "project_id": "项目 public_id",
  "billing_mode": "INTERNAL_METERED_ONLY",
  "billing_status": "SUSPENDED",
  "account_status": "ENABLED",
  "effective_status": "ALLOW_AI",
  "status_version": 1,
  "project_integration_status": "ENABLED",
  "reason_code": null,
  "effective_at": "2026-10-09T10:10:00"
}
```

前七个字段的名字与取值不变（不改成 spec §30 示例里的 `tenant_account_status` 等写法）；后三个是 AIH-TASK-050 加的
（设计闸门 #241 v6 §2「接口」），只增加字段，现有解析不受影响：

| 字段 | 说明 |
| --- | --- |
| `project_integration_status` | 凭据所属项目的集成状态：`ENABLED` / `DISABLED` |
| `reason_code` | `BLOCK_AI` 的原因（见下表）；`ALLOW_AI` 时为 `null`。⚠️ 只给集成方后端，**不得展示给终端用户**（spec §26） |
| `effective_at` | 最近一次状态跃迁的时刻；旧数据没有记录时取客户的创建时刻。格式见[时间](#时间) |

`effective_status` 与 `reason_code` 由同一个函数合成，判定顺序固定，第一条命中即 `BLOCK_AI`：

| 顺序 | 条件 | `reason_code` |
| --- | --- | --- |
| 1 | `account_status = CLOSED` | `ACCOUNT_CLOSED` |
| 2 | `account_status = DISABLED` | `ACCOUNT_DISABLED` |
| 3 | `account_status = PENDING_ACTIVATION` | `ACCOUNT_PENDING_ACTIVATION` |
| 4 | `project_integration_status = DISABLED` | `PROJECT_INTEGRATION_DISABLED` |
| 5 | `billing_mode = PREPAID` 且 `billing_status ≠ ACTIVE` | `BALANCE_NOT_POSITIVE` |
| — | 以上都不命中 | `ALLOW_AI`，`reason_code = null` |

管理员动作（1–4）排在余额（5）前面：充值后集成方看到的仍是管理员停用的原因，不会误以为充值能解封。

身份由签名凭据决定，请求方不可代填租户或项目。内部模式仅豁免余额导致的暂停；管理员禁用
（账户或项目）仍返回 `BLOCK_AI`。本接口也是集成方[周期对账](#对账)用的接口，版本规则与状态 webhook 相同。集成方须验证返回的租户、项目和版本；只有经校验的 `BLOCK_AI` 才拦截新的模型调用，查询失败或应答无法校验时照常放行（不变量 1：中心计费故障不得中断客户 AI 服务），用量仍写入集成方本地 outbox，恢复后补送（[设计](design/INTERNAL-METERED-ONLY-2026-10-08.md)）。

## 集成：用量事件摄取

设计依据：设计闸门 #176（[AIH-TASK-029-usage-ingest.md](design/AIH-TASK-029-usage-ingest.md) v8 第 2 节）；
可选 `feature` 依据设计闸门 #281 `APPROVED: design v23`。
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
| `feature` | 否 | 可选功能标签；`null` 等同不带。`^[a-z][a-z0-9_]{0,31}(\.[a-z][a-z0-9_]{0,31}){1,3}$` 且最长 64 字符，例如 `crm.contact_summary`。只用于统计，不参与计价 |
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
`api_key` 的新 `key_version`（轮换重叠期内）重发同一事件是合法重复，不是冲突。成功响应不带 `retryable`。规范对象只在事件带非空
`feature` 时加入该键，因此不带或 `feature: null` 的历史指纹保持不变；同一 `event_id` 的 `feature` 有无或值不同会造成
`IDEMPOTENCY_CONFLICT`。

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

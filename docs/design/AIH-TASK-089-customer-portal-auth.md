# AIH-TASK-089 到 097 设计：客户门户认证（已批准 v10）

> **来源**：设计闸门 Issue #368。本文件是 `APPROVED: design v10` 那一版正文的**逐字副本**（审查：v1–v5 由 Codex，v6–v10 在 Codex 额度用完期间由 Claude Code 审查；送审时状态行为 `READY_FOR_REVIEW`，批准后 Issue 上只把状态行改成了 `APPROVED: design v10`）。
> 放进仓库，是因为 OpenClaw Worker 在沙箱里不联网、读不到 GitHub Issue。
> **实现以本文件为准**；与 Issue 不一致时，以 Issue #368 上被批准的 v10 为准。设计要改，就回到 Issue 升版本、重新过闸门，不要直接改本文件。
> **前置**：设计闸门 #369（[AIH-TASK-086-session-revocation.md](AIH-TASK-086-session-revocation.md)）的实现 `AIH-TASK-086`、管理员 PR、`AIH-TASK-087`、`AIH-TASK-088` 必须先全部合并。
> §11 的五个实现任务登记成九个编号（任务 B、D 各拆成两个，E 拆成三个，只为让每个任务的规模与可改路径符合登记规则；拆分不改变范围、顺序约束与验收）：
> `AIH-TASK-089`（A 数据库）→ `090`（B 的一半：登录状态判断、2FA 注册要求 `ACTIVE`、写权限锁、配置与审计动作）→ `091`（B 的另一半：门户账号服务）→ `092`（C 接受邀请页）→ `093`（D 的前一半：接受邀请、`/api/v1/me`、`/2fa/self/*`、`/customer/me` 与客户鉴权依赖）→ `094`（D 的后一半：管理端门户账号六个接口与邀请邮件）→ `095`（E：管理端门户账号面板）→ `096`（E：按角色分流与门户外壳）→ `097`（E：门户首页与开启 2FA）。
> 设计 §8 的顺序约束照旧成立：接受邀请页（092）与接受邀请接口（093）都先于能发出邀请的 094 上线。
> 设计 §2「邀请邮件与重置的隔离」要求 `reset_password` 只认 `PASSWORD_RESET` 用途：该文件名含 password、Worker 不能改，由管理员在 `AIH-TASK-089` 合并之后、`091` 开启之前以 PR 完成。

---

## 0. 准入判定

| 任务涉及 | 要填的范围 |
| --- | --- |
| 钱包 / 账本 / 定价 / 汇率 / 支付 / 幂等 / 状态机 | **全部章节** |
| 用量摄取 / 认证与会话（集成侧与平台侧）/ Webhook | §1–§7（**§6 不可省**） |
| 前端 / 文档 / CI / 脚本 | **不走本闸门** |
| 既不碰钱、也不碰用量摄取 / 认证与会话 / Webhook 的后端改动 | **不走本闸门** |

本任务档位：`全部`

本任务属于「认证与会话」，同时新增了一个状态机（门户账号的 `INVITED` → `ACTIVE` ⇄ `DISABLED`），还有一次表结构迁移，所以按最高档填全部章节。不碰钱：不写钱包、账本、定价、支付。

---

状态：`READY_FOR_REVIEW`
负责人：Claude
**设计版本**：`v10`
对应需求：前置设计闸门 #369（改密码吊销全部会话，须先实现）；spec §51、§52、§53、§54、§66、§88、§90、§96、§97、§112、§127（Customer authentication）；`REQ-PRIV-001`；AIH-TASK-020 设计 #136 v2 §2 末尾的账户状态契约
目标 PR：待开（实现拆成五个任务，见 §11「实现拆分」）

## 1. 目标与边界

**要解决的问题**
Phase 4 的客户门户（仪表盘、钱包、充值、收据）需要客户能登录。今天做不到，原因有三：
- `users` 没有指向租户的列
- 产品代码里唯一能建用户的入口是 `create-admin` CLI，只建 ADMIN
- 没有任何「调用方是客户、属于哪个租户、能不能写」的后端判定

**可观察的完成标准**
1. 管理员在某个客户名下开通门户账号，填写登录邮箱。系统建出一个 `INVITED` 的 CUSTOMER 用户，并发出邀请邮件
2. 客户点邀请链接、设置密码后，账号变为 `ACTIVE`，可以用现有的 `/api/v1/auth/login` 登录
3. 客户登录后：
   - 调 `/api/v1/admin/*` 一律 403 `ADMIN_REQUIRED`
   - 调 `/api/v1/customer/*`，租户只取自会话
   - 管理员调 `/api/v1/customer/*` 一律 403 `CUSTOMER_REQUIRED`
4. 租户 `account_status` 为 `DISABLED` 或 `CLOSED` 时，客户仍能登录，但所有客户侧写接口返回 403 `ACCOUNT_READ_ONLY`。`PENDING_ACTIVATION` 与 `ENABLED` 一样，可以完整使用
5. 客户可以自己开启 TOTP 2FA（可选）。开启后登录走现有的 `TOTP_REQUIRED` 流程。另有配置开关可以改为强制
6. 管理员可以：查看门户账号状态、重发邀请（只在 `INVITED` 时，可同时更正邮箱）、停用、重新启用。每个动作都有审计
7. 忘记密码 / 重置密码对 `ACTIVE` 的客户照常可用

**明确不做什么**
- 客户自助注册（Kelvin 2026-10-19）
- 一个客户多个门户用户、客户侧角色权限（spec §52、§121）
- `ACTIVE` 账号修改登录邮箱：客户换人或改邮箱，V1 由管理员停用后联系处理，另开任务（见 §10）
- 客户自己关闭 2FA：要新的安全流程，另开任务（管理员重置客户 2FA 在本设计内，见 §2）
- 管理员重置 ADMIN 账号的 2FA：不在本设计内
- 门户的业务页面（仪表盘、钱包、充值、收据）：各自另开任务
- 客户侧隐私请求的自助 UI（spec §112.1 允许推迟）
- 改用事务邮件 API 服务：沿用 ADR-0014 的 SMTP（见 §10 假设 A1）

**现有行为与问题证据**
- `app/models/auth.py`：`UserRole.CUSTOMER` 存在，但注释写明「CUSTOMER 现在建不出来」。`User` 没有 `tenant_id`
- `app/cli.py`：`create-admin` 是唯一构造 `User` 的产品代码
- `app/services/auth.py` 登录流程只拦 `UserStatus.DISABLED`（`if locked or user.status is UserStatus.DISABLED`），刷新和 2FA 两处也是同样的写法。加入新状态 `INVITED` 后，这三处如果不改成「不是 `ACTIVE` 就拒」，就会让未激活的账号通过状态检查。本设计把它们一起改掉（§6、§7）
- `app/services/auth.py`：未启用 2FA 的 CUSTOMER 直接签发会话，注释说「是否强制由配置决定（Phase 4 的事）」
- `require_admin` 已经能拒绝非 ADMIN（`tests/backend/test_admin_customers_api.py` 的路由枚举用例造过 CUSTOMER 并断言 403）。客户侧还没有对应的依赖
- 登录响应（`LoginResponse`）不带角色，前端无从按角色跳转

## 2. 设计概要

### 数据流

```text
管理员 POST /admin/customers/{id}/portal-account {email}
  → require_admin → 锁租户行 → 校验（租户非 CLOSED、该租户无门户账号、邮箱未被任何用户占用）
  → 同一事务：插入 users（CUSTOMER, INVITED, tenant_id, 不可用密码哈希）
              + password_reset_tokens（purpose=INVITATION, TTL 72h）
              + domain_outbox（PORTAL_INVITATION_REQUESTED, {to, token, expires_at}）
              + audit PORTAL_ACCOUNT_CREATED
  → 201
  → outbox worker 渲染邀请邮件 → SMTP（ADR-0014）

客户 POST /auth/invitation/accept {token, password}
  → 锁租户行 → 锁 user 行 → 重读令牌判有效 → 看锁住的租户非 CLOSED → 强度校验 → 条件 UPDATE 消费 INVITATION 令牌（顺序见 §4「接受邀请的判定顺序」）
  → 同一事务：写密码哈希、status INVITED→ACTIVE、activated_at、作废该用户其余未用邀请令牌、audit PORTAL_INVITATION_ACCEPTED
  → 200（不自动登录，前端转登录页）

客户 POST /auth/login（现有）→ 现有锁定 / 限流 / 2FA 流程 → 访问令牌 + 刷新 cookie
客户 GET /customer/me → require_customer（读库：role=CUSTOMER、ACTIVE、tenant_id）→ 租户摘要 + read_only
以后的客户写接口 → require_customer_writable → 租户 account_status ∈ {DISABLED, CLOSED} 时 403 ACCOUNT_READ_ONLY
```

### 数据库（迁移 0025，接在 #369 的 `0024_password_version` 之后）

`users`：
- `tenant_id BIGINT NULL`：外键 `fk_users_tenant` 指向 `tenants.id`，`ON DELETE RESTRICT`（租户本来就只停用、不删除）
- `activated_at DATETIME NULL`：客户第一次接受邀请的时刻，用来区分「重新启用」后应回到 `INVITED` 还是 `ACTIVE`
- `UNIQUE (tenant_id)`，名为 `uq_users_tenant`
  - 这是 spec §52「一个客户一个门户账号」的数据库兜底
  - ADMIN 的 `tenant_id` 为 NULL，MySQL 和 SQLite 的唯一索引都允许多个 NULL
- `CHECK ck_users_admin_no_tenant`：`role = 'CUSTOMER' OR tenant_id IS NULL`，即 ADMIN 一定不属于任何租户
  - 「CUSTOMER 一定属于某个租户」**不**写成数据库约束，理由与代价见 §9 备选 F。由两处保证：
    - 唯一的创建入口（门户账号开通）总是写 `tenant_id`，之后没有任何接口能改它
    - `require_customer` 遇到 `tenant_id` 为空的 CUSTOMER 一律 403 `CUSTOMER_REQUIRED`，不放行到任何客户接口
- `CHECK ck_users_status`：`status IN ('ACTIVE', 'INVITED', 'DISABLED')`。现在没有这条 CHECK，补上
- `CHECK ck_users_invited_customer`：`status <> 'INVITED' OR role = 'CUSTOMER'`
- `UserStatus` 加 `INVITED`。枚举列宽固定 64，加值本身不需要改列宽

`password_reset_tokens`：
- `purpose VARCHAR(32) NOT NULL DEFAULT 'PASSWORD_RESET'`，`CHECK IN ('PASSWORD_RESET', 'INVITATION')`。已有行回填为 `PASSWORD_RESET`
- 索引不变。按令牌查找走 `token_hash` 的唯一索引，按用户作废走现有的 `ix_password_reset_tokens_user (user_id, used_at)`；它同时是 `user_id` 外键在 MySQL 上依赖的索引，不重建
- 选择复用这张表而不是新建一张，理由见 §9

### API

**认证（`/api/v1/auth`，nginx 的认证限流区覆盖）**

| 方法 | 路径 | 主体 | 请求 | 成功 | 错误 |
| --- | --- | --- | --- | --- | --- |
| POST | `/invitation/accept` | 匿名，凭令牌 | `{token, password}` | 200 `{}` | 400 `INVALID_INVITATION_TOKEN`（令牌不存在、已用、过期、用途不对，或账号不是 `INVITED`，统一一个码）；409 `ACCOUNT_CLOSED`（租户已关户）；400 `WEAK_PASSWORD`（密码强度，与重置同一个错误）。判定顺序见 §4 |
| POST | `/2fa/self/enrol` | `require_customer` + 重验密码 | `{password}` | 200 `{secret, otpauth_uri}`（只返回一次） | 401 `INVALID_CREDENTIALS`（密码错）；403 `CUSTOMER_REQUIRED`；409 已启用 |
| POST | `/2fa/self/confirm` | `require_customer` | `{code}` | 200 `{recovery_codes}`（只返回一次） | 401 `INVALID_TOTP`（验证码错，沿用 `confirm_enrolment` 的现有错误）；403；409 |

**管理端（`/api/v1/admin/customers/{customer_id}/portal-account`，全部 `require_admin`）**

| 方法 | 路径后缀 | 请求 | 成功 | 错误 |
| --- | --- | --- | --- | --- |
| GET | — | — | 200 `{email, status, invited_at, invitation_expires_at, activated_at, last_login_at, two_factor_enabled}` | 404 `CUSTOMER_NOT_FOUND` / `PORTAL_ACCOUNT_NOT_FOUND` |
| POST | — | `{email}` | 201，同 GET | 409 `PORTAL_ACCOUNT_EXISTS` / `EMAIL_IN_USE` / `ACCOUNT_CLOSED`；422 邮箱格式 |
| POST | `/resend-invitation` | `{email?}` | 200，同 GET | 409 `PORTAL_ACCOUNT_NOT_INVITED`（不在 `INVITED`）/ `EMAIL_IN_USE` / `ACCOUNT_CLOSED` |
| POST | `/disable` | — | 200，同 GET | 409 `PORTAL_ACCOUNT_ALREADY_DISABLED` |
| POST | `/enable` | — | 200，同 GET | 409 `PORTAL_ACCOUNT_NOT_DISABLED` / `ACCOUNT_CLOSED` |
| POST | `/reset-2fa` | `{reason}`（必填，1–255 个字符，与现有写审计原因的接口共用同一个 `_Reason` 约束；`audit_logs.reason` 是 `String(255)`） | 200，同 GET（`two_factor_enabled` 变为 false） | 409 `PORTAL_2FA_NOT_ENABLED`（该账号没有已确认的 2FA，也没有待确认的注册）；422 原因为空或过长 |

**客户端（`/api/v1/customer`）**

| 方法 | 路径 | 依赖 | 请求 | 成功 | 错误 |
| --- | --- | --- | --- | --- | --- |
| GET | `/me` | `require_customer` | — | 200 `{customer_id, company_name, account_status, read_only}` | 401 / 403 `CUSTOMER_REQUIRED` |

**当前用户（`GET /api/v1/me`，任一已登录用户）**：200 `{role, email, two_factor_enabled}`；401 `TOKEN_INVALID`。
- 刻意**不**放在 `/api/v1/auth/` 下：前端对那个前缀下的 401 不触发刷新，访问令牌一过期，每次打开页面都会被直接踢回登录；而且那个前缀受登录限流（nginx 每分钟 10 次），每次进页面都调它会撞上限流。放在 `/api/v1/me`，过期时照常走刷新，也不进认证限流区

- `customer_id` 是租户的 `public_id`。客户请求里**不接受**任何租户标识（spec §90）
- **管理端 GET 的两个邀请字段**没有专门的列，取自该用户最新一行 `purpose = 'INVITATION'` 的令牌（按 `created_at`、再按 `id` 取最大）：
  - `invited_at` = 那一行的 `created_at`；从未发过邀请时为 null
  - `invitation_expires_at` = 账号是 `INVITED` 且那一行未用、未过期时取它的 `expires_at`，否则为 null（已激活、已停用、重新启用后旧令牌已作废，都是 null，表示需要重发）
- **客户开启 2FA 的两个接口放在 `/api/v1/auth/2fa/self/` 下**，不放在 `/api/v1/customer/` 下：
  - 这样落在 nginx 的认证限流区（重验密码是一个可以拿来猜密码的入口）；进程内令牌桶只在处理函数显式调用 `_rate_limit(request)` 时生效，所以 `/2fa/self/enrol` 的处理函数第一步就调用它（`/2fa/recovery-codes` 现在没有调用，不在本设计范围）
  - 前端对 `/api/v1/auth/` 下的 401 不触发令牌刷新（`frontend/src/api/client.ts`），密码错、验证码错的 401 是业务结果，可以直接沿用现有错误码
  - 重验密码失败的处理照现有 `/2fa/recovery-codes` 的先例：返回 401 `INVALID_CREDENTIALS`，不计入登录锁定、不写审计（调用方已持有有效访问令牌）；成功开启时由 `confirm_enrolment` 照常写 `TWO_FACTOR_ENABLED` 审计
  - 只允许 CUSTOMER（ADMIN 在登录时强制注册，不走这里）；复用现有的 `start_enrolment` / `confirm_enrolment` 服务，不另写一套；这两个服务本身按「登录流程的改动」一节加上状态检查，按 #369 加上版本号复查
  - 只读状态下仍然允许：保护账号的操作不应被账户停用挡住

### 依赖（`app/api/auth.py`）

- `require_customer(request) -> CustomerPrincipal(user, tenant_id)`
  - 先 `require_current_user`（读库、要求 `ACTIVE`），再要求 `role is CUSTOMER` 且 `tenant_id` 不为空，否则 403 `CUSTOMER_REQUIRED`
  - `tenant_id` 取自库里的 user 行，不取自令牌
- `require_customer_writable(request) -> CustomerPrincipal`
  - 在上面的基础上读租户 `account_status`，`DISABLED` 或 `CLOSED` 时 403 `ACCOUNT_READ_ONLY`
  - ⚠️ 它只是**提前拒绝**，不是保证：它读完状态就结束了事务，之后管理员仍可能把租户改成 `DISABLED`
- `lock_tenant_for_customer_write(session, tenant_id) -> Tenant`（`app/services/account_status.py`，与改账户状态的代码放在一起）
  - 在调用方的写事务里 `SELECT tenants … FOR UPDATE` 锁租户行并重查 `account_status`，`DISABLED` 或 `CLOSED` 时抛 `ACCOUNT_READ_ONLY`，否则返回锁住的租户
  - 改账户状态（`change_account_status`）同样先锁这一行，所以两者排队：写先拿到锁就在停用之前完成，停用先提交则写入方锁到后读到 `DISABLED` 并被拒
  - **以后每个客户写操作（充值等）都必须在自己的写事务里先调用它**，`require_customer_writable` 只省掉一次明显无望的请求。这条要求写进 `docs/api.md` 的客户接口约定，后续写接口的设计闸门要引用它
  - 本设计没有客户写接口，交付依赖、辅助函数与测试（§7「写权限并发」用一个测试专用的写操作验证）
- 与 `require_admin` 一样，每个客户处理函数都**显式**调用依赖，由路由枚举用例兜底（§7）

### 登录流程的改动（`app/services/auth.py`）

- 三处状态判断从 `status is DISABLED` 改为 `status is not ACTIVE`：登录、TOTP 第二步、刷新
- 2FA 注册的两个服务函数 `start_enrolment`、`confirm_enrolment`（`app/services/two_factor.py`）在各自的事务里先 `SELECT users … FOR UPDATE`，用户不是 `ACTIVE` 时抛 401 `TOKEN_INVALID`，不写任何 2FA 行
  - 现在它们只凭 pending 令牌就动手，不看用户状态。登录时拿到 `ENROL_2FA` 令牌（ADMIN，或 `customer_2fa_required` 为 True 时的 CUSTOMER）之后被停用的账号，在令牌 600 秒有效期内仍能注册自己的验证器；重新启用时就可能被一个管理员不知道的验证器锁在门外，而本设计不提供重置 2FA
  - 锁用户行让它与停用（锁租户行、再锁用户行）排队：停用先提交则注册被拒；注册先提交，停用照常生效，之后的登录被状态检查挡住
  - `/2fa/self/*` 两个接口走同样的服务，所以 `require_customer` 之后才被停用的竞态也被这一步挡住
  - 审计里的 `LOGIN_FAILED` reason 对 `INVITED` 记 `NOT_ACTIVE`，对外仍是 401 `INVALID_CREDENTIALS`
- 新配置 `customer_2fa_required: bool = False`。为 True 时，未注册 2FA 的 CUSTOMER 和 ADMIN 一样拿到 `ENROL_2FA` 阶段
- 登录本身**不看**租户 `account_status`：四种状态都能登录，只读限制在写接口那一层（020 契约）

### 改密码让旧会话失效（依赖设计闸门 #369）

改密码与会话吊销由前置设计 #369「改密码吊销全部会话：密码版本号」统一处理，本设计只做两件事：

- 接受邀请是「由本人设置新密码」的入口之一：在 §4 判定顺序第 7 步写密码哈希的同一事务里 `password_version + 1`（#369 §2 的改密码点）
- `/2fa/self/enrol`、`/2fa/self/confirm` 调用 `start_enrolment`、`confirm_enrolment` 时，把所用访问令牌的版本号传给服务，由服务在锁住用户行后复查（#369 §2 使用点表）

#369 保证：改密码一旦提交，之前用旧密码换来的访问令牌、pending 令牌、刷新令牌一律无效，2FA 注册、确认与重新生成恢复码不能用旧会话完成。本设计的全部实现任务排在 #369 的实现之后（§11）。

### 管理员重置客户 2FA（恢复路径）

客户可以自己开启 2FA，就必须有恢复的路：客户丢了手机和恢复码；或者攻击者拿到还没开 2FA 的客户的密码，抢先注册了自己的验证器（主人重置密码后登录仍要 TOTP）。两种情况下，没有这条路门户账号就永久不可用。

- 只针对门户账号（CUSTOMER）；ADMIN 的 2FA 不在本设计内
- 管理员必须先在线下核实对方确实是该客户（runbook 写明核实方式：用客户的登记联系电话回拨或当面确认，不凭邮件或即时消息里的自称），再在管理端执行，填写原因
- 效果：删除该用户的 `two_factor_settings` 行（无论已确认还是待确认）与全部恢复码；`password_version + 1`，按 #369 吊销该用户全部会话（攻击者手里的会话一并失效）；写审计 `PORTAL_2FA_RESET`
- 之后客户用密码登录（未开启强制时直接进入，开启强制时走 `ENROL_2FA`），可以重新开启 2FA。若密码也可能已泄露，管理员同时提示客户先走「忘记密码」
- 不发通知邮件：通知体系归 Phase 6，本设计不新增第二类邮件；审计与管理员的线下沟通是这一步的记录

### 邀请邮件与重置的隔离

- 现有 `reset_password` 查令牌时加上 `purpose = 'PASSWORD_RESET'`，`accept_invitation` 只认 `purpose = 'INVITATION'`。两种令牌互相用不了
- `request_reset` 本来就只给 `ACTIVE` 用户发，`INVITED` 的客户点「忘记密码」仍返回同一个 200、不发信。没收到邀请就由管理员重发，不开第二条通道
- 邀请邮件：outbox 事件 `PORTAL_INVITATION_REQUESTED`，payload 是 `{to, token, expires_at}`，链接是 `{frontend_base_url}/accept-invitation?token=…`
  - `expires_at` 是写入令牌行时的同一个值（不带时区的 UTC，**截到整秒**：取 `now` 去掉微秒再加有效期，因为 MySQL 的 `DATETIME` 只存到秒，不截就会出现「信里的时刻与库里差几分之一秒」；ISO 8601 字符串），渲染时只用它，不读当前配置。这样 outbox 重试、配置改动之后，信里写的到期时刻仍然和令牌真实的到期时刻一致
  - 文案写明到期时刻：马来西亚时间（UTC+8，固定偏移，不依赖 tzdata）精确到分钟，后面括号附 UTC；另提示「没收到请查垃圾箱」。不写「72 小时」这类相对时长
  - 链接路径由前端 `routes/paths.ts` 登记，并纳入现有「后端拼的链接对得上前端路由」的守卫

### 事务边界

- 开通、重发、停用、启用：各自一个事务。先 `SELECT … FOR UPDATE` 锁租户行，再锁门户用户行（如果有）。业务写入、令牌、outbox、审计同一事务提交
- 接受邀请：一个事务。先 `SELECT tenants … FOR UPDATE` 锁租户行，再锁 user 行，再加锁重读并条件 UPDATE 消费令牌（以受影响行数判定，与现有重置同一手法）；租户状态看锁住的那一行，不用普通读（§4「接受邀请的判定顺序」）
- 重置客户 2FA：一个事务。锁租户行 → 锁 user 行 → 删除 2FA 设置与恢复码 → `password_version + 1` → 审计

### 外部系统与异步

只有邮件，走 domain outbox 和现有投递任务（Invariant 13/14）。邮件发不出去不影响账号状态，管理员可以重发。

### 时间语义

全部是不带时区的 UTC。令牌有效期以写入时的 `expires_at` 为准，不在校验时按配置重算。

### 金额

不涉及。

## 3. 不变量影响矩阵

| 不变量 | 是否触碰 | 可能怎样被破坏 | 设计控制 | 验证方式 |
| --- | --- | --- | --- | --- |
| INV-1 中心故障不中断 AI | 否 | 门户认证与 AI 调用路径无关：AI 模块用自己的 AI 密钥鉴权（AIH-TASK-058），不经 `users` | — | 不改 AI 模块文件，`check_repo_policy` 的边界检查照常 |
| INV-2 事件不重复扣费 | 否 | 不写账本 | — | — |
| INV-3 支付不重复入账 | 否 | 本设计没有支付 | 以后的充值接口用 `require_customer_writable`，入账规则归支付闸门 | — |
| INV-4 余额只经账本变动 | 否 | 不改余额 | — | — |
| INV-5 历史账本不可变 | 否 | 不碰 `wallet_transactions` | — | — |
| INV-6 事件保留版本引用 | 否 | 不碰用量与定价 | — | — |
| INV-7 客户不可见成本毛利 | **是** | 客户令牌调到管理端接口，就能看到成本与毛利（用量分析、用量事件等） | `require_admin` 对 CUSTOMER 一律 403；`/customer/me` 只返回四个字段，没有任何金额 | §7 路由枚举：CUSTOMER 令牌调全部 `/api/v1/admin/*` 都是 403；`/customer/me` 的响应键集合逐字断言 |
| INV-8 租户不可互访 | **是** | ① 客户接口信任请求里的租户 id；② 一个用户挂到别的租户；③ 同一租户出现两个门户账号 | ① 租户只取自库里的 user 行；② `tenant_id` 只在开通时写，之后没有任何接口能改；③ `uq_users_tenant` 加锁租户行 | §7 越权用例：A 的令牌带 B 的 id 作查询参数，返回的仍是 A；并发开通只成功一个 |
| INV-9 对话内容不入库 | 否 | 不处理 prompt / response | — | — |
| INV-10 金额用 Decimal | 否 | 无金额 | — | — |
| INV-11 event_id 至多一次财务效果 | 否 | 无用量事件 | — | — |
| INV-12 定稿对账单不可变 | 否 | 无对账单 | — | — |
| INV-13 状态与事件原子提交 | **是** | 账号已建好、邀请 outbox 没写（或反过来），管理员以为发了信，其实没发 | 用户行、令牌、outbox、审计同一事务 | §7 事务中途失败：在 outbox 插入时注入异常，四张表都没有新行 |
| INV-14 队列丢失不毁持久工作 | **是** | Celery 丢任务，邀请信永远不发 | outbox 是持久的，现有周期恢复会重投；GET 接口显示 `invited_at`，管理员能看出来并重发 | 复用现有 outbox 恢复用例；§7 加一条「worker 不跑时 outbox 行仍在 `PENDING`」 |

## 4. 状态与并发

### 门户账号状态（`users.status`，只对 CUSTOMER；ADMIN 仍只有 `ACTIVE` / `DISABLED`，由 CHECK 约束）

| 当前状态 | 事件 | 前置条件 | 新状态 | 副作用 | 非法时结果 |
| --- | --- | --- | --- | --- | --- |
| （无） | 管理员开通 | 租户存在且不是 `CLOSED`；该租户没有门户账号；邮箱没有被任何用户占用 | `INVITED` | 新邀请令牌、outbox、审计 `PORTAL_ACCOUNT_CREATED` | 409 `PORTAL_ACCOUNT_EXISTS` / `EMAIL_IN_USE` / `ACCOUNT_CLOSED` |
| `INVITED` | 管理员重发（可改邮箱） | 租户不是 `CLOSED`；给了新邮箱且归一后与当前邮箱不同时，新邮箱没有被任何用户占用 | `INVITED` | 作废该用户全部未用邀请令牌，签发新令牌、outbox，审计 `PORTAL_INVITATION_RESENT` | 409 `PORTAL_ACCOUNT_NOT_INVITED` / `EMAIL_IN_USE` / `ACCOUNT_CLOSED` |
| `INVITED` | 客户接受邀请 | 令牌是 `INVITATION` 用途、未用、未过期；租户不是 `CLOSED`；密码强度合格 | `ACTIVE` | 写密码哈希和 `activated_at`，作废其余邀请令牌，审计 `PORTAL_INVITATION_ACCEPTED` | 400 `INVALID_INVITATION_TOKEN` / 409 `ACCOUNT_CLOSED` / 400 `WEAK_PASSWORD` |
| `INVITED` / `ACTIVE` | 管理员停用 | — | `DISABLED` | 吊销全部刷新令牌，作废全部未用令牌（两种用途都作废），`password_version + 1`（#369：停用前签发的访问令牌与 pending 令牌永久失效，之后重新启用也不会复活它们），审计 `PORTAL_ACCOUNT_DISABLED` | 409 `PORTAL_ACCOUNT_ALREADY_DISABLED` |
| `DISABLED` | 管理员启用 | 租户不是 `CLOSED` | `activated_at` 为空回到 `INVITED`，否则回到 `ACTIVE` | 审计 `PORTAL_ACCOUNT_ENABLED`。不自动重发邀请，需要时管理员再点重发 | 409 `PORTAL_ACCOUNT_NOT_DISABLED` / `ACCOUNT_CLOSED` |
| `ACTIVE` / `DISABLED` | 管理员重置 2FA | 该账号有已确认或待确认的 2FA；必须写原因 | 不变 | 删除 `two_factor_settings` 行与全部恢复码；`password_version + 1`（#369，吊销该用户全部会话）；审计 `PORTAL_2FA_RESET`（`reason` 原样记入，`after_state` 只放 `tenant_public_id`）| 409 `PORTAL_2FA_NOT_ENABLED` |

- 停用对已发出的访问令牌立即生效：`require_current_user` 每次都读库，要求 `ACTIVE`
- 租户的 `account_status` 与门户账号状态互相独立。租户停用不改门户账号，只让写接口变成只读

### 重发时的邮箱比较

- 请求里的 `email` 先按登录同一规则归一（去首尾空白、转小写），再与当前邮箱比较
- 没给 `email`，或归一后与当前邮箱相同：不算改邮箱，不做占用检查，审计 `email_changed: false`
- 归一后不同：新邮箱被任何用户占用时 409 `EMAIL_IN_USE`；否则改邮箱，审计 `email_changed: true`

### 接受邀请的判定顺序（照现有 `reset_password` 的写法）

1. 按 `token_hash` 不加锁读出令牌行，取 `user_id`；再不加锁读出该用户的 `tenant_id`（创建后不会变）。任一查不到 → 400 `INVALID_INVITATION_TOKEN`
2. `SELECT tenants … FOR UPDATE` 锁租户行，再 `SELECT users … FOR UPDATE` 锁用户行，再加锁重读令牌行（`populate_existing`）。加锁顺序「租户 → 用户 → 令牌」与管理端门户操作、改账户状态一致，不会成环；锁读总是读最新已提交版本，不受第 1 步可能建立的快照影响
3. 令牌不是 `INVITATION` 用途、已用、已过期（`expires_at <= now`），或用户不是 `INVITED` → 400 `INVALID_INVITATION_TOKEN`
4. 看锁住的租户行，`CLOSED` → 409 `ACCOUNT_CLOSED`
5. 密码强度不合格 → 400 `WEAK_PASSWORD`。排在消费令牌之前：弱密码不烧掉仍然有效的令牌，客户改个密码就能重试
6. 条件 UPDATE 消费令牌（`used_at IS NULL AND expires_at > now`），受影响行数为 0 → 400 `INVALID_INVITATION_TOKEN`
7. 写密码哈希、`password_version + 1`（#369）、`status = ACTIVE`、`activated_at`，作废该用户其余未用的邀请令牌，写审计，提交

这样错误码只取决于「链接还能不能用」和「租户状态」，不取决于密码的内容。

### 串行化

- 开通、重发、停用、启用：`SELECT tenants … FOR UPDATE` 串行化同一租户的门户操作
- 接受邀请：先锁租户行、再锁 user 行，再条件 UPDATE 消费令牌，与停用、重发、关户互斥。这样「停用」和「接受」同时到达时，结果是两种确定顺序之一：
  - 停用先提交：令牌已作废，接受返回 400
  - 接受先提交：停用把 `ACTIVE` 改成 `DISABLED`
- 「关户」与「接受」同理：关户先提交，接受拿到租户锁后读到 `CLOSED`，409；接受先提交，账号变为 `ACTIVE`，随后关户生效，账号只读

### 数据库唯一性

- `users.email` 唯一（已有）
- `uq_users_tenant`
- 令牌 `token_hash` 唯一（已有）
- 并发开通撞上唯一约束时不返回 500。映射办法不解析数据库的报错文本（两种方言的文本不同）：
  1. 捕获 `IntegrityError`，回滚
  2. 在新事务里查：该租户已有门户账号 → 409 `PORTAL_ACCOUNT_EXISTS`；否则这个邮箱已被任何用户占用 → 409 `EMAIL_IN_USE`
  3. 两者都不成立（理论上不该出现）→ 原样抛出，按 500 处理并记日志
  - 重发改邮箱撞上 `users.email` 唯一约束时同样处理，结果是 409 `EMAIL_IN_USE`

### 幂等与重放

- 开通不是幂等的：第二次调用返回 409 `PORTAL_ACCOUNT_EXISTS`，响应里不带邮箱，前端据此刷新状态
- 重发每次都换新令牌，旧链接立即失效
- 同一个邀请令牌提交两次：第二次受影响行数为 0，返回 400，且不覆盖第一次设置的密码

### 重试

只在 outbox 投递层重试（现有退避与死信）。所有 HTTP 接口都不在服务端自动重试。

## 5. 失败与恢复矩阵

| 失败点 | 对外结果 | 数据最终状态 | 是否可重试 | 恢复来源 | 告警 |
| --- | --- | --- | --- | --- | --- |
| 校验失败（邮箱格式、密码强度、状态不对） | 422 / 409 / 400，统一错误体 | 不写任何行 | 改正后可重试 | 调用方 | 无 |
| DB 提交失败（开通 / 重发 / 停用 / 启用） | 500 带 `request_id` | 整体回滚，没有半个账号 | 可原样重试，会得到确定的成功或 409 | 调用方 | 现有 5xx 日志 |
| DB 提交失败（重置 2FA） | 500 带 `request_id` | 整体回滚：2FA 设置、恢复码、版本号都不变，客户的会话照旧有效 | 可原样重试 | 管理员 | 现有 5xx 日志 |
| DB 提交失败（接受邀请） | 500 | 回滚：令牌未消费，账号仍是 `INVITED` | 可用同一链接重试 | 客户 | 同上 |
| 提交确认丢失（已提交、响应没回来） | 客户端看到网络错误 | 已提交 | 接受邀请重试会得到 400，前端提示「可能已设置，请直接登录」；管理端重试得到 409，刷新 GET 即可看到真实状态 | 调用方 | 无 |
| Redis/Celery 丢失 | 接口照常成功 | outbox 行留在 `PENDING` | 是 | 现有 outbox 周期恢复 | 现有 `outbox_backlog` 维度 |
| SMTP 超时或拒收 | 接口早已成功 | outbox 按退避重试，最终进死信 | 是 | 现有投递任务；管理员重发 | 现有 outbox 告警维度 |
| 邮件进垃圾箱（送达但看不见） | 无报错 | 账号停在 `INVITED`，`invited_at` 越来越旧 | — | 管理员看 GET 后人工跟进或重发；邮件正文也提示查垃圾箱 | 无（已知缺口，见 §10） |
| 客户丢失 2FA 或被他人抢先注册 2FA | 客户登录卡在 TOTP | 账号仍是 `ACTIVE`，2FA 属于错误的持有人或已不可用 | 是 | 管理员线下核实后「重置 2FA」，客户重新登录并可重新开启 | 无（客户联系 Acuven） |
| 重复请求（重复开通、重复接受、并发停用） | 见 §4 | 只有一次生效 | — | 数据库约束 + 条件 UPDATE | 无 |

## 6. 数据与安全边界

**租户过滤**
- 客户侧所有查询的租户条件来自 `CustomerPrincipal.tenant_id`。它取自库里的 user 行，令牌里的 claim 不用作授权依据
- 客户请求的路径、查询串、请求体都没有租户参数；即使带了也不读

**鉴权主体**
- 管理端门户账号接口：ADMIN
- 客户接口：处于 `ACTIVE` 状态的 CUSTOMER
- 接受邀请：只凭一次性令牌（256 位随机串，库里只存 SHA-256），不需要登录
- 角色和状态每个请求都读库，降级、停用立即生效

**客户侧禁止返回的字段**
- `/customer/me` 只返回 `customer_id`、`company_name`、`account_status`、`read_only` 四个字段：不返回任何金额、成本、毛利、内部 id、`billing_mode`
- `/api/v1/me` 不返回 `tenant_id`、内部用户 id、哈希、锁定信息

**日志、审计、异常**
- 以下内容绝不进日志、审计、异常文本或响应（spec §94）：
  - 密码
  - 邀请令牌和重置令牌原文
  - TOTP 密钥和 `otpauth_uri`
  - 恢复码
- 新审计动作：`PORTAL_ACCOUNT_CREATED`、`PORTAL_INVITATION_RESENT`、`PORTAL_INVITATION_ACCEPTED`、`PORTAL_ACCOUNT_DISABLED`、`PORTAL_ACCOUNT_ENABLED`、`PORTAL_2FA_RESET`
  - 实体是 `users`
  - `after_state` 只放 `tenant_public_id`、`status`；重发时另加 `email_changed: true/false`
  - **不放邮箱值**（与 `CUSTOMER_CREATE` 不记联系方式同一口径，`REQ-PRIV-001`）
  - 审计查询接口已有「用户类实体把 `entity_id` 显示为邮箱」的规则，只对 ADMIN 可见
- 客户的 `LOGIN` / `LOGIN_FAILED` 沿用现有写法，`actor_role=CUSTOMER`
- 邀请邮件 outbox 的 payload 含令牌原文，与现有重置邮件同一做法与同一风险。outbox 没有清理任务，这是已知缺口，记在 `docs/TODO.md`「密码重置与通知投递的几项加固」，本设计不扩大它

**密钥**
- 不新增密钥
- 客户的 TOTP 密钥沿用 ADR-0004 的信封加密，恢复码沿用 Argon2id
- `INVITED` 账号的密码哈希是一个随机 32 字节秘密的 Argon2id 哈希，秘密随即丢弃，所以这个账号不可能凭密码登录。另外有状态检查兜底，两层独立

**限流与枚举**
- `/auth/invitation/accept` 在 nginx 的 `/api/v1/auth/` 限流区内，另有进程内令牌桶兜底
- 令牌错误一律回同一个 400 码，不区分「不存在、已用、过期、用途不对、账号状态不对」
- 管理端的 `EMAIL_IN_USE` 会暴露「这个邮箱已有账号」，但只有 ADMIN 能调，可以接受

**个人数据与保留**
- 门户登录邮箱是个人数据（spec §112 的「authentication data」分类）
- 它和租户联系邮箱是两回事：`tenants.email` 不唯一、可以不同；建账号时前端默认带出联系邮箱，但以管理员提交的为准
- 删除与匿名化随关户任务和 PDPA 数据治理政策处理（上线闸门），本设计不新增保留规则

**不处理 prompt / response。**

## 7. 测试证据计划

SQL 断言不写死方言相关的字面量。只有 MySQL 才有的断言放进 `if engine.dialect.name == "mysql"` 分支。

| 风险/需求 | 测试层级 | 场景 | 预期结果 |
| --- | --- | --- | --- |
| 正常路径 | integration（SQLite + MySQL） | 开通 → outbox 渲染出链接 → 用令牌接受 → 登录 → `/customer/me` | 用户 `INVITED`→`ACTIVE`，`tenant_id` 正确；`/customer/me` 四个键逐字；审计依次是 `PORTAL_ACCOUNT_CREATED`、`PORTAL_INVITATION_ACCEPTED`、`LOGIN`（`actor_role=CUSTOMER`） |
| 正常路径 | integration | 客户开启 2FA：`/2fa/self/enrol`（重验密码）→ `/2fa/self/confirm` → 退出 → 登录 | 第二次登录得到 `TOTP_REQUIRED`；输对验证码后签发会话；ADMIN 调这两个接口 403 `CUSTOMER_REQUIRED`；密码错 401 `INVALID_CREDENTIALS` 且失败计数不变、不写审计；验证码错 401 `INVALID_TOTP` |
| 正常路径 | integration | `customer_2fa_required=True` 时，未注册 2FA 的客户登录 | 得到 `ENROL_2FA`；配置为 False 时直接签发会话 |
| 正常路径 | integration | 停用 → 启用：一个从未激活的账号、一个已激活的账号 | 分别回到 `INVITED`、`ACTIVE`；停用期间访问令牌立即 401，刷新令牌已吊销；停用后 `password_version` 比停用前大 1；**重新启用之后**，用停用前签发的访问令牌调任一已登录接口仍是 401 `TOKEN_INVALID`（不会因启用而复活），重新登录签发的新令牌可用 |
| 边界值 | integration | 令牌在 `expires_at` 前一秒接受、`expires_at` 当刻接受 | 前一秒成功；当刻 400（`expires_at > now` 才有效，与重置一致） |
| 边界值 | integration | 邮箱大小写、首尾空白不同但实为同一地址 | 归一后判同一个，409 `EMAIL_IN_USE`；与某个 ADMIN 的邮箱相同时也是 409 |
| 边界值 | integration | 租户为 `CLOSED` 时开通、重发、启用、接受邀请 | 全部 409 `ACCOUNT_CLOSED`，不写行 |
| 状态判定（登录改动） | integration | `INVITED` 账号用任意密码登录、刷新、提交 TOTP | 401 `INVALID_CREDENTIALS`，审计 reason `NOT_ACTIVE`；把三处判断改回 `is DISABLED` 的变异必须让这条用例变红 |
| 只读契约 | integration | 租户依次处于 `PENDING_ACTIVATION` / `ENABLED` / `DISABLED` / `CLOSED`，客户登录并调 `/customer/me`、一个挂了 `require_customer_writable` 的测试专用路由、`/2fa/self/enrol` | 四种状态都能登录；`read_only` 依次为 false/false/true/true；写依赖在后两种状态下 403 `ACCOUNT_READ_ONLY`；2FA 接口四种状态都允许 |
| 并发 | integration（MySQL） | 同一租户两个并发开通，邮箱不同 | 一个 201，一个 409 `PORTAL_ACCOUNT_EXISTS`，库里一个用户 |
| 并发 | integration（MySQL） | 两个租户并发用同一邮箱开通 | 一个 201，一个 409 `EMAIL_IN_USE` |
| 并发 | integration（MySQL） | 同一邀请令牌并发提交两次、密码不同 | 一个 200、一个 400；最终的密码哈希能验证成功那一次的密码 |
| 并发 | integration（MySQL） | 停用与接受同时到达（用两个连接、屏障控制先后，两种顺序各跑一次） | 只出现 §4 写的两种结果之一，不会出现停用之后账号仍是 `ACTIVE` |
| 幂等重放 | integration | 重复开通、用已用过的令牌接受、重发之后用旧链接接受 | 409；400；400 |
| 令牌隔离 | integration | 测试里绕过服务直接造「状态对、用途错」的令牌行：给 `INVITED` 客户插一行 `PASSWORD_RESET` 令牌交给接受邀请；给 `ACTIVE` 用户插一行 `INVITATION` 令牌交给 `reset_password`（只有这样，删掉任一边的用途条件用例才会变红；用正常途径造的令牌会先被状态检查拦下，证明不了用途过滤） | 都是 400，且不改密码、不改状态、不消费令牌 |
| 令牌隔离 | integration | `INVITED` 客户调 `/password/forgot` | 同一个 200，不写重置令牌，不写 outbox |
| 判定顺序 | integration | 接受邀请：已用的令牌配弱密码；有效令牌配弱密码后再配强密码；有效令牌但租户 `CLOSED` 配弱密码 | 依次 400 `INVALID_INVITATION_TOKEN`；先 400 `WEAK_PASSWORD`、令牌未消费，再 200；409 `ACCOUNT_CLOSED` |
| 重发邮箱比较 | integration | 重发时不给邮箱、给与当前邮箱仅大小写和首尾空白不同的邮箱、给一个被别的用户占用的邮箱、给一个新邮箱 | 前两种成功且审计 `email_changed: false`；第三种 409 `EMAIL_IN_USE`；第四种成功、邮箱更新、`email_changed: true` |
| 邀请字段 | integration | 管理端 GET：刚开通、重发之后、接受之后、停用后再启用（回到 `INVITED`）、令牌过期之后 | `invited_at` 依次为首个与最新邀请的时刻、保持最新邀请时刻；`invitation_expires_at` 依次为令牌到期时刻、新令牌到期时刻、null、null、null |
| 邮件到期时刻 | unit | 用 payload 里的 `expires_at` 渲染邮件，再把配置的有效期改掉重新渲染 | 两次正文的到期时刻相同，都等于 payload 的值换算到 UTC+8，并附 UTC |
| 唯一冲突映射 | integration（MySQL） | 让开通在检查之后、插入之前撞上另一个事务刚提交的同租户账号或同邮箱用户 | 分别 409 `PORTAL_ACCOUNT_EXISTS`、`EMAIL_IN_USE`，不出现 500，库里没有多出的行 |
| 停用后的 2FA 注册 | integration | 用户登录拿到 `ENROL_2FA` 的 pending 令牌后被停用（ADMIN 与开启强制的 CUSTOMER 各一次），再用该令牌调 `/2fa/enrol`、`/2fa/confirm`；客户在 `/2fa/self/enrol` 之后被停用，再调 `/2fa/self/confirm` | 都是 401 `TOKEN_INVALID`，`two_factor_settings` 与恢复码都没有新行；客户账号**重新启用之后**，用停用前拿到的 `ENROL_2FA` pending 令牌调 `/2fa/enrol`、`/2fa/confirm` 仍是 401 `TOKEN_INVALID`（版本号已在停用时 +1）；重新登录后照常走注册。ADMIN 没有停用接口，重新启用的断言只针对门户账号 |
| 写权限并发 | integration（MySQL） | 测试专用写操作在事务里调 `lock_tenant_for_customer_write` 后写一行；另一个连接同时把租户改成 `DISABLED`，用屏障控制两种先后各跑一次 | 写先拿到锁：写入成功，停用随后生效；停用先提交：写入方拿到锁后抛 `ACCOUNT_READ_ONLY`，测试表没有新行。另断言：只做一次不加锁的状态读取（与 `require_customer_writable` 的行为等价；服务层任务里还没有这个依赖，用等价读取代替）而不调辅助函数时，停用在两者之间提交，写入会成功（证明提前拒绝本身不是保证，用例写明这是刻意的） |
| 关户与接受并发 | integration（MySQL） | 一个连接锁住租户行并把 `account_status` 改为 `CLOSED`（模拟关户任务，测试里直接写），另一个连接同时接受邀请；先接受后普通读一次以建立快照的写法也跑一遍；屏障控制两种先后 | 关户先提交：409 `ACCOUNT_CLOSED`，账号仍是 `INVITED`、令牌未消费；接受先提交：200，账号 `ACTIVE` |
| 改密码后的旧会话（客户路径） | integration | 客户 `/2fa/self/enrol` 之后重置密码，再用旧访问令牌调 `/2fa/self/confirm`；接受邀请之后，接受前用于别处的同一用户令牌（测试里手工签发版本 0 的访问令牌）调 `/api/v1/me` | 都是 401 `TOKEN_INVALID`，不写任何 2FA 行；接受邀请后库里 `password_version` 为 1。其余改密码与令牌的场景由 #369 的测试覆盖 |
| 重置客户 2FA | integration | 客户已开启 2FA 并持有会话；管理员调 `/reset-2fa`；另测只有待确认注册、从未开启、原因为空、原因恰好 255 个字符、原因 256 个字符五种 | 成功时 `two_factor_settings` 与恢复码都没有了，`password_version` +1，客户原来的访问令牌与刷新令牌都 401，审计 `PORTAL_2FA_RESET` 带原因、不含邮箱；客户随后用密码登录不再要求 TOTP；待确认注册同样被清除；从未开启 409 `PORTAL_2FA_NOT_ENABLED`；原因为空 422；255 个字符成功且审计里的原因逐字相同（SQLite 与真 MySQL 都跑）；256 个字符 422，不写任何行 |
| 重置 2FA 与客户确认 2FA 并发 | integration（MySQL） | 客户 `/2fa/self/confirm` 与管理员 `/reset-2fa` 同时进行，屏障控制两种先后 | 确认先提交：2FA 启用，随后重置把它删掉并吊销会话；重置先提交：确认在锁用户行后读到新版本号，401，不启用 |
| 无租户的 CUSTOMER | integration | 测试里直接插一个 `tenant_id` 为空的 CUSTOMER 用户并签发访问令牌，调 `/customer/me` 与 `/2fa/self/enrol` | 403 `CUSTOMER_REQUIRED`，不读任何租户数据 |
| 事务中途失败 | integration | 开通时在插入 outbox 那一步注入 `OperationalError` | 500 带 `request_id`；users、令牌、outbox、审计都没有新行 |
| 事务中途失败 | integration | 接受时在写审计那一步注入异常 | 500；令牌未消费，账号仍是 `INVITED`，原链接还能用 |
| Redis/Celery 丢失 | integration | 开通后不跑 worker | 服务层（任务 B）：outbox 行留在 `PENDING`；接口层（任务 D，邀请邮件渲染已登记之后）：现有恢复扫描能领到它（恢复扫描只选有渲染器的事件类型） |
| 租户越权 | integration | 客户 A 调 `/customer/me?tenant_id=<B>`、带 B 的 `public_id` 作查询参数和请求体 | 返回的始终是 A |
| 租户越权 | integration | CUSTOMER 令牌枚举调用全部 `/api/v1/admin/*` 路由 | 全部 403 `ADMIN_REQUIRED`（扩展现有路由枚举用例） |
| 租户越权 | integration | ADMIN 令牌枚举调用全部 `/api/v1/customer/*` 路由；匿名调用 | 403 `CUSTOMER_REQUIRED`；401 |
| 路由守卫 | integration | 枚举 `/api/v1/customer/*` 的处理函数 | 每个都显式调用了 `require_customer` 或 `require_customer_writable`（与 admin 侧的守卫用例同一写法） |
| 数据库兜底 | integration（SQLite + MySQL） | 绕过服务：插入 `role=ADMIN` 且带 `tenant_id`；同一 `tenant_id` 两行；`status='INVITED'` 的 ADMIN；`status='OTHER'` 的用户；`purpose='OTHER'` 的令牌 | 全部被 CHECK 或唯一约束拒绝；另断言 `role=CUSTOMER` 且无 `tenant_id` 的插入**被允许**（§9 备选 F 的取舍，用例写明这是刻意的） |
| 泄漏 | integration | 全流程中抓日志、审计 JSON、异常响应 | 不出现密码、令牌原文、TOTP 密钥、恢复码；门户审计的 `after_state` 不含邮箱值 |
| 链接守卫 | integration | 邀请邮件渲染出的链接与前端 `paths.ts` 登记的路由对齐（扩展 `test_password_reset_link.py`） | 路径与查询参数名都对得上 |
| 邀请链接匿名可达 | 前端 route（Vitest，任务 C） | 以匿名状态渲染 `/accept-invitation?token=x` | 渲染接受邀请表单，**不**跳转登录页；同一用例以「已登录 ADMIN」状态渲染也显示表单 |
| 邀请链接匿名可达 | integration（任务 D，扩展 `test_password_reset_link.py`） | 取后端邀请邮件真实渲染出的链接，核对 `paths.ts` 登记了该路径，且 `routes/index.tsx` 中该路由不在 `RequireAuth` 元素之内（与重置链接同一份守卫） | 三项都成立；把路由挪进 `RequireAuth` 的变异必须让用例变红 |
| 上线顺序 | 契约（`.platform/tasks.yaml`） | D 的 `depends_on` 含 C | 登记 PR 里核对 |
| 迁移 | integration（MySQL） | `upgrade` 到 0025（接在 0024 之后）：已有重置令牌回填 `PASSWORD_RESET`、已有 ADMIN 满足新 CHECK；`downgrade` 在没有 CUSTOMER 和 INVITATION 行时成功，有这类行时拒绝 | 与预期一致 |
| 金额精度 | — | 不适用：本设计没有金额 | — |

## 8. 迁移与上线

- **数据迁移步骤**：迁移 0025（`down_revision` 是 #369 的 `0024_password_version`，保证只有一个 alembic head）
  1. `users` 加两列，都可为 NULL，不回填
  2. `users` 加外键 `fk_users_tenant`、唯一约束和三条 CHECK。现有行都是 ADMIN、`ACTIVE` 或 `DISABLED`、`tenant_id` 为 NULL，全部满足
  3. `password_reset_tokens` 加 `purpose` 列（带默认值）与 CHECK；索引不动
- **锁表与性能**：两张表都很小（生产 `users` 2 行）。MySQL 8 在线 DDL 加列可以即时完成，加约束会扫表，毫秒级
- **兼容窗口**：迁移先于代码生效（现有部署流程先跑迁移）
  - 旧代码遇到新表：读 `users` 时多出的列被忽略；插入重置令牌时 `purpose` 取默认值
  - 所以「新迁移 + 旧代码」的窗口里，重置功能照常
- **部署顺序**：先完成前置设计 #369 的实现，再按 §11 的拆分逐个合并、自动部署（A → B → C → D → E，`depends_on` 强制）。任何一个任务单独上线都不改变现有行为：
  - A 数据库层：只加列和约束
  - B 业务层：没有入口
  - C 前端接受邀请页：页面先上线，此时还没有任何邀请令牌，提交只会得到 400 或 404，不影响任何人
  - D 接口层：管理员开通、发出邀请从这一刻起才可能；这时接受页已经在线
  - E 门户其余前端：只是体验，缺它时客户已经能接受邀请并登录（登录后落在现有的兜底页，见下）
- **回滚或前滚**：
  - 生产只前滚
  - 0025 的 `downgrade` 在存在 CUSTOMER 用户或 `INVITATION` 令牌时拒绝执行，避免丢掉租户关联
  - 代码回滚到旧版本时，CUSTOMER 用户依然存在，旧代码会把 `INVITED` 当作「不是 `DISABLED`」放过状态检查。这时它的密码哈希不可用（§6），仍然登录不了
- **部分部署时的行为**：
  - 前端现有的兜底路由在 `RequireAuth` 之内（`frontend/src/routes/index.tsx`），匿名访问一个没登记的路径会被送到登录页，而 `INVITED` 账号没有可用密码 —— 这就是死链。所以**接受邀请页（匿名可达、不在 `RequireAuth` 之内）必须先于任何能发出邀请的代码上线**：它是任务 C，能发出邀请的管理端开通 / 重发接口与邀请邮件渲染都在任务 D，D `depends_on` C
  - D 已上线、E 还没上线时：客户能接受邀请、能登录，但前端还没有按角色跳转，客户登录后会进入现有的管理端外壳，所有管理端接口对他返回 403，页面显示错误态。不泄漏数据（§6），只是不可用。这段窗口里管理员**不应开通**真实客户的门户账号；E 合并部署后才对客户开放。这一条写进 D 的 PR 描述与 `docs/runbook.md`
  - 万一 D 先于 C 上线（`depends_on` 被绕过）：D 的链接守卫用例要求 `paths.ts` 已登记且路由在 `RequireAuth` 之外，CI 会先挂，合并不了
- **监控与告警负责人**：没有新告警维度。邀请信投递复用现有 outbox 告警（负责人 Kelvin，经 Healthchecks 通知）

## 9. 方案取舍

| 方案 | 优点 | 风险 | 未采用原因 |
| --- | --- | --- | --- |
| 采用：`users` 加 `tenant_id` + 唯一约束；邀请复用 `password_reset_tokens`，加 `purpose` | 一张用户表、一套登录；单用、只存哈希、条件 UPDATE、按用户作废这些已经审过的令牌机制直接复用 | 两种用途共用一张表，查询漏写 `purpose` 就会串用 | — 控制：两个服务各自只认自己的用途；§7「令牌隔离」两条用例，删掉任一边的 `purpose` 条件都会变红 |
| 备选 A：新建 `customer_portal_accounts` 表关联 `users` 与 `tenants` | 不改 `users` | 「一个租户一个账号」和「一个用户一个租户」要两条唯一约束加一次 join，鉴权时每个请求都多一次查表 | spec §52 是 1:1，`users` 上一个可空列就能表达，CHECK 还能把「ADMIN 不属于任何租户」写死在库里 |
| 备选 B：新建 `user_invitations` 表 | 用途天然隔离 | 要复制一遍已经过五轮审查的单用 / 并发 / 作废逻辑，以及对应的并发测试 | 复制代码比加一个判别列风险更大；判别列的风险由 §7 的用例兜住 |
| 备选 C：管理员直接设初始密码，线下交给客户 | 不依赖邮件送达 | 管理员知道客户密码；密码经即时通讯工具流转 | 违背「密码只有本人知道」，审计也说不清是谁登录的 |
| 备选 D：门户登录另起一组接口（`/api/v1/customer/auth/*`） | 两端可以分开调参 | 锁定、限流、重放检测等安全控制要维护两份 | spec §51 要求一套认证；Kelvin 2026-10-19 同意共用登录入口 |
| 备选 E：租户 `DISABLED` / `CLOSED` 时直接拒绝登录 | 实现最简单 | 客户看不到账单和收据 | 违反 020 契约（Kelvin 2026-09-28 确认的只读） |
| 备选 F：数据库同时强制「CUSTOMER 必须有 `tenant_id`」（v2 的 `ck_users_role_tenant`） | 少依赖一处应用代码 | 现有 12 个后端测试文件（`grep -l UserRole.CUSTOMER tests/backend` 实数）各自用本地的 `make_user` 造不挂租户的 CUSTOMER 来断言 403，`test_admin_customers_api.py` 还把 ADMIN 原地改成 CUSTOMER 测降级；约束一加它们全部失败，只能在同一个 PR 里一起改，单个任务的可改路径远超 12 个 | 不挂租户的 CUSTOMER 只能经直接写库出现（唯一的创建入口总是写 `tenant_id`），出现了也只会被 `require_customer` 拒在所有客户接口之外，拿不到任何租户数据。数据库仍然强制「ADMIN 不挂租户」与「一个租户最多一个门户账号」这两条真正关乎越权的约束 |

## 10. 未决问题与假设

**未决问题**：无阻断项。下面三项已拍板：
- 管理员开通并发邀请、不做自助注册
- 客户 2FA 可选，留强制开关
- `PENDING_ACTIVATION` 可以完整使用

**尚未验证的假设**
- **A1**：沿用 SMTP（ADR-0014）发邀请信可以接受（Kelvin 2026-10-19 同意）。已知 Outlook.com 可能把邮件判进垃圾箱，且没有退信和投诉抑制
- **A2**：邀请有效期 72 小时（新配置 `customer_invitation_ttl_seconds = 259200`）够用，过期后由管理员重发
- **A3**：客户访问令牌和刷新令牌的 TTL 与 ADMIN 相同（600 秒 / 闲置 1800 秒 / 绝对 12 小时），不单独配置

**需要谁拍板**：无。本设计以上述决定为前提。以后如果需要「`ACTIVE` 账号改登录邮箱」「客户自己关闭 2FA」「重置 ADMIN 的 2FA」，各开一个闸门。

**如果假设错误，具体后果**
- A1 不成立：客户收不到或看不到邀请，账号停在 `INVITED`
  - 管理员可以从 GET 接口看出来，但没有自动告警
  - 补救办法是换成事务邮件 API 服务（ADR-0013 的方向），不影响本设计的数据结构
- A2、A3 不成立：只需改配置，不需要迁移

## 11. 审查与版本绑定

审查方要回答的五个问题见 [scripts/review_checklist.md](../../scripts/review_checklist.md) 的「设计审查」一节。

版本绑定规则：
- 批准必须写成 `APPROVED: design v<N>`
- 设计版本一变，之前的批准作废
- 实质修改（契约、表结构、事务边界、状态机、失败语义、不变量控制）时版本 +1，并在 §12 记录，状态退回 `READY_FOR_REVIEW`

### 实现拆分（每个任务一层，用 `depends_on` 串起来，每层合并后都能单独通过检查与 CI）

任务 A 依赖 #369 的全部实现（P1、P2、P3）已经合并：接受邀请要递增的 `password_version` 列与 `/2fa/self/*` 要传的版本号参数都来自那里。

| 任务 | 层 | 内容 | 依赖 |
| --- | --- | --- | --- |
| A | 数据库 | 迁移 0025（接在 #369 的 0024 之后）；模型改动：`UserStatus.INVITED`、`User.tenant_id` / `activated_at`、`PasswordResetToken.purpose`；数据库兜底与迁移测试；`docs/database-schema.md` 补 `users` 一节 | — |
| B | 业务规则 | `app/services/portal_accounts.py`：开通、重发、停用、启用、接受邀请、重置 2FA；登录、TOTP、刷新三处改为 `is not ACTIVE`；2FA 注册两个服务函数锁用户行并要求 `ACTIVE`；接受邀请递增 `password_version`（#369）；`lock_tenant_for_customer_write`；重置服务限定用途；`customer_2fa_required`、`customer_invitation_ttl_seconds` 配置；新审计动作；对应的服务层与并发测试 | A |
| C | 前端（不走闸门） | 接受邀请页：`routes/paths.ts` 登记 `/accept-invitation`，路由放在 `RequireAuth` 之外（与重置密码页同级），表单提交 `POST /api/v1/auth/invitation/accept`，成功后转登录页；§7「邀请链接匿名可达」的前端用例。先于 D 上线，此时后端接口尚不存在，页面对任何令牌都显示「链接无效或已过期」 | B（只为排序；不依赖 B 的代码） |
| D | 接口 | 管理端六个、认证四个（接受邀请、`/api/v1/me`、`/2fa/self/enrol`、`/2fa/self/confirm`）、客户端一个（`/customer/me`）接口；`require_customer` / `require_customer_writable`；邀请邮件渲染；扩展链接守卫（路径已登记、路由在 `RequireAuth` 之外）；路由枚举与越权用例；`docs/api.md`；`docs/runbook.md` 写「E 上线前不为真实客户开通」 | B、C |
| E | 前端（不走闸门） | 管理端客户详情页的门户账号面板；登录后按 `/api/v1/me` 的角色跳转；门户外壳和 `/portal` 首页（显示 `/customer/me`、只读提示、开启 2FA） | D |

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
| v1 | 2026-10-19 | 初稿 | — |
| v2 | 2026-10-19 | 接受邀请页拆成独立任务 C，先于能发出邀请的接口任务 D 上线；原 D 改为 E。§8 补「部分部署时的行为」三种窗口；§7 加匿名可达的前端用例、链接守卫检查路由不在 `RequireAuth` 内、`depends_on` 核对 | Codex 审查 v1（REQUEST_CHANGES）：C 单独上线时邀请链接落到 `RequireAuth` 内的兜底路由，匿名客户被送去登录页，邀请无法接受 |
| v3 | 2026-10-19 | ① 数据库只强制「ADMIN 不挂租户」（`ck_users_admin_no_tenant`），「CUSTOMER 必须挂租户」改由唯一创建入口与 `require_customer` 保证（§2、§9 备选 F）；外键命名 `fk_users_tenant`；`password_reset_tokens` 的索引不重建。② 接受邀请的判定顺序写明（§4），弱密码改为与重置一致的 400 `WEAK_PASSWORD`。③ 客户开启 2FA 的接口移到 `/api/v1/auth/2fa/self/`，沿用现有 401 错误码，失败处理照 `/2fa/recovery-codes`。④ 重发时邮箱先归一再比较（§4）。⑤ 唯一冲突的映射办法（§4）。⑥ 管理端 GET 的 `invited_at` / `invitation_expires_at` 取自最新邀请令牌（§2）。⑦ 邀请邮件 payload 加 `expires_at`，信里写到期时刻而不是时长（§2）。§7 相应增删用例 | 登记前预审（`worker.design_precheck`）对 v2 的 12 条发现：086 的 SCOPE_GAP 与两条 UNDECIDED、087 的三条 UNDECIDED、089 的两条 DESIGN_DEFECT 与一条 UNDECIDED、090 的两条 UNDECIDED 与一条 DESIGN_DEFECT |
| v4 | 2026-10-19 | ① `start_enrolment` / `confirm_enrolment` 在事务里锁用户行并要求 `ACTIVE`，停用后的 pending 令牌与 `/2fa/self/*` 都不能再注册 2FA（§2 登录流程的改动、§7「停用后的 2FA 注册」）。② 新增 `lock_tenant_for_customer_write`：客户写操作在自己的写事务里锁租户行并重查账户状态，`require_customer_writable` 明确只是提前拒绝（§2 依赖、§7「写权限并发」）。§11 任务 B 相应加上这两项 | Codex 审查 v3（REQUEST_CHANGES）两条阻断：停用前拿到的 `ENROL_2FA` 令牌停用后仍能注册 2FA；只读检查与写入之间的停用会让充值在 `DISABLED` 下写入（spec §112.1） |
| v5 | 2026-10-19 | ① 接受邀请先锁租户行再锁用户行，看锁住的租户判 `CLOSED`（§4 判定顺序、串行化），§7 加「关户与接受并发」。② 新增 `users.password_changed_at`，重置密码与接受邀请写入；`iat` 不晚于它的访问令牌与 pending 令牌一律 401，检查放在 `require_current_user`、`complete_second_factor`、`start_enrolment`、`confirm_enrolment`（§2「改密码让旧会话失效」），§7 加「改密码后的旧会话」「改密码与 2FA 确认并发」。§11 任务 A、B 相应加上 | Codex 审查 v4（REQUEST_CHANGES）两条阻断：接受邀请未锁租户行，关户后仍可能激活；重置密码后旧访问令牌仍能确认 2FA，主人被锁在门外（spec §53） |
| v6 | 2026-10-19 | 去掉 v5 的 `password_changed_at` 与 `iat` 比较，改为依赖前置设计 #369（密码版本号）：接受邀请递增 `password_version`，`/2fa/self/*` 把令牌版本号传给服务；§7 只保留客户路径的用例，其余移到 #369；§8、§11 写明 #369 先实现 | Codex 审查 v5（REQUEST_CHANGES）：登录在重置提交前用旧密码通过校验，`iat` 晚于改密时刻而被放行。Kelvin 2026-10-19 决定把「改密码吊销会话」拆成前置设计闸门 #369 |
| v7 | 2026-10-19 | ① 新增管理员「重置客户 2FA」：`POST …/portal-account/reset-2fa`（必填原因），删除 2FA 设置与恢复码、`password_version + 1` 吊销全部会话、审计 `PORTAL_2FA_RESET`，管理员先线下核实身份（§1、§2、§4、§5、§6、§7、§11）。② §2 数据流与「事务边界」里接受邀请的加锁写法改成与 §4 一致（先锁租户行） | Claude 审查 v6（REQUEST_CHANGES）：客户能开启 2FA 却没有任何恢复路径，丢失或被抢先注册后账号永久不可用；§2 两处仍写「先锁 user 行」，与 §4 矛盾 |
| v8 | 2026-10-19 | 重置 2FA 的原因上限从 500 改为 255 个字符，与 `audit_logs.reason` 列宽和现有 `_Reason` 一致（§2）；§5 加「DB 提交失败（重置 2FA）」；§7 加 255 / 256 个字符的边界用例 | Claude 审查 v7（REQUEST_CHANGES）：500 字的原因在生产 MySQL 上会报 Data too long 回滚或被静默截断 |
| v9 | 2026-10-19 | ① 停用门户账号时 `password_version + 1`，停用前的访问令牌与 pending 令牌不会因重新启用而复活（§4）。② 当前用户接口从 `/api/v1/auth/me` 移到 `/api/v1/me`：避开前端对 `/api/v1/auth/` 不刷新的规则与认证限流（§2）。③ `/2fa/self/enrol` 显式调用 `_rate_limit`（§2）。④ 邀请令牌的 `expires_at` 截到整秒（§2）。⑤ §7「令牌隔离」改为造「状态对、用途错」的令牌行，「Redis/Celery 丢失」按任务 B / D 分开断言，「写权限并发」的对照断言用等价的不加锁读取。⑥ 错误码名称更正：设计里的 `INVALID_TOKEN` 是现有 `InvalidToken` 的错误码，实际为 `TOKEN_INVALID`，全文改正 | 登记前预审（`worker.design_precheck`）对 v8 的发现：089 的三条 DESIGN_DEFECT 与两条 UNDECIDED、091 的一条 DESIGN_DEFECT 与一条 UNDECIDED、087 的错误码 DESIGN_DEFECT |
| v10 | 2026-10-19 | ① 本设计的迁移编号改为 0025、接在 #369 的 `0024_password_version` 之后（§2、§7、§8、§11），避免两个 alembic head。② §7「停用 → 启用」与「停用后的 2FA 注册」各加断言：重新启用后，停用前的访问令牌与 pending 令牌仍是 401，版本号比停用前大 1 | Claude 审查 v9（REQUEST_CHANGES）：迁移编号与 #369 冲突；v9 新增的停用 +1 没有对应的测试断言 |

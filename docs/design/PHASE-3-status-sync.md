# Phase 3 服务状态同步设计：项目集成状态、reason_code、出站状态 webhook 与对账查询（已批准 v6）

> **来源**：设计闸门 Issue #241。本文件是 `APPROVED: design v6` 那一版正文的**逐字副本**，审查者为 Codex。
> 放进仓库，是因为 OpenClaw Worker 在沙箱里不联网、读不到 GitHub Issue。
> **实现以本文件为准**；与 Issue 不一致时，以 Issue #241 上被批准的 v6 为准。设计要改，就回到 Issue 升版本、重新过闸门，不要直接改本文件。
> §8「拆分」的七项依次登记为 `AIH-TASK-047`（数据库）、`AIH-TASK-048`（版本收拢）、`AIH-TASK-049`（业务规则）、`AIH-TASK-050`（接口）、`AIH-TASK-051`（投递）、`AIH-TASK-052`（告警与运维文档）、`AIH-TASK-053`（前端）。
> 正文与 Issue 逐字相同；正文里的相对链接按本文件在 `docs/design/` 下的位置书写，可直接点开。

---

## 0. 准入判定

本任务档位：`全部`。它新增项目级状态机（`integration_status`），改有效状态的合成规则，并新增出站 Webhook 的投递状态机。

---

状态：`READY_FOR_REVIEW`
负责人：Claude
**设计版本**：`v6`（v1–v4 被 Codex 判 REQUEST_CHANGES；v5 批准后登记预审发现文字矛盾与未定细节，见 §12）
对应需求：spec §24、§25、§26、§27、§28、§29、§30、§92、§119；`REQ-STATUS-001`、`REQ-AVAIL-001`、`REQ-TXN-001`；[ADR-0004](../adr/ADR-0004-credential-encryption.md) §4a；[内部计量补充](INTERNAL-METERED-ONLY-2026-10-08.md)
目标 PR：待开（批准后按 §8「拆分」登记三个实现任务）

## 1. 目标与边界

- **要解决的问题**：集成方（第一家是 `acuven_ai_api`）现在每次调模型前都实时调一次 `GET /api/v1/integration/effective-status`，查不到就放行。后果有两个：
  1. Billing Hub 不可用时，**已经被停用的租户**也会被放行。spec §26 要求的是集成方本地保存最后一次接受的状态和版本，按本地状态执行。
  2. 每次模型调用都多一次跨服务请求。
  spec §28–§30 规定的推送（签名 webhook）和兜底（周期对账）都还没有。另外，有效状态的公式少了项目维度（`project.integration_status`），也没有 `reason_code`。
- **可观察的完成标准**：
  1. `projects` 有 `integration_status`（`ENABLED` / `DISABLED`），已有项目回填 `ENABLED`。管理员可以改它，改动在租户行锁内让 `status_version` +1，并写审计。
  2. 有效状态由一个纯函数合成：输入是账户状态、计费状态、计费模式、项目集成状态，输出 `ALLOW_AI` / `BLOCK_AI` 和 `reason_code`（取值见 §2）。`effective-status` 接口、webhook payload 都只调这个函数。
  3. `effective-status` 的响应增加 `project_integration_status`、`reason_code`、`effective_at`，原有字段不变。
  4. 管理员可以为项目设置 `status_webhook_url`（只收 https）。设置之后，只要项目的 `integration_status = ENABLED`（§25），平台就把该项目「当前」的有效状态签名推送过去：
     - 首次推送在 `status_version` 变化后 p95 ≤ 60 秒内发出（§119）；
     - 失败按指数退避重试，**不设放弃上限**，最终一定送达最新版本（§29）；
     - 没有 `ACTIVE` 签名密钥、或密钥解不开（密文损坏、主密钥未配置）时不发送，记错误码，进健康告警。
  5. 健康检查多一个维度 `status_webhook`，同时报三类问题：推送连续失败的项目数、配置了 URL 但没有 `ACTIVE` 密钥的项目数、最近一次因签名错误（解密失败或主密钥未配置）未发送的项目数。
  6. `docs/api.md` 写清集成方的接收契约：验签、时间窗（±300 秒）、按 `status_version` 应用、同版本幂等、旧版本确认后忽略，以及对账的周期、单次超时、失败重试与滞后告警（见 §2「时间」）。
- **明确不做什么**：
  - 集成方（`acuven_ai_api`）的接收端、本地版本化状态与对账轮询：在那个仓库按本文的契约另做
  - 管理端前端（批准后另登记前端任务）
  - Email / WhatsApp 通知（Phase 6）、`customer.billing.low_balance`（§92 列为 Future）
  - `backend_base_url`（§28 列出的字段）：投递只需要 `status_webhook_url`，没有任何功能读 `backend_base_url`，不建
  - 改动现有 `domain_outbox` 事件的类型与 payload（`tenant.billing_status_changed`、`tenant.account_status_changed`）：原样保留，留给 Phase 6 通知使用。本设计的投递不消费 outbox（理由见 §9），但每次跃迁仍写一条领域 outbox 事件（见 §2「`status_version` 递增点收拢」，不变量 13）
- **现有行为与问题证据**：
  - `app/api/integration_usage.py:56-84` 的 `_effective_status` 只看 `ai_service_enabled(tenant)`（`app/models/tenancy.py:85-90`），没有项目维度，也没有原因码
  - `projects` 只有身份字段（`app/models/tenancy.py:167-192`），文件头注明集成字段「刻意延后」
  - `status_version` 有三个递增点：`app/repositories/wallet.py:530`（余额驱动）、`app/services/account_status.py:133`（管理员启停）、`app/services/internal_billing.py:87`（设为内部计量）。最后一个**不写 outbox 事件**
  - 签名库 `signing_material` / `sign_status_webhook`（`app/services/webhook_signing.py:337-387`）已上线，还没有发送方
  - `app/` 里没有任何 HTTP 客户端库，唯一的出站 HTTP 是 BNM 汇率适配器（标准库 `urllib`，禁止重定向，`app/core/fx_source.py:134-150`）

## 2. 设计概要

```text
任一状态跃迁（余额 / 账户 / 计费模式 / 项目集成状态）
  → 同一事务：改状态、tenant.status_version +1、tenant.status_changed_at = now、审计
  → 同一事务写一条领域 outbox 事件（见「`status_version` 递增点收拢」）→ 提交
    （投递不消费 outbox：「要推送」由版本差推导，见下）

celery beat 每 15 秒 → status_webhook.dispatch
  → 领取到期的投递行：project_status_deliveries 中
      项目有 URL 且 integration_status = ENABLED、delivered_version < tenant.status_version（或从未送达）、
      next_attempt_at ≤ now、租约空或已过期
    （FOR UPDATE SKIP LOCKED；同一事务读状态快照 → compose_effective_status → payload；
      写租约令牌与 attempted_version，提交）
  → 事务外：signing_material → sign_status_webhook → POST（超时 5 秒，不跟随重定向）
  → 结算（带租约令牌的条件 UPDATE）：
      2xx：delivered_version = GREATEST(COALESCE(delivered_version, -1), attempted_version)，清零失败计数
      其他：attempt_count +1，next_attempt_at = now + 退避，记 HTTP 状态 / 错误码

集成方：收 webhook 与周期对账都走同一条规则——版本更大才应用，同版本幂等，旧版本确认后忽略
```

### 有效状态合成（纯函数，放在 `app/models/tenancy.py`，与 `ai_service_enabled` 同处）

`compose_effective_status(account_status, billing_status, billing_mode, integration_status) -> (effective_status, reason_code)`。判定顺序固定，第一条命中即返回 `BLOCK_AI`：

| 顺序 | 条件 | `reason_code` |
| --- | --- | --- |
| 1 | `account_status = CLOSED` | `ACCOUNT_CLOSED` |
| 2 | `account_status = DISABLED` | `ACCOUNT_DISABLED` |
| 3 | `account_status = PENDING_ACTIVATION` | `ACCOUNT_PENDING_ACTIVATION` |
| 4 | `integration_status = DISABLED` | `PROJECT_INTEGRATION_DISABLED` |
| 5 | `billing_mode = PREPAID` 且 `billing_status ≠ ACTIVE` | `BALANCE_NOT_POSITIVE` |
| — | 以上都不命中 | `ALLOW_AI`，`reason_code = null` |

- 管理员动作（1–4）排在余额（5）前面。这样充值后集成方看到的仍是管理员停用的原因，不会误以为充值能解封（§24：充值不得重开被管理员停用的租户 / 项目）。
- `ai_service_enabled(tenant)` 保留给租户级调用方，内部改为调用本函数（`integration_status` 传 `ENABLED`），两处不各写一份公式。
- 现有两种 outbox payload 里的 `effective_status`（`wallet.py:557`、`account_status.py:156`）也改为调用本函数，取值不变。

### 接口

**改：`GET /api/v1/integration/effective-status`**（鉴权、身份来源不变）。`data` 新增三个字段：

| 字段 | 说明 |
| --- | --- |
| `project_integration_status` | `ENABLED` / `DISABLED` |
| `reason_code` | 上表取值，`ALLOW_AI` 时为 `null` |
| `effective_at` | 最近一次状态跃迁时刻：`tenant.status_changed_at`，旧数据为空时取 `tenants.created_at`。格式见 api.md「时间」 |

原有字段 `account_status`、`billing_status`、`billing_mode`、`effective_status`、`status_version` 保持原名，不改成 spec §30 示例里的 `tenant_account_status` 等写法（已上线、集成方在用；Kelvin 2026-10-09 拍板，见 §10）。

**新：`POST /api/v1/admin/customers/{customer_id}/projects/{project_id}/integration-status`**，只有 ADMIN 能调，处理函数第一行是 `require_admin`。

| 字段 | 规则 |
| --- | --- |
| `integration_status` | `ENABLED` / `DISABLED` |
| `reason` | 去首尾空白后 1–255，只进审计 |

- 目标等于当前：200，不写库
- 成功：200，返回项目对象（含新字段）
- 错误：404 `CUSTOMER_NOT_FOUND` / `PROJECT_NOT_FOUND`
- 租户 `CLOSED`（将来）时 409 `ACCOUNT_CLOSED`

**新：`PUT /api/v1/admin/customers/{customer_id}/projects/{project_id}/status-webhook`**，只有 ADMIN 能调。

| 字段 | 规则 |
| --- | --- |
| `url` | `null` 表示清除。否则按下面的规则规范化并校验，失败 422 |

URL 规范化与校验（`urllib.parse.urlsplit`）：

0. **先查原始输入**（在 `urlsplit` 之前）：含空格、控制字符（0x00–0x20、0x7F）的一律 422。`urlsplit` 会悄悄去掉制表符与换行，所以这一步必须在拆分之前做。原始输入里出现 `@` 或 `#`（含空的用户信息、空的 fragment）一律 422；主机名后面跟着冒号却没有端口（`https://h:/x`）也是 422。
1. scheme 转小写，必须是 `https`；不带用户信息、不带 fragment；端口省略或 443。
2. 主机名：去掉末尾一个点、转小写，再做 IDNA 编码成 ASCII（编码失败 422）；结果不能为空，每个点分段也不能为空（`https://./x`、`https://:443/x` 一律 422）。IDNA 编码后，每个点分段必须是 LDH 形式：只含小写字母、数字与连字符，不以连字符开头或结尾，长 1–63（`xn--` 开头的 punycode 段符合）；整个主机名 ≤ 253。下划线、`%`、`<` 等一律 422。
3. **IP 字面量一律拒绝**：
   - 带方括号的 IPv6；
   - `ipaddress.ip_address` 能解析的；
   - 每个点分段都是十进制数字或 `0x` 十六进制（覆盖 `127.1`、`2130706433`、`0x7f.1` 这类简写）。
4. 路径为空时取 `/`；查询串为空（含只有一个 `?`）时去掉；路径与查询串只允许 0x21–0x7E 的可打印 ASCII（空格、控制字符、非 ASCII 一律 422，需要时由集成方自己百分号编码），**不做**百分号编码的规范化，按原样保留。IDNA 用 Python 标准库的 `idna` 编解码器（`str.encode("idna")`），不引入依赖。
5. 保存的是规范化后重新拼出的 URL（`https://<规范化主机名><path>[?query]`，端口 443 省略），总长 ≤ 2048。「与当前值相同」按这个字符串逐字节比较。

审计与比较一律用规范化后的值。

- **规范化后与当前值相同**（含两次都是 `null`）：200，不写库——不重置游标、不写审计
- **真实变化**：
  - 设为一个地址：UPDATE 该项目的投递行（每个项目恰好一行），`delivered_version` 置空，让新地址收到一次当前状态；同时清空租约，使发往旧地址的在途请求不能再结算（见 §2「事务边界」）
  - 清除：同样重置投递行（行保留，不再被领取，因为没有地址）
- 写审计 `PROJECT_STATUS_WEBHOOK_SET`：`before_state` / `after_state` 只有 `status_webhook_host`（规范化主机名或 `null`），不记完整 URL（路径里可能带集成方的令牌）
- 请求体必须有 `url` 字段（缺失 422；显式 `null` 表示清除），其他字段 422
- 判定顺序：请求体与 URL 规范化校验（422）→ 客户与项目查找（404）→ 设为地址而租户 `CLOSED`（409）→ 与当前值相同（200 不写）→ 写入
- 响应 200，`data` = `{"status_webhook_url": <规范化 URL 或 null>, "status_webhook_configured": <bool>}`
- 错误：404 `CUSTOMER_NOT_FOUND` / `PROJECT_NOT_FOUND`；设为地址而租户 `CLOSED` 时 409 `ACCOUNT_CLOSED`；422 校验失败
- 项目对象新增 `integration_status`、`status_webhook_configured`（布尔）；完整 URL 只出现在本接口和下面的 GET 里

**新：`GET /api/v1/admin/customers/{customer_id}/projects/{project_id}/status-webhook`**，只有 ADMIN 能调。返回与 PUT 相同的 `data`，只读、不写审计。错误：404 `CUSTOMER_NOT_FOUND` / `PROJECT_NOT_FOUND`。

**不新增** spec §30 的 `GET /api/v1/integration/account-status`：对账直接用 `effective-status`（Kelvin 2026-10-09 拍板，见 §10）。

### Webhook 请求

`POST <status_webhook_url>`，四个签名头沿用 [api.md「状态 webhook 签名」](../api.md)。请求体（`Content-Type: application/json`，按键排序、无多余空白）：

```json
{
  "event_id": "sevt_<project_public_id>_<status_version>",
  "tenant_id": "租户 public_id",
  "project_id": "项目 public_id",
  "account_status": "ENABLED",
  "billing_status": "SUSPENDED",
  "billing_mode": "PREPAID",
  "project_integration_status": "ENABLED",
  "effective_status": "BLOCK_AI",
  "reason_code": "BALANCE_NOT_POSITIVE",
  "status_version": 42,
  "effective_at": "2026-10-09T10:10:00"
}
```

- 字段名与 `effective-status` 的响应**完全一致**，集成方只写一份解析
- **时间窗**：集成方校验 `X-Acuven-Timestamp` 与本机时间相差不超过 300 秒，与入站签名的 `MAX_SKEW_SECONDS` 相同；超出就回非 2xx，平台按失败重试，每次重发都会重新生成时间戳
- **`event_id` 由（项目, 版本）确定**：同一版本重发时 id 不变；同一版本的状态快照也唯一（任何状态变化都会让版本 +1）
- 不带余额、阈值、成本，不带管理员原因
- 不带 §92 的事件类型名（`customer.billing.suspended` 等），见 §9；Kelvin 2026-10-09 拍板，见 §10

### 数据库（迁移 0020）

**`projects` 加两列**：

- `integration_status VARCHAR(16) NOT NULL DEFAULT 'ENABLED'`，`CHECK IN ('ENABLED','DISABLED')`
- `status_webhook_url VARCHAR(2048) NULL`

**`tenants` 加一列**：`status_changed_at DATETIME NULL`。旧行保持空，读取时回退到 `created_at`。

**新表 `project_status_deliveries`**（每个项目**恰好一行**：迁移 0020 给已有项目各插一行，`next_attempt_at` = 迁移时刻；以后建项目时在同一事务里插入。行从不删除，项目本身也不能删，外键是 RESTRICT。之后的所有路径只 UPDATE、不再插入，因此不存在「先查后插」的间隙锁死锁）：

| 列 | 类型 | 说明 |
| --- | --- | --- |
| `project_id` | BIGINT PK | 与 `tenant_id` 组成复合外键 → `projects(id, tenant_id)`，RESTRICT |
| `tenant_id` | BIGINT NOT NULL | 领取时直接 JOIN `tenants` |
| `delivered_version` | BIGINT NULL | 已确认送达的最大版本；空 = 从未送达 |
| `attempted_version` | BIGINT NULL | 当前租约正在发送的版本 |
| `attempt_count` | INT NOT NULL DEFAULT 0 | 自上次成功以来的连续失败次数 |
| `next_attempt_at` | DATETIME NOT NULL | |
| `lease_token` | CHAR(36) NULL | 领取时写入的 uuid4，结算时作围栏 |
| `lease_expires_at` | DATETIME NULL | |
| `last_attempt_at` / `delivered_at` | DATETIME NULL | |
| `last_http_status` | SMALLINT NULL | |
| `last_error_code` | VARCHAR(64) NULL | 只存错误码：`TIMEOUT`、`CONNECTION_ERROR`、`HTTP_STATUS`、`NO_ACTIVE_SECRET`、`SECRET_DECRYPT_FAILED`、`ENCRYPTION_NOT_CONFIGURED`（地址在写入时已校验，投递时不再有地址被拒的错误码） |

- 索引 `(next_attempt_at)`
- `CHECK (attempt_count >= 0)`、`CHECK (delivered_version IS NULL OR delivered_version >= 0)`
- 投递状态不另存列，由数据推导（§29 的四个状态）：
  - `DELIVERED`：`delivered_version = tenant.status_version`
  - `PENDING`：落后且 `attempt_count = 0`
  - `RETRYING`：落后且 `1 ≤ attempt_count < 告警阈值`
  - `FAILED`：落后且 `attempt_count ≥ 告警阈值`，**仍然每隔退避上限重试一次**

### `status_version` 递增点收拢

新增 `bump_status_version(session, tenant, now, *, make_event)`，放在 `app/repositories/tenancy.py`（`wallet.py` 是 repository，只能调用同层或更低层，不能导入 services）。`make_event: Callable[[Tenant], DomainOutbox]` 是必填的。现有的事件构造函数（`account_status._status_event`、`wallet._outbox_event` 等）签名不变，调用方用一个闭包包装它们，payload 也在闭包里组装；bump 在版本 +1 之后才调用这个闭包，所以事件里一定是新版本。闭包在调用时按名字查找模块里的构造函数，现有测试对它们的替换仍然生效。一次调用做完四件事：

1. 版本 +1；
2. `status_changed_at = now`；
3. 调用 `make_event(tenant)` 得到一条 `domain_outbox` 事件，加进同一个 session；
4. 把该租户所有投递行的退避作废：先经 `projects` 上已有的 `ix_projects_tenant_id` 取该租户的项目 id（一致性读，不加锁；没有项目就跳过这一步），再按主键 `UPDATE project_status_deliveries SET next_attempt_at = now WHERE project_id IN (…) AND next_attempt_at > now`。每个项目恰好有一行投递行，所以 IN 里的每个 id 都命中已存在的主键记录：InnoDB 只加记录锁、不加间隙锁，也不锁其他租户的行；读取之后才建的项目，其投递行插入时就是 `next_attempt_at = now`。新版本因此不受旧版本失败退避的影响，下一轮 beat（≤ 15 秒）即可领取，满足 §119「首次投递尝试 p95 ≤ 60 秒」。正在租约中的行不受影响，由结算规则接住（见 §4「投递行」）。

**没有「只加版本、不写事件」的入口**，所以每次跃迁都有一条与状态原子提交的持久领域事件（不变量 13）。现有三处递增点和新的项目集成状态跃迁一律改为调用它，调用都发生在已持有租户行锁的事务里：

| 递增点 | 事件类型 | payload |
| --- | --- | --- |
| `wallet.py` `_apply_billing_status` | `tenant.billing_status_changed`（已有） | 不变 |
| `account_status.py` `_apply` | `tenant.account_status_changed`（已有） | 不变 |
| `internal_billing.py` `enable_internal_metering` | `tenant.billing_mode_changed`（**新增**，补上现在漏写的那一条） | `billing_mode`、`previous_billing_mode`、`effective_status`（`compose_effective_status`，项目维度按 `ENABLED`，与已有两种租户级事件一致）、`status_version`、`changed_at`（与 `tenant.account_status_changed` 同格式）；`aggregate_type = tenant`、`aggregate_id` = 租户 public_id；`status = PENDING`、`next_retry_at = now`；构造函数放在 `internal_billing.py`，写法照 `account_status._status_event` |
| 项目集成状态跃迁（新） | `project.integration_status_changed`（新） | `project_id`、`integration_status`、`previous_integration_status`、`effective_status`、`reason_code`、`status_version`、`changed_at`；`aggregate_type = project`、`aggregate_id` = 项目 public_id |

- 新增的两种事件与已有两种一样，目前没有处理器，以 `PENDING` 持久等待 Phase 6 的通知处理器。
- payload 不带管理员原因：原因只进审计。
- 投递不消费这些事件，只看版本差。

### 事务边界

- **项目集成状态跃迁**：一个 `session_scope`。先锁租户行，再锁项目行（`SELECT … FOR UPDATE`）→ 判定 → 改项目 → `bump_status_version` → 审计 → 提交。
  - `reason` 在请求模型与服务函数里都按「去首尾空白后 1–255」校验
  - 审计 `PROJECT_INTEGRATION_STATUS_CHANGED`：`entity_type = project`、`entity_id` = 项目 public_id；`before_state` / `after_state` 只有 `integration_status` 与 `status_version`；`reason` 进审计的 `reason` 字段
  - 现有加锁顺序有两条：钱包 → 租户（记账）、只锁租户（账户状态）；webhook 密钥那条只锁项目
  - 本路径是租户 → 项目。没有任何路径「先锁项目、再锁租户」，因此不会成环
- **设置 URL**：一个事务。锁租户行（`FOR SHARE`，读 `account_status`）→ 锁项目行 → 设为地址而租户 `CLOSED` 时 409 `ACCOUNT_CLOSED`（清除地址不受此限，关户的客户也能停止推送）→ 规范化后与当前值相同就直接返回 → 改 URL → UPDATE 该项目的投递行（`delivered_version = NULL`、`attempted_version = NULL`、`lease_token = NULL`、`lease_expires_at = NULL`、`attempt_count = 0`、`next_attempt_at = now`）→ 审计。
  - 清空 `lease_token` 是为了作废在途租约：发往旧地址的请求即使随后返回 2xx，结算时令牌也对不上，什么都不写，所以新地址的游标不会被旧地址的送达推进。
  - 投递行可以立刻被重新领取，向新地址发送；旧地址同时可能还收到一次在途请求，这是旧集成方的事，不影响新地址。
  - 两个事务争的是投递行的行锁：改 URL 持锁时，领取（`FOR UPDATE SKIP LOCKED`）跳过这一行，下一轮读到新 URL；领取先提交，改 URL 就清掉它的令牌。领取在同一事务里读 URL，发出的请求一定发往领取时那个令牌对应的 URL。
- **dispatch 循环**：逐行处理——领取一行 → 发送 → 结算，再领下一行，直到没有到期行或用完 `status_webhook_dispatch_budget_seconds`（12 秒）。结算后又到期的行（例如有了更新版本），同一次 dispatch 可以再次领取。租约从这一行被领取时算起，60 秒远大于单次 5 秒超时，所以不会出现「一批里排在后面的行租约先过期、被别人重复领取」。
- **领取**：一个短事务。`SELECT … FOR UPDATE OF project_status_deliveries SKIP LOCKED LIMIT 1`（只锁投递行，JOIN 进来的租户与项目是一致性读、不加锁）；同一事务里读出租户与项目的状态快照，组装好 payload → 写 `lease_token`、`lease_expires_at = now + 60s`、`attempted_version` = 快照里的版本 → 提交。快照和版本出自同一次读取，二者一定对应。
- **发送**：在事务外，发送领取时组装的 payload。发送期间版本又变了也不改发：本轮送达旧版本，下一轮按版本差再发新版本。
- **结算**：一个事务，顺序固定：
  1. `SELECT status_version FROM tenants WHERE id = ? FOR SHARE`：对租户行加共享锁，读**当前已提交**的版本；
  2. 带令牌的条件 UPDATE 投递行（`WHERE project_id = ? AND lease_token = ?`），退避按第 1 步读到的版本决定（见 §4「投递行」）；
  3. 提交。令牌不匹配（租约已被别人接手）就什么都不写。

  `bump_status_version` 持有租户行的排他锁，与第 1 步的共享锁互斥，所以两者严格串行，不存在「结算读到旧版本、随后覆盖新版本的立即重排」：
  - 跃迁先拿到租户锁：结算等它提交，读到新版本，`next_attempt_at = now`；
  - 结算先拿到租户锁：结算按旧版本写入退避并提交，跃迁随后拿到锁，把 `next_attempt_at` 提前到 now。

  两条路径都是先锁租户、后锁投递行，不会互相死锁。

### 外部系统与异步边界

- 出站 HTTP 沿用 BNM 适配器的写法：标准库 `urllib`、自定义 opener 禁止重定向、可注入的 transport（测试用）、超时由配置给出
- 响应体最多读 1 KiB 后丢弃，不存、不记日志
- 只把 2xx 视为送达；3xx、4xx、5xx、超时、连接错误一律重试

### 时间

- `status_changed_at`、`effective_at`、投递表各时间列：服务端 `now`，UTC 无时区，截到整秒（与 020 一致）
- 签名时间戳是每次发送时刻的 Unix 秒（api.md 已定）
- **集成方的对账契约**（写进 api.md，由 `acuven_ai_api` 实现与测试）：
  - 后台每 4 分钟调一次 `effective-status`，单次请求超时 10 秒；不在 AI 调用路径上（§30）
  - 失败（超时、非 200、应答校验不过）后 30 秒重试一次，之后回到 4 分钟节奏
  - Billing Hub 可用时的最坏滞后：4 分钟 + 10 秒超时 + 30 秒重试 + 10 秒 = 4 分 50 秒 < 5 分钟（§119；§30：等于上限不算满足）。可以容忍一次偶发失败
  - 连续失败（Billing Hub 不可用）时，任何设计都给不出 5 分钟上限。此时由不变量 1 约束：保留最后一次接受的状态，从未拿到过状态就放行；最后一次成功对账早于 5 分钟时，集成方发出自己的健康告警
  - 版本应用规则与 webhook 相同：版本更大才应用

### 金额

不适用。不读、不改任何金额。payload 不带余额。

### 配置（`BILLING_` 前缀，带默认值）

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `status_webhook_timeout_seconds` | 5 | 单次 POST 超时 |
| `status_webhook_retry_base_seconds` | 30 | 退避 = min(base × 2^(n−1), 上限) |
| `status_webhook_retry_cap_seconds` | 3600 | 退避上限 |
| `status_webhook_lease_seconds` | 60 | 单行租约，大于单次超时 5 秒 + 签名开销 |
| `status_webhook_dispatch_budget_seconds` | 12 | 一次 dispatch 的时间预算，小于 beat 间隔 |
| beat 间隔 | 15 秒 | `expires = 14` |

### 健康告警维度（`GET /internal/alerts`，接在现有维度之后）

**只加一个维度 `status_webhook`**：外部监控是每个维度一个 Healthchecks 检查，现有用量 18、免费档上限 20（`docs/runbook.md`）。三类问题合在一行，级别取最严重的那类。

只统计 `integration_status = ENABLED` 且配了地址的项目；停用或没有地址的项目一律不计（它们不推送）。摘要不含租户与项目 id。

| 计数 | 统计 |
| --- | --- |
| `failing` | 落后且 `attempt_count ≥ 6` 的项目数。阈值 6 是 `app/services/alerts.py` 里的常量，与现有维度的阈值写法一致，不进配置。按默认退避，第 6 次失败在第一次失败后约 15 分钟（30 + 60 + 120 + 240 + 480 = 930 秒） |
| `missing secret` | 由数据推导：没有 `ACTIVE` 签名密钥的项目数（不看 `last_error_code`） |
| `signing error` | 落后且 `last_error_code` 为 `SECRET_DECRYPT_FAILED` 或 `ENCRYPTION_NOT_CONFIGURED` 的项目数 |

- 级别：`signing error > 0` 为 P1（加密配置坏了）；否则 `failing > 0` 或 `missing secret > 0` 为 P2；否则 OK。
- 摘要逐字：OK 时 `no status webhook problem`；否则 `failing <a>, missing secret <b>, signing error <c>`（三个都是十进制整数，不做单复数变化）。

`docs/api.md` 的 `GET /internal/alerts` 一节同步列出这个维度；`docs/runbook.md` 补上新建 Healthchecks 检查与 `BILLING_HEALTHCHECK_ALERT_STATUS_WEBHOOK_URL`（`deploy/monitor.sh` 按维度名通用处理，不用改），用量变为 19。

## 3. 不变量影响矩阵

| 不变量 | 是否触碰 | 可能怎样被破坏 | 设计控制 | 验证方式 |
| --- | --- | --- | --- | --- |
| INV-1 中心故障不中断 AI | **是** | 投递失败或中心宕机导致集成方拦截 | 投递失败不改任何状态；契约规定集成方没有本地状态时放行、有本地状态时按最后一次接受的状态执行（§26）；对账在后台运行，不阻塞 AI（§30） | 投递失败用例断言租户 / 项目状态与版本不变；api.md 契约写明 |
| INV-2 事件不重复扣费 | 否 | 不碰用量与扣费；记账路径只把版本递增改成调用 helper | helper 只替换两行赋值 | 现有扣费测试全过 |
| INV-3 支付不重复入账 | 否 | 还没有支付 | — | — |
| INV-4 余额只经账本变动 | 否 | 不读写钱包 | 新服务不导入钱包模型 | 项目状态跃迁用例断言钱包 `version` 不变 |
| INV-5 历史账本不可变 | 否 | 不碰账本 | — | — |
| INV-6 事件保留版本引用 | 否 | 不碰用量事件 | — | — |
| INV-7 客户不可见成本毛利 | **是** | payload 或响应带出余额、成本、管理员原因 | payload 白名单只有上面 11 个字段 | 序列化用例断言键集合完全相等 |
| INV-8 租户不可互访 | **是** | 把 A 项目的状态推给 B 的 URL；用 A 的凭据读到 B 的状态 | 投递行以项目为主键，payload 从该行的项目与租户组装；URL 只从同一行读；签名密钥按项目取；`effective-status` 的身份只来自凭据 | 两租户各一个项目的投递用例，断言 URL、签名版本、payload 归属；凭据越权用例 |
| INV-9 对话内容不入库 | 否 | 不处理对话 | — | — |
| INV-10 金额用 Decimal | 否 | 无金额 | — | — |
| INV-11 event_id 至多一次财务效果 | 否 | webhook 的 `event_id` 不是用量事件 id，也没有财务效果 | 前缀 `sevt_` 与用量事件 id 区分 | — |
| INV-12 定稿对账单不可变 | 否 | 还没有对账单 | — | — |
| INV-13 状态与事件原子提交 | **是** | 状态改了而版本没加，导致漏推；版本加了而状态回滚，导致推送一个不存在的版本；跃迁没有持久领域事件（现在 `internal_billing.py` 就漏写） | 状态、版本、`status_changed_at`、审计、领域 outbox 事件同一事务；事件由 `bump_status_version` 写，没有不写事件的递增入口。webhook 投递从已提交的版本差推导，不另需投递事件 | 审计或 outbox 写入失败时的回滚用例（SQLite 与 MySQL 各一次）；四个递增点各一个用例，断言恰好一条对应类型的 outbox 行、payload 的版本等于新版本，且跃迁后投递被领取；测试扫描 `app/` 下的源码（不含 `tests/`），断言匹配 `status_version\s*(\+=|=\s*\S*status_version\s*\+)` 的语句只出现在 `app/repositories/tenancy.py` 的 `bump_status_version` 里 |
| INV-14 队列丢失不毁持久工作 | **是** | Redis / Celery 丢消息后漏推 | 待推送状态完全在 MySQL（版本差 + 投递行），beat 每 15 秒全表扫描到期行；不靠任何队列消息携带状态 | 用例：不经 Celery，直接调用 `dispatch` 就能补发；租约过期后能被重新领取 |

## 4. 状态与并发

**项目集成状态**：

| 当前 | 目标 | 前置条件 | 新状态 | 副作用 | 非法时 |
| --- | --- | --- | --- | --- | --- |
| `ENABLED` | `DISABLED` | 租户未 `CLOSED` | `DISABLED` | 版本 +1、`status_changed_at`、审计 `PROJECT_INTEGRATION_STATUS_CHANGED` | — |
| `DISABLED` | `ENABLED` | 租户未 `CLOSED` | `ENABLED` | 同上 | — |
| 任意 | 任意（含与当前相同） | 租户 `CLOSED` | — | — | 409 `ACCOUNT_CLOSED` |
| 任意 | 与当前相同 | 租户未 `CLOSED` | 不变 | 无（200，不写） | — |

判定顺序：请求体校验（422，含 `reason`）→ 客户与项目查找（404）→ 租户 `CLOSED`（409，即使目标等于当前）→ 目标与当前相同（200 不写）→ 按上表跃迁。

- 停用项目**不吊销凭据、不拒绝摄取**：在途用量照常入账，与账户 `DISABLED` 的契约一致（020 §2）。
- 停用项目**不推送**（§25；Kelvin 2026-10-09 拍板，见 §10）：使项目停用的那一次跃迁也不推送。集成方经周期对账（≤ 4 分钟 + 请求延迟，满足 §119 的 5 分钟上限）拿到 `BLOCK_AI` / `PROJECT_INTEGRATION_DISABLED`；停用项目的凭据仍有效，所以对账可用。重新启用时版本 +1，下一轮即推送当前状态。

**投递行**（每个项目一行）：

| 当前 | 事件 | 条件 | 结果 |
| --- | --- | --- | --- |
| 落后、到期、无租约或租约过期 | 领取 | — | 写租约令牌与 `attempted_version` |
| 已领取 | 2xx | 令牌匹配 | `delivered_version = GREATEST(COALESCE(delivered_version, -1), attempted_version)`，`attempt_count = 0`，清租约与 `attempted_version`，记 `delivered_at` 与 `last_attempt_at`；`last_error_code = NULL`、`last_http_status` = 本次状态码（告警随之解除）；`next_attempt_at = now`（版本已追上时扫描条件不成立，不会重发；没追上时下一轮立即发新版本） |
| 已领取 | 非 2xx、超时、连接错误 | 令牌匹配 | `attempt_count +1`（退避公式里的 n 就是加一后的值），清租约与 `attempted_version`，记错误码，`last_http_status` = 收到的状态码（没有 HTTP 响应时为空），`last_attempt_at = now`；`next_attempt_at = now + 退避`，但结算第 1 步在共享锁下读到的租户版本大于 `attempted_version` 时取 `now`（有更新的版本待发，不继承旧版本的退避） |
| 已领取 | 无 `ACTIVE` 密钥 | 令牌匹配 | 不发送；错误码 `NO_ACTIVE_SECRET`，`last_http_status` 为空，计数、退避与其余列同上 |
| 已领取 | 有 `ACTIVE` 行，但加载主密钥抛 `EncryptionNotConfigured`、或解密抛 `DecryptionFailed` | 令牌匹配 | 判定顺序固定：先查有没有 `ACTIVE` 行（只读、不解密），没有就是上一行；有才加载主密钥、再解密。不发送；错误码 `SECRET_DECRYPT_FAILED` / `ENCRYPTION_NOT_CONFIGURED`，计数与退避同上；异常在 worker 内捕获并结算，不让租约反复过期 |
| 任意 | 管理员启用一把新的 webhook 密钥 | — | 同一事务里把该项目投递行的 `next_attempt_at` 置为 now，并清空租约与 `attempted_version`；在途结算因令牌不匹配不写，不会再用退避把它覆盖。恢复后下一轮即发送 |
| 已领取 | 任意结算 | 令牌不匹配 | 不写（租约已被接手） |
| 任意 | 项目 `integration_status = DISABLED` | — | 不领取；投递行原样保留（含落后的版本），重新启用后照常领取 |
| 任意 | 管理员改 URL | — | `delivered_version = NULL`、`attempted_version = NULL`、清租约（令牌与到期时间）、`attempt_count = 0`、`next_attempt_at = now`；在途请求之后的结算因令牌不匹配而不写 |

- **串行化**：同一项目的投递靠行锁 + 租约，同一时刻只有一个发送方。租约 60 秒，远大于 5 秒超时；即使慢请求跨过了租约，接手方发的也是同一版本或更新的版本，集成方按版本应用，结果仍然正确。
- **加锁顺序**：`bump_status_version` 在租户行锁内更新投递行（租户 → 投递行）；项目集成状态跃迁是租户 → 项目 → 投递行；改 URL 是租户（共享锁）→ 项目 → 投递行；启用 webhook 密钥是项目 → 投递行；建项目是插入项目行与它的投递行；领取只锁投递行（`FOR UPDATE OF`），租户与项目只做一致性读；结算先对租户行加共享锁、再更新投递行（租户 → 投递行，与 `bump_status_version` 同序）。没有任何路径先锁投递行、再锁租户或项目，因此不成环。现有的钱包 → 租户顺序不变。
- **数据库保证的唯一性**：投递表以 `project_id` 为主键；`status_version` 的单调性来自「只在租户行锁内经 helper 递增」。
- **幂等键**：webhook 是 `(project_id, status_version)`，体现在 `event_id` 上。集成方的应用规则：版本更大才应用，同版本幂等，旧版本回 2xx 后忽略。
- **相同 ID、不同载荷**：同一版本的状态快照唯一，正常情况下不会出现；万一出现，集成方按「同版本幂等」处理，不覆盖已应用的状态。
- **重试层**：只在 Billing Hub 的投递行里重试；集成方不需要回调。
- **原子提交**：状态 + 版本 + 时间 + 审计；投递行的领取和结算各自是独立的短事务。

## 5. 失败与恢复矩阵

| 失败点 | 对外结果 | 数据最终状态 | 是否可重试 | 恢复来源 | 告警 |
| --- | --- | --- | --- | --- | --- |
| 管理接口校验失败（URL 不合规等） | 422 | 无改动 | 改请求后可 | — | 无 |
| 项目状态跃迁中途 DB 失败 | 500 | 整体回滚，版本不变 | 可 | — | 错误日志 |
| 领取后 worker 崩溃 | — | 租约 60 秒后过期 | 自动 | 下一次 beat 重新领取 | 无 |
| 集成方超时 / 5xx / 4xx | — | `attempt_count +1`，退避 | 自动，**不设上限** | beat 扫描 | 连续 ≥ 6 次进健康告警 |
| 集成方返回 3xx | — | 视为失败（不跟随重定向） | 自动 | 同上 | 同上 |
| 没有 `ACTIVE` 签名密钥 | — | 不发送，`NO_ACTIVE_SECRET` | 管理员启用密钥后自动发 | beat 扫描 | `status_webhook` 的 `missing secret` 计数 |
| 密钥解不开（密文损坏、AAD 不符） | — | 不发送，`SECRET_DECRYPT_FAILED`，退避重试 | 管理员签发并启用新密钥（旧 `ACTIVE` 同时退役），启用时把 `next_attempt_at` 置为 now | beat 扫描 | `status_webhook` 的 `signing error` 计数（P1），错误日志只记项目 id 与 `key_version` |
| 主密钥未配置 | — | 不发送，`ENCRYPTION_NOT_CONFIGURED`，退避重试 | 运维补上主密钥文件并重启；退避上限 1 小时内自动恢复 | beat 扫描 | 同上 |
| 结算时 DB 失败 | — | 租约留着，60 秒后过期重发 | 自动 | 集成方按版本幂等 | 错误日志 |
| 发送期间版本又变了 | — | 送达的是旧版本，`delivered_version < tenant 版本` | 自动 | 下一轮发新版本 | 无 |
| Redis / Celery 丢失 | 推送暂停 | 版本差和投递行都在 MySQL | 恢复后自动 | beat 全表扫描 | 现有 worker 健康项 |
| 中心整体宕机 | 集成方收不到推送、对账失败 | 集成方保留最后一次接受的状态 | 恢复后自动 | 推送 + 对账 | 集成方侧监控 |
| 重复请求（管理接口） | 200 不写 | 不变 | — | — | 无 |
| 项目被停用 | 不推送 | 投递行不再被领取 | 重新启用后自动 | 集成方周期对账 | 无（停用是管理员的有意操作） |

## 6. 数据与安全边界

- **租户过滤**：管理接口只按路径 `customer_id` → `project_id` 取（项目不属于该客户时 404，与不存在无法区分）。投递行的项目、租户、URL、密钥来自同一个项目。`effective-status` 的身份只来自凭据。
- **鉴权主体**：两个新管理接口只有 ADMIN；集成接口沿用 HMAC 凭据；没有客户侧接口。
- **禁止返回的字段**：payload 与 `effective-status` 不带余额、阈值、成本、毛利、管理员原因、内部 id。`reason_code` 只给集成方后端，不给终端用户；api.md 写明集成方不得把它展示给终端用户（§26）。
- **SSRF**：URL 只能由 ADMIN 设置。校验规则见 §2「接口」的 URL 规范化与校验（https、端口 443、IP 字面量含各种简写一律拒绝、不带用户信息）；发送时禁止重定向。**不做** DNS 解析后的内网地址拦截（见 §10「尚未验证的假设」）。
- **日志 / 审计 / 异常**：
  - 日志只记项目 public_id、版本、HTTP 状态、错误码，不记 URL、签名、密钥、响应体
  - 审计只记 URL 的主机名
  - 异常信息不带 URL
- **密钥**：每次发送时经 `signing_material` 解密（ADR-0004）。不缓存明文，不进日志。轮换期间集成方同时接受新旧版本（api.md 已写）。
- **个人数据 / 对话内容**：不涉及。
- **保留**：投递表每个项目一行、原地更新，不会增长；审计永久保留。

## 7. 测试证据计划

| 风险 / 需求 | 层级 | 场景 | 预期 |
| --- | --- | --- | --- |
| 合成规则 | unit | 4 维取值全组合（4×2×2×2 = 32 组） | 每组的 `effective_status` 与 `reason_code` 与 §2 表一致；内部计量 + 余额停用 → `ALLOW_AI` |
| 优先级 | unit | 账户 `DISABLED` + 项目 `DISABLED` + 余额停用 | `ACCOUNT_DISABLED` |
| 公式只有一份 | unit | `ai_service_enabled` 与本函数（项目 `ENABLED`）逐组对比 | 全等 |
| 正常推送 | integration | 设 URL + 启用密钥 → 扣费导致停用 → 调 `dispatch`（注入 transport） | 收到 1 次 POST：四个头可按 api.md 验签；payload 键集合 = 11 个字段；`BLOCK_AI` / `BALANCE_NOT_POSITIVE`；`delivered_version` = 新版本 |
| 四个递增点 | integration | 余额、账户、内部计量、项目状态各触发一次 | 每次版本 +1、`status_changed_at` 更新，`dispatch` 都会推送 |
| 退避期间出现新版本 | integration | v5 连续失败到退避上限（`next_attempt_at` = 1 小时后）→ 租户跃迁到 v6 → 调 `dispatch` | `bump_status_version` 把 `next_attempt_at` 置为 now；本轮即领取并发送 v6 |
| 在途时出现新版本 | integration | 领取 v5（transport 阻塞）→ 跃迁到 v6 → v5 返回 500 并结算 | 结算读到 v6 > 5，`next_attempt_at = now`；同一次 dispatch 在预算内可以立即再领取这一行并发送 v6，否则下一轮发送 |
| 结算与跃迁并发（两种提交顺序） | MySQL，两个连接 | ① 连接 A 执行 `bump_status_version` 到 v6 但不提交；连接 B 对 v5 做失败结算（应阻塞在 `FOR SHARE`）；A 提交后 B 继续；② 连接 B 先完成失败结算并提交，连接 A 再跃迁到 v6 并提交 | 两种顺序结束后都是 `next_attempt_at ≤ now`，下一轮即领取并发送 v6；①中 B 确实等到 A 提交才返回；无死锁 |
| 加锁不成环 | MySQL | `dispatch` 领取与 `bump_status_version`、改 URL 并发 | 无死锁；领取跳过被锁的投递行 |
| 对账契约 | 文档 + `acuven_ai_api` | api.md 写明周期、超时、重试与告警；超时路径、30 秒重试、滞后告警由 `acuven_ai_api` 的实现任务测试 | 本仓库只交付契约文字与 `effective-status` 本身；集成方测试列为该任务的验收标准 |
| 失败退避 | integration | transport 依次返回 500、超时、302、200 | `attempt_count` 1、2、3 后清零；`next_attempt_at` 按公式；302 不跟随 |
| 不设上限 | integration | 连续失败 10 次 | 仍会被领取，`attempt_count` 继续增长、`next_attempt_at` 停在退避上限（告警断言归「告警范围」一行） |
| 缺密钥 | integration | 有 URL、无 `ACTIVE` 密钥 | 不发 POST；`NO_ACTIVE_SECRET`；启用密钥后下一轮送达 |
| 密钥解不开 | integration | `ACTIVE` 行的密文被改坏（或 AAD 换成别的项目） | 不发 POST；`SECRET_DECRYPT_FAILED`、`attempt_count +1`、租约清空；签发并启用新密钥后，`next_attempt_at = now`，下一轮送达 |
| 主密钥未配置 | integration | keyring 加载抛 `EncryptionNotConfigured` | 不发 POST；`ENCRYPTION_NOT_CONFIGURED`；同上计数与告警 |
| URL 规范化 | unit | 大写 scheme 与主机名、末尾点、IDN 主机名、`:443`、`127.1`、`2130706433`、`0x7f.1`、`[::1]`、`10.0.0.1`、`http://`、`:8443`、userinfo、fragment、超长 | 前四类规范化后接受；其余 422 |
| 同一地址 | integration | 规范化后与当前相同（例如只差大小写）再 PUT；两次清除 | 200，不重置游标、不写审计 |
| 清除从未设过的地址 | integration | 项目从未设过地址，PUT `null` | 200，不写库、不写审计 |
| 每个项目恰好一行 | integration + MySQL | 迁移 0020 升级已有两个项目的库；之后新建一个项目 | 三个项目各有一行投递行；建项目失败回滚时投递行也不在 |
| dispatch 预算 | integration | 30 行到期，transport 每次耗时 1 秒（伪时钟） | 一次 dispatch 只处理到 12 秒预算为止；每行的租约从领取时算起；没有一行被重复领取 |
| 地址读取 | integration | GET status-webhook | 返回规范化 URL 与 `status_webhook_configured`；不写审计；非 ADMIN 403；越权 404 |
| CLOSED 优先 | integration | 租户 `CLOSED`（直接改库造数据），目标与当前相同 | 409 `ACCOUNT_CLOSED` |
| 成功清除错误 | integration | `SECRET_DECRYPT_FAILED` 若干次 → 启用新密钥 → 送达 | `last_error_code` 为空（告警回到 OK 的断言归「告警范围」一行） |
| 告警范围 | integration | 一个 ENABLED 项目失败 6 次、一个 DISABLED 项目失败 6 次、一个没有地址的项目；再造缺密钥与 `SECRET_DECRYPT_FAILED` 各一个；之后送达一次清除错误 | `failing` 只计第一个；各组合下级别与摘要逐字符合 §2「健康告警维度」；清除错误后 `signing error` 归零 |
| 退避重排只锁本租户 | MySQL | A 租户的事务执行 `bump_status_version` 不提交，同时 B 租户执行 `bump_status_version`，并直接按主键 UPDATE 自己的投递行（模拟设置地址，本测试不依赖设置地址的服务） | B 不被阻塞 |
| 只有一个递增入口 | unit | 扫描 `app/` 源码 | 只有 `app/repositories/tenancy.py` 命中 |
| 改 URL | integration | 已送达 v5 → 改 URL | 新 URL 收到 v5 |
| 首次送达 | integration + MySQL | 新设 URL（`delivered_version` 为空）→ 送达 v3 | `delivered_version = 3`；下一轮不再领取 |
| 改 URL 与在途请求并发 | integration | 领取 v5 发往旧 URL（transport 阻塞）→ 改 URL 提交 → 旧请求返回 200 并结算 | 旧结算不写（令牌不匹配），`delivered_version` 仍为空；下一轮新 URL 收到 v5 |
| 停用不推 | integration | 已送达 v5 → 停用项目（v6）→ 调 `dispatch` → 重新启用（v7）→ 调 `dispatch` | 停用后不发 POST，`delivered_version` 仍为 5；`effective-status` 返回 `BLOCK_AI` / `PROJECT_INTEGRATION_DISABLED`；重新启用后收到 v7 的 `ALLOW_AI` |
| 并发 / 租约 | MySQL | 两个 `dispatch` 同时运行 | 每个项目每轮只发 1 次（SKIP LOCKED） |
| 租约过期 | integration | 领取后不结算，时间推进 61 秒 | 被重新领取；旧令牌的结算不写 |
| 发送期间版本变化 | integration | 领取 v5 → 发送前租户变成 v6 | 先发 v5，`delivered_version = 5`；同一次 dispatch（预算内）或下一轮再发 v6，最终 `delivered_version = 6` |
| 事务中途失败 | SQLite + MySQL | 项目状态跃迁时审计写入抛错 | 状态、版本、`status_changed_at` 全部不变 |
| Redis / Celery 丢失 | integration | 不经 Celery 直接调 `dispatch` | 正常补发 |
| 租户越权 | integration | A、B 两租户各有项目和 URL；只让 A 跃迁 | 只有 A 的 URL 收到 POST，payload 只含 A 的 id，签名用 A 项目的密钥；B 的投递行不变 |
| 管理接口越权 | integration | 用 A 的客户 id + B 的项目 id 调两个新接口 | 404 `PROJECT_NOT_FOUND`，不写库 |
| 非 ADMIN | integration | 两个新接口加入路由枚举用例 | 403 |
| URL 校验 | unit | http、IP 字面量、userinfo、fragment、非 443 端口、超长 | 422 |
| 日志脱敏 | integration | 推送失败一次 | 日志与审计里没有完整 URL、签名、密钥 |
| 接口兼容 | integration | 现有 `effective-status` 用例 | 原字段不变，新增三个字段 |
| 金额精度 | — | 不适用：无金额 | — |

## 8. 迁移与上线

- **迁移 0020**：给 `projects` 加两列（已有行回填 `ENABLED` / NULL），给 `tenants` 加一列（NULL），建 `project_status_deliveries`。生产上项目与租户都只有个位数行，加列的锁表时间可以忽略。
- **兼容窗口**：`effective-status` 只增加字段，`acuven_ai_api` 现有的解析不受影响。
- **部署顺序**：合并即部署（迁移 → api → worker / beat）。部署后没有任何项目配置 URL，不会发出任何请求。
- **部分部署**：beat 先于迁移起来时，查询会因缺表失败，任务报错重试；迁移完成后自动恢复，不留脏数据。
- **回滚**：生产上只前滚。关闭推送的办法是清掉项目的 URL，不需要回滚迁移。
- **0020 的 downgrade**：照 0019 的写法，只在不会丢数据时才执行——没有任何项目是 `DISABLED`、没有任何项目设了地址、没有任何投递行有 `delivered_version`、没有任何租户有 `status_changed_at`；否则抛错拒绝降级。
- **监控**：健康检查新增一个维度 `status_webhook`（见 §2），接入现有告警，Healthchecks 多一个检查。负责人：管理员。
- **拆分**（每个任务只做一层，用 `depends_on` 串起来，每层合并后都能单独通过 CI）：
  1. **数据库**：迁移 0020（含给已有项目回填投递行）、模型、建项目时在同一事务插入投递行。从这一项上线起，每个项目都恰好有一行投递行。
  2. **版本收拢**：`compose_effective_status`、`bump_status_version` 并替换三处递增点，内部计量补写事件。
  3. **业务规则**：项目集成状态跃迁、设置 / 清除地址（规范化与校验）、启用 webhook 密钥时重排该项目的投递。只写服务函数与测试，不开接口。
  4. **接口**：`effective-status` 新增字段、三个管理接口（集成状态、PUT / GET 地址）、项目对象新字段、api.md 的接收契约。
  5. **投递**：dispatch 循环、领取、发送、结算、beat 任务、配置项。
  6. **告警与运维文档**：`status_webhook` 告警维度、api.md 的 `GET /internal/alerts` 一节、runbook。
  7. **前端**：项目集成状态与 webhook URL 的管理页面。不走闸门，与已登记的 046 放在同一面板。
- **上线后的联调**（在 `acuven_ai_api` 仓库，另做）：接收端 + 本地版本化状态 + 按 §2「时间」的对账契约（4 分钟周期、10 秒超时、失败 30 秒重试、滞后超过 5 分钟告警），并测试超时路径。

## 9. 方案取舍

| 方案 | 优点 | 风险 | 未采用原因 |
| --- | --- | --- | --- |
| **采用：每项目一行「最新状态」游标**，由版本差推导要推什么 | 不依赖每个递增点都写对 outbox（现在就有一处没写）；不会积压中间状态；无界重试也不增长存储；补发就是全表扫描 | 中间状态不逐一推送：投递停顿期间先停用再恢复，集成方不知道中间停用过（见下） | — |
| 逐事件投递：每次跃迁 × 每个项目写一行 delivery（spec §29 的字面读法） | 每个跃迁都有一条记录 | 要在四个递增点都写扇出，漏一处就漏推（`internal_billing.py` 已经漏了）；集成方本来就丢弃旧版本，中间状态推了也会被忽略；失败期间每次跃迁都新增一行，按项目数 × 跃迁次数增长 | 推中间状态没有消费方。§29 的「不永久丢失状态变化」的意思是最新状态最终送达，游标方案按版本保证这一点 |
| 给现有 `domain_outbox` 的状态事件加处理器、扇出到项目 | 复用现有表 | 依赖每个递增点都写 outbox（同上）；`domain_outbox` 有 `FAILED` 死信终态，与 §29「不得永久丢失」冲突 | 同上 |
| 新增 §30 的 `GET /account-status`，与 `effective-status` 并存 | 字面符合 spec 路径 | 两个接口返回同一份数据，要同步维护两份 | 两个接口返回同一份数据，维护两份；Kelvin 2026-10-09 拍板只留一个 |
| 用 httpx 发送 | API 友好 | 新增运行时依赖 | `urllib` 的禁止重定向 + 可注入 transport 写法已在 BNM 适配器里验证过 |

**一致性模型（Kelvin 2026-10-09 选 A）**：spec 自己的规则就是「最新状态 + 版本」：集成方只应用更大的版本（§28），对账只返回当前状态（§30），安全上限是本地状态落后不超过 5 分钟（§119）。在这个模型下，逐次投递并不能多挡住任何一段状态：

- 投递停顿期间先停用、再恢复：逐次投递在恢复之后才补发「停用」，集成方先按它拦截、随后又被「恢复」放开，等于在本应放行的时候多拦了一段；游标方案直接送达最新的「恢复」。两种方案在停顿窗口内都没能拦截，这个窗口受 §119 的上限约束，而不是靠投递记录的条数。
- 项目停用本来就不推送（§25，Kelvin 拍板），只靠对账，与投递方案无关。

spec §25、§27、§28、§29、§30、§92 已按这个模型与 §10 的四条决定修订，修订 PR 在本设计复审之前合并（#242）。修订要点：

- §25、§27「创建持久的状态 webhook outbox 记录」→「为跃迁写一条持久的领域 outbox 记录；状态 webhook 的投递由已提交的状态推导，每个启用且配了 URL 的项目最终收到最新 `status_version`」
- §29「Do not permanently lose status transitions」→「每个项目的最新版本最终一定送达；中间版本可以合并，不逐一推送」，并加上「集成方本地状态的最大滞后受 §119 状态对账安全上限约束」
- §29 的四个投递状态保留，由投递行推导（见 §2「数据库」）


## 10. 未决问题与假设

**Kelvin 2026-10-09 已拍板**：

1. **停用的项目不推送**，照 spec §25 原文。项目停用那一次也不推，集成方经周期对账得知（见 §4）。
2. **对账用现有的 `effective-status`**，不新建 spec §30 的 `account-status`；字段名沿用现有写法（`account_status`，不用 `tenant_account_status`）。spec §28 的 payload 示例与 §30 的路径和字段名已由 #242 修订。
3. **不用 spec §92 的事件类型名**：payload 是完整状态快照，集成方按 `effective_status` 与 `reason_code` 处理。§92 已由 #242 修订。
4. **投递采用「最新状态」游标，不逐次持久投递**（v1 审查后选 A）：spec 已由 #242 修订，不变量 13 由每次跃迁的领域 outbox 事件保证（§2）。

**尚未验证的假设**：

- 集成方的 URL 解析到公网地址。我们不做 DNS 解析后的内网地址拦截（SSRF 的第二层防线），理由是 URL 只有 ADMIN 能设，而且目前唯一的集成方是自家服务。将来开放客户自助配置 URL 时，必须先补这一层。写进 TODO「后续计划」。
- 6 次连续失败（约 15 分钟）作为告警阈值是否合适，等真实流量再调。如果假设错了，后果是告警过早或过晚，不影响正确性。

**需要谁拍板**：产品口径已由 Kelvin 拍板（上面 1–3）；其余由 Codex 审查。

## 11. 审查与版本绑定

审查方要回答的五个问题见 [scripts/review_checklist.md](../../scripts/review_checklist.md) 的「设计审查」一节。批准必须写成 `APPROVED: design v<N>`；设计版本一变，之前的批准作废（[WORKFLOW §3](../WORKFLOW.md)）。

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
| v1 | 2026-10-09 | 初稿 | — |
| v2 | 2026-10-09 | 结算对空游标取 `COALESCE(…, -1)`；改 URL 同时清租约，作废在途请求的结算，并补两条测试；写明一致性模型与 spec §25、§27、§29 的修订措辞（Kelvin 选 A） | Codex 对 v1 的三个阻断项 |
| v3 | 2026-10-09 | `bump_status_version` 同时写领域 outbox 事件，补上 `internal_billing.py` 漏写的那一条并新增项目集成状态事件（不变量 13）；签名密钥解不开 / 主密钥未配置的结算、告警、恢复与测试；启用新密钥时立即重排投递；spec 修订改由 #242 先行合并 | Codex 对 v2 的两个阻断项 |
| v4 | 2026-10-09 | `bump_status_version` 同时作废该租户投递行的退避；结算时有更新版本则立即重排；领取只锁投递行并写明加锁顺序；集成方对账契约补上单次超时、失败重试、最坏滞后算式与连续失败时的告警；补四条测试 | Codex 对 v3 的两个阻断项 |
| v5 | 2026-10-09 | 结算事务先对租户行加共享锁读当前版本，再更新投递行，与 `bump_status_version` 串行且同序加锁；补两种提交顺序的并发测试 | Codex 对 v4 的阻断项 |
| v6 | 2026-10-09 | 登记预审（design_precheck）发现后修订：§2 概要图与 v3 的领域事件对齐；`compose_effective_status` 放 models、`bump_status_version` 放 repositories 并改为由调用方传入事件构造函数；退避重排改为按主键更新，不在投递表上做范围扫描；项目集成状态的判定顺序（CLOSED 先于相同目标）；地址的规范化、IP 字面量判定、相同地址不写、清除且无行不插入、审计字段；PUT 响应形状并新增 GET 地址接口；接收方时间窗 ±300 秒；成功结算清除错误码；三个告警维度的统计范围、级别与摘要；去掉 `URL_REJECTED`；拆分改为六项；补九条测试。第二轮预审后又补：每个项目恰好一行投递行（迁移回填、建项目时插入，之后只 UPDATE），消除间隙锁与先查后插的死锁；`make_event` 由调用方闭包包装现有构造函数；dispatch 逐行领取并设 12 秒预算；告警阈值的时间改为约 15 分钟；结算各列的写法与密钥判定顺序；启用密钥同时清租约；地址规范化的路径、查询串与 IDNA 细节；设为地址在 CLOSED 时 409、清除不受限；PUT 请求体与错误；事件聚合与审计字段 。第三轮预审后再补：数据库与建项目插行单独成第一项（拆分改为七项）；地址的主机名非空、路径与查询串字符集、校验顺序；内部计量事件的聚合与构造位置；dispatch 可在同一次循环里再领取；三个告警合并为一个维度 `status_webhook`（Healthchecks 免费档上限），阈值改为 alerts.py 常量、摘要逐字。第四轮预审后再补：地址原始输入在拆分前查空格与控制字符、主机名 LDH 字符集；0020 的 downgrade 规则；一条测试的写法。第五轮预审后再补：downgrade 的拒绝条件加上 `status_changed_at`；地址里的 `@`、`#` 与空端口；集成状态接口的完整判定顺序 | 预审对 v5 的 NOT_READY |

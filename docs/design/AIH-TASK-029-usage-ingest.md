# AIH-TASK-029 设计：用量摄取端点：签名、防重放、持久化 202、全局幂等与冲突（已批准 v8）

> **来源**：设计闸门 Issue #176。本文件是 `APPROVED: design v8` 那一版正文的**逐字副本**，审查者为 Codex。
> 放进仓库，是因为 OpenClaw Worker 在沙箱里不联网、读不到 GitHub Issue。
> **实现以本文件为准**；与 Issue 不一致时，以 Issue #176 上被批准的 v8 为准。设计要改，就回到 Issue 升版本、重新过闸门，不要直接改本文件。
> 正文里的「T-E」即 [PHASE-2-plan.md](PHASE-2-plan.md) 的占位名；写「编号登记时分配」的实现任务，登记为 `AIH-TASK-029`。正文提到的其他占位名（T-A…T-L）对应的登记编号见 `docs/TODO.md` 的 Phase 2 一节。
> 与 Issue 正文的唯一差别：一处链接目标由 GitHub 网页上的 `../blob/main/…` 改成仓库内的相对路径，链接文字与其余正文逐字相同。

---

## 0. 准入判定

| 任务涉及 | 要填的范围 |
| --- | --- |
| 钱包 / 账本 / 定价 / 汇率 / 支付 / 幂等 / 状态机 | **全部章节** |
| 用量摄取 / 认证与会话（集成侧与平台侧）/ Webhook | §1–§7（**§6 不可省**） |
| 前端 / 文档 / CI / 脚本 | 不走本闸门 |
| 既不碰钱、也不碰用量摄取 / 认证与会话 / Webhook 的后端改动 | 不走本闸门 |

本任务档位：`全部`。它是用量摄取与集成认证的入口，并且是全局 `event_id` 幂等（spec §23）的数据库层实现 —— 同一个事件至多一次财务效果（INV-11）从这里开始。

---

状态：`READY_FOR_REVIEW`
负责人：Claude
**设计版本**：`v8`（v1：token 四列的 CHECK 用了链式 `=` → v2 显式条件 + 形态复合外键；v2：`unit` 与 `quantity_kind` 只在应用层校验 → v3 复合外键扩到单位与数量类型；v3：凭据与租户 / 项目没有绑在同一个外键里 → v4 三列复合外键；v4：`UNKNOWN_USAGE_TYPE` 的可重试语义前后不一 → v5 改为 409 并定死「只看 `retryable`」；v5：必需列没有写明 NOT NULL → v6 逐列写明；v6：缺 §79 的 `provider_id` / `model_id`，nginx 的 413 不是信封 → v7 补列与外键、nginx 返回 JSON 413；v7：401 标可重试会无限重试永久失败、`event_id` 未限定格式 → v8 401 不可重试、`event_id` 限 UUIDv7 / ULID，见 §12）
对应需求：spec §11、§12、§13、§20、§23、§36、§37、§38、§79、§82（接收部分）、§83、§107、§113、§114、§119；`REQ-INGEST-001`、`REQ-INGEST-002`、`REQ-IDEMP-001`、`REQ-AUTH-001`（重放证据）、`REQ-PRIV-001`、`REQ-AVAIL-001`
目标 PR：待开（批准后在 `.platform/tasks.yaml` 登记实现任务，编号登记时分配）
Phase 2 计划：`docs/design/PHASE-2-plan.md` 的 T-E；依赖 `docs/design/AIH-TASK-025-ai-catalog.md`（已批准 v4）§2「对下游任务的契约」T-E 一行

## 1. 目标与边界

- **要解决的问题**：集成方（应用后端）现在没有任何上报用量的入口。spec §20 要求中心侧先**持久接收**用量事件、返回 `202`，定价与扣费异步进行；§23 要求 `event_id` 全局唯一、重复请求安全、同 ID 不同内容判为冲突且无财务效果；§37 要求签名请求带防重放。AIH-TASK-012 已建好凭据与验签库，但明确把「接到端点、nonce、`last_used_at`、重放测试证据」留给本任务。
- **可观察的完成标准**：
  1. `POST /api/v1/integration/usage-events` 对一个签名正确、载荷合法的新事件，在 `usage_events` 里提交一行 `RECEIVED` 之后才返回 `202`（REQ-INGEST-001）。
  2. 同一事件（归属与指纹一致）再发：返回 `200` 与当前处理状态，库里仍只有一行，不产生第二次任何效果。
  3. 同一 `event_id`、归属或指纹不同：返回 `409 IDEMPOTENCY_CONFLICT`，原事件不变，冲突写进只增的冲突表并写审计；告警计数能读到它。
  4. 签名错误、凭据不可用、时间戳出窗、同一 `X-Acuven-Request-Id` 重放：一律 `401`，不写 `usage_events`。
  5. 载荷里给出的 `tenant_id` / `project_id` 与凭据归属不符：`403`，不写 `usage_events`，另写一条审计（§11「rejected and audited」）。
- **明确不做什么**：
  - 定价、FX、扣费、状态推进（`RECEIVED` 之后的一切）—— T-G / T-H。本任务只落 `RECEIVED`，**不入队、不发 Celery 任务**：T-H 从数据库扫描 `RECEIVED`（REQ-INGEST-002），入队只是它的加速手段，由 T-H 定
  - 解析 `provider` / `model` 到目录 id —— T-H 在计费事务里用 `resolve_model` 解析（AIH-TASK-025 契约）并写入本任务建好的 `provider_id` / `model_id` 两列；本任务原样存字符串、两列留空
  - 计价快照列（价格版本、规则、FX、成本、计费额）—— T-H 的迁移加。那时表里只有尚未计费的事件，加可空列成本低；现在加会让本任务依赖 T-C 还没定的汇率精度
  - 批量端点 `POST …/usage-events/batch` —— T-F（复用本任务的单条逻辑）
  - 按凭据限流 —— 见 §9；本任务只加请求体大小上限
  - `GET /api/v1/integration/account-status` 与 `/health` —— Phase 3
- **现有行为与问题证据**：
  - `app/services/integration_auth.py` 文件头：「这一层不写日志、不做 nonce、不写 `last_used_at`、不缓存」
  - `docs/api.md`「集成请求签名」末尾：「防重放随摄取端点实现」
  - `docs/design/AIH-TASK-012-integration-access.md` 把 REQ-AUTH-001 的重放证据后移到本任务
  - `app/core/broker.py` 文件头：Redis 挂了 API 仍要收用量、落库并返回 202

## 2. 设计概要

```text
POST /api/v1/integration/usage-events
  ① 读原始请求体（> 16 KiB → 413，不解析）
  ② 解析五个 X-Acuven-* 请求头（缺失 / 格式错 → 401）
  ③ find_verifiable_credential(api_key, key_version, now)（不可用 → 401）
  ④ 解密 secret（进程内缓存，见「凭据与缓存」）→ verify_signature（失败 → 401）
  ⑤ nonce：Redis SET NX，键 = api_key + X-Acuven-Request-Id，过期时刻 = 请求时间戳 + 300 秒
       已存在 → 401；Redis 不可用 → 放行并记告警日志（见「防重放」）
  ⑥ 解析 JSON 并校验（schema_version、字段、按上报形态查 usage_meter_types）→ 422
  ⑦ 诊断用的 tenant_id / project_id 与凭据归属比对 → 不符：独立事务写审计，403
  ⑧ 规范化 → 指纹
  ⑨ session_scope：INSERT usage_events（status = RECEIVED）→ commit → 202 accepted
       撞 event_id 唯一约束 → 回滚，新事务读已有行：
         归属与指纹一致 → 200 already_received / already_processed
         不一致 → 写冲突行（去重）与审计，commit → 409 IDEMPOTENCY_CONFLICT
  ⑩ 响应之后（best effort）：节流地更新凭据的 last_used_at
```

### 接口

`POST /api/v1/integration/usage-events`，只接受集成签名认证（不接受管理员会话）。请求头按 `docs/api.md`「集成请求签名」，本任务补充：

- `X-Acuven-Request-Id`：`^[A-Za-z0-9._:-]{1,128}$`，不符 → 401。**每一次 HTTP 尝试都必须换一个新值**（重试也要换），写进 `docs/api.md`；时间戳本来就每次重新生成。
- `X-Acuven-Key-Version`：十进制正整数，不符 → 401。

请求体（JSON 对象，`extra="forbid"`，未知字段 422）：

| 字段 | 必填 | 规则 |
| --- | --- | --- |
| `schema_version` | 是 | 只接受 `"1.0"`，否则 422 `UNSUPPORTED_SCHEMA_VERSION` |
| `event_id` | 是 | **UUIDv7 或 ULID 的规范写法**（§23「UUIDv7 or ULID semantics」）：UUIDv7 = 小写、带连字符的 36 字符，`^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$`（版本位为 7、变体位为 `10`）；ULID = 26 字符大写 Crockford base32，`^[0-7][0-9A-HJKMNP-TV-Z]{25}$`（首字符 ≤ 7 保证不溢出 128 位）。其他任何格式（包括大写的 UUID、小写的 ULID、其他版本的 UUID）→ 422 `VALIDATION_ERROR`。只接受规范写法是为了让「同一个 ID」只有一种字节表示，唯一约束（`utf8mb4_0900_bin`）不会因大小写或写法不同而把同一事件当成两个。格式本身保证不了唯一，但排除了低熵的手写 ID（如 `1`、`test`）让别的合法事件被判冲突的主要来源 |
| `request_id` | 是 | AI 调用的 id；`^[A-Za-z0-9._:-]{1,128}$` |
| `conversation_id` | 否 | 同上；可空（不是每次 AI 调用都属于会话） |
| `tenant_id` / `project_id` | 否 | 只作诊断：若给出，必须等于凭据所属租户 / 项目的 `public_id`，否则 403（见下） |
| `provider` | 是 | `^[a-z0-9][a-z0-9_-]{0,63}$`（AIH-TASK-025 的供应商代码写法）。格式不合法的字符串永远解析不到目录，与其存成永远的 `MODEL_UNKNOWN`，不如立即 422 让集成方看到 |
| `model` | 是 | `^[A-Za-z0-9][A-Za-z0-9._:/@-]{0,127}$`（025 的模型代码 / 别名写法）；格式合法但目录里没有的，照收，由 T-H 标 `MODEL_UNKNOWN`（§84） |
| `usage_type` | 是 | 必须是 `usage_meter_types.code` 之一（查表，不写死清单；`RETIRED` 的照收）。不在表里 → 409 `UNKNOWN_USAGE_TYPE`，`retryable = true`（见下） |
| `input_tokens` / `output_tokens` / `cache_creation_input_tokens` / `cache_read_input_tokens` | 按形态 | `LLM_TOKEN_FIELDS`：四个都必填，JSON 整数，0 ≤ n ≤ 10^12；`QUANTITY`：一个都不许出现 |
| `quantity` | 按形态 | `QUANTITY`：必填，JSON **字符串**，`^\d{1,12}(\.\d{1,8})?$`，≥ 0；`quantity_kind = INTEGER` 时不许有小数部分（`"3.0"` 也拒绝）；`LLM_TOKEN_FIELDS`：不许出现 |
| `unit` | 按形态 | `QUANTITY`：必填，必须**逐字等于**该类型的 `unit`；`LLM_TOKEN_FIELDS`：不许出现 |
| `occurred_at` | 是 | RFC 3339，必须带时区（`Z` 或 `±hh:mm`），最多 6 位小数秒；换算成 UTC。晚于服务端当前时间超过 300 秒 → 422 `OCCURRED_AT_IN_FUTURE`；**没有过去的下限**（晚到事件是合法的，§46 的跨期由后续 Phase 处理） |

客户计算的费用、成本等字段不存在于模型中，出现即 422（§11「Do NOT send calculated customer cost」）。

**`UNKNOWN_USAGE_TYPE` 为什么可重试**：计量类型可以由管理员新建（AIH-TASK-025）。应用先上线新用量类型、管理员后建类型时，这些事件若被判为不可重试就会丢在应用侧；标为可重试，应用的 outbox（Phase 3）会退避重试，管理员建好类型后自然补进来。用 409 而不是 422：请求本身格式没错，是**平台当前的目录状态**还不接受它，状态变了同一请求就能成功 —— 这正是 409 的语义；所有 422 一律 `retryable = false`，不留例外。

**校验顺序**（决定一个请求报哪个码）：① 请求体大小 → ② 签名与凭据 → ③ nonce → ④ JSON 语法与顶层结构（字段名、`extra="forbid"`、各字段的类型与格式，**不含**按形态的字段组校验）→ ⑤ `schema_version` → ⑥ 查 `usage_type`：不在表里即 409 `UNKNOWN_USAGE_TYPE`，不再往下 → ⑦ 按该类型的形态校验字段组、`unit`、`quantity_kind` → ⑧ `occurred_at` 的未来上界 → ⑨ 归属比对。未知类型因此永远报 `UNKNOWN_USAGE_TYPE`，不会先被形态校验判成 `VALIDATION_ERROR`。

响应（§107 信封）：

| 情形 | HTTP | `data` |
| --- | --- | --- |
| 新事件已持久接收 | 202 | `{"event_id", "status": "accepted", "processing_status": "RECEIVED"}` |
| 合法重复，事件尚未 `PROCESSED` | 200 | `{"event_id", "status": "already_received", "processing_status": <当前状态>}` |
| 合法重复，事件已 `PROCESSED` | 200 | `{"event_id", "status": "already_processed", "processing_status": "PROCESSED"}` |

错误（`error.code`）。**集成方只看 `retryable` 决定是否重试，不按 HTTP 状态码推断**（每个错误码的 `retryable` 是固定的，见表；表下说明它在响应里的位置）：

| HTTP | `error.code` | 什么时候 | `retryable` | 客户端该做什么 | 写库 |
| --- | --- | --- | --- | --- | --- |
| 401 | `INTEGRATION_AUTH_FAILED` | 请求头缺失或格式错、凭据不可用、签名错、时间戳出窗、nonce 重放 —— **一律同一个码**，具体原因只进服务端日志（沿用 `docs/api.md` 的约定） | `false` | 停止重试该事件，进应用侧死信（**保留，不删除**）并告警；修好配置（凭据、时钟、Request-Id 生成）后由运维人工重投（Phase 3 的 Billing Client 提供重投） | 否 |
| 403 | `CREDENTIAL_SCOPE_MISMATCH` | 载荷里的 `tenant_id` / `project_id` 与凭据归属不符 | `false` | 停止重试该事件，进应用侧死信并告警 | 只写审计（独立事务） |
| 409 | `IDEMPOTENCY_CONFLICT` | 同 `event_id`、归属或指纹不同 | `false` | 同上 | 冲突行与审计 |
| 409 | `UNKNOWN_USAGE_TYPE` | `usage_type` 不在表里 | `true` | 保留事件，退避重试；管理员建好类型后自然成功 | 否 |
| 413 | `PAYLOAD_TOO_LARGE` | 请求体 > 16 KiB | `false` | 死信并告警 | 否 |
| 422 | `VALIDATION_ERROR` / `UNSUPPORTED_SCHEMA_VERSION` / `OCCURRED_AT_IN_FUTURE` | 见上表 | `false` | 死信并告警 | 否 |
| 500 | `INTERNAL_ERROR` | 未预期错误（含提交失败） | `true` | 退避重试 | 否（事务回滚） |
| 503 | `SERVICE_UNAVAILABLE` | 数据库不可用 | `true` | 退避重试 | 否 |

**为什么 401 不可重试**：它的每一个原因都是集成方的配置或实现问题 —— 凭据被吊销、`key_version` 错、签名算法错、时钟偏差超过 5 分钟、同一个 Request-Id 被重复使用 —— 同一个请求原样重试永远不会成功；而平台侧的瞬时故障（例如查凭据时数据库不可用）走 503，不会表现为 401。计划内的凭据轮换有重叠期（AIH-TASK-012），不会产生 401。所以不可重试 + 死信 + 告警 + 修好后人工重投，比「重试到天荒地老」更安全，且不需要在响应里泄露失败的具体原因。

成功响应（202 / 200）不带 `retryable`（不需要重试）。「死信」「退避上限」「应用侧告警」由 Phase 3 的 Billing Client 设计实现，本任务只保证每个码的 `retryable` 固定不变、写进 `docs/api.md`。

`retryable` 的位置：错误响应在信封顶层加 `retryable`（布尔）；集成端点的**每一个**错误响应都带它（包括 FastAPI 自身的 422 与 500，由集成路由的异常处理统一补上）。这是对 §107 信封的**只增扩展**，只用在集成端点，管理端响应不变。

**验签用的原始请求体**：签名覆盖原始字节的 SHA-256（`docs/api.md`），所以必须先读原始字节验签、再解析 JSON；FastAPI 的模型参数会先解析，处理函数改为读 `await request.body()` 后手工校验。

### nginx：超大请求也返回同一种信封（本任务负责集成端点的 nginx 配置）

`deploy/nginx/billing.conf` 的 `client_max_body_size 1m` 会让超过 1 MiB 的请求在 nginx 就被拒，默认返回 HTML 的 413，集成方拿不到 `error.code` 与 `retryable`。所以本任务在 nginx 加一个集成端点专用的前缀块：

```nginx
location /api/v1/integration/ {
    client_max_body_size 1m;
    error_page 413 = @integration_too_large;
    proxy_pass $billing_api$request_uri;
    include /etc/nginx/conf.d/billing-proxy-headers.inc;
}
location @integration_too_large {
    default_type application/json;
    return 413 '{"success":false,"data":null,"error":{"code":"PAYLOAD_TOO_LARGE","message":"request body too large"},"retryable":false,"request_id":null}';
}
```

- 应用层仍执行自己的上限（单条 16 KiB，批量由 T-F 定为 1 MiB），在 nginx 上限以内的超限请求由应用返回同样的码。两层给出的 `error.code` 与 `retryable` 相同，集成方看到的永远是信封。
- `request_id` 在 nginx 返回的 413 里为 `null`（请求没有到达应用，没有应用侧的请求 id）；`docs/api.md` 写明。
- 静态校验：`tests/backend/test_compose.py` 同款用例钉住这个块（前缀、上限、`error_page`、返回体是合法 JSON 且含 `PAYLOAD_TOO_LARGE` 与 `"retryable":false`）。
- **经过 nginx 的验收**：合并部署后，在 VPS 上经本栈 nginx 发一个 1 MiB + 1 字节的请求，确认返回 413 JSON（写进 `docs/runbook.md` 的部署后检查，并在任务记录里留下实测结果）。

### 防重放（nonce）

照 ADR-0004 §5：

- 在**验签通过之后**写：`SET nonce:{api_key}:{request_id_header} 1 NX PXAT (timestamp + 300) × 1000`。时间戳已出窗的请求在 ④ 就被拒，不查 Redis。键长上界：`nonce:` + 64 + `:` + 128。
- `SET` 返回已存在 → 401（重放）。
- **Redis 不可用（连接、超时 200 ms）→ 放行**，写一条 `WARNING` 结构化日志（`nonce_store_unavailable`，不含请求头原值以外的秘密）。依据：REQ-AVAIL-001（计费故障不能阻塞 AI 侧的上报）、`app/core/broker.py` 的约定、ADR-0004 §5 的显式风险接受（Redis 丢失时最多约 10 分钟失去 nonce 层）。**财务安全网是 `event_id` 唯一约束**：被重放的请求最多产生一个「合法重复」，没有第二次效果。
- nonce 写在落库**之前**：若之后落库失败（500），同一 Request-Id 就不能再用 —— 这没关系，因为契约要求每次尝试都换新 Request-Id。
- Redis 客户端：新建一个进程级连接池（`redis.from_url(settings.redis_url, socket_timeout=0.2, socket_connect_timeout=0.2)`），不复用 `broker.py` 的探活连接。

### 凭据与缓存

- ③ 每个请求都读一次凭据行（`find_verifiable_credential`，不加锁）：吊销与过期**即时生效**，不被缓存延迟。
- ④ 只缓存**解密结果**：键 = `(credential.id, sha256(encrypted_secret), encryption_key_version)`，进程内 LRU（上限 1024），TTL 300 秒。行的密文或主密钥版本一变，键就变，旧条目自然失效。缓存值只在内存，不进日志、不进异常（ADR-0004 场景 A）。
- ⑩ `last_used_at`：在响应之后、单独的短事务里 `UPDATE … SET last_used_at = now WHERE id = ? AND (last_used_at IS NULL OR last_used_at < now - 60 秒)` —— 每个凭据每分钟至多一次写，不在热路径上锁凭据行。失败只记日志。只在 202 / 200 时更新（认证成功且被接受或是合法重复）。

### 归属比对与审计（§11「rejected and audited」）

- 事件的归属**只来自凭据**：`tenant_id`、`project_id`、`integration_credential_id` 都取凭据行。
- 载荷若带 `tenant_id` / `project_id`，比对凭据的租户 / 项目 `public_id`；不符 → 在**独立事务**里写审计 `USAGE_EVENT_SCOPE_MISMATCH`（先例：登录失败的审计单独提交），然后 403。审计：操作者为空（系统），`entity_type = integration_credential`、`entity_id = api_key`；`after_state` 只有 `key_version`、`event_id`、载荷给的两个值、凭据实际的两个 `public_id`；不含载荷其余内容。
- 只有认证通过的请求才会走到这一步，所以写审计的只可能是持有有效密钥的一方；不做额外的去重。

### 规范化与指纹

指纹 = 下面规范对象的 JSON（键排序、无空白、UTF-8）的 SHA-256 十六进制：

| 键 | 值 |
| --- | --- |
| `schema_version`、`event_id`、`request_id`、`conversation_id`（缺省为 `null`）、`provider`、`model`、`usage_type` | 原样字符串 |
| 四个 token 字段 | 整数；`QUANTITY` 形态时不出现 |
| `quantity` | `QUANTITY` 形态时：量化到 8 位小数的定点字符串（`"37.42"` 与 `"37.420"` 得到同一个 `"37.42000000"`） |
| `unit` | `QUANTITY` 形态时原样 |
| `occurred_at` | UTC、`YYYY-MM-DDTHH:MM:SS.ffffffZ`（`…Z` 与等价的 `+08:00` 写法得到同一个值） |

- **不含**诊断用的 `tenant_id` / `project_id`（归属另行比对，且它们可选）。
- 语义相同、写法不同的两次请求得到同一指纹 → 判为合法重复，不是冲突。
- 服务端指纹不要求与客户端 outbox 的 `payload_fingerprint`（§21）一致；那是客户端自己的比对。

### 幂等与冲突（§23）

- **插入优先**：直接 `INSERT`，唯一约束 `uq_usage_events_event_id` 是最终仲裁（§23「read-before-insert alone is insufficient」）。
- 撞约束 → 回滚 → 新事务读已有行，比对**归属**（`tenant_id`、`project_id`、凭据的 `public_api_key`）与**指纹**：
  - 全部一致 → 合法重复（200）。
  - 任何一项不同 → 冲突（409）。
- **归属比对 `api_key` 而不是凭据行**：轮换后同一 `api_key` 的新 `key_version` 是新的一行（AIH-TASK-012），客户端在重叠期内用新版本重试同一事件是合法的，不应判冲突。§23 的「credential」按 `api_key` 理解；换了 `api_key`（另一套凭据）则一定是冲突。
- **冲突记录**：新表 `usage_event_conflicts`（只增，见「数据库」），同一 `(event_id, 请求方 api_key, 指纹)` 只记一次（唯一约束，撞了就忽略），避免客户端反复重试刷爆；每次**新增**冲突行时同一事务写审计 `USAGE_EVENT_IDEMPOTENCY_CONFLICT`。原事件的状态**不改**：`IDEMPOTENCY_CONFLICT` 是请求的结果，不是原事件的状态（§83 的这个取值保留在 CHECK 里，本任务不写它）。

### 数据库（迁移序号取实现时的下一个，预计 0016）

**`usage_events`**（本任务只建摄取需要的列）

| 列 | 类型 | 说明 |
| --- | --- | --- |
| `id` | BIGINT PK | |
| `public_id` | CHAR(36) UNIQUE | 管理端以后用（T-J） |
| `event_id` | VARCHAR(64) bin，UNIQUE | 全局唯一（§23、§79） |
| `schema_version` | VARCHAR(8) | |
| `tenant_id` / `project_id` | BIGINT | 复合外键 `(project_id, tenant_id)` → `projects(id, tenant_id)`，RESTRICT |
| `integration_credential_id` | BIGINT | 接收时用的那一行（含版本）。复合外键 `(integration_credential_id, project_id, tenant_id)` → `integration_credentials(id, project_id, tenant_id)`，RESTRICT：**事件的租户与项目由数据库保证就是该凭据所属的租户与项目**，不可能拼出「A 的凭据 + B 的项目」。本任务的迁移给 `integration_credentials` 加 UNIQUE `(id, project_id, tenant_id)`（只新增索引；凭据行本身已有 `(project_id, tenant_id)` → `projects` 的复合外键，AIH-TASK-012） |
| `request_id` | VARCHAR(128) bin | |
| `conversation_id` | VARCHAR(128) bin NULL | |
| `provider_code_raw` | VARCHAR(64) bin | 计划第 1 节第 5 条 |
| `model_code_raw` | VARCHAR(128) bin | 同上 |
| `provider_id` / `model_id` | BIGINT NULL | §79 的目录引用。本任务写入时为空；T-H 在计费事务里按 `occurred_at` 解析后写入（未知模型时保持为空，原始字符串仍在）。复合外键 `(model_id, provider_id)` → `ai_models(id, provider_id)`（025 已建该唯一约束），RESTRICT：模型必然属于该供应商；另加 `provider_id` → `ai_providers.id` 单列外键（只知道供应商、模型未知时也受约束）。CHECK：`model_id IS NULL OR provider_id IS NOT NULL` |
| `usage_meter_type_id` | BIGINT | 接收时按 `usage_type` 查到的类型 |
| `payload_shape` | VARCHAR(32) | 与下面两列一起组成复合外键 `(usage_meter_type_id, payload_shape, unit, quantity_kind)` → `usage_meter_types(id, payload_shape, unit, quantity_kind)`，RESTRICT：事件记下的形态、单位、数量类型**由数据库保证**与所属计量类型一致。本任务的迁移给 `usage_meter_types` 加 UNIQUE `(id, payload_shape, unit, quantity_kind)`（只新增索引，不改 025 的列与约束） |
| `quantity_kind` | VARCHAR(16) | 复合外键的一部分，等于该计量类型的 `quantity_kind` |
| `input_tokens` / `output_tokens` / `cache_creation_input_tokens` / `cache_read_input_tokens` | BIGINT NULL | 列名与载荷字段、与 025 的 `quantity_field` 取值一致（§79 写的是 `cache_creation_tokens` / `cache_read_tokens`；§74 允许按仓库约定命名，取与载荷一致的名字免得三处各叫一个名） |
| `quantity` | DECIMAL(20,8) NULL | |
| `unit` | VARCHAR(16) bin **NOT NULL** | 复合外键的一部分，等于该计量类型的 `unit`：`QUANTITY` 形态即上报的 `unit`（服务层已校验逐字相等，数据库再由外键兜底）；`LLM_TOKEN_FIELDS` 形态存类型的单位 `TOKEN`（025 的 CHECK 保证这类类型的单位是 `TOKEN`） |
| `payload_fingerprint` | CHAR(64) | |
| `status` | VARCHAR(32)，CHECK §83 的九个取值，默认 `RECEIVED` | 本任务只写 `RECEIVED` |
| `error_code` / `error_message` | VARCHAR(64) / VARCHAR(255) NULL | T-H 写 |
| `occurred_at` | DATETIME(6) | 保留微秒：别名与版本边界按 `[from, to)` 比较（025 §7「边界时刻」） |
| `received_at` | DATETIME | 整秒，与其他服务端时间一致 |
| `processed_at` | DATETIME NULL | T-H 写 |
| `created_at` | DATETIME | |

- **非空**：表中标 `NULL` 的只有 `conversation_id`、`provider_id`、`model_id`、四个 token 列、`quantity`、`error_code`、`error_message`、`processed_at`；**其余每一列都是 `NOT NULL`**，逐一列出：`id`、`public_id`、`event_id`、`schema_version`、`tenant_id`、`project_id`、`integration_credential_id`、`request_id`、`provider_code_raw`、`model_code_raw`、`usage_meter_type_id`、`payload_shape`、`quantity_kind`、`unit`、`payload_fingerprint`、`status`、`occurred_at`、`received_at`、`created_at`。这很关键：MySQL 对任一组成列为 `NULL` 的外键不做检查、对结果为 UNKNOWN 的 CHECK 视为通过 —— 复合外键 `(integration_credential_id, project_id, tenant_id)`、`(project_id, tenant_id)`、`(usage_meter_type_id, payload_shape, unit, quantity_kind)` 与下面的形态 CHECK 只有在这些列非空时才真正生效。
- CHECK（形态与字段组，显式写出，不用链式比较）：
  ```sql
  (payload_shape = 'LLM_TOKEN_FIELDS'
     AND input_tokens IS NOT NULL AND output_tokens IS NOT NULL
     AND cache_creation_input_tokens IS NOT NULL AND cache_read_input_tokens IS NOT NULL
     AND quantity IS NULL)
  OR
  (payload_shape = 'QUANTITY'
     AND input_tokens IS NULL AND output_tokens IS NULL
     AND cache_creation_input_tokens IS NULL AND cache_read_input_tokens IS NULL
     AND quantity IS NOT NULL)
  ```
  配合上面的复合外键，一条 `LLM_TOKEN` 事件在数据库层不可能缺 token 字段或带 `quantity`，一条 `QUANTITY` 类事件也不可能带 token 字段，且单位与数量类型必然与所属计量类型一致。
- CHECK：`quantity_kind <> 'INTEGER' OR quantity IS NULL OR quantity = FLOOR(quantity)`（整数类型的数量不许有小数部分）。
- CHECK：四个 token 列各自 `IS NULL OR ≥ 0`；`quantity IS NULL OR quantity ≥ 0`。
- 计量类型的 `unit` / `quantity_kind` 建后不可改（025 保证），所以外键引用的四列组合稳定。
- 索引：`(status, occurred_at)`（T-H 扫描 `RECEIVED`）、`tenant_id`、`project_id`、`conversation_id`、`request_id`、`occurred_at`、`provider_id`、`model_id`（§79）。
- 行永不删除。

**`usage_event_conflicts`**（只增）

| 列 | 类型 | 说明 |
| --- | --- | --- |
| `id` | BIGINT PK | |
| `usage_event_id` | BIGINT FK → `usage_events.id`，RESTRICT | 被撞的原事件 |
| `event_id` | VARCHAR(64) bin | |
| `integration_credential_id` | BIGINT FK，RESTRICT | 冲突请求用的凭据 |
| `api_key` | VARCHAR(64) bin | 冲突请求的 `public_api_key`（去重键的一部分） |
| `payload_fingerprint` | CHAR(64) | 冲突请求的指纹 |
| `mismatch` | VARCHAR(32)，CHECK `OWNERSHIP` / `FINGERPRINT` / `BOTH` | |
| `received_at` | DATETIME | |

- 所有列 `NOT NULL`（包括外键列 `usage_event_id`、`integration_credential_id` 与去重键的三列 —— 去重唯一约束遇到 `NULL` 会失效）。
- UNIQUE `(event_id, api_key, payload_fingerprint)`：同一冲突只记一次。
- 数据库层只追加：BEFORE UPDATE / DELETE 触发器 SIGNAL（照 `audit_logs` 的 0010 写法与权限预检）。

### 事务边界

| 路径 | 事务 |
| --- | --- |
| 新事件 | 一个：`INSERT usage_events` → commit → 202 |
| 合法重复 | 插入事务回滚；新的只读事务读已有行 → 200 |
| 冲突 | 插入事务回滚；新事务：读已有行 → `INSERT` 冲突行（撞去重约束则不写审计）→ 审计 → commit → 409 |
| 归属不符 | 不开插入事务；独立事务写审计 → 403 |
| `last_used_at` | 响应后独立短事务，失败不影响响应 |

`202` 只在 `usage_events` 行**提交成功之后**返回（REQ-INGEST-001）。本任务不写 outbox、不入队。

### 外部系统与异步边界

- Redis：只用于 nonce，不可用时放行（见上）。
- Celery：不用。T-H 从 `(status, occurred_at)` 索引扫描 `RECEIVED`（REQ-INGEST-002：数据库行才是事实来源）。

### 时间语义

- `occurred_at`：集成方报的 AI 调用发生时刻，UTC 微秒精度；上界 = 服务端 `now + 300 秒`，无下界。
- `received_at`：服务端接收时刻（整秒）。
- 签名时间戳：本次发送时刻，只用于验签窗口与 nonce 过期，不入库。
- 所有比较都用 UTC 无时区。

### 金额精度

本任务不算钱。`quantity` 是用量不是金额，但同样只按十进制字符串解析、绝不经过 `float`（INV-10 的同一做法），存 DECIMAL(20,8)，超过 8 位小数直接 422、不静默舍入。

### 审计

| `action` | 什么时候 | `entity_type` / `entity_id` | 内容 |
| --- | --- | --- | --- |
| `USAGE_EVENT_SCOPE_MISMATCH` | 载荷归属与凭据不符 | `integration_credential` / `api_key` | 见上 |
| `USAGE_EVENT_IDEMPOTENCY_CONFLICT` | 新增一条冲突行 | `usage_event` / 原事件的 `public_id` | `after_state`：`event_id`、冲突请求的 `api_key` 与 `key_version`、`mismatch` 类型；不含载荷内容与指纹以外的数据 |

两个动作都不在 spec §66 的清单里，按先例补上并记进 `docs/TODO.md`。操作者为空（系统），带请求的 ip 与 user agent。

### 告警

- `IDEMPOTENCY_CONFLICT`（§23「Admin security/data-integrity alert = required」）：T-C 建的告警计数接口加一个维度 `usage_event_conflicts`（最近 24 小时的冲突行数与最早时刻），`deploy/monitor.sh` 加对应的 Healthchecks 检查，> 0 即 P2。本任务依赖 T-C（迁移串行，T-C 先于本任务落地），接口与脚本的接入方式照 T-C 批准的设计。
- 「摄取中断」（§120 Billing ingestion outage）：平台侧能观测到的中断就是 API 或数据库不可用，已由现有的 services / readyz 两个检查覆盖；应用侧「发不出去」由 Phase 3 的积压监控覆盖。本任务不另加维度，并在 `docs/TODO.md` 写明这条映射。
- Redis 不可用导致 nonce 放行：只写 `WARNING` 日志；Redis 本身的健康已由 readyz 的 `degraded` 覆盖。

## 3. 不变量影响矩阵

| 不变量 | 是否触碰 | 可能怎样被破坏 | 设计控制 | 验证方式 |
| --- | --- | --- | --- | --- |
| INV-1 中心故障不中断 AI | **是**（间接） | 摄取端点在 Redis 故障时拒收，逼应用侧积压或阻塞 | Redis 不可用时放行；端点只依赖数据库；应用侧本就异步上报（Phase 3） | Redis 不可用时仍 202 的用例 |
| INV-2 事件不重复扣费 | **是** | 同一事件被存成两行 | `event_id` 唯一约束 + 插入优先；本任务不扣费 | 并发重复用例（真 MySQL） |
| INV-3 支付不重复入账 | 否 | — | — | — |
| INV-4 余额只经账本变动 | 否 | 不碰钱包 | 不导入钱包模块 | — |
| INV-5 历史账本不可变 | 否 | — | — | — |
| INV-6 事件保留版本引用 | 否（本任务） | 快照列由 T-H 加 | — | T-H 闸门 |
| INV-7 客户不可见成本毛利 | 否 | 响应不含任何金额 | 响应白名单 | 字段断言 |
| INV-8 租户不可互访 | **是** | 用 A 的凭据写入 B 的事件；用 A 的凭据探测 B 的事件是否存在；绕过服务直接写库拼出不一致的归属 | 归属只来自凭据；载荷归属不符即拒；冲突响应只返回错误码，不回显已有事件的归属或内容；`(凭据, 项目, 租户)` 复合外键 | 越权与探测用例；复合外键的直接插入用例（MySQL） |
| INV-9 对话内容不入库 | **是** | 载荷夹带 prompt / 回复 | `extra="forbid"`；只有元数据字段；字段长度与字符集受限 | 多余字段 422 用例 |
| INV-10 金额用 Decimal | 否（无金额） | `quantity` 经 `float` 失真 | 十进制字符串解析，DECIMAL 存储 | 精度边界用例 |
| INV-11 event_id 至多一次财务效果 | **是** | 冲突请求改写原事件；冲突请求产生新行 | 冲突只写冲突表，原事件一个字节不变；新行只在首次 | 冲突后原行逐列不变的断言 |
| INV-12 定稿对账单不可变 | 否 | — | — | — |
| INV-13 状态与事件原子提交 | **是** | 冲突行写了而审计没写（或反之） | 同一事务 | 审计写入抛错时冲突行也回滚的用例 |
| INV-14 队列丢失不毁持久工作 | **是** | 202 之前只写了队列 | 202 只在数据库行提交后返回；不入队 | 提交失败时不返回 202 的用例 |

## 4. 状态与并发

本任务只产生 `RECEIVED`，不推进任何状态。

| 当前 | 事件 | 结果 |
| --- | --- | --- |
| （不存在） | 新事件 | `RECEIVED` |
| 任何状态 | 合法重复 | 不变，返回当前状态 |
| 任何状态 | 冲突 | 不变，冲突表 +1（去重） |

- **串行化**：不加任何行锁。两个并发的相同 `event_id` 请求由唯一约束决出一个赢家，输家走「撞约束 → 读已有行」路径；此时赢家可能尚未提交 —— MySQL 的唯一约束会让输家的 `INSERT` 等待赢家提交或回滚，所以输家读到的一定是已提交的行（赢家回滚则输家插入成功）。
- **数据库保证的唯一性**：`usage_events.event_id`；`usage_events.public_id`；`usage_event_conflicts (event_id, api_key, payload_fingerprint)`。
- **幂等键**：`event_id`（业务层）+ `X-Acuven-Request-Id`（传输层 nonce）。
- **相同 ID、不同载荷**：409，原事件不变，冲突表记录。
- **重试层**：服务端不重试；客户端按 HTTP 状态与 `retryable` 决定。
- **原子提交**：事件行；冲突行 + 审计。

## 5. 失败与恢复矩阵

| 失败点 | 对外结果 | 数据最终状态 | 是否可重试 | 恢复来源 | 告警 |
| --- | --- | --- | --- | --- | --- |
| 请求体过大 | 413 | 无 | 否 | — | 无 |
| 认证失败（任一原因，含 nonce 重放） | 401，`retryable = false` | 无 | 否（修好配置后人工重投） | 应用侧死信 | 服务端日志带具体原因；应用侧告警 |
| Redis 不可用 | 照常处理 | 照常 | — | — | WARNING 日志；readyz degraded |
| 校验失败 | 422（`retryable = false`）；未知类型 409（`retryable = true`） | 无 | 见错误表 | — | 无 |
| 归属不符 | 403 | 只有审计 | 否 | — | 审计 |
| 数据库不可用 / 提交失败 | 503 / 500 | 事件行未提交 | 是 | 客户端 outbox 重试 | 现有 readyz / services 检查 |
| 提交成功、响应丢失 | 客户端超时 | `RECEIVED` 已存 | 重试 → 200 already_received | 数据库 | 无 |
| 并发重复 | 一个 202、其余 200 | 一行 | — | — | 无 |
| 冲突 | 409 | 原事件不变，冲突表 +1 | 否 | 管理员查冲突表 | 告警计数维度 |
| 冲突审计写入失败 | 500 | 冲突行也回滚 | 是（下次重试会再记） | — | 错误日志 |
| `last_used_at` 写失败 | 不影响响应 | 未更新 | 下次请求再试 | — | 日志 |
| Celery 丢失 | 无影响 | 本任务不用队列 | — | 数据库扫描（T-H） | — |

## 6. 数据与安全边界

- **租户过滤**：事件归属只来自凭据行；载荷里的 `tenant_id` / `project_id` 只作比对，从不用来决定写入哪个租户。
- **鉴权主体**：只有集成凭据（HMAC 签名）。本路由不接受管理员或客户会话，也不在 `/api/v1/admin` 下。
- **禁止返回的字段**：已有事件的归属、内容、指纹；任何金额；凭据信息。冲突与重复响应只回 `event_id` 与处理状态 —— 而处理状态只对**同一归属**的合法重复返回，冲突只回错误码，不泄露别家事件的存在细节。
- **日志 / 审计 / 异常**：
  - 不记录请求体、签名、secret、解密结果；认证失败的具体原因只进服务端日志。
  - 日志绑定 `tenant_id`、`project_id`、`event_id`（§94 的四个字段里的三个）；`X-Acuven-Request-Id` 作为 `integration_request_id` 记录。
  - 审计内容见上表，不含载荷。
- **密钥**：secret 只在内存里解密；缓存键不含 secret；缓存值不序列化、不进日志。
- **prompt / response 或客户数据**：不接收（`extra="forbid"`），不存储（REQ-PRIV-001）。
- **保留与删除**：`usage_events` 与冲突表都不删除；保留期随上线闸门的留存政策。

## 7. 测试证据计划

| 风险/需求 | 测试层级 | 场景 | 预期结果 |
| --- | --- | --- | --- |
| 正常路径 | integration（API） | LLM_TOKEN 事件；QUANTITY 事件（AUDIO_SECOND 小数、IMAGE_GENERATION 整数） | 202；一行 `RECEIVED`，各列正确 |
| 202 先落库 | integration | 提交抛错 | 500，不返回 202，库里无行 |
| 合法重复 | integration | 同一请求重发（新签名、新 Request-Id）；`quantity` 写法不同但数值相同；`occurred_at` 用等价时区写法 | 200 `already_received`，仍一行 |
| 已处理的重复 | integration | 直接把行改成 `PROCESSED` 后重发 | 200 `already_processed` |
| 轮换期间重复 | integration | 同一 `api_key` 的新 `key_version` 重发同一事件 | 200（合法重复，不是冲突） |
| 冲突：指纹 | integration | 同 `event_id`、token 数不同 | 409；原行逐列不变；冲突行一条、`mismatch = FINGERPRINT`；审计一条 |
| 冲突：归属 | integration | 另一项目（另一 `api_key`）用同 `event_id` | 409；`mismatch = OWNERSHIP`；响应不含原事件任何信息 |
| 冲突去重 | integration | 同一冲突请求重发三次 | 冲突行与审计各一条 |
| 并发重复与并发冲突 | integration（真 MySQL） | 多线程同时发同一事件；同时发同 ID 不同内容 | 恰好一行；前者其余 200，后者冲突记录正确 |
| 签名与凭据 | integration | 缺头、错签名、错 key_version、已吊销、过期、时间戳出窗（±301 秒）、恰好 300 秒 | 401 同一 code、`retryable = false`；恰好 300 秒通过 |
| `event_id` 格式 | integration | 合法的 UUIDv7、合法的 ULID；大写 UUIDv7；小写 ULID；UUIDv4；变体位错误的 UUIDv7；首字符为 8 的 ULID；`1`、`test`、65 字符 | 前两者 202；其余 422 `VALIDATION_ERROR` |
| 重放（REQ-AUTH-001 证据） | integration | 同一请求原样重发（同 Request-Id、同时间戳、同签名） | 第二次 401；库里一行 |
| Redis 不可用 | integration | nonce 存储抛连接错误 | 202，WARNING 日志一条 |
| nonce 过期时刻 | unit | 断言 `PXAT` = 时间戳 + 300 秒（毫秒） | 同左 |
| 归属不符 | integration | 载荷 `tenant_id` 或 `project_id` 与凭据不符 | 403；无事件行；审计一条，内容只含规定字段 |
| 字段校验 | integration | 每个字段的边界：长度、字符集、token 上下界、`quantity` 小数位 8 / 9、INTEGER 类型带 `.0`、形态混用（LLM_TOKEN 带 quantity、QUANTITY 带 token）、`unit` 不等、`schema_version` 不支持、多余字段（含费用字段） | 422 与对应 code |
| 未知 usage_type | integration | 不在表里的类型（字段组按任意形态写、甚至字段组本身不合法）；管理员建类型后重发 | 409 `UNKNOWN_USAGE_TYPE`、`retryable = true`（校验顺序保证不会先报 `VALIDATION_ERROR`）；建好后 202 |
| `retryable` 全覆盖 | integration | 逐一触发错误表的每个码（含 FastAPI 自身的 422 与未预期的 500） | 每个错误响应都带 `retryable`，取值与错误表一致 |
| `RETIRED` 类型 | integration | 已停用的计量类型 | 202 |
| 未知模型 | integration | 格式合法、目录里没有的模型 | 202（留给 T-H 标 `MODEL_UNKNOWN`） |
| 时间 | integration | `occurred_at` 晚于 now 301 秒 / 300 秒；很久以前；不带时区 | 422 / 202 / 202 / 422 |
| 请求体上限 | integration | 16 KiB + 1 字节 | 413 `PAYLOAD_TOO_LARGE`、`retryable = false`（应用层） |
| nginx 413 | unit（`test_compose.py` 同款）+ 部署后实测 | 配置块的静态断言；部署后经 nginx 发 1 MiB + 1 字节 | 返回 413 JSON 信封，`error.code = PAYLOAD_TOO_LARGE`、`retryable = false` |
| 目录引用列（MySQL） | integration | 直接写入：`model_id` 属于另一供应商；`model_id` 非空而 `provider_id` 为空；不存在的 `provider_id` | 复合外键 / CHECK / 外键拒绝；两列都空（本任务写入时的状态）通过 |
| `last_used_at` 节流 | integration | 60 秒内连续两次请求；超过 60 秒 | 只更新一次；再次更新 |
| 解密缓存 | unit | 同一凭据连续请求；轮换后（密文变） | 只解密一次；轮换后重新解密 |
| 吊销即时生效 | integration | 缓存命中后吊销凭据 | 下一个请求 401 |
| 冲突表只追加 | integration（MySQL） | 直接 UPDATE / DELETE 冲突行 | 触发器拒绝 |
| 数据库约束 | integration（MySQL） | 直接插入：两组字段都填；两组都空；**token 交错空值**（`1, NULL, 1, NULL` 及其余交错组合，逐一列举）；`QUANTITY` 形态缺 `unit`；token 为负；`quantity` 为负；非法 status | CHECK 拒绝 |
| 必需列非空（MySQL） | integration | 直接插入：对 `usage_events` 每一个必需列（上文逐一列出的那些）各插一行只把该列置为 `NULL`，其余合法；对 `usage_event_conflicts` 每一列同样 | 全部被 NOT NULL 拒绝（证明外键与 CHECK 不会因 `NULL` 被绕过） |
| 凭据与归属一致（MySQL） | integration | 直接插入：凭据属于租户 A 的项目 P1，事件的 `(tenant_id, project_id)` 写成租户 B 的项目 P2（P2 本身合法存在）；以及同租户的另一个项目 | 复合外键拒绝 |
| 形态、单位、数量类型与计量类型一致（MySQL） | integration | 直接插入：`usage_meter_type_id` 指向 `LLM_TOKEN`、`payload_shape = 'QUANTITY'`（及反向）；`unit` 与类型不符（如 `AUDIO_SECOND` 配 `MINUTE`）；`quantity_kind` 与类型不符；`IMAGE_GENERATION`（INTEGER）配 `quantity = 2.5` | 复合外键 / CHECK 拒绝 |
| 事务中途失败 | integration（SQLite 与 MySQL 各一次） | 冲突路径审计写入抛错 | 冲突行回滚 |
| 越权 | integration | 用管理员会话调用；不带签名 | 401 |
| 迁移 | integration（MySQL） | upgrade / downgrade | 表、约束、触发器建删正确 |
| §119 接收延迟 | 不在本任务 | p95 ≤ 500 ms 的实测归 T-I（压测） | — |

## 8. 迁移与上线

- **数据迁移步骤**：建 `usage_events`、`usage_event_conflicts`，冲突表的两个触发器照 0010 的写法与权限预检。序号取实现时的下一个。
- **锁表与性能影响**：新表，不锁已有表。
- **兼容窗口**：旧代码不读新表。
- **部署顺序**：合并即自动部署，先迁移后代码。
- **回滚或前滚**：downgrade 删触发器、删两表。上线后到 Phase 3 试点前没有集成方在发事件，表是空的；试点之后回滚会丢事件 —— 那时只能前滚。
- **部分部署时的行为**：迁移成功、代码未更新：新表空置。代码更新、迁移失败：容器起不来，旧容器继续服务（端点不存在，集成方 404 → 重试）。
- **nginx**：`/api/` 已整体公开，端点无需改 nginx；`client_max_body_size 1m` 大于 16 KiB，应用层上限先生效。
- **监控指标和告警负责人**：冲突维度见 §2「告警」；负责人 Kelvin（Healthchecks 通知）。

## 9. 方案取舍

| 方案 | 优点 | 风险 | 未采用原因 |
| --- | --- | --- | --- |
| **采用**：插入优先 + 唯一约束仲裁；冲突写独立只增表 | 数据库是唯一仲裁者（§23）；原事件绝不被冲突改写；冲突可查、可计数 | 多一张表 | — |
| 先查后插 | 少一次回滚 | §23 明确说不够；并发下两个都插 | 不采用 |
| 冲突时把原事件改成 `IDEMPOTENCY_CONFLICT` | 状态集中 | 一个恶意或出错的请求就能改变合法事件的处理状态，破坏 INV-11 | 不采用 |
| 冲突只写审计、不建表 | 少一张表 | 告警计数要扫审计表；审计的 `before/after` 是 JSON，查询不便；去重无约束可依 | 不采用 |
| 归属比对凭据行（含 `key_version`） | 字面贴合 §23 | 轮换重叠期内的合法重试被判冲突 | 按 `api_key` 比对，理由见 §2 |
| Redis 不可用时 503 | 防重放层不打折 | 计费侧一个非持久组件的故障阻塞上报，违背 REQ-AVAIL-001；而资金安全本就由 `event_id` 保证 | 放行（ADR-0004 §5 已接受） |
| nonce 存数据库 | 不依赖 Redis | 每个请求多一次写，§119 的吞吐与延迟目标更难达到；ADR-0004 §5 已定非支付端点用 Redis | 不采用 |
| 摄取时就解析 `provider` / `model` 到 id | 事件一落库就有模型 | 与 025 的解析语义（计费事务内、加锁、按 `occurred_at`）重复一套；未知模型要在两处处理 | 只存原始字符串，T-H 解析 |
| 本任务就加全部 §79 快照列 | 表一次成形 | 依赖 T-C 的汇率精度与 T-B / T-D 的表；本任务无法写这些列的任何测试 | 本任务只加 `provider_id` / `model_id`（只依赖已批准的 025）；价格、规则、汇率与金额快照列由 T-H 加，那时表里只有未计费事件 |
| 按凭据限流 | 防单一集成方刷爆 | 需要跨进程的计数（进程内桶在多 worker 下不准）；§119 要求单租户能到 100 事件 / 秒突发，阈值要随 T-I 的压测定 | 另记 TODO，压测后再定；本任务只有请求体上限 |
| `UNKNOWN_USAGE_TYPE` 用 422 且不可重试 | 语义简单 | 管理员晚建类型时事件丢在应用侧 | 409 + `retryable = true` |
| 客户端按 HTTP 状态码推断是否重试 | 不需要额外字段 | 同一个状态码（409）里既有该重试的也有不该重试的 | 只看 `retryable` |
| 401 可重试（v5–v7） | 时钟偏差等偶发配置问题能自愈 | 吊销、签名错误等永久失败被无限重试（Codex 判 v7 阻断） | 不可重试 + 死信 + 人工重投 |
| `event_id` 任意字符串（v1–v7） | 集成方自由 | 低熵 ID 让别的合法事件被判冲突 | 限 UUIDv7 / ULID 规范写法 |

## 10. 未决问题与假设

- **未决问题**：无阻断项。
- **需要谁拍板**：无（全部为技术取舍，见 §9）。
- **尚未验证的假设**：
  1. MySQL 8.4 上，两个事务并发插入同一唯一键时，后者等待前者提交或回滚（InnoDB 的重复键检查会对已存在的索引记录加共享锁）—— §7 的并发用例在 CI 的真 MySQL 上实测。
  2. `redis-py` 的 `SET` 支持 `pxat`（redis-py ≥ 4.0、Redis ≥ 6.2）—— 实现时核对仓库锁定的版本与生产 Redis 版本；若不支持，按 ADR-0004 §5 的退路用固定 TTL 660 秒。
  3. 生产 nginx 把原始请求体原样转给应用（没有改写 body 的模块）—— 签名覆盖原始字节，现有配置只做反向代理。
- **如果假设错误**：1 → 改为「撞约束后加锁读」（`SELECT … FOR SHARE`），设计版本 +1；2 → 固定 TTL，不改设计语义；3 → 验签失败会在上线后的第一次集成测试里立刻暴露。

## 11. 审查与版本绑定

审查方要回答的五个问题常驻在 [scripts/review_checklist.md](../../scripts/review_checklist.md) 的「设计审查」一节。

- Codex 的批准必须写成 `APPROVED: design v<N>`，`<N>` 取本 Issue 顶部的「设计版本」
- 设计版本一变，之前的 APPROVE 自动作废
- 实质修改：改了契约、表结构、事务边界、状态机、失败语义、不变量控制

### 设计闸门判定

- [ ] 需求、非目标和验收标准明确
- [ ] 关键契约与事务边界明确
- [ ] 触碰的不变量都有控制措施
- [ ] 失败路径都有确定的最终状态
- [ ] 高风险控制都有测试场景
- [ ] 没有未解决的阻断假设
- [ ] 审批绑定到明确的设计版本

### 实现范围（批准后登记为 Worker 任务的 `allowed_change_paths`，文件名避开 Worker 的敏感路径词）

- `alembic/versions/<日期>_<序号>_usage_events.py`、`alembic/env.py`
- `app/models/usage.py`（新）、`app/models/auth.py`（两个审计动作）
- `app/repositories/usage_events.py`（新）、`app/repositories/integration_access.py`（`last_used_at` 节流写）
- `app/services/usage_ingest.py`（新）、`app/services/integration_auth.py`（解密缓存）
- `app/core/nonce.py`（新）、`app/core/errors.py`（`retryable` 扩展）
- `deploy/nginx/billing.conf`（集成端点前缀块与 JSON 413）、`tests/backend/test_compose.py`、`docs/runbook.md`（部署后检查）
- `app/schemas/usage_ingest.py`（新）、`app/api/integration_usage.py`（新）、`app/main.py`
- T-C 建的告警计数接口与 `deploy/monitor.sh`（加冲突维度；具体文件以 T-C 批准的设计为准）
- `tests/backend/test_usage_ingest_api.py`、`tests/backend/test_usage_ingest_service.py`（新）、`tests/backend/test_migrations.py`、`tests/backend/test_model_columns.py`
- `docs/api.md`、`docs/database-schema.md`、`docs/TODO.md`

## 12. 版本变更记录

| 版本 | 日期 | 改了什么 | 触发原因 |
| --- | --- | --- | --- |
| v1 | 2026-09-29 | 初稿 | — |
| v2 | 2026-09-29 | `usage_events` 的字段组 CHECK 改为按形态显式列出的两组条件（原链式 `=` 挡不住交错空值）；加 `payload_shape` 列与复合外键 `(usage_meter_type_id, payload_shape)` → `usage_meter_types(id, payload_shape)`；token 与 `quantity` 的非负改为逐列 CHECK；§7 补交错空值与形态不符的直接插入用例 | Codex 判 v1 REQUEST_CHANGES：链式 `=` 不等于「四列同空同非空」 |
| v3 | 2026-09-29 | `unit` 改为 NOT NULL（`LLM_TOKEN_FIELDS` 存 `TOKEN`）；加 `quantity_kind` 列；复合外键扩为 `(usage_meter_type_id, payload_shape, unit, quantity_kind)` → `usage_meter_types` 同名四列（迁移给该表加对应唯一索引）；加整数数量不许有小数的 CHECK；形态 CHECK 去掉 `unit` 的空与非空分支；§7 补单位、数量类型不符的直接插入用例 | Codex 判 v2 REQUEST_CHANGES：`unit` 与 `quantity_kind` 只在应用层校验 |
| v4 | 2026-09-29 | `usage_events` 加复合外键 `(integration_credential_id, project_id, tenant_id)` → `integration_credentials(id, project_id, tenant_id)`（迁移给凭据表加对应唯一索引）；INV-8 一行补这条控制；§7 补不一致归属的直接插入用例 | Codex 判 v3 REQUEST_CHANGES：凭据与租户 / 项目没有由数据库绑定 |
| v5 | 2026-09-29 | `UNKNOWN_USAGE_TYPE` 改为 409、`retryable = true`；规定集成方只看 `retryable`，错误表逐码给出 `retryable` 与客户端动作（补 500）；写明校验顺序（先查类型、再按形态校验）；集成端点的每个错误响应都带 `retryable`；§5、§7、§9 相应调整 | Codex 判 v4 REQUEST_CHANGES：`UNKNOWN_USAGE_TYPE` 的可重试语义前后不一 |
| v6 | 2026-09-29 | `usage_events` 逐列写明非空（只有 `conversation_id`、四个 token 列、`quantity`、`error_code`、`error_message`、`processed_at` 可空），说明这是复合外键与 CHECK 生效的前提；`usage_event_conflicts` 全部列非空；§7 补逐列置空的直接插入用例 | Codex 判 v5 REQUEST_CHANGES：必需列未写 NOT NULL，含 NULL 的外键与 CHECK 会被绕过 |
| v7 | 2026-09-29 | 加 §79 的 `provider_id` / `model_id`（可空，T-H 写入）与复合外键、CHECK、索引；nginx 为 `/api/v1/integration/` 前缀返回 JSON 信封的 413（静态用例 + 部署后经 nginx 实测）；实现范围加 nginx 配置与相关测试 | Codex 判 v6 REQUEST_CHANGES：缺 §79 的目录引用列；T-F 审查（#180）指出 nginx 的 413 不是信封，单条端点同样适用 |
| v8 | 2026-09-29 | `INTEGRATION_AUTH_FAILED` 改为 `retryable = false`（死信、告警、修好后人工重投），写明理由；`event_id` 限定为 UUIDv7 或 ULID 的规范写法并给出正则；§5、§7、§9 相应调整 | Codex 判 v7 REQUEST_CHANGES：401 标可重试会无限重试永久失败；`event_id` 未按 §23 限定格式 |

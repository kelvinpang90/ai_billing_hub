# AIH-TASK-019 设计：出站 webhook 签名密钥（已批准 v1）

> **来源**：设计闸门 Issue #135。本文件是 `APPROVED: design v1` 那一版正文的**逐字副本**，审查者为 Codex。
> 放进仓库，是因为 OpenClaw Worker 在沙箱里不联网、读不到 GitHub Issue。
> **实现以本文件为准**；与 Issue 不一致时，以 Issue #135 上被批准的 v1 为准。设计要改，就回到 Issue 升版本、重新过闸门，不要直接改本文件。
> 正文里写「编号登记时分配」的实现任务，登记为 `AIH-TASK-019`。
> 与 Issue 正文的唯一差别：两处链接目标由 GitHub 网页上的 `../blob/main/…` 改成仓库内的相对路径，链接文字与其余正文逐字相同。

---

## 0. 准入判定

| 任务涉及 | 要填的范围 |
| --- | --- |
| 钱包 / 账本 / 定价 / 汇率 / 支付 / 幂等 / 状态机 | **全部章节** |
| 用量摄取 / 认证与会话（集成侧与平台侧）/ Webhook | §1–§7（**§6 不可省**） |
| 前端 / 文档 / CI / 脚本 | 不走本闸门 |
| 既不碰钱、也不碰用量摄取 / 认证与会话 / Webhook 的后端改动 | 不走本闸门 |

本任务档位：`全部`。它是出站 Webhook 的签名密钥（第二行，§6 不可省），而且密钥有自己的状态机（待启用、签名中、已退役）。各节都写，不适用的写明理由。

---

状态：`READY_FOR_REVIEW`
负责人：Claude
**设计版本**：`v1`
对应需求：spec §28、§36、§37、§57、§66、§74、§76、§96；`REQ-AUTH-001`、`REQ-STATUS-001`、`REQ-PRIV-001`、`REQ-TXN-001`；[ADR-0004](../adr/ADR-0004-credential-encryption.md) §4、§4a
目标 PR：待开（批准后在 `.platform/tasks.yaml` 登记实现任务，编号登记时分配）

## 1. 目标与边界

- **要解决的问题**：平台要向集成应用后端推送签名的状态 webhook（§28：「same canonical HMAC rules … with a separate per-project outbound signing secret」），但还没有地方存这把出站签名密钥。ADR-0004 §4a 已于 2026-09-25 选定方案 i：新建 `project_webhook_secrets` 表，结构对齐 `integration_credentials`（多行、签名版本与主密钥版本分列、状态、时间），实现另过设计闸门 —— 就是本 Issue。它也是 TODO 里 D4 唯一没收口的一项。
- **可观察的完成标准**：
  1. 管理员为某个客户的某个项目**签发**一把出站签名密钥，得到 201：`key_version`、`status = PENDING`、`secret`。`secret` 只在这一次响应里出现（§36）。库里同一事务多出一行密文和一条 `WEBHOOK_SECRET_ISSUE` 审计。
  2. 管理员**启用**一个 `PENDING` 版本：它变成 `ACTIVE`（平台从此用它签名），原来的 `ACTIVE` 版本同一事务变成 `RETIRED`。写 `WEBHOOK_SECRET_ACTIVATE` 审计。
  3. 管理员**退役**一个版本（丢弃没用上的 `PENDING`，或在泄露时下掉 `ACTIVE`），写 `WEBHOOK_SECRET_RETIRE` 审计。
  4. 管理员**列出**项目的各版本：不含 `secret`、不含密文。
  5. **签名库**（不接任何发送方）：给定项目，取出此刻 `ACTIVE` 的明文与版本号；给定请求要素，按 §37 算出四个请求头（`X-Acuven-Timestamp`、`X-Acuven-Signature`、`X-Acuven-Event-ID`、`X-Acuven-Key-Version`）。
  6. `secret` 不出现在日志、审计前后状态、列表响应与异常信息里；库里只有密文。
- **明确不做什么**：
  - 发送 webhook 本身：`status_webhook_url`、`backend_base_url`、`projects.integration_status`、`webhook_deliveries` 表、投递 worker、重试与退避、payload 形状与事件类型映射 —— 归 Phase 3 的「出站服务状态 webhook 投递」设计闸门（它依赖本表与状态模型任务）
  - 客户在门户里自助管理、前端页面（批准后另登记前端任务）
  - 主密钥版本的重新包裹（ADR-0004 §4b，已在 TODO）
  - 解密结果缓存（没有热路径）
- **现有行为与问题证据**：
  - `grep -rn -i "webhook" app/` 只命中三处注释（`app/api/__init__.py`、`app/models/tenancy.py`、`app/tasks/outbox.py`），没有表、没有签名、没有发送方；状态跃迁写进 outbox 的事件 `tenant.billing_status_changed`（`app/repositories/wallet.py`）在 `app/tasks/outbox.py` 里没有处理器，以 `PENDING` 持久等待
  - `projects` 只有身份字段（`app/models/tenancy.py`）；`docs/database-schema.md`「尚未建的列」把 webhook 列划给本任务
  - `app/services/integration_auth.py` 已有 §37 的 `canonical_request(method, path_and_query, timestamp, request_id, body)` 与 `sign(secret, canonical)`，本任务复用，不另写一套

## 2. 设计概要

```text
管理端请求 → Pydantic 校验（extra="forbid"）
  → 处理函数第一行 require_admin
  → services.webhook_signing.<issue|activate|retire>（session_scope：一个事务）
      按路径 public_id 读客户、再按客户读项目 → 404
      锁项目行（SELECT … FOR UPDATE）—— 同一项目的密钥操作串行
      读该项目全部版本 → 按状态机判定 → 404 / 409
      [签发] 生成 secret → encrypt_secret（带 AAD）→ INSERT
      [启用 / 退役] UPDATE 状态与时间列
      写审计
      commit
  → 响应模型（白名单字段）→ success() 信封，带 Cache-Control: no-store

（以后的）投递 worker → signing_material(project) → sign_status_webhook(...) → 四个请求头
```

### 接口

全部在 `/api/v1/admin/customers/{customer_id}/projects/{project_id}` 下（下称 `…`），只有 ADMIN 能调，处理函数第一条语句是 `require_admin`（加进 AIH-TASK-006 的路由枚举用例）。

| 方法与路径 | 请求体 | 成功 |
| --- | --- | --- |
| `POST …/webhook-secrets` | `{}` | 201，签发的版本（含 `secret`） |
| `GET …/webhook-secrets` | —（分页 §108） | 200，版本分页，`key_version` 从小到大 |
| `POST …/webhook-secrets/{key_version}/activate` | `{}` | 200，启用后的全部版本（数组，从小到大） |
| `POST …/webhook-secrets/{key_version}/retire` | `{"reason": str}` | 200，该版本 |

- 请求体一律 `extra="forbid"`：不能指定 `secret`、`key_version`、`status`、任何 id；客户与项目只来自路径，操作者只来自令牌。
- `reason`：去首尾空白后 1–255，只写业务说明，不写个人数据（进审计，永久保留）。
- `{key_version}` 是正整数；不是整数时 422。
- 四个接口的成功响应都带 `Cache-Control: no-store`。

**版本对象**（响应白名单）：`key_version`、`status`（`PENDING` / `ACTIVE` / `RETIRED`）、`created_at`、`activated_at`、`retired_at`。**签发的版本** = 版本对象 + `secret`。响应里没有内部 id、`tenant_id`、内部 `project_id`、密文与主密钥版本。

**`secret` 格式**：`whs_` + 64 个小写十六进制字符（256 位，`secrets.token_hex(32)`）。HMAC 的密钥是这**整个字符串**的 UTF-8 字节，与入站的 `sk_…` 规则一致。文档与测试一律用全零占位值。

### 错误

| HTTP | `error.code` | 什么时候 | 写库 |
| --- | --- | --- | --- |
| 404 | `CUSTOMER_NOT_FOUND` | 客户不存在 | 否 |
| 404 | `PROJECT_NOT_FOUND` | 项目不存在或不属于该客户（两者一模一样） | 否 |
| 404 | `WEBHOOK_SECRET_NOT_FOUND` | 该项目没有这个 `key_version` | 否 |
| 409 | `WEBHOOK_SECRET_PENDING_EXISTS` | 签发时已有一个 `PENDING` 版本（先启用或退役它） | 否 |
| 409 | `WEBHOOK_SECRET_NOT_PENDING` | 启用一个 `RETIRED` 版本 | 否 |
| 409 | `WEBHOOK_SECRET_CONFLICT` | 数据库唯一约束兜底触发（并发下理论上被行锁挡住，见 §4） | 否 |
| 503 | `ENCRYPTION_NOT_CONFIGURED` | 主密钥未配置（只影响签发；列表、启用、退役照常） | 否 |
| 500 | `INTERNAL_ERROR` | 意外错误；事务整体回滚，这次生成的 `secret` 作废、从未返回 | 否 |

另有通用的 401 / 403 / 422 / 503 `DATABASE_NOT_CONFIGURED`。

### 数据库：`project_webhook_secrets`

| 列 | 类型 | 说明 |
| --- | --- | --- |
| `id` | BIGINT PK 自增 | 内部 |
| `project_id` / `tenant_id` | BIGINT NOT NULL | 复合外键 → `projects(id, tenant_id)`（`uq_projects_id_tenant`，0007 已建），`ON DELETE RESTRICT` |
| `key_version` | INT NOT NULL | 签名版本，从 1 起，客户可见（`X-Acuven-Key-Version`） |
| `status` | VARCHAR(16) NOT NULL | `PENDING` / `ACTIVE` / `RETIRED` |
| `encrypted_secret` | TEXT NOT NULL | `encrypt_secret` 的输出 |
| `encryption_key_version` | INT NOT NULL | 主密钥版本（ADR-0004 §4：与签名版本分列，绝不混用） |
| `active_slot` | BIGINT，生成列（STORED）= `CASE WHEN status = 'ACTIVE' THEN project_id END` | 唯一索引，保证每个项目至多一个 `ACTIVE` |
| `pending_slot` | BIGINT，生成列（STORED）= `CASE WHEN status = 'PENDING' THEN project_id END` | 唯一索引，保证每个项目至多一个 `PENDING` |
| `created_at` / `activated_at` / `retired_at` | DATETIME | 后两者可空 |

约束：

- `UNIQUE(project_id, key_version)`
- `CHECK (key_version >= 1)`、`CHECK (status IN ('PENDING','ACTIVE','RETIRED'))`
- 时间与状态一致：`PENDING` ⇒ `activated_at IS NULL AND retired_at IS NULL`；`ACTIVE` ⇒ `activated_at IS NOT NULL AND retired_at IS NULL`；`RETIRED` ⇒ `retired_at IS NOT NULL`
- 生成列上的两个唯一索引依赖「唯一索引允许多个 NULL」，MySQL 与 SQLite 都是这个语义（§10 列为待测假设）

**AAD**：`b"project_webhook_secrets|" + project_public_id + b"|" + str(key_version)`（ASCII）。把密文绑定到项目与版本：把一行的密文拷到别的项目或别的版本上，解密即失败（ADR-0004 §1，照 012 的做法）。

### 签名库（不接发送方）

- `signing_material(session, keyring, project_id) -> SigningMaterial | None`：读该项目的 `ACTIVE` 行、解密，返回 `(secret, key_version)`；没有 `ACTIVE` 返回 `None`（调用方以后据此让投递等待，而不是不签名就发）。
- `sign_status_webhook(secret, key_version, *, url_path_and_query, event_id, body, now) -> dict[str, str]`：`timestamp` = `now` 的 Unix 纪元秒整数（与入站一致，每次发送重新生成，§37）；规范串用现有 `canonical_request("POST", url_path_and_query, timestamp, event_id, body)` —— **§37 规范串的第四行在出站 webhook 上填 `X-Acuven-Event-ID`**（§28 的出站请求头里没有 request id，event id 就是那一行的角色）；返回四个请求头。
- 这一条对规范串第四行的定义写进 `docs/api.md` 的「状态 webhook 签名」一节，给集成方照做。

### 事务边界

每个接口一个 `session_scope`：锁项目行 → 读版本 → 改状态 / 插行 → 写审计 → 提交。签发时 `load_keyring` 放在最前面（主密钥没配置就 503，什么都不做）。启用是**一个事务里两行状态变化**（新 `ACTIVE`、旧 `RETIRED`），任何一步失败整体回滚，不会出现「两把 ACTIVE」或「一把都没有」的中间态。

### 时间

`created_at` / `activated_at` / `retired_at` 取服务端 `now`，截到整秒（与 012、006 一致），UTC 无时区。签名时间戳是发送时刻，与这些列无关。

### 金额

不适用：不涉及金额。

### 审计

| 动作 | `entity_type` / `entity_id` | before → after | `reason` |
| --- | --- | --- | --- |
| `WEBHOOK_SECRET_ISSUE` | `project_webhook_secret` / 项目 `public_id` | 无 → `key_version`、`status` | 无 |
| `WEBHOOK_SECRET_ACTIVATE` | 同上 | 新旧两个版本的 `key_version`、`status` | 无 |
| `WEBHOOK_SECRET_RETIRE` | 同上 | 该版本的 `key_version`、`status` | 请求里的 `reason` |

前后状态**永远不含** `secret`、密文与主密钥版本。三个动作名不在 spec §66 的清单里（那里只有入站的 `API_KEY_*` 与 `PROJECT_UPDATE`），按 `PROJECT_CREATE` 的先例在 `docs/TODO.md` 记为对 §66 的补充。

### 配置

不新增配置项。出站的重叠期在集成方一侧（ADR-0004 §4a：平台同一时刻只用一把签名，旧版本多久停止接受由后端决定）；平台侧没有需要配置的时长。

## 3. 不变量影响矩阵

| 不变量 | 是否触碰 | 可能怎样被破坏 | 设计控制 | 验证方式 |
| --- | --- | --- | --- | --- |
| INV-1 中心故障不中断 AI | 否 | 本任务只有管理端接口与一个未接线的库，不在 AI 调用路径上 | — | — |
| INV-2 事件不重复扣费 | 否 | 不涉及用量事件与扣费 | — | — |
| INV-3 支付不重复入账 | 否 | 不涉及支付 | — | — |
| INV-4 余额只经账本变动 | 否 | 不读写钱包 | — | — |
| INV-5 历史账本不可变 | 否 | 不碰账本 | — | — |
| INV-6 事件保留版本引用 | 否 | 不涉及用量事件；以后投递记录要存 `key_version`，那是 Phase 3 的事 | — | — |
| INV-7 客户不可见成本毛利 | 否 | 响应里只有版本与状态字段 | 响应白名单 | 响应字段断言 |
| INV-8 租户不可互访 | **是** | 用 A 客户的路径操作 B 客户的项目；把一个项目的密文挪到另一个项目 | 项目只按「路径客户 → 该客户名下的项目」查，查不到一律 404；复合外键保证行的 `tenant_id` 与项目一致；AAD 绑定项目 `public_id` 与版本 | 跨客户 / 跨项目用例；密文互换后解密失败用例 |
| INV-9 对话内容不入库 | 否 | 不处理对话 | — | — |
| INV-10 金额用 Decimal | 否 | 无金额 | — | — |
| INV-11 event_id 至多一次财务效果 | 否 | 签名库只是把 `event_id` 放进规范串，不产生财务效果 | — | — |
| INV-12 定稿对账单不可变 | 否 | 不涉及对账单 | — | — |
| INV-13 状态与事件原子提交 | **是（类比）** | 密钥状态变了而审计没写，或启用只改了一半 | 状态变化与审计同一事务；启用的两行在同一事务 | 审计失败、提交失败时的回滚用例（SQLite 与 MySQL 各一次） |
| INV-14 队列丢失不毁持久工作 | 否 | 本任务没有异步任务 | — | — |

## 4. 状态与并发

| 当前状态 | 事件 | 前置条件 | 新状态 | 副作用 | 非法时结果 |
| --- | --- | --- | --- | --- | --- |
| （无行） | 签发 | 该项目没有 `PENDING`；主密钥已配置 | `PENDING`，`key_version` = 该项目最大版本 + 1（没有则 1） | 插行、审计 `ISSUE`，响应带 `secret` | 已有 `PENDING` → 409 `WEBHOOK_SECRET_PENDING_EXISTS`；无主密钥 → 503 |
| `PENDING` | 启用 | — | `ACTIVE`，`activated_at = now` | 原 `ACTIVE`（若有）同事务 → `RETIRED`、`retired_at = now`；审计 `ACTIVATE` | — |
| `ACTIVE` | 启用 | — | 不变 | 无（幂等，返回 200 与当前各版本，不写审计） | — |
| `RETIRED` | 启用 | — | — | — | 409 `WEBHOOK_SECRET_NOT_PENDING` |
| `PENDING` | 退役 | — | `RETIRED` | 审计 `RETIRE`（带 reason） | — |
| `ACTIVE` | 退役 | — | `RETIRED` | 审计 `RETIRE`；**项目此后没有签名密钥**，以后的投递要等到有新 `ACTIVE` 为止（Phase 3 设计负责「等待」的语义） | — |
| `RETIRED` | 退役 | — | 不变 | 无（幂等，200，不写审计） | — |

`RETIRED` 是终态。版本号只增不减，退役的版本号不复用。

**正常轮换流程**（对应 ADR-0004 §4a 四步）：签发新版本（`PENDING`）→ 管理员把 `secret` 交给集成方，集成方配置成「新旧两个版本都接受」→ 管理员启用新版本（平台原子切换签名）→ 集成方确认新签名能通过后，自己停止接受旧版本。平台侧不需要知道第 4 步何时发生：旧版本在第 3 步已经 `RETIRED`，平台不会再用它签。

- **串行化**：每个操作先 `SELECT … FOR UPDATE` 锁项目行，同一项目的签发、启用、退役排队执行。项目行锁与钱包 / 租户锁没有交集（钱包路径不锁项目），不引入新的死锁顺序。
- **数据库保证的唯一性**：`(project_id, key_version)` 唯一；`active_slot`、`pending_slot` 唯一 —— 即使绕过行锁，也不可能出现两把 `ACTIVE` 或两个 `PENDING`，也不会有重复版本号。唯一约束冲突映射为 409 `WEBHOOK_SECRET_CONFLICT`。
- **幂等键**：签发不幂等（再调一次在已有 `PENDING` 时得到 409，没有时得到新版本 —— 与入站建凭据一致）；启用、退役按「目标状态已达成即 200 不写」幂等。
- **相同 ID、不同载荷**：不适用（没有客户端提供的幂等键）。
- **重试层**：不在服务端重试；客户端对 5xx 与超时重发。签发的响应丢了：新版本已是 `PENDING` 但拿不到 `secret`，退役它再签发一次（`secret` 不能事后找回）。
- **原子提交**：状态变化（启用时两行）+ 审计。

## 5. 失败与恢复矩阵

| 失败点 | 对外结果 | 数据最终状态 | 是否可重试 | 恢复来源 | 告警 |
| --- | --- | --- | --- | --- | --- |
| 校验失败（多余字段、`reason` 为空、`key_version` 非整数） | 422 | 无改动 | 改请求后可 | — | 无 |
| 客户 / 项目 / 版本不存在 | 404 | 无改动 | 否 | — | 无 |
| 状态机非法（已有 `PENDING`、启用 `RETIRED`） | 409 | 无改动 | 按提示先处理再做 | — | 无 |
| 主密钥未配置 | 503 `ENCRYPTION_NOT_CONFIGURED` | 无改动 | 配好后可 | 运维配置主密钥 | 现有启动告警 |
| 加密或审计写入抛错 | 500 | 事务回滚，无行、无审计；生成的 `secret` 作废 | 可 | — | 错误日志（不含 secret） |
| DB 提交失败 | 500 | 回滚，同上 | 可 | — | 错误日志 |
| 提交成功、响应丢失（签发） | 客户端超时 | 新版本已是 `PENDING`，`secret` 不可找回 | 退役该版本后重新签发 | 列表接口能看到这个 `PENDING` | 无 |
| 提交成功、响应丢失（启用 / 退役） | 客户端超时 | 状态已变 | 重发即幂等 200 | — | 无 |
| Redis / Celery 丢失 | 不适用 | 本任务没有异步任务 | — | — | — |
| 外部服务超时 | 不适用 | 本任务不调外部服务 | — | — | — |
| 重复请求 | 启用 / 退役：200 幂等；签发：见 §4 | — | — | — | — |
| 解密失败（`signing_material`） | 抛 `DecryptionFailed`（500 语义），不返回任何明文 | 不改库 | 否 | 主密钥或密文有问题，按 ADR-0004 恢复流程 | 错误日志（不含密文） |

## 6. 数据与安全边界

- **租户过滤**：路径客户 → 按 `tenant_id` 查该客户名下的项目 → 版本行按 `project_id` 查。三层都查不到时响应一样（404），不泄露存在性。复合外键在数据库层保证行的租户与项目一致。
- **鉴权主体**：只有 ADMIN（`require_admin`，角色以数据库为准）。没有客户侧接口。
- **禁止返回的字段**：内部 id、`tenant_id`、内部 `project_id`、`encrypted_secret`、`encryption_key_version`、生成列。`secret` 只在签发的 201 里出现一次，响应模型用 `SecretStr` 并在序列化时显式取值，其余路径不可能带出。
- **日志 / 审计 / 异常**：这一层不写日志；审计只含 `key_version` 与 `status`（和 `reason`）；异常消息与 `repr` 不含 `secret` 与密文；签名库不记录明文、规范串与签名。
- **密钥存储、读取与轮换**：ADR-0004 §1 信封加密（AES-256-GCM，带 AAD），主密钥经 Compose `secrets:` 文件注入；`encryption_key_version` 存主密钥版本，与签名的 `key_version` 分列（ADR-0004 §4）；轮换流程见 §4（ADR-0004 §4a 的四步）；重新包裹不递增 `key_version`（ADR-0004 §4b，不在本任务）。
- **prompt / response 或客户数据**：不处理。
- **保留与删除**：没有删除接口，退役只改状态、不清密文（与入站凭据一致：以后的投递记录要引用版本）。项目本身也没有删除接口，外键 `RESTRICT`。

## 7. 测试证据计划

| 风险/需求 | 测试层级 | 场景 | 预期结果 |
| --- | --- | --- | --- |
| 正常路径 | integration（API） | 签发 → 列表 → 启用 → 再签发 → 启用 | 版本 1、2 依次 `PENDING`→`ACTIVE`；启用 2 后 1 为 `RETIRED`；每步一条对应审计 |
| `secret` 只出现一次 | integration | 签发响应有 `secret`；列表、启用、退役响应都没有 | 字段断言；响应头 `Cache-Control: no-store` |
| 存的是密文 | integration | 读库 | `encrypted_secret` ≠ 明文，能用正确 AAD 解出明文 |
| AAD 绑定 | unit | 把 A 项目版本 1 的密文拷到 B 项目或版本 2 | 解密失败 |
| 泄露面 | integration | 签发后检查审计前后状态、日志（caplog）、异常消息 | 都不含 `secret` 与密文 |
| 鉴权 | integration | 匿名 401、CUSTOMER 403，都不写库；四个处理函数首条语句是 `require_admin`（AST 用例） | 同左 |
| 租户越权 | integration | 用 A 客户路径操作 B 客户的项目；版本号属于别的项目 | 404，不写库 |
| 边界值 | integration | 多余字段、`reason` 为空白 / 256 字符、`key_version` 为 0 / 非整数 | 422，不写库 |
| 状态机 | integration | 已有 `PENDING` 再签发；启用 `RETIRED`；重复启用 `ACTIVE`；重复退役 | 409 / 409 / 200 不写审计 / 200 不写审计 |
| 退役 `ACTIVE` | integration | 退役后 `signing_material` | 返回 `None` |
| 并发 | integration（真 MySQL） | 两个线程同时签发；两个线程同时启用不同的 `PENDING`（先造数据） | 只有一个成功，另一个 409；任何时刻至多一个 `ACTIVE`、一个 `PENDING` |
| 数据库兜底 | integration（MySQL 与 SQLite） | 绕过服务层直接插第二个 `ACTIVE` / `PENDING` 行；插重复 `key_version`；违反时间与状态一致的 CHECK | 数据库拒绝 |
| 事务中途失败 | integration（MySQL 与 SQLite 各一次） | 审计写入抛错、提交抛错（签发 / 启用 / 退役） | 全部回滚：启用后仍是原来的 `ACTIVE`，没有新行、没有审计 |
| 主密钥未配置 | integration | 签发 | 503，不写库；列表、启用、退役照常 |
| 签名库 | unit | 固定 secret、时间、event id、路径与 body | 头部四项正确；签名等于用 `canonical_request` + `sign` 手算的值；第四行是 event id；时间戳是整数秒 |
| 迁移 | integration（MySQL） | upgrade / downgrade | 表、生成列、唯一索引、CHECK、复合外键存在；downgrade 删表 |
| Redis/Celery 丢失 | 不适用 | 没有异步任务 | — |
| 金额精度 | 不适用 | 无金额 | — |

## 8. 迁移与上线

- **数据迁移步骤**：新增一张空表，没有数据迁移。迁移序号取实现时的下一个（现在是 0008；若状态模型任务先合并则顺延）。
- **锁表与性能影响**：只建新表与索引，不改现有表，不锁业务表。
- **兼容窗口**：新表与新接口，没有旧调用方；旧代码看不到新表，无影响。
- **部署顺序**：合并即自动部署，容器启动前跑迁移（既有流程）。先迁移后代码。
- **回滚或前滚**：downgrade 删表。表里若已有生产数据，回滚会丢掉已签发的密钥 —— 那时应前滚修复而不是回滚（集成方手里的 secret 会失效）。
- **部分部署时的行为**：迁移成功、代码未更新：新表闲置，无影响。代码更新、迁移失败：容器启动失败，旧容器继续服务（既有部署流程）。
- **监控指标和告警负责人**：无新增指标。签发量极低（管理员手工），不需要监控；解密失败走现有错误日志。

## 9. 方案取舍

| 方案 | 优点 | 风险 | 未采用原因 |
| --- | --- | --- | --- |
| **采用**：`PENDING` / `ACTIVE` / `RETIRED` 三态，平台同一时刻只用一把签名，启用即原子切换 | 与 ADR-0004 §4a 的出站流程一一对应；「待启用的下一把」有地方放；签名方永远只有一把，不存在「两把都能签」的歧义 | 退役 `ACTIVE` 后项目没有签名密钥（这是泄露时想要的效果，但要让管理员知道） | — |
| 照搬入站：`ACTIVE` / `REVOKED` 加 `valid_from` / `valid_until` 重叠期 | 与 012 代码结构最像 | 入站的重叠期在**校验方**（平台）一侧：两把同时可用于校验。出站平台是**签名方**，同一时刻只能用一把签；把重叠期放在平台侧没有意义，还会让「现在用哪把签」变得不确定 | 语义不对 |
| 不要 `PENDING`，签发即生效 | 少一个状态 | 签发的一瞬间平台就开始用新密钥签，集成方还没配好，这段时间的 webhook 全部验签失败（ADR-0004 §4a 第 1–2 步被跳过） | 会造成投递失败 |
| 唯一 `ACTIVE` 只靠行锁、不加生成列唯一索引 | 表简单 | 绕过服务层的写入（运维脚本、以后的代码）可能造出两把 `ACTIVE`，签名方不确定 | 数据库层兜底的代价很小（两列两个索引） |
| 本任务连投递一起做（ADR-0004 §4a 原话「随发送 webhook 的代码一起做」） | 表与唯一的使用方同时上线，不会出现「有表没人用」 | 投递依赖 `status_webhook_url`、`integration_status`、有效状态合成（状态模型任务）、`webhook_deliveries` 与重试，是 Phase 3 的一整块；一起做会让这个设计闸门同时覆盖密钥、状态模型与投递三块 | 拆开：表 + 管理端 + 签名库先做（与 012「校验库先于摄取端点」同一做法），投递归 Phase 3 设计闸门。这一点偏离了 ADR-0004 §4a 的「随发送代码一起做」，见 §10 |

## 10. 未决问题与假设

- **未决问题**：无阻断项。
- **尚未验证的假设**：
  1. MySQL 8.4 与 SQLite 都允许在 STORED 生成列上建唯一索引，且唯一索引允许多个 NULL。MySQL 8.0 起支持，SQLite 3.31 起支持生成列并可索引；CI 的迁移测试会实际插入违反约束的行来验证。若 SQLite 侧不支持，退路是只在 MySQL 上建这两个唯一索引、SQLite 测试靠服务层用例覆盖，并在实现 PR 里说明。
  2. 集成方能按「规范串第四行填 `X-Acuven-Event-ID`」实现验签 —— 这是本设计对 §28 与 §37 之间空白的补充定义，写进 `docs/api.md`；Phase 3 试点（`ai_chatbot_demo`）是第一个实现方。
- **需要谁拍板**（不阻断审查）：本设计把 ADR-0004 §4a「随发送 webhook 的代码一起做」拆成两步（理由见 §9 最后一行）。若 Kelvin 不同意拆分，本任务退回，与 Phase 3 投递合并成一个设计闸门。拆分被接受后，在 ADR-0004 文件头追加一行修订说明（按 ADR 修订规则属于「补完待定项」）。
- **如果假设错误**：假设 1 错 → 迁移测试失败，实现无法合并，不会带着虚假的保证上线。假设 2 错 → 只影响 `docs/api.md` 的说明与签名库的一行，Phase 3 设计闸门里可改，届时本库还没有调用方。

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

### 实现范围（批准后登记为 Worker 任务的 `allowed_change_paths`）

文件名避开 OpenClaw Worker 的敏感路径规则（`secret` / `key` / `credential` / `token` 等整词），统一用 `webhook_signing`：

- `alembic/versions/<日期>_<序号>_webhook_signing.py`（实现时定名）
- `alembic/env.py`
- `app/models/integration.py`（新模型）
- `app/models/auth.py`（三个 `AuditAction`）
- `app/repositories/webhook_signing.py`
- `app/services/webhook_signing.py`（管理端操作 + `signing_material` + `sign_status_webhook`）
- `app/schemas/webhook_signing.py`
- `app/api/admin_customers.py`（四个路由）
- `tests/backend/test_webhook_signing_api.py`
- `tests/backend/test_webhook_signing_service.py`
- `tests/backend/test_admin_customers_api.py`（路由枚举与合法请求体）
- `tests/backend/test_migrations.py`
- `docs/api.md`（四个接口 + 状态 webhook 签名规则）
- `docs/database-schema.md`（新表；「尚未建的列」更新）
- `docs/TODO.md`（记录段；D4 收口；§66 动作名补充）

## 12. 版本变更记录

| 版本 | 日期 | 改了什么 | 触发原因 |
| --- | --- | --- | --- |
| v1 | 2026-09-27 | 初稿 | — |

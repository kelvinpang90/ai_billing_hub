# AIH-TASK-012 设计：API 凭据（已批准 v1）

> **来源**：设计闸门 Issue #118。本文件是 `APPROVED: design v1` 那一版正文的**逐字副本**，审查者为 Codex。
> 放进仓库，是因为 OpenClaw Worker 在沙箱里不联网、读不到 GitHub Issue。
> **实现以本文件为准**；与 Issue 不一致时，以 Issue #118 上被批准的 v1 为准。设计要改，就回到 Issue 升版本、重新过闸门，不要直接改本文件。
> 正文里写「编号登记时分配」的实现任务，登记为 `AIH-TASK-012`。
> 与 Issue 正文的唯一差别：三处链接目标由 GitHub 网页上的 `../blob/main/…` 改成仓库内的相对路径，链接文字与其余正文逐字相同。
> ⚠️ §11「实现范围」里六个带 `credentials` 的**文件名**撞上 OpenClaw Worker 的敏感路径规则（`credential` / `secret` / `key` / `token` 等整词的路径一律不许改），实现时改用 `integration_access`（例如 `app/services/integration_access.py`），以 `.platform/tasks.yaml` 里 `AIH-TASK-012` 的 `allowed_change_paths` 为准。只是文件名，表名 `integration_credentials`、契约、表结构与事务边界都不变，不构成实质修改。

---

## 0. 准入判定

| 任务涉及 | 要填的范围 |
| --- | --- |
| 钱包 / 账本 / 定价 / 汇率 / 支付 / 幂等 / 状态机 | **全部章节** |
| 用量摄取 / 认证与会话（集成侧与平台侧）/ Webhook | §1–§7（**§6 不可省**） |
| 前端 / 文档 / CI / 脚本 | 不走本闸门 |
| 既不碰钱、也不碰用量摄取 / 认证与会话 / Webhook 的后端改动 | 不走本闸门 |

本任务档位：`全部`。它是集成侧认证的凭据（第二行，§6 不可省），而且凭据有自己的状态机（生效、轮换重叠、吊销）。最低要求是 §1–§7，这里各节都写，不适用的写明理由。

---

状态：`READY_FOR_REVIEW`
负责人：Claude
**设计版本**：`v1`
对应需求：spec §36、§37、§57、§66、§74.4、§96、§124（Phase 1：「建 API 凭据」）；`REQ-AUTH-001`、`REQ-PRIV-001`、`REQ-TXN-001`；[ADR-0004](../adr/ADR-0004-credential-encryption.md)
目标 PR：待开（批准后在 `.platform/tasks.yaml` 登记实现任务，编号登记时分配）

## 1. 目标与边界

- **要解决的问题**：Phase 1 验收写着「建 API 凭据」（§124），而平台上还没有集成凭据的任何东西：没有表、没有管理端接口、没有签名校验。Phase 3 的 Billing Client 与 Phase 2 的用量摄取都要靠它认证（§35、§36）。加密原语已经就绪：`app/core/crypto.py` 的信封加密（AES-256-GCM，ADR-0004 §1），目前只有 2FA 在用。
- **可观察的完成标准**：
  1. 管理员为某个客户的某个项目**建凭据**，得到 201：`api_key`、`key_version = 1`、`secret`。`secret` 只在这一次响应里出现（§36「Never expose secret again after initial creation」）。库里同一事务多出一行 `integration_credentials`（`encrypted_secret` 是密文）和一条 `API_KEY_CREATE` 审计。
  2. 管理员**列出**项目的凭据：每个版本一行，不含 `secret`、不含密文。
  3. 管理员**轮换**一个 `api_key`：`api_key` 不变，新增版本 `key_version = N+1`，响应里只这一次带新 `secret`；此前仍在生效、没有截止时间的版本，`valid_until` 设为 `now + 重叠期`（默认 7 天，可配置）。同一事务写 `API_KEY_ROTATE` 审计。
  4. 管理员**吊销**：可以只吊销一个版本（重叠期提前结束），也可以吊销整个 `api_key`（所有版本）。立即生效，同一事务写 `API_KEY_REVOKE` 审计。
  5. **校验库**：按 §37 规范化请求串并用 HMAC-SHA256 校验签名的纯函数，外加「按 `api_key` + `key_version` 找出此刻可用于校验的版本」的查询函数。本任务不接到任何端点上（摄取端点属于 Phase 2 / 3）。
  6. `secret` 不出现在日志、审计前后状态、列表响应与异常信息里，库里只有密文（REQ-AUTH-001「never logged」、§36）。
- **明确不做什么**（Kelvin 2026-09-25 的四项决定之内）：
  - **出站 webhook 签名密钥**：方案定为 ADR-0004 §4a 的方案 i（新建 `project_webhook_secrets` 表，结构对齐本表），实现另过设计闸门。本设计只保证表结构可以照搬；
  - **防重放存储**（nonce）与**摄取端点**：随 Phase 2 / 3 的接收接口一起做（ADR-0004 §5 已定存储方案）。REQ-AUTH-001 要求的「replay」测试证据因此后移，记进 `docs/TODO.md`；
  - `last_used_at` 的**写入**：列建好，但现在没有任何流量会用到凭据，写入随摄取端点做；
  - 解密结果的缓存（ADR-0004 场景 A 的「短期缓存」）：随摄取端点做，那时才有热路径；
  - 主密钥版本的重新包裹（ADR-0004 §4b）：已在 TODO 里，与本任务无关；
  - 客户在门户里自助管理凭据、前端页面、Billing Client。
- **现有行为与问题证据**：
  - `grep -rn "hmac\|X-Acuven" app/` 没有结果；`AuditAction` 里没有任何 `API_KEY_*`；
  - `docs/database-schema.md` 里没有 `integration_credentials`；
  - `app/core/crypto.py` 的 `encrypt_secret` 返回的版本号是**主密钥**版本，2FA 把它存进名为 `key_version` 的列。§74.4 与 ADR-0004 §4 里的 `key_version` 是**客户看得见的签名密钥版本**，ADR 明写两者不能混用。本表把两者分成两列（见 §2）。

## 2. 设计概要

```text
管理端请求 → Pydantic 校验（extra="forbid"）
  → 处理函数第一行 require_admin
  → services.integration_credentials.<create|rotate|revoke_version|revoke_key>（session_scope：一个事务）
      按路径 public_id 读客户、再按客户读项目（不加锁）→ 404
      [轮换 / 吊销] 按 api_key 锁住该项目下这个 key 的所有版本行（SELECT … FOR UPDATE，按 key_version 排序）→ 404 / 409
      生成 secret（仅建凭据与轮换）→ encrypt_secret（带 AAD）→ INSERT / UPDATE 凭据行
      写审计（API_KEY_CREATE / ROTATE / REVOKE）
      commit
  → 响应模型（白名单字段）→ success() 信封，带 Cache-Control: no-store
```

### 接口

全部在 `/api/v1/admin` 下，只有 ADMIN 能调，处理函数第一条语句是 `require_admin`（沿用 AIH-TASK-006 的路由枚举用例）。`{customer_id}`、`{project_id}` 是 `public_id`；`{api_key}` 是凭据的 `api_key`（非机密的查找标识，§36）。

| 方法与路径 | 请求体 | 成功 |
| --- | --- | --- |
| `POST /customers/{customer_id}/projects/{project_id}/credentials` | `{}` | 201，凭据版本 + `secret` |
| `GET /customers/{customer_id}/projects/{project_id}/credentials` | — | 200，凭据版本分页 |
| `POST /customers/{customer_id}/projects/{project_id}/credentials/{api_key}/rotate` | `{"current_key_version": int}` | 201，新版本 + `secret` |
| `POST /customers/{customer_id}/projects/{project_id}/credentials/{api_key}/versions/{key_version}/revoke` | `{"reason": str}` | 200，该版本 |
| `POST /customers/{customer_id}/projects/{project_id}/credentials/{api_key}/revoke` | `{"reason": str}` | 200，该 key 的全部版本 |

- 请求体一律 `extra="forbid"`：不能指定 `api_key`、`secret`、`key_version`、`tenant_id`、`project_id`、`valid_until` 或任何 id。客户与项目只来自路径，操作者只来自令牌。
- `current_key_version`：轮换时调用方声明「我看到的最新版本号」。与锁内读到的最大版本号不同就 409，挡住双击与两个管理员同时轮换（两次轮换会各生成一个 `secret`，先生成的那个会在重叠期后失效，而调用方可能只保存了它）。
- `reason`：去掉首尾空白后 1–255 字，写进审计的 `reason` 列（§66）。只写业务说明，不写个人数据。
- 吊销是幂等的：目标版本已经 `REVOKED` 时返回 200 和当前状态，不写任何东西（没有发生新的动作）。

**凭据版本对象**（列表项；建凭据与轮换的响应在此之外多一个 `secret`）：

```json
{
  "api_key": "ak_00000000000000000000000000000000",
  "key_version": 2,
  "status": "ACTIVE",
  "valid_from": "2026-09-25T08:30:00",
  "valid_until": null,
  "last_used_at": null,
  "created_at": "2026-09-25T08:30:00",
  "revoked_at": null
}
```

- `status` 只有 `ACTIVE` 与 `REVOKED` 两个存储值。「已过期」不存：`ACTIVE` 且 `valid_until <= now` 的版本就是过期，不可用于校验（见 §4）。列表响应另带一个只读的 `verifiable` 布尔值，按当前时刻算，方便管理员看。
- 列表按 `api_key` 的创建先后、再按 `key_version` 排序，分页沿用 `page` / `page_size` 约定。
- 响应里没有内部自增 id、`tenant_id`、`project_id`（内部）、密文、主密钥版本。

**格式**：

- `api_key` = `ak_` + 32 个小写十六进制字符（`secrets.token_hex(16)`）。前缀让密钥扫描工具与人都认得出它是什么。
- `secret` = `sk_` + 64 个小写十六进制字符（`secrets.token_hex(32)`，256 位）。HMAC 的密钥就是这整个字符串的 UTF-8 字节。
- 文档与测试里的示例一律用全零占位值（`ak_000…`、`sk_000…`），不写看起来像真的随机串（本仓库 secret-scan 的教训，见 AIH-TASK-011 记录）。

**错误**：

| HTTP | `error.code` | 什么时候 | 写库 |
| --- | --- | --- | --- |
| 401 / 403 | `TOKEN_INVALID` / `ADMIN_REQUIRED` | 沿用 | 否 |
| 404 | `CUSTOMER_NOT_FOUND` | 客户不存在，沿用 | 否 |
| 404 | `PROJECT_NOT_FOUND`（新增） | 项目不存在，或不属于路径里的客户（两者一模一样） | 否 |
| 404 | `CREDENTIAL_NOT_FOUND`（新增） | `api_key` / `key_version` 不存在，或属于别的项目（一模一样） | 否 |
| 409 | `CREDENTIAL_VERSION_CONFLICT`（新增） | 轮换时 `current_key_version` 不是当前最大版本 | 否 |
| 409 | `CREDENTIAL_REVOKED`（新增） | 轮换一个所有版本都已吊销的 `api_key` | 否 |
| 422 | `VALIDATION_ERROR` | 请求体不合法，沿用 | 否 |
| 503 | `ENCRYPTION_NOT_CONFIGURED` | 主密钥未配置（沿用 `app/core/crypto.py`） | 否 |
| 503 | `DATABASE_NOT_CONFIGURED` | 沿用 | 否 |
| 500 | `INTERNAL_ERROR` | 意外错误，沿用 | 整个事务回滚 |

### 数据库（迁移 0007）

新表 `integration_credentials`（§74.4 的列，另加一列主密钥版本）：

| 列 | 类型 | 说明 |
| --- | --- | --- |
| `id` | BIGINT PK | 内部，不出库 |
| `tenant_id` | BIGINT NOT NULL | 所属客户 |
| `project_id` | BIGINT NOT NULL | 所属项目 |
| `public_api_key` | VARCHAR(64) NOT NULL | `ak_…`；MySQL 上用 `utf8mb4_0900_bin`（区分大小写、NO PAD，与 `reference_id` 同一理由） |
| `key_version` | INT NOT NULL | 签名密钥版本，客户在 `X-Acuven-Key-Version` 里看到的就是它；`CHECK (key_version >= 1)` |
| `encrypted_secret` | TEXT NOT NULL | `encrypt_secret` 的输出 |
| `encryption_key_version` | INT NOT NULL | **主密钥**版本（`encrypt_secret` 的第二个返回值）。ADR-0004 §4 要求与 `key_version` 分开；重新包裹只改它，不改 `key_version`（ADR-0004 §4b） |
| `status` | VARCHAR(16) NOT NULL | `ACTIVE` / `REVOKED`，`CHECK` 限定 |
| `valid_from` | DATETIME NOT NULL | 建立时刻 |
| `valid_until` | DATETIME NULL | NULL = 没有截止；轮换时写入 |
| `last_used_at` | DATETIME NULL | 本任务不写 |
| `created_at` | DATETIME NOT NULL | |
| `revoked_at` | DATETIME NULL | `CHECK ((status = 'REVOKED') = (revoked_at IS NOT NULL))` |

- **唯一约束 `(public_api_key, key_version)`**，而不是 §74.4 字面的「`public_api_key` 唯一」。这是 Kelvin 2026-09-25 选定「轮换时 API key 不变、新增版本」（方案 A）的直接后果：同一个 `api_key` 的多个版本各占一行，ADR-0004 §4a 的入站轮换流程也正是「旧行旁边加一行新 `key_version`」。§74.4 那句「The unique API key is a lookup identifier」的意图（`api_key` 能唯一定位到凭据）仍然成立：`api_key` 加版本号唯一定位一行，且同一个 `api_key` 的所有行都属于同一个项目（见下）。
- **`(project_id, tenant_id)` 复合外键 → `projects(id, tenant_id)`**，`ON DELETE RESTRICT`。为此给 `projects` 加唯一约束 `uq_projects_id_tenant (id, tenant_id)`（`id` 本来就唯一，这个约束只为让复合外键成立）。效果：凭据行的 `tenant_id` 不可能与它的项目所属客户不一致，由数据库保证（INV-8）。
- **同一个 `api_key` 的所有版本属于同一个项目**：轮换时新行的 `tenant_id` / `project_id` 取自锁住的旧行，不取自别处；并有测试断言。数据库层不再加触发器：唯一能插入新版本的代码路径就是轮换。
- 索引：`ix_integration_credentials_project_id (project_id)`；唯一约束本身覆盖按 `api_key` 的查找。

### 加密：绑定到行（AAD）

`encrypt_secret` / `decrypt_secret` 加一个可选关键字参数 `associated_data: bytes | None = None`，传给数据层的 AES-GCM（包裹 DEK 那一层不变）。默认 `None`，2FA 的调用与已存的密文完全不变，令牌格式不变。

凭据的 AAD = `b"integration_credentials|" + api_key + b"|" + str(key_version)`。效果：把一行的密文拷到另一行（换 key 或换版本）就解不开，`DecryptionFailed`。spec 没有要求这一条，但成本只有几行，而它挡住的是「有库写权限的人把自己知道的 secret 换到别人的 key 下」这一类攻击。

### 校验库（不接端点）

`app/services/integration_auth.py`：

- `canonical_request(method, path_and_query, timestamp, request_id, body: bytes) -> str`：§37 的五行，`\n` 连接：
  1. `method`：大写；
  2. `NORMALIZED_PATH_AND_QUERY`：**本设计补的定义**（spec 没写死，见 §10）。路径原样取（不解码、不折叠 `..`、不去尾斜杠）；查询串按 `&` 拆成键值对（保留空值），按键、再按值做字节序排序后用 `&` 重新连接，原样保留各自的百分号编码；没有查询串时不带 `?`；
  3. `X-Acuven-Timestamp`：**本设计补的定义**：Unix 纪元秒的十进制整数字符串，不带小数、不带正负号、不补零；
  4. `X-Acuven-Request-Id`：原样；
  5. `SHA256(RAW_REQUEST_BODY)` 的小写十六进制（空请求体也照算）。
- `verify_signature(secret, *, method, path_and_query, timestamp, request_id, body, signature, now, max_skew_seconds=300) -> None`：不通过就抛 `SignatureRejected(code)`，`code` 取 `MALFORMED_TIMESTAMP`、`TIMESTAMP_OUT_OF_WINDOW`、`BAD_SIGNATURE` 之一。时间窗是 `|now - timestamp| <= max_skew_seconds`（§37 建议的 ±5 分钟）。签名是 HMAC-SHA256 输出的小写十六进制，用 `hmac.compare_digest` 比较（常量时间）。签名不是 64 位小写十六进制时按 `BAD_SIGNATURE` 处理。
- `find_verifiable_credential(session, api_key, key_version, now) -> IntegrationCredential | None`：`status = ACTIVE`、`valid_from <= now`、且 `valid_until IS NULL OR now < valid_until` 的那一行，否则 `None`。不存在、已吊销、已过期、版本号不是正整数，全部返回 `None`：以后的端点对这些情况给同一个 401，不让调用方区分。
- 解密交给调用方（`decrypt_secret`，带同样的 AAD）。本任务不做 nonce、不写 `last_used_at`、不缓存。

### 事务边界

每个管理端动作一个 `session_scope()`：凭据行的写入与审计同一事务（REQ-TXN-001 的精神；本任务不涉及钱包与状态跃迁）。repository 只 flush。

- **建凭据**：不加锁；`api_key` 是新随机值，唯一约束兜底（撞上的概率可忽略，撞上就是 500，不重试）。
- **轮换 / 吊销**：`SELECT … FROM integration_credentials WHERE public_api_key = :k AND project_id = :p ORDER BY key_version FOR UPDATE`，锁住这个 key 的所有版本行，再做判断与写入。两次轮换并发时，后到的在锁上等待，拿到锁后读到新的最大版本号 → `CREDENTIAL_VERSION_CONFLICT`；唯一约束 `(public_api_key, key_version)` 是最后的兜底（`IntegrityError` 映射为同一个 409）。
- 客户与项目用不加锁的读，只为拿内部 id；不锁 `tenants` / `projects`（与钱包路径没有交集，不涉及加锁顺序）。

### 时间

`created_at` / `valid_from` / `revoked_at` 用 `utc_now()` 截到整秒（与 `services/customers.py` 的 `_now` 同一写法）。`valid_until = now + overlap`。

### 配置

`credential_rotation_overlap_seconds: int = 604800`（7 天，Kelvin 2026-09-25），`ge=0`。ADR-0004 §4a 要求可配置、不写死；运维手册（runbook）里写明怎么改。`0` 表示轮换即让旧版本立刻失效。

### 审计（§66 的名字）

`AuditAction` 加 `API_KEY_CREATE`、`API_KEY_ROTATE`、`API_KEY_REVOKE`（加值不需要迁移，`app/models/auth.py` 的约定）。`entity_type = integration_credential`，`entity_id = api_key`，`actor_user_id` / `actor_role` 是管理员，带 ip 与 user agent：

- `API_KEY_CREATE`：`after_state` = `api_key`、`key_version`、`project_public_id`、`tenant_public_id`、`valid_from`；
- `API_KEY_ROTATE`：`before_state` = 各未吊销版本的 `key_version` 与 `valid_until`；`after_state` = 新 `key_version`，以及各旧版本改后的 `valid_until`；
- `API_KEY_REVOKE`：`before_state` / `after_state` = 受影响版本的 `key_version` 与 `status`；`reason` 列 = 请求里的原因。

审计里**永远没有** `secret`、密文、主密钥版本。

## 3. 不变量影响矩阵

| 不变量 | 是否触碰 | 可能怎样被破坏 | 设计控制 | 验证方式 |
| --- | --- | --- | --- | --- |
| INV-1 中心故障不中断 AI | 否 | 本任务只在管理端；校验库不接端点 | — | — |
| INV-2 事件不重复扣费 | 否 | 不处理用量事件 | — | — |
| INV-3 支付不重复入账 | 否 | 不处理支付 | — | — |
| INV-4 余额只经账本变动 | 否 | 不碰钱包 | — | — |
| INV-5 历史账本不可变 | 否 | 不碰账本 | — | — |
| INV-6 事件保留版本引用 | 否（为以后铺路） | 以后的用量事件要记 `integration_credential_id`（§79）；本表的行永不删除（`RESTRICT`、没有删除接口），这个引用以后不会悬空 | 没有删除路径 | 用例：模块里没有删除凭据行的函数 |
| INV-7 客户不可见成本毛利 | 否 | 本任务没有客户侧接口 | — | — |
| INV-8 租户不可互访 | 是 | ① 匿名或 CUSTOMER 管理凭据；② 用 A 客户的路径操作 B 客户的项目或凭据；③ 凭据行的 `tenant_id` 与项目不一致，以后按凭据认证时串户 | ① `require_admin` 进路由枚举用例；② 项目按「客户 + 项目 public_id」查，凭据按「项目 + api_key」查，别人的一律 404；③ 复合外键 `(project_id, tenant_id)` → `projects(id, tenant_id)` | 用例：401 / 403 不写库；A 的客户路径 + B 的项目 → 404；A 的项目路径 + B 的 api_key → 404；MySQL 上直接插入不一致的 `tenant_id` 被外键拒绝 |
| INV-9 对话内容不入库 | 否 | 不处理对话 | — | — |
| INV-10 金额用 Decimal | 否 | 没有金额 | — | — |
| INV-11 至多一次财务效果 | 否 | 没有财务效果 | — | — |
| INV-12 定稿对账单不可变 | 否 | — | — | — |
| INV-13 状态与事件原子提交 | 是（凭据状态 + 审计） | 凭据行进了库、审计没进，或反过来 | 一个 `session_scope` | 用例：注入审计写入失败、注入提交失败，SQLite 与 MySQL 各一次：凭据行与审计都不留下（建凭据、轮换、吊销各一组） |
| INV-14 队列丢失不毁持久工作 | 否 | 不入队 | — | — |

REQ-AUTH-001（加密、版本化、轮换、不入日志）是本任务的主体，见 §6 与 §7。

## 4. 状态与并发

每个凭据版本的状态：

| 当前状态 | 事件 | 前置条件 | 新状态 | 副作用 | 非法时结果 |
| --- | --- | --- | --- | --- | --- |
| （无） | 建凭据 | 项目存在且属于该客户 | `ACTIVE`，`valid_until = NULL` | `API_KEY_CREATE` | 404 |
| 最新版本 `ACTIVE` | 轮换 | `current_key_version` = 当前最大版本；该 key 至少一个版本 `ACTIVE` | 新版本 `ACTIVE`、`valid_until = NULL`；旧的未吊销版本中 `valid_until` 为 NULL 或晚于 `now + overlap` 的，改为 `now + overlap` | `API_KEY_ROTATE` | 409 `CREDENTIAL_VERSION_CONFLICT` / `CREDENTIAL_REVOKED` |
| `ACTIVE` | 吊销版本 | — | `REVOKED`，`revoked_at = now` | `API_KEY_REVOKE` | — |
| `REVOKED` | 吊销版本 | — | 不变 | 无（幂等） | — |
| 任意 | 吊销 key | — | 该 key 所有 `ACTIVE` 版本 → `REVOKED` | 一条 `API_KEY_REVOKE`（列出受影响版本） | 全部已吊销时不变、不写 |
| `REVOKED` | 任何事件 | — | 不变 | — | **终态**：没有「恢复」 |

- **可用于校验** = `ACTIVE` 且 `valid_from <= now` 且（`valid_until` 为空或 `now < valid_until`）。过期不需要定时任务去改状态：时间到了就自然不可用。
- **重叠期里同时可用的版本**：新版本，加上还没到 `valid_until` 的旧版本。管理员看 `last_used_at`（以后有流量时）确认旧版本没有流量后，可以提前吊销它（ADR-0004 §4a 第 4、5 步）。
- 吊销最新版本、而旧版本还在重叠期内：允许。结果是这个 key 只剩旧版本可用，直到它过期；这是管理员的明确操作，审计里看得见。
- **并发**：同一个 key 的轮换与吊销都锁该 key 的全部版本行，串行执行；不同 key、不同项目互不阻塞。
- **数据库负责的唯一性**：`(public_api_key, key_version)`；`projects (id, tenant_id)`。

## 5. 失败与恢复矩阵

| 失败点 | 对外结果 | 数据最终状态 | 是否可重试 | 恢复来源 | 告警 |
| --- | --- | --- | --- | --- | --- |
| 校验失败（多余字段、`reason` 空或超长、`current_key_version` 不是正整数） | 422，只列字段名 | 没有写入 | 修正后重试 | — | 无 |
| 无令牌 / 非 ADMIN | 401 / 403 | 没有写入 | — | — | 无 |
| 客户、项目、凭据不存在或不匹配 | 404 | 没有写入 | 否 | — | 无 |
| 主密钥未配置 | 503 `ENCRYPTION_NOT_CONFIGURED` | 没有写入（在生成 secret 与写库之前判定） | 配好后 | 部署配置 | 全局处理器记录 |
| 轮换版本冲突 / key 已全部吊销 | 409 | 没有写入 | 否（刷新列表后再决定） | 管理员 | 无 |
| 加密失败、写凭据或审计失败、提交失败 | 500 | 整个事务回滚，什么都不留下；**已生成的 secret 从未返回给调用方**，随事务一起丢弃 | 是（建凭据：直接重试；轮换：带同一个 `current_key_version` 重试） | 管理员重发 | 全局处理器记异常与 request_id；异常信息里没有 secret（§6） |
| 进程在提交后、响应前崩溃 | 连接断开 | 新版本已入库，但调用方没拿到 secret | 是：对轮换，再轮换一次（旧的那个新版本随重叠期失效，或直接吊销它）；对建凭据，吊销后重建 | 管理员 | 同上 |
| 吊销重复提交 | 200，状态不变 | 不变 | — | — | 无 |
| 数据库未配置 | 503 | 没有写入 | — | 部署配置 | `/readyz` |
| Redis / Celery 丢失 | 不影响：本任务不入队、不用 Redis | — | — | — | — |

「提交后、响应前崩溃」那一行是 secret「只显示一次」的固有代价：secret 不可能事后再取。恢复方式是生成新的，而不是找回旧的。

## 6. 数据与安全边界

- **secret 的生命周期**：
  - 生成：`secrets.token_hex(32)`，只在建凭据 / 轮换的那次请求内存里；
  - 存储：`encrypt_secret`（AES-256-GCM 信封加密，带行 AAD）后写 `encrypted_secret`；库里没有明文，也没有可逆的弱编码；
  - 返回：只在那一次 201 响应的 `secret` 字段里；响应带 `Cache-Control: no-store`；
  - 读取：只有校验路径（以后的摄取端点）调用 `decrypt_secret`，本任务里只有测试调用它；
  - 轮换：新 secret 新行，旧行的密文不动，只写 `valid_until`；
  - 吊销：只改状态，不删行、不清密文（保留可追溯性，吊销后校验函数不再返回它）。
- **主密钥**：沿用 ADR-0004 §2（Docker Compose `secrets:` 文件，UID 10001、`0400`，不走环境变量）。生产上 2FA 已在用它，本任务不新增任何密钥文件。
- **鉴权主体**：只从已验签的访问令牌取，角色以数据库为准（`require_admin`）。审计的 `actor_user_id` 就是这个管理员。
- **租户过滤**：管理端按角色跨租户（§56），但每个查询都以路径里的客户、项目为界，见 INV-8 一行。
- **CSRF**：访问令牌走 `Authorization: Bearer` 头，与 AIH-TASK-006 相同，不受 CSRF 影响（§96）。
- **日志与异常**：这一层不写日志。引擎已是 `hide_parameters=True`，SQL 异常文本里没有参数值（密文也不会出现）。secret 只作为局部变量传给 `encrypt_secret` 和响应模型，不进任何异常消息、不进 `repr`：响应模型的 `secret` 字段用 `SecretStr` 之类的包装，`repr` 显示为掩码，只在序列化响应时取值。
- **审计**：见 §2「审计」；不含 secret、密文、主密钥版本。
- **客户侧禁止返回的字段**：本任务没有客户侧接口；管理端响应只有 §2 的白名单。
- **仓库是公开的**：文档、测试、PR 正文里的示例只用全零占位值；真实的 secret 只存在于生产库的密文与那一次响应里。
- **prompt / response**：不处理。
- **数据保留**：凭据行永久保留（以后用量事件要引用它），没有删除接口。

## 7. 测试证据计划

SQLite 测接口契约、鉴权、校验、加密与泄露；MySQL（CI 必跑、不许 skip）测迁移、复合外键、锁与并发、回滚。

| 风险/需求 | 测试层级 | 场景 | 预期结果 |
| --- | --- | --- | --- |
| 建凭据 | API（SQLite） | ADMIN 为项目建凭据 | 201；键集合等于白名单 + `secret`；`api_key` 符合 `^ak_[0-9a-f]{32}$`、`secret` 符合 `^sk_[0-9a-f]{64}$`、`key_version = 1`、`ACTIVE`、`valid_until = null`；`Cache-Control: no-store`；一行凭据、一条 `API_KEY_CREATE` |
| secret 只返回一次 | API（SQLite） | 建完再列表 | 列表项没有 `secret` 字段，也没有密文；任何响应体里都搜不到那个 secret |
| 存储是密文 | 服务（SQLite） | 建凭据后读库 | `encrypted_secret` 不含 secret 原文；`decrypt_secret`（带 AAD）还原出的值等于返回的 secret；`encryption_key_version` 等于主密钥的活动版本 |
| AAD 绑定 | unit / 服务 | 把一行的 `encrypted_secret` 拷到另一行（换 key、换版本）再解密 | `DecryptionFailed`；2FA 的既有密文（不带 AAD）照常解开 |
| 泄露：日志 | API（SQLite） | 建凭据成功一次、轮换一次，再让一次轮换在写审计时失败；抓全部日志 | 日志里没有任何一个 secret |
| 泄露：审计 | 服务 | 三种动作之后读 `audit_logs` | 前后状态与 `reason` 里都没有 secret、密文；键集合与 §2「审计」一致 |
| 泄露：异常信息 | 服务 | 注入失败后检查抛出的异常及其 `__cause__` 链的 `str` / `repr` | 都不含 secret |
| 鉴权 | API（SQLite） | 本任务的五个路由进 `EXPECTED_ADMIN_ROUTES` 与 `VALID_BODIES`；无令牌、CUSTOMER 令牌 | 401 / 403；`integration_credentials`、`audit_logs` 行数不变 |
| 处理函数顺序 | unit | AST 取五个处理函数的第一条语句 | 都是对 `require_admin` 的调用 |
| 跨客户 / 跨项目 | API（SQLite） | A 的客户路径 + B 的项目；A 的项目 + B 的 `api_key`；不存在的 `key_version` | 404（`PROJECT_NOT_FOUND` / `CREDENTIAL_NOT_FOUND`），响应一模一样，不写库 |
| 多余字段 | API（SQLite） | 请求体带 `api_key` / `secret` / `key_version` / `valid_until` / `tenant_id` | 422 |
| 轮换 | 服务（SQLite） | 建凭据后轮换（`current_key_version = 1`） | 新版本 2，`api_key` 相同；版本 1 的 `valid_until = now + overlap`；版本 2 的 `valid_until = null`；一条 `API_KEY_ROTATE` |
| 重叠期可配置 | 服务 | overlap 设为 0 与 3600 | 版本 1 的 `valid_until` 分别是 `now` 与 `now + 3600s` |
| 轮换冲突 | API（SQLite） | 用 `current_key_version = 1` 连续轮换两次 | 第二次 409 `CREDENTIAL_VERSION_CONFLICT`，只有一个新版本 |
| 并发轮换 | 服务（MySQL） | 两个线程同时以 `current_key_version = 1` 轮换 | 恰好一个成功、一个 409；只有版本 2，没有版本 3；没有 500 |
| 轮换已吊销的 key | API（SQLite） | 吊销整个 key 后轮换 | 409 `CREDENTIAL_REVOKED` |
| 吊销版本 | API（SQLite） | 吊销版本 1；再吊销一次 | 第一次 200、`REVOKED`、`revoked_at` 有值、一条审计；第二次 200、不写审计 |
| 吊销 key | API（SQLite） | 有两个版本时吊销整个 key | 两个版本都 `REVOKED`；一条审计列出两个版本 |
| 校验可用性 | 服务（SQLite） | `find_verifiable_credential` 对：生效中的版本；重叠期内的旧版本；`valid_until` 已过的旧版本；吊销的版本；不存在的 key；版本号 0 或负数 | 前两种返回行，其余全部 `None` |
| 签名：正确 | unit | 用固定 secret、固定请求算出的签名（测试里给出期望的十六进制值，作为以后 Billing Client 的对照向量） | 通过 |
| 签名：篡改 | unit | 改方法、路径、查询串、请求体一个字节、request id、时间戳、签名一个字符 | 每种都 `BAD_SIGNATURE`（时间戳越界的那种是 `TIMESTAMP_OUT_OF_WINDOW`） |
| 签名：规范化 | unit | 查询参数顺序不同、重复键、空值；空请求体 | 规范化结果相同的请求签名相同；空请求体按 `SHA256("")` 计算 |
| 签名：时间窗 | unit | 时间戳偏差 300 秒、301 秒、-301 秒；`"1.5"`、`"+100"`、`"abc"`、空串 | 300 通过；±301 `TIMESTAMP_OUT_OF_WINDOW`；其余 `MALFORMED_TIMESTAMP` |
| 签名：格式与常量时间 | unit | 签名是大写十六进制、长度不对、非十六进制；源码检查 | 都 `BAD_SIGNATURE`；比较用 `hmac.compare_digest`（源码断言） |
| 回滚 | 服务（SQLite 与 MySQL 各一次） | 建凭据、轮换、吊销：分别注入审计写入失败与提交失败 | 凭据行与审计都与调用前相同 |
| 主密钥未配置 | API（SQLite） | `master_key_file` 为空时建凭据 / 轮换 | 503 `ENCRYPTION_NOT_CONFIGURED`，不写库；列表与吊销照常工作（不需要主密钥） |
| 迁移 0007 | 迁移（MySQL） | 升降；列、约束、索引、排序规则；复合外键拒绝不一致的 `tenant_id`；有凭据的项目删不掉 | 与 §2「数据库」一致 |
| 没有删除路径 | unit | 检查 repository 模块 | 没有删除凭据行的函数，没有 `DELETE FROM integration_credentials` |
| 防重放 | — | 本任务不做 nonce（Kelvin 2026-09-25） | 后移，记入 `docs/TODO.md`，随摄取端点补齐 REQ-AUTH-001 的 replay 证据 |

## 8. 迁移与上线

- **迁移 0007**：新建 `integration_credentials`（含 `CHECK`、唯一约束、索引、复合外键）；给 `projects` 加唯一约束 `uq_projects_id_tenant (id, tenant_id)`。生产上 `projects` 只有两行（验收夹具与废弃的核对客户），加约束瞬间完成；新表为空。`downgrade` 先删表、再删约束。文件头写 §132 第 13 条分析（锁表、备份、部署顺序、失败处理、回滚）。
- **部署顺序**：合并即部署；迁移由部署脚本执行。前提：生产已配置主密钥文件（2FA 在用，已满足）。
- **兼容**：`encrypt_secret` / `decrypt_secret` 只加带默认值的关键字参数，2FA 与既有密文不受影响。
- **回滚**：回退合并提交并执行 0007 的 `downgrade`（此时表里若已有凭据，先确认没有人依赖它们——Phase 1 没有任何消费方）。
- **监控**：沿用应用日志与 `/readyz`。上线后在验收夹具项目上建一个凭据、轮换一次、吊销，核对审计与密文（部署后核对项，写进 TODO）。

## 9. 方案取舍

| 方案 | 优点 | 风险 | 未采用原因 |
| --- | --- | --- | --- |
| **采用**：轮换时 `api_key` 不变、新增版本（Kelvin 2026-09-25 选方案 A） | 后端只换 secret；`X-Acuven-Key-Version` 有意义；与 §37 的请求头和 ADR-0004 §4a 一致 | 唯一约束要改成 `(public_api_key, key_version)`，偏离 §74.4 的字面 | — |
| 每次轮换发新的 `api_key`（方案 B） | 完全照 §74.4 字面 | `X-Acuven-Key-Version` 基本无用；后端要同时换两个值 | Kelvin 选了 A |
| 状态只存 `ACTIVE` / `REVOKED`，过期由 `valid_until` 推出（采用） | 不需要定时任务去改状态；时间到了自然失效 | 列表里要现算 `verifiable` | — |
| 存 `EXPIRED` 状态 | 状态一眼可见 | 要有定时任务按时翻状态，任务一停状态就错 | 多一个会出错的部件 |
| 密文绑定 AAD（采用） | 行间拷贝密文无效 | `crypto.py` 多一个参数 | spec 没要求，但成本极低 |
| 不加 AAD | 不动 `crypto.py` | 有库写权限的人能把已知 secret 换到别的 key 下 | 防得住的就防 |
| 复合外键保证 `tenant_id` 与项目一致（采用） | 数据库层保证，不靠代码自觉 | `projects` 要多一个唯一约束 | — |
| 只在服务层检查 | 不动 `projects` | 以后新代码路径可能漏掉 | INV-8 值得数据库层兜底 |
| 轮换带 `current_key_version`（采用） | 双击、并发轮换得到确定的 409，不会产生管理员不知道的 secret | 调用方多传一个字段 | — |
| 轮换用幂等键 | 双击得到同一个结果 | 重放时要能再次返回同一个 secret，就得存明文或可再解密的副本，与「只显示一次」冲突 | 与 §36 冲突 |
| 把出站 webhook 密钥一起做 | 一次过闸门 | 范围翻倍，而且还没有发 webhook 的代码 | Kelvin 2026-09-25：方案 i 另过闸门 |

## 10. 未决问题与假设

- **未决问题**：无阻断项。
- **Kelvin 2026-09-25 的决定**：
  1. 出站 webhook 签名密钥用 ADR-0004 §4a 方案 i（新建 `project_webhook_secrets` 表），另过设计闸门；该决定写回 ADR-0004（随本任务的登记 PR）；
  2. 轮换时 `api_key` 不变、新增版本（方案 A）；
  3. 重叠期默认 7 天，可配置；
  4. 本任务只做纯函数签名校验，防重放存储与 replay 证据随摄取端点后移。
- **本设计补的定义**（spec §37 没有写死，Billing Client 与服务端必须一致）：`X-Acuven-Timestamp` 是 Unix 纪元秒的十进制整数；`NORMALIZED_PATH_AND_QUERY` 的规则见 §2「校验库」。这两条写进 `docs/api.md` 的签名一节，作为唯一出处。
- **假设**：
  1. 生产已配置主密钥文件（2FA 在用）；
  2. 管理员人数少，同一个 key 的行锁不会成为瓶颈。
- **明确后移**：出站 webhook 密钥表；防重放 nonce 与摄取端点；`last_used_at` 写入；解密结果缓存；主密钥版本重新包裹；客户门户自助管理；前端页面。
- **如果假设错了**：主密钥没配置时，建凭据与轮换返回 503、什么都不写，其余功能不受影响。

## 11. 审查与版本绑定

审查方要回答的五个问题见 [scripts/review_checklist.md](../../scripts/review_checklist.md) 的「设计审查」一节。

**版本绑定规则**（出处是 [WORKFLOW §3](../WORKFLOW.md)）：

- 批准必须写成 `APPROVED: design v<N>`，`<N>` 取本 Issue 顶部的「设计版本」
- **设计版本一变，之前的 APPROVE 自动作废**，必须重新过闸门
- 什么算「实质修改」：改了契约、表结构、事务边界、状态机、失败语义、不变量控制。改错别字不算
- 实质修改时：把顶部版本 +1，在下方追加一条变更说明，状态退回 `READY_FOR_REVIEW`

### 设计闸门判定

同时满足才算通过：

- [ ] 需求、非目标和验收标准明确
- [ ] 关键契约与事务边界明确
- [ ] 触碰的不变量都有控制措施
- [ ] 失败路径都有确定的最终状态
- [ ] 高风险控制都有测试场景
- [ ] 没有未解决的阻断假设
- [ ] 审批绑定到明确的设计版本

### 实现范围（批准后 Worker 的 `allowed_change_paths`）

1. `alembic/versions/<日期>_0007_integration_credentials.py`：新建，迁移 0007
2. `alembic/env.py`：import 新模型
3. `app/models/integration.py`：新建，`IntegrationCredential` 与状态枚举
4. `app/models/tenancy.py`：`projects` 的 `uq_projects_id_tenant`
5. `app/models/auth.py`：`AuditAction` 加三个值
6. `app/core/crypto.py`：`associated_data` 可选参数
7. `app/core/config.py`：`credential_rotation_overlap_seconds`
8. `app/repositories/integration_credentials.py`：新建
9. `app/services/integration_credentials.py`：新建，四个管理端动作
10. `app/services/integration_auth.py`：新建，规范化、签名校验、`find_verifiable_credential`
11. `app/schemas/integration_credentials.py`：新建
12. `app/api/admin_customers.py`：五个处理函数
13. `tests/backend/test_admin_customers_api.py`：`EXPECTED_ADMIN_ROUTES` 与 `VALID_BODIES`
14. `tests/backend/test_integration_credentials_api.py`：新建
15. `tests/backend/test_integration_credentials_service.py`：新建
16. `tests/backend/test_integration_auth.py`：新建
17. `tests/backend/test_crypto.py`：AAD 用例
18. `tests/backend/test_migrations.py`：0007
19. `docs/api.md`：五个接口的契约，与签名规则（时间戳格式、规范化）
20. `docs/database-schema.md`：新表、`projects` 的新约束，更新「尚未建的列」
21. `docs/TODO.md`：任务记录；Phase 1 的「API 凭据」在 CI 全绿并合并时勾上；记下后移的 replay 证据

## 12. 版本变更记录

| 版本 | 日期 | 改了什么 | 触发原因 |
| --- | --- | --- | --- |
| v1 | 2026-09-25 | 初稿 | — |


# AI 模块：acuven_ai_api 并入本服务（已批准 v13）

> **来源**：设计闸门 Issue #281。本文件是 `APPROVED: design v13` 那一版正文的**逐字副本**，审查者为 Codex。
> 放进仓库，是因为 OpenClaw Worker 在沙箱里不联网、读不到 GitHub Issue。
> **实现以本文件为准**；与 Issue 不一致时，以 Issue #281 上被批准的 v13 为准。设计要改，就回到 Issue 升版本、重新过闸门，不要直接改本文件。
> §8「拆分」的十一项登记为十四个任务（§8 第 6、8 项写明可分批交付）：第 1–5 项依次是 `AIH-TASK-056`（数据库）、`AIH-TASK-057`（摄取契约加 `feature`）、`AIH-TASK-058`（管理规则）、`AIH-TASK-059`（管理接口）、`AIH-TASK-060`（AI 纯规则）；第 6 项分三批：`AIH-TASK-061`（用量写入、提交结果核查与落库失败处置）、`AIH-TASK-062`（配置、key 文件、公网入口与优雅停机）、`AIH-TASK-063`（调用接口）；第 7 项是 `AIH-TASK-064`（投递）；第 8 项分两批：`AIH-TASK-065`（告警维度）、`AIH-TASK-066`（命令与运维文档）；第 9、10、11 项依次是 `AIH-TASK-067`（健康检查）、`AIH-TASK-068`（前端）、`AIH-TASK-069`（CI 策略）。
> 要搬的 `acuven_ai_api` 源码与测试放在 [ai-module-reference/](ai-module-reference/README.md)（`b57c5b0` 的逐字副本）。
> 正文与 Issue 逐字相同；正文里的相对链接按本文件在 `docs/design/` 下的位置书写，可直接点开。

---

## 0. 准入判定

本任务档位：`全部`。它新增 AI 调用方的认证（AI 密钥）、本地用量 outbox 的投递状态机，让本服务第一次以「集成方」身份向自己的摄取端点上报用量，并给用量摄取契约加一个可选字段 `feature`（用量摄取、幂等指纹、INV-1 都在路径上）。

---

状态：`READY_FOR_REVIEW`（§10 的问题 Kelvin 2026-10-12、2026-10-13 已全部答复）
负责人：Claude
**设计版本**：`v13`（每次实质修改 +1，见 §12）
对应需求：spec §1 `REQ-AVAIL-001`、§2.1、§11、§20、§21、§22、§26、§36、§37、§38、§39、§79、§99、§121、§126、§133、§135；`REQ-AUTH-001`、`REQ-INGEST-001`、`REQ-IDEMP-001`、`REQ-PRIV-001`；[ADR-0002](../adr/ADR-0002-production-datastore-isolation.md)、[ADR-0004](../adr/ADR-0004-credential-encryption.md)；新增 spec §141 与 ADR-0012（规格 PR #283）；项目群决定 `acuven_hub/DECISIONS.md` E-17、E-18、E-19、E-20（Kelvin 2026-10-12，出处 MERGE 1–8）
目标 PR：规格与 ADR 的文档 PR #283；实现 PR 待开（批准后按 §8「拆分」登记）

## 1. 目标与边界

- **要解决的问题**：Kelvin 2026-10-12 决定把 `acuven_ai_api`（系统侧 AI API）并入本服务，合成一个服务进程：同一个应用、一套部署、一个数据库，身份统一成「租户 × 项目」（E-17、E-18）。计费仍异步、不阻断 AI（E-19，F-01 不变）；AI 自己的用量仍用项目签名凭据走 HMAC 上报本服务自己的摄取端点；Chatbot（`acuven_aichat`）仍是外部接入方；接受与计费共用发布节奏，但要有缓解（E-20）。2026-10-13 Kelvin 进一步定：**功能的定义与开发放在业务系统（CRM 等），AI 模块只负责调用模型与计费；增加功能只需改业务系统**。第 3 阶段试点和 E-13 的成功标准在合并后的服务上验收。
- **可观察的完成标准**：
  1. 本仓库新增子包 `app/ai_api/`。AI 请求路径只读三类数据（AI 密钥、模型目录、租户与项目的状态列），只写一张表（`ai_usage_outbox`），不 import 钱包、账本、定价、汇率、支付、用量摄取与计费的任何模块；由一条 AST 白名单测试机械保证（§7）。
  2. 对外只有一个调用接口 `POST /v1/messages`：请求是 Anthropic Messages API 的一个子集（`model`、`system`、`messages`、`max_tokens`、`output_config`）加一个必填的 `feature`（功能标签）；响应是模型原文、`stop_reason`、四类 token 与 `request_id`。提示词、输入输出格式、输出解析与质量评测全部归调用方。`acuven_ai_api` 的 `/v1/capabilities/{capability}` 不搬（没有任何调用方在用，CONTACT_SUMMARY_V1 改为 CRM 自己的内部设计）。
  3. 护栏：只许用模型目录里状态为 `ACTIVE` 的 Anthropic 模型；请求体 ≤ 64 KiB；`max_tokens` ≤ 4000（两个上限可配置）。
  4. AI 调用方就是「租户 × 项目」：管理员在现有后台给某个项目签发 AI 密钥（只显示一次）、吊销 AI 密钥，都写审计。没有能力白名单。`effective-status` 的字段与语义不变，只表达钱和账户（F-09）。
  5. **功能标签**：每次调用带 `feature`（如 `crm.contact_summary`），随用量事件送进摄取端，存进 `usage_events.feature`；后台用量事件列表可按功能筛选并显示。摄取契约里它是**可选**字段，外部接入方（Chatbot）可以带也可以不带。
  6. 推理成功后先写 `ai_usage_outbox` 再返回；celery-beat 周期扫描，celery-worker 用该项目现有的 `ACTIVE` 集成凭据按 HMAC 契约 POST 到本服务的 `/api/v1/integration/usage-events`——与 Chatbot 走同一套验签、幂等与入账。摄取端点或计费 worker 全部失败时，AI 调用仍返回 200、outbox 行留在 `PENDING`（CI 测试锁住）。
  7. 发布耦合的缓解：`/healthz/ai` 与 `/healthz/billing` 两个就绪检查，部署冒烟以 `/healthz/ai` 为准；AI 请求有 75 秒总时限，api 优雅停机 85 秒（`stop_grace_period` 100 秒），停机时被取消的在途调用也留下告警与暂存；AI 模块的改动和支付 / 钱包改动不进同一个 PR，由 CI 机械检查。
  8. 第 3 阶段两条路径（内部计量、`PREPAID` 测试租户）加 E-13 标准在合并后的服务上验收；验收记录写明「自调验不到跨主机网络与时钟偏差，留给 `acuven_aichat` 接入时验收」（E-19）。
- **明确不做什么**：
  - AI 模块部署到客户机器（G-03 2026-10-12 已取消，写进 spec §121）。
  - 同步扣费：AI 请求路径上不查余额、不扣钱包、不等计价（写进 spec §121）。
  - 给 Chatbot 或其他外部接入方转发模型调用：它们仍自己直连供应商（spec §121 / §135 的「中央 AI 网关」限定为对外部接入方）。
  - Messages API 子集以外的能力：工具调用、图片、流式输出、`thinking` 配置、`temperature`、`stop_sequences`、供应商兜底（`fallbacks`）。要用时改 AI 模块，另做。
  - 按功能的汇总统计页（每功能的次数、平均 token、成本）：本设计只把 `feature` 存下来并支持筛选；汇总归 Phase 5 用量分析（写进 TODO 后续计划）。
  - 请求级幂等键、按客户端用量限流、供应商未知用量对账、按事件自动核对最终计价、本地 outbox 清理——留在正式档，不在本设计。
  - 定价规则加项目作用域（F-08，spec §16，TODO 后续计划里另立设计闸门）。本设计不依赖它。
  - CRM 侧接入代码（在 `acuven_crm_demo` 仓库做；§2「调用方怎么接」给出它要做的事）。
  - `acuven_ai_api` 仓库的 README 指向与归档（在那个仓库另做，§8）。
- **现有行为与问题证据**：
  - `acuven_ai_api` 2026-10-10 部署到 VPS（只在内网）。**2026-10-12 只读核实**：库 `acuven_ai_api` 在共享 `infra_mysql` 上，只有 `usage_outbox` 一张表且**为空**；`AI_API_CLIENTS_JSON` 有 **0** 个客户端绑定；从未发生真实模型调用。所以**不需要数据迁移**。
  - **2026-10-12 已下线**（Kelvin 答复 Q5）：该仓库的 Deploy 工作流已禁用，VPS 上两个容器已 `docker compose down`；库、镜像与部署目录保留。
  - 它的身份是第二套：`AI_API_CLIENTS_JSON` 里每个 `client_id` 手工绑定 token 摘要、Billing Hub 的租户 / 项目 ID、集成 key / secret 与能力列表，与 Billing Hub 后台要人工对齐（E-18 要消除的就是这一点）。它的每项能力都写死在服务里（提示词、输入格式），加一项能力要改它的代码——这正是 2026-10-13 的决定要去掉的。
  - `usage_events` 现在没有「哪个功能」的字段：只要一个项目里有两个 AI 功能，后台就分不清用量属于哪个功能。
  - 本仓库现状：`deploy/nginx/billing.conf` 的 `location /` 把未知路径交给前端，`/v1/…` 现在会落到 SPA；`Dockerfile` 的 `uvicorn` 没设优雅停机，compose 没设 `stop_grace_period`（Docker 默认 10 秒后 SIGKILL）；`billing_nginx` 在 `proxy_net` 上，`infra_nginx` 以 `billing_nginx:80` 转发。摄取的单条与批量 schema 都是 `extra="forbid"`，加字段要改 schema；指纹（`app/services/usage_ingest.py:payload_fingerprint`）是固定字段集合的规范 JSON。
  - spec 冲突（规格 PR #283 一并处理）：§2.1 / ADR-0002「永不共享数据库」；§121 / §135「V1 不做中央 AI 网关」；§3.3、§126 仍写试点是独立的 `acuven_ai_api`；§11 / §79 没有 `feature`。

## 2. 设计概要

```text
业务后端（CRM 等：自己拼提示词、定输出格式）
  ──(X-Acuven-Client-Id + Bearer)──▶ [公网 HTTPS → infra_nginx →] billing_nginx /v1/（按来源限速）──▶ api 进程
  （同一台 VPS 上的业务后端直接走 proxy_net 上的 http://billing_nginx，不绕公网）
  app/ai_api 请求路径（async）：
    校验请求体（子集、上限、feature）→ 只读事务：AI 密钥 → 项目 / 租户 → compose_effective_status → 模型目录
    → BLOCK_AI：403，不调模型；模型不在目录：422
    → 调 Anthropic（httpx2 AsyncClient，60 s）
    → 新事务：INSERT ai_usage_outbox（PENDING，只有元数据 + feature）→ 提交
    → 200：原文、stop_reason、token、request_id（不解析、不判断内容）
celery-beat（每 10 s）──▶ celery-worker：领取到期行（条件 UPDATE + claim_id + 60 s 租约）
    → 取项目的 ACTIVE 集成凭据、进程内解密 → 按 api.md 的 HMAC 契约签名
    → POST http://api:8000/api/v1/integration/usage-events（与 Chatbot 同一端点、同一验签与入账）
    → 202 / 200 already_* 且 event_id 匹配：DELIVERED；retryable：PENDING + 退避；否则：DEAD + 告警
摄取端点 → usage_events(RECEIVED, feature) → 现有计费 worker 定价、扣费（PREPAID）或只计量（INTERNAL_METERED_ONLY）
```

### 模块边界（E-17）

- 新子包 `app/ai_api/`：`messages.py`（`/v1/messages` 请求 / 响应模型与纯校验规则，只用标准库与 pydantic）、`provider.py`（Anthropic 适配器，async，从 `acuven_ai_api/app/provider.py` 改写）、`access.py`（请求路径的只读查询：密钥、项目 / 租户状态、模型目录）、`routes.py`（`/v1/messages` 路由）、`outbox.py`（模型与写入）、`client_rules.py`（签名规范串、回执与错误判定，**从 `acuven_ai_api/app/billing_rules.py` 原样搬，不复用本仓库验签侧的 `canonical_request`**）、`delivery.py`（Celery 任务：领取、签名、发送、结算）、`admin.py`（AI 密钥的管理服务函数）。管理接口的路由放 `app/api/admin_ai_access.py`。
- **AI 请求路径可 import 的白名单**（`routes.py`、`access.py`、`provider.py`、`messages.py`、`outbox.py`）：`app.ai_api.*`、`app.core.*`（配置、数据库、日志、错误）、`app.models.base`、`app.models.tenancy`（只用 `Tenant`、`Project` 与纯函数 `compose_effective_status`）、`app.models.ai_catalog`（只读 `AiProvider`、`AiModel`、`AiModelAlias`）。投递侧（`delivery.py`、`client_rules.py`）另可 import `app.models.integration`（读 `IntegrationCredential`）、`app.services.integration_auth` 里的 `credential_aad` 与 `is_verifiable`、`app.core.crypto`（解密）。管理与运维侧（`admin.py`、`ops.py`，不在 AI 请求路径上）另可 import `app.models.auth`（`AuditAction`）与 `app.services.auth.record_audit`（写审计），以及管理接口要用的 `app.models.tenancy` 查询；`ops.py` 另可 import 投递侧可用的模块。白名单只约束本仓库的 `app.*` 模块，第三方库（`celery`、`httpx2`、`sqlalchemy`、`pydantic` 等）不受限。其余 `app.*` 模块一律不许，尤其 `app.services.{usage_ingest,usage_billing,pricing_*,provider_prices,fx_rates,wallet_adjustments,internal_billing}`、`app.repositories.{wallet,usage_events,pricing_rules,provider_prices,fx_rates}`、`app.models.{wallet,usage,pricing_rules,provider_prices,fx_rates}`。
- **反向**：本仓库原有模块不许 import `app.ai_api`，例外只有 `app/main.py`（挂路由）、`app/api/admin_ai_access.py`、`app/api/health.py`（`/healthz/ai`）、`app/core/celery_app.py`（注册 beat 任务）、`app/services/alerts.py`（告警维度）、`app/cli.py`（死信重投）与迁移所需的模型汇总处。
- 签名侧独立实现是刻意的：自调时签名方与验签方若共用同一段规范串代码，两边同时写错也会互相验过，自调就什么都没验。另加签名交叉测试（§7）。
- 模型目录的检查只读 `ai_models` 是否存在且 `ACTIVE`，**不读供应商价格**（价格属于定价，不进 AI 请求路径）。模型在目录里但还没发布价格时，用量照常记录，摄取后计价得到 `PRICING_ERROR` 并进现有告警（spec §16「不丢事件」）。

### 调用接口 `POST /v1/messages`

鉴权头：`X-Acuven-Client-Id: aic_…`、`Authorization: Bearer ait_…`。请求体是 JSON 对象，`extra="forbid"`（未列出的字段一律 422）：

| 字段 | 规则 |
| --- | --- |
| `feature` | 必填。功能标签，`^[a-z][a-z0-9_]{0,31}(\.[a-z][a-z0-9_]{0,31}){1,3}$` 且 ≤ 64 字符，例如 `crm.contact_summary`、`shop.order_summary`。调用方自己起名，AI 模块不维护清单；只用来统计，不影响调用与计价 |
| `model` | 必填，≤ 128 字符。必须是模型目录里供应商 `anthropic` 下一个 `ACTIVE` 模型的 `code`，或此刻生效、指向 `ACTIVE` 模型的别名；否则 422 `MODEL_NOT_ALLOWED` |
| `system` | 可选，字符串 |
| `messages` | 必填，1–50 条；每条 `{"role": "user" \| "assistant", "content": 非空字符串}`，第一条必须是 `user` |
| `max_tokens` | 必填，整数 1 – `BILLING_AI_MAX_OUTPUT_TOKENS`（默认 4000） |
| `output_config` | 可选，`extra="forbid"`：`format`（`{"type": "json_schema", "schema": 对象}`）、`effort`（`low` / `medium` / `high`）。原样转给供应商 |

整个请求体 ≤ `BILLING_AI_MAX_REQUEST_BYTES`（默认 65536 字节），在应用里按原始字节判；nginx `client_max_body_size 80k` 是外层兜底。

200 响应（不套 §107 信封，形状贴近 Anthropic，调用方照官方文档写解析即可）：

```json
{
  "request_id": "01J…（26 位 ULID，与用量事件的 request_id 相同）",
  "model": "供应商响应里的实际模型名",
  "text": "所有 text 块按顺序、以一个换行符（\n）拼接；没有 text 块时为空串",
  "stop_reason": "end_turn | max_tokens | refusal | …（供应商原样）",
  "usage": {"input_tokens": 0, "output_tokens": 0, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}
}
```

`request_id` 是 AI 模块生成的 ULID，与用量事件的 `request_id` 相同，调用方存它来对账；它不是 HTTP 关联 ID——中间件的关联 ID（`X-Request-ID` 响应头、错误信封里的 `request_id`）照常存在，两者无关，AI 模块的日志两个都记。`usage` 只是 token 数，不含任何金额（INV-7）。拒答、被截断、输出不合调用方的格式，都**照常 200**，由调用方按 `stop_reason` 与自己的 schema 判断；用量照常记录（token 已消耗）。

错误（§107 信封，状态码是契约）：

| 状态 | 什么时候 | 用量 |
| --- | --- | --- |
| 401 | 缺头、`client_id` 不存在、token 不对、密钥已吊销 | 无 |
| 403 | 租户 / 项目的有效状态是 `BLOCK_AI`（固定文案，不说原因，spec §26） | 无 |
| 413 | 请求体超过 nginx 的 `client_max_body_size 80k`，nginx 直接返回与应用同形的 JSON 信封（`PAYLOAD_TOO_LARGE`，写法照 `/api/v1/integration/` 的 `@integration_too_large`），请求不到应用 | 无 |
| 422 | 请求体不合规（含 64 KiB 到 80 KiB 之间由应用按原始字节判出的超限、未知字段、`feature` 格式）；`MODEL_NOT_ALLOWED`；供应商以 400 / 404 / 413 / 422 拒绝请求（例如调用方给的 JSON schema 不合法）→ `PROVIDER_REJECTED_REQUEST`，只带供应商错误的 `type`，不带 `message`（可能回显内容） | 无 |
| 502 | 供应商超时、网络错误、429、5xx、401 / 403（我们的 key 问题）、响应里没有可读 `usage` | 无（供应商可能已计费，未知用量对账在正式档） |
| 503 | 鉴权读库失败；Anthropic key 未配置；outbox 提交失败（不返回正文，偏离 spec §20，Kelvin 2026-10-12 同意） | outbox 失败时：最高级告警 + 暂存，库恢复后落成 `HELD` 行等人工处置（见「用量落库失败的处置」） |

### 功能标签 `feature` 的落点

- **AI 模块**：请求里的 `feature` 写进 outbox 载荷，原样随用量事件上报。
- **摄取契约**（单条 `POST /api/v1/integration/usage-events` 与批量 `…/batch` 的每一条）：新增**可选**字段 `feature`，规则同上；显式的 JSON `null` 等同于不带（不进指纹、存为空）；不合规 → 422 `VALIDATION_ERROR`（与其他字段同一套错误）。老调用方不带它，行为不变。
- **指纹**：`payload_fingerprint` 的规范对象**只在事件带 `feature` 时**加入 `"feature"` 键。不带的事件指纹与今天逐字相同，已入库事件的重放仍判重复；同一 `event_id` 一次带、一次不带（或值不同）→ 指纹不同 → `IDEMPOTENCY_CONFLICT`（INV-11 / `REQ-IDEMP-001` 的既有规则）。
- **存储**：`usage_events` 新增可空列 `feature` VARCHAR(64)（MySQL 用 `utf8mb4_0900_bin`，按字节比较）；新增索引 `(tenant_id, feature, occurred_at)`。
- **计价不看它**：定价规则解析、扣费、内部计量一概不读 `feature`（INV-6 不受影响）。
- **后台**：`GET /api/v1/admin/usage-events` 增加查询参数 `feature`（精确匹配；格式校验放在管理员鉴权之后，与现有时间范围参数同一顺序：未登录先得 401，登录后格式不合规得 422），列表与详情返回 `feature`；前端用量事件页加一列与一个筛选框。客户门户以后再说。

### 数据库（一次迁移：两张新表 + `usage_events` 加一列一索引）

- `ai_client_keys`：`id` BIGINT PK；`tenant_id`、`project_id`（复合外键 → `projects(id, tenant_id)`，`RESTRICT`，INV-8）；`client_id` VARCHAR(40) **唯一**（`aic_` + 32 位小写十六进制，非机密，MySQL 用 `utf8mb4_0900_bin`）；`token_sha256` CHAR(64)；`status`（`ACTIVE` / `REVOKED`，CHECK）；`created_at`、`revoked_at`（CHECK：`(status = 'REVOKED') = (revoked_at IS NOT NULL)`）。索引 `(project_id)`。行不删除。**不建 `last_used_at`**：AI 请求路径只写 outbox。
- `ai_usage_outbox`：`id` BIGINT PK；`event_id` CHAR(26) **唯一**、`request_id` CHAR(26)、`claim_id` CHAR(26) NULL（三者 MySQL 用 `ascii_bin`）；`tenant_id`、`project_id`（复合外键同上）；`feature` VARCHAR(64) NOT NULL（冗余一份，便于运维按功能查积压；AI 模块的调用一定带它）；`payload_json` TEXT NOT NULL；`status` VARCHAR(16) NOT NULL（`PENDING` / `SENDING` / `DELIVERED` / `DEAD` / `HELD` / `DISCARDED`，CHECK）；`attempts` INT ≥ 0；`next_attempt_at` **`DATETIME(6)`**（`acuven_ai_api` 2026-10-09 在 MySQL 8.0 实测：秒级 `DATETIME` 四舍五入会让「到期时间 = 现在」的行被舍到未来、当轮领不到）；`last_error` VARCHAR(64) NULL；`created_at` NOT NULL；`delivered_at` NULL；`resolved_at` NULL（`HELD` 被人工 `release` / `discard` 的时刻）；`review_required_at` NULL 与 `reviewed_at` NULL（「请求已回 503、用量却已进入投递」的复核标记，见「提交结果不确定」）；`dead_acked_at` NULL（`DEAD` 行被运维处理完的时刻，与 503 复核分开存，见「告警维度」）。除注明 NULL 的列外都是 NOT NULL。索引 `(status, next_attempt_at)`。行不删除（V1 不做清理）。
- `usage_events`：加可空列 `feature` 与索引（见上）。MySQL 8.4 对可空列走 INSTANT，索引走 INPLACE，不锁写。
- 审计动作新增 `AI_CLIENT_KEY_CREATE`、`AI_CLIENT_KEY_REVOKE`、`AI_USAGE_REQUEUE`、`AI_USAGE_DEAD_ACK`、`AI_USAGE_HELD_RELEASE`、`AI_USAGE_HELD_DISCARD`、`AI_USAGE_HELD_IMPORT`、`AI_USAGE_REVIEW_ACK`，**由写它们的任务各自加**（不在数据库那一层）；`entity_type` 用 `ai_client_key`（`entity_id` = `client_id`）、`ai_usage_outbox`（`entity_id` = `event_id`），在各自模块内定义为本地 `Final` 常量（`test_every_written_entity_type_is_classified` 只认本模块字面量），并在 `audit_query.py` 归类为公开 ID 类。`audit_logs.action` 是非原生枚举的 VARCHAR(64)、没有 CHECK（迁移 0003），新增成员不需要迁移。
- 下行迁移：只在两张新表都为空、且 `usage_events` 没有任何非空 `feature` 时执行，否则抛错拒绝（照 0019 / 0020 的写法）。

### 用量载荷（`payload_json`）

`schema_version="1.0"`、`event_id`（ULID，写 outbox 时生成一次，之后永不改）、`request_id`（与 200 响应里的相同）、`feature`、`tenant_id` / `project_id`（**公开 UUID**，诊断用；摄取端的权威归属仍取签名凭据，不一致 403）、`provider="anthropic"`、`model`（供应商响应里的实际模型名）、`usage_type="LLM_TOKEN"`、四类 token、`occurred_at`（模型返回后的 UTC 时刻，微秒）。不含 `system`、`messages`、模型输出（INV-9）。

### 请求路径（`app/ai_api/routes.py`、`access.py`）

**时限**：整个请求有总时限 `BILLING_AI_REQUEST_DEADLINE_SECONDS`（默认 75 秒）。AI 模块用自己的 SQLAlchemy engine（同一个库、同一个 URL，独立连接池），取连接等待 2 秒、建连接 2 秒、单次读写 5 秒（`BILLING_AI_DB_POOL_TIMEOUT_SECONDS` / `_CONNECT_TIMEOUT_SECONDS` / `_RW_TIMEOUT_SECONDS`）。于是一次库访问最坏 2 + 2 + 5 = 9 秒：鉴权读 ≤ 9 秒；写 outbox 加提交后的核查（核查是一条新连接：2 + 5）最坏 9 + 7 = 16 秒。模型调用的超时取 `min(60, 剩余时限 − 16)` 秒，所以最坏 9 + 50 + 16 = 75 秒，等于总时限、小于优雅停机 85 秒。

**这些都是墙钟上限，不是单次操作的超时**：httpx 与数据库驱动的超时只管「一次连接、一次读」，对方慢速逐段返回时，每一段都不超时、总时长却会越界。所以三个阶段各自包一层 `asyncio.timeout`（墙钟），超了就不再等：

- **鉴权读**（`asyncio.timeout(9)`）：超时 → 503，不调模型。线程里的只读查询可能还在跑，放着它跑完即可（只读，没有副作用）。
- **模型调用**（`asyncio.timeout(min(60, 剩余时限 − 16))`）：超时 → 502，与供应商超时同一处理；没拿到 `usage` 就没有用量事件。
- **写 outbox 与提交后的核查**（`asyncio.timeout(16)`）：超时 → 按「结果未知」处理（`commit_outcome=unknown` 的 `CRITICAL` + 暂存，503）。线程里的提交可能稍后才完成，落库时由「提交结果不确定」第 2 步对上那一行。

同步的库访问一律提交给 AI 模块**专用的、固定大小的** `concurrent.futures.ThreadPoolExecutor`（`max_workers = BILLING_AI_DB_THREADS`，默认 10），协程用 `asyncio.wrap_future` 等结果，外面包着该阶段的 `asyncio.timeout`。这样：

- 墙钟一到，协程立刻不再等（不像 Starlette 的 `run_in_threadpool` 要等线程跑完）；
- 还在排队、没开始的任务随取消一并撤掉；已经在跑的那个继续占着它的工作线程，直到驱动自己的读写超时或连接断开让它结束；
- **存活线程数的硬上限就是 `max_workers`**，不会再起新线程，也不会挤占计费接口共用的默认线程池。
- **排队也有上限**：提交前先过一道准入计数，上限 `max_workers + BILLING_AI_DB_QUEUE_MAX`（默认 10 + 20）。计数在提交时加一，**在底层任务真正结束时**（future 的完成回调）减一——不是在等它的协程超时放手时减，所以卡住的线程一直占着名额。满了就不提交、立刻失败：鉴权读阶段 503；写入阶段按「确认未写入」（任务根本没提交）记 `CRITICAL` 并暂存、503；暂存的后台循环满了就这一轮不落库、下一轮再试。库恢复后卡住的线程陆续结束、名额释放，服务自己恢复，不需要重启。进程里与 AI 库访问有关的内存因此有上界。（不用 anyio 的 `CapacityLimiter`：放手等待时它会把名额还回去，线程却还在跑，限不住存活线程数。）

超时那一刻，`CRITICAL` 日志与暂存都在协程里同步完成（只写日志和内存，不碰库），所以即使随后进程被强制停止，记录也已经留下。这样一次请求的墙钟时长最多 75 秒，停机时优雅期 85 秒内一定结束。独立连接池也让 AI 流量占不满计费接口的连接。

1. 按原始字节判请求体上限，再按上表校验结构。不合规 422，不调模型。
2. 一个只读事务内：按 `X-Acuven-Client-Id` 取 `ai_client_keys` 行（`ACTIVE`），`hmac.compare_digest(sha256(token), token_sha256)`；不匹配、缺头、非 `Bearer` → 401。再取项目与租户，`compose_effective_status(tenant.account_status, tenant.billing_status, tenant.billing_mode, project.integration_status)`：`BLOCK_AI` → 403。最后查模型目录：不允许 → 422 `MODEL_NOT_ALLOWED`。然后检查 Anthropic key 是否已配置：没有 → 503 `AI_PROVIDER_NOT_CONFIGURED`。顺序固定为「校验 → 鉴权 → 状态 → 模型 → key」，未鉴权的调用方探测不到模型目录与配置状态。key 文件在进程内首次用到时读一次并缓存（换 key 要重启 api）；`/healthz/ai` 每次都重读文件。
3. 这个事务失败（数据库不可用）→ 503，不调模型。**状态是进程内直接读权威列**，不经 HMAC 自调 `effective-status`、不存本地副本（Kelvin 2026-10-12 同意）。所以 spec §126 的「Webhook 接收端、本地版本化状态、周期对账」三项不适用于 AI 模块。
4. 调模型：`httpx2.AsyncClient`，超时 `min(BILLING_AI_PROVIDER_TIMEOUT_SECONDS, 剩余时限 − 16)`（见「时限」），`await` 期间不占线程池。请求体只由上表字段组成（AI 模块**不加任何系统提示**，提示词与「资料是不可信数据」这类防护归调用方）。
5. 新的写事务：INSERT 一行 `ai_usage_outbox`（`PENDING`、`next_attempt_at = now`）并提交。提交报错时**不直接当成没写进去**，按下面「提交结果不确定」先核查；确认没写进去（或核查不了）才 503、不返回正文，并按「用量落库失败的处置」处理。
5a. **取消**：模型已返回、outbox 提交还没确认时，如果请求任务被取消（停机时 uvicorn 的优雅期到点、强制取消在途任务），取消处理里同样记 `CRITICAL`（`commit_outcome=unknown`）并放进暂存队列（同「用量落库失败的处置」第 1、2 步），然后让取消继续传播。线程里的提交可能在取消之后才完成，所以这一条按「结果未知」处理，落库时由「提交结果不确定」第 2 步对上已存在的行。停机时暂存队列最后再试一次落库。
6. 200。
7. 请求路径**不碰 Redis**：不发 Celery 触发，投递完全靠 beat 扫描（§9）。

### 提交结果不确定

数据库可能已经写进去，但应用在收到确认之前连接断了，`commit()` 抛错。这时不能直接当成「没写」。

1. **当场核查**：用 AI 模块 engine 的一条新连接查 `SELECT status FROM ai_usage_outbox WHERE event_id = ?`（建连接 2 秒 + 读 5 秒，计入「时限」一段的 16 秒里）。
   - 查到了：其实已经写进去 → 照常 200 返回结果，记一条 WARNING。没有偏离，也不需要人工处置。
   - 查过了、没有：确实没写进去 → 503，按「用量落库失败的处置」。
   - 查不了（库仍不可用）：结果未知 → 503，按「用量落库失败的处置」，`CRITICAL` 日志里注明 `commit_outcome=unknown`。
2. **暂存落库时再核一次**（「用量落库失败的处置」第 3 步）：INSERT `HELD` 撞上 `event_id` 唯一约束，说明那一行已经存在。按那一行现在的样子处理，**只碰从未经人工决定过的行**（`resolved_at` 为空）：
   - `UPDATE … SET status='HELD' WHERE event_id=? AND status='PENDING' AND attempts=0 AND resolved_at IS NULL` 受影响 1 行 → **从未尝试投递过**，转成 `HELD`，照常等人工 `release` / `discard`。这条 UPDATE 与投递的领取 UPDATE 是同一行上的两个条件更新，数据库行锁保证只有一个生效。
   - 否则 `UPDATE … SET review_required_at=now WHERE event_id=? AND (status IN ('SENDING','DELIVERED','DEAD') OR (status='PENDING' AND attempts>0)) AND resolved_at IS NULL AND review_required_at IS NULL` 受影响 1 行 → 投递已经尝试过（领走中、已送达、进死信，或发送失败后退回 `PENDING`——摄取端可能已经接受、只是回执丢了），按下一条处理。这一行保持原状态，投递照常继续，同一 `event_id` 在摄取端至多一次财务效果，复核决定保留或调账退回。
   - 两条都是 0 行 → 什么也不改，只回报现状：行已是 `HELD`（同一条目暂存了两次、或已经落过一次）、已 `DISCARDED`、已被人工 `release` 过（`resolved_at` 非空，人已决定照收），或已经带着复核标记。
   - 写上 `review_required_at` 的那一行：记 `CRITICAL`（固定文案 `AI usage entered billing although the request returned 503`），告警维度把「`review_required_at` 非空且 `reviewed_at` 为空」计为严重。人工复核后决定保留这笔费用，或用现有的管理端手工调账退回（AIH-TASK-010，带审计），然后 `ai-usage-held ack <event_id> --reason …` 写 `reviewed_at` 与审计 `AI_USAGE_REVIEW_ACK`；只对「`review_required_at` 非空且 `reviewed_at` 为空」的行生效，其余拒绝。`DEAD` 行的处理完毕另用 `ai-usage-dead-ack`（见「死信重投」），两件事分两列存，互不影响。
3. 两条路最后都是确定的、被告警的、经人工处置并留审计的状态；不存在「回了 503 却悄悄计费」的路径。

### 用量落库失败的处置（spec §20：最高级告警 + 可审计的人工对账）

模型已经返回、token 已经花掉，但 outbox 那一行没提交上（数据库刚好不可用）。

1. **立即告警**：记一条 `CRITICAL` 日志，消息文本固定为 `AI usage not persisted`（告警按它匹配，改它等于把告警改没了），字段只有元数据，字段名固定为 `event_id`、`request_id`、`feature`、`tenant_public_id`、`project_public_id`、`model`、`usage_input`、`usage_output`、`usage_cache_write`、`usage_cache_read`、`occurred_at`、`commit_outcome`（`absent` / `unknown`）。四类用量**不用带 `token` 的字段名**：`app/core/logging.py` 会把名字里含 `token`、`prompt`、`completion` 等子串的字段整个脱敏，带了就丢数。现有 `JsonFormatter` 把这条记录输出成一行 JSON，`import` 就读这一行。这条日志随现有日志外送离开本机，是这次用量最早、也是兜底的记录。
2. **暂存**：同一份元数据（就是将要写进 outbox 的那份载荷）放进 api 进程内的暂存队列（上限 `BILLING_AI_HELD_BUFFER_MAX`，默认 1000 条；满了丢最旧的一条，丢之前再记一条 `CRITICAL`，原始那条日志仍在）。
3. **库恢复后落成事故记录**：api 进程启动时开一个后台循环（随应用生命周期启停），每 `BILLING_AI_HELD_RETRY_SECONDS`（默认 30）秒把暂存队列里的条目逐条 INSERT 进 `ai_usage_outbox`，状态 **`HELD`**（不会被投递扫描领取），`attempts=0`、`next_attempt_at` 与 `created_at` 取写入时刻（载荷里的 `occurred_at` 不变）。撞上 `event_id` 唯一约束时按「提交结果不确定」第 2 步处理。处理完就从队列移除。一轮里遇到连接类错误（取不到连接、连不上、超时）就停下这一轮，剩下的条目留到下一轮，不对每一条都等满超时。优雅停机时最后再试一次，失败的条目只剩第 1 步那条日志。
4. **告警持续**：`ai_usage_outbox` 告警维度里 `HELD` 行数 > 0 → 严重，直到人工处置完。
5. **人工处置**（CLI，写审计）：
   - `ai-usage-held release <event_id> --reason …`：`HELD → PENDING`，照常投递计费（token 确实花了；适合内部租户或与客户约定照收的情况）。审计 `AI_USAGE_HELD_RELEASE`。
   - `ai-usage-held discard <event_id> --reason …`：`HELD → DISCARDED`（终态，不计费；调用方没拿到结果，可以选择不收）。审计 `AI_USAGE_HELD_DISCARD`。
   - 两者都写 `resolved_at`；只对 `HELD` 行生效，其他状态拒绝。
   - 进程在暂存落库之前就重启了：这次用量只剩第 1 步的日志。运维按日志里的元数据判断要不要补；补的话用 `ai-usage-held import --from-log-line '<那一行 JSON>'`（只认消息文本为 `AI usage not persisted` 且字段齐全的行，否则拒绝）：日志里的 `event_id` 在库里**不存在**时，按日志字段写成一行 `HELD`，再走上面两条之一；**已存在**时与暂存落库撞行完全同一套规则（「提交结果不确定」第 2 步）：仍是从未经人工决定的 `PENDING` 就转 `HELD`，已进入投递就写复核标记并记那句 `CRITICAL`，其余（已 `HELD`、已 `DISCARDED`、已 `release` 过、已带复核标记）不改、只回报现状。每次都写审计 `AI_USAGE_HELD_IMPORT`（after 里记处理结果），打印处理结果。

### 投递（`app/ai_api/delivery.py`，Celery）

- beat 每 `BILLING_AI_DELIVERY_SWEEP_SECONDS`（默认 10）秒发一次 `ai_api.deliver_due`。注册方式照现有任务：`celery.shared_task(name="ai_api.deliver_due")`（任务名与 beat 条目名都是 `ai_api.deliver_due`），模块名 `app.ai_api.delivery` 加进 `app/core/celery_app.py` 的 `TASK_MODULES`；beat 条目照现有条目设 `expires`（取扫描周期，过期的触发直接丢弃）。一次运行有时间预算 `BILLING_AI_DELIVERY_RUN_BUDGET_SECONDS`（默认 45 秒，< 租约 60 秒）：超过就不再领新行，已领的照常结算；所以一次运行最多处理满 `BILLING_AI_DELIVERY_BATCH` 行或用完预算，两个以先到者为准，多次运行可以并发（领取本身互斥）。任务循环领取，直到没有到期行或本轮处理满 `BILLING_AI_DELIVERY_BATCH`（默认 20）行。
- **领取**：选 `status='PENDING' AND next_attempt_at <= now` 或 `status='SENDING' AND next_attempt_at <= now`（租约过期）的行，逐行条件 UPDATE：`status='SENDING'`、新 `claim_id`、`attempts+1`、`next_attempt_at = now + 租约 60 秒`，WHERE 带同样的资格条件；受影响行数 = 1 才算领到（照 `acuven_ai_api/app/worker.py:claim_one`）。
- **签名凭据**：在该项目 `integration_credentials` 里按 `is_verifiable` 判定为可验签的行中，取 `valid_from` 最晚的一行，并列时取 `id` 最大（一个项目可以有多个 `api_key`，`key_version` 只在同一个 `api_key` 内有序，不能跨 key 比较），用 `app.core.crypto` 与 `credential_aad` 在 worker 进程内解密；明文只在内存。没有可用凭据 → `SIGNING_CREDENTIAL_MISSING`，**可重试**（管理员补签后自动恢复），进告警。解密失败（`DecryptionFailed`）或主密钥未配置（`EncryptionNotConfigured`）→ `SIGNING_CREDENTIAL_UNREADABLE`，可重试，进告警。
- **发送**：`urllib` 同步 POST（不跟随重定向），目标 `BILLING_AI_INGEST_BASE_URL`（默认 `http://api:8000`，compose 内网直连 api）+ `/api/v1/integration/usage-events`，超时 10 秒（< 租约 60 秒）。每次尝试新的 `X-Acuven-Request-Id`、当前时间戳，**body 与 `event_id` 永不变**。
- **判定**（原样搬 `client_rules.py`）：202 `accepted` 或 200 `already_received` / `already_processed` 且回执 `event_id` 匹配 → `DELIVERED`；网络错误、回执不匹配、错误信封 `retryable=true` → `PENDING`，`next_attempt_at = now + 退避`；**退避 = `min(3600, 2^min(attempts,12))` 秒乘以 [0.5, 1.0] 内均匀分布的随机系数**（spec §22「exponential backoff + jitter」：同一次故障里一起失败的事件错开重试，摄取端恢复时不会集中涌入；随机源可注入，测试用固定值）；**响应不是错误信封**（不是 JSON，或没有布尔 `retryable`）时先看状态码，再交给 `error_outcome`：5xx → `PENDING`（错误码 `BILLING_UNAVAILABLE`，摄取端挂了不能把用量判死），其他 → `DEAD`（`INVALID_BILLING_RESPONSE`）；其余错误 → `DEAD`，`last_error` 存错误码。
- **结算**：条件 UPDATE，WHERE `id` 且 `status='SENDING'` 且 `claim_id = 本次领取的 claim_id`。租约已被别人收回时受影响行数为 0，什么也不改。
- 进 `DEAD` 记 ERROR 日志（`event_id`、错误码）。

### 管理（`app/ai_api/admin.py` + `app/api/admin_ai_access.py`，ADMIN，沿用现有后台会话与 2FA）

| 接口 | 作用 | 事务与审计 |
| --- | --- | --- |
| `GET /api/v1/admin/customers/{customer_id}/projects/{project_id}/ai-access` | 该项目全部 AI 密钥（含已吊销），按 `created_at` 倒序，不分页（每个项目至多 2 把有效、总数很少）；每条只有 `client_id`、`status`、`created_at`、`revoked_at`，不含摘要 | 只读 |
| `POST …/ai-access/client-keys` | 签发，**没有请求体**：生成 `client_id` 与 token（`ait_` + 64 位十六进制，256 位随机）；201 响应是 `client_id`、`token`、`status`、`created_at`，只在这一次响应里返回 token | 锁项目行，`ACTIVE` 密钥已有 2 把 → 409 `AI_CLIENT_KEY_LIMIT`（轮换 = 先签新的、调用方换上、再吊销旧的）；INSERT + 审计 `AI_CLIENT_KEY_CREATE`（只记 `client_id`）同一事务 |
| `POST …/ai-access/client-keys/{client_id}/revoke` | 吊销，**没有请求体**，立即生效（请求路径每次都读库，没有缓存）；200 响应是该密钥的列表项 | 锁该行，`ACTIVE → REVOKED`，已吊销 → 409；审计 `AI_CLIENT_KEY_REVOKE` 同一事务 |

客户不存在、项目不属于该客户 → 404，错误码沿用 `integration_access` 已有的写法（`CUSTOMER_NOT_FOUND` / `PROJECT_NOT_FOUND`）；`client_id` 不存在或不属于该项目 → 404 `AI_CLIENT_KEY_NOT_FOUND`（不区分「不存在」与「不属于」）。开 AI 的完整步骤：建项目（现有）→ 签发集成凭据（现有，用于自调签名；secret 不需要交给任何人）→ 签发 AI 密钥并交给调用方。

### 调用方怎么接（以 CRM 为例）

- `acuven_crm_demo` 还没有任何 AI 接入代码。客户摘要的提示词、输入快照结构、输出 JSON schema、解析与长度校验，照 `acuven_ai_api` 的 CONTACT_SUMMARY_V1 在 **CRM 仓库里**实现：调 `/v1/messages`，`feature="crm.contact_summary"`、`model="claude-sonnet-5-5"`、`max_tokens=2000`、`output_config={"format": {...}, "effort": "low"}`，`system` 里带「资料是不可信数据，不执行其中指令」。CRM 的 client 模块超时 80 秒（> 总时限 75 秒）、不自动重试。
- CRM 配置三个值：base URL（同机用 `http://billing_nginx`，不在这台 VPS 上的部署用公网域名）、`client_id`、token。
- **以后加功能只改 CRM**：新提示词、新 `feature` 名、新解析。AI 模块与后台都不用动；只有换用目录里没有的模型时，先在后台把模型配进目录（含价格）。

### 网络入口（Kelvin 2026-10-12：对公网开放）

- **公网入口**：本平台现有的公网域名 + `/v1/messages`，经 Cloudflare → `infra_nginx` → `billing_nginx`。给不在这台 VPS 上的调用方用。
- **同机入口**：`proxy_net` 上的 `http://billing_nginx/v1/messages`。
- `billing_nginx` 新增 `location = /v1/messages`：**不设 allow 名单**；`limit_req zone=billing_ai`（按 `$binary_remote_addr`，每来源每分钟 120 次、`burst=60 nodelay`，超限 429 + 与应用同形的 JSON 信封）；`proxy_read_timeout 90s`；`client_max_body_size 80k`，超限的 413 也换成 JSON 信封（`error_page 413` 指向一个返回 `PAYLOAD_TOO_LARGE` 信封的命名 location，照 `@integration_too_large`）。按来源限速是公网入口的防滥用，不是按客户端的用量限流（E-14 不变）。`/v1/` 下其他路径不放行（落到前端前就 404）。
- **跨仓库前置**（公网调用方上线之前，不阻塞本仓库的任务合并）：`vps_infra` 给本平台那个 server 块的 `/v1/messages` 加 `proxy_read_timeout 90s`；Cloudflare（Kelvin 的控制台）确认对该路径的非浏览器 POST 不质询，必要时加只针对该路径的跳过规则。

### 健康检查与优雅停机（E-20）

- `GET /healthz` 不变（纯存活，容器健康检查仍只探它）。
- 新增 `GET /healthz/ai`：数据库可连且 Anthropic key 文件可读非空 → 200；否则 503 信封（只说哪一项不行，不回 key）。**不调 Anthropic、不查 Redis**。
- 新增 `GET /healthz/billing`：与 `/readyz` 同一组检查（数据库不通 503；Redis 不通 200 `degraded`）。`/readyz` 保留不变。
- 两个健康检查路径在 nginx 上与 `/readyz` 同一份 allow 名单。
- `deploy/deploy.sh` 冒烟在现有检查之后加：`GET /healthz/ai` 必须 200，**且**响应正文是 JSON、信封里 `data.status` 为 `ok`，否则按现有逻辑回滚。只看 200 不够：边缘 nginx 没有这条 location 时，请求会落到前端 SPA，也回 200。
- （随 §8 第 6 项上线）`Dockerfile` 的 `uvicorn` 加 `--timeout-graceful-shutdown 85`（≥ 请求总时限 75 秒 + 10 秒余量）；compose `api` 加 `stop_grace_period: 100s`（再给 uvicorn 15 秒完成强制取消与退出）。正常情况下所有在途调用都在总时限内结束；万一优雅期到点，被取消的调用走「取消」那一步留下告警与暂存，不会无声丢失。代价：单容器「先停后起」，排空期间新请求拿 502，最坏约 85 秒（Kelvin 2026-10-12 接受约 75 秒；为给总时限留余量调到 85 秒）。

### 配置（`BILLING_` 前缀，带默认值；compose 显式转发）

| 变量 | 默认 | 谁用 |
| --- | --- | --- |
| `BILLING_AI_ANTHROPIC_KEY_FILE` | `/run/secrets/billing_anthropic_key`（compose secret，宿主机 `./secrets/anthropic.key`） | api |
| `BILLING_AI_PROVIDER_TIMEOUT_SECONDS` | `60`（实际取 `min(60, 剩余时限 − 16)`） | api |
| `BILLING_AI_REQUEST_DEADLINE_SECONDS` | `75` | api |
| `BILLING_AI_MAX_REQUEST_BYTES` | `65536` | api |
| `BILLING_AI_MAX_OUTPUT_TOKENS` | `4000` | api |
| `BILLING_AI_INGEST_BASE_URL` | `http://api:8000` | celery-worker |
| `BILLING_AI_DELIVERY_SWEEP_SECONDS` / `_BATCH` / `_LEASE_SECONDS` / `_RUN_BUDGET_SECONDS` | `10` / `20` / `60` / `45` | celery-beat / worker |
| `BILLING_AI_DB_POOL_TIMEOUT_SECONDS` / `_CONNECT_TIMEOUT_SECONDS` / `_RW_TIMEOUT_SECONDS` | `2` / `2` / `5` | api |
| `BILLING_AI_DB_THREADS` / `BILLING_AI_DB_QUEUE_MAX` | `10` / `20` | api |
| `BILLING_AI_PENDING_ALERT_SECONDS` | `900` | 告警 |
| `BILLING_AI_HELD_BUFFER_MAX` / `BILLING_AI_HELD_RETRY_SECONDS` | `1000` / `30` | api |

没有「默认模型」配置：模型由调用方在请求里指定，受模型目录约束。key 走文件、不走环境变量（ADR-0004 §2）。

### 告警维度（`GET /internal/alerts`，接在现有维度之后）

`ai_usage_outbox`（严重 = `P1`、警告 = `P2`，与 `status_webhook` 维度同一用法）：`DEAD` 且 `dead_acked_at` 为空的行数 > 0 → 严重（行不删除、部分错误码不能重投，只按「`DEAD` 总数」计会让告警永远停在严重；运维处理完——重投，或判定不重投并按 runbook 报给 Kelvin——之后用 `ai-usage-dead-ack` 写 `dead_acked_at`，告警才消）；`HELD` 行数 > 0 → 严重；`review_required_at` 非空且 `reviewed_at` 为空的行数 > 0 → 严重；最老的 `PENDING` / `SENDING` 行的 `created_at` 早于 `BILLING_AI_PENDING_ALERT_SECONDS` → 警告。摘要只有计数与秒数。阈值经 `AlertContext` 的新字段传入（带默认值），`app/api/internal_alerts.py` 构造它时从配置读；`docs/api.md` 的告警维度表加一行。

### 死信重投（`python -m app.cli ai-usage-requeue <event_id>… [--include-delivered] --reason <文字>`）

- 默认只把 `DEAD` 且 `last_error ∈ {INTEGRATION_AUTH_FAILED, INVALID_BILLING_RESPONSE}` 的行改回 `PENDING`：`next_attempt_at = now`，清 `claim_id`、`last_error`、`dead_acked_at`（再死一次要重新告警），`attempts` 归零（退避从头算）；沿用原 `event_id` 与原 body；`--include-delivered` 改回的行另清 `delivered_at`；`reviewed_at` 与 `review_required_at` 不动（那是 503 复核，另一件事）；其余错误码（`CREDENTIAL_SCOPE_MISMATCH`、`IDEMPOTENCY_CONFLICT`、422 类、`PAYLOAD_TOO_LARGE`、`INVALID_LOCAL_EVENT` 与未知码）拒绝。
- `--include-delivered`：把 `DELIVERED` 行也改回 `PENDING`，**只用于验收**「同一 `event_id` 重投只生效一次」。
- 每改一行写一条审计 `AI_USAGE_REQUEUE`（`actor_user_id` 为空、`reason` 为操作员给的文字）。打印「改了几行 / 共几行」。
- `python -m app.cli ai-usage-dead-ack <event_id>… --reason <文字>`：给 `DEAD` 且 `dead_acked_at` 为空的行写 `dead_acked_at`，其余拒绝；每行一条审计 `AI_USAGE_DEAD_ACK`。

### 时间

`occurred_at` = 模型返回之后的 UTC 时刻；签名时间戳取发送那一刻。自调时签名方与验签方同机同钟，±300 秒时间窗与跨主机网络在自调里**验不到**（E-19），验收记录写明。

### 金额

AI 模块不算任何金额，不写任何金额列，响应里只有 token 数。`feature` 不参与计价。

## 3. 不变量影响矩阵

| 不变量 | 是否触碰 | 可能怎样被破坏 | 设计控制 | 验证方式 |
| --- | --- | --- | --- | --- |
| INV-1 中心故障不中断 AI | **是** | 计费逻辑（摄取、定价、钱包、计费 worker、Redis）出错拖垮 AI 请求；AI 请求等计费 | AI 请求路径不调计费代码（import 白名单）、不碰 Redis、不等投递。**新增的耦合**：同一个 MySQL 与同一个进程——数据库或进程不可用时 AI 也不可用（spec §141.5、ADR-0012） | 测试：摄取端点一律 503 / 抛异常、计费 worker 抛异常、broker 不可达三种情况下 AI 调用 200 且 outbox 留 `PENDING`；import 白名单 AST 测试 |
| INV-2 事件不重复扣费 | 是 | worker 租约过期后重复 POST 同一事件 | `event_id` 写 outbox 时生成一次，重试沿用原 body；摄取端按 `event_id` 去重 | 测试：同一行投递两次，摄取端只有一条 `usage_events`、`PREPAID` 钱包只扣一次 |
| INV-3 支付不重复入账 | 否 | AI 模块不接触支付 | import 白名单禁止 | AST 测试 |
| INV-4 余额只经账本变动 | 否 | AI 模块不写钱包 | 同上 | AST 测试 |
| INV-5 历史账本不可变 | 否 | 同上 | 同上 | AST 测试 |
| INV-6 事件保留版本引用 | 否 | `feature` 被拿去参与定价，导致同一事件重算结果不同 | 定价规则解析与扣费不读 `feature`；载荷不带价格 | 现有计价测试 + 一条：同一事件带与不带 `feature`（不同 `event_id`）计价结果相同 |
| INV-7 客户不可见成本毛利 | 是（防） | `/v1` 响应带出成本 | 响应只有原文与 token 数；AI 模块不读成本列 | 测试：200 / 4xx / 5xx 响应体字段集合断言 |
| INV-8 租户不可互访 | **是** | 调用方用 A 项目的密钥给 B 项目记账 | 归属只来自服务端：`client_id` → 密钥行 → 项目 → 租户；请求里没有租户 / 项目字段；新表复合外键；签名凭据按 outbox 行的项目取；摄取端再按凭据核对诊断 ID | 测试：A 的密钥调用只产生 A 项目的行；跨客户管理接口 404；外键拒绝错配插入；伪造诊断 ID 得到 `CREDENTIAL_SCOPE_MISMATCH` 进 `DEAD` |
| INV-9 对话内容不入库 | **是** | `system` / `messages` / 模型输出进表、日志、异常、审计；供应商错误消息回显内容 | 内容只在内存；outbox 只有元数据；日志只记 ID、`feature`、模型、token、错误类别；422 信封只回字段名；供应商错误只透出 `type` | 测试：`system`、`messages` 与假模型输出各带一个哨兵字符串，跑完整调用 + 投递 + 422 + 502 + 供应商 400，新表、审计、`caplog`、响应信封里都找不到哨兵 |
| INV-10 金额用 Decimal | 否 | AI 模块没有金额 | 只有整数 token | — |
| INV-11 event_id 至多一次财务效果 | **是** | 加 `feature` 改变指纹，让历史事件的重放被判冲突；或让「同 ID 不同功能」被当成重复 | 指纹只在带 `feature` 时加键：不带的事件指纹逐字不变；带了且值不同 → 冲突 | 测试：今天已有的指纹向量不变；同 `event_id` 不同 `feature` → `IDEMPOTENCY_CONFLICT`、无财务效果；`--include-delivered` 重投 → 200 `already_*` |
| INV-12 定稿对账单不可变 | 否 | AI 模块不碰对账单；`feature` 是新列，不改已定稿数据 | import 白名单；迁移只加可空列 | — |
| INV-13 状态与事件原子提交 | 是（管理侧） | 签发 / 吊销与审计分开提交 | 同一事务 | 测试：审计写入失败时数据改动回滚 |
| INV-14 队列丢失不毁持久工作 | **是** | 用 Celery 消息承载「要投递的事件」 | outbox 行是事实来源；请求路径不发消息；beat 每次从库里扫；租约过期自动收回 | 测试：领取后不结算（模拟崩溃），租约过期后重领送达；broker 不可用期间 AI 照常、恢复后补投 |

## 4. 状态与并发

**`ai_usage_outbox.status`**

| 当前状态 | 事件 | 前置条件 | 新状态 | 副作用 | 非法时结果 |
| --- | --- | --- | --- | --- | --- |
| — | 模型返回且有 `usage` | 鉴权、`ALLOW_AI`、模型允许 | `PENDING` | `attempts=0`、`next_attempt_at=now` | 提交失败 → 503，进暂存队列 |
| — | 暂存条目在库恢复后写入 | `event_id` 尚不存在 | `HELD` | 从暂存队列移除 | `event_id` 已存在 → 见下两行 |
| `PENDING` | 暂存条目或 `import` 撞上已存在的行 | `attempts = 0` 且 `resolved_at` 为空 | `HELD` | 条件 UPDATE；从暂存队列移除 | 不满足 → 下一行 |
| `SENDING` / `DELIVERED` / `DEAD`，或 `attempts > 0` 的 `PENDING` | 暂存条目或 `import` 撞上已存在的行 | `resolved_at` 与 `review_required_at` 都为空 | 不变 | 写 `review_required_at`、`CRITICAL`；从暂存队列移除 | 不满足 → 下一行 |
| `HELD` / `DISCARDED` / 已 `release` 的行 / 已带复核标记的行 | 暂存条目或 `import` 撞上已存在的行 | — | 不变 | 不改，只回报现状；从暂存队列移除 | — |
| 任意（`review_required_at` 非空） | CLI `ai-usage-held ack` | `reviewed_at` 为空 | 不变 | 写 `reviewed_at`、审计 | 已复核 → 拒绝 |
| `HELD` | CLI `release` | — | `PENDING` | `resolved_at`、`next_attempt_at=now`、审计 | 非 `HELD` → 拒绝 |
| `HELD` | CLI `discard` | — | `DISCARDED`（终态） | `resolved_at`、审计 | 非 `HELD` → 拒绝 |
| `PENDING` | 领取 | `next_attempt_at <= now` | `SENDING` | 新 `claim_id`、`attempts+1`、租约 60 s | 条件不满足 → 受影响 0 行，跳过 |
| `SENDING` | 领取（租约过期） | `next_attempt_at <= now` | `SENDING` | 换新 `claim_id`、`attempts+1` | 同上 |
| `SENDING` | 结算：合法回执 | `claim_id` 匹配 | `DELIVERED` | `delivered_at`、清 `claim_id` | `claim_id` 不匹配 → 0 行，不改 |
| `SENDING` | 结算：可重试 | `claim_id` 匹配 | `PENDING` | 退避、`last_error` | 同上 |
| `SENDING` | 结算：不可重试 | `claim_id` 匹配 | `DEAD` | `last_error`、ERROR 日志 | 同上 |
| `DEAD` | CLI 重投 | 错误码在白名单 | `PENDING` | `next_attempt_at=now`、`attempts=0`，清 `claim_id`、`last_error`、`dead_acked_at`，审计 | 其他错误码 → 拒绝，不改 |
| `DEAD` | CLI `ai-usage-dead-ack` | `dead_acked_at` 为空 | 不变 | 写 `dead_acked_at`、审计 | 已处理 → 拒绝 |
| `DELIVERED` | CLI 重投 `--include-delivered` | — | `PENDING` | 同上 | — |

`DELIVERED` 只表示摄取端持久接收（`REQ-INGEST-001`），不表示计价完成。

领取条件只认 `PENDING` 与租约过期的 `SENDING`，`HELD` 与 `DISCARDED` 永远不会被投递扫描碰到。

**`ai_client_keys.status`**：`ACTIVE → REVOKED`（终态）。

**并发**

- 多个 celery-worker 进程同时领取：靠条件 UPDATE 的受影响行数串行化。
- 租约过期后的重复发送：可能出现两次 HTTP；摄取端 `event_id` 唯一约束保证至多一次财务效果；旧 claim 的结算被 `claim_id` 围栏挡住。
- 签发：在项目行上 `SELECT … FOR UPDATE`，「至多 2 把 `ACTIVE`」由服务函数在锁内计数保证（MySQL 没有部分唯一索引，生成列方案对「2 把」不适用）。
- 请求路径只读、不加锁。吊销与一次在途调用并发时，在途那次以它读到的状态完成，下一次请求即被拒。
- 幂等键：`event_id`。同 ID 不同载荷（含 `feature` 不同）由摄取端判 `IDEMPOTENCY_CONFLICT`，进 `DEAD`，不可 CLI 重投；本模块写入后不改 body，正常不会产生。
- 重试在投递层，请求路径不重试模型调用。

## 5. 失败与恢复矩阵

| 失败点 | 对外结果 | 数据最终状态 | 是否可重试 | 恢复来源 | 告警 |
| --- | --- | --- | --- | --- | --- |
| 请求体校验失败 / 超限 / `MODEL_NOT_ALLOWED` | 422 | 无 | 调用方改请求 | — | 无 |
| AI 密钥不对 / `BLOCK_AI` | 401 / 403 | 无 | 管理员处理后可 | — | 无 |
| 同一来源超过 nginx 限速 | 429（JSON 信封，请求没到应用） | 无 | 稍后 | — | nginx 日志 |
| 鉴权读库失败 | 503，不调模型 | 无 | 是 | — | `/readyz`、`/healthz/ai` |
| 供应商拒绝请求（400 类） | 422 `PROVIDER_REJECTED_REQUEST` | 无 | 调用方改请求 | — | 日志 |
| 供应商失败 / 超时 / 无 `usage` | 502 | 无事件（供应商可能已计费） | 调用方决定 | — | 日志 |
| 模型成功、outbox 提交失败（核查确认没写进去或核查不了） | 503，不回正文 | 暂存队列 → 库恢复后 `HELD` 行 | 人工决定 `release` / `discard` | 暂存后台循环；进程已重启时按 `CRITICAL` 日志 `import` | `CRITICAL` 日志（固定文案）+ `HELD > 0` 严重 |
| 提交报错但其实写进去了，当场核查查到 | 200 照常 | `PENDING` 行 | — | 正常投递 | WARNING 日志 |
| 提交报错、核查不了，事后发现其实写进去了 | 503 | 仍 `PENDING` → 转 `HELD`；已被投递领走 → 标 `review_required_at` | 人工 `release` / `discard`，或复核后保留 / 调账退回再 `ack` | 暂存后台循环 | `CRITICAL` + 严重告警直到处置 |
| 模型拒答 / 截断 / 输出不合调用方格式 | 200（`stop_reason` 如实返回） | `PENDING` 行已提交 | 调用方决定 | 正常投递 | 无 |
| Redis / broker 不可用 | AI 照常 200 | 行留 `PENDING` | 是 | broker 恢复后扫描补投 | `/readyz` degraded |
| celery-beat / worker 停摆 | AI 照常 200 | 积压 `PENDING` | 是 | 恢复后扫描 | 积压告警 |
| worker 领取后崩溃 | — | `SENDING`，租约 60 s 后可重领 | 是 | 租约过期重领 | 无 |
| 摄取端不可达 / 超时 / 5xx `retryable` | — | `PENDING` + 退避（封顶 1 小时） | 是 | 下一轮扫描 | 超过 15 分钟进积压告警 |
| 摄取端非 `retryable` 错误（含 `feature` 校验失败） | — | `DEAD` | 白名单码可 CLI 重投 | 人工；处理完 `ack` | 未复核的 `DEAD > 0` 严重 |
| 摄取端回非信封的 5xx | — | `PENDING` + 退避（`BILLING_UNAVAILABLE`） | 是 | 下一轮扫描 | 超过 15 分钟进积压告警 |
| 项目无可用签名凭据 / 解不开 | — | `PENDING` + 退避 | 是 | 补签 / 修主密钥后自动 | 积压告警 |
| 同一事件重复送达 | — | `DELIVERED` | — | 摄取端 200 `already_*` | 无 |
| 外部接入方带了不合规的 `feature` | 摄取端 422 `VALIDATION_ERROR`（`retryable=false`） | 无事件 | 接入方改载荷 | — | 接入方自己的死信 |
| 部署：api 收到 SIGTERM | 在途请求在总时限内（≤ 75 s）正常结束；新请求 502 | 在途调用正常写 outbox | 调用方重试 | 新容器 | 部署日志 |
| 部署：优雅期 85 s 到点、uvicorn 强制取消在途任务 | 在途请求断开 | 模型已返回、未写 outbox 的那次：`CRITICAL` + 暂存，停机前最后一次落库成 `HELD`；落不了就只剩那条 `CRITICAL` 日志，按日志 `import` | 人工 | 暂存 / 日志 | `CRITICAL` |
| 部署：100 s 后 Docker SIGKILL | 进程直接死 | 只会发生在 uvicorn 强制取消后 15 秒仍未退出；取消那一步的 `CRITICAL` 已经先记下 | 人工 | 日志 | `CRITICAL` |
| `/healthz/ai` 不通过 | 部署回滚到上一个镜像 | 迁移已执行（新表、新可空列与旧镜像兼容） | 修好重部署 | — | 部署失败 |

## 6. 数据与安全边界

- **鉴权主体**：`/v1/messages` = 项目级 AI 密钥。管理接口 = 现有 ADMIN 会话（含 2FA）。投递 = 项目集成凭据的 HMAC。三者互不通用。
- **租户过滤**：请求路径从密钥行出发连接项目与租户，请求体里没有任何归属字段；管理接口按 `customer_id` + `project_id` 双重定位；新表复合外键（INV-8）。后台按 `feature` 筛选仍在现有管理端权限内，`feature` 不是新的访问维度。
- **AI 密钥**：token 256 位随机，库里只存 SHA-256；常量时间比较；只在签发响应里出现一次；不进日志、审计、异常。
- **密钥泄漏的后果比 v2 大**（通用接口的代价，Kelvin 2026-10-13 接受）：拿到一把 AI 密钥就能用目录里的任何模型做任何事，不再局限于某个功能；费用记在那个项目上（`PREPAID` 扣它的钱包，内部租户不扣钱、也没有上限，E-14）。控制：吊销即时生效；单次请求有体积与 `max_tokens` 上限；公网入口按来源限速；模型只限目录内。
- **防提示注入归调用方**：AI 模块不加系统提示、不判断内容。调用方必须在 `system` 里声明业务数据不可信、不执行其中指令（CRM 照 CONTACT_SUMMARY_V1 做）。AI 模块本来就只返回文本，不执行任何动作，模型输出不会变成本平台里的写操作。
- **集成凭据（签名用）**：沿用 ADR-0004 的加密存储；celery-worker 进程内解密，明文只在内存。
- **Anthropic key**：compose secret 文件，只挂给 api；宿主机文件须在合并引入它的任务**之前**建好（缺文件时 compose 起不来，2026-10-04 #215 的教训）。
- **日志**：允许 `request_id`、`event_id`、`feature`、项目公开 ID、模型名、token 数、错误类别 / 错误码；禁止 `system`、`messages`、模型输出、供应商错误 `message`、token / key / secret。
- **`feature` 的数据性质**：调用方起的功能名，格式限死为小写标识符加点，放不进姓名、电话等个人数据；存进 `usage_events` 无隐私风险。
- **网络暴露**：`/v1/messages` 对公网开放；`/api/…` 与健康检查的现有访问控制不变。
- **数据保留**：`ai_usage_outbox`、`ai_client_keys` 行 V1 不删；`usage_events.feature` 随事件按现有保留规则。

## 7. 测试证据计划

| 风险/需求 | 测试层级 | 场景 | 预期结果 |
| --- | --- | --- | --- |
| 模块边界 | unit（AST） | 扫描 `app/ai_api/**` 与全仓库的 import | 请求路径只 import 白名单；原有模块只在列出的几处 import `app.ai_api` |
| 正常路径 | integration（TestClient + 假供应商 transport + MySQL） | 签发密钥，合规请求（含 `output_config`） | 200，响应字段如 §2；发给供应商的请求体只含子集字段、没有额外系统提示；一行 `PENDING`，载荷含 `feature` |
| 请求校验 | unit / integration | 缺 `feature`、`feature` 大写或无点、未知字段（`tools`、`stream`、`temperature`）、`messages` 为空或首条非 `user`、`max_tokens` 4001、请求体 65537 字节 | 全部 422，供应商未被调用 |
| 模型护栏 | integration | 目录里 `ACTIVE` 模型、`RETIRED` 模型、不存在的模型、生效中的别名、已截止的别名 | 200 / 422 / 422 / 200 / 422 |
| 供应商错误映射 | integration | 供应商回 400、429、500、超时、无 `usage` | 422 `PROVIDER_REJECTED_REQUEST`（无 `message`）/ 502 / 502 / 502 / 502，均无 outbox 行 |
| 拒答与截断 | integration | 供应商回 `stop_reason=refusal` / `max_tokens` | 200，`stop_reason` 原样，用量已入 outbox |
| 计费全挂 AI 仍成功（E-20） | integration | 摄取端点一律 503 / 抛异常；`usage_billing` 抛异常；broker 不可达 | AI 调用 200；投递一轮后行仍 `PENDING`、`attempts=1`；再调仍 200 |
| 状态拦截 | integration | 账户 `DISABLED`、项目 `DISABLED`、`PREPAID` 余额 ≤ 0、内部计量余额 0 | 403 / 403 / 403 / 200；403 时供应商未被调用、无行 |
| 鉴权 | integration | 缺头、错 token、已吊销、第三把签发 | 401 / 401 / 401 / 409 |
| 并发签发 | integration（MySQL，两个连接） | 项目已有 1 把 `ACTIVE`，两个线程同时签发（用屏障让两边都过了参数校验后再进事务） | 恰好一个成功、一个 409；最终 `ACTIVE` 恰为 2；审计只有一条 `AI_CLIENT_KEY_CREATE` |
| 摄取 `feature` | integration（单条与批量） | 带合法 `feature`、不带、带非法值 | 入库带值 / 入库为空 / 422；批量里一条非法只影响那一条 |
| 指纹兼容 | unit | 不带 `feature` 的事件 | 指纹与改动前的固定向量逐字相同 |
| 指纹冲突 | integration | 同 `event_id`：先不带后带 `feature`；先后两个不同值 | `IDEMPOTENCY_CONFLICT`，无第二次财务效果 |
| 后台筛选 | integration | `GET /admin/usage-events?feature=crm.contact_summary` | 只返回该功能；列表与详情含 `feature` |
| 并发领取 | integration（MySQL） | 两个线程同时领取同一批到期行 | 每行恰被领到一次 |
| 幂等重放 | integration | 同一行租约过期被领两次并都发送；`--include-delivered` | 一条 `usage_events`；`PREPAID` 一行账本；200 `already_*` |
| 旧 claim 不覆盖 | integration | A 领取后过期、B 重领结算 `DELIVERED`、A 再结算 | 最终 `DELIVERED` |
| 事务中途失败 | integration | outbox 提交抛错；签发时审计写入抛错 | 503 且无 `PENDING` 行；签发整体回滚 |
| 提交结果不确定 | integration（MySQL） | 让 `commit()` 真正提交后再抛错：① 当场核查查到；② 核查也失败，之后暂存落库时行仍 `PENDING`；③ 同②但投递已先把它送达 | ① 200；② 503 → 行转 `HELD`，投递扫描不再碰它；③ 503 → 行保持 `DELIVERED`、写 `review_required_at`、告警严重，`ack` 后写 `reviewed_at` 与审计、告警消失；②与投递领取并发时只有一个条件更新生效 |
| 时限与取消 | integration | 鉴权读库挂住；供应商挂住；供应商慢速逐段返回（每一段都不触发 httpx 的读超时、累计超过预算）；写 outbox 挂住；在模型返回后、提交前取消请求任务 | 读库 9 秒内 503；慢速逐段的供应商在预算到点时 502、无用量事件；写 outbox 16 秒内按结果未知 503 并进暂存；同步库调用永不返回（线程阻塞在一个事件上）时，鉴权读与写入两个阶段都在各自墙钟上限内返回，`CRITICAL` 与暂存在超时那一刻已经存在；库调用持续卡住时连续发远多于 `BILLING_AI_DB_THREADS + BILLING_AI_DB_QUEUE_MAX` 的请求：存活的 AI 工作线程数始终不超过 `BILLING_AI_DB_THREADS`，已提交未结束的任务数始终不超过两者之和，超出的请求立刻 503（不排队），计费接口照常应答；放开事件后线程全部结束、准入计数归零，新请求恢复正常；每种情况整个请求都在 75 秒墙钟内结束（测试用缩小的时限与假时钟）；取消时留下固定文案的 `CRITICAL`（只有元数据）并进暂存队列，停机前落成 `HELD` |
| 用量落库失败的处置 | integration | outbox 提交抛错 → 恢复数据库 → 跑一次暂存循环；再分别 `release`、`discard`；同一 `event_id` 暂存两次；暂存队列满；`import` 一条日志字段（`event_id` 不存在；已存在且仍 `PENDING`；已存在且已送达；已是 `HELD`） | 第一次失败立即有 `CRITICAL`（文案固定、只有元数据）；恢复后一行 `HELD`、告警严重；`release` 后变 `PENDING` 并送达、`discard` 后 `DISCARDED` 且永不送达，各一条审计；重复的只写一次；满时丢最旧并再记 `CRITICAL`；`import` 四种情况依次得到新 `HELD` 行、转 `HELD`、写 `review_required_at` 与 `CRITICAL` 且告警严重、只回报现状，每次一条审计 |
| 重启后补录（组合） | integration（MySQL） | 提交其实成功 → 当场核查失败 → 返回 503 → 暂存随进程丢失（清空队列模拟重启）→ 投递把原行送达 → 从那条 `CRITICAL` 日志 `import` | 原行写上 `review_required_at`，告警严重；`ack` 后写 `reviewed_at` 与审计，告警消失；全程只有一次财务效果 |
| 退避抖动 | unit | 注入随机系数 0.5 与 1.0；`attempts` 为 1、12、20 | 退避落在 [0.5 × 基数, 基数] 内，基数封顶 3600 秒 |
| Redis/Celery 丢失 | integration | 领取后不结算；broker 不可达期间连续调用 | 租约过期后送达；AI 全 200，恢复后全部送达 |
| 签名凭据 | integration | 无 `ACTIVE` 凭据；主密钥缺失；轮换重叠期；同一项目两个 `api_key`、各自 `key_version` 不同 | 可重试并进告警；取 `valid_from` 最晚的可验签行，并列取 `id` 最大，与 `key_version` 大小无关 |
| 非信封响应 | integration | 摄取端回非 JSON 的 502、回非 JSON 的 400 | 前者 `PENDING`（`BILLING_UNAVAILABLE`），后者 `DEAD`（`INVALID_BILLING_RESPONSE`） |
| 死信告警可复核 | integration | 一行进 `DEAD` → `ai-usage-dead-ack` → 重投后再进 `DEAD`；另一行先被 503 复核并 `ack`、再进 `DEAD` 并重投 | 严重 → 消失 → 再次严重；后者的 `reviewed_at` 不受重投影响 |
| 撞行规则 | integration（MySQL） | 暂存条目撞上：`attempts=0` 的 `PENDING`、发送失败后退回的 `PENDING`（`attempts=1`，摄取端其实已入账）、已被 `release` 的 `PENDING`、`HELD`（同一条目第二次）、`DISCARDED`、`DELIVERED`、已带复核标记的 `DELIVERED` | 依次：转 `HELD` / 写复核标记并 `CRITICAL`（行仍 `PENDING`、照常投递，摄取端回 `already_*`，只一次财务效果）/ 不改 / 不改 / 不改 / 写复核标记并 `CRITICAL` / 不改 |
| 日志字段 | unit | 用现有 `JsonFormatter` 输出一条 `AI usage not persisted` 记录 | 四个 `usage_*` 字段数值原样保留、没有被脱敏；`import` 能读回同一组字段 |
| 超限的 413 | unit（`test_compose.py`） | 读 nginx 配置 | `/v1/messages` 的 413 指向返回 JSON 信封的命名 location |
| 签名交叉验证 | unit（Worker 沙箱可跑） | AI 侧 `client_rules` 生成的头交给验签侧 `verify_signature`；另用标准库 `hmac` 手算固定向量 | 验签通过；向量逐字相同 |
| 租户越权 | integration | A 的密钥调用；跨客户管理接口；伪造诊断 ID | 只有 A 的行；404；`CREDENTIAL_SCOPE_MISMATCH` 进 `DEAD` |
| 内容不落地（INV-9） | integration | `system`、`messages`、模型输出各带哨兵，跑调用 + 投递 + 422 + 502 + 供应商 400 | 新表、审计、`caplog`、所有响应信封里都没有哨兵 |
| 请求路径只写 outbox | integration | `before_cursor_execute` 记录一次调用期间的语句 | 写语句只有一条 `INSERT INTO ai_usage_outbox` |
| 健康检查 | integration | key 文件缺失；数据库不可达；Redis 不可达 | `/healthz/ai` 503 / 503 / 200；`/healthz/billing` 200 / 503 / 200 degraded；`/healthz` 始终 200 |
| 部署冒烟 | unit（`test_deploy.py`） | `/healthz/ai` 回 200 且 `data.status=ok`；回 200 但正文是 HTML（SPA 兜底）；回 503 | 通过 / 回滚 / 回滚 |
| 部署配置 | unit（`test_compose.py`） | 读 compose 与 nginx | `stop_grace_period: 100s`；`--timeout-graceful-shutdown 85`；两者与 `BILLING_AI_REQUEST_DEADLINE_SECONDS` 的关系（总时限 + 10 ≤ 优雅期 < 停机宽限）；健康检查 allow 名单与 `/readyz` 相同；`/v1/messages` 无 `deny all`、有 `billing_ai` 限速、`proxy_read_timeout` ≥ 75s、`client_max_body_size 80k`；`/v1/` 其他路径不转给 api |
| 金额精度 | — | AI 模块无金额 | 不适用 |

`acuven_ai_api` 的纯规则测试（签名规范串、回执、错误判定）搬过来作为 Worker 沙箱可跑的纯逻辑测试；`TestClient`、MySQL 与并发用例只在 CI。

## 8. 迁移与上线

- **数据迁移：不需要**（`acuven_ai_api` 的 outbox 为空、0 绑定）。
- **表结构迁移**：一次新迁移建两张新表、给 `usage_events` 加可空列与索引。凡是「从 head 降到固定版本、断言字面表集合 / 列集合」的迁移测试（`test_migrations.py`、`test_fx_rates_db.py` 等）都要一并更新并写进任务的 `allowed_change_paths`。
- **部署顺序**：每个任务合并即部署（迁移 → api → worker / beat）。摄取端先接受 `feature`，AI 路由后上线，所以 AI 模块发出的第一条带 `feature` 的事件一定被接受。没有签发 AI 密钥之前 `/v1/messages` 是惰性的。**AI 密钥要等第 8 项（告警与运维：`HELD` / 待复核 / `DEAD` 的告警与处置命令）合并后才签发**：在那之前请求路径与投递虽已上线，但没有调用方，落库失败的记录也就无从产生；`docs/deployment.md` 与第 4 项的管理接口说明（`docs/api.md`）都写明这一条。
- **合并「请求路径」那个任务之前**，Kelvin 在 VPS 的 `secrets/` 下建好 `anthropic.key`（建议沿用 `acuven_ai_api` 部署目录 `.env` 里那把）。
- **兼容窗口**：摄取契约只加可选字段，现有调用方不受影响；`/v1` 没有旧调用方。
- **回滚 / 前滚**：生产只前滚。关掉 AI = 吊销密钥，立即生效。迁移 downgrade 条件见 §2。
- **部分部署**：beat 先于迁移起来 → 扫描缺表报错、任务重试；迁移完成后自动恢复。
- **监控**：`ai_usage_outbox` 告警维度接入现有 `monitor.sh` / Healthchecks；负责人：管理员。
- **拆分**（每个任务只做一层，`depends_on` 串起来，每层合并后都能单独过 CI；编号在登记 PR 里定）：
  1. **数据库**：迁移（两张新表、`usage_events.feature` 与索引）与模型。审计动作与实体归类由写它们的任务各自加（第 3、8 项）。
  2. **摄取契约加 `feature`**：单条与批量 schema、校验、指纹（有才加键）、入库；`docs/api.md` 摄取一节。
  3. **管理规则**：签发、吊销（锁、上限、审计），只写服务函数与测试。
  4. **管理接口**：`ai-access` 三个接口；用量事件列表的 `feature` 筛选与列表 / 详情字段；`docs/api.md`。
  5. **AI 纯规则**：`/v1/messages` 请求校验、`client_rules.py`、假供应商下的 provider 适配器、签名交叉测试、`httpx2` 升为运行时依赖（打包冒烟）；全部可在 Worker 沙箱跑（不开路由）。
  6. **请求路径**（体量大，可分批交付：先用量写入与落库失败处置，再配置、secret、nginx 入口与优雅停机，最后路由；**优雅停机必须不晚于路由上线**）：路由、鉴权与进程内状态、模型护栏、outbox 写入、AI 模块独立 engine 与时限、提交结果核查、取消处理、落库失败的 `CRITICAL` 日志与暂存队列及其后台循环（含撞上已存在行时的转 `HELD` / 标复核）、配置与 compose secret、nginx `location = /v1/messages`、**优雅停机**（`Dockerfile` 的 `--timeout-graceful-shutdown 85` 与 api 的 `stop_grace_period: 100s`，以及 `test_compose.py` 钉住它们与请求总时限的关系；必须与请求路径同一次上线，否则请求路径上线后、优雅停机上线前的部署会在 Docker 默认的 10 秒后强杀在途调用，用量不留记录）、import 白名单测试、「计费全挂 AI 仍成功」测试、`docs/api.md` 的 `/v1/messages` 契约。⚠️ 合并前 Kelvin 建好 key 文件。
  7. **投递**：beat 任务、领取 / 签名 / 发送 / 结算、配置项。
  8. **告警与运维**（可分两批：先告警维度，再命令与运维文档）：`ai_usage_outbox` 告警维度（含 `HELD`；`tests/backend/test_internal_alerts.py` 钉着维度名）、阈值配置、`docs/api.md` 告警维度表；CLI 重投、`ai-usage-dead-ack` 与 `ai-usage-held`（`release` / `discard` / `import` / `ack`）及其审计动作；`docs/runbook.md` 与 `docs/deployment.md`。
  9. **健康检查**：`/healthz/ai`、`/healthz/billing`、nginx allow、`deploy.sh` 冒烟、`test_compose.py`（优雅停机已随第 6 项上线）。
  10. **前端**（不走闸门）：项目详情里的「AI API」面板（签发时 token 只显示一次、吊销）；用量事件页加「功能」列与筛选。
  11. **CI 策略**（不走闸门）：`check_repo_policy.py` 拒绝同时改 `app/ai_api/` 与钱包 / 支付 / 定价路径的 PR。
- **上线后的试点验收**（人工；第 3 阶段 + E-13）：
  - 前置（Kelvin 在后台）：模型目录有 `claude-sonnet-5-5`、`LLM_TOKEN` 供应商价格与参考定价已发布；内部租户的 `acuven-crm` 项目签集成凭据、签 AI 密钥；另建 `PREPAID` 测试租户与项目，同样两步并调账充值。
  - 内部计量路径：真实调用 200；后台按 `request_id` 查到恰好一条事件、`feature` 正确，核对预计供应商成本、参考售价、钱包实扣 0；停用租户 → 403，恢复 → 200。
  - `PREPAID` 路径：调用扣钱包；`--include-delivered` 重投同一 `event_id`，钱包只扣一次；调用到余额 ≤ 0 → 下一次 403，记录停机时的余额与透支额（ADR-0010）；充值 → 200。
  - 「计费离线时 AI 仍可用」的等价演练：把 celery-worker 的 `BILLING_AI_INGEST_BASE_URL` 临时改成不可达地址并重建 celery-worker，调用照常 200、行积压；改回后补投、每个 `event_id` 只出现一次。
  - E-13：CRM 客户摘要上线（CRM 仓库）；后台按 `feature=crm.contact_summary` 筛选，统计每次摘要的平均 token 与预计供应商成本。
  - 验收记录写明：自调**验不到跨主机网络与时钟偏差**，留给 `acuven_aichat` 接入时验收（E-19）。
- **`acuven_ai_api` 下线**：2026-10-12 已禁用 Deploy 工作流、删除 VPS 容器。保留空库（2026-11-11 之后经 Kelvin 同意再删）、部署目录与镜像。本仓库「请求路径」任务合并后，在那个仓库改 README 指向本仓库、删部署 secrets、归档。它没有登记 OpenClaw 控制面，没有登记要撤（I-07）。

## 9. 方案取舍

| 方案 | 优点 | 风险 | 未采用原因 |
| --- | --- | --- | --- |
| **采用：同一进程、同一个库；通用 `/v1/messages`；功能定义在业务系统；用量 HMAC 自调并带 `feature`；状态进程内读** | 加功能只改业务系统；AI 模块很少改动，发布耦合跟着变小；身份只有一套；自调是真实的端到端回归 | 密钥泄漏的后果变大；防提示注入与质量评测全靠调用方；数据库或进程不可用 → AI 不可用 | — |
| 每个功能一个专用接口、提示词放在 AI 模块（v2） | AI 模块能控制每个功能的输入输出与提示词；密钥只能做被允许的事 | 每加一个功能两边都要改、还要后台开通 | Kelvin 2026-10-13：太麻烦，功能放业务系统 |
| 提示词模板存在 AI 模块的数据库里，后台可编辑 | 不改代码就能调提示词 | 业务系统仍要改输入与界面，等于两处维护；多一套模板版本管理 | 同上，加功能仍要动两处 |
| 不加 `feature`，每个功能单开一个项目 | 不改摄取契约 | 项目与密钥随功能数增长，调用方要管多把密钥 | Kelvin 2026-10-13：要加功能标签 |
| `feature` 加进指纹时总是带键（缺省为 null） | 规范对象形状固定 | 所有历史事件的指纹都变，重放被误判冲突 | 只在有值时加键，历史指纹不变 |
| 同一个镜像起两个容器、按角色开关路由（E-20 的退路） | AI 不随计费部署重启 | 多一个容器与健康检查；仍共用数据库 | Kelvin 2026-10-12：现在不做 |
| AI 用量直接调用摄取函数 | 少一次本机 HTTP | 绕过验签契约；破坏边界 | Kelvin MERGE 第 6 条 |
| 状态也走 HMAC 自调 `effective-status` | 与外部接入方一致 | 多一次本机 HTTP，把线程池与 Redis 引进请求路径 | Kelvin 2026-10-12 同意进程内读 |
| 写 outbox 后立即 `delay()` 触发投递 | 投递更快 | broker 不可达时可能阻塞请求路径 | 10 秒延迟无业务影响 |
| 签名侧复用验签的 `canonical_request` | 少一份代码 | 签名与验签同源，同时写错会互相验过 | 见 §2「模块边界」 |
| 模型调用用同步 `urllib` 放线程池 | 不新增依赖 | 60 秒的调用占住线程，饿死摄取与后台接口 | `httpx2` 已是 dev 依赖 |
| 模型护栏读供应商价格（只许已发布价格的模型） | 杜绝 `PRICING_ERROR` | AI 请求路径要 import 定价，破坏边界 | 只查目录；缺价格由现有 `PRICING_ERROR` 告警发现，事件不丢 |

## 10. 未决问题与假设

**Kelvin 2026-10-12 已拍板**：

1. **状态怎么读**：进程内直接读，不经 HMAC 自调 `effective-status`。
2. **模型成功但 outbox 写失败**：503、不返回正文，偏离 spec §20，写进 §141.3 与 ADR-0012。§20 其余两项（最高级告警、可审计的人工对账）照做，见 §2「用量落库失败的处置」（v4 补）。
3. **`/v1` 暴露范围**：对公网开放；按来源限速是本设计加的防滥用措施，不是按客户端用量限流。
4. **进程内并发上限**：不设。
5. **`acuven_ai_api` 下线**：现在下线，已执行。
6. **AI 改动与支付改动不进同一个 PR**：CI 机械检查。
7. **部署排空窗口**：接受最长约 75 秒。v5 为给请求总时限留余量调到约 85 秒（见 §2「健康检查与优雅停机」）。

**Kelvin 2026-10-13 已拍板**：

8. **功能定义放业务系统**：AI 模块只提供通用调用与计费，增加功能只改业务系统（取代 v2 的「能力注册表 / 能力白名单 / 每功能专用接口」）。
9. **功能标签**：现在就加（`feature`，见 §2）。
10. **模型护栏**：只许目录里的模型（同意推荐；落地为「目录内 `ACTIVE`」，不读价格，理由见 §9 最后一行）。
11. **单次上限**：请求体 64 KiB、`max_tokens` 4000，可配置（同意推荐）。
12. **接口形状**：Anthropic Messages API 子集，返回原文、`stop_reason`、token 与 `request_id`（同意推荐）。

**不需要拍板、但要知道的**：

- 这一版取代了 E-17 实施要点里的「对外 `/v1` 接口与响应契约不变」：没有调用方在用旧接口，CRM 也还没接，所以没有兼容成本。`acuven_hub/DECISIONS.md` 由项目群那边同步。
- 定价按产品线分开（F-08）不阻塞本设计；`feature` 不参与计价。
- 每个开 AI 的项目都要先签一份集成凭据（自调签名用）。

**尚未验证的假设**：

- `httpx2` 的 `AsyncClient` 与 `acuven_ai_api` 用的 `httpx` 在超时与错误分类上行为一致；错了由供应商错误映射测试发现。
- Anthropic 对不合法 `output_config.format` 返回 400（而不是 200 后乱答）；错了的后果是调用方拿到 200 + 不合格式的原文，由调用方的解析兜住，不影响计费。

**需要谁拍板**：产品口径已由 Kelvin 全部拍板；其余由 Codex 审查。

## 11. 审查与版本绑定

审查方要回答的五个问题常驻在 [scripts/review_checklist.md](../../scripts/review_checklist.md) 的「设计审查」一节，此处不重复。

**版本绑定规则**（规则的出处是 [WORKFLOW §3](../WORKFLOW.md)；这里保留一份是因为审查材料里只有本 Issue 正文，两处改一起改）：

- Codex 的批准必须写成 `APPROVED: design v<N>`，`<N>` 取本 Issue 顶部的「设计版本」
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

## 12. 版本变更记录

| 版本 | 日期 | 改了什么 | 触发原因 |
| --- | --- | --- | --- |
| v1 | 2026-10-12 | 初稿 | — |
| v2 | 2026-10-12 | `/v1` 改为对公网开放：公网与同机两个入口、`billing_nginx` 按来源限速、`infra_nginx` 超时与 Cloudflare 两项跨仓库前置；`acuven_ai_api` 已下线；新增「能力注册表与以后新增能力」；§10 改为已拍板 | Kelvin 答复 §10：Q3 公网、Q5 现在下线、Q6 问以后怎么加，其余同意推荐 |
| v3 | 2026-10-13 | 去掉能力注册表、能力白名单与每功能专用接口，改为通用 `POST /v1/messages`（Messages API 子集 + 必填 `feature`），功能定义移到业务系统；新增模型护栏（目录内 `ACTIVE`）与单次上限；摄取契约加可选 `feature`（指纹只在有值时加键）、`usage_events` 加列与索引、后台按功能筛选；表结构从三张新表改为两张新表 + 一列；拒答 / 截断改为 200 由调用方判断；供应商 400 类映射 422；§3–§10 跟着改；拆分从 10 项改为 11 项 | Kelvin 2026-10-13：功能定义放 CRM，AI 模块只负责调用与计费；功能标签现在就加；护栏与接口形状按推荐 |
| v4 | 2026-10-13 | 补 outbox 落库失败的处置：固定文案的 `CRITICAL` 日志、进程内暂存队列与后台循环、库恢复后落成 `HELD` 行、`HELD > 0` 严重告警、CLI `release` / `discard` / `import` 与审计；outbox 状态加 `HELD`、`DISCARDED` 与 `resolved_at`；补 MySQL 并发签发测试 | Codex 对 v3 的两个阻断项（spec §20 的告警与对账未闭合；密钥上限缺并发测试） |
| v5 | 2026-10-13 | 提交报错时先核查再定结果；暂存落库撞上已存在的行时，`PENDING` 转 `HELD`、已进入投递的标 `review_required_at` 并告警、`ack` 复核留审计；AI 模块独立 engine 与 5 秒库超时、75 秒请求总时限、优雅停机 85 秒 / 停机宽限 100 秒、取消时也记 `CRITICAL` 并暂存；outbox 加两列；补对应状态行、失败矩阵与测试 | Codex 对 v4 的两个阻断项（提交结果不确定时会绕过人工处置自动计费；停机超时会无声丢失用量） |
| v6 | 2026-10-13 | 签名凭据改为「可验签行里 `valid_from` 最晚、并列取 `id` 最大」；非信封的 5xx 可重试、其他非信封响应进 `DEAD`；`DEAD` 告警只计未复核的行，`ack` 扩到 `DEAD`、重投清 `reviewed_at`；部署冒烟同时校验正文 `data.status`；写明 import 白名单只约束 `app.*`、投递任务照现有写法注册；补对应状态行、失败矩阵与测试 | 登记前预审（另一会话按 v2 登记时的核对结论，代码已核实）：`key_version` 跨 `api_key` 不可比；非信封 5xx 会把用量判死；`DEAD` 总数告警会永久报警；SPA 兜底让冒烟误判成功 |
| v7 | 2026-10-13 | 日志 `import` 遇到已存在的 `event_id` 时不再拒绝，改为照「提交结果不确定」第 2 步转 `HELD` 或写复核标记，新增审计 `AI_USAGE_HELD_IMPORT`；投递退避加 [0.5, 1.0] 随机系数；补重启后补录的组合测试与退避抖动测试 | Codex 对 v6 的两个阻断项（重启后补录无法建立复核状态；退避缺 spec §22 要求的 jitter） |
| v8 | 2026-10-13 | 优雅停机从第 9 项挪到第 6 项（与请求路径同一次上线）；`httpx2` 升运行时依赖挪到第 5 项；写明 AI 密钥要等第 8 项合并后才签发；第 9 项改名「健康检查」 | Codex 对 v7 的阻断项（分批上线时，请求路径已上线、优雅停机未上线的那段部署会强杀在途调用、不留记录） |
| v9 | 2026-10-13 | 撞行规则只碰从未经人工决定的行，`HELD` / `DISCARDED` / 已 `release` / 已带复核标记一律不改；`DEAD` 的处理完毕改用独立的 `dead_acked_at` 与 `ai-usage-dead-ack`（新审计动作 `AI_USAGE_DEAD_ACK`），重投的字段重置写全；`CRITICAL` 字段名避开会被脱敏的子串并定为 `import` 的输入格式；取消后线程可能晚提交，按结果未知处理；落库轮次遇连接错误即停；`HELD` 行的初值；库访问分三个超时并重算 75 秒总时限、模型超时改为 `min(60, 剩余 − 16)`、不另设计时器、同步库访问走线程池；key 文件读取与 503 的位置；200 的 `request_id` 与 HTTP 关联 ID 的关系；文本以换行拼接；nginx 超限 413 也给 JSON 信封；摄取 `feature: null` 等同不带；后台 `feature` 参数在鉴权之后校验；三个管理接口的请求体、响应字段、列表范围与 404 错误码，`entity_id` 的取值；outbox 各列的可空、宽度与排序规则；投递任务名、beat `expires`、运行时间预算与主密钥未配置的错误码；严重 / 警告对应 `P1` / `P2`，阈值经 `AlertContext` 传入；管理与运维侧可 import 审计；审计动作改由写它们的任务各自加；§8 写明第 6、8 项可分批交付，优雅停机不晚于路由 | 登记前预审第一轮（`worker.design_precheck`，导出 `0f370a1`）：061、062、064 的 `DESIGN_DEFECT` 与各任务的 `UNDECIDED` / `SCOPE_GAP` / `TOO_LARGE` |
| v10 | 2026-10-13 | 三个阶段（鉴权读、模型调用、写 outbox 与核查）各用 `asyncio.timeout` 管墙钟时长：鉴权读超时 503、模型超时 502、写入超时按结果未知进暂存；补分阶段慢响应与慢速逐段返回的测试 | Codex 对 v9 的阻断项（httpx 与驱动的超时是单次操作的，不能保证 75 秒墙钟总时限） |
| v11 | 2026-10-13 | 同步库访问改用 `anyio.to_thread.run_sync(abandon_on_cancel=True)` 与 AI 模块自己的线程容量限制（`BILLING_AI_DB_THREADS`），不用会等线程跑完的 `run_in_threadpool`；写明超时那一刻告警与暂存只写日志和内存；补「同步库调用永不返回」与「线程名额占满」的测试 | Codex 对 v10 的阻断项（`run_in_threadpool` 不响应取消，外层墙钟到点也要等线程跑完） |
| v12 | 2026-10-13 | AI 模块的同步库访问改用专用的固定大小 `ThreadPoolExecutor`（`max_workers` 即存活线程上限）+ `asyncio.wrap_future` + 各阶段 `asyncio.timeout`；不用 `CapacityLimiter`；补「连续超时时存活线程不超过上限」的测试 | Codex 对 v11 的阻断项（`abandon_on_cancel` 放手时会归还 `CapacityLimiter` 名额，限不住仍在跑的线程） |
| v13 | 2026-10-13 | 撞行时只有 `attempts = 0` 的 `PENDING` 转 `HELD`，尝试过投递的 `PENDING` 与已领走 / 已送达 / 死信一样写复核标记；AI 库访问加准入计数（上限 = 线程数 + `BILLING_AI_DB_QUEUE_MAX`，底层任务结束才释放），满了立刻失败；补对应测试 | Codex 对 v12 的两个阻断项（回执丢失后退回 `PENDING` 的行被当成从未投递；线程池等待队列无界） |

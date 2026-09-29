# AIH-TASK-030 设计：批量用量摄取端点（已批准 v3）

> **来源**：设计闸门 Issue #180。本文件是 `APPROVED: design v3` 那一版正文的**逐字副本**，审查者为 Codex。
> 放进仓库，是因为 OpenClaw Worker 在沙箱里不联网、读不到 GitHub Issue。
> **实现以本文件为准**；与 Issue 不一致时，以 Issue #180 上被批准的 v3 为准。设计要改，就回到 Issue 升版本、重新过闸门，不要直接改本文件。
> 正文里的「T-F」即 [PHASE-2-plan.md](PHASE-2-plan.md) 的占位名；写「编号登记时分配」的实现任务，登记为 `AIH-TASK-030`。正文提到的其他占位名（T-A…T-L）对应的登记编号见 `docs/TODO.md` 的 Phase 2 一节。
> 与 Issue 正文的唯一差别：一处链接目标由 GitHub 网页上的 `../blob/main/…` 改成仓库内的相对路径，链接文字与其余正文逐字相同。

---

## 0. 准入判定

| 任务涉及 | 要填的范围 |
| --- | --- |
| 钱包 / 账本 / 定价 / 汇率 / 支付 / 幂等 / 状态机 | **全部章节** |
| 用量摄取 / 认证与会话（集成侧与平台侧）/ Webhook | §1–§7（**§6 不可省**） |
| 前端 / 文档 / CI / 脚本 | 不走本闸门 |
| 既不碰钱、也不碰用量摄取 / 认证与会话 / Webhook 的后端改动 | 不走本闸门 |

本任务档位：`全部`。它是用量摄取的批量入口，逐条走全局 `event_id` 幂等（§23）。

---

状态：`READY_FOR_REVIEW`
负责人：Claude
**设计版本**：`v3`（v1：nginx 的 413 不是信封 → v2 依赖 T-E 的 JSON 413；v2 已获批准，但 T-E v8 把 401 改为不可重试，本设计的失败矩阵写死了旧值，v3 同步，见 §12）
对应需求：spec §20、§23、§37、§38、§39、§107、§119；`REQ-INGEST-001`、`REQ-INGEST-002`、`REQ-IDEMP-001`、`REQ-AUTH-001`
目标 PR：待开（批准后在 `.platform/tasks.yaml` 登记实现任务，编号登记时分配）
Phase 2 计划：`docs/design/PHASE-2-plan.md` 的 T-F。**依赖 T-E（#176）**：本设计复用 T-E 的认证、nonce、逐条校验、指纹、插入优先、冲突记录与错误码表，只定义「批」这一层；T-E 若在审查中改变这些契约，本设计随之升版本。

## 1. 目标与边界

- **要解决的问题**：spec §38 列了 `POST /api/v1/integration/usage-events/batch`，§39 定义了批的语义：默认上限 100 条且可配置、允许部分成功、一条失败不回滚其他条、响应给出逐条结果与汇总、冲突与校验错误不可重试。应用积压补投（Phase 3）要靠它把 §119 的「积压回补 ≥ 峰值 5 倍」做出来。
- **可观察的完成标准**：
  1. 一个签名正确的批，其中每条事件的处理结果与「把它单独发给 T-E 的单条端点」完全相同（状态、`error_code`、`retryable`、写库效果）。
  2. 任何一条的失败都不影响其他条；已提交的条在后续条失败时不回滚。
  3. 响应 200，带 `accepted` / `duplicates` / `rejected` 汇总与按请求顺序排列的逐条结果。
  4. 批层面的失败（认证、请求体过大、顶层结构不合法、条数超限）整体拒绝、不写任何事件，带 `retryable`。
- **明确不做什么**：单条端点（T-E）；按凭据限流（T-E 已记为待办）；异步处理（T-H）。
- **现有行为与问题证据**：T-E 设计 §1 把批量端点划给本任务。

## 2. 设计概要

```text
POST /api/v1/integration/usage-events/batch
  ① 读原始请求体（> 1 MiB → 413 PAYLOAD_TOO_LARGE，retryable = false）
  ② 认证：与 T-E ②–④ 相同（签名覆盖整个批的原始字节）→ 401 INTEGRATION_AUTH_FAILED
  ③ nonce：与 T-E ⑤ 相同，整批一个 X-Acuven-Request-Id
  ④ 顶层结构：{"events": [ … ]}，extra="forbid"；events 为非空数组、条数 ≤ BILLING_INGEST_BATCH_MAX（默认 100，§39）
       → 不合法 422 VALIDATION_ERROR / 超限 422 BATCH_TOO_LARGE，retryable = false，不写任何事件
  ⑤ 按数组顺序逐条：用 T-E 的「单条处理」函数（校验顺序 ④–⑨、指纹、插入优先、冲突记录）
       每条自己一个事务；这一条的结果写进 results[i]，继续下一条
  ⑥ 200：{accepted, duplicates, rejected, results[]}
  ⑦ 响应之后：按 T-E 的规则节流更新 last_used_at（一次）
```

### 单条处理的复用

T-E 的端点实现必须拆成两层（本设计对 T-E 实现提出的结构要求，T-E 的实现范围已包含 `app/services/usage_ingest.py`）：

- 请求层：读体、认证、nonce —— 批与单条各做一次。
- **单条层 `ingest_one(session_factory, credential, raw_event, now) -> ItemOutcome`**：从「JSON 对象」开始，走 T-E 的校验顺序 ④–⑨、指纹、插入优先 / 冲突路径，自带事务，返回 `(status, processing_status, error_code, retryable)`，**不抛出业务错误**（只有数据库不可用这类基础设施错误以 `retryable = true` 的结果返回）。
- 单条端点 = 请求层 + 一次 `ingest_one` + 把结果映射成 T-E 的 HTTP 响应；批量端点 = 请求层 + 逐条 `ingest_one` + 汇总。**两者共用同一份单条逻辑**，保证「批里的一条」与「单独发」结果一致。

### 响应（§39、§107 信封）

```json
{
  "success": true,
  "data": {
    "accepted": 97,
    "duplicates": 2,
    "rejected": 1,
    "results": [
      {"index": 0, "event_id": "evt_…", "status": "accepted", "processing_status": "RECEIVED", "error_code": null, "retryable": false},
      {"index": 1, "event_id": "evt_…", "status": "already_received", "processing_status": "RECEIVED", "error_code": null, "retryable": false},
      {"index": 2, "event_id": "evt_…", "status": "rejected", "processing_status": null, "error_code": "IDEMPOTENCY_CONFLICT", "retryable": false}
    ]
  },
  "error": null,
  "request_id": "…"
}
```

- `status`：`accepted`（新接收）/ `already_received` / `already_processed`（合法重复，同 T-E）/ `rejected`（其余一切，含可重试的）。
- `accepted` + `duplicates` + `rejected` = 条数；`duplicates` 数 `already_*` 两种。
- `error_code` 与 `retryable` 取 T-E 错误表里的码与固定取值（例如 `UNKNOWN_USAGE_TYPE` → `true`、`IDEMPOTENCY_CONFLICT` → `false`、`VALIDATION_ERROR`（含 `event_id` 不是 UUIDv7 / ULID）→ `false`、数据库不可用 → `SERVICE_UNAVAILABLE` / `true`）。本设计不另行规定任何码的 `retryable`，一律以 T-E 批准的错误表为准。成功的条 `retryable = false`（不需要重试）。
- `event_id`：该条里的 `event_id` 字符串；该条连 `event_id` 都取不到（不是对象、缺字段）时为 `null`，此时客户端按 `index` 对应。
- 客户端只按每条的 `retryable` 决定该条是否重投（与 T-E 同一规则）；批本身 200 不代表每条都成功。

### 批内重复的 `event_id`

按数组顺序处理：第一条正常；后面同 `event_id` 的条走 T-E 的冲突路径 —— 内容相同得 `already_received`（计入 `duplicates`），内容不同得 `IDEMPOTENCY_CONFLICT`（记冲突行与审计）。不在进入逐条处理前预先去重（预先去重会让「批里的一条」与「单独发」的结果不同）。

### 基础设施错误中途发生

某一条处理时数据库不可用：该条 `SERVICE_UNAVAILABLE`、`retryable = true`；**继续尝试后面的条**（每条各自一个事务、各自重连），不提前放弃 —— 数据库短暂抖动时能多收几条；若持续不可用，后续条都会得到同样的结果。已提交的条不受影响（REQ-INGEST-001：每条的「已接收」只在它自己的行提交之后成立）。

### 请求体上限

1 MiB。100 条 × 单条 16 KiB 的上限理论上可达 1.6 MiB，但单条实际约 1 KiB；超过 1 MiB 的批 413 `PAYLOAD_TOO_LARGE`、`retryable = false`，客户端拆小重发。

**两层给出同一个信封**：T-E（v7）为 `/api/v1/integration/` 前缀配置了 nginx 的 `client_max_body_size 1m` 与 `error_page 413` → JSON 信封（`PAYLOAD_TOO_LARGE`、`retryable = false`、`request_id = null`）；本端点在应用层同样按 1 MiB 判定（请求恰在 nginx 上限附近时由应用给出同一个码）。所以无论在哪一层被拒，集成方看到的都是信封，能按 `retryable` 决定终态。本任务不再改 nginx；若 T-E 的前缀块上限被改，本端点的应用层上限必须同步（`test_compose.py` 用例钉住两者相等）。单条内部的 16 KiB 上限对批里的每个元素同样适用（按该元素重新序列化后的长度判定），超出即该条 `rejected` / `PAYLOAD_TOO_LARGE` / `false`。

### 数据库

无新表。复用 T-E 的 `usage_events`、`usage_event_conflicts`。

### 事务边界

每条一个事务（`ingest_one` 内部），与 T-E 单条完全相同；批本身没有外层事务（§39「一条失败不回滚其他条」）。审计（归属不符、冲突）与各自的那条在同一事务或 T-E 规定的独立事务里。

### 外部系统与异步边界

与 T-E 相同：Redis 只用于 nonce（不可用时放行）；不入队。

### 时间语义

整批共用一个服务端 `now`（签名窗口、`occurred_at` 未来上界、`received_at`），在请求层取一次，保证同一批里的判定口径一致。

### 金额精度

不涉及（同 T-E）。

### 审计

与 T-E 相同，逐条产生（归属不符、冲突）。

### 性能

§119：接收 p95 ≤ 500 ms「单条或批量」。100 条串行各自提交，单次提交若在 3–4 ms 量级则整批约 0.4 s；实测归 T-I（压测）。若达不到，T-I 按「同一请求内的多条合并为一个事务、冲突条单独重试」的方案另开任务 —— 那会改变「一条失败不回滚其他条」的实现方式，必须另过闸门，本任务不预先做。

## 3. 不变量影响矩阵

| 不变量 | 是否触碰 | 可能怎样被破坏 | 设计控制 | 验证方式 |
| --- | --- | --- | --- | --- |
| INV-1 | **是**（间接） | 批层面的一个坏元素让整批被拒，应用侧积压 | 元素级错误只影响该条 | 混合批用例 |
| INV-2 事件不重复扣费 | **是** | 批内或批间重复产生两行 | 逐条走 T-E 的唯一约束与插入优先 | 批内重复、批与单条交叉重复用例 |
| INV-8 租户不可互访 | **是** | 批里夹带别家租户的归属 | 归属只来自凭据；逐条比对诊断字段 | 混合归属的批用例 |
| INV-9 对话内容不入库 | **是** | 元素夹带内容 | 逐条 `extra="forbid"` | 用例 |
| INV-11 | **是** | 同 INV-2 | 同上 | 同上 |
| INV-14 队列丢失不毁持久工作 | **是** | 批的 200 被误读为全部已持久 | 逐条结果；每条的「已接收」只在该条提交后 | 中途数据库故障用例 |
| 其余 | 否 | — | — | — |

## 4. 状态与并发

- 状态：与 T-E 相同（只产生 `RECEIVED`）。
- **串行化**：批内按顺序逐条；两个并发批里有相同 `event_id` 时，由唯一约束决出（T-E）。
- **幂等键**：逐条的 `event_id`；整批的 `X-Acuven-Request-Id`（nonce）。**重发整批**（新签名、新 Request-Id）是安全的：已接收的条得到 `already_received`。
- **相同 ID、不同载荷**：逐条冲突（T-E）。
- **重试层**：客户端按逐条 `retryable` 只重投需要的条。

## 5. 失败与恢复矩阵

| 失败点 | 对外结果 | 数据最终状态 | 是否可重试 | 恢复来源 | 告警 |
| --- | --- | --- | --- | --- | --- |
| 请求体 > 1 MiB | 413，`retryable = false` | 无 | 拆小后重发 | — | 无 |
| 认证 / nonce | 401，`retryable = false`（T-E v8） | 无 | 否：整批进应用侧死信，修好配置后人工重投 | — | 日志；应用侧告警 |
| 顶层结构不合法 / 条数超限 | 422，`retryable = false` | 无 | 修正后 | — | 无 |
| 某条校验失败 / 冲突 / 归属不符 | 200，该条 `rejected` | 其他条照常 | 按该条 `retryable` | — | 冲突与归属不符同 T-E |
| 某条时数据库不可用 | 200，该条 `SERVICE_UNAVAILABLE` / `true` | 之前的条已提交 | 重投该条 | 客户端 | readyz |
| 所有条都时数据库不可用 | 200，全部 `rejected` / `true` | 无 | 重投 | 客户端 | readyz |
| 响应丢失 | 超时 | 部分或全部已提交 | 重发整批 → 已提交的条 `already_received` | 数据库 | 无 |
| 未预期异常（非逐条可归类） | 500，`retryable = true` | 已提交的条保留 | 重发整批（安全） | — | 错误日志 |

## 6. 数据与安全边界

- **租户过滤、鉴权主体、禁止返回的字段、日志、密钥、内容**：与 T-E 相同；批响应的逐条结果只含本凭据自己请求里的 `event_id` 与处理状态，冲突条不回显原事件任何信息。
- 批请求体不进日志；日志只记条数与汇总。

## 7. 测试证据计划

| 风险/需求 | 测试层级 | 场景 | 预期结果 |
| --- | --- | --- | --- |
| 正常 | integration（API） | 3 条合法事件 | 200；`accepted = 3`；三行 `RECEIVED` |
| 与单条一致 | integration | 对同一组输入（合法、重复、冲突、每种校验错误、未知类型、归属不符），分别用单条端点与批量端点在两个干净库里处理 | 逐条结果（状态、`error_code`、`retryable`）与写库效果完全相同 |
| 部分成功 | integration | 合法、冲突、校验错误混在一批 | 合法的写入；汇总与逐条正确 |
| 批内重复 | integration | 同一 `event_id` 相同内容两次；不同内容两次 | 第二条 `already_received` / `IDEMPOTENCY_CONFLICT` |
| 中途数据库故障 | integration | 第 2 条处理时注入数据库错误 | 第 1 条已提交；第 2 条 `SERVICE_UNAVAILABLE` / `true`；第 3 条照常尝试 |
| 条数上限 | integration | 100 条；101 条；0 条；配置改为 10 后 11 条 | 200 / 422 `BATCH_TOO_LARGE` / 422 / 422 |
| 请求体上限 | integration | 1 MiB + 1 字节（应用层）；单个元素超 16 KiB | 413 JSON 信封、`retryable = false`；该条 `PAYLOAD_TOO_LARGE` |
| 经过 nginx 的 413 | 部署后实测 | 经本栈 nginx 向批量端点发 1 MiB + 1 字节 | 413 JSON 信封（T-E 的前缀块），结果写进任务记录 |
| 两层上限一致 | unit（`test_compose.py` 同款） | nginx 集成前缀块的上限与 `BILLING_INGEST_BATCH_MAX_BYTES` 默认值 | 相等 |
| 元素不是对象 / 缺 `event_id` | integration | `events` 里有数字、空对象 | 该条 `rejected`、`event_id = null`、`VALIDATION_ERROR` |
| 认证与重放 | integration | 错签名；同一 Request-Id 重发 | 401、`retryable = false`；不写任何事件 |
| 重发整批 | integration | 同一批（新签名、新 Request-Id）发两次 | 第二次全部 `already_received` |
| 同一 `now` | unit | 冻结时钟，批内逐条的 `received_at` 与未来上界 | 全部相同 |
| 并发批 | integration（真 MySQL） | 两个批含同一 `event_id` 并发 | 恰好一行；另一批该条 `already_received` |

## 8. 迁移与上线

- 无迁移。合并即自动部署；端点立即可用。
- nginx：集成前缀块由 T-E 建；本任务不改 nginx，只在 `tests/backend/test_compose.py` 加「两层上限一致」的用例。
- 回滚：纯代码。

## 9. 方案取舍

| 方案 | 优点 | 风险 | 未采用原因 |
| --- | --- | --- | --- |
| **采用**：逐条事务、共用单条函数、批层 200 + 逐条结果 | §39 字面；批与单条结果一致 | 100 次提交的延迟（T-I 实测） | — |
| 整批一个事务 | 快 | 一条失败回滚全部，违反 §39 | 不采用（压测不达标时另过闸门） |
| 进入逐条前先去重 | 少几次数据库往返 | 批里的一条与单独发结果不同 | 不采用 |
| 有任一条失败就返回 207 / 4xx | 状态码更「显眼」 | §39 的形状是汇总 + 逐条；客户端必须看逐条 `retryable` 才知道怎么做 | 200 |
| 数据库故障后放弃剩余条 | 省几次失败的尝试 | 短暂抖动时少收几条 | 继续尝试 |

## 10. 未决问题与假设

- **未决问题**：无。
- **需要谁拍板**：无。
- **尚未验证的假设**：T-E 批准的版本保留本设计引用的契约（校验顺序、错误表与 `retryable`、`ingest_one` 可拆出）；T-E 若改，本设计升版本。

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

- `app/services/usage_ingest.py`（若 T-E 实现时未拆出 `ingest_one`，在这里拆）
- `app/schemas/usage_ingest.py`、`app/api/integration_usage.py`、`app/core/config.py`（`BILLING_INGEST_BATCH_MAX`）
- `tests/backend/test_usage_ingest_batch_api.py`（新）、`tests/backend/test_usage_ingest_api.py`、`tests/backend/test_config.py`、`tests/backend/test_compose.py`
- `docs/api.md`、`docs/TODO.md`

## 12. 版本变更记录

| 版本 | 日期 | 改了什么 | 触发原因 |
| --- | --- | --- | --- |
| v1 | 2026-09-29 | 初稿 | — |
| v2 | 2026-09-29 | 请求体上限改为依赖 T-E v7 的 nginx JSON 413 与应用层同一上限，补「经过 nginx 的 413」部署后实测与「两层上限一致」用例 | Codex 判 v1 REQUEST_CHANGES：nginx 默认 413 是 HTML，集成方拿不到信封 |
| v3 | 2026-09-29 | 失败矩阵与认证用例的 401 改为 `retryable = false`（与 T-E v8 一致）；逐条结果的 `retryable` 明确以 T-E 批准的错误表为唯一来源 | 同步 T-E v8（#176）：401 改为不可重试 |

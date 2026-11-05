# AIH-TASK-164..171 设计：Billing Client 库 acuven_billing_client（已批准 v24）

> **来源**：设计闸门 Issue #571。本文件是 `APPROVED: design v24` 那一版正文的**逐字副本**（送审时状态行为 `READY_FOR_REVIEW`，批准后 Issue 上只把状态行改成了 `APPROVED: design v24`）。审查：Codex 额度不可用期间由 Claude Code 审查，二十四轮，每一版的改动与触发原因见 §12。中间几次获批准后，登记前预审（`worker.design_precheck`）又报出设计缺陷，各升一版重新过闸门；v14 之后的几轮集中在本地存储故障、降级与恢复。
> 放进仓库，是因为 OpenClaw Worker 在沙箱里不联网、读不到 GitHub Issue。
> **实现以本文件为准**；与 Issue 不一致时，以 Issue #571 上被批准的 v24 为准。设计要改，就回到 Issue 升版本、重新过闸门，不要直接改本文件。
> Kelvin 2026-11-05 的决定：四个产品决定（第一个接入方、outbox 存储、第一版范围、发布方式，见 §1）；v12 之后继续收口；一个 outbox 文件只给一个进程用（§1「进程模型」）。
> 审查建议项（不阻断），已写进契约：v6 —— `from_env` 解析 `WEBHOOK_SECRETS` 格式错误时异常消息不得含密钥（`AIH-TASK-164`）；v21 —— 不建表的打开方式先确认 `<path>` 存在、再取单进程锁，路径不存在时不建任何文件（`AIH-TASK-165`）。

## 0. 准入判定

| 任务涉及 | 要填的范围 |
| --- | --- |
| 钱包 / 账本 / 定价 / 汇率 / 支付 / 幂等 / 状态机 | **全部章节** |
| 用量摄取 / 认证与会话（集成侧与平台侧）/ Webhook | §1–§7（**§6 不可省** —— 密钥存储与轮换、鉴权、租户过滤、日志脱敏都在那一节） |
| 前端 / 文档 / CI / 脚本 | **不走本闸门**，直接开 PR |
| 既不碰钱、也不碰用量摄取 / 认证与会话 / Webhook 的后端改动 | **不走本闸门**，直接开 PR |

本任务档位：`全部`。理由：客户端有 outbox 状态机，`event_id` 幂等决定「同一次用量至多扣一次费」，同时涉及用量上报、集成签名与状态 webhook 验签。

---

状态：`READY_FOR_REVIEW`
负责人：Claude
**设计版本**：`v24`
对应需求：spec §35（Integrated Application Billing Client）、§36（集成凭据与轮换）、§37（请求签名）、§38（集成接口）；`docs/api.md`「集成请求签名」「集成用量事件摄取」「批量摄取」「有效状态」「状态 webhook」「对账」各节
目标 PR：待开（拆成 `AIH-TASK-164`..`171`，见 §11）

## 1. 目标与边界

### 要解决的问题

外部接入方（不经 AI 模块调模型的应用，按 acuven_hub `DECISIONS.md` E-17、E-19）要把用量可靠地上报给本平台，并按平台的有效状态决定是否拦截新的模型调用。服务端的签名、摄取、批量、有效状态与状态 webhook 契约都已上线，但每个接入方各写一遍容易写错（签名规范串、重试判定、版本规则都有细节）。spec §35 要求一个可复用的 Python 包 `acuven_billing_client`，让应用层不必知道计费 API 的细节。

### Kelvin 2026-11-05 的四个决定

| 问题 | 决定 |
| --- | --- |
| 第一个接入方 | `acuven_aichat`（DECISIONS F-03 的第二个接入方、第一个跨主机接入方；FastAPI + Redis，没有 SQL 库，Python 3.13）。接入本身在它自己的仓库另做，不在本设计内 |
| 本地 outbox 存在哪 | 本地 SQLite 文件，放在容器的持久卷上 |
| 第一版范围 | spec §35 七项全做：建用量事件、本地 outbox、后台投递与重试、请求签名、有效状态对账、状态 webhook 验签助手、健康指标 |
| 包放在哪、怎么装 | 本仓库 `integration-client/` 子目录（公开仓库），接入方用 pip 从 git 安装并钉到某个提交；本仓库 CI 加契约测试，证明客户端的签名与载荷能过服务端 |
| 进程模型（2026-11-05 v19 审查后） | **一个 outbox 文件只给一个进程用**：两个接入方都是单进程 uvicorn。多进程共用文件时，运行中替换损坏文件会碰到 SQLite 关闭旧连接时按路径删 `-wal` / `-shm` 的问题，标准库没有办法绕开。以后要多 worker 时，每个 worker 配自己的 outbox 文件，另过设计闸门 |

### 可观察的完成标准

1. 接入方调用 `record_llm_usage(...)` / `record_quantity_usage(...)` 后，事件在返回前已持久写入本地 SQLite（或在存储故障时进入有界的内存暂存，见 §5），返回 `event_id`；进程重启后未送达的事件仍会送达。
2. 后台投递按批量接口送出，服务端回执为 `accepted` / `already_received` / `already_processed` 的事件标为已送达；按回执的 `retryable` 决定重试或进死信；同一事件每次重试发送**逐字节相同**的事件内容，不会触发 `IDEMPOTENCY_CONFLICT`。
3. 客户端生成的签名能通过服务端 `app/services/integration_auth.py` 的校验；`tests/backend/test_integration_auth.py` 的全零对照向量在客户端得到相同签名。
4. 有效状态：每 240 秒对账一次，失败 30 秒后补一次；只应用经校验、`status_version` 更大的状态；只有经校验的 `BLOCK_AI` 才让 `should_block_ai()` 返回真，查询失败或从未成功时返回假（不变量 1）。
5. 状态 webhook 助手：对平台发来的请求验签、校时间窗、校租户与项目，再按版本规则应用；验签失败抛异常（接入方回非 2xx），旧版本或同版本也算成功（接入方回 2xx）。
6. `health()` 返回积压、死信、最老待发年龄、最近一次送达与对账成功时刻等指标，并给出与服务端 `ai_usage_outbox` 维度同口径的建议级别。
7. 本仓库 CI 跑客户端自己的测试与契约测试，不出现 skipped。

### 明确不做什么

- **不改服务端**：不改任何 `app/` 代码、接口、迁移；`GET /api/v1/integration/health`（spec §38 列了、服务端没有实现）不做，客户端的健康指标全部是本地指标。
- **不做异步（asyncio）接口**：第一版只有同步接口；投递在后台线程里跑。FastAPI 接入方在 async 路由里调用 `record_*` 是一次本地 SQLite 提交（毫秒级），可直接调用或放进线程池，由接入方决定，README 写明。
- **不用 Redis、不用 Celery**：outbox 与状态都在 SQLite；投递是包内的后台线程。
- **不处理对话内容**：事件里没有 prompt / response 字段（不变量 9），客户端也不提供这类字段。
- **不做 OpenAI 转写、会话生命周期等业务逻辑**：客户端只按服务端的计量类型目录发事件；哪些用量算哪种 `usage_type` 由接入方决定。
- **不在本仓库接入 `acuven_aichat`**，也不改它的仓库。
- **不发布到 PyPI**，不建独立仓库。
- **偏离 `docs/api.md` 的一处「客户端该做什么」**：`docs/api.md` 的错误表对 401 `INTEGRATION_AUTH_FAILED` 写的是客户端应进死信；本设计改为保持待发、固定 60 秒重试并报 P1（理由见 §2「投递」与 §9 备选 D）。本设计不改服务端文档，登记时在 `docs/TODO.md`「后续计划」加一项「把 `docs/api.md` 错误表里 401 这一行的客户端建议改成与 Billing Client 一致」。
- **不修服务端文档的两处过时说法**（`docs/api.md` 里「状态 webhook 发送方还没有上线」与「`last_used_at` 总是 null」）：与本设计无关，另开一项。

### 现有行为与证据

- 服务端契约（全部已上线）：签名 `docs/api.md`「集成请求签名」（唯一出处）与 `app/services/integration_auth.py`；单条与批量摄取 `app/api/integration_usage.py`、`app/schemas/usage_ingest.py`、`docs/api.md`「集成用量事件摄取」「批量摄取」；有效状态与对账 `docs/api.md`「有效状态」「对账」；状态 webhook `app/services/status_delivery.py`、`app/services/webhook_signing.py`、`docs/api.md`「状态 webhook」。
- 进程内 AI 模块已经实现了同类链路（`app/ai_api/outbox.py`、`app/ai_api/delivery.py`、`app/ai_api/client_rules.py`），本设计的回执判定与退避照搬它；但它的规范串不做查询串排序、不大写方法（它只发无查询串的 POST），客户端不能直接复用它的签名函数。
- AIH-TASK-029 的设计把死信、退避上限、应用侧告警与人工重投交给 Billing Client（`docs/design/AIH-TASK-029-usage-ingest.md`）；`docs/ARCHITECTURE.md` 写明 integration-client 的目录「在对应 Phase 再定」，本设计定下来。

## 2. 设计概要

### 端到端数据流

```text
接入方调用 record_*（模型调用成功之后）
  → 校验字段（与服务端同一套规则）→ 生成 event_id（ULID）→ 序列化成紧凑 JSON 字节
  → SQLite 事务：INSERT usage_outbox(PENDING) → COMMIT（synchronous=FULL）→ 返回 event_id
后台线程（每进程一个，间隔 10 秒）
  → 领取：BEGIN IMMEDIATE → 选到期的 PENDING 与租约过期的 SENDING → 置 SENDING + 租约 + attempts+1 → COMMIT
  → 组批（≤100 条、≤1 MiB）→ 每次尝试新的 Request-Id 与时间戳 → 签名 → POST /api/v1/integration/usage-events/batch
  → 按回执逐条落状态：DELIVERED / PENDING（退避）/ DEAD → COMMIT
  → 到点对账：GET /api/v1/integration/effective-status → 校验 → 按版本应用到 SQLite 的状态表
接入方的 webhook 路由
  → verify_status_webhook(原始请求) → apply → 回 2xx；验签失败回非 2xx
接入方的模型调用前
  → should_block_ai()（只读 SQLite 状态表，失败放行）
```

### 包的位置与形态

- 目录 `integration-client/`：自己的 `pyproject.toml`（项目名 `acuven-billing-client`，版本 `0.1.0`，`requires-python >= 3.12`，**没有第三方运行时依赖**）、`README.md`（接入指南）、包 `acuven_billing_client/`、测试 `tests/`。
- 只用标准库：`sqlite3`、`urllib.request`、`hmac`、`hashlib`、`json`、`threading`、`secrets`、`decimal`、`datetime`、`logging`。理由：服务端用的是 `httpx2`，两个接入方用的是 `httpx`，不引第三方依赖就不会和任何一方的依赖冲突。
- 安装：接入方在自己的依赖文件里写 pip 的 git 依赖，指向本仓库并钉到一个提交、`subdirectory=integration-client`。版本号随客户端改动递增，写在 `__version__` 与 `pyproject.toml`，两处一致。
- 本仓库的 CI：根 `pyproject.toml` 的 `[tool.pytest.ini_options]` 把 `integration-client/tests` 加进 `testpaths`、把 `integration-client` 加进 `pythonpath`，客户端测试就在现有的 `backend-tests` 分片里跑（受「不许 skipped」约束）；契约测试放在 `tests/backend/`，同时导入客户端与服务端。不改 `.github/workflows/`。
- 镜像不受影响：`Dockerfile` 只复制 `app`、`alembic`。

### 模块与公开接口

| 模块 | 内容 |
| --- | --- |
| `config.py` | `BillingClientConfig`（不可变）：`base_url`、`api_key`、`key_version`（正整数）、`secret`、`tenant_id`、`project_id`（对外 id，用于校验状态应答，必填）、`outbox_path`、`webhook_secrets: dict[int, str]`（可空）、可调参数（见下表）；`from_env(prefix="ACUVEN_BILLING_")` 读 `BASE_URL`、`API_KEY`、`KEY_VERSION`、`API_SECRET`、`TENANT_ID`、`PROJECT_ID`、`OUTBOX_PATH`、`WEBHOOK_SECRETS`（`<版本>:<密钥>` 用逗号分隔）；`__repr__` 与 `__str__` 不含 `secret` 与 webhook 密钥 |
| `ids.py` | `new_ulid()`：48 位毫秒时间 + 80 位随机，26 个大写 Crockford 字符（与 `app/ai_api/client_rules.py` 同算法） |
| `events.py` | 字段校验与序列化（见「事件」） |
| `signing.py` | 规范串、签名、请求头（见「签名」）；`verify_webhook_signature` 也在这里 |
| `store.py` | SQLite 存储（见「存储」） |
| `transport.py` | `Transport` 协议 `send(method, url, headers, body, timeout) -> Response(status, body)`；默认实现用 `urllib.request`，**不跟随重定向**，响应体最多读 64 KiB；测试注入假实现 |
| `delivery.py` | 领取、组批、回执判定、退避、死信（见「投递」） |
| `status.py` | 对账与版本规则（见「有效状态」） |
| `webhook.py` | 状态 webhook 验签与解析（见「状态 webhook」） |
| `metrics.py` | `health()`（见「健康指标」） |
| `client.py` | 门面 `BillingClient(config, *, transport=None, clock=None)`：`record_llm_usage`、`record_quantity_usage`、`start()`、`stop(timeout)`、`deliver_once()`、`reconcile_status_once()`、`should_block_ai()`、`current_status()`、`handle_status_webhook(...)`、`health()`、`requeue_dead(event_id, *, reason)`、`ack_dead(event_id, *, reason)`、`list_dead(limit)`、`replay_delivered(*, delivered_from, delivered_until, reason)`、`list_incidents(limit)`、`ack_incident(incident_id, *, reason)`、`reset_status(*, reason)` |
| `__main__.py` | 运维命令行：`python -m acuven_billing_client --outbox <路径> stats \| dead [--limit N] \| requeue <event_id> --reason <原因> \| ack <event_id> --reason <原因> \| replay --delivered-from <时刻> --delivered-until <时刻> --reason <原因> \| incidents [--limit N] \| ack-incident <incident_id> --reason <原因> \| reset-status --tenant-id <id> --project-id <id> --reason <原因>`（时刻为带时区的 ISO 8601），只读写本地 SQLite，不联网、不需要凭据；文件不存在时退出码 1、不建文件。`stats` 输出一个 JSON 对象：库里能算出的键（`pending`、`sending`、`dead_unacked`、`incidents_unacked`、`oldest_pending_age_seconds`、`auth_failing`、`last_delivery_ok_at`）加 `status` 数组（`status_state` 与 `reconcile_state` 里每一对租户 × 项目一项：`tenant_id`、`project_id`、`effective_status`、`status_version`、`applied_at`、`last_ok_at`、`last_error_at`）；**不输出 `level` 与四个进程内计数**（命令行看不到运行中进程的内存）。`dead` 每行输出一个 JSON 对象（`event_id`、`last_error_code`、`attempts`、`dead_at`、`dead_acked_at`），`--limit` 缺省 50；`incidents` 同样每行一个 JSON 对象（`incident_id`、`kind`、`event_id`、`dropped_count`、`first_occurred_at`、`last_occurred_at`、`recorded_at`、`acked_at`）。`requeue`、`ack`、`replay`、`ack-incident`、`reset-status` 的 `--reason` 必填（去掉首尾空白后 1 到 500 个字符）。退出码：成功 0、目标不存在或状态不对 1、参数错 2、outbox 正被运行中的进程使用（取不到单进程锁）3。**命令行只用于没有进程在用的文件**（停机后的文件、运行中隔离出来的旧文件）；接入方运行时，死信、重放、事故确认与 `reset-status` 等处置一律通过进程内 `BillingClient` 的同名方法完成，由接入方自己暴露一个受保护的入口（README 给写法） |

可调参数（默认值）：投递间隔 10 秒、每批最多 100 条、每批最多 1,000,000 字节（低于服务端 1 MiB）、租约 60 秒、HTTP 超时 10 秒、对账间隔 240 秒、对账失败后补查 30 秒、已送达行保留 7 天（`delivered_retention_seconds`，可配置，**不得小于 1 天**，配置更小时构造配置对象报错；接入方须把它设成不短于中心的灾备重放窗口，spec §21、§98.1）、内存暂存上限 1000 条、待记事故的 `event_id` 上限 10000 个。

### 事件

- 公开函数：`record_llm_usage(*, provider, model, usage_type, input_tokens, output_tokens, cache_creation_input_tokens, cache_read_input_tokens, request_id, occurred_at=None, conversation_id=None, feature=None, event_id=None)` 与 `record_quantity_usage(*, provider, model, usage_type, quantity, unit, request_id, occurred_at=None, conversation_id=None, feature=None, event_id=None)`。四个 token 字段的名字照服务端 `UsageEventPayload`，都是 0 到 10^12 的整数（`bool` 不算整数）。两个函数都只收关键字参数，传了未知参数由 Python 抛 `TypeError`（就是「多了字段」的拒绝方式，不另做 `**kwargs`）。没给的可选字段（`conversation_id`、`feature`）**不出现在载荷里**，不写成 `null`；LLM 事件的载荷里没有 `quantity` 与 `unit`，QUANTITY 事件的载荷里没有四个 token 字段（服务端对形态不符的字段即使为 `null` 也拒绝）。
- 校验规则与服务端 `app/schemas/usage_ingest.py` 一致（正则与范围逐条照抄，`schema_version` 固定 `"1.0"`）；不合规抛 `ValueError`，**不写 outbox**——这是接入方的编程错误，越早暴露越好，写进 outbox 只会在服务端 422 后变成死信。`usage_type` 是否在服务端目录里客户端不知道，不在本地校验（服务端回 `UNKNOWN_USAGE_TYPE` 时可重试，见「投递」）。
- `occurred_at`：必须是带时区的 `datetime`，缺省取当前 UTC；序列化为 RFC 3339 的 UTC 形式（`Z` 结尾，有微秒时保留 6 位）。不在本地判断「未来 300 秒」：由服务端判，回执不可重试则进死信。
- `quantity`：只接受 `Decimal` 或十进制字符串，转成 `^[0-9]{1,12}(\.[0-9]{1,8})?$` 的定点字符串：先转成 `Decimal`、去掉小数部分的尾零（`37.420` → `37.42`，`5.000` → `5`），**再**按正则判位数（所以 `0.000000010` 规范化为 `0.00000001` 后合法），输出不用科学计数法；负数、非有限值、整数部分超过 12 位或规范化后小数超过 8 位抛 `ValueError`；不接受 `float`（不变量 10）。
- 载荷里**不写** `tenant_id` / `project_id`：服务端这两个字段可选、只用于诊断（`app/schemas/usage_ingest.py`），事件归属由签名凭据决定。写进去的话，配置一旦写错，已入库的载荷字节就永远与凭据不符（`CREDENTIAL_SCOPE_MISMATCH`），改对配置也无法恢复。配置里的这两个值只用于校验状态应答与 webhook。
- `event_id`：缺省用 `new_ulid()`。接入方可传自己的 `event_id`（UUIDv7 小写带连字符，或 ULID），用于「同一次模型调用重复记账」的去重：同 `event_id` 已在 outbox 且内容字节相同 → 直接返回该 id，不新增；内容不同 → 抛 `EventConflictError`，不写入（与服务端 409 同义，早一步拦住）。 **传了 `event_id` 就必须显式给出 `occurred_at`**，否则抛 `ValueError`：缺省的 `occurred_at` 取调用时刻，同一个 `event_id` 第二次调用的载荷必然不同，去重会变成冲突；这是编程错误，开发期就暴露，不会在生产的 AI 响应路径上抛 `EventConflictError`。README 写明：只有在接入方确实可能用同一个 `event_id` 传入不同内容时（这是接入方的 bug）才会出现 `EventConflictError`，正常的重复记账与崩溃后重放都是同内容、返回已有 id。
- 序列化：`json.dumps(..., separators=(",", ":"), sort_keys=True, ensure_ascii=False).encode("utf-8")`，单条不超过 16 KiB（超过抛 `ValueError`）。按服务端的字段规则，所有字符串字段都只含 ASCII，所以服务端按紧凑重新序列化判 16 KiB 时长度与这里相同。**这串字节写进 outbox 后不再改动**，之后每次重试都原样发送。

### 签名

- 规范串五行（`\n` 连接、末尾无换行）：大写方法、规范化的路径与查询串、时间戳、请求 id、请求体 SHA-256 小写十六进制；规则逐条照 `docs/api.md`「集成请求签名」（路径原样，查询串按键、值、整段的 UTF-8 字节序排序，没有查询串时不带 `?`）。
- 签名 = `HMAC-SHA256(secret 的 UTF-8 字节, 规范串的 UTF-8 字节)`，小写十六进制。
- 请求头：`X-Acuven-Api-Key`、`X-Acuven-Key-Version`（十进制、不补零）、`X-Acuven-Timestamp`（每次尝试取 `int(clock())`，`clock` 是 `BillingClient` 注入的时钟，缺省 `time.time`）、`X-Acuven-Request-Id`（每次尝试新的 ULID）、`X-Acuven-Signature`，另加 `Content-Type: application/json`（有请求体时）。
- 状态 webhook 验签用同一个规范串函数，第四行换成 `X-Acuven-Event-ID`。`signing.verify_webhook_signature(*, secrets, key_version, method, path_and_query, timestamp, event_id, body, signature) -> bool` 只做「按版本取密钥、算签名、常量时间比较」，版本不在 `secrets` 里返回假；请求头解析、时间窗、载荷校验都在 `webhook.py`。

### 存储（SQLite）

- 文件路径来自配置；目录不存在时报错（不替接入方建目录，免得写到容器的临时层）。每次连接设置 `journal_mode=WAL`、`synchronous=FULL`、`busy_timeout=5000`、`foreign_keys=ON`。每个线程各用自己的连接。
- **构造时存储不可用 → 降级运行**（不变量 1、spec §20）：`BillingClient` 构造时正常打开失败（目录不存在、文件损坏或不是 SQLite、`schema_version` 比代码新、磁盘满导致 `-wal` / `-shm` 建不出来、其他 `sqlite3.Error` 或 `OSError`）**不抛异常**，照样构造出客户端，处于降级状态：`store_available` 为假；`record_*` 校验后直接进内存暂存（规则与运行中存储故障相同，暂存满则丢弃并进待记事故）；`should_block_ai()` 返回假、`current_status()` 返回 `None`；死信运维、重放等处置函数抛 `StoreUnavailableError`；投递与对账不进行；`health().level = P1`。投递线程每个间隔先尝试重新打开（**例外：因文件损坏而降级时不重开同一个文件**——转入降级的错误是 `SQLITE_CORRUPT` 或 `SQLITE_NOTADB` 时，记下当时那个文件的身份 `(st_dev, st_ino)` 为「坏文件」，之后只有路径上的文件身份与它不同（运维已换走）或路径不存在时才重新打开；否则保持降级、不打开、不回灌，免得部分损坏的文件能打开、一写又坏而来回切换，并把暂存写进正要隔离的文件。磁盘满、I/O 错误、被锁、版本更新、残留文件等其他原因照常每个间隔重试）；`deliver_once()` 与 `reconcile_status_once()` 在降级时也先尝试重新打开（只用这两个函数、不调 `start()` 的接入方同样能恢复）。打开成功即 `store_available` 为真，随后照常回灌暂存、写待记事故、投递与对账。打开失败的原因记一条 ERROR 日志（只记异常类型与文件路径，不记文件内容）。只有配置本身不合法（`BillingClientConfig` 校验）才在构造时抛错。
- **运行中转入降级**：运行中任何一次读写库抛出 `IntegrityError` 以外、且 `sqlite_errorname` 不是 `SQLITE_BUSY` / `SQLITE_LOCKED`（锁等待超时，属暂时性）的 `sqlite3.Error` 或 `OSError`（例如 `SQLITE_CORRUPT`、`SQLITE_NOTADB`、`SQLITE_IOERR*`、`SQLITE_FULL`、`SQLITE_CANTOPEN`、`SQLITE_READONLY*`），客户端作废全部连接（见下一条「连接作废与文件替换检测」），转入与构造时打开失败**同一套**降级状态（`store_available` 为假、记账进暂存、按上面的规则重新打开）；正在进行的那次调用照原来的规则处理（`record_*` 进暂存、投递一轮中止）。`SQLITE_BUSY` / `SQLITE_LOCKED` 不转降级：`record_*` 照旧进暂存，下一轮回灌再试。这样运行中出现的损坏也能用下面「运行中隔离旧文件」的步骤恢复。
- **单进程锁**：正常打开时先在 `<path>.lock` 上取**排他的系统级文件锁**（POSIX 用 `fcntl.flock(LOCK_EX | LOCK_NB)`，Windows 用 `msvcrt.locking`），进程存活期间一直持有；取不到（另一个进程正在用这个 outbox）就抛 `StoreLockedError`，`BillingClient` 按「构造时存储不可用」转入降级并在 ERROR 日志里写明原因，每个间隔再试。锁文件只用于加锁，不写内容、不删除。
- **连接登记与作废**：本进程的全部连接都以 `check_same_thread=False` 建立并登记在 `Store` 的登记表里；每次使用连接时持有登记表的**共享锁**，`invalidate()` 取**排他锁**——等正在使用的调用结束后，逐个关闭登记表里的全部连接并清空登记表，连接代号加一。各线程下次使用时发现自己的连接已不在登记表（或代号不同），就重新建立（降级中则不建、按降级处理）。**重新打开、包括新建库，只在 `invalidate()` 完成之后进行**，所以新库建好时本进程已没有任何旧连接；又因为单进程锁保证没有别的进程打开这个文件，不会出现旧连接关闭时按路径删掉新库 `-wal` / `-shm` 的情况。
- **文件替换检测**：每个连接记下建立时所打开文件的身份（`os.stat` 的 `(st_dev, st_ino)`）。**每次**用连接前都比对路径当前的文件身份：不同或路径不存在 → 说明文件被移走或替换（运维的运行中隔离），按「运行中转入降级」处理：`invalidate()`，再重新打开路径上的文件。**新建库的前提**：只有 `<path>`、`<path>-wal`、`<path>-shm` 三个都不存在时才新建；主库不在而 `-wal` 或 `-shm` 还在，不新建、不碰这两个文件，保持降级并记 ERROR（只记哪几个文件还在），下一个间隔再试，否则新库会与旧 WAL 配错对。**注意 SQLite 关闭连接的行为**：主库被移走或替换后，SQLite 关闭连接时会发现文件已移动（`SQLITE_FCNTL_HAS_MOVED`），**跳过关闭时的检查点，也不删除 `-wal` / `-shm`**。所以 WAL 里已提交但未检查点的内容只留在原路径的 `-wal` 里，被移走的主库里没有。因此：运维必须把主库、`-wal`、`-shm` **三个文件一起**移走，并保持配对的名字（例如 `x.db`、`x.db-wal`、`x.db-shm`）；只移走主库时，原路径上的 `-wal` / `-shm` 会让客户端按上一句保持降级、ERROR 日志点名残留文件，运维随后要把它们按配对名字放到被隔离的主库旁边，命令行读取时才能看到 WAL 里的事件。
- **损坏文件的恢复**（README 写明）：**不要停接入方**——降级期间的用量只在内存暂存与待记事故里，停进程就全部丢失。降级状态下客户端并没有打开这个文件，所以可以在进程运行中把旧 outbox 文件**连同 `-wal`、`-shm` 三个一起**移到别处隔离保存、不删除，并保持配对的名字（`x.db`、`x.db-wal`、`x.db-shm`；WAL 里可能有已提交但没检查点的事件，只移主库会把它们和主库分开）；三个都移走后客户端才会新建库；下一个投递间隔（或下一次 `deliver_once()`）重新打开时会建出新的空文件，`store_available` 变真，暂存回灌入库、待记事故写进 `incidents`。等 `health()` 里 `held_in_memory` 与 `incidents_pending` 都归零后，再对隔离的旧文件用 `python -m acuven_billing_client --outbox <旧文件> stats` / `dead` / `incidents` 尝试读出还没送达的事件与未确认事故；读不出或读出的未送达事件，作为 spec §20 的人工对账事故处理（spec §20 明确接受这种罕见的本地持久化故障不能自动恢复）。客户端不提供把旧文件里的事件导入新文件的命令。
- **不建表的打开方式**（命令行用）：文件不存在、零字节、不是 SQLite 文件、没有 `meta` 表或 `schema_version` 比代码新时报错，**不建表、不改日志模式、不写任何东西**；**同样先取单进程锁**（取不到抛 `StoreLockedError`，命令行退出 3），取到之后其余与正常打开相同。正常打开（`BillingClient` 用）才会建表与设置 WAL。
- 表 `meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)`：`schema_version = 1`。打开时库里版本比代码新就报错、不动数据（防止旧版客户端写坏新格式）。
- 表 `usage_outbox`：

| 列 | 类型 | 说明 |
| --- | --- | --- |
| `event_id` | TEXT PRIMARY KEY | |
| `payload` | BLOB NOT NULL | 序列化后的事件字节，写入后不改 |
| `status` | TEXT NOT NULL CHECK (`PENDING`、`SENDING`、`DELIVERED`、`DEAD`) | |
| `attempts` | INTEGER NOT NULL DEFAULT 0 | 每次领取 +1 |
| `next_attempt_at` | REAL NOT NULL | Unix 秒；`PENDING` 时到点才领 |
| `lease_until` | REAL | `SENDING` 时的租约到期 |
| `last_error_code` | TEXT | 最近一次失败的码（服务端 `error_code` 或本地码），不存响应体 |
| `created_at` | REAL NOT NULL | |
| `delivered_at` | REAL | |
| `dead_at` | REAL | |
| `dead_acked_at` | REAL | 运维确认过的死信 |

  索引：`(status, next_attempt_at)`。
- 表 `status_state`：**主键 `(tenant_id, project_id)`**（都是 TEXT NOT NULL），其余列为有效状态应答的另外 8 个字段与 `applied_at`：`billing_mode`、`billing_status`、`account_status`、`project_integration_status`、`effective_at` 为 TEXT NOT NULL，`effective_status` 为 TEXT NOT NULL CHECK（`ALLOW_AI`、`BLOCK_AI`），`status_version` 为 INTEGER NOT NULL，`reason_code` 为 TEXT（可空），`applied_at` 为 REAL NOT NULL。**行只在第一次应用状态时插入**，所以「有这一对的行」就等于「有已应用的状态」。替换时这 9 列整体覆盖（`applied_at` 写本次时刻）。所有读写都带配置里的 `tenant_id`、`project_id` 作条件，只认这一对：outbox 文件换了租户或项目时，旧那一对的行留在表里但永远不被读到，新项目从「没有状态」开始。
- 表 `ops_log`：**只增不改**的处置记录（spec §31「每一次重放、放弃或人工对账都要有原因与审计记录」）。列 `id` INTEGER PRIMARY KEY AUTOINCREMENT、`action` TEXT NOT NULL CHECK（`REQUEUE_DEAD`、`ACK_DEAD`、`REPLAY_DELIVERED`、`ACK_INCIDENT`、`RESET_STATUS`）、`target_id` TEXT NOT NULL（前三种动作是 `event_id`，`ACK_INCIDENT` 是 `incident_id`，`RESET_STATUS` 是 `<tenant_id>/<project_id>`）、`reason` TEXT NOT NULL、`at` REAL NOT NULL。每一次处置与它改动的行**在同一个事务里**写一行（重放一个时间段时每个被改回的事件各一行）。客户端代码里没有任何更新或删除 `ops_log` 的路径。
- 表 `incidents`：人工对账事故（spec §20：本地写不进 outbox 的事件，存储恢复后要有可审计的人工对账事故记录）。列 `incident_id` TEXT PRIMARY KEY（每次记录都生成新的：`inc_` 加一个新 ULID）、`kind` TEXT NOT NULL CHECK（`DROPPED`：暂存已满而丢弃；`CONFLICT_DROPPED`：回灌时同 id 不同内容而丢弃；`OVERFLOW_DROPPED`：待记事故列表也满了之后又丢弃的若干条，只知道条数）、`event_id` TEXT（前两种为被丢弃事件的 `event_id`；`OVERFLOW_DROPPED` 为空）、`dropped_count` INTEGER NOT NULL（前两种为 1）、`first_occurred_at` REAL NOT NULL、`last_occurred_at` REAL NOT NULL、`recorded_at` REAL NOT NULL、`acked_at` REAL、`ack_reason` TEXT。去重只针对**未确认**的事故：同一个 `event_id`、同一种 `kind` 已有一行 `acked_at` 为空的事故时不再插入；已确认的不算，所以同一个 `event_id`（接入方自带的 id 可能重复使用）在前一条事故确认之后再次被丢弃，会记一条新事故、重新报 P1。`OVERFLOW_DROPPED` 不去重。只记 id 与条数，不记载荷。
- 表 `reconcile_state`：主键 `(tenant_id, project_id)`，列 `last_ok_at`、`last_error_at`（都是 REAL，可空）。对账成功与失败只写这张表，**不碰 `status_state`**；webhook 不写这张表。这样第一次对账就失败也不会产生一行没有版本的状态，webhook 也不会冒充对账成功。
- 投递成功时刻 `last_delivery_ok_at` 存在 `meta` 表（键 `last_delivery_ok_at`），与租户、项目无关。
- 写入事件：`BEGIN IMMEDIATE`（先拿写锁，多个线程同时写同一个 `event_id` 时不会两边都查到「不存在」，也不会在读后升级写锁时得到 `SQLITE_BUSY_SNAPSHOT`）→ 查同 `event_id` → 不存在则 `INSERT` → `COMMIT` 后才返回。
- **主键冲突与存储故障分开**：即使有写锁，`INSERT` 仍可能因主键冲突抛 `sqlite3.IntegrityError`（例如别的工具写过同一文件）；它按「事件」的去重规则处理：读出已有行的 `payload`，字节相同 → 返回已有 id；不同 → 抛 `EventConflictError`。只有 `IntegrityError` 以外的 `sqlite3.Error`（磁盘满、文件损坏、`busy_timeout` 到期仍拿不到锁等）才算存储故障、进内存暂存（§5）。
- **暂存回灌**：后台线程每轮开头逐条把暂存写回 SQLite，规则同上：写入成功或主键冲突且字节相同 → 视为已入库、移出暂存；主键冲突且字节不同 → 移出暂存并丢弃，记 ERROR 日志（只带 `event_id`），`conflicts_dropped` 加 1，并把 `(event_id, CONFLICT_DROPPED, 时刻)` 放进「待记事故」列表；仍是存储故障 → 留在暂存、下一轮再试，并停止本轮回灌（存储整体不可用时不必逐条撞）。
- **待记事故**：暂存已满而丢弃的事件，把 `(event_id, DROPPED, 时刻)` 放进同一个进程内列表（上限 10000 个）。列表也满了时不再记 id，只累加一个**溢出计数**并记下溢出的首次与最近时刻、记 ERROR。后台线程每轮回灌之后，在存储可用时把列表与溢出计数**在同一个事务里**写进 `incidents` 表（溢出计数大于 0 时写一行 `OVERFLOW_DROPPED`，`dropped_count` 为该计数），写入前在锁内把列表与溢出计数（条数与首次、最近时刻）整体**移入一份待写快照**并把进程内的列表与计数清零，写成功就丢掉快照，写失败再并回去：先放快照里的条目、再放写库期间新增的，超出列表上限（10000）的 id 转为溢出计数：计入条数，并用它的时刻更新溢出的首次与最近时刻（不再保留 id）；两段溢出合并时条数相加、首次时刻取较早者、最近时刻取较晚者；写库期间新发生的丢弃从零开始累计，下一次写成自己的一行，起止时刻不会与前一行混在一起。同一时刻只有一处在写（专用锁）。所以存储恢复后 `incidents_pending` 一定回到 0，溢出的条数也有了持久记录。事故在 `incidents` 里 `acked_at` 为空就一直让 `health().level = P1`，直到运维用 `ack_incident`（必填原因，同一事务写 `ops_log`）确认——进程重启也不会让告警消失。

### 投递

- **领取**（`BEGIN IMMEDIATE`，SQLite 的写锁保证同一时刻只有一个连接在领取）：按 `(created_at, event_id)` 升序选 `status = PENDING AND next_attempt_at <= now` 或 `status = SENDING AND lease_until < now`（领了没回来的，例如进程崩溃）的行，逐条累加直到 100 条或下一条会让批量请求体超过 1,000,000 字节；置 `SENDING`、`lease_until = now + 60`、`attempts + 1`；`COMMIT`。本进程的多个线程（后台线程与 `deliver_once()`）靠这把写锁与租约不重复领取；进程崩溃后重启、租约过期被重领导致重复发送是安全的（服务端按 `event_id` 幂等，回 `already_*`）。
- **请求体**：`{"events":[` + 各条 `payload` 原样用 `,` 连接 + `]}`，不重新序列化。
- **地址与签名路径**：`base_url` 去掉末尾的 `/` 后直接拼接口路径（例如 `/api/v1/integration/usage-events/batch`）；`base_url` 自带的路径前缀保留。签名用的路径是实际请求 URL 的路径部分（含前缀），没有查询串。
- **什么算错误信封**：响应体是 JSON 对象，含对象 `error`（其中 `code` 是字符串）与顶层布尔 `retryable`（`docs/api.md`「错误与 `retryable`」）。缺任何一项都按「没有信封」处理。
- **回执判定**（照 `app/ai_api/delivery.py` 与 `docs/api.md` 的错误信封；`retryable` 只看信封里的布尔值，不从状态码推断）。**「与事件内容有关、可进死信」的错误码是一个白名单**，取自 `docs/api.md` 单条摄取错误表中 `retryable = false` 且由事件内容决定的码：`VALIDATION_ERROR`、`UNSUPPORTED_SCHEMA_VERSION`、`OCCURRED_AT_IN_FUTURE`、`IDEMPOTENCY_CONFLICT`、`PAYLOAD_TOO_LARGE`、`CREDENTIAL_SCOPE_MISMATCH`。只有逐条结果或单条接口回执里 `retryable = false` 且码在白名单里，才进 `DEAD`；`retryable = false` 而码不在白名单里（例如 `base_url` 配错得到框架的 404 / 405、服务端以后新增的码）一律 `PENDING` 退避并记下该码，不进死信——不能判断是事件本身的问题，就不丢给死信（与 §9 备选 D 同一理由）：

| 情形 | 判定 |
| --- | --- |
| 200，`results` 的长度与发出的条数相同，且每条的 `index` 与 `event_id` 对得上 | 逐条：`status` 为 `accepted` / `already_received` / `already_processed` → `DELIVERED`；`rejected` 且 `retryable` 为假、码在白名单 → `DEAD`；其余 `rejected`（可重试，或码不在白名单）→ `PENDING` 退避；某一条的 `status` 缺失或不是这四个之一、或 `rejected` 却没有布尔 `retryable` 或字符串 `error_code` → 只这一条 `PENDING` 退避、`last_error_code = INVALID_BILLING_RECEIPT`；`last_error_code` 记该条的 `error_code` |
| 200 但结构不对：响应不是对象、没有 `data.results` 数组、长度与发出的条数不同、任何一条缺 `index` 或 `index` / `event_id` 与发出的位置对不上 | 整批 `PENDING` 退避，`last_error_code = INVALID_BILLING_RECEIPT` |
| 非 200，信封有布尔 `retryable = true`，或状态码 ≥ 500 且没有信封 | 整批 `PENDING` 退避，记信封的 `error_code`（没有就记 `BILLING_UNAVAILABLE`） |
| 401 `INTEGRATION_AUTH_FAILED` | 整批 `PENDING`，**不进死信**：这是凭据问题（密钥、版本、时钟），事件本身没错，每次发送都用当前配置重新签名，所以改对配置就能恢复；按下面「凭据错误的重试」用固定 60 秒间隔，不按指数退避；`health()` 的 `auth_failing` 因此为真（见「健康指标」） |
| 批层面的 413 `PAYLOAD_TOO_LARGE`、422 `VALIDATION_ERROR` 或 422 `BATCH_TOO_LARGE`（`retryable = false`；批量请求体本身的问题，可能由其中某一条引起） | 整批不落死信，改为**同一轮里逐条用单条接口重发**，再按单条回执判定（单条的 `retryable = false` → `DEAD`）。单发途中每发一条之前，把这批里**还没落状态的行**的租约统一续到 `now + 60`（同一个栅栏条件：`status = SENDING AND lease_until = 上次写下的值`，续约后记下新值）；续约影响的行数少于预期，说明有行已被别人重领，立即停止回退，本进程不再处理这批剩下的行 |
| 其他非 200 且 `retryable = false`（码不是上面三种，例如框架的 404 / 405） | 整批 `PENDING` 退避，记信封的 `error_code`；不改单发、不进死信 |
| 非 200、没有信封、状态码 < 500 | 整批 `PENDING` 退避，`last_error_code = INVALID_BILLING_RESPONSE`（不进死信：无法判断是不是事件本身的问题） |
| 网络错误、超时、重定向 | 整批 `PENDING` 退避，`last_error_code = NETWORK_ERROR` |

  单条接口的回执：202 `accepted`、200 `already_received` / `already_processed`（回执里的 `event_id` 必须等于发出的）→ `DELIVERED`；200 / 202 但 `event_id` 对不上或 `status` 不是这三个之一 → 该条 `PENDING` 退避、`last_error_code = INVALID_BILLING_RECEIPT`；其余按上表同样的信封规则。单发回退途中，某一条得到的结果**与这条事件无关**（网络错误、`retryable = true` 且码不是 `UNKNOWN_USAGE_TYPE` 的信封、401、`retryable = false` 但码不在白名单、无信封的非 200）时立即停止回退：这一条与剩下还没发的行都按该结果落状态（同一个错误码与退避规则）；与这条事件有关的结果（送达、码在白名单的不可重试进死信、`UNKNOWN_USAGE_TYPE`（可重试，但只取决于这一条的 `usage_type`）退避、`INVALID_BILLING_RECEIPT`）只落这一条，继续下一条。续租不足而放弃的行不落状态，在本轮摘要里计入「栅栏失效」。
- 载荷不带租户与项目，所以 `CREDENTIAL_SCOPE_MISMATCH`（逐条或单条接口的 403）不会由客户端配置引起；万一出现，按信封的 `retryable`（为假）进 `DEAD`、记 ERROR，由运维排查后 `requeue_dead` 或 `ack_dead`，不特殊处理。
- **凭据错误的重试**：`last_error_code` 为 `INTEGRATION_AUTH_FAILED` 的行，`next_attempt_at = now + 60`（`attempts` 照常加 1）。凭据修好、进程重启后最多约 60 秒这些行就会重发并送达，`auth_failing` 随之消失（送达时清掉 `last_error_code`）；不用指数退避，否则修好凭据后要等最长一小时才恢复、P1 也挂这么久。
- **退避**：`next_attempt_at = now + min(3600, 2 ** min(attempts, 12)) × uniform(0.5, 1.0)` 秒；可重试的情况**没有放弃上限**（用量不能丢，spec §35）。
- **死信**：只有服务端明确说这一条不可重试、且码在上面的白名单里（逐条回执或单条接口）才进 `DEAD`；记一条 ERROR 日志（只带 `event_id` 与 `error_code`）。运维用 `requeue_dead` 改回 `PENDING`、`attempts = 0`、`next_attempt_at = now`、清掉 `last_error_code`（服务端那边原因修好后重投），或 `ack_dead` 记 `dead_acked_at`（确认放弃，保留行）。
- `mark_delivered` 同时清掉 `last_error_code`；领取不改它（保留上一次失败的原因供指标与排查）。
- **清理**：每轮投递顺带删除 `delivered_at` 早于 `delivered_retention_seconds`（缺省 7 天）的行（单次最多 1000 行）。`DEAD` 行不自动删。
- **中心恢复后的重放**（spec §98.1）：中心从备份恢复后，恢复点之后它已回 202 的事件可能丢了。运维用 `replay_delivered(delivered_from, delivered_until, reason)`（命令行 `replay`）把 `delivered_at` 落在 `[delivered_from, delivered_until)` 的 `DELIVERED` 行改回 `PENDING`（`attempts = 0`、`next_attempt_at = now`、清 `delivered_at` 与 `last_error_code`），每行在同一事务里写一条 `REPLAY_DELIVERED` 的 `ops_log`；返回改回的条数。载荷与 `event_id` 不变，服务端按 `event_id` 幂等，没丢的事件回 `already_*`，丢了的重新入账。时间段的起点应早于中心的恢复点，README 写明。`delivered_from >= delivered_until` 报错。
- **后台线程**：`start()` 启动**两个**守护线程，共用一个停止事件：**投递线程**每个间隔循环一次「暂存回灌（见 §5）→ 投递一轮（一次领取、一批）→ 等到下一次间隔或 `stop` 事件」；**对账线程**按「有效状态」的节奏循环「对账一次 → 等到下一次对账时刻或 `stop` 事件」。两者分开，是为了投递一轮再慢（批层不可重试改单发最坏约 100 × 10 秒）也不推迟对账，有效状态的节奏与「最多滞后约 4 分 50 秒」才成立。`start()` 后两个线程的第一轮都立即执行；任一线程一轮里的任何异常都捕获、记 ERROR 日志、继续下一轮，线程不退出。`stop(timeout)` 设停止事件并等两个线程结束；正在发送的那批租约到期后会被重领。`deliver_once()` 与 `reconcile_status_once()` 供测试与不想要后台线程的接入方用。

### 有效状态

- `reconcile_status_once()`：签名 `GET /api/v1/integration/effective-status`（空请求体），超时 10 秒。
- 校验应答：200；`data` 是对象，**含**服务端的 10 个字段且类型对（多出的字段忽略、不存：`docs/api.md` 写明这个接口只增加字段来演进，多一个字段就拒会让钉在旧提交上的接入方全部对账失败）；`tenant_id`、`project_id` 等于配置值；`status_version` 是非负整数；`effective_status` 只能是 `ALLOW_AI` / `BLOCK_AI`。任何一项不过 → 视为对账失败，不应用、记 WARNING，写 `reconcile_state.last_error_at`。
- 应用（与状态 webhook 共用的 `apply_status`）：在 `BEGIN IMMEDIATE` 里读 `status_state` 中配置的那一对的行，还没有行就插入，`status_version` 大于已存的才整行替换；相等或更小不改；返回是否改了。`apply_status` **只管版本规则**，不写对账时刻。
- 对账成功（应答通过校验，不论 `apply_status` 是否改了）由对账代码另写 `reconcile_state.last_ok_at`；webhook 应用不写它，所以 `status_reconcile_stale` 只反映轮询是否正常。
- 节奏（照 `docs/api.md`「对账」）：`start()` 后立即查第一次；成功后 240 秒再查；**一段连续失败只补查一次**：这段失败的第一次之后 30 秒再查，之后每次失败都等 240 秒，直到某次成功，下一段连续失败再给一次 30 秒补查。
- **状态重置**（中心从备份恢复后）：恢复会让 `status_version` 回到较小的值，按版本规则客户端会一直忽略它们，若本地存的是恢复前的 `BLOCK_AI` 就会一直拦截 AI（违反不变量 1）。运维用 `reset_status(reason)`（命令行 `reset-status`，必填原因）删掉配置那一对（命令行可指定任一对）的 `status_state` 行，同一事务写一条 `RESET_STATUS` 的 `ops_log`；删除后 `should_block_ai()` 立即为假，下一次对账或 webhook 按「还没有行」直接应用。`reconcile_state` 不动。README 把它写进中心恢复后的步骤，与 `replay` 并列。
- `should_block_ai()`：读 `status_state` 中配置的那一对的行；只有存在该行且 `effective_status = BLOCK_AI` 才返回真。读库失败或没有该行（从未应用过任何状态，或已被 `reset_status` 删掉）都返回假，并记 WARNING（不变量 1：计费侧故障不中断 AI）。是否「对账成功过」不影响它：webhook 应用的 `BLOCK_AI` 在对账一直失败时同样生效。`current_status()` 返回已应用的状态（没有或读库失败则 `None`，不抛）。

### 状态 webhook

- `handle_status_webhook(*, method, raw_path, query_string, headers, body, signed_path=None)`：`signed_path` 给出时（一个已经带查询串的字符串），用它代替 `raw_path` + `query_string` 参与规范串的第二行（仍按查询串规则排序）；用于接入方在会改写路径的反向代理后面（例如剥掉前缀），此时应传平台配置里 webhook URL 的路径与查询串。
  1. 取 `X-Acuven-Timestamp`、`X-Acuven-Signature`、`X-Acuven-Event-ID`、`X-Acuven-Key-Version`（大小写不敏感）；缺任何一个、格式不对 → `WebhookVerificationError`。
  2. 时间戳格式同请求签名，`|now − ts| > 300` → 错误。
  3. 用 `webhook_secrets[key_version]` 按规范串（第四行是 Event-ID）计算签名，`hmac.compare_digest` 比较；版本不在配置里或不等 → 错误。轮换期间配置里放新旧两个版本即可两者都认。
  4. 请求体按 JSON 解析：是对象且**含** 11 个字段（有效状态的 10 个加 `event_id`）、类型对（多出的字段忽略，理由同对账），`event_id` 等于请求头的 Event-ID，租户与项目等于配置值；否则错误。
  5. 按上面的版本规则应用，返回 `applied: bool`（旧版本或同版本为假，但**不是错误**）。验签通过而写库抛 `sqlite3.Error` 时抛 `StatusStoreError`（不是 `WebhookVerificationError`），接入方回 503，平台按退避重发。
- 接入方的路由：成功（不论是否应用）回 2xx；`WebhookVerificationError` 回 401；`StatusStoreError` 回 503。README 给一个 FastAPI 的十行示例（取 `request.scope["raw_path"]`、`request.scope["query_string"]` 与原始请求体），并写明：代理改写了路径时要传 `signed_path`，否则每次推送都会验签失败、状态只能靠对账（最多滞后约 4 分 50 秒）。
- `webhook_secrets` 为空时调用 → `WebhookVerificationError`（没配就不接受）。

### 健康指标

`health()` 返回一个字典（键固定，值都是 JSON 可序列化的）：`store_available`（布尔，见「存储」的降级运行；为假时的取值见本节末尾）、`pending`、`sending`、`dead_unacked`、`incidents_unacked`（`incidents` 表里 `acked_at` 为空的条数）、`oldest_pending_age_seconds`（`now` 减去 `PENDING` 与 `SENDING` 行里最早的 `created_at`，含租约已过期的 `SENDING`；没有这类行则 `null`）、`held_in_memory`、`incidents_pending`（进程内还没写进 `incidents` 的待记事故条数加溢出计数；存储可用后的下一轮回灌就会写进库并归零）、`dropped_events`（自进程启动的累计数）、`conflicts_dropped`（自进程启动的累计数，见「存储」的暂存回灌）、`last_delivery_ok_at`、`last_status_reconcile_ok_at`（`reconcile_state.last_ok_at`）、`status_reconcile_stale`（最近一次对账成功早于 300 秒或从未成功）、`auth_failing`（从库里算：存在 `status` 为 `PENDING` 或 `SENDING` 且 `last_error_code` 为 `INTEGRATION_AUTH_FAILED` 的行；不放在进程内存里，重启后与命令行 `stats` 都看得到）、`level`。`level` 的判定与服务端 `ai_usage_outbox` 维度同口径：`store_available` 为假 或 `dead_unacked > 0` 或 `incidents_unacked > 0` 或 `incidents_pending > 0` 或 `held_in_memory > 0` 或 `auth_failing` → `P1`（`held_in_memory > 0` 说明存储正在故障、有用量只在内存里，此时重启会无记录地丢失，spec §20 要求最高级告警）（`incidents_pending` 是还没写进 `incidents` 表的待记事故条数，含列表满了只计数的；`dropped_events` 与 `conflicts_dropped` 是进程累计数，只作展示、**不参与** `level`，否则事故全部确认后 P1 仍要等重启才消失）；`oldest_pending_age_seconds > 900` 或 `status_reconcile_stale` → `P2`；否则 `OK`。读库失败 → `level = P1`，不抛异常。**两种情况（`store_available` 为假、读库失败）统一处理**：从库里算出的键（`pending`、`sending`、`dead_unacked`、`incidents_unacked`、`oldest_pending_age_seconds`、`last_delivery_ok_at`、`last_status_reconcile_ok_at`、`status_reconcile_stale`、`auth_failing`）为 `null`；`store_available` 与四个进程内计数（`held_in_memory`、`incidents_pending`、`dropped_events`、`conflicts_dropped`）照实返回；`level` 照常计算（此时一定是 P1）。`health()` 与命令行 JSON 里的时刻（`last_delivery_ok_at`、`last_status_reconcile_ok_at`、`applied_at`、`last_ok_at`、`last_error_at`、`dead_at`、`dead_acked_at`、`recorded_at`、`acked_at`）都是 Unix 秒（JSON 数字），与库里存的一致；没有值时为 `null`。`held_in_memory`、`incidents_pending`、`dropped_events`、`conflicts_dropped` 四个进程内计数也作为 `BillingClient` 的只读属性提供（AIH-TASK-168 实现，`health()` 在 AIH-TASK-170 汇总）。

### 事务边界

- 写事件：一个 `BEGIN IMMEDIATE` 事务（查重 + 插入），提交后返回。
- 领取、落回执、应用状态：各自一个 `BEGIN IMMEDIATE` 事务。**HTTP 请求不在任何事务里**：领取提交后才发送，回来再开新事务落状态。
- 与接入方自己的数据库没有事务关系：「模型调用成功」与「写 outbox」之间进程崩溃会丢这一次用量（接入方先调模型、后记账）。这是接入方侧的已知窗口，README 写明；用接入方自己的 `event_id` 去重可以在重放时补记而不重复（见「事件」）。

### 时间语义

- `occurred_at`：用量发生时刻，由接入方给（缺省为调用 `record_*` 的时刻），UTC。
- 签名时间戳：每次发送的当前时刻，与 `occurred_at` 无关；接入方主机须做 NTP 同步（spec §37），偏差超过 300 秒时服务端回 401，客户端表现为 `auth_failing`。
- outbox 里的时刻都是 Unix 秒（REAL），只用于本地调度与指标。

### 金额与精度

客户端不处理金额；`quantity` 用 `Decimal` 与定点字符串，不经 `float`。

## 3. 不变量影响矩阵

| 不变量 | 是否触碰 | 可能怎样被破坏 | 设计控制 | 验证方式 |
| --- | --- | --- | --- | --- |
| INV-1 中心故障不中断 AI | 是 | 平台不可用时 `record_*` 阻塞或抛错；对账失败时误拦 AI | `record_*` 只写本地 SQLite，不联网；存储故障进内存暂存不抛（§5），构造时存储不可用也降级运行而不抛；`should_block_ai()` 只在经校验的 `BLOCK_AI` 时为真，失败放行 | §7「平台不可用」「存储故障」「对账失败放行」 |
| INV-2 事件不重复扣费 | 是 | 重试时内容变了 → 409 后进死信，或被当成两条 | `event_id` 生成一次、载荷字节写入后不改，每次重试原样发；服务端按 `event_id` 幂等 | §7「重试载荷逐字节相同」「租约过期重领」 |
| INV-3 支付不重复入账 | 否 | 客户端不碰支付 | — | — |
| INV-4 余额只经账本变动 | 否 | 客户端不碰钱包；入账在服务端 | — | — |
| INV-5 历史账本不可变 | 否 | 同上 | — | — |
| INV-6 事件保留版本引用 | 否 | 定价与汇率版本由服务端在入账时绑定 | — | — |
| INV-7 客户不可见成本毛利 | 否 | 客户端只看到有效状态，应答里没有成本字段 | 客户端只读取并保存服务端的 10 个已知字段，多出的字段丢弃不存；应答里本来就没有成本字段 | §7「状态应答字段」 |
| INV-8 租户不可互访 | 是 | 配置错了把别的项目的状态当成自己的 | 状态应答与 webhook 都核对配置的 `tenant_id`、`project_id`；事件不带租户与项目，归属完全由服务端按签名凭据决定 | §7「租户或项目不符」 |
| INV-9 对话内容不入库 | 是 | 事件里夹带对话内容 | 事件字段固定、`extra` 不允许，没有任何文本内容字段；日志不记载荷 | §7「未知字段被拒」 |
| INV-10 金额用 Decimal | 是（数量） | `quantity` 经 `float` 丢精度 | 只收 `Decimal` 或字符串，`float` 抛 `ValueError` | §7「数量精度」 |
| INV-11 event_id 至多一次财务效果 | 是 | 同上 INV-2 | 同上；本地对同 `event_id` 不同内容直接拒绝 | §7「本地去重与冲突」 |
| INV-12 定稿对账单不可变 | 否 | 不碰对账单 | — | — |
| INV-13 状态与事件原子提交 | 是（本地） | 状态改了但租约、尝试次数没改，或反过来 | 领取、落回执各一个事务，行内各列一起更新 | §7「落回执中途失败」 |
| INV-14 队列丢失不毁持久工作 | 是 | 进程或容器重启丢掉未送达的用量 | SQLite 持久卷 + `synchronous=FULL`；`SENDING` 靠租约过期重领 | §7「重启后继续送」「租约过期重领」 |

## 4. 状态与并发

### outbox 行的状态

| 当前状态 | 事件 | 前置条件 | 新状态 | 副作用 | 非法时结果 |
| --- | --- | --- | --- | --- | --- |
| （无） | `record_*` | 字段合规；`event_id` 不存在 | `PENDING`（`next_attempt_at = now`） | — | 字段不合规抛 `ValueError`；同 id 不同内容抛 `EventConflictError` |
| （已存在） | `record_*` 同 `event_id` 同内容 | — | 不变 | 返回已有 id | — |
| `PENDING` | 领取 | `next_attempt_at <= now` | `SENDING` | `lease_until = now+60`、`attempts+1` | 不满足就不领 |
| `SENDING` | 领取 | `lease_until < now` | `SENDING` | 新租约、`attempts+1` | 不满足就不领 |
| `SENDING` | 回执：已接收 | 本进程持有的租约（`lease_until` 与领取时写的值相等） | `DELIVERED` | `delivered_at` | 租约已被别人重领：不改（以重领者为准） |
| `SENDING` | 回执：可重试 / 网络错误 / 凭据错误 | 同上 | `PENDING` | `next_attempt_at` 退避、`last_error_code` | 同上 |
| `SENDING` | 回执：不可重试 | 同上 | `DEAD` | `dead_at`、`last_error_code`、ERROR 日志 | 同上 |
| `DEAD` | `requeue_dead`（必填原因） | — | `PENDING` | `attempts = 0`、`next_attempt_at = now`、清 `dead_at` 与 `dead_acked_at`；同一事务写 `ops_log`（`REQUEUE_DEAD`） | 其他状态调用：返回假，不改、不写 `ops_log`；原因为空：抛 `ValueError` |
| `DEAD` | `ack_dead`（必填原因） | `dead_acked_at` 为空 | `DEAD` | `dead_acked_at = now`；同一事务写 `ops_log`（`ACK_DEAD`） | 已确认或非 `DEAD`：返回假，不改、不写 `ops_log`；原因为空：抛 `ValueError` |
| `DELIVERED` | `replay_delivered`（必填原因） | `delivered_at` 在给定时间段内 | `PENDING` | `attempts = 0`、`next_attempt_at = now`、清 `delivered_at` 与 `last_error_code`；每行同一事务写 `ops_log`（`REPLAY_DELIVERED`） | 时间段为空或倒置、原因为空：抛 `ValueError` |
| 事故未确认 | `ack_incident`（必填原因） | `acked_at` 为空 | 事故已确认 | `acked_at = now`、`ack_reason`；同一事务写 `ops_log`（`ACK_INCIDENT`） | 已确认或不存在：返回假，不改、不写 `ops_log`；原因为空：抛 `ValueError` |
| `DELIVERED` | 清理 | `delivered_at < now − delivered_retention_seconds` | （删除） | — | — |

`DELIVERED` 与 `DEAD` 不会被领取。

### 并发

- **串行化**：SQLite 文件级写锁（`BEGIN IMMEDIATE`）；领取、落回执、应用状态都在写事务里，同一时刻只有一个连接写。读（`should_block_ai`、`health`）在 WAL 下不被写阻塞。
- **租约栅栏**：落回执时用 `UPDATE ... WHERE event_id = ? AND status = 'SENDING' AND lease_until = ?`（领取时写下的那个值），影响 0 行说明租约已过期被别人重领，本次结果丢弃。
- **数据库负责的唯一性**：`event_id` 主键。
- **幂等键**：`event_id`（本地主键 + 服务端唯一约束）。
- **相同 id、不同载荷**：本地拒绝（`EventConflictError`）；万一到了服务端（例如两个不同的 outbox 文件），服务端 409 `retryable=false` → 本地 `DEAD`。
- **重试在哪一层**：只在客户端投递层；`Transport` 不重试。
- **必须原子提交**：一行的状态、租约、尝试次数、错误码一起改；状态表整行替换。

### 有效状态的版本

| 已存版本 | 收到的版本 | 结果 |
| --- | --- | --- |
| 无 | 任意（校验通过） | 应用 |
| `v` | `> v` | 整行替换 |
| `v` | `<= v` | 不改；对账算成功、webhook 回 2xx |

## 5. 失败与恢复矩阵

| 失败点 | 对外结果 | 数据最终状态 | 是否可重试 | 恢复来源 | 告警 |
| --- | --- | --- | --- | --- | --- |
| 校验失败（字段不合规） | `record_*` 抛 `ValueError` | 不写入 | 否（接入方代码问题） | 接入方修代码 | 接入方自己的错误日志 |
| 写入时主键冲突（`IntegrityError`） | 字节相同：返回已有 id；不同：抛 `EventConflictError` | 原行不变 | — | — | — |
| SQLite 写入失败（`IntegrityError` 以外的 `sqlite3.Error`：磁盘满、文件损坏、锁超时 5 秒） | `record_*` **不抛**，返回 `event_id` | 事件进进程内暂存（上限 1000 条）；后台线程每轮先尝试把暂存写回 SQLite | 是 | 后台线程回灌 | `held_in_memory > 0` → `health().level = P1`（回灌成功、暂存清空后解除）；记 ERROR 日志（不带载荷） |
| 暂存回灌时主键冲突且字节不同 | 不影响接入方（`record_*` 早已返回） | 暂存里这条丢弃，库里原行不变；`event_id` 写进 `incidents`（`CONFLICT_DROPPED`） | 否 | 人工对账，确认后 `ack_incident` | `conflicts_dropped` 累加；`incidents` 未确认 → P1；ERROR 日志带 `event_id` |
| 暂存已满 | `record_*` 不抛 | 这条事件丢弃；`event_id` 进待记事故，存储恢复后写进 `incidents` | 否（spec §20 接受这种罕见情况不能自动恢复） | 人工对账，确认后 `ack_incident` | `dropped_events` 累加；`incidents` 未确认 → P1，重启后仍在；ERROR 日志带 `event_id` |
| 进程在暂存回灌前退出 | — | 暂存里的事件与还没写进 `incidents` 的待记事故都丢失 | 否 | 无（已知窗口，§10） | 无法在退出后告警；`held_in_memory` 平时可见 |
| 中心从备份恢复，`status_version` 回退，本地仍存着恢复前的 `BLOCK_AI` | 新的模型调用被拦截 | `status_state` 不变（更小的版本被忽略） | 是 | 运维 `reset-status`（必填原因）后下一次对账应用中心的当前状态 | 由中心的恢复流程触发；README 写明恢复后先 `reset-status` 再 `replay` |
| 中心从备份恢复，丢了恢复点之后已回 202 的事件 | 不影响接入方 | 本地这些行是 `DELIVERED` | 是 | 运维按时间段 `replay_delivered`（必填原因）；服务端幂等 | 由中心的恢复流程触发；本地重放写 `ops_log` |
| 网络错误 / 超时 / 平台 5xx | 不影响接入方 | `PENDING`，按退避重试 | 是 | 后台线程 | `oldest_pending_age_seconds > 900` → P2 |
| `base_url` 配错（404 / 405 等不在白名单的不可重试结果） | 不影响接入方 | `PENDING` 退避，记该码 | 是（改对配置后自然恢复） | 运维修配置、重启 | 积压 → P2 |
| 凭据错误（401） | 不影响接入方 | `PENDING`，固定 60 秒后重试 | 是（每次发送用当前配置重签，修凭据后约 60 秒内恢复） | 运维修配置、重启 | `auth_failing` → P1 |
| 服务端判单条不可重试 | 不影响接入方 | `DEAD` | 否（自动） | 运维 `requeue_dead`（原因修好后）或 `ack_dead` | `dead_unacked > 0` → P1；ERROR 日志 |
| 批层面不可重试（413/422） | 不影响接入方 | 同一轮逐条单发，按单条回执落状态 | 视单条回执 | 后台线程 | 单条进死信时同上 |
| 回执结构不对 | 不影响接入方 | `PENDING` 退避 | 是 | 后台线程 | 持续时 P2（积压） |
| 发出后、落回执前进程崩溃 | — | 行停在 `SENDING`，租约到期后被重领、重发 | 是 | 服务端幂等回 `already_*` | 无 |
| 落回执的事务失败 | — | 行停在 `SENDING`，同上 | 是 | 同上 | ERROR 日志 |
| 对账失败 / 应答无法校验 | `should_block_ai()` 维持已存状态；从未成功则放行 | 状态表不变 | 是（30 秒后补查） | 下一轮对账或 webhook | 超过 300 秒 → `status_reconcile_stale` → P2 |
| webhook 验签通过但写库失败 | 接入方回 503（`StatusStoreError`） | 状态表不变 | 平台按退避重发 | 平台重发或下一轮对账 | 接入方日志 |
| webhook 验签失败 | 接入方回 401 | 状态表不变 | 平台按退避重发 | 修配置的 `webhook_secrets` | 接入方日志；对账仍兜底 |
| 状态表读失败 | `should_block_ai()` 返回假 | — | — | — | WARNING 日志 |
| Redis/Celery 丢失 | 不涉及：客户端不用 Redis 与 Celery | — | — | — | — |
| 重复请求（接入方用同一 `event_id` 记两次） | 返回同一个 id | 一行 | — | — | — |
| 构造时存储不可用（目录不存在、文件损坏、版本比代码新、磁盘满、I/O 错误） | `BillingClient` 照样构造，降级运行：记账进内存暂存、`should_block_ai()` 放行 | 不动原文件 | 是（每个投递间隔重试打开） | 存储恢复后自动回到正常；文件损坏时按 README 在进程运行中隔离旧文件，不重启 | `store_available` 为假 → `health().level = P1`；ERROR 日志 |
| 运行中文件损坏、I/O 错误或磁盘满（非 `BUSY` / `LOCKED`） | 不影响接入方：记账进暂存、`should_block_ai()` 放行 | 关闭连接，转入降级；不动原文件 | 是（每个投递间隔或 `deliver_once()` 重试打开） | 磁盘问题解决后自动恢复；文件损坏时按 README 在运行中隔离旧文件 | `store_available` 为假 → P1；ERROR 日志 |
| 主库被移走但 `-wal` / `-shm` 还在 | 不影响接入方：记账进暂存、放行 | 不新建库，不碰残留文件；残留的 `-wal` 里可能有已提交但未检查点的事件 | 是（每个间隔再试） | 运维把残留的 `-wal` / `-shm` 按配对名字移到被隔离的主库旁边（不能分开放或删除），随后客户端新建库；再用命令行读取配好对的旧文件 | `store_available` 为假 → P1；ERROR 日志点名还在的文件 |
| 接入方运行时运维要处置（中心恢复后 `reset-status`、死信、事故确认等） | 命令行退出 3，提示使用进程内方法 | 不变 | — | 调用接入方暴露的受保护入口，它转调 `BillingClient` 的同名方法（必填原因、写 `ops_log`） | — |
| 文件损坏且旧文件里还有未送达事件 | 不影响接入方 | 旧文件隔离保存 | 否（spec §20 接受） | 运维用命令行读旧文件，读不出或未送达的按人工对账事故处理 | 降级期间一直 P1 |

## 6. 数据与安全边界

- **租户过滤**：客户端只代表配置里的一个租户 × 项目；身份由凭据决定，服务端按凭据过滤。状态应答与 webhook 载荷都核对配置的 `tenant_id`、`project_id`，不符即拒（INV-8）。
- **鉴权主体**：签名凭据（`api_key` + `key_version` + `secret`）；webhook 用该项目的 webhook 签名密钥（按版本）。
- **密钥存储与读取**：密钥只在内存里的配置对象中，从接入方的环境变量或密钥管理读入；**不写进 SQLite、不写日志、不进异常消息**；配置对象的 `repr` / `str` 不含密钥。outbox 里只有事件载荷，没有任何凭据。
- **轮换**（spec §36）：服务端轮换时 `api_key` 不变、版本号 +1，重叠期默认 7 天。接入方在重叠期内把配置换成新版本与新密钥并重启；outbox 里的事件每次发送都用当前配置重新签名，换凭据不影响积压事件。webhook 密钥轮换：重叠期内配置里同时放新旧两个版本。
- **客户侧禁止返回的内部字段**：客户端不向任何人返回数据；它读到的状态应答本来就不含成本、毛利（INV-7）。
- **日志**：logger 名 `acuven_billing_client`。允许：`event_id`、`error_code`、条数、状态码、`status_version`、`effective_status`。禁止：密钥、签名、请求头原文、事件载荷、响应体原文。
- **prompt / response**：事件没有这类字段（INV-9）。
- **数据保留**：已送达行保留 `delivered_retention_seconds`（缺省 7 天、至少 1 天、须不短于中心的灾备重放窗口）后删除；死信、`ops_log` 与 `incidents` 不自动删；SQLite 文件在接入方的持久卷上，随接入方的备份策略。

## 7. 测试证据计划

客户端测试放 `integration-client/tests/`（SQLite 用 `tmp_path` 下的文件，HTTP 用注入的假 `Transport`，时间用注入的时钟）；契约测试放 `tests/backend/test_billing_client_contract.py`（同时导入客户端与服务端，服务端走 FastAPI `TestClient`，通过一个把 `Transport` 转发到 `TestClient` 的适配器）。

| 风险/需求 | 测试层级 | 场景 | 预期结果 | 任务 |
| --- | --- | --- | --- | --- |
| 签名对照向量 | unit | 用 `tests/backend/test_integration_auth.py` 的全零占位输入 | 得到同一个签名 | 164 |
| 查询串规范化 | unit | `docs/api.md` 的例子；无查询串；只有 `?`；重复键；没有 `=` 的段；百分号编码 | 与文档规则逐条一致 | 164 |
| 字段校验 | unit 表驱动 | 每个字段的合法与非法边界（照服务端 schema）；未知关键字参数；`float` 数量 | 合法通过；非法抛 `ValueError`；未知参数抛 `TypeError` | 164 |
| 可选字段与形态 | unit | 不给 `conversation_id`、`feature`；LLM 与 QUANTITY 各一条 | 载荷里没有这些键（不是 `null`）；LLM 载荷无 `quantity` / `unit`，QUANTITY 载荷无 token 字段 | 164 |
| 数量精度 | unit | `Decimal("37.420")`、`"0.00000001"`、`"0.000000010"`、`Decimal("1E+3")`、12 位整数、13 位整数、规范化后 9 位小数、负数、`NaN` | 前四个分别为 `37.42`、`0.00000001`、`0.00000001`、`1000`；越界与非有限抛 `ValueError` | 164 |
| 序列化稳定 | unit | 同一输入序列化两次 | 字节相同 | 164 |
| 配置不泄露密钥 | unit | `repr(config)`、`str(config)`、`from_env` 缺项 | 不含密钥；缺项报错点名变量名 | 164 |
| 写入持久 | integration（SQLite） | `record_*` 返回后关闭连接再打开 | 行在，`PENDING` | 165 |
| 自带 `event_id` 须给 `occurred_at` | unit | 传 `event_id` 不传 `occurred_at`；两样都传后同参数调用两次 | 前者抛 `ValueError`、不写入；后者一行、两次返回同一 id、不抛 | 165 |
| 本地去重与冲突 | integration | 同 id 同内容两次；同 id 不同内容 | 一行；冲突抛 `EventConflictError` 且不改原行 | 165 |
| 并发写同一 `event_id` | integration | 两个线程各自连接、同时用同一个接入方 `event_id` 写（同内容一组、不同内容一组） | 同内容：一行，两边都返回该 id；不同内容：一行，一边成功、另一边抛 `EventConflictError`；都不进暂存 | 165 |
| 主键冲突不算存储故障 | integration | 绕过查重直接让 `INSERT` 撞主键（同内容、不同内容） | 同内容返回已有 id，不同内容抛 `EventConflictError`；暂存为空 | 165 |
| 不建表的打开方式 | integration | 文件不存在、零字节、非 SQLite 文件、别的应用的 SQLite 库（没有 `meta`）、版本更新的库 | 都报错；文件内容与日志模式不变，不存在的文件不被创建 | 165 |
| 版本保护 | integration | 库里 `schema_version = 2` | `Store` 正常打开时抛错，表不变（`BillingClient` 构造时的行为见「构造时存储不可用」） | 165 |
| 领取并发 | integration | 两个线程各自连接同时领取 | 同一行只被一个领到 | 165 |
| 领取顺序 | integration | 同一 `created_at` 的三行 | 按 `event_id` 升序领取 | 165 |
| 错误码的清除 | integration | 带 `last_error_code` 的行送达；死信 `requeue_dead` | 两者 `last_error_code` 都清空；领取不清 | 165、166 |
| 租约过期重领 | integration | 领取后不落回执，时钟走过 60 秒再领 | 被重领，`attempts` 再加 1；旧租约的回执落不进去 | 165 |
| 状态机全表 | integration | §4 每一行 | 结果与表一致，非法调用不改 | 165、166 |
| 清理 | integration | 8 天前送达与 6 天前送达各一行、死信一行；另一组把保留期配成 2 天 | 缺省时只删第一行；保留期 2 天时两行都删；死信不删 | 165 |
| 保留期下限 | unit | `delivered_retention_seconds` 配成 1 天减 1 秒 | 构造配置对象报错 | 164 |
| 重放已送达 | integration | 三行 `DELIVERED`，`delivered_at` 分别在时间段之前、之内、之后 | 只有中间一行改回 `PENDING`（`attempts = 0`、`delivered_at` 清空），载荷不变；`ops_log` 恰好一行 `REPLAY_DELIVERED`；时间段倒置或原因为空抛 `ValueError` 且不改任何行 | 166 |
| 处置必填原因与审计 | integration | `requeue_dead`、`ack_dead`、`ack_incident` 各带原因一次、不带原因一次；非法状态调用一次 | 带原因的成功并在同一事务写 `ops_log`；不带原因抛 `ValueError`、不改；非法状态返回假、不写 `ops_log`；`ops_log` 在事务中途失败时与状态改动一起回滚 | 166 |
| 回写失败并回 | unit | 待记事故上限配成 3，快照里 2 条 id 与溢出 2 条（时刻 t1、t2），写库失败期间又丢弃 2 条（时刻 t3、t4） | 并回后列表 3 条、溢出 3 条（多出的 1 个 id 转为溢出），溢出首次 t1、最近 t4；下次写库成功后 `incidents_pending = 0` | 168 |
| webhook 的 `BLOCK_AI` 在对账失败时生效 | unit | 对账一直失败，一条合规 webhook 应用 `BLOCK_AI` | `should_block_ai()` 为真；`reset_status` 后为假 | 169 |
| 事故记录 | integration | 暂存满丢弃两条、回灌冲突一条，之后存储恢复 | 写入前 `incidents_pending = 3`；写入后为 0，`incidents` 有三行（`DROPPED` 两条、`CONFLICT_DROPPED` 一条），只有 `event_id` 没有载荷；新建 `BillingClient`（模拟重启）后 `incidents_unacked = 3`；`ack_incident` 后减一 | 168 |
| 事故确认后再丢弃 | integration | 接入方自带 `event_id` 的事件被丢弃并记事故，`ack_incident` 后同一个 `event_id` 再次被丢弃 | 第二次记了一条新事故（新的 `incident_id`），`incidents_unacked = 1`；未确认期间重复丢弃不新增 | 166 |
| 状态重置 | integration | 本地存着版本 50 的 `BLOCK_AI`；中心恢复后对账得到版本 3 的 `ALLOW_AI` | 重置前：忽略版本 3、仍拦截；`reset_status` 后 `should_block_ai()` 立即为假，下一次对账应用版本 3；`ops_log` 有一条 `RESET_STATUS`；原因为空抛 `ValueError` | 166 |
| 溢出事故 | integration | 待记事故上限配成 2，存储故障期间暂存满后丢弃 5 条，之后存储恢复 | 写入前 `incidents_pending = 5`；写入后为 0，`incidents` 有 2 行 `DROPPED` 与 1 行 `OVERFLOW_DROPPED`（`dropped_count = 3`、`event_id` 为空、首次与最近时刻正确）；三行都 `ack_incident` 后 `level` 不再因事故为 P1；`ops_log` 有三行 `ACK_INCIDENT` | 168 |
| 确认后告警消失 | unit | 同一进程里丢弃并记入事故后，逐条 `ack_incident` | 三条都确认后 `level` 不再因事故为 P1（`dropped_events`、`conflicts_dropped` 仍是累计值但不影响 `level`） | 170 |
| 回执判定全表 | unit（假 Transport） | §2「投递」表的每一行，含逐条混合（接收、重复、可重试、不可重试） | 每条落到表中的状态与错误码 | 167 |
| 重试载荷逐字节相同 | unit | 一批第一次 503、第二次 200 | 两次请求体里每条事件字节相同；两次 Request-Id 与时间戳不同 | 167 |
| 组批上限 | unit | 150 条小事件；100 条各约 15 KiB 的事件（单条上限 16 KiB 以内） | 前者切成 100 + 50；后者按 1,000,000 字节切，每批请求体不超过上限 | 167 |
| 批层不可重试改单发 | unit | 批量回 422 `BATCH_TOO_LARGE` | 同一轮逐条单发，单条回执决定状态 | 167 |
| 单发回退里的事件相关可重试码 | unit | 单发途中第 2 条回 409 `UNKNOWN_USAGE_TYPE`（`retryable = true`） | 只第 2 条 `PENDING` 退避、记该码，继续发第 3 条 | 167 |
| 地址拼接 | unit | `base_url` 带末尾 `/`；带路径前缀 | 请求 URL 与签名路径都是前缀加接口路径，没有双斜杠 | 167 |
| 单发回退的停止条件 | unit | 单发途中第 2 条遇网络错误；另一组第 2 条单条回执 `event_id` 不符 | 前者第 2 条与其后的行都 `PENDING`、记 `NETWORK_ERROR`，不再发；后者只第 2 条记 `INVALID_BILLING_RECEIPT`，继续发第 3 条 | 167 |
| 凭据错误固定 60 秒重试 | unit（注入时钟） | 批量 401 后第 5 次尝试；之后换成能通过的凭据 | `next_attempt_at = now + 60`（不按指数）；修好后下一次在 60 秒内送达，`last_error_code` 清空 | 167 |
| 凭据错误不进死信 | unit | 批量 401；单条接口 401 | 全部 `PENDING`，`last_error_code = INTEGRATION_AUTH_FAILED` | 167 |
| 死信白名单 | unit | 批量 404 信封 `retryable = false`；逐条 `rejected`、`retryable = false`、码为未知的 `SOMETHING_NEW`；单发回退中单条 405 | 都 `PENDING` 退避并记该码，不进 `DEAD`；单发回退遇 405 停止 | 167 |
| 逐条结果残缺 | unit | 批量 200，一条 `status = "weird"`，一条 `rejected` 缺 `retryable`，其余正常 | 只这两条 `INVALID_BILLING_RECEIPT` 退避，其余按各自结果 | 167 |
| 范围不符按信封 | unit | 批量里逐条 `CREDENTIAL_SCOPE_MISMATCH`（`retryable = false`）；单条接口 403 | 该条 `DEAD`，记 ERROR；不置 `auth_failing` | 167 |
| 载荷不带租户与项目 | unit | LLM 与 QUANTITY 各一条 | 载荷里没有 `tenant_id`、`project_id` 键 | 164 |
| 退避 | unit | attempts 1..13 | 区间 `[0.5, 1.0] × min(3600, 2^min(n,12))` | 167 |
| 平台不可用 | unit | 连续网络错误 | `record_*` 照常成功；行留 `PENDING` | 168 |
| 运行中转入降级 | unit | 存储可用并已记账 2 条；之后让读写抛 `SQLITE_CORRUPT`（替身）再记账 1 条；另一组抛 `SQLITE_BUSY` | 前者：`store_available` 变假、连接关闭、第 3 条进暂存；去掉替身后下一轮重新打开、回灌入库。后者：`store_available` 仍为真、第 3 条进暂存、下一轮回灌 | 168 |
| 空闲线程不写旧文件 | integration | 两个线程各自已建立连接；第三个线程触发 `SQLITE_CORRUPT` 转入降级；隔离旧文件后重新打开；原先空闲的两个线程再 `record_*` | 两个线程的事件都进入新文件，旧文件的行数不变；新库的 `-wal` 在旧连接全部关闭之后才出现、之后一直在；新库 `PRAGMA integrity_check` 为 ok；已提交的事件可读 | 165、168 |
| 单进程锁 | integration | 一个 `Store` 已打开；另一个进程（`multiprocessing` 起的子进程）用同一路径构造 `BillingClient` | 子进程的客户端降级、`store_available` 为假、ERROR 日志写明被锁；第一个进程退出后，子进程下一个间隔取到锁、恢复 | 165、168 |
| 命令行对着被锁的 outbox | unit | 一个进程（`multiprocessing` 子进程）持有 `BillingClient`；命令行对同一路径执行 `stats`、`requeue`、`reset-status` | 都退出 3、输出一行 JSON 的 error、不改文件；子进程退出后同样的命令正常执行 | 170 |
| 只移走主库 | integration | 存储可用、本进程打开着库、WAL 里有未检查点的已提交行时，只把主库移走 | 下一次使用检测到替换、转入降级；原路径的 `-wal` 仍在、不新建库、ERROR 日志点名残留文件；把 `-wal` / `-shm` 按配对名字移到被隔离的主库旁边后：下一个间隔新建库并回灌，命令行对配好对的旧文件能读出全部已提交行、`integrity_check` 为 ok | 165、168 |
| 三个文件一起移走 | integration | 同上，但把主库、`-wal`、`-shm` 按配对名字一起移走 | 下一次使用转入降级并立即新建库；命令行对被隔离的那一组能读出全部已提交行 | 165、168 |
| 部分损坏的文件不来回切换 | unit | 一个能打开、但写某些页时报 `SQLITE_CORRUPT` 的文件（替身）；转入降级后过几个投递间隔 | 不再打开这个文件、`store_available` 保持为假、暂存不写进它；把它移走（换成新的路径文件）后下一个间隔才重新打开并回灌。另一组：因 `SQLITE_FULL` 降级，磁盘恢复后同一个文件下一个间隔重新打开 | 168 |
| 运行中隔离损坏文件 | unit | 构造时文件损坏进入降级，记账 3 条（暂存上限配成 2，第 3 条丢弃进待记事故）；进程不停，把损坏文件移走 | 下一轮投递建出新文件、`store_available` 为真；暂存 2 条入库、`incidents` 有 1 条 `DROPPED`；`held_in_memory` 与 `incidents_pending` 都为 0 | 168 |
| 降级后只靠 `deliver_once()` 恢复 | unit | 不调 `start()`；构造时打开失败，之后让打开成功 | 一次 `deliver_once()` 先重新打开、再回灌并投递；`reconcile_status_once()` 在降级时同样先重新打开 | 168 |
| 构造时存储不可用 | unit | 分别用：目录不存在、写满随机字节的文件、`schema_version = 2` 的库、打开时抛 `sqlite3.OperationalError` 的替身 | 构造不抛；`store_available` 为假；`record_*` 返回 `event_id` 并进暂存；`should_block_ai()` 为假；`requeue_dead` 抛 `StoreUnavailableError`；之后让打开成功（换成可用的文件或去掉替身），下一轮投递 `store_available` 为真、暂存回灌入库 | 168 |
| 存储故障 | unit | SQLite 写入抛错；暂存满 | `record_*` 不抛；暂存回灌后入库；满了 `BillingClient.dropped_events` 加 1 | 168 |
| 回灌时冲突 | unit | 暂存里一条，回灌前库里已有同 id 的行（同内容、不同内容各一次） | 同内容：移出暂存、不计数；不同内容：移出暂存、`BillingClient.conflicts_dropped` 加 1、原行不变 | 168 |
| 单发回退续租 | unit（注入时钟） | 批量 422 后逐条单发，每条耗时 30 秒；中途让另一个连接重领其中一行 | 每条发送前续租；发现被重领后停止回退，剩下的行不由本进程落状态 | 167 |
| 后台线程不死 | unit | 一轮里抛异常 | 记日志，下一轮照常；`stop()` 在超时内返回 | 168 |
| 重启后继续送 | integration | 写入后新建一个 `BillingClient` 指向同一文件再 `deliver_once` | 送达 | 168 |
| 对账应用与版本 | unit | 版本 3 → 5 → 4 | 应用 5，忽略 4；4 时仍更新 `reconcile_state.last_ok_at` | 169 |
| 先失败后成功 | unit | 第一次对账失败（没有任何状态行），第二次成功 | 失败只写 `reconcile_state.last_error_at`，`status_state` 没有行；第二次应用成功 | 169 |
| webhook 不冒充对账 | unit | 对账一直失败，期间收到一条合规 webhook | 状态被应用；`reconcile_state.last_ok_at` 不变；`status_reconcile_stale` 仍按对账判定 | 169 |
| webhook 写库失败 | unit | 验签通过，`apply_status` 抛 `sqlite3.Error` | 抛 `StatusStoreError`，不是 `WebhookVerificationError` | 169 |
| 状态应答字段 | unit | 缺字段、类型错、`effective_status` 非法；另一组多一个未知字段 | 前者不应用、记失败；多字段的照常应用，库里不存未知字段 | 169 |
| 换项目不沿用旧状态 | unit | 库里已有另一租户 × 项目的 `BLOCK_AI`、版本 99；当前配置的项目对账得到 `ALLOW_AI`、版本 1 | 新行应用；`should_block_ai()` 为假；旧行不变且不被读到；webhook 同样只按配置那一对比较版本 | 169 |
| 租户或项目不符 | unit | 应答或 webhook 的租户 / 项目与配置不同 | 拒绝，不应用 | 169 |
| 对账失败放行 | unit | 从未成功；成功一次 `BLOCK_AI` 后连续失败 | 前者 `should_block_ai()` 假；后者维持真 | 169 |
| 状态表读失败放行 | unit | 注入读库时抛 `sqlite3.Error` 的存储 | `should_block_ai()` 返回假、不抛、记 WARNING；`current_status()` 返回 `None`、不抛 | 169 |
| 对账不被投递拖住 | unit | 投递一轮被假 Transport 卡住（阻塞直到测试放行），期间到了对账时刻 | 对账线程照常发出对账请求 | 169 |
| 对账节奏 | unit（注入时钟） | `start()` 后第一次；成功；失败；再失败；再失败；成功；失败 | 立即 / 240 / 30 / 240 / 240 / 240 / 30 秒 | 169 |
| webhook 验签 | unit | 传 `signed_path` 时用它代替原始路径（含查询串排序）；正确签名；错签名；时间窗外 301 秒；恰好 300 秒；未知版本；缺请求头；体里 `event_id` 与头不符；新旧两个版本都配 | 只有合规的通过；两个版本都认 | 169 |
| 健康指标 | unit | 各计数与阈值边界（900 秒、300 秒）；`store_available` 为假与读库失败两种情况下，库里的键为 `null`、`store_available` 与四个进程内计数照实返回；`store_available` 为假时 `level = P1`、库里的键为 `null`；暂存里有 1 条而库可读时 `level = P1`、回灌后恢复；`auth_failing` 与未确认事故在新建 `BillingClient`（模拟重启）后仍让 `level = P1` | 键集合固定；`level` 判定正确；读库失败 `P1` 不抛 | 170 |
| 命令行 | unit | `stats`（含两对租户 × 项目）、`dead`（缺省与 `--limit`）、`requeue`、`ack`、`replay`、`incidents`、`ack-incident`、`reset-status`（各带与不带 `--reason`；`reset-status` 另测那一对没有行时）、文件不存在、参数错 | 输出格式与 §2 一致、不含 `level` 与进程内计数；文件不存在退出 1 且不建文件；参数错退出 2；不联网 | 170 |
| 契约：签名过服务端 | integration（服务端） | 客户端签的请求打到 `TestClient` 的摄取接口 | 202；服务端 `integration_auth` 验签通过 | 171 |
| 契约：载荷过服务端 | integration（服务端） | LLM 与 QUANTITY 两类事件经客户端写入、`deliver_once` 送到批量接口 | 回执 `accepted`；本地 `DELIVERED`；再送一次得 `already_*` | 171 |
| 契约：有效状态 | integration（服务端） | 客户端对账打到服务端 | 应用成功，字段集合与服务端一致 | 171 |
| 契约：webhook | integration（服务端） | 用服务端 `status_delivery` / `webhook_signing` 构造出站请求（不发网络），交给客户端验签 | 通过并应用 | 171 |
| 租户越权（服务端） | 已由服务端测试覆盖 | 凭据与事件里的租户不符 | 403；客户端载荷不带租户与项目，不会触发 | — |
| 金额精度 | 见「数量精度」 | — | — | 164 |
| 幂等重放 | 见「契约：载荷过服务端」第二次发送 | — | — | 171 |
| 事务中途失败 | integration | 落回执的事务里抛错 | 行停在 `SENDING`，租约到期后重领 | 165 |
| Redis/Celery 丢失 | 不适用 | 客户端不用 | — | — |

## 8. 迁移与上线

- **服务端**：无改动、无迁移、无部署影响（镜像不含 `integration-client/`）。
- **客户端 SQLite**：首次打开时建表并写 `schema_version = 1`；以后改表结构必须升 `schema_version`，并在客户端里写升级步骤（本版没有）。
- **兼容窗口**：服务端契约不变，客户端只是新的调用方。
- **部署顺序**：本仓库合并 `AIH-TASK-164`..`171` 后，接入方（`acuven_aichat`）在自己的仓库按 README 接入：钉提交、配持久卷与环境变量、接 webhook 路由、在模型调用前查 `should_block_ai()`、模型调用成功后 `record_*`、启动时 `start()`、退出时 `stop()`、把 `health()` 接进自己的健康检查。
- **回滚**：接入方把依赖钉回旧提交即可；outbox 文件版本不变时新旧客户端可互读。
- **部分部署**：不适用（单个包）。
- **监控**：接入方用 `health().level`；负责人是接入方的运维（目前是 Kelvin）。

## 9. 方案取舍

| 方案 | 优点 | 风险 | 未采用原因 |
| --- | --- | --- | --- |
| 采用：只用标准库，同步接口 + 后台线程，SQLite outbox | 零依赖，不会和接入方的 `httpx` / 服务端的 `httpx2` 冲突；SQLite 自带、持久、单进程内多线程可共用（进程模型见 §1） | `urllib` 没有连接池，每次投递新建连接 | — |
| 备选 A：依赖 `httpx` | 有连接池、超时更细 | 本仓库测试环境装的是 `httpx2`，客户端测试要另装 `httpx`；与接入方锁定的版本可能冲突 | 投递频率低（10 秒一批），连接池收益小 |
| 备选 B：asyncio 接口 | 与 FastAPI 风格一致 | 要同时维护同步版（命令行、测试）；后台任务的生命周期绑在事件循环上，接入方用错就停投 | 写入是毫秒级本地提交，同步接口够用；可以以后加 |
| 备选 C：outbox 放 Redis（开 AOF） | 两个接入方都有 Redis | 持久性取决于 Redis 配置；spec §37 不让只靠 Redis | Kelvin 2026-11-05 选 SQLite |
| 备选 D：401 进死信 | 简单 | 一次凭据配错会把全部积压事件打进死信，要人工逐条重投 | 事件本身没错，保持待发、只报警 |
| 备选 E：客户端复用服务端的签名函数（`import app...`） | 只有一份实现 | 接入方装不了 `app`；会把服务端依赖带过去 | 改为各自实现 + 契约测试锁一致 |
| 备选 F：独立仓库 | 版本独立 | 签名契约的一致性要跨仓库保证 | Kelvin 2026-11-05 选本仓库子目录 |

## 10. 未决问题与假设

- **未决问题**：无。
- **假设**：
  1. 接入方在模型调用**成功之后**才调 `record_*`；调用前崩溃丢一次用量是接入方侧的窗口（§2「事务边界」），用自己的 `event_id` 可在重放时补记。
  2. 接入方的 outbox 文件放在持久卷上；放在容器临时层上时重建容器会丢积压事件。README 写明；`BillingClient` 构造时目录不存在会进入降级运行并报 P1（§2「构造时存储不可用 → 降级运行」），但无法判断目录是不是持久卷。
  3. 接入方主机做 NTP 同步；偏差超过 300 秒表现为 401，客户端报 `auth_failing`。
  4. 进程内暂存（存储故障时）在进程退出前没回灌就丢失；上限 1000 条，满了丢弃并计数。这是为满足不变量 1（不因计费侧故障中断 AI）接受的窗口，与服务端 AI 模块的有界内存缓冲同一取舍。
- **需要谁拍板**：无（四个产品决定 Kelvin 2026-11-05 已定，见 §1）。
- **如果假设错误**：1、2、4 的后果是少计用量（不会多扣），由服务端的用量与模型侧账单对账发现；3 的后果是投递停滞并报 P1。

## 11. 审查与版本绑定

审查方要回答的五个问题常驻在 [scripts/review_checklist.md](../../scripts/review_checklist.md) 的「设计审查」一节，此处不重复。

**版本绑定规则**（规则的出处是 [WORKFLOW §3](../../docs/WORKFLOW.md)；这里保留一份是因为审查材料里只有本 Issue 正文，两处改一起改）：

- 批准必须写成 `APPROVED: design v<N>`，`<N>` 取本 Issue 顶部的「设计版本」
- **设计版本一变，之前的 APPROVE 自动作废**，必须重新过闸门
- 什么算「实质修改」：改了契约、表结构、事务边界、状态机、失败语义、不变量控制。改错别字不算
- 实质修改时：把顶部版本 +1，在下方追加一条变更说明，状态退回 `READY_FOR_REVIEW`

### 实现拆分

一个任务只做一层，按顺序依赖：

| 任务 | 内容 |
| --- | --- |
| `AIH-TASK-164` | 包骨架（`integration-client/pyproject.toml`、`README.md` 骨架、`__init__.py`）、`config.py`、`ids.py`、`events.py`、`signing.py` 与测试；根 `pyproject.toml` 的 pytest `testpaths`、`pythonpath` |
| `AIH-TASK-165` | `store.py` 的 outbox 核心：建表与两种打开方式、版本保护、写事件与去重、领取与租约栅栏、落回执、清理、投递相关的只读计数，与测试 |
| `AIH-TASK-166` | `store.py` 的运维与状态部分：死信运维、重放、`ops_log`、`incidents`、`status_state` 与 `reconcile_state` 的读写与状态重置，与测试 |
| `AIH-TASK-167` | `transport.py`、`delivery.py`（一轮投递：领取、组批、回执判定、单发回退与续租、退避、凭据错误重试、清理）与测试；不含门面 |
| `AIH-TASK-168` | `client.py` 门面的记账与投递部分（`record_*`、内存暂存与回灌、三个进程内计数、后台线程、`deliver_once`、死信运维）与测试 |
| `AIH-TASK-169` | `status.py`、`webhook.py`、门面的状态部分与测试 |
| `AIH-TASK-170` | `metrics.py`、`__main__.py`（全部子命令，含 `reset-status`）与它们的测试 |
| `AIH-TASK-171` | `tests/backend/test_billing_client_contract.py`、README 接入指南写全、`docs/ARCHITECTURE.md` 记下 `integration-client/` 的位置、`docs/TODO.md` 勾掉 Phase 3「Billing Client」 |

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
| v1 | 2026-11-05 | 初稿 | — |
| v2 | 2026-11-05 | 写事件用 `BEGIN IMMEDIATE`；主键冲突与存储故障分开，定义暂存回灌撞主键的最终状态与 `conflicts_dropped`（P1）；单发回退途中续租；webhook 助手可传 `signed_path`；§1 写明与 `docs/api.md` 错误表的一处偏离；§5、§7 补对应行 | v1 审查阻断项 1 条、建议项 3 条 |
| v3 | 2026-11-05 | 传了 `event_id` 必须显式给 `occurred_at`（否则 `ValueError`），README 写明 `EventConflictError` 只在接入方用同 id 传不同内容时出现；§7 补「自带 `event_id` 须给 `occurred_at`」与「状态表读失败放行」两行 | v2 审查阻断项 2 条 |
| v4 | 2026-11-05 | `status_state` 主键改为 `(tenant_id, project_id)`，读写只认配置那一对；`last_delivery_ok_at` 移到 `meta`；对账应答与 webhook 载荷只要求含已知字段、忽略多出的字段；`auth_failing` 改为从库里算；INV-7 控制措施与 §7 对应改写、补「换项目不沿用旧状态」 | v3 审查阻断项 1 条、建议项 2 条 |
| v5 | 2026-11-05 | 对账时刻移到新表 `reconcile_state`，`status_state` 只在首次应用时插入、`apply_status` 只管版本、webhook 不写对账时刻；凭据错误改为固定 60 秒重试，送达与 `requeue_dead` 清 `last_error_code`；webhook 写库失败抛 `StatusStoreError`（回 503）；定下可选字段省略、数量先去尾零再判位数、未知参数 `TypeError`、`verify_webhook_signature` 的参数与职责、签名时间戳取注入时钟、领取次序 `(created_at, event_id)`、单条回执不符与单发回退的停止条件、对账节奏（启动立即查、一段连续失败只补查一次）、`oldest_pending_age_seconds` 口径、命令行输出与退出码；`status_state` 列类型；组批测试改用 15 KiB 事件；拆成六个任务（投递引擎与门面分开） | 登记前预审（`worker.design_precheck`）对 164..168 报出的发现 |
| v6 | 2026-11-05 | 载荷不再写 `tenant_id` / `project_id`；只有 401 保持待发并固定 60 秒重试，`CREDENTIAL_SCOPE_MISMATCH` 按信封进死信；`auth_failing` 只看 `INTEGRATION_AUTH_FAILED`；§1 的文档偏离只剩 401 一行；§5、§7 对应改写 | v5 审查阻断项 1 条 |
| v7 | 2026-11-05 | 进死信的错误码改为白名单（单条摄取错误表里由事件内容决定的六个码），其他 `retryable = false` 的结果保持待发；只有批层 413 / 422 三种码才改单发；逐条结果的 `status` 未知或缺 `retryable` 只影响该条；对账改为单独的线程，不再排在投递之后 | 登记前预审第二轮（`worker.design_precheck`）报出的三个设计缺陷 |
| v8 | 2026-11-05 | 单发回退里 `UNKNOWN_USAGE_TYPE` 只影响那一条、不停止回退；写明错误信封的判定、回执整批失败与单条失败的界线、`base_url` 与接口路径的拼接、续租不足放弃的行计入「栅栏失效」、命令行用的不建表打开方式；§7 补对应行 | 登记前预审第三轮报出的设计缺陷 1 条与未定项 |
| v9 | 2026-11-05 | 加中心恢复后的按时间段重放 `replay_delivered`，已送达保留期可配置、至少 1 天、须不短于中心灾备重放窗口；重投、放弃、重放、确认事故都必填原因，并在同一事务写只增不改的 `ops_log`；暂存丢弃与回灌冲突的事件 id 存储恢复后写进 `incidents`，未确认前一直 P1；命令行加 `replay`、`incidents`、`ack-incident`；§4、§5、§7 对应改写 | v8 审查阻断项 3 条（spec §21 与 §98.1、§31、§20） |
| v10 | 2026-11-05 | `level` 的 P1 改看 `incidents_unacked` 与新增的 `incidents_pending`，`dropped_events`、`conflicts_dropped` 只作展示；写明 `health()` 与命令行 JSON 的时刻是 Unix 秒；§7 补「确认后告警消失」 | 登记前预审第四轮（AIH-TASK-169）报出的设计缺陷 |
| v11 | 2026-11-05 | 待记事故列表满后的溢出计数在存储恢复时写成一行 `OVERFLOW_DROPPED` 事故（记条数与起止时刻）并清零；`incidents` 主键改为 `incident_id`，加 `dropped_count`、首次与最近时刻；`ops_log` 的 `event_id` 列改名 `target_id`；`ack_incident` 与命令行按 `incident_id` 确认；§7 补「溢出事故」 | v10 审查阻断项 1 条 |
| v12 | 2026-11-05 | `held_in_memory > 0` 加进 P1 条件；§5「SQLite 写入失败」的告警列与 §7「健康指标」对应补充 | v11 审查阻断项 1 条 |
| v13 | 2026-11-05 | 事故 `incident_id` 每次新生成，只对未确认的同一 `event_id` 与 `kind` 去重；待记事故写入前整体移入待写快照，写库期间的新丢弃单独成行；加 `reset_status` 与命令行 `reset-status`（必填原因、写 `ops_log` 的 `RESET_STATUS`），用于中心恢复后版本回退；§4 补事故确认一行；`store.py` 拆成两个任务，全批改为 `AIH-TASK-164`..`170`，§7 任务列随之改号 | 登记前预审第六轮报出的设计缺陷 2 条与 165 规模过大 |
| v14 | 2026-11-05 | 待记事故写库失败并回时超出上限的 id 转为溢出、两段溢出的合并规则；`should_block_ai()` 只看有没有那一对的状态行，与对账是否成功过无关；170 拆成指标与命令行（170）和契约测试与文档（171）；§7 补「回写失败并回」与「webhook 的 BLOCK_AI 在对账失败时生效」 | 登记前预审第七轮报出的设计缺陷 2 条与 170 规模过大 |
| v15 | 2026-11-05 | 构造 `BillingClient` 时存储不可用改为降级运行（不抛、记账进暂存、放行 AI、P1、每个投递间隔重试打开）；`health()` 加 `store_available`；README 写损坏文件的隔离与人工对账；§3 INV-1、§5、§7 对应补充 | v14 审查阻断项 1 条 |
| v16 | 2026-11-05 | 损坏文件的恢复改为不停进程、运行中隔离旧文件，等暂存与待记事故归零后再处理旧文件；`deliver_once()` 与 `reconcile_status_once()` 在降级时也先尝试重新打开；§5、§7 对应补充 | v15 审查阻断项 2 条 |
| v17 | 2026-11-05 | 运行中遇到非 `BUSY` / `LOCKED` 的存储错误（损坏、I/O、磁盘满等）关闭连接、转入与构造失败同一套降级状态；§5、§7 对应补充 | v16 审查阻断项 1 条 |
| v18 | 2026-11-05 | 定义连接作废与文件替换检测：进程内连接代号，每次使用前比对代号与文件身份（设备号加 inode），不同就重建或转入降级；多进程各自检测替换；§7 补「空闲线程与其他进程不写旧文件」 | v17 审查阻断项 1 条 |
| v19 | 2026-11-05 | 新建库须 `<path>`、`-wal`、`-shm` 三者都不存在，否则保持降级；README 写明三个文件一起移走；§5、§7 对应补充 | v18 审查阻断项 1 条 |
| v20 | 2026-11-05 | Kelvin 定一个 outbox 文件只给一个进程用：加单进程锁（`<path>.lock` 排他文件锁，取不到则降级）；连接改为登记表加读写锁，`invalidate()` 关闭全部旧连接后才重新打开或新建库；去掉多进程共用文件的说法；§7 改「空闲线程不写旧文件」（含新库 `-wal` 与 `integrity_check`）并补「单进程锁」 | v19 审查阻断项 1 条（旧连接关闭时按路径删新库 WAL） |
| v21 | 2026-11-05 | 命令行的不建表打开也先取单进程锁，取不到退出 3；命令行只用于没有进程在用的文件，运行中的处置走进程内方法、由接入方暴露受保护入口；§5、§7 对应补充 | v20 审查阻断项 1 条 |
| v22 | 2026-11-05 | 写明 `invalidate()` 关闭最后一个连接时 SQLite 把 WAL 检查点进被移走的主库并按原路径删 `-wal` / `-shm`，只移走主库在本进程打开着库时也安全，「三者都不在才新建」主要防本进程没打开库时的残留；§7「只移走主库」按此改成两组；待记事故并回时转为溢出的 id 也更新溢出的首次与最近时刻；§10 假设 2 改为构造时目录不存在降级运行 | 登记前预审第八轮报出的设计缺陷 3 条 |
| v23 | 2026-11-05 | 改正 v22 对 SQLite 关闭行为的说法：主库被移走后关闭连接跳过检查点、不删 `-wal` / `-shm`；README 与 §5 要求三个文件按配对名字一起移走，只移主库时把残留按配对名字补放到被隔离的主库旁边；§7「只移走主库」改为验证真实行为，并补「三个文件一起移走」 | v22 审查阻断项 1 条 |
| v24 | 2026-11-05 | 因 `SQLITE_CORRUPT` / `SQLITE_NOTADB` 降级时记下坏文件身份，不再重开同一个文件，直到路径上的文件被换走；`health()` 在存储不可用与读库失败两种情况下统一为库里的键 `null`、进程内计数照实返回；§7 补「部分损坏的文件不来回切换」 | 登记前预审第九轮报出的设计缺陷 2 条 |

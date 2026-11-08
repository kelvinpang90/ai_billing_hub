# acuven-billing-client

`acuven_billing_client` 是给外部接入方（不经 AI 模块调模型的应用）用的 Python 包：把用量事件可靠地上报给
Acuven 计费平台，并按平台的有效状态决定是否拦截新的模型调用。它负责建用量事件、本地 outbox、后台投递与重试、
请求签名、有效状态对账、状态 webhook 验签与健康指标，接入方不必了解计费 API 的细节。

设计依据：设计闸门 #571（`docs/design/AIH-TASK-164-billing-client.md`）。与服务端的契约测试在本仓库
`tests/backend/test_billing_client_contract.py`。

- 只用标准库，没有第三方运行时依赖；需要 Python 3.12 或更高版本。
- 只有同步接口；投递与对账在包内的后台线程里跑。
- 不发布到 PyPI。

下面的示例里，`<...>` 都是占位，换成你自己的值；示例不含任何真实地址或密钥。

## 安装

用 pip 从本仓库的 git 地址安装，**钉到一个提交**，并指定 `subdirectory=integration-client`：

```text
acuven-billing-client @ git+https://<仓库地址>.git@<提交 SHA>#subdirectory=integration-client
```

把上面这一行写进接入方自己的依赖文件（例如 `requirements.txt`）；`<仓库地址>` 与 `<提交 SHA>` 换成实际值。
升级时改钉的提交；包的版本号写在 `acuven_billing_client.__version__` 与 `pyproject.toml`，两处一致。
回滚就是把依赖钉回旧提交：outbox 文件格式版本不变时新旧客户端可互读。

## 接入指南

### 1. 配置：八个环境变量与持久卷

`BillingClientConfig.from_env()` 读下面八个变量（前缀缺省 `ACUVEN_BILLING_`，可用 `from_env(prefix=...)` 换）：

| 变量 | 必填 | 含义 |
| --- | --- | --- |
| `ACUVEN_BILLING_BASE_URL` | 是 | 计费平台的 http / https 地址，可带路径前缀；客户端在后面拼 `/api/v1/integration/...` |
| `ACUVEN_BILLING_API_KEY` | 是 | 集成凭据的 API key |
| `ACUVEN_BILLING_KEY_VERSION` | 是 | 凭据版本，正整数 |
| `ACUVEN_BILLING_API_SECRET` | 是 | 该版本的签名密钥 |
| `ACUVEN_BILLING_TENANT_ID` | 是 | 租户的对外 id；只用于核对状态应答与 webhook，**不写进事件** |
| `ACUVEN_BILLING_PROJECT_ID` | 是 | 项目的对外 id；同上 |
| `ACUVEN_BILLING_OUTBOX_PATH` | 是 | 本地 outbox SQLite 文件的路径，**必须在持久卷上** |
| `ACUVEN_BILLING_WEBHOOK_SECRETS` | 否 | 状态 webhook 的签名密钥，`<版本>:<密钥>` 用逗号分隔；不配就不接收 webhook |

```text
ACUVEN_BILLING_BASE_URL=<计费平台地址>
ACUVEN_BILLING_API_KEY=<api key>
ACUVEN_BILLING_KEY_VERSION=1
ACUVEN_BILLING_API_SECRET=<签名密钥>
ACUVEN_BILLING_TENANT_ID=<租户对外 id>
ACUVEN_BILLING_PROJECT_ID=<项目对外 id>
ACUVEN_BILLING_OUTBOX_PATH=/data/billing/outbox.db
ACUVEN_BILLING_WEBHOOK_SECRETS=1:<webhook 密钥>
```

- 缺变量或格式不对时 `from_env` 抛 `ValueError`，消息只点名变量，不带值；配置对象的 `repr` / `str` 也不含密钥。
- 密钥从接入方自己的环境变量或密钥管理读入，不要写进代码或镜像。
- **持久卷**：`OUTBOX_PATH` 所在目录要挂在容器的持久卷上，并由部署事先建好（客户端不替你建目录）。放在容器的
  临时层上，重建容器就会丢掉还没送达的用量。目录不存在时客户端照样构造，但进入降级运行并报 P1（见第 9 节）。
- **凭据轮换**：平台轮换时 `api_key` 不变、版本号加 1，旧版本有重叠期（缺省 7 天）。在重叠期内把
  `KEY_VERSION` 与 `API_SECRET` 换成新值并重启即可；积压的事件每次发送都按当前配置重新签名。
  webhook 密钥轮换：重叠期内在 `WEBHOOK_SECRETS` 里同时放新旧两个版本，例如 `1:<旧密钥>,2:<新密钥>`。
- **主机时钟**：接入方主机要做 NTP 同步，偏差超过 300 秒时平台回 401（见第 6 节）。

其余可调参数（投递间隔、批大小、保留期等）有缺省值，`from_env` 不读；要改时在它的结果上用
`dataclasses.replace`，同样会过一遍校验。最常要改的是已送达行的保留期：

```python
import dataclasses

from acuven_billing_client import BillingClientConfig

config = dataclasses.replace(
    BillingClientConfig.from_env(),
    # 不得短于中心的灾备重放窗口（见第 8 节）；缺省 7 天，最少 1 天。
    delivered_retention_seconds=14 * 86_400,
)
```

`delivered_retention_seconds` **必须不短于中心的灾备重放窗口**：中心从备份恢复后要靠本地还留着的 `DELIVERED`
行重放，保留期比窗口短，窗口前段的事件就已经被清理、重放不回来了。

### 2. 一个 outbox 文件只给一个进程用

- 客户端打开 outbox 时在 `<OUTBOX_PATH>.lock` 上取系统级排他锁，进程存活期间一直持有。第二个进程用同一个路径时
  取不到锁，进入降级运行（ERROR 日志写明被锁、`health()` 报 P1），每个投递间隔再试。锁文件不写内容，不要删。
- 第一版按单进程 uvicorn 设计。**多 worker 时每个 worker 配自己的 outbox 文件**（例如路径里带 worker 序号），
  绝不共用一个文件；正式支持多 worker 另过设计闸门。
- 同一个进程里只建一个 `BillingClient`，在线程之间共用（它是线程安全的）。

### 3. 生命周期：start 与 stop

```python
from contextlib import asynccontextmanager

from fastapi import FastAPI

from acuven_billing_client import BillingClient, BillingClientConfig

billing = BillingClient(BillingClientConfig.from_env())


@asynccontextmanager
async def lifespan(app: FastAPI):
    billing.start()
    yield
    billing.close(timeout=30)


app = FastAPI(lifespan=lifespan)
```

- 构造 `BillingClient` 只有配置不合法才抛错；存储打不开时不抛，进入降级运行（第 9 节）。
- `start()` 起两个守护线程：投递线程（每 10 秒一轮：回灌内存暂存 → 写待记事故 → 领取一批送到批量接口）与
  对账线程（启动后立即对账一次，之后每 240 秒，失败后 30 秒补一次）。两者的第一轮都立即执行。
- 退出时调 `stop(timeout)` 或 `close(timeout)`：`stop` 只停线程，`close` 再关掉 SQLite 并放掉单进程锁。
  正在发送的那一批不会丢，租约到期后被重领、重发（平台按 `event_id` 幂等）。
- 不想要后台线程的场合（脚本、测试）可以不调 `start()`，自己定时调 `deliver_once()` 与 `reconcile_status_once()`。

### 4. 记账：模型调用成功之后

```python
event_id = billing.record_llm_usage(
    provider="anthropic",
    model="<模型代码>",
    usage_type="LLM_TOKEN",
    input_tokens=usage.input_tokens,
    output_tokens=usage.output_tokens,
    cache_creation_input_tokens=usage.cache_creation_input_tokens,
    cache_read_input_tokens=usage.cache_read_input_tokens,
    request_id=call_id,
    conversation_id=conversation_id,  # 可选
    feature="chat.reply",  # 可选
)

billing.record_quantity_usage(
    provider="openai",
    model="<模型代码>",
    usage_type="AUDIO_SECOND",
    quantity=Decimal("12.5"),  # 只收 Decimal 或十进制字符串，不收 float
    unit="SECOND",
    request_id=call_id,
)
```

- **在模型调用成功之后**调用。`record_*` 返回前事件已经提交进本地 SQLite（存储故障时进有界的内存暂存），
  不联网、不会因为平台不可用而阻塞或抛错。它是一次毫秒级的本地提交，在 async 路由里可以直接调，也可以放进线程池，
  由你决定。
- 字段不合规抛 `ValueError`、不写 outbox：这是调用方的编程错误，开发期就会暴露。多传未知参数抛 `TypeError`。
  `usage_type` 是否在平台目录里客户端不知道，不在本地校验。
- **载荷不带租户与项目**：没有 `tenant_id` / `project_id` 参数，事件归属完全由签名凭据决定。
- **自带 `event_id`**（UUIDv7 小写带连字符，或 ULID）可以给「同一次模型调用重复记账」去重：同 id 同内容再记一次
  直接返回该 id、不新增。**传了 `event_id` 就必须显式给出 `occurred_at`**（带时区的 `datetime`），否则抛
  `ValueError`：缺省的 `occurred_at` 取调用时刻，第二次调用的内容必然不同。
- `EventConflictError` 只在你用同一个 `event_id` 传了**不同内容**时出现，这是接入方的 bug；正常的重复记账与
  崩溃后的重放都是同内容，返回已有的 id。
- 同一事件之后每次重试都原样发送写入时的字节，不会触发平台的 `IDEMPOTENCY_CONFLICT`。

### 5. 拦截：should_block_ai 在模型调用之前

```python
if billing.should_block_ai():
    raise HTTPException(status_code=402, detail="AI is paused for this account")
response = call_model(...)
billing.record_llm_usage(...)
```

- 只读本地 SQLite，不联网。只有收到过经校验的 `BLOCK_AI`（来自对账或 webhook）才返回真。
- 从未成功对账、读库失败、存储降级时都返回假（放行）：计费侧故障不中断 AI。
- `current_status()` 返回已应用的状态（10 个字段加 `applied_at`），没有时为 `None`。

### 6. 状态 webhook 路由（FastAPI）

平台在有效状态变化时推送签名的 POST。路由要把**原始路径、原始查询串与原始请求体**原样交给客户端：

```python
from fastapi import Request, Response

from acuven_billing_client import StatusStoreError, WebhookVerificationError


@app.post("/billing/status-webhook")
async def billing_status_webhook(request: Request) -> Response:
    body = await request.body()  # 原始字节，不要先 json 解析再序列化
    try:
        billing.handle_status_webhook(
            method=request.method,
            raw_path=request.scope["raw_path"],
            query_string=request.scope["query_string"],
            headers=request.headers.raw,
            body=body,
        )
    except WebhookVerificationError:
        return Response(status_code=401)  # 验签、时间窗、租户或项目不符
    except StatusStoreError:
        return Response(status_code=503)  # 验签通过但本地写不进去，平台会退避重发
    return Response(status_code=204)  # 已应用，或旧版本 / 同版本（不是错误）
```

- 返回 2xx 的情况：应用了新状态，或收到的版本不比已存的新（`handle_status_webhook` 返回假，但不是错误）。
- `WebhookVerificationError` 回 **401**；`StatusStoreError` 回 **503**。不要把两者合并成一种返回。
- `ACUVEN_BILLING_WEBHOOK_SECRETS` 为空时一律 `WebhookVerificationError`。
- **反向代理改写了路径**（例如剥掉前缀 `/billing`）时，应用看到的路径与平台签名时用的不同，每次推送都会验签失败、
  状态只能靠对账（最多滞后约 4 分 50 秒）。这时传 `signed_path`：平台配置里那个 webhook 地址的路径加查询串，例如
  `signed_path="/acuven/billing/status-webhook?v=1"`；给了 `signed_path` 就不再用 `raw_path` 与 `query_string`。
- webhook 只是加速；对账每 240 秒兜底一次，webhook 不写对账成功时刻。

### 7. 健康检查与告警

把 `health()` 接进你自己的健康检查或监控（它不抛异常、不联网）：

```python
@app.get("/healthz/billing")
def billing_health() -> dict:
    return billing.health()
```

返回的键固定：`store_available`、`pending`、`sending`、`dead_unacked`、`incidents_unacked`、
`oldest_pending_age_seconds`、`held_in_memory`、`incidents_pending`、`dropped_events`、`conflicts_dropped`、
`last_delivery_ok_at`、`last_status_reconcile_ok_at`、`status_reconcile_stale`、`auth_failing`、`level`。
时刻都是 Unix 秒。

`level` 与平台 `ai_usage_outbox` 维度同口径：

| level | 条件 | 含义与处置 |
| --- | --- | --- |
| `P1` | `store_available` 为假，或 `dead_unacked > 0`，或 `incidents_unacked > 0`，或 `incidents_pending > 0`，或 `held_in_memory > 0`，或 `auth_failing`；读库失败也是 P1 | 有用量只在内存里（此时重启会丢）、存储不可用、有死信或未确认的事故、凭据被拒。立即处理 |
| `P2` | `oldest_pending_age_seconds > 900`，或 `status_reconcile_stale`（最近一次对账成功早于 300 秒或从未成功） | 平台不可达或 `base_url` 配错导致积压；状态可能滞后 |
| `OK` | 其余 | — |

`dropped_events` 与 `conflicts_dropped` 是进程启动以来的累计数，只作展示、不参与 `level`；事故确认后 P1 即解除。

**401 的固定 60 秒重试**：平台回 401 `INTEGRATION_AUTH_FAILED`（密钥、版本错，或主机时钟偏差超过 300 秒）时，
事件**不进死信**，保持待发、每 60 秒按当前配置重签重发一次，`auth_failing` 为真、报 P1。修好配置或时钟并重启后，
最多约 60 秒积压就会送达、P1 消失，不需要人工重投。（这一点与 `docs/api.md` 错误表的建议不同，以本客户端为准。）

日志用 logger `acuven_billing_client`，只带 `event_id`、错误码、条数、状态码与状态版本，不记密钥、签名与载荷。

### 8. 运维处置

处置分两种写法，**取决于有没有进程正在用这个 outbox 文件**。

**进程运行中**：一律通过进程内 `BillingClient` 的方法处置。命令行对正在使用的文件取不到单进程锁，会退出 3。
接入方自己暴露一个**受保护的**入口（只给运维、要鉴权，不对公网开放）转调这些方法，**原因必填**（去掉首尾空白后
1 到 500 个字符），每次处置都会在同一事务里写一条只增不改的 `ops_log`：

| 方法 | 用途 |
| --- | --- |
| `list_dead(limit)` / `requeue_dead(event_id, reason=...)` / `ack_dead(event_id, reason=...)` | 看死信；原因修好后重投，或确认放弃（保留行） |
| `replay_delivered(delivered_from=..., delivered_until=..., reason=...)` | 把这段时间（左闭右开，带时区的 `datetime`）里送达的事件改回待发，返回条数 |
| `list_incidents(limit)` / `ack_incident(incident_id, reason=...)` | 看与确认人工对账事故 |
| `reset_status(reason=...)` | 删掉本地已应用的状态，`should_block_ai()` 立即为假，下一次对账或 webhook 重新应用 |

```python
from datetime import datetime

from fastapi import Body, Depends

from acuven_billing_client import StoreUnavailableError


# require_ops_admin 是接入方自己的运维鉴权依赖。
@app.post("/ops/billing/replay", dependencies=[Depends(require_ops_admin)])
def billing_replay(
    delivered_from: datetime = Body(),
    delivered_until: datetime = Body(),
    reason: str = Body(),
) -> dict:
    try:
        count = billing.replay_delivered(
            delivered_from=delivered_from, delivered_until=delivered_until, reason=reason
        )
    except StoreUnavailableError:
        return {"ok": False, "error": "store unavailable"}
    return {"ok": True, "replayed": count}


@app.post("/ops/billing/reset-status", dependencies=[Depends(require_ops_admin)])
def billing_reset_status(reason: str = Body(embed=True)) -> dict:
    return {"ok": True, "deleted": billing.reset_status(reason=reason)}
```

`requeue_dead`、`ack_dead`、`ack_incident` 同样各开一个入口。原因为空抛 `ValueError`；存储降级时处置方法抛
`StoreUnavailableError`。

**没有进程在用的文件**（停机后的文件、运行中隔离出来的旧文件）才用命令行。它只读写本地 SQLite，不联网、不需要凭据：

```text
python -m acuven_billing_client --outbox <文件> stats
python -m acuven_billing_client --outbox <文件> dead [--limit N]
python -m acuven_billing_client --outbox <文件> requeue <event_id> --reason <原因>
python -m acuven_billing_client --outbox <文件> ack <event_id> --reason <原因>
python -m acuven_billing_client --outbox <文件> replay --delivered-from <时刻> --delivered-until <时刻> --reason <原因>
python -m acuven_billing_client --outbox <文件> incidents [--limit N]
python -m acuven_billing_client --outbox <文件> ack-incident <incident_id> --reason <原因>
python -m acuven_billing_client --outbox <文件> reset-status --tenant-id <id> --project-id <id> --reason <原因>
```

- 时刻是带时区的 ISO 8601，例如 `2026-10-20T08:00:00+00:00`。
- 输出是 JSON（`stats` 一个对象，`dead` 与 `incidents` 每行一个对象）。`stats` 不含 `level` 与四个进程内计数：
  命令行看不到运行中进程的内存。
- 退出码：`0` 成功；`1` 文件不存在（不会建文件）、目标不存在或状态不对；`2` 参数错；
  **`3` 文件正被运行中的进程使用**（这时改用上面的进程内入口）；`4` 其他存储错误。

**中心从备份恢复之后**（由中心的恢复流程通知），按这个顺序做：

1. **先 `reset_status`**（进程运行中走受保护入口；停机时用命令行 `reset-status`）。恢复会让状态版本回到较小的值，
   客户端按版本规则会一直忽略它们；本地若存着恢复前的 `BLOCK_AI`，不重置就会一直拦截 AI。
2. **再按时间段 `replay_delivered`**：`delivered_from` 取**早于中心恢复点**的时刻（留出余量），`delivered_until`
   取当前时刻。中心恢复点之后回过 202 的事件可能已经丢了；重放后平台按 `event_id` 幂等，没丢的回 `already_*`，
   丢了的重新入账。这一步要求本地还留着这些 `DELIVERED` 行，所以保留期须不短于中心的灾备重放窗口（第 1 节）。

### 9. 存储不可用时的降级运行与损坏文件的隔离

构造时或运行中本地 SQLite 不可用（目录不存在、磁盘满、I/O 错误、文件损坏或不是 SQLite、格式版本比代码新、
被另一个进程锁住）时，客户端**不抛异常，降级运行**：

- `store_available` 为假，`health().level = P1`，ERROR 日志写明异常类型与文件路径；
- `record_*` 校验后进内存暂存（上限 1000 条）；暂存满了再来的事件被丢弃，记进待记事故；
- `should_block_ai()` 返回假（放行）、`current_status()` 返回 `None`；处置方法抛 `StoreUnavailableError`；
  投递与对账暂停；
- 每个投递间隔（以及每次 `deliver_once()` / `reconcile_status_once()`）尝试重新打开；打开成功后自动回灌暂存、
  把待记事故写进 `incidents` 表，恢复投递与对账。磁盘满、被锁等问题解决后不需要人工介入。

**文件损坏时**（`SQLITE_CORRUPT` / `SQLITE_NOTADB`），客户端不会再打开同一个文件，需要运维把它换走。步骤：

1. **不要停进程**。降级期间的用量只在内存暂存与待记事故里，停进程就全部丢失。
2. 在进程运行中，把主库与 `-wal`、`-shm` **三个文件一起**移到别处隔离保存，**保持配对的名字**，不要删除：

   ```text
   mv /data/billing/outbox.db     /data/billing/quarantine/outbox.db
   mv /data/billing/outbox.db-wal /data/billing/quarantine/outbox.db-wal
   mv /data/billing/outbox.db-shm /data/billing/quarantine/outbox.db-shm
   ```

   `-wal` 里可能有已提交但还没写回主库的事件，只移主库会把它们和主库分开。`<OUTBOX_PATH>.lock` 留在原处。
3. **只移了主库时**，原路径上残留的 `-wal` / `-shm` 会让客户端保持降级、不新建库，ERROR 日志点名还在的文件。
   把它们按配对名字补放到被隔离的主库旁边（例如 `outbox.db-wal` → `quarantine/outbox.db-wal`），不要分开放或删掉。
4. 三个文件都不在原路径后，下一个投递间隔（或下一次 `deliver_once()`）客户端会新建空的 outbox，
   `store_available` 变真，暂存回灌入库、待记事故写进 `incidents`。
5. 等 `health()` 里 `held_in_memory` 与 `incidents_pending` **都归零**，再对隔离的旧文件用命令行读：

   ```text
   python -m acuven_billing_client --outbox /data/billing/quarantine/outbox.db stats
   python -m acuven_billing_client --outbox /data/billing/quarantine/outbox.db dead
   python -m acuven_billing_client --outbox /data/billing/quarantine/outbox.db incidents
   ```

6. 读不出，或读出还没送达的事件与未确认的事故，按下一节的人工对账处理。客户端不提供把旧文件里的事件导入新文件的命令。

### 10. 事故的人工对账与 ack-incident

本地写不进 outbox 而丢掉的用量会记成 `incidents` 表里的事故，未确认前一直报 P1，进程重启也不会消失：

- `DROPPED`：存储故障期间内存暂存已满而丢弃的事件（记 `event_id`）；
- `CONFLICT_DROPPED`：回灌时库里已有同 `event_id`、内容不同的行而丢弃（记 `event_id`）；
- `OVERFLOW_DROPPED`：待记事故列表也满了之后又丢弃的若干条，只有条数与起止时刻。

处置：用 `list_incidents`（运行中）或命令行 `incidents`（文件没有进程在用时）列出未确认的事故；按 `event_id` 与
时间段，对照接入方自己的调用记录与模型供应商的账单，确认哪些用量没有进入平台，按平台的人工对账流程处理；处理完
用 `ack_incident(incident_id, reason=...)`（或命令行 `ack-incident`）逐条确认，原因写清处理结果。只记 id 与条数，
事故里没有载荷。死信同理：查明原因后 `requeue_dead` 重投或 `ack_dead` 确认放弃。

### 11. 已知窗口

下面几种情况客户端无法自动补救，接入时要知道：

- **模型调用成功与写 outbox 之间**：客户端与接入方自己的数据库没有事务关系，接入方先调模型、后记账，两者之间进程
  崩溃会丢这一次用量。用自己的 `event_id`（同时给 `occurred_at`）记账，可以在重放时补记而不重复。
- **内存暂存与待记事故**：存储故障期间，进程在回灌之前退出，暂存里的事件与还没写进 `incidents` 的待记事故都会丢失，
  退出后也无法告警；平时看 `held_in_memory` 与 `incidents_pending`，大于 0 即 P1。
- **outbox 不在持久卷上**：重建容器会丢积压事件；客户端无法判断目录是不是持久卷。
- **发出后、落回执前崩溃**：这批停在 `SENDING`，租约到期后重领重发，平台按 `event_id` 幂等回 `already_*`，不会重复计费。

这些窗口的后果都是少计用量（不会多扣），由平台的用量与模型侧账单对账发现。

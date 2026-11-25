# RUNBOOK — 故障处置

> spec §136 要求 runbook 覆盖 18 个故障场景。**这份文件按场景逐个补，不预留空条目** ——
> 空标题会让人以为「已经有预案了」。
> 最后更新：2026-11-25

---

## spec §136 场景对照表

| # | spec §136 场景（原文） | 对应章节 |
| --- | --- | --- |
| 1 | Billing API down | [Billing API 不可用](#billing-api-不可用) |
| 2 | MySQL down | [MySQL 不可用](#mysql-不可用) |
| 3 | Redis down | [Redis / Celery broker 不可用](#redis--celery-broker-不可用) |
| 4 | Celery backlog | [Celery 队列积压](#celery-队列积压)；[celery-beat 停止调度](#celery-beat-停止调度) |
| 5 | Usage backlog | [用量计费：上线前步骤、错误事件、积压与卡住回收](#用量计费上线前步骤错误事件积压与卡住回收)；[AI 模块用量 outbox：死信、暂存、待复核与积压](#ai-模块用量-outbox死信暂存待复核与积压)；[用量摄取：幂等冲突与摄取中断](#用量摄取幂等冲突usage_event_conflicts-告警与摄取中断) |
| 6 | payment webhook failure | [支付：复核、事件冲突、事件积压与配置](#支付复核事件冲突事件积压与配置wallet_negative_balance-告警的支付部分) |
| 7 | stale pending payment and payment reconciliation failure | [支付：复核、事件冲突、事件积压与配置](#支付复核事件冲突事件积压与配置wallet_negative_balance-告警的支付部分)（`stale pending`、`oldest open event`、`review open` 三行）；[过期之后到账](#过期之后到账expired-之后的付款成功) |
| 8 | pricing error | [用量计费：上线前步骤、错误事件、积压与卡住回收](#用量计费上线前步骤错误事件积压与卡住回收)（`PRICING_ERROR`） |
| 9 | FX rate error and synchronization failure | [FX 汇率：拉取失败](#fx-汇率拉取失败fx_fetch-告警)；[FX 汇率：过期，或没有生效的汇率](#fx-汇率过期或没有生效的汇率fx_stale-告警)；[FX 汇率：发布了错误的汇率](#fx-汇率发布了错误的汇率退役与补发)；[FX 告警维度不上报](#fx-告警维度不上报healthchecks-报-fx_fetch--fx_stale未上报) |
| 10 | unknown model | [用量计费：上线前步骤、错误事件、积压与卡住回收](#用量计费上线前步骤错误事件积压与卡住回收)（`MODEL_UNKNOWN`） |
| 11 | negative wallet | [钱包核对发现不一致](#钱包核对发现不一致wallet_negative_balance-告警-p1)；[用量计费：上线前步骤、错误事件、积压与卡住回收](#用量计费上线前步骤错误事件积压与卡住回收)（负余额） |
| 12 | status webhook failure | [状态 webhook：推送连续失败、缺签名密钥、签名错误](#状态-webhook推送连续失败缺签名密钥签名错误status_webhook-告警) |
| 13 | out-of-order status delivery | [状态推送乱序](#状态推送乱序) |
| 14 | backup failure and full restore | [备份失败：全量备份与 binlog 离机](#备份失败全量备份与-binlog-离机)；[整台 VPS 没了（灾难恢复）](#整台-vps-没了灾难恢复) |
| 15 | encryption master-key recovery | [主密钥（`master.key`）丢失或要在新主机上重建](#主密钥masterkey丢失或要在新主机上重建) |
| 16 | document-volume recovery | [文档存储恢复](#文档存储恢复)；[整台 VPS 没了（灾难恢复）](#整台-vps-没了灾难恢复) |
| 17 | late prior-period usage and cross-period rebill | [上期用量与跨期调整](#上期用量与跨期调整)；[月度对账单](#月度对账单)；[Rebill](#rebill) |
| 18 | account closure with unresolved financial events | [关户](#关户) |

这 18 行与 spec §136 的 runbook 清单逐条对应、顺序相同，场景名照抄原文。**spec 改了这份清单，要同步改这张表**；新增或改名章节时，
表里的链接一起改。

---

## 本文件的写法

每个场景固定五节：**怎么发现 · 影响什么 · 立刻做什么 · 恢复 · 绝不能做什么**。

「绝不能做什么」这一节不是客套。故障处置里最贵的错误都不是「没做对」，而是
「做了不该做的」—— 半夜被叫醒的人手边只有这份文档。

文件末尾的「配置项」一节不是故障场景，不套这五节：每个配置项固定写 **含义 · 默认值与取值规则 · 怎么改 ·
改完怎么生效 · 对已有数据的影响 · 绝不能做什么**。

---

## Billing API 不可用

**首次编写：2026-11-22（AIH-TASK-230）。依据：`deploy/monitor.sh:138-164`（services 维度）、`deploy/monitor.sh:193-223`（readyz 维度）、
`docker-compose.yml:241-253`（api 的容器健康检查）、`deploy/nginx/billing.conf:88-107`（上游与超时）、[deployment.md](deployment.md) §8.1、§9.5。**

这里的 API 指 `api` 容器：计费 API、管理端与客户门户的后端、Stripe 回调与 AI 模块（`/v1/messages`）在同一个进程里。
`celery-worker` 与 `celery-beat` 是另外的容器，不经过它。

### 怎么发现

| 信号 | 说明 |
| --- | --- |
| Telegram：`ai_billing_hub services` 变 DOWN，正文 `P1 api is not running`、`P1 api state=<状态>` 或 `P1 api health=<状态>` | **主信号**。巡检每 5 分钟一次，发现问题先隔 45 秒复核再发（部署换版本时也会短暂不健康） |
| Telegram：`ai_billing_hub readyz` 变 DOWN，正文 `P1 readyz is not answering 2xx (<URL>)` | API 挂了时 `/readyz` 连不上；同一维度通常还有 `P1 billing_ai_readiness_failed` 一行。⚠️ 维度已红时第二个原因不会再通知一次，看 `journalctl -t billing-monitor -p err` 的**全部**行 |
| 告警维度的检查一起「未上报」，巡检日志 `alerts endpoint is not answering 200` | 巡检读不到 `/internal/alerts` 时刻意不推告警维度，见「FX 告警维度不上报」 |
| 对外请求 502 | 边缘 nginx 连不上 api：`proxy_connect_timeout 2s`，api 停掉时约 4 秒回 502（`deploy/nginx/billing.conf:92-102`） |
| 对外请求 503（JSON 信封） | 不是 nginx 自己回的：api 活着、但它的依赖不可用（数据库不通时 `/readyz` 是 503 `DATABASE_UNAVAILABLE`）—— 按「MySQL 不可用」处理 |

⚠️ **api 的容器健康检查探的是 `/healthz`（存活），不是 `/readyz`**（`docker-compose.yml:242-244`）：数据库不通时 api 仍然是
`healthy`。所以 `api health=unhealthy` 说明进程自己不应答；`readyz` 红而 `api` 那一行是绿的，多半是数据库。
`billing_nginx` 那一行同时报时先看边缘 nginx：巡检的 `/readyz` 也经过它。

### 影响什么

| 仍然正常 | 受影响 |
| --- | --- |
| MySQL 里已落库的一切：用量事件、账本、支付与网关事件、outbox 行 | 用量摄取：集成方拿不到 2xx，按 `retryable` 退避重试，事件留在集成方的 outbox（[api.md「错误与 `retryable`」](api.md#错误与-retryable)） |
| celery-worker / celery-beat：计费扫描、投递、状态推送、对账照常（它们读写数据库，不经过 api） | Stripe 回调收不到 2xx，Stripe 自动重试约三天（见「支付」一节） |
| 集成方按最后一次接受的状态放行或拦截（[api.md「集成方怎么应用」](api.md#集成方怎么应用)） | 管理端、客户门户、AI 调用（`/v1/messages`）不可用；进程非正常退出时 AI 模块内存里的暂存缓冲随之丢失，只留 `AI usage not persisted` 日志 |
| | 告警维度不上报（见上） |

### 立刻做什么

1. 分清是 api、边缘 nginx 还是数据库：`docker compose ps`，看 `api`、`billing_nginx`、`mysql` 三行
2. 看存活与就绪，不自己拼探测地址，用巡检已有的两条路：
   - **存活（`/healthz`）**：就是上一步 `api` 那一行的 health —— 容器健康检查探的正是 `/healthz`（`docker-compose.yml:241-244`）
   - **就绪（`/readyz`）**：巡检在宿主机上按 `BILLING_MONITOR_READYZ_URL`（默认由 `.env` 的 `BILLING_HTTP_PORT` 拼出）探它
     （`deploy/monitor.sh:102-105`、`deploy/monitor.sh:193-207`）。看最近一轮的结论：`journalctl -t billing-monitor -p err`；
     要一份当下的结论就手工跑一次 `BILLING_MONITOR_RECHECK_SECONDS=0 bash deploy/monitor.sh`（同第 6 条）

   `api health=unhealthy` 或 `api is not running` = api 进程的问题，往下走；`api` 是 `healthy` 而 `P1 readyz is not answering 2xx`
   = 多半是数据库，转「MySQL 不可用」
3. 看 api 日志：`docker compose logs --since 30m api | tail -200`
4. **是不是刚部署过**：`cat .last-good-deploy`，对照 GitHub Actions 的 Deploy 运行记录。`deploy.sh` 健康检查或冒烟没过时会自动
   回滚并以非零退出；新版本已经上线而本身有问题时，按 [deployment.md](deployment.md) §9.5 手工回滚（Deploy workflow 的 `ref`
   填上一个好的 SHA）
5. 不是版本问题、进程卡死或反复崩溃：`docker compose restart api`。影响面见「xlsx 导出失败」一节「恢复」第 2 条
   （优雅排空最长 85 秒、暂存缓冲最后一次落库限时 5 秒）
6. 修完手工跑一次：`BILLING_MONITOR_RECHECK_SECONDS=0 bash deploy/monitor.sh`

### 恢复

1. `api` 是 `healthy`（`/healthz` 通）；手工跑一次巡检时 readyz 维度没有问题行（`/readyz` 200 且 `data.status` 为 `ok`）；下一轮巡检 `ai_billing_hub services` 与 `ai_billing_hub readyz` 回到
   **up**（前提是同一维度没有别的行），告警维度恢复上报
2. 停机期间的 `AI usage not persisted` 日志行，按「AI 模块用量 outbox」一节的「积压与从日志补录」补录；`HELD` 与待复核按同一节处理
3. 集成方的积压由它们自行补投，已经落库的事件重投得到 `200 already_received`，不会重复计费（见「用量摄取」一节）；
   Stripe 的重投按「支付」一节消化
4. 停机跨过会计期 cut-off 时，那段用量会进上期调整，见「上期用量与跨期调整」

### 绝不能做什么

- **不要把 api 的容器健康检查改成 `/readyz`**：数据库一抖，API 就会被反复重启（`docker-compose.yml:242-244`）
- **不要用 `docker compose down` 代替 `restart`**：会删网络与容器，扩大影响，也丢掉了部署的回滚落脚点（[deployment.md](deployment.md) §9.6）
- **不要在 VPS 上改代码或现场构建镜像来「先修好」**：版本有问题只走 §9.5 的回滚
- **不要为了让巡检变绿去改 `BILLING_MONITOR_READYZ_URL`，或放宽 `/readyz` 的 nginx 名单**：它们与 `/internal/alerts` 同一份名单

---

## MySQL 不可用

**首次编写：2026-11-22（AIH-TASK-230）。依据：`app/api/health.py:53-123`、`app/core/database.py:36-48`、`app/core/database.py:111-124`、
`app/ai_api/routes.py:105-177`、`app/ai_api/outbox.py:184-252`、`app/ai_api/held.py:52-247`、`docker-compose.yml:185-194`、
[api.md「错误与 `retryable`」](api.md#错误与-retryable)、[deployment.md](deployment.md) §9.6（一次真实的数据库停机）。**

### 怎么发现

| 信号 | 说明 |
| --- | --- |
| Telegram：`ai_billing_hub services` 变 DOWN，正文 `P1 mysql is not running` 或 `P1 mysql health=<状态>` | 容器健康检查是 `mysqladmin ping`（每 10 秒，连续 12 次失败才算不健康） |
| Telegram：`ai_billing_hub readyz` 变 DOWN，正文 `P1 readyz is not answering 2xx (<URL>)` | `/readyz` 与 `/healthz/billing`（同一个函数）在数据库不通时返回 **503** `DATABASE_UNAVAILABLE`（没配数据库地址时 `DATABASE_NOT_CONFIGURED`），信封里不说原因；原因在 API 日志 `Database readiness probe failed`（带脱敏异常栈） |
| 同一维度 `P1 billing_ai_readiness_failed` | `/healthz/ai` 503 `AI_STORE_UNAVAILABLE`，日志 `AI readiness database probe failed` |
| 告警维度一起「未上报」 | `/internal/alerts` 查库失败返回 503，巡检日志 `alerts endpoint is not answering 200` |
| `ai_billing_hub binlog` 收到 `/fail`，正文 `cannot reach the database` | binlog 离机每分钟连一次库（见「备份失败」） |

⚠️ `/healthz` 仍然是 200、api 容器仍然是 `healthy`：这是刻意的（`app/api/health.py:1-10`），数据库恢复期间不让 API 被反复重启。

### 影响什么

| 路径 | 行为（以代码为准） |
| --- | --- |
| 用量摄取 | 503 `SERVICE_UNAVAILABLE`、`retryable: true`，事件没有落库；集成方保留事件、退避重试，恢复后补投 |
| AI 调用：鉴权就读不到库 | 503 `AI_STORE_UNAVAILABLE`，**不调用模型、不产生用量**（`app/ai_api/routes.py:105-123`） |
| AI 调用：模型已经返回、写用量时库断了 | 写不进 `ai_usage_outbox`：CRITICAL 日志 `AI usage not persisted`（带 `commit_outcome`），条目进 api 内存里的暂存缓冲，请求返回 503 `AI_USAGE_NOT_PERSISTED`（`app/ai_api/outbox.py:195-252`、`app/ai_api/routes.py:166-177`）。缓冲每 `BILLING_AI_HELD_RETRY_SECONDS`（默认 30 秒）重试一次，写进库时是 `HELD` 行；上限 `BILLING_AI_HELD_BUFFER_MAX`（默认 1000），满了丢掉最老的一条并记 CRITICAL `AI usage held buffer overflow`（`app/ai_api/held.py:69-74`）；api 停机时最后一次落库限时 5 秒，落不完的只剩日志 |
| celery-worker / beat 的任务 | 计费扫描、投递、状态推送、钱包核对、对账单生成这一轮读写失败、放弃；数据库就是待办清单，恢复后下一轮接着做 |
| 管理端、客户门户 | 读写数据库的接口失败 |
| 已经提交的数据 | 不丢：MySQL 的数据在命名卷 `mysql-data`（`docker-compose.yml:185-186`） |
| 备份 | binlog 离机与当天的全量都会失败、各自发 `/fail`（见「备份失败」） |

### 立刻做什么

1. 看容器与日志：`docker compose ps mysql`；`docker compose logs --since 30m mysql | tail -200`
2. 看磁盘：`ai_billing_hub disk` 是否同时红着、`df -h`。磁盘写满时 MySQL 停止写入；binlog 占满的处置见
   [deployment.md](deployment.md) §5.2.2 末尾（**先查离机是不是坏了**，删文件是最后一步）
3. 是不是部署引起的（例如网络重建把 mysql 停了）：对照 Deploy 运行记录，按 [deployment.md](deployment.md) §9.6 的经过与恢复方式处理
4. 容器停了、数据文件完好：`docker compose up -d mysql`，等它 `healthy`
5. 起不来、数据文件损坏：不要现场修数据文件，按「整台 VPS 没了（灾难恢复）」第 6–8 步从备份恢复到新库，**对账通过之后**再切换
6. 修完手工跑一次：`BILLING_MONITOR_RECHECK_SECONDS=0 bash deploy/monitor.sh`

### 恢复

1. `mysql` 是 `healthy`；`/readyz` 200 且 `data.database` 为 `ok`；`/healthz/ai` 200；巡检的 services 与 readyz 回到 **up**
2. AI 暂存：缓冲里的条目由重试写成 `HELD` 行，`ai_usage_outbox` 维度的 `held` 随之非零（P1）—— 按「AI 模块用量 outbox」一节的
   `HELD` 与「待复核」处理；api 在这期间重启过的，从 `AI usage not persisted` 日志行补录（同一节）
3. 集成方补投；计费积压由扫描自动消化，盯 `usage_processing_backlog`。只读核对：`SELECT status, COUNT(*) FROM usage_events GROUP BY status`
4. binlog：恢复后下一分钟的离机推走积压，`ai_billing_hub binlog` 转绿；报 `gap in the binlog chain` 时按「备份失败」处理
5. 停机跨过会计期 cut-off 时，见「上期用量与跨期调整」

### 绝不能做什么

- **不要重启 api 指望数据库「好起来」**：api 没坏；重启只会让暂存缓冲走一次限时 5 秒的最后落库，落不完的条目只剩日志
- **不要直接 UPDATE `ai_usage_outbox` 把 `HELD` 改成 `PENDING`**：只用 `ai-usage-held release` / `discard`（「AI 模块用量 outbox」一节）
- **不要 `docker compose down -v`**：`-v` 会连 `mysql-data` 卷一起删掉
- **不要为了腾磁盘删 `/var/lib/mysql` 下的文件或还没离机的 binlog**：见 [deployment.md](deployment.md) §5.2.2
- **不要在数据库恢复之前手工重跑财务任务**：任务幂等，待办都在库里，恢复后自动接上

---

## AI 模块用量 outbox：死信、暂存、待复核与积压

告警维度 `ai_usage_outbox` 的 `dead`、`held`、`review required` 任一非零都是 P1；最老的
`PENDING` / `SENDING` 超过 `BILLING_AI_PENDING_ALERT_SECONDS`（默认 900 秒）是 P2。先从
`/internal/alerts` 的计数确认类别，再查只含元数据的应用日志；不要把提示词、模型回答或密钥复制到工单。

### DEAD

查 `ai_usage_outbox` 的 `status='DEAD'`、`last_error` 与 `dead_acked_at`。只有
`INTEGRATION_AUTH_FAILED` 与 `INVALID_BILLING_RESPONSE` 可以在根因修好后用
`python -m app.cli ai-usage-requeue <event_id> --reason '<原因>'` 重投；其余错误不能反复重投。
已处理且决定不重投的死信，用 `ai-usage-dead-ack` 留审计并解除该条告警。

### HELD

`HELD` 表示模型已经消耗用量，但请求路径未能确认写入；它不会被投递 worker 领取。核对客户约定后：

- 照收：`python -m app.cli ai-usage-held release <event_id> --reason '<决定依据>'`，变为 `PENDING`；
- 不收：`python -m app.cli ai-usage-held discard <event_id> --reason '<决定依据>'`，变为终态 `DISCARDED`。

绝不直接更新数据库状态。两条命令与审计在同一事务中提交。

### 待复核

`review_required_at` 有值而 `reviewed_at` 为空，表示请求曾返回 503、但同一 `event_id` 可能已进入投递。
先在管理端核对摄取、计费与客户影响；决定保留费用，或先用管理端的手工调账退回。完成该业务决定后运行
`python -m app.cli ai-usage-held ack <event_id> --reason '<核对结果>'`。不要用 `discard` 代替复核：该行可能已送达。

### 积压与从日志补录

`PENDING` / `SENDING` 积压先确认 api、celery-beat、celery-worker 与摄取端可用；恢复后扫描会自行补投。
不要手工改 `next_attempt_at` 或多开 worker。若 API 在暂存落库前重启，只能从消息恰为
`AI usage not persisted` 的 JSON 日志行补录：

`python -m app.cli ai-usage-held import --from-log-line '<完整 JSON 行>' --reason '<事故编号>'`

命令会逐字段校验、核对日志中的租户与项目归属，并打印 `created`、`held`、`review_marked` 或
`unchanged:<状态>`；`rejected` 必须按输出修正输入或升级事故，不会写审计。每次成功补录都留下审计。

---

## AI 就绪检查失败（`billing_ai_readiness_failed` 告警）

**首次编写：2026-10-18（AIH-TASK-075）。依据：[deployment.md](deployment.md) §8.1、「AI 模块部署前置与配置」（AIH-TASK-068 / 069）。**

### 怎么发现

```text
名称：billing_ai_readiness_failed
条件：宿主机每 5 分钟在 /readyz 之后 GET /healthz/ai（BILLING_MONITOR_AI_READY_URL 可覆盖），
     非 2xx，或正文不含 "status":"ok"（前端兜底的 200 HTML 也算）
分级：P1（AI 请求路径不可用）
通知：并在 Healthchecks.io 检查 `ai_billing_hub readyz` 里 → Telegram；状态翻转才发，恢复再发一次
正文：P1 billing_ai_readiness_failed: <URL> (2xx: yes|no) —— 不带响应体
```

⚠️ **它没有自己的检查**（免费档 20 个已用满）。`readyz` 已经因为别的原因红着时，AI 就绪坏掉**不会再通知一次**；
收到 readyz 的通知后要看 `journalctl -t billing-monitor -p err` 里的**全部**行。`2xx: yes` 表示拿到的是 200 但不是
就绪信封 —— 多半是 nginx 没把 `/healthz/ai` 交给 API（配置没 reload 上），走前端兜底了。

### 影响什么

| 仍然正常 | 受影响 |
| --- | --- |
| 计费路径、`/readyz`、用量摄取、管理端 | AI 请求路径：key 文件读不出时上游调用发不出去；AI 专用库不通时请求无法落库 |
| 已经落库的 AI 用量 outbox（投递 worker 照常） | 新签发的 AI 密钥没法用 —— 部署前置要求先等 `/healthz/ai` 变绿 |

### 立刻做什么

先在宿主机上看错误码（只读，回环地址；**不要**把响应贴进工单以外的地方）：

```bash
curl -sS -w '\n%{http_code}\n' http://127.0.0.1:<端口>/healthz/ai
```

| 现象 | 先查 |
| --- | --- |
| 503，`AI_PROVIDER_NOT_CONFIGURED` | **key 文件**（第 1 步） |
| 503，`AI_STORE_UNAVAILABLE` | **AI 专用库连接**（第 2 步） |
| 200 但是 HTML / 403 / 404 | nginx：`deploy/nginx/billing.conf` 的 `location = /healthz/ai` 与名单，是否 reload 过 |
| 连不上（`000`）、502 | API 挂了 —— `readyz` 那一行应同时是 P1，先按 API 故障处理 |

1. **key 文件**：宿主机 `secrets/anthropic.key` 是否存在、非空、属主 `10001`、权限 `0400`（与其它 secret 同一条，ADR-0004）；
   容器里 `BILLING_AI_ANTHROPIC_KEY_FILE`（默认 `/run/secrets/billing_anthropic_key`）是否指向它。
   ⚠️ 只看 `ls -l` 与 `sha256sum secrets/anthropic.key | cut -c1-8`，**不要打印内容**。修好文件后要重建 api 容器
   （secret 走 bind mount，Docker Desktop 下缺文件会被建成目录 —— 见「主密钥」一节）
2. **AI 专用库连接**：看 API 日志 `AI readiness database probe failed`（带脱敏异常栈）；`mysql` 容器与 `readyz` 的
   `database` 若都正常，问题在 AI 专用的连接池与线程：核对 `BILLING_AI_DB_*` 超时与并发（`BILLING_AI_DB_THREADS` /
   `BILLING_AI_DB_QUEUE_MAX`）—— 池子被慢查询占满、排队超限时也报 `AI_STORE_UNAVAILABLE`
3. 其余按 [deployment.md](deployment.md)「AI 模块部署前置与配置」与本文件「AI 模块用量 outbox」一节排查
4. 修完手工跑一次：`BILLING_MONITOR_RECHECK_SECONDS=0 bash deploy/monitor.sh`

### 恢复

1. `curl` 拿到 200 且 `data.status` 为 `ok`；下一轮巡检 `ai_billing_hub readyz` 转绿（前提是同一维度没有别的行）
2. 故障期间被 `HELD` 或待复核的 AI 用量，按「AI 模块用量 outbox」一节处理

### 绝不能做什么

- **不要把 key 写进 `.env`、镜像或命令行**来「先让它绿」。key 只放文件
- **不要为了让告警安静去改 `BILLING_MONITOR_AI_READY_URL`** 指向别的路径（例如 `/healthz`）：那条只是存活检查，永远是绿的
- **不要放宽 `/healthz/ai` 的 nginx 名单**：它与 `/readyz` 同一份

---

## Redis / Celery broker 不可用

**首次编写：2026-09-12（T0.5）。这是 §136 的 18 个场景里第一个落地的。**

### 怎么发现

| 信号 | 说明 |
| --- | --- |
| `GET /readyz` 返回 **200**，`data.redis == "unavailable"`、`data.status == "degraded"` | **主信号。** 注意状态码仍是 200 —— 见下 |
| 日志里 `logger=app.api.health`、`message="Readiness degraded"`、`component="redis"` 的 WARNING | 每次就绪探测各一条，可直接作为告警条件 |
| 日志里 `logger=app.core.broker`、`message="Redis probe failed"` 的 ERROR | 带异常栈（已脱敏），用于判断是连不上、超时还是认证问题 |

⚠️ **负载均衡不会替你发现这件事。**`/readyz` 在 Redis 不可用时**刻意**返回 200，
原因见下一节。因此这个故障**只能靠日志告警发现**，没有第二条路。

**告警**（2026-09-16 落地，见 [deployment.md](deployment.md) §8.1）：

```text
名称：billing_readiness_degraded_redis
条件：宿主机每 5 分钟 GET /readyz，响应体 data.status != "ok"
     （非 2xx 是另一回事：数据库不通或 API 挂了，按 P1 处理）
分级：P2（服务未中断，但投递在积压）
通知：Healthchecks.io 检查 `ai_billing_hub readyz` → Telegram；状态翻转才发，恢复再发一次
连续 30 分钟未恢复：升级 P1
```

⚠️ **判据是响应体，不是日志。**原先写的条件是「过去 5 分钟出现 >= 1 条 `message="Readiness degraded"` 且 `component="redis"` 的日志」—— 那需要日志聚合，而本阶段没有。日志那条判据等有了聚合仍然成立，**告警名不变**：它是这份文档与通知之间的契约。

### 影响什么

| 仍然正常 | 受影响 |
| --- | --- |
| 收用量事件、落库、返回 `202` | 异步投递**不发生**，事件停在数据库里 |
| `/healthz`、只读查询 | 周期任务（对账扫描、状态轮询）不执行 |
| 客户的 AI 服务 | 通知、Webhook 出站延迟 |

**没有数据丢失。** spec §74.6 规定 Redis/Celery 只承载投递触发，数据库 Outbox
才是可恢复的事实来源；Invariant 14 要求队列丢失不得摧毁已持久化的工作。
Redis 回来之后，欠下的投递由周期恢复补上。

积压的量级会随停机时长线性增长 —— 恢复后**要盯 worker 的消费速率**，别让补投
把数据库压垮。

### 立刻做什么

1. 确认是 broker 本身还是网络：在 API 容器里 `redis-cli -u "$BILLING_REDIS_URL" ping`
2. 看 Redis 进程 / 容器状态与内存（`maxmemory` 打满会让写入失败而 `ping` 仍然通 ——
   **这种情况 `/readyz` 会显示 `ok`，别被它骗了**）
3. **不要**动数据库，**不要**重放任何财务操作。事件都在库里，等投递恢复

### 恢复

1. 恢复 Redis 服务
2. 确认 `/readyz` 的 `data.redis` 变回 `"ok"`、`data.status` 变回 `"ok"`
3. 确认 worker 重新连上：`celery -A app.worker inspect ping`
4. 盯积压消化：队列长度回落、上期调整（`PRIOR_PERIOD_ADJUSTMENT`）笔数是否异常增长
   —— 跨了会计期 cut-off 的积压会变成上期调整（见 [ADR-0003](adr/ADR-0003-financial-period-and-cutoff.md)）

### 绝不能做什么

- **不要把 `/readyz` 在 Redis 不可用时改成 503。**`REQ-AVAIL-001` 要求 Redis
  不可用不得成为终端 AI 请求路径上的同步依赖。改成 503 会让所有实例被摘出
  轮转 —— 那正是 Invariant 1 要防的中断，而且是我们自己造出来的
- **不要清空队列**（`FLUSHDB` / `FLUSHALL`）来「让它快点恢复」。队列里是尚未
  投递的触发；清掉之后要靠周期恢复重建，恢复窗口反而更长
- **不要为了赶进度手工重跑财务任务。**任务是幂等的，但手工触发绕过了幂等键的
  来源（Outbox 行），可能造出重复效果

---

## celery-beat 停止调度

**首次编写：2026-09-13（T0.9）。§136 的 18 个场景里第二个落地的。**

⚠️ **这是本平台最安静的一种故障。**beat 不服务任何请求，所以它死了之后：API 正常、
`/healthz` 正常、`/readyz` 正常、worker 正常、日志里没有任何错误。唯一的现象是
**周期任务不再发生** —— 而「没发生」是没有信号的。

T0.8d 之后 beat 有了真实职责（outbox 的周期恢复），所以它停了就意味着：投递触发丢失
之后**再也没有人补投**，信静静地躺在 `domain_outbox` 里，状态永远是 `PENDING`。

### 怎么发现

| 信号 | 说明 |
| --- | --- |
| `docker compose ps` 里 `celery-beat` 显示 **unhealthy** | **主信号**（T0.9 加的探针） |
| Telegram 收到 `ai_billing_hub services` 变 DOWN，正文 `P2 celery-beat health=unhealthy` | 上面那条探针的**送达途径**（2026-09-16，`deploy/monitor.sh` 每 5 分钟巡检一次）。 ⚠️ 探针红了没人看，等于没有探针 |
| `domain_outbox` 里 `status='PENDING'` 且 `next_retry_at` 早于现在的行在累积 | 业务侧的表现，比探针慢但更贴近后果 |

探针的判据是 **beat 自己的调度状态文件有多久没动**：

```bash
docker compose exec celery-beat sh -c \
  'echo $(( $(date +%s) - $(stat -c %Y /var/lib/celery/beat-schedule) ))s'
```

⚠️ **`celery inspect ping` 对 beat 无效** —— 那问的是 worker。beat 不接受 inspect
调用，这就是为什么它需要一套单独的判据。

⚠️ **beat 每派发一次才落一次盘**（`beat_sync_every=1`，见 `app/core/celery_app.py`）。
默认值下是每 3 分钟才同步 —— 实测连续观察三个 60 秒周期文件时间戳纹丝不动，
拿默认行为做探针等于给它一个 3 分钟的盲区。改调度周期时，探针阈值要一起改。

### 影响什么

| 仍然正常 | 受影响 |
| --- | --- |
| 所有 API 端点、登录、密码重置的**受理** | **周期恢复停止**：投递触发一旦丢失就不再补投 |
| worker 仍在执行**被直接触发**的任务 | 密码重置邮件在触发丢失时永远发不出去 |
| 数据完整性 —— `domain_outbox` 是事实来源，行都在 | Phase 2 之后：对账扫描、状态轮询一并停摆 |

⚠️ **没有数据丢失**，这一点要先确认下来再动手。INV-14 的整套设计就是为了这个：
beat 停多久都不会毁掉已持久化的工作，恢复之后会自然补上。

### 立刻做什么

1. 确认是 beat 本身还是 broker：看 `redis` 与 `celery-worker` 两格的健康状态。
   ⚠️ Redis 挂了会让 worker 也红，但 **beat 的探针不经过 broker** —— 只有 beat
   红而其它绿，才是 beat 自己的问题
2. 看积压规模：

   ```sql
   SELECT status, COUNT(*) FROM domain_outbox GROUP BY status;
   ```

3. 重启 beat：`docker compose restart celery-beat`
4. ⚠️ **不要**手工去重放积压 —— 见下

### 恢复

1. beat 起来后确认探针转绿（起步宽限 90 秒，要等过一个调度周期）
2. 确认周期任务恢复：worker 日志里重新出现 `app.tasks.outbox.recover`
3. 盯 `PENDING` 行数回落。⚠️ 积压不会一口气全放出去：`beat_schedule` 的
   `expires` 比周期略短，攒下的重复触发会被丢弃 —— 这是设计，不是丢任务
4. 如果积压里有已经进 `FAILED` 的死信，那是**另一件事**，按投递失败单独处理

### 绝不能做什么

- **不要手工去 `UPDATE domain_outbox SET status='PENDING'`「帮它重试」。**
  周期恢复会自己捡起该捡的行；手工改状态会把已经进死信的行也放回去，
  而那些行是**重试到上限都发不出去**的，放回去只是让它们再烧一轮
- **不要把 beat 的探针阈值调大来「让它别报警」。**阈值的意义是「多久没派发算异常」，
  调大它只是把盲区变宽 —— 而这个故障本来就是靠这一个信号发现的
- **不要为了「快点恢复」同时起多个 beat。**两个 beat 会各自按自己的时钟派发，
  同一个周期任务被放进队列两次。任务本身幂等，但这会让积压排查更难读
- **不要在没确认 `domain_outbox` 行还在的情况下重建容器。**调度状态在命名卷
  `celery-beat-state` 里，`docker compose down -v` 会连它一起删掉

---

## Celery 队列积压

**首次编写：2026-11-15（AIH-TASK-208）。依据：[deployment.md](deployment.md) §8.1。**

⚠️ **容器照样是 healthy。**celery-worker 活着但处理不过来、或卡在某个任务上时，它的探针仍然通过，
API 一切正常 —— 唯一的现象是投递、计费、对账**慢慢滞后**。这条告警读的是 broker 里的队列长度。

### 怎么发现

```text
条件：宿主机每 5 分钟（services 维度逐个查完容器之后）
     timeout 10 docker compose exec -T redis redis-cli -n 0 LLEN celery
     （库号与 docker-compose.yml 的 BILLING_REDIS_URL 一致；celery 是 Celery 的默认队列）
分级：长度 ≥ 500 = P2（BILLING_MONITOR_CELERY_QUEUE_WARN）
通知：并在 Healthchecks.io 检查 `ai_billing_hub services` 里 → Telegram；状态翻转才发，恢复再发一次
正文：P2 celery queue backlog <N> (warn 500)
```

⚠️ **它没有自己的检查**（免费档 20 个已用满，2026-10-17 定）。`services` 已经因为别的原因红着时，积压**不会再通知一次**；
收到 services 的通知后要看 `journalctl -t billing-monitor -p err` 里的**全部**行。

读不到长度（redis 没在跑、命令失败或超时）时这项跳过、不报，日志里是
`celery queue length unavailable; skipping the queue check` —— redis 停了已由同一维度的 `redis` 那一行报出。

正常时队列接近 0：Beat 的周期任务只在 worker 跟不上时才会堆起来。⚠️ Redis 设了 `maxmemory` 与 `allkeys-lru`
（`docker-compose.yml`），积压再涨下去**消息会被淘汰**，那时队列反而变短 —— 告警消失不一定是恢复了。

### 影响什么

| 仍然正常 | 受影响 |
| --- | --- |
| 所有 API 端点，收用量事件、落库、返回 `202` | 异步投递、用量计费、对账扫描**滞后**，滞后随积压增长 |
| 数据完整性 —— 持久工作在 MySQL（`domain_outbox` 等），行都在 | 跨了会计期 cut-off 的积压会变成上期调整（见 [ADR-0003](adr/ADR-0003-financial-period-and-cutoff.md)） |

**没有数据丢失。** Redis 只承载触发（spec §74.6）；即便消息被淘汰，INV-14 也靠周期恢复补回。

### 立刻做什么

1. 看 celery-worker 日志，判断是**处理不过来**还是**卡在某个任务上**：

   ```bash
   docker compose logs --since 30m celery-worker | tail -200
   docker compose exec celery-worker celery -A app.worker inspect active
   ```

   同一个任务 id 一直出现在 `active` 里、日志长时间没有新的 `succeeded`，就是卡住了
2. 确认 Beat 没有重复调度：`docker compose ps celery-beat` 只有**一个**在跑；日志里同一个周期任务
   每个周期只派发一次。两个 beat 会让同一批周期任务进队列两次（见「celery-beat 停止调度」）
3. 再读一次长度，看是在涨还是在落：

   ```bash
   docker compose exec -T redis redis-cli -n 0 LLEN celery
   ```

4. 卡住或一直只涨不落时，重启 worker：`docker compose restart celery-worker`。进行中的任务会被中断，
   任务幂等、持久工作在 MySQL，由周期恢复补上

### 恢复

1. 队列长度回落到接近 0
2. 下一轮巡检 `ai_billing_hub services` 回到 **up**（前提是同一维度没有别的行）
3. 盯 `domain_outbox` 的 `PENDING` 行数与计费积压回落；反复积压时看 worker 并发（deployment.md §3.2）是否该调

### 绝不能做什么

- **不要清空 Redis 或删队列**（`FLUSHDB` / `FLUSHALL` / `DEL celery`）来「让它快点恢复」。持久工作在 MySQL，
  INV-14 靠周期恢复补回 —— 清队列丢掉的只是触发，**只会让恢复多等一轮**
- **不要调高 `BILLING_MONITOR_CELERY_QUEUE_WARN`** 来让告警安静：积压过大时 Redis 会按 `allkeys-lru` 淘汰消息，
  这条告警必须在那之前响
- **不要为了「快点消化」多起一个 beat**：同一批周期任务会被派发两次，积压只会更大

---

## TLS 证书即将到期或读不到

**首次编写：2026-11-03（AIH-TASK-150）。依据：[deployment.md](deployment.md) §8.1。**

⚠️ **证书不归本项目续期。**对外域名的证书由共享基建的 nginx（`infra_nginx`，同机另外八个项目共用）
负责签发与续期；本项目的巡检**只负责发现**。这条告警响了，修的地方几乎总在共享基建那边。

### 怎么发现

```text
条件：宿主机每 5 分钟取 .env 的 BILLING_FRONTEND_BASE_URL（只认 https），
     timeout 15 openssl s_client -connect <主机>:443 取对外证书的 notAfter
分级：剩余 < 21 天 = P2（BILLING_MONITOR_TLS_WARN_DAYS）；
     剩余 < 7 天或已过期 = P1（BILLING_MONITOR_TLS_CRIT_DAYS）；读不到证书 = P2
通知：并在 Healthchecks.io 检查 `ai_billing_hub disk` 里 → Telegram；状态翻转才发，恢复再发一次
正文：P2 tls certificate expires in <N> days (warn 21) / P1 tls certificate expires in <N> days (crit 7)
     / P1 tls certificate has expired / P2 tls certificate could not be read —— 不带主机名
```

⚠️ **它没有自己的检查**（免费档 20 个已用满）。`disk` 已经因为别的原因红着时，证书出事**不会再通知一次**；
收到 disk 的通知后要看 `journalctl -t billing-monitor -p err` 里的**全部**行。

Let's Encrypt 正常在剩 30 天时续期，所以剩 21 天还没换 = **续期已经失败约 9 天**。

`BILLING_FRONTEND_BASE_URL` 为空或不是 https 时这项跳过：日志里是
`no https BILLING_FRONTEND_BASE_URL configured: skipping the certificate check`，disk 的 OK 摘要写 `+ tls skipped`。

### 影响什么

| 仍然正常 | 受影响 |
| --- | --- |
| 证书过期之前：一切正常，这条告警就是为了在那之前响 | 过期之后：浏览器拒绝打开管理端与客户门户；客户的集成调用（用量摄取、AI 请求）在 TLS 握手处失败 |
| 宿主机回环上的 `/readyz`、巡检其余各项 | 读不到证书：多半是对外入口本身不通（边缘 nginx 停了、DNS 或防火墙），影响同上 |

### 立刻做什么

1. 在宿主机上看对外证书（只读；主机名从 `.env` 取，**不要**贴进公开的地方）：

   ```bash
   host="$(sed -n 's/^BILLING_FRONTEND_BASE_URL=https:\/\///p' .env | cut -d/ -f1 | cut -d: -f1)"
   timeout 15 openssl s_client -connect "$host:443" -servername "$host" </dev/null 2>/dev/null \
     | openssl x509 -noout -enddate -issuer
   ```

2. **快到期**：去共享基建那边查续期为什么没发生 —— `infra_nginx` 的证书续期任务（certbot 定时任务 / 容器）
   的日志、ACME 校验路径（`/.well-known/acme-challenge/`）是否被某个项目的配置挡住、DNS 是否还指向这台机器。
   续期成功后要让 `infra_nginx` **reload**，否则磁盘上是新证书、对外仍是旧证书
3. **读不到**：先从外网 `curl -sSI https://<主机>/healthz` 看入口通不通；`infra_nginx` 容器是否在跑；
   本项目的 `billing_nginx` 是否健康（services 维度应同时报）。宿主机自己出不了网时也会读不到
4. 修完手工跑一次：`BILLING_MONITOR_RECHECK_SECONDS=0 bash deploy/monitor.sh`

### 恢复

1. 第 1 步的命令看到新的 `notAfter`（剩余 ≥ 21 天）
2. 下一轮巡检 `ai_billing_hub disk` 回到 **up**（前提是同一维度没有别的行），OK 摘要以 `+ tls` 结尾的那一段出现

### 绝不能做什么

- **不要在本项目里自己签发或替换证书**来「先让它绿」：证书归共享基建管，两边各签一张会让下一次续期覆盖掉其中一张
- **不要为了让告警安静把 `BILLING_FRONTEND_BASE_URL` 改成 http 或清空**：检查会跳过，而证书照样会过期
- **不要调低 `BILLING_MONITOR_TLS_WARN_DAYS` / `BILLING_MONITOR_TLS_CRIT_DAYS`**：它们的意义是「还剩多少时间找人」，
  调低只会让告警来得更晚

---

## 主机时钟未同步

**首次编写：2026-11-03（AIH-TASK-150）。依据：[deployment.md](deployment.md) §8.1。**

⚠️ **时钟漂移不会让任何请求报错**，但会悄悄弄错一切跟时间有关的东西：集成调用签名的时间窗、会计期 cut-off、
汇率生效时刻、日志与备份的时间戳、证书有效期判断。

### 怎么发现

```text
条件：宿主机每 5 分钟读 timedatectl show -p NTPSynchronized --value
分级：值不是 yes（no 等）= P2
通知：并在 Healthchecks.io 检查 `ai_billing_hub disk` 里 → Telegram；状态翻转才发，恢复再发一次
正文：P2 host clock is not NTP-synchronized
```

⚠️ **它没有自己的检查**（免费档 20 个已用满），`disk` 已红时不会再单独通知 —— 看 `journalctl -t billing-monitor -p err` 的全部行。

命令不存在或输出为空时这项跳过，**不报警**（与 binlog 预算「量不到不报警」同一条理由）：日志里是
`clock sync status unavailable; skipping the clock check`，disk 的 OK 摘要写 `+ clock skipped`。

### 影响什么

| 仍然正常 | 受影响 |
| --- | --- |
| 刚失去同步时：本地时钟仍然大体准确，漂移按天累积 | 漂移变大之后：带时间窗的签名校验开始拒绝客户的集成调用；跨会计期边界的事件可能被记进错误的期间；日志与 binlog 时间对不上，按时间点恢复时难以定位 |

### 立刻做什么

1. 在宿主机上看现状：`timedatectl status`（`System clock synchronized`、`NTP service`）与
   `timedatectl timesync-status`（服务器、偏移量）
2. NTP 服务没开：`sudo timedatectl set-ntp true`；开着但不同步：查 `systemd-timesyncd`（或 chrony）的日志，
   以及出站 UDP 123 是否被防火墙挡住
3. 修完手工跑一次：`BILLING_MONITOR_RECHECK_SECONDS=0 bash deploy/monitor.sh`

### 恢复

1. `timedatectl show -p NTPSynchronized --value` 输出 `yes`
2. 下一轮巡检 `ai_billing_hub disk` 回到 **up**（前提是同一维度没有别的行）
3. 失步期间偏移量若已经大到秒级以上，核对那段时间的集成调用拒绝记录与跨会计期边界的事件

### 绝不能做什么

- **不要用 `date -s` 手工把时钟一下子拨到位**：时间倒退会让数据库与日志里出现「先发生的事排在后面」；
  交给 NTP 慢慢校正
- **不要为了让告警安静停掉或卸载 `timedatectl`**：检查会跳过，而时钟照样在漂

---

## 日志写入过快

**首次编写：2026-11-25（AIH-TASK-252）。依据：[deployment.md](deployment.md) §7、§8.1。**

⚠️ **危害不是磁盘，是证据。**容器日志用 json-file 按大小轮转（每容器缺省 3 × 10m），而外送每天 03:47 才一次。
某个服务开始刷屏时，**事故前的那段历史会在外送之前被轮转顶掉** —— 磁盘水位要很久之后才会动，
而调查要看的恰恰是被顶掉的那一段。

### 怎么发现

```text
条件：宿主机每 5 分钟对七个服务逐个读最近一小时的日志字节数
     （timeout 10 docker compose logs --no-color --no-log-prefix --since 1h <服务>，读满告警线就停）
告警线：每容器轮转容量（BILLING_LOG_MAX_SIZE × BILLING_LOG_MAX_FILES）÷ BILLING_MONITOR_LOG_RATE_HORIZON_HOURS（默认 60）
     缺省 31457280 ÷ 60 = 524288 B ≈ 512 KiB 每小时每服务
分级：最近一小时 ≥ 告警线 = P2
通知：并在 Healthchecks.io 检查 `ai_billing_hub disk` 里 → Telegram；状态翻转才发，恢复再发一次
正文：P2 log rate <服务> >= <告警线> B in the last hour (rotation <容量> B per container would not last <小时>h)
```

⚠️ **它没有自己的检查**（免费档 20 个已用满），`disk` 已红时不会再单独通知 —— 看 `journalctl -t billing-monitor -p err` 的全部行。
每个超线的服务各一行。

正文里的字节数是「≥ 告警线」，不是实际量：巡检读满告警线就停。命令失败、超时或没有输出时这项按 0 处理、**不报警**
（容器停了由 services 维度报）。

### 影响什么

| 仍然正常 | 受影响 |
| --- | --- |
| 请求路径与异步链路本身（这条告警只说明日志量，不说明服务坏了） | 该服务的历史日志：照这个速度，轮转容量撑不到下一次外送，**更早的行会在外送之前被顶掉**，事故调查缺一段 |
| 磁盘水位（多数时候还远没到 80%） | 刷屏本身往往是别的故障的症状：重试风暴、连接失败循环、被攻击或被刷接口 |

### 立刻做什么

1. 按告警里的服务名看它最近在刷什么（只读）：

   ```bash
   docker compose logs --no-log-prefix --since 10m <服务> | tail -n 50
   docker compose logs --no-log-prefix --since 1h <服务> | cut -c1-120 | sort | uniq -c | sort -rn | head -20
   ```

2. 先判断是不是**重试风暴**（同一条报错反复出现：下游连不上、任务失败后不停重试）或**被攻击**
   （`billing_nginx` / `api` 里同一来源、同一路径的请求刷屏，401 / 404 / 429 成片）。是的话按对应场景处置：
   [Redis / Celery broker 不可用](#redis--celery-broker-不可用)、[MySQL 不可用](#mysql-不可用)、
   [Celery 队列积压](#celery-队列积压) 等；被攻击时在边缘限流或封源
3. **需要保住证据时，立刻手工跑一次外送**，不要等 03:47：在部署目录跑
   `flock -n /tmp/ai_billing_hub-logs.lock bash deploy/log_ship.sh`（与 cron 同一个脚本、同一把锁，
   窗口从上一次成功那一刻起，宁可重叠不留缝）。刷屏持续期间可以隔几小时再跑一次
4. 修完手工跑一次：`BILLING_MONITOR_RECHECK_SECONDS=0 bash deploy/monitor.sh`

### 恢复

1. 第 1 步的命令看到那个服务的输出回到平时的量
2. 下一轮巡检 `ai_billing_hub disk` 回到 **up**（前提是同一维度没有别的行）。⚠️ 判据看的是**最近一小时**，
   刷屏停下之后最多要等一小时这一行才会消失
3. 刷屏期间若手工外送过，确认那一轮 `ai_billing_hub logs` 是成功 ping，R2 上有对应的归档

### 绝不能做什么

- **不要为了消告警调小 `BILLING_MONITOR_LOG_RATE_HORIZON_HOURS`**：告警线 = 容量 ÷ 小时数，调小只会把告警线**抬高**、
  掩盖问题 —— 而轮转照样在外送之前把历史顶掉
- **不要清空容器日志或删 `/var/lib/docker/containers/*/*-json.log`**：事故调查要的正是这一段；
  删掉之后外送也抓不到了
- **不要先重建那个容器来「止血」**（`down`、`up -d --force-recreate`）：容器删掉时它的 json 日志一起没了；先外送，再处置

---

## 备份失败：全量备份与 binlog 离机

**首次编写：2026-11-22（AIH-TASK-230）。依据：`deploy/cron.d/ai_billing_hub:26`、`:33`、`:54`；`deploy/backup.sh:40-50`、`:91-100`、
`:246-247`、`:308-312`；`deploy/binlog_ship.sh:47-92`、`:162-166`、`:239-244`、`:291-294`；[deployment.md](deployment.md) §5.2.6、§5.2.7。**
备份失败之后要恢复整库，见下一节「整台 VPS 没了（灾难恢复）」。

### 怎么发现

告警全靠 Healthchecks.io 的心跳（dead man's switch）：脚本成功时 ping，失败时立刻 ping `/fail`，「该到的 ping 没到」或收到
`/fail` 时由 Healthchecks 发 Telegram，恢复后再发一次。

| 任务（cron） | 检查与 `.env` 键 | 立刻 `/fail` | 只有「没收到」才发现 |
| --- | --- | --- | --- |
| `binlog_ship.sh`，每分钟 | `ai_billing_hub binlog`（周期 1 分钟、宽限 5 分钟），`BILLING_HEALTHCHECK_BINLOG_URL` | 任何失败；**破 RPO**（上一次成功离机距今超过 `BILLING_BINLOG_RPO_SECONDS`，默认 300 秒，被锁跳过的那一轮也查）。同时以 err 级别写 syslog：`journalctl -t billing-binlog -p err` | cron 没跑、整机挂了、脚本卡死；被锁跳过的那一轮不 ping |
| `backup.sh`，每天马来西亚 03:17 | `ai_billing_hub full backup`（cron `17 3 * * *`、宽限 1 小时），`BILLING_HEALTHCHECK_BACKUP_URL` | 任何失败，`/fail` 的正文就是失败原因 | cron 没跑、整机挂了、脚本卡死 |
| `restore_drill.sh`，每周日 04:47 | `ai_billing_hub restore drill`，`BILLING_HEALTHCHECK_DRILL_URL` | 任何失败，包括 R2 上最新一份全量超过 26 小时（全量没到 R2） | 同上 |

⚠️ **没有告警的情形**（照脚本实际行为，如实写）：

- **`.env` 没配心跳地址**：脚本照常跑，失败时只多打一行 `nobody will be told about this`，**没有任何通知**（`deploy/backup.sh:45-46`、
  `deploy/binlog_ship.sh:67`）
- **心跳 ping 本身失败**：只记一行 `heartbeat ping failed`，本轮结论不变；Healthchecks 按「没收到」处理，要等宽限期过去才报
- **`backup.sh` 没有单独的 err 级 syslog**：它的输出整段以 info 级进 `billing-backup`，`journalctl -t billing-backup -p err` 查不到
  失败，要看全部行。binlog 才有单独的 err 行
- **`--dry-run` 不 ping**：手工演练不会让检查变绿，也不会报失败
- **本地轮转删除失败被忽略**（`deploy/backup.sh:308` 的 `|| true`），不报
- **R2 上的保留期**由桶的 lifecycle 规则管，脚本不检查（[deployment.md](deployment.md) §5.2.1 末尾）
- **「能恢复」的证明每周一次**：全量与 binlog 的成功 ping 只说明传上去了，能不能恢复要等周日的演练

### 影响什么

| 失败 | 影响 |
| --- | --- |
| binlog 离机失败 / 破 RPO | 业务不受影响；但 RPO ≤ 5 分钟不再成立：此刻机器没了，丢的是最后一次成功离机之后的全部写入 |
| binlog 链断档（`gap in the binlog chain`） | 最近那份全量之后、断档之后的写入不能再按时间点恢复；之后每一轮推送都报这条，直到处理 |
| 全量失败 | 最近一份可用的全量变旧；恢复仍可以用更早一份全量加之后完整的 binlog 链 |
| 全量已上传、`FLUSH BINARY LOGS` 失败 | 备份在桶里；在下一次写入之前，恢复**这一份**会因锚点 binlog 不在 R2 被拒（`deploy/backup.sh:246-247`） |
| 月 / 年备份复制失败 | 当天的全量已在桶里；判据是「这个月还没有」，下一轮自动补 |

### 立刻做什么

1. 看失败原因（只读）：Telegram 通知的正文就是 `/fail` 的正文；再看
   `journalctl -t billing-binlog -p err --since -1h` 与 `journalctl -t billing-backup --since -1d`
2. 按报错处理：

   | 报错 | 先查 |
   | --- | --- |
   | `R2 is not configured (BILLING_R2_BUCKET / BILLING_R2_ENDPOINT); …` | `.env` 的这两个键 |
   | `upload failed; …` / `… cannot be read back` / `size mismatch after upload: …` | R2 是否可达、`R2_ACCESS_KEY_ID` / `R2_SECRET_ACCESS_KEY`。全量失败时报错里带着本地那份已验证备份的路径 |
   | `BILLING_BACKUP_PASSPHRASE is not set …` / `encryption failed` / `… cannot be decrypted — NOT uploading` / `… differs — NOT uploading` | 备份口令。**不上传是对的**：解不开的备份比没有更糟 |
   | `mysqldump failed` / `the dump is empty` / `the dump is truncated (no completion marker)` / `cannot reach the database` | 数据库或磁盘：按「MySQL 不可用」，磁盘看 `ai_billing_hub disk` |
   | `RPO breached: …` | 上一轮卡住或连续失败：同一时段的其它 err 行就是原因 |
   | `gap in the binlog chain: …` | 按报错里写的做：先手工跑一次 `bash deploy/backup.sh` 建立新锚点，再删报错点名的进度文件（`deploy/binlog_ship.sh:239-244`）。断档那一段之后无法按时间点恢复，记进事故记录 |
   | `binary logging is off; PITR is impossible` | `docker-compose.yml` 里 mysql 的启动参数被改过 |
   | `the backup IS uploaded, but FLUSH BINARY LOGS failed; …` | 备份已在桶里；下一次有写入后 binlog 离机会关掉并推走那个文件。要马上可恢复，修好后手工再跑一次 `bash deploy/backup.sh` |

3. 修好后验证：全量手工跑一次 `bash deploy/backup.sh`；binlog 等下一分钟的 cron

### 恢复

1. 对应的检查收到成功 ping、回到 **up**（binlog：下一分钟；全量：手工跑一次或次日 03:17）
2. 下一个周日的恢复演练照常通过（`ai_billing_hub restore drill` 为 up）
3. 破过 RPO 或断过链的，把时间窗写进事故记录：它就是那段时间里的真实 RPO

### 绝不能做什么

- **不要用 `--dry-run` 让检查「先绿」**：它不上传，也不 ping
- **不要删还没离机的 binlog**，也不要为了消 `RPO breached` 删进度文件：只有 `gap in the binlog chain` 按报错要求先跑全量、再删那一个
- **不要调大 `BILLING_BINLOG_RPO_SECONDS` 来让告警安静**：它就是 RPO 预算
- **不要清空 `.env` 里的心跳地址来止住通知**：脚本照跑，失败只剩一行 `nobody will be told about this`
- **不要把 `.env`、备份口令、R2 凭据或心跳地址贴进工单、聊天或截图**：知道心跳地址的人都能伪造「成功」（[deployment.md](deployment.md) §5.2.6）

---

## 整台 VPS 没了（灾难恢复）

**首次编写：2026-09-16（T0.9）。§136 的 18 个场景里第三个落地的，也是 §98.1 要求的「书面的灾难恢复顺序与恢复后对账」。**

⚠️ **RTO ≤ 4 小时是这一节的预算，不是口号。**四小时要装下：发现 → 决策 → 起新主机 →
拉备份 → 解密 → 导入 → 重放 binlog → **对账** → 切流量。没演练过的流程，光「解密口令放在哪」
就能吃掉一小时。

### 怎么发现

| 信号 | 说明 |
| --- | --- |
| **binlog 心跳先停**（周期 1 分钟、宽限 5 分钟） | **最快的信号**，也正好是 RPO 的量级 |
| `ai_billing_hub services` / `readyz` / `disk` 三个检查同时 DOWN | 巡检每 5 分钟一次、宽限 15 分钟。⚠️ `disk` 单独 DOWN 不是整机的事：这个维度里有磁盘水位、binlog 预算、TLS 证书到期、主机时钟同步四项，按正文那一行找对应的节（下面「TLS 证书即将到期或读不到」「主机时钟未同步」） |
| 站点 502 | 边缘 `infra_nginx` 找不到上游 |

⚠️ **告警本身跑在那台机器上 —— 它挂了就不会有任何「错误日志」。**能发现这件事的只有
外部服务的 dead man's switch：「该到的 ping 没到」。这就是每个脚本成功时也要 ping 的原因。

⚠️ **先分清楚是整机不可用，还是只有服务挂了。**后者按对应场景处理（重启栈、看 `journalctl`），
**不要**直接进灾难恢复 —— 重建一台新主机的代价远大于重启一次栈。

### 影响什么

| 仍然安全 | 已经失去 |
| --- | --- |
| R2 上的全量、binlog、月 / 年备份、日志归档 | 这台主机上的一切：容器、数据库文件、本地备份副本、`.env`、`secrets/` |
| Bitwarden 里的 `master.key` / `jwt.key` / 备份口令离线副本 | `.last-good-deploy`（⚠️ 它也在这台机器上 —— 改从 GitHub Actions 的 Deploy 运行记录里找最后一次成功的 `ref`） |
| GitHub 上的代码与镜像（ghcr） | 边缘代理指向这台机器的那条配置 |

**数据丢失窗口 = 最后一次成功的 binlog 心跳到故障的时间**，设计上 ≤ 5 分钟（RPO）。
这个数字要在对账时**实测出来**，不要假设。

### 立刻做什么

⚠️ **顺序是有讲究的**，下面每一步都依赖上一步的产出：

1. **确认故障范围**（主机控制台 / ping / 供应商状态页），决定「修」还是「重建」。写下决策时刻 ——
   RTO 从这一刻算
2. **取离线材料**（Bitwarden）：`master.key`、`jwt.key`、SMTP 密码、`BILLING_BACKUP_PASSPHRASE`、
   R2 的 `R2_ACCESS_KEY_ID` / `R2_SECRET_ACCESS_KEY`、以及 `.env` 里其余的口令
3. **起新主机**：同区域，≥ 2 核 / 4 GB / 48 GB 盘（现网规格见 [deployment.md](deployment.md) §3.1），
   装 Docker 与 git
4. **取代码**：`git clone` 之后 **checkout 上一次成功部署的那个 SHA**
   （来源：R2 `config/` 最新快照里的 `.last-good-deploy`；或 GitHub Actions → Deploy → 最后一次成功的那次 ——
   合并触发的看它的 commit，手动触发的看 `ref` 输入）。
   ⚠️ **不要直接用 `main` 的最新提交** —— 它未必部署过，灾难当天不是验证新代码的时候
5. **还原密钥与配置**：`secrets/` 下三个文件，然后
   ```bash
   chown 10001:10001 secrets/*        # 10001 = 容器运行 UID（ADR-0004）
   chmod 0400 secrets/*
   ```
   再写 `.env`（数据库口令、R2 凭据、备份口令、心跳地址等；键名清单见 R2 `config/` 里最新那份快照）。
   ⚠️ `BILLING_IMAGE` / `BILLING_FRONTEND_IMAGE` 两行**不要手写**，由第 9 步的 `deploy.sh` 部署成功后写入
6. **只起数据库**：`docker compose up -d mysql`，等它 healthy。
   ⚠️ 先起全栈会让应用对着一个空库跑。这一步不需要本项目的镜像 —— MySQL 用的是公共镜像，新主机上 compose 会自己拉
7. **恢复**：
   ```bash
   deploy/restore.sh full/<最新一份全量的 key> billing_restore
   ```
   脚本会拉回全量、解密、导入，再重放它之后**全部**的 binlog（PITR）。
   要恢复到误操作之前的某一刻就给第三个参数（UTC 时间）
8. **对账**（见下一节），**通过之后**再往下走
9. **切换并起全栈**：把恢复库切成生产库名（改名或改 `.env` 里的 `BILLING_MYSQL_DATABASE`），然后
   ```bash
   BILLING_IMAGE_REPO=ghcr.io/<owner>/<repo> \
   BILLING_FRONTEND_IMAGE_REPO=ghcr.io/<owner>/<repo>-frontend \
     deploy/deploy.sh <第 4 步那个 SHA>
   ```
   它会按那个 commit 拉两个不可变镜像、跑迁移、等七个服务 healthy、跑冒烟，把这一次记进 `.last-good-deploy`，
   并把两个镜像钉进 `.env`（[deployment.md](deployment.md) §9.7）。
   ⚠️ **两个 `*_REPO` 必须在命令行上给**：`deploy.sh` 只从 shell 环境读它们、不读 `.env`（平时是 CD workflow 传的）。
   漏了它不会报错，而是**落进演练模式**：不拉镜像、去找本地构建的 `acuven-billing-hub:local`，新主机上没有，栈起不来
   （2026-09-18 查实）。两个包是公开的，不需要 `docker login`。
   ⚠️ **不要在这里手工 `docker compose up -d`**（Codex 审查 PR #67 指出）：新主机的 `.env` 里还没有部署钉进去的镜像，
   compose 会退回 compose 文件里那个本地构建的默认标签 `acuven-billing-hub:local`，同样起不来。
   ⚠️ 迁移对一份刚恢复的库通常是空操作（dump 里带着 `alembic_version`）——**这是对的**，它同时也是「代码与表结构对得上」的一次检查
10. **切流量**：把边缘 `infra_nginx` / DNS 指到新主机
11. **恢复运维设施**：按 [deployment.md](deployment.md) §5.2.5 重装 cron（五行），确认四类心跳
    在下一个周期内全部回绿

### 恢复后对账（§98.1 要求）

⚠️ **「导入成功」不等于「数据对」。**下面这些**每一条都要留下实际数字**，写进当次事故记录：

```sql
-- 1. 表清单：应为 8 张（7 张业务表 + alembic_version）
SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = DATABASE();

-- 2. 数据丢失窗口：最后一条审计记录的时间 vs 故障时刻
SELECT MAX(created_at) FROM audit_logs;

-- 3. 待投递的事件：beat 起来之后会自动补投，这里只看量级
SELECT status, COUNT(*) FROM domain_outbox GROUP BY status;

-- 4. 账号：与最近一次演练 / 月报的数字对得上
SELECT COUNT(*) FROM users;
SELECT COUNT(*) FROM two_factor_settings;
```

- **主密钥可用性**：用恢复出来的 `master.key` 解开一条 TOTP 密文（做法见下一节）。
  ⚠️ 不做这一步的话，「能登录第一因子、过不了第二因子」要等第一个管理员上线才暴露
- **RPO / RTO 实测值**：把「最后一条审计记录」与「决策时刻」的差、以及第 1 步到第 10 步的耗时
  都记下来。⚠️ 这两个数字是下一次演练与容量决策的输入，事后补不出来
- ⚠️ **钱包余额 = 账本流水之和、用量事件计数、支付回调对账 —— 现在做不了**：
  这些表要到 Phase 1 才存在。写在这里是为了它们落地时**不会被忘掉**，不是假装已经做了

### 恢复

1. 观察一个完整周期：binlog 每分钟离机、巡检每 5 分钟、当晚 03:17 的全量与 03:47 的日志外送
2. 下一个周日的自动恢复演练照常跑通（`ai_billing_hub restore drill` 变绿）
3. 把这次的 RPO / RTO 实测值与偏差写进收尾 PR 描述的任务记录（[TODO](TODO.md) 只改勾选项）

### 绝不能做什么

- **不要在核对之前切流量。**切早了的话，新写入会落在一个可能不完整的库上 ——
  那时想退回去，连「退回哪个时刻」都说不清
- **不要用 `BILLING_RESTORE_FULL_ONLY=1` 跳过 binlog 重放**，除非你**已经确认**
  binlog 链断了并且**书面承认**要丢掉那一段。丢数据的决定必须是人做的、并且写下来
- **不要在新主机上生成新的 `master.key`。**那不是「重新初始化」，是**把所有 TOTP 注册一次性作废**
- **不要把 `.env`、密钥内容贴进聊天、工单或截图。**灾难当天最容易发生这件事
- **不要跳过第 4 步直接用 `main`。**灾难恢复不是发布新版本的时机
- **不要用 `docker compose up -d` 起应用**（起 mysql 除外）。理由见第 9 步：新主机的 `.env` 里还没有部署钉进去的镜像 ——compose 会去找一个本地构建的默认标签，然后失败。**起应用只走 `deploy/deploy.sh`**
- **恢复完成之前不要往 `main` 合并任何东西。**2026-09-18 起合并即部署、不经人工批准：`VPS_*` secret 指着哪台机器，
  一次合并就会对着它开火 —— 指着旧机器是一次红色的 CD，已经改指新机器则是一次没人计划过的发布

---

## 主密钥（`master.key`）丢失或要在新主机上重建

**首次编写：2026-09-16（T0.9）。这一节是 [deployment.md](deployment.md) §6 要求的「runbook 写入主密钥恢复流程（脱敏）」——⚠️ 具体的值只在 Bitwarden 里，本文件与仓库都不许出现。**

### 怎么发现

| 信号 | 说明 |
| --- | --- |
| 部署失败：`bind source path does not exist` | 生产（Linux）上 secret 文件不存在时，创建容器直接失败，旧版本原样留着 |
| 日志里 TOTP 解密失败 / 启动时读不到密钥 | Docker Desktop 下 secret 文件不存在时，它把路径建成一个**目录**挂进去，读它抛 `OSError` —— 密钥静默变成空串 |
| 第二因子全体失效：密码对、验证码一律不通过 | 密文还在库里，只是解不开 |
| 灾难恢复的第 5 步 | 这时不是「丢了」，而是**要把离线副本放回去** |

⚠️ **备份是另一把钥匙。**`BILLING_BACKUP_PASSPHRASE` 只管备份的加解密，
`master.key` 只管应用里的信封加密（ADR-0004）。**丢哪一把，后果完全不同**，别混。

### 影响什么

| 仍然正常 | 受影响 |
| --- | --- |
| 数据库、备份、日志归档 | **所有 TOTP 注册**：密文解不开，第二因子全废 |
| 密码登录的第一因子 | 管理员登录卡在第二步 |
| R2 上的备份（那把是备份口令） | 依赖信封加密的一切后续功能（Phase 1 起的凭据存储） |

⚠️ **真正丢了（离线副本也没有）= 不可恢复**：只能让每个管理员重新扫码注册，
并按 [ADR-0004](adr/ADR-0004-credential-encryption.md) 走一次密钥轮换。

### 立刻做什么

1. 从 **Bitwarden** 取离线副本（条目名与字段在私有金库里，本文件不记）
2. 写回文件 —— ⚠️ **格式是 `version:base64`，单行、结尾不要多余换行**：
   ```bash
   printf '%s' '<从 Bitwarden 粘贴>' > secrets/master.key
   chown 10001:10001 secrets/master.key
   chmod 0400 secrets/master.key
   ```
   ⚠️ `compose` 的 `file:` secret 走 bind mount，`uid`/`gid`/`mode` 三个选项在普通
   compose 下**被忽略** —— 宿主机上的属主与权限原样带进容器，那条 `chown` 是唯一的生效途径
3. **先核对指纹，再起栈**：只比对哈希前缀，**不要把内容打印出来**
   ```bash
   sha256sum secrets/master.key | cut -c1-8      # 与 Bitwarden 里记的前缀比对
   ```
4. 起栈：`docker compose up -d`，等七个服务 healthy。
   ⚠️ 这只在**这台主机部署过**时成立：`deploy.sh` 部署成功后把两个镜像钉在 `.env` 里
   （[deployment.md](deployment.md) §9.7）。**灾难恢复的新主机上没有那两行** —— 在那里
   不要在这一步起栈，等灾难恢复第 9 步的 `deploy/deploy.sh`
5. **验证真的能解密**（这一步不能省）：在一个**断网**的一次性容器里，用这把密钥解开一条
   从数据库里取出来的 TOTP 密文，只打印 `decrypted` —— 做法与每周自动演练里那一段相同
   （见 [deployment.md](deployment.md) §5.2.7）。
   ⚠️ **「容器起来了」不等于「密钥是对的」**：错的密钥要等第一个管理员登录才暴露

### 恢复

1. 用验证器 App 走一次完整登录（第一因子 + 第二因子），确认 TOTP 真的通过
2. 把这次恢复的时刻、指纹前缀、耗时记进当次事故 / 演练记录
3. ⚠️ 如果这次是因为**离线副本也差点没有**才惊险过关，那就补一条：按 ADR-0004 第 3 节
   确认副本的存放位置与访问控制仍然成立（与数据库备份**分开存放**、两套访问控制）

**已经演练过**（2026-09-15）：只凭 Bitwarden 里的离线 `master.key`，在断网容器里解开恢复库里的
TOTP 密文、生成的验证码与验证器 App 一致、错误的密钥被拒绝；从恢复到解密用时 4 分 12 秒。

### 绝不能做什么

- **不要生成一把新的覆盖上去。**那不是修复，是**把所有 TOTP 注册一次性作废** ——
  而且覆盖之后，原来的密文永远解不开了
- **不要把老版本从钥匙串里删掉。**轮换之后老行仍然要用老密钥解开
  （重包裹的后台任务还没有，见 [TODO](TODO.md) 的 T0.9）
- **不要把密钥值写进 `.env`、仓库、聊天、工单或截图。**ADR-0004 定的是「只有路径进环境变量」
- **不要为了读到密钥把容器改回 root。**ADR-0004 明写；要改的是宿主机文件的属主

---

## 文档存储恢复

**首次编写：2026-11-22（AIH-TASK-230）。依据（同日核实）：`docker-compose.yml:477-480`、`docker-compose.yml:229-230`、
`app/api/receipt_download.py:82-110`、`app/services/receipt_pdf.py:1-17`、`app/services/statement_pdf.py:1-19`、`app/services/pdf_text.py:29-45`、
`Dockerfile:18`；[deployment.md](deployment.md) §5.1 的分层表与 §11。**

⚠️ **本平台没有独立的文档存储。**核实结果：

- `docker-compose.yml` 只有三个命名卷：`mysql-data`、`redis-data`、`celery-beat-state`，没有文档卷；api 唯一的可写挂载是 `/tmp` 的
  tmpfs（xlsx 导出的临时文件，容器重启即清空，见「xlsx 导出失败」）
- 收据 PDF：下载时读 `payment_receipts` 行的快照（`snapshot_json`），由 `app/services/receipt_pdf.py` 现场渲染
- 对账单 PDF：下载时读 `monthly_statements` 行的快照、对账单号与定稿时刻，由 `app/services/statement_pdf.py` 现场渲染，不存文件
  （见「月度对账单」的「PDF 下载」）
- 中文字体 `app/data/NotoSansSC-Regular.ttf` 随 `app` 包打进镜像，不在卷上
- [deployment.md](deployment.md) §5.1 的第四层「不可变文档文件」按设计 Phase 3 才有，现在没有这类数据

所以**文档的恢复就是数据库的恢复**。⚠️ 将来新增文档存储（卷、对象存储或 R2 的文档桶）时，必须回来把本节改写成那份存储自己的
发现、备份与恢复步骤，并同步改开头的对照表。

### 怎么发现

没有「文档卷丢了」这个信号，也没有单独的告警维度。客户或管理员反馈收据、对账单下载失败（500 `INTERNAL_ERROR`）时，按请求时刻
与 `request_id` 去 api 日志里找：对账单按「月度对账单」的「PDF 下载失败」那张表；收据的日志是 `Receipt snapshot invalid; download refused`
（带收据 `public_id`）或字体、渲染类错误，按 [payment-flow.md](payment-flow.md) 的收据一节处理。

### 影响什么

| 情形 | 影响 |
| --- | --- |
| 数据库在 | 每一份收据、对账单都能重新下载，同一镜像里字节完全相同 |
| 数据库丢了或损坏 | 收据、对账单与账本是同一份数据，一起丢；从备份恢复之后一起回来 |
| 镜像里的字体缺失或损坏 | 只影响含非 Latin-1 字符（如中文公司名）的那些下载；数据不受影响 |

### 立刻做什么

1. 先分清是数据库的问题还是渲染的问题：`/readyz` 是否 200、其他接口是否正常
2. 数据库不可用：按「MySQL 不可用」；数据库丢失或损坏：按「整台 VPS 没了（灾难恢复）」从备份恢复
3. 渲染问题（快照不合规、字体读不到）：按「月度对账单」的「PDF 下载失败」处置；字体问题是**重新部署带字体的镜像**

### 恢复

1. 数据库恢复并通过「恢复后对账」之后，在管理端各下载一份收据与一份对账单，与页面上的详情核对（做法同「月度对账单」的
   「首个账期生成后的人工核对」）
2. 不需要另外恢复任何文件

### 绝不能做什么

- **不要为了「备份文档」另建一个卷、把 PDF 落盘**：那是新增文档存储，要先按上面的提醒补齐本节与备份方案
- **不要为了让某份文档能下载去改 `payment_receipts` 或 `monthly_statements` 的行**：行不可改（触发器拒绝），修复要另过设计闸门
- **不要换 `app/data/NotoSansSC-Regular.ttf`**：会改变历史收据与对账单的字形，要另过设计闸门

---

## FX 汇率：拉取失败（`fx_fetch` 告警）

**首次编写：2026-10-04（AIH-TASK-043）。依据：设计闸门 #183 v3（[AIH-TASK-028-fx-rates.md](design/AIH-TASK-028-fx-rates.md) §2「两个告警维度」、§5）、
[currency-and-fx.md](currency-and-fx.md)、[deployment.md](deployment.md) §8.4。**

### 怎么发现

| 信号 | 说明 |
| --- | --- |
| Telegram：`ai_billing_hub fx_stale` 变 DOWN，正文 `P2 fx_stale: fx_fetch P2: USD no successful fetch in 72 hours (<n> failed) \| fx_stale <级别>: …` | **主信号**（2026-11-21 起 `fx_fetch` 与 `fx_stale` 并成一行、共用 `ai_billing_hub fx_stale` 一个检查；正文按成员列出 `fx_fetch <级别>: … \| fx_stale <级别>: …`，看是哪个成员不是 OK，见 [deployment.md](deployment.md) §8.4「2026-11-21 合并维度」）。判据：某个配置币种最近 72 小时没有任何成功的拉取（`NEW_DRAFT` / `NO_NEW_QUOTE` / `NO_QUOTE_FOR_DATE`），且至少一条 `FAILED` —— 即连续 3 天、9 次拉取全部失败 |
| `GET /api/v1/admin/fx-rates/fetch-attempts?outcome=FAILED` | 每次失败一行，`error_code` 说明原因 |
| worker 日志 | 某个币种写库失败时的 ERROR（那一次不留拉取记录，任务以失败结束） |

⚠️ **周末、公众假期不会触发它**：BNM 那几天答「无记录」，记为 `NO_QUOTE_FOR_DATE`，算成功。
72 小时里**一条记录都没有**（Beat 停了、worker 没起）也**不是**这条告警 —— 那由 `fx_stale` 与「celery-beat 停止调度」兜住。

### 影响什么

| 仍然正常 | 受影响 |
| --- | --- |
| 计费：已发布的版本照旧生效，`resolve_fx_rate` 只读库、**热路径从不调 BNM**（ADR-0005 第 6 条） | 没有新草稿：管理员没有新的 BNM 价可发布 |
| 管理员手工录入、发布、退役 | 时间一长，`fx_stale` 会接着响（当前版本报价日超过 5 个日历日） |

**没有数据丢失，也没有计费失败。**拉取失败只影响草稿与告警。

### 立刻做什么

1. 看失败原因：`GET /api/v1/admin/fx-rates/fetch-attempts?outcome=FAILED&base_currency=USD`

   | `error_code` | 多半是 |
   | --- | --- |
   | `NETWORK` / `TIMEOUT` | worker 出不去或 BNM 不可达。在 worker 容器里试一次出站 HTTPS（SMTP 也走出站，可对比） |
   | `HTTP_5xx` / `HTTP_429` | BNM 自己的问题，等它恢复 |
   | `HTTP_404` | 404 但不是「无记录」那个响应体：BNM 改了路径 |
   | `BAD_PAYLOAD` | BNM 改了响应形状（设计 §10 假设 2）—— 要改适配器，开任务 |
   | `UNIT_NOT_EXACT` | `middle_rate / unit` 超过 10 位小数，同上 |

2. 手工触发一次确认现状（幂等，拉的永远是吉隆坡当天）：
   ```bash
   docker compose exec celery-worker python -c "from app.tasks.fx_fetch import fetch; print(fetch())"
   ```
3. 短期内恢复不了、而当前版本又快过期：管理员按 BNM 网站公布的中午场中间价**手工录入**草稿
   （`POST /api/v1/admin/fx-rates`，`source_reference` 写清出处）并发布 —— 这正是 ADR-0005 第 1 条留的通道

### 恢复

1. 下一次 Beat（吉隆坡 12:30 / 14:30 / 17:30）或手工触发出现成功记录，`fx_fetch` 下一轮巡检转绿
2. 拿到 `NEW_DRAFT` 后，管理员核对并发布（见下面「过期」一节的发布步骤）
3. 适配器要改（`BAD_PAYLOAD` / `UNIT_NOT_EXACT`）：开任务改 `app/core/fx_source.py`，不改设计语义；在那之前靠手工录入

### 绝不能做什么

- **不要在计费路径上加「拉不到就实时调 BNM」。**ADR-0005 第 6 条：外部接口的抖动不能变成计费失败
- **不要为了让告警安静把 72 小时窗口调大、或删拉取记录。**拉取记录只增（触发器拒绝改删），它是草稿的来源证明
- **不要让自动拉取直接发布**来「省掉人工那一步」。ADR-0005 第 3 条：发布是「这个数字要开始算钱了」的确认点

---

## FX 汇率：过期，或没有生效的汇率（`fx_stale` 告警）

**首次编写：2026-10-04（AIH-TASK-043）。依据同上一节。**

### 怎么发现

| 信号 | 说明 |
| --- | --- |
| Telegram：`ai_billing_hub fx_stale` 变 DOWN，正文 `P2 fx_stale: fx_fetch <级别>: … \| fx_stale P2: USD quoted <报价日> (<n> calendar days)`（2026-11-21 起与 `fx_fetch` 同一行，见上一节） | 当前生效版本的报价日距今**超过 5 个日历日**（吉隆坡日期相减；Kelvin 2026-09-29 定，ADR-0005 收口条件旁有注明）。手工版本取 `observed_at` 的吉隆坡日期 |
| 正文里 `fx_stale P2: USD no rate in effect` | 该币种**此刻没有任何生效的版本**：从没发布过、最后一个被退役了、或只有未来的预约 |
| 业务侧：非 MYR 事件判 `FX_RATE_ERROR` | 只在「没有生效的版本」时出现，计费 worker 落地之后（T-H）才有这个信号 |

三种常见原因：

1. **忘了发布** —— 拉取一直成功（`fx_fetch` 是绿的），草稿堆着没人发布。最常见
2. **BNM 持续「无记录」** —— 拉取记录全是 `NO_QUOTE_FOR_DATE`：长假（周末 + 两三天公众假期在 5 天内，偶尔会超）；
   或者**币种配错**（BNM 对不存在的币种代码也答「无记录」，所以不会触发 `fx_fetch`）—— 后者的表现是 `no rate in effect`
   且从来没有过该币种的 BNM 草稿，查 `BILLING_FX_CURRENCIES`
3. **拉取一直失败** —— 那 `fx_fetch` 应当先响过，按上一节处理

### 影响什么

| 情形 | 影响 |
| --- | --- |
| 过期（有生效版本，只是旧） | 计费照常，按旧汇率算。偏差由定价规则 markup 的 FX 缓冲吸收（ADR-0005「负面」） |
| 没有生效的版本 | 非 MYR 事件 `FX_RATE_ERROR`、**不扣费**；发布之后由 T-H 重新入队补算 |

### 立刻做什么

1. 看草稿：`GET /api/v1/admin/fx-rates?status=DRAFT&base_currency=USD`；看拉取：`GET /api/v1/admin/fx-rates/fetch-attempts?base_currency=USD`
2. 有 BNM 草稿：与 BNM 网站上**同一报价日**的中午场中间价核对，`POST /api/v1/admin/fx-rates/{fx_rate_id}/publish`，
   **不给 `effective_from`** —— 从发布那一刻（`t`）起生效，不从报价日回溯（Kelvin 2026-09-29）
3. 没有草稿（长假、BNM 不覆盖的币种）：手工录入草稿（`source_reference` 写出处）再发布
4. 币种配错：查容器里实际的值
   `docker compose exec api python -c 'from app.core.config import get_settings; print(get_settings().fx_currency_codes)'`。
   ⚠️ 截至 AIH-TASK-043，`docker-compose.yml` 的 `x-backend.environment` **没有转发** `BILLING_FX_CURRENCIES`，
   只改 `.env` 进不了容器（理由同「配置项」一节）；要改币种得先在 compose 里加转发，另开任务

发布返回 409 `EFFECTIVE_FROM_CONFLICT`：有一个尚未开始的预约版本（或同一秒刚发布过）。要么指定一个晚于预约起点的
`effective_from`，要么先退役那条预约（撤销），要么下一秒重发。

### 恢复

1. 下一轮巡检（5 分钟内）`fx_stale` 转绿，正文变成 `fx_fetch OK: … | fx_stale OK: USD quoted <新报价日> (<n> calendar days)`（`fx_fetch` 成员也不是 OK 时整行仍红，按上一节处理）
2. 「没有生效的版本」那种：时间线为空时不给时刻的发布得到 `[null, null)`、退役留下的空档由不给时刻的发布从退役时刻起补上
   （设计 §2 P1 / P4）—— 这段时间里 `FX_RATE_ERROR` 的事件此后取得到汇率，由 T-H 重新入队补算；已取到汇率的时刻结果不变

### 绝不能做什么

- **不要把 5 天的阈值调大来「让它别响」。**它是「拉到了但忘了发布」唯一的信号（ADR-0005 第 6 条第二条告警）
- **不要直接改库里的版本行**（改 `effective_from` / `rate`、把草稿 `UPDATE` 成 `PUBLISHED`）。触发器会拒绝大部分写法；
  侥幸过去的会绕开锁、区间计算与审计
- **不要不核对就发布 BNM 草稿。**发布之后这个数字就开始算钱，而且已被引用的版本不可修改

---

## FX 汇率：发布了错误的汇率（退役与补发）

**首次编写：2026-10-04（AIH-TASK-043）。依据：设计闸门 #183 v3 §2「退役」「发布」、§5「发布了错误汇率」。**

### 怎么发现

| 信号 | 说明 |
| --- | --- |
| 管理员核对时发现已发布版本的 `rate` 与出处不符 | 例如手工录入时小数点错位、把买入价当成中间价 |
| 客户或财务对某段时间的 MYR 换算提出异议 | 查那段时间生效的版本：版本列表 `GET /api/v1/admin/fx-rates?base_currency=USD` 的 `effective_from` / `effective_to` |
| 拉取记录出现 `QUOTE_CHANGED` | BNM 更正了同一报价日的价；已有版本**不会**跟着变 |

### 影响什么

- 错误版本生效期间计费的事件按错误汇率算。⚠️ **已经计费的事件不会因为退役而改变**：一个（币种, 时刻）一旦取到某个
  版本，以后永远取到同一个（ADR-0005「同一事件重算得出相同 MYR」）。已计费事件的汇率不在 Rebill 范围内（设计闸门 #830 §1「明确不做什么」），需要时用手工调账，见 [Rebill](#rebill)；未计费的
  `FX_RATE_ERROR` 走重新入队
- 退役之后到补发之前，该币种的新事件 `FX_RATE_ERROR`、不扣费；补发把这段空档补上（见下），不会永久丢

### 立刻做什么

先看错误版本处在哪种状态（`GET /api/v1/admin/fx-rates/{fx_rate_id}`）：

| 错误版本 | 做法 |
| --- | --- |
| 还是草稿 | 丢弃：`POST …/{fx_rate_id}/discard`。BNM 草稿不可编辑，丢弃后手工录入 |
| 已发布、**正在生效**（未截断，起点已过） | 1. **先**建好正确的草稿（手工录入，`source_reference` 写清出处与「更正 <错误版本 id>」）<br>2. 退役错误版本：`POST …/{fx_rate_id}/retire`，`reason` 写清原因 —— 从退役时刻 `t` 起该币种取不到汇率<br>3. **立刻**发布正确草稿，**不给 `effective_from`**：按 P4 从退役时刻起首尾相接补上空档（设计 §2 P4、E9 ①） |
| 已发布、**尚未开始**（预约的） | 退役即撤销预约：它变成空区间、永不生效；前一个版本若仍是已发布、且正是被它截断的，恢复为未截断（R2；以退役结束的不恢复）。然后按需重新发布正确的版本 |
| 已发布、**已被截断**（历史版本） | 409 `FX_RATE_NOT_RETIRABLE`：它的区间已经结束，不能退役。已计费事件的汇率不在 Rebill 范围内（设计闸门 #830 §1「明确不做什么」），需要时用手工调账，见 [Rebill](#rebill) |

先建草稿再退役，是为了把「退役 → 补发」之间无汇率的空档压到几秒；即使拖久了，补发不给时刻也会从退役时刻起补上。

### 恢复

1. 版本列表里：错误版本 `RETIRED [原起点, 退役时刻)`，正确版本 `PUBLISHED [退役时刻, null)`，两者首尾相接
2. `fx_stale` 保持绿（正确版本的报价日在 5 天内）
3. 审计里有 `FX_RATE_RETIRE`（带 `reason`）与 `FX_RATE_PUBLISH` 两条，按 `entity_type = fx_rate_version` 可查
4. 错误版本生效期间已计费的事件：列出受影响的时间段与币种（记进当次事故记录）。已计费事件的汇率不在 Rebill 范围内（设计闸门 #830
   §1「明确不做什么」），需要时用手工调账，见 [Rebill](#rebill)；未计费的 `FX_RATE_ERROR` 走重新入队

### 绝不能做什么

- **不要直接 `UPDATE` 已发布版本的 `rate`。**触发器会拒绝；即使绕过去，已按它计费的事件与快照里的 `fx_rate_applied`
  就对不上了 —— 这正是 INV-6 要防的
- **不要用「给一个过去的 `effective_from`」来回溯纠正。**F 早于 `t` 一律 422 `EFFECTIVE_FROM_IN_PAST`：只有「无汇率 →
  有汇率」可以覆盖过去，已取到汇率的时刻不许改
- **不要退役一个正在生效的版本而不马上补发。**退役之后该币种的新事件全部 `FX_RATE_ERROR`、不扣费，直到下一次发布

---

## FX 告警维度不上报（Healthchecks 报 `fx_fetch` / `fx_stale`「未上报」）

**首次编写：2026-10-04（AIH-TASK-043）。依据：设计闸门 #183 v3 §2「内部告警接口」、§5 最后两行；[deployment.md](deployment.md) §8.4。**

### 怎么发现

Telegram：`ai_billing_hub fx_stale` 与其余告警维度的检查**一起**变 DOWN（2026-11-21 起 `fx_fetch` 并进了 `fx_stale`，
不再有自己的检查），原因是「该到的 ping 没到」（Healthchecks 宽限期 15 分钟过了），而不是收到 `/fail`。`monitor.sh` 在这几种情况下**刻意不推**告警维度：

| 巡检日志（`journalctl -t billing-monitor`） | 原因 |
| --- | --- |
| `alerts endpoint is not answering 200` | API 挂了（`readyz` 维度应同时 DOWN）；或接口返回 404 / 401 / 503 |
| `the monitor token file is unreadable or empty` | 宿主机上读不到令牌文件（权限、路径） |
| `no BILLING_MONITOR_TOKEN_HOST_FILE configured` | `.env` 里没配宿主机路径 |
| 只有一个维度 DOWN，日志 `no BILLING_HEALTHCHECK_ALERT_…_URL configured` | 那个维度的地址没写进 `.env` |
| 所有检查一起 DOWN | cron 没跑或整台机器挂了 —— 按「celery-beat 停止调度」「整台 VPS 没了」处理 |

### 影响什么

只影响**告警**：告警维度此刻没人看着。计费、拉取、发布都不受影响。

### 立刻做什么

1. `readyz` 也 DOWN：先按 API / 数据库故障处理，告警维度会随之恢复
2. 只有告警维度的检查 DOWN：在宿主机上看状态码（不带令牌）：
   `curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:<端口>/internal/alerts`

   | 状态码 | 含义 | 处理 |
   | --- | --- | --- |
   | 401 | nginx 放行、应用的令牌可用 —— 问题在宿主机那一侧 | 查 `BILLING_MONITOR_TOKEN_HOST_FILE` 指向的是不是 `secrets/monitor.token`、部署用户能不能读（下面「配置项」） |
   | 404 | 应用侧令牌不可用（文件读不出、少于 32 个字符）；应用日志每次请求一条 ERROR（不含路径与内容） | 修文件的属主 / 权限 / 内容 |
   | 403 | nginx 拒绝 —— 名单与来源对不上 | 核对 `deploy/nginx/billing.conf` 里 `/internal/alerts` 与 `/readyz` 的名单（应完全相同） |
   | 503 | 令牌通过但查库失败 | 看 API 日志 `Evaluating the alert dimensions failed` |

3. 修完手工跑一次：`BILLING_MONITOR_RECHECK_SECONDS=0 bash deploy/monitor.sh`

### 恢复

两个检查收到成功 ping 即转绿（或 `/fail`，那就是真有 FX 问题，按上面两节处理）。

### 绝不能做什么

- **不要把令牌写进 `.env`、crontab 或命令行**（`curl -H "Authorization: Bearer …"`）：同机任何用户 `ps` 都看得到，
  而这台 VPS 上还跑着另外八个项目。`.env` 里只放文件路径
- **不要为了让部署用户读到而把文件改成 `0444` 或复制第二份。**属主 `10001`、属组部署用户的主组、`0440` 就够；
  第二份要和第一份同步轮换，迟早对不上
- **不要把 `/internal/alerts` 的 nginx 名单放宽**来「先通了再说」：它与 `/readyz` 同一份，测试钉着

---

## 用量摄取：幂等冲突（`usage_event_conflicts` 告警）与摄取中断

**首次编写：2026-10-05（AIH-TASK-029）。依据：设计闸门 #176 v8（[AIH-TASK-029-usage-ingest.md](design/AIH-TASK-029-usage-ingest.md)
§2「告警」「nginx」、§5）；接口契约见 [api.md](api.md#集成用量事件摄取)。**

### 怎么发现

| 信号 | 说明 |
| --- | --- |
| Telegram：`ai_billing_hub usage_event_conflicts` 变 DOWN，正文 `P2 usage_event_conflicts: <n> conflicts in 24 hours, earliest <UTC 时刻>` | **主信号**（spec §23 要求的数据完整性告警，P2）。判据：最近 24 小时里记下了任何一条冲突行。`monitor.sh` 按维度名读 `.env` 的 `BILLING_HEALTHCHECK_ALERT_USAGE_EVENT_CONFLICTS_URL`，脚本不用改；没配这个地址时巡检日志会写 `no BILLING_HEALTHCHECK_ALERT_USAGE_EVENT_CONFLICTS_URL configured` |
| 管理端审计：`GET /api/v1/admin/audit-logs?action=USAGE_EVENT_IDEMPOTENCY_CONFLICT` | 每条**新的**冲突一条审计（同一冲突请求重发不再加），`entity_id` 是被撞的原事件，`after_state` 有请求方的 `api_key`、`key_version` 与不符类型 |
| 管理端审计：`action=USAGE_EVENT_SCOPE_MISMATCH` | 集成方在载荷里报的 `tenant_id` / `project_id` 与凭据不符（403）。不进告警维度，查冲突时顺带看一眼：常与配置串了有关 |
| **摄取中断**（spec §120 的 Billing ingestion outage） | 平台侧能观测到的中断就是 API 或数据库不可用，**由现有的 `services`（`api` / `mysql` / `billing_nginx` 容器）与 `readyz` 两个检查覆盖**，不另设维度。应用侧「发不出去」由 Phase 3 的积压监控覆盖。⚠️ Redis 不可用**不是**摄取中断：nonce 层放行、事件照收（API 日志里每个请求一条 `nonce_store_unavailable` WARNING），按「Redis / Celery broker 不可用」处理 |

### 影响什么

| 情形 | 仍然正常 | 受影响 |
| --- | --- | --- |
| 幂等冲突 | 原事件一个字节都没改，照常计费；冲突请求**没有任何财务效果** | 冲突的那个请求被拒（409，`retryable = false`），在集成方那边进死信 |
| 摄取中断 | 集成方的 outbox 退避重试，事件留在应用侧，不丢（REQ-AVAIL-001：AI 服务本身不受影响） | 中断期间没有新事件落库，计费随之推迟 |

### 立刻做什么

1. 看冲突明细（只读）：
   ```bash
   docker compose exec mysql sh -c 'mysql -u"$MYSQL_USER" -p"$MYSQL_PASSWORD" "$MYSQL_DATABASE" -e "
     SELECT c.received_at, c.event_id, c.api_key, c.mismatch, e.public_id, e.received_at
       FROM usage_event_conflicts c JOIN usage_events e ON e.id = c.usage_event_id
      ORDER BY c.id DESC LIMIT 20"'
   ```
2. 按 `mismatch` 判断：

   | `mismatch` | 多半是 |
   | --- | --- |
   | `FINGERPRINT` | 同一个 `api_key` 用同一个 `event_id` 发了不同的内容：集成方的 `event_id` 生成或 outbox 改写了已排队的载荷 |
   | `OWNERSHIP` | 另一个 `api_key`（另一个项目）用了别人已用过的 `event_id`，内容相同：两套系统共用了 ID 来源，或复制了载荷 |
   | `BOTH` | 同上，内容也不同 |

3. 联系 `api_key` 对应的集成方修复。冲突的事件在他们那边是死信：修好之后用**新的** `event_id` 重投。
4. 摄取中断：先按 `readyz` / `services` 的告警处理 API 或数据库故障；恢复后集成方自行补投，已经落库的事件重投得到
   `200 already_received`，不会重复计费。

### 恢复

- 冲突维度在最后一条冲突行满 24 小时后自动转绿；冲突行本身永久保留（只增），不需要也不能清理。
- 摄取中断随 API / 数据库恢复而结束，`readyz` 与 `services` 转绿即可。

### 部署后检查：超大请求经 nginx 返回 JSON 413

集成前缀 `/api/v1/integration/` 的 nginx 上限是 1 MiB，超过时由 nginx 返回与应用同一个 JSON 信封（`PAYLOAD_TOO_LARGE`、
`retryable: false`、`request_id: null`），而不是默认的 HTML。静态配置由 `tests/backend/test_compose.py` 钉着，**经过 nginx 的
行为只能部署后实测**：合并部署 AIH-TASK-029（以及以后每次改 `deploy/nginx/billing.conf` 的集成前缀块）之后，在 VPS 上：

```bash
head -c $((1024 * 1024 + 1)) /dev/zero \
  | curl -s -X POST -H 'Content-Type: application/json' --data-binary @- \
      -w '\nHTTP %{http_code}\n' http://127.0.0.1:<BILLING_HTTP_PORT>/api/v1/integration/usage-events
```

期望恰好是：

```text
{"success":false,"data":null,"error":{"code":"PAYLOAD_TOO_LARGE","message":"request body too large"},"retryable":false,"request_id":null}
HTTP 413
```

把这两行原样写进该次任务记录（PR 描述）。得到 HTML 或别的状态码：集成前缀块没生效（`nginx -t`、`docker compose exec
billing_nginx nginx -T | grep -A4 integration` 核对挂载的配置），按「绝不能做什么」最后一条处理，不要先调大上限。

### 绝不能做什么

- **不要删冲突行，也不要改原事件来「消告警」**：冲突表只增（触发器拒绝 UPDATE / DELETE），它是「这个 `event_id` 被别人
  用过」的证据，也是告警的依据；原事件不变是 INV-11 的保证
- **不要把原事件的状态改成 `IDEMPOTENCY_CONFLICT`**：冲突是那个请求的结果，不是原事件的状态；改了它，合法事件就不计费了
- **不要让集成方「改了内容、沿用同一个 `event_id` 再发」**：永远是冲突。更正一律用新的 `event_id`
- **不要为了「先通了再说」调大集成前缀的 `client_max_body_size`**：应用侧单条上限是 16 KiB、批量的另由批量摄取定，
  两层的上限与返回的信封要一起改

---

## 用量计费：上线前步骤、错误事件、积压与卡住回收

**首次编写：2026-10-05（AIH-TASK-032）。依据：设计闸门 #181 v2（[AIH-TASK-032-usage-billing.md](design/AIH-TASK-032-usage-billing.md)
§2「状态机」「重新入队」「告警维度」「认领、租约与防护令牌」、§5、§8）；接口契约见
[api.md](api.md#管理端用量事件重新入队)，表、触发器与锁顺序见 [database-schema.md](database-schema.md)。**

计费 worker 由 celery-beat 的 N + 1 个条目驱动：`usage-billing-sweep-1` … `usage-billing-sweep-N` 每 10 秒一起派出
N 个扫描，认领并计费到期的事件（`RECEIVED`，或退避到期的 `FAILED_RETRYABLE`），`usage-stale-recovery` 每 60 秒回收
租约已过期的 `PROCESSING`。摄取端不入队：**数据库里的事件状态就是待办清单**，Redis / Celery / Beat 丢了、停了，恢复后
第一轮扫描接着做，什么都不丢、不重复扣。

**扫描并行与扫描名额**（AIH-TASK-141，设计闸门 #522 v4，[AIH-TASK-141-billing-sweep-parallelism.md](design/AIH-TASK-141-billing-sweep-parallelism.md)）：

- N 是 `app/core/celery_app.py` 的常量 `USAGE_BILLING_SWEEP_PARALLELISM`（当前 2）。每个扫描先抢一个「扫描名额」：MySQL
  命名锁 `<库名>:usage-billing-sweep:<i>`（`i = 1 … N`，`GET_LOCK` 超时 0，库名让压测库、测试库不与生产库抢同一组名额）。
  **抢不到就立即返回 0、不认领任何事件**；所以同时计费的扫描最多 N 个，扫描跨过 10 秒周期（等锁、积压大）时新派的扫描
  几毫秒内返回，积压留到名额释放后的下一轮。
- 扫描在认领每一批、处理每个事件之前确认名额仍在（`IS_USED_LOCK(name) = CONNECTION_ID()`）；名额连接断开（网络、被
  KILL、MySQL 重启）时扫描停下、记 warning，本批没处理的事件留在 `PROCESSING`，租约（120 秒）过期后由卡住回收放回、
  再被认领计费。抢名额时 `GET_LOCK` 返回 NULL 或出错：这一轮跳过、记 warning，下一个 10 秒再来。释放失败时名额连接被
  丢弃，MySQL 随连接断开释放命名锁。子进程崩溃时命名锁随连接自动释放，没有要清理的残留。
- 查看谁占着名额（排障）：`SELECT IS_USED_LOCK(CONCAT(DATABASE(), ':usage-billing-sweep:', 1));`（2 同理），返回占用者的
  连接 id，空闲时为 NULL。
- celery-worker 的并发默认 **3**；⚠️ `BILLING_CELERY_CONCURRENCY` **必须至少比 N 多 1**。调小到 ≤ 2 时，积压期间两个扫描
  占满全部子进程，其他周期任务（状态推送等）排在后面、过期的触发被丢弃等下一个周期：不丢数据，只是延迟变大。
  见 [deployment.md](deployment.md) 3.2「celery-worker 并发与计费扫描并行数」。

### 上线前要做的（合并部署之前）

不做的话，事件全部停在错误状态（不扣费、不丢，修好后重新入队即可，但告警会一直红）：

1. **至少一条全局默认定价规则**：管理端定价规则发布一条 `GLOBAL` 的 MARKUP（或 FIXED_RATE）规则。没有任何规则时事件
   进 `PRICING_ERROR` / `NO_PRICING_RULE`。
2. **试点模型的供应商价格**：Phase 3 试点用到的每个 `(provider, model)` 先在 AI 目录里建好（含别名），再发布价格版本，
   覆盖它们的全部计价分量（LLM 四个 token 分量、转写的 `AUDIO_SECOND` 等）。缺了进 `MODEL_UNKNOWN` 或 `PRICING_ERROR`。
3. **USD 汇率**：价格以 USD 计时必须有生效的 USD → MYR 汇率（BNM 草稿由管理员发布，或手工录入），否则进
   `FX_RATE_ERROR`。见上面「FX 汇率：过期，或没有生效的汇率」。
4. **Healthchecks 新建七个检查**（Simple，5 分钟，宽限 15 分钟，Telegram），Ping URL 写进 `.env`（`monitor.sh` 按维度名
   读，脚本不用改；规则见「配置项」里 `BILLING_MONITOR_TOKEN_HOST_FILE` 一节）：

   | 检查 | `.env` 的键 |
   | --- | --- |
   | `ai_billing_hub usage_pricing_error` | `BILLING_HEALTHCHECK_ALERT_USAGE_PRICING_ERROR_URL` |
   | `ai_billing_hub usage_fx_rate_error`（2026-11-21 并进 `usage_pricing_error`，检查删除） | `BILLING_HEALTHCHECK_ALERT_USAGE_FX_RATE_ERROR_URL` |
   | `ai_billing_hub usage_model_unknown`（2026-11-21 并进 `usage_pricing_error`，检查删除） | `BILLING_HEALTHCHECK_ALERT_USAGE_MODEL_UNKNOWN_URL` |
   | `ai_billing_hub usage_failed` | `BILLING_HEALTHCHECK_ALERT_USAGE_FAILED_URL` |
   | `ai_billing_hub usage_processing_backlog` | `BILLING_HEALTHCHECK_ALERT_USAGE_PROCESSING_BACKLOG_URL` |
   | `ai_billing_hub wallet_negative_balance` | `BILLING_HEALTHCHECK_ALERT_WALLET_NEGATIVE_BALANCE_URL` |
   | `ai_billing_hub outbox_backlog` | `BILLING_HEALTHCHECK_ALERT_OUTBOX_BACKLOG_URL` |

   之后 Healthchecks 用量 8 + 2 + 1 + 7 = 18（免费档上限 20；2026-11-21 合并维度后整个项目是 17 个，见 [deployment.md](deployment.md) §8.4）。验证：`BILLING_MONITOR_RECHECK_SECONDS=0 bash deploy/monitor.sh`，
   日志里出现这七个维度的行，七个检查变绿。
5. 迁移 0018 是纯加列与约束（文件头有 §132 第 13 条分析），部署顺序照常「先迁移、再换镜像」；一旦有事件被计费，**只能
   前滚**（downgrade 会丢快照）。

### 怎么发现

| 信号 | 说明 |
| --- | --- |
| `usage_pricing_error` 变 P2（2026-11-21 起这一行合并 `usage_pricing_error`、`usage_fx_rate_error`、`usage_model_unknown` 三个成员，共用 `ai_billing_hub usage_pricing_error` 一个检查） | 有事件停在某个错误状态。正文按成员列出 `usage_pricing_error <级别>: … \| usage_fx_rate_error <级别>: … \| usage_model_unknown <级别>: …`，是 P2 的那个成员就是事件所在的状态。每个成员的摘要有条数、最早接收时刻与至多 3 个细分码（`NO_PROVIDER_PRICE`、`MISSING_PROVIDER_COMPONENT`、`NO_PRICING_RULE`、`MISSING_RULE_COMPONENT`） |
| `usage_failed` 变 P2 / **P1** | 有 `FAILED_FINAL`，或有 `FAILED_RETRYABLE` 已尝试 ≥ 3 次。**P1** = 有 `LEDGER_CONFLICT`（同一事件已有金额不同的账本行，只可能是缺陷）或 `AMOUNT_OUT_OF_RANGE`（计算结果超出 DECIMAL(20,8)） |
| `usage_processing_backlog` 变 P2 / **P1** | 最早的可处理事件等了 > 5 分钟（> 30 分钟 P1），或有租约过期仍未回收的 `PROCESSING`（> 10 分钟 P1 = **回收任务本身停了**）。摘要有到期数、`PROCESSING` 数、最早认领时刻、过期数。另两段（AIH-TASK-158，设计闸门 #565 v6 §2「告警」）接在后面，各报 P2：**缺失对账单** `<n> statements missing, earliest period <YYYY-MM>` = 某期到期后 6–30 小时内仍有合格租户没有对账单（没有时是 `0 statements missing`），按「月度对账单」一节处置；**晚到用量** `late usage 24h <a>/<b> rows (<x>%), <y>% of amount` = 最近 24 小时入账的预付用量里，过了原用量月 cut-off 才入账的条数或金额占比 ≥ 1%（宽限日内入账的上月用量不算），投递链路有延迟，按下面积压的处置查根因。第六段（AIH-TASK-250，设计闸门 #755 v1 §2「告警」）报 P2：**供应商成本未对账** `provider cost <code>,<code> not reconciled for <YYYY-MM>`（供应商代码按字母升序）= 上一个 UTC 月结束后第 9 至 12 天（`月末 + 9 天 ≤ now < 月末 + 12 天`，只报 72 小时，之后自动回到 OK，不长期遮住同一检查后来的故障）内，该月有经 AI 模块的已处理用量的供应商还没有当前对账记录；处置：到管理端「Provider costs」页录入上月 Console 成本页的消耗额（不含税）。首个对账月之前的月份不报；没有要报的时是 `provider cost reconciled or not due`。摘要恰好 `<积压>; <PROCESSING>; <过期租约>; <缺失对账单>; <晚到用量>; <供应商成本>`，级别取最严重的一段 |
| `wallet_negative_balance` 变 P2 / **P1** | 一行合并负余额与钱包核对（设计闸门 #474 v13 §2「告警」），摘要恰好 `<负余额部分>; <核对部分>`，只有计数与日期。P2：有钱包余额 < 0（§7 第 7 条接受透支；停机规则仍是 `balance <= 0`，ADR-0010；摘要 `<n> wallets below zero`）；或核对从未完成（`reconciliation never completed`）、最近一次完成超过 30 小时（末尾 `stale`）；或有时钟异常的运行（`clock anomaly <行数>`）。**P1** = 核对发现余额与账本不一致（`inconsistent <M>` 或 `later inconsistent <K>` 大于 0），按下面「钱包核对发现不一致」处理。摘要末尾出现 `; payments: …` 时是支付的判据（AIH-TASK-175，设计闸门 #580 v3 §2「告警」；AIH-TASK-181，设计闸门 #585 v3 §2「告警」：未结案的复核一直报到结案、24 小时内的争议与退款、过期一小时仍 `PENDING`），按下面「支付：复核、事件冲突、事件积压与配置（`wallet_negative_balance` 告警的支付部分）」处理（争议与退款也在那一节） |
| `outbox_backlog` 变 P2 | 一行合并积压与死信（AIH-TASK-149），摘要恰好 `<积压部分>; <死信部分>`，只有计数与时刻；只看有处理器的 outbox 行（密码重置信、门户邀请信、`NOTIFICATION_EMAIL_REQUESTED`（通知邮件，AIH-TASK-152）、`PAYMENT_RECEIPT_EMAIL_REQUESTED`（付款邮件，AIH-TASK-198）、`PASSWORD_CHANGED_EMAIL_REQUESTED`（改密通知，设计闸门 #751 v3）与两类通知事件 `tenant.low_balance`、`tenant.billing_status_changed`）。两种 P2 可同时出现。**积压**：积压部分是 `<n> row(s) pending over 10 minutes, oldest created <时刻>`（OK 时是 `no handled outbox row pending over 10 minutes`）= 有行超过 10 分钟仍 `PENDING`：worker 或邮件出了问题，按「Redis / Celery broker 不可用」与邮件配置排查。**死信**：死信部分是 `<n> dead letter(s) in 7 days, latest <时刻>`（OK 时是 `no dead letter in 7 days`）= 最近 7 天有行重试用尽结算为 `FAILED`，`latest` 是其中最晚的 `processed_at`，按下面「outbox 死信的处置」处理 |
| worker 日志 | `Recovered a usage event stuck in PROCESSING past its lease`（WARNING，带 `event_id`、`claimed_at`、`attempt_count`）；`A usage event's ledger reference was already posted with another amount`（ERROR，账本冲突）；`Billing a usage event failed; the attempt is recorded`（WARNING，带错误类型与码） |

### 影响什么

| 情形 | 仍然正常 | 受影响 |
| --- | --- | --- |
| 错误状态（`MODEL_UNKNOWN` / `PRICING_ERROR` / `FX_RATE_ERROR` / `FAILED_FINAL`） | AI 服务与摄取不受影响；事件留在库里、不丢；**没有扣费**，余额不动 | 这些事件迟迟不扣费：客户的余额偏高、停机来得晚（透支风险），直到修好配置、重新入队 |
| 处理积压 / 回收停了 | 同上；积压的事件恢复后按顺序计费，每个事件至多一次 | 扣费延迟（§119：p95 ≤ 60 秒、p99 ≤ 5 分钟的目标不达标），停机判定随之推迟 |
| 负余额 | 计费照常（§7 第 7 条接受透支），停机跃迁与那一笔扣费同事务 | 客户在停机前透支了多少，就是平台的信用风险 |
| 账本冲突（缺陷） | 原账本行不动，没有第二次扣费 | 这一个事件没有计费，要人工排查 |

### 立刻做什么

先排查（只读）：

```bash
docker compose exec mysql sh -c 'mysql -u"$MYSQL_USER" -p"$MYSQL_PASSWORD" "$MYSQL_DATABASE" -e "
  SELECT status, error_code, provider_code_raw, model_code_raw, COUNT(*), MIN(received_at), MAX(attempt_count)
    FROM usage_events
   WHERE status IN (\"MODEL_UNKNOWN\", \"PRICING_ERROR\", \"FX_RATE_ERROR\", \"FAILED_FINAL\", \"FAILED_RETRYABLE\")
   GROUP BY status, error_code, provider_code_raw, model_code_raw"'
```

按状态与细分码修配置：

| 状态 / `error_code` | 多半是 | 修什么 |
| --- | --- | --- |
| `MODEL_UNKNOWN` | 上报的 `provider` / `model` 在目录里没有，也没有在 `occurred_at` 生效的别名 | AI 目录建模型或映射别名（从 `occurred_at` 起生效的段） |
| `PRICING_ERROR` / `NO_PROVIDER_PRICE` | 该模型在 `occurred_at` 没有已发布的价格版本 | 发布价格版本。生效区间要覆盖事件的 `occurred_at`：时间线为空时不指定时刻发布的第一个版本是「一直以来」；之后只在末尾追加、从发布时刻起生效，已经过去的空档补不上（见下「绝不能做什么」） |
| `PRICING_ERROR` / `MISSING_PROVIDER_COMPONENT` | 价格版本缺该计量类型的某个分量 | 发布一个分量齐全的新版本 |
| `PRICING_ERROR` / `NO_PRICING_RULE` | 没有任何一级规则覆盖（连全局默认都没有） | 发布全局默认规则 |
| `PRICING_ERROR` / `MISSING_RULE_COMPONENT` | 命中的 FIXED_RATE 规则缺该计量类型的分量（**不下落**到更低一级） | 发布一条分量齐全的规则 |
| `FX_RATE_ERROR` | 非 MYR 的价格在 `occurred_at` 没有生效的汇率 | 发布汇率 |
| `FAILED_FINAL` / `STALE_PROCESSING_EXHAUSTED` | 认领了 10 次（`BILLING_USAGE_MAX_ATTEMPTS`）都没处理完：worker 反复崩在这一条上 | 查 worker 日志与这条事件，修好后重新入队 |
| `FAILED_FINAL` / `LOCK_WAIT_TIMEOUT` / `DEADLOCK` / `UNEXPECTED_ERROR` | 意外异常重试用尽（`error_message` 是异常类型） | 查日志，修好后重新入队 |
| `FAILED_FINAL` / `ACCOUNT_CLOSED` | 租户已关户（`CLOSED`）：不入账，进人工复核（AIH-TASK-020 的契约） | 人工复核；**不要**重新入队 |
| `FAILED_FINAL` / `AMOUNT_OUT_OF_RANGE` | 计算结果超出 DECIMAL(20,8)：多半是价格或数量录错 | 核对价格版本与事件数量；修的是价格就发布新版本后重新入队 |
| `FAILED_FINAL` / `LEDGER_CONFLICT` | **缺陷**：账本里已有这个 `event_id` 的行而金额不同 | 原账本行不动；按缺陷排查，用 `SYSTEM_CORRECTION` 记补偿行。重新入队会被拒（409 / 触发器） |
| `FAILED_RETRYABLE` | 正在退避重试（2^n 秒、封顶 300 秒），会自动再认领 | 不用管；≥ 3 次时 `usage_failed` 告警，按 `error_code` 查 |

修好配置之后**重新入队**。只对**从未产生财务效果**的错误事件（四个错误状态之一、账本里没有它的行）。每条一个事务、一条 `USAGE_EVENT_REQUEUE`
审计，`reason` 必填。改回 `RECEIVED` 后下一轮扫描（10 秒内）照常计费。

```bash
# 一条：路径里是事件的 public_id
curl -s -X POST -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"reason": "Published the missing price version"}' \
  https://<主机>/api/v1/admin/usage-events/<usage_event_id>/requeue

# 批量：同一状态（必填）与可选筛选，一次至多 1000 条，超过就再调一次
curl -s -X POST -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"status": "PRICING_ERROR", "error_code": "NO_PRICING_RULE", "reason": "Published the global default rule"}' \
  https://<主机>/api/v1/admin/usage-events/requeue
```

响应只有放回 / 跳过的条数与 `public_id`。放回后若又进同一错误状态，说明配置还没修对（例如价格版本的生效时刻没有
覆盖 `occurred_at`）—— 不要反复重新入队，先查上面的表。

积压或回收停了：先按「celery-beat 停止调度」「Redis / Celery broker 不可用」确认 beat 与 worker 在跑；不需要手工补投
任何东西 —— 数据库就是待办清单。负余额：联系客户充值，或按流程调账（管理端手工调账接口）。

晚到用量占比高（AIH-TASK-158）：说明上报到入账之间有延迟（客户侧投递、摄取或计费积压），晚到的用量会进当时开放期的
对账单、归为上期调整，金额不丢。按上面积压的处置查根因（beat / worker、`ai_usage_outbox` 与 `outbox_backlog` 维度、客户侧
投递）；**不要为了消告警改账本或事件**。窗口是最近 24 小时，延迟消除后占比随窗口滑过自动回落。

缺失对账单（AIH-TASK-158）：按下面「月度对账单」一节处置 —— 先看 AIH-TASK-157 写的生成失败日志
（`Statement generation failed`），再用管理端补生成（AIH-TASK-159 上线之后）。

### 恢复

- 错误状态的维度在最后一条该状态的事件被重新入队（或修好后被计费）后自动转绿。
- 积压维度在最早的可处理事件被认领、过期的 `PROCESSING` 被回收后转绿；beat / worker 恢复后第一轮（10 秒 / 60 秒内）
  就会开始。
- 核对：`SELECT status, COUNT(*) FROM usage_events GROUP BY status`；钱包与账本一致性用 `verify_wallet`（只报告不修复）。

### 卡住回收是什么

崩溃在「认领已提交、处理未提交」之间的事件停在 `PROCESSING`（处理事务已随连接断开回滚，没有扣费）。每 60 秒的回收任务
只看**租约已过期**（认领 + `BILLING_USAGE_LEASE_SECONDS`，默认 120 秒）的行，并且用 `FOR UPDATE SKIP LOCKED`：仍被某个
处理事务持有行锁的事件（即使已过租约，例如在等钱包锁）**不会被回收**，只有持有者已崩溃的孤儿才会。回收把它改成
`FAILED_RETRYABLE`（`STALE_PROCESSING`，立即可再认领）；认领次数已达上限的改成 `FAILED_FINAL`
（`STALE_PROCESSING_EXHAUSTED`）。原处理者若事后醒来，会在处理事务第一步看到令牌不是自己的而放弃；账本
`(USAGE_EVENT, event_id)` 唯一约束是最后一道，所以**至多一次扣费**。

- 偶尔几条回收（日志里的 WARNING）= worker 被重启或崩溃过，正常。
- 持续出现、或同一事件反复回收直到 `STALE_PROCESSING_EXHAUSTED` = worker 处理这类事件时崩溃，查 worker 日志。
- `usage_processing_backlog` 报「lease expired」且 > 10 分钟（P1）= **回收任务本身没在跑**：按「celery-beat 停止调度」
  处理；beat 恢复后第一轮就会回收。

### 绝不能做什么

- **不要直接 UPDATE `usage_events` 把事件改回 `RECEIVED`**：用重新入队接口（有审计、会校验账本）。`PROCESSED` 的事件
  任何 UPDATE 都会被触发器拒绝；有账本行的错误事件改回 `RECEIVED` 也会被拒
- **不要重新入队 `ACCOUNT_CLOSED` 与 `LEDGER_CONFLICT`**：前者要人工复核，后者是缺陷，修数据要另写补偿行
- **不要为了「让事件能计价」想办法回溯发布价格、规则或汇率**（例如直接写库改 `effective_from`）：除了时间线为空时的
  第一个版本，发布只在末尾追加、从发布时刻起生效（不许回溯是价格、规则、汇率各自设计的约定）。`occurred_at` 落在空档
  里的事件，要由那时生效的版本计价 —— 那时没有就是没有，留在错误状态。Rebill 不处理未计费事件；价格与规则不能回溯发布，所以这类事件重新入队也算不出价，只能按手工调账处理（见
  [Rebill](#rebill)）
- **不要手工把卡住的 `PROCESSING` 改状态**：等租约过期由回收任务处理；手工改会和仍在处理的 worker 抢
- **不要为了消 `wallet_negative_balance` 去改余额**：余额只经账本变动（INV-4），充值或调账走管理端接口

### 配置项：`BILLING_USAGE_LEASE_SECONDS`、`BILLING_USAGE_MAX_ATTEMPTS`

| 属性 | `usage_lease_seconds` | `usage_max_attempts` |
| --- | --- | --- |
| 环境变量 | `BILLING_USAGE_LEASE_SECONDS` | `BILLING_USAGE_MAX_ATTEMPTS` |
| 默认值 | `120` 秒 | `10` 次 |
| 取值 | 正整数（`gt=0`，0 或负数进程起不来） | 正整数（同左） |
| 含义 | 认领后多久仍是 `PROCESSING` 才算卡住、可被回收。远大于一次处理的正常耗时（毫秒级）；调小会让回收与正在处理的 worker 抢同一批事件（防护令牌兜得住，但白做） | 一个事件最多被认领几次；达到后，意外异常与卡住回收都判 `FAILED_FINAL`。退避是 2^n 秒、封顶 300 秒 |

⚠️ 这两项目前**没有**在 `docker-compose.yml` 的 `x-backend.environment` 里转发（本任务不改 compose），所以容器里一律是
代码默认值；只改 `.env` 不会生效。要改需另立任务在 compose 里加转发（照 `BILLING_CREDENTIAL_ROTATION_OVERLAP_SECONDS`
的写法与 `test_compose.py` 的默认值比对）。

---

## outbox 死信的处置（`outbox_backlog` 告警的死信部分）

**首次编写：AIH-TASK-149。**

### 怎么发现

`outbox_backlog` 变 P2，摘要的死信部分是 `1 dead letter in 7 days, latest <时刻>` 或
`<n> dead letters in 7 days, latest <时刻>`：最近 7 天（`processed_at` 晚于 `now - 7 天`）有**有处理器**的
`domain_outbox` 行重试用尽、结算为 `FAILED`。以前这只有一条 worker 的 error 日志。摘要只有计数与时刻，
不带租户、`outbox_id`、`aggregate_id`、邮箱或 `event_type`。

### 立刻做什么

1. 只读查出是哪些行（只看类型与时刻，**不要把 `payload_json` 的内容贴进任何对话**：邮件类里有一次性令牌与邮箱）：

   ```sql
   SELECT id, event_type, processed_at, attempt_count
     FROM domain_outbox
    WHERE status = 'FAILED' AND processed_at > UTC_TIMESTAMP() - INTERVAL 7 DAY
    ORDER BY processed_at DESC;
   ```

2. 先找原因：对着 worker 的 error 日志（按 `outbox_id`）看是 SMTP 配置、对方拒收还是数据问题，修好根因。
3. 按类型处置：
   - **邮件类 · 密码重置**：让用户重新点「忘记密码」发起一次，新行会重新投递；旧行不动。
     同一账号 5 分钟内重复申请不会再发信（按账号限频，`BILLING_PASSWORD_RESET_MIN_INTERVAL_SECONDS`，设计闸门 #751 v3）：
     用户说「点了没收到」时先请他查垃圾箱，窗口过后再点一次。
   - **邮件类 · 门户邀请**：由管理员重新邀请（见「客户门户账号」的「邀请没收到怎么办」）。
   - **邮件类 · 通知邮件**（`NOTIFICATION_EMAIL_REQUESTED`，AIH-TASK-152）：进死信后 payload 已置空（里面有联系邮箱）。
     按这一行的 `aggregate_id`（通知的 `public_id`）在 `notifications` 表找到那条通知，再按它的 `tenant_id` 找到租户；
     门户通知不受影响。处置后**不会自动重发**这封信，需要时由人联系客户。
   - **邮件类 · 付款邮件**（`PAYMENT_RECEIPT_EMAIL_REQUESTED`，AIH-TASK-198）：进死信后 payload 已置空（里面有联系邮箱与公司名），不能重投。
     收据不受影响，客户照常可在门户下载。按这一行的 `aggregate_id`（收据的 `public_id`）在 `payment_receipts` 表找到收据号与 `payment_id`，
     再按 `payment_id` 查 `payments.public_id`，在管理端支付详情核对；处置后**不会自动重发**这封信，需要时由人告知客户收据号。
   - **邮件类 · 改密通知**（`PASSWORD_CHANGED_EMAIL_REQUESTED`，设计闸门 #751 v3）：进死信后 payload 已置空（里面有用户邮箱），不能重投。
     密码本身已经改好、会话已全部登出，不受影响。按这一行的 `aggregate_id`（即用户 id）在 `users` 表找到账号；
     怀疑账号被盗用时由人联系该用户确认是不是本人改的。处置后**不会自动重发**这封信。
   - **通知类**（`tenant.low_balance`、`tenant.billing_status_changed`）：进死信时 payload 保留，看这一行的
     `payload_json` 与 worker 的 error 日志（`Giving up on an outbox notification`，带 `outbox_id`）。

### 恢复

死信过了 7 天窗口（`processed_at` 不再晚于 `now - 7 天`），死信部分自动回到 `no dead letter in 7 days`，
不需要确认。7 天覆盖周末没人看告警的情况。

### 绝不能做什么

- **不要为了消告警去改这些行的 `status` 或 `processed_at`。**告警会自己恢复；改状态会把重试到上限都发不出去的行
  放回去再烧一轮，改时刻会抹掉排查依据。

---

## 钱包核对发现不一致（`wallet_negative_balance` 告警 P1）

**首次编写：2026-10-29（AIH-TASK-133）。依据：设计闸门 #474 v13（[AIH-TASK-129-wallet-reconciliation.md](design/AIH-TASK-129-wallet-reconciliation.md)
§1「明确不做什么」、§2「告警」、§5、§8）；问题码的判定见 AIH-TASK-005（[AIH-TASK-005-wallet-ledger.md](design/AIH-TASK-005-wallet-ledger.md)），
告警接口见 [api.md「内部告警接口」](api.md#内部告警接口)。**

钱包余额由账本插入触发器维护，正常写路径不会不一致。celery-beat 每 300 秒跑一次 `wallet-reconciliation-step`，每个马来西亚
日历日通常完成一次全量核对（03:00 之后开始），逐个钱包在只读事务里把 `wallets` 与按序的 `wallet_transactions` 对一遍，
不一致的钱包写进 `wallet_reconciliation_findings`。核对**只读、不修复**：发现不一致说明有东西绕过了触发器
（`TRUNCATE`、手工 SQL、触发器被删或被改），**按数据事故处理**。

### 怎么发现

| 信号 | 说明 |
| --- | --- |
| Telegram：`ai_billing_hub wallet_negative_balance` 变 DOWN，正文 `P1 wallet_negative_balance: … reconciliation <日期> checked <N> inconsistent <M>…` | **P1** = 「待判运行集」（最近一次完成的核对运行，加上它之后插入的全部运行：进行中、已放弃、已完成）里有运行记下了不一致。`inconsistent <M>` 是最近一次完成运行的不一致数；`later inconsistent <K>` 是之后的运行里不一致数的最大值。不一致一经提交**立即** P1，不等运行完成 |
| 同上，正文以 `P2` 开头，带 `never completed` 或 `stale` | 核对从未完成，或最近一次完成已超过 30 小时：beat / worker 停了，或某个钱包每次都读不完（worker 日志 `wallet_reconciliation_failed`，带 `stage`、`run_id`、`error_type`）。先按「celery-beat 停止调度」「Redis / Celery broker 不可用」排查 |
| 同上，带 `clock anomaly <行数>` | 有 `run_date` 晚于马来西亚今天、或 `finished_at` 晚于此刻 10 分钟以上的运行：见下「时钟异常的 P2」 |

摘要只有计数与日期，不含客户与金额：是哪些客户要到管理端核对页或按下面的查询看。

### 影响什么

| 仍然正常 | 受影响 |
| --- | --- |
| AI 服务、摄取与计费照常；核对不加锁、不阻塞记账 | 不一致的钱包余额**不可信**：停机判定（`balance <= 0`）、低余额提醒、对账单都基于这个余额 |
| 其他钱包（不一致按钱包逐个判定） | 账本被清空或篡改时，按账本重放出的余额与钱包缓存对不上，之后对这个钱包的每一笔记账都在错误的基础上累加 |

### 立刻做什么

**1. 找到客户与问题码。**管理端核对页（顶栏「钱包核对」，`/wallet-reconciliation`，AIH-TASK-134、135 上线后）：运行表里不一致数
大于 0 的那一行，点开看客户、问题码、快照里的余额、版本、账本行数与核对时刻。页面上线之前用只读查询：

```bash
docker compose exec mysql sh -c 'mysql -u"$MYSQL_USER" -p"$MYSQL_PASSWORD" "$MYSQL_DATABASE" -e "
  SELECT r.id, r.run_date, r.status, r.finished_at, t.public_id AS customer, f.problem_codes,
         f.balance, f.wallet_version, f.ledger_rows, f.checked_at
    FROM wallet_reconciliation_findings f
    JOIN wallet_reconciliation_runs r ON r.id = f.run_id
    JOIN tenants t ON t.id = f.tenant_id
   ORDER BY r.id DESC, f.tenant_id
   LIMIT 50"'
```

**2. 看懂问题码**（一个钱包可同时有几个，按下表顺序存储）：

| 问题码 | 含义 | 多半是 |
| --- | --- | --- |
| `WALLET_MISSING` | 租户没有钱包行 | 钱包行被删（单独出现，不再判其他码） |
| `LEDGER_MISSING` | 钱包版本 > 0，账本一行都没有 | 账本被 `TRUNCATE` 或整批删除 |
| `BALANCE_NOT_LEDGER_SUM` | 钱包余额不等于账本 `amount` 之和 | 钱包余额被直接改写，或账本行被删、被改 |
| `BALANCE_NOT_LAST_BALANCE_AFTER` | 钱包余额不等于最后一行的 `balance_after` | 同上 |
| `VERSION_NOT_LAST_SEQUENCE` | 钱包版本不等于最后一行的 `wallet_sequence` | 钱包版本被直接改写，或末尾的账本行被删 |
| `SEQUENCE_GAP` | 账本序号不是从 1 起连续的 1..n | 中间的账本行被删 |
| `CHAIN_BROKEN` | 某行的 `balance_before` 不等于上一行的 `balance_after`（第一行不是 0） | 历史账本行被篡改，或中间有行被删 |
| `ROW_ARITHMETIC_BROKEN` | 某行 `balance_after` 不等于 `balance_before + amount` | 绕过 CHECK 改了账本行（正常情况下数据库写不进去） |
| `BILLING_STATUS_MISMATCH` | 客户的 `billing_status` 与余额不符（余额 > 0 应为 `ACTIVE`，≤ 0 应为 `SUSPENDED`） | 租户行或钱包余额被直接改写 |

**3. 按数据事故流程处置**（只读排查在前，任何修复都要在流程内、有审计）：

1. **冻结排查**：通知负责人，停止对这个客户的人工调账与充值，直到查清；需要时经管理端账户状态把客户停用（`DISABLED`），
   集成方随之拦截 AI 调用，不再产生新的扣费。在工单里记下运行日期、客户 `public_id` 与问题码（不贴余额截图到聊天）。
2. **核对触发器与审计**：确认账本与钱包的触发器还在、没有被改（只读）：

   ```bash
   docker compose exec mysql sh -c 'mysql -u"$MYSQL_USER" -p"$MYSQL_PASSWORD" "$MYSQL_DATABASE" -e "
     SELECT TRIGGER_NAME, EVENT_MANIPULATION, EVENT_OBJECT_TABLE, CREATED
       FROM information_schema.TRIGGERS
      WHERE TRIGGER_SCHEMA = DATABASE()
        AND EVENT_OBJECT_TABLE IN (\"wallets\", \"wallet_transactions\")"'
   ```

   与 [database-schema.md](database-schema.md) 列出的触发器逐个比对；再查 `audit_logs` 里这段时间的管理操作、MySQL 的
   慢日志或通用日志里有没有绕过应用的 `TRUNCATE` / `UPDATE` / `DELETE`，以及最近一次发布与迁移。先找到原因，再修数据。
3. **修复**，二选一：
   - **从备份恢复**：按「整台 VPS 没了（灾难恢复）」的恢复与「恢复后对账」步骤，恢复到出问题之前的备份，再补回之后的
     合法变动；
   - **在维护窗口内人工修复**：由管理员在维护窗口内按事故工单与本 runbook 修复，修复前先备份，修复本身记审计（谁、何时、
     为什么、改了哪些行）。
4. **验证**：修好后等下一次核对（或在次日 03:00 之后的运行完成后）确认这个客户不再出现在 findings 里。

**为什么 `SYSTEM_CORRECTION` 修不好**：账本插入触发器要求新行的 `balance_before` 等于钱包当前余额、`wallet_sequence` 等于钱包
版本 + 1。账本被清空、历史行被篡改或钱包缓存被直接改写之后，补记一行只会在已经坏掉的链后面再接一环：`LEDGER_MISSING`、
`SEQUENCE_GAP`、`CHAIN_BROKEN` 这些问题一个都消不掉，还会让账本更难还原。`SYSTEM_CORRECTION` 只能在账本与钱包本身一致的前提下
调整余额（例如给客户补偿），不是修复数据损坏的工具。

**时钟异常的 P2 如何核查**：摘要里的 `clock anomaly <行数>` 来自 `run_date` 晚于马来西亚今天、或 `finished_at` 晚于此刻
10 分钟以上的运行（时钟偏快时插入，或有人手工插入）。只读查出这些行：

```bash
docker compose exec mysql sh -c 'mysql -u"$MYSQL_USER" -p"$MYSQL_PASSWORD" "$MYSQL_DATABASE" -e "
  SELECT id, run_date, status, started_at, finished_at, wallets_checked, wallets_inconsistent
    FROM wallet_reconciliation_runs
   WHERE run_date > DATE(UTC_TIMESTAMP() + INTERVAL 8 HOUR)
      OR finished_at > UTC_TIMESTAMP() + INTERVAL 10 MINUTE"'
```

再核对主机与容器的时钟（`date -u`、`timedatectl` 的 NTP 同步状态）。时钟修好后，未来日期的行在那个日期到来时变回正常行，
P2 随之消失；这些行记下的不一致照样算 P1、照样要处理。

### 恢复

- **P1 一直持续到出现一次没有不一致的完成运行**：那时待判运行集只剩它和之后的运行。修复后最晚在次日 03:00 之后的运行完成时
  转绿。这是有意的：数据没真正修好，告警就不消。
- `stale` / `never completed`：beat 与 worker 恢复后核对从库里的游标继续，下一次完成即转绿。
- 核对：管理端核对页最新一次完成运行的不一致数为 0；或上面的 findings 查询里最新的完成运行没有行。

### 绝不能做什么

- **不得在流程之外直接改 `wallets` 或 `wallet_transactions`**（包括为了消告警改余额、改版本、删或补账本行）：余额只经账本变动
  （INV-4），历史账本不可变（INV-5）；人工修复只在维护窗口内、按事故工单、记审计
- **不要用 `SYSTEM_CORRECTION` 或手工调账「抹平」不一致**：理由见上，它修不好这些问题码，只会把损坏埋得更深
- **不要删 `wallet_reconciliation_runs` / `wallet_reconciliation_findings` 的行来消 P1**：它们是事故的证据（外键也是 `RESTRICT`）
- **不要为了消 `clock anomaly` 手工改运行的 `run_date` 或 `finished_at`**：修的是时钟，不是记录

---

## 支付：复核、事件冲突、事件积压与配置（`wallet_negative_balance` 告警的支付部分）

**首次编写：2026-11-10（AIH-TASK-175）。依据：设计闸门 #580 v3（[AIH-TASK-172-payments-gateway.md](design/AIH-TASK-172-payments-gateway.md)
§2「支付 Webhook」「事件处理任务」「告警」、§5）；告警接口见 [api.md「内部告警接口」](api.md#内部告警接口)。
更新：AIH-TASK-181，设计闸门 #585 v3（[AIH-TASK-179-payments-review.md](design/AIH-TASK-179-payments-review.md)
§2「争议与退款」「告警」）：复核一直报到结案、争议与退款、过期一小时仍 `PENDING`。
更新：AIH-TASK-201，设计闸门 #589 v2（[AIH-TASK-196-payment-receipts.md](design/AIH-TASK-196-payment-receipts.md)
§2「补发扫描」「告警」）：入账一小时后仍没有收据。**

Stripe 回调验签通过后只落库（`payment_gateway_events`），不入队；celery-beat 每 `BILLING_PAYMENT_EVENT_POLL_SECONDS`（缺省 15 秒）
跑一次 `payment-events-process-due`，一次领一个到期事件、向 Stripe 查询权威状态后确认支付。`charge.dispute.created`（争议 / 拒付）与
`charge.refunded`（Stripe 后台退款）两种事件也落库为 `RECEIVED`，但事件任务对它们**不查 Stripe、不改支付、不改钱包**，只按
`payment_intent` 关联到支付（`gateway_payment_intent_id`）：关联上为 `PROCESSED`，没有 `payment_intent` 或找不到为 `UNMATCHED`
（`PAYMENT_NOT_FOUND`）。Healthchecks 已满，支付的信号并进 `wallet_negative_balance` 维度：摘要在原有两部分之后追加
`; payments: review open <a>, event conflicts 24h <b>, oldest open event <c>s, unmatched or rejected 24h <d>, stale pending <e>, disputes or refunds 24h <f>, receipts missing 1h <g>, config <ok|missing>`，
没有要报的时不出现这一段。支付开关（`BILLING_PAYMENTS_ENABLED`）关闭时事件任务每一轮直接返回。
收据正常与入账同一事务开具；开具前的检查不过时入账照常提交、没有收据，由 celery-beat 每 300 秒跑一次的补发扫描
`receipts-issue-missing`（`app.tasks.receipts.issue_missing`）补开（不发付款邮件）。补发扫描**不看支付开关**，开关关闭时照常运行。

### 怎么发现

| 信号 | 说明 |
| --- | --- |
| `review open <a>` 大于 0（**P1**） | 有支付处于 `REVIEW_REQUIRED`（金额、币种或引用不符，同一 PaymentIntent 两笔内部支付，Stripe 状态不一致，租户已关户或是内部租户）：**没有入账**，钱已在 Stripe。**不论进入多久，一直报到结案**（「按实付入账」或「不入账结案」）为止 |
| `disputes or refunds 24h <f>` 大于 0（**P1**） | 最近 24 小时收到了 `charge.dispute.created` 或 `charge.refunded`（不含 `IGNORED` 的，例如 `LIVEMODE_MISMATCH`；不含关联到「不入账结案」支付的 `charge.refunded`，那是结案后在 Stripe 退款的预期结果）。**支付与钱包都没有被改动**，按下面「争议与退款」处理；24 小时后自动不再计入 |
| `stale pending <e>` 大于 0（P2） | 有支付 `PENDING` 且 `expires_at` 已过去一小时以上：补对账在过期后的第一个小时每 5 分钟查一次，过了一小时还没收口，多半是补对账没在工作或 Stripe 持续不可用。少数是正常的：会话已 `complete` 而异步付款仍在处理，这时会一直报到付款有结果或 7 天超时 |
| `receipts missing 1h <g>` 大于 0（P2） | 有支付 `PAID`、`paid_at` 已过去一小时以上（覆盖 12 轮补发扫描）仍没有收据：开具检查持续不过（账本行缺失、税务政策读不出、当年编号系列缺失或耗尽），或补发扫描停了。钱已入账，只是客户与管理员暂时下载不到收据 |
| `event conflicts 24h <b>` 大于 0（**P1**） | 最近 24 小时同一 Stripe 事件 id 收到了内容不同的请求体（`fingerprint_conflict_at`）；原行未被覆盖。worker 日志 `Payment event redelivered with a different body; the stored row is kept`（ERROR，带事件 id 与类型） |
| `config missing`（**P1**） | 开关打开但凭据不可用：两个 Stripe 密钥文件有一个读不到或为空、Stripe 密钥前缀不是 `sk_live_` / `sk_test_`、签名密钥前缀不是 `whsec_`、`BILLING_STRIPE_API_BASE_URL` 为空，或 `BILLING_FRONTEND_BASE_URL` 为空。回调与建单返回 503，事件任务不领取事件。API 与 worker 日志有 `payments credential unavailable: …`（只带文件路径与原因） |
| `oldest open event <c>s` 超过 900（P2） | 有 `RECEIVED` / `RETRY` / `PROCESSING` 的事件已等了 15 分钟以上：Stripe 不可用、beat / worker 停了，或事件一直重试（`last_error_code` 见下） |
| `unmatched or rejected 24h <d>` 大于 0（P2） | 最近 24 小时有事件结算为 `UNMATCHED`（`PAYMENT_NOT_FOUND`、`NO_CLIENT_REFERENCE`）或 `REJECTED`（Stripe 拒绝了权威查询，例如 `resource_missing`；`GATEWAY_REFERENCE_TAKEN`；`NO_GATEWAY_REFERENCE`） |
| worker 日志 | `Payment event not processed`（WARNING，带事件 id、类型、`processing_status`、`error_code`、`attempts`）；`Payment event lease was lost; the result is discarded`（WARNING：租约过期后被另一轮重领，重领那一轮的结果为准）；`Payment needs review`（WARNING，带支付 `public_id` 与原因） |
| 补发扫描的日志 | 每轮结束一条 `Receipt back-fill finished`（INFO，只带 `issued` 与 `failed` 两个计数；`failed` 持续大于 0 就是有支付补不上）；开具检查不过时 `Receipt not issued; a pre-issue check failed`（ERROR，带原因码 `reason` 与支付 `payment_id`）；某一笔抛了异常时 `Receipt back-fill failed for a payment`（ERROR，只带支付 `payment_id`，不带异常文本） |

摘要只有计数与秒数，不含客户、金额与 Stripe 对象 id：具体是哪些支付与事件按下面的查询看。

### 影响什么

| 仍然正常 | 受影响 |
| --- | --- |
| AI 服务、用量计费、其他客户的充值；已入账的支付不会被重复入账（三层唯一约束） | `REVIEW_REQUIRED` 的那几笔钱已收而钱包未加，客户可能仍处于停机；事件积压期间付款的客户迟迟看不到到账；`config missing` 时所有回调 503（Stripe 会自动重试约三天） |

### 立刻做什么

**1. 看事件。**只读查询最近的非正常事件（`metadata_json` 只有白名单字段，没有个人数据）：

```bash
docker compose exec mysql sh -c 'mysql -u"$MYSQL_USER" -p"$MYSQL_PASSWORD" "$MYSQL_DATABASE" -e "
  SELECT e.id, e.gateway_event_id, e.event_type, e.processing_status, e.last_error_code, e.attempts,
         e.received_at, e.next_attempt_at, e.processed_at, e.fingerprint_conflict_at, p.public_id AS payment
    FROM payment_gateway_events e
    LEFT JOIN payments p ON p.id = e.payment_id
   WHERE e.processing_status IN (\"RECEIVED\", \"RETRY\", \"PROCESSING\", \"UNMATCHED\", \"REJECTED\")
      OR e.fingerprint_conflict_at IS NOT NULL
   ORDER BY e.received_at DESC
   LIMIT 50"'
```

**2. 看复核单。**

```bash
docker compose exec mysql sh -c 'mysql -u"$MYSQL_USER" -p"$MYSQL_PASSWORD" "$MYSQL_DATABASE" -e "
  SELECT p.public_id, t.public_id AS customer, p.status_reason, p.expected_amount, p.confirmed_amount,
         p.confirmed_currency, p.gateway_payment_id, p.review_required_at
    FROM payments p JOIN tenants t ON t.id = p.tenant_id
   WHERE p.status = \"REVIEW_REQUIRED\"
   ORDER BY p.review_required_at DESC
   LIMIT 50"'
```

**3. 按原因处置：**

| 原因 | 多半是 | 做什么 |
| --- | --- | --- |
| 复核单（`status_reason`） | 见设计 §2「可信确认」的判定表 | 在 Stripe Dashboard 按 `gateway_payment_id` 核对会话与付款。人工处理出口属于后续批次；此前只记录在工单里，**不要**手工改支付状态或补账本行 |
| `event conflicts` | 有人用同一事件 id 发了不同内容（重放或伪造），或 Stripe 侧异常 | 按事件 id 在 Stripe Dashboard 看原始事件；核对签名密钥是否泄露，泄露时按配置项一节轮换 |
| `GATEWAY_UNAVAILABLE` / `GATEWAY_STATE_UNKNOWN` / `CONFIRM_CONFLICT`（`RETRY`） | Stripe 不可用或返回了表外状态；并发确认撞了唯一约束 | 事件按退避自动重试、不放弃（最长约一小时一次）；Stripe 恢复后自然消化。积压不降时先按「celery-beat 停止调度」「Redis / Celery broker 不可用」排查 |
| `NO_GATEWAY_REFERENCE`（`RETRY` 或 `REJECTED`） | 建单时 Stripe 超时、支付没有会话 id，而事件也不带会话 id | 支付过期前重试；过期后 `REJECTED`。按支付 `public_id` 在 Stripe Dashboard 查是否真有付款 |
| `GATEWAY_REFERENCE_TAKEN`（`REJECTED`） | 事件的会话 id 已属于另一笔支付 | 不会入账；在 Stripe 核对会话的 `client_reference_id`，按复核处理 |
| `PAYMENT_NOT_FOUND` / `NO_CLIENT_REFERENCE`（`UNMATCHED`） | 不是本系统建的会话（例如 Dashboard 手工建的付款链接），或测试与生产的端点配串了 | 核对 Stripe Dashboard 的回调端点只订阅那四种 `checkout.session.*` 事件与 `charge.dispute.created`、`charge.refunded`（共六种）、指向正确的环境。争议与退款事件的 `UNMATCHED` 见下面「争议与退款」 |
| `disputes or refunds` | Stripe 侧发生了争议（拒付）或有人在 Stripe 后台退款 | 见下面「争议与退款」 |
| `receipts missing` | 开具检查持续不过，或补发扫描停了 | 见下面第 5 步 |
| `stale pending` | 补对账没在工作，或 Stripe 持续不可用 | 先按「celery-beat 停止调度」「Redis / Celery broker 不可用」排查，并看 worker 日志里补对账的 ERROR；Stripe 恢复后补对账自然收口。会话已 `complete`、异步付款仍在处理的，等它有结果 |
| Stripe 错误码（`REJECTED`，如 `resource_missing`） | 会话在 Stripe 不存在：密钥与端点不属于同一账户或模式 | 核对密钥文件与 `BILLING_STRIPE_API_BASE_URL` |
| `config missing` | 密钥文件没放、权限不对、前缀错，或地址没配 | 看日志里 `payments credential unavailable` 点名的文件与原因，修好后重启 api 与 worker |

**4. 争议与退款（`disputes or refunds 24h`）。**系统只告警、**不改账**（spec §8、§112.1：不做自动退款，外部退款不得直接改余额）。
先查这 24 小时的争议与退款事件和它们关联的支付：

```bash
docker compose exec mysql sh -c 'mysql -u"$MYSQL_USER" -p"$MYSQL_PASSWORD" "$MYSQL_DATABASE" -e "
  SELECT e.gateway_event_id, e.event_type, e.processing_status, e.last_error_code, e.received_at,
         JSON_UNQUOTE(JSON_EXTRACT(e.metadata_json, \"$.payment_intent\")) AS payment_intent,
         p.public_id AS payment, p.status, p.status_reason, p.resolution
    FROM payment_gateway_events e
    LEFT JOIN payments p ON p.id = e.payment_id
   WHERE e.event_type IN (\"charge.dispute.created\", \"charge.refunded\")
     AND e.processing_status <> \"IGNORED\"
   ORDER BY e.received_at DESC
   LIMIT 50"'
```

按关联到的支付分三种处理：

| 关联到的支付 | 做什么 |
| --- | --- |
| `PAID`（自动入账或人工「按实付入账」） | Kelvin 在 Stripe Dashboard 核实争议或退款的金额后，需要扣回时用现有的人工调账 `REFUND_ADJUSTMENT`（借方）扣回，**原因里写支付编号**（`public_id`）。**支付本身保持 `PAID`**：扣回是一条新的账本行，不改原来的 `TOPUP`（INV-4、INV-5） |
| 「不入账结案」（`resolution = CLOSED`） | 结案后在 Stripe 后台退款产生的 `charge.refunded` 是预期结果，钱从没入过账，**不计入告警、无需扣回**。落在这种支付上的争议照样计入 P1：同样无需扣回，在 Stripe 里应诉或接受即可 |
| `GATEWAY_PAYMENT_REUSED` 的复核单 | 它的 PaymentIntent 记在另一笔已 `PAID` 的支付上，这笔钱已经给那一笔入过账。所以这种复核单**结案时不在 Stripe 退款**（退款等于把已入账的钱退回，要先扣回那一笔）；若已误退，退款事件会关联到那笔 `PAID`，按第一行扣回 |

另外几种情形：

- 争议或退款落在仍是 `REVIEW_REQUIRED` 的支付上：先按人工复核出口结案（钱已被退或被拒付时选「不入账结案」，且不再在 Stripe 退款），不改账。
- 事件 `UNMATCHED`（没有 `payment_intent`，或 PaymentIntent 不在任何支付上）：在 Stripe Dashboard 按 PaymentIntent 找到对应的付款与它的
  `client_reference_id`，再按它对应的支付归入上面各种。不入账结案时没记下 PaymentIntent 的支付（例如 `GATEWAY_INCONSISTENT`），结案后的退款
  关联不上、照样计入 P1：核对它确实是刚做的那笔结案退款即可，不改账。

**5. 缺收据（`receipts missing 1h`）。**先找出是哪几笔（只读）：

```bash
docker compose exec mysql sh -c 'mysql -u"$MYSQL_USER" -p"$MYSQL_PASSWORD" "$MYSQL_DATABASE" -e "
  SELECT p.public_id, p.paid_at, p.tax_policy_version_id
    FROM payments p
    LEFT JOIN payment_receipts r ON r.payment_id = p.id
   WHERE p.status = \"PAID\" AND r.id IS NULL
   ORDER BY p.paid_at, p.id
   LIMIT 50"'
```

再看 worker 日志里补发扫描的记录：`Receipt back-fill finished` 一条都没有，是补发扫描没在跑，先按「celery-beat 停止调度」
「Redis / Celery broker 不可用」排查，恢复后下一轮自动补齐。有记录而 `failed` 大于 0，按这几笔的 `public_id` 找
`Receipt not issued; a pre-issue check failed` 的原因码，按 [payment-flow.md](payment-flow.md) 的收据一节处理：

| 原因码 | 多半是 | 做什么 |
| --- | --- | --- |
| `RECEIPT_SERIES_UNAVAILABLE` | 当年（马来西亚时间）没有编号系列行（2099 年之后），或这一年的 `last_sequence` 已到 999999 | 不能手工补行或改计数（会让编号复用）：要新的迁移补系列行（过设计闸门）。上线后补发扫描自动收口 |
| `RECEIPT_LEDGER_MISSING` | 这笔支付没有 `TOPUP` 账本行，或金额不是两位小数的正数 | **数据事故**：正常入账路径里账本行与收据同一事务、先写账本，不会出现。账本只追加，不能靠改数据恢复；告警会一直报，按事故上报 Kelvin，等人工开具或更正的设计 |
| `RECEIPT_TAX_POLICY_UNREADABLE` | 支付建单时那一版税务政策的 `treatment_json` 读不出 | 同上，按数据事故处理；税务政策版本有触发器拒绝改删，不能靠改数据恢复 |
| 只有 `Receipt back-fill failed for a payment` | 开具时数据库错误（死锁、断连） | 下一轮自动重试；持续出现时按 MySQL 的排查步骤看连接与锁 |

### 恢复

- `event conflicts 24h`、`unmatched or rejected 24h`、`disputes or refunds 24h` 在事件发生 24 小时后自动归零，摘要随之不再出现支付段落。
- `review open` 一直报到复核单结案；结案即消失。
- `stale pending` 在那笔支付收口（`PAID` / `EXPIRED` / `FAILED` / `REVIEW_REQUIRED`）后消失。
- `receipts missing 1h` 在补发扫描给那笔支付开出收据后消失；账本行缺失或税务政策读不出的那几笔会一直报。
- `oldest open event` 在积压消化后回到 0；`config missing` 在配置修好、重启后消失。

### 绝不能做什么

- **不要手工把事件改成 `PROCESSED` 或改 `attempts` / `next_attempt_at` 来消告警**：事件是入账的触发，改掉会让真实付款不再被确认
- **不要手工改 `payments.status`、补 `TOPUP` 账本行或直接改钱包**：入账只经 `confirm_payment` 与账本（INV-3、INV-4），
  `REVIEW_REQUIRED` 永不自动入账
- **不要因为争议或退款去改支付状态或原 `TOPUP` 行**：扣回只用人工调账 `REFUND_ADJUSTMENT` 新增一行（INV-5）
- **不要删 `payment_gateway_events` 的行**：它是去重的依据（同一事件 id 只落一行），删掉后 Stripe 的重投会被当成新事件
- **不要为了消 `receipts missing` 手工插收据行或改 `receipt_number_series`**：编号只由开具在系列行锁下分配，手工改会让编号重复或复用；
  收据行插进去就不可改、不可删

### 过期之后到账（`EXPIRED` 之后的付款成功）

**首次编写：2026-11-22（AIH-TASK-230）。依据：`app/models/payments.py:83`、`app/services/payments.py:252-301`、
`app/services/payment_reconcile.py:11-27`、`docs/payment-flow.md:136`、`docs/payment-flow.md:294`（ADR-0007）。**长时间 `PENDING`
见上面「怎么发现」的 `stale pending` 一行，网关事件积压见 `oldest open event` 一行；这里只补 `EXPIRED` 之后才到的付款成功。

`EXPIRED` **不是终态**（`FINAL_STATUSES` 只有 `PAID` 与 `REVIEW_REQUIRED`）：之后收到这笔的付款成功事件，事件任务照常认回会话 id、
向 Stripe 查权威状态，按同一张判定表落状态 —— 判定全部通过就 `EXPIRED` → `PAID` 自动入账（同一事务开收据），任何一条不过就
`EXPIRED` → `REVIEW_REQUIRED`，**不入账**。⚠️ 补对账只查 `PENDING` 的支付，不会回头再查已经 `EXPIRED` 的：过期之后的到账只靠
Stripe 的事件送进来。

#### 怎么发现

- 自动入账是正常路径，**没有告警**：worker 日志 `Payment status changed`（INFO，`old_status` 为 `EXPIRED`、`new_status` 为 `PAID`），
  审计 `GET /api/v1/admin/audit-logs?action=PAYMENT_STATUS_CHANGE`
- 判定不过：`review open` 大于 0（**P1**），按上面的复核单处理
- 客户反馈「付了款，页面却显示已过期」

#### 影响什么

- 自动入账：钱已收、入账一次（三层唯一约束），客户在事件处理之前看到的是 `EXPIRED`
- 进了复核：钱在 Stripe、钱包没加，客户可能仍处于停机，直到结案
- 事件一直没到：钱在 Stripe、支付停在 `EXPIRED`，平台这一侧没有任何信号

#### 立刻做什么

1. 找出过期之后才入账的支付（只读）：

   ```bash
   docker compose exec mysql sh -c 'mysql -u"$MYSQL_USER" -p"$MYSQL_PASSWORD" "$MYSQL_DATABASE" -e "
     SELECT public_id, status, status_reason, expires_at, paid_at, review_required_at
       FROM payments
      WHERE (status = \"PAID\" AND paid_at > expires_at)
         OR (status = \"REVIEW_REQUIRED\" AND review_required_at > expires_at)
      ORDER BY id DESC
      LIMIT 50"'
   ```

2. 已经 `PAID`：不用处理；收据没开出来时看上面第 5 步（`receipts missing`）
3. `REVIEW_REQUIRED`：按上面第 3 步的复核单处理，结案走 [payment-flow.md §6](payment-flow.md#6-复核处理)
4. 客户说已付款而支付仍是 `EXPIRED`：用第 1 步「看事件」的查询找这笔的网关事件；按支付 `public_id` 在 Stripe Dashboard 查是否真有
   付款。有付款而事件没到或没认回（`NO_GATEWAY_REFERENCE` 那一行），记进工单交 Kelvin

#### 恢复

- 支付变成 `PAID` 且有收据，或复核单已结案

#### 绝不能做什么

- **不要手工把 `EXPIRED` 改成 `PAID`、补 `TOPUP` 行或直接改钱包**：入账只经 `confirm_payment`（见上面「绝不能做什么」）
- **不要看到 `EXPIRED` 就在 Stripe 给客户退款**：之后的付款成功事件可能已经让它自动入账；先确认支付的当前状态
- **不要为了「让它再查一次」改 `expires_at` 或 `last_reconciled_at`**：补对账的候选条件全写在这些列上，改了会让它查错单子

---

## 状态 webhook：推送连续失败、缺签名密钥、签名错误（`status_webhook` 告警）

**首次编写：2026-10-12（AIH-TASK-052）。依据：设计闸门 #241 v6（[PHASE-3-status-sync.md](design/PHASE-3-status-sync.md)
§2「健康告警维度」「配置」、§4「投递行」、§5、§8「回滚」「监控」）；接口契约见 [api.md](api.md#管理端项目集成状态与状态-webhook-地址)
与 [api.md「内部告警接口」](api.md#内部告警接口)。**

平台把每个项目**当前**的有效状态签名推送到管理员设置的 `status_webhook_url`。celery-beat 每 15 秒跑一次投递：
项目的投递游标（`project_status_deliveries` 每个项目一行）落后于客户的 `status_version` 就发，只有 2xx 算送达，其余按
`min(30 × 2^(n−1), 3600)` 秒退避重试，**不设放弃上限**。只有 `integration_status = ENABLED` 且设了地址的项目推送，
告警也只统计这些项目。

### 上线前要做的（合并部署之前）

1. **Healthchecks 新建一个检查**（Simple，5 分钟，宽限 15 分钟，Telegram），Ping URL 写进 `.env`（`monitor.sh` 按维度名
   读，脚本不用改；规则见「配置项」里 `BILLING_MONITOR_TOKEN_HOST_FILE` 一节）：

   | 检查 | `.env` 的键 |
   | --- | --- |
   | `ai_billing_hub status_webhook` | `BILLING_HEALTHCHECK_ALERT_STATUS_WEBHOOK_URL` |

   之后 Healthchecks 用量 18 + 1 = **19**（免费档上限 20，只剩 1 个；2026-11-21 合并维度后整个项目是 17 个，见 [deployment.md](deployment.md) §8.4）。三类问题合在这一个检查里，就是为了不超上限
   （设计 §2）。验证：`BILLING_MONITOR_RECHECK_SECONDS=0 bash deploy/monitor.sh`，日志里出现这个维度的行，
   检查变绿。没配这个键时巡检日志写 `no BILLING_HEALTHCHECK_ALERT_STATUS_WEBHOOK_URL configured`，Healthchecks 宽限期后报
   「未上报」。
2. ⚠️ 心跳地址**不要**写进仓库或文档（理由见「配置项」那一节的「绝不能做什么」）。

### 怎么发现

| 信号 | 说明 |
| --- | --- |
| Telegram：`ai_billing_hub status_webhook` 变 DOWN，正文 `P1 status_webhook: failing <a>, missing secret <b>, signing error <c>` | **P1** = `signing error > 0`：有项目最近一次因 `SECRET_DECRYPT_FAILED`（密钥解不开：密文损坏、AAD 不符）或 `ENCRYPTION_NOT_CONFIGURED`（主密钥未配置）没有发送，且投递落后。加密配置坏了 |
| 同上，正文以 `P2` 开头 | `failing > 0`：有项目投递落后且连续失败 ≥ 6 次（按默认退避，第一次失败后约 15 分钟）；或 `missing secret > 0`：有项目没有 `ACTIVE` 签名密钥（由数据推导，不看错误码），平台不会不签名就发送 |
| worker 日志 `A status webhook was not sent: the signing secret could not be opened`（ERROR） | 带项目 `public_id`、`status_version`、`error_code`、`key_version`。不带地址、密钥 |
| worker 日志 `A status webhook was not sent: the project has no active signing secret`（WARNING） | 带项目 `public_id`、`status_version`、`error_code = NO_ACTIVE_SECRET` |
| worker 日志 `A status webhook was not delivered; it is retried with backoff`（WARNING） | 带项目 `public_id`、`status_version`、`http_status`、`error_code`（`TIMEOUT` / `CONNECTION_ERROR` / `HTTP_STATUS`） |

摘要只有三个计数，不含客户与项目 id：是哪些项目要按下面「立刻做什么」查库。

### 影响什么

| 仍然正常 | 受影响 |
| --- | --- |
| 客户的 AI 服务：投递失败**不改任何状态**（不变量 1），集成方按最后一次接受的状态执行 | 集成方本地状态更新变慢：只能靠每 4 分钟一次的[周期对账](api.md#对账)拿到新状态（§119 的 5 分钟上限仍由对账兜住） |
| 计费、摄取、余额、账户与项目状态本身 | 停机 / 复机传到集成方的时间从秒级变成对账周期；对账也失败时，集成方停在旧状态 |
| 投递游标：待推送的版本全在 MySQL，修好后自动补发最新版本，不丢 | `signing error` 若是主密钥未配置，**所有**设了地址的项目一起停发 |

### 立刻做什么

先查是哪些项目（只读；**不要 SELECT `status_webhook_url`**，路径里可能带集成方的令牌）：

```bash
docker compose exec mysql sh -c 'mysql -u"$MYSQL_USER" -p"$MYSQL_PASSWORD" "$MYSQL_DATABASE" -e "
  SELECT p.public_id AS project, t.public_id AS customer, t.status_version, d.delivered_version,
         d.attempt_count, d.last_error_code, d.last_http_status, d.last_attempt_at, d.next_attempt_at,
         EXISTS (SELECT 1 FROM project_webhook_secrets s
                  WHERE s.project_id = p.id AND s.status = \"ACTIVE\") AS has_active_secret
    FROM project_status_deliveries d
    JOIN projects p ON p.id = d.project_id AND p.tenant_id = d.tenant_id
    JOIN tenants t ON t.id = d.tenant_id
   WHERE p.integration_status = \"ENABLED\" AND p.status_webhook_url IS NOT NULL
     AND (COALESCE(d.delivered_version, -1) < t.status_version
          OR NOT EXISTS (SELECT 1 FROM project_webhook_secrets s
                          WHERE s.project_id = p.id AND s.status = \"ACTIVE\"))"'
```

然后按类别处理（`…` 是 `/api/v1/admin/customers/{customer_id}/projects/{project_id}`，都要 ADMIN）：

| 类别 | 多半是 | 怎么处理 |
| --- | --- | --- |
| `signing error`，`last_error_code = SECRET_DECRYPT_FAILED` | 这个项目的 `ACTIVE` 密文解不开（损坏，或 AAD 对不上） | **签发并启用新密钥**：`POST …/webhook-secrets`（`{}`，响应里的 `secret` 只出现这一次，当场交给集成方）→ 集成方配置成新旧版本都接受 → `POST …/webhook-secrets/{key_version}/activate`（`{}`）。启用时旧 `ACTIVE` 同一事务退役，并把这个项目的投递重排到「立即」，下一轮（≤ 15 秒）就发。已有 `PENDING` 时签发会 409：集成方已拿到它就直接启用，拿不到就先退役它再签发 |
| `signing error`，`last_error_code = ENCRYPTION_NOT_CONFIGURED` | worker 读不到主密钥：`master.key` 文件缺失、挂成了目录，或 `BILLING_MASTER_KEY_FILE` 没配 | **补主密钥文件后重启**：按上面「主密钥（`master.key`）丢失或要在新主机上重建」放回文件、核对指纹（**不要生成新的**），然后重启后端容器：只是文件内容的事用 `docker compose restart celery-worker`；改了 `.env` 里的键则要 `docker compose up -d` 重建（理由见「配置项」）。不用逐个项目处理：每行在自己的退避到期时（上限 1 小时）自动重发 |
| `missing secret` | 项目设了地址，但从未启用过密钥，或 `ACTIVE` 被退役了（例如泄露时）而没有启用新的 | 同第一行：**签发并启用**一把密钥。启用后下一轮就发 |
| `failing`，`last_error_code = TIMEOUT` / `CONNECTION_ERROR` | 集成方接收端不可达：宕机、DNS、证书、防火墙 | **检查集成方地址与它的应答**：`GET …/status-webhook` 看地址（只在终端里看，不要贴进工单、聊天或截图），请集成方确认接收端在跑、能从公网以 https:443 访问 |
| `failing`，`HTTP_STATUS` 且 `last_http_status` 是 3xx | 地址变了，对方在做重定向；平台**不跟随重定向** | 向集成方要最终地址，`PUT …/status-webhook` 改成它（新地址会收到一次当前状态） |
| `failing`，`HTTP_STATUS` 且 4xx | 多半是集成方验签不过：用错了 `key_version` 的密钥、规范串写错、本机时钟偏差超过 300 秒 | 请集成方按 [api.md「状态 webhook 签名」](api.md#状态-webhook-签名) 核对；轮换期间它要同时接受新旧版本 |
| `failing`，`HTTP_STATUS` 且 5xx | 集成方接收端自身出错 | 请集成方查自己的日志；平台照常重试，不用管 |

**关闭推送**（例如集成方要停机维护、或地址已经作废而新地址还没有）：清除项目的地址。

```bash
curl -s -X PUT -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"url": null}' \
  https://<主机>/api/v1/admin/customers/<customer_id>/projects/<project_id>/status-webhook
```

- 清除后投递行保留但不再被领取，这个项目也不再计入告警；集成方经周期对账照常拿到状态。客户已关户（`CLOSED`）也能清除。
- 一个地址都不推了也是这样逐个项目清除：**生产只前滚，关闭推送不需要回滚迁移**（设计 §8）。
- 要恢复时再 `PUT` 一个地址：游标清零，新地址会收到一次当前状态。

### 恢复

维度在问题消失后的下一轮巡检（5 分钟内）自动转绿，不需要手工清除：

- `failing`：这一行送达一次（`attempt_count` 归零），或追上了客户的版本。
- `missing secret`：项目有了 `ACTIVE` 密钥。
- `signing error`：这一行送达一次（成功结算清空 `last_error_code`）。只启用了新密钥还不够，要等它真的发出去并收到 2xx。

核对：再跑一遍上面的查询，结果为空；worker 日志里不再有上面三种 WARNING / ERROR。

### 绝不能做什么

- **不要直接 UPDATE `project_status_deliveries`**（清 `attempt_count`、`last_error_code`，或改 `delivered_version`）来消告警。
  改 `delivered_version` 等于宣布「已送达」，集成方就再也收不到那个版本；清计数只是把问题藏起来。要立刻重发，启用密钥或
  重设地址都会合法地重排
- **不要用停用项目（`integration-status` 改 `DISABLED`）来关闭推送。**那会让这个项目的有效状态变成 `BLOCK_AI`，集成方拦截
  客户的 AI 调用。关闭推送只用清除地址
- **不要为了消 `signing error` 生成一把新的主密钥覆盖上去。**所有已有密文（TOTP、集成凭据、webhook 密钥）会一起解不开；
  主密钥只能放回原来那份（见「主密钥」一节）
- **不要退役 `ACTIVE` 签名密钥却不启用新的**：项目立刻变成 `missing secret`，推送停止
- **不要把完整地址写进工单、聊天、截图或日志查询结果**：路径里可能带集成方的令牌；审计里也只记主机名

---

## 状态推送乱序

**首次编写：2026-11-22（AIH-TASK-230）。依据：`app/services/status_delivery.py:4-20`、`app/services/status_delivery.py:440-480`；
[api.md「状态 webhook 签名」](api.md#状态-webhook-签名)里「请求体」的 `event_id` 规则、[api.md「集成方怎么应用」](api.md#集成方怎么应用)、[api.md「对账」](api.md#对账)。**

平台这一侧的保证：

- 每个项目一行投递行，只有 `delivered_version` 落后于客户的 `status_version` 才推送；领取时从同一次读取组装快照与版本，重试也是
  重新领取、发的是当时的最新快照，中间版本被合并
- 结算带领取令牌，令牌对不上（改了地址、启用了新密钥、租约被接手）什么都不写；送达时 `delivered_version` 取新旧两者的较大值，
  **只增不减**
- `event_id` 是 `sevt_<项目 public_id>_<status_version>`，同一版本重发时不变

仍然会乱序：推送与集成方每 4 分钟一次的周期对账（`effective-status`）是两条并行的路，网络也可能让先发的请求后到。所以契约要求
**集成方按 `status_version` 判断**：版本更大才应用，同版本幂等，更小的忽略并回 2xx。

### 怎么发现

平台**没有**这个场景的告警维度：版本比较发生在集成方那边，平台看不到集成方本地存的版本。能看到的信号：

- 客户或集成方报告状态不对：充值复机之后仍被拦截，或停机之后仍然放行
- `status_webhook` 维度的 `failing`，`last_http_status` 是 4xx：集成方对旧版本回了非 2xx（契约要求回 2xx），按「状态 webhook」一节
  那一行请集成方核对

### 影响什么

| 仍然正常 | 受影响 |
| --- | --- |
| 平台的计费、余额、账户与项目状态：投递不改任何状态（不变量 1） | 集成方若按到达顺序应用，旧状态会覆盖新状态：该拦的放行（透支风险）或该放的拦截（客户 AI 服务中断） |
| 投递游标：不会因为乱序往回走 | 直到下一次推送或对账带来更高的版本之前，集成方停在错误的状态 |

### 立刻做什么

1. 只读查平台当前的版本与这个项目的投递游标（**不要 SELECT `status_webhook_url`**）：

   ```bash
   docker compose exec mysql sh -c 'mysql -u"$MYSQL_USER" -p"$MYSQL_PASSWORD" "$MYSQL_DATABASE" -e "
     SELECT p.public_id AS project, t.public_id AS customer, t.status_version, d.delivered_version,
            d.delivered_at, d.attempt_count, d.last_error_code, d.last_http_status
       FROM project_status_deliveries d
       JOIN projects p ON p.id = d.project_id AND p.tenant_id = d.tenant_id
       JOIN tenants t ON t.id = d.tenant_id
      WHERE p.public_id = \"<project_id>\""'
   ```

2. 请集成方报出它本地这个项目保存的 `status_version`，与上面的 `status_version` 比：
   - 本地更小：它漏了更新 —— 投递落后时按「状态 webhook」一节排查；投递已追上（`delivered_version` 等于 `status_version`）时，
     请集成方查它是否拒绝或丢弃了那次推送，下一次周期对账会把它拉到最新
   - 本地相等或更大，但状态与平台不同：它没有按版本判断（按到达顺序或按时间覆盖了）
3. 请集成方按 [api.md「集成方怎么应用」](api.md#集成方怎么应用)修正：只按 `status_version` 比较，旧版本忽略并回 2xx；对账用同一条规则
4. 平台这一侧**不需要重发**：游标落后时会自动推送最新版本，已追上时没有更新的状态可发

### 恢复

- 集成方本地的 `status_version` 与平台一致，状态与 `effective-status` 的返回相同
- `status_webhook` 维度没有因 4xx 产生的 `failing`

### 绝不能做什么

- **不要直接 UPDATE `project_status_deliveries`**（例如把 `delivered_version` 改小）来「让平台重发」：理由见「状态 webhook」一节
- **不要为了让集成方收到一次推送去改客户的账户、计费或项目集成状态**：那是真实的状态跃迁，版本 +1、有效状态随之改变、写审计
- **不要停用项目来「止住乱序」**：有效状态会变成 `BLOCK_AI`，集成方拦截客户的 AI 调用
- **不要同意集成方改用 `effective_at` 或到达时间排序**：契约只认 `status_version`

---

## 税务政策：首条政策的发布（只做一次）

**首次编写：2026-10-19（AIH-TASK-079）。依据：设计闸门 #332 v3 §2「接口：CLI」、§5、§6、§8 部署顺序第 4、5 步；
[ADR-0008](adr/ADR-0008-sst-tax-treatment.md) 第 5 节与「收口条件」。**

这一节不是故障场景，是一次性的运维步骤，所以不套五节，写 **什么时候做 · 执行前核对 · 怎么做 · 期望结果与失败处理 ·
核对 · 做完之后 · 绝不能做什么**。部署侧的位置见 [deployment.md](deployment.md) §9.8。

⚠️ 下面的 `<部署目录>`、`<管理员邮箱>` 都是占位符：真实主机名、目录与邮箱不进仓库。

### 什么时候做

AIH-TASK-077、080、078、079 都已合并并自动部署到生产之后，**整个生产生命周期只做一次**。由 Kelvin 或经 Kelvin
授权的会话在 VPS 上执行。部署本身不会写入任何政策：合并之后 `tax_policy_versions` 一直是空表，直到有人运行这条命令。

它写入的是 MY / SST 的第一条 `PUBLISHED` 政策：未注册 SST、不收税、`rate` 为空，生效起点固定为
`2026-10-16T16:00:00` UTC（= 吉隆坡 2026-10-17 00:00），说明文字照 ADR-0008 第 5 节。内容是代码里的常量，命令没有
任何参数能改它。

### 执行前核对

1. **审批人是决策人自己的 ADMIN 账号**，状态 `ACTIVE`。账号还没有的话，先用 `create-admin` 建好并完成 2FA 注册。
2. ⚠️ **不得用生产上的验收专用管理员**（浏览器验收沿用的那个夹具 ADMIN 账号，见 `docs/TODO.md`「已阻塞」第一条）：
   `approved_by` 会永远留在这条政策上，而那个账号在第一个真实客户进生产之前就要降权或撤销。
3. **邮箱逐字核对**，对着决策人登录管理端时用的邮箱一个字一个字比。⚠️ **写错不可改**：填成另一个有效的 ADMIN
   时命令照样成功，`approved_by` 就是错的人，已发布的政策永远不能改（INV-6）；只能等以后的管理端路径退役并重发
   （另一个设计闸门）。填成不存在 / 非 ADMIN / 已停用的账号时命令会拒绝、什么都不写，改正后重跑即可。

### 怎么做

```bash
cd <部署目录>
docker compose exec -T api python -m app.cli publish-initial-tax-policy --approved-by-email '<管理员邮箱>'
echo "exit=$?"
```

- 命令**从不提示输入**，也不读标准输入，所以 `-T` 能跑；没有「确认吗？」这一步 —— 防手滑全靠上面的核对
- 唯一的参数是 `--approved-by-email`；邮箱先去首尾空白、转小写（与登录同一个规则）再匹配
- stdout 只有一行 `public_id`；说明与错误都在 stderr

### 期望结果与失败处理

| 退出码与输出 | 含义 | 下一步 |
| --- | --- | --- |
| **0**，stdout 一行 `public_id`，stderr `Published tax policy MY/SST effective from 2026-10-16T16:00:00 UTC.` | 发布成功：一条政策 + `TAX_POLICY_CREATE`、`TAX_POLICY_PUBLISH` 两条审计，同一事务 | 记下 `public_id`，做下面的「核对」 |
| **1**，stderr `A tax policy version already exists; nothing was written.` | 表里已经有政策（无论什么状态），这次什么都没写 | 已经发布过：**不要再做任何事**，按「核对」取 `public_id`。核对发现表里那一行不是预期的已发布政策（例如有人绕过服务写了草稿）→ 停下人工排查，**不得 `TRUNCATE`** |
| **1**，stderr `No active ADMIN account for <归一后的邮箱>; nothing was written.` | 邮箱没有账号、不是 ADMIN 或不是 `ACTIVE`（三种不区分），什么都没写 | 回到「执行前核对」，改正后重跑 |
| **2**，stderr 是邮箱格式的原因，或 argparse 用法 | 邮箱格式不对 / 漏了 `--approved-by-email`；没有连库 | 改参数后重跑 |
| **其他非零**，stderr 是数据库异常（不带 SQL 参数），外加 `If the error happened at commit, the policy may already be published; run the same command again to find out.` | 数据库错误。若出在 `COMMIT` 之前，整体回滚、什么都没写；若 `COMMIT` 已发出而确认丢了，**无法确定**是否已提交 —— 两者之一，不会出现半个 | **原样再运行一次**（同一条命令、同一个邮箱），见下 |
| 非零，异常不是数据库错误（例如复查失败 `TaxPolicyRecheckFailed`、`ValueError`） | 代码缺陷，事务已回滚或根本没连库 | 不要重试，开缺陷任务 |

**因数据库错误非零退出时，原样再运行一次即可判定**（「表里有任何一行就拒绝」让重跑本身就是核对，不需要人去猜）：

- 重跑得到 **退出码 1 且提示已存在** = 上次其实已经提交了。stdout 这次为空，按「核对」从库里或审计日志取 `public_id`
- 重跑得到 **退出码 0** = 上次没有提交，这次发布成功，`public_id` 就是这次 stdout 的那一行
- 重跑仍是数据库错误：先按 API / 数据库故障处理（`readyz`、MySQL 状态），恢复后再原样运行一次

进程在打印之前被杀（终端断开、容器重启，stdout 为空）也同样处理：原样重跑，退出码 1 证明已发布。

### 核对

1. 库里恰好一行，内容如设计 §2：
   ```bash
   cd <部署目录>
   docker compose exec -T mysql sh -c 'mysql -u"$MYSQL_USER" -p"$MYSQL_PASSWORD" "$MYSQL_DATABASE" -e "
     SELECT public_id, jurisdiction, policy_type, rate, status, effective_from, effective_to
     FROM tax_policy_versions"'
   ```
   期望：一行，`MY`、`SST`、`rate` 为 `NULL`、`PUBLISHED`、`effective_from = 2026-10-16 16:00:00`、`effective_to` 为 `NULL`。
   `public_id` 与命令打印的一致
2. 管理端审计日志 `GET /api/v1/admin/audit-logs?entity_type=tax_policy_version` 恰好两条：`TAX_POLICY_CREATE`、
   `TAX_POLICY_PUBLISH`，`entity_id` 都是这个 `public_id`，操作者是审批人、`user_agent` 是 `cli`、`reason` 是
   `INITIAL_POLICY`。说明文字的中文要走接口核对（`mysql` 客户端的默认字符集可能把中文显示成 `????`）

### 做完之后：解除 Phase 4 闸门

**仅代码合并、仅运行成功都不算解除。**运维步骤完成后开一个文档 PR（不走设计闸门）：

- ADR-0008「收口条件」2026-10-17 进度里把第 2、3 步标为完成，写上发布日期与 `public_id`（`public_id` 不是秘密，可以进
  公开仓库）
- `docs/TODO.md` D2 一行与 Phase 4 一节去掉「不得开工」的阻塞句

**这个 PR 合并之后，Phase 4 其余任务（充值、收据、对账单、支付）才可以登记为 `ready`**（支付部分另外还要等 D6）。

### 绝不能做什么

- **不要用 SQL 直接插入或改写 `tax_policy_versions`。**触发器会拒绝；即使绕过去，也绕过了服务层的校验、审计与事务内
  复查，政策一经发布就被以后的支付、收据、对账单永久引用
- **不要 `TRUNCATE` 后重跑。**`TRUNCATE` 是 DDL、不经触发器；重跑会让政策换一个 `public_id` 与 `approved_at`，审计里留下
  两次发布、库里只剩一次。迁移降级同理：表里有行时 `downgrade` 会拒绝，**生产只前滚**
- **不要为「换审批人」重跑。**会被拒绝（退出码 1），本来也不该：已发布的政策不可改
- **不要用验收专用管理员当审批人**，也不要在不确定邮箱时「先跑了再说」：成功之后没有撤销
- **不要把真实邮箱、主机名或部署目录写进工单、PR 或本仓库的文档**；`public_id` 可以

---

## 月度对账单

**首次编写：2026-11-06（AIH-TASK-157）。依据：设计闸门 #565 v6
（[docs/design/AIH-TASK-154-monthly-statements.md](design/AIH-TASK-154-monthly-statements.md)）§2「账期与时间」
「Celery 任务」、§5、§10「已拍板」。
更新：AIH-TASK-220，设计闸门 #708 v3（[AIH-TASK-216-statement-pdf.md](design/AIH-TASK-216-statement-pdf.md)
§2「下载」、§5、§6「日志」、§8、§10），见下「PDF 下载」。**

**什么时候生成**：每个预付租户（`billing_mode = PREPAID`）每个账期（马来西亚日历月）一份，生成即定稿（`FINALIZED`）。
账期 `M` 在新月第 2 日 01:00（马来西亚时间）到期（`cutoff(M)` = 新月第 2 日 00:00，再加 1 小时余量），之后的第一次
整点任务生成它；beat 条目 `statements-generate-due` 每小时整点（`crontab(minute=0)`，UTC）跑一次
`app.tasks.statements.generate_due`，所以正常就是 01:00 那一次。**首个账期是 `2026-12`**，2027-01-02 01:00 才到期，
之前每一轮都是空跑（日志 `Generated due monthly statements`，`count` 为 0）；不回补更早的月份。

**每小时补生成**：每一轮都从 `2026-12` 扫到最近一个到期的账期，对每个预付租户按账期升序补上缺的那些，每份一个事务；
已有的不重算。所以任务漏跑、worker 重启、Redis 被清空都不丢东西：下一个整点自动补齐。

### 怎么发现

- 每一轮一条 info 日志 `Generated due monthly statements`，`count` 是本轮新生成的份数
- 生成失败：error 日志 `Statement generation failed`，**只带**租户 `customer_id`（public_id）、账期 `period`（`YYYY-MM`）与
  异常类型名 `error_type`，不带金额与公司名。自检失败时同一轮前面还有一条 `Statement integrity check failed`，另带检查编号
  `check`；税务政策取不到时前面有一条 `Statement tax policy unavailable`
- 某个租户整体出错（不在某一期里）：error 日志 `Statement catch-up failed for a tenant`，带 `customer_id` 与 `error_type`
- 编号碰撞：warning `Statement number collided; the next run retries`（`customer_id`、`period`）。随机部分撞上了已有编号，
  本轮放弃这个租户，下一轮换随机数，**不用处理**
- 告警（AIH-TASK-158）：某期到期后 6–30 小时内（`cutoff(M) + 6h ≤ now < cutoff(M) + 30h`）仍有合格租户缺对账单，
  `usage_processing_backlog` 报 P2，摘要里这一段是 `<n> statements missing, earliest period <YYYY-MM>`（只有缺失项数与
  最早的账期，不带租户）。⚠️ **只报这 24 小时**，窗口过后维度恢复 OK：**回到 OK 不代表已补上**，之后缺失只能从上面的
  error 日志与管理端对账单列表看到，要到管理端对账单列表确认。只报 24 小时是为了不让永久停出一直占着这个维度的
  Healthchecks 检查、遮住之后的积压与过期租约（P1）
- 处置这条告警：先按上面的 error 日志（`Statement generation failed` 及同一轮的前一条）定位租户、账期与 `error_type`，
  按下表处置；之后在管理端对该客户补生成（AIH-TASK-159 上线之后），再到管理端对账单列表确认那一期已有

按 `error_type` 读：

| `error_type` | 含义 | 处置 |
| --- | --- | --- |
| `PreviousStatementMissing` | 上一期该有对账单却没有；正常不会单独出现（catch-up 按账期升序，前一期失败就停） | 找这个租户更早一期的失败日志，按那一条处置 |
| `StatementTaxPolicyUnavailable` | 该期 `cutoff` 时刻没有生效的税务政策，或读出校验失败 | 见下「税务政策取不到」 |
| `StatementIntegrityError` | 数据缺陷，自检不等（看同一轮的 `check`） | 见下「自检失败：永久停出」 |
| 数据库类异常（`OperationalError` 等） | 提交或读库失败，事务已回滚 | 先按数据库故障处理；恢复后下一个整点自动补上 |

### 影响什么

- 失败只影响这一个租户：它从失败那一期起本轮不再生成，其他租户照常
- 客户与管理员看不到那一期（及之后）的对账单；计费、扣费、钱包都不受影响（对账单只读账本与事件）
- 已定稿的对账单不会因为之后的入账而改变：晚到的行进当时开放期的对账单，归为上期调整

### 立刻做什么

**任务停了（beat 或 worker 没跑）**：先按「celery-beat 停止调度」「Redis / Celery broker 不可用」两节恢复调度。恢复之后
**等下一个整点**即可，缺的账期会一次补齐；急的话在管理端对该客户手动补生成（AIH-TASK-159 上线之后才有这个入口），
它与任务走同一个函数，已有的不会重复生成。

**税务政策取不到（`StatementTaxPolicyUnavailable`）**：

1. 先确认 MY / SST 的税务政策版本：是否有一条 `PUBLISHED` 版本覆盖日志里那一期的 `cutoff` 时刻（新月第 2 日 00:00 马来西亚
   时间）。生产自 2026-10-17 起已有一条不设尽头的版本，正常不会触发
2. 缺版本：按税务政策发布流程补一条覆盖该时刻的版本（不要用 SQL 直接写）。**下一个整点自动补上**，这个租户后面的账期也
   随之补齐
3. 版本在、但读出校验失败（`treatment_json` 不合规）：这是数据缺陷，按下面「自检失败」的处置走（查出缺陷行、不改不删、
   开设计闸门）

**自检失败：永久停出（`StatementIntegrityError`）**：

⚠️ **已知限制**（设计 §5，Kelvin 2026-11-04 拍板）：这是数据缺陷，账本与事件不可变，**重试不会好**。这个租户从该期起
**永久停出**：该期每小时都失败、无行，之后每一期都因上期缺失而无行；其他租户不受影响。本批没有放行开关，也没有作废
占位单。

1. 从 error 日志取租户 `customer_id`、账期 `period` 与检查编号 `check`
2. 按检查编号查出缺陷行（都只读）：
   - `1`：期初加本期发生额不等于期末 —— 对这个租户、这一期的账本行逐行核对归属与金额
   - `2`：收费事件的 `billable_cost` 之和与对应账本行金额之和不符 —— 找 `billable_cost ≠ −amount` 的事件与账本行
   - `3`：本期收费事件的条数与本期用量账本行的条数不符 —— 对比本期收费事件与本期 `AI_USAGE` 账本行，找对不上的那几条
   - `missing_event`：有 `AI_USAGE` 账本行连不上事件（没有任何事件的 `wallet_transaction_id` 指向它）
3. 记下缺陷行的 id 与成因（代码缺陷，或绕过服务层直接写库），**再开设计闸门定修复**（例如作废与更正版本）

### 恢复

- 任务停了、数据库故障、编号碰撞：恢复后下一个整点自动补齐，不需要人做什么
- 税务政策缺版本：补发布之后下一个整点自动补齐
- 自检失败：**没有自动恢复**，只能等设计闸门定下的修复上线

### 绝不能做什么

- **不要手工 UPDATE 或 DELETE 对账单行。**定稿的行由触发器拒绝；即使用 `TRUNCATE` 之类绕过去，也破坏了「每行账本恰好进一份
  对账单」的链（下一期的期初取的是上一期的期末与高水位）
- **不要为了让自检通过去改或删账本、事件或对账单。**账本与已处理的事件不可变（INV-5、INV-12）；改了只会把一个可定位的缺陷
  变成查不清的缺陷
- **不要为了「重试」反复手动补生成一个自检失败的租户**：结果相同，只多一串 error 日志
- **不要把日志里没有的东西（金额、公司名）补进工单或告警**：日志只带租户 public_id、账期、异常类型名与检查编号

### PDF 下载

客户在门户对账单详情、管理员在管理端对账单详情（只有 `FINALIZED` 的显示按钮）点「Download PDF」，得到
`<statement_number>.pdf`；两端下载的是同一份 PDF。**下载是即时从快照渲染的，不存文件**：没有卷、没有缓存
（`Cache-Control: no-store`），每次下载都从 `monthly_statements` 行里的快照、对账单号与定稿时刻重新渲染，不读账本、
不按今天的公司名、项目名或税务政策重算。所以没有「文件丢了要补」的情形，也没有要清理的文件。

⚠️ **已知限制**（设计 §10）：

- **开具方名称是代码常量**（`Acuven Technology Sdn Bhd`），不在快照里：改这个常量，历史对账单重新下载时会印新名称。
  **换字体文件**（`app/data/NotoSansSC-Regular.ttf`）会改变历史 PDF 的字形。二者要改都须**另过设计闸门**；金额与客户
  数据不受影响
- **可重现限于同一代码与依赖版本**：同一份对账单在同一镜像里任何时候、任何进程下载，字节完全相同；升级 fpdf2 会改变
  字节（例如 PDF 里的 Producer 版本串）而**不改变内容**，版面模板改动也会改变历史 PDF 的外观（不改数字）。客户拿着旧
  文件比对字节不同时，按这一条解释
- 快照不合规的对账单**永久**下载不了（行不可改），见下表第一行；正常生成不会产生这种行

#### PDF 下载失败

客户与管理员看到的都是 500 `INTERNAL_ERROR`（固定文案 `An internal error occurred.`）。⚠️ **三种都没有告警维度**
（设计 §8：Healthchecks 已满，不加），**只在 ERROR 日志**；通常是客户或管理员反馈「下载失败」后，按请求时刻与
`request_id` 去 api 的日志里找。日志只带对账单 `statement_id`（`public_id`）与（第三种）异常类型名 `error_type`，
不带公司名、项目名、金额、快照内容与异常原文。

| ERROR 日志 | 含义 | 处置 |
| --- | --- | --- |
| `Statement snapshot invalid; download refused` | 快照不合规（多键、少键、版本不对、字段形状不对等数据缺陷，正常生成不会产生）。**这一份对账单永久下载不了**（定稿行不可改），重试不会好 | 按日志的 `statement_id` 只读查 `monthly_statements` 那一行，核对快照哪一处不合规（对照设计 §2「校验」），记下成因（代码缺陷，或绕过服务层直接写库）；**不改、不删行**；修复要**另过设计闸门**做作废与更正版本。在那之前客户仍可在门户看这份对账单的 JSON 详情 |
| `Statement font unavailable; download refused` | 镜像里的中文字体读不到或损坏（打包缺陷）。**只影响含非 Latin-1 字符**（如中文公司名、项目名）的对账单，全 Latin-1 的照常下载 | 查镜像里的 `app/data/NotoSansSC-Regular.ttf` 是否在、能否读，查构建期的 packaging smoke（`scripts/packaging_smoke.py`，Docker 构建与 CI 都跑）为什么没拦住；**重新部署带字体的镜像**，之后重试即可 |
| `Statement PDF rendering failed` | 渲染时其他异常（例如 fpdf2 内部错误） | 看 `error_type`；按 `statement_id` 取出该行的快照、对账单号与定稿时刻，**在本地用同一快照**（同一代码与依赖版本）调 `render_statement_pdf` 复现，按原因修代码 |

#### 首个账期生成后的人工核对

⚠️ 生产首个账期 `2026-12` 在 2027-01-02 01:00 才生成，在那之前生产没有任何对账单：**那之前的部署验收只证明页面与
接口可达**（浏览器验收的对账单 PDF 步骤只能走「列表为空即通过」），没有真正下载过一份（设计 §10 假设 6）。

**`2026-12` 的对账单生成之后**，管理员在管理端 `/statements` 打开一份对账单详情，点「Download PDF」下载一份，核对：

1. PDF 的字段与金额和页面上的详情一致（对账单号、账期、期初、充值、本期用量、调整、上期调整、期末、请求数、会话数、
   token 数、按供应商 / 模型 / 项目的明细与合计；金额 2 位小数）
2. 公司名或项目名含中文时显示正确（不是 `?` 或缺字框）；有含中文的客户就挑它的那一份
3. 不含成本、毛利、加价、汇率、税额栏或 SST 注册号

不一致时按上表与设计 §10 处置并记下对账单 `public_id`，不要把金额与公司名写进工单。

---

## 上期用量与跨期调整

**首次编写：2026-11-22（AIH-TASK-230）。依据：[ADR-0003](adr/ADR-0003-financial-period-and-cutoff.md)「决策」规则 2–5 与「需要监控的风险」；
`app/services/statements.py:73-82`、`app/services/statements.py:209-230`、`app/services/statements.py:531-535`；`app/models/wallet.py:65-66`；
本文件「月度对账单」；[api.md「快照」](api.md#快照)的 `prior_period`；[api.md「管理端手工调账」](api.md#管理端手工调账)；
[TODO.md](TODO.md) Phase 8 的「Rebill 调整」。**

**规则（以代码为准）**：一行用量账本（`AI_USAGE`）的用量月 `U` 是事件 `occurred_at` 所在的马来西亚日历月。入账时刻早于 `cutoff(U)`
（`U` 次月第 2 日 00:00，马来西亚时间）的计入 `U`；晚于它的计入**入账那个月**的对账单，放在「上期调整」（`prior_period.usage`，
按原用量月 `usage_period` 列出条数与金额）。已定稿的对账单不变。没有单独的账本类型：上期调整是对账单按这条规则对 `AI_USAGE`
行的归类；扣费本身在入账那一刻就发生，余额与停机判定不等对账单。宽限日（次月第 1 日）内入账的上月用量仍计入原月，不算晚到。

**跨期 Rebill**：由 AIH-TASK-253..260 与 262（设计闸门 #830）交付，流程与故障处置见 [Rebill](#rebill)。`REBILL_CREDIT` /
`REBILL_DEBIT` 行按确认时刻（`created_at`）的马来西亚月计入当月对账单的 `prior_period.rebill`；原账期已定稿的对账单不变。

### 怎么发现

- `usage_processing_backlog` 的「晚到用量」一段报 P2：`late usage 24h <a>/<b> rows (<x>%), <y>% of amount`，最近 24 小时入账的预付
  用量里，过了原用量月 cut-off 才入账的条数或金额占比 ≥ 1%（见「用量计费」一节「怎么发现」）
- 对账单详情或 PDF 的「上期调整」（`Prior-period adjustments`）有行
- 客户问「为什么上个月的用量出现在这个月的对账单上」

### 影响什么

| 仍然正常 | 受影响 |
| --- | --- |
| 金额：晚到的用量照扣一次，不丢、不重复 | 客户的这一期对账单多出「上期调整」，要能解释 |
| 已定稿的往期对账单：一个字节都不变（INV-12） | 晚到占比高说明投递链路有系统性延迟：每一笔都应能追溯到一次具体的延迟（ADR-0003） |
| | 已计费金额本身错了的事件：价格、规则发布错的走 [Rebill](#rebill)；汇率错的不在 Rebill 范围内，需要时用手工调账 |

### 立刻做什么

1. 晚到占比告警：按「用量计费」一节「晚到用量占比高」的处置查根因（beat / worker、`ai_usage_outbox` 与 `outbox_backlog` 维度、
   客户侧投递）；停机或积压跨过 cut-off 的，先按对应章节恢复
2. 核对某客户某一期：管理端对账单详情（`GET /api/v1/admin/statements/{statement_id}`）的 `prior_period`
3. 向客户解释：对账单 PDF 上这一节开头有固定说明（原月 cut-off 之后才处理的往期用量，按原用量月列在这里）
4. 已计费的金额本身错了：按 [Rebill](#rebill) 一节「先选对手段」处理（计价错误 → Rebill；汇率错误与计价无关的补偿 → 手工调账）

### 恢复

- 晚到用量一段在延迟消除后随 24 小时窗口自动回落
- 上期调整本身不需要恢复：它就是正确的入账结果

### 绝不能做什么

- **不要重算或改写已定稿的对账单**来把晚到用量「并回」原月：ADR-0003 备选方案 C 已否决（INV-12），定稿行由触发器拒绝修改
- **不要改事件的 `occurred_at` 或账本行**来移动用量所属的期间：已处理的事件与账本不可变（INV-5、INV-12）
- **不要用手工调账代替 Rebill**：手工调账只收 `ADJUSTMENT_CREDIT`、`ADJUSTMENT_DEBIT`、`BONUS`、`REFUND_ADJUSTMENT`，rebill 类型 422；
  调账计入本期的「调整」而不是「上期调整」，也不引用原事件 —— 客户看到的是一笔与原用量无关的调整。手工调账只用于本文件其他章节
  写明的场景（争议与退款的扣回、关户前的余额清零、负余额、AI 用量待复核的退回）
- **不要为了消晚到告警删事件或账本行**

---

## Rebill

**首次编写：2026-11-25（AIH-TASK-259）。依据：设计闸门 #830 v12（[AIH-TASK-253-usage-rebill.md](design/AIH-TASK-253-usage-rebill.md)）
§1「Kelvin 2026-11-23 的决定」「明确不做什么」、§2「选事件」「预览指纹」「服务」、§4「跃迁表」、§5、§8；接口契约见
[api.md「管理端 Rebill」](api.md#管理端-rebill)；迁移见 `alembic/versions/20261124_0042_usage_rebills.py` 文件头「失败处理」「回滚」。**

已计费（`PROCESSED`）的预付事件发现算错了钱时，管理员对**一个客户**的一组事件按更正版本重算：先预览（旧计费、新计费、差额），
再确认（必填原因、带幂等键）。确认在客户钱包写**恰好一行** `REBILL_CREDIT`（多扣了，退回）或 `REBILL_DEBIT`（少扣了，补扣），
逐事件的明细写进 `usage_rebills` / `usage_rebill_lines` 两张只追加的表。事件行、原 `AI_USAGE` 行与已定稿的对账单一个字节都不改。
入口：管理端侧栏「Rebills」（`/rebills` 列表、`/rebills/new` 新建、`/rebills/:id` 详情）；用量事件详情页对已计费的预付事件有
「Rebill」按钮，跳到新建页并预填客户与该事件。只有 ADMIN 能用，客户门户没有任何 Rebill 入口。

### 先选对手段

| 情形 | 用什么 | 说明 |
| --- | --- | --- |
| **未计费**的错误事件（`MODEL_UNKNOWN` / `PRICING_ERROR` / `FX_RATE_ERROR` / `FAILED_FINAL`，账本里没有它的行） | **重新入队**（见「用量计费」一节「立刻做什么」） | Rebill 只选 `PROCESSED` 事件，不处理未计费事件（设计 §1「明确不做什么」）。修好配置后重新入队，由计费照常算 |
| **已计费**的预付事件，**计价**错了：价格版本、定价规则录错，或计价代码有 bug 已修好 | **Rebill** | 指定更正规则、更正价格版本（只影响 MARKUP 规则），或都不指定（按当前有效版本重算，修了 bug 之后用）。不许手填金额 |
| 与计价无关的补偿或退款（争议、商务让利、关户前清零等），以及已计费事件的**汇率**错误 | **手工调账**（[api.md「管理端手工调账」](api.md#管理端手工调账)） | 汇率不在 Rebill 范围内：已计费事件的汇率沿用原快照（设计 §1「明确不做什么」） |

Rebill 也不做：内部计量租户（409 `REBILL_NOT_PREPAID`）、跨客户批量（按客户逐个做）、定时或自动 Rebill、撤销（撤销 = 再做一次
Rebill，把版本改回去）。

### 上线后第一次使用前

AIH-TASK-256 起确认接口即上线（不加开关，设计 §1「我定的」第 8 条）。**在对任何真实客户确认 Rebill 之前**，用测试专用验收夹具
（`docs/TODO.md`「测试专用验收夹具」的验收管理员与验收租户）
走一遍。先看验收租户有没有**计费额大于 0 的已计费预付事件**：管理端用量事件列表按该客户与 `status=PROCESSED` 筛
（`GET /api/v1/admin/usage-events?customer_id=<验收租户>&status=PROCESSED`），找 `billing_mode_snapshot` 为 `PREPAID`、
`billable_cost` 大于 0 的行。

**有这样的事件**：只选**其中一个**，用显式事件（`{"usage_event_ids": ["<该事件 public_id>"]}`）做一次小额补退、再补扣还原。

1. **记下基线**：该事件的详情（`GET /api/v1/admin/usage-events/{usage_event_id}`）的 `billable_cost`、`effective_charge`、
   `pricing_rule_id`（原规则）与 `occurred_at`；验收租户的钱包余额与流水
   （`GET /api/v1/admin/customers/{customer_id}/wallet/transactions`）；用量分析按该客户、覆盖 `occurred_at` 的区间
   （`GET /api/v1/admin/usage-analytics?group_by=customer&customer_id=<验收租户>&occurred_from=…&occurred_to=…`）的
   `prepaid.billable_cost`。
2. **准备补退用的更正规则**（它从不参与真实计费）：查原规则（`GET /api/v1/admin/pricing-rules/{rule_id}`）的 `strategy`，然后为
   验收租户新建一条 `CUSTOMER` 范围的规则草稿（`POST /api/v1/admin/pricing-rules`，`priority_scope` 为 `CUSTOMER`、`customer_id`
   为验收租户）：原规则是 MARKUP 就用**更低的倍数**；是 FIXED_RATE 就把**每个分量价调低**（分量要齐全，否则发布 409
   `PRICING_RULE_INCOMPLETE`）。发布时给 `effective_from`，设在**至少一天之后**（带时区、整秒；409 `EFFECTIVE_FROM_CONFLICT`
   时换更晚的时刻）；发布后**立即停用**（`POST …/{rule_id}/retire`，`reason` 写明是上线核对）。读回核对：`status` 为 `RETIRED`、
   `effective_from` 等于 `effective_to`（空区间，永不命中）。更正版本只看「已审批发布过」、不看生效区间，所以它可用。
3. **补退预览**：`/rebills/new` 选验收租户、「事件列表」填这个事件、更正规则选第 2 步的规则、价格版本不选 →「预览」。
   **`ledger_type` 必须不为 `null`**（应是 `REBILL_CREDIT`）才确认。降价后差额被舍入成 0（`ledger_type` 为 `null`、确认按钮禁用）时：
   换一个计费额更大的事件，或照第 2 步再建一条价格更低的规则；**都不行就不做真实确认**，改走下面「没有这样的事件」的路径。
4. **补退确认**：填原因（例如 `Go-live check on the acceptance fixture: credit`）→「确认」。核对：
   - 账本：流水里多**一行** `REBILL_CREDIT`，金额等于预览的 `ledger_amount`，`description` 为 `Rebill correction (1 usage events)`；
     原 `AI_USAGE` 行不变
   - 余额：增加 `ledger_amount`
   - 审计：`GET /api/v1/admin/audit-logs?action=REBILL&entity_type=usage_rebill&entity_id=<Rebill public_id>` 恰好一条，原因一致，
     没有估算成本
   - 用量分析：同一查询的 `prepaid.billable_cost` 减少了同样的金额，`estimated_provider_cost_myr` 不变
   - 事件详情：`billable_cost` 与快照字段不变，`effective_charge` 等于预览的 `corrected_charge`，`rebills[]` 一项
5. **补扣还原**：再开 `/rebills/new`，同一事件，更正规则选**事件原来的规则**（第 1 步记下的 `pricing_rule_id`，也是第一次预览明细的
   `previous_pricing_rule`）→「预览」：应为 `REBILL_DEBIT`、`ledger_amount` 是第 4 步金额的相反数、`corrected_charge` 等于原
   `billable_cost`；`ledger_type` 不为 `null` 才确认。确认后核对：余额、用量分析的 `prepaid.billable_cost` 与事件的
   `effective_charge` 都回到第 1 步的基线；流水有两行 `REBILL_*`，相加为 0；审计 `REBILL` 两条；事件 `rebills[]` 两项。
6. **副作用**（对测试租户可以接受）：若验收租户正在停机（余额 ≤ 0），补退可能让它复机、补扣又让它停机，各产生一次状态推送；
   两行 `REBILL_*` 进确认那个马来西亚月对账单的 `prior_period.rebill`（同一月里相加为 0，所以不要跨月末做这两步）。

**没有这样的事件**（例如验收租户从未被计费）：**不做真实确认**。第一次对真实客户做 Rebill 时**只先预览**，把预览（明细、合计、
`ledger_type` / `ledger_amount`、`balance` / `balance_after`）交给 Kelvin 看过，**由 Kelvin 确认后**再点「确认」；确认后照上面第 4 步的
清单核对账本、余额、审计与用量分析。这一步不是可选的。

### 怎么发现

| 信号 | 说明 |
| --- | --- |
| 客户或财务对某段时间的扣费提出异议，查到用错了价格版本或规则 | 管理端用量事件详情的 `provider_price_version_id` / `pricing_rule_id` 是计费所用的版本 |
| 发现已发布的价格版本或规则录错（单价、倍数、分量） | 发布不许回溯（见「用量计费」一节「绝不能做什么」），已按它计费的事件只能用 Rebill 纠正 |
| 计价代码的 bug 修好之后 | 已按旧代码计费的事件用「按当前有效版本重算」 |
| 确认返回 409 / 422 | 见下「立刻做什么」的错误表 |

### 影响什么

| 仍然正常 | 受影响 |
| --- | --- |
| 事件行、原 `AI_USAGE` 行、已定稿的对账单一列不改（INV-5、INV-12）；门户用量页仍显示处理时的计费额 | 客户钱包多一行 `REBILL_*`，余额随之变；客户在钱包流水里看得到（类型、金额、余额、`description`、时间），不发邮件 |
| 估算成本与对账后成本仍以事件原快照为准 | 用量分析的收入与毛利按原事件的维度与 `occurred_at` 计入差额 |
| AI 服务、摄取与计费不受影响（Rebill 只在管理端） | Rebill 行按确认时刻（`created_at`）的马来西亚月计入当月对账单的 `prior_period.rebill`；原账期的对账单只被明细引用 |
| | **停复机**：补扣让余额 ≤ 0（正好为 0 也算）时客户照常停机（`SUSPENDED`）；补退让停机客户的余额 > 0 时照常复机（`ACTIVE`），与账本行同一事务，各产生状态推送（见「状态 webhook」一节）。预览页在 `balance_after` 跨过 0 时有醒目提示 |

### 立刻做什么

1. **定范围**：一个客户；用「事件列表」（1–1000 个事件 `public_id`）或「日期范围」（按 `occurred_at` 取 `[from, to)`，项目、供应商、
   模型可选，匹配超过 1000 条 422 `REBILL_SELECTION_TOO_LARGE`，按更短的区间分几次做）。**`occurred_to` 取已过 T+1 cut-off 的时刻**
   （原用量月次月第 2 日 00:00 马来西亚时间之后），避免与仍在计费的用量交错（只是建议，不强制）。
2. **定更正版本**：更正规则与价格版本都必须**先经草稿 → 发布**（`PUBLISHED` 或 `RETIRED`；`DRAFT`、`DISCARDED` 不可用），
   **不看生效区间**。价格版本须同供应商、同模型、同来源币种、分量齐全；规则的客户 / 项目 / 供应商 / 模型各自为空或等于事件的；
   任一事件不适用整个请求 422，消息带第一个不适用事件的 `public_id`。要纠正的是「过去用错了价」：先把正确的版本建草稿、审批发布
   （从发布时刻起它也正常参与以后的计费），再拿它做更正。
3. **预览**，核对明细、`unchanged_count`、合计、`ledger_type` / `ledger_amount` 与 `balance_after`；`ledger_type` 为 `null`
   （没有可入账的差额）时不能确认。补扣让余额跨过 0 会停机，补退会复机，确认前想清楚。
4. **确认**：填原因（1–255 字符）。**一次 Rebill 的对象 = 预览展示的那组事件**：预览之后才计费的事件不在本次范围，由下一次
   预览发现、再做一次 Rebill；本次只改明细里列出的事件。
5. **提交结果不明**（500、超时、连接中断）：结果只有「已完整提交」或「已完整回滚」两种，不会有一半。**用同一幂等键重发**
   （页面重试沿用同一个键）：已提交 → 200 `replayed: true` 返回原结果；已回滚 → 照常执行。**要换新幂等键，必须先重新预览**：
   新预览已反映第一次是否提交，换键直接确认可能把同一更正再做一次。先到 `/rebills` 列表或该客户的钱包流水确认有没有这一行也行。

| 错误 | 含义 | 下一步 |
| --- | --- | --- |
| 409 `REBILL_PREVIEW_STALE` | 预览之后有别的 Rebill 改了同一事件、匹配集合变了（新事件被计费进来），或请求参数与预览时不同 | 重新预览，核对后再确认 |
| 409 `REBILL_CONFLICT` | 同一幂等键已用于不同的请求 | 重新预览，用新的幂等键确认 |
| 409 `NOTHING_TO_REBILL` | 条件匹配为 0；或确认时没有差额不为 0 的事件 | 检查范围与更正版本 |
| 409 `REBILL_NET_ZERO` | 补退与补扣相抵为 0 | 把补退与补扣的事件分两次做 |
| 409 `USAGE_EVENT_NOT_REBILLABLE` / `REBILL_NOT_PREPAID` | 事件不属于该客户、未计费或不是预付；客户是内部计量 | 未计费的走重新入队；内部计量不做 Rebill |
| 409 `ACCOUNT_CLOSED` | 客户已关户，什么都没写 | 不重试；按「关户」一节处理 |
| 422 `REBILL_PRICE_NOT_APPLICABLE` / `REBILL_RULE_NOT_APPLICABLE` | 更正版本不适用于消息里那个事件 | 换适用的版本，或把那个事件排除出范围 |
| 422 `AMOUNT_OUT_OF_RANGE` | 结果超出 `DECIMAL(20,8)` | 多半是更正版本录错，核对单价或倍数 |
| 500 | 锁等待超时、死锁或提交失败；不自动重试 | 按上面第 5 步同键重发 |

### 恢复

- **更正做错了**：没有撤销接口，也不改已有行；再做一次 Rebill，把版本改回去（同一事件以当前有效计费 `effective_charge` 为基准）。
- **核对**：`/rebills/:id` 详情（或 `GET /api/v1/admin/rebills/{rebill_id}`）的明细、两个合计与 `wallet_transaction_id`；用量事件详情的
  `effective_charge` 恒等于 `billable_cost` 加 `rebills[]` 的 `difference` 之和；钱包与账本一致性用 `verify_wallet`（只报告不修复）。
- **迁移 0042 中断**：`upgrade` 中途失败（锁等待超时、进程被杀）时原样重新部署即可，判定与续跑见迁移文件头「失败处理」
  （`alembic/versions/20261124_0042_usage_rebills.py:88-106`）；报错点名「形状不对」的表或触发器时停下，照文件头那两条由人对照设计处置。
- **回滚**：生产只回滚应用镜像（`deploy/deploy.sh` 的回滚只切换镜像、不降级迁移）。**有了真实 Rebill 之后 0042 不可降级**：
  两张表是账本里 `REBILL_*` 行的审计链（spec §19），只回滚代码（新表对旧代码无害：旧代码不读它们）。先只读确认有没有行：

  ```bash
  docker compose exec mysql sh -c 'mysql -u"$MYSQL_USER" -p"$MYSQL_PASSWORD" "$MYSQL_DATABASE" -e "
    SELECT (SELECT COUNT(*) FROM usage_rebills) AS rebills,
           (SELECT COUNT(*) FROM usage_rebill_lines) AS rebill_lines,
           (SELECT version_num FROM alembic_version) AS revision"'
  ```

- **降级 0042**（只在两张表都为空时有意义）：不是部署流水线的一部分，只由人手工执行。**先停 `api` 容器**（Rebill 唯一的写入方是
  管理端确认接口），确认没有进行中的请求，再执行 `alembic downgrade 0041_provider_cost_recon`。这是操作前提，不由数据库强制。
  任一张表有行时降级抛 `RuntimeError`（`… is not empty: downgrading would delete rebill records (spec §19); restore from backup or
  keep this revision`），不发 DDL、不删行，版本仍是 0042 —— 这时就保留这个修订，只回滚代码。`--sql` 离线模式直接抛错（须在线执行）。
  降级在 MySQL 上用一条语句同时删两张表（原子 DDL）；中途被打断时，仍先停 `api`，原样重跑同一条命令（表已不在时不发 DDL、只把版本退回）。
- **不得把应用回滚到 AIH-TASK-258 之前的版本**（设计 v10 §8）：那之前的用量分析不计入 Rebill 差额，收入与毛利会漏计已入账的差额。
  确需回滚到那么早时，先停用管理端，并由人按 `usage_rebill_lines` 手工核对报表（只读查询，例如按 `usage_event_id` 汇总 `difference`）。

### 绝不能做什么

- **不要手工 `UPDATE` / `DELETE` `usage_rebills` 与 `usage_rebill_lines`，也不要为了降级 `TRUNCATE` 或清表。**MySQL 触发器拒绝改与删；
  绕过去，账本里的 `REBILL_*` 行（不可改、不可删）就无法解释（spec §19、INV-5）
- **不要直接写库插入 Rebill 行或 `REBILL_*` 账本行**：唯一的写入方是确认接口（锁钱包、幂等、指纹、审计、停复机在同一事务）
- **不要换新幂等键直接确认一个结果不明的提交**：先同键重发，或先重新预览
- **不要用未经发布的版本、也不要为了让 Rebill 能用而回溯发布或直接写库改生效时刻**：更正版本只要「已审批发布过」，不需要在过去生效
- **不要用 Rebill 纠正汇率或未计费事件**，也**不要用手工调账代替计价错误的 Rebill**（手工调账不记录原事件、原金额与所用规则）
- **不要在 `api` 容器还在跑时降级 0042**，也不要在有 Rebill 之后把应用回滚到 AIH-TASK-258 之前（见「恢复」）

---

## 供应商成本对账

**首次编写：2026-11-21（AIH-TASK-251）。依据：设计闸门 #755 v1
（[docs/design/AIH-TASK-242-provider-cost-reconciliation.md](design/AIH-TASK-242-provider-cost-reconciliation.md)）§1、§2「口径」
「告警」、§5、§6「密钥」「公开仓库与保留」、§8「上线后的人工步骤」「回滚或前滚」、§10。对账后成本怎样分摊进毛利分析见设计闸门
#756 v2（[AIH-TASK-247-provider-cost-allocation.md](design/AIH-TASK-247-provider-cost-allocation.md)）。**接口契约见
[api.md「管理端供应商成本对账」](api.md#管理端供应商成本对账)。

这一节不是故障场景，是运维流程（一次性的换钥 + 每月一次的录入），所以不套五节，写 **是什么 · 换钥（只做一次）· 首个对账月 ·
每月怎么做 · 诊断计数怎么读 · 期望结果与失败处理 · 更正 · 回滚 · 绝不能做什么**。

⚠️ 下面的 `<部署目录>`、`<端口>`、`<工作区名称>` 都是占位符：主机名、地址、目录、Console 工作区的真实名称、密钥值与实际金额都不进
仓库（仓库是公开的，设计 §6）。

### 是什么

管理员每月把供应商 Console 成本页上**本平台专用 workspace** 上一个 UTC 自然月的消耗额（**不含税**、来源币种原值）录进管理端
「Provider costs」页（`/provider-costs`，只对 ADMIN），和按事件快照算出的估算成本比较：

- **期间**：UTC 自然月 `[YYYY-MM-01 00:00 UTC, 下月 1 日 00:00 UTC)`，与 Console 成本页的日边界一致；**不是**对账单的马来西亚月，
  两者不互相核对
- **范围**：只含经 AI 模块发出的调用（事件的 `event_id` 在 `ai_usage_outbox` 里）；集成方用自己的供应商密钥上报的用量不在范围内
- **粒度**：供应商 × 月一个合计数，不分模型、不分项目
- **不改任何已计费金额**：不动用量事件（含估算快照）、钱包、账本、对账单；客户永远看不到（INV-7）
- 差额率（来源币种）≥ 5% 只在页面高亮，**不告警**；只有「上月满 9 天仍未对账」并进 `usage_processing_backlog` 报 72 小时 P2
  （见「用量计费」一节「怎么发现」表里 `usage_processing_backlog` 那一行的第六段）

### 换钥（只做一次）

AI 模块现在用的 Anthropic 密钥是多个项目共用的，供应商侧分不出本平台那一份花费；换成本平台专用的密钥之后才能对账。
由 Kelvin 或经 Kelvin 授权的会话执行。

1. **在 Console 建专用 workspace 与密钥**：Kelvin 在 Console 里为本平台建一个专用 workspace，在其中生成一把新的 API 密钥。
   只要普通 API 密钥：**不生成、不保存供应商 Admin 密钥**（本平台不接供应商的成本 API）
2. **替换宿主机上 AI 模块读取的 secret 文件，重建 api 容器**：就是「AI 就绪检查失败」一节第 1 步那个 key 文件（属主 `10001`、
   权限 `0400`，ADR-0004）。只把新密钥写进文件，**不打印内容**，用 `ls -l` 与 `sha256sum` 前 8 位确认文件已换、权限没变；secret
   走 bind mount，换完重建 api 容器：
   ```bash
   cd <部署目录>
   docker compose up -d --force-recreate api
   curl -sS -w '\n%{http_code}\n' http://127.0.0.1:<端口>/healthz/ai
   ```
   期望 200 且 `data.status` 为 `ok`。503 `AI_PROVIDER_NOT_CONFIGURED` 按「AI 就绪检查失败」一节第 1 步排查；确认 AI 调用正常后，
   旧的共用密钥是否吊销由 Kelvin 决定（别的项目可能还在用）
3. **记下切换时刻**（UTC，精确到分钟即可）：首个对账月由它决定，见下一小节。记在运维记录里，与密钥文件分开

### 首个对账月

**首个对账月 = 切换时刻之后的第一个完整 UTC 月**。代码常量 `FIRST_RECONCILIATION_MONTH`（`app/services/provider_cost_reconciliation.py`）
现在是 `2026-12`，前提是 **2026-12-01 00:00 UTC（马来西亚 08:00）之前**换好（Kelvin 2026-11-16 定）。早于这个常量的月份页面上不列、
不能录入，告警也不报。

- 切换时刻早于 2026-12-01 00:00 UTC：常量不用改
- 首个对账月与常量不同时，**在写入任何对账记录之前**，用一个只改这个常量的小 PR 把它改对（不走设计闸门；相关测试都 monkeypatch
  了这个常量，只改常量不会让 CI 挂）
- **换钥晚于 2026-12-01 时**：每月 **10 日之前**（AIH-TASK-250 的告警窗口从上月月末 + 9 天、即 10 日 00:00 UTC 开始）开一个只改
  `FIRST_RECONCILIATION_MONTH` 的 PR，把它推到**当前月的下一个月**；到下月 10 日前仍未换钥，就再推一个月；如此直到它等于换钥后的
  第一个完整 UTC 月。例：到 2026-12-09 仍未换钥 → 改为 `2027-01`；到 2027-01-09 仍未换钥 → 改为 `2027-02`；2027-01-20 换好 →
  首个完整月就是 `2027-02`，常量已经对了，不再推。不推的话，告警会为一个还在用共用密钥的月份报「未对账」
- 常量还没改对时，页面会把首个对账月之前的月份列为 `Not reconciled`：**不要录这些月份**（那时的花费分不出本平台那一份，设计 §10
  已知限制 2）

### 每月怎么做

1. **什么时候**：每月初，上一个 UTC 月结束、Console 成本页上月的数字已经稳定之后（月结束后几天）；最晚在第 10 日 00:00 UTC 之前录完，
   否则告警从那时起报 72 小时 P2
2. **在 Console 取数**：打开成本页，选本平台专用 workspace、上一个月（按 UTC 划日），取当月的**消耗额，不含税**（外国数字服务税等
   一概不录），记下币种（Anthropic 是 USD）。不录信用卡账单的实付额：预付额度的购买时点与消耗月份对不上
3. **在管理端录入**：「Provider costs」页选供应商 `anthropic`，在上月那一行点「Reconcile」，填：
   - 币种：Console 上的币种（`USD`）
   - 实际金额：Console 上的数字原样填，至多 8 位小数，**不自己换算成 MYR**
   - 来源说明：例如 `Console cost, <工作区名称>, YYYY-MM`（工作区 + 月份 + 页面或账单号）；不写合同价
4. **先预览**：「Preview」不写库，显示估算事件数、估算（来源币种与 MYR）、换算后的实际 MYR、所用汇率与取值时刻、两种差额与差额率、
   三个诊断计数。按下一小节读诊断计数；能先处理的（未处理、未投递）先处理，再重新预览
5. **再确认**：「Confirm」只在本次预览成功、且预览后表单没改动时可点。成功后提示 `The reconciliation was confirmed.`，那一行变为
   `Reconciled`，写入一行不可变记录与一条审计 `PROVIDER_COST_RECONCILIATION_CREATE`（审计里没有来源说明）
6. **差额超过 5%**（`Above 5%`）：先用诊断计数解释；解释不了时查该月的供应商价格版本是否漏录调价、折扣。修价格只影响以后的事件，
   **不改已处理事件**（对账不触发 Rebill；客户计费额确有错时另按 [Rebill](#rebill) 一节处理）；本月的记录照实录，不为了让差额变小改录入额

### 诊断计数怎么读

| 计数 | 含义 | 对差额的影响 | 处置 |
| --- | --- | --- | --- |
| 未处理（`unprocessed_event_count`） | 期间内（按 `occurred_at`）属于 AI 模块、状态不是 `PROCESSED` 的用量事件数，**不限供应商**（未知模型的事件可能没有供应商） | 供应商已计费、估算里还没有 → 差额偏正 | 按「用量计费」一节处理错误事件与积压，处理完重新预览 |
| 未投递（`outbox_unsettled_count`） | 期间内（按 outbox 的 `created_at`）状态为 `PENDING` / `SENDING` / `HELD` / `DEAD` 的 AI 模块 outbox 行数 | 同上，差额偏正 | 按「AI 模块用量 outbox」一节处理 |
| 已丢弃（`outbox_discarded_count`） | 同一区间状态为 `DISCARDED` 的 outbox 行数 | 永远没有估算，差额永久偏正 | 不用处理；差额里这一部分按它解释 |

- outbox 按 `created_at` 归期是近似：月界附近的调用可能差一两笔落到相邻月
- 处理不完的计数不妨碍确认：三个计数连同估算一起快照进记录
- 确认之后才处理完的事件会让估算变化：详情里显示「确认之后估算已变化」（对比实时估算与记录里的快照），记录本身不变；要让记录里的
  估算快照跟上，用下面的「更正」（同一个实际金额，理由写明重新快照）

### 期望结果与失败处理

预览与确认都可能返回下面的错误（确认多一个 409 `RECONCILIATION_ESTIMATE_CHANGED`）；错误只有错误码与信息，**任何一种都不写库**。

| 错误 | 含义 | 下一步 |
| --- | --- | --- |
| 422 `RECONCILIATION_MONTH_INVALID` | 月份没结束、早于 `FIRST_RECONCILIATION_MONTH` 或格式不对 | 月份结束后再录；早于首月的不录 |
| 422 `RECONCILIATION_REASON_INVALID` | 更正没填理由、理由全是空白或超过 255 字符 | 改理由后重试 |
| 422 `FX_RATE_UNAVAILABLE` | 录入币种不是 MYR，而期间最后一刻（下月 1 日 00:00 UTC 前 1 微秒）没有生效的已发布汇率 | 见下「月末没有汇率」 |
| 422 `SOURCE_CURRENCY_MISMATCH` | 范围内已处理事件的来源币种与录入币种不同 | 见下「币种不一致」 |
| 422 `RECONCILIATION_AMOUNT_OUT_OF_RANGE` | 换算后的 MYR 超出 DECIMAL(20,8) | 多半是金额录错（多打了位数），核对 Console 上的数字 |
| 404 `AI_PROVIDER_NOT_FOUND` | 供应商代码不在 AI 目录里 | 页面只列 `anthropic`；出现就是目录被改过，查目录 |
| 409 `RECONCILIATION_EXISTS` / `RECONCILIATION_NOT_CURRENT` | 该月已经有记录，或要更正的那一版已被别人更正 | 页面自动重新拉月份总览；需要改时对**当前**版本点「Correct」 |
| 409 `RECONCILIATION_ESTIMATE_CHANGED` | 预览之后又有事件被处理，估算变了 | 重新预览，再确认 |
| 500 `INTERNAL_ERROR` | 数据库提交失败等；整体回滚，既没有记录也没有审计 | 先按数据库 / API 故障处理，恢复后重新预览、确认 |

**月末没有汇率（`FX_RATE_UNAVAILABLE`）**：

1. 在管理端汇率页看该币种（USD）的版本时间线，找出期间最后一刻为什么没有版本
2. 按 [currency-and-fx.md](currency-and-fx.md) §4 补录：时间线为空 → **P1**；末尾版本已有尽头、月末时刻落在它之后的空档里 → **P4**
   （不给时刻发布，从那个尽头起补上空档）。对过去生效只有这两条，**不能「发布一个该时刻的汇率」**：发布不接受过去的时刻
   （422 `EFFECTIVE_FROM_IN_PAST`），已有汇率的时刻也不会被改写
3. 补上之后重新预览、确认
4. **月末时刻落在 P1、P4 都补不了的汇率空档**（例如空档之后已经有新的版本）：**本月暂不能对账，记待办**（写进相关 PR 描述的「已知
   未做 / 留给后续」，由管理员收进规划块），**汇率补录另走设计闸门**；不要为了补这一刻去退役或改写已发布的版本

**币种不一致（`SOURCE_CURRENCY_MISMATCH`）**：

- 范围内事件**都是另一种币种**：换成那个币种录入（金额仍是 Console 上该币种的原值）。表单的币种下拉只有 `USD` 与 `MYR`，换成另一个
  再预览一次即可判断
- 换了币种仍然不一致，即范围内事件**币种混杂**：**本月无法对账，需另走设计闸门**（设计 §10 假设 4：Anthropic 的价格版本以 USD
  计价，正常不会出现）

### 更正

记录一经确认**不可改、不可删**（MySQL 触发器拒绝任何 `UPDATE` / `DELETE`）。录错了、Console 上的数字后来变了、或要重新快照估算：

1. 在已对账那一行点「Correct」：表单预填当前记录的币种、金额与来源说明，**理由必填**（1–255 字符，写明为什么改）
2. 改好后同样先「Preview」再「Confirm」
3. 结果：追加一行取代行，它成为当前版本，旧行标为「已被取代」，两行都永久保留；点开这一行可看整条更正链（时间、操作人、理由、各版本
   金额）。审计 `before` 是被取代那一版的金额
4. 只能更正当前版本；两人同时更正同一版时后提交的得到 409 `RECONCILIATION_NOT_CURRENT`，重新拉总览后对新的当前版本再更正

### 回滚

- 生产只回滚应用镜像，**不降级迁移 0041**（降级只删空表，表里有行就拒绝）
- 记录写错一律用「更正」追加取代行，**不删行**

### 绝不能做什么

- **不要用 SQL 插入、改写或删除 `provider_cost_reconciliations`。**触发器会拒绝改与删；直接插入绕过了预览的估算快照、汇率版本引用与审计
- **不要 `TRUNCATE` 后重录，也不要为此降级 0041。**对账记录与审计永久保留
- **不要录含税的数、卡账单实付额或自己换算的 MYR**；录 Console 成本页上本平台专用 workspace 的不含税消耗额原值
- **不要录首个对账月之前的月份**，也不要在已经写入记录之后把 `FIRST_RECONCILIATION_MONTH` 推过那些月份（已有记录会从总览里消失）
- **不要为了消 `FX_RATE_UNAVAILABLE` 去退役或补发月末附近已在用的汇率版本**；补录只走 P1 / P4，补不了的另走设计闸门
- **不要把密钥打印出来、写进 `.env`、镜像、命令行或工单**；不保存供应商 Admin 密钥
- **不要把 Console 地址、工作区真实名称、主机名、实际金额写进工单、PR 或本仓库的文档**

---

## xlsx 导出失败

**首次编写：2026-11-20（AIH-TASK-241）。依据：设计闸门 #753 v1（[设计文件](design/AIH-TASK-234-excel-exports.md)）§2「tmpfs（AIH-TASK-234）」、
§5（含「各导出的失败日志」）、§8「监控」、§10。**接口契约见 [api.md「客户导出」](api.md#客户导出)与其中的「xlsx 格式」小节。

客户门户的四个导出（用量、钱包流水、月度汇总、会话）与管理端毛利分析导出带 `format=xlsx` 时返回 `.xlsx`。openpyxl
生成时总是先把工作表写进 `api` 容器的 `/tmp`，那里挂的是内存文件系统（tmpfs，上限 128 MiB，计入 `api` 的
`mem_limit`）；成功或失败后 `write_xlsx` 都会删掉这次的临时文件。CSV、JSON 接口与 AI 模块都不写 `/tmp`。

### 怎么发现

⚠️ **没有告警维度**（设计 §8：无新告警），**只在 ERROR 日志**；通常是客户或管理员反馈「导出 Excel 失败」（对外是 500
`INTERNAL_ERROR`，固定文案）后，按请求时刻与 `request_id` 去 api 的日志里找。各导出的失败日志不同（设计 §5「各导出的失败日志」）：

| 导出 | ERROR 日志（消息） | 怎么认 |
| --- | --- | --- |
| 用量、钱包流水、会话（占导出租约） | `customer_export_failed`（`app.api.customer`） | `extra` 的 `path` 是对应的导出路径 |
| 管理端毛利分析 | `admin_usage_analytics_export_failed`（`app.api.admin_usage_analytics`） | `path` 是 `/api/v1/admin/usage-analytics/export` |
| 月度汇总（不占租约、不包 `try`）的**任何**失败 | `Unhandled exception`（`app.core.errors` 的 `handle_unexpected_error`，通用 500 处理器） | `path` 是 `/api/v1/customer/usage-summary/export` |
| 钱包导出在拿租约**之前**发现钱包缺失 | `Unhandled exception`（同上） | `path` 是 `/api/v1/customer/wallet/transactions/export` |

四种都带栈、`extra` 只有 `path`，不带筛选值、公司名、金额与文件内容。⚠️ 查月度汇总的 xlsx 失败要找**路径为
`/api/v1/customer/usage-summary/export` 的 `Unhandled exception`**，不是 `customer_export_failed`。`QUERY_TOO_BROAD`
不是失败日志（是现有的 `customer_usage_query_timeout`），`EXPORT_BUSY`（503）是正常的串行，不在本节。

### 影响什么

| 情形 | 影响 |
| --- | --- |
| 一次生成出错（openpyxl 异常、`MemoryError`、超长文本等） | 只影响那一次 xlsx 导出；同样参数的 CSV、其他接口与 AI 模块不受影响；只读，没有业务状态要修 |
| tmpfs 写满（栈里是 `OSError: No space left on device`） | **所有** xlsx 导出都 500，直到处置完；CSV、JSON 接口与 AI 模块照常 |
| 按下面第三步重启 `api` | 影响扩大到**整个 api 进程**：计费、门户、管理端与 AI 模块一起中断（见「恢复」） |

### 立刻做什么

1. 按「怎么发现」的表找到那一行 ERROR 日志，看栈。
2. 栈里**不是** `No space left on device`：这是生成本身的错误。偶发的让客户重新导出；同样的错误对同样的参数反复出现，按栈修代码
   （临时文件已由 `write_xlsx` 删掉，不需要人清理）；急用时请客户先用 CSV（同一查询、同一列）。
3. 栈里**是** `No space left on device`：照设计 §2「tmpfs 写满时怎么判断、怎么处置」三步走。正常路径写不满 128 MiB
   （单个导出的工作表 ≤ 96 MiB、大导出由租约串行），所以「满」是异常，先查证再动手：
   1. **确认是否真的满**（只读）：

      ```bash
      docker compose exec api df -h /tmp
      docker compose exec api ls -la --time-style=full-iso /tmp
      ```

      看 `Use%`，以及是什么文件、多大、多旧。**没满就不是这个原因**，回到失败日志的栈。
   2. **不重启的处置**：残留的是 `openpyxl.` 开头、修改时间早于 10 分钟的文件时，只删它们：

      ```bash
      docker compose exec api find /tmp -maxdepth 1 -name 'openpyxl.*' -mmin +10 -delete
      docker compose exec api df -h /tmp
      ```

      10 分钟远长于任何一次生成加发送，不会删到正在写的文件；这一步不影响任何请求。删后已不满，就到「恢复」第 1 条。
   3. **第二步之后仍满**（占满的不是 openpyxl 的文件）**才重启 `api`**，做法与影响面见「恢复」第 2 条。

### 恢复

1. 请客户（或管理员自己）**重新导出**；处置期间急用的先用 CSV。
2. 需要重启 `api` 时：`docker compose restart api`。影响面（以代码为准）：
   - uvicorn 优雅排空最长 85 秒（`Dockerfile` 的 `--timeout-graceful-shutdown 85`）、容器宽限 100 秒
     （`docker-compose.yml` 的 `stop_grace_period: 100s`），**期间新请求失败**，进行中的 AI 调用在排空窗口内尽量完成；
     计费、门户、管理端与 AI 模块在同一进程，**一起中断**
   - 停机时 AI 模块内存里的暂存缓冲（`HeldBuffer`）做最后一次落库，**限时 5 秒**（`app/main.py` 的停机钩子）；落不完的条目
     只留日志 —— 按本文件「AI 模块用量 outbox」一节的「积压与从日志补录」，从消息为 `AI usage not persisted` 的日志行用
     `python -m app.cli ai-usage-held import --from-log-line '<完整 JSON 行>' --reason '<事故编号>'` 补录
   - **时机选低峰**；不紧急时（只影响 xlsx 导出，CSV 可替代）先告诉客户改用 CSV，等低峰再重启
   - 重启后 tmpfs 清空，再 `docker compose exec api df -h /tmp` 复核，然后请客户重新导出

**客户问「Excel 里金额求和为 0」**：这不是故障。金额（以及 `quantity`、比率）是**文本单元格**，字符与 CSV、JSON 接口逐字相同，
为的是不让 Excel 的双精度丢掉 8 位小数的精度，所以 Excel 标「以文本形式存储的数字」、直接 `SUM` 得 0。答复客户：求和写法见
[api.md「客户导出」](api.md#客户导出)的「xlsx 格式」小节（例如 `=SUMPRODUCT(VALUE(range))`）；月度汇总本来就有 `TOTAL` 行。
token、序号、计数等整数列是数字单元格，可以直接求和。

### 绝不能做什么

- **不要删 `/tmp` 下不明来源或 10 分钟内的文件**：可能是另一个导出正在写的文件（月度汇总不占租约，可以与大导出并发），删了会让它失败
- **不要用 `docker compose down` 代替 `restart`**：会删网络与容器，扩大影响
- **不要为腾空间去改 tmpfs 的 `size`**：上限是设计定的（计入 `mem_limit`），要改须回设计闸门
- **不要去掉 `api` 的 tmpfs，或把临时目录改到宿主机磁盘**：客户计费明细会明文落盘（spec §112 要求导出安全删除）
- **不过 AIH-TASK-235 的用例就不要升级 openpyxl**：依赖上界写成 `<3.2`，是因为 `write_xlsx` 失败时的临时文件清理依赖
  openpyxl 3.1 的私有属性；属性名变了，清理会失效，残留文件才会真的把 tmpfs 写满

---

## 客户门户账号

**首次编写：2026-10-23（AIH-TASK-094）。依据：设计闸门 #368 v12 §2「API」管理端、「管理员重置客户 2FA」、
「邀请邮件与重置的隔离」，§5、§8「部分部署时的行为」。**接口契约见 [api.md「管理端门户账号」](api.md#管理端门户账号)。

这一节不是故障场景，是日常运维步骤，所以不套五节，写 **什么时候可以开通 · 开通与重发 · 邀请没收到怎么办 ·
重置客户 2FA · 验收夹具客户门户账号 · 绝不能做什么**。

⚠️ 下面的 `<部署目录>`、`<customer_id>` 都是占位符：真实主机名、目录、邮箱与账号标识不进仓库。

### 什么时候可以开通

⚠️ **门户前端（AIH-TASK-095 到 097）全部合并部署之前，不为真实客户开通门户账号**（设计 §8）。

本任务上线后，客户已经能接受邀请、能登录，但前端还没有按角色分流：客户登录后落在现有的管理端外壳里，所有
管理端接口对他返回 403，页面只显示错误态。不泄漏数据，但不可用，客户只会以为系统坏了。这段窗口里只允许给
下面「验收夹具客户门户账号」一节的夹具租户开通。095、096、097 都合并并自动部署之后，才对真实客户开放。

### 开通与重发

- **开通**：`POST /api/v1/admin/customers/<customer_id>/portal-account`，请求体 `{"email": "<登录邮箱>"}`。
  登录邮箱以客户书面确认的为准，可以与客户资料里的联系邮箱不同；一个客户只能有一个门户账号。
  成功后账号是 `INVITED`，邀请信立即投递，链接有效期默认 72 小时（`BILLING_CUSTOMER_INVITATION_TTL_SECONDS`）。
  信里写明到期时刻（马来西亚时间，附 UTC）。
- 常见的 409：`PORTAL_ACCOUNT_EXISTS`（已经开通过，先 GET 看状态）、`EMAIL_IN_USE`（这个地址已被别的账号用，
  包括管理员账号；问客户换一个地址）、`ACCOUNT_CLOSED`（客户已关户，不开通）。
- **重发**：`POST …/portal-account/resend-invitation`。只在 `INVITED` 时可用；旧链接立即失效，新链接重新计时。
  客户报告邮箱填错时，带上正确的地址重发：`{"email": "<正确的登录邮箱>"}`。`ACTIVE` 账号不能经这里改邮箱
  （V1 不支持，需要时先停用再联系处理）。
- **停用 / 启用**：`POST …/disable`、`POST …/enable`。停用立即生效（会话、邀请与重置链接全部失效）；启用不自动
  重发邀请，从未接受过邀请的账号启用后回到 `INVITED`，需要时再点重发。

### 邀请没收到怎么办

1. 先 `GET …/portal-account` 看状态：`invited_at` 是最近一次邀请的时刻；`invitation_expires_at` 为 `null`
   表示链接已失效（过期、已接受、停用过），只能重发。
2. 请客户查垃圾箱 / 垃圾邮件文件夹（Outlook.com 等邮箱常把它判进去，邮件正文也有同样提示），并核对他提供的
   地址与 GET 返回的 `email` 是否一致。不一致就带正确地址重发。
3. 仍没有：看出站投递是否卡住 —— 出站事件 `PORTAL_INVITATION_REQUESTED` 与密码重置邮件走同一条 outbox 与投递
   任务，按「Redis / Celery broker 不可用」「celery-beat 停止调度」两节排查。broker 不可用不影响接口：行已在库里，
   恢复后周期扫描会补投。进了死信（`FAILED`）就修好 SMTP 后重发一次。
4. 客户点了「忘记密码」也收不到信是正常的：`INVITED` 账号没有可用密码，忘记密码对它不发信。只能由管理员重发邀请。

### 重置客户 2FA

客户丢了手机与恢复码，或者怀疑有人抢先在他的账号上注册了验证器时使用。效果：删除该账号的 2FA 与全部恢复码，
使该账号全部会话失效；客户之后用密码登录，可以重新开启 2FA。

1. ⚠️ **先在线下核实对方确实是该客户**：用客户资料里**登记的联系电话**回拨，或当面确认。**不凭邮件或即时消息
   里的自称** —— 攻击者正是会发这种消息的人；对方在消息里给的电话号码同样不算。核实不了就不做。
2. 调 `POST …/portal-account/reset-2fa`，请求体 `{"reason": "…"}`。原因必填，1–255 个字符，永久写进审计。
   写**怎么核实的与为什么**，例如 `Lost phone; identity confirmed by a call-back to the registered number.`；
   ⚠️ **不写个人数据**（电话号码、邮箱、联系人姓名）。
3. 409 `PORTAL_2FA_NOT_ENABLED` 表示这个账号本来就没有 2FA（也没有待确认的注册），什么都没改。
4. 若密码也可能已泄露（例如怀疑被人抢先注册了验证器），同时提示客户先走「忘记密码」再登录。
5. 不发通知邮件；审计 `PORTAL_2FA_RESET` 与这次线下沟通就是记录。

### 验收夹具客户门户账号

浏览器验收（AIH-TASK-097 及之后客户侧的任务）要用一个固定的客户账号。本任务部署后由管理员做一次：

1. 用验收专用管理员给生产上的验收夹具租户（`docs/TODO.md`「测试专用验收夹具」）调开通接口。
2. 邀请令牌**从 VPS 上该租户的 outbox 行读取，不经邮件**：
   ```bash
   cd <部署目录>
   docker compose exec -T mysql sh -c 'mysql -u"$MYSQL_USER" -p"$MYSQL_PASSWORD" "$MYSQL_DATABASE" -N' <<'SQL'
   SELECT o.payload_json
   FROM domain_outbox o
   JOIN users u ON o.aggregate_type = 'users' AND o.aggregate_id = CAST(u.id AS CHAR)
   JOIN tenants t ON t.id = u.tenant_id
   WHERE t.public_id = '<customer_id>' AND o.event_type = 'PORTAL_INVITATION_REQUESTED'
   ORDER BY o.id DESC
   LIMIT 1;
   SQL
   ```
   取 JSON 里的 `token`。投递成功或进死信时 `payload_json` 会被置空：读到 `NULL` 就调一次重发，立刻再读最新一行。
   令牌只在终端里用一次，不复制到工单、聊天或本仓库。
3. 用这张令牌调 `POST /api/v1/auth/invitation/accept` 设置密码，账号变为 `ACTIVE`；再用登录接口确认能登录。
4. 该账号**不开启 2FA**（浏览器验收要能自动登录）。
5. 凭据（登录邮箱与密码）只放在 VPS 上**仓库外的一个 0600 文件**里，照验收夹具管理员的做法：不在部署用的 git
   工作副本内、不挂进任何容器、不进对话，连路径也不写进仓库。
6. 运营者把 `customer_email`、`customer_password` 两个字段用 `cmdkey` 录入 Worker 机器的 Windows 凭据管理器，并
   加进本机配置里 `acceptance.browser` 的字段清单。
7. ⚠️ **值与账号标识都不写进本仓库**（仓库是公开的），包括邮箱、用户 id 与密码。

### 绝不能做什么

- **095–097 全部上线之前不给真实客户开通**（见上）
- **不凭邮件或即时消息里的自称重置 2FA**；核实不了就不做。原因里不写个人数据
- **不用 SQL 直接改 `users`、`password_reset_tokens` 或 `two_factor_settings`**：绕过了加锁顺序、令牌作废、
  会话失效与审计。改邮箱、停用、重置 2FA 一律走上面的接口
- **不把邀请令牌、客户密码转发到即时消息或工单里**；邀请只经邮件送达（验收夹具是唯一例外，且只在 VPS 上读）
- **不给验收夹具客户账号开启 2FA**，也不拿它做任何真实业务操作

---

## 关户

**首次编写：2026-11-16（AIH-TASK-215）。依据：设计闸门 #707 v4
（[docs/design/AIH-TASK-209-account-closure.md](design/AIH-TASK-209-account-closure.md)）§1「明确不做什么」、§2「runbook」
「契约四」、§8、§10。**接口契约见 [api.md「管理端关户」](api.md#管理端关户)。

关户把客户的账户状态从 `DISABLED` 改为终态 `CLOSED`（spec §112.1）。关户接口只在锁内同时满足以下条件时才成功：账户是
`DISABLED`；钱包余额恰好为 0；没有未结的用量事件、AI 模块 outbox 行、支付与网关事件。管理端客户详情的「Close account」
弹窗（读 `GET /api/v1/admin/customers/<customer_id>/closure-check`）会列出还差什么。下面的顺序就是为了逐条满足这些条件。

### 怎么发现

关户不是告警触发的，是业务决定：

- 客户书面要求终止服务；
- 按合同终止（合同到期不续，或依合同条款由我方终止）。

开关户工单，工单号用作下面各步的原因。只是暂停服务（欠费、临时停用）不要关户，用停用（`DISABLED`）即可：停用可以恢复，关户不能。

### 影响什么

关户提交后：

- **不可重开**：`CLOSED` 是终态，没有任何接口离开它。账户状态接口对 `CLOSED` 一律 409；再次关户返回 200、什么都不写。
- **AI 调用**：有效状态为 `BLOCK_AI` / `ACCOUNT_CLOSED`，状态投递随关户重排、推给集成方。
- **各写入口 409**：手工调账，以及建项目、建集成凭据、轮换集成凭据、签发 AI 客户端密钥、签发与启用 webhook 签名密钥、开通内部计量、
  设置或清除低余额阈值，一律 409 `ACCOUNT_CLOSED`、不写任何东西。管理端这些按钮照旧显示，点了会显示这个错误。
  照常允许：吊销集成凭据与 AI 客户端密钥、退役 webhook 签名密钥、查看与编辑客户资料、清除项目状态 webhook 地址、门户账号的停用与重置、
  用量事件重新入队（结果仍是 `FAILED_FINAL`）、支付复核的「不入账结案」。
- **新入账一律被拒**：记账总入口 `post_transaction` 在租户行锁内拒绝 `CLOSED`。关户后到达的用量事件计费为 `FAILED_FINAL` /
  `ACCOUNT_CLOSED`、没有账本行；迟到到账的支付进 `REVIEW_REQUIRED` / `TENANT_CLOSED`、没有账本行；争议与退款事件只关联、不能记账扣回。
- **对账单**：关户当月（马来西亚日历月）在次月第 2 日照常出最后一份，没有发生额时是期初 = 期末 = 0 的一份；之后的账期不再生成，
  「缺失对账单」告警也不把它们算作缺失。已开具的收据与对账单保留不变。
- **客户门户**：只读（看账单与收据）。

### 立刻做什么

固定顺序：**每步做完再做下一步；任何一步发现问题就停下来，不跳步。**

1. **停用**：管理端客户详情「Disable account」，原因写关户工单号。此后新的 AI 调用与新充值被拒（spec §112.1 第 1 步）。
2. **等 AI 模块 outbox 清空**：在 `closure-check` 看 `unresolved_ai_usage` 归零（停用后不再有新的 AI 调用，在途的会在调用超时后写入并投递）。
   `HELD` 行按本文件「AI 模块用量 outbox：死信、暂存、待复核与积压」一节的 `HELD` 处理（`release` 或 `discard`，带原因）。
   ⚠️ **这一步完成之前不要吊销集成凭据**：投递要用项目的集成凭据签名，先吊销会让行一直停在 `PENDING`（`SIGNING_CREDENTIAL_MISSING`
   不断重试），反过来挡住关户。
3. **吊销凭据**：吊销该客户全部 AI 客户端密钥与全部集成凭据（整个 key）。集成方在此之后上报的用量被 401 拒绝；吊销前与集成方确认已无
   待上报的用量。
4. **等用量、支付与网关事件到终态**：`unresolved_usage_events`、`unresolved_payments`、`unresolved_payment_events` 归零。
   - 用量停在 `MODEL_UNKNOWN` / `PRICING_ERROR` / `FX_RATE_ERROR`：先修目录、价格或汇率，再按「用量计费」一节重新入队（见下面已知限制 3）；
   - 支付 `REVIEW_REQUIRED`：按 [payment-flow.md §6](payment-flow.md#6-复核处理) 结案；
   - `EXPIRED` 支付在创建满 7 天后不再挡；
   - 网关事件积压按「支付：复核、事件冲突、事件积压与配置」一节处理。
5. **余额清零**（**只用调账，不直接改钱包**；每笔调账的原因写清楚）：
   - **正余额，来自付款**：在 Stripe 后台对相应付款人工退款，退款额不超过该付款在 Stripe 上的可退额（系统不校验，见下面已知限制 2）。
     **每笔退款对应一条 `REFUND_ADJUSTMENT`**，**原因里写支付号**（`payments.public_id`）**与 Stripe 退款号**。退款会产生
     `charge.refunded` 事件（P1「disputes or refunds 24h」，预期内，24 小时后消失）；**等它被处理完**（`unresolved_payment_events`
     归零）再继续。
   - **正余额，不来自付款**（赠送、更正）：`ADJUSTMENT_DEBIT`，原因写明来源。
   - **负余额**：客户在系统外付清的，`ADJUSTMENT_CREDIT`，原因写外部付款凭据号；核销的，`ADJUSTMENT_CREDIT`，原因写「坏账核销」与审批人。
   - 核对余额恰好 `0.00000000`。
6. **收据与对账单**：不需要操作。已开具的收据不可变；没有收据的 `PAID` 支付由补发扫描照常补开；关户当月的对账单在次月第 2 日自动生成，
   之后不再生成。
7. **关户**：客户详情「Close account」（只在 `DISABLED` 时出现），确认 `closure-check` 全部为零，填原因（关户工单号），输入公司名确认
   （与客户详情里的公司名完全相同，区分大小写）。409 时弹窗显示原因码并重新检查，回到对应的步骤处理；5xx 或网络错误点「Try again」
   原样重发是安全的（已关户是 200 不写，未关户则重新判定）。
8. **关户之后**：
   - `FAILED_FINAL` / `ACCOUNT_CLOSED` 的用量事件人工复核，**不要重新入队**；
   - `REVIEW_REQUIRED` / `TENANT_CLOSED` 的支付只能「不入账结案」，并在 Stripe 退款；
   - 争议与退款事件不能再记账，在 Stripe 与会计账上处理（见下面已知限制 1）；
   - 账户不能重开；同一家公司要重新使用，新建客户。

⚠️ **已知限制**（设计 §10）：

1. **关户后的争议与退款不能再记账扣回**：事件只关联、不改账（`post_transaction` 拒绝 `CLOSED`，调账 409 `ACCOUNT_CLOSED`）。
   在 Stripe 与会计账上处理。这是「不得静默重开」的直接结果。
2. **退款扣回不校验可退额**：`REFUND_ADJUSTMENT` 不校验「≤ 该支付的可退额」，也没有结构化字段引用原支付。只能靠第 5 步在 Stripe
   核实金额、在原因里写支付号与 Stripe 退款号。
3. **用量停在 `MODEL_UNKNOWN` / `PRICING_ERROR` / `FX_RATE_ERROR` 时关不了户**：V1 没有「人工判为终结」的出口，必须修好目录、价格或
   汇率后重新入队，让它计费到终态。某个事件永远无法定价时，这个客户就关不了户，要另开设计闸门加出口。

### 恢复

- **`CLOSED` 是终态，没有恢复路径。**同一家公司要重新使用，在管理端**新建客户**（新的 `public_id`、新钱包、新项目与凭据）；旧客户的
  账本、收据与对账单保留不动。
- 关户返回 409：什么都没写，账户仍是 `DISABLED`。按 `closure-check` 的 `blockers` 回到上面对应的步骤，处理完再关。
- 关户返回 500（数据库错误、死锁）：整个事务回滚，账户仍是 `DISABLED`，重发即可。没有钱包导致的 500 是数据缺陷，按数据事故处理。

### 绝不能做什么

- **不要在 AI 模块 outbox 清空之前吊销集成凭据**（第 2 步）：行会一直停在 `PENDING`，挡住关户
- **不要直接改钱包、账本或 `account_status` 来「凑」关户条件或重开账户**：余额只经调账清零（INV-4）；改库绕过了锁、版本、状态投递与审计。
  没有「改回 `ENABLED`」这种操作
- **不要在 Stripe 退款超过该付款的可退额，也不要一条 `REFUND_ADJUSTMENT` 合并多笔退款**：系统不校验，一笔退款一条调账，原因写支付号与
  Stripe 退款号
- **不要在 `charge.refunded` 事件处理完之前关户**：`unresolved_payment_events` 不为零时关户本来也会 409；关户后到达的退款事件不能再扣回
- **不要重新入队 `FAILED_FINAL` / `ACCOUNT_CLOSED` 的用量事件**：结果仍是 `FAILED_FINAL`，只多一条审计
- **回滚提醒（设计 §8）**：若应用镜像回滚到 AIH-TASK-209 / AIH-TASK-210 之前而库里已有 `CLOSED` 客户，计费与支付确认仍会挡住用量与到账，
  但**手工调账**与建项目、建与轮换集成凭据、签发 AI 客户端密钥、签发与启用 webhook 签名密钥、开通内部计量、设置低余额阈值失去了拒绝。
  **不要对 `CLOSED` 客户做调账与这些写操作**，尽快前滚修复
- **不要把客户个人数据写进关户原因**：原因永久进审计，写工单号即可

---

## 配置项

**首次编写：2026-09-26（AIH-TASK-013）。**这一节收「运维会去改的配置项」，一项一小节。
事实来源写在每一小节开头；与代码不一致时**以代码为准**，并回来改这份文档。

### `BILLING_CREDENTIAL_ROTATION_OVERLAP_SECONDS` —— 集成 API 凭据的轮换重叠期

**依据**：[AIH-TASK-012 设计](design/AIH-TASK-012-integration-access.md)第 2 节「配置」与第 4 节状态表、
`app/core/config.py` 的 `credential_rotation_overlap_seconds`、[api.md](api.md) 的「轮换」一节、
`app/services/integration_access.py` 的 `rotate_credential`、`docker-compose.yml` 的 `x-backend.environment`、
`.env.example` 的「集成 API 凭据」一段。

#### 含义

管理员**轮换**一个集成 API 凭据（`POST …/credentials/{api_key}/rotate`）之后，**旧版本还能继续用于签名校验多少秒**。
重叠期里新旧版本都能通过校验，集成方在这段时间里换上新 `secret`；到点之后旧版本自然不可用 ——
没有定时任务去改状态，校验时按 `valid_until` 判断（`now < valid_until` 才可用）。

| 属性 | 值 |
| --- | --- |
| 配置项（代码里） | `credential_rotation_overlap_seconds` |
| 环境变量 | `BILLING_CREDENTIAL_ROTATION_OVERLAP_SECONDS`（前缀 `BILLING_` 来自 `Settings` 的 `env_prefix`） |
| 默认值 | **`604800` 秒 = 7 天**（Kelvin 2026-09-25 定） |
| 取值规则 | **整数秒，且不小于 0**（`Field(ge=0)`） |
| `0` 的意思 | 轮换即让旧版本**立刻**失效 —— 旧版本的 `valid_until` 被写成轮换那一刻，而校验要求 `now < valid_until` |
| 谁用它 | 只有轮换这一个动作。建凭据、吊销、列表都不读它 |

⚠️ **单位是秒，不是天或小时。**写 `7`、`7d`、`1.5` 都不是你想要的：`7` 是 7 秒；
`7d`、`1.5` 这类非整数与负数过不了配置校验，进程**起不来**（`Settings` 在启动时解析一次，校验失败就抛错）。
常用换算：`3600` = 1 小时，`86400` = 1 天，`604800` = 7 天。

#### 怎么改

在部署目录的 `.env` 里加（或改）一行：

```bash
BILLING_CREDENTIAL_ROTATION_OVERLAP_SECONDS=3600
```

- 容器里的值来自 `docker-compose.yml` 的 `x-backend.environment` 那一行
  `BILLING_CREDENTIAL_ROTATION_OVERLAP_SECONDS: ${BILLING_CREDENTIAL_ROTATION_OVERLAP_SECONDS:-604800}`（#124 起）。
  compose 读 `.env` **只为做 `${…}` 插值**，不把 `.env` 整个注入容器 —— 本栈**没有** `env_file:`，
  只有 `environment` 里逐项列出的变量才进容器。这一行的作用就是把 `.env` 里的值转进 api / celery-worker /
  celery-beat 三个后端容器
- `.env` 里不写这一行（或删掉它）= 用 compose 里的默认值 `604800`，与代码默认值一致
- `docker-compose.prod.yml` 只覆盖 `BILLING_ENVIRONMENT` 与 `BILLING_SESSION_COOKIE_SECURE`，**不碰**这一项，
  所以生产与本地走的是同一条转发
- 不经 compose、直接跑 uvicorn 时：`Settings` 从进程环境变量读，也读工作目录下的 `.env`（`env_file=".env"`）。
  `.env` 不进镜像（`.dockerignore` 排除了它），所以**容器里只有 compose 转发这一条路**
- ⚠️ **不要去改 `docker-compose.yml` 里的默认值**来「改配置」。那是仓库文件，下次部署 checkout 会把它换回去；
  而且 `tests/backend/test_compose.py` 钉着那个默认值必须等于代码默认值

#### 改完怎么生效

**必须重建后端容器，只改 `.env` 不会生效。**两层原因：

1. 容器的环境变量是**创建时**定下的。`docker compose restart` 只重启进程、不重新创建容器，拿到的仍是旧值
2. 进程里配置只解析一次：`get_settings()` 带 `lru_cache`，`create_app` 启动时把它放进 `app.state.settings`，
   轮换接口读的就是这一份。运行中的进程不会重读 `.env` 或环境变量

所以改完 `.env` 之后：

```bash
docker compose up -d     # 只重建配置变了的服务：这里是 api / celery-worker / celery-beat
```

⚠️ 这条命令只在**这台主机部署过**时成立：部署成功后两个镜像钉在 `.env` 里（[deployment.md](deployment.md) §9.7）。
灾难恢复的新主机上还没有那两行，不要在那里手工 `up -d`（理由见上面「整台 VPS 没了」第 9 步）。

**确认生效**（只读，不需要真的轮换一次）：

```bash
docker compose exec api python -c \
  'from app.core.config import get_settings; print(get_settings().credential_rotation_overlap_seconds)'
```

打印的是新值才算生效。⚠️ 这条命令起的是一个**新的** Python 进程，它证明的是「容器环境里的值对了」；
API 进程本身是随容器重建的，所以两者一致 —— 前提是你用的是 `up -d` 重建，而不是 `restart`。

#### 对已轮换凭据的影响

⚠️ **改这个配置不回溯。**它只在**轮换那一刻**被读一次，算出 `valid_until = 轮换时刻 + 重叠期` 写进库里。
已经写进库的 `valid_until` 是固定的时刻，不存「重叠期是多少」，改配置、重建容器都不会动它。

| 情形 | 结果 |
| --- | --- |
| 已经轮换过、旧版本正在重叠期里 | **不变**。旧版本仍按当初写入的 `valid_until` 到点失效 —— 把配置从 7 天改成 1 小时，**不会**让它提前失效；改成 0 也不会 |
| 从来没轮换过的凭据（`valid_until` 为空） | 不变，照常可用。直到下一次轮换才会用到新配置 |
| 改配置之后的**下一次轮换** | 按新值算：`截止 = 此刻 + 新重叠期`。这个 `api_key` 下**所有未吊销的旧版本**里，`valid_until` 为空、或晚于这个截止的，一律改成这个截止；本来就早于它的不动。已吊销的版本不动。新版本没有截止 |

最后一行有一个**容易踩到的后果**：把重叠期改短之后再轮换，**上一轮还在重叠期里的更老版本也会被一并截短**。
例：版本 1 因为上一次轮换（重叠期 7 天）还剩 5 天；现在把配置改成 `3600` 再轮换一次 ——
版本 1 与版本 2 的 `valid_until` 都变成「此刻 + 1 小时」。改成 `0` 再轮换，则**所有**未吊销的旧版本当场失效。
反过来，把重叠期改长**不会延长**任何已写入的截止：只有「为空或晚于新截止」的才会被改。

要**提前结束**某个旧版本的重叠期，不是改这个配置 —— 那对它不起作用 —— 而是吊销那个版本
（`POST …/versions/{key_version}/revoke`，立即生效、写审计）。每次轮换改了哪些版本的截止，
都记在那条 `API_KEY_ROTATE` 审计的 `before_state` / `after_state` 里。

#### 绝不能做什么

- **不要把它改成 0 当作「吊销旧版本」的办法。**它不影响已经轮换过的版本，只会让**下一次**轮换对集成方
  毫无缓冲 —— 集成方还没来得及换 `secret`，签名就开始被拒。要撤销一个版本，用吊销接口
- **不要只改 `.env` 就当改好了。**不重建容器，线上还是旧值；而轮换的后果（旧版本何时失效）要到重叠期结束时才看得出来
- **不要在集成方正在切换 `secret` 的时候缩短它再轮换。**上面那张表的最后一行：更老的版本也会被截短

### `BILLING_MONITOR_TOKEN_HOST_FILE` —— 监控令牌文件的宿主机路径（及两个 FX 告警地址）

**依据**：设计闸门 #183 v3（[AIH-TASK-028-fx-rates.md](design/AIH-TASK-028-fx-rates.md) §2「内部告警接口」「两个告警维度」、
§6「密钥」、§8「合并后的运维步骤」）、`deploy/monitor.sh` 的 `check_alerts` / `publish_alerts`、`docker-compose.yml` 的
`billing_monitor_token` secret、[deployment.md](deployment.md) §8.4（完整步骤与验证命令）。

#### 含义

`deploy/monitor.sh` 读 `GET /internal/alerts` 要带 Bearer 令牌；令牌在宿主机文件 `secrets/monitor.token` 里，容器经 compose
secret 读**同一份**文件（容器内 `/run/secrets/billing_monitor_token`，compose 写死，不经 `.env`）。
`.env` 里的这一项只告诉**宿主机上的** `monitor.sh` 那个文件在哪。

| 属性 | 值 |
| --- | --- |
| 读它的 | 只有 `deploy/monitor.sh`（字面解析 `.env`，不 source）。应用不读它 |
| 默认值 | 无。不配 = `monitor.sh` 只记 `no BILLING_MONITOR_TOKEN_HOST_FILE configured`，不推告警维度 |
| 取值 | 令牌文件的**绝对路径**，例如 `<部署目录>/secrets/monitor.token` |
| 令牌文件 | `openssl rand -hex 32`（64 个字符）；属主 `10001`、**属组为部署用户的主组**、`0440` |
| 相关的键 | `BILLING_HEALTHCHECK_ALERT_FX_STALE_URL`：FX 那一行（成员 `fx_fetch`、`fx_stale`）的 Healthchecks 检查的 Ping URL；`BILLING_HEALTHCHECK_ALERT_FX_FETCH_URL` 2026-11-21 起不再被读。通用规则 `BILLING_HEALTHCHECK_ALERT_<维度大写>_URL` |

⚠️ **为什么是 `0440` 而不是其他 secrets 的 `0400`**：`monitor.sh` 由 cron 以部署用户（不是 root）运行，`0400` 它读不到，
告警维度就永远推不出去。容器以 uid 10001 按属主读，部署用户按属组读，其他用户读不到。

#### 怎么改（首次配置 = 设计 §8 的合并后运维步骤）

1. 令牌文件：已有且不少于 32 个字符就不重新生成，只改属主与权限（`sudo chown 10001:"$(id -gn <部署用户>)"`、
   `sudo chmod 0440`）；没有或过短才 `openssl rand -hex 32` 生成。`.env` 加
   `BILLING_MONITOR_TOKEN_HOST_FILE=<部署目录>/secrets/monitor.token`
2. Healthchecks 新建 `ai_billing_hub fx_stale` 一个检查（Simple，5 分钟，宽限 15 分钟，Telegram），
   Ping URL 写进 `.env` 的 `BILLING_HEALTHCHECK_ALERT_FX_STALE_URL`（2026-11-21 之前 `fx_fetch` 还有自己的检查）
3. 在 worker 容器里手工触发一次拉取，确认 BNM 可达（「无记录」也算可达）：
   `docker compose exec celery-worker python -c "from app.tasks.fx_fetch import fetch; print(fetch())"`
4. 管理员发布第一个汇率版本（见上面「FX 汇率：过期，或没有生效的汇率」的「立刻做什么」）

逐条的命令、核对与验证在 [deployment.md](deployment.md) §8.4。

**轮换令牌**：重新生成文件内容（属主与权限照旧）即可。

#### 改完怎么生效

- `.env` 的这几项**不需要重建任何容器**：`monitor.sh` 每一轮（5 分钟）重新读 `.env`
- 令牌文件的内容**也不需要重启**：应用每次请求都重新读文件
- 验证：`BILLING_MONITOR_RECHECK_SECONDS=0 bash deploy/monitor.sh`，日志里出现 `fx_stale: fx_fetch OK: … | fx_stale OK: …`，检查变绿

#### 对已有数据的影响

无。令牌与地址只决定告警能不能送达，不碰汇率版本、拉取记录或计费。

#### 绝不能做什么

- **不要把令牌本身写进 `.env`**，也不要 `cat` 它、贴进对话或截图。`.env` 里只放路径（设计 §6）
- **不要改成 `0444` 或复制第二份令牌**给部署用户：两份要同步轮换，迟早对不上
- **不要删掉文件来「停用」接口。**生产上 compose 的 `file:` secret 缺文件时**部署直接失败**（[deployment.md](deployment.md) §6）
- **不要把心跳地址写进仓库或文档。**知道地址就能伪造「成功」；`config_snapshot.sh` 上传前按键名模式
  `^BILLING_HEALTHCHECK_[A-Z0-9_]*_URL=` 自查，撞上就拒传

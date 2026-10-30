# AIH-TASK-141 至 142 设计：计费扫描并行（已批准 v4）

> **来源**：设计闸门 Issue #522。本文件是 `APPROVED: design v4` 那一版正文的**逐字副本**。审查经过见 §12：v1、v2、v3 由 Codex 审查、被要求修改（v1：扫描时长没有上界，跨周期后计费会占满全部子进程；v2：名额连接独自断开时旧扫描仍在计费；v3：名额两个失败分支没有测试）；v4 由 Codex 批准。每一版送审之前都先跑了登记前预审，发现都已并入。
> 放进仓库，是因为 OpenClaw Worker 在沙箱里不联网、读不到 GitHub Issue。
> **实现以本文件为准**；与 Issue 不一致时，以 Issue #522 上被批准的 v4 为准。设计要改，就回到 Issue 升版本、重新过闸门，不要直接改本文件。
> **前置**：设计闸门 #181 v2（[AIH-TASK-032-usage-billing.md](AIH-TASK-032-usage-billing.md)，异步计费 worker）；测量依据是 [perf-baseline.md](../perf-baseline.md) 10.6。
> §11「任务拆分」的两个实现任务登记为 `AIH-TASK-141`（调度与并发）→ `AIH-TASK-142`（压测脚本）。

---
## 0. 准入判定

| 任务涉及 | 要填的范围 |
| --- | --- |
| 钱包 / 账本 / 定价 / 汇率 / 支付 / 幂等 / 状态机 | **全部章节** |
| 用量摄取 / 认证与会话（集成侧与平台侧）/ Webhook | §1–§7（**§6 不可省**） |
| 前端 / 文档 / CI / 脚本 | **不走本闸门**，直接开 PR |
| 既不碰钱、也不碰用量摄取 / 认证与会话 / Webhook 的后端改动 | **不走本闸门**，直接开 PR |

本任务档位：`全部`

理由：改的是用量计费 worker 的调度（多少个扫描同时认领并扣费）。它不改认领、处理、扣费的任何一个事务，但改变了「同时有几个进程在扣同一个钱包」，属于钱包 / 账本与幂等的并发面，所以按全部章节填。

---

状态：`READY_FOR_REVIEW`
负责人：Claude
**设计版本**：`v4`（每次实质修改 +1，见 §12）
对应需求：`REQ-INGEST-002` / spec §82、§83、§110、§119；设计闸门 #181 v2（异步计费 worker）
目标 PR：待开

## 1. 目标与边界

- **要解决的问题**：生产上用量计费的吞吐被 Beat 派发限死在约 22 条每秒，加 worker 无效。
  - Beat 每 10 秒只派一次 `bill_pending_events`，一次扫描只在一个 worker 子进程上跑，预算 8 秒（`app/core/celery_app.py` 的 `usage-billing-sweep`，`app/services/usage_billing.py` 的 `bill_pending_events`）。
  - 所以无论 `BILLING_CELERY_CONCURRENCY` 是几，同一时刻只有一个进程在计费。上限 ≈ 8 / 10 × 1 / 单条耗时。
- **可观察的完成标准**：
  1. Beat 每 10 秒派出 `USAGE_BILLING_SWEEP_PARALLELISM`（= 2）个扫描，二者同时运行、各自认领不同的事件。
  2. 任何时刻正在认领与处理事件的扫描最多 `USAGE_BILLING_SWEEP_PARALLELISM` 个，**与扫描各自跑了多久无关**：每次扫描先抢一个「扫描名额」（MySQL 命名锁），抢不到就立即返回、不认领任何事件；扫描在认领每一批与处理每个事件之前确认名额仍在自己手里，丢了就立即停下。唯一的例外是刚丢掉名额的扫描最多再做完手上正在处理的那一个事件（见 §4「上界」）。
  3. celery-worker 的默认并发从 2 改为 3：计费扫描最多占 2 个子进程（抢不到名额的扫描只占几毫秒），至少留 1 个给其他周期任务。
  4. 重跑 `docs/perf-baseline.md` 10.6 的剖析（压测脚本按新的派发节奏模拟，见 §11 的 AIH-TASK-142），积压清空速率高于这一节记录的 Beat 节奏实测（1 个 worker 22.0 条每秒、2 个 worker 21.1 条每秒）。这一条是上线后的运维验证，不是 CI 门槛。
- **明确不做什么**：
  - 不改认领（`claim_events`）、处理（`process_event`）、卡住回收的任何 SQL、锁顺序或事务边界。扫描名额是包在整轮扫描外面的一把命名锁，用自己的连接、不进入任何认领或处理事务。
  - 不改扫描的 10 秒周期、8 秒预算、每批 20 条。
  - 不改 celery-worker 的 CPU / 内存配额（1.0 核 / 768 MiB）。配额是 10.6「下一步」第 2 条，另立任务。
  - 不做计价解析 `resolve_charge_inputs` 的优化（10.6 结论第 4 条，「后续计划」里已有一项）。
  - 不做方案 C（同租户批事务），不改变 10.2 的判据。
  - 不追求 §119 的 500 条每秒：单租户串行提交的上限约 110 条每秒（10.6 结论第 5 条），本设计只解除「多 worker 无效」这一层。
  - 不做按租户分片或专用计费队列（§9 方案 C、D）。
- **现有行为与问题证据**：`docs/perf-baseline.md` 10.6（2026-10-30，生产 VPS，`billing_perf`）：
  - Beat 节奏下：1 个 worker 22.0 条每秒、忙碌 83.4%；2 个 worker 21.1 条每秒、Beat 节奏空转 57.4%。
  - 不等 Beat（扫描一结束就派下一次）：1 个 worker 26.0 条每秒；2 个 worker 36.0 条每秒，CPU 节流明显，单条墙钟 36 → 53 ms。
  - 这说明第二个 worker 只要有活干，就能多处理约 40%；现在它没活干，是派发造成的。

## 2. 设计概要

```text
celery-beat（每 10 秒）
  ├─ usage-billing-sweep-1 ─┐
  └─ usage-billing-sweep-2 ─┤  同名任务 app.tasks.usage_billing.bill_pending_events，expires 9 秒
                            ▼
celery-worker（prefork，并发 3，prefetch 1，acks_late）
  子进程 A：bill_pending_events —— 抢名额 1（GET_LOCK，超时 0）→ claim_events（SKIP LOCKED，20 条）→ 逐条 process_event → 直到 8 秒预算用完或无事可做 → 释放名额
  子进程 B：bill_pending_events —— 名额 1 已被占，抢到名额 2 → 同上；SKIP LOCKED 让 B 跳过 A 已锁的行，认领到另一批
  （上一轮的 A、B 跑过了 10 秒还没结束时，这一轮的两个扫描抢不到任何名额，立即返回 0，不认领）
  子进程 C：其他周期任务（状态 webhook 推送、outbox 恢复、卡住回收、AI 用量投递、余额核对、汇率拉取）
```

- **输入、输出和错误契约**：任务签名与返回值（认领条数）不变；抢不到名额时返回 0。服务函数 `bill_pending_events` 新增关键字参数 `parallelism: int | None = None`：`None` 表示不抢名额（现有调用方与测试的行为不变）；给了值就必须 ≥ 1，否则 `ValueError`。
- **新增或修改的 API**：无。
- **数据库表、字段、索引及唯一约束**：无改动。并发正确性完全靠 #181 v2 已有的机制：认领 `FOR UPDATE SKIP LOCKED`、每次认领一个新的防护令牌、处理事务核对令牌、账本 `(USAGE_EVENT, event_id)` 唯一约束、`PROCESSED` 不可改的触发器。
- **事务边界**：不变。认领一个短事务；每个事件一个处理事务（事件行锁 → 三把共享锁 → 钱包行锁 → 扣费 → 状态，同一事务提交）。
- **改动点**：
  0. **扫描名额**（`app/services/usage_billing.py`，新的私有上下文管理器，由 `bill_pending_events` 在 `parallelism` 非 `None` 时使用）：
     - 名额是 MySQL 命名锁，名字是 `CONCAT(DATABASE(), ':usage-billing-sweep:', i)`，`i` 从 1 到 `parallelism`。带上库名，是因为命名锁在整个 MySQL 实例内共享：同一实例上的 `billing_perf` 压测库、CI 里 xdist 各 worker 的测试库，不能和生产库抢同一组名额。库名最长约 20 个字符（`billing`、`billing_perf`、`billing_test_gw<N>`），名字远在 MySQL 64 字符的上限之内。
     - 抢名额：从 session factory 绑定的 engine 取一个**专用连接**（`AUTOCOMMIT`，不开事务，不与认领和处理的 session 共用），按 `i = 1 … parallelism` 依次 `SELECT GET_LOCK(name, 0)`（超时 0，不等待），第一个返回 1 的就是本轮的名额；全部返回 0 时不认领、直接返回 `SweepResult(claimed=0)`，记一行 debug 日志。任何一次 `GET_LOCK` 返回 NULL 或语句本身失败，都**不再尝试后面的名额**：这一轮不认领、返回 `SweepResult(claimed=0)`，记一行 warning 日志（出错多半是这条连接或 MySQL 有问题，保守地跳过这一轮，下一个 10 秒再来）。
     - **确认名额仍在**：在每次 `claim_events` 之前、每次 `process_event` 之前，都在名额连接上执行 `SELECT IS_USED_LOCK(name) = CONNECTION_ID()`。返回 1 才继续；返回 0 或 NULL，或语句本身失败（名额连接已断开、被 KILL），就认为名额已丢：不再认领、不再处理本批里剩下的事件，记一行 warning 日志，结束本轮（返回的 `SweepResult` 照常包含已认领条数与已处理事件的结局）。本批里已认领但没处理的事件留在 `PROCESSING`，由每 60 秒的卡住回收在租约（`usage_lease_seconds`，120 秒）过期后释放为可再认领，与「处理者崩溃」走同一条恢复路径（#181 v2 §2），每个事件至多一次财务效果照旧由 `SKIP LOCKED`、防护令牌与账本唯一约束保证。每个事件多一条不碰表的 SELECT（10.6 实测单条约 36 ms，这条往返在 1 ms 以内）。
     - 释放：整轮扫描结束（包括抛异常、名额已丢）时在 `finally` 里 `SELECT RELEASE_LOCK(name)`，再把连接还回连接池。释放语句本身失败时 `invalidate()` 这个连接（让池子丢掉它、MySQL 随连接断开释放锁），不把带着命名锁的连接还回池里。
     - 崩溃安全：命名锁随会话存在，子进程被杀或连接断开时 MySQL 自动释放；不落任何表，没有需要清理的残留。
     - SQLite（本地与单元测试）：没有命名锁，名额永远抢得到（不做限制）；生产只跑 MySQL。
     - **三条语句（抢、确认、释放）各包成一个模块级私有函数**，名额的其余逻辑只经它们访问 MySQL，测试经 `monkeypatch` 替换它们来注入 NULL、0 与异常：三个函数都接收名额序号 `slot: int`（1 … `parallelism`），名字只在 SQL 里拼：模块级常量 `_SWEEP_SLOT_NAME = "CONCAT(DATABASE(), ':usage-billing-sweep:', :slot)"`，`slot` 作为绑定参数传入，Python 侧不拼名字字符串。`_get_lock(connection, slot) -> int | None` 执行 `SELECT GET_LOCK(<名字>, 0)`；`_holds_lock(connection, slot) -> int | None` 执行 `SELECT IS_USED_LOCK(<名字>) = CONNECTION_ID()`；`_release_lock(connection, slot) -> int | None` 执行 `SELECT RELEASE_LOCK(<名字>)`；三者都原样返回结果（1 / 0 / None）。三个函数都不吞异常：语句失败时异常向上抛，由调用方按上面的规则处理（`_get_lock` 失败 = 这一轮返回 0；`_holds_lock` 失败 = 名额已丢；`_release_lock` 失败 = `invalidate()` 名额连接、记日志、不向外抛）。
     - `app/tasks/usage_billing.py` 的 `bill_pending_events` 任务传 `parallelism=USAGE_BILLING_SWEEP_PARALLELISM`（从 `app.core.celery_app` 导入）。
  1. `app/core/celery_app.py`：
     - 新增常量 `USAGE_BILLING_SWEEP_PARALLELISM = 2`。
     - `beat_schedule` 里原来的 `usage-billing-sweep` 一个条目，换成按这个常量生成的 `usage-billing-sweep-1` … `usage-billing-sweep-N`。每个条目的 task、周期（10 秒）、`expires`（9 秒）与原来相同。
     - 用常量而不是配置项：周期、`expires`、预算都已经是这个文件里的常量，同一处管理；改它要同时改 worker 并发（下一条），不应该让 `.env` 单独改一边。
  2. `docker-compose.yml`：celery-worker 的 `--concurrency=${BILLING_CELERY_CONCURRENCY:-2}` 默认值改为 `3`，注释写明「至少比计费扫描的并行数多 1」。生产 `.env` 没有设 `BILLING_CELERY_CONCURRENCY`（2026-10-30 核对），部署后直接取新的默认值。
  3. `docs/runbook.md`、`docs/deployment.md`：Beat 条目名、扫描名额（命名锁的名字与「抢不到就返回 0」）、并发数与「并发 ≥ 并行数 + 1」的约束。
- **外部系统与异步任务边界**：Beat 只是触发，数据库是事实来源（INV-14）。两个条目在同一时刻入队，各自带 `expires`；worker 以 prefetch 1 取任务，所以两个扫描落到两个不同的子进程。
- **时间语义**：不变。`occurred_at`、`processed_at`、价格 / 规则 / 汇率按 `occurred_at` 取版本，都在 `process_event` 里，本设计不碰。
  - 两个扫描并行时，同一租户的事件可能不再严格按 `id` 顺序提交（A 处理第 1–20 条，B 处理第 21–40 条，B 的第 21 条可能先于 A 的第 20 条提交）。这在今天也会发生：一次扫描超出周期时，下一次扫描会和它重叠；卡住回收后的重试也会乱序。账本序号按提交顺序分配，余额只依赖提交顺序（钱包行锁串行化），计费额只依赖事件自己的 `occurred_at` 与快照，所以乱序提交不改变任何金额。见 §4。
- **金额精度、舍入位置及币种转换**：不变，全部在 `process_event` 与计价引擎里。

## 3. 不变量影响矩阵

| 不变量 | 是否触碰 | 可能怎样被破坏 | 设计控制 | 验证方式 |
| --- | --- | --- | --- | --- |
| INV-1 中心故障不中断 AI | 否 | 计费吞吐只影响事件从 `RECEIVED` 到 `PROCESSED` 的延迟，不在 AI 调用路径上 | — | — |
| INV-2 事件不重复扣费 | **是** | 两个扫描同时认领同一事件；或一方处理中、另一方回收后再处理 | 认领 `FOR UPDATE SKIP LOCKED`（同一行只会被一方锁住并认领）；处理事务核对防护令牌；账本 `(USAGE_EVENT, event_id)` 唯一；`PROCESSED` 不可改 | #181 已有的并发用例（`test_two_connections_claim_forty_events_each_exactly_once`、`test_four_workers_bill_two_hundred_events_and_survive_a_crash`）照旧通过；新增 §7「两个扫描并行」 |
| INV-3 支付不重复入账 | 否 | 不涉及支付 | — | — |
| INV-4 余额只经账本变动 | **是** | 两个进程同时改同一钱包余额 | 扣费仍只经 `post_transaction`，在钱包行锁内读余额、写账本、改余额；两个进程在这把锁上串行 | §7「两个扫描并行」核对每个租户「期初 − Σ 计费额 = 期末」与 `verify_wallet` |
| INV-5 历史账本不可变 | 否 | 只追加，不改已有账本行；触发器不变 | — | — |
| INV-6 事件保留版本引用 | 否 | 快照写入在 `process_event` 里，不变 | — | — |
| INV-7 客户不可见成本毛利 | 否 | 不改任何对外接口 | — | — |
| INV-8 租户不可互访 | 否 | 认领不分租户（今天就是），处理按事件自己的租户取钱包与规则，不变 | — | — |
| INV-9 对话内容不入库 | 否 | 不涉及 | — | — |
| INV-10 金额用 Decimal | 否 | 不新增金额计算 | — | — |
| INV-11 event_id 至多一次财务效果 | **是** | 同 INV-2 | 同 INV-2 | 同 INV-2 |
| INV-12 定稿对账单不可变 | 否 | 对账单尚未实现；乱序提交不改变事件的 `occurred_at`，不影响期间归属 | — | — |
| INV-13 状态与事件原子提交 | 否 | 余额跌破 0 时的停机跃迁、`status_version` +1、审计、outbox 仍与那一笔扣费同一事务（`post_transaction`），不变。两个进程同时扣同一钱包时，钱包行锁保证只有一笔扣费看到「由正变为 ≤ 0」 | — | §7「两个扫描并行」里造一个会跌破 0 的租户，断言恰好一次停机跃迁 |
| INV-14 队列丢失不毁持久工作 | **是** | 多了一个 Beat 条目，队列丢失时丢的是两个触发 | 数据库仍是事实来源：丢了触发，下一个 10 秒照常派；`expires` 防止 Beat 恢复后一口气放出积攒的触发 | `test_celery.py` 断言每个扫描条目的周期与 `expires` |

## 4. 状态与并发

不新增状态，不改跃迁表（#181 v2 §4 原样适用）。并发面的变化只有一处：**同一时刻最多有 `USAGE_BILLING_SWEEP_PARALLELISM` 个扫描在认领与处理**，由扫描名额保证。

- **为什么需要名额、不能只靠 Beat 的节奏**：8 秒预算只在每批认领之前检查（`bill_pending_events` 的循环），一批 20 条里每条的处理事务又可能等钱包锁或共享锁最多 10 秒（会话级 `innodb_lock_wait_timeout`），Celery 的 `expires` 也只拦「还没开始的任务」。所以一轮扫描的实际时长没有上界，可能跨过好几个 10 秒周期；没有名额时，Beat 每 10 秒再派 2 个，计费会占满全部子进程。今天（1 个扫描、并发 2）也有同样的缺口：扫描跨周期时两个子进程都在计费。名额把这个上界变成硬的：跑得再久，同时计费的也只有 N 个，新派的扫描抢不到名额就在几毫秒内返回。
- **上界**：任何时刻正在处理事件的扫描 ≤ `USAGE_BILLING_SWEEP_PARALLELISM` + 「刚丢掉名额、正在做完手上那一个事件的扫描」。后者只在名额连接独自断开时出现，每个这样的扫描最多再占一个事件的处理时长（正常约 36 ms，最坏是一次等锁超时 10 秒），之后在下一次确认时停下。所以计费占满全部子进程的时长有界，不会像 v1 那样随积压持续。
- **名额与事件锁的关系**：名额只决定「这一轮扫描要不要开始认领」，不参与任何事件的正确性；就算名额失效（例如两个进程都以为抢到了同一个），后果也只是多一个并行扫描，`SKIP LOCKED` + 防护令牌 + 账本唯一约束照样保证每个事件至多一次财务效果。

- **并发写入如何串行化**：事件行用 `SKIP LOCKED` 分开（不等待、不重复）；同一钱包的扣费在钱包行锁上串行（等锁上限 10 秒，会话级 `innodb_lock_wait_timeout`）；价格 / 规则 / 汇率发布与计费之间的共享锁不变。
- **数据库负责的唯一性**：账本 `(USAGE_EVENT, event_id)` 唯一；事件 `event_id` 全局唯一（摄取侧）。
- **幂等键**：`event_id`，不变。
- **相同 ID、不同载荷**：摄取侧处理（409 `IDEMPOTENCY_CONFLICT`），不经过本设计。
- **重试发生在哪一层**：事件级退避重试（`FAILED_RETRYABLE` + `next_attempt_at`），不变；Celery 任务本身不重试（失败了等下一个 10 秒）。
- **必须原子提交的操作**：不变（每个事件的快照、账本行、余额、状态、可能的停机跃迁同一事务）。
- **同一租户的提交顺序**：并行后，同一租户的事件提交顺序可能与 `id` 顺序不同。会受影响的只有「哪一笔扣费让余额先跌破 0」：它决定停机跃迁挂在哪个事件上，但不改变跃迁是否发生、何时之后的事件按停机处理（停机不拦计费，§112.1 / AIH-TASK-020）。账本序号、余额、每个事件的计费额都不依赖 `id` 顺序。今天扫描重叠、重试、卡住回收也会造成同样的乱序，所以这不是新语义。
- **等锁**：两个扫描处理同一个最热租户时，第二个会在钱包行锁上等第一个提交（持锁约 9 ms，10.6 实测等锁 p95 7 ms）。不会死锁：处理事务按固定顺序取锁（事件行 → 共享锁 → 钱包行），两个事务持有的事件行不同、争的只有同一把钱包锁。

## 5. 失败与恢复矩阵

| 失败点 | 对外结果 | 数据最终状态 | 是否可重试 | 恢复来源 | 告警 |
| --- | --- | --- | --- | --- | --- |
| 校验失败（事件无法计价） | 无对外结果 | 与今天相同：`PRICING_ERROR` / `FAILED_RETRYABLE` / `FAILED_FINAL` | 按 #181 | 不变 | 不变 |
| DB 提交失败 / 等钱包锁超时 | 无 | 该事件事务回滚，记 `FAILED_RETRYABLE` + 退避；另一个扫描不受影响 | 是 | 下一次扫描 | 不变（`usage_processing_backlog`） |
| Redis / Celery 丢失 | 无 | 事件停在 `RECEIVED`；已认领的停在 `PROCESSING` | 是 | Beat 恢复后下一个 10 秒；租约过期的由每 60 秒的卡住回收释放 | `usage_processing_backlog` |
| worker 子进程崩溃（处理到一半） | 无 | 处理事务回滚，事件停在 `PROCESSING`，行锁随连接断开释放 | 是 | 卡住回收（租约过期后），与今天相同 | 不变 |
| 两个扫描都占着子进程、其他周期任务排队 | 第 3 个子进程照常取其他周期任务；排在它后面的其他任务按各自的耗时排队，与今天「2 个子进程、计费占 1 个」时可用的子进程数相同 | 无数据影响 | — | 第 3 个子进程（并发 3） | 状态推送超时由现有维度覆盖 |
| 扫描跨过 10 秒周期（等锁、积压大） | 本轮的两个扫描继续占着名额；Beat 新派的扫描抢不到名额，几毫秒内返回 0 | 无数据影响；积压留到名额释放后的下一轮 | — | 名额释放后的下一次 Beat 触发 | `usage_processing_backlog`（不变） |
| 抢名额时 MySQL 出错（`GET_LOCK` 返回 NULL 或语句失败） | 这一轮不认领、返回 0，记 warning | 无数据影响 | 是 | 下一次 Beat 触发 | 日志；持续时由 `usage_processing_backlog` 兜底 |
| 名额连接在扫描中途断开（网络、被 KILL、MySQL 重启），处理连接仍正常 | MySQL 释放名额，下一轮扫描可以抢到它；旧扫描在下一次确认（下一个事件或下一批之前）时发现名额已丢，停下 | 旧扫描本批里没处理的事件留在 `PROCESSING`，租约过期后由卡住回收放回，再被认领计费；已处理的照常 `PROCESSED` | 是 | 卡住回收 + 下一次 Beat 触发 | warning 日志；回收由现有日志记录 |
| 释放名额失败 | 连接被 `invalidate()`，MySQL 随连接断开释放命名锁 | 无数据影响；名额不会泄漏到连接池里的其他使用者 | — | — | 日志 |
| 持有名额的子进程崩溃 | 无 | 命名锁随连接断开释放；它认领的事件照 #181 由卡住回收释放 | 是 | 卡住回收 + 下一次 Beat 触发 | 不变 |
| 有人把 `BILLING_CELERY_CONCURRENCY` 设成 ≤ 2 | 积压时两个扫描占满全部子进程，其他周期任务要等扫描结束；超过各自 `expires` 的触发被丢弃，等下一个周期 | 无数据丢失（都是「库里是事实、触发可丢」的任务），只是延迟变大 | — | 改回 ≥ 3 | 无专门告警；`docs/deployment.md` 写明约束，`test_compose.py` 守住默认值 |
| 重复请求（两个扫描抢同一批） | 无 | `SKIP LOCKED` 保证各认领不同的行；空认领只是一次查询 | — | — | — |

## 6. 数据与安全边界

- **租户过滤**：不变。认领不分租户；处理按事件自己的租户取钱包、规则、项目。
- **鉴权主体及权限**：不涉及（后台任务，无请求主体）。
- **客户侧禁止返回的内部字段**：不涉及，不改任何接口。
- **日志、审计、异常**：不变。每个扫描仍只记「认领了几条、各结局几条」，不记事件内容。
- **密钥**：不涉及。
- **prompt / response 或客户数据**：不涉及。
- **数据保留及删除**：不涉及。

## 7. 测试证据计划

| 风险/需求 | 测试层级 | 场景 | 预期结果 |
| --- | --- | --- | --- |
| 正常路径：Beat 条目 | unit（`tests/backend/test_celery.py`） | 读 `beat_schedule` | 扫描条目恰好 `USAGE_BILLING_SWEEP_PARALLELISM` 个、名字是 `usage-billing-sweep-1..N`，task 都是 `app.tasks.usage_billing.bill_pending_events`、周期 10、`expires` 9；`usage-stale-recovery` 只有一个；其余条目集合不变 |
| 边界值：常量 | unit | 读常量 | `USAGE_BILLING_SWEEP_PARALLELISM` 是 ≥ 1 的整数，当前为 2 |
| 并发数约束 | unit（`tests/backend/test_compose.py`） | 读 compose 里 celery-worker 的 `--concurrency` 默认值 | 默认值 = 3，且 ≥ `USAGE_BILLING_SWEEP_PARALLELISM` + 1（两边一起改才通过） |
| 并发：两个扫描并行、余额跨零 | integration（真 MySQL，`tests/backend/test_usage_billing_concurrency.py`） | 用文件里现有的 `world`（一个租户、一个模型）与 `publish_markup_usd()`；先用一笔 `ADJUSTMENT_DEBIT`（`post_transaction`，写法同 `World.fund`）把余额压到 `100 × MARKUP_BILLABLE + MARKUP_BILLABLE / 2`，记下此时的余额与 `status_version`；造 120 条事件；用文件里的 `run_in_threads` 让两个线程同时调用 `world.bill(NOW, parallelism=2)`（写法同四 worker 用例；`World.bill` 新增关键字参数 `parallelism: int | None = None`，原样传给 `bill_pending_events`，现有调用都不传、行为不变）。并发阶段结束后，照四 worker 用例在 `NOW + 1 小时` 反复 `world.bill(..., parallelism=2)` 直到认领 0 条：并发时撞上死锁或等锁超时而退避的事件在这一步补完，**这是 #181 已接受的行为，不算失败** | 两个线程都返回 `SweepResult`、各自 `claimed ≥ 1`（`bill_pending_events` 只返回条数，不比较具体事件集合；「恰好一次」由下面的账本一一对应与唯一约束证明）；补完之后：全部 `PROCESSED`；`AI_USAGE` 账本 120 行，按 `reference_id` 与事件一一对应、`amount = −billable_cost`；余额 = 压低后的余额 − Σ 计费额，`verify_wallet` 通过；租户 `billing_status` 为 `SUSPENDED`，`status_version` 恰好 +1（余额只跨零一次，两个进程并发扣同一钱包也只有一笔扣费看到「由正变为 ≤ 0」） |
| 名额：抢不到就不认领 | integration（真 MySQL，`tests/backend/test_usage_billing_concurrency.py`） | 造 10 条到期事件；用两个外部连接分别 `GET_LOCK` 住名额 1、2（名字与实现相同，用 `CONCAT(DATABASE(), ...)` 拼）；调用 `bill_pending_events(..., parallelism=2)` | 返回 `claimed == 0`，10 条都还是 `RECEIVED`、`attempt_count` 不变；外部连接释放名额 2 后再调用一次，认领到事件并处理；这次调用结束后名额 2 空闲（实现释放了自己抢到的名额）、名额 1 仍被外部连接持有；外部连接再释放名额 1，之后两个名额都空闲（`IS_FREE_LOCK` 为 1） |
| 名额：跨周期时第三个扫描立即返回 | integration（真 MySQL） | 造 40 条到期事件；一个外部连接 `SELECT … FOR UPDATE` 锁住该租户的钱包行；两个线程调用 `bill_pending_events(..., parallelism=2)`，等到 `performance_schema.data_lock_waits` 里（按 `DATABASE()` 与表名 `wallets` 过滤，写法同 `tests/backend/test_session_revocation_concurrency.py` 的 `_WAITING_ON_TABLE`）出现 2 个等待者；此时主线程再调用一次 `bill_pending_events(..., parallelism=2)`；然后外部连接回滚放锁、两个线程结束 | 第三次调用在 1 秒内返回 `claimed == 0`；放锁后两个线程正常结束，全部事件（照四 worker 用例在 `NOW + 1 小时` 补完退避的）`PROCESSED`、账本一一对应；最后两个名额都空闲 |
| 名额：名额连接中途断开 | integration（真 MySQL） | 造 30 条到期事件；`monkeypatch` 包一层 `process_event`：处理完第 3 个事件后，用一个外部连接查 `IS_USED_LOCK(name)` 得到持有者的连接 id 并 `KILL` 它；调用 `bill_pending_events(..., parallelism=2)` | 调用正常返回（不抛异常）；恰好 3 个事件 `PROCESSED`；本批其余已认领的事件停在 `PROCESSING`；外部连接能立刻 `GET_LOCK(name, 0)` 抢到这个名额（随后释放）；把时钟推过租约跑一次卡住回收，再在 `NOW + 1 小时` 反复 `bill_pending_events` 直到认领 0 条：全部 `PROCESSED`、`AI_USAGE` 账本按 `reference_id` 一一对应、`verify_wallet` 通过 |
| 名额：释放失败就丢弃连接 | integration（真 MySQL） | 造 5 条到期事件；`monkeypatch` 替换 `_release_lock`，让它抛 `sqlalchemy.exc.OperationalError`；调用 `bill_pending_events(..., parallelism=2)` | 事件照常处理；释放异常不向外抛（记日志）；名额连接被 `invalidate()`：外部连接在 1 秒内（每 50 ms 轮询一次）看到该名额 `IS_FREE_LOCK` 为 1；之后再调用一次 `bill_pending_events(..., parallelism=2)` 能正常抢到名额（证明池里没有一个还占着名额的连接） |
| 名额：`GET_LOCK` 返回 NULL 或出错 | integration（真 MySQL，参数化两种：返回 `None`、抛 `sqlalchemy.exc.OperationalError`） | 造 10 条到期事件；`monkeypatch` 替换 `_get_lock`，第一次调用就返回 `None` 或抛异常，并记下调用次数；调用 `bill_pending_events(..., parallelism=2)` | 返回 `claimed == 0`，不抛异常；`_get_lock` 只被调用 1 次（不再试名额 2）；10 条仍是 `RECEIVED`、`attempt_count` 不变；记了一行 warning；两个名额都空闲 |
| 名额：确认时返回 0、NULL 或出错 | integration（真 MySQL，参数化三种：返回 `0`、返回 `None`、抛 `sqlalchemy.exc.OperationalError`） | 造 10 条到期事件；`monkeypatch` 包一层 `_holds_lock`：前 3 次调用交给真函数（认领前 1 次、第 1 与第 2 个事件之前各 1 次），第 4 次起（第 3 个事件之前）返回 `0` / `None` 或抛异常；调用 `bill_pending_events(..., parallelism=2)` | 正常返回、不抛异常；恰好 2 个事件 `PROCESSED`，其余 8 条停在 `PROCESSING`；记了一行 warning；调用结束后两个名额都空闲（实现在 `finally` 里照样释放了真正持有的命名锁）；把时钟推过租约跑一次卡住回收，再在 `NOW + 1 小时` 反复 `bill_pending_events` 直到认领 0 条：全部 `PROCESSED`、`AI_USAGE` 账本按 `reference_id` 一一对应、`verify_wallet` 通过 |
| 名额：扫描抛异常也释放 | integration（真 MySQL） | `monkeypatch` 让 `claim_events` 抛异常，调用 `bill_pending_events(..., parallelism=2)` | 异常照常抛出；两个名额都空闲 |
| 名额：参数与 SQLite | unit（`tests/backend/test_usage_billing.py`，SQLite） | `parallelism=0`；`parallelism=2` 在 SQLite 上 | 前者 `ValueError`、不认领；后者照常认领并处理（SQLite 不做限制） |
| 任务传名额数 | unit（`tests/backend/test_usage_billing.py`） | `monkeypatch` 替换服务函数，调用任务 `app.tasks.usage_billing.bill_pending_events` | 服务函数收到 `parallelism == USAGE_BILLING_SWEEP_PARALLELISM` |
| 幂等重放 | integration（与「并发：两个扫描并行、余额跨零」是**同一个用例**，写在它的末尾，不另建场景） | 那个用例补完之后，再用 `run_in_threads` 让两个线程各调用一次 `world.bill(NOW + 1 小时, parallelism=2)` | 两个都认领 0 条，账本行数、余额与 `status_version` 不变 |
| 事务中途失败 | integration | 已有的 `test_four_workers_bill_two_hundred_events_and_survive_a_crash` | 照旧通过（不改） |
| Redis / Celery 丢失 | unit | 已有的 `expires` 断言扩展到每个扫描条目 | 每个条目 `expires` < 周期 |
| 租户越权 | 不新增 | 认领与处理不分租户，按事件取租户的逻辑不变 | 已有用例覆盖 |
| 金额精度 | 不新增 | 不新增金额计算 | 已有用例覆盖 |
| 吞吐（运维验证） | 生产 VPS 上的 `billing_perf`（AIH-TASK-142 之后） | `perf_usage_profile.py` 按新的派发节奏（每 10 秒派 N 个扫描）跑 1 个与 2 个 worker | 2 个 worker 的清空速率高于 10.6 的 21.1 条每秒；结果回填 `docs/perf-baseline.md` |

**MySQL 用例的清理**：沿用 `test_usage_billing_concurrency.py` 现有的 `engine` / `world` fixture，它在每个用例前后调用文件里的 `clean()`（TRUNCATE 本文件写过的表、删本文件建的租户与目录行），与现有用例完全相同；不新增清理逻辑、不动触发器。

## 8. 迁移与上线

- **数据迁移步骤**：无。
- **锁表与性能影响**：
  - 无 DDL。
  - 积压时 celery-worker 同时有 2 个计费进程，1 核配额下 CPU 节流会增加（10.6：不等 Beat 时 2 个 worker 节流约 16.7 秒每 55.5 秒），同一容器里其他任务（状态 webhook 推送等）的单次耗时会变长；无积压时第二个扫描只多一次空认领查询（每 10 秒一条 SQL）。
  - 内存：每多一个 prefork 子进程约 +100 MiB RSS（2026-10-30 生产实测，每个子进程约 99–107 MiB，容器合计 177 MiB / 768 MiB），并发 3 预计约 280 MiB，仍在 768 MiB 之内。
- **兼容窗口**：不需要。新旧 Beat 条目都派同一个任务；部署时 Beat 与 worker 同时换镜像。
- **部署顺序**：合并到 main 即部署；compose 默认并发改为 3，celery-worker 随部署重建。生产 `.env` 没有覆盖 `BILLING_CELERY_CONCURRENCY`，无管理员步骤。
- **回滚或前滚方式**：回滚提交即可，回到 1 个扫描、并发 2。无数据需要处理。
- **部分部署时的行为**：Beat 先换、worker 后换的几十秒内，旧 worker（不抢名额、并发 2）会同时跑两个扫描，即上面失败矩阵「有人把并发设成 ≤ 2」那一行，只是其他周期任务略有延迟；worker 先换、Beat 后换时，新 worker 只收到一个扫描，行为与今天相同。
- **监控指标和告警负责人**：`usage_processing_backlog`（不变）。上线后的吞吐由 AIH-TASK-142 合并后在 VPS 上重跑剖析验证，负责人 Claude，结果走文档 PR。

## 9. 方案取舍

| 方案 | 优点 | 风险 | 未采用原因 |
| --- | --- | --- | --- |
| **采用**：Beat 每 10 秒派 N（= 2）个扫描，worker 并发 N + 1 | 只改调度与一个默认值；并发正确性完全复用 #181 已论证、已测试的 `SKIP LOCKED` + 防护令牌 + 唯一约束；其他周期任务可用的子进程数与今天相同（今天：2 个子进程、计费占 1 个；改后：3 个、计费占 2 个） | 1 核配额下两个计费进程会互相节流，收益低于 2 倍（10.6 不等 Beat 的实测：26 → 36 条每秒）；`BILLING_CELERY_CONCURRENCY` 被人调小时会挤占其他任务（见 §5） | — |
| 备选 E：不加名额，只靠 Beat 每 10 秒派 N 个 + `expires` | 少一把锁 | 一轮扫描的时长没有上界（预算只在每批之前检查，单条可等锁 10 秒），跨周期后计费会占满全部子进程 | v1 采用过，Codex 判阻断（v1 → v2） |
| 备选 F：给扫描加 Celery `time_limit` / `soft_time_limit` | 时长有硬上界 | 到点时可能正处在处理事务中途：硬限制直接杀子进程，事件停在 `PROCESSING` 等卡住回收；软限制抛异常的位置不可控 | 用杀进程换时长上界，比名额粗暴，且不减少同时计费的进程数 |
| 备选 A：扫描结束时若还有积压就立即续派自己 | 占空比接近 100%（10.6：1 个 worker 从 22 升到 26 条每秒） | 并行扫描的数量不再由 Beat 决定：每个扫描各续派一个，Beat 每 10 秒再加一个，积压期间会持续增长，直到占满全部子进程、挤掉其他周期任务；要限住它得另加一个分布式计数或租约 | 有了名额之后续派也能被限住，但仍要处理「续派与 Beat 同时来」的竞争；相对采用方案，单 worker 的占空比只差约 20%，先不做 |
| 备选 B：把扫描预算从 8 秒拉长、或缩短周期 | 一行常量 | 不解决「同一时刻只有一个进程在计费」 | 不解决问题本身 |
| 备选 C：按租户分片（每个扫描只认领 hash(tenant) mod N 的事件） | 同一租户不会被两个进程同时处理，没有钱包锁等待 | 最热租户的积压只能由一个进程处理，正是 §119 最热租户场景要解的那一种；要改认领 SQL 与索引 | 对最热租户没有收益，而它才是瓶颈 |
| 备选 D：计费用专用队列与专用 worker 容器 | 计费与其他周期任务彻底隔离 | 多一个容器（再 768 MiB、再 1 核配额，而机器只有 2 核、同机 28 个容器）；compose、监控、巡检都要加一份 | 现在的规模用「并发 N + 1」就能保证其他任务至少有一个子进程，不值得多一个容器 |

## 10. 未决问题与假设

- **未决问题**：无。
- **尚未验证的假设**：
  0. MySQL 8.4 的 `GET_LOCK` 在一个会话里可以同时持有多把不同名字的锁、超时 0 时立即返回 0，连接断开时自动释放。依据：MySQL 5.7 起支持一个会话持有多把命名锁；§7 的三个名额用例在 CI 的 MySQL 上实测。
  1. Beat 两个同周期条目在同一时刻入队，prefetch 1 下会落到两个不同的子进程。依据：`worker_prefetch_multiplier = 1` + `task_acks_late`，每个子进程同时只持有一个任务。
  2. 生产吞吐的提升接近 10.6「不等 Beat、2 个 worker」的 36 条每秒乘以 0.8 的占空比（约 29 条每秒）。依据是 10.6 的同机实测；真实生产上 celery-worker 与 api 是两个容器、各自 1 核，比压测的一次性容器（摄取已结束、只剩计费）更宽松，但同机其他项目的负载会变。
- **需要谁拍板**：无（Kelvin 2026-10-30 已选定这一批）。
- **如果假设错误，具体后果**：
  0. `IS_USED_LOCK(name) = CONNECTION_ID()` 在持有者连接上返回 1、名额被别的连接持有或空闲时返回 0 或 NULL；被 `KILL` 的连接上执行任何语句都会报错。依据：MySQL 文档对 `IS_USED_LOCK` 与 `CONNECTION_ID` 的定义；§7「名额连接中途断开」在 CI 的 MySQL 上实测。
  0. 名额不起作用 → 退回 v1 的行为（并行数没有硬上界），正确性不受影响；§7 的名额用例会在 CI 上失败，实现不会合并。
  1. 两个扫描落到同一子进程依次执行 → 吞吐不变，但正确性不受影响（重叠运行本来就安全）；重跑剖析会直接看出来，届时设计版本 +1 改派发方式。
  2. 提升小于预期 → 只影响收益，不影响正确性；结果写进 perf-baseline，按 10.6「下一步」继续看 CPU 配额与计价解析。

## 11. 审查与版本绑定

审查方要回答的五个问题常驻在 [scripts/review_checklist.md](../../scripts/review_checklist.md) 的「设计审查」一节，此处不重复。

**版本绑定规则**（规则的出处是 [WORKFLOW §3](../../docs/WORKFLOW.md)；这里保留一份是因为审查材料里只有本 Issue 正文，两处改一起改）：

- Codex 的批准必须写成 `APPROVED: design v<N>`，`<N>` 取本 Issue 顶部的「设计版本」
- **设计版本一变，之前的 APPROVE 自动作废**，必须重新过闸门
- 什么算「实质修改」：改了契约、表结构、事务边界、状态机、失败语义、不变量控制。改错别字不算
- 实质修改时：把顶部版本 +1，在下方追加一条变更说明，状态退回 `READY_FOR_REVIEW`

**任务拆分**（两个任务，`depends_on` 串起来）：

1. `AIH-TASK-141` 计费扫描并行：`app/core/celery_app.py` 的常量与 Beat 条目、`app/services/usage_billing.py` 的扫描名额与 `parallelism` 参数、`app/tasks/usage_billing.py` 传名额数、`docker-compose.yml` 的默认并发、`tests/backend/test_usage_billing.py`（名额的参数与 SQLite、任务传名额数）、`tests/backend/test_celery.py`、`tests/backend/test_compose.py`、`tests/backend/test_usage_billing_concurrency.py`（§7「两个扫描并行、余额跨零」、七个名额用例与幂等重放）、`docs/runbook.md`、`docs/deployment.md`。
2. `AIH-TASK-142` 压测脚本按新的派发节奏模拟：`scripts/perf_usage.py` 与 `scripts/perf_usage_profile.py` 的派发线程每 10 秒派 `USAGE_BILLING_SWEEP_PARALLELISM` 个扫描（导入同一个常量，不另设参数），worker 子进程调用 `bill_pending_events` 时也传 `parallelism=USAGE_BILLING_SWEEP_PARALLELISM`（与生产任务一致），对应的 `tests/backend/test_perf_usage.py`、`tests/backend/test_perf_usage_profile.py`，`docs/perf-baseline.md` 10.3 与 10.6 的派发描述。只改脚本与文档，不跑压测；在 VPS 上重跑是合并后的运维步骤。

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
| v4 | 2026-10-30 | §2：`GET_LOCK` 与名额确认两条语句各包成模块级私有函数 `_get_lock` / `_holds_lock`（原样返回 1 / 0 / None，失败时抛异常）；§7 加「`GET_LOCK` 返回 NULL 或出错」（两种）与「确认时返回 0、NULL 或出错」（三种）两个参数化用例；§2 写明 `GET_LOCK` 返回 NULL 或出错时不再试后面的名额；「幂等重放」写成余额跨零用例的末尾一步 | Codex v3 REQUEST_CHANGES：`GET_LOCK` 返回 NULL / 出错与 `IS_USED_LOCK` 返回 0 / NULL 两条失败分支没有测试；登记前预审：NULL 的处理在 §2 与 §5 不一致、幂等重放的场景没定 |
| v3 | 2026-10-30 | 扫描在每次认领与每次处理之前，在名额连接上用 `IS_USED_LOCK(name) = CONNECTION_ID()` 确认名额仍在，丢了（含语句失败）就停下，本批剩余事件交给卡住回收；§4 写明上界（N + 刚丢名额、正在做完手上一个事件的扫描）；§5 加「名额连接中途断开」；§7 加「名额连接中途断开」「释放失败就丢弃连接」两个用例 | Codex v2 REQUEST_CHANGES：名额连接独自断开时 MySQL 释放名额，旧扫描仍在另一条连接上继续计费，下一轮扫描抢到名额，并行数突破上限；连接失效与释放失败两条路径没有测试 |
| v2 | 2026-10-30 | 新增「扫描名额」：每轮扫描先用带库名的 MySQL 命名锁（`GET_LOCK`，超时 0）抢 N 个名额之一，抢不到立即返回 0，结束时释放、释放失败就丢弃连接；`bill_pending_events` 加 `parallelism` 参数（`None` 不限），任务传常量；§2、§4、§5、§7、§8、§9、§10、§11 相应补充 | Codex v1 REQUEST_CHANGES：预算只在每批之前检查、单条可等锁 10 秒、`expires` 不拦运行中的任务，扫描跨周期后 Beat 再派的扫描会占满全部子进程，v1「最多占 2 个子进程」没有保证 |
| v1 | 2026-10-30 | 初稿 | — |

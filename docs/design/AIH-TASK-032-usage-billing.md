# AIH-TASK-032 设计：异步计费 worker：扣费、负余额、停机与恢复（已批准 v2）

> **来源**：设计闸门 Issue #181。本文件是 `APPROVED: design v2` 那一版正文的**逐字副本**，审查者为 Codex。
> 放进仓库，是因为 OpenClaw Worker 在沙箱里不联网、读不到 GitHub Issue。
> **实现以本文件为准**；与 Issue 不一致时，以 Issue #181 上被批准的 v2 为准。设计要改，就回到 Issue 升版本、重新过闸门，不要直接改本文件。
> 正文里的「T-H」即 [PHASE-2-plan.md](PHASE-2-plan.md) 的占位名；写「编号登记时分配」的实现任务，登记为 `AIH-TASK-032`。正文提到的其他占位名（T-A…T-L）对应的登记编号见 `docs/TODO.md` 的 Phase 2 一节。
> 与 Issue 正文的唯一差别：一处链接目标由 GitHub 网页上的 `../blob/main/…` 改成仓库内的相对路径，链接文字与其余正文逐字相同。

---

## 0. 准入判定

| 任务涉及 | 要填的范围 |
| --- | --- |
| 钱包 / 账本 / 定价 / 汇率 / 支付 / 幂等 / 状态机 | **全部章节** |
| 用量摄取 / 认证与会话（集成侧与平台侧）/ Webhook | §1–§7（**§6 不可省**） |
| 前端 / 文档 / CI / 脚本 | 不走本闸门 |
| 既不碰钱、也不碰用量摄取 / 认证与会话 / Webhook 的后端改动 | 不走本闸门 |

本任务档位：`全部`。它扣客户的钱：用量事件的状态机、钱包扣费、负余额与停机（§7、§25、§82、§83），并且是 ADR-0011 指定的「最热租户压测 + 方案 C 判据」的设计闸门。

---

状态：`READY_FOR_REVIEW`
负责人：Claude
**设计版本**：`v2`（v1 被 Codex 判 REQUEST_CHANGES：用长事务行锁替代了 spec §82 / §83 / §110 的「认领为 PROCESSING + 卡住回收」。v2 按 spec 字面实现认领、租约、防护令牌与卡住回收，见 §12）
对应需求：spec §7（第 6–11 条）、§8、§20、§25、§46、§78、§80、§81、§82、§83、§84、§110、§113、§114、§117、§119、§120；`REQ-INGEST-002`、`REQ-IDEMP-001`、`REQ-FIN-001`、`REQ-FIN-002`、`REQ-TXN-001`、`REQ-PRICE-001`；ADR-0003（入账期间看 `processed_at`）、ADR-0010（停机规则 `balance <= 0` 不变）、ADR-0011（每事件一行账本；压测与方案 C 判据）
目标 PR：待开（批准后在 `.platform/tasks.yaml` 登记实现任务，编号登记时分配）
Phase 2 计划：`docs/design/PHASE-2-plan.md` 的 T-H。依赖已批准的设计：AIH-TASK-025（目录与 `resolve_model`，v4）、T-B #177（价格，v3）、T-D #178（规则，v3）、T-G #179（计价引擎，v1）；以及审查中的 T-C（汇率与告警注册表）、T-E #176（`usage_events`）。本设计只引用它们已写明的契约，它们若改契约本设计随之升版本。

## 1. 目标与边界

- **要解决的问题**：T-E 只把事件落成 `RECEIVED`。spec §82 要求 worker 把它计价、扣费、写快照，§78 / ADR-0011 要求每个事件恰好一行 `AI_USAGE` 账本，§83 要求任何事件都不能被静默丢弃、错误事件对管理员可见，§7 / ADR-0010 要求余额 ≤ 0 即停机且跃迁与扣费原子提交，REQ-INGEST-002 要求队列丢了也能从数据库状态恢复。
- **可观察的完成标准**：
  1. `RECEIVED` 的事件在 §119 的目标内（p95 ≤ 60 秒）变成 `PROCESSED`，带齐 §14 / §79 的快照，钱包扣掉 `billable_cost`，账本多一行 `AI_USAGE`（金额为负，`reference = USAGE_EVENT / event_id`）。
  2. 同一事件无论被处理几次、几个 worker 同时处理，至多一行账本、至多扣一次（INV-2 / INV-11）。
  3. 无法计价的事件停在 `MODEL_UNKNOWN` / `PRICING_ERROR` / `FX_RATE_ERROR`（带细分码），不扣费；管理员修好目录、价格、规则或汇率后可以把它重新入队。
  4. 进程崩溃、Redis / Celery 丢失、Beat 停摆后恢复：没有事件丢失，没有事件被扣两次；崩溃时留在 `PROCESSING` 的事件在租约过期后被回收（§110「stale processing recovery」），可观测、可告警。
  5. 余额从正变为 ≤ 0 的那一笔扣费，与计费状态跃迁、`status_version` +1、审计、outbox 同一事务（沿用 `post_transaction`）。
  6. 七个告警维度接进 T-C 的告警接口。
  7. ADR-0011 的压测方案与方案 C 的切换判据写定（本设计 §2「最热租户」），由 T-I 执行。
- **明确不做什么**：
  - 计价算法 —— T-G；本任务只调用
  - reprocess / rebill（改变**已计费**事件的金额）—— Phase 8（§19、§62）；本任务的「重新入队」只作用于**从未产生财务效果**的错误事件
  - 方案 C（同租户批事务）的实现 —— 只在 T-I 压测不达标时另开任务、另过闸门
  - 管理端用量查询接口 —— T-J（本任务只加重新入队的两个写接口）
  - 前端 —— T-L
  - `reconciled_provider_cost_myr` —— Phase 8（供应商账单对账）
- **现有行为与问题证据**：`app/repositories/wallet.py` 的 `post_transaction` 已处理钱包行锁、账本唯一效果（`(reference_type, reference_id)` 冲突时 `replayed` / `LedgerConflict`）、计费状态跃迁与审计 / outbox；账本 CHECK 要求 `AI_USAGE` 金额 < 0（迁移 0006 `ck_wallet_transactions_sign`）。没有任何调用它的用量路径。

## 2. 设计概要

```text
Beat「usage-billing-sweep」每 10 秒 → 任务 bill_pending_events()：
  循环（直到没有可认领的事件，或本轮用满 8 秒预算）：
    认领事务（短）：
      Ⓐ SELECT id FROM usage_events
           WHERE status IN ('RECEIVED','FAILED_RETRYABLE') AND (next_attempt_at IS NULL OR next_attempt_at <= now_c)
             AND occurred_at <= now_c
           ORDER BY id LIMIT 20 FOR UPDATE SKIP LOCKED              → 没有：结束本轮
      Ⓑ UPDATE 这些行 SET status = 'PROCESSING', claim_token = <新 uuid4>, claimed_at = now_c,
           lease_expires_at = now_c + BILLING_USAGE_LEASE_SECONDS（默认 120）, attempt_count = attempt_count + 1
      Ⓒ 提交（PROCESSING 从此对外可见，§82「safely claim RECEIVED → PROCESSING」）
    对每个认领到的事件，一个处理事务：
      ① SELECT … FROM usage_events WHERE id = ? FOR UPDATE
         状态不是 PROCESSING 或 claim_token 不是自己的（已被回收、另有人处理）→ 回滚，放弃这一条（防护令牌）
      ② 读租户（account_status）
      ③ resolve_model(provider_code_raw, model_code_raw, occurred_at)   → 供应商行 FOR SHARE（025）
      ④ fx_rate_locks FOR SHARE（T-C）；pricing_rule_locks FOR SHARE（T-D）
      ⑤ 持锁后取 now_r；occurred_at > now_r（防御性：认领时已过滤，不应发生）→ 改回 RECEIVED、attempt_count − 1、清认领 → 提交
      ⑥ resolve_charge_inputs(locking=True)（T-G）→ 错误：写状态与细分码、清认领 → 提交 → 下一条
      ⑦ price_event（T-G）→ ChargeResult
      ⑧ billable_cost > 0：post_transaction(AI_USAGE, -billable_cost, USAGE_EVENT, event_id)
           （钱包 → 租户锁；跃迁、status_version、审计、outbox、低余额事件都在里面）
      ⑨ 写快照、status = PROCESSED、processed_at、清认领 → 提交
    意外异常：回滚；另开短事务 —— 锁事件行，仍是 PROCESSING 且令牌是自己的才写：
      FAILED_RETRYABLE + 退避（attempt_count 已在认领时 +1）；达到上限 → FAILED_FINAL；清认领

Beat「usage-stale-recovery」每 60 秒 → 任务 recover_stale_processing()（§110）：
  SELECT … WHERE status = 'PROCESSING' AND lease_expires_at < now FOR UPDATE SKIP LOCKED LIMIT 500
  → attempt_count 已达上限：FAILED_FINAL（error_code = STALE_PROCESSING_EXHAUSTED）
    否则：FAILED_RETRYABLE（error_code = STALE_PROCESSING，next_attempt_at = now）
  → 清认领 → 提交；每回收一条记一行 WARNING 日志（event_id、claimed_at、attempt_count）
```

### 认领、租约与防护令牌（§82、§83、§110）

- **认领是一次提交**：事件从 `RECEIVED` / 到期的 `FAILED_RETRYABLE` 变成 `PROCESSING`，带 `claim_token`（每次认领一个新 uuid4）与 `lease_expires_at`。认领事务很短（只改状态列），`SKIP LOCKED` 让多个 worker 并行认领互不阻塞、不会认领同一行。
- **防护令牌**：处理事务第一步锁住事件行并核对 `status = PROCESSING AND claim_token = 自己的令牌`。租约过期后被回收、又被别的 worker 认领的事件，原处理者会在这一步看到令牌不同而放弃 —— 同一时刻至多一个有效的处理者。账本的 `(USAGE_EVENT, event_id)` 唯一约束是最后一道：即使防护失效，也至多一次财务效果（`post_transaction` 返回 `replayed` 或抛 `LedgerConflict`，见状态表）。
- **卡住回收**：崩溃在「认领已提交、处理未提交」之间的事件停在 `PROCESSING`。回收任务只看 `lease_expires_at < now` 的行，且用 `FOR UPDATE SKIP LOCKED` —— **仍被某个处理事务持有行锁的事件不会被回收**（即使它已超过租约，例如在等钱包锁），所以回收不会与正在进行的处理竞争；真正的孤儿（持有者已崩溃、连接已断、行锁已释放）才会被回收。
- **可观测**：`PROCESSING` 的数量、最早的 `claimed_at`、租约已过期的数量都进告警维度 `usage_processing_backlog`（见下）。
- 租约 120 秒远大于一次处理的正常耗时（毫秒级）；处理事务里没有任何外部调用（计价只读数据库）。
- `occurred_at` 的时间过滤放在认领时（`occurred_at <= now_c`），处理时持锁后再核一次（防御性，见 ⑤）。

### 锁顺序（全局唯一，写进 `docs/database-schema.md`）

**事件行（X）→ 供应商行（S）→ `fx_rate_locks`（S）→ `pricing_rule_locks`（S）→ 钱包（X）→ 租户（X）**

- 事件行只有本任务（认领、处理、回收）与「重新入队」接口会锁，都只锁事件行本身、且都先于其他锁，不会反向。
- 三把共享锁的发布方（025 改映射 / T-B 价格发布锁供应商行；T-C、T-D 发布锁各自的单行表）各自只拿一把排他锁，不持有其他锁，不形成环。
- 钱包 → 租户是 `post_transaction` 既有的顺序；账户状态变更（AIH-TASK-020）只锁租户，不锁钱包，不形成环。
- ② 读租户的 `account_status` 是普通读（不加锁）；它只用于 `CLOSED` 判断，而 `CLOSED` 是终态，读到旧值的唯一后果是在关户后的极短窗口里多记一笔 —— 关户任务的前置条件（「无非终态的用量事件」）会挡住这种情况。
- 本任务的事务把 `innodb_lock_wait_timeout` 设为会话级 10 秒（默认 50 秒）：发布方持有排他锁时，计费等待超过 10 秒就回滚并按「可重试」记一次（下一轮再来），不长时间占着 worker。

### 时间与 `occurred_at`

⑤ 是 025 / T-B / T-C / T-D 共同依赖的契约：**三把共享锁都拿到之后**才取 `now_r`，只计价 `occurred_at ≤ now_r` 的事件。认领时已经只取 `occurred_at ≤ now_c` 的事件，而 `now_r` 在 `now_c` 之后取（同一时钟），所以正常情况下 ⑤ 恒成立；它仍按契约在持锁后核对一次。于是任何发布方在锁后取的边界 `t` 都晚于本事务里被计价事件的 `occurred_at`（或者发布先完成、本事务读到新表），同一个 `occurred_at` 的解析结果永不改变。`occurred_at` 晚于当前时间的事件（T-E 允许最多 300 秒的未来偏差）不会被认领，到时再说。

③ 之后若模型未知且供应商行本身不存在（供应商代码未知），则没有供应商锁可拿；此时直接判 `MODEL_UNKNOWN`（不依赖任何版本），⑤ 照样在 FX 与规则锁之后执行，结论不受影响。

### 状态机（§83）

| 当前 | 触发 | 新状态 | 财务效果 | 说明 |
| --- | --- | --- | --- | --- |
| `RECEIVED` / `FAILED_RETRYABLE`（到期），且 `occurred_at ≤ now` | 认领 | `PROCESSING`（令牌、租约、`attempt_count` +1） | 无 | 认领事务提交 |
| `PROCESSING`（令牌匹配） | 计价成功，`billable_cost > 0` | `PROCESSED` | 一行 `AI_USAGE`，钱包扣减 | 快照齐全 |
| `PROCESSING`（令牌匹配） | 计价成功，`billable_cost = 0` | `PROCESSED` | **无**（账本 CHECK 要求 `AI_USAGE < 0`，T-G 契约 ③） | 快照齐全，`wallet_transaction_id` 为空 |
| 同上 | 模型未知 | `MODEL_UNKNOWN` | 无 | §84 |
| 同上 | 无价格 / 价格缺计量类型 / 无规则 / 规则缺计量类型 | `PRICING_ERROR`（`error_code` 为 T-G 的细分码） | 无 | §16 |
| 同上 | 非 MYR 且无汇率 | `FX_RATE_ERROR` | 无 | §17.1 |
| 同上 | 计算溢出（T-G `ChargeOverflow`） | `FAILED_FINAL`（`AMOUNT_OUT_OF_RANGE`） | 无 | |
| 同上 | 租户 `CLOSED` | `FAILED_FINAL`（`ACCOUNT_CLOSED`） | 无 | AIH-TASK-020 的契约：关户后「不入账，进人工复核」；`CLOSED` 目前不会出现，守卫先写好 |
| 同上 | `post_transaction` 抛 `LedgerConflict`（同一 `event_id` 已有金额不同的账本行 —— 只可能是缺陷） | `FAILED_FINAL`（`LEDGER_CONFLICT`） | 无（原账本行不动） | 告警 |
| 同上 | `post_transaction` 返回 `replayed = True`（已有同额账本行 —— 只可能是前一次提交了账本却没提交事件，而二者同事务，所以同样是缺陷信号） | `PROCESSED`，`wallet_transaction_id` 指向已有行 | 无新增 | 记 `WARNING` 日志 |
| `PROCESSING`（令牌匹配） | 意外异常 / 锁超时 / 死锁 | `FAILED_RETRYABLE`（另一短事务写，令牌仍匹配才写），`next_attempt_at = now + min(2^n, 300) 秒` | 无（主事务已回滚） | `attempt_count` 已在认领时 +1 |
| `PROCESSING`（令牌不匹配） | 处理事务开始时核对 | 不变 | 无 | 原处理者放弃（已被回收或另有人处理） |
| `PROCESSING`，租约已过期，且行未被锁 | 卡住回收 | `FAILED_RETRYABLE`（`STALE_PROCESSING`，立即可再认领）；达上限 → `FAILED_FINAL`（`STALE_PROCESSING_EXHAUSTED`） | 无 | §110 |
| `PROCESSING` → 失败记录时 | `attempt_count` 已达 `BILLING_USAGE_MAX_ATTEMPTS`（默认 10） | `FAILED_FINAL`（保留最后的 `error_code`） | 无 | |
| `MODEL_UNKNOWN` / `PRICING_ERROR` / `FX_RATE_ERROR` / `FAILED_FINAL` | 管理员重新入队 | `RECEIVED`（清空 `error_code`、`attempt_count`、`next_attempt_at`） | 无 | 见「重新入队」 |

表中「同上」各行（模型未知、价格、汇率、溢出、关户、账本冲突、replayed）的当前状态都是 `PROCESSING`（令牌匹配）；所有离开 `PROCESSING` 的跃迁都同时清空 `claim_token`、`claimed_at`、`lease_expires_at`。
| `PROCESSED` | 任何 | — | — | **终态，数据库触发器禁止任何修改** |

`DISABLED` 的租户照常计费（AIH-TASK-020 契约：§112.1 在途的合法事件要处理完）。

### 数据库（迁移序号取实现时的下一个）

`usage_events` 加列（T-E 建的表；此时表里只有尚未计费的事件）。`provider_id` / `model_id` 已由 T-E 建好（可空、复合外键），本任务在计费事务里写入解析结果（模型未知时保持为空）：

| 列 | 类型 | 说明 |
| --- | --- | --- |
| `provider_price_version_id` | FK NULL → `provider_price_versions.id` | |
| `pricing_rule_id` | FK NULL → `pricing_rules.id` | |
| `fx_rate_version_id` | FK NULL → `fx_rate_versions.id` | MYR 原币时为空 |
| `provider_source_currency` | CHAR(3) NULL | |
| `provider_source_cost` | DECIMAL(20,8) NULL | |
| `fx_rate_applied` | DECIMAL(24,10) NULL | MYR 原币时为空 |
| `estimated_provider_cost_myr` | DECIMAL(20,8) NULL | |
| `billable_cost` | DECIMAL(20,8) NULL | 含税（ADR-0008） |
| `wallet_transaction_id` | FK NULL → `wallet_transactions.id` | 计费额为 0 时为空 |
| `attempt_count` | INT NOT NULL DEFAULT 0 | 认领时 +1 |
| `next_attempt_at` | DATETIME NULL | |
| `claim_token` | CHAR(36) NULL | 当前认领的防护令牌 |
| `claimed_at` / `lease_expires_at` | DATETIME NULL | |

约束：

- CHECK（已处理 ⇒ 快照齐全）：`status <> 'PROCESSED' OR (provider_id, model_id, provider_price_version_id, pricing_rule_id, provider_source_currency, provider_source_cost, estimated_provider_cost_myr, billable_cost, processed_at 全部 IS NOT NULL AND ((provider_source_currency = 'MYR' AND fx_rate_version_id IS NULL AND fx_rate_applied IS NULL) OR (provider_source_currency <> 'MYR' AND fx_rate_version_id IS NOT NULL AND fx_rate_applied IS NOT NULL)))`（实现时逐列写 `IS NOT NULL`，不用行构造器）。
- CHECK：`status <> 'PROCESSED' OR billable_cost = 0 OR wallet_transaction_id IS NOT NULL`；`billable_cost IS NULL OR billable_cost >= 0`；成本列同样 ≥ 0。
- CHECK（未处理 ⇒ 没有财务链接）：`status = 'PROCESSED' OR wallet_transaction_id IS NULL`。
- CHECK（认领字段与状态一致）：`(status = 'PROCESSING') = (claim_token IS NOT NULL)`，且 `status <> 'PROCESSING' OR (claimed_at IS NOT NULL AND lease_expires_at IS NOT NULL AND claimed_at < lease_expires_at)`；非 `PROCESSING` 时三列都为空（实现时逐列写，不用布尔等式比较以免 `NULL` 语义歧义）。
- UNIQUE `wallet_transaction_id`：一行账本至多对应一个事件（账本本身已对 `(USAGE_EVENT, event_id)` 唯一）。
- 触发器 BEFORE UPDATE：`OLD.status = 'PROCESSED'` → 任何修改都 SIGNAL（INV-6 / INV-11：已计费事件的快照与状态永不改变；Phase 8 的 rebill 另写调整行，不改原事件）。
- 触发器 BEFORE UPDATE：`NEW.status = 'PROCESSED' AND NEW.billable_cost > 0` 时，校验 `wallet_transaction_id` 指向的账本行 `reference_type = 'USAGE_EVENT' AND reference_id = NEW.event_id AND amount = -NEW.billable_cost`，否则 SIGNAL（事件与账本的对应由数据库保证）。
- 触发器 BEFORE UPDATE：`OLD.status IN ('MODEL_UNKNOWN','PRICING_ERROR','FX_RATE_ERROR','FAILED_FINAL') AND NEW.status = 'RECEIVED'` 时，校验账本里**不存在** `(USAGE_EVENT, NEW.event_id)` 的行，否则 SIGNAL（重新入队只对从未产生财务效果的事件）。
- 索引：`(status, next_attempt_at, id)`（认领）、`(status, lease_expires_at)`（卡住回收）；T-E 的 `(status, occurred_at)`、`provider_id`、`model_id` 已建。

### 重新入队（§84「reprocess event」的无财务效果版本）

| 方法与路径 | 作用 |
| --- | --- |
| `POST /api/v1/admin/usage-events/{usage_event_id}/requeue` | 单个：必填 `reason` |
| `POST /api/v1/admin/usage-events/requeue` | 批量：`status`（四个错误状态之一，必填）、可选 `error_code`、`customer_id`、`provider`、`model`、`occurred_from` / `occurred_to`，上限 1000 条；必填 `reason` |

- 只有 ADMIN，第一条语句 `require_admin`，加进路由枚举；路径 id 是事件 `public_id`。
- 每个事件：锁事件行 → 状态必须是四个错误状态之一（否则单个 409 `USAGE_EVENT_NOT_REQUEUABLE`，批量则跳过并计数）→ 改回 `RECEIVED`、清错误与尝试计数 → 审计 `USAGE_EVENT_REQUEUE`（`reason`、原状态与错误码）→ 提交。批量逐条事务，响应给出重新入队与跳过的数量。
- 路径名用 `requeue` 而不是 §89 的 `reprocess`：后者在 Phase 8 专指「对已计费事件重算并生成调整行」，两件事的财务含义完全不同，不能共用一个名字。
- 不自动重新入队（例如发布价格后自动把 `PRICING_ERROR` 全部放回）：错误原因多样，自动放回可能反复失败；V1 由管理员看告警、修配置、再重新入队。在 `docs/runbook.md` 写明流程。

### 告警维度（注册到 T-C 的 `app/services/alerts.py`）

| 维度 | 何时告警 | 分级 |
| --- | --- | --- |
| `usage_pricing_error` | 存在 `PRICING_ERROR` 事件 | P2 |
| `usage_fx_rate_error` | 存在 `FX_RATE_ERROR` 事件 | P2 |
| `usage_model_unknown` | 存在 `MODEL_UNKNOWN` 事件 | P2 |
| `usage_failed` | 存在 `FAILED_FINAL` 事件；或有 `FAILED_RETRYABLE` 事件已重试 ≥ 3 次 | P1（`LEDGER_CONFLICT` / `AMOUNT_OUT_OF_RANGE`）/ P2 |
| `usage_processing_backlog` | 最早的可处理事件（`RECEIVED`，或到期的 `FAILED_RETRYABLE`，且 `occurred_at ≤ now`）等待超过 5 分钟（§119 p99）；或存在租约已过期仍未回收的 `PROCESSING`（回收任务本身停了）；摘要含 `PROCESSING` 数、最早 `claimed_at`、过期数 | P2；等待超过 30 分钟或过期 `PROCESSING` 超过 10 分钟为 P1 |
| `wallet_negative_balance` | 存在余额 < 0 的钱包（§120） | P2 |
| `outbox_backlog` | `domain_outbox` 里有处理器的事件类型存在超过 10 分钟仍 `PENDING` 的行（§120；没有处理器的类型按设计持久等待，不计入） | P2 |

每行摘要只含计数、最早时刻与最多 3 个细分码，不含租户与金额。Healthchecks 用量：8（现有）+ 2（T-C）+ 1（T-E）+ 7 = 18，免费档上限 20。

### 最热租户：压测方案与方案 C 的判据（ADR-0011 要求）

**方案**（由 T-I 在生产同规格环境执行，本设计定义场景与通过线）：

| 场景 | 负载 | 通过线 |
| --- | --- | --- |
| S1 突发 | 单个租户，100 事件 / 秒持续 5 分钟（§119 突发），同时 3 个其他租户各 5 事件 / 秒 | 事件到扣费延迟 p95 ≤ 60 秒、p99 ≤ 5 分钟（§119）；报告 p50 / p95 / p99 |
| S2 回补 | 单个租户预先积压 30 000 条 `RECEIVED` | 清空速率 ≥ 500 事件 / 秒（§119「≥ 峰值 5 倍」） |
| S3 崩溃与重试 | S1 进行中随机杀掉 worker 进程 3 次、注入 1% 的意外异常 | 结束后：`PROCESSED` 事件数 = 账本 `AI_USAGE` 行数（不含 0 元事件）；每个租户 `期初余额 − Σ billable_cost = 期末余额`；`verify_wallet` 全部通过；无 `LEDGER_CONFLICT` |
| S4 与发布并发 | S1 进行中每 10 秒发布一次价格 / 规则 / 汇率 | S1 的通过线仍满足；发布请求 p95 ≤ 5 秒；抽样事件用快照重算（T-G `reprice_from_snapshot`）与快照一致 |

**方案 C 的切换判据**：S1 的 p95 或 p99 不达标、或 S2 的清空速率 < 500 / 秒，**且**瓶颈经测量是同一钱包行锁上的串行提交（最热租户的单事件事务平均持锁时间 × 目标速率 > 1 秒 / 秒）—— 则另开任务实现方案 C（同租户 N 个事件一个事务，每事件仍一行账本），另过设计闸门。若瓶颈在别处（例如数据库整体吞吐、worker 数量），先调那一处，不切方案 C。

### 事务边界

| 路径 | 事务 |
| --- | --- |
| 认领 | 短事务：`SKIP LOCKED` 选行 → 改为 `PROCESSING` → 提交 |
| 处理一个事件 | 一个事务：①–⑨ 全部（含 `post_transaction` 内的钱包、账本、跃迁、审计、outbox） |
| 记一次失败 | 主事务回滚后另开短事务：锁事件行 → 仍是 `PROCESSING` 且令牌匹配才写 → 提交 |
| 卡住回收 | 短事务：`SKIP LOCKED` 选租约过期的 `PROCESSING` → 改状态、清认领 → 提交 |
| 重新入队 | 每个事件一个事务 |
| 告警维度 | 只读 |

### 外部系统与异步边界

- Celery Beat 两个条目：`usage-billing-sweep` 每 10 秒（`expires = 9 秒`）、`usage-stale-recovery` 每 60 秒（`expires = 54 秒`）；重叠运行是安全的（`SKIP LOCKED` + 防护令牌）。
- 队列丢失、Beat 停摆：数据库里的 `RECEIVED` / `FAILED_RETRYABLE` / 过期的 `PROCESSING` 就是待办清单（REQ-INGEST-002 / INV-14），恢复后第一轮扫描与回收继续；停摆期间 `usage_processing_backlog` 告警。
- 摄取端（T-E）不入队；本任务不增加任何从摄取到队列的耦合。
- worker 并发度沿用现有 celery-worker 配置；多个 worker 靠 `SKIP LOCKED` 分摊。

### 时间语义

- `occurred_at`：选版本与规则（下层契约）。
- `processed_at`：本事务的服务端时刻（截到整秒）；ADR-0003：入账期间按 `processed_at`，晚到的事件记入处理时的期间。
- 账本行的时间由 `post_transaction` 按既有规则写。

### 金额精度

- 计算全部在 T-G；本任务把 `ChargeResult` 的存储值原样写入，账本金额 = `-billable_cost`（不再舍入）。
- 钱包余额由账本插入触发器推进（AIH-TASK-005），本任务不直接改余额（INV-4）。

### 审计

| `action` | 什么时候 | 写入方 |
| --- | --- | --- |
| `TENANT_BILLING_STATUS_CHANGED` | 扣费使余额跨过 0 | `post_transaction`（既有，操作者 SYSTEM） |
| `USAGE_EVENT_REQUEUE` | 重新入队 | 本任务；§66 之外，按先例补上并记进 `docs/TODO.md` |

逐个事件的计费不写审计：事件行 + 账本行就是审计链（§139「从 AI 请求到钱包交易与余额」），每秒上百条的审计行没有增量信息。

## 3. 不变量影响矩阵

| 不变量 | 是否触碰 | 可能怎样被破坏 | 设计控制 | 验证方式 |
| --- | --- | --- | --- | --- |
| INV-1 中心故障不中断 AI | 否（间接） | — | 计费异步，与 AI 调用无关 | — |
| INV-2 事件不重复扣费 | **是** | 两个 worker 同时处理；崩溃后重复处理；重新入队已计费的事件 | 行锁 + `SKIP LOCKED`；同一事务；账本 `(USAGE_EVENT, event_id)` 唯一；`PROCESSED` 不可改；重新入队触发器校验无账本行 | 并发、崩溃、S3 压测 |
| INV-3 | 否 | — | — | — |
| INV-4 余额只经账本变动 | **是** | worker 直接改余额 | 只调用 `post_transaction` | 静态检查（本任务模块不导入钱包更新函数以外的写路径）；余额核对 |
| INV-5 历史账本不可变 | **是** | — | 账本触发器（既有）；本任务只插入 | 既有用例 |
| INV-6 事件保留版本引用 | **是** | 快照缺失；快照被改；发布与计费交错 | 已处理 ⇒ 快照齐全的 CHECK；`PROCESSED` 不可改的触发器；锁顺序与 ⑤ | CHECK 与触发器用例；S4 |
| INV-7 客户不可见成本毛利 | 否（本任务无客户接口） | — | — | — |
| INV-8 租户不可互访 | **是** | 事件扣到别的租户的钱包 | 钱包按事件的 `tenant_id` 取（T-E 由数据库保证它就是凭据的租户）；规则按 `tenant_id` 解析 | 两租户用例 |
| INV-9 | 否 | — | — | — |
| INV-10 金额用 Decimal | **是** | 写入时再次舍入或经 `float` | 原样写 `ChargeResult` | 用例 |
| INV-11 event_id 至多一次财务效果 | **是** | 同 INV-2 | 同 INV-2；事件与账本对应的触发器 | 同上 |
| INV-12 | 否 | — | — | — |
| INV-13 状态与事件原子提交 | **是** | 扣了钱没改事件状态；跃迁了没写 outbox | 一个事务；`post_transaction` 内既有的原子性 | 各步抛错的回滚用例（SQLite 与 MySQL） |
| INV-14 队列丢失不毁持久工作 | **是** | 待办只在队列里 | 数据库扫描；无租约状态 | 停掉 Beat 再恢复的用例 |

## 4. 状态与并发

状态表见 §2。

- **串行化**：事件行锁（同一事件）；钱包行锁（同一租户的扣费，§81）；三把共享锁（与发布方互斥）。
- **数据库保证的唯一性**：账本 `(reference_type, reference_id)`（既有）；`usage_events.wallet_transaction_id`。
- **幂等键**：`event_id`（账本引用）。
- **相同 ID、不同载荷**：`LedgerConflict` → `FAILED_FINAL` + P1 告警（只可能是缺陷）。
- **重试层**：意外异常按退避重试，上限后 `FAILED_FINAL`；计价错误不自动重试（等管理员）。
- **原子提交**：事件状态与快照、账本行、钱包、计费跃迁、审计、outbox。

## 5. 失败与恢复矩阵

| 失败点 | 对外结果 | 数据最终状态 | 是否可重试 | 恢复来源 | 告警 |
| --- | --- | --- | --- | --- | --- |
| worker 在认领提交之前崩溃 | — | 回滚，事件仍 `RECEIVED` | 自动（下一轮） | 数据库 | 持续则 backlog |
| worker 在认领提交之后、处理提交之前崩溃 | — | 处理事务回滚；事件停在 `PROCESSING`，行锁随连接释放 | 租约过期后由卡住回收改为 `FAILED_RETRYABLE`，再被认领 | 回收任务 | 过期 `PROCESSING` 进 backlog 维度 |
| 租约过期而原处理者仍在处理（持有行锁） | — | 回收跳过该行（`SKIP LOCKED`）；原处理者正常提交 | — | — | — |
| 原处理者在回收之后才开始处理事务 | — | 令牌不匹配，放弃 | — | — | — |
| 卡住回收任务停摆 | — | 过期 `PROCESSING` 堆积 | 恢复后自动 | — | backlog 维度 P1 |
| 锁等待超时 / 死锁 | — | 回滚；`FAILED_RETRYABLE` + 退避 | 自动 | — | 重试 ≥ 3 次 → `usage_failed` |
| 模型未知 / 价格 / 规则 / 汇率缺失 | — | 对应错误状态，不扣费 | 管理员修好后重新入队 | 管理员 | 对应维度 |
| 计算溢出 | — | `FAILED_FINAL` | 否 | 人工 | `usage_failed` P1 |
| 账本冲突（缺陷） | — | `FAILED_FINAL`，原账本不动 | 否 | 人工排查 | `usage_failed` P1 |
| 余额跌破 0 | — | 扣费照常（§7 第 7 条接受透支），停机跃迁同事务 | — | 充值 / 调账 | `wallet_negative_balance` |
| 租户已关户 | — | `FAILED_FINAL`（`ACCOUNT_CLOSED`） | 否 | 人工复核 | `usage_failed` |
| Redis / Celery / Beat 丢失 | — | 事件留在数据库 | 恢复后自动 | 数据库扫描 | backlog |
| 数据库不可用 | — | 无法处理 | 恢复后自动 | — | readyz / services |
| 记失败的短事务也失败 | — | 事件停在 `PROCESSING` | 租约过期后由卡住回收处理 | 回收任务 | 日志；backlog |
| 重新入队已计费的事件 | 409 / 触发器拒绝 | 不变 | 否 | — | 无 |

## 6. 数据与安全边界

- **租户过滤**：事件的租户来自 T-E（由数据库绑定到凭据）；钱包与规则都按它取。
- **鉴权主体**：worker 是系统（审计操作者 SYSTEM，沿用 `post_transaction`）；重新入队只有 ADMIN。
- **禁止返回的字段**：重新入队的响应只含数量与事件 `public_id`，不含成本。
- **日志 / 审计 / 异常**：日志绑定 `tenant_id`、`project_id`、`event_id`（§94），不打印金额明细与载荷；异常只记类型与错误码。
- **密钥 / prompt**：不涉及。
- **保留与删除**：不删除任何行。

## 7. 测试证据计划

| 风险/需求 | 测试层级 | 场景 | 预期结果 |
| --- | --- | --- | --- |
| 正常路径（§114） | integration | LLM_TOKEN、MARKUP、USD | `PROCESSED`，快照与 T-G 结果一致，一行账本（金额 = −计费额），余额扣减 |
| FIXED_RATE、MYR 原币、非 token | integration | 各一 | 同上；MYR 时 FX 两列为空 |
| 0 元事件 | integration | 全部 token 为 0 | `PROCESSED`，无账本行，`wallet_transaction_id` 为空 |
| 错误状态（§113、§114） | integration | 模型未知；无价格；价格缺计量类型；无规则；规则缺计量类型；无汇率 | 对应状态与细分码；余额与账本不变 |
| 重复处理（§114 Duplicate） | integration | 两个 worker 同时认领（两个连接）；处理后再强行改回 `RECEIVED`（被触发器拒绝） | 同一事件只被一方认领；一行账本；触发器拒绝 |
| 认领 | integration（真 MySQL） | 两个连接并发认领 40 条可处理事件 | 每条恰好被认领一次；认领字段齐全；`attempt_count` +1 |
| 防护令牌 | integration | 认领后手工把令牌改掉（模拟被回收再认领），原处理者开始处理 | 放弃，无账本、无状态变化 |
| 卡住回收（§110） | integration | 认领后不处理、把 `lease_expires_at` 调到过去；运行回收 | `FAILED_RETRYABLE`（`STALE_PROCESSING`），下一轮被重新认领并处理成功，一行账本 |
| 回收不抢活的事件 | integration（真 MySQL） | 一个连接持有事件行锁（模拟处理中）且租约已过期；另一连接运行回收 | 跳过该行；持有者提交后事件为 `PROCESSED` |
| 回收上限 | integration | 反复认领后都不处理，直到 `attempt_count` 达上限 | `FAILED_FINAL`（`STALE_PROCESSING_EXHAUSTED`） |
| 认领字段 CHECK（MySQL） | integration | 直接写入 `PROCESSING` 但缺令牌；非 `PROCESSING` 却有令牌；租约早于认领时刻 | CHECK 拒绝 |
| 并发与崩溃（§117） | integration（真 MySQL） | 多 worker 并发处理同一租户 200 条；在 ⑧ 之后、⑨ 之前注入崩溃（断开连接） | 一一对应；崩溃的那条停在 `PROCESSING`，租约过期后被回收并再次处理成功，仍一行账本 |
| 跨零停机（§7、ADR-0010） | integration | 余额 0.05，计费额 0.10；余额恰好扣到 0；挂起中继续扣 | 跃迁 `SUSPENDED` 与账本同事务、`status_version` +1、审计、outbox；恰好 0 为 `SUSPENDED`；挂起中继续扣费不再跃迁 |
| `DISABLED` 租户 | integration | 停用租户的事件 | 照常计费 |
| `CLOSED` 租户 | integration | 直接把租户改成 `CLOSED` | `FAILED_FINAL`（`ACCOUNT_CLOSED`），无账本 |
| 未来的 `occurred_at` | integration | `occurred_at = now + 60 秒` | 不被认领、尝试次数不变；60 秒后被认领并处理 |
| 锁顺序与发布并发 | integration（真 MySQL） | 计费事务持三把共享锁时，分别发布价格 / 规则 / 汇率 / 改别名 | 发布阻塞到计费提交；计费结果与发布前的表一致 |
| 锁超时 | integration（真 MySQL） | 发布方持排他锁 > 10 秒 | 处理事务回滚、`FAILED_RETRYABLE`、退避 |
| 退避与上限 | unit | 连续意外异常 | `next_attempt_at` 按 2^n 封顶 300 秒；第 10 次后 `FAILED_FINAL` |
| 重新入队 | integration（API） | 单个与批量；对 `PROCESSED` / `RECEIVED` 的事件；数据库层直接把有账本行的事件改回 `RECEIVED` | 成功并审计 / 409 或跳过 / 触发器拒绝 |
| 已处理不可改 | integration（MySQL） | 直接 UPDATE `PROCESSED` 事件的任何列 | 触发器拒绝 |
| 快照齐全 CHECK | integration（MySQL） | 直接把事件改成 `PROCESSED` 但缺一个快照列；非 MYR 缺汇率列；MYR 却有汇率列 | CHECK 拒绝 |
| 事件与账本对应 | integration（MySQL） | `wallet_transaction_id` 指向别的事件的账本行 / 金额不符 | 触发器拒绝 |
| 事务中途失败（INV-13） | integration（SQLite 与 MySQL 各一次） | `post_transaction` 之后写事件时抛错 | 账本、钱包、跃迁、审计、outbox 全部回滚 |
| 队列丢失（§114、REQ-INGEST-002） | integration | 不经 Beat，直接调用扫描与回收函数；停掉 Beat 一段时间后恢复（含若干过期的 `PROCESSING`） | 积压全部处理 |
| 快照可重算（ADR-0005） | integration | 对已处理事件调用 `reprice_from_snapshot` | 与快照逐位相同 |
| 告警维度 | unit | 七个维度各自的 OK / P2 / P1 | 与表一致；摘要不含租户与金额 |
| 余额核对 | integration | 一批处理后 | `verify_wallet` 通过；`Σ AI_USAGE` = 期初 − 期末 |
| 最热租户（ADR-0011） | 不在本任务的 CI | S1–S4 由 T-I 执行 | — |

## 8. 迁移与上线

- **数据迁移步骤**：`usage_events` 加列、约束、索引、三个触发器（先做 0006 同款预检）。此时表里只有未计费事件（Phase 3 试点前没有集成方在发事件），加列成本低。
- **部署顺序**：合并即自动部署，先迁移后代码。Beat 新条目随部署生效。
- **上线前的运维步骤**（写进 `docs/runbook.md`）：至少发布一条全局默认规则、试点模型的价格与 USD 汇率，否则事件全部进错误状态；Healthchecks 新建七个检查。
- **回滚或前滚**：downgrade 删触发器与列。一旦有事件被计费，回滚会丢快照 —— 只能前滚。
- **部分部署**：迁移成功、代码未更新：新列为空，旧代码不计费（事件留在 `RECEIVED`）。代码更新、迁移失败：容器起不来，旧容器继续服务。
- **监控负责人**：Kelvin（Healthchecks 通知）。

## 9. 方案取舍

| 方案 | 优点 | 风险 | 未采用原因 |
| --- | --- | --- | --- |
| **采用**（v2）：认领提交为 `PROCESSING` + 租约 + 防护令牌 + 卡住回收（§82 / §83 / §110 字面）；处理仍是一个事件一个事务 | 中间态可观测、可告警、可演练恢复；回收用 `SKIP LOCKED` 不抢活的处理；防护令牌 + 账本唯一约束保证至多一次效果 | 每个事件多一次短提交（认领按 20 条一批摊薄）；最热租户吞吐受限（压测验证，不达标走方案 C） | — |
| v1：不提交 `PROCESSING`，整个处理在一个持行锁的事务里 | 少一次提交、没有中间态 | 偏离 §82 / §83 / §110 的处理契约，卡住与恢复不可观测（Codex 判 v1 阻断） | 不采用 |
| 摄取后立即入队一个任务 | 延迟更低 | 摄取路径依赖队列；队列与数据库两个事实来源 | Beat 每 10 秒扫描已满足 p95 ≤ 60 秒；需要时再加 |
| 0 元事件也写账本 | 事件与账本一一对应 | 要改 AIH-TASK-005 已上线的账本 CHECK | 不写；事件快照完整 |
| 计价错误自动定期重试 | 少人工 | 原因多样，反复失败浪费资源、刷日志 | 管理员修好后重新入队 |
| 重新入队用 §89 的 `reprocess` 路径 | 字面贴合 | Phase 8 的 reprocess 会改变已计费金额，名字相同会混淆 | `requeue` |
| 逐事件写审计 | 审计完整 | 每秒上百行无增量信息；事件与账本已是审计链 | 不写 |

## 10. 未决问题与假设

- **未决问题**：无阻断项。
- **需要谁拍板**：无。
- **尚未验证的假设**：
  1. MySQL 8.4 的 `FOR UPDATE SKIP LOCKED` 与会话级 `innodb_lock_wait_timeout` 在当前配置下可用 —— 8.0 起支持；CI 的 MySQL 实测。worker 崩溃后，MySQL 在检测到连接断开时回滚其事务并释放行锁（TCP 断开即刻；进程被冻结而连接仍在的情形，依靠 `wait_timeout` 与处理事务的会话级锁超时），回收任务因此能在租约过期后接手。
  2. 触发器里读 `wallet_transactions` 校验事件与账本对应、读账本判断「无财务效果」不需要额外锁：二者都在同一事务里、账本行由同一事务插入或由唯一约束保证不存在。
  3. 最热租户的吞吐未知 —— 这正是 S1 / S2 要测的；不达标的处理在 §2 写定。
- **如果假设错误**：1 → 改用不带 `SKIP LOCKED` 的认领（先 `UPDATE … WHERE status = 'RECEIVED' LIMIT 1` 标记认领，再处理），设计版本 +1；3 → 按判据另开方案 C 任务。

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

- `alembic/versions/<日期>_<序号>_usage_billing.py`
- `app/models/usage.py`（加列）、`app/models/auth.py`（审计动作）
- `app/services/usage_billing.py`（新：扫描、单事件处理、失败记录）
- `app/services/usage_requeue.py`（新）、`app/services/alerts.py`（七个维度）
- `app/tasks/usage_billing.py`（新：扫描与卡住回收两个任务）、`app/core/celery_app.py`（`TASK_MODULES` 与两个 Beat 条目）、`app/core/config.py`（`BILLING_USAGE_MAX_ATTEMPTS`、`BILLING_USAGE_LEASE_SECONDS` 等）
- `app/schemas/usage_requeue.py`（新）、`app/api/admin_usage_events.py`（新，只含两个重新入队接口；T-J 在同一文件加查询接口）、`app/main.py`
- `tests/backend/test_usage_billing.py`、`tests/backend/test_usage_billing_concurrency.py`、`tests/backend/test_usage_requeue_api.py`、`tests/backend/test_usage_alerts.py`（新）
- `tests/backend/test_admin_customers_api.py`、`tests/backend/test_migrations.py`、`tests/backend/test_model_columns.py`、`tests/backend/test_celery.py`
- `docs/database-schema.md`（锁顺序）、`docs/runbook.md`、`docs/api.md`、`docs/TODO.md`

## 12. 版本变更记录

| 版本 | 日期 | 改了什么 | 触发原因 |
| --- | --- | --- | --- |
| v1 | 2026-09-29 | 初稿 | — |
| v2 | 2026-09-29 | 改为按 spec 字面：认领事务提交 `PROCESSING`（`claim_token`、`claimed_at`、`lease_expires_at`、`attempt_count` +1），处理事务核对防护令牌，新增每 60 秒的卡住回收任务（`SKIP LOCKED`，不抢仍持锁的处理）；认领字段 CHECK 与索引；backlog 维度纳入过期 `PROCESSING`；状态表、事务边界、失败矩阵、测试与取舍相应改写 | Codex 判 v1 REQUEST_CHANGES：放弃 `PROCESSING` 与卡住回收，偏离 §82 / §83 / §110 |

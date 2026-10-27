# AIH-TASK-129 至 135 设计：钱包余额核对与告警（已批准 v13）

> **来源**：设计闸门 Issue #474。本文件是 `APPROVED: design v13` 那一版正文的**逐字副本**。审查经过见 §12：v1、v4、v5、v6、v8、v9、v10 由 Codex 审查、被要求修改；v2、v11 曾获 Codex 批准，v3 曾获 Claude 审查者批准（Codex 额度用完期间，经 Kelvin 同意），之后登记前预审又查出须修正的契约，版本上升，批准随之作废；v7、v12 未送审，即因登记前预审的发现升版；v13 由 Codex 审查通过。送审时状态行为 `READY_FOR_REVIEW`，批准后 Issue 上只把状态行改成了 `APPROVED: design v13`。
> 放进仓库，是因为 OpenClaw Worker 在沙箱里不联网、读不到 GitHub Issue。
> **实现以本文件为准**；与 Issue 不一致时，以 Issue #474 上被批准的 v13 为准。设计要改，就回到 Issue 升版本、重新过闸门，不要直接改本文件。
> **前置**：设计闸门 #471（[AIH-TASK-125-portal-notifications.md](AIH-TASK-125-portal-notifications.md)）的 `AIH-TASK-125..128`（迁移 0028 在 0029 之前，`AppLayout.tsx`、路由、`en.json` 与验收脚本同改，排在它们之后）；判定沿用 AIH-TASK-005 的 `ledger_problems` / `verify_wallet`（[AIH-TASK-005-wallet-ledger.md](AIH-TASK-005-wallet-ledger.md)）；告警接口见设计闸门 #183。
> §11「任务拆分」的七个实现任务登记为 `AIH-TASK-129`（迁移与模型）→ `AIH-TASK-130`（流式判定）→ `AIH-TASK-131`（收尾、开始与领租约的步骤函数）→ `AIH-TASK-132`（扫描、续租、进度提交、`step` 与定时任务）→ `AIH-TASK-133`（告警合并）→ `AIH-TASK-134`（管理端接口）→ `AIH-TASK-135`（管理端页面），按这个顺序依赖。

---
## 0. 准入判定

| 任务涉及 | 要填的范围 |
| --- | --- |
| 钱包 / 账本 / 定价 / 汇率 / 支付 / 幂等 / 状态机 | **全部章节** |
| 用量摄取 / 认证与会话（集成侧与平台侧）/ Webhook | §1–§7（**§6 不可省**） |
| 前端 / 文档 / CI / 脚本 | **不走本闸门**，直接开 PR |
| 既不碰钱、也不碰用量摄取 / 认证与会话 / Webhook 的后端改动 | **不走本闸门**，直接开 PR |

本任务档位：`全部`

理由：本设计读钱包与账本（只读，但判定的是余额正确性）、新建两张表与迁移、引入一个核对运行的状态机（`RUNNING → COMPLETED / ABANDONED`）与并发推进，并改动钱包健康告警的判级。不写钱包、不写账本、不改 `post_transaction`。

---

状态：`READY_FOR_REVIEW`
负责人：Claude
**设计版本**：`v13`
对应需求：spec §78（「The system must be able to verify `wallet.balance` against the ordered ledger and alert on any mismatch」）、§7、§8、§120（Negative wallet balance）、§98.1；docs/TODO.md「待登记：余额不一致的定时核对与告警」与 Phase 0「缺余额不一致的定时核对与告警（005 记录的后移项）」；设计闸门 #183 v3（告警接口）
目标 PR：待开（登记 PR）；实现为 AIH-TASK-129..135

## 1. 目标与边界

- **要解决的问题**：钱包余额由账本插入触发器维护（AIH-TASK-005），正常写路径不会不一致；但绕过触发器的操作（`TRUNCATE`、手工 SQL、触发器被删或被改）会让 `wallets.balance` 与账本对不上，而现在**没有任何东西会发现它**。`app/repositories/wallet.py` 已有判定函数 `ledger_problems` 与入口 `verify_wallet`（8 个问题码），但没有调用方；`verify_wallet` 一次把一个钱包的全部账本行读进内存。
- **可观察的完成标准**：
  1. 每个马来西亚日历日通常一次完成的核对运行（同一时刻至多一条进行中的运行由数据库保证），覆盖**运行开始前已提交的**全部租户的钱包（运行期间才提交的新租户见 §2「覆盖范围」）；上线后第一次在约 5 分钟内开始，之后在马来西亚时间 03:00 之后的第一个周期开始。
  2. 每个钱包的判定与 `ledger_problems` 完全相同（同样的问题码、同样的顺序），但逐行流式读取，内存与账本行数无关。
  3. 核对在每个钱包自己的只读事务里读（一致性快照），并发记账不产生误报。
  4. 发现不一致：告警接口的 `wallet_negative_balance` 维度变 P1——不论发现它的运行是否已完成（运行中或已放弃的运行记下的不一致同样触发）；核对超过 30 小时没有完成：P2。
  5. 管理端能列出最近的核对运行，点进一次运行能看到哪些客户的钱包不一致、问题码是什么。
- **明确不做什么**：
  - 不修复。本设计不写钱包、不写账本、不自动调账。**不一致按数据事故处理**：账本插入触发器要求新行的 `balance_before` 等于钱包余额、`wallet_sequence` 等于版本 + 1，所以账本被清空、历史行被篡改或钱包缓存被直接改写之后，`SYSTEM_CORRECTION` 补一行**修不好**这些问题码（它只能在账本与钱包本身一致的前提下调整余额）。修复走 runbook 的事故流程（从备份恢复，或在维护窗口内由管理员按 runbook 人工修复并记审计），P1 一直持续到数据真正修好——这是有意的。
  - 不核对用量事件与账本的对应（每个 `PROCESSED` 事件恰有一行 `AI_USAGE`），不核对支付。
  - 不新增 Healthchecks 检查（Kelvin 2026-10-17 定：免费档已满 20 个，新信号并进已有维度）。
  - 不发客户通知；客户侧不可见。
  - 不做增量核对（只验新增行）：每次都从每个钱包的第一行验起，见 §9 C。
- **现有行为与问题证据**：
  - `ledger_problems(balance, version, billing_status, lines)` 先按 `wallet_sequence` 排序，再依次判 `LEDGER_MISSING`、`BALANCE_NOT_LEDGER_SUM`、`BALANCE_NOT_LAST_BALANCE_AFTER`、`VERSION_NOT_LAST_SEQUENCE`、`SEQUENCE_GAP`、`CHAIN_BROKEN`、`ROW_ARITHMETIC_BROKEN`、`BILLING_STATUS_MISMATCH`；`verify_wallet` 另有 `WALLET_MISSING`。全部是 `Decimal` 比较。
  - `app/services/alerts.py` 的 `wallet_negative_balance`：任何钱包余额 < 0 即 P2，摘要只有计数。`deploy/monitor.sh` 每 5 分钟读 `GET /internal/alerts`，按维度名拼 `BILLING_HEALTHCHECK_ALERT_<维度大写>_URL` 推送。
  - Celery worker 并发为 2（`BILLING_CELERY_CONCURRENCY` 缺省），与用量计费、outbox 投递、状态 webhook 共用。
  - 生产 MySQL 的 `transaction_isolation` 是 `REPEATABLE-READ`（2026-10-27 只读核对），应用不改隔离级别。

## 2. 设计概要

```text
beat（每 300 秒）→ wallet_reconciliation.step
   ├─ 第 0 步：把 run_date <> 今天（早于或晚于）的 RUNNING 标 ABANDONED（按主键逐行，单独事务）
   ├─ 第 1 步：今天没有 COMPLETED、当前没有 RUNNING 且到点 → INSERT 一条 RUNNING（running_slot = 1；
   │          同时至多一条 RUNNING 由 running_slot 唯一约束保证，run_date 不唯一）
   ├─ 第 2 步：读 RUNNING → 领租约（领不到返回 busy）→ 领到之后读游标
   └─ 第 3–4 步：从游标之后逐个租户：
        每个钱包一个只读事务（一致性快照）分块读账本 → 问题码（期间按需续租）
        预算 20 秒用完或没有下一个租户 → 一个写事务：
          带租约与游标的 CAS 推进 cursor、累加计数、插入不一致的 findings、释放租约（没有下一个租户时同时置 COMPLETED）
告警 wallet_negative_balance：负余额（P2）+ 待判运行集（最近一次 COMPLETED 及之后的全部运行）里有不一致 → P1；最近 COMPLETED 超过 30 小时或没有 → P2
管理端：GET runs 列表、GET runs/{run_id}/findings
```

### 数据库（迁移 0029，AIH-TASK-129）

**`wallet_reconciliation_runs`**（一次核对运行一行）

| 列 | 类型 | 空 | 说明 |
| --- | --- | --- | --- |
| `id` | BIGINT 自增主键 | 否 | |
| `public_id` | CHAR(36) | 否 | uuid4，唯一 `uq_wallet_reconciliation_runs_public_id` |
| `run_date` | DATE | 否 | 运行所属的马来西亚日历日（`kuala_lumpur_date(started_at)`）；**不唯一**，普通索引 `ix_wallet_reconciliation_runs_run_date`。一天可能有多行：被收尾的 `ABANDONED` 行不挡住当天再开始；「一天通常只有一次完成的运行」由开始规则保证（见 §2「核对运行」第 1 步） |
| `status` | VARCHAR(16) | 否 | `RUNNING` / `COMPLETED` / `ABANDONED`；CHECK `ck_wallet_reconciliation_runs_status`；模型 `Enum(native_enum=False, length=16)` |
| `started_at` | DATETIME | 否 | 不带时区的 UTC，取整到秒 |
| `finished_at` | DATETIME | 是 | `COMPLETED` / `ABANDONED` 的时刻；CHECK `ck_wallet_reconciliation_runs_finished`：`(status = 'RUNNING') = (finished_at IS NULL)`，写成两条分支的 OR，两库同义 |
| `cursor_tenant_id` | BIGINT | 否 | 已核对到的最大 `tenants.id`；新运行为 0；库级缺省 `0` |
| `wallets_checked` | INT | 否 | 已核对的租户数；库级缺省 `0` |
| `wallets_inconsistent` | INT | 否 | 其中有问题码的租户数；库级缺省 `0`；CHECK `ck_wallet_reconciliation_runs_counts`：`wallets_inconsistent >= 0 AND wallets_inconsistent <= wallets_checked` |
| `lease_owner` | CHAR(36) | 是 | 当前持有推进权的 worker 令牌（每次 step 新生成的 uuid4）；没有持有者时为 NULL |
| `lease_until` | DATETIME | 是 | 租约到期时刻（不带时区的 UTC）；CHECK `ck_wallet_reconciliation_runs_lease`：`(lease_owner IS NULL) = (lease_until IS NULL)`，写成两条分支的 OR，两库同义 |
| `running_slot` | SMALLINT | 是 | `RUNNING` 时恒为 `1`，其余状态恒为 NULL；唯一 `uq_wallet_reconciliation_runs_running_slot`（NULL 可有多行，`1` 只能有一行）：**同一时刻至多一条 `RUNNING` 由数据库保证**，不论 `run_date` 是哪天。CHECK `ck_wallet_reconciliation_runs_running_slot`：`(status = 'RUNNING' AND running_slot IS NOT NULL AND running_slot = 1) OR (status <> 'RUNNING' AND running_slot IS NULL)`。**必须写 `running_slot IS NOT NULL`**：SQL 是三值逻辑，`running_slot` 为 NULL 时 `running_slot = 1` 是 UNKNOWN，少了这一项整条 CHECK 对「`RUNNING` 且 NULL」的结果是 UNKNOWN，而 CHECK 只拒绝 FALSE，会把这种行放进来；加上之后第一个分支为 FALSE，整条为 FALSE，被拒绝。本表其余 CHECK 只涉及非空列或 `IS NULL` 判断，不会得到 UNKNOWN |

索引：两个唯一索引（`public_id`、`running_slot`）之外，普通索引 `ix_wallet_reconciliation_runs_run_date (run_date)` 与 `ix_wallet_reconciliation_runs_status_finished (status, finished_at)`（告警取最近一次 `COMPLETED`）。

三列计数的缺省值：迁移写 `server_default='0'`，模型同时写 `default=0` 与 `server_default='0'`；写入方（AIH-TASK-131）插入新运行时**显式**给出这三个值，不依赖缺省。迁移重跑时的形状比对**不比较缺省值**（缺省值不影响正确性）。

**`wallet_reconciliation_findings`**（一次运行里一个不一致的钱包一行；一致的钱包不写行）

| 列 | 类型 | 空 | 说明 |
| --- | --- | --- | --- |
| `id` | BIGINT 自增主键 | 否 | |
| `run_id` | BIGINT | 否 | 外键 `fk_wallet_reconciliation_findings_run` → `wallet_reconciliation_runs.id`，`ON DELETE RESTRICT` |
| `tenant_id` | BIGINT | 否 | 外键 `fk_wallet_reconciliation_findings_tenant` → `tenants.id`，`ON DELETE RESTRICT` |
| `problem_codes` | VARCHAR(255) | 否 | 问题码按 `ledger_problems` 的输出顺序以英文逗号连接（`WALLET_MISSING` 单独一个）；非空：CHECK `ck_wallet_reconciliation_findings_codes`：`problem_codes <> ''` |
| `balance` | DECIMAL(20,8) | 是 | 快照里的 `wallets.balance`；钱包不存在时为 NULL |
| `wallet_version` | BIGINT | 是 | 快照里的 `wallets.version`；钱包不存在时为 NULL |
| `ledger_rows` | BIGINT | 否 | 快照里读到的账本行数 |
| `checked_at` | DATETIME | 否 | 这个钱包的快照事务开始前取的时刻（UTC，取整到秒） |

索引：唯一 `uq_wallet_reconciliation_findings_run_tenant (run_id, tenant_id)`（一次运行一个租户至多一行；也是管理端按运行列出的索引，同时满足 `run_id` 外键对索引的要求）；普通索引 `ix_wallet_reconciliation_findings_tenant (tenant_id)`，**显式声明**（模型与迁移都写），供 `tenant_id` 外键使用：MySQL 已有以外键列开头的索引时不再自动建索引，所以表上恰好是这两个索引（加主键），重跑时的形状比对不会遇到隐式索引。

**迁移写法**（与设计闸门 #471 v7 §8 的 0028 同一套）：
- upgrade：用 inspector 判两张表。都不存在 → 建两张；都存在且形状逐项一致（列名、类型与宽度、可空性；唯一索引与普通索引的名称与列序；外键的引用与 `ON DELETE RESTRICT`；CHECK 名称存在）→ 跳过、不发 DDL；**只存在 runs 且它的形状正确、为空** → 只建 findings（补完一次在两条 CREATE 之间中断的升级）；其余（只存在 findings、只存在的 runs 有行或形状不对、任一项缺失多出不同）→ 抛错（点名表），不发 DDL。这些分支服务于「MySQL DDL 自动提交，执行到一半中断或写 `alembic_version` 失败」时的重跑。外键的 `ON DELETE` 规则不靠 inspector 反射（MySQL 反射不返回缺省的 `RESTRICT`），改读 `information_schema.REFERENTIAL_CONSTRAINTS.DELETE_RULE`，写法同 `tests/backend/test_migrations.py` 现有的外键规则断言。抛错时消息写明处理方法：先确认那（两）张表没有任何行；为空则手工删掉它们后重跑升级；有行则停下，按数据事故处理，不要删。
- downgrade：**只删空表**：两张都存在且都为空 → 先删 findings 再删 runs；**只存在其中一张且它为空** → 删掉它（补完一次在两条 DROP 之间中断的降级；MySQL 的 DDL 自动提交，中断后会只剩 runs、版本号仍为 0029）；任一张有行 → 抛错（点名表、说明处理方法是保留表只回滚应用镜像），不发 DDL、不删行；两张都不存在时跳过；离线 `--sql` 模式无法判空，直接抛错、不输出 `DROP TABLE`。
- 模型放 `app/models/wallet_reconciliation.py`，在 `alembic/env.py` 登记；表结构写进 `docs/database-schema.md`。

### 钱包判定（AIH-TASK-130，`app/repositories/wallet.py`）

- 把 `ledger_problems` 的判定改写成**按序消费**：新增 `ledger_problems_in_order(*, balance, version, billing_status, lines: Iterable[LedgerLine])`，要求 `lines` 已按 `wallet_sequence` 升序，逐行累加（合计、行数、上一行的 `balance_after`、链与算术两个标志、序号是否恰为 1..n），最后按原有顺序产出问题码。
  - 序号检查的流式等价：第 k 行（从 1 起）的 `wallet_sequence` 必须等于 k；行数 0 时视为 `[] == []`。
  - 末行余额：记住最后一行的 `balance_after`。
- 原 `ledger_problems` 保留签名与行为：先排序，再调 `ledger_problems_in_order`。**两处只有一份规则。**
- 新增 `check_wallet_snapshot(session, tenant_id, *, chunk_rows: int = 5000, on_chunk: Callable[[], None] | None = None) -> WalletCheck`：在调用方给的会话里（调用方负责事务）读钱包行、租户行，再按键集分块读账本四列：每块一条 `SELECT … WHERE wallet_id = ? AND tenant_id = ? AND wallet_sequence > :last ORDER BY wallet_sequence LIMIT :chunk_rows`（**第一块不带 `wallet_sequence > :last` 条件**，从第二块起 `:last` 取上一块最后一行的序号，所以任何序号的行都不会被跳过，与 `ledger_problems` 读到的行完全相同）（不用 `yield_per`，内存与行数无关）；MySQL 上每块的 SELECT 带 `MAX_EXECUTION_TIME(60000)` 优化器提示（毫秒数取同一模块的常量 `LEDGER_CHUNK_TIMEOUT_MS = 60000`，不是参数）（单条语句至多 60 秒，超时抛错由调用方处理），SQLite 上不渲染；每读完一块、喂给判定之后调用一次 `on_chunk()`（给了的话；核对运行用它续租），喂给 `ledger_problems_in_order`。返回 `WalletCheck`：一个 `NamedTuple`，字段依次为 `problem_codes: list[str]`、`balance: Decimal | None`、`version: int | None`、`ledger_rows: int`（AIH-TASK-131 按这几个名字取值）；钱包不存在时为 `WalletCheck([WALLET_MISSING], None, None, 0)`。**所有读都是普通读**（不加锁、不 `populate_existing` 之外的特殊选项），所以在 REPEATABLE READ 下同一事务的几次读看到同一个快照。
- `verify_wallet` 改为调 `check_wallet_snapshot` 并只返回问题码，行为不变（现有测试照常通过）。
- 不改 `post_transaction`、触发器、任何写函数。

### 核对运行（AIH-TASK-131：第 0–2 步的步骤函数；AIH-TASK-132：第 3–5 步、`step` 主流程、Celery 任务与 beat；`app/services/wallet_reconciliation.py` 与 `app/tasks/wallet_reconciliation.py`）

常量：`STEP_SECONDS = 300`（beat 周期）、`STEP_BUDGET_SECONDS = 20`、`TENANT_BATCH = 50`、`START_HOUR_MYT = 3`、`STALE_AFTER = 30 小时`、`LEASE_SECONDS = 290`、`LEASE_RENEW_AFTER_SECONDS = 60`（单块账本 SELECT 的 60 秒上限是 AIH-TASK-130 在 `app/repositories/wallet.py` 定义的常量，不在这里）。

`STALE_AFTER` 的取值：正常情况下相邻两次完成相隔约 24 小时（都在 03:00 之后的第一个周期开始）。最长的正常间隔出现在上线当天：表为空时第一次运行立即开始，若恰在马来西亚 00:00 刚过开始并完成，下一次要到次日 03:00 之后才开始，间隔约 27 小时，再加上一次运行本身的时长。30 小时覆盖这种情况而不误报；代价是某天的运行没完成时，最晚约在次日 09:00 才报 P2。

`step(factory, *, clock=utc_now, monotonic=time.monotonic)`，每次 beat 触发调用一次（`clock` 给墙上时间，`monotonic` 给预算用的单调时钟；两者都可由测试注入）（Celery 任务 `app.tasks.wallet_reconciliation.step`，beat 条目 `wallet-reconciliation-step`，`schedule = 300`，`expires = 270`）：

0. **收尾旧运行**（每次 step 都先做，单独一个短事务）。**每次 step 只在这里读一次** `now = clock()`（取整到秒）与 `today = kuala_lumpur_date(now)`，第 0、1、2 步都用这同一个 `now` 与 `today`；只有第 4 步提交前另读一次（`commit_now`），第 3 步每个钱包的 `checked_at` 各读一次。普通读（不加锁）出 `status = 'RUNNING'` 且 `run_date <> today`（早于**或晚于**今天）的运行 `id`；对每个 `id` 执行 `UPDATE … SET status = 'ABANDONED', finished_at = :now, running_slot = NULL, lease_owner = NULL, lease_until = NULL WHERE id = :id AND status = 'RUNNING'`（按主键逐行，不按范围），提交。没有这种行时不发 UPDATE。**日期不是今天的 `RUNNING` 一律收尾**：前一天的运行最晚在马来西亚 00:00 之后的第一个周期被收尾；`run_date` 晚于今天的 `RUNNING`（时钟偏快时插入，或手工插入）同样被收尾，否则它会一直占着 `running_slot`，时钟恢复后今天的运行永远插不进去。收尾只改状态、不删 findings：未来日期的运行记下的不一致仍在待判运行集里、照样 P1。`run_date` 晚于今天的 `COMPLETED` 行不是 `RUNNING`，不被收尾，只触发告警的 P2 `clock anomaly`（§2「告警」）。代价：若同时有时钟不一致的几个 worker，它们会互相收尾对方日期的运行；worker 在同一台主机上、共用一个时钟，出现时也会以 `clock anomaly` 报出。这一步失败（异常）时按第 5 步处理（`stage = retire`），**这次 step 到此结束**，不开始也不推进；下个周期从第 0 步重来。所以「开始」只会发生在收尾成功之后，不会出现新旧两条 `RUNNING` 并存。
1. **开始**：用第 0 步的 `now` 与 `today`。若今天没有 `COMPLETED` 的运行（`run_date = today AND status = 'COMPLETED'` 的行不存在；同日的 `ABANDONED` 行不算）、当前没有任何 `RUNNING`，且（马来西亚时间 `now + 8h` 的小时 ≥ 3，**或**表里一行运行都没有），则**单独一个事务**只做一件事：`INSERT` 新运行（`status = RUNNING`、`running_slot = 1`、`cursor_tenant_id = 0`、计数 0），提交。插入失败时按下面「插入失败的统一处理」：回滚后重读，读到任何 `RUNNING` 就继续第 2 步，否则 `stage = start` 的异常。**跨午夜的竞态**：两个 worker 分别持有午夜前后的 `today` 时，先插入的一方占住 `running_slot`，后插入的一方必然冲突；所以不同日期的两条 `RUNNING` 不可能并存。若先插入的是昨天日期的那条，下一个周期的第 0 步把它收尾；今天的运行按开始规则在马来西亚 03:00 之后开始（同日被收尾的 `ABANDONED` 行不挡住它）。**同一天多跑一次**：一个 worker 读到「今天没有 `COMPLETED`」之后、插入之前，若恰有另一条运行完成，它会再插入一条今天的运行，当天多做一次全量核对；结果仍然正确，只多一次负载，接受。（插入之前第 0 步已把日期不是今天的 `RUNNING` 收尾，插入之后不需要再收尾。）
   - **为什么拆成两个事务、按主键更新**：在 MySQL REPEATABLE READ 下，「按 `status = 'RUNNING'` 范围 UPDATE、再在同一事务里 INSERT」会对那段索引范围加间隙锁，两个 worker 同时开始时容易互相等待。拆开之后收尾按主键逐行、只锁已存在的旧行，开始只做一条 INSERT。**但 INSERT 本身仍可能死锁**：收尾把旧行的 `running_slot` 置空后，唯一索引里那条 `1` 的旧条目在 purge 之前仍以删除标记存在；两个 worker 同时插入 `running_slot = 1` 时，各自的重复键检查都会在它上面加共享 next-key 锁，再请求插入锁，InnoDB 判为死锁（1213），回滚其中一方。所以开始阶段把死锁当作正常的并发结果处理（下一条）。
   - **插入失败的统一处理**：插入报 `IntegrityError`、死锁（1213）或锁等待超时（`OperationalError`）时，都先回滚，再**锁读**一次（`SELECT id FROM wallet_reconciliation_runs WHERE running_slot = 1`，SQLAlchemy 的 `with_for_update(read=True)`：MySQL 上是共享锁读，SQLite 上不渲染）：锁读会等正在插入那一方的事务提交或回滚之后才返回，读到的是最新已提交的值（普通读在 REPEATABLE READ 下可能看不到对方刚插入、还没提交的行）；读到一行 `RUNNING` → 视为另一个 worker 已开始，继续第 2 步（不记 error）；锁读之后仍一行都没有（对方也回滚了，或者是别的约束失败）→ 走第 5 步的异常处理（`stage = start`），本周期返回 `error`，下个周期重来。`running_slot` 唯一约束保证不论哪种失败都不会多出并行的运行。
2. **推进**：单独一个短的只读事务，读 `status = 'RUNNING'` 且 `run_date <= today` 的运行（通常至多一行；若读到多行，取 `run_date` 最大的一行，其余留给第 0 步），记下 `id` 与 `public_id`，事务随即结束（**这里不读游标**）。没有 → 返回 `idle`。**事务边界**：这次读、第 3 步每个钱包的快照读、第 4 步的进度提交，各是独立的短事务，20 秒的扫描期间不保持任何一个事务打开；第 4 步靠 CAS 而不是靠持有的读事务来判断进度是否被别人推进过。
   - **领租约**（紧接着，单独一个短事务）：`token` 为这次 step 新生成的 uuid4。`UPDATE wallet_reconciliation_runs SET lease_owner = :token, lease_until = :now + 290 秒 WHERE id = :id AND status = 'RUNNING' AND (lease_until IS NULL OR lease_until < :now)`，提交。受影响 0 行 → 另一个 worker 正持有未到期的租约（或运行刚结束），返回 `busy`，**不扫描**。所以同一时刻至多一个 worker 在核对，至多占一个 worker 槽。领到之后，再用一个短的只读事务读这一行的 `cursor_tenant_id` 作为 `old_cursor`。**读到运行之后的失败**（领租约的 UPDATE 或读游标抛错）：先用手里的令牌尽力释放租约（若已领到），再抛出 `ClaimError`，它带着这次运行的 `public_id`，原异常作为 `__cause__`；第 5 步据此记 `run_id`。读到运行之前就失败时原样抛出（此时还不知道是哪次运行）。上一个持有者只有在提交进度的同一条 UPDATE 里才会释放租约，所以领到租约之后读到的游标一定是最新提交的值。
3. 用领租约之后读到的 `old_cursor`、预算截止 `deadline = monotonic() + 20`。循环：每批**单独一个短的只读事务**（新会话，与第 2 步不共用）取 `tenants.id > cursor ORDER BY id LIMIT 50` 的租户 id，取完即结束这个事务；对每个租户：
   - **续租**：调 `check_wallet_snapshot` 时传 `on_chunk`；每读完一块与每核对完一个钱包，若距上次领租或续租已超过 60 秒（按 `monotonic()`），单独一个短事务执行 `UPDATE … SET lease_until = :clock_now + 290 秒 WHERE id = :id AND lease_owner = :token AND status = 'RUNNING'`（`clock_now` 为此刻的 `clock()`），提交。受影响 0 行 → 租约已丢（过期后被别的 worker 接手，或运行已被收尾），立即停止：这一段不提交，返回 `superseded`。续租事务与快照读用不同的会话，不打断快照。
   - **单块超时**：某一块的 SELECT 超过 60 秒（MySQL 报执行时间超限）→ 按第 5 步处理（`stage = advance`）：这一段不提交，`cursor` 停在原处，下一个周期从同一个钱包重来；一直超时则运行完不成，30 小时后 P2 `stale`。
   - **新开一个会话与事务**（只读：只发 SELECT，结束时回滚），取 `checked_at`，调 `check_wallet_snapshot`；
   - 有问题码 → 记一条待写的 finding；
   - `cursor` 推进到这个租户 id，`checked += 1`；
   - 每个租户之后看一次 `monotonic()`，过了截止就停。
   - 一批取空（没有更大的租户 id）→ 标记「已到末尾」。
4. **提交进度**（一个写事务）：
   - 提交前重新取一次 `commit_now = clock()`（取整到秒；不早于这一段任何 finding 的 `checked_at`）；
   - `UPDATE wallet_reconciliation_runs SET cursor_tenant_id = :cursor, wallets_checked = wallets_checked + :checked, wallets_inconsistent = wallets_inconsistent + :bad , lease_owner = NULL, lease_until = NULL [, status = 'COMPLETED', finished_at = :commit_now, running_slot = NULL（已到末尾时）] WHERE id = :id AND status = 'RUNNING' AND cursor_tenant_id = :old_cursor AND lease_owner = :token`（提交即释放租约）；
   - 受影响行数为 1 → 插入这一段的 findings → 提交；返回 `progressed` 或 `completed`。
   - 受影响行数不是 1 → 回滚（不插入 findings），返回 `superseded`：租约已被别人接手、另一个 worker 推进了同一段，或运行已被标成 `ABANDONED`。若租约仍是自己的（例如只有游标对不上），再用新会话尽力释放（同第 5 步的释放语句）。**规则：凡是没有以进度提交结束、而本次 step 领过租约的，返回前都尽力释放租约**。
   - 这一段一个租户都没核对（预算刚开始就用完不可能发生，但 `cursor` 之后没有租户时）→ 只在「已到末尾」时执行同一条带 `COMPLETED` 的 CAS。
5. 任何异常：回滚当前事务，记一条 error 日志 `wallet_reconciliation_failed`，只带三个字段：`stage`（`retire` / `start` / `advance` / `commit`，分别对应第 0 步、第 1 步、第 2–3 步、第 4 步）、`run_id`（运行的 `public_id`；**还不知道是哪次运行时为 null**：`retire`、`start` 两个阶段，以及第 2 步在读到运行之前就失败的 `advance`；读到运行之后的失败从 `ClaimError` 取 `public_id`）、`error_type`（异常类名）；不带租户、余额或问题码。持有租约时再用一个新会话尽力释放（`UPDATE … SET lease_owner = NULL, lease_until = NULL WHERE id = :id AND lease_owner = :token`，失败时记一条 warning 级日志 `wallet_reconciliation_lease_release_failed`，只带 `run_id` 与 `error_type` 两个字段，不再重试；释放不了就等 290 秒后自然过期）。任务返回 `error`，不向外抛；进度停在上一次提交的位置，下一个周期从那里继续。

要点：
- **一致性**：每个钱包的读在一个只读事务里完成，REPEATABLE READ 的一致性快照保证钱包行与账本行是同一时刻的状态：并发记账在快照之后提交的行不可见，钱包行也是快照时的值，所以不会把「正在记账」误判为不一致。SQLite（测试）是串行化隔离，同样成立。
- **不加锁**：核对不拿钱包锁、租户锁或任何 `FOR UPDATE`，不阻塞记账。
- **并发**：推进先要领到租约，所以两个 worker 不会同时扫描同一运行；租约过期后被接手时，原持有者的续租与进度 CAS 都带 `lease_owner = :token`，受影响 0 行、整段回滚。进度 CAS 另带 `cursor_tenant_id = :old_cursor`，两道条件都成立才提交。所以同一运行里一个租户只会被计数一次、至多一条 finding（另由 `(run_id, tenant_id)` 唯一约束兜底）。
- **覆盖范围**：全部租户（含 `DISABLED`、`CLOSED`、内部计量租户）；每个租户一个钱包（`uq_wallets_tenant_id`），所以「核对的租户数」就是钱包数，钱包不存在计为 `WALLET_MISSING`。**覆盖保证**：每批租户 id 查询都在运行开始之后执行，所以**运行开始前已提交的全部租户一定被这次运行覆盖**。运行期间才提交的新租户：id 大于当时游标的会被这次运行覆盖；id 小于当时游标的（自增 id 的分配顺序不等于提交顺序：先分到较小 id 的建户事务可能晚提交），这次运行看不到，由下一次运行覆盖（每次运行都从 `cursor_tenant_id = 0` 重新扫起）。这样漏到下一次的只可能是运行期间刚建好的钱包（余额 0、账本为空，或刚有几笔流水），检测延迟至多约一天，接受（§10）。
- **时间**：`started_at`、`finished_at`、`checked_at` 是 UTC、取整到秒；`run_date` 由 `kuala_lumpur_date` 算（固定 +8，不依赖 tzdata）。
- **资源**：任何时刻至多一个 worker 槽在核对（租约互斥）；正常情况下每 300 秒约 20 秒。单个钱包很长时，持租约的 worker 会边读边续租，在这个钱包读完前一直占着这一个槽，但另一个槽始终留给计费、outbox 与 webhook；MySQL 上单条语句至多 60 秒（`MAX_EXECUTION_TIME`）。持租约的 worker 卡死或崩溃：不再续租，290 秒后租约过期，下一个周期的 step 接手；原 worker 若之后恢复，它的续租与提交都会因 `lease_owner` 不符而作废。

### 告警（AIH-TASK-133，`app/services/alerts.py`）

维度名仍是 `wallet_negative_balance`（改名会改 Healthchecks 的环境变量名），查询函数改为两部分合并：

| 条件 | 级别 |
| --- | --- |
| 「待判运行集」里任何一条运行的 `wallets_inconsistent > 0` | P1 |
| 有余额 < 0 的钱包（现有规则） | P2 |
| 没有 `COMPLETED` 运行，或最近一次的 `finished_at` 早于 `now - 30 小时` | P2 |
| 存在 `run_date` 晚于 `kuala_lumpur_date(now)` 的运行，或 `finished_at` 晚于 `now + 10 分钟` 的运行（时钟异常） | P2 |
| 以上都不成立 | OK |

- 级别取最严重的一条。摘要只有计数与日期（INV-7、设计闸门 #183 v3 §6），恰好是 `<负余额部分>; <核对部分>`：
  - 负余额部分沿用现有文字：`no wallet below zero` / `1 wallet below zero` / `<n> wallets below zero`。
  - 核对部分：没有 `COMPLETED` 运行时为 `reconciliation never completed`；否则为 `reconciliation <run_date> checked <N> inconsistent <M>`（取最近一次 `COMPLETED`），`finished_at` 早于 `now - 30 小时` 时在末尾再加一个空格与 `stale`。`K > 0` 时，核对部分之后再接 `; later inconsistent <K>`；有时钟异常的行时，最后再接 `; clock anomaly <行数>`。
  - 例：`no wallet below zero; reconciliation 2026-10-28 checked 3 inconsistent 0`；`2 wallets below zero; reconciliation 2026-10-26 checked 3 inconsistent 1 stale`；`no wallet below zero; reconciliation never completed`；`no wallet below zero; reconciliation 2026-10-25 checked 3 inconsistent 0 stale; later inconsistent 1`；`no wallet below zero; reconciliation 2026-10-27 checked 3 inconsistent 0 stale; later inconsistent 1; clock anomaly 1`。
- **正常行与异常行**：`today = kuala_lumpur_date(now)`。`run_date <= today` 且（`finished_at` 为 NULL 或 `finished_at <= now + 10 分钟`）的运行是**正常行**，其余是**时钟异常的行**。异常行触发上表最后一行的 P2，**不参与**「最近完成」与 `stale` 的判断（所以不能掩盖 `stale`）；但它们**照样进入**待判运行集：异常行记下的不一致同样触发 P1，并计入 `K`，摘要里看得到。
- **最近一次 `COMPLETED`**：在正常行的 `COMPLETED` 运行里取 **`id` 最大**的一条；没有正常的 `COMPLETED` 运行（包括只有时钟异常的 `COMPLETED` 行）时，视为「没有 `COMPLETED` 运行」：摘要为 `reconciliation never completed`，判级为 P2，待判运行集为全部运行。`id` 是插入顺序，由数据库自增分配、不受墙上时钟影响：运行按开始的先后得到递增的 `id`，扫描发生在插入之后，所以 `id` 更大的完成运行反映的是更晚的一次全量扫描。**不按 `finished_at` 或 `run_date` 排**：这两个值都来自墙上时钟，时钟偏差时会与真实先后不一致。
- **待判运行集**：最近一次 `COMPLETED` 运行，加上 `id` 比它大的**全部运行**（不论状态：`RUNNING`、`ABANDONED`、`COMPLETED`；不论正常行还是异常行）；没有 `COMPLETED` 运行时为全部运行。最近完成、待判集与摘要里的 `run_date`、`N`、`M` 用的是同一个排序键（`id`），所以摘要总能解释 P1 的来源。
  - 已提交的不一致**立即**生效：运行还在推进、或当天没跑完被标 `ABANDONED`，它记下的不一致都在待判集里，P1 不必等到运行完成。
  - P1 一直持续到出现一次没有不一致的 `COMPLETED` 运行为止：那时待判集只剩它和之后的运行（修复后最晚第二天凌晨恢复）。
  - 运行一直完不成时，待判集每天多一行（一天一条运行），查询只读这张小表。
- 待判运行集里除「最近一次 `COMPLETED`」本身以外的运行，其 `wallets_inconsistent` 的最大值记为 `K`（取最大值而不是求和：同一个钱包会在每天的运行里各被记一次）。
- `docs/api.md` 告警表与 `docs/runbook.md` 的这一行随之改写；runbook 新增「钱包核对发现不一致」一节：在管理端核对页找到客户与问题码，各问题码的含义；处置是数据事故流程（冻结排查、核对触发器与审计、从备份恢复或在维护窗口内按 runbook 人工修复并记审计），说明为什么 `SYSTEM_CORRECTION` 修不好这类问题；不得在流程之外直接改 `wallets` 或 `wallet_transactions`。

### 管理端接口（AIH-TASK-134，`app/api/admin_wallet_reconciliation.py`）

两个只读接口，处理函数第一条语句 `require_admin(request)`；分页与现有管理端列表相同（`page`、`page_size`，`Page[...]` 外壳，超出范围 422）；不写审计、不写日志。

**`GET /api/v1/admin/wallet-reconciliation/runs`**：按 `started_at` 倒序、同一时刻按 `id` 倒序。条目恰好：`run_id`（`public_id`）、`run_date`（`YYYY-MM-DD`）、`status`、`started_at`、`finished_at`（可为 null）、`wallets_checked`、`wallets_inconsistent`。

**`GET /api/v1/admin/wallet-reconciliation/runs/{run_id}/findings`**：`run_id` 是普通字符串路径参数（路由守卫用字面路径请求每个管理端接口，期望 401 / 403，所以不在 FastAPI 层校验形状），在 `require_admin` 之后判：不是小写规范 UUID 或查不到 → 404 `RECONCILIATION_RUN_NOT_FOUND`。按 `tenant_id` 升序分页。条目恰好：`customer_id`（租户 `public_id`）、`company_name`、`problem_codes`（字符串数组，按存储顺序拆开）、`balance`（8 位定点字符串或 null）、`wallet_version`（整数或 null）、`ledger_rows`、`checked_at`。

时间格式与管理端审计列表的时间字段相同（不带时区的 UTC）。

### 前端（AIH-TASK-135，不在闸门范围内，写在这里只为说明接口够用）

- 管理端新页 `/wallet-reconciliation`，顶栏加一项：运行表（运行日、状态、开始与结束时间、核对数、不一致数，不一致数 > 0 醒目显示）；点一行在下方显示该运行的不一致列表（客户名链到客户详情页、问题码逐个显示为标签并带说明、余额、版本、账本行数、核对时间）；两张表都分页；空状态说明「还没有核对运行」与「这次运行全部一致」。
- 浏览器验收：沿用 `scripts/acceptance/admin_customers.mjs`，步骤 `login`、`open_wallet_reconciliation`（只读：按 href 点顶栏项，被动观察页面发出的运行列表请求，断言表格行数与 `items` 一致，`items` 非空时第一行的核对数等于 `items[0].wallets_checked`；不点行、不翻页）。

## 3. 不变量影响矩阵

| 不变量 | 是否触碰 | 可能怎样被破坏 | 设计控制 | 验证方式 |
| --- | --- | --- | --- | --- |
| INV-1 中心故障不中断 AI | 是（间接） | 核对拖慢记账或占满 worker | 不加锁；每 300 秒至多约 20 秒占一个槽；读在只读事务里 | §7「不加锁」「预算」 |
| INV-2 事件不重复扣费 | 否 | — | 不写账本 | 不改写路径 |
| INV-3 支付不重复入账 | 否 | — | 不碰支付 | — |
| INV-4 余额只经账本变动 | 是 | 核对代码顺手「修」余额 | 只读；没有任何写 `wallets` / `wallet_transactions` 的语句 | §7 正常路径断言钱包与账本不变；代码检索 |
| INV-5 历史账本不可变 | 是 | 同上 | 同上 | 同上 |
| INV-6 事件保留版本引用 | 否 | — | — | — |
| INV-7 客户不可见成本毛利 | 是 | 告警摘要带出租户或金额 | 摘要只有计数与日期；核对结果只在管理端 | §7「告警摘要」 |
| INV-8 租户不可互访 | 否 | 客户侧无入口 | 两个接口只给 ADMIN | §7「鉴权」 |
| INV-9 对话内容不入库 | 否 | — | — | — |
| INV-10 金额用 Decimal | 是 | 流式累加或 SQLite 求和经 float | 合计在 Python 里用 `Decimal` 逐行累加，不用 SQL `SUM`；`balance` 存 `DECIMAL(20,8)` | §7「金额精度」（MySQL） |
| INV-11 event_id 至多一次财务效果 | 否 | — | — | — |
| INV-12 定稿对账单不可变 | 否 | — | — | — |
| INV-13 状态与事件原子提交 | 是 | 进度推进了但 findings 没写（或反之） | 进度 CAS 与 findings 插入同一事务 | §7「事务中途失败」 |
| INV-14 队列丢失不毁持久工作 | 是 | 运行停在中途永不完成 | 进度在库里；任何一次 beat 触发都从 `cursor` 继续；隔天未完成的标 `ABANDONED`、开新运行；30 小时没完成即 P2 | §7「Redis/Celery 丢失」「隔天」 |

## 4. 状态与并发

**核对运行**

| 当前状态 | 事件 | 前置条件 | 新状态 | 副作用 | 非法时结果 |
| --- | --- | --- | --- | --- | --- |
| （无行） | 开始 | 今天没有 `COMPLETED`、当前没有 `RUNNING`，且马来西亚时间 ≥ 03:00 或表为空 | `RUNNING` | 单独事务只插入这一行（日期不是今天的 `RUNNING` 已在本次第 0 步收尾） | `IntegrityError` 且读得到任何 `RUNNING` → 回滚，继续推进；读不到 → `error` |
| `RUNNING` | 推进一段 | `cursor_tenant_id` 仍等于读到的值，且 `lease_owner = :token` | `RUNNING` | 计数累加、插入 findings | 受影响 0 行 → 整段回滚，`superseded` |
| `RUNNING` | 推进到末尾 | 同上，且没有更大的租户 id | `COMPLETED`（`finished_at`） | 同上 | 同上 |
| `RUNNING` | 收尾（每个周期的第 0 步） | `run_date <> today`（`today` 取这次 step 第 0 步读的时刻；早于或晚于都收尾） | `ABANDONED`（`finished_at`） | 无 | 受影响 0 行（已被别的 worker 收尾）→ 不变 |
| `RUNNING`（无租约或租约已过期） | 领租约 | `lease_until IS NULL OR lease_until < now` | `RUNNING`（`lease_owner`、`lease_until` 有值） | 无 | 受影响 0 行 → `busy`，不扫描 |
| `RUNNING`（持有租约） | 续租 | `lease_owner = :token` | `RUNNING`（`lease_until` 推后） | 无 | 受影响 0 行 → 停止，`superseded` |
| `COMPLETED` / `ABANDONED` | 任何 | — | 不变 | — | 推进 CAS 带 `status = 'RUNNING'`，受影响 0 行 |

- **串行化**：同一时刻至多一条 `RUNNING` 由 `running_slot` 唯一约束保证（不依赖「先收尾再开始」的顺序）；一天通常一次完成的运行由开始规则保证（尽力而为，见第 1 步「同一天多跑一次」）；推进靠租约（同一时刻至多一个推进者）加 `cursor_tenant_id` 的比较并交换（两道条件都在进度 UPDATE 里）。因为只有一条 `RUNNING`、新运行只能在它结束之后插入，所以新运行的 `id` 一定比所有已结束的运行大：「`id` 更大的完成运行反映更晚的一次全量扫描」由此成立。
- **数据库的唯一性保证**：`running_slot` 唯一（同时至多一条 `RUNNING`）；`(run_id, tenant_id)` 唯一（一次运行一个租户一条 finding）；`public_id` 唯一。`run_date` 不唯一（被收尾的行不能挡住当天再开始）。
- **幂等键**：运行的开始靠 `running_slot`（同时至多一条 `RUNNING`）与开始规则（今天没有 `COMPLETED` 才开始），不靠 `run_date`；进度是 `(id, cursor_tenant_id)` 加租约令牌。
- **重试层**：只靠 beat 的下一次触发（每 300 秒），任务本身不重试。
- **必须原子提交**：进度 CAS + 这一段的 findings。开始（插入新运行）与收尾（标 `ABANDONED`）**刻意分成两个事务**，避免间隙锁死锁；收尾在前、开始在后，收尾失败则本次 step 不开始，单一 `RUNNING` 另由 `running_slot` 唯一约束在数据库层保证（推进规则「取 `run_date` 最大的」只作为防御）。
- **与记账并发**：核对读快照、不加锁，记账照常进行；快照之后提交的账本行属于下一次核对。

## 5. 失败与恢复矩阵

| 失败点 | 对外结果 | 数据最终状态 | 是否可重试 | 恢复来源 | 告警 |
| --- | --- | --- | --- | --- | --- |
| 读某个钱包时 DB 异常 | 这一段不提交 | 进度停在上次提交处，无新 findings | 是 | 下一次 beat 从 `cursor` 继续 | error 日志；30 小时没完成 → P2 |
| 单个钱包读得很久（超过一个 beat 周期） | 不影响其他工作 | 持租约的 worker 边读边续租；其他周期的 step 领不到租约，返回 `busy` | — | — | 无（运行过久时由 30 小时 `stale` 暴露） |
| 某一块账本 SELECT 超过 60 秒（MySQL） | 这一段不提交 | `cursor` 停在原处；租约尽力释放 | 是 | 下一个周期从同一钱包重来 | error 日志（`stage = advance`）；一直超时 → 30 小时后 P2 |
| 持租约的 worker 卡死或崩溃 | 暂停至多约 290 秒 | 租约过期；下一个周期的 step 接手 | 是 | 租约过期后的 step | 无 |
| 进度提交失败 | 同上 | 回滚，进度与 findings 都不变 | 是 | 同上 | 同上 |
| worker 崩溃 / Redis、Celery 丢失 | 暂停 | `RUNNING` 行与进度在库里 | 是 | broker 恢复后 beat 照常触发，从 `cursor` 继续 | 30 小时没完成 → P2 |
| 当天没跑完 | 次日开始新运行 | 旧运行 `ABANDONED`（它的 findings 保留）；新运行从头开始 | 是 | 次日运行 | 旧运行已记下的不一致 → P1（在待判集里）；最近 `COMPLETED` 超过 30 小时 → P2 |
| 两个 worker 同时推进 | 不重复 | 一方提交，另一方 `superseded` 整段回滚 | — | — | 无 |
| 两个 worker 同时开始 | 不重复 | 一条 `RUNNING`；另一方插入时 `running_slot` 唯一冲突（`IntegrityError`），重读到已有的 `RUNNING`，继续推进 | — | — | 无 |
| 收尾（第 0 步）失败 | 本周期不开始也不推进 | 不变 | 是 | 下个周期从第 0 步重来 | error 日志（`stage = retire`） |
| 开始时遇到死锁或锁等待超时 | 本周期不开始 | 无新行或新行已提交 | 是 | 下个周期重来 | error 日志 |
| 发现不一致 | 管理端可见 | findings 行；运行照常推进 | — | runbook 的数据事故流程（`SYSTEM_CORRECTION` 修不好，见 §1） | 这一段的 findings 提交后立即 P1（不必等运行完成），持续到出现一次没有不一致的完成运行 |
| 出现未来日期的运行（时钟异常或手工插入） | 告警 P2 | 未来日期的 `RUNNING` 在下一个周期的第 0 步被收尾为 `ABANDONED`（释放 `running_slot`，findings 保留）；未来日期的 `COMPLETED` 原样保留；今天的运行照常开始 | — | 人工核查时钟 | P2 `clock anomaly`；未来日期运行记下的不一致照样 P1 |
| 钱包不存在 | 同上 | finding `WALLET_MISSING`，`balance` 与 `wallet_version` 为 NULL | — | 人工 | P1 |
| 迁移重跑（表已建、版本号未写） | 正常 | 形状一致 → 不发 DDL，版本号写入 | — | — | — |
| 表非空时降级 | 迁移失败 | 表与数据原样，版本号仍为 0029 | 否（按设计拒绝） | 只回滚镜像 | 部署日志 |
| 管理端 `run_id` 非法或不存在 | 404 `RECONCILIATION_RUN_NOT_FOUND` | 不变 | 否 | — | 无 |
| 滚动更新：旧镜像没有这个任务 | 不核对 | 无 | — | 新镜像起来后 beat 触发 | 旧维度只看负余额 |

## 6. 数据与安全边界

- **租户过滤**：核对是平台级任务，按全部租户迭代，结果只写进管理端可见的两张表；没有客户侧入口。
- **鉴权**：两个接口 `require_admin`；CUSTOMER 403、未登录 401（现有依赖），并登记进 `tests/backend/test_admin_customers_api.py` 的管理端路由守卫。
- **客户侧禁止返回的字段**：不适用（无客户接口）。管理端可见余额与版本（管理员本来就能看钱包）。
- **日志**：只有两种：error 日志 `wallet_reconciliation_failed` 只带 `stage`（四个值之一）、`run_id`（还不知道是哪次运行时为 null，见第 5 步）与 `error_type`；warning 日志 `wallet_reconciliation_lease_release_failed`（任何路径上的尽力释放失败时）只带 `run_id` 与 `error_type`，不带租户、余额或问题码；告警摘要只有计数与日期。
- **审计**：不写审计。核对只读，不改变任何业务状态；运行与 findings 两张表本身就是记录。
- **密钥**：不涉及。
- **prompt / response 与客户数据**：不处理。
- **数据保留**：运行每天一行，findings 只在不一致时写；随数据库长期保留，PDPA 与留存政策（上线闸门）一并处理。

## 7. 测试证据计划

| 风险/需求 | 测试层级 | 场景 | 预期结果 |
| --- | --- | --- | --- |
| 迁移形状 | integration（MySQL） | 0028 → 0029 → 0028 → 0029（两表为空） | 两张表的列、可空性、列宽、唯一与普通索引的列序、两个外键 RESTRICT、六条 CHECK 与模型一致；空表降级只删这两张表 |
| 迁移重跑：形状正确 | integration（MySQL） | 升级后把 `alembic_version` 改回 0028 再升级 | 不发 DDL；版本号 0029 |
| 迁移重跑：形状不对 | integration（MySQL） | 版本号 0028，分别：只存在 findings 一张；只存在 runs 且它有一行；缺 `uq_wallet_reconciliation_runs_running_slot`；findings 缺 `ix_wallet_reconciliation_findings_tenant`；findings 的 `run_id` 外键不是 RESTRICT；runs 多一列 | 每个子场景都抛错、不发 DDL、版本号仍为 0028、预置表原样 |
| 补完中断的升级 | integration（MySQL） | 版本号 0028，只存在形状正确且为空的 runs 表 | 升级只发一条建 findings 的 DDL；版本号 0029；两张表形状与模型一致 |
| 带数据降级 | integration（MySQL） | 插入一条运行（与一条 finding）后降级 | 抛错；不发 DDL；行原样；版本号仍为 0029 |
| 只存在一张时降级 | integration（MySQL） | 版本号 0029，手工删掉 findings 表（模拟两条 DROP 之间中断）；分别让 runs 为空与有一行 | 为空：降级删掉 runs、版本号 0028；有一行：抛错（点名 runs）、不发 DDL、runs 原样、版本号仍为 0029 |
| 离线降级 | unit | `alembic downgrade --sql` 到 0028 | 抛错；输出里没有 `DROP TABLE` |
| CHECK | integration（两库） | `RUNNING` 带 `finished_at`、`COMPLETED` 不带 `finished_at`、未知状态、`wallets_inconsistent > wallets_checked`、空 `problem_codes`、只有 `lease_owner` 没有 `lease_until`（及反之）、`RUNNING` 而 `running_slot` 为 NULL、`COMPLETED` 而 `running_slot = 1`、两条 `running_slot = 1` 的行（`run_date` 不同） | 全部被数据库拒绝 |
| 判定等价 | unit（随机对照） | 标准库 `random`、固定种子、至少 1000 组随机账本（0–30 行；含乱序、断档、重复序号、篡改的 `balance_before` / `balance_after`、错误的钱包余额、版本与计费状态），分别喂给改写前的 `ledger_problems` 实现（测试里保留一份原样副本作对照）与 `ledger_problems_in_order`（先排好序） | 两者输出逐项相同（含顺序）；`tests/backend/test_wallet_rules.py` 现有用例照常通过 |
| 流式读取 | integration | `chunk_rows = 3`。**只在 SQLite 上**（模型建表，没有触发器；但有 `ck_wallet_transactions_arithmetic`，所以每种篡改都保持 `balance_after = balance_before + amount`）：10 行一致账本上分别制造 `BALANCE_NOT_LEDGER_SUM`（改钱包余额）、`BALANCE_NOT_LAST_BALANCE_AFTER`（末行的 `amount` 与 `balance_after` 同加 1）、`VERSION_NOT_LAST_SEQUENCE`（改钱包版本）、`SEQUENCE_GAP`（删掉中间一行）、`CHAIN_BROKEN`（中间一行的 `balance_before` 与 `balance_after` 同加 1）、`BILLING_STATUS_MISMATCH`（改租户计费状态）；`LEDGER_MISSING` 用钱包版本 > 0 而账本 0 行。`ROW_ARITHMETIC_BROKEN` 在两个库里都写不进去（同一条 CHECK），只在「判定等价」的纯函数随机对照里覆盖。**MySQL 上**只验一致的 10 行钱包，与 `TRUNCATE` 账本后的 `LEDGER_MISSING` | 每种情形下 `check_wallet_snapshot` 的问题码与 `verify_wallet`、与对同一批行调 `ledger_problems` 的结果都相同（一种篡改可能同时带出几个码，比的是整份列表），且目标问题码在列表里；`ledger_rows` 等于实际读到的行数（多数情形 10，`SEQUENCE_GAP` 9，`LEDGER_MISSING` 0）；返回值是 `WalletCheck`，字段名如 §2 |
| 一致性快照 | integration（MySQL） | 会话 A 开始核对某钱包并读完钱包行后暂停；会话 B 对同一钱包记一笔并提交；A 继续读账本 | A 不报任何问题（看到的是记账前的快照）；之后再核对同样不报 |
| 不加锁 | integration（MySQL） | 核对一个钱包读到一半时暂停，另一个会话对同一钱包 `post_transaction` | 记账不等待、立即提交 |
| 正常路径 | integration | 3 个租户（一个一致、一个账本被清空而钱包版本 > 0、一个直接改了钱包余额；SQLite 用模型建表、没有触发器，直接 DELETE / UPDATE；MySQL 上只做一致与 `TRUNCATE` 账本两种，因为触发器挡住直接改钱包），跑 `step` 直到 `completed` | 一条 `COMPLETED` 运行：`wallets_checked` 3、`wallets_inconsistent` 2（MySQL 上 2 与 1）；两条 findings 的问题码分别含 `LEDGER_MISSING` 与 `BALANCE_NOT_LEDGER_SUM`；钱包与账本行不变 |
| 运行期间提交的低 id 租户 | integration | 3 个租户；一次运行推进过第 2 个租户之后，插入一个显式指定 id 介于第 1 与第 2 个之间的新租户与钱包（模拟较小 id 的建户事务晚提交）；跑到完成；再跑下一次运行 | 这次运行 `wallets_checked` 为 3、不含新租户；下一次运行 `wallets_checked` 为 4、覆盖了它 |
| 钱包不存在 | integration | 在 SQLite 测试库（模型建表、没有触发器）里删掉某租户的钱包行 | finding `WALLET_MISSING`，`balance`、`wallet_version` 为 NULL，`ledger_rows` 0 |
| 开始规则 | unit + integration | 表空时任意时刻；表非空、马来西亚 02:59 与 03:00；今天已有运行 | 表空立即开始；02:59 不开始、03:00 开始；已有 `COMPLETED` 不再开始；今天只有 `ABANDONED` 时可以再开始；有 `RUNNING`（不论日期）时不开始 |
| 隔天 | integration | 昨天的运行仍 `RUNNING`，今天 03:00 后 `step` | 昨天的变 `ABANDONED`（带 `finished_at`）；今天的新运行从 `cursor` 0 开始 |
| 并发开始 | integration（MySQL） | 表里有一条昨天的 `RUNNING`；两个线程用屏障同时执行 `step`（各自独立的连接）；重复 20 次 | 每次都恰好一条 `RUNNING`（今天的）、昨天的那条变 `ABANDONED`；两个 `step` 都不返回 `error`（其中一方可能遇到 1213 死锁或唯一冲突，都被吸收为「已有 `RUNNING`、继续推进」）；不出现 `stage = start` 的 error 日志 |
| 唯一冲突后锁读 | integration（MySQL） | A 插入 `running_slot = 1` 后暂停、不提交；B 的插入等 A 的锁；A 提交后 B 得到唯一冲突、回滚，再锁读 | B 的锁读读到 A 那一行，`start_run` 返回 `already_running`，不抛出 |
| 锁等待超时 | integration（MySQL） | B 的会话 `SET SESSION innodb_lock_wait_timeout = 1`；A 插入后暂停 3 秒不提交；B 执行 `step`；A 提交后 B 再执行一次 `step` | 第一次：B 的插入超时（1205）、回滚，锁读同样超时，`step` 返回 `error`、一条 `stage = start` 的日志、没有新行；第二次：读到 A 那条 `RUNNING`，`start_run` 返回 `already_running`。真正的 1213 死锁时序无法稳定制造，由「并发开始」的 20 次重复覆盖 |
| 跨午夜开始竞态 | integration（MySQL） | 表为空；两个线程用屏障同时执行 `step`，注入的 `clock` 让 A 读到马来西亚 23:59:59、B 读到次日 00:00:01；分别跑「A 先插入」与「B 先插入」两种顺序 | 任何时刻至多一条 `RUNNING`；后插入的一方冲突（唯一冲突或死锁）后继续第 2 步、不返回 `error`；至多一方领到租约；「A 先插入」时：下一个周期（马来西亚 00:05）的第 0 步把 A 那条收尾，此时表非空且早于 03:00，不开始；03:00 之后的周期开始今天的运行，它的 `id` 大于 A 那条 |
| 凌晨空表的收尾不挡住当天 | integration（MySQL） | 表为空；B（次日 00:00:01）插入次日的 `RUNNING` 后，A（23:59:59）执行第 0 步把它收尾为 `ABANDONED`；随后 03:00 之后用正确时钟 | 次日那条 `ABANDONED` 不挡住次日再开始：03:00 之后开始一条新的次日运行（AIH-TASK-131 验到「开始」为止，直接调步骤函数）；AIH-TASK-132 用 `step` 验到这条新运行完成 |
| 收尾失败则本次不开始 | integration | 昨天的 `RUNNING`、今天 03:00 之后；让第 0 步抛错；再跑一次 `step` | 第一次返回 `error`、记一条 `stage = retire`、`run_id` 为 null 的日志，没有插入今天的运行、昨天那条仍 `RUNNING`；第二次把昨天的标 `ABANDONED`（`finished_at` 为第二次第 0 步开始时的时刻）并开始今天的运行 |
| 领租约后读游标 | integration | worker A 领到租约并提交一段（`cursor` 前进、租约释放）；worker B 在 A 提交之前已完成第 2 步的读取，A 提交之后才领租约 | B 领到租约后读到的是 A 提交后的 `cursor`，从那里继续，正常提交；没有 `superseded` |
| 读到运行之后失败 | integration | 让领租约之后读游标抛错 | `claim_run` 抛 `ClaimError`、带这次运行的 `public_id`、`__cause__` 为原异常；租约已被释放；`step` 记一条 `stage = advance`、`run_id` 为该 `public_id` 的 `wallet_reconciliation_failed` |
| 游标不符时释放租约 | integration | 让 B 的进度 CAS 因 `cursor_tenant_id` 不符而受影响 0 行（直接改库里的游标模拟） | B 返回 `superseded`；之后 `lease_owner`、`lease_until` 为 NULL，下一次 `step` 立即领到租约 |
| 时钟与插入顺序不一致 | integration | 两条 `COMPLETED`：`id` 较小的 `run_date` 较晚、`finished_at` 较晚且一致；`id` 较大的 `run_date` 较早、`finished_at` 较早且有 1 条不一致（两条都是正常行） | 「最近完成」取 `id` 较大的那条：P1，摘要里是它的 `run_date` 与 `inconsistent 1` |
| 时钟偏快期间跑完的运行 | integration | 第 D 天的 `COMPLETED`（`id` 4）有 1 条不一致；之后插入、跑完的 `run_date` 为 D+1 的运行（`id` 5，模拟时钟偏快）发现 0 条；`now` 分别取 D 当天与 D+1 当天 | D 当天：`id` 5 是异常行，P1 仍由 `id` 4 触发，另有 `clock anomaly 1`；D+1 当天：`id` 5 成为正常行与最近完成，OK——它是在 `id` 4 之后才做的全量扫描 |
| 非 `running_slot` 冲突的插入失败 | integration | 让开始阶段的插入抛一个 `IntegrityError`，而表里没有任何 `RUNNING`（模拟别的约束失败） | 返回 `error`；`stage = start` 的日志一条；没有新行 |
| 并发推进 | integration（MySQL） | 两个 worker 读到同一 `cursor`：一个领到租约并核对一段；另一个在租约有效期内执行 `step` | 后者返回 `busy`、不扫描、不写任何行；前者提交；计数等于租户数，没有重复 finding |
| 租约过期后接手 | integration | worker A 领到租约后暂停（不续租）；把时钟拨过 `lease_until`，worker B 执行 `step` 并完成一段；A 继续并尝试续租与提交 | B 领到租约并提交；A 的续租受影响 0 行、返回 `superseded`，没有提交任何 findings 或进度 |
| 长读取续租 | integration | `chunk_rows = 3`、某钱包 30 行；注入 `monotonic` 让每块之间相隔 25 秒；读到一半时另一个 worker 执行 `step` | 读这个钱包期间至少续租一次（`lease_until` 推后）；另一个 `step` 返回 `busy`；读完后照常提交 |
| 单块超时 | integration（MySQL） | 捕获执行语句：每块账本 SELECT 恰好带一次 `MAX_EXECUTION_TIME(60000)` 提示；另让某一块抛出 MySQL 执行超时错误 | 提示存在且只在最外层；超时那次 `step` 返回 `error`、`stage = advance`、`cursor` 不变、租约被释放；下一次 `step` 从同一个钱包重来 |
| 异常时释放租约 | integration | 让进度提交抛错 | 返回 `error`；之后 `lease_owner`、`lease_until` 为 NULL，下一次 `step` 立即领到租约 |
| 释放失败只记 warning | integration | 让进度提交抛错，同时让随后的释放语句也抛错 | 返回 `error`；一条 `wallet_reconciliation_failed`（`stage = commit`）与一条 `wallet_reconciliation_lease_release_failed`（恰好 `run_id`、`error_type`）；租约保留到 `lease_until` 过期后被下一次 `step` 领走 |
| 预算 | unit | 注入 `monotonic`，第 2 个租户之后超时 | 这一段只核对 2 个租户、`cursor` 停在第 2 个；下一次从第 3 个继续 |
| 事务中途失败 | integration | 让 findings 插入抛错 | 进度 CAS 被回滚；`cursor` 与计数不变；返回 `error`，不向外抛；恰好一条 `wallet_reconciliation_failed`，字段恰好 `stage`（`commit`）、`run_id`（该运行的 `public_id`）、`error_type` |
| 首次运行在凌晨 | integration | 表为空、马来西亚 00:00:30 开始并完成；注入时钟走到次日 02:59 与次日 03:05 之后的完成 | 次日 02:59 时告警 OK（未超过 30 小时）；次日 03:00 后开始第二次运行 |
| 完成时刻 | integration | 一段里核对两个租户，`clock` 每次调用前进 1 秒，到末尾完成 | `finished_at` 不早于这一段任何 finding 的 `checked_at` |
| Redis/Celery 丢失 | integration | 直接调 `step` 多次（不经队列），中途换新的会话工厂 | 从库里的 `cursor` 继续，最终 `COMPLETED` |
| 告警 | integration | 无运行；有完成运行且一致；一致但 `finished_at` 31 小时前（另测 29 小时前仍 OK）；完成运行有不一致；同时有负余额 | OK 条件之外依次：P2 `never completed`；OK；P2 `stale`；P1；P1（取最严重）；摘要只有计数与日期，不含租户、余额 |
| 告警的新旧并存 | integration | 最近完成运行有不一致，之后一条新运行正在 `RUNNING` 且暂无不一致 | 仍 P1 |
| 未完成运行里的不一致 | integration | 没有 `COMPLETED` 运行，今天的 `RUNNING` 已记下 1 条不一致 | 立即 P1；摘要 `…; reconciliation never completed; later inconsistent 1` |
| 运行始终完不成 | integration | 最近 `COMPLETED` 是 3 天前且一致；之后三条运行（两条 `ABANDONED`、一条 `RUNNING`）的不一致数分别为 1、2、0 | P1；摘要带 `stale` 与 `later inconsistent 2`（最大值） |
| 未来日期的运行 | integration | 表里有一条 `run_date` 为明天的 `COMPLETED`（`finished_at` 也在明天）与一条 `run_date` 为后天、占着 `running_slot` 的 `RUNNING`；今天 03:00 之后执行 `step` 直到完成 | 第一次 `step` 的第 0 步把后天那条 `RUNNING` 收尾为 `ABANDONED`（`running_slot` 置空），随后今天的运行开始并照常完成；明天那条 `COMPLETED` 原样保留；告警 P2、摘要以 `; clock anomaly 2` 结尾；「最近完成」取今天那条；把今天那条的 `finished_at` 拨到 31 小时前时摘要带 `stale` |
| 完成时刻在未来的运行 | integration | 一条三天前一致的 `COMPLETED`（`id` 较小）；之后一条 `run_date` 为今天之前、`finished_at` 为 `now + 2 小时`、有 1 条不一致的 `COMPLETED`（`id` 较大） | **P1**（异常行记下的不一致照样告警），摘要带 `stale`、`later inconsistent 1` 与 `clock anomaly 1`；「最近完成」仍取三天前那条 |
| 前一天的运行在零点后收尾 | integration | 昨天的 `RUNNING`，马来西亚今天 00:05 执行 `step` | 昨天的变 `ABANDONED`；今天 03:00 之前不开始新运行（表非空） |
| 事务不跨扫描 | integration | 包装会话工厂记录每个会话的开启与关闭；跑一个包含 3 个租户的段 | 没有任何会话在两个钱包的快照读之间保持开启；推进读、每批租户 id 查询、每个快照读、进度提交各是独立会话 |
| 一次 step 一个今天 | unit | 注入 `clock`：第一次调用返回马来西亚 23:59:59，之后返回次日 00:00:01 | 第 0、1、2 步都按第一次读到的日期判断（不把刚插入或正在推进的运行当成「昨天」收尾） |
| 不一致修复后恢复 | integration | 待判集里有不一致的 `ABANDONED` 运行，之后出现一条没有不一致的 `COMPLETED` 运行 | OK（`ABANDONED` 那条已不在待判集里） |
| 管理端列表 | integration | 25 条运行、`page_size` 10；`started_at` 相同的两条 | 倒序、相同时刻按 `id` 倒序；条目恰好 7 个键 |
| 管理端 findings | integration | 某运行 3 条 finding（一条 `WALLET_MISSING`） | 按租户升序；条目恰好 7 个键；`problem_codes` 是数组；`WALLET_MISSING` 那条 `balance`、`wallet_version` 为 null |
| 路径参数 | integration | 非 UUID、大写 UUID、不存在的 UUID；未登录用字面路径 | 前三者 404 `RECONCILIATION_RUN_NOT_FOUND`；最后一个 401 |
| 鉴权 | integration | CUSTOMER、未登录 | 403、401；路由守卫列出两个新接口 |
| 金额精度（判定） | integration（**仅 MySQL**，AIH-TASK-130） | 经正常记账（`post_transaction`）得到余额 `999999999999.99999999` 的一致钱包（两行：`999999999999.99999998` 与 `0.00000001`） | `check_wallet_snapshot` 不报问题；返回的 `balance` 为 `Decimal('999999999999.99999999')`，与库里逐字相同；先断言这个值经 float 往返会变（用例有区分力）。不一致方向的精度由「判定等价」的纯函数随机对照覆盖（`Decimal` 逐行累加）|
| 金额精度（记录） | integration（**仅 MySQL**，AIH-TASK-132） | 同上的一致钱包，再 `TRUNCATE` 账本（钱包行保留余额，得到 `LEDGER_MISSING`）；跑 `step` 到完成 | finding 的 `balance` 读回为 `999999999999.99999999`，逐字相同 |

## 8. 迁移与上线

- **数据迁移步骤**：0029 只建两张表及其索引、约束；不改已有表、不回填。写法见 §2「迁移写法」。
- **锁表与性能影响**：建新表不锁已有表。运行时：每天一次全量只读扫描，分散在每 300 秒至多 20 秒的段里；读不加锁。
- **兼容窗口**：`deploy.sh` 先迁移、再换镜像。129 合并后表存在但没人写；130 合并后只是判定函数改成流式（`verify_wallet` 行为不变）；131 合并后只多了没有调用方的步骤函数；132 合并后 beat 开始跑核对、写运行与 findings，告警还只看负余额；133 合并后告警开始看核对结果；134、135 合并后才有管理端接口与页面。
- **部署顺序**：129 → 130 → 131 → 132 → 133 → 134 → 135（`depends_on` 串起来；129 依赖 128，排在门户通知之后）。
- **上线后第一次**：132 上线时表为空，第一个 beat 周期（≤ 300 秒）就开始第一次运行，生产租户很少，一两个周期内完成。133 上线时通常已有完成的运行，告警不会因 `never completed` 而短暂变 P2；若 133 恰在第一次运行完成之前上线，会有一两个周期的 P2，随后恢复。部署后核对一次管理端核对页（134、135 上线后）。
- **回滚或前滚**：生产只回滚应用镜像，不降级 0029（表非空时降级被拒）。132 回滚到旧镜像：不再核对，运行与 findings 保留；133 已上线时会在 30 小时后报 P2 `stale`，这正是想要的信号。133 回滚：告警回到只看负余额。
- **部分部署**：API 新、worker 旧：告警看到的最近完成运行会逐渐变旧，30 小时后 P2——这正是想要的信号。
- **监控**：沿用 `wallet_negative_balance` 维度（P1 / P2），负责人照 runbook 现有分工；`docs/deployment.md` §8 不新增检查。

## 9. 方案取舍

| 方案 | 优点 | 风险 | 未采用原因 |
| --- | --- | --- | --- |
| 采用：每日一次、分段推进、每个钱包一个快照事务、结果入表、并进已有维度 | 不加锁不阻塞记账；进度可恢复；管理端可定位；不新增检查 | 大钱包单次读可能超过段预算 | — |
| A：告警接口每 5 分钟现算 | 不需要表与任务 | 每 5 分钟全量扫描全部账本，随用量线性增长；告警请求变慢 | 开销与用量成正比，且没有可定位的结果 |
| B：SQL 聚合（`SUM`、`LAG` 窗口函数）一条语句判一个钱包 | 少传数据 | SQLite 的 `SUM` 走浮点（#460 v2 已记录），测试与生产语义不同；规则要在 SQL 里再写一份 | 两份规则、两库不同精度 |
| C：增量核对（只验上次之后的新行，存检查点） | 每次扫描量小 | 篡改历史行（`CHAIN_BROKEN` 的主要来源）不会被发现；检查点本身要防篡改 | 核对的意义就在于发现历史被改 |
| D：新增独立维度 `wallet_reconciliation` | 告警语义单一 | 要多一个 Healthchecks 检查 | Kelvin 2026-10-17 定不新增检查 |
| E：不设租约，只靠游标 CAS | 少一套领取与续租逻辑 | 单个钱包读超过一个 beat 周期时，下一个周期会并发扫描同一段，占满两个 worker 槽，拖慢计费、outbox 与 webhook | v4 审查指出；v5 改为租约加单条语句超时 |
| F：单一 `RUNNING` 只靠「先收尾再开始」 | 不用多一列 | 两个 worker 分别持有午夜前后的 `today` 时，两次收尾都可能先于两次插入，当时的 `run_date` 唯一约束（v8 起已去掉）拦不住不同日期的两条 `RUNNING`，两者各自领租约、占满两个槽，旧日期的运行还可能拿到更大的 `id` | v5 审查指出；v6 加 `running_slot` 唯一约束 |

## 10. 未决问题与假设

- **未决问题**：无。Kelvin 2026-10-27 拍板：每天一次；并进 `wallet_negative_balance`；管理端要只读列表。
- **尚未验证的假设**：
  1. 单个钱包的全部账本行能在一个快照事务里读完。现在生产账本行很少；钱包达到百万行级时，一次读可能要很久，占着一个 worker 槽（租约保证不会是两个）、让 InnoDB 保留更久的 undo。单块 SELECT 有 60 秒上限；若某个钱包总是读不完，运行完不成、30 小时后 P2，届时另立「快照分段 + 检查点」的设计。
  2. 一天的 288 个段（每段约 20 秒）足够扫完全部账本。不够时运行当天完不成、次日 `ABANDONED`，30 小时后 P2，信号是可见的；已经扫到的不一致照常触发 P1（待判运行集），不会因运行完不成而被吞掉。超出部分（每天都扫不到的那些钱包）要靠 P2 暴露，届时另立「分段快照 + 检查点」的设计。
- **接受的行为**：运行期间才提交、且 id 小于当时游标的新租户，不在这次运行里，由下一次运行覆盖（§2「覆盖范围」）；所以「完成的运行」只保证覆盖它开始前已提交的租户。
- **需要谁拍板**：无。
- **时钟偏差的残余影响**：判定顺序用 `id`，不受时钟影响；但 `run_date`（一天一次）与 `stale`（30 小时）仍取自墙上时钟。时钟偏快期间可能提前开始「明天」的运行、占用明天的 `run_date`；`stale` 的判断会随偏差量前后移动。偏差持续期间，时钟异常的行会触发 P2 `clock anomaly`，人工核查时钟即可。
- **如果假设错误，具体后果**：核对变慢或当天完不成，由 P2 `stale` 暴露；不会误报不一致，也不会影响账本正确性。

## 11. 审查与版本绑定

审查方要回答的五个问题常驻在 [scripts/review_checklist.md](../../scripts/review_checklist.md) 的「设计审查」一节，此处不重复。

**版本绑定规则**（规则的出处是 [WORKFLOW §3](../../docs/WORKFLOW.md)；这里保留一份是因为审查材料里只有本 Issue 正文，两处改一起改）：

- Codex 的批准必须写成 `APPROVED: design v<N>`，`<N>` 取本 Issue 顶部的「设计版本」
- **设计版本一变，之前的 APPROVE 自动作废**，必须重新过闸门
- 什么算「实质修改」：改了契约、表结构、事务边界、状态机、失败语义、不变量控制。改错别字不算
- 实质修改时：把顶部版本 +1，在下方追加一条变更说明，状态退回 `READY_FOR_REVIEW`

**任务拆分**（每层合并后单独通过检查与 CI）：

| 任务 | 层 | 内容 | 依赖 |
| --- | --- | --- | --- |
| AIH-TASK-129 | 数据库 | 迁移 0029、两个模型、`docs/database-schema.md`、迁移测试 | 128 |
| AIH-TASK-130 | 业务规则 | 流式判定：`ledger_problems_in_order`、`check_wallet_snapshot`、`verify_wallet` 委托、随机对照与流式与快照测试 | 129 |
| AIH-TASK-131 | 业务规则 | 核对运行的步骤函数：第 0 步（收尾）、第 1 步（开始）、第 2 步（读运行与领租约、领租约后读游标）与它们的测试；不挂 Celery 任务、不加 beat | 130 |
| AIH-TASK-132 | 业务规则 | 核对运行的扫描与提交：第 3–5 步（分段扫描、续租、进度 CAS、异常与租约释放）、`step` 主流程、Celery 任务与 beat 条目及其测试 | 131 |
| AIH-TASK-133 | 业务规则 | 告警合并：`wallet_negative_balance` 判级与摘要、告警测试、`docs/api.md` 与 `docs/runbook.md` | 132 |
| AIH-TASK-134 | 接口 | 两个管理端只读接口、`docs/api.md`、路由守卫 | 133 |
| AIH-TASK-135 | 前端 | 管理端核对页、顶栏项、浏览器验收步骤 | 134 |

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
| v1 | 2026-10-27 | 初稿 | — |
| v2 | 2026-10-28 | P1 改看「待判运行集」（最近一次 `COMPLETED` 加上之后的全部运行，含 `RUNNING` 与 `ABANDONED`）：已提交的不一致立即告警、不随运行完不成而丢失；摘要加 `unfinished inconsistent <K>`；§5「当天没跑完」、§10 假设 2 同步；§7 加三条告警用例 | Codex v1 REQUEST_CHANGES：只看最近 `COMPLETED` 时，运行始终完不成会让已发现的不一致永不触发 P1；同版另据登记前预审统一了 §5 的 P1 时机、日志 `stage` 与步骤的对应、「事务中途失败」的日志期望 |
| v3 | 2026-10-28 | §1 改正修复方式：`SYSTEM_CORRECTION` 修不好这类不一致，按数据事故流程处理，P1 持续到修好；第 0 步只收尾 `run_date < today` 的运行，第 2 步只推进 `run_date <= today` 的运行，告警忽略并单独报出未来日期的行（P2 `clock anomaly`）；写明推进读、快照读、进度提交各为独立短事务；原 130 拆成 130（流式判定）、131（核对运行）、132（告警合并），接口与前端改为 133、134；§5、§7 同步。同版另据 v3 草稿的预审：§4 收尾的前置条件与第 0 步一致；计数列库级缺省 0、形状比对不比缺省；一次 step 只读一次 `now`；每批租户 id 查询单独一个短事务；正常行与异常行的划分（异常行不进待判集）；金额精度用例拆到 130 与 131；「最近完成」与待判集统一按 `id`（插入顺序）而不按墙上时钟 | 登记前预审（v2 批准之后）：修复方式与触发器矛盾、未来日期的运行会让核对静默停止、事务边界未定、130 预计超过 1500 行 |
| v4 | 2026-10-28 | §2 迁移写法：外键 `ON DELETE` 规则改读 `information_schema`，错误消息写明处理方法；§2 钱包判定：`WalletCheck` 定为 `NamedTuple` 并写明字段名；§7「流式读取」改正：`LEDGER_MISSING` 用 0 行账本制造，需要直接写坏数据的情形只在 SQLite 上跑，`ledger_rows` 期望按实际读到的行数 | 登记前预审（v3 批准之后）：`LEDGER_MISSING` 在 10 行账本上不可能出现，§7 那一行自相矛盾；MySQL 的触发器挡住部分损坏的制造；`WalletCheck` 形状与外键规则的读取方式未定 |
| v5 | 2026-10-28 | 运行表加 `lease_owner`、`lease_until` 与 CHECK `ck_wallet_reconciliation_runs_lease`；推进先领租约（领不到返回 `busy`），扫描中每 60 秒续租，进度 CAS 带 `lease_owner`、提交即释放，异常时尽力释放；账本固定按键集分块，MySQL 上每块 SELECT 带 `MAX_EXECUTION_TIME(60000)`；时钟异常的行照样进入待判运行集（P1 不被降级），`K` 改为待判集里除最近完成之外的最大值，摘要改为 `later inconsistent`；§4、§5、§7、§9 E、§10 同步 | Codex v4 REQUEST_CHANGES：异常行记下的不一致只报 P2；单个钱包读取超过一个周期时下一周期会并发扫描、占满 worker。同版另据 v5 草稿的预审：领租约之后才读游标、未以提交结束时一律尽力释放租约；第 0 步失败则本次 step 结束（删去不可能出现的「开始后收尾失败」）；流式用例遵守 SQLite 上的算术 CHECK、`ROW_ARITHMETIC_BROKEN` 只在纯函数里覆盖；只有异常 `COMPLETED` 行时视为从未完成；第一块账本不设下界；释放失败记 `wallet_reconciliation_lease_release_failed` warning |
| v6 | 2026-10-28 | 运行表加 `running_slot`（`RUNNING` 时为 1、其余为 NULL）、唯一 `uq_wallet_reconciliation_runs_running_slot` 与 CHECK `ck_wallet_reconciliation_runs_running_slot`：同一时刻至多一条 `RUNNING` 由数据库保证；开始、完成、收尾同步维护这一列；开始时的 `IntegrityError` 若读到任何 `RUNNING` 也视为别人已开始；§4、§7（跨午夜开始竞态、CHECK）、§9 F 同步 | Codex v5 REQUEST_CHANGES：跨午夜时两个 worker 各插入一条不同日期的 `RUNNING`，各自领租约占满 worker，较旧的运行还可能拿到更大的 `id` |
| v7 | 2026-10-28 | 第 0 步收尾所有 `run_date <> today` 的 `RUNNING`（早于或晚于今天），未来日期的 `RUNNING` 不再占住 `running_slot`；§4 收尾行、§5「出现未来日期的运行」、§7「未来日期的运行」同步 | Codex v6 REQUEST_CHANGES：未来日期的 `RUNNING` 不被收尾也不被推进，`running_slot` 唯一约束会让今天的运行永远插不进去。同版另据预审：单块超时的毫秒数改为 `app/repositories/wallet.py` 的常量 `LEDGER_CHUNK_TIMEOUT_MS`，从核对运行的常量里拿掉 |
| v8 | 2026-10-28 | `run_date` 改为普通索引（不唯一），开始规则改为「今天没有 `COMPLETED` 且当前没有 `RUNNING`」，开始时的 `IntegrityError` 只看是否已有 `RUNNING`；迁移的升级可补建缺的 findings、降级可删掉仅存的空表（补完中断的 DDL）；MySQL 上两条精度用例改为能造出来的构造；原 131 拆成 131（第 0–2 步）与 132（第 3–5 步、`step`、Celery 与 beat），告警、接口、前端顺延为 133、134、135；§5、§7、§8、§11 同步 | 登记前预审（v7）：凌晨空表时被收尾的次日行会占住 `run_date` 一整天；降级在两条 DROP 之间中断后两个方向都拒绝；跨午夜用例的期望与开始规则矛盾；MySQL 精度用例造不出来；131 预计约 2000 行 |
| v9 | 2026-10-28 | `ck_wallet_reconciliation_runs_running_slot` 第一个分支加 `running_slot IS NOT NULL`，使「`RUNNING` 且 `running_slot` 为 NULL」被数据库拒绝（原写法在三值逻辑下得 UNKNOWN、被放行） | Codex v8 REQUEST_CHANGES：CHECK 对 `RUNNING + NULL` 的结果为 UNKNOWN，单一 `RUNNING` 的数据库保证失效 |
| v10 | 2026-10-28 | §2 概要图改为与第 0–4 步逐步一致（第 0 步收尾 `run_date <> today`、开始靠 `running_slot` 而非 `run_date` 唯一）；§4 幂等键、§5「两个 worker 同时开始」、§7「非 `running_slot` 冲突的插入失败」、§9 F 去掉 `run_date` 唯一的旧说法 | Codex v9 REQUEST_CHANGES 与登记前预审：v8 把 `run_date` 改为不唯一后，概要与这几处仍写着唯一约束、只收尾更早的运行，与详细契约冲突 |
| v11 | 2026-10-28 | §1 与 §2「覆盖范围」写明精确的覆盖保证（运行开始前已提交的全部租户一定被覆盖；运行期间提交、id 小于当时游标的新租户由下一次运行覆盖），§10 记为接受的行为，§7 加「运行期间提交的低 id 租户」；开始阶段的插入失败（唯一冲突、死锁、锁等待超时）统一为回滚后重读、读到 `RUNNING` 即继续，§7「并发开始」改为重复 20 次且允许其中一方遇到死锁 | Codex v10 REQUEST_CHANGES：自增 id 的分配顺序不等于提交顺序，低 id 的建户事务晚于游标提交时这次运行会漏掉它却标为完成；登记前预审：收尾留下的删除标记索引条目会让两个并发 INSERT 死锁 |
| v12 | 2026-10-28 | 开始阶段插入失败后的重读改为锁读（等对方提交或回滚），死锁牺牲方不会因读不到未提交的行而误报；第 2 步在读到运行之后失败时抛 `ClaimError`（带 `public_id`，先释放租约），第 5 步与 §6 的 `run_id` 改为「还不知道是哪次运行时为 null」；§7 加「死锁后锁读等对方提交」「读到运行之后失败」，「凌晨空表的收尾不挡住当天」写明 131 与 132 各验到哪一步 | v11 获批后的登记前预审：死锁牺牲方普通重读可能看不到对方未提交的插入；`advance` 阶段在读到运行前失败时 `run_id` 无从取得；`claim_run` 失败时 step 拿不到 `public_id` |
| v13 | 2026-10-28 | §7「死锁后锁读等对方提交」按 InnoDB 的实际行为改写为「唯一冲突后锁读」与「锁等待超时」两条可复现的用例；真正的死锁由「并发开始」的重复覆盖 | 登记前预审：对方未提交时 B 的插入只会等待、不会失败，原用例的时序造不出来 |
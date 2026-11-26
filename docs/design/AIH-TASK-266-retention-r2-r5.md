# AIH-TASK-266..269 设计：数据留存 R2、R5 —— 领域 outbox、通知与钱包核对记录的到期清理（已批准 v2）

> **来源**：设计闸门 Issue #862。本文件是 `APPROVED: design v2` 那一版正文的**逐字副本**（送审时状态行为 `READY_FOR_REVIEW`）。审查：v1、v2 由 Claude Code 审查（Kelvin 2026-11-23 定以后一律由 Claude Code 审查）；v1 的阻断项是 `ai_usage_outbox` 的行被供应商成本对账用来判定 AI 模块来源，v2 因此把 R3 移出本设计。
> 放进仓库，是因为 OpenClaw Worker 在沙箱里不联网、读不到 GitHub Issue。
> **实现以本文件为准**；与 Issue 不一致时，以 Issue #862 上被批准的 v2 为准。设计要改，就回到 Issue 升版本、重新过闸门，不要直接改本文件。

状态：`READY_FOR_REVIEW` · 设计版本：`v2` · 目标 PR：待开（登记 PR 在本设计批准后开）
负责人：Claude
对应需求：ADR-0017（2026-11-23 已接受）第 1、2 条；`docs/data-governance.md` 第 4.6、4.8、6 节，第 10 节的 K10、K12、K13 与第 12 节的 R2、R5（这份文档的节号，不是 spec 的章节）；spec §112、`REQ-PRIV-001`；清理框架沿用设计闸门 #833 v7（`docs/design/AIH-TASK-263-retention-purge.md`，已由 AIH-TASK-263..265 交付）；`docs/TODO.md`「`password_reset_tokens` 与 `domain_outbox` 都只增不减」（本设计关掉后一半）。R3（`ai_usage_outbox`，K11）不在本设计，理由见 §1「R3 推迟」

## 0. 准入判定

本任务档位：**§1–§7**。删除的是投递机制的终态行与运维记录，不碰钱包、账本、定价、支付，也不改任何状态机；但 `wallet_reconciliation_*` 是账本完整性的证据、`domain_outbox` 是 Invariant 13、14 的事实来源，按 `docs/data-governance.md` 第 12 节「清理都碰个人数据或删除行，全部需要设计闸门」。§6 不省。§8–§12 一并写出。

## 1. 目标与边界

### 要解决的问题

三类表只增不减：`domain_outbox`（每封邮件、每条通知事件一行）、`notifications`（每条门户通知一行）、`wallet_reconciliation_runs` / `_findings`（每天一次核对一行，不一致的钱包各一行）。ADR-0017 已批准它们的保留期（K10、K12、K13），并要求所有清理走 R1 建好的同一套框架（ADR-0017 第 2 条）。R1 已上线；本设计在框架里注册四项新清理，不改框架本身。

### R3 推迟（v2，#862 第一轮审查阻断项）

`ai_usage_outbox` 的行不只是投递记录：`app/repositories/provider_cost_reconciliations.py:91-99` 的 `ai_module_origin()` 用「`event_id` 在 `ai_usage_outbox` 里存在」判定一个用量事件是否属于供应商成本对账的范围，`app/repositories/usage_analytics.py:43,475,495`（成本分摊，设计 #755 / AIH-TASK-247）与 `outbox_counts` 复用它。已批准的设计 #755 `docs/design/AIH-TASK-242-provider-cost-reconciliation.md` §10 假设 2 写明「`ai_usage_outbox` 的行永不删除……将来加 outbox 清理须保留 `event_id` 或另存来源标记（另过闸门）」。按 K11 删掉送达满 90 天的行，超过 90 天的月份的对账估算、分摊后成本与毛利会悄悄变小，被删的 `event_id` 无法恢复。

所以 R3 拆成两步、另开闸门，不在本设计：先给「AI 模块来源」一个不依赖 outbox 行的持久来源（例如 `usage_events` 上的来源列，含迁移与回填），切换 `ai_module_origin()`；再按 K11 清理 outbox。`ai_usage_outbox` 没有个人数据，生产 2026-10-16 起 219 行，推迟只是体量多留一段时间。`docs/TODO.md`「后续计划」的数据留存一行由登记 PR 改写为「R3（先加 AI 模块来源标记，另过闸门）、R4、R7」。

### 已定（ADR-0017 与 `docs/data-governance.md` 第 10 节，Kelvin 2026-11-23 批准）

- K10 `domain_outbox`：终态（`SENT` / `FAILED`）满 90 天删除；三类没有处理器的 `PENDING` 行选 **a. 维持永久 `PENDING`**（Kelvin 同日单独选定）——本设计一行 `PENDING` 都不碰。
- K12 `notifications`：满 24 个月删除；「关户客户随 K4 删除」那一半属于 R4，不在本设计。
- K13 钱包核对运行与发现：满 24 个月删除，最近一次 `COMPLETED` 不删；先删发现、后删运行（第 4.8 节）。
- 机制：照 ADR-0017 第 2 条与设计 #833 v7——每项独立的 `off` / `count` / `delete` 开关，缺省 `off`；按主键分批、每批 ≤ 1000 行一个短事务、MySQL 上 `READ COMMITTED`；删除与一条 `DATA_RETENTION_PURGE` 审计同一事务提交；先在生产 `count` 一轮。

### 我定的（未单独问，审查者重点看，§10 也列出）

1. **四项清理、四个开关**：`domain_outbox`、`notifications`、`wallet_reconciliation_findings`、`wallet_reconciliation_runs`。核对记录拆成两项（两张表、两个主键序列），注册顺序发现在前、运行在后；运行一项的条件自带「没有发现指向它」，所以只开运行、不开发现也安全（只删没有发现的运行，绝大多数运行没有发现）。
2. **「24 个月」写成 731 天**：框架的保留期是 `dt.timedelta`；任意连续 24 个日历月最长 731 天（含一个 2 月 29 日），731 天保证每一行都「满 24 个月」，最多晚一天删。
3. **每一项的时钟取「进入终态的那一刻」**：`domain_outbox` 用 `processed_at`；`notifications` 用 `created_at`；核对运行与它的发现都用运行的 `finished_at`（发现跟着运行走，不按自己的 `checked_at`）。
4. **「最近一次 `COMPLETED`」= `status = 'COMPLETED'` 里 `id` 最大的一行，它和比它 `id` 大的运行都不删**：告警的「最近完成」与「待判运行集」（`app/services/alerts.py:650-700`）就是按 `id` 取的，规则与它对齐。
5. **保留期仍是代码常量**：90 天、731 天、731 天、731 天，各项自己的常量；改它要改 ADR-0017。

### 可观察的完成标准

1. 四项都 `delete` 时，每天一次的任务删除：结算满 90 天的 `SENT` / `FAILED` 领域 outbox 行；写入满 731 天的通知；所属运行结束满 731 天、且不是最近一次 `COMPLETED`（也不比它新）的核对发现与运行。
2. 一行 `PENDING`（含三类无处理器事件）、`RUNNING` 的核对运行都不动；最近一次 `COMPLETED` 与比它新的运行一行不动。
3. 每个删除批次在与 `DELETE` 同一事务里写一条 `DATA_RETENTION_PURGE` 审计，`after_state` 恰为 `{item, cutoff, batch, deleted}`，与 R1 相同。
4. `count` 只数不删；`off` 不碰表。四个新开关缺省 `off`，部署后什么都不发生。
5. 删除之后：outbox 投递与恢复扫描、积压与死信告警、门户通知列表与未读数、钱包核对告警与管理端核对列表的行为都不变（测试钉住）；`ai_usage_outbox` 一行不动，供应商成本对账与分摊不受影响。

### 明确不做什么

- 不做 R3（见上「R3 推迟」）、R4（关户后的个人数据，含「关户客户的通知随 K4 删除」）、R6、R7；R6 的文档部分另登记为不走闸门的任务（见 §11）。
- 不改清理框架（`run_purge`、批次、审计、隔离级别、命名锁、Beat），只注册新项与配置。
- 不改任何表结构、不加索引、不加迁移（§8）。
- 不改 K10 的选择：无处理器的 `PENDING` 行维持永久保留。
- 不碰 `app/ai_api/` 与 `ai_usage_outbox`。
- 不新增告警维度、不新增 Healthchecks 检查。

### 现有行为与证据

- **`domain_outbox`**（`app/models/auth.py:520-575`，迁移 0004）：状态只有 `PENDING` / `SENT` / `FAILED`，刻意没有「领取中」。结算（`app/tasks/outbox.py:530-551` 邮件类、`:640-662` 通知类、`:775-793` 通知死信）都是 `WHERE status='PENDING' AND attempt_count=:attempt` 的条件更新，同时写 `processed_at`；邮件类结算置空 `payload_json`；`SENT` / `FAILED` 之后没有任何路径再改它（runbook 明令不得手工改回 `PENDING`）。读旧行的只有：恢复扫描（只读 `PENDING`，`:810-822`）、积压告警（只读 `PENDING`，`app/services/alerts.py:845-858`）、死信告警（`FAILED` 且 `processed_at` 在最近 7 天内，`:860-872`）。没有触发器；没有入向外键——`notifications.source_outbox_id` 刻意不建外键（`app/models/notifications.py:84-87`，AIH-TASK-125 设计 §9 B「日后清理 outbox 不被通知表挡住」）。生产现状（2026-11-25 只读查询）：5 行，全是 `SENT`，`processed_at` 无空值，最早 2026-09-15；没有无处理器的 `PENDING` 行。
- **`notifications`**（`app/models/notifications.py`，迁移 0028）：写入后只有 `read_at` 会变；`source_outbox_id` 唯一、无外键。门户列表（`app/api/customer.py:715-746`）按 `source_outbox_id` 倒序翻页，**游标是上一页最后一条的 `public_id`**，经 `source_outbox_id_for` 换成 `source_outbox_id`；游标那条不存在时返回 422 `cursor`。标已读（`app/repositories/notifications.py:62-84`）对不存在的行读回 `None`。没有触发器；外键只有出向的 `tenants`（`RESTRICT`），没有入向外键。生产 2 行。
- **钱包核对**（`app/models/wallet_reconciliation.py`，迁移 0029）：运行 `RUNNING → COMPLETED / ABANDONED`，结束后不再变，`finished_at` 与状态由 CHECK 绑定；发现以 `fk_wallet_reconciliation_findings_run`（`RESTRICT`）指向运行。读它的：核对本身（当天是否已有 `COMPLETED`，`app/services/wallet_reconciliation.py:120-130`）、告警（`id` 最大的正常 `COMPLETED` 与 `id` 更大的运行，`app/services/alerts.py:650-700`）、管理端列表（`app/api/admin_wallet_reconciliation.py`，按 `started_at` 倒序分页，发现按运行列出）。没有触发器。生产 28 行运行、0 行发现，最早 2026-10-29。
- **MySQL 自增**：生产是 MySQL 8.4，自增计数器持久化（8.0 起），删除旧行不会让主键被复用；`notifications.source_outbox_id` 的唯一性因此不受 outbox 删除影响。
- **框架**（`app/services/retention_purge.py`）：`ITEMS` 注册表；`app/tasks/retention.py:45-47` 的 `_modes` 对**每一个注册项**读 `retention_<项名>_mode` 配置——注册一项就必须同时有它的配置字段，否则任务 `AttributeError`。`run_purge` 拒绝不认识的项名与模式值。
- **compose**：`docker-compose.yml` 不读 `.env`，配置键必须在 `x-backend.environment` 显式转发（#859，`tests/backend/test_compose.py` 的 `test_the_retention_settings_reach_backend_services` 钉住转发与缺省值一致）。Worker 契约不能改 compose 与 `.env.example`。

## 2. 设计概要

### 数据流

不变（设计 #833 v7 §2「数据流」）：Beat 每天 20:17 UTC → `app.tasks.retention.purge` → 全 `off` 直接返回 → 命名锁 → 按注册顺序逐项 `off` / `count` / `delete` → 每批 `DELETE` + 审计同一事务。注册顺序变为：

```text
refresh_tokens → password_reset_tokens → recovery_codes            （R1，不变）
→ domain_outbox → notifications                                    （本设计）
→ wallet_reconciliation_findings → wallet_reconciliation_runs      （本设计，发现在前）
```

任何一项抛错本轮停止、后面的项不处理（框架现有行为），所以发现在前、运行在后时，发现那一项出错不会让运行那一项在同一轮里撞外键。

### 四项清理的条件

所有时间比较都是「严格早于 `cutoff`」，`cutoff = now − 该项的保留期`。`select_ids` 按主键升序、带全部条件；`delete_ids` 是 `DELETE … WHERE id IN (:ids) AND <同样的条件>`；`count` 与 `select_ids` 同条件。条件只依赖行本身的列与同表 / 关联表的其他行（规则可重放）。

1. **`domain_outbox`**（保留期 90 天）：`status IN ('SENT', 'FAILED') AND processed_at < :cutoff`。不看 `event_type`：无处理器的三类事件永远是 `PENDING`，自然不在条件里（K10 选 a）。`processed_at` 为空的终态行（正常不存在，生产为 0）不删、不报错。
2. **`notifications`**（保留期 731 天）：`created_at < :cutoff`。已读、未读一样。
3. **`wallet_reconciliation_findings`**（保留期 731 天）：`run_id IN (SELECT id FROM wallet_reconciliation_runs WHERE <可删运行的状态与时间条件>)`，其中「可删运行的状态与时间条件」=

   ```text
   status IN ('COMPLETED', 'ABANDONED')
   AND finished_at < :cutoff
   AND id < (SELECT MAX(id) FROM wallet_reconciliation_runs WHERE status = 'COMPLETED')
   ```

   没有任何 `COMPLETED` 时 `MAX` 为 `NULL`，`id < NULL` 不成立，一行都不删。
4. **`wallet_reconciliation_runs`**（保留期 731 天）：上面「可删运行的状态与时间条件」`AND NOT EXISTS (SELECT 1 FROM wallet_reconciliation_findings f WHERE f.run_id = wallet_reconciliation_runs.id)`。`DELETE` 里引用目标表自身的 `MAX(id)` 写成派生表 `id < (SELECT latest FROM (SELECT MAX(id) AS latest FROM wallet_reconciliation_runs WHERE status = 'COMPLETED') AS latest_completed)`，绕开 MySQL「`DELETE` 的子查询不能直接引用目标表」的限制（与 R1 刷新令牌的写法同一个理由）；`select_ids` 与 `count` 用同一个表达式。

### 注册与配置（每个任务注册自己的项并加自己的开关）

- `app/services/retention_purge.py`：每项一个保留期常量（`DOMAIN_OUTBOX_RETENTION = 90 天`、`NOTIFICATIONS_RETENTION = 731 天`、`WALLET_RECONCILIATION_RETENTION = 731 天`）与 `select_ids` / `delete_ids` / `count` 三个函数，按上面的顺序追加到 `ITEMS`。R1 的 `RETENTION`（30 天）不改名、不改值。
- `app/core/config.py`：`retention_domain_outbox_mode`、`retention_notifications_mode`、`retention_wallet_reconciliation_findings_mode`、`retention_wallet_reconciliation_runs_mode`，都是已有的 `RetentionMode`（`off` / `count` / `delete`），缺省 `off`；环境变量 `BILLING_RETENTION_DOMAIN_OUTBOX_MODE` 等。每轮上限沿用 `retention_max_rows_per_item`，不新增。
- **注册一项与它的配置字段必须在同一个任务里**（`_modes` 对每个注册项读配置，见 §1「现有行为」）。
- `docker-compose.yml` 的 `x-backend.environment` 与 `.env.example` 的四个新键由管理员在**第一个任务运行之前**一次性加上（Worker 契约改不了这两处）；配置里还没有对应字段时，`Settings` 的 `extra="ignore"` 忽略多出来的环境变量，转发无害。每个任务把自己的字段加进 `test_the_retention_settings_reach_backend_services` 的字段清单。

### 审计、日志与查询

不变：`DATA_RETENTION_PURGE`、`entity_type = data_retention_run`、`after_state = {item, cutoff, batch, deleted}`、INFO `retention_purge_item` / WARNING `retention_purge_no_progress` / ERROR `retention_purge_failed`。不加新审计动作，不改前端审计清单。`item` 取上面的四个项名。

### 事务边界

不变：每批一个短事务，第一句在 MySQL 上设 `READ COMMITTED`，`DELETE` 与这一批的审计一起提交；`count` 一个只读事务。不取钱包、租户或用户行锁。

### 时间语义

整轮共用一个 `now`（整秒 UTC），每项 `cutoff = now − 该项保留期`。所有比较的列都是 UTC 无时区列。

### 金额与精度

不涉及金额（`notifications.balance`、`wallet_reconciliation_findings.balance` 随行删除，不读不算）。

## 3. 不变量影响矩阵

| 不变量 | 是否触碰 | 可能怎样被破坏 | 设计控制 | 验证方式 |
| --- | --- | --- | --- | --- |
| INV-1 中心故障不中断 AI | 否 | 清理只在 worker 上跑，不碰 AI 模块的表 | — | — |
| INV-2 事件不重复扣费 | 否 | 不碰用量事件与 `ai_usage_outbox` | — | — |
| INV-3 支付不重复入账 | 否 | 不碰支付与网关事件 | — | — |
| INV-4 余额只经账本变动 | 否 | 不碰钱包与账本；核对记录只是核对的结果 | 只删核对表 | — |
| INV-5 历史账本不可变 | 否 | 核对表、通知、outbox 都不是账本 | 注册表只在代码里，新项必须过闸门 | 测试断言注册表恰好七项、顺序固定 |
| INV-6 事件保留版本引用 | 否 | 不碰用量事件 | — | — |
| INV-7 客户不可见成本毛利 | 否 | 不产生对外数据 | — | — |
| INV-8 租户不可互访 | 否 | 清理不按租户、不返回数据；审计无行内容 | 同 R1 | 测试断言审计 `after_state` 恰为四个键 |
| INV-9 对话内容不入库 | 否 | 不处理 AI 内容 | — | — |
| INV-10 金额用 Decimal | 否 | 不涉及金额运算 | — | — |
| INV-11 event_id 至多一次财务效果 | 否 | 不碰用量事件 | — | — |
| INV-12 定稿对账单不可变 | 否 | 不碰对账单 | — | — |
| INV-13 状态与事件原子提交 | **是** | 删掉还没投递完的领域 outbox 行，状态变了、事件却没了 | 只删 `SENT` / `FAILED` 且 `processed_at` 满 90 天；`PENDING` 一律不碰 | 测试：`PENDING`（有处理器、无处理器、`next_retry_at` 很早的）都不删 |
| INV-14 队列丢失不毁持久工作 | **是** | 删掉恢复扫描要捡回的行；Beat 漏跑 | 恢复扫描只读 `PENDING`，不在删除范围；规则可重放，漏跑下一轮补 | 测试：清理后跑一次恢复扫描，`PENDING` 行照常被派发；连续两轮第二轮只删新到期的行 |

其他保证（不是编号不变量，但是本设计的核心风险）：

| 保证 | 可能怎样被破坏 | 设计控制 | 验证方式 |
| --- | --- | --- | --- |
| 领域 outbox 死信告警 | 删掉 7 天窗口内的 `FAILED`，告警被清掉 | 只删结算满 90 天的行，远超 7 天窗口 | 测试：7 天内的 `FAILED` 不删，告警照常 |
| 门户通知 | 删掉仍在保留期内的通知 | 只删写入满 731 天的 | 测试：边界与正常路径 |
| 门户通知翻页（v2 更正） | 客户拿着上一页最后一条的 `public_id` 当游标，恰好在两次翻页之间那一条被删 | 不额外处理：下一页返回 422 `cursor`，与游标指向一条不存在的通知现有行为相同，客户从第一页重新翻；被删的都是满 731 天的最旧的行，只有翻到最后几页时才可能碰上 | 测试：游标那条被删后下一页 422 `cursor`；从第一页重新翻得到剩下的行、连续不重复；未读数只计剩下的 |
| 通知幂等 | outbox 行删除后同一 `source_outbox_id` 再派生一条通知 | 被删的 outbox 行都是终态，不会再被投递；MySQL 自增不复用 | 测试：删掉一条 `SENT` 的通知类 outbox 行后跑恢复扫描，不产生新通知 |
| 供应商成本对账与分摊 | 删掉 `ai_usage_outbox` 行让 `ai_module_origin()` 漏判 | 本设计不碰 `ai_usage_outbox`（R3 推迟） | 测试：注册表里没有 `ai_usage_outbox` 一项；静态扫描 `app/services/retention_purge.py` 不导入 `app.ai_api` |
| 核对告警 | 删掉「最近一次 `COMPLETED`」或比它新的运行，告警误报「从未完成」或漏报不一致 | 只删 `id` 小于最大 `COMPLETED` `id` 的结束满 731 天的运行 | 测试：最近 `COMPLETED` 很旧也不删；比它新的 `ABANDONED` 不删；告警结果前后一致 |
| 核对外键 | 先删运行撞上发现的 `RESTRICT` | 发现一项排在运行一项前面；运行一项的条件要求没有发现指向它 | 测试：只开运行一项时，有发现的运行不删、不报错 |

## 4. 状态与并发

### 跃迁

被删的行都已是终态，删除不改变任何业务状态；四项都没有回到非终态的路径（`SENT` / `FAILED` 不再改；通知写入后只改 `read_at`；核对运行结束后不再变）。任务本身的状态同 R1。

### 并发

- **两个实例**：框架的 MySQL 命名锁，不变。
- **与业务的写**：被删的行业务不再写。领域 outbox 结算 `WHERE status='PENDING'`，恢复扫描只读 `PENDING`；新行插入。通知只改 `read_at`：被删的通知可能正被标已读——先删则 `mark_read` 读回 `None`，与现有「标一条不存在的通知」同一结果；先标已读则删除照常（`read_at` 不在条件里）。核对只写 `RUNNING` 行与新发现。`READ COMMITTED` 下清理的查找不加间隙锁，`DELETE … WHERE id IN (…)` 只锁被删的行。
- **核对运行的 `MAX(id)`**：在 `select_ids` 与 `DELETE` 各求一次（一致读）。并发完成一次新运行只会让 `MAX(id)` 变大，可删集合只会变大一点（多出来的仍满足结束满 731 天），不会让最近一次 `COMPLETED` 被删：被删的行在 `DELETE` 时刻都满足 `id < 当时的 MAX(id)`。
- **发现与运行之间**：发现一项删完一批提交后，运行一项才会在后面的批次里看到这些运行「没有发现」。同一轮里发现一项因为上限没删完，剩下有发现的运行这一轮不删，下一轮继续。
- **幂等**：同 R1，重跑只会删 0 行。

## 5. 失败与恢复矩阵

| 失败点 | 对外结果 | 数据最终状态 | 是否可重试 | 恢复来源 | 告警 |
| --- | --- | --- | --- | --- | --- |
| 配置不合法（新开关的值） | 启动时配置校验失败 | 不运行 | 改配置 | — | 部署失败即可见 |
| 某批删除时数据库报错（不可用、锁等待超时、死锁） | 无 | 当前批回滚；之前的批保留；本轮停止，后面的项不处理 | 下一轮重做 | 规则可重放 | ERROR `retention_purge_failed`（`item`、`error_type`） |
| 删除运行撞上发现的外键 | 不应发生（条件要求没有发现）；若发生按上一行处理 | 当前批回滚、本轮停止 | 修代码 | — | 同上 |
| 选到却删 0 行 | 不应发生（四项没有回到非终态的路径）；若发生这一项本轮结束 | 不变 | 下一轮重做 | — | WARNING `retention_purge_no_progress` |
| Redis / Celery 丢失、Beat 漏跑 | 无 | 表多长一天 | 下一轮补 | INV-14 | 无 |
| 管理员没先加 compose 转发就开了开关 | 开关不生效 | 不变 | 补转发 | — | 一轮之后日志里没有这一项的 `retention_purge_item`（runbook 写明怎么查） |
| 客户翻页时游标那条被删 | 422 `cursor` | 不变 | 从第一页重新翻 | — | 无（与现有不存在的游标同一行为） |

## 6. 数据与安全边界

- **租户过滤**：全局规则，不按租户，不返回行内容。
- **鉴权主体**：无对外接口；worker 以运行账号执行。「不 `TRUNCATE`、不 `DROP`、不关外键检查」仍靠代码约定与静态扫描（数据库账号拆分未完成）。
- **客户侧禁止返回的字段**：不涉及。门户通知列表只是少了满 731 天的行。
- **日志、审计、异常**：只有项名、模式、截止时刻、行数、耗时与异常类型；不含租户 id、邮箱、payload。
- **密钥**：不读不写。`domain_outbox` 的邮件类 payload 在结算时已置空，删除的是已无令牌明文的行；通知类 payload 只有余额、阈值、状态与流水 `public_id`。
- **prompt / response**：不涉及。
- **数据保留与删除影响**：删除不可恢复（备份按 K15 / K16 到期）。`notifications` 与核对发现里的余额数字随行删除；账本与钱包不受影响。

## 7. 测试证据计划

| 风险/需求 | 测试层级 | 场景 | 预期结果 |
| --- | --- | --- | --- |
| 领域 outbox 正常路径 | integration | `SENT` / `FAILED` 结算满 90 天与不满 90 天；`processed_at` 为空的 `SENT`；`PENDING`（有处理器、三类无处理器、`created_at` 很早的） | 只删满 90 天的 `SENT` / `FAILED`；每批一条审计 |
| 领域 outbox 死信告警 | integration | 7 天内的 `FAILED` | 不删；死信告警计数不变 |
| 恢复扫描与通知幂等 | integration | 删掉一条满 90 天的通知类 `SENT` 行后跑一次恢复扫描；另有一条 `PENDING` | `PENDING` 照常派发；不产生新通知 |
| 通知正常路径 | integration | 写入满 731 天（已读、未读）与不满的 | 只删满 731 天的 |
| 通知翻页 | integration | 同一租户 5 条，按 `page_size = 2` 取第一页；之后删掉最旧 2 条，再拿第一页最后一条当游标取第二页；另拿一条已被删的通知 `public_id` 当游标 | 第二页只有剩下那一条；被删那条当游标得到 422 `cursor`；从第一页重新翻得到剩下 3 条、连续不重复；未读数只计剩下的 |
| 通知标已读竞争 | integration | 删掉一条后再标它已读 | 与「标一条不存在的通知」同一结果 |
| 核对：最近一次 `COMPLETED` | integration | 只有一条 `COMPLETED` 且结束满 731 天；另一种：它之后有结束满 731 天的 `ABANDONED` | 都不删 |
| 核对：正常路径 | integration | 多条结束满 731 天的 `COMPLETED` / `ABANDONED`（其中两条有发现）、一条 `RUNNING`、最近一条 `COMPLETED` | 发现先删、运行后删；`RUNNING` 与最近 `COMPLETED` 留着；核对告警的结果清理前后相同 |
| 核对：只开运行一项 | integration | 发现一项 `off`、运行一项 `delete` | 有发现的运行不删、不报错；没有发现的照删 |
| 核对：没有任何 `COMPLETED` | unit | 全是结束很久的 `ABANDONED` | 一行不删 |
| 边界值 | unit | 每项的时钟列恰好等于 `cutoff` | 不删（严格早于） |
| 保留期常量 | unit | 读注册表 | 七项、顺序固定；R1 三项 30 天，领域 outbox 90 天，通知与核对两项 731 天；项名与配置键一一对应；没有 `ai_usage_outbox` |
| count 与 off | integration | 四项各 `count` / `off` | `count` 不删、行数与 `delete` 一致；`off` 不执行这张表的 SQL |
| 批与上限 | integration | 某一项 2 500 行可删、上限 2 000 | 两批 2 000 行、两条审计后停；下一轮删 500 |
| 幂等重放 | integration | 同一 `now` 连续两轮 | 第二轮删 0 行、不写审计 |
| 锁范围（MySQL） | integration | 打桩让一个 `domain_outbox` 删除批次停在提交前；这期间另一连接插入一行新领域 outbox 并按现有结算写法把一行 `PENDING` 结算为 `SENT`；用 `performance_schema.data_lock_waits` 按 `DATABASE()` 与表名过滤 | 业务操作都在 5 秒内成功、没有等这张表的锁；清理随后提交 |
| R1 用例不受新项干扰 | integration | 已有的 `tests/backend/test_retention_purge.py` 用例 | 用例里 `delete` / `count` 的模式表只开 R1 三项（新项 `off`），避免 MySQL 测试库里别的用例留下的行被新项删掉、审计条数对不上；断言不放宽 |
| 安全 | unit | 静态扫描 `app/services/retention_purge.py` | 不出现 `TRUNCATE`、`DROP`、`FOREIGN_KEY_CHECKS`；不导入 `app.ai_api` |
| 配置与转发 | unit | `tests/backend/test_config.py`、`test_compose.py` | 新开关缺省 `off`、非法值校验失败；compose 转发的缺省值与 `Settings` 一致 |

四个变异必须变红：`domain_outbox` 条件去掉 `status` 过滤（「领域 outbox 正常路径」的 `PENDING` 被删）；核对运行去掉 `id < MAX(id)`（「核对：最近一次 `COMPLETED`」变红）；核对运行去掉 `NOT EXISTS` 发现（「核对：只开运行一项」撞外键、变红）；把任一项的「严格早于」改为「早于或等于」（「边界值」变红）。

## 8. 迁移与上线

- **不加迁移**：三类表的体量（生产今天合计 35 行）全表扫描可以接受。`domain_outbox` 的条件走 `ix_domain_outbox_due` 的 `status` 前缀；通知与核对按主键或全表。`count` 耗时超过 60 秒时按 R1 同一规则另开任务加索引。
- **上线顺序**：
  1. 本设计批准后、第一个任务运行前，管理员开一个小 PR：`docker-compose.yml` 的 `x-backend.environment` 转发四个新键（缺省 `off`），`.env.example` 写出它们；合并即部署，什么都不发生（配置里还没有字段，`extra="ignore"`）。
  2. AIH-TASK-266..268 依次合并即部署，各自的开关缺省 `off`。
  3. AIH-TASK-269 写 runbook 之后，管理员按 runbook：四项改 `count` → 等一轮看行数与耗时 → 改 `delete`。改 `.env` 后重建 worker 与 beat。
- **回滚**：开关改回 `off`，或回滚代码。已删除的行无法恢复（备份里有）。
- **实际生效时间**：生产最早的领域 outbox 行 2026-09-15，90 天后（2026-12 中旬）才有可删的行；通知与核对要到 2028 年 10 月以后。`count` 一轮在那之前看到 0 是正常的。

## 9. 方案取舍

| 方案 | 优点 | 缺点 | 结论 |
| --- | --- | --- | --- |
| **采用**：R3 推迟，先另过闸门给 AI 模块来源一个持久标记 | 不破坏设计 #755 的前提，对账与分摊不受影响 | `ai_usage_outbox` 多留一段时间（无个人数据，体量小） | 采用（v2） |
| 本设计里一并给 `usage_events` 加来源列并切换 `ai_module_origin()` | 一次做完 | 加迁移、回填、改对账与分摊的查询，范围翻倍，且要重过 #755 相关的不变量 | 否 |
| R3 改为只置空 `payload_json`、保留行 | 不动来源判定 | `payload_json` 非空约束要迁移；该表没有个人数据，置空不解决体量 | 否 |
| **采用**：四项四个开关 | 每张表能单独 `count` / `delete`、单独回退 | 配置多 | 采用 |
| 核对两张表合成一项、一个开关 | 配置少 | 框架一项只管一张表的主键序列；合成要改框架 | 否 |
| **采用**：「24 个月」= 731 天 | 每一行都满 24 个月 | 最多晚一天 | 采用 |
| 按日历月相减 | 精确 | 框架保留期是 `timedelta`，要改框架 | 否 |
| **采用**：核对按 `id` 认「最近一次 `COMPLETED`」 | 与告警的取法一致 | 时钟异常的最大 `id` 行会让告警取更早的一条（见 §10 假设） | 采用 |
| **采用**：通知游标被删时沿用 422 `cursor` | 不改接口 | 极少数情况下客户要从第一页重翻 | 采用（v2 更正论据） |

## 10. 未决问题与假设

我定的（§1 已列）：

1. 四项四个开关，核对发现在前、运行在后，运行一项自带「没有发现」。
2. 「24 个月」写成 731 天。
3. 每项的时钟取进入终态的时刻。
4. 「最近一次 `COMPLETED`」按 `id` 最大取。
5. 保留期是代码常量。
6. R3 推迟（v2），另开闸门先加 AI 模块来源标记。

假设：

- 生产 MySQL 是 8.0 以上（今天 8.4.11），自增计数器持久化、主键不复用。SQLite 测试库删掉最大主键的行后会复用主键，测试不依赖「删掉后再插入的主键更大」。
- 告警的「最近完成」只取时钟正常的行；若 `id` 最大的 `COMPLETED` 恰好时钟异常（`finished_at` 晚于现在 10 分钟以上），它本身不满足「结束满 731 天」不会被删，但告警会取更早的一条正常 `COMPLETED`，那一条若也结束满 731 天则可能被删。这要求核对连续 24 个月没有一次正常完成，不处理，runbook 写一句。
- 生产里不存在 `processed_at` 为空的终态领域 outbox 行（今天为 0）；若有，它们不删，`count` 不计入。

## 11. 审查与版本绑定

### 实现拆分

| 任务 | 内容 | 主要文件 | 预计行数 |
| --- | --- | --- | --- |
| AIH-TASK-266 | `domain_outbox` 一项（R2）与开关 | `app/services/retention_purge.py`、`app/core/config.py`、`docs/deployment.md`、`tests/backend/test_retention_purge.py`（注册测试与 R1 用例的模式表）、`tests/backend/test_retention_outbox.py`（新）、`tests/backend/test_retention_purge_mysql.py`（锁范围）、`tests/backend/test_config.py`、`tests/backend/test_compose.py` | ~550 |
| AIH-TASK-267 | `notifications` 一项（R5 前半）与开关 | 同上（新测试 `tests/backend/test_retention_notifications.py`，不改 MySQL 锁范围文件） | ~400 |
| AIH-TASK-268 | 核对发现与运行两项（R5 后半）与两个开关 | 同上（新测试 `tests/backend/test_retention_reconciliation.py`） | ~600 |
| AIH-TASK-269 | 文档：runbook「数据留存清理」一节扩到七项；`docs/data-governance.md` 第 4.6、4.8、12 节标明 R2、R5 已交付、第 4.7 节与第 12 节写明 R3 推迟的理由；`docs/TODO.md` 勾掉「只增不减」那一行 | `docs/runbook.md`、`docs/data-governance.md`、`docs/TODO.md` | ~200 |

三个代码任务都改 `app/services/retention_purge.py` 与注册测试，按 266 → 267 → 268 串行；269 依赖 268。R6 的文档部分另登记为不走闸门的 AIH-TASK-270，与本设计无依赖。

### 设计闸门判定

本设计删除投递表的终态行与核对记录，档位 §1–§7，批准前不写代码。批准后登记 266..269（与不走闸门的 270），按控制面规则预审。

## 12. 版本变更记录

| 版本 | 日期 | 变更 |
| --- | --- | --- |
| v1 | 2026-11-25 | 初稿（R2、R3、R5，AIH-TASK-266..270） |
| v2 | 2026-11-25 | #862 第一轮审查（Claude）阻断项：`ai_usage_outbox` 的行是供应商成本对账与分摊判定「AI 模块来源」的唯一依据（`ai_module_origin()`），设计 #755 §10 假设 2 要求清理前先保留来源标记并另过闸门 —— R3 移出本设计，推迟到另开闸门加来源标记之后；任务改为 266..269，R6 文档改为 270。不阻断项：通知游标是 `public_id` 而不是 `source_outbox_id`，游标那条被删时返回 422 `cursor` —— §1、§3、§5、§7 更正论据并补用例。另：变异补一个「去掉 `NOT EXISTS` 发现」；锁范围测试只留 `domain_outbox` |


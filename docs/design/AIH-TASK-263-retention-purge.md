# AIH-TASK-263..265 设计：数据留存 R1 —— 认证令牌的到期清理与清理框架（已批准 v7）

> **来源**：设计闸门 Issue #833。本文件是 `APPROVED: design v7` 那一版正文的**逐字副本**（送审时状态行为 `READY_FOR_REVIEW`）。审查：v1 由 Codex 审查；Codex 额度用完后，Kelvin 2026-11-23 定以后一律由 Claude Code 审查，v2–v7 由 Claude Code 审查（v6、v7 只改了节号写法与变更记录措辞，以通过文档检查）。
> 放进仓库，是因为 OpenClaw Worker 在沙箱里不联网、读不到 GitHub Issue。
> **实现以本文件为准**；与 Issue 不一致时，以 Issue #833 上被批准的 v7 为准。设计要改，就回到 Issue 升版本、重新过闸门，不要直接改本文件。
> **审查的一条不阻断更正**（v5 审查）：第 4 节说业务路径不会读写待删的行并不完全准确——重置成功时会把同一用户所有未用的兄弟令牌置为已用（不带过期条件），出示已死家族里用过的令牌时会吊销整个家族；两者可能与清理批次短暂等锁，按第 5 节处理。AIH-TASK-263 的契约要求测试这两条路径。

状态：`READY_FOR_REVIEW` · 设计版本：`v7` · 目标 PR：待开（登记 PR 在本设计批准后开）
负责人：Claude
对应需求：ADR-0017（2026-11-23 已接受）第 1、2、4 条；`docs/data-governance.md` 第 0、4.4、6 节，第 10 节的 K7–K9 与第 12 节的 R1（这份文档的节号，不是 spec 的章节）；spec §112、§112.1、`REQ-PRIV-001`；`docs/TODO.md`「`password_reset_tokens` 与 `domain_outbox` 都只增不减」（本任务关掉前一半）

## 0. 准入判定

本任务档位：**§1–§7**（认证与会话：删除认证令牌行，影响刷新令牌的重放检测与门户邀请状态的显示）。§6 不省。§8–§12 一并写出，因为本设计建立的清理框架会被 R2–R5 复用。不碰钱、不碰用量摄取与 Webhook。

## 1. 目标与边界

### 要解决的问题

`refresh_tokens`、`password_reset_tokens`、`recovery_codes` 只增不减：每次登录与刷新各一行刷新令牌，每次忘记密码与邀请各一行重置令牌，每次重新生成 2FA 各十行恢复码。它们都是认证相关的个人数据（`docs/data-governance.md` §3），过了保留期没有理由继续存。ADR-0017 已批准保留期 K7–K9，并要求所有清理按同一套机制执行（ADR-0017 第 2 条），R1 是第一个、也负责把这套机制建起来。

### 已定（ADR-0017 与 §10，Kelvin 2026-11-23 批准）

- K7 `refresh_tokens`：过期满 30 天删除。
- K8 `password_reset_tokens`（含邀请）：过期满 30 天删除。
- K9 已用或已吊销的 `recovery_codes`：满 30 天删除。
- 机制（ADR-0017 第 2 条）：每天一次的 Beat；只删到期且已终结的行，`DELETE` 的 `WHERE` 重复选择条件；每批最多 1000 行、短事务、每张表每轮有总上限；每轮一条系统身份的汇总审计；不 `TRUNCATE`、不 `DROP`、不关外键与触发器；每项独立开关、缺省关；先在生产「只数不删」跑一轮。

### 我定的（未单独问，审查者重点看，§10 也列出）

1. **刷新令牌按家族删，不按行删**：现在的重放检测靠旧行还在（`app/services/auth.py:665-712`：出示一条已用过的令牌，即使已过期，也会吊销整个家族并写 `TOKEN_REUSED` 审计；行不在了只是 401，`auth.py:572-573`）。每次轮换都给新行一个新的 `expires_at`，所以一个一直在续期的家族里，最早那几行可能早已「过期满 30 天」。按行删会让这个家族失去对最早几条被盗令牌的检测。按家族删：**一个家族的所有行里最晚的 `expires_at` 也早于截止时刻**，才删这个家族的全部行。这样被删的家族不可能再被续期（续期必须出示未过期的令牌），检测能力对活着的家族不变。这仍满足 K7「过期满 30 天删除」（每一行都过期满 30 天了），只是有的行会晚于 30 天才删。
2. **邀请令牌照 K8 一律删除，门户账号视图的「邀请时间」随之变空**（v2，#833 第一轮审查：v1 保留每个用户最新的一条会让它无限期留存，违反 K8）。管理端门户账号视图的 `invited_at` 读的是该用户最新一条 `INVITATION` 行（`app/services/portal_accounts.py:240-265`）；那一条过期满 30 天被删后，`invited_at` 返回 `null`。接口与页面已经支持空值（`frontend/src/api/portalAccounts.ts:42` 的类型是 `string | null`，`PortalAccountPanel.tsx:328-330` 用 `TimeValue` 显示），不改代码；`invitation_expires_at` 只对未过期的邀请有值，不受影响；`INVITED` 状态的用户可以照常重发邀请（生成新行）。
3. **只数模式与开关合成一个三值配置**：每项一个 `off` / `count` / `delete`，缺省 `off`。`count` 只数不删、写一行 INFO 日志；`delete` 真删并写审计。
4. **失败不新增告警维度**：这三张表没有触发器、也没有任何表用外键指向它们（调查结论见「现有行为」），所以清理不会撞上数据库拒绝；剩下的失败只有数据库不可用或锁等待，那已由 `readyz` 与现有维度报出。清理失败的影响只是表多长一天。失败写一行 ERROR 日志（固定事件名），下一轮重做；Healthchecks 免费档已用满（2026-10-17 Kelvin 定不新增检查）。
5. **保留期是代码常量，不是配置**：30 天是 ADR 批准的政策，改它要改 ADR；配置里只有开关与每轮上限。

### 可观察的完成标准

1. 三项开关都是 `delete` 时，每天一次的任务删除：所有行都过期满 30 天的刷新令牌家族；过期满 30 天的重置与邀请令牌（邀请照 K8 全删，不保留最新一条）；用掉或吊销满 30 天的恢复码。
2. 未到期、未终结的行一条不动；活着的刷新令牌家族的任何一行都不动。
3. 每个删除批次在与 `DELETE` 同一事务里写一条 `DATA_RETENTION_PURGE` 审计，`after_state` 恰为 `{item, cutoff, batch, deleted}`（项名、截止时刻、本项本轮的批次号、本批删除行数 > 0）；删除 0 行不写；不另写按轮的汇总审计。
4. 开关是 `count` 时一行都不删，日志写出每张表会删多少行；开关是 `off` 时不碰这张表。
5. 删除按主键分批、每批最多 1000 行一个事务，每张表每轮最多删到上限；两个实例不会同时跑（MySQL 命名锁）。
6. 删除之后：重放检测、忘记密码限频、邀请接受、门户账号视图的邀请时间、2FA 校验的行为都不变（测试钉住）。

### 明确不做什么

- 不做 R2–R7（领域 outbox、AI outbox、关户个人数据、通知与核对记录、审计 IP）；框架为它们留出注册一项的位置，但本任务只注册三项。
- 不改任何表结构、不加索引、不加迁移（理由见 §8）。
- 不新增告警维度、不新增 Healthchecks 检查。
- 不改刷新令牌的有效期与轮换规则。
- 不做管理端界面。

### 现有行为与证据

- 三张表都在 `app/models/auth.py`：`refresh_tokens`（144-178，迁移 0002，0024 加 `password_version`）、`recovery_codes`（420-437，迁移 0003）、`password_reset_tokens`（457-504，迁移 0004，0025 加 `purpose`，0040 加索引）。都以 `user_id` 外键 `ON DELETE CASCADE` 指向 `users`。
- 没有触发器指向这三张表；没有任何表用外键指向它们（`grep` alembic 与模型）。审计里只有软引用：`entity_type = "refresh_tokens"` 的 `entity_id` 是家族 id，`"recovery_codes"` 的是用户 id，删除行不影响审计。
- `refresh_tokens`：`issued_at`、`expires_at` 非空；`used_at`、`revoked_at` 可空；没有状态列、没有 `created_at`；索引只有 `(user_id, family_id)`。每次签发 `expires_at = now + refresh_token_ttl_seconds`（`auth.py:281`），轮换也一样，所以过期时间按行滑动，代码里没有家族的绝对上限（`docs/data-governance.md` §4.4 与 §10 K7 写的「绝对期限 12 小时」与代码不符，AIH-TASK-265 更正）。
- `password_reset_tokens`：`expires_at`、`created_at` 非空，`used_at` 可空，`purpose ∈ {PASSWORD_RESET, INVITATION}`；没有吊销列（作废的兄弟行被置 `used_at`）。读旧行的地方：忘记密码限频查 `created_at` 在最近 `password_reset_min_interval_seconds`（≤ 3600 秒）内的行（`password_reset.py:231-240`）；门户账号视图读最新一条邀请（见「我定的」第 2 条）；按哈希查令牌时行不在等同无效。
- `recovery_codes`：`used_at`、`revoked_at` 可空，`created_at` 非空；读它的地方只查未用未吊销的行（`two_factor.py:421-455`）；重置门户 2FA 时今天已经硬删（`portal_accounts.py:731`）。
- 先例：`app/tasks/payment_events.py`（开关提前返回、MySQL `GET_LOCK(CONCAT('payment_events:', DATABASE()), 0)` 防重叠、SQLite 跳过、批量上限加时间预算）；`app/tasks/statements.py`（`lru_cache` 的会话工厂、整秒时间、一行 INFO 日志）；系统身份审计 `AuditLog(actor_user_id=None, actor_role=SYSTEM_ACTOR_ROLE)`（`app/repositories/wallet.py:71,863-864`）。
- Beat 在 `app/core/celery_app.py:156-225`，任务模块在 `TASK_MODULES` 显式列出；时区 UTC。每日全量备份 03:17、日志外送 03:47（马来西亚时间，`docs/data-governance.md` 第 225 行）。

## 2. 设计概要

### 数据流

```text
Beat（每天 20:17 UTC = 04:17 马来西亚时间）→ app.tasks.retention.purge
  → 开关全 off？直接返回
  → MySQL 命名锁 retention_purge:<库名>（拿不到就返回，另一实例在跑）
  → 逐项（按注册顺序）：
      off   → 跳过
      count → 一条 COUNT，记入结果
      delete→ 循环：选一批主键（≤1000，按主键升序，带全部条件）→ 按主键删、WHERE 重复条件 → 提交
              每批：同一事务里 DELETE + 一条 DATA_RETENTION_PURGE 审计 → 提交
              直到选不到或达到本项每轮上限
  → 一行 INFO 日志（每项的模式、截止时刻、行数）
  → 任何一项抛错：回滚当前批、记一行 ERROR、停止本轮（已提交的批保留），释放命名锁
```

### 清理框架（`app/services/retention_purge.py`，AIH-TASK-263）

一项清理 `PurgeItem` 是一个冻结的数据类：

| 字段 | 含义 |
| --- | --- |
| `name` | 稳定名，例如 `refresh_tokens`；也是审计里的键与配置名的后缀 |
| `retention` | `dt.timedelta`，代码常量（本任务三项都是 30 天） |
| `select_ids(session, cutoff, limit) -> list[int]` | 返回最多 `limit` 个可删的主键，按主键升序 |
| `delete_ids(session, cutoff, ids) -> int` | `DELETE … WHERE id IN (ids) AND <同样的条件>`，返回删除行数 |
| `count(session, cutoff) -> int` | 与 `select_ids` 同条件的行数 |

`run_purge(session_factory, *, modes: Mapping[str, Mode], max_rows_per_item: int, now: dt.datetime) -> PurgeResult`：

- `cutoff = now − item.retention`；`now` 由调用方传入、截到整秒（UTC，无时区）。
- `delete`：`while deleted < max_rows_per_item`：一个**短事务**里 `select_ids(limit=min(1000, 余量))`，为空就结束；`delete_ids` 后提交。每批一个独立事务（`session_scope`），不跨批持锁。
- **无进展即停**（v5）：某一批 `select_ids` 选到了主键、`delete_ids` 却删除 0 行（条件在选择与删除之间不一致，正常不会发生），这一项本轮结束（不写审计，因为删除 0 行），记一行 WARNING 日志 `retention_purge_no_progress`（`item`、本批选到的主键数），继续下一项。这保证任何一项都不会让本轮空转、占住命名锁。
- `count`：一个只读事务一条 `count`。
- 任何异常：当前事务回滚，`PurgeResult` 记下出错的项与异常类型，**停止本轮**，不再处理后面的项；已提交的批保留（规则可重放，下一轮继续）。
- 审计（v2，#833 第一轮审查：v1 删除先提交、审计另开事务，审计失败就永久缺失）：**每个删除批次在同一个事务里写一条审计**，与 `DELETE` 一起提交或一起回滚——删除了就一定有审计，没有审计就一定没删。每批：

```text
AuditLog(actor_user_id=None, actor_role="SYSTEM", action=DATA_RETENTION_PURGE,
         entity_type="data_retention_run", entity_id=<now 的 UTC 日期 YYYY-MM-DD>,
         after_state={"item": "<项名>", "cutoff": "<ISO 秒>", "batch": <本项本轮第几批，从 1 起>,
                      "deleted": <本批删除行数，> 0>})
```

  `before_state`、`reason`、IP、UA 都为空。删除 0 行的批不写（选不到主键时直接结束，不会走到删除）。一轮的汇总 = 同一 `entity_id`、同一 `item` 的各批 `deleted` 之和；平常每项每天一批（每天新到期的行远少于 1000），即每项一条。
  **与 ADR-0017 第 2 条第 4 款的关系**：那一款写的是「每轮写一条审计」。按轮写一条就必须在所有批次提交之后另开事务，审计与删除不可能原子；按批写在删除的同一事务里才能保证「删了必有审计」。内容限制不变（只有截止时刻与行数、不含行内容、0 行不写）。AIH-TASK-265 把那一款的措辞补完为「每个删除批次一条，与删除在同一事务提交」，作为 ADR 的原地修订（实现细节的补完，保留期与范围不变）。

- 不写含行内容的日志或审计；日志只有项名、模式、截止时刻、行数、出错时的异常类型。

### 三项清理的条件（AIH-TASK-263）

所有时间比较都是「严格早于 `cutoff`」。

1. **`refresh_tokens`（按家族）**：可删的家族 =「这个 `(user_id, family_id)` 的所有行都 `expires_at < cutoff`」。`select_ids`（两条语句，都是普通读）：① 取候选家族：`SELECT user_id, family_id FROM refresh_tokens GROUP BY user_id, family_id HAVING MAX(expires_at) < :cutoff ORDER BY MIN(id) LIMIT :n`（**分组前不加 `WHERE expires_at < :cutoff`**——v5，#833 第四轮审查：先过滤会让 `HAVING` 恒为真，活着但有旧行的家族也被选成候选；全表分组的耗时按 §8 实测再定）；② 取这些家族的行主键：`WHERE (user_id, family_id) IN (候选)`，走 `ix_refresh_tokens_family`，按主键升序，合计不超过 `limit`（一个家族的行数超过余量时这一批只取它的前 `limit` 行，余下的下一批再删——条件仍成立）。`delete_ids`（v4 定死写法，一条语句）：`DELETE FROM refresh_tokens WHERE id IN (:ids) AND expires_at < :cutoff AND (user_id, family_id) NOT IN (SELECT user_id, family_id FROM (SELECT user_id, family_id FROM refresh_tokens WHERE (user_id, family_id) IN (:本批候选家族) AND expires_at >= :cutoff) AS live)`。派生表只经 `(user_id, family_id)` 索引探查**本批候选家族**，不做全表聚合；双层子查询是为绕开 MySQL「`DELETE` 的子查询不能直接引用目标表」的限制（派生表会先物化）。在 `READ COMMITTED` 下（见「事务边界」），派生表的读是非加锁的一致读，`DELETE` 只对 `id IN (:ids)` 命中的行加记录锁。
2. **`password_reset_tokens`**：`expires_at < cutoff`（两种 `purpose` 一样）。`delete_ids` 重复这个条件。
3. **`recovery_codes`**：`(used_at IS NOT NULL AND used_at < cutoff) OR (revoked_at IS NOT NULL AND revoked_at < cutoff)`。

条件里不出现「行是否被读过」之类应用层状态；只依赖行本身的列与同表的其他行（规则可重放）。

### 任务与配置（AIH-TASK-264）

- `app/tasks/retention.py`：`@shared_task(name="app.tasks.retention.purge")`，`lru_cache` 的会话工厂照 `app/tasks/statements.py`；读配置；三项都 `off` 时直接返回；MySQL 上用 `GET_LOCK(CONCAT('retention_purge:', DATABASE()), 0)`，拿不到就记一行 INFO 返回，`finally` 里 `RELEASE_LOCK`（写法照 `app/tasks/payment_events.py:182-197`，SQLite 跳过）；调用 `run_purge`；返回删除总数。
- `app/core/celery_app.py`：`TASK_MODULES` 加 `app.tasks.retention`；Beat 加一项 `crontab(minute=17, hour=20)`（UTC，即马来西亚 04:17，排在 03:17 的全量备份与 03:47 的日志外送之后），`expires` 3600 秒。
- `app/core/config.py`：
  - `retention_refresh_tokens_mode`、`retention_password_reset_tokens_mode`、`retention_recovery_codes_mode`：`Literal["off", "count", "delete"]`，缺省 `"off"`（环境变量 `BILLING_RETENTION_REFRESH_TOKENS_MODE` 等）。
  - `retention_max_rows_per_item: int = Field(default=50_000, ge=1_000, le=1_000_000)`。
- `.env.example` 写出这四个键与缺省值；`docs/deployment.md` 的配置清单同步。

### 审计动作与查询（AIH-TASK-263）

- `AuditAction.DATA_RETENTION_PURGE`（`app/models/auth.py`，非原生枚举，不需要迁移）。
- `app/services/audit_query.py`：`data_retention_run` 归入「对外 id」一类（`entity_id` 是日期，不是内部 id）。
- `frontend/src/api/adminAudit.ts` 的动作清单照后端枚举顺序加这一项（`adminAudit.test.ts` 逐项比对）。

### 事务边界

- 每批一个事务：`DELETE` 与这一批的审计行一起提交；`count` 一个只读事务。不在一个长事务里删完一张表。
- **清理的每个事务在 MySQL 上用 `READ COMMITTED`**（v4，#833 第三轮审查）：会话的第一句是 `session.connection(execution_options={"isolation_level": "READ COMMITTED"})`，只在 `mysql` 方言上设，写法照 `app/services/password_reset.py:172-190`（SQLite 不接受，也没有行锁与快照问题；连接归还时隔离级别还原）。理由：`REPEATABLE READ` 下，`DELETE` 里读同表的子查询与按范围的查找会加 next-key 锁；派生表或聚合写法可能把整张 `refresh_tokens` 的行与间隙锁住，挡住所有登录（插入）与刷新（`app/services/auth.py:590` 的条件更新）。`READ COMMITTED` 下查找不加间隙锁、子查询是一致读，删除只锁它删的那些行——而那些行都是到期满 30 天的终态行，业务不会再写。三项清理都这样，`count` 的只读事务不需要。
- 不取钱包、租户或用户行锁；删除只锁被删的行（按主键）。

### 时间语义

- `now` 是任务开始时的 UTC 时刻（整秒），整轮共用一个 `cutoff`，跨批不变（批与批之间新到期的行留到下一轮）。
- `expires_at`、`used_at`、`revoked_at`、`created_at` 都是 UTC 无时区列，直接比较。

### 金额与精度

不涉及金额。

## 3. 不变量影响矩阵

| 不变量 | 是否触碰 | 可能怎样被破坏 | 设计控制 | 验证方式 |
| --- | --- | --- | --- | --- |
| INV-1 中心故障不中断 AI | 否 | 清理只在 worker 上跑，不在 AI 调用路径上 | — | — |
| INV-2 事件不重复扣费 | 否 | 不碰用量与账本 | — | — |
| INV-3 支付不重复入账 | 否 | 不碰支付 | — | — |
| INV-4 余额只经账本变动 | 否 | 不碰钱包 | — | — |
| INV-5 历史账本不可变 | 否 | 三张表不是账本；框架不允许注册账本类表（R2–R5 各自过闸门） | 注册表只在代码里，新项必须过设计闸门 | 测试断言本任务只注册三项 |
| INV-6 事件保留版本引用 | 否 | 不碰用量事件 | — | — |
| INV-7 客户不可见成本毛利 | 否 | 不产生对外数据 | — | — |
| INV-8 租户不可互访 | 否 | 清理不按租户、不返回数据；审计无行内容 | 只记项名、截止时刻、批次号与行数 | 测试断言审计 `after_state` 恰为 `{item, cutoff, batch, deleted}`、不含行内容 |
| INV-9 对话内容不入库 | 否 | 不处理 AI 内容 | — | — |
| INV-10 金额用 Decimal | 否 | 不涉及金额 | — | — |
| INV-11 event_id 至多一次财务效果 | 否 | 不碰用量事件 | — | — |
| INV-12 定稿对账单不可变 | 否 | 不碰对账单 | — | — |
| INV-13 状态与事件原子提交 | 否 | 删除不产生领域事件；每批独立提交是有意的（可重放） | 规则只依赖行本身，部分完成也一致 | 测试：中途失败后下一轮补完 |
| INV-14 队列丢失不毁持久工作 | **是** | Beat 漏跑、任务丢失 | 规则可重放：下一轮按同样条件继续；不依赖任何「上次跑到哪」的状态 | 测试：连续两轮，第二轮只删新到期的行 |

认证相关的保证（不是编号不变量，但是本设计的核心风险）：

| 保证 | 可能怎样被破坏 | 设计控制 | 验证方式 |
| --- | --- | --- | --- |
| 刷新令牌重放检测 | 删了活着的家族里的旧行，被盗旧令牌再出现时不再吊销家族 | 按家族删：家族最晚的 `expires_at` 早于截止时刻才删；这样的家族不可能再续期 | 测试：活着的家族里过期满 30 天的旧行不删；出示它仍吊销家族、写 `TOKEN_REUSED` |
| 忘记密码限频 | 删掉窗口内的令牌，限频失效 | 限频窗口 ≤ 3600 秒，而删除只针对过期满 30 天的行 | 测试：限频窗口内的行不受影响 |
| 邀请时间显示 | 删光邀请行，门户账号视图的邀请时间变空 | 这是 K8 的预期效果；接口与页面已支持空值 | 测试：邀请全部过期满 30 天被删后，视图 `invited_at` 为 `null`、`invitation_expires_at` 为 `null`、状态不变；`INVITED` 用户重发邀请照常 |
| 令牌校验 | 删掉仍有效的令牌 | 只删过期（或用掉 / 吊销）满 30 天的行 | 测试：边界时刻（恰好 `cutoff`）不删 |

## 4. 状态与并发

### 跃迁

被删的行都已是终态（过期、用掉、吊销），删除不改变任何业务状态。唯一的「状态」是任务本身：

| 当前 | 事件 | 前置条件 | 结果 | 非法时 |
| --- | --- | --- | --- | --- |
| 无 | Beat 触发 | 至少一项不是 `off` | 拿命名锁 → 逐项处理 → 审计 → 释放锁 | 拿不到锁：INFO 后返回 |

### 并发

- **两个实例**：MySQL 命名锁 `retention_purge:<库名>`，拿不到就返回。命名锁按连接持有，任务在 `finally` 里释放；进程被杀时连接断开、锁自动释放。
- **与登录、刷新、重置、2FA 并发**：清理只删「已终态且到期满 30 天」的行，这些行没有任何业务路径会再写它们；业务路径只按主键或哈希锁自己的行。刷新令牌按家族删，而可删的家族不会再有新行（续期需要出示未过期的令牌，家族里最晚的也已过期 30 天）。所以清理与业务之间没有写冲突。
- **删除语句的锁**（v4 重写）：清理事务是 `READ COMMITTED`（见 §2「事务边界」），选择主键与 `DELETE` 里的子查询都是非加锁的一致读，不加间隙锁；`DELETE … WHERE id IN (…)` 只对被删的行加记录锁。被删的行都是终态且到期满 30 天，业务路径（登录插入新家族、刷新按哈希条件更新活着家族里的行、重置按哈希消费未过期的令牌、2FA 只读未用的恢复码）不会读写它们，所以与业务没有锁交集。批次小（≤ 1000 行）、事务短；若仍遇到死锁或锁等待超时，本轮停止、下一轮重做（§5）。
- **数据库保证的唯一性**：不新增唯一约束。删除的幂等由条件保证：同一行删两次，第二次 `WHERE` 不命中、删除 0 行。
- **幂等键**：不需要。重跑同一轮只会删 0 行。

## 5. 失败与恢复矩阵

| 失败点 | 对外结果 | 数据最终状态 | 是否可重试 | 恢复来源 | 告警 |
| --- | --- | --- | --- | --- | --- |
| 配置不合法（模式值、上限越界） | 启动时配置校验失败（与其他配置项一致） | 不运行 | 改配置 | — | 部署失败即可见 |
| 某批删除时数据库报错（不可用、锁等待超时、死锁） | 无对外结果 | 当前批回滚；之前提交的批保留；本轮停止 | 下一轮自动重做 | 规则可重放 | ERROR 日志 `retention_purge_failed`（`item`、`error_type`）；数据库不可用另由 `readyz` 报出 |
| 写审计时失败 | 无 | 审计与这一批的删除同一事务，一起回滚：这一批的行仍在；之前提交的批各有自己的审计；本轮停止 | 下一轮自动重做 | 规则可重放 | ERROR 日志 `retention_purge_failed` |
| Redis / Celery 丢失、Beat 漏跑 | 无 | 表多长一天 | 下一轮自动补 | 规则可重放（INV-14） | 无（影响只是体量） |
| 外部服务超时 | 不适用 | — | — | — | — |
| 重复触发（两个实例） | 第二个拿不到命名锁、返回 | 不变 | — | — | INFO 日志 |
| 删除被外键或触发器拒绝 | 不可能（三张表没有入向外键与触发器，见 §1）；若将来有人加了，按上面「数据库报错」处理 | 当前批回滚、本轮停止 | 修代码 | — | ERROR 日志 |

## 6. 数据与安全边界

- **租户过滤**：清理按全局规则执行，不按租户；不返回任何行内容，不需要租户过滤。审计与日志只有项名、截止时刻、行数。
- **鉴权主体**：没有对外接口；由 worker 进程以运行账号执行（与其他任务相同）。数据库账号拆分尚未完成（`docs/TODO.md`），所以「不 `TRUNCATE`、不 `DROP`」目前靠代码约定与测试（静态扫描本模块不出现这两个词）。
- **客户侧禁止返回的字段**：不涉及。
- **日志、审计、异常**：不含令牌哈希、用户 id、邮箱；异常只记类型名。
- **密钥**：不读、不写任何密钥。
- **prompt / response**：不涉及。
- **数据保留与删除影响**：删除的是哈希过的令牌与恢复码，原值从未入库；删除后无法恢复（备份里仍有，按 K15 / K16 到期）。用户本身不受影响（`users` 行不动）。

## 7. 测试证据计划

| 风险/需求 | 测试层级 | 场景 | 预期结果 |
| --- | --- | --- | --- |
| 正常路径 | integration | 三项都 `delete`；造到期与未到期、终态与非终态的行 | 只删到期且终态的；每项一条审计（各一批），`after_state` 只有项名、截止时刻、批次号与删除数 |
| 刷新令牌家族 | integration | 家族 A 所有行过期满 30 天；家族 B 有一行过期满 30 天、最新一行未过期；家族 C 最新一行恰好在 `cutoff` | A 全删；B、C 一行不删 |
| 活家族排在前面 | integration | 活着的家族 B 有过期满 30 天的旧行且 `MIN(id)` 比可删家族 A 小，B 的最新一行未过期 | 本轮结束；A 全删、B 一行不动；没有 `deleted` 为 0 的审计；没有 `retention_purge_no_progress` 日志（B 不会被选为候选） |
| 无进展即停 | unit | 打桩让某项的 `select_ids` 返回主键、`delete_ids` 返回 0 | 这一项本轮结束、记一行 WARNING、不写审计；后面的项照常执行 |
| 重放检测不变 | integration | 家族 B 清理后，出示它过期满 30 天的旧行 | 仍吊销整个家族、写 `TOKEN_REUSED` |
| 邀请与重置令牌 | integration | 用户 A 有三条过期满 30 天的邀请；用户 B 有一条过期满 30 天的邀请与一条未过期的新邀请；另有两条过期满 30 天的重置令牌 | 删 A 的三条、B 的旧一条与两条重置令牌，B 的新邀请保留；A 的门户账号视图 `invited_at` 为 `null`，B 的为新邀请的时间 |
| 限频 | integration | 刚签发的重置令牌（窗口内） | 不删；限频照常生效 |
| 恢复码 | integration | 用掉满 30 天、吊销满 30 天、未用、用掉不满 30 天 | 只删前两种；2FA 校验照常 |
| 边界值 | unit | `expires_at`、`used_at`、`revoked_at` 恰好等于 `cutoff` | 不删（严格早于） |
| 批与上限 | integration | 2 500 行可删、上限 2 000 | 两批（各 1000）删 2 000 行、两条审计（批次 1、2）后停在上限；下一轮删余下 500、一条审计 |
| 大家族跨批 | integration | 一个可删家族 1 500 行 | 两批删完，中间状态下条件仍成立 |
| count 模式 | integration | 三项都 `count` | 一行不删、不写审计；结果与日志给出与 `delete` 模式相同的行数 |
| off 模式 | integration | 三项都 `off` | 任务直接返回，不执行任何 SQL（捕获语句断言） |
| 并发：两个实例 | integration（MySQL） | 一个连接先拿住命名锁，再跑任务 | 任务记 INFO 后返回、不删任何行 |
| 并发：与业务 | integration（MySQL） | 清理进行中（打桩让它在一批删除后暂停），另一线程完成一次登录、一次刷新、一次忘记密码申请 | 业务都成功；清理继续并只删到期终态行 |
| 锁范围（MySQL） | integration | 造一个可删家族与一个活着的家族（同一用户与另一用户各一）；打桩让一个删除批次的事务停在 `DELETE` 之后、提交之前；这期间另一个连接完成一次登录（插入新家族）、一次对活着家族的刷新、一次忘记密码申请；用 `performance_schema.data_lock_waits` 按 `DATABASE()` 与表名过滤 | 三个业务操作都在 5 秒内成功，期间 `data_lock_waits` 没有等 `refresh_tokens` / `password_reset_tokens` 的记录；清理批次随后提交 |
| 隔离级别（MySQL） | integration | 捕获清理事务在该连接上执行的语句并在事务内读 `@@transaction_isolation` | 删除批次的事务是 `READ-COMMITTED`；`refresh_tokens` 的 `DELETE` 的派生表带候选家族的 `IN` 条件（只在 `mysql` 分支断言字面量）；结束后连接隔离级别回到引擎默认值 |
| 幂等重放 | integration | 同一 `now` 连续跑两次 | 第二次删除 0 行、不写审计 |
| 事务中途失败 | integration | ① 第二批删除时注入异常；② 第二批写审计时注入异常 | 两种情况：第一批的删除与它的审计都在；第二批的行都还在、没有第二批的审计；本轮停止；下一轮补完 |
| 审计与删除一致 | integration | 任意一轮之后 | 每条 `DATA_RETENTION_PURGE` 审计的 `deleted` 之和等于实际减少的行数；没有删除行数为 0 的审计 |
| Redis/Celery 丢失 | 不适用 | 漏跑由下一轮补（上面「幂等重放」与「中途失败」覆盖规则可重放） | — |
| 租户越权 | 不适用 | 没有对外接口 | — |
| 金额精度 | 不适用 | 不涉及金额 | — |
| 安全 | unit | 静态扫描 `app/services/retention_purge.py` 与 `app/tasks/retention.py` | 不出现 `TRUNCATE`、`DROP`、`FOREIGN_KEY_CHECKS` |
| 注册 | unit | 读注册表 | 恰好三项、名字与配置键一一对应、保留期都是 30 天 |
| Beat | unit | 读 `celery_app` 配置 | 任务已在 `TASK_MODULES`；Beat 项为每天 20:17 UTC、`expires` 3600 |
| 审计清单 | unit | 现有的前后端审计动作逐项比对、实体类型归类守卫 | 都通过 |

四个变异必须变红：在候选家族查询的分组前加回 `WHERE expires_at < :cutoff`（「活家族排在前面」变为不结束或删错，用超时断言变红）；把刷新令牌改为按行删（「刷新令牌家族」与「重放检测不变」变红）；把「严格早于」改为「早于或等于」（「边界值」变红）；去掉 `READ COMMITTED` 的设置（「锁范围」变红）。

## 8. 迁移与上线

- **不加迁移**：三张表都没有 `expires_at` / `used_at` 上的索引，清理是每天一次、按批的后台查询，表的体量（刷新令牌每次登录与刷新一行）在可预见的范围内全表扫描可以接受；加索引会让每次登录、刷新多一次索引维护。若生产上 `count` 模式的耗时超过 60 秒，另开任务加索引（runbook 写明怎么看耗时）。
- **上线顺序**：合并即部署，三项缺省 `off`，部署后什么都不发生。然后按 runbook：管理员在 VPS 的 `.env` 把三项改为 `count` → 等一轮 → 看日志里的行数是否合理 → 改为 `delete`。改 `.env` 后需要重建 worker 与 beat 容器（runbook 写明命令）。
- **回滚**：把三项改回 `off`；或回滚代码。已删除的行无法恢复（备份里有），这是政策本身的效果。

## 9. 方案取舍

| 方案 | 优点 | 缺点 | 结论 |
| --- | --- | --- | --- |
| **采用**：刷新令牌按家族删 | 重放检测对活着的家族不变 | 长寿家族的旧行晚于 30 天才删 | 采用 |
| 刷新令牌按行删 | 简单、严格 30 天 | 活着的家族失去对最早被盗令牌的检测 | 否 |
| 给刷新令牌家族加绝对上限再按行删 | 两全 | 改认证规则，超出 R1 范围，要另过闸门 | 否（可作为以后的改进） |
| **采用**：邀请照 K8 全删 | 符合 K8 | 管理端「邀请时间」在邀请过期满 30 天后变空 | 采用（v2） |
| 保留每个用户最新一条邀请 | 邀请时间一直可见 | 一条令牌无限期留存，违反 K8 | 否（v1 的做法，#833 审查阻断） |
| 每项两个配置（开关 + 只数） | 与 ADR 字面一致 | 四种组合里有一种无意义（关 + 只数） | 否：合成三值 |
| 新增告警维度 | 失败可见 | Healthchecks 已满；失败只影响体量 | 否 |
| 加 `expires_at` 索引 | 查询快 | 每次登录与刷新多一次索引维护；体量还不需要 | 否（按实测再定） |

## 10. 未决问题与假设

我定的（§1 已列）：

1. 刷新令牌按家族删。
2. 邀请照 K8 全删，门户账号视图的邀请时间随之变空（v2）。
3. 开关与只数合成 `off` / `count` / `delete`。
4. 失败只写 ERROR 日志、不新增告警。
5. 保留期是代码常量。

假设：

- 生产三张表的行数在十万量级以内（`count` 模式的第一轮会给出实数，超出时按 §8 另开加索引任务）。
- `docs/data-governance.md` §4.4 与 §10 K7 里「刷新令牌绝对期限 12 小时」的说法与代码不符；本设计按代码（过期时间按行滑动）处理，AIH-TASK-265 更正文档措辞，不改批准的保留期。

## 11. 审查与版本绑定

### 实现拆分

| 任务 | 内容 | 主要文件 | 预计行数 |
| --- | --- | --- | --- |
| AIH-TASK-263 | 清理框架与三项清理、审计动作 | `app/services/retention_purge.py`、`app/models/auth.py`、`app/services/audit_query.py`、`frontend/src/api/adminAudit.ts`、测试 | ~900 |
| AIH-TASK-264 | Celery 任务、Beat、配置、`.env.example`、部署文档的配置清单 | `app/tasks/retention.py`、`app/core/celery_app.py`、`app/core/config.py`、`.env.example`、`docs/deployment.md`、测试 | ~400 |
| AIH-TASK-265 | runbook「数据留存清理」一节（`count` → `delete` 的上线步骤、看日志、出错处置、按 `entity_id` 汇总审计）；`docs/data-governance.md` 更正「12 小时」措辞、不存在的 §6.3 引用与 §6 第 4 款；ADR-0017 第 2 条第 4 款补完为按批审计；`docs/TODO.md` 勾掉 `password_reset_tokens` 那一半 | `docs/runbook.md`、`docs/data-governance.md`、`docs/adr/ADR-0017-data-retention.md`、`docs/TODO.md` | ~200 |

### 设计闸门判定

本设计碰认证与会话（删除认证令牌），档位 §1–§7，批准前不写代码。批准后登记 263..265，按控制面规则预审。

## 12. 版本变更记录

| 版本 | 日期 | 变更 |
| --- | --- | --- |
| v1 | 2026-11-23 | 初稿 |
| v2 | 2026-11-23 | #833 第一轮审查（Codex）两个阻断项：① 保留每个用户最新一条邀请会让它无限期留存，违反 K8 —— 邀请照 K8 全删，门户账号视图的邀请时间随之变空（接口与页面已支持空值）；② 删除先提交、审计另开事务，审计失败就永久缺失 —— 每个删除批次在同一事务里写一条审计，ADR-0017 第 2 条第 4 款由 AIH-TASK-265 补完措辞；测试补审计失败分支与审计、删除一致性 |
| v3 | 2026-11-23 | #833 第二轮审查（Claude）阻断项：§1 完成标准第 1、3 条与 §3 INV-8 一行仍是 v1 的写法（保留最新邀请、按轮一条汇总审计），与 v2 正文矛盾 —— 改为邀请照 K8 全删、每批一条与删除同事务的审计 `{item, cutoff, batch, deleted}` |
| v4 | 2026-11-23 | #833 第三轮审查（Claude）阻断项：刷新令牌删除语句的写法交给实现选，派生表或全表聚合在 `REPEATABLE READ` 下可能锁住整张表、挡住登录与刷新 —— 清理事务在 MySQL 上用 `READ COMMITTED`，`DELETE` 写法定死为只按候选家族经索引探查的派生表；§4 锁的说明重写；补「锁范围」「隔离级别」两条 MySQL 测试与一个变异 |
| v5 | 2026-11-23 | #833 第四轮审查（Claude）阻断项：候选家族查询在分组前过滤 `expires_at < :cutoff`，`HAVING` 恒为真，活家族被反复选中、删 0 行、循环不结束 —— 去掉分组前的过滤；循环加「选到却删 0 行即结束本项」与 WARNING 日志；补「活家族排在前面」「无进展即停」测试与一个变异 |
| v6 | 2026-11-23 | 登记时 `scripts/check_docs.py` 拒绝第 5 行把 data-governance.md 的第 0 节写成「§ 加数字」的引用（它把代码块外的「§ 加数字」一律当 spec 章节核对，spec 没有第 0 章）—— 「对应需求」一行引用 `docs/data-governance.md` 的节号改写为「第 N 节」，内容不变 |
| v7 | 2026-11-23 | v6 的变更记录本身又写出了同样的引用，再被 `scripts/check_docs.py` 拒绝 —— 只改写这一行变更记录的措辞，内容不变 |

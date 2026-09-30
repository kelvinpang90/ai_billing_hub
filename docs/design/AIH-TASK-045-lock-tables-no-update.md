# AIH-TASK-045 设计：两张锁表禁止 UPDATE（pricing_rule_locks 与 fx_rate_locks）（已批准 v1）

> **来源**：设计闸门 Issue #200。本文件是 `APPROVED: design v1` 那一版正文的**逐字副本**，审查者为 Codex。
> 放进仓库，是因为 OpenClaw Worker 在沙箱里不联网、读不到 GitHub Issue。
> **实现以本文件为准**；与 Issue 不一致时，以 Issue #200 上被批准的 v1 为准。设计要改，就回到 Issue 升版本、重新过闸门，不要直接改本文件。
> 正文里「预计 `AIH-TASK-045`」的实现任务已登记为 `AIH-TASK-045`；与 Issue 正文没有差别（正文里的两个相对链接从本目录出发同样指向仓库根下的文件）。

---

## 0. 准入判定

| 任务涉及 | 要填的范围 |
| --- | --- |
| 钱包 / 账本 / 定价 / 汇率 / 支付 / 幂等 / 状态机 | **全部章节** |
| 用量摄取 / 认证与会话（集成侧与平台侧）/ Webhook | §1–§7（**§6 不可省**） |
| 前端 / 文档 / CI / 脚本 | 不走本闸门 |
| 既不碰钱、也不碰用量摄取 / 认证与会话 / Webhook 的后端改动 | 不走本闸门 |

本任务档位：`全部`。两张锁表分别是定价规则（#178）与汇率（#183）发布、退役与计费的串行化锁，属于定价 / 汇率。

---

状态：`READY_FOR_REVIEW`
负责人：Claude
**设计版本**：`v1`
对应需求：spec §17.1、§132 第 13 条；INV-6；设计闸门 #178（`APPROVED: design v4`）§2 `pricing_rule_locks`、#183（`APPROVED: design v3`）§2 `fx_rate_locks`
目标 PR：待开（批准后在 `.platform/tasks.yaml` 登记一个实现任务，编号登记时分配，预计 `AIH-TASK-045`，排在「当前计划」第 1 条）

## 1. 目标与边界

- **要解决的问题**：`pricing_rule_locks`（迁移 0014）与 `fx_rate_locks`（迁移 0015）都是「恰好一行 `id = 1`」的锁表。两份设计只要求拒绝 INSERT（已有一行时）与 DELETE，迁移照做，**没有拒绝 UPDATE**。直接执行 `UPDATE fx_rate_locks SET id = 2` 就能把唯一那一行移走，之后：
  - 两张版本表 BEFORE UPDATE 触发器里的 `SELECT … WHERE id = 1 FOR UPDATE` 锁不到任何行、也不报错，绕过服务的直接写入不再被串行，区间不重叠检查退化成无锁的检查（两条并发的直接写入可能都通过）
  - 服务层的 `lock_pricing_rules` / `lock_pricing_rules_shared`（以及 F2 起汇率的同类函数）用 `scalar_one()`，锁行不在时抛异常、fail closed —— 发布、退役失败；T-H 接上后计费也全部失败，直到有人把行改回来
  - 这个缺口由 PR #199（收尾 AIH-TASK-028）的 Codex 审查指出，#197 的 Worker 审查也记为非阻断观察。Kelvin 2026-09-30 决定先修再收尾 028
- **可观察的完成标准**：
  1. 迁移 0016 之后，对两张锁表的任何 UPDATE（改 `id`、`SET id = 1` 这种不改值的写法、带不带 WHERE）都被数据库拒绝，`SQLSTATE 45000`；锁表仍恰好一行 `id = 1`
  2. 锁定读（`FOR UPDATE` / `FOR SHARE`）照常工作：它们不是 UPDATE 语句，不触发触发器；服务层发布、退役与既有全部测试不变
  3. `downgrade` 只删这两个触发器
- **明确不做什么**：
  - 不改 0014 / 0015 已有的任何触发器、约束、表结构，不改 ORM 模型与服务代码
  - 不处理 DDL 级的绕过（`TRUNCATE`、`DROP TRIGGER`、`ALTER TABLE`）：它们不经触发器，而且能执行它们的账号同样能删掉本任务加的触发器；0006 / 0014 / 0015 的文件头都已把它记为残余风险，靠的是「数据库账号权限拆分」（规划块「后续计划」已有一条）
  - 不改 025 / 026 的价格版本：它们用供应商行做锁，不是单行锁表，没有这个问题
- **现有行为与问题证据**：`alembic/versions/20260929_0014_pricing_rules.py` 与 `20260929_0015_fx_rates.py` 的 `_TRIGGERS` 里，锁表各只有 `_before_insert` / `_before_delete` 两个；`tests/backend/test_migrations.py` 的 0014 / 0015 触发器集合同样只有这两个。全仓库没有任何代码对锁表执行 UPDATE（只有 `SELECT … FOR UPDATE` / `FOR SHARE` 与建表时写入那一行）。

## 2. 设计概要

```text
部署：预检（触发器权限）→ CREATE TRIGGER trg_pricing_rule_locks_before_update → CREATE TRIGGER trg_fx_rate_locks_before_update → alembic_version = 0016
运行：任何 UPDATE 两张锁表 → BEFORE UPDATE → SIGNAL 45000 → 语句失败、行不变
      SELECT … FOR UPDATE / FOR SHARE → 不触发 → 照常加锁
```

- **迁移**：`alembic/versions/20260930_0016_lock_tables_no_update.py`，revision `0016_lock_tables_no_update`，`down_revision` = `0015_fx_rates`。只建两个触发器，写法照 0014 / 0015 的锁表 DELETE 触发器（单条 `SIGNAL`，整条 `CREATE TRIGGER` 是一条语句）：

```sql
CREATE TRIGGER trg_pricing_rule_locks_before_update
BEFORE UPDATE ON pricing_rule_locks
FOR EACH ROW
SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'pricing_rule_locks holds exactly one row'

CREATE TRIGGER trg_fx_rate_locks_before_update
BEFORE UPDATE ON fx_rate_locks
FOR EACH ROW
SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'fx_rate_locks holds exactly one row'
```

  - 无条件拒绝，不判断 `id` 是否变化：锁表只有 `id` 一列，这一行只该被锁、永远不该被写；放行「不改值的 UPDATE」没有任何用途，只会多一个分支
  - 错误文本沿用同一张表 INSERT / DELETE 触发器的文本，三种写法报同一句话
  - 先做 0006 同款的触发器权限预检（`@@log_bin` / `@@log_bin_trust_function_creators`），在任何 DDL 之前；`--sql` 离线模式跳过预检（与 0015 相同）
  - 文件头写 §132 第 13 条分析（见 §8）
- **其他写入路径**：`INSERT … ON DUPLICATE KEY UPDATE` 与 `REPLACE` 先经 BEFORE INSERT，已有一行时被既有触发器拒绝；没有外键引用两张锁表，不存在 `ON UPDATE CASCADE` 路径
- **ORM**：不变。触发器不在模型里声明；SQLite 单元测试不建触发器，与 0014 / 0015 一样
- **API、事务边界、外部系统、时间语义、金额**：都不变。本设计只加数据库兜底，不改任何读写顺序与锁顺序（供应商 S → FX S → 规则 S → 钱包 → 租户 不变）
- **迁移序号**：`.platform/tasks.yaml` 第 1735 行的注释与 `AIH-TASK-029`、`AIH-TASK-032` 的契约把 0016 / 0017 预留给用量摄取与异步计费。本任务占 0016，登记 PR 同时把 029 改成 `20260929_0017_usage_events.py`（`down_revision` 0016）、032 改成 `20260929_0018_usage_billing.py`（`down_revision` 0017），允许路径与验收条件同步。029 / 032 的设计文件写的是「序号取实现时的下一个」，设计本身不用改；两个任务都还是 `ready`，没有开始实现

## 3. 不变量影响矩阵

| 不变量 | 是否触碰 | 可能怎样被破坏 | 设计控制 | 验证方式 |
| --- | --- | --- | --- | --- |
| INV-1 中心故障不中断 AI | 否 | 本任务只加拒绝写锁表的触发器；集成侧不访问这两张表 | — | — |
| INV-2 事件不重复扣费 | 否 | 不改计费路径与事件状态 | — | — |
| INV-3 支付不重复入账 | 否 | 不涉及支付 | — | — |
| INV-4 余额只经账本变动 | 否 | 不涉及钱包与账本 | — | — |
| INV-5 历史账本不可变 | 否 | 不涉及账本 | — | — |
| INV-6 事件保留版本引用 | **是（加强）** | 锁行被移走 → 直接写入不再与发布、计费串行 → 同一时刻可能先后解析到不同版本；服务层 fail closed 时计费全部失败 | 两张锁表拒绝 UPDATE，与既有 INSERT / DELETE 触发器一起，DML 范围内锁表恒为 `{1}` | §7 前四行 |
| INV-7 客户不可见成本毛利 | 否 | 不新增任何读接口与返回字段 | — | — |
| INV-8 租户不可互访 | 否 | 锁表是全局的，不含租户数据 | — | — |
| INV-9 对话内容不入库 | 否 | 不写任何业务数据 | — | — |
| INV-10 金额用 Decimal | 否 | 没有金额 | — | — |
| INV-11 event_id 至多一次财务效果 | 否 | 不改事件处理 | — | — |
| INV-12 定稿对账单不可变 | 否 | 不涉及对账单 | — | — |
| INV-13 状态与事件原子提交 | 否 | 不改任何事务的写入集合 | — | — |
| INV-14 队列丢失不毁持久工作 | 否 | 不涉及队列 | — | — |

## 4. 状态与并发

- 无状态机：锁表只有一行、没有状态列
- **串行化不变**：发布 / 退役仍以排他锁、计费仍以共享锁锁同一行；版本表触发器仍先 `FOR UPDATE` 这一行。锁定读不是 UPDATE 语句，不触发新触发器，所以没有新的等待、没有新的死锁顺序
- **数据库负责的唯一性**：锁表的「恰好一行 `id = 1`」由主键 + BEFORE INSERT（已有一行时）+ BEFORE DELETE + 本设计的 BEFORE UPDATE 共同保证（DML 范围内）
- 幂等键、相同 ID 不同载荷、重试：不适用（没有接口、没有异步任务）
- 原子性：迁移里两条 `CREATE TRIGGER` 各自隐式提交（见 §5、§8）

## 5. 失败与恢复矩阵

| 失败点 | 对外结果 | 数据最终状态 | 是否可重试 | 恢复来源 | 告警 |
| --- | --- | --- | --- | --- | --- |
| 预检失败（binlog 开着、`log_bin_trust_function_creators` 关着） | 部署中止（`deploy.sh` 迁移失败即中止），旧镜像继续跑 | 什么都没建，`alembic_version` 仍是 `0015_fx_rates` | 是：打开开关后重新部署 | 迁移本身 | Deploy workflow 失败 |
| 第一个触发器建成、第二个失败或进程被杀 | 同上 | `pricing_rule_locks` 已拒绝 UPDATE，`fx_rate_locks` 未拒绝，`alembic_version` 仍是 0015；重跑撞 "Trigger already exists" | 是：`DROP TRIGGER IF EXISTS trg_pricing_rule_locks_before_update` 后重新部署（文件头写明） | 迁移本身 | Deploy workflow 失败 |
| 上线后有人直接 UPDATE 锁表 | 语句报 `45000`，事务由调用方决定回滚 | 锁表不变 | 否（本就不该写） | — | 不另设告警：只能由有库写权限的人手工触发，报错当场可见 |
| 校验失败 / DB 提交失败 / 外部服务超时 / Redis、Celery 丢失 / 重复请求 | 不适用：本任务没有接口、没有业务写入、没有外部调用、没有异步任务 | — | — | — | — |

## 6. 数据与安全边界

- 租户过滤：不适用，锁表全局、不含租户数据
- 鉴权主体：迁移由部署账号执行；触发器对所有账号生效（包括应用运行账号与直接连库的人）
- 客户侧返回字段、日志与审计内容：不变；触发器的错误文本只含表名
- 密钥：不涉及（ADR-0004 不受影响）
- prompt / response 与客户数据：不涉及
- 数据保留与删除：不涉及。残余风险同 0006 / 0014 / 0015：`TRUNCATE` / `DROP TRIGGER` / `ALTER TABLE` 是 DDL，不经触发器；运行账号与迁移账号拆分之前，拿到运行账号就能执行它们 —— 由「后续计划」的账号权限拆分处理，不在本设计范围

## 7. 测试证据计划

真 MySQL（`BILLING_TEST_DATABASE_URL`，未设置时 skip，CI 必跑），每条拒绝用例配一条证明不误杀的通过用例；每个用例在回滚的事务里。新文件 `tests/backend/test_lock_tables_db.py`，两张表各跑一遍（参数化）：

| 风险/需求 | 测试层级 | 场景 | 预期结果 |
| --- | --- | --- | --- |
| 正常路径 | integration（真 MySQL） | `UPDATE <锁表> SET id = 2 WHERE id = 1` | 被拒绝，`SQLSTATE 45000`，文本 `<锁表> holds exactly one row`；之后 `SELECT id` 仍是 `[1]` |
| 边界值 | integration | `UPDATE <锁表> SET id = 1`（不改值）、`UPDATE <锁表> SET id = id + 1`（无 WHERE） | 都被拒绝，行不变 |
| 不误杀 | integration | 一个连接 `SELECT id FROM <锁表> WHERE id = 1 FOR UPDATE`；另一个连接在前者提交后 `FOR SHARE` | 锁定读都返回 `[1]`、不报错 |
| 其他写法 | integration | `INSERT … ON DUPLICATE KEY UPDATE id = 2`、`REPLACE INTO <锁表> (id) VALUES (1)` | 都被既有 BEFORE INSERT 拒绝，行不变 |
| 迁移 | integration（真 MySQL） | 从 0015 `upgrade` 到 0016、再 `downgrade` 到 0015 | 升级只多出这两个触发器，降级只少这两个；表、行、其他触发器不变 |
| 迁移（结构） | unit（`test_migrations.py`） | 版本链 0015 → 0016；两个触发器的表、时机、事件；预检在任何 DDL 之前；`downgrade` 只删这两个；既有用例里按「截至某版本的全部触发器 / 表」断言的集合同步算上 0016 | 通过 |
| 并发 | — | 不适用：本任务不改任何锁顺序；发布与计费的并发用例由 027 与 F2b（AIH-TASK-044）覆盖，不重复 | — |
| 幂等重放 / 事务中途失败 / Redis、Celery 丢失 / 租户越权 / 金额精度 | — | 不适用：没有接口、异步任务、租户数据与金额 | — |

既有测试必须全部不改地通过（`test_pricing_rules_service.py`、`test_fx_rates_db.py` 等），证明服务层与既有触发器没有被影响。

## 8. 迁移与上线

- **数据迁移步骤**：预检 → 两个 `CREATE TRIGGER`。不 ALTER、不读写任何行
- **锁表与性能影响**：`CREATE TRIGGER` 对目标表取短暂的元数据锁，要等进行中的事务结束；两张锁表上最长的事务是发布 / 退役（毫秒级），F2 / T-H 接上前没有计费事务持锁。不需要停机
- **兼容窗口**：旧代码从不 UPDATE 锁表，迁移先于换镜像，没有兼容问题
- **部署顺序**：`deploy.sh` 先迁移、再换镜像（与既有迁移相同）
- **回滚或前滚**：`downgrade` 删两个触发器，不碰数据；回滚后回到今天的状态（缺口重新出现，但不丢数据）
- **部分部署时的行为**：见 §5 第二行；文件头写清手工清场步骤
- **备份**：要求前置备份（与 0005–0015 同一口径），虽然本迁移不改任何数据
- **监控指标和告警负责人**：不新增。部署失败由 Deploy workflow 报出，Kelvin 负责

## 9. 方案取舍

| 方案 | 优点 | 风险 | 未采用原因 |
| --- | --- | --- | --- |
| 采用：BEFORE UPDATE 触发器，无条件拒绝 | 与同表 INSERT / DELETE 触发器同一写法；不改模型与既有对象；表达的就是「这行只锁不写」 | 需要触发器权限（已有预检） | — |
| 备选 A：`CHECK (id = 1)` | 声明式；不需要触发器权限 | 要改两个 ORM 模型让 SQLite 与 MySQL 一致，`test_migrations.py` 的 CHECK 对比随之变化；放行不改值的 UPDATE | 防护效果与采用方案相同，改动面更大（多两个模型文件与两次 ALTER TABLE），且与同表另外两条规则的写法不一致 |
| 备选 B：让版本表的 BEFORE UPDATE 触发器在锁不到行时 SIGNAL | 连 `TRUNCATE` 后的空表也 fail closed | 要 DROP + 重建 0014 / 0015 里两个最复杂的触发器，重新验证已批准的全部规则 | 多出的只是对 `TRUNCATE` 的防护，而能 `TRUNCATE`（需 DROP 权限）的账号同样能删掉触发器，没有实际收益 |
| 备选 C：在 #178 / #183 各自升版本、分两个迁移 | 每份设计自成一体 | 两次闸门、两个任务、两个迁移号，内容完全对称 | Kelvin 2026-09-30 选新开一个闸门一次审完 |

## 10. 未决问题与假设

- 未决问题：无
- 尚未验证的假设：
  - `SELECT … FOR UPDATE` / `FOR SHARE` 不触发 BEFORE UPDATE —— MySQL 的触发器只对 INSERT / UPDATE / DELETE 语句触发，这是既定语义；§7「不误杀」一行在 CI 的真 MySQL 上验证
  - 全仓库没有代码 UPDATE 锁表 —— 已用 `git grep` 核对；§7 要求既有测试不改地全部通过作为运行时证据
- 需要谁拍板：Codex 审设计；登记与合并由 Kelvin
- 如果假设错误，具体后果：锁定读若被拒绝，发布 / 退役会全部失败（fail closed，不产生错误数据），CI 的真 MySQL 用例会先挡住

## 11. 审查与版本绑定

审查方要回答的五个问题常驻在 [scripts/review_checklist.md](../../scripts/review_checklist.md) 的「设计审查」一节，此处不重复。

**版本绑定规则**（规则的出处是 [WORKFLOW §3](../../docs/WORKFLOW.md)；这里保留一份是因为审查材料里只有本 Issue 正文，两处改一起改）：

- Codex 的批准必须写成 `APPROVED: design v<N>`，`<N>` 取本 Issue 顶部的「设计版本」
- **设计版本一变，之前的 APPROVE 自动作废**，必须重新过闸门
- 什么算「实质修改」：改了契约、表结构、事务边界、状态机、失败语义、不变量控制。改错别字不算
- 实质修改时：把顶部版本 +1，在下方追加一条变更说明，状态退回 `READY_FOR_REVIEW`

**与既有设计的关系**：批准后，登记 PR 把本设计的批准版副本放进 `docs/design/`（文件名随登记编号），并在 `AIH-TASK-027-pricing-rules.md` 与 `AIH-TASK-028-fx-rates.md` 讲锁表的那一句后各加一行指向它；#178 v4 与 #183 v3 的正文与批准不变。

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
| v1 | 2026-09-30 | 初稿 | PR #199 的 Codex 审查阻断：两张锁表可被 UPDATE 移走锁行 |

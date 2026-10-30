# AIH-TASK-143 至 144 设计：计价解析少发重复的 SQL（已批准 v1）

> **来源**：设计闸门 Issue #528。本文件是 `APPROVED: design v1` 那一版正文的**逐字副本**。审查经过见 §12：v1 由 Codex 首轮批准，没有阻断项；批准后按登记 PR #530 的审查建议在「对应需求」补了 `REQ-PRICE-001`（勘误，版本不变）。每一版送审之前都先跑了登记前预审，发现都已并入。
> 放进仓库，是因为 OpenClaw Worker 在沙箱里不联网、读不到 GitHub Issue。
> **实现以本文件为准**；与 Issue 不一致时，以 Issue #528 上被批准的 v1 为准。设计要改，就回到 Issue 升版本、重新过闸门，不要直接改本文件。
> **前置**：设计闸门 #178 v4（定价规则）、#179 v1（计价引擎）、#181 v2（[AIH-TASK-032-usage-billing.md](AIH-TASK-032-usage-billing.md)，异步计费 worker）、#507 v4（项目级规则）；测量依据是 [perf-baseline.md](../perf-baseline.md) 10.6 与 AIH-TASK-141 上线后的重跑。
> §11「任务拆分」的两个实现任务登记为 `AIH-TASK-143`（规则解析一条语句）→ `AIH-TASK-144`（计费不再重复解析）。

---
## 0. 准入判定

| 任务涉及 | 要填的范围 |
| --- | --- |
| 钱包 / 账本 / 定价 / 汇率 / 支付 / 幂等 / 状态机 | **全部章节** |
| 用量摄取 / 认证与会话（集成侧与平台侧）/ Webhook | §1–§7（**§6 不可省**） |
| 前端 / 文档 / CI / 脚本 | **不走本闸门**，直接开 PR |
| 既不碰钱、也不碰用量摄取 / 认证与会话 / Webhook 的后端改动 | **不走本闸门**，直接开 PR |

本任务档位：`全部`

理由：改的是计费事务里的定价规则解析（`resolve_pricing_rule`）与计价输入解析（`resolve_charge_inputs`）怎样读库、读几次、加哪些共享锁。选到哪条规则、哪个价格版本、哪个汇率决定扣多少钱，属于定价；它们在扣费的同一事务里加锁读，属于钱包与幂等的并发面。所以按全部章节填。

---

状态：`READY_FOR_REVIEW`
负责人：Claude
**设计版本**：`v1`（每次实质修改 +1，见 §12）
对应需求：`REQ-INGEST-002`、`REQ-PRICE-001` / spec §16、§59、§82、§119；设计闸门 #178 v4（定价规则）、#179 v1（计价引擎）、#181 v2（异步计费 worker）、#507 v4（项目级规则）
目标 PR：待开

## 1. 目标与边界

- **要解决的问题**：计费处理一条用量事件要发 28 条 SQL，其中计价解析 `resolve_charge_inputs` 占 16 条、约 19–21 ms，是单条事件里最大的阶段（`docs/perf-baseline.md` 10.6 结论第 4 条）。逐条计时找到两处纯粹的重复往返：
  1. **规则解析逐级各查一次。**`app/repositories/pricing_rules.py` 的 `resolve_pricing_rule` 按 `SCOPE_ORDER` 的八级，每一级发一条 `SELECT … FOR SHARE`，第一级命中才停。生产库目前只有一条规则，在最低的 `GLOBAL` 级（第 8 级，2026-10-30 只读核对），所以生产上每条事件要查 8 次，前 7 次都是空结果；压测与剖析用的是 `GLOBAL_PROVIDER_MODEL` 规则（第 7 级），查 7 次。
  2. **计费处理把「模型 + 两把单行共享锁」取了两遍。**`app/services/usage_billing.py` 的 `_process` 在步骤 ③④ 先调 `catalog.resolve_model`、`rates.lock_fx_rates_shared`、`rules.lock_pricing_rules_shared`（为了在取 `now_r` 之前拿到三把共享锁），丢掉 `resolve_model` 的返回值；步骤 ⑥ 调 `resolve_charge_inputs(locking=True)`，后者又把这 4 条语句发一遍（锁已持有，是重入，但每条都是一次往返）。
- **可观察的完成标准**：
  1. `resolve_pricing_rule` 无论命中哪一级、还是都不中，都只发**一条**读 `pricing_rules` 的语句（另加命中后读分量的一条，与今天相同）；选中的规则、各级的先后、错误语义与今天逐级查完全相同。
  2. 计费处理一条事件，`resolve_model`（2 条语句，经别名时 4 条）与两把单行共享锁各只执行一次；锁顺序仍是 供应商（S）→ `fx_rate_locks`（S）→ `pricing_rule_locks`（S）→ 取 `now_r` → 价格 / 汇率 / 规则 → 钱包。
  3. 在剖析的形态（`GLOBAL_PROVIDER_MODEL` 规则、USD 价格、模型代码直接命中）下，每条事件的 SQL 从 28 条降到 18 条（规则少 6 条、重复少 4 条）；生产现在的 `GLOBAL` 规则下少 11 条。由 `scripts/perf_usage_profile.py` 的「每条事件 SQL 条数」读出。
  4. 上线后在 VPS 上重跑 `docs/perf-baseline.md` 10.6 的剖析，`resolve_charge_inputs` 的平均耗时低于本节记录的值（1 个 worker、Beat 节奏 21.37 ms），每条事件 SQL 条数为 18。这一条是上线后的运维验证，不是 CI 门槛。
- **明确不做什么**：
  - 不改规则、价格版本、汇率的选择语义：八级顺序、半开区间、`PUBLISHED` / `RETIRED` 才参与、空区间永不匹配、命中的 FIXED_RATE 缺分量不下落、项目为空时不看项目级，全部不变。
  - 不改任何表结构、索引、迁移、触发器。
  - 不改价格版本与分量、规则与分量的读法（各仍是两条语句）；不合并计量分量的读；不加进程内缓存（见 §9）。
  - 不改试算（`preview_charge`，`locking=False`）的调用方式与结果。
  - 不改认领、扣费、快照、卡住回收；不改扫描的派发与名额（#522 v4）。
  - 不做方案 C（同租户批事务）；不改 worker 与 MySQL 的 CPU 配额。
- **现有行为与问题证据**：
  - `docs/perf-baseline.md` 10.6（2026-10-30，生产 VPS，`billing_perf`）：`resolve_charge_inputs` 平均 19.37 ms（1 个 worker、Beat 节奏），每条事件 28 条 SQL，数据库耗时约占墙钟 46%。
  - AIH-TASK-141 上线后（2026-10-30 重跑同一剖析，AIH-TASK-142 的脚本）：Beat 节奏下 1 个 worker 23.2 条每秒、2 个 worker 29.9 条每秒；`resolve_charge_inputs` 21.37 / 27.35 ms；每条事件仍是 28 条 SQL。派发不再是上限（2 个 worker 时 Beat 节奏空转 20.5%），单条事件成本成了主要项。
  - 同日在 `billing_perf` 上对一条已处理的剖析事件（`GLOBAL_PROVIDER_MODEL` 规则）重复调用 `resolve_charge_inputs(locking=True)` 300 次（每次回滚，只读）：每次 16 条语句、数据库耗时 17.44 ms。其中读 `pricing_rules` 的 7 条合计约 9.1 ms（前 6 条空结果，各约 1.28 ms），供应商、模型、两把单行锁 4 条合计约 2.5 ms。cProfile 里 `resolve_pricing_rule` 占 `resolve_charge_inputs` 累计时间的 57%。

## 2. 设计概要

```text
_process（计费事务，调用方的 session_scope）
  ① 事件行（X）+ 防护令牌     ② 租户（普通读）
  ③ model = catalog.resolve_model(...)            ← 供应商行 S 锁；返回值不再丢掉
  ④ lock_fx_rates_shared → lock_pricing_rules_shared
  ⑤ now_r = clock()；未到期 → NOT_DUE
  ⑥ resolve_charge_inputs(..., locking=True, resolved_model=model)
        └─ 给了 resolved_model：跳过它自己的 resolve_model 与两把锁（调用方已按顺序持有）
           计量分量 → 价格版本 + 分量 → 汇率（非 MYR）→ resolve_pricing_rule（一条语句选级）+ 规则分量
  ⑦ price_resolved   ⑧ post_transaction（钱包 X → 租户 X）   ⑨ 快照、PROCESSED
```

### 改动一：`resolve_pricing_rule` 一条语句选级（`app/repositories/pricing_rules.py`）

- 签名、返回值、文档里的调用方契约不变。
- 先按今天的规则算出要查的级：`SCOPE_ORDER` 里、`project_id is None` 时去掉三个项目级（与今天 `continue` 的那几级相同）。对每一级用 `_level_scope` 得到范围、用 `_in_scope` 得到它的五个等值条件（今天每一级的 WHERE 就是这五条加下面三条）。
- 发**一条**语句：

  ```python
  in_effect = select(PricingRule).where(
      or_(*(and_(*_in_scope(_level_scope(level, ...))) for level in queried)),
      PricingRule.status.in_(PERIOD_STATUSES),
      started,
      not_ended,
  )
  found = session.execute(_shared(in_effect)).scalars().all()
  ```

  `_shared` 不变（`FOR SHARE` + `populate_existing`）。三条公共条件（状态、`started`、`not_ended`）与今天每一级的写法逐字相同。
- 在 Python 里选级：按 `queried` 的顺序（即 `SCOPE_ORDER` 的顺序）找第一个有命中行的级；命中行的级别以行的 `priority_scope` 判定。
  - 没有任何行 → 返回 `None`（与今天各级都不中相同）。
  - 选中的级恰好一行 → `_resolved(session, rule)`（不变，读分量的那一条仍单独发）。
  - 选中的级多于一行 → 抛 `sqlalchemy.exc.MultipleResultsFound`（与今天那一级的 `scalar_one_or_none` 抛的异常类型相同）。同一范围的非空区间不重叠由服务层在规则锁内保证、触发器兜底，这只可能是数据损坏。
  - **比选中级更低的级**有多行时不报错：今天那些级根本不查，行为相同。
- 一行只可能落在一个级里：`_in_scope` 的第一条就是 `priority_scope == level`，而一条规则只有一个 `priority_scope`。所以「按行的 `priority_scope` 分组」与「按命中了哪个 OR 分支分组」是同一回事。
- 为什么结果与逐级查完全相同：逐级查返回第一个非空级的那一行；一条语句返回所有级的命中行的并集，再取第一个非空级。两者对每一级用的是同一组条件，并集按级拆开后每一级的行集合与逐级查那一级的行集合相同（同一事务、同一组锁内的加锁读，都读最新提交）。

### 改动二：`resolve_charge_inputs` 接收调用方已解析的模型（`app/services/pricing_engine.py`、`app/services/usage_billing.py`）

- `resolve_charge_inputs` 新增关键字参数 `resolved_model: AiModel | None = None`：
  - `None`（默认）：行为与今天完全相同（试算、现有测试、`MODEL_UNKNOWN` 的路径都走这里）。
  - 给了值：**必须** `locking=True`，否则 `ValueError("a resolved model is only for the billing transaction")`。函数不调用 `catalog.resolve_model`，也不调用 `lock_fx_rates_shared` / `lock_pricing_rules_shared`；把 `resolved_model` 当作 `resolve_model` 的结果，其余步骤（计量分量、价格、汇率、规则）与今天相同。`ResolvedModelRef` 照今天的写法由它构造：`matched_via` 仍按 `model.code == model_code` 判定，`provider_code` 仍取参数。
  - 契约写进文档：给了 `resolved_model`，就表示调用方已在**同一事务**里按顺序执行过 `resolve_model(provider_code, model_code, occurred_at)`（得到的就是这个对象）、`lock_fx_rates_shared`、`lock_pricing_rules_shared`。函数不复查这一点（复查就要再发语句，正是要省掉的）；唯一的调用方是 `_process`，由 §7 的用例钉住。
- `_process` 改两处：
  - 步骤 ③ 保存返回值：`model = catalog.resolve_model(...)`。注释照旧：供应商代码未知时没有锁可拿，后面照样判 `MODEL_UNKNOWN`。
  - 步骤 ⑥ 传 `resolved_model=model`。`model` 为 `None`（模型未知）时传的就是 `None`，`resolve_charge_inputs` 走默认路径、自己再解析一次并返回 `MODEL_UNKNOWN`。这条路径与今天相同（多发 1–3 条语句），是罕见路径，不为它另写分支。
- 步骤顺序、`now_r` 的位置、`NOT_DUE` 的处理、之后的 ⑦⑧⑨ 都不变。

### 其余

- **输入、输出和错误契约**：`resolve_pricing_rule` 不变。`resolve_charge_inputs` 只多一个可选关键字参数，返回值不变；新增一个 `ValueError`（`resolved_model` 与 `locking=False` 同时给）。
- **新增或修改的 API**：无（HTTP 接口不变）。
- **数据库表、字段、索引及唯一约束**：不变。一条语句选级仍走现有的 `ix_pricing_rules_resolve`（`priority_scope, tenant_id, provider_id, model_id, effective_from`）或优化器挑的其他索引；正确性不依赖执行计划（见 §4）。
- **事务边界**：不变。全部在 `_process` 的计费事务里；试算在它自己的只读事务里。
- **外部系统与异步任务边界**：无。
- **时间语义**：`occurred_at` 的半开区间比较原样复用今天的 `started` / `not_ended`；`now_r` 仍在三把共享锁之后取。
- **金额精度、舍入位置及币种转换**：不涉及。`price_event` 与它的输入不变。

## 3. 不变量影响矩阵

| 不变量 | 是否触碰 | 可能怎样被破坏 | 设计控制 | 验证方式 |
| --- | --- | --- | --- | --- |
| INV-1 中心故障不中断 AI | 否 | 计费是异步 worker，与 AI 调用路径无关 | 不改 AI 模块与摄取 | — |
| INV-2 事件不重复扣费 | 否 | 认领、防护令牌、账本唯一约束都不改 | 只改同一事务里读定价输入的语句条数 | 现有 `test_usage_billing_concurrency.py` 全部照跑 |
| INV-3 支付不重复入账 | 否 | 不涉及支付 | — | — |
| INV-4 余额只经账本变动 | 否 | 扣费仍只经 `post_transaction` | 步骤 ⑧ 不改 | 现有用例 |
| INV-5 历史账本不可变 | 否 | 不写账本以外的东西，不改已有行 | — | — |
| INV-6 事件保留版本引用 | **是** | 一条语句选错级（例如选了更低一级、或漏掉某级），快照里的 `pricing_rule_id` 就错；跳过重复解析时用了不同的模型对象，`provider_id` / `model_id` 就错 | 选级按 `SCOPE_ORDER` 取第一个非空级，级别以行的 `priority_scope` 判定；`resolved_model` 就是同一事务里 `resolve_model` 返回的对象 | §7 的八级逐级下落用例（SQLite 与 MySQL）、项目为空、跨租户 / 跨项目用例；`resolved_model` 给与不给结果逐字段相同 |
| INV-7 客户不可见成本毛利 | 否 | 不改任何对外接口 | — | — |
| INV-8 租户不可互访 | **是** | 一条语句里的 OR 写错，可能把别的租户 / 项目的规则拿来计费 | 每个 OR 分支就是今天那一级的 `_in_scope` 五个等值条件（含 `tenant_id`、`project_id`），逐字复用，不新写条件 | 现有「一个租户的规则不给另一个租户计费」「一个项目的规则不给另一个项目 / 另一个租户」三个用例照跑并断言单条语句 |
| INV-9 对话内容不入库 | 否 | 不处理 prompt / response | — | — |
| INV-10 金额用 Decimal | 否 | 不新增任何金额计算 | — | 现有静态检查用例 |
| INV-11 event_id 至多一次财务效果 | 否 | 同 INV-2 | — | 同 INV-2 |
| INV-12 定稿对账单不可变 | 否 | 对账单尚未实现，本设计不写任何账期数据 | — | — |
| INV-13 状态与事件原子提交 | 否 | 快照、`PROCESSED` 与扣费仍在同一事务 | 不改事务边界 | 现有用例 |
| INV-14 队列丢失不毁持久工作 | 否 | 不改派发与认领 | — | — |

## 4. 状态与并发

不新增状态，不改任何跃迁（用量事件的状态机见 #181 v2，原样）。

- **并发写入如何串行化**：不变。规则的发布与停用先拿 `pricing_rule_locks` 的排他锁，计费在 `resolve_model` 之后拿它的共享锁，二者严格先后（#178 v4 §2「为什么与计费不会交错」）；价格与供应商行锁、汇率与 `fx_rate_locks` 同理。所以「一个事件一旦选到某条规则 / 版本 / 汇率，以后永远选到同一条」只依赖这三把锁，不依赖解析里读了哪些行、读了几次。
- **一条语句选级的加锁范围**：
  - 它仍是加锁读（`FOR SHARE`），读的是最新提交，不会掉进 REPEATABLE READ 的快照陷阱（`test_rule_resolution_reads_past_the_transaction_snapshot` 照跑）。
  - 它加共享锁的行与间隙可能比今天多：今天命中第 k 级就不再查更低的级；现在所有要查的级一起扫，更低级的命中行也会被加共享锁，扫描路径还取决于优化器选的索引。
  - 这不影响正确性：发布、停用要改规则行之前都先等 `pricing_rule_locks`，而计费事务持有它的共享锁直到提交，所以多出来的共享行锁不会让任何发布 / 停用多等（它们本来就在等那把单行锁）。
  - 不拿规则锁就写 `pricing_rules` 的只有草稿的建、改、丢弃（`app/services/pricing_rules.py` 的 `create_draft` / `update_draft` / `discard_draft`）。两种互等都与今天相同，只是涉及的行与间隙可能更多：
    - 草稿事务插入一行时，可能要等计费事务在那个间隙上的共享锁，最多等一个计费事务的时长（几十毫秒）；
    - 计费的加锁读扫到一行未提交的草稿（插入或 `FOR UPDATE` 改过的行）时，要等那个草稿事务提交，草稿事务很短。
  - 不会形成环：草稿事务在 `pricing_rules` 上只碰一行（插入一行，或先 `FOR UPDATE` 再改同一行），之后写的分量、审计都不是计费事务要锁的行；计费事务要的钱包、租户排他锁草稿事务都不碰。万一 InnoDB 判出死锁或锁等待超时（`_limit_lock_wait` 的上限不变），计费一侧按 #181 v2 回滚、退避重试，与今天相同。
- **跳过重复解析的锁**：今天步骤 ⑥ 里重复的那 4 条语句全是重入（同一事务已持有同样的锁），不会多拿任何锁。去掉它们后，事务持有的锁集合与今天相同，取锁顺序相同。
- **数据库负责哪些唯一性保证**：不变（账本 `(USAGE_EVENT, event_id)` 唯一、`ux_pricing_rules_open_slot`、区间触发器）。
- **幂等键**：不变（`event_id`）。
- **相同 ID、不同载荷**：不涉及（摄取不变）。
- **重试发生在哪一层**：不变（#181 v2：处理事务回滚后 `record_failure` 写退避）。
- **哪些操作必须原子提交**：不变（快照、状态、账本、钱包、停机跃迁同一事务）。

## 5. 失败与恢复矩阵

| 失败点 | 对外结果 | 数据最终状态 | 是否可重试 | 恢复来源 | 告警 |
| --- | --- | --- | --- | --- | --- |
| 校验失败：`resolved_model` 与 `locking=False` 同时给 | `ValueError`（编程错误，只可能出现在代码里） | 调用方事务回滚；计费路径从不这样调用 | 否 | 修代码 | 计费路径若出现，按 #181 v2 记 `UNEXPECTED_ERROR` 退避，与今天任何意外异常相同 |
| 一条语句选级时，选中的级多于一行（数据损坏） | `MultipleResultsFound`，与今天相同 | 处理事务回滚，事件 `FAILED_RETRYABLE` → 达上限 `FAILED_FINAL`（`UNEXPECTED_ERROR`），与今天相同 | 退避重试，到上限终止 | 人工修数据后由管理员重放 | 与今天相同（意外异常的日志） |
| DB 提交失败 / 语句超时 / 锁等待超时 | 与今天相同 | 处理事务回滚，`record_failure` 写退避 | 是 | 下一轮扫描 | 与今天相同 |
| Redis / Celery 丢失 | 与今天相同 | 事件留在库里（`RECEIVED` / `PROCESSING`），卡住回收放回 | 是 | 下一轮扫描、卡住回收 | 与今天相同 |
| 外部服务超时 | 不适用：解析只读本库 | — | — | — | — |
| 重复请求 | 不适用：解析无副作用，计费重放由账本唯一约束兜底（不变） | — | — | — | — |
| 模型未知（`resolve_model` 返回 `None`） | `MODEL_UNKNOWN`，与今天相同 | `_process` 传 `resolved_model=None`，函数自己再解析一次，返回 `MODEL_UNKNOWN`；事件按 #181 v2 落 `MODEL_UNKNOWN` | 与今天相同 | 与今天相同 | 与今天相同 |

## 6. 数据与安全边界

- **租户过滤发生在哪里**：规则解析的每个 OR 分支都带今天那一级的 `tenant_id` / `project_id` 等值条件（全局级是 `IS NULL`），逐字复用 `_in_scope`；价格与汇率不按租户（全局数据，与今天相同）。租户与项目 id 取自事件行，由集成凭据派生（§11），不来自请求参数。
- **鉴权主体及权限**：不涉及。计费是 worker 内部调用；试算的管理员鉴权不变。
- **客户侧禁止返回的内部字段**：不新增任何输出；内部 id 仍只进快照。
- **日志、审计、异常中允许和禁止出现的数据**：不新增日志。新增的 `ValueError` 只带固定文案，不带 id 或金额。
- **密钥**：不涉及。
- **是否处理 prompt / response 或客户数据**：否。
- **数据保留及删除影响**：无。

## 7. 测试证据计划

| 风险/需求 | 测试层级 | 场景 | 预期结果 |
| --- | --- | --- | --- |
| 正常路径：一条语句 | integration（SQLite） | `tests/backend/test_pricing_rules_resolve.py` 的 `_rule_queries` 计数改为断言：项目为空且无规则、有项目且第 1 级命中、有项目但无项目级规则，三种情况读 `pricing_rules` 的语句都是 **1** 条（今天分别是 5、1、8） | 都是 1；选中的规则与今天的断言相同 |
| 正常路径：八级先后 | integration（SQLite + MySQL） | 现有 `test_the_eight_levels_fall_through_in_order`：每一级各一条规则，每次停用最高的一级再解析。**新增一个 MySQL 版本**（同一流程用 `mysql_factory`），因为 OR 与 `IS NULL` 的组合、`FOR SHARE` 只有在 MySQL 上才真跑 | 两个库上依次选到八级的规则、最后 `None`，与 `SCOPE_ORDER` 一致 |
| 边界值：命中级缺分量不下落 | integration | 现有 `test_a_hit_stops_even_without_the_events_component` 照跑 | 仍返回命中的 FIXED_RATE 规则，不下落 |
| 边界值：项目为空 | integration | 现有 `test_without_a_project_the_project_levels_are_not_queried`：三个项目级各有规则、`project_id=None` | 返回 `None`（项目级不可见），且只发 1 条语句 |
| 边界值：草稿、丢弃、撤销的预约 | integration | 现有 `test_drafts_discarded_drafts_and_withdrawn_reservations_never_match` 照跑 | 都不匹配 |
| 数据损坏 | integration（SQLite） | 新增：绕过服务层直接改行，让同一级有两条区间重叠、都带 `effective_to` 的已发布规则（SQLite 没有区间触发器；带 `effective_to` 的行不占 `open_slot` 唯一索引），解析落在重叠处的时刻 | 抛 `MultipleResultsFound`；另一组：第 1 级一条正常规则 + 更低一级两条重叠规则 → 返回第 1 级那条、不抛 |
| 并发：发布与计费互斥 | integration（MySQL） | 现有 `test_billing_waits_for_an_uncommitted_publish`、`test_a_publish_waits_for_an_uncommitted_billing_run`、`test_two_billing_runs_share_the_rule_lock`、`test_rule_resolution_reads_past_the_transaction_snapshot` 照跑 | 结果与今天相同 |
| 并发：多扫描计费 | integration（MySQL） | 现有 `test_usage_billing_concurrency.py` 全部照跑 | 不重复扣费、余额与账本一致 |
| 跳过重复解析：结果相同 | integration（SQLite） | 新增（`test_pricing_engine.py`）：同一事件、同一事务里先手动执行 `resolve_model` 与两把锁，再分别调 `resolve_charge_inputs(locking=True)` 与 `resolve_charge_inputs(locking=True, resolved_model=model)`；模型经代码命中、经别名命中各一组 | 两次的 `ChargeResolution` 逐字段相等（含 `matched_via`） |
| 跳过重复解析：不再重复 | integration（SQLite） | 新增：给 `resolved_model` 时，`monkeypatch` 计数 `catalog.resolve_model`、`rates.lock_fx_rates_shared`、`rules.lock_pricing_rules_shared` | 三者调用次数都是 0 |
| 跳过重复解析：误用 | unit | 新增：`resolved_model` 给值、`locking=False` | `ValueError` |
| 计费的锁顺序与次数 | integration（SQLite） | 现有 `test_the_occurred_at_check_comes_after_the_three_shared_locks` 增加断言：一次 `process_event` 里 `resolve_model`、`lock_fx_rates_shared`、`lock_pricing_rules_shared` 各被调用**恰好 1 次** | 前四步顺序不变，三者各 1 次，`now_r` 1 次，最后一步仍是 `post_transaction` |
| 模型未知 | integration | 现有 `MODEL_UNKNOWN` 用例照跑 | 事件落 `MODEL_UNKNOWN`，与今天相同 |
| 试算不变 | integration | 现有 `test_pricing_preview_api.py` 照跑（试算不传 `resolved_model`） | 与今天相同 |
| 幂等重放 | integration | 不新增：解析无副作用；现有账本重放用例照跑 | 与今天相同 |
| 事务中途失败 | integration | 不新增：解析只读；现有「处理事务中途异常 → 回滚、退避」用例照跑 | 与今天相同 |
| Redis/Celery 丢失 | — | 不适用：不改派发与认领，现有卡住回收用例照跑 | 与今天相同 |
| 租户越权 | integration | 现有 `test_one_tenants_rule_never_prices_another_tenants_event`、`test_one_projects_rule_never_prices_another_projects_event`、`test_a_project_rule_never_prices_another_tenants_event` 照跑 | 与今天相同 |
| 金额精度 | — | 不适用：`price_event` 及其输入不变；现有精度与静态检查用例照跑 | 与今天相同 |

## 8. 迁移与上线

- **数据迁移步骤**：无（不改表结构）。
- **锁表与性能影响**：无迁移。运行时每条事件少 10 条语句；规则解析的单条语句可能比今天的单级查询略慢（多扫几个级），总耗时仍应明显下降，由 §1 完成标准 4 的重跑验证。
- **兼容窗口**：无。新参数有默认值，旧调用方行为不变。
- **部署顺序**：两个实现任务可以按任意顺序合并、各自部署；每一个单独上线都只减少语句条数，不改结果。
- **回滚或前滚方式**：回退对应的提交即可，无数据要处理。
- **部分部署时的行为**：api 与 celery-worker 同一镜像；部署期间新旧 worker 同时计费，两者选出的规则、版本、汇率相同（§2「为什么结果与逐级查完全相同」），锁协议相同。
- **监控指标和告警负责人**：沿用现有计费告警（`FAILED_FINAL`、积压）；性能由管理员在上线后重跑剖析、写进 `docs/perf-baseline.md`（另走文档 PR）。

## 9. 方案取舍

| 方案 | 优点 | 风险 | 未采用原因 |
| --- | --- | --- | --- |
| 采用：一条 OR 语句选级 + 传入已解析的模型 | 每条事件少 10 条语句；条件逐字复用 `_in_scope`，语义可逐级对照；不改表、不加缓存 | 加锁范围可能比逐级查大，取决于执行计划（§4 已论证不影响正确性） | — |
| 备选：每级一个 `SELECT … FOR SHARE`，用 `UNION ALL` 拼成一条 | 每个分支的执行计划与今天那一级相同 | MySQL 要求带锁定子句的 UNION 分支加括号；SQLAlchemy 编译 `union_all` 时把 `LOCK IN SHARE MODE` 直接接在分支后面、不加括号（本地编译验证过），要手写编译；ORM 读实体还要 `from_statement`，`populate_existing` 的写法也要另验 | 复杂度高，而它省下的往返与 OR 方案相同 |
| 备选：规则、价格、汇率做进程内缓存 | 省得更多 | 缓存失效要与发布 / 停用的锁协议对齐，否则会用旧规则计费（破坏「一个事件一旦选到某条规则，以后永远选到同一条」的前提是锁内读最新提交） | 风险落在定价正确性上，收益可以先由本方案拿到大半 |
| 备选：价格版本与分量、规则与分量各合成一条 JOIN | 再省 2 条语句（约 2.4 ms） | 无分量的版本 / MARKUP 规则要用外连接，否则会把「缺分量」误判成「没有版本 / 规则」；改动面扩到价格仓储 | 先拿最大的两块；剩下的留给后续，重跑剖析后再看值不值得 |
| 备选：只做其中一项 | 改动更小 | — | 两项互不依赖，拆成两个任务分别交付（§11），都要做 |

## 10. 未决问题与假设

- **未决问题**：无。
- **尚未验证的假设**：
  - 一条 OR 语句的耗时不超过今天单级查询的几倍。依据：`pricing_rules` 是小表（生产上目前 1 行，规则按客户、项目配下去也是几十到几百行的量级），OR 的八个分支都是等值条件，可走 `ix_pricing_rules_resolve` 的多个范围或任一外键索引；`billing_perf` 上 8 行规则时 `EXPLAIN` 走 `fk_pricing_rules_provider` 的 ref_or_null，估算 3 行。
  - 剖析形态的语句条数从 28 降到 18。依据是 §1 的逐条计时：规则 7 条变 1 条，重复的 4 条去掉。
- **需要谁拍板**：无。
- **如果假设错误，具体后果**：只影响性能收益（完成标准 4 达不到），不影响选中的规则与扣费金额；由上线后的剖析发现，回到本闸门另议（例如改成 UNION ALL）。

## 11. 审查与版本绑定

审查方要回答的五个问题常驻在 [scripts/review_checklist.md](../../scripts/review_checklist.md) 的「设计审查」一节，此处不重复。

**版本绑定规则**（规则的出处是 [WORKFLOW §3](../../docs/WORKFLOW.md)；这里保留一份是因为审查材料里只有本 Issue 正文，两处改一起改）：

- Codex 的批准必须写成 `APPROVED: design v<N>`，`<N>` 取本 Issue 顶部的「设计版本」
- **设计版本一变，之前的 APPROVE 自动作废**，必须重新过闸门
- 什么算「实质修改」：改了契约、表结构、事务边界、状态机、失败语义、不变量控制。改错别字不算
- 实质修改时：把顶部版本 +1，在下方追加一条变更说明，状态退回 `READY_FOR_REVIEW`

### 任务拆分

两个实现任务互不依赖、改的文件不重叠，按登记顺序串行交付：

| 任务 | 内容 | 可改路径 |
| --- | --- | --- |
| AIH-TASK-143 | 改动一：`resolve_pricing_rule` 一条语句选级；§7 里规则解析的各项（计数断言、MySQL 版八级下落、数据损坏） | `app/repositories/pricing_rules.py`、`tests/backend/test_pricing_rules_resolve.py` |
| AIH-TASK-144 | 改动二：`resolve_charge_inputs` 的 `resolved_model`、`_process` 传入；§7 里跳过重复解析的各项与锁顺序断言 | `app/services/pricing_engine.py`、`app/services/usage_billing.py`、`tests/backend/test_pricing_engine.py`、`tests/backend/test_usage_billing.py` |

上线后的剖析重跑与 `docs/perf-baseline.md` 记录是运维步骤，不登记为任务。

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
| v1 | 2026-10-30 | 初稿 | — |
| v1（勘误） | 2026-10-30 | 「对应需求」补 `REQ-PRICE-001`；只补追溯 ID，契约、事务、失败语义与测试都不变，不算实质修改，版本不变 | 登记 PR #530 的 Codex 审查建议 |


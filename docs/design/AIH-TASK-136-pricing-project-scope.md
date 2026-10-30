# AIH-TASK-136 至 140 设计：定价规则加项目作用域（已批准 v4）

> **来源**：设计闸门 Issue #507。本文件是 `APPROVED: design v4` 那一版正文的**逐字副本**。审查经过见 §12：v1、v3 由 Codex 审查、被要求修改（v1：迁移中途中断时数据库层的不可变与区间不重叠会失效；v3：测试清理会留下缺失的删除触发器、只剩一个隔离触发器的重跑未覆盖）；v2 曾获 Codex 批准，之后登记前预审查出降级的离线顺序与新列位置的缺陷，升版作废；v4 由 Codex 批准。每一版送审之前都先跑了登记前预审，发现都已并入。
> 放进仓库，是因为 OpenClaw Worker 在沙箱里不联网、读不到 GitHub Issue。
> **实现以本文件为准**；与 Issue 不一致时，以 Issue #507 上被批准的 v4 为准。设计要改，就回到 Issue 升版本、重新过闸门，不要直接改本文件。
> **前置**：设计闸门 #474（[AIH-TASK-129-wallet-reconciliation.md](AIH-TASK-129-wallet-reconciliation.md)）的 `AIH-TASK-129..135`（迁移 0029 在 0030 之前，验收脚本同改，排在它们之后）；定价规则本身见设计闸门 #178 v4（[AIH-TASK-027-pricing-rules.md](AIH-TASK-027-pricing-rules.md)）。
> §11「任务拆分」的五个实现任务登记为 `AIH-TASK-136`（迁移）→ `AIH-TASK-137`（八级枚举与范围字段校验）→ `AIH-TASK-138`（解析与服务）→ `AIH-TASK-139`（接口）→ `AIH-TASK-140`（前端）。

---
## 0. 准入判定

| 任务涉及 | 要填的范围 |
| --- | --- |
| 钱包 / 账本 / 定价 / 汇率 / 支付 / 幂等 / 状态机 | **全部章节** |
| 用量摄取 / 认证与会话（集成侧与平台侧）/ Webhook | §1–§7（**§6 不可省**） |
| 前端 / 文档 / CI / 脚本 | **不走本闸门**，直接开 PR |
| 既不碰钱、也不碰用量摄取 / 认证与会话 / Webhook 的后端改动 | **不走本闸门**，直接开 PR |

本任务档位：`全部`

理由：本设计改定价规则的范围与 spec §16 的解析顺序（决定每个用量事件按哪条规则计费、扣多少钱），改 `pricing_rules` 的表结构、生成列、CHECK 与触发器（迁移），并改计费路径传给解析的参数。不改钱包、账本、汇率与供应商价格。

---

状态：`READY_FOR_REVIEW`
负责人：Claude
**设计版本**：`v4`
对应需求：spec §16（解析顺序）、§15、§59、§74.3、§141（「AI 模块……与 AI Chatbot 分开、按项目分开定价」）；docs/TODO.md「待登记：定价规则加项目作用域：同一客户的 AI Chatbot 与 AI API 分开定价，约定一个项目对应一条产品线下的一个模块」（Kelvin 2026-10-09：要分开）；docs/design/AI-MODULE.md F-08；设计闸门 #178 v4（定价规则，AIH-TASK-027）
目标 PR：待开（登记 PR）；实现为 AIH-TASK-136..140

## 1. 目标与边界

- **要解决的问题**：spec §141 要求 AI 模块与 AI Chatbot「按项目分开定价」，但定价规则只有租户、供应商、模型三个范围列，§16 的五级里最细只到「客户 + 供应商 + 模型」。同一客户的两个项目（例如 AI Chatbot 的项目与 AI API 的项目）用同一个模型时，现在只能按同一条规则计费。
- **可观察的完成标准**：
  1. 管理员能给某客户的某个项目建、发布、停用三级规则：项目 + 供应商 + 模型（`PROJECT_PROVIDER_MODEL`）、项目 + 供应商（`PROJECT_PROVIDER`）、项目默认（`PROJECT`）。
  2. 解析顺序变为八级：`PROJECT_PROVIDER_MODEL` → `PROJECT_PROVIDER` → `PROJECT` → `CUSTOMER_PROVIDER_MODEL` → `CUSTOMER_PROVIDER` → `CUSTOMER` → `GLOBAL_PROVIDER_MODEL` → `GLOBAL`；第一级命中即返回，命中后不因 FIXED_RATE 缺分量而下落（与现在相同）。
  3. 一个项目的规则只给**这个项目**的事件计费：同客户的其他项目、其他客户的事件永远选不到它。
  4. 一个项目没有项目级规则时，它的事件选到的规则与本设计上线前**完全相同**（仍从客户级开始）。
  5. 上线前已计费事件的 `pricing_rule_id` 不变；上线后选到项目级规则的事件同样永久保留它（§16「保留选中的规则」）。
  6. 管理端试算可选填项目，结果与真实计费选到同一条规则。
  7. spec §16、§59、§74.3 与 `docs/pricing-engine.md`、`docs/database-schema.md`、`docs/api.md` 同步为八级。
- **明确不做什么**：
  - 不给项目加产品线或模块字段（Kelvin 2026-10-30 定：「一个项目对应一条产品线下的一个模块」只写进 spec 的约定，不落成数据）。
  - 不按 `feature` 定价（§11：`feature` 永不影响定价，不变）。
  - 不改已有规则：上线时没有任何项目级规则，管理员按需新建。
  - 不改 FIXED_RATE 的分量、MARKUP 的倍数语义、发布 / 停用的状态机与锁。
  - 不做按项目的成本、毛利分析（Phase 7 另立）。
- **现有行为与问题证据**：
  - `app/models/pricing_rules.py`：`PricingScope` 五个值按优先顺序声明，`SCOPE_ORDER = tuple(PricingScope)`；`SCOPE_COLUMNS_CHECK` 让每一级恰好对应一种 NULL 组合；生成列 `scope_key = CONCAT(priority_scope, ':', COALESCE(tenant_id, 0), ':', COALESCE(provider_id, 0), ':', COALESCE(model_id, 0))` 与 `open_slot`，唯一索引 `ux_pricing_rules_open_slot (scope_key, open_slot)`；迁移 0014 的 `trg_pricing_rules_before_update` 在规则锁内按同一表达式现算 `scope_key` 查区间重叠，并列出发布后不可变的列。
  - `app/repositories/pricing_rules.py::resolve_pricing_rule(session, tenant_id, provider_id, model_id, occurred_at)` 按 `SCOPE_ORDER` 逐级 `FOR SHARE` 查；`app/services/pricing_engine.py::resolve_charge_inputs` 调用它；`app/services/usage_billing.py::_process` 调 `resolve_charge_inputs` 时**不传 `project_id`**，尽管 `usage_events.project_id` 非空、且由集成凭据派生（§11）。
  - `projects` 有 `uq_projects_id_tenant (id, tenant_id)`；`usage_events` 用复合外键 `(project_id, tenant_id)` 指向它。

## 2. 设计概要

```text
管理员 POST /admin/pricing-rules（priority_scope = PROJECT_*，customer_id + project_id + …）
  → 校验范围字段与 priority_scope 恰好匹配 → 项目必须属于该客户（否则 404 PROJECT_NOT_FOUND）
  → INSERT 草稿（tenant_id = 客户，project_id = 项目；复合外键保证项目属于该租户）
  → 发布 / 停用：照旧（规则锁 FOR UPDATE、t 在锁后取、只在本范围末尾追加、触发器兜底）

计费 usage_billing._process（照旧在 resolve_model 之后取规则锁 FOR SHARE）
  → resolve_charge_inputs(…, tenant_id = event.tenant_id, project_id = event.project_id, …)
  → resolve_pricing_rule 按八级逐级 FOR SHARE 查，第一级命中即返回
  → 快照 event.pricing_rule_id（照旧）→ 扣费（照旧）
```

### 数据库（迁移 0030）

`pricing_rules`：

- 新列 `project_id BIGINT NULL`，位置紧接 `tenant_id` 之后（迁移用 `AFTER tenant_id`，模型里也声明在 `tenant_id` 之后，两边列序一致）。
- 新复合外键 `fk_pricing_rules_project (project_id, tenant_id) → projects (id, tenant_id) ON DELETE RESTRICT`，以及它在子表上的索引 `ix_pricing_rules_project (project_id, tenant_id)`。`project_id` 为 NULL 时 MySQL 不检查这条外键（复合外键任一列为 NULL 即不检查），所以非项目级规则不受影响；项目级规则的项目一定属于同一租户，由数据库保证。
- `ck_pricing_rules_priority_scope` 改为八个值：`'PROJECT_PROVIDER_MODEL', 'PROJECT_PROVIDER', 'PROJECT', 'CUSTOMER_PROVIDER_MODEL', 'CUSTOMER_PROVIDER', 'CUSTOMER', 'GLOBAL_PROVIDER_MODEL', 'GLOBAL'`。
- `ck_pricing_rules_scope_columns` 改为八种组合，每级恰好一种（四列 `tenant_id`、`project_id`、`provider_id`、`model_id`）：

  | 级 | tenant_id | project_id | provider_id | model_id |
  | --- | --- | --- | --- | --- |
  | `PROJECT_PROVIDER_MODEL` | 非空 | 非空 | 非空 | 非空 |
  | `PROJECT_PROVIDER` | 非空 | 非空 | 非空 | NULL |
  | `PROJECT` | 非空 | 非空 | NULL | NULL |
  | `CUSTOMER_PROVIDER_MODEL` | 非空 | NULL | 非空 | 非空 |
  | `CUSTOMER_PROVIDER` | 非空 | NULL | 非空 | NULL |
  | `CUSTOMER` | 非空 | NULL | NULL | NULL |
  | `GLOBAL_PROVIDER_MODEL` | NULL | NULL | 非空 | 非空 |
  | `GLOBAL` | NULL | NULL | NULL | NULL |

- 生成列 `scope_key` 改为 `CONCAT(priority_scope, ':', COALESCE(tenant_id, 0), ':', COALESCE(project_id, 0), ':', COALESCE(provider_id, 0), ':', COALESCE(model_id, 0))`（SQLite 同值的 `||` 写法），列宽 `SCOPE_KEY_LENGTH = 128` 不变（最长 `PROJECT_PROVIDER_MODEL` 22 个字符 + 4 个冒号 + 4 个 BIGINT 各至多 20 位 = 106）。同一客户两个项目的规则因此是两个范围，各有自己的时间线与 `open_slot`。已有行的 `scope_key` 只多出一段 `:0`，彼此仍不同，唯一索引重建不会冲突。
- `trg_pricing_rules_before_update` 重建（DROP + CREATE），只改两处：② 发布后不可变的列里加 `project_id`；④ 区间重叠检查里现算的 `scope_key` 加 `COALESCE(NEW.project_id, 0)` 一段，与新的生成列表达式逐字一致。其余检查（①、①′、③、⑤）与 0014 逐字相同。
- 列表（`list_rules`）的排序在租户之后、供应商之前加 `project_id`（NULL 在前），同一客户两个项目的规则不混在一起；其余排序键不变。
- `ix_pricing_rules_resolve` 不变：项目级的查询按 `(priority_scope, tenant_id, provider_id, model_id)` 前缀落到该客户的项目级规则，再按 `project_id` 过滤；规则表是管理员手建的小表，不加列。
- **升级的写法（MySQL）**，五步，任何一步都可以安全重跑。离线模式（`--sql`）不做任何判断，见本列表最后一条。在线时**先判断是否已完成**：`project_id` 列存在、形状核对通过（第 3 步的标记）、主触发器含 `NEW.project_id`、两个隔离触发器都不存在 → 整个升级已完成，直接返回，不发任何 DDL（连隔离触发器也不建）。否则依次：
  1. **预检**：在任何 DDL 之前查 `@@log_bin` 与 `@@log_bin_trust_function_creators`，写法与报错文案照 0014（binlog 开着而开关关着时应用账号建不了触发器，ERROR 1419）；不满足就抛错、什么都不改。
  2. **写入隔离**：建两个隔离触发器 `trg_pricing_rules_migration_guard_insert`（BEFORE INSERT）与 `trg_pricing_rules_migration_guard_update`（BEFORE UPDATE），正文只有一句 `SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'pricing_rules is being migrated'`，不引用任何列。**两个隔离触发器各自按是否存在判定**：哪个不存在就建哪个，已存在的跳过；所以上一次只建成了其中一个时，重跑补建另一个。**第 3、4 步都已完成时跳过这一步**，直接到第 5 步（上一次停在解除隔离的中途：不再补建已删掉的那个）。**两个隔离触发器都用 `PRECEDES` 排在同一时机的主触发器之前**（`… FOR EACH ROW PRECEDES trg_pricing_rules_before_insert` / `PRECEDES trg_pricing_rules_before_update`），所以隔离期间被拒的写入一律得到隔离消息，不先撞上主触发器的不可变或区间检查；建隔离触发器时主触发器不存在（只可能是手工删过）就不带 `PRECEDES`。MySQL 8 允许同一张表同一时机有多个触发器，隔离触发器与原有触发器并存。从这一步到第 5 步之间，**对 `pricing_rules` 的任何 INSERT 与 UPDATE 都被拒绝**（DELETE 本来就被 0014 的触发器拒绝），不论来自旧服务、新服务还是手工 SQL；所以中途任何一步失败、库停在半途时，数据库层的不可变与区间不重叠不会被绕过，而是**整张表暂时不可写**，直到重跑迁移补完。计费只读这张表（`FOR SHARE`），不受影响；受影响的只有管理员在这几秒里的建草稿、改草稿、发布、停用、丢弃，它们得到错误、什么都没写。
  3. **表**：`project_id` 列不存在时，用**一条** `ALTER TABLE pricing_rules` 完成全部表结构改动：`ADD COLUMN project_id BIGINT NULL AFTER tenant_id`、`ADD INDEX ix_pricing_rules_project`、`ADD CONSTRAINT fk_pricing_rules_project FOREIGN KEY …`、`DROP CHECK` 再 `ADD CONSTRAINT` 同名的两条 CHECK、`DROP INDEX ux_pricing_rules_open_slot`、`MODIFY COLUMN scope_key … GENERATED ALWAYS AS (新表达式) STORED`、`ADD UNIQUE INDEX ux_pricing_rules_open_slot (scope_key, open_slot)`。MySQL 8 的 InnoDB DDL 是原子的，这一条语句要么全部生效、要么全不生效，所以**「`project_id` 列存在」就是这一步已完成的标志**；列存在时的形状核对**在第 2 步建隔离触发器之前**做（紧接第 1 步预检）：任何一项不符就抛错、不发任何 DDL，库里若已有隔离触发器（之前中断过）也原样留下（这只可能是手工改过表，需要人来看）。核对的标记：`fk_pricing_rules_project` 存在、引用 `projects (id, tenant_id)`、`DELETE_RULE` 为 `RESTRICT`；`ix_pricing_rules_project` 的列序为 `(project_id, tenant_id)`；`ck_pricing_rules_priority_scope` 的 `CHECK_CLAUSE` 含 `PROJECT_PROVIDER_MODEL`；`ck_pricing_rules_scope_columns` 的 `CHECK_CLAUSE` 含 `project_id`；`scope_key` 的 `GENERATION_EXPRESSION` 含 `project_id`；`ux_pricing_rules_open_slot` 是唯一索引、列序为 `(scope_key, open_slot)`。只查这些子串与列序，不比较 MySQL 改写过的全文。已有行全部满足新 CHECK（`project_id` 都是 NULL，五种旧组合是八种新组合的子集）。这条语句的写法（同一条语句里删掉再加回同名 CHECK 与同名唯一索引、改 STORED 生成列表达式、加复合外键）已在 MySQL 8.4 上验证可行，已有行的 `scope_key` 随之重算为多一段 `:0`。
  4. **触发器**：读 `information_schema.TRIGGERS` 里 `trg_pricing_rules_before_update` 的 `ACTION_STATEMENT`：含 `NEW.project_id` → 已完成、跳过；存在但不含 → `DROP TRIGGER` 后 `CREATE`；不存在（上一次在 DROP 与 CREATE 之间中断）→ 直接 `CREATE`。判定只看 `NEW.project_id` 这一个标记，不比较 MySQL 改写过的全文。
  5. **解除隔离**：先查两个隔离触发器各自是否存在，只对存在的那个发 `DROP TRIGGER`；上一次只删掉一个时，重跑只删剩下的那个（恰好一条 DROP）。只有第 3、4 步都已完成（`project_id` 列存在、形状核对通过、主触发器含 `NEW.project_id`）才执行这一步；所以隔离触发器还在，就说明迁移没有完成。
  - **离线模式（`--sql`）的升级**：读不了库，照 0014 与 0027 的惯例跳过预检与全部检查，按「什么都没做过」依次输出建两个隔离触发器、那条 ALTER、主触发器的 `DROP TRIGGER IF EXISTS` 与 `CREATE TRIGGER`、删两个隔离触发器。
- **降级**，同样可重跑。**离线模式（`--sql`）最先抛错**，先于下面的一切判断：离线读不了库，既无法判断降级是否已完成，也无法判断表里有没有项目级规则。在线时**先判断是否已完成**：`project_id` 列不存在、主触发器存在且不含 `project_id`、两个隔离触发器都不存在 → 直接返回，不发任何 DDL。否则依次：
  1. 预检照升级第 1 步（降级同样要重建触发器）。
  2. **写入隔离**：同升级第 2 步，建两个隔离触发器（各自按是否存在判定；第 4、5 步都已完成时跳过这一步，直接到第 6 步）。放在拒绝检查之前，检查与之后的 DDL 之间就不会有人插入项目级规则。
  3. `project_id` 列存在时，表里有任何 `project_id IS NOT NULL` 或 `priority_scope` 是三个项目级之一的行就先删掉隔离触发器、再抛错，表与触发器回到降级之前的样子——降级会丢掉这些规则，而它们可能已被用量事件的 `pricing_rule_id` 引用（INV-6）。列已不存在（上一次降级已完成表的那一步）时跳过这一查询：没有这一列就不可能有项目级规则（CHECK 不允许）。
  4. `project_id` 列存在时，用一条 `ALTER TABLE` 恢复 0014 的两条 CHECK、生成列表达式与唯一索引，并删外键、索引与列；列不存在时跳过。
  5. 触发器恢复为 0014 的正文：存在且 `ACTION_STATEMENT` 不含 `project_id` → 跳过；含 → DROP 后 CREATE；不存在 → CREATE。
  6. **解除隔离**：同升级第 5 步；只有第 4、5 步都已完成（列已不存在、主触发器不含 `project_id`）才执行。
  - **为什么降级先改表、后换触发器**：（隔离触发器之外的第二道保证）这样即使隔离触发器被人手工删掉，任何一步失败也不会让区间检查悄悄失效。ALTER 失败时表完全不变，0030 的触发器照旧与表一致；ALTER 完成、触发器还没换时，0030 的触发器引用的 `NEW.project_id` 已不存在，MySQL 对这张表的**每一次 UPDATE 都报错**（ERROR 1054，失败即拒绝、不放行），直到重跑降级换上 0014 的触发器。反过来先换触发器，ALTER 一旦失败，库停在 0030 却挂着按旧表达式算范围键的触发器，区间检查悄悄失效。（2026-10-30 在一次性 MySQL 8.4 容器里验证：删掉被触发器引用的列可以成功，之后的 UPDATE 报 1054。）
  - **为什么升级只能先改表、后建触发器**：`CREATE TRIGGER` 引用不存在的列在创建时就报 ERROR 1054（同一次验证），所以 0030 的触发器只能在列加上之后建；这个窗口里旧触发器按旧表达式算的范围键与新的 `scope_key` 不相等，区间检查本身失效，所以这个窗口必须被第 2 步的写入隔离覆盖（见 §4「迁移与在线计费的并发」）。
- 模型 `app/models/pricing_rules.py` 同步：`PricingScope` 按优先顺序声明八个值，新增 `has_project`（三个项目级为真）；三个项目级的 `has_customer` 也为真（它们同样带 `tenant_id`）；`SCOPE_ORDER = tuple(PricingScope)` 不变。`PRIORITY_SCOPE_CHECK`、`SCOPE_COLUMNS_CHECK`、`SCOPE_KEY_EXPRESSION`、`SQLITE_SCOPE_KEY_EXPRESSION` 与迁移 0030 逐字一致，`test_migrations.py` 比对两边（比对对象从 0014 改为 0030）。

### 解析

- `Scope` 增加 `project_id`；`_in_scope` 对四列 NULL 安全比较；`scope_of(row)` 带上 `row.project_id`。
- `resolve_pricing_rule(session, tenant_id, project_id, provider_id, model_id, occurred_at)`，`project_id: int | None`：`_level_scope` 对 `has_project` 的级填 `project_id`、其余级填 NULL；`project_id` 为 `None`（试算没给项目）时前三级不查，直接从客户级开始；逐级、`FOR SHARE`、`PUBLISHED` / `RETIRED`、`effective_from ≤ occurred_at < effective_to`、第一级命中即返回、同一级多于一条抛错——全部与现在相同，只是多了三级在前面。
- `resolve_charge_inputs` 增加必填关键字参数 `project_id: int | None`，原样传给 `resolve_pricing_rule`；`usage_billing._process` 传 `event.project_id`（事件的项目由集成凭据派生，§11，永远非空）；试算传请求里的项目或 `None`。
- **为什么项目级规则只会匹配本项目、本租户的事件**：项目级查询同时比较 `tenant_id = 事件的租户` 与 `project_id = 事件的项目`；事件的 `(project_id, tenant_id)` 与规则的 `(project_id, tenant_id)` 都有指向 `projects (id, tenant_id)` 的复合外键，所以只要 `project_id` 相同，租户也一定相同；两项比较同时写，是为了 SQLite（没有外键强制时）与审查可读性。
- **为什么没有项目级规则时结果不变**：前三级查询都带 `project_id = 事件的项目`，没有这样的行时三次查询都返回空，随后的五级查询与现在逐字相同（`project_id IS NULL` 的条件对所有已有行成立）。

### 发布、停用与生效时刻

- 不改。项目级三级是普通范围：`effective_from ≥ t`，不许「一直以来」（只有 `GLOBAL` 第一条可以，`_placement` 的 `backdate` 只对 `GLOBAL` 为真，触发器 ⑤ 同理）。所以一条项目级规则只会改变 `t` 之后的时刻选哪条规则，**不会**让已经按客户级或全局规则计过费的时刻改选它（与 #178 v4 §2「回溯生效只允许全局默认的第一条」的论证相同：计费只处理 `occurred_at ≤` 持锁后当前时间的事件，发布的 `t` 在锁后取、向上取整到下一个整秒）。
- 上线时表里没有任何项目级规则，所以上线本身不改变任何事件的选择。

### 接口

- `POST /api/v1/admin/pricing-rules`：请求增加可选字段 `project_id`（项目的 `public_id`）。范围字段与 `priority_scope` 恰好匹配的规则扩为四个字段：三个项目级要 `customer_id` 与 `project_id`（`PROJECT_PROVIDER_MODEL` 另要 `provider_id`、`model_id`，`PROJECT_PROVIDER` 另要 `provider_id`）；其余五级不许带 `project_id`。多给或少给都是 422（与现在相同）。客户不存在 404 `CUSTOMER_NOT_FOUND`；项目不存在或不属于该客户 404 `PROJECT_NOT_FOUND`（按 `get_project_for_tenant(tenant, project_id)` 查，不区分两种情况）。`PROJECT_NOT_FOUND` 一律抛已有的 `app.services.integration_access.ProjectNotFound`（同一语义：「不存在，或属于另一个客户」），不新建错误类。
- `PATCH /pricing-rules/{rule_id}`：照旧不收任何范围字段。
- `GET /pricing-rules`：筛选增加可选 `project_id`，**只能与 `customer_id` 一起给**（单独给 422 `VALIDATION_ERROR`）；项目按 `get_project_for_tenant(客户, project_id)` 查，不存在或不属于该客户 404 `PROJECT_NOT_FOUND`。排序照旧按 `SCOPE_ORDER` 再按时间，三个项目级排在最前。
- `PricingRuleView` 增加 `project_id`（项目 `public_id`）与 `project_name`，非项目级为 `null`。
- `POST /api/v1/admin/pricing-preview`：请求增加可选 `project_id`。校验顺序：客户（404 `CUSTOMER_NOT_FOUND`）→ 项目（给了才查，404 `PROJECT_NOT_FOUND`，同上一个错误类）→ 计量类型（404 `USAGE_METER_TYPE_NOT_FOUND`）→ 其余照旧。给了项目：按事件属于该项目来解析（八级）。没给：前三级不查（等价于「该客户一个没有项目级规则的项目」），结果与现在相同。响应照旧带选中规则的 `priority_scope` 与 `id`。
- **审计**：现在记录范围的地方（`_scope_state`，即 `PRICING_CREATE`、`PRICING_PUBLISH`、`PRICING_RETIRE` 的状态里那组 `priority_scope` / `customer_id` / `provider_code` / `model_code`）增加 `project_id`（项目 `public_id`，非项目级为 `null`），沿用「不用内部 id」的约定。`PRICING_UPDATE` 与 `PRICING_DISCARD` 照旧只记变化的字段（草稿的范围不可改），不加项目。不新增审计动作。

### 前端

- 规则页的范围下拉加三个项目级（排在最前）；选项目级时，选完客户之后出现项目下拉（数据来自已有的 `GET /admin/customers/{customer_id}/projects`），项目必选。规则表格增加「Project」列（非项目级显示「—」）；筛选增加项目（先选客户才可选）。
- 规则详情页显示项目名。
- 试算页在客户之后加一个可选的项目下拉；结果照旧显示命中的级别。
- 浏览器验收沿用已有步骤 `open_pricing_rules`、`open_pricing_preview`（表头断言加上「Project」列、试算页出现项目下拉），不新增写操作。

### 时间、金额、币种

不变：`occurred_at` 是选规则的时刻，区间半开 `[effective_from, effective_to)`；金额、舍入、币种都只在选中规则之后发生，本设计不改。

## 3. 不变量影响矩阵

| 不变量 | 是否触碰 | 可能怎样被破坏 | 设计控制 | 验证方式 |
| --- | --- | --- | --- | --- |
| INV-1 中心故障不中断 AI | 否 | 本设计不在 AI 调用路径上；计费是异步的，解析多三次按索引的查询 | — | — |
| INV-2 事件不重复扣费 | 否 | 扣费的幂等（事件状态 + 账本唯一约束）不变；本设计只改选哪条规则 | 不改 `_process` 的锁与状态机 | 已有用例照旧通过 |
| INV-3 支付不重复入账 | 否 | 不涉及支付 | — | — |
| INV-4 余额只经账本变动 | 否 | 不写钱包、不写账本之外的余额 | — | — |
| INV-5 历史账本不可变 | 否 | 不改已写账本；已计费事件不重算 | 新规则只在 `t` 之后生效 | §7「上线不改变已计费事件」 |
| INV-6 事件保留版本引用 | 是 | ① 降级删掉被事件引用的项目级规则；② 迁移改了已有规则的范围；③ 项目级规则发布后被改 | ① 有项目级规则时降级抛错；② 已有行只有 `scope_key` 多一段 `:0`，`project_id` 为 NULL，其余列不变，规则外键与 `pricing_rule_id` 不动；③ 触发器 ② 的不可变列加 `project_id` | §7「降级拒绝」「发布后项目不可改」「迁移不改已有规则」 |
| INV-7 客户不可见成本毛利 | 否 | 规则只在管理端；客户侧接口不返回规则 | 新字段只进管理端视图 | 已有客户侧用例照旧 |
| INV-8 租户不可互访 | 是 | 一个项目的规则给另一个项目或另一个租户的事件计费 | 项目级查询同时比较 `tenant_id` 与 `project_id`；复合外键保证规则的项目属于规则的租户；建规则时项目必须属于该客户 | §7「项目隔离」「跨租户」「项目不属于客户」 |
| INV-9 对话内容不入库 | 否 | 不处理 prompt / response | — | — |
| INV-10 金额用 Decimal | 否 | 不新增任何金额计算 | — | — |
| INV-11 event_id 至多一次财务效果 | 否 | 同 INV-2 | — | — |
| INV-12 定稿对账单不可变 | 否 | 对账单尚未实现（Phase 7） | — | — |
| INV-13 状态与事件原子提交 | 否 | 规则的发布与审计照旧同一事务 | 不改事务边界 | 已有用例 |
| INV-14 队列丢失不毁持久工作 | 否 | 不改队列；`PRICING_ERROR` 事件的手工重排照旧 | — | — |

## 4. 状态与并发

- 规则的状态机（`DRAFT → PUBLISHED → RETIRED`、`DRAFT → DISCARDED`）不变，跃迁表见 #178 v4 §4；三个项目级与其他级走同一张表。
- **串行化**：照旧。发布 / 停用先 `pricing_rule_locks FOR UPDATE`，计费在 `resolve_model` 之后 `FOR SHARE`、持有到提交；所以一个事件一旦选到某条规则（包括项目级规则），以后永远选到同一条。
- **数据库负责的唯一性**：同一范围（新的 `scope_key`，含项目）至多一条未截断的已发布规则（`ux_pricing_rules_open_slot`）；同一范围的非空区间不重叠（触发器 ④，规则锁内）；范围与 NULL 组合一一对应（CHECK）；项目属于规则的租户（复合外键）。
- **幂等键**：不新增。建草稿不幂等（与现在相同）；发布已发布的规则返回当前视图（与现在相同）。
- **迁移与在线计费的并发**：`MODIFY COLUMN … STORED` 在 MySQL 上重建表、持元数据锁；正在进行的计费事务持有规则行的共享锁，ALTER 等它们提交；ALTER 进行期间新的计费在元数据锁上排队。规则表是管理员手建的小表（生产上至多数十行），重建在秒级完成。表的那一步完成之后、主触发器重建之前，旧触发器按旧表达式现算的范围键与新的 `scope_key` 永远不相等，区间重叠检查实际失效；DROP 与 CREATE 之间则没有主触发器。这两个窗口都在写入隔离（§2 升级第 2 步到第 5 步）之内：其间对 `pricing_rules` 的任何INSERT 与 UPDATE 都被拒绝，所以不存在「只靠服务层保证」的时刻；迁移中途失败时，隔离触发器留在库里，表保持不可写，直到重跑迁移补完（§5）。迁移在新代码启动之前跑完（§8），隔离持续的时间就是这几条 DDL 的时间（秒级）。

## 5. 失败与恢复矩阵

| 失败点 | 对外结果 | 数据最终状态 | 是否可重试 | 恢复来源 | 告警 |
| --- | --- | --- | --- | --- | --- |
| 建规则时范围字段不匹配 | 422 | 无写入 | 改请求后可 | — | 无 |
| 项目不存在或不属于客户 | 404 `PROJECT_NOT_FOUND` | 无写入 | 改请求后可 | — | 无 |
| 发布时区间冲突 | 409（照旧 `EFFECTIVE_FROM_CONFLICT`） | 草稿不变 | 换时刻后可 | — | 无 |
| 事件的项目没有项目级规则、客户级与全局也没有 | 事件 `PRICING_ERROR` / `NO_PRICING_RULE`（照旧） | 事件留在 `PRICING_ERROR`，不扣费、不丢失 | 管理员补规则后手工重排（照旧） | 事件本身 | `usage_pricing_error` P2（照旧） |
| 命中项目级 FIXED_RATE 规则但缺事件所需分量 | `PRICING_ERROR` / `MISSING_RULE_COMPONENT`，**不**下落到客户级（与现在同一语义） | 同上 | 同上 | 同上 | 同上 |
| 计费事务提交失败 | 无对外 | 事件回到可重试状态，快照未写（照旧） | 是（照旧） | 事件本身 | 照旧 |
| 迁移中途失败 | 部署失败，旧容器继续运行；计费照常（只读这张表）；管理员的规则写操作得到错误、什么都不写 | 隔离触发器在库里，`pricing_rules` 拒绝一切 INSERT 与 UPDATE；表的那一步是一条原子 DDL：要么完全没改，要么全部改完；主触发器可能是旧的、或不存在（DROP 之后中断） | 修复后重跑：隔离触发器已存在就跳过，表按「`project_id` 列存在」跳过，主触发器按 `NEW.project_id` 标记跳过或补建，最后删隔离触发器（§2「升级的写法」） | 迁移本身 | 部署工作流失败 |
| binlog 开着而 `log_bin_trust_function_creators` 关着 | 迁移在任何 DDL 之前抛错，部署失败 | 什么都没改 | 设好开关后重新部署（照 0014） | — | 部署工作流失败 |
| 降级时表里有项目级规则 | 迁移抛错 | 不动 | 先由管理员决定如何处置这些规则 | — | — |
| Redis / Celery 丢失 | 不涉及：本设计不新增任务；计费的恢复照旧 | — | — | — | — |
| 外部服务超时 | 不涉及：解析不调用外部服务 | — | — | — | — |
| 重复请求 | 建两条草稿（与现在相同）；发布照旧幂等 | — | — | — | — |

## 6. 数据与安全边界

- **租户过滤**：规则是平台级配置，只有管理员（`require_admin`）能读写；项目级规则在解析时按事件的租户与项目过滤（§2「解析」）。
- **鉴权主体**：管理端接口照旧 ADMIN；客户侧接口不返回规则、不返回项目级的存在与否。
- **客户侧禁止返回**：规则的策略、倍数、分量（INV-7，照旧）；本设计不新增客户侧字段。
- **日志与审计**：审计里项目用 `public_id`；应用日志照旧不打印分量明细与倍数。
- **密钥**：不涉及。
- **prompt / response**：不涉及。
- **保留与删除**：规则永不删除（触发器，照旧）；项目有规则时删不掉（外键 RESTRICT）。现在没有删除项目的接口，此约束只防手工 SQL。

## 7. 测试证据计划

| 风险/需求 | 测试层级 | 场景 | 预期结果 |
| --- | --- | --- | --- |
| 离线升级 | unit | `--sql` 模式升级到 0030 | 依次输出建两个隔离触发器、那条 ALTER、`DROP TRIGGER IF EXISTS` 与 `CREATE TRIGGER`、删两个隔离触发器，不连库 |
| 迁移结构 | integration（MySQL） | 升级到 0030 | 列、复合外键、索引、两条 CHECK、`scope_key` 表达式、`open_slot` 唯一索引、触发器与模型逐字一致（`test_migrations.py` 比对 0030 的字面量） |
| 迁移不改已有规则 | integration（MySQL） | 0029 下建五级各一条已发布规则（含一条被事件引用的），升级到 0030 | 所有列（除 `scope_key` 多一段 `:0`）不变；`project_id` 全为 NULL；事件的 `pricing_rule_id` 不变；同一事件按新代码重新解析选到同一条规则 |
| 补完中断的迁移 | integration（MySQL） | ① 只执行表的那条 ALTER、不重建触发器，再跑升级；② 执行 ALTER 并 DROP 触发器、不 CREATE，再跑升级；③ 升级完成后再跑一次 | 三种都成功，结构与一次跑完相同；③ 不发任何 DDL（捕获执行语句断言，隔离触发器也没有建） |
| 只有一个隔离触发器 | integration（MySQL） | ① 升级停在只建成 INSERT 那个隔离触发器之后；② 升级的第 3、4 步已完成、解除隔离只删掉了其中一个；③ 降级同样的两个位置；每个位置上各跑一次重跑 | ① 重跑补建另一个隔离触发器、完成升级，最后两个都不在；② 重跑不再改表与主触发器（捕获执行语句断言只有一条 DROP TRIGGER），删掉剩下的那个；③ 同理；每个位置在重跑之前，库里那一个隔离触发器照旧拒绝它那一类写入 |
| 隔离触发器的顺序 | integration（MySQL） | 升级停在建完隔离触发器之后 | `information_schema.TRIGGERS` 里同一时机的隔离触发器 `ACTION_ORDER` 小于主触发器 |
| 形状不符 | integration（MySQL） | 手工加上 `project_id` 列但不改 CHECK，再跑升级 | 抛错，不发任何 DDL，不建隔离触发器；`finally` 里先 `ALTER TABLE pricing_rules DROP COLUMN project_id` 删掉手工加的列，再升到 head |
| 迁移期间的写入隔离 | integration（MySQL） | 升级停在 ① 建完隔离触发器、② ALTER 之后、③ 主触发器 DROP 之后、未 CREATE 三个位置（逐步调用迁移的内部函数，或在下一步之前抛出）；每个位置上直接用 SQL 插入一条规则、更新一条已发布规则的 `effective_from`、写一条与已有规则区间重叠的行 | 三个位置上所有写入都被拒（`pricing_rules is being migrated`），表内容不变；重跑升级后隔离触发器消失，同样的非法写入被主触发器以原有的消息拒绝，合法写入成功 |
| 降级期间的写入隔离 | integration（MySQL） | 降级停在建完隔离触发器之后、ALTER 之后两个位置；同样的写入 | 都被拒；重跑降级后隔离触发器消失，结构与 0029 相同 |
| 补完中断的降级 | integration（MySQL） | ① 验证隔离之外的第二道保证（模拟隔离触发器被手工删掉）：在没有隔离触发器的库上只执行降级的那条 ALTER、不换主触发器，先 UPDATE 一条规则的 `updated_at`，再跑降级；② 降级完成后再跑一次 | ① 中间那次 UPDATE 被拒（ERROR 1054），重跑降级成功、结构与 0029 相同；② 不发任何 DDL、不查 `project_id` |
| 预检 | integration（MySQL） | 让预检查询返回 binlog 开、开关关（monkeypatch 执行结果） | 升级与降级都在任何 DDL 之前抛错，消息与 0014 同义 |
| 降级拒绝 | integration（MySQL） | 有一条 `PROJECT` 草稿时降级到 0029；离线模式降级 | 都抛错、表不变 |
| 降级恢复 | integration（MySQL） | 没有项目级规则时降级到 0029 | 结构与 0029 逐字一致；已有规则不变 |
| CHECK 只放行八种组合 | integration（MySQL + SQLite） | 逐一插入八级各自的合法组合，以及每级把四列之一翻转后的非法组合（例如 `PROJECT` 不带 `project_id`、`CUSTOMER` 带 `project_id`） | 合法的插入成功，非法的被 CHECK 拒绝 |
| 项目属于租户 | integration（MySQL） | 插入 `tenant_id = A`、`project_id = B 租户的项目` 的规则 | 外键拒绝 |
| 两个项目各自一条时间线 | integration | 同一客户两个项目各发布一条 `PROJECT` 规则、都不截断 | 两条都成功（`open_slot` 不冲突）；同一项目第二条照旧截断第一条 |
| 发布后项目不可改 | integration（MySQL） | 直接 UPDATE 已发布项目级规则的 `project_id` | 触发器拒绝 `a published pricing rule is immutable` |
| 八级顺序 | integration | 同一事件的八级各有一条已发布规则；依次停用最高的一级 | 每次命中下一级，顺序正好是八级的顺序 |
| 没有项目级规则时不变 | integration | 只有五级旧规则；事件属于任一项目 | 选到的规则与 `project_id` 无关，与上线前相同 |
| 项目隔离 | integration | 客户 A 的项目 P1 有 `PROJECT` 规则；同客户项目 P2 的事件 | P2 的事件选到客户级（或更低）规则，不选 P1 的规则 |
| 跨租户 | integration | 客户 A 项目 P1 有项目级规则；客户 B 的事件 | 永远选不到 P1 的规则 |
| 命中项目级 FIXED_RATE 缺分量 | integration | 项目级 FIXED_RATE 规则缺事件的计量类型，客户级有 MARKUP | `PRICING_ERROR` / `MISSING_RULE_COMPONENT`，不下落 |
| 不许回溯 | integration | 发布 `PROJECT` 规则时给一个早于 `t` 的 `effective_from`；或不给时刻 | 前者 422 `EFFECTIVE_FROM_IN_PAST`；后者起点为 `t`（不是「一直以来」） |
| 已选定的时刻不改选 | integration（MySQL） | 事件 E1 按客户级规则计费（`PROCESSED`）后，发布同项目的 `PROJECT` 规则（起点 `t`）；另有一个 `occurred_at < t` 的同项目事件 E2，发布之前已摄取、发布之后才计费；再摄取 `occurred_at ≥ t` 的 E3 | E1 的 `pricing_rule_id` 与账本不变，`reprice_from_snapshot(E1)` 得到原金额；E2 选到客户级规则（项目级规则在它的时刻还没生效）；E3 选到项目级规则 |
| 并发：发布与计费 | integration（MySQL） | 计费持规则共享锁期间发布项目级规则 | 发布等计费提交；事件选到发布前的规则；之后 `occurred_at ≥ t` 的事件选到新规则（沿用 #178 的锁用例，加项目级一例） |
| 接口：范围字段 | integration | 八级各缺一个、多一个范围字段 | 422；项目级给了不属于客户的项目 404 `PROJECT_NOT_FOUND` |
| 接口：筛选与视图 | integration | 按 `customer_id` + `project_id` 筛选；单独给 `project_id`；项目不属于该客户 | 只返回该项目的规则；单独给 422；不属于 404 `PROJECT_NOT_FOUND`；视图带 `project_id` 与 `project_name` |
| 接口：审计 | integration | 建、改、发布、停用、丢弃项目级规则 | 建、发布、停用的状态里范围带项目 `public_id`；改与丢弃照旧只有变化的字段；都没有内部 id |
| 试算 | integration | 给项目、不给项目、给不属于客户的项目、客户不存在且给了项目 | 给项目：与真实计费选到同一条规则；不给：与现在相同；不属于：404 `PROJECT_NOT_FOUND`；客户不存在：404 `CUSTOMER_NOT_FOUND`（先于项目） |
| 租户越权 | integration | 非 ADMIN 调规则与试算接口 | 403（照旧） |
| 幂等重放 | integration | 重复发布同一条项目级规则 | 返回当前视图、不写第二次审计（照旧） |
| 事务中途失败 | integration | 发布项目级规则时复查区间失败 | 整体回滚、草稿不变（照旧路径，加项目级一例） |
| Redis/Celery 丢失 | — | 不涉及（本设计不新增任务） | — |
| 金额精度 | integration | 项目级 MARKUP 与 FIXED_RATE 各计费一次 | 金额与同参数的客户级规则相同（只换了范围） |
| 前端 | unit（vitest） | 规则页：选项目级后出现项目下拉、项目必选；表格有「Project」列；试算页的项目下拉可选可空 | 请求体与契约一致；非项目级不带 `project_id` |

**MySQL 用例的清理**：规则行永不删除（0014 的 `trg_pricing_rules_before_delete`），还可能被用量事件以 RESTRICT 外键引用；而表里留着项目级行会让之后任何经过 0030 的降级被拒（§2「降级」第 3 步）。所以凡是在共享测试库里写入项目级规则的 MySQL 用例，**开始与 `finally` 两处**都按现有定价用例的清场写法清表（`tests/backend/test_pricing_rules_service.py` 与 `test_pricing_rules_resolve.py` 里的 `TRUNCATE TABLE pricing_rule_components` / `TRUNCATE TABLE pricing_rules`；表被外键引用时先 `SET FOREIGN_KEY_CHECKS = 0`、TRUNCATE 之后在 `finally` 里恢复为 1，写法同 `test_account_status_service.py` 清账本）。TRUNCATE 是 DDL、不经过任何触发器，所以**不删除、不重建任何触发器**，清场失败也不会让删除触发器处于缺失状态；清完断言表里没有项目级行。迁移用例里被打断的升级 / 降级同样在 `finally` 里把库补到 head（重跑升级，于是残留的隔离触发器也被删掉），不把半途的库留给下一个用例。

## 8. 迁移与上线

- **数据迁移步骤**：迁移 0030 只改结构；不改任何已有行的业务列（`scope_key` 是生成列，随表达式重算）。
- **锁表与性能影响**：`MODIFY COLUMN … STORED` 重建 `pricing_rules`；生产上至多数十行，秒级。期间计费在元数据锁上短暂排队，不丢事件。解析多三次按索引前缀的查询。
- **兼容窗口**：迁移先于新代码运行（部署工作流先跑 `alembic upgrade head` 再换容器）。迁移之后、新容器起来之前，旧代码照旧运行：旧代码不写 `project_id`，插入的五级规则满足新 CHECK；旧代码按五级解析，表里还没有项目级规则，结果相同。
- **部署顺序**：AIH-TASK-136（迁移）→ 137（八级枚举与范围字段校验）→ 138（解析与服务）→ 139（接口）→ 140（前端）。每层合并后都能单独通过 CI：136 之后数据库已能存项目级规则，但应用的 `PricingScope` 还没有这三个值，应用层建不出、也读不到它们（表里也没有）；137 之后枚举是八级，项目级规则仍建不出来（请求的范围字段校验要求 `project_id`，而请求模型在 139 之前没有这个字段，返回 422），解析对项目级填 NULL、结果不变；138 之后计费按八级解析，但仍没有项目级规则（服务层函数已接受项目，接口还不传）；139 之后管理员才能建项目级规则、试算才能选项目。
- **回滚或前滚方式**：代码回滚到 136 之前要先降级迁移；表里一旦有项目级规则就不能降级（§2「降级」），只能前滚修复。
- **部分部署时的行为**：见「兼容窗口」。
- **监控指标和告警负责人**：照旧 `usage_pricing_error`（P2）；Kelvin。

## 9. 方案取舍

| 方案 | 优点 | 风险 | 未采用原因 |
| --- | --- | --- | --- |
| 采用：规则加 `project_id`，三个项目级排在客户级之前（Kelvin 2026-10-30 定） | 「这个项目按什么价」只看前三级就能回答；项目没有规则时行为与现在相同；与客户级对称，管理员不用学新概念 | 改生成列与触发器的迁移要逐字对齐 | — |
| A：只加一级「项目默认」 | 迁移与接口更小 | 项目要按模型分别定价时只能落回客户级，与「分开定价」不符；以后再细分又要过一次闸门 | Kelvin 选三级 |
| B：项目级与客户级按精细度交错（项目 + 供应商 + 模型 → 客户 + 供应商 + 模型 → 项目 + 供应商 → …） | 「越精细越优先」 | 客户的某个模型专价会压过项目的默认价，管理员很难说清一个项目到底按什么价 | Kelvin 选「全部在客户级之前」 |
| C：给项目加产品线字段、按（客户, 产品线）定价 | 产品线能用于分析 | 多一层迁移与接口；同一产品线下的两个项目无法分开定价 | Kelvin 定：只写约定、不落数据 |
| D：项目级规则不存 `tenant_id`，只存 `project_id` | 少一列冗余 | 按客户筛选看不到项目级规则；复合外键无从保证租户一致；`has_customer` 语义分裂 | 存两列、用复合外键保证一致更简单 |
| E：不改 `scope_key`，另加一个生成列与唯一索引 | 不重建已有生成列 | 两套「同一范围」的定义，触发器与服务层都要同时维护两套，容易漏 | 只维护一套范围键 |

## 10. 未决问题与假设

- **未决问题**：无。
- **尚未验证的假设**：
  1. 表的那条 ALTER 在 MySQL 8.4 上可行（2026-10-30 在一次性 MySQL 8.4 容器里用同形的表验证过：同名 CHECK 与同名唯一索引删后再加、改 STORED 生成列表达式、加复合外键在同一条语句里执行成功，已有行的 `scope_key` 正确重算，外键拒绝跨租户的项目；同一张表同一时机可以有两个 BEFORE UPDATE 触发器，只有 SIGNAL 的隔离触发器拒绝 INSERT 与 UPDATE，隔离期间 ALTER 与主触发器的 DROP 照常执行，删掉隔离触发器后写入恢复）；实现任务的 MySQL 迁移用例再验一次。
  2. 生产 `pricing_rules` 行数很少（至多数十行），重建表的锁等待可以忽略。
- **需要谁拍板**：已由 Kelvin 2026-10-30 拍板：三级、全部在客户级之前、产品线不落数据。
- **如果假设错误，具体后果**：假设 1 在生产版本上不成立时，那条原子 ALTER 整体失败，表不变、部署失败、旧容器继续运行（§5）；改写法需要重新过闸门。

## 11. 审查与版本绑定

审查方要回答的五个问题常驻在 [scripts/review_checklist.md](../../scripts/review_checklist.md) 的「设计审查」一节，此处不重复。

**版本绑定规则**（规则的出处是 [WORKFLOW §3](../../docs/WORKFLOW.md)；这里保留一份是因为审查材料里只有本 Issue 正文，两处改一起改）：

- Codex 的批准必须写成 `APPROVED: design v<N>`，`<N>` 取本 Issue 顶部的「设计版本」
- **设计版本一变，之前的 APPROVE 自动作废**，必须重新过闸门
- 什么算「实质修改」：改了契约、表结构、事务边界、状态机、失败语义、不变量控制。改错别字不算
- 实质修改时：把顶部版本 +1，在下方追加一条变更说明，状态退回 `READY_FOR_REVIEW`

### 任务拆分

| 任务 | 层 | 内容 |
| --- | --- | --- |
| AIH-TASK-136 | 数据库 | 迁移 0030（§2「数据库」，含预检、离线、降级拒绝与补完中断的 DDL）；模型的新列、外键、索引、两条 CHECK 文本与生成列表达式（`PricingScope` 不改，项目级的行在本任务里只用 SQL 文本读写）；`test_migrations.py` 在 head 上的 `pricing_rules` 形状改为 0030 之后的；`docs/database-schema.md` |
| AIH-TASK-137 | 数据库（模型） | `PricingScope` 八级与 `has_project`；`Scope` 加 `project_id`、`_in_scope` 比较四列、`scope_of` 带项目，`_level_scope` 在本任务里对项目级填 NULL（于是项目级查询匹配不到任何行，解析结果与现在相同）；请求与服务层的「范围字段恰好匹配」把 `project_id` 算进项目级的必填字段（请求模型还没有这个字段，所以项目级规则在本任务里建不出来，返回 422）；既有用例里断言五级或逐级建规则的写法同步（逐级建规则只遍历五个非项目级） |
| AIH-TASK-138 | 业务规则 | `resolve_pricing_rule` 与 `resolve_charge_inputs` 加 `project_id`，`list_rules` 的排序加 `project_id`，计费传 `event.project_id`，试算在本任务里传 `None`（行为不变）；服务层建草稿与列表的函数接受项目（项目属于客户、404 `PROJECT_NOT_FOUND`）、审计的范围带项目（接口在本任务里还不传项目，所以对外行为不变）；spec §16、§59、§74.3 与 `docs/pricing-engine.md` 改为八级 |
| AIH-TASK-139 | 接口 | 请求加 `project_id`（规则的建草稿与列表筛选、试算；试算服务按请求里的项目解析），视图加 `project_id` 与 `project_name`（服务层的视图组装同改）；`docs/api.md` |
| AIH-TASK-140 | 前端 | 规则页、详情页、试算页（§2「前端」）与浏览器验收的两个已有步骤 |

**AIH-TASK-136 不再往下拆**：登记前预审估计它（迁移连同 §7 的迁移场景）会超过约 1500 行。不拆的原因：136 一合并，迁移就在生产部署时执行；中断恢复、写入隔离与降级拒绝是这次迁移在生产上出问题时唯一的保护，它们的测试必须与迁移代码在同一个 PR 里通过 CI。把这些测试拆到后续任务，等于先把未经测试的恢复分支送上生产。迁移之外的部分已经拆出（137 起）。

### 设计闸门判定

同时满足才算通过：

- [x] 需求、非目标和验收标准明确
- [x] 关键契约与事务边界明确
- [x] 触碰的不变量都有控制措施
- [x] 失败路径都有确定的最终状态
- [x] 高风险控制都有测试场景
- [x] 没有未解决的阻断假设
- [x] 审批绑定到明确的设计版本

## 12. 版本变更记录

| 版本 | 日期 | 改了什么 | 触发原因 |
| --- | --- | --- | --- |
| v1 | 2026-10-30 | 初稿 | — |
| v4 | 2026-10-30 | §7「MySQL 用例的清理」改为沿用现有定价用例的清场写法（开始与 finally 都 TRUNCATE，被外键引用时先关外键检查），不再删除与重建删除触发器；两个隔离触发器各自按是否存在建与删，§7 加「只有一个隔离触发器」 | Codex v3 REQUEST_CHANGES：测试里的规则被用量事件 RESTRICT 外键引用，先删删除触发器再 DELETE 会失败并留下缺失的删除触发器；只建成或只删掉一个隔离触发器时的重跑没有测试 |
| v3 | 2026-10-30 | §7 加「MySQL 用例的清理」（写入规则行的用例在 finally 里绕过删除触发器清掉自己的行，被打断的迁移用例把库补回 head）；降级的离线模式改为最先抛错（先于「是否已完成」的判断），升级写明离线模式不做任何判断；降级步骤重新编号；`project_id` 列写明 `AFTER tenant_id`；§11 写明 AIH-TASK-136 不再拆的原因 | v2 获批后的登记前预审：降级先做读库的「是否已完成」判断、再处理离线模式，`--sql` 下做不到；新列的位置未定，迁移与模型的列序可能不一致；测试写入的项目级行删不掉，会让之后经过 0030 的降级被拒 |
| v2 | 2026-10-30 | 迁移的升级与降级都先判断是否已完成（完成即不发任何 DDL），再加「写入隔离」（隔离触发器用 `PRECEDES` 排在主触发器之前）：先建两个只 SIGNAL 的隔离触发器、全部 DDL 完成后才删，其间对 `pricing_rules` 的 INSERT 与 UPDATE 一律被拒；§4 去掉「这个窗口只靠服务层」的说法；§5「迁移中途失败」写明留下的状态；§7 加「迁移期间的写入隔离」「降级期间的写入隔离」，「离线升级」输出隔离触发器 | Codex v1 REQUEST_CHANGES：升级在 ALTER 之后、主触发器建成之前中断时，旧服务仍在运行，手工 SQL 能改已发布规则或写出重叠区间，数据库层的不可变与区间不重叠失效 |

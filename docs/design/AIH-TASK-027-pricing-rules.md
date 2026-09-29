# AIH-TASK-027 设计：定价规则 MARKUP 与 FIXED_RATE（已批准 v3）

> **来源**：设计闸门 Issue #178。本文件是 `APPROVED: design v3` 那一版正文的**逐字副本**，审查者为 Codex。
> 放进仓库，是因为 OpenClaw Worker 在沙箱里不联网、读不到 GitHub Issue。
> **实现以本文件为准**；与 Issue 不一致时，以 Issue #178 上被批准的 v3 为准。设计要改，就回到 Issue 升版本、重新过闸门，不要直接改本文件。
> 正文里的「T-D」即 [PHASE-2-plan.md](PHASE-2-plan.md) 的占位名；写「编号登记时分配」的实现任务，登记为 `AIH-TASK-027`。正文提到的其他占位名（T-A…T-L）对应的登记编号见 `docs/TODO.md` 的 Phase 2 一节。
> 与 Issue 正文的唯一差别：一处链接目标由 GitHub 网页上的 `../blob/main/…` 改成仓库内的相对路径，链接文字与其余正文逐字相同。

---

## 0. 准入判定

| 任务涉及 | 要填的范围 |
| --- | --- |
| 钱包 / 账本 / 定价 / 汇率 / 支付 / 幂等 / 状态机 | **全部章节** |
| 用量摄取 / 认证与会话（集成侧与平台侧）/ Webhook | §1–§7（**§6 不可省**） |
| 前端 / 文档 / CI / 脚本 | 不走本闸门 |
| 既不碰钱、也不碰用量摄取 / 认证与会话 / Webhook 的后端改动 | 不走本闸门 |

本任务档位：`全部`。它决定客户被扣多少钱（§15 的 MARKUP / FIXED_RATE 与 §16 的五级优先顺序），是版本化的状态机。

---

状态：`READY_FOR_REVIEW`
负责人：Claude
**设计版本**：`v3`（v1：FIXED_RATE 完整性只在服务层 → v2 发布跃迁触发器；v2：已截断区间的重叠数据库挡不住 → v3 区间不重叠触发器，见 §12）
对应需求：spec §14、§15、§15.1、§16、§59、§66、§74.3、§80、§89、§113；`REQ-PRICE-001`、`REQ-FIN-001`、`REQ-FIN-002`、`REQ-PRIV-002`；ADR-0008（含税定价）
目标 PR：待开（批准后在 `.platform/tasks.yaml` 登记实现任务，编号登记时分配）
Phase 2 计划：`docs/design/PHASE-2-plan.md` 的 T-D；依赖 `docs/design/AIH-TASK-025-ai-catalog.md`（已批准 v4）

## 1. 目标与边界

- **要解决的问题**：spec §15 定义两种客户定价策略（MARKUP：MYR 估算成本 × 倍数；FIXED_RATE：Acuven 自定的 MYR 分量价）；§16 定义五级规则优先顺序、按 `occurred_at` 选、已选定的规则永久保留、重试不许换到新发布的规则；§74.3 给了两张表的最低字段与「混合 / 不完整的规则在数据库与服务层都要拒绝」。现在都没有。
- **可观察的完成标准**：
  1. 管理员能按五级范围之一建规则草稿（MARKUP 带倍数、FIXED_RATE 带分量），编辑、丢弃、发布（可预约未来生效，§59）、停用（§59「disable rule」），查看历史；每一步有审计。
  2. 混合（MARKUP 带分量、FIXED_RATE 带倍数）或不完整的规则在数据库层与服务层都被拒绝。
  3. 只读查询函数 `resolve_pricing_rule(session, tenant_id, provider_id, model_id, occurred_at)` 按 §16 的顺序返回那一刻生效的规则，**与发布串行**：一个事件一旦选到某条规则（或确定无规则之外的某条），以后永远选到同一条。
- **明确不做什么**：
  - 计费额计算、舍入、试算预览 —— T-G（预览依赖 FX，§59 的「preview calculation」归 T-G）
  - 税额反算与 `tax_policy_versions` —— Phase 4（ADR-0008）；本任务只保证「价是含税价」被写明
  - reprocess / rebill 换规则 —— Phase 8（§16 最后一句、§19）
  - 前端 —— T-L
- **现有行为与问题证据**：`app/models/` 没有定价表；AIH-TASK-025 §2「对下游任务的契约」规定 T-D 用它的计价分量、不许给 `RETIRED` 的目录项建新规则、发布方要与计费解析串行。

## 2. 设计概要

```text
管理端：建草稿 / 改草稿 / 丢弃 → 写规则与分量 → 审计 → commit
管理端：发布 / 停用 →
    SELECT … FROM pricing_rule_locks WHERE id = 1 FOR UPDATE
    拿到锁之后取 t（服务端当前时间向上取整到下一个整秒）
    校验 → 截断同范围的末尾规则 / 写本规则区间与状态 → 同一事务复查 → 审计 → commit

计费（T-G / T-H 在计费事务里调用，本任务只提供）：
    （resolve_model 已对供应商行加 FOR SHARE）
    SELECT … FROM pricing_rule_locks WHERE id = 1 FOR SHARE（持有到计费事务提交）
    resolve_pricing_rule(...)：按五级顺序，逐级加锁读该级范围内覆盖 occurred_at 的
        PUBLISHED / RETIRED 规则；第一级命中即返回（含分量）；五级都没有 → None（T-G 判 PRICING_ERROR）
```

### 为什么用一张单行锁表

发布与计费必须串行（否则发布事务提交前，计费可能读到旧规则并写进快照，提交后同一时刻又解析到新规则 —— 025 v3 被判阻断的同一个坑）。025 与价格版本用供应商行做这把锁，但「客户默认」「全局默认」两级规则没有供应商，锁不到任何供应商行；按租户行锁又会与计费既有的「钱包 → 租户」顺序相反。所以新建 `pricing_rule_locks`（迁移写入唯一一行 `id = 1`）：

- 发布 / 停用：`FOR UPDATE`。
- 计费：`FOR SHARE`，在 `resolve_model` 之后、锁钱包之前取，持有到计费事务提交。共享锁之间不冲突，计费事务彼此不阻塞；只有低频的发布会让计费等一下。
- 计费路径的锁顺序变为：**供应商（S）→ 定价规则锁（S）→ 钱包 → 租户**（写进 T-H 契约）。发布路径只拿这一把锁，不形成环。
- 这张表只有一行、没有业务字段；BEFORE INSERT / DELETE 触发器保证它恰好一行。

### 数据库（迁移序号取实现时的下一个，预计 0015）

**`pricing_rules`**

| 列 | 类型 | 说明 |
| --- | --- | --- |
| `id`、`public_id` | | |
| `priority_scope` | VARCHAR(32)，CHECK 五个取值 | `CUSTOMER_PROVIDER_MODEL` / `CUSTOMER_PROVIDER` / `CUSTOMER` / `GLOBAL_PROVIDER_MODEL` / `GLOBAL`（§16 从高到低） |
| `tenant_id` | BIGINT NULL FK → `tenants.id`，RESTRICT | |
| `provider_id` | BIGINT NULL FK → `ai_providers.id`，RESTRICT | |
| `model_id` | BIGINT NULL | 复合外键 `(model_id, provider_id)` → `ai_models(id, provider_id)`，RESTRICT |
| `strategy` | VARCHAR(16)，CHECK `MARKUP` / `FIXED_RATE` | |
| `markup_multiplier` | DECIMAL(20,8) NULL | |
| `status` | CHECK `DRAFT` / `PUBLISHED` / `RETIRED` / `DISCARDED` | `DISCARDED` 是 §74.3 之外补的：丢弃的草稿留痕不删 |
| `effective_from` / `effective_to` | DATETIME NULL | 半开区间；草稿为空 |
| `scope_key` | 生成列（STORED）：`CONCAT(priority_scope, ':', COALESCE(tenant_id, 0), ':', COALESCE(provider_id, 0), ':', COALESCE(model_id, 0))` | 把「同一范围」变成一个非空值，绕开 MySQL 唯一约束不管 `NULL` 的问题 |
| `open_slot` | 生成列（STORED）：`status = 'PUBLISHED' AND effective_to IS NULL` 时为 1，否则 `NULL` | |
| `created_by` / `approved_by` | FK → `users.id` | `approved_by` 即发布人 |
| `created_at` / `updated_at` / `approved_at` | DATETIME | |

约束：

- **范围与 NULL 组合一一对应**（§74.3「不能有含糊的 NULL 组合」）：
  ```sql
  (priority_scope = 'CUSTOMER_PROVIDER_MODEL' AND tenant_id IS NOT NULL AND provider_id IS NOT NULL AND model_id IS NOT NULL)
  OR (priority_scope = 'CUSTOMER_PROVIDER'    AND tenant_id IS NOT NULL AND provider_id IS NOT NULL AND model_id IS NULL)
  OR (priority_scope = 'CUSTOMER'             AND tenant_id IS NOT NULL AND provider_id IS NULL     AND model_id IS NULL)
  OR (priority_scope = 'GLOBAL_PROVIDER_MODEL' AND tenant_id IS NULL    AND provider_id IS NOT NULL AND model_id IS NOT NULL)
  OR (priority_scope = 'GLOBAL'               AND tenant_id IS NULL     AND provider_id IS NULL     AND model_id IS NULL)
  ```
  §16 没有的组合（例如「全局 + 供应商、不带模型」「客户 + 模型、不带供应商」）因此无法写入。
- **策略与倍数**：`(strategy = 'MARKUP' AND markup_multiplier IS NOT NULL AND markup_multiplier > 0) OR (strategy = 'FIXED_RATE' AND markup_multiplier IS NULL)`。
- 状态与区间、发布人同 T-B 的写法：`DRAFT` / `DISCARDED` ⇒ 区间与发布人为空；`PUBLISHED` / `RETIRED` ⇒ 发布人非空；`effective_from IS NULL OR effective_to IS NULL OR effective_from < effective_to OR (status = 'RETIRED' AND effective_from = effective_to)`。
- UNIQUE `(scope_key, open_slot)`：同一范围至多一条未截断的已发布规则。
- UNIQUE `(id, strategy)`：给分量表的复合外键用。
- 索引：`(priority_scope, tenant_id, provider_id, model_id, effective_from)`（解析时逐级查）。

**`pricing_rule_components`**（只属于 FIXED_RATE 规则）

| 列 | 类型 | 说明 |
| --- | --- | --- |
| `id` | BIGINT PK | |
| `pricing_rule_id` | BIGINT | |
| `strategy` | VARCHAR(16)，CHECK `= 'FIXED_RATE'` | 复合外键 `(pricing_rule_id, strategy)` → `pricing_rules(id, strategy)`，RESTRICT：**MARKUP 规则在数据库层挂不上分量** |
| `usage_meter_component_id` | FK → `usage_meter_components.id`，RESTRICT | 025 的计价分量；§74.3 的 `meter_type_id`、`component_code`、`unit` 由它唯一确定 |
| `unit_quantity` | DECIMAL(20,8)，CHECK > 0 | |
| `rate_amount` | DECIMAL(20,8)，CHECK > 0 | **MYR 含税单价**（ADR-0008） |
| `currency` | CHAR(3)，CHECK `= 'MYR'` | §74.3 |
| `created_at` | DATETIME | |

- UNIQUE `(pricing_rule_id, usage_meter_component_id)`。

**不可变（触发器，照 0006 的写法与权限预检）**：与价格版本相同的三条 ——

- `pricing_rules` BEFORE UPDATE：`DRAFT` 可改；`DISCARDED` 不可改；`PUBLISHED` / `RETIRED` 只允许改 `effective_to`、`status`（仅 `PUBLISHED → RETIRED`）、`updated_at`。
- `pricing_rules` BEFORE DELETE：一律拒绝。
- `pricing_rule_components` BEFORE INSERT / UPDATE / DELETE：所属规则不是 `DRAFT` 就拒绝。
- `pricing_rule_locks`：BEFORE INSERT（已有一行时）与 BEFORE DELETE 拒绝。
- `pricing_rules` BEFORE INSERT：`NEW.status` 必须是 `DRAFT`（任何规则都只能经「草稿 → 发布」这一步进入已发布，发布跃迁上的检查因此绕不过去）。
- **发布跃迁上的完整性**（`pricing_rules` BEFORE UPDATE，`OLD.status = 'DRAFT' AND NEW.status = 'PUBLISHED' AND NEW.strategy = 'FIXED_RATE'` 时）：
  1. 该规则至少有一个分量，否则 SIGNAL；
  2. 对该规则分量所涉及的每一个计量类型，025 的 `usage_meter_components` 里该类型的**每一个**分量都在该规则的分量里，否则 SIGNAL：
     ```sql
     IF (SELECT COUNT(*) FROM pricing_rule_components WHERE pricing_rule_id = NEW.id) = 0 THEN SIGNAL …; END IF;
     IF EXISTS (
       SELECT 1 FROM usage_meter_components umc
       WHERE umc.meter_type_id IN (
               SELECT m.meter_type_id FROM pricing_rule_components c
               JOIN usage_meter_components m ON m.id = c.usage_meter_component_id
               WHERE c.pricing_rule_id = NEW.id)
         AND umc.id NOT IN (
               SELECT usage_meter_component_id FROM pricing_rule_components WHERE pricing_rule_id = NEW.id)
     ) THEN SIGNAL …; END IF;
     ```
  服务层在发布前做同样的校验（给出 409 `PRICING_RULE_INCOMPLETE` 与缺失清单）；触发器是绕过服务路径时的最后一道。
- **区间不重叠由数据库兜底**（`pricing_rules` BEFORE UPDATE，`NEW.status IN ('PUBLISHED','RETIRED')` 且 `effective_from`、`effective_to` 或 `status` 有变化时）：触发器先执行 `SELECT id INTO @lock_id FROM pricing_rule_locks WHERE id = 1 FOR UPDATE`（与服务层发布同一把锁，把绕过服务的直接写入也串行起来），再检查同一 `scope_key`内是否存在另一行满足：状态为 `PUBLISHED` / `RETIRED`、区间非空（`effective_from IS NULL OR effective_to IS NULL OR effective_from < effective_to`）、且与本行区间相交 —— 相交判定 `COALESCE(o.effective_from, '1000-01-01') < COALESCE(NEW.effective_to, '9999-12-31') AND COALESCE(NEW.effective_from, '1000-01-01') < COALESCE(o.effective_to, '9999-12-31')`，另一行条件 `o.scope_key = NEW.scope_key AND o.id <> NEW.id`；存在即 SIGNAL。空区间（撤销的预约）不参与判定。
- **服务层的写入顺序**（保证正常路径过得了上一条触发器）：发布时先截断前一个（`effective_to` 写入），再把本行从草稿改为已发布；撤销预约时先把被撤销的一行改成空区间，再把前一个恢复为未截断。
- 完整性一经发布就保持成立：已发布规则的分量不可增删改（上一条触发器）；025 不允许给已有计量类型追加分量（`QUANTITY` 类型恰好一个分量由数据库保证，`LLM_TOKEN` 的四个分量是种子），所以「该类型的全部分量」这个集合不会在发布之后变大。

### 完整性（发布时，服务层）

- MARKUP：倍数 > 0，没有分量（数据库已保证）。
- FIXED_RATE：至少一个分量；凡是出现的计量类型，它在 025 里的全部分量都必须出现（与价格版本同一条规则）。
- 目录状态：规则引用的供应商、模型，以及 FIXED_RATE 分量所属计量类型都不能是 `RETIRED`（025 契约）；规则引用的租户必须存在（账户状态不限：停用的客户也可以预先配置价格）。

### 发布：区间怎么定

按 `scope_key` 分组，每组是一条只在末尾追加的时间线，规则与价格版本相同：拿到锁之后取 `t`；可选的预约生效时刻 F 必须 ≥ `t`、且大于该组末尾规则的起点；未指定则取 `t`；末尾规则若未截断，截断于本规则的起点；末尾规则已截断或已停用（尽头 E）时，本规则从 `max(E, 所请求或 t)` 起。请求的 F < `t` → 422 `EFFECTIVE_FROM_IN_PAST`，F 早于末尾起点或尽头 → 409 `EFFECTIVE_FROM_CONFLICT`。每次写完在同一事务里复查该组区间（只有第一条起点可为空；前一条的尽头 ≤ 后一条的起点），不满足回滚。

**回溯生效只允许「全局默认」的第一条**（与价格版本不同，原因是规则有五级下落）：

- 对任何一级 L 补一条对过去生效的规则，会让那些时刻原本在 L 以下某一级选中规则、**已经计过费**的事件，重新解析时改选 L —— 结果变了。
- 只有最低一级 `GLOBAL` 没有「以下」：在它之前，未被更高级覆盖的时刻都解析不到规则（`PRICING_ERROR`，从未扣费），补上只把「无规则」变成「有规则」，更高级覆盖的时刻结果不变。
- 所以：`GLOBAL` 范围且该组从没有已发布规则、且请求未给生效时刻 → `effective_from = NULL`（一直以来）；**其他一切情况 `effective_from ≥ t`**。

**停用（§59 disable rule）**：与价格版本的退役同一套 ——

| 被停用的规则 | 做什么 |
| --- | --- |
| 当前末尾、已开始生效 | `effective_to = t`，`RETIRED`：`t` 起该范围不再命中，事件**下落到更低一级**（或无规则 → `PRICING_ERROR`） |
| 当前末尾、尚未开始（预约） | `effective_to = effective_from`（空区间），`RETIRED`；前一条若被它截断，前一条的 `effective_to` 恢复为 `NULL` |
| 已被后继截断的历史规则 | 409 `PRICING_RULE_NOT_RETIRABLE` |

停用后的空档不回填：停用的意思就是「此后这一级不再适用」。

**为什么与计费不会交错**：发布 / 停用在定价规则锁的排他模式内、`t` 在拿到锁之后取；计费在同一把锁的共享模式内，且只处理 `occurred_at ≤` 持锁后当前时间的事件（025 的 T-H 契约）。于是计费先 → 事件的 `occurred_at` < `t` ≤ 本次写入产生的任何新边界，新旧表对它的五级解析结果相同；发布先 → 计费读到的已是新表。加上「只在末尾追加 / 只有全局默认的第一条可回溯」，**一个（租户, 供应商, 模型, 时刻）一旦选到某条规则，以后永远选到同一条**（§16「Retrying the same event must not select a newly published rule」）。

### 解析（§16 的下落语义）

`resolve_pricing_rule` 依次查五级，每一级找「范围匹配、状态为 `PUBLISHED` 或 `RETIRED`、`effective_from ≤ occurred_at < effective_to`」的规则（`NULL` 边界视为无穷）：

1. `CUSTOMER_PROVIDER_MODEL`：`tenant_id`、`provider_id`、`model_id` 都等于事件的
2. `CUSTOMER_PROVIDER`：`tenant_id`、`provider_id` 相等
3. `CUSTOMER`：`tenant_id` 相等
4. `GLOBAL_PROVIDER_MODEL`：`provider_id`、`model_id` 相等
5. `GLOBAL`

第一级命中即返回（规则 + 分量）；五级都没有 → `None`。**一旦命中某一级，就不因该规则缺某个分量而下落到更低一级**（T-G 契约：命中的 FIXED_RATE 规则缺事件所需分量 → `PRICING_ERROR`），否则同一事件的价格会取决于配置的缺口，也让「选定的规则」变得含糊。所有读都是加锁读（`FOR SHARE`，避免 REPEATABLE READ 的快照读陷阱）。

### 含税（ADR-0008）

- FIXED_RATE 分量的 `rate_amount` 是 **MYR 含税单价**；MARKUP 算出的计费额也是含税金额。本任务不存税额、不引用税务政策版本。
- 接口的字段说明、`docs/api.md` 与本任务新建的 `docs/pricing-engine.md`（计划要求的文档，本任务写规则部分，T-G 补计算部分）都写明「含税（tax-inclusive）」；录入者录错不会报错，这是 ADR-0008 已接受的代价，界面标注由 T-L 负责。
- 倍数没有「含税」与否之分：它乘的是供应商成本，得到的就是含税计费额；税率变化吃掉的是毛利（ADR-0008 已写明）。

### 接口

全部只有 ADMIN，处理函数第一条语句 `require_admin`，加进 `EXPECTED_ADMIN_ROUTES` 与 `VALID_BODIES`；id 用 `public_id`（客户用 `customer_id`，即租户 `public_id`）；分页照 §108；金额与倍数一律 JSON 字符串、精确解析、超过 8 位小数 422。

| 方法与路径 | 作用 |
| --- | --- |
| `GET /api/v1/admin/pricing-rules` | 列表（即 §59「查看价格历史」），可按 `priority_scope`、`customer_id`、`provider_id`、`model_id`、`status` 筛；按范围、`effective_from` 排序 |
| `POST /api/v1/admin/pricing-rules` | 建草稿：`priority_scope`、按范围给 `customer_id` / `provider_id` / `model_id`、`strategy`、`markup_multiplier`（MARKUP）或 `components[]`（FIXED_RATE：`component_code`、`unit_quantity`、`rate_amount`） |
| `GET /api/v1/admin/pricing-rules/{rule_id}` | 详情 |
| `PATCH /api/v1/admin/pricing-rules/{rule_id}` | 只对草稿（§89 的 PATCH）：可改策略、倍数、分量（整体替换）；范围字段不可改（改范围请新建）；非草稿 409 |
| `POST /api/v1/admin/pricing-rules/{rule_id}/publish` | 发布：可选 `effective_from`（RFC 3339 带时区、整秒）（§89 没列这个接口，§66 有 `PRICING_PUBLISH`；§89 允许调整命名） |
| `POST /api/v1/admin/pricing-rules/{rule_id}/retire` | 停用：必填 `reason` |
| `POST /api/v1/admin/pricing-rules/{rule_id}/discard` | 丢弃草稿 |

- 请求里范围字段与 `priority_scope` 不匹配（多给或少给）→ 422。
- 已发布再发布 → 200 不写。

错误：

| HTTP | `error.code` | 什么时候 |
| --- | --- | --- |
| 404 | `PRICING_RULE_NOT_FOUND` / `CUSTOMER_NOT_FOUND` / `AI_PROVIDER_NOT_FOUND` / `AI_MODEL_NOT_FOUND` / `USAGE_METER_COMPONENT_NOT_FOUND` | 不存在，或模型不属于该供应商 |
| 409 | `PRICING_RULE_NOT_DRAFT` / `PRICING_RULE_INCOMPLETE` / `CATALOG_ITEM_RETIRED` / `PRICING_RULE_NOT_RETIRABLE` / `PRICING_RULE_FINAL` / `EFFECTIVE_FROM_CONFLICT` | 见上文 |
| 422 | `EFFECTIVE_FROM_IN_PAST` / `VALIDATION_ERROR` | 见上文 |

### 事务边界

每个写接口一个 `session_scope`。发布与停用：锁 `pricing_rule_locks` → 取 `t` → 读并校验 → 写本规则与（需要时）前一条 → 复查 → 审计 → 提交。草稿操作不拿锁。

### 外部系统与异步边界

无。不写 outbox。

### 时间语义

与价格版本相同：UTC 无时区、半开区间、与 `occurred_at` 比较；`t` 向上取整到整秒，其余服务端时间截到整秒；请求的 `effective_from` 必须是整秒。

### 金额精度

- `rate_amount`、`unit_quantity`、`markup_multiplier`：DECIMAL(20,8)，正数，精确解析，超精度拒绝。
- 本任务不做计算；§80 的一次舍入归 T-G。

### 审计

| `action` | 什么时候 |
| --- | --- |
| `PRICING_CREATE`（§66） | 建草稿 |
| `PRICING_UPDATE`（§66） | 改草稿 |
| `PRICING_PUBLISH`（§66） | 发布；前：被截断的前一条规则的 `public_id` 与原 `effective_to` |
| `PRICING_RETIRE` | 停用；`reason`；被恢复的前一条（若有） |
| `PRICING_DISCARD` | 丢弃草稿 |

`entity_type = pricing_rule`、`entity_id = public_id`；前后状态含范围（租户 `public_id`、供应商 / 模型 `code`）、策略、倍数、分量、区间，不含内部 id。后两个动作按先例补上并记进 `docs/TODO.md`。

### 对下游任务的契约

| 下游 | 契约 |
| --- | --- |
| T-G | MARKUP：未舍入的 MYR 估算成本 × 倍数，只乘一次，再按 §80 舍入一次；FIXED_RATE：按分量取数（025 的 `quantity_field`）× `rate_amount` / `unit_quantity` 求和，再舍入一次，与供应商成本无关；命中的 FIXED_RATE 规则缺所需分量 → `PRICING_ERROR`，**不下落**；`resolve_pricing_rule` 返回 `None` → `PRICING_ERROR` |
| T-H | 计费事务里：`resolve_model`（供应商 S）→ 取 `pricing_rule_locks` 的 `FOR SHARE` → 价格、规则的加锁读 → 钱包 → 租户；`pricing_rule_id` 写进事件快照 |
| T-L | 界面标注「含税」；FIXED_RATE 按计量类型成组录入分量 |

## 3. 不变量影响矩阵

| 不变量 | 是否触碰 | 可能怎样被破坏 | 设计控制 | 验证方式 |
| --- | --- | --- | --- | --- |
| INV-1 | 否 | — | — | — |
| INV-2 事件不重复扣费 | 否（间接） | 规则变化触发重算 | 不提供重算入口 | — |
| INV-3 / INV-4 / INV-5 | 否 | 不碰钱包与账本 | — | — |
| INV-6 事件保留版本引用 | **是** | ① 已被引用的规则或分量被改；② 发布与计费交错；③ 回溯规则让已计费时刻改选另一级；④ 缺分量时下落，同一事件的选择取决于配置缺口；⑤ 不完整的 FIXED_RATE 规则被发布（绕过服务），命中它的事件全部 `PRICING_ERROR` | ① 触发器；② 单行锁串行、`t` 在锁后取、加锁读；③ 只有全局默认的第一条可回溯；④ 命中即停、缺分量判错不下落；⑤ 发布跃迁触发器 + 只能以草稿插入 | 触发器用例、并发用例、性质用例、完整性直接写库用例（§7） |
| INV-7 客户不可见成本毛利 | **是** | 倍数（即 markup）经客户接口泄露 | 只有管理端路由 | 越权用例 |
| INV-8 租户不可互访 | **是** | 客户级规则挂错租户；A 的规则被用于 B 的事件 | 范围列有外键；解析按事件的 `tenant_id` 精确匹配 | 两个租户的解析用例 |
| INV-9 | 否 | — | — | — |
| INV-10 金额用 Decimal | **是** | 单价或倍数经 `float` 失真 | 字符串精确解析，DECIMAL | 精度用例 |
| INV-11 / INV-12 | 否 | — | — | — |
| INV-13 状态与事件原子提交 | **是** | 规则发布了而审计没写；前一条截断了而本条没发布 | 同一事务 | 回滚用例（SQLite 与 MySQL 各一次） |
| INV-14 | 否 | — | — | — |

**本任务自己的不变量**：一个（租户, 供应商, 模型, 时刻）一旦选到某条规则，以后永远选到同一条（原本无规则的时刻除外）；同一范围在任一时刻至多一条规则生效；混合或不完整的规则不可能处于已发布状态。

## 4. 状态与并发

| 当前 | 操作 | 新状态 |
| --- | --- | --- |
| — | 建草稿 | `DRAFT` |
| `DRAFT` | 编辑 | `DRAFT` |
| `DRAFT` | 丢弃 | `DISCARDED` |
| `DRAFT` | 发布 | `PUBLISHED` |
| `PUBLISHED` | 停用（末尾） | `RETIRED` |
| `PUBLISHED` | 被后继截断 | `PUBLISHED`（`effective_to` 写入） |
| `RETIRED` / `DISCARDED` | 任何 | 409 |

- **串行化**：发布 / 停用在 `pricing_rule_locks` 排他锁内；计费在同一行共享锁内。草稿编辑与发布同一条规则：两者都对规则行 `FOR UPDATE`，串行。
- **数据库保证的唯一性**：`(scope_key, open_slot)`；`(pricing_rule_id, usage_meter_component_id)`；`(id, strategy)`；`public_id`。
- **幂等键**：无客户端幂等键；重复发布 200 不写。
- **相同 ID、不同载荷**：不适用。
- **重试层**：服务端不重试。
- **原子提交**：规则、前一条的截断或恢复、复查、审计。

## 5. 失败与恢复矩阵

| 失败点 | 对外结果 | 数据最终状态 | 是否可重试 | 恢复来源 | 告警 |
| --- | --- | --- | --- | --- | --- |
| 校验失败（含范围与字段不匹配、混合策略） | 422 | 无改动 | 改后可 | — | 无 |
| 不完整 / 目录停用 / 状态不对 / 时刻冲突 | 409 | 无改动 | 改后可 | — | 无 |
| 复查不通过（实现错误） | 500 | 回滚 | 否 | 修代码 | 错误日志 |
| 审计或提交失败 | 500 | 回滚 | 是 | — | 错误日志 |
| 锁等待超时（与计费争规则锁） | 500 | 回滚 | 是 | — | 错误日志 |
| 提交成功、响应丢失 | 超时 | 已发布 | 重发 → 200 不写 | — | 无 |
| 发布了错误的规则 | — | 已被引用则不可改 | 发布新规则（从 `t` 起）；历史纠正走 Phase 8 reprocess | 审计 | 无 |
| 某客户没有任何可命中的规则 | — | T-H 判 `PRICING_ERROR` | 管理员补规则后重新入队 | — | T-H 的定价错误告警 |

## 6. 数据与安全边界

- **租户过滤**：客户级规则以租户外键绑定；解析按事件的 `tenant_id` 精确匹配，不会用到别家的客户级规则。
- **鉴权主体**：只有 ADMIN。没有客户或集成侧接口（客户永远看不到倍数与成本，INV-7 / REQ-PRIV-002）。
- **禁止返回的字段**：内部自增 id。
- **日志 / 审计 / 异常**：审计含规则内容（业务数据）；应用日志不打印分量明细。
- **密钥 / prompt**：不涉及。
- **保留与删除**：不删除任何行（触发器）。
- **公开仓库提醒**：测试与文档用虚构价格与倍数。

## 7. 测试证据计划

| 风险/需求 | 测试层级 | 场景 | 预期结果 |
| --- | --- | --- | --- |
| 正常路径 | integration（API） | 五级各建一条并发布；MARKUP 与 FIXED_RATE 各一 | 各步状态、区间、审计正确 |
| 五级下落 | unit | 逐级去掉规则（停用），对同一事件解析 | 依次落到下一级；全无 → None |
| 命中即停 | unit | 客户级 FIXED_RATE 只含 `AUDIO_SECOND`，事件是 `LLM_TOKEN` | 返回客户级规则（不下落）；T-G 契约判 `PRICING_ERROR` |
| 租户隔离 | unit | 租户 A 的客户级规则，对租户 B 的事件解析 | 不命中 |
| 范围与 NULL 组合 | integration（MySQL） | 直接插入 §16 之外的组合（全局 + 供应商无模型、客户 + 模型无供应商、只有模型） | CHECK 拒绝 |
| 混合策略 | integration（MySQL + API） | MARKUP 带分量（直接插入）；FIXED_RATE 带倍数；MARKUP 无倍数或倍数 ≤ 0 | 外键 / CHECK 拒绝；API 422 |
| FIXED_RATE 完整性 | integration | 零个分量发布；`LLM_TOKEN` 只给三个分量 | 409 |
| 完整性由数据库兜底（MySQL） | integration | 绕过服务：直接 `UPDATE pricing_rules SET status = 'PUBLISHED'`（零个分量；`LLM_TOKEN` 缺一个分量）；直接 `INSERT` 一条 `status = 'PUBLISHED'` 的规则 | 触发器 SIGNAL 拒绝；完整的 FIXED_RATE 与 MARKUP 规则直接改状态则通过（证明触发器不误杀） |
| 目录停用 | integration | 停用的供应商 / 模型 / 计量类型 | 409 |
| 预约与撤销预约 | integration | 预约未来生效；生效前停用 | 前一条截断于 F / 恢复为空；被撤销的规则永不命中 |
| 不许回溯 | integration | F < `t`；非全局范围的第一条不给生效时刻 | 422；`effective_from = t`（不是空） |
| 全局默认第一条回溯 | integration | 从没有全局默认时发布、不给生效时刻 | `effective_from` 为空；很久以前的时刻命中它；同一时刻原本被客户级命中的事件仍命中客户级 |
| 停用后下落 | integration | 停用正在生效的客户级规则 | `t` 前命中客户级、`t` 起落到全局 |
| 停用历史规则 | integration | 停用已被截断的规则 | 409 |
| 触发器（MySQL） | integration | 改已发布规则的倍数 / 范围 / 起点；删规则；给已发布的 FIXED_RATE 增删分量；把 `RETIRED` 改回；删或再插锁表行 | SIGNAL 拒绝；只有 `effective_to` 与 `PUBLISHED → RETIRED` 通过 |
| 唯一约束（MySQL） | integration | 同一范围两条未截断的已发布规则（含全局范围，验证 `scope_key` 绕开 NULL） | 拒绝 |
| 区间不重叠（MySQL） | integration | 绕过服务直接写：同一范围两条**已截断**且相交的规则（部分相交、包含、起点相同）；一条未截断与一条起点更晚的已截断；首尾相接（`to = 下一条 from`）；空区间与任何区间；不同范围相交 | 相交的被拒绝；首尾相接、空区间、不同范围通过；服务层正常的发布 / 撤销预约路径通过 |
| 发布与计费并发 | integration（真 MySQL，两个连接） | ① 发布写完未提交时，计费取共享锁并解析（`occurred_at ≥ t`）② 反过来 | ① 计费阻塞到发布提交，选到新规则 ② 发布阻塞到计费提交，`t` 晚于事件；提交后再解析一致 |
| 共享锁互不阻塞 | integration（真 MySQL） | 两个计费事务同时取共享锁 | 不阻塞 |
| 快照读陷阱 | integration（真 MySQL） | 计费事务先普通读、另一连接发布并提交、再解析 | 选到新规则（加锁读） |
| 时间稳定性（性质用例） | unit | 随机的五级「发布 / 预约 / 停用 / 撤销预约」序列，每步后对早于该步 `t` 的固定时刻解析 | 一旦选到规则 R，之后永远是 R |
| 精度 | integration | 单价与倍数 8 / 9 位小数；0；负数；数字而非字符串 | 通过 / 422 |
| 事务中途失败 | integration（SQLite 与 MySQL 各一次） | 发布截断前一条后审计抛错 | 两条都回滚 |
| 鉴权 | integration | 匿名 401、CUSTOMER 403；首条语句 `require_admin`；路由枚举 | 同左 |
| 迁移 | integration（MySQL） | upgrade 后锁表恰好一行；downgrade | 同左 |

## 8. 迁移与上线

- **数据迁移步骤**：建 `pricing_rule_locks`（写入 `id = 1`）、`pricing_rules`、`pricing_rule_components`，生成列、约束、触发器（先做 0006 同款预检）。序号取实现时的下一个。
- **锁表与性能影响**：新表。计费路径多一次单行共享锁（T-H 接入时生效）。
- **兼容窗口 / 部署顺序 / 部分部署**：同 T-B（新表、先迁移后代码、失败时旧容器继续服务）。
- **回滚或前滚**：downgrade 删触发器、删表；录了规则之后回滚会丢规则 —— 前滚。
- **生产上的初始数据**：不预置规则；上线后由管理员录入（至少一条全局默认，否则所有事件都会 `PRICING_ERROR`；这一条写进 `docs/runbook.md` 的上线步骤由 T-H 负责）。
- **监控**：无新增；无规则在 T-H 以 `PRICING_ERROR` 告警。

## 9. 方案取舍

| 方案 | 优点 | 风险 | 未采用原因 |
| --- | --- | --- | --- |
| **采用**：单行锁表串行 + 按范围只在末尾追加 + 触发器 + 只有全局默认第一条可回溯 | 所有五级都有同一把锁；与 025 / 价格版本同一套证明；历史选择永不改变 | 计费多一次单行共享锁；规则发布会短暂阻塞计费 | — |
| 锁供应商行 | 与价格版本共用 | 客户默认、全局默认两级没有供应商 | 不可行 |
| 发布时按 id 顺序锁全部供应商行 | 不加新表 | 与并发建供应商之间有缝；供应商越多锁越多 | 不采用 |
| 锁租户行 | 客户级规则天然有租户 | 全局规则没有租户；计费路径是「钱包 → 租户」，规则解析在钱包之前，顺序会反 | 不采用 |
| 任何范围的第一条都可回溯 | 初始配置时能补历史 | 让已按低一级计过费的时刻改选高一级 | 只允许全局默认 |
| 命中的 FIXED_RATE 缺分量时下落 | 少出错误 | 同一事件的价格取决于配置缺口；「选定的规则」变得含糊 | 命中即停 |
| 范围用可空列 + 应用层判断 | 表更简单 | §74.3 明确要求数据库拒绝含糊组合 | CHECK 显式列出五种组合 |
| 分量存 `meter_type_id` + `component_code` + `unit`（§74.3 字面） | 字面贴合 | 与 025 的分量表重复 | 引用 025 分量行，语义可追溯 |

## 10. 未决问题与假设

- **未决问题**：无阻断项。
- **需要谁拍板**：无。
- **尚未验证的假设**：
  1. MySQL 8.4 支持以 `CONCAT(…, COALESCE(…))` 定义的 STORED 生成列参与唯一约束 —— 支持（确定性表达式）；迁移测试在 CI 的 MySQL 上实跑。
  2. 复合外键 `(pricing_rule_id, strategy)` 引用 `pricing_rules(id, strategy)` 时，子表的 `strategy` 由 CHECK 固定为 `FIXED_RATE`，父行若为 MARKUP 则外键不成立 —— 这是本设计要的效果；父行 `strategy` 在草稿阶段可改（MARKUP ↔ FIXED_RATE），改成 MARKUP 前服务层先删分量（同一事务），否则外键拒绝更新（RESTRICT）。
- **如果假设错误**：1 → 改用 `scope_key` 普通列由服务层写入、触发器校验，不改语义。

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

- `alembic/versions/<日期>_<序号>_pricing_rules.py`、`alembic/env.py`
- `app/models/pricing_rules.py`（新）、`app/models/auth.py`（审计动作）
- `app/repositories/pricing_rules.py`（新，含 `resolve_pricing_rule` 与取共享锁的函数）
- `app/services/pricing_rules.py`（新）、`app/schemas/pricing_rules.py`（新）
- `app/api/admin_pricing_rules.py`（新）、`app/main.py`
- `tests/backend/test_pricing_rules_api.py`、`tests/backend/test_pricing_rules_service.py`、`tests/backend/test_pricing_rules_resolve.py`（新）
- `tests/backend/test_admin_customers_api.py`、`tests/backend/test_migrations.py`、`tests/backend/test_model_columns.py`
- `docs/pricing-engine.md`（新，规则部分）、`docs/api.md`、`docs/database-schema.md`、`docs/TODO.md`

## 12. 版本变更记录

| 版本 | 日期 | 改了什么 | 触发原因 |
| --- | --- | --- | --- |
| v1 | 2026-09-29 | 初稿 | — |
| v2 | 2026-09-29 | 加 `pricing_rules` 的 BEFORE INSERT（只能插入草稿）与「草稿 → 已发布」跃迁上的 FIXED_RATE 完整性触发器（至少一个分量、涉及的计量类型分量齐全）；说明发布后完整性为何保持成立；INV-6 与 §7 补对应控制与直接写库用例 | Codex 判 v1 REQUEST_CHANGES：FIXED_RATE 完整性只在服务层校验 |
| v3 | 2026-09-29 | 加区间不重叠触发器（锁 `pricing_rule_locks` 后检查同一 `scope_key` 的非空区间相交）并规定服务层写入顺序；§7 补直接写库的重叠用例 | Codex 判 v2 REQUEST_CHANGES：已截断区间的重叠只靠服务层 |

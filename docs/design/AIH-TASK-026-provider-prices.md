# AIH-TASK-026 设计：供应商价格版本与泛化价格分量（已批准 v3）

> **来源**：设计闸门 Issue #177。本文件是 `APPROVED: design v3` 那一版正文的**逐字副本**，审查者为 Codex。
> 放进仓库，是因为 OpenClaw Worker 在沙箱里不联网、读不到 GitHub Issue。
> **实现以本文件为准**；与 Issue 不一致时，以 Issue #177 上被批准的 v3 为准。设计要改，就回到 Issue 升版本、重新过闸门，不要直接改本文件。
> 正文里的「T-B」即 [PHASE-2-plan.md](PHASE-2-plan.md) 的占位名；写「编号登记时分配」的实现任务，登记为 `AIH-TASK-026`。正文提到的其他占位名（T-A…T-L）对应的登记编号见 `docs/TODO.md` 的 Phase 2 一节。
> 与 Issue 正文的唯一差别：一处链接目标由 GitHub 网页上的 `../blob/main/…` 改成仓库内的相对路径，链接文字与其余正文逐字相同。

---

## 0. 准入判定

| 任务涉及 | 要填的范围 |
| --- | --- |
| 钱包 / 账本 / 定价 / 汇率 / 支付 / 幂等 / 状态机 | **全部章节** |
| 用量摄取 / 认证与会话（集成侧与平台侧）/ Webhook | §1–§7（**§6 不可省**） |
| 前端 / 文档 / CI / 脚本 | 不走本闸门 |
| 既不碰钱、也不碰用量摄取 / 认证与会话 / Webhook 的后端改动 | 不走本闸门 |

本任务档位：`全部`。它是供应商成本价的版本与状态机，每个计费事件的估算成本（§14）都从这里取价。

---

状态：`READY_FOR_REVIEW`
负责人：Claude
**设计版本**：`v3`（v1、v2 都已获批准。T-D 的两轮审查先后指出「分量完整性只在服务层」「已截断区间的重叠数据库挡不住」，两个缺口本设计同样存在，v2、v3 主动补上，因此重新送审，见 §12）
对应需求：spec §14、§15.1、§17、§18（手工录入与审批）、§58、§66、§74.1、§80、§89、§113；`REQ-PRICE-001`、`REQ-FIN-001`、`REQ-FIN-002`、`REQ-PRIV-002`
目标 PR：待开（批准后在 `.platform/tasks.yaml` 登记实现任务，编号登记时分配）
Phase 2 计划：`docs/design/PHASE-2-plan.md` 的 T-B；依赖 `docs/design/AIH-TASK-025-ai-catalog.md`（已批准 v4）

## 1. 目标与边界

- **要解决的问题**：spec §17 要求供应商成本价版本化、按 `occurred_at` 取、已发布不可变；§15.1 要求价格由分量表示，缺分量不许按 0 算；§74.1 给了两张表的最低字段。现在一张都没有，T-G / T-H 算不出估算成本。
- **可观察的完成标准**：
  1. 管理员能为某个（供应商, 模型）建价格草稿（原币种、来源说明、若干分量），编辑、丢弃、发布、退役；每一步有审计。
  2. 发布后，版本与它的分量在数据库层不可改（触发器），只允许「截断结束时刻」与「退役」。
  3. 同一（供应商, 模型）的已发布区间首尾相接、不重叠；一个时刻至多一个版本生效。
  4. 只读查询函数 `resolve_provider_price(session, provider_id, model_id, occurred_at)` 返回那一刻生效的版本及其分量，**与发布串行**：一个事件一旦取到某个版本，以后永远取到同一个版本。
- **明确不做什么**：
  - FX 与换算 —— T-C；价格只存原币种（ADR-0005 §5）
  - 估算成本计算与试算预览 —— T-G
  - 供应商价格自动同步、成本对账 —— Phase 8（本任务的 `source_type` 只有 `MANUAL`，给 `SYNC` 留取值）
  - 客户价（定价规则）—— T-D
  - 前端 —— T-K
- **现有行为与问题证据**：`app/models/` 没有价格相关的表；AIH-TASK-025 §2「对下游任务的契约」规定 T-B 用它的计价分量、不许给 `RETIRED` 的目录项建新版本，并要求发布方与解析串行（025 §2 T-H 契约第 ④ 条）。

## 2. 设计概要

```text
管理端：建草稿 / 改草稿 / 丢弃 → 写版本与分量 → 审计 → commit
管理端：发布 / 退役 →
    SELECT … FROM ai_providers WHERE id = ? FOR UPDATE（与 025 的改映射同一把锁）
    拿到锁之后取 t（服务端当前时间向上取整到下一个整秒）
    校验 → 截断当前末尾版本 / 写本版本的区间与状态 → 同一事务复查区间首尾相接 → 审计 → commit

计费（T-G / T-H 在计费事务里调用，本任务只提供）：
    resolve_model(...) 已对供应商行加了 FOR SHARE 并持有到计费事务提交
    resolve_provider_price(session, provider_id, model_id, occurred_at)
        加锁读（FOR SHARE）：满足 effective_from ≤ occurred_at < effective_to、状态为 PUBLISHED 或 RETIRED 的版本
        及其分量 → 没有 → None（T-G 判 PRICING_ERROR）
```

### 数据库（迁移序号取实现时的下一个，预计 0013）

**`provider_price_versions`**

| 列 | 类型 | 说明 |
| --- | --- | --- |
| `id`、`public_id` | | |
| `provider_id` / `model_id` | BIGINT | 复合外键 `(model_id, provider_id)` → `ai_models(id, provider_id)`（025 已建该唯一约束），RESTRICT |
| `source_currency` | CHAR(3) | ISO 4217 大写三字母（`^[A-Z]{3}$`）；可以是 `MYR`（此时 T-G 不需要 FX） |
| `source_type` | VARCHAR(16)，CHECK `MANUAL` / `SYNC` | 本任务只写 `MANUAL`；`SYNC` 留给 Phase 8 |
| `source_reference` | VARCHAR(255) | 必填，去首尾空白后 1–255：价格出处（例如供应商价格页与查看日期）。不写合同价（仓库公开，但数据在库里，这条是提醒录入者） |
| `status` | VARCHAR(16)，CHECK `DRAFT` / `PUBLISHED` / `RETIRED` / `DISCARDED` | 见「状态」。`DISCARDED` 是 §74.1 三个取值之外补的：丢弃的草稿不删除、留痕 |
| `effective_from` | DATETIME NULL | 草稿为空；发布时写入。`NULL` 仅用于「该（供应商, 模型）的第一个已发布版本且未指定生效时刻」= 一直以来 |
| `effective_to` | DATETIME NULL | `NULL` = 仍生效（或尚未发布） |
| `open_slot` | 生成列（STORED）：`status = 'PUBLISHED' AND effective_to IS NULL` 时为 1，否则 `NULL` | |
| `created_by` / `approved_by` | BIGINT FK → `users.id` | `approved_by` 即发布人，发布时写 |
| `created_at` / `updated_at` / `approved_at` | DATETIME | `approved_at` 即发布时刻 |

- UNIQUE `(provider_id, model_id, open_slot)`：一个（供应商, 模型）至多一个未截断的已发布版本。
- CHECK：
  - `status IN ('DRAFT','DISCARDED')` ⇒ `effective_from`、`effective_to`、`approved_by`、`approved_at` 全为空
  - `status IN ('PUBLISHED','RETIRED')` ⇒ `approved_by`、`approved_at` 非空
  - `effective_from IS NULL OR effective_to IS NULL OR effective_from < effective_to OR (status = 'RETIRED' AND effective_from = effective_to)`（最后一项是「撤销尚未生效的预约版本」留下的空区间，见下）

**`provider_price_components`**

| 列 | 类型 | 说明 |
| --- | --- | --- |
| `id` | BIGINT PK | |
| `provider_price_version_id` | FK，RESTRICT | |
| `usage_meter_component_id` | FK → `usage_meter_components.id`，RESTRICT | 025 的计价分量。§74.1 的 `meter_type_id`、`component_code`、`unit` 由它唯一确定，不在本表重复存（避免三处不一致）；API 与文档仍按 `component_code` 呈现 |
| `unit_quantity` | DECIMAL(20,8)，CHECK > 0 | 多少个计量单位对应一个 `rate_amount`（例如每 1 000 000 个 token） |
| `rate_amount` | DECIMAL(20,8)，CHECK > 0 | 原币种单价（§74.1：必须为正） |
| `metadata_json` | JSON NULL | §74.1 的字段；只作备注（例如供应商的档位名），**计价不读它**。区分价格的维度必须是不同的分量（§15.1），不能藏在这里 |
| `created_at` | DATETIME | |

- UNIQUE `(provider_price_version_id, usage_meter_component_id)`。

**不可变（触发器，照 0006 的写法与权限预检）**

- `provider_price_versions` BEFORE UPDATE：
  - `OLD.status = 'DRAFT'`：允许任意改（服务层限定可改字段）
  - `OLD.status = 'DISCARDED'`：拒绝任何改动
  - `OLD.status IN ('PUBLISHED','RETIRED')`：除 `effective_to`、`status`（只允许 `PUBLISHED → RETIRED`）、`updated_at` 外任何列变化都 SIGNAL
- `provider_price_versions` BEFORE DELETE：一律拒绝。
- `provider_price_components` BEFORE INSERT / UPDATE / DELETE：所属版本不是 `DRAFT` 就 SIGNAL（触发器里读父行状态）。
- `provider_price_versions` BEFORE INSERT：`NEW.status` 必须是 `DRAFT`（版本只能经「草稿 → 发布」进入已发布，发布跃迁上的检查因此绕不过去）。
- **发布跃迁上的完整性**（`provider_price_versions` BEFORE UPDATE，`OLD.status = 'DRAFT' AND NEW.status = 'PUBLISHED'` 时）：至少一个分量；分量涉及的每个计量类型，其在 `usage_meter_components` 里的全部分量都在本版本里；否则 SIGNAL。写法与定价规则的同一条触发器相同：
  ```sql
  IF (SELECT COUNT(*) FROM provider_price_components WHERE provider_price_version_id = NEW.id) = 0 THEN SIGNAL …; END IF;
  IF EXISTS (
    SELECT 1 FROM usage_meter_components umc
    WHERE umc.meter_type_id IN (
            SELECT m.meter_type_id FROM provider_price_components c
            JOIN usage_meter_components m ON m.id = c.usage_meter_component_id
            WHERE c.provider_price_version_id = NEW.id)
      AND umc.id NOT IN (
            SELECT usage_meter_component_id FROM provider_price_components WHERE provider_price_version_id = NEW.id)
  ) THEN SIGNAL …; END IF;
  ```
  服务层在发布前做同样的校验并给出缺失清单（409 `PRICE_VERSION_INCOMPLETE`）；触发器是绕过服务路径时的最后一道。发布后分量不可改、025 不允许给已有计量类型追加分量，所以完整性一经发布就保持成立。
- **区间不重叠由数据库兜底**（`provider_price_versions` BEFORE UPDATE，`NEW.status IN ('PUBLISHED','RETIRED')` 且 `effective_from`、`effective_to` 或 `status` 有变化时）：触发器先执行 `SELECT id INTO @lock_id FROM ai_providers WHERE id = NEW.provider_id FOR UPDATE`（与服务层发布同一把锁，把绕过服务的直接写入也串行起来），再检查同一（供应商, 模型）内是否存在另一行满足：状态为 `PUBLISHED` / `RETIRED`、区间非空（`effective_from IS NULL OR effective_to IS NULL OR effective_from < effective_to`）、且与本行区间相交 —— 相交判定 `COALESCE(o.effective_from, '1000-01-01') < COALESCE(NEW.effective_to, '9999-12-31') AND COALESCE(NEW.effective_from, '1000-01-01') < COALESCE(o.effective_to, '9999-12-31')`，另一行条件 `o.provider_id = NEW.provider_id AND o.model_id = NEW.model_id AND o.id <> NEW.id`；存在即 SIGNAL。空区间（撤销的预约）不参与判定。
- **服务层的写入顺序**（保证正常路径过得了上一条触发器）：发布时先截断前一个（`effective_to` 写入），再把本行从草稿改为已发布；撤销预约时先把被撤销的一行改成空区间，再把前一个恢复为未截断。

### 状态

| 当前 | 操作 | 前置条件 | 新状态 | 副作用 |
| --- | --- | --- | --- | --- |
| — | 建草稿 | 供应商、模型、分量所属计量类型都不是 `RETIRED`（025 契约） | `DRAFT` | 审计 |
| `DRAFT` | 编辑 | 同上 | `DRAFT` | 分量整体替换；审计 |
| `DRAFT` | 丢弃 | — | `DISCARDED` | 审计 |
| `DRAFT` | 发布 | 完整性校验通过（见下）；同上的目录状态 | `PUBLISHED` | 写区间、截断前一个版本；审计 `PROVIDER_PRICE_PUBLISH` |
| `PUBLISHED`（当前生效或已被截断） | 退役 | 见「退役」 | `RETIRED` | 审计 |
| `PUBLISHED`（被后继截断） | 后继发布 | — | 不变（仍 `PUBLISHED`），`effective_to` 被写入 | 在后继的审计里 |
| `RETIRED` / `DISCARDED` | 任何 | — | — | 409 |

**完整性（发布时校验，服务层 + 发布跃迁触发器）**：版本至少包含一个分量；**凡是出现的计量类型，它在 025 里的全部分量都必须出现**（例如出现 `LLM_TOKEN` 就必须有输入、输出、缓存写入、缓存读取四个价）。计量类型整体没出现的，发布后该模型那一类用量在计价时判 `PRICING_ERROR`（T-G）—— 缺价不按 0 算（§15.1）。

### 发布：区间怎么定（与 025 的别名同一套推理）

发布请求可带可选的 `effective_from`（未来时刻，§17 的例子是按日期生效的版本）。拿到供应商行锁之后取 `t`（当前时间向上取整到下一个整秒），设该（供应商, 模型）已发布（`PUBLISHED` / `RETIRED`）版本中 `effective_from` 最晚的一个为「末尾版本」 L：

| 情形 | 本版本的 `effective_from` | 对 L 做什么 |
| --- | --- | --- |
| 从没有已发布版本，请求未给 `effective_from` | `NULL`（一直以来） | — |
| 从没有已发布版本，请求给了 F | F（必须 ≥ `t`） | — |
| 有 L，请求未给 | `t` | 若 L 未截断：`effective_to = t` |
| 有 L，请求给了 F | F（必须 ≥ `t` 且 > L 的 `effective_from`） | 若 L 未截断：`effective_to = F` |
| 有 L，且 L 已被截断或已退役（区间有尽头 E） | `max(E, 所请求或 t)`；若请求 F < E → 409 | — |

- **不许回溯**：`effective_from` 永远 ≥ `t`（第一个版本未指定时的 `NULL` 除外）。请求给的 F < `t` → 422 `EFFECTIVE_FROM_IN_PAST`。
- **只在末尾追加**：不能在已发布的时间线中间插入版本。
- **第一个版本对过去生效是安全的**：在它之前，这个（供应商, 模型）的所有事件一律 `PRICING_ERROR`、从未扣过钱（T-H 契约），补上价格只会把「无价」变成「有价」。
- **退役后的空档不回填**（与 025 别名不同）：退役表示管理员明确宣布「从此刻起不按这个价算」，空档里的事件保持 `PRICING_ERROR`，由管理员另行决定（发布新版本，从 E 或更晚起算；或 Phase 8 reprocess）。回填会让退役失去意义。

**为什么与计费不会交错**：发布在供应商行排他锁内、`t` 在拿到锁之后取；计费在同一行共享锁内、只处理 `occurred_at ≤` 持锁后当前时间的事件（025 §2「时间」的同一证明）。于是：计费先 → 事件的 `occurred_at` < `t` ≤ 本版本及被截断处的任何边界，新旧表对它给出同一个版本；发布先 → 计费读到的已是新表。**已取到某个版本的（模型, 时刻）永远取到同一个版本。**

**事务内复查**：每次发布 / 退役写完后，在同一事务里按 `effective_from` 排序读取该（供应商, 模型）全部 `PUBLISHED` / `RETIRED` 版本，断言：只有第一个的 `effective_from` 可为空；相邻两个 `前.effective_to` 为空时它必须是最后一个，非空时 `≤ 后.effective_from`（等号 = 首尾相接，小于 = 退役留下的空档）；不满足就回滚（防实现错误）。

### 退役

| 被退役的版本 | 条件 | 做什么 |
| --- | --- | --- |
| 当前末尾、已开始生效（`effective_from` 为空或 ≤ `t`）、未截断 | — | `effective_to = t`，`RETIRED`：`t` 起发生的事件 `PRICING_ERROR` |
| 当前末尾、尚未开始（预约的，`effective_from` > `t`） | — | `effective_to = effective_from`（空区间，永不匹配），`RETIRED`；若前一个版本 P 的 `effective_to` 等于本版本的 `effective_from`（被本版本截断的），把 P 的 `effective_to` 恢复为 `NULL` |
| 已被后继截断的（历史版本） | — | 409 `PRICE_VERSION_NOT_RETIRABLE`：它的区间已经结束，退役没有意义；要纠正历史价格走 Phase 8 reprocess |

恢复 P 的 `effective_to` 只影响 `occurred_at ≥ 本版本 effective_from > t` 的时刻，按上面的证明，这些时刻还没有任何事件被计费，所以安全。触发器允许已发布行改 `effective_to`（见上），服务层只在这两种退役里改它。

### 接口

全部只有 ADMIN，处理函数第一条语句 `require_admin`，加进 `EXPECTED_ADMIN_ROUTES` 与 `VALID_BODIES`；路径里的 id 是 `public_id`；分页照 §108。金额字段一律 JSON 字符串、正则解析、不经过 `float`，超过 8 位小数 422、不静默舍入（照 `wallet_adjustments` 的写法）。

| 方法与路径 | 作用 |
| --- | --- |
| `GET /api/v1/admin/provider-prices` | 列表，可按 `provider_id`、`model_id`、`status` 筛；按供应商、模型、`effective_from` 排序 |
| `POST /api/v1/admin/provider-prices` | 建草稿：`provider_id`、`model_id`、`source_currency`、`source_reference`、`components[]`（`component_code`、`unit_quantity`、`rate_amount`、可选 `metadata`） |
| `GET /api/v1/admin/provider-prices/{price_version_id}` | 详情（含分量、区间、发布人） |
| `PATCH /api/v1/admin/provider-prices/{price_version_id}` | 只对草稿：`source_currency`、`source_reference`、`components[]`（整体替换）；非草稿 409 |
| `POST /api/v1/admin/provider-prices/{price_version_id}/publish` | 发布：可选 `effective_from`（RFC 3339，必须带时区） |
| `POST /api/v1/admin/provider-prices/{price_version_id}/retire` | 退役：必填 `reason` |
| `POST /api/v1/admin/provider-prices/{price_version_id}/discard` | 丢弃草稿 |

- `components[]` 里的 `component_code` 必须是 025 的计价分量；同一版本不重复。
- 已发布再发布：200，不写（幂等）。已丢弃 / 已退役再做任何操作：409。

错误：

| HTTP | `error.code` | 什么时候 |
| --- | --- | --- |
| 404 | `PRICE_VERSION_NOT_FOUND` / `AI_PROVIDER_NOT_FOUND` / `AI_MODEL_NOT_FOUND` / `USAGE_METER_COMPONENT_NOT_FOUND` | 不存在或模型不属于该供应商 |
| 409 | `PRICE_VERSION_NOT_DRAFT` | 编辑 / 丢弃非草稿 |
| 409 | `PRICE_VERSION_INCOMPLETE` | 发布时完整性不满足（列出缺的分量） |
| 409 | `CATALOG_ITEM_RETIRED` | 供应商、模型或分量所属计量类型已停用 |
| 409 | `PRICE_VERSION_NOT_RETIRABLE` | 退役历史版本 |
| 409 | `PRICE_VERSION_FINAL` | 对已退役 / 已丢弃版本操作 |
| 409 | `EFFECTIVE_FROM_CONFLICT` | 请求的生效时刻早于末尾版本的起点或尽头 |
| 422 | `EFFECTIVE_FROM_IN_PAST` | 请求的生效时刻早于 `t` |
| 422 | `VALIDATION_ERROR` | 格式、精度、正数 |

### 事务边界

每个写接口一个 `session_scope`。发布与退役：锁供应商行 → 取 `t` → 读并校验 → 写本版本与（需要时）前一个版本 → 复查 → 审计 → 提交。建草稿、改草稿、丢弃不锁供应商行（草稿不参与计费）。

锁顺序：只锁供应商行；计费路径的锁顺序（供应商 → 钱包 → 租户，025）不变，不形成环。

### 外部系统与异步边界

无。不写 outbox。

### 时间语义

- 区间是 UTC 无时区、半开 `[effective_from, effective_to)`，与 `occurred_at` 比较。
- `t`、`created_at`、`updated_at`、`approved_at` 取服务端时间；`t` 向上取整到下一个整秒，其余截到整秒。
- 请求给的 `effective_from` 换算成 UTC 后必须是整秒（有小数秒 → 422），免得边界落在秒内。

### 金额精度

- `unit_quantity`、`rate_amount`：DECIMAL(20,8)，正数，按十进制字符串精确解析，超出 8 位小数拒绝。
- 本任务不做任何乘除；§80 的高精度中间计算与一次舍入归 T-G。
- 价格是原币种（通常 USD），不换算、不存 MYR（ADR-0005 §5）。

### 审计

| `action` | 什么时候 | 内容 |
| --- | --- | --- |
| `PROVIDER_PRICE_CREATE` | 建草稿 | 后：供应商 / 模型 `code`、币种、来源、分量（`component_code`、`unit_quantity`、`rate_amount`） |
| `PROVIDER_PRICE_UPDATE` | 改草稿 | 前后：变化的字段与分量 |
| `PROVIDER_PRICE_DISCARD` | 丢弃 | 前后状态 |
| `PROVIDER_PRICE_PUBLISH` | 发布（spec §66） | 后：区间、发布人；前：被截断的前一个版本的 `public_id` 与原 `effective_to` |
| `PROVIDER_PRICE_RETIRE` | 退役 | 前后：状态与区间；被恢复的前一个版本（若有）；`reason` |

`entity_type = provider_price_version`、`entity_id = public_id`。除 `PROVIDER_PRICE_PUBLISH` 外四个不在 §66 清单里，按先例补上并记进 `docs/TODO.md`。

**价格不是秘密但也不对客户可见**：审计与管理端接口含成本价，只在管理端（INV-7 / REQ-PRIV-002）。

### 对下游任务的契约

| 下游 | 契约 |
| --- | --- |
| T-G | 用 `resolve_provider_price` 取版本与分量；分量按 025 的 `quantity_field` 取数；事件需要的分量在版本里缺失 → `PRICING_ERROR`，不按 0；`source_currency = MYR` 时不查 FX |
| T-H | 在同一个计费事务里，先 `resolve_model`（它加供应商行共享锁），再 `resolve_provider_price`；两者都用加锁读；把 `provider_price_version_id` 写进事件快照 |
| T-K | 列表与详情接口即本任务的接口；界面标明「成本价，原币种，客户不可见」 |

## 3. 不变量影响矩阵

| 不变量 | 是否触碰 | 可能怎样被破坏 | 设计控制 | 验证方式 |
| --- | --- | --- | --- | --- |
| INV-1 中心故障不中断 AI | 否 | 不在 AI 路径 | — | — |
| INV-2 事件不重复扣费 | 否（间接） | 价格变化触发重复计费 | 本任务不提供任何重算入口 | — |
| INV-3 | 否 | — | — | — |
| INV-4 余额只经账本变动 | 否 | 不碰钱包 | — | — |
| INV-5 历史账本不可变 | 否 | — | — | — |
| INV-6 事件保留版本引用 | **是** | ① 已被事件引用的版本或分量被改；② 发布与计费交错，同一时刻前后取到不同版本；③ 回溯发布改变已计费时刻的价格；④ 不完整的版本被发布（绕过服务），该模型的相关事件全部 `PRICING_ERROR` | ① 触发器：发布后只许截断与退役，分量只在草稿阶段可改，禁止删除；② 供应商行锁串行、`t` 在锁后取、加锁读；③ 生效时刻 ≥ `t`，只在末尾追加，第一个版本例外（只把无价变有价）；④ 发布跃迁触发器 + 只能以草稿插入 | 触发器用例（MySQL）；「发布与计费并发」「快照读」用例；性质用例；完整性直接写库用例（见 §7） |
| INV-7 客户不可见成本毛利 | **是** | 成本价经客户接口泄露 | 只有 `/api/v1/admin` 路由，`require_admin` | 越权用例 |
| INV-8 租户不可互访 | 否 | 价格是全局数据 | — | — |
| INV-9 | 否 | — | — | — |
| INV-10 金额用 Decimal | **是** | 单价经 `float` 失真或被静默舍入 | 字符串精确解析，DECIMAL(20,8)，超精度拒绝 | 精度边界用例 |
| INV-11 | 否（间接） | 同 INV-2 | — | — |
| INV-12 | 否 | — | — | — |
| INV-13 状态与事件原子提交 | **是** | 版本发布了而审计没写；前一个版本截断了而本版本没发布 | 同一事务 | 审计 / 复查 / 提交抛错的回滚用例（SQLite 与 MySQL 各一次） |
| INV-14 | 否 | 无异步 | — | — |

**本任务自己的不变量**：同一（供应商, 模型）在任一时刻至多一个版本生效；一个（模型, 时刻）一旦取到某个版本，以后永远取到同一个版本（除非那一刻原本无价）。控制与验证同 INV-6。

## 4. 状态与并发

状态表见 §2「状态」。

- **串行化**：发布、退役在供应商行排他锁内；计费取价在同一行共享锁内（由 `resolve_model` 加、持有到计费事务提交）。草稿操作不锁。
- **数据库保证的唯一性**：`(provider_id, model_id, open_slot)`；`(provider_price_version_id, usage_meter_component_id)`；`public_id`。
- **幂等键**：无客户端幂等键。发布已发布的版本 → 200 不写；建草稿重发会建出两个草稿（草稿不影响计费，管理员丢弃多余的即可）。
- **相同 ID、不同载荷**：不适用。
- **重试层**：服务端不重试。
- **原子提交**：版本、前一个版本的截断或恢复、复查、审计。

## 5. 失败与恢复矩阵

| 失败点 | 对外结果 | 数据最终状态 | 是否可重试 | 恢复来源 | 告警 |
| --- | --- | --- | --- | --- | --- |
| 校验失败 | 422 | 无改动 | 改后可 | — | 无 |
| 不完整 / 目录已停用 / 状态不对 / 时刻冲突 | 409 | 无改动 | 改后可 | — | 无 |
| 复查不通过（实现错误） | 500 | 回滚 | 否 | 修代码 | 错误日志 |
| 审计或提交失败 | 500 | 回滚 | 是 | — | 错误日志 |
| 锁等待超时（与计费争供应商行） | 500 | 回滚 | 是 | — | 错误日志 |
| 提交成功、响应丢失 | 超时 | 已发布 | 重发发布 → 200 不写 | — | 无 |
| 发布了错误的价格 | — | 已被计费引用则不可改 | 发布新版本（从 `t` 起）；历史纠正走 Phase 8 reprocess | 审计 | 无 |
| 触发器被绕过的直接 SQL | 数据库拒绝 | 不变 | — | — | — |

## 6. 数据与安全边界

- **租户过滤**：不适用（全局数据）。
- **鉴权主体**：只有 ADMIN。没有客户或集成侧接口。
- **禁止返回的字段**：内部自增 id。成本价只在管理端出现。
- **日志 / 审计 / 异常**：审计含价格（管理员可见的业务数据，不是秘密）；应用日志不打印分量明细。
- **密钥**：不涉及。
- **prompt / response 或客户数据**：不涉及。
- **保留与删除**：不删除任何行（触发器保证）。
- **公开仓库提醒**：测试与文档里的价格一律用明显的虚构值，不写真实供应商合同价。

## 7. 测试证据计划

| 风险/需求 | 测试层级 | 场景 | 预期结果 |
| --- | --- | --- | --- |
| 正常路径 | integration（API） | 建草稿 → 改 → 发布 → 再建草稿 → 发布（截断前一个）→ 退役 | 各步状态、区间、审计正确 |
| 第一个版本 | integration | 无已发布版本时发布、不给生效时刻 | `effective_from` 为空；很久以前的时刻取到它 |
| 预约生效 | integration | 给未来 F 发布 | 前一个版本截断于 F；F 之前取旧、之后取新 |
| 不许回溯 | integration | 给 F < `t`；给 F ≤ 末尾版本起点 | 422 / 409 |
| 撤销预约 | integration | 预约版本在生效前退役 | 空区间；前一个版本 `effective_to` 恢复为空；任何时刻都取不到被撤销的版本 |
| 退役当前版本 | integration | 退役正在生效的版本 | `t` 起取不到价（None）；`t` 之前照旧 |
| 退役历史版本 | integration | 退役已被截断的版本 | 409 |
| 退役后再发布 | integration | 退役于 E，之后发布新版本 | 新版本从 `max(E, t)` 起；空档保持无价 |
| 完整性 | integration | 只给 `LLM_TOKEN` 的三个分量；给零个分量；重复分量；不存在的分量 | 409 / 422 / 404 |
| 目录停用 | integration | 供应商 / 模型 / 计量类型停用后建草稿或发布 | 409 |
| 精度 | integration | `rate_amount` 8 位 / 9 位小数；0；负数；指数写法；JSON 数字而非字符串 | 通过 / 422 |
| 触发器（MySQL） | integration | 直接 UPDATE 已发布版本的币种 / 分量单价 / `effective_from`；DELETE 任何版本；给已发布版本 INSERT 分量；把 `RETIRED` 改回 `PUBLISHED` | 全部 SIGNAL 拒绝；只有 `effective_to` 与 `PUBLISHED → RETIRED` 通过 |
| 完整性由数据库兜底（MySQL） | integration | 绕过服务：直接 `UPDATE … SET status = 'PUBLISHED'`（零个分量；`LLM_TOKEN` 缺一个分量）；直接 `INSERT` 一条 `status = 'PUBLISHED'` 的版本 | 触发器拒绝；完整的版本直接改状态则通过 |
| 区间不重叠（MySQL） | integration | 绕过服务直接写：同一（供应商, 模型）两个**已截断**且相交的版本（部分相交、包含、起点相同）；首尾相接；空区间；不同模型相交 | 相交的被拒绝；其余通过；服务层正常的发布 / 退役 / 撤销预约路径通过 |
| 数据库约束（MySQL） | integration | 同一（供应商, 模型）两个未截断的已发布版本；草稿带区间 | 唯一约束 / CHECK 拒绝 |
| 发布与计费并发 | integration（真 MySQL，两个连接） | ① 发布事务写完未提交时，计费事务 `resolve_model` + `resolve_provider_price`（`occurred_at ≥ t`）② 反过来 | ① 计费阻塞到发布提交，取到新版本 ② 发布阻塞到计费提交，`t` 晚于该事件；提交后再取同一时刻结果一致 |
| 快照读陷阱 | integration（真 MySQL） | 计费事务先做一次普通读，另一连接发布并提交，计费事务再取价 | 取到新版本（加锁读） |
| 时间稳定性（性质用例） | unit | 随机的「发布 / 预约 / 退役 / 撤销预约」序列，每步后对早于该步 `t` 的一组固定时刻取价 | 一旦取到版本 V，之后永远是 V |
| 事务中途失败 | integration（SQLite 与 MySQL 各一次） | 发布时截断写入后审计抛错 | 两个版本都回滚到发布前 |
| 鉴权 | integration | 匿名 401、CUSTOMER 403；首条语句 `require_admin`（AST）；路由枚举 | 同左 |
| 迁移 | integration（MySQL） | upgrade / downgrade；权限预检 | 表、约束、触发器建删正确 |

## 8. 迁移与上线

- **数据迁移步骤**：建两表、生成列、约束、触发器（先做 0006 同款的 binlog / 触发器权限预检）。序号取实现时的下一个。
- **锁表与性能影响**：新表。
- **兼容窗口**：旧代码不读新表。
- **部署顺序**：合并即自动部署，先迁移后代码。
- **回滚或前滚**：downgrade 删触发器、删表。生产上录了价格之后回滚会丢价格 —— 前滚修复。
- **部分部署时的行为**：迁移成功、代码未更新：新表空置。代码更新、迁移失败：容器起不来，旧容器继续服务。
- **生产上的初始数据**：本任务不预置价格；上线后由管理员录入（试点要的是 Anthropic 与 OpenAI 转写的价格）。
- **监控指标和告警负责人**：无新增；「缺价」在 T-H 以 `PRICING_ERROR` 告警。

## 9. 方案取舍

| 方案 | 优点 | 风险 | 未采用原因 |
| --- | --- | --- | --- |
| **采用**：只在末尾追加的版本序列 + 供应商行锁串行 + 触发器保证不可变 | 与 025 同一套证明与同一把锁，计费路径不加新锁；历史取价永不改变 | 发布与计费争同一把锁（发布低频） | — |
| 分量存 `meter_type_id` + `component_code` + `unit`（§74.1 字面） | 字面贴合 | 与 025 的分量表三处重复，可能不一致 | 引用 025 的分量行，三者由它唯一确定；§74 允许按仓库约定命名，语义可追溯 |
| 允许在时间线中间插入 / 修改历史区间 | 录错了能直接改 | 已计费事件的价格变化，违反 §17「历史成本不因当前价格变化而改变」 | 纠正历史走 Phase 8 reprocess |
| 退役后自动回填空档（像 025 的别名） | 空档事件能被计价 | 退役失去意义 —— 管理员退役正是为了「此后不按这个价算」 | 不回填 |
| 不支持预约，发布即生效 | 更简单 | §17 的例子与 §58「Effective date」都指向按日期生效；供应商调价常提前公布 | 支持预约，撤销预约的规则见上 |
| 丢弃草稿直接删除 | 少一个状态 | 草稿编辑有审计引用；删除行与「从不删除」的约定不一致 | `DISCARDED` |
| 发布需要另一位管理员批准 | 四眼原则 | 目前只有一位管理员，做不到 | `approved_by` 记发布人；以后有多位管理员时另开任务加「发布人 ≠ 建草稿人」 |
| 分量允许 0 价 | 能表达免费分量 | §74.1 明写必须为正 | 保持正数；若将来遇到真正免费的分量，另写偏离 ADR |

## 10. 未决问题与假设

- **未决问题**：无阻断项。
- **需要谁拍板**：无（§9 的取舍都在 spec 与已批准设计的范围内）。
- **尚未验证的假设**：
  1. MySQL 8.4 的 BEFORE UPDATE 触发器能比较 NEW / OLD 的 JSON 列（`metadata_json` 在已发布后不可改）—— 触发器用 `NOT (NEW.metadata_json <=> OLD.metadata_json)`；迁移测试在 CI 的 MySQL 上实跑。
  2. 触发器里读父行状态不需要额外锁：分量写入都在同一事务里、父行状态只在发布事务中改变，发布事务已锁供应商行；并发的草稿编辑与发布同一版本 —— 发布事务 `SELECT … FOR UPDATE` 版本行，草稿编辑也对版本行 `FOR UPDATE`，两者串行。
- **如果假设错误**：1 → 改为比较 `JSON_EXTRACT` 的规范化文本；2 → 在触发器里对父行 `FOR UPDATE`，不改设计语义。

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

- `alembic/versions/<日期>_<序号>_provider_prices.py`、`alembic/env.py`
- `app/models/provider_prices.py`（新）、`app/models/auth.py`（审计动作）
- `app/repositories/provider_prices.py`（新，含 `resolve_provider_price`）
- `app/services/provider_prices.py`（新）
- `app/schemas/provider_prices.py`（新）
- `app/api/admin_provider_prices.py`（新）、`app/main.py`
- `tests/backend/test_provider_prices_api.py`、`tests/backend/test_provider_prices_service.py`、`tests/backend/test_provider_prices_resolve.py`（新）
- `tests/backend/test_admin_customers_api.py`（路由枚举）、`tests/backend/test_migrations.py`、`tests/backend/test_model_columns.py`
- `docs/api.md`、`docs/database-schema.md`、`docs/TODO.md`

## 12. 版本变更记录

| 版本 | 日期 | 改了什么 | 触发原因 |
| --- | --- | --- | --- |
| v1 | 2026-09-29 | 初稿 | — |
| v2 | 2026-09-29 | 加 `provider_price_versions` 的 BEFORE INSERT（只能插入草稿）与「草稿 → 已发布」跃迁上的分量完整性触发器；INV-6 与 §7 补对应控制与直接写库用例。v1 的其余内容不变 | 主动修正：T-D 审查（#178）指出的同类缺口在本设计同样存在 |
| v3 | 2026-09-29 | 加区间不重叠触发器（锁供应商行后检查同一（供应商, 模型）的非空区间相交）并规定服务层写入顺序；§7 补直接写库的重叠用例 | 主动修正：T-D 第二轮审查（#178）指出的同类缺口 |

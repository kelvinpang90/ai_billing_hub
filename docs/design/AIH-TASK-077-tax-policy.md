# AIH-TASK-077 / 078 / 079 设计：税务政策版本表与首条 PUBLISHED 税务政策（已批准 v2）

> **来源**：设计闸门 Issue #332。本文件是 `APPROVED: design v2` 那一版正文的**逐字副本**，审查者为 Codex。
> 放进仓库，是因为 OpenClaw Worker 在沙箱里不联网、读不到 GitHub Issue。
> **实现以本文件为准**；与 Issue 不一致时，以 Issue #332 上被批准的 v2 为准。设计要改，就回到 Issue 升版本、重新过闸门，不要直接改本文件。
> §11 的三个实现任务登记为 `AIH-TASK-077`（数据库迁移与模型）、`AIH-TASK-078`（仓储与服务）、`AIH-TASK-079`（运维 CLI 与文档）。

---

## 0. 准入判定

| 任务涉及 | 要填的范围 |
| --- | --- |
| 钱包 / 账本 / 定价 / 汇率 / 支付 / 幂等 / 状态机 | **全部章节** |
| 用量摄取 / 认证与会话（集成侧与平台侧）/ Webhook | §1–§7（**§6 不可省**） |
| 前端 / 文档 / CI / 脚本 | 不走本闸门 |
| 既不碰钱、也不碰用量摄取 / 认证与会话 / Webhook 的后端改动 | 不走本闸门 |

本任务档位：`全部`。它建 spec §74.9 的 `tax_policy_versions`（一张带状态机、发布后不可变的版本表），并写入第一条 `PUBLISHED`；以后的支付（§74.7 `payments.tax_policy_version_id`）、收据（§45）与对账单都快照它。本任务不动钱，但它是 Phase 4 全部财务凭证的税务口径来源，且建表之后发布的行永远不可改、迁移不可回滚，所以按全部章节填。

---

状态：`待审`（`READY_FOR_REVIEW`）
负责人：Claude
**设计版本**：`v2`
对应需求：spec §45（收据须带 tax-policy 快照字段）、§45.1（税务与合规闸门）、§74.7（`payments.tax_policy_version_id` / `tax_amount`）、§74.9（`tax_policy_versions`，含「No tax policy may be published until the §45.1 decision gate is approved」）、§132 第 13 条；`REQ-TAX-001`；ADR-0008（第 5 节与收口条件，2026-10-17 由决策人补完，PR #328）
目标 PR：待开（批准后按 §11 登记；`docs/TODO.md`「待登记」里的「Phase 4 第一个任务：`tax_policy_versions` 建表并写入第一条 `PUBLISHED`」即本闸门）

## 1. 目标与边界

- **要解决的问题**：ADR-0008 收口条件的第 2 步是「`tax_policy_versions` 写入第一条 `PUBLISHED`」，第 3 步（解除 §45.1 对生产充值 / 收据 / 对账单 schema 的阻塞）以它为前提。spec §45.1 的六项已由决策人全部拍板（ADR-0008 第 5 节：未注册 SST、也不需要注册；价格是最终价；不拆税额、不设税率；收据与对账单不出现税额栏，印一句「最终价、不另收税费」的说明）。但这张表还不在代码里，写不进去 —— ADR-0008 原文「第 2 步还没做：`tax_policy_versions` 表还不在代码里」。所以 Phase 4 至今不能开工。
- **可观察的完成标准**：
  1. 迁移 `0022_tax_policy` 之后，生产库有 `tax_policy_locks`（恰好一行 `id = 1`）与 `tax_policy_versions`（空表），状态机与「发布后不可变」由 MySQL 触发器兜底。
  2. 运维在生产 `api` 容器里执行一次 `python -m app.cli publish-initial-tax-policy --approved-by-email <管理员邮箱>`：同一事务写入一条 `PUBLISHED` 行（内容见 §2「首条政策的内容」）与 `TAX_POLICY_CREATE`、`TAX_POLICY_PUBLISH` 两条审计，标准输出打印新行的 `public_id`。
  3. 再执行一次：退出码 1、什么都不写（表里已有任何一行就拒绝），所以可以放心重试。
  4. `resolve_tax_policy(session, "MY", "SST", at)` 对 `at ≥ 2026-10-16T16:00:00`（UTC，= 吉隆坡 2026-10-17 00:00）返回这条政策，之前返回 `None`。
  5. **Phase 4 闸门**：这条政策在**生产**上 `PUBLISHED` 之后（不是代码合并之后），Phase 4 其余任务（充值、收据、对账单、支付）才可以开工（ADR-0008 收口条件第 3 步）。
- **明确不做什么**：
  - 管理端接口与管理端页面 —— 等 Acuven 注册 SST、需要第二条政策时另立任务、另过闸门（决策人 2026-10-17 定）
  - 退役（`PUBLISHED → RETIRED`）、截断、预约、撤销预约的**服务函数** —— 没有写入方。`RETIRED` 与相应的触发器规则留在表结构里（spec §74.9 列了它），不写函数
  - `DISCARDED` 状态 —— spec §74.9 没有，也没有写入方（理由见 §9）
  - 迁移里预置政策 —— 不种子（理由见 §9）
  - 任何税额计算：ADR-0008 §2 的反算公式在未注册期间不执行；`payments.tax_amount` 恒为空是 Phase 4 支付设计的事
  - 收据 / 对账单如何快照本政策 —— 归 Phase 4 各自的闸门（本设计只给出 §2「对下游任务的契约」）
  - 计费路径（用量事件）—— 用量计费不读税务政策：价格是最终价，§80 的金额就是客户应付
- **现有行为与问题证据**：`app/models/` 下没有任何税务模型；`alembic/versions/` 的最新修订是 `0021_ai_module`（`alembic/versions/20261013_0021_ai_module.py:50`）；ADR-0008「收口条件」2026-10-17 进度写明第 2、3 步未完成、「在此之前，Phase 4 不得开工」照旧成立；`docs/TODO.md` 第 61 行把本任务列为 Phase 4 第一个任务、需设计闸门。

## 2. 设计概要

```text
运维（077、078、079 都合并并自动部署之后，在生产 api 容器里执行一次）：
  python -m app.cli publish-initial-tax-policy --approved-by-email <管理员邮箱>
    → normalise_email（app/core/emails.py:28；格式不对 → 退出码 2，不连库）
    → services.tax_policy.publish_initial_tax_policy(session_factory, approver_email=…)
       事务外：parse_treatment(INITIAL_TREATMENT) + 校验 rate 与 treatment 一致（失败是代码缺陷，抛错，不连库）
       一个事务（session_scope）：
        ① SELECT … FROM tax_policy_locks WHERE id = 1 FOR UPDATE        ← 第一条语句
        ② SELECT 1 FROM tax_policy_versions LIMIT 1 FOR SHARE             ← 有任何一行 → TaxPolicyAlreadyExists
        ③ SELECT … FROM users WHERE email = ? FOR SHARE                   ← 不存在 / 非 ADMIN / 非 ACTIVE → TaxPolicyApproverInvalid
        ④ now = clock()（锁后取），stamp = 截到整秒
        ⑤ INSERT 草稿（DRAFT，created_by = 审批人）→ flush
        ⑥ 审计 TAX_POLICY_CREATE
        ⑦ 草稿 → PUBLISHED [2026-10-16T16:00:00, NULL)，approved_by / approved_at → flush
           （MySQL：BEFORE UPDATE 触发器在这里查跃迁、尽头为空，并锁 tax_policy_locks 查区间不重叠）
        ⑧ 事务内复查：(MY, SST) 的 PUBLISHED / RETIRED 行恰好一条，且就是本行
        ⑨ 审计 TAX_POLICY_PUBLISH
        ⑩ commit
    → 标准输出一行 public_id，退出码 0

Phase 4 消费方（以后各自过闸门；本设计只提供只读函数）：
  resolve_tax_policy(session, "MY", "SST", at) → ResolvedTaxPolicy | None（加锁读 FOR SHARE）
```

没有 HTTP 接口、没有异步任务、没有出站调用。

### 数据库（迁移 `alembic/versions/20261017_0022_tax_policy.py`，`revision = "0022_tax_policy"`，`down_revision = "0021_ai_module"`）

写法全部照汇率的三张表：模型 `app/models/fx_rates.py`、迁移 `alembic/versions/20260929_0015_fx_rates.py`、锁表拒绝 UPDATE 的 `alembic/versions/20260930_0016_lock_tables_no_update.py`。

**`tax_policy_locks`**：单行锁表，只有 `id INT PRIMARY KEY`（`autoincrement=False`）。迁移在建触发器之前写入 `id = 1`（照 0015:336）；模型用 `event.listen(…, "after_create", DDL("INSERT …"))` 让 SQLite 的 `create_all` 同样写入这一行（照 `app/models/fx_rates.py:181`）。三个触发器：BEFORE INSERT（已有一行时拒绝，照 0015:174）、BEFORE UPDATE（无条件拒绝，照 0016:87）、BEFORE DELETE（拒绝，照 0015:185）。DML 范围内锁表恒为 `{1}`。

**`tax_policy_versions`**（spec §74.9）

| 列 | 类型 | 说明 |
| --- | --- | --- |
| `id` | BIGINT PK，SQLite 上 INTEGER（`BigInteger().with_variant(Integer, "sqlite")`，照 `app/models/fx_rates.py:53`） | 内部 id；Phase 4 的 `payments.tax_policy_version_id` 引用它，不对外 |
| `public_id` | CHAR(36) NOT NULL，UNIQUE `uq_tax_policy_versions_public_id` | 仓储层 `str(uuid.uuid4())` 生成（照 `app/repositories/fx_rates.py:60`） |
| `jurisdiction` | VARCHAR(16) NOT NULL，`Enum(TaxJurisdiction, native_enum=False, length=16)`，CHECK `jurisdiction IN ('MY')` | `TaxJurisdiction(StrEnum)` 只有 `MY`：没有写入方的取值不预先加（`app/models/auth.py:119` 的同一条规矩） |
| `policy_type` | VARCHAR(16) NOT NULL，`Enum(TaxPolicyType, …, length=16)`，CHECK `policy_type IN ('SST')` | 只有 `SST`，理由同上 |
| `rate` | DECIMAL(9,6) NULL，`Numeric(9, 6, asdecimal=True)`，CHECK `rate IS NULL OR (rate >= 0 AND rate < 1)` | 小数表示（0.08 = 8%）。首条政策为 `NULL`（未注册，不适用）。精度理由见下 |
| `treatment_json` | JSON NOT NULL | 形状在应用层校验（见「`treatment_json` 的形状」）；`JSON` 类型照 `app/models/provider_prices.py:201` 的 `metadata_json` |
| `status` | VARCHAR(16) NOT NULL，`Enum(TaxPolicyStatus, native_enum=False, length=16)`，CHECK `status IN ('DRAFT', 'PUBLISHED', 'RETIRED')` | spec §74.9 的三个值，**不加 `DISCARDED`**（§9） |
| `effective_from` / `effective_to` | DATETIME NULL | 半开区间 `[from, to)`，不带时区的 UTC；草稿两者为空 |
| `open_slot` | INTEGER，生成列 STORED：`CASE WHEN status = 'PUBLISHED' AND effective_to IS NULL THEN 1 END` | 与 0015:167 逐字相同；未截断的已发布版本为 1，其余一律为 `NULL`（不是 0） |
| `source_reference` | VARCHAR(255) NOT NULL | 政策出处，纯文本，不写链接 |
| `created_by` | BIGINT NOT NULL，FK → `users.id` ON DELETE RESTRICT（`fk_tax_policy_versions_created_by`） | 与汇率不同，这里**非空**：政策只能由人建，没有系统写入方 |
| `approved_by` | BIGINT NULL，FK → `users.id` ON DELETE RESTRICT（`fk_tax_policy_versions_approved_by`） | 发布人 |
| `approved_at` | DATETIME NULL | 发布时刻 |
| `created_at` / `updated_at` | DATETIME NOT NULL | spec §74.9 只列了 `created_at`；`updated_at` 是仓库惯例（三张版本表都有），发布与以后的截断 / 退役要写它 |

- **唯一索引** `ux_tax_policy_versions_open_slot (jurisdiction, policy_type, open_slot)`：一个（辖区, 政策类型）至多一个未截断的已发布版本（MySQL 与 SQLite 的唯一索引都不管 `NULL`）。它的前缀也服务 `resolve_tax_policy` 的查询，不另建索引（表永远只有个位数的行）。
- **`rate` 的精度 DECIMAL(9,6)**：6 位小数 = 百分比保留 4 位小数，马来西亚历来的 SST / GST 税率（5%、6%、8%、10%）都精确表示；ADR-0008 §2 的反算在 `Decimal` 里做、只在净额舍入一次，所以 `rate` 的精度不参与舍入。整数部分留 3 位而不是 1 位（DECIMAL(7,6)），是为了让「把 8% 录成 `8`」这类错误落在 CHECK 上（MySQL errno 3819，`ck_tax_policy_versions_rate`，有明确的约束名），而不是落在列宽上（MySQL errno 1264 / 严格模式外静默截断，SQLite 不检查列宽）。
- **CHECK（九条）**。⚠️ 条件文本作为常量写在 `app/models/tax_policy.py`，迁移里逐字重复一份冻结字面量，`tests/backend/test_migrations.py` 归一空白后比对两边（照 `app/models/fx_rates.py:112` 起的常量与 0015:108 的 `_CHECKS`）：

  | 名字 | 条件 | 说明 |
  | --- | --- | --- |
  | `ck_tax_policy_versions_jurisdiction` | `jurisdiction IN ('MY')` | |
  | `ck_tax_policy_versions_policy_type` | `policy_type IN ('SST')` | |
  | `ck_tax_policy_versions_rate` | `rate IS NULL OR (rate >= 0 AND rate < 1)` | |
  | `ck_tax_policy_versions_status` | `status IN ('DRAFT', 'PUBLISHED', 'RETIRED')` | |
  | `ck_tax_policy_versions_unpublished`（`UNPUBLISHED_CHECK`） | `status <> 'DRAFT' OR (effective_from IS NULL AND effective_to IS NULL AND approved_by IS NULL AND approved_at IS NULL)` | 草稿没有区间、没有发布人 |
  | `ck_tax_policy_versions_approved`（`APPROVED_CHECK`） | `status NOT IN ('PUBLISHED', 'RETIRED') OR (approved_by IS NOT NULL AND approved_at IS NOT NULL)` | 发布过的一定有发布人与时刻 |
  | `ck_tax_policy_versions_published_start`（`PUBLISHED_START_CHECK`） | `status NOT IN ('PUBLISHED', 'RETIRED') OR effective_from IS NOT NULL` | 与汇率不同：税务政策没有「一直以来」，发布过的一定有具体起点（理由见 §9） |
  | `ck_tax_policy_versions_period`（`PERIOD_CHECK`） | `effective_from IS NULL OR effective_to IS NULL OR effective_from < effective_to OR (status = 'RETIRED' AND effective_from = effective_to)` | 与 `app/models/fx_rates.py:130` 逐字相同：区间不倒置，空区间只能出现在 `RETIRED` 行上 |
  | `ck_tax_policy_versions_retired_end`（`RETIRED_END_CHECK`） | `status <> 'RETIRED' OR effective_to IS NOT NULL` | 停用必有尽头（`app/models/fx_rates.py:136`） |

- **触发器**（MySQL；建之前做 0006 / 0015 同款权限预检：查 `@@log_bin` 与 `@@log_bin_trust_function_creators`，binlog 开而开关关时**在任何 DDL 之前**抛错，`--sql` 离线模式跳过，照 0015:317–328）。版本表三个：
  - **BEFORE INSERT**：`NOT (NEW.status <=> 'DRAFT')` → SIGNAL。版本只能经「草稿 → 发布」进入已发布，发布跃迁上的检查绕不过去（0015:192）。
  - **BEFORE UPDATE**，按顺序（照 0015:216–285 的五道）：
    1. `OLD.status = 'RETIRED'` → SIGNAL：终态，拒绝任何改动。
    2. `OLD.status = 'DRAFT' AND NEW.status NOT IN ('DRAFT', 'PUBLISHED')` → SIGNAL：草稿不能直接退役。
    3. `OLD.status = 'DRAFT' AND NEW.status = 'PUBLISHED' AND NEW.effective_to IS NOT NULL` → SIGNAL：新发布的版本永远是时间线末尾、未截断。
    3b. **发布跃迁上的税务一致性**（v2）：`OLD.status = 'DRAFT' AND NEW.status = 'PUBLISHED'` 时，令 `reg = JSON_EXTRACT(NEW.treatment_json, '$.sst_registered')`、`chg = JSON_EXTRACT(NEW.treatment_json, '$.tax_charged')`，下列任一成立 → SIGNAL：`NOT (JSON_TYPE(reg) <=> 'BOOLEAN')`；`NOT (JSON_TYPE(chg) <=> 'BOOLEAN')`；`JSON_UNQUOTE(reg) = 'false' AND JSON_UNQUOTE(chg) = 'true'`（未注册不得收税）；`(JSON_UNQUOTE(chg) = 'false') <> (NEW.rate IS NULL)`（不收税 ⇔ 没有税率）。只检查这两个标志与 `rate` 的关系，不检查 `document_notice` 等其余形状（那些在应用层，§9）。触发器只在 MySQL 上存在，所以 CHECK 那条「两种方言写不出同一条文本」的顾虑在这里不成立。已发布行之后不能再改这三处（规则 4），所以只在发布跃迁上查一次就覆盖了全部已发布行
    4. `OLD.status = 'PUBLISHED'`：除 `effective_to`、`status`、`updated_at` 外，`id`、`public_id`、`jurisdiction`、`policy_type`、`rate`、`treatment_json`、`source_reference`、`effective_from`、`created_by`、`approved_by`、`approved_at`、`created_at` 逐列 `NOT (NEW.x <=> OLD.x)` 任一成立 → SIGNAL；`status` 只允许变为 `RETIRED`。不比 `open_slot`：MySQL 触发器不能用 NEW / OLD 引用生成列，它只由被比较的列算出（0015:213 的注释）。`treatment_json` 是 JSON 列，`<=>` 按 JSON 值比较（§10 假设 1，由 §7 用例验证）。
    5. **区间不重叠**：`NEW.status IN ('PUBLISHED', 'RETIRED')` 且 `effective_from`、`effective_to` 或 `status` 有变化时，先 `SELECT id INTO locked_id FROM tax_policy_locks WHERE id = 1 FOR UPDATE`（与服务层同一把锁，绕过服务的直接写入也被串行）；本行为空区间时到此为止；否则同一 `(jurisdiction, policy_type)` 内存在另一行 `PUBLISHED` / `RETIRED`、区间非空、且与本行相交 → SIGNAL。相交判定照 0015:276–279 的 `COALESCE(…, '1000-01-01') < COALESCE(…, '9999-12-31')` 写法（起点在已发布行上恒非空，但 COALESCE 保持与 0015 同构，也覆盖 `PUBLISHED_START_CHECK` 尚未生效的 BEFORE 阶段）。
  - **BEFORE DELETE**：一律拒绝。版本会被支付、收据、对账单的快照永久引用（INV-6）。
- **规则 2–5 里有一部分今天没有写入方**（截断、退役、空区间）：它们与汇率表同构地写在触发器里，是因为以后的管理端接口（另一个闸门）会用到，而那时再改一张已被财务快照引用的表的触发器要多一个迁移、多一次权限预检。这是结构性兜底，不是服务函数；服务层本次只走「插入草稿 → 发布」一条路径。
- **数据库不拦的**：直接写库把首条政策发布成一个任意的过去起点（与 0015 相同的口径：拦它要比较应用与数据库两个时钟）。`TRUNCATE` / `DROP` 是 DDL，不经触发器（与 0006 / 0015 / 0016 相同的残余风险，归「迁移账号与运行账号拆分」）。

### `treatment_json` 的形状（应用层校验）

首条政策的值（键名英文，说明文字照 ADR-0008 第 5 节逐字）：

```json
{
  "schema_version": 1,
  "sst_registered": false,
  "tax_charged": false,
  "document_notice": {
    "zh": "价格为最终价，已包含所有费用，不另收任何税费",
    "en": "Prices are final and include all charges. No tax is charged separately."
  }
}
```

- 校验放在 `app/repositories/tax_policy.py` 的 `parse_treatment(raw) -> TaxTreatment`（冻结 dataclass，含冻结的 `DocumentNotice(zh, en)`），写入前（服务层）与读出时（`resolve_tax_policy`）各走一次；不合格抛 `ValueError`。规则（显式检查，不用 Pydantic 的宽松转换）：
  1. 顶层是 `dict`，键集合**恰好**是上面四个（缺一个、多一个都拒绝）；
  2. `schema_version` 是 `int` 且 `== 1`（`bool` 不算 `int`：`type(x) is int`）；
  3. `sst_registered`、`tax_charged` 的 `type(x) is bool`（字符串 `"false"`、`0` 都拒绝）；
  4. `document_notice` 是 `dict`，键集合恰好是 `zh`、`en`；两个值都是 `str`，非空、去首尾空白后与原值相同、不含换行、长度 ≤ 255；
  5. 跨字段：`sst_registered` 为假 ⇒ `tax_charged` 为假（未注册不得收税）。
- 服务层另查一条跨列规则（`rate` 是列、不在 JSON 里）：`tax_charged` 为假 ⇔ `rate IS NULL`。首条政策两者分别是 `false` 与 `NULL`。 这条规则与「未注册不得收税」在三处各查一次（v2）：写入前（服务层）、发布跃迁上（MySQL 触发器规则 3b）、读出时（`resolve_tax_policy`）；规则只写一份，放在仓储层的 `check_rate_consistency(rate, treatment)`，服务层与 `resolve_tax_policy` 都调它。
- `document_notice` 就是 ADR-0008 第 5 节要求「随税务政策版本快照进每张收据与对账单」的那句话：Phase 4 的收据与对账单从这里取，以后改措辞 = 新版本，不改历史凭证。
- 为什么不在数据库里 CHECK JSON 形状、为什么不拆成类型化的列，见 §9。

### 首条政策的内容（`app/services/tax_policy.py` 的模块常量）

| 字段 | 值 |
| --- | --- |
| `jurisdiction` / `policy_type` | `MY` / `SST` |
| `rate` | `NULL` |
| `treatment_json` | 上一节的对象 |
| `effective_from` | `2026-10-16T16:00:00`（不带时区的 UTC）= 吉隆坡 2026-10-17 00:00（固定 +8，马来西亚无夏令时，不依赖 tzdata，与汇率设计同一口径）；常量旁注明换算，§7 有用例断言 `datetime(2026, 10, 17, tzinfo=timezone(timedelta(hours=8)))` 换成 UTC 去时区后等于它 |
| `effective_to` | `NULL` |
| `source_reference` | `ADR-0008 §5 (docs/adr/ADR-0008-sst-tax-treatment.md), decided by the decision maker on 2026-10-17; no tax adviser consulted`（仓库内路径，不是链接；未聘顾问一事照 spec §45.1 的要求写进出处） |
| `created_by` = `approved_by` | CLI 参数指定的那个 ADMIN 的 `users.id` |
| `created_at` = `updated_at` = `approved_at` | 锁后取的服务端当前时间，截到整秒 |

**为什么 `effective_from` 可以早于发布时刻**：CLI 在部署之后才运行，`approved_at` 必然晚于 2026-10-16T16:00:00，所以这条政策对 `[2026-10-16T16:00:00, approved_at)` 是追溯生效。这是安全的，理由与汇率设计 P1（时间线为空时「一直以来」）相同 —— 它只把「无政策」变成「有政策」：发布之前 `resolve_tax_policy` 对任何时刻都返回 `None`，而 ADR-0008 收口条件禁止任何消费方（充值、收据、对账单、支付）在这条政策发布之前开工，所以不存在任何已经按「无政策」快照过的凭证。这一条依赖流程（Phase 4 闸门），不依赖代码（§10 假设 2）。

### 仓储层（`app/repositories/tax_policy.py`，只 flush 不 commit）

| 函数 | 作用 |
| --- | --- |
| `lock_tax_policies(session)` | `SELECT … FROM tax_policy_locks WHERE id = 1 FOR UPDATE`（`populate_existing`），照 `app/repositories/fx_rates.py:77` |
| `any_version_exists(session) -> bool` | `SELECT 1 … LIMIT 1 FOR SHARE`：锁后的读一律是加锁读（汇率设计「锁在前」：REPEATABLE READ 下普通读可能读到等锁之前的快照） |
| `approver_by_email(session, email) -> User | None` | `FOR SHARE` 读 `users`；角色与状态由服务层判断 |
| `insert_draft(session, *, jurisdiction, policy_type, rate, treatment, source_reference, created_by, now)` | 生成 `public_id`、`status = DRAFT`、写 `created_at` / `updated_at`，flush |
| `mark_published(session, row, *, effective_from, approved_by, now)` | 只接受草稿；`DRAFT → PUBLISHED`、`[effective_from, NULL)`、`approved_by` / `approved_at` / `updated_at`，flush（照 `app/repositories/fx_rates.py:309`） |
| `period_versions(session, jurisdiction, policy_type) -> list` | `PUBLISHED` / `RETIRED` 行，`FOR UPDATE`，供事务内复查 |
| `parse_treatment(raw) -> TaxTreatment` / `treatment_json(t) -> dict` | 见上一节 |
| `resolve_tax_policy(session, jurisdiction, policy_type, at) -> ResolvedTaxPolicy | None` | 见下 |

**`resolve_tax_policy`**（照 `app/repositories/fx_rates.py:344` 的 `resolve_fx_rate`）：取同一 `(jurisdiction, policy_type)`、`status IN ('PUBLISHED', 'RETIRED')`、`effective_from <= at` 且（`effective_to IS NULL` 或 `effective_to > at`）的行，加锁读（`with_for_update(read=True)` + `populate_existing`，照 `app/repositories/fx_rates.py:64` 的 `_shared`）；没有 → `None`；多于一个是数据损坏，`scalar_one_or_none` 抛错，不静默挑一个。空区间永不匹配（`from = to` 时两个条件不能同时成立）。`at` 是不带时区的 UTC。返回：

```python
@dataclass(frozen=True)
class ResolvedTaxPolicy:
    version_id: int          # 内部 id：消费方写进 payments.tax_policy_version_id，不对外
    public_id: str
    jurisdiction: str
    policy_type: str
    rate: Decimal | None
    treatment: TaxTreatment  # 经 parse_treatment，读出时再校验一次
    effective_from: dt.datetime
    effective_to: dt.datetime | None
```

读出时 `parse_treatment` 或 `check_rate_consistency` 失败（库里被绕过服务写坏，例如 SQLite 上没有触发器时 ORM 直接写入的矛盾行）→ 抛错，不返回半个结果：消费方据此拒绝出凭证（fail closed）。

### 服务层（`app/services/tax_policy.py`）

- `publish_initial_tax_policy(session_factory, *, approver_email: str, clock: Clock = utc_now) -> str`：按本节开头的数据流 ①–⑩，返回新行的 `public_id`。`approver_email` 由调用方先经 `normalise_email`；服务层只按归一后的值精确匹配。
- 错误（`AppError` 子类，照 `app/services/fx_rates.py:72` 起的写法；今天没有 HTTP 映射，`http_status` 按仓库惯例照填）：
  - `TaxPolicyAlreadyExists`：`TAX_POLICY_EXISTS`（409）—— 表里已有任何一行（无论什么状态）
  - `TaxPolicyApproverInvalid`：`TAX_POLICY_APPROVER_INVALID`（422）—— 邮箱没有账号、角色不是 `ADMIN`、或状态不是 `ACTIVE`；三种不区分（CLI 的提示文字也不区分，见 §6）
- 事务内复查（⑧）：`period_versions` 加锁重读，断言恰好一行且 `id` 是本行、`effective_to` 为空；不满足抛 `RuntimeError` 整体回滚（防实现错误；SQLite 没有触发器，这一步是那里唯一的结构检查）。

### 接口：CLI（`app/cli.py`）

- 子命令 `publish-initial-tax-policy`，唯一参数 `--approved-by-email`（必填，`argparse` 缺参数时退出码 2，照 `app/cli.py:110` 的 `create-admin` 写法）。
- 函数 `publish_initial_tax_policy(email) -> int`，结构照 `app/cli.py:41` 的 `create_admin`：`get_settings()` → `normalise_email`（`InvalidEmail` → 打印原因到 stderr，返回 2，**不建引擎**）→ `create_database_engine` / `create_session_factory` → 调服务。
- **从不提示输入**：不调用 `input()` / `getpass`，不读标准输入（`docker compose exec -T` 也能跑）。
- 退出码与输出：

  | 情形 | 退出码 | stdout | stderr |
  | --- | --- | --- | --- |
  | 成功 | 0 | 一行 `public_id` | `Published tax policy MY/SST effective from 2026-10-16T16:00:00 UTC.` |
  | 邮箱格式不对 | 2 | 空 | `InvalidEmail` 的原因 |
  | 审批人不合格 | 1 | 空 | `No active ADMIN account for <归一后的邮箱>; nothing was written.` |
  | 已有政策 | 1 | 空 | `A tax policy version already exists; nothing was written.` |
  | 缺 `--approved-by-email` | 2（argparse） | 空 | argparse 用法 |
  | 数据库错误 | 非零（异常） | 空 | 异常（引擎 `hide_parameters=True`，`app/core/database.py:63`，不带 SQL 参数），外加一句固定提示（v2）：`If the error happened at commit, the policy may already be published; run the same command again to find out.` |

- 审计的 `context` 是 `RequestContext(ip_address=None, user_agent="cli")`（`app/cli.py:84` 的先例）。

### 事务边界

| 路径 | 事务 |
| --- | --- |
| `publish_initial_tax_policy` | 一个 `session_scope`（`app/core/database.py:98`）：锁（第一条语句）→ 存在性检查 → 审批人 → 草稿 → 审计 → 发布 → 复查 → 审计 → commit；任何异常整体回滚 |
| `resolve_tax_policy` | 不开事务，在调用方的事务里加锁读 |
| CLI | 只调上面一个服务函数；格式校验在事务外 |

### 外部系统与异步边界

无。没有出站调用、没有 Celery 任务、没有 outbox、没有告警维度。

### 时间语义

- `effective_from` / `effective_to` / `approved_at` / `created_at` / `updated_at`：不带时区的 UTC，整秒；区间半开 `[from, to)`。
- 吉隆坡日期与 UTC 的换算用固定 +8 小时。
- `approved_at` 取锁后的服务端时间（与汇率发布一致），它晚于 `effective_from`（见「首条政策的内容」）。

### 金额精度

- 本任务不产生、不改变任何金额。`rate` 为 `NULL`；列类型 `DECIMAL(9,6)`、读出为 `Decimal`（INV-10）。
- ADR-0008 §2 的反算公式（`net = ROUND_HALF_UP(gross / (1 + r), 8)`、`tax = gross - net`）在 `rate IS NULL` 时不执行；那是 Phase 4 支付设计的契约，见下。

### 审计

两个新 `AuditAction`：`TAX_POLICY_CREATE`、`TAX_POLICY_PUBLISH`，在 `app/models/auth.py` 的 `AuditAction` **末尾**追加（写入方都是 `app/services/tax_policy.py`，与版本行同一事务；两者都不在 spec §66 清单里，按 `PROJECT_CREATE` / `FX_RATE_*` 的先例补上并写进 PR 描述）。`frontend/src/api/adminAudit.ts:30` 的 `AUDIT_ACTIONS` 按同一顺序追加两项 —— `adminAudit.test.ts` 解析 `app/models/auth.py` 逐项比对（同文件 20–29 行的注释），漏了前端测试就红。`entity_type = "tax_policy_version"`、`entity_id = public_id`，归进 `app/services/audit_query.py:39` 的 `PUBLIC_ENTITY_TYPES`（否则 `tests/backend/test_admin_audit_api.py` 的扫描用例变红，汇率同一情形）。`actor` 是审批人，`reason = "INITIAL_POLICY"`（照 `create_admin` 的 `reason="BOOTSTRAP"`，`app/cli.py:91`）。

| `action` | 前（before） | 后（after） |
| --- | --- | --- |
| `TAX_POLICY_CREATE` | — | `jurisdiction`、`policy_type`、`rate`（`null` 或字符串）、`treatment`（整个对象，已校验）、`source_reference`、`status`（`DRAFT`）、`created_by_email` |
| `TAX_POLICY_PUBLISH` | `status`（`DRAFT`） | `status`（`PUBLISHED`）、`effective_from`、`effective_to`（`null`）、`approved_by_email`、`approved_at` |

不含内部 id；人用登录邮箱；时刻 ISO 8601 UTC（照 `app/services/fx_rates.py` 的审计写法）。`treatment` 只是一句公开的说明文字与三个标志，没有个人数据。

### 对下游任务的契约

| 下游 | 契约 |
| --- | --- |
| Phase 4 其余全部任务 | **开工前提**：本政策已在**生产** `PUBLISHED`（运维执行完 §8 的步骤、并由文档 PR 在 ADR-0008 收口条件里勾掉第 2、3 步）。仅代码合并不算 |
| Phase 4 支付 / 收据 / 对账单 | 在生成凭证的事务里调 `resolve_tax_policy(session, "MY", "SST", 支付时刻或账期)`；`None` 或抛错 → 拒绝出凭证（不默认「无税」）。把 `version_id` 写进 `payments.tax_policy_version_id`（§74.7）；把 `treatment.document_notice` 的两句话原样快照进收据 / 对账单（ADR-0008 第 5 节）；`tax_charged` 为假时 `tax_amount` 为空、不执行反算 |
| 同上 | 若届时存在管理端发布 / 退役路径，消费方需要与发布串行：由第一个消费方在自己的闸门里加 `lock_tax_policies_shared`（`FOR SHARE`，照 `app/repositories/fx_rates.py:92`），锁顺序写进那个闸门。本任务**不**提供这个函数（没有调用方；§10 未决 3） |
| 以后的「注册 SST」任务 | 管理端接口、第二条政策的发布规则（截断首条、区间从哪一刻起）另过闸门；表结构与触发器已能承载 |

## 3. 不变量影响矩阵

| 不变量 | 是否触碰 | 可能怎样被破坏 | 设计控制 | 验证方式 |
| --- | --- | --- | --- | --- |
| INV-1 中心故障不中断 AI | 否 | — | AI 请求与用量计费路径都不读税务政策（价格是最终价）；本任务不碰 `app/ai_api/` 与计费服务 | 改动路径不含这些文件（§11） |
| INV-2 事件不重复扣费 | 否 | — | 不碰用量事件与扣费 | — |
| INV-3 支付不重复入账 | 否 | — | 支付尚不存在；本表只被将来的支付引用，不产生入账 | — |
| INV-4 余额只经账本变动 | 否 | — | 不碰钱包与账本；ADR-0008 第 5 节「充值属服务预付」也意味着充值时不产生税务事件 | — |
| INV-5 历史账本不可变 | 否（同类控制） | 已发布的政策被改，等于改写引用它的历史凭证的含义 | 归 INV-6 那一行的控制 | 见 INV-6 |
| INV-6 事件保留版本引用 | **是** | ① 已发布行被改（措辞、税率、起点）；② 已发布行被删；③ 绕过服务直接插入已发布行、草稿直接退役、发布时带尽头；④ 两条同时生效的政策；⑤ 读到被写坏的 `treatment_json` 时静默当成「无税」；⑥ 发布一条自相矛盾的政策（声称收税却没有税率，或未注册却收税） | ① BEFORE UPDATE 对 `PUBLISHED` 逐列 `<=>`，`RETIRED` 终态；② BEFORE DELETE 拒绝 + FK RESTRICT；③ BEFORE INSERT 只许草稿、禁止 `DRAFT → RETIRED`、发布跃迁上尽头必须为空；④ `open_slot` 唯一索引 + 锁内区间不重叠触发器 + 服务层事务内复查；⑤ `resolve_tax_policy` 读出时再校验形状，失败抛错，契约要求消费方拒绝出凭证；⑥ `check_rate_consistency` 在写入前与读出时各查一次，MySQL 发布跃迁上触发器规则 3b 再查一次（v2） | §7「触发器」「CHECK」「open_slot」「区间不重叠」「解析」各行 |
| INV-7 客户不可见成本毛利 | 否 | — | 表里没有成本与毛利；本任务没有任何对外接口。`document_notice` 将来给客户看，本就是公开文字 | — |
| INV-8 租户不可互访 | 否 | — | 全局数据，不按租户；没有租户可见的读路径 | — |
| INV-9 对话内容不入库 | 否 | — | 不处理 prompt / response | — |
| INV-10 金额用 Decimal | **是**（间接） | `rate` 经 `float`；把百分数录成整数 | `Numeric(9, 6, asdecimal=True)`；CHECK `0 ≤ rate < 1`；首条为 `NULL` | §7「CHECK」「列」 |
| INV-11 event_id 至多一次财务效果 | 否 | — | 不处理事件；CLI 的「至多一次」由「表非空即拒绝」保证（§4），与 event_id 无关 | — |
| INV-12 定稿对账单不可变 | 否（为它铺路） | 对账单快照的政策措辞事后被改 | 对账单尚不存在；本表的不可变规则（INV-6 行）是它将来快照的前提 | — |
| INV-13 状态与事件原子提交 | **是** | 版本发布了而审计没写；草稿写了而发布失败留下孤儿草稿 | 草稿、发布、两条审计、复查同一事务；任何一步抛错整体回滚 | §7「事务中途失败」（SQLite 与 MySQL 各一次） |
| INV-14 队列丢失不毁持久工作 | 否 | — | 没有队列、没有异步任务 | — |

**本任务自己的不变量**：同一（辖区, 政策类型）在任一时刻至多一条政策生效；政策一经发布，内容永不改变；生产库里第一条政策只能经 CLI 产生一次。

## 4. 状态与并发

| 当前状态 | 事件 | 前置条件 | 新状态 | 副作用 | 非法时结果 |
| --- | --- | --- | --- | --- | --- |
| — | `publish_initial_tax_policy` 步骤 ⑤ | 持锁；表为空；审批人是 ACTIVE ADMIN | `DRAFT` | 审计 `TAX_POLICY_CREATE` | 表非空 → `TaxPolicyAlreadyExists`；审批人不合格 → `TaxPolicyApproverInvalid`；都不写 |
| `DRAFT` | 同一事务的步骤 ⑦ | 同上 | `PUBLISHED [2026-10-16T16:00:00, NULL)` | 审计 `TAX_POLICY_PUBLISH` | 触发器 / 复查失败 → 整个事务回滚，连草稿一起没有 |
| — | 直接 INSERT 非草稿 | — | — | — | MySQL SIGNAL（1644） |
| `DRAFT` | 直接改为 `RETIRED` | — | — | — | SIGNAL |
| `DRAFT` | 直接改为 `PUBLISHED` 且带尽头 | — | — | — | SIGNAL |
| `PUBLISHED` | 改 `effective_to` / 改为 `RETIRED`（带尽头） | 不与其他行相交 | `PUBLISHED`（已截断）/ `RETIRED` | — | **本任务没有写入方**；触发器允许，留给以后的管理端路径 |
| `PUBLISHED` | 改任何其他列；改回 `DRAFT` | — | — | — | SIGNAL |
| `PUBLISHED` | 改为 `RETIRED` 不带尽头 | — | — | — | CHECK `ck_tax_policy_versions_retired_end`（3819） |
| `RETIRED` | 任何改动 | — | — | — | SIGNAL |
| 任何 | DELETE | — | — | — | SIGNAL |

- **并发写入如何串行化**：服务函数的第一条语句是 `tax_policy_locks` 的 `FOR UPDATE`，两次并发运行的 CLI 串行；后到者在锁后用加锁读看到先到者已提交的行，判 `TaxPolicyAlreadyExists`。版本表 BEFORE UPDATE 触发器的区间检查也先拿这把锁，绕过服务的直接写入同样被串行。SQLite 忽略锁（测试单线程）。
- **数据库负责的唯一性**：`public_id`；`(jurisdiction, policy_type, open_slot)`。
- **幂等键**：没有请求 id。「生产库里只有一条首条政策」由「锁内表非空即拒绝」保证：第二次运行是**报错的空操作**（退出码 1，不写），而不是「内容相同就返回成功」—— 后者要定义「相同」（审批人不同算不算），而运维要的只是「可以放心重跑」，报错空操作已经满足（决策人 2026-10-17 定）。
- **相同 ID、不同载荷**：第二次运行换了审批人邮箱 → 同样拒绝，已发布的行不变（审批人写错的后果见 §5）。
- **重试发生在哪一层**：只有人工重跑 CLI；服务内不重试。
- **必须原子提交**：草稿 + `TAX_POLICY_CREATE` + 发布 + 复查 + `TAX_POLICY_PUBLISH`。

## 5. 失败与恢复矩阵

| 失败点 | 对外结果 | 数据最终状态 | 是否可重试 | 恢复来源 | 告警 |
| --- | --- | --- | --- | --- | --- |
| 校验失败：邮箱格式不对 | 退出码 2，stderr 说明 | 未连库，什么都没写 | 是，改参数 | — | 无 |
| 校验失败：审批人不存在 / 非 ADMIN / 已停用 | 退出码 1 | 回滚，什么都没写 | 是，换正确的账号 | 先用 `create-admin` 建账号或启用 | 无 |
| 校验失败：`INITIAL_TREATMENT` 不合格（代码缺陷） | 异常，非零退出 | 事务外失败，没连库 | 否，修代码 | — | 无（测试应先抓住，§7） |
| 已有政策（第二次运行） | 退出码 1 | 不变 | 不需要 | — | 无 |
| 已有非预期的行（有人绕过服务写了草稿） | 退出码 1 | 不变 | 否 | 人工排查；不得 TRUNCATE（§8「绝不能做」） | 无 |
| 提交之前失败：锁等待超时 / 连接断 / 语句报错（`COMMIT` 尚未发出） | 异常，非零退出 | 整体回滚，什么都没写 | 是，直接重跑 | — | 无 |
| **提交结果不明**（v2）：`COMMIT` 已发出，连接在收到确认之前断开或超时 | 异常，非零退出；CLI 在 stderr 加一句：数据库错误发生时无法确定是否已提交，请原样再运行一次 | 不确定：可能已提交（一行 + 两条审计），也可能已回滚（什么都没有）；两者之一，不会出现半个 | 是，**原样再运行一次即可判定**：退出码 1 且提示已存在 = 上次已提交（`public_id` 按下一行取）；退出码 0 = 上次没有提交、这次发布成功 | 「表非空即拒绝」让重跑本身就是核对，不需要人去猜 | 无 |
| 提交成功、打印前进程被杀 | 无 stdout | 政策已发布 | 重跑会得到退出码 1（证明已发布） | `public_id` 从管理端审计日志（`entity_type = tax_policy_version`）或 `SELECT public_id FROM tax_policy_versions` 取 | 无 |
| 触发器或复查拒绝（实现错误） | 异常 | 回滚 | 否，修代码 | — | 错误输出 |
| 审批人邮箱填错成另一个有效 ADMIN | 退出码 0 | 政策已发布，`approved_by` 是错的人，**不可改**（INV-6） | 否 | 以后的管理端路径退役并重发；当前只能接受。runbook 要求执行前核对邮箱（§8） | 无 |
| 迁移：权限预检失败 | 部署失败，容器不换 | 什么都没建，`alembic_version` 仍是 `0021_ai_module` | 打开 `log_bin_trust_function_creators` 后重新部署 | — | 部署流水线失败通知 |
| 迁移：中途失败（DDL 不参与事务） | 部署失败，旧容器继续服务 | 可能留下 `tax_policy_locks`（含那一行）及部分触发器 / 版本表 | 确认 `alembic_version` 仍是 `0021_ai_module`、版本表为空后，按 `tax_policy_versions` → `tax_policy_locks` 顺序 DROP（不存在的跳过），再重新部署 | 迁移文件头的清场步骤 | 同上 |
| Redis / Celery 丢失 | 不适用 | — | — | 本任务不用队列 | — |
| 外部服务超时 | 不适用 | — | — | 没有出站调用 | — |
| 重复请求 | 见「已有政策」 | | | | |

## 6. 数据与安全边界

- **租户过滤**：不适用。全局数据，没有租户可见的读路径。
- **鉴权主体及权限**：没有 HTTP 入口。能执行 CLI 的人是能进生产 `api` 容器的人（VPS 上的部署用户 / Kelvin，或经 Kelvin 授权的运维会话）—— 与 `create-admin` 同一道门。`--approved-by-email` 只决定**记在谁名下**，不是认证；服务层要求它是 `ACTIVE` 的 `ADMIN`，防止把政策记在客户账号或已停用账号名下。不要求审批人已注册 2FA（CLI 不经登录；§10 小决定）。
- **审批人是谁**：决策人自己的 ADMIN 账号。⚠️ **不得用生产上的验收专用管理员**（`docs/TODO.md` 第 176 行那个只用于验收的账号）—— 它的 `approved_by` 会永远留在这条政策上。runbook 写明。
- **客户侧禁止返回的内部字段**：`id` / `version_id`、`created_by`、`approved_by`（内部用户 id）。本任务没有客户侧接口；`ResolvedTaxPolicy.version_id` 只给消费方写快照。
- **日志、审计、异常**：CLI 的 stdout 只有 `public_id`；stderr 可出现归一后的审批人邮箱（与 `create-admin` 相同）。审计里人用邮箱、不放内部 id。数据库异常不带 SQL 参数（引擎 `hide_parameters=True`）。三种审批人不合格的情形用同一句提示，不在输出里区分「不存在 / 不是 ADMIN / 已停用」—— 操作者本来就有库的访问权，区分没有安全意义，但统一更简单。
- **密钥**：不涉及。没有新的密钥、令牌或配置项。
- **prompt / response 或客户数据**：不涉及。
- **数据保留及删除**：政策版本永不删除（触发器 + FK RESTRICT）；审批人账号因此不能被删除（`users` 的 FK RESTRICT），只能停用 —— 与三张版本表相同。
- **公开仓库**：文档与测试里不写真实邮箱、主机名；runbook 用 `<管理员邮箱>`、`<部署目录>` 占位。测试用 `@tax-policy-test.example.com` 域名。`document_notice` 两句话是 ADR 里的公开文字。

## 7. 测试证据计划

标「MySQL」的用例需要 `BILLING_TEST_DATABASE_URL`，没有就 skip（**skip 不是通过**，CI 设置它）；Worker 沙箱跑不了，第一次真跑在 CI 的 MySQL 分片。SQL 断言不写死方言字面量（CLAUDE.md「写后端测试的约定」）。MySQL 用例共享测试库：每个用例前后 `SET FOREIGN_KEY_CHECKS = 0; TRUNCATE TABLE tax_policy_versions` 与 `TRUNCATE TABLE audit_logs`（版本与审计都拒绝 DELETE，照 `tests/backend/test_fx_rates_service.py:366–383` 的 `clean`），**不碰 `tax_policy_locks`**（那一行属于迁移）；否则留下的行会让 `test_migrations.py` 的降级用例撞上 0022 的降级拒绝。各测试模块自带 fixture，不跨文件共用（不改 `conftest.py`）。

| 风险/需求 | 测试层级 | 场景 | 预期结果 |
| --- | --- | --- | --- |
| 正常路径（服务） | integration（SQLite 与 MySQL 各一次，`test_tax_policy_service.py`） | 冻结时钟 `2026-10-18T03:04:05.678`；建一个 ACTIVE ADMIN；调 `publish_initial_tax_policy` | 返回 uuid 形式的 `public_id`；表里恰好一行：`MY` / `SST` / `rate IS NULL` / `treatment_json` 与 §2 的对象相等 / `PUBLISHED` / `[2026-10-16T16:00:00, NULL)` / `source_reference` 与常量相等 / `created_by = approved_by =` 该管理员 / `created_at = updated_at = approved_at = 2026-10-18T03:04:05`；MySQL 上 `open_slot = 1` |
| 审计 | 同上 | 同上 | 恰好两条，顺序 `TAX_POLICY_CREATE`、`TAX_POLICY_PUBLISH`；`entity_type = tax_policy_version`、`entity_id = public_id`；`actor` 是审批人、`user_agent = cli`、`reason = INITIAL_POLICY`；前后状态与 §2「审计」表逐键相等、不含任何内部 id |
| 幂等重放 | 同上 | 成功后再调一次（同一审批人；换一个审批人） | 两次都 `TaxPolicyAlreadyExists`；仍一行、两条审计，行内容逐列不变 |
| 已有非预期的行 | 同上 | 先用 ORM 直接插一条 `DRAFT`，再调服务 | `TaxPolicyAlreadyExists`；仍只有那条草稿、没有审计 |
| 审批人 | 同上 | 邮箱无账号；`CUSTOMER` 角色；`DISABLED` 的 ADMIN；大写加首尾空白的邮箱（经 `normalise_email` 后传入） | 前三个 `TaxPolicyApproverInvalid`、不写；第四个成功 |
| 事务中途失败 | 同上（SQLite 与 MySQL 各一次） | monkeypatch 让 `TAX_POLICY_PUBLISH` 那次 `record_audit` 抛错；另一例让复查抛错 | 回滚：零行、零审计；之后正常调用成功（证明没有残留） |
| 并发 | integration（MySQL，两个线程两个连接） | 两个线程用屏障同时调服务 | 恰好一个成功、一个 `TaxPolicyAlreadyExists`；一行、两条审计 |
| 锁在前 | integration（MySQL） | 连接 A 开事务、`lock_tax_policies` 并插入一条草稿但不提交；线程 B 调服务（等锁）；A 提交 | B 等到 A 提交后判 `TaxPolicyAlreadyExists`（加锁读看到了 A 的行，不是锁前快照） |
| `treatment_json` 解析 | unit（`test_tax_policy_service.py`） | §2 的对象；缺 `tax_charged`；多一个键；`schema_version` 为 `2` / `True` / `"1"`；`sst_registered` 为 `"false"` / `0`；`document_notice` 缺 `en` / 多 `ms` / `zh` 为空 / 首尾有空格 / 含换行 / 256 个字符；`sst_registered=false, tax_charged=true` | 第一个通过，其余 `ValueError` |
| `rate` 与 `tax_charged` 一致 | unit | `rate=None, tax_charged=False`；`rate=Decimal("0.08"), tax_charged=False`；`rate=None, tax_charged=True`（`sst_registered=True`） | 第一个通过，其余拒绝 |
| 说明文字不漂移 | unit | 读 `docs/adr/ADR-0008-sst-tax-treatment.md` | `INITIAL_TREATMENT` 的 `zh`、`en` 两句都在 ADR 里逐字出现 |
| 起点换算 | unit | `datetime(2026, 10, 17, tzinfo=timezone(timedelta(hours=8))).astimezone(timezone.utc).replace(tzinfo=None)` | 等于常量 `2026-10-16T16:00:00` |
| `resolve_tax_policy` 边界 | integration（SQLite 与 MySQL） | 发布后取 `2026-10-16T15:59:59`、`16:00:00`、`2099-01-01T00:00:00`；`jurisdiction` / `policy_type` 不匹配；发布前（只有草稿）取 | `None`、命中、命中、`None`、`None`；命中时 `version_id` 等于行 id、`rate is None`、`treatment` 等于解析结果、对象不可变（`FrozenInstanceError`） |
| `resolve_tax_policy` 其他状态 | integration（SQLite，ORM 直接造行，绕开没有写入方的退役） | 一条 `RETIRED [d1, d2)`：取 `d1`、`d2 - 1s`、`d2`；一条空区间 `RETIRED [d, d)`：取 `d` | 命中、命中、`None`；`None` |
| 读出时校验 | integration（SQLite） | ORM 直接写一条 `treatment_json` 缺键的已发布行，再 resolve | 抛错，不返回结果 |
| 读出时一致性（v2） | integration（SQLite，无触发器） | ORM 直接写三条已发布行，分别是 `rate=NULL` + `tax_charged=true`（`sst_registered=true`）、`rate=0.08` + `tax_charged=false`、`sst_registered=false` + `tax_charged=true` + `rate=0.08`，各自单独 resolve | 三条都抛错，不返回结果 |
| 触发器：发布时税务一致性（v2） | integration（MySQL，`test_tax_policy_db.py`） | 草稿分别为上一行的三种矛盾组合，以及 `tax_charged` 是字符串 `"false"`、`sst_registered` 缺失；各自改为 `PUBLISHED`；另一条 `rate=NULL` + 两个标志都是 `false` 的草稿改为 `PUBLISHED` | 前五个 1644；最后一个通过（证明 `JSON_TYPE` 返回 `BOOLEAN`、`JSON_UNQUOTE` 返回 `true` / `false`，§10 假设 4） |
| 区间重叠的数据损坏 | integration（SQLite，无触发器） | ORM 造两条覆盖同一时刻的已发布行（一条截断、一条未截断但区间相交） | `resolve_tax_policy` 抛错（`scalar_one_or_none`），不静默挑一个 |
| 触发器：插入 | integration（MySQL，`test_tax_policy_db.py`，每例一个回滚的事务，照 `tests/backend/test_fx_rates_db.py`） | 直接插入 `PUBLISHED` / `RETIRED` 行；插入 `DRAFT` | errno 1644 ×2；草稿通过 |
| 触发器：草稿 | 同上 | `DRAFT → RETIRED`（带尽头）；`DRAFT → PUBLISHED` 带尽头；`DRAFT → PUBLISHED` 起点 + 无尽头 + 发布人；`DRAFT` 改 `source_reference` | 1644、1644、通过、通过 |
| 触发器：已发布不可变 | 同上 | 已发布行逐列改 `rate`（`NULL → 0.08`）、`treatment_json`（改 `en` 一个字；键顺序不同但值相同的对象另测）、`effective_from`、`source_reference`、`jurisdiction`、`policy_type`、`approved_by`、`approved_at`、`created_by`、`created_at`、`public_id`；改回 `DRAFT` | 每列 1644；键顺序不同而值相同的 `treatment_json` **通过**（证明 `<=>` 按 JSON 值比较、不误杀，§10 假设 1）；改回草稿 1644 |
| 触发器：已发布允许的改动 | 同上 | 只改 `updated_at`；截断（写 `effective_to`）；`PUBLISHED → RETIRED` 带尽头；`PUBLISHED → RETIRED` 不带尽头 | 通过、通过、通过、3819（`retired_end`） |
| 触发器：终态与删除 | 同上 | 已退役行改 `effective_to` / `updated_at`；删已发布行；删草稿 | 1644 ×4 |
| 锁表 | 同上 | 迁移后行集合；再插一行；`UPDATE … SET id = 2`；删那一行 | `{1}`；1644 ×3 |
| CHECK | 同上 | `rate` 为 `1.000000` / `-0.000001` / `8` / `0` / `0.999999` / `NULL`；`jurisdiction = 'SG'`；`policy_type = 'GST'`；`status = 'DISCARDED'`；草稿带 `approved_by`；草稿带 `effective_from`；`DRAFT → PUBLISHED` 缺 `approved_at`；`DRAFT → PUBLISHED` 起点为空 | 3819、3819、3819、通过、通过、通过；3819 ×7（每条都断言约束名） |
| `open_slot` 与唯一索引 | integration（SQLite 无触发器：唯一索引是唯一的拦截；MySQL 只断言「被拒绝」） | 已有一条未截断的已发布行，再把第二条草稿改为 `PUBLISHED [更晚, NULL)` | SQLite：`IntegrityError`（唯一索引）；MySQL：被拒绝（重叠触发器先于唯一索引，**不断言是哪一道**，照汇率设计 v3 的写法）。另断言：草稿、截断的已发布行 `open_slot` 为 `NULL`，未截断的为 1 |
| 区间不重叠 | integration（MySQL） | A `[d1, NULL)` 已发布；B 草稿直接改为 `PUBLISHED [d2, NULL)`（d2 > d1）不先截断 A；先截断 A 于 d2 再发布 B（首尾相接）；A 截断于 d3 > d2 时发布 B（相交） | 1644；通过；1644 |
| 触发器拿锁 | integration（MySQL，两个连接，`innodb_lock_wait_timeout = 1`） | 连接 1 持 `tax_policy_locks FOR UPDATE` 不提交；连接 2 直接把草稿改为已发布 | 连接 2 锁等待超时（errno 1205）；连接 1 提交后连接 2 重试通过 |
| 外键 | integration（MySQL） | 删除被 `created_by` / `approved_by` 引用的用户 | errno 1451 |
| 迁移：结构 | `test_migrations.py` | `0022` 接在 `0021_ai_module` 之后；九条 CHECK 与生成列表达式归一空白后与模型常量相等；触发器恰好六个（名字、表、时机、事件）；创建顺序 `[tax_policy_locks, tax_policy_versions]`、降级顺序相反；`_TABLES_0022` 加进 `test_0010_*` / `test_0011_*` 降级用例的「随降级删掉的表」集合（`tests/backend/test_migrations.py:1937`、`:2036`） | 相等 |
| 迁移：预检 | `test_migrations.py`（monkeypatch，照 `test_0015_precheck_*`） | binlog 开 + 开关关；其余三种组合 | 第一种在任何 DDL 之前 `RuntimeError`，其余放行 |
| 迁移：降级 | `test_migrations.py`（monkeypatch，照 `:285` 的 `test_0021_downgrade_refuses_lossy_data_before_ddl`；另一例 MySQL 集成） | 版本表有一行时降级；空表时降级；`--sql` 离线 | 有行：在任何 DDL 之前 `RuntimeError("Cannot downgrade tax policy …")`；空表：删两张表、不碰既有表；离线：跳过预检只输出 DDL |
| 迁移：SQLite | `test_migrations.py`（照 `:3465`） | `create_all` | `tax_policy_locks` 恰好一行 `id = 1`；生成列在 SQLite 上按表达式计算 |
| 列 | `test_model_columns.py`（照 `:162` 的汇率一例） | 收集到 `tax_policy_versions` 的三个枚举列 | `jurisdiction` / `policy_type` / `status` 宽 16；`rate` 是 `Numeric(9, 6)`、`asdecimal` |
| CLI 正常路径 | integration（SQLite 文件库 + `create_all`，与 MySQL + `alembic upgrade head` 各一次；`monkeypatch.setenv("BILLING_DATABASE_URL", …)` + `get_settings.cache_clear()`，照 `tests/backend/ai_mysql_world.py:44–47`；`cli.main([...])` + `capsys`，照 `tests/backend/test_ai_ops_requeue.py:297`） | `main(["publish-initial-tax-policy", "--approved-by-email", " Admin@Tax-Policy-Test.example.com "])` | 退出码 0；stdout 恰好是库里那一行的 `public_id` 加换行；stderr 是成功说明 |
| CLI 第二次运行 | 同上 | 紧接着再跑一次 | 退出码 1；stdout 为空；stderr 含 `already exists`；仍一行、两条审计 |
| 提交结果不明（v2） | 服务：integration（SQLite 与 MySQL）；CLI：同上 | monkeypatch 让会话的 `commit` 先真正提交、再抛 `OperationalError`（模拟确认丢失）；然后原样再调一次 | 第一次：异常 / CLI 非零退出且 stderr 含「再运行一次」的提示；库里一行、两条审计。第二次：`TaxPolicyAlreadyExists` / CLI 退出码 1；仍一行、两条审计 |
| CLI 拒绝 | 同上 | 邮箱 `not-an-email`；无账号；`CUSTOMER`；`DISABLED`；缺参数 | 2（且 monkeypatch 让 `create_database_engine` 被调用即失败，证明不连库）；1；1；1；`SystemExit(2)`；拒绝时都不写 |
| CLI 从不提示 | 同上 | monkeypatch `builtins.input` 与 `getpass.getpass` 被调用即 `AssertionError`，标准输入换成空流 | 正常路径与拒绝路径都不触发 |
| 审计分类 | `test_admin_audit_api.py`（不改，现有扫描） | 新增的写审计处 | `tax_policy_version` 已归进 `PUBLIC_ENTITY_TYPES`，用例通过 |
| 前端清单 | `frontend/src/api/adminAudit.test.ts`（不改，现有比对） | `AUDIT_ACTIONS` | 与 `AuditAction` 逐项、同序相等 |
| Redis/Celery 丢失 | 不适用 | 本任务不用队列 | — |
| 租户越权 | 不适用 | 全局数据，没有租户可见的读路径 | — |
| 金额精度 | 见「CHECK」行的 `rate` 各值与「列」行 | | |

## 8. 迁移与上线

- **数据迁移步骤**（迁移 `0022_tax_policy`）：0. 权限预检；1. `create_table tax_policy_locks` 并写入 `id = 1`；2. `create_table tax_policy_versions`（列、两个 FK、`public_id` 唯一、生成列与唯一索引、九条 CHECK）；3. 六个触发器。**不写任何政策**（不种子，§9）。
- **迁移文件头的 §132 第 13 条分析**（照 0015:34–66 的六段，实现时逐段写出）：
  - **已有数据**：不 ALTER、不读写任何既有表；两张都是新表；外键只指向 `users`。
  - **锁表与停机**：只建新表、索引、触发器与锁表的一行；建外键时对 `users` 取短暂的元数据锁、不扫行；不需要停机。
  - **备份**：要求前置备份（与 0005–0021 同一口径）；不改动、不回填任何既有数据，不预置政策。
  - **部署顺序**：`deploy.sh` 先迁移、再换镜像；旧代码不读新表。
  - **失败处理**：MySQL DDL 不参与事务。预检失败什么都没建；中途失败按 §5 的清场步骤（确认 `alembic_version` 仍是 `0021_ai_module`、版本表为空，依次 DROP `tax_policy_versions`、`tax_policy_locks`，触发器随表删除），再重新部署。此时没有写入方，清场不丢业务数据。
  - **回滚**：`downgrade` 先查 `SELECT 1 FROM tax_policy_versions LIMIT 1`，有行就在任何 DDL 之前拒绝（照 0019:78–93、0021:155–162）—— 首条政策发布之后它就是被支付、收据快照引用的版本，删表等于删历史；空表时按 `tax_policy_versions` → `tax_policy_locks` 删表。`--sql` 离线模式读不到数据，跳过预检只输出 DDL（与 0021 相同）。生产只前滚。
  - **残余风险**：`TRUNCATE` / `DROP` / `DROP TRIGGER` 是 DDL，不经触发器（与 0006 / 0015 / 0016 相同）。
- **锁表与性能影响**：见上；表永远只有个位数的行。
- **兼容窗口**：无。新表没有旧读者；没有改既有列。
- **部署顺序**（合并即按提交 SHA 自动部署生产，每一步单独部署都安全）：
  1. 077 合并 → 两张空表（锁表一行）。没有写入方。
  2. 078 合并 → 服务函数存在，但没有入口；行为不变。
  3. 079 合并 → CLI 可用；表仍为空，直到有人运行它。
  4. **运维步骤（只做一次，由 Kelvin 或经 Kelvin 授权的会话在 VPS 上执行；写进 `docs/deployment.md` 与 `docs/runbook.md`）**：
     1. 核对审批人：决策人自己的 ADMIN 账号，`ACTIVE`；**不是**验收专用管理员。邮箱逐字核对 —— 写错不可改（§5）。
     2. `cd <部署目录> && docker compose exec -T api python -m app.cli publish-initial-tax-policy --approved-by-email '<管理员邮箱>'`
     3. 期望退出码 0、stdout 一行 `public_id`。退出码 1 且提示已存在 → 已经发布过，不要再做任何事。**因数据库错误非零退出时，原样再运行一次**（v2）：退出码 1 且提示已存在 = 上次其实已提交，按第 4 步核对并取 `public_id`；退出码 0 = 这次发布成功。其他失败按 §5。
     4. 核对：`docker compose exec -T mysql mysql … -e "SELECT public_id, jurisdiction, policy_type, rate, status, effective_from, effective_to FROM tax_policy_versions"` 恰好一行、内容如 §2；管理端审计日志按 `entity_type = tax_policy_version` 能查到两条。
     5. ⚠️ **绝不能做**：用 SQL 直接插入 / 改写这张表；`TRUNCATE` 后重跑（会让政策换一个 `public_id`、`approved_at`，而且绕过了所有触发器）；为「换审批人」重跑（会被拒绝，本来也不该）。
  5. 运维步骤完成后，开一个文档 PR（不走闸门）：ADR-0008「收口条件」2026-10-17 进度里把第 2、3 步标为完成并写上日期与 `public_id`（`public_id` 不是秘密，可以进公开仓库）；`docs/TODO.md` D2 一行与 Phase 4 一节去掉「不得开工」的阻塞句。**这个 PR 合并之后 Phase 4 其余任务才可以登记为 `ready`**（Phase 4 支付部分另外还要等 D6）。
- **回滚或前滚方式**：发布前可以降级（空表）；发布后只前滚。错误内容的政策只能由以后的管理端路径退役并发布新版本（另一个闸门）。
- **部分部署时的行为**：迁移成功、代码未更新：新表空置；代码更新、迁移失败：容器起不来，旧容器继续服务（与历次相同）。078 / 079 在 077 之前不会被部署（`depends_on`）。
- **监控指标和告警负责人**：不新增告警维度（一次性操作，结果靠步骤 4.4 的人工核对与审计）；负责人 Kelvin。

## 9. 方案取舍

| 方案 | 优点 | 风险 | 未采用原因 |
| --- | --- | --- | --- |
| **采用**：建表（照汇率表的状态机、触发器与锁表）+ 服务函数「锁内建草稿并发布、同一事务写审计」+ 一次性 CLI；部署后运维执行一次 | 与三张版本表同一套约定（只以草稿插入、`approved_by` 指向真实管理员、发布写审计）；表结构以后直接承载管理端路径；重跑安全 | 多一步人工运维；审批人填错不可改 | — |
| 迁移里种子首条政策 | 合并即生效，少一步运维 | `approved_by` 要指向真实的 `users.id`，迁移不知道是谁（生产与测试库不同）；要么让 `approved_by` 为空（违反 `APPROVED_CHECK`），要么在迁移里先插草稿再改已发布并手写审计行，把服务层的规则复制进迁移；三张版本表都不在迁移里种子（0015 文件头「不预置任何汇率」） | 决策人 2026-10-17 定不种子 |
| 现在就做管理端接口与页面 | 以后注册 SST 时现成 | 只为一次写入做一整层接口、前端、鉴权与验收；发布第二条政策的规则（截断首条、从哪一刻起）现在没有需求可依 | 不做；注册 SST 时另立任务（决策人定） |
| 加 `DISCARDED`（照汇率表） | 与汇率表完全同构 | 没有写入方：草稿与发布在同一事务里，不会留下需要丢弃的草稿；没有写入方的枚举值会让人以为那件事存在（`app/models/auth.py:119` 的规矩）；spec §74.9 也没有它 | 不加；以后管理端路径需要时在它的闸门里加（改 CHECK 与触发器是一个小迁移） |
| 不建锁表与区间不重叠触发器（只有一行，`open_slot` 足够） | 少一张表、少两个触发器 | `open_slot` 只管「至多一个未截断的」，管不住两个已截断而相交的区间；以后加锁表要改一张已被财务快照引用的表的触发器 | 建；与汇率 / 定价规则同一套，成本一次付清 |
| 起点用 `NULL`（「一直以来」，汇率 P1 的写法） | 不用决定日期 | 税务政策是有决定日期的法律事实，「一直以来」声称 2026-10-17 之前也是这个口径，ADR 没有这样说 | 用决策人定的 2026-10-17 00:00 吉隆坡；并加 `PUBLISHED_START_CHECK` 把「发布过的必有起点」交给数据库 |
| 起点用发布时刻（`approved_at`） | 不追溯 | 生效日由运维什么时候敲命令决定，而不是由决定日期决定；不同环境起点不同 | 用固定常量；追溯只是「无 → 有」，安全理由见 §2 |
| `treatment_json` 用 TEXT（`json.dumps`，照 `ai_usage_outbox.payload_json`） | 字节级稳定 | 库不校验 JSON 语法；触发器比较的是字节，键顺序不同也算改动 | 用 JSON：库校验语法、`<=>` 按值比较；先例 `provider_price_components.metadata_json`。消费方解析使用，不依赖字节 |
| 数据库 CHECK JSON 形状（`JSON_TYPE` / `JSON_EXTRACT`） | 数据库兜底形状 | MySQL `JSON_TYPE` 返回 `OBJECT`、SQLite `json_type` 返回 `object`，同一条 CHECK 文本无法两边通用，`test_migrations.py` 的逐字比对就做不了；与汇率设计 v3「`^[A-Z]{3}$` 只在应用层校验」同一类取舍 | 应用层写入前与读出时各校验一次；数据库只保证是合法 JSON |
| 把 `sst_registered` / `tax_charged` / 两句说明拆成类型化的列 | 数据库能 CHECK | spec §74.9 定的是 `treatment_json`；注册之后的政策会多出字段（例如服务分类），每次都要迁移 | 用 JSON，形状带 `schema_version` |
| Pydantic 模型校验 | 声明式 | 默认宽松转换（`"false"` → `False`、`1` → `True`）要逐字段开 strict；仓储层与 `app/models` 里没有 Pydantic 的先例 | 冻结 dataclass + 显式检查，规则见 §2 |
| `rate` 用 DECIMAL(7,6) / DECIMAL(5,4) | 更紧 | 把 `8` 录进来时报列宽错误（1264）或在 SQLite 上悄悄存下，而不是有名字的 CHECK 错误 | DECIMAL(9,6)，理由见 §2 |
| CLI「内容相同就返回成功」 | 更像幂等 | 要定义「相同」（审批人不同、时间不同算不算）；成功的退出码会掩盖「其实上次已经做过」 | 有任何行就报错空操作（决策人定） |
| CLI 执行前交互确认 | 防手滑 | `docker compose exec -T` 与自动化跑不了；`create-admin` 的交互只为了不让密码进命令行，这里没有秘密 | 从不提示（决策人定）；防手滑靠 runbook 的核对步骤 |
| 077 一个任务做数据库 + 业务规则（决策人原定） | 少一次登记与合并 | 横跨两层，违反「一个任务只做一层」；`allowed_change_paths` 13 个，超过 12 | 拆成 077 / 078（§11），待决策人确认编号（§10 未决 1） |

## 10. 未决问题与假设

- **未决问题**（需要 Kelvin 拍板，都不阻断设计本身）：
  1. **任务拆分与编号**：决策人原定两个任务（数据库 + 业务规则一个、CLI 一个）。按 CLAUDE.md「一个任务只做一层」且数据库 + 业务规则需要 13 个路径（> 12），§11 拆成三个：077（数据库）、078（业务规则）、079（CLI，依赖 078）。若决策人坚持两个任务，077 与 078 合为一个、13 个路径，需要在登记 PR 里写明超限理由；设计内容不变。
  2. **审批人账号**：假设生产上存在决策人自己的 `ACTIVE` ADMIN 账号（不是验收专用管理员）。若没有，运维步骤之前先用 `create-admin` 建一个并完成 2FA 注册。
  3. **消费方的共享锁**：本任务不提供 `lock_tax_policies_shared`（没有调用方）。是否由 Phase 4 第一个消费方的闸门加，还是本任务现在就加（与 `resolve_tax_policy` 配套，汇率是一起交付的）—— 审查若认为应现在加，078 加一个函数、一条并发用例，不改其他设计。
- **v1 自行写定、未另请 Kelvin 拍板的小决定**（审查若认为需要人拍板请指出）：`jurisdiction` / `policy_type` 用只含一个值的枚举 + CHECK；`rate` 精度 DECIMAL(9,6)；`treatment_json` 的四个键名与 `schema_version`；`PUBLISHED_START_CHECK`；`created_by` 非空；审批人不要求已注册 2FA；审计 `reason = "INITIAL_POLICY"`；CLI 退出码（格式错 2、被拒绝 1）与输出文字；`source_reference` 的措辞。
- **尚未验证的假设**：
  1. MySQL 8 的 `<=>` 对 JSON 列按 JSON 值比较（文档列出 `<=>` 属于 JSON 可用的比较运算符），键顺序不同而值相同不算改动。§7 有正反两个用例验证；若不成立，触发器里改为比较 `CAST(… AS CHAR)` 的规范化文本（MySQL 存 JSON 时已规范化键顺序），设计版本 +1。
  2. 追溯生效安全，依赖「Phase 4 任何消费方都不会在本政策发布前上线」—— 这是 ADR-0008 收口条件与 §8 步骤 5 的流程约束，代码不强制。若被违反（某个消费方在发布前已按「无政策」出过凭证），那些凭证的时刻在发布后会解析到本政策，消费方的闸门必须自行处理。
  3. 生产 MySQL 的字符集能存 `document_notice` 的中文（现有表已存中文的客户名称与审计内容，预计成立）；§7 MySQL 正常路径用例会读回比对。
  4. （v2）MySQL 8 的 `JSON_TYPE` 对 JSON 布尔值返回 `'BOOLEAN'`，`JSON_UNQUOTE(JSON_EXTRACT(…))` 对布尔值返回 `'true'` / `'false'`。§7「触发器：发布时税务一致性」的放行用例验证；不成立则触发器规则 3b 改写、设计版本 +1。
- **如果假设错误，具体后果**：假设 1 错 → 已发布行的 `treatment_json` 要么改不动（误杀，只影响将来的管理端路径），要么改得动（不可变被绕过）；用例会在 CI 红，不会带病上线。假设 2 错 → 历史凭证与政策的对应关系混乱，属于消费方闸门的缺陷，本表不变。

## 11. 审查与版本绑定

审查方要回答的五个问题常驻在 `scripts/review_checklist.md` 的「设计审查」一节，此处不重复。

**版本绑定规则**（出处 `docs/WORKFLOW.md` §3）：

- Codex 的批准必须写成 `APPROVED: design v<N>`，`<N>` 取本 Issue 顶部的「设计版本」
- **设计版本一变，之前的 APPROVE 自动作废**，必须重新过闸门
- 什么算「实质修改」：改了契约、表结构、事务边界、状态机、失败语义、不变量控制。改错别字不算
- 实质修改时：把顶部版本 +1，在下方追加一条变更说明，状态退回 `READY_FOR_REVIEW`

### 设计闸门判定

同时满足才算通过：

- [ ] 需求、非目标和验收标准明确
- [ ] 关键契约与事务边界明确
- [ ] 触碰的不变量都有控制措施
- [ ] 失败路径都有确定的最终状态
- [ ] 高风险控制都有测试场景
- [ ] 没有未解决的阻断假设
- [ ] 审批绑定到明确的设计版本

### 实现范围（批准后逐个登记为 Worker 任务）

**为什么拆成三个**：决策人原定两个任务（数据库 + 业务规则一个、CLI 一个）。前者横跨数据库与业务规则两层，`allowed_change_paths` 数下来是 13 个（迁移、`alembic/env.py`、模型、仓储、服务、`app/models/auth.py`、`app/services/audit_query.py`、`frontend/src/api/adminAudit.ts`、两个测试模块、`test_migrations.py`、`test_model_columns.py`、`docs/database-schema.md`，外加 `docs/TODO.md`），超过 12 个的上限，也违反「一个任务只做一层」。按层拆成 077 / 078，CLI 顺延为 079，每一层合并后都能单独通过检查与 CI、单独部署（§8）。编号见 §10 未决 1。

| 任务 | 层 | 对应章节 | 依赖 | `allowed_change_paths` |
| --- | --- | --- | --- | --- |
| 077（`AIH-TASK-077`） | 数据库迁移与模型 | §2「数据库」、§8 迁移 | — | `alembic/versions/20261017_0022_tax_policy.py`（新）、`alembic/env.py`（注册模型模块）、`app/models/tax_policy.py`（新：两张表、三个枚举、九个 CHECK 常量、`OPEN_SLOT_EXPRESSION`、锁行的 `after_create`）、`tests/backend/test_tax_policy_db.py`（新，MySQL：触发器、CHECK、`open_slot`、区间、锁、外键）、`tests/backend/test_migrations.py`、`tests/backend/test_model_columns.py`、`docs/database-schema.md`（新增一节）、`docs/TODO.md`（8） |
| 078（`AIH-TASK-078`） | 业务规则：仓储与服务 | §2「`treatment_json` 的形状」「首条政策的内容」「仓储层」「服务层」「事务边界」「审计」「对下游任务的契约」、§4 | 077 | `app/repositories/tax_policy.py`（新，含 `parse_treatment` 与 `resolve_tax_policy`）、`app/services/tax_policy.py`（新）、`app/models/auth.py`（`AuditAction` 末尾两个值）、`app/services/audit_query.py`（`PUBLIC_ENTITY_TYPES`）、`frontend/src/api/adminAudit.ts`（`AUDIT_ACTIONS` 同序追加两项；只为让现有的 `adminAudit.test.ts` 比对通过，没有页面变化，**不带浏览器验收块**）、`tests/backend/test_tax_policy_service.py`（新，SQLite 与 MySQL）、`docs/TODO.md`（7） |
| 079（`AIH-TASK-079`） | 接口：运维 CLI 与文档 | §2「接口：CLI」、§5、§8 运维步骤 | 078 | `app/cli.py`、`tests/backend/test_tax_policy_cli.py`（新，SQLite 文件库与 MySQL）、`docs/deployment.md`（部署后运维步骤一节）、`docs/runbook.md`（「税务政策：首条政策的发布（只做一次）」一节：怎么做、失败了怎么办、绝不能做什么）、`docs/TODO.md`（5） |

- 三个任务都不碰 `app/ai_api/`，也不碰钱包 / 支付 / 定价文件（`scripts/check_repo_policy.py` 的 AIH-TASK-071 规则不相关）。
- **验收标准草案**（登记时细化，每个不超过 8 条）：
  - 077：① 0022 接在 `0021_ai_module` 之后，建两张表、九条 CHECK、生成列与唯一索引、六个触发器，锁表恰好一行；② 权限预检先于任何 DDL；③ 模型常量与迁移字面量逐字一致；④ 触发器每条规则（含发布跃迁上的税务一致性 3b）在真 MySQL 上拒绝（1644）、允许的写入通过；⑤ 每条 CHECK 拒绝（3819）、边界值通过；⑥ 降级有行时在 DDL 前拒绝、空表时只删两张表；⑦ SQLite `create_all` 写入锁行；⑧ `docs/database-schema.md` 新增一节。
  - 078：① 一个事务写一条内容如 §2 的 `PUBLISHED` 行与两条审计；② 锁是第一条语句、之后全是加锁读；③ 表非空 → `TaxPolicyAlreadyExists`、不写；④ 审批人不合格 → `TaxPolicyApproverInvalid`、不写；⑤ `parse_treatment` 与 `check_rate_consistency` 如 §2，写入前与读出时都查；⑥ `resolve_tax_policy` 的边界与读出时校验如 §7；⑦ 中途失败整体回滚，提交结果不明时重调得到 `TaxPolicyAlreadyExists`；⑧ 两个 `AuditAction`、`PUBLIC_ENTITY_TYPES` 与前端 `AUDIT_ACTIONS` 同步。
  - 079：① 子命令与参数如 §2；② 成功退出 0、stdout 只有 `public_id`；③ 第二次运行退出 1、不写；④ 邮箱格式错退出 2 且不连库、审批人不合格退出 1、数据库错误时 stderr 提示原样重跑；⑤ 从不提示输入；⑥ SQLite 与 MySQL 各跑一遍；⑦ `docs/deployment.md` 与 `docs/runbook.md` 写明 §8 的运维步骤与「绝不能做」。
- **登记前预审**：三个任务都有设计闸门、077 改数据库结构，按 CLAUDE.md「登记任务的约定」在控制面仓库对每个任务运行 `python -m worker.design_precheck …`，完整输出贴进登记 PR；`NOT_READY` 的发现逐条处理。
- **设计正文进仓库**：批准后，登记 PR 把被批准那一版的 Issue 正文逐字放进 `docs/design/AIH-TASK-077-tax-policy.md`（Worker 沙箱读不到 Issue，照 `docs/design/AIH-TASK-028-fx-rates.md` 文件头的先例）。
- **Phase 4 的闸门不随这三个任务的合并解除**，见 §8 部署顺序第 4、5 步。

## 12. 版本变更记录

| 版本 | 日期 | 改了什么 | 触发原因 |
| --- | --- | --- | --- |
| v1 | 2026-10-17 | 初稿 | ADR-0008 第 5 节与收口条件于 2026-10-17 补完（PR #328），收口第 2 步需要建表并发布首条政策 |
| v2 | 2026-10-17 | ① 新增 MySQL 发布跃迁触发器规则 3b：`sst_registered` / `tax_charged` 必须是 JSON 布尔、未注册不得收税、不收税 ⇔ `rate IS NULL`；② 这条一致性规则收进仓储层 `check_rate_consistency`，写入前与 `resolve_tax_policy` 读出时都查；③ §5 把「连接断」拆成「提交之前失败」（整体回滚）与「提交结果不明」（原样重跑即可判定），CLI 在数据库错误时提示重跑，runbook 第 3 步同步；④ §3、§7、§10、§11 相应补充 | Codex v1 两个阻断项：矛盾政策可被发布并正常解析；提交确认丢失被误判为必然回滚 |


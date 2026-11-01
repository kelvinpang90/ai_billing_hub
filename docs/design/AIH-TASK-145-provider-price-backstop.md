# AIH-TASK-145 设计：供应商价格版本的数据库兜底（已批准 v5）

> **来源**：设计闸门 Issue #535。本文件是 `APPROVED: design v5` 那一版正文的**逐字副本**。审查经过见 §12：v1 送审之前跑了两轮登记前预审，三条发现都已并入；v1–v4 由 Codex 审查、被要求修改（v1：形状核对只查子串；v2：归一化删反斜杠会改变字面量含义；v3：没核对同名 CHECK 是否生效；v4 第一次送审时顶部版本号漏改，第二次：相交判定的哨兵日期在边界上漏判）；v5 送审时 Codex 额度用完、审查中断，经 Kelvin 同意改由 Claude 审查者批准（独立、只读的 Claude Code 会话）；批准后的最终登记前预审发现 §2、§7 有一处说法与 MySQL 实际行为不符，按勘误更正（§12「v5（勘误）」），规则与用例不变，版本仍是 v5。
> 放进仓库，是因为 OpenClaw Worker 在沙箱里不联网、读不到 GitHub Issue。
> **实现以本文件为准**；与 Issue 不一致时，以 Issue #535 上被批准的 v5 为准。设计要改，就回到 Issue 升版本、重新过闸门，不要直接改本文件。
> **前置**：供应商价格本身见设计闸门 #177 v4（[AIH-TASK-026-provider-prices.md](AIH-TASK-026-provider-prices.md)）；迁移 0031 接在 AIH-TASK-136 的 0030 之后（设计闸门 #507 v4，[AIH-TASK-136-pricing-project-scope.md](AIH-TASK-136-pricing-project-scope.md)）。
> §11「任务拆分」的一个实现任务登记为 `AIH-TASK-145`。

---
## 0. 准入判定

| 任务涉及 | 要填的范围 |
| --- | --- |
| 钱包 / 账本 / 定价 / 汇率 / 支付 / 幂等 / 状态机 | **全部章节** |
| 用量摄取 / 认证与会话（集成侧与平台侧）/ Webhook | §1–§7（**§6 不可省**） |
| 前端 / 文档 / CI / 脚本 | **不走本闸门**，直接开 PR |
| 既不碰钱、也不碰用量摄取 / 认证与会话 / Webhook 的后端改动 | **不走本闸门**，直接开 PR |

本任务档位：`全部`

理由：本设计给供应商价格版本（决定每个用量事件的成本价，并经 MARKUP 决定客户扣费）补数据库层的兜底：新迁移改 `provider_price_versions` 的 CHECK 与 BEFORE UPDATE 触发器，收紧价格版本状态机里绕过服务的写法。不改服务层、计费路径、接口与前端。

---

状态：`READY_FOR_REVIEW`
负责人：Claude
**设计版本**：`v5`（v1：形状核对只查子串 → v2 归一化后整段比对；v2：归一化删反斜杠会改变字面量含义 → v3 不做归一化，原文按固定写法全文匹配；v3：没核对同名 CHECK 是否生效 → v4 还要求在本表上且 `ENFORCED`；v4：沿用的相交判定用哨兵日期，边界上的区间漏判 → v5 改为按空值逐项判断，并把已发布版本尽头的改法限定为服务层的三种，见 §12）
对应需求：spec §14、§15.1、§17、§74.1；`REQ-PRICE-001`；不变量 INV-6；docs/TODO.md「待登记：供应商价格版本的数据库兜底：起点为空只许在该（供应商, 模型）没有区间非空的已发布版本时出现、改为 `RETIRED` 必须同时写 `effective_to`（026 记录的后续，新迁移，需设计闸门）」（Kelvin 2026-09-30 决定另开任务）；设计闸门 #177 v4（供应商价格，AIH-TASK-026，`docs/design/AIH-TASK-026-provider-prices.md`）
目标 PR：待开（登记 PR）；实现为 AIH-TASK-145

## 1. 目标与边界

- **要解决的问题**：AIH-TASK-026 的实现审查（#192）发现，供应商价格版本有两处规则只由服务层保证，绕过服务直接写库（手工 SQL、将来的脚本或同步任务的缺陷）可以破坏它们：
  1. **起点为空只该出现在时间线为空时**（设计 #177 v4 §2）：服务层只在该（供应商, 模型）没有任何区间非空的已发布 / 已退役版本时，才允许不指定生效时刻、得到「一直以来」的版本。迁移 0013 的触发器只查区间不重叠，所以直接写库可以在已有版本**之前**发布一个 `[NULL, E)` 的版本（`E` 不晚于最早那个版本的起点）。它从未存在过的「过去」被补上了价格：此前因无价而 `PRICING_ERROR` 的事件重新入队时会按它计费。这是 spec §17 与 Phase 2 计划 T-B 禁止的「回溯发布」。
  2. **退役必有尽头**：服务层的退役一律写 `effective_to`（正在生效的截断于 `t`，撤销的预约写成空区间）。直接写库可以把版本改为 `RETIRED` 而不写尽头：它照旧被取价（`resolve_provider_price` 按区间取，不看状态），而且它没有尽头，该（供应商, 模型）之后的每一次发布都撞上区间不重叠触发器。
  
  审查同类问题时还有第三处（本设计一并补上，理由见 §9 方案 C）：
  3. **已退役不是终态**：0013 的触发器允许改 `RETIRED` 行的 `effective_to`。服务层从不改已退役的行（任何操作都是 409 `PRICE_VERSION_FINAL`）。改短会让已取到这一价格的时刻失去价格，改长会把价格延伸进退役留下的无价空档。汇率（设计闸门 #183，迁移 0015）与税率（迁移 0022）的触发器都把 `RETIRED` 当终态，定价规则与本表还没有。
- **可观察的完成标准**：
  1. 在 MySQL 上绕过服务直接写库：草稿改为带尽头的 `PUBLISHED` 被拒绝；时间线上已有区间非空的已发布或已退役版本时，草稿改为起点为空的 `PUBLISHED` 被拒绝；`PUBLISHED` 改为 `RETIRED` 而不写 `effective_to` 被拒绝；对 `RETIRED` 行的任何改动都被拒绝。
  2. 不误杀：时间线为空（没有版本，或只有撤销预约留下的空区间）时，起点为空的发布照常通过；服务层的发布、截断、退役、退役后再发布、预约、撤销预约在真触发器下全部通过，结果与今天相同。
  3. 迁移不改任何已有行；可在线执行、可安全重跑；中途任何一步中断，库上的保护都不弱于迁移之前；可降级，降级不丢数据。
  4. `docs/TODO.md` Phase 1「`provider_price_versions` + 泛化价格分量」一项勾上（它只因这两个缺口没勾）。
- **明确不做什么**：
  - 不改服务层、仓储层、计费、试算、接口与前端：它们早已按这些规则写，本设计只让数据库也拒绝违反规则的写入。
  - 不改供应商价格分量表与它的三个触发器，不改 BEFORE INSERT（只能以草稿插入）与 BEFORE DELETE（一律拒绝）。
  - 不改定价规则：`pricing_rules` 的 `RETIRED` 同样可改尽头（0030 的触发器 ② 允许改 `PUBLISHED` / `RETIRED` 的 `effective_to`），是另一张表、另一份设计（#178 v4、#507 v4），记为后续计划另开（§10）。
  - 不检查历史：迁移不追溯判断已有行当初是否按规则发布（见 §10 假设 2 与 §8 的上线前核对）。
  - 不在数据库里兜底**与发布时刻 `t` 相关**的规则：起点不早于 `t`、截断与退役的时刻是 `t`、撤销预约只在起点晚于 `t` 时允许。触发器里取的「现在」与服务层在锁后取的 `t` 不是同一个时刻，判不准；这与定价规则（#178 v4 §10 假设 3：「≥ `t`」不由数据库兜底）、汇率的做法相同，仍由服务层保证。本设计兜底的都是不依赖时刻的结构性规则。
- **现有行为与问题证据**：
  - `alembic/versions/20260929_0013_provider_prices.py`：`trg_provider_price_versions_before_update` ① 只拒绝 `OLD.status = 'DISCARDED'`；② 对 `OLD.status IN ('PUBLISHED', 'RETIRED')` 只冻结 `effective_to`、`status`、`updated_at` 以外的列；③ 发布跃迁上只查分量完整，不查 `NEW.effective_to`；④ 区间不重叠。五条 CHECK 里没有「退役必有尽头」。
  - `app/services/provider_prices.py`：`_placement` 在没有末尾版本时才返回请求的起点（可为 `None`），`publish` 一律 `effective_to = None`；`retire` 一律写尽头；`truncate` 只作用于未截断的末尾版本，`restore_open` 只作用于 `PUBLISHED` 的前一个版本，没有任何路径改 `RETIRED` 行。
  - 同类兜底的先例：`ck_fx_rate_versions_retired_end`、`ck_pricing_rules_retired_end`、`ck_tax_policy_versions_retired_end` 都是 `status <> 'RETIRED' OR effective_to IS NOT NULL`；迁移 0015 的 `trg_fx_rate_versions_before_update` 在发布跃迁上要求 `NEW.effective_to IS NULL`、拒绝改 `RETIRED` 行。
  - 生产核对（2026-10-31，只读）：库在 `0030_pricing_rule_project_scope`；`provider_price_versions` 共 1 行，`PUBLISHED`、起点与尽头都为空；没有 `RETIRED` 行，没有带尽头的 `PUBLISHED` 行。

## 2. 设计概要

```text
任何 UPDATE provider_price_versions（服务层，或绕过服务的直接写库）
  → BEFORE UPDATE 触发器 trg_provider_price_versions_before_update_v2（本设计新建，替换 0013 的同类触发器）
      ① RETIRED / DISCARDED 是终态：任何改动都拒绝                     ← 收紧（原只拒绝 DISCARDED）
      ② 草稿只能改成 DRAFT / PUBLISHED / DISCARDED                     ← 与 0013 相同
      ③ DRAFT → PUBLISHED 时 NEW.effective_to 必须为空                  ← 新增
      ④ PUBLISHED 的行只许改 effective_to、PUBLISHED → RETIRED、updated_at ← 与 0013 相同（只是不再覆盖 RETIRED）
      ⑤ DRAFT → PUBLISHED 时分量完整                                   ← 与 0013 相同
      ⑥ 区间或状态有变化时：锁供应商行，再查非空区间不重叠              ← 与 0013 相同
  → CHECK ck_provider_price_versions_retired_end：status <> 'RETIRED' OR effective_to IS NOT NULL   ← 新增
  → 写入成功；用量计费读版本的方式、取价结果都不变
```

### 数据库（迁移 0031）

- **新 CHECK** `ck_provider_price_versions_retired_end`：`status <> 'RETIRED' OR effective_to IS NOT NULL`（与汇率、定价规则、税率三张表的同名 CHECK 逐字同一写法）。模型 `app/models/provider_prices.py` 新增常量 `RETIRED_END_CHECK` 与对应的 `CheckConstraint`，SQLite 用 `create_all` 建表时也有这一条。
- **新触发器** `trg_provider_price_versions_before_update_v2`（BEFORE UPDATE），正文由 0013 的 `trg_provider_price_versions_before_update` 改五处得到，其余逐字相同：
  1. 第一段 `IF OLD.status = 'DISCARDED'` 改为 `IF OLD.status IN ('RETIRED', 'DISCARDED')`，消息改为 `'a retired or discarded provider price version is final'`。
  2. 紧接「草稿只能改成 …」一段之后新增：`IF OLD.status = 'DRAFT' AND NEW.status = 'PUBLISHED' AND NEW.effective_to IS NOT NULL THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'a provider price version is published without an end'`（与 0015 的同一段同一写法）。
  3. 不可变检查一段的条件 `IF OLD.status IN ('PUBLISHED', 'RETIRED')` 改为 `IF OLD.status = 'PUBLISHED'`（`RETIRED` 已由第 1 处整体拒绝，这一段对它不再可达）。冻结的列与「只能退役」的检查不变。
  4. （v5）区间不重叠一段的**相交判定**不再用哨兵日期。0013 写的是 `COALESCE(o.effective_from, '1000-01-01') < COALESCE(NEW.effective_to, '9999-12-31') AND COALESCE(NEW.effective_from, '1000-01-01') < COALESCE(o.effective_to, '9999-12-31')`，而 `DATETIME` 的取值范围是 `'1000-01-01 00:00:00'` 到 `'9999-12-31 23:59:59'`，两个哨兵都不是严格的界：起点恰好是 `'9999-12-31 00:00:00'` 的已有版本会被判为与 `[NULL, NULL)` 不相交（`'9999-12-31 00:00:00' < '9999-12-31'` 为假）。（勘误：下界在 MySQL 8.4 上实测不漏判——`COALESCE` 的结果按字符串比较，`'1000-01-01' < '1000-01-01 00:00:00'` 成立；但这依赖类型推断，新判定不依赖它。）改为按空值逐项判断：`(o.effective_from IS NULL OR NEW.effective_to IS NULL OR o.effective_from < NEW.effective_to) AND (NEW.effective_from IS NULL OR o.effective_to IS NULL OR NEW.effective_from < o.effective_to)`。这就是半开区间 `[from, to)`（空端为无穷）相交的定义，对 `DATETIME` 的任何取值都成立；与服务层 `verify_periods` 按 `None` 判断的写法一致。旧判定成立时新判定一定成立（逐项比较：哨兵只会把「相交」误判成「不相交」，不会反过来），所以新判定只拒绝得更多。
  5. （v5）紧接不可变检查一段（第 3 处）之内、「只能退役」的检查之后新增：`OLD.status = 'PUBLISHED'` 时，若 `effective_to` 有变化，只允许「`OLD.effective_to IS NULL` 而 `NEW.effective_to` 非空」（截断，或退役时写尽头）或「`OLD.effective_to` 非空而 `NEW.effective_to IS NULL`、且状态不变」（恢复为未截断）；另外 `PUBLISHED → RETIRED` 只允许 `OLD.effective_to IS NULL`。否则 `SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'a published provider price version ends only by truncation, restore or retirement'`。这正是服务层仅有的三种改法（`truncate` 只作用于未截断的末尾版本，`restore_open` 只把被撤销预约截断的版本恢复为空，`retire` 只接受未截断的版本），所以不误杀；它堵住的是直接写库把**已截断**版本的尽头改成另一个值（改短让已取到价格的时刻失去价格，改长被区间不重叠挡住或延伸进空档）、或把已截断版本直接退役。
  
  分量完整一段一字不改；区间不重叠一段除第 4 处的相交判定外不改（触发条件、先锁 `ai_providers` 那一行再查、只看同一（供应商, 模型）里区间非空的 `PUBLISHED` / `RETIRED` 行、本行为空区间时不判定，都与 0013 相同），所以锁的顺序不变：先锁 `ai_providers` 的那一行（与服务层发布、退役同一把锁），再查相交。
- **为什么换名字、不同名重建**：MySQL 没有原子替换触发器的语句，同名重建只能先 `DROP` 再 `CREATE`，两句之间这张表没有 BEFORE UPDATE 保护。迁移 0030 为此加了两个只 SIGNAL 的隔离触发器。本设计改用「先建新名字的、再删旧的」：MySQL 8 允许同一张表同一时机有多个触发器，按 `ACTION_ORDER` 依次执行，任一 SIGNAL 都拒绝整条语句。新触发器在每一处都**不弱于**旧的（五处改动都只拒绝得更多），所以两个并存时的效果等于新触发器；只有旧的、两个都有、只有新的，这三种状态下保护都不弱于 0013，不存在没有保护的时刻，也不需要让整张表暂时不可写。（2026-10-31 在一次性 MySQL 8.4 容器里验证：两个 BEFORE UPDATE 触发器并存、后建的 `ACTION_ORDER` 为 2，任一拒绝都拒绝；删掉先建的后只剩后建的生效。）
- **新规则合起来的效果**：
  - 「起点为空只出现在时间线为空时」：发布跃迁上尽头必须为空（第 2 处），所以起点为空的发布一定是 `[NULL, NULL)`；按第 4 处的相交判定，它与任何非空区间都相交（两个合取项的第一个析取支都为真），⑥ 拒绝，不论已有区间落在 `DATETIME` 范围的哪一端。于是起点为空的发布只在该（供应商, 模型）没有任何区间非空的已发布 / 已退役版本时通过，与服务层 `_placement` 的条件（没有末尾版本 L；L 取自区间非空的版本）完全一致。发布后 `effective_from` 不可改（④；`RETIRED` 由 ①），所以这个性质之后也不会被破坏。
  - 「新发布的永远在末尾」：发布的区间都是 `[F, NULL)`，与任何起点不早于 F、或尽头晚于 F 的非空区间都相交，所以只能放在所有非空区间之后，不能插进中间或退役留下的空档。这与服务层的发布规则相同，也是起点为空规则的前提。
  - 「退役必有尽头」：新 CHECK。
  - 「已退役是终态」：①。
- 触发器正文、CHECK 条件文本与升级、降级用到的标记都写成模块级常量（照 0013 的 `_TRIGGERS` 写法），与模型逐字一致（空白除外），`tests/backend/test_migrations.py` 比对两边。0013 的正文在本迁移里另存一份冻结副本，供降级恢复，测试比对它与 0013 文件里的原文逐字一致。
- **升级（MySQL 在线）**，每一步都按库的现状判断、可安全重跑。先读三样：CHECK `ck_provider_price_versions_retired_end` 是否存在、它的 `CHECK_CLAUSE`；新触发器是否存在、它的 `ACTION_STATEMENT`；旧触发器是否存在。三项都已是目标状态（CHECK 存在且与目标逐字相同、新触发器存在且与目标逐字相同、旧触发器不存在；「逐字相同」按下面第 2 步的比较）→ 直接返回，不发任何语句。否则依次：
  1. **预检**：查 `@@log_bin` 与 `@@log_bin_trust_function_creators`（查询、判定与报错文案照 0013，改为本迁移的名字）。binlog 开着而开关关着时应用账号建不了触发器（ERROR 1419），在任何 DDL 之前抛错。
  2. **形状核对**（v3：不做任何归一化，原文全文匹配）：已存在的同名对象必须与本迁移要建的**逐字相同**，否则抛错、不发任何 DDL，消息写明哪一个对象不符、需要人对照本设计查看（只可能是手工建过同名对象）。
     - **CHECK**（v4 加前两项）：同名约束按 `CONSTRAINT_SCHEMA = DATABASE()` 与名字查 `information_schema.TABLE_CONSTRAINTS`，必须恰好一行，且 `CONSTRAINT_TYPE = 'CHECK'`、`TABLE_NAME = 'provider_price_versions'`、`ENFORCED = 'YES'`（MySQL 的 CHECK 名字在一个库里唯一，所以挂在别的表上的同名约束同样算「不符」；`NOT ENFORCED` 的约束不校验任何写入，条件写得再对也等于没有）。然后 `information_schema.CHECK_CONSTRAINTS.CHECK_CLAUSE` 的原文（不去反引号、不去反斜杠、不改大小写、不合并空白）必须**全文匹配**正则 ``^\(\(`status` <> _(utf8mb4|utf8mb3|latin1)\\'RETIRED\\'\) or \(`effective_to` is not null\)\)$``，即 MySQL 8.4 保存这一条件的规范写法 ``((`status` <> _utf8mb4\'RETIRED\') or (`effective_to` is not null))``，唯一可变的是字符集前缀，且只能是这三个之一（它取决于建约束时连接的字符集；三者对 ASCII 字面量 `RETIRED` 的含义相同，本迁移经应用的连接串建约束，得到的是 `_utf8mb4`）。正则写成模块级常量，用 `re.fullmatch` 判断。MySQL 按表达式树重写条件，所以任何多出、少了或换了的子表达式（例如多一个 `OR source_type = 'MANUAL'`）、两项顺序颠倒、字面量里多一个转义（例如 `'\rETIRED'`）、其他字符集前缀（例如 `_binary`），原文都不匹配。（2026-10-31 在一次性 MySQL 8.4 容器里验证：utf8mb4 连接下目标条件保存为上面的规范写法、latin1 连接下前缀是 `_latin1`；多一个 `OR` 的与两项顺序颠倒的保存为不同的原文。）顺序颠倒的条件逻辑上等价也拒绝：同名对象不是本迁移建的，宁可要人看一眼。
     - **新触发器**：`information_schema.TRIGGERS.ACTION_STATEMENT` 是 MySQL 保存的触发器正文原文（`FOR EACH ROW` 之后的部分，即本迁移发出的那段文字；0030 的 `_trigger_body` 依据同一点）。它去掉首尾空白后必须**等于**本迁移冻结的新触发器正文（`FOR EACH ROW` 之后的部分）去掉首尾空白，不做其他任何改写；另外 `ACTION_TIMING` 为 `BEFORE`、`EVENT_MANIPULATION` 为 `UPDATE`、`EVENT_OBJECT_TABLE` 为 `provider_price_versions`。降级时旧触发器与冻结的 0013 正文按同样方式比较。MySQL 的触发器没有「停用」的状态（与 CHECK 的 `NOT ENFORCED` 不同），存在即生效，所以比较表、时机、事件与正文就够。不比较 `DEFINER` 与 `SQL_MODE`：`DEFINER` 不对（账号不存在或缺权限）时触发器执行报错、整条 UPDATE 被拒，只会更严；正文里没有反斜杠、没有双引号、没有 `||`，不受 `NO_BACKSLASH_ESCAPES`、`ANSI_QUOTES`、`PIPES_AS_CONCAT` 等影响语法解析的 `sql_mode` 影响，比较的含义也不随 `sql_mode` 变（v5 起正文里没有日期字面量）。
     - 两个比较各写成一个函数，升级的「是否已完成」、本步与降级第 2 步共用。
  3. **数据核对**：CHECK 不存在时，查 `SELECT id FROM provider_price_versions WHERE status = 'RETIRED' AND effective_to IS NULL ORDER BY id LIMIT 20`。有行就抛错，不发任何 DDL；消息列出这些 `id`（内部主键，不是客户数据），并说明它们违反「退役必有尽头」、要人判断每一行应有的尽头后手工补上（`UPDATE … SET effective_to = …` 在 0013 的触发器下允许），再重新部署。迁移不替人补尽头：尽头决定哪些时刻有价，猜错就是计费错误。
  4. **加 CHECK**：CHECK 不存在时 `ALTER TABLE provider_price_versions ADD CONSTRAINT ck_provider_price_versions_retired_end CHECK (…) ENFORCED`（v4：显式写 `ENFORCED`，与默认相同，让语句本身说明意图）。MySQL 8 加 CHECK 时校验全部已有行，任一违反就整条失败（ERROR 3819）、表不变（同一次验证）。第 3 步与这一步之间若有人恰好写进一行违反的（只可能是绕过服务的直接写库），这一步失败、什么都没改，重跑时第 3 步列出它。
  5. **建新触发器**：新触发器不存在时 `CREATE TRIGGER trg_provider_price_versions_before_update_v2 …`。不带 `FOLLOWS` / `PRECEDES`：与旧的并存时排在它之后，两者的拒绝条件见上文，顺序不影响结果，只影响并存期间被两者都拒绝的写入得到哪一条消息。
  6. **删旧触发器**：旧触发器存在时 `DROP TRIGGER trg_provider_price_versions_before_update`。
  
  第 4 步在第 5 步之前，第 6 步一定在第 5 步之后：先有新的，才删旧的。
  - **离线模式（`--sql`）**：读不了库，照 0013 与 0030 的惯例跳过预检与全部判断，按「什么都没做过」依次输出第 4 步的 `ALTER`、第 5 步的 `CREATE TRIGGER`、`DROP TRIGGER IF EXISTS trg_provider_price_versions_before_update`。离线脚本没有第 3 步：执行脚本的人在已有违反行时会在 `ALTER` 上得到 ERROR 3819、后面不再执行（与在线时同样安全）。
- **降级**，同样按现状判断、可重跑。先读同样三样：CHECK 不存在、旧触发器存在且与冻结的 0013 正文逐字相同（比较同升级第 2 步）、新触发器不存在 → 直接返回。否则依次：
  1. 预检同升级第 1 步（降级要重建旧触发器）。
  2. 旧触发器存在但与冻结的 0013 正文不是逐字相同 → 抛错（手工建过同名对象），不发任何 DDL；新触发器存在但与本迁移的正文不是逐字相同 → 同样抛错（不删一个来历不明的触发器）。旧触发器不存在时，按冻结的 0013 正文 `CREATE TRIGGER trg_provider_price_versions_before_update`。
  3. 新触发器存在时 `DROP TRIGGER trg_provider_price_versions_before_update_v2`。
  4. CHECK 存在时 `ALTER TABLE provider_price_versions DROP CHECK ck_provider_price_versions_retired_end`。
  
  先建旧的、再删新的，理由同升级。降级只放宽约束、不改任何行，所以没有数据前提，也不需要拒绝降级的条件。
  - **离线模式（`--sql`）的降级**：按「升级已完成」依次输出第 2 步的 `CREATE TRIGGER`、`DROP TRIGGER IF EXISTS trg_provider_price_versions_before_update_v2`、第 4 步的 `ALTER`。降级只放宽，离线输出不会造成数据错误，所以不像 0030 那样拒绝离线降级。

### 时间、金额、币种

不涉及。本设计不改任何时间语义、金额计算或币种：`effective_from` / `effective_to` 的含义（`[F, T)`，UTC 整秒）、取价规则、换算都不变。

## 3. 不变量影响矩阵

| 不变量 | 是否触碰 | 可能怎样被破坏 | 设计控制 | 验证方式 |
| --- | --- | --- | --- | --- |
| INV-1 中心故障不中断 AI | 否 | 不涉及：只改供应商价格表上的约束，AI 调用路径不读它们 | — | — |
| INV-2 事件不重复扣费 | 否 | 认领与扣费不变 | — | 既有用例照跑 |
| INV-3 支付不重复入账 | 否 | 不涉及支付 | — | — |
| INV-4 余额只经账本变动 | 否 | 不涉及钱包 | — | — |
| INV-5 历史账本不可变 | 否 | 不写账本 | — | — |
| INV-6 事件保留版本引用 | **是** | ① 起点为空的版本被直接写进已有时间线之前，此前无价的时刻有了价格（回溯）；② 退役不写尽头，退役后仍被取价；③ 改已退役版本的尽头，已取到价格的时刻失去价格或空档被回填；④ 带尽头的发布插进时间线中间或空档；⑤ 迁移中途中断，触发器缺失、保护被绕过；⑥ 迁移改动已有行，已被用量事件引用的版本变了 | ① ③ 的 `[NULL, NULL)` 加 ⑥ 不重叠；② CHECK；③ 触发器 ①；④ 触发器 ③；⑤ 先建新、后删旧，每个中间状态都不弱于 0013；⑥ 迁移只加约束与触发器、不写任何行，`id` 不变 | §7 的直接写库用例、不误杀用例、迁移各用例 |
| INV-7 客户不可见成本毛利 | 否 | 不改任何接口或返回字段；拒绝的写入只发生在数据库层 | — | — |
| INV-8 租户不可互访 | 否 | 供应商价格是全局目录，没有租户列；不改任何查询 | — | — |
| INV-9 对话内容不入库 | 否 | 不涉及 | — | — |
| INV-10 金额用 Decimal | 否 | 不新增任何金额计算或列 | — | — |
| INV-11 event_id 至多一次财务效果 | 否 | 计费事务不变 | — | — |
| INV-12 定稿对账单不可变 | 否 | 对账单未实现 | — | — |
| INV-13 状态与事件原子提交 | 否 | 服务层的事务边界不变；被拒绝的直接写入整条回滚 | — | — |
| INV-14 队列丢失不毁持久工作 | 否 | 不涉及队列 | — | — |

## 4. 状态与并发

迁移 0031 之后、MySQL 上 `provider_price_versions` 的 UPDATE（SQLite 没有触发器，由服务层按同样的规则写；CHECK 两边都有）：

| 当前状态 | 事件（UPDATE） | 前置条件 | 新状态 | 副作用 | 非法时结果 |
| --- | --- | --- | --- | --- | --- |
| `DRAFT` | 改草稿内容 | — | `DRAFT` | — | — |
| `DRAFT` | 发布 | 分量完整；`effective_to` 为空（**新**）；与同一（供应商, 模型）的非空区间不相交 | `PUBLISHED` | 先锁供应商行 | SIGNAL 1644 |
| `DRAFT` | 丢弃 | — | `DISCARDED` | — | — |
| `DRAFT` | 直接改为 `RETIRED` | — | — | — | SIGNAL（与 0013 相同） |
| `PUBLISHED` | 截断 / 恢复未截断（改 `effective_to`） | 只许空 → 有值、或有值 → 空（**新**，v5）；不与非空区间相交 | `PUBLISHED` | 先锁供应商行 | SIGNAL |
| `PUBLISHED` | 退役 | 退役前未截断（**新**，v5）；`effective_to` 非空（**新**，CHECK）；不相交 | `RETIRED` | 先锁供应商行 | CHECK 3819 / SIGNAL |
| `PUBLISHED` | 改其他列、改成其他状态 | — | — | — | SIGNAL（与 0013 相同） |
| `RETIRED` | 任何改动 | — | — | — | SIGNAL（**新**：原来允许改 `effective_to`） |
| `DISCARDED` | 任何改动 | — | — | — | SIGNAL（与 0013 相同，消息改为新的） |

- **并发写入如何串行化**：不变。服务层的发布、退役先锁供应商行；触发器 ⑥ 锁同一行再查相交（0013 起如此）。新增的检查（③ 与 CHECK）只看本行，不需要锁；① 在锁之前就拒绝。
- **数据库负责的唯一性**：不变（`ux_provider_price_versions_open_slot`：一个（供应商, 模型）至多一个未截断的已发布版本）。
- **幂等键 / 相同 ID 不同载荷 / 重试**：不涉及。本设计不新增写操作；服务层的幂等（已发布再发布返回 200）不变。
- **原子提交**：被拒绝的语句整条不生效；服务层事务里任一语句被拒绝即整个事务回滚（与今天相同，服务层本来就不会写出被拒绝的语句）。
- **迁移与在线写入的并发**：
  - `ALTER TABLE … ADD CONSTRAINT … CHECK` 在 MySQL 8 上重建表（`ALGORITHM=COPY`）并持元数据锁：重建期间允许读，写入排队；计费事务持有的版本行共享锁（`FOR SHARE`）让 ALTER 在最后取排他元数据锁前等它们提交，进行期间新的计费在元数据锁上排队，不丢事件。表很小（生产 1 行），重建在毫秒到秒级。
  - `CREATE TRIGGER` / `DROP TRIGGER` 各取一次短暂的排他元数据锁，同上排队。
  - 没有任何时刻这张表缺 BEFORE UPDATE 保护（§2「为什么换名字」），所以迁移期间管理员的发布、退役照常可用，结果受的约束不弱于迁移前。

## 5. 失败与恢复矩阵

| 失败点 | 对外结果 | 数据最终状态 | 是否可重试 | 恢复来源 | 告警 |
| --- | --- | --- | --- | --- | --- |
| 校验失败（绕过服务的直接写库违反新规则） | 该语句报错（SIGNAL 1644 / CHECK 3819） | 该语句不生效；所在事务由调用者回滚 | 否（写法本身错误） | 改用服务层接口 | 无（直接写库的人看到报错） |
| DB 提交失败（服务层发布 / 退役） | 与今天相同：500，回滚 | 不变 | 是 | 管理员重试 | 与今天相同 |
| Redis/Celery 丢失 | 不涉及：迁移与触发器不经队列 | — | — | — | — |
| 外部服务超时 | 不涉及 | — | — | — | — |
| 重复请求 | 与今天相同（已发布再发布 200、已退役再退役 409） | 不变 | — | — | — |
| 迁移：预检失败 | 部署失败，旧镜像照旧运行 | 什么都没改，`alembic_version` 仍是 `0030_pricing_rule_project_scope` | 是 | 给账号打开 `log_bin_trust_function_creators` 后重新部署 | 部署工作流失败 |
| 迁移：形状核对不符（同名对象与目标不是逐字相同，含「看起来像、但逻辑更弱」的、`NOT ENFORCED` 的、挂在别的表上的） | 部署失败，消息写明哪个对象 | 什么都没改 | 否（要人） | 人对照本设计查看手工建过的同名对象，删掉或改正后重新部署 | 部署工作流失败 |
| 迁移：数据核对发现退役无尽头的行 | 部署失败，消息列出行 `id` | 什么都没改 | 补完尽头后是 | 人逐行判断应有的尽头、手工补上 | 部署工作流失败 |
| 迁移：加 CHECK 失败（3819，第 3 步之后才写进的违反行，或其他 DDL 错误） | 部署失败 | 表不变（InnoDB DDL 原子）；触发器仍是 0013 的 | 是 | 重新部署，第 3 步列出违反行 | 部署工作流失败 |
| 迁移：加了 CHECK、建新触发器之前中断 | 部署失败 | 有新 CHECK、只有旧触发器：不弱于 0013 | 是 | 重新部署：CHECK 存在跳过，建新触发器、删旧的 | 部署工作流失败 |
| 迁移：建了新触发器、删旧的之前中断 | 部署失败 | 两个触发器并存：效果等于新的 | 是 | 重新部署：只删旧的 | 部署工作流失败 |
| 迁移：全部完成、写 `alembic_version` 之前退出 | 部署失败 | 已是目标状态 | 是 | 重新部署：判为已完成，不发任何语句，只前进版本 | 部署工作流失败 |
| 降级中途中断（任一步） | 降级命令失败 | 每个中间状态都不弱于 0013（先建旧、再删新、最后删 CHECK） | 是 | 重跑降级 | — |

## 6. 数据与安全边界

- **租户过滤**：不涉及。供应商价格是全局目录，没有租户列；本设计不新增、不改任何查询（迁移的数据核对只读本表的 `id`、`status`、`effective_to`）。
- **鉴权主体及权限**：不变。管理端价格接口仍只对 `ADMIN` 开放；迁移由部署时的迁移账号执行，与 0013、0030 相同（建触发器要求 binlog 预检）。
- **客户侧禁止返回的内部字段**：不变。不改任何接口；成本价从不对客户返回。
- **日志、审计、异常**：迁移的报错消息只含表名、约束 / 触发器名与违反行的内部 `id`，不含价格、来源说明或用户信息。触发器的 SIGNAL 消息是固定英文文本，不含行内容。服务层审计不变（不新增审计动作：被数据库拒绝的直接写库本来就不经服务、不产生审计）。
- **密钥**：不涉及。
- **prompt / response 或客户数据**：不涉及。
- **数据保留及删除**：不变。版本仍一律不许删除。

## 7. 测试证据计划

MySQL 用例只在 `BILLING_TEST_DATABASE_URL` 的真 MySQL 上跑（未设置时 skip、CI 必跑），写法照 `tests/backend/test_pricing_rule_project_scope_migration.py`。中断用 `before_cursor_execute` 监听在某一步要发的语句之前抛错来制造。每个迁移用例开始与结束时把库带回 head，`finally` 里断言：两张价格表为空（`TRUNCATE`，关外键检查；不删、不重建任何触发器），触发器清单恰好是 head 的那一份，CHECK 存在。

| 风险/需求 | 测试层级 | 场景 | 预期结果 |
| --- | --- | --- | --- |
| 正常路径：迁移结构 | integration（MySQL） | 升级到 head 后读 `information_schema` | `ck_provider_price_versions_retired_end` 存在、`CHECK_CLAUSE` 含 `'RETIRED'` 与 `effective_to`；本表的触发器恰好是 BEFORE INSERT、`…_before_update_v2`、BEFORE DELETE 三个，旧的 BEFORE UPDATE 不存在 |
| 正常路径：字面量 | unit | 迁移的 CHECK 常量 vs 模型 `RETIRED_END_CHECK`；新触发器正文 vs 0013 原文；冻结的 0013 副本 vs 0013 文件 | 前两者逐字一致（空白除外）；新正文与 0013 原文的差异恰好是 §2 的五处（逐行比对，其余行相同）；新正文里没有 `'1000-01-01'` 与 `'9999-12-31'`；副本与 0013 逐字一致 |
| 正常路径：离线 | integration（MySQL 的 alembic 配置） | `--sql` 升级与降级 | 升级依次输出 `ALTER … ADD CONSTRAINT`、`CREATE TRIGGER …_v2`、`DROP TRIGGER IF EXISTS trg_provider_price_versions_before_update`，不读库；降级依次输出 `CREATE TRIGGER trg_provider_price_versions_before_update`、`DROP TRIGGER IF EXISTS …_v2`、`ALTER … DROP CHECK` |
| 正常路径：不改已有行 | integration（MySQL） | 降到 0030，经服务层造出：未截断的已发布、被截断的已发布、带尽头的已退役、撤销预约留下的空区间、草稿、丢弃的草稿（含分量）；记下两张价格表全部行与列；升级到 head | 两张表逐行逐列不变 |
| 不误杀：服务路径 | integration（MySQL） | 既有 `test_the_service_paths_pass_the_triggers`（发布、截断、退役、退役后再发布、预约、撤销预约、恢复前一个） | 在 head 上照旧全部通过、断言不改 |
| 起点为空（回溯） | integration（MySQL） | 绕过服务：已有一个区间非空的版本（未截断的已发布；被截断的已发布；带尽头的已退役，各一次），再把一个完整的草稿直接改为 `PUBLISHED`、起点为空、尽头为空；同上但尽头为最早版本的起点（`[NULL, E)`） | 前三次 SIGNAL「provider price periods overlap」；`[NULL, E)` 的 SIGNAL「published without an end」；库不变 |
| 相交判定的边界（v5） | integration（MySQL） | 绕过服务：① 已有版本 `['9999-12-31 00:00:00', '9999-12-31 00:00:01')`（先发布为 `['9999-12-31 00:00:00', NULL)` 再截断），把完整草稿直接改为 `[NULL, NULL)` 的 `PUBLISHED`；② 已有版本 `[NULL, '1000-01-01 00:00:00')`（空时间线上先发布为 `[NULL, NULL)` 再截断），同样再发布 `[NULL, NULL)`；③ 已有版本 `['9999-12-31 23:59:58', NULL)`，再发布 `['9999-12-31 23:59:59', NULL)` 而不先截断；④ 对照：① 的已有版本之后发布 `['9999-12-31 00:00:01', NULL)` | ①②③ SIGNAL「provider price periods overlap」（勘误：① 在 0013 的判定下会被放行；② 不会——`COALESCE` 的结果按字符串比较，`'1000-01-01' < '1000-01-01 00:00:00'` 成立——留作下界的回归用例；用例注释照此写明）；④ 通过（首尾相接） |
| 起点为空不误杀 | integration（MySQL） | 绕过服务：该（供应商, 模型）没有任何版本；只有一个撤销预约留下的空区间（经「发布 `[F, NULL)` → 改为 `RETIRED`、`effective_to = effective_from`」得到）；另一个模型已有版本 | 三次都通过 |
| 带尽头的发布（插空档） | integration（MySQL） | 绕过服务：草稿直接改为 `PUBLISHED`，区间分别是：落在已退役版本留下的空档里的 `[S, E)`、整段在最早版本之前的 `[S, E)`、在末尾之后的 `[S, E)` | 全部 SIGNAL「published without an end」 |
| 退役必有尽头 | integration（MySQL）+ unit（SQLite） | 绕过服务：`PUBLISHED` 改为 `RETIRED`、不写 `effective_to`（未截断的与被截断后又把尽头清空的，各一次）；同样改为 `RETIRED` 并写尽头 | 前两次 ERROR 3819、约束名 `ck_provider_price_versions_retired_end`；写尽头的通过。SQLite（`create_all`）上 ORM 直接把一行改为 `RETIRED` 而尽头为空：`IntegrityError`，写尽头的通过 |
| 已发布版本的尽头只按三种方式变（v5） | integration（MySQL） | 绕过服务：已截断的 `PUBLISHED` 行把尽头改短、改长到仍不与后继相交的值（后继是撤销预约留下的空区间时）；已截断的 `PUBLISHED` 行直接改为 `RETIRED`（尽头不变、改短各一次）；对照：未截断的截断、被截断的恢复为空、未截断的退役 | 前四次 SIGNAL「a published provider price version ends only by truncation, restore or retirement」；对照三次通过 |
| 已退役是终态 | integration（MySQL） | 绕过服务：对带尽头的 `RETIRED` 行分别改短尽头、改长尽头、清空尽头、只改 `updated_at`、改回 `PUBLISHED`；对撤销预约留下的空区间行改尽头 | 全部 SIGNAL「a retired or discarded provider price version is final」；库不变 |
| 已丢弃是终态、已退役不能改回 | integration（MySQL） | 既有用例 `test_only_truncation_and_retirement_pass_on_a_published_version`：改丢弃的草稿；把已退役的版本改回 `PUBLISHED` | 两处都 SIGNAL，断言都改为「a retired or discarded provider price version is final」（后一处原来是「can only be retired」：① 现在先于不可变检查拒绝 `RETIRED` 行） |
| 既有直接写库用例的前置 | integration（MySQL） | `test_provider_prices_service.py` 里经 `_publish_directly` 直接发布**带尽头**的版本来造前置状态的用例（区间不重叠、空区间与其他模型等） | 改为先直接发布为 `[F, NULL)`、再用第二条 UPDATE 写尽头（与服务层「先发布、后截断」同一路径）；其余结论只有一处改变。`test_the_database_refuses_overlapping_periods` 的候选同样改为先发布为 `[F, NULL)`、再截断：原本被拒的五个参数（partial、contained、same-start、from-the-beginning、open-over-it）在发布那一步就被重叠触发器拒绝，消息不变；`end-to-end-after`（`[T0+10s, T0+20s)`）与 `open-after` 仍通过；**`end-to-end-before`（`[NULL, T0)`，排在已有版本之前）由通过改为拒绝**：它只能先发布为 `[NULL, NULL)`，与已有的 `[T0, T0+10s)` 相交，被重叠触发器拒绝；直接写带尽头的 `[NULL, T0)` 则得到「published without an end」。这正是 §1 第 1 处要堵的回溯发布，用例改为断言拒绝，并在注释里写明原因 |
| 并发 | integration（MySQL） | 既有的发布 / 退役并发用例 | 照旧通过（锁顺序不变） |
| 迁移：中断后重跑 | integration（MySQL） | 升级在三个位置中断：发 `CREATE TRIGGER …_v2` 之前（已有 CHECK）；发 `DROP TRIGGER` 之前（两个触发器并存）；写 `alembic_version` 之前（已完成）；各自重跑升级 | 每次重跑只发剩下的语句（第三种一条 DDL 都不发），最终结构同 head |
| 迁移：中间状态不弱于 0013 | integration（MySQL） | 在上面前两个中断位置，逐条执行 0013 时代就被拒绝的直接写库（改已发布版本的币种、已退役改回已发布、草稿直接改为 `RETIRED`、相交的区间）与本设计新拒绝的（带尽头的发布、退役不写尽头） | 前一组在两个位置都被拒绝；后一组在「两个触发器并存」位置被拒绝，「只有 CHECK」位置的退役不写尽头被拒绝 |
| 迁移：降级中断后重跑 | integration（MySQL） | 降级在两个位置中断：发 `DROP TRIGGER …_v2` 之前（两个并存）；发 `DROP CHECK` 之前；各自重跑降级 | 只发剩下的语句，最终结构同 0030 |
| 迁移：降级恢复 0013 | integration（MySQL） | 降到 0030 | 本表的 BEFORE UPDATE 只有 `trg_provider_price_versions_before_update`，`ACTION_STATEMENT` 与 0013 原文的正文一致（空白除外）；CHECK 不存在；0013 时代允许的直接写（`PUBLISHED` 改为 `RETIRED` 不写尽头）通过——证明真的恢复了 |
| 迁移：数据核对 | integration（MySQL） | 降到 0030，经直接写库造一行退役无尽头的版本，升级 | 抛错，消息含该行 `id`；没有发任何 DDL（CHECK 不存在、触发器仍只有旧的）、`alembic_version` 仍是 0030；补上尽头后再升级成功 |
| 迁移：形状核对 | integration（MySQL）+ unit | 降到 0030 后手工建，各自升级一次：① 同名 CHECK 而条件是 `status <> 'RETIRED' OR effective_to IS NOT NULL OR source_type = 'MANUAL'`（含目标的全部文字、逻辑更弱）；② 同名 CHECK 而条件是 `effective_to IS NOT NULL OR status <> 'RETIRED'`（同样的两项、顺序不同）；③ 同名 CHECK 而条件是 `status <> '\rETIRED' OR effective_to IS NOT NULL`（字面量里多一个转义）；⑧ 同名 CHECK、条件与目标相同，但 `NOT ENFORCED`（v4）；⑨ 本表上没有、但同一个库里另一张临时表上有同名 CHECK（条件与目标相同、`ENFORCED`，v4）；④ 同名新触发器，正文是目标正文少一段检查；⑤ 同名新触发器，正文与目标相同但多一条 SIGNAL；⑥ 同名新触发器，正文与目标只差一处字面量里的空格数。降级方向：⑦ 新触发器在、旧触发器是手工建的另一份正文。unit：正则对 `_utf8mb4`、`_utf8mb3`、`_latin1` 三种前缀的规范写法都全文匹配；对 `_binary` 前缀、对 ① ② ③ 在 MySQL 上实际保存的原文（用例里写成字面量，与 integration 用例读到的比对一致）、对规范写法前后多一个空格都不匹配 | ①–⑨ 都抛错，消息写明哪一个对象不符，没有发任何 DDL（`information_schema` 前后相同）；unit 各项如所列。另外「迁移结构」一行在 head 上断言：本表上这条约束 `ENFORCED = 'YES'`、实际的 `CHECK_CLAUSE` 全文匹配正则、新触发器的 `ACTION_STATEMENT` 去掉首尾空白后等于冻结正文（证明比较的就是 MySQL 实际保存的） |
| 迁移：预检 | unit | `@@log_bin` / `@@log_bin_trust_function_creators` 四种组合 | 只有「开着而开关关着」拒绝；在任何 DDL 之前 |
| 迁移：降级不需要数据前提 | integration（MySQL） | 库里有各种状态的版本时降级 | 通过、行不变 |
| Redis/Celery 丢失 | — | 不涉及：本设计不经队列 | — |
| 租户越权 | — | 不涉及：供应商价格是全局目录、没有租户列 | — |
| 金额精度 | — | 不涉及：不新增任何金额计算 | — |
| 幂等重放 | — | 不涉及：不新增写操作；服务层的幂等用例照跑 | — |

**0030 的迁移测试**：0031 成为 head 之后，`tests/backend/test_pricing_rule_project_scope_migration.py` 里从 head 降级到 0029 的用例会先执行 0031 的降级，其中断言「降级的预检报错文案」与「降级不发任何 DDL」的两个（`test_0030_precheck_refuses_before_any_ddl[downgrade]`、`test_0030_downgrade_refuses_with_a_project_rule`）因此失败。这两个用例（以及同一模块里同样从 head 出发、断言 0030 降级发出的语句或报错的用例）改为先把库降到 `0030_pricing_rule_project_scope`（在被观察的部分之外），再执行它们原本的 0030 降级与断言；`finally` 照旧回到 head。断言的内容不变。

`tests/backend/test_migrations.py` 里在 head 上断言本表形状的用例（CHECK 清单与条数、触发器清单、模型常量与迁移字面量的比对）改为 0031 之后的形状；只在 0013 那一版上验证 0013 自身的用例照旧。

## 8. 迁移与上线

- **数据迁移步骤**：无。不改、不回填、不删除任何行；只加一条 CHECK、建一个触发器、删一个触发器（§2）。
- **锁表与性能影响**：见 §4「迁移与在线写入的并发」。表很小（生产 1 行），秒级以内；计费只在元数据锁上短暂排队，不丢事件。
- **兼容窗口**：新约束只拒绝服务层从不写出的状态，所以迁移之后、新镜像起来之前的旧代码照常运行；新代码相对旧代码只多了模型上的 CHECK 声明（SQLite 建表用），行为相同。
- **部署顺序**：`deploy.sh` 先迁移、再换镜像，与以往相同。**上线前核对**（运维步骤，只读）：在生产库上跑 §2 升级第 3 步的同一条查询，并核对没有「起点为空、但同一（供应商, 模型）里有更早发布的区间非空版本」的行。2026-10-31 已跑一次：1 个版本，`PUBLISHED`、起点尽头都为空，两项都为 0。合并前若生产上新录过价格，再跑一次。
- **回滚或前滚方式**：可降级（§2「降级」），降级不丢数据、不需要数据前提；代码回滚到本迁移之前要先降级（模型的 CHECK 声明与库一致即可，旧代码不依赖它）。
- **部分部署时的行为**：迁移中途失败的每个状态都不弱于 0013（§5），重新部署补完；不存在「表暂时不可写」的状态。
- **监控指标和告警负责人**：不新增。迁移失败即部署工作流失败（现有通知）；被拒绝的直接写库只有执行它的人看得到，服务层路径不会触发。

## 9. 方案取舍

| 方案 | 优点 | 风险 | 未采用原因 |
| --- | --- | --- | --- |
| A（采用）：新 CHECK + 新名字的触发器，先建新、后删旧 | 每个中间状态都不弱于 0013，没有无保护的时刻，也没有整表不可写的时刻；每一步按「对象是否存在」判断，重跑简单；步骤少（加 CHECK、建、删） | 触发器名字永久带 `_v2` 后缀，与其他表的命名不一致；文档与测试里的触发器清单要同步 | — |
| B：同名重建，照 0030 加隔离触发器 | 名字不变 | 步骤多一倍（建两个隔离触发器、`PRECEDES` 排序、解除隔离），中途失败时整张表不可写、要靠重跑解除；0030 需要隔离，是因为它的「旧触发器 + 新表」中间状态会让区间检查**算错**范围键，而本设计的中间状态都更严，不需要隔离 | 收益只有名字，代价是更多的失败状态与测试面 |
| C：只补 TODO 记录的两处，`RETIRED` 仍可改尽头 | 严格按登记的范围 | 留下第三处同类缺口：改短已退役版本的尽头让已取到价格的时刻失去价格，改长把价格延伸进退役空档（INV-6）；汇率设计 #183 v1 的登记前预审正是把这一处判为缺陷（docs/design/AIH-TASK-028-fx-rates.md §12「v2 的触发」第 3、4 条） | 服务层从不改已退役的行，收紧没有任何成本；同一个触发器本来就要重建 |
| D：起点为空另写一段 EXISTS 检查（照定价规则 0014 v4 的 ⑤） | 规则写得直白 | 定价规则的条件是「该范围出现过任何已发布 / 已退役行（含空区间）」，比供应商价格服务层的条件（没有区间非空的版本）更严：只有撤销预约留下的空区间时，服务层允许起点为空，照抄会误杀服务层的正常发布 | 「发布时尽头必须为空 + 区间不重叠」恰好等价于服务层的条件（§2「新规则合起来的效果」），而且同时堵住了带尽头的发布插空档 |
| E：迁移替人补退役无尽头的行（例如补成起点或 `NOW()`） | 部署不会被这种行卡住 | 尽头决定哪些时刻有价，猜错就是计费错误；生产上没有这种行 | 拒绝并列出行 `id`，由人判断（§2 升级第 3 步） |

## 10. 未决问题与假设

- **未决问题**：无。
- **尚未验证的假设**：
  1. 生产库在部署时仍满足 CHECK。依据：2026-10-31 只读核对为 0 行；服务层从不写出这种行。若不成立：部署在第 3 步失败、什么都不改，按消息补尽头后重新部署。
  2. 已有行当初都按「起点为空只在时间线为空时」发布。迁移不追溯检查（只有发布时刻能判断，而 `approved_at` 是整秒、同一秒的两次发布分不出先后）。依据：生产只有 1 个版本，`§8` 的上线前核对覆盖它；服务层的发布事务里本来就有 `verify_periods` 复查，之后对同一（供应商, 模型）的任何发布都会在这种时间线上抛 `PricePeriodsBroken`。若不成立：那一行的回溯价格仍在，新约束只阻止今后再写出同类的行。已有行之间因哨兵日期漏判而相交的情况同理（只有区间端点落在 `'1000-01-01 00:00:00'` 或 `'9999-12-31'` 当天才可能，生产 1 个版本，不存在）。
- **需要谁拍板**：无。范围比 TODO 记录多出的第三处（已退役是终态）是同类缺口、零成本，写在 §1 与 §9 方案 C；若 Kelvin 要求严格按记录的两处，删掉 §2 第 1 处改动与对应用例即可，不影响其余部分。
- **后续**（不在本设计内）：① `pricing_rules` 的 `RETIRED` 同样可改尽头，与汇率、税率、本设计之后的供应商价格不一致；② 定价规则（0014 / 0030）、汇率（0015）、税率（0022）的区间不重叠触发器用的是同样的哨兵日期，有同样的边界漏判。登记 PR 在「后续计划」加一项，另过设计闸门。

## 11. 审查与版本绑定

审查方要回答的五个问题常驻在 [scripts/review_checklist.md](../../scripts/review_checklist.md) 的「设计审查」一节，此处不重复。

**版本绑定规则**（规则的出处是 [WORKFLOW §3](../../docs/WORKFLOW.md)；这里保留一份是因为审查材料里只有本 Issue 正文，两处改一起改）：

- Codex 的批准必须写成 `APPROVED: design v<N>`，`<N>` 取本 Issue 顶部的「设计版本」
- **设计版本一变，之前的 APPROVE 自动作废**，必须重新过闸门
- 什么算「实质修改」：改了契约、表结构、事务边界、状态机、失败语义、不变量控制。改错别字不算
- 实质修改时：把顶部版本 +1，在下方追加一条变更说明，状态退回 `READY_FOR_REVIEW`

### 任务拆分

一个实现任务：`AIH-TASK-145`（迁移 0031、模型的 CHECK 声明、测试与文档）。不再拆：只有数据库一层，服务层、接口、前端都不改；预计改动约 950 行（迁移约 250 行，其中触发器正文约 110 行；新测试模块约 500 行；既有用例的前置改写与新增约 200 行），可改路径 8 个。

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
| v1 | 2026-10-31 | 初稿 | — |
| v2 | 2026-10-31 | §2 升级第 2 步的形状核对由「查两段子串」改为整段比对：CHECK 归一化后必须等于 MySQL 的规范写法，触发器正文归一化空白后必须等于冻结正文；「是否已完成」与降级第 2 步共用同样的比较，降级在新触发器来历不明时也拒绝；§5、§7 相应改写，§7 加四种「看起来像、但不一样」的同名对象与归一化的单元用例 | Codex 设计审查（v1）阻断：同名但逻辑更弱的 CHECK（例如多一个 `OR`）含全部标记，会被当成目标状态放行，「退役必有尽头」失去数据库兜底 |
| v3 | 2026-10-31 | §2 升级第 2 步去掉归一化：CHECK 的原文用全文正则匹配 MySQL 规范写法，唯一可变的是字符集前缀且限三种；触发器正文只去首尾空白后逐字相等；§7 加「字面量多一个转义」「字面量空格数不同」「`_binary` 前缀」与对应单元用例 | Codex 设计审查（v2）阻断：归一化无条件删反斜杠会改变字面量含义，`'\rETIRED'` 这样的弱约束归一化后与目标相同 |
| v4 | 2026-10-31 | §2 升级第 2 步的 CHECK 核对加两项：同名约束必须在本表上、且 `ENFORCED = 'YES'`；第 4 步的 `ALTER` 显式写 `ENFORCED`；写明触发器没有停用状态、为什么不比较 `DEFINER` 与 `SQL_MODE`；§5、§7 相应补 `NOT ENFORCED` 与「挂在别的表上」两种同名约束 | Codex 设计审查（v3）阻断：条件相同但 `NOT ENFORCED` 的同名 CHECK 会被当成目标约束，迁移成功后仍可写入无尽头的退役版本 |
| v5 | 2026-10-31 | §2 新触发器加两处改动：第 4 处，区间不重叠的相交判定由哨兵日期 `COALESCE(…, '1000-01-01' / '9999-12-31')` 改为按空值逐项判断；第 5 处（送审前自查补上），已发布版本的尽头只按截断、恢复、退役三种方式变，已截断的不能直接退役；「为什么换名字」与 §7 字面量一行相应改为五处；§4 状态表补前置条件；§7 加相交判定的四个边界用例与尽头改法的用例；§1 写明与 `t` 相关的规则不由数据库兜底；§10 补已有行的说明与其他三张表的后续 | Codex 设计审查（v4）阻断：已有版本起点为 `'9999-12-31 00:00:00'` 时，`[NULL, NULL)` 的发布被哨兵判定为不相交，起点为空的规则可被绕过 |
| v5（勘误） | 2026-11-01 | §2 第 4 处与 §7「相交判定的边界」一行：更正「下界同样漏判」的说法。只有上界（已有版本起点为 `'9999-12-31 00:00:00'`）在 0013 下会被放行；下界实测不漏判（`COALESCE` 按字符串比较），用例 ② 留作回归用例。设计规则、预期结果与用例不变，版本仍是 v5 | 批准后的最终登记前预审发现；2026-11-01 在一次性 MySQL 8.4 容器里实测：用 0013 的判定，① 判为不相交、② 判为相交 |

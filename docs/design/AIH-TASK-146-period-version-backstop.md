# AIH-TASK-146..148 设计：定价规则、汇率、税率版本的数据库兜底（已批准 v1）

> **来源**：设计闸门 Issue #539。本文件是 `APPROVED: design v1` 那一版正文的**逐字副本**（审查：Codex 第一轮批准，无阻断项）；顶部「状态」一行在批准后由 `READY_FOR_REVIEW` 改为 `APPROVED`。送审前的登记前预审发现与处理见 §11「任务拆分」。
> 放进仓库，是因为 OpenClaw Worker 在沙箱里不联网、读不到 GitHub Issue。
> **实现以本文件为准**；与 Issue 不一致时，以 Issue #539 上被批准的 v1 为准。设计要改，就回到 Issue 升版本、重新过闸门，不要直接改本文件。
> **前置**：供应商价格的同类兜底见设计闸门 #535 v5（[AIH-TASK-145-provider-price-backstop.md](AIH-TASK-145-provider-price-backstop.md)）；迁移 0032 接在 AIH-TASK-145 的 0031 之后，145 的契约见 PR #538 的修正。
> §11「任务拆分」的三个实现任务登记为 `AIH-TASK-146`、`AIH-TASK-147`、`AIH-TASK-148`。

---
## 0. 准入判定

| 任务涉及 | 要填的范围 |
| --- | --- |
| 钱包 / 账本 / 定价 / 汇率 / 支付 / 幂等 / 状态机 | **全部章节** |
| 用量摄取 / 认证与会话（集成侧与平台侧）/ Webhook | §1–§7（**§6 不可省**） |
| 前端 / 文档 / CI / 脚本 | **不走本闸门**，直接开 PR |
| 既不碰钱、也不碰用量摄取 / 认证与会话 / Webhook 的后端改动 | **不走本闸门**，直接开 PR |

本任务档位：`全部`

理由：本设计给定价规则、汇率、税率三张版本表补数据库层的兜底：三个新迁移各重建一张表的 BEFORE UPDATE 触发器，收紧版本状态机里绕过服务的写法。定价规则决定每个用量事件的计费额，汇率决定外币成本的换算，税率被支付、收据、对账单快照引用。不改服务层、计费路径、接口与前端。

---

状态：`APPROVED`（`APPROVED: design v1`，Codex，2026-11-01）
负责人：Claude
**设计版本**：`v1`
对应需求：spec §15.1、§16、§17、§45、§74.1、§74.9；`REQ-PRICE-001`；不变量 INV-6；docs/TODO.md「待登记：定价规则的已停用（`RETIRED`）行仍可改尽头，收紧为终态；定价规则、汇率、税率的区间不重叠触发器改用不带哨兵日期的相交判定（与供应商价格 AIH-TASK-145 一致；新迁移，需设计闸门；见设计闸门 #535 §10）」；设计闸门 #535 v5（供应商价格的同类兜底，`docs/design/AIH-TASK-145-provider-price-backstop.md`）；#178 v4 / #507 v4（定价规则）、#183 v3（汇率）、AIH-TASK-077 的税务政策设计
目标 PR：待开（登记 PR）；实现为 AIH-TASK-146（定价规则）、AIH-TASK-147（汇率）、AIH-TASK-148（税率）

## 1. 目标与边界

- **要解决的问题**：设计闸门 #535 给供应商价格版本补了数据库兜底（AIH-TASK-145），审查过程中发现另外三张同构的版本表有同类缺口，只由服务层保证，绕过服务直接写库（手工 SQL、将来的脚本或同步任务的缺陷）可以破坏它们：

  | 缺口 | 定价规则（`pricing_rules`，迁移 0030 的触发器） | 汇率（`fx_rate_versions`，0015） | 税率（`tax_policy_versions`，0022） |
  | --- | --- | --- | --- |
  | ① 已停用不是终态：可以改 `RETIRED` 行的 `effective_to` | **有** | 已堵（0015 v2） | 已堵（0022） |
  | ② 发布时可以带尽头：草稿直接改为带 `effective_to` 的 `PUBLISHED` | **有** | 已堵 | 已堵 |
  | ③ 区间相交判定用哨兵日期，边界上漏判 | **有** | **有** | **有** |
  | ④ 已发布版本的尽头可以从一个值直接改成另一个值，已截断的可以直接停用 | **有** | **有** | **有** |

  - **①**：0030 的 `trg_pricing_rules_before_update` 只拒绝 `OLD.status = 'DISCARDED'`，对 `PUBLISHED` / `RETIRED` 冻结的列不含 `effective_to`。服务层从不改已停用的规则（任何操作都是 409 `PRICING_RULE_FINAL`）。改短让已命中这条规则的时刻失去规则（事件改落到更低一级或 `PRICING_ERROR`），改长把规则延伸进停用留下的空档，清空尽头则被 CHECK 拒绝。
  - **②**：0030 的发布跃迁上不查 `NEW.effective_to`。直接写库可以把 `[S, E)` 的规则发布进停用留下的空档，或整段放在该范围最早一条规则之前（`S`、`E` 都非空，`E` 不晚于它的起点）。后者就是服务层禁止的回溯：服务层的新规则起点永远 ≥ `t`（`app/services/pricing_rules.py` 模块文档「按范围只在末尾追加」），0030 的 ⑤ 只拦起点为空的那一种。补在过去的规则会让已经按更低一级计过费的时刻改选这一级，重新入队的事件按它计费（#178 v4 §2「回溯生效只允许全局默认的第一条」）。
  - **③**：三个触发器的区间不重叠一段都写成 `COALESCE(o.effective_from, '1000-01-01') < COALESCE(NEW.effective_to, '9999-12-31') AND COALESCE(NEW.effective_from, '1000-01-01') < COALESCE(o.effective_to, '9999-12-31')`。`DATETIME` 的上界是 `'9999-12-31 23:59:59'`，`'9999-12-31'` 不是严格的界：已有区间的起点恰好是 `'9999-12-31 00:00:00'` 时，`COALESCE` 的结果按字符串比较，`'9999-12-31 00:00:00' < '9999-12-31'` 为假，任何尽头为空的新区间都被判为与它不相交，于是能发布到它之前（设计闸门 #535 v4 审查发现，v5 勘误在一次性 MySQL 8.4 容器里实测：上界漏判、下界不漏判）。
  - **④**：三个触发器都允许 `PUBLISHED` 行的 `effective_to` 任意改（只受区间不重叠与 CHECK 约束），也允许已截断的版本直接改为 `RETIRED`。服务层只有三种改法：截断（未截断的末尾版本 `NULL → t`）、恢复（被撤销预约截断的前一个版本 `T → NULL`，状态不变）、停用（只接受未截断的版本，`NULL → t` 同时改状态）。直接写库把已截断版本的尽头改短，会让已取到它的时刻失去它；改长被区间不重叠挡住，或在后继是撤销预约留下的空区间时延伸进去。
- **可观察的完成标准**：
  1. 在 MySQL 上绕过服务直接写库：定价规则的 `RETIRED` 行任何改动被拒绝；定价规则的草稿改为带尽头的 `PUBLISHED` 被拒绝；三张表上，已有区间起点为 `'9999-12-31 00:00:00'` 时把草稿发布到它之前被拒绝；已截断的 `PUBLISHED` 行把尽头改成另一个非空值、或直接改为 `RETIRED` 被拒绝。
  2. 不误杀：三张表的服务层写法（发布、截断、停用、停用后再发布、预约、撤销预约、恢复前一个）在真触发器下全部通过，结果与今天相同；首尾相接的发布照常通过。
  3. 每个迁移不改任何已有行；可在线执行、可安全重跑；中途任何一步中断，库上的保护都不弱于迁移之前；可降级，降级不丢数据。
- **明确不做什么**：
  - 不改服务层、仓储层、计费、试算、接口、前端与模型：它们早已按这些规则写；三张表的「停用必有尽头」CHECK（`ck_pricing_rules_retired_end`、`ck_fx_rate_versions_retired_end`、`ck_tax_policy_versions_retired_end`）都已存在，不加新 CHECK。
  - 不改三张表的 BEFORE INSERT / BEFORE DELETE 触发器、定价规则分量表的触发器、两张锁表的触发器（0014 / 0015 / 0016 / 0022）。
  - 不改定价规则的「起点为空只许全局默认的第一条」（0030 的 ⑤）与 FIXED_RATE 完整性两段，也不改税率的待遇一致性检查：一字不动。
  - 不在数据库里兜底**与发布时刻 `t` 相关**的规则：起点不早于 `t`、截断与停用的时刻是 `t`、撤销预约只在起点晚于 `t` 时允许。触发器里取的「现在」与服务层在锁后取的 `t` 不是同一个时刻，判不准；与 #178 v4 §10 假设 3、#535 v5 §1 相同，仍由服务层保证。所以「先恢复为未截断、再截断到另一个时刻」这种两条语句的写法不在本设计的拦截范围内（恢复只在后继都是空区间时通过，之后的截断时刻属于与 `t` 相关的规则）。
  - 不检查历史：迁移不追溯判断已有行当初是否按规则写入（§10 假设 2）。
  - 不改供应商价格（AIH-TASK-145 已登记，设计闸门 #535 v5）。
- **现有行为与问题证据**：
  - `alembic/versions/20261030_0030_pricing_rule_project_scope.py` 的 `_TRIGGERS["trg_pricing_rules_before_update"]`：第一段只拒绝 `DISCARDED`；`IF OLD.status IN ('PUBLISHED', 'RETIRED')` 一段冻结的列不含 `effective_to`；发布跃迁上只查 FIXED_RATE 完整性；区间不重叠一段用哨兵日期。
  - `alembic/versions/20260929_0015_fx_rates.py` 与 `alembic/versions/20261017_0022_tax_policy.py` 的 BEFORE UPDATE：已有 `RETIRED` 终态与「发布时尽头必须为空」；区间不重叠一段用同样的哨兵日期；`IF OLD.status = 'PUBLISHED'` 一段对 `effective_to` 不设限。
  - 服务层的写法：定价规则 `app/services/pricing_rules.py` 的 `publish` 一律 `effective_to = None`、`retire` 只接受 `PUBLISHED` 且 `effective_to is None` 的规则；`app/repositories/pricing_rules.py` 与 `app/repositories/fx_rates.py` 的 `truncate` 只作用于未截断的已发布版本、`restore_open` 只作用于已截断的已发布版本、`mark_retired` 只接受未截断的已发布版本。税率只有 `publish_initial_tax_policy` 一条写入路径（AIH-TASK-077 设计 §1：截断、停用、预约、撤销预约都没有写入方，规则留在触发器里给以后的管理端路径）。
  - 生产核对（2026-11-01，只读）：库在 `0030_pricing_rule_project_scope`。`pricing_rules` 1 行，`PUBLISHED`、起点与尽头都为空；`fx_rate_versions` 1 行 `PUBLISHED`（起点与尽头都为空）、17 行 `DRAFT`；`tax_policy_versions` 1 行 `PUBLISHED`（有起点、尽头为空）。三张表都没有 `RETIRED` 行、没有带尽头的 `PUBLISHED` 行，没有任何端点落在 `1000-01-01` 或 `9999-12-31` 当天。三张表的触发器名与上面三个迁移建的一致。
  - 同类兜底的先例：设计闸门 #535 v5（供应商价格）§2 的第 1–5 处改动；本设计的定价规则改动与它一一对应，汇率、税率只需要其中两处。

## 2. 设计概要

```text
任何 UPDATE pricing_rules / fx_rate_versions / tax_policy_versions（服务层，或绕过服务的直接写库）
  → BEFORE UPDATE 触发器 <原名>_v2（本设计新建，替换原触发器）
      定价规则：① RETIRED / DISCARDED 是终态              ← 收紧（原只拒绝 DISCARDED）
                ② 草稿只能改成 DRAFT / PUBLISHED / DISCARDED ← 不变
                ③ DRAFT → PUBLISHED 时 NEW.effective_to 必须为空 ← 新增（汇率、税率已有）
                ④ PUBLISHED 的行冻结其余列、只能停用        ← 不变（只是不再覆盖 RETIRED）
                ⑤ PUBLISHED 的尽头只按截断 / 恢复 / 停用三种方式变 ← 新增（三张表）
                ⑥ FIXED_RATE 完整性                         ← 不变
                ⑦ 锁 pricing_rule_locks，查非空区间不相交   ← 相交判定去哨兵（三张表）
                ⑧ 起点为空只许全局默认的第一条              ← 不变
      汇率、税率：原有各段不变，只加 ⑤、改 ⑦ 的相交判定
  → 写入成功；计费读规则、汇率、税率的方式与取到的结果都不变
```

### 三个迁移

| 任务 | 迁移 | 表 | 新触发器 | 替换的触发器（降级时恢复它的冻结副本） | 改动处 |
| --- | --- | --- | --- | --- | --- |
| AIH-TASK-146 | `0032_pricing_rule_backstop`（文件 `alembic/versions/20261101_0032_pricing_rule_backstop.py`，`down_revision = "0031_provider_price_backstop"`） | `pricing_rules` | `trg_pricing_rules_before_update_v2` | `trg_pricing_rules_before_update`（0030 建的那一份） | P1–P5 |
| AIH-TASK-147 | `0033_fx_rate_backstop`（`20261101_0033_fx_rate_backstop.py`，`down_revision = "0032_pricing_rule_backstop"`） | `fx_rate_versions` | `trg_fx_rate_versions_before_update_v2` | `trg_fx_rate_versions_before_update`（0015） | P4、P5 |
| AIH-TASK-148 | `0034_tax_policy_backstop`（`20261101_0034_tax_policy_backstop.py`，`down_revision = "0033_fx_rate_backstop"`） | `tax_policy_versions` | `trg_tax_policy_versions_before_update_v2` | `trg_tax_policy_versions_before_update`（0022） | P4、P5 |

三个迁移结构相同，只是表、名字与正文不同；彼此独立（不同的表），按编号串成一条迁移链。

### 新触发器正文

每个新触发器的正文由被替换触发器的正文（建它的迁移里 `_TRIGGERS` 的那条字面量）做下列**逐字替换**得到，其余逐字不变：触发器名加 `_v2`；以下各处。每一处「原文」在原正文里恰好出现一次。

- **P1（只定价规则）**：原文
  ```sql
      IF OLD.status = 'DISCARDED' THEN
          SIGNAL SQLSTATE '45000'
              SET MESSAGE_TEXT = 'a discarded pricing rule is final';
      END IF;
  ```
  改为
  ```sql
      IF OLD.status IN ('RETIRED', 'DISCARDED') THEN
          SIGNAL SQLSTATE '45000'
              SET MESSAGE_TEXT = 'a retired or discarded pricing rule is final';
      END IF;
  ```
- **P2（只定价规则）**：紧接「草稿只能改成 …」一段（以 `SET MESSAGE_TEXT = 'a draft pricing rule is published or discarded';` 与其后的 `END IF;` 结束）之后插入（与 0015 同一段同一写法）：
  ```sql
      IF OLD.status = 'DRAFT' AND NEW.status = 'PUBLISHED' AND NEW.effective_to IS NOT NULL THEN
          SIGNAL SQLSTATE '45000'
              SET MESSAGE_TEXT = 'a pricing rule is published without an end';
      END IF;
  ```
- **P3（只定价规则）**：`    IF OLD.status IN ('PUBLISHED', 'RETIRED') THEN` 改为 `    IF OLD.status = 'PUBLISHED' THEN`。`RETIRED` 已由 P1 整体拒绝，这一段对它不再可达；冻结的列与「只能停用」的检查不变。
- **P4（三张表）**：区间不重叠一段的相交判定，原文
  ```sql
                 AND COALESCE(o.effective_from, '1000-01-01')
                     < COALESCE(NEW.effective_to, '9999-12-31')
                 AND COALESCE(NEW.effective_from, '1000-01-01')
                     < COALESCE(o.effective_to, '9999-12-31')) THEN
  ```
  改为
  ```sql
                 AND (o.effective_from IS NULL OR NEW.effective_to IS NULL
                      OR o.effective_from < NEW.effective_to)
                 AND (NEW.effective_from IS NULL OR o.effective_to IS NULL
                      OR NEW.effective_from < o.effective_to)) THEN
  ```
  这是半开区间 `[from, to)`（空端为无穷）相交的定义，对 `DATETIME` 的任何取值都成立，与 #535 v5 §2 第 4 处、服务层各 `verify_periods` 按 `None` 判断的写法一致。旧判定成立时新判定一定成立（哨兵只会把「相交」误判成「不相交」，不会反过来），所以只拒绝得更多。触发条件、先锁锁表、只看同一范围里区间非空的 `PUBLISHED` / `RETIRED` 行、本行为空区间时不判定，都不变。
- **P5（三张表）**：在 `IF OLD.status = 'PUBLISHED' THEN` 一段之内、「只能停用」的检查（定价规则以 `SET MESSAGE_TEXT = 'a published pricing rule can only be retired';` 结束；汇率、税率以 `SET MESSAGE_TEXT = 'a published fx rate version can only be retired';` / `'a published tax policy version can only be retired';` 结束）与其后的 `END IF;` 之后、本段的 `END IF;` 之前插入：
  ```sql
          IF (NOT (NEW.effective_to <=> OLD.effective_to)
              AND OLD.effective_to IS NOT NULL
              AND NOT (NEW.effective_to IS NULL AND NEW.status = OLD.status))
             OR (NEW.status = 'RETIRED' AND OLD.effective_to IS NOT NULL) THEN
              SIGNAL SQLSTATE '45000'
                  SET MESSAGE_TEXT =
                      '<主语> ends only by truncation, restore or retirement';
          END IF;
  ```
  `<主语>` 依次是 `a published pricing rule`、`a published fx rate version`、`a published tax policy version`（与各自「只能停用」消息的主语相同）。`SET MESSAGE_TEXT =` 与消息分两行写，是为了三张表的这一行都不超过 100 列（ruff）；MySQL 在 `=` 与字面量之间接受换行。条件的含义：尽头有变化时，只允许「原来为空」（截断，或停用时写尽头）或「原来非空、改为空、且状态不变」（恢复为未截断）；另外改为 `RETIRED` 只允许原来未截断。这正是服务层仅有的三种改法，所以不误杀。
- 三张表的新正文里都没有 `'1000-01-01'` 与 `'9999-12-31'`。
- （2026-11-01 在一次性 MySQL 8.4 容器里验证：按上面的替换生成的三个新触发器都能由应用的连接建出；把它们以「先建新、再删旧」的迁移装到库上后，三张表既有的服务层用例 `test_the_service_paths_pass_the_triggers`、`test_the_service_write_orders_pass` 全部通过；既有直接写库用例中结论改变的恰好是 §7「既有用例」所列的那些。）

### 新规则合起来的效果

- **定价规则「只在末尾追加」**：P2 之后，任何发布的区间都是 `[S, NULL)` 或 `[NULL, NULL)`。按 P4，`[S, NULL)` 与一个非空区间 `o` 相交当且仅当 `o.effective_to IS NULL OR S < o.effective_to`，所以只有在该范围所有非空区间都在 `S` 之前结束时才能发布：不能插进中间、不能补进停用留下的空档之前的位置、不能放在最早一条之前（回溯）。`[NULL, NULL)` 与任何非空区间都相交，只在时间线为空时通过，再由 ⑧ 限定为全局默认的第一条。这与服务层 `_placement` 的结果一致（起点 ≥ 末尾规则的尽头或 `t`）。
- **汇率、税率**：已有 ③（发布不带尽头），P4 补上边界上的漏判后，同样的「只在末尾追加」对 `DATETIME` 的任何取值都成立。
- **已停用是终态**（定价规则）：P1。汇率、税率早已如此。
- **尽头只按三种方式变**（三张表）：P5。

### 为什么换名字、不同名重建

与 #535 v5 §2「为什么换名字」相同：MySQL 没有原子替换触发器的语句，同名重建只能先 `DROP` 再 `CREATE`，两句之间这张表没有 BEFORE UPDATE 保护（0030 为此加了两个隔离触发器）。本设计改用「先建新名字的、再删旧的」：MySQL 8 允许同一张表同一时机有多个触发器，按 `ACTION_ORDER` 依次执行，任一 SIGNAL 都拒绝整条语句。新触发器在每一处都**不弱于**旧的（P1、P2、P4、P5 只拒绝得更多；P3 去掉的只是对 `RETIRED` 已不可达的分支），所以两个并存时的效果等于新触发器；只有旧的、两个都有、只有新的，这三种状态下保护都不弱于迁移之前，不存在没有保护的时刻，也不需要让整张表暂时不可写。新旧两个触发器在同一事务里先后锁同一个锁表行，第二次是同一事务的重入，不会自锁。

### 升级（MySQL 在线），三个迁移相同

每一步都按库的现状判断、可安全重跑。先读两样：新触发器是否存在、它的 `ACTION_STATEMENT`；旧触发器是否存在。新触发器存在且与目标逐字相同（下面第 2 步的比较）、旧触发器不存在 → 直接返回，不发任何语句。否则依次：

1. **预检**：查 `@@log_bin` 与 `@@log_bin_trust_function_creators`（查询、判定与报错文案照 0030，名字改为本迁移的表）。binlog 开着而开关关着时应用账号建不了触发器（ERROR 1419），在任何 DDL 之前抛错。
2. **形状核对**：新触发器已存在时，`information_schema.TRIGGERS` 里它的 `ACTION_STATEMENT` 去掉首尾空白后必须**等于**本迁移冻结的新正文（`FOR EACH ROW` 之后的部分）去掉首尾空白，不做其他任何改写；另外 `ACTION_TIMING = 'BEFORE'`、`EVENT_MANIPULATION = 'UPDATE'`、`EVENT_OBJECT_TABLE` 是本表。不符就抛错、不发任何 DDL，消息写明是哪个触发器、需要人对照本设计查看（只可能是手工建过同名对象）。理由与 #535 v5 §2 第 2 步相同：触发器没有「停用」状态，存在即生效；不比较 `DEFINER`（不对时执行报错、整条 UPDATE 被拒，只会更严）与 `SQL_MODE`（正文里没有反斜杠、双引号、`||`，也没有日期字面量）。这个比较写成一个函数，「是否已完成」、本步与降级第 2 步共用。旧触发器不核对：本迁移只删它，新触发器在它被删之前已经在位。
3. **建新触发器**：新触发器不存在时 `CREATE TRIGGER <新名> …`，不带 `FOLLOWS` / `PRECEDES`（与旧的并存时排在它之后；顺序不影响结果，只影响并存期间被两者都拒绝的写入得到哪一条消息）。
4. **删旧触发器**：旧触发器存在时 `DROP TRIGGER <旧名>`。

第 4 步一定在第 3 步之后：先有新的，才删旧的。

- **离线模式（`--sql`）**：读不了库，照 0013、0030 的惯例跳过预检与全部判断，按「什么都没做过」依次输出 `CREATE TRIGGER <新名> …`、`DROP TRIGGER IF EXISTS <旧名>`。

### 降级，三个迁移相同

同样按现状判断、可重跑。先读同样两样：旧触发器存在且与冻结副本逐字相同（比较同升级第 2 步）、新触发器不存在 → 直接返回。否则依次：

1. 预检同升级第 1 步（降级要重建旧触发器）。
2. 旧触发器存在但与冻结副本不是逐字相同 → 抛错（手工建过同名对象），不发任何 DDL；新触发器存在但与本迁移的新正文不是逐字相同 → 同样抛错（不删一个来历不明的触发器）。旧触发器不存在时，按冻结副本 `CREATE TRIGGER <旧名> …`。
3. 新触发器存在时 `DROP TRIGGER <新名>`。

先建旧的、再删新的，理由同升级。降级只放宽约束、不改任何行，没有数据前提，也不需要拒绝降级的条件。离线模式依次输出 `CREATE TRIGGER <旧名> …`、`DROP TRIGGER IF EXISTS <新名>`；降级只放宽，离线输出不会造成数据错误，所以不像 0030 那样拒绝离线降级。

### 字面量

新正文、冻结副本、预检的查询与报错、各个名字都写成模块级常量（照 0030 的 `_TRIGGERS` 写法，整条 `CREATE TRIGGER` 是一条语句）。冻结副本是被替换触发器的完整 `CREATE TRIGGER` 字面量：定价规则取 0030 的、汇率取 0015 的、税率取 0022 的；测试比对它与源迁移文件里的字面量逐字一致。新迁移不导入旧迁移文件（迁移文件之间不互相依赖，照 0030 冻结 0014 的做法）。

### 时间、金额、币种

不涉及。不改任何时间语义（`[F, T)`，UTC 整秒）、金额计算、取值规则或币种。

## 3. 不变量影响矩阵

| 不变量 | 是否触碰 | 可能怎样被破坏 | 设计控制 | 验证方式 |
| --- | --- | --- | --- | --- |
| INV-1 中心故障不中断 AI | 否 | 不涉及：只改三张表上的触发器，AI 调用路径不读它们 | — | — |
| INV-2 事件不重复扣费 | 否 | 认领与扣费不变 | — | 既有用例照跑 |
| INV-3 支付不重复入账 | 否 | 不涉及支付（税率被支付快照引用，但本设计只收紧写入，不改已有行） | — | — |
| INV-4 余额只经账本变动 | 否 | 不涉及钱包 | — | — |
| INV-5 历史账本不可变 | 否 | 不写账本 | — | — |
| INV-6 事件保留版本引用 | **是** | ① 改已停用定价规则的尽头，已命中它的时刻改选别的规则，或空档被回填；② 带尽头的定价规则直接发布进空档或最早一条之前（回溯）；③ 已有区间起点在 `'9999-12-31 00:00:00'` 时，新版本被哨兵判定放到它之前；④ 已截断版本的尽头被改成另一个值，已取到它的时刻失去它；⑤ 迁移中途中断，触发器缺失、保护被绕过；⑥ 迁移改动已有行，被事件、支付、收据引用的版本变了 | ① P1；② P2 加 P4；③ P4；④ P5；⑤ 先建新、后删旧，每个中间状态都不弱于迁移之前；⑥ 迁移只建、删触发器，不写任何行，`id` 不变 | §7 的直接写库用例、不误杀用例、迁移各用例 |
| INV-7 客户不可见成本毛利 | 否 | 不改任何接口或返回字段 | — | — |
| INV-8 租户不可互访 | 否 | 不改任何查询；定价规则的范围键不变 | — | — |
| INV-9 对话内容不入库 | 否 | 不涉及 | — | — |
| INV-10 金额用 Decimal | 否 | 不新增任何金额计算或列 | — | — |
| INV-11 event_id 至多一次财务效果 | 否 | 计费事务不变 | — | — |
| INV-12 定稿对账单不可变 | 否 | 对账单未实现 | — | — |
| INV-13 状态与事件原子提交 | 否 | 服务层的事务边界不变；被拒绝的直接写入整条回滚 | — | — |
| INV-14 队列丢失不毁持久工作 | 否 | 不涉及队列 | — | — |

## 4. 状态与并发

迁移之后、MySQL 上三张表的 UPDATE（SQLite 没有触发器，由服务层按同样的规则写）。「新」标出本设计新增的拒绝；汇率、税率没有 P1–P3 是因为它们早已如此。

| 当前状态 | 事件（UPDATE） | 前置条件 | 新状态 | 副作用 | 非法时结果 |
| --- | --- | --- | --- | --- | --- |
| `DRAFT` | 改草稿内容 | — | `DRAFT` | — | — |
| `DRAFT` | 发布 | 尽头为空（定价规则**新**，P2）；与同一范围的非空区间不相交（三张表判定**新**，P4）；其余各表原有的发布前置不变 | `PUBLISHED` | 先锁该表的锁表行 | SIGNAL 1644 |
| `DRAFT` | 丢弃（定价规则、汇率） | — | `DISCARDED` | — | — |
| `DRAFT` | 直接改为 `RETIRED` | — | — | — | SIGNAL（不变） |
| `PUBLISHED` | 截断（尽头 空 → 有值） | 不与非空区间相交 | `PUBLISHED` | 先锁锁表行 | CHECK 3819 / SIGNAL |
| `PUBLISHED` | 恢复（尽头 有值 → 空，状态不变） | 不与非空区间相交 | `PUBLISHED` | 先锁锁表行 | SIGNAL |
| `PUBLISHED` | 尽头 有值 → 另一个有值 | — | — | — | SIGNAL（**新**，P5） |
| `PUBLISHED` | 停用 | 停用前未截断（**新**，P5）；写尽头（CHECK）；不相交 | `RETIRED` | 先锁锁表行 | SIGNAL / CHECK 3819 |
| `PUBLISHED` | 改其他列、改成其他状态 | — | — | — | SIGNAL（不变） |
| `RETIRED` | 任何改动 | — | — | — | SIGNAL（定价规则**新**，P1；汇率、税率不变） |
| `DISCARDED` | 任何改动 | — | — | — | SIGNAL（定价规则消息改为新的） |

- **并发写入如何串行化**：不变。服务层的发布、停用先锁各自的锁表行（`pricing_rule_locks` / `fx_rate_locks` / `tax_policy_locks`）；触发器的区间检查锁同一行再查相交。新增的检查（P1、P2、P5）只看本行，在锁之前就拒绝。
- **数据库负责的唯一性**：不变（各表的 `open_slot` 唯一索引）。
- **幂等键 / 相同 ID 不同载荷 / 重试**：不涉及，本设计不新增写操作。
- **原子提交**：被拒绝的语句整条不生效；服务层事务里任一语句被拒绝即整个事务回滚（服务层本来就不会写出被拒绝的语句）。
- **迁移与在线写入的并发**：`CREATE TRIGGER` / `DROP TRIGGER` 各取一次短暂的排他元数据锁：持有该表行锁或共享锁的事务（计费事务对规则行、汇率行持共享锁）先提交，期间新的读写在元数据锁上排队，不丢事件。不 `ALTER TABLE`、不重建表。没有任何时刻这张表缺 BEFORE UPDATE 保护，迁移期间管理员的发布、停用照常可用，结果受的约束不弱于迁移前。

## 5. 失败与恢复矩阵

三个迁移相同；「迁移之前」指 0031 / 0032 / 0033 完成后的状态。

| 失败点 | 对外结果 | 数据最终状态 | 是否可重试 | 恢复来源 | 告警 |
| --- | --- | --- | --- | --- | --- |
| 校验失败（绕过服务的直接写库违反新规则） | 该语句报错（SIGNAL 1644） | 该语句不生效；所在事务由调用者回滚 | 否（写法本身错误） | 改用服务层接口 | 无（直接写库的人看到报错） |
| DB 提交失败（服务层发布 / 停用） | 与今天相同：500，回滚 | 不变 | 是 | 管理员重试 | 与今天相同 |
| Redis/Celery 丢失 | 不涉及：迁移与触发器不经队列 | — | — | — | — |
| 外部服务超时 | 不涉及 | — | — | — | — |
| 重复请求 | 与今天相同（已发布再发布 200、已停用再停用 409） | 不变 | — | — | — |
| 迁移：预检失败 | 部署失败，旧镜像照旧运行 | 什么都没改，`alembic_version` 不变 | 是 | 给账号打开 `log_bin_trust_function_creators` 后重新部署 | 部署工作流失败 |
| 迁移：形状核对不符（同名新触发器与目标不是逐字相同） | 部署失败，消息写明哪个触发器 | 什么都没改 | 否（要人） | 人对照本设计查看手工建过的同名对象，删掉或改正后重新部署 | 部署工作流失败 |
| 迁移：建新触发器失败 | 部署失败 | 只有旧触发器：与迁移之前相同 | 是 | 重新部署 | 部署工作流失败 |
| 迁移：建了新触发器、删旧的之前中断 | 部署失败 | 两个触发器并存：效果等于新的 | 是 | 重新部署：形状核对通过，只删旧的 | 部署工作流失败 |
| 迁移：全部完成、写 `alembic_version` 之前退出 | 部署失败 | 已是目标状态 | 是 | 重新部署：判为已完成，不发任何语句，只前进版本 | 部署工作流失败 |
| 降级中途中断（任一步） | 降级命令失败 | 每个中间状态都不弱于迁移之前（先建旧、再删新） | 是 | 重跑降级 | — |
| 降级：同名对象来历不明 | 降级命令失败，消息写明哪个触发器 | 什么都没改 | 否（要人） | 人对照本设计查看 | — |

## 6. 数据与安全边界

- **租户过滤**：不涉及。不新增、不改任何查询；定价规则按范围键比较的方式不变。
- **鉴权主体及权限**：不变。管理端接口仍只对 `ADMIN` 开放；迁移由部署时的迁移账号执行，与 0013–0031 相同（建触发器要求 binlog 预检）。
- **客户侧禁止返回的内部字段**：不变。不改任何接口。
- **日志、审计、异常**：迁移的报错消息只含表名与触发器名，不含行内容。触发器的 SIGNAL 消息是固定英文文本。服务层审计不变（被数据库拒绝的直接写库本来就不经服务、不产生审计）。
- **密钥**：不涉及。
- **prompt / response 或客户数据**：不涉及。
- **数据保留及删除**：不变。三张表的行仍一律不许删除。

## 7. 测试证据计划

MySQL 用例只在 `BILLING_TEST_DATABASE_URL` 的真 MySQL 上跑（未设置时 skip、CI 必跑）。中断用 `before_cursor_execute` 监听在某一步要发的语句之前抛错来制造（照 `tests/backend/test_pricing_rule_project_scope_migration.py`）。

**迁移用例的观察位置**（三个新模块都照这一条写）：被观察的部分以本迁移为准——用例开始时先回到 head、再降到本迁移的 revision（这一步在被观察部分之外）；升级只观察 down_revision → 本迁移，降级只观察本迁移 → down_revision，不从 head 起数语句、不在 head 上断言本迁移之外的东西；`finally` 回到 head，断言本表为空（`TRUNCATE`，关外键检查；不删、不重建任何触发器）、本表的 BEFORE UPDATE 恰好只有新触发器。这样之后的迁移（例如 0033 之于 0032 的模块）不会改变这些用例的结论。（2026-11-01 在一次性 MySQL 8.4 容器里验证：按这一条改写 0030 的迁移模块后，依次加上只改名的 0031 与本设计的 0032–0034，0030 的模块全部通过。）

**前提：145 先把三处既有用例的观察位置改到 0030**。今天 main 上有三处在 head 上取基准，0031 起任何给触发器改名的迁移都会打破它们：`tests/backend/test_pricing_rule_project_scope_migration.py`（`_start` 停在 head，`_finish` 回到 head 后断言版本是 0030，`_assert_head_structure` 与中断重跑在 head 上比 0030 的触发器）、`tests/backend/test_fx_rates_db.py::test_upgrade_and_downgrade`（在 head 上取全库触发器名，往返 0014 后比对）、`tests/backend/test_migrations.py::test_0022_downgrade_refuses_while_a_version_exists`（从 head 降级，比对前后的全库触发器）。AIH-TASK-145 的契约（PR #538 修正）要求它的实现把这三处的观察位置改到 `0030_pricing_rule_project_scope`：0030 模块的 `_start` 回到 head 再降到 0030、`_finish` 断言版本是 head；另外两个先降到 0030 再取基准。改完之后，这三处不再受 0032–0034 影响（上面的容器实测里，加上 0032–0034 时这三处都通过），所以 146–148 不改它们，也不把这几个文件列进自己的可改路径（147 本来就要改 `test_fx_rates_db.py` 的其他用例，但不动 `test_upgrade_and_downgrade`）。为了让登记前预审读到的是改好之后的代码，146–148 在 145 合并之后才登记，预审在那时的 main 上重跑。

每个任务一个新模块：`tests/backend/test_pricing_rule_backstop_migration.py`（146）、`tests/backend/test_fx_rate_backstop_migration.py`（147）、`tests/backend/test_tax_policy_backstop_migration.py`（148），各含下表「迁移」各行；直接写库的行为用例加在各表既有的直接写库模块里：`tests/backend/test_pricing_rules_service.py`（146）、`tests/backend/test_fx_rates_db.py`（147）、`tests/backend/test_tax_policy_db.py`（148）。

| 风险/需求 | 测试层级 | 场景 | 预期结果 |
| --- | --- | --- | --- |
| 迁移结构 | integration（MySQL） | 在本迁移的 revision 上读 `information_schema.TRIGGERS` | 本表的触发器恰好是原来的 BEFORE INSERT、`<旧名>_v2`、原来的 BEFORE DELETE；新触发器的 `ACTION_STATEMENT` 去掉首尾空白后等于冻结的新正文；形状核对函数对它返回「相同」 |
| 字面量 | unit | 新正文 vs 被替换触发器的源字面量；冻结副本 vs 源迁移文件 | 新正文由源字面量恰好经 §2 列出的替换得到（逐行比对：删去的行与加上的行恰好是 §2 各处，其余行相同；定价规则五处、汇率税率两处，加触发器名）；新正文里没有 `'1000-01-01'` 与 `'9999-12-31'`；冻结副本与源迁移文件里的字面量逐字一致 |
| 离线 | integration（MySQL 的 alembic 配置） | `--sql` 升级与降级（只跑 down_revision 与本迁移之间） | 升级依次输出 `CREATE TRIGGER <新名>`、`DROP TRIGGER IF EXISTS <旧名>`，不读库；降级依次输出 `CREATE TRIGGER <旧名>`、`DROP TRIGGER IF EXISTS <新名>` |
| 不改已有行 | integration（MySQL） | 降到 down_revision，造出各种状态的行（定价规则、汇率：未截断的已发布、被截断的已发布、带尽头的已停用、撤销预约留下的空区间、草稿、丢弃的草稿；税率：已发布、被截断的已发布、已停用、草稿）；记下本表全部行与列；升级 | 逐行逐列不变 |
| 升级中断后重跑 | integration（MySQL） | 在两个位置中断：发 `DROP TRIGGER <旧名>` 之前（两个并存）；写 `alembic_version` 之前（已完成）；各自重跑 | 第一种只发 `DROP TRIGGER`，第二种一条 DDL 都不发；最终结构同「迁移结构」 |
| 中间状态不弱于迁移之前 | integration（MySQL） | 在「两个并存」的位置，逐条执行迁移之前就被拒绝的直接写（各表：改已发布行的冻结列、草稿直接改为 `RETIRED`、相交的区间）与本设计新拒绝的（各表的 P4、P5 场景，定价规则另加 P1、P2 场景） | 全部被拒绝 |
| 降级中断后重跑 | integration（MySQL） | 在两个位置中断：发 `DROP TRIGGER <新名>` 之前（两个并存）；写 `alembic_version` 之前；各自重跑降级 | 只发剩下的语句；最终本表的 BEFORE UPDATE 只有旧触发器 |
| 降级恢复 | integration（MySQL） | 降到 down_revision | 本表的 BEFORE UPDATE 只有 `<旧名>`，`ACTION_STATEMENT` 与冻结副本的正文一致；一条迁移之前允许、本设计拒绝的直接写（定价规则：改已停用规则的尽头；汇率、税率：已截断版本的尽头改成另一个值）通过——证明真的恢复了 |
| 形状核对 | integration（MySQL） | 降到 down_revision 后手工建，各自升级一次：① 同名新触发器，正文是目标正文少 P5 一段；② 正文与目标相同但多一条 SIGNAL；③ 正文与目标只差一处字面量里的空格数；④ 同名新触发器挂在另一张表上（正文同目标、表名不同的临时表）。降级方向：⑤ 新触发器在、旧触发器是手工建的另一份正文；⑥ 旧触发器在且相同、新触发器是手工建的另一份正文 | 都抛错，消息写明哪一个触发器，没有发任何 DDL（`information_schema.TRIGGERS` 前后相同） |
| 预检 | unit | `@@log_bin` / `@@log_bin_trust_function_creators` 四种组合 | 只有「开着而开关关着」拒绝；在任何 DDL 之前 |
| 降级不需要数据前提 | integration（MySQL） | 本表有各种状态的行时降级 | 通过、行不变 |
| 不误杀：服务路径 | integration（MySQL） | 既有 `test_the_service_paths_pass_the_triggers`（定价规则）、`test_the_service_write_orders_pass` 与 `test_withdrawing_a_reservation_after_a_retirement_keeps_the_retired_end`（汇率）、税率的服务层发布用例 | 在 head 上照旧通过、断言不改 |
| 定价规则：已停用是终态（146） | integration（MySQL） | 绕过服务：对带尽头的 `RETIRED` 规则分别改短尽头、改长尽头、清空尽头、只改 `updated_at`、改回 `PUBLISHED`；对撤销预约留下的空区间规则改尽头 | 全部 SIGNAL「a retired or discarded pricing rule is final」；库不变 |
| 定价规则：发布不带尽头（146） | integration（MySQL） | 绕过服务：草稿直接改为 `PUBLISHED`，区间分别是：落在已停用规则留下的空档里的 `[S, E)`；整段在该范围最早一条规则之前的 `[S, E)`（`S`、`E` 非空，回溯）；在末尾之后的 `[S, E)`；对照：在末尾之后的 `[S, NULL)` | 前三次 SIGNAL「a pricing rule is published without an end」；对照通过 |
| 相交判定的边界（三张表各一组） | integration（MySQL） | 绕过服务，同一范围里：① 已有版本 `['9999-12-31 00:00:00', '9999-12-31 00:00:01')`（先发布为 `['9999-12-31 00:00:00', NULL)` 再截断），把完整草稿直接发布为 `[F, NULL)`（`F` 是普通时刻）；汇率另加一次 `[NULL, NULL)`；③ 已有版本 `['9999-12-31 23:59:58', NULL)`，再发布 `['9999-12-31 23:59:59', NULL)` 而不先截断；④ 对照：① 的已有版本之后发布 `['9999-12-31 00:00:01', NULL)` | ①③ SIGNAL「<表> periods overlap」（用例注释写明：① 在旧判定下会被放行；③ 在旧判定下由 `open_slot` 唯一索引以 1062 拒绝，新判定在触发器里就拒绝）；④ 通过（首尾相接） |
| 尽头只按三种方式变（三张表各一组） | integration（MySQL） | 绕过服务：已截断的 `PUBLISHED` 行把尽头改短；改长到仍不与后继相交的值（后继是撤销预约留下的空区间；税率没有撤销预约，用「没有后继」）；已截断的 `PUBLISHED` 行直接改为 `RETIRED`（尽头不变、改短各一次）；对照：未截断的截断、被截断的恢复为空（后继为空区间或没有后继）、未截断的停用 | 前四次 SIGNAL「<主语> ends only by truncation, restore or retirement」；对照三次通过 |
| 并发 | integration（MySQL） | 各表既有的「区间检查拿锁」用例 | 照旧通过（锁顺序不变） |
| 定价规则既有用例（146） | integration（MySQL） | `tests/backend/test_pricing_rules_service.py` 里结论改变的五个（2026-11-01 在容器里装上 0032 实测，恰好这五个失败） | 见下方「既有用例」 |
| 汇率既有用例（147） | integration（MySQL） | `tests/backend/test_fx_rates_db.py` 里结论改变的两个（同上，装上 0033） | 见下方 |
| 税率既有用例（148） | integration（MySQL） | `tests/backend/test_tax_policy_db.py` 里结论改变的一个（同上，装上 0034） | 见下方 |
| Redis/Celery 丢失 | — | 不涉及 | — |
| 租户越权 | — | 不涉及：不改任何查询 | — |
| 金额精度 | — | 不涉及：不新增任何金额计算 | — |
| 幂等重放 | — | 不涉及：不新增写操作；服务层的幂等用例照跑 | — |

**既有用例**（预期都写在这里，实现照此改，不改其余断言）：

- 146，`tests/backend/test_pricing_rules_service.py`：
  - `test_only_truncation_and_retirement_pass_on_a_published_rule`：先截断、再把同一条直接改为 `RETIRED` 的那一步改为对一条**未截断**的规则直接停用（通过）；另断言已截断的直接停用 SIGNAL P5 的消息；把已停用改回 `PUBLISHED`、改丢弃的草稿两处的消息改为「a retired or discarded pricing rule is final」（P1 先于不可变检查）。
  - `test_a_retired_rule_always_has_an_end`：清空已停用规则尽头的那一步改为断言 SIGNAL「a retired or discarded pricing rule is final」（P1 先于 CHECK）；直接停用不写尽头仍是 CHECK 3819。
  - `test_the_database_refuses_overlapping_periods`：已有规则改为先直接发布为 `[T0, NULL)`、再用第二条 UPDATE 截断为 `[T0, T0+10s)`；候选同样先发布为 `[S, NULL)`，`E` 非空时再截断。`partial`、`contained`、`same-start`、`from-the-beginning`、`open-over-it` 在发布那一步被拒绝，消息仍是「pricing rule periods overlap」（区间检查先于 ⑧）；`end-to-end-after` 与 `open-after` 通过；**`end-to-end-before`（`[T0-10s, T0)`）由通过改为拒绝**：它只能先发布为 `[T0-10s, NULL)`，与已有的规则相交；直接写带尽头的则得到 P2 的消息。这正是 §1 ② 要堵的回溯，用例改为断言拒绝，注释写明原因。
  - `test_empty_periods_and_other_scopes_do_not_overlap`：`[T0, T0+10s)` 的已有规则改为先发布为 `[T0, NULL)`、再截断；结论不变。
  - `test_only_the_first_global_rule_starts_with_no_time`：「已有已发布规则 `[T0, NULL)`」那一段改为三步，都在同一个嵌套事务模式下断言：① 候选 `(None, T0)` 被拒绝，消息改为「a pricing rule is published without an end」（P2 先于 ⑧）；② 原来通过的 `(T0 - DAY, T0)` 改为断言被拒绝，消息同 ①（P2）；③ 新增：候选 `(None, None)` 被「pricing rule periods overlap」拒绝（与已有的 `[T0, NULL)` 相交，区间检查先于 ⑧）。文档字符串里「候选在已有行的起点处结束、所以是起点检查在拒绝」的说法改为：已有非空区间时起点为空的发布先被区间检查拒绝，起点检查只在只有空区间时起作用。开头「第一条全局规则」与「只有撤销预约的空区间」两段不变（后者仍由 ⑧ 拒绝）。
- 147，`tests/backend/test_fx_rates_db.py`：
  - `test_two_truncated_versions_that_intersect_are_refused`：把 X 的尽头改进 Y（`10:00:12`）、改过 Y（`10:00:20`）两次的消息改为 P5 的「a published fx rate version ends only by truncation, restore or retirement」；清空 X 的尽头仍是「fx rate periods overlap」（恢复与非空的 Y 相交）；最后「改短到 `10:00:05`」一步改为断言被 P5 拒绝、X 仍是 `[10:00:00, 10:00:10)`；其后「改回 `10:00:10`」一步此时是尽头不变的写（P5 只管尽头有变化的写），照旧通过，保留不改；Y 的断言不变。
  - `test_the_same_edits_pass_on_a_draft_and_a_published_version`：已发布版本「截断到 13 点、再改到 11 点」改为「截断到 13 点、恢复为空、再截断到 11 点」，断言不变。
- 148，`tests/backend/test_tax_policy_db.py`：
  - `test_a_published_version_truncates_and_retires`：最后「先截断到 D3、再停用于 D3」改为：断言已截断的直接停用被 P5 的「a published tax policy version ends only by truncation, restore or retirement」拒绝；恢复为空后停用于 D3 通过，`period` 断言不变。
- 每个任务在 `tests/backend/test_migrations.py` 里：在 head 上断言本表触发器的用例改为本迁移之后的形状——`test_0006_keys_indexes_checks_and_triggers` 的全库触发器集合里本表的 BEFORE UPDATE 换成 `<旧名>_v2`；146 的 `test_0014_keys_foreign_keys_checks_and_triggers`（触发器清单与 BEFORE UPDATE 正文：按名字 `trg_pricing_rules_before_update_v2` 查、与 0032 的新正文比）、147 的 `test_0015_keys_foreign_keys_checks_and_triggers`、148 的 `test_0022_keys_foreign_keys_checks_triggers_and_the_lock_row` 同理；只在 0014 / 0015 / 0022 / 0030 那一版上验证它们自身的用例照旧。（2026-11-01 在容器里实测：0031 的契约勘误落地之后，装上 0032、0033、0034 时 `test_migrations.py` 里分别恰好多失败这几个。）

## 8. 迁移与上线

- **数据迁移步骤**：无。不改、不回填、不删除任何行；每个迁移只建一个触发器、删一个触发器（§2）。
- **锁表与性能影响**：只有 `CREATE TRIGGER` / `DROP TRIGGER` 的短暂元数据锁（§4）；不重建表。生产三张表各 1–18 行，毫秒级。
- **兼容窗口**：新触发器只拒绝服务层从不写出的状态，所以迁移之后、新镜像起来之前的旧代码照常运行；三个任务都不改应用代码。
- **部署顺序**：三个任务各自合并、各自按 `deploy.sh` 先迁移、再换镜像，与以往相同。0032 接在 AIH-TASK-145 的 0031 之后，所以 146 依赖 145；147、148 依次依赖前一个。不需要上线前的数据核对（没有新 CHECK）。
- **回滚或前滚方式**：可降级（§2「降级」），降级不丢数据、不需要数据前提。
- **部分部署时的行为**：迁移中途失败的每个状态都不弱于迁移之前（§5），重新部署补完；不存在「表暂时不可写」的状态。
- **监控指标和告警负责人**：不新增。迁移失败即部署工作流失败（现有通知）。

## 9. 方案取舍

| 方案 | 优点 | 风险 | 未采用原因 |
| --- | --- | --- | --- |
| A（采用）：每张表一个新名字的触发器，先建新、后删旧，一个迁移一张表 | 每个中间状态都不弱于迁移之前；步骤只有建、删两步；与 #535 v5 同一做法，审查与测试可以照搬 | 触发器名字永久带 `_v2` 后缀；文档与测试里的触发器清单要同步 | — |
| B：同名重建，照 0030 加隔离触发器 | 名字不变 | 步骤多一倍，中途失败时整张表不可写；0030 需要隔离是因为它的中间状态会让区间检查算错范围键，本设计的中间状态都更严 | 收益只有名字 |
| C：只做 TODO 记录的两处（定价规则 `RETIRED` 终态、三张表去哨兵） | 范围最小 | 定价规则仍可直接写库发布带尽头的规则（回溯、插空档），三张表的已截断尽头仍可改成另一个值；同一个触发器本来就要重建 | Kelvin 2026-11-01 选择对齐 #535 v5 的全部规则 |
| D：一个迁移改三张表 | 少两个迁移文件 | 单个任务预计 2000 行以上，超过拆分线；一张表的问题会挡住另外两张 | 三张表互不相关，按表拆成三个任务，每个都能单独过 CI |
| E：升级时也核对旧触发器的正文，不符就拒绝 | 发现手工改过的旧触发器 | 旧触发器被删，新触发器已在位且已核对，旧的长什么样不影响结果；多一种失败状态 | 与 #535 v5 相同，不核对 |

## 10. 未决问题与假设

- **未决问题**：无。
- **尚未验证的假设**：
  1. AIH-TASK-145（0031）先合并、先部署，并按 PR #538 修正后的契约把三处既有用例的观察位置改到 0030（§7「前提」）。依据：146 的 `depends_on` 是 145，0032 的 `down_revision` 是 0031；#538 的验收标准逐一点名了这三处。若 145 的实现没有照做，或改了迁移编号：146–148 登记前在 145 合并后的 main 上重跑预审与本地容器实测时会发现，按实际情况改 146 的契约（把这些文件与改法加进 146），不改本设计的规则。
  2. 已有行当初都按规则写入。迁移不追溯检查（只有写入时刻能判断）。依据：2026-11-01 生产核对，三张表都没有 `RETIRED` 行、没有带尽头的已发布行、没有端点落在哨兵日期当天的行；各服务层在发布、停用事务里都有 `verify_periods` 复查。若不成立：那些行仍在，新触发器只阻止今后再写出同类的行。
  3. 税率今天没有截断、停用、预约、撤销预约的写入方（AIH-TASK-077 设计 §1）。P5 把以后的管理端路径限定为与汇率相同的三种改法；那个路径的设计闸门若需要别的改法，要在那里改触发器。
- **需要谁拍板**：无（范围与拆分 Kelvin 2026-11-01 已定：对齐 #535 v5 的全部规则；一份设计、按表拆三个任务）。

## 11. 审查与版本绑定

审查方要回答的五个问题常驻在 [scripts/review_checklist.md](../../scripts/review_checklist.md) 的「设计审查」一节，此处不重复。

**版本绑定规则**（规则的出处是 [WORKFLOW §3](../../docs/WORKFLOW.md)；这里保留一份是因为审查材料里只有本 Issue 正文，两处改一起改）：

- Codex 的批准必须写成 `APPROVED: design v<N>`，`<N>` 取本 Issue 顶部的「设计版本」
- **设计版本一变，之前的 APPROVE 自动作废**，必须重新过闸门
- 什么算「实质修改」：改了契约、表结构、事务边界、状态机、失败语义、不变量控制。改错别字不算
- 实质修改时：把顶部版本 +1，在下方追加一条变更说明，状态退回 `READY_FOR_REVIEW`

### 任务拆分

三个实现任务，一张表一个，依次依赖：

| 任务 | 内容 | 可改路径 | 预计改动 |
| --- | --- | --- | --- |
| `AIH-TASK-146`（依赖 145） | 迁移 0032（定价规则，P1–P5）、新迁移模块、定价规则既有直接写库用例与新增用例、`test_migrations.py` 的 head 形状、`docs/database-schema.md` 定价规则一节 | 5 个 | 约 900 行 |
| `AIH-TASK-147`（依赖 146） | 迁移 0033（汇率，P4、P5）、新迁移模块、汇率直接写库用例、`test_migrations.py`、`docs/database-schema.md` 汇率一节 | 5 个 | 约 750 行 |
| `AIH-TASK-148`（依赖 147） | 迁移 0034（税率，P4、P5）、新迁移模块、税率直接写库用例、`test_migrations.py`、`docs/database-schema.md` 税率一节 | 5 个 | 约 700 行 |

都只有数据库一层，服务层、接口、前端、模型都不改。三个任务在 AIH-TASK-145 合并之后登记（§7「前提」），登记前预审在那时的 main 上重跑。

送审前的登记前预审（2026-11-01，在 main `59ee935` 加本设计草稿与三个契约草稿的导出上）报了六条，处理如下：`test_only_the_first_global_rule_starts_with_no_time` 的改法不明确 → §7 写明三步；147 的「改回 `10:00:10`」被误写成被拒绝 → §7 更正为尽头不变的写照旧通过；其余四条（0030 模块、`test_fx_rates_db.py::test_upgrade_and_downgrade` 在 0032 / 0033 / 0034 之后失败且不在可改路径里）都是 §7「前提」那三处，由 145 按 #538 修正后的契约改掉，登记时在 145 合并后的 main 上重跑预审确认。

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
| v1 | 2026-11-01 | 初稿 | — |

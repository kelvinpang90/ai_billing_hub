# AIH-TASK-028 设计：FX 汇率版本、BNM 拉取、审批与告警计数接口（已批准 v2）

> **来源**：设计闸门 Issue #183。本文件是 `APPROVED: design v2` 那一版正文的**逐字副本**，审查者为 Codex。
> 放进仓库，是因为 OpenClaw Worker 在沙箱里不联网、读不到 GitHub Issue。
> **实现以本文件为准**；与 Issue 不一致时，以 Issue #183 上被批准的 v2 为准。设计要改，就回到 Issue 升版本、重新过闸门，不要直接改本文件。
> 正文里的「T-C」即 [PHASE-2-plan.md](PHASE-2-plan.md) 的占位名；§11 的六个实现任务 F1–F6 登记为 `AIH-TASK-028`（F1）、`AIH-TASK-039`（F2）、`AIH-TASK-040`（F3）、`AIH-TASK-041`（F4）、`AIH-TASK-042`（F5）、`AIH-TASK-043`（F6）。正文提到的其他占位名（T-A…T-L）对应的登记编号见 `docs/TODO.md` 的 Phase 2 一节。
> 与 Issue 正文的唯一差别：一处链接目标由 GitHub 网页上的 `../blob/main/…` 改成仓库内的相对路径，链接文字与其余正文逐字相同。

---

## 0. 准入判定

| 任务涉及 | 要填的范围 |
| --- | --- |
| 钱包 / 账本 / 定价 / 汇率 / 支付 / 幂等 / 状态机 | **全部章节** |
| 用量摄取 / 认证与会话（集成侧与平台侧）/ Webhook | §1–§7（**§6 不可省**） |
| 前端 / 文档 / CI / 脚本 | 不走本闸门 |
| 既不碰钱、也不碰用量摄取 / 认证与会话 / Webhook 的后端改动 | 不走本闸门 |

本任务档位：`全部`。它是汇率的版本与状态机（§17.1），每个事件的 MYR 估算成本都经它换算；另外新建一个带令牌认证的内部告警接口。

---

状态：`READY_FOR_REVIEW`
负责人：Claude
**设计版本**：`v2`（v1 已批准。v2 修正登记前预审与人工核实发现的 §2 缺陷 —— 预约存在时不指定时刻的发布没有合法结果、撤销预约后下一次发布必然失败、撤销预约会把已退役的版本恢复成无尽头、数据库兜底缺 026 v4 / 027 v4 那几道 —— 按 2026-09-30 对 BNM 接口的实测改写适配器（v1 §10 假设 2「周末返回上一交易日」不成立；BNM 输出的是双精度字面量，按 v1 的精确解析每次拉取都会失败），写死 v1 留空的小决定，并把实现范围按层拆成六个任务，见 §12）
对应需求：spec §5、§14、§17.1、§58、§74.2、§80、§95、§110、§113、§120；`REQ-FIN-001`、`REQ-FIN-002`；ADR-0005（FX 来源与取值规则）
目标 PR：待开（批准后在 `.platform/tasks.yaml` 按 §11 登记六个实现任务，编号登记时分配）
Phase 2 计划：`docs/design/PHASE-2-plan.md` 的 T-C（含第 1 节第 2、3 条：BNM 适配器归 Phase 2；告警走计数接口 + `deploy/monitor.sh` + Healthchecks，计数接口与它的访问控制在本闸门定）

## 1. 目标与边界

- **要解决的问题**：供应商价格是原币种（ADR-0005 §5），计费要按事件 `occurred_at` 时生效的**已发布**汇率换成 MYR（§17.1）。流程必须是「自动拉取 → 草稿 → 管理员发布」，自动拉取绝不静默发布；外部接口不可用或缺历史汇率时必须能手工录入；取不到汇率的事件判 `FX_RATE_ERROR`、不扣费。ADR-0005 已定来源（BNM openAPI）、每日拉取、手工发布、按 `occurred_at` 取值、热路径绝不调 BNM，并要求两条告警在 Phase 2 前落地。现在这些都没有，也没有任何告警接口。
- **可观察的完成标准**：
  1. 每天吉隆坡 12:30、14:30、17:30（v2：三次，见 §2「BNM 适配器」），Celery Beat 按**吉隆坡当天的日期**向 BNM 取配置币种（V1 为 USD）的中午场中间价，取到就写成 `DRAFT`；同一报价日不产生第二条草稿；每次拉取的结果留下一条记录。
  2. 管理员能手工录入草稿、编辑、丢弃、发布、退役；发布后数据库层不可改。
  3. 只读查询函数 `resolve_fx_rate(session, base_currency, occurred_at)` 与发布串行：一个事件一旦取到某个汇率版本，以后永远取到同一个。
  4. `GET /internal/alerts`（带令牌、只对宿主机与内网开放）逐行给出告警维度的结论；`deploy/monitor.sh` 按维度各推一个 Healthchecks 检查。本任务带两个维度：`fx_fetch`（连续拉取失败）与 `fx_stale`（当前生效的汇率过期）。
- **明确不做什么**：
  - 用汇率算钱 —— T-G（§80 的高精度中间计算与一次舍入）
  - 自动发布 —— ADR-0005 §3：V1 由管理员手工发布
  - 其他来源的适配器 —— 只留可替换的接口（§17.1「pluggable」），V1 只有 BNM 与手工
  - 其他告警维度 —— T-E（幂等冲突）与 T-H（定价错误、汇率错误、未知模型、负余额、outbox 积压）接进本任务建的接口
  - 前端 —— T-K
- **现有行为与问题证据**：`app/` 里没有任何出站 HTTP 客户端（运行时依赖没有 `httpx` / `requests`）；`app/core/celery_app.py` 的 Beat 只有 `outbox-recovery`；`deploy/monitor.sh` 只有服务、readyz、磁盘三个维度；ADR-0005 收口条件「两条告警在 Phase 2 前落地」未勾。

## 2. 设计概要

```text
拉取（Celery Beat，每天 UTC 04:30 / 06:30 / 09:30 = 吉隆坡 12:30 / 14:30 / 17:30）：
  D = 吉隆坡今天的日期
  对每个配置币种：FxRateSource.fetch(currency, D)（事务外，超时 10 秒）
    → 有报价：同一事务里 INSERT 草稿（撞「同币种同报价日」唯一约束 = 已有，不重复）+ INSERT 拉取记录
    → BNM 答「无记录」（周末、公众假期、当天中午场尚未公布）：INSERT 拉取记录（NO_QUOTE_FOR_DATE），不写草稿
    → 失败：INSERT 拉取记录（FAILED + 错误码），不写草稿

管理端：手工建草稿 / 改 / 丢弃 → 审计（不拿全局锁）
管理端：发布 / 退役 →
  主事务第一条语句：SELECT … FROM fx_rate_locks WHERE id = 1 FOR UPDATE
  拿到锁之后取 t → 之后全是加锁读 → 区间计算 → 写 → 同一事务复查 → 审计 → commit

计费（T-G / T-H 在计费事务里调用，本任务只提供）：
  SELECT … FROM fx_rate_locks WHERE id = 1 FOR SHARE（持有到计费事务提交）
  resolve_fx_rate(session, base_currency, occurred_at)：加锁读覆盖 occurred_at 的 PUBLISHED / RETIRED 版本 → 没有 → None（FX_RATE_ERROR）

告警：GET /internal/alerts（Bearer 令牌）→ 纯文本，每行一个维度
  monitor.sh 每 5 分钟读一次 → 每个维度推一个 Healthchecks 检查
```

### 数据库（迁移序号取实现时的下一个，预计 0015）

**`fx_rate_locks`**：单行（迁移写入 `id = 1`），BEFORE INSERT（已有一行时）/ DELETE 触发器拒绝。发布 `FOR UPDATE`、计费 `FOR SHARE`，与 025 的供应商行锁同一套推理（见「发布」）。

**`fx_rate_versions`**（§74.2）

| 列 | 类型 | 说明 |
| --- | --- | --- |
| `id`、`public_id` | | |
| `base_currency` | CHAR(3)，`^[A-Z]{3}$`，CHECK ≠ `MYR` | |
| `quote_currency` | CHAR(3)，CHECK `= 'MYR'` | §17.1 |
| `rate` | DECIMAL(24,10)，CHECK > 0 | **1 单位 `base_currency` = 多少 MYR**。BNM 按 `unit` 报价（某些币种按 100 单位），入库前除以 `unit`，要求结果在 10 位小数内精确（否则拉取记为 FAILED `UNIT_NOT_EXACT`，不写草稿）；BNM 报价数字怎么还原成十进制见「BNM 适配器」；手工录入同样最多 10 位小数，不舍入 |
| `source` | VARCHAR(16)，CHECK `BNM` / `MANUAL` | ADR-0005 §1、§2 |
| `source_reference` | VARCHAR(255) | BNM：`bnm:exchange-rate:<ccy>:<报价日>:session=1200:middle_rate:unit=<n>`；手工：必填，录入者写出处 |
| `source_quote_date` | DATE NULL | BNM 的报价日（吉隆坡日期）；手工录入可空 |
| `observed_at` | DATETIME | BNM：报价日 12:00 吉隆坡 = 当日 04:00 UTC（固定 +8，马来西亚无夏令时，不依赖镜像里的 tzdata）；手工：录入者给出的观测时刻 |
| `status` | CHECK `DRAFT` / `PUBLISHED` / `RETIRED` / `DISCARDED` | `DISCARDED` 是 §74.2 之外补的：丢弃的草稿留痕 |
| `effective_from` / `effective_to` | DATETIME NULL | 半开区间；草稿为空 |
| `open_slot` | 生成列（STORED）：`CASE WHEN status = 'PUBLISHED' AND effective_to IS NULL THEN 1 END` —— 未截断的已发布版本为 1，**其余一律为 `NULL`**（不是 0） | v2 第一轮审查后写明表达式：v1 只写「时为 1」，若按布尔表达式实现，非开放行得 0，第二条非开放行（例如 A 被截断后下一天的草稿）就撞唯一约束，此后草稿一条也建不出来。MySQL 唯一约束不管 `NULL`，只有 1 参与唯一性。与 026 迁移 0013 的 `_OPEN_SLOT` 相同 |
| `created_by` | FK → `users.id` NULL | BNM 拉取为空（系统） |
| `approved_by` / `approved_at` | | 发布人与发布时刻 |
| `created_at` / `updated_at` | DATETIME | |

- UNIQUE `(base_currency, quote_currency, open_slot)`：一个币种对至多一个未截断的已发布版本。
- UNIQUE `(base_currency, quote_currency, source, source_quote_date)`：同一报价日的 BNM 草稿只有一条（MySQL 唯一约束不管 `NULL`，手工录入不受限）。
- CHECK（v2 逐条写出，与 026 / 027 同一写法）：
  1. `status IN ('DRAFT','DISCARDED')` ⇒ `effective_from`、`effective_to`、`approved_by`、`approved_at` 全为空
  2. `status IN ('PUBLISHED','RETIRED')` ⇒ `approved_by`、`approved_at` 非空
  3. `effective_from IS NULL OR effective_to IS NULL OR effective_from < effective_to OR (status = 'RETIRED' AND effective_from = effective_to)`：**空区间只能出现在 `RETIRED` 行上**
  4. **停用必有尽头**（v2，027 v4 同一条）：`status <> 'RETIRED' OR effective_to IS NOT NULL`
  5. `source = 'BNM'` ⇒ `source_quote_date IS NOT NULL`
- **触发器**（照 0006 的写法与权限预检；v2 列全）：
  - BEFORE INSERT：`NEW.status = 'DRAFT'`（版本只能经「草稿 → 发布」进入已发布，发布跃迁上的检查因此绕不过去）。
  - BEFORE UPDATE，按 `OLD.status`：
    - `DRAFT`：可改任意列（服务层限定可改字段），但 `NEW.status` 只能是 `DRAFT` / `PUBLISHED` / `DISCARDED`（v2：禁止 `DRAFT → RETIRED`，026 v4 / 027 v4 同一条。否则绕过服务直接改状态，一个没经过发布跃迁检查的版本进入 `RETIRED`，而 `RETIRED` 在自己的区间里照样被取汇率）。
    - `DRAFT → PUBLISHED`（v2，发布跃迁）：`NEW.effective_to` 必须为空。新发布的版本永远是时间线末尾、未截断（见「发布」）；这一条加上下面的区间不重叠检查，使**起点为空的版本只可能在该币种对没有任何区间非空的已发布 / 已退役版本时产生**（起点、尽头都为空的区间与任何非空区间都相交），直接写库无法在已有版本之后发布一个「一直以来」的版本，也无法把版本插进时间线中间或退役留下的空档。
    - `PUBLISHED`：除 `effective_to`、`status`（只允许 `PUBLISHED → RETIRED`）、`updated_at` 外任何列变化都 SIGNAL。
    - `RETIRED`、`DISCARDED`：**拒绝任何改动**（v2：v1 允许改 `RETIRED` 行的 `effective_to`。服务层从不改已退役的行 —— 它是终态，任何操作 409 —— 而允许改尽头就能把已退役版本的尽头清空，或改短而让已取到汇率的时刻失去汇率。026 / 027 对 `RETIRED` 的写法较宽，本设计收紧，与状态机一致）。
  - **区间不重叠**（BEFORE UPDATE，`NEW.status IN ('PUBLISHED','RETIRED')` 且 `effective_from`、`effective_to` 或 `status` 有变化时）：触发器先执行 `SELECT id INTO @lock_id FROM fx_rate_locks WHERE id = 1 FOR UPDATE`（与服务层发布同一把锁，把绕过服务的直接写入也串行起来）；本行为空区间时到此为止（空区间不匹配任何时刻，v2 写明）；否则检查同一币种对内是否存在另一行满足：状态为 `PUBLISHED` / `RETIRED`、区间非空（`effective_from IS NULL OR effective_to IS NULL OR effective_from < effective_to`）、且与本行区间相交 —— 相交判定 `COALESCE(o.effective_from, '1000-01-01') < COALESCE(NEW.effective_to, '9999-12-31') AND COALESCE(NEW.effective_from, '1000-01-01') < COALESCE(o.effective_to, '9999-12-31')`，另一行条件 `o.base_currency = NEW.base_currency AND o.quote_currency = NEW.quote_currency AND o.id <> NEW.id`；存在即 SIGNAL。
  - BEFORE DELETE：一律拒绝。
- **服务层的写入顺序**（保证正常路径过得了上面的触发器）：发布时先截断前一个（`effective_to` 写入），再把本行从草稿改为已发布；撤销预约时先把被撤销的一行改成空区间的 `RETIRED`，再把前一个恢复为未截断。
- **数据库不拦的**（与 027 v4 §10 假设 3 同一口径）：直接写库写入一个过去的**具体**起点。拦它要比较应用时钟与数据库时钟，两者不同源；`t` 只由服务层在锁后取。数据库兜底的是不依赖时钟的结构性规则：只能以草稿插入、草稿不能直接退役、新发布的版本没有尽头、区间不重叠、停用必有尽头、发布后只许截断与退役、退役后不可改。

**`fx_fetch_attempts`**（只增）

| 列 | 类型 | 说明 |
| --- | --- | --- |
| `id` | BIGINT PK | |
| `base_currency` | CHAR(3) | |
| `source` | VARCHAR(16) | `BNM` |
| `requested_date` | DATE | v2：向 BNM 要的报价日（吉隆坡当天） |
| `outcome` | CHECK `NEW_DRAFT` / `NO_NEW_QUOTE` / `NO_QUOTE_FOR_DATE` / `FAILED` | `NO_NEW_QUOTE`：取到报价，但该报价日已有 BNM 版本（同一天的第二、三次拉取，或并发拉取的输家）。`NO_QUOTE_FOR_DATE`（v2）：BNM 明确答「该日无记录」—— 周末、公众假期、或当天中午场尚未公布。三者都是**成功**，不计入失败 |
| `quote_date` | DATE NULL | 取到报价时的报价日 |
| `error_code` | VARCHAR(64) NULL | `FAILED` 时：`TIMEOUT` / `HTTP_<status>` / `BAD_PAYLOAD` / `UNIT_NOT_EXACT` / `NETWORK`；`NO_NEW_QUOTE` 时可为 `QUOTE_CHANGED`（见 §4）；不存响应体 |
| `fx_rate_version_id` | FK NULL | `NEW_DRAFT` 时指向新草稿 |
| `attempted_at` | DATETIME | |

- BEFORE UPDATE / DELETE 触发器拒绝（只增）。

### BNM 适配器（ADR-0005 §1；v2 按 2026-09-30 的实测改写）

- 接口 `FxRateSource`（Protocol）：`fetch(currency, quote_date) -> FxQuote | None`，`FxQuote(quote_date, middle_rate, unit, rate, reference)`；返回 `None` 表示来源明确答「该日无报价」；失败抛带错误码的异常。工厂函数 `build_fx_source(settings)` 是唯一决定实现的地方（照 `app/core/mailer.py` 的 `build_transport`），换来源不影响已发布版本与历史计算（§17.1）。
- BNM 实现用标准库 `urllib.request`（**不加运行时依赖**：只有这一个出站调用，标准库足够）：
  - 请求：`GET {base_url}/public/exchange-rate/{ccy}/date/{YYYY-MM-DD}?session=1200&quote=rm`，请求头 `Accept: application/vnd.BNM.API.v1+json`（实测：不带这个头，返回的是 HTML 页面），超时 `BILLING_FX_FETCH_TIMEOUT_SECONDS`（默认 10）。只接受 HTTPS 的 `base_url`（配置校验）。
  - **按日期取，不用「最新」端点**（v2）：实测「最新」端点 `…/exchange-rate/USD?session=1200&quote=rm` 在当天中午场公布之前返回 404「No records found.」，**不回退到上一交易日**（v1 §10 假设 2 不成立）；按日期取把「要哪一天」写死在请求里，响应可以逐字段核对。`{YYYY-MM-DD}` = 吉隆坡当天（UTC 时刻 + 8 小时后取日期）。
  - **200** 的响应体（形状见 §10 的录制）逐项核对，任何一项不符 → `BAD_PAYLOAD`：`data.currency_code` = 所请求币种；`data.rate.date` = 所请求日期；`meta.session = "1200"`（实测：漏传 `session` 时 BNM 返回的是 `1130` 场，那一场的 `middle_rate` 是 `null`）；`meta.quote = "rm"`；`data.unit` 是正整数；`data.rate.middle_rate` 是 JSON 数字（`null`、字符串、缺失都不行）。
  - **`middle_rate` 怎么还原成十进制**（v2）：BNM 把报价当 IEEE 754 双精度数输出，JSON 里的字面量是 17 位有效数字，例如公布值 4.0830 输出为 `4.0830000000000002`。按字面精确解析（`json.loads(..., parse_float=Decimal)`）得到 16 位小数，除以 `unit` 必然「超 10 位小数」—— **每一次拉取都会失败**。规则：先按双精度解析，再取**最短往返十进制表示**（Python `repr(float)`，即 `Decimal(repr(x))`），这正是 BNM 公布的那个四位小数数（§10 录制的六个币种全部验证过）；还原后必须 > 0、最多 **6** 位小数（BNM 公布 4 位，留 2 位余量），否则 `BAD_PAYLOAD`。之后的一切（除以 `unit`、入库、比较）都是 `Decimal`。这是全系统唯一经过 `float` 的地方，理由是来源本身就是双精度数；手工录入与管理端接口仍是字符串精确解析。
  - `rate = middle_rate / unit`，要求在 10 位小数内精确，否则 `UNIT_NOT_EXACT`（实测 `JPY`：`2.5936 / 100 = 0.025936`；`IDR`：`0.0227 / 100 = 0.000227`）。
  - **404 且响应体是 `{"message":"No records found.","code":404}`**（按 JSON 解析后 `code = 404` 且 `message = "No records found."`）→ 返回 `None`，任务记 `NO_QUOTE_FOR_DATE`。实测周六（2026-09-26）、国庆日（2026-08-31）、当天中午场公布之前、**以及不存在的币种代码**都是这个响应 —— 所以币种配错不会被当成拉取失败，由 `fx_stale`（「没有生效的版本」即 P2）兜住，见「两个告警维度」。其他 404（响应体不是这个形状）→ `FAILED HTTP_404`。
  - 其余 HTTP 状态 → `HTTP_<status>`；超时 → `TIMEOUT`；连接失败 → `NETWORK`；响应体不是 JSON → `BAD_PAYLOAD`。
- 配置：`BILLING_FX_SOURCE`（默认 `bnm`）、`BILLING_FX_BNM_BASE_URL`（默认 BNM 公开地址，即 ADR-0005 写的 `api.bnm.gov.my`，HTTPS）、`BILLING_FX_CURRENCIES`（默认 `USD`，逗号分隔、大写三字母、不得含 `MYR`）、`BILLING_FX_FETCH_TIMEOUT_SECONDS`。
- **Beat：一天三次**（v2）`crontab(minute=30, hour="4,6,9")`（Celery 时区是 UTC；= 吉隆坡 12:30 / 14:30 / 17:30），每条 `expires` 3600 秒。中午场的公布时刻没有公开的保证，一天只拉一次时，公布晚于 12:30 的那天就拿不到草稿（该次记 `NO_QUOTE_FOR_DATE`、不告警），要等 `fx_stale` 在五天后才发现；三次拉取由报价日唯一约束保证至多一条草稿，后两次记 `NO_NEW_QUOTE`。任务幂等，可以手工再触发（管理员或运维在 worker 容器里调用，参数同 Beat）。
- 出站调用在事务外；写草稿与写拉取记录在同一事务里。拉取成功但写库失败 → 任务报错，下一次运行重新拉取（草稿唯一约束保证不重复）。
- **热路径绝不调用 BNM**：`resolve_fx_rate` 只读库（ADR-0005 §4）。拉取失败时，已发布版本照旧生效（只要它没被截断），计费不受影响。

### 状态

| 当前 | 操作 | 新状态 | 说明 |
| --- | --- | --- | --- |
| — | BNM 拉取 / 手工录入 | `DRAFT` | 手工：`rate`、`observed_at`、`source_reference` 必填 |
| `DRAFT` | 编辑（仅手工来源） | `DRAFT` | BNM 草稿不可编辑（要改就丢弃后手工录入，出处不同）→ 409 `FX_RATE_NOT_EDITABLE` |
| `DRAFT` | 丢弃 | `DISCARDED` | |
| `DRAFT` | 发布 | `PUBLISHED` | 见「发布」 |
| `DRAFT` | 退役 | — | 409 `FX_RATE_NOT_RETIRABLE`（草稿用丢弃，v2 写明） |
| `PUBLISHED`（未截断） | 退役 | `RETIRED` | 见「退役」 |
| `PUBLISHED`（已截断） | 退役 | — | 409 `FX_RATE_NOT_RETIRABLE` |
| `PUBLISHED` | 编辑 / 丢弃 | — | 409 `FX_RATE_NOT_DRAFT` |
| `PUBLISHED` | 发布 | `PUBLISHED` | 200，不写；请求体里的 `effective_from` 被忽略（v2 写明） |
| `RETIRED` / `DISCARDED` | 任何写操作 | — | 409 `FX_RATE_FINAL` |

### 发布：区间怎么定（v2 重写）

按币种对，每对是一条只在末尾追加的时间线。

**术语**（v2，与 026 v4 相同）：
- 区间 `[effective_from, effective_to)` 满足 `effective_from IS NULL OR effective_to IS NULL OR effective_from < effective_to` 的叫**非空区间**；`effective_from = effective_to` 的叫**空区间**，永不匹配任何时刻，只出现在 `RETIRED` 行上（CHECK 3）。
- **时间线**：该币种对里区间非空的 `PUBLISHED` / `RETIRED` 行。**空区间的行不在时间线上**：不当末尾版本 L、不算「有过版本」、不进事务内复查的排序、不参与数据库的重叠判定。
- **末尾版本 L**：时间线里 `effective_from` 最晚的一行（为空视为最早）。时间线上的行互不相交，所以 L 唯一。
- **`t`**：拿到 `fx_rate_locks` 排他锁之后取服务端当前 UTC 时间 `now`，截到整秒再加 1 秒（`now` 恰好是整秒时也加 1 秒，与 025 起的 `boundary_after` 相同），所以 `t` 严格晚于 `now`。
- 请求可带预约时刻 F（RFC 3339，必须带时区，换成 UTC 后必须是整秒，否则 422 `VALIDATION_ERROR`）。**F < `t` → 422 `EFFECTIVE_FROM_IN_PAST`，先于下表的任何判断。**

| 行 | L 的情形 | 请求未给 F | 请求给了 F（已保证 F ≥ `t`） |
| --- | --- | --- | --- |
| P1 | 没有 L（时间线为空） | 本版本 `[NULL, NULL)`（一直以来） | 本版本 `[F, NULL)` |
| P2 | L 未截断、已开始（L 起点为空或 < `t`） | 本版本 `[t, NULL)`；L 的 `effective_to = t` | 本版本 `[F, NULL)`；L 的 `effective_to = F`（F ≥ `t` > L 起点，恒成立） |
| P3 | L 未截断、尚未开始（L 起点 ≥ `t`：预约的，或同一秒里刚发布的） | **409 `EFFECTIVE_FROM_CONFLICT`，什么都不写**（v2） | F > L 起点：本版本 `[F, NULL)`，L 的 `effective_to = F`；F ≤ L 起点：409 `EFFECTIVE_FROM_CONFLICT` |
| P4 | L 有尽头 E（L 已退役；或 L 是 `PUBLISHED`、它的后继已被退役成空区间） | 本版本 `[E, NULL)`（首尾相接，补上空档） | 本版本 `[F, NULL)`，`[E, F)` 保持无汇率 |

- 「L 未截断」的 L 必然是 `PUBLISHED`（CHECK 4：已退役必有尽头）。
- **P4 里 E ≤ `t` 恒成立**：E 要么是某次退役时的 `t`，要么是某个起点恰好等于退役时 `t` 的版本的起点（见「退役」R1），都不晚于此后任何一次操作的 `t`。所以 P4 未给 F 时是对过去生效，覆盖的恰好是 `[E, t)` 这段退役后无汇率的空档；给了 F 时 F ≥ `t` ≥ E。v1 的「F 必须 = E 或 ≥ `t`」因此简化为统一的「F ≥ `t`」：要补空档就不给 F（v2 写明；v1 的两句在 F = E < `t` 时互相矛盾）。
- **P3 为什么 409 而不自动推后**（v2，修 v1 的缺陷，与 026 v4 相同）：v1 这一格写「未给 → `t`，截断 L 于 `t`」，但 L 起点 ≥ `t`，截断得到起点晚于终点的倒挂区间（L 是预约）或空区间（L 是同一秒刚发布的）的**已发布**行，CHECK 3 必然拒绝 —— 这一格在 v1 里没有合法结果。汇率从哪一刻生效应由管理员明确：先撤销预约，或指定一个晚于 L 起点的时刻；同一秒里连续两次不给时刻的发布，第二次 409，下一秒重发即可。
- **汇率从发布时刻起生效，不从报价日起算**（§10 第 1 条，Kelvin 2026-09-29 已同意）：BNM 报价日 D 的草稿最早在 D 日 12:30 吉隆坡才存在，发布更晚；若让它从 D 日 0 点起生效，就是回溯 —— D 日上午已按前一个汇率计费的事件，重算时会取到新汇率，违反 ADR-0005「同一事件重算得出相同 MYR」。`observed_at` / `source_quote_date` 记录报价日，供审计与过期判断；ADR-0005 §4「按 `occurred_at` 落在哪个已发布区间」的规则不变。
- **对过去生效只有 P1 与 P4（未给 F）两处，覆盖的时刻此前一律取不到汇率**：P1 的时间线为空（从未有过区间非空的版本；只发布过又被撤销的预约，它们从未匹配过任何时刻）；P4 覆盖的是退役后的空档。这些时刻的事件是 `FX_RATE_ERROR`、从未扣费（§17.1），补上只把「无汇率」变成「有汇率」，已取到汇率的时刻结果不变。这正是 §17.1「历史汇率缺失时必须手工录入」的做法（v1 已批准，v2 只把「从没有」精确成「时间线为空」）。与 026 不同，汇率在退役后**回填**空档：汇率缺失的补救就是事后录入，而价格的退役表示「此后不按这个价算」。
- **与计费不会交错**：发布在排他锁内、`t` 在锁后取；计费在共享锁内，只处理 `occurred_at ≤` 持锁后当前时间的事件（025 的 T-H 契约）。计费先 → 事件 `occurred_at` < `t` ≤ 任何新边界；发布先 → 计费读到新表。加上「只在末尾追加 / 只有无汇率变有汇率」，**一个（币种, 时刻）一旦取到某个版本，以后永远取到同一个**。

**例子**（v2；日期都是 2026-10-01，时刻都是 UTC；「F=次日 04:30」即 2026-10-02 04:30:00。每个例子是一个测试用例，见 §7）：

| 例 | 之前 | 操作 | 之后 |
| --- | --- | --- | --- |
| E1（P1，空集合） | 只有草稿 A | 10:00:00.4 发布 A，不给 F（`t` = 10:00:01） | A `PUBLISHED [NULL, NULL)` |
| E2（P1，给 F） | 只有草稿 A | 发布 A，F=次日 04:30 | A `[次日 04:30, NULL)`；此前的时刻仍取不到汇率 |
| E3（P1，只有空区间行） | X `RETIRED [次日 04:30, 次日 04:30)`（撤销过的预约），草稿 A | 发布 A，不给 F | A `[NULL, NULL)`（空区间不算「有过版本」） |
| E4（P2） | A `[NULL, NULL)` | 10:05:00.2 发布 B，不给 F（`t` = 10:05:01） | A `[NULL, 10:05:01)`，B `[10:05:01, NULL)` |
| E5（P2，预约） | A `[NULL, NULL)` | 发布 B，F=次日 04:30 | A `[NULL, 次日 04:30)`，B `[次日 04:30, NULL)` |
| E6（P3，同一秒两次） | E4 之后 | 同一秒 10:05:00.7 发布 C，不给 F（`t` = 10:05:01 = B 起点） | 409 `EFFECTIVE_FROM_CONFLICT`，什么都不写 |
| E7（P2，下一秒重发） | E4 之后 | 10:05:01.1 发布 C，不给 F（`t` = 10:05:02） | B `[10:05:01, 10:05:02)`，C `[10:05:02, NULL)` |
| E8（P3，预约存在） | E5 之后，当前 11:00 | 发布 C：① 不给 F ② F=次日 04:30 ③ F=10-03 04:30 | ① ② 409，什么都不写 ③ B `[次日 04:30, 10-03 04:30)`，C `[10-03 04:30, NULL)` |
| E9（P4，补空档） | A `[NULL, 10:05:01)`，B `RETIRED [10:05:01, 12:00:01)` | 15:00:00.5 发布 C：① 不给 F ② F=次日 04:30 | ① C `[12:00:01, NULL)`（12:00:01–15:00:01 之间此前取不到汇率的事件，此后取到 C）② C `[次日 04:30, NULL)`，`[12:00:01, 次日 04:30)` 保持无汇率 |
| E10（P2，F < `t`） | A `[NULL, NULL)`，当前 11:00 | 发布 B，F=09:00 | 422 `EFFECTIVE_FROM_IN_PAST` |

### 退役（v2 重写）

退役在同一把锁内、`t` 同上。被退役的版本记作 V：

| 行 | V 的情形 | 做什么 |
| --- | --- | --- |
| R1 | V 是 L、未截断、已开始（起点为空或 **≤** `t`） | V 改为 `RETIRED`，`effective_to = t`：`t` 起该币种事件 `FX_RATE_ERROR`，直到下一个版本（P4）从 `t` 起补上。V 起点恰好等于 `t`（同一秒里发布又退役）时得到空区间 `[t, t)`，**不恢复任何版本** |
| R2 | V 是 L、未截断、尚未开始（起点 **>** `t`：撤销预约） | 先把 V 改为 `RETIRED`、`effective_to = effective_from`（空区间）；再找 P = 时间线里起点早于 V 的最后一个版本，**只有 P 是 `PUBLISHED` 且 `P.effective_to` 等于 V 的起点时**，把 `P.effective_to` 恢复为 `NULL`；否则不动 P |
| R3 | V 是 `PUBLISHED` 但已截断（历史版本，或尽头来自一个被退役成空区间的后继） | 409 `FX_RATE_NOT_RETIRABLE`：它的区间已经结束。纠正已计费事件用的汇率不在本任务：走 Phase 8 reprocess |
| R4 | V 是草稿 | 409 `FX_RATE_NOT_RETIRABLE` |
| R5 | V 已退役 / 已丢弃 | 409 `FX_RATE_FINAL` |

- **R1 与 R2 的界线是「起点 > `t`」**（v2 写明，v1 只写了「已生效 / 尚未生效」）：起点恰好等于 `t` 只可能是同一秒里刚发布的版本；归入 R1 与 026 v4 一致。两种归法都安全 —— `t` 及之后的时刻还没有任何事件被计费 —— 选 R1 是因为它不改任何其他行。
- **恢复 P 的条件**（v2，修 v1 的缺陷）：v1 写「前一个版本恢复为未截断」，没有条件。P 若以**退役**结束（E9 ② 那样：先退役、再从未来某刻预约了 V），v1 会把一个 `RETIRED` 行的尽头清空 —— 它从此无尽头，悄悄覆盖了退役后本该 `FX_RATE_ERROR` 的空档，而 v1 的触发器允许改 `RETIRED` 行的 `effective_to`。v2 只恢复被 V 截断的 `PUBLISHED` 行；数据库层面 CHECK 4（停用必有尽头）与「`RETIRED` 不可改」两道各自挡住这件事。
- 恢复 P 的 `effective_to` 只影响 `occurred_at ≥ V 的起点 > t` 的时刻，按「与计费不会交错」的证明，这些时刻还没有事件被计费，所以安全。
- **写入顺序**：先 V、再 P（重叠触发器因此在每一步都看不到相交）。

**例子**（接上表的写法）：

| 例 | 之前 | 操作 | 之后 |
| --- | --- | --- | --- |
| E11（R1） | A `[NULL, 10:05:01)`，B `[10:05:01, NULL)` | 12:00:00.3 退役 B（`t` = 12:00:01） | B `RETIRED [10:05:01, 12:00:01)`；A 不变；12:00:01 起取不到汇率 |
| E12（R1，起点 = `t`） | E4 之后（B 在 10:05:00.2 发布） | 同一秒 10:05:00.6 退役 B（`t` = 10:05:01） | B `RETIRED [10:05:01, 10:05:01)`（空区间）；A 仍 `[NULL, 10:05:01)`，不恢复；此后 L = A（尽头 10:05:01），下一次不给 F 的发布按 P4 从 10:05:01 起 |
| E13（R2，撤销截断了 P 的预约） | E5 之后，当前 11:00 | 退役 B；然后 11:10:00.2 发布 C，不给 F（`t` = 11:10:01） | 退役后：B `RETIRED [次日 04:30, 次日 04:30)`，A `[NULL, NULL)`。发布后：L = A（B 不在时间线上）→ P2：A `[NULL, 11:10:01)`，C `[11:10:01, NULL)`。v1 若把 B 当 L，C 会从 B 的「尽头」起、与重新打开的 A 相交，这次发布必然失败 |
| E14（R2，P 以退役结束） | E9 ② 之后：A `[NULL, 10:05:01)`，B `RETIRED [10:05:01, 12:00:01)`，C `[次日 04:30, NULL)`；当前 16:00 | 退役 C | C `RETIRED [次日 04:30, 次日 04:30)`；B 不变（`RETIRED` 不恢复，v1 会把 B 的尽头清空）。此后 L = B（尽头 12:00:01），下一次不给 F 的发布按 P4 从 12:00:01 起补空档 |
| E15（R2，前面没有版本） | 只有 A `[次日 04:30, NULL)`（E2 之后），当前 11:00 | 退役 A | A `RETIRED [次日 04:30, 次日 04:30)`；时间线为空，下一次不给 F 的发布按 P1 从「一直以来」起 |
| E16（R3 / R4 / R5） | E4 之后的 A（已截断）；E12 之后的 A（`PUBLISHED`，尽头来自空区间后继）；一个草稿；E11 之后的 B | 分别退役 | 409 `FX_RATE_NOT_RETIRABLE` ×3；409 `FX_RATE_FINAL` |

### 锁在前（v2，026 v4 同一条规则）

MySQL 的 REPEATABLE READ 在事务里第一次普通读时建立快照，之后的普通读都读那个快照。发布与退役若在拿锁之前做过一次普通读，锁后的普通读就可能是等锁之前的旧数据（例如看不到等锁期间另一个管理员刚发布的版本，按旧的 L 计算区间）。规则：

1. 发布、退役的主事务**第一条语句**是 `SELECT … FROM fx_rate_locks WHERE id = 1 FOR UPDATE`（锁是全局的，不需要像 026 那样先在另一个事务里查所属供应商）。
2. 锁之后**每一次读都是加锁读**：被操作的版本行 `FOR UPDATE`（与改草稿、丢弃对版本行的 `FOR UPDATE` 串行）；同一币种对的 `PUBLISHED` / `RETIRED` 行 `FOR UPDATE`；响应与审计用到的用户 `FOR SHARE`。
3. 建草稿、改草稿、丢弃不拿全局锁（草稿不参与计费），只对版本行 `FOR UPDATE`；发布会在锁后重新加锁读草稿的全部内容。

### 事务内复查（v2 写明）

每次发布 / 退役写完后，在同一事务里加锁读取该币种对全部 `PUBLISHED` / `RETIRED` 行，断言：① 空区间的行都是 `RETIRED`；② 其余（时间线上的）按 `effective_from` 排序（为空的最早），只有第一个的 `effective_from` 可为空；相邻两个 `前.effective_to` 为空时它必须是最后一个，非空时 `≤ 后.effective_from`（等号 = 首尾相接，小于 = 退役留下的空档）；③ 至多一个 `effective_to` 为空。不满足就回滚（防实现错误，500）。v1 的「只有第一条起点可为空；相邻首尾相接或有退役空档」没说空区间怎么排：把 E13 退役后的 B 排进去，重新打开的 A 排在它前面、尽头为空却不是最后一个，正常的撤销预约路径必然回滚（026 v3 的同一个缺陷）。

### 接口

管理端（只有 ADMIN，第一条语句 `require_admin`，加进路由枚举；id 用 `public_id`；分页 §108；`rate` 是 JSON 字符串、精确解析、最多 10 位小数）：

| 方法与路径 | 作用 |
| --- | --- |
| `GET /api/v1/admin/fx-rates` | 版本列表（即 §58 的草稿、审批与版本历史），可按币种、`status`、`source` 筛 |
| `POST /api/v1/admin/fx-rates` | 手工建草稿：`base_currency`、`rate`、`observed_at`、`source_reference` |
| `GET /api/v1/admin/fx-rates/{fx_rate_id}` | 详情 |
| `PATCH /api/v1/admin/fx-rates/{fx_rate_id}` | 只对手工来源的草稿：`rate`、`observed_at`、`source_reference` |
| `POST /api/v1/admin/fx-rates/{fx_rate_id}/publish` | 发布：可选 `effective_from` |
| `POST /api/v1/admin/fx-rates/{fx_rate_id}/retire` | 退役：必填 `reason` |
| `POST /api/v1/admin/fx-rates/{fx_rate_id}/discard` | 丢弃草稿 |
| `GET /api/v1/admin/fx-rates/fetch-attempts` | 最近的拉取记录（§58「同步状态」）；路由须先于 `{fx_rate_id}` 注册，否则 `fetch-attempts` 被当成 id |

- 响应里的发布人、创建人用登录邮箱（`approved_by_email` / `created_by_email`），不返回内部用户 id（v2 写明，与 026 相同）。
- 列表的 `status` / `source` 不是合法取值 → 422。

错误码与价格版本同构：`FX_RATE_NOT_FOUND`（404）；`FX_RATE_NOT_DRAFT`、`FX_RATE_NOT_EDITABLE`（BNM 草稿）、`FX_RATE_NOT_RETIRABLE`、`FX_RATE_FINAL`、`EFFECTIVE_FROM_CONFLICT`（409）；`EFFECTIVE_FROM_IN_PAST`、`VALIDATION_ERROR`（422）。各自的触发条件见「状态」「发布」「退役」三张表。

内部告警接口：

- `GET /internal/alerts`，**不在 `/api/` 下**，不套 §107 信封：它是给 `monitor.sh` 的机器接口，返回 `text/plain`，每行 `<维度> <OK|P1|P2> <一句摘要>`，例如 `fx_fetch OK last success 2026-09-29 (USD)`、`fx_stale P2 USD rate in effect was quoted 2026-09-24, 6 calendar days old`（v2：v1 的例子写成了「working days」，阈值是日历日）。
- **两层访问控制**：
  1. nginx：`location = /internal/alerts` 与 `/readyz` 同一份 allow 名单（回环、本栈网段、proxy_net），其余 deny；另加 `location /internal/ { deny all; }`，防止以后在 `/internal/` 下新增路径时忘了保护。
  2. 应用：`Authorization: Bearer <令牌>`，令牌从 `BILLING_MONITOR_TOKEN_FILE` 所指的文件读（Compose `secrets:` 文件，照 `smtp_password_file` 的先例；compose 直接把容器内路径写进服务环境，不经 `.env`），常量时间比较（`hmac.compare_digest`）。需要第二层是因为 proxy_net 上还有同机其他项目的容器（`deploy/nginx/billing.conf` 的注释）。
- **令牌文件的每种状态**（v2 写明）：

  | `BILLING_MONITOR_TOKEN_FILE` | 文件 | 接口 |
  | --- | --- | --- |
  | 空（未配置） | — | 404（等于未启用），不记日志 |
  | 已配置 | 读不出（不存在、是目录 —— compose 缺 `file:` secret 时实测挂成空目录 —— 或无权限） | 404，每次请求记一条 ERROR 日志（不含路径与内容） |
  | 已配置 | 去首尾空白后为空或少于 32 个字符 | 404，同上记 ERROR |
  | 已配置 | 可用 | 缺 `Authorization`、不是 `Bearer`、令牌不对 → 401，不说明原因；对 → 200 |

  每次请求重新读文件（轮换令牌不需要重启）。令牌判定先于任何数据库查询。配置错误的结果是接口 404 → `monitor.sh` 不推告警维度 → Healthchecks 宽限期后报「未上报」，所以不会静默。生成：`openssl rand -hex 32`（64 个字符）。
- 响应只有计数、日期与币种，不含租户、金额或任何个人数据。
- 维度注册表：`app/services/alerts.py` 里一个有序的维度列表，每个维度是一个只读查询函数；T-E、T-H 往里加，不改接口形状。

### 两个告警维度（ADR-0005 §6）

| 维度 | 何时告警 | 分级 |
| --- | --- | --- |
| `fx_fetch` | 某个配置币种在最近 72 小时内**没有任何**成功的拉取记录（`NEW_DRAFT`、`NO_NEW_QUOTE` 或 `NO_QUOTE_FOR_DATE`），且至少有一条 `FAILED` —— 即「连续 3 天拉取失败」。周末、假期、中午场晚公布时 BNM 答「无记录」，记为 `NO_QUOTE_FOR_DATE`（成功），不会误报。72 小时内一条记录都没有 → OK（从未运行或 Beat 停了，由 `fx_stale` 负责） | P2 |
| `fx_stale` | 某个配置币种**当前生效**的版本（覆盖当前时刻的那一个，与 `resolve_fx_rate` 同一规则），其报价日（`source_quote_date`，手工录入取 `observed_at` 的吉隆坡日期）距今超过 **5 个日历日**（按吉隆坡日期相减；§10 第 2 条，Kelvin 2026-09-29 选定）；或该币种根本没有生效的版本 | P2 |

`monitor.sh`：新增 `check_alerts`（`curl -fsS -m 10 -H "Authorization: Bearer $(cat 令牌文件)"`，令牌文件的宿主机路径由 `.env` 的 `BILLING_MONITOR_TOKEN_HOST_FILE` 给出，字面解析、不 source），逐行解析，每个维度用 `BILLING_HEALTHCHECK_ALERT_<维度大写>_URL` 推一个检查；缺配置的维度只记日志（沿用 `heartbeat` 的做法）。接口不通时**不推**这些维度（readyz 维度已报同一个原因，避免双响），Healthchecks 的宽限期到了会自行报「未上报」。复核（`RECHECK_SECONDS`）一并覆盖。

**`deploy/config_snapshot.sh`**（v2，预审的 SCOPE_GAP）：它上传前的自查把每个心跳地址当凭据（知道地址就能伪造「成功」），但地址是**逐个列举**的；新增的 `BILLING_HEALTHCHECK_ALERT_FX_FETCH_URL` / `…_FX_STALE_URL` 不会被查。改为从 `.env` 里取出所有匹配 `^BILLING_HEALTHCHECK_[A-Z0-9_]*_URL=` 的键逐个自查，不再手写清单 —— 于是 T-E、T-H 以后只在 `.env` 加地址（本节「对下游任务的契约」的约定），不必再改这个脚本，不会重演这个缺口。`tests/backend/test_deploy.py` 的对应用例同步改（它现在断言清单里有两个具体键名）。改动归 §11 的部署任务，路径加进它的 `allowed_change_paths`。

Healthchecks 用量：现有 8 个，本任务 +2，T-E +1，T-H 计划 +5，共 16 个，免费档上限 20。

### 事务边界

| 路径 | 事务 |
| --- | --- |
| 拉取 | 外部调用在事务外；草稿与拉取记录同一事务 |
| 手工草稿 / 编辑 / 丢弃 | 一个事务，含审计；不拿全局锁，版本行 `FOR UPDATE` |
| 发布 / 退役 | 锁（第一条语句）→ `t` → 加锁读 → 写 → 复查 → 审计 → 提交 |
| 告警接口 | 只读 |

计费路径的锁顺序（写进 T-H 契约）：供应商（S，025）→ **FX（S，本任务）** → 定价规则（S，T-D）→ 钱包 → 租户。发布路径各只拿自己那一把锁，不形成环。

### 外部系统与异步边界

- BNM：只在 Beat 任务里调用，失败只影响草稿与告警，不影响计费。
- 生产 celery-worker 有出站网络（SMTP 已在用）；到 BNM 的 HTTPS 是否可达要在部署后实测一次（§10 假设 3）。

### 时间语义

- 区间、`occurred_at`、`t`：UTC 无时区，半开 `[from, to)`。`t` 的取法见「发布」的术语；`created_at`、`updated_at`、`approved_at`、`attempted_at` 取服务端时间截到整秒。
- 报价日与「吉隆坡今天」：UTC 时刻加固定 8 小时后取日期（不依赖 tzdata）。
- 过期判断：吉隆坡「今天」的日期减去报价日（日历日），> 5 即过期。5 天覆盖「周末 + 两到三天公众假期」这样的长假而不误报；不维护公众假期表。

### 金额精度

- `rate`：DECIMAL(24,10)；精确除以 `unit`，不舍入；超精度拒绝。BNM 报价的还原见「BNM 适配器」（最短往返十进制表示、最多 6 位小数）；管理端接口的 `rate` 是字符串精确解析、最多 10 位小数。
- §80 的换算（供应商原币种成本 × `rate`）、高精度中间值与一次舍入归 T-G；T-G 把 `fx_rate_applied` 存成本任务的 `rate` 原值（同精度）。

### 审计

| `action` | 什么时候 |
| --- | --- |
| `FX_RATE_CREATE` | 手工建草稿（BNM 草稿不写审计：拉取记录就是它的来源证明，操作者是系统） |
| `FX_RATE_UPDATE` | 改手工草稿 |
| `FX_RATE_DISCARD` | 丢弃 |
| `FX_RATE_PUBLISH` | 发布；前：被截断的前一个版本的 `public_id` 与原 `effective_to` |
| `FX_RATE_RETIRE` | 退役；`reason`；被恢复的前一个版本（若有） |

`entity_type = fx_rate_version`、`entity_id = public_id`，归进 `app/services/audit_query.py` 的 `PUBLIC_ENTITY_TYPES`（否则 `test_every_written_entity_type_is_classified` 在 CI 变红，与 026 / 027 同一情形）。五个动作都不在 §66 清单里，按先例补上并记进 `docs/TODO.md`。

### 对下游任务的契约

| 下游 | 契约 |
| --- | --- |
| T-G | `source_currency = MYR` 不查汇率；否则 `resolve_fx_rate` 返回 `None` → `FX_RATE_ERROR`，不扣费；换算按 §80（高精度、只在最终计费额舍入一次） |
| T-H | 计费事务里在 `resolve_model` 之后取 `fx_rate_locks` 的 `FOR SHARE`，汇率用加锁读；`fx_rate_version_id` 与 `fx_rate_applied` 写进快照；锁顺序见上；新增告警维度注册到 `app/services/alerts.py` |
| T-E | 冲突维度注册到同一注册表，`monitor.sh` 与 `config_snapshot.sh` 无需改动（按行、按键名模式通用处理），只需在 `.env` 加对应的 Healthchecks 地址 |
| T-K | 汇率页标明「中间价、吉隆坡中午场、从发布时刻起生效」；预约存在时不给时刻发布会 409，界面提示先撤销预约或指定更晚的时刻 |

## 3. 不变量影响矩阵

| 不变量 | 是否触碰 | 可能怎样被破坏 | 设计控制 | 验证方式 |
| --- | --- | --- | --- | --- |
| INV-1 中心故障不中断 AI | 否（间接） | BNM 故障拖垮计费 | 热路径不调 BNM；拉取失败只影响草稿 | 拉取失败时 `resolve_fx_rate` 照常的用例 |
| INV-2 / INV-3 / INV-4 / INV-5 | 否 | 不碰钱包与账本 | — | — |
| INV-6 事件保留版本引用 | **是** | ① 已发布版本被改；② 发布与计费交错；③ 回溯发布（从报价日起算）让已计费时刻换汇率；④ 撤销预约把已退役版本的尽头清空，已退役的汇率悄悄覆盖空档（v2）；⑤ 绕过服务直接写库：草稿直接退役、已有版本之后发布「一直以来」、退役不写尽头、改已退役版本的尽头（v2）；⑥ 发布 / 退役按锁前的旧快照计算区间（v2） | ① 触发器；② 单行锁串行、`t` 锁后取、加锁读；③ 从发布时刻起生效，只有「无汇率 → 有汇率」可以覆盖过去（P1、P4）；④ R2 只恢复被截断的 `PUBLISHED` 行；⑤ 禁止 `DRAFT → RETIRED`、发布跃迁上尽头为空 + 区间不重叠、CHECK 4、`RETIRED` 不可改；⑥ 锁在前 | 触发器、并发、性质用例；§2 例子逐条的用例；直接写库用例（§7） |
| INV-7 客户不可见成本毛利 | **是**（间接） | 汇率与成本经客户接口泄露；告警接口泄露业务数据 | 只有管理端与带令牌的内部接口；告警行不含金额 | 越权与内容断言 |
| INV-8 租户不可互访 | 否 | 全局数据 | — | — |
| INV-9 | 否 | — | — | — |
| INV-10 金额用 Decimal | **是** | `rate` 经 `float` 失真；`unit` 除法被舍入；BNM 的双精度字面量被当成精确值（v2：每次拉取都判超精度） | 管理端字符串精确解析；BNM 报价取最短往返表示后转 `Decimal`、限 6 位小数，之后全程 `Decimal`；精确除法，不精确即失败 | 精度、`unit` 与录制响应的用例 |
| INV-11 / INV-12 | 否 | — | — | — |
| INV-13 状态与事件原子提交 | **是** | 发布了而审计没写；草稿写了而拉取记录没写 | 同一事务 | 回滚用例（SQLite 与 MySQL 各一次） |
| INV-14 队列丢失不毁持久工作 | 否（间接） | Beat 丢一次调度 | 一天三次，下一次调度照常；告警维度看 72 小时窗口 | — |

**本任务自己的不变量**：同一币种对在任一时刻至多一个版本生效；一个（币种, 时刻）一旦取到某个版本，以后永远取到同一个版本（除非那一刻原本无汇率）。

## 4. 状态与并发

状态表见 §2。

- **串行化**：发布 / 退役在 `fx_rate_locks` 排他锁内，锁是主事务第一条语句、之后全是加锁读；计费在同一行共享锁内。草稿操作只对版本行 `FOR UPDATE`（与发布对版本行的 `FOR UPDATE` 串行）。并发拉取（手工重触发与定时同时跑）由 `(base, quote, source, source_quote_date)` 唯一约束决出一条草稿，输家记 `NO_NEW_QUOTE`。
- **数据库保证的唯一性**：`(base_currency, quote_currency, open_slot)`；`(base_currency, quote_currency, source, source_quote_date)`；`public_id`。
- **幂等键**：拉取靠报价日唯一约束；发布已发布 → 200 不写。
- **相同 ID、不同载荷**：同一报价日 BNM 再次返回不同的价（BNM 更正）→ 已有版本不变，记 `NO_NEW_QUOTE` 并在拉取记录的 `error_code` 写 `QUOTE_CHANGED`；比较的是还原后的 `rate`（`Decimal` 数值相等）。管理员看到后可丢弃旧草稿、手工录入。
- **重试层**：Beat 每日三次；失败不在任务内重试（下一次调度或手工重触发）。
- **原子提交**：版本 + 截断 / 恢复 + 复查 + 审计；草稿 + 拉取记录。

## 5. 失败与恢复矩阵

| 失败点 | 对外结果 | 数据最终状态 | 是否可重试 | 恢复来源 | 告警 |
| --- | --- | --- | --- | --- | --- |
| BNM 超时 / 网络 / HTTP 错误 / 格式变了 | — | 拉取记录 `FAILED`，无草稿；已发布版本照旧生效 | 下一次调度或手工 | BNM 恢复；或手工录入 | 72 小时无成功 → `fx_fetch` |
| BNM 答「无记录」（周末 / 假期 / 中午场尚未公布） | — | `NO_QUOTE_FOR_DATE` | 当天后两次调度 | — | 不告警（持续则 `fx_stale`，阈值 5 个日历日） |
| 币种配错（BNM 对不存在的代码也答「无记录」） | — | 每次 `NO_QUOTE_FOR_DATE` | — | 改配置 | `fx_stale`（该币种没有生效的版本 → 立即 P2） |
| 管理员忘了发布 | — | 旧版本继续生效 | — | 发布 | `fx_stale` |
| 草稿写库失败 | 任务报错 | 无草稿、无记录 | 下一次调度 | — | 错误日志；持续则 `fx_fetch` |
| 预约存在时不给时刻发布、F 不晚于预约起点 | 409 | 无改动 | 撤销预约或指定更晚的时刻 | — | 无 |
| 发布时复查不通过 | 500 | 回滚 | 否 | 修代码 | 错误日志 |
| 发布锁超时 | 500 | 回滚 | 是 | — | 错误日志 |
| 从未发布过汇率 | — | 非 MYR 事件 `FX_RATE_ERROR` | 发布后由 T-H 重新入队 | — | `fx_stale`（无生效版本）+ T-H 的汇率错误维度 |
| 发布了错误汇率 | — | 已引用则不可改 | 退役 + 发布正确版本（从 `t` 起补上）；历史纠正走 Phase 8 | 审计 | 无 |
| 告警接口不通 | — | — | — | — | readyz 维度；Healthchecks 宽限期后报「未上报」 |
| 令牌未配置 / 文件读不出 / 内容过短 | 接口 404（后两种记 ERROR 日志） | — | 配置后生效（不需重启） | — | 各告警维度未上报 |
| 触发器被绕过的直接 SQL | 数据库拒绝 | 不变 | — | — | — |

## 6. 数据与安全边界

- **租户过滤**：不适用（全局数据）。
- **鉴权主体**：管理端接口只有 ADMIN；告警接口是 nginx 网段白名单 + Bearer 令牌（令牌不是管理员凭据，只能读告警行）。
- **禁止返回的字段**：内部 id；告警行不含金额、租户与个人数据。
- **日志 / 审计 / 异常**：拉取失败只记错误码，不记 BNM 响应体；令牌不进日志、不进 `.env`（`.env` 里只有令牌文件的宿主机路径）；令牌文件读不出时的日志不含路径。
- **密钥**：监控令牌是新的密钥文件，属主 `10001`、权限 `0400`（与其他 secrets 同法）；宿主机上 `monitor.sh` 读同一份文件的宿主机路径。部署步骤写进 `docs/deployment.md` 与 `docs/runbook.md`。
- **prompt / response**：不涉及。
- **保留与删除**：汇率版本与拉取记录都不删除。
- **公开仓库**：§10 录制的 BNM 响应是公开的汇率数据，可以进仓库；测试里自造的汇率与金额用明显的虚构值。

## 7. 测试证据计划

| 风险/需求 | 测试层级 | 场景 | 预期结果 |
| --- | --- | --- | --- |
| 发布与退役的规则（v2） | integration（SQLite 与 MySQL 各一次） | §2 例子 E1–E16 **各一个用例**：经服务层、冻结时钟建「之前」的数据，执行「操作」，断言「之后」的每一行（状态、区间）与响应码；E9 ①、E13 另断言 `resolve_fx_rate` 对空档内 / 预约起点之后的时刻前后的结果 | 与例子逐字一致 |
| BNM 解析 | unit（§10 的录制响应，不联网） | USD 中午场；JPY（`unit = 100`）；`1130` 场（`middle_rate` 为 `null`、`meta.session` 不符）；「无记录」404；由录制派生：`data.rate.date` / `currency_code` 不符、`middle_rate` 为 0 / 字符串 / 缺失、还原后超 6 位小数、`unit` 为 0 / 3（除不尽）、非 JSON（HTML）、其他形状的 404、HTTP 500、超时 | USD `rate = 4.083`、JPY `rate = 0.025936`；`None`（无记录）；其余各自的错误码 |
| 双精度还原（v2） | unit | 录制响应里六个币种的 `middle_rate` 字面量 | 还原为 4.083 / 2.5936 / 0.0227 / 3.1935 / 4.6387 / 5.4057；`json.loads(..., parse_float=Decimal)` 的写法被本用例判失败（防回退） |
| 请求 | unit | 适配器发出的 URL 与请求头 | `/public/exchange-rate/USD/date/<吉隆坡今天>?session=1200&quote=rm`，`Accept: application/vnd.BNM.API.v1+json`；UTC 16:00 与 15:59 跨吉隆坡日期 |
| 拉取幂等 | integration | 同一报价日拉三次；并发拉两次 | 一条草稿；其余 `NO_NEW_QUOTE` |
| 无记录 | integration | BNM 答「无记录」 | `NO_QUOTE_FOR_DATE`，无草稿，`fx_fetch` 仍 OK |
| BNM 更正同日价格 | integration | 同一报价日返回不同 `middle_rate`；返回字面量不同但还原后相同 | 草稿不变，记录 `QUOTE_CHANGED`；后者不记 |
| 不自动发布 | integration | 拉取成功 | 只有草稿，`resolve_fx_rate` 仍取旧版本 |
| 手工录入与编辑 | integration（API） | 建、改、丢弃；编辑 BNM 草稿；退役草稿 | 正常 / 409 `FX_RATE_NOT_EDITABLE` / 409 `FX_RATE_NOT_RETIRABLE` |
| 事务内复查（v2） | unit | 空区间夹在时间线里（E13 退役后）；空区间出现在非 `RETIRED` 行；两个时间线上的版本相交；未截断的不是最后一个；两个起点为空 | 第一种通过，其余抛错 |
| 时间稳定性（性质用例） | unit | 随机「发布 / 预约 / 退役 / 撤销预约 / 补空档 / 同一秒重复」序列，每步后对早于该步 `t` 的固定时刻取汇率 | 一旦取到版本 V，之后永远是 V；每一步要么成功、要么是 §2 列出的 409 / 422，从不 500 |
| 发布与计费并发 | integration（真 MySQL） | 发布写完未提交时计费取共享锁并取汇率；反过来 | 同 025 的两种顺序结论 |
| 快照读陷阱（计费侧） | integration（真 MySQL） | 计费事务先普通读、另一连接发布、再取汇率 | 取到新版本 |
| 快照读陷阱（发布侧，v2） | integration（真 MySQL，两个连接） | 另一连接锁着 `fx_rate_locks`、发布一个版本；本连接发布等锁，它提交后继续 | 本次发布按新的 L 计算区间（P2 截断刚发布的那个，或 P3 的 409），复查通过 |
| 触发器（MySQL） | integration | 改已发布版本的 `rate` / 起点；删版本；改拉取记录；删或再插锁表行；直接插入已发布的版本；草稿直接改为 `RETIRED`（v2）；改已退役版本的 `effective_to` 或任何列（v2）；已退役改回已发布 | 全部拒绝 |
| `open_slot` 与唯一约束（MySQL，v2 第一轮审查后补） | integration | 同一币种对：A 发布、B 发布截断 A 之后，再建 BNM 草稿与手工草稿各一条；再丢弃一条、退役 B；此时共存多条 `DRAFT` / `DISCARDED` / 已截断 `PUBLISHED` / `RETIRED`；再直接写入第二条未截断的 `PUBLISHED` | 前面全部成功，非开放行的 `open_slot` 为 `NULL`；最后一步撞唯一约束 |
| 数据库兜底（MySQL，v2） | integration | 绕过服务直接写：已有时间线时把草稿改为起点为空的 `PUBLISHED`；把草稿改为带尽头的 `PUBLISHED`（插进退役空档）；`PUBLISHED` 改为 `RETIRED` 而不写尽头；空时间线（只有空区间行）时把草稿改为起点为空的 `PUBLISHED` | 前三种拒绝；第四种通过（证明不误杀）；服务层的正常路径不受影响 |
| 区间不重叠（MySQL） | integration | 绕过服务直接写：同一币种两个已截断且相交的版本；首尾相接；空区间；空区间落在另一行区间内部 | 相交的被拒绝，其余通过；服务层正常路径通过 |
| 精度 | integration | `rate` 10 / 11 位小数；0；负数；数字而非字符串 | 通过 / 422 |
| `fx_fetch` 维度 | unit | 72 小时内只有 FAILED；有一条 `NO_NEW_QUOTE`；有一条 `NO_QUOTE_FOR_DATE`；完全没有记录 | P2 / OK / OK / OK |
| `fx_stale` 维度 | unit | 当前生效版本的报价日距今 5 / 6 个日历日（冻结的吉隆坡日期，含跨月）；没有生效版本；只有未来的预约 | OK / P2 / P2 / P2 |
| 告警接口访问控制 | integration | 无令牌、错令牌、非 Bearer；未配置；已配置但文件不存在 / 是目录 / 为空 / 31 个字符；正确令牌；换了文件内容后不重启再请求 | 401 / 401 / 401；404（无日志）；404 ×4（ERROR 日志，不含路径）；200 纯文本；新令牌生效 |
| nginx 规则 | unit（`test_compose.py` 同款） | `/internal/alerts` 的 allow 名单与 `/readyz` 一致；`/internal/` 其余 deny | 一致 |
| `config_snapshot.sh`（v2） | unit（`test_deploy.py`） | 自查的键从 `.env` 按 `BILLING_HEALTHCHECK_*_URL` 模式取；不再有手写清单 | 断言模式存在、清单不存在；自查仍在加密之前 |
| `monitor.sh` | 本地演练（假接口 + 假心跳，照 `monitor.sh` 既有的演练方式） | 两个维度 OK / P2；接口不通；接口 404；缺某维度的地址 | 各推对应检查 / 不推 / 不推 / 只记日志 |
| 事务中途失败 | integration（SQLite 与 MySQL 各一次） | 发布审计抛错；撤销预约时恢复 P 之后审计抛错；拉取记录写入抛错 | 回滚 |
| 鉴权 | integration | 管理端匿名 401、CUSTOMER 403；首条语句 `require_admin`；路由枚举 | 同左 |
| 迁移 | integration（MySQL） | upgrade / downgrade；锁表恰好一行 | 同左 |

## 8. 迁移与上线

- **数据迁移步骤**：建 `fx_rate_locks`（写入一行）、`fx_rate_versions`、`fx_fetch_attempts`，约束与触发器（先做 0006 同款预检）。序号取实现时的下一个。
- **部署顺序**：合并即自动部署，先迁移后代码。§11 的六个任务依次合并，每一步单独部署也安全：数据库任务之后只有空表；规则与接口任务之后管理员可以录入与发布，但还没有拉取；拉取任务之后开始每天生成草稿；告警任务之后接口可用（令牌文件建好之前是 404）；部署任务之后 `monitor.sh` 开始推维度。
- **合并后的运维步骤**（写进 `docs/deployment.md` 与 `docs/runbook.md`，由 Kelvin 或经授权在 VPS 上执行）：
  1. 生成监控令牌文件 `secrets/monitor.token`（`openssl rand -hex 32`，`10001:10001 0400`）；compose 已按固定的容器内路径挂好（告警任务合并时），`.env` 只加 `BILLING_MONITOR_TOKEN_HOST_FILE`（宿主机路径，给 `monitor.sh`）
  2. Healthchecks 新建 `fx_fetch`、`fx_stale` 两个检查，地址写进 `.env`
  3. 在 worker 容器里手工触发一次拉取，确认 BNM 可达（周末或 12:30 前答「无记录」也算可达）
  4. 管理员发布第一个汇率版本
- **回滚或前滚**：downgrade 删触发器与表；发布过汇率后回滚会丢版本 —— 前滚。
- **部分部署**：迁移成功、代码未更新：新表空置；Beat 未更新则不拉取。代码更新、迁移失败：容器起不来，旧容器继续服务。
- **监控负责人**：Kelvin（Healthchecks 通知）。

## 9. 方案取舍

| 方案 | 优点 | 风险 | 未采用原因 |
| --- | --- | --- | --- |
| **采用**：从发布时刻起生效 + 只在末尾追加 + 单行锁串行 + 只有「无 → 有」可覆盖过去 | 重算结果永远一致（ADR-0005 收口条件）；与 025 / 价格 / 规则同一套证明 | 报价日当天中午之前的事件用前一天的价（差异是一天的汇率波动，由 markup 的 FX 缓冲吸收，ADR-0005 已提） | — |
| 从报价日 0 点起生效 | 更贴近「当日汇率」的直觉 | 回溯：已计费的上午事件重算会换汇率；或者计费要等人工发布，违背 §119 的 p99 ≤ 5 分钟 | 不采用（§10 第 1 条，Kelvin 已确认） |
| 自动发布 BNM 草稿 | 不用人工 | §17.1「绝不静默发布」；ADR-0005 §3 定了手工 | 不采用 |
| 加 `httpx` 依赖 | 接口更顺手 | 多一个运行时依赖只为一个每日调用 | 标准库 `urllib` |
| 告警接口放在 `/api/v1/admin/` 下用管理员会话 | 复用鉴权 | 管理员登录要 2FA，cron 走不了 | 独立内部路径 + 令牌 |
| 告警接口只靠 nginx 网段白名单 | 少一个密钥 | proxy_net 上有同机其他项目的容器 | 加令牌 |
| 告警接口返回 JSON 信封 | 与 API 一致 | `monitor.sh` 是 bash，宿主机未必有 `jq` | 纯文本逐行 |
| 过期按日历日 3 天 | ADR-0005 字面 | 每个长周末误报 | 放宽为 5 个日历日（§10 第 2 条，Kelvin 选定）；这是对 ADR-0005「超过 3 天」阈值的调整，实现时在 ADR-0005 的收口条件旁注明 |
| 按工作日 3 天 | 周末不误报 | 要做工作日计算，公众假期仍会误报 | Kelvin 选了日历日 |
| 维护马来西亚公众假期表 | 零误报 | 每年要维护，漏维护就漏报或误报 | 不维护；假期偶有一次误报可接受 |
| 预约存在时不给时刻的发布自动推后到预约起点之后（v2） | 少一次 409 | 生效时刻由系统猜；025 别名的「起点 + 1 秒」不适合汇率 | 409，与 026 v4 相同 |
| 用 BNM「最新」端点（v1） | 周末可能取到上一交易日 | 实测公布前 404、不回退；响应的日期要反推 | 按吉隆坡当天的日期取（v2） |
| `middle_rate` 按 JSON 字面量精确解析（v1 的「精确」读法） | 不经过 `float` | BNM 输出的是双精度的 17 位字面量，每次都超精度失败 | 最短往返表示（v2），只在这一处 |
| 一天拉一次（v1） | 简单 | 中午场晚于 12:30 公布的那天拿不到草稿，五天后才由 `fx_stale` 发现 | 一天三次（v2），唯一约束保证幂等 |
| `config_snapshot.sh` 手写清单里加两个键 | 改动最小 | 以后每加一个维度都要改这个脚本，T-E / T-H 的任务范围里没有它 | 按键名模式取（v2） |
| 撤销预约后被撤销的行仍当末尾版本（v1 未写明） | — | 下一次发布必然与恢复的前一个版本相交（E13） | 空区间不在时间线上（v2，026 v4） |

## 10. 未决问题与假设

- **未决问题**：无。Kelvin 2026-09-29 的答复：
  1. **汇率从发布时刻起生效**（而不是从 BNM 报价日 0 点起），发布之前发生的用量按上一个汇率算 —— 原文「同意」。
  2. **过期告警阈值为 5 个日历日**（三个选项中选 b：不按工作日、不维持 ADR-0005 的 3 天）—— 原文「b」。
- **v2 自行写定、未另请 Kelvin 拍板的小决定**（都照 026 v4 / 027 v4 已批准的先例；审查若认为需要人拍板请指出）：P3 判 409；R1 / R2 的界线是「起点 > `t`」；空区间不在时间线上（含「从没有已发布版本」的判断，取 026 的口径而非 027 的字面口径 —— 汇率只有一级，没有「下落」问题）；`t` 在整秒时也加 1 秒；令牌文件读不出按未启用处理；Beat 一天三次。
- **已在 2026-09-30 实测的接口事实**（v2；Claude 在开发机上用 `curl` 直接请求 BNM 公开接口，吉隆坡时间 12:00 前后）：
  - 路径 `/public/exchange-rate/{ccy}/date/{YYYY-MM-DD}?session=1200&quote=rm` 与请求头 `Accept: application/vnd.BNM.API.v1+json` 可用；不带请求头返回 HTML。
  - 字段路径：`data.currency_code`、`data.unit`、`data.rate.date`、`data.rate.middle_rate`、`meta.session`、`meta.quote`。数字是双精度的 17 位字面量。
  - 周六、国庆日、不存在的币种、当天中午场公布之前：404，响应体 `{"message":"No records found.","code":404}`。「最新」端点在公布之前同样 404，公布之后才返回当天的价，不回退上一交易日（**v1 假设 2 不成立**）。
  - 2026-09-30 的中午场在吉隆坡 12:11 公布（每两分钟轮询一次：12:10 仍 404，12:12 取到，`meta.last_updated` 为 `2026-09-30 12:11:18`）。
  - 漏传 `session` 时返回 `1130` 场，其 `middle_rate` 为 `null`。
  - **录制的响应**（逐字，含 BNM 输出的浮点字面量；实现任务把它们作为测试里的原样字符串常量，不另建夹具文件，也不联网重录）：

    ```text
    # USD，2026-09-29，session=1200（HTTP 200）
    {"data":{"currency_code":"USD","unit":1,"rate":{"date":"2026-09-29","buying_rate":4.0789999999999997,"selling_rate":4.0869999999999997,"middle_rate":4.0830000000000002}},"meta":{"quote":"rm","session":"1200","last_updated":"2026-09-30 11:56:16","total_result":1}}
    # JPY，2026-09-29，session=1200（HTTP 200，unit = 100）
    {"data":{"currency_code":"JPY","unit":100,"rate":{"date":"2026-09-29","buying_rate":2.5908000000000002,"selling_rate":2.5964,"middle_rate":2.5935999999999999}},"meta":{"quote":"rm","session":"1200","last_updated":"2026-09-29 23:01:20","total_result":1}}
    # USD，2026-09-29，未传 session（HTTP 200，BNM 给的是 1130 场）
    {"data":{"currency_code":"USD","unit":1,"rate":{"date":"2026-09-29","buying_rate":4.0650000000000004,"selling_rate":4.0899999999999999,"middle_rate":null}},"meta":{"quote":"rm","session":"1130","last_updated":"2026-09-30 11:56:16","total_result":1}}
    # USD，2026-09-26（周六），session=1200（HTTP 404）
    {"message":"No records found.","code":404}
    ```

    双精度还原用例用的另四个字面量（同日 session=1200 的 `middle_rate`）：IDR `0.022700000000000001`（`unit` 100）、SGD `3.1934999999999998`、EUR `4.6387`、GBP `5.4057000000000004`。
    其余 BAD_PAYLOAD 场景由上面的录制**派生**（改一个字段），用例里注明「派生自 2026-09-29 USD 录制」，不冒充真实响应。
- **尚未验证的假设**：
  1. 中午场每天都在 12:30 之前公布：只观察到一天（12:11）。一天三次拉取不依赖它的具体时刻；若 17:30 仍常常取不到，把第三次推后，不改设计语义。
  2. BNM 的响应形状今后不变 —— 变了就是 `BAD_PAYLOAD`，`fx_fetch` 三天后告警，改适配器、不改设计语义。
  3. 生产 celery-worker 能经 HTTPS 访问 BNM —— 合并后的运维步骤 3 实测。
- **如果假设或口径错误**：口径 1 不同意 → 需要改为「计费等待当日汇率发布」的方案，并评估对 §119 的影响，设计版本 +1；口径 2 再改阈值 → 只改维度的判断，设计版本 +1。

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

### 实现范围（v2：按层拆成六个任务，批准后逐个登记为 Worker 任务）

v1 的实现范围是一个任务、39 个允许路径，横跨数据库、规则、接口、后台任务与部署。v2 按层拆开：每个任务只做一层，用 `depends_on` 串起来，每一层合并后都能单独通过检查与 CI、单独部署（§8）；规则多的部分先交付规则与 §2 例子对应的测试，再接接口。编号登记时分配，下表用 F1–F6 指代。

| 任务 | 层 | 对应 v2 章节 | 依赖 | `allowed_change_paths` |
| --- | --- | --- | --- | --- |
| F1 | 数据库迁移与模型触发器 | §2「数据库」、§8 迁移 | AIH-TASK-027 | `alembic/versions/<日期>_0015_fx_rates.py`、`alembic/env.py`、`app/models/fx_rates.py`、`tests/backend/test_fx_rates_db.py`（新：CHECK、触发器、直接写库）、`tests/backend/test_migrations.py`、`tests/backend/test_model_columns.py`、`docs/database-schema.md`、`docs/TODO.md`（8） |
| F2 | 业务规则服务与仓储 | §2「状态」「发布」「退役」「锁在前」「事务内复查」「审计」「时间语义」、§4 | F1 | `app/repositories/fx_rates.py`（含 `resolve_fx_rate` 与取共享锁的函数）、`app/services/fx_rates.py`、`app/models/auth.py`、`app/services/audit_query.py`、`tests/backend/test_fx_rates_service.py`（E1–E16、复查、回滚）、`tests/backend/test_fx_rates_resolve.py`（并发、快照读、性质用例）、`docs/currency-and-fx.md`（新，规则部分）、`docs/TODO.md`（8） |
| F3 | 后台任务：BNM 拉取与 Celery | §2「BNM 适配器」、`fx_fetch_attempts`、§4 拉取并发、§10 录制 | F2 | `app/core/fx_source.py`、`app/core/config.py`（四个 FX 配置项）、`app/tasks/fx_fetch.py`、`app/core/celery_app.py`、`tests/backend/test_fx_source.py`（录制响应作字符串常量）、`tests/backend/test_fx_fetch.py`、`tests/backend/test_celery.py`、`tests/backend/test_config.py`、`docs/currency-and-fx.md`（BNM 一节）、`docs/TODO.md`（10） |
| F4 | 管理端接口 | §2「接口」（管理端八个）、§6 | F2 | `app/schemas/fx_rates.py`、`app/api/admin_fx_rates.py`、`app/main.py`、`tests/backend/test_fx_rates_api.py`、`tests/backend/test_admin_customers_api.py`（路由枚举）、`docs/api.md`、`docs/TODO.md`（7） |
| F5 | 内部告警接口与两个维度 | §2「内部告警接口」「两个告警维度」 | F3 | `app/services/alerts.py`、`app/api/internal_alerts.py`、`app/core/config.py`（`monitor_token_file`）、`app/main.py`、`docker-compose.yml`（令牌 secret 与容器内路径 —— 必须与配置项同一个任务：`test_every_credential_file_setting_is_wired_into_compose` 要求每个 `*_file` 配置项都在 compose 里挂好）、`tests/backend/test_internal_alerts.py`、`tests/backend/test_compose.py`、`docs/api.md`、`docs/TODO.md`（9） |
| F6 | 部署与告警接线 | §2 nginx 两层中的第一层、`monitor.sh`、`config_snapshot.sh`、§8 运维步骤 | F5 | `deploy/nginx/billing.conf`、`deploy/monitor.sh`、`deploy/config_snapshot.sh`、`.env.example`、`tests/backend/test_compose.py`（nginx 规则）、`tests/backend/test_deploy.py`、`docs/deployment.md`、`docs/runbook.md`、`docs/adr/ADR-0005-fx-rate-source.md`（收口条件旁注明两条告警已落地、过期阈值按 Kelvin 2026-09-29 的决定为 5 个日历日）、`docs/TODO.md`（10） |

- F4 与 F3 都只依赖 F2，可以不分先后；F5 依赖 F3 是因为 `fx_fetch` 维度读拉取记录的 `outcome` 与配置的币种。
- 下游：T-G（计价）只需要 F2；T-E、T-H 往注册表加维度需要 F5；前端 T-K 需要 F4。登记时同步它们的 `depends_on`。
- 每个任务的验收标准不超过 8 条、预计改动不超过约 1500 行；F2 最大（服务 + 16 个例子用例 + 并发与性质用例），若登记前预审仍判过大，把 `test_fx_rates_resolve.py` 的并发与性质用例拆成 F2 之后的一个纯测试任务。

## 12. 版本变更记录

| 版本 | 日期 | 改了什么 | 触发原因 |
| --- | --- | --- | --- |
| v1 | 2026-09-29 | 初稿 | — |
| v2 | 2026-09-30 | ① 发布规则重写为 P1–P4：末尾版本未截断且尚未开始时不给时刻 → 409（v1 截成倒挂或空区间，没有合法结果）；「F = E 或 ≥ `t`」统一为「F ≥ `t`」。② 空区间不在时间线上：不当末尾版本、不算「有过版本」、不进复查排序；复查写明三条断言。③ 退役重写为 R1–R5：R1 / R2 界线「起点 > `t`」；撤销预约只恢复被它截断的 `PUBLISHED` 行。④ 数据库：CHECK 逐条写出并加「停用必有尽头」；触发器禁止 `DRAFT → RETIRED`、发布跃迁上尽头必须为空、`RETIRED` 不可改、空区间跳过重叠判定。⑤ 新增「锁在前」。⑥ 规则表每行配例子 E1–E16，§7 每个例子一个用例。⑦ BNM 适配器按实测改写：按日期取、逐字段核对、双精度字面量取最短往返表示、「无记录」404 → 新结果 `NO_QUOTE_FOR_DATE`（拉取记录加 `requested_date`）、一天三次；§10 附录制响应。⑧ 令牌文件每种状态的行为；`fx_stale` 例子改成日历日；compose 直接写容器内路径。⑨ `config_snapshot.sh` 按键名模式自查。⑩ §11 拆成六个任务。⑪ v2 第一轮设计审查（REQUEST_CHANGES）后补：`open_slot` 写明 `CASE … THEN 1 END`（其余为 `NULL`），§7 补「截断之后再建草稿」的约束用例；v2 尚未获批，按 027 v4 多轮审查的先例不另升版本 | 2026-09-30 OpenClaw 登记前预审（ACVDEV-TASK-059/062）对 v1 判 `NOT_READY`，九条发现逐条核实见下；另自查发现 v1 的复查排序与 026 v3 同一缺陷、「F = E」与「F ≥ `t`」矛盾、BNM 双精度字面量（被取消的 run `fcf9c426` 自编的夹具是干净小数、用 `parse_float=Decimal`，按真实响应上线后每次拉取都会判 `UNIT_NOT_EXACT`） |

### v2 的触发：登记前预审九条逐条核实

| # | 预审发现 | 核实 | v2 的处理 |
| --- | --- | --- | --- |
| 1 | DESIGN_DEFECT §2：不给时刻发布、L 起点 ≥ `t` 时截断成空或倒挂区间 | **成立**（Kelvin 已人工核实）。v1 规则表第 3 行只对「给了 F」要求 F > L 起点 | P3 → 409（E6、E8） |
| 2 | DESIGN_DEFECT §2：撤销预约后，空的 `RETIRED` 行若算末尾版本，下一次发布与重新打开的前一个版本相交 | **成立**。v1 没定义末尾版本是否含空区间，按字面「L 已退役（尽头 E）」取，新版本从 E 起、前一个版本已恢复为无尽头，二者相交（`open_slot` 唯一约束也会拒绝）。同源的第二处：v1 的复查把空区间排进时间线，撤销预约本身就过不了复查 | 空区间不在时间线上（E13）；复查三条断言 |
| 3 | DESIGN_DEFECT §2：撤销预约把 `RETIRED` 的前一个版本恢复为无尽头，已退役的汇率悄悄覆盖空档 | **成立**。v1「前一个版本恢复为未截断」无条件，v1 触发器允许改 `RETIRED` 行的 `effective_to`，也没有「停用必有尽头」 | R2 只恢复被截断的 `PUBLISHED` 行（E14）；CHECK 4；`RETIRED` 不可改 |
| 4 | DESIGN_DEFECT §2：数据库兜底缺 027 v4 那几道 | **成立**，四点全对：没有「停用必有尽头」；`DRAFT → RETIRED` 未禁；直接写库可在已有版本之后发布起点为空的版本（v1 只有重叠判定，起点为空、尽头非空且落在最早版本之前的行过得去）；`RETIRED` 行可改 `effective_to` | CHECK 4；禁止 `DRAFT → RETIRED`；发布跃迁上尽头必须为空（与重叠判定合起来，起点为空只可能出现在空时间线上）；`RETIRED` 不可改 |
| 5 | SCOPE_GAP `deploy/config_snapshot.sh`：自查逐个列举心跳地址，新地址加不进去 | **成立**，但不是 CI 会红的缺口：快照本身只收键名，新地址不会进快照；缺的是自查这道兜底。逐个列举的写法以后每加一个维度都要改它 | 改成按键名模式自查，归 F6，路径进 `allowed_change_paths` |
| 6 | UNDECIDED §10：BNM 字段路径与周末行为未核实，离线 Worker 录不到真实响应 | **成立，且比预审说的更严重**：2026-09-30 实测，v1 用的「最新」端点在公布前 404、不回退（假设 2 不成立）；数字是双精度字面量，按 v1「精确解析」每次失败 | 按日期取、逐字段核对、最短往返还原、「无记录」→ `NO_QUOTE_FOR_DATE`；§10 附录制响应，Worker 照抄 |
| 7 | UNDECIDED §2：起点等于 `t` 的退役算哪种；空区间是否计入首版与末尾版本规则；`t` 取整在整秒时是否 +1 | **成立**，三处 v1 都没写 | 起点 = `t` 归 R1（E12）；空区间两者都不计（E3、E13、E15）；整秒也 +1（与现有 `boundary_after` 一致） |
| 8 | UNDECIDED §2：令牌文件已配置但缺失 / 为空的行为未写；`fx_stale` 例子写工作日 | **部分成立**：v1 §5 有「令牌文件缺失 → 404」一行，但为空、是目录（compose 缺文件时实测挂成空目录）、无权限都没写，也没说记不记日志；例子确实写成了「working days」 | 令牌文件状态表；例子改为日历日 |
| 9 | TOO_LARGE：39 个路径、13 条标准 | **成立** | §11 拆成六个任务，每个 7–10 个路径 |

**送审前的模型核对**（v2）：把 P1–P4、R1–R5 写成一个约 150 行的 Python 模型，随机生成 2 万个「发布（给 / 不给 F，F 在 `t` 前后）/ 退役 / 同一秒重复」序列，每一步后断言：CHECK 1–4、时间线不相交、至多一个未截断、事务内复查三条、对 `≤ now` 的每个时刻「一旦取到版本 V，之后永远是 V」、P4 的 E ≤ `t`、除 §2 列出的 409 / 422 外没有别的失败。v2 全部通过；把 v1 的三处写法分别放回去（P3 截断、空区间算末尾版本、无条件恢复），每一处都被断言抓到。这个模型只是送审前的自查，不进仓库；§7 的性质用例由 F2 按同样的断言写。

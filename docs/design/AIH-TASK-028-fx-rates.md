# AIH-TASK-028 设计：FX 汇率版本、BNM 拉取、审批与告警计数接口（已批准 v1）

> **来源**：设计闸门 Issue #183。本文件是 `APPROVED: design v1` 那一版正文的**逐字副本**，审查者为 Codex。
> 放进仓库，是因为 OpenClaw Worker 在沙箱里不联网、读不到 GitHub Issue。
> **实现以本文件为准**；与 Issue 不一致时，以 Issue #183 上被批准的 v1 为准。设计要改，就回到 Issue 升版本、重新过闸门，不要直接改本文件。
> 正文里的「T-C」即 [PHASE-2-plan.md](PHASE-2-plan.md) 的占位名；写「编号登记时分配」的实现任务，登记为 `AIH-TASK-028`。正文提到的其他占位名（T-A…T-L）对应的登记编号见 `docs/TODO.md` 的 Phase 2 一节。
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
**设计版本**：`v1`
对应需求：spec §5、§14、§17.1、§58、§74.2、§80、§95、§110、§113、§120；`REQ-FIN-001`、`REQ-FIN-002`；ADR-0005（FX 来源与取值规则）
目标 PR：待开（批准后在 `.platform/tasks.yaml` 登记实现任务，编号登记时分配）
Phase 2 计划：`docs/design/PHASE-2-plan.md` 的 T-C（含第 1 节第 2、3 条：BNM 适配器归 Phase 2；告警走计数接口 + `deploy/monitor.sh` + Healthchecks，计数接口与它的访问控制在本闸门定）

## 1. 目标与边界

- **要解决的问题**：供应商价格是原币种（ADR-0005 §5），计费要按事件 `occurred_at` 时生效的**已发布**汇率换成 MYR（§17.1）。流程必须是「自动拉取 → 草稿 → 管理员发布」，自动拉取绝不静默发布；外部接口不可用或缺历史汇率时必须能手工录入；取不到汇率的事件判 `FX_RATE_ERROR`、不扣费。ADR-0005 已定来源（BNM openAPI）、每日拉取、手工发布、按 `occurred_at` 取值、热路径绝不调 BNM，并要求两条告警在 Phase 2 前落地。现在这些都没有，也没有任何告警接口。
- **可观察的完成标准**：
  1. 每天吉隆坡 12:30 之后，Celery Beat 从 BNM 拉取配置的币种（V1 为 USD）的中间价，写成 `DRAFT`；同一报价日重复拉取不产生第二条草稿；每次拉取的结果留下一条记录。
  2. 管理员能手工录入草稿、编辑、丢弃、发布、退役；发布后数据库层不可改。
  3. 只读查询函数 `resolve_fx_rate(session, base_currency, occurred_at)` 与发布串行：一个事件一旦取到某个汇率版本，以后永远取到同一个。
  4. `GET /internal/alerts`（带令牌、只对宿主机与内网开放）逐行给出告警维度的结论；`deploy/monitor.sh` 按维度各推一个 Healthchecks 检查。本任务带两个维度：`fx_fetch`（连续拉取失败）与 `fx_stale`（最新发布的汇率过期）。
- **明确不做什么**：
  - 用汇率算钱 —— T-G（§80 的高精度中间计算与一次舍入）
  - 自动发布 —— ADR-0005 §3：V1 由管理员手工发布
  - 其他来源的适配器 —— 只留可替换的接口（§17.1「pluggable」），V1 只有 BNM 与手工
  - 其他告警维度 —— T-E（幂等冲突）与 T-H（定价错误、汇率错误、未知模型、负余额、outbox 积压）接进本任务建的接口
  - 前端 —— T-K
- **现有行为与问题证据**：`app/` 里没有任何出站 HTTP 客户端（运行时依赖没有 `httpx` / `requests`）；`app/core/celery_app.py` 的 Beat 只有 `outbox-recovery`；`deploy/monitor.sh` 只有服务、readyz、磁盘三个维度；ADR-0005 收口条件「两条告警在 Phase 2 前落地」未勾。

## 2. 设计概要

```text
拉取（Celery Beat，每天 UTC 04:30 = 吉隆坡 12:30）：
  对每个配置币种：FxRateSource.fetch(currency)（事务外，超时 10 秒）
    → 成功：同一事务里 INSERT 草稿（撞「同币种同报价日」唯一约束 = 已有，不重复）+ INSERT 拉取记录
    → 失败：INSERT 拉取记录（FAILED + 错误码），不写草稿

管理端：手工建草稿 / 改 / 丢弃 → 审计
管理端：发布 / 退役 →
  SELECT … FROM fx_rate_locks WHERE id = 1 FOR UPDATE
  拿到锁之后取 t（向上取整到下一个整秒）→ 区间计算 → 写 → 同一事务复查 → 审计 → commit

计费（T-G / T-H 在计费事务里调用，本任务只提供）：
  SELECT … FROM fx_rate_locks WHERE id = 1 FOR SHARE（持有到计费事务提交）
  resolve_fx_rate(session, base_currency, occurred_at)：加锁读覆盖 occurred_at 的 PUBLISHED / RETIRED 版本 → 没有 → None（FX_RATE_ERROR）

告警：GET /internal/alerts（Bearer 令牌）→ 纯文本，每行一个维度
  monitor.sh 每 5 分钟读一次 → 每个维度推一个 Healthchecks 检查
```

### 数据库（迁移序号取实现时的下一个，预计 0014）

**`fx_rate_locks`**：单行（迁移写入 `id = 1`），BEFORE INSERT（已有一行时）/ DELETE 触发器拒绝。发布 `FOR UPDATE`、计费 `FOR SHARE`，与 025 的供应商行锁同一套推理（见「发布」）。

**`fx_rate_versions`**（§74.2）

| 列 | 类型 | 说明 |
| --- | --- | --- |
| `id`、`public_id` | | |
| `base_currency` | CHAR(3)，`^[A-Z]{3}$`，CHECK ≠ `MYR` | |
| `quote_currency` | CHAR(3)，CHECK `= 'MYR'` | §17.1 |
| `rate` | DECIMAL(24,10)，CHECK > 0 | **1 单位 `base_currency` = 多少 MYR**。BNM 按 `unit` 报价（某些币种按 100 单位），入库前除以 `unit`，要求结果在 10 位小数内精确（否则拉取记为 FAILED `UNIT_NOT_EXACT`，不写草稿）；手工录入同样最多 10 位小数，不舍入 |
| `source` | VARCHAR(16)，CHECK `BNM` / `MANUAL` | ADR-0005 §1、§2 |
| `source_reference` | VARCHAR(255) | BNM：`bnm:exchange-rate:<ccy>:<报价日>:session=1200:middle_rate:unit=<n>`；手工：必填，录入者写出处 |
| `source_quote_date` | DATE NULL | BNM 的报价日（吉隆坡日期）；手工录入可空 |
| `observed_at` | DATETIME | BNM：报价日 12:00 吉隆坡 = 当日 04:00 UTC（固定 +8，马来西亚无夏令时，不依赖镜像里的 tzdata）；手工：录入者给出的观测时刻 |
| `status` | CHECK `DRAFT` / `PUBLISHED` / `RETIRED` / `DISCARDED` | `DISCARDED` 是 §74.2 之外补的：丢弃的草稿留痕 |
| `effective_from` / `effective_to` | DATETIME NULL | 半开区间；草稿为空 |
| `open_slot` | 生成列（STORED）：`status = 'PUBLISHED' AND effective_to IS NULL` 时为 1 | |
| `created_by` | FK → `users.id` NULL | BNM 拉取为空（系统） |
| `approved_by` / `approved_at` | | 发布人与发布时刻 |
| `created_at` / `updated_at` | DATETIME | |

- UNIQUE `(base_currency, quote_currency, open_slot)`：一个币种对至多一个未截断的已发布版本。
- UNIQUE `(base_currency, quote_currency, source, source_quote_date)`：同一报价日的 BNM 草稿只有一条（MySQL 唯一约束不管 `NULL`，手工录入不受限）。
- CHECK：状态与区间、发布人的对应同价格版本（`DRAFT` / `DISCARDED` ⇒ 区间与发布人为空；`PUBLISHED` / `RETIRED` ⇒ 发布人非空；区间合法或退役留下的空区间）；`source = 'BNM'` ⇒ `source_quote_date IS NOT NULL`。
- **区间不重叠由数据库兜底**（`fx_rate_versions` BEFORE UPDATE，`NEW.status IN ('PUBLISHED','RETIRED')` 且 `effective_from`、`effective_to` 或 `status` 有变化时）：触发器先执行 `SELECT id INTO @lock_id FROM fx_rate_locks WHERE id = 1 FOR UPDATE`（与服务层发布同一把锁，把绕过服务的直接写入也串行起来），再检查同一币种对内是否存在另一行满足：状态为 `PUBLISHED` / `RETIRED`、区间非空（`effective_from IS NULL OR effective_to IS NULL OR effective_from < effective_to`）、且与本行区间相交 —— 相交判定 `COALESCE(o.effective_from, '1000-01-01') < COALESCE(NEW.effective_to, '9999-12-31') AND COALESCE(NEW.effective_from, '1000-01-01') < COALESCE(o.effective_to, '9999-12-31')`，另一行条件 `o.base_currency = NEW.base_currency AND o.quote_currency = NEW.quote_currency AND o.id <> NEW.id`；存在即 SIGNAL。空区间（撤销的预约）不参与判定。
- **服务层的写入顺序**（保证正常路径过得了上一条触发器）：发布时先截断前一个（`effective_to` 写入），再把本行从草稿改为已发布；撤销预约时先把被撤销的一行改成空区间，再把前一个恢复为未截断。
- 触发器（照 0006 的写法与权限预检）：BEFORE INSERT 要求 `NEW.status = 'DRAFT'`（版本只能经「草稿 → 发布」进入已发布）；`PUBLISHED` / `RETIRED` 行只允许改 `effective_to`、`status`（仅 `PUBLISHED → RETIRED`）、`updated_at`；`DISCARDED` 不可改；一律不许 DELETE。

**`fx_fetch_attempts`**（只增）

| 列 | 类型 | 说明 |
| --- | --- | --- |
| `id` | BIGINT PK | |
| `base_currency` | CHAR(3) | |
| `source` | VARCHAR(16) | `BNM` |
| `outcome` | CHECK `NEW_DRAFT` / `NO_NEW_QUOTE` / `FAILED` | `NO_NEW_QUOTE`：接口成功，但返回的报价日已有草稿（周末、假期 BNM 返回上一个交易日的价 —— 这是**成功**，不计入失败） |
| `quote_date` | DATE NULL | 成功时的报价日 |
| `error_code` | VARCHAR(64) NULL | `TIMEOUT` / `HTTP_<status>` / `BAD_PAYLOAD` / `UNIT_NOT_EXACT` / `NETWORK`；不存响应体 |
| `fx_rate_version_id` | FK NULL | `NEW_DRAFT` 时指向新草稿 |
| `attempted_at` | DATETIME | |

- BEFORE UPDATE / DELETE 触发器拒绝（只增）。

### BNM 适配器（ADR-0005 §1）

- 接口 `FxRateSource`（Protocol）：`fetch(currency) -> FxQuote(quote_date, middle_rate, unit, reference)`，失败抛带错误码的异常。工厂函数 `build_fx_source(settings)` 是唯一决定实现的地方（照 `app/core/mailer.py` 的 `build_transport`），换来源不影响已发布版本与历史计算（§17.1）。
- BNM 实现用标准库 `urllib.request`（**不加运行时依赖**：只有这一个出站调用，标准库足够），`GET {base_url}/public/exchange-rate/{ccy}?session=1200&quote=rm`，请求头 `Accept: application/vnd.BNM.API.v1+json`，超时 `BILLING_FX_FETCH_TIMEOUT_SECONDS`（默认 10）。只接受 HTTPS 的 `base_url`（配置校验）。
- 取 `middle_rate`（ADR-0005「BNM 的中间价」）与 `unit`；`session=1200`（中午场，ADR-0005 §2「吉隆坡 12:00 BNM 公布之后」）。字段缺失、类型不对、`middle_rate ≤ 0` → `BAD_PAYLOAD`。
- 配置：`BILLING_FX_SOURCE`（默认 `bnm`）、`BILLING_FX_BNM_BASE_URL`（默认 BNM 公开地址）、`BILLING_FX_CURRENCIES`（默认 `USD`，逗号分隔、大写三字母）、`BILLING_FX_FETCH_TIMEOUT_SECONDS`。
- Beat：`crontab(hour=4, minute=30)`（Celery 时区是 UTC），`expires` 3600 秒。任务幂等（唯一约束兜底），可以手工再触发。
- 出站调用在事务外；写草稿与写拉取记录在同一事务里。拉取成功但写库失败 → 任务报错，下一次运行重新拉取（草稿唯一约束保证不重复）。
- **热路径绝不调用 BNM**：`resolve_fx_rate` 只读库（ADR-0005 §4）。拉取失败时，已发布版本照旧生效（只要它没被截断），计费不受影响。

### 状态

| 当前 | 操作 | 新状态 | 说明 |
| --- | --- | --- | --- |
| — | BNM 拉取 / 手工录入 | `DRAFT` | 手工：`rate`、`observed_at`、`source_reference` 必填 |
| `DRAFT` | 编辑（仅手工来源） | `DRAFT` | BNM 草稿不可编辑（要改就丢弃后手工录入，出处不同） |
| `DRAFT` | 丢弃 | `DISCARDED` | |
| `DRAFT` | 发布 | `PUBLISHED` | 见下 |
| `PUBLISHED` | 退役（末尾） | `RETIRED` | 见下 |
| `RETIRED` / `DISCARDED` | 任何 | 409 | |

### 发布：区间怎么定

按币种对，每对是一条只在末尾追加的时间线。拿到 `fx_rate_locks` 排他锁之后取 `t`（向上取整到下一个整秒）；可选预约时刻 F 必须 ≥ `t`（整秒、带时区）：

| 情形 | 本版本的 `effective_from` | 对末尾版本 L |
| --- | --- | --- |
| 该币种对从没有已发布版本，未给 F | `NULL`（一直以来） | — |
| 从没有，给了 F | F | — |
| 有 L 且 L 未截断 | 未给 → `t`；给了 → F（须 > L 的起点） | 截断于本版本起点 |
| L 已退役或已截断（尽头 E） | **E**（首尾相接，补上空档）；给了 F 则 F 必须 = E 或 ≥ `t`，否则 409 | — |

- **汇率从发布时刻起生效，不从报价日起算**（§10 第 1 条，Kelvin 2026-09-29 已同意）：BNM 报价日 D 的草稿最早在 D 日 12:30 吉隆坡才存在，发布更晚；若让它从 D 日 0 点起生效，就是回溯 —— D 日上午已按前一个汇率计费的事件，重算时会取到新汇率，违反 ADR-0005「同一事件重算得出相同 MYR」。`observed_at` / `source_quote_date` 记录报价日，供审计与过期判断；ADR-0005 §4「按 `occurred_at` 落在哪个已发布区间」的规则不变。
- **第一个版本对过去生效，退役后的空档由下一个版本补上**：这两处被覆盖的时刻此前一律取不到汇率，事件是 `FX_RATE_ERROR`、从未扣费（§17.1），补上只把「无汇率」变成「有汇率」，已取到汇率的时刻结果不变。这正是 §17.1「历史汇率缺失时必须手工录入」的做法：退役（或从未有过）之后发布一条手工版本，它就覆盖那段空档。
- **与计费不会交错**：发布在排他锁内、`t` 在锁后取；计费在共享锁内，只处理 `occurred_at ≤` 持锁后当前时间的事件（025 的 T-H 契约）。计费先 → 事件 `occurred_at` < `t` ≤ 任何新边界；发布先 → 计费读到新表。加上「只在末尾追加 / 只把无汇率变有汇率」，**一个（币种, 时刻）一旦取到某个版本，以后永远取到同一个**。
- 每次写完在同一事务里复查该币种对的区间（只有第一条起点可为空；相邻首尾相接或有退役空档），不满足回滚。

**退役**（汇率错了，从此刻起不再用）：当前末尾、已生效 → `effective_to = t`，`t` 起该币种事件 `FX_RATE_ERROR`，直到发布下一个版本（它从 `t` 起补上）。当前末尾、尚未生效（预约）→ 空区间，前一个版本恢复为未截断。历史版本 → 409。**纠正已计费事件用的汇率**不在本任务：走 Phase 8 reprocess。

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
| `GET /api/v1/admin/fx-rates/fetch-attempts` | 最近的拉取记录（§58「同步状态」） |

错误码与价格版本同构：`FX_RATE_NOT_FOUND`、`FX_RATE_NOT_DRAFT`、`FX_RATE_NOT_EDITABLE`（BNM 草稿）、`FX_RATE_NOT_RETIRABLE`、`FX_RATE_FINAL`、`EFFECTIVE_FROM_CONFLICT`（409）；`EFFECTIVE_FROM_IN_PAST`、`VALIDATION_ERROR`（422）。

内部告警接口：

- `GET /internal/alerts`，**不在 `/api/` 下**，不套 §107 信封：它是给 `monitor.sh` 的机器接口，返回 `text/plain`，每行 `<维度> <OK|P1|P2> <一句摘要>`，例如 `fx_fetch OK last success 2026-09-29 (USD)`、`fx_stale P2 latest published USD rate observed 2026-09-24, 4 working days old`。
- **两层访问控制**：
  1. nginx：`location = /internal/alerts` 与 `/readyz` 同一份 allow 名单（回环、本栈网段、proxy_net），其余 deny；另加 `location /internal/ { deny all; }`，防止以后在 `/internal/` 下新增路径时忘了保护。
  2. 应用：`Authorization: Bearer <令牌>`，令牌从 `BILLING_MONITOR_TOKEN_FILE` 读（Compose `secrets:` 文件，照 `smtp_password_file` 的先例），常量时间比较；未配置令牌 → 接口返回 404（等于未启用）；令牌不对 → 401，不说明原因。需要第二层是因为 proxy_net 上还有同机其他项目的容器（`deploy/nginx/billing.conf` 的注释）。
- 响应只有计数、日期与币种，不含租户、金额或任何个人数据。
- 维度注册表：`app/services/alerts.py` 里一个有序的维度列表，每个维度是一个只读查询函数；T-E、T-H 往里加，不改接口形状。

### 两个告警维度（ADR-0005 §6）

| 维度 | 何时告警 | 分级 |
| --- | --- | --- |
| `fx_fetch` | 某个配置币种在最近 72 小时内**没有任何**成功的拉取记录（`NEW_DRAFT` 或 `NO_NEW_QUOTE`），且至少有一条 `FAILED` —— 即「连续 3 天拉取失败」。周末假期 BNM 返回上一交易日的价，记为 `NO_NEW_QUOTE`（成功），不会误报 | P2 |
| `fx_stale` | 某个配置币种最新**已发布且当前生效**的版本，其报价日（`source_quote_date`，手工录入取 `observed_at` 的吉隆坡日期）距今超过 **5 个日历日**（按吉隆坡日期相减；§10 第 2 条，Kelvin 2026-09-29 选定）；或该币种根本没有生效的版本 | P2 |

`monitor.sh`：新增 `check_alerts`（`curl -fsS -m 10 -H "Authorization: Bearer $(cat 令牌文件)"`，令牌文件路径由 `.env` 的 `BILLING_MONITOR_TOKEN_HOST_FILE` 给出，字面解析、不 source），逐行解析，每个维度用 `BILLING_HEALTHCHECK_ALERT_<维度大写>_URL` 推一个检查；缺配置的维度只记日志（沿用 `heartbeat` 的做法）。接口不通时**不推**这些维度（readyz 维度已报同一个原因，避免双响），Healthchecks 的宽限期到了会自行报「未上报」。复核（`RECHECK_SECONDS`）一并覆盖。

Healthchecks 用量：现有 8 个，本任务 +2，T-E +1，T-H 计划 +5，共 16 个，免费档上限 20。

### 事务边界

| 路径 | 事务 |
| --- | --- |
| 拉取 | 外部调用在事务外；草稿与拉取记录同一事务 |
| 手工草稿 / 编辑 / 丢弃 | 一个事务，含审计；不拿锁 |
| 发布 / 退役 | 锁 → `t` → 写 → 复查 → 审计 → 提交 |
| 告警接口 | 只读 |

计费路径的锁顺序（写进 T-H 契约）：供应商（S，025）→ **FX（S，本任务）** → 定价规则（S，T-D）→ 钱包 → 租户。发布路径各只拿自己那一把锁，不形成环。

### 外部系统与异步边界

- BNM：只在 Beat 任务里调用，失败只影响草稿与告警，不影响计费。
- 生产 celery-worker 有出站网络（SMTP 已在用）；到 BNM 的 HTTPS 是否可达要在部署后实测一次（§10 假设 3）。

### 时间语义

- 区间、`occurred_at`、`t`：UTC 无时区，半开 `[from, to)`。
- 报价日是吉隆坡日期；换算用固定 +8 小时（不依赖 tzdata）。
- 过期判断：吉隆坡「今天」的日期减去报价日（日历日），> 5 即过期。5 天覆盖「周末 + 两到三天公众假期」这样的长假而不误报；不维护公众假期表。

### 金额精度

- `rate`：DECIMAL(24,10)；精确解析与精确除以 `unit`，不舍入；超精度拒绝。
- §80 的换算（供应商原币种成本 × `rate`）、高精度中间值与一次舍入归 T-G；T-G 把 `fx_rate_applied` 存成本任务的 `rate` 原值（同精度）。

### 审计

| `action` | 什么时候 |
| --- | --- |
| `FX_RATE_CREATE` | 手工建草稿（BNM 草稿不写审计：拉取记录就是它的来源证明，操作者是系统） |
| `FX_RATE_UPDATE` | 改手工草稿 |
| `FX_RATE_DISCARD` | 丢弃 |
| `FX_RATE_PUBLISH` | 发布；前：被截断的前一个版本 |
| `FX_RATE_RETIRE` | 退役；`reason`；被恢复的前一个版本（若有） |

`entity_type = fx_rate_version`。五个动作都不在 §66 清单里，按先例补上并记进 `docs/TODO.md`。

### 对下游任务的契约

| 下游 | 契约 |
| --- | --- |
| T-G | `source_currency = MYR` 不查汇率；否则 `resolve_fx_rate` 返回 `None` → `FX_RATE_ERROR`，不扣费；换算按 §80（高精度、只在最终计费额舍入一次） |
| T-H | 计费事务里在 `resolve_model` 之后取 `fx_rate_locks` 的 `FOR SHARE`，汇率用加锁读；`fx_rate_version_id` 与 `fx_rate_applied` 写进快照；锁顺序见上；新增告警维度注册到 `app/services/alerts.py` |
| T-E | 冲突维度注册到同一注册表，`monitor.sh` 无需改动（按行通用处理），只需在 `.env` 加对应的 Healthchecks 地址 |
| T-K | 汇率页标明「中间价、吉隆坡中午场、从发布时刻起生效」 |

## 3. 不变量影响矩阵

| 不变量 | 是否触碰 | 可能怎样被破坏 | 设计控制 | 验证方式 |
| --- | --- | --- | --- | --- |
| INV-1 中心故障不中断 AI | 否（间接） | BNM 故障拖垮计费 | 热路径不调 BNM；拉取失败只影响草稿 | 拉取失败时 `resolve_fx_rate` 照常的用例 |
| INV-2 / INV-3 / INV-4 / INV-5 | 否 | 不碰钱包与账本 | — | — |
| INV-6 事件保留版本引用 | **是** | ① 已发布版本被改；② 发布与计费交错；③ 回溯发布（从报价日起算）让已计费时刻换汇率 | ① 触发器；② 单行锁串行、`t` 锁后取、加锁读；③ 从发布时刻起生效，只有「无汇率 → 有汇率」可以覆盖过去 | 触发器、并发、性质用例 |
| INV-7 客户不可见成本毛利 | **是**（间接） | 汇率与成本经客户接口泄露；告警接口泄露业务数据 | 只有管理端与带令牌的内部接口；告警行不含金额 | 越权与内容断言 |
| INV-8 租户不可互访 | 否 | 全局数据 | — | — |
| INV-9 | 否 | — | — | — |
| INV-10 金额用 Decimal | **是** | `rate` 经 `float` 失真；`unit` 除法被舍入 | 字符串精确解析；精确除法，不精确即失败 | 精度与 `unit` 用例 |
| INV-11 / INV-12 | 否 | — | — | — |
| INV-13 状态与事件原子提交 | **是** | 发布了而审计没写；草稿写了而拉取记录没写 | 同一事务 | 回滚用例（SQLite 与 MySQL 各一次） |
| INV-14 队列丢失不毁持久工作 | 否（间接） | Beat 丢一次调度 | 下一次调度照常；告警维度看 72 小时窗口 | — |

## 4. 状态与并发

状态表见 §2。

- **串行化**：发布 / 退役在 `fx_rate_locks` 排他锁内；计费在同一行共享锁内。并发拉取（手工重触发与定时同时跑）由 `(base, quote, source, source_quote_date)` 唯一约束决出一条草稿，输家记 `NO_NEW_QUOTE`。
- **数据库保证的唯一性**：`(base_currency, quote_currency, open_slot)`；`(base_currency, quote_currency, source, source_quote_date)`；`public_id`。
- **幂等键**：拉取靠报价日唯一约束；发布已发布 → 200 不写。
- **相同 ID、不同载荷**：同一报价日 BNM 再次返回不同的价（BNM 更正）→ 已有草稿不变，记 `NO_NEW_QUOTE` 并在拉取记录的 `error_code` 写 `QUOTE_CHANGED`；管理员看到后可丢弃旧草稿、手工录入。
- **重试层**：Beat 每日一次；失败不在任务内重试（下一次调度或手工重触发）。
- **原子提交**：版本 + 截断 / 恢复 + 复查 + 审计；草稿 + 拉取记录。

## 5. 失败与恢复矩阵

| 失败点 | 对外结果 | 数据最终状态 | 是否可重试 | 恢复来源 | 告警 |
| --- | --- | --- | --- | --- | --- |
| BNM 超时 / 网络 / HTTP 错误 / 格式变了 | — | 拉取记录 `FAILED`，无草稿；已发布版本照旧生效 | 下一次调度或手工 | BNM 恢复；或手工录入 | 72 小时无成功 → `fx_fetch` |
| BNM 当天无新价（周末 / 假期） | — | `NO_NEW_QUOTE` | — | — | 不告警（`fx_stale` 阈值 5 个日历日，覆盖周末与短假期） |
| 管理员忘了发布 | — | 旧版本继续生效 | — | 发布 | `fx_stale` |
| 草稿写库失败 | 任务报错 | 无草稿、无记录 | 下一次调度 | — | 错误日志；持续则 `fx_fetch` |
| 发布时复查不通过 | 500 | 回滚 | 否 | 修代码 | 错误日志 |
| 发布锁超时 | 500 | 回滚 | 是 | — | 错误日志 |
| 从未发布过汇率 | — | 非 MYR 事件 `FX_RATE_ERROR` | 发布后由 T-H 重新入队 | — | `fx_stale`（无生效版本）+ T-H 的汇率错误维度 |
| 发布了错误汇率 | — | 已引用则不可改 | 退役 + 发布正确版本（从 `t` 起）；历史纠正走 Phase 8 | 审计 | 无 |
| 告警接口不通 | — | — | — | — | readyz 维度；Healthchecks 宽限期后报「未上报」 |
| 令牌文件缺失 | 接口 404 | — | 配置后生效 | — | 各告警维度未上报 |

## 6. 数据与安全边界

- **租户过滤**：不适用（全局数据）。
- **鉴权主体**：管理端接口只有 ADMIN；告警接口是 nginx 网段白名单 + Bearer 令牌（令牌不是管理员凭据，只能读告警行）。
- **禁止返回的字段**：内部 id；告警行不含金额、租户与个人数据。
- **日志 / 审计 / 异常**：拉取失败只记错误码，不记 BNM 响应体；令牌不进日志、不进 `.env`（`.env` 里只有令牌文件的路径）。
- **密钥**：监控令牌是新的密钥文件，属主 `10001`、权限 `0400`（与其他 secrets 同法）；宿主机上 `monitor.sh` 读同一份文件的宿主机路径。部署步骤写进 `docs/deployment.md` 与 `docs/runbook.md`。
- **prompt / response**：不涉及。
- **保留与删除**：汇率版本与拉取记录都不删除。

## 7. 测试证据计划

| 风险/需求 | 测试层级 | 场景 | 预期结果 |
| --- | --- | --- | --- |
| BNM 解析 | unit（录制的 JSON，不联网） | 正常；`unit = 100`；`unit` 除不尽；字段缺失；`middle_rate` 为 0；非 JSON；HTTP 500；超时 | 正确的报价 / 对应错误码 |
| 拉取幂等 | integration | 同一报价日拉两次；并发拉两次 | 一条草稿；第二次 `NO_NEW_QUOTE` |
| 周末 | integration | BNM 返回上一交易日 | `NO_NEW_QUOTE`，`fx_fetch` 仍 OK |
| BNM 更正同日价格 | integration | 同一报价日返回不同 `middle_rate` | 草稿不变，记录 `QUOTE_CHANGED` |
| 不自动发布 | integration | 拉取成功 | 只有草稿，`resolve_fx_rate` 仍取旧版本 |
| 手工录入与编辑 | integration（API） | 建、改、丢弃；编辑 BNM 草稿 | 正常 / 409 `FX_RATE_NOT_EDITABLE` |
| 发布区间 | integration | 第一个版本；再发布（截断前一个）；预约；退役当前；退役预约；退役后再发布（补空档） | 区间与规则一致 |
| 不许回溯 | integration | F < `t`；F 早于末尾起点 | 422 / 409 |
| 时间稳定性（性质用例） | unit | 随机「发布 / 预约 / 退役 / 撤销预约 / 补空档」序列，每步后对早于该步 `t` 的固定时刻取汇率 | 一旦取到版本 V，之后永远是 V |
| 发布与计费并发 | integration（真 MySQL） | 发布写完未提交时计费取共享锁并取汇率；反过来 | 同 025 的两种顺序结论 |
| 快照读陷阱 | integration（真 MySQL） | 计费事务先普通读、另一连接发布、再取汇率 | 取到新版本 |
| 触发器（MySQL） | integration | 改已发布版本的 `rate` / 起点；删版本；改拉取记录；删或再插锁表行；直接插入已发布的版本 | 拒绝 |
| 区间不重叠（MySQL） | integration | 绕过服务直接写：同一币种两个已截断且相交的版本；首尾相接；空区间 | 相交的被拒绝，其余通过；服务层正常路径通过 |
| 精度 | integration | `rate` 10 / 11 位小数；0；负数；数字而非字符串 | 通过 / 422 |
| `fx_fetch` 维度 | unit | 72 小时内只有 FAILED；有一条 NO_NEW_QUOTE；完全没有记录 | P2 / OK / OK（没有记录 = 从未运行，由 `fx_stale` 负责） |
| `fx_stale` 维度 | unit | 最新报价日距今 5 / 6 个日历日（冻结的吉隆坡日期，含跨月）；没有生效版本 | OK / P2 / P2 |
| 告警接口访问控制 | integration | 无令牌、错令牌、未配置令牌、正确令牌 | 401 / 401 / 404 / 200 纯文本 |
| nginx 规则 | unit（`test_compose.py` 同款） | `/internal/alerts` 的 allow 名单与 `/readyz` 一致；`/internal/` 其余 deny | 一致 |
| `monitor.sh` | 本地演练（假接口 + 假心跳，照 `monitor.sh` 既有的演练方式） | 两个维度 OK / P2；接口不通；缺某维度的地址 | 各推对应检查 / 不推 / 只记日志 |
| 事务中途失败 | integration（SQLite 与 MySQL 各一次） | 发布审计抛错；拉取记录写入抛错 | 回滚 |
| 鉴权 | integration | 管理端匿名 401、CUSTOMER 403；首条语句 `require_admin`；路由枚举 | 同左 |
| 迁移 | integration（MySQL） | upgrade / downgrade；锁表恰好一行 | 同左 |

## 8. 迁移与上线

- **数据迁移步骤**：建 `fx_rate_locks`（写入一行）、`fx_rate_versions`、`fx_fetch_attempts`，约束与触发器（先做 0006 同款预检）。序号取实现时的下一个。
- **部署顺序**：合并即自动部署，先迁移后代码。
- **合并后的运维步骤**（写进 `docs/deployment.md` 与 `docs/runbook.md`，由 Kelvin 或经授权在 VPS 上执行）：
  1. 生成监控令牌文件（随机 32 字节，`10001:10001 0400`），在 `.env` 写 `BILLING_MONITOR_TOKEN_FILE`（容器内路径）与 `BILLING_MONITOR_TOKEN_HOST_FILE`（宿主机路径）；compose 加这个 secret
  2. Healthchecks 新建 `fx_fetch`、`fx_stale` 两个检查，地址写进 `.env`
  3. 在 worker 容器里手工触发一次拉取，确认 BNM 可达、生成草稿
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

## 10. 未决问题与假设

- **未决问题**：无。Kelvin 2026-09-29 的答复：
  1. **汇率从发布时刻起生效**（而不是从 BNM 报价日 0 点起），发布之前发生的用量按上一个汇率算 —— 原文「同意」。
  2. **过期告警阈值为 5 个日历日**（三个选项中选 b：不按工作日、不维持 ADR-0005 的 3 天）—— 原文「b」。
- **需要谁拍板**：已拍板。
- **尚未验证的假设**：
  1. BNM 接口的路径、请求头与返回字段（`middle_rate`、`unit`、`date`）与本设计一致 —— 实现时用一次真实响应录成测试数据（录制在本地进行，不在 CI 联网），不一致则改适配器、不改设计语义。
  2. BNM 在周末 / 假期对 `session=1200` 返回上一交易日的价（而不是 404）—— 若是 404，适配器把「非交易日的 404」记为 `NO_NEW_QUOTE`，需要从响应判断是「无数据」而非故障；实现时以真实响应为准，设计语义（周末不算失败）不变。
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

### 实现范围（批准后登记为 Worker 任务的 `allowed_change_paths`）

- `alembic/versions/<日期>_<序号>_fx_rates.py`、`alembic/env.py`
- `app/models/fx_rates.py`（新）、`app/models/auth.py`（审计动作）
- `app/repositories/fx_rates.py`（新，含 `resolve_fx_rate` 与取共享锁的函数）
- `app/services/fx_rates.py`（新）、`app/services/alerts.py`（新，维度注册表）
- `app/core/fx_source.py`（新，Protocol + BNM 实现 + 工厂）、`app/core/config.py`
- `app/tasks/fx_fetch.py`（新）、`app/core/celery_app.py`（`TASK_MODULES` 与 Beat）
- `app/schemas/fx_rates.py`（新）、`app/api/admin_fx_rates.py`（新）、`app/api/internal_alerts.py`（新）、`app/main.py`
- `deploy/nginx/billing.conf`、`deploy/monitor.sh`、`docker-compose.yml`（secret 声明）
- `tests/backend/test_fx_rates_api.py`、`tests/backend/test_fx_rates_service.py`、`tests/backend/test_fx_source.py`、`tests/backend/test_internal_alerts.py`（新）、`tests/backend/fixtures/bnm_*.json`（新）
- `tests/backend/test_admin_customers_api.py`、`tests/backend/test_migrations.py`、`tests/backend/test_model_columns.py`、`tests/backend/test_compose.py`、`tests/backend/test_celery.py`
- `docs/currency-and-fx.md`（新）、`docs/adr/ADR-0005-fx-rate-source.md`（收口条件旁注明两条告警已落地、过期阈值按 Kelvin 2026-09-29 的决定为 5 个日历日）、`docs/api.md`、`docs/database-schema.md`、`docs/deployment.md`、`docs/runbook.md`、`docs/TODO.md`

## 12. 版本变更记录

| 版本 | 日期 | 改了什么 | 触发原因 |
| --- | --- | --- | --- |
| v1 | 2026-09-29 | 初稿 | — |

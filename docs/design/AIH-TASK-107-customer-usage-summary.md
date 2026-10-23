# AIH-TASK-107 / 108 设计：客户门户本月用量（已批准 v1）

> **来源**：设计闸门 Issue #403。本文件是 `APPROVED: design v1` 那一版正文的**逐字副本**（审查者为独立、只读的 Claude Code 会话，Codex 额度不可用期间 Kelvin 2026-10-23 选择；送审时状态行为 `READY_FOR_REVIEW`，批准后 Issue 上只把状态行改成了 `APPROVED: design v1`）。
> 放进仓库，是因为 OpenClaw Worker 在沙箱里不联网、读不到 GitHub Issue。
> **实现以本文件为准**；与 Issue 不一致时，以 Issue #403 上被批准的 v1 为准。设计要改，就回到 Issue 升版本、重新过闸门，不要直接改本文件。
> **前置**：设计闸门 #398（[AIH-TASK-103-customer-usage.md](AIH-TASK-103-customer-usage.md)）的 `AIH-TASK-103`、`AIH-TASK-104`（索引、2 秒执行预算、`QueryTooBroad`、日期换算）与 `AIH-TASK-105`、`AIH-TASK-106`；设计闸门 #393（[AIH-TASK-099-customer-wallet.md](AIH-TASK-099-customer-wallet.md)）的 `AIH-TASK-101`、`AIH-TASK-102`。
> §8 的两个实现任务登记为 `AIH-TASK-107`（接口）→ `AIH-TASK-108`（门户首页的本月用量卡），按这个顺序依赖。

---

## 0. 准入判定

| 任务涉及 | 要填的范围 |
| --- | --- |
| 钱包 / 账本 / 定价 / 汇率 / 支付 / 幂等 / 状态机 | **全部章节** |
| 用量摄取 / 认证与会话（集成侧与平台侧）/ Webhook | §1–§7（**§6 不可省**） |
| 前端 / 文档 / CI / 脚本 | 不走本闸门 |
| 既不碰钱、也不碰用量摄取 / 认证与会话 / Webhook 的后端改动 | 不走本闸门 |

本任务档位：`全部`。只读，不写任何表、不改表结构，但对客户展示的是按月汇总的计费额（`billable_cost` 合计），同一张表上就是成本与毛利列（INV-7），并且第一次在客户侧定义「月」的边界。只读带来的「不触碰」在 §3 逐条说明理由。

---

状态：`待审`（`READY_FOR_REVIEW`）
负责人：Claude
**设计版本**：`v1`
对应需求：spec §67「Customer Dashboard」（This Month Usage、Request count、Conversation count、usage chart；「Do NOT show internal Acuven costs.」）、§87（Date / Day / Month 维度）、§90（`/api/v1/customer/*`，租户只取自会话）、§109（展示时区 `Asia/Kuala_Lumpur`）、§111（聚合可由请求数据重算，不替代请求数据）、`REQ-PRIV-001`（INV-8）、`REQ-PRIV-002`（INV-7）
前置设计：#368 v12（客户门户认证）、#393 v3（客户钱包与门户首页）、#398 v4（客户用量明细：已计费口径、马来西亚日历日、`ix_usage_events_tenant_occurred`、2 秒执行预算与 `QUERY_TOO_BROAD`）
目标 PR：待开（批准后按 §8 拆两个任务登记；`docs/TODO.md`「后续计划」里「Phase 4 客户仪表盘的其余部分」中的本月用量、请求数、对话数与用量图即本闸门）

## 1. 目标与边界

- **要解决的问题**：门户首页（#393）现在只有余额与最近流水，spec §67 要求的「本月用量、请求数、对话数、用量图」还没有。#393 把它们推迟到「月份边界与时区定下来之后」；#398 已定客户侧日期按马来西亚时间理解（`docs/REVIEW-LOG.md` 2026-10-23），本设计在此基础上定「本月」。
- **可观察的完成标准**：
  1. `GET /api/v1/customer/usage-summary` 返回登录客户自己某个马来西亚日历月（默认当月）的：已计费事件的计费额合计、请求数、对话数，以及该月每一天的请求数与计费额；恰好 §2 列出的键。
  2. 口径与 #398 的用量页完全一致：只算 `status = PROCESSED`、按 `occurred_at` 的马来西亚日历日归属；用量页把日期区间设为同一个月时，条数与计费额之和与本接口一致。
  3. 每条查询沿用 2 秒执行预算，超时 422 `QUERY_TOO_BROAD`；首页上这一块显示「暂时算不出」，不影响余额卡与其余部分。
  4. 门户首页加「本月用量」卡（计费额、请求数、对话数）与按日的请求数柱状图，链到用量页；页面不请求、不显示任何成本、毛利或内部字段。
- **明确不做什么**（Kelvin 2026-10-23 逐项确认）：
  - **不建聚合表**：读时在 `usage_events` 上实时汇总。等有客户真的经常超时（`customer_usage_query_timeout` 日志）再另开闸门建按日聚合表。
  - **按调用发生时间（`occurred_at`）归月**，不按记账时间：与用量页一致。所以「本月计费额」与「本月钱包流水里 `AI_USAGE` 的合计」可能差在跨月的几笔（月末发生、次月才计费的事件计入发生的月；钱包流水按记账时间）。页面不把两者并列比较。
  - 不做低余额提示、最小恢复额（仍在后续计划，依赖各自的配置设计）。
  - 不做按项目、供应商、模型拆分的汇总（用量页可以筛选）；不做跨月趋势、不做导出。
  - 不写任何表，不改表结构、不加索引。
- **现有行为与问题证据**：
  - `usage_events` 已有（AIH-TASK-103 交付后）`ix_usage_events_tenant_occurred (tenant_id, occurred_at, public_id)`，可按租户与时间区间范围扫描。
  - AIH-TASK-104 交付 `CUSTOMER_USAGE_QUERY_TIMEOUT_MS = 2000`、MySQL 错误 3024 → `QueryTooBroad` → 422 `QUERY_TOO_BROAD`，以及马来西亚日历日到 UTC 的换算（复用 `KUALA_LUMPUR_OFFSET`）。本设计复用它们，不另写一份。
  - AI 模块代为上报的事件没有 `conversation_id`（`app/ai_api/client_rules.py`），不会被计入对话数。

## 2. 设计概要

```text
GET（客户访问令牌）→ 查询参数校验（FastAPI，处理函数之前，422）→ require_customer（第一条语句：401 / 403）
→ tenant_id = CustomerPrincipal.tenant_id → 月份换算成 UTC 区间（422）
→ 同一个只读事务里两条 SELECT（按日分组、对话去重）→ 补齐没有事件的日子 → 白名单投影 → 200
（无状态变化、无财务效果、无出站事件、无审计）
```

### 接口：`GET /api/v1/customer/usage-summary`

| 查询参数 | 规则 |
| --- | --- |
| `month` | 可选，`YYYY-MM`，马来西亚日历月；省略 = 请求时刻所在的马来西亚日历月。必须在 `2020-01` 到 `2099-12` 之间（含两端），在任何日期运算之前判，越界或格式错 422 `VALIDATION_ERROR`。允许未来的月份（结果全为 0），不报错 |

未知查询参数忽略；不接受任何租户、客户标识。

成功 200，`data`：

```json
{
  "month": "2026-10",
  "charged_amount": "12.34567800",
  "request_count": 1234,
  "conversation_count": 56,
  "days": [
    { "date": "2026-10-01", "request_count": 10, "charged_amount": "0.41200000" },
    { "date": "2026-10-02", "request_count": 0, "charged_amount": "0.00000000" }
  ]
}
```

| 字段 | 说明 |
| --- | --- |
| `month` | 实际统计的月份（省略参数时就是当月），`YYYY-MM` |
| `charged_amount` | 该月已计费事件的 `billable_cost` 之和（含税，ADR-0008），8 位小数字符串；等于 `days` 里各日 `charged_amount` 之和（同一条查询的按日结果相加，Decimal 精确相加，不经 float） |
| `request_count` | 该月 `PROCESSED` 事件数（spec §9：一次供应商调用一条）；等于各日 `request_count` 之和 |
| `conversation_count` | 该月 `PROCESSED` 事件中**不同的非空** `conversation_id` 的个数。同一个对话跨月时，在它有事件的每个月各计一次；没有 `conversation_id` 的事件（例如 AI 模块的调用）不计入。不是各日去重数之和 |
| `days` | 该月的**每一天**各一项（28–31 项），按日期升序；没有事件的日子 `request_count = 0`、`charged_amount = "0.00000000"`。`date` 是马来西亚日历日 |

**刻意不返回**：成本、原币成本、汇率、参考价、毛利、token 合计（spec §67 没有要求，用量页有逐条 token）、按项目 / 供应商 / 模型的拆分、内部 id。

### 时间

- 月份区间：`from_utc = 当月 1 日 00:00 − 8 小时`，`to_utc = 次月 1 日 00:00 − 8 小时`，按 `occurred_at` 取 `[from_utc, to_utc)`。与 #398 同一换算（复用 `KUALA_LUMPUR_OFFSET`）。例：`month = 2026-10` → `[2026-09-30 16:00:00, 2026-10-31 16:00:00)` UTC。
- 「当月」= 请求处理时刻（`now`，UTC）加 8 小时后的年月；服务端取一次，同一个请求里不再变。
- 按日归属：`date = (occurred_at + 8 小时) 的日期`。MySQL 用 `DATE(DATE_ADD(occurred_at, INTERVAL 8 HOUR))`，SQLite（只在测试里用）用 `date(occurred_at, '+8 hours')`，由一个按方言选择的 SQL 表达式生成，两边结果相同（测试断言）。
- 晚到或晚计费的事件计入它**发生**的那一天：同一个月的数字会随时间变大（spec §111：聚合可由请求数据重算）。

### 查询

两条 SELECT，**在同一个只读事务里**执行（MySQL 默认 `REPEATABLE READ`，同一事务内的一致性读共用一个快照，所以两条看到同一组行，合计与对话数不会来自不同时刻）：

```sql
-- ① 按日
SELECT <马来西亚日期表达式> AS day, COUNT(*) AS request_count, COALESCE(SUM(billable_cost), 0) AS charged_amount
FROM usage_events
WHERE tenant_id = :tenant_id AND status = 'PROCESSED'
  AND occurred_at >= :from_utc AND occurred_at < :to_utc
GROUP BY day

-- ② 对话去重
SELECT COUNT(DISTINCT conversation_id)
FROM usage_events
WHERE tenant_id = :tenant_id AND status = 'PROCESSED'
  AND occurred_at >= :from_utc AND occurred_at < :to_utc
```

- 两条都走 `ix_usage_events_tenant_occurred` 的租户 + 时间范围；`status`、`billable_cost`、`conversation_id` 要回表取，扫描量与该客户当月的事件数成正比。
- MySQL 上两条都带 `/*+ MAX_EXECUTION_TIME(2000) */`（复用 AIH-TASK-104 的常量与超时参数）；任一条超时 → `QueryTooBroad` → 422 `QUERY_TOO_BROAD`，并记一条 `customer_usage_query_timeout` 日志（与 #398 同一条日志、同样不带参数值）。
- `COUNT(DISTINCT)` 不计 NULL，正好对应「没有 `conversation_id` 的事件不计入」。
- 补齐日子在 Python 里做：按月生成全部日期，查到的日子填值，其余填 0。`charged_amount` 合计 = 各日 Decimal 相加，再经现有 `money_text` 格式化（不舍入，8 位小数）。
- 新函数放在 `app/repositories/usage_events.py`，与 AIH-TASK-104 的客户用量查询放在一起；不读、不 SELECT 任何成本列。

### 错误

401 `TOKEN_INVALID`（无 / 坏 / 过期令牌，以及非 `ACTIVE` 的门户账号，沿用 `require_current_user` 的现有行为）、403 `CUSTOMER_REQUIRED`（ADMIN 令牌、没有租户的 CUSTOMER）、422 `VALIDATION_ERROR`（`month` 格式或越界）、422 `QUERY_TOO_BROAD`、500、503。没有 404、没有 `ACCOUNT_READ_ONLY`（读接口，只读状态下照常可读）。

### 金额

接口只格式化不舍入；前端按 spec §80 与 `docs/REVIEW-LOG.md` 2026-10-23 的约定：本月计费额是 AI 用量，用 AIH-TASK-102 的 `formatAmountRounded` 显示最多 6 位，每日计费额同样最多 6 位。

### 前端（第二个任务，不在闸门范围内，写在这里只为说明接口够用）

- 门户首页（AIH-TASK-101 之后的 `PortalHomePage`）加「本月用量」卡：计费额、请求数、对话数（旁注「只计带对话标识的请求」），月份标题按马来西亚时间；下方一个按日的请求数柱状图：用纯 CSS / SVG 画，柱高按 `request_count`（整数）相对当月最大值的比例，不引入图表依赖、不把金额转成浮点；每根柱的悬停提示显示当天请求数与计费额（字符串舍入）。
- 卡片链到用量页（不带参数，用量页默认就是本月）。
- 加载中、失败（带 `request_id`）、`QUERY_TOO_BROAD`（显示「本月用量暂时算不出，可在用量页按较短区间查看」）三种状态；任何一种都不影响余额卡、最近流水与开启 2FA。
- 浏览器验收：用验收夹具的客户账号加一个只读步骤。

## 3. 不变量影响矩阵

| 不变量 | 是否触碰 | 可能怎样被破坏 | 设计控制 | 验证方式 |
| --- | --- | --- | --- | --- |
| INV-1 中心故障不中断 AI | 否（间接） | 只读接口，不在 AI 或摄取路径上；但与计费、AI 模块共用数据库，一个大客户的整月汇总可能长时间扫表 | 每条 SELECT 2 秒执行预算，超时即中止 | 测试：MySQL 上测试专用 1 毫秒超时得 `QUERY_TOO_BROAD`（§7「扫描上限」） |
| INV-2 事件不重复扣费 | 否 | 不写 | 无写路径 | 测试：调用前后各表行数与余额不变 |
| INV-3 支付不重复入账 | 否 | 同上 | 同上 | 同上 |
| INV-4 余额只经账本变动 | 否 | 同上 | 同上 | 同上 |
| INV-5 历史账本不可变 | 否 | 不读账本 | — | — |
| INV-6 事件保留版本引用 | 否 | 不读不返回版本引用 | — | — |
| INV-7 客户不可见成本毛利 | **是** | ① 汇总时顺手 SUM 了成本列并返回；② 内部计量租户的 `reference_customer_price`（假想售价）被当成计费额汇总 | ① 两条 SELECT 只读 `billable_cost`、`conversation_id`、`occurred_at`、`status`，响应是 5 键白名单（`days` 每项 3 键）；② 只 SUM `billable_cost`（内部计量恒为 0） | 测试：成本、原币成本、汇率、参考价写成哨兵值，断言原始响应文本里没有；内部计量租户的 `charged_amount = "0.00000000"`、`request_count` 照常计数；键集合逐字断言 |
| INV-8 租户不可互访 | **是** | 从请求里取租户；漏掉租户过滤；对话去重跨了租户 | 租户只来自 `CustomerPrincipal.tenant_id`，接口没有任何租户参数；两条 SELECT 都 `WHERE tenant_id = :tenant_id` | 测试：客户 A、B 同月都有事件、且共用同一个 `conversation_id` 值；A 带 `?customer_id=<B>&tenant_id=<B 的内部 id>` 只得到 A 的数字；A 的 `conversation_count` 不因 B 的同名对话变化 |
| INV-9 对话内容不入库 | 否 | 只计数 `conversation_id`，不返回它 | — | — |
| INV-10 金额用 Decimal | 是 | SUM 或补齐、相加时经 float；前端经 float 画图 | 数据库 SUM 返回 Decimal，Python 侧 Decimal 相加；`money_text` 格式化；柱高只用整数 `request_count` | 测试：三条 `0.00000001` 的事件合计 `"0.00000003"`；`999999999999.99999999` 与 `0.00000001` 同日相加得 `"1000000000000.00000000"`（Decimal 不溢出） |
| INV-11 event_id 至多一次财务效果 | 否 | 不写 | — | — |
| INV-12 定稿对账单不可变 | 否 | 对账单还不存在；本接口不是对账单，数字会随晚到事件变化（§2「时间」） | — | — |
| INV-13 状态与事件原子提交 | 否 | 没有状态变化与出站事件；两条 SELECT 在同一快照里 | 同一只读事务 | 测试：单元层断言两条 SELECT 在同一个连接、同一个事务里发出 |
| INV-14 队列丢失不毁持久工作 | 否 | 不用 Redis / Celery | — | — |

## 4. 状态与并发

- **没有状态变化**，所以没有跃迁表。
- **与计费并发**：两条 SELECT 在同一个 `REPEATABLE READ` 事务里，看到同一个快照：合计、按日与对话数来自同一组 `PROCESSED` 行。事务开始之后才变成 `PROCESSED` 的事件不计入这一次，下一次请求就计入。
- **不加锁**：不阻塞计费与摄取。
- 唯一性、幂等键、重放：不适用（只读）。
- 重试：失败由前端重新请求，后端不重试。

## 5. 失败与恢复矩阵

| 失败点 | 对外结果 | 数据最终状态 | 是否可重试 | 恢复来源 | 告警 |
| --- | --- | --- | --- | --- | --- |
| `month` 格式错或越界 | 422 `VALIDATION_ERROR`，不回显值 | 不变 | 修正后重试 | — | 无 |
| 未登录 / 令牌过期 / 门户账号非 `ACTIVE` | 401 `TOKEN_INVALID` | 不变 | 刷新后重试（非 `ACTIVE` 不行） | 刷新令牌 / 管理员处理账号 | 无 |
| ADMIN、无租户 CUSTOMER | 403 `CUSTOMER_REQUIRED` | 不变 | 否 | — | 无 |
| 租户 `DISABLED` / `CLOSED` | 200 照常 | 不变 | — | — | 无 |
| 任一条 SELECT 超过 2 秒 | 422 `QUERY_TOO_BROAD`；首页这一块显示「暂时算不出」，其余照常 | 不变（语句被中止，没有部分结果返回） | 是；客户可在用量页按较短区间查看 | — | `customer_usage_query_timeout` 日志；超时常见时另开闸门建聚合表 |
| DB 不可用 / 查询失败 | 503 / 500（带 `request_id`） | 不变 | 是 | 数据库恢复 | 现有 readyz 巡检 |
| Redis/Celery 丢失 | 无影响 | 不变 | — | — | 无 |
| 外部服务超时 | 不适用 | — | — | — | — |
| 重复请求 | 期间没有新计费的事件时结果相同 | 不变 | 是 | — | 无 |

## 6. 数据与安全边界

- **租户过滤**：两条 SELECT 都 `WHERE tenant_id = :tenant_id`，`tenant_id` 只来自 `require_customer`。
- **鉴权主体**：只有 CUSTOMER（`ACTIVE`、有租户）。处理函数第一条语句显式调用 `require_customer`；AIH-TASK-093 的路由枚举用例覆盖新路由。
- **客户侧禁止返回的字段**：见 §2「刻意不返回」。
- **日志、审计、异常**：沿用请求日志；不记录 `month` 之外的任何参数值（本接口也没有别的参数），不记录金额与计数。读操作不写审计。
- **密钥**：不涉及。
- **prompt / response 与客户数据**：不处理内容；只返回该客户自己的计数与计费额。`conversation_id` 只参与计数，不返回。
- **数据保留**：不新增存储。

## 7. 测试证据计划

后端（`tests/backend/test_customer_usage_summary_api.py`，SQLite + MySQL 参数化；客户登录夹具照 `tests/backend/test_customer_api.py` 的写法；SQL 文本断言不写死方言字面量；示例 uuid 全零）：

| 风险/需求 | 测试层级 | 场景 | 预期结果 |
| --- | --- | --- | --- |
| 正常路径 | API | `month=2026-10`：10 月 1 日 3 条、10 月 15 日 2 条 `PROCESSED`，其中 3 条有两个不同的 `conversation_id`、2 条没有 | 恰好 5 个键；`request_count = 5`、`conversation_count = 2`、`charged_amount` 等于 5 条之和；`days` 恰好 31 项、按日期升序，1 日与 15 日有值、其余为 0 与 `"0.00000000"`；每项恰好 3 个键 |
| 月份边界 | API | 事件在 UTC `2026-09-30 15:59:59.999999`、`16:00:00`、`2026-10-31 15:59:59.999999`、`16:00:00` | `month=2026-10` 只计中间两条，分别归到 10 月 1 日与 10 月 31 日 |
| 日期归属两边一致 | API（SQLite + MySQL） | 事件在 UTC `2026-10-01 15:59:59` 与 `16:00:00` | 分别归到马来西亚 10 月 1 日与 10 月 2 日，两种方言结果相同 |
| 默认月份 | API | 不带 `month`，把「现在」固定在 UTC `2026-10-31 15:59:59` 与 `16:00:00` | 分别统计 `2026-10` 与 `2026-11` |
| 只算已计费 | API | 同月另有 `RECEIVED`、`PRICING_ERROR`、`MODEL_UNKNOWN`、`FAILED_FINAL` 事件 | 都不计入三个数字与 `days` |
| 与用量页一致 | API | 同一批事件，用 #398 的 `GET /api/v1/customer/usage-events` 以同一个月的首末日翻完 | 条数等于 `request_count`，各条 `charged_amount` 之和等于本接口的 `charged_amount` |
| 对话计数 | API | 同一 `conversation_id` 在 9 月与 10 月各有事件；10 月内同一对话 5 条 | 9 月、10 月各计 1；10 月不因 5 条而计 5 |
| 参数规则 | API | `month` 为 `2026-13`、`2026-1`、`2019-12`、`2100-01`、`abc` | 422 `VALIDATION_ERROR`，文案不回显值；`2020-01`、`2099-12` 为 200；未来月份 200 且全为 0 |
| 空月 | API | 该月没有任何事件 | 三个数字为 0、`charged_amount = "0.00000000"`，`days` 照常补齐 |
| 扫描上限 | MySQL | 写入 30 000 条当月 `PROCESSED` 事件，测试专用 1 毫秒超时 | `QueryTooBroad`，接口层 422 `QUERY_TOO_BROAD` 并有 `customer_usage_query_timeout` 日志；默认超时下正常返回且数字正确；编译出的两条 SQL 在 MySQL 带提示、SQLite 不带 |
| 同一快照 | unit | repository 函数 | 两条 SELECT 在同一个连接、同一个事务里发出（照现有 SQL 捕获用例的写法） |
| 金额精度 | API（SQLite + MySQL） | 三条 `0.00000001`；同日 `999999999999.99999999` 与 `0.00000001` | `"0.00000003"`；`"1000000000000.00000000"` |
| 内部计量租户 | API | `INTERNAL_METERED_ONLY` 的 `PROCESSED` 事件，`reference_customer_price` 为哨兵值 | `charged_amount = "0.00000000"`，`request_count` 照常；响应里没有哨兵值 |
| 无成本毛利 | API | 成本、原币成本、汇率、参考价写成哨兵值 | 原始响应文本里没有任何哨兵值 |
| 租户越权 | API | 客户 A、B 同月有事件且共用 `conversation_id` 值；A 带 `?customer_id=<B>&tenant_id=<B 的内部 id>` | 只得到 A 的数字 |
| 鉴权 | API | 无令牌、坏令牌、ADMIN 令牌、门户账号停用后的旧令牌 | 401 `TOKEN_INVALID` / 401 / 403 `CUSTOMER_REQUIRED` / 401 `TOKEN_INVALID`，且不发汇总查询 |
| 只读契约 | API | 租户四种 `account_status` | 都 200 |
| 路由守卫 | integration | AIH-TASK-093 的 `/api/v1/customer/*` 枚举用例 | 新处理函数被枚举到且显式调用了 `require_customer` |
| 无写入 | API | 每个用例前后 | `usage_events`、`wallet_transactions`、`wallets`、`audit_logs`、`domain_outbox` 不变 |
| 事务中途失败 | API | repository 抛数据库异常 | 500 / 503 带 `request_id`，各表不变 |
| Redis/Celery 丢失 | — | 不使用 | 不适用 |

前端（Vitest）：卡片的三个数字与月份标题；计费额 6 位显示；柱高按 `request_count` 比例（当月最大值为 0 时所有柱高为 0、不除以零）；悬停提示；三种状态（含 `QUERY_TOO_BROAD` 文案）且任一状态下余额卡与开启 2FA 照常显示；不请求 `/api/v1/admin/` 路径。

## 8. 迁移与上线

- **数据迁移步骤**：无。不改表、不加索引（复用 AIH-TASK-103 的 `ix_usage_events_tenant_occurred`）。
- **锁表与性能影响**：无 DDL。每次请求两条范围扫描，扫描量与该客户当月事件数成正比，上限是每条 2 秒。首页每次打开都会调一次；不做缓存（Kelvin 2026-10-23 选读时实时算）。
- **兼容窗口**：新接口；前端在后端之后上。
- **部署顺序与拆分**（批准后登记为两个任务）：
  1. 接口：repository 函数、`app/schemas/customer_usage.py` 加汇总模型、`app/api/customer.py` 一个路由、测试、`docs/api.md`（依赖 AIH-TASK-104：常量、`QueryTooBroad`、日期换算与 `customer_usage.py` 都由它交付）。
  2. 前端：门户首页的本月用量卡与按日柱状图、文案、测试、验收脚本一个只读步骤（依赖 1、AIH-TASK-101、AIH-TASK-102、AIH-TASK-105）。
- **回滚或前滚方式**：回滚到上一个镜像即可。
- **部分部署时的行为**：只部署 1 → 接口可用、首页没有这一块。
- **监控指标和告警负责人**：不新增指标；`customer_usage_query_timeout` 日志可以看超时次数；Kelvin 负责。

## 9. 方案取舍

| 方案 | 优点 | 风险 | 未采用原因 |
| --- | --- | --- | --- |
| **采用**：读时在 `usage_events` 上实时汇总，两条 SELECT 同一快照，2 秒预算 | 数字永远最新；不加表、不加后台任务、不需要重建命令；口径与用量页天然一致 | 大客户整月扫描可能超时，首页这一块显示「暂时算不出」 | — |
| 备选 A：按客户 × 项目 × 马来西亚日的聚合表，由定时任务维护、可重建 | 读快、能支撑大客户 | 多一个迁移、一个定时任务与重建命令；有几分钟延迟；按日的去重对话数不能相加，对话数仍要实时算 | Kelvin 2026-10-23 选读时实时算；超时日志常见时再按证据做 |
| 备选 B：按记账时间（钱包流水 `AI_USAGE`）归月 | 与钱包流水对得上 | 与用量页的日期口径不同；请求数、对话数仍要按事件算，同一张卡上两种口径 | Kelvin 2026-10-23 选按调用发生时间 |
| 备选 C：一条 SELECT 用 `WITH ROLLUP` 同时出按日与合计，并在同一条里 `COUNT(DISTINCT)` | 一条语句 | 按日分组里的 `COUNT(DISTINCT conversation_id)` 在合计行上的语义各方言不同，SQLite 不支持 `WITH ROLLUP` | 两条语句、同一快照更清楚 |
| 备选 D：前端用图表库画用量图 | 交互更丰富 | Worker 不能装依赖；图表库通常把数值当浮点 | 柱高只用整数请求数，纯 CSS / SVG 足够 |
| 备选 E：对话数用各日去重数之和 | 可以从按日结果直接得到 | 跨天的同一对话被计多次，数字偏大 | 用整月 `COUNT(DISTINCT)` |

## 10. 未决问题与假设

- **未决问题**：无阻断项。
- **已拍板的取舍**（Kelvin 2026-10-23）：读时实时算；按 `occurred_at` 归月；对话数 = 当月不同的非空 `conversation_id`；用量图 = 当月每天的请求数与计费额。
- **尚未验证的假设**：
  1. AIH-TASK-103、104 按 #398 v4 交付索引、常量、`QueryTooBroad`、日期换算与 `app/schemas/customer_usage.py`。若形状不同，接口任务停下说明，不自行补。
  2. 现阶段客户当月事件数在 2 秒预算内可汇总完（生产上现在只有夹具与内部租户，每月几百到几千条）。错了的后果是首页这一块显示「暂时算不出」，不是错数据；按超时日志另开闸门建聚合表。
  3. 生产 MySQL 的事务隔离级别是默认的 `REPEATABLE READ`（`app/core/database.py` 没有改隔离级别）。错了（例如改成 `READ COMMITTED`）的后果是合计与对话数可能来自相差几毫秒的两个快照，只影响显示，不影响任何财务数据。
- **需要谁拍板**：无新增。
- **如果假设错误，具体后果**：见上。

## 11. 审查与版本绑定

审查方要回答的五个问题常驻在 [scripts/review_checklist.md](../../scripts/review_checklist.md) 的「设计审查」一节，此处不重复。

**版本绑定规则**（规则的出处是 [WORKFLOW §3](../../docs/WORKFLOW.md)；这里保留一份是因为审查材料里只有本 Issue 正文，两处改一起改）：

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

## 12. 版本变更记录

| 版本 | 日期 | 改了什么 | 触发原因 |
| --- | --- | --- | --- |
| v1 | 2026-10-23 | 初稿 | — |

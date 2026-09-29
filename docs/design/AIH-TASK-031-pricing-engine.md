# AIH-TASK-031 设计：计价引擎与管理端试算预览（已批准 v1）

> **来源**：设计闸门 Issue #179。本文件是 `APPROVED: design v1` 那一版正文的**逐字副本**，审查者为 Codex。
> 放进仓库，是因为 OpenClaw Worker 在沙箱里不联网、读不到 GitHub Issue。
> **实现以本文件为准**；与 Issue 不一致时，以 Issue #179 上被批准的 v1 为准。设计要改，就回到 Issue 升版本、重新过闸门，不要直接改本文件。
> 正文里的「T-G」即 [PHASE-2-plan.md](PHASE-2-plan.md) 的占位名；写「编号登记时分配」的实现任务，登记为 `AIH-TASK-031`。正文提到的其他占位名（T-A…T-L）对应的登记编号见 `docs/TODO.md` 的 Phase 2 一节。
> 与 Issue 正文的唯一差别：一处链接目标由 GitHub 网页上的 `../blob/main/…` 改成仓库内的相对路径，链接文字与其余正文逐字相同。

---

## 0. 准入判定

| 任务涉及 | 要填的范围 |
| --- | --- |
| 钱包 / 账本 / 定价 / 汇率 / 支付 / 幂等 / 状态机 | **全部章节** |
| 用量摄取 / 认证与会话（集成侧与平台侧）/ Webhook | §1–§7（**§6 不可省**） |
| 前端 / 文档 / CI / 脚本 | 不走本闸门 |
| 既不碰钱、也不碰用量摄取 / 认证与会话 / Webhook 的后端改动 | 不走本闸门 |

本任务档位：`全部`。它是算钱的那一段：估算供应商成本、MYR 换算、客户计费额与舍入（§14、§15、§80）。

---

状态：`READY_FOR_REVIEW`
负责人：Claude
**设计版本**：`v1`
对应需求：spec §14、§15、§15.1、§16、§17、§17.1、§59（试算预览）、§80、§113；`REQ-PRICE-001`、`REQ-FIN-001`、`REQ-FIN-002`、`REQ-PRIV-002`；ADR-0005（「同一事件重算得出相同 MYR」）、ADR-0008（含税）
目标 PR：待开（批准后在 `.platform/tasks.yaml` 登记实现任务，编号登记时分配）
Phase 2 计划：`docs/design/PHASE-2-plan.md` 的 T-G。依赖：`docs/design/AIH-TASK-025-ai-catalog.md`（已批准 v4，计量分量与 `quantity_field`）、T-B #177（价格版本与 `resolve_provider_price`）、T-C（汇率版本与 `resolve_fx_rate`，`rate` 为 DECIMAL(24,10)、1 单位原币 = 多少 MYR）、T-D #178（定价规则与 `resolve_pricing_rule`）。本设计只引用这三者已写明的契约；三者若在审查中改变契约，本设计随之升版本。

## 1. 目标与边界

- **要解决的问题**：spec §14 要求每个事件保存供应商原币成本、原币种、汇率版本、所用汇率、MYR 估算成本；§15 定义 MARKUP 与 FIXED_RATE 的算法；§80 要求中间值高精度、各分量先求和、存储前只做一次 `ROUND_HALF_UP` 到 8 位；§59 要求管理员能按显式 `occurred_at` 试算，列出每个分量与版本。现在没有任何计算代码。
- **可观察的完成标准**：
  1. 一个纯函数 `price_event(...)`：输入事件的计量数量、价格版本分量、汇率（或无）、定价规则，输出 §14 的全部快照值、计费额与逐分量明细；同样的输入永远得到逐位相同的输出。
  2. 一个解析函数 `resolve_charge_inputs(...)`：在调用方事务里按 T-A / T-B / T-C / T-D 的契约依次取模型、价格版本、汇率、规则，返回齐全的输入或一个明确的错误状态（`MODEL_UNKNOWN` / `PRICING_ERROR` / `FX_RATE_ERROR`）。
  3. 管理端试算接口：给出客户、供应商、模型、用量与 `occurred_at`，返回每个分量、每个版本、未舍入的中间值与舍入结果，或上述错误状态；不写库。
- **明确不做什么**：
  - 取事件、写快照、扣钱包、状态推进 —— T-H（本任务不碰 `usage_events` 与钱包）
  - 税额反算 —— Phase 4（ADR-0008：计费额是含税金额，本任务不拆税）
  - 毛利 —— 由 `billable_cost − 成本` 在报表中派生（§14、Phase 5 / 7），不在这里存
  - 用草稿版本试算（what-if）—— 只用已发布的版本与规则；见 §9
- **现有行为与问题证据**：`app/models/base.py` 的 `quantize_money` 是唯一的 `ROUND_HALF_UP` 到 8 位实现（文件注释要求只有一份）；没有高精度计算上下文与任何计价代码。

## 2. 设计概要

```text
T-H（计费事务内）或试算接口：
  resolve_charge_inputs(session, tenant_id, provider_code, model_code, meter_type, occurred_at, locking)
    1. resolve_model（025）                         → 无 → MODEL_UNKNOWN
    2. resolve_provider_price（T-B）                → 无 → PRICING_ERROR（NO_PROVIDER_PRICE）
       版本不含该事件计量类型的分量                  → PRICING_ERROR（MISSING_PROVIDER_COMPONENT）
    3. source_currency ≠ MYR：resolve_fx_rate（T-C）→ 无 → FX_RATE_ERROR
    4. resolve_pricing_rule（T-D）                  → 无 → PRICING_ERROR（NO_PRICING_RULE）
       FIXED_RATE 规则不含该计量类型的分量            → PRICING_ERROR（MISSING_RULE_COMPONENT），不下落
  price_event(quantities, provider_components, fx_rate, rule) → ChargeResult（纯函数，不碰数据库）
```

错误按上面的顺序判定，**先遇到哪个报哪个**，并带一个细分的 `error_code`（括号里的），供 T-H 写进事件、供告警与管理员排查。

### 计量数量

事件的数量按 025 的 `usage_meter_components.quantity_field` 取：`LLM_TOKEN_FIELDS` 形态的四个分量分别取四个 token 字段；`QUANTITY` 形态的唯一分量取 `quantity`。**事件所需的分量 = 该计量类型在 025 里的全部分量**（不论某个数量是否为 0）。价格版本与 FIXED_RATE 规则在发布时已保证「涉及的计量类型分量齐全」（T-B / T-D 的发布跃迁触发器），所以缺分量只会是「整个计量类型没被覆盖」，判 `PRICING_ERROR`，绝不按 0 算（§15.1）。

### 算法（§14、§15、§80）

全部用 `decimal.Decimal`，在一个专用上下文里计算：`prec = 50`、`rounding = ROUND_HALF_UP`、陷阱全关（`Inexact` / `Rounded` 不抛）。50 位有效数字远超 DECIMAL(20,8) 与 DECIMAL(24,10) 的量级，除法的截断误差在第 40 位以后，不影响任何存储值。**只有下面标「存储」的值做舍入，而且每个都直接由未舍入的中间值舍入一次**，舍入统一调用 `quantize_money`（到 8 位，`ROUND_HALF_UP`）。

对事件的每个分量 i（数量 qᵢ）：

```text
供应商原币分量成本   pcᵢ = qᵢ × price.rate_amountᵢ / price.unit_quantityᵢ        （未舍入）
供应商原币成本       PC  = Σ pcᵢ                                                  （未舍入）
MYR 估算成本         EC  = PC × fx.rate        （source_currency = MYR 时 EC = PC）（未舍入）

MARKUP：
  计费额             B   = EC × rule.markup_multiplier                            （未舍入；只乘一次）
FIXED_RATE：
  客户分量价         bᵢ  = qᵢ × rule.rate_amountᵢ / rule.unit_quantityᵢ           （未舍入）
  计费额             B   = Σ bᵢ                                                   （未舍入；与供应商成本无关）

存储：
  provider_source_cost        = quantize_money(PC)
  estimated_provider_cost_myr = quantize_money(EC)
  billable_cost               = quantize_money(B)
  fx_rate_applied             = fx.rate 原值（DECIMAL(24,10)，不舍入）；MYR 时为空
  provider_source_currency    = price.source_currency
```

- MYR 估算成本与计费额都从**未舍入**的 `PC` / `EC` 算起，不用已舍入的存储值（§15「unrounded MYR estimated provider cost」）；所以「存储的 EC × 倍数」与「存储的 B」可能差最后一位，这是 §80 要求的结果，不是误差 —— 在 `docs/pricing-engine.md` 写明。
- 所有量与价都 ≥ 0（摄取与发布已保证），所以 `B ≥ 0`。**`B` 舍入后可能是 0**（例如全部 token 为 0，或量极小）：见「对下游任务的契约」。
- `billable_cost` 是含税金额（ADR-0008）；本任务不拆税、不存税额。
- 逐分量明细（试算与测试用）包括每个 `pcᵢ`、`bᵢ` 的未舍入值；T-H **不**存逐分量明细（§79 没有这一列；版本与规则不可变，明细随时可由快照引用的版本重算）。

### 确定性（ADR-0005 收口条件）

`price_event` 是纯函数：同样的数量、同样的版本分量、同样的汇率与规则，得到逐位相同的结果（上下文固定，不依赖线程或进程的默认 `decimal` 上下文 —— 用 `localcontext(PRICING_CONTEXT)` 包住）。T-H 的快照引用的版本与规则都不可变（T-B / T-C / T-D 的触发器），所以「同一事件重算得出相同 MYR」：取快照里的版本与规则重新调用 `price_event`，结果与快照一致。本任务提供 `reprice_from_snapshot(...)` 供测试与以后的对账使用（只读）。

### 解析函数与锁（给 T-H）

`resolve_charge_inputs(..., locking=True)`：按 025、T-C、T-D 的契约取锁与加锁读 —— 供应商行 `FOR SHARE`（在 `resolve_model` 里）→ `fx_rate_locks` `FOR SHARE` → `pricing_rule_locks` `FOR SHARE`，价格、汇率、规则都用加锁读。这些锁由调用方的计费事务持有到提交。本函数**不**判断 `occurred_at` 是否晚于当前时间（那是 T-H 在持锁后做的，025 契约），但提供 `locking` 参数给试算用（`locking=False`：普通读、不加锁）。

### 试算接口（§59「preview calculation」）

`POST /api/v1/admin/pricing-preview`，只有 ADMIN，第一条语句 `require_admin`，加进路由枚举。只读，不写库、不写审计（它不改变任何东西）。

请求：

| 字段 | 规则 |
| --- | --- |
| `customer_id` | 租户 `public_id`；不存在 404 |
| `provider` / `model` | 字符串，按 025 的格式；按 `occurred_at` 走 `resolve_model`（含别名） |
| `usage_type` | 计量类型 `code` |
| 数量 | 与摄取同样的形态规则（四个 token 整数，或 `quantity` 字符串 + `unit`） |
| `occurred_at` | 必填，RFC 3339 带时区；可以是未来（§59：「must accept an explicit occurred_at」，未来生效的规则与价格也能预览） |

响应（200）：

```json
{
  "status": "PRICED" | "MODEL_UNKNOWN" | "PRICING_ERROR" | "FX_RATE_ERROR",
  "error_code": null | "NO_PROVIDER_PRICE" | …,
  "model": {"provider": "...", "model": "...", "matched_via": "code" | "alias"},
  "provider_price_version": {"id": "...", "source_currency": "USD", "effective_from": "...", "effective_to": null},
  "fx_rate_version": {"id": "...", "rate": "4.4500000000", "observed_at": "..."} | null,
  "pricing_rule": {"id": "...", "priority_scope": "...", "strategy": "MARKUP", "markup_multiplier": "2.00000000"} | null,
  "components": [{"component_code": "...", "quantity": "...", "provider_cost_unrounded": "...", "customer_price_unrounded": "..." | null}],
  "provider_source_cost_unrounded": "...", "provider_source_cost": "...",
  "estimated_provider_cost_myr_unrounded": "...", "estimated_provider_cost_myr": "...",
  "billable_cost_unrounded": "...", "billable_cost": "...",
  "tax_inclusive": true
}
```

- 错误状态也返回 200，并给出已解析到的部分（例如模型与价格版本都有、缺汇率）—— 试算的目的就是让管理员看到缺什么（§59）；只有请求本身不合法才 4xx。
- 未舍入值以十进制字符串返回（最多 50 位有效数字）。
- 试算包含成本与倍数，只在管理端（INV-7 / REQ-PRIV-002）。
- 试算与计费用同一套解析与计算代码（只差 `locking`），不另写一份。

### 数据库

无新表、无迁移。

### 事务边界

- `price_event`：纯函数，无事务。
- `resolve_charge_inputs`：在调用方事务里运行；本任务不开事务、不提交。
- 试算接口：一个只读事务（`locking=False`）。

### 外部系统与异步边界

无。

### 时间语义

`occurred_at` 决定一切版本与规则的选择（§16、§17、§17.1），由下层函数按半开区间比较；本任务只传递。

### 金额精度

见「算法」。存储值 DECIMAL(20,8)（`fx_rate_applied` 为 DECIMAL(24,10)）；中间值 50 位精度；每个存储值只舍入一次；绝不经过 `float`。

### 审计

无（试算只读；计价结果的审计链由 T-H 的快照与账本承担）。

### 对下游任务的契约

| 下游 | 契约 |
| --- | --- |
| T-H | ① 在计费事务里调用 `resolve_charge_inputs(locking=True)`，锁顺序：供应商（S）→ FX（S）→ 定价规则（S）→ 钱包 → 租户；② 错误状态原样写进事件（`status` 与 `error_code`），不扣费；③ **`billable_cost` 舍入后为 0 时不写账本**（账本对 `AI_USAGE` 要求金额 < 0，`ck_wallet_transactions_sign`），事件照样标 `PROCESSED` 并保存全部快照；④ 快照写 `provider_price_version_id`、`pricing_rule_id`、`fx_rate_version_id`（MYR 时为空）、`fx_rate_applied`（MYR 时为空）、`provider_source_cost`、`provider_source_currency`、`estimated_provider_cost_myr`、`billable_cost`；账本金额 = `-billable_cost` |
| T-L | 试算页按上面的响应展示；标注「含税」「估算成本，客户不可见」 |
| 文档 | 新建 `docs/pricing-engine.md` 的「计算」一节（T-D 写「规则」一节；两者谁先落地谁建文件，另一个追加） |

## 3. 不变量影响矩阵

| 不变量 | 是否触碰 | 可能怎样被破坏 | 设计控制 | 验证方式 |
| --- | --- | --- | --- | --- |
| INV-1 | 否 | 不在 AI 路径 | — | — |
| INV-2 事件不重复扣费 | 否（间接） | — | 本任务不扣费 | — |
| INV-3 / INV-4 / INV-5 | 否 | 不碰钱包与账本 | — | — |
| INV-6 事件保留版本引用 | **是** | 计算结果依赖版本以外的东西（全局 `decimal` 上下文、当前时间），重算不同 | 纯函数、固定上下文、只依赖传入的版本；`reprice_from_snapshot` | 重算一致性用例、跨上下文用例 |
| INV-7 客户不可见成本毛利 | **是** | 试算的成本与倍数经非管理端接口泄露 | 只有管理端路由 | 越权用例 |
| INV-8 租户不可互访 | **是** | 试算用 A 的客户级规则算 B | 规则解析按请求的 `customer_id` 精确匹配（T-D） | 两个客户的试算用例 |
| INV-9 | 否 | — | — | — |
| INV-10 金额用 Decimal | **是** | `float`、默认上下文（28 位）、早舍入、`ROUND_HALF_EVEN` | 专用 50 位上下文；只在存储值处调用 `quantize_money` 一次；静态检查禁止 `float(` 出现在计价模块 | 精度与舍入用例（见 §7） |
| INV-11 / INV-12 | 否 | — | — | — |
| INV-13 | 否（本任务不写库） | — | — | — |
| INV-14 | 否 | — | — | — |

## 4. 状态与并发

- 本任务没有状态机；错误状态（`MODEL_UNKNOWN` / `PRICING_ERROR` / `FX_RATE_ERROR`）是返回值，由 T-H 写入。
- **串行化**：由下层的锁保证（`locking=True` 时），见「解析函数与锁」。
- **唯一性 / 幂等 / 冲突**：不适用（不写库）。
- **原子提交**：不适用。

## 5. 失败与恢复矩阵

| 失败点 | 对外结果 | 数据最终状态 | 是否可重试 | 恢复来源 | 告警 |
| --- | --- | --- | --- | --- | --- |
| 模型未知 | `MODEL_UNKNOWN` | 不写（T-H 写事件状态） | 映射别名后由 T-H 重新入队 | 管理员 | T-H 的维度 |
| 无价格 / 价格缺计量类型 | `PRICING_ERROR` + 细分码 | 同上 | 发布价格后重新入队 | 管理员 | T-H 的维度 |
| 无汇率 | `FX_RATE_ERROR` | 同上 | 发布汇率后重新入队 | 管理员 | T-H 的维度 + T-C 的 `fx_stale` |
| 无规则 / 规则缺计量类型 | `PRICING_ERROR` + 细分码 | 同上 | 发布规则后重新入队 | 管理员 | T-H 的维度 |
| 计费额为 0 | `PRICED`，`billable_cost = 0` | T-H 不写账本 | — | — | 无 |
| 结果超出 DECIMAL(20,8) 的范围（数量与单价都在上限时理论可达 10^24 量级） | 计算抛 `ChargeOverflow` | T-H 把事件标 `FAILED_FINAL`（`error_code = AMOUNT_OUT_OF_RANGE`） | 否 | 人工 | T-H 的维度 |
| 锁等待超时（计费路径） | 由 T-H 处理 | 回滚 | 是 | T-H 重试 | T-H |

## 6. 数据与安全边界

- **租户过滤**：试算的客户级规则只按请求的客户解析（T-D）。
- **鉴权主体**：试算只有 ADMIN；其余是内部函数。
- **禁止返回的字段**：内部 id（版本与规则用 `public_id`）。
- **日志 / 审计 / 异常**：计价模块不打印金额明细到应用日志；异常只带错误码。
- **密钥 / prompt / 客户数据**：不涉及。
- **保留与删除**：不写库。

## 7. 测试证据计划

| 风险/需求 | 测试层级 | 场景 | 预期结果 |
| --- | --- | --- | --- |
| MARKUP（§113） | unit | LLM_TOKEN 四个分量、USD 价格、汇率、倍数 2.0 | 各中间值与三个存储值等于手算结果 |
| FIXED_RATE（§113） | unit | 同一事件用 FIXED_RATE 规则 | 计费额只由规则分量决定，与供应商价与汇率无关；但 §14 的成本快照照算 |
| 缓存读写分量（§113） | unit | 缓存写入、读取为非 0 | 分别按各自单价计入 |
| 非 token（§113） | unit | AUDIO_SECOND 小数秒；IMAGE_GENERATION 整数 | 正确 |
| MYR 原币 | unit | `source_currency = MYR` | 不需要汇率；`fx_rate_version_id`、`fx_rate_applied` 为空；`EC = PC` |
| 只舍入一次（§80） | unit | 构造「逐分量先舍入」与「先求和后舍入」结果不同的数量与单价 | 等于后者 |
| `ROUND_HALF_UP` | unit | 构造第 9 位恰好是 5 的结果 | 进位（与 `ROUND_HALF_EVEN` 不同的那种情形） |
| 未舍入链 | unit | 构造 `quantize(EC) × 倍数` 与 `quantize(EC × 倍数)` 不同 | `billable_cost` 等于后者 |
| 上下文独立 | unit | 在进程默认上下文被改成 `prec = 5`、`ROUND_DOWN` 的情况下计算 | 结果与默认情况逐位相同 |
| 确定性（ADR-0005） | unit | 同一输入算 100 次；`reprice_from_snapshot` | 逐位相同；与首次结果一致 |
| 计费额为 0 | unit | 全部 token 为 0；量极小 | `billable_cost = 0` |
| 溢出 | unit | 数量与单价取上限 | `ChargeOverflow` |
| 错误顺序与细分码 | integration | 分别制造：模型未知；无价格；价格缺计量类型；非 MYR 无汇率；无规则；FIXED_RATE 缺计量类型 | 对应的状态与细分码；同时缺价格与汇率时报先遇到的 `PRICING_ERROR` |
| 不下落 | integration | 客户级 FIXED_RATE 不含该计量类型，全局默认 MARKUP 存在 | `PRICING_ERROR`（`MISSING_RULE_COMPONENT`），不用全局默认 |
| 别名 | integration | 用别名字符串试算 | `matched_via = alias`，结果同用模型代码 |
| 试算接口 | integration（API） | 正常；各错误状态（200 带部分结果）；未来的 `occurred_at` 命中预约版本；非法请求 | 同左 / 422 / 404 |
| 试算不写库 | integration | 调用前后比较所有表行数 | 不变；无审计 |
| 试算与计费同一代码 | unit | 试算与 `resolve_charge_inputs` + `price_event` 对同一输入 | 结果相同 |
| 静态检查 | unit | 扫描计价模块源码 | 没有 `float(`，没有 `ROUND_HALF_EVEN`，`quantize` 只经 `quantize_money` |
| 鉴权 | integration | 匿名 401、CUSTOMER 403；首条语句 `require_admin`；路由枚举 | 同左 |
| 客户隔离 | integration | 对 A、B 两个客户试算，A 有客户级规则 | B 不命中 A 的规则 |

## 8. 迁移与上线

- **数据迁移步骤**：无。
- **部署顺序**：合并即自动部署；试算接口立即可用（有价格、汇率、规则之后才有结果）。
- **回滚**：纯代码，回滚即可。
- **监控**：无新增；错误状态的告警由 T-H 负责。

## 9. 方案取舍

| 方案 | 优点 | 风险 | 未采用原因 |
| --- | --- | --- | --- |
| **采用**：纯函数 + 专用 50 位上下文 + 每个存储值从未舍入值舍入一次 | §80 字面；可重算、可测试 | — | — |
| 用舍入后的 `estimated_provider_cost_myr` 乘倍数 | 「计费额 = 存储的成本 × 倍数」对得上 | 违反 §15「unrounded MYR estimated provider cost」 | 不采用；差异在文档里说明 |
| 用进程默认的 `decimal` 上下文 | 少一行代码 | 默认精度 28 位、可被别处代码改掉，重算不一致 | 专用上下文 |
| 事件上存逐分量明细 | 审计更直接 | §79 没有这列；版本不可变，明细可由快照重算 | 不存；提供 `reprice_from_snapshot` |
| 试算允许用草稿版本 | 发布前 what-if | 试算与计费的解析路径不同，要多一套代码与测试 | V1 不做；需要时另开任务 |
| 计费额为 0 也写一行 0 元账本 | 账本与事件一一对应 | 账本 CHECK 要求 `AI_USAGE < 0`，改 CHECK 要动 AIH-TASK-005 的已上线约束；0 元行对余额无意义 | 0 元不写账本（T-H 契约）；事件快照仍完整 |
| 缺规则分量时下落到更低一级 | 少一些错误 | 同一事件的价格取决于配置缺口（T-D 已决定不下落） | 不采用 |

## 10. 未决问题与假设

- **未决问题**：无阻断项。
- **需要谁拍板**：无。
- **尚未验证的假设**：
  1. T-C 的最终契约是「`rate` = 1 单位原币等于多少 MYR，DECIMAL(24,10)」—— T-C 正在拟定（v1 草稿即此）；若它改变，本设计升版本。
  2. T-B / T-D 批准的版本保留「发布跃迁保证计量类型分量齐全」—— 两者 v2 都写了这一条，正在审查。
- **如果假设错误**：随之升版本重审；算法本身不变。

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

- `app/services/pricing_engine.py`（新：上下文、`price_event`、`resolve_charge_inputs`、`reprice_from_snapshot`）
- `app/schemas/pricing_preview.py`（新）、`app/api/admin_pricing_preview.py`（新）、`app/main.py`
- `tests/backend/test_pricing_engine.py`、`tests/backend/test_pricing_preview_api.py`（新）
- `tests/backend/test_admin_customers_api.py`（路由枚举）
- `docs/pricing-engine.md`（「计算」一节）、`docs/api.md`、`docs/TODO.md`

## 12. 版本变更记录

| 版本 | 日期 | 改了什么 | 触发原因 |
| --- | --- | --- | --- |
| v1 | 2026-09-29 | 初稿 | — |

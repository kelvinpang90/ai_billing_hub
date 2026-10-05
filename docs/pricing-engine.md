# 计价引擎

客户被扣多少钱由两部分决定：**选哪条定价规则**（本文件「规则」一节，AIH-TASK-027），与**按规则怎么算**（「计算」一节，
AIH-TASK-031）。spec 依据是 §14、§15、§15.1、§16、§17.1、§80；含税口径见
[ADR-0008](adr/ADR-0008-sst-tax-treatment.md)。计算的设计全文见
[design/AIH-TASK-031-pricing-engine.md](design/AIH-TASK-031-pricing-engine.md)（设计闸门 #179 v1）。

接口见 [api.md](api.md) 的「管理端定价规则」；表、触发器与锁顺序见 [database-schema.md](database-schema.md) 的「客户定价
规则」；设计全文见 [design/AIH-TASK-027-pricing-rules.md](design/AIH-TASK-027-pricing-rules.md)（设计闸门 #178 v4）。

## 规则

### 两种策略（spec §15）

| 策略 | 客户计费额 | 规则里存什么 |
| --- | --- | --- |
| `MARKUP` | MYR 估算成本 × `markup_multiplier` | 倍数（> 0），没有分量 |
| `FIXED_RATE` | 按分量取数 × `rate_amount` / `unit_quantity` 求和，与供应商成本无关 | 分量（每个引用 025 目录里的一个计价分量），没有倍数 |

一条规则只能是其中一种：混合（MARKUP 带分量、FIXED_RATE 带倍数）在接口层 422，在数据库层被 CHECK 与复合外键拒绝。
FIXED_RATE 发布时必须完整：至少一个分量，出现的计量类型的全部分量都在（出现 `LLM_TOKEN` 就要输入、输出、缓存写入、
缓存读取四个价）。

### 含税（ADR-0008）

**规则里的价格一律是 MYR 含税价（tax-inclusive）。**

- FIXED_RATE 分量的 `rate_amount` 是 **MYR 含税单价**，币种固定为 MYR。
- MARKUP 的倍数没有「含税」与否之分：它乘的是供应商成本（换算成 MYR 的估算成本），得到的就是含税计费额；税率变化吃掉
  的是毛利（ADR-0008 已写明）。
- 本任务不存税额、不引用税务政策版本；税额反算与 `tax_policy_versions` 归 Phase 4。录入者把不含税价录进来不会报错，
  这是 ADR-0008 已接受的代价；界面标注「含税」由前端任务负责。

### 五级范围与优先顺序（spec §16）

每条规则属于下面五级之一（`priority_scope`），从高到低：

| 顺序 | `priority_scope` | 范围 | 匹配事件的条件 |
| --- | --- | --- | --- |
| 1 | `CUSTOMER_PROVIDER_MODEL` | 某客户 + 某供应商 + 某模型 | 租户、供应商、模型都等于事件的 |
| 2 | `CUSTOMER_PROVIDER` | 某客户 + 某供应商 | 租户、供应商相等 |
| 3 | `CUSTOMER` | 某客户的默认 | 租户相等 |
| 4 | `GLOBAL_PROVIDER_MODEL` | 全局 + 某供应商 + 某模型 | 供应商、模型相等 |
| 5 | `GLOBAL` | 全局默认 | 总是 |

范围与三列（租户、供应商、模型）的 NULL 组合一一对应，§16 没有的组合（例如「全局 + 供应商，不带模型」「客户 + 模型，
不带供应商」）写不进去。客户级规则只按事件自己的租户精确匹配，一个租户的规则永远用不到另一个租户的事件上（INV-8）。

### 解析：逐级下落，命中即停

计费侧对一个事件（租户, 供应商, 模型, `occurred_at`）调用 `resolve_pricing_rule`，按上表顺序逐级找「范围匹配、状态为
`PUBLISHED` 或 `RETIRED`、`effective_from ≤ occurred_at < effective_to`」的规则（`NULL` 边界视为无穷）：

1. **第一级命中即返回**（规则 + 分量），不再看更低的级别；
2. 这一级没有生效的规则，才**下落**到下一级；
3. 五级都没有 → 没有规则，计费侧判 `PRICING_ERROR`（不按 0 算）。

⚠️ **命中后不因缺分量而下落**：命中的是一条 FIXED_RATE 规则、而它缺事件所需的分量（例如客户级规则只定了
`AUDIO_SECOND` 的价，事件却是 `LLM_TOKEN`）时，结果是 `PRICING_ERROR`，**不会**去用更低一级的规则。否则同一事件的
价格会取决于配置的缺口，「选定的规则」也变得含糊。

停用（`RETIRED`）的规则在它自己的区间里照样命中（已经发生的用量照样按它算）；停用之后的时刻这一级不再适用，事件下落
到更低一级。

### 时间：只在末尾追加，一旦选定永不改变

- 每个范围是一条时间线：同一范围的规则区间首尾相接、不重叠，新规则只加在末尾（可预约未来生效），前一条被截断在新规则
  的起点。边界时刻 `t` 是发布时服务端当前时间向上取整到下一个整秒；所有时间都是不带时区的 UTC，半开区间。
- **回溯生效只允许「全局默认」的第一条**：`GLOBAL` 范围从没有已发布规则时，不指定生效时刻的发布从「一直以来」起。
  其他任何一级补一条对过去生效的规则，都会让那些原本在更低一级选中规则、已经计过费的时刻改选这一级；`GLOBAL` 没有
  「以下」，补上只把「无规则」变成「有规则」。其余一切情况 `effective_from ≥ t`。
- 停用留下的空档不回填。
- 发布 / 停用在 `pricing_rule_locks` 那一行的排他锁里完成、`t` 在拿到锁之后取；计费在同一行的共享锁里解析，且只处理
  `occurred_at` 不晚于持锁后当前时间的事件。所以**一个（租户, 供应商, 模型, 时刻）一旦选到某条规则，以后永远选到同一
  条**（原本无规则的时刻除外）；重试同一个事件不会换到新发布的规则（spec §16 最后一句）。换规则重算历史归 Phase 8
  的 reprocess。

### 给计费侧的调用约定

在计费事务里依次：`resolve_model`（供应商行共享锁）→ `lock_pricing_rules_shared`（`pricing_rule_locks` 共享锁，持有到
事务提交）→ 价格与规则的加锁读（`resolve_provider_price`、`resolve_pricing_rule`）→ 钱包 → 租户；把规则的内部 id 写进
事件快照（`pricing_rule_id`）。锁顺序固定为 **供应商（S）→ 定价规则锁（S）→ 钱包 → 租户**。

## 计算

代码在 `app/services/pricing_engine.py`（AIH-TASK-031）。三个函数：`price_event`（纯函数，不碰数据库）、
`resolve_charge_inputs`（在调用方事务里取模型、价格、汇率与规则）、`reprice_from_snapshot`（按快照引用的版本重算，
只读）。管理端试算接口（[api.md](api.md) 的「管理端试算预览」）与计费用的是同一套解析与计算代码，只差 `locking`。

### 事件需要哪些分量

事件的数量按 025 的 `quantity_field` 取：`LLM_TOKEN` 的四个分量分别取 `input_tokens`、`output_tokens`、
`cache_creation_input_tokens`、`cache_read_input_tokens`；`QUANTITY` 形态的唯一分量取 `quantity`。**事件需要的分量 =
它的计量类型在 025 里的全部分量**，不论某个数量是否为 0。价格版本或 FIXED_RATE 规则缺其中任何一个 → `PRICING_ERROR`，
**绝不按 0 算**（§15.1）。版本或规则另外定了别的计量类型的价，与这个事件无关。

### 算法（§14、§15、§80）

对事件的每个分量 i（数量 qᵢ）：

```text
供应商原币分量成本   pcᵢ = qᵢ × price.rate_amountᵢ / price.unit_quantityᵢ        （未舍入）
供应商原币成本       PC  = Σ pcᵢ                                                  （未舍入）
MYR 估算成本         EC  = PC × fx.rate        （原币为 MYR 时 EC = PC，不查汇率）（未舍入）

MARKUP：      B  = EC × markup_multiplier                                        （未舍入；只乘一次）
FIXED_RATE：  bᵢ = qᵢ × rule.rate_amountᵢ / rule.unit_quantityᵢ；B = Σ bᵢ         （未舍入；与供应商成本无关）

存储：
  provider_source_cost        = quantize_money(PC)
  estimated_provider_cost_myr = quantize_money(EC)
  billable_cost               = quantize_money(B)
  fx_rate_applied             = fx.rate 原值（DECIMAL(24,10)，不舍入）；MYR 时为空
  provider_source_currency    = 价格版本的 source_currency
```

`fx.rate` 是 1 单位原币等于多少 MYR（[currency-and-fx.md](currency-and-fx.md)）。FIXED_RATE 的成本快照照算（§14），
只是计费额不用它。

### 精度与舍入

- 全部用 `decimal.Decimal`，在专用上下文 `PRICING_CONTEXT` 里计算：`prec = 50`、`ROUND_HALF_UP`、陷阱全关，用
  `localcontext` 包住。**不依赖进程或线程的默认上下文**（默认只有 28 位，而且可能被别处的代码改掉）：同样的输入在
  任何默认上下文下都得到逐位相同的结果。50 位远超 DECIMAL(20,8) 与 DECIMAL(24,10) 的量级，除法的截断误差在第 40 位
  以后，不影响任何存储值。
- **只有存储值舍入，每个都直接由未舍入的中间值舍入一次**（§80：各分量先求和，再舍入一次）。舍入只经
  `app/models/base.py` 的 `quantize_money`（8 位小数、`ROUND_HALF_UP`）。逐分量先舍入会出错：两个各 0.000000004 的
  分量，先舍入得 0，先求和得 0.00000001。
- MYR 估算成本与计费额都从**未舍入**的值算起（§15「unrounded MYR estimated provider cost」）。所以**「存储的
  `estimated_provider_cost_myr` × 倍数」与「存储的 `billable_cost`」可以差最后一位**：例如 EC = 0.07866666666588、
  倍数 2，存储的 EC 是 0.07866667，而 B = quantize(0.15733333333176) = 0.15733333，不是 0.07866667 × 2 = 0.15733334。
  这是 §80 要求的结果，不是误差；对账时按快照引用的版本重算（`reprice_from_snapshot`），不要拿两个存储值互推。
- 计价模块里没有任何浮点转换，也没有第二份舍入实现（测试扫描源码）。

### 计费额为 0 与溢出

- 所有量与价都 ≥ 0，所以计费额 ≥ 0。**舍入后可以是 0**（全部 token 为 0，或量极小）：事件照样 `PROCESSED`、保存全部
  快照，但**不写账本**（账本对 `AI_USAGE` 要求金额 < 0）。
- 任何一个存储值超出 DECIMAL(20,8)（整数部分 12 位）：抛 `ChargeOverflow`（只带错误码 `AMOUNT_OUT_OF_RANGE`，不带
  金额）；计费侧把事件标 `FAILED_FINAL`。试算接口回 422。

### 解析与错误顺序

`resolve_charge_inputs` 按下面的顺序取输入，**先遇到哪个错误报哪个**，带一个细分码：

| 步骤 | 没有时的状态 | `error_code` |
| --- | --- | --- |
| 1. `resolve_model`（含别名，按 `occurred_at`） | `MODEL_UNKNOWN` | 空 |
| 2. `resolve_provider_price` | `PRICING_ERROR` | `NO_PROVIDER_PRICE` |
| 　 价格版本不含该计量类型的全部分量 | `PRICING_ERROR` | `MISSING_PROVIDER_COMPONENT` |
| 3. 原币不是 MYR 时 `resolve_fx_rate` | `FX_RATE_ERROR` | 空 |
| 4. `resolve_pricing_rule` | `PRICING_ERROR` | `NO_PRICING_RULE` |
| 　 命中的 FIXED_RATE 规则不含该计量类型的全部分量（**不下落**） | `PRICING_ERROR` | `MISSING_RULE_COMPONENT` |

所以同时缺价格与汇率时报 `PRICING_ERROR`（`NO_PROVIDER_PRICE`）。错误状态不扣费；管理员补上目录、价格、汇率或规则后
由计费侧重新入队。

### 锁（给计费侧）

`resolve_charge_inputs(locking=True)` 的取锁顺序：**供应商行（S，在 `resolve_model` 里）→ `fx_rate_locks`（S）→
`pricing_rule_locks`（S）**，之后价格、汇率、规则都是加锁读；这些锁由调用方的计费事务持有到提交，后面接钱包 → 租户。
函数本身不判断 `occurred_at` 是否晚于当前时间：那是计费侧持锁后做的（025 契约）。

`locking=False`（试算）不取两张单行锁表的共享锁，发布不必等试算。025 / 026 / 027 / 028 的解析函数自身的读按它们的
契约不变（MySQL 上是 `FOR SHARE`），随试算的只读事务结束即释放。

### 确定性与重算（ADR-0005）

`price_event` 是纯函数：同样的数量、版本分量、汇率与规则，得到逐位相同的结果。快照引用的价格版本、汇率版本与规则
发布后都不可变，所以「同一事件重算得出相同 MYR」：`reprice_from_snapshot` 取快照里的版本与规则重新调用
`price_event`，结果与快照一致；之后发布的新版本只影响它们生效之后的时刻。

### 给计费侧（T-H / AIH-TASK-032）的契约

1. 在计费事务里调用 `resolve_charge_inputs(locking=True)`；锁顺序：供应商（S）→ FX（S）→ 定价规则（S）→ 钱包 → 租户；
2. 错误状态原样写进事件（`status` 与 `error_code`），不扣费；
3. **`billable_cost` 舍入后为 0 时不写账本**，事件照样标 `PROCESSED` 并保存全部快照；
4. 快照写 `provider_price_version_id`、`pricing_rule_id`、`fx_rate_version_id`（MYR 时为空）、`fx_rate_applied`（MYR 时
   为空）、`provider_source_cost`、`provider_source_currency`、`estimated_provider_cost_myr`、`billable_cost`；账本金额 =
   `-billable_cost`。逐分量明细不存（版本与规则不可变，随时可由快照重算）；
5. `ChargeOverflow` → 事件 `FAILED_FINAL`（`AMOUNT_OUT_OF_RANGE`）。

`billable_cost` 是含税金额（ADR-0008）：本任务不拆税、不存税额；毛利由报表从 `billable_cost − 成本` 派生，不在这里存。

# ADR-0006 — 支付网关：先定准入契约与适配器边界，供应商选型延到 Phase 4 前

| | |
| --- | --- |
| **状态** | 已接受 |
| **日期** | 2026-09-10 |
| **来源** | [TODO.md](../TODO.md) 的 D6。spec §41 列出了评估项与候选，但把选型留给「implementation 期间」 |
| **影响** | `payments`、`payment_gateway_events`（spec §74.7）、Phase 4（spec §127） |
| **相关** | Invariant 3（同一笔支付永不重复入账）、spec §43 支付流程 |

---

## 背景

D6 被记为「必须先做的前置决策」，因为它看起来会决定表结构。但拆开看，`payments` 与 `payment_gateway_events` 的结构只依赖网关**必须满足的性质**，不依赖是哪一家：

- 要建 `(gateway, gateway_event_id)` 唯一约束，只需要确认「网关会给一个稳定唯一的标识」
- 要做 spec §43 要求的 Celery Beat 补偿扫描，只需要确认「网关有服务端状态查询」

而选型本身需要的输入——手续费结构、结算周期、合同条件——是商务信息，且在没有真实交易量预测之前做选择，是用最少的信息做最贵的绑定。

所以本 ADR 把 D6 拆成两半：**现在定契约，之后定供应商。**

## 决策

### 1. 四条硬性准入条件

任何候选网关必须**全部**满足，否则不进入候选：

| | 条件 | 为什么是硬性的 |
| --- | --- | --- |
| **H1** | 回调携带**稳定且唯一**的网关侧标识；同一笔支付被重复投递时该标识不变 | 没有它，`(gateway, gateway_event_id)` 唯一约束建不起来，**Invariant 3 在支付侧就落不了地** |
| **H2** | 提供服务端**权威状态查询**接口 | spec §43 明确要求「Webhook delivery is not assumed reliable」，补偿扫描完全依赖这个接口 |
| **H3** | 回调可验签或可鉴权 | spec §43 的可信确认第一条 |
| **H4** | 提供沙箱环境 + 结算对账明细 | 没有沙箱就无法在接入前实测 H1–H3；没有对账明细就无法核对网关手续费（spec §44） |

外加 **FPX 支持** —— V1 的优先支付方式（spec §41）。

### 2. 适配器接口

钱包逻辑只依赖以下接口，不依赖任何具体网关的代码或字段（spec §41：「Wallet logic must never directly depend on specific gateway code」）：

```text
create_payment(tenant, amount, currency, reference) -> gateway_payment_ref, redirect_url
get_payment_status(gateway_payment_ref)            -> 权威状态 + 金额 + 币种
verify_webhook(raw_request)                        -> 验签结果
parse_webhook(raw_request)                         -> gateway_event_id, gateway_payment_ref, 状态, 金额, 币种
list_settlements(date_range)                       -> 对账明细
```

`parse_webhook` 必须返回 `gateway_event_id`——这是 H1 在代码层的落点。

### 3. 候选短名单

Billplz、Curlec by Razorpay、iPay88、Fiuu、ToyyibPay、Stripe。

spec §41 已列出其中几家。**这份名单里的费率与能力信息是二手的，未经验证**，只用于确定评估范围，不构成推荐。

### 4. 选型转为商务任务，硬截止在 Phase 4 开工前

选定之后补写一份新的 ADR，记录选型理由、实测的 H1–H4 结果、费率与结算周期。

## 备选方案

### A. 现在就选定一家

否决理由：需要费率、合同、结算周期等商务输入，这些现在拿不到；而一旦写进 ADR 和适配器实现，换供应商的代价会显著上升。

### B. V1 同时接两家

否决理由：双倍的对账、监控与运维成本，V1 的交易量撑不起这个开销。适配器接口本身已经保留了将来加第二家的能力。

### C. 不定契约，等选型时一起定

否决理由：这才是真正会阻塞的做法。`payments` 表的唯一约束悬空，Phase 1 就动不了。D6 之所以被列为「前置决策」，正是因为这个依赖——本 ADR 通过定契约切断了它。

## 后果

### 正面

- Phase 1–3 立即解除阻塞，表结构可以现在就定下来
- 四条准入条件把「接入到一半才发现网关不给稳定回调标识」这类返工**前移到签约之前**
- 适配器边界写死在 ADR 里，避免将来实现时图省事直接在钱包逻辑里引网关字段

### 负面 —— 已知代价

- **适配器接口是在没有任何真实网关接入经验的情况下设计的**，第一次真正接入时大概率要改。这是明知的代价：改一层适配器，好过改表结构和账本语义。
- **短名单里的信息未经验证。**H1–H4 必须在签约前用沙箱逐条实测，不能凭供应商文档——文档说「回调带 transaction id」和「重复投递时该 id 不变」是两件事。
- 推迟选型意味着 Phase 4 开工前存在一个**硬截止的商务依赖**。如果到时候还没定，Phase 4 直接停摆，而且没有技术手段可以绕过。

## 收口条件

- Phase 1 建 `payments` / `payment_gateway_events` 时，按 H1 落 `(gateway, gateway_event_id)` 唯一约束
- Phase 4 开工前完成：供应商选定 + 沙箱实测 H1–H4 + 补写选型 ADR
- 若最终选定的网关无法满足 H1，必须在那份 ADR 里写明退化方案与它的残留风险（例如以内部 `payment_id` 与网关交易号的组合作为幂等键），**不允许默认它满足**

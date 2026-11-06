# ADR-0015 — 支付网关选定 Stripe：沙箱实测 H1–H4 的结果

| | |
| --- | --- |
| **状态** | **已接受**：决策人 2026-10-15 选定 Stripe；H1–H4 沙箱实测于 2026-10-19 至 2026-11-06 完成 |
| **日期** | 2026-11-06 |
| **来源** | [ADR-0006](ADR-0006-payment-gateway-contract.md) 第 4 节与「收口条件」；[TODO.md](../TODO.md) 的 D6 |
| **影响** | Phase 4（spec §127）的支付网关适配器、支付 Webhook、stale payment 对账、网关手续费核对（spec §44）；`payments` / `payment_gateway_events`（spec §74.7） |
| **相关** | Invariant 3（同一笔支付永不重复入账）、spec §41、§43 |

---

## 背景

ADR-0006 定了四条硬性准入（H1–H4）加 FPX 支持，要求选定供应商后**用沙箱逐条实测**、补写选型 ADR，硬截止在 Phase 4 开工前。决策人 2026-10-15 选定 Stripe 并申请了账号。

实测环境：本项目专用的 Stripe 测试模式 sandbox（马来西亚、结算币种 MYR，只用于本项目），API 版本 `2026-09-30`。全部是测试模式，没有真实资金流动。测试密钥只放在操作者本机，不进仓库。

## 决策

**V1 的支付网关选定 Stripe，FPX 作为优先支付方式。** H1–H4 与 FPX 的实测结果如下，全部满足。

### 实测结果

| | 条件 | 结果 | 怎么测的 |
| --- | --- | --- | --- |
| **FPX** | 支持 FPX | 通过 | 2026-10-19：RM100 的 FPX 测试支付成功；失败路径下 PaymentIntent 回到 `requires_payment_method`，错误码 `payment_method_not_available` |
| **H1** | 回调带稳定唯一的标识，重复投递时不变 | 通过 | 2026-10-30：用 Stripe CLI 的 `listen` 把回调转到本机接收脚本，一笔测试卡支付收到 `charge.succeeded`、`payment_intent.succeeded`、`payment_intent.created` 三个事件；再用 `events resend` 重发 `payment_intent.succeeded`，第二次投递的事件 id 与第一次相同，请求体逐字节一致，只有签名头里的时间戳不同 |
| **H2** | 服务端权威状态查询 | 通过 | 2026-10-19：`GET /v1/payment_intents/{id}` 返回权威状态、金额与币种；被拒付与未付款都是 `requires_payment_method`，靠 `last_payment_error` 区分 |
| **H3** | 回调可验签 | 通过 | 2026-10-30：`Stripe-Signature` 头带时间戳 `t` 与 `v1` 签名，`v1` = HMAC-SHA256(签名密钥, `t` + `.` + 原始请求体)；四次投递全部验过，请求体改动一个字节后全部验不过 |
| **H4** | 沙箱 + 结算对账明细 | 通过 | 2026-10-19 / 26：PaymentIntent 展开 `latest_charge.balance_transaction` 能拿到逐笔金额、手续费、净额、可用时间与状态，资金约 T+6 日进入可用余额（单一样本）。2026-10-30 绑定测试银行账户、提现计划改为自动每日（延迟 5 天）后，2026-11-02 发出两笔自动提现，状态 `paid`、`reconciliation_status` 为 `completed`。2026-11-06 复查：用提现 id 查 `balance_transactions?payout=<提现 id>`，每笔提现各返回提现流水本身与对应的那一笔收款流水，金额、手续费、净额与支付当时记录的逐字一致（卡 10000 / 500 / 9500，FPX 10000 / 400 / 9600），非提现流水的净额之和等于提现金额 |

### 对适配器接口（ADR-0006 第 2 节）的落点

- `parse_webhook` 的 `gateway_event_id` 取 Stripe 事件的 `id`（`evt_…`）。`(gateway, gateway_event_id)` 唯一约束按 ADR-0006 原样成立，**不需要退化方案**。
- `verify_webhook` 按 H3 的签名方式实现，并校验时间戳与当前时间的差在容忍窗口内（防重放）。签名密钥**按端点**发放：CLI 监听、测试端点、生产端点各有自己的密钥，不能混用。
- `get_payment_status` 用 PaymentIntent 查询。对账与补偿扫描以它为准，回调只是提示（spec §43）。
- `create_payment`：这个 API 版本下，PaymentIntent 不接受 `payment_method_types` 参数，可用的支付方式只能在 Dashboard 里开关。适配器不传支付方式，由账户配置决定；FPX 是否开着，属于部署前的检查项。
- `create_payment` 必须带 `Idempotency-Key`：同一个键重放，返回的是同一个 PaymentIntent（2026-10-19 实测）。
- `list_settlements` 以余额流水为单位。卡支付的流水类型是 `charge`、来源是 `ch_…`；FPX 的是 `payment`、来源是 `py_…`。**对账代码不能只认 `charge`**，要按 PaymentIntent 的 `latest_charge` 对来源。
- 提现对账只对**自动提现**有效：生产账户的提现计划必须设为自动，手动提现的 `reconciliation_status` 是 `not_applicable`，反查不到逐笔流水（2026-11-06 复查确认自动提现可以反查）。
- 自动提现**按支付方式拆成多笔**：同一天卡与 FPX 各发了一笔提现。对账按提现 id 逐笔核对，不能假设一天只有一笔提现
- MYR 的最低支付金额是 RM2（低于它，创建 PaymentIntent 返回 `amount_too_small`）。充值金额的下限不能低于它。

## 备选方案

### A. 继续按 ADR-0006 的短名单比较其余五家再定

否决理由：决策人已于 2026-10-15 选定 Stripe，H1–H4 与 FPX 全部实测通过，没有触发 ADR-0006「无法满足 H1 时写退化方案」的情形。再比较只会推迟 Phase 4，而适配器接口保留了以后加第二家的能力（ADR-0006 备选 B）。

### B. 等 Phase 4 的回调端点部署后再测 H1、H3

否决理由：H1 的结论决定 `payment_gateway_events` 的幂等键怎么设计。它若不成立，要在设计闸门之前写退化方案；等端点上线才发现，改的是迁移、适配器与测试。决策人 2026-10-19 原本选了这条路，2026-10-30 改为先用本机 CLI 测。

## 后果

### 正面

- D6 收口，Phase 4 支付相关的设计闸门可以开，设计以实测结果为准，而不是供应商文档。
- 幂等键直接用 Stripe 事件 id，Invariant 3 在支付侧按 ADR-0006 原样落地。

### 负面 —— 已知代价

- **H1、H3 是经 CLI 转发测的，不是 Stripe 向公网端点的直接投递。**Stripe 对失败投递的自动重试节奏与重试时的事件 id 没有实测。Phase 4 支付 Webhook 任务的上线验收，必须在生产端点上再验一次：首次故意返回非 2xx，确认 Stripe 自动重试时事件 id 不变，并用该端点自己的签名密钥验签。
- **手续费只在测试模式里看过，费率与结算周期不记在本 ADR。**这**偏离 ADR-0006 第 4 节**（选型 ADR 要记录费率与结算周期）：仓库是公开的，供应商合同价不进仓库（ADR-0001）；测试模式按笔扣的手续费也不是合同价，结算周期只有一个约 T+6 的样本。真实费率与结算周期由决策人在 Phase 4「支付网关适配器与支付 Webhook」的设计闸门之前，按 Stripe 的商务条款确认，记在仓库外的私有位置；那个设计闸门要写明 spec §44 的手续费核对以什么为预期值（合同费率，或逐笔对账明细里的实际手续费）。
- **支付方式靠 Dashboard 开关、不在代码里。**有人在 Dashboard 关掉 FPX，代码不会知道。部署检查与运维文档要写这一条。
- 绑定单一供应商：换网关要重写适配器，并迁移在途支付的对账（ADR-0006 已接受这个代价）。

## 收口条件

- Phase 4 支付 Webhook 任务的上线验收包含上面「负面」第一条的生产端点复验（自动重试下事件 id 不变、按端点密钥验签）。
- 生产账户启用前确认：提现计划为自动；FPX 已在 Dashboard 开启；回调端点只订阅适配器用到的事件类型。
- ADR-0004「收口条件」第 ⑤ 项（支付网关回调的幂等标识）以本 ADR 的 H1 为准。

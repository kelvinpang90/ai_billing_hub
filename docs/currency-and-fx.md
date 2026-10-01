# 币种与汇率（FX）

> 依据：设计闸门 #183 `APPROVED: design v3`（[design/AIH-TASK-028-fx-rates.md](design/AIH-TASK-028-fx-rates.md)）、
> [ADR-0005](adr/ADR-0005-fx-rate-source.md)、spec §17.1、§74.2。
> 本文件目前只有**规则部分**（AIH-TASK-039）。BNM 拉取一节由 AIH-TASK-040 补上；管理端接口见 `docs/api.md`（AIH-TASK-041）。
> 规则与设计文件不一致时，以设计文件为准。

代码：`app/services/fx_rates.py`（草稿、发布、退役）、`app/repositories/fx_rates.py`（`resolve_fx_rate`、锁）、`app/models/fx_rates.py`（表、CHECK；触发器在迁移 0015）。

## 1. `rate` 的含义

- 一个版本只描述一个币种对：`base_currency` → `MYR`（`quote_currency` 永远是 `MYR`）。
- `rate` = **1 单位 `base_currency` 等于多少 MYR**。例：`base_currency = USD`、`rate = 4.083` 表示 1 USD = 4.083 MYR。
- 精度：DECIMAL(24,10)。手工录入最多 10 位小数、必须 > 0，**超精度就拒绝，不舍入**（422 `VALIDATION_ERROR`）；全程 `Decimal`，不经过 `float`。
- 手工录入的 `base_currency` 是任何 `^[A-Z]{3}$` 且 ≠ `MYR` 的代码（格式只在应用层校验），不限于 BNM 自动拉取的配置币种。
- 这是**中间价**（BNM 吉隆坡中午场）。买卖差价与汇率波动由定价的 markup 吸收（ADR-0005）。

## 2. 状态

| 当前 | 操作 | 结果 |
| --- | --- | --- |
| — | BNM 拉取 / 手工录入 | `DRAFT` |
| `DRAFT` | 编辑 | 手工草稿可改 `rate`、`observed_at`、`source_reference`；BNM 草稿 409 `FX_RATE_NOT_EDITABLE`（丢弃后手工录入） |
| `DRAFT` | 丢弃 | `DISCARDED`（留痕，不删除） |
| `DRAFT` | 发布 | `PUBLISHED`，见第 4 节 |
| `DRAFT` | 退役 | 409 `FX_RATE_NOT_RETIRABLE`（草稿用丢弃） |
| `PUBLISHED`（未截断） | 退役 | `RETIRED`，见第 5 节 |
| `PUBLISHED`（已截断） | 退役 | 409 `FX_RATE_NOT_RETIRABLE` |
| `PUBLISHED` | 编辑 / 丢弃 | 409 `FX_RATE_NOT_DRAFT` |
| `PUBLISHED` | 发布 | 成功、什么都不写，请求里的 `effective_from` 被忽略 |
| `RETIRED` / `DISCARDED` | 任何写操作 | 409 `FX_RATE_FINAL` |

不存在的版本：404 `FX_RATE_NOT_FOUND`。自动拉取**绝不**发布：只有管理员手工发布（ADR-0005 §3）。

## 3. 时间线术语

所有时刻都是 UTC、不带时区、精确到秒；区间是半开区间 `[effective_from, effective_to)`，`NULL` 起点 = 一直以来，`NULL` 尽头 = 仍生效。

- **非空区间**：`effective_from IS NULL OR effective_to IS NULL OR effective_from < effective_to`。
- **空区间** `[F, F)`：永不匹配任何时刻，只出现在 `RETIRED` 行上（撤销的预约、同一秒里发布又退役）。
- **时间线**：一个币种对里区间非空的 `PUBLISHED` / `RETIRED` 行。**空区间的行不在时间线上**：不当末尾版本、不算「有过版本」、不进事务内复查的排序。时间线上的行互不相交。
- **末尾版本 L**：时间线里 `effective_from` 最晚的一行（为空视为最早）。
- **`t`**：拿到 `fx_rate_locks` 排他锁**之后**取的服务端当前时间，截到整秒再加 1 秒（恰好整秒也加 1 秒，`boundary_after`）；`t` 严格晚于「现在」。
- **F**：发布请求可带的预约时刻（UTC 整秒）。

## 4. 发布：P1–P4

先判 **F < `t` → 422 `EFFECTIVE_FROM_IN_PAST`**，先于下表任何判断。

| 行 | L 的情形 | 未给 F | 给了 F（F ≥ `t`） |
| --- | --- | --- | --- |
| P1 | 没有 L（时间线为空） | 本版本 `[NULL, NULL)` | 本版本 `[F, NULL)` |
| P2 | L 未截断、已开始（起点为空或 < `t`） | 本版本 `[t, NULL)`；L 截断于 `t` | 本版本 `[F, NULL)`；L 截断于 F |
| P3 | L 未截断、尚未开始（起点 ≥ `t`） | 409 `EFFECTIVE_FROM_CONFLICT`，什么都不写 | F > L 起点：本版本 `[F, NULL)`，L 截断于 F；否则 409 `EFFECTIVE_FROM_CONFLICT` |
| P4 | L 有尽头 E（L 已退役，或它的后继被退役成空区间） | 本版本 `[E, NULL)`，补上退役留下的空档 | 本版本 `[F, NULL)`，`[E, F)` 保持无汇率 |

- **从发布时刻起生效，不从报价日起算**：BNM 报价日 D 的草稿最早在 D 日 12:30（吉隆坡）才存在，若从 D 日 0 点生效就是回溯 —— 已按前一个汇率计费的上午事件重算时会换汇率。`observed_at` / `source_quote_date` 只记录报价时刻，供审计与过期判断。
- **对过去生效只有 P1 与 P4（未给 F）**，它们覆盖的时刻此前一律取不到汇率（`FX_RATE_ERROR`、从未扣费）：只把「无汇率」变成「有汇率」，已取到汇率的时刻结果不变。历史汇率缺失时的手工补录就走这两条。
- P3 不自动推后：同一秒里连续两次不给时刻的发布，第二次 409，下一秒重发即可；预约存在时，先撤销预约或指定晚于预约起点的时刻。
- 写入顺序：先截断 L，再把本行从草稿改为已发布。

## 5. 退役：R1–R5

退役在同一把锁内，`t` 同上。被退役的版本记作 V，`reason` 必填。

| 行 | V 的情形 | 做什么 |
| --- | --- | --- |
| R1 | 未截断、已开始（起点为空或 **≤** `t`） | V 改为 `RETIRED`、`effective_to = t`：`t` 起该币种取不到汇率，直到下一次发布按 P4 从 `t` 起补上。起点恰好等于 `t` 时得到空区间 `[t, t)`，不恢复任何版本 |
| R2 | 未截断、尚未开始（起点 **>** `t`：撤销预约） | 先把 V 改为空区间的 `RETIRED`；再找 P = 时间线里起点早于 V 的最后一个版本，**只有 P 是 `PUBLISHED` 且 `P.effective_to` 等于 V 的起点**时恢复为未截断，否则不动 P |
| R3 | `PUBLISHED` 但已截断 | 409 `FX_RATE_NOT_RETIRABLE`：区间已经结束；纠正已计费事件走 Phase 8 reprocess |
| R4 | 草稿 | 409 `FX_RATE_NOT_RETIRABLE` |
| R5 | 已退役 / 已丢弃 | 409 `FX_RATE_FINAL` |

- 恢复 P 只影响 `occurred_at ≥ V 的起点 > t` 的时刻，它们还没有被计费，所以安全。以退役结束的 P 永不恢复（退役的行不可改）。
- 写入顺序：先 V、再 P。
- 发布了错误汇率：退役它，再发布正确的版本（按 P4 从退役时刻起补上）。

## 6. 锁与事务

- **锁在前**：发布与退役的主事务**第一条语句**是 `SELECT … FROM fx_rate_locks WHERE id = 1 FOR UPDATE`；拿到锁之后才取 `t`；之后每一次读都是加锁读（版本行 `FOR UPDATE`，同一币种对的 `PUBLISHED` / `RETIRED` 行 `FOR UPDATE`，响应里的用户 `FOR SHARE`）。原因：MySQL REPEATABLE READ 在第一次普通读时建立快照，锁前的普通读会让锁后的普通读看到等锁之前的旧数据。
- 发布 / 退役的一个事务里依次是：锁 → `t` → 加锁读 → 区间计算 → 写 → **事务内复查** → 审计 → 提交。
- **事务内复查**：写完后加锁重读该币种对全部 `PUBLISHED` / `RETIRED` 行，断言 ① 空区间的行都是 `RETIRED`；② 时间线按起点排序后只有第一个起点可为空，尽头为空的必须是最后一个，相邻两个 `前.effective_to ≤ 后.effective_from`；③ 至多一个尽头为空。不满足就整体回滚（实现错误，500）。
- 建草稿、改草稿、丢弃不拿全局锁（草稿不参与计费），只对版本行 `FOR UPDATE`；与发布对版本行的 `FOR UPDATE` 串行。
- 版本、被截断或恢复的前一个版本与审计同一事务（INV-13）。数据库触发器另外兜底：只能以草稿插入、草稿不能直接退役、新发布的版本没有尽头、区间不重叠、退役必有尽头、发布后只许截断与退役、退役与丢弃后不可改、版本不删除。

## 7. 审计

`entity_type = fx_rate_version`，`entity_id` = 版本的 `public_id`。前后状态里不出现内部 id，人用登录邮箱，时刻是 ISO 8601 UTC，`rate` 是字符串。BNM 拉取的草稿不写审计（拉取记录就是它的来源证明）。

| `action` | 前 | 后 |
| --- | --- | --- |
| `FX_RATE_CREATE` | — | `base_currency`、`rate`、`observed_at`、`source_reference`、`status` |
| `FX_RATE_UPDATE` | 变化了的字段的旧值 | 同一组字段的新值（没有实际变化时不写、不记审计） |
| `FX_RATE_DISCARD` | `status` | `status` |
| `FX_RATE_PUBLISH` | `status`；`truncated_version`：被截断的前一个版本的 `id` 与原 `effective_to`（没有则省略） | `status`、`effective_from`、`effective_to`、`approved_by_email`、`approved_at`；`truncated_version` 的新 `effective_to` |
| `FX_RATE_RETIRE` | `status`、`effective_from`、`effective_to` | `status`、`effective_from`、`effective_to`、`reason`；`restored_version`：被恢复的前一个版本的 `id`（没有则省略） |

## 8. 对下游的契约

- **取汇率**：`resolve_fx_rate(session, base_currency, occurred_at)` 返回覆盖 `occurred_at` 的 `PUBLISHED` / `RETIRED` 版本（`effective_from ≤ occurred_at < effective_to`，`NULL` 端不比），没有就是 `None`。它只读库、绝不调用 BNM；是加锁读（`FOR SHARE`）。
- **计费（T-H）**：在计费事务里 `resolve_model` 之后调用 `lock_fx_rates_shared(session)`（`fx_rate_locks` 的 `FOR SHARE`，持有到计费事务提交），再调用 `resolve_fx_rate`；只处理 `occurred_at ≤` 持锁后当前时间的事件。`fx_rate_version_id` 与 `fx_rate_applied`（`rate` 原值、同精度）写进事件快照。锁顺序：**供应商（S）→ FX（S）→ 定价规则（S）→ 钱包 → 租户**；发布路径各只拿自己那一把锁，不形成环。
- 由此保证：**一个（币种, 时刻）一旦取到某个版本，以后永远取到同一个版本**（除非那一刻原本无汇率）。计费先 → 事件 `occurred_at` < `t` ≤ 任何新边界；发布先 → 计费读到新表。
- **计价（T-G）**：`source_currency = MYR` 不查汇率；否则 `resolve_fx_rate` 返回 `None` → `FX_RATE_ERROR`，不扣费；换算按 §80（高精度中间值、只在最终计费额舍入一次）。
- **前端（T-K）**：汇率页标明「中间价、吉隆坡中午场、从发布时刻起生效」；预约存在时不给时刻发布会 409，界面提示先撤销预约或指定更晚的时刻。

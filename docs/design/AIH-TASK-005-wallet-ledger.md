# AIH-TASK-005 设计：钱包与不可变账本数据层（已批准 v6）

> **来源**：设计闸门 Issue #88。本文件是 `APPROVED: design v6` 那一版正文的**逐字副本**，审查者为独立、只读的 Claude Code 会话（claude-opus-5），因为 Codex 额度不可用，临时替代。
> 放进仓库，是因为 OpenClaw Worker 在沙箱里不联网、读不到 GitHub Issue。
> **实现以本文件为准**；与 Issue 不一致时，以 Issue #88 上被批准的 v6 为准。设计要改，就回到 Issue 升版本、重新过闸门，不要直接改本文件。

---

## 0. 准入判定

本任务档位：`全部`（钱包 / 账本）

---

状态：`READY_FOR_REVIEW`
负责人：Claude
**设计版本**：`v6`
对应需求：`REQ-FIN-002`、`REQ-IDEMP-001`（为其提供数据库兜底）、`REQ-STATUS-001`（计费状态这一维）、`REQ-PRIV-001`；spec §5、§6、§7、§8、§24、§25、§27、§60、§75、§77、§78、§80、§81
目标 PR：待开（实现由 OpenClaw Worker 按登记任务 `AIH-TASK-005` 完成）

## 1. 目标与边界

- **要解决的问题**：Phase 1 的验收链是「建客户 → 自动有钱包 → … → 调账生效 → 全部审计」。`AIH-TASK-004` 建好了 `tenants` / `projects`，但还没有钱包，也没有账本。之后的调账、充值、用量扣费都要落到这一层。本任务只建**数据层**：模型、迁移 `0006`、repository 和测试。做法与 004 相同。
- **可观察的完成标准**：
  1. `wallets` 与 `wallet_transactions` 两张表按 §2 建成；生产迁移后既有租户（目前 0 个）各有一个余额为 0 的钱包。
  2. 余额只能经 `post_transaction` 变动：锁住钱包后追加一行账本，由数据库触发器在同一条语句里把钱包推进到这一行的结果。
  3. 每一行账本都满足 `balance_after = balance_before + amount`，而且这一点由数据库 `CHECK` 强制。
  4. 同一个财务来源 `(reference_type, reference_id)` 至多一行，由唯一约束强制。重放时返回既有行、不重复记账；载荷不一致时报冲突。
  5. 在真实 MySQL 上并发记账不丢更新，最终余额等于流水之和。
  6. `verify_wallet` 能发现余额与账本不一致、序号断档、前后余额断链。
  7. **数据库拒绝**任何对账本行的 UPDATE 或 DELETE，并拒绝任何不对应一行新账本的余额或版本变动（触发器）。
  8. 调账与系统更正必须带非空原因；调账必须带操作者；两者都与一条 `audit_logs` 在同一事务里提交。
  9. **计费状态随余额原子变化**（spec §7.8–§7.10、§24、§25、§27；INV-13）：同一次记账在同一事务里完成以下几件事：
     - 余额 ≤ 0 时 `tenants.billing_status` 为 `SUSPENDED`，> 0 时为 `ACTIVE`，余额正好为 0 仍是 `SUSPENDED`；
     - 只有真正跃迁时，才让 `status_version` +1，并写审计与出站事件 `tenant.billing_status_changed`；
     - 余额从阈值之上跌到阈值及以下时，写出站事件 `tenant.low_balance`（§7.11，只在跨越时发）。
  10. 出站事件以 `PENDING` 持久保存；outbox 恢复任务只重投有处理器的事件类型，所以这两类事件在以后接上投递之前，不会被重试成死信（INV-14）。
- **明确不做**：
  - API、服务层、管理端 UI；
  - 除调账、系统更正与计费状态跃迁之外的审计（那些在本任务里写，见完成标准 8、9）；
  - 充值与支付、用量事件处理、rebill；
  - 管理员控制的 `account_status`（`PENDING_ACTIVATION` / `ENABLED` / `DISABLED` / `CLOSED`）、项目的 `integration_status`、有效状态合成（§24 的 `ALLOW_AI` / `BLOCK_AI`）：另有状态模型任务。本任务只做**由余额驱动**的 `billing_status` 这一维；
  - 出站事件的**实际投递**：状态 webhook（§28）、低余额 / 暂停 / 恢复通知（§47）。本任务只把它们作为持久化意图写进 `domain_outbox`；
  - 「建客户时自动建钱包」的服务编排：本任务只提供 `create_wallet`，由之后的客户管理服务任务在同一个事务里调用；
  - `tenants.currency`：由 `wallets.currency` 承载，不在租户上重复；
  - 数据库账号的权限拆分（迁移账号和运行账号分开）：只有它能挡住 `TRUNCATE` / `DROP`，另立运维任务（§10 残余风险）；
  - 余额不一致的监控告警接线：本任务只提供核对函数。
- **前置条件（本任务之前，单独的基础设施 PR）**：MySQL 开 `log_bin_trust_function_creators=ON`，否则应用账号建不了触发器（§8 有实测证据）。
- **现有行为与证据**：`main` 上还没有任何钱包相关代码。`docs/database-schema.md` 已把 `low_balance_threshold` 与 `currency` 列为「留给钱包任务」。本设计把 `currency` 放到 `wallets` 上，并在 `tenants` 上建 `low_balance_threshold`、`billing_status`、`status_version`（§2），实现时同步更新该文档。

## 2. 设计概要

```text
调用方（未来的服务层，持有事务）
  → post_transaction(tenant_id, type, amount, reference, …)
  → 校验（Decimal、≤8 位小数、非零、类型↔符号↔来源类型）
  → SELECT wallet … FOR UPDATE（按 tenant_id）
  → 在锁内按 (reference_type, reference_id) 查重
      · 已存在且完全一致 → 返回既有行（replayed=True），不写任何东西
      · 已存在但不一致 → LedgerConflictError，不写任何东西
  → INSERT wallet_transactions（balance_before / balance_after / wallet_sequence）
        数据库 BEFORE INSERT：锁钱包，校验租户一致、balance_before = 当前余额、序号 = version + 1
        数据库 AFTER INSERT：把钱包 balance / version 更新为这一行的结果
        （repository **不再**直接 UPDATE wallets；账本插入与钱包变动在同一条语句里一一对应）
  → 来源是 ADMIN_ADJUSTMENT / SYSTEM 时：INSERT audit_logs（WALLET_ADJUSTMENT_POSTED）
  → 计费状态：按新余额求出应有的 billing_status；与现值不同时：
        UPDATE tenants（billing_status、status_version+1）
        INSERT audit_logs（TENANT_BILLING_STATUS_CHANGED，系统操作者）
        INSERT domain_outbox（tenant.billing_status_changed）
  → 低余额：阈值非空，且 旧余额 > 阈值 ≥ 新余额 时 INSERT domain_outbox（tenant.low_balance）
  → flush（不 commit；以上全部在调用方的同一个事务里提交 —— INV-13）
```

租户行的锁：记账时先锁钱包，再以 `SELECT … FOR UPDATE` 锁同一租户的 `tenants` 行，加锁顺序固定为钱包 → 租户，避免死锁。同一租户的状态跃迁因此串行。

**ORM 刷新语义（v5）**：
- 两处加锁读都必须 `.with_for_update().execution_options(populate_existing=True)`。否则会话里已有的 `Wallet` / `Tenant` 对象会被原样返回，锁拿到了，读到的却是旧值。本仓库在 `app/services/password_reset.py` 已经踩过这个坑。
- `balance_before` 与序号取自锁内刚刷新的 `wallets` 行。
- 跃迁时 `status_version` = 锁内刚读到的值 + 1。
- 账本插入并 flush 之后，立即 `session.expire(wallet)`：钱包已被 `AFTER INSERT` 触发器在库里改写，内存对象必然过期。
- `PostResult` 返回的余额以账本行的 `balance_after` 为准。
- 因此同一事务里可以连续多次记账，每一次都从库里的现值出发。

**表 `tenants` 新增列**（spec §75、§24；表里目前 0 行）

| 列 | 类型 | 约束 |
| --- | --- | --- |
| `billing_status` | VARCHAR(64) | 非空，默认 `SUSPENDED`，`CHECK IN ('ACTIVE','SUSPENDED')`。新租户余额为 0，按 §7.10 应为 `SUSPENDED`，充值使余额 > 0 后自动变成 `ACTIVE` |
| `status_version` | BIGINT | 非空，默认 0，`CHECK >= 0`；只增不减（§24：每次有效状态跃迁 +1） |
| `low_balance_threshold` | DECIMAL(20,8) | 可空；NULL 表示不发低余额事件；`CHECK >= 0` |

**计费状态跃迁规则**（纯函数，由 `post_transaction` 在锁内调用）：

| 当前 `billing_status` | 新余额 | 结果 |
| --- | --- | --- |
| `ACTIVE` | > 0 | 不变 |
| `ACTIVE` | ≤ 0 | → `SUSPENDED`，`status_version` +1，写审计与事件 |
| `SUSPENDED` | ≤ 0（含正好为 0） | 不变（§7.10） |
| `SUSPENDED` | > 0 | → `ACTIVE`，`status_version` +1，写审计与事件（§7.9） |

在 `SUSPENDED` 状态下继续扣费不会产生新的跃迁、审计或事件（§7.11）。

**出站事件**（写入现有的 `domain_outbox`，`status = PENDING`，`next_retry_at = now`）：

- `tenant.billing_status_changed`：`aggregate_type = 'tenant'`，`aggregate_id` = 租户 `public_id`。`payload_json` 包含：
  - `billing_status`
  - `status_version`
  - `reason`：`BALANCE_NON_POSITIVE` 或 `BALANCE_POSITIVE`
  - `balance`：字符串形式的 Decimal
  - `wallet_transaction`：触发它的账本行 `public_id`
- `tenant.low_balance`：`aggregate` 同上。`payload_json` 包含：
  - `balance`
  - `threshold`
  - `wallet_transaction`
- payload 里没有联系人等个人数据，也没有成本或毛利。

**outbox 恢复任务**（`app/tasks/outbox.py` 的 `recover`）：查询条件加上 `event_type IN (有渲染器的类型)`。没有处理器的类型因此保持 `PENDING` 持久保存，不会被反复重投、再退避成 `FAILED` 死信。以后的 webhook 或通知任务加上处理器后，就会接手这些积存的事件。`deliver` 的行为不变。

**表 `wallets`**（spec §77）

| 列 | 类型 | 约束 |
| --- | --- | --- |
| `id` | BIGINT | 主键，自增 |
| `tenant_id` | BIGINT | 非空，外键 → `tenants.id` `ON DELETE RESTRICT`，**唯一**（一租户一钱包） |
| `currency` | CHAR(3) | 非空，`CHECK (currency = 'MYR')`（§5） |
| `balance` | DECIMAL(20,8) | 非空，默认 0。**允许为负**（§7.7–§7.8） |
| `version` | BIGINT | 非空，默认 0；每次记账 +1，等于最后一行账本的 `wallet_sequence` |
| `created_at` / `updated_at` | DATETIME | 非空，naive UTC，由调用方传入 |

另加 `UNIQUE (id, tenant_id)`，供账本的复合外键引用。

**表 `wallet_transactions`**（spec §78）

| 列 | 类型 | 约束 |
| --- | --- | --- |
| `id` | BIGINT | 主键，自增 |
| `public_id` | CHAR(36) | 非空，唯一，uuid4 |
| `wallet_id` | BIGINT | 非空 |
| `tenant_id` | BIGINT | 非空；`(wallet_id, tenant_id)` 复合外键 → `wallets(id, tenant_id)`，数据库保证账本行的租户与钱包一致 |
| `wallet_sequence` | BIGINT | 非空，`UNIQUE (wallet_id, wallet_sequence)`，从 1 连续递增（新增列，见 §9） |
| `transaction_type` | VARCHAR(64) | 非空，§8 的 9 种之一（非原生 enum，宽度写死，沿用 auth 表的做法） |
| `amount` | DECIMAL(20,8) | 非空，有符号：贷方为正、借方为负 |
| `balance_before` / `balance_after` | DECIMAL(20,8) | 非空；`CHECK (balance_after = balance_before + amount)` |
| `reference_type` | VARCHAR(32) | 非空，`USAGE_EVENT` / `PAYMENT` / `ADMIN_ADJUSTMENT` / `REBILL` / `SYSTEM` |
| `reference_id` | VARCHAR(64) | 非空；`UNIQUE (reference_type, reference_id)` —— 每个财务来源一次效果 |
| `description` | VARCHAR(255) | 可空 |
| `metadata_json` | JSON | 可空 |
| `created_by` | BIGINT | 可空，外键 → `users.id` `ON DELETE RESTRICT`；NULL 表示系统 |
| `created_at` | DATETIME | 非空 |

索引：`(tenant_id, wallet_sequence)`，供按租户分页查流水。

数据库 `CHECK`：

- 类型与符号一致：
  - 贷方（`TOPUP`、`ADJUSTMENT_CREDIT`、`REBILL_CREDIT`、`BONUS`）要求 `amount > 0`；
  - 借方（`AI_USAGE`、`ADJUSTMENT_DEBIT`、`REBILL_DEBIT`、`REFUND_ADJUSTMENT`）要求 `amount < 0`；
  - `SYSTEM_CORRECTION` 要求 `amount <> 0`。
- 类型与来源类型一致：
  - `AI_USAGE` ↔ `USAGE_EVENT`；
  - `TOPUP` ↔ `PAYMENT`；
  - 调账类与 `BONUS`、`REFUND_ADJUSTMENT` ↔ `ADMIN_ADJUSTMENT`；
  - `REBILL_*` ↔ `REBILL`；
  - `SYSTEM_CORRECTION` ↔ `SYSTEM`。

  `REFUND_ADJUSTMENT` 按 §8 的含义是系统外人工退款后扣回，所以归借方。
- 原因必填（spec §8、§60）：来源是 `ADMIN_ADJUSTMENT` 或 `SYSTEM` 的行，`description` 非空且去掉首尾空白后非空；它就是 §60 的 `reason`。
- 操作者必填（spec §60）：来源是 `ADMIN_ADJUSTMENT` 的行，`created_by` 非空。

**数据库触发器**（迁移 `0006` 用 `op.execute` 建，只针对 MySQL）：

- `wallet_transactions` 上的 `BEFORE INSERT`：以 `SELECT … FOR UPDATE` 锁住 `NEW.wallet_id` 对应的钱包，并要求以下条件同时成立，否则 `SIGNAL`（「ledger row does not extend the wallet」）：
  - 钱包存在，而且 `tenant_id = NEW.tenant_id`；
  - `NEW.balance_before = wallets.balance`；
  - `NEW.wallet_sequence = wallets.version + 1`。

  **账本不可能被孤立插入**：每一行都必须接在钱包当前状态之后。
- `wallet_transactions` 上的 `AFTER INSERT`：`UPDATE wallets SET balance = NEW.balance_after, version = NEW.wallet_sequence, updated_at = NEW.created_at`。插入账本的同一条语句里，钱包就被推进到这一行的结果。
- `wallet_transactions` 上的 `BEFORE UPDATE` 与 `BEFORE DELETE`：一律 `SIGNAL SQLSTATE '45000'`（「append-only」）。账本只能插入。**INV-5 在数据库层面强制。**
- `wallets` 上的 `BEFORE UPDATE`：
  - `tenant_id`、`currency`、`created_at` 变动一律拒绝；
  - `balance` 或 `version` 变动时，必须同时满足：
    - `NEW.version = OLD.version + 1`；
    - 存在一行 `wallet_transactions`，满足 `wallet_id = NEW.id`、`wallet_sequence = NEW.version`、`balance_before = OLD.balance`、`balance_after = NEW.balance`。

    否则拒绝。**INV-4 在数据库层面双向强制**：
    - 只有账本的 `AFTER INSERT` 触发器发出的更新能满足这个条件，因为它对应的那一行刚刚插入；
    - 直接改余额找不到序号为 `version + 1` 的账本行，所以被拒绝；
    - 反过来，账本插入必然带动钱包（上一条）。
  - **实测**（2026-09-19，一次性 `mysql:8.4` 容器，开启前置开关，用应用账号执行；没有触发 1442「表已被调用语句使用」）：
    - 合法插入后，钱包由数据库更新为 100 元、版本 1；
    - `balance_before` 不对、序号跳号、租户不符的插入都返回 45000；
    - 直接改余额、改版本、改账本、删账本都返回 45000；
    - 第二笔 −30.12345678 之后，钱包为 69.87654322、版本 2，链条首尾相接；
    - 两个会话并发插入同一钱包时，后到的会话在触发器的行锁上等待前一个提交，然后因为 `balance_before` 已经过时而被拒绝，最终只有一笔生效。
- `wallets` 上的 `BEFORE INSERT`（v6）：要求 `NEW.balance = 0 AND NEW.version = 0`，否则 `SIGNAL`（「a wallet starts empty」）。新钱包只能从空开始，余额只能经账本出现，也挡住「删空钱包再带余额插回」。迁移第 5 步的回填插入的正是 0，不受影响。
- `wallets` 上的 `BEFORE DELETE`：钱包一旦有账本行，外键 `RESTRICT` 就会拒绝删除。没有账本的钱包允许删除，这是回填和测试清理的需要。

**repository（`app/repositories/wallet.py`）**：同步 `Session`，只 flush、不 commit，沿用 004 的约定。

- `create_wallet(session, *, tenant_id, now) -> Wallet`：余额 0，`version` 0，`currency` 固定 `MYR`。重复创建由唯一约束拒绝。
- `get_wallet_for_tenant(session, tenant_id) -> Wallet | None`
- `post_transaction(session, *, tenant_id, transaction_type, amount, reference_type, reference_id, now, created_by=None, description=None, metadata=None, actor_role=None) -> PostResult(transaction, replayed)`
  - 来源是 `ADMIN_ADJUSTMENT` 或 `SYSTEM` 时：
    - `description`（原因）必须非空；
    - 来源是 `ADMIN_ADJUSTMENT` 时，`created_by` 也必须非空；
    - 首次记账时，在同一次 flush 里插入 `AuditLog`：
      - `action = WALLET_ADJUSTMENT_POSTED`（`AuditAction` 新增的值；列宽已写死为 64，不需要 ALTER）；
      - `actor_user_id = created_by`，`actor_role = actor_role`；
      - `entity_type = 'wallet_transaction'`，`entity_id = public_id`；
      - `before_state` 与 `after_state` 为该行的前后余额；
      - `reason = description`，`created_at = now`。
    - 重放不写审计。
- `list_transactions_for_tenant(session, tenant_id, *, limit, before_sequence=None) -> list[WalletTransaction]`：按 `wallet_sequence` 倒序，用 keyset 分页。`limit` 有上限。
- `verify_wallet(session, tenant_id) -> list[str]`：返回问题码列表，空表示一致。检查以下几点：
  - 余额等于流水之和；
  - 余额等于最后一行的 `balance_after`；
  - `version` 等于最后一行的序号；
  - 序号从 1 连续；
  - 每行的 `balance_before` 等于上一行的 `balance_after`；
  - `tenants.billing_status` 与余额正负一致：余额 > 0 对应 `ACTIVE`，≤ 0 对应 `SUSPENDED`（v5）。

**错误契约**：`LedgerError` 的子类：`InvalidAmount`、`InvalidTransaction`、`WalletNotFound`、`LedgerConflict`。所有错误都在写入之前抛出。错误消息里不带金额以外的业务数据。

**金额精度**：
- 只接受 `Decimal`。`float`、`int`、`str` 一律拒绝。
- 小数位数超过 8 位时拒绝，**不在账本层舍入**。§80 规定的那唯一一次 `ROUND_HALF_UP` 发生在定价层；账本层如果再舍入一次，就成了第二次舍入。
- `NaN` 与 `Infinity` 拒绝。
- 结果超出 `DECIMAL(20,8)` 范围时拒绝。
- 余额相加用 `Decimal` 精确计算。

**时间语义**：`created_at` 是记账时刻，由调用方传入 naive UTC。与业务发生时间（`occurred_at`）的关系由调用方记在 `metadata_json` 或来源表里，本任务不引入财务期间逻辑。

## 3. 不变量影响矩阵

| 不变量 | 是否触碰 | 可能怎样被破坏 | 设计控制 | 验证方式 |
| --- | --- | --- | --- | --- |
| INV-1 中心故障不中断 AI | 否 | — | 不在任何 AI 请求路径上；§7.6 明确 V1 不做同步额度预留。`billing_status` 只有经过以后的有效状态合成和 webhook 传播，才会影响 AI 调用，而且那条路径用的是集成端的本地缓存 | 不适用 |
| INV-2 事件不重复扣费 | 是（打地基） | 同一用量事件记两次 `AI_USAGE` | `UNIQUE (reference_type, reference_id)`，`AI_USAGE` 必须挂 `USAGE_EVENT`；锁内查重，重放不记账 | 单测：同一 `USAGE_EVENT` 记两次，只有一行、余额只变一次；MySQL 并发：同一来源并发记账只有一行 |
| INV-3 支付不重复入账 | 是（打地基） | 同一支付两次 `TOPUP` | 同上，`TOPUP` 必须挂 `PAYMENT` | 同上 |
| INV-4 余额只经账本变动 | 是 | 有代码直接改 `wallets.balance` | **数据库**：`wallets` 的 `BEFORE UPDATE` 触发器要求每次余额或版本变动都对应一行 `wallet_sequence = NEW.version` 且前后余额吻合的账本，否则拒绝。**账本插入必然推进钱包**：账本 `BEFORE INSERT` / `AFTER INSERT` 触发器保证不存在孤立账本；repository 不直接写余额。**核对**：`verify_wallet` | MySQL：直接 `UPDATE wallets SET balance = …` 被拒绝；改 `tenant_id`、`currency` 被拒绝；并发测试最终余额等于流水之和 |
| INV-5 历史账本不可变 | 是 | UPDATE/DELETE 账本行 | **数据库**：`wallet_transactions` 的 `BEFORE UPDATE` 与 `BEFORE DELETE` 触发器一律拒绝。**应用**：模型与 repository 没有任何更新或删除路径。**残余**：`TRUNCATE` / `DROP` 是 DDL，不经触发器（§8 实测），只有迁移会执行 DDL；由 `verify_wallet` 发现（`version` > 0 而账本为空或断档），另立任务拆分账号权限（§10） | MySQL：`UPDATE wallet_transactions …` 与 `DELETE …` 都返回 SQLSTATE 45000；单测：repository 没有 update/delete 函数（反射）；`TRUNCATE` 之后 `verify_wallet` 报错 |
| INV-6 事件保留版本引用 | 否 | — | 版本快照属于 `usage_events`（Phase 2）；账本只引用 `reference_id` | 不适用 |
| INV-7 客户不可见成本毛利 | 是（约束） | 在 `metadata_json` 里写入成本或毛利 | 账本没有成本列；约定 `metadata_json` 只放客户可见的计费上下文。repository 拒绝键名为 `cost`、`provider_cost`、`margin`、`estimated_provider_cost_myr` 的 metadata | 单测：带这些键时拒绝 |
| INV-8 租户不可互访 | 是 | 用 A 租户的上下文读到或写到 B 的钱包 | 所有读写以 `tenant_id` 为入口；账本 `(wallet_id, tenant_id)` 复合外键；列表与核对都按 `tenant_id` 过滤 | 单测：A 的 `tenant_id` 读不到 B 的流水；MySQL：插入 `tenant_id` 与钱包不符的行被外键拒绝 |
| INV-9 对话内容不入库 | 是（约束） | 在 `description` 或 `metadata_json` 写入 prompt / response | 约定这两个字段只放业务标识；拒绝 `prompt`、`response`、`messages`、`content` 键；`description` 限 255 字 | 单测：带这些键时拒绝 |
| INV-10 金额用 Decimal | 是 | float 进入余额计算 | 只接受 `Decimal`；列统一用 `Money`（DECIMAL(20,8)）；不在账本层舍入 | 单测：float、int、str、NaN、Infinity、超过 8 位小数、超范围都被拒绝；`test_model_columns` 确认金额列是 Numeric(20,8) |
| INV-11 event_id 至多一次财务效果 | 是（打地基） | 同 INV-2 | 同 INV-2；「同来源、不同载荷」→ `LedgerConflict`，不写入 | 单测：同 reference 不同金额 → 冲突且无写入 |
| INV-12 定稿对账单不可变 | 否 | — | 对账单属于 Phase 7；账本只追加，为「晚到调整走开放期间」提供前提 | 不适用 |
| INV-13 原子提交 | 是 | 余额跨零而租户状态没变；账本、余额、状态、审计、出站事件分开提交 | 一次调用在同一次 flush 里完成：账本 INSERT、钱包 UPDATE、（跃迁时）租户 UPDATE、审计 INSERT、`domain_outbox` INSERT；**不 commit**，由调用方一次提交 | 单测：跨零记账后调用方 rollback，账本、余额、`billing_status`、`status_version`、审计、outbox 全部不留痕；commit 后全部都在，而且彼此一致 |
| INV-14 队列丢失不毁持久工作 | 是 | 状态变更或低余额事件只放在队列里，队列丢了就没了；或者没有处理器的事件被重试成死信 | 事件与状态在同一事务写进 `domain_outbox`（持久化），不依赖 Redis 或 Celery；`recover` 只重投有处理器的类型，其余保持 `PENDING` 等待以后的处理器 | 单测：两类新事件在 `recover` 之后仍是 `PENDING`、`attempt_count` 为 0；既有的密码重置事件照常被重投 |

## 4. 状态与并发

状态机只有由余额驱动的 `billing_status` 这一维，完整跃迁表见 §2「计费状态跃迁规则」：

| 当前状态 | 事件 | 前置条件 | 新状态 | 副作用 | 非法时结果 |
| --- | --- | --- | --- | --- | --- |
| `ACTIVE` | 记账 | 新余额 ≤ 0 | `SUSPENDED` | `status_version` +1、审计、`tenant.billing_status_changed` | 不适用（由余额决定） |
| `SUSPENDED` | 记账 | 新余额 > 0 | `ACTIVE` | 同上 | 不适用 |
| 任意 | 记账 | 应有状态等于现有状态 | 不变 | 无（低余额事件另判） | — |

`billing_status` 与 `status_version` 只能经 `post_transaction` 改变。本任务不提供手工改计费状态的入口：spec §24 说计费状态只随余额变化，管理员控制的是 `account_status`，那属于另一个任务。

- 加锁顺序固定为钱包 → 租户，同一租户的记账与跃迁因此串行，`status_version` 不会被并发加两次或丢失。

**并发**：
- `post_transaction` 先 `SELECT … FROM wallets WHERE tenant_id = ? FOR UPDATE`，同一钱包的记账因此串行。锁持有到调用方提交或回滚为止。
- 纵深防御：
  - `UNIQUE (wallet_id, wallet_sequence)`：即使锁被绕过，两行也不可能占用同一序号，丢更新会变成唯一冲突，而不是悄悄覆盖；
  - `CHECK (balance_after = balance_before + amount)`：数据库拒绝算式不成立的行；
  - `wallets` 的触发器：余额变动必须对应刚插入的那一行账本，所以不存在「只改了余额」的中间态可以提交。

**数据库负责的唯一性**：
- `wallets.tenant_id`
- `wallet_transactions.public_id`
- `(reference_type, reference_id)`
- `(wallet_id, wallet_sequence)`

**幂等键**：`(reference_type, reference_id)`。
- 用量事件用 `event_id`，支付用网关事件或支付的内部 ID。
- 管理端调账用调用方生成的请求 ID（uuid），防止双击重复调账。

**相同 ID、不同载荷**：`wallet_id`、`transaction_type`、`amount` 任一不同就报 `LedgerConflict`，不写入。三者都相同则是重放，返回既有行，`replayed=True`。

**重试层**：repository 不重试。锁等待超时和死锁以 `OperationalError` 抛给调用方，由调用方回滚后决定是否重试。重试是安全的，因为幂等键保证只有一次效果。

**必须原子提交的**：账本 INSERT 与钱包 UPDATE 在同一个事务里；未来的审计与 outbox 也放进这个事务（INV-13）。

## 5. 失败与恢复矩阵

| 失败点 | 对外结果 | 数据最终状态 | 是否可重试 | 恢复来源 | 告警 |
| --- | --- | --- | --- | --- | --- |
| 校验失败（金额、类型、来源、metadata、调账缺原因或操作者） | `InvalidAmount` / `InvalidTransaction` | 无任何写入 | 否，调用方修正输入 | — | 不需要 |
| 绕过 repository 直接改余额或账本 | 数据库 SQLSTATE 45000 | 语句被拒绝，数据不变 | 否 | — | 由之后的服务层记录错误 |
| 迁移前置条件缺失（`log_bin_trust_function_creators` 未开） | 迁移在**建任何表之前**失败，报明确原因 | 无 DDL 执行，`alembic_version` 仍是 0005 | 开启前置条件后可以 | 重新部署 | 部署失败本身会报出（CD 红） |
| 钱包不存在 | `WalletNotFound` | 无写入 | 建钱包后可以 | — | 不需要 |
| 同来源不同载荷 | `LedgerConflict` | 无写入，既有行不变 | 否 | 人工核查来源 | 由之后的服务层记录 |
| 同来源重放 | 返回既有行，`replayed=True` | 不变 | 是（天然幂等） | — | 不需要 |
| 锁等待超时或死锁 | `OperationalError` | 调用方回滚，无写入 | 是 | 调用方重试 | 频繁出现时由 §95 监控（之后接线） |
| DB 提交失败 / 进程在提交前崩溃 | 异常 | InnoDB 回滚，账本与余额都不留痕 | 是 | 重试，幂等键保证一次效果 | — |
| 唯一约束兜底触发（锁被绕过的并发插入） | `IntegrityError` | 后到的事务回滚 | 是 | 重试会走重放或冲突分支 | — |
| Redis / Celery 丢失 | 不影响记账 | 状态变更与低余额事件已在同一事务写进 `domain_outbox`，队列丢了也不会丢 | — | 以后的处理器从 `PENDING` 行接手 | — |
| 会话里已有旧的 `Wallet` / `Tenant` 对象（之前读过，或同一事务里上一笔记账之后） | 不适用：加锁读用 `populate_existing=True` 刷新，记账后使 `Wallet` 过期 | 每次都从库里的现值出发；跃迁判定与序号都正确 | — | — | — |
| 跃迁途中失败（写租户、审计或 outbox 时出错） | 异常 | 整个事务回滚：账本、余额、状态、审计、事件全部不留痕 | 是 | 调用方重试，幂等键保证只有一次效果 | — |
| 外部服务超时 | 不涉及（无外部调用） | — | — | — | — |
| 余额与账本不一致（绕过 repository 的写入） | `verify_wallet` 返回问题码 | 数据保持原样，不自动修复 | — | 人工核查，用 `SYSTEM_CORRECTION` 记补偿行 | 告警接线另立任务（§1 不做项） |

## 6. 数据与安全边界

- **租户过滤**：repository 的所有入口都要求 `tenant_id`。账本 `tenant_id` 由数据库复合外键保证与钱包一致。`tenant_id` 必须来自已认证的上下文；本任务没有 API，这一点由之后的服务层保证。
- **鉴权主体**：不适用，本任务没有入口。`created_by` 记录操作者的用户 ID，由调用方传入。
- **客户侧禁止返回的字段**：账本不含成本或毛利。`created_by` 与 `metadata_json` 之后是否对客户展示，由客户门户任务决定；本任务不定义客户可见的序列化。
- **日志、审计、异常**：
  - repository 不写日志。异常消息只含问题码与金额，不含 `description` 或 `metadata_json`。
  - 调账与系统更正在同一事务里写 `audit_logs`（spec §8、§60），内容是：
    - 操作者（`created_by`、`actor_role`）；
    - 前后余额；
    - 原因；
    - 账本行的 `public_id`。

    不写 `metadata_json`。
  - `ip_address` 与 `user_agent` 由之后的服务层传入，本任务不接 HTTP，保持为空。
- **密钥**：不涉及。
- **prompt / response 与客户数据**：不处理。`metadata_json` 的禁用键见 INV-9。
- **数据保留**：账本属于财务记录，永久保留（§112）。`ON DELETE RESTRICT` 阻止因删除租户、钱包或用户而级联删除账本。

## 7. 测试证据计划

⚠️ **测试层级规则（v4）**：钱包余额和版本由 MySQL 触发器推进，SQLite 没有这些触发器。所以：
- 凡是调用 `post_transaction`、`create_wallet`、`verify_wallet` 的场景，一律在真 MySQL 上跑（`BILLING_TEST_DATABASE_URL`；CI 必跑，把任何 skipped 判失败）。下表里这类场景写成「unit」的，按 MySQL 执行。
- SQLite 或纯 Python 只测纯函数：金额校验、类型↔符号↔来源映射、计费状态跃迁函数、低余额跨越判定、metadata 禁用键。
- Worker 跑不了 MySQL 测试，准入以 CI 为准。

| 风险 / 需求 | 测试层级 | 场景 | 预期结果 |
| --- | --- | --- | --- |
| 正常路径 | MySQL | 建钱包；记 `TOPUP` +100、`AI_USAGE` −0.12345678、`ADJUSTMENT_DEBIT` −50 | 余额 = 49.87654322；三行序号 1..3；每行 `before`/`after` 首尾相接；`version` = 3；`verify_wallet` 为空 |
| 负余额 | unit | 余额 10 时记 `AI_USAGE` −15 | 允许，余额 −5（§7.7） |
| 边界值 | unit | `Decimal('0.00000001')`、`Decimal('-0.00000001')`、9 位小数、0、NaN、Infinity、float、int、str、超出 DECIMAL(20,8) | 前两个成功，其余都在写入前被拒绝 |
| 类型、符号、来源一致 | unit + MySQL | `TOPUP` 负数；`AI_USAGE` 正数；`AI_USAGE` 挂 `PAYMENT`；`REFUND_ADJUSTMENT` 正数 | repository 拒绝；MySQL 上直接插入同样的行被 `CHECK` 拒绝 |
| 算式 CHECK | MySQL | 直接插入 `balance_after ≠ balance_before + amount` 的行 | 数据库拒绝 |
| 币种 | MySQL | 直接插入 `currency = 'USD'` 的钱包 | 数据库拒绝 |
| 并发 | MySQL（`BILLING_TEST_DATABASE_URL`，仅 CI） | 20 个线程、各自独立会话，对同一钱包各记 10 笔不同来源的借贷 | 最终余额 = 全部金额之和；序号 1..200 连续；链条首尾相接；`verify_wallet` 为空 |
| 幂等重放 | unit + MySQL | 同一 `(USAGE_EVENT, e1)` 顺序记两次；MySQL 上 8 个线程并发记同一来源 | 只有一行；余额只变一次；其余调用 `replayed=True`，或在唯一约束兜底后重试走重放分支 |
| 相同 ID、不同载荷 | unit | 同来源，金额不同或类型不同 | `LedgerConflict`；无新行；余额不变 |
| 事务中途失败 | unit + MySQL | `post_transaction` 之后调用方 rollback；MySQL 上 flush 之后抛异常 | 账本与余额都不留痕 |
| Redis / Celery 丢失 | 不适用 | 本任务不涉及队列 | — |
| 租户越权 | unit + MySQL | 用 A 的 `tenant_id` 列 B 的流水、核对 B 的钱包；MySQL 插入 `tenant_id` 与钱包不符的账本行 | 读不到；复合外键拒绝 |
| 金额精度 | unit | 1,000 笔 `Decimal('0.00000001')` 的借贷后核对 | 余额精确等于流水之和，无浮点误差；金额列是 Numeric(20,8)（`test_model_columns`） |
| 篡改检测 | unit | 直接改余额；直接改某行金额；删掉一行（SQLite 没有这些触发器，所以能直接改） | `verify_wallet` 分别报对应的问题码 |
| 数据库拒绝改账本（INV-5） | MySQL | 对一行已提交的账本执行 `UPDATE … SET amount`、`UPDATE … SET description`、`DELETE` | 都返回 SQLSTATE 45000；数据不变 |
| 数据库拒绝孤立账本（INV-4 另一方向） | MySQL | 直接 INSERT 一行其余约束都满足、但 `balance_before` ≠ 当前余额 / 序号 ≠ `version + 1` / `tenant_id` 与钱包不符的账本；再 INSERT 一行正确接续的账本，但不经 repository | 前三种返回 SQLSTATE 45000、无任何写入；第四种由触发器把钱包推进到这一行的结果，余额与账本仍然一致，`verify_wallet` 为空。之后经 repository 用同一来源记账，正常走重放或冲突分支，不会出现「占住幂等键却不影响余额」 |
| 并发插入同一钱包 | MySQL | 两个会话用同一个 `balance_before` 与序号插入 | 后到的在触发器行锁上等待；前一个提交后，后到的被 45000 拒绝；只有一笔生效 |
| 数据库拒绝绕过账本改余额（INV-4） | MySQL | 直接执行 `UPDATE wallets SET balance = balance + 1`；`UPDATE wallets SET version = version + 1`；`SET tenant_id` / `SET currency` | 都被拒绝；经 repository 正常记账仍然成功 |
| `TRUNCATE` 残余风险 | MySQL | 记几笔之后 `TRUNCATE wallet_transactions`（它是子表，外键不阻止清空子表） | `verify_wallet` 报「余额与账本不一致 / 账本缺失」 |
| 调账原因与操作者 | unit + MySQL | `ADJUSTMENT_DEBIT` 不带原因；原因全是空白；不带 `created_by`；`SYSTEM_CORRECTION` 不带原因；MySQL 上直接插入这样的行 | repository 拒绝；数据库 `CHECK` 拒绝 |
| 调账审计原子性 | unit + MySQL | 记一笔调账，然后调用方 rollback；另一笔 commit | rollback 后账本、余额、审计都没有；commit 后正好一条 `WALLET_ADJUSTMENT_POSTED` 审计，字段与账本一致；重放不增加审计 |
| 跨零暂停 | unit | 余额 10、`ACTIVE`，记 `AI_USAGE` −15 | 余额 −5；`SUSPENDED`；`status_version` +1；一条 `TENANT_BILLING_STATUS_CHANGED` 审计；一条 `tenant.billing_status_changed` 事件，payload 为 `SUSPENDED`、新版本、`BALANCE_NON_POSITIVE` |
| 正好为 0 | unit | 余额 10、`ACTIVE`，记 −10；再记 +0.00000001 | 第一笔后 `SUSPENDED`（0 仍暂停）；第二笔后 `ACTIVE`，各一次跃迁 |
| 恢复 | unit | `SUSPENDED`、余额 −5，记 `TOPUP` +3，再记 +10 | +3 后余额 −2，仍 `SUSPENDED`、无新事件；+10 后 `ACTIVE`、`status_version` +1、一条事件 |
| 暂停中继续扣费 | unit | `SUSPENDED` 时连续记 3 笔 `AI_USAGE` | 无新跃迁、审计或事件；`status_version` 不变 |
| 新租户默认状态 | unit + MySQL | 建租户与钱包 | `billing_status = SUSPENDED`、`status_version = 0`、余额 0 |
| 低余额跨越 | unit | 阈值 20：余额 50 → 25（不跨）→ 15（跨）→ 10（已在阈值下）→ 30 → 18（再次跨） | 恰好两条 `tenant.low_balance`，分别对应 15 和 18 那两笔；阈值为 NULL 时一条都没有 |
| 状态原子性 | unit + MySQL | 跨零记账后调用方 rollback；MySQL 上 flush 之后、commit 之前抛异常 | 账本、余额、状态、版本、审计、outbox 全部回到记账前 |
| outbox 不成死信 | unit | 写一条 `tenant.billing_status_changed` 和一条既有的密码重置事件，都到期，然后运行 `recover` | 只重投密码重置那一条；新事件仍是 `PENDING`、`attempt_count = 0` |
| 状态版本并发 | MySQL | 多线程交替记借贷，让余额反复跨零 | 最终 `status_version` 等于实际跃迁次数（按账本序号重算）；没有重复或丢失 |
| 会话里有旧租户对象 | MySQL | 会话 A 先 `get_tenant_by_public_id`（这时 `ACTIVE`、余额 3）；会话 B 记 −8 并提交（`SUSPENDED`、版本 +1）；会话 A 再记 `TOPUP` +10 并提交 | A 的记账把状态跃迁回 `ACTIVE`，`status_version` 在 B 的基础上 +1（没有重复版本号），写审计与复通事件；最终余额 5、`ACTIVE`，`verify_wallet` 为空 |
| 同一事务连续记账 | MySQL | 同一个 session、同一个事务里连记 3 笔（跨一次零线），最后一次提交 | 三笔都成功，序号 1..3，余额与状态正确，只有一次跃迁；没有 45000 |
| 状态与余额一致性核对 | MySQL | 用测试专用连接把 `billing_status` 改成与余额不符 | `verify_wallet` 报状态不一致 |
| 钱包只能从空开始（INV-4） | MySQL | 直接 `INSERT INTO wallets` 时带 `balance = 500` 或 `version = 3`；删掉一个空钱包后再带余额插回 | 都返回 SQLSTATE 45000；余额为 0、版本为 0 的插入（`create_wallet` 与回填）照常成功 |
| 跨租户复用同一来源（INV-8、INV-11） | MySQL | 租户 A 先记 `(USAGE_EVENT, e1)` −5；租户 B 再用同一个 `(USAGE_EVENT, e1)` 记 −5 | B 得到 `LedgerConflict`（既有行的 `wallet_id` 不同），不写入任何行；A、B 余额都不变；B 拿不到 A 的账本行 |
| 迁移前置条件 | MySQL | 在 `log_bin_trust_function_creators = OFF` 且非 SUPER 的连接上执行 `upgrade` | 迁移在建表之前失败并给出明确原因。CI 用 root 连接，拿不到这个条件，所以这一项以前置条件的代码审查加 §8 的一次性实测为证据，CI 只验证开关开着时迁移成功 |
| 不可变 | unit | 反射检查 repository 的公开函数 | 没有 update 或 delete 账本的函数 |
| 迁移 | MySQL | `upgrade head`、`downgrade` 到 0005；迁移前已有租户时回填钱包 | 表、约束、索引与模型一致；每个既有租户正好一个余额为 0 的钱包；降级后两表消失，其余表不变 |

## 8. 迁移与上线

- **前置条件（单独的基础设施 PR，先合并部署）**：`docker-compose.yml` 的 mysql `command` 加 `--log-bin-trust-function-creators=ON`，并加一条 `test_compose` 守卫。
  - **实测证据**（2026-09-19，一次性 `mysql:8.4` 容器，配置与生产 compose 相同：binlog 开、ROW、应用账号由 `MYSQL_USER` 创建并拥有库级权限）：
    - 开关关着时，应用账号 `CREATE TRIGGER` 报 `ERROR 1419 … You do not have the SUPER privilege and binary logging is enabled`；
    - 开关打开后，应用账号可以建触发器；`UPDATE` 与 `DELETE` 返回 `ERROR 1644 (45000)`；`INSERT` 照常；
    - `mysqldump` 会导出触发器，所以恢复出来的库仍然受强制；
    - `TRUNCATE` 能绕过触发器（残余风险，见 §3 INV-5 与 §10）。
  - **为什么风险可以接受**：这个开关限制的是「按语句记录 binlog」时不确定的存储程序。本库的 binlog 是 ROW 格式，触发器也只做 `SIGNAL`。
  - **代价**：改 mysql 的 `command` 会让那次部署重建 mysql 容器，数据卷不变，有几秒停机，要在那个 PR 正文里写明（`docs/deployment.md` §9.4 的约定）。
- **数据迁移步骤**：`0006_wallets_ledger`
  0. **预检**：在 MySQL 上查 `@@log_bin` 与 `@@log_bin_trust_function_creators`。binlog 开着而开关关着时，立即抛出明确的错误，**不执行任何 DDL**。
  1. `create_table wallets`；
  2. `create_table wallet_transactions`；
  3. 建 6 个触发器：账本的 `BEFORE INSERT`、`AFTER INSERT`、`BEFORE UPDATE`、`BEFORE DELETE`，以及钱包的 `BEFORE INSERT`、`BEFORE UPDATE`；
  4. `ALTER TABLE tenants` 加 `billing_status`（默认 `SUSPENDED`）、`status_version`（默认 0）、`low_balance_threshold`（可空），以及对应的 `CHECK`。`tenants` 目前 0 行，而且 MySQL 8.4 带默认值的加列是即时操作；
  5. 用 `INSERT INTO wallets (…) SELECT … FROM tenants` 给每个既有租户补一个余额为 0 的钱包。这是插入，不触发 `wallets` 的 UPDATE 触发器。生产上目前有 0 个租户，这一步是为了让「每个租户都有钱包」对任何库都成立。既有租户的状态由第 4 步的默认值设为 `SUSPENDED`，与余额 0 一致。
- **锁表与性能**：两张新表的 DDL 瞬时完成。对既有表只 ALTER `tenants`（加三列）：它目前 0 行，而且 MySQL 8.4 带默认值的加列是即时操作。回填只读 `tenants`（0 行）。不在有数据的表上持有长时间的元数据锁，不需要停机。
- **兼容窗口**：旧代码不知道这两张表，也不会读写它们。新代码在本任务里没有任何调用方。
- **部署顺序**：合并即部署，`deploy.sh` 先迁移、再换镜像，不另设批准。
- **回滚与前滚**：`deploy.sh` 不回滚迁移。回滚镜像后旧代码不受新表影响。`downgrade` 按外键反向删表。⚠️ 一旦有任何真实记账，**生产上不要 downgrade 这一版**，它会连同全部账本一起删掉。
- **部分失败**：MySQL 的 DDL 不参与事务。
  - 如果第 1 步建好、第 2 步失败：`wallets` 留在库里，`alembic_version` 不前进，重跑会撞「表已存在」。
  - 处置：确认 `alembic_version` 仍是 `0005_tenants_projects`、`wallets` 为空，然后 `DROP TABLE wallets` 再重新部署。
  - 如果第 3、4、5 步失败：两张新表都已存在（触发器可能建了一部分，`tenants` 可能加了一部分列），同样确认为空后按反向顺序删表，再删掉 `tenants` 上已加的列，然后重新部署。删表会连带删掉该表上的触发器。
  - 迁移里只有第 5 步写数据，而且它可以重做（源表为空，或删表后重跑）。
  - `downgrade`：先删 `tenants` 的三列，再按外键反向删两张表。
  - 最常见的失败原因是缺前置条件，已由第 0 步的预检在 DDL 之前挡住。
- **监控**：账本核对告警另立任务（§1 不做项），负责人 Kelvin。

## 9. 方案取舍

| 方案 | 优点 | 风险 | 未采用原因 |
| --- | --- | --- | --- |
| **采用**：`SELECT … FOR UPDATE` + 账本 `wallet_sequence` 唯一 + 算式 `CHECK` + **触发器**（账本禁止 UPDATE/DELETE；钱包余额变动必须对应新账本行）+ `verify_wallet` | spec §81 的推荐做法；INV-4 与 INV-5 在数据库层面强制；丢更新会变成唯一冲突 | 需要 MySQL 开 `log_bin_trust_function_creators`（前置 PR，有实测证据）；`TRUNCATE` / `DROP` 不经触发器 | — |
| 只做应用层控制加 `verify_wallet`，数据库强制后移（v1 的方案） | 不需要改 MySQL 配置 | 应用账号能直接 UPDATE/DELETE 历史账本，只能事后发现（Codex 在 v1 判为阻断） | 不满足 INV-5 与 spec §8、§78 的不可变要求 |
| 只保留「钱包变动必须有账本」单向触发器，repository 自己更新钱包（v3 的方案） | 各层职责直观 | 运行账号可以插入孤立账本：余额不变，却永久占住幂等键，真实交易会被错判为重放（Codex 在 v3 判为阻断） | 改成由账本插入触发器推进钱包，双向一一对应 |
| 只授权存储过程写账本（Codex 在 v3 提出的例子） | 数据库层面的唯一写入口 | 要收回运行账号对账本表的 INSERT，必须先拆分库级授权（同下一行）；存储过程还会把业务逻辑挪进 SQL | 触发器在不拆分权限的前提下达到同样的一一对应（已实测）；权限拆分留给运维任务 |
| 收回应用账号对账本表的 UPDATE/DELETE 权限 | 还能挡住 `TRUNCATE` / `DROP` | 应用账号现在是库级授权（compose 的 `MYSQL_USER`），MySQL 不能从库级授权里单独收回某张表的权限；要先拆分迁移账号和运行账号，改动涉及部署、密钥和恢复流程 | 触发器已经覆盖 DML 路径；只剩 DDL 这一块需要它，另立运维任务（§10） |
| 乐观锁（只比较 `version`，不加行锁） | 不阻塞读 | 高并发下大量重试；spec §81 推荐行锁 | 选择与 spec 一致的悲观锁；`version` 仍然保留，用于核对 |
| 不加 `wallet_sequence`，只靠时间或 `id` 排序 | 少一列 | `id` 自增在并发插入时不保证等于提交顺序；链条核对和丢更新检测都没有数据库层面的依据 | 多一列换来数据库层面的顺序与丢更新检测，值得 |
| 计费状态跃迁放到以后的状态模型任务（v2 的方案） | 本任务更小 | 在两个任务之间，余额可以跨零而租户状态不变，违反 spec §7.8–§7.10 与 INV-13（Codex 在 v2 判为阻断） | 不满足原子性。只把**由余额驱动**的那一维并进来，管理员控制的 `account_status` 与有效状态合成仍然分开 |
| 事件直接写进 outbox，不改 `recover` | 不碰既有代码 | 没有处理器的事件会被 `recover` 反复重投，退避后成为 `FAILED` 死信，还不断打错误日志 | 给 `recover` 加一个按处理器类型过滤的条件，是最小的改动，而且让事件持久等待 |
| R3：把 AI 用量按时间窗聚合成一行账本 | 账本行数少 | §9 规定计费的事实来源是请求级；聚合会让「一个事件一次效果」的唯一约束失去对象，也让晚到事件的处理更复杂 | 先不聚合，每个 `USAGE_EVENT` 一行（Kelvin 2026-09-19 同意默认）。以后要聚合，可以新增 `reference_type = USAGE_WINDOW`，不需要改表结构；性能验证放到 Phase 2 的 §119 压测 |
| 在账本层把金额舍入到 8 位 | 调用方省事 | 与 §80「只舍入一次」冲突：定价层已经舍入过，账本层再舍入就是第二次 | 账本层对超过 8 位的金额直接拒绝，不舍入 |

## 10. 未决问题与假设

- **未决问题**：无阻断项。以下是明确后移的事项，不影响本任务的正确性：
  1. **残余风险**：`TRUNCATE` / `DROP` 这类 DDL 不经触发器（§8 实测）。应用代码里没有任何 DDL；DDL 只在迁移里出现，迁移要经过 PR 审查；清空账本后 `verify_wallet` 能发现（`version` > 0 而账本为空）；binlog 支持时间点恢复。彻底封住它需要拆分迁移账号和运行账号，另立运维任务。
  2. `tenants.currency` 由 `wallets.currency` 承载，不在租户上重复；设置 `low_balance_threshold` 的管理入口（§56）归客户管理服务任务。实现时同步更新 `docs/database-schema.md`「尚未建的列」。
  4. `account_status` / `integration_status` 与有效状态合成（§24）归状态模型任务。那个任务要保证：有效状态的每次跃迁都让 `status_version` +1，与本任务的计费跃迁共用同一个版本号，而且都只增不减。
  3. 余额不一致的告警接线：另立任务。
- **尚未验证的假设**：
  - MySQL 8.4 会强制本设计的 `CHECK`，包括含 `IN` 的组合条件。8.0.16 起支持，CI 的迁移测试会实际插入违反约束的行来验证。
  - 生产的 `innodb_lock_wait_timeout` 是默认的 50 秒。本任务不依赖具体取值。
- **需要谁拍板**：R3 已由 Kelvin 同意默认（不聚合）。开启 `log_bin_trust_function_creators` 的前置 PR 在 Kelvin 已批准的范围内（「修复、审查、合并、部署一并做」）。
- **如果假设错误**：如果 MySQL 不强制某条 `CHECK`，CI 的迁移测试会失败，实现无法合并，不会带着虚假的保证上线。

## 11. 审查与版本绑定

审查方要回答的五个问题常驻在 [scripts/review_checklist.md](../../scripts/review_checklist.md) 的「设计审查」一节，此处不重复。

**版本绑定规则**（出处是 [WORKFLOW §3](../../docs/WORKFLOW.md)）：

- Codex 的批准必须写成 `APPROVED: design v<N>`，`<N>` 取本 Issue 顶部的「设计版本」
- **设计版本一变，之前的 APPROVE 自动作废**，必须重新过闸门
- 实质修改：改了契约、表结构、事务边界、状态机、失败语义、不变量控制。改错别字不算
- 实质修改时：把顶部版本 +1，在下方追加一条变更说明，状态退回 `READY_FOR_REVIEW`

### 设计闸门判定

- [x] 需求、非目标和验收标准明确
- [x] 关键契约与事务边界明确
- [x] 触碰的不变量都有控制措施
- [x] 失败路径都有确定的最终状态
- [x] 高风险控制都有测试场景
- [x] 没有未解决的阻断假设
- [x] 审批绑定到明确的设计版本

## 12. 版本变更记录

| 版本 | 日期 | 改了什么 | 触发原因 |
| --- | --- | --- | --- |
| v1 | 2026-09-19 | 初稿 | — |
| v2 | 2026-09-19 | ① 数据库层面强制：账本 `BEFORE UPDATE` / `BEFORE DELETE` 触发器拒绝一切修改（INV-5）；钱包 `BEFORE UPDATE` 触发器要求余额或版本变动对应一行新账本、禁止改 `tenant_id` 与 `currency`（INV-4）。② 新增前置条件：MySQL 开 `log_bin_trust_function_creators`，附一次性容器的实测证据；迁移加预检，在任何 DDL 之前失败。③ 调账与系统更正：原因非空（`CHECK`），调账操作者非空（`CHECK`），与 `audit_logs`（新增 `WALLET_ADJUSTMENT_POSTED`）同一事务写入；重放不写审计。④ 补对应测试；把 `TRUNCATE` / `DROP` 记为残余风险 | Codex 设计审查 v1 的两个阻断项：账本不可变没有数据库强制；人工退款缺原因与审计契约（spec §8） |
| v3 | 2026-09-19 | ① `tenants` 加 `billing_status`（默认 `SUSPENDED`）、`status_version`、`low_balance_threshold`。② `post_transaction` 在同一事务里按新余额做计费状态跃迁（≤ 0 暂停，> 0 恢复，正好为 0 仍暂停），跃迁时 `status_version` +1，写 `TENANT_BILLING_STATUS_CHANGED` 审计与 `tenant.billing_status_changed` 事件。③ 低余额向下跨越阈值时写 `tenant.low_balance` 事件。④ 加锁顺序固定为钱包 → 租户。⑤ `recover` 只重投有处理器的事件类型，新事件持久等待、不会成为死信。⑥ 补跨零、正好为 0、恢复、暂停中扣费、低余额、原子性、outbox、版本并发的测试 | Codex 设计审查 v2 的阻断项：余额跨零的状态跃迁没有和钱包变动原子提交（spec §7.8–§7.10、§25、§27；INV-13） |
| v4 | 2026-09-19 | ① 账本的 `BEFORE INSERT` 触发器锁住钱包，并校验租户、`balance_before` 与序号；`AFTER INSERT` 触发器把钱包推进到这一行的结果；repository 不再直接 UPDATE 钱包。账本插入与钱包变动因此一一对应，没有孤立账本（附一次性容器实测）。② 测试层级：记账行为一律在 MySQL（CI）上跑，SQLite 只测纯函数。③ 补孤立插入与并发插入测试；取舍表补「只授权存储过程」一项 | Codex 设计审查 v3 的阻断项：可以插入合法但孤立的账本行，占住幂等键而余额不变 |
| v5 | 2026-09-19 | ① 加锁读 `populate_existing=True`，记账后使 `Wallet` 过期，`status_version` 取锁内现值 +1，返回余额以账本行为准，所以同一事务可以连续记账。② `verify_wallet` 核对 `billing_status` 与余额正负。③ 补「会话里有旧租户对象」「同一事务连续记账」「状态一致性」三条 MySQL 测试。④ 改正 §7「正常路径」的测试层级和 §8 关于 ALTER 的表述 | 设计审查 v4（Claude Code 审查者，claude-opus-5）的阻断项：触发器改写钱包后，ORM 的旧对象会导致状态误判和同一事务内二次记账失败 |
| v6 | 2026-09-19 | ① 钱包 `BEFORE INSERT` 触发器：新钱包必须余额 0、版本 0，余额不能凭空出现。② 补「钱包只能从空开始」「跨租户复用同一来源」两条 MySQL 测试 | 设计审查 v5（Claude Code 审查者，claude-opus-5；那次输出因开场白未通过格式校验、没有发布，内容照样采纳）的两个阻断项：钱包 INSERT 路径没有约束；跨租户同一来源的冲突分支没有测试 |

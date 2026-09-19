# 数据库表结构

> spec §136 要求维护的文档之一，Phase 1 起按表逐步补。
> 本文件记**已裁决的表结构**与裁决理由；字段语义以 spec 为准（§74–§79），冲突时以 spec 为准并走勘误。
> 最后更新：2026-09-19

---

## 通用约定

沿用 `app/models/auth.py` 已有的写法，新表不另立：

- **主键 / 外键**：`BigInteger().with_variant(Integer, "sqlite")`。生产 MySQL 是 `BIGINT`；
  单元测试跑内存 SQLite，只有 `INTEGER PRIMARY KEY` 会自增
- **时间戳**：`DATETIME`，存不带时区的 UTC（spec §109），由调用方传入
- **对外标识 `public_id`**：`CHAR(36)`、唯一，值为 `uuid4` 字符串。内部自增 `id` 不出库；
  对外（URL、API、页面）只用 `public_id`，满足 spec §115「猜不到别人的 tenant ID」。
  用 `uuid4` 而不是 uuidv7：Python 3.12 标准库没有 uuid7，这两张表行数小，用不上时间有序带来的索引局部性
- **金额**：一律 `DECIMAL(20,8)`（`Money`，`app/models/base.py`；spec §80），永远不用浮点。
  金额列：`tenants.low_balance_threshold`、`wallets.balance`、`wallet_transactions` 的
  `amount` / `balance_before` / `balance_after`
- **枚举列**：非原生 enum，`VARCHAR` 宽度写死（理由见 `app/models/auth.py` 的 `_ENUM_LENGTH`）

---

## `tenants`（spec §75）

| 列 | 类型 | 约束 |
| --- | --- | --- |
| `id` | BIGINT | 主键，自增 |
| `public_id` | CHAR(36) | 非空，唯一 |
| `company_name` | VARCHAR(255) | 非空 |
| `contact_name` | VARCHAR(255) | 可空 |
| `email` | VARCHAR(320) | 非空，**不唯一**（联系邮箱不是登录账号；同一联系人可对应多家公司） |
| `phone` | VARCHAR(32) | 可空 |
| `billing_status` | VARCHAR(64) | 非空，默认 `SUSPENDED`，`CHECK IN ('ACTIVE','SUSPENDED')`（AIH-TASK-005） |
| `status_version` | BIGINT | 非空，默认 0，`CHECK >= 0`；只增不减（AIH-TASK-005） |
| `low_balance_threshold` | DECIMAL(20,8) | 可空，`CHECK >= 0`；NULL 表示不发低余额事件（AIH-TASK-005） |
| `created_at` | DATETIME | 非空 |
| `updated_at` | DATETIME | 非空 |

`contact_name` / `email` / `phone` 是个人数据（`REQ-PRIV-001`）：不进日志、不进异常消息。

**计费状态由余额驱动**（spec §7 第 8–10 条、§24；设计闸门 #88）：余额 > 0 为 `ACTIVE`，≤ 0 为
`SUSPENDED`，**正好为 0 也是暂停**。新租户余额为 0，所以默认 `SUSPENDED`。`billing_status` 与
`status_version` 只经 `app/repositories/wallet.py` 的 `post_transaction` 改变，与账本行同一事务提交；
只有真正跃迁时 `status_version` 才 +1。设置 `low_balance_threshold` 的管理入口（spec §56）归客户管理
服务任务。管理员控制的 `account_status` 是另一维，尚未建（见文末）。

## `projects`（spec §76，UI 字段见 §57）

| 列 | 类型 | 约束 |
| --- | --- | --- |
| `id` | BIGINT | 主键，自增 |
| `public_id` | CHAR(36) | 非空，唯一 |
| `tenant_id` | BIGINT | 非空，外键 → `tenants.id`，`ON DELETE RESTRICT`，有索引 |
| `name` | VARCHAR(255) | 非空 |
| `description` | VARCHAR(1000) | 可空 |
| `created_at` | DATETIME | 非空 |
| `updated_at` | DATETIME | 非空 |

按项目的读取**一律带 `tenant_id`**：给定 `(tenant_id, public_id)` 查不到就是查不到，
不区分「不存在」与「属于别的租户」（spec §97、§115）。

### 裁决：§57 与 §76 的不一致（Kelvin，2026-09-19）

spec §57 的项目字段写 `project_id` 与 `description`，§76 的表写 `id` + `public_id`、没有 `description`。

- **§57 的 `project_id` 就是 §76 的 `public_id`**：界面上标作 Project ID 显示的是 `public_id`，内部 `id` 不出库
- **加 `description`，可空**：§57 的管理端要用它。§74 写明表定义是「最低要求」，多一列不与 spec 冲突，**不需要勘误**

---

## `wallets`（spec §77；AIH-TASK-005，迁移 `0006_wallets_ledger`）

设计依据：[design/AIH-TASK-005-wallet-ledger.md](design/AIH-TASK-005-wallet-ledger.md)（设计闸门 #88 v6）。

| 列 | 类型 | 约束 |
| --- | --- | --- |
| `id` | BIGINT | 主键，自增 |
| `tenant_id` | BIGINT | 非空，外键 → `tenants.id` `ON DELETE RESTRICT`，**唯一**（一租户一钱包） |
| `currency` | CHAR(3) | 非空，`CHECK (currency = 'MYR')`（spec §5） |
| `balance` | DECIMAL(20,8) | 非空，默认 0，**允许为负**（spec §7 第 7–8 条） |
| `version` | BIGINT | 非空，默认 0；等于最后一行账本的 `wallet_sequence` |
| `created_at` / `updated_at` | DATETIME | 非空 |

另有 `UNIQUE (id, tenant_id)`，供账本的复合外键引用。`tenants.currency` 不建：币种由这里承载。

## `wallet_transactions`（spec §78；AIH-TASK-005）

| 列 | 类型 | 约束 |
| --- | --- | --- |
| `id` | BIGINT | 主键，自增 |
| `public_id` | CHAR(36) | 非空，唯一，uuid4 |
| `wallet_id` / `tenant_id` | BIGINT | 非空；`(wallet_id, tenant_id)` 复合外键 → `wallets(id, tenant_id)` `ON DELETE RESTRICT` |
| `wallet_sequence` | BIGINT | 非空，`UNIQUE (wallet_id, wallet_sequence)`，从 1 连续递增（spec 之外新增的一列，理由见设计 §9） |
| `transaction_type` | VARCHAR(64) | 非空，spec §8 的 9 种之一 |
| `amount` | DECIMAL(20,8) | 非空，有符号：贷方为正、借方为负 |
| `balance_before` / `balance_after` | DECIMAL(20,8) | 非空，`CHECK (balance_after = balance_before + amount)` |
| `reference_type` | VARCHAR(32) | 非空，`USAGE_EVENT` / `PAYMENT` / `ADMIN_ADJUSTMENT` / `REBILL` / `SYSTEM` |
| `reference_id` | VARCHAR(64) | 非空，`UNIQUE (reference_type, reference_id)`：每个财务来源至多一次效果 |
| `description` | VARCHAR(255) | 可空；调账与系统更正时就是 spec §60 的原因 |
| `metadata_json` | JSON | 可空；成本、毛利与对话内容类的键由 repository 拒绝 |
| `created_by` | BIGINT | 可空，外键 → `users.id` `ON DELETE RESTRICT`；NULL 表示系统 |
| `created_at` | DATETIME | 非空 |

索引 `ix_wallet_transactions_tenant_sequence (tenant_id, wallet_sequence)`：按租户倒序分页查流水。

其余 `CHECK`（与模型上的定义逐条一致，`tests/backend/test_migrations.py` 比对两边）：

- 类型与符号：`TOPUP`、`ADJUSTMENT_CREDIT`、`REBILL_CREDIT`、`BONUS` 为正；`AI_USAGE`、
  `ADJUSTMENT_DEBIT`、`REBILL_DEBIT`、`REFUND_ADJUSTMENT` 为负；`SYSTEM_CORRECTION` 非零
- 类型与来源：`AI_USAGE` ↔ `USAGE_EVENT`；`TOPUP` ↔ `PAYMENT`；调账类、`BONUS`、`REFUND_ADJUSTMENT`
  ↔ `ADMIN_ADJUSTMENT`；`REBILL_*` ↔ `REBILL`；`SYSTEM_CORRECTION` ↔ `SYSTEM`
- 来源是 `ADMIN_ADJUSTMENT` 或 `SYSTEM` 时 `description` 去掉首尾空格后非空；来源是
  `ADMIN_ADJUSTMENT` 时 `created_by` 非空

### 触发器（迁移 0006 建，只在 MySQL 上）

| 触发器 | 作用 |
| --- | --- |
| `wallet_transactions` BEFORE INSERT | 锁住钱包，要求租户一致、`balance_before` = 当前余额、`wallet_sequence` = `version + 1`，否则 45000 |
| `wallet_transactions` AFTER INSERT | 同一条语句里把钱包推进到这一行的 `balance_after` / `wallet_sequence` |
| `wallet_transactions` BEFORE UPDATE / BEFORE DELETE | 一律 45000：账本只能插入（Invariant 5） |
| `wallets` BEFORE INSERT | 新钱包必须余额 0、版本 0 |
| `wallets` BEFORE UPDATE | 拒绝改 `tenant_id` / `currency` / `created_at`；余额或版本的变动必须对应一行刚插入的账本（Invariant 4） |

⚠️ 前置条件：MySQL `log_bin_trust_function_creators = ON`，否则应用账号建不了触发器；迁移 0006 的
第 0 步在任何 DDL 之前检查它。⚠️ 残余风险：`TRUNCATE` / `DROP` 是 DDL，不经触发器，由
`verify_wallet` 发现，彻底封住要拆分迁移账号与运行账号（设计 §10）。

---

## 尚未建的列

AIH-TASK-004 建了两张表的身份与归属字段，AIH-TASK-005 在 `tenants` 上加了 `billing_status`、
`status_version`、`low_balance_threshold`。下面这些 spec 列刻意留给后续任务，届时都是**纯新增列**：

| 表 | 列 | 留给谁 | 为什么现在不建 |
| --- | --- | --- | --- |
| `tenants` | `account_status` | 状态模型任务（走设计闸门） | §24 由管理员控制的那一维，以及有效状态合成；碰状态机按 [WORKFLOW §3](WORKFLOW.md) 必须过设计闸门。那个任务的跃迁与计费跃迁共用 `status_version` |
| `tenants` | `currency` | **不建** | 币种由 `wallets.currency` 承载，不在租户上重复（设计闸门 #88） |
| `projects` | `integration_status` | 状态模型任务（走设计闸门） | 同上，§24 |
| `projects` | `status_webhook_url`、`encrypted_webhook_secret`、`webhook_key_version` | Webhook 任务 | 密钥的 schema 还在等 [ADR-0004](adr/ADR-0004-credential-encryption.md) 第 4a 节二选一 |
| `projects` | `backend_base_url` | Phase 3 | 应用后端集成（§35–§39） |

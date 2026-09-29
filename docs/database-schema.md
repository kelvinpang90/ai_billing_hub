# 数据库表结构

> spec §136 要求维护的文档之一，Phase 1 起按表逐步补。
> 本文件记**已裁决的表结构**与裁决理由；字段语义以 spec 为准（§74–§79），冲突时以 spec 为准并走勘误。
> 最后更新：2026-09-28

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
| `account_status` | VARCHAR(32) | 非空，服务端默认 `'ENABLED'`，`ck_tenants_account_status`：`CHECK IN ('PENDING_ACTIVATION','ENABLED','DISABLED','CLOSED')`；无索引（AIH-TASK-020，迁移 `0009_tenant_account_status`） |
| `status_version` | BIGINT | 非空，默认 0，`CHECK >= 0`；只增不减，计费与账户两维共用（AIH-TASK-005 / 020） |
| `low_balance_threshold` | DECIMAL(20,8) | 可空，`CHECK >= 0`；NULL 表示不发低余额事件（AIH-TASK-005） |
| `created_at` | DATETIME | 非空 |
| `updated_at` | DATETIME | 非空 |

`contact_name` / `email` / `phone` 是个人数据（`REQ-PRIV-001`）：不进日志、不进异常消息。

**计费状态由余额驱动**（spec §7 第 8–10 条、§24；设计闸门 #88）：余额 > 0 为 `ACTIVE`，≤ 0 为
`SUSPENDED`，**正好为 0 也是暂停**。新租户余额为 0，所以默认 `SUSPENDED`。`billing_status` 与
`status_version` 只经 `app/repositories/wallet.py` 的 `post_transaction` 改变，与账本行同一事务提交；
只有真正跃迁时 `status_version` 才 +1。设置 `low_balance_threshold` 的管理入口（spec §56）归客户管理
服务任务。

**账户状态由管理员控制**（spec §24、§56；设计闸门 #136 v2，AIH-TASK-020）：与计费状态互相独立，共用
`status_version`。只经 `app/services/account_status.py` 改变：在租户行锁（`SELECT … FOR UPDATE`，不锁钱包）
内改状态、`status_version` +1、`updated_at`，与一条 `TENANT_ACCOUNT_STATUS_CHANGED` 审计和一条
`tenant.account_status_changed` outbox 事件同一事务提交。计费跃迁同样在租户行锁内 +1，所以两维交错时版本号
严格递增、不重复。新租户与迁移前已有的租户都是 `ENABLED`（服务端默认值回填）。本任务只产生
`ENABLED` / `DISABLED`：`PENDING_ACTIVATION` 没有产生路径（V1 没有自助注册），`CLOSED` 归关户任务；两者
现在就是 CHECK 允许的取值，关户任务不必再改列。跃迁表见 [api.md](api.md) 的「管理端账户状态」。

各账户状态下以后功能的行为（设计 §2 末尾「对以后功能的契约」，Kelvin 2026-09-28 确认；本任务不实现，
由各自 Phase 的设计闸门落地，同一份记在 [TODO.md](TODO.md) 的 AIH-TASK-020 一节）：

| 功能 | `PENDING_ACTIVATION` | `ENABLED` | `DISABLED` | `CLOSED`（关户任务） |
| --- | --- | --- | --- | --- |
| 新的 AI 调用（有效状态） | 阻断 | 看计费与集成状态 | 阻断 | 阻断 |
| 用量事件摄取与扣费（Phase 2） | 照常 | 照常 | **照常**（§112.1：在途的合法事件要处理完） | 不入账，进人工复核 |
| 新充值（Phase 4） | 允许 | 允许 | **拒绝**（§112.1 第 1 步） | 拒绝 |
| 管理员调账 | 允许 | 允许 | 允许（关户前清偿 / 退款要用） | 拒绝 |
| 建项目、建凭据、轮换凭据 | 允许 | 允许 | 允许 | 拒绝（吊销凭据、查看、编辑客户仍允许） |
| 客户门户登录（Phase 4） | 由 Phase 4 定 | 允许 | 只读（看账单与收据） | 只读 |

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

另有 `UNIQUE (id, tenant_id)`（`uq_projects_id_tenant`，迁移 0007，AIH-TASK-012）：`id` 本来就唯一，这个约束
只为让 `integration_credentials` 的复合外键 `(project_id, tenant_id)` 成立。

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
| `reference_id` | VARCHAR(64) `utf8mb4_0900_bin` | 非空，`UNIQUE (reference_type, reference_id)`：每个财务来源至多一次效果。按字节比较：只差大小写或尾部空格的两个来源不是同一个 |
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

## `integration_credentials`（spec §74.4；AIH-TASK-012，迁移 `0007_integration_access`）

设计依据：[design/AIH-TASK-012-integration-access.md](design/AIH-TASK-012-integration-access.md)（设计闸门 #118 v1）。
一行是**一个 `api_key` 的一个签名版本**：轮换时 `api_key` 不变、新增一行。

| 列 | 类型 | 约束 |
| --- | --- | --- |
| `id` | BIGINT | 主键，自增；不出库 |
| `tenant_id` / `project_id` | BIGINT | 非空；`(project_id, tenant_id)` 复合外键 → `projects(id, tenant_id)` `ON DELETE RESTRICT` |
| `public_api_key` | VARCHAR(64) `utf8mb4_0900_bin` | 非空；`ak_` + 32 个小写十六进制字符。按字节比较：只差大小写或尾部空格的两个 key 不是同一个 |
| `key_version` | INT | 非空，`CHECK (key_version >= 1)`；**签名密钥版本**，客户在 `X-Acuven-Key-Version` 里看到的就是它 |
| `encrypted_secret` | TEXT | 非空；`encrypt_secret` 的输出（AES-256-GCM 信封加密），带行 AAD。库里没有明文 |
| `encryption_key_version` | INT | 非空；**主密钥**版本。与 `key_version` 分开（ADR-0004 §4）：重新包裹只改它 |
| `status` | VARCHAR(16) | 非空，`CHECK IN ('ACTIVE','REVOKED')`。「已过期」不存，由 `valid_until` 推出 |
| `valid_from` | DATETIME | 非空，建立时刻 |
| `valid_until` | DATETIME | 可空；NULL 表示没有截止，轮换时写入「现在 + 重叠期」 |
| `last_used_at` | DATETIME | 可空；列已建，写入随摄取端点做 |
| `created_at` | DATETIME | 非空 |
| `revoked_at` | DATETIME | 可空；`CHECK ((status = 'REVOKED') = (revoked_at IS NOT NULL))` |

- **唯一约束 `(public_api_key, key_version)`**（`uq_integration_credentials_key_version`），而不是 spec §74.4
  字面的「`public_api_key` 唯一」：同一个 `api_key` 的多个版本各占一行（Kelvin 2026-09-25 选定「轮换时 key 不变、
  新增版本」）。`api_key` 加版本号唯一定位一行；同一个 `api_key` 的所有行属于同一个项目（唯一插入新版本的路径是
  轮换，新行的归属取自锁住的旧行）。
- **复合外键**保证凭据行的 `tenant_id` 就是项目所属的租户（INV-8），由数据库而不是代码自觉保证。
- 索引 `ix_integration_credentials_project_id (project_id)`；按 `api_key` 的查找走唯一约束。
- **AAD**：`encrypted_secret` 加密时的关联数据是 `integration_credentials|<api_key>|<key_version>`。把一行的
  密文拷到另一行就解不开。
- **行永不删除**：以后的用量事件要引用它（INV-6）。外键 `RESTRICT`，代码里没有删除路径；吊销只改状态，
  不清密文。

---

## `project_webhook_secrets`（spec §28；AIH-TASK-019，迁移 `0008_webhook_signing`）

设计依据：[design/AIH-TASK-019-webhook-signing.md](design/AIH-TASK-019-webhook-signing.md)（设计闸门 #135 v1），
[ADR-0004](adr/ADR-0004-credential-encryption.md) 第 4a 节方案 i。一行是**一个项目的出站状态 webhook 签名密钥的
一个版本**。平台同一时刻只用 `ACTIVE` 那一把签名。

| 列 | 类型 | 约束 |
| --- | --- | --- |
| `id` | BIGINT | 主键，自增；不出库 |
| `project_id` / `tenant_id` | BIGINT | 非空；`(project_id, tenant_id)` 复合外键 → `projects(id, tenant_id)`（`uq_projects_id_tenant`，0007 建）`ON DELETE RESTRICT` |
| `key_version` | INT | 非空，`CHECK (key_version >= 1)`；**签名版本**，客户在 `X-Acuven-Key-Version` 里看到的就是它。按项目从 1 递增，不复用 |
| `status` | VARCHAR(16) | 非空，`CHECK IN ('PENDING','ACTIVE','RETIRED')`。`RETIRED` 是终态 |
| `encrypted_secret` | TEXT | 非空；`encrypt_secret` 的输出（AES-256-GCM 信封加密），带 AAD。库里没有明文 |
| `encryption_key_version` | INT | 非空；**主密钥**版本。与 `key_version` 分开（ADR-0004 §4） |
| `active_slot` | BIGINT，STORED 生成列 | `CASE WHEN status = 'ACTIVE' THEN project_id END`；唯一索引 `ux_project_webhook_secrets_active_slot` |
| `pending_slot` | BIGINT，STORED 生成列 | `CASE WHEN status = 'PENDING' THEN project_id END`；唯一索引 `ux_project_webhook_secrets_pending_slot` |
| `created_at` | DATETIME | 非空 |
| `activated_at` | DATETIME | 可空；启用时刻 |
| `retired_at` | DATETIME | 可空；退役时刻 |

- **唯一约束 `(project_id, key_version)`**（`uq_project_webhook_secrets_key_version`）。
- **每个项目至多一个 `ACTIVE`、至多一个 `PENDING`**：由两个生成列上的唯一索引保证。非该状态的行在列上是
  NULL，唯一索引允许多个 NULL（MySQL 与 SQLite 都是这个语义，测试实际插入违反约束的行验证）。绕过服务层
  的写入也造不出两把 `ACTIVE`。
- **时间与状态一致**（`ck_project_webhook_secrets_times`）：`PENDING` ⇒ `activated_at`、`retired_at` 都为空；
  `ACTIVE` ⇒ `activated_at` 非空、`retired_at` 为空；`RETIRED` ⇒ `retired_at` 非空（从 `PENDING` 直接退役的
  没有 `activated_at`）。
- **复合外键**保证行的 `tenant_id` 就是项目所属的租户（INV-8）。
- **AAD**：`encrypted_secret` 加密时的关联数据是 `project_webhook_secrets|<项目 public_id>|<key_version>`（ASCII）。
  把一行的密文拷到别的项目或别的版本上就解不开。
- **行不删除**：外键 `RESTRICT`，代码里没有删除路径；退役只改状态，不清密文（以后的投递记录要引用版本）。
- 写路径先 `SELECT … FOR UPDATE` 锁项目行，同一项目的签发、启用、退役串行执行；启用在同一事务里把原 `ACTIVE`
  改为 `RETIRED`（先退旧、再启新）。

---

## `audit_logs`（spec §66；T0.8a 迁移 `0002_auth_tables` 建表，AIH-TASK-021 迁移 `0010_audit_logs_append_only` 加触发器，AIH-TASK-022 迁移 `0011_audit_logs_query_indexes` 加查询索引）

| 列 | 类型 | 约束 |
| --- | --- | --- |
| `id` | BIGINT | 主键，自增 |
| `actor_user_id` | BIGINT | 可空（登录失败时不一定知道是谁）；**刻意没有外键**：审计要比它记录的用户活得更久，删用户不连带、也不被审计挡住 |
| `actor_role` | VARCHAR(32) | 可空 |
| `action` | VARCHAR(64) | 非空，`AuditAction` 之一（非原生 enum） |
| `entity_type` / `entity_id` | VARCHAR(64) | 可空 |
| `before_state` / `after_state` | TEXT | 可空；写入走服务层的字段白名单，密码、令牌、密钥不进 |
| `ip_address` | VARCHAR(45) | 可空 |
| `user_agent` | VARCHAR(512) | 可空 |
| `reason` | VARCHAR(255) | 可空 |
| `created_at` | DATETIME | 非空 |

索引：

| 索引 | 列 | 来源与用途 |
| --- | --- | --- |
| `ix_audit_logs_actor_created` | `(actor_user_id, created_at)` | 0002；按操作者筛选 |
| `ix_audit_logs_entity` | `(entity_type, entity_id, id)` | 0011；管理端审计查询按实体筛选，末尾的 `id` 让按 id 倒序的分页沿索引读 |
| `ix_audit_logs_action` | `(action, id)` | 0011；按动作筛选，同上 |
| `ix_audit_logs_created_at` | `(created_at)` | 0011；按时间段筛选 |

都不唯一。0011 的三个由 `GET /api/v1/admin/audit-logs`（spec §89，契约见 [api.md](api.md) 的「管理端审计日志」）使用，
模型 `AuditLog.__table_args__` 同步声明；InnoDB 在线建二级索引，不锁表、不停机（分析见迁移文件头）。
`users`、`two_factor_settings`、`recovery_codes` 三类审计的 `entity_id` 存的是内部用户 id，查询接口不把它返回、
也不让它参与筛选，换成用户邮箱。

### 只追加：由触发器强制（迁移 0010 建，只在 MySQL 上）

| 触发器 | 作用 |
| --- | --- |
| `trg_audit_logs_before_update` | BEFORE UPDATE，一律 `SIGNAL SQLSTATE '45000'`，消息 `audit_logs is append-only` |
| `trg_audit_logs_before_delete` | BEFORE DELETE，同上 |

写法照搬账本的 `trg_wallet_transactions_before_update` / `before_delete`。应用本来就只有插入路径
（`record_audit`）；触发器把「审计只能插入」从代码约定变成数据库保证：绕过应用、直接连库的 UPDATE /
DELETE 也会被拒。迁移前已有的审计行不受影响（触发器不读不改任何行）。

- ⚠️ 前置条件与账本相同：`log_bin_trust_function_creators = ON`；迁移 0010 的第 0 步在任何 DDL 之前检查它。
- ⚠️ **残余风险**：`TRUNCATE` / `DROP` 是 DDL，不经触发器；有 `TRIGGER` / `DROP` 权限的账号也能先删触发器再改数据。
  这两条挡不住，归「迁移账号与运行账号拆分」那项运维任务。测试库正是靠 `TRUNCATE TABLE audit_logs` 清场的
  —— 这本身就说明了为什么生产上运行账号不该有 DDL 权限。
- SQLite 上的单元测试用 `create_all` 建表，没有这两个触发器；只追加的行为只在真 MySQL 上验
  （`tests/backend/test_migrations.py` 的 `test_0010_*`）。
- `scripts/perf_baseline.py` 收尾不再删除审计行：压测账号那一轮的审计留在库里。

---

## 尚未建的列

AIH-TASK-004 建了两张表的身份与归属字段，AIH-TASK-005 在 `tenants` 上加了 `billing_status`、
`status_version`、`low_balance_threshold`，AIH-TASK-020 加了 `account_status`（迁移 0009）。AIH-TASK-012 建了
spec §74.4 的 `integration_credentials`（入站 API 凭据），没有给 `projects` 加列，只加了一个唯一约束。
AIH-TASK-019 建了 `project_webhook_secrets`（出站签名密钥），也没有给 `projects` 加列：spec §76 的
`encrypted_webhook_secret` / `webhook_key_version` 由这张表取代，**不再建**。下面这些 spec 列刻意留给后续任务，
届时都是**纯新增列**：

| 表 | 列 | 留给谁 | 为什么现在不建 |
| --- | --- | --- | --- |
| `tenants` | `currency` | **不建** | 币种由 `wallets.currency` 承载，不在租户上重复（设计闸门 #88） |
| `projects` | `integration_status` | 状态模型任务（走设计闸门） | §24 项目的集成状态，与有效状态合成（`ALLOW_AI` / `BLOCK_AI`）一起归「出站服务状态 webhook 投递」设计闸门（AIH-TASK-020 设计 §1 非目标）；碰状态机按 [WORKFLOW §3](WORKFLOW.md) 必须过设计闸门 |
| `projects` | `status_webhook_url` | Phase 3「出站服务状态 webhook 投递」设计闸门 | 投递目标随投递 worker、重试与 `webhook_deliveries` 一起做（AIH-TASK-019 设计 §1 非目标） |
| `projects` | `encrypted_webhook_secret`、`webhook_key_version` | **不建** | 由 `project_webhook_secrets` 表取代（[ADR-0004](adr/ADR-0004-credential-encryption.md) 第 4a 节方案 i，AIH-TASK-019）：多版本、签名版本与主密钥版本分列 |
| `projects` | `backend_base_url` | Phase 3 | 应用后端集成（§35–§39） |

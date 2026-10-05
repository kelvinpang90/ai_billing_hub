# 数据库表结构

> spec §136 要求维护的文档之一，Phase 1 起按表逐步补。
> 本文件记**已裁决的表结构**与裁决理由；字段语义以 spec 为准（§74–§79），冲突时以 spec 为准并走勘误。
> 最后更新：2026-10-05

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
  `amount` / `balance_before` / `balance_after`；`provider_price_components` 的 `rate_amount`（原币种单价）与
  `unit_quantity`（多少个计量单位）同一精度。汇率不是金额：`fx_rate_versions.rate` 是 `DECIMAL(24,10)`（设计闸门
  #183 §2），同样不用浮点
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
由各自 Phase 的设计闸门落地，同一份记在 [TODO 历史记录](archive/TODO_RECORDS_2026-09-30.md) 的 AIH-TASK-020 一节）：

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

## AI 目录（spec §74、§12、§15.1、§84；AIH-TASK-025，迁移 `0012_ai_catalog`）

设计依据：[design/AIH-TASK-025-ai-catalog.md](design/AIH-TASK-025-ai-catalog.md)（设计闸门 #163 v4）§2「数据库」。
五张表：`usage_meter_types`、`usage_meter_components`、`ai_providers`、`ai_models`、`ai_model_aliases`。
接口见 [api.md](api.md) 的「AI 目录」。

共同的约定：

- **代码列一律 `utf8mb4_0900_bin`**（区分大小写、不忽略尾部空格；只在 MySQL 上指定）：`usage_meter_types.code`
  与 `unit`、`usage_meter_components.component_code`、`ai_providers.code`、`ai_models.code`、`ai_model_aliases.alias`。
  理由同 `integration_credentials.public_api_key`：库默认的 `utf8mb4_0900_ai_ci` 会把 `GPT-4o` 与 `gpt-4o`
  当成同一个值。
- **行永不删除**：价格版本（T-B）、定价规则（T-D）与用量事件会永久引用它们（INV-6）。外键都是 `RESTRICT`，
  代码里没有删除路径；停用只改状态，别名段只会被截断。
- **代码建后不可改**：`code`、`alias`、`component_code`、`unit`、`quantity_kind`、`payload_shape` 没有任何
  修改路径（PATCH 带它们是 422）。
- **状态**（三张表的 `status`）：`VARCHAR(16)`，`CHECK IN ('ACTIVE','RETIRED')`，默认 `ACTIVE`。停用只影响
  「以后选不选它」，不影响摄取、计价与解析；双向都允许，没有终态。

### `usage_meter_types`（计量类型：迁移种子 + 管理员新建）

| 列 | 类型 | 约束 |
| --- | --- | --- |
| `id` | BIGINT | 主键，自增；不出库 |
| `public_id` | CHAR(36) | 非空，唯一（`uq_usage_meter_types_public_id`） |
| `code` | VARCHAR(32) bin | 非空，唯一（`uq_usage_meter_types_code`）；即上报事件的 `usage_type` |
| `display_name` | VARCHAR(255) | 非空；可改 |
| `payload_shape` | VARCHAR(32) | 非空，`CHECK IN ('LLM_TOKEN_FIELDS','QUANTITY')`：上报形态 |
| `unit` | VARCHAR(16) bin | 非空；上报的 `unit` 必须与它相等 |
| `quantity_kind` | VARCHAR(16) | 非空，`CHECK IN ('INTEGER','DECIMAL')` |
| `status` | VARCHAR(16) | 见上 |
| `created_at` / `updated_at` | DATETIME | 非空 |

- 唯一约束 `(id, payload_shape)`（`uq_usage_meter_types_id_shape`）：只为分量表的复合外键。
- `ck_usage_meter_types_token_shape`：`payload_shape <> 'LLM_TOKEN_FIELDS' OR (unit = 'TOKEN' AND quantity_kind = 'INTEGER')`。
- 管理员新建的类型一律是 `QUANTITY`；`LLM_TOKEN_FIELDS` 只有种子 `LLM_TOKEN`。

### `usage_meter_components`（计价分量）

| 列 | 类型 | 约束 |
| --- | --- | --- |
| `id` | BIGINT | 主键，自增；不出库，没有 `public_id`（对外用 `component_code`） |
| `meter_type_id` / `payload_shape` | BIGINT / VARCHAR(32) | 非空；复合外键 `(meter_type_id, payload_shape)` → `usage_meter_types(id, payload_shape)` `ON DELETE RESTRICT`（`fk_usage_meter_components_type`）：分量的形态**由数据库保证**与所属类型一致。子表索引 `ix_usage_meter_components_type_shape` |
| `component_code` | VARCHAR(64) bin | 非空，唯一（`uq_usage_meter_components_code`）。T-B 的价格分量、T-D 的 FIXED_RATE 分量按它挂 |
| `quantity_field` | VARCHAR(64) | 非空；这个分量从上报事件的哪个字段取数量（T-G 按它取数，不按类型写分支） |
| `created_at` | DATETIME | 非空 |

- `ck_usage_meter_components_quantity_field`：`(payload_shape = 'QUANTITY' AND quantity_field = 'quantity') OR
  (payload_shape = 'LLM_TOKEN_FIELDS' AND quantity_field IN ('input_tokens','output_tokens','cache_creation_input_tokens','cache_read_input_tokens'))`。
- 唯一约束 `(meter_type_id, quantity_field)`（`uq_usage_meter_components_type_field`）：同一类型下一个字段只对应
  一个分量，同一批数量不会被算两次；加上上一条 CHECK，`QUANTITY` 类型至多一个分量。
- 「每个类型至少一个分量」数据库表达不了：由「建类型与建分量同一事务」保证（没有单独建类型或单独建分量的
  路径）。万一出现没有分量的类型，T-G 按 `PRICING_ERROR` 处理，不按 0 计。

### 种子（迁移 0012 写入，常量写死在迁移里）

| 计量类型 | 形态 | 单位 | 数量 | 分量 → 取数字段 |
| --- | --- | --- | --- | --- |
| `LLM_TOKEN` | `LLM_TOKEN_FIELDS` | `TOKEN` | `INTEGER` | `LLM_INPUT_TOKEN` → `input_tokens`；`LLM_OUTPUT_TOKEN` → `output_tokens`；`LLM_CACHE_WRITE_TOKEN` → `cache_creation_input_tokens`；`LLM_CACHE_READ_TOKEN` → `cache_read_input_tokens` |
| `EMBEDDING_TOKEN` | `QUANTITY` | `TOKEN` | `INTEGER` | `EMBEDDING_TOKEN` → `quantity` |
| `AUDIO_SECOND` | `QUANTITY` | `SECOND` | `DECIMAL` | `AUDIO_SECOND` → `quantity` |
| `AUDIO_MINUTE` | `QUANTITY` | `MINUTE` | `DECIMAL` | `AUDIO_MINUTE` → `quantity` |
| `TTS_CHARACTER` | `QUANTITY` | `CHARACTER` | `INTEGER` | `TTS_CHARACTER` → `quantity` |
| `IMAGE_GENERATION` | `QUANTITY` | `IMAGE` | `INTEGER` | `IMAGE_GENERATION` → `quantity` |
| `OCR_PAGE` | `QUANTITY` | `PAGE` | `INTEGER` | `OCR_PAGE` → `quantity` |
| `DOCUMENT_PAGE` | `QUANTITY` | `PAGE` | `INTEGER` | `DOCUMENT_PAGE` → `quantity` |
| `CUSTOM` | `QUANTITY` | `UNIT` | `DECIMAL` | `CUSTOM` → `quantity` |

9 个类型、12 个分量，全部 `ACTIVE`；显示名见 `app/models/ai_catalog.py` 的 `SEED_METER_TYPES`（代码里的同一份
清单，`tests/backend/test_migrations.py` 逐行比对迁移、代码常量与库里三方）。⚠️ 缓存写入只有一个分量
`LLM_CACHE_WRITE_TOKEN`（已知限制，见 [TODO 历史记录](archive/TODO_RECORDS_2026-09-30.md) 的 AIH-TASK-025 记录段）。本任务**不**预置任何供应商与模型。

### `ai_providers`

| 列 | 类型 | 约束 |
| --- | --- | --- |
| `id` | BIGINT | 主键，自增；不出库 |
| `public_id` | CHAR(36) | 非空，唯一（`uq_ai_providers_public_id`） |
| `code` | VARCHAR(64) bin | 非空，唯一（`uq_ai_providers_code`）；与上报事件的 `provider` 精确比较 |
| `display_name` | VARCHAR(255) | 非空；可改 |
| `status` | VARCHAR(16) | 见上 |
| `created_at` / `updated_at` | DATETIME | 非空 |

⚠️ 这一行也是**解析与改映射的串行点**：建模型、映射别名、撤销别名 `SELECT … FOR UPDATE` 它；计费解析
（`resolve_model`）在计费事务里 `FOR SHARE` 它并持有到提交（设计 §2「时间」）。

### `ai_models`

| 列 | 类型 | 约束 |
| --- | --- | --- |
| `id` | BIGINT | 主键，自增；不出库 |
| `public_id` | CHAR(36) | 非空，唯一（`uq_ai_models_public_id`） |
| `provider_id` | BIGINT | 非空；外键 → `ai_providers(id)` `ON DELETE RESTRICT`（`fk_ai_models_provider`） |
| `code` | VARCHAR(128) bin | 非空；与上报事件的 `model` 精确比较 |
| `display_name` | VARCHAR(255) | 非空；可改 |
| `status` | VARCHAR(16) | 见上 |
| `created_at` / `updated_at` | DATETIME | 非空 |

- 唯一约束 `(provider_id, code)`（`uq_ai_models_provider_code`），也是按供应商查模型的索引。
- 唯一约束 `(id, provider_id)`（`uq_ai_models_id_provider`）：只为别名表的复合外键（先例 `uq_projects_id_tenant`）。
- 同一供应商下，模型代码不能是别名表里**出现过**的任何字符串（不论哪一段、是否已截断），反之亦然。跨两张表，
  数据库表达不了：服务层在供应商行锁内检查。

### `ai_model_aliases`（§84 的「映射」，按 `occurred_at` 分段）

一行是「某个字符串在 `[effective_from, effective_to)` 内指向某个模型」。

| 列 | 类型 | 约束 |
| --- | --- | --- |
| `id` | BIGINT | 主键，自增；不出库 |
| `public_id` | CHAR(36) | 非空，唯一（`uq_ai_model_aliases_public_id`） |
| `provider_id` / `model_id` | BIGINT | 非空；复合外键 `(model_id, provider_id)` → `ai_models(id, provider_id)` `ON DELETE RESTRICT`（`fk_ai_model_aliases_model`）：别名只能指向**同一供应商**的模型，由数据库保证。子表索引 `ix_ai_model_aliases_model` |
| `alias` | VARCHAR(128) bin | 非空；上报事件里出现、但不是模型代码的那个字符串 |
| `effective_from` | DATETIME | 可空；`NULL` = 「一直以来」，只有一个字符串的第一段是 `NULL` |
| `effective_to` | DATETIME | 可空；`NULL` = 仍生效。只能从 `NULL` 改成一个时刻（截断），截断后不再改 |
| `open_slot` | INT，STORED 生成列 | `CASE WHEN effective_to IS NULL THEN 1 END`；只读 |
| `created_at` | DATETIME | 非空 |
| `closed_at` | DATETIME | 可空；与 `effective_to` 同时写 |

- **唯一索引 `(provider_id, alias, open_slot)`**（`ux_ai_model_aliases_open_slot`）：一个字符串至多一段未截断。
  已截断的段在 `open_slot` 上是 NULL，唯一索引允许多个 NULL（写法同 `project_webhook_secrets.active_slot`）。
  它也是按 (供应商, 字符串) 查段的索引。
- `ck_ai_model_aliases_period`：`effective_from IS NULL OR effective_to IS NULL OR effective_from < effective_to`。
- **各段首尾相接、不重叠**跨行，数据库表达不了：服务层在供应商行锁内保证（新段的起点永远取当前段被截断的
  那个时刻，或撤销后最后一段的终点），并在每次写入后于同一事务里复查该字符串的全部段（排序后相邻段
  `effective_to == 下一段 effective_from`、只有第一段 `effective_from` 为空），不满足就回滚。
- 段从不修改指向、从不往过去延伸；改指向与撤销只截断当前段、从边界时刻 `t`（拿到供应商行锁之后的当前时间
  向上取整到下一个整秒）起开新段。

---

## 供应商成本价（spec §14、§15.1、§17、§74.1；AIH-TASK-026，迁移 `0013_provider_prices`）

设计依据：[design/AIH-TASK-026-provider-prices.md](design/AIH-TASK-026-provider-prices.md)（设计闸门 #177 v4）
§2「数据库」。两张表：`provider_price_versions`、`provider_price_components`。接口见 [api.md](api.md) 的
「管理端供应商价格」。

共同的约定：

- **价格只存原币种**（通常 USD，也可以是 MYR），不换算、不存 MYR（ADR-0005 §5）；FX 归 T-C。成本价只在管理端
  出现，客户不可见（INV-7）。
- **行永不删除**：版本由触发器拒绝 DELETE（丢弃的草稿也留痕）；外键都是 `RESTRICT`。
- **发布后不可变**：由下面的触发器保证，只在 MySQL 上。SQLite（单元测试）上没有触发器，服务层按同样的规则写。

### `provider_price_versions`

| 列 | 类型 | 约束 |
| --- | --- | --- |
| `id` | BIGINT | 主键，自增；不出库（T-H 把它写进事件快照，但不对外） |
| `public_id` | CHAR(36) | 非空，唯一（`uq_provider_price_versions_public_id`） |
| `provider_id` / `model_id` | BIGINT | 非空；复合外键 `(model_id, provider_id)` → `ai_models(id, provider_id)` `ON DELETE RESTRICT`（`fk_provider_price_versions_model`）：版本只能挂在**同一供应商**的模型上。子表索引 `ix_provider_price_versions_model` |
| `source_currency` | CHAR(3) | 非空；ISO 4217 大写三字母（格式在接口层校验） |
| `source_type` | VARCHAR(16) | 非空，`CHECK IN ('MANUAL','SYNC')`；本任务只写 `MANUAL`，`SYNC` 留给 Phase 8 |
| `source_reference` | VARCHAR(255) | 非空；价格出处（例如供应商价格页与查看日期），去首尾空白后 1–255。不写合同价 |
| `status` | VARCHAR(16) | 非空，`CHECK IN ('DRAFT','PUBLISHED','RETIRED','DISCARDED')`；`DISCARDED` 是 §74.1 之外补的 |
| `effective_from` | DATETIME | 可空；草稿为空。`NULL` 另一种含义只有一处：该（供应商, 模型）第一个已发布版本未指定生效时刻 = 「一直以来」 |
| `effective_to` | DATETIME | 可空；`NULL` = 仍生效（或尚未发布） |
| `open_slot` | INT，STORED 生成列 | `CASE WHEN status = 'PUBLISHED' AND effective_to IS NULL THEN 1 END`；只读 |
| `created_by` | BIGINT | 非空；外键 → `users(id)` `RESTRICT`（`fk_provider_price_versions_created_by`） |
| `approved_by` | BIGINT | 可空；发布人；外键 → `users(id)` `RESTRICT`（`fk_provider_price_versions_approved_by`） |
| `created_at` / `updated_at` | DATETIME | 非空 |
| `approved_at` | DATETIME | 可空；发布时刻 |

- **唯一索引 `(provider_id, model_id, open_slot)`**（`ux_provider_price_versions_open_slot`）：一个（供应商, 模型）
  至多一个未截断的已发布版本。其余行在 `open_slot` 上是 NULL，唯一索引允许多个 NULL。
- `ck_provider_price_versions_unpublished`：`DRAFT` / `DISCARDED` 的 `effective_from`、`effective_to`、`approved_by`、
  `approved_at` 全为空。
- `ck_provider_price_versions_approved`：`PUBLISHED` / `RETIRED` 的 `approved_by`、`approved_at` 非空。
- `ck_provider_price_versions_period`：`effective_from IS NULL OR effective_to IS NULL OR effective_from < effective_to
  OR (status = 'RETIRED' AND effective_from = effective_to)`。最后一项是「撤销尚未生效的预约」留下的空区间
  `[F, F)`，它永不匹配任何时刻。

### `provider_price_components`

| 列 | 类型 | 约束 |
| --- | --- | --- |
| `id` | BIGINT | 主键，自增；不出库 |
| `provider_price_version_id` | BIGINT | 非空；外键 → `provider_price_versions(id)` `RESTRICT`（`fk_provider_price_components_version`） |
| `usage_meter_component_id` | BIGINT | 非空；外键 → `usage_meter_components(id)` `RESTRICT`（`fk_provider_price_components_meter_component`）。§74.1 的 `meter_type_id`、`component_code`、`unit` 由它唯一确定，不在本表重复存；子表索引 `ix_provider_price_components_meter_component` |
| `unit_quantity` | DECIMAL(20,8) | 非空，`CHECK > 0`；多少个计量单位对应一个 `rate_amount`（例如每 1 000 000 个 token） |
| `rate_amount` | DECIMAL(20,8) | 非空，`CHECK > 0`；原币种单价 |
| `metadata_json` | JSON | 可空；只作备注（例如供应商的档位名），**计价不读它**。区分价格的维度必须是不同的分量（§15.1） |
| `created_at` | DATETIME | 非空 |

- 唯一约束 `(provider_price_version_id, usage_meter_component_id)`（`uq_provider_price_components_version_component`）。
- **完整性**：凡是版本里出现的计量类型，它在 `usage_meter_components` 里的全部分量都必须出现（出现 `LLM_TOKEN`
  就要四个价）；整体没出现的计量类型不要求（那一类用量计价时 `PRICING_ERROR`，不按 0 算）。发布时服务层校验并
  列出缺的分量，发布跃迁上的触发器兜底。

### 触发器（迁移 0013 建，只在 MySQL 上）

写法与权限预检照迁移 0006（binlog 开着而 `log_bin_trust_function_creators` 关着时，迁移在任何 DDL 之前拒绝运行）。
拒绝一律 `SIGNAL SQLSTATE '45000'`（MySQL 错误号 1644）。

| 触发器 | 做什么 |
| --- | --- |
| `trg_provider_price_versions_before_insert` | `status` 不是 `DRAFT` 就拒绝：版本只能以草稿进入，发布跃迁上的检查因此绕不过去 |
| `trg_provider_price_versions_before_update` | ① `DISCARDED` 的行拒绝任何改动；草稿只能改成 `DRAFT` / `PUBLISHED` / `DISCARDED`（不许直接 `DRAFT → RETIRED` 绕过 ③ 的完整性检查）；② `PUBLISHED` / `RETIRED` 的行除 `effective_to`、`status`（只许 `PUBLISHED → RETIRED`）与 `updated_at` 外任何列变化都拒绝；③ `DRAFT → PUBLISHED` 时：至少一个分量，且出现的计量类型的全部分量都在；④ 新状态是 `PUBLISHED` / `RETIRED` 且区间或状态有变化时：先 `SELECT … FROM ai_providers WHERE id = NEW.provider_id FOR UPDATE`（与服务层发布同一把锁），再查同一（供应商, 模型）里是否有另一个**非空区间**的已发布版本与本行相交，有就拒绝。空区间不参与判定 |
| `trg_provider_price_versions_before_delete` | 一律拒绝 |
| `trg_provider_price_components_before_insert` / `_before_update` / `_before_delete` | 所属版本（更新时新旧两个）不是 `DRAFT` 就拒绝：分量只在草稿阶段可增删改 |

`DRAFT` 的行在 ② 之外，可以任意改（服务层只改 `source_currency`、`source_reference`、`updated_at` 与发布、丢弃时的列）。
**残余风险**：`TRUNCATE` / `DROP` 是 DDL，不经触发器（与 0006 相同）。

### 锁与写入顺序

- **发布、退役**：`SELECT … FROM ai_providers WHERE id = ? FOR UPDATE`（与 025 改映射同一把锁），拿到锁之后才取
  边界时刻 `t`（服务端当前时间向上取整到下一个整秒）；再 `FOR UPDATE` 读版本行与该（供应商, 模型）的全部已发布
  版本。写完在同一事务里复查区间，再写审计、提交。
  ⚠️ 这把锁是事务的**第一条语句**：版本属于哪个供应商在另一个短事务里查；锁之后对模型、分量、计量类型分量、
  响应里的供应商 / 模型 / 用户的读一律 `FOR SHARE`。MySQL 的 REPEATABLE READ 在事务第一次普通读时建立快照，
  锁前做过普通读的话，锁后的普通读（以及触发器里的完整性子查询）仍读那个快照，看不见等锁期间提交的模型停用
  或分量替换。改草稿同样先 `FOR UPDATE` 版本行，所以与发布串行。
- **计费取价**（T-G / T-H，本任务不接线）：先 `resolve_model`（对供应商行 `FOR SHARE`），再
  `resolve_provider_price`（对版本与分量 `FOR SHARE`），锁持有到计费事务提交。锁顺序：供应商（共享）→ 钱包 →
  租户，价格表不引入新的锁顺序。
- **建草稿、改草稿、丢弃**：不锁供应商行（草稿不参与计费），只 `FOR UPDATE` 版本行。
- **写入顺序**（每一步立刻 flush，数据库的区间触发器与 `open_slot` 唯一索引按行检查）：发布时先截断前一个
  （写 `effective_to`），再把本行从草稿改为已发布；撤销预约时先把被撤销的一行改成空区间，再把前一个恢复为未截断。
  改草稿的分量是整体替换：先删旧分量（flush），再插新分量。

---

## 客户定价规则（spec §15、§16、§74.3；ADR-0008；AIH-TASK-027，迁移 `0014_pricing_rules`）

设计依据：[design/AIH-TASK-027-pricing-rules.md](design/AIH-TASK-027-pricing-rules.md)（设计闸门 #178 v4）
§2「数据库」。三张表：`pricing_rule_locks`、`pricing_rules`、`pricing_rule_components`。接口见 [api.md](api.md) 的
「管理端定价规则」；五级顺序与下落语义见 [pricing-engine.md](pricing-engine.md)。

共同的约定：

- **价格是 MYR 含税价**（ADR-0008）：`rate_amount` 是含税单价，倍数乘出来的是含税计费额。不存税额、不引用税务政策
  版本。倍数与单价只在管理端出现（INV-7）。
- **行永不删除**：规则由触发器拒绝 DELETE（丢弃的草稿也留痕）；外键都是 `RESTRICT`。
- **发布后不可变**：由下面的触发器保证，只在 MySQL 上。SQLite（单元测试）上没有触发器，服务层按同样的规则写。

### `pricing_rule_locks`（单行锁表）

| 列 | 类型 | 约束 |
| --- | --- | --- |
| `id` | INT | 主键，不自增；迁移写入唯一一行 `id = 1` |

没有业务字段。发布 / 停用对这一行 `SELECT … FOR UPDATE`，计费对它 `FOR SHARE`（见下「锁顺序」）。之所以不锁供应商
行：「客户默认」「全局默认」两级规则没有供应商；锁租户行又会与计费既有的「钱包 → 租户」顺序相反。触发器保证它恰好
一行。单元测试的 `Base.metadata.create_all` 同样写入这一行（`after_create` 事件，见 `app/models/pricing_rules.py`）。

### `pricing_rules`

| 列 | 类型 | 约束 |
| --- | --- | --- |
| `id` | BIGINT | 主键，自增；不出库（T-H 把它写进事件快照 `pricing_rule_id`，但不对外） |
| `public_id` | CHAR(36) | 非空，唯一（`uq_pricing_rules_public_id`） |
| `priority_scope` | VARCHAR(32) | 非空，`CHECK IN ('CUSTOMER_PROVIDER_MODEL','CUSTOMER_PROVIDER','CUSTOMER','GLOBAL_PROVIDER_MODEL','GLOBAL')`（§16 从高到低） |
| `tenant_id` | BIGINT | 可空；外键 → `tenants(id)` `RESTRICT`（`fk_pricing_rules_tenant`） |
| `provider_id` | BIGINT | 可空；外键 → `ai_providers(id)` `RESTRICT`（`fk_pricing_rules_provider`） |
| `model_id` | BIGINT | 可空；复合外键 `(model_id, provider_id)` → `ai_models(id, provider_id)` `RESTRICT`（`fk_pricing_rules_model`）：规则只能挂在同一供应商的模型上。子表索引 `ix_pricing_rules_model` |
| `strategy` | VARCHAR(16) | 非空，`CHECK IN ('MARKUP','FIXED_RATE')` |
| `markup_multiplier` | DECIMAL(20,8) | 可空；只有 MARKUP 有且 > 0 |
| `status` | VARCHAR(16) | 非空，`CHECK IN ('DRAFT','PUBLISHED','RETIRED','DISCARDED')`；`DISCARDED` 是 §74.3 之外补的 |
| `effective_from` | DATETIME | 可空；草稿为空。`NULL` 另一种含义只有一处：`GLOBAL` 范围第一条、未指定生效时刻 = 「一直以来」 |
| `effective_to` | DATETIME | 可空；`NULL` = 仍生效（或尚未发布） |
| `scope_key` | VARCHAR(128)，STORED 生成列 | `CONCAT(priority_scope, ':', COALESCE(tenant_id, 0), ':', COALESCE(provider_id, 0), ':', COALESCE(model_id, 0))`：把「同一范围」变成一个非空值，绕开 MySQL 唯一约束不管 NULL 的问题；只读。SQLite 上用 `\|\|` 写同一个值（`CONCAT` 要 SQLite 3.44） |
| `open_slot` | INT，STORED 生成列 | `CASE WHEN status = 'PUBLISHED' AND effective_to IS NULL THEN 1 END`；只读 |
| `created_by` | BIGINT | 非空；外键 → `users(id)` `RESTRICT`（`fk_pricing_rules_created_by`） |
| `approved_by` | BIGINT | 可空；发布人；外键 → `users(id)` `RESTRICT`（`fk_pricing_rules_approved_by`） |
| `created_at` / `updated_at` | DATETIME | 非空 |
| `approved_at` | DATETIME | 可空；发布时刻 |

- `ck_pricing_rules_scope_columns`：**范围与 NULL 组合一一对应**（§74.3「不能有含糊的 NULL 组合」）：
  `CUSTOMER_PROVIDER_MODEL` 三列都非空；`CUSTOMER_PROVIDER` 租户与供应商非空、模型空；`CUSTOMER` 只有租户；
  `GLOBAL_PROVIDER_MODEL` 供应商与模型非空、租户空；`GLOBAL` 三列都空。§16 之外的组合写不进去。
- `ck_pricing_rules_markup`：`(strategy = 'MARKUP' AND markup_multiplier IS NOT NULL AND markup_multiplier > 0)
  OR (strategy = 'FIXED_RATE' AND markup_multiplier IS NULL)`。
- `ck_pricing_rules_unpublished` / `_approved` / `_period`：与 `provider_price_versions` 同一写法（草稿与丢弃的草稿没有
  区间与发布人；发布过的有发布人；区间不倒置，唯一的空区间 `[F, F)` 在 `RETIRED` 上）。
- `ck_pricing_rules_retired_end`（v4）：`status <> 'RETIRED' OR effective_to IS NOT NULL`，停用必有尽头。没有尽头的
  `RETIRED` 行会一直被解析命中，并让该范围之后的发布都撞上区间不重叠触发器。
- **唯一索引 `(scope_key, open_slot)`**（`ux_pricing_rules_open_slot`）：同一范围至多一条未截断的已发布规则（全局
  范围也是：`scope_key` 非空）。
- 唯一约束 `(id, strategy)`（`uq_pricing_rules_id_strategy`）：只为分量表的复合外键。
- 索引 `ix_pricing_rules_resolve (priority_scope, tenant_id, provider_id, model_id, effective_from)`：解析时逐级查。

### `pricing_rule_components`（只属于 FIXED_RATE 规则）

| 列 | 类型 | 约束 |
| --- | --- | --- |
| `id` | BIGINT | 主键，自增；不出库 |
| `pricing_rule_id` | BIGINT | 非空 |
| `strategy` | VARCHAR(16) | 非空，`CHECK = 'FIXED_RATE'`；复合外键 `(pricing_rule_id, strategy)` → `pricing_rules(id, strategy)` `ON DELETE RESTRICT ON UPDATE RESTRICT`（`fk_pricing_rule_components_rule`，子表索引 `ix_pricing_rule_components_rule`）：**MARKUP 规则在数据库层挂不上分量**；有分量的草稿改不成 MARKUP（服务层先删分量） |
| `usage_meter_component_id` | BIGINT | 非空；外键 → `usage_meter_components(id)` `RESTRICT`（`fk_pricing_rule_components_meter_component`）。§74.3 的 `meter_type_id`、`component_code`、`unit` 由它唯一确定 |
| `unit_quantity` | DECIMAL(20,8) | 非空，`CHECK > 0` |
| `rate_amount` | DECIMAL(20,8) | 非空，`CHECK > 0`；**MYR 含税单价** |
| `currency` | CHAR(3) | 非空，`CHECK = 'MYR'` |
| `created_at` | DATETIME | 非空 |

- 唯一约束 `(pricing_rule_id, usage_meter_component_id)`（`uq_pricing_rule_components_rule_component`）。
- **完整性**（FIXED_RATE 发布时）：至少一个分量；凡是出现的计量类型，它在 `usage_meter_components` 里的全部分量
  都必须出现。服务层校验并列出缺的分量，发布跃迁上的触发器兜底。

### 触发器（迁移 0014 建，只在 MySQL 上）

写法与权限预检照迁移 0006。拒绝一律 `SIGNAL SQLSTATE '45000'`（MySQL 错误号 1644）。

| 触发器 | 做什么 |
| --- | --- |
| `trg_pricing_rule_locks_before_insert` | 表里已有一行时拒绝 |
| `trg_pricing_rule_locks_before_delete` | 一律拒绝 |
| `trg_pricing_rule_locks_before_update` | BEFORE UPDATE（迁移 0016，一律拒绝）：不许把锁行改走（改 `id`、`SET id = 1` 这种不改值的写法都拒绝），否则 ④ 的 `WHERE id = 1 FOR UPDATE` 会静默锁不到行（AIH-TASK-045，设计闸门 #200 v1）；错误文本与上两个相同。锁定读不是 UPDATE，不触发 |
| `trg_pricing_rules_before_insert` | `status` 不是 `DRAFT` 就拒绝：规则只能以草稿进入，发布跃迁上的检查因此绕不过去 |
| `trg_pricing_rules_before_update` | ① `DISCARDED` 的行拒绝任何改动；草稿只能改成 `DRAFT` / `PUBLISHED` / `DISCARDED`（设计 §4 的状态表；不许直接 `DRAFT → RETIRED` 绕过 ③）；② `PUBLISHED` / `RETIRED` 的行除 `effective_to`、`status`（只许 `PUBLISHED → RETIRED`）与 `updated_at` 外任何列变化都拒绝；③ `DRAFT → PUBLISHED` 且 `FIXED_RATE` 时：至少一个分量，且出现的计量类型的全部分量都在；④ 新状态是 `PUBLISHED` / `RETIRED` 且区间或状态有变化时：先 `SELECT … FROM pricing_rule_locks WHERE id = 1 FOR UPDATE`（与服务层发布同一把锁），再查同一 `scope_key` 里是否有另一个**非空区间**的已发布规则与本行相交，有就拒绝；空区间不参与判定；⑤ 同一把锁内，`DRAFT → PUBLISHED` 且 `effective_from` 为空时：只许 `GLOBAL` 范围、且该范围没有任何 `PUBLISHED` / `RETIRED` 行（含撤销的预约留下的空区间），否则拒绝（v4：「一直以来」只给全局默认的第一条） |
| `trg_pricing_rules_before_delete` | 一律拒绝 |
| `trg_pricing_rule_components_before_insert` / `_before_update` / `_before_delete` | 所属规则（更新时新旧两个）不是 `DRAFT` 就拒绝 |

⚠️ MySQL 的触发器不能用 `NEW` / `OLD` 引用生成列，所以 ④ 按 `scope_key` 的定义现算出本行的键，与另一行存下的
`scope_key` 比较；② 也不比两个生成列（它们只由被比较的列算出）。**残余风险**：`TRUNCATE` / `DROP` 是 DDL，不经
触发器（与 0006 相同）。

### 锁顺序与写入顺序

- **发布、停用**：事务的第一条语句是 `SELECT … FROM pricing_rule_locks WHERE id = 1 FOR UPDATE`，拿到锁之后才取
  边界时刻 `t`（服务端当前时间向上取整到下一个整秒）；再 `FOR UPDATE` 读规则行与该范围的全部已发布规则；对租户、
  供应商、模型、分量、计量类型分量与用户的读一律 `FOR SHARE`（不读 REPEATABLE READ 在锁前建立的快照）。写完在同一
  事务里复查区间，再写审计、提交。发布路径只拿这一把锁，不形成环。
- **计费**（T-G / T-H，本任务不接线）：**供应商（S）→ 定价规则锁（S）→ 钱包 → 租户**。`resolve_model` 对供应商行
  `FOR SHARE`；之后 `lock_pricing_rules_shared` 对 `pricing_rule_locks` 取 `FOR SHARE`，持有到计费事务提交；价格与
  规则的解析都是 `FOR SHARE`。共享锁之间不冲突，计费事务彼此不阻塞；只有低频的发布 / 停用会让计费等一下。
- **建草稿、改草稿、丢弃**：不拿规则锁（草稿不参与计费），只 `FOR UPDATE` 规则行；与发布同一条规则串行。
- **写入顺序**（每一步立刻 flush，区间触发器与 `open_slot` 唯一索引按行检查）：发布时先截断前一条（写
  `effective_to`），再把本行从草稿改为已发布；撤销预约时先把被撤销的一行改成空区间，再把前一条恢复为未截断。改草稿：
  先删旧分量（改成 MARKUP 时复合外键要求分量先没了），再改策略与倍数（同一条 UPDATE，CHECK 要求两者一致），最后插入
  新分量。

---

## FX 汇率（spec §17.1、§74.2；ADR-0005；AIH-TASK-028，迁移 `0015_fx_rates`）

设计依据：[design/AIH-TASK-028-fx-rates.md](design/AIH-TASK-028-fx-rates.md)（设计闸门 #183 v3）§2「数据库」与 §8。
三张表：`fx_rate_locks`、`fx_rate_versions`、`fx_fetch_attempts`。AIH-TASK-028 是设计 §11 的 F1，只建表、约束与触发器；
发布 / 退役规则与 `resolve_fx_rate`（AIH-TASK-039）、BNM 拉取（AIH-TASK-040）、管理端接口（AIH-TASK-041）、告警接口
（AIH-TASK-042）都在后续任务里。

共同的约定：

- **`rate` 是 1 单位 `base_currency` 等于多少 MYR**，DECIMAL(24,10)，精确、不舍入（BNM 按 `unit` 报价，入库前除以
  `unit`，要求 10 位小数内精确）。
- **行永不删除**：版本由触发器拒绝 DELETE（丢弃的草稿也留痕），拉取记录只增；外键都是 `RESTRICT`。
- **发布后不可变**：由下面的触发器保证，只在 MySQL 上。SQLite（单元测试）上没有触发器，服务层按同样的规则写。
- **币种格式**（`^[A-Z]{3}$`）只在应用层校验，数据库不做正则 CHECK（设计 v3：MySQL 的 `REGEXP` 在默认排序规则下不分
  大小写，SQLite 没有 `REGEXP`；与 `provider_price_versions.source_currency` 相同）。

### `fx_rate_locks`（单行锁表）

| 列 | 类型 | 约束 |
| --- | --- | --- |
| `id` | INT | 主键，不自增；迁移写入唯一一行 `id = 1` |

没有业务字段。发布 / 退役对这一行 `SELECT … FOR UPDATE`（主事务的第一条语句），计费对它 `FOR SHARE`（见下「锁顺序」）；
区间不重叠触发器同样先对它 `FOR UPDATE`，把绕过服务的直接写入也串行起来。触发器保证它恰好一行。单元测试的
`Base.metadata.create_all` 同样写入这一行（`after_create` 事件，见 `app/models/fx_rates.py`）。

### `fx_rate_versions`（§74.2）

| 列 | 类型 | 约束 |
| --- | --- | --- |
| `id` | BIGINT | 主键，自增；不出库（T-H 把它写进事件快照 `fx_rate_version_id`，但不对外） |
| `public_id` | CHAR(36) | 非空，唯一（`uq_fx_rate_versions_public_id`） |
| `base_currency` | CHAR(3) | 非空，`CHECK <> 'MYR'` |
| `quote_currency` | CHAR(3) | 非空，`CHECK = 'MYR'`（§17.1） |
| `rate` | DECIMAL(24,10) | 非空，`CHECK > 0` |
| `source` | VARCHAR(16) | 非空，`CHECK IN ('BNM','MANUAL')`（ADR-0005 §1、§2） |
| `source_reference` | VARCHAR(255) | 非空。BNM：`bnm:exchange-rate:<ccy>:<报价日>:session=1200:middle_rate:unit=<n>`；手工：录入者写出处 |
| `source_quote_date` | DATE | 可空；BNM 的报价日（吉隆坡日期），手工录入可空 |
| `observed_at` | DATETIME | 非空。BNM：报价日 12:00 吉隆坡 = 当日 04:00 UTC；手工：录入者给出的观测时刻 |
| `status` | VARCHAR(16) | 非空，`CHECK IN ('DRAFT','PUBLISHED','RETIRED','DISCARDED')`；`DISCARDED` 是 §74.2 之外补的 |
| `effective_from` | DATETIME | 可空；草稿为空。`NULL` 另一种含义：「一直以来」，只可能出现在该币种对时间线为空时的发布 |
| `effective_to` | DATETIME | 可空；`NULL` = 仍生效（或尚未发布） |
| `open_slot` | INT，STORED 生成列 | `CASE WHEN status = 'PUBLISHED' AND effective_to IS NULL THEN 1 END`：未截断的已发布版本为 1，**其余一律为 `NULL`**（不是 0，否则第二条非开放行就撞唯一索引）；只读 |
| `created_by` | BIGINT | 可空（BNM 拉取为空，系统）；外键 → `users(id)` `RESTRICT`（`fk_fx_rate_versions_created_by`） |
| `approved_by` | BIGINT | 可空；发布人；外键 → `users(id)` `RESTRICT`（`fk_fx_rate_versions_approved_by`） |
| `approved_at` | DATETIME | 可空；发布时刻 |
| `created_at` / `updated_at` | DATETIME | 非空 |

- **唯一索引 `(base_currency, quote_currency, open_slot)`**（`ux_fx_rate_versions_open_slot`）：一个币种对至多一个未截断
  的已发布版本。
- **唯一约束 `(base_currency, quote_currency, source, source_quote_date)`**（`uq_fx_rate_versions_quote_date`）：同一报价日
  的 BNM 版本只有一条，并发拉取由它决出一条草稿（输家记 `NO_NEW_QUOTE`）；丢弃的 BNM 草稿也占着这个报价日。约束含
  `source`，所以同日的手工版本不与 BNM 版本冲突；但带报价日的手工版本同样按（币种对、来源、报价日）唯一，两条同日的
  手工版本会冲突。只有报价日为 `NULL` 的行不受限（MySQL 唯一约束不管 `NULL`；设计 §2 的手工录入接口不收报价日，写入的
  就是 `NULL`）。
- 设计 §2 的 CHECK 1–5：
  1. `ck_fx_rate_versions_unpublished`：`DRAFT` / `DISCARDED` 没有区间、没有发布人与发布时刻
  2. `ck_fx_rate_versions_approved`：`PUBLISHED` / `RETIRED` 必有发布人与发布时刻
  3. `ck_fx_rate_versions_period`：区间不倒置；空区间 `[F, F)` 只能出现在 `RETIRED` 行上（撤销的预约、同一秒里发布又退役）
  4. `ck_fx_rate_versions_retired_end`：`status <> 'RETIRED' OR effective_to IS NOT NULL`，停用必有尽头
  5. `ck_fx_rate_versions_bnm_quote_date`：`source <> 'BNM' OR source_quote_date IS NOT NULL`

### `fx_fetch_attempts`（只增）

| 列 | 类型 | 约束 |
| --- | --- | --- |
| `id` | BIGINT | 主键，自增；不出库 |
| `base_currency` | CHAR(3) | 非空 |
| `source` | VARCHAR(16) | 非空，`CHECK = 'BNM'` |
| `requested_date` | DATE | 非空；向 BNM 要的报价日（吉隆坡当天） |
| `outcome` | VARCHAR(32) | 非空，`CHECK IN ('NEW_DRAFT','NO_NEW_QUOTE','NO_QUOTE_FOR_DATE','FAILED')`。`NO_QUOTE_FOR_DATE` 有 17 个字符，所以不是 16（设计 v3）。前三个都是成功 |
| `quote_date` | DATE | 可空；取到报价时的报价日 |
| `error_code` | VARCHAR(64) | 可空；`FAILED` 时 `TIMEOUT` / `HTTP_<status>` / `BAD_PAYLOAD` / `UNIT_NOT_EXACT` / `NETWORK`；`NO_NEW_QUOTE` 时可为 `QUOTE_CHANGED`。不存响应体 |
| `fx_rate_version_id` | BIGINT | 可空；`NEW_DRAFT` 时指向新草稿；外键 → `fx_rate_versions(id)` `RESTRICT`（`fk_fx_fetch_attempts_version`） |
| `attempted_at` | DATETIME | 非空 |

- `ck_fx_fetch_attempts_outcome_columns`（设计 v3，按 `outcome` 的列组合）：`NEW_DRAFT` 有报价日与版本、无错误码；
  `NO_NEW_QUOTE` 有报价日、无版本、错误码为空或 `QUOTE_CHANGED`；`NO_QUOTE_FOR_DATE` 三列都空；`FAILED` 有错误码、无
  报价日与版本。
- 索引 `ix_fx_fetch_attempts_currency_time (base_currency, attempted_at)`：`fx_fetch` 告警维度按币种查最近 72 小时。

### 触发器（迁移 0015 建，只在 MySQL 上）

写法与权限预检照迁移 0006。拒绝一律 `SIGNAL SQLSTATE '45000'`（MySQL 错误号 1644）。

| 触发器 | 做什么 |
| --- | --- |
| `trg_fx_rate_locks_before_insert` | 表里已有一行时拒绝 |
| `trg_fx_rate_locks_before_delete` | 一律拒绝 |
| `trg_fx_rate_locks_before_update` | BEFORE UPDATE（迁移 0016，一律拒绝）：不许把锁行改走（改 `id`、`SET id = 1` 这种不改值的写法都拒绝），否则 ⑤ 的 `WHERE id = 1 FOR UPDATE` 会静默锁不到行（AIH-TASK-045，设计闸门 #200 v1）；错误文本与上两个相同。锁定读不是 UPDATE，不触发 |
| `trg_fx_rate_versions_before_insert` | `status` 不是 `DRAFT` 就拒绝：版本只能经「草稿 → 发布」进入已发布，发布跃迁上的检查因此绕不过去 |
| `trg_fx_rate_versions_before_update` | ① `RETIRED`、`DISCARDED` 的行拒绝任何改动（v2 收紧：`RETIRED` 的尽头也不许改，否则能把已退役版本的尽头清空或改短）；② 草稿只能改成 `DRAFT` / `PUBLISHED` / `DISCARDED`（不许直接 `DRAFT → RETIRED`）；③ `DRAFT → PUBLISHED` 时 `effective_to` 必须为空（新发布的版本永远是时间线末尾、未截断）；④ `PUBLISHED` 的行除 `effective_to`、`status`（只许 `PUBLISHED → RETIRED`）与 `updated_at` 外任何列变化都拒绝；⑤ 新状态是 `PUBLISHED` / `RETIRED` 且区间或状态有变化时：先 `SELECT … FROM fx_rate_locks WHERE id = 1 FOR UPDATE`，本行为空区间时到此为止，否则查同一币种对里是否有另一个**非空区间**的 `PUBLISHED` / `RETIRED` 版本与本行相交，有就拒绝 |
| `trg_fx_rate_versions_before_delete` | 一律拒绝 |
| `trg_fx_fetch_attempts_before_update` / `_before_delete` | 一律拒绝（只增） |

③ 加 ⑤ 使**起点为空的版本只可能在该币种对的时间线为空时产生**（起点、尽头都为空的区间与任何非空区间都相交）：直接写库
无法在已有版本之后发布一个「一直以来」的版本，也无法把版本插进时间线中间或退役留下的空档。⚠️ MySQL 的触发器不能用
`NEW` / `OLD` 引用生成列，所以 ④ 不比 `open_slot`（它只由被比较的列算出）。锁的结果用局部变量接（设计写的是 `@lock_id`
会话变量），语义相同、不污染会话。**数据库不拦的**（设计 §2）：直接写库写入一个过去的**具体**起点 —— 拦它要比较应用
时钟与数据库时钟，两者不同源，`t` 只由服务层在锁后取。**残余风险**：`TRUNCATE` / `DROP` 是 DDL，不经触发器（与 0006
相同）。

### 锁顺序与写入顺序

- **发布、退役**（AIH-TASK-039）：事务的第一条语句是 `SELECT … FROM fx_rate_locks WHERE id = 1 FOR UPDATE`，拿到锁之后
  才取 `t`，之后全是加锁读。发布路径只拿这一把锁，不形成环。
- **计费**（T-G / T-H 在计费事务里调用）：**供应商（S）→ FX（S）→ 定价规则（S）→ 钱包 → 租户**。`resolve_model` 对供应商
  行 `FOR SHARE`；之后对 `fx_rate_locks` 取 `FOR SHARE`、`resolve_fx_rate` 加锁读，再对 `pricing_rule_locks` 取
  `FOR SHARE`；都持有到计费事务提交。共享锁之间不冲突，计费事务彼此不阻塞；只有低频的发布 / 退役会让计费等一下。
- **建草稿、改草稿、丢弃**：不拿全局锁（草稿不参与计费），只 `FOR UPDATE` 版本行。
- **写入顺序**（区间触发器与 `open_slot` 唯一索引按行检查，服务层每一步立刻 flush）：发布时先截断前一个（写
  `effective_to`），再把本行从草稿改为已发布；撤销预约时先把被撤销的一行改成空区间的 `RETIRED`，再把前一个恢复为
  未截断（只有它仍是 `PUBLISHED`、尽头等于被撤销行的起点时）。

---

## 用量事件（spec §23、§79、§83；AIH-TASK-029，迁移 `0017_usage_events`）

设计依据：[design/AIH-TASK-029-usage-ingest.md](design/AIH-TASK-029-usage-ingest.md)（设计闸门 #176 v8）§2「数据库」与
§8。两张表：`usage_events`、`usage_event_conflicts`；另给两张已有表各加一个唯一约束（只新增索引，不改列）：

- `integration_credentials`：`uq_integration_credentials_id_scope (id, project_id, tenant_id)`；
- `usage_meter_types`：`uq_usage_meter_types_id_shape_unit_kind (id, payload_shape, unit, quantity_kind)`。

`id` 本来就唯一，这两个约束只为让下面的复合外键成立（先例：`uq_projects_id_tenant`）。

共同的约定：

- **本任务只写 `RECEIVED`**，不计费、不入队；计价快照列（价格版本、规则、FX、成本、计费额）由计费任务（T-H）的迁移加。
- **行永不删除**：外键都是 `RESTRICT`；冲突表由触发器拒绝 UPDATE / DELETE（只在 MySQL 上）。
- **必需列逐列 `NOT NULL`**（设计 v6）：MySQL 对任一组成列为 `NULL` 的外键不做检查、对结果为 UNKNOWN 的 CHECK 放行，
  所以复合外键与形态 CHECK 只有在组成列非空时才真正生效。

### `usage_events`（§79）

| 列 | 类型 | 约束 |
| --- | --- | --- |
| `id` | BIGINT | 主键，自增；不出库 |
| `public_id` | CHAR(36) | 非空，唯一（`uq_usage_events_public_id`）；管理端以后用，冲突审计的 `entity_id` |
| `event_id` | VARCHAR(64) `utf8mb4_0900_bin` | 非空，**全局唯一**（`uq_usage_events_event_id`，§23 的最终仲裁）。UUIDv7 或 ULID 的规范写法（应用层校验） |
| `schema_version` | VARCHAR(8) | 非空 |
| `tenant_id` / `project_id` | BIGINT | 非空；复合外键 `(project_id, tenant_id)` → `projects(id, tenant_id)` `RESTRICT`（`fk_usage_events_project`） |
| `integration_credential_id` | BIGINT | 非空；接收时用的凭据行（含版本）。复合外键 `(integration_credential_id, project_id, tenant_id)` → `integration_credentials(id, project_id, tenant_id)` `RESTRICT`（`fk_usage_events_credential`）：**事件的租户与项目由数据库保证就是凭据所属的租户与项目**（INV-8） |
| `request_id` | VARCHAR(128) `utf8mb4_0900_bin` | 非空；AI 调用的 id |
| `conversation_id` | VARCHAR(128) `utf8mb4_0900_bin` | 可空 |
| `provider_code_raw` | VARCHAR(64) `utf8mb4_0900_bin` | 非空；上报的原始字符串 |
| `model_code_raw` | VARCHAR(128) `utf8mb4_0900_bin` | 非空；同上 |
| `provider_id` / `model_id` | BIGINT | 可空；§79 的目录引用，摄取时为空，计费事务按 `occurred_at` 解析后写入（未知模型保持为空）。复合外键 `(model_id, provider_id)` → `ai_models(id, provider_id)`（`fk_usage_events_model`）与单列 `provider_id` → `ai_providers(id)`（`fk_usage_events_provider`），都是 `RESTRICT` |
| `usage_meter_type_id` | BIGINT | 非空；接收时按 `usage_type` 查到的计量类型 |
| `payload_shape` | VARCHAR(32) | 非空；与下面两列一起组成复合外键 `(usage_meter_type_id, payload_shape, unit, quantity_kind)` → `usage_meter_types(id, payload_shape, unit, quantity_kind)` `RESTRICT`（`fk_usage_events_meter_type`）：形态、单位、数量类型**由数据库保证**与所属计量类型一致 |
| `quantity_kind` | VARCHAR(16) | 非空；复合外键的一部分 |
| `input_tokens` / `output_tokens` / `cache_creation_input_tokens` / `cache_read_input_tokens` | BIGINT | 可空；各自 `CHECK IS NULL OR >= 0`。列名与载荷字段、与 `usage_meter_components.quantity_field` 一致（见下「取名」） |
| `quantity` | DECIMAL(20,8) | 可空；`CHECK IS NULL OR >= 0`。用量不是金额，但同一精度、不经过浮点 |
| `unit` | VARCHAR(16) `utf8mb4_0900_bin` | 非空；复合外键的一部分。`QUANTITY` 形态即上报的 `unit`，`LLM_TOKEN_FIELDS` 形态是类型的单位 `TOKEN` |
| `payload_fingerprint` | CHAR(64) | 非空；规范化载荷的 SHA-256 |
| `status` | VARCHAR(32) | 非空，默认 `RECEIVED`；`CHECK` §83 的九个取值。`IDEMPOTENCY_CONFLICT` 留在取值里但不写：冲突是请求的结果，不是原事件的状态 |
| `error_code` / `error_message` | VARCHAR(64) / VARCHAR(255) | 可空；计费侧写 |
| `occurred_at` | DATETIME(6) | 非空；保留微秒（别名与版本边界按 `[from, to)` 比较） |
| `received_at` / `created_at` | DATETIME | 非空；整秒 |
| `processed_at` | DATETIME | 可空；计费侧写 |

- **形态 CHECK**（`ck_usage_events_shape_fields`，设计 v2，显式写出两组条件、不用链式比较）：`LLM_TOKEN_FIELDS` 四个
  token 列都非空且 `quantity` 为空；`QUANTITY` 四个 token 列都为空且 `quantity` 非空。交错的空值（`1, NULL, 1, NULL`
  之类）因此进不来。
- `ck_usage_events_integer_quantity`：`quantity_kind <> 'INTEGER' OR quantity IS NULL OR quantity = FLOOR(quantity)`。
  ⚠️ 只在 MySQL 上建（模型用 `ddl_if`）：单元测试的 SQLite 不一定编译了 `FLOOR`，服务层同样校验。
- `ck_usage_events_catalog_reference`：`model_id IS NULL OR provider_id IS NOT NULL`。
- 索引：`ix_usage_events_status_occurred (status, occurred_at)`（计费侧扫描 `RECEIVED`）、`tenant_id`、
  `ix_usage_events_project_id (project_id, tenant_id)`、`ix_usage_events_model_id (model_id, provider_id)`、
  `provider_id`、`conversation_id`、`request_id`、`occurred_at`，以及两个复合外键在子表上的索引
  `ix_usage_events_credential`、`ix_usage_events_meter_type`。`project_id` 与 `model_id` 的索引取复合外键需要的形式
  （首列就是该列），一个索引同时服务查询与外键，不让 MySQL 再自动建一个。
- **取名**：spec §79 写的是 `cache_creation_tokens` / `cache_read_tokens`；§74 允许按仓库约定命名，这里取与上报载荷、
  与 AIH-TASK-025 的 `quantity_field` 一致的 `cache_creation_input_tokens` / `cache_read_input_tokens`，免得三处各叫
  一个名。

### `usage_event_conflicts`（只增）

| 列 | 类型 | 约束 |
| --- | --- | --- |
| `id` | BIGINT | 主键，自增 |
| `usage_event_id` | BIGINT | 非空；被撞的原事件，外键 → `usage_events(id)` `RESTRICT`（`fk_usage_event_conflicts_event`） |
| `event_id` | VARCHAR(64) `utf8mb4_0900_bin` | 非空 |
| `integration_credential_id` | BIGINT | 非空；冲突请求用的凭据行，外键 → `integration_credentials(id)` `RESTRICT`（`fk_usage_event_conflicts_credential`） |
| `api_key` | VARCHAR(64) `utf8mb4_0900_bin` | 非空；冲突请求的 `public_api_key` |
| `payload_fingerprint` | CHAR(64) | 非空；冲突请求的指纹 |
| `mismatch` | VARCHAR(32) | 非空，`CHECK IN ('OWNERSHIP','FINGERPRINT','BOTH')` |
| `received_at` | DATETIME | 非空 |

- **所有列 `NOT NULL`**：去重唯一约束遇到 `NULL` 会失效。
- 唯一约束 `uq_usage_event_conflicts_dedupe (event_id, api_key, payload_fingerprint)`：同一冲突只记一次，客户端反复
  重试同一个冲突请求不会刷爆这张表；撞了它的那次不写审计。
- 告警维度 `usage_event_conflicts` 按 `received_at` 数最近 24 小时的行（[api.md](api.md#内部告警接口)）。

### 触发器（迁移 0017 建，只在 MySQL 上）

写法与权限预检照迁移 0010（`audit_logs`）。拒绝一律 `SIGNAL SQLSTATE '45000'`（MySQL 错误号 1644）。

| 触发器 | 做什么 |
| --- | --- |
| `trg_usage_event_conflicts_before_update` | 一律拒绝（只增） |
| `trg_usage_event_conflicts_before_delete` | 一律拒绝（只增） |

**残余风险**：`TRUNCATE` / `DROP` 是 DDL，不经触发器（与 0006 / 0010 相同）。

### 写入顺序与事务

- 新事件：一个事务 `INSERT usage_events` → commit，之后才回 202。插入优先，不先查；撞 `uq_usage_events_event_id` 时
  回滚，新事务读已有行比对归属（租户、项目、`api_key`）与指纹。MySQL 上撞唯一约束的 INSERT 会等先到的事务提交或回滚，
  所以读到的一定是已提交的行。
- 冲突：冲突行与审计同一事务；撞去重约束（并发的同一冲突）时整个事务回滚、不写第二条审计。
- 不加任何行锁。

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

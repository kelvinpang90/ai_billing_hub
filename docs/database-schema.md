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
- **金额**：一律 `Money`（`DECIMAL(20,8)`，`app/models/base.py`）。下面两张表目前都不含金额列

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
| `created_at` | DATETIME | 非空 |
| `updated_at` | DATETIME | 非空 |

`contact_name` / `email` / `phone` 是个人数据（`REQ-PRIV-001`）：不进日志、不进异常消息。

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

## 尚未建的列

AIH-TASK-004 只建两张表的身份与归属字段。下面这些 spec 列刻意留给后续任务，届时都是**纯新增列**：

| 表 | 列 | 留给谁 | 为什么现在不建 |
| --- | --- | --- | --- |
| `tenants` | `account_status`、`billing_status`、`status_version` | 状态模型任务（走设计闸门） | §24 状态机；碰状态机按 [WORKFLOW §3](WORKFLOW.md) 必须过设计闸门，且应与状态迁移逻辑一起设计 |
| `tenants` | `low_balance_threshold`、`currency` | 钱包任务 | 金额与币种语义归钱包 |
| `projects` | `integration_status` | 状态模型任务（走设计闸门） | 同上，§24 |
| `projects` | `status_webhook_url`、`encrypted_webhook_secret`、`webhook_key_version` | Webhook 任务 | 密钥的 schema 还在等 [ADR-0004](adr/ADR-0004-credential-encryption.md) 第 4a 节二选一 |
| `projects` | `backend_base_url` | Phase 3 | 应用后端集成（§35–§39） |

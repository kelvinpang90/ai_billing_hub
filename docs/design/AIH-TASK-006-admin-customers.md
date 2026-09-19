# AIH-TASK-006 设计：管理端客户管理（已批准 v3）

> **来源**：设计闸门 Issue #96。本文件是 `APPROVED: design v3` 那一版正文的**逐字副本**，审查者为独立、只读的 Claude Code 会话（claude-opus-5），因为 Codex 额度不可用，临时替代。
> 放进仓库，是因为 OpenClaw Worker 在沙箱里不联网、读不到 GitHub Issue。
> **实现以本文件为准**；与 Issue 不一致时，以 Issue #96 上被批准的 v3 为准。设计要改，就回到 Issue 升版本、重新过闸门，不要直接改本文件。

---

## 0. 准入判定

| 任务涉及 | 要填的范围 |
| --- | --- |
| 钱包 / 账本 / 定价 / 汇率 / 支付 / 幂等 / 状态机 | **全部章节** |
| 用量摄取 / 认证与会话（集成侧与平台侧）/ Webhook | §1–§7（**§6 不可省**） |
| 前端 / 文档 / CI / 脚本 | 不走本闸门 |
| 既不碰钱、也不碰用量摄取 / 认证与会话 / Webhook 的后端改动 | 不走本闸门 |

本任务档位：`全部`。本任务在建客户的同一事务里建钱包（钱包主题），还新增管理员鉴权（认证主题），按主题取最高档。

---

状态：`READY_FOR_REVIEW`
负责人：Claude
**设计版本**：`v3`
对应需求：spec §124（Phase 1：Admin customer management、Tenant、Project、Audit Log）、§56、§57、§66、§75、§76、§89、§96、§97、§107、§108、§115、§132；`REQ-PRIV-001`
目标 PR：待开（由 OpenClaw Worker 执行 `AIH-TASK-006`）

## 1. 目标与边界

- **要解决的问题**：Phase 1 的验收写着「管理员建客户 → 自动有钱包 → 建项目 → 所有动作都有审计」（§124）。数据层已经有了：`tenants` / `projects`（AIH-TASK-004）、`wallets` / 账本（AIH-TASK-005）。但目前没有服务层，也没有任何管理端接口。另外，现有代码只校验「令牌有效」，没有任何地方校验调用者是不是管理员。
- **可观察的完成标准**：
  1. 管理员调 `POST /api/v1/admin/customers`，得到一个新客户。库里同时多了：
     - 一行 `tenants`：`billing_status = SUSPENDED`、`status_version = 0`；
     - 一个 MYR 钱包：余额 0、版本 0；
     - 一条审计 `CUSTOMER_CREATE`。

     这三行同一事务提交，要么全在，要么全不在。
  2. 管理员能分页列出客户，能按 `public_id` 查看单个客户：客户字段、计费状态、钱包币种、余额与版本。
  3. 管理员能给某个客户建项目：一行 `projects`，加一条审计 `PROJECT_CREATE`，同一事务。管理员也能分页列出该客户的项目。
  4. 没有令牌或令牌无效返回 401；CUSTOMER 角色调这些接口返回 403；任何响应都不含内部自增 id。
  5. `docs/api.md` 记下这五个接口的请求、响应与错误契约。
- **明确不做什么**：
  - 编辑客户（`PATCH`）、账户生命周期 `account_status`、低余额阈值配置：留给状态模型任务（§24），它另过设计闸门；
  - 管理员调账接口：下一个任务。本任务不调 `post_transaction`，不产生任何账本行；
  - API 凭据、出站 webhook 与其密钥、`backend_base_url`、`integration_status`：各自的任务；
  - 前端页面：另开任务。本任务没有 UI，所以 §132 的「前端加载 / 错误状态」一项不适用；
  - 删除或停用客户与项目：不提供，财务记录永久保留（外键 RESTRICT）。
- **现有行为与问题证据**：
  - `app/api/` 只有 `auth.py`、`two_factor.py`、`health.py`；
  - 全仓库没有 `Depends(` 或角色校验，`UserRole.ADMIN` 只在登录时用来强制 2FA；
  - `create_wallet` 的文档说明要求「与建客户同一事务」调用，但没有调用方；
  - `AuditAction` 没有 `CUSTOMER_CREATE`，它的文档说明要求「在实现的任务里加」。

## 2. 设计概要

```text
HTTP 请求 → require_admin（验签 → 读库确认 ACTIVE 且角色 ADMIN）→ Pydantic 校验
  → services.customers（session_scope：一个事务）
      建客户：create_tenant → create_wallet → record_audit(CUSTOMER_CREATE) → commit
      建项目：按 public_id 找租户 → create_project → record_audit(PROJECT_CREATE) → commit
      读：按 public_id 找租户 / 分页查询（只读，不写审计）
  → 响应模型（只含对外字段）→ success() 信封
```

### 接口（都在 `/api/v1/admin`，都要 ADMIN）

| 方法与路径 | 请求 | 成功 | 错误 |
| --- | --- | --- | --- |
| `POST /customers` | `{company_name, email, contact_name?, phone?}` | 201，客户详情 | 401 / 403 / 422 / 503 |
| `GET /customers?page=&page_size=` | 查询参数 | 200，`{items, page, page_size, total}` | 401 / 403 / 422 / 503 |
| `GET /customers/{customer_id}` | 路径里是客户 `public_id` | 200，客户详情 | 401 / 403 / 404 / 503 |
| `POST /customers/{customer_id}/projects` | `{name, description?}` | 201，项目 | 401 / 403 / 404 / 422 / 503 |
| `GET /customers/{customer_id}/projects?page=&page_size=` | 查询参数 | 200，分页项目 | 401 / 403 / 404 / 422 / 503 |

- **信封**：沿用 `success()` / `failure()`（§107）。错误码：
  - 401：`TOKEN_INVALID`（沿用 `InvalidToken`）；
  - 403：新增 `ADMIN_REQUIRED`；
  - 404：新增 `CUSTOMER_NOT_FOUND`；
  - 422：`VALIDATION_ERROR`（沿用）；
  - 503：`DATABASE_NOT_CONFIGURED`（沿用）。
- **客户详情**：
  - 字段：`id`（= `public_id`）、`company_name`、`contact_name`、`email`、`phone`、`billing_status`、`status_version`、`created_at`、`updated_at`；
  - 另附 `wallet: {currency, balance, version}`。`balance` 是字符串，先把 `Decimal` `quantize` 到 8 位小数再用 `format(value, "f")` 格式化，所以恰好 8 位小数（如 `"0.00000000"`）。刚建好的钱包在内存里是 `Decimal(0)`，不先 quantize 会输出 `"0"`，与从库里读回的 `"0.00000000"` 不一致。永远不转成浮点数；
  - 不返回内部自增 id、`low_balance_threshold`（本任务不配置它）以及任何成本或毛利。
- **列表项**：客户详情去掉 `wallet`。项目字段：`id`（= `public_id`）、`name`、`description`、`created_at`、`updated_at`。
- **分页**（§108）：
  - `page` 取 1–10000，默认 1；`page_size` 取 1–100，默认 20；超出范围是 422，不静默截断（`page` 的上限防止 OFFSET 溢出变成 500）；
  - 客户按 `id` 倒序（最新在前），项目按 `id` 正序；
  - `total` 是总条数，数据量小，一条 `COUNT` 即可。
- **校验**：
  - `company_name` 与 `name` 去掉首尾空白后，长度在 1–255 之间；
  - `email` 用 `EmailStr`，长度不超过 320；
  - `contact_name` 不超过 255，`phone` 不超过 32，`description` 不超过 1000，都可空；空串一律存成 NULL；
  - 多余的字段拒绝（`extra="forbid"`）。客户不能通过请求体指定 `public_id`、`billing_status`、余额或任何 id。
- **数据库**：不加表、不加列、不加迁移。
  - `AuditAction` 加两个值 `CUSTOMER_CREATE`、`PROJECT_CREATE`。该列是非原生枚举、宽度 64，不需要 ALTER；
  - repository 新增两个函数：`list_tenants(session, *, offset, limit)` 返回 `(rows, total)`；`list_projects_for_tenant` 加可选的 `offset` / `limit`，不传时行为不变。
- **事务边界**：每个写接口一个 `session_scope()`，也就是 `app/core/database.py` 里现成的「成功提交、异常回滚」。repository 仍然只 flush 不 commit。
- **异常不带 SQL 参数**：`create_database_engine` 给 `create_engine` 加上 `hide_parameters=True`。现在的全局异常处理器用 `logger.exception` 记下完整异常文本（`app/core/errors.py`、`app/core/logging.py`），而 SQLAlchemy 的异常文本默认带 `[parameters: …]`。本任务是第一条把请求里的 email、contact_name、phone 写进库的路径，不加这一条，一次数据库瞬时故障就会把这三项个人数据写进应用日志。改在引擎上而不是只在本任务的服务里捕获，是因为它对所有经这个引擎的语句都成立，以后新增的写路径不会再漏。
- **外部系统 / 异步**：无。不发出站事件，不入队。
- **时间**：`created_at` / `updated_at` 用 `utc_now()`（naive UTC，§109）。客户、钱包、审计三行用同一个 `now`。
- **金额**：只读出、不计算。余额按 `Decimal` 格式化成 8 位小数字符串，不做舍入以外的任何运算。

### 鉴权：`require_admin(request) -> User`

放在 `app/api/auth.py`，紧挨 `require_current_user`。

- 先调 `require_current_user`：验签、确认是访问令牌、读库、确认 `ACTIVE`。
- 再按**数据库里的**角色判断：不是 `ADMIN` 就抛 `ADMIN_REQUIRED`（403）。令牌里的 `role` claim 不被信任，这样降权即时生效。
- ADMIN 只有在通过 2FA 之后才拿得到访问令牌（`services/auth.py` 的登录流程），所以这里不再重复检查 2FA。

## 3. 不变量影响矩阵

| 不变量 | 是否触碰 | 可能怎样被破坏 | 设计控制 | 验证方式 |
| --- | --- | --- | --- | --- |
| INV-1 中心故障不中断 AI | 否 | 本任务只加管理端接口，不在用量摄取路径上 | — | — |
| INV-2 事件不重复扣费 | 否 | 不处理用量事件 | — | — |
| INV-3 支付不重复入账 | 否 | 不处理支付 | — | — |
| INV-4 余额只经账本变动 | 是（只读 + 建空钱包） | 建钱包时带入非零余额 | 只调 `create_wallet`（余额 0、版本 0），迁移 0006 的 `BEFORE INSERT` 触发器也拒绝非零；本任务没有任何余额写入路径 | MySQL 用例：新钱包余额 0、版本 0；仓库里没有 `UPDATE wallets` |
| INV-5 历史账本不可变 | 否 | 不写账本 | — | — |
| INV-6 事件保留版本引用 | 否 | 无用量事件 | — | — |
| INV-7 客户不可见成本毛利 | 是 | 响应里带出内部字段 | 响应模型独立定义、字段白名单，从不序列化 ORM 对象；管理端响应目前也没有成本字段 | 用例断言响应键集合恰好等于白名单 |
| INV-8 租户不可互访 | 是 | ① CUSTOMER 角色或匿名调管理端接口，包括某个处理函数漏调 `require_admin`；② 猜测或枚举内部 id；③ 项目挂到别的客户下 | ① `require_admin` 读库校验角色，403；② 路径只收 `public_id`（uuid4），响应从不含内部 id，不存在即 404；③ 项目的 `tenant_id` 只来自按路径 `public_id` 解析出的租户，不来自请求体 | 用例：从 `app.routes` 枚举**所有** `/api/v1/admin` 前缀路由，逐个断言无令牌 401、CUSTOMER 令牌 403、不写库，并断言枚举结果恰好是这五个接口；响应无内部 id；请求体带 `tenant_id` 被 422 拒绝；项目只出现在所属客户下 |
| INV-9 对话内容不入库 | 否 | 不处理 prompt / response | — | — |
| INV-10 金额用 Decimal | 是（只读） | 余额被转成浮点数输出 | 余额用 `Decimal` 格式化成 8 位小数字符串，schema 里是 `str` | 用例断言 `balance == "0.00000000"` 且类型是字符串 |
| INV-11 event_id 至多一次财务效果 | 否 | 没有财务效果 | — | — |
| INV-12 定稿对账单不可变 | 否 | 无对账单 | — | — |
| INV-13 状态与事件原子提交 | 是 | 建了客户却没有钱包或审计，或反过来 | 客户、钱包、审计同一 `session_scope`，任何一步异常整体回滚；项目与审计同理 | 用例：让钱包插入失败，断言没有客户行、没有审计行；MySQL 上走真实触发器 |
| INV-14 队列丢失不毁持久工作 | 否 | 不用队列 | — | — |

## 4. 状态与并发

- **状态**：本任务不改变任何状态。新客户的 `billing_status` 取表的默认值 `SUSPENDED`（余额 0 不可用），`status_version = 0`。不写跃迁审计，也不发 `tenant.billing_status_changed`，因为这是初始值，不是跃迁。
- **并发**：只有插入。`wallets.tenant_id` 唯一约束保证一个客户最多一个钱包；两个并发的建客户请求各建各的租户，互不影响。
- **唯一性**：`public_id` 唯一（uuid4），`email` 按 §75 **不唯一**，同一联系人可以对应多个客户。
- **幂等**：`POST /customers` 与 `POST .../projects` 不设幂等键。重复提交会得到两个客户或两个项目。理由：
  - 它们不产生任何财务效果（钱包余额为 0）；
  - 管理员在列表里能看见；
  - 重复客户的停用由后续的账户状态任务提供。

  这是明确接受的风险，写在 §10。
- **相同 ID、不同载荷**：不适用，客户端不指定 id。
- **重试层**：不自动重试。失败即回滚，由调用方决定是否重发。
- **必须原子**：客户、钱包、`CUSTOMER_CREATE` 三行；项目与 `PROJECT_CREATE` 两行。

## 5. 失败与恢复矩阵

| 失败点 | 对外结果 | 数据最终状态 | 是否可重试 | 恢复来源 | 告警 |
| --- | --- | --- | --- | --- | --- |
| 校验失败 | 422 `VALIDATION_ERROR`，只列字段名 | 没有任何写入 | 修正后重试 | — | 无 |
| 无令牌、令牌无效、账号停用 | 401 `TOKEN_INVALID` | 没有任何写入 | 重新登录 | — | 无 |
| 非 ADMIN | 403 `ADMIN_REQUIRED` | 没有任何写入 | 否 | — | 无 |
| 客户不存在 | 404 `CUSTOMER_NOT_FOUND` | 没有任何写入 | 否 | — | 无 |
| 建钱包失败（约束或触发器拒绝） | 500 `INTERNAL_ERROR`（不暴露细节） | 整个事务回滚：没有客户、钱包或审计 | 是 | 重发请求 | 全局处理器记异常与 request_id；引擎的 `hide_parameters=True` 保证异常文本里没有 SQL 参数，所以不含个人数据 |
| 写审计失败 | 500 | 整个事务回滚 | 是 | 重发请求 | 同上 |
| DB 提交失败 | 500 | 事务回滚，数据库保证不留半截 | 是 | 重发请求 | 同上 |
| 数据库未配置 | 503 `DATABASE_NOT_CONFIGURED` | 没有任何写入 | 配好后重试 | 部署配置 | 已有的 `/readyz` 会报 |
| Redis/Celery 丢失 | 不影响：本任务不用 | — | — | — | — |
| 外部服务超时 | 不影响：本任务不调外部服务 | — | — | — | — |
| 重复请求 | 201，得到第二个客户或项目 | 两条独立记录，各自完整 | — | 管理员识别；停用由后续任务提供 | 无 |

## 6. 数据与安全边界

- **租户过滤**：
  - 管理端按角色跨租户，这是 §56 要求的；
  - 按客户的读写都从路径 `public_id` 解析出租户，项目的读写再用解析出的内部 `tenant_id` 过滤（`get_project_for_tenant` / `list_projects_for_tenant` 已经这样做）；
  - 内部 id 从不出现在请求或响应里。
- **鉴权主体**：只从已验签的访问令牌里取，角色以数据库为准。只有 ADMIN 能调这五个接口，CUSTOMER 一律 403。
- **CSRF**（§96）：访问令牌走 `Authorization: Bearer` 头，前端只放在内存里。浏览器不会跨站自动附带这个头，所以这些接口不受 CSRF 影响，不另加 CSRF 令牌。刷新令牌 cookie 只作用于 `/api/v1/auth`（SameSite=strict），碰不到管理端接口。
- **客户侧禁止返回的字段**：本任务没有客户侧接口。管理端响应同样只返回白名单字段，不返回内部 id。
- **日志**：
  - 这一层不记请求体，也不记 `email` / `contact_name` / `phone`（`REQ-PRIV-001`，与 tenancy repository 的约定一致）；
  - 意外异常由现有的全局处理器用 `logger.exception` 记下（含异常文本与 request_id），对外不暴露栈（§107）。数据库异常的文本默认带 SQL 参数，所以引擎统一设 `hide_parameters=True`，见 §2。这样即使 INSERT 在数据库层失败，日志里也只有语句与异常类型，没有参数值。
- **审计**：
  - `CUSTOMER_CREATE`：actor 是管理员，`entity_type = "tenant"`，`entity_id = public_id`。`after_state` 只含 `public_id`、`company_name`、`billing_status`、`wallet_currency`，**不含** `email`、`contact_name`、`phone`（个人数据不进长期保留的审计表）；
  - `PROJECT_CREATE`：`entity_type = "project"`，`entity_id` 是项目的 `public_id`；`after_state` 含 `public_id`、`name` 与所属客户的 `public_id`；
  - 都带 ip 与 user agent（沿用 `request_context`）。

  §66 的动作清单里只有 `PROJECT_UPDATE`，没有 `PROJECT_CREATE`。本任务按 §124「所有动作都有审计」补上它，偏离点记在 TODO。
- **密钥**：本任务不读写任何密钥。
- **prompt / response 或客户数据**：不处理对话内容。客户的联系信息按上面的规则只存业务表，不进日志和审计。
- **数据保留与删除**：不提供删除。客户、项目、钱包之间的外键都是 RESTRICT。

## 7. 测试证据计划

| 风险/需求 | 测试层级 | 场景 | 预期结果 |
| --- | --- | --- | --- |
| 正常路径：建客户 | API（SQLite） | ADMIN 调 `POST /customers` | 201；响应键集合等于白名单；`billing_status = SUSPENDED`；`wallet = {currency: MYR, balance: "0.00000000", version: 0}`；库里恰好一行客户、一行钱包、一条 `CUSTOMER_CREATE`，审计 `after_state` 不含 email / contact / phone |
| 正常路径：原子性与真实触发器 | 服务（MySQL） | 在迁移 0006 的库上调 `create_customer` | 三行都在；钱包余额 0、版本 0；`verify_wallet` 返回空列表 |
| 事务中途失败 | 服务（SQLite 与 MySQL） | 让 `create_wallet` 抛异常（或写审计时抛异常） | 没有客户行、钱包行或审计行 |
| 鉴权：每个接口 | API | 从 `create_app()` 的 `app.routes` 枚举所有路径以 `/api/v1/admin` 开头的路由，按「方法 + 路径」参数化（路径参数填一个真实客户的 `public_id`）：无令牌、CUSTOMER 令牌各调一次 | 每个都是 401 / 403 `ADMIN_REQUIRED`，且调用前后 `tenants`、`projects`、`wallets`、`audit_logs` 行数不变；另断言枚举到的「方法 + 路径」集合恰好等于 §2 的五个——既防枚举落空，新增管理端接口时也会被迫补进这个测试 |
| 鉴权：令牌种类 | API | 以 `GET /customers` 为代表：坏令牌 / pending 令牌 / 停用账号 / ADMIN 令牌 | 401 / 401 / 401 / 200 |
| 降权即时生效 | API | ADMIN 拿到令牌后把库里的角色改成 CUSTOMER，再调接口 | 403 |
| 边界值 | API | `company_name` 为空白、长 256、恰好 255；`page_size` 为 0、101、100；`page` 为 0、10001、10000；多余字段 `tenant_id` / `billing_status` | 422 / 422 / 201；422 / 422 / 200；422 / 422 / 200；422 |
| 分页 | API | 建 25 个客户，查第 1、2 页（每页 20） | 20 + 5 条，最新在前，`total = 25`；超出末页返回空 `items` |
| 租户越权 | API | 客户 A 下的项目，用客户 B 的路径去列 | 只列出 B 自己的项目；不存在的 `public_id` 得到 404，与别人的 id 无法区分 |
| 金额精度 | API | `POST /customers` 的响应与随后 `GET /customers/{id}` 的响应里的余额 | 两处都是字符串 `"0.00000000"`，不是数字，也不是 `"0"` |
| 幂等重放 | API | 同一请求体 POST 两次 | 两个不同 `public_id`，各有钱包（按 §4 接受的行为） |
| 并发 | — | 只有插入、没有读改写，并发不会互相覆盖；钱包唯一约束已由 AIH-TASK-005 的 MySQL 用例覆盖 | 不单独测 |
| 日志不含个人数据（`REQ-PRIV-001`） | API（SQLite） | 用 `create_database_engine` 建一个 SQLite 文件库的引擎（与生产同一个工厂），建表后删掉 `tenants` 表，用一组有辨识度的 email、contact_name、phone 调 `POST /customers`，抓取这次请求的全部日志输出 | 500 `INTERNAL_ERROR`；日志里有这次的异常，但不含请求里的 email、contact_name、phone 中的任何一个；库里没有新行 |
| 引擎配置 | unit | `create_database_engine` 建出的引擎 | `engine.hide_parameters` 为真 |
| Redis/Celery 丢失 | — | 本任务不用 | 不适用 |

## 8. 迁移与上线

- **数据迁移**：无。`AuditAction` 新增的两个值宽度都在 64 以内，列是非原生枚举。
- **锁表与性能**：无 DDL。列表查询带 `LIMIT` / `OFFSET` 与一条 `COUNT`，数据量是个位数到百级。
- **兼容窗口**：只加接口，不改现有接口。
- **部署顺序**：合并即部署，与往常一样。没有前置配置。
- **回滚**：回退合并提交即可。已建的客户、钱包、审计是合法数据，保留。
- **部分部署**：不涉及多组件。
- **监控**：沿用 `/healthz`、`/readyz` 与应用日志。上线后在生产上手工建一个测试客户验证（部署后核对项，不写进代码）。

## 9. 方案取舍

| 方案 | 优点 | 风险 | 未采用原因 |
| --- | --- | --- | --- |
| 采用：服务层用 `session_scope` 一次提交客户、钱包、审计；鉴权用在处理函数里显式调用的 `require_admin` | 与现有 `require_current_user` 的调用方式一致；事务边界一眼可见 | 每个处理函数都要记得调用。由 §7 的路由枚举用例兜底：任何 `/api/v1/admin` 接口漏调，匿名与 CUSTOMER 两个断言都会失败 | — |
| FastAPI `Depends(require_admin)` 挂在路由器上 | 不会漏调用 | 仓库里还没有 `Depends` 惯例，引入新模式要同时改风格 | 一致性优先。改用路由器依赖可以作为独立的重构任务，那时所有接口一起改 |
| 建客户时发 `tenant.billing_status_changed` 事件 | 下游一开始就知道状态 | 初始值不是跃迁，发了会让「跃迁审计与事件一一对应」的规则出例外 | 不发。状态模型任务再定义「初始状态通知」 |
| 给 POST 加幂等键 | 防重复提交 | 要设计键的存储与过期；这里没有财务效果 | 见 §4，作为明确接受的风险 |

## 10. 未决问题与假设

- **未决问题**：无阻断项。
- **假设**：
  1. 管理员数量少、客户与项目是百级。`OFFSET` 分页足够，§108 允许「小数据集用传统分页」；
  2. `PROJECT_CREATE` 不在 §66 清单里，按 §124 补上。它只是审计动作名，不影响外部契约。
- **需要谁拍板**：无。重复提交不设幂等键这条已在 §4 说明理由，Kelvin 如有异议可以在审查时提出。
- **如果假设 1 错了**：列表会变慢，但不影响正确性。改成游标分页只改列表接口的参数，不改数据。

## 11. 审查与版本绑定

审查方要回答的五个问题见 [scripts/review_checklist.md](../../scripts/review_checklist.md) 的「设计审查」一节。

**版本绑定规则**（出处是 [WORKFLOW §3](../../docs/WORKFLOW.md)）：

- 批准必须写成 `APPROVED: design v<N>`，`<N>` 取本 Issue 顶部的「设计版本」
- **设计版本一变，之前的 APPROVE 自动作废**，必须重新过闸门
- 什么算「实质修改」：改了契约、表结构、事务边界、状态机、失败语义、不变量控制。改错别字不算
- 实质修改时：把顶部版本 +1，在下方追加一条变更说明，状态退回 `READY_FOR_REVIEW`

### 设计闸门判定

同时满足才算通过：

- [x] 需求、非目标和验收标准明确
- [x] 关键契约与事务边界明确
- [x] 触碰的不变量都有控制措施
- [x] 失败路径都有确定的最终状态
- [x] 高风险控制都有测试场景
- [x] 没有未解决的阻断假设
- [ ] 审批绑定到明确的设计版本

### 实现范围（Worker 的 `allowed_change_paths`，登记时照抄）

1. `app/api/auth.py`：新增 `require_admin`
2. `app/api/admin_customers.py`：新建，五个接口
3. `app/main.py`：注册路由
4. `app/schemas/customers.py`：新建，请求与响应模型
5. `app/services/customers.py`：新建，`create_customer`、`list_customers`、`get_customer`、`create_project`、`list_projects`
6. `app/repositories/tenancy.py`：新增 `list_tenants`；`list_projects_for_tenant` 加可选分页参数
7. `app/models/auth.py`：`AuditAction` 加 `CUSTOMER_CREATE`、`PROJECT_CREATE`
8. `tests/backend/test_admin_customers_api.py`：新建，SQLite
9. `tests/backend/test_customer_service.py`：新建，原子性（SQLite 与 MySQL）
10. `tests/backend/test_tenancy_repository.py`：分页用例
11. `docs/api.md`：新建，记五个接口的契约
12. `docs/TODO.md`：任务记录
13. `app/core/database.py`：`create_engine` 加 `hide_parameters=True`
14. `tests/backend/test_database.py`：断言引擎隐藏参数

## 12. 版本变更记录

| 版本 | 日期 | 改了什么 | 触发原因 |
| --- | --- | --- | --- |
| v1 | 2026-09-19 | 初稿 | — |
| v2 | 2026-09-19 | ① §7 鉴权改为从 `app.routes` 枚举所有 `/api/v1/admin` 路由逐个断言 401 / 403 / 不写库，并断言路由集合恰好是五个；INV-8 与 §9 同步；② 余额先 quantize 到 8 位再格式化，POST 与 GET 都断言 `"0.00000000"`；③ `page` 上限 10000 | 设计审查 v1 的阻断项（漏调 `require_admin` 测不出来）与两条非阻断备注 |
| v3 | 2026-09-19 | 引擎统一 `hide_parameters=True`，数据库异常文本不再带 SQL 参数；§5、§6 按现有全局处理器的真实行为改写；§7 加「日志不含个人数据」与引擎配置两条用例；实现范围加 `app/core/database.py`、`tests/backend/test_database.py` | 设计审查 v2 的阻断项：全局处理器会把 SQL 参数（含 email、contact、phone）写进日志 |

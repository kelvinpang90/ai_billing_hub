# AIH-TASK-010 设计：管理端手工调账（草稿 v1，**未批准**）

> **状态说明**：这是设计闸门的**待审草稿**，不是批准副本。OpenClaw Worker 在沙箱里不联网，开不了 Issue，所以先把正文落在仓库里。
> 下一步由 Kelvin（或 Claude 在联网会话里）用本文件正文开 design-gate Issue，跑 `scripts\codex-review.ps1 -Issue <N> -Post`，拿到 `APPROVED: design v<N>` 之后，把本文件换成批准那一版的逐字副本（与 [AIH-TASK-005](AIH-TASK-005-wallet-ledger.md)、[AIH-TASK-006](AIH-TASK-006-admin-customers.md) 同一做法）。
> **在那之前不写实现代码**（[WORKFLOW §3](../WORKFLOW.md)：碰钱的任务通过后才开始写代码）。

---

## 0. 准入判定

| 任务涉及 | 要填的范围 |
| --- | --- |
| 钱包 / 账本 / 定价 / 汇率 / 支付 / 幂等 / 状态机 | **全部章节** |
| 用量摄取 / 认证与会话（集成侧与平台侧）/ Webhook | §1–§7（**§6 不可省**） |
| 前端 / 文档 / CI / 脚本 | 不走本闸门 |
| 既不碰钱、也不碰用量摄取 / 认证与会话 / Webhook 的后端改动 | 不走本闸门 |

本任务档位：`全部`。它往账本里记钱，带幂等键，还会触发计费状态跃迁，三样都在第一行的主题里。

---

状态：`READY_FOR_REVIEW`
负责人：Claude
**设计版本**：`v1`
对应需求：spec §8、§60、§66、§124（Phase 1：「调账生效 · 所有动作都有审计」）；`REQ-FIN-002`、`REQ-IDEMP-001`、`REQ-STATUS-001`（计费状态这一维）、`REQ-PRIV-001`
目标 PR：待开（由 OpenClaw Worker 执行 `AIH-TASK-010`）

## 1. 目标与边界

- **要解决的问题**：Phase 1 验收里的「调账生效 · 所有动作都有审计」（§124）至今没有入口。数据层已经就绪：`post_transaction` 会在同一次 flush 里写账本行、`WALLET_ADJUSTMENT_POSTED` 审计，以及计费状态跃迁（租户行、`TENANT_BILLING_STATUS_CHANGED` 审计、`tenant.billing_status_changed` 出站事件）。鉴权也已就绪：`require_admin` 与管理端客户接口（AIH-TASK-006、AIH-TASK-009）。缺的只是带鉴权的服务层和一个接口。
- **可观察的完成标准**：
  1. 管理员调 `POST /api/v1/admin/customers/{customer_id}/wallet/adjustments`，带类型、金额、原因、幂等键，得到 201 和一行账本。库里同一事务多出：一行 `wallet_transactions`（来源 `ADMIN_ADJUSTMENT`，`created_by` = 管理员）、一条 `WALLET_ADJUSTMENT_POSTED` 审计（带操作者、前后余额、原因、ip、user agent）。MySQL 上钱包余额与版本由触发器同步推进。
  2. 这一笔让余额从 ≤ 0 变成 > 0（或反过来）时，同一事务里还有：租户 `billing_status` 跃迁、`status_version` +1、一条 `TENANT_BILLING_STATUS_CHANGED` 审计、一条 `tenant.billing_status_changed` 出站事件。
  3. 同一个幂等键重复提交只入账一次；第二次返回 200，账本行 `id` 与第一次相同，不再写任何东西。同一个键配不同的类型、金额或客户返回 409，不写任何东西。
  4. 匿名请求 401、CUSTOMER 请求 403，都不写库。缺原因、原因全是空白、金额超过 8 位小数、金额为 0、类型与符号不符、类型不是调账类：422，不写库。
  5. 写审计失败或提交失败时，账本行、审计、租户状态、出站事件全部不留下；SQLite 与 MySQL 上各验证一次。
  6. `docs/api.md` 记下这个接口的契约，并从「不在本批接口里」删去「管理员调账」。
- **明确不做什么**：
  - 管理端查看流水、余额历史（§60 的「Admin sees」列表）：另开任务。本任务的响应里带这一笔之后的余额，客户详情接口已经能看当前余额；
  - 充值与支付（`TOPUP`）、系统更正（`SYSTEM_CORRECTION`）、rebill：不从这个接口进，类型白名单只收 `ADMIN_ADJUSTMENT` 那四种；
  - 调账审批流（双人复核、限额）：spec 没有要求，V1 不做，列入 §10；
  - 撤销调账：账本只追加（INV-5），撤销就是再记一笔反向调账，用新的幂等键；
  - 低余额阈值配置、`account_status`：状态模型任务；
  - 前端页面：另开任务。
- **现有行为与问题证据**：
  - `docs/api.md`「不在本批接口里」一节列着「管理员调账」；`docs/TODO.md` Phase 1 的「管理员手工调账」仍是 `[ ]`；
  - 全仓库只有测试调用 `post_transaction`，没有服务层调用方；
  - `post_transaction` 写的 `WALLET_ADJUSTMENT_POSTED` 审计没有 ip 与 user agent：AIH-TASK-005 设计 §6 写明「由之后的服务层传入」，但 repository 现在没有接收这两项的参数。

## 2. 设计概要

```text
HTTP 请求 → Pydantic 校验（类型白名单、金额字符串、原因、幂等键）
  → 处理函数第一行 require_admin（验签 → 读库确认 ACTIVE 且角色 ADMIN）
  → services.wallet_adjustments.post_adjustment（session_scope：一个事务）
      按路径 public_id 读租户（不加锁）→ 404
      post_transaction(ADMIN_ADJUSTMENT, reference_id = 幂等键, created_by = 管理员,
                       description = 原因, actor_role = ADMIN, ip, user_agent)
          锁钱包 → 锁内按来源查重（重放 / 冲突）→ 锁租户
          → INSERT 账本（MySQL 触发器推进钱包）
          → INSERT 审计 WALLET_ADJUSTMENT_POSTED
          → 需要时：UPDATE 租户状态 + 审计 TENANT_BILLING_STATUS_CHANGED + outbox
          → 需要时：outbox tenant.low_balance
      读回租户当前的 billing_status / status_version
      commit
  → 唯一约束兜底（IntegrityError）时：整个事务回滚，换新事务重试一次（见 §4）
  → 响应模型（白名单字段）→ success() 信封
```

### 接口

`POST /api/v1/admin/customers/{customer_id}/wallet/adjustments`，只有 ADMIN 能调。

**请求体**（`extra="forbid"`）：

| 字段 | 必填 | 规则 |
| --- | --- | --- |
| `transaction_type` | 是 | `ADJUSTMENT_CREDIT`、`ADJUSTMENT_DEBIT`、`BONUS`、`REFUND_ADJUSTMENT` 之一，也就是 `REFERENCE_TYPE_FOR` 里映射到 `ADMIN_ADJUSTMENT` 的四种。其他类型 422 |
| `amount` | 是 | **JSON 字符串**，带符号，就是记进账本的那个数。格式 `^-?\d{1,12}(\.\d{1,8})?$`：整数部分最多 12 位，小数最多 8 位。不是 0。符号必须与类型一致：`ADJUSTMENT_CREDIT` / `BONUS` 为正，`ADJUSTMENT_DEBIT` / `REFUND_ADJUSTMENT` 为负。JSON 数字一律 422（不让浮点数进来） |
| `reason` | 是 | 去掉首尾空白后长度 1–255（就是账本的 `description` 列，也就是 §60 的 reason）。存的是去掉首尾空白后的值 |
| `idempotency_key` | 是 | 小写 uuid 的 36 字符串，由调用方（前端）每次打开调账表单时生成一个。存成账本的 `reference_id` |

- 请求体不能带 `tenant_id`、`customer_id`、`created_by`、`balance`、`metadata` 或任何 id。客户只来自路径，操作者只来自令牌。
- 超过 8 位小数一律 422，**不舍入**（账本层的约定：§80 那唯一一次舍入在定价层）。
- `metadata_json` 不开放，写 NULL。

**成功响应**（首次 201，重放 200，`data` 是调账对象）：

```json
{
  "id": "账本行 public_id",
  "customer_id": "客户 public_id",
  "transaction_type": "ADJUSTMENT_CREDIT",
  "amount": "20.00000000",
  "balance_before": "-5.00000000",
  "balance_after": "15.00000000",
  "wallet_sequence": 4,
  "reason": "Goodwill credit for outage 2026-09-20",
  "idempotency_key": "b7a1…",
  "created_at": "2026-09-23T08:30:00",
  "replayed": false,
  "billing_status": "ACTIVE",
  "status_version": 3
}
```

- 金额字段是字符串，先 `quantize` 到 8 位再 `format(value, "f")`，恰好 8 位小数（与客户详情的余额同一写法）。
- `id` 到 `created_at` 取自账本行，重放时与第一次逐字相同。
- `billing_status` / `status_version` 是**提交时**租户的当前值，重放时可能与第一次不同（中间有别的记账）。`replayed` 标明是不是重放。
- 不返回内部自增 id、`created_by`（内部用户 id）、`metadata_json`、低余额阈值、任何成本或毛利。

**错误**：

| HTTP | `error.code` | 什么时候 | 写库 |
| --- | --- | --- | --- |
| 401 | `TOKEN_INVALID` | 沿用 | 否 |
| 403 | `ADMIN_REQUIRED` | 沿用 | 否 |
| 404 | `CUSTOMER_NOT_FOUND` | 路径里的客户不存在，沿用 | 否 |
| 409 | `ADJUSTMENT_CONFLICT`（新增） | 这个幂等键已经记过，但客户、类型或金额不同（`LedgerConflict`） | 否 |
| 422 | `VALIDATION_ERROR` | 请求体不合法，沿用；只列字段名，不回显值 | 否 |
| 422 | `BALANCE_OUT_OF_RANGE`（新增） | 这一笔之后余额超出 DECIMAL(20,8)（`InvalidAmount("BALANCE_OUT_OF_RANGE")`） | 否 |
| 500 | `INTERNAL_ERROR` | 意外错误，包括客户没有钱包（`WalletNotFound`，数据不一致）；沿用 | 整个事务回滚 |
| 503 | `DATABASE_NOT_CONFIGURED` | 沿用 | 否 |

`post_transaction` 抛出的其余 `InvalidAmount` / `InvalidTransaction` 在请求校验层已经全部挡住，理论上到不了服务层。万一到了（校验层与 repository 规则漂移），按 422 `VALIDATION_ERROR` 返回，消息里只有问题码。

### 数据库

- 不加表、不加列、不加迁移。`AuditAction` 已有 `WALLET_ADJUSTMENT_POSTED` 与 `TENANT_BILLING_STATUS_CHANGED`。
- 幂等依赖现有的 `UNIQUE (reference_type, reference_id)`，`reference_id` 列在 MySQL 上是二进制排序规则，所以只差大小写的键不会被判成同一个。本设计要求键是小写 uuid，在校验层就统一了。

### repository 的一处改动

`post_transaction` 加两个可选关键字参数 `ip_address: str | None = None`、`user_agent: str | None = None`，只传给 `_adjustment_audit`，写进 `WALLET_ADJUSTMENT_POSTED` 审计的同名列（`user_agent` 截到 512，与 `record_audit` 一致）。默认 None，现有调用方与测试的行为不变。

计费状态跃迁的审计**不带** ip 与 user agent：它的操作者是系统（`actor_role = SYSTEM`），不是发起这次请求的人，这一点保持 AIH-TASK-005 的约定。

### 事务边界

一次调账一个 `session_scope()`：成功提交，任何异常回滚。repository 只 flush。账本行、调账审计、租户状态、跃迁审计、出站事件都在这一个事务里（INV-13）。

⚠️ 服务层读租户用**不加锁**的 `get_tenant_by_public_id`，只拿内部 id。加锁全部交给 `post_transaction`，顺序固定为钱包 → 租户。服务层要是先锁租户，就与记账路径的加锁顺序相反，会死锁。

### 时间

`created_at` 用 `utc_now()` 截到整秒（`services/customers.py` 的 `_now`，理由同那里：MySQL `DATETIME` 不存小数秒）。账本行、两条审计、出站事件用同一个时刻。调账没有 `occurred_at` 与 `processed_at` 之分：它的发生时刻就是记账时刻，按记账时刻落入当前开放期间（ADR-0003）。

### 金额

- 请求里是字符串，按正则校验后 `Decimal(value)`，不经过 float。
- 不舍入、不换算（V1 只有 MYR）。余额相加由 `post_transaction` 用 `Decimal` 精确计算。

## 3. 不变量影响矩阵

| 不变量 | 是否触碰 | 可能怎样被破坏 | 设计控制 | 验证方式 |
| --- | --- | --- | --- | --- |
| INV-1 中心故障不中断 AI | 否 | 本接口只在管理端，不在 AI 请求路径上。计费状态跃迁只写 outbox，传到集成端要等以后的 webhook 任务 | — | — |
| INV-2 事件不重复扣费 | 否 | 不处理用量事件；类型白名单排除 `AI_USAGE` | 类型白名单 | 用例：`AI_USAGE` 422 |
| INV-3 支付不重复入账 | 否 | 类型白名单排除 `TOPUP`，调账不能冒充充值 | 同上 | 用例：`TOPUP` 422 |
| INV-4 余额只经账本变动 | 是 | 服务层直接改钱包余额 | 只调 `post_transaction`；MySQL 触发器拒绝任何不对应账本行的余额变动 | MySQL 用例：调账后 `verify_wallet` 为空；新代码里没有 `UPDATE wallets` |
| INV-5 历史账本不可变 | 是 | 「撤销调账」改写旧行 | 不提供撤销；反向调账是新的一行。触发器拒绝 UPDATE / DELETE | 沿用 AIH-TASK-005 的 MySQL 用例 |
| INV-6 事件保留版本引用 | 否 | 调账不涉及定价版本 | — | — |
| INV-7 客户不可见成本毛利 | 是 | 响应或审计带出内部字段 | 响应模型字段白名单，不序列化 ORM 对象；`metadata` 不开放 | 用例：响应键集合恰好等于白名单 |
| INV-8 租户不可互访 | 是 | ① 匿名或 CUSTOMER 调账；② 请求体指定别的客户；③ 用 A 的幂等键在 B 上重放，拿到 A 的账本行 | ① `require_admin`，并进路由枚举用例；② 客户只来自路径，`extra="forbid"`；③ `post_transaction` 查重时比较 `wallet_id`，不同就是冲突 | 用例：路由枚举 401 / 403 不写库；请求体带 `customer_id` 422；A 的键在 B 上 409、响应里没有 A 的任何字段 |
| INV-9 对话内容不入库 | 是（约束） | 原因里写对话内容 | 原因是管理员手写的业务说明，限 255 字；不开放 `metadata` | 不单独测（管理员输入，不是 AI 流量） |
| INV-10 金额用 Decimal | 是 | JSON 数字经 float 进入计算；超过 8 位被静默舍入 | 金额只收字符串，正则限 8 位小数，`Decimal(str)` 构造；响应金额是字符串 | 用例：JSON 数字 422；`"0.000000001"` 422；`"0.00000001"` 成功，响应 `"0.00000001"` |
| INV-11 至多一次财务效果 | 是 | 双击或网络重试记两笔 | 幂等键 = `reference_id`，唯一约束兜底；锁内查重；并发漏网时重试一次走重放分支（§4） | 用例：同键两次只一行；MySQL 并发同键只一行 |
| INV-12 定稿对账单不可变 | 否 | 调账只追加到当前开放期间，不碰已定稿的对账单（对账单属于 Phase 7） | — | — |
| INV-13 状态与事件原子提交 | 是 | 账本行进了库，审计或租户状态没进；或反过来 | 全部在一个 `session_scope` 里 | 用例：注入审计写入失败、注入提交失败，SQLite 与 MySQL 各一次：账本行、两类审计、租户状态、outbox 都不留下 |
| INV-14 队列丢失不毁持久工作 | 否（沿用） | 状态变更事件只进队列 | `post_transaction` 把事件以 `PENDING` 写进 `domain_outbox`，与账本同一事务；本任务不入队 | 沿用 AIH-TASK-005 的用例 |

## 4. 状态与并发

计费状态跃迁完全沿用 AIH-TASK-005 的规则，本任务不新增状态：

| 当前状态 | 事件 | 前置条件 | 新状态 | 副作用 | 非法时结果 |
| --- | --- | --- | --- | --- | --- |
| `SUSPENDED` | 调账 | 新余额 > 0 | `ACTIVE` | `status_version` +1、`TENANT_BILLING_STATUS_CHANGED`（`BALANCE_POSITIVE`）、`tenant.billing_status_changed` | 不适用（由余额决定） |
| `ACTIVE` | 调账 | 新余额 ≤ 0 | `SUSPENDED` | `status_version` +1、审计（`BALANCE_NON_POSITIVE`）、事件 | 不适用 |
| 任意 | 调账 | 应有状态等于现有状态 | 不变 | 无（低余额事件另判） | — |

- **串行化**：`post_transaction` 先 `SELECT … FOR UPDATE` 锁钱包、再锁租户。同一客户的调账、以后的扣费与充值因此串行。
- **数据库负责的唯一性**：`(reference_type, reference_id)`、`(wallet_id, wallet_sequence)`、`public_id`。
- **幂等键**：`(ADMIN_ADJUSTMENT, idempotency_key)`。键由前端在打开表单时生成，双击与超时重发都带同一个键。
- **相同键、不同载荷**：`wallet_id`（客户）、`transaction_type`、`amount` 任一不同 → 409，不写入。三者相同而**原因不同**，按重放处理，返回第一次的原因：`post_transaction` 的判等只看财务效果，这是 AIH-TASK-005 的既定语义，本任务不改。响应里的 `reason` 就是库里那一份，调用方看得见。
- **并发重放的漏网路径（MySQL 的 REPEATABLE READ）**：服务层先用普通 SELECT 读租户，事务的一致性快照在那一刻建立。同一个键的两个请求并发时，后到的在钱包锁上等待；前一个提交后，后到的拿到锁，但锁内的查重是普通 SELECT，读的是旧快照，看不到刚提交的那行，于是继续插入，最后撞上 `UNIQUE (reference_type, reference_id)`，得到 `IntegrityError`。
  - 处置：服务层捕获 `IntegrityError`，整个事务回滚，**换一个新事务重试一次**。新事务的快照能看到那一行，走重放（200）或冲突（409）分支。
  - 只重试一次。第二次还是 `IntegrityError` 就原样抛出，按 500 处理。只可能是别的约束出了问题，不能靠重试解决。
  - 为什么不让查重改成加锁读：AIH-TASK-005 刻意不对不存在的键加锁读，因为会取间隙锁，不同租户相邻的来源 ID 会互相阻塞（`_find_by_reference` 的注释）。
- **重试层**：只有上面那一次，在服务层。锁等待超时与死锁（`OperationalError`）不重试，回滚后按 500 返回，由管理员带同一个键重发，幂等键保证只有一次效果。
- **必须原子提交**：账本行、`WALLET_ADJUSTMENT_POSTED`、（跃迁时）租户行 + `TENANT_BILLING_STATUS_CHANGED` + `tenant.billing_status_changed`、（跨阈值时）`tenant.low_balance`。

## 5. 失败与恢复矩阵

| 失败点 | 对外结果 | 数据最终状态 | 是否可重试 | 恢复来源 | 告警 |
| --- | --- | --- | --- | --- | --- |
| 校验失败（类型、金额格式、符号、原因、幂等键、多余字段） | 422 `VALIDATION_ERROR`，只列字段名 | 没有任何写入 | 修正后重试 | — | 无 |
| 无令牌、令牌无效、账号停用 | 401 | 没有任何写入 | 重新登录 | — | 无 |
| 非 ADMIN | 403 | 没有任何写入 | 否 | — | 无 |
| 客户不存在 | 404 | 没有任何写入 | 否 | — | 无 |
| 客户没有钱包 | 500 | 没有任何写入 | 否 | 人工核查（每个客户都应有钱包） | 全局处理器记异常与 request_id |
| 同键不同载荷 | 409 `ADJUSTMENT_CONFLICT` | 没有任何写入，既有行不变 | 否，换新键 | 管理员核对 | 无 |
| 余额超出范围 | 422 `BALANCE_OUT_OF_RANGE` | 没有任何写入 | 否 | — | 无 |
| 写账本、审计、租户或 outbox 失败 | 500 `INTERNAL_ERROR` | 整个事务回滚，什么都不留下 | 是，带同一个键 | 管理员重发 | 全局处理器记异常与 request_id；引擎 `hide_parameters=True`，日志里没有原因文本等 SQL 参数 |
| DB 提交失败 / 进程在提交前崩溃 | 500 或连接断开 | InnoDB 回滚，什么都不留下 | 是，带同一个键 | 管理员重发；若其实已提交，重发走重放分支 | 同上 |
| 并发同键撞唯一约束 | 服务层重试一次后 200（重放）或 409 | 只有一行 | 已自动重试 | — | 无 |
| 锁等待超时 / 死锁 | 500 | 回滚 | 是，带同一个键 | 管理员重发 | 同上 |
| 重复请求（同键同载荷） | 200，`replayed: true`，同一个账本行 | 不变 | — | — | 无 |
| 数据库未配置 | 503 | 没有任何写入 | 配好后 | 部署配置 | `/readyz` |
| Redis / Celery 丢失 | 不影响：本任务不入队，事件已持久写进 `domain_outbox` | — | — | 以后的投递任务从 `PENDING` 接手 | — |
| 外部服务超时 | 不涉及：不调外部服务 | — | — | — | — |

## 6. 数据与安全边界

- **租户过滤**：管理端按角色跨租户（§56）。客户只从路径 `public_id` 解析，内部 `tenant_id` 从解析结果取，不来自请求体。幂等键跨客户复用会被判为冲突，拿不到别人的账本行。
- **鉴权主体**：只从已验签的访问令牌取，角色以数据库为准（`require_admin`）。`created_by` 与审计的 `actor_user_id` 就是这个管理员，`actor_role` 取其数据库角色 `ADMIN`。
- **CSRF**：与 AIH-TASK-006 相同，访问令牌走 `Authorization: Bearer` 头，不受 CSRF 影响（§96）。
- **客户侧禁止返回的字段**：本任务没有客户侧接口。管理端响应只有 §2 列出的白名单字段。
- **日志**：这一层不写日志，不记请求体，也不记原因文本。原因是管理员的自由文本，可能碰巧写进个人数据，所以只进账本与审计，不进应用日志。意外异常由全局处理器记下，引擎的 `hide_parameters=True` 保证异常文本里没有 SQL 参数。
- **审计**：
  - `WALLET_ADJUSTMENT_POSTED`（`post_transaction` 写）：`actor_user_id`、`actor_role`、`entity_type = wallet_transaction`、`entity_id` = 账本行 `public_id`、前后余额、`reason`，本任务补上 ip 与 user agent。这就是 §60 要求系统自动记录的操作者、时间、前后余额。
  - `TENANT_BILLING_STATUS_CHANGED`（跃迁时，`post_transaction` 写）：操作者是系统，内容沿用 AIH-TASK-005。
  - 重放不写审计：没有发生新的动作。
- **UI 约定**（写进 `docs/api.md`）：原因只写业务说明，不写客户的联系人、电话等个人数据。
- **密钥**：不涉及。
- **prompt / response**：不处理。
- **数据保留与删除**：账本与审计永久保留，不提供删除或撤销。

## 7. 测试证据计划

测试层级沿用 AIH-TASK-005 §7 的规则：SQLite 上没有钱包触发器，钱包余额不会随账本推进。所以凡是依赖「记账前余额」的场景（跨零、连续记账、余额值）一律放在 MySQL 上（CI 必跑，不许 skip）。SQLite 只测接口契约、鉴权、校验、幂等与回滚。钱包从 0 开始的第一笔在 SQLite 上结果也正确，可以用来测响应格式。

| 风险/需求 | 测试层级 | 场景 | 预期结果 |
| --- | --- | --- | --- |
| 正常路径 | API（SQLite） | 新客户（余额 0），ADMIN 记 `ADJUSTMENT_CREDIT` `"20"` | 201；响应键集合等于白名单；`amount = "20.00000000"`、`balance_before = "0.00000000"`、`balance_after = "20.00000000"`、`wallet_sequence = 1`、`replayed = false`；一行账本（`created_by` = 管理员、`reference_id` = 幂等键）；一条 `WALLET_ADJUSTMENT_POSTED`，带 ip 与 user agent |
| 正常路径与真实触发器 | 服务（MySQL） | 同上，之后记 `ADJUSTMENT_DEBIT` `"-5.12345678"` | 钱包余额 14.87654322、版本 2；`verify_wallet` 为空 |
| 鉴权：第一行调用 `require_admin` | API（SQLite） | 路由枚举用例的 `EXPECTED_ADMIN_ROUTES` 加上本接口，`VALID_BODIES` 加一个合法请求体；无令牌、CUSTOMER 令牌各调一次 | 401 / 403 `ADMIN_REQUIRED`；调用前后 `wallet_transactions`、`audit_logs`、`tenants`、`domain_outbox` 行数不变 |
| 鉴权：处理函数顺序 | unit | 读处理函数源码（`inspect.getsource`）或 AST，取函数体第一条语句 | 是对 `require_admin` 的调用 |
| 缺原因 | API（SQLite） | 不带 `reason`；`reason = "   "`；长 256；恰好 255 | 422 / 422 / 422 / 201；前三种账本行数不变 |
| 金额精度 | API（SQLite） | `"0.000000001"`（9 位）；`"0"`；`"-0.00000000"`；JSON 数字 `20`；`"1e2"`；13 位整数；`"0.00000001"` | 前六种 422、账本行数不变；最后一种 201，响应 `"0.00000001"` |
| 类型与符号 | API（SQLite） | `ADJUSTMENT_CREDIT` 配负数；`ADJUSTMENT_DEBIT` 配正数；`TOPUP`；`AI_USAGE`；`SYSTEM_CORRECTION` | 都是 422，不写库 |
| 多余字段 | API（SQLite） | 请求体带 `customer_id` / `tenant_id` / `created_by` / `metadata` | 422 |
| 幂等重放 | API（SQLite） | 同一请求体提交两次 | 第一次 201、第二次 200；两次 `id` 相同；第二次 `replayed = true`；账本一行、`WALLET_ADJUSTMENT_POSTED` 一条 |
| 相同键、不同载荷 | API（SQLite） | 同键改金额；同键改类型；同键换一个客户 | 都是 409 `ADJUSTMENT_CONFLICT`；账本仍只一行；换客户那次的响应里没有第一个客户的任何字段 |
| 并发同键 | 服务（MySQL） | 8 个线程各自独立会话，同一个键、同一载荷同时调 `post_adjustment` | 恰好一行账本、一条调账审计；恰好一个 `replayed = false`，其余都是 `replayed = true`，账本行 `id` 全部相同；没有 500 |
| 事务中途失败：审计 | 服务（SQLite 与 MySQL 各一次） | 让 `_adjustment_audit` 返回的对象在 flush 时失败（例如 monkeypatch 成 `actor_role` 超过列宽的行，或直接抛异常） | 异常传出；账本行、审计、租户 `billing_status` / `status_version`、outbox 都与调用前相同 |
| 事务中途失败：提交 | 服务（SQLite 与 MySQL 各一次） | 在一次跨零调账里让 `Session.commit` 抛异常（monkeypatch） | 同上，什么都不留下 |
| 跨零恢复 | 服务（MySQL） | 先记 `ADJUSTMENT_DEBIT` `"-5"`（余额 −5，`SUSPENDED`），再记 `ADJUSTMENT_CREDIT` `"20"` | 第二笔后余额 15、`ACTIVE`、`status_version` +1；同一事务里一条 `TENANT_BILLING_STATUS_CHANGED`（`BALANCE_POSITIVE`，`after_state.wallet_transaction` = 第二笔的 `id`）、一条 `tenant.billing_status_changed`；响应的 `billing_status = ACTIVE` |
| 跨零暂停 | 服务（MySQL） | 余额 15、`ACTIVE`，记 `ADJUSTMENT_DEBIT` `"-15"` | 余额 0、`SUSPENDED`，一次跃迁 |
| 租户越权 | API（SQLite） | 不存在的 `public_id`；内部自增 id | 404，不写库 |
| 余额超出范围 | 服务（MySQL） | 余额接近上限时再记一笔大额贷方 | 422 `BALANCE_OUT_OF_RANGE`，不写库 |
| 日志不含原因 | API（SQLite） | 用有辨识度的原因文本调账（成功一次、再让它在数据库层失败一次），抓取全部日志 | 日志里没有这段原因文本 |
| Redis/Celery 丢失 | — | 本任务不入队 | 不适用 |

## 8. 迁移与上线

- **数据迁移**：无。不加表、不加列、不加枚举值。
- **锁表与性能**：无 DDL。每次调账锁一个钱包行和一个租户行，持锁时间是一个短事务。管理员手工操作，频率极低。
- **兼容窗口**：只加接口；`post_transaction` 只加两个带默认值的可选参数，现有调用方不变。
- **部署顺序**：合并即部署，没有前置配置。
- **回滚**：回退合并提交即可。已经记下的调账是合法的财务记录，保留；要抵消就记反向调账。
- **部分部署**：单组件，不涉及。
- **监控**：沿用应用日志与 `/readyz`。上线后在生产上给测试客户记一笔小额调账、再记一笔反向调账，核对余额、审计与 `verify_wallet`（部署后核对项，写进 TODO，不写进代码）。

## 9. 方案取舍

| 方案 | 优点 | 风险 | 未采用原因 |
| --- | --- | --- | --- |
| **采用**：请求里的 `amount` 带符号、就是账本里的数，并且必须与类型的符号一致 | 发出去的就是记进去的，审计与账本上的数与请求一眼对得上；类型和符号要写对两次，方向写反会被 422 挡住 | 前端要按类型带符号 | — |
| `amount` 只收正数，符号由类型推出 | 前端简单 | 方向只靠 `transaction_type` 一处决定，选错类型就是一笔方向相反的钱，没有第二道确认 | 调账是人手输入的钱，多一道一致性校验值得 |
| 幂等键放在 `Idempotency-Key` 头里 | 符合常见 HTTP 惯例 | 本仓库还没有这个惯例；头不在 Pydantic 的请求体校验里，缺失或格式不对要另写一套 422 | 放请求体，与其他字段同一套校验与 422 契约。以后统一改头部惯例时一起改 |
| 服务层按唯一约束兜底重试一次（采用） | 并发同键得到确定的 200 / 409，不出 500 | 多一段重试代码 | — |
| 把锁内查重改成加锁读 | 不需要重试 | 对不存在的键加锁读会取间隙锁，不同租户相邻的来源 ID 互相阻塞（AIH-TASK-005 刻意避免） | 不改 repository 的加锁策略 |
| 服务层另写一条带 ip / user agent 的审计，repository 不改 | 不动 repository | 一次调账两条内容重叠的审计，或者得绕开 `post_transaction` 内置的那条 | `post_transaction` 加两个可选参数，改动最小，而且 AIH-TASK-005 设计 §6 本来就说这两项由服务层传入 |
| 重放返回 201 | 调用方不用区分 | 看不出这次到底有没有入账 | 重放 200 加 `replayed` 字段，调用方看得出来 |

## 10. 未决问题与假设

- **未决问题**：无阻断项。
- **明确后移**：
  1. 管理端查看流水（§60 的「Admin sees」）另开任务；
  2. 调账审批、单笔限额：spec 没要求，V1 不做。需要时另过设计闸门；
  3. 前端调账表单另开任务。它要负责在打开表单时生成幂等键，并在超时后带同一个键重发。
- **假设**：
  1. 管理员人数少、调账频率低，钱包行锁不会成为瓶颈；
  2. 生产 MySQL 用默认的 REPEATABLE READ。如果改成 READ COMMITTED，§4 的漏网路径变少，但重试逻辑仍然正确、无害。
- **需要谁拍板**：`amount` 带符号（§9 第一行）是契约选择，Kelvin 如果更想要「只收正数」，在审查时提出，改动只在校验层。
- **如果假设错了**：锁成了瓶颈也只影响管理端调账的延迟，不影响正确性。

## 11. 审查与版本绑定

审查方要回答的五个问题见 [scripts/review_checklist.md](../../scripts/review_checklist.md) 的「设计审查」一节。

**版本绑定规则**（出处是 [WORKFLOW §3](../WORKFLOW.md)）：

- 批准必须写成 `APPROVED: design v<N>`，`<N>` 取本文件顶部的「设计版本」
- **设计版本一变，之前的 APPROVE 自动作废**，必须重新过闸门
- 什么算「实质修改」：改了契约、表结构、事务边界、状态机、失败语义、不变量控制。改错别字不算
- 实质修改时：把顶部版本 +1，在下方追加一条变更说明，状态退回 `READY_FOR_REVIEW`

### 设计闸门判定

同时满足才算通过：

- [ ] 需求、非目标和验收标准明确
- [ ] 关键契约与事务边界明确
- [ ] 触碰的不变量都有控制措施
- [ ] 失败路径都有确定的最终状态
- [ ] 高风险控制都有测试场景
- [ ] 没有未解决的阻断假设
- [ ] 审批绑定到明确的设计版本

### 实现范围（批准后 Worker 的 `allowed_change_paths`）

1. `app/repositories/wallet.py`：`post_transaction` 与 `_adjustment_audit` 加 `ip_address`、`user_agent`
2. `app/services/wallet_adjustments.py`：新建，`post_adjustment`（事务、唯一约束兜底重试一次、错误映射）
3. `app/schemas/wallet_adjustments.py`：新建，请求与响应模型
4. `app/api/admin_customers.py`：新增处理函数，第一行 `require_admin`
5. `tests/backend/test_admin_customers_api.py`：`EXPECTED_ADMIN_ROUTES` 与 `VALID_BODIES` 加本接口
6. `tests/backend/test_wallet_adjustment_api.py`：新建，SQLite 上的接口契约
7. `tests/backend/test_wallet_adjustment_service.py`：新建，回滚（SQLite 与 MySQL）、跨零、并发（MySQL）
8. `tests/backend/test_wallet_repository.py`：ip 与 user agent 进审计
9. `docs/api.md`：新增契约，删去「不在本批接口里」的「管理员调账」
10. `docs/TODO.md`：任务记录；Phase 1 的「管理员手工调账」在 CI 全绿并合并时勾上

## 12. 版本变更记录

| 版本 | 日期 | 改了什么 | 触发原因 |
| --- | --- | --- | --- |
| v1 | 2026-09-23 | 初稿 | — |

# AIH-TASK-020 设计：租户账户状态（已批准 v2）

> **来源**：设计闸门 Issue #136。本文件是 `APPROVED: design v2` 那一版正文的**逐字副本**，审查者为 Codex。
> 放进仓库，是因为 OpenClaw Worker 在沙箱里不联网、读不到 GitHub Issue。
> **实现以本文件为准**；与 Issue 不一致时，以 Issue #136 上被批准的 v2 为准。设计要改，就回到 Issue 升版本、重新过闸门，不要直接改本文件。
> 正文里写「编号登记时分配」的实现任务，登记为 `AIH-TASK-020`。
> 与 Issue 正文的唯一差别：一处链接目标由 GitHub 网页上的 `../blob/main/…` 改成仓库内的相对路径，链接文字与其余正文逐字相同。

---

## 0. 准入判定

| 任务涉及 | 要填的范围 |
| --- | --- |
| 钱包 / 账本 / 定价 / 汇率 / 支付 / 幂等 / 状态机 | **全部章节** |
| 用量摄取 / 认证与会话（集成侧与平台侧）/ Webhook | §1–§7（**§6 不可省**） |
| 前端 / 文档 / CI / 脚本 | 不走本闸门 |
| 既不碰钱、也不碰用量摄取 / 认证与会话 / Webhook 的后端改动 | 不走本闸门 |

本任务档位：`全部`。它是租户的状态机，与计费状态共用 `status_version`。

---

状态：`READY_FOR_REVIEW`
负责人：Claude
**设计版本**：`v2`（v1 被 Codex 判 REQUEST_CHANGES：关户缺「无未结财务事件」前置条件。v2 把关户整体移出本任务，见 §9、§12）
对应需求：spec §24、§25、§56、§66、§75、§112.1；`REQ-STATUS-001`、`REQ-TXN-001`、`REQ-PRIV-001`
目标 PR：待开（批准后在 `.platform/tasks.yaml` 登记实现任务，编号登记时分配）

## 1. 目标与边界

- **要解决的问题**：spec §24 把租户状态分成三维：账户生命周期 `account_status`（`PENDING_ACTIVATION / ENABLED / DISABLED / CLOSED`）、由余额驱动的 `billing_status`、项目的 `integration_status`；§56 要求管理员能「独立于计费状态改变账户生命周期」。现在只有 `billing_status`（AIH-TASK-005），管理员没有办法停用一个客户（例如滥用、合同终止、准备关户）。
- **可观察的完成标准**：
  1. `tenants` 有 `account_status` 列；已有的租户与新建的租户都是 `ENABLED`。
  2. 管理员调 `POST /api/v1/admin/customers/{customer_id}/account-status`（目标 `ENABLED` 或 `DISABLED`，带原因）改变账户状态；合法跃迁 200，非法跃迁 409，不写库。
  3. 每次真实跃迁在同一事务里：改状态、`status_version` +1、写一条 `TENANT_ACCOUNT_STATUS_CHANGED` 审计、写一条 `tenant.account_status_changed` outbox 事件。
  4. 客户详情与客户列表的响应多一个 `account_status` 字段。
- **明确不做什么**：
  - **关户（`→ CLOSED`）**：spec §112.1 要求关户前「等合法的在途用量事件与支付到达终态」且「无未结财务事件」。用量事件（Phase 2）与支付（Phase 4）的表都还不存在，这个前置条件现在无法做成可验证、可测试的规则。关户另立任务，在 Phase 2 与 Phase 4 落地后做；Kelvin 2026-09-28 确认过的关户口径（余额恰好为 0、关户后拒绝新调账 / 建项目 / 建凭据 / 轮换凭据、`CLOSED` 为终态）原样作为那个任务的输入，记进 `docs/TODO.md`。本任务不产生 `CLOSED`，但 CHECK 约束允许这个取值，免得关户任务再改列
  - 有效状态合成（`ALLOW_AI` / `BLOCK_AI`）与 `reason_code` 取值表、`projects.integration_status`、状态 webhook 的 payload —— 只有 Phase 3 的投递与对账接口在用，归「出站服务状态 webhook 投递」设计闸门。本任务只保证那时要的输入（账户状态、计费状态、共用的 `status_version`）已经在库里、且每次变化都有 outbox 事件
  - 对以后功能的约束（摄取、充值、客户登录在各状态下的行为）只在 §2 末尾写成**契约**，由各自 Phase 的设计闸门实现
  - `PENDING_ACTIVATION` 的产生路径：V1 没有自助注册（§52：客户门户账号由管理员建），本任务不产生这个值，只让它是合法取值并定义从它出去的跃迁
  - 前端（批准后另登记前端任务）
- **现有行为与问题证据**：
  - `app/models/tenancy.py` 的文件头：「`account_status` … 刻意不建」；`docs/database-schema.md` 第 48 行：「`account_status` 是另一维，尚未建」
  - `docs/api.md`「编辑客户」：账户状态不走 PATCH，PATCH 带 `account_status` 返回 422
  - 计费跃迁的写法：`app/repositories/wallet.py` 的 `_apply_billing_status` —— 在租户行锁内 `status_version` +1、`TENANT_BILLING_STATUS_CHANGED` 审计（操作者 SYSTEM）、`tenant.billing_status_changed` outbox 行，同一事务。本任务照这个写法

## 2. 设计概要

```text
管理端请求 → Pydantic 校验（extra="forbid"）
  → 处理函数第一行 require_admin
  → services.account_status.change(...)（session_scope：一个事务）
      按 public_id 读客户并锁租户行（SELECT … FOR UPDATE）→ 404
      目标状态 == 当前状态 → 200，不写
      查跃迁表 → 非法 409
      改 account_status、status_version +1、updated_at
      写审计 TENANT_ACCOUNT_STATUS_CHANGED
      写 outbox tenant.account_status_changed
      commit
  → 客户详情（多 account_status 字段）→ success() 信封
```

### 接口

`POST /api/v1/admin/customers/{customer_id}/account-status`，只有 ADMIN，处理函数第一条语句是 `require_admin`（加进路由枚举用例）。

| 字段 | 必填 | 规则 |
| --- | --- | --- |
| `account_status` | 是 | `ENABLED` 或 `DISABLED`（`PENDING_ACTIVATION` 与 `CLOSED` 不能作为目标，422） |
| `reason` | 是 | 去首尾空白后 1–255；只写业务说明，不写个人数据（进审计，永久保留） |

- 其他字段 422（包括 `status_version`、`billing_status`、余额、任何 id）。
- 成功：200，`data` 是客户详情（含新的 `account_status` 与 `status_version`）。
- 目标状态等于当前状态：200，返回当前详情，**不写库**（不写审计、不加版本、不写 outbox）—— 与编辑客户「没变化就不写」一致。

错误：

| HTTP | `error.code` | 什么时候 | 写库 |
| --- | --- | --- | --- |
| 404 | `CUSTOMER_NOT_FOUND` | 客户不存在 | 否 |
| 409 | `ACCOUNT_STATUS_TRANSITION_INVALID` | 跃迁表里没有这一格（例如从 `CLOSED` 出去；本任务里 `CLOSED` 不会出现，但判定按表写全） | 否 |

**客户对象**：客户详情与客户列表项都加 `account_status`。PATCH 继续拒绝 `account_status`（422），账户状态只走本接口。

### 数据库

- `tenants.account_status`：`VARCHAR(32) NOT NULL`，服务端默认 `'ENABLED'`（迁移时给已有行回填，生产上现在 2 行，其中一行是验收夹具）
- `CHECK (account_status IN ('PENDING_ACTIVATION','ENABLED','DISABLED','CLOSED'))`
- 不加索引：没有按账户状态筛选的查询
- 不改 `status_version` 的定义：它已是租户级、只增不减的计数器，本任务让账户状态跃迁也 +1

### 事务边界

一个 `session_scope`：锁租户行 → 判定 → 改租户 → 审计 → outbox → 提交。任何一步失败整体回滚。

**只锁租户行，不锁钱包**：本任务不读余额。`post_transaction` 的顺序是钱包 → 租户；本路径只拿租户一把锁，不会与它形成环，不引入新的死锁顺序。两条路径都在租户行锁内给 `status_version` +1，所以两种跃迁交错时版本号仍严格递增、不重复。

### 外部系统与异步边界

写一条 outbox 事件 `tenant.account_status_changed`，`aggregate_type = tenant`、`aggregate_id` = 租户 `public_id`，payload：`account_status`、`previous_account_status`、`billing_status`、`status_version`、`changed_at`。**原因不进 payload**（只进审计），因为 payload 以后会被转成对外的 webhook。现在没有处理器，事件以 `PENDING` 持久等待（与 `tenant.billing_status_changed` 相同，`recover` 只重投有处理器的类型）。Phase 3 的投递设计把这两个事件合成有效状态、扇出到各项目。

### 时间

`updated_at` 与 payload 的 `changed_at` 取服务端 `now`，截到整秒（与 006 / 011 / 012 一致），UTC 无时区。

### 金额

不适用：本任务不读也不改任何金额（v1 的关户余额检查已随关户移出）。

### 审计

| 字段 | 值 |
| --- | --- |
| `action` | `TENANT_ACCOUNT_STATUS_CHANGED` |
| 操作者 | 调用的管理员，带 ip 与 user agent |
| `entity_type` / `entity_id` | `tenant` / 租户 `public_id` |
| `before_state` | `account_status`、`status_version` |
| `after_state` | `account_status`、`status_version` |
| `reason` | 请求里的 `reason` |

spec §66 的清单里只有 `TENANT_SUSPEND` / `TENANT_REACTIVATE`，而余额驱动的那一维已用 `TENANT_BILLING_STATUS_CHANGED`。这里按同一写法用一个动作名覆盖账户状态的跃迁，在 `docs/TODO.md` 记为对 §66 的映射：`→ DISABLED` 对应 `TENANT_SUSPEND`，`DISABLED → ENABLED` 对应 `TENANT_REACTIVATE`。

### 对以后功能的契约（本任务不实现，写进 `docs/database-schema.md` 与 `docs/TODO.md` 对应 Phase；Kelvin 2026-09-28 已确认）

| 功能 | `PENDING_ACTIVATION` | `ENABLED` | `DISABLED` | `CLOSED`（关户任务） |
| --- | --- | --- | --- | --- |
| 新的 AI 调用（有效状态） | 阻断 | 看计费与集成状态 | 阻断 | 阻断 |
| 用量事件摄取与扣费（Phase 2） | 照常 | 照常 | **照常**（§112.1：在途的合法事件要处理完） | 不入账，进人工复核 |
| 新充值（Phase 4） | 允许 | 允许 | **拒绝**（§112.1 第 1 步） | 拒绝 |
| 管理员调账 | 允许 | 允许 | 允许（关户前清偿 / 退款要用） | 拒绝 |
| 建项目、建凭据、轮换凭据 | 允许 | 允许 | 允许 | 拒绝（吊销凭据、查看、编辑客户仍允许） |
| 客户门户登录（Phase 4） | 由 Phase 4 定 | 允许 | 只读（看账单与收据） | 只读 |

**关户任务的前置条件**（写进 TODO，供那个任务的设计闸门使用）：只能从 `DISABLED` 关；钱包余额恰好为 0；没有非终态的用量事件与支付（§112.1）；在记账总入口 `post_transaction` 的租户行锁内拒绝给 `CLOSED` 租户入账；`CLOSED` 为终态。

## 3. 不变量影响矩阵

| 不变量 | 是否触碰 | 可能怎样被破坏 | 设计控制 | 验证方式 |
| --- | --- | --- | --- | --- |
| INV-1 中心故障不中断 AI | 否（间接） | 本任务不在 AI 调用路径上；账户状态以后经有效状态影响 AI，由应用侧缓存执行，中心故障时沿用缓存 | Phase 3 设计负责 | — |
| INV-2 事件不重复扣费 | 否 | 不涉及用量事件与扣费；记账路径一行不改 | — | — |
| INV-3 支付不重复入账 | 否 | 还没有支付 | — | — |
| INV-4 余额只经账本变动 | 否 | 不读写钱包 | 服务层不导入钱包模型 | 用例断言跃迁前后钱包 `version` 不变 |
| INV-5 历史账本不可变 | 否 | 不碰账本 | — | — |
| INV-6 事件保留版本引用 | 否 | 不涉及用量事件 | — | — |
| INV-7 客户不可见成本毛利 | 否 | 响应只多一个 `account_status` | 响应白名单 | 字段断言 |
| INV-8 租户不可互访 | **是** | 用一个客户的路径改另一个客户的状态 | 只按路径 `public_id` 取租户，不接受请求体里的 id | 越权与多余字段用例 |
| INV-9 对话内容不入库 | 否 | 不处理对话 | — | — |
| INV-10 金额用 Decimal | 否 | 无金额 | — | — |
| INV-11 event_id 至多一次财务效果 | 否 | 同 INV-2 | — | — |
| INV-12 定稿对账单不可变 | 否 | 还没有对账单 | — | — |
| INV-13 状态与事件原子提交 | **是** | 状态改了而审计或 outbox 没写；两种跃迁交错使版本号重复或倒退 | 状态、版本、审计、outbox 同一事务；两种跃迁都在租户行锁内 +1 | 审计 / outbox / 提交失败时的回滚用例（SQLite 与 MySQL 各一次）；并发版本用例（MySQL） |
| INV-14 队列丢失不毁持久工作 | **是** | 事件只进内存队列 | 只写数据库 outbox（`PENDING`），没有处理器前持久等待 | 断言 outbox 行存在且为 `PENDING` |

## 4. 状态与并发

| 当前状态 | 事件（目标） | 前置条件 | 新状态 | 副作用 | 非法时结果 |
| --- | --- | --- | --- | --- | --- |
| `PENDING_ACTIVATION` | `ENABLED` | — | `ENABLED` | 版本 +1、审计、outbox | — |
| `PENDING_ACTIVATION` | `DISABLED` | — | `DISABLED` | 同上（先停用、再结算，为关户做准备） | — |
| `ENABLED` | `DISABLED` | — | `DISABLED` | 同上 | — |
| `DISABLED` | `ENABLED` | — | `ENABLED` | 同上 | — |
| `CLOSED` | 任何目标 | — | — | — | 409 `ACCOUNT_STATUS_TRANSITION_INVALID`（终态；本任务不产生 `CLOSED`，这一行保证以后关户任务上线后也不会被本接口重开） |
| 任何状态 | 与当前相同 | — | 不变 | 无（200，不写） | — |

`→ PENDING_ACTIVATION` 与 `→ CLOSED` 不是本接口的合法目标（请求校验层 422）。

计费状态不受账户状态影响，改账户状态也不改计费状态。两者共用 `status_version`：任何一维的真实跃迁都 +1。

- **串行化**：租户行锁。同一租户的账户状态变更之间、以及它与计费跃迁之间都串行。
- **数据库保证的唯一性**：无新增唯一约束；`status_version` 的单调性由「只在租户行锁内 +1」保证（与计费跃迁同一机制）。
- **幂等键**：没有客户端幂等键；目标状态等于当前状态即返回 200 不写，所以超时重发是安全的。
- **相同 ID、不同载荷**：不适用。原因不同的重发（目标相同）按幂等处理，保留第一次的审计。
- **重试层**：服务端不重试；客户端对 5xx 与超时重发。
- **原子提交**：状态、版本、审计、outbox。

## 5. 失败与恢复矩阵

| 失败点 | 对外结果 | 数据最终状态 | 是否可重试 | 恢复来源 | 告警 |
| --- | --- | --- | --- | --- | --- |
| 校验失败（含目标为 `PENDING_ACTIVATION` / `CLOSED`） | 422 | 无改动 | 改请求后可 | — | 无 |
| 客户不存在 | 404 | 无改动 | 否 | — | 无 |
| 非法跃迁（从 `CLOSED` 出去） | 409 | 无改动 | 否 | — | 无 |
| 审计或 outbox 写入抛错 | 500 | 整体回滚，状态与版本不变 | 可 | — | 错误日志 |
| DB 提交失败 | 500 | 回滚，同上 | 可 | — | 错误日志 |
| 提交成功、响应丢失 | 客户端超时 | 状态已变 | 重发同一目标 → 200 不写 | — | 无 |
| 锁等待超时（与记账争租户行锁） | 500（MySQL 锁超时） | 回滚 | 可 | — | 错误日志 |
| Redis / Celery 丢失 | 无影响 | outbox 行在数据库里 `PENDING` | — | 以后的处理器从数据库取 | — |
| 外部服务超时 | 不适用 | 本任务不调外部服务 | — | — | — |
| 重复请求 | 200 不写 | 不变 | — | — | — |

## 6. 数据与安全边界

- **租户过滤**：租户只来自路径 `public_id`；请求体不能带任何 id。
- **鉴权主体**：只有 ADMIN（`require_admin`，角色以数据库为准）。没有客户侧接口。
- **禁止返回的字段**：内部 id、低余额阈值、任何成本或毛利（沿用客户对象的白名单，只加 `account_status`）。
- **日志 / 审计 / 异常**：原因只进审计的 `reason`，不进应用日志、不进 outbox payload；审计前后状态只有 `account_status` 与 `status_version`，不含联系人、邮箱、电话（REQ-PRIV-001）。
- **密钥**：不涉及。
- **prompt / response 或客户数据**：不处理。
- **保留与删除**：不删除任何数据。

## 7. 测试证据计划

| 风险/需求 | 测试层级 | 场景 | 预期结果 |
| --- | --- | --- | --- |
| 正常路径 | integration（API） | `ENABLED → DISABLED → ENABLED` | 每步 200；`status_version` 各 +1；每步一条审计与一条 `PENDING` outbox，payload 字段正确、不含原因 |
| 跃迁表 | unit | 表里每一格（含 `PENDING_ACTIVATION` 出发的两格与 `CLOSED` 出发的非法格，后者用直接改库造数据） | 合法格返回新状态；`CLOSED` 出发 409 `ACCOUNT_STATUS_TRANSITION_INVALID` |
| 边界值 | integration | 目标 `PENDING_ACTIVATION` / `CLOSED` / 小写 / 空；`reason` 空白 / 256 字符 | 422，不写库 |
| 幂等 | integration | 目标等于当前 | 200，不写审计、不加版本、不写 outbox |
| 与计费状态独立 | integration | `DISABLED` 状态下调账跨零；跃迁前后钱包 | 计费状态照常跃迁、账户状态不变，版本各 +1；账户状态跃迁不改钱包 `version` |
| 并发版本 | integration（真 MySQL） | 两个线程同时改账户状态、另一线程调账跨零 | `status_version` 严格递增、无重复，与两类审计条数之和一致 |
| 事务中途失败 | integration（SQLite 与 MySQL 各一次） | 审计写入抛错、outbox 写入抛错、提交抛错 | 全部回滚：状态、版本不变，无审计、无 outbox |
| 鉴权与越权 | integration | 匿名 401、CUSTOMER 403；多余字段（`status_version`、`tenant_id`） | 401 / 403 / 422，不写库；首条语句 `require_admin`（AST 用例） |
| PATCH 仍拒绝 | integration | PATCH 带 `account_status` | 422（新增显式用例） |
| 响应字段 | integration | 详情与列表 | 有 `account_status`，没有阈值、成本与内部 id |
| 迁移 | integration（MySQL） | upgrade 后已有行为 `ENABLED`；插入非法值被 CHECK 拒绝；downgrade 删列 | 同左 |
| Redis/Celery 丢失 | 不适用 | 没有异步处理器；outbox 行在数据库 | — |
| 金额精度 | 不适用 | 无金额 | — |

## 8. 迁移与上线

- **数据迁移步骤**：`tenants` 加列 `account_status VARCHAR(32) NOT NULL DEFAULT 'ENABLED'` 与 CHECK。服务端默认值给已有行回填。迁移序号取实现时的下一个。
- **锁表与性能影响**：MySQL 8.4 加带默认值的列走 INSTANT 算法，不重建表；生产上 `tenants` 只有 2 行。
- **兼容窗口**：旧代码不认识新列，但也不写它（服务端默认值兜底），无影响；新代码依赖新列，迁移必须先于代码（既有流程：容器启动前跑迁移）。
- **部署顺序**：合并即自动部署；先迁移后代码。
- **回滚或前滚**：downgrade 先删 CHECK 再删列。若已有租户被置为 `DISABLED`，回滚会丢掉这个状态 —— 那时前滚修复。
- **部分部署时的行为**：迁移成功、代码未更新：所有租户 `ENABLED`，旧代码无感。代码更新、迁移失败：容器启动失败，旧容器继续服务。
- **监控指标和告警负责人**：无新增指标；账户状态变更是低频的管理员操作，审计即记录。

## 9. 方案取舍

| 方案 | 优点 | 风险 | 未采用原因 |
| --- | --- | --- | --- |
| **采用**：本任务只做 `ENABLED` / `DISABLED`（含从 `PENDING_ACTIVATION` 出发），显式跃迁表 + 一个动作名 + 共用 `status_version` | 每条规则现在都可验证、可测试；不碰记账路径 | 管理员暂时不能关户（生产上还没有真实客户，现在也没有要关的户） | — |
| 本任务连关户一起做（v1），「无未结财务事件」写成恒为空的检查 | 状态机一次做完 | 用量事件与支付的表还不存在，检查只能是空实现，既测不出任何东西，又给人「已经保证」的错觉；Codex 在 v1 判为阻断 | 关户推迟到 Phase 2 / 4 之后，那时前置条件有真实数据可查 |
| 账户状态跃迁不加 `status_version`（只让有效状态变化时加） | 版本号更「省」 | 要先有有效状态合成（Phase 3），且集成方就看不到账户维度的单调序；账户状态的每次变化都可能改变有效状态 | 每次跃迁都 +1：多加的版本对集成方无害（同样的有效状态重复应用是幂等的，§28），少加才有害 |
| 新租户默认 `PENDING_ACTIVATION`，要管理员再点一次「启用」 | 与 spec 取值更「完整」 | 新租户余额为 0，本来就是 `SUSPENDED`、AI 被阻断，多一步启用没有额外的保护，只多一个会被忘掉的操作 | 默认 `ENABLED`（Kelvin 2026-09-28 确认） |
| 锁钱包再锁租户（与 `post_transaction` 同序） | 与记账路径完全同构 | 本任务不读余额，多锁一把只增加争用 | 只锁租户行即可保证版本单调，也不会与钱包 → 租户的顺序成环 |

## 10. 未决问题与假设

- **未决问题**：无。Kelvin 2026-09-28 确认了 v1 §10 的 6 个业务口径（原文「Issue #136 §10 的 6 条都同意」）。v2 的处理：
  1. 跃迁表：保留「`CLOSED` 为终态」；「关户必须先停用」与「余额恰好为 0」移到关户任务的前置条件；`PENDING_ACTIVATION → DISABLED` 由 v1 的非法改为合法（按 Codex 的意见：先停用、再结算）
  2. 新租户与已有租户默认 `ENABLED`：照做
  3. 关户前置条件：移到关户任务（见 §2 末尾）
  4. `DISABLED` 期间允许调账、拒绝新充值、用量照常入账：作为契约写进 TODO（本任务不涉及充值与用量）
  5. 已关户拒绝的操作：移到关户任务
  6. 每次账户状态跃迁都让 `status_version` +1：照做
- **需要谁拍板**：v2 把关户移出本任务，是在 Kelvin 已确认的范围上收窄、不改变口径；Kelvin 不同意时退回。
- **尚未验证的假设**：MySQL 8.4 对「加带默认值的 NOT NULL 列」走 INSTANT，不重建表。即使不是，表只有 2 行，影响可忽略；迁移测试在 CI 的 MySQL 上实跑。
- **如果假设或口径错误**：口径任何一条以后被改，状态机或 §2 的契约表跟着改，设计版本 +1 后再送审。

## 11. 审查与版本绑定

审查方要回答的五个问题常驻在 [scripts/review_checklist.md](../../scripts/review_checklist.md) 的「设计审查」一节。

- Codex 的批准必须写成 `APPROVED: design v<N>`，`<N>` 取本 Issue 顶部的「设计版本」
- 设计版本一变，之前的 APPROVE 自动作废
- 实质修改：改了契约、表结构、事务边界、状态机、失败语义、不变量控制

### 设计闸门判定

- [ ] 需求、非目标和验收标准明确
- [ ] 关键契约与事务边界明确
- [ ] 触碰的不变量都有控制措施
- [ ] 失败路径都有确定的最终状态
- [ ] 高风险控制都有测试场景
- [ ] 没有未解决的阻断假设
- [ ] 审批绑定到明确的设计版本

### 实现范围（批准后登记为 Worker 任务的 `allowed_change_paths`）

- `alembic/versions/<日期>_<序号>_tenant_account_status.py`（实现时定名）
- `app/models/tenancy.py`（列、枚举、CHECK）
- `app/models/auth.py`（`TENANT_ACCOUNT_STATUS_CHANGED`）
- `app/services/account_status.py`（新）
- `app/schemas/customers.py`（请求模型、客户对象加字段）
- `app/api/admin_customers.py`（路由与错误映射）
- `tests/backend/test_account_status_api.py`（新）
- `tests/backend/test_account_status_service.py`（新）
- `tests/backend/test_admin_customers_api.py`
- `tests/backend/test_migrations.py`
- `docs/api.md`、`docs/database-schema.md`、`docs/TODO.md`

## 12. 版本变更记录

| 版本 | 日期 | 改了什么 | 触发原因 |
| --- | --- | --- | --- |
| v1 | 2026-09-27 | 初稿 | — |
| v2 | 2026-09-28 | 关户（`→ CLOSED`）整体移出本任务，连同余额前置条件、`post_transaction` 的已关户检查、已关户的建项目 / 凭据限制与相关错误码；`PENDING_ACTIVATION → DISABLED` 改为合法；只锁租户行；实现范围去掉记账、调账、凭据相关文件 | Codex 判 v1 REQUEST_CHANGES：关户缺 §112.1 的「无未结财务事件」前置条件，而该条件在 Phase 2 / 4 之前无法验证 |

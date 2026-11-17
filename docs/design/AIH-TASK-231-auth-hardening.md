# AIH-TASK-231..233 与人工 PR 设计：认证与配置加固（重置申请按账号限频、改密通知邮件、`frontend_base_url` 格式校验、认证配置项下界）（已批准 v3）

> **来源**：设计闸门 Issue #751。本文件是 `APPROVED: design v3` 那一版正文的**逐字副本**（送审时状态行为 `READY_FOR_REVIEW`）。审查：Codex 额度不可用期间由 Claude Code 审查。v1 审查发现跨账号并发死锁与状态矛盾，v2 改为 READ COMMITTED；v2 审查发现两条并发回归用例会失效，v3 补上，v3 审查通过、没有阻断项。I1–I3 由 Worker 交付（AIH-TASK-231..233）；I4–I7 的核心文件名含 password，Worker 的敏感路径规则不收，由人工 PR 交付。
> 放进仓库，是因为 OpenClaw Worker 在沙箱里不联网、读不到 GitHub Issue。
> **实现以本文件为准**；与 Issue 不一致时，以 Issue #751 上被批准的 v3 为准。设计要改，就回到 Issue 升版本、重新过闸门，不要直接改本文件。

## 0. 准入判定

| 任务涉及 | 要填的范围 |
| --- | --- |
| 钱包 / 账本 / 定价 / 汇率 / 支付 / 幂等 / 状态机 | **全部章节** |
| 用量摄取 / 认证与会话（集成侧与平台侧）/ Webhook | §1–§7（**§6 不可省** —— 密钥存储与轮换、鉴权、租户过滤、日志脱敏都在那一节） |
| 前端 / 文档 / CI / 脚本 | **不走本闸门**，直接开 PR |
| 既不碰钱、也不碰用量摄取 / 认证与会话 / Webhook 的后端改动 | **不走本闸门**，直接开 PR |

本任务档位：`§1–§7`。理由：四项都落在平台侧认证与会话上（匿名的忘记密码端点、重置成功的副作用、会话令牌寿命、登录锁定与认证限流、重置链接的基址）。不碰钱包、账本、定价、支付与状态机。因为含一个迁移（0040，只加索引）与一个新的 outbox 事件类型，§8–§10 也照填。

---

状态：`READY_FOR_REVIEW`
负责人：Claude
**设计版本**：`v3`
对应需求：spec §53（Forgot Password、Login attempt rate limiting、session/token revocation）、§47（Email 通道）、§66、§74.6、§98.1；Invariant 1、8、13、14；ADR-0009；建立在设计闸门 #32 v5（T0.8d 重置）、#368 v12（门户账号，用户行锁）、#369 v6（`password_version`）、#559 v2（邮件模板层与通知邮件）之上
目标 PR：待开（拆成 `I1`..`I7`：I1–I3 由 Worker 交付为 `AIH-TASK-231`..`233`，I4–I7 为人工 PR，见 §11）

## 1. 目标与边界

### 要解决的问题

`docs/TODO.md`「待办：密码重置与通知投递的几项加固」与「待办：既有配置项缺下界校验」里还开着的四项：

1. **同一邮箱的重置申请没有按账号限频**（`docs/TODO.md:517`）。限流只按来源（`app/api/auth.py:239` 的进程内令牌桶 + nginx `billing_auth` 区，`deploy/nginx/billing.conf:39`、`:223`），换来源就能对一个已知邮箱连发，受害者收到一串重置邮件，也消耗发信域名的声誉。
2. **重置成功后不通知账号本人**（`docs/TODO.md:519`）。账号被盗时，「你的密码刚刚被修改」是受害者唯一可能察觉的信号。
3. **`frontend_base_url` 不校验格式**（`docs/TODO.md:521`）。配错时链接是坏的，症状要到用户点不开才暴露。
4. **既有 TTL / 计数类认证配置项没有下界**（`docs/TODO.md:534`）：0 或负数都不会被拒，各自造成一种「配置对了一半」的故障。

### Kelvin 已定

| 问题 | 决定 |
| --- | --- |
| 按账号限频的间隔 | 同一账号 5 分钟内最多一封重置邮件；做成配置项，默认 300 秒 |
| 改密通知发给谁 | 重置成功的那个账号的邮箱，ADMIN 与 CUSTOMER 都发 |
| 限频口径 | 窗口内**签发过** `PASSWORD_RESET` 令牌就挡，**不论**那张令牌后来用没用掉、过没过期（与 TODO 原文「最近一条未过期令牌」不同，理由见 §2「限频规则」） |
| 生产 `frontend_base_url` 的现值 | 已核实：https，没有路径、查询串、片段与末尾斜杠，满足 §2 的全部规则（含生产必须 https） |
| 交付方式 | I1–I3 由 OpenClaw Worker 交付；I4–I7 由人工会话交付（见 §11「实现拆分」） |

我定的（未单独问，§10 列出供拍板）：

- 被限频的申请**不写审计**，只记一条 INFO 日志（理由见 §6）。
- 加一个索引（迁移 0040，编号已由管理员定归本批），让限频查询只读窗口内的行（理由与不加索引的后果见 §9）。
- `_start_reset` 这一个事务在 MySQL 上用 READ COMMITTED，限频查询用普通读而不是锁读（理由见 §2「限频规则」与 §4）。
- 改密通知邮件带一条「忘记密码」页的链接，不带令牌、IP、设备信息。
- `frontend_base_url` 在 `environment = production` 时必须是 https。

### 可观察的完成标准

1. 同一个 ACTIVE 账号在 `password_reset_min_interval_seconds`（默认 300）秒内的第二次及以后的忘记密码申请：仍返回与第一次逐字节相同的 200 `{}`，耗时仍被补齐到同一下限，但不签发令牌、不写 outbox 行、不写审计。
2. 同一账号的两次并发申请，最终只签发一张令牌、只写一行重置邮件 outbox 行（MySQL 上验证）。
3. 每次重置成功，在同一事务里多写一行 `PASSWORD_CHANGED_EMAIL_REQUESTED` outbox 行；该账号的邮箱最终收到一封英文邮件，说明密码已在某时刻（马来西亚时间，附 UTC）被修改、所有会话已登出，并给出「忘记密码」页的链接。重置失败（令牌无效、密码太弱、账号停用）不写这一行。
4. `frontend_base_url` 为空仍是合法的「未配置」；非空时不是合规的 http/https 根地址（见 §2）的值让进程启动失败；`production` 下 http 也让进程启动失败。
5. 七个认证配置项与新配置项按 §2「配置下界」的表校验，越界让进程启动失败；默认值、`.env.example`、`docker-compose*.yml`、`docs/deployment.md` 里的现值都满足（见 §2 末尾的核对）。

### 明确不做什么

- **不改对外契约**：`/password/forgot`、`/password/reset` 的请求、响应、状态码、错误码、按来源限流都不变；前端不改（忘记密码页文案不提限频）。
- **不做** `password_reset_tokens` 与 `domain_outbox` 的清理归档（`docs/TODO.md:518`，归 T0.9）。
- **不做**「新签发的重置令牌让同一账号之前未用的令牌失效」。
- **不给**接受邀请（首次设密码，`app/services/portal_accounts.py:571`）、命令行建管理员（`app/cli.py:85`）、管理员重置门户 2FA 发改密通知：它们都不是「修改已有密码」。
- **不给**改密通知做 WhatsApp、门户通知、退订或收件人配置；模板只有英文（沿用 AIH-TASK-151）。
- **不改** `password_reset_ttl_seconds`、`pending_token_ttl_seconds`、`enrolment_pending_token_ttl_seconds`、`customer_invitation_ttl_seconds` 已有的下界，也**不加**上界（只在新配置项上同时定上下界）。唯一的牵连：新配置项要求 `password_reset_min_interval_seconds ≤ password_reset_ttl_seconds`，于是重置令牌寿命实际不能低于 60 秒（见 §2「配置下界」）。
- **不改** `docker-compose.yml` 的环境变量转发：这几个认证配置项今天都不转发（生产取默认值），新配置项照此办理。
- **不改** `TokenBucket` 构造时对非正数的拒绝（`app/core/ratelimit.py`），它作为第二道检查保留。

### 现有行为与证据

- `request_reset`（`app/services/password_reset.py:76`）：整个函数被补齐到 `_MIN_DURATION_SECONDS = 0.12`（`:59`、`:110`、`:123`），`on_queued` 触发也在补齐窗口内（`:113`–`:118`）。`_start_reset`（`:138`）：先不加锁按邮箱读用户（`:160`，避免对随机邮箱留下间隙锁），存在时再按主键 `FOR UPDATE` + `populate_existing` 复查（`:174`–`:179`），非 ACTIVE 返回 `None`（`:180`），否则同一事务写令牌、outbox、审计（`:193`–`:240`）。
- `reset_password`（`:244`）：锁顺序「先用户、后令牌」（`:292`、`:307`），消费令牌、换哈希、`password_version + 1`、清锁定、吊销全部刷新令牌、作废其余重置令牌、写审计，一次提交（`:325`–`:386`）。不写 outbox。
- `password_reset_tokens` 只有 `ix_password_reset_tokens_user (user_id, used_at)` 与 `token_hash` 唯一索引（`app/models/auth.py:466`、`:482`）；同一张表还装着门户邀请令牌（`purpose = INVITATION`，`:433`–`:444`）。
- 邮件：`app/core/email_templates.py` 的模板标题固定、`render_email` 要求参数键集合完全相等（`:127`–`:141`）；`app/tasks/outbox.py` 的 `_RENDERERS`（`:360`）与 `handled_event_types()`（`:372`）决定 `recover` 重投哪些类型（`:762`–`:768`）与 `outbox_backlog` 告警（`app/services/alerts.py:837`）；`_settle` 成功或死信都清空 `payload_json`（`:477`–`:500`）；`recover` 每 60 秒一次（`app/core/celery_app.py:50`）。
- `frontend_base_url`：`app/core/config.py:253`，无校验；四个渲染函数与支付建单都 `strip().rstrip("/")` 后直接拼 `f"{base}/<路径>"`（`app/tasks/outbox.py:86`、`:101`、`:142`、`:159`、`:208`、`:258`、`:284`、`:335`；`app/services/payments.py:629`）；为空时拒绝渲染（`:87`）或支付 503。前端没有 Vite `base` 与路由 `basename`，所有路由从根开始（`frontend/src/routes/paths.ts`）。compose 默认值是 http 的本机地址（`docker-compose.yml:89`），生产覆盖文件不改它（`docker-compose.prod.yml:22` 只改 `BILLING_ENVIRONMENT`）。
- 认证配置项：`app/core/config.py:183`–`:214`，都是裸 `int`。使用点：访问令牌 `exp`（`app/core/tokens.py:125`）、刷新令牌绝对期限（`app/services/auth.py:281`）与 cookie `max_age`（`app/api/auth.py:107`）、闲置期限从**当前这张刷新令牌的签发时刻**算（`app/services/auth.py:670`）、锁定阈值（`:193`）与递增倍数 1/2/4（`:43`–`:50`）、进程内令牌桶（`app/main.py:197`–`:200`，非正数时 `TokenBucket` 抛 `ValueError`，`app/core/ratelimit.py`）。
- 前端在页面加载时静默刷新一次（`frontend/src/auth/AuthProvider.tsx:39`），之后**只在 401 时**刷新（`frontend/src/api/client.ts:160`），刷新端点过进程内令牌桶（`app/api/auth.py:177`）。
- 容器里没有 `.env` 文件（`.dockerignore` 排除），配置只来自 compose 转发的环境变量；这七个认证配置项都不转发（`docker-compose.yml:57`–`:126`），`.env.example` 与 `docs/deployment.md` 也都没有设它们。
- 部署：`deploy/deploy.sh:358`–`:359` 先用**新镜像、同一套环境变量**跑 `alembic upgrade head`，而 `alembic/env.py:73` 调 `get_settings()`；配置校验不过时迁移这一步失败，脚本退出、旧版本继续跑。

## 2. 设计概要

### 数据流

```text
POST /password/forgot（不变：按来源限流 → request_reset，整体补齐到 0.12 秒）
  └─ _start_reset：
       新：MySQL 上本事务用 READ COMMITTED（会话的第一个动作，见「限频规则」）
       不加锁按邮箱读用户（不变）→ 存在则按主键锁用户并复查（不变）→ 非 ACTIVE：返回 None（不变）
       新：普通读「本账号窗口内的重置令牌」── 有 → INFO 日志、返回 None（不写任何行）
       └─ 没有 → 令牌 + 重置邮件 outbox + 审计，一次提交（不变）

POST /password/reset（不变的判定与写入）
  └─ reset_password：…… 换哈希、吊销会话、作废其余令牌、审计
       新：同一事务再写一行 PASSWORD_CHANGED_EMAIL_REQUESTED（不 delay）
  └─ 一次提交

recover（每 60 秒）──► deliver ──► _render_password_changed ──► Adapter ──► SMTP（现有邮件路径）

进程启动 / alembic upgrade：Settings() 校验 frontend_base_url 与各配置项下界，不过即抛错
```

### 限频规则（`_start_reset`，I4）

- **位置**：在「锁用户行并复查为 ACTIVE」之后、生成令牌之前。非 ACTIVE 与不存在的账号仍按原路径返回，不做这一步。
- **判据**：`cutoff = moment - password_reset_min_interval_seconds`；在同一事务、**仍持有用户行锁**时执行一条**不加锁**的读（不带 `with_for_update()`）

  ```python
  select(PasswordResetToken.id)
  .where(
      PasswordResetToken.user_id == user.id,
      PasswordResetToken.purpose == TokenPurpose.PASSWORD_RESET,
      PasswordResetToken.created_at > cutoff,
  )
  .limit(1)
  ```

  有一行 → 被限频。窗口是左开的：恰好 `interval` 秒前签发的令牌不再挡（`created_at == cutoff` 放行）。
- **为什么不论用没用掉、过没过期**（Kelvin 已定）：要限的是「发出去的信」，而每张 `PASSWORD_RESET` 令牌就是一封信。只看未用掉的会被绕开：攻击者拿不到令牌，但重置成功或管理员停用都会把令牌置为已用，且窗口（≤ 1 小时）远短于令牌寿命之外的任何清理周期。已接受的代价：用户刚重置成功、又在 5 分钟内点忘记密码，不会再收到信。
- **本事务的隔离级别：MySQL 上用 READ COMMITTED**（仓库里的新模式，见下）。
  - **问题**：MySQL 默认 REPEATABLE READ，一致性快照在事务的**第一次非锁定读**时建立 —— 就是 `:160` 那次按邮箱的不加锁读。同一账号两个并发申请时，后到者的快照早于先到者提交；即使它在用户行锁上排队、拿到锁时先到者已提交，RR 下的普通 SELECT 仍读不到先到者的令牌，于是两封都发。
  - **为什么不用锁读**（v1 的做法，审查阻断）：`FOR UPDATE` 范围读在 RR 下带间隙锁。两个**不同**账号的窗口查询落在同一个间隙（最典型是索引末尾的 supremum，间隙锁彼此兼容），两边都持有间隙锁后各自 INSERT，插入意向锁互相被挡，InnoDB 报 1213 死锁；被回滚的一方 500，而只有存在且 ACTIVE 的账号走得到这一步，500 成了弱的账号存在信号。
  - **READ COMMITTED 为什么够**：RC 下每条一致性读语句各取一个最新的读视图。同一账号的申请由用户行锁串行化（`:174`–`:179`）；先到者在**提交之后**才释放用户行锁，后到者拿到锁之后才执行令牌查询，那条普通 SELECT 的读视图一定包含先到者已提交的令牌 → 被限频。RC 下普通读不加任何锁，`FOR UPDATE` 按主键锁用户行只加记录锁，INSERT 不会被本类事务的间隙锁挡（RC 不为查找加间隙锁），所以两个不同账号的申请之间没有任何锁交集（死锁论证见 §4）。
  - **怎么设**：`_start_reset` 里 `with session_factory() as session:` 之后的**第一句**：

    ```python
    if session.get_bind().dialect.name == "mysql":
        session.connection(execution_options={"isolation_level": "READ COMMITTED"})
    ```

    必须在这个会话的任何 `execute` 之前：`Session.connection()` 的 `execution_options` 只在为本事务取连接时生效一次，事务开始后再设无效（SQLAlchemy 2.0「为单个事务设置隔离级别」的标准写法；仓库锁定 `sqlalchemy>=2.0`，当前 2.0.52）。只在 `mysql` 方言上设：SQLite 方言只接受 `SERIALIZABLE` / `READ UNCOMMITTED` / `AUTOCOMMIT`，设 RC 会抛错；SQLite 本来就没有行锁与快照问题，单元测试照旧。按方言名分支照 `app/services/usage_billing.py:630`–`:634` 的写法。
  - **不泄漏到连接池**：这个会话只有一个事务（签发路径 `commit` 后 `return`，限频与非 ACTIVE 路径直接 `return`，异常路径向上抛），`with` 结束时会话关闭、连接归还连接池；SQLAlchemy 在连接归还时把经 `execution_options` 改过的隔离级别还原为引擎默认值（RR），下一个借到这条连接的事务看到的是 RR。§7 用「连接池只有一条连接」的引擎验证这一点。
  - **为什么是新模式、为什么只在这里**：仓库里之前只有 `usage_billing.py:633` 为命名锁专用连接设 `AUTOCOMMIT`，没有切换事务隔离级别的先例。这里切换的理由是上面那个具体的死锁；范围只限 `_start_reset` 这一个事务（不改引擎默认值、不改 `session_scope`、不改 `reset_password` 等其他认证事务 —— 它们靠锁读，在 RR 下现有的并发论证不变）。RC 与 binlog：生产与 CI 的 MySQL 8.4 binlog 格式是 ROW（`docker-compose.yml` mysql 服务注释「格式也已经是 ROW（实测确认）」），RC 在 ROW 格式下安全（STATEMENT 格式下 MySQL 会拒绝 RC 事务的写入）。
  - **对本事务其余语句的影响**：按邮箱的不加锁读、按主键的加锁复查、三次 INSERT 的语义在 RC 下不变；`:157`–`:159`「不对随机邮箱留间隙锁」的考虑在 RC 下更彻底成立。
- **被限频时**：`logger.info("Password reset request throttled for this account", extra={"user_id": user.id})`；不带邮箱、不带令牌；`return None`，会话关闭即回滚、释放锁；不调 `on_queued`（没有行可投）。
- **对外**：响应、状态码、耗时都不变：被限频路径的真实工作是三次 SELECT，少于签发路径（多三次 INSERT 与一次提交），都在 0.12 秒的下限之内，由 `request_reset` 的 `finally` 补齐（`app/services/password_reset.py:120`–`:123`）。被限频不产生任何外部可见的差异，所以不打开新的用户枚举通道：对不存在的邮箱连发两次与对存在的邮箱连发两次，外部看到的完全一样。
- **限频不会挡住真用户拿到可用链接**：配置要求 `interval ≤ password_reset_ttl_seconds`（见「配置下界」）。被限频意味着窗口内已有一封信发到**这个账号自己的邮箱**，那封信里的令牌签发不到 `interval` 秒、寿命 ≥ `interval`，此刻一定还没过期。攻击者每 5 分钟替受害者申请一次，受害者收件箱里始终有一条有效链接（被别人申请的重置邮件也发给账号本人，不发给申请者）。

### 数据库（迁移 0040，I1）

- 迁移文件 `alembic/versions/20261116_0040_reset_request_index.py`，`revision = "0040_reset_request_index"`，`down_revision = "0039_payment_receipts"`（编号已定，见本节末条）。⚠️ revision 不超过 32 个字符：`alembic_version.version_num` 是 `VARCHAR(32)`（先例 `alembic/versions/20261018_0023_wallet_ledger_reference_index.py:52`–`:53`）。
- 只做一件事：`CREATE INDEX ix_password_reset_tokens_user_purpose_created ON password_reset_tokens (user_id, purpose, created_at)`；`downgrade` 只删这个索引。照 0011（`alembic/versions/20260928_0011_audit_logs_query_indexes.py`）的文件头写 §132 第 13 条分析：不改列、不写数据；MySQL 8.4 InnoDB 加二级索引默认在线（`ALGORITHM=INPLACE`、`LOCK=NONE`），表照常读写；失败时没有半成品（只有一个索引），重跑安全；回滚只删索引。
- 模型 `PasswordResetToken.__table_args__`（`app/models/auth.py:480`）加同名 `Index`；`ix_password_reset_tokens_user` 保留不动（它是 `user_id` 外键依赖的索引）。
- 效果：限频查询走 `(user_id, purpose, created_at)` 的范围扫描，只读窗口内的行（正常 0–1 行），不加锁；耗时不受该账号历史令牌与邀请令牌数量影响（不加索引时的后果见 §9 备选 A）。
- 编号已定（管理员核实）：0040 归本批；E 批没有迁移，最新是 0039；L 批用 0041、以 `0040_reset_request_index` 为 `down_revision`。

### 改密通知（I5 渲染，I6 写 outbox 行）

- 事件类型常量 `EVENT_PASSWORD_CHANGED: Final = "PASSWORD_CHANGED_EMAIL_REQUESTED"`，定义在 `app/services/password_reset.py`（照 `EVENT_PASSWORD_RESET` 的写法，由 `app/tasks/outbox.py` 导入）；`aggregate_type = AGGREGATE_USERS`（`"users"`），`aggregate_id = str(user.id)`。
- **写入（I6）**：`reset_password` 在 `record_audit`（`:377`）之后、`commit`（`:386`）之前 `session.add` 一行：`status = PENDING`、`attempt_count = 0`、`next_retry_at = created_at = moment`；**不** `delay()`，只靠 `recover`（最多晚 60 秒；重置端点也不需要 `on_queued` 那种补齐窗口内的触发）。任何一步失败，整个重置回滚：令牌没被消费、密码没变、没有通知行。
- **payload**（`json.dumps(sort_keys=True)`）恰好两个键：`to` = 锁住的那个用户行的 `email`；`changed_at` = `moment.replace(microsecond=0).isoformat()`（不带时区的 UTC，整秒）。不放 IP、User-Agent、角色、租户、令牌。
- **渲染（I5）** `_render_password_changed(settings, payload)`，登记进 `_RENDERERS`：`frontend_base_url` 为空；键集合不恰好是 `{to, changed_at}`；`to` 不是非空字符串或含 `\r` / `\n`；`changed_at` 经现有 `_malaysia_time` 读不出 —— 任一成立记**固定文本**的 ERROR（不带任何 payload 值）、返回 `None`（按现有规则算一次失败，重试后死信）。否则 `OutboundMessage(channel=EMAIL, recipient=to, template=TEMPLATE_PASSWORD_CHANGED, params={"changed_at": _malaysia_time(...), "link": f"{base}/forgot-password"})`。路径取前端 `ROUTES.forgotPassword`（`frontend/src/routes/paths.ts`），由 `tests/backend/test_password_reset_link.py` 机械比对，并核对它不在 `RequireAuth` 之内。
- **模板**（`app/core/email_templates.py`）`TEMPLATE_PASSWORD_CHANGED: Final = "password_changed"`，必需参数 `{changed_at, link}`；标题固定为 `Your Acuven Billing password was changed`；正文：

  ```text
  The password for your Acuven Billing account was changed at $changed_at.
  For your security, every signed-in session has been signed out.

  If you made this change, you do not need to do anything.

  If you did not make this change, someone else may have access to your email.
  Reset your password again straight away from this page, and contact Acuven:
  $link
  ```

  文案要素：发生了什么（密码被改）、何时（马来西亚时间到分钟，附 UTC）、已采取的措施（全部会话登出 —— 刷新令牌全部吊销，`app/services/password_reset.py:359`–`:363`；访问令牌因 `password_version` 递增在下一次使用时失效，`app/api/auth.py:319`）、不是本人时怎么办。不写邮箱、角色、IP、设备、令牌；链接不带查询参数。ADMIN 与 CUSTOMER 用同一个模板。
- 投递、重试、死信、`outbox_backlog` 告警沿用现有邮件路径；`handled_event_types()` 因登记进 `_RENDERERS` 自动包含它；`_settle` 照旧清空 payload（里面有邮箱）。

### `frontend_base_url` 的格式（I3）

`app/core/config.py` 新增 `require_frontend_base_url(value: str) -> str` 与 `@field_validator("frontend_base_url")`，另加一个 `@model_validator(mode="after")` 管 production：

1. 去掉首尾空白；结果为空 → 返回 `""`（「未配置」仍合法；`"   "` 也规范成 `""`，与现有判空写法一致）。
2. 含任何空白字符、ASCII 控制字符（`< 0x20` 与 `0x7f`）或反斜杠 → 拒绝。理由：`urlsplit` 会悄悄去掉制表符与换行再解析，校验看到的与拼进邮件正文的不是同一个字符串；反斜杠在浏览器里被当成 `/`。
3. 含 `?` 或 `#` → 拒绝。理由：渲染直接在后面接 `/<路径>?token=…`；`urlsplit` 对空查询串（末尾一个 `?`）给出 `query == ""`，只查 `query` 拦不住。
4. `urlsplit` 后：`scheme` 是 `http` 或 `https`；`hostname` 非空；没有 `username` / `password`；`port` 读得出（非数字或超出 0–65535 时 `ValueError`）且不为 0；`path` 只能是 `""` 或 `"/"`。理由：前端从根路由（没有 `base` / `basename`），带路径前缀拼出的每条链接都是 404。
5. 返回去掉末尾 `/` 的值。现有调用方的 `strip().rstrip("/")` 不动（幂等）。
6. `environment == "production"` 且值非空而 `scheme != "https"` → 拒绝。理由：重置与邀请链接带着一次性令牌，生产的会话 cookie 是 `Secure`（`app/core/config.py:218`），http 链接会让令牌明文过网；本地默认值是 http，`local` 照常允许。

校验失败抛 `ValueError`，`Settings()` 构造失败：API、worker、beat 与部署时的迁移步骤都起不来（见 §5、§8）。

### 配置下界（I2）

| 配置项 | 默认 | 下界 | 理由（每条都按现有代码算） |
| --- | --- | --- | --- |
| `access_token_ttl_seconds` | 600 | `ge=60` | 前端只在 401 时刷新，每次刷新过一次进程内令牌桶与 nginx `billing_auth`（每来源每分钟 10 次）。寿命低于 60 秒时一个活跃会话每分钟刷新不止一次，同一出口 IP 后的几个会话就会把认证限流吃满，表现为「用着用着被踢回登录页」。60 秒让每个会话每分钟至多刷新一次 |
| `refresh_token_idle_seconds` | 1800 | `≥ access_token_ttl_seconds + 60`（模型校验） | 闲置期限从当前刷新令牌的签发时刻算（`app/services/auth.py:670`），而这张刷新令牌与访问令牌同时签发、访问令牌过期后才会被用来刷新。闲置期限 ≤ 访问令牌寿命时，**每一次**刷新都已过闲置期限，活跃用户每 10 分钟被登出一次。多出的 60 秒是访问令牌过期后用户下一次操作的宽限：比它短，用户停顿一下就被当作闲置 |
| `refresh_token_ttl_seconds` | 43200 | `≥ refresh_token_idle_seconds`（模型校验） | 绝对寿命短于闲置期限时闲置期限永远不起作用，配置的「闲置 30 分钟登出」是假的；它也同时是 cookie 的 `max_age` |
| `login_max_failures` | 5 | `ge=3` | 1 时一次输错就锁 15 分钟，任何知道邮箱的人每个锁定周期发一次请求就能把账号一直锁着；2 时大小写锁定键加一次手误就锁。3 次给真人留出两次手误 |
| `login_lockout_seconds` | 900 | `ge=300` | 锁定防的是针对单个账号的分布式猜测：在递增封顶（×4）时，每个账号的猜测速率上限是 `login_max_failures / (4 × lockout)`。下界组合（3 次、300 秒）约每天 216 次；默认（5 次、900 秒）约每天 120 次；若允许 60 秒则约每天 1080 次，接近按来源限流的量级、锁定形同虚设。300 秒仍允许把默认值调低到三分之一 |
| `auth_rate_limit_burst` | 20 | `ge=3` | 一次冷启动登录要连过三次令牌桶：页面加载的静默刷新、`/login`、`/login/totp`（`app/api/auth.py:121`、`:153`、`:177`）。容量小于 3 时，没有任何攻击，正常用户也登不完一次 |
| `auth_rate_limit_per_minute` | 10 | `ge=3` | 被限流的用户一分钟内要能补回完整的一次登录（3 次）；低于它，一次 429 之后要等一分多钟才能再试 |
| `password_reset_min_interval_seconds`（新） | 300 | `ge=60`、`le=3600`，且 `≤ password_reset_ttl_seconds`（模型校验） | 下界：低于 60 秒时，对一个账号每分钟可发的信已与按来源限流同量级，控制形同虚设。上界：丢了信的真用户最多等 1 小时。`≤ ttl`：保证被限频时窗口内那封信的链接一定还有效（见「限频规则」最后一条），否则攻击者可以在每封信过期后、窗口重开前替受害者抢先申请，让受害者长期拿不到可用链接 |

- 前三条与最后一条的跨字段关系写进一个新的 `@model_validator(mode="after")`（不并进 `_status_webhook_timings_fit`，各管各的），错误信息只说哪条关系不成立，不回显值以外的东西。
- 非跨字段的下界用 `Field(default=…, ge=…)`，与文件里其他配置项同一写法。
- 新配置项环境变量名 `BILLING_PASSWORD_RESET_MIN_INTERVAL_SECONDS`；放在 `password_reset_ttl_seconds`（`app/core/config.py:235`）之后，注释写清用途与上面三条理由。
- **现值核对**：默认值全部满足（60 ≤ 600；1800 ≥ 660；43200 ≥ 1800；5 ≥ 3；900 ≥ 300；20 ≥ 3；10 ≥ 3；60 ≤ 300 ≤ 3600 且 300 ≤ 1800）。`.env.example`、`docker-compose.yml`、`docker-compose.prod.yml`、`docs/deployment.md` 都没有设这几个键，容器里也没有 `.env`，生产取的就是默认值。仓库里的测试有四处低于新下界或违反新关系：`tests/backend/test_auth_api.py:185`（`burst=2`）与 `:206`（`burst=1`）由 I2 改为 `burst=3` 并相应加请求次数；`tests/backend/test_config.py:159` 的 `password_reset_ttl_seconds=1` 由 I2 改为 300（那条用例只证明「正数照常通过」）；`tests/backend/test_password_reset.py:354` 的 `Settings(password_reset_ttl_seconds=60)` 要另传 `password_reset_min_interval_seconds=60`，但 Worker 的敏感路径规则收不了这个文件，改由管理员在登记 PR 里预先改（`Settings` 是 `extra="ignore"`，I2 之前多传的这个键被忽略，见 §8「前置」）。直接构造 `TokenBucket` 的用例（如 `tests/backend/test_password_reset_api.py:287`）不经 `Settings`，不受影响。

### 锁顺序

全局仍是「（租户 →）用户 → 令牌」：`request_reset` 只锁用户行（RC，记录锁），令牌表上只有自己 INSERT 的新行；`reset_password` 用户 → 令牌（不变，RR）；`accept_invitation` 租户 → 用户 → 令牌（不变，RR）；门户停用 租户 → 用户 → 令牌（不变，RR）。没有反向的路径；完整的死锁论证见 §4「并发」。

### 事务边界

- 忘记密码：签发路径不变（令牌、outbox、审计一次提交）；被限频路径不写任何行，会话关闭即回滚。
- 重置：原事务里多一行 outbox，与改密码、吊销会话、作废令牌、审计一次提交（INV-13）。
- 配置：进程启动时一次性校验，不涉及数据库。

### 时间语义

全部是不带时区的 UTC。限频窗口按 `moment` 与库里的 `created_at` 比较；MySQL `DATETIME` 不存小数秒、对小数秒四舍五入，所以窗口边界有不超过 1 秒的误差，不影响目的；测试用整秒的 `now`。通知里的时刻存整秒 UTC，显示时换成马来西亚时间到分钟（现有 `_malaysia_time`）。

### 金额与精度

不涉及金额。

## 3. 不变量影响矩阵

| 不变量 | 是否触碰 | 可能怎样被破坏 | 设计控制 | 验证方式 |
| --- | --- | --- | --- | --- |
| INV-1 中心故障不中断 AI | 是（间接） | 新校验让生产配置不合规时 api 进程起不来；AI 模块与 api 同进程（ADR-0012），会一起不可用 | 部署脚本先用新镜像与同一套环境变量跑迁移，迁移经 `get_settings()` 先撞上校验、部署中止、旧版本继续跑（`deploy/deploy.sh:358`–`:359`）；生产的 `BILLING_FRONTEND_BASE_URL` 已核实满足 I3 的全部规则；新下界下生产现值（即默认值）全部满足 | §7「配置下界」「frontend_base_url」 |
| INV-2 事件不重复扣费 | 否 | 不碰用量事件与计费 | — | — |
| INV-3 支付不重复入账 | 否 | 不碰支付；`frontend_base_url` 的规范化（去末尾 `/`）与支付建单的现有 `strip().rstrip("/")` 幂等，跳回地址不变 | — | `tests/backend/test_payment_create.py:632` 现有用例照常通过 |
| INV-4 余额只经账本变动 | 否 | 不碰钱包 | — | — |
| INV-5 历史账本不可变 | 否 | 不碰账本 | — | — |
| INV-6 事件保留版本引用 | 否 | 不碰用量事件与定价版本 | — | — |
| INV-7 客户不可见成本毛利 | 否 | 新邮件只有改密时刻与一条前端链接，没有任何金额 | — | — |
| INV-8 租户不可互访 | 否（核对过） | 改密通知发错人；限频按错账号计 | 收件人取自同一事务里锁住的那个用户行的 `email`，不读租户联系邮箱、不读请求参数；限频按 `user_id` 计，只看这个用户的 `PASSWORD_RESET` 令牌 | §7「通知 payload」「限频按账号」 |
| INV-9 对话内容不入库 | 否 | 不碰 AI 对话 | — | — |
| INV-10 金额用 Decimal | 否 | 不涉及金额 | — | — |
| INV-11 event_id 至多一次财务效果 | 否 | 不碰用量事件 | — | — |
| INV-12 定稿对账单不可变 | 否 | 不碰对账单 | — | — |
| INV-13 状态与事件原子提交 | 是 | 密码改了而通知行没写，或通知行写了而密码没改 | 通知 outbox 行与改密码、吊销会话、审计同一事务提交；被限频路径不写任何行 | §7「同事务」「事务中途失败」 |
| INV-14 队列丢失不毁持久工作 | 是 | Redis 丢失、`delay` 丢失时通知永不投递 | 通知只靠库里的 outbox 行与 `recover`（每 60 秒）；新类型登记进 `_RENDERERS`，因而进 `handled_event_types()` 的恢复名单与积压告警 | §7「Redis/Celery 丢失」 |

## 4. 状态与并发

### 状态

不新增状态机。改密通知 outbox 行沿用现有小状态机（`PENDING` → `SENT` / `FAILED`，`app/models/auth.py:491`）；令牌与用户状态的跃迁不变。

### 并发

- **`_start_reset` 事务持有与等待的锁（MySQL，RC）**：按邮箱的普通读 —— 无锁；按主键 `FOR UPDATE` 复查用户 —— `users` 上这一行的记录锁（RC 不加间隙锁）；令牌窗口查询 —— 普通读，无锁；三次 INSERT（令牌、outbox、审计）—— 新行的隐式记录锁，外键检查对 `users` 上本账号那一行加共享锁（本事务已持有它的排他锁）。本事务**可能等待**的只有两类：(a) 拿用户行锁时等持有同一账号用户行的事务；(b) INSERT 令牌时，插入意向锁被**别的** RR 事务（`reset_password`、门户停用、重发邀请、接受邀请里按 `user_id` 的范围 UPDATE）在 `password_reset_tokens` 索引上留下的间隙锁挡住。
- **为什么不会死锁**：死锁要求本事务等待的那个事务反过来等待本事务持有的锁。
  - (a)：持有同一账号用户行的事务在拿到用户行之后只锁这个账号的令牌与会话，不会等本事务的任何锁（本事务此时还什么都没持有）；它提交后本事务继续。
  - (b)：挡住本事务的是在**另一个账号** X 上工作的事务（它持有 X 的用户行），它此后只会去锁 X 的令牌、刷新令牌与 X 的用户行，或插入 `domain_outbox` / `audit_logs`；本事务持有的只有本账号 Y 的用户行和**尚未插入成功**的令牌行（插入正在等待，行还不存在），以及已插入的 outbox / 审计行（没有任何事务在这两张表上做范围加锁：投递按主键 UPDATE，`app/tasks/outbox.py:429`–`:441`）。所以它不等本事务，没有环。
  - 两个 `_start_reset`（不同账号）之间：两边都在 RC 下，都不持有间隙锁，用户行不同，INSERT 互不阻挡 —— 审查发现的 supremum 场景在这里不存在。
  - 同一账号的两个 `_start_reset`：只在用户行上排队，见下一条。
- **同一账号两次并发的忘记密码**：两者都在用户行 `FOR UPDATE` 上排队。先到者签发并**提交后**释放锁；后到者拿到锁之后才执行令牌窗口查询，RC 下这条普通读的读视图在执行时才建立，一定包含先到者已提交的令牌 → 被限频。最终一张令牌、一行重置邮件。若删掉设置 RC 的那一句（回到 RR），后到者读的是建立在第一次非锁定读时的旧快照，两封都发（§7 场景 ① 就是为了让这种删除变红）。
- **忘记密码与重置并发（同一账号）**：都先锁用户行，排成一队，无环。重置先提交时，忘记密码的窗口查询看到的令牌都还在（重置只把 `used_at` 置上），窗口内仍算签发过 → 限频（符合「发出去的信」口径）。
- **忘记密码与门户停用、接受邀请并发**：停用先提交 → 忘记密码锁到 `DISABLED`，返回 `None`（不变）；`INVITED` 账号在复查处就返回，不会走到令牌查询与 INSERT。
- **两条有效链接同时提交重置**：现有用户级串行化只让一条成功（`app/services/password_reset.py:280`–`:296`）；失败的那条抛 `InvalidResetToken`、整个回滚，不写通知行。最终一行通知。
- **若仍被选为死锁牺牲者**（上面的论证未覆盖到的、例如将来别的事务改了加锁方式）：最终状态见 §5，不在补齐窗口内重试。
- **数据库负责的唯一性**：无新增；「一个窗口至多一封」由用户行锁串行化 + RC 下拿锁之后的普通读保证，不靠唯一约束（理由见 §9 备选 D）。
- **幂等**：通知 outbox 行一次成功重置写一行；投递是至少一次（worker 在 SMTP 之后、结算之前崩溃会再发一封同样的信，与现有邮件同一口径，`app/tasks/outbox.py:11`–`:15`），内容相同、无害。
- **重试层**：限频与改密不重试；邮件投递在 outbox 层按现有退避重试。

## 5. 失败与恢复矩阵

| 失败点 | 对外结果 | 数据最终状态 | 是否可重试 | 恢复来源 | 告警 |
| --- | --- | --- | --- | --- | --- |
| 配置不合规（下界、跨字段关系、`frontend_base_url`） | 进程起不来；部署时迁移步骤失败、部署中止 | 旧版本继续跑，数据不变 | 改配置后重新部署 | 管理员按错误信息改 `.env` / compose | 部署工作流失败；本地启动直接报错 |
| 被限频 | 与正常申请相同的 200 `{}`，耗时相同 | 不写任何行 | 窗口过后可再申请 | — | 无（INFO 日志） |
| 限频查询数据库错误（断连、锁等待超时） | 与现有签发路径出错相同（异常向上抛，补齐后 500） | 事务回滚，不写任何行 | 用户重试 | — | 现有 5xx 日志 |
| 签发时被选为死锁牺牲者（MySQL 1213；按 §4 的论证本设计下不应出现） | 与上一行相同：`OperationalError` 向上抛，`request_reset` 的 `finally` 照样补齐到下限，端点 500 | 本事务整体回滚：没有令牌、outbox、审计行；另一方照常提交 | 用户重试（下一次申请在窗口内若另一方是同一账号的申请则被限频，否则签发）；**不在补齐窗口内自动重试** —— 重试会让「存在且 ACTIVE」的路径多出一段可测耗时，而 RC 已消除已知的死锁来源 | — | 现有 5xx 日志；§7 跨账号用例防回归 |
| 重置时写通知行失败 / 提交失败 | 与现有重置出错相同（500） | 整个重置回滚：令牌未消费、密码未变、会话未吊销、没有通知行 | 用户拿同一条链接重试 | — | 现有 5xx 日志 |
| Redis / Celery 丢失 | 无（重置照常成功） | 通知行 `PENDING` 留在库里 | 是 | `recover` 每 60 秒重投 | `outbox_backlog`（超过 10 分钟 P2） |
| 通知渲染失败（`frontend_base_url` 为空、payload 不合规） | 用户没收到通知；重置照常成功 | 行重试到 `FAILED`，payload 清空 | 是（至死信） | 配好基址后新行正常；死信行按 runbook 处置（不自动重发） | `outbox_backlog`（死信 P2） |
| SMTP 超时 / 拒收 | 同上 | 同上 | 是（至死信） | 现有 outbox 重试 | `outbox_backlog` |
| 重复请求（同一账号连点忘记密码） | 每次相同的 200 | 窗口内只有第一次签发 | — | — | — |
| 迁移 `0040_reset_request_index` 中途失败 | 部署中止 | 索引要么没建，要么已建（只有一个 DDL）；`alembic_version` 停在 `0039_payment_receipts` | 是 | 已建时 `DROP INDEX ix_password_reset_tokens_user_purpose_created` 后重新部署（照 0011 文件头） | 部署工作流失败 |
| I4 已上线而 0040 未生效（不可能按部署顺序出现，见 §8） | 限频照常正确 | — | — | — | — |

## 6. 数据与安全边界

- **鉴权主体**：`/password/forgot` 匿名（按来源限流不变）；`/password/reset` 凭重置令牌（不变）。本批不新增接口与权限。
- **租户过滤**：不涉及租户维度的查询。限频只按 `user_id`；改密通知的收件人只取自那个用户行。
- **用户枚举**：限频结果绝不进入响应体、状态码、响应头或耗时（§2「限频规则」）；被限频与不存在的账号一样不调用 `on_queued`。日志只在服务端。
- **日志**：被限频记 INFO，固定文本，只带 `user_id`；不带邮箱、令牌、来源 IP（来源 IP 已在访问日志里）。改密通知渲染失败记固定文本的 ERROR，不带任何 payload 值。配置校验失败的错误信息里可以有配置值本身（基址不是秘密），没有凭据。
- **审计**：
  - 被限频的申请**不写审计**。理由：(1) 它没有改变任何状态，也没有发出任何东西；(2) 审计表只追加、不可删（spec §66），每个被限频的请求写一行，就是让匿名调用者按来源限流的速率往一张永久表里写数据；(3) 与现有「不存在或停用的账号」路径一致（只记 INFO，`app/services/password_reset.py:184`–`:188`）。窗口里第一次申请照旧写 `PASSWORD_RESET_REQUESTED`，所以审计链上每 5 分钟至多一条，足以看出「有人在反复申请」。
  - 重置成功照旧写 `PASSWORD_RESET`；通知 outbox 行不另写审计（outbox 行本身就是记录）。
- **邮件内容**：改密通知不含令牌、IP、设备、角色、租户名；只有改密时刻与一条不带参数的前端链接。payload 含邮箱，投递成功或死信后由 `_settle` 清空（现有规则）。
- **链接安全**：`frontend_base_url` 只来自配置，不来自请求头（现有理由，`app/core/config.py:246`–`:252`）；新校验拒绝能让拼接结果变形的字符（空白、控制字符、反斜杠、`?`、`#`）与带路径前缀的值；生产要求 https。
- **密钥**：不新增、不读取任何密钥。
- **prompt / response / 客户数据**：不处理。
- **数据保留**：限频把每个账号的重置令牌增长压到每 5 分钟至多一行，`password_reset_tokens` 的清理仍归 T0.9（`docs/TODO.md:518`）；新索引随表增长，量级与 `ix_password_reset_tokens_user` 相同。改密通知 outbox 行长期保留，payload 在结算时清空。

## 7. 测试证据计划

遵守仓库根 `CLAUDE.md`「写后端测试的约定」：SQL 断言不写方言字面量；MySQL 专属用例放 `BILLING_TEST_DATABASE_URL` 守卫下；确认对方在等行锁时查 `performance_schema.data_lock_waits`（联 `data_locks`，按 `DATABASE()` 与表名过滤，写法照 `tests/backend/test_session_revocation_concurrency.py` 的 `_WAITING_ON_TABLE`），不查 `INNODB_TRX`。

| 风险/需求 | 测试层级 | 场景 | 预期结果 | 任务 |
| --- | --- | --- | --- | --- |
| 迁移与模型 | integration（SQLite + MySQL） | 升到 head 后 `password_reset_tokens` 的索引；`0040_reset_request_index` 的 `down_revision` 与长度（≤ 32）；降到 0039；模型声明与迁移字面量一致；0025 用例里「索引只有 0004 那一个」的两处断言改为只约束 0025 自己（不放宽 0025 的检查）；`tests/backend/test_payment_receipts_migration.py:1784` 的 `_at_0039` 升到 `head` 后断言版本是 0039，改为先升到 `head`、再降到 `0039_payment_receipts` | 新索引列为 `(user_id, purpose, created_at)`、非唯一；降级后只剩原有索引；0025 的断言仍证明 0025 没加索引；0039 的用例在 head 为 0040 时照常通过 | I1 |
| 配置下界 | unit | 每个配置项取下界、下界减一；跨字段：`idle = access + 59` 与 `+ 60`、`refresh_ttl = idle - 1` 与 `= idle`、`interval = ttl + 1` 与 `= ttl`；`interval = 59`、`60`、`3600`、`3601`；默认值构造；从环境变量读新配置项 | 下界与边界值通过，越界与关系不成立抛 `ValidationError`；默认值通过 | I2 |
| 限流用例仍有效 | integration（API） | `test_auth_api.py` 两条限流用例改为 `burst=3` 后 | 仍能观察到 429 与 `Retry-After`，429 文案不含邮箱 | I2 |
| frontend_base_url | unit | 空串、全空白；https 根、http 根、带端口、末尾 `/`、首尾空白；拒绝：无方案、ftp、无主机、带用户名或密码、端口非数字 / 0 / 70000、路径 `/app`、`//`、末尾 `?`、`?a=1`、末尾 `#`、`#x`、中间含空格、含 `\n` / `\t`、含反斜杠；`production` + http、`production` + https、`production` + 空串 | 合法值规范成去末尾 `/`；非法值 `ValidationError`；生产只拒 http | I3 |
| 渲染不受规范化影响 | unit | 现有 `frontend_base_url=f"  {BASE}/  "` 的渲染用例；`tests/backend/test_receipt_email.py:232` 的参数化 `["", "   ", "/"]` 里的 `"/"` 在新校验下构造 `Settings` 即抛错（没有主机），去掉这一项（与 `test_notification_email.py:395` 的 `["", "   "]` 一致） | 照常通过（链接不变）；空与全空白仍不渲染 | I3 |
| 正常路径（限频后仍发第一封） | integration | 新账号第一次申请 | 令牌、outbox、审计各一行（现有用例照常） | I4 |
| 边界值 | integration | 第一次申请 `now = T`；第二次 `now = T + interval - 1`、`T + interval` | 前者不写任何行，后者签发第二张 | I4 |
| 限频按账号与用途 | integration | 账号 A 刚申请过时账号 B 申请；账号有窗口内的 `INVITATION` 令牌时申请；窗口内令牌已被用掉（`used_at` 非空）时申请 | B 照常签发；邀请令牌不挡；已用掉的仍挡 | I4 |
| 对外不可区分 | integration（API + unit） | 已知账号连发两次 vs 未知邮箱连发两次；补齐用例加上被限频路径 | 四次响应状态码与 body 完全相同；被限频路径的耗时同样落在下限上；被限频时 `on_queued` 与 broker 触发都没有被调用 | I4 |
| 被限频不留痕 | integration | 被限频的那次 | 没有新令牌、outbox、审计行；INFO 日志只带 `user_id`、不含邮箱 | I4 |
| 并发：同一账号（拿锁后读到已提交） | integration（MySQL） | ① 一个连接 `BEGIN`、锁住用户行并插入一张窗口内令牌（未提交）；另一个线程调 `request_reset`，经 `performance_schema.data_lock_waits`（联 `data_locks`，按 `DATABASE()` 与表名 `users` 过滤）确认它在 `users` 上等待后，提交前者 | 线程被限频、没有写任何行。变异：删掉设置 RC 的那一句（回到 RR、普通读）时这条变红 | I4 |
| 并发：同一账号两次并发只发一封 | integration（MySQL） | ② 两个线程同时 `request_reset` 同一账号（重复 20 轮） | 每轮恰好一张令牌、一行重置邮件 outbox、一条审计；两个线程都正常返回 | I4 |
| 并发：两个不同账号落在同一间隙 | integration（MySQL） | ③ 新建两个 ACTIVE 账号 U1 < U2，`user_id` 是表里最大的两个、都没有任何令牌（两边的窗口查询都扫到新索引末尾的 supremum）。测试引擎上挂 `before_cursor_execute` 钩子：语句以 `INSERT INTO password_reset_tokens` 开头时，在一个三方屏障上等待（两个线程 + 主线程）。两个线程同时对 U1、U2 调 `request_reset`；两者都到屏障时（都已做完窗口查询、都还没插入），主线程查 `performance_schema.data_locks`（`OBJECT_SCHEMA = DATABASE()`、`OBJECT_NAME = 'password_reset_tokens'`），钩子在同一时刻用自己的连接读 `@@transaction_isolation`；然后放行 | 屏障点上 `password_reset_tokens` 没有任何锁行（尤其没有 `GAP` 或 supremum 锁）；两个线程读到的隔离级别都是 `READ-COMMITTED`；放行后两者都在超时内正常返回（没有 1213），各有一张令牌、一行 outbox。变异：把 RC 换回 v1 的 `with_for_update()` 锁读时，屏障点出现 supremum 间隙锁、放行后一方以 1213 失败，这条变红 | I4 |
| 隔离级别不泄漏到连接池 | integration（MySQL） | ④ `pool_size=1`、`max_overflow=0` 的引擎上依次跑签发、被限频、未知邮箱、非 ACTIVE 四条路径，以及一次在 `_start_reset` 里注入异常的路径；每条之后从同一会话工厂取新会话执行 `SELECT @@transaction_isolation` | 每次都是 `REPEATABLE-READ`（同一条物理连接，证明归还时已还原） | I4 |
| SQLite 不设隔离级别 | unit | ⑤ 现有 SQLite 上的全部重置用例 | 照常通过（方言分支不在 SQLite 上设 RC，不抛错） | I4 |
| 既有用例的连发 | integration | 服务层：`test_password_reset.py` 的 `test_resetting_also_invalidates_the_other_outstanding_links`、`test_each_reset_bumps_the_password_version` 等连续签发多张令牌的用例；API 层：`tests/backend/test_session_revocation.py:693`–`:708` 循环三次忘记密码；MySQL 并发：`tests/backend/test_password_reset_concurrency.py` 的 `test_two_live_links_submitted_together_settle_cleanly`（`:133`–`:134`）与 `test_the_loser_reports_an_invalid_token_not_a_weak_password`（`:205`–`:206`）经辅助函数 `issue_token`（`:107`–`:113`）连着签发两张令牌 | 服务层改为给前面的签发传早于窗口的 `now`；API 层传不了 `now`，在下一次忘记密码之前改库把该用户已有 `PASSWORD_RESET` 令牌的 `created_at` 提前到窗口之外；并发文件的 `issue_token` 加可选参数 `now` 并断言 `request_reset` 的返回值不是 `None`（以后再被限频会直接报错，不会悄悄读回上一行 outbox），两条用例的第一次签发传早于窗口的 `now`，保证两张令牌不同（`first != second`）；原断言不变，两条用例仍分别守住「删掉 `reset_password` 的用户行锁就变红」（PR #39）与「后到者得到 `INVALID_RESET_TOKEN` 而不是 `WEAK_PASSWORD`」 | I4 |
| 模板 | unit | `render_email(TEMPLATE_PASSWORD_CHANGED, …)`；参数缺一个、多一个 | 标题与正文与 §2 逐字一致；不合规返回 `None` | I5 |
| 渲染 | unit | payload 合规；键多一个、少一个；`to` 为空、非字符串、含 `\r` / `\n`；`changed_at` 读不出；`frontend_base_url` 为空 | 合规时 `Channel.EMAIL`、收件人为 `to`、`changed_at` 为 MYT 显示值、链接为 `{base}/forgot-password`；不合规返回 `None`，日志不含 payload 值 | I5 |
| 链接对得上前端 | unit | 渲染出的链接路径 vs `paths.ts` 的 `forgotPassword`；该路由不在 `RequireAuth` 之内 | 一致 | I5 |
| 邮件接入 outbox | integration | `handled_event_types()` 的集合断言加上新类型；`recover` 领取并投递一行 | 投递后 `SENT`、payload 清空 | I5 |
| 同事务 | integration | 重置成功 | 恰好多一行 `PASSWORD_CHANGED_EMAIL_REQUESTED`：`aggregate_type = users`、`aggregate_id = str(user.id)`、payload 恰好 `{to, changed_at}`、`to` 为该用户邮箱、`changed_at` 为整秒 UTC、`PENDING`；ADMIN 与 CUSTOMER 各一例 | I6 |
| 失败不写通知 | integration | 令牌无效、已用、过期、密码太弱、账号停用 | 没有通知行 | I6 |
| 事务中途失败 | integration | 在通知行写入后、提交前注入异常 | 密码、令牌、会话、审计、outbox 全部不变 | I6 |
| 通知 payload | integration | payload 内容 | 没有令牌、IP、User-Agent、角色、租户字段 | I6 |
| Redis/Celery 丢失 | integration | 重置成功后不触发任何投递，直接跑 `recover` | 通知行被领取并投递 | I6 |
| 租户越权 | integration | 两个租户各一个门户账号，A 重置 | 只有 A 的邮箱收到通知行 | I6 |
| 金额精度 | — | 不涉及金额 | — | — |

## 8. 迁移与上线

- **迁移 `0040_reset_request_index`**（文件 `alembic/versions/20261116_0040_reset_request_index.py`）：只加一个二级索引 `ix_password_reset_tokens_user_purpose_created`，在线 DDL，不写数据；`downgrade` 只删它。文件头照 0011 写锁表、失败处理与回滚。编号已定：0040 归本批（E 批没有迁移、最新是 0039）；L 批的 0041 以 `0040_reset_request_index` 为 `down_revision`，所以 I1 要先于 L 批的迁移合并，否则 L 批的迁移找不到父版本。
- **隔离级别不是迁移**：`_start_reset` 的 READ COMMITTED 只在会话里设，不改 MySQL 服务器配置与引擎默认值，部署无额外步骤；前提 binlog 为 ROW 格式在生产与 CI 都已成立（§2）。
- **前置（管理员，在登记 PR 里做，不在任何任务契约里）**：
  1. `tests/backend/test_password_reset.py:354` 的 `Settings(password_reset_ttl_seconds=60)` 改为另传 `password_reset_min_interval_seconds=60`。I2 加的跨字段约束（`interval ≤ ttl`）会让这一行在 I2 合并后报错，而 Worker 的敏感路径规则收不了这个文件。`Settings` 是 `extra="ignore"`（`app/core/config.py:127`），I2 之前多传的这个键被忽略，所以提前改不影响现状。
  2. `.env.example` 的两处注释（见下面「管理员步骤」），同样因为 Worker 改不了 `.env*`。
- **部署顺序**（每个 PR 合并即部署生产）：
  1. I1（索引，Worker）。合并后只多一个没人用的索引。
  2. I2（下界 + 新配置项，Worker）。生产取默认值，全部满足；新配置项此时还没有读取方。
  3. I3（`frontend_base_url`，Worker）。生产现值已核实满足全部规则（https、无路径、查询串、片段与末尾斜杠），无部署风险。即使将来配错，部署也会在迁移步骤失败、旧版本继续跑（不中断服务，但部署红）。
  4. I4（限频，人工）。依赖 I1、I2。
  5. I5（通知渲染，人工）。先于 I6，让通知行一出现就有处理器。
  6. I6（写通知行，人工）。依赖 I4、I5（与 I4 改同一文件，串行）。
  7. I7（文档，人工）。
- **管理员步骤（Worker 改不了 `.env*`）**：随登记 PR 给 `.env.example` 的「密码重置与出站邮件」一节加一条注释行 `# BILLING_PASSWORD_RESET_MIN_INTERVAL_SECONDS=300`，写明用途、取值范围 60–3600、不得大于 `BILLING_PASSWORD_RESET_TTL_SECONDS`；并在 `BILLING_FRONTEND_BASE_URL` 的注释里补一句「只写根地址：http 或 https（生产必须 https），不带路径、查询串与片段」。
- **兼容窗口**：I6 上线前的重置不发通知（不补发）。I5 已上线而 I6 未上线时没有通知行，无影响；若部署顺序被打乱、I6 先于 I5，通知行以 `PENDING` 躺着（不在恢复名单里、不计积压），I5 上线后被投递，信里的时刻仍是当时的改密时刻。
- **回滚**：只回滚应用镜像，不降级 `0040_reset_request_index`（旧代码不读这个索引）。回滚到 I4 之前：限频消失，回到现状；回滚到 I6 之前：已写的通知行若处理器也被回滚则以 `PENDING` 等待，重新部署后投递。
- **部分部署**：API 与 worker 用同一镜像同时换；API 新、worker 旧的窗口里新通知行等 worker 更新（同上）。
- **监控与告警负责人**：沿用 `outbox_backlog`（Kelvin）；不新增告警维度。

## 9. 方案取舍

| 方案 | 优点 | 风险 | 未采用原因 |
| --- | --- | --- | --- |
| 采用：用户行锁串行化 + `_start_reset` 事务在 MySQL 上用 READ COMMITTED + 拿锁之后普通读令牌表 + `(user_id, purpose, created_at)` 索引 | 不加列、状态只有一份（令牌表本身）；同一账号并发正确；不加任何间隙锁，不同账号之间没有锁交集；查询只读窗口内的行 | 要一个迁移；仓库里第一次按事务切隔离级别（新模式），正确性依赖「设 RC 是会话第一句」与「拿锁之后才读」两个顺序，§7 用变异用例钉住 | — |
| 备选 K：保留 `FOR UPDATE` 锁读（v1），对 1213 在补齐窗口内重试一次 | 不引入新模式 | 跨账号落在同一间隙（supremum 最常见）时死锁是常态而非偶发，重试只是把一部分 500 变成多一段耗时；重试本身会让「存在且 ACTIVE」的路径耗时更长，压缩补齐余量；第二次仍可能死锁 | 治标；RC 直接消除死锁来源 |
| 备选 L：把整个应用（引擎默认）改成 READ COMMITTED | 没有按事务切换的特殊写法 | 改变所有现有事务的一致性语义，`reset_password`、钱包、账本等的并发论证都是按 RR 写的，要逐一重审 | 范围远超本批 |
| 备选 M：事务开头执行 `SET TRANSACTION ISOLATION LEVEL READ COMMITTED` | 不依赖 SQLAlchemy 的连接选项 | 只对「下一个事务」生效，必须在任何语句之前执行，且依赖驱动没有提前开事务；隔离级别不经 SQLAlchemy 设置，它的「归还时还原」就无从谈起，正确性全靠 MySQL 自己「只管下一个事务」的语义 | `Session.connection(execution_options=…)` 是 SQLAlchemy 的标准写法，归还时由它还原 |
| 备选 A：不加索引，沿用 `(user_id, used_at)` | 不要迁移 | 查询要扫该账号**全部**历史令牌（含邀请令牌）。限频后一个被持续骚扰的账号每天最多新增 288 行，一年约 10.5 万行；那时每次申请要扫十万级的行，耗时会逼近乃至超过 0.12 秒的补齐下限 —— 超过之后「这个邮箱存在」重新可以从耗时读出 | 代价随时间无界增长，而索引只是一次在线 DDL；迁移编号已定，采用索引 |
| 备选 B：`users` 加 `last_reset_requested_at` 列 | 已持有的用户行锁直接读到最新值，不需要隔离级别切换与索引 | 同样要迁移；同一事实存两份（令牌表的 `created_at` 与这一列），两者可能不一致；每次签发多写一次 `users` 行 | 冗余状态 |
| 备选 C：Redis 计数 | 不碰数据库 | Redis 丢失时控制失效（fail-open）；`app/core/ratelimit.py` 文件头已论证安全控制不放 Redis | 与既有原则冲突 |
| 备选 D：唯一约束（例如每个账号每个时间桶一行） | 由数据库保证 | 要新列；桶边界两侧几秒内可各发一封；撞约束要在补齐窗口内处理 `IntegrityError` | 语义不精确、实现更绕 |
| 备选 E：被限频时写审计 | 审计里能看到每一次骚扰 | 匿名调用者能按来源限流的速率往永久的审计表写行 | 见 §6；每个窗口的第一次已有审计 |
| 备选 F：改密通知带来源 IP 与设备 | 用户更容易判断是不是自己 | IP 在代理配置不对时可伪造或全是代理地址（`app/core/config.py:220`–`:229`）；多一类个人数据进 payload | 收益小于风险；时刻已足以判断 |
| 备选 G：改密通知 `delay()` 立即触发 | 少等最多 60 秒 | 要把触发接进重置端点；失败处理照 `_trigger_outbox` 再写一遍 | 60 秒对安全通知可以接受 |
| 备选 H：`frontend_base_url` 只在渲染时判空（现状） | 不改配置 | 配错要到用户点不开才暴露（TODO 原话） | 正是要解决的问题 |
| 备选 I：生产也允许 http | 少一条规则 | 带令牌的链接走明文 | 生产现值已是 https，这条规则没有迁移代价，只防将来配错 |
| 备选 J：所有配置项一律 `gt=0` | 简单 | 1 秒的访问令牌、0 秒宽限的闲置期限、容量 1 的令牌桶都能通过，故障照旧 | TODO 明确要求逐个想清楚 |

## 10. 未决问题与假设

- **未决问题**：无。v1 的三条待拍板项已全部定下（下面「已定」第 3–5 条）。
- **已定**：
  1. 限频口径（Kelvin）：窗口内签发过 `PASSWORD_RESET` 令牌就挡，不论是否已用或过期；后果（重置成功后 5 分钟内再申请不会发信）已接受。
  2. 生产 `frontend_base_url`（已核实）：https、无路径、查询串、片段与末尾斜杠，满足 I3 的全部规则含生产必须 https；I3 没有部署风险。
  3. 迁移编号与索引（管理员已核实并定）：0040 归本批，索引保留；E 批没有迁移；L 批用 0041、以 `0040_reset_request_index` 为 `down_revision`。结论见 §2「数据库」、§8。
  4. 下界取值（由本设计定）：`access ≥ 60`、`idle ≥ access + 60`、`refresh ≥ idle`、`failures ≥ 3`、`lockout ≥ 300`、`burst ≥ 3`、`per_minute ≥ 3`、`interval ∈ [60, 3600]` 且 `≤ password_reset_ttl_seconds`。理由：每一条都从现有代码的使用方式算出（§2「配置下界」表），都只是防误配的下限，默认值（即生产现值）全部满足且都留有余量，不改变任何现有行为；以后要调整是改一个 `Field` 的数字，不涉及数据与契约。
  5. 改密通知的文案与链接（由本设计定）：一个模板同时给 ADMIN 与 CUSTOMER，结尾「contact Acuven」，链接指向忘记密码页，不带 IP 与设备。理由：两类账号的处置相同（重新重置、告知 Acuven），一个模板少一处要同步的地方；文案要素与不带 IP 的理由见 §2「改密通知」与 §9 备选 F；文案改动只改模板字符串与逐字比对的测试，不涉及契约。
  6. 交付方式：I1–I3 由 Worker 交付；I4–I7 涉及的文件路径（`app/services/password_reset.py`、`tests/backend/test_password_reset*.py`、`tests/backend/test_password_changed_email.py`）含 `password` / `token` 等词元，被 Worker 的敏感路径规则拒收，由人工会话交付（§11）。
- **尚未验证的假设**：
  1. 生产没有通过任何途径设过这七个认证配置项（compose 不转发、容器里没有 `.env`、`deploy/deploy.sh` 只往 `.env` 写两个镜像键，见 `pin_deployed_images`，`:68`–`:77`），实际取默认值。
  2. 除 `_start_reset` 外，其他事务仍是 MySQL 默认的 REPEATABLE READ（`app/core/database.py` 不设隔离级别），§4 的死锁论证按此写；SQLAlchemy 2.0 在连接归还连接池时把经 `execution_options` 设的隔离级别还原（§7 场景 ④ 验证）。
  3. MySQL 优化器对限频查询选用新索引。选了旧索引时结果仍正确（RC 下普通读不加锁），只是扫的行多（退化成备选 A 的耗时代价），I1 不对优化器的选择做断言。
  4. `tests/backend/test_perf_usage.py:94` 等用 `environment="production"` 构造 `Settings` 的用例，在 CI 里不会从环境或 `.env` 读到 http 的 `BILLING_FRONTEND_BASE_URL`（CI 不设它、仓库里没有 `.env`）。开发机上若本地 `.env` 设了 http 地址，这些用例会因 I3 的生产规则失败。
- **如果假设错误，具体后果**：1 → 某个越界值让 I2 部署在迁移步骤失败、旧版本继续跑、部署红，由管理员改配置后重跑，不中断服务；2 → 若还原不生效，RC 会泄漏到借到同一连接的下一个事务，§7 场景 ④ 会红，实现改为在 `_start_reset` 结束时显式还原或改用专用连接，不得带着泄漏合并；3 → 只影响性能；4 → 那几条本地用例失败，改为显式传 `frontend_base_url=""`。
- **已知限制**：被限频不可见，用户在 5 分钟内反复点「忘记密码」只会收到第一封信；前端文案不变（明确不做）。死信的改密通知不能重发（payload 已清空），runbook 写处置。

**存在未解决的财务、安全、租户隔离或不可逆迁移问题时，不得标记 `READY_FOR_REVIEW`。**v2 已没有待拍板项：迁移编号与索引已定，其余都由本设计定下并给出理由。

## 11. 审查与版本绑定

审查方要回答的五个问题常驻在 [scripts/review_checklist.md](../../scripts/review_checklist.md) 的「设计审查」一节，此处不重复。

**版本绑定规则**（规则的出处是 [WORKFLOW §3](../../docs/WORKFLOW.md)；这里保留一份是因为审查材料里只有本 Issue 正文，两处改一起改）：

- 批准必须写成 `APPROVED: design v<N>`，`<N>` 取本 Issue 顶部的「设计版本」
- **设计版本一变，之前的 APPROVE 自动作废**，必须重新过闸门
- 什么算「实质修改」：改了契约、表结构、事务边界、状态机、失败语义、不变量控制。改错别字不算
- 实质修改时：把顶部版本 +1，在下方追加一条变更说明，状态退回 `READY_FOR_REVIEW`

### 实现拆分

依赖：I1 → I4；I2 → I3；I2 → I4；I4 → I6；I5 → I6；I1..I6 → I7。全部是后端或文档，没有用户可见页面的改动，不带浏览器验收。

**交付方式**：

- **I1–I3 由 OpenClaw Worker 交付**，登记为 `AIH-TASK-231`（I1）、`AIH-TASK-232`（I2）、`AIH-TASK-233`（I3）。三者的可改路径都不含敏感词元。登记 PR 同时做 §8「前置」的两件事（`tests/backend/test_password_reset.py:354` 与 `.env.example`）。
- **I4–I7 由人工会话交付**，不分配 AIH 编号。原因：它们要改的 `app/services/password_reset.py`、`tests/backend/test_password_reset*.py`、`tests/backend/test_password_changed_email.py` 的路径分量含 `password` / `token` 等词元，Worker 的敏感路径规则（控制面 `worker/paths.py:16`–`:20`）一律拒收。按常规手工 PR：Claude 实现、另一方独立审查（`scripts/codex-review.ps1`）、Kelvin 批准合并；一个 PR 一层，按 I4 → I5 → I6 → I7 的顺序，前一个合并并部署后再开下一个（I5 不依赖 I4，但同在人工队列里，按顺序做以免并行改动交叉）。
- 所有任务与 PR：不改 `.env*`、`.platform/`，不加依赖，不连生产；Draft PR 交付；PR 正文「设计闸门」写本 Issue 与批准的版本，「已知未做 / 留给后续」如实写；复选框只用 `[ ]` / `[x]`；本地检查按 `docs/WORKFLOW.md` §7 全过。

#### Worker 任务

**I1 = AIH-TASK-231（数据库）给重置令牌表加按用户、用途与创建时刻的索引**（预计约 220 行）

- 可改路径：`alembic/versions/20261116_0040_reset_request_index.py`（新）、`app/models/auth.py`、`tests/backend/test_migrations.py`、`tests/backend/test_payment_receipts_migration.py`、`docs/database-schema.md`
- 验收：
  1. 迁移 `revision = "0040_reset_request_index"`（不超过 32 个字符）、`down_revision = "0039_payment_receipts"`，只建 `ix_password_reset_tokens_user_purpose_created (user_id, purpose, created_at)`，`downgrade` 只删它；文件头照 0011 写锁表、失败处理与回滚
  2. 模型声明同名索引，`ix_password_reset_tokens_user` 不动
  3. `test_migrations.py` 新增 0040 的用例：衔接 0039、revision 长度、升级后索引列与非唯一、降级后消失、模型与迁移字面量一致
  4. 0025 用例里「索引只有 0004 那一个、没有含 `purpose` 的索引」的两处断言改为只约束 0025 自己（在 0025 版本上断言或显式排除 0040 的索引），不放宽 0025 的其他检查
  5. `test_payment_receipts_migration.py:1784` 的 `_at_0039` 改为先升到 `head`、再降到 `0039_payment_receipts`（库可能已在 head 0040，Alembic 不允许「升级」到更早的版本），其余断言不变
  6. `docs/database-schema.md` 的 `password_reset_tokens` 一节写明新索引与用途（限频查询，设计 §2）；`purpose` 一行约束列里的「不建索引」改为「不单独建索引；是复合索引 `(user_id, purpose, created_at)` 的第二列（迁移 0040）」
  7. 不改任何服务代码

**I2 = AIH-TASK-232（配置）给认证相关的寿命、计数与限流配置项加下界并新增重置申请最短间隔**（预计约 250 行）

- 可改路径：`app/core/config.py`、`tests/backend/test_config.py`、`tests/backend/test_auth_api.py`
- 前置：登记 PR 已把 `tests/backend/test_password_reset.py:354` 改为另传 `password_reset_min_interval_seconds=60`（§8「前置」第 1 条）；本任务不碰该文件
- 验收：
  1. 七个配置项与新配置项 `password_reset_min_interval_seconds`（默认 300）按设计 §2「配置下界」的表加 `Field` 下界与一个新的 `model_validator`（三条跨字段关系加 `interval ≤ password_reset_ttl_seconds`），注释写明每条理由
  2. `test_config.py`：每项的下界与下界减一、每条跨字段关系的两侧、新配置项的上下界、默认值构造、从 `BILLING_PASSWORD_RESET_MIN_INTERVAL_SECONDS` 读取
  3. `test_config.py:159` 的 `password_reset_ttl_seconds=1` 改为 300；`test_auth_api.py` 两条限流用例改为 `burst=3` 并相应增加请求次数，仍断言 429、`Retry-After` 与文案不含邮箱
  4. 全量后端检查通过（含 `tests/backend/test_password_reset.py`，它依赖登记 PR 的前置改动）
  5. 不改 `TokenBucket`、`docker-compose*.yml`、`_status_webhook_timings_fit` 与 `frontend_base_url`
  6. 新配置项此任务里没有读取方

**I3 = AIH-TASK-233（配置）启动时校验 frontend_base_url 的格式**（预计约 160 行）

- 可改路径：`app/core/config.py`、`tests/backend/test_config.py`、`tests/backend/test_receipt_email.py`
- 验收：
  1. 新增 `require_frontend_base_url` 与 `frontend_base_url` 的字段校验，规则逐条照设计 §2「frontend_base_url 的格式」第 1–5 步；返回去掉末尾斜杠的值
  2. 生产规则（第 6 步）写成 `model_validator`：`environment` 为 `production` 且值非空而不是 https 时拒绝
  3. 测试覆盖设计 §7「frontend_base_url」一行列出的每个合法与非法输入
  4. `test_receipt_email.py:232` 的参数化去掉 `"/"`（新校验下它构造 `Settings` 即抛错），保留 `""` 与 `"   "`
  5. 其余渲染、支付建单、告警的用例不改也全部通过（首尾空白与末尾斜杠的输入仍得到同样的链接）
  6. 不改 `app/tasks/outbox.py`、`app/services/payments.py`、`app/services/alerts.py` 里对它的使用

#### 人工 PR

**I4（规则，人工）忘记密码按账号限频，五分钟内同一账号只发一封重置邮件**（预计约 380 行；依赖 I1、I2 已部署）

- 可改文件：`app/services/password_reset.py`、`tests/backend/test_password_reset.py`、`tests/backend/test_password_reset_api.py`、`tests/backend/test_password_reset_concurrency.py`、`tests/backend/test_session_revocation.py`
- 验收：
  1. `_start_reset` 的会话第一句：仅在 `mysql` 方言上 `session.connection(execution_options={"isolation_level": "READ COMMITTED"})`，在任何 `execute` 之前；不改引擎、`session_scope` 与其他事务
  2. 在锁用户行并复查为 ACTIVE 之后、生成令牌之前，按设计 §2「限频规则」做**不加锁**的窗口查询（只看 `PASSWORD_RESET`，左开窗口，不论已用或过期）；命中时记固定文本的 INFO（只带 `user_id`）、返回 `None`，不写任何行、不调 `on_queued`；不在补齐窗口内对任何数据库错误重试
  3. 不改 `request_reset` 的补齐与 `on_queued` 逻辑、不改端点与响应；被限频路径同样补齐到下限；单元与集成用例覆盖设计 §7 的「边界值」「限频按账号与用途」「对外不可区分」「被限频不留痕」「SQLite 不设隔离级别」，补齐用例加上被限频路径
  4. MySQL 用例覆盖设计 §7 的场景 ①–④（同一账号拿锁后读到已提交、同一账号两次并发只发一封、两个不同账号落在同一间隙、隔离级别不泄漏）；判等待用 `performance_schema.data_lock_waits`、判持锁用 `data_locks`，不用 `INNODB_TRX`；PR 里写明做过两次变异：删掉设 RC 那一句时场景 ① 红、换回 `with_for_update()` 锁读时场景 ③ 红
  5. `test_password_reset.py` 里连续签发多张令牌的用例改为给前面的签发传早于窗口的 `now`；`test_session_revocation.py:693`–`:708` 的三次忘记密码，在每次之前改库把该用户已有 `PASSWORD_RESET` 令牌的 `created_at` 提前到窗口之外；`test_password_reset_concurrency.py` 的 `issue_token` 加可选 `now` 并断言 `request_reset` 返回值不是 `None`，`:133`–`:134` 与 `:205`–`:206` 两条用例的第一次签发传早于窗口的 `now`，断言两张令牌不同；原断言不变
  6. 函数与模块的说明注释补上限频的位置、为什么本事务用 READ COMMITTED 而不用锁读（含 v1 的跨账号死锁）、为什么只在 MySQL 上设、连接归还时还原，以及「不改对外结果」

**I5（规则，人工）新增密码已修改通知邮件的模板与渲染并接入 outbox 投递**（预计约 280 行；不依赖 I4，排在它之后）

- 可改文件：`app/core/email_templates.py`、`app/tasks/outbox.py`、`app/services/password_reset.py`、`tests/backend/test_password_changed_email.py`（新）、`tests/backend/test_outbox.py`、`tests/backend/test_password_reset_link.py`
- 验收：
  1. `app/services/password_reset.py` 只加常量 `EVENT_PASSWORD_CHANGED = "PASSWORD_CHANGED_EMAIL_REQUESTED"` 并导出，不加生产者
  2. 模板 `TEMPLATE_PASSWORD_CHANGED` 的标题、正文、必需参数与设计 §2 逐字一致
  3. `_render_password_changed` 按设计 §2 的每条校验，不合规记固定文本 ERROR、返回 `None`；登记进 `_RENDERERS`
  4. 新测试文件覆盖设计 §7「模板」「渲染」「邮件接入 outbox」；`test_outbox.py` 的 `handled_event_types()` 集合断言加上新类型
  5. `test_password_reset_link.py` 比对渲染出的链接路径与 `paths.ts` 的 `forgotPassword`，并核对它不在 `RequireAuth` 之内
  6. 不改其他渲染函数、模板、`_settle` 与 `recover`

**I6（规则，人工）重置密码成功时同一事务写密码已修改通知**（预计约 200 行；依赖 I4、I5 已部署）

- 可改文件：`app/services/password_reset.py`、`tests/backend/test_password_reset.py`、`tests/backend/test_password_reset_api.py`、`tests/backend/test_session_revocation.py`
- 验收：
  1. `reset_password` 在审计之后、提交之前写一行通知 outbox 行，字段与 payload 照设计 §2「改密通知」；不 `delay()`
  2. 覆盖设计 §7 的「同事务」「失败不写通知」「事务中途失败」「通知 payload」「Redis/Celery 丢失」「租户越权」
  3. 既有用例里按「最后一行 outbox」取重置令牌的（`test_password_reset.py` 的 `issue_token`、`test_password_reset_api.py:84`、`test_session_revocation.py:242`、`:731`），改为按事件类型 `EVENT_PASSWORD_RESET` 取，原断言不变
  4. 不改 `request_reset`、判定顺序、锁顺序与审计内容

**I7（文档，人工）补限频、改密通知与配置校验的运维与部署说明并勾选待办**（预计约 120 行；依赖 I1–I6 已合并）

- 可改文件：`docs/runbook.md`、`docs/deployment.md`、`docs/api.md`、`docs/TODO.md`
- 验收：
  1. `docs/runbook.md`：`outbox_backlog` 一行与死信处置各补上 `PASSWORD_CHANGED_EMAIL_REQUESTED`（死信时 payload 已清空、不能重发；按 `aggregate_id` 即用户 id 查账号，怀疑盗用时由人联系用户）；「密码重置」死信处置补一句「同一账号 5 分钟内重复申请不会再发信，先查垃圾箱」
  2. `docs/deployment.md`：新配置项、七个配置项的下界与跨字段关系、`BILLING_FRONTEND_BASE_URL` 的格式规则与生产必须 https、配置不合规时部署在迁移步骤中止
  3. `docs/api.md`：`outbox_backlog` 一行的已处理事件类型补上新类型
  4. `docs/TODO.md`：勾掉「对同一个邮箱的重置申请没有频率限制」「重置成功后不发通知」「frontend_base_url 不校验格式」「既有配置项缺下界校验」四项；限频口径与 TODO 原文的差异写在 PR 描述里
  5. 不写任何主机名、地址与邮箱

### 设计闸门判定

- [ ] 需求、非目标和验收标准明确
- [ ] 关键契约与事务边界明确
- [ ] 触碰的不变量都有控制措施
- [ ] 失败路径都有确定的最终状态
- [ ] 高风险控制都有测试场景
- [ ] 没有未解决的阻断假设
- [ ] 审批绑定到明确的设计版本

## 12. 版本变更记录

| 版本 | 日期 | 改了什么 | 触发原因 |
| --- | --- | --- | --- |
| v1 | 2026-11-16 | 初稿 | — |
| v2 | 2026-11-17 | §2「限频规则」：`_start_reset` 的事务在 MySQL 上改用 READ COMMITTED（会话第一句经 `Session.connection(execution_options=…)` 设，只在 mysql 方言，连接归还时还原），窗口查询由 `FOR UPDATE` 锁读改为拿用户行锁之后的普通读；§4 重写死锁论证；§5 加「签发时被选为死锁牺牲者」一行（不在补齐窗口内重试）；§7 拆出并发场景 ①–⑤，新增两个不同账号落在同一间隙、隔离级别不泄漏两条 MySQL 用例；§9 加备选 K、L、M，改采用行与备选 A；§2「数据库」、§8、§10：迁移编号 0040 已定归本批、索引保留，§10 其余待拍板项由设计定下；§11 I4 验收同步 | 设计闸门 #751 审查 REQUEST_CHANGES：跨账号窗口查询落在同一间隙（supremum）时两边的间隙锁让 INSERT 互相等待、InnoDB 1213 死锁，§4「不会死锁」论证错误、缺跨账号用例与死锁牺牲者路径；顶部状态与 §10「拍板前保持 DRAFT」矛盾 |
| v3 | 2026-11-17 | §7「既有用例的连发」与 §11 I4 验收第 5 条：补上 `test_password_reset_concurrency.py` 两条连发用例与 `issue_token` 的改法（第一次签发传早于窗口的 `now`、断言返回值不是 `None`、断言两张令牌不同） | 设计闸门 #751 v2 审查 REQUEST_CHANGES：限频上线后这两条 MySQL 并发回归用例会读回同一张令牌、悄悄失去保护 |

# Spec v1.0 评审意见

审阅对象：`Acuven_Central_AI_Billing_Platform_Spec_v1.0.md`（4054 行，140 节）
审阅日期：2026-09-09

**结论**：架构方向（异步 outbox + 中心化计费 + 不可变账本 + 版本化定价）是对的，可以照着做。但定价引擎和财务边界这两块存在会直接导致算错钱的空洞，Phase 2 之前必须补齐。以下按严重性分级。

---

## P0 — 阻断级（不定清楚 Phase 2 写不下去，或写出来是错的）

### 1. 汇率完全缺失 —— 最大的洞

- §5 规定「MYR only，不实现货币转换」，§14 举例 `Actual Provider Cost = RM0.018421`。
- 但 Anthropic / OpenAI 的价目表是 **USD**。从 USD 单价推出 MYR 实际成本，中间必然有一个汇率。
- 全文 4054 行没有出现过 exchange rate / FX / USD。
- 连带后果：
  - §17 要求「历史成本不因供应商现价变动而改变」，但汇率天天变 —— 汇率也必须版本化，`usage_events` 要存 `fx_rate_version_id`。
  - §86 的 Gross Margin 直接建立在实际成本上，汇率错则毛利全错。
  - §80 的 `DECIMAL(20,8)` 精度设计没考虑汇率乘法带来的二次舍入。
- **要定的**：汇率从哪来（人工录 / API）、多久更新、是否版本化、按 `occurred_at` 还是结算日取、`provider_price_versions` 存 USD 还是存换算后的 MYR。

### 2. API Secret 存储方式自相矛盾

- §36：「Never expose secret again after initial creation. Store only secure hash where feasible.」
- §37：要求 HMAC 请求签名。
- **HMAC 验签需要服务端持有明文 secret**，只存 hash 根本验不了签。二者不能同时成立。
- §76 写的 `webhook_secret_hash / encrypted secret` 用斜杠糊过去了，是同一个问题。
- **要定的**：改成「可逆加密存储（KMS / 应用层信封加密）」，并明确 hash 只适用于「一次性比对型」凭据。这条不改，Codex 一定会实现错。

### 3. 用量摄取是同步计费还是先落库后异步计费 —— 未定义

- §82 的伪代码看起来是在 HTTP 请求内同步完成「锁钱包 → 写账本 → 更新余额」。
- 但 §20 要求快速返回，§91 对支付 webhook 明确说「heavy work 异步」，摄取路径反而没说。
- 后果：同租户所有请求会在钱包行锁上串行化，成为全平台吞吐瓶颈；§39 批量 100 条时一个 HTTP 请求要连续锁 100 次钱包。
- **建议**：摄取 = 鉴权 + 校验 + 落 `usage_event(status=RECEIVED)` + `202` 返回，计费在 Celery 里做。这样 §23 的幂等只需保证「入库幂等」，财务幂等由 worker 侧状态机保证。这个决定影响表结构，必须现在定。

### 4. 挂起 / 恢复阈值有边界死锁

- §7.8 / §49：`balance <= 0` → SUSPENDED；§7.9 / §27：`balance > 0` → ACTIVE。
- 客户欠 -RM50，正好充值 RM50 → 余额 = 0 → 既不满足恢复条件，又仍满足挂起条件 → **付了钱还是被停，且没有任何机制能自动救回来**。
- 另外挂起阈值被硬编码为 0，和 §42「最低充值额不许硬编码」的原则不一致；也没有滞后带（hysteresis），余额在 0 附近抖动会反复触发挂起/恢复 webhook + 邮件 + WhatsApp。
- **建议**：定义 `suspend_at` / `resume_at` 两个可配置阈值（例如 suspend ≤ -RM5、resume ≥ RM10），单一状态机，边界归属写死一边。

### 5. `event_id` 唯一性作用域未定义 —— 有跨租户投毒风险

- §23 只说 `UNIQUE(event_id)`，而 event_id 由**客户后端生成**。
- 如果是全局唯一：租户 A 用了租户 B 已存在的 event_id，事件会被当作 duplicate **静默丢弃并返回成功** → 计费逃逸，且 §83「Never silently discard」被绕过。
- **建议**：`UNIQUE(tenant_id, event_id)`，或服务端强制 `evt_<tenant_public_id>_<uuid>` 前缀校验。
- 附带：§82 把重复检查放在 DB transaction **之前**，并发同 event_id 会双双穿过检查。必须明确「捕获唯一约束冲突 → 返回 already_processed」这条路径，光靠预检查是错的。

### 6. 定价引擎的核心表结构一个字都没有

- §74 列了 `provider_price_versions` / `pricing_rules` 两张表名，§75–79 却只给了 tenants / projects / wallets / wallet_transactions / usage_events 的字段。**整个系统最核心的两张表没有字段定义。**
- 具体没回答的问题：
  - MARKUP 和 FIXED_RATE 两种策略怎么在同一张表里表达？分量费率放 JSON 还是拆行？
  - 费率有哪些分量？§9 / §33 / §79 都采集了 `cache_creation_tokens` / `cache_read_tokens`，但 §15 的 FIXED_RATE 只定义了 input / output 两个价格 —— **缓存 token 按什么价算？** Anthropic 缓存写入约 1.25x、读取约 0.1x，忽略它实际成本就是错的，直接体现在毛利上。
  - 非 token 计量（`AUDIO_SECOND` / `OCR_PAGE`）的费率怎么表达？§12 要求引擎从一开始就支持泛化单位，但定价模型只按 token 设计。
- 这是 Phase 2 的第一块砖，必须补。

### 7. 数据库备份 / 恢复策略完全缺失

- §99 单 VPS + Docker Compose，跑的是**钱包余额和不可变财务账本**。
- 全文没有一处提到 backup、binlog、PITR、RPO、RTO、异地备份、恢复演练。§93 只说 PDF 存 docker volume。
- VPS 磁盘挂掉 = 所有客户余额和账本归零，且**无法从客户后端重建**（客户后端 outbox 在 SENT 之后不保留）。
- **要加**：MySQL 每日全备 + binlog、异地存储、明确 RPO/RTO、恢复演练列入 §132 Definition of Done。财务系统这条不能省。

---

## P1 — 严重缺口（会在上线后出事）

### 8. 异步计费 × 月度对账单：晚到事件没有 cut-off 规则

- §85 明确接受「计费中断 → 恢复后补处理」，§22 的重试退避能排到数小时。
- §46 要求「每月 1 号生成上月账单」。
- 一笔 8 月 31 日发生、9 月 2 日才投递成功的事件，属于 8 月还是 9 月？账单已经出了怎么办？
- 全文没有 cut-off / 晚到事件 / 期间归属的任何规则。**这是异步计费的固有冲突，spec 完全没触及。**
- **建议**：账单在月末后 N 天生成（如 T+3），晚于 cut-off 到达的事件一律计入下期，并在下期账单单列 `Prior Period Adjustment`。

### 9. 跨期 Rebill 对已出账单的影响未定义

- §19 允许对历史事件做补偿交易，§65 又说账单不可重算。
- 那么对上月事件的 `REBILL_CREDIT` 出现在哪里？§46 的账单字段里没有「上期调整」这一行。
- 与第 8 条一起解决。

### 10. 支付对账定时任务缺失

- §31 花了整节论证「webhook 不可靠所以要 5 分钟轮询兜底」，这个哲学是对的；§40 的 adapter 接口里也有 `get_payment_status`。
- 但 §110 的 Celery Beat 任务清单里**没有支付对账** —— 有账单生成、价格同步、通知重试、状态 webhook 重试，唯独漏了支付。
- 网关 webhook 丢一次 = 客户真金白银付了钱但钱包不加，且系统永远不会自愈。比状态 webhook 丢失严重得多。
- **要加**：定时扫描超时未决的 `PENDING` payment，主动 `get_payment_status` 对账；同时给 `EXPIRED` 定义超时时长（现在只有状态名，没有产生机制）。

### 11. 状态 Webhook 乱序无保护

- §29 webhook 失败重试 + 指数退避 → **SUSPENDED 和 ACTIVE 两条 webhook 可能乱序到达**。
- 客户后端若按到达顺序覆盖本地状态，会被卡在错误状态，直到 §30 的 5 分钟轮询纠正（最长错 5 分钟）。
- §28 payload 里有 `effective_at`，但没有任何一句要求「客户后端必须丢弃早于本地状态的事件」。
- **要加**：payload 增加单调递增的 `status_version`（或明确要求按 `effective_at` 比较后丢弃旧事件），并写进 §26 的客户后端行为里。

### 12. tenant 状态与 project 状态的关系未定义

- §24 状态挂在 tenant 上；§57 / §76 project 又有自己的 `status`；§28 webhook payload 同时带 tenant_id 和 project_id；§30 `account-status` 返回 project 粒度的 status。
- 一个 project 被 disable 而 tenant 是 ACTIVE 时，`GET /account-status` 返回什么？没写。
- **要定**：明确「有效状态 = tenant.status AND project.status」的合成规则，并写清 account-status 返回的是合成后的值。

### 13. SST（服务税）未决 —— 影响数据结构，越晚改越贵

- §121 只把 LHDN e-Invoice 排除在 V1 之外，但 **e-Invoice 和 SST 是两回事**。
- 数字服务在马来西亚适用 SST。收 RM100 是含税还是不含税？§45 的 Receipt 字段清单里既没有税额行也没有 SST 登记号。
- 预付模式还有税点问题：税在**收款时**确认还是**消费时**确认？这决定 receipt 和 statement 的字段。
- V1 可以不做税务计算，但必须现在向会计确认结论，并在 receipt / statement 表里预留字段。事后加税字段等于重做所有历史凭证。

### 14. 批量接口的事务语义未定义

- §39 说「每个事件独立结果」，返回 `accepted / duplicates / rejected`。
- 但没说：整批一个事务还是逐条事务？部分成功后客户端怎么标记 outbox？批量处理超时算成功还是失败？
- 客户端拿到 `accepted: 98, duplicates: 2` 却不知道**具体哪两条**重复，就无法正确更新本地 outbox。
- **要定**：`results` 必须是 `[{event_id, status}]` 的逐条数组，且明确逐条独立事务。

### 15. 客户定价规则按哪个时间点解析 —— 未统一

- §82 明确「resolve provider cost version **at occurred_at**」（供应商成本按发生时间）。
- 但 §16 的客户定价规则解析链条**没说按什么时间**，§59 还要求支持 future-effective pricing。
- 积压 30 小时的事件，期间管理员改了定价规则 → 按发生时价还是处理时价？两种实现都说得通，Codex 会随机选一个。
- **要定**：统一按 `occurred_at`，并写进 §133 不变量。

### 16. 支付金额异常处理未定义

- §118 的测试要求「wrong amount rejected」，但正文没有任何一节定义什么是 wrong amount、怎么处理。
- 网关回调金额小于下单金额（部分支付）、大于下单金额（多付）分别怎么办？拒绝、按实付入账、还是挂起人工？
- 也没定义：同一客户是否允许并存多笔 PENDING payment、单笔充值上限、最低额校验在后端是否强制（§42 只说了前端不许硬编码）。

### 17. 销户 / 退款 / 余额清算未定义

- §7.3「Credit never expires」+ §8「V1 不做自动退款」+ §56 只有 deactivate 没有 delete。
- 客户不用了、余额还剩 RM300，怎么处理？这既是财务问题（递延收入负债怎么核销）也是合规问题。
- 另外 PDPA 2010 一字未提：§75 存了联系人姓名 / 邮箱 / 电话且 §112 要求永久保留。V1 可以不做数据主体请求，但应在 §121 明确列为 out of scope，而不是留白。

---

## P2 — 建议澄清 / 减少误导

### 18. 「Customer Backend」这个词有严重歧义，可能导致过度设计

- spec 里 Customer = 付费给 Acuven 的企业客户（tenant）。
- 但「Customer Backend」实际指的是 **Acuven 自己开发和运维的后端**（acuven_aichat / ai_chatbot_demo / rs-roof-pms 等），不是客户自建的系统。
- 这个混淆会影响信任模型判断：§98「Customer Backend must NEVER connect directly to this database」、§37 完整的 HMAC 签名体系，都是按「对方是不受信第三方」设计的。如果两端都是自己的机器、同一套 vps_infra，部分机制可以简化（保留也有理由：防误接、便于将来真的开放给第三方），但这个取舍要写出来。
- **建议**：全文把「Customer Backend」改名为「Tenant Runtime / Integrated Backend」，并在 §2 用一段话明确写出信任边界假设和取舍理由。

### 19. §7 规则 6 是一条无法实现的空条款

- 原文：「Customer cannot spend intentionally below RM0 during normal operation」。
- 但架构上根本没有同步授权（§85 明确放弃），唯一的拦截点是 §26「本地状态为 SUSPENDED 时不调用 AI」，那已经是事后的了。
- 这条规则实际什么都没约束，却会让读者误以为存在余额拦截机制。
- **建议改写为**：「唯一的消费拦截点是本地缓存的 SUSPENDED 状态；在状态传播延迟窗口内允许透支。」

### 20. 每请求一行账本 vs 100 万事件目标，没做权衡分析

- §111 明确「聚合不替换 request 数据」，§8 要求每笔余额变动都产生一条 transaction。
- 100 万 usage events = 100 万行 `wallet_transactions` + 100 万次钱包 UPDATE，而单笔金额可能只有 RM0.04。
- §119 只说「设计要能撑 100 万事件」，但没分析钱包行锁的串行化上限和账本膨胀。
- **建议**：至少讨论「按对话或按时间窗聚合成一笔 AI_USAGE ledger、usage_events 保留明细」的方案，即使 V1 决定不做，也要写明为什么。

### 21. HMAC 时间戳与 occurred_at 容易被实现者混淆

- §37 要求 ±5 分钟时间窗；§22 允许 outbox 积压数小时甚至 30 小时。
- 签名的 timestamp 是**发送时刻**、`occurred_at` 是**发生时刻**，两者必须分开 —— 但 spec 没点破。实现者若用 occurred_at 签名，积压事件会全军覆没。
- 另外 §96 提到 replay protection 用 timestamp / event ID，但没定义 nonce 缓存的存储和 TTL（应等于时间窗大小，放 Redis）。

### 22. UsageEvent 契约不完整

- §11 的示例只给了 LLM_TOKEN 场景，缺 `quantity` / `unit`（§79 表里有），也没给 AUDIO_SECOND 的示例 payload —— 而 §12 / §34 明确要求支持。
- 缺 `schema_version` 字段，未来 payload 演进时新旧客户端无法共存。
- 建议 §11 给出 2–3 个不同 usage_type 的完整示例，并把 API 契约作为 §38 的一部分固化。

### 23. 与现有 vps_infra 共享基建的关系未说明

- §98 说「Central Billing 有自己的 MySQL database」，但没说是**独立实例**还是**共享实例上的独立 db**。
- 按 `E:\projects` 现状，其他项目都接 `infra_mysql` 共享实例。财务系统建议独立实例（爆炸半径、备份策略、资源隔离都不同），但这个决定要写进 spec，而不是留给实现者。

### 24. 文档结构问题

- 标题层级坏了：`# 3. Technology Stack` 下面 `## 3.1` 是二级，但 `# 3.2 Frontend`（第 165 行）和 `# 3.3`（第 193 行）被写成了**一级标题**，与 §4、§5 同级。
- 140 个一级标题、层级全平、没有目录，纸面上难以导航。
- 没有需求编号可追溯性（无法在代码 / PR 里引用「实现 REQ-042」），而 §132 的 Definition of Done 又要求逐条验收。建议给每条硬性要求编号。

### 25. 其他小问题

- §101 仓库名 `acuv-ai-billing` 与实际目录 `ai_billing_hub` 不一致，先统一。
- 缺量化的非功能指标：摄取 QPS 目标、「事件发生 → 钱包扣款」的延迟 SLO、可用性目标。§119 只有一个 100 万事件的容量数字。
- §113 的单元测试清单没有覆盖：余额恰好为 0、跨期 rebill、汇率换算、缓存 token 计价 —— 都是上面 P0 / P1 指出的高风险点。

---

## 建议的处理顺序

1. **先答三个业务问题**（不写代码也能答，且决定表结构）：汇率来源与版本化策略、SST 税务口径、挂起 / 恢复阈值。
2. **补两张核心表的字段定义**：`provider_price_versions`、`pricing_rules`（含缓存 token 与非 token 计量的费率表达）。
3. **定死三个架构选择**：摄取同步还是异步、`event_id` 唯一性作用域、账单 cut-off 规则。
4. **修一处矛盾**：§36 secret 存储改为可逆加密。
5. **加一节**：备份与恢复（RPO / RTO + 演练），列入 Definition of Done。

以上做完，spec 可以进入 Phase 0 / Phase 1 实施。

---

## 写得好的部分（不用动）

- §20–§23 异步 outbox + 幂等的整体设计，方向完全正确。
- §14 / §69 实际成本与客户售价的隔离，以及客户不可见字段的明确列举。
- §17 供应商价格版本化 + §19 用补偿交易而非改历史账本。
- §86 明确区分「钱包充值」和「已确认收入」—— 这一点很多计费系统会做错。
- §133 的 10 条不变量，是全文最有价值的一节，建议在每个 PR 模板里引用。

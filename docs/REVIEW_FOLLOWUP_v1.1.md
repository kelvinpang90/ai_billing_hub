# 评审意见落实核对（v1.0 评审 → spec v1.1）

> 逐条比对 [SPEC_REVIEW_v1.0.md](SPEC_REVIEW_v1.0.md) 的 25 条意见与 [spec v1.1](Acuven_Central_AI_Billing_Platform_Spec_v1.1.md) 正文。
> 核对日期：2026-09-10 · 核对方式：按每条意见点名的章节读原文比对，非抽样

**结论**：25 条里 **20 条已解决**，**5 条有残留**（#4 / #16 / #20 / #21 / #24）。所有 P0 阻断级（7 条）**全部解决**，Phase 0 / Phase 1 没有被评审意见卡住的地方。

残留的 5 条都不阻断 Phase 0–1，但 #16（并发 PENDING payment）必须在 Phase 4 之前定，#4（停复机阈值）建议在 Phase 2 之前定。

---

## P0 — 阻断级（7 条，全部解决）

### 1. 汇率完全缺失 —— ✅ 已解决

- §5 新增：供应商源价格可为非 MYR，必须经 §17.1 版本化 FX 流程换算；新增 `REQ-FIN-001`
- §17.1 新增整节：可插拔 FX 适配器 → 自动抓取 → DRAFT → 管理员审批 → PUBLISHED；API 不可用时强制手工录入；**自动抓取绝不许绕过审批直接发布**
- 取值时点定死：**按 `occurred_at`** 取生效的已发布版本
- §14 要求每个事件快照 `provider_source_cost` / `provider_source_currency` / `fx_rate_version_id` / `fx_rate_applied` / `estimated_provider_cost_myr`
- §74.2 给出 `fx_rate_versions` 完整字段；§79 `usage_events` 含上述字段
- 无可用汇率 → `FX_RATE_ERROR`，告警，**不扣钱包**
- §80 明确高精度中间值 + 分量求和后**只舍入一次**，回应了「汇率乘法二次舍入」

### 2. API Secret 存储自相矛盾 —— ✅ 已解决

- §36 直接写死：请求签名密钥与出站 webhook 签名密钥必须用**认证式可逆加密**或有文档的安全密钥派生方案，因为 HMAC 需要密钥材料；**「口令式单向哈希对这些密钥无效」**
- 加密主密钥存数据库之外、走生产密钥管理注入、独立备份、纳入恢复流程
- 日志 / 审计前后值 / API 响应 / 异常栈**都不得暴露明文**
- 轮换支持有界重叠期 + 显式 key version；过期后旧版本停止验签
- §74.4 `integration_credentials.encrypted_secret` + `key_version`；§76 `encrypted_webhook_secret` + `webhook_key_version`——原来那个 `hash / encrypted` 斜杠糊法没了

### 3. 摄取同步还是异步 —— ✅ 已解决

- §20 明确**两条**异步边界：① 终端用户请求不等中心计费；② 中心摄取先持久化再异步定价
- 新增 `REQ-INGEST-001`：`202` 只表示中心已持久化接管，**不表示定价或扣款完成**
- 新增 `REQ-INGEST-002`：**光进队列不算确认**，`RECEIVED` 数据库记录才是事实来源，worker 必须能靠扫库恢复
- §82 拆成两段：API 接受流程（鉴权 → 校验归属 → 规范化+指纹 → 插入 RECEIVED → 202）与 worker 流程（定价 → 锁钱包 → 写账本）
- 附带解决了「批量 100 条要连锁 100 次钱包」——批量只落库

### 4. 挂起/恢复阈值边界死锁 —— ⚠️ 部分解决

**已解决的部分**：
- §7.10 把边界归属写死一边：**余额恰好 RM0 保持 `SUSPENDED`**；充值 API 与 UI 必须计算并显示「最小恢复金额」——既清掉负数又留正余额。欠 RM50 充 RM50 卡死的场景没了
- §42 后端强制该最小恢复额，结果余额必须**严格大于 RM0**
- §7.11 + §48：通知**只在真实状态跃迁时**发；低余额告警要从阈值上方跌破才触发，回到上方才重新武装。余额在阈值附近抖动刷通知的问题解决了
- §113 单元测试清单已列「exactly zero remains suspended」「minimum recovery top-up」

**残留**：
- 停机阈值**仍硬编码为 0**（§49 `balance <= 0`），未引入评审建议的可配置 `suspend_at` / `resume_at` 双阈值与滞后带
- 这与 §42「最低充值额不许硬编码」的原则仍不一致
- 后果已被最小恢复额机制大幅削弱，属**可接受的取舍**，但应显式记为已知决定，而不是留白

### 5. `event_id` 唯一性作用域 / 跨租户投毒 —— ✅ 已解决（换了方案）

评审建议 `UNIQUE(tenant_id, event_id)`，spec **保留全局 `UNIQUE(event_id)`**，但用更严的方式堵住了两个洞：

- §23 + §79：首次接收时一并存下 credential / tenant / project 归属与**规范化 payload 指纹**。只有归属与指纹全部匹配才算重试；其余一律 `IDEMPOTENCY_CONFLICT` + `HTTP 409` + **零财务效果** + 安全告警
- 「静默丢弃并返回成功」的计费逃逸路径没了
- §23 + §81 明确：**数据库唯一性是最终并发权威，光靠读前检查不够**；必须 insert-first / upsert 或捕获唯一约束冲突后再比对。原来「预检查在事务之前」的竞态修掉了
- §82 的接受流程也按这个顺序重写了

### 6. 定价引擎核心表一个字都没有 —— ✅ 已解决

- §15.1 新增：价格必须表示为**版本化的价格分量**，分量含 `meter_type` / `component_code` / `unit` / `unit_quantity` / `rate_amount` / `rate_currency`
- 分量码清单含 `LLM_CACHE_WRITE_TOKEN` / `LLM_CACHE_READ_TOKEN` —— **缓存 token 定价的洞补上了**，且明确「缓存折扣倍数绝不许做成引擎里的全局常量」
- 非 token 计量（`AUDIO_SECOND` / `OCR_PAGE` / `TTS_CHARACTER` …）用同一套分量表达
- **任一必需分量解析不到 → `PRICING_ERROR`，引擎绝不许当零成本**
- §74.1 `provider_price_versions` + `provider_price_components` 完整字段
- §74.3 `pricing_rules` + `pricing_rule_components` 完整字段；MARKUP 与 FIXED_RATE 的字段互斥规则写明（MARKUP 必须有 multiplier 且无分量，FIXED_RATE 反之），数据库与服务层都要拒绝混合/残缺
- 同范围已发布区间不许重叠；`priority_scope` 要能无歧义编码 §16 的解析顺序

### 7. 备份/恢复策略完全缺失 —— ✅ 已解决

- §98.1 新增整节，给出量化目标：**RPO ≤ 5 分钟，RTO ≤ 4 小时**
- 最低控制项：MySQL binlog PITR + 定期全备、**加密的异地备份**与留存期、文档文件/部署配置/加密主密钥单独权限备份、备份新鲜度自动监控、**季度恢复演练**（要验钱包、账本、用量事件、支付、对账单、文档完整性）、灾难恢复顺序与恢复后对账
- 明确 Redis/Celery 不是财务与投递状态的权威存储，队列丢失后所有持久化作业必须能靠扫 MySQL + domain outbox 重建
- §132 DoD 第 13 条 + 上线闸门「RPO/RTO 恢复演练通过」；§95 监控含备份新鲜度与失败；§120 告警含备份失败

---

## P1 — 严重缺口（10 条，8 条解决 / 2 条有残留）

### 8. 晚到事件没有 cut-off —— ✅ 已解决

§46 定死：用量期按 Asia/Kuala_Lumpur 的月末 23:59:59.999999 截止，**T+1 宽限期 = 新月第一个完整日**，第 2 日生成并定稿。用量期按 `occurred_at` 判定，账本入账期按 `processed_at` 判定。cut-off 之后才处理的上期事件计入当前开放期间，单列 `PRIOR_PERIOD_ADJUSTMENT` 并回指原用量期。

### 9. 跨期 Rebill 对已出账单的影响 —— ✅ 已解决

§19 明确：对已定稿对账单的修正**绝不改动该对账单**，补偿交易发在当前开放期间，记为 `PRIOR_PERIOD_ADJUSTMENT`，引用原事件 / 原钱包交易 / 原对账单期间，在下期对账单单独展示。§46 的对账单字段清单已加「prior-period usage and rebill adjustments」一行。§74.8 有 `cutoff_at` 与 `snapshot_json`。§114 集成测试含「Cross-period rebill adjustment」。

### 10. 支付对账定时任务缺失 —— ✅ 已解决

§43：Celery Beat 必须周期扫描滞留 `PENDING` 支付并调 `get_payment_status`，带受控退避；且要求为选定网关定义 pending 超时、终态映射、最长对账时长、网关不可用行为、`EXPIRED` 跃迁。§110 任务清单已加「stale payment reconciliation」。§74.7 `payments` 含 `expires_at` / `last_reconciled_at`。§95 / §120 含 stale payment 监控与告警。

### 11. 状态 Webhook 乱序无保护 —— ✅ 已解决

§24：每次有效状态跃迁递增**租户范围内单调的 `status_version`**，webhook 与对账响应都必须带。§28 payload 已含 `status_version`，且明确应用侧「只原子应用比本地更大的版本，同版本幂等，旧版本确认后忽略」。§30 对账用同一比较规则。§26 应用侧行为已写进去。

### 12. tenant 状态与 project 状态的关系 —— ✅ 已解决

§24 拆成三个独立维度（tenant 账户生命周期 / tenant 计费状态 / project 集成状态），给出合成规则：三者同时满足才 `ALLOW_AI`，否则 `BLOCK_AI` + 机器可读 `reason_code`。并明确**充值只能改 `billing_status`，绝不能复活被管理员停用或已关闭的租户/项目**。§28 / §30 的 payload 同时返回三个原始状态与合成后的 `effective_status`。

### 13. SST 未决 —— ✅ 已解决（按评审建议：设闸门 + 预留字段）

§45.1 新增决策闸门：Phase 4 实施前必须取得并记录会计/税务意见，覆盖是否需注册 SST、服务分类与豁免、充值属于储值/押金/服务预付、税点确认时机与税率版本、钱包额度含税还是不含税、收据与对账单强制字段。**未批准前生产的 top-up / receipt / statement schema 阻塞**，且明确「本规格不得自行发明税率」。§74.9 `tax_policy_versions` 表已建，§74.7 `payments` 含 `tax_policy_version_id` / `tax_amount`，§45 收据含不可变的开票方/客户/税务政策快照字段。§132 上线闸门含此条。

> ⚠️ 闸门建好了，**结论本身还没做** —— 这是待办（TODO 的 D2），不是 spec 缺陷。

### 14. 批量接口事务语义 —— ✅ 已解决

§39：`results` 已是逐条数组，每条含 `event_id` / `status` / `processing_status` / `error_code` / `retryable`。明确**每条独立校验与持久化，一条无效不得回滚已接受的**；客户端只把明确 accepted 或匹配 duplicate 的本地 outbox 行标记 `SENT`。HTTP 超时或响应丢失时用同一批不可变 `event_id` 与 payload 整批安全重试；`IDEMPOTENCY_CONFLICT` 与校验错误不可重试。

### 15. 客户定价规则解析时点 —— ✅ 已解决

§16：定价规则选择用 `occurred_at`，**不是摄取或处理时间**；生效区间为半开 `[from, to)`，同范围同优先级已发布区间不许重叠。且明确 `pricing_rule_id` 永久保留，**重试同一事件不得选中新发布的规则**；蓄意 reprocess 可选修正规则但要在 rebill 审计里同时留旧引用。§82 流程按此写，Invariant 6 覆盖。

### 16. 支付金额异常处理 —— ⚠️ 部分解决

**已解决的部分**：§43 给出可信确认的**五个必要条件**（网关签名有效 / 内部支付单已知且租户匹配 / 网关状态确认已付 / **金额与内部订单额完全一致** / 币种为 MYR / 该网关支付标识未曾入账过任何钱包）。金额或币种不符 → 不入账、留存原始网关事件、标 `REVIEW_REQUIRED`、管理员告警、要求经审计的人工处理。部分支付与多付都落在这条里。§42 后端强制最低额与可选上限（不再只是前端）。§74.7 `(gateway, gateway_event_id)` 唯一 + 一个网关支付标识至多产生一笔 TOPUP。

**残留**：
- **同一租户是否允许并存多笔 `PENDING` payment，仍无任何规定**。§74.7 有 `expires_at`，§43 要求定义 pending 超时，但没说并发约束
- 影响：客户连点两次充值 → 两笔 PENDING → 都付了怎么办、UI 显示哪一笔。Phase 4 之前必须定

### 17. 销户/退款/余额清算 + PDPA —— ✅ 已解决

§112.1 新增：五步有状态关户流程（DISABLED 阻断新调用与新充值 → 吊销凭据并等待在途事件与支付到终态 → 负余额清偿或正余额退款，走经审计的人工支付 + 不可变账本调整 → 结清收据/对账单并保留财务快照 → 钱包为零且无未决财务事件才置 `CLOSED`）。明确**人工外部退款绝不许直接改钱包余额**，关户后发现的晚到事件进人工复核、不得静默重开账户。§112 要求上线前批准数据治理政策，按财务记录/用量元数据/个人数据/运营日志分类留存，且**明示财务留存期不等于可以无限期留存租户联系人数据**。PDPA 明确**不在 out of scope**，V1 只可延后自助隐私请求 UI，运营流程上线前仍必须有。

---

## P2 — 建议澄清（8 条，6 条解决 / 2 条有残留）

### 18. 「Customer Backend」术语歧义 —— ✅ 已解决

§2.1 新增整节，全文改称 **Integrated Application Backend**，并明确定义为「Acuven 自管的应用后端，不是付费客户运营的软件」。信任边界的取舍也写出来了：即便两端都由 Acuven 运营，仍视为**独立的信任域与故障域**，只走认证 REST + 签名 Webhook，永不共享数据库；按项目发凭据、HMAC 签名、最小权限隔离保持强制。

### 19. §7 规则 6 是空条款 —— ✅ 已解决

§7.6 已改写为评审建议的措辞：「V1 不做同步的按请求额度预留。新 AI 调用**只**被 §24–§30 描述的本地缓存有效服务状态拦截。」§7.7 补充：处理与状态传播延迟**可能**导致负余额，这是可接受的最终一致行为，**不是授权保证**。

### 20. 每请求一行账本 vs 100 万事件 —— ⚠️ 部分解决

**已解决的部分**：§82 明确承认「即使处理是异步的，**钱包变更仍按租户钱包串行化**」，并要求容量测试必须测最热租户速率与积压回补，「Celery 不能消除这个财务串行化要求」。§119 给了量化目标（稳态 ≥20 events/s、5 分钟突发 ≥100 events/s、事件到扣款 p95 ≤60s / p99 ≤5min、积压回补 ≥5× 峰值），并要求报告 p50/p95/p99 与最热租户负载。

**残留**：**没有对「按对话或时间窗聚合成一笔 AI_USAGE ledger、usage_events 保留明细」这个方案做权衡分析**。§111 仍只说「聚合不替代明细」。评审要求「即使 V1 决定不做，也要写明为什么」——这句理由还没写。

### 21. HMAC 时间戳与 occurred_at 混淆 —— ⚠️ 部分解决

**已解决的部分**：§37 直接点破：「签名时间戳是**本次传输尝试时刻**，每次重试重新生成，**不是** Usage Event 的 `occurred_at`。」积压事件签名全军覆没的坑堵上了。同时新增规范化签名串定义（method + 规范路径查询 + timestamp + request-id + body 的 SHA256）、UTF-8、小写十六进制、常量时间比较、生产主机 NTP 同步与时钟漂移告警。

**残留**：**重放保护的 nonce 存储介质与 TTL 仍未定义**。§37 只说「不要把 Redis 硬编码成唯一的重放存储，持久化选择必须扛得住要求的故障模型」，§96 只说「用 timestamp / event ID 做重放保护」。评审要求的「TTL 应等于时间窗大小」没写。

### 22. UsageEvent 契约不完整 —— ✅ 已解决

§11 已加 `schema_version`；给了**两个**完整示例（`LLM_TOKEN` 与 `AUDIO_SECOND`，后者含 `quantity` / `unit`）；明确契约要按 `usage_type` 定义必填/可选/互斥字段、数值范围、标识符最大长度、时间戳格式、可接受的未来时间偏移；**Decimal 数量序列化为 JSON 字符串**以避免二进制浮点歧义；租户与项目身份从凭据推导，payload 里的值只作诊断，不一致即拒绝并审计。

### 23. 与 vps_infra 共享基建的关系 —— ✅ 已解决

§98 已写死：在单台生产 VPS 上，中心计费使用**专用的** MySQL 与 Redis 容器、凭据、数据库、持久卷、资源限制与备份任务，**不与现有应用共享数据库或 Redis 实例**。共享宿主网络或外部反向代理只允许通过有文档的最小权限接口。

> 这条直接给出了 TODO 里 D3 的答案：**独立实例，不接 `infra_mysql` / `infra_redis`**。D3 从「待拍板」降级为「落 ADR 记录」。

### 24. 文档结构问题 —— ⚠️ 大部分解决，有小残留

**已解决**：
- §3.2 / §3.3 的标题层级已修（现在是 `##`，与 §3.1 同级）
- 新增「Document Navigation」导航段，按主题给出章节区间
- 新增 13 个 `REQ-*` ID + §139.1 关键需求追溯表（规则 / 章节 / 必需测试证据三列），并明确「实现、迁移、API、测试必须引用适用 ID」

**残留**：
- `REQ-*` 只覆盖 13 条关键要求，**不是评审说的「每条硬性要求」**。§132 的 DoD 要求逐条验收，细粒度编号仍缺
- 仍是 140 个一级标题、无完整 TOC（Document Navigation 是粗粒度区间，不是目录）
- §139.1 自身写成了 `# 139.1`（一级标题带小数点），与 §17.1 / §45.1 等用 `##` 的写法不一致 —— 纯瑕疵

### 25. 其他小问题 —— ✅ 已解决

- 仓库名：`acuv-ai-billing` 全文已消失，§101 的树用的是 `ai_billing_hub`
- 量化非功能指标：§119 已补摄取 QPS、事件到扣款延迟 SLO、积压回补吞吐、Webhook 入队延迟、对账安全界，以及 99.5% 月可用性目标（并声明不削弱 `REQ-AVAIL-001`）
- 测试清单：§113 已补「exactly zero remains suspended」「minimum recovery top-up」「FX version selection and conversion」「cache write/read component calculations」「pricing rule selection by occurred_at」；§114 补「Cross-period rebill adjustment」

---

## 残留项汇总（已进 [TODO.md](TODO.md)）

| # | 残留 | 来源 | 何时必须定 |
| --- | --- | --- | --- |
| R1 | 停机阈值仍硬编码 0，无可配置 `suspend_at`/`resume_at` 与滞后带 | P0-4 | Phase 2 前（决定采纳或明确记为取舍） |
| R2 | 同租户并存多笔 `PENDING` payment 的规则未定义 | P1-16 | **Phase 4 前必须定** |
| R3 | 账本行数 / 钱包串行化的聚合权衡分析未写 | P2-20 | Phase 2 前（即使不做也要写明理由） |
| R4 | 重放保护的 nonce 存储介质与 TTL 未定义 | P2-21 | Phase 2 前（影响 §37 实现） |
| R5 | `REQ-*` 仅 13 条，未覆盖每条硬性要求；无完整 TOC | P2-24 | Phase 1 前（影响 §132 可验收性） |

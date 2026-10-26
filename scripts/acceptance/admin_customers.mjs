/**
 * 部署后浏览器验收：管理端客户页（AIH-TASK-015），本任务为 AIH-TASK-024；AIH-TASK-023 加了审计页的两个只读步骤
 * （open_audit、check_audit_entry）；AIH-TASK-035 加了 AI 目录的两个只读步骤（open_catalog、check_meter_types）；
 * AIH-TASK-036 加了供应商价格与汇率的两个只读步骤（open_provider_prices、open_fx_rates）；AIH-TASK-037 加了定价规则
 * 与试算的两个只读步骤（open_pricing_rules、open_pricing_preview）；AIH-TASK-038 加了用量事件的一个只读步骤
 * （open_usage_events）；AIH-TASK-046 加了出站 webhook 签名密钥的一个只读步骤（check_webhook_signing）；AIH-TASK-053
 * 加了项目集成状态的一个只读步骤（check_project_integration）；AIH-TASK-070 加了 AI 密钥的一个只读步骤
 * （check_ai_access）；AIH-TASK-074 加了用量分析的两个只读步骤（open_usage_analytics、check_usage_analytics）；
 * AIH-TASK-082 加了客户账户状态的一个只读步骤（check_account_status）；AIH-TASK-085 加了钱包流水的一个只读步骤
 * （check_wallet_transactions）；AIH-TASK-092 加了客户门户接受邀请页的一个只读步骤（check_accept_invitation_page），
 * 它排在 login **之前**跑；AIH-TASK-095 加了客户门户账号面板的一个只读步骤（check_portal_account）；AIH-TASK-097 加了
 * 客户门户的两个步骤（login_customer 用夹具客户的门户账号登录，check_portal_home 只读核对门户首页）；AIH-TASK-100
 * 加了门户钱包页的一个只读步骤（check_portal_wallet）；AIH-TASK-101 加了门户首页余额卡与最近流水的一个只读步骤
 * （check_portal_dashboard）；AIH-TASK-105 加了门户用量页的一个只读步骤（check_portal_usage）；AIH-TASK-108 加了
 * 门户首页本月用量卡的一个只读步骤（check_portal_usage_summary）；AIH-TASK-111 加了客户详情低余额阈值面板的一个只读
 * 步骤（check_low_balance_threshold）。
 * 文件名沿用，不改。open_catalog / check_meter_types
 * 原定随 AIH-TASK-036 跑，因视口判断缺陷（#252 已修）挪到 AIH-TASK-037 跑；这两个步骤本身没有改。
 *
 * 谁在什么时候跑它
 * ----------------
 * 由 Worker 在**部署成功之后**运行（.platform/tasks.yaml 里本任务的 `acceptance` 块，命令 acceptance.browser）。
 * 它**不走 MXC**，并且**拿凭据**：验收夹具账号的邮箱、密码、TOTP 密钥只在这一步由 Worker 从标准输入交进来
 * （控制面安全边界 A7）。实现这个脚本的会话本身不拿凭据。
 *
 * 输入（标准输入的第一行，一个 JSON 对象）
 * ----------------------------------------
 *   steps        要执行的步骤名，按顺序（只执行这里列出的）
 *   writes       布尔；为 true 也只做只读步骤
 *   attempt      第几次尝试（正整数）
 *   cdp          浏览器级 DevTools WebSocket 地址
 *   hosts        项目声明的主机，顺序同 .platform/project.yaml 的 acceptance_hosts；入口取 hosts[0]
 *   credentials  { email, password, totp_secret, customer_email, customer_password }
 *                email、password、totp_secret 是夹具管理员的（login 用）；customer_email、customer_password 是夹具客户
 *                门户账号的（login_customer 用，按 docs/runbook.md「验收夹具客户门户账号」建好并录入）
 * 缺字段或类型不对：`ACCEPTANCE_VERDICT: FAIL <第一个步骤> login_failed`，不打印收到的内容。customer_email、
 * customer_password 不在这条规则里：它们只在执行 login_customer 时检查，缺失或不是非空字符串时
 * `ACCEPTANCE_VERDICT: FAIL login_customer login_failed`（同样不打印收到的内容）；不跑 login_customer 时有没有它们
 * 都不影响其余步骤。
 *
 * 输出（标准输出只有这两类行）
 * ----------------------------
 *   ACCEPTANCE_STEP: <step> ok                  每完成一个声明步骤一行，按声明顺序
 *   ACCEPTANCE_VERDICT: PASS                    最后一行
 *   ACCEPTANCE_VERDICT: FAIL <step> <code>      最后一行；code ∈ login_failed、navigation_failed、page_error、
 *                                               element_missing、assertion_failed、host_unreachable（主文档完全
 *                                               没有得到 HTTP 响应）、service_unavailable（主文档得到 502/503/504）
 * 退出码：PASS 为 0，FAIL 为 1。异常信息、URL（含查询串）、响应体、页面内容、凭据、TOTP 码一律不打印、不保存；
 * 标准错误也不写。
 *
 * 约束（评审逐条核对用）
 * ----------------------
 * - 只用 Node 内置模块：全局 WebSocket、node:crypto、node:process、node:buffer、node:timers/promises。不引入依赖。
 * - 入口：从 hosts[0] 的 https 首页开始。起始时浏览器里应恰好有一个 type 为 page、URL 为 about:blank 的目标；
 *   在 cdp（浏览器级地址）上用 Target.getTargets 找到它、Target.attachToTarget（flatten）接管。找不到恰好一个就
 *   `FAIL <第一个步骤> navigation_failed`。不新建目标或浏览器上下文。本文件不写任何主机名 —— 主机只写在
 *   .platform/project.yaml 的 acceptance_hosts。除首页外，只有 check_accept_invitation_page 会用 Page.navigate 打开
 *   同一主机上的 /accept-invitation（带全零占位令牌），并在断言之后用 openEntry 重新打开首页。
 * - 只经 cdp 操作那一个已有的页面目标。本脚本不自己联网（没有 fetch / http / https / net / dns），页面里执行的
 *   脚本只读 DOM、聚焦、点击、滚动，不发请求。不用 Target.createBrowserContext / Target.createTarget、不改代理、
 *   不拦截或伪造请求（不用 Fetch.*、Network.setRequestInterception、Network.emulateNetworkConditions）、不读写
 *   Cookie 与存储（不用 Network.getCookies、Storage.*、DOMStorage.* 等）、不截图（不用 Page.captureScreenshot）、
 *   不写任何文件。用到的 CDP 方法只有：Target.getTargets、Target.attachToTarget、Network.enable、
 *   Network.getResponseBody、Page.navigate、Runtime.evaluate、Input.insertText。
 * - 凭据只经 Input.insertText 填进登录表单，不进任何页面脚本的源码。TOTP 按 RFC 6238 现算（node:crypto 的
 *   HMAC-SHA1、30 秒步长、6 位），base32 解码在本文件里自己写。Input.insertText 另外只用于往审计页「Action」
 *   下拉的搜索框里输入动作值 `LOGIN`（常量，不是凭据）。
 * - 夹具管理员的邮箱（credentials.email）只填进登录表单：不填进审计页的任何筛选框、不进页面脚本源码；
 *   与页面文字或接口响应的比较都在本进程内做，不打印。夹具客户门户账号的邮箱与密码（credentials.customer_email、
 *   customer_password）同样只经 Input.insertText 填进登录表单，不进页面脚本源码、不打印。
 * - 门户首页（check_portal_home）只看不点：不点「Set up two-factor authentication」「Turn on two-factor
 *   authentication」或任何按钮与链接，不填「Current password」也不提交任何表单（开启 2FA 会写数据）。
 * - 门户钱包页（check_portal_wallet）只点门户导航里的「Wallet」（href 为 /portal/wallet）这一个链接：不点「Older」
 *   「Newest」（不翻页）、不点任何别的按钮与链接、不提交任何表单。
 * - 门户首页的余额卡与最近流水（check_portal_dashboard）只看不点：停留在门户首页，不点「View all transactions」或任何
 *   别的链接与按钮（含开启 2FA），不填也不提交任何表单。
 * - 门户首页的本月用量卡（check_portal_usage_summary）只看不点：停留在门户首页，不点卡上的「View usage details」、
 *   「Try again」或任何别的链接与按钮（含开启 2FA），不填也不提交任何表单。
 * - 门户用量页（check_portal_usage）只点门户导航里的「Usage」（href 为 /portal/usage）这一个链接：用页面默认的日期区间，
 *   不动日期、项目、供应商、模型、会话筛选，不点「Apply filters」「Reset」（不提交任何表单）、不点「Older」「Newest」
 *   （不翻页）、不点表格的任何一行或展开按钮（不展开详情）、不点任何别的按钮与链接。
 * - 只读：只点「Continue」「Verify」（登录）、顶栏「Customers」、列表的「Next Page」（翻页）、夹具客户的公司名
 *   链接、顶栏「Audit log」、审计页「Action」下拉里 title 为 `LOGIN` 的那一项和「Apply filters」按钮（只让页面按
 *   新的筛选条件重新发 GET）、顶栏「AI catalog」（href 为 /catalog/providers）、供应商页里的「Meter types」链接
 *   （href 为 /catalog/meter-types，不在顶栏里）、顶栏「Provider prices」（href 为 /pricing/provider-prices）、
 *   顶栏「Exchange rates」（href 为 /fx-rates）、顶栏「Pricing rules」（href 为 /pricing/rules）、顶栏
 *   「Pricing preview」（href 为 /pricing/preview）、顶栏「Usage events」（href 为 /usage-events）、顶栏
 *   「Usage analytics」（href 为 /usage-analytics）、夹具客户详情里
 *   项目列表第一行的展开按钮（aria-label 为「Expand row」，只展开、不收起）。夹具客户详情资料卡上的「Disable account」
 *   「Enable account」按钮（check_account_status）**只查不点**：只确认对应的那一个在，不点它、不打开启用 / 停用弹窗、
 *   不填原因。接受邀请页（check_accept_invitation_page）不填密码、不点「Activate my account」、不提交表单，
 *   不发任何写请求。夹具客户详情里的钱包流水面板（check_wallet_transactions）只看不点：不动「Source」下拉、不点「Older」
 *   「Newest」、不点 AI 用量行的「View usage event」链接。夹具客户详情里的门户账号面板（check_portal_account）只看
 *   不点：不点「Create portal account」「Resend invitation」「Disable portal access」「Enable portal access」「Reset 2FA」
 *   或任何确认按钮，不填也不提交开通表单里的「Login email」。夹具客户详情里的低余额阈值面板
 *   （check_low_balance_threshold）只看不点：不点「Change」「Clear」「Save threshold」「Clear threshold」或任何按钮，
 *   不打开修改表单或清除确认框，不填也不提交「New threshold」。不点新建客户、编辑 / 保存、手工调账、
 *   新建项目、建凭据、轮换、吊销、签发签名密钥（「Issue new secret」）、启用（「Activate v…」）、退役（「Retire v…」）、
 *   停用 / 启用项目集成（「Disable integration」「Enable integration」）、保存 / 清除状态 webhook 地址（「Save address」
 *   「Clear address」）等任何会写数据的按钮；只打开公司名以 `[TEST] Acceptance Fixture` 开头的夹具客户的详情。项目列表
 *   不翻页、不展开第一行以外的行；签名密钥面板、集成凭据面板与集成状态面板里不点任何按钮、不翻页，集成状态面板里不填
 *   地址输入框。审计页不改每页条数、不翻页、
 *   不展开行、不点「Clear filters」。目录页不点「New provider」「New meter type」「Rename」「Retire」「Reactivate」
 *   或任何确认按钮、不点状态筛选、不翻页、不打开任何供应商的详情、不填也不提交任何表单。价格页与汇率页不点
 *   「New draft」「New manual draft」「Edit」「Publish」「Retire」「Discard」或任何确认按钮、不动供应商 / 模型 /
 *   状态筛选、不翻页、不打开任何价格版本的详情、不填也不提交任何表单。规则页不点「New draft」「Edit」「Publish」
 *   「Disable」「Discard」或任何确认按钮、不动范围 / 客户 / 供应商 / 模型 / 状态筛选、不翻页、不打开任何规则的详情、
 *   不填也不提交任何表单。试算页不选也不填任何字段、不点「Calculate」（试算本身只读，但「不提交任何表单」一视同仁）。
 *   用量事件页不勾选任何行、不点「Requeue selected」「Requeue all matching…」「Requeue」或任何确认按钮、不填也不
 *   提交筛选表单（不点「Apply filters」「Clear filters」）、不翻页、不打开任何事件的详情（「View」链接）。
 *   用量分析页不改分组维度、时间段或任何筛选、不点「Apply filters」「Reset to last 30 days」、不翻页、不改每页条数。
 *   登录会在服务端留下会话与
 *   登录审计，这是只读验收不可避免的，不算写数据 —— check_audit_entry 核对的正是这一条。若账号走到「首次启用
 *   2FA」那一步，那会写数据，直接 login_failed，不继续。
 * - 每个步骤都真的检查它声称的东西；等待一律有上限（见下面的常量），总时长控制在 timeout_seconds（300 秒）内。
 * - 找元素只按可见文字（文案取自 frontend/src/i18n/locales/en.json）或语义结构（label→control、header、
 *   table/thead/th、th→td、li、role、title 与 aria-label 属性、表格行的 data-row-key），不依赖 antd 生成的类名哈希。
 *   文案与选择器集中在下面的常量里。
 *
 * 步骤
 * ----
 *   check_accept_invitation_page
 *                   必须排在 login 与 login_customer **之前**（本次运行跑过其中任一个就 assertion_failed）：浏览器刚启动、没有任何会话，
 *                   正是客户点邀请链接时的匿名场景；本脚本不新建浏览器上下文，所以匿名只能靠步骤顺序保证。打开
 *                   /accept-invitation?token=<全零占位令牌>（frontend/src/routes/paths.ts 的 acceptInvitation 与
 *                   ACCEPT_INVITATION_TOKEN_PARAM），断言页面渲染出接受邀请表单：「Choose a password」「Confirm your
 *                   password」两个 label 都关联着 type 为 password 的输入框、「Activate my account」按钮在；地址没有被
 *                   重定向（仍是 /accept-invitation，不是 /login），表单出现之后再看一小段时间（ACCEPT_INVITATION_SETTLE_MS）
 *                   仍然如此。不填、不点、不提交，不发写请求。断言完成后调用 openEntry 重新打开首页，等到被 RequireAuth
 *                   送回 /login 且登录表单在，把页面交还到与第一步开始前相同的状态（未登录、停在 /login）—— 随后的
 *                   login 步骤不自己导航，只认当前页面是登录页
 *   login          登录表单填邮箱、密码 → Continue → 验证码（TOTP）→ Verify；以离开 /login、出现管理端顶栏
 *                   （header 里有「Sign out」与「Customers」）为准
 *   open_customers  点顶栏「Customers」，在客户列表里按公司名找到夹具客户那一行；分页时逐页点「Next Page」，
 *                   不改每页条数或任何其他设置
 *   open_fixture    点夹具客户的公司名链接进详情，详情里「Company name」一项与列表里的公司名相同
 *   check_balance   后端值取详情页自己发出的 GET /api/v1/admin/customers/{customer_id} 的响应体（Network.enable
 *                   被动观察、Network.getResponseBody 读取）里的 wallet.balance / wallet.currency；页面上
 *                   「Balance」一项的文字必须与 formatMoney(balance, currency) 逐字相同。报到前把这一项滚动到视口
 *                   中间，让 Worker 在这一步的截图里看得到余额
 *   open_audit      点顶栏「Audit log」（href 为 /audit）进审计页；页面不是加载失败、无权限、筛选被拒或空列表，
 *                   审计表格（有「Action」「Actor」两列）至少有一行数据
 *   check_audit_entry
 *                   在「Action」下拉里选 LOGIN（app/models/auth.py 的 AuditAction.LOGIN）并点「Apply filters」；
 *                   后端值取页面在点击之后自己发出的、查询串是 page=1 且 action=LOGIN（没有别的筛选条件）的
 *                   GET /api/v1/admin/audit-logs 的响应体（被动观察，不自己发请求、不拦截），响应的 page 为 1。
 *                   其中第一条 actor_email 等于夹具管理员邮箱（去首尾空白、不分大小写）的记录就是本次 login 留下的：
 *                   action 为 LOGIN、actor_role 为 ADMIN、entity_type 为 users，created_at（不带时区的 UTC）与本次
 *                   login 步骤完成的时刻相差不超过 120 秒；本次运行没跑过 login 就 assertion_failed。表格的数据行
 *                   与响应的 items 一一对应（「Action」列全是 LOGIN），那条记录所在行的「Action」「Actor」两列文字与
 *                   它的 action / actor_email 一致；报到前把这一行滚动到视口中间
 *   open_catalog    点顶栏「AI catalog」（文案取 en.json 的 nav.catalog，href 为 paths.ts 的 catalogProviders，即
 *                   /catalog/providers）进供应商页；页面落定且不是加载失败（任何错误提示）、无权限或路由兜底页。
 *                   空列表合法（生产上不预置供应商），有表格时表头要有「Code」「Display name」两列
 *   check_meter_types
 *                   在供应商页里点「Meter types」链接（href 为 /catalog/meter-types，不在顶栏里）进计量类型页，页面
 *                   落定且不是加载失败、无权限、空列表。后端值取页面在点击之后自己发出的
 *                   GET /api/v1/admin/usage-meter-types 的响应体（被动观察，不自己发请求、不拦截）：items 里有迁移
 *                   种子的 9 个代码（LLM_TOKEN、EMBEDDING_TOKEN、AUDIO_SECOND、AUDIO_MINUTE、TTS_CHARACTER、
 *                   IMAGE_GENERATION、OCR_PAGE、DOCUMENT_PAGE、CUSTOM），其中 LLM_TOKEN 恰好 4 个分量。表格（表头有
 *                   「Code」「Components ← quantity field」）的数据行与响应的 items 一一对应：同样的顺序与代码、每行
 *                   「Components」格里的 <li> 个数等于该项的分量数（LLM_TOKEN 那一行因此是 4 个）；报到前把 LLM_TOKEN
 *                   那一行滚动到视口中间（窄视口下这一行可能比视口还高，那时改为行首对齐视口顶端，只要求行首在视口内）
 *   open_provider_prices
 *                   点顶栏「Provider prices」（文案取 en.json 的 nav.providerPrices，href 为 paths.ts 的 providerPrices，
 *                   即 /pricing/provider-prices）进价格页；页面落定且不是加载失败（任何错误提示）、无权限（pricing.forbidden）
 *                   或路由兜底页。空列表合法（pricing.prices.empty），有表格时表头要有「Provider」「Model」两列
 *   open_fx_rates   点顶栏「Exchange rates」（nav.fxRates，href 为 paths.ts 的 fxRates，即 /fx-rates）进汇率页；页面落定
 *                   且不是加载失败、无权限或路由兜底页。版本列表为空（fx.empty）或有表格（表头有「Currency」「Rate (MYR)」）
 *                   都合法；「BNM fetch attempts」一节（fx.attempts.title）可见，且它的表格（表头有「Attempted at」「Outcome」）
 *                   或空状态（fx.attempts.empty）已经画出来，空列表合法；报到前把这一节的标题滚动到视口中间
 *   open_pricing_rules
 *                   点顶栏「Pricing rules」（nav.pricingRules，href 为 paths.ts 的 pricingRules，即 /pricing/rules）进规则页；
 *                   页面落定且不是加载失败（任何错误提示，含客户 / 供应商下拉取不到）、无权限（pricing.rules.forbidden）或
 *                   路由兜底页。空列表合法（pricing.rules.empty），有表格时表头要有「Scope」「Strategy」两列
 *   open_pricing_preview
 *                   点顶栏「Pricing preview」（nav.pricingPreview，href 为 paths.ts 的 pricingPreview，即 /pricing/preview）进
 *                   试算页；表单可见：「Customer」「Provider」「Model」「Usage type」「Occurred at (Kuala Lumpur time)」五个
 *                   label 都关联着控件、「Calculate」按钮在，且页面写着「Estimated cost, not visible to customers.」
 *                   （pricing.preview.estimateNote）。不是加载失败、无权限或路由兜底页；表单出现之后再看一小段时间
 *                   （PREVIEW_LOOKUP_SETTLE_MS），下拉要的客户、供应商、计量类型取不到（错误提示）或无权限同样算失败。不提交
 *   open_usage_events
 *                   点顶栏「Usage events」（nav.usageEvents，href 为 paths.ts 的 usageEvents，即 /usage-events）进用量事件页；
 *                   页面落定且不是加载失败（任何错误提示）、无权限（usage.forbidden）或路由兜底页。空列表合法（usage.empty：
 *                   生产上可能还没有用量事件），有表格时表头要有「Occurred at」「Billable amount」两列
 *   check_webhook_signing
 *                   在夹具客户详情（open_fixture 打开的那一页）的项目列表（表头有「Project name」「Description」）里点
 *                   第一行的展开按钮（aria-label 为「Expand row」，antd enUS 语言包；只点展开，不点任何写按钮）。项目列表
 *                   加载失败是 page_error，没有项目是 assertion_failed。展开区里的签名密钥面板（role=region、aria-label
 *                   为「Webhook signing secrets」，en.json 的 webhookSigning.title）落定且不是加载失败
 *                   （webhookSigning.loadFailed）或无权限（customers.forbidden）。后端值取页面在点击之后自己发出的
 *                   GET /api/v1/admin/customers/{customer_id}/projects/{project_id}/webhook-secrets 的响应体（被动观察，
 *                   不自己发请求、不拦截；customer_id 是夹具客户、project_id 是第一行的 data-row-key）。面板表格（表头有
 *                   「Key version」「Status」「Activated」）的数据行与响应的 items 一一对应：同样的顺序，每行「Key version」
 *                   列是该项的 key_version、「Status」列是该项 status 的文案（PENDING / ACTIVE / RETIRED →
 *                   Pending / Active / Retired，webhookSigning.status.*）；items 为空时面板显示空状态
 *                   （webhookSigning.empty 或 webhookSigning.emptyPage）、没有数据行。报到前把面板滚动到视口里（放得下时
 *                   整块在视口内，放不下时面板顶端在视口内）
 *   check_ai_access
 *                   在夹具客户详情的项目列表里点第一行展开按钮（已展开则不再点；只点展开，不点签发或吊销）。AI 密钥
 *                   面板（role=region、aria-label 为「AI client keys」）落定且不是加载失败或无权限。后端值取页面自己发出的
 *                   GET /api/v1/admin/customers/{customer_id}/projects/{project_id}/ai-access 响应体（被动观察，不自己发请求）；
 *                   面板的密钥行数、client_id 与状态文案和响应数组一致，空数组显示空状态；报到前把面板滚进视口。
 *   check_project_integration
 *                   在夹具客户详情（open_fixture 打开的那一页）的项目列表里点第一行的展开按钮（同 check_webhook_signing：
 *                   aria-label 为「Expand row」；已经展开着就不再点；只点展开，不点任何写按钮）。项目列表加载失败是
 *                   page_error，没有项目是 assertion_failed。展开区里的集成状态面板（role=region、aria-label 为
 *                   「Integration status and status webhook」，en.json 的 projectIntegration.title）落定（地址读完，不再显示
 *                   projectIntegration.webhook.loading）且不是加载失败（projectIntegration.webhook.loadFailed）或无权限
 *                   （customers.forbidden）。后端值取页面自己发出的 GET /api/v1/admin/customers/{customer_id}/projects（查询串
 *                   page=1）的响应体（被动观察，不自己发请求、不拦截；取这个客户最近一次成功读完的那一次）：items 的第一项的 id
 *                   等于第一行的 data-row-key，它的 integration_status（ENABLED / DISABLED）对应的文案（Enabled / Disabled，
 *                   projectIntegration.status.*）与面板「Integration status」一项（projectIntegration.field.status，面板里
 *                   带边框的 Descriptions：label 在 <th>，值在紧跟的 <td>）的文字相同。报到前把面板滚动到视口里（规则同
 *                   check_webhook_signing）
 *   open_usage_analytics
 *                   点顶栏「Usage analytics」（nav.usageAnalytics，href 为 paths.ts 的 usageAnalytics，即 /usage-analytics）
 *                   进用量分析页；页面落定且不是加载失败（任何错误提示）、无权限（usageAnalytics.forbidden）或路由兜底页。
 *                   空列表合法（usageAnalytics.empty：所选时间段里可能还没有已处理的用量），有表格时表头要有
 *                   「Prepaid (MYR)」「Internal metering only (MYR)」两组列（usageAnalytics.prepaid.title /
 *                   usageAnalytics.internal.title）
 *   check_usage_analytics
 *                   用量分析页（open_usage_analytics 打开的那一页）落定且不是加载失败、无权限或路由兜底页。后端值取页面
 *                   自己发出的 GET /api/v1/admin/usage-analytics 的响应体（被动观察，不自己发请求、不拦截；本次运行跑过
 *                   open_usage_analytics 时只认点顶栏之后发出的，取成功读完的最近一次）：表格（表头有上面两组列）里带
 *                   data-row-key 的数据行数与响应 data.items 的条数一致；items 为空时页面显示空状态（usageAnalytics.empty
 *                   或 usageAnalytics.emptyPage）、没有数据行。不改筛选、不翻页。有数据行时报到前把表格滚动到视口里
 *                   （放得下时整张在视口内，放不下时表格顶端在视口内）
 *   check_account_status
 *                   在夹具客户详情（open_fixture 打开的那一页）上，后端值取详情页自己发出的
 *                   GET /api/v1/admin/customers/{customer_id} 的响应体（被动观察，不自己发请求、不拦截；取这个客户最近一次
 *                   成功读完的那一次）里的 account_status（PENDING_ACTIVATION / ENABLED / DISABLED / CLOSED，认不出的值
 *                   是 assertion_failed）。资料卡「Account status」一项（customers.field.accountStatus，带边框的
 *                   Descriptions：label 在 <th>，值在紧跟的 <td>）的文字与它的文案（Pending activation / Enabled /
 *                   Disabled / Closed，customers.accountStatus.*）相同；页面上恰好有与之对应的那一个操作按钮：ENABLED
 *                   对应「Disable account」（customers.accountStatus.disable）、DISABLED 对应「Enable account」
 *                   （customers.accountStatus.enable），另一个不在；PENDING_ACTIVATION 与 CLOSED 两个都不在。这个按钮
 *                   只查不点、不打开弹窗。报到前把「Account status」这一行滚动到视口中间
 *   check_wallet_transactions
 *                   在夹具客户详情（open_fixture 打开的那一页）上，钱包流水面板（role=region、aria-label 为「Wallet
 *                   transactions」，en.json 的 wallet.transactions.title）落定且不是加载失败（wallet.transactions.loadFailed）
 *                   或无权限（customers.forbidden）。后端值取面板自己发出的
 *                   GET /api/v1/admin/customers/{customer_id}/wallet/transactions 的响应体（被动观察，不自己发请求、不拦截；
 *                   只认不带 before_sequence 与 reference_type 的最新一页，取这个客户最近一次成功读完的那一次）。面板表格
 *                   （表头有「Sequence」「Balance after」）里带 data-row-key 的数据行数与响应 data.items 的条数一致；items 非空
 *                   时第一行「Sequence」列的文字等于 items 第一项的 wallet_sequence；items 为空时面板显示空状态
 *                   （wallet.transactions.empty）、没有数据行，同样算通过。不改筛选、不翻页、不点任何链接。报到前把面板滚动到
 *                   视口里（放得下时整块在视口内，放不下时面板顶端在视口内）
 *   check_portal_account
 *                   在夹具客户详情（open_fixture 打开的那一页）上，门户账号面板（role=region、aria-label 为「Customer portal
 *                   account」，en.json 的 portalAccount.title）落定（不再显示 portalAccount.loading）且不是加载失败
 *                   （portalAccount.loadFailed）或无权限（customers.forbidden）。后端值取面板自己发出的
 *                   GET /api/v1/admin/customers/{customer_id}/portal-account 的响应（被动观察，不自己发请求、不拦截；取这个
 *                   客户最近一次读完的那一次，只认 200 与 404）。响应为 404、错误码 PORTAL_ACCOUNT_NOT_FOUND 时，面板显示未开通
 *                   （portalAccount.none）、没有「Portal status」一项；为 200 时，面板「Portal status」一项（portalAccount.field.status，
 *                   面板里带边框的 Descriptions：label 在 <th>，值在紧跟的 <td>）的文字是响应 status（INVITED / ACTIVE / DISABLED，
 *                   认不出的值是 assertion_failed）的文案（Invited / Active / Disabled，portalAccount.status.*），且不显示未开通。
 *                   其余响应（别的状态码、别的错误码、不是信封）是 assertion_failed。不点开通、不点任何按钮、不填表单。报到前把
 *                   面板滚动到视口里（规则同 check_wallet_transactions）
 *   check_low_balance_threshold
 *                   要排在 open_fixture 之后（本次运行没打开夹具客户详情就 assertion_failed）。在夹具客户详情（open_fixture
 *                   打开的那一页）上，低余额阈值面板（role=region、aria-label 为「Low balance threshold」，en.json 的
 *                   lowBalance.title）落定（不再显示 lowBalance.loading）且不是加载失败（lowBalance.loadFailed，page_error）
 *                   或无权限（customers.forbidden）。后端值取面板自己发出的
 *                   GET /api/v1/admin/customers/{customer_id}/low-balance-threshold 的响应体（被动观察，不自己发请求、不拦截；
 *                   取这个客户最近一次成功读完的那一次，不是成功信封是 assertion_failed）里的 low_balance_threshold：恰好 8 位
 *                   小数的字符串或 null，别的形状是 assertion_failed。面板「Threshold」一项（lowBalance.field.threshold，面板里
 *                   带边框的 Descriptions：label 在 <th>，值在紧跟的 <td>）恰好一项：null 时文字是「Not set」（lowBalance.notSet）；
 *                   否则文字去掉空白与千分位逗号后，等于响应值按 2 位 ROUND_HALF_UP 舍入后的值（roundHalfUp2：字符串运算、不经
 *                   浮点）。不点修改或清除、不填也不提交任何表单。报到前把面板滚动到视口里（规则同 check_wallet_transactions）
 *   login_customer  用 credentials.customer_email / customer_password 登录。与 login 一样不自己导航，只认当前页面是
 *                   登录页（浏览器刚启动时首页被 RequireAuth 送到 /login；本次运行已经以管理员或客户登录过就没有登录表单，
 *                   报 element_missing），所以它要排在 login 之前、或单独跑。两个字段缺失或类型不对：login_failed，不打印
 *                   收到的内容。登录表单填邮箱、密码 → Continue；落定后必须在门户首页：地址是 /portal（paths.ts 的
 *                   portalHome），门户外壳（header 里有「Sign out」与「Customer portal」，en.json 的 nav.signOut、
 *                   portal.title）在，管理端顶栏（header 里有「Customers」）不在。出现管理端顶栏或停在别的已登录页面是
 *                   assertion_failed；还在 /login 且有错误提示、或走到验证码（门户账号已开启 2FA，本脚本没有它的 TOTP 密钥）
 *                   或「首次启用 2FA」那一步是 login_failed，不继续
 *   check_portal_home
 *                   本次运行跑过 login_customer 才能跑（否则 assertion_failed）。门户首页落定（不再显示 portal.home.loading）
 *                   且不是加载失败（portal.home.loadFailed）。后端值取页面自己发出的 GET /api/v1/customer/me 的响应体（被动
 *                   观察，不自己发请求、不拦截；取最近一次读完的那一次，不是 200 就是 assertion_failed）里的 company_name、
 *                   account_status（PENDING_ACTIVATION / ENABLED / DISABLED / CLOSED，认不出的值是 assertion_failed）与
 *                   read_only。页面「Company name」「Account status」两项（portal.home.field.*，带边框的 Descriptions：label
 *                   在 <th>，值在紧跟的 <td>）各恰好一项，文字分别等于 company_name（空白折叠后）与它的文案（Pending
 *                   activation / Enabled / Disabled / Closed，portal.home.accountStatus.*）；只读提示（portal.home.readOnly.title）
 *                   在且仅在 read_only 为 true 时出现。页面上没有任何管理端菜单项：没有 role 为 menu / menuitem 的元素，没有
 *                   文字是管理端顶栏任一项（nav.dashboard、nav.customers 等）的链接，也没有指向管理端路径（paths.ts 的
 *                   dashboard、customers、audit 等）的链接。不点开启 2FA、不填也不提交任何表单。报到前把「Company name」这一行
 *                   滚动到视口中间
 *   check_portal_wallet
 *                   本次运行跑过 login_customer 才能跑（否则 assertion_failed）。点门户导航里的「Wallet」链接（en.json 的
 *                   portal.nav.wallet，href 为 paths.ts 的 portalWallet，即 /portal/wallet）进钱包页；页面落定：地址是
 *                   /portal/wallet，门户外壳在、管理端顶栏不在，余额卡（role=region、aria-label 为「Your wallet」，
 *                   portal.wallet.summary.title）不再显示 portal.wallet.summary.loading、流水一块（role=region、aria-label 为
 *                   「Transactions」，portal.wallet.transactions.title）不再显示 portal.wallet.transactions.loading，且两块都不是
 *                   加载失败（portal.wallet.summary.loadFailed / portal.wallet.transactions.loadFailed，page_error）。后端值取
 *                   页面在点击之后自己发出的 GET /api/v1/customer/wallet 与不带 before_sequence 的
 *                   GET /api/v1/customer/wallet/transactions 的响应体（被动观察，不自己发请求、不拦截；各取最近一次读完的那一次，
 *                   不是 200 就是 assertion_failed）。余额卡「Balance」一项（portal.wallet.field.balance，带边框的 Descriptions：
 *                   label 在 <th>，值在紧跟的 <td>）恰好一项，文字以响应 currency 加一个空格开头，去掉这个前缀、空白与千分位逗号后
 *                   等于响应 balance 按 2 位 ROUND_HALF_UP 舍入后的值（本文件的 roundHalfUp2：字符串运算、不经浮点，舍入结果为
 *                   -0.00 时按 0.00 比较）。流水表格（表头有「Balance after」「Event ID」）里带 data-row-key 的数据行数与响应
 *                   data.items 的条数一致；items 非空时第一行「Event ID」列的文字等于 items 第一项的 event_id（为 null 时该列为空）；
 *                   items 为空时显示空状态（portal.wallet.transactions.empty）、没有数据行，同样算通过。不翻页、不提交任何表单。
 *                   报到前把余额卡滚动到视口里（放得下时整块在视口内，放不下时顶端在视口内）
 *   check_portal_dashboard
 *                   本次运行跑过 login_customer 才能跑（否则 assertion_failed），且要排在 check_portal_wallet 之前：它不自己
 *                   导航、不点任何链接，只认当前页面是门户首页（地址是 /portal，否则 navigation_failed）。页面落定：门户外壳
 *                   在、管理端顶栏不在，余额卡（role=region、aria-label 为「Your wallet」，portal.wallet.summary.title）不再显示
 *                   portal.wallet.summary.loading、最近流水一块（role=region、aria-label 为「Recent transactions」，
 *                   portal.home.recentTransactions.title）不再显示 portal.wallet.transactions.loading，且两块都不是加载失败
 *                   （portal.wallet.summary.loadFailed / portal.wallet.transactions.loadFailed，page_error）。后端值取页面在
 *                   login_customer 点「Continue」之后自己发出的 GET /api/v1/customer/wallet 与查询串 page_size=5、不带
 *                   before_sequence 的 GET /api/v1/customer/wallet/transactions 的响应体（被动观察，不自己发请求、不拦截；各取
 *                   最近一次读完的那一次，不是 200 就是 assertion_failed）。余额卡「Balance」一项恰好一项，文字以响应 currency
 *                   加一个空格开头，去掉这个前缀、空白与千分位逗号后等于响应 balance 按 2 位 ROUND_HALF_UP 舍入后的值
 *                   （roundHalfUp2，同 check_portal_wallet）。响应 data.items 不超过 5 条（否则 assertion_failed）；最近流水
 *                   表格（表头有「Balance after」「Event ID」）里带 data-row-key 的数据行数与 items 条数一致；items 为空时显示
 *                   空状态（portal.wallet.transactions.empty）、没有数据行，同样算通过。不点链接、不翻页、不提交任何表单。
 *                   报到前把余额卡滚动到视口里（规则同 check_portal_wallet）
 *   check_portal_usage
 *                   本次运行跑过 login_customer 才能跑（否则 assertion_failed）。点门户导航里的「Usage」链接（en.json 的
 *                   portal.nav.usage，href 为 paths.ts 的 portalUsage，即 /portal/usage）进用量页，用页面默认的日期区间（马来西亚
 *                   时间本月 1 日到今天），不改任何筛选。页面落定：地址是 /portal/usage，门户外壳在、管理端顶栏不在，用量一块
 *                   （role=region、aria-label 为「Usage」，portal.usage.title）不再显示 portal.usage.loading。显示
 *                   portal.usage.queryTooBroad（默认区间的查询超过了后端 2 秒的执行预算，设计闸门 #398 §2「查询」）是
 *                   assertion_failed，显示 portal.usage.loadFailed 是 page_error。后端值取页面在点击之后自己发出的、不带 cursor 与
 *                   event_id 的 GET /api/v1/customer/usage-events 的响应体（被动观察，不自己发请求、不拦截；取最近一次读完的那一次）：
 *                   响应是 422、错误码 QUERY_TOO_BROAD 时同样是 assertion_failed（原因同上：默认区间都查不完，用量页对这个客户
 *                   不可用），其余不是 200 的响应也是 assertion_failed。用量表格（表头有「Time (MYT)」「Charged (MYR)」）里带
 *                   data-row-key 的数据行数与响应 data.items 的条数一致；items 非空时第一行「Charged (MYR)」列的文字去掉空白与
 *                   千分位逗号后，与 items 第一项 charged_amount 按 6 位 ROUND_HALF_UP 舍入后的值（本文件的 roundHalfUp：字符串
 *                   运算、不经浮点）数值相同 —— 两边都按页面的写法规范成「去掉末尾多余的 0、至少留 2 位小数」再逐字比较；items 为空
 *                   时显示空状态（portal.usage.empty）、没有数据行，同样算通过。不翻页、不展开、不提交任何表单。报到前把用量一块
 *                   滚动到视口里（放得下时整块在视口内，放不下时顶端在视口内）
 *   check_portal_usage_summary
 *                   本次运行跑过 login_customer 才能跑（否则 assertion_failed），且要排在 check_portal_wallet 与 check_portal_usage
 *                   之前：它不自己导航、不点任何链接，只认当前页面是门户首页（地址是 /portal，否则 navigation_failed）。页面落定：
 *                   门户外壳在、管理端顶栏不在，本月用量卡（role=region、aria-label 为「This month's usage」，
 *                   portal.home.usageSummary.title）不再显示 portal.home.usageSummary.loading。显示
 *                   portal.home.usageSummary.queryTooBroad（整月汇总超过了后端 2 秒的执行预算，设计闸门 #403 §2「查询」）是
 *                   assertion_failed，显示 portal.home.usageSummary.loadFailed 是 page_error。后端值取页面在 login_customer 点
 *                   「Continue」之后自己发出的、不带 month 的 GET /api/v1/customer/usage-summary 的响应体（被动观察，不自己发请求、
 *                   不拦截；取最近一次读完的那一次）：响应是 422、错误码 QUERY_TOO_BROAD 时同样是 assertion_failed（原因同上：本月
 *                   用量对这个客户算不出，首页这一块不可用），其余不是 200 的响应、字段缺失或类型不对也是 assertion_failed。卡里
 *                   「Charged (MYR)」「Requests」「Conversations」三项（portal.home.usageSummary.field.*，带边框的 Descriptions：
 *                   label 在 <th>，值在紧跟的 <td>）各恰好一项：「Requests」「Conversations」去掉空白与千分位逗号后分别等于响应的
 *                   request_count、conversation_count；「Charged (MYR)」去掉空白与千分位逗号后，与响应 charged_amount 按 6 位
 *                   ROUND_HALF_UP 舍入后的值（本文件的 roundHalfUp：字符串运算、不经浮点）数值相同 —— 两边都按页面的写法规范成
 *                   「去掉末尾多余的 0、至少留 2 位小数」再逐字比较。月份标题（portal.home.usageSummary.month）是响应的 month 加
 *                   「 (Malaysia time)」。按日柱状图（role=list、aria-label 为「Requests per day (Malaysia time)」，
 *                   portal.home.usageSummary.chart.caption）里 role=listitem 的柱子个数等于响应 days 的项数。不点链接、不提交
 *                   任何表单。报到前把本月用量卡滚动到视口里（规则同 check_portal_wallet）
 * 不认识的步骤名：`FAIL <该步骤> assertion_failed`。
 */

import { Buffer } from "node:buffer";
import { createHmac } from "node:crypto";
import process from "node:process";
import { setTimeout as sleep } from "node:timers/promises";

// ---------------------------------------------------------------------------
// 页面文案与选择器（改前端文案时回来同步这里）
// ---------------------------------------------------------------------------

/** 页面上的可见文字。除特别注明的以外，全部取自 frontend/src/i18n/locales/en.json（括号里是键名）。 */
const TEXT = {
  loginEmail: "Email", // login.email —— 登录表单的 label
  loginPassword: "Password", // login.password
  loginSubmit: "Continue", // login.submit
  loginCode: "Verification code", // login.code
  loginVerify: "Verify", // login.verify
  enrolIntro: "Two-factor authentication is required. Scan this with your authenticator app.", // enrol.intro
  enrolCode: "Enter the code your app shows", // enrol.code
  navCustomers: "Customers", // nav.customers
  navSignOut: "Sign out", // nav.signOut
  appErrorTitle: "This page could not be loaded", // appError.title（RouteErrorBoundary）
  notFoundTitle: "Page not found", // notFound.title
  companyName: "Company name", // customers.field.companyName —— 列表的列头、详情的一项
  listLoading: "Loading customers…", // customers.list.loading
  listLoadFailed: "The customer list could not be loaded.", // customers.list.loadFailed
  listEmpty: "No customers yet.", // customers.list.empty
  detailLoading: "Loading the customer…", // customers.detail.loading
  detailLoadFailed: "The customer could not be loaded.", // customers.detail.loadFailed
  detailNotFound: "Customer not found", // customers.detail.notFound
  walletBalance: "Balance", // customers.wallet.balance
  accountStatusLabel: "Account status", // customers.field.accountStatus —— 资料卡 Descriptions 的一项
  // 账户状态 → 「Account status」一项的文案（customers.accountStatus.pendingActivation / enabled / disabled / closed）。
  accountStatusText: {
    PENDING_ACTIVATION: "Pending activation",
    ENABLED: "Enabled",
    DISABLED: "Disabled",
    CLOSED: "Closed",
  },
  accountDisable: "Disable account", // customers.accountStatus.disable —— 只查它在，不点
  accountEnable: "Enable account", // customers.accountStatus.enable —— 只查它在，不点
  navAudit: "Audit log", // nav.audit
  auditLoading: "Loading audit records…", // audit.loading
  auditLoadFailed: "The audit log could not be loaded.", // audit.loadFailed
  auditForbidden: "Only administrators can view the audit log.", // audit.forbidden
  auditInvalidFilters: "The filters were not accepted.", // audit.invalidFilters
  auditEmpty: "No audit records yet.", // audit.empty
  auditEmptyFiltered: "No audit records match these filters.", // audit.emptyFiltered
  auditEmptyPage: "There are no audit records on this page.", // audit.emptyPage
  auditColumnAction: "Action", // audit.column.action —— 审计表格的列头
  auditColumnActor: "Actor", // audit.column.actor
  auditFilterAction: "Action", // audit.filter.action —— 筛选表单里动作下拉的 label
  auditApply: "Apply filters", // audit.filter.apply
  navCatalog: "AI catalog", // nav.catalog —— 顶栏的目录项
  catalogForbidden: "Only administrators can manage the AI catalog.", // catalog.forbidden
  catalogCode: "Code", // catalog.field.code —— 目录表格的列头
  catalogDisplayName: "Display name", // catalog.field.displayName
  providersLoading: "Loading providers…", // catalog.providers.loading
  providersEmpty: "No providers yet.", // catalog.providers.empty
  providersMeterTypesLink: "Meter types", // catalog.providers.meterTypesLink —— 供应商页里的链接
  meterTypesLoading: "Loading meter types…", // catalog.meterTypes.loading
  meterTypesEmpty: "No meter types yet.", // catalog.meterTypes.empty
  meterTypesComponents: "Components ← quantity field", // catalog.meterTypes.field.components
  navProviderPrices: "Provider prices", // nav.providerPrices —— 顶栏的价格项
  navFxRates: "Exchange rates", // nav.fxRates —— 顶栏的汇率项
  pricingForbidden: "Only administrators can manage prices and exchange rates.", // pricing.forbidden
  pricingProvider: "Provider", // pricing.field.provider —— 价格表格的列头
  pricingModel: "Model", // pricing.field.model
  pricesLoading: "Loading provider prices…", // pricing.prices.loading
  pricesEmpty: "No provider prices yet.", // pricing.prices.empty
  fxCurrency: "Currency", // fx.field.currency —— 汇率版本表格的列头
  fxRate: "Rate (MYR)", // fx.field.rate
  fxLoading: "Loading exchange rates…", // fx.loading
  fxEmpty: "No exchange rates yet.", // fx.empty
  fxAttemptsTitle: "BNM fetch attempts", // fx.attempts.title —— 拉取记录一节的标题
  fxAttemptedAt: "Attempted at", // fx.attempts.field.attemptedAt —— 拉取记录表格的列头
  fxOutcome: "Outcome", // fx.attempts.field.outcome
  fxAttemptsLoading: "Loading fetch attempts…", // fx.attempts.loading
  fxAttemptsEmpty: "No fetch attempts yet.", // fx.attempts.empty
  navPricingRules: "Pricing rules", // nav.pricingRules —— 顶栏的规则项
  navPricingPreview: "Pricing preview", // nav.pricingPreview —— 顶栏的试算项
  rulesForbidden: "Only administrators can manage pricing rules and preview charges.", // pricing.rules.forbidden
  rulesScope: "Scope", // pricing.rules.field.scope —— 规则表格的列头
  rulesStrategy: "Strategy", // pricing.rules.field.strategy
  rulesLoading: "Loading pricing rules…", // pricing.rules.loading
  rulesEmpty: "No pricing rules yet.", // pricing.rules.empty
  previewCustomer: "Customer", // pricing.rules.field.customer —— 试算表单的 label
  previewUsageType: "Usage type", // pricing.preview.field.usageType
  previewOccurredAt: "Occurred at (Kuala Lumpur time)", // pricing.preview.field.occurredAt
  previewSubmit: "Calculate", // pricing.preview.submit —— 只确认它在，不点
  previewEstimateNote: "Estimated cost, not visible to customers.", // pricing.preview.estimateNote
  navUsageEvents: "Usage events", // nav.usageEvents —— 顶栏的用量事件项
  usageForbidden: "Only administrators can view usage events.", // usage.forbidden
  usageLoading: "Loading usage events…", // usage.loading
  usageEmpty: "No usage events yet.", // usage.empty
  usageOccurredAt: "Occurred at", // usage.field.occurredAt —— 用量事件表格的列头
  usageBillableCost: "Billable amount", // usage.field.billableCost
  navUsageAnalytics: "Usage analytics", // nav.usageAnalytics —— 顶栏的用量分析项
  analyticsForbidden: "Only administrators can view usage analytics.", // usageAnalytics.forbidden
  analyticsLoading: "Loading usage analytics…", // usageAnalytics.loading
  analyticsEmpty: "No processed usage matches these conditions.", // usageAnalytics.empty
  analyticsEmptyPage: "There are no groups on this page.", // usageAnalytics.emptyPage
  analyticsPrepaid: "Prepaid (MYR)", // usageAnalytics.prepaid.title —— 分析表格的列组表头
  analyticsInternal: "Internal metering only (MYR)", // usageAnalytics.internal.title
  customersForbidden: "Only administrators can manage customers.", // customers.forbidden —— 客户区块（含签名密钥面板）的无权限
  projectName: "Project name", // customers.projects.field.name —— 项目表格的列头
  projectDescription: "Description", // customers.projects.field.description
  projectsLoading: "Loading projects…", // customers.projects.loading
  projectsLoadFailed: "The projects could not be loaded.", // customers.projects.loadFailed
  projectsEmpty: "This customer has no projects yet.", // customers.projects.empty
  webhookTitle: "Webhook signing secrets", // webhookSigning.title —— 签名密钥面板的 aria-label
  webhookLoading: "Loading webhook signing secrets…", // webhookSigning.loading
  webhookLoadFailed: "The webhook signing secrets could not be loaded.", // webhookSigning.loadFailed
  webhookEmpty: "This project has no webhook signing secrets yet.", // webhookSigning.empty
  webhookEmptyPage: "There are no signing versions on this page.", // webhookSigning.emptyPage
  webhookKeyVersion: "Key version", // webhookSigning.field.keyVersion —— 签名密钥表格的列头
  webhookStatus: "Status", // webhookSigning.field.status
  webhookActivatedAt: "Activated", // webhookSigning.field.activatedAt（集成凭据的表格没有这一列，靠它区分两张表）
  // 签名版本状态 → 「Status」列的文案（webhookSigning.status.pending / active / retired）。
 webhookStatusText: { PENDING: "Pending", ACTIVE: "Active", RETIRED: "Retired" },
  aiAccessTitle: "AI client keys", // aiAccess.title
  aiAccessLoading: "Loading AI client keys…", // aiAccess.loading
  aiAccessLoadFailed: "The AI client keys could not be loaded.", // aiAccess.loadFailed
  aiAccessEmpty: "This project has no AI client keys yet.", // aiAccess.empty
  aiAccessClientId: "Client ID", // aiAccess.field.clientId
  aiAccessStatus: "Status", // aiAccess.field.status
  aiAccessStatusText: { ACTIVE: "Active", REVOKED: "Revoked" },
  integrationTitle: "Integration status and status webhook", // projectIntegration.title —— 集成状态面板的 aria-label
  integrationStatusLabel: "Integration status", // projectIntegration.field.status —— 面板里 Descriptions 的一项
  integrationLoading: "Loading the status webhook address…", // projectIntegration.webhook.loading
  integrationLoadFailed: "The status webhook address could not be loaded.", // projectIntegration.webhook.loadFailed
  // 项目集成状态 → 面板「Integration status」一项的文案（projectIntegration.status.enabled / disabled）。
  integrationStatusText: { ENABLED: "Enabled", DISABLED: "Disabled" },
  walletTxTitle: "Wallet transactions", // wallet.transactions.title —— 钱包流水面板的 aria-label
  walletTxLoading: "Loading wallet transactions…", // wallet.transactions.loading
  walletTxLoadFailed: "The wallet transactions could not be loaded.", // wallet.transactions.loadFailed
  walletTxEmpty: "This customer has no wallet transactions yet.", // wallet.transactions.empty
  walletTxSequence: "Sequence", // wallet.transactions.field.sequence —— 流水表格的列头
  walletTxBalanceAfter: "Balance after", // wallet.transactions.field.balanceAfter
  portalTitle: "Customer portal account", // portalAccount.title —— 门户账号面板的 aria-label
  portalLoading: "Loading the portal account…", // portalAccount.loading
  portalLoadFailed: "The portal account could not be loaded.", // portalAccount.loadFailed
  portalNone: "This customer has no portal account yet.", // portalAccount.none
  portalStatusLabel: "Portal status", // portalAccount.field.status —— 面板里 Descriptions 的一项
  // 门户账号状态 → 面板「Portal status」一项的文案（portalAccount.status.invited / active / disabled）。
  portalStatusText: { INVITED: "Invited", ACTIVE: "Active", DISABLED: "Disabled" },
  lowBalanceTitle: "Low balance threshold", // lowBalance.title —— 低余额阈值面板的 aria-label
  lowBalanceLoading: "Loading the low balance threshold…", // lowBalance.loading
  lowBalanceLoadFailed: "The low balance threshold could not be loaded.", // lowBalance.loadFailed
  lowBalanceLabel: "Threshold", // lowBalance.field.threshold —— 面板里 Descriptions 的一项
  lowBalanceNotSet: "Not set", // lowBalance.notSet
  portalShellTitle: "Customer portal", // portal.title —— 门户外壳 header 里的标题
  portalHomeLoading: "Loading your account…", // portal.home.loading
  portalHomeLoadFailed: "Your account details could not be loaded.", // portal.home.loadFailed
  portalHomeCompanyName: "Company name", // portal.home.field.companyName —— 门户首页 Descriptions 的一项
  portalHomeAccountStatus: "Account status", // portal.home.field.accountStatus
  // 客户账户状态 → 门户首页「Account status」一项的文案（portal.home.accountStatus.pendingActivation / enabled / disabled / closed）。
  portalHomeAccountStatusText: {
    PENDING_ACTIVATION: "Pending activation",
    ENABLED: "Enabled",
    DISABLED: "Disabled",
    CLOSED: "Closed",
  },
  portalHomeReadOnly: "Your account is read-only", // portal.home.readOnly.title —— 只读提示的标题
  portalNavWallet: "Wallet", // portal.nav.wallet —— 门户导航的钱包项
  portalWalletTitle: "Your wallet", // portal.wallet.summary.title —— 余额卡的 aria-label
  portalWalletLoading: "Loading your wallet…", // portal.wallet.summary.loading
  portalWalletLoadFailed: "Your wallet could not be loaded.", // portal.wallet.summary.loadFailed
  portalWalletBalance: "Balance", // portal.wallet.field.balance —— 余额卡 Descriptions 的一项
  portalTxTitle: "Transactions", // portal.wallet.transactions.title —— 流水一块的 aria-label
  portalTxLoading: "Loading your transactions…", // portal.wallet.transactions.loading
  portalTxLoadFailed: "Your transactions could not be loaded.", // portal.wallet.transactions.loadFailed
  portalTxEmpty: "You have no transactions yet.", // portal.wallet.transactions.empty
  portalTxBalanceAfter: "Balance after", // portal.wallet.transactions.field.balanceAfter —— 流水表格的列头
  portalTxEventId: "Event ID", // portal.wallet.transactions.field.eventId
  portalRecentTitle: "Recent transactions", // portal.home.recentTransactions.title —— 门户首页最近流水一块的 aria-label
  portalNavUsage: "Usage", // portal.nav.usage —— 门户导航的用量项
  portalUsageTitle: "Usage", // portal.usage.title —— 用量一块的 aria-label
  portalUsageLoading: "Loading your usage…", // portal.usage.loading
  portalUsageLoadFailed: "Your usage could not be loaded.", // portal.usage.loadFailed
  // portal.usage.queryTooBroad
  portalUsageTooBroad:
    "This search covers too much usage to finish in time. Narrow the date range or change the filters, then try again.",
  portalUsageEmpty: "No billed usage matches these filters.", // portal.usage.empty
  portalUsageTime: "Time (MYT)", // portal.usage.field.occurredAt —— 用量表格的列头
  portalUsageCharged: "Charged (MYR)", // portal.usage.field.chargedAmount
  portalUsageSummaryTitle: "This month's usage", // portal.home.usageSummary.title —— 本月用量卡的 aria-label
  portalUsageSummaryLoading: "Loading this month's usage…", // portal.home.usageSummary.loading
  portalUsageSummaryLoadFailed: "This month's usage could not be loaded.", // portal.home.usageSummary.loadFailed
  // portal.home.usageSummary.queryTooBroad
  portalUsageSummaryTooBroad:
    "This month's usage cannot be worked out right now. You can view it on the Usage page with a shorter date range.",
  // portal.home.usageSummary.month 是「{{month}} (Malaysia time)」：月份标题是响应的 month 加这个后缀。
  portalUsageSummaryMonthSuffix: " (Malaysia time)",
  portalUsageSummaryCharged: "Charged (MYR)", // portal.home.usageSummary.field.chargedAmount —— 卡里 Descriptions 的一项
  portalUsageSummaryRequests: "Requests", // portal.home.usageSummary.field.requestCount
  portalUsageSummaryConversations: "Conversations", // portal.home.usageSummary.field.conversationCount
  portalUsageSummaryChart: "Requests per day (Malaysia time)", // portal.home.usageSummary.chart.caption —— 柱状图的 aria-label
  // 管理端顶栏的全部项（frontend/src/layouts/AppLayout.tsx；nav.dashboard、nav.customers、nav.audit、nav.catalog、
  // nav.providerPrices、nav.fxRates、nav.pricingRules、nav.pricingPreview、nav.usageEvents、nav.usageAnalytics）。
  // 门户首页上一项都不许出现。
  adminNavTexts: [
    "Dashboard",
    "Customers",
    "Audit log",
    "AI catalog",
    "Provider prices",
    "Exchange rates",
    "Pricing rules",
    "Pricing preview",
    "Usage events",
    "Usage analytics",
  ],
  // antd 分页「下一页」那个 <li> 的 title。不在 en.json 里：来自 antd 自带的 enUS 语言包
  // （frontend/src/App.tsx 的 <ConfigProvider locale={enUS}>）。
  nextPageTitle: "Next Page",
  // antd 表格展开 / 收起按钮的 aria-label，同样来自 antd 的 enUS 语言包（Table.expand / Table.collapse）。
  expandRowLabel: "Expand row",
  collapseRowLabel: "Collapse row",
  acceptInvitationPassword: "Choose a password", // acceptInvitation.password —— 接受邀请表单的 label
  acceptInvitationConfirm: "Confirm your password", // acceptInvitation.confirmPassword
  acceptInvitationSubmit: "Activate my account", // acceptInvitation.submit —— 只确认它在，不点
  // 路由路径，取自 frontend/src/routes/paths.ts 的 ROUTES。
  loginPath: "/login",
  acceptInvitationPath: "/accept-invitation",
  customersPath: "/customers",
  customerCreatePath: "/customers/new",
  auditPath: "/audit",
  catalogProvidersPath: "/catalog/providers",
  catalogMeterTypesPath: "/catalog/meter-types",
  providerPricesPath: "/pricing/provider-prices",
  fxRatesPath: "/fx-rates",
  pricingRulesPath: "/pricing/rules",
  pricingPreviewPath: "/pricing/preview",
  usageEventsPath: "/usage-events",
  usageAnalyticsPath: "/usage-analytics",
  portalHomePath: "/portal",
  portalWalletPath: "/portal/wallet",
  portalUsagePath: "/portal/usage",
  // 管理端顶栏各项指向的路径（paths.ts 的 dashboard、customers、audit、catalogProviders、catalogMeterTypes、
  // providerPrices、fxRates、pricingRules、pricingPreview、usageEvents、usageAnalytics）。门户首页上不许有指向它们的链接。
  adminPaths: [
    "/",
    "/customers",
    "/audit",
    "/catalog/providers",
    "/catalog/meter-types",
    "/pricing/provider-prices",
    "/fx-rates",
    "/pricing/rules",
    "/pricing/preview",
    "/usage-events",
    "/usage-analytics",
  ],
};

/** 邀请链接里装令牌的查询参数名（frontend/src/routes/paths.ts 的 ACCEPT_INVITATION_TOKEN_PARAM）。 */
const ACCEPT_INVITATION_TOKEN_PARAM = "token";
/** check_accept_invitation_page 用的占位令牌：全零，不对应任何真实邀请；表单不提交，它也不会发给后端。 */
const PLACEHOLDER_INVITATION_TOKEN = "0".repeat(64);

/** 夹具客户的公司名前缀。只看、只打开以它开头的客户。 */
const FIXTURE_PREFIX = "[TEST] Acceptance Fixture";

/** 详情接口的路径（frontend/src/api/adminCustomers.ts 的 CUSTOMERS_URL + customerUrl）。 */
const CUSTOMER_DETAIL_API = /^\/api\/v1\/admin\/customers\/([^/]+)$/;

/** 列表里夹具那一行链接的 href（frontend/src/routes/paths.ts 的 customerDetailPath）。 */
const CUSTOMER_DETAIL_HREF = /^\/customers\/([^/?#]+)$/;

/** 审计查询接口的路径（frontend/src/api/adminAudit.ts 的 AUDIT_LOGS_URL）。 */
const AUDIT_LOGS_API = /^\/api\/v1\/admin\/audit-logs$/;

/** check_audit_entry 筛的动作：app/models/auth.py 的 AuditAction.LOGIN。 */
const LOGIN_ACTION = "LOGIN";
/** 本次登录留下的那条审计应有的其余字段（app/services/auth.py 写登录审计的那一处）。 */
const LOGIN_ACTOR_ROLE = "ADMIN";
const LOGIN_ENTITY_TYPE = "users";
/** 那条审计的 created_at 与本次 login 步骤完成时刻的最大差距。 */
const LOGIN_AUDIT_MAX_SKEW_MS = 120_000;
/** 审计页只按动作筛：页面发出的查询串里只允许有这几个参数。 */
const AUDIT_QUERY_PARAMS = new Set(["page", "page_size", "action"]);
/** 后端的不带时区 UTC 时间（docs/api.md「时间」）。 */
const NAIVE_UTC = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$/;

/** 计量类型列表接口的路径（frontend/src/api/adminCatalog.ts 的 METER_TYPES_URL）。 */
const METER_TYPES_API = /^\/api\/v1\/admin\/usage-meter-types$/;
/**
 * 迁移种子的 9 个计量类型（docs/design/AIH-TASK-025-ai-catalog.md §2「种子」）。LLM_TOKEN 是唯一的多字段形态，
 * 4 个分量（input / output / cache write / cache read）。
 */
const SEED_METER_TYPES = [
  "LLM_TOKEN",
  "EMBEDDING_TOKEN",
  "AUDIO_SECOND",
  "AUDIO_MINUTE",
  "TTS_CHARACTER",
  "IMAGE_GENERATION",
  "OCR_PAGE",
  "DOCUMENT_PAGE",
  "CUSTOM",
];
const LLM_TOKEN_CODE = "LLM_TOKEN";
const LLM_TOKEN_COMPONENTS = 4;

/** 签名密钥列表接口的路径（frontend/src/api/webhookSigning.ts 的 secretsUrl）。 */
const WEBHOOK_SECRETS_API = /^\/api\/v1\/admin\/customers\/([^/]+)\/projects\/([^/]+)\/webhook-secrets$/;
const AI_ACCESS_API = /^\/api\/v1\/admin\/customers\/([^/]+)\/projects\/([^/]+)\/ai-access$/;

/** 项目列表接口的路径（frontend/src/api/adminCustomers.ts 的 listProjects）。 */
const PROJECT_LIST_API = /^\/api\/v1\/admin\/customers\/([^/]+)\/projects$/;

/** 用量分析接口的路径（frontend/src/api/adminUsageAnalytics.ts 的 USAGE_ANALYTICS_URL）。 */
const USAGE_ANALYTICS_API = /^\/api\/v1\/admin\/usage-analytics$/;

/** 钱包流水接口的路径（frontend/src/api/walletTransactions.ts 的 listWalletTransactions）。 */
const WALLET_TRANSACTIONS_API = /^\/api\/v1\/admin\/customers\/([^/]+)\/wallet\/transactions$/;

/** 门户账号接口的路径（frontend/src/api/portalAccounts.ts 的 portalAccountUrl）。 */
const PORTAL_ACCOUNT_API = /^\/api\/v1\/admin\/customers\/([^/]+)\/portal-account$/;
/** 还没开通时 GET 的错误码（docs/api.md「管理端门户账号」）。 */
const PORTAL_ACCOUNT_NOT_FOUND = "PORTAL_ACCOUNT_NOT_FOUND";

/** 低余额阈值接口的路径（frontend/src/api/adminCustomers.ts 的 lowBalanceThresholdUrl）。 */
const LOW_BALANCE_THRESHOLD_API = /^\/api\/v1\/admin\/customers\/([^/]+)\/low-balance-threshold$/;
/** 阈值对象里的金额：恰好 8 位小数的字符串（docs/api.md「管理端低余额阈值」）。 */
const THRESHOLD_AMOUNT = /^[0-9]+\.[0-9]{8}$/;

/** 客户自己的摘要接口的路径（frontend/src/api/customer.ts 的 getCustomerMe）。 */
const CUSTOMER_ME_API = /^\/api\/v1\/customer\/me$/;

/** 客户自己的钱包与流水接口的路径（frontend/src/api/customerWallet.ts 的两个 URL 常量）。 */
const CUSTOMER_WALLET_API = /^\/api\/v1\/customer\/wallet$/;
const CUSTOMER_WALLET_TRANSACTIONS_API = /^\/api\/v1\/customer\/wallet\/transactions$/;
/** 门户首页最近流水的条数（frontend/src/features/portal/PortalHomePage.tsx 的 RECENT_TRANSACTIONS_PAGE_SIZE）。 */
const RECENT_TRANSACTIONS_PAGE_SIZE = 5;

/** 客户自己的用量接口的路径（frontend/src/api/customerUsage.ts 的 CUSTOMER_USAGE_EVENTS_URL）。 */
const CUSTOMER_USAGE_EVENTS_API = /^\/api\/v1\/customer\/usage-events$/;
/** 查询超过执行预算时的错误码（docs/api.md「客户用量」）。 */
const QUERY_TOO_BROAD = "QUERY_TOO_BROAD";
/** 用量页计费额的显示位数（设计闸门 #398 §2「金额」：最多 6 位小数）。门户首页本月用量卡同一规则（#403 §2「金额」）。 */
const USAGE_PLACES = 6;

/** 客户自己的用量汇总接口的路径（frontend/src/api/customerUsage.ts 的 CUSTOMER_USAGE_SUMMARY_URL）。 */
const CUSTOMER_USAGE_SUMMARY_API = /^\/api\/v1\/customer\/usage-summary$/;

// ---------------------------------------------------------------------------
// 时限（全部有上限；总预算留出余量，保证在 timeout_seconds = 300 之内打出结论）
// ---------------------------------------------------------------------------

const TOTAL_BUDGET_MS = 270_000;
/** 兜底：万一某个 CDP 命令卡住超出预算，到点直接按当前步骤的超时码收尾。 */
const HARD_STOP_MS = 285_000;
const STDIN_TIMEOUT_MS = 10_000;
const STDIN_MAX_BYTES = 64 * 1024;
const CONNECT_TIMEOUT_MS = 10_000;
const COMMAND_TIMEOUT_MS = 15_000;
const NAVIGATION_TIMEOUT_MS = 45_000;
const DOCUMENT_EVENT_WAIT_MS = 5_000;
const ELEMENT_WAIT_MS = 30_000;
const LOGIN_WAIT_MS = 30_000;
const RESPONSE_WAIT_MS = 20_000;
const SCROLL_WAIT_MS = 3_000;
/** 试算页的表单出现之后再看多久：下拉要的数据是之后才回来的，取不到时的错误提示也是之后才出现。 */
const PREVIEW_LOOKUP_SETTLE_MS = 3_000;
/** 接受邀请表单出现之后再看多久：确认它没有随后被送去登录页、也没有被收走。 */
const ACCEPT_INVITATION_SETTLE_MS = 2_000;
const POLL_MS = 250;
/** 报到之后停一下再往下走：Worker 读到报到行后自己截图，不能让它截到下一步的页面。 */
const STEP_SETTLE_MS = 1_000;
/** 逐页找夹具客户的页数上限（另受总预算约束）。 */
const MAX_LIST_PAGES = 100;

// ---------------------------------------------------------------------------
// TOTP（RFC 6238）与 base32（RFC 4648）
// ---------------------------------------------------------------------------

const TOTP_STEP_SECONDS = 30;
const TOTP_DIGITS = 6;
/** 当前时间窗剩不到这么多秒就等下一个窗口再算，免得码在提交途中过期。 */
const TOTP_MIN_REMAINING_SECONDS = 5;
const BASE32_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567";

/** base32 → 字节。忽略空白、连字符与末尾的 `=`，大小写不敏感；有非法字符或解出来为空时返回 null。 */
function base32Decode(text) {
  const clean = text.replace(/[\s-]/g, "").replace(/=+$/, "").toUpperCase();
  let value = 0;
  let bits = 0;
  const bytes = [];
  for (const char of clean) {
    const index = BASE32_ALPHABET.indexOf(char);
    if (index < 0) {
      return null;
    }
    value = (value << 5) | index;
    bits += 5;
    if (bits >= 8) {
      bits -= 8;
      bytes.push((value >>> bits) & 0xff);
    }
    value &= (1 << bits) - 1;
  }
  return bytes.length === 0 ? null : Buffer.from(bytes);
}

function totp(key, nowMs) {
  const counter = Buffer.alloc(8);
  counter.writeBigUInt64BE(BigInt(Math.floor(nowMs / 1000 / TOTP_STEP_SECONDS)));
  const mac = createHmac("sha1", key).update(counter).digest();
  const offset = mac[mac.length - 1] & 0x0f;
  const binary =
    ((mac[offset] & 0x7f) << 24) |
    (mac[offset + 1] << 16) |
    (mac[offset + 2] << 8) |
    mac[offset + 3];
  return String(binary % 10 ** TOTP_DIGITS).padStart(TOTP_DIGITS, "0");
}

async function freshTotp(key) {
  const remaining = TOTP_STEP_SECONDS - ((Date.now() / 1000) % TOTP_STEP_SECONDS);
  if (remaining < TOTP_MIN_REMAINING_SECONDS) {
    await sleep(Math.ceil(remaining * 1000) + 250);
  }
  return totp(key, Date.now());
}

// ---------------------------------------------------------------------------
// 金额格式：照抄 frontend/src/components/MoneyText.tsx 的 formatAmount / formatMoney
// （不 import 前端代码；那边改了规则要回来同步）。只做字符串操作：整数部分千分位，
// 小数去末尾 0、至少 2 位，不舍入；全零不带负号；不是十进制字符串就原样返回。
// ---------------------------------------------------------------------------

const DECIMAL = /^(-?)(\d+)(?:\.(\d+))?$/;
const MIN_FRACTION_DIGITS = 2;

function formatAmount(amount) {
  const match = DECIMAL.exec(amount);
  if (match === null) {
    return amount;
  }
  const [, sign = "", integer = "", fraction = ""] = match;
  const grouped = integer.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  const trimmed = fraction.replace(/0+$/, "").padEnd(MIN_FRACTION_DIGITS, "0");
  const isZero = /^0*$/.test(integer) && /^0*$/.test(fraction);
  return `${isZero ? "" : sign}${grouped}.${trimmed}`;
}

function formatMoney(amount, currency) {
  return `${currency} ${formatAmount(amount)}`;
}

/**
 * check_portal_wallet 的期望值：十进制字符串按 2 位 ROUND_HALF_UP 舍入，纯字符串运算（不经浮点），不加千分位；
 * 舍入结果为 -0.00 时回 0.00。规则同 frontend/src/components/MoneyText.tsx 的 formatAmountRounded，但这里是
 * 独立算一遍、不照抄它的代码。不是十进制字符串时回 null。check_portal_usage 用同一个函数按 6 位舍入（roundHalfUp）。
 */
const BALANCE_PLACES = 2;

function roundHalfUp2(amount) {
  return roundHalfUp(amount, BALANCE_PLACES);
}

/** 按 places 位 ROUND_HALF_UP 舍入，结果恰好 places 位小数；其余规则见 roundHalfUp2。 */
function roundHalfUp(amount, places) {
  const match = typeof amount === "string" ? DECIMAL.exec(amount) : null;
  if (match === null) {
    return null;
  }
  const [, sign = "", integer = "", fraction = ""] = match;
  const padded = fraction.padEnd(places + 1, "0");
  let digits = `${integer}${padded.slice(0, places)}`.split("").map(Number);
  if (padded.charCodeAt(places) >= "5".charCodeAt(0)) {
    // 从最低位往上进位；最高位也进位时在前面补一个 1。
    let index = digits.length - 1;
    while (index >= 0 && digits[index] === 9) {
      digits[index] = 0;
      index -= 1;
    }
    if (index < 0) {
      digits = [1, ...digits];
    } else {
      digits[index] += 1;
    }
  }
  const text = digits.join("");
  const whole = text.slice(0, text.length - places).replace(/^0+(?=\d)/, "");
  const rounded = `${whole}.${text.slice(text.length - places)}`;
  const isZero = /^0+$/.test(text);
  return `${isZero ? "" : sign}${rounded}`;
}

/**
 * check_portal_usage 比较前的规范写法：十进制字符串去掉小数末尾多余的 0、至少留 2 位（页面 formatAmount 的写法，
 * 不加千分位）。`"0.041200"` → `"0.0412"`，`"12.500000"` → `"12.50"`。不是十进制字符串时回 null。
 */
function trimFraction(amount) {
  const match = typeof amount === "string" ? DECIMAL.exec(amount) : null;
  if (match === null) {
    return null;
  }
  const [, sign = "", integer = "", fraction = ""] = match;
  return `${sign}${integer}.${fraction.replace(/0+$/, "").padEnd(MIN_FRACTION_DIGITS, "0")}`;
}

// ---------------------------------------------------------------------------
// 输出与收尾
// ---------------------------------------------------------------------------

/** 步骤失败，只带失败码 —— 不带任何消息，免得有人顺手把它打出来。 */
class StepFailure extends Error {
  constructor(code) {
    super("");
    this.code = code;
  }
}

const fail = (code) => new StepFailure(code);

let finished = false;
let currentStep = "login";
let cdp = null;

function emit(line) {
  process.stdout.write(`${line}\n`);
}

function finish(verdictLine, exitCode) {
  if (finished) {
    return;
  }
  finished = true;
  emit(verdictLine);
  try {
    cdp?.close();
  } catch {
    // 连接已经断了也没关系：页面目标留给 Worker 截图，本脚本不关它。
  }
  process.stdout.write("", () => process.exit(exitCode));
}

function finishPass() {
  finish("ACCEPTANCE_VERDICT: PASS", 0);
}

function finishFail(step, code) {
  finish(`ACCEPTANCE_VERDICT: FAIL ${step} ${code}`, 1);
}

/** 等待超时时各步骤报的码。 */
function timeoutCode(step) {
  return step === "login" || step === "login_customer" ? "login_failed" : "element_missing";
}

// ---------------------------------------------------------------------------
// 输入
// ---------------------------------------------------------------------------

const DEFAULT_FIRST_STEP = "login";
/** 步骤名也会出现在输出里，所以只收这种形状，别的都算输入不合法。 */
const STEP_NAME = /^[A-Za-z0-9_.-]{1,64}$/;
/** 与 .platform/project.yaml 对 acceptance_hosts 的要求一致：小写 DNS 主机名，不带协议、端口、路径、通配符。 */
const HOST_NAME = /^(?=.{1,253}$)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+$/;

const isObject = (value) => typeof value === "object" && value !== null && !Array.isArray(value);
const isStepName = (value) => typeof value === "string" && STEP_NAME.test(value);
const isNonEmptyString = (value) => typeof value === "string" && value !== "";

function isWebSocketUrl(value) {
  if (typeof value !== "string") {
    return false;
  }
  try {
    const url = new URL(value);
    return url.protocol === "ws:" || url.protocol === "wss:";
  } catch {
    return false;
  }
}

function readFirstLine() {
  return new Promise((resolve) => {
    let buffered = "";
    let done = false;
    const settle = (value) => {
      if (done) {
        return;
      }
      done = true;
      process.stdin.removeAllListeners("data");
      process.stdin.pause();
      resolve(value);
    };
    process.stdin.setEncoding("utf8");
    process.stdin.on("data", (chunk) => {
      buffered += chunk;
      const newline = buffered.indexOf("\n");
      if (newline >= 0) {
        settle(buffered.slice(0, newline));
      } else if (buffered.length > STDIN_MAX_BYTES) {
        settle(null);
      }
    });
    process.stdin.on("end", () => settle(buffered));
    process.stdin.on("error", () => settle(null));
    setTimeout(() => settle(null), STDIN_TIMEOUT_MS).unref();
  });
}

/** 校验输入。不合法时只回第一个步骤名（用于 FAIL 行），不回任何收到的内容。 */
function parseInput(line) {
  let value;
  try {
    value = JSON.parse(line ?? "");
  } catch {
    return { ok: false, firstStep: DEFAULT_FIRST_STEP };
  }
  const firstStep =
    isObject(value) && Array.isArray(value.steps) && isStepName(value.steps[0])
      ? value.steps[0]
      : DEFAULT_FIRST_STEP;
  const invalid = { ok: false, firstStep };
  if (!isObject(value)) {
    return invalid;
  }
  const { steps, writes, attempt, cdp: cdpUrl, hosts, credentials } = value;
  if (!Array.isArray(steps) || steps.length === 0 || !steps.every(isStepName)) {
    return invalid;
  }
  if (typeof writes !== "boolean" || !Number.isInteger(attempt) || attempt < 1) {
    return invalid;
  }
  if (!isWebSocketUrl(cdpUrl)) {
    return invalid;
  }
  if (!Array.isArray(hosts) || hosts.length === 0 || !hosts.every((h) => typeof h === "string" && HOST_NAME.test(h))) {
    return invalid;
  }
  if (!isObject(credentials)) {
    return invalid;
  }
  const { email, password, totp_secret: totpSecret } = credentials;
  if (!isNonEmptyString(email) || !isNonEmptyString(password) || !isNonEmptyString(totpSecret)) {
    return invalid;
  }
  const totpKey = base32Decode(totpSecret);
  if (totpKey === null) {
    return invalid;
  }
  // 夹具客户门户账号的两个字段只给 login_customer 用：这里不因它们缺失而判输入不合法（其余步骤不受影响），
  // 只把「不是非空字符串」记成 null，由 login_customer 自己报 login_failed。
  const { customer_email: customerEmail, customer_password: customerPassword } = credentials;
  const customerCredentials =
    isNonEmptyString(customerEmail) && isNonEmptyString(customerPassword)
      ? { email: customerEmail, password: customerPassword }
      : null;
  // writes 为 true 时同样只做只读步骤，所以这里不再往下传它。
  return { ok: true, steps, cdpUrl, host: hosts[0], email, password, totpKey, customerCredentials };
}

// ---------------------------------------------------------------------------
// CDP 连接（全局 WebSocket，flatten 会话）
// ---------------------------------------------------------------------------

class CdpConnection {
  #ws;
  #nextId = 1;
  #pending = new Map();
  #listeners = new Set();
  closed = false;

  static open(url) {
    return new Promise((resolve, reject) => {
      if (typeof globalThis.WebSocket !== "function") {
        reject(fail("navigation_failed"));
        return;
      }
      let ws;
      try {
        ws = new globalThis.WebSocket(url);
      } catch {
        reject(fail("navigation_failed"));
        return;
      }
      const timer = setTimeout(() => {
        try {
          ws.close();
        } catch {
          // 连不上就是连不上，下面按失败处理。
        }
        reject(fail("navigation_failed"));
      }, CONNECT_TIMEOUT_MS);
      ws.addEventListener(
        "open",
        () => {
          clearTimeout(timer);
          resolve(new CdpConnection(ws));
        },
        { once: true },
      );
      ws.addEventListener(
        "error",
        () => {
          clearTimeout(timer);
          reject(fail("navigation_failed"));
        },
        { once: true },
      );
    });
  }

  constructor(ws) {
    this.#ws = ws;
    ws.addEventListener("message", (event) => this.#onMessage(event.data));
    ws.addEventListener("close", () => {
      this.closed = true;
      for (const { reject, timer } of this.#pending.values()) {
        clearTimeout(timer);
        reject(new Error(""));
      }
      this.#pending.clear();
    });
  }

  #onMessage(data) {
    let message;
    try {
      message = JSON.parse(typeof data === "string" ? data : String(data));
    } catch {
      return;
    }
    if (typeof message.id === "number") {
      const pending = this.#pending.get(message.id);
      if (pending === undefined) {
        return;
      }
      this.#pending.delete(message.id);
      clearTimeout(pending.timer);
      // ⚠️ 浏览器回的错误消息不往外带：它可能含 URL。
      if (message.error !== undefined) {
        pending.reject(new Error(""));
      } else {
        pending.resolve(message.result ?? {});
      }
      return;
    }
    if (typeof message.method === "string") {
      for (const listener of this.#listeners) {
        listener(message);
      }
    }
  }

  send(method, params = {}, sessionId = undefined, timeoutMs = COMMAND_TIMEOUT_MS) {
    if (this.closed) {
      return Promise.reject(new Error(""));
    }
    const id = this.#nextId++;
    const payload = sessionId === undefined ? { id, method, params } : { id, method, params, sessionId };
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.#pending.delete(id);
        reject(new Error(""));
      }, timeoutMs);
      this.#pending.set(id, { resolve, reject, timer });
      try {
        this.#ws.send(JSON.stringify(payload));
      } catch {
        this.#pending.delete(id);
        clearTimeout(timer);
        reject(new Error(""));
      }
    });
  }

  onEvent(listener) {
    this.#listeners.add(listener);
  }

  close() {
    this.#ws.close();
  }
}

// ---------------------------------------------------------------------------
// 被动观察网络（Network.enable）。不拦截、不改写、不读 Cookie；只记下判断所需的最少信息，
// 不保存 URL、请求头或响应头。
// ---------------------------------------------------------------------------

const network = {
  host: "",
  /** 主文档：loaderId → HTTP 状态码。 */
  documents: new Map(),
  /** 最近一次收到的文档响应的状态码；Page.navigate 本身超时、拿不到 loaderId 时用它判。 */
  lastDocumentStatus: null,
  /** 详情接口：requestId → { customerId, status, finished, failed, seq }。 */
  details: new Map(),
  /** 审计查询接口：requestId → { loginFirstPage, status, finished, failed, seq }。查询串本身不保存。 */
  audits: new Map(),
  /** 计量类型列表接口：requestId → { status, finished, failed, seq }。查询串本身不保存。 */
  meterTypes: new Map(),
  /** 签名密钥列表接口：requestId → { customerId, projectId, status, finished, failed, seq }。查询串本身不保存。 */
  webhookSecrets: new Map(),
  aiAccess: new Map(),
  /** 项目列表接口：requestId → { customerId, firstPage, status, finished, failed, seq }。查询串本身不保存。 */
  projectLists: new Map(),
  /** 用量分析接口：requestId → { status, finished, failed, seq }。查询串本身不保存。 */
  usageAnalytics: new Map(),
  /** 钱包流水接口：requestId → { customerId, newest, status, finished, failed, seq }。查询串本身不保存。 */
  walletTransactions: new Map(),
  /** 门户账号接口（只记 GET）：requestId → { customerId, status, finished, failed, seq }。 */
  portalAccounts: new Map(),
  /** 低余额阈值接口（只记 GET）：requestId → { customerId, status, finished, failed, seq }。 */
  lowBalanceThresholds: new Map(),
  /** 客户自己的摘要接口（只记 GET）：requestId → { status, finished, failed, seq }。 */
  customerMe: new Map(),
  /** 客户自己的钱包接口（只记 GET）：requestId → { status, finished, failed, seq }。 */
  customerWallet: new Map(),
  /** 客户自己的流水接口（只记 GET）：requestId → { newest, recent, status, finished, failed, seq }。查询串本身不保存。 */
  customerTransactions: new Map(),
  /** 客户自己的用量接口（只记 GET）：requestId → { newest, status, finished, failed, seq }。查询串本身不保存。 */
  customerUsage: new Map(),
  /** 客户自己的用量汇总接口（只记 GET）：requestId → { newest, status, finished, failed, seq }。查询串本身不保存。 */
  customerUsageSummary: new Map(),
  seq: 0,
};

/** 各类被观察的接口里，requestId 对应的那一条记录。 */
function trackedRequest(requestId) {
  return (
    network.details.get(requestId) ??
    network.audits.get(requestId) ??
    network.meterTypes.get(requestId) ??
    network.webhookSecrets.get(requestId) ??
    network.aiAccess.get(requestId) ??
    network.projectLists.get(requestId) ??
    network.usageAnalytics.get(requestId) ??
    network.walletTransactions.get(requestId) ??
    network.portalAccounts.get(requestId) ??
    network.lowBalanceThresholds.get(requestId) ??
    network.customerMe.get(requestId) ??
    network.customerWallet.get(requestId) ??
    network.customerTransactions.get(requestId) ??
    network.customerUsage.get(requestId) ??
    network.customerUsageSummary.get(requestId)
  );
}

/**
 * 是不是客户自己的用量汇总接口（同源 https、路径恰好是 /api/v1/customer/usage-summary）：是的话回 { newest }（不带
 * month，即后端按请求时刻取的当月，门户首页发的就是这一种），不是回 null。只留这一个布尔，不保存 URL。
 */
function customerUsageSummaryTarget(url) {
  if (typeof url !== "string") {
    return null;
  }
  try {
    const parsed = new URL(url);
    if (
      parsed.protocol !== "https:" ||
      parsed.hostname !== network.host ||
      !CUSTOMER_USAGE_SUMMARY_API.test(parsed.pathname)
    ) {
      return null;
    }
    return { newest: !parsed.searchParams.has("month") };
  } catch {
    return null;
  }
}

/**
 * 是不是客户自己的用量接口（同源 https、路径恰好是 /api/v1/customer/usage-events）：是的话回 { newest }（不带 cursor
 * 与 event_id 的最新一页，即页面按日期区间查的第一页），不是回 null。只留这一个布尔，不保存 URL。
 */
function customerUsageTarget(url) {
  if (typeof url !== "string") {
    return null;
  }
  try {
    const parsed = new URL(url);
    if (
      parsed.protocol !== "https:" ||
      parsed.hostname !== network.host ||
      !CUSTOMER_USAGE_EVENTS_API.test(parsed.pathname)
    ) {
      return null;
    }
    const query = parsed.searchParams;
    return { newest: !query.has("cursor") && !query.has("event_id") };
  } catch {
    return null;
  }
}

/**
 * 是不是客户自己的钱包或流水接口（同源 https）：钱包回 { kind: "wallet" }，流水回 { kind: "transactions", newest, recent }
 * （newest：不带 before_sequence 的最新一页；recent：其中 page_size 恰好一个且为 5，即门户首页的最近流水），都不是回
 * null。只留这些，不保存 URL。
 */
function customerWalletTarget(url) {
  if (typeof url !== "string") {
    return null;
  }
  try {
    const parsed = new URL(url);
    if (parsed.protocol !== "https:" || parsed.hostname !== network.host) {
      return null;
    }
    if (CUSTOMER_WALLET_API.test(parsed.pathname)) {
      return { kind: "wallet" };
    }
    if (CUSTOMER_WALLET_TRANSACTIONS_API.test(parsed.pathname)) {
      const query = parsed.searchParams;
      const newest = !query.has("before_sequence");
      const pageSizes = query.getAll("page_size");
      const recent = newest && pageSizes.length === 1 && pageSizes[0] === String(RECENT_TRANSACTIONS_PAGE_SIZE);
      return { kind: "transactions", newest, recent };
    }
    return null;
  } catch {
    return null;
  }
}

/** 是不是客户自己的摘要接口（同源 https、路径恰好是 /api/v1/customer/me，查询串不看、不保存）。 */
function isCustomerMe(url) {
  if (typeof url !== "string") {
    return false;
  }
  try {
    const parsed = new URL(url);
    return parsed.protocol === "https:" && parsed.hostname === network.host && CUSTOMER_ME_API.test(parsed.pathname);
  } catch {
    return false;
  }
}

/** 是不是门户账号接口（同源 https、路径恰好是 …/portal-account）：是的话回路径里的客户 id，不是回 null。 */
function portalAccountCustomerId(url) {
  if (typeof url !== "string") {
    return null;
  }
  try {
    const parsed = new URL(url);
    if (parsed.protocol !== "https:" || parsed.hostname !== network.host) {
      return null;
    }
    const match = PORTAL_ACCOUNT_API.exec(parsed.pathname);
    return match === null ? null : decodeURIComponent(match[1]);
  } catch {
    return null;
  }
}

/** 是不是低余额阈值接口（同源 https、路径恰好是 …/low-balance-threshold）：是的话回路径里的客户 id，不是回 null。 */
function lowBalanceThresholdCustomerId(url) {
  if (typeof url !== "string") {
    return null;
  }
  try {
    const parsed = new URL(url);
    if (parsed.protocol !== "https:" || parsed.hostname !== network.host) {
      return null;
    }
    const match = LOW_BALANCE_THRESHOLD_API.exec(parsed.pathname);
    return match === null ? null : decodeURIComponent(match[1]);
  } catch {
    return null;
  }
}

/**
 * 是不是钱包流水接口（同源 https）：是的话回路径里的客户 id 与「是不是不带游标、不带来源筛选的最新一页」，
 * 不是回 null。只留这两样，不保存 URL。
 */
function walletTransactionsTarget(url) {
  if (typeof url !== "string") {
    return null;
  }
  try {
    const parsed = new URL(url);
    if (parsed.protocol !== "https:" || parsed.hostname !== network.host) {
      return null;
    }
    const match = WALLET_TRANSACTIONS_API.exec(parsed.pathname);
    if (match === null) {
      return null;
    }
    const query = parsed.searchParams;
    return {
      customerId: decodeURIComponent(match[1]),
      newest: !query.has("before_sequence") && !query.has("reference_type"),
    };
  } catch {
    return null;
  }
}

/** 是不是用量分析接口（同源 https、路径恰好是这个接口，查询串不看、不保存）。 */
function isUsageAnalytics(url) {
  if (typeof url !== "string") {
    return false;
  }
  try {
    const parsed = new URL(url);
    return parsed.protocol === "https:" && parsed.hostname === network.host && USAGE_ANALYTICS_API.test(parsed.pathname);
  } catch {
    return false;
  }
}

/**
 * 是不是项目列表接口（同源 https）：是的话回路径里的客户 id 与「查询串是不是第 1 页」，不是回 null。
 * 只留这两样，不保存 URL。
 */
function projectListTarget(url) {
  if (typeof url !== "string") {
    return null;
  }
  try {
    const parsed = new URL(url);
    if (parsed.protocol !== "https:" || parsed.hostname !== network.host) {
      return null;
    }
    const match = PROJECT_LIST_API.exec(parsed.pathname);
    if (match === null) {
      return null;
    }
    const query = parsed.searchParams;
    return {
      customerId: decodeURIComponent(match[1]),
      firstPage: query.getAll("page").length === 1 && query.get("page") === "1",
    };
  } catch {
    return null;
  }
}

/** 是不是签名密钥列表接口（同源 https）：是的话回路径里的客户与项目 id，不是回 null。查询串不看、不保存。 */
function webhookSecretsTarget(url) {
  if (typeof url !== "string") {
    return null;
  }
  try {
    const parsed = new URL(url);
    if (parsed.protocol !== "https:" || parsed.hostname !== network.host) {
      return null;
    }
    const match = WEBHOOK_SECRETS_API.exec(parsed.pathname);
    return match === null
      ? null
      : { customerId: decodeURIComponent(match[1]), projectId: decodeURIComponent(match[2]) };
  } catch {
    return null;
  }
}

function aiAccessTarget(url) {
  if (typeof url !== "string") return null;
  try {
    const parsed = new URL(url);
    if (parsed.protocol !== "https:" || parsed.hostname !== network.host) return null;
    const match = AI_ACCESS_API.exec(parsed.pathname);
    return match === null ? null : { customerId: decodeURIComponent(match[1]), projectId: decodeURIComponent(match[2]) };
  } catch { return null; }
}

/** 是不是计量类型列表接口（同源 https、路径恰好是列表，查询串不看）。 */
function isMeterTypesList(url) {
  if (typeof url !== "string") {
    return false;
  }
  try {
    const parsed = new URL(url);
    return parsed.protocol === "https:" && parsed.hostname === network.host && METER_TYPES_API.test(parsed.pathname);
  } catch {
    return false;
  }
}

function detailCustomerId(url) {
  if (typeof url !== "string") {
    return null;
  }
  try {
    const parsed = new URL(url);
    if (parsed.protocol !== "https:" || parsed.hostname !== network.host) {
      return null;
    }
    const match = CUSTOMER_DETAIL_API.exec(parsed.pathname);
    return match === null ? null : decodeURIComponent(match[1]);
  } catch {
    return null;
  }
}

/**
 * 是不是审计查询接口：不是回 null；是的话回「查询串是不是恰好第 1 页、只按 action=LOGIN 筛」。
 * 只留这一个布尔，不保存 URL。
 */
function auditQueryKind(url) {
  if (typeof url !== "string") {
    return null;
  }
  try {
    const parsed = new URL(url);
    if (parsed.protocol !== "https:" || parsed.hostname !== network.host || !AUDIT_LOGS_API.test(parsed.pathname)) {
      return null;
    }
    const query = parsed.searchParams;
    const onlyKnown = [...query.keys()].every((name) => AUDIT_QUERY_PARAMS.has(name));
    return {
      loginFirstPage:
        onlyKnown &&
        query.getAll("page").length === 1 &&
        query.get("page") === "1" &&
        query.getAll("action").length === 1 &&
        query.get("action") === LOGIN_ACTION,
    };
  } catch {
    return null;
  }
}

function observeNetwork(sessionId) {
  cdp.onEvent((message) => {
    if (message.sessionId !== sessionId) {
      return;
    }
    const params = message.params ?? {};
    switch (message.method) {
      case "Network.requestWillBeSent": {
        const customerId = detailCustomerId(params.request?.url);
        if (customerId !== null && params.request?.method === "GET") {
          network.details.set(params.requestId, {
            customerId,
            status: null,
            finished: false,
            failed: false,
            seq: ++network.seq,
          });
        }
        const audit = auditQueryKind(params.request?.url);
        if (audit !== null && params.request?.method === "GET") {
          network.audits.set(params.requestId, {
            loginFirstPage: audit.loginFirstPage,
            status: null,
            finished: false,
            failed: false,
            seq: ++network.seq,
          });
        }
        if (isMeterTypesList(params.request?.url) && params.request?.method === "GET") {
          network.meterTypes.set(params.requestId, {
            status: null,
            finished: false,
            failed: false,
            seq: ++network.seq,
          });
        }
        const webhook = webhookSecretsTarget(params.request?.url);
        if (webhook !== null && params.request?.method === "GET") {
          network.webhookSecrets.set(params.requestId, {
            customerId: webhook.customerId,
            projectId: webhook.projectId,
            status: null,
            finished: false,
            failed: false,
            seq: ++network.seq,
          });
        }
        const aiAccess = aiAccessTarget(params.request?.url);
        if (aiAccess !== null && params.request?.method === "GET") {
          network.aiAccess.set(params.requestId, {
            customerId: aiAccess.customerId,
            projectId: aiAccess.projectId,
            status: null,
            finished: false,
            failed: false,
            seq: ++network.seq,
          });
        }
        const projectList = projectListTarget(params.request?.url);
        if (projectList !== null && params.request?.method === "GET") {
          network.projectLists.set(params.requestId, {
            customerId: projectList.customerId,
            firstPage: projectList.firstPage,
            status: null,
            finished: false,
            failed: false,
            seq: ++network.seq,
          });
        }
        if (isUsageAnalytics(params.request?.url) && params.request?.method === "GET") {
          network.usageAnalytics.set(params.requestId, {
            status: null,
            finished: false,
            failed: false,
            seq: ++network.seq,
          });
        }
        const walletTransactions = walletTransactionsTarget(params.request?.url);
        if (walletTransactions !== null && params.request?.method === "GET") {
          network.walletTransactions.set(params.requestId, {
            customerId: walletTransactions.customerId,
            newest: walletTransactions.newest,
            status: null,
            finished: false,
            failed: false,
            seq: ++network.seq,
          });
        }
        const portalCustomerId = portalAccountCustomerId(params.request?.url);
        if (portalCustomerId !== null && params.request?.method === "GET") {
          network.portalAccounts.set(params.requestId, {
            customerId: portalCustomerId,
            status: null,
            finished: false,
            failed: false,
            seq: ++network.seq,
          });
        }
        const thresholdCustomerId = lowBalanceThresholdCustomerId(params.request?.url);
        if (thresholdCustomerId !== null && params.request?.method === "GET") {
          network.lowBalanceThresholds.set(params.requestId, {
            customerId: thresholdCustomerId,
            status: null,
            finished: false,
            failed: false,
            seq: ++network.seq,
          });
        }
        if (isCustomerMe(params.request?.url) && params.request?.method === "GET") {
          network.customerMe.set(params.requestId, {
            status: null,
            finished: false,
            failed: false,
            seq: ++network.seq,
          });
        }
        const customerWallet = customerWalletTarget(params.request?.url);
        if (customerWallet !== null && params.request?.method === "GET") {
          if (customerWallet.kind === "wallet") {
            network.customerWallet.set(params.requestId, {
              status: null,
              finished: false,
              failed: false,
              seq: ++network.seq,
            });
          } else {
            network.customerTransactions.set(params.requestId, {
              newest: customerWallet.newest,
              recent: customerWallet.recent,
              status: null,
              finished: false,
              failed: false,
              seq: ++network.seq,
            });
          }
        }
        const customerUsage = customerUsageTarget(params.request?.url);
        if (customerUsage !== null && params.request?.method === "GET") {
          network.customerUsage.set(params.requestId, {
            newest: customerUsage.newest,
            status: null,
            finished: false,
            failed: false,
            seq: ++network.seq,
          });
        }
        const customerUsageSummary = customerUsageSummaryTarget(params.request?.url);
        if (customerUsageSummary !== null && params.request?.method === "GET") {
          network.customerUsageSummary.set(params.requestId, {
            newest: customerUsageSummary.newest,
            status: null,
            finished: false,
            failed: false,
            seq: ++network.seq,
          });
        }
        break;
      }
      case "Network.responseReceived": {
        const status = typeof params.response?.status === "number" ? params.response.status : null;
        if (params.type === "Document" && typeof params.loaderId === "string") {
          network.documents.set(params.loaderId, status);
          network.lastDocumentStatus = status;
        }
        const tracked = trackedRequest(params.requestId);
        if (tracked !== undefined) {
          tracked.status = status;
        }
        break;
      }
      case "Network.loadingFinished": {
        const tracked = trackedRequest(params.requestId);
        if (tracked !== undefined) {
          tracked.finished = true;
        }
        break;
      }
      case "Network.loadingFailed": {
        const tracked = trackedRequest(params.requestId);
        if (tracked !== undefined) {
          tracked.failed = true;
        }
        break;
      }
      default:
        break;
    }
  });
}

/** 这个客户最近一次成功读完的详情响应的 requestId。 */
function latestDetailResponse(customerId) {
  let best = null;
  for (const [requestId, detail] of network.details) {
    if (
      detail.customerId === customerId &&
      detail.status === 200 &&
      detail.finished &&
      !detail.failed &&
      (best === null || detail.seq > best.seq)
    ) {
      best = { requestId, seq: detail.seq };
    }
  }
  return best;
}

/** afterSeq 之后发出、第 1 页只按 action=LOGIN 筛、成功读完的审计查询里最近的一次。 */
function latestLoginAuditResponse(afterSeq) {
  let best = null;
  for (const [requestId, audit] of network.audits) {
    if (
      audit.loginFirstPage &&
      audit.seq > afterSeq &&
      audit.status === 200 &&
      audit.finished &&
      !audit.failed &&
      (best === null || audit.seq > best.seq)
    ) {
      best = { requestId, seq: audit.seq };
    }
  }
  return best;
}

/** 在此之前发出、还没有结束的第 1 页 LOGIN 审计查询：要等它落定再挑「最近的一次」。 */
function loginAuditInFlight(afterSeq) {
  for (const audit of network.audits.values()) {
    if (audit.loginFirstPage && audit.seq > afterSeq && !audit.finished && !audit.failed) {
      return true;
    }
  }
  return false;
}

/** afterSeq 之后发出、成功读完的计量类型列表请求里最近的一次；还有在路上的就回 null，等它落定。 */
function latestMeterTypesResponse(afterSeq) {
  let best = null;
  for (const [requestId, request] of network.meterTypes) {
    if (request.seq <= afterSeq) {
      continue;
    }
    if (!request.finished && !request.failed) {
      return null;
    }
    if (request.status === 200 && request.finished && !request.failed && (best === null || request.seq > best.seq)) {
      best = { requestId, seq: request.seq };
    }
  }
  return best;
}

/**
 * afterSeq 之后发出、这个客户的这个项目的、成功读完的签名密钥列表请求里最近的一次；还有在路上的就回 null，
 * 等它落定。
 */
function latestWebhookSecretsResponse(afterSeq, customerId, projectId) {
  let best = null;
  for (const [requestId, request] of network.webhookSecrets) {
    if (request.seq <= afterSeq || request.customerId !== customerId || request.projectId !== projectId) {
      continue;
    }
    if (!request.finished && !request.failed) {
      return null;
    }
    if (request.status === 200 && request.finished && !request.failed && (best === null || request.seq > best.seq)) {
      best = { requestId, seq: request.seq };
    }
  }
  return best;
}

function latestAiAccessResponse(afterSeq, customerId, projectId) {
  let best = null;
  for (const [requestId, request] of network.aiAccess) {
    if (request.seq <= afterSeq || request.customerId !== customerId || request.projectId !== projectId) continue;
    if (!request.finished && !request.failed) return null;
    if (request.status === 200 && request.finished && !request.failed && (best === null || request.seq > best.seq)) best = { requestId, seq: request.seq };
  }
  return best;
}

/**
 * 这个客户第 1 页的、成功读完的项目列表请求里最近的一次；还有在路上的就回 null，等它落定。列表是详情页打开时
 * 自己发出的（重读时也是页面自己发），所以不按序号截断。
 */
function latestProjectListResponse(customerId) {
  let best = null;
  for (const [requestId, request] of network.projectLists) {
    if (request.customerId !== customerId || !request.firstPage) {
      continue;
    }
    if (!request.finished && !request.failed) {
      return null;
    }
    if (request.status === 200 && request.finished && !request.failed && (best === null || request.seq > best.seq)) {
      best = { requestId, seq: request.seq };
    }
  }
  return best;
}

/** afterSeq 之后发出、成功读完的用量分析请求里最近的一次；还有在路上的就回 null，等它落定。 */
function latestUsageAnalyticsResponse(afterSeq) {
  let best = null;
  for (const [requestId, request] of network.usageAnalytics) {
    if (request.seq <= afterSeq) {
      continue;
    }
    if (!request.finished && !request.failed) {
      return null;
    }
    if (request.status === 200 && request.finished && !request.failed && (best === null || request.seq > best.seq)) {
      best = { requestId, seq: request.seq };
    }
  }
  return best;
}

/**
 * 这个客户不带游标、不带来源筛选的、成功读完的钱包流水请求里最近的一次；还有在路上的就回 null，等它落定。
 * 请求是详情页打开时面板自己发出的（重读时也是面板自己发），所以不按序号截断。
 */
function latestWalletTransactionsResponse(customerId) {
  let best = null;
  for (const [requestId, request] of network.walletTransactions) {
    if (request.customerId !== customerId || !request.newest) {
      continue;
    }
    if (!request.finished && !request.failed) {
      return null;
    }
    if (request.status === 200 && request.finished && !request.failed && (best === null || request.seq > best.seq)) {
      best = { requestId, seq: request.seq };
    }
  }
  return best;
}

/**
 * 这个客户的、读完的门户账号 GET 里最近的一次（200 是已开通，404 可能是未开通，由调用方看错误码）；还有在路上的
 * 就回 null，等它落定。请求是详情页打开时面板自己发出的，所以不按序号截断。
 */
function latestPortalAccountResponse(customerId) {
  let best = null;
  for (const [requestId, request] of network.portalAccounts) {
    if (request.customerId !== customerId) {
      continue;
    }
    if (!request.finished && !request.failed) {
      return null;
    }
    if (
      (request.status === 200 || request.status === 404) &&
      request.finished &&
      !request.failed &&
      (best === null || request.seq > best.seq)
    ) {
      best = { requestId, status: request.status, seq: request.seq };
    }
  }
  return best;
}

/**
 * 这个客户的、成功读完的低余额阈值 GET 里最近的一次；还有在路上的就回 null，等它落定。请求是详情页打开时面板
 * 自己发出的，所以不按序号截断。
 */
function latestLowBalanceThresholdResponse(customerId) {
  let best = null;
  for (const [requestId, request] of network.lowBalanceThresholds) {
    if (request.customerId !== customerId) {
      continue;
    }
    if (!request.finished && !request.failed) {
      return null;
    }
    if (request.status === 200 && request.finished && !request.failed && (best === null || request.seq > best.seq)) {
      best = { requestId, seq: request.seq };
    }
  }
  return best;
}

/**
 * afterSeq 之后发出、读完的 GET /api/v1/customer/me 里最近的一次（任何状态码，由调用方判）；还有在路上的就回 null，
 * 等它落定。
 */
function latestCustomerMeResponse(afterSeq) {
  let best = null;
  for (const [requestId, request] of network.customerMe) {
    if (request.seq <= afterSeq) {
      continue;
    }
    if (!request.finished && !request.failed) {
      return null;
    }
    if (request.finished && !request.failed && (best === null || request.seq > best.seq)) {
      best = { requestId, status: request.status, seq: request.seq };
    }
  }
  return best;
}

/**
 * afterSeq 之后发出、读完的 GET /api/v1/customer/wallet（requests 为 network.customerWallet）或最新一页的
 * GET /api/v1/customer/wallet/transactions（network.customerTransactions，只认 newest）里最近的一次（任何状态码，
 * 由调用方判）；还有在路上的就回 null，等它落定。check_portal_usage 拿它取不带 cursor 与 event_id 的
 * GET /api/v1/customer/usage-events（network.customerUsage，同样只认 newest）；check_portal_usage_summary 拿它取
 * 不带 month 的 GET /api/v1/customer/usage-summary（network.customerUsageSummary，同样只认 newest）。
 */
function latestCustomerWalletResponse(requests, afterSeq) {
  let best = null;
  for (const [requestId, request] of requests) {
    if (request.seq <= afterSeq || request.newest === false) {
      continue;
    }
    if (!request.finished && !request.failed) {
      return null;
    }
    if (request.finished && !request.failed && (best === null || request.seq > best.seq)) {
      best = { requestId, status: request.status, seq: request.seq };
    }
  }
  return best;
}

/**
 * afterSeq 之后发出、读完的门户首页最近流水（GET /api/v1/customer/wallet/transactions，page_size=5、不带
 * before_sequence）里最近的一次（任何状态码，由调用方判）；这样的请求还有在路上的就回 null，等它落定。
 */
function latestRecentTransactionsResponse(afterSeq) {
  let best = null;
  for (const [requestId, request] of network.customerTransactions) {
    if (request.seq <= afterSeq || request.recent !== true) {
      continue;
    }
    if (!request.finished && !request.failed) {
      return null;
    }
    if (request.finished && !request.failed && (best === null || request.seq > best.seq)) {
      best = { requestId, status: request.status, seq: request.seq };
    }
  }
  return best;
}

// ---------------------------------------------------------------------------
// 页面里执行的函数。
//
// ⚠️ 它们被 toString() 后送进页面执行，只能用参数（lib、arg）与页面自己的全局。
//    只读 DOM、聚焦、点击、滚动 —— 不发请求、不碰 Cookie 与存储。返回值只留在本进程内存里，不打印。
// ---------------------------------------------------------------------------

function pageLib() {
  const norm = (value) => String(value ?? "").replace(/\s+/g, " ").trim();
  /** 文字恰好是 text 的叶子元素（antd 的标题、提示都是一个叶子节点）。 */
  const leaf = (text) => {
    if (!document.body) {
      return null;
    }
    for (const el of document.body.querySelectorAll("*")) {
      if (el.childElementCount === 0 && norm(el.textContent) === text) {
        return el;
      }
    }
    return null;
  };
  /** 可见文字为 text 的 <label> 所关联的表单控件（antd Form.Item 的 label[for]）。 */
  const control = (text) => {
    for (const label of document.querySelectorAll("label")) {
      if (norm(label.textContent) === text && label.control) {
        return label.control;
      }
    }
    return null;
  };
  const button = (text, root) => {
    for (const el of (root ?? document).querySelectorAll("button")) {
      if (norm(el.textContent) === text) {
        return el;
      }
    }
    return null;
  };
  const alert = () => {
    for (const el of document.querySelectorAll('[role="alert"]')) {
      if (norm(el.textContent) !== "") {
        return true;
      }
    }
    return false;
  };
  /** 管理端布局：<header> 里有「Sign out」按钮与「Customers」链接（frontend/src/layouts/AppLayout.tsx）。 */
  const layout = (T) => {
    const header = document.querySelector("header");
    if (!header || !button(T.navSignOut, header)) {
      return false;
    }
    for (const a of header.querySelectorAll("a")) {
      if (norm(a.textContent) === T.navCustomers) {
        return true;
      }
    }
    return false;
  };
  /** 带边框的 antd Descriptions 是一张表：label 在 <th>，值在紧跟的 <td>。 */
  const valueCells = (labelText) => {
    const cells = [];
    for (const th of document.querySelectorAll("th")) {
      const td = th.nextElementSibling;
      if (norm(th.textContent) === labelText && td && td.tagName === "TD") {
        cells.push(td);
      }
    }
    return cells;
  };
  const common = (T) => ({
    path: location.pathname,
    appError: leaf(T.appErrorTitle) !== null,
    notFound: leaf(T.notFoundTitle) !== null,
  });
  /**
   * 审计表格（表头里有「Action」「Actor」两列，frontend/src/features/audit/AuditLogPage.tsx）的数据行，按显示顺序。
   * 只认表格自己的 thead / tbody（`:scope >`），不把展开行里嵌套的表算进来；空状态那一行只有一格，跳过。
   * 没有这张表时回 null。
   */
  const auditRows = (T) => {
    for (const table of document.querySelectorAll("table")) {
      const heads = [...table.querySelectorAll(":scope > thead > tr > th")];
      const action = heads.findIndex((th) => norm(th.textContent) === T.auditColumnAction);
      const actor = heads.findIndex((th) => norm(th.textContent) === T.auditColumnActor);
      if (action < 0 || actor < 0) {
        continue;
      }
      const rows = [];
      for (const tr of table.querySelectorAll(":scope > tbody > tr")) {
        if (tr.getAttribute("aria-hidden") === "true") {
          continue;
        }
        const cells = tr.querySelectorAll(":scope > td");
        if (cells.length !== heads.length) {
          continue;
        }
        rows.push({ tr, action: norm(cells[action].textContent), actor: norm(cells[actor].textContent) });
      }
      return rows;
    }
    return null;
  };
  /**
   * 表头恰好含 headTexts 每一列的第一张表：回 { index: 列名 → 第几列, rows: [{ tr, cells }] }，只认表格自己的
   * thead / tbody，空状态那一行（只有一格）跳过。没有这张表时回 null。
   */
  const tableWith = (headTexts) => {
    for (const table of document.querySelectorAll("table")) {
      const heads = [...table.querySelectorAll(":scope > thead > tr > th")].map((th) => norm(th.textContent));
      if (!headTexts.every((text) => heads.includes(text))) {
        continue;
      }
      const index = Object.fromEntries(headTexts.map((text) => [text, heads.indexOf(text)]));
      const rows = [];
      for (const tr of table.querySelectorAll(":scope > tbody > tr")) {
        if (tr.getAttribute("aria-hidden") === "true") {
          continue;
        }
        const cells = [...tr.querySelectorAll(":scope > td")];
        if (cells.length !== heads.length) {
          continue;
        }
        rows.push({ tr, cells });
      }
      return { index, rows };
    }
    return null;
  };
  /** 计量类型表格（表头有「Code」「Components ← quantity field」）里代码为 code 的那一行的 <tr>；没有时 null。 */
  const meterTypeRow = (T, code) => {
    const table = tableWith([T.catalogCode, T.meterTypesComponents]);
    if (table === null) {
      return null;
    }
    const row = table.rows.find(({ cells }) => norm(cells[table.index[T.catalogCode]].textContent) === code);
    return row === undefined ? null : row.tr;
  };
  /** 客户详情里项目表格（表头有「Project name」「Description」）的第一个数据行；没有时 null。 */
  const firstProjectRow = (T) => {
    const table = tableWith([T.projectName, T.projectDescription]);
    return table === null || table.rows.length === 0 ? null : table.rows[0].tr;
  };
  /**
   * 第一个项目展开区里的签名密钥面板（role=region、aria-label 为面板标题）；没展开或没有时 null。
   * antd 把展开区画成紧跟在那一行后面的 <tr>，只在这一行里找，不把别的项目的面板算进来。
   */
  const webhookRegion = (T) => {
    const first = firstProjectRow(T);
    const expanded = first === null ? null : first.nextElementSibling;
    if (!expanded) {
      return null;
    }
    for (const el of expanded.querySelectorAll('[role="region"]')) {
      if (el.getAttribute("aria-label") === T.webhookTitle) {
        return el;
      }
    }
    return null;
  };
  /** 第一个项目展开区里的集成状态面板（role=region、aria-label 为面板标题）；规则同 webhookRegion。 */
  const integrationRegion = (T) => {
    const first = firstProjectRow(T);
    const expanded = first === null ? null : first.nextElementSibling;
    if (!expanded) {
      return null;
    }
    for (const el of expanded.querySelectorAll('[role="region"]')) {
      if (el.getAttribute("aria-label") === T.integrationTitle) {
        return el;
      }
    }
    return null;
  };
  const aiAccessRegion = (T) => {
    const first = firstProjectRow(T);
    const expanded = first === null ? null : first.nextElementSibling;
    if (!expanded) return null;
    for (const el of expanded.querySelectorAll('[role="region"]')) {
      if (el.getAttribute("aria-label") === T.aiAccessTitle) return el;
    }
    return null;
  };
  /**
   * 用量分析表格：表头（两行，含列组表头）里有「Prepaid (MYR)」「Internal metering only (MYR)」两组列的那张表；
   * 没有时 null。表头是两行，单元格数与表头格数对不上，所以不用 tableWith。
   */
  const analyticsTable = (T) => {
    for (const table of document.querySelectorAll("table")) {
      const heads = [...table.querySelectorAll(":scope > thead > tr > th")].map((th) => norm(th.textContent));
      if (heads.includes(T.analyticsPrepaid) && heads.includes(T.analyticsInternal)) {
        return table;
      }
    }
    return null;
  };
  /** 客户详情里的钱包流水面板（role=region、aria-label 为面板标题）；没有时 null。 */
  const walletTxRegion = (T) => {
    for (const el of document.querySelectorAll('[role="region"]')) {
      if (el.getAttribute("aria-label") === T.walletTxTitle) {
        return el;
      }
    }
    return null;
  };
  /** 客户详情里的门户账号面板（role=region、aria-label 为面板标题）；没有时 null。 */
  const portalRegion = (T) => {
    for (const el of document.querySelectorAll('[role="region"]')) {
      if (el.getAttribute("aria-label") === T.portalTitle) {
        return el;
      }
    }
    return null;
  };
  /** 客户详情里的低余额阈值面板（role=region、aria-label 为面板标题）；没有时 null。 */
  const lowBalanceRegion = (T) => {
    for (const el of document.querySelectorAll('[role="region"]')) {
      if (el.getAttribute("aria-label") === T.lowBalanceTitle) {
        return el;
      }
    }
    return null;
  };
  return {
    norm,
    leaf,
    control,
    button,
    alert,
    layout,
    valueCells,
    common,
    auditRows,
    tableWith,
    meterTypeRow,
    firstProjectRow,
    webhookRegion,
    integrationRegion,
    aiAccessRegion,
    analyticsTable,
    walletTxRegion,
    portalRegion,
    lowBalanceRegion,
  };
}

function probeLogin(lib, T) {
  const base = lib.common(T);
  return {
    ...base,
    form:
      lib.control(T.loginEmail) !== null &&
      lib.control(T.loginPassword) !== null &&
      lib.button(T.loginSubmit) !== null,
    code: lib.control(T.loginCode) !== null && lib.button(T.loginVerify) !== null,
    enrol: lib.control(T.enrolCode) !== null || lib.leaf(T.enrolIntro) !== null,
    alert: lib.alert(),
    layout: base.path !== T.loginPath && lib.layout(T),
  };
}

/** 接受邀请页：两个密码框（label 关联着 type 为 password 的输入框）与提交按钮都在才算表单在。只读，不点。 */
function probeAcceptInvitation(lib, T) {
  const isPassword = (el) => el !== null && el.tagName === "INPUT" && el.type === "password";
  return {
    ...lib.common(T),
    form:
      isPassword(lib.control(T.acceptInvitationPassword)) &&
      isPassword(lib.control(T.acceptInvitationConfirm)) &&
      lib.button(T.acceptInvitationSubmit) !== null,
    loginForm: lib.control(T.loginEmail) !== null && lib.button(T.loginSubmit) !== null,
  };
}

function focusField(lib, labelText) {
  const el = lib.control(labelText);
  if (!el || el.disabled) {
    return false;
  }
  el.focus();
  if (typeof el.select === "function") {
    el.select();
  }
  return document.activeElement === el;
}

/** 只回长度，不回值。 */
function fieldLength(lib, labelText) {
  const el = lib.control(labelText);
  return el && typeof el.value === "string" ? el.value.length : -1;
}

function clickButton(lib, text) {
  const el = lib.button(text);
  if (!el || el.disabled) {
    return false;
  }
  el.click();
  return true;
}

function clickNavCustomers(lib, T) {
  const header = document.querySelector("header");
  if (!header) {
    return false;
  }
  for (const a of header.querySelectorAll("a")) {
    if (lib.norm(a.textContent) === T.navCustomers && a.getAttribute("href") === T.customersPath) {
      a.click();
      return true;
    }
  }
  return false;
}

function probeList(lib, { T, prefix }) {
  const base = lib.common(T);
  const rawPage = new URLSearchParams(location.search).get("page");
  const page = rawPage !== null && /^\d{1,6}$/.test(rawPage) ? Number.parseInt(rawPage, 10) : 1;
  const out = {
    ...base,
    page,
    busy: document.querySelector('[aria-busy="true"]') !== null || lib.leaf(T.listLoading) !== null,
    failed: lib.leaf(T.listLoadFailed) !== null,
    empty: lib.leaf(T.listEmpty) !== null,
    table: false,
    signature: "",
    matches: [],
    hasNext: false,
  };
  for (const table of document.querySelectorAll("table")) {
    const heads = [...table.querySelectorAll("thead th")];
    const column = heads.findIndex((th) => lib.norm(th.textContent) === T.companyName);
    if (column < 0) {
      continue;
    }
    out.table = true;
    const keys = [];
    for (const tr of table.querySelectorAll("tbody tr")) {
      if (tr.getAttribute("aria-hidden") === "true") {
        continue;
      }
      const cells = tr.querySelectorAll(":scope > td");
      // 空状态那一行只有一格（colspan），不是数据行。
      if (cells.length !== heads.length) {
        continue;
      }
      keys.push(tr.getAttribute("data-row-key") ?? "");
      const cell = cells[column];
      const link = cell.querySelector("a");
      const name = lib.norm(cell.textContent);
      if (link && name.startsWith(prefix)) {
        out.matches.push({ name, href: link.getAttribute("href") ?? "" });
      }
    }
    out.signature = keys.join(",");
    break;
  }
  const next = document.querySelector(`li[title="${T.nextPageTitle}"]`);
  out.hasNext = next !== null && next.getAttribute("aria-disabled") !== "true";
  return out;
}

function clickNextPage(lib, T) {
  const next = document.querySelector(`li[title="${T.nextPageTitle}"]`);
  if (!next || next.getAttribute("aria-disabled") === "true") {
    return false;
  }
  (next.querySelector("button") ?? next).click();
  return true;
}

function clickFixture(lib, { name, href }) {
  for (const a of document.querySelectorAll("table tbody a")) {
    if (a.getAttribute("href") === href && lib.norm(a.textContent) === name) {
      a.click();
      return true;
    }
  }
  return false;
}

function probeDetail(lib, T) {
  return {
    ...lib.common(T),
    loading: lib.leaf(T.detailLoading) !== null,
    failed: lib.leaf(T.detailLoadFailed) !== null,
    missing: lib.leaf(T.detailNotFound) !== null,
    names: lib.valueCells(T.companyName).map((td) => lib.norm(td.textContent)),
  };
}

/** 余额一项的原文（不做空白归一：要逐字相同）。 */
function readBalance(lib, T) {
  const cells = lib.valueCells(T.walletBalance);
  return { path: location.pathname, count: cells.length, text: cells.length === 1 ? cells[0].textContent : null };
}

/** 把「Balance」这一行（label 与值）滚到视口中间。只滚动，不点任何东西。 */
function scrollBalanceIntoView(lib, T) {
  const cells = lib.valueCells(T.walletBalance);
  if (cells.length !== 1) {
    return false;
  }
  (cells[0].closest("tr") ?? cells[0]).scrollIntoView({ block: "center", inline: "nearest" });
  return true;
}

function balanceInView(lib, T) {
  const cells = lib.valueCells(T.walletBalance);
  if (cells.length !== 1) {
    return false;
  }
  const rect = (cells[0].closest("tr") ?? cells[0]).getBoundingClientRect();
  return rect.height > 0 && rect.top >= 0 && rect.bottom <= window.innerHeight;
}

/**
 * 资料卡「Account status」一项的文字，以及「Disable account」「Enable account」两个按钮各有几个。
 * 只读 DOM：按钮只数、不点。
 */
function readAccountStatus(lib, T) {
  const cells = lib.valueCells(T.accountStatusLabel);
  const buttons = [...document.querySelectorAll("button")].map((el) => lib.norm(el.textContent));
  return {
    ...lib.common(T),
    count: cells.length,
    text: cells.length === 1 ? lib.norm(cells[0].textContent) : null,
    disableButtons: buttons.filter((text) => text === T.accountDisable).length,
    enableButtons: buttons.filter((text) => text === T.accountEnable).length,
  };
}

/** 把「Account status」这一行滚到视口中间。只滚动，不点任何东西。 */
function scrollAccountStatusIntoView(lib, T) {
  const cells = lib.valueCells(T.accountStatusLabel);
  if (cells.length !== 1) {
    return false;
  }
  (cells[0].closest("tr") ?? cells[0]).scrollIntoView({ block: "center", inline: "nearest" });
  return true;
}

function accountStatusInView(lib, T) {
  const cells = lib.valueCells(T.accountStatusLabel);
  if (cells.length !== 1) {
    return false;
  }
  const rect = (cells[0].closest("tr") ?? cells[0]).getBoundingClientRect();
  return rect.height > 0 && rect.top >= 0 && rect.bottom <= window.innerHeight;
}

function clickNavAudit(lib, T) {
  const header = document.querySelector("header");
  if (!header) {
    return false;
  }
  for (const a of header.querySelectorAll("a")) {
    if (lib.norm(a.textContent) === T.navAudit && a.getAttribute("href") === T.auditPath) {
      a.click();
      return true;
    }
  }
  return false;
}

/** 审计页的状态。行的文字（含操作者邮箱）只回到本进程内存里比较，不打印。 */
function probeAudit(lib, T) {
  const rows = lib.auditRows(T);
  return {
    ...lib.common(T),
    busy: document.querySelector('[aria-busy="true"]') !== null || lib.leaf(T.auditLoading) !== null,
    failed: lib.leaf(T.auditLoadFailed) !== null,
    forbidden: lib.leaf(T.auditForbidden) !== null,
    invalid: lib.leaf(T.auditInvalidFilters) !== null,
    empty: lib.leaf(T.auditEmpty) !== null || lib.leaf(T.auditEmptyFiltered) !== null,
    emptyPage: lib.leaf(T.auditEmptyPage) !== null,
    table: rows !== null,
    rows: rows === null ? [] : rows.map(({ action, actor }) => ({ action, actor })),
  };
}

/**
 * 点动作下拉里 title 恰好是 value、文字也恰好是 value 的那一项（antd Select 的选项带 title 与
 * aria-selected；LOGIN_FAILED 也匹配搜索词，靠精确比较排除）。只点看得见的那一个。
 */
function clickAuditOption(lib, value) {
  for (const el of document.querySelectorAll("[aria-selected][title]")) {
    if (el.getAttribute("title") === value && lib.norm(el.textContent) === value && el.getClientRects().length > 0) {
      el.click();
      return true;
    }
  }
  return false;
}

/** 把第 index 个数据行滚到视口中间。只滚动，不点任何东西（不展开行）。 */
function scrollAuditRowIntoView(lib, { T, index }) {
  const rows = lib.auditRows(T);
  if (rows === null || index >= rows.length) {
    return false;
  }
  rows[index].tr.scrollIntoView({ block: "center", inline: "nearest" });
  return true;
}

function auditRowInView(lib, { T, index }) {
  const rows = lib.auditRows(T);
  if (rows === null || index >= rows.length) {
    return false;
  }
  const rect = rows[index].tr.getBoundingClientRect();
  return rect.height > 0 && rect.top >= 0 && rect.bottom <= window.innerHeight;
}

function clickNavCatalog(lib, T) {
  const header = document.querySelector("header");
  if (!header) {
    return false;
  }
  for (const a of header.querySelectorAll("a")) {
    if (lib.norm(a.textContent) === T.navCatalog && a.getAttribute("href") === T.catalogProvidersPath) {
      a.click();
      return true;
    }
  }
  return false;
}

/** 供应商页的状态。错误提示（任何 role=alert）与无权限分开报。 */
function probeProviders(lib, T) {
  const forbidden = lib.leaf(T.catalogForbidden) !== null;
  return {
    ...lib.common(T),
    busy: document.querySelector('[aria-busy="true"]') !== null || lib.leaf(T.providersLoading) !== null,
    forbidden,
    failed: !forbidden && lib.alert(),
    empty: lib.leaf(T.providersEmpty) !== null,
    table: lib.tableWith([T.catalogCode, T.catalogDisplayName]) !== null,
  };
}

/** 供应商页里（不在顶栏里）的「Meter types」链接。只点这一个。 */
function clickMeterTypesLink(lib, T) {
  for (const a of document.querySelectorAll("a")) {
    if (
      a.closest("header") === null &&
      lib.norm(a.textContent) === T.providersMeterTypesLink &&
      a.getAttribute("href") === T.catalogMeterTypesPath
    ) {
      a.click();
      return true;
    }
  }
  return false;
}

/** 计量类型页的状态与表格：每个数据行的代码与「Components」格里 <li> 的个数，按显示顺序。 */
function probeMeterTypes(lib, T) {
  const forbidden = lib.leaf(T.catalogForbidden) !== null;
  const table = lib.tableWith([T.catalogCode, T.meterTypesComponents]);
  return {
    ...lib.common(T),
    busy: document.querySelector('[aria-busy="true"]') !== null || lib.leaf(T.meterTypesLoading) !== null,
    forbidden,
    failed: !forbidden && lib.alert(),
    empty: lib.leaf(T.meterTypesEmpty) !== null,
    table: table !== null,
    rows:
      table === null
        ? []
        : table.rows.map(({ cells }) => ({
            code: lib.norm(cells[table.index[T.catalogCode]].textContent),
            components: cells[table.index[T.meterTypesComponents]].querySelectorAll("li").length,
          })),
  };
}

/**
 * 把代码为 code 的那一行滚到视口中间。只滚动，不点任何东西。
 * 窄视口下分量格会断词换行，LLM_TOKEN 那一行可能比视口还高：居中会把行首推出视口，这时改为行首对齐视口顶端。
 */
function scrollMeterTypeIntoView(lib, { T, code }) {
  const tr = lib.meterTypeRow(T, code);
  if (tr === null) {
    return false;
  }
  const block = tr.getBoundingClientRect().height <= window.innerHeight ? "center" : "start";
  tr.scrollIntoView({ block, inline: "nearest" });
  return true;
}

/** 放得下视口的行要整行在视口内；放不下的只要求行首在视口内。 */
function meterTypeInView(lib, { T, code }) {
  const tr = lib.meterTypeRow(T, code);
  if (tr === null) {
    return false;
  }
  const rect = tr.getBoundingClientRect();
  const topVisible = rect.top >= 0 && rect.top < window.innerHeight;
  return rect.height > 0 && topVisible && (rect.bottom <= window.innerHeight || rect.height > window.innerHeight);
}

/** 顶栏里文字为 text、href 恰好为 href 的链接（价格与汇率两项共用）。只点这一个。 */
function clickNavLink(lib, { text, href }) {
  const header = document.querySelector("header");
  if (!header) {
    return false;
  }
  for (const a of header.querySelectorAll("a")) {
    if (lib.norm(a.textContent) === text && a.getAttribute("href") === href) {
      a.click();
      return true;
    }
  }
  return false;
}

/** 价格页的状态。错误提示（任何 role=alert）与无权限分开报；有表格时表头要有「Provider」「Model」。 */
function probeProviderPrices(lib, T) {
  const forbidden = lib.leaf(T.pricingForbidden) !== null;
  return {
    ...lib.common(T),
    busy: document.querySelector('[aria-busy="true"]') !== null || lib.leaf(T.pricesLoading) !== null,
    forbidden,
    failed: !forbidden && lib.alert(),
    empty: lib.leaf(T.pricesEmpty) !== null,
    table: lib.tableWith([T.pricingProvider, T.pricingModel]) !== null,
  };
}

/**
 * 汇率页的状态：版本列表（同上）与拉取记录一节（标题「BNM fetch attempts」在不在、它的表格或空状态出没出来）。
 */
function probeFxRates(lib, T) {
  const forbidden = lib.leaf(T.pricingForbidden) !== null;
  return {
    ...lib.common(T),
    busy:
      document.querySelector('[aria-busy="true"]') !== null ||
      lib.leaf(T.fxLoading) !== null ||
      lib.leaf(T.fxAttemptsLoading) !== null,
    forbidden,
    failed: !forbidden && lib.alert(),
    empty: lib.leaf(T.fxEmpty) !== null,
    table: lib.tableWith([T.fxCurrency, T.fxRate]) !== null,
    attemptsSection: lib.leaf(T.fxAttemptsTitle) !== null,
    attemptsShown: lib.leaf(T.fxAttemptsEmpty) !== null || lib.tableWith([T.fxAttemptedAt, T.fxOutcome]) !== null,
  };
}

/** 把拉取记录一节的标题滚到视口中间。只滚动，不点任何东西。 */
function scrollFxAttemptsIntoView(lib, T) {
  const title = lib.leaf(T.fxAttemptsTitle);
  if (title === null) {
    return false;
  }
  title.scrollIntoView({ block: "center", inline: "nearest" });
  return true;
}

function fxAttemptsInView(lib, T) {
  const title = lib.leaf(T.fxAttemptsTitle);
  if (title === null) {
    return false;
  }
  const rect = title.getBoundingClientRect();
  return rect.height > 0 && rect.top >= 0 && rect.bottom <= window.innerHeight;
}

/** 规则页的状态。错误提示（任何 role=alert）与无权限分开报；有表格时表头要有「Scope」「Strategy」。 */
function probePricingRules(lib, T) {
  const forbidden = lib.leaf(T.rulesForbidden) !== null;
  return {
    ...lib.common(T),
    busy: document.querySelector('[aria-busy="true"]') !== null || lib.leaf(T.rulesLoading) !== null,
    forbidden,
    failed: !forbidden && lib.alert(),
    empty: lib.leaf(T.rulesEmpty) !== null,
    table: lib.tableWith([T.rulesScope, T.rulesStrategy]) !== null,
  };
}

/** 试算页的状态：表单的五个 label 都关联着控件、「Calculate」在、「估算成本，客户不可见」在。只读，不碰表单。 */
function probePricingPreview(lib, T) {
  const forbidden = lib.leaf(T.rulesForbidden) !== null;
  const labels = [T.previewCustomer, T.pricingProvider, T.pricingModel, T.previewUsageType, T.previewOccurredAt];
  return {
    ...lib.common(T),
    busy: document.querySelector('[aria-busy="true"]') !== null,
    forbidden,
    failed: !forbidden && lib.alert(),
    form:
      labels.every((text) => lib.control(text) !== null) &&
      lib.button(T.previewSubmit) !== null &&
      lib.leaf(T.previewEstimateNote) !== null,
  };
}

/**
 * 用量事件页的状态。错误提示（任何 role=alert）与无权限分开报；有表格时表头要有「Occurred at」「Billable amount」。
 * 页面上「成本与毛利仅管理员可见」的说明是普通段落，不是 role=alert。只读，不碰筛选表单与勾选框。
 */
function probeUsageEvents(lib, T) {
  const forbidden = lib.leaf(T.usageForbidden) !== null;
  return {
    ...lib.common(T),
    busy: document.querySelector('[aria-busy="true"]') !== null || lib.leaf(T.usageLoading) !== null,
    forbidden,
    failed: !forbidden && lib.alert(),
    empty: lib.leaf(T.usageEmpty) !== null,
    table: lib.tableWith([T.usageOccurredAt, T.usageBillableCost]) !== null,
  };
}

/**
 * 用量分析页的状态。错误提示（任何 role=alert）与无权限分开报；有表格时回带 data-row-key 的数据行数（不含 antd
 * 的测量行与空状态行）。页面上的估算说明是普通段落，不是 role=alert。只读，不碰筛选表单与分页。
 */
function probeUsageAnalytics(lib, T) {
  const forbidden = lib.leaf(T.analyticsForbidden) !== null;
  const table = lib.analyticsTable(T);
  let rows = 0;
  if (table !== null) {
    for (const tr of table.querySelectorAll(":scope > tbody > tr[data-row-key]")) {
      if (tr.getAttribute("aria-hidden") !== "true") {
        rows += 1;
      }
    }
  }
  return {
    ...lib.common(T),
    busy: document.querySelector('[aria-busy="true"]') !== null || lib.leaf(T.analyticsLoading) !== null,
    forbidden,
    failed: !forbidden && lib.alert(),
    empty: lib.leaf(T.analyticsEmpty) !== null || lib.leaf(T.analyticsEmptyPage) !== null,
    table: table !== null,
    rows,
  };
}

/** 把分析表格滚进视口：放得下时居中，放不下时顶端对齐视口顶端。只滚动，不点任何东西。 */
function scrollAnalyticsTableIntoView(lib, T) {
  const table = lib.analyticsTable(T);
  if (table === null) {
    return false;
  }
  const block = table.getBoundingClientRect().height <= window.innerHeight ? "center" : "start";
  table.scrollIntoView({ block, inline: "nearest" });
  return true;
}

/** 放得下视口的表格要整张在视口内；放不下的只要求顶端在视口内。 */
function analyticsTableInView(lib, T) {
  const table = lib.analyticsTable(T);
  if (table === null) {
    return false;
  }
  const rect = table.getBoundingClientRect();
  const topVisible = rect.top >= 0 && rect.top < window.innerHeight;
  return rect.height > 0 && topVisible && (rect.bottom <= window.innerHeight || rect.height > window.innerHeight);
}

/**
 * 客户详情里项目区块的状态：第一行的项目 id（data-row-key）与它的展开按钮现在是展开还是收起。只读。
 */
function probeProjects(lib, T) {
  const table = lib.tableWith([T.projectName, T.projectDescription]);
  const first = lib.firstProjectRow(T);
  let expand = null;
  if (first !== null) {
    for (const el of first.querySelectorAll("button[aria-label]")) {
      const label = el.getAttribute("aria-label");
      if (label === T.expandRowLabel) {
        expand = "collapsed";
      } else if (label === T.collapseRowLabel) {
        expand = "expanded";
      }
    }
  }
  return {
    ...lib.common(T),
    busy: lib.leaf(T.projectsLoading) !== null,
    failed: lib.leaf(T.projectsLoadFailed) !== null,
    empty: lib.leaf(T.projectsEmpty) !== null,
    table: table !== null,
    projectId: first === null ? null : first.getAttribute("data-row-key"),
    expand,
  };
}

/** 点第一个项目那一行的展开按钮（aria-label 为「Expand row」）。只点这一个，不点任何别的。 */
function clickExpandFirstProject(lib, T) {
  const first = lib.firstProjectRow(T);
  if (first === null) {
    return false;
  }
  for (const el of first.querySelectorAll("button")) {
    if (el.getAttribute("aria-label") === T.expandRowLabel && !el.disabled) {
      el.click();
      return true;
    }
  }
  return false;
}

/**
 * 第一个项目展开区里签名密钥面板的状态与表格：每个数据行「Key version」「Status」两列的文字，按显示顺序。
 * 只在面板里面找（不看同一展开区里的集成凭据面板）。只读，不点面板里的任何按钮。
 */
function probeWebhookSigning(lib, T) {
  const region = lib.webhookRegion(T);
  const out = {
    ...lib.common(T),
    region: region !== null,
    busy: false,
    failed: false,
    forbidden: false,
    empty: false,
    table: false,
    rows: [],
  };
  if (region === null) {
    return out;
  }
  const has = (text) =>
    [...region.querySelectorAll("*")].some((el) => el.childElementCount === 0 && lib.norm(el.textContent) === text);
  out.busy = region.querySelector('[aria-busy="true"]') !== null || has(T.webhookLoading);
  out.failed = has(T.webhookLoadFailed);
  out.forbidden = has(T.customersForbidden);
  out.empty = has(T.webhookEmpty) || has(T.webhookEmptyPage);
  const wanted = [T.webhookKeyVersion, T.webhookStatus, T.webhookActivatedAt];
  for (const table of region.querySelectorAll("table")) {
    const heads = [...table.querySelectorAll(":scope > thead > tr > th")].map((th) => lib.norm(th.textContent));
    if (!wanted.every((text) => heads.includes(text))) {
      continue;
    }
    const keyVersion = heads.indexOf(T.webhookKeyVersion);
    const status = heads.indexOf(T.webhookStatus);
    out.table = true;
    for (const tr of table.querySelectorAll(":scope > tbody > tr")) {
      if (tr.getAttribute("aria-hidden") === "true") {
        continue;
      }
      const cells = tr.querySelectorAll(":scope > td");
      // 空状态那一行只有一格（colspan），不是数据行。
      if (cells.length !== heads.length) {
        continue;
      }
      out.rows.push({ keyVersion: lib.norm(cells[keyVersion].textContent), status: lib.norm(cells[status].textContent) });
    }
    break;
  }
  return out;
}

function probeAiAccess(lib, T) {
  const region = lib.aiAccessRegion(T);
  const out = { ...lib.common(T), region: region !== null, busy: false, failed: false, forbidden: false, empty: false, table: false, rows: [] };
  if (region === null) return out;
  const has = (text) => [...region.querySelectorAll("*")].some((el) => el.childElementCount === 0 && lib.norm(el.textContent) === text);
  out.busy = region.querySelector('[aria-busy="true"]') !== null || has(T.aiAccessLoading);
  out.failed = has(T.aiAccessLoadFailed);
  out.forbidden = has(T.customersForbidden);
  out.empty = has(T.aiAccessEmpty);
  for (const table of region.querySelectorAll("table")) {
    const heads = [...table.querySelectorAll(":scope > thead > tr > th")].map((th) => lib.norm(th.textContent));
    if (!heads.includes(T.aiAccessClientId) || !heads.includes(T.aiAccessStatus)) continue;
    const clientId = heads.indexOf(T.aiAccessClientId);
    const status = heads.indexOf(T.aiAccessStatus);
    out.table = true;
    for (const tr of table.querySelectorAll(":scope > tbody > tr")) {
      const cells = tr.querySelectorAll(":scope > td");
      if (cells.length === heads.length) out.rows.push({ clientId: lib.norm(cells[clientId].textContent), status: lib.norm(cells[status].textContent) });
    }
    break;
  }
  return out;
}

function scrollAiAccessPanelIntoView(lib, T) {
  const region = lib.aiAccessRegion(T);
  if (region === null) return false;
  region.scrollIntoView({ block: region.getBoundingClientRect().height <= window.innerHeight ? "center" : "start", inline: "nearest" });
  return true;
}

function aiAccessPanelInView(lib, T) {
  const region = lib.aiAccessRegion(T);
  if (region === null) return false;
  const rect = region.getBoundingClientRect();
  return rect.height > 0 && rect.top >= 0 && rect.top < window.innerHeight && (rect.bottom <= window.innerHeight || rect.height > window.innerHeight);
}

/** 把签名密钥面板滚进视口：放得下时居中，放不下时顶端对齐视口顶端。只滚动，不点任何东西。 */
function scrollWebhookPanelIntoView(lib, T) {
  const region = lib.webhookRegion(T);
  if (region === null) {
    return false;
  }
  const block = region.getBoundingClientRect().height <= window.innerHeight ? "center" : "start";
  region.scrollIntoView({ block, inline: "nearest" });
  return true;
}

/** 放得下视口的面板要整块在视口内；放不下的只要求顶端在视口内。 */
function webhookPanelInView(lib, T) {
  const region = lib.webhookRegion(T);
  if (region === null) {
    return false;
  }
  const rect = region.getBoundingClientRect();
  const topVisible = rect.top >= 0 && rect.top < window.innerHeight;
  return rect.height > 0 && topVisible && (rect.bottom <= window.innerHeight || rect.height > window.innerHeight);
}

/**
 * 第一个项目展开区里集成状态面板的状态：加载中、加载失败、无权限，以及「Integration status」一项的文字（面板里
 * 带边框的 Descriptions：label 在 <th>，值在紧跟的 <td>；没有这一项时为 null）。只在面板里面找。只读，不点面板里的
 * 任何按钮、不碰地址输入框。
 */
function probeProjectIntegration(lib, T) {
  const region = lib.integrationRegion(T);
  const out = {
    ...lib.common(T),
    region: region !== null,
    busy: false,
    failed: false,
    forbidden: false,
    status: null,
  };
  if (region === null) {
    return out;
  }
  const has = (text) =>
    [...region.querySelectorAll("*")].some((el) => el.childElementCount === 0 && lib.norm(el.textContent) === text);
  out.busy = region.querySelector('[aria-busy="true"]') !== null || has(T.integrationLoading);
  out.failed = has(T.integrationLoadFailed);
  out.forbidden = has(T.customersForbidden);
  for (const th of region.querySelectorAll("th")) {
    const td = th.nextElementSibling;
    if (lib.norm(th.textContent) === T.integrationStatusLabel && td && td.tagName === "TD") {
      out.status = lib.norm(td.textContent);
      break;
    }
  }
  return out;
}

/** 把集成状态面板滚进视口：放得下时居中，放不下时顶端对齐视口顶端。只滚动，不点任何东西。 */
function scrollIntegrationPanelIntoView(lib, T) {
  const region = lib.integrationRegion(T);
  if (region === null) {
    return false;
  }
  const block = region.getBoundingClientRect().height <= window.innerHeight ? "center" : "start";
  region.scrollIntoView({ block, inline: "nearest" });
  return true;
}

/** 放得下视口的面板要整块在视口内；放不下的只要求顶端在视口内。 */
function integrationPanelInView(lib, T) {
  const region = lib.integrationRegion(T);
  if (region === null) {
    return false;
  }
  const rect = region.getBoundingClientRect();
  const topVisible = rect.top >= 0 && rect.top < window.innerHeight;
  return rect.height > 0 && topVisible && (rect.bottom <= window.innerHeight || rect.height > window.innerHeight);
}

/**
 * 钱包流水面板的状态与表格：带 data-row-key 的数据行数（不含 antd 的测量行与空状态行）与第一行「Sequence」列的
 * 文字。只在面板里面找。只读，不碰下拉、按钮与链接。
 */
function probeWalletTransactions(lib, T) {
  const region = lib.walletTxRegion(T);
  const out = {
    ...lib.common(T),
    region: region !== null,
    busy: false,
    failed: false,
    forbidden: false,
    empty: false,
    table: false,
    rows: 0,
    firstSequence: null,
  };
  if (region === null) {
    return out;
  }
  const has = (text) =>
    [...region.querySelectorAll("*")].some((el) => el.childElementCount === 0 && lib.norm(el.textContent) === text);
  out.busy = region.querySelector('[aria-busy="true"]') !== null || has(T.walletTxLoading);
  out.failed = has(T.walletTxLoadFailed);
  out.forbidden = has(T.customersForbidden);
  out.empty = has(T.walletTxEmpty);
  for (const table of region.querySelectorAll("table")) {
    const heads = [...table.querySelectorAll(":scope > thead > tr > th")].map((th) => lib.norm(th.textContent));
    if (!heads.includes(T.walletTxSequence) || !heads.includes(T.walletTxBalanceAfter)) {
      continue;
    }
    const sequence = heads.indexOf(T.walletTxSequence);
    out.table = true;
    for (const tr of table.querySelectorAll(":scope > tbody > tr[data-row-key]")) {
      if (tr.getAttribute("aria-hidden") === "true") {
        continue;
      }
      const cells = tr.querySelectorAll(":scope > td");
      if (cells.length !== heads.length) {
        continue;
      }
      if (out.rows === 0) {
        out.firstSequence = lib.norm(cells[sequence].textContent);
      }
      out.rows += 1;
    }
    break;
  }
  return out;
}

/** 把钱包流水面板滚进视口：放得下时居中，放不下时顶端对齐视口顶端。只滚动，不点任何东西。 */
function scrollWalletTxPanelIntoView(lib, T) {
  const region = lib.walletTxRegion(T);
  if (region === null) {
    return false;
  }
  const block = region.getBoundingClientRect().height <= window.innerHeight ? "center" : "start";
  region.scrollIntoView({ block, inline: "nearest" });
  return true;
}

/** 放得下视口的面板要整块在视口内；放不下的只要求顶端在视口内。 */
function walletTxPanelInView(lib, T) {
  const region = lib.walletTxRegion(T);
  if (region === null) {
    return false;
  }
  const rect = region.getBoundingClientRect();
  const topVisible = rect.top >= 0 && rect.top < window.innerHeight;
  return rect.height > 0 && topVisible && (rect.bottom <= window.innerHeight || rect.height > window.innerHeight);
}

/**
 * 门户账号面板的状态：在加载、加载失败、无权限、未开通，以及「Portal status」一项的文字（面板里带边框的
 * Descriptions：label 在 <th>，值在紧跟的 <td>；没有这一项时 null）。只在面板里面找。只读，不碰按钮与表单。
 */
function probePortalAccount(lib, T) {
  const region = lib.portalRegion(T);
  const out = {
    ...lib.common(T),
    region: region !== null,
    busy: false,
    failed: false,
    forbidden: false,
    none: false,
    statusCount: 0,
    statusText: null,
  };
  if (region === null) {
    return out;
  }
  const has = (text) =>
    [...region.querySelectorAll("*")].some((el) => el.childElementCount === 0 && lib.norm(el.textContent) === text);
  out.busy = region.querySelector('[aria-busy="true"]') !== null || has(T.portalLoading);
  out.failed = has(T.portalLoadFailed);
  out.forbidden = has(T.customersForbidden);
  out.none = has(T.portalNone);
  const cells = [];
  for (const th of region.querySelectorAll("th")) {
    const td = th.nextElementSibling;
    if (lib.norm(th.textContent) === T.portalStatusLabel && td && td.tagName === "TD") {
      cells.push(td);
    }
  }
  out.statusCount = cells.length;
  out.statusText = cells.length === 1 ? lib.norm(cells[0].textContent) : null;
  return out;
}

/** 把门户账号面板滚进视口：放得下时居中，放不下时顶端对齐视口顶端。只滚动，不点任何东西。 */
function scrollPortalPanelIntoView(lib, T) {
  const region = lib.portalRegion(T);
  if (region === null) {
    return false;
  }
  const block = region.getBoundingClientRect().height <= window.innerHeight ? "center" : "start";
  region.scrollIntoView({ block, inline: "nearest" });
  return true;
}

/** 放得下视口的面板要整块在视口内；放不下的只要求顶端在视口内。 */
function portalPanelInView(lib, T) {
  const region = lib.portalRegion(T);
  if (region === null) {
    return false;
  }
  const rect = region.getBoundingClientRect();
  const topVisible = rect.top >= 0 && rect.top < window.innerHeight;
  return rect.height > 0 && topVisible && (rect.bottom <= window.innerHeight || rect.height > window.innerHeight);
}

/**
 * 低余额阈值面板的状态：在加载、加载失败、无权限，以及「Threshold」一项（面板里带边框的 Descriptions：label 在
 * <th>，值在紧跟的 <td>）的个数与文字（不是恰好一项时文字为 null）。只在面板里面找。只读，不碰按钮与表单。
 */
function probeLowBalanceThreshold(lib, T) {
  const region = lib.lowBalanceRegion(T);
  const out = {
    ...lib.common(T),
    region: region !== null,
    busy: false,
    failed: false,
    forbidden: false,
    valueCount: 0,
    valueText: null,
  };
  if (region === null) {
    return out;
  }
  const has = (text) =>
    [...region.querySelectorAll("*")].some((el) => el.childElementCount === 0 && lib.norm(el.textContent) === text);
  out.busy = region.querySelector('[aria-busy="true"]') !== null || has(T.lowBalanceLoading);
  out.failed = has(T.lowBalanceLoadFailed);
  out.forbidden = has(T.customersForbidden);
  const cells = [];
  for (const th of region.querySelectorAll("th")) {
    const td = th.nextElementSibling;
    if (lib.norm(th.textContent) === T.lowBalanceLabel && td && td.tagName === "TD") {
      cells.push(td);
    }
  }
  out.valueCount = cells.length;
  out.valueText = cells.length === 1 ? lib.norm(cells[0].textContent) : null;
  return out;
}

/** 把低余额阈值面板滚进视口：放得下时居中，放不下时顶端对齐视口顶端。只滚动，不点任何东西。 */
function scrollLowBalancePanelIntoView(lib, T) {
  const region = lib.lowBalanceRegion(T);
  if (region === null) {
    return false;
  }
  const block = region.getBoundingClientRect().height <= window.innerHeight ? "center" : "start";
  region.scrollIntoView({ block, inline: "nearest" });
  return true;
}

/** 放得下视口的面板要整块在视口内；放不下的只要求顶端在视口内。 */
function lowBalancePanelInView(lib, T) {
  const region = lib.lowBalanceRegion(T);
  if (region === null) {
    return false;
  }
  const rect = region.getBoundingClientRect();
  const topVisible = rect.top >= 0 && rect.top < window.innerHeight;
  return rect.height > 0 && topVisible && (rect.bottom <= window.innerHeight || rect.height > window.innerHeight);
}

/**
 * 客户登录各阶段：在 probeLogin 之外，再看门户外壳（frontend/src/layouts/PortalLayout.tsx：<header> 里有「Sign out」
 * 按钮与「Customer portal」标题）在不在。只读 DOM。
 */
function probeCustomerLogin(lib, T) {
  const header = document.querySelector("header");
  const portalShell =
    header !== null &&
    lib.button(T.navSignOut, header) !== null &&
    [...header.querySelectorAll("*")].some(
      (el) => el.childElementCount === 0 && lib.norm(el.textContent) === T.portalShellTitle,
    );
  const base = lib.common(T);
  return {
    ...base,
    form:
      lib.control(T.loginEmail) !== null &&
      lib.control(T.loginPassword) !== null &&
      lib.button(T.loginSubmit) !== null,
    code: lib.control(T.loginCode) !== null && lib.button(T.loginVerify) !== null,
    enrol: lib.control(T.enrolCode) !== null || lib.leaf(T.enrolIntro) !== null,
    alert: lib.alert(),
    adminLayout: base.path !== T.loginPath && lib.layout(T),
    portalShell: base.path !== T.loginPath && portalShell,
  };
}

/**
 * 门户首页的状态：门户外壳、在加载、加载失败、「Company name」「Account status」两项的文字（带边框的 Descriptions：
 * label 在 <th>，值在紧跟的 <td>；不是恰好一项时为 null）、只读提示在不在，以及页面上有没有管理端菜单项。
 * 只读 DOM，不点、不填。
 */
function probePortalHome(lib, T) {
  const header = document.querySelector("header");
  const has = (text) => lib.leaf(text) !== null;
  const single = (label) => {
    const cells = lib.valueCells(label);
    return { count: cells.length, text: cells.length === 1 ? lib.norm(cells[0].textContent) : null };
  };
  const company = single(T.portalHomeCompanyName);
  const status = single(T.portalHomeAccountStatus);
  let adminMenu = document.querySelector('[role="menu"], [role="menuitem"]') !== null;
  for (const a of document.querySelectorAll("a")) {
    let path = null;
    try {
      path = new URL(a.href, location.href).pathname;
    } catch {
      path = null;
    }
    if (T.adminNavTexts.includes(lib.norm(a.textContent)) || (path !== null && T.adminPaths.includes(path))) {
      adminMenu = true;
    }
  }
  return {
    ...lib.common(T),
    portalShell:
      header !== null &&
      lib.button(T.navSignOut, header) !== null &&
      [...header.querySelectorAll("*")].some(
        (el) => el.childElementCount === 0 && lib.norm(el.textContent) === T.portalShellTitle,
      ),
    adminLayout: lib.layout(T),
    busy: has(T.portalHomeLoading),
    failed: has(T.portalHomeLoadFailed),
    companyCount: company.count,
    companyText: company.text,
    statusCount: status.count,
    statusText: status.text,
    readOnly: has(T.portalHomeReadOnly),
    adminMenu,
  };
}

/**
 * 把「Company name」这一行（<td> 所在的 <tr>）滚动到视口中间。只滚动，不点任何东西。
 * ⚠️ 这些函数各自 toString() 后送进页面，彼此不能互相调用，所以找行的那几句在下面两个函数里各写一遍。
 */
function scrollPortalHomeIntoView(lib, T) {
  const cells = lib.valueCells(T.portalHomeCompanyName);
  const row = cells.length === 1 ? cells[0].closest("tr") : null;
  if (row === null) {
    return false;
  }
  row.scrollIntoView({ block: "center", inline: "nearest" });
  return true;
}

function portalHomeInView(lib, T) {
  const cells = lib.valueCells(T.portalHomeCompanyName);
  const row = cells.length === 1 ? cells[0].closest("tr") : null;
  if (row === null) {
    return false;
  }
  const rect = row.getBoundingClientRect();
  return rect.height > 0 && rect.top >= 0 && rect.bottom <= window.innerHeight;
}

/**
 * 门户钱包页的状态：门户外壳、管理端顶栏在不在；余额卡（role=region、aria-label 为「Your wallet」）在加载、加载失败、
 * 「Balance」一项的文字（带边框的 Descriptions：label 在 <th>，值在紧跟的 <td>；不是恰好一项时为 null）；流水一块
 * （role=region、aria-label 为「Transactions」）在加载、加载失败、空状态，以及表格（表头有「Balance after」「Event ID」）
 * 里带 data-row-key 的数据行数与第一行「Event ID」列的文字。只读 DOM，不点、不填。
 */
function probePortalWallet(lib, T) {
  const header = document.querySelector("header");
  const regionOf = (label) =>
    [...document.querySelectorAll('[role="region"]')].find((el) => el.getAttribute("aria-label") === label) ?? null;
  const has = (root, text) =>
    [...root.querySelectorAll("*")].some((el) => el.childElementCount === 0 && lib.norm(el.textContent) === text);
  const card = regionOf(T.portalWalletTitle);
  const transactions = regionOf(T.portalTxTitle);
  const out = {
    ...lib.common(T),
    portalShell:
      header !== null &&
      lib.button(T.navSignOut, header) !== null &&
      [...header.querySelectorAll("*")].some(
        (el) => el.childElementCount === 0 && lib.norm(el.textContent) === T.portalShellTitle,
      ),
    adminLayout: lib.layout(T),
    card: card !== null,
    cardBusy: false,
    cardFailed: false,
    balanceCount: 0,
    balanceText: null,
    transactions: transactions !== null,
    txBusy: false,
    txFailed: false,
    txEmpty: false,
    table: false,
    rows: 0,
    firstEventId: null,
  };
  if (card !== null) {
    out.cardBusy = card.querySelector('[aria-busy="true"]') !== null || has(card, T.portalWalletLoading);
    out.cardFailed = has(card, T.portalWalletLoadFailed);
    const cells = [];
    for (const th of card.querySelectorAll("th")) {
      const td = th.nextElementSibling;
      if (lib.norm(th.textContent) === T.portalWalletBalance && td && td.tagName === "TD") {
        cells.push(td);
      }
    }
    out.balanceCount = cells.length;
    out.balanceText = cells.length === 1 ? lib.norm(cells[0].textContent) : null;
  }
  if (transactions !== null) {
    out.txBusy = transactions.querySelector('[aria-busy="true"]') !== null || has(transactions, T.portalTxLoading);
    out.txFailed = has(transactions, T.portalTxLoadFailed);
    out.txEmpty = has(transactions, T.portalTxEmpty);
    for (const table of transactions.querySelectorAll("table")) {
      const heads = [...table.querySelectorAll(":scope > thead > tr > th")].map((th) => lib.norm(th.textContent));
      if (!heads.includes(T.portalTxBalanceAfter) || !heads.includes(T.portalTxEventId)) {
        continue;
      }
      const eventId = heads.indexOf(T.portalTxEventId);
      out.table = true;
      for (const tr of table.querySelectorAll(":scope > tbody > tr[data-row-key]")) {
        if (tr.getAttribute("aria-hidden") === "true") {
          continue;
        }
        const cells = tr.querySelectorAll(":scope > td");
        if (cells.length !== heads.length) {
          continue;
        }
        if (out.rows === 0) {
          out.firstEventId = lib.norm(cells[eventId].textContent);
        }
        out.rows += 1;
      }
      break;
    }
  }
  return out;
}

/** 门户导航里文字为「Wallet」、href 恰好为 /portal/wallet 的链接。只点这一个。 */
function clickPortalNavWallet(lib, T) {
  const header = document.querySelector("header");
  if (!header) {
    return false;
  }
  for (const a of header.querySelectorAll("a")) {
    if (lib.norm(a.textContent) === T.portalNavWallet && a.getAttribute("href") === T.portalWalletPath) {
      a.click();
      return true;
    }
  }
  return false;
}

/** 把余额卡滚进视口：放得下时居中，放不下时顶端对齐视口顶端。只滚动，不点任何东西。 */
function scrollPortalWalletIntoView(lib, T) {
  const card = [...document.querySelectorAll('[role="region"]')].find(
    (el) => el.getAttribute("aria-label") === T.portalWalletTitle,
  );
  if (card === undefined) {
    return false;
  }
  const block = card.getBoundingClientRect().height <= window.innerHeight ? "center" : "start";
  card.scrollIntoView({ block, inline: "nearest" });
  return true;
}

/** 放得下视口的余额卡要整块在视口内；放不下的只要求顶端在视口内。 */
function portalWalletInView(lib, T) {
  const card = [...document.querySelectorAll('[role="region"]')].find(
    (el) => el.getAttribute("aria-label") === T.portalWalletTitle,
  );
  if (card === undefined) {
    return false;
  }
  const rect = card.getBoundingClientRect();
  const topVisible = rect.top >= 0 && rect.top < window.innerHeight;
  return rect.height > 0 && topVisible && (rect.bottom <= window.innerHeight || rect.height > window.innerHeight);
}

/**
 * 门户用量页的状态：门户外壳、管理端顶栏在不在；用量一块（role=region、aria-label 为「Usage」）在加载、加载失败、
 * 查询过宽、空状态，以及表格（表头有「Time (MYT)」「Charged (MYR)」）里带 data-row-key 的数据行数与第一行
 * 「Charged (MYR)」列的文字。展开出来的详情行不带 data-row-key、单元格数也对不上表头，不计入。只读 DOM，不点、不填。
 */
function probePortalUsage(lib, T) {
  const header = document.querySelector("header");
  const has = (root, text) =>
    [...root.querySelectorAll("*")].some((el) => el.childElementCount === 0 && lib.norm(el.textContent) === text);
  const region =
    [...document.querySelectorAll('[role="region"]')].find((el) => el.getAttribute("aria-label") === T.portalUsageTitle) ??
    null;
  const out = {
    ...lib.common(T),
    portalShell:
      header !== null &&
      lib.button(T.navSignOut, header) !== null &&
      [...header.querySelectorAll("*")].some(
        (el) => el.childElementCount === 0 && lib.norm(el.textContent) === T.portalShellTitle,
      ),
    adminLayout: lib.layout(T),
    region: region !== null,
    busy: false,
    failed: false,
    tooBroad: false,
    empty: false,
    table: false,
    rows: 0,
    firstCharged: null,
  };
  if (region === null) {
    return out;
  }
  out.busy = region.querySelector('[aria-busy="true"]') !== null || has(region, T.portalUsageLoading);
  out.failed = has(region, T.portalUsageLoadFailed);
  out.tooBroad = has(region, T.portalUsageTooBroad);
  out.empty = has(region, T.portalUsageEmpty);
  for (const table of region.querySelectorAll("table")) {
    const heads = [...table.querySelectorAll(":scope > thead > tr > th")].map((th) => lib.norm(th.textContent));
    if (!heads.includes(T.portalUsageTime) || !heads.includes(T.portalUsageCharged)) {
      continue;
    }
    const charged = heads.indexOf(T.portalUsageCharged);
    out.table = true;
    for (const tr of table.querySelectorAll(":scope > tbody > tr[data-row-key]")) {
      if (tr.getAttribute("aria-hidden") === "true") {
        continue;
      }
      const cells = tr.querySelectorAll(":scope > td");
      if (cells.length !== heads.length) {
        continue;
      }
      if (out.rows === 0) {
        out.firstCharged = lib.norm(cells[charged].textContent);
      }
      out.rows += 1;
    }
    break;
  }
  return out;
}

/** 门户导航里文字为「Usage」、href 恰好为 /portal/usage 的链接。只点这一个。 */
function clickPortalNavUsage(lib, T) {
  const header = document.querySelector("header");
  if (!header) {
    return false;
  }
  for (const a of header.querySelectorAll("a")) {
    if (lib.norm(a.textContent) === T.portalNavUsage && a.getAttribute("href") === T.portalUsagePath) {
      a.click();
      return true;
    }
  }
  return false;
}

/** 把用量一块滚进视口：放得下时居中，放不下时顶端对齐视口顶端。只滚动，不点任何东西。 */
function scrollPortalUsageIntoView(lib, T) {
  const region = [...document.querySelectorAll('[role="region"]')].find(
    (el) => el.getAttribute("aria-label") === T.portalUsageTitle,
  );
  if (region === undefined) {
    return false;
  }
  const block = region.getBoundingClientRect().height <= window.innerHeight ? "center" : "start";
  region.scrollIntoView({ block, inline: "nearest" });
  return true;
}

/** 放得下视口的用量一块要整块在视口内；放不下的只要求顶端在视口内。 */
function portalUsageInView(lib, T) {
  const region = [...document.querySelectorAll('[role="region"]')].find(
    (el) => el.getAttribute("aria-label") === T.portalUsageTitle,
  );
  if (region === undefined) {
    return false;
  }
  const rect = region.getBoundingClientRect();
  const topVisible = rect.top >= 0 && rect.top < window.innerHeight;
  return rect.height > 0 && topVisible && (rect.bottom <= window.innerHeight || rect.height > window.innerHeight);
}

/**
 * 门户首页本月用量卡的状态：门户外壳、管理端顶栏在不在；卡（role=region、aria-label 为「This month's usage」）在加载、
 * 加载失败、查询过宽；「Charged (MYR)」「Requests」「Conversations」三项各有几项、恰好一项时的文字（带边框的
 * Descriptions：label 在 <th>，值在紧跟的 <td>）；以月份标题后缀结尾的叶子文字（月份标题与柱状图说明都以它结尾，
 * 由调用方逐字比较）；柱状图（role=list、aria-label 为图的说明）在不在、里面 role=listitem 的柱子个数。
 * 只读 DOM，不点、不填。
 */
function probePortalUsageSummary(lib, T) {
  const header = document.querySelector("header");
  const has = (root, text) =>
    [...root.querySelectorAll("*")].some((el) => el.childElementCount === 0 && lib.norm(el.textContent) === text);
  const card =
    [...document.querySelectorAll('[role="region"]')].find(
      (el) => el.getAttribute("aria-label") === T.portalUsageSummaryTitle,
    ) ?? null;
  const out = {
    ...lib.common(T),
    portalShell:
      header !== null &&
      lib.button(T.navSignOut, header) !== null &&
      [...header.querySelectorAll("*")].some(
        (el) => el.childElementCount === 0 && lib.norm(el.textContent) === T.portalShellTitle,
      ),
    adminLayout: lib.layout(T),
    card: card !== null,
    busy: false,
    failed: false,
    tooBroad: false,
    monthTexts: [],
    chargedCount: 0,
    charged: null,
    requestsCount: 0,
    requests: null,
    conversationsCount: 0,
    conversations: null,
    chart: false,
    bars: 0,
  };
  if (card === null) {
    return out;
  }
  out.busy = card.querySelector('[aria-busy="true"]') !== null || has(card, T.portalUsageSummaryLoading);
  out.failed = has(card, T.portalUsageSummaryLoadFailed);
  out.tooBroad = has(card, T.portalUsageSummaryTooBroad);
  const single = (label) => {
    const cells = [];
    for (const th of card.querySelectorAll("th")) {
      const td = th.nextElementSibling;
      if (lib.norm(th.textContent) === label && td && td.tagName === "TD") {
        cells.push(td);
      }
    }
    return { count: cells.length, text: cells.length === 1 ? lib.norm(cells[0].textContent) : null };
  };
  const charged = single(T.portalUsageSummaryCharged);
  const requests = single(T.portalUsageSummaryRequests);
  const conversations = single(T.portalUsageSummaryConversations);
  out.chargedCount = charged.count;
  out.charged = charged.text;
  out.requestsCount = requests.count;
  out.requests = requests.text;
  out.conversationsCount = conversations.count;
  out.conversations = conversations.text;
  for (const el of card.querySelectorAll("*")) {
    const text = lib.norm(el.textContent);
    if (el.childElementCount === 0 && text.endsWith(T.portalUsageSummaryMonthSuffix)) {
      out.monthTexts.push(text);
    }
  }
  const chart =
    [...card.querySelectorAll('[role="list"]')].find(
      (el) => el.getAttribute("aria-label") === T.portalUsageSummaryChart,
    ) ?? null;
  if (chart !== null) {
    out.chart = true;
    out.bars = chart.querySelectorAll(':scope > [role="listitem"]').length;
  }
  return out;
}

// ---------------------------------------------------------------------------
// 驱动页面
// ---------------------------------------------------------------------------

let sessionId = "";
let deadline = 0;

/** 在页面里执行一个上面的函数。执行不了（页面在跳转、抛异常）就回 null，由调用方继续等。 */
async function evaluate(fn, arg) {
  if (cdp.closed) {
    throw fail("page_error");
  }
  const expression = `(${fn.toString()})((${pageLib.toString()})(), ${JSON.stringify(arg ?? null)})`;
  try {
    const result = await cdp.send("Runtime.evaluate", { expression, returnByValue: true }, sessionId);
    if (result.exceptionDetails !== undefined) {
      return null;
    }
    return result.result?.value ?? null;
  } catch {
    return null;
  }
}

/** 轮询 probe 直到 done 成立或到时（受总预算约束）。回 { ok, value }，value 是最后一次探到的值。 */
async function waitFor(probe, done, limitMs) {
  const until = Math.min(Date.now() + limitMs, deadline);
  let value = null;
  for (;;) {
    value = await probe();
    if (value !== null && done(value)) {
      return { ok: true, value };
    }
    if (Date.now() >= until) {
      return { ok: false, value };
    }
    await sleep(POLL_MS);
  }
}

/**
 * 聚焦 label 对应的输入框并用 Input.insertText 填入。text 只进这一个 CDP 命令，不进页面脚本。
 * value 可以是一个返回文字的函数（TOTP）：聚焦之后才算，码不会在等元素时过期。
 * 填不进去时报 failCode（登录表单是 login_failed）。
 */
async function typeInto(labelText, value, failCode = "login_failed") {
  const focused = await waitFor(() => evaluate(focusField, labelText), (v) => v === true, ELEMENT_WAIT_MS);
  if (!focused.ok) {
    throw fail("element_missing");
  }
  const text = typeof value === "function" ? await value() : value;
  try {
    await cdp.send("Input.insertText", { text }, sessionId);
  } catch {
    throw fail(failCode);
  }
  if ((await evaluate(fieldLength, labelText)) !== text.length) {
    throw fail(failCode);
  }
}

async function attachToBlankPage(cdpUrl) {
  cdp = await CdpConnection.open(cdpUrl);
  let targets;
  try {
    ({ targetInfos: targets } = await cdp.send("Target.getTargets"));
  } catch {
    throw fail("navigation_failed");
  }
  const blank = (Array.isArray(targets) ? targets : []).filter(
    (t) => t.type === "page" && t.url === "about:blank",
  );
  if (blank.length !== 1) {
    throw fail("navigation_failed");
  }
  try {
    ({ sessionId } = await cdp.send("Target.attachToTarget", { targetId: blank[0].targetId, flatten: true }));
  } catch {
    throw fail("navigation_failed");
  }
  if (typeof sessionId !== "string" || sessionId === "") {
    throw fail("navigation_failed");
  }
  observeNetwork(sessionId);
  try {
    await cdp.send("Network.enable", {}, sessionId);
  } catch {
    throw fail("navigation_failed");
  }
}

/** 打开 https://<hosts[0]>/，按主文档有没有 HTTP 响应、状态码几何来判。 */
async function openEntry(host) {
  await openPath(host, "/");
}

/** 打开 https://<host><pathAndQuery>，判法同 openEntry。地址（含查询串）不打印。 */
async function openPath(host, pathAndQuery) {
  let navigation = null;
  try {
    navigation = await cdp.send(
      "Page.navigate",
      { url: `https://${host}${pathAndQuery}` },
      sessionId,
      NAVIGATION_TIMEOUT_MS,
    );
  } catch {
    navigation = null;
  }
  const loaderId = typeof navigation?.loaderId === "string" ? navigation.loaderId : null;
  let status = network.lastDocumentStatus;
  if (loaderId !== null) {
    const seen = await waitFor(
      async () => (network.documents.has(loaderId) ? network.documents.get(loaderId) : null),
      () => true,
      DOCUMENT_EVENT_WAIT_MS,
    );
    status = seen.ok ? seen.value : null;
  }
  if (status === null) {
    // 主文档完全没有得到 HTTP 响应（DNS、连接、TLS、代理隧道失败，或导航根本没发出去）。
    throw fail("host_unreachable");
  }
  if (status === 502 || status === 503 || status === 504) {
    throw fail("service_unavailable");
  }
  if (status >= 400) {
    throw fail("page_error");
  }
  if (navigation === null || (typeof navigation.errorText === "string" && navigation.errorText !== "")) {
    throw fail("navigation_failed");
  }
}

// ---------------------------------------------------------------------------
// 步骤
// ---------------------------------------------------------------------------

async function stepCheckAcceptInvitationPage(ctx) {
  // 匿名只靠步骤顺序保证（不新建浏览器上下文）：登录过之后再跑，测的就不是邀请链接的场景了。
  if (ctx.loginCompletedAt !== null || ctx.customerLoginSeq !== null) {
    throw fail("assertion_failed");
  }
  await openPath(
    ctx.host,
    `${TEXT.acceptInvitationPath}?${ACCEPT_INVITATION_TOKEN_PARAM}=${PLACEHOLDER_INVITATION_TOKEN}`,
  );

  // 路由若落进 RequireAuth，页面先转圈、再被送去 /login —— 地址一变就停下来判。
  const shown = await waitFor(
    () => evaluate(probeAcceptInvitation, TEXT),
    (s) => s.appError || s.notFound || s.path !== TEXT.acceptInvitationPath || s.form,
    ELEMENT_WAIT_MS,
  );
  if (!shown.ok) {
    throw fail("element_missing");
  }
  const page = shown.value;
  if (page.appError) {
    throw fail("page_error");
  }
  if (page.path !== TEXT.acceptInvitationPath || page.loginForm) {
    throw fail("assertion_failed");
  }
  if (page.notFound) {
    throw fail("navigation_failed");
  }

  // 表单出现之后再看一小段时间：这段时间里地址变了或表单没了都算失败。
  const changed = await waitFor(
    () => evaluate(probeAcceptInvitation, TEXT),
    (s) => s.path !== TEXT.acceptInvitationPath || !s.form,
    ACCEPT_INVITATION_SETTLE_MS,
  );
  if (changed.ok) {
    throw fail("assertion_failed");
  }

  // 交还页面：重新打开首页，等 RequireAuth 把匿名访问送回 /login。随后的 login 步骤不自己导航。
  await openEntry(ctx.host);
  const back = await waitFor(
    () => evaluate(probeLogin, TEXT),
    (s) => s.appError || s.notFound || s.layout || (s.path === TEXT.loginPath && s.form),
    ELEMENT_WAIT_MS,
  );
  if (!back.ok) {
    throw fail("element_missing");
  }
  if (back.value.appError) {
    throw fail("page_error");
  }
  if (back.value.notFound || back.value.layout) {
    throw fail("assertion_failed");
  }
}

async function stepLogin(ctx) {
  // 首页未登录时由 RequireAuth 送到 /login。
  const form = await waitFor(
    () => evaluate(probeLogin, TEXT),
    (s) => s.form || s.layout || s.appError || s.notFound,
    ELEMENT_WAIT_MS,
  );
  if (form.ok && form.value.appError) {
    throw fail("page_error");
  }
  if (!form.ok || !form.value.form) {
    throw fail("element_missing");
  }

  await typeInto(TEXT.loginEmail, ctx.email);
  await typeInto(TEXT.loginPassword, ctx.password);
  if ((await evaluate(clickButton, TEXT.loginSubmit)) !== true) {
    throw fail("element_missing");
  }

  const afterPassword = await waitFor(
    () => evaluate(probeLogin, TEXT),
    (s) => s.layout || s.code || s.enrol || s.alert || s.appError,
    LOGIN_WAIT_MS,
  );
  if (!afterPassword.ok) {
    throw fail("login_failed");
  }
  const stage = afterPassword.value;
  if (stage.appError) {
    throw fail("page_error");
  }
  // 走到「首次启用 2FA」会写数据（绑定密钥、发恢复码），只读验收不能往下走。
  if (stage.alert || stage.enrol) {
    throw fail("login_failed");
  }

  if (!stage.layout) {
    await typeInto(TEXT.loginCode, () => freshTotp(ctx.totpKey));
    if ((await evaluate(clickButton, TEXT.loginVerify)) !== true) {
      throw fail("element_missing");
    }
  }

  const signedIn = await waitFor(
    () => evaluate(probeLogin, TEXT),
    (s) => s.layout || s.alert || s.appError,
    LOGIN_WAIT_MS,
  );
  if (signedIn.ok && signedIn.value.appError) {
    throw fail("page_error");
  }
  if (!signedIn.ok || !signedIn.value.layout) {
    throw fail("login_failed");
  }
  // check_audit_entry 用它判断「本次登录留下的那条审计」。
  ctx.loginCompletedAt = Date.now();
}

async function stepOpenCustomers(ctx) {
  const clicked = await waitFor(() => evaluate(clickNavCustomers, TEXT), (v) => v === true, ELEMENT_WAIT_MS);
  if (!clicked.ok) {
    throw fail("element_missing");
  }

  const arg = { T: TEXT, prefix: FIXTURE_PREFIX };
  let expectedPage = null;
  let previousSignature = null;
  for (let pages = 1; ; pages++) {
    const listed = await waitFor(
      () => evaluate(probeList, arg),
      (s) =>
        s.appError ||
        s.notFound ||
        (s.path === TEXT.customersPath &&
          !s.busy &&
          (s.failed ||
            s.empty ||
            (s.table && (expectedPage === null || (s.page === expectedPage && s.signature !== previousSignature))))),
      ELEMENT_WAIT_MS,
    );
    if (!listed.ok) {
      throw fail("element_missing");
    }
    const list = listed.value;
    if (list.appError || list.failed) {
      throw fail("page_error");
    }
    if (list.notFound) {
      throw fail("navigation_failed");
    }
    if (list.empty) {
      throw fail("assertion_failed");
    }

    const fixture = list.matches.find((m) => {
      const match = CUSTOMER_DETAIL_HREF.exec(m.href);
      return match !== null && m.href !== TEXT.customerCreatePath;
    });
    if (fixture !== undefined) {
      ctx.fixture = fixture;
      return;
    }

    if (!list.hasNext || pages >= MAX_LIST_PAGES) {
      throw fail("assertion_failed");
    }
    previousSignature = list.signature;
    expectedPage = list.page + 1;
    if ((await evaluate(clickNextPage, TEXT)) !== true) {
      throw fail("element_missing");
    }
  }
}

async function stepOpenFixture(ctx) {
  if (ctx.fixture === null) {
    throw fail("assertion_failed");
  }
  const { name, href } = ctx.fixture;
  if ((await evaluate(clickFixture, { name, href })) !== true) {
    throw fail("element_missing");
  }

  const opened = await waitFor(
    () => evaluate(probeDetail, TEXT),
    (s) =>
      s.appError ||
      s.notFound ||
      (s.path === href && !s.loading && (s.failed || s.missing || s.names.length > 0)),
    ELEMENT_WAIT_MS,
  );
  if (!opened.ok) {
    throw fail("element_missing");
  }
  const detail = opened.value;
  if (detail.appError || detail.failed) {
    throw fail("page_error");
  }
  if (detail.notFound) {
    throw fail("navigation_failed");
  }
  if (detail.missing || detail.names.length !== 1 || detail.names[0] !== name) {
    throw fail("assertion_failed");
  }
  try {
    ctx.customerId = decodeURIComponent(CUSTOMER_DETAIL_HREF.exec(href)[1]);
  } catch {
    throw fail("assertion_failed");
  }
}

async function stepCheckBalance(ctx) {
  if (ctx.customerId === null || ctx.fixture === null) {
    throw fail("assertion_failed");
  }
  const customerId = ctx.customerId;

  // 后端值：详情页自己发出的那次 GET 的响应体。
  const observed = await waitFor(
    async () => latestDetailResponse(customerId),
    () => true,
    RESPONSE_WAIT_MS,
  );
  if (!observed.ok) {
    throw fail("assertion_failed");
  }
  let expected;
  try {
    const { body, base64Encoded } = await cdp.send(
      "Network.getResponseBody",
      { requestId: observed.value.requestId },
      sessionId,
    );
    const envelope = JSON.parse(base64Encoded ? Buffer.from(body, "base64").toString("utf8") : body);
    const data = envelope?.success === true ? envelope.data : null;
    const wallet = data?.wallet;
    if (data?.id !== customerId || typeof wallet?.balance !== "string" || typeof wallet?.currency !== "string") {
      throw fail("assertion_failed");
    }
    expected = formatMoney(wallet.balance, wallet.currency);
  } catch {
    // 读不到、不是 JSON、不是成功信封、不是这个客户：都算没观察到可比的后端值。
    throw fail("assertion_failed");
  }

  const shown = await waitFor(
    () => evaluate(readBalance, TEXT),
    (s) => s.path === ctx.fixture.href && s.count === 1 && s.text === expected,
    ELEMENT_WAIT_MS,
  );
  if (!shown.ok) {
    throw fail(shown.value !== null && shown.value.count === 1 ? "assertion_failed" : "element_missing");
  }

  // Worker 在报到后截图，余额在首屏以下：先滚进视口。
  if ((await evaluate(scrollBalanceIntoView, TEXT)) !== true) {
    throw fail("element_missing");
  }
  const visible = await waitFor(() => evaluate(balanceInView, TEXT), (v) => v === true, SCROLL_WAIT_MS);
  if (!visible.ok) {
    throw fail("assertion_failed");
  }
}

async function stepCheckAccountStatus(ctx) {
  if (ctx.customerId === null || ctx.fixture === null) {
    throw fail("assertion_failed");
  }
  const customerId = ctx.customerId;
  const href = ctx.fixture.href;

  // 后端值：详情页自己发出的那次 GET 的响应体（与 check_balance 同一个来源）。
  const observed = await waitFor(
    async () => latestDetailResponse(customerId),
    () => true,
    RESPONSE_WAIT_MS,
  );
  if (!observed.ok) {
    throw fail("assertion_failed");
  }
  let status;
  try {
    const { body, base64Encoded } = await cdp.send(
      "Network.getResponseBody",
      { requestId: observed.value.requestId },
      sessionId,
    );
    const envelope = JSON.parse(base64Encoded ? Buffer.from(body, "base64").toString("utf8") : body);
    const data = envelope?.success === true ? envelope.data : null;
    status = data?.account_status;
    if (data?.id !== customerId || typeof status !== "string" || !Object.hasOwn(TEXT.accountStatusText, status)) {
      throw fail("assertion_failed");
    }
  } catch {
    // 读不到、不是 JSON、不是成功信封、不是这个客户、状态不认识：都算没观察到可比的后端值。
    throw fail("assertion_failed");
  }
  const expectedText = TEXT.accountStatusText[status];
  // ENABLED 对应停用、DISABLED 对应启用；另两个状态两个按钮都不该在。
  const expectedDisable = status === "ENABLED" ? 1 : 0;
  const expectedEnable = status === "DISABLED" ? 1 : 0;

  const shown = await waitFor(
    () => evaluate(readAccountStatus, TEXT),
    (s) =>
      s.appError ||
      s.notFound ||
      (s.path === href &&
        s.count === 1 &&
        s.text === expectedText &&
        s.disableButtons === expectedDisable &&
        s.enableButtons === expectedEnable),
    ELEMENT_WAIT_MS,
  );
  if (!shown.ok) {
    const s = shown.value;
    if (s === null || s.path !== href || s.count !== 1) {
      throw fail("element_missing");
    }
    // 状态文字不对，或者该在的按钮不在、不该在的按钮在了。
    throw fail(s.text === expectedText && s.disableButtons + s.enableButtons < expectedDisable + expectedEnable
      ? "element_missing"
      : "assertion_failed");
  }
  if (shown.value.appError) {
    throw fail("page_error");
  }
  if (shown.value.notFound) {
    throw fail("navigation_failed");
  }

  // Worker 在报到后截图：先把「Account status」这一行滚进视口。只滚动，不点按钮。
  if ((await evaluate(scrollAccountStatusIntoView, TEXT)) !== true) {
    throw fail("element_missing");
  }
  const visible = await waitFor(() => evaluate(accountStatusInView, TEXT), (v) => v === true, SCROLL_WAIT_MS);
  if (!visible.ok) {
    throw fail("assertion_failed");
  }
}

/** 审计页落定：不在加载，且是某一种终态（表格、失败、无权限、筛选被拒、空列表）。 */
function auditSettled(s) {
  return (
    s.appError ||
    s.notFound ||
    (s.path === TEXT.auditPath &&
      !s.busy &&
      (s.failed || s.forbidden || s.invalid || s.empty || (s.table && (s.rows.length > 0 || s.emptyPage))))
  );
}

/** 审计页落在了不该落的状态上：各自的失败码。 */
function rejectAuditState(s) {
  if (s.appError || s.failed || s.invalid) {
    throw fail("page_error");
  }
  if (s.notFound || s.path !== TEXT.auditPath) {
    throw fail("navigation_failed");
  }
  if (s.forbidden || s.empty || !s.table || s.rows.length === 0) {
    throw fail("assertion_failed");
  }
}

async function stepOpenAudit() {
  const clicked = await waitFor(() => evaluate(clickNavAudit, TEXT), (v) => v === true, ELEMENT_WAIT_MS);
  if (!clicked.ok) {
    throw fail("element_missing");
  }
  const opened = await waitFor(() => evaluate(probeAudit, TEXT), auditSettled, ELEMENT_WAIT_MS);
  if (!opened.ok) {
    throw fail("element_missing");
  }
  rejectAuditState(opened.value);
}

/** 不带时区的 UTC → 毫秒；形状不对回 null。 */
function naiveUtcMs(value) {
  if (typeof value !== "string" || !NAIVE_UTC.test(value)) {
    return null;
  }
  const ms = Date.parse(`${value}Z`);
  return Number.isNaN(ms) ? null : ms;
}

/** 登录邮箱的比较：后端存小写（app/services/audit_query.py），stdin 里的可能带大写或空白。只在本进程内比，不打印。 */
function sameEmail(a, b) {
  return typeof a === "string" && typeof b === "string" && a.trim().toLowerCase() === b.trim().toLowerCase();
}

async function stepCheckAuditEntry(ctx) {
  if (ctx.loginCompletedAt === null) {
    throw fail("assertion_failed");
  }
  const ready = await waitFor(() => evaluate(probeAudit, TEXT), auditSettled, ELEMENT_WAIT_MS);
  if (!ready.ok) {
    throw fail("element_missing");
  }
  rejectAuditState(ready.value);

  // 点「Apply filters」之前的序号：只认在这之后页面自己重新发出的请求。
  const beforeApply = network.seq;
  await typeInto(TEXT.auditFilterAction, LOGIN_ACTION, "element_missing");
  const picked = await waitFor(() => evaluate(clickAuditOption, LOGIN_ACTION), (v) => v === true, ELEMENT_WAIT_MS);
  if (!picked.ok) {
    throw fail("element_missing");
  }
  const applied = await waitFor(() => evaluate(clickButton, TEXT.auditApply), (v) => v === true, ELEMENT_WAIT_MS);
  if (!applied.ok) {
    throw fail("element_missing");
  }

  // 后端值：页面自己发出的第 1 页 action=LOGIN 查询的响应体。有还在路上的就等它落定。
  const observed = await waitFor(
    async () => (loginAuditInFlight(beforeApply) ? null : latestLoginAuditResponse(beforeApply)),
    (v) => v !== null,
    RESPONSE_WAIT_MS,
  );
  if (!observed.ok) {
    throw fail("assertion_failed");
  }
  let items;
  let index;
  try {
    const { body, base64Encoded } = await cdp.send(
      "Network.getResponseBody",
      { requestId: observed.value.requestId },
      sessionId,
    );
    const envelope = JSON.parse(base64Encoded ? Buffer.from(body, "base64").toString("utf8") : body);
    const data = envelope?.success === true ? envelope.data : null;
    if (data?.page !== 1 || !Array.isArray(data?.items)) {
      throw fail("assertion_failed");
    }
    items = data.items;
    index = items.findIndex((item) => sameEmail(item?.actor_email, ctx.email));
    const record = index < 0 ? null : items[index];
    const createdMs = naiveUtcMs(record?.created_at);
    if (
      record === null ||
      record.action !== LOGIN_ACTION ||
      record.actor_role !== LOGIN_ACTOR_ROLE ||
      record.entity_type !== LOGIN_ENTITY_TYPE ||
      createdMs === null ||
      Math.abs(createdMs - ctx.loginCompletedAt) > LOGIN_AUDIT_MAX_SKEW_MS
    ) {
      throw fail("assertion_failed");
    }
  } catch {
    // 读不到、不是 JSON、不是成功信封、不是第 1 页、找不到本次登录的那条：都算没观察到可比的后端值。
    throw fail("assertion_failed");
  }
  const record = items[index];

  // 表格显示的就是这一份响应：行数相同、动作列全是 LOGIN、那一行的动作与操作者和记录一致。
  const shown = await waitFor(
    () => evaluate(probeAudit, TEXT),
    (s) =>
      s.path === TEXT.auditPath &&
      !s.busy &&
      s.rows.length === items.length &&
      s.rows.every((row) => row.action === LOGIN_ACTION) &&
      s.rows[index].action === record.action &&
      s.rows[index].actor === record.actor_email,
    ELEMENT_WAIT_MS,
  );
  if (!shown.ok) {
    throw fail(shown.value !== null && shown.value.table ? "assertion_failed" : "element_missing");
  }

  // Worker 在报到后截图：先把那一行滚进视口。
  if ((await evaluate(scrollAuditRowIntoView, { T: TEXT, index })) !== true) {
    throw fail("element_missing");
  }
  const visible = await waitFor(
    () => evaluate(auditRowInView, { T: TEXT, index }),
    (v) => v === true,
    SCROLL_WAIT_MS,
  );
  if (!visible.ok) {
    throw fail("assertion_failed");
  }
}

/** 目录页落定：不在加载，且是某一种终态（表格、错误提示、无权限、空列表），或者走到了别处。 */
function catalogSettled(path) {
  return (s) =>
    s.appError ||
    s.notFound ||
    (s.path === path && !s.busy && (s.failed || s.forbidden || s.empty || s.table));
}

/** 目录页落在了不该落的状态上：各自的失败码。emptyAllowed 为真时空列表合法。 */
function rejectCatalogState(s, path, emptyAllowed) {
  if (s.appError || s.failed) {
    throw fail("page_error");
  }
  if (s.notFound || s.path !== path) {
    throw fail("navigation_failed");
  }
  if (s.forbidden || (s.empty && !emptyAllowed) || (!s.empty && !s.table)) {
    throw fail("assertion_failed");
  }
}

async function stepOpenCatalog() {
  const clicked = await waitFor(() => evaluate(clickNavCatalog, TEXT), (v) => v === true, ELEMENT_WAIT_MS);
  if (!clicked.ok) {
    throw fail("element_missing");
  }
  const opened = await waitFor(
    () => evaluate(probeProviders, TEXT),
    catalogSettled(TEXT.catalogProvidersPath),
    ELEMENT_WAIT_MS,
  );
  if (!opened.ok) {
    throw fail("element_missing");
  }
  // 生产上不预置任何供应商（设计 §8），空列表合法。
  rejectCatalogState(opened.value, TEXT.catalogProvidersPath, true);
}

async function stepCheckMeterTypes() {
  // 点链接之前的序号：只认在这之后页面自己发出的请求。
  const beforeClick = network.seq;
  const clicked = await waitFor(() => evaluate(clickMeterTypesLink, TEXT), (v) => v === true, ELEMENT_WAIT_MS);
  if (!clicked.ok) {
    throw fail("element_missing");
  }
  const opened = await waitFor(
    () => evaluate(probeMeterTypes, TEXT),
    catalogSettled(TEXT.catalogMeterTypesPath),
    ELEMENT_WAIT_MS,
  );
  if (!opened.ok) {
    throw fail("element_missing");
  }
  rejectCatalogState(opened.value, TEXT.catalogMeterTypesPath, false);

  // 后端值：页面自己发出的那次列表 GET 的响应体。有还在路上的就等它落定。
  const observed = await waitFor(
    async () => latestMeterTypesResponse(beforeClick),
    (v) => v !== null,
    RESPONSE_WAIT_MS,
  );
  if (!observed.ok) {
    throw fail("assertion_failed");
  }
  let expected;
  try {
    const { body, base64Encoded } = await cdp.send(
      "Network.getResponseBody",
      { requestId: observed.value.requestId },
      sessionId,
    );
    const envelope = JSON.parse(base64Encoded ? Buffer.from(body, "base64").toString("utf8") : body);
    const items = envelope?.success === true ? envelope.data?.items : null;
    if (!Array.isArray(items)) {
      throw fail("assertion_failed");
    }
    expected = items.map((item) => ({
      code: typeof item?.code === "string" ? item.code : null,
      components: Array.isArray(item?.components) ? item.components.length : -1,
    }));
    const codes = new Set(expected.map((item) => item.code));
    const llm = expected.find((item) => item.code === LLM_TOKEN_CODE);
    if (
      expected.some((item) => item.code === null || item.components < 0) ||
      !SEED_METER_TYPES.every((code) => codes.has(code)) ||
      llm === undefined ||
      llm.components !== LLM_TOKEN_COMPONENTS
    ) {
      throw fail("assertion_failed");
    }
  } catch {
    // 读不到、不是 JSON、不是成功信封、缺种子、LLM_TOKEN 分量数不对：都算没观察到可比的后端值。
    throw fail("assertion_failed");
  }

  // 表格显示的就是这一份响应：行数、顺序、代码、每行的分量个数都一致。
  const shown = await waitFor(
    () => evaluate(probeMeterTypes, TEXT),
    (s) =>
      s.path === TEXT.catalogMeterTypesPath &&
      !s.busy &&
      s.rows.length === expected.length &&
      s.rows.every((row, i) => row.code === expected[i].code && row.components === expected[i].components),
    ELEMENT_WAIT_MS,
  );
  if (!shown.ok) {
    throw fail(shown.value !== null && shown.value.table ? "assertion_failed" : "element_missing");
  }

  // Worker 在报到后截图：先把 LLM_TOKEN 那一行滚进视口。
  const arg = { T: TEXT, code: LLM_TOKEN_CODE };
  if ((await evaluate(scrollMeterTypeIntoView, arg)) !== true) {
    throw fail("element_missing");
  }
  const visible = await waitFor(() => evaluate(meterTypeInView, arg), (v) => v === true, SCROLL_WAIT_MS);
  if (!visible.ok) {
    throw fail("assertion_failed");
  }
}

async function stepOpenProviderPrices() {
  const link = { text: TEXT.navProviderPrices, href: TEXT.providerPricesPath };
  const clicked = await waitFor(() => evaluate(clickNavLink, link), (v) => v === true, ELEMENT_WAIT_MS);
  if (!clicked.ok) {
    throw fail("element_missing");
  }
  // 落定的判断与失败码同目录页（catalogSettled / rejectCatalogState 不看页面是哪一种，只看这几个状态位）。
  const opened = await waitFor(
    () => evaluate(probeProviderPrices, TEXT),
    catalogSettled(TEXT.providerPricesPath),
    ELEMENT_WAIT_MS,
  );
  if (!opened.ok) {
    throw fail("element_missing");
  }
  // 生产上可能还没有任何价格版本，空列表合法。
  rejectCatalogState(opened.value, TEXT.providerPricesPath, true);
}

/** 汇率页落定：不在加载，且版本列表落在某一种终态、拉取记录一节也画出来了（或整页失败 / 无权限）。 */
function fxSettled(s) {
  return (
    s.appError ||
    s.notFound ||
    (s.path === TEXT.fxRatesPath &&
      !s.busy &&
      (s.failed || s.forbidden || ((s.empty || s.table) && s.attemptsShown)))
  );
}

async function stepOpenFxRates() {
  const link = { text: TEXT.navFxRates, href: TEXT.fxRatesPath };
  const clicked = await waitFor(() => evaluate(clickNavLink, link), (v) => v === true, ELEMENT_WAIT_MS);
  if (!clicked.ok) {
    throw fail("element_missing");
  }
  const opened = await waitFor(() => evaluate(probeFxRates, TEXT), fxSettled, ELEMENT_WAIT_MS);
  if (!opened.ok) {
    throw fail("element_missing");
  }
  // 空的版本列表与空的拉取记录都合法（生产上可能还没拉取过）。
  rejectCatalogState(opened.value, TEXT.fxRatesPath, true);
  if (!opened.value.attemptsSection || !opened.value.attemptsShown) {
    throw fail("assertion_failed");
  }

  // Worker 在报到后截图：先把拉取记录一节滚进视口。
  if ((await evaluate(scrollFxAttemptsIntoView, TEXT)) !== true) {
    throw fail("element_missing");
  }
  const visible = await waitFor(() => evaluate(fxAttemptsInView, TEXT), (v) => v === true, SCROLL_WAIT_MS);
  if (!visible.ok) {
    throw fail("assertion_failed");
  }
}

async function stepOpenPricingRules() {
  const link = { text: TEXT.navPricingRules, href: TEXT.pricingRulesPath };
  const clicked = await waitFor(() => evaluate(clickNavLink, link), (v) => v === true, ELEMENT_WAIT_MS);
  if (!clicked.ok) {
    throw fail("element_missing");
  }
  // 落定的判断与失败码同目录页与价格页。
  const opened = await waitFor(
    () => evaluate(probePricingRules, TEXT),
    catalogSettled(TEXT.pricingRulesPath),
    ELEMENT_WAIT_MS,
  );
  if (!opened.ok) {
    throw fail("element_missing");
  }
  // 生产上可能还没有任何定价规则，空列表合法。
  rejectCatalogState(opened.value, TEXT.pricingRulesPath, true);
}

/** 试算页落定：表单画出来了，或者错误提示、无权限、走到了别处。 */
function previewSettled(s) {
  return (
    s.appError ||
    s.notFound ||
    (s.path === TEXT.pricingPreviewPath && !s.busy && (s.failed || s.forbidden || s.form))
  );
}

/** 试算页落在了不该落的状态上：各自的失败码。 */
function rejectPreviewState(s) {
  if (s.appError || s.failed) {
    throw fail("page_error");
  }
  if (s.notFound || s.path !== TEXT.pricingPreviewPath) {
    throw fail("navigation_failed");
  }
  if (s.forbidden || !s.form) {
    throw fail("assertion_failed");
  }
}

async function stepOpenPricingPreview() {
  const link = { text: TEXT.navPricingPreview, href: TEXT.pricingPreviewPath };
  const clicked = await waitFor(() => evaluate(clickNavLink, link), (v) => v === true, ELEMENT_WAIT_MS);
  if (!clicked.ok) {
    throw fail("element_missing");
  }
  const opened = await waitFor(() => evaluate(probePricingPreview, TEXT), previewSettled, ELEMENT_WAIT_MS);
  if (!opened.ok) {
    throw fail("element_missing");
  }
  rejectPreviewState(opened.value);

  // 表单先画出来，下拉要的客户、供应商、计量类型之后才回来：再看一小段时间，期间出现错误提示、无权限或离开了
  // 这一页都算失败。到时什么都没出现就是正常（waitFor 以「没等到」结束）。
  const late = await waitFor(
    () => evaluate(probePricingPreview, TEXT),
    (s) => s.appError || s.notFound || s.failed || s.forbidden || s.path !== TEXT.pricingPreviewPath || !s.form,
    PREVIEW_LOOKUP_SETTLE_MS,
  );
  if (late.ok) {
    rejectPreviewState(late.value);
  }
}

async function stepOpenUsageEvents() {
  const link = { text: TEXT.navUsageEvents, href: TEXT.usageEventsPath };
  const clicked = await waitFor(() => evaluate(clickNavLink, link), (v) => v === true, ELEMENT_WAIT_MS);
  if (!clicked.ok) {
    throw fail("element_missing");
  }
  // 落定的判断与失败码同目录页、价格页与规则页。
  const opened = await waitFor(
    () => evaluate(probeUsageEvents, TEXT),
    catalogSettled(TEXT.usageEventsPath),
    ELEMENT_WAIT_MS,
  );
  if (!opened.ok) {
    throw fail("element_missing");
  }
  // 生产上可能还没有任何用量事件，空列表合法。
  rejectCatalogState(opened.value, TEXT.usageEventsPath, true);
}

async function stepOpenUsageAnalytics(ctx) {
  // 点顶栏之前的序号：check_usage_analytics 只认在这之后页面自己发出的请求。
  const beforeClick = network.seq;
  const link = { text: TEXT.navUsageAnalytics, href: TEXT.usageAnalyticsPath };
  const clicked = await waitFor(() => evaluate(clickNavLink, link), (v) => v === true, ELEMENT_WAIT_MS);
  if (!clicked.ok) {
    throw fail("element_missing");
  }
  ctx.usageAnalyticsAfterSeq = beforeClick;
  // 落定的判断与失败码同目录页、价格页、规则页与用量事件页。
  const opened = await waitFor(
    () => evaluate(probeUsageAnalytics, TEXT),
    catalogSettled(TEXT.usageAnalyticsPath),
    ELEMENT_WAIT_MS,
  );
  if (!opened.ok) {
    throw fail("element_missing");
  }
  // 默认时间段（最近 30 天）里可能还没有已处理的用量，空列表合法。
  rejectCatalogState(opened.value, TEXT.usageAnalyticsPath, true);
}

async function stepCheckUsageAnalytics(ctx) {
  const ready = await waitFor(
    () => evaluate(probeUsageAnalytics, TEXT),
    catalogSettled(TEXT.usageAnalyticsPath),
    ELEMENT_WAIT_MS,
  );
  if (!ready.ok) {
    throw fail("element_missing");
  }
  rejectCatalogState(ready.value, TEXT.usageAnalyticsPath, true);

  // 后端值：页面自己发出的那次分析 GET 的响应体。有还在路上的就等它落定。本次运行没跑过 open_usage_analytics
  // 时认这一页上已经观察到的请求。
  const afterSeq = ctx.usageAnalyticsAfterSeq ?? 0;
  const observed = await waitFor(
    async () => latestUsageAnalyticsResponse(afterSeq),
    (v) => v !== null,
    RESPONSE_WAIT_MS,
  );
  if (!observed.ok) {
    throw fail("assertion_failed");
  }
  let expected;
  try {
    const { body, base64Encoded } = await cdp.send(
      "Network.getResponseBody",
      { requestId: observed.value.requestId },
      sessionId,
    );
    const envelope = JSON.parse(base64Encoded ? Buffer.from(body, "base64").toString("utf8") : body);
    const items = envelope?.success === true ? envelope.data?.items : null;
    if (!Array.isArray(items)) {
      throw fail("assertion_failed");
    }
    expected = items.length;
  } catch {
    // 读不到、不是 JSON、不是成功信封、没有 items：都算没观察到可比的后端值。
    throw fail("assertion_failed");
  }

  // 表格显示的就是这一份响应：数据行数与 items 条数一致；items 为空时是空状态、没有数据行。
  const shown = await waitFor(
    () => evaluate(probeUsageAnalytics, TEXT),
    (s) =>
      s.path === TEXT.usageAnalyticsPath &&
      !s.busy &&
      !s.failed &&
      !s.forbidden &&
      (expected === 0 ? s.empty && s.rows === 0 : s.table && s.rows === expected),
    ELEMENT_WAIT_MS,
  );
  if (!shown.ok) {
    throw fail(shown.value !== null && (shown.value.table || shown.value.empty) ? "assertion_failed" : "element_missing");
  }

  if (expected === 0) {
    return;
  }
  // Worker 在报到后截图：先把表格滚进视口。
  if ((await evaluate(scrollAnalyticsTableIntoView, TEXT)) !== true) {
    throw fail("element_missing");
  }
  const visible = await waitFor(() => evaluate(analyticsTableInView, TEXT), (v) => v === true, SCROLL_WAIT_MS);
  if (!visible.ok) {
    throw fail("assertion_failed");
  }
}

async function stepCheckWebhookSigning(ctx) {
  if (ctx.customerId === null || ctx.fixture === null) {
    throw fail("assertion_failed");
  }
  const customerId = ctx.customerId;
  const href = ctx.fixture.href;

  // 夹具客户详情里的项目区块落定。
  const listed = await waitFor(
    () => evaluate(probeProjects, TEXT),
    (s) => s.appError || s.notFound || (s.path === href && !s.busy && (s.failed || s.empty || s.table)),
    ELEMENT_WAIT_MS,
  );
  if (!listed.ok) {
    throw fail("element_missing");
  }
  const projects = listed.value;
  if (projects.appError || projects.failed) {
    throw fail("page_error");
  }
  if (projects.notFound || projects.path !== href) {
    throw fail("navigation_failed");
  }
  if (projects.empty || !projects.table || typeof projects.projectId !== "string" || projects.projectId === "") {
    throw fail("assertion_failed");
  }
  const projectId = projects.projectId;

  // 点展开之前的序号：只认在这之后页面自己发出的请求。已经展开着（不该发生）就认之前的请求。
  const beforeClick = network.seq;
  let afterSeq = 0;
  if (projects.expand === "collapsed") {
    if ((await evaluate(clickExpandFirstProject, TEXT)) !== true) {
      throw fail("element_missing");
    }
    afterSeq = beforeClick;
  } else if (projects.expand !== "expanded") {
    throw fail("element_missing");
  }

  // 签名密钥面板落定：不在加载，且是某一种终态（表格、空状态、加载失败、无权限）。
  const opened = await waitFor(
    () => evaluate(probeWebhookSigning, TEXT),
    (s) =>
      s.appError ||
      s.notFound ||
      (s.path === href && s.region && !s.busy && (s.failed || s.forbidden || s.empty || s.table)),
    ELEMENT_WAIT_MS,
  );
  if (!opened.ok) {
    throw fail("element_missing");
  }
  const panel = opened.value;
  if (panel.appError || panel.failed) {
    throw fail("page_error");
  }
  if (panel.notFound || panel.path !== href) {
    throw fail("navigation_failed");
  }
  if (panel.forbidden) {
    throw fail("assertion_failed");
  }

  // 后端值：面板自己发出的那次列表 GET 的响应体。有还在路上的就等它落定。
  const observed = await waitFor(
    async () => latestWebhookSecretsResponse(afterSeq, customerId, projectId),
    (v) => v !== null,
    RESPONSE_WAIT_MS,
  );
  if (!observed.ok) {
    throw fail("assertion_failed");
  }
  let expected;
  try {
    const { body, base64Encoded } = await cdp.send(
      "Network.getResponseBody",
      { requestId: observed.value.requestId },
      sessionId,
    );
    const envelope = JSON.parse(base64Encoded ? Buffer.from(body, "base64").toString("utf8") : body);
    const items = envelope?.success === true ? envelope.data?.items : null;
    if (!Array.isArray(items)) {
      throw fail("assertion_failed");
    }
    expected = items.map((item) => ({
      keyVersion: Number.isInteger(item?.key_version) ? String(item.key_version) : null,
      status:
        typeof item?.status === "string" && Object.hasOwn(TEXT.webhookStatusText, item.status)
          ? TEXT.webhookStatusText[item.status]
          : null,
    }));
    if (expected.some((item) => item.keyVersion === null || item.status === null)) {
      throw fail("assertion_failed");
    }
  } catch {
    // 读不到、不是 JSON、不是成功信封、版本号或状态不认识：都算没观察到可比的后端值。
    throw fail("assertion_failed");
  }

  // 面板显示的就是这一份响应：items 为空时是空状态；否则行数、顺序、版本号与状态文案都一致。
  const shown = await waitFor(
    () => evaluate(probeWebhookSigning, TEXT),
    (s) =>
      s.path === href &&
      s.region &&
      !s.busy &&
      !s.failed &&
      !s.forbidden &&
      (expected.length === 0
        ? s.empty && s.rows.length === 0
        : s.table &&
          s.rows.length === expected.length &&
          s.rows.every((row, i) => row.keyVersion === expected[i].keyVersion && row.status === expected[i].status)),
    ELEMENT_WAIT_MS,
  );
  if (!shown.ok) {
    throw fail(shown.value !== null && shown.value.region ? "assertion_failed" : "element_missing");
  }

  // Worker 在报到后截图：先把面板滚进视口。
  if ((await evaluate(scrollWebhookPanelIntoView, TEXT)) !== true) {
    throw fail("element_missing");
  }
  const visible = await waitFor(() => evaluate(webhookPanelInView, TEXT), (v) => v === true, SCROLL_WAIT_MS);
  if (!visible.ok) {
    throw fail("assertion_failed");
  }
}

async function stepCheckAiAccess(ctx) {
  if (ctx.customerId === null || ctx.fixture === null) throw fail("assertion_failed");
  const customerId = ctx.customerId;
  const href = ctx.fixture.href;
  const listed = await waitFor(
    () => evaluate(probeProjects, TEXT),
    (s) => s.appError || s.notFound || (s.path === href && !s.busy && (s.failed || s.empty || s.table)),
    ELEMENT_WAIT_MS,
  );
  if (!listed.ok) throw fail("element_missing");
  const projects = listed.value;
  if (projects.appError || projects.failed) throw fail("page_error");
  if (projects.notFound || projects.path !== href) throw fail("navigation_failed");
  if (projects.empty || !projects.table || typeof projects.projectId !== "string" || projects.projectId === "") throw fail("assertion_failed");
  const beforeClick = network.seq;
  let afterSeq = 0;
  if (projects.expand === "collapsed") {
    if ((await evaluate(clickExpandFirstProject, TEXT)) !== true) throw fail("element_missing");
    afterSeq = beforeClick;
  } else if (projects.expand !== "expanded") throw fail("element_missing");
  const opened = await waitFor(
    () => evaluate(probeAiAccess, TEXT),
    (s) => s.appError || s.notFound || (s.path === href && s.region && !s.busy && (s.failed || s.forbidden || s.empty || s.table)),
    ELEMENT_WAIT_MS,
  );
  if (!opened.ok) throw fail("element_missing");
  const panel = opened.value;
  if (panel.appError || panel.failed) throw fail("page_error");
  if (panel.notFound || panel.path !== href) throw fail("navigation_failed");
  if (panel.forbidden) throw fail("assertion_failed");
  const observed = await waitFor(
    async () => latestAiAccessResponse(afterSeq, customerId, projects.projectId),
    (v) => v !== null,
    RESPONSE_WAIT_MS,
  );
  if (!observed.ok) throw fail("assertion_failed");
  let expected;
  try {
    const { body, base64Encoded } = await cdp.send("Network.getResponseBody", { requestId: observed.value.requestId }, sessionId);
    const envelope = JSON.parse(base64Encoded ? Buffer.from(body, "base64").toString("utf8") : body);
    const items = envelope?.success === true ? envelope.data : null;
    if (!Array.isArray(items)) throw fail("assertion_failed");
    expected = items.map((item) => ({
      clientId: typeof item?.client_id === "string" ? item.client_id : null,
      status: typeof item?.status === "string" && Object.hasOwn(TEXT.aiAccessStatusText, item.status) ? TEXT.aiAccessStatusText[item.status] : null,
    }));
    if (expected.some((item) => item.clientId === null || item.status === null)) throw fail("assertion_failed");
  } catch { throw fail("assertion_failed"); }
  const shown = await waitFor(
    () => evaluate(probeAiAccess, TEXT),
    (s) => s.path === href && s.region && !s.busy && !s.failed && !s.forbidden && (expected.length === 0 ? s.empty && s.rows.length === 0 : s.table && s.rows.length === expected.length && s.rows.every((row, i) => row.clientId === expected[i].clientId && row.status === expected[i].status)),
    ELEMENT_WAIT_MS,
  );
  if (!shown.ok) throw fail(shown.value !== null && shown.value.region ? "assertion_failed" : "element_missing");
  if ((await evaluate(scrollAiAccessPanelIntoView, TEXT)) !== true) throw fail("element_missing");
  const visible = await waitFor(() => evaluate(aiAccessPanelInView, TEXT), (v) => v === true, SCROLL_WAIT_MS);
  if (!visible.ok) throw fail("assertion_failed");
}

async function stepCheckProjectIntegration(ctx) {
  if (ctx.customerId === null || ctx.fixture === null) {
    throw fail("assertion_failed");
  }
  const customerId = ctx.customerId;
  const href = ctx.fixture.href;

  // 夹具客户详情里的项目区块落定（与 check_webhook_signing 相同）。
  const listed = await waitFor(
    () => evaluate(probeProjects, TEXT),
    (s) => s.appError || s.notFound || (s.path === href && !s.busy && (s.failed || s.empty || s.table)),
    ELEMENT_WAIT_MS,
  );
  if (!listed.ok) {
    throw fail("element_missing");
  }
  const projects = listed.value;
  if (projects.appError || projects.failed) {
    throw fail("page_error");
  }
  if (projects.notFound || projects.path !== href) {
    throw fail("navigation_failed");
  }
  if (projects.empty || !projects.table || typeof projects.projectId !== "string" || projects.projectId === "") {
    throw fail("assertion_failed");
  }
  const projectId = projects.projectId;

  // 只点展开；已经展开着（例如 check_webhook_signing 刚展开过）就不再点。
  if (projects.expand === "collapsed") {
    if ((await evaluate(clickExpandFirstProject, TEXT)) !== true) {
      throw fail("element_missing");
    }
  } else if (projects.expand !== "expanded") {
    throw fail("element_missing");
  }

  // 集成状态面板落定：地址读完（不在加载），且是某一种终态（显示了状态、加载失败、无权限）。
  const opened = await waitFor(
    () => evaluate(probeProjectIntegration, TEXT),
    (s) =>
      s.appError ||
      s.notFound ||
      (s.path === href && s.region && !s.busy && (s.failed || s.forbidden || s.status !== null)),
    ELEMENT_WAIT_MS,
  );
  if (!opened.ok) {
    throw fail("element_missing");
  }
  const panel = opened.value;
  if (panel.appError || panel.failed) {
    throw fail("page_error");
  }
  if (panel.notFound || panel.path !== href) {
    throw fail("navigation_failed");
  }
  if (panel.forbidden) {
    throw fail("assertion_failed");
  }

  // 后端值：详情页自己发出的第 1 页项目列表 GET 的响应体。有还在路上的就等它落定。
  const observed = await waitFor(
    async () => latestProjectListResponse(customerId),
    (v) => v !== null,
    RESPONSE_WAIT_MS,
  );
  if (!observed.ok) {
    throw fail("assertion_failed");
  }
  let expected;
  try {
    const { body, base64Encoded } = await cdp.send(
      "Network.getResponseBody",
      { requestId: observed.value.requestId },
      sessionId,
    );
    const envelope = JSON.parse(base64Encoded ? Buffer.from(body, "base64").toString("utf8") : body);
    const items = envelope?.success === true ? envelope.data?.items : null;
    const first = Array.isArray(items) && items.length > 0 ? items[0] : null;
    const status = first?.integration_status;
    if (
      first?.id !== projectId ||
      typeof status !== "string" ||
      !Object.hasOwn(TEXT.integrationStatusText, status)
    ) {
      throw fail("assertion_failed");
    }
    expected = TEXT.integrationStatusText[status];
  } catch {
    // 读不到、不是 JSON、不是成功信封、第一项不是第一行的项目、状态不认识：都算没观察到可比的后端值。
    throw fail("assertion_failed");
  }

  // 面板显示的就是这一份响应里第一个项目的集成状态。
  const shown = await waitFor(
    () => evaluate(probeProjectIntegration, TEXT),
    (s) => s.path === href && s.region && !s.busy && !s.failed && !s.forbidden && s.status === expected,
    ELEMENT_WAIT_MS,
  );
  if (!shown.ok) {
    throw fail(shown.value !== null && shown.value.region ? "assertion_failed" : "element_missing");
  }

  // Worker 在报到后截图：先把面板滚进视口。
  if ((await evaluate(scrollIntegrationPanelIntoView, TEXT)) !== true) {
    throw fail("element_missing");
  }
  const visible = await waitFor(() => evaluate(integrationPanelInView, TEXT), (v) => v === true, SCROLL_WAIT_MS);
  if (!visible.ok) {
    throw fail("assertion_failed");
  }
}

async function stepCheckWalletTransactions(ctx) {
  if (ctx.customerId === null || ctx.fixture === null) {
    throw fail("assertion_failed");
  }
  const customerId = ctx.customerId;
  const href = ctx.fixture.href;

  // 面板落定：不在加载，且是某一种终态（表格、空状态、加载失败、无权限）。
  const opened = await waitFor(
    () => evaluate(probeWalletTransactions, TEXT),
    (s) =>
      s.appError ||
      s.notFound ||
      (s.path === href && s.region && !s.busy && (s.failed || s.forbidden || s.empty || s.table)),
    ELEMENT_WAIT_MS,
  );
  if (!opened.ok) {
    throw fail("element_missing");
  }
  const panel = opened.value;
  if (panel.appError || panel.failed) {
    throw fail("page_error");
  }
  if (panel.notFound || panel.path !== href) {
    throw fail("navigation_failed");
  }
  if (panel.forbidden) {
    throw fail("assertion_failed");
  }

  // 后端值：面板自己发出的最新一页 GET 的响应体。有还在路上的就等它落定。
  const observed = await waitFor(
    async () => latestWalletTransactionsResponse(customerId),
    (v) => v !== null,
    RESPONSE_WAIT_MS,
  );
  if (!observed.ok) {
    throw fail("assertion_failed");
  }
  let expectedRows;
  let expectedFirst;
  try {
    const { body, base64Encoded } = await cdp.send(
      "Network.getResponseBody",
      { requestId: observed.value.requestId },
      sessionId,
    );
    const envelope = JSON.parse(base64Encoded ? Buffer.from(body, "base64").toString("utf8") : body);
    const items = envelope?.success === true ? envelope.data?.items : null;
    if (!Array.isArray(items)) {
      throw fail("assertion_failed");
    }
    expectedRows = items.length;
    expectedFirst = null;
    if (items.length > 0) {
      if (!Number.isInteger(items[0]?.wallet_sequence)) {
        throw fail("assertion_failed");
      }
      expectedFirst = String(items[0].wallet_sequence);
    }
  } catch {
    // 读不到、不是 JSON、不是成功信封、没有 items、序号不是整数：都算没观察到可比的后端值。
    throw fail("assertion_failed");
  }

  // 面板显示的就是这一份响应：数据行数一致、第一行的序号一致；items 为空时是空状态、没有数据行。
  const shown = await waitFor(
    () => evaluate(probeWalletTransactions, TEXT),
    (s) =>
      s.path === href &&
      s.region &&
      !s.busy &&
      !s.failed &&
      !s.forbidden &&
      (expectedRows === 0
        ? s.empty && s.rows === 0
        : s.table && s.rows === expectedRows && s.firstSequence === expectedFirst),
    ELEMENT_WAIT_MS,
  );
  if (!shown.ok) {
    throw fail(shown.value !== null && shown.value.region ? "assertion_failed" : "element_missing");
  }

  // Worker 在报到后截图：先把面板滚进视口。
  if ((await evaluate(scrollWalletTxPanelIntoView, TEXT)) !== true) {
    throw fail("element_missing");
  }
  const visible = await waitFor(() => evaluate(walletTxPanelInView, TEXT), (v) => v === true, SCROLL_WAIT_MS);
  if (!visible.ok) {
    throw fail("assertion_failed");
  }
}

async function stepCheckPortalAccount(ctx) {
  if (ctx.customerId === null || ctx.fixture === null) {
    throw fail("assertion_failed");
  }
  const customerId = ctx.customerId;
  const href = ctx.fixture.href;

  // 面板落定：不在加载，且是某一种终态（已开通的详情、未开通、加载失败、无权限）。
  const opened = await waitFor(
    () => evaluate(probePortalAccount, TEXT),
    (s) =>
      s.appError ||
      s.notFound ||
      (s.path === href && s.region && !s.busy && (s.failed || s.forbidden || s.none || s.statusCount > 0)),
    ELEMENT_WAIT_MS,
  );
  if (!opened.ok) {
    throw fail("element_missing");
  }
  const panel = opened.value;
  if (panel.appError || panel.failed) {
    throw fail("page_error");
  }
  if (panel.notFound || panel.path !== href) {
    throw fail("navigation_failed");
  }
  if (panel.forbidden) {
    throw fail("assertion_failed");
  }

  // 后端值：面板自己发出的 GET 的响应（200 或 404）。有还在路上的就等它落定。
  const observed = await waitFor(
    async () => latestPortalAccountResponse(customerId),
    (v) => v !== null,
    RESPONSE_WAIT_MS,
  );
  if (!observed.ok) {
    throw fail("assertion_failed");
  }
  // null 表示未开通；否则是「Portal status」一项应有的文案。
  let expectedText;
  try {
    const { body, base64Encoded } = await cdp.send(
      "Network.getResponseBody",
      { requestId: observed.value.requestId },
      sessionId,
    );
    const envelope = JSON.parse(base64Encoded ? Buffer.from(body, "base64").toString("utf8") : body);
    if (observed.value.status === 404) {
      if (envelope?.success !== false || envelope?.error?.code !== PORTAL_ACCOUNT_NOT_FOUND) {
        throw fail("assertion_failed");
      }
      expectedText = null;
    } else {
      const status = envelope?.success === true ? envelope.data?.status : null;
      if (typeof status !== "string" || !Object.hasOwn(TEXT.portalStatusText, status)) {
        throw fail("assertion_failed");
      }
      expectedText = TEXT.portalStatusText[status];
    }
  } catch {
    // 读不到、不是 JSON、不是信封、404 却不是「未开通」、状态不认识：都算没观察到可比的后端值。
    throw fail("assertion_failed");
  }

  // 面板显示的就是这一份响应：未开通时显示未开通、没有状态一项；已开通时状态一项的文字一致、不显示未开通。
  const shown = await waitFor(
    () => evaluate(probePortalAccount, TEXT),
    (s) =>
      s.path === href &&
      s.region &&
      !s.busy &&
      !s.failed &&
      !s.forbidden &&
      (expectedText === null
        ? s.none && s.statusCount === 0
        : !s.none && s.statusCount === 1 && s.statusText === expectedText),
    ELEMENT_WAIT_MS,
  );
  if (!shown.ok) {
    throw fail(shown.value !== null && shown.value.region ? "assertion_failed" : "element_missing");
  }

  // Worker 在报到后截图：先把面板滚进视口。只滚动，不点任何按钮。
  if ((await evaluate(scrollPortalPanelIntoView, TEXT)) !== true) {
    throw fail("element_missing");
  }
  const visible = await waitFor(() => evaluate(portalPanelInView, TEXT), (v) => v === true, SCROLL_WAIT_MS);
  if (!visible.ok) {
    throw fail("assertion_failed");
  }
}

async function stepCheckLowBalanceThreshold(ctx) {
  // 要排在 open_fixture 之后：没打开夹具客户详情就没有可看的面板。
  if (ctx.customerId === null || ctx.fixture === null) {
    throw fail("assertion_failed");
  }
  const customerId = ctx.customerId;
  const href = ctx.fixture.href;

  // 面板落定：不在加载，且是某一种终态（「Threshold」一项、加载失败、无权限）。
  const opened = await waitFor(
    () => evaluate(probeLowBalanceThreshold, TEXT),
    (s) =>
      s.appError ||
      s.notFound ||
      (s.path === href && s.region && !s.busy && (s.failed || s.forbidden || s.valueCount > 0)),
    ELEMENT_WAIT_MS,
  );
  if (!opened.ok) {
    throw fail("element_missing");
  }
  const panel = opened.value;
  if (panel.appError || panel.failed) {
    throw fail("page_error");
  }
  if (panel.notFound || panel.path !== href) {
    throw fail("navigation_failed");
  }
  if (panel.forbidden) {
    throw fail("assertion_failed");
  }

  // 后端值：面板自己发出的 GET 的响应体。有还在路上的就等它落定。
  const observed = await waitFor(
    async () => latestLowBalanceThresholdResponse(customerId),
    (v) => v !== null,
    RESPONSE_WAIT_MS,
  );
  if (!observed.ok) {
    throw fail("assertion_failed");
  }
  // null 表示未设置；否则是「Threshold」一项应有的、按 2 位舍入后的值（不带千分位）。
  let expected;
  try {
    const envelope = await readEnvelope(observed.value.requestId);
    const data = envelope?.success === true ? envelope.data : null;
    if (data === null || typeof data !== "object" || !Object.hasOwn(data, "low_balance_threshold")) {
      throw fail("assertion_failed");
    }
    const value = data.low_balance_threshold;
    if (value === null) {
      expected = null;
    } else {
      if (typeof value !== "string" || !THRESHOLD_AMOUNT.test(value)) {
        throw fail("assertion_failed");
      }
      expected = roundHalfUp2(value);
      if (expected === null) {
        throw fail("assertion_failed");
      }
    }
  } catch {
    // 读不到、不是 JSON、不是成功信封、没有这个键、金额形状不对：都算没观察到可比的后端值。
    throw fail("assertion_failed");
  }

  // 面板显示的就是这一份响应：null 时显示未设置；否则去掉空白与千分位逗号后等于舍入后的值。
  const shown = await waitFor(
    () => evaluate(probeLowBalanceThreshold, TEXT),
    (s) =>
      s.path === href &&
      s.region &&
      !s.busy &&
      !s.failed &&
      !s.forbidden &&
      s.valueCount === 1 &&
      (expected === null
        ? s.valueText === TEXT.lowBalanceNotSet
        : s.valueText !== null && s.valueText.replace(/[\s,]/g, "") === expected),
    ELEMENT_WAIT_MS,
  );
  if (!shown.ok) {
    throw fail(shown.value !== null && shown.value.region ? "assertion_failed" : "element_missing");
  }

  // Worker 在报到后截图：先把面板滚进视口。只滚动，不点任何按钮。
  if ((await evaluate(scrollLowBalancePanelIntoView, TEXT)) !== true) {
    throw fail("element_missing");
  }
  const visible = await waitFor(() => evaluate(lowBalancePanelInView, TEXT), (v) => v === true, SCROLL_WAIT_MS);
  if (!visible.ok) {
    throw fail("assertion_failed");
  }
}

async function stepLoginCustomer(ctx) {
  // 字段缺失或类型不对：不打印收到的内容，直接 login_failed。
  if (ctx.customerCredentials === null) {
    throw fail("login_failed");
  }
  // 与 login 一样不自己导航：浏览器刚启动时首页被 RequireAuth 送到 /login。
  const form = await waitFor(
    () => evaluate(probeCustomerLogin, TEXT),
    (s) => s.form || s.adminLayout || s.portalShell || s.appError || s.notFound,
    ELEMENT_WAIT_MS,
  );
  if (form.ok && form.value.appError) {
    throw fail("page_error");
  }
  if (!form.ok || !form.value.form) {
    throw fail("element_missing");
  }

  await typeInto(TEXT.loginEmail, ctx.customerCredentials.email);
  await typeInto(TEXT.loginPassword, ctx.customerCredentials.password);
  const beforeSubmitSeq = network.seq;
  if ((await evaluate(clickButton, TEXT.loginSubmit)) !== true) {
    throw fail("element_missing");
  }

  // 登录页先跳到管理端首页，RequireRole 问过 /api/v1/me 之后再把 CUSTOMER 转到 /portal：等到落定在某一边。
  const landed = await waitFor(
    () => evaluate(probeCustomerLogin, TEXT),
    (s) =>
      s.appError ||
      s.adminLayout ||
      s.code ||
      s.enrol ||
      (s.path === TEXT.loginPath && s.alert) ||
      (s.path === TEXT.portalHomePath && s.portalShell),
    LOGIN_WAIT_MS,
  );
  if (!landed.ok) {
    // 还在登录页（或停在 RequireRole 的错误态）：没登进门户。
    const stuck = landed.value;
    if (stuck !== null && stuck.path !== TEXT.loginPath && stuck.alert) {
      throw fail("page_error");
    }
    throw fail(stuck !== null && stuck.path !== TEXT.loginPath ? "assertion_failed" : "login_failed");
  }
  const page = landed.value;
  if (page.appError) {
    throw fail("page_error");
  }
  // 门户账号走到验证码或「首次启用 2FA」：本脚本没有客户的 TOTP 密钥，后者还会写数据，都不往下走。
  if (page.code || page.enrol || page.path === TEXT.loginPath) {
    throw fail("login_failed");
  }
  if (page.adminLayout) {
    throw fail("assertion_failed");
  }

  // 落在门户首页之后再看一小段时间：不能随后又被转去管理端或别处。
  const moved = await waitFor(
    () => evaluate(probeCustomerLogin, TEXT),
    (s) => s.adminLayout || s.path !== TEXT.portalHomePath || !s.portalShell,
    ACCEPT_INVITATION_SETTLE_MS,
  );
  if (moved.ok) {
    throw fail("assertion_failed");
  }
  ctx.customerLoginSeq = beforeSubmitSeq;
}

/** 后端值里 company_name 与页面文字比较前的归一：与页面脚本的 norm 相同（空白折叠、去首尾空白）。 */
function normText(value) {
  return String(value ?? "").replace(/\s+/g, " ").trim();
}

async function stepCheckPortalHome(ctx) {
  if (ctx.customerLoginSeq === null) {
    throw fail("assertion_failed");
  }
  const afterSeq = ctx.customerLoginSeq;

  // 首页落定：门户外壳在，不在加载，且是某一种终态（显示了摘要，或加载失败）。
  const opened = await waitFor(
    () => evaluate(probePortalHome, TEXT),
    (s) =>
      s.appError ||
      s.notFound ||
      s.adminLayout ||
      (s.path === TEXT.portalHomePath && s.portalShell && !s.busy && (s.failed || s.companyCount > 0)),
    ELEMENT_WAIT_MS,
  );
  if (!opened.ok) {
    throw fail("element_missing");
  }
  const page = opened.value;
  if (page.appError || page.failed) {
    throw fail("page_error");
  }
  if (page.notFound || page.path !== TEXT.portalHomePath) {
    throw fail("navigation_failed");
  }
  if (page.adminLayout || page.adminMenu) {
    throw fail("assertion_failed");
  }

  // 后端值：页面自己发出的 GET /api/v1/customer/me。有还在路上的就等它落定。
  const observed = await waitFor(async () => latestCustomerMeResponse(afterSeq), (v) => v !== null, RESPONSE_WAIT_MS);
  if (!observed.ok || observed.value.status !== 200) {
    throw fail("assertion_failed");
  }
  let expected;
  try {
    const { body, base64Encoded } = await cdp.send(
      "Network.getResponseBody",
      { requestId: observed.value.requestId },
      sessionId,
    );
    const envelope = JSON.parse(base64Encoded ? Buffer.from(body, "base64").toString("utf8") : body);
    const data = envelope?.success === true ? envelope.data : null;
    if (
      typeof data?.company_name !== "string" ||
      typeof data?.account_status !== "string" ||
      typeof data?.read_only !== "boolean" ||
      !Object.hasOwn(TEXT.portalHomeAccountStatusText, data.account_status)
    ) {
      throw fail("assertion_failed");
    }
    expected = {
      company: normText(data.company_name),
      status: TEXT.portalHomeAccountStatusText[data.account_status],
      readOnly: data.read_only,
    };
  } catch {
    // 读不到、不是 JSON、不是信封、字段缺失、状态不认识：都算没观察到可比的后端值。
    throw fail("assertion_failed");
  }

  // 页面显示的就是这一份响应：两项各恰好一项且文字一致，只读提示与 read_only 一致，没有管理端菜单项。
  const shown = await waitFor(
    () => evaluate(probePortalHome, TEXT),
    (s) =>
      s.path === TEXT.portalHomePath &&
      s.portalShell &&
      !s.busy &&
      !s.failed &&
      !s.adminLayout &&
      !s.adminMenu &&
      s.companyCount === 1 &&
      s.companyText === expected.company &&
      s.statusCount === 1 &&
      s.statusText === expected.status &&
      s.readOnly === expected.readOnly,
    ELEMENT_WAIT_MS,
  );
  if (!shown.ok) {
    throw fail(shown.value !== null && shown.value.companyCount > 0 ? "assertion_failed" : "element_missing");
  }

  // Worker 在报到后截图：先把「Company name」这一行滚到视口中间。只滚动，不点任何按钮。
  if ((await evaluate(scrollPortalHomeIntoView, TEXT)) !== true) {
    throw fail("element_missing");
  }
  const visible = await waitFor(() => evaluate(portalHomeInView, TEXT), (v) => v === true, SCROLL_WAIT_MS);
  if (!visible.ok) {
    throw fail("assertion_failed");
  }
}

/** 读一个被观察的请求的响应体并解析成信封；读不到或不是 JSON 时抛（调用方把它当成 assertion_failed）。 */
async function readEnvelope(requestId) {
  const { body, base64Encoded } = await cdp.send("Network.getResponseBody", { requestId }, sessionId);
  return JSON.parse(base64Encoded ? Buffer.from(body, "base64").toString("utf8") : body);
}

async function stepCheckPortalWallet(ctx) {
  if (ctx.customerLoginSeq === null) {
    throw fail("assertion_failed");
  }

  // 经门户导航打开钱包页：只点「Wallet」这一个链接。
  const afterSeq = network.seq;
  const clicked = await waitFor(
    () => evaluate(clickPortalNavWallet, TEXT),
    (v) => v === true,
    ELEMENT_WAIT_MS,
  );
  if (!clicked.ok) {
    throw fail("element_missing");
  }

  // 钱包页落定：门户外壳在，余额卡与流水一块都不在加载，且各是某一种终态。
  const opened = await waitFor(
    () => evaluate(probePortalWallet, TEXT),
    (s) =>
      s.appError ||
      s.notFound ||
      s.adminLayout ||
      (s.path === TEXT.portalWalletPath &&
        s.portalShell &&
        s.card &&
        s.transactions &&
        !s.cardBusy &&
        !s.txBusy &&
        (s.cardFailed || s.balanceCount > 0) &&
        (s.txFailed || s.txEmpty || s.table)),
    ELEMENT_WAIT_MS,
  );
  if (!opened.ok) {
    throw fail("element_missing");
  }
  const page = opened.value;
  if (page.appError || page.cardFailed || page.txFailed) {
    throw fail("page_error");
  }
  if (page.notFound || page.path !== TEXT.portalWalletPath) {
    throw fail("navigation_failed");
  }
  if (page.adminLayout) {
    throw fail("assertion_failed");
  }

  // 后端值：页面在点击之后自己发出的两个 GET 的响应体（被动观察，不自己发请求）。有还在路上的就等它落定。
  const walletSeen = await waitFor(
    async () => latestCustomerWalletResponse(network.customerWallet, afterSeq),
    (v) => v !== null,
    RESPONSE_WAIT_MS,
  );
  const transactionsSeen = await waitFor(
    async () => latestCustomerWalletResponse(network.customerTransactions, afterSeq),
    (v) => v !== null,
    RESPONSE_WAIT_MS,
  );
  if (
    !walletSeen.ok ||
    walletSeen.value.status !== 200 ||
    !transactionsSeen.ok ||
    transactionsSeen.value.status !== 200
  ) {
    throw fail("assertion_failed");
  }
  let expected;
  try {
    const walletEnvelope = await readEnvelope(walletSeen.value.requestId);
    const data = walletEnvelope?.success === true ? walletEnvelope.data : null;
    const balance = roundHalfUp2(data?.balance);
    if (typeof data?.currency !== "string" || data.currency === "" || balance === null) {
      throw fail("assertion_failed");
    }
    const transactionsEnvelope = await readEnvelope(transactionsSeen.value.requestId);
    const items = transactionsEnvelope?.success === true ? transactionsEnvelope.data?.items : null;
    if (!Array.isArray(items)) {
      throw fail("assertion_failed");
    }
    let firstEventId = null;
    if (items.length > 0) {
      const eventId = items[0]?.event_id;
      if (eventId !== null && typeof eventId !== "string") {
        throw fail("assertion_failed");
      }
      // event_id 为 null 的行在表里留空。
      firstEventId = normText(eventId ?? "");
    }
    expected = { currency: data.currency, balance, rows: items.length, firstEventId };
  } catch {
    // 读不到、不是 JSON、不是成功信封、余额不是十进制字符串、没有 items、event_id 类型不对：都算没观察到可比的后端值。
    throw fail("assertion_failed");
  }

  // 页面显示的就是这两份响应：余额去掉币种前缀、空格与千分位逗号后等于舍入后的值；数据行数一致，第一行的
  // event_id 一致；items 为空时是空状态、没有数据行。
  const prefix = `${expected.currency} `;
  const shownBalance = (text) =>
    typeof text === "string" && text.startsWith(prefix) ? text.slice(prefix.length).replace(/[\s,]/g, "") : null;
  const shown = await waitFor(
    () => evaluate(probePortalWallet, TEXT),
    (s) =>
      s.path === TEXT.portalWalletPath &&
      s.portalShell &&
      !s.adminLayout &&
      !s.cardBusy &&
      !s.cardFailed &&
      !s.txBusy &&
      !s.txFailed &&
      s.balanceCount === 1 &&
      shownBalance(s.balanceText) === expected.balance &&
      (expected.rows === 0
        ? s.txEmpty && s.rows === 0
        : s.table && s.rows === expected.rows && s.firstEventId === expected.firstEventId),
    ELEMENT_WAIT_MS,
  );
  if (!shown.ok) {
    throw fail(shown.value !== null && shown.value.card ? "assertion_failed" : "element_missing");
  }

  // Worker 在报到后截图：先把余额卡滚进视口。只滚动，不点任何按钮（不点「Older」「Newest」）。
  if ((await evaluate(scrollPortalWalletIntoView, TEXT)) !== true) {
    throw fail("element_missing");
  }
  const visible = await waitFor(() => evaluate(portalWalletInView, TEXT), (v) => v === true, SCROLL_WAIT_MS);
  if (!visible.ok) {
    throw fail("assertion_failed");
  }
}

/**
 * 门户首页的余额卡与最近流水用 probePortalWallet 读：同一张余额卡、同一张流水表，只是流水那一块的 aria-label 是
 * 「Recent transactions」。换掉这一个文案再送进页面，不另写一份同样的探测函数。
 */
const PORTAL_DASHBOARD_TEXT = { ...TEXT, portalTxTitle: TEXT.portalRecentTitle };

async function stepCheckPortalDashboard(ctx) {
  if (ctx.customerLoginSeq === null) {
    throw fail("assertion_failed");
  }
  const afterSeq = ctx.customerLoginSeq;

  // 不导航、不点：停留在 login_customer 落定的门户首页，等余额卡与最近流水各自落定在某一种终态。
  const opened = await waitFor(
    () => evaluate(probePortalWallet, PORTAL_DASHBOARD_TEXT),
    (s) =>
      s.appError ||
      s.notFound ||
      s.adminLayout ||
      s.path !== TEXT.portalHomePath ||
      (s.portalShell &&
        s.card &&
        s.transactions &&
        !s.cardBusy &&
        !s.txBusy &&
        (s.cardFailed || s.balanceCount > 0) &&
        (s.txFailed || s.txEmpty || s.table)),
    ELEMENT_WAIT_MS,
  );
  if (!opened.ok) {
    throw fail("element_missing");
  }
  const page = opened.value;
  if (page.appError || page.cardFailed || page.txFailed) {
    throw fail("page_error");
  }
  if (page.notFound || page.path !== TEXT.portalHomePath) {
    throw fail("navigation_failed");
  }
  if (page.adminLayout) {
    throw fail("assertion_failed");
  }

  // 后端值：页面自己发出的钱包与最近流水两个 GET 的响应体（被动观察，不自己发请求）。有还在路上的就等它落定。
  const walletSeen = await waitFor(
    async () => latestCustomerWalletResponse(network.customerWallet, afterSeq),
    (v) => v !== null,
    RESPONSE_WAIT_MS,
  );
  const transactionsSeen = await waitFor(
    async () => latestRecentTransactionsResponse(afterSeq),
    (v) => v !== null,
    RESPONSE_WAIT_MS,
  );
  if (
    !walletSeen.ok ||
    walletSeen.value.status !== 200 ||
    !transactionsSeen.ok ||
    transactionsSeen.value.status !== 200
  ) {
    throw fail("assertion_failed");
  }
  let expected;
  try {
    const walletEnvelope = await readEnvelope(walletSeen.value.requestId);
    const data = walletEnvelope?.success === true ? walletEnvelope.data : null;
    const balance = roundHalfUp2(data?.balance);
    if (typeof data?.currency !== "string" || data.currency === "" || balance === null) {
      throw fail("assertion_failed");
    }
    const transactionsEnvelope = await readEnvelope(transactionsSeen.value.requestId);
    const items = transactionsEnvelope?.success === true ? transactionsEnvelope.data?.items : null;
    if (!Array.isArray(items) || items.length > RECENT_TRANSACTIONS_PAGE_SIZE) {
      throw fail("assertion_failed");
    }
    expected = { currency: data.currency, balance, rows: items.length };
  } catch {
    // 读不到、不是 JSON、不是成功信封、余额不是十进制字符串、没有 items 或超过 5 条：都算没观察到可比的后端值。
    throw fail("assertion_failed");
  }

  // 页面显示的就是这两份响应：余额去掉币种前缀、空格与千分位逗号后等于舍入后的值；最近流水的数据行数一致，
  // items 为空时是空状态、没有数据行。
  const prefix = `${expected.currency} `;
  const shownBalance = (text) =>
    typeof text === "string" && text.startsWith(prefix) ? text.slice(prefix.length).replace(/[\s,]/g, "") : null;
  const shown = await waitFor(
    () => evaluate(probePortalWallet, PORTAL_DASHBOARD_TEXT),
    (s) =>
      s.path === TEXT.portalHomePath &&
      s.portalShell &&
      !s.adminLayout &&
      !s.cardBusy &&
      !s.cardFailed &&
      !s.txBusy &&
      !s.txFailed &&
      s.balanceCount === 1 &&
      shownBalance(s.balanceText) === expected.balance &&
      (expected.rows === 0 ? s.txEmpty && s.rows === 0 : s.table && s.rows === expected.rows),
    ELEMENT_WAIT_MS,
  );
  if (!shown.ok) {
    throw fail(shown.value !== null && shown.value.card ? "assertion_failed" : "element_missing");
  }

  // Worker 在报到后截图：先把余额卡滚进视口。只滚动，不点任何链接或按钮。
  if ((await evaluate(scrollPortalWalletIntoView, TEXT)) !== true) {
    throw fail("element_missing");
  }
  const visible = await waitFor(() => evaluate(portalWalletInView, TEXT), (v) => v === true, SCROLL_WAIT_MS);
  if (!visible.ok) {
    throw fail("assertion_failed");
  }
}

async function stepCheckPortalUsage(ctx) {
  if (ctx.customerLoginSeq === null) {
    throw fail("assertion_failed");
  }

  // 经门户导航打开用量页：只点「Usage」这一个链接，之后用页面默认的日期区间，不碰筛选表单。
  const afterSeq = network.seq;
  const clicked = await waitFor(
    () => evaluate(clickPortalNavUsage, TEXT),
    (v) => v === true,
    ELEMENT_WAIT_MS,
  );
  if (!clicked.ok) {
    throw fail("element_missing");
  }

  // 用量页落定：门户外壳在，用量一块不在加载，且是某一种终态。
  const opened = await waitFor(
    () => evaluate(probePortalUsage, TEXT),
    (s) =>
      s.appError ||
      s.notFound ||
      s.adminLayout ||
      (s.path === TEXT.portalUsagePath &&
        s.portalShell &&
        s.region &&
        !s.busy &&
        (s.failed || s.tooBroad || s.empty || s.table)),
    ELEMENT_WAIT_MS,
  );
  if (!opened.ok) {
    throw fail("element_missing");
  }
  const page = opened.value;
  if (page.appError || page.failed) {
    throw fail("page_error");
  }
  if (page.notFound || page.path !== TEXT.portalUsagePath) {
    throw fail("navigation_failed");
  }
  // 默认区间（本月）都超出后端的执行预算：页面对这个客户不可用，判失败（不是页面坏了，是查询过宽）。
  if (page.adminLayout || page.tooBroad) {
    throw fail("assertion_failed");
  }

  // 后端值：页面在点击之后自己发出的、不带 cursor 与 event_id 的 GET 的响应体（被动观察，不自己发请求）。
  const usageSeen = await waitFor(
    async () => latestCustomerWalletResponse(network.customerUsage, afterSeq),
    (v) => v !== null,
    RESPONSE_WAIT_MS,
  );
  if (!usageSeen.ok) {
    throw fail("assertion_failed");
  }
  let expected;
  try {
    const envelope = await readEnvelope(usageSeen.value.requestId);
    if (envelope?.error?.code === QUERY_TOO_BROAD) {
      // 422 QUERY_TOO_BROAD：默认区间（本月）的查询超出后端 2 秒的执行预算，用量页对这个客户不可用。
      throw fail("assertion_failed");
    }
    if (usageSeen.value.status !== 200 || envelope?.success !== true) {
      throw fail("assertion_failed");
    }
    const items = envelope.data?.items;
    if (!Array.isArray(items)) {
      throw fail("assertion_failed");
    }
    let firstCharged = null;
    if (items.length > 0) {
      firstCharged = trimFraction(roundHalfUp(items[0]?.charged_amount, USAGE_PLACES));
      if (firstCharged === null) {
        throw fail("assertion_failed");
      }
    }
    expected = { rows: items.length, firstCharged };
  } catch {
    // 读不到、不是 JSON、不是成功信封（含 QUERY_TOO_BROAD）、没有 items、计费额不是十进制字符串：都算没观察到可比的后端值。
    throw fail("assertion_failed");
  }

  // 页面显示的就是这份响应：数据行数一致，第一行的计费额去掉空白与千分位逗号后与 6 位舍入的值数值相同；
  // items 为空时是空状态、没有数据行。
  const shownCharged = (text) => (typeof text === "string" ? trimFraction(text.replace(/[\s,]/g, "")) : null);
  const shown = await waitFor(
    () => evaluate(probePortalUsage, TEXT),
    (s) =>
      s.path === TEXT.portalUsagePath &&
      s.portalShell &&
      !s.adminLayout &&
      !s.busy &&
      !s.failed &&
      !s.tooBroad &&
      (expected.rows === 0
        ? s.empty && s.rows === 0
        : s.table && s.rows === expected.rows && shownCharged(s.firstCharged) === expected.firstCharged),
    ELEMENT_WAIT_MS,
  );
  if (!shown.ok) {
    throw fail(shown.value !== null && shown.value.region ? "assertion_failed" : "element_missing");
  }

  // Worker 在报到后截图：先把用量一块滚进视口。只滚动，不点任何按钮（不翻页、不展开）。
  if ((await evaluate(scrollPortalUsageIntoView, TEXT)) !== true) {
    throw fail("element_missing");
  }
  const visible = await waitFor(() => evaluate(portalUsageInView, TEXT), (v) => v === true, SCROLL_WAIT_MS);
  if (!visible.ok) {
    throw fail("assertion_failed");
  }
}

/**
 * 把本月用量卡滚进视口用 scrollPortalWalletIntoView / portalWalletInView：规则相同，只是找的那张卡的 aria-label 是
 * 「This month's usage」。换掉这一个文案再送进页面，不另写一份同样的滚动函数。
 */
const PORTAL_USAGE_SUMMARY_SCROLL_TEXT = { ...TEXT, portalWalletTitle: TEXT.portalUsageSummaryTitle };

async function stepCheckPortalUsageSummary(ctx) {
  if (ctx.customerLoginSeq === null) {
    throw fail("assertion_failed");
  }
  const afterSeq = ctx.customerLoginSeq;

  // 不导航、不点：停留在 login_customer 落定的门户首页，等本月用量卡落定在某一种终态。
  const opened = await waitFor(
    () => evaluate(probePortalUsageSummary, TEXT),
    (s) =>
      s.appError ||
      s.notFound ||
      s.adminLayout ||
      s.path !== TEXT.portalHomePath ||
      (s.portalShell && s.card && !s.busy && (s.failed || s.tooBroad || s.chart)),
    ELEMENT_WAIT_MS,
  );
  if (!opened.ok) {
    throw fail("element_missing");
  }
  const page = opened.value;
  if (page.appError || page.failed) {
    throw fail("page_error");
  }
  if (page.notFound || page.path !== TEXT.portalHomePath) {
    throw fail("navigation_failed");
  }
  // 本月的整月汇总超出后端的执行预算：首页这一块对这个客户不可用，判失败（不是页面坏了，是查询过宽）。
  if (page.adminLayout || page.tooBroad) {
    throw fail("assertion_failed");
  }

  // 后端值：页面自己发出的、不带 month 的 GET 的响应体（被动观察，不自己发请求）。有还在路上的就等它落定。
  const summarySeen = await waitFor(
    async () => latestCustomerWalletResponse(network.customerUsageSummary, afterSeq),
    (v) => v !== null,
    RESPONSE_WAIT_MS,
  );
  if (!summarySeen.ok) {
    throw fail("assertion_failed");
  }
  let expected;
  try {
    const envelope = await readEnvelope(summarySeen.value.requestId);
    if (envelope?.error?.code === QUERY_TOO_BROAD) {
      // 422 QUERY_TOO_BROAD：本月的整月汇总超出后端 2 秒的执行预算，首页这一块对这个客户不可用。
      throw fail("assertion_failed");
    }
    if (summarySeen.value.status !== 200 || envelope?.success !== true) {
      throw fail("assertion_failed");
    }
    const data = envelope.data;
    // 计费额按 6 位 ROUND_HALF_UP 舍入（字符串运算，不经浮点），再规范成页面的写法。
    const charged = trimFraction(roundHalfUp(data?.charged_amount, USAGE_PLACES));
    if (
      typeof data?.month !== "string" ||
      !Number.isInteger(data?.request_count) ||
      !Number.isInteger(data?.conversation_count) ||
      !Array.isArray(data?.days) ||
      charged === null
    ) {
      throw fail("assertion_failed");
    }
    expected = {
      monthText: `${data.month}${TEXT.portalUsageSummaryMonthSuffix}`,
      charged,
      requests: String(data.request_count),
      conversations: String(data.conversation_count),
      bars: data.days.length,
    };
  } catch {
    // 读不到、不是 JSON、不是成功信封（含 QUERY_TOO_BROAD）、字段缺失或类型不对：都算没观察到可比的后端值。
    throw fail("assertion_failed");
  }

  // 页面显示的就是这份响应：月份标题一致；请求数、对话数去掉空白与千分位逗号后与响应相同；计费额去掉空白与千分位逗号
  // 后与 6 位舍入的值数值相同；柱子个数等于 days 的项数。
  const plain = (text) => (typeof text === "string" ? text.replace(/[\s,]/g, "") : null);
  const shown = await waitFor(
    () => evaluate(probePortalUsageSummary, TEXT),
    (s) =>
      s.path === TEXT.portalHomePath &&
      s.portalShell &&
      !s.adminLayout &&
      !s.busy &&
      !s.failed &&
      !s.tooBroad &&
      s.monthTexts.includes(expected.monthText) &&
      s.chargedCount === 1 &&
      trimFraction(plain(s.charged)) === expected.charged &&
      s.requestsCount === 1 &&
      plain(s.requests) === expected.requests &&
      s.conversationsCount === 1 &&
      plain(s.conversations) === expected.conversations &&
      s.chart &&
      s.bars === expected.bars,
    ELEMENT_WAIT_MS,
  );
  if (!shown.ok) {
    throw fail(shown.value !== null && shown.value.card ? "assertion_failed" : "element_missing");
  }

  // Worker 在报到后截图：先把本月用量卡滚进视口。只滚动，不点任何链接或按钮。
  if ((await evaluate(scrollPortalWalletIntoView, PORTAL_USAGE_SUMMARY_SCROLL_TEXT)) !== true) {
    throw fail("element_missing");
  }
  const visible = await waitFor(
    () => evaluate(portalWalletInView, PORTAL_USAGE_SUMMARY_SCROLL_TEXT),
    (v) => v === true,
    SCROLL_WAIT_MS,
  );
  if (!visible.ok) {
    throw fail("assertion_failed");
  }
}

const STEPS = {
  check_accept_invitation_page: stepCheckAcceptInvitationPage,
  login: stepLogin,
  open_customers: stepOpenCustomers,
  open_fixture: stepOpenFixture,
  check_balance: stepCheckBalance,
  open_audit: stepOpenAudit,
  check_audit_entry: stepCheckAuditEntry,
  open_catalog: stepOpenCatalog,
  check_meter_types: stepCheckMeterTypes,
  open_provider_prices: stepOpenProviderPrices,
  open_fx_rates: stepOpenFxRates,
  open_pricing_rules: stepOpenPricingRules,
  open_pricing_preview: stepOpenPricingPreview,
  open_usage_events: stepOpenUsageEvents,
 check_webhook_signing: stepCheckWebhookSigning,
  check_ai_access: stepCheckAiAccess,
  check_project_integration: stepCheckProjectIntegration,
  open_usage_analytics: stepOpenUsageAnalytics,
  check_usage_analytics: stepCheckUsageAnalytics,
  check_account_status: stepCheckAccountStatus,
  check_wallet_transactions: stepCheckWalletTransactions,
  check_portal_account: stepCheckPortalAccount,
  check_low_balance_threshold: stepCheckLowBalanceThreshold,
  login_customer: stepLoginCustomer,
  check_portal_home: stepCheckPortalHome,
  check_portal_wallet: stepCheckPortalWallet,
  check_portal_dashboard: stepCheckPortalDashboard,
  check_portal_usage: stepCheckPortalUsage,
  check_portal_usage_summary: stepCheckPortalUsageSummary,
};

// ---------------------------------------------------------------------------
// 入口
// ---------------------------------------------------------------------------

async function main() {
  const startedAt = Date.now();
  deadline = startedAt + TOTAL_BUDGET_MS;
  setTimeout(() => finishFail(currentStep, timeoutCode(currentStep)), HARD_STOP_MS).unref();

  const input = parseInput(await readFirstLine());
  process.stdin.destroy();
  if (!input.ok) {
    finishFail(input.firstStep, "login_failed");
    return;
  }

  currentStep = input.steps[0];
  network.host = input.host;
  const ctx = {
    /** hosts[0]；check_accept_invitation_page 打开接受邀请页、再用 openEntry 交还页面时用。 */
    host: input.host,
    email: input.email,
    password: input.password,
    totpKey: input.totpKey,
    /** 夹具客户门户账号的 { email, password }；输入里缺失或类型不对时为 null（login_customer 报 login_failed）。 */
    customerCredentials: input.customerCredentials,
    /**
     * login_customer 点「Continue」之前的网络序号（成功后才写入）：check_portal_home、check_portal_dashboard 与
     * check_portal_usage_summary 只认这之后发出的请求，check_portal_wallet 与 check_portal_usage 要求它不为 null（各自再按点击「Wallet」「Usage」之前的
     * 序号截断）。
     * 本次运行没跑过 login_customer、或它没成功时为 null。
     */
    customerLoginSeq: null,
    fixture: null,
    customerId: null,
    /** login 步骤完成的时刻（毫秒）；本次运行没跑过 login 时为 null。 */
    loginCompletedAt: null,
    /** open_usage_analytics 点顶栏之前的网络序号；本次运行没跑过它时为 null。 */
    usageAnalyticsAfterSeq: null,
  };

  try {
    await attachToBlankPage(input.cdpUrl);
    await openEntry(input.host);
    for (const step of input.steps) {
      currentStep = step;
      const run = Object.hasOwn(STEPS, step) ? STEPS[step] : null;
      if (run === null) {
        throw fail("assertion_failed");
      }
      await run(ctx);
      emit(`ACCEPTANCE_STEP: ${step} ok`);
      await sleep(STEP_SETTLE_MS);
    }
    finishPass();
  } catch (error) {
    finishFail(currentStep, error instanceof StepFailure ? error.code : "page_error");
  }
}

// 任何没接住的异常也只打一行结论，不打异常本身。
process.on("uncaughtException", () => finishFail(currentStep, "page_error"));
process.on("unhandledRejection", () => finishFail(currentStep, "page_error"));

void main();

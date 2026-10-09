/**
 * 部署后浏览器验收：管理端客户页（AIH-TASK-015），本任务为 AIH-TASK-024；AIH-TASK-023 加了审计页的两个只读步骤
 * （open_audit、check_audit_entry）；AIH-TASK-035 加了 AI 目录的两个只读步骤（open_catalog、check_meter_types）；
 * AIH-TASK-036 加了供应商价格与汇率的两个只读步骤（open_provider_prices、open_fx_rates）。文件名沿用，不改。
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
 *   credentials  { email, password, totp_secret }
 * 缺字段或类型不对：`ACCEPTANCE_VERDICT: FAIL <第一个步骤> login_failed`，不打印收到的内容。
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
 *   .platform/project.yaml 的 acceptance_hosts。
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
 *   与页面文字或接口响应的比较都在本进程内做，不打印。
 * - 只读：只点「Continue」「Verify」（登录）、顶栏「Customers」、列表的「Next Page」（翻页）、夹具客户的公司名
 *   链接、顶栏「Audit log」、审计页「Action」下拉里 title 为 `LOGIN` 的那一项和「Apply filters」按钮（只让页面按
 *   新的筛选条件重新发 GET）、顶栏「AI catalog」（href 为 /catalog/providers）、供应商页里的「Meter types」链接
 *   （href 为 /catalog/meter-types，不在顶栏里）、顶栏「Provider prices」（href 为 /pricing/provider-prices）、
 *   顶栏「Exchange rates」（href 为 /fx-rates）。不点新建客户、编辑 / 保存、手工调账、建凭据、轮换、吊销等任何
 *   会写数据的按钮；只打开公司名以 `[TEST] Acceptance Fixture` 开头的夹具客户的详情。审计页不改每页条数、不翻页、
 *   不展开行、不点「Clear filters」。目录页不点「New provider」「New meter type」「Rename」「Retire」「Reactivate」
 *   或任何确认按钮、不点状态筛选、不翻页、不打开任何供应商的详情、不填也不提交任何表单。价格页与汇率页不点
 *   「New draft」「New manual draft」「Edit」「Publish」「Retire」「Discard」或任何确认按钮、不动供应商 / 模型 /
 *   状态筛选、不翻页、不打开任何价格版本的详情、不填也不提交任何表单。登录会在服务端留下会话与
 *   登录审计，这是只读验收不可避免的，不算写数据 —— check_audit_entry 核对的正是这一条。若账号走到「首次启用
 *   2FA」那一步，那会写数据，直接 login_failed，不继续。
 * - 每个步骤都真的检查它声称的东西；等待一律有上限（见下面的常量），总时长控制在 timeout_seconds（300 秒）内。
 * - 找元素只按可见文字（文案取自 frontend/src/i18n/locales/en.json）或语义结构（label→control、header、
 *   table/thead/th、th→td、li、role、title 属性），不依赖 antd 生成的类名哈希。文案与选择器集中在下面的常量里。
 *
 * 步骤
 * ----
 *   login           登录表单填邮箱、密码 → Continue → 验证码（TOTP）→ Verify；以离开 /login、出现管理端顶栏
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
 *                   那一行滚动到视口中间
 *   open_provider_prices
 *                   点顶栏「Provider prices」（文案取 en.json 的 nav.providerPrices，href 为 paths.ts 的 providerPrices，
 *                   即 /pricing/provider-prices）进价格页；页面落定且不是加载失败（任何错误提示）、无权限（pricing.forbidden）
 *                   或路由兜底页。空列表合法（pricing.prices.empty），有表格时表头要有「Provider」「Model」两列
 *   open_fx_rates   点顶栏「Exchange rates」（nav.fxRates，href 为 paths.ts 的 fxRates，即 /fx-rates）进汇率页；页面落定
 *                   且不是加载失败、无权限或路由兜底页。版本列表为空（fx.empty）或有表格（表头有「Currency」「Rate (MYR)」）
 *                   都合法；「BNM fetch attempts」一节（fx.attempts.title）可见，且它的表格（表头有「Attempted at」「Outcome」）
 *                   或空状态（fx.attempts.empty）已经画出来，空列表合法；报到前把这一节的标题滚动到视口中间
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
  // antd 分页「下一页」那个 <li> 的 title。不在 en.json 里：来自 antd 自带的 enUS 语言包
  // （frontend/src/App.tsx 的 <ConfigProvider locale={enUS}>）。
  nextPageTitle: "Next Page",
  // 路由路径，取自 frontend/src/routes/paths.ts 的 ROUTES。
  loginPath: "/login",
  customersPath: "/customers",
  customerCreatePath: "/customers/new",
  auditPath: "/audit",
  catalogProvidersPath: "/catalog/providers",
  catalogMeterTypesPath: "/catalog/meter-types",
  providerPricesPath: "/pricing/provider-prices",
  fxRatesPath: "/fx-rates",
};

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
  return step === "login" ? "login_failed" : "element_missing";
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
  // writes 为 true 时同样只做只读步骤，所以这里不再往下传它。
  return { ok: true, steps, cdpUrl, host: hosts[0], email, password, totpKey };
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
  seq: 0,
};

/** 三类被观察的接口里，requestId 对应的那一条记录。 */
function trackedRequest(requestId) {
  return network.details.get(requestId) ?? network.audits.get(requestId) ?? network.meterTypes.get(requestId);
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
  return { norm, leaf, control, button, alert, layout, valueCells, common, auditRows, tableWith, meterTypeRow };
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

/** 把代码为 code 的那一行滚到视口中间。只滚动，不点任何东西。 */
function scrollMeterTypeIntoView(lib, { T, code }) {
  const tr = lib.meterTypeRow(T, code);
  if (tr === null) {
    return false;
  }
  tr.scrollIntoView({ block: "center", inline: "nearest" });
  return true;
}

function meterTypeInView(lib, { T, code }) {
  const tr = lib.meterTypeRow(T, code);
  if (tr === null) {
    return false;
  }
  const rect = tr.getBoundingClientRect();
  return rect.height > 0 && rect.top >= 0 && rect.bottom <= window.innerHeight;
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
  let navigation = null;
  try {
    navigation = await cdp.send("Page.navigate", { url: `https://${host}/` }, sessionId, NAVIGATION_TIMEOUT_MS);
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

const STEPS = {
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
    email: input.email,
    password: input.password,
    totpKey: input.totpKey,
    fixture: null,
    customerId: null,
    /** login 步骤完成的时刻（毫秒）；本次运行没跑过 login 时为 null。 */
    loginCompletedAt: null,
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

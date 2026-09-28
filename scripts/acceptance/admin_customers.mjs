/**
 * 部署后浏览器验收：AIH-TASK-015 的管理端客户页（只读）。OpenClaw P6 的试点，由 AIH-TASK-024 写出。
 *
 * 谁跑、在哪跑（.platform/commands.yaml 的 `acceptance.browser`）
 * - **只由 Worker 在合并后部署成功时运行**（一个 run 最多 3 次尝试），不是提 PR 前的检查。
 * - **不走 MXC、网络不受限、拿凭据**（控制面安全边界 A7）：验收夹具管理员的凭据由 Worker 从本机凭据管理器读出，
 *   经 stdin 一行 JSON 交来。脚本进程本身不受沙箱约束，能不能越界只看评审 —— 所以下面每条约束都写成可逐条核对的样子。
 *
 * 输入（stdin 一行 JSON）
 *   { "steps": [...], "writes": bool, "attempt": int, "cdp": "ws://…", "credentials": { "email", "password", "totp_secret" } }
 * - 字段缺失或类型不对：`ACCEPTANCE_VERDICT: FAIL <第一个步骤> login_failed`，不打印收到的内容。
 * - `cdp` 必须是 ws:// / wss:// 的调试地址（浏览器级 `/devtools/browser/…` 或页面级 `/devtools/page/…`）。
 *   不接受 http:// —— 从 http 端点查 `/json` 就是自己联网，这里不做。
 * - `writes`：只校验类型。本脚本只有只读步骤，writes 为 true 时也一样只读。
 * - `attempt`：大于 1 时先等到下一个 TOTP 时间格再登录。后端拒绝不晚于上次已用时间格的码
 *   （app/services/two_factor.py 的 last_used_counter），上一次尝试若刚用过当前格的码，重试会必然失败。
 * - 站点主机不在输入里，也不读任何文件：取自 Worker 已经打开在站点上的那个页面目标。
 *   浏览器级地址：Target.getTargets 里 https 页面目标的 origin，必须恰好一个（零个或多个不同 origin 都不猜）；
 *   页面级地址：连上后 Runtime.evaluate 读 location.href 取 origin。必须是 https、合法 DNS 主机名、不带端口。
 *   不把主机名抄进本文件。
 *
 * 输出（标准输出只有两类行）
 *   ACCEPTANCE_STEP: <step> ok                 每完成一个声明步骤一行，按声明顺序
 *   ACCEPTANCE_VERDICT: PASS                   最后一行
 *   ACCEPTANCE_VERDICT: FAIL <step> <code>     最后一行
 * - code ∈ login_failed、navigation_failed、page_error、element_missing、assertion_failed；
 *   host_unreachable 只在主文档完全没有得到 HTTP 响应时报，service_unavailable 只在主文档得到 502 / 503 / 504 时报。
 * - 连不上 cdp 地址、找不到可用的页面目标、从页面目标定不出站点 origin：navigation_failed（都是「到不了要验的页面」）。
 * - 出错时不打印异常信息、URL、查询串、响应体；不写 stderr；未捕获的异常也只落成一行 FAIL。
 * - 退出码：PASS 为 0，FAIL 为 1。以 VERDICT 行为准。
 *
 * 步骤（只执行 steps 列出的；不认识的步骤名：FAIL <该步骤> assertion_failed）
 * - login          打开 /login，把凭据填进登录表单，TOTP 第二步现算验证码；以离开登录页、出现管理端布局
 *                  （页头里有 Customers 链接与 Sign out 按钮）为准。账号若被要求注册 2FA，直接 login_failed —— 注册是写操作。
 * - open_customers 点页头的 Customers 进客户列表，按公司名找以 [TEST] Acceptance Fixture 开头的那一行；
 *                  分页时点「下一页」逐页找。不改每页条数、不排序、不筛选。同一页出现多个夹具行：assertion_failed。
 * - open_fixture   从列表点那一行的公司名链接进详情；详情页「Company name」一项必须与列表里的公司名逐字相同。
 * - check_balance  后端值取详情页**自己发出的** GET /api/v1/admin/customers/{customer_id} 的响应体
 *                  （data.wallet.balance / data.wallet.currency）：Network.enable 被动观察，
 *                  Network.getResponseBody 读取。页面「Balance」一项的文字必须与 formatMoney(balance, currency) 逐字相同。
 *   后面的步骤缺前提（例如没跑 open_fixture 就 check_balance）时，自己走到那个页面，但不替没声明的步骤报 ok。
 *
 * 约束（评审逐条核对）
 * - 只用 Node 自带模块：全局 WebSocket（Node 22+）、node:crypto、node:process、
 *   node:buffer、node:timers/promises。不用 node:fs，不读也不写任何文件。没有依赖，不碰 package.json / package-lock.json。
 * - 只经 cdp 地址操作那个浏览器，连它**已有的**页面目标（Target.attachToTarget）。不自己联网（不用 fetch / http /
 *   https / net / dns）；不开新浏览器、新上下文或新标签页（不用 Target.createBrowserContext / Target.createTarget）；
 *   不改代理；不拦截或伪造请求（不用 Fetch.*、Network.setRequestInterception、Network.emulateNetworkConditions）；
 *   不读写 Cookie 与存储（不用 Network.getCookies / Storage.* / DOMStorage.*）；不截图（不用 Page.captureScreenshot）；
 *   不读也不写任何文件。能发的 CDP 方法只有 ALLOWED_CDP_METHODS 里那几个，别的在发出前就被拒绝。
 * - 在页面里执行的只有 pageProbe（只读 DOM、聚焦输入框、滚动到元素）和页面级地址下的一次 `location.href`，
 *   不发请求、不碰存储。
 *   点击走 Input.dispatchMouseEvent，输入走 Input.insertText —— 凭据与验证码只经这条路进登录表单，
 *   不出现在任何执行到页面里的脚本文本中，也不回读（只核对输入框里的字符数）。
 * - 不打印、不保存凭据、TOTP 码与页面内容。TOTP 按 RFC 6238 现算：HMAC-SHA1、30 秒步长、6 位；base32 解码自己写。
 * - 只读：只点登录页的 Continue / Verify、页头的 Customers、分页的「下一页」、夹具行的公司名链接。
 *   新建客户、编辑 / 保存、手工调账、建项目、建凭据、轮换、吊销、登出一概不点（click() 另有一道文案黑名单兜底）。
 *   只打开公司名以 [TEST] Acceptance Fixture 开头的客户详情。登录本身会在服务端留下会话与登录审计，
 *   这是只读验收不可避免的，不算写数据。不点 Sign out：登出会写会话吊销，浏览器由 Worker 收拾。
 * - 每次等待都有上限，超时按对应失败码结束；整次运行在 TOTAL_BUDGET_MS 之内（任务 timeout_seconds 为 300 秒）。
 * - 找元素只按可见文字（frontend/src/i18n/locales/en.json）与语义结构（label→control、header、table/thead/tbody、
 *   th/td、title、aria-busy / aria-disabled / role="alert"），不依赖 antd 的类名或类名哈希。文案与路径集中在下面的常量里。
 */

import { Buffer } from "node:buffer";
import { createHmac } from "node:crypto";
import process from "node:process";
import { setTimeout as sleep } from "node:timers/promises";

// ---------------------------------------------------------------------------------------------
// 输出协议与失败码
// ---------------------------------------------------------------------------------------------

const STEP_PREFIX = "ACCEPTANCE_STEP:";
const VERDICT_PREFIX = "ACCEPTANCE_VERDICT:";

const CODE = Object.freeze({
  LOGIN_FAILED: "login_failed",
  NAVIGATION_FAILED: "navigation_failed",
  PAGE_ERROR: "page_error",
  ELEMENT_MISSING: "element_missing",
  ASSERTION_FAILED: "assertion_failed",
  HOST_UNREACHABLE: "host_unreachable",
  SERVICE_UNAVAILABLE: "service_unavailable",
});

/** 输入里 steps 本身不可用时，FAIL 行里的「第一个步骤」。 */
const DEFAULT_FIRST_STEP = "login";
/** 步骤名只收这种形状 —— 它会被原样打进输出行，不能带空格或别的东西。 */
const STEP_NAME = /^[a-z][a-z0-9_]{0,63}$/;
const MAX_STEPS = 32;

// ---------------------------------------------------------------------------------------------
// 时间上限（毫秒）。任务的 timeout_seconds 是 300；留出收尾与 Worker 自己的余量。
// ---------------------------------------------------------------------------------------------

const TOTAL_BUDGET_MS = 280_000;
/** 看门狗：万一有哪处等待漏了上限，到点也只打一行 FAIL 就退出。 */
const WATCHDOG_MS = TOTAL_BUDGET_MS + 10_000;
const STDIN_TIMEOUT_MS = 10_000;
const MAX_INPUT_CHARS = 64 * 1024;
const CONNECT_TIMEOUT_MS = 10_000;
const CDP_CALL_TIMEOUT_MS = 15_000;
const CLEANUP_TIMEOUT_MS = 2_000;
/** Page.navigate 之后等主文档响应的上限。 */
const DOCUMENT_RESPONSE_TIMEOUT_MS = 45_000;
/** Page.navigate 已经报错时，再等一下可能已在路上的响应事件（5xx 空响应体时两者都会来）。 */
const DOCUMENT_RESPONSE_GRACE_MS = 3_000;
const ELEMENT_TIMEOUT_MS = 30_000;
const LOGIN_RESULT_TIMEOUT_MS = 30_000;
const LIST_TIMEOUT_MS = 30_000;
const DETAIL_TIMEOUT_MS = 30_000;
const BALANCE_TIMEOUT_MS = 30_000;
const POLL_INTERVAL_MS = 250;
const MAX_LIST_PAGES = 200;

// ---------------------------------------------------------------------------------------------
// TOTP（RFC 6238）
// ---------------------------------------------------------------------------------------------

const TOTP_STEP_MS = 30_000;
const TOTP_DIGITS = 6;
/** 当前时间格剩得太少就等下一格，免得码在路上过期。 */
const TOTP_MIN_REMAINING_MS = 5_000;
const BASE32_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567";

// ---------------------------------------------------------------------------------------------
// 站点与路径
// ---------------------------------------------------------------------------------------------

/**
 * 站点 origin 取自 Worker 已打开的页面目标（见 siteOriginOf）。管理端页面与 API 同源，所以只要一个。
 * 只认 https、小写 DNS 主机名、不带端口。
 */
const SITE_PROTOCOL = "https:";
const HOSTNAME = /^(?=.{1,253}$)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+$/;

/** frontend/src/routes/paths.ts 的 ROUTES.login / ROUTES.customers。 */
const PATHS = Object.freeze({ login: "/login", customers: "/customers" });
/** ROUTES.customerDetail（`/customers/:customerId`）；`/customers/new` 是建客户页，另行排除。 */
const CUSTOMER_DETAIL_PATH = /^\/customers\/([^/]+)$/;
const CUSTOMER_CREATE_SEGMENT = "new";
/** frontend/src/api/adminCustomers.ts 的 CUSTOMERS_URL + customerUrl()：详情 GET。`…/projects` 不算。 */
const CUSTOMER_API_PATH = /^\/api\/v1\/admin\/customers\/([^/]+)$/;
/** frontend/src/api/adminCustomers.ts 的 DEFAULT_PAGE_SIZE / MAX_PAGE_SIZE / MAX_PAGE。 */
const DEFAULT_PAGE_SIZE = 20;
const MAX_PAGE_SIZE = 100;
const MAX_PAGE = 10_000;

/** 夹具客户（docs/TODO.md「验收租户与项目」）：只认公司名以它开头的客户。 */
const FIXTURE_PREFIX = "[TEST] Acceptance Fixture";

// ---------------------------------------------------------------------------------------------
// 页面文案：全部取自 frontend/src/i18n/locales/en.json（右边注释是键名）
// ---------------------------------------------------------------------------------------------

const TEXT = Object.freeze({
  loginEmail: "Email", // login.email（Form.Item 的 label）
  loginPassword: "Password", // login.password
  loginSubmit: "Continue", // login.submit
  loginCode: "Verification code", // login.code
  loginVerify: "Verify", // login.verify
  enrolIntro: "Two-factor authentication is required. Scan this with your authenticator app.", // enrol.intro
  enrolConfirm: "Turn on two-factor authentication", // enrol.confirm
  navCustomers: "Customers", // nav.customers（AppLayout 页头菜单）
  navSignOut: "Sign out", // nav.signOut（AppLayout 页头按钮）
  appErrorTitle: "This page could not be loaded", // appError.title（RouteErrorBoundary）
  notFoundTitle: "Page not found", // notFound.title
  forbidden: "Only administrators can manage customers.", // customers.forbidden
  companyName: "Company name", // customers.field.companyName（列表表头 / 详情 Descriptions 标签）
  walletBalance: "Balance", // customers.wallet.balance（钱包 Descriptions 标签）
  listLoading: "Loading customers…", // customers.list.loading
  listLoadFailed: "The customer list could not be loaded.", // customers.list.loadFailed
  listEmpty: "No customers yet.", // customers.list.empty
  listTotalTemplate: "{{total}} customers", // customers.list.total
  detailLoading: "Loading the customer…", // customers.detail.loading
  detailLoadFailed: "The customer could not be loaded.", // customers.detail.loadFailed
  detailNotFound: "Customer not found", // customers.detail.notFound
});

/** antd 自带文案：frontend/src/App.tsx 用 `ConfigProvider locale={enUS}`，en_US 的 Pagination.next_page。 */
const ANTD_TEXT = Object.freeze({ nextPage: "Next Page" });

/** 出现在页面上即说明整页出了问题的文案 → 失败码。 */
const PAGE_FAILURE_TEXTS = Object.freeze({
  appErrorTitle: CODE.PAGE_ERROR,
  notFoundTitle: CODE.NAVIGATION_FAILED,
});

/**
 * 会写数据的按钮与链接文案（en.json）。click() 拒绝点任何文字落在这里的目标 —— 正常流程根本不会去找它们，
 * 这是第二道闸。
 */
const WRITE_ACTION_TEXTS = new Set([
  "New customer", // customers.list.create
  "Create customer", // customers.create.submit
  "Edit", // customers.detail.edit
  "Save changes", // customers.form.save
  "Adjust balance", // wallet.adjustment.open
  "Review", // wallet.adjustment.review
  "Confirm and post", // wallet.adjustment.confirm
  "Retry with the same key", // wallet.adjustment.retry
  "New project", // customers.projects.create
  "Create project", // customers.projects.submit
  "New API key", // integrations.create.open
  "Rotate", // integrations.rotate.open / integrations.rotate.confirm
  "Revoke key", // integrations.revoke.key
  "Revoke permanently", // integrations.revoke.confirm
  "Turn on two-factor authentication", // enrol.confirm
  "Sign out", // nav.signOut
]);
const WRITE_ACTION_PATTERNS = [/^Revoke v\d+$/]; // integrations.revoke.version

// ---------------------------------------------------------------------------------------------
// CDP：只允许这些方法
// ---------------------------------------------------------------------------------------------

const ALLOWED_CDP_METHODS = new Set([
  "Target.getTargets", // 列出已有目标，挑一个页面
  "Target.attachToTarget", // 连已有页面（flatten 会话），不新建
  "Target.detachFromTarget", // 收尾时断开，不关页面
  "Page.getFrameTree", // 主 frame 的 id，用来认主文档响应
  "Page.navigate", // 打开 /login（或缺前提时的 /customers）
  "Network.enable", // 被动观察响应（状态码、详情 GET）
  "Network.getResponseBody", // 读详情页自己那次 GET 的响应体
  "Runtime.evaluate", // 只执行 pageProbe，以及页面级地址下读一次 location.href
  "Input.dispatchMouseEvent", // 点击
  "Input.insertText", // 往已聚焦的登录表单输入框里输入
]);

// ---------------------------------------------------------------------------------------------
// 金额格式：照抄 frontend/src/components/MoneyText.tsx 的 formatAmount / formatMoney，不 import 前端代码。
// 只做字符串操作：整数部分千分位，小数去末尾 0、至少 2 位，不舍入；全零不带负号；不是十进制串就原样返回。
// ---------------------------------------------------------------------------------------------

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

/** `formatMoney("0.00000000", "MYR")` → `"MYR 0.00"`。 */
function formatMoney(amount, currency) {
  return `${currency} ${formatAmount(amount)}`;
}

// ---------------------------------------------------------------------------------------------
// 失败与输出
// ---------------------------------------------------------------------------------------------

class StepFailure extends Error {
  constructor(code) {
    super(code);
    this.code = code;
  }
}

class CdpClosedError extends Error {}

const startedAt = Date.now();
const budgetEnd = startedAt + TOTAL_BUDGET_MS;
const remainingMs = () => budgetEnd - Date.now();

let currentStep = DEFAULT_FIRST_STEP;
let finished = false;
let session = null;

function failLine(step, code) {
  return `${VERDICT_PREFIX} FAIL ${step} ${code}`;
}

async function finish(line, exitCode) {
  if (finished) {
    return;
  }
  finished = true;
  clearTimeout(watchdog);
  await Promise.race([closeSession(), sleep(CLEANUP_TIMEOUT_MS)]).catch(() => undefined);
  process.stdout.write(`${line}\n`, () => process.exit(exitCode));
}

const watchdog = setTimeout(() => {
  void finish(failLine(currentStep, CODE.PAGE_ERROR), 1);
}, WATCHDOG_MS);

// 未捕获的东西也不许把堆栈打出来。
process.on("uncaughtException", () => void finish(failLine(currentStep, CODE.PAGE_ERROR), 1));
process.on("unhandledRejection", () => void finish(failLine(currentStep, CODE.PAGE_ERROR), 1));

// ---------------------------------------------------------------------------------------------
// 输入
// ---------------------------------------------------------------------------------------------

function readInputLine() {
  return new Promise((resolve) => {
    let buffer = "";
    let done = false;
    const settle = (value) => {
      if (done) {
        return;
      }
      done = true;
      clearTimeout(timer);
      process.stdin.removeAllListeners("data");
      process.stdin.destroy();
      resolve(value);
    };
    const timer = setTimeout(() => settle(null), STDIN_TIMEOUT_MS);
    process.stdin.setEncoding("utf8");
    process.stdin.on("data", (chunk) => {
      buffer += chunk;
      const newline = buffer.indexOf("\n");
      if (newline >= 0) {
        settle(buffer.slice(0, newline));
      } else if (buffer.length > MAX_INPUT_CHARS) {
        settle(null);
      }
    });
    process.stdin.on("end", () => settle(buffer));
    process.stdin.on("error", () => settle(null));
  });
}

function isPlainObject(value) {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isNonEmptyString(value) {
  return typeof value === "string" && value.length > 0;
}

/** 不合法时只返回「第一个步骤」，绝不带回收到的内容。 */
function parseInput(line) {
  let data = null;
  try {
    data = line === null ? null : JSON.parse(line.replace(/\r$/, ""));
  } catch {
    data = null;
  }
  const rawSteps = isPlainObject(data) ? data.steps : undefined;
  const firstStep =
    Array.isArray(rawSteps) && typeof rawSteps[0] === "string" && STEP_NAME.test(rawSteps[0])
      ? rawSteps[0]
      : DEFAULT_FIRST_STEP;
  const invalid = { ok: false, firstStep };
  if (!isPlainObject(data)) {
    return invalid;
  }

  const { steps, writes, attempt, cdp, credentials } = data;
  if (
    !Array.isArray(steps) ||
    steps.length === 0 ||
    steps.length > MAX_STEPS ||
    !steps.every((step) => typeof step === "string" && STEP_NAME.test(step))
  ) {
    return invalid;
  }
  if (typeof writes !== "boolean" || !Number.isInteger(attempt) || attempt < 1) {
    return invalid;
  }
  if (!isNonEmptyString(cdp)) {
    return invalid;
  }
  let cdpUrl = null;
  try {
    cdpUrl = new URL(cdp);
  } catch {
    return invalid;
  }
  if (cdpUrl.protocol !== "ws:" && cdpUrl.protocol !== "wss:") {
    return invalid;
  }
  if (
    !isPlainObject(credentials) ||
    !isNonEmptyString(credentials.email) ||
    !isNonEmptyString(credentials.password) ||
    !isNonEmptyString(credentials.totp_secret)
  ) {
    return invalid;
  }
  const totpKey = base32Decode(credentials.totp_secret);
  if (totpKey === null) {
    return invalid;
  }
  return {
    ok: true,
    steps,
    writes,
    attempt,
    cdp,
    pageEndpoint: cdpUrl.pathname.startsWith("/devtools/page/"),
    email: credentials.email,
    password: credentials.password,
    totpKey,
  };
}

// ---------------------------------------------------------------------------------------------
// TOTP：RFC 4648 base32 解码（自己写）+ RFC 6238（HMAC-SHA1、30 秒、6 位）
// ---------------------------------------------------------------------------------------------

function base32Decode(text) {
  const clean = text.replace(/[\s-]/g, "").replace(/=+$/, "").toUpperCase();
  if (clean.length === 0 || !/^[A-Z2-7]+$/.test(clean)) {
    return null;
  }
  const bytes = [];
  let bits = 0;
  let value = 0;
  for (const char of clean) {
    value = (value << 5) | BASE32_ALPHABET.indexOf(char);
    bits += 5;
    if (bits >= 8) {
      bits -= 8;
      bytes.push((value >>> bits) & 0xff);
      value &= (1 << bits) - 1;
    }
  }
  return bytes.length > 0 ? Buffer.from(bytes) : null;
}

function totpCode(key, nowMs) {
  const counter = Math.floor(nowMs / TOTP_STEP_MS);
  const message = Buffer.alloc(8);
  message.writeBigUInt64BE(BigInt(counter));
  const mac = createHmac("sha1", key).update(message).digest();
  const offset = mac[mac.length - 1] & 0x0f;
  const binary =
    ((mac[offset] & 0x7f) << 24) |
    (mac[offset + 1] << 16) |
    (mac[offset + 2] << 8) |
    mac[offset + 3];
  return String(binary % 10 ** TOTP_DIGITS).padStart(TOTP_DIGITS, "0");
}

async function waitForNextTotpWindow() {
  await sleep(TOTP_STEP_MS - (Date.now() % TOTP_STEP_MS) + 250);
}

async function avoidTotpWindowEdge() {
  const left = TOTP_STEP_MS - (Date.now() % TOTP_STEP_MS);
  if (left < TOTP_MIN_REMAINING_MS) {
    await sleep(left + 250);
  }
}

// ---------------------------------------------------------------------------------------------
// CDP 连接（全局 WebSocket）
// ---------------------------------------------------------------------------------------------

class CdpConnection {
  #ws;
  #nextId = 1;
  #pending = new Map();
  #listeners = new Set();
  #closed = false;

  static open(url) {
    return new Promise((resolve, reject) => {
      let ws = null;
      try {
        ws = new WebSocket(url);
      } catch {
        reject(new CdpClosedError());
        return;
      }
      const timer = setTimeout(
        () => {
          try {
            ws.close();
          } catch {
            // 已经关了
          }
          reject(new CdpClosedError());
        },
        Math.max(1, Math.min(CONNECT_TIMEOUT_MS, remainingMs())),
      );
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
          reject(new CdpClosedError());
        },
        { once: true },
      );
    });
  }

  constructor(ws) {
    this.#ws = ws;
    ws.addEventListener("message", (event) => this.#onMessage(event));
    ws.addEventListener("close", () => this.#shutdown());
    ws.addEventListener("error", () => this.#shutdown());
  }

  #onMessage(event) {
    if (typeof event.data !== "string") {
      return;
    }
    let message = null;
    try {
      message = JSON.parse(event.data);
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
      // 错误内容不往外带：调用方只需要知道「失败了」。
      if (message.error) {
        pending.reject(new Error("cdp error"));
      } else {
        pending.resolve(message.result ?? {});
      }
      return;
    }
    if (typeof message.method === "string") {
      for (const listener of this.#listeners) {
        listener(message.method, message.params ?? {}, message.sessionId);
      }
    }
  }

  #shutdown() {
    if (this.#closed) {
      return;
    }
    this.#closed = true;
    for (const pending of this.#pending.values()) {
      clearTimeout(pending.timer);
      pending.reject(new CdpClosedError());
    }
    this.#pending.clear();
  }

  send(method, params, sessionId) {
    if (!ALLOWED_CDP_METHODS.has(method)) {
      return Promise.reject(new Error("cdp method not allowed"));
    }
    if (this.#closed) {
      return Promise.reject(new CdpClosedError());
    }
    const id = this.#nextId++;
    const message = { id, method, params: params ?? {} };
    if (sessionId !== undefined) {
      message.sessionId = sessionId;
    }
    return new Promise((resolve, reject) => {
      const timer = setTimeout(
        () => {
          this.#pending.delete(id);
          reject(new Error("cdp timeout"));
        },
        Math.max(1, Math.min(CDP_CALL_TIMEOUT_MS, remainingMs())),
      );
      this.#pending.set(id, { resolve, reject, timer });
      try {
        this.#ws.send(JSON.stringify(message));
      } catch {
        clearTimeout(timer);
        this.#pending.delete(id);
        reject(new CdpClosedError());
      }
    });
  }

  onEvent(listener) {
    this.#listeners.add(listener);
  }

  close() {
    this.#shutdown();
    try {
      this.#ws.close();
    } catch {
      // 已经关了
    }
  }
}

// ---------------------------------------------------------------------------------------------
// 页面内探针：唯一会被 Runtime.evaluate 执行的代码。只读 DOM、聚焦、滚动；不发请求、不碰存储。
// 它被 toString() 后送进页面，所以必须自包含，不能引用外面的任何名字。
// ---------------------------------------------------------------------------------------------

function pageProbe(req) {
  const norm = (value) => (value ?? "").replace(/\s+/g, " ").trim();
  const shown = (el) => el.getClientRects().length > 0;
  const pathOf = (anchor) => {
    try {
      const url = new URL(anchor.href, location.href);
      return url.origin === location.origin ? url.pathname : null;
    } catch {
      return null;
    }
  };
  const exact = (selector, text, root = document) =>
    Array.from(root.querySelectorAll(selector)).filter(
      (el) => norm(el.textContent) === text && shown(el),
    );
  const labelled = (text) => {
    const labels = exact("label", text);
    return labels.length === 1 ? (labels[0].control ?? null) : null;
  };
  const linkIn = (root, text, path) =>
    exact("a[href]", text, root).find((anchor) => pathOf(anchor) === path) ?? null;
  const headerLink = (text, path) => {
    for (const header of document.querySelectorAll("header")) {
      const hit = linkIn(header, text, path);
      if (hit) {
        return hit;
      }
    }
    return null;
  };
  const locate = (target) => {
    switch (target.by) {
      case "label":
        return labelled(target.text);
      case "button": {
        const buttons = exact("button", target.text);
        return buttons.length === 1 ? buttons[0] : null;
      }
      case "headerLink":
        return headerLink(target.text, target.path);
      case "rowLink": {
        const links = Array.from(document.querySelectorAll("tbody a[href]")).filter(
          (anchor) =>
            pathOf(anchor) === target.path && anchor.textContent.trim() === target.text && shown(anchor),
        );
        return links.length === 1 ? links[0] : null;
      }
      case "title":
        return (
          Array.from(document.querySelectorAll(target.tag)).find(
            (el) => el.getAttribute("title") === target.text && shown(el),
          ) ?? null
        );
      default:
        return null;
    }
  };

  if (req.op === "point") {
    const el = locate(req.target);
    if (!el) {
      return null;
    }
    el.scrollIntoView({ block: "center", inline: "center" });
    const rect = el.getBoundingClientRect();
    if (rect.width <= 0 || rect.height <= 0) {
      return null;
    }
    const x = rect.left + rect.width / 2;
    const y = rect.top + rect.height / 2;
    const hit = document.elementFromPoint(x, y);
    return { x, y, hit: hit !== null && (hit === el || el.contains(hit)) };
  }

  if (req.op === "focus") {
    const el = locate(req.target);
    if (!el || typeof el.focus !== "function") {
      return false;
    }
    el.focus();
    if (typeof el.select === "function") {
      el.select();
    }
    return document.activeElement === el;
  }

  if (req.op === "valueLength") {
    const el = locate(req.target);
    return el && typeof el.value === "string" ? el.value.length : -1;
  }

  // req.op === "state"：当前页面的一份摘要。
  const elements = document.body ? Array.from(document.body.querySelectorAll("*")) : [];
  const wanted = new Map(Object.entries(req.flags).map(([key, text]) => [text, key]));
  const flags = Object.fromEntries(Object.keys(req.flags).map((key) => [key, false]));
  const totalPattern = new RegExp(req.totalPattern);
  let total = null;
  for (const el of elements) {
    const text = norm(el.textContent);
    const key = wanted.get(text);
    if (key !== undefined && !flags[key] && shown(el)) {
      flags[key] = true;
    }
    if (total === null) {
      const match = totalPattern.exec(text);
      if (match && shown(el)) {
        total = Number.parseInt(match[1], 10);
      }
    }
  }

  const readInt = (raw, fallback, max) => {
    if (raw === null || !/^\d{1,6}$/.test(raw)) {
      return fallback;
    }
    const value = Number.parseInt(raw, 10);
    return value >= 1 && value <= max ? value : fallback;
  };
  const query = new URLSearchParams(location.search);
  const pathname = location.pathname;

  const admin =
    pathname !== req.paths.login &&
    Array.from(document.querySelectorAll("header")).some(
      (header) =>
        exact("button", req.text.navSignOut, header).length > 0 &&
        linkIn(header, req.text.navCustomers, req.paths.customers) !== null,
    );

  let login = null;
  if (pathname === req.paths.login) {
    login = {
      email: labelled(req.text.loginEmail) !== null,
      password: labelled(req.text.loginPassword) !== null,
      submit: exact("button", req.text.loginSubmit).length === 1,
      code: labelled(req.text.loginCode) !== null,
      verify: exact("button", req.text.loginVerify).length === 1,
      alert: Array.from(document.querySelectorAll('[role="alert"]')).some(
        (el) => shown(el) && norm(el.textContent) !== "",
      ),
    };
  }

  let list = null;
  if (pathname === req.paths.customers) {
    for (const table of document.querySelectorAll("table")) {
      const head = Array.from(table.querySelectorAll("thead th")).find(
        (th) => norm(th.textContent) === req.text.companyName,
      );
      if (!head) {
        continue;
      }
      const column = head.cellIndex;
      const rows = Array.from(table.tBodies)
        .flatMap((body) => Array.from(body.rows))
        .filter((tr) => tr.getAttribute("aria-hidden") !== "true");
      const matches = [];
      // 这一页有哪些行，只留一个指纹（FNV-1a），用来判断翻页后数据换了没有；别的客户的信息不带出页面。
      let hash = 0x811c9dc5;
      for (const tr of rows) {
        const cell = tr.cells[column];
        const link = cell ? cell.querySelector("a[href]") : null;
        if (!link) {
          continue;
        }
        const path = pathOf(link) ?? "";
        for (let i = 0; i < path.length; i += 1) {
          hash = Math.imul(hash ^ path.charCodeAt(i), 0x01000193) >>> 0;
        }
        hash = Math.imul(hash ^ 0x0a, 0x01000193) >>> 0;
        const name = link.textContent.trim();
        if (name.startsWith(req.fixturePrefix)) {
          matches.push({ name, path });
        }
      }
      const next = locate({ by: "title", tag: "li", text: req.text.nextPage });
      list = {
        matches,
        signature: `${rows.length}:${hash}`,
        nextPresent: next !== null,
        nextDisabled: next === null || next.getAttribute("aria-disabled") === "true",
      };
      break;
    }
  }

  let detail = null;
  if (new RegExp(req.detailPattern).test(pathname)) {
    // Descriptions 的一项：标签所在格的下一个兄弟就是值（bordered 为 th→td；非 bordered 为 label→content）。
    const rowValues = (label) => {
      const values = [];
      for (const el of elements) {
        if (el.closest("thead") || norm(el.textContent) !== label) {
          continue;
        }
        if (Array.from(el.children).some((child) => norm(child.textContent) === label)) {
          continue;
        }
        const cell = el.closest("th, td");
        const content =
          cell && norm(cell.textContent) === label ? cell.nextElementSibling : el.nextElementSibling;
        if (content && shown(content)) {
          values.push(content.textContent.trim());
        }
      }
      return values;
    };
    detail = {
      companyNames: rowValues(req.text.companyName),
      balances: rowValues(req.text.walletBalance),
    };
  }

  return {
    origin: location.origin,
    pathname,
    page: readInt(query.get("page"), 1, req.maxPage),
    pageSize: readInt(query.get("page_size"), req.defaultPageSize, req.maxPageSize),
    admin,
    busy: document.querySelector('[aria-busy="true"]') !== null,
    total,
    flags,
    login,
    list,
    detail,
  };
}

// ---------------------------------------------------------------------------------------------
// 会话：连已有页面目标，被动观察网络
// ---------------------------------------------------------------------------------------------

let input = null;
let siteOrigin = null;
let mainFrameId = null;
let networkSeq = 0;
/** loaderId → 主文档响应（只留状态码与 origin）。 */
const documentResponses = new Map();
/** requestId → 详情 GET 的观察结果（不留 URL）。 */
const detailRequests = new Map();

const state = {
  /** 列表里找到的夹具：{ name, path, id }。 */
  fixture: null,
  /** 点进详情之前的网络序号：只认这之后发出的详情 GET。 */
  detailMarker: null,
};

function originOf(url) {
  try {
    return new URL(url).origin;
  } catch {
    return null;
  }
}

function customerIdFromApiUrl(url) {
  let parsed = null;
  try {
    parsed = new URL(url);
  } catch {
    return null;
  }
  if (parsed.origin !== siteOrigin) {
    return null;
  }
  const match = CUSTOMER_API_PATH.exec(parsed.pathname);
  if (match === null) {
    return null;
  }
  try {
    return decodeURIComponent(match[1]);
  } catch {
    return null;
  }
}

function onPageEvent(method, params) {
  if (method === "Network.requestWillBeSent") {
    if (params.request?.method !== "GET") {
      return;
    }
    const customerId = customerIdFromApiUrl(params.request.url);
    if (customerId !== null) {
      networkSeq += 1;
      detailRequests.set(params.requestId, {
        requestId: params.requestId,
        customerId,
        seq: networkSeq,
        status: null,
        finished: false,
        failed: false,
      });
    }
    return;
  }
  if (method === "Network.responseReceived") {
    if (params.type === "Document" && params.frameId === mainFrameId && params.loaderId) {
      documentResponses.set(params.loaderId, {
        status: params.response?.status,
        origin: originOf(params.response?.url ?? ""),
      });
    }
    const request = detailRequests.get(params.requestId);
    if (request !== undefined) {
      request.status = params.response?.status ?? null;
    }
    return;
  }
  if (method === "Network.loadingFinished") {
    const request = detailRequests.get(params.requestId);
    if (request !== undefined) {
      request.finished = true;
    }
    return;
  }
  if (method === "Network.loadingFailed") {
    const request = detailRequests.get(params.requestId);
    if (request !== undefined) {
      request.failed = true;
    }
  }
}

/**
 * 页面目标的 URL → 站点 origin（https、合法主机名、无端口、无账号）；不合格返回 null。
 * 站点 origin 只从 Worker 已打开的页面目标来，不读文件、不另收输入。
 */
function siteOriginOf(url) {
  let parsed = null;
  try {
    parsed = new URL(url);
  } catch {
    return null;
  }
  if (
    parsed.protocol !== SITE_PROTOCOL ||
    parsed.port !== "" ||
    parsed.username !== "" ||
    parsed.password !== "" ||
    !HOSTNAME.test(parsed.hostname)
  ) {
    return null;
  }
  return parsed.origin;
}

/** 第一个真正要动浏览器的步骤才连；连不上就是这个步骤 navigation_failed。 */
async function openSession() {
  if (session !== null) {
    return session;
  }
  try {
    if (typeof WebSocket !== "function") {
      throw new Error("no WebSocket");
    }
    const cdp = await CdpConnection.open(input.cdp);
    let sessionId;
    if (!input.pageEndpoint) {
      // 浏览器级地址：Worker 已打开的 https 站点页面目标。不同 origin 不止一个时不猜，直接失败。
      const { targetInfos = [] } = await cdp.send("Target.getTargets", {});
      const pages = targetInfos.filter(
        (target) =>
          target.type === "page" && typeof target.url === "string" && siteOriginOf(target.url) !== null,
      );
      const origins = new Set(pages.map((target) => siteOriginOf(target.url)));
      if (origins.size !== 1) {
        cdp.close();
        throw new Error("no site target");
      }
      const target = pages[0];
      siteOrigin = siteOriginOf(target.url);
      ({ sessionId } = await cdp.send("Target.attachToTarget", {
        targetId: target.targetId,
        flatten: true,
      }));
    }
    cdp.onEvent((method, params, eventSessionId) => {
      if (eventSessionId === sessionId) {
        onPageEvent(method, params);
      }
    });
    const page = { send: (method, params) => cdp.send(method, params ?? {}, sessionId) };
    if (siteOrigin === null) {
      // 页面级地址：这个页面本身就是 Worker 打开的站点页；只读 location.href，不发请求。
      const { result, exceptionDetails } = await page.send("Runtime.evaluate", {
        expression: "location.href",
        returnByValue: true,
      });
      siteOrigin = exceptionDetails ? null : siteOriginOf(result?.value);
      if (siteOrigin === null) {
        cdp.close();
        throw new Error("no site origin");
      }
    }
    const { frameTree } = await page.send("Page.getFrameTree");
    mainFrameId = frameTree?.frame?.id ?? null;
    await page.send("Network.enable", {});
    session = { cdp, page, sessionId };
    return session;
  } catch {
    throw new StepFailure(CODE.NAVIGATION_FAILED);
  }
}

async function closeSession() {
  if (session === null) {
    return;
  }
  const { cdp, sessionId } = session;
  session = null;
  if (sessionId !== undefined) {
    await cdp.send("Target.detachFromTarget", { sessionId }).catch(() => undefined);
  }
  cdp.close();
}

// ---------------------------------------------------------------------------------------------
// 页面操作
// ---------------------------------------------------------------------------------------------

async function evaluate(request) {
  const result = await session.page.send("Runtime.evaluate", {
    expression: `(${pageProbe.toString()})(${JSON.stringify(request)})`,
    returnByValue: true,
    awaitPromise: false,
  });
  if (result.exceptionDetails || result.result === undefined) {
    throw new Error("evaluate failed");
  }
  return result.result.value;
}

const TOTAL_TEMPLATE_PARTS = TEXT.listTotalTemplate.split("{{total}}");
const escapeRegExp = (text) => text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

const PROBE_BASE = Object.freeze({
  paths: PATHS,
  text: { ...TEXT, nextPage: ANTD_TEXT.nextPage },
  fixturePrefix: FIXTURE_PREFIX,
  detailPattern: CUSTOMER_DETAIL_PATH.source,
  totalPattern: `^${escapeRegExp(TOTAL_TEMPLATE_PARTS[0])}(\\d+)${escapeRegExp(TOTAL_TEMPLATE_PARTS[1] ?? "")}$`,
  defaultPageSize: DEFAULT_PAGE_SIZE,
  maxPageSize: MAX_PAGE_SIZE,
  maxPage: MAX_PAGE,
  flags: {
    appErrorTitle: TEXT.appErrorTitle,
    notFoundTitle: TEXT.notFoundTitle,
    forbidden: TEXT.forbidden,
    enrolIntro: TEXT.enrolIntro,
    enrolConfirm: TEXT.enrolConfirm,
    listLoading: TEXT.listLoading,
    listLoadFailed: TEXT.listLoadFailed,
    listEmpty: TEXT.listEmpty,
    detailLoading: TEXT.detailLoading,
    detailLoadFailed: TEXT.detailLoadFailed,
    detailNotFound: TEXT.detailNotFound,
  },
});

function snapshot() {
  return evaluate({ ...PROBE_BASE, op: "state" });
}

async function snapshotOrNull() {
  try {
    return await snapshot();
  } catch {
    return null;
  }
}

/**
 * 每次读到页面摘要都先过一遍。不在站点上返回 false（让调用方接着等 —— 刚导航时 evaluate 可能还落在旧文档里，
 * 真离开了站点就会等到超时）；整页报错、落到 404 直接失败。
 */
function onSite(snap) {
  if (snap.origin !== siteOrigin) {
    return false;
  }
  for (const [flag, code] of Object.entries(PAGE_FAILURE_TEXTS)) {
    if (snap.flags[flag]) {
      throw new StepFailure(code);
    }
  }
  return true;
}

/**
 * 轮询到 check 给出结果（非 undefined）为止，有上限。check 抛 StepFailure 即确定失败；
 * 其它异常（页面正在跳转、执行上下文刚换掉）当作「还没好」继续等，连接断了则直接失败。
 */
async function waitFor(check, timeoutMs, timeoutCode) {
  const until = Math.min(Date.now() + timeoutMs, budgetEnd);
  for (;;) {
    try {
      const outcome = await check();
      if (outcome !== undefined) {
        return outcome;
      }
    } catch (error) {
      if (error instanceof StepFailure || error instanceof CdpClosedError) {
        throw error;
      }
    }
    if (Date.now() >= until) {
      throw new StepFailure(typeof timeoutCode === "function" ? timeoutCode() : timeoutCode);
    }
    await sleep(POLL_INTERVAL_MS);
  }
}

/** 打开站点上的一个路径，并按主文档的 HTTP 响应判定能不能继续。 */
async function navigate(path) {
  let result = null;
  try {
    result = await session.page.send("Page.navigate", { url: `${siteOrigin}${path}` });
  } catch (error) {
    if (error instanceof CdpClosedError) {
      throw error;
    }
    throw new StepFailure(CODE.NAVIGATION_FAILED);
  }
  const loaderId = result.loaderId;
  if (!loaderId && !result.errorText) {
    throw new StepFailure(CODE.NAVIGATION_FAILED);
  }
  const limit = result.errorText ? DOCUMENT_RESPONSE_GRACE_MS : DOCUMENT_RESPONSE_TIMEOUT_MS;
  const until = Math.min(Date.now() + limit, budgetEnd);
  let response = loaderId ? documentResponses.get(loaderId) : undefined;
  while (response === undefined && loaderId && Date.now() < until) {
    await sleep(100);
    response = documentResponses.get(loaderId);
  }
  if (response === undefined) {
    // 主文档完全没有得到 HTTP 响应。
    throw new StepFailure(CODE.HOST_UNREACHABLE);
  }
  if (response.status === 502 || response.status === 503 || response.status === 504) {
    throw new StepFailure(CODE.SERVICE_UNAVAILABLE);
  }
  if (typeof response.status !== "number" || response.status < 200 || response.status >= 400) {
    throw new StepFailure(CODE.NAVIGATION_FAILED);
  }
  if (response.origin !== siteOrigin) {
    throw new StepFailure(CODE.NAVIGATION_FAILED);
  }
}

function isWriteAction(text) {
  return WRITE_ACTION_TEXTS.has(text) || WRITE_ACTION_PATTERNS.some((pattern) => pattern.test(text));
}

async function mouse(type, point, extra) {
  await session.page.send("Input.dispatchMouseEvent", { type, x: point.x, y: point.y, ...extra });
}

/** 真点击：滚到元素、确认那一点落在它身上，再发鼠标事件。 */
async function click(target, missingCode) {
  if (isWriteAction(target.text)) {
    throw new StepFailure(CODE.ASSERTION_FAILED);
  }
  const point = await waitFor(
    async () => {
      const found = await evaluate({ ...PROBE_BASE, op: "point", target });
      return found && found.hit ? found : undefined;
    },
    ELEMENT_TIMEOUT_MS,
    missingCode,
  );
  await mouse("mouseMoved", point, { button: "none", buttons: 0 });
  await mouse("mousePressed", point, { button: "left", buttons: 1, clickCount: 1 });
  await mouse("mouseReleased", point, { button: "left", buttons: 0, clickCount: 1 });
}

/**
 * 往登录表单的输入框里输入。文字只经 Input.insertText 进页面；事后只核对字符数，不回读内容。
 */
async function typeInto(target, text, failCode) {
  const focused = await evaluate({ ...PROBE_BASE, op: "focus", target });
  if (focused !== true) {
    throw new StepFailure(CODE.ELEMENT_MISSING);
  }
  await session.page.send("Input.insertText", { text });
  const length = await evaluate({ ...PROBE_BASE, op: "valueLength", target });
  if (length !== text.length) {
    throw new StepFailure(failCode);
  }
}

// ---------------------------------------------------------------------------------------------
// 定位目标（文案见 TEXT / ANTD_TEXT）
// ---------------------------------------------------------------------------------------------

const TARGET = Object.freeze({
  email: { by: "label", text: TEXT.loginEmail },
  password: { by: "label", text: TEXT.loginPassword },
  submit: { by: "button", text: TEXT.loginSubmit },
  code: { by: "label", text: TEXT.loginCode },
  verify: { by: "button", text: TEXT.loginVerify },
  navCustomers: { by: "headerLink", text: TEXT.navCustomers, path: PATHS.customers },
  nextPage: { by: "title", tag: "li", text: ANTD_TEXT.nextPage },
});

// ---------------------------------------------------------------------------------------------
// 步骤
// ---------------------------------------------------------------------------------------------

async function stepLogin() {
  await openSession();
  await navigate(PATHS.login);
  await waitFor(
    async () => {
      const snap = await snapshot();
      if (!onSite(snap)) {
        return undefined;
      }
      const form = snap.login;
      return form && form.email && form.password && form.submit ? true : undefined;
    },
    ELEMENT_TIMEOUT_MS,
    CODE.ELEMENT_MISSING,
  );

  if (input.attempt > 1) {
    await waitForNextTotpWindow();
  }

  await typeInto(TARGET.email, input.email, CODE.LOGIN_FAILED);
  await typeInto(TARGET.password, input.password, CODE.LOGIN_FAILED);
  await click(TARGET.submit, CODE.ELEMENT_MISSING);

  const next = await waitFor(
    async () => {
      const snap = await snapshot();
      if (!onSite(snap)) {
        return undefined;
      }
      if (snap.admin) {
        return "admin";
      }
      // 要求注册 2FA = 夹具账号没配好；注册是写操作，不做。
      if (snap.flags.enrolIntro || snap.flags.enrolConfirm) {
        throw new StepFailure(CODE.LOGIN_FAILED);
      }
      if (snap.login?.alert) {
        throw new StepFailure(CODE.LOGIN_FAILED);
      }
      return snap.login?.code && snap.login.verify ? "totp" : undefined;
    },
    LOGIN_RESULT_TIMEOUT_MS,
    CODE.LOGIN_FAILED,
  );

  if (next === "totp") {
    await avoidTotpWindowEdge();
    const code = totpCode(input.totpKey, Date.now());
    await typeInto(TARGET.code, code, CODE.LOGIN_FAILED);
    await click(TARGET.verify, CODE.ELEMENT_MISSING);
    await waitFor(
      async () => {
        const snap = await snapshot();
        if (!onSite(snap)) {
          return undefined;
        }
        if (snap.admin) {
          return true;
        }
        if (snap.login?.alert) {
          throw new StepFailure(CODE.LOGIN_FAILED);
        }
        return undefined;
      },
      LOGIN_RESULT_TIMEOUT_MS,
      CODE.LOGIN_FAILED,
    );
  }

  // 以离开登录页、出现管理端布局为准。
  const final = await snapshot();
  if (!onSite(final) || !final.admin || final.pathname === PATHS.login) {
    throw new StepFailure(CODE.LOGIN_FAILED);
  }
}

/** 已在站点的管理端里就不动；否则打开 /customers，靠浏览器里已有的会话进管理端。 */
async function ensureAdmin() {
  await openSession();
  const current = await snapshotOrNull();
  if (current !== null && current.origin === siteOrigin && current.admin) {
    return;
  }
  await navigate(PATHS.customers);
  await waitFor(
    async () => {
      const snap = await snapshot();
      if (!onSite(snap)) {
        return undefined;
      }
      if (snap.admin) {
        return true;
      }
      if (snap.pathname === PATHS.login) {
        throw new StepFailure(CODE.LOGIN_FAILED);
      }
      return undefined;
    },
    ELEMENT_TIMEOUT_MS,
    CODE.ELEMENT_MISSING,
  );
}

/** 客户列表加载完成、停在第 expectedPage 页、且（翻页时）数据已经换成新的一页。 */
function listLoaded(expectedPage, previousSignature) {
  return async () => {
    const snap = await snapshot();
    if (!onSite(snap)) {
      return undefined;
    }
    if (!snap.admin) {
      if (snap.pathname === PATHS.login) {
        throw new StepFailure(CODE.LOGIN_FAILED);
      }
      return undefined;
    }
    if (snap.pathname !== PATHS.customers) {
      return undefined;
    }
    if (snap.flags.listLoadFailed || snap.flags.forbidden) {
      throw new StepFailure(CODE.PAGE_ERROR);
    }
    if (snap.flags.listEmpty) {
      // 一个客户都没有：夹具不在。
      throw new StepFailure(CODE.ASSERTION_FAILED);
    }
    if (snap.list === null || snap.busy || snap.flags.listLoading) {
      return undefined;
    }
    if (snap.page !== expectedPage) {
      return undefined;
    }
    if (previousSignature !== null && snap.list.signature === previousSignature) {
      return undefined;
    }
    return snap;
  };
}

function hasNextPage(snap) {
  if (!snap.list.nextPresent || snap.list.nextDisabled) {
    return false;
  }
  return snap.total === null || snap.page * snap.pageSize < snap.total;
}

async function findFixtureRow() {
  await click(TARGET.navCustomers, CODE.ELEMENT_MISSING);
  let snap = await waitFor(listLoaded(1, null), LIST_TIMEOUT_MS, CODE.ELEMENT_MISSING);

  for (let visited = 1; visited <= MAX_LIST_PAGES; visited += 1) {
    const { matches } = snap.list;
    if (matches.length > 1) {
      // 认不出哪一个才是夹具。
      throw new StepFailure(CODE.ASSERTION_FAILED);
    }
    if (matches.length === 1) {
      const [row] = matches;
      const match = CUSTOMER_DETAIL_PATH.exec(row.path ?? "");
      if (match === null || match[1] === CUSTOMER_CREATE_SEGMENT || !row.name.startsWith(FIXTURE_PREFIX)) {
        throw new StepFailure(CODE.ASSERTION_FAILED);
      }
      let id = null;
      try {
        id = decodeURIComponent(match[1]);
      } catch {
        throw new StepFailure(CODE.ASSERTION_FAILED);
      }
      state.fixture = { name: row.name, path: row.path, id };
      return snap;
    }
    if (!hasNextPage(snap)) {
      // 翻完了也没有。
      throw new StepFailure(CODE.ASSERTION_FAILED);
    }
    const page = snap.page;
    const signature = snap.list.signature;
    await click(TARGET.nextPage, CODE.ELEMENT_MISSING);
    snap = await waitFor(listLoaded(page + 1, signature), LIST_TIMEOUT_MS, CODE.ELEMENT_MISSING);
  }
  throw new StepFailure(CODE.ASSERTION_FAILED);
}

async function stepOpenCustomers() {
  await ensureAdmin();
  await findFixtureRow();
}

function detailLoaded(fixture) {
  return async () => {
    const snap = await snapshot();
    if (!onSite(snap)) {
      return undefined;
    }
    if (!snap.admin) {
      if (snap.pathname === PATHS.login) {
        throw new StepFailure(CODE.LOGIN_FAILED);
      }
      return undefined;
    }
    if (snap.pathname !== fixture.path || snap.detail === null) {
      return undefined;
    }
    if (snap.flags.detailLoadFailed || snap.flags.forbidden) {
      throw new StepFailure(CODE.PAGE_ERROR);
    }
    if (snap.flags.detailNotFound) {
      throw new StepFailure(CODE.ASSERTION_FAILED);
    }
    if (snap.flags.detailLoading || snap.detail.companyNames.length === 0) {
      return undefined;
    }
    return snap;
  };
}

/** 从列表点进夹具详情，核对公司名。open_fixture 与缺前提的 check_balance 共用。 */
async function openFixture() {
  const current = await snapshotOrNull();
  const rowOnScreen =
    state.fixture !== null &&
    current !== null &&
    current.origin === siteOrigin &&
    current.admin &&
    current.pathname === PATHS.customers &&
    current.list !== null &&
    current.list.matches.length === 1 &&
    current.list.matches[0].path === state.fixture.path;
  if (!rowOnScreen) {
    await ensureAdmin();
    await findFixtureRow();
  }
  const fixture = state.fixture;

  // 只认点击之后发出的详情 GET。
  state.detailMarker = networkSeq;
  await click({ by: "rowLink", path: fixture.path, text: fixture.name }, CODE.ELEMENT_MISSING);
  const snap = await waitFor(detailLoaded(fixture), DETAIL_TIMEOUT_MS, CODE.ELEMENT_MISSING);
  const names = snap.detail.companyNames;
  if (names.length !== 1 || names[0] !== fixture.name || !names[0].startsWith(FIXTURE_PREFIX)) {
    throw new StepFailure(CODE.ASSERTION_FAILED);
  }
}

async function stepOpenFixture() {
  await openSession();
  await openFixture();
}

/** 标记之后、这个客户的、200 且已收完的最近一次详情 GET。 */
function latestDetailResponse(customerId, marker) {
  let latest = null;
  for (const request of detailRequests.values()) {
    if (
      request.customerId === customerId &&
      request.seq > marker &&
      request.status === 200 &&
      request.finished &&
      !request.failed &&
      (latest === null || request.seq > latest.seq)
    ) {
      latest = request;
    }
  }
  return latest;
}

/** 从响应信封（docs/api.md、frontend/src/api/client.ts 的 ApiEnvelope）里取 wallet，算出页面应显示的文字。 */
async function expectedBalanceText(request, fixture) {
  let result = null;
  try {
    result = await session.page.send("Network.getResponseBody", { requestId: request.requestId });
  } catch (error) {
    if (error instanceof CdpClosedError) {
      throw error;
    }
    return null;
  }
  let envelope = null;
  try {
    const text = result.base64Encoded
      ? Buffer.from(result.body ?? "", "base64").toString("utf8")
      : (result.body ?? "");
    envelope = JSON.parse(text);
  } catch {
    throw new StepFailure(CODE.ASSERTION_FAILED);
  }
  const data = isPlainObject(envelope) && envelope.success === true ? envelope.data : null;
  const wallet = isPlainObject(data) ? data.wallet : null;
  if (
    !isPlainObject(wallet) ||
    typeof wallet.balance !== "string" ||
    !isNonEmptyString(wallet.currency) ||
    data.id !== fixture.id ||
    data.company_name !== fixture.name
  ) {
    throw new StepFailure(CODE.ASSERTION_FAILED);
  }
  return formatMoney(wallet.balance, wallet.currency);
}

async function stepCheckBalance() {
  await openSession();
  const current = await snapshotOrNull();
  const onDetail =
    state.fixture !== null &&
    state.detailMarker !== null &&
    current !== null &&
    current.origin === siteOrigin &&
    current.admin &&
    current.pathname === state.fixture.path;
  if (!onDetail) {
    await openFixture();
  }
  const fixture = state.fixture;

  let usedRequest = null;
  let expected = null;
  let timeoutCode = CODE.ASSERTION_FAILED; // 没观察到响应，或文字对不上
  await waitFor(
    async () => {
      const request = latestDetailResponse(fixture.id, state.detailMarker);
      if (request !== null && request !== usedRequest) {
        usedRequest = request;
        expected = await expectedBalanceText(request, fixture);
      }
      if (expected === null) {
        timeoutCode = CODE.ASSERTION_FAILED;
        return undefined;
      }
      const snap = await snapshot();
      if (!onSite(snap)) {
        timeoutCode = CODE.NAVIGATION_FAILED;
        return undefined;
      }
      if (snap.pathname !== fixture.path || snap.detail === null) {
        throw new StepFailure(CODE.NAVIGATION_FAILED);
      }
      if (snap.flags.detailLoadFailed || snap.flags.forbidden) {
        throw new StepFailure(CODE.PAGE_ERROR);
      }
      const { balances } = snap.detail;
      if (balances.length !== 1) {
        timeoutCode = CODE.ELEMENT_MISSING;
        return undefined;
      }
      if (balances[0] === expected) {
        return true;
      }
      timeoutCode = CODE.ASSERTION_FAILED;
      return undefined;
    },
    BALANCE_TIMEOUT_MS,
    () => timeoutCode,
  );
}

const STEPS = new Map([
  ["login", stepLogin],
  ["open_customers", stepOpenCustomers],
  ["open_fixture", stepOpenFixture],
  ["check_balance", stepCheckBalance],
]);

// ---------------------------------------------------------------------------------------------
// 入口
// ---------------------------------------------------------------------------------------------

async function main() {
  const line = await readInputLine();
  const parsed = parseInput(line);
  if (!parsed.ok) {
    await finish(failLine(parsed.firstStep, CODE.LOGIN_FAILED), 1);
    return;
  }
  input = parsed;

  for (const step of input.steps) {
    currentStep = step;
    const run = STEPS.get(step);
    if (run === undefined) {
      await finish(failLine(step, CODE.ASSERTION_FAILED), 1);
      return;
    }
    try {
      await run();
    } catch (error) {
      await finish(failLine(step, error instanceof StepFailure ? error.code : CODE.PAGE_ERROR), 1);
      return;
    }
    if (finished) {
      return;
    }
    process.stdout.write(`${STEP_PREFIX} ${step} ok\n`);
  }
  await finish(`${VERDICT_PREFIX} PASS`, 0);
}

main().catch(() => void finish(failLine(currentStep, CODE.PAGE_ERROR), 1));

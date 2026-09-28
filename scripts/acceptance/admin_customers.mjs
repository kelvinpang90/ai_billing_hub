/**
 * 部署后浏览器验收：管理端客户页（AIH-TASK-015），本任务为 AIH-TASK-024。
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
 *   HMAC-SHA1、30 秒步长、6 位），base32 解码在本文件里自己写。
 * - 只读：只点「Continue」「Verify」（登录）、顶栏「Customers」、列表的「Next Page」（翻页）和夹具客户的公司名
 *   链接。不点新建客户、编辑 / 保存、手工调账、建凭据、轮换、吊销等任何会写数据的按钮；只打开公司名以
 *   `[TEST] Acceptance Fixture` 开头的夹具客户的详情。登录会在服务端留下会话与登录审计，这是只读验收不可避免的，
 *   不算写数据。若账号走到「首次启用 2FA」那一步，那会写数据，直接 login_failed，不继续。
 * - 每个步骤都真的检查它声称的东西；等待一律有上限（见下面的常量），总时长控制在 timeout_seconds（300 秒）内。
 * - 找元素只按可见文字（文案取自 frontend/src/i18n/locales/en.json）或语义结构（label→control、header、
 *   table/thead/th、th→td、role、title 属性），不依赖 antd 生成的类名哈希。文案与选择器集中在下面的常量里。
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
  // antd 分页「下一页」那个 <li> 的 title。不在 en.json 里：来自 antd 自带的 enUS 语言包
  // （frontend/src/App.tsx 的 <ConfigProvider locale={enUS}>）。
  nextPageTitle: "Next Page",
  // 路由路径，取自 frontend/src/routes/paths.ts 的 ROUTES。
  loginPath: "/login",
  customersPath: "/customers",
  customerCreatePath: "/customers/new",
};

/** 夹具客户的公司名前缀。只看、只打开以它开头的客户。 */
const FIXTURE_PREFIX = "[TEST] Acceptance Fixture";

/** 详情接口的路径（frontend/src/api/adminCustomers.ts 的 CUSTOMERS_URL + customerUrl）。 */
const CUSTOMER_DETAIL_API = /^\/api\/v1\/admin\/customers\/([^/]+)$/;

/** 列表里夹具那一行链接的 href（frontend/src/routes/paths.ts 的 customerDetailPath）。 */
const CUSTOMER_DETAIL_HREF = /^\/customers\/([^/?#]+)$/;

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
  seq: 0,
};

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
        break;
      }
      case "Network.responseReceived": {
        const status = typeof params.response?.status === "number" ? params.response.status : null;
        if (params.type === "Document" && typeof params.loaderId === "string") {
          network.documents.set(params.loaderId, status);
          network.lastDocumentStatus = status;
        }
        const detail = network.details.get(params.requestId);
        if (detail !== undefined) {
          detail.status = status;
        }
        break;
      }
      case "Network.loadingFinished": {
        const detail = network.details.get(params.requestId);
        if (detail !== undefined) {
          detail.finished = true;
        }
        break;
      }
      case "Network.loadingFailed": {
        const detail = network.details.get(params.requestId);
        if (detail !== undefined) {
          detail.failed = true;
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
  return { norm, leaf, control, button, alert, layout, valueCells, common };
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
 */
async function typeInto(labelText, value) {
  const focused = await waitFor(() => evaluate(focusField, labelText), (v) => v === true, ELEMENT_WAIT_MS);
  if (!focused.ok) {
    throw fail("element_missing");
  }
  const text = typeof value === "function" ? await value() : value;
  try {
    await cdp.send("Input.insertText", { text }, sessionId);
  } catch {
    throw fail("login_failed");
  }
  if ((await evaluate(fieldLength, labelText)) !== text.length) {
    throw fail("login_failed");
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

const STEPS = {
  login: stepLogin,
  open_customers: stepOpenCustomers,
  open_fixture: stepOpenFixture,
  check_balance: stepCheckBalance,
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

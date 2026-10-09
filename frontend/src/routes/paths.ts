import { generatePath } from "react-router";

/**
 * 路径字面量只在这里出现一次。
 *
 * 散在各处的话，改一个路由要靠全文搜索，而搜漏的那一处是一个**编译期发现不了**
 * 的死链接 —— `<Link to="/dashbaord">` 类型完全合法。
 */
export const ROUTES = {
  dashboard: "/",
  login: "/login",
  forgotPassword: "/forgot-password",
  /**
   * ⚠️ 这一条**不只是前端的事**：重置邮件里的链接由后端拼（`app/tasks/outbox.py`
   * 的 `_render_password_reset`），那边硬编码着同一个路径。两处对不上时的失败方式是
   * 「信发出去了、用户点进来是 404」—— 前端测试与后端测试各自全绿，谁也发现不了。
   * 所以由 `tests/backend/test_password_reset_link.py` 机械比对这两处。
   */
  resetPassword: "/reset-password",
  customers: "/customers",
  /** 静态段比 `:customerId` 优先匹配（react-router 按具体程度排序），所以 `new` 不会被当成 id。 */
  customerCreate: "/customers/new",
  customerDetail: "/customers/:customerId",
  /**
   * 管理端审计日志（AIH-TASK-023）。部署后浏览器验收脚本
   * `scripts/acceptance/admin_customers.mjs` 的 `open_audit` 按这个 href 找顶栏链接，改了要同步那里。
   */
  audit: "/audit",
  /**
   * 管理端 AI 目录（AIH-TASK-035）。顶栏的目录项指向供应商页；验收脚本
   * `scripts/acceptance/admin_customers.mjs` 的 `open_catalog` / `check_meter_types` 按这几个 href 找链接，
   * 改了要同步那里。
   */
  catalogProviders: "/catalog/providers",
  catalogProviderDetail: "/catalog/providers/:providerId",
  catalogMeterTypes: "/catalog/meter-types",
} as const;

/**
 * 客户详情页的地址。用 `generatePath` 填参数，不手拼字符串。
 *
 * ⚠️ `generatePath` 不做 URL 转义。这里的 id 只来自后端（uuid 的 `public_id`），
 * 不会含 `/`；换成用户输入的值之前先想清楚这一点。
 */
export function customerDetailPath(customerId: string): string {
  return generatePath(ROUTES.customerDetail, { customerId });
}

/** 供应商详情页的地址。id 只来自后端（uuid 的 `public_id`），理由同 {@link customerDetailPath}。 */
export function catalogProviderDetailPath(providerId: string): string {
  return generatePath(ROUTES.catalogProviderDetail, { providerId });
}

/** 重置链接里装令牌的查询参数名。后端那一处由同一条用例比对。 */
export const RESET_TOKEN_PARAM = "token";

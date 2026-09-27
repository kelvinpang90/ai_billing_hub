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
  /** 静态段 `new` 比 `:customerId` 优先匹配，所以它不会被当成一个客户 id。 */
  customerNew: "/customers/new",
  customerDetail: "/customers/:customerId",
} as const;

/**
 * 某个客户的详情页。
 *
 * ⚠️ 自己 `encodeURIComponent`：`generatePath` 只做替换、不保证转义，而一个带 `/`
 * 的值会悄悄变成另一条路由。
 */
export function customerDetailPath(customerId: string): string {
  return generatePath(ROUTES.customerDetail, { customerId: encodeURIComponent(customerId) });
}

/** 列表页的查询参数名（分页状态放在地址里，从详情页返回时还在原来那一页）。 */
export const PAGE_PARAM = "page";
export const PAGE_SIZE_PARAM = "page_size";

/** 重置链接里装令牌的查询参数名。后端那一处由同一条用例比对。 */
export const RESET_TOKEN_PARAM = "token";

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
} as const;

/** 重置链接里装令牌的查询参数名。后端那一处由同一条用例比对。 */
export const RESET_TOKEN_PARAM = "token";

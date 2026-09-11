/**
 * 路径字面量只在这里出现一次。
 *
 * 散在各处的话，改一个路由要靠全文搜索，而搜漏的那一处是一个**编译期发现不了**
 * 的死链接 —— `<Link to="/dashbaord">` 类型完全合法。
 */
export const ROUTES = {
  dashboard: "/",
} as const;

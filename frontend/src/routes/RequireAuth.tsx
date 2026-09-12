/**
 * Send anonymous visitors to the login page.
 *
 * ⚠️ **这不是访问控制。**spec §51：「Backend MUST independently enforce
 * authorization. Never rely only on frontend menu hiding.」——它只决定「该画
 * 登录页还是后台」。绕过它什么也拿不到：每个业务端点都自己验令牌。
 */

import { Spin } from "antd";
import { Navigate, Outlet, useLocation } from "react-router";

import { useAuth } from "../auth/AuthProvider";
import { ROUTES } from "./paths";

export function RequireAuth() {
  const { status } = useAuth();
  const location = useLocation();

  if (status === "unknown") {
    // ⚠️ 这一支不能省。页面刚加载时访问令牌还在静默换取中（它只在内存里，
    // 刷新必然丢），此刻当成未登录会把人直接踢到登录页 —— 表现就是
    // 「每按一次 F5 都要重新登录」。
    return (
      <div style={{ display: "flex", justifyContent: "center", paddingTop: 96 }}>
        <Spin size="large" />
      </div>
    );
  }

  if (status === "anonymous") {
    // 记下他本来想去哪，登录后送回去。`replace` 是为了让浏览器的「后退」
    // 不会退回到一个刚刚被拒绝的页面。
    return <Navigate to={ROUTES.login} replace state={{ from: location.pathname }} />;
  }

  return <Outlet />;
}

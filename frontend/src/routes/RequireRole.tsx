/**
 * 按 `GET /api/v1/me` 的角色决定画管理端还是客户门户（AIH-TASK-096，设计 AIH-TASK-089 §11 任务 E）。
 *
 * 只放在 `RequireAuth` **里面**用：走到这里时一定已经有访问令牌。角色对就放行；ADMIN 走到门户被转回管理端首页，
 * CUSTOMER 走到管理端被转到门户首页。
 *
 * ⚠️ **这不是访问控制。**spec §51 要求后端独立鉴权：CUSTOMER 调 `/api/v1/admin/*` 一律 403 `ADMIN_REQUIRED`，
 * ADMIN 调 `/api/v1/customer/*` 一律 403 `CUSTOMER_REQUIRED`（docs/api.md「客户请求的约定」）。这里只决定画哪一套外壳。
 *
 * ⚠️ 但它也**不默认放行**：`/api/v1/me` 失败、或回来一个两者都不是的角色时，画错误态，不画任何一边的页面。
 * 猜错的代价是把客户放进一整套管理端菜单里看一排 403。
 */

import { useQuery } from "@tanstack/react-query";
import { Alert, Button, Space, Spin } from "antd";
import type { ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { Navigate, Outlet } from "react-router";

import { ME_QUERY_KEY, ROLE_ADMIN, ROLE_CUSTOMER, getMe, type Role } from "../api/auth";
import { useAuth } from "../auth/AuthProvider";
import { RequestReference } from "../components/RequestReference";
import { ROUTES } from "./paths";

/** 每个角色自己的首页：走错了外壳就转到这里。 */
const HOME_OF: Record<Role, string> = {
  [ROLE_ADMIN]: ROUTES.dashboard,
  [ROLE_CUSTOMER]: ROUTES.portalHome,
};

function isRole(value: string): value is Role {
  return value === ROLE_ADMIN || value === ROLE_CUSTOMER;
}

export function RequireRole({ role }: { role: Role }) {
  const { t } = useTranslation();
  const { signOut } = useAuth();
  const me = useQuery({
    queryKey: ME_QUERY_KEY,
    queryFn: ({ signal }) => getMe(signal),
    // ⚠️ 没有观察者就立刻丢掉。退出登录时 `RequireAuth` 把这里卸载；同一个标签页里换一个账号登录，
    // 拿到的必须是新账号的角色，不能是缓存里上一个人的。
    gcTime: 0,
  });

  if (me.isPending) {
    // 与 `RequireAuth` 的等待态刻意长得一样：用户看到的是同一个转圈接着转，而不是闪一下换一种。
    return (
      <div style={{ display: "flex", justifyContent: "center", paddingTop: 96 }}>
        <Spin size="large" />
      </div>
    );
  }

  // 没有外壳可用（还不知道该画哪一套），所以退出登录的按钮放在这里，免得人被困在这一页。
  const failed = (description: ReactNode) => (
    <div style={{ maxWidth: 640, margin: "96px auto 0", padding: "0 24px" }}>
      <Alert
        type="error"
        showIcon
        title={t("requireRole.loadFailed")}
        description={description}
        action={
          <Space>
            <Button size="small" onClick={() => void me.refetch()}>
              {t("requireRole.retry")}
            </Button>
            <Button size="small" onClick={() => void signOut()}>
              {t("nav.signOut")}
            </Button>
          </Space>
        }
      />
    </div>
  );

  if (me.isError) {
    return failed(<RequestReference error={me.error} />);
  }

  const actual = me.data.role;
  if (!isRole(actual)) {
    return failed(t("requireRole.unknownRole"));
  }

  if (actual !== role) {
    // `replace`：浏览器的「后退」不该退回一个刚刚被转走的页面，否则就是一次后退、一次转向的死循环。
    return <Navigate to={HOME_OF[actual]} replace />;
  }

  return <Outlet />;
}

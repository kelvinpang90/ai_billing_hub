/**
 * 客户门户的外层框架（AIH-TASK-096，设计 AIH-TASK-089 §11 任务 E）：门户的所有页面共用。
 *
 * ⚠️ 这里**不出现任何管理端菜单**，也不复用 `AppLayout`。客户点得到的管理端链接只会把他转回门户
 * （`RequireRole`），而后端对 `/api/v1/admin/*` 一律 403 —— 菜单在这里只是噪音，不是权限（spec §51）。
 * 门户自己的导航由之后的任务（首页、钱包、用量）各自加。
 */

import { Button, Layout } from "antd";
import { useTranslation } from "react-i18next";
import { Link, Outlet } from "react-router";

import { useAuth } from "../auth/AuthProvider";
import { ROUTES } from "../routes/paths";

const { Header, Content } = Layout;

export function PortalLayout() {
  const { t } = useTranslation();
  const { signOut } = useAuth();

  return (
    <Layout style={{ minHeight: "100vh" }}>
      <Header style={{ display: "flex", alignItems: "center", gap: 24 }}>
        <Link to={ROUTES.portalHome} style={{ color: "#fff", fontWeight: 600 }}>
          {t("app.name")}
        </Link>
        <span style={{ color: "rgba(255, 255, 255, 0.65)", flex: 1 }}>{t("portal.title")}</span>
        {/* 登出必须调后端，理由同 `AppLayout`：只清本地令牌的话，那张刷新 cookie 还活着。 */}
        <Button onClick={() => void signOut()}>{t("nav.signOut")}</Button>
      </Header>
      <Content style={{ padding: 24 }}>
        <Outlet />
      </Content>
    </Layout>
  );
}

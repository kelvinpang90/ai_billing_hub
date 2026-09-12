/**
 * 外层框架：所有登录后的页面共用。
 *
 * ⚠️ 菜单只是导航，**不是权限**。spec §51 写死了「Backend MUST independently
 * enforce authorization. Never rely only on frontend menu hiding.」——
 * T0.8 加角色时，这里按角色少显示几项菜单不构成任何访问控制。
 */

import { Button, Layout, Menu } from "antd";
import { useTranslation } from "react-i18next";
import { Link, Outlet, useLocation } from "react-router";

import { useAuth } from "../auth/AuthProvider";
import { ROUTES } from "../routes/paths";

const { Header, Content } = Layout;

export function AppLayout() {
  const { t } = useTranslation();
  const location = useLocation();
  const { signOut } = useAuth();

  return (
    <Layout style={{ minHeight: "100vh" }}>
      <Header style={{ display: "flex", alignItems: "center", gap: 24 }}>
        <span style={{ color: "#fff", fontWeight: 600 }}>{t("app.name")}</span>
        <Menu
          theme="dark"
          mode="horizontal"
          selectedKeys={[location.pathname]}
          style={{ flex: 1, minWidth: 0 }}
          items={[
            {
              key: ROUTES.dashboard,
              label: <Link to={ROUTES.dashboard}>{t("nav.dashboard")}</Link>,
            },
          ]}
        />
        {/* 登出必须调后端：只清本地令牌的话，那张刷新 cookie 还活着，
            谁拿到它都能继续换访问令牌。 */}
        <Button onClick={() => void signOut()}>{t("nav.signOut")}</Button>
      </Header>
      <Content style={{ padding: 24 }}>
        <Outlet />
      </Content>
    </Layout>
  );
}

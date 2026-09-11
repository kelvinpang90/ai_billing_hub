/**
 * 外层框架：所有登录后的页面共用。
 *
 * ⚠️ 菜单只是导航，**不是权限**。spec §51 写死了「Backend MUST independently
 * enforce authorization. Never rely only on frontend menu hiding.」——
 * T0.8 加角色时，这里按角色少显示几项菜单不构成任何访问控制。
 */

import { Layout, Menu } from "antd";
import { useTranslation } from "react-i18next";
import { Link, Outlet, useLocation } from "react-router";

import { ROUTES } from "../routes/paths";

const { Header, Content } = Layout;

export function AppLayout() {
  const { t } = useTranslation();
  const location = useLocation();

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
      </Header>
      <Content style={{ padding: 24 }}>
        <Outlet />
      </Content>
    </Layout>
  );
}

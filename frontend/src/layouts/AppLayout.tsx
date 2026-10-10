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

  // 客户详情、建客户都算在「客户」这一项下面，高亮不能只认精确路径。
  const inCustomers =
    location.pathname === ROUTES.customers ||
    location.pathname.startsWith(`${ROUTES.customers}/`);
  // 目录的三页（供应商、供应商详情、计量类型）都算在顶栏的「目录」这一项下面。
  const inCatalog =
    location.pathname === ROUTES.catalogProviders ||
    location.pathname.startsWith(`${ROUTES.catalogProviders}/`) ||
    location.pathname === ROUTES.catalogMeterTypes;
  // 价格列表与价格版本详情都算在顶栏的「供应商价格」这一项下面。
  const inProviderPrices =
    location.pathname === ROUTES.providerPrices ||
    location.pathname.startsWith(`${ROUTES.providerPrices}/`);
  // 规则列表与规则详情都算在顶栏的「定价规则」这一项下面。
  const inPricingRules =
    location.pathname === ROUTES.pricingRules ||
    location.pathname.startsWith(`${ROUTES.pricingRules}/`);
  // 用量事件列表与事件详情都算在顶栏的「用量事件」这一项下面。
  const inUsageEvents =
    location.pathname === ROUTES.usageEvents ||
    location.pathname.startsWith(`${ROUTES.usageEvents}/`);
  let selectedKey = location.pathname;
  if (inCustomers) {
    selectedKey = ROUTES.customers;
  } else if (inCatalog) {
    selectedKey = ROUTES.catalogProviders;
  } else if (inProviderPrices) {
    selectedKey = ROUTES.providerPrices;
  } else if (inPricingRules) {
    selectedKey = ROUTES.pricingRules;
  } else if (inUsageEvents) {
    selectedKey = ROUTES.usageEvents;
  }

  return (
    <Layout style={{ minHeight: "100vh" }}>
      <Header style={{ display: "flex", alignItems: "center", gap: 24 }}>
        <span style={{ color: "#fff", fontWeight: 600 }}>{t("app.name")}</span>
        <Menu
          theme="dark"
          mode="horizontal"
          selectedKeys={[selectedKey]}
          style={{ flex: 1, minWidth: 0 }}
          items={[
            {
              key: ROUTES.dashboard,
              label: <Link to={ROUTES.dashboard}>{t("nav.dashboard")}</Link>,
            },
            {
              key: ROUTES.customers,
              label: <Link to={ROUTES.customers}>{t("nav.customers")}</Link>,
            },
            {
              key: ROUTES.audit,
              label: <Link to={ROUTES.audit}>{t("nav.audit")}</Link>,
            },
            {
              key: ROUTES.catalogProviders,
              label: <Link to={ROUTES.catalogProviders}>{t("nav.catalog")}</Link>,
            },
            {
              key: ROUTES.providerPrices,
              label: <Link to={ROUTES.providerPrices}>{t("nav.providerPrices")}</Link>,
            },
            {
              key: ROUTES.fxRates,
              label: <Link to={ROUTES.fxRates}>{t("nav.fxRates")}</Link>,
            },
            {
              key: ROUTES.pricingRules,
              label: <Link to={ROUTES.pricingRules}>{t("nav.pricingRules")}</Link>,
            },
            {
              key: ROUTES.pricingPreview,
              label: <Link to={ROUTES.pricingPreview}>{t("nav.pricingPreview")}</Link>,
            },
            {
              key: ROUTES.usageEvents,
              label: <Link to={ROUTES.usageEvents}>{t("nav.usageEvents")}</Link>,
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

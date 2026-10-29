/**
 * 客户门户的外层框架（AIH-TASK-096，设计 AIH-TASK-089 §11 任务 E）：门户的所有页面共用。
 *
 * ⚠️ 这里**不出现任何管理端菜单**，也不复用 `AppLayout`。客户点得到的管理端链接只会把他转回门户
 * （`RequireRole`），而后端对 `/api/v1/admin/*` 一律 403 —— 菜单在这里只是噪音，不是权限（spec §51）。
 * 门户自己的导航是几个普通链接（AIH-TASK-100 加了首页与钱包，AIH-TASK-105 加了用量，AIH-TASK-123 加了会话，AIH-TASK-128
 * 加了通知；其余由之后的任务各自加），不用 antd `Menu`：
 * 门户里不出现 role 为 menu / menuitem 的元素，与管理端顶栏一眼可分。每个链接都指向 `ROUTES.portalHome` 之下。
 *
 * 「Notifications」旁边的未读数（AIH-TASK-128，设计闸门 #471 v7 §2「前端」）：进入门户时读一次
 * `GET /api/v1/customer/notifications`（不带查询串）的 `unread_count`，通知页标已读后由那一页让它重读；不轮询。
 * 0 时不显示；读取失败时也不显示数字，导航照常可用。数字放在链接**旁边**，不进链接的文字。
 */

import { useQuery } from "@tanstack/react-query";
import { Button, Layout } from "antd";
import { useTranslation } from "react-i18next";
import { Link, Outlet } from "react-router";

import { CUSTOMER_NOTIFICATIONS_UNREAD_QUERY_KEY, listCustomerNotifications } from "../api/customerNotifications";
import { useAuth } from "../auth/AuthProvider";
import { ROUTES } from "../routes/paths";

const { Header, Content } = Layout;

function UnreadNotificationsCount() {
  const { t } = useTranslation();
  const unread = useQuery({
    queryKey: CUSTOMER_NOTIFICATIONS_UNREAD_QUERY_KEY,
    queryFn: async ({ signal }) => (await listCustomerNotifications({}, signal)).unread_count,
    // 进入门户时读一次：换页、切回标签页都不重读，只有通知页标已读后让它过期重读。
    staleTime: Infinity,
    refetchOnWindowFocus: false,
    // 理由同钱包页：同一个标签页换账号登录，拿到的必须是新客户的未读数。
    gcTime: 0,
  });

  if (unread.isError || unread.data === undefined || unread.data === 0) {
    return null;
  }
  return (
    <span
      aria-label={t("portal.nav.unreadCount")}
      title={t("portal.nav.unreadCount")}
      style={{
        background: "#ff4d4f",
        color: "#fff",
        borderRadius: 10,
        padding: "0 6px",
        fontSize: 12,
        lineHeight: "20px",
        fontVariantNumeric: "tabular-nums",
      }}
    >
      {String(unread.data)}
    </span>
  );
}

export function PortalLayout() {
  const { t } = useTranslation();
  const { signOut } = useAuth();

  return (
    <Layout style={{ minHeight: "100vh" }}>
      <Header style={{ display: "flex", alignItems: "center", gap: 24 }}>
        <Link to={ROUTES.portalHome} style={{ color: "#fff", fontWeight: 600 }}>
          {t("app.name")}
        </Link>
        <span style={{ color: "rgba(255, 255, 255, 0.65)" }}>{t("portal.title")}</span>
        <nav style={{ display: "flex", gap: 16, flex: 1 }}>
          <Link to={ROUTES.portalHome} style={{ color: "#fff" }}>
            {t("portal.nav.home")}
          </Link>
          <Link to={ROUTES.portalWallet} style={{ color: "#fff" }}>
            {t("portal.nav.wallet")}
          </Link>
          <Link to={ROUTES.portalUsage} style={{ color: "#fff" }}>
            {t("portal.nav.usage")}
          </Link>
          <Link to={ROUTES.portalConversations} style={{ color: "#fff" }}>
            {t("portal.nav.conversations")}
          </Link>
          <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
            <Link to={ROUTES.portalNotifications} style={{ color: "#fff" }}>
              {t("portal.nav.notifications")}
            </Link>
            <UnreadNotificationsCount />
          </span>
        </nav>
        {/* 登出必须调后端，理由同 `AppLayout`：只清本地令牌的话，那张刷新 cookie 还活着。 */}
        <Button onClick={() => void signOut()}>{t("nav.signOut")}</Button>
      </Header>
      <Content style={{ padding: 24 }}>
        <Outlet />
      </Content>
    </Layout>
  );
}

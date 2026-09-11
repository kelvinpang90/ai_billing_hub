/**
 * 兜底路由。
 *
 * 存在的理由：SPA 的 nginx 配置把所有未知路径都回 `index.html`（见
 * `frontend/nginx.conf` 的 `try_files`），所以打错的 URL **不会**得到 404，
 * 而是加载整个应用然后匹配不到任何路由。没有这一页的话，用户看到的是一片空白。
 */

import { Button, Result } from "antd";
import { useTranslation } from "react-i18next";
import { Link } from "react-router";

import { ROUTES } from "./paths";

export function NotFoundPage() {
  const { t } = useTranslation();

  return (
    <Result
      status="404"
      title={t("notFound.title")}
      subTitle={t("notFound.description")}
      extra={
        <Link to={ROUTES.dashboard}>
          <Button type="primary">{t("notFound.backToDashboard")}</Button>
        </Link>
      }
    />
  );
}

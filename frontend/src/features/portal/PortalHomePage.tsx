/**
 * 客户门户首页（`/portal`）。
 *
 * **现在只是一句欢迎占位**（AIH-TASK-096）：它存在是为了让 CUSTOMER 登录后有地方可落。真内容
 * （`/api/v1/customer/me`、只读提示、开启 2FA）属于 AIH-TASK-097。
 */

import { Card, Typography } from "antd";
import { useTranslation } from "react-i18next";

export function PortalHomePage() {
  const { t } = useTranslation();

  return (
    <Card title={t("portal.home.title")}>
      <Typography.Text>{t("portal.home.intro")}</Typography.Text>
    </Card>
  );
}

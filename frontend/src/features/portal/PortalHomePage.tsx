/**
 * 客户门户首页（`/portal`，AIH-TASK-097，设计 AIH-TASK-089 §11 任务 E）。
 *
 * 显示 `GET /api/v1/customer/me` 的公司名与账户状态；`read_only` 为 `true`（`DISABLED` / `CLOSED`）时加一条只读提示；
 * 还没开启 2FA 时下面是开启流程（`EnableTwoFactor`，它自己看 `/api/v1/me`）。
 *
 * ⚠️ 这一页**只读 `/api/v1/customer/me`**，不请求也不显示任何金额、成本、毛利或内部字段。`customer_id` 是内部用来
 * 对账的标识，客户用不上，也不显示。
 */

import { useQuery } from "@tanstack/react-query";
import { Alert, Button, Card, Descriptions, Space, Spin, Typography } from "antd";
import { useTranslation } from "react-i18next";

import { CUSTOMER_ME_QUERY_KEY, getCustomerMe } from "../../api/customer";
import { RequestReference } from "../../components/RequestReference";
import { EnableTwoFactor } from "./EnableTwoFactor";

/** 账户状态的文案。认不出的值原样显示：后端多一种状态时，宁可露出代码，也不猜成某一种。 */
function AccountStatusText({ status }: { status: string }) {
  const { t } = useTranslation();
  switch (status) {
    case "PENDING_ACTIVATION":
      return t("portal.home.accountStatus.pendingActivation");
    case "ENABLED":
      return t("portal.home.accountStatus.enabled");
    case "DISABLED":
      return t("portal.home.accountStatus.disabled");
    case "CLOSED":
      return t("portal.home.accountStatus.closed");
    default:
      return status;
  }
}

function AccountSummary() {
  const { t } = useTranslation();
  const account = useQuery({
    queryKey: CUSTOMER_ME_QUERY_KEY,
    queryFn: ({ signal }) => getCustomerMe(signal),
    // 理由同 `RequireRole`：同一个标签页换账号登录，拿到的必须是新客户的摘要。
    gcTime: 0,
  });

  if (account.isPending) {
    return (
      <Card title={t("portal.home.title")}>
        <Space aria-busy="true">
          <Spin />
          <Typography.Text>{t("portal.home.loading")}</Typography.Text>
        </Space>
      </Card>
    );
  }

  if (account.isError) {
    return (
      <Alert
        type="error"
        showIcon
        message={t("portal.home.loadFailed")}
        description={<RequestReference error={account.error} />}
        action={
          <Button size="small" onClick={() => void account.refetch()}>
            {t("portal.home.retry")}
          </Button>
        }
      />
    );
  }

  const { company_name: companyName, account_status: accountStatus, read_only: readOnly } = account.data;
  return (
    <Card title={t("portal.home.title")}>
      {readOnly ? (
        <Alert
          type="warning"
          showIcon
          style={{ marginBottom: 16 }}
          message={t("portal.home.readOnly.title")}
          description={t("portal.home.readOnly.body")}
        />
      ) : null}
      <Descriptions bordered column={1} size="small">
        <Descriptions.Item label={t("portal.home.field.companyName")}>{companyName}</Descriptions.Item>
        <Descriptions.Item label={t("portal.home.field.accountStatus")}>
          <AccountStatusText status={accountStatus} />
        </Descriptions.Item>
      </Descriptions>
    </Card>
  );
}

export function PortalHomePage() {
  return (
    <Space direction="vertical" size="large" style={{ width: "100%" }}>
      <AccountSummary />
      {/* 摘要读失败不影响开启 2FA：两块各自请求、各自报错。 */}
      <EnableTwoFactor />
    </Space>
  );
}

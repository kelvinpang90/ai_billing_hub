/**
 * 客户门户首页（`/portal`，AIH-TASK-097，设计 AIH-TASK-089 §11 任务 E）。
 *
 * 显示 `GET /api/v1/customer/me` 的公司名与账户状态；`read_only` 为 `true`（`DISABLED` / `CLOSED`）时加一条只读提示；
 * 下面是余额卡与最近 5 条流水（AIH-TASK-101，设计闸门 #393 v3 §2「前端」、§8 第 3 步），复用钱包页的
 * `WalletSummaryCard` 与 `CustomerTransactionsTable`，附一个到钱包页的链接；最近流水不翻页。
 * 余额卡之后是本月用量卡（`UsageSummaryCard`，AIH-TASK-108，设计闸门 #403 v1 §2「前端」）：计费额、请求数、对话数与
 * 按日请求数柱状图，链到用量页。还没开启 2FA 时最下面是开启流程（`EnableTwoFactor`，它自己看 `/api/v1/me`）。
 *
 * 各块各自请求、各自画加载中与失败（带 request_id）：哪一块读失败（含本月用量的 `QUERY_TOO_BROAD`）都不影响首页
 * 其余部分。
 *
 * ⚠️ 这一页只请求 `/api/v1/customer/me`、`/api/v1/customer/wallet`、`/api/v1/customer/wallet/transactions` 与
 * `/api/v1/customer/usage-summary`，不请求任何 `/api/v1/admin/` 路径，不显示成本、毛利或内部字段（INV-7）。金额的
 * 显示精度在复用的组件里，这里不碰金额。`customer_id` 是内部用来对账的标识，客户用不上，也不显示。
 */

import { useQuery } from "@tanstack/react-query";
import { Alert, Button, Card, Descriptions, Empty, Skeleton, Space, Spin, Typography } from "antd";
import type { ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router";

import { CUSTOMER_ME_QUERY_KEY, getCustomerMe } from "../../api/customer";
import { customerTransactionListQueryKey, listCustomerTransactions } from "../../api/customerWallet";
import { RequestReference } from "../../components/RequestReference";
import { ROUTES } from "../../routes/paths";
import { CustomerTransactionsTable } from "./CustomerTransactionsTable";
import { EnableTwoFactor } from "./EnableTwoFactor";
import { UsageSummaryCard } from "./UsageSummaryCard";
import { WalletSummaryCard } from "./WalletSummaryCard";

/** 首页只看最近几条：设计 §2「前端」定为 `page_size=5`，不翻页，完整流水在钱包页。 */
const RECENT_TRANSACTIONS_PAGE_SIZE = 5;

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

function RecentTransactions() {
  const { t } = useTranslation();
  const transactions = useQuery({
    queryKey: customerTransactionListQueryKey(null, RECENT_TRANSACTIONS_PAGE_SIZE),
    queryFn: ({ signal }) => listCustomerTransactions({ pageSize: RECENT_TRANSACTIONS_PAGE_SIZE }, signal),
    // 理由同余额卡：同一个标签页换账号登录，拿到的必须是新客户的流水。
    gcTime: 0,
  });

  let body: ReactNode;
  if (transactions.isPending) {
    body = (
      <Space direction="vertical" aria-busy="true" style={{ width: "100%" }}>
        <Skeleton active title={false} paragraph={{ rows: 3 }} />
        <Typography.Text type="secondary">{t("portal.wallet.transactions.loading")}</Typography.Text>
      </Space>
    );
  } else if (transactions.isError) {
    body = (
      <Alert
        type="error"
        showIcon
        message={t("portal.wallet.transactions.loadFailed")}
        description={<RequestReference error={transactions.error} />}
        action={
          <Button size="small" onClick={() => void transactions.refetch()}>
            {t("portal.wallet.retry")}
          </Button>
        }
      />
    );
  } else if (transactions.data.items.length === 0) {
    body = <Empty description={t("portal.wallet.transactions.empty")} />;
  } else {
    body = <CustomerTransactionsTable items={transactions.data.items} loading={transactions.isFetching} />;
  }

  return (
    // role + aria-label：部署后验收脚本按语义结构找到这一块（不依赖 antd 的类名）。
    <Card
      role="region"
      aria-label={t("portal.home.recentTransactions.title")}
      title={t("portal.home.recentTransactions.title")}
      extra={<Link to={ROUTES.portalWallet}>{t("portal.home.recentTransactions.viewAll")}</Link>}
    >
      {body}
    </Card>
  );
}

export function PortalHomePage() {
  return (
    <Space direction="vertical" size="large" style={{ width: "100%" }}>
      <AccountSummary />
      {/* 每一块各自请求、各自报错：摘要、余额、本月用量或流水读失败都不影响其余几块，也不影响开启 2FA。 */}
      <WalletSummaryCard />
      <UsageSummaryCard />
      <RecentTransactions />
      <EnableTwoFactor />
    </Space>
  );
}

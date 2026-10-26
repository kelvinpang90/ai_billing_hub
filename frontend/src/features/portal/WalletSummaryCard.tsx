/**
 * 门户的余额卡（`GET /api/v1/customer/wallet`，AIH-TASK-100；设计闸门 #393 v3 §2「前端」）。钱包页与门户首页
 * （AIH-TASK-101）共用：卡自己请求、自己画加载中与失败，放到哪一页都不用另配。
 *
 * - 余额按设计 §2「金额（前端显示）」显示 2 位小数：`formatAmountRounded`（字符串 `ROUND_HALF_UP`），币种在前，
 *   写法同 `formatMoney`（`MYR 12.46`）。不转成 number、不另写舍入。
 * - 计费状态只在 `billing_mode` 为 `PREPAID` 时显示。`INTERNAL_METERED_ONLY` 的客户余额通常为 0、`billing_status`
 *   为 `SUSPENDED`，但 AI 照常可用 —— 显示出来就是误报「已暂停」。AI 能不能用看 `ai_service`。
 * - `ai_service` 与四种 `ai_service_reason` 各有固定文案；认不出的值原样显示，不猜成某一种。
 * - 低余额警告（AIH-TASK-112，设计闸门 #433 v2 §2「前端」）：只在 `low_balance` 为 `true` 时显示，带按 2 位显示的
 *   阈值与币种，请客户尽快充值；没有充值按钮。判定只看 `low_balance`，不自己比较余额与阈值（规则只在后端）。
 *
 * ⚠️ 只显示这几个字段：没有成本、毛利或任何内部字段（INV-7）。
 */

import { useQuery } from "@tanstack/react-query";
import { Alert, Button, Card, Descriptions, Space, Spin, Typography } from "antd";
import type { ReactNode } from "react";
import { useTranslation } from "react-i18next";

import { CUSTOMER_WALLET_QUERY_KEY, getCustomerWallet } from "../../api/customerWallet";
import { formatAmountRounded } from "../../components/MoneyText";
import { RequestReference } from "../../components/RequestReference";

/** 余额、`balance_after` 与非 AI 用量行金额的显示位数（设计 §2，spec §80「MYR normal display → 2 decimals」）。 */
const DISPLAY_PLACES = 2;

/** 币种在前：`MYR 12.46`。写法与 `formatMoney` 相同，只是小数按显示位数舍入。 */
function formatBalance(balance: string, currency: string): string {
  return `${currency} ${formatAmountRounded(balance, DISPLAY_PLACES)}`;
}

function BillingStatusText({ status }: { status: string }) {
  const { t } = useTranslation();
  switch (status) {
    case "ACTIVE":
      return t("portal.wallet.billingStatus.active");
    case "SUSPENDED":
      return t("portal.wallet.billingStatus.suspended");
    default:
      return status;
  }
}

function AiServiceText({ service }: { service: string }) {
  const { t } = useTranslation();
  switch (service) {
    case "ALLOW_AI":
      return t("portal.wallet.aiService.allow");
    case "BLOCK_AI":
      return t("portal.wallet.aiService.block");
    default:
      return service;
  }
}

function AiServiceReasonText({ reason }: { reason: string }) {
  const { t } = useTranslation();
  switch (reason) {
    case "ACCOUNT_CLOSED":
      return t("portal.wallet.aiServiceReason.accountClosed");
    case "ACCOUNT_DISABLED":
      return t("portal.wallet.aiServiceReason.accountDisabled");
    case "ACCOUNT_PENDING_ACTIVATION":
      return t("portal.wallet.aiServiceReason.accountPendingActivation");
    case "BALANCE_NOT_POSITIVE":
      return t("portal.wallet.aiServiceReason.balanceNotPositive");
    default:
      return reason;
  }
}

export function WalletSummaryCard() {
  const { t } = useTranslation();
  const wallet = useQuery({
    queryKey: CUSTOMER_WALLET_QUERY_KEY,
    queryFn: ({ signal }) => getCustomerWallet(signal),
    // 理由同门户首页的摘要：同一个标签页换账号登录，拿到的必须是新客户的钱包。
    gcTime: 0,
  });

  let body: ReactNode;
  if (wallet.isPending) {
    body = (
      <Space aria-busy="true">
        <Spin />
        <Typography.Text>{t("portal.wallet.summary.loading")}</Typography.Text>
      </Space>
    );
  } else if (wallet.isError) {
    body = (
      <Alert
        type="error"
        showIcon
        message={t("portal.wallet.summary.loadFailed")}
        description={<RequestReference error={wallet.error} />}
        action={
          <Button size="small" onClick={() => void wallet.refetch()}>
            {t("portal.wallet.retry")}
          </Button>
        }
      />
    );
  } else {
    const { currency, balance, billing_mode: billingMode, billing_status: billingStatus } = wallet.data;
    const { ai_service: aiService, ai_service_reason: aiServiceReason } = wallet.data;
    const { low_balance: lowBalance, low_balance_threshold: lowBalanceThreshold } = wallet.data;
    body = (
      <Space direction="vertical" style={{ width: "100%" }}>
        {lowBalance ? (
          <Alert
            type="warning"
            showIcon
            message={t("portal.wallet.lowBalance.title")}
            description={
              lowBalanceThreshold !== null
                ? t("portal.wallet.lowBalance.message", { threshold: formatBalance(lowBalanceThreshold, currency) })
                : t("portal.wallet.lowBalance.messageWithoutThreshold")
            }
          />
        ) : null}
        <Descriptions bordered column={1} size="small">
        <Descriptions.Item label={t("portal.wallet.field.balance")}>
          <span style={{ fontVariantNumeric: "tabular-nums" }}>{formatBalance(balance, currency)}</span>
        </Descriptions.Item>
        {billingMode === "PREPAID" ? (
          <Descriptions.Item label={t("portal.wallet.field.billingStatus")}>
            <BillingStatusText status={billingStatus} />
          </Descriptions.Item>
        ) : null}
        <Descriptions.Item label={t("portal.wallet.field.aiService")}>
          <AiServiceText service={aiService} />
        </Descriptions.Item>
        {aiServiceReason !== null ? (
          <Descriptions.Item label={t("portal.wallet.field.aiServiceReason")}>
            <AiServiceReasonText reason={aiServiceReason} />
          </Descriptions.Item>
        ) : null}
        </Descriptions>
      </Space>
    );
  }

  return (
    // role + aria-label：部署后验收脚本按语义结构找到这张卡（不依赖 antd 的类名）。
    <Card role="region" aria-label={t("portal.wallet.summary.title")} title={t("portal.wallet.summary.title")}>
      {body}
    </Card>
  );
}

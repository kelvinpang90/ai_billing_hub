/**
 * 客户门户钱包页（`/portal/wallet`，AIH-TASK-100；设计闸门 #393 v3 §2「前端」、§8 第 2 步）。
 *
 * 上面是余额卡（`WalletSummaryCard`），下面是自己钱包的流水（`GET /api/v1/customer/wallet/transactions`）。
 * 语义以 docs/api.md「客户钱包」为准。只读：页面上没有任何写操作。
 *
 * 分页是序号游标，没有页码与总数：「Older」把响应的 `next_before_sequence` 原样作为下一次的 `before_sequence`，
 * 它为 `null`（没有更早的行）时按钮不出现；「Newest」回到不带游标的最新一页。游标只放在组件状态里。
 *
 * 三种状态都画出来（spec §132 DoD 第 8 条）：加载中、失败（带 request_id）、空列表。
 *
 * ⚠️ 只请求 `/api/v1/customer/*`：不请求任何 `/api/v1/admin/` 路径，不显示成本、毛利或内部字段（INV-7）。
 */

import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { Alert, Button, Card, Empty, Skeleton, Space, Typography } from "antd";
import { useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";

import { customerTransactionListQueryKey, listCustomerTransactions } from "../../api/customerWallet";
import { RequestReference } from "../../components/RequestReference";
import { CustomerTransactionsTable } from "./CustomerTransactionsTable";
import { WalletSummaryCard } from "./WalletSummaryCard";

/** 每页行数：与接口的默认值相同（docs/api.md：1–100，默认 20）。 */
export const WALLET_PAGE_SIZE = 20;

function TransactionsSection() {
  const { t } = useTranslation();
  const [beforeSequence, setBeforeSequence] = useState<number | null>(null);

  const transactions = useQuery({
    queryKey: customerTransactionListQueryKey(beforeSequence, WALLET_PAGE_SIZE),
    queryFn: ({ signal }) => listCustomerTransactions({ pageSize: WALLET_PAGE_SIZE, beforeSequence }, signal),
    // 翻页时先留着上一页，不让整张表闪成骨架屏。
    placeholderData: keepPreviousData,
    // 理由同余额卡：同一个标签页换账号登录，拿到的必须是新客户的流水。
    gcTime: 0,
  });

  const next = transactions.data?.next_before_sequence ?? null;
  // 占位数据是上一页的：拿它的游标翻页会重复请求同一页，等新的一页回来再让按钮可点。
  const settled = !transactions.isPlaceholderData;

  const toolbar = (
    <Space wrap style={{ marginBottom: 16 }}>
      <Button onClick={() => setBeforeSequence(null)} disabled={beforeSequence === null}>
        {t("portal.wallet.transactions.newest")}
      </Button>
      {next !== null && !transactions.isError ? (
        <Button onClick={() => setBeforeSequence(next)} disabled={!settled}>
          {t("portal.wallet.transactions.older")}
        </Button>
      ) : null}
    </Space>
  );

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
    body = (
      <Empty
        description={
          beforeSequence === null
            ? t("portal.wallet.transactions.empty")
            : t("portal.wallet.transactions.emptyPage")
        }
      />
    );
  } else {
    body = <CustomerTransactionsTable items={transactions.data.items} loading={transactions.isFetching} />;
  }

  return (
    // role + aria-label：部署后验收脚本按语义结构找到这一块（不依赖 antd 的类名）。
    <Card
      role="region"
      aria-label={t("portal.wallet.transactions.title")}
      title={t("portal.wallet.transactions.title")}
    >
      {toolbar}
      {body}
    </Card>
  );
}

export function WalletPage() {
  return (
    <Space direction="vertical" size="large" style={{ width: "100%" }}>
      {/* 两块各自请求、各自报错：余额读失败不挡流水，反之亦然。两者不是同一快照，不互相校验（设计 §4）。 */}
      <WalletSummaryCard />
      <TransactionsSection />
    </Space>
  );
}

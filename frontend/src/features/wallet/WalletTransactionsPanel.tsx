/**
 * 客户详情里的钱包流水面板（`GET /api/v1/admin/customers/{customer_id}/wallet/transactions`）。
 *
 * 语义以 docs/api.md「管理端钱包流水」为准（AIH-TASK-084；设计闸门 #351 v2 §2「前端」）。只读：面板上没有
 * 任何写操作。
 *
 * 分页是序号游标，没有页码与总数：「Older」把响应的 `next_before_sequence` 原样作为下一次的
 * `before_sequence`（为 `null` 时没有更早的行，按钮禁用），「Newest」回到不带游标的最新一页。切换来源时
 * 也回到最新一页 —— 游标是上一个筛选下的序号，换了筛选就没有意义了。游标与筛选只放在组件状态里，地址栏
 * 归客户详情用。
 *
 * 三种状态都画出来（spec §132 DoD 第 8 条）：加载中、失败（带 request_id）、空列表。
 *
 * ⚠️ 金额与余额**原样显示**接口给的十进制字符串（INV-10）：不转成 number、不经 `MoneyText` 改写
 * 小数位 —— 排查余额问题时要看到的就是账本里的那个值，8 位小数一位不少。
 *
 * AI 用量行有 `usage_event_id` 时链接到用量事件详情；为 `null`（反查不到事件，数据缺陷）时只显示来源，
 * 不出链接。
 */

import { keepPreviousData, useQuery } from "@tanstack/react-query";
import {
  Button,
  Card,
  Empty,
  Select,
  Skeleton,
  Space,
  Table,
  Typography,
  type TableColumnsType,
} from "antd";
import { useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router";

import { DEFAULT_PAGE_SIZE } from "../../api/adminCustomers";
import {
  WALLET_REFERENCE_TYPES,
  listWalletTransactions,
  walletTransactionListQueryKey,
  type WalletReferenceType,
  type WalletTransaction,
} from "../../api/walletTransactions";
import { DateTimeText } from "../../components/DateTimeText";
import { usageEventDetailPath } from "../../routes/paths";
import { CustomerErrorAlert } from "../customers/CustomerForm";

/** 下拉里「全部来源」的取值。不会发给后端：选它就是不带 `reference_type`。 */
const ALL_SOURCES = "ALL";

/** 空的可选字段显示成一道横线，与详情页的空字段一致。 */
const NO_VALUE = "—";

/** 来源的显示名。每个 key 都写成字面量，i18n 的 key 检查才找得到它们。 */
function useSourceLabels(): Record<WalletReferenceType, string> {
  const { t } = useTranslation();
  return {
    USAGE_EVENT: t("wallet.transactions.source.usageEvent"),
    PAYMENT: t("wallet.transactions.source.payment"),
    ADMIN_ADJUSTMENT: t("wallet.transactions.source.adminAdjustment"),
    REBILL: t("wallet.transactions.source.rebill"),
    SYSTEM: t("wallet.transactions.source.system"),
  };
}

function isReferenceType(value: string): value is WalletReferenceType {
  return (WALLET_REFERENCE_TYPES as readonly string[]).includes(value);
}

/** 十进制字符串原样输出，只加等宽数字。 */
function DecimalString({ value }: { value: string }) {
  return <span style={{ fontVariantNumeric: "tabular-nums" }}>{value}</span>;
}

export function WalletTransactionsPanel({ customerId }: { customerId: string }) {
  const { t } = useTranslation();
  const sourceLabels = useSourceLabels();
  const [referenceType, setReferenceType] = useState<WalletReferenceType | null>(null);
  const [beforeSequence, setBeforeSequence] = useState<number | null>(null);

  const transactions = useQuery({
    queryKey: walletTransactionListQueryKey(
      customerId,
      referenceType,
      beforeSequence,
      DEFAULT_PAGE_SIZE,
    ),
    queryFn: ({ signal }) =>
      listWalletTransactions(
        customerId,
        { pageSize: DEFAULT_PAGE_SIZE, beforeSequence, referenceType },
        signal,
      ),
    // 翻页、换筛选时先留着上一页，不让整张表闪成骨架屏。
    placeholderData: keepPreviousData,
  });

  const chooseSource = (value: string): void => {
    setReferenceType(isReferenceType(value) ? value : null);
    setBeforeSequence(null);
  };

  const next = transactions.data?.next_before_sequence ?? null;
  // 占位数据是上一页的：拿它的游标翻页会重复请求同一页，等新的一页回来再给按钮。
  const settled = !transactions.isPlaceholderData;

  const columns: TableColumnsType<WalletTransaction> = [
    {
      key: "created_at",
      title: t("wallet.transactions.field.createdAt"),
      render: (_: unknown, row) => <DateTimeText value={row.created_at} />,
    },
    {
      key: "wallet_sequence",
      title: t("wallet.transactions.field.sequence"),
      dataIndex: "wallet_sequence",
    },
    {
      key: "transaction_type",
      title: t("wallet.transactions.field.type"),
      render: (_: unknown, row) => <Typography.Text code>{row.transaction_type}</Typography.Text>,
    },
    {
      key: "amount",
      title: t("wallet.transactions.field.amount"),
      align: "right",
      render: (_: unknown, row) => <DecimalString value={row.amount} />,
    },
    {
      key: "balance_after",
      title: t("wallet.transactions.field.balanceAfter"),
      align: "right",
      render: (_: unknown, row) => <DecimalString value={row.balance_after} />,
    },
    {
      key: "reference_type",
      title: t("wallet.transactions.field.source"),
      render: (_: unknown, row) => (
        <Space direction="vertical" size={0}>
          {isReferenceType(row.reference_type)
            ? sourceLabels[row.reference_type]
            : row.reference_type}
          {row.reference_type === "USAGE_EVENT" && row.usage_event_id !== null ? (
            <Link to={usageEventDetailPath(row.usage_event_id)}>
              {t("wallet.transactions.viewUsageEvent")}
            </Link>
          ) : null}
        </Space>
      ),
    },
    {
      key: "description",
      title: t("wallet.transactions.field.reason"),
      render: (_: unknown, row) => (
        <Typography.Text style={{ whiteSpace: "pre-wrap" }}>
          {row.description ?? NO_VALUE}
        </Typography.Text>
      ),
    },
    {
      key: "created_by_email",
      title: t("wallet.transactions.field.operator"),
      render: (_: unknown, row) => row.created_by_email ?? NO_VALUE,
    },
  ];

  const toolbar = (
    <Space wrap style={{ marginBottom: 16 }}>
      <Select<string>
        aria-label={t("wallet.transactions.field.source")}
        style={{ minWidth: 200 }}
        value={referenceType ?? ALL_SOURCES}
        onChange={chooseSource}
        options={[
          { value: ALL_SOURCES, label: t("wallet.transactions.source.all") },
          ...WALLET_REFERENCE_TYPES.map((type) => ({ value: type, label: sourceLabels[type] })),
        ]}
      />
      <Button onClick={() => setBeforeSequence(null)} disabled={beforeSequence === null}>
        {t("wallet.transactions.newest")}
      </Button>
      <Button
        onClick={() => {
          if (next !== null) {
            setBeforeSequence(next);
          }
        }}
        disabled={next === null || !settled || transactions.isError}
      >
        {t("wallet.transactions.older")}
      </Button>
    </Space>
  );

  let body: ReactNode;
  if (transactions.isPending) {
    body = (
      <>
        <Skeleton active title={false} paragraph={{ rows: 3 }} />
        <Typography.Text type="secondary">{t("wallet.transactions.loading")}</Typography.Text>
      </>
    );
  } else if (transactions.isError) {
    body = (
      <CustomerErrorAlert
        error={transactions.error}
        title={t("wallet.transactions.loadFailed")}
        onRetry={() => void transactions.refetch()}
      />
    );
  } else if (transactions.data.items.length === 0) {
    let empty: string;
    if (beforeSequence !== null) {
      empty = t("wallet.transactions.emptyPage");
    } else if (referenceType !== null) {
      empty = t("wallet.transactions.emptyFiltered");
    } else {
      empty = t("wallet.transactions.empty");
    }
    body = <Empty description={empty} />;
  } else {
    body = (
      <Table<WalletTransaction>
        rowKey="id"
        columns={columns}
        dataSource={transactions.data.items}
        loading={transactions.isFetching}
        pagination={false}
        scroll={{ x: "max-content" }}
      />
    );
  }

  return (
    // role + aria-label：部署后验收脚本按语义结构找到这个面板（不依赖 antd 的类名）。
    <Card
      role="region"
      aria-label={t("wallet.transactions.title")}
      title={t("wallet.transactions.title")}
    >
      {toolbar}
      {body}
    </Card>
  );
}

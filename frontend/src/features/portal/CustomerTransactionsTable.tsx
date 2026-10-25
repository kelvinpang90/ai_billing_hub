/**
 * 门户的流水表（AIH-TASK-100；设计闸门 #393 v3 §2「前端」）。只管画一页行：请求、翻页、加载中 / 失败 / 空列表由
 * 调用方负责，所以钱包页（整页翻页）与门户首页（AIH-TASK-101，最近 5 条）共用同一张表。
 *
 * 列：时间、类型文案、金额、余额（后）、`event_id`。
 * - 类型按 `transaction_type` 显示固定文案（spec §70「customer-facing transaction descriptions」），九种之外的值
 *   原样显示。库里的 `description` 是写给内部看的，接口不返回，这里也没有这一列。
 * - 金额按设计 §2「金额（前端显示）」：`balance_after` 与非 `AI_USAGE` 行的 `amount` 2 位小数，`AI_USAGE` 行的
 *   `amount` 最多 6 位（与用量页同一规则，同一笔扣费两页显示相同）。一律经 `formatAmountRounded`，不转成 number、
 *   不在前端做加减：「上一行余额 − 本行金额」与本行余额的显示值可能差一分，这是显示舍入的预期结果。
 * - `event_id` 只有 AI 用量行有；为 `null` 时留空。有值时链到用量页、查询串只带 `event_id`（AIH-TASK-106；设计闸门
 *   #398 v4 §2「前端」），用量页据此只显示那一条。
 */

import { Table, type TableColumnsType } from "antd";
import { useTranslation } from "react-i18next";
import { Link } from "react-router";

import type { CustomerTransaction } from "../../api/customerWallet";
import { DateTimeText } from "../../components/DateTimeText";
import { formatAmountRounded } from "../../components/MoneyText";
import { ROUTES } from "../../routes/paths";
import { EVENT_ID_PARAM } from "./UsagePage";

/** spec §80「MYR normal display → 2 decimals」。 */
const NORMAL_PLACES = 2;
/** spec §80「Detailed usage may display up to 6 decimals」：只用于 `AI_USAGE` 行的金额。 */
const USAGE_PLACES = 6;

const AI_USAGE = "AI_USAGE";

/** 交易类型的文案。每个 key 都写成字面量，i18n 的 key 检查才找得到它们。 */
function TransactionTypeText({ type }: { type: string }) {
  const { t } = useTranslation();
  switch (type) {
    case "TOPUP":
      return t("portal.wallet.transactionType.topup");
    case "AI_USAGE":
      return t("portal.wallet.transactionType.aiUsage");
    case "ADJUSTMENT_CREDIT":
      return t("portal.wallet.transactionType.adjustmentCredit");
    case "ADJUSTMENT_DEBIT":
      return t("portal.wallet.transactionType.adjustmentDebit");
    case "REBILL_CREDIT":
      return t("portal.wallet.transactionType.rebillCredit");
    case "REBILL_DEBIT":
      return t("portal.wallet.transactionType.rebillDebit");
    case "REFUND_ADJUSTMENT":
      return t("portal.wallet.transactionType.refundAdjustment");
    case "BONUS":
      return t("portal.wallet.transactionType.bonus");
    case "SYSTEM_CORRECTION":
      return t("portal.wallet.transactionType.systemCorrection");
    default:
      return type;
  }
}

/** 用量页只看这一条事件的地址。 */
function usageEventPath(eventId: string): string {
  return `${ROUTES.portalUsage}?${new URLSearchParams({ [EVENT_ID_PARAM]: eventId }).toString()}`;
}

function Amount({ value, places }: { value: string; places: number }) {
  return <span style={{ fontVariantNumeric: "tabular-nums" }}>{formatAmountRounded(value, places)}</span>;
}

export function CustomerTransactionsTable({
  items,
  loading = false,
}: {
  items: CustomerTransaction[];
  /** 翻页时上一页还留在表里，新的一页在路上。 */
  loading?: boolean;
}) {
  const { t } = useTranslation();

  const columns: TableColumnsType<CustomerTransaction> = [
    {
      key: "created_at",
      title: t("portal.wallet.transactions.field.createdAt"),
      render: (_: unknown, row) => <DateTimeText value={row.created_at} />,
    },
    {
      key: "transaction_type",
      title: t("portal.wallet.transactions.field.type"),
      render: (_: unknown, row) => <TransactionTypeText type={row.transaction_type} />,
    },
    {
      key: "amount",
      title: t("portal.wallet.transactions.field.amount"),
      align: "right",
      render: (_: unknown, row) => (
        <Amount value={row.amount} places={row.transaction_type === AI_USAGE ? USAGE_PLACES : NORMAL_PLACES} />
      ),
    },
    {
      key: "balance_after",
      title: t("portal.wallet.transactions.field.balanceAfter"),
      align: "right",
      render: (_: unknown, row) => <Amount value={row.balance_after} places={NORMAL_PLACES} />,
    },
    {
      key: "event_id",
      title: t("portal.wallet.transactions.field.eventId"),
      render: (_: unknown, row) =>
        row.event_id === null ? "" : <Link to={usageEventPath(row.event_id)}>{row.event_id}</Link>,
    },
  ];

  return (
    <Table<CustomerTransaction>
      rowKey="id"
      columns={columns}
      dataSource={items}
      loading={loading}
      pagination={false}
      scroll={{ x: "max-content" }}
    />
  );
}

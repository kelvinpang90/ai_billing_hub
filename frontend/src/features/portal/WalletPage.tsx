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
 * 流水上方是导出区（AIH-TASK-118；设计闸门 #435 v10 §2「前端」，语义以 docs/api.md「客户导出」为准）：日期区间
 * 默认是马来西亚时间本月 1 日到今天（与用量页同一规则），「Export CSV」调 `GET /api/v1/customer/wallet/transactions/export`
 * 交给浏览器下载。区间不完整、倒置、越界或超过 92 天时按钮不可用并说明原因；导出中按钮转为加载状态、不可重复点。
 * `EXPORT_TOO_LARGE`、`QUERY_TOO_BROAD`、`EXPORT_BUSY` 各有专门文案，其余失败带 request_id。导出区的日期只管导出，
 * 不影响余额卡、流水列表与翻页。
 *
 * ⚠️ 只请求 `/api/v1/customer/*`：不请求任何 `/api/v1/admin/` 路径，不显示成本、毛利或内部字段（INV-7）。
 */

import { keepPreviousData, useMutation, useQuery } from "@tanstack/react-query";
import { Alert, Button, Card, Empty, Form, Input, Skeleton, Space, Typography } from "antd";
import { useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";

import { ApiError } from "../../api/client";
import { QUERY_TOO_BROAD } from "../../api/customerUsage";
import {
  customerTransactionListQueryKey,
  exportCustomerTransactions,
  listCustomerTransactions,
  type CustomerTransactionExportQuery,
} from "../../api/customerWallet";
import { EXPORT_BUSY, EXPORT_TOO_LARGE } from "../../api/download";
import { RequestReference } from "../../components/RequestReference";
import { CustomerTransactionsTable } from "./CustomerTransactionsTable";
import {
  MAX_DATE,
  MAX_RANGE_DAYS,
  MIN_DATE,
  dateRangeProblem,
  defaultDateRange,
  type DateRangeProblem,
} from "./UsagePage";
import { WalletSummaryCard } from "./WalletSummaryCard";

/** 每页行数：与接口的默认值相同（docs/api.md：1–100，默认 20）。 */
export const WALLET_PAGE_SIZE = 20;

/** 导出区的日期。都是 `<input type="date">` 的 `YYYY-MM-DD`。 */
interface ExportRangeValues {
  fromDate: string;
  toDate: string;
}

function RangeProblemText({ problem }: { problem: DateRangeProblem }) {
  const { t } = useTranslation();
  switch (problem) {
    case "missing":
      return t("portal.wallet.export.range.missing");
    case "outOfRange":
      return t("portal.wallet.export.range.outOfRange", { min: MIN_DATE, max: MAX_DATE });
    case "reversed":
      return t("portal.wallet.export.range.reversed");
    case "tooLong":
      return t("portal.wallet.export.range.tooLong", { days: MAX_RANGE_DAYS });
  }
}

/** 导出失败的说明：三种可以由客户自己处理的错误各有专门文案，其余是通用文案（request_id 另外显示）。 */
function ExportFailureText({ error }: { error: Error }) {
  const { t } = useTranslation();
  const code = error instanceof ApiError ? error.code : null;
  switch (code) {
    case EXPORT_TOO_LARGE:
      return t("portal.wallet.export.tooLarge");
    case QUERY_TOO_BROAD:
      return t("portal.wallet.export.tooBroad");
    case EXPORT_BUSY:
      return t("portal.wallet.export.busy");
    default:
      return t("portal.wallet.export.failed");
  }
}

const SPECIFIC_EXPORT_FAILURES: ReadonlySet<string> = new Set([EXPORT_TOO_LARGE, QUERY_TOO_BROAD, EXPORT_BUSY]);

/** 三种专门文案之外的失败：要显示 request_id。 */
function isGenericExportFailure(error: Error): boolean {
  return !(error instanceof ApiError) || !SPECIFIC_EXPORT_FAILURES.has(error.code);
}

function ExportSection() {
  const { t } = useTranslation();
  // 进页面那一刻的「今天」：停在页面上跨过午夜不会自己换区间。
  const [initialRange] = useState(() => defaultDateRange(new Date()));
  const [range, setRange] = useState<ExportRangeValues>(initialRange);

  const fromDate = range.fromDate.trim();
  const toDate = range.toDate.trim();
  const problem = dateRangeProblem(fromDate, toDate);
  const exportQuery: CustomerTransactionExportQuery | null = problem === null ? { fromDate, toDate } : null;

  const exporting = useMutation({
    mutationFn: (query: CustomerTransactionExportQuery) => exportCustomerTransactions(query),
  });

  const startExport = (): void => {
    // 按钮在导出中已禁用；这里再挡一次，连点也只发一个请求。
    if (exportQuery === null || exporting.isPending) {
      return;
    }
    exporting.mutate(exportQuery);
  };

  const changeRange = (_: Partial<ExportRangeValues>, values: Partial<ExportRangeValues>): void => {
    setRange({ fromDate: values.fromDate ?? "", toDate: values.toDate ?? "" });
    // 上一次导出的失败说的是旧的区间，换区间时收起。只收失败：导出中 reset 会让按钮提前恢复可点。
    if (exporting.isError) {
      exporting.reset();
    }
  };

  return (
    // role + aria-label：部署后验收脚本按语义结构找到这一块（不依赖 antd 的类名）。
    <Card role="region" aria-label={t("portal.wallet.export.title")} title={t("portal.wallet.export.title")}>
      <Form<ExportRangeValues>
        name="customerWalletExport"
        layout="vertical"
        initialValues={initialRange}
        onValuesChange={changeRange}
        style={{ display: "flex", flexWrap: "wrap", columnGap: 16, alignItems: "flex-end" }}
      >
        <Form.Item name="fromDate" label={t("portal.wallet.export.fromDate")}>
          <Input type="date" min={MIN_DATE} max={MAX_DATE} />
        </Form.Item>
        <Form.Item name="toDate" label={t("portal.wallet.export.toDate")}>
          <Input type="date" min={MIN_DATE} max={MAX_DATE} />
        </Form.Item>
        <Form.Item>
          <Button
            onClick={startExport}
            loading={exporting.isPending}
            disabled={exportQuery === null || exporting.isPending}
          >
            {t("portal.wallet.export.button")}
          </Button>
        </Form.Item>
      </Form>
      {problem !== null ? <Alert type="warning" showIcon message={<RangeProblemText problem={problem} />} /> : null}
      {exporting.error ? (
        <Alert
          type="error"
          showIcon
          closable
          onClose={() => exporting.reset()}
          message={<ExportFailureText error={exporting.error} />}
          description={isGenericExportFailure(exporting.error) ? <RequestReference error={exporting.error} /> : null}
        />
      ) : null}
    </Card>
  );
}

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
      {/* 导出区在流水上方，自己管自己的日期与失败，不碰流水的游标。 */}
      <ExportSection />
      <TransactionsSection />
    </Space>
  );
}

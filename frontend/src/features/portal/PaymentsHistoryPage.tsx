/**
 * 客户门户支付历史页（`/portal/payments`，AIH-TASK-193；设计闸门 #587 v3 §2「门户页面」）。
 *
 * 语义以 docs/api.md「支付（客户）」为准：读 `GET /api/v1/customer/payments`，**页码**分页，每页 20 条；顺序由后端决定
 * （`created_at` 倒序），前端不重排。页码放在地址的查询串 `page` 里：
 * - `page` 不是正整数（缺失、`0`、非数字）时按第 1 页读，并把地址改成 `page=1`（替换历史记录，不多一步「后退」）。
 * - 页码大于最后一页而 `total` 大于 0 时跳到最后一页（同样替换）。
 * - 上一页 / 下一页按 `total` 判断能不能翻，不照门户其他列表的游标写法。
 *
 * 点一行进付款结果页，路由 state 里带上当前页码（{@link PaymentsHistoryState}）：结果页的「返回支付历史」用它回到同一页。
 * 结果页的地址本身不加参数，与后端建单时拼的 `success_url` 一致。
 *
 * 列：编号、金额（`MoneyText`）、状态（七种客户侧状态各有文案，{@link PaymentStatusText}）、实际入账额、创建时间与付款时间
 * （吉隆坡时间，列头标 MYT）。实际入账额与付款时间为空时显示短横。三种状态都画出来：加载中、失败（带 request_id）、空列表。
 *
 * 收据列（AIH-TASK-204；设计闸门 #589 v2 §2「门户页面」）排在最后：有收据时是收据号与下载按钮，没有时是短横。点下载不进
 * 结果页；下载中只有这一行的按钮不可点，其他行照常可点。下载失败在表格上方显示一条页面级错误（错误码、信息与 request_id），
 * 只留最近一次失败（翻页之后才失败的照样显示），下一次点任一下载或翻页时清除。
 *
 * ⚠️ 只请求 `/api/v1/customer/*`：不请求任何 `/api/v1/admin/` 路径。说明文字不用 antd 的 `Alert`：它带 `role="alert"`，
 * 部署后浏览器验收把它当成错误提示（scripts/acceptance/admin_customers.mjs 的 `check_portal_payments`）。
 */

import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { Alert, Button, Card, Empty, Skeleton, Space, Table, Typography, type TableColumnsType } from "antd";
import { useEffect, useState, type ReactNode, type SyntheticEvent } from "react";
import { useTranslation } from "react-i18next";
import { useNavigate, useSearchParams } from "react-router";

import { ApiError } from "../../api/client";
import {
  customerPaymentListQueryKey,
  downloadCustomerReceipt,
  listCustomerPayments,
  type CustomerPayment,
} from "../../api/customerPayments";
import { DateTimeText } from "../../components/DateTimeText";
import { MoneyText } from "../../components/MoneyText";
import { RequestReference } from "../../components/RequestReference";
import { portalPaymentResultPath } from "../../routes/paths";

/** 每页条数（设计 §2「门户页面」）。 */
export const PAYMENTS_PAGE_SIZE = 20;

/** 地址里装页码的查询参数名。 */
export const PAGE_PARAM = "page";

/** 没有值的格子。纯标点，不进翻译文件。 */
const EMPTY = "—";

/** 正整数的写法：不带前导 0、不带符号与小数点。 */
const POSITIVE_INTEGER = /^[1-9][0-9]*$/;

/** 点一行进付款结果页时带的路由 state：结果页的「返回支付历史」用这个页码。 */
export interface PaymentsHistoryState {
  fromPage: number;
}

/** 查询串里的页码；不是正整数时为 null。 */
export function parsePage(raw: string | null): number | null {
  if (raw === null || !POSITIVE_INTEGER.test(raw)) {
    return null;
  }
  const page = Number(raw);
  return Number.isSafeInteger(page) ? page : null;
}

/** 七种客户侧状态的文案（含义照设计 §2 的状态表）；认不出的状态原样显示，不猜。 */
export function PaymentStatusText({ status }: { status: string }) {
  const { t } = useTranslation();
  switch (status) {
    case "PENDING":
      return t("portal.payments.status.pending");
    case "PAID":
      return t("portal.payments.status.paid");
    case "IN_REVIEW":
      return t("portal.payments.status.inReview");
    case "NOT_CREDITED":
      return t("portal.payments.status.notCredited");
    case "FAILED":
      return t("portal.payments.status.failed");
    case "EXPIRED":
      return t("portal.payments.status.expired");
    case "CANCELLED":
      return t("portal.payments.status.cancelled");
    default:
      return status;
  }
}

/** 实际入账额：没有入账时显示短横。 */
export function CreditedAmount({ payment }: { payment: CustomerPayment }) {
  return payment.credited_amount === null ? EMPTY : (
    <MoneyText amount={payment.credited_amount} currency={payment.currency} />
  );
}

/** 付款时间：还没付时显示短横。 */
export function PaidAt({ payment }: { payment: CustomerPayment }) {
  return payment.paid_at === null ? EMPTY : <DateTimeText value={payment.paid_at} />;
}

function asError(error: unknown): Error {
  return error instanceof Error ? error : new Error(String(error));
}

/** 收据下载失败：错误码、后端的信息与 request_id。 */
export function ReceiptDownloadFailure({ error }: { error: Error }) {
  const { t } = useTranslation();
  return (
    <Alert
      type="error"
      showIcon
      message={t("portal.payments.receipt.downloadFailed")}
      description={
        <Space direction="vertical" size={4}>
          {error instanceof ApiError ? (
            <Typography.Text type="secondary">
              {t("portal.payments.receipt.errorCode")}
              {": "}
              <Typography.Text code>{error.code}</Typography.Text>
            </Typography.Text>
          ) : null}
          <RequestReference error={error} />
        </Space>
      }
    />
  );
}

/** 收据下载按钮。外面包一层挡住点击与按键的冒泡：点它（禁用时也一样）不触发行的「进结果页」。 */
export function ReceiptDownloadButton({ disabled, onDownload }: { disabled: boolean; onDownload: () => void }) {
  const { t } = useTranslation();
  const stop = (event: SyntheticEvent): void => event.stopPropagation();
  return (
    <span onClick={stop} onKeyDown={stop}>
      <Button size="small" disabled={disabled} onClick={onDownload}>
        {t("portal.payments.receipt.download")}
      </Button>
    </span>
  );
}

export function PaymentsHistoryPage() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();

  const rawPage = searchParams.get(PAGE_PARAM);
  const parsedPage = parsePage(rawPage);
  const page = parsedPage ?? 1;

  // page 不是正整数：按第 1 页读（上面已是），并把地址改成 page=1。
  useEffect(() => {
    if (parsedPage === null) {
      setSearchParams(
        (current) => {
          const next = new URLSearchParams(current);
          next.set(PAGE_PARAM, "1");
          return next;
        },
        { replace: true },
      );
    }
  }, [parsedPage, setSearchParams]);

  const payments = useQuery({
    queryKey: customerPaymentListQueryKey(page, PAYMENTS_PAGE_SIZE),
    queryFn: ({ signal }) => listCustomerPayments(page, PAYMENTS_PAGE_SIZE, signal),
    // 翻页时先留着上一页，不让整张表闪成骨架屏。
    placeholderData: keepPreviousData,
    // 理由同钱包页：同一个标签页换账号登录，拿到的必须是新客户的支付。
    gcTime: 0,
  });

  const total = payments.data?.total ?? null;
  const lastPage = total === null ? null : Math.max(1, Math.ceil(total / PAYMENTS_PAGE_SIZE));
  // 只认这一页自己的响应：翻页途中留着的上一页不算。
  const settledOnThisPage = payments.isSuccess && !payments.isPlaceholderData;

  // 页码大于最后一页而 total 大于 0：跳到最后一页。
  useEffect(() => {
    if (settledOnThisPage && total !== null && total > 0 && lastPage !== null && page > lastPage) {
      setSearchParams(
        (current) => {
          const next = new URLSearchParams(current);
          next.set(PAGE_PARAM, String(lastPage));
          return next;
        },
        { replace: true },
      );
    }
  }, [settledOnThisPage, total, lastPage, page, setSearchParams]);

  // 正在下载收据的行（支付 id）；最近一次下载失败。
  const [downloading, setDownloading] = useState<ReadonlySet<string>>(() => new Set());
  const [downloadError, setDownloadError] = useState<Error | null>(null);

  const download = (paymentId: string, receiptId: string, receiptNumber: string): void => {
    setDownloadError(null);
    setDownloading((current) => new Set(current).add(paymentId));
    void (async () => {
      try {
        await downloadCustomerReceipt(receiptId, receiptNumber);
      } catch (error) {
        // 后到的失败覆盖先到的；翻页之后才落定的也照样显示。
        setDownloadError(asError(error));
      } finally {
        setDownloading((current) => {
          const next = new Set(current);
          next.delete(paymentId);
          return next;
        });
      }
    })();
  };

  const goTo = (nextPage: number): void => {
    setDownloadError(null);
    setSearchParams((current) => {
      const next = new URLSearchParams(current);
      next.set(PAGE_PARAM, String(nextPage));
      return next;
    });
  };

  const open = (payment: CustomerPayment): void => {
    const state: PaymentsHistoryState = { fromPage: page };
    void navigate(portalPaymentResultPath(payment.id), { state });
  };

  const columns: TableColumnsType<CustomerPayment> = [
    { key: "id", title: t("portal.payments.field.id"), render: (_: unknown, payment) => payment.id },
    {
      key: "amount",
      title: t("portal.payments.field.amount"),
      render: (_: unknown, payment) => <MoneyText amount={payment.amount} currency={payment.currency} />,
    },
    {
      key: "status",
      title: t("portal.payments.field.status"),
      render: (_: unknown, payment) => <PaymentStatusText status={payment.status} />,
    },
    {
      key: "credited_amount",
      title: t("portal.payments.field.creditedAmount"),
      render: (_: unknown, payment) => <CreditedAmount payment={payment} />,
    },
    {
      key: "created_at",
      title: t("portal.payments.field.createdAt"),
      render: (_: unknown, payment) => <DateTimeText value={payment.created_at} />,
    },
    {
      key: "paid_at",
      title: t("portal.payments.field.paidAt"),
      render: (_: unknown, payment) => <PaidAt payment={payment} />,
    },
    {
      key: "receipt",
      title: t("portal.payments.field.receipt"),
      render: (_: unknown, payment) => {
        const { receipt_id: receiptId, receipt_number: receiptNumber } = payment;
        if (receiptNumber === null) {
          return EMPTY;
        }
        return (
          <Space size="small">
            <span>{receiptNumber}</span>
            {receiptId === null ? null : (
              <ReceiptDownloadButton
                disabled={downloading.has(payment.id)}
                onDownload={() => download(payment.id, receiptId, receiptNumber)}
              />
            )}
          </Space>
        );
      },
    },
  ];

  let result: ReactNode;
  if (payments.isPending) {
    result = (
      <Space direction="vertical" aria-busy="true" style={{ width: "100%" }}>
        <Skeleton active title={false} paragraph={{ rows: 3 }} />
        <Typography.Text type="secondary">{t("portal.payments.loading")}</Typography.Text>
      </Space>
    );
  } else if (payments.isError) {
    result = (
      <Alert
        type="error"
        showIcon
        message={t("portal.payments.loadFailed")}
        description={<RequestReference error={payments.error} />}
        action={
          <Button size="small" onClick={() => void payments.refetch()}>
            {t("portal.payments.retry")}
          </Button>
        }
      />
    );
  } else if (payments.data.total === 0) {
    result = <Empty description={t("portal.payments.empty")} />;
  } else {
    const pageTotal = payments.data.total;
    const pages = Math.max(1, Math.ceil(pageTotal / PAYMENTS_PAGE_SIZE));
    result = (
      <>
        <Table<CustomerPayment>
          rowKey="id"
          columns={columns}
          dataSource={payments.data.items}
          loading={payments.isFetching}
          pagination={false}
          // 页码超过末页时后端回空 items、total 照报；跳到末页之前的那一下说清楚是「这一页没有」。
          locale={{ emptyText: t("portal.payments.emptyPage") }}
          onRow={(payment) => ({
            onClick: () => open(payment),
            onKeyDown: (event) => {
              if (event.key === "Enter" || event.key === " ") {
                event.preventDefault();
                open(payment);
              }
            },
            tabIndex: 0,
            style: { cursor: "pointer" },
          })}
        />
        <Space style={{ marginTop: 16 }}>
          <Button disabled={page <= 1} onClick={() => goTo(Math.min(page - 1, pages))}>
            {t("portal.payments.previous")}
          </Button>
          <Typography.Text type="secondary">
            {t("portal.payments.pageInfo", { page, pages, total: pageTotal })}
          </Typography.Text>
          <Button disabled={page * PAYMENTS_PAGE_SIZE >= pageTotal} onClick={() => goTo(page + 1)}>
            {t("portal.payments.next")}
          </Button>
        </Space>
      </>
    );
  }

  return (
    <Card title={t("portal.payments.title")}>
      <Typography.Paragraph type="secondary">{t("portal.payments.intro")}</Typography.Paragraph>
      {/* 列表与它的状态单独成一块，用例与验收脚本按 aria-label 找它。 */}
      <div role="region" aria-label={t("portal.payments.title")}>
        {downloadError === null ? null : (
          <div style={{ marginBottom: 16 }}>
            <ReceiptDownloadFailure error={downloadError} />
          </div>
        )}
        {result}
      </div>
    </Card>
  );
}

/**
 * 客户门户付款结果页（`/portal/payments/:paymentId`，AIH-TASK-193；设计闸门 #587 v3 §2「门户页面」）。
 *
 * Stripe 付完或取消后跳回这里（后端建单时拼的 `success_url` / `cancel_url`，只多一个查询串 `result`）；支付历史点一行也进
 * 这里。语义以 docs/api.md「支付（客户）」为准：读 `GET /api/v1/customer/payments/{payment_id}`。
 *
 * 刷新（一次读取 = 一个请求，不重试）：
 * - `status` 为 `PENDING` 时每 {@link POLL_INTERVAL_MS} 毫秒再读一次，最多 {@link MAX_REFRESHES} 次（约 2 分钟），之后停止并提示
 *   稍后在支付历史查看；其他状态停止刷新。离开页面时停止计时、放弃在路上的请求。
 * - 第一次读取失败（非 404）：显示错误与 request_id 和重试按钮，不自动刷新。
 * - 刷新中某次读取失败（非 404）：计入次数、继续刷新，页面保留上一次成功的内容；次数用完而最后一次失败时，同时显示「还在
 *   确认中」的提示、这次的错误与 request_id，并给重试按钮。
 * - 重试成功且为 `PENDING` 时从 0 重新计次数。
 * - 任何一次读到 404（`PAYMENT_NOT_FOUND`）：停止刷新，显示找不到这笔支付。
 *
 * 查询串 `result=cancelled` 时在顶部提示你取消了付款（状态仍以接口为准）；`result=success` 不改变任何显示 —— 跳回不代表
 * 已付（spec §43），状态只看接口。
 *
 * 「返回支付历史」回到路由 state 里的页码（支付历史点一行时带上，{@link PaymentsHistoryState}）；没有 state（例如从 Stripe
 * 跳回）时回第 1 页。
 *
 * 收据（AIH-TASK-204；设计闸门 #589 v2 §2「门户页面」）：`status` 为 `PAID` 且有收据时显示收据号与下载按钮（下载中不可再点；
 * 失败在按钮下方显示错误码、信息与 request_id，下一次点下载时清除）；`PAID` 而还没有收据时提示收据正在准备、稍后可在支付历史
 * 下载；其他状态不显示收据区域。刷新规则不因收据改变（只在 `PENDING` 时刷新）。
 *
 * ⚠️ 只请求 `/api/v1/customer/*`：不请求任何 `/api/v1/admin/` 路径。
 */

import { Alert, Button, Card, Descriptions, Skeleton, Space, Typography } from "antd";
import { useEffect, useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { Link, useLocation, useParams, useSearchParams } from "react-router";

import { ApiError } from "../../api/client";
import {
  PAYMENT_NOT_FOUND,
  downloadCustomerReceipt,
  getCustomerPayment,
  type CustomerPayment,
} from "../../api/customerPayments";
import { DateTimeText } from "../../components/DateTimeText";
import { MoneyText } from "../../components/MoneyText";
import { RequestReference } from "../../components/RequestReference";
import { ROUTES } from "../../routes/paths";
import {
  CreditedAmount,
  PAGE_PARAM,
  PaidAt,
  PaymentStatusText,
  ReceiptDownloadButton,
  ReceiptDownloadFailure,
  type PaymentsHistoryState,
} from "./PaymentsHistoryPage";

/** `PENDING` 时两次读取之间的间隔（设计 §2「门户页面」）。 */
export const POLL_INTERVAL_MS = 3_000;

/** `PENDING` 时最多再读几次（第一次读取不算）。 */
export const MAX_REFRESHES = 40;

/** 查询串里的跳回结果：只认 `cancelled`。 */
export const RESULT_PARAM = "result";
const RESULT_CANCELLED = "cancelled";

const PENDING_STATUS = "PENDING";
const PAID_STATUS = "PAID";

type ResultState =
  | { kind: "loading" }
  | { kind: "notFound" }
  /** 还没有成功读到过：错误与重试按钮。 */
  | { kind: "failed"; error: Error }
  /**
   * 读到过：`payment` 是最近一次成功的内容；`error` 是最近一次读取的失败（成功后清掉）；`polling` 为真时还会再读；
   * `exhausted` 为真时次数已用完、仍是 `PENDING`。
   */
  | { kind: "loaded"; payment: CustomerPayment; error: Error | null; polling: boolean; exhausted: boolean };

function isNotFound(error: Error): boolean {
  return error instanceof ApiError && error.code === PAYMENT_NOT_FOUND;
}

function asError(error: unknown): Error {
  return error instanceof Error ? error : new Error(String(error));
}

/** 路由 state 里的页码：正整数才认，否则回第 1 页。 */
function historyPage(state: unknown): number {
  if (typeof state === "object" && state !== null && "fromPage" in state) {
    const page = (state as PaymentsHistoryState).fromPage;
    if (typeof page === "number" && Number.isSafeInteger(page) && page >= 1) {
      return page;
    }
  }
  return 1;
}

/**
 * 读一笔支付并按上面的规则刷新。每重试一次就重新开始一轮：次数从 0 计。卸载或换一轮时停止计时、放弃在路上的请求，之后回来的
 * 结果一律不用。`retrying` 在重试的那一次读取落定之前为真。
 */
function usePaymentResult(paymentId: string): { state: ResultState; retrying: boolean; retry: () => void } {
  const [state, setState] = useState<ResultState>({ kind: "loading" });
  const [attempt, setAttempt] = useState(0);
  const [retrying, setRetrying] = useState(false);

  useEffect(() => {
    let active = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let controller: AbortController | undefined;
    let refreshes = 0;

    function schedule(): void {
      timer = setTimeout(() => {
        refreshes += 1;
        void read(true);
      }, POLL_INTERVAL_MS);
    }

    async function read(refresh: boolean): Promise<void> {
      controller = new AbortController();
      try {
        const payment = await getCustomerPayment(paymentId, controller.signal);
        if (!active) {
          return;
        }
        const pending = payment.status === PENDING_STATUS;
        const exhausted = pending && refreshes >= MAX_REFRESHES;
        setState({ kind: "loaded", payment, error: null, polling: pending && !exhausted, exhausted });
        if (pending && !exhausted) {
          schedule();
        }
      } catch (caught) {
        if (!active) {
          return;
        }
        const error = asError(caught);
        if (isNotFound(error)) {
          setState({ kind: "notFound" });
          return;
        }
        if (!refresh) {
          // 一轮的第一次读取失败：不自动刷新。读到过的内容（次数用完后的重试）照留，给重试按钮。
          setState((previous) =>
            previous.kind === "loaded"
              ? { ...previous, error, polling: false, exhausted: true }
              : { kind: "failed", error },
          );
          return;
        }
        // 刷新中失败：计入次数、保留上一次成功的内容，次数没用完就接着刷新。
        const exhausted = refreshes >= MAX_REFRESHES;
        setState((previous) =>
          previous.kind === "loaded" ? { ...previous, error, polling: !exhausted, exhausted } : previous,
        );
        if (!exhausted) {
          schedule();
        }
      } finally {
        if (active && !refresh) {
          setRetrying(false);
        }
      }
    }

    void read(false);

    return () => {
      active = false;
      clearTimeout(timer);
      controller?.abort();
    };
  }, [paymentId, attempt]);

  const retry = (): void => {
    setRetrying(true);
    setAttempt((current) => current + 1);
  };

  return { state, retrying, retry };
}

function PaymentDetails({ payment }: { payment: CustomerPayment }) {
  const { t } = useTranslation();
  return (
    <Descriptions
      bordered
      size="small"
      column={1}
      items={[
        { key: "id", label: t("portal.payments.field.id"), children: payment.id },
        {
          key: "amount",
          label: t("portal.payments.field.amount"),
          children: <MoneyText amount={payment.amount} currency={payment.currency} />,
        },
        {
          key: "status",
          label: t("portal.payments.field.status"),
          children: <PaymentStatusText status={payment.status} />,
        },
        {
          key: "credited_amount",
          label: t("portal.payments.field.creditedAmount"),
          children: <CreditedAmount payment={payment} />,
        },
        {
          key: "created_at",
          label: t("portal.payments.field.createdAt"),
          children: <DateTimeText value={payment.created_at} />,
        },
        { key: "paid_at", label: t("portal.payments.field.paidAt"), children: <PaidAt payment={payment} /> },
      ]}
    />
  );
}

/** 已付支付的收据：有收据时收据号与下载按钮，还没有时说明正在准备。只在 `PAID` 时画出来。 */
function ReceiptSection({ payment }: { payment: CustomerPayment }) {
  const { t } = useTranslation();
  const [downloading, setDownloading] = useState(false);
  const [error, setError] = useState<Error | null>(null);

  const { receipt_id: receiptId, receipt_number: receiptNumber } = payment;
  if (receiptId === null || receiptNumber === null) {
    return <Typography.Text type="secondary">{t("portal.paymentResult.receiptPreparing")}</Typography.Text>;
  }

  const download = (): void => {
    setError(null);
    setDownloading(true);
    void (async () => {
      try {
        await downloadCustomerReceipt(receiptId, receiptNumber);
      } catch (caught) {
        setError(asError(caught));
      } finally {
        setDownloading(false);
      }
    })();
  };

  return (
    <Space direction="vertical" size="small" style={{ width: "100%" }}>
      <Space size="small">
        <Typography.Text>
          {t("portal.paymentResult.receiptNumber")}
          {": "}
          {receiptNumber}
        </Typography.Text>
        <ReceiptDownloadButton disabled={downloading} onDownload={download} />
      </Space>
      {error === null ? null : <ReceiptDownloadFailure error={error} />}
    </Space>
  );
}

function PaymentResult({ paymentId }: { paymentId: string }) {
  const { t } = useTranslation();
  const location = useLocation();
  const [searchParams] = useSearchParams();
  const { state, retrying, retry } = usePaymentResult(paymentId);

  const retryButton = (
    <Button size="small" loading={retrying} onClick={retry}>
      {t("portal.paymentResult.retry")}
    </Button>
  );

  let body: ReactNode;
  switch (state.kind) {
    case "loading":
      body = (
        <Space direction="vertical" aria-busy="true" style={{ width: "100%" }}>
          <Skeleton active title={false} paragraph={{ rows: 3 }} />
          <Typography.Text type="secondary">{t("portal.paymentResult.loading")}</Typography.Text>
        </Space>
      );
      break;
    case "notFound":
      body = <Alert type="warning" showIcon message={t("portal.paymentResult.notFound")} />;
      break;
    case "failed":
      body = (
        <Alert
          type="error"
          showIcon
          message={t("portal.paymentResult.loadFailed")}
          description={<RequestReference error={state.error} />}
          action={retryButton}
        />
      );
      break;
    case "loaded":
      body = (
        <Space direction="vertical" size="middle" style={{ width: "100%" }}>
          {state.polling ? (
            <Typography.Text type="secondary">{t("portal.paymentResult.waiting")}</Typography.Text>
          ) : null}
          {state.exhausted ? (
            <Alert type="info" showIcon message={t("portal.paymentResult.stillConfirming")} />
          ) : null}
          {/* 刷新中途的失败不打扰：只在不再自动刷新时把这次的错误摆出来，给重试按钮。 */}
          {state.exhausted && state.error !== null ? (
            <Alert
              type="error"
              showIcon
              message={t("portal.paymentResult.loadFailed")}
              description={<RequestReference error={state.error} />}
              action={retryButton}
            />
          ) : null}
          <PaymentDetails payment={state.payment} />
          {state.payment.status === PAID_STATUS ? <ReceiptSection payment={state.payment} /> : null}
        </Space>
      );
      break;
  }

  const backTo = `${ROUTES.portalPayments}?${new URLSearchParams({ [PAGE_PARAM]: String(historyPage(location.state)) }).toString()}`;

  return (
    <Card role="region" aria-label={t("portal.paymentResult.title")} title={t("portal.paymentResult.title")}>
      <Space direction="vertical" size="middle" style={{ width: "100%" }}>
        {searchParams.get(RESULT_PARAM) === RESULT_CANCELLED ? (
          <Alert type="info" showIcon message={t("portal.paymentResult.cancelled")} />
        ) : null}
        {body}
        <Space>
          <Link to={backTo}>{t("portal.paymentResult.backToHistory")}</Link>
          <Link to={ROUTES.portalTopup}>{t("portal.paymentResult.topUpAgain")}</Link>
        </Space>
      </Space>
    </Card>
  );
}

export function PaymentResultPage() {
  const { paymentId = "" } = useParams();
  // 换一笔支付就从头来：状态、次数与重试都不沿用。
  return <PaymentResult key={paymentId} paymentId={paymentId} />;
}

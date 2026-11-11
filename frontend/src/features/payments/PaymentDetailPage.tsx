/**
 * 管理端支付详情页（`/payments/:paymentId`，AIH-TASK-185；设计闸门 #585 v3 §2「管理端页面」）。
 *
 * 语义以 docs/api.md「支付（管理端）」为准：支付详情（`GET /api/v1/admin/payments/{payment_id}`）的全部字段与关联的
 * 回调事件表；状态是 `REVIEW_REQUIRED` 时显示复核面板（`POST …/resolve`），其他状态不显示。
 *
 * 复核面板：「按实付入账」（`CREDIT`）与「不入账结案」（`CLOSE`）共用一个原因输入框，去掉首尾空白后 1–255 字才可点。
 * 点了先弹二次确认；按实付入账的确认框写明入账额以提交时 Stripe 的实收为准，并把当前的 `confirmed_amount` 按
 * `confirmed_currency` 作参考（金额为空说 Stripe 未报金额；只有币种为空时只显示数字并标币种未知；不是 MYR 照样按它的
 * 币种显示，后端会回 409 `CURRENCY_NOT_MYR`）。成功后用返回的详情直接刷新页面（状态已不是复核，面板随之收起），列表
 * 一并过期；失败时关掉确认框，在面板里显示后端的错误码、信息与 request_id，原因保留，可以改了再点。
 *
 * 金额用 `MoneyText`，币种取返回的字段；时间按吉隆坡时间（UTC+8）显示，标签标 MYT。状态原因、结案方式与事件的
 * 处理状态原样显示代码。加载中、失败（带 request_id）、404 三种状态都画出来。只请求 `/api/v1/admin/` 路径。
 *
 * ⚠️ 结案不自动重试（`api/adminPayments.ts`）：要不要重发由管理员决定。
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Alert,
  Button,
  Card,
  Descriptions,
  Empty,
  Form,
  Input,
  Modal,
  Result,
  Space,
  Table,
  Tag,
  Typography,
  type DescriptionsProps,
  type TableColumnsType,
} from "antd";
import { useRef, useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { Link, useParams } from "react-router";

import { ADMIN_REQUIRED } from "../../api/adminCustomers";
import {
  PAYMENT_LISTS_QUERY_KEY,
  PAYMENT_NOT_FOUND,
  getPayment,
  paymentDetailQueryKey,
  resolvePayment,
  type PaymentDetail,
  type PaymentEvent,
  type ResolveAction,
} from "../../api/adminPayments";
import { ApiError } from "../../api/client";
import { DateTimeText } from "../../components/DateTimeText";
import { MoneyText, formatAmount } from "../../components/MoneyText";
import { RequestReference } from "../../components/RequestReference";
import { ROUTES, customerDetailPath } from "../../routes/paths";
import { CodeText, LoadingBlock, NoticeAlert } from "../catalog/ProvidersPage";

/** 没有值的格子。纯标点，不进翻译文件。 */
const EMPTY = "—";

const REVIEW_REQUIRED = "REVIEW_REQUIRED";

/** 原因的上限（去掉首尾空白后，按字符计，与后端的长度规则一致）。 */
const REASON_MAX = 255;

/** 原因去掉首尾空白后在 1–255 字之内。 */
function isValidReason(reason: string): boolean {
  const length = Array.from(reason.trim()).length;
  return length >= 1 && length <= REASON_MAX;
}

/** 六种状态的文案（与列表页同一组键）；认不出的值原样返回。 */
function useStatusLabel(): (status: string) => string {
  const { t } = useTranslation();
  return (status) => {
    switch (status) {
      case "PENDING":
        return t("payments.status.pending");
      case "PAID":
        return t("payments.status.paid");
      case "FAILED":
        return t("payments.status.failed");
      case "EXPIRED":
        return t("payments.status.expired");
      case "CANCELLED":
        return t("payments.status.cancelled");
      case "REVIEW_REQUIRED":
        return t("payments.status.reviewRequired");
      default:
        return status;
    }
  };
}

function statusColor(status: string): string {
  switch (status) {
    case "PAID":
      return "success";
    case "FAILED":
      return "error";
    case "REVIEW_REQUIRED":
      return "warning";
    case "PENDING":
      return "processing";
    default:
      return "default";
  }
}

function StatusTag({ status }: { status: string }) {
  const statusLabel = useStatusLabel();
  return <Tag color={statusColor(status)}>{statusLabel(status)}</Tag>;
}

function OptionalCode({ value }: { value: string | null }) {
  return value === null ? <>{EMPTY}</> : <CodeText code={value} />;
}

function OptionalTime({ value }: { value: string | null }) {
  return value === null ? <>{EMPTY}</> : <DateTimeText value={value} />;
}

function OptionalMoney({ amount, currency }: { amount: string | null; currency: string }) {
  return amount === null ? <>{EMPTY}</> : <MoneyText amount={amount} currency={currency} />;
}

/**
 * Stripe 报的金额：有币种按它的币种显示（不是 MYR 也照样）；金额不为空而币种为空时只显示数字并标「币种未知」；
 * 金额为空时显示 `missing`。
 */
function ConfirmedAmount({ payment, missing }: { payment: PaymentDetail; missing: string }) {
  const { t } = useTranslation();
  const amount = payment.confirmed_amount;
  if (amount === null) {
    return <>{missing}</>;
  }
  if (payment.confirmed_currency === null) {
    return (
      <Space size={4}>
        <span style={{ fontVariantNumeric: "tabular-nums" }}>{formatAmount(amount)}</span>
        <Tag>{t("payments.currencyUnknown")}</Tag>
      </Space>
    );
  }
  return <MoneyText amount={amount} currency={payment.confirmed_currency} />;
}

/** 接口出错时的提示：403 说没有权限，其余用调用方给的标题。都带后端的错误码、message 与 request_id。 */
function PaymentErrorAlert({
  error,
  failedText,
  action,
}: {
  error: Error;
  failedText: string;
  action?: ReactNode;
}) {
  const { t } = useTranslation();
  const forbidden = error instanceof ApiError && error.code === ADMIN_REQUIRED;
  return (
    <Alert
      type={forbidden ? "warning" : "error"}
      showIcon
      style={{ marginBottom: 16 }}
      message={forbidden ? t("payments.forbidden") : failedText}
      description={
        <Space direction="vertical" size={4}>
          {error instanceof ApiError ? <CodeText code={error.code} /> : null}
          <RequestReference error={error} />
        </Space>
      }
      action={forbidden ? undefined : action}
    />
  );
}

export function PaymentDetailPage() {
  const { t } = useTranslation();
  const { paymentId = "" } = useParams();
  const queryClient = useQueryClient();
  const [notice, setNotice] = useState<string | null>(null);

  const payment = useQuery({
    queryKey: paymentDetailQueryKey(paymentId),
    queryFn: ({ signal }) => getPayment(paymentId, signal),
  });

  const backLink = <Link to={ROUTES.payments}>{t("payments.detail.backToList")}</Link>;

  if (payment.isPending) {
    return (
      <Card>
        <LoadingBlock text={t("payments.detail.loading")} />
      </Card>
    );
  }

  if (payment.isError) {
    const { error } = payment;
    // 404 是一个明确的答案（没有这笔支付），不是「出错了、请重试」。
    if (error instanceof ApiError && error.code === PAYMENT_NOT_FOUND) {
      return (
        <Result
          status="404"
          title={t("payments.detail.notFound")}
          subTitle={t("payments.detail.notFoundBody")}
          extra={backLink}
        />
      );
    }
    return (
      <Card>
        <PaymentErrorAlert
          error={error}
          failedText={t("payments.detail.loadFailed")}
          action={
            <Button size="small" onClick={() => void payment.refetch()}>
              {t("payments.retry")}
            </Button>
          }
        />
        {backLink}
      </Card>
    );
  }

  const current = payment.data;

  const resolved = (detail: PaymentDetail): void => {
    // 用返回的详情直接刷新，不再多问一次；列表里的状态与结案方式也变了。
    queryClient.setQueryData(paymentDetailQueryKey(paymentId), detail);
    void queryClient.invalidateQueries({ queryKey: PAYMENT_LISTS_QUERY_KEY });
    setNotice(t("payments.review.done"));
  };

  return (
    <Space direction="vertical" size="middle" style={{ width: "100%" }}>
      {backLink}
      <NoticeAlert message={notice} />
      <Card
        title={
          <Space size={4} wrap>
            <CodeText code={current.id} />
            <StatusTag status={current.status} />
          </Space>
        }
      >
        <PaymentDescriptions payment={current} />
      </Card>
      {current.status === REVIEW_REQUIRED ? (
        <ReviewPanel
          payment={current}
          onResolved={resolved}
          onReload={() => {
            setNotice(null);
            void payment.refetch();
          }}
        />
      ) : null}
      <Card title={t("payments.detail.events")}>
        <EventsTable events={current.events} />
      </Card>
    </Space>
  );
}

function PaymentDescriptions({ payment }: { payment: PaymentDetail }) {
  const { t } = useTranslation();
  const items: DescriptionsProps["items"] = [
    { key: "id", label: t("payments.field.id"), children: <CodeText code={payment.id} /> },
    {
      key: "customer",
      label: t("payments.field.customer"),
      children: <Link to={customerDetailPath(payment.customer_id)}>{payment.customer_name}</Link>,
    },
    { key: "customer_id", label: t("payments.field.customerId"), children: <CodeText code={payment.customer_id} /> },
    {
      key: "amount",
      label: t("payments.field.amount"),
      children: <MoneyText amount={payment.amount} currency={payment.currency} />,
    },
    { key: "status", label: t("payments.field.status"), children: <StatusTag status={payment.status} /> },
    {
      key: "status_reason",
      label: t("payments.field.statusReason"),
      children: <OptionalCode value={payment.status_reason} />,
    },
    { key: "resolution", label: t("payments.field.resolution"), children: <OptionalCode value={payment.resolution} /> },
    {
      key: "confirmed_amount",
      label: t("payments.field.confirmedAmount"),
      children: <ConfirmedAmount payment={payment} missing={EMPTY} />,
    },
    {
      key: "credited_amount",
      label: t("payments.field.creditedAmount"),
      children: <OptionalMoney amount={payment.credited_amount} currency={payment.currency} />,
    },
    {
      key: "gateway_fee",
      label: t("payments.field.gatewayFee"),
      children: <OptionalMoney amount={payment.gateway_fee} currency={payment.currency} />,
    },
    { key: "gateway", label: t("payments.field.gateway"), children: <CodeText code={payment.gateway} /> },
    {
      key: "gateway_reference",
      label: t("payments.field.gatewayReference"),
      children: <OptionalCode value={payment.gateway_reference} />,
    },
    {
      key: "gateway_payment_intent_id",
      label: t("payments.field.paymentIntent"),
      children: <OptionalCode value={payment.gateway_payment_intent_id} />,
    },
    { key: "created_at", label: t("payments.field.createdAt"), children: <DateTimeText value={payment.created_at} /> },
    { key: "expires_at", label: t("payments.field.expiresAt"), children: <DateTimeText value={payment.expires_at} /> },
    { key: "paid_at", label: t("payments.field.paidAt"), children: <OptionalTime value={payment.paid_at} /> },
    {
      key: "review_required_at",
      label: t("payments.field.reviewRequiredAt"),
      children: <OptionalTime value={payment.review_required_at} />,
    },
    {
      key: "last_reconciled_at",
      label: t("payments.field.lastReconciledAt"),
      children: <OptionalTime value={payment.last_reconciled_at} />,
    },
    { key: "resolved_at", label: t("payments.field.resolvedAt"), children: <OptionalTime value={payment.resolved_at} /> },
    { key: "resolved_by", label: t("payments.field.resolvedBy"), children: payment.resolved_by ?? EMPTY },
    {
      key: "resolution_reason",
      label: t("payments.field.resolutionReason"),
      children: payment.resolution_reason ?? EMPTY,
    },
  ];
  return <Descriptions bordered size="small" column={1} items={items} />;
}

function EventsTable({ events }: { events: readonly PaymentEvent[] }) {
  const { t } = useTranslation();
  if (events.length === 0) {
    return <Empty description={t("payments.detail.noEvents")} />;
  }
  const columns: TableColumnsType<PaymentEvent> = [
    {
      key: "gateway_event_id",
      title: t("payments.event.id"),
      render: (_: unknown, event) => <CodeText code={event.gateway_event_id} />,
    },
    { key: "event_type", title: t("payments.event.type"), dataIndex: "event_type" },
    { key: "processing_status", title: t("payments.event.processingStatus"), dataIndex: "processing_status" },
    {
      key: "last_error_code",
      title: t("payments.event.lastErrorCode"),
      render: (_: unknown, event) => event.last_error_code ?? EMPTY,
    },
    {
      key: "received_at",
      title: t("payments.event.receivedAt"),
      render: (_: unknown, event) => <DateTimeText value={event.received_at} />,
    },
    {
      key: "processed_at",
      title: t("payments.event.processedAt"),
      render: (_: unknown, event) => <OptionalTime value={event.processed_at} />,
    },
    {
      key: "fingerprint_conflict_at",
      title: t("payments.event.fingerprintConflictAt"),
      render: (_: unknown, event) => <OptionalTime value={event.fingerprint_conflict_at} />,
    },
  ];
  return (
    <Table<PaymentEvent>
      size="small"
      rowKey="gateway_event_id"
      columns={columns}
      dataSource={[...events]}
      pagination={false}
    />
  );
}

interface ResolveVariables {
  action: ResolveAction;
  reason: string;
}

/**
 * 复核面板。确认进行中禁用按钮、不许关闭，并用 ref 挡住第二次点击（`isPending` 要等下一次渲染才变 true）。
 * 失败时关掉确认框、在面板里显示错误，原因保留；成功交给 `onResolved`。
 */
function ReviewPanel({
  payment,
  onResolved,
  onReload,
}: {
  payment: PaymentDetail;
  onResolved: (detail: PaymentDetail) => void;
  onReload: () => void;
}) {
  const { t } = useTranslation();
  const [reason, setReason] = useState("");
  const [confirming, setConfirming] = useState<ResolveAction | null>(null);
  const inFlight = useRef(false);

  const resolve = useMutation({
    mutationFn: (variables: ResolveVariables) => resolvePayment(payment.id, variables),
    onSuccess: (detail) => {
      setConfirming(null);
      onResolved(detail);
    },
    onError: () => setConfirming(null),
    onSettled: () => {
      inFlight.current = false;
    },
  });

  const trimmed = reason.trim();
  const valid = isValidReason(reason);
  const tooLong = Array.from(trimmed).length > REASON_MAX;
  const pending = resolve.isPending;

  const open = (action: ResolveAction): void => {
    if (valid && !pending) {
      setConfirming(action);
    }
  };

  const confirm = (): void => {
    if (confirming === null || inFlight.current) {
      return;
    }
    inFlight.current = true;
    resolve.mutate({ action: confirming, reason: trimmed });
  };

  const back = (): void => {
    if (!pending) {
      setConfirming(null);
    }
  };

  return (
    <div role="region" aria-label={t("payments.review.title")}>
      <Card title={t("payments.review.title")}>
        <Typography.Paragraph type="secondary">{t("payments.review.intro")}</Typography.Paragraph>
        {resolve.isError ? (
          <PaymentErrorAlert
            error={resolve.error}
            failedText={t("payments.review.failed")}
            action={
              <Button size="small" onClick={onReload}>
                {t("payments.review.reload")}
              </Button>
            }
          />
        ) : null}
        <Form layout="vertical">
          <Form.Item
            label={t("payments.review.reason")}
            htmlFor="payment-review-reason"
            extra={tooLong ? undefined : t("payments.review.reasonHint")}
            validateStatus={tooLong ? "error" : ""}
            help={tooLong ? t("payments.review.reasonTooLong") : undefined}
          >
            <Input.TextArea
              id="payment-review-reason"
              rows={3}
              value={reason}
              disabled={pending}
              onChange={(event) => setReason(event.target.value)}
            />
          </Form.Item>
        </Form>
        <Space wrap>
          <Button type="primary" disabled={!valid || pending} onClick={() => open("CREDIT")}>
            {t("payments.review.credit")}
          </Button>
          <Button danger disabled={!valid || pending} onClick={() => open("CLOSE")}>
            {t("payments.review.close")}
          </Button>
        </Space>
      </Card>
      {confirming === null ? null : (
      <Modal
        open
        title={confirming === "CREDIT" ? t("payments.review.creditTitle") : t("payments.review.closeTitle")}
        onCancel={back}
        maskClosable={false}
        closable={!pending}
        keyboard={!pending}
        footer={[
          <Button key="back" onClick={back} disabled={pending}>
            {t("payments.review.cancel")}
          </Button>,
          <Button key="confirm" type="primary" danger onClick={confirm} loading={pending} disabled={pending}>
            {t("payments.review.confirm")}
          </Button>,
        ]}
      >
        <Descriptions
          bordered
          size="small"
          column={1}
          style={{ marginBottom: 16 }}
          items={[
            { key: "id", label: t("payments.field.id"), children: <CodeText code={payment.id} /> },
            { key: "reason", label: t("payments.review.reason"), children: trimmed },
            ...(confirming === "CREDIT"
              ? [
                  {
                    key: "reference",
                    label: t("payments.review.reference"),
                    children: <ConfirmedAmount payment={payment} missing={t("payments.review.noAmount")} />,
                  },
                ]
              : []),
          ]}
        />
        <Typography.Paragraph>
          {confirming === "CREDIT" ? t("payments.review.creditBody") : t("payments.review.closeBody")}
        </Typography.Paragraph>
      </Modal>
      )}
    </div>
  );
}

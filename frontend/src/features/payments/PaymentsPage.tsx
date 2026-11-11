/**
 * 管理端支付列表页（`/payments`，AIH-TASK-184；设计闸门 #585 v3 §2「管理端页面」）。
 *
 * 语义以 docs/api.md「支付（管理端）」为准：支付列表（`GET /api/v1/admin/payments`），可按状态（下拉：全部与六种）、
 * 客户（下拉，客户列表逐页取全）与创建日期起止（`YYYY-MM-DD`，马来西亚时间的日历日、含两端，原样发给接口）筛。
 * 日期形状不对、或起晚于止时不发请求、只在输入框下提示。每页 20 条，只有上一页 / 下一页，按 total 判断可不可用；
 * 改任一筛选回到第 1 页。顺序由后端决定，前端不重排。点一行跳到这笔支付的详情页（AIH-TASK-185）。
 *
 * 金额与手续费用 `MoneyText`，币种取返回的 `currency`；手续费为空显示短横。状态用文案；状态原因与结案方式原样显示代码
 * （状态原因的取值不固定，不翻译）。时间按吉隆坡时间（UTC+8）显示，列头标 MYT。
 *
 * 三种状态都画出来（spec §132 DoD 第 8 条）：加载中、失败（带 request_id）、空列表。403 说「没有权限」。只请求
 * `/api/v1/admin/` 路径。
 *
 * ⚠️ 说明不用 antd 的 `Alert`：它带 `role="alert"`，部署后浏览器验收把页面上任何 `role="alert"` 都当成错误提示
 * （scripts/acceptance/admin_customers.mjs 的 open_payments）。日期的格式提示放在 `extra` 里，只有不对时才进 `help`。
 */

import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { Alert, Button, Card, Empty, Form, Input, Select, Skeleton, Space, Table, Tag, Typography, type TableColumnsType } from "antd";
import { useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { useNavigate } from "react-router";

import { ADMIN_REQUIRED, DEFAULT_PAGE_SIZE } from "../../api/adminCustomers";
import {
  CREATED_DATE_PATTERN,
  PAYMENT_STATUSES,
  isPaymentStatus,
  listPayments,
  paymentListQueryKey,
  type PaymentFilter,
  type PaymentSummary,
} from "../../api/adminPayments";
import { ApiError } from "../../api/client";
import { DateTimeText } from "../../components/DateTimeText";
import { MoneyText } from "../../components/MoneyText";
import { RequestReference } from "../../components/RequestReference";
import { paymentDetailPath } from "../../routes/paths";
import { customerOptions, useAllCustomers } from "../pricing/PricingRulesPage";

/** 没有值的格子。纯标点，不进翻译文件。 */
const EMPTY = "—";

/** 状态下拉里的「全部」。不进查询串。 */
const ALL = "ALL";

/** 六种状态的文案（键名写成字面量，keys.test.ts 才认得出）；认不出的值原样返回。 */
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

function Loading({ text }: { text: string }) {
  return (
    <>
      <Skeleton active title={false} paragraph={{ rows: 3 }} />
      <Typography.Text type="secondary">{text}</Typography.Text>
    </>
  );
}

/** 支付接口出错时的提示：403 说没有权限，其余用调用方给的标题。都带后端 message 与 request_id。 */
function PaymentErrorAlert({
  error,
  failedText,
  onRetry,
}: {
  error: Error;
  failedText: string;
  onRetry: () => void;
}) {
  const { t } = useTranslation();
  const forbidden = error instanceof ApiError && error.code === ADMIN_REQUIRED;
  return (
    <Alert
      type={forbidden ? "warning" : "error"}
      showIcon
      style={{ marginBottom: 16 }}
      message={forbidden ? t("payments.forbidden") : failedText}
      description={<RequestReference error={error} />}
      action={
        // 无权限重试也一样，不给按钮。
        forbidden ? undefined : (
          <Button size="small" onClick={onRetry}>
            {t("payments.retry")}
          </Button>
        )
      }
    />
  );
}

interface FilterValues {
  status: string;
  customer_id: string | undefined;
  created_from: string;
  created_to: string;
}

export function PaymentsPage() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const statusLabel = useStatusLabel();
  const customers = useAllCustomers();
  const [status, setStatus] = useState<string>(ALL);
  const [customerId, setCustomerId] = useState<string | undefined>(undefined);
  const [fromText, setFromText] = useState("");
  const [toText, setToText] = useState("");
  const [page, setPage] = useState(1);

  const createdFrom = fromText.trim();
  const createdTo = toText.trim();
  // 形状不对、或起晚于止时不发请求，只在输入框下提示。空着就是不按那一端筛。同形状的日期串按字典序比较就是按日期比较。
  const fromValid = createdFrom === "" || CREATED_DATE_PATTERN.test(createdFrom);
  const toValid = createdTo === "" || CREATED_DATE_PATTERN.test(createdTo);
  const reversed = fromValid && toValid && createdFrom !== "" && createdTo !== "" && createdFrom > createdTo;
  const datesValid = fromValid && toValid && !reversed;

  const filter: PaymentFilter = {
    ...(isPaymentStatus(status) ? { status } : {}),
    ...(customerId === undefined ? {} : { customer_id: customerId }),
    ...(createdFrom === "" ? {} : { created_from: createdFrom }),
    ...(createdTo === "" ? {} : { created_to: createdTo }),
  };

  const payments = useQuery({
    queryKey: paymentListQueryKey(filter, page, DEFAULT_PAGE_SIZE),
    queryFn: ({ signal }) => listPayments(filter, page, DEFAULT_PAGE_SIZE, signal),
    enabled: datesValid,
    // 翻页时先留着上一页，不让整张表闪成骨架屏。
    placeholderData: keepPreviousData,
  });

  const changeFilter = (changed: Partial<FilterValues>): void => {
    if ("status" in changed) {
      setStatus(changed.status ?? ALL);
    }
    if ("customer_id" in changed) {
      setCustomerId(changed.customer_id);
    }
    if ("created_from" in changed) {
      setFromText(changed.created_from ?? "");
    }
    if ("created_to" in changed) {
      setToText(changed.created_to ?? "");
    }
    setPage(1);
  };

  const open = (payment: PaymentSummary): void => {
    void navigate(paymentDetailPath(payment.id));
  };

  const columns: TableColumnsType<PaymentSummary> = [
    { key: "id", title: t("payments.field.id"), dataIndex: "id" },
    { key: "customer_name", title: t("payments.field.customer"), dataIndex: "customer_name" },
    {
      key: "amount",
      title: t("payments.field.amount"),
      render: (_: unknown, payment) => <MoneyText amount={payment.amount} currency={payment.currency} />,
    },
    {
      key: "gateway_fee",
      title: t("payments.field.gatewayFee"),
      render: (_: unknown, payment) =>
        payment.gateway_fee === null ? EMPTY : <MoneyText amount={payment.gateway_fee} currency={payment.currency} />,
    },
    {
      key: "status",
      title: t("payments.field.status"),
      render: (_: unknown, payment) => <Tag color={statusColor(payment.status)}>{statusLabel(payment.status)}</Tag>,
    },
    {
      key: "status_reason",
      title: t("payments.field.statusReason"),
      render: (_: unknown, payment) => payment.status_reason ?? EMPTY,
    },
    {
      key: "resolution",
      title: t("payments.field.resolution"),
      render: (_: unknown, payment) => payment.resolution ?? EMPTY,
    },
    {
      key: "created_at",
      title: t("payments.field.createdAt"),
      render: (_: unknown, payment) => <DateTimeText value={payment.created_at} />,
    },
    {
      key: "paid_at",
      title: t("payments.field.paidAt"),
      render: (_: unknown, payment) => (payment.paid_at === null ? EMPTY : <DateTimeText value={payment.paid_at} />),
    },
  ];

  const filtered = Object.keys(filter).length > 0;

  let result: ReactNode;
  if (!datesValid) {
    // 不发请求：输入框下面已经说了原因。
    result = null;
  } else if (payments.isPending) {
    result = <Loading text={t("payments.list.loading")} />;
  } else if (payments.isError) {
    result = (
      <PaymentErrorAlert
        error={payments.error}
        failedText={t("payments.list.loadFailed")}
        onRetry={() => void payments.refetch()}
      />
    );
  } else if (payments.data.total === 0) {
    result = <Empty description={filtered ? t("payments.list.emptyFiltered") : t("payments.list.empty")} />;
  } else {
    const total = payments.data.total;
    result = (
      <>
        <Table<PaymentSummary>
          rowKey="id"
          columns={columns}
          dataSource={payments.data.items}
          loading={payments.isFetching}
          pagination={false}
          // 页码超过末页时后端回空 items、total 照报：说清楚是「这一页没有」。
          locale={{ emptyText: t("payments.list.emptyPage") }}
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
          <Button disabled={page <= 1} onClick={() => setPage(page - 1)}>
            {t("payments.list.previous")}
          </Button>
          <Typography.Text type="secondary">{t("payments.list.pageInfo", { page, total })}</Typography.Text>
          <Button disabled={page * DEFAULT_PAGE_SIZE >= total} onClick={() => setPage(page + 1)}>
            {t("payments.list.next")}
          </Button>
        </Space>
      </>
    );
  }

  let toHelp: string | undefined;
  if (!toValid) {
    toHelp = t("payments.filter.dateInvalid");
  } else if (reversed) {
    toHelp = t("payments.filter.dateReversed");
  }

  return (
    <Card title={t("payments.title")}>
      <Typography.Paragraph type="secondary">{t("payments.intro")}</Typography.Paragraph>
      {customers.isError ? (
        <PaymentErrorAlert
          error={customers.error}
          failedText={t("payments.filter.customersFailed")}
          onRetry={() => void customers.refetch()}
        />
      ) : null}
      <Form<FilterValues>
        name="payment-filter"
        layout="inline"
        style={{ marginBottom: 16, rowGap: 8 }}
        initialValues={{ status: ALL, customer_id: undefined, created_from: "", created_to: "" }}
        onValuesChange={changeFilter}
      >
        <Form.Item name="status" label={t("payments.field.status")}>
          <Select
            style={{ minWidth: 180 }}
            options={[
              { value: ALL, label: t("payments.filter.anyStatus") },
              ...PAYMENT_STATUSES.map((value) => ({ value, label: statusLabel(value) })),
            ]}
          />
        </Form.Item>
        <Form.Item name="customer_id" label={t("payments.field.customer")}>
          <Select
            allowClear
            showSearch
            optionFilterProp="label"
            style={{ minWidth: 240 }}
            placeholder={t("payments.filter.anyCustomer")}
            loading={customers.isPending}
            options={customerOptions(customers.data)}
          />
        </Form.Item>
        <Form.Item
          name="created_from"
          label={t("payments.filter.createdFrom")}
          extra={fromValid ? t("payments.filter.dateHint") : undefined}
          validateStatus={fromValid ? "" : "error"}
          help={fromValid ? undefined : t("payments.filter.dateInvalid")}
        >
          <Input
            allowClear
            autoComplete="off"
            spellCheck={false}
            style={{ width: 150 }}
            placeholder={t("payments.filter.datePlaceholder")}
          />
        </Form.Item>
        <Form.Item
          name="created_to"
          label={t("payments.filter.createdTo")}
          extra={toHelp === undefined ? t("payments.filter.dateHint") : undefined}
          validateStatus={toHelp === undefined ? "" : "error"}
          help={toHelp}
        >
          <Input
            allowClear
            autoComplete="off"
            spellCheck={false}
            style={{ width: 150 }}
            placeholder={t("payments.filter.datePlaceholder")}
          />
        </Form.Item>
      </Form>
      {/* 列表与它的状态单独成一块，用例与验收脚本按 aria-label 找它。 */}
      <div role="region" aria-label={t("payments.list.title")}>
        {result}
      </div>
    </Card>
  );
}

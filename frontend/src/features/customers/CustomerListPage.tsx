/**
 * 管理端客户列表（`GET /api/v1/admin/customers`，spec §108 分页）。
 *
 * 页码与每页条数放在地址栏里（`?page=2&page_size=50`）：刷新、后退、把链接发给
 * 同事都还在同一页。顺序由后端决定（最新在前），前端不重排。
 *
 * 三种状态都画出来（spec §132 DoD 第 8 条）：加载中、失败（带 request_id）、空列表。
 */

import { keepPreviousData, useQuery } from "@tanstack/react-query";
import {
  Button,
  Card,
  Empty,
  Skeleton,
  Table,
  Tag,
  Typography,
  type TableColumnsType,
} from "antd";
import { useTranslation } from "react-i18next";
import { Link, useSearchParams } from "react-router";

import {
  DEFAULT_PAGE_SIZE,
  MAX_PAGE,
  MAX_PAGE_SIZE,
  customerListQueryKey,
  listCustomers,
  type CustomerSummary,
} from "../../api/adminCustomers";
import { DateTimeText } from "../../components/DateTimeText";
import { ROUTES, customerDetailPath } from "../../routes/paths";
import { CustomerErrorAlert } from "./CustomerForm";

const PAGE_SIZE_OPTIONS = [10, 20, 50, MAX_PAGE_SIZE];

/**
 * 地址栏里的正整数。不合法或超出后端范围时退回默认值 —— 否则后端回 422，
 * 用户看到的是一张因为自己手改了 URL 而打不开的列表。
 */
function readPositiveInt(raw: string | null, fallback: number, max: number): number {
  if (raw === null || !/^\d{1,6}$/.test(raw)) {
    return fallback;
  }
  const value = Number.parseInt(raw, 10);
  return value >= 1 && value <= max ? value : fallback;
}

/** 计费状态由余额驱动，前端只显示。认不出的值原样显示，不猜。 */
export function BillingStatusTag({ status }: { status: string }) {
  const { t } = useTranslation();
  if (status === "ACTIVE") {
    return <Tag color="green">{t("customers.billingStatus.active")}</Tag>;
  }
  if (status === "SUSPENDED") {
    return <Tag color="red">{t("customers.billingStatus.suspended")}</Tag>;
  }
  return <Tag>{status}</Tag>;
}

export function CustomerListPage() {
  const { t } = useTranslation();
  const [searchParams, setSearchParams] = useSearchParams();
  const page = readPositiveInt(searchParams.get("page"), 1, MAX_PAGE);
  const pageSize = readPositiveInt(searchParams.get("page_size"), DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE);

  const customers = useQuery({
    queryKey: customerListQueryKey(page, pageSize),
    queryFn: ({ signal }) => listCustomers(page, pageSize, signal),
    // 翻页时先留着上一页，不让整张表闪成骨架屏。
    placeholderData: keepPreviousData,
  });

  const goTo = (nextPage: number, nextPageSize: number): void => {
    const next = new URLSearchParams();
    // 换了每页条数，原来的页码就没有意义了，回到第一页。
    next.set("page", String(nextPageSize === pageSize ? nextPage : 1));
    if (nextPageSize !== DEFAULT_PAGE_SIZE) {
      next.set("page_size", String(nextPageSize));
    }
    setSearchParams(next);
  };

  const columns: TableColumnsType<CustomerSummary> = [
    {
      key: "company_name",
      title: t("customers.field.companyName"),
      render: (_: unknown, customer) => (
        <Link to={customerDetailPath(customer.id)}>{customer.company_name}</Link>
      ),
    },
    { key: "email", title: t("customers.field.email"), dataIndex: "email" },
    {
      key: "billing_status",
      title: t("customers.field.billingStatus"),
      render: (_: unknown, customer) => <BillingStatusTag status={customer.billing_status} />,
    },
    {
      key: "created_at",
      title: t("customers.field.createdAt"),
      render: (_: unknown, customer) => <DateTimeText value={customer.created_at} />,
    },
  ];

  const createButton = (
    <Link to={ROUTES.customerCreate}>
      <Button type="primary">{t("customers.list.create")}</Button>
    </Link>
  );

  if (customers.isPending) {
    return (
      <Card title={t("customers.list.title")} extra={createButton}>
        <Skeleton active title={false} paragraph={{ rows: 4 }} />
        <Typography.Text type="secondary">{t("customers.list.loading")}</Typography.Text>
      </Card>
    );
  }

  if (customers.isError) {
    return (
      <Card title={t("customers.list.title")} extra={createButton}>
        <CustomerErrorAlert
          error={customers.error}
          title={t("customers.list.loadFailed")}
          onRetry={() => void customers.refetch()}
        />
      </Card>
    );
  }

  const { items, total } = customers.data;

  if (total === 0) {
    return (
      <Card title={t("customers.list.title")} extra={createButton}>
        <Empty description={t("customers.list.empty")} />
      </Card>
    );
  }

  return (
    <Card title={t("customers.list.title")} extra={createButton}>
      <Table<CustomerSummary>
        rowKey="id"
        columns={columns}
        dataSource={items}
        loading={customers.isFetching}
        // 页码超过末页时后端回空 items、total 照报：说清楚是「这一页没有」，不是「一个客户都没有」。
        locale={{ emptyText: t("customers.list.emptyPage") }}
        pagination={{
          current: page,
          pageSize,
          total,
          showSizeChanger: true,
          pageSizeOptions: PAGE_SIZE_OPTIONS,
          showTotal: (count) => t("customers.list.total", { total: count }),
          onChange: goTo,
        }}
      />
    </Card>
  );
}

/**
 * 管理端客户列表（spec §56、§108；docs/api.md「客户列表」）。
 *
 * 三种状态都要画出来（spec §132 DoD 第 8 条）：加载中、失败、空列表。失败那一支
 * 带后端的 message 与 request_id。
 *
 * 页码与每页条数放在 URL 查询串里（`?page=2&page_size=50`）：从详情页返回时还在
 * 原来那一页，链接也能直接发给别人。顺序沿用后端（最新在前），前端不再排序。
 *
 * ⚠️ 403 `ADMIN_REQUIRED` 照实显示「无权限」，前端不做角色判断 —— 菜单不是权限
 * （spec §51，见 AppLayout 注释）。
 */

import { keepPreviousData, useQuery } from "@tanstack/react-query";
import {
  Alert,
  Button,
  Card,
  Empty,
  Skeleton,
  Table,
  Tag,
  Typography,
  type TableColumnsType,
} from "antd";
import type { ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { Link, useNavigate, useSearchParams } from "react-router";

import {
  ADMIN_REQUIRED,
  DEFAULT_PAGE_SIZE,
  MAX_PAGE,
  MAX_PAGE_SIZE,
  customerListQueryKey,
  listCustomers,
  shouldRetryCustomerQuery,
  type BillingStatus,
  type CustomerSummary,
} from "../../api/adminCustomers";
import { ApiError } from "../../api/client";
import { DateTimeText } from "../../components/DateTimeText";
import { RequestReference } from "../../components/RequestReference";
import { ROUTES, customerDetailPath } from "../../routes/paths";

const PAGE_PARAM = "page";
const PAGE_SIZE_PARAM = "page_size";
const PAGE_SIZE_OPTIONS = [10, 20, 50, MAX_PAGE_SIZE];

/**
 * 查询串里的正整数；缺了、不是数、越界都回落到默认值。
 *
 * 越界的值原样发给后端会得到 422（spec §108 不静默截断）—— 那是后端对的地方，
 * 但一个手改坏了的地址不该让整页变成错误页。
 */
function readBounded(raw: string | null, fallback: number, max: number): number {
  if (raw === null || !/^\d+$/.test(raw)) {
    return fallback;
  }
  const value = Number.parseInt(raw, 10);
  return value >= 1 && value <= max ? value : fallback;
}

export function CustomerListPage() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  const page = readBounded(searchParams.get(PAGE_PARAM), 1, MAX_PAGE);
  const pageSize = readBounded(searchParams.get(PAGE_SIZE_PARAM), DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE);

  const customers = useQuery({
    queryKey: customerListQueryKey(page, pageSize),
    queryFn: ({ signal }) => listCustomers(page, pageSize, signal),
    // 翻页时先留着上一页，不闪回骨架屏。
    placeholderData: keepPreviousData,
    retry: shouldRetryCustomerQuery,
  });

  const changePage = (nextPage: number, nextPageSize: number): void => {
    // 换了每页条数就回第一页：原来的页码在新的条数下指的是另一批客户。
    const target = nextPageSize === pageSize ? nextPage : 1;
    setSearchParams({ [PAGE_PARAM]: String(target), [PAGE_SIZE_PARAM]: String(nextPageSize) });
  };

  const columns: TableColumnsType<CustomerSummary> = [
    {
      key: "company_name",
      title: t("customers.field.companyName"),
      render: (_value, customer) => (
        // 行本身也能点；这里拦住冒泡，免得一次点击导航两次。
        <Link to={customerDetailPath(customer.id)} onClick={(event) => event.stopPropagation()}>
          {customer.company_name}
        </Link>
      ),
    },
    { key: "email", title: t("customers.field.email"), dataIndex: "email" },
    {
      key: "billing_status",
      title: t("customers.field.billingStatus"),
      render: (_value, customer) => <BillingStatusTag status={customer.billing_status} />,
    },
    {
      key: "created_at",
      title: t("customers.field.createdAt"),
      render: (_value, customer) => <DateTimeText value={customer.created_at} />,
    },
  ];

  const newCustomer = (
    <Link to={ROUTES.newCustomer}>
      <Button type="primary">{t("customers.list.create")}</Button>
    </Link>
  );

  let body: ReactNode;
  if (customers.isPending) {
    body = (
      <>
        <Skeleton active title={false} paragraph={{ rows: 3 }} />
        <Typography.Text type="secondary">{t("customers.list.loading")}</Typography.Text>
      </>
    );
  } else if (customers.isError) {
    body = (
      <CustomerErrorAlert
        error={customers.error}
        title={t("customers.list.loadFailed")}
        onRetry={() => void customers.refetch()}
      />
    );
  } else if (customers.data.total === 0) {
    body = <Empty description={t("customers.list.empty")} />;
  } else {
    body = (
      <Table<CustomerSummary>
        rowKey="id"
        columns={columns}
        dataSource={customers.data.items}
        loading={customers.isPlaceholderData}
        onRow={(customer) => ({
          onClick: () => void navigate(customerDetailPath(customer.id)),
          style: { cursor: "pointer" },
        })}
        pagination={{
          current: page,
          pageSize,
          total: customers.data.total,
          showSizeChanger: true,
          pageSizeOptions: PAGE_SIZE_OPTIONS,
          onChange: changePage,
        }}
      />
    );
  }

  return (
    <Card title={t("customers.list.title")} extra={newCustomer}>
      {body}
    </Card>
  );
}

/**
 * 计费状态的标签。状态由余额驱动，前端只展示。
 *
 * 不认识的值原样显示：契约只有两个取值，但后端先加一个新值时，页面不该因此变空。
 */
export function BillingStatusTag({ status }: { status: BillingStatus }) {
  const { t } = useTranslation();
  if (status === "ACTIVE") {
    return <Tag color="green">{t("customers.billingStatus.active")}</Tag>;
  }
  if (status === "SUSPENDED") {
    return <Tag color="red">{t("customers.billingStatus.suspended")}</Tag>;
  }
  return <Tag>{String(status)}</Tag>;
}

/**
 * 客户页面的错误提示：后端的 message 与 request_id 一起显示（`RequestReference`）。
 *
 * 403 `ADMIN_REQUIRED` 换成「无权限」的说法，不给「再试一次」—— 再试也还是 403。
 */
export function CustomerErrorAlert({
  error,
  title,
  onRetry,
}: {
  error: Error;
  title: string;
  onRetry?: () => void;
}) {
  const { t } = useTranslation();
  const forbidden = error instanceof ApiError && error.code === ADMIN_REQUIRED;
  return (
    <Alert
      type={forbidden ? "warning" : "error"}
      showIcon
      style={{ marginBottom: 16 }}
      message={forbidden ? t("customers.forbidden") : title}
      description={<RequestReference error={error} />}
      action={
        onRetry !== undefined && !forbidden ? (
          <Button size="small" onClick={onRetry}>
            {t("customers.retry")}
          </Button>
        ) : null
      }
    />
  );
}

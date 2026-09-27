/**
 * 管理端客户列表（docs/api.md「客户列表」，spec §108 分页）。
 *
 * 顺序沿用后端（最新在前），前端不重排。页码与每页条数放在地址栏里：从详情页
 * 「后退」回来时还在原来那一页，链接也能直接发给别人。
 *
 * 三种状态都画出来（spec §132 DoD 第 8 条）：加载中、失败（后端的 message 与
 * request_id）、空列表。
 */

import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { Button, Card, Empty, Skeleton, Table, Typography } from "antd";
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
import { PAGE_PARAM, PAGE_SIZE_PARAM, ROUTES, customerDetailPath } from "../../routes/paths";
import { BillingStatusTag, CustomerErrorAlert, retryUnlessDefinite } from "./CustomerForm";

/** 每页条数的可选值，都不超过后端上限 100。 */
const PAGE_SIZE_OPTIONS = ["10", "20", "50", "100"];

/**
 * 地址栏里的数字：不是纯数字、或越出后端接受的范围，就用默认值。
 *
 * 越界时不照发：后端对越界回 422（不静默截断），而一个手改坏了的地址不该变成
 * 一页红色错误。
 */
function boundedInt(raw: string | null, max: number, fallback: number): number {
  if (raw === null || !/^\d{1,6}$/.test(raw)) {
    return fallback;
  }
  const value = Number.parseInt(raw, 10);
  return value >= 1 && value <= max ? value : fallback;
}

export function CustomerListPage() {
  const { t } = useTranslation();
  const [searchParams, setSearchParams] = useSearchParams();
  const page = boundedInt(searchParams.get(PAGE_PARAM), MAX_PAGE, 1);
  const pageSize = boundedInt(searchParams.get(PAGE_SIZE_PARAM), MAX_PAGE_SIZE, DEFAULT_PAGE_SIZE);

  const customers = useQuery({
    queryKey: customerListQueryKey(page, pageSize),
    queryFn: ({ signal }) => listCustomers(page, pageSize, signal),
    // 翻页时先留着上一页，表格转圈，而不是整块闪成骨架屏。
    placeholderData: keepPreviousData,
    retry: retryUnlessDefinite,
  });

  const goTo = (nextPage: number, nextPageSize: number): void => {
    setSearchParams({
      // 换了每页条数，原来的页码就没有意义了，回到第一页。
      [PAGE_PARAM]: String(nextPageSize === pageSize ? nextPage : 1),
      [PAGE_SIZE_PARAM]: String(nextPageSize),
    });
  };

  const createButton = (
    <Link to={ROUTES.customerNew}>
      <Button type="primary">{t("customers.create.title")}</Button>
    </Link>
  );

  return (
    <Card title={t("customers.list.title")} extra={createButton}>
      {customers.isPending ? (
        <>
          <Skeleton active title={false} paragraph={{ rows: 4 }} />
          <Typography.Text type="secondary">{t("customers.list.loading")}</Typography.Text>
        </>
      ) : null}

      {customers.isError ? (
        <CustomerErrorAlert
          error={customers.error}
          title={t("customers.list.failed")}
          action={
            <Button size="small" onClick={() => void customers.refetch()}>
              {t("customers.retry")}
            </Button>
          }
        />
      ) : null}

      {customers.isSuccess && customers.data.total === 0 ? (
        <Empty description={t("customers.list.empty")} />
      ) : null}

      {customers.isSuccess && customers.data.total > 0 ? (
        <Table<CustomerSummary>
          rowKey="id"
          dataSource={customers.data.items}
          loading={customers.isFetching}
          columns={[
            {
              key: "company_name",
              title: t("customers.field.companyName"),
              dataIndex: "company_name",
              render: (_value: string, customer) => (
                <Link to={customerDetailPath(customer.id)}>{customer.company_name}</Link>
              ),
            },
            {
              key: "email",
              title: t("customers.field.email"),
              dataIndex: "email",
            },
            {
              key: "billing_status",
              title: t("customers.field.billingStatus"),
              dataIndex: "billing_status",
              render: (_value: string, customer) => (
                <BillingStatusTag status={customer.billing_status} />
              ),
            },
            {
              key: "created_at",
              title: t("customers.field.createdAt"),
              dataIndex: "created_at",
              render: (_value: string, customer) => <DateTimeText value={customer.created_at} />,
            },
          ]}
          pagination={{
            current: page,
            pageSize,
            total: customers.data.total,
            showSizeChanger: true,
            pageSizeOptions: PAGE_SIZE_OPTIONS,
            onChange: goTo,
          }}
        />
      ) : null}
    </Card>
  );
}

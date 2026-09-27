/**
 * 管理端客户详情与编辑（docs/api.md「客户详情」「编辑客户」）。
 *
 * 显示客户详情的全部字段与钱包（币种、余额、版本）。编辑就在这一页上：
 * PATCH **只带实际改了的字段**，一个都没改就不发请求（后端对 `{}` 回 422）；
 * 成功后用响应本身刷新详情，不再多读一次。
 *
 * ⚠️ 页面上没有、也不发送账户状态、低余额阈值、成本与毛利：后端没有这些字段，
 * 这里也不预留位置。钱包只读 —— 调账不走这一页。
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Alert, Button, Card, Descriptions, Result, Skeleton, Space, Typography } from "antd";
import { useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { Link, useParams } from "react-router";

import {
  CUSTOMER_LIST_QUERY_KEY,
  CUSTOMER_NOT_FOUND,
  customerQueryKey,
  getCustomer,
  updateCustomer,
  type CustomerDetail,
  type UpdateCustomerPatch,
} from "../../api/adminCustomers";
import { ApiError } from "../../api/client";
import { DateTimeText } from "../../components/DateTimeText";
import { MoneyText } from "../../components/MoneyText";
import { ROUTES } from "../../routes/paths";
import {
  BillingStatusTag,
  CustomerErrorAlert,
  CustomerForm,
  customerPatch,
  formValuesOf,
  retryUnlessDefinite,
  type CustomerFormValues,
} from "./CustomerForm";

export function CustomerDetailPage() {
  const { t } = useTranslation();
  const { customerId = "" } = useParams();

  const customer = useQuery({
    queryKey: customerQueryKey(customerId),
    queryFn: ({ signal }) => getCustomer(customerId, signal),
    retry: retryUnlessDefinite,
  });

  const backToList = <Link to={ROUTES.customers}>{t("customers.detail.backToList")}</Link>;

  if (customer.isPending) {
    return (
      <Card extra={backToList}>
        <Skeleton active title={false} paragraph={{ rows: 6 }} />
        <Typography.Text type="secondary">{t("customers.detail.loading")}</Typography.Text>
      </Card>
    );
  }

  if (customer.isError) {
    // 「客户不存在」是一个明确的答复，不是故障：不给「重试」，也不摆出错误样式。
    if (customer.error instanceof ApiError && customer.error.code === CUSTOMER_NOT_FOUND) {
      return (
        <Result
          status="404"
          title={t("customers.detail.notFound")}
          subTitle={t("customers.detail.notFoundBody")}
          extra={
            <Link to={ROUTES.customers}>
              <Button type="primary">{t("customers.detail.backToList")}</Button>
            </Link>
          }
        />
      );
    }
    return (
      <Card extra={backToList}>
        <CustomerErrorAlert
          error={customer.error}
          title={t("customers.detail.failed")}
          action={
            <Button size="small" onClick={() => void customer.refetch()}>
              {t("customers.retry")}
            </Button>
          }
        />
      </Card>
    );
  }

  // `key`：从一个客户直接跳到另一个客户时，编辑中的半成品不能带过去。
  return <CustomerView key={customer.data.id} customer={customer.data} backToList={backToList} />;
}

function CustomerView({
  customer,
  backToList,
}: {
  customer: CustomerDetail;
  backToList: ReactNode;
}) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [editing, setEditing] = useState(false);
  const [unchanged, setUnchanged] = useState(false);

  const update = useMutation({
    mutationFn: (patch: UpdateCustomerPatch) => updateCustomer(customer.id, patch),
    onSuccess: (updated) => {
      // 响应就是改后的详情，直接放进缓存，不再多读一次。
      queryClient.setQueryData(customerQueryKey(customer.id), updated);
      void queryClient.invalidateQueries({ queryKey: CUSTOMER_LIST_QUERY_KEY });
      setEditing(false);
    },
  });

  const startEditing = (): void => {
    update.reset();
    setUnchanged(false);
    setEditing(true);
  };

  const save = (values: CustomerFormValues): void => {
    const patch = customerPatch(customer, values);
    if (Object.keys(patch).length === 0) {
      setUnchanged(true);
      return;
    }
    setUnchanged(false);
    update.mutate(patch);
  };

  const notSet = (value: string | null) =>
    value ?? <Typography.Text type="secondary">{t("customers.detail.notSet")}</Typography.Text>;

  return (
    <Space direction="vertical" size="large" style={{ width: "100%" }}>
      <Card
        title={customer.company_name}
        extra={
          <Space>
            {editing ? null : <Button onClick={startEditing}>{t("customers.edit.button")}</Button>}
            {backToList}
          </Space>
        }
      >
        {editing ? (
          <>
            <CustomerErrorAlert error={update.error} title={t("customers.edit.failed")} />
            {unchanged ? (
              <Alert
                type="info"
                showIcon
                style={{ marginBottom: 16 }}
                message={t("customers.edit.noChanges")}
              />
            ) : null}
            <CustomerForm
              initialValues={formValuesOf(customer)}
              submitLabel={t("customers.edit.submit")}
              busy={update.isPending}
              onSubmit={save}
              onCancel={() => setEditing(false)}
            />
          </>
        ) : (
          <Descriptions
            column={1}
            bordered
            size="small"
            items={[
              { key: "id", label: t("customers.field.id"), children: customer.id },
              {
                key: "company_name",
                label: t("customers.field.companyName"),
                children: customer.company_name,
              },
              {
                key: "contact_name",
                label: t("customers.field.contactName"),
                children: notSet(customer.contact_name),
              },
              { key: "email", label: t("customers.field.email"), children: customer.email },
              { key: "phone", label: t("customers.field.phone"), children: notSet(customer.phone) },
              {
                key: "billing_status",
                label: t("customers.field.billingStatus"),
                children: <BillingStatusTag status={customer.billing_status} />,
              },
              {
                key: "status_version",
                label: t("customers.field.statusVersion"),
                children: customer.status_version,
              },
              {
                key: "created_at",
                label: t("customers.field.createdAt"),
                children: <DateTimeText value={customer.created_at} />,
              },
              {
                key: "updated_at",
                label: t("customers.field.updatedAt"),
                children: <DateTimeText value={customer.updated_at} />,
              },
            ]}
          />
        )}
      </Card>

      <Card title={t("customers.wallet.title")}>
        <Descriptions
          column={1}
          bordered
          size="small"
          items={[
            {
              key: "currency",
              label: t("customers.wallet.currency"),
              children: customer.wallet.currency,
            },
            {
              key: "balance",
              label: t("customers.wallet.balance"),
              children: (
                <MoneyText amount={customer.wallet.balance} currency={customer.wallet.currency} />
              ),
            },
            {
              key: "version",
              label: t("customers.wallet.version"),
              children: customer.wallet.version,
            },
          ]}
        />
      </Card>
    </Space>
  );
}

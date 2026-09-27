/**
 * 管理端客户详情与编辑（docs/api.md「客户详情」「编辑客户」）。
 *
 * 显示客户详情的全部字段与钱包（币种、余额、版本）。**只显示契约里有的字段**：
 * 没有账户状态、低余额阈值、成本或毛利 —— 后端不给，这里也不留位置。
 *
 * 编辑只发实际改了的字段，一个都没改就不发请求；成功后用响应本身刷新详情，
 * 不再多读一次。计费状态与钱包由余额驱动，这里只读。
 */

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Alert, Button, Card, Descriptions, Result, Skeleton, Space, Typography } from "antd";
import { useRef, useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { Link, useParams } from "react-router";

import {
  CUSTOMER_LIST_QUERY_KEY,
  CUSTOMER_NOT_FOUND,
  customerDetailQueryKey,
  getCustomer,
  shouldRetryCustomerQuery,
  updateCustomer,
  type CustomerDetail,
} from "../../api/adminCustomers";
import { ApiError } from "../../api/client";
import { DateTimeText } from "../../components/DateTimeText";
import { MoneyText } from "../../components/MoneyText";
import { CUSTOMER_ID_PARAM, ROUTES } from "../../routes/paths";
import { CustomerForm, toUpdatePatch, type CustomerFormValues } from "./CustomerForm";
import { BillingStatusTag, CustomerErrorAlert } from "./CustomerListPage";

/** 可空字段为空时显示的占位符。 */
const EMPTY_VALUE = "—";

export function CustomerDetailPage() {
  const { t } = useTranslation();
  const customerId = useParams()[CUSTOMER_ID_PARAM] ?? "";

  const detail = useQuery({
    queryKey: customerDetailQueryKey(customerId),
    queryFn: ({ signal }) => getCustomer(customerId, signal),
    enabled: customerId !== "",
    retry: shouldRetryCustomerQuery,
  });

  const backToList = <Link to={ROUTES.customers}>{t("customers.detail.backToList")}</Link>;

  if (customerId === "" || isNotFound(detail.error)) {
    // ⚠️ 404 单独成一页，不走通用错误：「客户不存在」是确定的答案，不是故障，
    // 也不给「再试一次」。
    return (
      <Result
        status="404"
        title={t("customers.detail.notFoundTitle")}
        subTitle={t("customers.detail.notFoundBody")}
        extra={backToList}
      />
    );
  }

  if (detail.isPending) {
    return (
      <Card extra={backToList}>
        <Skeleton active title={false} paragraph={{ rows: 6 }} />
        <Typography.Text type="secondary">{t("customers.detail.loading")}</Typography.Text>
      </Card>
    );
  }

  if (detail.isError) {
    return (
      <Card extra={backToList}>
        <CustomerErrorAlert
          error={detail.error}
          title={t("customers.detail.loadFailed")}
          onRetry={() => void detail.refetch()}
        />
      </Card>
    );
  }

  // `key`：从一个客户的详情直接跳到另一个时，编辑状态不许带过去。
  return <CustomerDetailView key={detail.data.id} customer={detail.data} backToList={backToList} />;
}

function isNotFound(error: Error | null): boolean {
  return error instanceof ApiError && error.code === CUSTOMER_NOT_FOUND;
}

function CustomerDetailView({
  customer,
  backToList,
}: {
  customer: CustomerDetail;
  backToList: ReactNode;
}) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const inFlight = useRef(false);
  const [editing, setEditing] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);
  const [notice, setNotice] = useState<"saved" | "unchanged" | null>(null);

  const startEditing = (): void => {
    setNotice(null);
    setError(null);
    setEditing(true);
  };

  const stopEditing = (): void => {
    setNotice(null);
    setError(null);
    setEditing(false);
  };

  const save = (values: CustomerFormValues): void => {
    if (inFlight.current) {
      return;
    }
    const patch = toUpdatePatch(customer, values);
    if (Object.keys(patch).length === 0) {
      // 没有改动就不发：后端对 `{}` 回 422，对全同的值回 200 但什么也不写 ——
      // 两种都只是多一次往返。
      setError(null);
      setNotice("unchanged");
      return;
    }
    inFlight.current = true;
    setBusy(true);
    setError(null);
    setNotice(null);
    void (async () => {
      try {
        const updated = await updateCustomer(customer.id, patch);
        // 用响应本身刷新详情，不再读一次；列表里的公司名、邮箱可能变了，作废重读。
        queryClient.setQueryData(customerDetailQueryKey(customer.id), updated);
        void queryClient.invalidateQueries({ queryKey: CUSTOMER_LIST_QUERY_KEY });
        setEditing(false);
        setNotice("saved");
      } catch (caught) {
        if (!(caught instanceof ApiError)) {
          throw caught;
        }
        setError(caught);
      } finally {
        inFlight.current = false;
        setBusy(false);
      }
    })();
  };

  const optionalText = (value: string | null) => value ?? EMPTY_VALUE;

  return (
    <Space direction="vertical" size="large" style={{ width: "100%" }}>
      {backToList}
      <Card
        title={customer.company_name}
        extra={
          editing ? null : <Button onClick={startEditing}>{t("customers.detail.edit")}</Button>
        }
      >
        {notice === "saved" ? (
          <Alert
            type="success"
            showIcon
            style={{ marginBottom: 16 }}
            message={t("customers.edit.saved")}
          />
        ) : null}
        {notice === "unchanged" ? (
          <Alert
            type="info"
            showIcon
            style={{ marginBottom: 16 }}
            message={t("customers.edit.noChanges")}
          />
        ) : null}
        {error === null ? null : (
          <CustomerErrorAlert error={error} title={t("customers.edit.failed")} />
        )}

        {editing ? (
          <CustomerForm
            initialValues={{
              company_name: customer.company_name,
              email: customer.email,
              contact_name: customer.contact_name ?? "",
              phone: customer.phone ?? "",
            }}
            busy={busy}
            submitLabel={t("customers.edit.submit")}
            onSubmit={save}
            onCancel={stopEditing}
          />
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
              { key: "email", label: t("customers.field.email"), children: customer.email },
              {
                key: "contact_name",
                label: t("customers.field.contactName"),
                children: optionalText(customer.contact_name),
              },
              {
                key: "phone",
                label: t("customers.field.phone"),
                children: optionalText(customer.phone),
              },
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

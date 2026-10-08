/**
 * 管理端客户详情与编辑（`GET` / `PATCH /api/v1/admin/customers/{customer_id}`）。
 *
 * 显示客户详情的全部字段与钱包（币种、余额、版本）。余额只经 `MoneyText` 展示，
 * 不在这里解析（INV-10）。编辑只发实际改了的字段，没有改动就不发请求；成功后直接用
 * PATCH 的响应刷新详情，不再读一次。
 *
 * 页底是这个客户的项目区块（`ProjectsPanel`，AIH-TASK-016），在详情读到之后才挂上。
 *
 * 钱包卡片上的「Adjust balance」打开手工调账表单（`features/wallet/AdjustmentModal`，
 * AIH-TASK-017）。调账成功后由表单让这里的详情过期重读，余额与计费状态以后端为准。
 *
 * 没有账户状态、低余额阈值、成本或毛利 —— 后端没有这些字段，这里也不预留。
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Alert,
  Button,
  Card,
  Descriptions,
  Result,
  Skeleton,
  Space,
  Typography,
  type DescriptionsProps,
} from "antd";
import { useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link, useParams } from "react-router";

import {
  CUSTOMER_LISTS_QUERY_KEY,
  CUSTOMER_NOT_FOUND,
  customerDetailQueryKey,
  getCustomer,
  updateCustomer,
  type CustomerDetail,
  type CustomerPatch,
  type Wallet,
} from "../../api/adminCustomers";
import { ApiError } from "../../api/client";
import { DateTimeText } from "../../components/DateTimeText";
import { MoneyText } from "../../components/MoneyText";
import { ROUTES } from "../../routes/paths";
import { AdjustmentModal } from "../wallet/AdjustmentModal";
import {
  CustomerErrorAlert,
  CustomerForm,
  formValuesFrom,
  toPatch,
  type CustomerFormValues,
} from "./CustomerForm";
import { BillingStatusTag } from "./CustomerListPage";
import { InternalUsagePanel } from "./InternalUsagePanel";
import { ProjectsPanel } from "./ProjectsPanel";

type Notice = "saved" | "unchanged" | null;

/** 空的可选字段显示成一道横线，不留一格空白让人以为没加载完。 */
const NO_VALUE = "—";

export function CustomerDetailPage() {
  const { t } = useTranslation();
  const { customerId = "" } = useParams();
  const queryClient = useQueryClient();
  const [editing, setEditing] = useState(false);
  const [notice, setNotice] = useState<Notice>(null);
  const [adjusting, setAdjusting] = useState(false);
  // 与建客户同一个理由：双击时两次 onFinish 可能都赶在按钮禁用之前。
  const inFlight = useRef(false);

  const customer = useQuery({
    queryKey: customerDetailQueryKey(customerId),
    queryFn: ({ signal }) => getCustomer(customerId, signal),
  });

  const update = useMutation({
    mutationFn: (patch: CustomerPatch) => updateCustomer(customerId, patch),
    onSuccess: (updated) => {
      queryClient.setQueryData(customerDetailQueryKey(customerId), updated);
      // 公司名、邮箱都显示在列表里。
      void queryClient.invalidateQueries({ queryKey: CUSTOMER_LISTS_QUERY_KEY });
      setEditing(false);
      setNotice("saved");
    },
    onSettled: () => {
      inFlight.current = false;
    },
  });

  const backLink = <Link to={ROUTES.customers}>{t("customers.detail.backToList")}</Link>;

  if (customer.isPending) {
    return (
      <Card>
        <Skeleton active />
        <Typography.Text type="secondary">{t("customers.detail.loading")}</Typography.Text>
      </Card>
    );
  }

  if (customer.isError) {
    const { error } = customer;
    // 404 是一个明确的答案（没有这个客户），不是「出错了、请重试」。
    if (error instanceof ApiError && error.code === CUSTOMER_NOT_FOUND) {
      return (
        <Result
          status="404"
          title={t("customers.detail.notFound")}
          subTitle={t("customers.detail.notFoundBody")}
          extra={backLink}
        />
      );
    }
    return (
      <Card>
        <CustomerErrorAlert
          error={error}
          title={t("customers.detail.loadFailed")}
          onRetry={() => void customer.refetch()}
        />
        {backLink}
      </Card>
    );
  }

  const current = customer.data;

  const startEditing = (): void => {
    update.reset();
    setNotice(null);
    setEditing(true);
  };

  const save = (values: CustomerFormValues): void => {
    if (inFlight.current) {
      return;
    }
    const patch = toPatch(current, values);
    if (Object.keys(patch).length === 0) {
      // 后端对空 PATCH 回 422；而且没什么可存的，本来就不该发。
      setEditing(false);
      setNotice("unchanged");
      return;
    }
    inFlight.current = true;
    update.mutate(patch);
  };

  return (
    <Space direction="vertical" size="middle" style={{ width: "100%" }}>
      {backLink}

      {notice === "saved" ? (
        <Alert type="success" showIcon message={t("customers.detail.saved")} />
      ) : null}
      {notice === "unchanged" ? (
        <Alert type="info" showIcon message={t("customers.detail.unchanged")} />
      ) : null}

      {editing ? (
        <Card title={t("customers.detail.editTitle")} style={{ maxWidth: 640 }}>
          <CustomerForm
            initialValues={formValuesFrom(current)}
            submitLabel={t("customers.form.save")}
            submitting={update.isPending}
            error={update.error}
            onSubmit={save}
            onCancel={() => setEditing(false)}
          />
        </Card>
      ) : (
        <Card
          title={current.company_name}
          extra={<Button onClick={startEditing}>{t("customers.detail.edit")}</Button>}
        >
          <ProfileDescriptions customer={current} />
        </Card>
      )}

      <Card
        title={t("customers.wallet.title")}
        extra={<Button onClick={() => setAdjusting(true)}>{t("wallet.adjustment.open")}</Button>}
      >
        <WalletDescriptions wallet={current.wallet} />
      </Card>

      {current.billing_mode === "INTERNAL_METERED_ONLY" ? (
        <InternalUsagePanel customerId={current.id} />
      ) : null}

      {/* ⚠️ 只在打开时挂载、关闭即卸载：调账的幂等键在挂载时生成，这样每次打开才是新键。
          详见 AdjustmentModal 的文件头注释。 */}
      {adjusting ? (
        <AdjustmentModal customer={current} onClose={() => setAdjusting(false)} />
      ) : null}

      {/* key：换了客户，页码与建项目表单都从头来。 */}
      <ProjectsPanel key={current.id} customerId={current.id} />
    </Space>
  );
}

function ProfileDescriptions({ customer }: { customer: CustomerDetail }) {
  const { t } = useTranslation();
  const items: DescriptionsProps["items"] = [
    {
      key: "id",
      label: t("customers.field.id"),
      children: (
        <Typography.Text code copyable>
          {customer.id}
        </Typography.Text>
      ),
    },
    {
      key: "company_name",
      label: t("customers.field.companyName"),
      children: customer.company_name,
    },
    {
      key: "contact_name",
      label: t("customers.field.contactName"),
      children: customer.contact_name ?? NO_VALUE,
    },
    { key: "email", label: t("customers.field.email"), children: customer.email },
    { key: "phone", label: t("customers.field.phone"), children: customer.phone ?? NO_VALUE },
    {
      key: "billing_status",
      label: t("customers.field.billingStatus"),
      children: <BillingStatusTag status={customer.billing_status} />,
    },
    {
      key: "billing_mode",
      label: t("customers.field.billingMode"),
      children: customer.billing_mode,
    },
    {
      key: "ai_service_enabled",
      label: t("customers.field.aiService"),
      children: customer.ai_service_enabled ? t("customers.aiService.enabled") : t("customers.aiService.disabled"),
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
  ];
  return <Descriptions bordered column={1} items={items} />;
}

function WalletDescriptions({ wallet }: { wallet: Wallet }) {
  const { t } = useTranslation();
  const items: DescriptionsProps["items"] = [
    { key: "currency", label: t("customers.wallet.currency"), children: wallet.currency },
    {
      key: "balance",
      label: t("customers.wallet.balance"),
      children: <MoneyText amount={wallet.balance} currency={wallet.currency} />,
    },
    { key: "version", label: t("customers.wallet.version"), children: wallet.version },
  ];
  return <Descriptions bordered column={1} items={items} />;
}

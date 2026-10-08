import { useQuery } from "@tanstack/react-query";
import { Alert, Card, Table, Typography } from "antd";
import { useTranslation } from "react-i18next";

import { recentInternalUsage, type InternalUsageEvent } from "../../api/adminUsageEvents";
import { MoneyText } from "../../components/MoneyText";

export function InternalUsagePanel({ customerId }: { customerId: string }) {
  const { t } = useTranslation();
  const usage = useQuery({
    queryKey: ["internal-usage", customerId],
    queryFn: ({ signal }) => recentInternalUsage(customerId, signal),
  });

  return (
    <Card title={t("customers.internalUsage.title")} loading={usage.isPending}>
      <Typography.Paragraph type="secondary">
        {t("customers.internalUsage.explanation")}
      </Typography.Paragraph>
      {usage.isError ? <Alert type="error" showIcon message={t("customers.internalUsage.error")} /> : null}
      {usage.data ? (
        <Table<InternalUsageEvent>
          rowKey="id"
          dataSource={usage.data.items}
          pagination={false}
          columns={[
            { title: t("customers.internalUsage.time"), dataIndex: "occurred_at" },
            { title: t("customers.internalUsage.model"), dataIndex: "model" },
            { title: t("customers.internalUsage.status"), dataIndex: "status" },
            {
              title: t("customers.internalUsage.providerCost"),
              dataIndex: "estimated_provider_cost_myr",
              render: (amount: string | null) => amount === null ? "—" : <MoneyText amount={amount} currency="MYR" />,
            },
            {
              title: t("customers.internalUsage.referencePrice"),
              dataIndex: "reference_customer_price",
              render: (amount: string | null) => amount === null ? "—" : <MoneyText amount={amount} currency="MYR" />,
            },
            {
              title: t("customers.internalUsage.walletCharge"),
              dataIndex: "billable_cost",
              render: (amount: string | null) => amount === null ? "—" : <MoneyText amount={amount} currency="MYR" />,
            },
          ]}
        />
      ) : null}
    </Card>
  );
}

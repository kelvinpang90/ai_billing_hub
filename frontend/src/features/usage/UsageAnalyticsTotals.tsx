/**
 * 用量分析页表格上方的合计块（设计闸门 #709 v2 §2「页面」，AIH-TASK-224）。
 *
 * 覆盖全部分组、不随分页变化的合计：用量收入、估算成本、用量毛利（按 `occurred_at`），网关手续费、充值到账额与已付
 * 笔数（按 `paid_at`），扣手续费后的毛利与毛利率，以及内部计量的三项。
 *
 * 写法刻意固定：`<section role="region" aria-label="Totals">` 里一个带边框的 antd `Descriptions`（标签在 `<th>`、
 * 值在 `<td>`）。**不用** `Table`：部署后验收按表头与 `data-row-key` 识别数据表格，合计块不能被算成数据行；
 * **不用** `Alert`：它带 `role="alert"`，验收把页面上任何 `role="alert"` 都当成错误提示。两条提示也是普通文字。
 *
 * 对账后成本（设计闸门 #756 v2 §2「页面」，AIH-TASK-249）：追加 8 项，每项一对 `<th>` / `<td>`，`<td>` 里只有那一个值
 * （后端字符串、口径的显示文案或「—」）；口径、比率与说明不进同一格。块下方是覆盖说明（至多两行）与基于供应商成本的
 * 毛利的口径说明，也是普通文字。
 *
 * ⚠️ 金额与比率**原样**显示后端的十进制字符串，不经 number，也不重新格式化；null 显示「—」。
 */

import { Descriptions, Typography, type DescriptionsProps } from "antd";
import { useTranslation } from "react-i18next";

import type {
  ProviderCostBasis,
  UsageAnalyticsReconciliationCoverage,
  UsageAnalyticsReportTotals,
} from "../../api/adminUsageAnalytics";
import { EMPTY } from "./UsageEventsPage";

/** 后端给的十进制字符串，原样显示；没有时「—」。 */
function DecimalString({ value }: { value: string | null }) {
  return value === null ? <>{EMPTY}</> : <span style={{ fontVariantNumeric: "tabular-nums" }}>{value}</span>;
}

/** 供应商成本口径的显示文案（合计块与表格小字共用）：页面不显示后端原词。 */
export function ProviderCostBasisText({ basis }: { basis: ProviderCostBasis }) {
  const { t } = useTranslation();
  const labels: Record<ProviderCostBasis, string> = {
    estimated: t("usageAnalytics.basis.estimated"),
    reconciled: t("usageAnalytics.basis.reconciled"),
    mixed: t("usageAnalytics.basis.mixed"),
  };
  return <>{labels[basis]}</>;
}

/** 覆盖说明的两行（格式由设计写死）：第一行恒在；第二行只在有不可分摊的记录时出现。 */
function CoverageNote({ totals }: { totals: UsageAnalyticsReportTotals }) {
  const { t } = useTranslation();
  // 每条 `<provider_code> <YYYY-MM>`，不完整的月份后加「 (partial)」；顺序同 reconciliation_coverage。
  const describe = (records: UsageAnalyticsReconciliationCoverage[]): string =>
    records
      .map((record) =>
        record.fully_in_range
          ? t("usageAnalytics.totals.coverageMonth", { provider: record.provider_code, month: record.billing_month })
          : t("usageAnalytics.totals.coverageMonthPartial", {
              provider: record.provider_code,
              month: record.billing_month,
            }),
      )
      .join(", ");
  const allocated = totals.reconciliation_coverage.filter((record) => record.allocatable);
  const unallocated = totals.reconciliation_coverage.filter((record) => !record.allocatable);
  return (
    <>
      <Typography.Paragraph type="secondary" style={{ marginTop: 8, marginBottom: 0 }}>
        {allocated.length === 0
          ? t("usageAnalytics.totals.reconciledMonthsNone")
          : t("usageAnalytics.totals.reconciledMonths", { months: describe(allocated) })}
      </Typography.Paragraph>
      {unallocated.length > 0 ? (
        <Typography.Paragraph type="secondary" style={{ marginTop: 0, marginBottom: 0 }}>
          {t("usageAnalytics.totals.notAllocated", {
            months: describe(unallocated),
            cost: totals.unallocated_reconciled_cost_myr ?? EMPTY,
          })}
        </Typography.Paragraph>
      ) : null}
    </>
  );
}

export function UsageAnalyticsTotals({ totals }: { totals: UsageAnalyticsReportTotals }) {
  const { t } = useTranslation();
  const payments = totals.payments;

  const items: DescriptionsProps["items"] = [
    {
      key: "usage_revenue",
      label: t("usageAnalytics.totals.usageRevenue"),
      children: <DecimalString value={totals.prepaid.billable_cost} />,
    },
    {
      key: "estimated_cost",
      label: t("usageAnalytics.totals.estimatedCost"),
      children: <DecimalString value={totals.prepaid.estimated_provider_cost_myr} />,
    },
    {
      key: "usage_gross_margin",
      label: t("usageAnalytics.totals.usageGrossMargin"),
      children: <DecimalString value={totals.prepaid.gross_margin} />,
    },
    {
      key: "gateway_fees",
      label: t("usageAnalytics.totals.gatewayFees"),
      children: <DecimalString value={payments === null ? null : payments.gateway_fee} />,
    },
    {
      key: "gross_profit",
      label: t("usageAnalytics.totals.grossProfit"),
      children: <DecimalString value={totals.gross_profit} />,
    },
    {
      key: "gross_profit_rate",
      label: t("usageAnalytics.totals.grossProfitRate"),
      children: <DecimalString value={totals.gross_profit_rate} />,
    },
    {
      key: "topups",
      label: t("usageAnalytics.totals.topups"),
      children: <DecimalString value={payments === null ? null : payments.topup_amount} />,
    },
    {
      key: "paid_payments",
      label: t("usageAnalytics.totals.paidPayments"),
      children: payments === null ? EMPTY : String(payments.paid_count),
    },
    {
      key: "internal_events",
      label: t("usageAnalytics.totals.internalEvents"),
      children: String(totals.internal_metered_only.event_count),
    },
    {
      key: "internal_estimated_cost",
      label: t("usageAnalytics.totals.internalEstimatedCost"),
      children: <DecimalString value={totals.internal_metered_only.estimated_provider_cost_myr} />,
    },
    {
      key: "internal_reference_price",
      label: t("usageAnalytics.field.referencePrice"),
      children: <DecimalString value={totals.internal_metered_only.reference_customer_price} />,
    },
    {
      key: "provider_cost",
      label: t("usageAnalytics.totals.providerCost"),
      children: <DecimalString value={totals.prepaid.provider_cost_myr} />,
    },
    {
      key: "provider_cost_basis",
      label: t("usageAnalytics.totals.providerCostBasis"),
      children: <ProviderCostBasisText basis={totals.prepaid.provider_cost_basis} />,
    },
    {
      key: "internal_provider_cost",
      label: t("usageAnalytics.totals.internalProviderCost"),
      children: <DecimalString value={totals.internal_metered_only.provider_cost_myr} />,
    },
    {
      key: "internal_provider_cost_basis",
      label: t("usageAnalytics.totals.internalProviderCostBasis"),
      children: <ProviderCostBasisText basis={totals.internal_metered_only.provider_cost_basis} />,
    },
    {
      key: "gross_margin_on_provider_cost",
      label: t("usageAnalytics.totals.grossMarginOnProviderCost"),
      children: <DecimalString value={totals.prepaid.gross_margin_on_provider_cost} />,
    },
    {
      key: "gross_margin_on_provider_cost_rate",
      label: t("usageAnalytics.totals.grossMarginOnProviderCostRate"),
      children: <DecimalString value={totals.prepaid.gross_margin_on_provider_cost_rate} />,
    },
    {
      key: "gross_profit_on_provider_cost",
      label: t("usageAnalytics.totals.grossProfitOnProviderCost"),
      children: <DecimalString value={totals.gross_profit_on_provider_cost} />,
    },
    {
      key: "gross_profit_on_provider_cost_rate",
      label: t("usageAnalytics.totals.grossProfitOnProviderCostRate"),
      children: <DecimalString value={totals.gross_profit_on_provider_cost_rate} />,
    },
  ];

  return (
    <section role="region" aria-label={t("usageAnalytics.totals.title")} style={{ marginBottom: 16 }}>
      <Descriptions
        bordered
        size="small"
        title={t("usageAnalytics.totals.title")}
        column={{ xs: 1, sm: 1, md: 2, lg: 3 }}
        items={items}
      />
      {payments !== null && payments.gateway_fee_unknown_count > 0 ? (
        <Typography.Paragraph type="warning" style={{ marginTop: 8, marginBottom: 0 }}>
          {t("usageAnalytics.totals.unknownFees", { unknown: payments.gateway_fee_unknown_count })}
        </Typography.Paragraph>
      ) : null}
      {payments === null ? (
        <Typography.Paragraph type="secondary" style={{ marginTop: 8, marginBottom: 0 }}>
          {t("usageAnalytics.totals.feesNotAttributable")}
        </Typography.Paragraph>
      ) : null}
      <CoverageNote totals={totals} />
      <Typography.Paragraph type="secondary" style={{ marginTop: 8, marginBottom: 0 }}>
        {t("usageAnalytics.totals.providerCostNote")}
      </Typography.Paragraph>
    </section>
  );
}

/**
 * 管理端 Rebill 详情页（`/rebills/:rebillId`，AIH-TASK-257；设计闸门 #830 v12 §2「管理端页面」）。
 *
 * 语义以 docs/api.md「管理端 Rebill」的详情为准：`GET /api/v1/admin/rebills/{rebill_id}`，只读。
 *
 * 头部是客户、事件数、两个合计与差额、账本方向与金额、两个更正版本（各自为 `null` 时那一项显示「Current version」：确认时取的
 * 是每个事件当时的有效版本）、原因、操作人与确认时刻。金额都是接口返回的字符串，原样显示，前端不做金额运算。
 *
 * 下方明细表是全部明细（按明细 id 升序，顺序由后端决定）。「Est. cost (MYR)」显示 `corrected_estimated_provider_cost_myr`
 * —— Acuven 内部的估算成本（INV-7），所以页面写明「成本与毛利仅管理员可见」；不显示别的成本字段。「Usage event」链接到
 * 用量事件详情，其余 `public_id` 都是纯文本；没有原对账单时「Statement」显示「—」。
 *
 * 404 `REBILL_NOT_FOUND` 是一个明确的答案（没有这条 Rebill），不是「出错了、请重试」。
 */

import { useQuery } from "@tanstack/react-query";
import { Card, Result, Table, Typography, type DescriptionsProps, type TableColumnsType } from "antd";
import { useTranslation } from "react-i18next";
import { Link, useParams } from "react-router";

import { REBILL_NOT_FOUND, getRebill, rebillDetailQueryKey, type RebillDetailLine } from "../../api/adminRebills";
import { ApiError } from "../../api/client";
import { DateTimeText } from "../../components/DateTimeText";
import { ROUTES, usageEventDetailPath } from "../../routes/paths";
import { LoadingBlock, SummaryList } from "../catalog/ProvidersPage";
import { AdminOnlyNote, EMPTY } from "../usage/UsageEventsPage";
import { RebillErrorAlert, useLedgerLabel } from "./RebillsPage";

export function RebillDetailPage() {
  const { t } = useTranslation();
  const { rebillId = "" } = useParams();
  const ledgerLabel = useLedgerLabel();

  const rebill = useQuery({
    queryKey: rebillDetailQueryKey(rebillId),
    queryFn: ({ signal }) => getRebill(rebillId, signal),
  });

  const backLink = <Link to={ROUTES.rebills}>{t("rebills.detail.backToList")}</Link>;

  if (rebill.isPending) {
    return (
      <Card>
        <LoadingBlock text={t("rebills.detail.loading")} />
      </Card>
    );
  }

  if (rebill.isError) {
    const { error } = rebill;
    if (error instanceof ApiError && error.code === REBILL_NOT_FOUND) {
      return (
        <Result
          status="404"
          title={t("rebills.detail.notFound")}
          subTitle={t("rebills.detail.notFoundBody")}
          extra={backLink}
        />
      );
    }
    return (
      <Card>
        <RebillErrorAlert error={error} title={t("rebills.detail.loadFailed")} onRetry={() => void rebill.refetch()} />
        {backLink}
      </Card>
    );
  }

  const current = rebill.data;
  const currentVersion = t("rebills.detail.currentVersion");

  const summary: NonNullable<DescriptionsProps["items"]> = [
    { key: "id", label: t("rebills.detail.id"), children: current.id },
    { key: "customer", label: t("rebills.field.customer"), children: current.customer_id },
    { key: "events", label: t("rebills.field.events"), children: String(current.event_count) },
    { key: "previous_total", label: t("rebills.detail.previousTotal"), children: current.previous_total },
    { key: "corrected_total", label: t("rebills.detail.correctedTotal"), children: current.corrected_total },
    { key: "difference", label: t("rebills.detail.difference"), children: current.difference },
    { key: "ledger", label: t("rebills.field.ledger"), children: ledgerLabel(current.ledger_type) },
    { key: "ledger_amount", label: t("rebills.detail.ledgerAmount"), children: current.ledger_amount },
    {
      key: "pricing_rule",
      label: t("rebills.detail.pricingRule"),
      children: current.corrective_pricing_rule_id ?? currentVersion,
    },
    {
      key: "price_version",
      label: t("rebills.detail.priceVersion"),
      children: current.corrective_provider_price_version_id ?? currentVersion,
    },
    { key: "reason", label: t("rebills.field.reason"), children: current.reason },
    { key: "confirmed_by", label: t("rebills.detail.confirmedBy"), children: current.created_by_email },
    {
      key: "confirmed_at",
      label: t("rebills.detail.confirmedAt"),
      children: <DateTimeText value={current.created_at} />,
    },
  ];

  const columns: TableColumnsType<RebillDetailLine> = [
    {
      key: "usage_event",
      title: t("rebills.line.usageEvent"),
      render: (_: unknown, line) => <Link to={usageEventDetailPath(line.usage_event_id)}>{line.usage_event_id}</Link>,
    },
    {
      key: "occurred_at",
      title: t("rebills.line.occurred"),
      render: (_: unknown, line) => <DateTimeText value={line.occurred_at} />,
    },
    { key: "previous_charge", title: t("rebills.line.previous"), dataIndex: "previous_charge" },
    { key: "corrected_charge", title: t("rebills.line.corrected"), dataIndex: "corrected_charge" },
    { key: "difference", title: t("rebills.line.difference"), dataIndex: "difference" },
    {
      key: "estimated_cost",
      title: t("rebills.line.estimatedCost"),
      dataIndex: "corrected_estimated_provider_cost_myr",
    },
    { key: "original_period", title: t("rebills.line.originalPeriod"), dataIndex: "original_period" },
    {
      key: "statement",
      title: t("rebills.line.statement"),
      render: (_: unknown, line) => line.original_statement_id ?? EMPTY,
    },
  ];

  return (
    <Card title={t("rebills.detail.title")} extra={backLink}>
      <AdminOnlyNote />
      <SummaryList items={summary} />
      <Typography.Title level={5}>{t("rebills.detail.lines")}</Typography.Title>
      <Table<RebillDetailLine>
        rowKey="usage_event_id"
        columns={columns}
        dataSource={current.lines}
        pagination={false}
        scroll={{ x: true }}
      />
    </Card>
  );
}

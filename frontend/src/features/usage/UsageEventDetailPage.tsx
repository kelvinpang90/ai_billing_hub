/**
 * 管理端用量事件详情（`GET /api/v1/admin/usage-events/{id}` 与单个重新入队，docs/api.md「管理端用量事件」两节）。
 *
 * 显示事件本身、计费快照与版本（链接到价格版本、定价规则与汇率页）、成本、计费额、毛利（标明 estimated）、
 * 账本引用、认领次数与认领信息、冲突记录。页面写明「成本与毛利仅管理员可见」。
 *
 * 错误状态且账本里没有它的行时给出「重新入队」（必填原因，二次确认）；`PROCESSED` 等其余状态不给 ——
 * 后端照样会回 409，这里只是不让人去点一个注定失败的按钮。成功后详情与全部列表过期重读。
 *
 * 已计费的预付事件（`PROCESSED` 且 `effective_charge` 不为 null，AIH-TASK-260）给出「Rebill」按钮（跳到新建 Rebill 页并预填
 * 客户与这个事件），并显示当前有效计费与更正历史（点一行进 Rebill 详情）；其他事件都不显示。`billable_cost` 与快照字段仍是
 * 事件处理时的原值。
 *
 * 汇率版本没有详情页（汇率页只有列表），所以链接到汇率页，id 原样列出。
 *
 * ⚠️ 金额、汇率与数量按十进制字符串显示（MoneyText / DecimalText），不经 number。
 */

import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Button,
  Card,
  Descriptions,
  Empty,
  Result,
  Space,
  Table,
  Tag,
  Typography,
  type DescriptionsProps,
  type TableColumnsType,
} from "antd";
import { useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { Link, useNavigate, useParams } from "react-router";

import {
  USAGE_EVENTS_QUERY_KEY,
  USAGE_EVENT_NOT_FOUND,
  getUsageEvent,
  isRequeuableStatus,
  requeueUsageEvent,
  usageEventDetailQueryKey,
  type UsageEventConflict,
  type UsageEventDetail,
  type UsageEventRebill,
} from "../../api/adminUsageEvents";
import { ApiError } from "../../api/client";
import { DateTimeText } from "../../components/DateTimeText";
import { DecimalText } from "../../components/DecimalText";
import {
  ROUTES,
  catalogProviderDetailPath,
  customerDetailPath,
  newRebillPath,
  pricingRuleDetailPath,
  providerPriceDetailPath,
  rebillDetailPath,
} from "../../routes/paths";
import { CodeText, LoadingBlock, NoticeAlert } from "../catalog/ProvidersPage";
import {
  AdminOnlyNote,
  EMPTY,
  MYR,
  OptionalMoney,
  QuantityText,
  RequeueModal,
  UsageErrorAlert,
  UsageStatusTag,
  tokenCount,
} from "./UsageEventsPage";

/** 错误状态、且没有账本行（`LEDGER_CONFLICT` 的 `FAILED_FINAL` 有，后端会拒）。 */
export function canRequeue(event: UsageEventDetail): boolean {
  return isRequeuableStatus(event.status) && event.wallet_transaction === null;
}

/**
 * 已计费的预付事件（AIH-TASK-256：`PROCESSED` 且 `effective_charge` 不为 null）可以 Rebill，也有当前有效计费与更正历史。
 * 内部计量、未计费与错误状态的事件 `effective_charge` 为 null。
 */
export function canRebill(event: UsageEventDetail): boolean {
  return event.status === "PROCESSED" && event.effective_charge !== null;
}

export function UsageEventDetailPage() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const { usageEventId = "" } = useParams();
  const queryClient = useQueryClient();
  const [requeuing, setRequeuing] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);

  const event = useQuery({
    queryKey: usageEventDetailQueryKey(usageEventId),
    queryFn: ({ signal }) => getUsageEvent(usageEventId, signal),
  });

  const backLink = <Link to={ROUTES.usageEvents}>{t("usage.detail.backToList")}</Link>;

  if (event.isPending) {
    return (
      <Card>
        <LoadingBlock text={t("usage.detail.loading")} />
      </Card>
    );
  }

  if (event.isError) {
    const { error } = event;
    // 404 是一个明确的答案（没有这个事件），不是「出错了、请重试」。
    if (error instanceof ApiError && error.code === USAGE_EVENT_NOT_FOUND) {
      return (
        <Result
          status="404"
          title={t("usage.detail.notFound")}
          subTitle={t("usage.detail.notFoundBody")}
          extra={backLink}
        />
      );
    }
    return (
      <Card>
        <UsageErrorAlert error={error} title={t("usage.detail.loadFailed")} onRetry={() => void event.refetch()} />
        {backLink}
      </Card>
    );
  }

  const current = event.data;
  const subject = (
    <Space size={4} wrap>
      <CodeText code={current.id} />
      <UsageStatusTag status={current.status} />
    </Space>
  );

  const finish = (): void => {
    // 详情与全部列表一起过期（同一个前缀）。
    void queryClient.invalidateQueries({ queryKey: USAGE_EVENTS_QUERY_KEY });
    setRequeuing(false);
    setNotice(t("usage.requeue.done"));
  };

  let actions: ReactNode = null;
  if (canRequeue(current)) {
    actions = (
      <Button
        danger
        onClick={() => {
          setNotice(null);
          setRequeuing(true);
        }}
      >
        {t("usage.requeue.open")}
      </Button>
    );
  } else if (canRebill(current)) {
    actions = (
      <Button onClick={() => void navigate(newRebillPath(current.customer_id, current.id))}>
        {t("usage.rebill.open")}
      </Button>
    );
  }

  return (
    <Space direction="vertical" size="middle" style={{ width: "100%" }}>
      {backLink}
      <NoticeAlert message={notice} />
      <Card title={subject} extra={actions}>
        <AdminOnlyNote />
        <EventDescriptions event={current} />
      </Card>
      <Card title={t("usage.detail.billing")}>
        <BillingDescriptions event={current} />
      </Card>
      {canRebill(current) ? (
        <Card title={t("usage.rebill.history")}>
          <RebillHistory event={current} />
        </Card>
      ) : null}
      <Card title={t("usage.detail.processing")}>
        <ProcessingDescriptions event={current} />
      </Card>
      <Card title={t("usage.detail.conflicts")}>
        <ConflictsTable conflicts={current.conflicts} />
      </Card>

      {requeuing ? (
        <RequeueModal
          title={t("usage.requeue.title")}
          confirmTitle={t("usage.requeue.confirmTitle")}
          subject={<Typography.Paragraph>{subject}</Typography.Paragraph>}
          run={(reason) => requeueUsageEvent(current.id, reason)}
          onDone={finish}
          onCancel={() => setRequeuing(false)}
        />
      ) : null}
    </Space>
  );
}

/** 可空的文字值：有就等宽原样，没有就「—」。 */
function OptionalCode({ value }: { value: string | null }) {
  return value === null ? <>{EMPTY}</> : <CodeText code={value} />;
}

function OptionalTime({ value }: { value: string | null }) {
  return value === null ? <>{EMPTY}</> : <DateTimeText value={value} />;
}

/** 指向另一个详情页的 id；没有时「—」。 */
function RefLink({ id, to }: { id: string | null; to: (id: string) => string }) {
  return id === null ? <>{EMPTY}</> : <Link to={to(id)}>{id}</Link>;
}

function EventDescriptions({ event }: { event: UsageEventDetail }) {
  const { t } = useTranslation();
  const items: DescriptionsProps["items"] = [
    { key: "id", label: t("usage.field.id"), children: <CodeText code={event.id} /> },
    { key: "event_id", label: t("usage.field.eventId"), children: <CodeText code={event.event_id} /> },
    {
      key: "customer",
      label: t("usage.field.customer"),
      children: <Link to={customerDetailPath(event.customer_id)}>{event.customer_company_name}</Link>,
    },
    { key: "project_id", label: t("usage.field.projectId"), children: <CodeText code={event.project_id} /> },
    { key: "request_id", label: t("usage.field.requestId"), children: <CodeText code={event.request_id} /> },
    {
      key: "conversation_id",
      label: t("usage.field.conversationId"),
      children: <OptionalCode value={event.conversation_id} />,
    },
    { key: "provider", label: t("usage.field.provider"), children: <CodeText code={event.provider} /> },
    { key: "model", label: t("usage.field.model"), children: <CodeText code={event.model} /> },
    { key: "usage_type", label: t("usage.field.usageType"), children: <CodeText code={event.usage_type} /> },
    { key: "quantity", label: t("usage.field.quantity"), children: <QuantityText event={event} /> },
    { key: "unit", label: t("usage.field.unit"), children: <CodeText code={event.unit} /> },
    {
      key: "cache_tokens",
      label: t("usage.field.cacheTokens"),
      children: t("usage.cacheTokens", {
        write: tokenCount(event.cache_creation_input_tokens),
        read: tokenCount(event.cache_read_input_tokens),
      }),
    },
    { key: "status", label: t("usage.field.status"), children: <UsageStatusTag status={event.status} /> },
    { key: "error_code", label: t("usage.field.errorCode"), children: <OptionalCode value={event.error_code} /> },
    {
      key: "error_message",
      label: t("usage.field.errorMessage"),
      children: <OptionalCode value={event.error_message} />,
    },
    { key: "occurred_at", label: t("usage.field.occurredAt"), children: <DateTimeText value={event.occurred_at} /> },
    { key: "received_at", label: t("usage.field.receivedAt"), children: <DateTimeText value={event.received_at} /> },
    { key: "processed_at", label: t("usage.field.processedAt"), children: <OptionalTime value={event.processed_at} /> },
    { key: "created_at", label: t("usage.field.createdAt"), children: <DateTimeText value={event.created_at} /> },
    {
      key: "schema_version",
      label: t("usage.field.schemaVersion"),
      children: <CodeText code={event.schema_version} />,
    },
    {
      key: "payload_shape",
      label: t("usage.field.payloadShape"),
      children: <CodeText code={event.payload_shape} />,
    },
    {
      key: "quantity_kind",
      label: t("usage.field.quantityKind"),
      children: <CodeText code={event.quantity_kind} />,
    },
    {
      key: "payload_fingerprint",
      label: t("usage.field.payloadFingerprint"),
      children: <CodeText code={event.payload_fingerprint} />,
    },
  ];
  return <Descriptions bordered size="small" column={1} items={items} />;
}

/** 毛利：金额加「estimated」标记（basis 由后端给，只有预付事件有）。 */
function GrossMargin({ event }: { event: UsageEventDetail }) {
  const { t } = useTranslation();
  if (event.gross_margin === null) {
    return <>{EMPTY}</>;
  }
  return (
    <Space size={4}>
      <OptionalMoney amount={event.gross_margin} />
      {event.gross_margin_basis === "estimated" ? <Tag color="gold">{t("usage.estimated")}</Tag> : null}
    </Space>
  );
}

function BillingDescriptions({ event }: { event: UsageEventDetail }) {
  const { t } = useTranslation();
  const ledger: ReactNode =
    event.wallet_transaction === null ? (
      EMPTY
    ) : (
      <Space size={4} wrap>
        <CodeText code={event.wallet_transaction.id} />
        <OptionalMoney amount={event.wallet_transaction.amount} />
      </Space>
    );
  const sourceCost: ReactNode =
    event.provider_source_cost === null ? (
      EMPTY
    ) : (
      <OptionalMoney amount={event.provider_source_cost} currency={event.provider_source_currency ?? ""} />
    );
  const items: DescriptionsProps["items"] = [
    {
      key: "billing_mode",
      label: t("usage.field.billingMode"),
      children: <OptionalCode value={event.billing_mode_snapshot} />,
    },
    {
      key: "provider_ref",
      label: t("usage.field.providerRef"),
      children: <RefLink id={event.provider_ref_id} to={catalogProviderDetailPath} />,
    },
    { key: "model_ref", label: t("usage.field.modelRef"), children: <OptionalCode value={event.model_ref_id} /> },
    {
      key: "price_version",
      label: t("usage.field.priceVersion"),
      children: <RefLink id={event.provider_price_version_id} to={providerPriceDetailPath} />,
    },
    {
      key: "pricing_rule",
      label: t("usage.field.pricingRule"),
      children: <RefLink id={event.pricing_rule_id} to={pricingRuleDetailPath} />,
    },
    {
      key: "fx_rate_version",
      label: t("usage.field.fxRateVersion"),
      children: <RefLink id={event.fx_rate_version_id} to={() => ROUTES.fxRates} />,
    },
    {
      key: "fx_rate_applied",
      label: t("usage.field.fxRateApplied"),
      children: event.fx_rate_applied === null ? EMPTY : <DecimalText value={event.fx_rate_applied} />,
    },
    { key: "source_cost", label: t("usage.field.sourceCost"), children: sourceCost },
    {
      key: "estimated_cost",
      label: t("usage.field.estimatedCost"),
      children: <OptionalMoney amount={event.estimated_provider_cost_myr} />,
    },
    {
      key: "billable_cost",
      label: t("usage.field.billableCost"),
      children: <OptionalMoney amount={event.billable_cost} currency={MYR} />,
    },
    { key: "gross_margin", label: t("usage.field.grossMargin"), children: <GrossMargin event={event} /> },
    {
      key: "reference_price",
      label: t("usage.field.referencePrice"),
      children: <OptionalMoney amount={event.reference_customer_price} />,
    },
    { key: "ledger", label: t("usage.field.ledger"), children: ledger },
  ];
  return (
    <>
      <Typography.Paragraph type="secondary">{t("usage.detail.billingNote")}</Typography.Paragraph>
      <Descriptions bordered size="small" column={1} items={items} />
    </>
  );
}

function ProcessingDescriptions({ event }: { event: UsageEventDetail }) {
  const { t } = useTranslation();
  const items: DescriptionsProps["items"] = [
    { key: "attempt_count", label: t("usage.field.attemptCount"), children: String(event.attempt_count) },
    {
      key: "next_attempt_at",
      label: t("usage.field.nextAttemptAt"),
      children: <OptionalTime value={event.next_attempt_at} />,
    },
    { key: "claim_token", label: t("usage.field.claimToken"), children: <OptionalCode value={event.claim_token} /> },
    { key: "claimed_at", label: t("usage.field.claimedAt"), children: <OptionalTime value={event.claimed_at} /> },
    {
      key: "lease_expires_at",
      label: t("usage.field.leaseExpiresAt"),
      children: <OptionalTime value={event.lease_expires_at} />,
    },
  ];
  return <Descriptions bordered size="small" column={1} items={items} />;
}

/** 当前有效计费与更正历史（按明细 id 升序，顺序由后端决定）。金额原样显示；点一行进 Rebill 详情。 */
function RebillHistory({ event }: { event: UsageEventDetail }) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const items: DescriptionsProps["items"] = [
    {
      key: "effective_charge",
      label: t("usage.rebill.effectiveCharge"),
      children: <OptionalMoney amount={event.effective_charge} currency={MYR} />,
    },
  ];
  const open = (rebill: UsageEventRebill): void => {
    void navigate(rebillDetailPath(rebill.rebill_id));
  };
  const columns: TableColumnsType<UsageEventRebill> = [
    {
      key: "rebill",
      title: t("usage.rebill.field.rebill"),
      render: (_: unknown, rebill) => <CodeText code={rebill.rebill_id} />,
    },
    {
      key: "created_at",
      title: t("rebills.field.createdAt"),
      render: (_: unknown, rebill) => <DateTimeText value={rebill.created_at} />,
    },
    { key: "previous_charge", title: t("rebills.line.previous"), dataIndex: "previous_charge" },
    { key: "corrected_charge", title: t("rebills.line.corrected"), dataIndex: "corrected_charge" },
    { key: "difference", title: t("rebills.line.difference"), dataIndex: "difference" },
  ];
  return (
    <>
      <Typography.Paragraph type="secondary">{t("usage.rebill.note")}</Typography.Paragraph>
      <Descriptions bordered size="small" column={1} items={items} style={{ marginBottom: 16 }} />
      <Table<UsageEventRebill>
        size="small"
        rowKey="rebill_id"
        columns={columns}
        dataSource={event.rebills}
        pagination={false}
        locale={{ emptyText: t("usage.rebill.empty") }}
        onRow={(rebill) => ({
          onClick: () => open(rebill),
          onKeyDown: (keyEvent) => {
            if (keyEvent.key === "Enter" || keyEvent.key === " ") {
              keyEvent.preventDefault();
              open(rebill);
            }
          },
          tabIndex: 0,
          style: { cursor: "pointer" },
        })}
      />
    </>
  );
}

type ConflictRow = UsageEventConflict & { key: string };

function ConflictsTable({ conflicts }: { conflicts: readonly UsageEventConflict[] }) {
  const { t } = useTranslation();
  if (conflicts.length === 0) {
    return <Empty description={t("usage.detail.noConflicts")} />;
  }
  // 冲突行没有 id，同一请求方可以在同一秒撞两次：按写入顺序的位置区分。
  const rows: ConflictRow[] = conflicts.map((conflict, index) => ({ ...conflict, key: String(index) }));
  const columns: TableColumnsType<ConflictRow> = [
    {
      key: "received_at",
      title: t("usage.field.receivedAt"),
      render: (_: unknown, conflict) => <DateTimeText value={conflict.received_at} />,
    },
    {
      key: "api_key",
      title: t("usage.field.apiKey"),
      render: (_: unknown, conflict) => <CodeText code={conflict.api_key} />,
    },
    {
      key: "mismatch",
      title: t("usage.field.mismatch"),
      render: (_: unknown, conflict) => <Tag color="magenta">{conflict.mismatch}</Tag>,
    },
  ];
  return (
    <Table<ConflictRow> size="small" rowKey="key" columns={columns} dataSource={rows} pagination={false} />
  );
}

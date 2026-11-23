/**
 * 管理端供应商成本页（`/provider-costs`，AIH-TASK-246；设计闸门 #755 v1 §2「管理端页面」）。
 *
 * 语义以 docs/api.md「管理端供应商成本对账」为准：上面是一个供应商的月份总览（`GET …/months`），每个已结束的 UTC 月一行，
 * 最近的在前、不分页；未对账的行可「Reconcile」，已对账的行可「Correct」，两者打开同一个表单：先「Preview」（不写库），
 * 再「Confirm」（写一行不可变记录）。点已对账的行展开详情：整条更正链与「确认之后估算已变化」的提示。
 *
 * ⚠️ 「Confirm」只在本次预览成功、且表单在预览后没被改动时可点；提交带上**预览返回的**事件数与来源成本两个期望值。表单
 * 一有改动就丢掉预览结果（预览还在路上时改了表单，那次结果也不用）。
 *
 * ⚠️ 整页**不用 `Alert`、没有 `role="alert"`**：错误是一段 `Typography.Text type="danger"`，成功是 `message.success`。表单也
 * 因此不用 antd 的校验规则（`Form.Item` 的错误列表带 `role="alert"`）：不合规时「Preview」不可点，规则写在字段下面的说明里。
 *
 * 金额、汇率与比率原样显示后端字符串，不解析、不舍入；没有值的格子显示「—」。只请求 `/api/v1/admin/` 路径。
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  App,
  Button,
  Card,
  Descriptions,
  Form,
  Input,
  Modal,
  Select,
  Skeleton,
  Space,
  Table,
  Tag,
  Typography,
  type DescriptionsProps,
  type TableColumnsType,
} from "antd";
import { useRef, useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";

import { AI_PROVIDER_NOT_FOUND } from "../../api/adminCatalog";
import { ADMIN_REQUIRED } from "../../api/adminCustomers";
import {
  PROVIDER_CODES,
  PROVIDER_COSTS_QUERY_KEY,
  RECONCILIATION_ESTIMATE_CHANGED,
  RECONCILIATION_EXISTS,
  RECONCILIATION_NOT_CURRENT,
  SOURCE_CURRENCIES,
  SOURCE_TOTAL_PATTERN,
  TEXT_FIELD_MAX,
  confirmProviderCost,
  getProviderCostDetail,
  getProviderCostMonths,
  previewProviderCost,
  providerCostDetailQueryKey,
  providerCostMonthsQueryKey,
  type MonthItem,
  type PreviewBody,
  type ReconciliationView,
} from "../../api/adminProviderCosts";
import { ApiError } from "../../api/client";
import { DateTimeText } from "../../components/DateTimeText";
import { RequestReference } from "../../components/RequestReference";

/** 没有值的格子（U+2014）。纯标点，不进翻译文件。 */
const EMPTY = "—";

const PROVIDER_OPTIONS = PROVIDER_CODES.map((code) => ({ value: code, label: code }));
const CURRENCY_OPTIONS = SOURCE_CURRENCIES.map((currency) => ({ value: currency, label: currency }));

// 表单控件的 id：label 的 htmlFor 指向它们（不用 Form 的 name，见文件头）。
const PROVIDER_FIELD_ID = "provider-cost-provider";
const CURRENCY_FIELD_ID = "provider-cost-currency";
const AMOUNT_FIELD_ID = "provider-cost-amount";
const REFERENCE_FIELD_ID = "provider-cost-reference";
const REASON_FIELD_ID = "provider-cost-reason";

/** 来源币种的金额：后端字符串原样，后面跟币种。 */
function SourceAmount({ amount, currency }: { amount: string; currency: string }) {
  return (
    <>
      {amount}{" "}
      <Typography.Text type="secondary">{currency}</Typography.Text>
    </>
  );
}

/** 原币差额率：null 显示「—」；超阈值时值旁边跟一个文字标记（只在原币差额率这里加，MYR 差额率不加）。 */
function SourceVarianceRate({ view }: { view: ReconciliationView }) {
  const { t } = useTranslation();
  return (
    <>
      {view.source_variance_rate ?? EMPTY}
      {view.exceeds_threshold ? (
        <>
          {" "}
          <Tag color="warning">{t("providerCosts.aboveThreshold")}</Tag>
        </>
      ) : null}
    </>
  );
}

/** 已对账行当前版本的 id；未对账为 null。 */
function currentId(month: MonthItem): string | null {
  return month.status === "RECONCILED" ? (month.current?.reconciliation_id ?? null) : null;
}

function MonthStatusText({ month }: { month: MonthItem }) {
  const { t } = useTranslation();
  return month.status === "RECONCILED" ? (
    <Tag color="success">{t("providerCosts.status.reconciled")}</Tag>
  ) : (
    <Tag>{t("providerCosts.status.notReconciled")}</Tag>
  );
}

/**
 * 失败时的一段普通文字：标题、错误码、信息与 request_id（标题的取法同 `CatalogErrorAlert`：403 说没有权限，供应商查不到
 * 单独说）。不是 `Alert`，没有 `role="alert"`。
 */
function ErrorText({ error, title }: { error: Error; title: string }) {
  const { t } = useTranslation();
  const code = error instanceof ApiError ? error.code : null;
  let heading = title;
  if (code === ADMIN_REQUIRED) {
    heading = t("providerCosts.forbidden");
  } else if (code === AI_PROVIDER_NOT_FOUND) {
    heading = t("providerCosts.providerNotFound");
  }
  return (
    <Typography.Paragraph>
      <Typography.Text type="danger">
        <span>{heading}</span>
        {code === null ? null : (
          <>
            {" "}
            <Typography.Text code>{code}</Typography.Text>
          </>
        )}{" "}
        <RequestReference error={error} />
      </Typography.Text>
    </Typography.Paragraph>
  );
}

function Loading({ text }: { text: string }) {
  return (
    <>
      <Skeleton active title={false} paragraph={{ rows: 3 }} />
      <Typography.Text type="secondary">{text}</Typography.Text>
    </>
  );
}

/** 已对账行展开后的详情：确认之后估算是否变了，以及整条更正链（最早的在前，顺序由后端决定）。 */
function ReconciliationDetailPanel({ reconciliationId }: { reconciliationId: string }) {
  const { t } = useTranslation();
  const detail = useQuery({
    queryKey: providerCostDetailQueryKey(reconciliationId),
    queryFn: ({ signal }) => getProviderCostDetail(reconciliationId, signal),
  });

  const columns: TableColumnsType<ReconciliationView> = [
    {
      key: "created_at",
      title: t("providerCosts.detail.confirmedAt"),
      render: (_: unknown, view) => (view.created_at === null ? EMPTY : <DateTimeText value={view.created_at} />),
    },
    {
      key: "created_by_email",
      title: t("providerCosts.detail.confirmedBy"),
      render: (_: unknown, view) => view.created_by_email ?? EMPTY,
    },
    {
      key: "reason",
      title: t("providerCosts.detail.reason"),
      render: (_: unknown, view) => view.reason ?? EMPTY,
    },
    {
      key: "source_total",
      title: t("providerCosts.field.actualSource"),
      render: (_: unknown, view) => <SourceAmount amount={view.source_total} currency={view.source_currency} />,
    },
    {
      key: "reconciled_cost_myr",
      title: t("providerCosts.field.actualMyr"),
      dataIndex: "reconciled_cost_myr",
    },
    {
      key: "estimated_cost_myr",
      title: t("providerCosts.field.estimatedMyr"),
      dataIndex: "estimated_cost_myr",
    },
    {
      key: "is_current",
      title: t("providerCosts.detail.version"),
      render: (_: unknown, view) =>
        view.is_current === true ? (
          <Tag color="success">{t("providerCosts.detail.current")}</Tag>
        ) : (
          <Tag>{t("providerCosts.detail.superseded")}</Tag>
        ),
    },
  ];

  let body: ReactNode;
  if (detail.isPending) {
    body = <Loading text={t("providerCosts.detail.loading")} />;
  } else if (detail.isError) {
    body = <ErrorText error={detail.error} title={t("providerCosts.detail.loadFailed")} />;
  } else {
    const live = detail.data.live_estimate;
    body = (
      <>
        {detail.data.estimate_changed_since_confirmation ? (
          <Typography.Paragraph>
            <Typography.Text type="warning">{t("providerCosts.detail.estimateChanged")}</Typography.Text>{" "}
            <Typography.Text type="secondary">
              {t("providerCosts.detail.liveEstimate", {
                events: live.estimated_event_count,
                source: live.estimated_source_cost,
                currency: detail.data.reconciliation.source_currency,
                myr: live.estimated_cost_myr,
                mismatch: live.currency_mismatch_count,
                unprocessed: live.unprocessed_event_count,
              })}
            </Typography.Text>
          </Typography.Paragraph>
        ) : null}
        <Table<ReconciliationView>
          rowKey={(view) => view.reconciliation_id ?? ""}
          columns={columns}
          dataSource={detail.data.chain}
          pagination={false}
          size="small"
        />
      </>
    );
  }

  return (
    <div role="region" aria-label={t("providerCosts.detail.title")}>
      <Typography.Title level={5}>{t("providerCosts.detail.title")}</Typography.Title>
      {body}
    </div>
  );
}

/** 表单打开的是哪个月；`current` 不为 null 时是更正（取代它）。 */
interface FormTarget {
  month: MonthItem;
  current: ReconciliationView | null;
}

interface Draft {
  currency: string;
  amount: string;
  reference: string;
  reason: string;
}

/** Reconcile：表单为空、币种 USD。Correct：预填当前记录的币种、金额与来源说明，理由为空。 */
function initialDraft(current: ReconciliationView | null): Draft {
  if (current === null) {
    return { currency: "USD", amount: "", reference: "", reason: "" };
  }
  return {
    currency: current.source_currency,
    amount: current.source_total,
    reference: current.source_document_reference,
    reason: "",
  };
}

function trimmedWithin(value: string): boolean {
  const trimmed = value.trim();
  return trimmed.length > 0 && trimmed.length <= TEXT_FIELD_MAX;
}

/** 两条会让表单失去意义的 409：该月已有记录，或要更正的那一版已被取代。都要重拉月份总览。 */
function isStale(error: unknown): boolean {
  return (
    error instanceof ApiError && (error.code === RECONCILIATION_EXISTS || error.code === RECONCILIATION_NOT_CURRENT)
  );
}

function asError(error: unknown): Error {
  return error instanceof Error ? error : new Error(String(error));
}

/** 预览结果与诊断计数。金额与比率原样显示。 */
function PreviewResult({ view }: { view: ReconciliationView }) {
  const { t } = useTranslation();
  const items: DescriptionsProps["items"] = [
    {
      key: "actual_source",
      label: t("providerCosts.field.actualSource"),
      children: <SourceAmount amount={view.source_total} currency={view.source_currency} />,
    },
    { key: "events", label: t("providerCosts.preview.estimatedEvents"), children: view.estimated_event_count },
    {
      key: "estimated_source",
      label: t("providerCosts.preview.estimatedSource"),
      children: <SourceAmount amount={view.estimated_source_cost} currency={view.source_currency} />,
    },
    { key: "estimated_myr", label: t("providerCosts.field.estimatedMyr"), children: view.estimated_cost_myr },
    { key: "actual_myr", label: t("providerCosts.field.actualMyr"), children: view.reconciled_cost_myr },
    {
      key: "fx_rate",
      label: t("providerCosts.preview.fxRate"),
      children: view.fx_rate_applied ?? t("providerCosts.preview.noFx"),
    },
    { key: "fx_moment", label: t("providerCosts.preview.fxMoment"), children: view.fx_rate_moment ?? EMPTY },
    {
      key: "source_variance",
      label: t("providerCosts.preview.sourceVariance"),
      children: <SourceAmount amount={view.source_variance} currency={view.source_currency} />,
    },
    {
      key: "source_variance_rate",
      label: t("providerCosts.field.sourceVarianceRate"),
      children: <SourceVarianceRate view={view} />,
    },
    { key: "myr_variance", label: t("providerCosts.preview.myrVariance"), children: view.myr_variance },
    {
      key: "myr_variance_rate",
      label: t("providerCosts.field.myrVarianceRate"),
      children: view.myr_variance_rate ?? EMPTY,
    },
  ];
  const diagnostics: DescriptionsProps["items"] = [
    { key: "unprocessed", label: t("providerCosts.diagnostics.unprocessed"), children: view.unprocessed_event_count },
    { key: "unsettled", label: t("providerCosts.diagnostics.unsettled"), children: view.outbox_unsettled_count },
    { key: "discarded", label: t("providerCosts.diagnostics.discarded"), children: view.outbox_discarded_count },
  ];
  return (
    <div role="region" aria-label={t("providerCosts.preview.title")}>
      <Descriptions bordered size="small" column={1} title={t("providerCosts.preview.title")} items={items} />
      <Descriptions
        bordered
        size="small"
        column={1}
        title={t("providerCosts.diagnostics.title")}
        items={diagnostics}
        style={{ marginTop: 16 }}
      />
      <Typography.Paragraph type="secondary" style={{ marginTop: 8 }}>
        {t("providerCosts.diagnostics.note")}
      </Typography.Paragraph>
    </div>
  );
}

/** Reconcile 与 Correct 共用的表单：先预览，表单没改动才能确认。 */
function ReconcileModal({
  providerCode,
  target,
  onClose,
  onConfirmed,
  onStale,
}: {
  providerCode: string;
  target: FormTarget;
  onClose: () => void;
  onConfirmed: () => void;
  onStale: () => void;
}) {
  const { t } = useTranslation();
  const correcting = target.current !== null;
  const [draft, setDraft] = useState<Draft>(() => initialDraft(target.current));
  const [preview, setPreview] = useState<{ body: PreviewBody; result: ReconciliationView } | null>(null);
  const [failure, setFailure] = useState<{ error: Error; confirming: boolean } | null>(null);
  const [estimateChanged, setEstimateChanged] = useState(false);
  // 每次改动表单加一：预览回来时对不上，说明这份结果对应的已经不是表单上的值了。
  const editVersion = useRef(0);
  // `isPending` 要等下一次渲染才变 true，挡不住同一帧里的第二次点击。
  const confirming = useRef(false);

  const previewing = useMutation({ mutationFn: previewProviderCost });
  const confirmation = useMutation({ mutationFn: confirmProviderCost });
  const busy = previewing.isPending || confirmation.isPending;

  const change = (patch: Partial<Draft>): void => {
    editVersion.current += 1;
    setDraft((previous) => ({ ...previous, ...patch }));
    setPreview(null);
    setFailure(null);
    setEstimateChanged(false);
  };

  const amountValid = SOURCE_TOTAL_PATTERN.test(draft.amount);
  const referenceValid = trimmedWithin(draft.reference);
  const reasonValid = !correcting || trimmedWithin(draft.reason);
  const valid = amountValid && referenceValid && reasonValid;

  const body: PreviewBody = {
    provider_code: providerCode,
    billing_month: target.month.billing_month,
    source_currency: draft.currency,
    source_total: draft.amount,
    source_document_reference: draft.reference.trim(),
    supersedes_id: target.current?.reconciliation_id ?? null,
    reason: correcting ? draft.reason.trim() : null,
  };

  const runPreview = (): void => {
    const version = editVersion.current;
    setFailure(null);
    setEstimateChanged(false);
    setPreview(null);
    previewing.mutate(body, {
      onSuccess: (result) => {
        if (version === editVersion.current) {
          setPreview({ body, result });
        }
      },
      onError: (error) => {
        if (isStale(error)) {
          onStale();
        } else if (version === editVersion.current) {
          setFailure({ error: asError(error), confirming: false });
        }
      },
    });
  };

  const runConfirm = (): void => {
    if (preview === null || confirming.current) {
      return;
    }
    confirming.current = true;
    setFailure(null);
    confirmation.mutate(
      {
        ...preview.body,
        expected_event_count: preview.result.estimated_event_count,
        expected_estimated_source_cost: preview.result.estimated_source_cost,
      },
      {
        onSuccess: () => onConfirmed(),
        onError: (error) => {
          if (error instanceof ApiError && error.code === RECONCILIATION_ESTIMATE_CHANGED) {
            setPreview(null);
            setEstimateChanged(true);
          } else if (isStale(error)) {
            onStale();
          } else {
            setFailure({ error: asError(error), confirming: true });
          }
        },
        onSettled: () => {
          confirming.current = false;
        },
      },
    );
  };

  const title = correcting
    ? t("providerCosts.form.correctTitle", { month: target.month.billing_month })
    : t("providerCosts.form.reconcileTitle", { month: target.month.billing_month });

  return (
    <Modal
      open
      title={title}
      onCancel={onClose}
      footer={[
        <Button key="cancel" onClick={onClose}>
          {t("providerCosts.form.cancel")}
        </Button>,
        <Button key="preview" onClick={runPreview} disabled={!valid || busy} loading={previewing.isPending}>
          {t("providerCosts.form.preview")}
        </Button>,
        <Button
          key="confirm"
          type="primary"
          onClick={runConfirm}
          disabled={preview === null || busy}
          loading={confirmation.isPending}
        >
          {t("providerCosts.form.confirm")}
        </Button>,
      ]}
    >
      <Form layout="vertical" component="div">
        <Form.Item label={t("providerCosts.form.currency")} htmlFor={CURRENCY_FIELD_ID}>
          <Select
            id={CURRENCY_FIELD_ID}
            value={draft.currency}
            options={CURRENCY_OPTIONS}
            onChange={(currency: string) => change({ currency })}
          />
        </Form.Item>
        <Form.Item
          label={t("providerCosts.form.amount")}
          htmlFor={AMOUNT_FIELD_ID}
          extra={t("providerCosts.form.amountHint")}
        >
          <Input
            id={AMOUNT_FIELD_ID}
            value={draft.amount}
            inputMode="decimal"
            autoComplete="off"
            spellCheck={false}
            onChange={(event) => change({ amount: event.target.value })}
          />
        </Form.Item>
        <Form.Item
          label={t("providerCosts.form.reference")}
          htmlFor={REFERENCE_FIELD_ID}
          extra={t("providerCosts.form.referenceHint")}
        >
          <Input
            id={REFERENCE_FIELD_ID}
            value={draft.reference}
            autoComplete="off"
            onChange={(event) => change({ reference: event.target.value })}
          />
        </Form.Item>
        {correcting ? (
          <Form.Item
            label={t("providerCosts.form.reason")}
            htmlFor={REASON_FIELD_ID}
            extra={t("providerCosts.form.reasonHint")}
          >
            <Input
              id={REASON_FIELD_ID}
              value={draft.reason}
              autoComplete="off"
              onChange={(event) => change({ reason: event.target.value })}
            />
          </Form.Item>
        ) : null}
      </Form>
      <Typography.Paragraph type="secondary">{t("providerCosts.form.confirmHint")}</Typography.Paragraph>
      {estimateChanged ? (
        <Typography.Paragraph>
          <Typography.Text type="warning">{t("providerCosts.form.estimateChanged")}</Typography.Text>
        </Typography.Paragraph>
      ) : null}
      {failure === null ? null : (
        <ErrorText
          error={failure.error}
          title={failure.confirming ? t("providerCosts.form.confirmFailed") : t("providerCosts.form.previewFailed")}
        />
      )}
      {preview === null ? null : <PreviewResult view={preview.result} />}
    </Modal>
  );
}

export function ProviderCostsPage() {
  const { t } = useTranslation();
  const { message } = App.useApp();
  const queryClient = useQueryClient();
  const [providerCode, setProviderCode] = useState<string>(PROVIDER_CODES[0]);
  const [target, setTarget] = useState<FormTarget | null>(null);
  const [stale, setStale] = useState(false);

  const months = useQuery({
    queryKey: providerCostMonthsQueryKey(providerCode),
    queryFn: ({ signal }) => getProviderCostMonths(providerCode, signal),
  });

  // 总览与已展开的详情一起过期重读：确认之后链头变了，详情也要跟着变。
  const reload = (): void => {
    void queryClient.invalidateQueries({ queryKey: PROVIDER_COSTS_QUERY_KEY });
  };

  const open = (month: MonthItem): void => {
    setStale(false);
    setTarget({ month, current: month.status === "RECONCILED" ? month.current : null });
  };

  const columns: TableColumnsType<MonthItem> = [
    { key: "billing_month", title: t("providerCosts.field.month"), dataIndex: "billing_month" },
    {
      key: "status",
      title: t("providerCosts.field.status"),
      render: (_: unknown, month) => <MonthStatusText month={month} />,
    },
    {
      key: "actual_source",
      title: t("providerCosts.field.actualSource"),
      render: (_: unknown, month) =>
        month.current === null ? (
          EMPTY
        ) : (
          <SourceAmount amount={month.current.source_total} currency={month.current.source_currency} />
        ),
    },
    {
      key: "actual_myr",
      title: t("providerCosts.field.actualMyr"),
      render: (_: unknown, month) => month.current?.reconciled_cost_myr ?? EMPTY,
    },
    {
      key: "estimated_myr",
      title: t("providerCosts.field.estimatedMyr"),
      render: (_: unknown, month) => month.current?.estimated_cost_myr ?? EMPTY,
    },
    {
      key: "source_variance_rate",
      title: t("providerCosts.field.sourceVarianceRate"),
      render: (_: unknown, month) => (month.current === null ? EMPTY : <SourceVarianceRate view={month.current} />),
    },
    {
      key: "myr_variance_rate",
      title: t("providerCosts.field.myrVarianceRate"),
      render: (_: unknown, month) => (month.current === null ? EMPTY : (month.current.myr_variance_rate ?? EMPTY)),
    },
    {
      key: "actions",
      title: t("providerCosts.field.actions"),
      render: (_: unknown, month) => (
        <Button
          size="small"
          onClick={(event) => {
            // 行本身点了会展开详情；按钮只开表单。
            event.stopPropagation();
            open(month);
          }}
        >
          {month.status === "RECONCILED" && month.current !== null
            ? t("providerCosts.correct")
            : t("providerCosts.reconcile")}
        </Button>
      ),
    },
  ];

  let body: ReactNode;
  if (months.isPending) {
    body = <Loading text={t("providerCosts.months.loading")} />;
  } else if (months.isError) {
    // 加载失败只显示这段文字，不显示空状态。
    body = <ErrorText error={months.error} title={t("providerCosts.months.loadFailed")} />;
  } else {
    body = (
      <Table<MonthItem>
        rowKey="billing_month"
        columns={columns}
        dataSource={months.data.items}
        loading={months.isFetching}
        locale={{ emptyText: t("providerCosts.months.empty") }}
        pagination={false}
        // 只有已对账的行能展开：展开时才去取详情。
        expandable={{
          expandRowByClick: true,
          rowExpandable: (month) => currentId(month) !== null,
          expandedRowRender: (month) => {
            const reconciliationId = currentId(month);
            return reconciliationId === null ? null : <ReconciliationDetailPanel reconciliationId={reconciliationId} />;
          },
        }}
      />
    );
  }

  return (
    <Space direction="vertical" size="large" style={{ width: "100%" }}>
      <Card title={t("providerCosts.title")}>
        <Typography.Paragraph type="secondary">{t("providerCosts.note.period")}</Typography.Paragraph>
        <Typography.Paragraph type="secondary">{t("providerCosts.note.scope")}</Typography.Paragraph>
        <Typography.Paragraph type="secondary">{t("providerCosts.note.fx")}</Typography.Paragraph>
        <Typography.Paragraph type="secondary">{t("providerCosts.note.noBilling")}</Typography.Paragraph>
        <Form layout="inline" component="div" style={{ marginBottom: 16 }}>
          <Form.Item label={t("providerCosts.provider")} htmlFor={PROVIDER_FIELD_ID}>
            <Select
              id={PROVIDER_FIELD_ID}
              value={providerCode}
              options={PROVIDER_OPTIONS}
              onChange={(code: string) => setProviderCode(code)}
              style={{ minWidth: 160 }}
            />
          </Form.Item>
        </Form>
        {stale ? (
          <Typography.Paragraph>
            <Typography.Text type="warning">{t("providerCosts.stale")}</Typography.Text>
          </Typography.Paragraph>
        ) : null}
        {/* 月份表格与它的状态单独成一块，用例与验收脚本按 aria-label 找它。 */}
        <div role="region" aria-label={t("providerCosts.months.title")}>
          {body}
        </div>
      </Card>
      {target === null ? null : (
        <ReconcileModal
          key={`${target.month.billing_month}-${target.current?.reconciliation_id ?? ""}`}
          providerCode={providerCode}
          target={target}
          onClose={() => setTarget(null)}
          onConfirmed={() => {
            setTarget(null);
            reload();
            void message.success(t("providerCosts.confirmed"));
          }}
          onStale={() => {
            setTarget(null);
            setStale(true);
            reload();
          }}
        />
      )}
    </Space>
  );
}

/**
 * 管理端用量分析（`GET /api/v1/admin/usage-analytics`，docs/api.md「管理端用量分析」）。
 *
 * 顶栏的「Usage analytics」进这一页。按一个维度（供应商、模型、客户、项目、功能、马来西亚日、马来西亚月）分组，看
 * 次数、token、成本与毛利。只统计 `PROCESSED` 事件；顺序由后端决定（日 / 月按键升序，其余事件数倒序、分组键升序），
 * 前端不重排、不汇总、不算任何数。四种状态都画出来：加载中、失败（带 request_id）、空列表、无权限（403 单独说）。
 *
 * 查询成功时表格上方是合计块（`UsageAnalyticsTotals`，设计闸门 #709 v2 §2「页面」），包括没有任何分组的时候。
 * 「Export CSV」与其右边的「Export Excel」（`format=xlsx`，设计闸门 #753 v1）用**已应用**的条件（与表格同一组）经
 * `GET /api/v1/admin/usage-analytics/export` 下载，不影响表格与合计的查询；两个按钮共用一个下载状态。失败在按钮下方
 * 显示错误码、信息与 request_id，下次点任一导出或已应用条件变化时收起。
 *
 * 默认最近 30 天、按供应商分组。时间段按吉隆坡时间输入，发请求前换成**带时区**的 RFC 3339、整秒
 * （`displayTimeToRfc3339`）；两端必填、开始早于结束、跨度不超过 366 天，提交前先挡住。条件与页码放在组件状态里，
 * **不进地址栏**（同用量事件页）。改任何条件都回到第 1 页。
 *
 * PREPAID 与内部计量分两组列：内部计量租户的参考售价不是收入，不进毛利，所以那一组没有毛利列。
 *
 * ⚠️ 金额、平均 token 数与毛利率**原样**显示后端的十进制字符串，不经 number，也不重新格式化。说明文字不用 antd
 * 的 `Alert`：它带 `role="alert"`，部署后浏览器验收把页面上任何 `role="alert"` 都当成错误提示。
 */

import { keepPreviousData, useMutation, useQuery } from "@tanstack/react-query";
import {
  Alert,
  Button,
  Card,
  Empty,
  Form,
  Input,
  Select,
  Space,
  Table,
  Typography,
  type FormRule,
  type TableColumnsType,
} from "antd";
import { useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";

import { VALIDATION_ERROR } from "../../api/adminAudit";
import { ADMIN_REQUIRED, DEFAULT_PAGE_SIZE } from "../../api/adminCustomers";
import {
  USAGE_ANALYTICS_GROUP_BYS,
  USAGE_ANALYTICS_MAX_SPAN_DAYS,
  exportUsageAnalytics,
  getUsageAnalytics,
  usageAnalyticsQueryKey,
  type UsageAnalyticsGroupBy,
  type UsageAnalyticsItem,
  type UsageAnalyticsQuery,
} from "../../api/adminUsageAnalytics";
import { ApiError } from "../../api/client";
import { EXPORT_BUSY, EXPORT_TOO_LARGE, type ExportFormat } from "../../api/download";
import { displayTimeToRfc3339, utcToDisplayInput } from "../../components/DateTimeText";
import { RequestReference } from "../../components/RequestReference";
import { CodeText, LoadingBlock, PAGE_SIZE_OPTIONS, usePaging } from "../catalog/ProvidersPage";
import { UsageAnalyticsTotals } from "./UsageAnalyticsTotals";
import { AdminOnlyNote, EMPTY } from "./UsageEventsPage";

const DEFAULT_GROUP_BY: UsageAnalyticsGroupBy = "provider";
const DEFAULT_SPAN_DAYS = 30;
const DAY_MS = 86_400_000;

/** 筛选表单里的原值。时间是 `<input type="datetime-local">` 的吉隆坡墙上时间。 */
export interface AnalyticsFormValues {
  group_by?: UsageAnalyticsGroupBy | undefined;
  occurred_from?: string | undefined;
  occurred_to?: string | undefined;
  customer_id?: string | undefined;
  project_id?: string | undefined;
  provider?: string | undefined;
  model?: string | undefined;
  feature?: string | undefined;
}

const TEXT_FILTERS = ["customer_id", "project_id", "provider", "model", "feature"] as const;

/** 某个时刻（舍到整秒）的不带时区 UTC 写法。 */
function naiveUtcAt(instantMs: number): string {
  return new Date(Math.floor(instantMs / 1000) * 1000).toISOString().slice(0, 19);
}

/**
 * 默认条件：截至此刻的最近 30 天、按供应商分组、没有可选筛选。同时给出表单里的吉隆坡墙上时间与发给后端的
 * RFC 3339，两者是同一对时刻。
 */
export function defaultConditions(nowMs: number = Date.now()): {
  values: AnalyticsFormValues;
  query: UsageAnalyticsQuery;
} {
  const from = naiveUtcAt(nowMs - DEFAULT_SPAN_DAYS * DAY_MS);
  const to = naiveUtcAt(nowMs);
  return {
    values: { group_by: DEFAULT_GROUP_BY, occurred_from: utcToDisplayInput(from), occurred_to: utcToDisplayInput(to) },
    query: { group_by: DEFAULT_GROUP_BY, occurred_from: `${from}Z`, occurred_to: `${to}Z` },
  };
}

function asRfc3339(value: unknown): string | null {
  return typeof value === "string" ? displayTimeToRfc3339(value.trim()) : null;
}

/**
 * 表单值 → 查询条件。可选筛选空的不带；时间换成带时区的 RFC 3339。必填项换不出来时返回 `null`
 * （表单校验已经挡住，这里不猜）。
 */
export function toQuery(values: AnalyticsFormValues): UsageAnalyticsQuery | null {
  const from = asRfc3339(values.occurred_from);
  const to = asRfc3339(values.occurred_to);
  if (values.group_by === undefined || from === null || to === null) {
    return null;
  }
  const query: UsageAnalyticsQuery = { group_by: values.group_by, occurred_from: from, occurred_to: to };
  for (const name of TEXT_FILTERS) {
    const value = (values[name] ?? "").trim();
    if (value !== "") {
      query[name] = value;
    }
  }
  return query;
}

/** 后端给的十进制字符串，原样显示；没有时「—」。 */
function DecimalString({ value }: { value: string | null }) {
  return value === null ? <>{EMPTY}</> : <span style={{ fontVariantNumeric: "tabular-nums" }}>{value}</span>;
}

/** 整数计数（不是金额），原样转成文字。 */
function count(value: number): string {
  return String(value);
}

function rowKey(item: UsageAnalyticsItem): string {
  // 未解析的值单列为 null 组，每页至多一个；加前缀免得与真实的键撞上。
  return item.group_key === null ? "null-group" : `group:${item.group_key}`;
}

export function UsageAnalyticsPage() {
  const { t } = useTranslation();
  const [form] = Form.useForm<AnalyticsFormValues>();
  // 「最近 30 天」在打开页面的那一刻定下来，之后不随时间滑动：表格不会自己变。
  const [initial] = useState(() => defaultConditions());
  const [query, setQuery] = useState<UsageAnalyticsQuery>(initial.query);
  const paging = usePaging(DEFAULT_PAGE_SIZE);

  const report = useQuery({
    queryKey: usageAnalyticsQueryKey(query, paging.page, paging.pageSize),
    queryFn: ({ signal }) => getUsageAnalytics(query, paging.page, paging.pageSize, signal),
    // 翻页、换筛选时先留着上一份，不让整张表闪成骨架屏。换了维度就不留：旧的分组键放在新维度的表头下面是错的。
    placeholderData: (previous, previousQuery) =>
      previousQuery?.queryKey[2].group_by === query.group_by ? keepPreviousData(previous) : undefined,
  });

  const groupByLabels: Record<UsageAnalyticsGroupBy, string> = {
    provider: t("usageAnalytics.groupBy.provider"),
    model: t("usageAnalytics.groupBy.model"),
    customer: t("usageAnalytics.groupBy.customer"),
    project: t("usageAnalytics.groupBy.project"),
    feature: t("usageAnalytics.groupBy.feature"),
    day: t("usageAnalytics.groupBy.day"),
    month: t("usageAnalytics.groupBy.month"),
  };
  const groupByOptions = USAGE_ANALYTICS_GROUP_BYS.map((value) => ({ value, label: groupByLabels[value] }));

  // 导出与表格、合计的查询互不相干：它不改 query，也不让那两个重新请求。CSV 与 Excel 共用这一个下载状态。
  const exporting = useMutation({
    // CSV 不带 format，交给 api 的条件与加 Excel 之前逐字相同。
    mutationFn: ({ applied, format }: { applied: UsageAnalyticsQuery; format: ExportFormat }) =>
      exportUsageAnalytics(format === "csv" ? applied : { ...applied, format }),
  });

  const startExport = (format: ExportFormat): void => {
    // 按钮在导出中已禁用；这里再挡一次，连点也只发一个请求。用的是已应用的条件，不是表单里还没提交的草稿。
    if (exporting.isPending) {
      return;
    }
    exporting.reset();
    exporting.mutate({ applied: query, format });
  };

  // 加载状态只画在被点的那个按钮上；两个按钮在下载中都不可点。
  const exportingFormat = exporting.isPending ? exporting.variables.format : null;

  // 上一次导出的失败说的是旧的条件，换条件时收起。只收失败：导出中 reset 会让按钮提前恢复可点。
  const clearExportFailure = (): void => {
    if (exporting.isError) {
      exporting.reset();
    }
  };

  const apply = (values: AnalyticsFormValues): void => {
    const next = toQuery(values);
    if (next !== null) {
      setQuery(next);
      paging.reset();
      clearExportFailure();
    }
  };

  // 回到默认条件：最近 30 天重新从此刻算起。
  const reset = (): void => {
    const defaults = defaultConditions();
    form.resetFields();
    form.setFieldsValue(defaults.values);
    setQuery(defaults.query);
    paging.reset();
    clearExportFailure();
  };

  const requiredRule: FormRule = { required: true, message: t("usageAnalytics.filter.required") };

  const timeRule: FormRule = {
    validator: (_rule, value: unknown) =>
      typeof value !== "string" || value.trim() === "" || asRfc3339(value) !== null
        ? Promise.resolve()
        : Promise.reject(new Error(t("usage.filter.timeInvalid"))),
  };

  // 后端对 occurred_from >= occurred_to 与超过 366 天的跨度回 422；提前说清楚是哪一格的问题。两边都是同一种
  // 写法，先后可以按字符串比；跨度是时刻之差（毫秒），不是金额。
  const rangeRule: FormRule = ({ getFieldValue }) => ({
    validator: (_rule, value: unknown) => {
      const from = asRfc3339(getFieldValue("occurred_from"));
      const to = asRfc3339(value);
      if (from === null || to === null) {
        return Promise.resolve();
      }
      if (from >= to) {
        return Promise.reject(new Error(t("usage.filter.rangeInvalid")));
      }
      if (Date.parse(to) - Date.parse(from) > USAGE_ANALYTICS_MAX_SPAN_DAYS * DAY_MS) {
        return Promise.reject(new Error(t("usageAnalytics.filter.rangeTooLong", { days: USAGE_ANALYTICS_MAX_SPAN_DAYS })));
      }
      return Promise.resolve();
    },
  });

  // 表头跟着已经提交的维度走，不跟着表单里还没提交的那个。
  const groupBy = query.group_by;

  const columns: TableColumnsType<UsageAnalyticsItem> = [
    {
      key: "group",
      title: groupByLabels[groupBy],
      render: (_: unknown, item) => <GroupCell item={item} groupBy={groupBy} />,
    },
    { key: "event_count", title: t("usageAnalytics.field.events"), render: (_: unknown, item) => count(item.event_count) },
    {
      key: "input_tokens",
      title: t("usageAnalytics.field.inputTokens"),
      render: (_: unknown, item) => count(item.input_tokens),
    },
    {
      key: "output_tokens",
      title: t("usageAnalytics.field.outputTokens"),
      render: (_: unknown, item) => count(item.output_tokens),
    },
    {
      key: "cache_creation_input_tokens",
      title: t("usageAnalytics.field.cacheWriteTokens"),
      render: (_: unknown, item) => count(item.cache_creation_input_tokens),
    },
    {
      key: "cache_read_input_tokens",
      title: t("usageAnalytics.field.cacheReadTokens"),
      render: (_: unknown, item) => count(item.cache_read_input_tokens),
    },
    {
      key: "average_input_tokens",
      title: t("usageAnalytics.field.averageInputTokens"),
      render: (_: unknown, item) => <DecimalString value={item.average_input_tokens} />,
    },
    {
      key: "average_output_tokens",
      title: t("usageAnalytics.field.averageOutputTokens"),
      render: (_: unknown, item) => <DecimalString value={item.average_output_tokens} />,
    },
    {
      key: "prepaid",
      title: t("usageAnalytics.prepaid.title"),
      children: [
        {
          key: "prepaid_event_count",
          title: t("usageAnalytics.field.events"),
          render: (_: unknown, item) => count(item.prepaid.event_count),
        },
        {
          key: "prepaid_billable_cost",
          title: t("usageAnalytics.field.billableCost"),
          render: (_: unknown, item) => <DecimalString value={item.prepaid.billable_cost} />,
        },
        {
          key: "prepaid_estimated_cost",
          title: t("usageAnalytics.field.estimatedCost"),
          render: (_: unknown, item) => <DecimalString value={item.prepaid.estimated_provider_cost_myr} />,
        },
        {
          key: "prepaid_gross_margin",
          title: t("usageAnalytics.field.grossMargin"),
          render: (_: unknown, item) => <DecimalString value={item.prepaid.gross_margin} />,
        },
        {
          key: "prepaid_gross_margin_rate",
          title: t("usageAnalytics.field.grossMarginRate"),
          render: (_: unknown, item) => <DecimalString value={item.prepaid.gross_margin_rate} />,
        },
      ],
    },
    {
      key: "internal",
      title: t("usageAnalytics.internal.title"),
      children: [
        {
          key: "internal_event_count",
          title: t("usageAnalytics.field.events"),
          render: (_: unknown, item) => count(item.internal_metered_only.event_count),
        },
        {
          key: "internal_estimated_cost",
          title: t("usageAnalytics.field.estimatedCost"),
          render: (_: unknown, item) => <DecimalString value={item.internal_metered_only.estimated_provider_cost_myr} />,
        },
        {
          key: "internal_reference_price",
          title: t("usageAnalytics.field.referencePrice"),
          render: (_: unknown, item) => <DecimalString value={item.internal_metered_only.reference_customer_price} />,
        },
      ],
    },
  ];

  let result: ReactNode;
  if (report.isPending) {
    result = <LoadingBlock text={t("usageAnalytics.loading")} />;
  } else if (report.isError) {
    result = <AnalyticsErrorAlert error={report.error} onRetry={() => void report.refetch()} />;
  } else {
    let groups: ReactNode;
    if (report.data.total === 0) {
      groups = <Empty description={t("usageAnalytics.empty")} />;
    } else {
      groups = (
        <Table<UsageAnalyticsItem>
        rowKey={rowKey}
        columns={columns}
        dataSource={report.data.items}
        loading={report.isFetching}
        bordered
        size="small"
        scroll={{ x: true }}
        // 页码超过末页时后端回空 items、total 照报：说清楚是「这一页没有」。
        locale={{ emptyText: t("usageAnalytics.emptyPage") }}
        pagination={{
          current: paging.page,
          pageSize: paging.pageSize,
          total: report.data.total,
          showSizeChanger: true,
          pageSizeOptions: PAGE_SIZE_OPTIONS,
          showTotal: (total) => t("usageAnalytics.total", { total }),
          onChange: paging.goTo,
        }}
      />
      );
    }
    // 合计块只要查询成功就画，包括没有任何分组的时候（那时是后端给的零值）；表格自己的空状态照旧。
    result = (
      <>
        <UsageAnalyticsTotals totals={report.data.totals} />
        {groups}
      </>
    );
  }

  return (
    <Card title={t("usageAnalytics.title")}>
      <AdminOnlyNote />
      <Typography.Paragraph type="secondary">{t("usageAnalytics.estimateNote")}</Typography.Paragraph>
      <Typography.Paragraph type="secondary">{t("usageAnalytics.scopeNote")}</Typography.Paragraph>
      <Form<AnalyticsFormValues>
        form={form}
        name="usageAnalyticsFilters"
        layout="vertical"
        initialValues={initial.values}
        onFinish={apply}
        style={{ display: "flex", flexWrap: "wrap", columnGap: 16, alignItems: "flex-end" }}
      >
        <Form.Item name="group_by" label={t("usageAnalytics.filter.groupBy")} rules={[requiredRule]} style={{ minWidth: 180 }}>
          {/* ⚠️ `virtual={false}`：只有七项，全部渲染出来，测试才找得到每一项。 */}
          <Select virtual={false} options={groupByOptions} />
        </Form.Item>
        <Form.Item name="occurred_from" label={t("usage.filter.occurredFrom")} rules={[requiredRule, timeRule]}>
          <Input type="datetime-local" step={1} />
        </Form.Item>
        <Form.Item
          name="occurred_to"
          label={t("usage.filter.occurredTo")}
          dependencies={["occurred_from"]}
          rules={[requiredRule, timeRule, rangeRule]}
        >
          <Input type="datetime-local" step={1} />
        </Form.Item>
        <Form.Item name="customer_id" label={t("usage.filter.customerId")}>
          <Input autoComplete="off" spellCheck={false} />
        </Form.Item>
        <Form.Item name="project_id" label={t("usage.filter.projectId")}>
          <Input autoComplete="off" spellCheck={false} />
        </Form.Item>
        {/* 与用量事件列表同一种语义（docs/api.md），所以同一个说法。 */}
        <Form.Item name="provider" label={t("usage.filter.provider")}>
          <Input autoComplete="off" spellCheck={false} />
        </Form.Item>
        <Form.Item name="model" label={t("usage.filter.model")}>
          <Input autoComplete="off" spellCheck={false} />
        </Form.Item>
        <Form.Item name="feature" label={t("usage.filter.feature")}>
          <Input autoComplete="off" spellCheck={false} />
        </Form.Item>
        <Form.Item>
          <Space>
            <Button type="primary" htmlType="submit">
              {t("usage.filter.apply")}
            </Button>
            <Button onClick={reset}>{t("usageAnalytics.filter.reset")}</Button>
          </Space>
        </Form.Item>
      </Form>
      <div style={{ marginBottom: 16 }}>
        <Space>
          <Button
            onClick={() => startExport("csv")}
            loading={exportingFormat === "csv"}
            disabled={exporting.isPending}
          >
            {t("usageAnalytics.export.button")}
          </Button>
          <Button
            onClick={() => startExport("xlsx")}
            loading={exportingFormat === "xlsx"}
            disabled={exporting.isPending}
          >
            {t("usageAnalytics.export.buttonXlsx")}
          </Button>
        </Space>
        {exporting.error ? <ExportFailure error={exporting.error} /> : null}
      </div>
      {result}
    </Card>
  );
}

/**
 * 分组那一格：客户与项目显示名称和 public id，日 / 月只显示日期键（`YYYY-MM-DD` / `YYYY-MM`，没有名称行），其余显示
 * 键；未解析的 null 组单独说明。
 */
function GroupCell({ item, groupBy }: { item: UsageAnalyticsItem; groupBy: UsageAnalyticsGroupBy }) {
  const { t } = useTranslation();
  if (item.group_key === null) {
    return <Typography.Text type="secondary">{t("usageAnalytics.nullGroup")}</Typography.Text>;
  }
  if (groupBy === "day" || groupBy === "month") {
    return <span style={{ fontVariantNumeric: "tabular-nums" }}>{item.group_key}</span>;
  }
  if ((groupBy === "customer" || groupBy === "project") && item.group_name !== null) {
    return (
      <Space direction="vertical" size={0}>
        <span>{item.group_name}</span>
        <Typography.Text type="secondary" copyable={{ text: item.group_key }}>
          {item.group_key}
        </Typography.Text>
      </Space>
    );
  }
  return <CodeText code={item.group_key} />;
}

/**
 * 导出失败，显示在按钮下方：分组太多与已有导出在进行各有专门文案，下面都带错误码、后端 message 与 request_id。
 */
function ExportFailure({ error }: { error: Error }) {
  const { t } = useTranslation();
  const code = error instanceof ApiError ? error.code : null;
  let message = t("usageAnalytics.export.failed");
  if (code === EXPORT_TOO_LARGE) {
    message = t("usageAnalytics.export.tooLarge");
  } else if (code === EXPORT_BUSY) {
    message = t("usageAnalytics.export.busy");
  }
  return (
    <Alert
      type="error"
      showIcon
      style={{ marginTop: 8 }}
      message={message}
      description={
        <Space direction="vertical" size={0}>
          {code === null ? null : <Typography.Text code>{code}</Typography.Text>}
          <RequestReference error={error} />
        </Space>
      }
    />
  );
}

/**
 * 分析接口出错时的提示。403 `ADMIN_REQUIRED` 单独说「没有权限」—— 前端不做角色判断，是不是管理员只看后端这一句。
 * 422 说「条件不被接受」。两种情况下面都带后端 message 与 request_id。
 */
function AnalyticsErrorAlert({ error, onRetry }: { error: Error; onRetry: () => void }) {
  const { t } = useTranslation();
  const code = error instanceof ApiError ? error.code : null;
  let message = t("usageAnalytics.loadFailed");
  // 无权限与条件被拒，重试也还是一样。
  let noRetry = false;
  if (code === ADMIN_REQUIRED) {
    message = t("usageAnalytics.forbidden");
    noRetry = true;
  } else if (code === VALIDATION_ERROR) {
    message = t("usageAnalytics.invalidFilters");
    noRetry = true;
  }
  return (
    <Alert
      type={code === ADMIN_REQUIRED ? "warning" : "error"}
      showIcon
      style={{ marginBottom: 16 }}
      message={message}
      description={<RequestReference error={error} />}
      action={
        noRetry ? undefined : (
          <Button size="small" onClick={onRetry}>
            {t("usage.retry")}
          </Button>
        )
      }
    />
  );
}

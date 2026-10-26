/**
 * 客户门户用量页（`/portal/usage`，AIH-TASK-105；设计闸门 #398 v4 §2「前端」「金额」「时间」、§8 第 3 步）。
 *
 * 语义以 docs/api.md「客户用量」为准：项目下拉来自 `GET /api/v1/customer/projects`，表格来自
 * `GET /api/v1/customer/usage-events`。只读：页面上没有任何写操作。
 *
 * 日期按**马来西亚时间**（固定 UTC+8，没有夏令时）的日历日理解：默认是马来西亚时间本月 1 日到今天，日期选择限制在
 * 2020-01-01 到 2099-12-31。区间不完整、倒置、越界或超过 92 天时**不发请求**，在表格的位置说明原因 —— 后端反正会
 * 422，提前说清楚是哪里的问题。筛选条件点「Apply filters」才生效（只放在组件状态里，不进地址栏）。
 *
 * 查询串带 `event_id` 时（钱包流水跳过来的点查）只按它查：日期与其余筛选都不生效、表单禁用，并有提示与清除入口。
 *
 * 分页是不透明游标，没有页码与总数：「Older」把响应的 `next_cursor` 原样作为下一次的 `cursor`，它为 `null`（没有
 * 更早的行）时按钮不出现；「Newest」回到不带游标的最新一页。换了筛选条件游标自动作废。
 *
 * 三种状态都画出来（spec §132 DoD 第 8 条）：加载中、失败（带 request_id）、空列表。`QUERY_TOO_BROAD`（查询超过
 * 后端的执行时间预算）显示专门文案，请客户缩小日期区间或换筛选条件。点一行展开请求详情（`UsageDetail`）。
 *
 * 「Export CSV」（AIH-TASK-117；设计闸门 #435 v10 §2「前端」，语义以 docs/api.md「客户导出」为准）用页面**当前已应用**
 * 的筛选（不是表单里还没点「Apply filters」的值）调 `GET /api/v1/customer/usage-events/export`，交给浏览器下载。导出
 * 的日期恒必填：日期区间无效时、以及按 `event_id` 点查时（那时没有生效的日期区间）按钮不可用。导出中按钮转为加载
 * 状态、不可重复点。`EXPORT_TOO_LARGE`、`QUERY_TOO_BROAD`、`EXPORT_BUSY` 各有专门文案，其余失败带 request_id。
 * 导出不影响列表、筛选与翻页。
 *
 * ⚠️ 只请求 `/api/v1/customer/*`：不请求任何 `/api/v1/admin/` 路径，不显示成本、毛利或内部字段（INV-7）。
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
  Skeleton,
  Space,
  Table,
  Typography,
  type TableColumnsType,
} from "antd";
import { useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { useSearchParams } from "react-router";

import { ApiError } from "../../api/client";
import {
  CUSTOMER_PROJECTS_QUERY_KEY,
  QUERY_TOO_BROAD,
  customerUsageListQueryKey,
  exportCustomerUsageEvents,
  listCustomerProjects,
  listCustomerUsageEvents,
  type CustomerUsageEvent,
  type CustomerUsageExportQuery,
  type CustomerUsageQuery,
} from "../../api/customerUsage";
import { EXPORT_BUSY, EXPORT_TOO_LARGE } from "../../api/download";
import { RequestReference } from "../../components/RequestReference";
import { ChargedAmount, EMPTY, UsageDetail, UsageTime, UsageTypeText, tokenCount } from "./UsageDetail";

/** 每页行数：与接口的默认值相同（docs/api.md：1–100，默认 20）。 */
export const USAGE_PAGE_SIZE = 20;

/** 查询串里点查用的参数名（AIH-TASK-106 的钱包流水链接用同一个名字）。 */
export const EVENT_ID_PARAM = "event_id";

/** 接口接受的日期范围（含两端）。 */
export const MIN_DATE = "2020-01-01";
export const MAX_DATE = "2099-12-31";

/** 区间最多 92 天（`to_date − from_date ≤ 91`）。 */
export const MAX_RANGE_DAYS = 92;

/** 马来西亚时间：固定 UTC+8（与后端 `KUALA_LUMPUR_OFFSET` 相同）。 */
const MYT_OFFSET_MS = 8 * 60 * 60 * 1000;
const DAY_MS = 24 * 60 * 60 * 1000;

const DATE_SHAPE = /^(\d{4})-(\d{2})-(\d{2})$/;

/** `YYYY-MM-DD` → 自 1970-01-01 起的天数；形状不对或日历上不存在（`2026-02-30`）时 `null`。 */
function dayNumber(value: string): number | null {
  const match = DATE_SHAPE.exec(value);
  if (match === null) {
    return null;
  }
  const [year, month, day] = match.slice(1).map(Number);
  if (year === undefined || month === undefined || day === undefined) {
    return null;
  }
  const ms = Date.UTC(year, month - 1, day);
  const check = new Date(ms);
  if (check.getUTCFullYear() !== year || check.getUTCMonth() !== month - 1 || check.getUTCDate() !== day) {
    return null;
  }
  return ms / DAY_MS;
}

/**
 * 默认日期区间：马来西亚时间的本月 1 日到今天。UTC 16:00 起马来西亚已经是第二天，月末那一刻起就是下个月。
 * 两个都是 `YYYY-MM-DD`。
 */
export function defaultDateRange(now: Date): { fromDate: string; toDate: string } {
  const today = new Date(now.getTime() + MYT_OFFSET_MS).toISOString().slice(0, 10);
  return { fromDate: `${today.slice(0, 8)}01`, toDate: today };
}

export type DateRangeProblem = "missing" | "outOfRange" | "reversed" | "tooLong";

/** 日期区间能不能发给后端；能的话 `null`。规则同 docs/api.md「客户用量」的 `from_date` / `to_date`。 */
export function dateRangeProblem(fromDate: string, toDate: string): DateRangeProblem | null {
  if (fromDate === "" || toDate === "") {
    return "missing";
  }
  const from = dayNumber(fromDate);
  const to = dayNumber(toDate);
  // 形状一致（YYYY-MM-DD）时可以按字符串比较先后与边界。
  if (from === null || to === null || fromDate < MIN_DATE || toDate > MAX_DATE || fromDate > MAX_DATE || toDate < MIN_DATE) {
    return "outOfRange";
  }
  if (to < from) {
    return "reversed";
  }
  if (to - from > MAX_RANGE_DAYS - 1) {
    return "tooLong";
  }
  return null;
}

/** 筛选表单的值。日期是 `<input type="date">` 的 `YYYY-MM-DD`。 */
interface UsageFilterValues {
  fromDate: string;
  toDate: string;
  projectId?: string | undefined;
  provider?: string | undefined;
  model?: string | undefined;
  conversationId?: string | undefined;
}

/** 去首尾空白；空的当成没填。 */
function trimmed(value: string | undefined): string | undefined {
  const text = (value ?? "").trim();
  return text === "" ? undefined : text;
}

function RangeProblemText({ problem }: { problem: DateRangeProblem }) {
  const { t } = useTranslation();
  switch (problem) {
    case "missing":
      return t("portal.usage.range.missing");
    case "outOfRange":
      return t("portal.usage.range.outOfRange", { min: MIN_DATE, max: MAX_DATE });
    case "reversed":
      return t("portal.usage.range.reversed");
    case "tooLong":
      return t("portal.usage.range.tooLong", { days: MAX_RANGE_DAYS });
  }
}

/** 导出失败的说明：三种可以由客户自己处理的错误各有专门文案，其余是通用文案（request_id 另外显示）。 */
function ExportFailureText({ error }: { error: Error }) {
  const { t } = useTranslation();
  const code = error instanceof ApiError ? error.code : null;
  switch (code) {
    case EXPORT_TOO_LARGE:
      return t("portal.usage.export.tooLarge");
    case QUERY_TOO_BROAD:
      return t("portal.usage.export.tooBroad");
    case EXPORT_BUSY:
      return t("portal.usage.export.busy");
    default:
      return t("portal.usage.export.failed");
  }
}

const SPECIFIC_EXPORT_FAILURES: ReadonlySet<string> = new Set([EXPORT_TOO_LARGE, QUERY_TOO_BROAD, EXPORT_BUSY]);

/** 三种专门文案之外的失败：要显示 request_id。 */
function isGenericExportFailure(error: Error): boolean {
  return !(error instanceof ApiError) || !SPECIFIC_EXPORT_FAILURES.has(error.code);
}

export function UsagePage() {
  const { t } = useTranslation();
  const [form] = Form.useForm<UsageFilterValues>();
  const [searchParams, setSearchParams] = useSearchParams();
  // 进页面那一刻的「今天」：停在页面上跨过午夜不会自己换区间，点「Reset」会按新的今天重算。
  const [initialRange] = useState(() => defaultDateRange(new Date()));
  const [filters, setFilters] = useState<UsageFilterValues>(initialRange);
  // 游标连同它所属的条件一起记：条件一变，旧条件下的游标自动作废，回到最新一页。
  const [paging, setPaging] = useState<{ scope: string; cursor: string } | null>(null);

  const eventId = (searchParams.get(EVENT_ID_PARAM) ?? "").trim();
  const byEvent = eventId !== "";
  const problem = byEvent ? null : dateRangeProblem(filters.fromDate, filters.toDate);

  let base: CustomerUsageQuery | null = null;
  if (byEvent) {
    base = { eventId, pageSize: USAGE_PAGE_SIZE };
  } else if (problem === null) {
    base = {
      fromDate: filters.fromDate,
      toDate: filters.toDate,
      projectId: trimmed(filters.projectId) ?? null,
      provider: trimmed(filters.provider) ?? null,
      model: trimmed(filters.model) ?? null,
      conversationId: trimmed(filters.conversationId) ?? null,
      pageSize: USAGE_PAGE_SIZE,
    };
  }
  const scope = JSON.stringify(base);
  const cursor = paging !== null && paging.scope === scope ? paging.cursor : null;

  // 导出用已应用的筛选（与列表同一份），不带 pageSize 与 cursor。点查时没有生效的日期区间，导出不可用。
  let exportQuery: CustomerUsageExportQuery | null = null;
  if (!byEvent && problem === null) {
    exportQuery = {
      fromDate: filters.fromDate,
      toDate: filters.toDate,
      projectId: trimmed(filters.projectId) ?? null,
      provider: trimmed(filters.provider) ?? null,
      model: trimmed(filters.model) ?? null,
      conversationId: trimmed(filters.conversationId) ?? null,
    };
  }

  const projects = useQuery({
    queryKey: CUSTOMER_PROJECTS_QUERY_KEY,
    queryFn: ({ signal }) => listCustomerProjects(signal),
    // 理由同钱包页：同一个标签页换账号登录，拿到的必须是新客户的项目。
    gcTime: 0,
  });

  const usage = useQuery({
    queryKey: customerUsageListQueryKey({ ...base, cursor }),
    queryFn: ({ signal }) => listCustomerUsageEvents({ ...base, cursor }, signal),
    // 区间不合规时不发请求（后端反正会 422），页面上说明原因。
    enabled: base !== null,
    // 翻页、换条件时先留着上一份，不让整张表闪成骨架屏。
    placeholderData: keepPreviousData,
    gcTime: 0,
  });

  const exporting = useMutation({
    mutationFn: (query: CustomerUsageExportQuery) => exportCustomerUsageEvents(query),
  });

  const startExport = (): void => {
    // 按钮在导出中已禁用；这里再挡一次，连点也只发一个请求。
    if (exportQuery === null || exporting.isPending) {
      return;
    }
    exporting.mutate(exportQuery);
  };

  // 上一次导出的失败说的是旧的筛选条件，换条件时收起。只收失败：导出中 reset 会让按钮提前恢复可点。
  const clearExportFailure = (): void => {
    if (exporting.isError) {
      exporting.reset();
    }
  };

  const apply = (values: UsageFilterValues): void => {
    setFilters({ ...values, fromDate: values.fromDate.trim(), toDate: values.toDate.trim() });
    clearExportFailure();
  };

  const reset = (): void => {
    const range = defaultDateRange(new Date());
    form.resetFields();
    form.setFieldsValue(range);
    setFilters(range);
    clearExportFailure();
  };

  const showAll = (): void => {
    const next = new URLSearchParams(searchParams);
    next.delete(EVENT_ID_PARAM);
    setSearchParams(next);
  };

  const next = usage.data?.next_cursor ?? null;
  // 占位数据是上一份的：拿它的游标翻页会翻错条件或重复请求同一页，等新的一页回来再让按钮可点。
  const settled = !usage.isPlaceholderData;

  const columns: TableColumnsType<CustomerUsageEvent> = [
    {
      key: "occurred_at",
      title: t("portal.usage.field.occurredAt"),
      render: (_: unknown, row) => <UsageTime value={row.occurred_at} />,
    },
    { key: "project", title: t("portal.usage.field.project"), dataIndex: "project_name" },
    {
      key: "provider_model",
      title: t("portal.usage.field.providerModel"),
      render: (_: unknown, row) => `${row.provider} / ${row.model}`,
    },
    {
      key: "usage_type",
      title: t("portal.usage.field.usageType"),
      render: (_: unknown, row) => <UsageTypeText type={row.usage_type} />,
    },
    {
      key: "tokens",
      title: t("portal.usage.field.tokens"),
      align: "right",
      render: (_: unknown, row) =>
        row.input_tokens === null && row.output_tokens === null
          ? EMPTY
          : `${tokenCount(row.input_tokens)} / ${tokenCount(row.output_tokens)}`,
    },
    {
      key: "charged_amount",
      title: t("portal.usage.field.chargedAmount"),
      align: "right",
      render: (_: unknown, row) => <ChargedAmount value={row.charged_amount} />,
    },
  ];

  let body: ReactNode;
  if (problem !== null) {
    body = <Alert type="warning" showIcon message={<RangeProblemText problem={problem} />} />;
  } else if (usage.isPending) {
    body = (
      <Space direction="vertical" aria-busy="true" style={{ width: "100%" }}>
        <Skeleton active title={false} paragraph={{ rows: 3 }} />
        <Typography.Text type="secondary">{t("portal.usage.loading")}</Typography.Text>
      </Space>
    );
  } else if (usage.isError) {
    const tooBroad = usage.error instanceof ApiError && usage.error.code === QUERY_TOO_BROAD;
    body = (
      <Alert
        type="error"
        showIcon
        message={tooBroad ? t("portal.usage.queryTooBroad") : t("portal.usage.loadFailed")}
        description={<RequestReference error={usage.error} />}
        action={
          <Button size="small" onClick={() => void usage.refetch()}>
            {t("portal.usage.retry")}
          </Button>
        }
      />
    );
  } else if (usage.data.items.length === 0) {
    let empty: string;
    if (cursor !== null) {
      empty = t("portal.usage.emptyPage");
    } else if (byEvent) {
      empty = t("portal.usage.emptyEvent");
    } else {
      empty = t("portal.usage.empty");
    }
    body = <Empty description={empty} />;
  } else {
    body = (
      <Table<CustomerUsageEvent>
        rowKey="id"
        columns={columns}
        dataSource={usage.data.items}
        loading={usage.isFetching}
        pagination={false}
        scroll={{ x: "max-content" }}
        expandable={{
          expandRowByClick: true,
          expandedRowRender: (row) => <UsageDetail event={row} />,
        }}
        onRow={() => ({ style: { cursor: "pointer" } })}
      />
    );
  }

  const toolbar = (
    <Space wrap style={{ marginBottom: 16 }}>
      {problem === null ? (
        <Button onClick={() => setPaging(null)} disabled={cursor === null}>
          {t("portal.usage.newest")}
        </Button>
      ) : null}
      {problem === null && next !== null && !usage.isError ? (
        <Button onClick={() => setPaging({ scope, cursor: next })} disabled={!settled}>
          {t("portal.usage.older")}
        </Button>
      ) : null}
      <Button
        onClick={startExport}
        loading={exporting.isPending}
        disabled={exportQuery === null || exporting.isPending}
      >
        {t("portal.usage.export.button")}
      </Button>
    </Space>
  );

  const exportFailure = exporting.error ? (
    <Alert
      type="error"
      showIcon
      closable
      onClose={() => exporting.reset()}
      style={{ marginBottom: 16 }}
      message={<ExportFailureText error={exporting.error} />}
      description={isGenericExportFailure(exporting.error) ? <RequestReference error={exporting.error} /> : null}
    />
  ) : null;

  return (
    // role + aria-label：部署后验收脚本按语义结构找到这一块（不依赖 antd 的类名）。
    <Card role="region" aria-label={t("portal.usage.title")} title={t("portal.usage.title")}>
      {byEvent ? (
        <Alert
          type="info"
          showIcon
          style={{ marginBottom: 16 }}
          message={t("portal.usage.byEvent.title")}
          description={
            <Typography.Text>
              {t("portal.usage.byEvent.body")} <Typography.Text code>{eventId}</Typography.Text>
            </Typography.Text>
          }
          action={
            <Button size="small" onClick={showAll}>
              {t("portal.usage.byEvent.clear")}
            </Button>
          }
        />
      ) : null}
      <Form<UsageFilterValues>
        form={form}
        name="customerUsageFilters"
        layout="vertical"
        initialValues={initialRange}
        onFinish={apply}
        disabled={byEvent}
        style={{ display: "flex", flexWrap: "wrap", columnGap: 16, alignItems: "flex-end" }}
      >
        <Form.Item name="fromDate" label={t("portal.usage.filter.fromDate")}>
          <Input type="date" min={MIN_DATE} max={MAX_DATE} />
        </Form.Item>
        <Form.Item name="toDate" label={t("portal.usage.filter.toDate")}>
          <Input type="date" min={MIN_DATE} max={MAX_DATE} />
        </Form.Item>
        <Form.Item name="projectId" label={t("portal.usage.filter.project")} style={{ minWidth: 220 }}>
          {/* ⚠️ `virtual={false}`：项目很少，全部渲染出来，测试才找得到每一项。 */}
          <Select
            allowClear
            virtual={false}
            loading={projects.isPending}
            options={(projects.data?.items ?? []).map((project) => ({ value: project.id, label: project.name }))}
            placeholder={projects.isError ? t("portal.usage.filter.projectsFailed") : t("portal.usage.filter.anyProject")}
            status={projects.isError ? "error" : ""}
          />
        </Form.Item>
        <Form.Item name="provider" label={t("portal.usage.filter.provider")}>
          <Input autoComplete="off" spellCheck={false} />
        </Form.Item>
        <Form.Item name="model" label={t("portal.usage.filter.model")}>
          <Input autoComplete="off" spellCheck={false} />
        </Form.Item>
        <Form.Item name="conversationId" label={t("portal.usage.filter.conversationId")}>
          <Input autoComplete="off" spellCheck={false} />
        </Form.Item>
        <Form.Item>
          <Space>
            <Button type="primary" htmlType="submit">
              {t("portal.usage.filter.apply")}
            </Button>
            <Button onClick={reset}>{t("portal.usage.filter.reset")}</Button>
          </Space>
        </Form.Item>
      </Form>
      {toolbar}
      {exportFailure}
      {body}
    </Card>
  );
}

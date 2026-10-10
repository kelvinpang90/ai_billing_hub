/**
 * 管理端用量事件列表（`GET /api/v1/admin/usage-events`，docs/api.md「管理端用量事件：查询」）与重新入队
 * （「管理端用量事件：重新入队」）。
 *
 * 顶栏的「Usage events」进这一页。顺序由后端决定（按事件内部 id 倒序 ≈ 最新接收的在前），前端不重排。
 * 三种状态都画出来（spec §132 DoD 第 8 条）：加载中、失败（带 request_id）、空列表。403 说「没有权限」，
 * 422 说「筛选条件不被接受」并显示后端的 message（它只列字段名）。
 *
 * 筛选条件与页码放在组件状态里，**不进地址栏**（同审计页）：条件里有客户、会话与请求 id，进了地址栏就会
 * 留在浏览器历史与 nginx 的访问日志里。发生时间段按吉隆坡时间输入，发请求前换成**带时区**的 RFC 3339、整秒
 * （`displayTimeToRfc3339`）。改任何筛选条件都回到第 1 页。
 *
 * 状态用颜色区分：四个可重新入队的错误状态是红色。重新入队有两条路，都先填原因（写进审计）再二次确认：
 *
 * - 勾选：只有错误状态的行能勾。逐个调单个接口；已被别人放回或已处理的（409 / 404）计为跳过，与批量接口
 *   的 `skipped` 同义。
 * - 按当前筛选条件批量：只在筛选里选了一个错误状态、且没有按项目 / 会话 / 请求筛时可用 —— 批量接口不收这三个
 *   条件，带着它们提交会放回比列表多的事件。确认框里写明至多影响 min(总数, 1000) 条。
 *
 * 本文件后半是列表与详情两页共用的部件（状态标签、数量、错误提示、重新入队对话框），照价格页的先例在这里导出。
 *
 * ⚠️ 金额与数量按十进制字符串显示（MoneyText / DecimalText），不经 number。说明文字不用 antd 的 `Alert`：
 * 它带 `role="alert"`，部署后浏览器验收把页面上任何 `role="alert"` 都当成错误提示。
 */

import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Alert,
  Button,
  Card,
  Empty,
  Form,
  Input,
  Modal,
  Select,
  Space,
  Table,
  Tag,
  Typography,
  type DescriptionsProps,
  type FormRule,
  type TableColumnsType,
} from "antd";
import { useRef, useState, type Key, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router";

import { VALIDATION_ERROR } from "../../api/adminAudit";
import { ADMIN_REQUIRED, CUSTOMER_NOT_FOUND, DEFAULT_PAGE_SIZE } from "../../api/adminCustomers";
import {
  BULK_REQUEUE_LIMIT,
  USAGE_EVENTS_QUERY_KEY,
  USAGE_EVENT_NOT_FOUND,
  USAGE_EVENT_NOT_REQUEUABLE,
  USAGE_EVENT_STATUSES,
  bulkRequeueUsageEvents,
  isRequeuableStatus,
  listUsageEvents,
  requeueUsageEvent,
  usageEventListQueryKey,
  type BulkRequeueBody,
  type BulkRequeueResult,
  type UsageEventFilters,
  type UsageEventSummary,
} from "../../api/adminUsageEvents";
import { ApiError } from "../../api/client";
import { DateTimeText, displayTimeToRfc3339 } from "../../components/DateTimeText";
import { DecimalText } from "../../components/DecimalText";
import { MoneyText } from "../../components/MoneyText";
import { RequestReference } from "../../components/RequestReference";
import { usageEventDetailPath } from "../../routes/paths";
import { CodeText, LoadingBlock, NoticeAlert, PAGE_SIZE_OPTIONS, SummaryList, usePaging } from "../catalog/ProvidersPage";
import { FormModal, useTextRules } from "../pricing/ProviderPricesPage";

/** 没有值的格子。纯标点，不进翻译文件。 */
export const EMPTY = "—";

/** 计费额、估算成本、毛利与账本金额都是 MYR（钱包币种，docs/api.md「列表项」）。 */
export const MYR = "MYR";

const STATUS_OPTIONS = USAGE_EVENT_STATUSES.map((status) => ({ value: status, label: status }));

/** 筛选表单里的原值。时间是 `<input type="datetime-local">` 的吉隆坡墙上时间。 */
export interface UsageFilterValues {
  customer_id?: string | undefined;
  project_id?: string | undefined;
  conversation_id?: string | undefined;
  request_id?: string | undefined;
  provider?: string | undefined;
  model?: string | undefined;
  occurred_from?: string | undefined;
  occurred_to?: string | undefined;
  status?: string | undefined;
  error_code?: string | undefined;
}

const TEXT_FILTERS = [
  "customer_id",
  "project_id",
  "conversation_id",
  "request_id",
  "provider",
  "model",
  "status",
  "error_code",
] as const;
const TIME_FILTERS = ["occurred_from", "occurred_to"] as const;

/** 批量接口不收的条件：带着它们时不能按条件批量（会放回比列表多的事件）。 */
const NOT_IN_BULK = ["project_id", "conversation_id", "request_id"] as const;

function asRfc3339(value: unknown): string | null {
  return typeof value === "string" ? displayTimeToRfc3339(value.trim()) : null;
}

/** 表单值 → 查询条件。空的不带；时间换成带时区的 RFC 3339（表单校验已保证换得出来）。 */
export function toFilters(values: UsageFilterValues): UsageEventFilters {
  const filters: UsageEventFilters = {};
  for (const name of TEXT_FILTERS) {
    const value = (values[name] ?? "").trim();
    if (value !== "") {
      filters[name] = value;
    }
  }
  for (const name of TIME_FILTERS) {
    const value = asRfc3339(values[name]);
    if (value !== null) {
      filters[name] = value;
    }
  }
  return filters;
}

/**
 * 当前筛选条件 → 批量重新入队的请求体（不含原因）。不能按条件批量时返回 `null`：没有选一个错误状态，
 * 或按了批量接口不收的项目 / 会话 / 请求筛。
 */
export function toBulkConditions(filters: UsageEventFilters): Omit<BulkRequeueBody, "reason"> | null {
  const { status } = filters;
  if (status === undefined || !isRequeuableStatus(status)) {
    return null;
  }
  if (NOT_IN_BULK.some((name) => filters[name] !== undefined)) {
    return null;
  }
  const body: Omit<BulkRequeueBody, "reason"> = { status };
  for (const name of ["error_code", "customer_id", "provider", "model", "occurred_from", "occurred_to"] as const) {
    const value = filters[name];
    if (value !== undefined) {
      body[name] = value;
    }
  }
  return body;
}

/** 勾选的事件逐个放回。已被别人放回、已处理或已不存在的计为跳过；其余错误中止并抛出。 */
async function requeueEach(ids: readonly string[], reason: string): Promise<Omit<BulkRequeueResult, "ids">> {
  let requeued = 0;
  let skipped = 0;
  for (const id of ids) {
    try {
      await requeueUsageEvent(id, reason);
      requeued += 1;
    } catch (error) {
      if (
        error instanceof ApiError &&
        (error.code === USAGE_EVENT_NOT_REQUEUABLE || error.code === USAGE_EVENT_NOT_FOUND)
      ) {
        skipped += 1;
        continue;
      }
      throw error;
    }
  }
  return { requeued, skipped };
}

type Acting =
  | { kind: "selected"; ids: string[] }
  | { kind: "bulk"; conditions: Omit<BulkRequeueBody, "reason">; total: number };

export function UsageEventsPage() {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [form] = Form.useForm<UsageFilterValues>();
  const [filters, setFilters] = useState<UsageEventFilters>({});
  const paging = usePaging(DEFAULT_PAGE_SIZE);
  // 勾选只在这一页有意义：换页、换条件、放回之后都清掉。
  const [selected, setSelected] = useState<string[]>([]);
  const [acting, setActing] = useState<Acting | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const events = useQuery({
    queryKey: usageEventListQueryKey(filters, paging.page, paging.pageSize),
    queryFn: ({ signal }) => listUsageEvents(filters, paging.page, paging.pageSize, signal),
    // 翻页、换条件时先留着上一份，不让整张表闪成骨架屏。
    placeholderData: keepPreviousData,
  });

  const apply = (values: UsageFilterValues): void => {
    setFilters(toFilters(values));
    paging.reset();
    setSelected([]);
    setNotice(null);
  };

  const clear = (): void => {
    form.resetFields();
    apply({});
  };

  const goTo = (nextPage: number, nextPageSize: number): void => {
    paging.goTo(nextPage, nextPageSize);
    setSelected([]);
  };

  const refresh = (): void => {
    // 列表与详情一起过期：放回的事件状态变了。
    void queryClient.invalidateQueries({ queryKey: USAGE_EVENTS_QUERY_KEY });
  };

  const finish = (result: Omit<BulkRequeueResult, "ids">): void => {
    refresh();
    setActing(null);
    setSelected([]);
    setNotice(t("usage.requeue.bulkDone", { requeued: result.requeued, skipped: result.skipped }));
  };

  const timeRule: FormRule = {
    validator: (_rule, value: unknown) =>
      typeof value !== "string" || value.trim() === "" || asRfc3339(value) !== null
        ? Promise.resolve()
        : Promise.reject(new Error(t("usage.filter.timeInvalid"))),
  };

  // 后端对 occurred_from >= occurred_to 回 422；提前说清楚是哪一格的问题。两边都是同一种写法，可以按字符串比。
  const rangeRule: FormRule = ({ getFieldValue }) => ({
    validator: (_rule, value: unknown) => {
      const from = asRfc3339(getFieldValue("occurred_from"));
      const to = asRfc3339(value);
      return from === null || to === null || from < to
        ? Promise.resolve()
        : Promise.reject(new Error(t("usage.filter.rangeInvalid")));
    },
  });

  const columns: TableColumnsType<UsageEventSummary> = [
    {
      key: "occurred_at",
      title: t("usage.field.occurredAt"),
      render: (_: unknown, event) => <DateTimeText value={event.occurred_at} />,
    },
    { key: "customer", title: t("usage.field.customer"), dataIndex: "customer_company_name" },
    {
      key: "provider_model",
      title: t("usage.field.providerModel"),
      render: (_: unknown, event) => (
        <span>
          <CodeText code={event.provider} /> / <CodeText code={event.model} />
        </span>
      ),
    },
    {
      key: "usage_type",
      title: t("usage.field.usageType"),
      render: (_: unknown, event) => <CodeText code={event.usage_type} />,
    },
    {
      key: "quantity",
      title: t("usage.field.quantity"),
      render: (_: unknown, event) => <QuantityText event={event} />,
    },
    {
      key: "status",
      title: t("usage.field.status"),
      render: (_: unknown, event) => <UsageStatusTag status={event.status} />,
    },
    {
      key: "error_code",
      title: t("usage.field.errorCode"),
      render: (_: unknown, event) => (event.error_code === null ? EMPTY : <CodeText code={event.error_code} />),
    },
    {
      key: "billable_cost",
      title: t("usage.field.billableCost"),
      render: (_: unknown, event) => <OptionalMoney amount={event.billable_cost} />,
    },
    {
      key: "estimated_cost",
      title: t("usage.field.estimatedCost"),
      render: (_: unknown, event) => <OptionalMoney amount={event.estimated_provider_cost_myr} />,
    },
    {
      key: "details",
      title: t("usage.field.details"),
      render: (_: unknown, event) => <Link to={usageEventDetailPath(event.id)}>{t("usage.view")}</Link>,
    },
  ];

  const conditions = toBulkConditions(filters);
  const total = events.data?.total ?? 0;
  // 换条件、翻页之后新的一页回来之前，表里留着的是上一份（keepPreviousData）：这时的 total 与各行都不属于
  // 当前条件，重新入队的入口一律关掉，免得确认框里的条数、能勾的行与实际提交的条件对不上。
  const stale = events.isPlaceholderData;

  let result: ReactNode;
  if (events.isPending) {
    result = <LoadingBlock text={t("usage.loading")} />;
  } else if (events.isError) {
    result = (
      <UsageErrorAlert
        error={events.error}
        title={t("usage.loadFailed")}
        onRetry={() => void events.refetch()}
      />
    );
  } else if (events.data.total === 0) {
    const filtered = Object.keys(filters).length > 0;
    result = <Empty description={filtered ? t("usage.emptyFiltered") : t("usage.empty")} />;
  } else {
    result = (
      <>
        <Space wrap style={{ marginBottom: 16 }}>
          <Button
            danger
            disabled={stale || selected.length === 0}
            onClick={() => {
              setNotice(null);
              setActing({ kind: "selected", ids: selected });
            }}
          >
            {t("usage.selected.open", { selected: selected.length })}
          </Button>
          <Button
            danger
            disabled={stale || conditions === null}
            onClick={() => {
              if (conditions !== null) {
                setNotice(null);
                setActing({ kind: "bulk", conditions, total });
              }
            }}
          >
            {t("usage.bulk.open")}
          </Button>
          {conditions === null ? (
            <Typography.Text type="secondary">{t("usage.bulk.unavailable")}</Typography.Text>
          ) : null}
        </Space>
        <Table<UsageEventSummary>
          rowKey="id"
          columns={columns}
          dataSource={events.data.items}
          loading={events.isFetching}
          scroll={{ x: true }}
          // 页码超过末页时后端回空 items、total 照报：说清楚是「这一页没有」。
          locale={{ emptyText: t("usage.emptyPage") }}
          rowSelection={{
            selectedRowKeys: selected,
            onChange: (keys: Key[]) => setSelected(keys.map(String)),
            // 只有错误状态的行能勾：其余状态后端一律 409。
            getCheckboxProps: (event) => ({ disabled: stale || !isRequeuableStatus(event.status) }),
          }}
          pagination={{
            current: paging.page,
            pageSize: paging.pageSize,
            total: events.data.total,
            showSizeChanger: true,
            pageSizeOptions: PAGE_SIZE_OPTIONS,
            showTotal: (count) => t("usage.total", { total: count }),
            onChange: goTo,
          }}
        />
      </>
    );
  }

  return (
    <Card title={t("usage.title")}>
      <NoticeAlert message={notice} />
      <AdminOnlyNote />
      <Form<UsageFilterValues>
        form={form}
        name="usageFilters"
        layout="vertical"
        onFinish={apply}
        style={{ display: "flex", flexWrap: "wrap", columnGap: 16, alignItems: "flex-end" }}
      >
        <Form.Item name="customer_id" label={t("usage.filter.customerId")}>
          <Input autoComplete="off" spellCheck={false} />
        </Form.Item>
        <Form.Item name="project_id" label={t("usage.filter.projectId")}>
          <Input autoComplete="off" spellCheck={false} />
        </Form.Item>
        <Form.Item name="conversation_id" label={t("usage.filter.conversationId")}>
          <Input autoComplete="off" spellCheck={false} />
        </Form.Item>
        <Form.Item name="request_id" label={t("usage.filter.requestId")}>
          <Input autoComplete="off" spellCheck={false} />
        </Form.Item>
        <Form.Item name="provider" label={t("usage.filter.provider")}>
          <Input autoComplete="off" spellCheck={false} />
        </Form.Item>
        <Form.Item name="model" label={t("usage.filter.model")}>
          <Input autoComplete="off" spellCheck={false} />
        </Form.Item>
        <Form.Item name="occurred_from" label={t("usage.filter.occurredFrom")} rules={[timeRule]}>
          <Input type="datetime-local" step={1} />
        </Form.Item>
        <Form.Item
          name="occurred_to"
          label={t("usage.filter.occurredTo")}
          dependencies={["occurred_from"]}
          rules={[timeRule, rangeRule]}
        >
          <Input type="datetime-local" step={1} />
        </Form.Item>
        <Form.Item name="status" label={t("usage.filter.status")} style={{ minWidth: 220 }}>
          {/* ⚠️ `virtual={false}`：只有九项，全部渲染出来，测试才找得到每一项。 */}
          <Select allowClear virtual={false} options={STATUS_OPTIONS} placeholder={t("usage.filter.anyStatus")} />
        </Form.Item>
        <Form.Item name="error_code" label={t("usage.filter.errorCode")}>
          <Input autoComplete="off" spellCheck={false} />
        </Form.Item>
        <Form.Item>
          <Space>
            <Button type="primary" htmlType="submit">
              {t("usage.filter.apply")}
            </Button>
            <Button onClick={clear}>{t("usage.filter.clear")}</Button>
          </Space>
        </Form.Item>
      </Form>
      {result}
      {acting?.kind === "selected" ? (
        <RequeueModal
          title={t("usage.selected.title")}
          confirmTitle={t("usage.selected.confirmTitle", { selected: acting.ids.length })}
          subject={
            <Typography.Paragraph>{t("usage.selected.body", { selected: acting.ids.length })}</Typography.Paragraph>
          }
          run={async (reason) => {
            try {
              return await requeueEach(acting.ids, reason);
            } catch (error) {
              // 中途失败时前面的已经放回了：列表照样重读。
              refresh();
              throw error;
            }
          }}
          onDone={finish}
          onCancel={() => setActing(null)}
        />
      ) : null}
      {acting?.kind === "bulk" ? (
        <RequeueModal
          title={t("usage.bulk.title")}
          confirmTitle={t("usage.bulk.confirmTitle")}
          subject={<BulkSubject conditions={acting.conditions} total={acting.total} />}
          run={(reason) => bulkRequeueUsageEvents({ ...acting.conditions, reason })}
          onDone={finish}
          onCancel={() => setActing(null)}
        />
      ) : null}
    </Card>
  );
}

/** 批量的条件与影响条数。条数是 min(列表总数, 1000)：后端一次至多处理 1000 条，有账本行的会被跳过。 */
function BulkSubject({ conditions, total }: { conditions: Omit<BulkRequeueBody, "reason">; total: number }) {
  const { t } = useTranslation();
  const items: NonNullable<DescriptionsProps["items"]> = [
    { key: "status", label: t("usage.field.status"), children: <UsageStatusTag status={conditions.status} /> },
  ];
  const optional = [
    ["error_code", t("usage.filter.errorCode")],
    ["customer_id", t("usage.filter.customerId")],
    ["provider", t("usage.filter.provider")],
    ["model", t("usage.filter.model")],
  ] as const;
  for (const [name, label] of optional) {
    const value = conditions[name];
    if (value !== undefined) {
      items.push({ key: name, label, children: <CodeText code={value} /> });
    }
  }
  if (conditions.occurred_from !== undefined) {
    items.push({
      key: "occurred_from",
      label: t("usage.filter.occurredFrom"),
      children: <RfcTime value={conditions.occurred_from} />,
    });
  }
  if (conditions.occurred_to !== undefined) {
    items.push({
      key: "occurred_to",
      label: t("usage.filter.occurredTo"),
      children: <RfcTime value={conditions.occurred_to} />,
    });
  }
  return (
    <>
      <SummaryList items={items} />
      <Typography.Paragraph strong>
        {t("usage.bulk.count", { events: Math.min(total, BULK_REQUEUE_LIMIT), limit: BULK_REQUEUE_LIMIT })}
      </Typography.Paragraph>
      <Typography.Paragraph type="secondary">{t("usage.bulk.body")}</Typography.Paragraph>
    </>
  );
}

/** 带 `Z` 的 RFC 3339：去掉 `Z` 就是后端的不带时区 UTC 写法，交给 DateTimeText 换成吉隆坡时间。 */
function RfcTime({ value }: { value: string }) {
  return <DateTimeText value={value.endsWith("Z") ? value.slice(0, -1) : value} />;
}

// ---------------------------------------------------------------------------
// 列表与详情共用
// ---------------------------------------------------------------------------

/** 「成本与毛利仅管理员可见」。普通段落，不用 `Alert`（见文件头）。 */
export function AdminOnlyNote() {
  const { t } = useTranslation();
  return (
    <Typography.Paragraph>
      <Tag color="purple">{t("usage.adminOnlyTag")}</Tag>
      <Typography.Text type="secondary">{t("usage.adminOnly")}</Typography.Text>
    </Typography.Paragraph>
  );
}

/** 金额（MYR）；没有时是「—」。 */
export function OptionalMoney({ amount, currency = MYR }: { amount: string | null; currency?: string }) {
  return amount === null ? <>{EMPTY}</> : <MoneyText amount={amount} currency={currency} />;
}

/** 状态标签：四个可重新入队的错误状态红色，其余各有颜色；认不出的原样、无色。 */
export function UsageStatusTag({ status }: { status: string }) {
  let color: string | undefined;
  if (isRequeuableStatus(status)) {
    color = "red";
  } else if (status === "PROCESSED") {
    color = "green";
  } else if (status === "RECEIVED" || status === "PROCESSING") {
    color = "blue";
  } else if (status === "FAILED_RETRYABLE") {
    color = "orange";
  } else if (status === "IDEMPOTENCY_CONFLICT") {
    color = "magenta";
  }
  return color === undefined ? <Tag>{status}</Tag> : <Tag color={color}>{status}</Tag>;
}

/** token 数是整数（不是金额），原样转成文字；没有时「—」。 */
export function tokenCount(value: number | null): string {
  return value === null ? EMPTY : String(value);
}

/** 数量：`QUANTITY` 形态是十进制字符串加单位；token 形态是输入 / 输出 token 数。 */
export function QuantityText({ event }: { event: UsageEventSummary }) {
  const { t } = useTranslation();
  if (event.quantity !== null) {
    return (
      <Space size={4}>
        <DecimalText value={event.quantity} />
        <Typography.Text type="secondary">{event.unit}</Typography.Text>
      </Space>
    );
  }
  return (
    <span>
      {t("usage.tokens", { input: tokenCount(event.input_tokens), output: tokenCount(event.output_tokens) })}
    </span>
  );
}

/**
 * 用量事件接口出错时的提示。
 *
 * 403 `ADMIN_REQUIRED` 单独说「没有权限」—— 前端不做角色判断，是不是管理员只看后端这一句。两节已知的
 * 404 / 409 / 422 码显示对应的文案；认不出的码用调用方给的标题。两种情况下面都带后端 message 与
 * request_id（`RequestReference`），所以未知码也看得到后端的说法。
 */
export function UsageErrorAlert({
  error,
  title,
  onRetry,
}: {
  error: Error | null;
  title: string;
  onRetry?: (() => void) | undefined;
}) {
  const { t } = useTranslation();
  if (error === null) {
    return null;
  }
  const code = error instanceof ApiError ? error.code : null;
  let message = title;
  // 无权限与筛选被拒，重试也还是一样。
  let noRetry = false;
  switch (code) {
    case ADMIN_REQUIRED:
      message = t("usage.forbidden");
      noRetry = true;
      break;
    case USAGE_EVENT_NOT_FOUND:
      message = t("usage.error.notFound");
      break;
    case CUSTOMER_NOT_FOUND:
      message = t("usage.error.customerNotFound");
      break;
    case USAGE_EVENT_NOT_REQUEUABLE:
      message = t("usage.error.notRequeuable");
      break;
    case VALIDATION_ERROR:
      message = t("usage.error.validation");
      noRetry = true;
      break;
    default:
      break;
  }
  return (
    <Alert
      type={code === ADMIN_REQUIRED ? "warning" : "error"}
      showIcon
      style={{ marginBottom: 16 }}
      message={message}
      description={<RequestReference error={error} />}
      action={
        onRetry === undefined || noRetry ? undefined : (
          <Button size="small" onClick={onRetry}>
            {t("usage.retry")}
          </Button>
        )
      }
    />
  );
}

interface ReasonValues {
  reason: string;
}

/**
 * 重新入队：填原因（必填，去首尾空白后 1–255，写进审计）→ 确认 → 发请求。单个、勾选与批量共用。
 *
 * 确认进行中禁用两个按钮、不许关闭，并用 ref 挡住第二次点击（`isPending` 要等下一次渲染才变 true）。
 * 失败时留在确认框里显示错误（可以返回去改原因）；成功后交给 `onDone`，由调用方关掉对话框并让列表过期重读。
 */
export function RequeueModal<Result>({
  title,
  confirmTitle,
  subject,
  run,
  onDone,
  onCancel,
}: {
  title: string;
  confirmTitle: string;
  subject: ReactNode;
  run: (reason: string) => Promise<Result>;
  onDone: (result: Result) => void;
  onCancel: () => void;
}) {
  const { t } = useTranslation();
  const [form] = Form.useForm<ReasonValues>();
  const [reviewing, setReviewing] = useState<string | null>(null);
  const reasonRules = useTextRules(t("usage.requeue.reasonRequired"), t("usage.requeue.reasonTooLong"));

  return (
    <>
      <FormModal title={title} form={form} onCancel={onCancel}>
        {subject}
        <Form<ReasonValues>
          name="requeue"
          form={form}
          layout="vertical"
          initialValues={{ reason: "" }}
          onFinish={(values) => setReviewing(values.reason.trim())}
        >
          <Form.Item
            name="reason"
            label={t("usage.requeue.reason")}
            extra={t("usage.requeue.reasonHint")}
            rules={reasonRules}
          >
            <Input.TextArea rows={3} />
          </Form.Item>
        </Form>
      </FormModal>
      {reviewing === null ? null : (
        <RequeueConfirmModal
          title={confirmTitle}
          run={() => run(reviewing)}
          onDone={onDone}
          onBack={() => setReviewing(null)}
        >
          {subject}
          <SummaryList items={[{ key: "reason", label: t("usage.requeue.reason"), children: reviewing }]} />
          <Typography.Paragraph type="secondary">{t("usage.requeue.body")}</Typography.Paragraph>
        </RequeueConfirmModal>
      )}
    </>
  );
}

function RequeueConfirmModal<Result>({
  title,
  run,
  onDone,
  onBack,
  children,
}: {
  title: string;
  run: () => Promise<Result>;
  onDone: (result: Result) => void;
  onBack: () => void;
  children: ReactNode;
}) {
  const { t } = useTranslation();
  const inFlight = useRef(false);
  const write = useMutation({
    mutationFn: run,
    onSuccess: (result) => onDone(result),
    onSettled: () => {
      inFlight.current = false;
    },
  });

  const confirm = (): void => {
    if (inFlight.current) {
      return;
    }
    inFlight.current = true;
    write.mutate();
  };

  const pending = write.isPending;
  const back = (): void => {
    if (!pending) {
      onBack();
    }
  };

  return (
    <Modal
      open
      title={title}
      onCancel={back}
      maskClosable={false}
      closable={!pending}
      keyboard={!pending}
      footer={[
        <Button key="back" onClick={back} disabled={pending}>
          {t("pricing.backToEdit")}
        </Button>,
        <Button key="confirm" type="primary" danger onClick={confirm} loading={pending} disabled={pending}>
          {t("usage.requeue.confirm")}
        </Button>,
      ]}
    >
      {children}
      <div style={{ marginTop: 16 }}>
        <UsageErrorAlert error={write.error} title={t("usage.requeue.failed")} />
      </div>
    </Modal>
  );
}

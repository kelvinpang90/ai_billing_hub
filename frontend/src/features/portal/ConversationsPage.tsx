/**
 * 客户门户会话页（`/portal/conversations`，AIH-TASK-123；设计闸门 #460 v2 §2「前端」、§8 第 3 步）。
 *
 * 语义以 docs/api.md「客户会话」为准：项目下拉来自 `GET /api/v1/customer/projects`，表格来自
 * `GET /api/v1/customer/conversations`。只读：页面上没有任何写操作。
 *
 * 日期与用量页**同一套规则**，用的就是 `UsagePage.tsx` 导出的那几个函数（不另写一份）：默认马来西亚时间本月 1 日到
 * 今天，日期选择限制在 2020-01-01 到 2099-12-31，区间不完整、倒置、越界或超过 92 天时不发请求、在表格的位置说明原因。
 * 筛选只有日期与项目（接口不按供应商、模型或 `conversation_id` 筛），点「Apply filters」才生效，只放在组件状态里。
 *
 * 表格上方一句说明：只列有会话 ID 的请求；没有会话 ID 的在用量页看；合计只算所选区间内的请求（跨区间边界的会话只算
 * 区间内那部分，「First request」是区间内的第一条，不是会话真正的开始）。
 *
 * 分页是不透明游标：「Older」把 `next_cursor` 原样作为下一次的 `cursor`，为 `null` 时按钮不出现；「Newest」回到不带
 * 游标的最新一页；换了条件游标自动作废。
 *
 * 点一行（或会话 ID 链接）跳到用量页，恰好带 `conversation_id`、`project_id`、`from_date`、`to_date` 四个参数，日期是
 * 页面**当前已应用**的区间（不是表单里还没点「Apply filters」的值）：那里列出的请求与这一行的合计逐条对得上。
 *
 * 三种状态都画出来：加载中、失败（带 request_id）、空列表；`QUERY_TOO_BROAD` 显示专门文案。
 *
 * 「Export CSV」（AIH-TASK-124；设计闸门 #460 v2 §2「前端」，语义以 docs/api.md「客户导出」的 conversations/export
 * 小节为准）用页面**当前已应用**的筛选（日期与项目，不是表单里还没点「Apply filters」的值）调
 * `GET /api/v1/customer/conversations/export`，交给浏览器下载；不带 `page_size` 与 `cursor`。日期区间无效时按钮不可用；
 * 导出中按钮转为加载状态、不可重复点。`EXPORT_TOO_LARGE`、`QUERY_TOO_BROAD`、`EXPORT_BUSY` 各有专门文案，其余失败带
 * request_id；换筛选时收起上一次的失败。导出不影响列表、筛选与翻页。
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
import { Link, useNavigate } from "react-router";

import { ApiError } from "../../api/client";
import {
  customerConversationListQueryKey,
  exportCustomerConversations,
  listCustomerConversations,
  type CustomerConversation,
  type CustomerConversationExportQuery,
  type CustomerConversationQuery,
} from "../../api/customerConversations";
import { CUSTOMER_PROJECTS_QUERY_KEY, QUERY_TOO_BROAD, listCustomerProjects } from "../../api/customerUsage";
import { EXPORT_BUSY, EXPORT_TOO_LARGE } from "../../api/download";
import { RequestReference } from "../../components/RequestReference";
import { ChargedAmount, UsageTime, tokenCount } from "./UsageDetail";
import {
  MAX_DATE,
  MIN_DATE,
  RangeProblemText,
  conversationUsagePath,
  dateRangeProblem,
  defaultDateRange,
} from "./UsagePage";

/** 每页行数：与接口的默认值相同（docs/api.md：1–100，默认 20）。 */
export const CONVERSATIONS_PAGE_SIZE = 20;

/** 筛选表单的值。日期是 `<input type="date">` 的 `YYYY-MM-DD`。 */
interface ConversationFilterValues {
  fromDate: string;
  toDate: string;
  projectId?: string | undefined;
}

/** 一行的 key：会话的键是（项目, `conversation_id`），同一个 id 可以出现在两个项目里。 */
function rowKey(row: CustomerConversation): string {
  return `${row.project_id}|${row.conversation_id}`;
}

/** 导出失败的说明：三种可以由客户自己处理的错误各有专门文案，其余是通用文案（request_id 另外显示）。 */
function ExportFailureText({ error }: { error: Error }) {
  const { t } = useTranslation();
  const code = error instanceof ApiError ? error.code : null;
  switch (code) {
    case EXPORT_TOO_LARGE:
      return t("portal.conversations.export.tooLarge");
    case QUERY_TOO_BROAD:
      return t("portal.conversations.export.tooBroad");
    case EXPORT_BUSY:
      return t("portal.conversations.export.busy");
    default:
      return t("portal.conversations.export.failed");
  }
}

const SPECIFIC_EXPORT_FAILURES: ReadonlySet<string> = new Set([EXPORT_TOO_LARGE, QUERY_TOO_BROAD, EXPORT_BUSY]);

/** 三种专门文案之外的失败：要显示 request_id。 */
function isGenericExportFailure(error: Error): boolean {
  return !(error instanceof ApiError) || !SPECIFIC_EXPORT_FAILURES.has(error.code);
}

export function ConversationsPage() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const [form] = Form.useForm<ConversationFilterValues>();
  // 进页面那一刻的「今天」：停在页面上跨过午夜不会自己换区间，点「Reset」会按新的今天重算。
  const [initialRange] = useState(() => defaultDateRange(new Date()));
  const [filters, setFilters] = useState<ConversationFilterValues>(initialRange);
  // 游标连同它所属的条件一起记：条件一变，旧条件下的游标自动作废，回到最新一页。
  const [paging, setPaging] = useState<{ scope: string; cursor: string } | null>(null);

  const problem = dateRangeProblem(filters.fromDate, filters.toDate);
  const projectId = (filters.projectId ?? "").trim();

  let base: CustomerConversationQuery | null = null;
  if (problem === null) {
    base = {
      fromDate: filters.fromDate,
      toDate: filters.toDate,
      projectId: projectId === "" ? null : projectId,
      pageSize: CONVERSATIONS_PAGE_SIZE,
    };
  }
  const scope = JSON.stringify(base);
  const cursor = paging !== null && paging.scope === scope ? paging.cursor : null;

  // 导出用已应用的筛选（与列表同一份），不带 pageSize 与 cursor。区间不合规时没有可导出的条件，按钮不可用。
  let exportQuery: CustomerConversationExportQuery | null = null;
  if (base !== null) {
    exportQuery = { fromDate: base.fromDate, toDate: base.toDate, projectId: base.projectId ?? null };
  }

  const projects = useQuery({
    queryKey: CUSTOMER_PROJECTS_QUERY_KEY,
    queryFn: ({ signal }) => listCustomerProjects(signal),
    // 理由同钱包页：同一个标签页换账号登录，拿到的必须是新客户的项目。
    gcTime: 0,
  });

  const conversations = useQuery({
    queryKey: customerConversationListQueryKey({ ...(base ?? initialRange), cursor }),
    queryFn: ({ signal }) => listCustomerConversations({ ...(base ?? initialRange), cursor }, signal),
    // 区间不合规时不发请求（后端反正会 422），页面上说明原因。
    enabled: base !== null,
    // 翻页、换条件时先留着上一份，不让整张表闪成骨架屏。
    placeholderData: keepPreviousData,
    gcTime: 0,
  });

  /** 这一行在用量页的地址：日期取已应用的区间（列表就是按它查的）。 */
  const usagePathOf = (row: CustomerConversation): string =>
    conversationUsagePath({
      conversationId: row.conversation_id,
      projectId: row.project_id,
      fromDate: filters.fromDate,
      toDate: filters.toDate,
    });

  const exporting = useMutation({
    mutationFn: (query: CustomerConversationExportQuery) => exportCustomerConversations(query),
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

  const apply = (values: ConversationFilterValues): void => {
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

  const next = conversations.data?.next_cursor ?? null;
  // 占位数据是上一份的：拿它的游标翻页会翻错条件或重复请求同一页，等新的一页回来再让按钮可点。
  const settled = !conversations.isPlaceholderData;

  const columns: TableColumnsType<CustomerConversation> = [
    {
      key: "conversation_id",
      title: t("portal.conversations.field.conversationId"),
      render: (_: unknown, row) => <Link to={usagePathOf(row)}>{row.conversation_id}</Link>,
    },
    { key: "project", title: t("portal.conversations.field.project"), dataIndex: "project_name" },
    {
      key: "first_occurred_at",
      title: t("portal.conversations.field.firstOccurredAt"),
      render: (_: unknown, row) => <UsageTime value={row.first_occurred_at} />,
    },
    {
      key: "last_occurred_at",
      title: t("portal.conversations.field.lastOccurredAt"),
      render: (_: unknown, row) => <UsageTime value={row.last_occurred_at} />,
    },
    {
      key: "request_count",
      title: t("portal.conversations.field.requestCount"),
      align: "right",
      render: (_: unknown, row) => String(row.request_count),
    },
    {
      key: "input_tokens",
      title: t("portal.conversations.field.inputTokens"),
      align: "right",
      render: (_: unknown, row) => tokenCount(row.input_tokens),
    },
    {
      key: "output_tokens",
      title: t("portal.conversations.field.outputTokens"),
      align: "right",
      render: (_: unknown, row) => tokenCount(row.output_tokens),
    },
    {
      key: "charged_amount",
      title: t("portal.conversations.field.chargedAmount"),
      align: "right",
      render: (_: unknown, row) => <ChargedAmount value={row.charged_amount} />,
    },
  ];

  let body: ReactNode;
  if (problem !== null) {
    body = <Alert type="warning" showIcon message={<RangeProblemText problem={problem} />} />;
  } else if (conversations.isPending) {
    body = (
      <Space direction="vertical" aria-busy="true" style={{ width: "100%" }}>
        <Skeleton active title={false} paragraph={{ rows: 3 }} />
        <Typography.Text type="secondary">{t("portal.conversations.loading")}</Typography.Text>
      </Space>
    );
  } else if (conversations.isError) {
    const tooBroad = conversations.error instanceof ApiError && conversations.error.code === QUERY_TOO_BROAD;
    body = (
      <Alert
        type="error"
        showIcon
        message={tooBroad ? t("portal.conversations.queryTooBroad") : t("portal.conversations.loadFailed")}
        description={<RequestReference error={conversations.error} />}
        action={
          <Button size="small" onClick={() => void conversations.refetch()}>
            {t("portal.usage.retry")}
          </Button>
        }
      />
    );
  } else if (conversations.data.items.length === 0) {
    body = (
      <Empty description={cursor === null ? t("portal.conversations.empty") : t("portal.conversations.emptyPage")} />
    );
  } else {
    body = (
      <Table<CustomerConversation>
        rowKey={rowKey}
        columns={columns}
        dataSource={conversations.data.items}
        loading={conversations.isFetching}
        pagination={false}
        scroll={{ x: "max-content" }}
        onRow={(row) => ({
          style: { cursor: "pointer" },
          onClick: (event) => {
            // 点的是会话 ID 链接时由链接自己跳，这里不再跳一次（否则历史里多一条）。
            if (event.target instanceof Element && event.target.closest("a") !== null) {
              return;
            }
            void navigate(usagePathOf(row));
          },
        })}
      />
    );
  }

  return (
    // role + aria-label：部署后验收脚本（check_portal_conversations）按语义结构找到这一块（不依赖 antd 的类名）。
    <Card role="region" aria-label={t("portal.conversations.title")} title={t("portal.conversations.title")}>
      <Form<ConversationFilterValues>
        form={form}
        name="customerConversationFilters"
        layout="vertical"
        initialValues={initialRange}
        onFinish={apply}
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
        <Form.Item>
          <Space>
            <Button type="primary" htmlType="submit">
              {t("portal.usage.filter.apply")}
            </Button>
            <Button onClick={reset}>{t("portal.usage.filter.reset")}</Button>
          </Space>
        </Form.Item>
      </Form>
      <Typography.Paragraph type="secondary">{t("portal.conversations.note")}</Typography.Paragraph>
      <Space wrap style={{ marginBottom: 16 }}>
        {problem === null ? (
          <Button onClick={() => setPaging(null)} disabled={cursor === null}>
            {t("portal.usage.newest")}
          </Button>
        ) : null}
        {problem === null && next !== null && !conversations.isError ? (
          <Button onClick={() => setPaging({ scope, cursor: next })} disabled={!settled}>
            {t("portal.usage.older")}
          </Button>
        ) : null}
        <Button onClick={startExport} loading={exporting.isPending} disabled={exportQuery === null || exporting.isPending}>
          {t("portal.conversations.export.button")}
        </Button>
      </Space>
      {exporting.error ? (
        <Alert
          type="error"
          showIcon
          closable
          onClose={() => exporting.reset()}
          style={{ marginBottom: 16 }}
          message={<ExportFailureText error={exporting.error} />}
          description={isGenericExportFailure(exporting.error) ? <RequestReference error={exporting.error} /> : null}
        />
      ) : null}
      {body}
    </Card>
  );
}

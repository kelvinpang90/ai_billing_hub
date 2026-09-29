/**
 * 管理端审计日志（`GET /api/v1/admin/audit-logs`，spec §89 / §101，AIH-TASK-023）。
 *
 * 只读：这一页不发任何写请求。顺序由后端决定（最新在前，按审计行的自增 id），前端不重排。
 *
 * 筛选条件与页码放在组件状态里，**不进地址栏**（与客户列表不同）：条件里有操作者邮箱，
 * 进了地址栏就会留在浏览器历史里，刷新时还会连同查询串进 nginx 的访问日志。
 *
 * 时间段按吉隆坡时间输入，发请求前换成不带时区的 UTC（`displayTimeToUtc`，与
 * `DateTimeText` 的方向相反）。改任何筛选条件都回到第 1 页。
 *
 * 三种状态都画出来（spec §132 DoD 第 8 条）：加载中、失败（带 request_id）、空列表。
 * 403 说「没有权限」，422 说「筛选条件不被接受」并显示后端的 message（它只列字段名）。
 */

import { keepPreviousData, useQuery } from "@tanstack/react-query";
import {
  Alert,
  Button,
  Card,
  Descriptions,
  Empty,
  Form,
  Input,
  Select,
  Skeleton,
  Space,
  Table,
  Typography,
  type FormRule,
  type TableColumnsType,
} from "antd";
import { useState, type Key, type ReactNode } from "react";
import { useTranslation } from "react-i18next";

import {
  AUDIT_ACTIONS,
  SYSTEM_ACTOR_ROLE,
  VALIDATION_ERROR,
  auditLogListQueryKey,
  listAuditLogs,
  type AuditLogEntry,
  type AuditLogFilters,
} from "../../api/adminAudit";
import { ADMIN_REQUIRED, DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE } from "../../api/adminCustomers";
import { ApiError } from "../../api/client";
import { DateTimeText, displayTimeToUtc } from "../../components/DateTimeText";
import { RequestReference } from "../../components/RequestReference";

const PAGE_SIZE_OPTIONS = [10, 20, 50, MAX_PAGE_SIZE];

/** 没有值的格子。纯标点，不进翻译文件。 */
const EMPTY = "—";

const ACTION_OPTIONS = AUDIT_ACTIONS.map((action) => ({ value: action, label: action }));

/** 筛选表单里的原值。时间是 `<input type="datetime-local">` 的吉隆坡墙上时间。 */
export interface AuditFilterValues {
  action?: string | undefined;
  entity_type?: string | undefined;
  entity_id?: string | undefined;
  actor_email?: string | undefined;
  created_from?: string | undefined;
  created_to?: string | undefined;
}

const TEXT_FILTERS = ["action", "entity_type", "entity_id", "actor_email"] as const;
const TIME_FILTERS = ["created_from", "created_to"] as const;

function asUtc(value: unknown): string | null {
  return typeof value === "string" ? displayTimeToUtc(value.trim()) : null;
}

/** 表单值 → 查询条件。空的不带；时间换成不带时区的 UTC（表单校验已保证换得出来）。 */
export function toFilters(values: AuditFilterValues): AuditLogFilters {
  const filters: AuditLogFilters = {};
  for (const name of TEXT_FILTERS) {
    const value = (values[name] ?? "").trim();
    if (value !== "") {
      filters[name] = value;
    }
  }
  for (const name of TIME_FILTERS) {
    const value = asUtc(values[name]);
    if (value !== null) {
      filters[name] = value;
    }
  }
  return filters;
}

/** 表格行：审计本身没有 id（后端刻意不给），这一页里的位置就是它的 key。 */
type AuditRow = AuditLogEntry & { key: string };

/** 操作者。没有邮箱时区分「系统动作」与「不知道是谁」。 */
export function ActorText({ entry }: { entry: AuditLogEntry }) {
  const { t } = useTranslation();
  if (entry.actor_email !== null) {
    return <span>{entry.actor_email}</span>;
  }
  return (
    <Typography.Text type="secondary">
      {entry.actor_role === SYSTEM_ACTOR_ROLE ? t("audit.actor.system") : t("audit.actor.unknown")}
    </Typography.Text>
  );
}

/** 对象：类型，加上对外 id（或以用户为对象时的那个用户的邮箱）。 */
function EntityText({ entry }: { entry: AuditLogEntry }) {
  if (entry.entity_type === null) {
    return <span>{EMPTY}</span>;
  }
  const target = entry.entity_id ?? entry.entity_user_email;
  return (
    <div>
      <div>{entry.entity_type}</div>
      {target !== null && (
        <Typography.Text type="secondary" copyable>
          {target}
        </Typography.Text>
      )}
    </div>
  );
}

function StateBlock({ state }: { state: Record<string, unknown> | null }) {
  const { t } = useTranslation();
  if (state === null) {
    return <Typography.Text type="secondary">{t("audit.detail.none")}</Typography.Text>;
  }
  return (
    <pre style={{ margin: 0, whiteSpace: "pre-wrap", wordBreak: "break-word" }}>
      {JSON.stringify(state, null, 2)}
    </pre>
  );
}

/** 展开行：前后状态与 user agent。 */
function AuditEntryDetails({ entry }: { entry: AuditLogEntry }) {
  const { t } = useTranslation();
  const none = <Typography.Text type="secondary">{t("audit.detail.none")}</Typography.Text>;
  return (
    <Descriptions
      bordered
      size="small"
      column={1}
      items={[
        { key: "actor_role", label: t("audit.detail.actorRole"), children: entry.actor_role ?? none },
        { key: "user_agent", label: t("audit.detail.userAgent"), children: entry.user_agent ?? none },
        { key: "before_state", label: t("audit.detail.before"), children: <StateBlock state={entry.before_state} /> },
        { key: "after_state", label: t("audit.detail.after"), children: <StateBlock state={entry.after_state} /> },
      ]}
    />
  );
}

/** 403 说没有权限（是不是管理员只看后端这一句）；422 说筛选条件不被接受；其余是加载失败，可重试。 */
function AuditErrorAlert({ error, onRetry }: { error: Error; onRetry: () => void }) {
  const { t } = useTranslation();
  const code = error instanceof ApiError ? error.code : null;
  const forbidden = code === ADMIN_REQUIRED;
  const invalid = code === VALIDATION_ERROR;
  let title = t("audit.loadFailed");
  if (forbidden) {
    title = t("audit.forbidden");
  } else if (invalid) {
    title = t("audit.invalidFilters");
  }
  return (
    <Alert
      type={forbidden ? "warning" : "error"}
      showIcon
      message={title}
      description={<RequestReference error={error} />}
      action={
        forbidden || invalid ? undefined : (
          <Button size="small" onClick={onRetry}>
            {t("audit.retry")}
          </Button>
        )
      }
    />
  );
}

export function AuditLogPage() {
  const { t } = useTranslation();
  const [form] = Form.useForm<AuditFilterValues>();
  const [filters, setFilters] = useState<AuditLogFilters>({});
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(DEFAULT_PAGE_SIZE);
  // 行 key 是「这一页的第几条」：换页、换条件时必须一起清掉，否则新的第 1 条会顶着旧的展开状态。
  const [expanded, setExpanded] = useState<readonly Key[]>([]);

  const logs = useQuery({
    queryKey: auditLogListQueryKey(filters, page, pageSize),
    queryFn: ({ signal }) => listAuditLogs(filters, page, pageSize, signal),
    // 翻页、换条件时先留着上一份，不让整张表闪成骨架屏。
    placeholderData: keepPreviousData,
  });

  const apply = (values: AuditFilterValues): void => {
    setFilters(toFilters(values));
    setPage(1);
    setExpanded([]);
  };

  const clear = (): void => {
    form.resetFields();
    apply({});
  };

  const goTo = (nextPage: number, nextPageSize: number): void => {
    // 换了每页条数，原来的页码就没有意义了，回到第一页。
    setPage(nextPageSize === pageSize ? nextPage : 1);
    setPageSize(nextPageSize);
    setExpanded([]);
  };

  const timeRule: FormRule = {
    validator: (_rule, value: unknown) =>
      typeof value !== "string" || value.trim() === "" || asUtc(value) !== null
        ? Promise.resolve()
        : Promise.reject(new Error(t("audit.filter.timeInvalid"))),
  };

  // 后端对 created_from >= created_to 回 422；提前说清楚是哪一格的问题。
  const rangeRule: FormRule = ({ getFieldValue }) => ({
    validator: (_rule, value: unknown) => {
      const from = asUtc(getFieldValue("created_from"));
      const to = asUtc(value);
      return from === null || to === null || from < to
        ? Promise.resolve()
        : Promise.reject(new Error(t("audit.filter.rangeInvalid")));
    },
  });

  const columns: TableColumnsType<AuditRow> = [
    {
      key: "created_at",
      title: t("audit.column.time"),
      render: (_: unknown, entry) => <DateTimeText value={entry.created_at} />,
    },
    { key: "action", title: t("audit.column.action"), dataIndex: "action" },
    {
      key: "actor",
      title: t("audit.column.actor"),
      render: (_: unknown, entry) => <ActorText entry={entry} />,
    },
    {
      key: "entity",
      title: t("audit.column.entity"),
      render: (_: unknown, entry) => <EntityText entry={entry} />,
    },
    {
      key: "ip_address",
      title: t("audit.column.ip"),
      render: (_: unknown, entry) => entry.ip_address ?? EMPTY,
    },
    {
      key: "reason",
      title: t("audit.column.reason"),
      render: (_: unknown, entry) => entry.reason ?? EMPTY,
    },
  ];

  let result: ReactNode;
  if (logs.isPending) {
    result = (
      <>
        <Skeleton active title={false} paragraph={{ rows: 4 }} />
        <Typography.Text type="secondary">{t("audit.loading")}</Typography.Text>
      </>
    );
  } else if (logs.isError) {
    result = <AuditErrorAlert error={logs.error} onRetry={() => void logs.refetch()} />;
  } else if (logs.data.total === 0) {
    const filtered = Object.keys(filters).length > 0;
    result = <Empty description={filtered ? t("audit.emptyFiltered") : t("audit.empty")} />;
  } else {
    const rows: AuditRow[] = logs.data.items.map((entry, index) => ({ ...entry, key: String(index) }));
    result = (
      <Table<AuditRow>
        rowKey="key"
        columns={columns}
        dataSource={rows}
        loading={logs.isFetching}
        // 页码超过末页时后端回空 items、total 照报：说清楚是「这一页没有」。
        locale={{ emptyText: t("audit.emptyPage") }}
        expandable={{
          expandedRowRender: (entry) => <AuditEntryDetails entry={entry} />,
          expandedRowKeys: expanded,
          onExpandedRowsChange: setExpanded,
        }}
        pagination={{
          current: page,
          pageSize,
          total: logs.data.total,
          showSizeChanger: true,
          pageSizeOptions: PAGE_SIZE_OPTIONS,
          showTotal: (count) => t("audit.total", { total: count }),
          onChange: goTo,
        }}
      />
    );
  }

  return (
    <Card title={t("audit.title")}>
      <Form<AuditFilterValues>
        form={form}
        name="auditFilters"
        layout="vertical"
        onFinish={apply}
        style={{ display: "flex", flexWrap: "wrap", columnGap: 16, alignItems: "flex-end" }}
      >
        <Form.Item name="action" label={t("audit.filter.action")} style={{ minWidth: 260 }}>
          {/* ⚠️ `virtual={false}`：选项只有二十来个，全部渲染出来，测试与浏览器验收脚本
              才找得到每一项（虚拟列表只渲染可视区域里的几项）。 */}
          <Select
            showSearch
            allowClear
            virtual={false}
            options={ACTION_OPTIONS}
            placeholder={t("audit.filter.anyAction")}
          />
        </Form.Item>
        <Form.Item name="entity_type" label={t("audit.filter.entityType")}>
          <Input />
        </Form.Item>
        <Form.Item name="entity_id" label={t("audit.filter.entityId")}>
          <Input />
        </Form.Item>
        <Form.Item name="actor_email" label={t("audit.filter.actorEmail")}>
          <Input inputMode="email" />
        </Form.Item>
        <Form.Item name="created_from" label={t("audit.filter.createdFrom")} rules={[timeRule]}>
          <Input type="datetime-local" step={1} />
        </Form.Item>
        <Form.Item
          name="created_to"
          label={t("audit.filter.createdTo")}
          dependencies={["created_from"]}
          rules={[timeRule, rangeRule]}
        >
          <Input type="datetime-local" step={1} />
        </Form.Item>
        <Form.Item>
          <Space>
            <Button type="primary" htmlType="submit">
              {t("audit.filter.apply")}
            </Button>
            <Button onClick={clear}>{t("audit.filter.clear")}</Button>
          </Space>
        </Form.Item>
      </Form>
      {result}
    </Card>
  );
}

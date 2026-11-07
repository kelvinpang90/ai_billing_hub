/**
 * 管理端月度对账单页（`/statements`，AIH-TASK-161；设计闸门 #565 v6 §2「前端」）。
 *
 * 语义以 docs/api.md「月度对账单（管理端）」为准：上面是对账单列表（`GET /api/v1/admin/statements`），可按客户
 * （下拉，客户列表逐页取全）与账期（`YYYY-MM` 输入框，形状不对时不发请求、只提示）筛，每页 20 条，只有上一页 /
 * 下一页；改任一筛选回到第 1 页。顺序由后端决定，前端不重排。点一行在下面显示这份对账单的详情（`GET …/{statement_id}`），
 * 再点同一行收起；改筛选或翻页时清掉已选的详情。
 *
 * 按客户筛选时显示「Generate missing statements」（`POST /api/v1/admin/customers/{customer_id}/statements/generate`，
 * 没有请求体）：成功说新生成了几份，失败显示错误与 request_id；两种结果都刷新列表（失败之前可能已生成了几期）。
 *
 * 金额一律用 `MoneyText`，币种固定 MYR（对账单只有 MYR）。时间按吉隆坡时间（UTC+8）显示，列头标 MYT。快照里的
 * `tax_notice.en` 原样显示，不改写、不拼接。期末余额按 T+1 归属计算，不是月末那一刻的钱包余额 —— 页面上写明这一点。
 *
 * 三种状态都画出来（spec §132 DoD 第 8 条）：加载中、失败（带 request_id）、空列表。403 说「没有权限」。只请求
 * `/api/v1/admin/` 路径。
 *
 * ⚠️ 说明与成功提示不用 antd 的 `Alert`：它带 `role="alert"`，部署后浏览器验收把页面上任何 `role="alert"` 都当成
 * 错误提示（scripts/acceptance/admin_customers.mjs）。账期的格式提示放在 `extra` 里，只有格式不对时才进 `help`。
 */

import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
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
  Tag,
  Typography,
  type TableColumnsType,
} from "antd";
import { useRef, useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";

import { ADMIN_REQUIRED, CUSTOMER_NOT_FOUND, DEFAULT_PAGE_SIZE } from "../../api/adminCustomers";
import {
  PERIOD_PATTERN,
  STATEMENT_CURRENCY,
  STATEMENT_LISTS_QUERY_KEY,
  STATEMENT_NOT_ELIGIBLE,
  STATEMENT_NOT_FOUND,
  generateStatements,
  getStatement,
  listStatements,
  statementDetailQueryKey,
  statementListQueryKey,
  type GeneratedStatement,
  type ModelUsage,
  type ProjectUsage,
  type ProviderUsage,
  type StatementDetail,
  type StatementFilter,
  type StatementSummary,
  type UsageCounts,
} from "../../api/adminStatements";
import { ApiError } from "../../api/client";
import { DateTimeText } from "../../components/DateTimeText";
import { MoneyText } from "../../components/MoneyText";
import { RequestReference } from "../../components/RequestReference";
import { customerOptions, useAllCustomers } from "../pricing/PricingRulesPage";

/** 没有值的格子。纯标点，不进翻译文件。 */
const EMPTY = "—";

type TFunction = ReturnType<typeof useTranslation>["t"];

function Money({ amount }: { amount: string }) {
  return <MoneyText amount={amount} currency={STATEMENT_CURRENCY} />;
}

/** 对账单状态的文案；认不出的值原样显示。 */
function StatusTag({ status }: { status: string }) {
  const { t } = useTranslation();
  switch (status) {
    case "FINALIZED":
      return <Tag color="success">{t("statements.status.finalized")}</Tag>;
    case "DRAFT":
      return <Tag>{t("statements.status.draft")}</Tag>;
    case "VOID":
      return <Tag color="default">{t("statements.status.void")}</Tag>;
    default:
      return <Tag>{status}</Tag>;
  }
}

function Loading({ text }: { text: string }) {
  return (
    <>
      <Skeleton active title={false} paragraph={{ rows: 3 }} />
      <Typography.Text type="secondary">{text}</Typography.Text>
    </>
  );
}

/**
 * 对账单接口出错时的提示：403 说没有权限，已知的 404 / 409 码说对应的一句，其余用调用方给的标题。都带后端
 * message 与 request_id；给了 `onRetry` 且不是无权限 / 不存在时带「Try again」。
 */
function StatementErrorAlert({
  error,
  failedText,
  onRetry,
}: {
  error: Error;
  failedText: string;
  onRetry?: (() => void) | undefined;
}) {
  const { t } = useTranslation();
  const code = error instanceof ApiError ? error.code : null;
  const forbidden = code === ADMIN_REQUIRED;
  let title = failedText;
  switch (code) {
    case ADMIN_REQUIRED:
      title = t("statements.forbidden");
      break;
    case STATEMENT_NOT_FOUND:
      title = t("statements.detail.notFound");
      break;
    case CUSTOMER_NOT_FOUND:
      title = t("statements.generate.customerNotFound");
      break;
    case STATEMENT_NOT_ELIGIBLE:
      title = t("statements.generate.notEligible");
      break;
    default:
      break;
  }
  // 无权限与不存在重试也一样，不给按钮。
  const retry = forbidden || code === STATEMENT_NOT_FOUND ? undefined : onRetry;
  return (
    <Alert
      type={forbidden ? "warning" : "error"}
      showIcon
      style={{ marginBottom: 16 }}
      message={title}
      description={<RequestReference error={error} />}
      action={
        retry === undefined ? undefined : (
          <Button size="small" onClick={() => retry()}>
            {t("statements.retry")}
          </Button>
        )
      }
    />
  );
}

/** 明细表共有的四列：请求数、输入 / 输出 token、客户计费。 */
function countColumns<T extends UsageCounts>(t: TFunction): TableColumnsType<T> {
  return [
    { key: "request_count", title: t("statements.field.requests"), render: (_: unknown, row) => row.request_count },
    { key: "input_tokens", title: t("statements.field.inputTokens"), render: (_: unknown, row) => row.input_tokens },
    { key: "output_tokens", title: t("statements.field.outputTokens"), render: (_: unknown, row) => row.output_tokens },
    { key: "charges", title: t("statements.field.charges"), render: (_: unknown, row) => <Money amount={row.charges} /> },
  ];
}

/** 一张明细表，单独成一块（role=region），用例按 aria-label 找它。顺序由后端决定。 */
function BreakdownTable<T extends UsageCounts>({
  title,
  rows,
  rowKey,
  columns,
}: {
  title: string;
  rows: T[];
  rowKey: (row: T) => string;
  columns: TableColumnsType<T>;
}) {
  const { t } = useTranslation();
  return (
    <div role="region" aria-label={title} style={{ marginBottom: 16 }}>
      <Typography.Title level={5}>{title}</Typography.Title>
      <Table<T>
        size="small"
        rowKey={rowKey}
        columns={columns}
        dataSource={rows}
        pagination={false}
        locale={{ emptyText: t("statements.detail.noUsage") }}
      />
    </div>
  );
}

/** 上期调整表的一行：按原用量期间逐行，最后是 rebill 与小计。 */
interface PriorRow {
  key: string;
  label: ReactNode;
  requests: number | null;
  amount: string;
}

/** 选中的那一份对账单的详情。换一份时由调用方用 `key` 重建。 */
function StatementDetailCard({ statementId }: { statementId: string }) {
  const { t } = useTranslation();
  const detail = useQuery({
    queryKey: statementDetailQueryKey(statementId),
    queryFn: ({ signal }) => getStatement(statementId, signal),
  });

  const title = t("statements.detail.title");
  let body: ReactNode;
  if (detail.isPending) {
    body = <Loading text={t("statements.detail.loading")} />;
  } else if (detail.isError) {
    body = (
      <StatementErrorAlert
        error={detail.error}
        failedText={t("statements.detail.loadFailed")}
        onRetry={() => void detail.refetch()}
      />
    );
  } else {
    body = <StatementDetailBody statement={detail.data} />;
  }

  return (
    <Card role="region" aria-label={title} title={title}>
      {body}
    </Card>
  );
}

function StatementDetailBody({ statement }: { statement: StatementDetail }) {
  const { t } = useTranslation();
  const snapshot = statement.snapshot;

  const priorRows: PriorRow[] = [
    ...snapshot.prior_period.usage.map((usage) => ({
      key: `usage-${usage.usage_period}`,
      label: usage.usage_period,
      requests: usage.request_count,
      amount: usage.amount,
    })),
    { key: "rebill", label: t("statements.prior.rebill"), requests: null, amount: snapshot.prior_period.rebill },
    { key: "total", label: t("statements.prior.total"), requests: null, amount: snapshot.prior_period.total },
  ];
  const priorColumns: TableColumnsType<PriorRow> = [
    { key: "label", title: t("statements.prior.usagePeriod"), render: (_: unknown, row) => row.label },
    { key: "requests", title: t("statements.field.requests"), render: (_: unknown, row) => row.requests ?? EMPTY },
    { key: "amount", title: t("statements.prior.amount"), render: (_: unknown, row) => <Money amount={row.amount} /> },
  ];

  return (
    <>
      <Descriptions
        bordered
        size="small"
        column={1}
        style={{ marginBottom: 16 }}
        items={[
          { key: "number", label: t("statements.field.number"), children: statement.statement_number },
          { key: "customer", label: t("statements.field.customer"), children: statement.company_name },
          { key: "period", label: t("statements.field.period"), children: statement.period },
          { key: "status", label: t("statements.field.status"), children: <StatusTag status={statement.status} /> },
          {
            key: "finalized_at",
            label: t("statements.field.finalizedAt"),
            children: <DateTimeText value={statement.finalized_at} />,
          },
          {
            key: "cutoff_at",
            label: t("statements.field.cutoffAt"),
            children: <DateTimeText value={statement.cutoff_at} />,
          },
          {
            key: "generated_by",
            label: t("statements.field.generatedBy"),
            children: statement.generated_by_email ?? t("statements.generatedBySystem"),
          },
          {
            key: "ledger_high_sequence",
            label: t("statements.field.ledgerHighSequence"),
            children: statement.ledger_high_sequence,
          },
        ]}
      />

      <div role="region" aria-label={t("statements.balance.title")} style={{ marginBottom: 16 }}>
        <Typography.Title level={5}>{t("statements.balance.title")}</Typography.Title>
        <Descriptions
          bordered
          size="small"
          column={1}
          items={[
            {
              key: "opening",
              label: t("statements.balance.opening"),
              children: <Money amount={snapshot.opening_balance} />,
            },
            { key: "topup", label: t("statements.balance.topup"), children: <Money amount={snapshot.total_topup} /> },
            {
              key: "ai_usage",
              label: t("statements.balance.aiUsage"),
              children: <Money amount={snapshot.total_ai_usage} />,
            },
            {
              key: "adjustments",
              label: t("statements.balance.adjustments"),
              children: <Money amount={snapshot.total_adjustments} />,
            },
          ]}
        />
      </div>

      <div role="region" aria-label={t("statements.prior.title")} style={{ marginBottom: 16 }}>
        <Typography.Title level={5}>{t("statements.prior.title")}</Typography.Title>
        <Typography.Paragraph type="secondary">{t("statements.prior.note")}</Typography.Paragraph>
        <Table<PriorRow> size="small" rowKey="key" columns={priorColumns} dataSource={priorRows} pagination={false} />
      </div>

      <Descriptions
        bordered
        size="small"
        column={1}
        style={{ marginBottom: 16 }}
        items={[
          {
            key: "closing",
            label: t("statements.balance.closing"),
            children: <Money amount={snapshot.closing_balance} />,
          },
        ]}
      />

      <div role="region" aria-label={t("statements.usage.title")} style={{ marginBottom: 16 }}>
        <Typography.Title level={5}>{t("statements.usage.title")}</Typography.Title>
        <Descriptions
          bordered
          size="small"
          column={1}
          items={[
            { key: "requests", label: t("statements.field.requests"), children: snapshot.request_count },
            { key: "conversations", label: t("statements.usage.conversations"), children: snapshot.conversation_count },
            { key: "input_tokens", label: t("statements.field.inputTokens"), children: snapshot.input_tokens },
            { key: "output_tokens", label: t("statements.field.outputTokens"), children: snapshot.output_tokens },
            {
              key: "customer_charges",
              label: t("statements.usage.customerCharges"),
              children: <Money amount={snapshot.total_customer_charges} />,
            },
          ]}
        />
      </div>

      <BreakdownTable<ProviderUsage>
        title={t("statements.byProvider.title")}
        rows={snapshot.by_provider}
        rowKey={(row) => row.provider_code}
        columns={[
          { key: "provider_code", title: t("statements.field.provider"), render: (_: unknown, row) => row.provider_code },
          ...countColumns<ProviderUsage>(t),
        ]}
      />
      <BreakdownTable<ModelUsage>
        title={t("statements.byModel.title")}
        rows={snapshot.by_model}
        rowKey={(row) => `${row.provider_code}/${row.model_code}`}
        columns={[
          { key: "provider_code", title: t("statements.field.provider"), render: (_: unknown, row) => row.provider_code },
          { key: "model_code", title: t("statements.field.model"), render: (_: unknown, row) => row.model_code },
          ...countColumns<ModelUsage>(t),
        ]}
      />
      <BreakdownTable<ProjectUsage>
        title={t("statements.byProject.title")}
        rows={snapshot.by_project}
        rowKey={(row) => row.project_public_id}
        columns={[
          { key: "project_name", title: t("statements.field.project"), render: (_: unknown, row) => row.project_name },
          ...countColumns<ProjectUsage>(t),
        ]}
      />

      <div role="region" aria-label={t("statements.taxNotice.title")}>
        <Typography.Title level={5}>{t("statements.taxNotice.title")}</Typography.Title>
        {/* 快照里的原文，逐字显示：不改写、不拼接，换行照留。 */}
        <Typography.Paragraph style={{ whiteSpace: "pre-wrap", marginBottom: 0 }}>
          {snapshot.tax_notice.en}
        </Typography.Paragraph>
      </div>
    </>
  );
}

interface FilterValues {
  customer_id: string | undefined;
  period: string;
}

/** 一次补生成的结果，记着是哪个客户的：换了客户就不再显示。 */
interface GenerateOutcome {
  customerId: string;
  created: GeneratedStatement[] | null;
  error: Error | null;
}

/** 补生成成功的那一句：新生成了几份（0 份时说什么都没缺）。 */
function GeneratedText({ created }: { created: GeneratedStatement[] }) {
  const { t } = useTranslation();
  let text: string;
  if (created.length === 0) {
    text = t("statements.generate.none");
  } else if (created.length === 1) {
    text = t("statements.generate.doneOne");
  } else {
    text = t("statements.generate.done", { count: created.length });
  }
  return (
    <Typography.Paragraph type="success" style={{ marginBottom: 16 }}>
      {text}
    </Typography.Paragraph>
  );
}

export function StatementsPage() {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const customers = useAllCustomers();
  const [customerId, setCustomerId] = useState<string | undefined>(undefined);
  const [periodText, setPeriodText] = useState("");
  const [page, setPage] = useState(1);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [outcome, setOutcome] = useState<GenerateOutcome | null>(null);
  const inFlight = useRef(false);

  const period = periodText.trim();
  // 账期形状不对时不发请求，只在输入框下提示。空着就是不按账期筛。
  const periodValid = period === "" || PERIOD_PATTERN.test(period);
  const filter: StatementFilter = {
    ...(customerId === undefined ? {} : { customer_id: customerId }),
    ...(period === "" ? {} : { period }),
  };

  const statements = useQuery({
    queryKey: statementListQueryKey(filter, page, DEFAULT_PAGE_SIZE),
    queryFn: ({ signal }) => listStatements(filter, page, DEFAULT_PAGE_SIZE, signal),
    enabled: periodValid,
    // 翻页时先留着上一页，不让整张表闪成骨架屏。
    placeholderData: keepPreviousData,
  });

  const generate = useMutation({
    mutationFn: (id: string) => generateStatements(id),
    onSuccess: (created, id) => setOutcome({ customerId: id, created, error: null }),
    onError: (error, id) => setOutcome({ customerId: id, created: null, error }),
    onSettled: () => {
      inFlight.current = false;
      // 成败都刷新：失败之前已生成的几期照常保留，刷新列表才看得到。
      void queryClient.invalidateQueries({ queryKey: STATEMENT_LISTS_QUERY_KEY });
    },
  });

  const startGenerate = (): void => {
    if (customerId === undefined || inFlight.current) {
      return;
    }
    inFlight.current = true;
    setOutcome(null);
    generate.mutate(customerId);
  };

  const changeFilter = (changed: Partial<FilterValues>): void => {
    if ("customer_id" in changed) {
      setCustomerId(changed.customer_id);
      setOutcome(null);
    }
    if ("period" in changed) {
      setPeriodText(changed.period ?? "");
    }
    setPage(1);
    setSelectedId(null);
  };

  const goTo = (nextPage: number): void => {
    setPage(nextPage);
    setSelectedId(null);
  };

  const toggle = (statement: StatementSummary): void => {
    setSelectedId((current) => (current === statement.statement_id ? null : statement.statement_id));
  };

  const columns: TableColumnsType<StatementSummary> = [
    { key: "statement_number", title: t("statements.field.number"), dataIndex: "statement_number" },
    { key: "company_name", title: t("statements.field.customer"), dataIndex: "company_name" },
    { key: "period", title: t("statements.field.period"), dataIndex: "period" },
    {
      key: "status",
      title: t("statements.field.status"),
      render: (_: unknown, statement) => <StatusTag status={statement.status} />,
    },
    {
      key: "closing_balance",
      title: t("statements.field.closingBalance"),
      render: (_: unknown, statement) => <Money amount={statement.closing_balance} />,
    },
    {
      key: "finalized_at",
      title: t("statements.field.finalizedAt"),
      render: (_: unknown, statement) => <DateTimeText value={statement.finalized_at} />,
    },
  ];

  const filtered = Object.keys(filter).length > 0;

  let result: ReactNode;
  if (!periodValid) {
    // 不发请求：输入框下面已经说了格式。
    result = null;
  } else if (statements.isPending) {
    result = <Loading text={t("statements.list.loading")} />;
  } else if (statements.isError) {
    result = (
      <StatementErrorAlert
        error={statements.error}
        failedText={t("statements.list.loadFailed")}
        onRetry={() => void statements.refetch()}
      />
    );
  } else if (statements.data.total === 0) {
    result = <Empty description={filtered ? t("statements.list.emptyFiltered") : t("statements.list.empty")} />;
  } else {
    const total = statements.data.total;
    result = (
      <>
        <Table<StatementSummary>
          rowKey="statement_id"
          columns={columns}
          dataSource={statements.data.items}
          loading={statements.isFetching}
          pagination={false}
          // 页码超过末页时后端回空 items、total 照报：说清楚是「这一页没有」。
          locale={{ emptyText: t("statements.list.emptyPage") }}
          onRow={(statement) => ({
            onClick: () => toggle(statement),
            onKeyDown: (event) => {
              if (event.key === "Enter" || event.key === " ") {
                event.preventDefault();
                toggle(statement);
              }
            },
            tabIndex: 0,
            "aria-selected": selectedId === statement.statement_id,
            style: { cursor: "pointer" },
          })}
        />
        <Space style={{ marginTop: 16 }}>
          <Button disabled={page <= 1} onClick={() => goTo(page - 1)}>
            {t("statements.list.previous")}
          </Button>
          <Typography.Text type="secondary">{t("statements.list.pageInfo", { page, total })}</Typography.Text>
          <Button disabled={page * DEFAULT_PAGE_SIZE >= total} onClick={() => goTo(page + 1)}>
            {t("statements.list.next")}
          </Button>
        </Space>
      </>
    );
  }

  const showOutcome = outcome !== null && outcome.customerId === customerId;

  return (
    <Space direction="vertical" size="large" style={{ width: "100%" }}>
      <Card title={t("statements.title")}>
        <Typography.Paragraph type="secondary">{t("statements.intro")}</Typography.Paragraph>
        <Typography.Paragraph type="secondary">{t("statements.closingNote")}</Typography.Paragraph>
        {customers.isError ? (
          <StatementErrorAlert
            error={customers.error}
            failedText={t("statements.filter.customersFailed")}
            onRetry={() => void customers.refetch()}
          />
        ) : null}
        <Form<FilterValues>
          name="statement-filter"
          layout="inline"
          style={{ marginBottom: 16, rowGap: 8 }}
          initialValues={{ customer_id: undefined, period: "" }}
          onValuesChange={changeFilter}
        >
          <Form.Item name="customer_id" label={t("statements.field.customer")}>
            <Select
              allowClear
              showSearch
              optionFilterProp="label"
              style={{ minWidth: 240 }}
              placeholder={t("statements.filter.anyCustomer")}
              loading={customers.isPending}
              options={customerOptions(customers.data)}
            />
          </Form.Item>
          <Form.Item
            name="period"
            label={t("statements.field.period")}
            extra={periodValid ? t("statements.filter.periodHint") : undefined}
            validateStatus={periodValid ? "" : "error"}
            help={periodValid ? undefined : t("statements.filter.periodInvalid")}
          >
            <Input
              allowClear
              autoComplete="off"
              spellCheck={false}
              style={{ width: 140 }}
              placeholder={t("statements.filter.periodPlaceholder")}
            />
          </Form.Item>
          {customerId === undefined ? null : (
            <Form.Item>
              <Button loading={generate.isPending} disabled={generate.isPending} onClick={startGenerate}>
                {t("statements.generate.button")}
              </Button>
            </Form.Item>
          )}
        </Form>
        {showOutcome && outcome.created !== null ? <GeneratedText created={outcome.created} /> : null}
        {showOutcome && outcome.error !== null ? (
          <StatementErrorAlert error={outcome.error} failedText={t("statements.generate.failed")} />
        ) : null}
        {/* 列表与它的状态单独成一块，用例与验收脚本按 aria-label 找它，不与下面的详情混在一起。 */}
        <div role="region" aria-label={t("statements.list.title")}>
          {result}
        </div>
        {periodValid && selectedId === null && statements.isSuccess && statements.data.total > 0 ? (
          <Typography.Paragraph type="secondary" style={{ marginTop: 16, marginBottom: 0 }}>
            {t("statements.list.select")}
          </Typography.Paragraph>
        ) : null}
      </Card>
      {selectedId === null ? null : <StatementDetailCard key={selectedId} statementId={selectedId} />}
    </Space>
  );
}

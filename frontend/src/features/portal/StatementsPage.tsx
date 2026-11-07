/**
 * 客户门户对账单页（`/portal/statements`，AIH-TASK-162；设计闸门 #565 v6 §2「前端」）。
 *
 * 语义以 docs/api.md「月度对账单（客户）」为准：上面是自己已定稿的对账单列表（`GET /api/v1/customer/statements`），
 * 每页 12 条，只有上一页 / 下一页（按 `total` 判断能不能翻）；顺序由后端决定（账期倒序），前端不重排。点一行在列表
 * 下面显示这份对账单的详情（`GET …/{statement_id}`）：不开弹窗、不跳子路由；再点同一行收起，点另一行换成那一行，
 * 翻页时清掉已选的详情。详情读失败只影响详情那一块，列表照常可用。
 *
 * 金额按门户的显示规则：币种在前、2 位小数（`formatAmountRounded`，与钱包页的余额同一写法，`MYR 87.50`）。时间按
 * 吉隆坡时间（UTC+8）显示，列头标 MYT。快照里的 `tax_notice.en` 原样显示，不改写、不拼接。期末余额按 T+1 归属计算，
 * 不是月末那一刻的钱包余额 —— 页面上写明这一点。
 *
 * 三种状态都画出来（spec §132 DoD 第 8 条）：加载中、失败（带 request_id）、空列表（说明对账单在每月结束后的第 2 日
 * 生成）。
 *
 * ⚠️ 只请求 `/api/v1/customer/*`：不请求任何 `/api/v1/admin/` 路径，不显示成本、毛利或内部字段（INV-7）。说明文字
 * 不用 antd 的 `Alert`：它带 `role="alert"`，部署后浏览器验收把它当成错误提示（scripts/acceptance/admin_customers.mjs）。
 */

import { keepPreviousData, useQuery } from "@tanstack/react-query";
import {
  Alert,
  Button,
  Card,
  Descriptions,
  Empty,
  Skeleton,
  Space,
  Table,
  Typography,
  type TableColumnsType,
} from "antd";
import { useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";

import { ApiError } from "../../api/client";
import {
  STATEMENT_CURRENCY,
  STATEMENT_NOT_FOUND,
  customerStatementDetailQueryKey,
  customerStatementListQueryKey,
  getCustomerStatement,
  listCustomerStatements,
  type CustomerStatementDetail,
  type CustomerStatementSummary,
  type ModelUsage,
  type ProjectUsage,
  type ProviderUsage,
  type UsageCounts,
} from "../../api/customerStatements";
import { DateTimeText } from "../../components/DateTimeText";
import { formatAmountRounded } from "../../components/MoneyText";
import { RequestReference } from "../../components/RequestReference";

/** 每页条数（设计 §2「前端」）。 */
export const STATEMENTS_PAGE_SIZE = 12;

/** 金额的显示位数：与钱包页的余额相同（spec §80「MYR normal display → 2 decimals」）。 */
const DISPLAY_PLACES = 2;

/** 没有值的格子。纯标点，不进翻译文件。 */
const EMPTY = "—";

type TFunction = ReturnType<typeof useTranslation>["t"];

/** 币种在前：`MYR 87.50`。写法与钱包页的余额相同。 */
function Money({ amount }: { amount: string }) {
  return (
    <span style={{ fontVariantNumeric: "tabular-nums" }}>
      {`${STATEMENT_CURRENCY} ${formatAmountRounded(amount, DISPLAY_PLACES)}`}
    </span>
  );
}

function Loading({ text }: { text: string }) {
  return (
    <Space direction="vertical" aria-busy="true" style={{ width: "100%" }}>
      <Skeleton active title={false} paragraph={{ rows: 3 }} />
      <Typography.Text type="secondary">{text}</Typography.Text>
    </Space>
  );
}

/** 明细表共有的四列：请求数、输入 / 输出 token、计费。 */
function countColumns<T extends UsageCounts>(t: TFunction): TableColumnsType<T> {
  return [
    {
      key: "request_count",
      title: t("portal.statements.usage.requests"),
      render: (_: unknown, row) => row.request_count,
    },
    {
      key: "input_tokens",
      title: t("portal.statements.usage.inputTokens"),
      render: (_: unknown, row) => row.input_tokens,
    },
    {
      key: "output_tokens",
      title: t("portal.statements.usage.outputTokens"),
      render: (_: unknown, row) => row.output_tokens,
    },
    {
      key: "charges",
      title: t("portal.statements.field.charges"),
      render: (_: unknown, row) => <Money amount={row.charges} />,
    },
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
        locale={{ emptyText: t("portal.statements.detail.noUsage") }}
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
    queryKey: customerStatementDetailQueryKey(statementId),
    queryFn: ({ signal }) => getCustomerStatement(statementId, signal),
    // 理由同钱包页：同一个标签页换账号登录，拿到的必须是新客户的对账单。
    gcTime: 0,
  });

  const title = t("portal.statements.detail.title");
  let body: ReactNode;
  if (detail.isPending) {
    body = <Loading text={t("portal.statements.detail.loading")} />;
  } else if (detail.isError) {
    const notFound = detail.error instanceof ApiError && detail.error.code === STATEMENT_NOT_FOUND;
    body = (
      <Alert
        type="error"
        showIcon
        message={notFound ? t("portal.statements.detail.notFound") : t("portal.statements.detail.loadFailed")}
        description={<RequestReference error={detail.error} />}
        action={
          // 不存在重试也一样，不给按钮。
          notFound ? undefined : (
            <Button size="small" onClick={() => void detail.refetch()}>
              {t("portal.statements.retry")}
            </Button>
          )
        }
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

function StatementDetailBody({ statement }: { statement: CustomerStatementDetail }) {
  const { t } = useTranslation();
  const snapshot = statement.snapshot;

  const priorRows: PriorRow[] = [
    ...snapshot.prior_period.usage.map((usage) => ({
      key: `usage-${usage.usage_period}`,
      label: usage.usage_period,
      requests: usage.request_count,
      amount: usage.amount,
    })),
    {
      key: "rebill",
      label: t("portal.statements.prior.rebill"),
      requests: null,
      amount: snapshot.prior_period.rebill,
    },
    {
      key: "total",
      label: t("portal.statements.prior.total"),
      requests: null,
      amount: snapshot.prior_period.total,
    },
  ];
  const priorColumns: TableColumnsType<PriorRow> = [
    { key: "label", title: t("portal.statements.prior.usagePeriod"), render: (_: unknown, row) => row.label },
    {
      key: "requests",
      title: t("portal.statements.usage.requests"),
      render: (_: unknown, row) => row.requests ?? EMPTY,
    },
    {
      key: "amount",
      title: t("portal.statements.prior.amount"),
      render: (_: unknown, row) => <Money amount={row.amount} />,
    },
  ];

  return (
    <>
      <Descriptions
        bordered
        size="small"
        column={1}
        style={{ marginBottom: 16 }}
        items={[
          { key: "number", label: t("portal.statements.field.number"), children: statement.statement_number },
          { key: "company", label: t("portal.statements.field.company"), children: snapshot.company_name },
          { key: "period", label: t("portal.statements.field.period"), children: statement.period },
          {
            key: "finalized_at",
            label: t("portal.statements.field.finalizedAt"),
            children: <DateTimeText value={statement.finalized_at} />,
          },
        ]}
      />

      <div role="region" aria-label={t("portal.statements.balance.title")} style={{ marginBottom: 16 }}>
        <Typography.Title level={5}>{t("portal.statements.balance.title")}</Typography.Title>
        <Descriptions
          bordered
          size="small"
          column={1}
          items={[
            {
              key: "opening",
              label: t("portal.statements.balance.opening"),
              children: <Money amount={snapshot.opening_balance} />,
            },
            {
              key: "topup",
              label: t("portal.statements.balance.topup"),
              children: <Money amount={snapshot.total_topup} />,
            },
            {
              key: "ai_usage",
              label: t("portal.statements.balance.aiUsage"),
              children: <Money amount={snapshot.total_ai_usage} />,
            },
            {
              key: "adjustments",
              label: t("portal.statements.balance.adjustments"),
              children: <Money amount={snapshot.total_adjustments} />,
            },
          ]}
        />
      </div>

      <div role="region" aria-label={t("portal.statements.prior.title")} style={{ marginBottom: 16 }}>
        <Typography.Title level={5}>{t("portal.statements.prior.title")}</Typography.Title>
        <Typography.Paragraph type="secondary">{t("portal.statements.prior.note")}</Typography.Paragraph>
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
            label: t("portal.statements.balance.closing"),
            children: <Money amount={snapshot.closing_balance} />,
          },
        ]}
      />

      <div role="region" aria-label={t("portal.statements.usage.title")} style={{ marginBottom: 16 }}>
        <Typography.Title level={5}>{t("portal.statements.usage.title")}</Typography.Title>
        <Descriptions
          bordered
          size="small"
          column={1}
          items={[
            { key: "requests", label: t("portal.statements.usage.requests"), children: snapshot.request_count },
            {
              key: "conversations",
              label: t("portal.statements.usage.conversations"),
              children: snapshot.conversation_count,
            },
            { key: "input_tokens", label: t("portal.statements.usage.inputTokens"), children: snapshot.input_tokens },
            {
              key: "output_tokens",
              label: t("portal.statements.usage.outputTokens"),
              children: snapshot.output_tokens,
            },
            {
              key: "customer_charges",
              label: t("portal.statements.usage.customerCharges"),
              children: <Money amount={snapshot.total_customer_charges} />,
            },
          ]}
        />
      </div>

      <BreakdownTable<ProviderUsage>
        title={t("portal.statements.byProvider.title")}
        rows={snapshot.by_provider}
        rowKey={(row) => row.provider_code}
        columns={[
          {
            key: "provider_code",
            title: t("portal.statements.field.provider"),
            render: (_: unknown, row) => row.provider_code,
          },
          ...countColumns<ProviderUsage>(t),
        ]}
      />
      <BreakdownTable<ModelUsage>
        title={t("portal.statements.byModel.title")}
        rows={snapshot.by_model}
        rowKey={(row) => `${row.provider_code}/${row.model_code}`}
        columns={[
          {
            key: "provider_code",
            title: t("portal.statements.field.provider"),
            render: (_: unknown, row) => row.provider_code,
          },
          { key: "model_code", title: t("portal.statements.field.model"), render: (_: unknown, row) => row.model_code },
          ...countColumns<ModelUsage>(t),
        ]}
      />
      {/* 项目只显示生成时的项目名；`project_public_id` 只当行 key，不显示。 */}
      <BreakdownTable<ProjectUsage>
        title={t("portal.statements.byProject.title")}
        rows={snapshot.by_project}
        rowKey={(row) => row.project_public_id}
        columns={[
          {
            key: "project_name",
            title: t("portal.statements.field.project"),
            render: (_: unknown, row) => row.project_name,
          },
          ...countColumns<ProjectUsage>(t),
        ]}
      />

      <div role="region" aria-label={t("portal.statements.taxNotice.title")}>
        <Typography.Title level={5}>{t("portal.statements.taxNotice.title")}</Typography.Title>
        {/* 快照里的原文，逐字显示：不改写、不拼接，换行照留。 */}
        <Typography.Paragraph style={{ whiteSpace: "pre-wrap", marginBottom: 0 }}>
          {snapshot.tax_notice.en}
        </Typography.Paragraph>
      </div>
    </>
  );
}

export function StatementsPage() {
  const { t } = useTranslation();
  const [page, setPage] = useState(1);
  const [selectedId, setSelectedId] = useState<string | null>(null);

  const statements = useQuery({
    queryKey: customerStatementListQueryKey(page, STATEMENTS_PAGE_SIZE),
    queryFn: ({ signal }) => listCustomerStatements(page, STATEMENTS_PAGE_SIZE, signal),
    // 翻页时先留着上一页，不让整张表闪成骨架屏。
    placeholderData: keepPreviousData,
    // 理由同钱包页：同一个标签页换账号登录，拿到的必须是新客户的对账单。
    gcTime: 0,
  });

  const goTo = (nextPage: number): void => {
    setPage(nextPage);
    setSelectedId(null);
  };

  const toggle = (statement: CustomerStatementSummary): void => {
    setSelectedId((current) => (current === statement.statement_id ? null : statement.statement_id));
  };

  const columns: TableColumnsType<CustomerStatementSummary> = [
    { key: "statement_number", title: t("portal.statements.field.number"), dataIndex: "statement_number" },
    { key: "period", title: t("portal.statements.field.period"), dataIndex: "period" },
    {
      key: "closing_balance",
      title: t("portal.statements.field.closingBalance"),
      render: (_: unknown, statement) => <Money amount={statement.closing_balance} />,
    },
    {
      key: "finalized_at",
      title: t("portal.statements.field.finalizedAt"),
      render: (_: unknown, statement) => <DateTimeText value={statement.finalized_at} />,
    },
  ];

  let result: ReactNode;
  if (statements.isPending) {
    result = <Loading text={t("portal.statements.loading")} />;
  } else if (statements.isError) {
    result = (
      <Alert
        type="error"
        showIcon
        message={t("portal.statements.loadFailed")}
        description={<RequestReference error={statements.error} />}
        action={
          <Button size="small" onClick={() => void statements.refetch()}>
            {t("portal.statements.retry")}
          </Button>
        }
      />
    );
  } else if (statements.data.total === 0) {
    result = <Empty description={t("portal.statements.empty")} />;
  } else {
    const total = statements.data.total;
    result = (
      <>
        <Table<CustomerStatementSummary>
          rowKey="statement_id"
          columns={columns}
          dataSource={statements.data.items}
          loading={statements.isFetching}
          pagination={false}
          // 页码超过末页时后端回空 items、total 照报：说清楚是「这一页没有」。
          locale={{ emptyText: t("portal.statements.emptyPage") }}
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
            {t("portal.statements.previous")}
          </Button>
          <Typography.Text type="secondary">{t("portal.statements.pageInfo", { page, total })}</Typography.Text>
          <Button disabled={page * STATEMENTS_PAGE_SIZE >= total} onClick={() => goTo(page + 1)}>
            {t("portal.statements.next")}
          </Button>
        </Space>
      </>
    );
  }

  return (
    <Space direction="vertical" size="large" style={{ width: "100%" }}>
      <Card title={t("portal.statements.title")}>
        <Typography.Paragraph type="secondary">{t("portal.statements.intro")}</Typography.Paragraph>
        <Typography.Paragraph type="secondary">{t("portal.statements.closingNote")}</Typography.Paragraph>
        {/* 列表与它的状态单独成一块，用例与验收脚本按 aria-label 找它，不与下面的详情混在一起。 */}
        <div role="region" aria-label={t("portal.statements.listTitle")}>
          {result}
        </div>
      </Card>
      {selectedId === null ? null : <StatementDetailCard key={selectedId} statementId={selectedId} />}
    </Space>
  );
}

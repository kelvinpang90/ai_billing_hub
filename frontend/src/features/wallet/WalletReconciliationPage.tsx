/**
 * 管理端钱包核对页（`/wallet-reconciliation`，AIH-TASK-135；设计闸门 #474 v13 §2「前端」）。
 *
 * 语义以 docs/api.md「钱包核对」为准：上面是核对运行表（`GET /api/v1/admin/wallet-reconciliation/runs`），点一行在
 * 下面显示这次运行的不一致钱包（`GET …/runs/{run_id}/findings`）。两张表各自分页，页码只放在组件状态里；换一次运行，
 * 不一致表回到第 1 页。顺序都由后端决定，前端不重排。
 *
 * ⚠️ **只读**：这一页没有任何修复或写操作入口。不一致按数据事故处理（docs/runbook.md），不在这里改钱包或账本。
 *
 * 时间按吉隆坡时间（UTC+8）显示，列头标 MYT。余额与客户详情页同一个金额组件 `MoneyText`；findings 不带币种，
 * 按 V1 恒定的钱包币种显示。钱包不存在时余额与版本为 `null`，说「没有钱包」/ 「—」，不猜一个 0。
 *
 * 三种状态都画出来（spec §132 DoD 第 8 条）：加载中、失败（带 request_id）、空状态（还没有核对运行 / 这次运行
 * 全部一致）。403 说「没有权限」。只请求 `/api/v1/admin/` 路径。
 */

import { keepPreviousData, useQuery } from "@tanstack/react-query";
import {
  Alert,
  Button,
  Card,
  Empty,
  Skeleton,
  Space,
  Table,
  Tag,
  Typography,
  type TableColumnsType,
} from "antd";
import { useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router";

import { ADMIN_REQUIRED, DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE } from "../../api/adminCustomers";
import {
  RECONCILIATION_RUN_NOT_FOUND,
  WALLET_CURRENCY,
  listReconciliationFindings,
  listReconciliationRuns,
  reconciliationFindingsQueryKey,
  reconciliationRunsQueryKey,
  type ReconciliationFinding,
  type ReconciliationRun,
} from "../../api/adminWalletReconciliation";
import { ApiError } from "../../api/client";
import { DateTimeText } from "../../components/DateTimeText";
import { MoneyText } from "../../components/MoneyText";
import { RequestReference } from "../../components/RequestReference";
import { customerDetailPath } from "../../routes/paths";

const PAGE_SIZE_OPTIONS = [10, 20, 50, MAX_PAGE_SIZE];

/** 没有值的格子。纯标点，不进翻译文件。 */
const EMPTY = "—";

/** 运行状态的文案；认不出的值原样显示。 */
function RunStatusText({ status }: { status: string }) {
  const { t } = useTranslation();
  switch (status) {
    case "RUNNING":
      return <Tag color="processing">{t("walletReconciliation.status.running")}</Tag>;
    case "COMPLETED":
      return <Tag color="success">{t("walletReconciliation.status.completed")}</Tag>;
    case "ABANDONED":
      return <Tag color="warning">{t("walletReconciliation.status.abandoned")}</Tag>;
    default:
      return <Tag>{status}</Tag>;
  }
}

/** 问题码的一句说明（docs/api.md「钱包核对」的问题码表）；认不出的码没有说明。 */
function useProblemDescription(): (code: string) => string | null {
  const { t } = useTranslation();
  return (code) => {
    switch (code) {
      case "WALLET_MISSING":
        return t("walletReconciliation.problem.walletMissing");
      case "LEDGER_MISSING":
        return t("walletReconciliation.problem.ledgerMissing");
      case "BALANCE_NOT_LEDGER_SUM":
        return t("walletReconciliation.problem.balanceNotLedgerSum");
      case "BALANCE_NOT_LAST_BALANCE_AFTER":
        return t("walletReconciliation.problem.balanceNotLastBalanceAfter");
      case "VERSION_NOT_LAST_SEQUENCE":
        return t("walletReconciliation.problem.versionNotLastSequence");
      case "SEQUENCE_GAP":
        return t("walletReconciliation.problem.sequenceGap");
      case "CHAIN_BROKEN":
        return t("walletReconciliation.problem.chainBroken");
      case "ROW_ARITHMETIC_BROKEN":
        return t("walletReconciliation.problem.rowArithmeticBroken");
      case "BILLING_STATUS_MISMATCH":
        return t("walletReconciliation.problem.billingStatusMismatch");
      default:
        return null;
    }
  };
}

/** 问题码逐个显示为标签，后面跟一句说明。按后端给的顺序（判定顺序）。 */
function ProblemList({ codes }: { codes: string[] }) {
  const describe = useProblemDescription();
  return (
    <ul style={{ margin: 0, paddingLeft: 0, listStyle: "none" }}>
      {codes.map((code, index) => {
        const description = describe(code);
        return (
          <li key={`${index}-${code}`} style={{ marginBottom: 4 }}>
            <Tag color="error">{code}</Tag>
            {description !== null && <Typography.Text type="secondary">{description}</Typography.Text>}
          </li>
        );
      })}
    </ul>
  );
}

/** 403 说没有权限；不一致表上 404 说这次运行不存在；其余是加载失败，可重试。都带 request_id。 */
function ReconciliationErrorAlert({
  error,
  failedText,
  onRetry,
}: {
  error: Error;
  failedText: string;
  onRetry: () => void;
}) {
  const { t } = useTranslation();
  const code = error instanceof ApiError ? error.code : null;
  const forbidden = code === ADMIN_REQUIRED;
  const notFound = code === RECONCILIATION_RUN_NOT_FOUND;
  let title = failedText;
  if (forbidden) {
    title = t("walletReconciliation.forbidden");
  } else if (notFound) {
    title = t("walletReconciliation.findings.notFound");
  }
  return (
    <Alert
      type={forbidden ? "warning" : "error"}
      showIcon
      message={title}
      description={<RequestReference error={error} />}
      action={
        forbidden || notFound ? undefined : (
          <Button size="small" onClick={onRetry}>
            {t("walletReconciliation.retry")}
          </Button>
        )
      }
    />
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

/** 选中的那一次运行的不一致钱包。换运行时由调用方用 `key` 重建，页码回到第 1 页。 */
function FindingsCard({ run }: { run: ReconciliationRun }) {
  const { t } = useTranslation();
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(DEFAULT_PAGE_SIZE);

  const findings = useQuery({
    queryKey: reconciliationFindingsQueryKey(run.run_id, page, pageSize),
    queryFn: ({ signal }) => listReconciliationFindings(run.run_id, page, pageSize, signal),
    placeholderData: keepPreviousData,
  });

  const goTo = (nextPage: number, nextPageSize: number): void => {
    setPage(nextPageSize === pageSize ? nextPage : 1);
    setPageSize(nextPageSize);
  };

  const columns: TableColumnsType<ReconciliationFinding> = [
    {
      key: "customer",
      title: t("walletReconciliation.findings.field.customer"),
      render: (_: unknown, finding) => (
        <Link to={customerDetailPath(finding.customer_id)}>{finding.company_name}</Link>
      ),
    },
    {
      key: "problems",
      title: t("walletReconciliation.findings.field.problems"),
      render: (_: unknown, finding) => <ProblemList codes={finding.problem_codes} />,
    },
    {
      key: "balance",
      title: t("walletReconciliation.findings.field.balance"),
      render: (_: unknown, finding) =>
        finding.balance === null ? (
          <Typography.Text type="secondary">{t("walletReconciliation.findings.noWallet")}</Typography.Text>
        ) : (
          <MoneyText amount={finding.balance} currency={WALLET_CURRENCY} />
        ),
    },
    {
      key: "wallet_version",
      title: t("walletReconciliation.findings.field.walletVersion"),
      render: (_: unknown, finding) => finding.wallet_version ?? EMPTY,
    },
    {
      key: "ledger_rows",
      title: t("walletReconciliation.findings.field.ledgerRows"),
      dataIndex: "ledger_rows",
    },
    {
      key: "checked_at",
      title: t("walletReconciliation.findings.field.checkedAt"),
      render: (_: unknown, finding) => <DateTimeText value={finding.checked_at} />,
    },
  ];

  let body: ReactNode;
  if (findings.isPending) {
    body = <Loading text={t("walletReconciliation.findings.loading")} />;
  } else if (findings.isError) {
    body = (
      <ReconciliationErrorAlert
        error={findings.error}
        failedText={t("walletReconciliation.findings.loadFailed")}
        onRetry={() => void findings.refetch()}
      />
    );
  } else if (findings.data.total === 0) {
    // 一致的钱包不记录：没有记录就是核对过的全部一致。没跑完的运行只能说「已核对的部分」。
    body = (
      <Empty
        description={
          run.status === "COMPLETED"
            ? t("walletReconciliation.findings.empty")
            : t("walletReconciliation.findings.emptyUnfinished")
        }
      />
    );
  } else {
    body = (
      <Table<ReconciliationFinding>
        rowKey="customer_id"
        columns={columns}
        dataSource={findings.data.items}
        loading={findings.isFetching}
        locale={{ emptyText: t("walletReconciliation.findings.emptyPage") }}
        pagination={{
          current: page,
          pageSize,
          total: findings.data.total,
          showSizeChanger: true,
          pageSizeOptions: PAGE_SIZE_OPTIONS,
          showTotal: (count) => t("walletReconciliation.findings.total", { total: count }),
          onChange: goTo,
        }}
      />
    );
  }

  const title = t("walletReconciliation.findings.title", { date: run.run_date });
  return (
    <Card role="region" aria-label={title} title={title} extra={<RunStatusText status={run.status} />}>
      {body}
    </Card>
  );
}

export function WalletReconciliationPage() {
  const { t } = useTranslation();
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(DEFAULT_PAGE_SIZE);
  const [selected, setSelected] = useState<ReconciliationRun | null>(null);

  const runs = useQuery({
    queryKey: reconciliationRunsQueryKey(page, pageSize),
    queryFn: ({ signal }) => listReconciliationRuns(page, pageSize, signal),
    // 翻页时先留着上一份，不让整张表闪成骨架屏。
    placeholderData: keepPreviousData,
  });

  const goTo = (nextPage: number, nextPageSize: number): void => {
    // 换了每页条数，原来的页码就没有意义了，回到第一页。
    setPage(nextPageSize === pageSize ? nextPage : 1);
    setPageSize(nextPageSize);
  };

  const columns: TableColumnsType<ReconciliationRun> = [
    { key: "run_date", title: t("walletReconciliation.runs.field.runDate"), dataIndex: "run_date" },
    {
      key: "status",
      title: t("walletReconciliation.runs.field.status"),
      render: (_: unknown, run) => <RunStatusText status={run.status} />,
    },
    {
      key: "started_at",
      title: t("walletReconciliation.runs.field.startedAt"),
      render: (_: unknown, run) => <DateTimeText value={run.started_at} />,
    },
    {
      key: "finished_at",
      title: t("walletReconciliation.runs.field.finishedAt"),
      render: (_: unknown, run) => (run.finished_at === null ? EMPTY : <DateTimeText value={run.finished_at} />),
    },
    {
      key: "wallets_checked",
      title: t("walletReconciliation.runs.field.walletsChecked"),
      dataIndex: "wallets_checked",
    },
    {
      key: "wallets_inconsistent",
      title: t("walletReconciliation.runs.field.walletsInconsistent"),
      render: (_: unknown, run) =>
        run.wallets_inconsistent > 0 ? (
          <Typography.Text type="danger" strong>
            {run.wallets_inconsistent}
          </Typography.Text>
        ) : (
          run.wallets_inconsistent
        ),
    },
  ];

  let result: ReactNode;
  if (runs.isPending) {
    result = <Loading text={t("walletReconciliation.runs.loading")} />;
  } else if (runs.isError) {
    result = (
      <ReconciliationErrorAlert
        error={runs.error}
        failedText={t("walletReconciliation.runs.loadFailed")}
        onRetry={() => void runs.refetch()}
      />
    );
  } else if (runs.data.total === 0) {
    result = <Empty description={t("walletReconciliation.runs.empty")} />;
  } else {
    result = (
      <Table<ReconciliationRun>
        rowKey="run_id"
        columns={columns}
        dataSource={runs.data.items}
        loading={runs.isFetching}
        // 页码超过末页时后端回空 items、total 照报：说清楚是「这一页没有」。
        locale={{ emptyText: t("walletReconciliation.runs.emptyPage") }}
        onRow={(run) => ({
          onClick: () => setSelected(run),
          onKeyDown: (event) => {
            if (event.key === "Enter" || event.key === " ") {
              event.preventDefault();
              setSelected(run);
            }
          },
          tabIndex: 0,
          "aria-selected": selected?.run_id === run.run_id,
          style: { cursor: "pointer" },
        })}
        pagination={{
          current: page,
          pageSize,
          total: runs.data.total,
          showSizeChanger: true,
          pageSizeOptions: PAGE_SIZE_OPTIONS,
          showTotal: (count) => t("walletReconciliation.runs.total", { total: count }),
          onChange: goTo,
        }}
      />
    );
  }

  return (
    <Space direction="vertical" size="large" style={{ width: "100%" }}>
      <Card title={t("walletReconciliation.title")}>
        <Typography.Paragraph type="secondary">{t("walletReconciliation.note")}</Typography.Paragraph>
        <Typography.Title level={5}>{t("walletReconciliation.runs.title")}</Typography.Title>
        {/* 运行表与它的状态单独成一块，用例与验收脚本按 aria-label 找它，不与下面的不一致表混在一起。 */}
        <div role="region" aria-label={t("walletReconciliation.runs.title")}>
          {result}
        </div>
        {selected === null && runs.isSuccess && runs.data.total > 0 && (
          <Typography.Paragraph type="secondary" style={{ marginTop: 16, marginBottom: 0 }}>
            {t("walletReconciliation.runs.select")}
          </Typography.Paragraph>
        )}
      </Card>
      {selected !== null && <FindingsCard key={selected.run_id} run={selected} />}
    </Space>
  );
}

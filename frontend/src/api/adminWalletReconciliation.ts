/**
 * 管理端钱包核对（契约见 docs/api.md「钱包核对」，AIH-TASK-134；设计闸门 #474 v13 §2）。
 *
 * 两个只读接口：核对运行的分页与某一次运行里不一致钱包的分页。要 ADMIN，角色以后端数据库为准 ——
 * 前端不做任何角色判断，403 原样交给页面显示。查看本身不写审计、不写任何行。
 *
 * ⚠️ 余额是**字符串**（INV-10），这一层不解析。`run_id` 只来自运行列表的响应，**原样**放进路径：不校验形状、
 * 不改大小写 —— 不是小写规范 UUID 或没有这次运行时后端回 404 `RECONCILIATION_RUN_NOT_FOUND`，那才是准绳。
 */

import type { Page } from "./adminCustomers";
import { apiGet } from "./client";

const RUNS_URL = "/api/v1/admin/wallet-reconciliation/runs";

/** 不一致记录：`run_id` 不是小写规范 UUID，或没有这次运行（两种情形响应相同）。 */
export const RECONCILIATION_RUN_NOT_FOUND = "RECONCILIATION_RUN_NOT_FOUND";

/** findings 不带币种：V1 的钱包币种恒为 MYR，余额按它显示。 */
export const WALLET_CURRENCY = "MYR";

/** `RUNNING` 进行中；`COMPLETED` 已核对完开始前已有的全部租户；`ABANDONED` 当天没跑完、被收尾。 */
export type ReconciliationRunStatus = "RUNNING" | "COMPLETED" | "ABANDONED";

/**
 * 问题码，顺序即后端的判定顺序（docs/api.md「钱包核对」的问题码表）。`problem_codes` 按这个顺序排列。
 * 页面对每个码各有一条说明；后端以后加了新码而这里没跟上，页面照样显示码本身。
 */
export const PROBLEM_CODES = [
  "WALLET_MISSING",
  "LEDGER_MISSING",
  "BALANCE_NOT_LEDGER_SUM",
  "BALANCE_NOT_LAST_BALANCE_AFTER",
  "VERSION_NOT_LAST_SEQUENCE",
  "SEQUENCE_GAP",
  "CHAIN_BROKEN",
  "ROW_ARITHMETIC_BROKEN",
  "BILLING_STATUS_MISMATCH",
] as const;

export type ProblemCode = (typeof PROBLEM_CODES)[number];

/** 一次核对运行。时间是不带时区的 UTC，精确到秒（docs/api.md「时间」）。 */
export interface ReconciliationRun {
  run_id: string;
  /** 马来西亚日历日，`YYYY-MM-DD`（按 `started_at` 换算到 UTC+8）。 */
  run_date: string;
  status: ReconciliationRunStatus;
  started_at: string;
  /** `RUNNING` 时为 `null`。 */
  finished_at: string | null;
  /** 到目前为止核对过的钱包数；`RUNNING` / `ABANDONED` 时是已核对的部分。 */
  wallets_checked: number;
  /** 其中不一致的钱包数，等于这次运行的不一致记录条数。 */
  wallets_inconsistent: number;
}

/** 一次运行里一个不一致的钱包。一致的钱包不记录。 */
export interface ReconciliationFinding {
  /** 客户的 `id`（`public_id`），可直接用于客户详情。 */
  customer_id: string;
  /** 读取时的当前公司名。 */
  company_name: string;
  /** 至少一个，按判定顺序排列。 */
  problem_codes: string[];
  /** 核对那一刻的余额，恰好 8 位小数的字符串；钱包不存在（`WALLET_MISSING`）时为 `null`。 */
  balance: string | null;
  /** 核对那一刻的版本号；钱包不存在时为 `null`。 */
  wallet_version: number | null;
  /** 核对时读到的账本行数；钱包不存在时为 0。 */
  ledger_rows: number;
  checked_at: string;
}

export const WALLET_RECONCILIATION_QUERY_KEY = ["admin", "wallet-reconciliation"] as const;

export function reconciliationRunsQueryKey(page: number, pageSize: number) {
  return [...WALLET_RECONCILIATION_QUERY_KEY, "runs", page, pageSize] as const;
}

export function reconciliationFindingsQueryKey(runId: string, page: number, pageSize: number) {
  return [...WALLET_RECONCILIATION_QUERY_KEY, "findings", runId, page, pageSize] as const;
}

function pageQuery(page: number, pageSize: number): string {
  return new URLSearchParams({ page: String(page), page_size: String(pageSize) }).toString();
}

/** 核对运行分页，最新开始的在前（顺序由后端决定，前端不重排）。 */
export function listReconciliationRuns(
  page: number,
  pageSize: number,
  signal?: AbortSignal,
): Promise<Page<ReconciliationRun>> {
  return apiGet<Page<ReconciliationRun>>(`${RUNS_URL}?${pageQuery(page, pageSize)}`, signal);
}

/** 一次运行的不一致记录分页，按客户的创建顺序（顺序由后端决定，前端不重排）。 */
export function listReconciliationFindings(
  runId: string,
  page: number,
  pageSize: number,
  signal?: AbortSignal,
): Promise<Page<ReconciliationFinding>> {
  return apiGet<Page<ReconciliationFinding>>(
    `${RUNS_URL}/${runId}/findings?${pageQuery(page, pageSize)}`,
    signal,
  );
}

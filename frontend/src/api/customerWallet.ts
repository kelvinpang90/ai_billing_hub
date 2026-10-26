/**
 * 客户自己的钱包（`GET /api/v1/customer/wallet`、`GET /api/v1/customer/wallet/transactions`）。
 *
 * 语义以 docs/api.md「客户钱包」为准（AIH-TASK-099，设计闸门 #393 v3）。只读。
 *
 * ⚠️ **不带任何租户或客户标识**：客户是谁只取自访问令牌（spec §90）。这一层也不接受这类参数 —— 不给调用方
 * 留下「顺手带上 customer_id」的口子。
 *
 * 分页是**序号游标**，规则同管理端钱包流水：省略 `before_sequence` 是最新一页；更早的一页把响应的
 * `next_before_sequence` 原样作为 `before_sequence` 发回去，它为 `null` 时没有更早的行。
 *
 * ⚠️ 金额与余额是**字符串**（INV-10），类型刻意写成 `string`，不在这一层解析。
 *
 * 流水导出（`GET /api/v1/customer/wallet/transactions/export`，AIH-TASK-118；设计闸门 #435 v10）的语义以
 * docs/api.md「客户导出」为准：只带 `from_date`、`to_date`，同样不带任何租户或客户标识。
 */

import { apiGet } from "./client";
import { downloadFile } from "./download";

export const CUSTOMER_WALLET_URL = "/api/v1/customer/wallet";
export const CUSTOMER_WALLET_TRANSACTIONS_URL = "/api/v1/customer/wallet/transactions";
export const CUSTOMER_WALLET_TRANSACTIONS_EXPORT_URL = "/api/v1/customer/wallet/transactions/export";

/** 钱包摘要。恰好这 8 个键（docs/api.md）：没有钱包 `version`、`status_version`、任何内部 id。 */
export interface CustomerWallet {
  /** V1 恒为 `MYR`。 */
  currency: string;
  /** 恰好 8 位小数、带符号的十进制字符串，可为负。 */
  balance: string;
  /** `PREPAID` / `INTERNAL_METERED_ONLY`。写成 `string`：认不出的值原样显示。 */
  billing_mode: string;
  /** `ACTIVE` / `SUSPENDED`。 */
  billing_status: string;
  /** `ALLOW_AI` / `BLOCK_AI`。判断 AI 能不能用看它，不看 `billing_status`。 */
  ai_service: string;
  /** `BLOCK_AI` 时是四种原因之一，`ALLOW_AI` 时为 `null`。 */
  ai_service_reason: string | null;
  /** 管理员设的低余额提醒线：恰好 8 位小数的字符串；未设置（不提醒）为 `null`（AIH-TASK-110，设计闸门 #433 v2）。 */
  low_balance_threshold: string | null;
  /**
   * `PREPAID`、阈值非空且余额 ≤ 阈值时为 `true`。规则只在后端算：前端只看它，不自己比较余额与阈值。
   * `INTERNAL_METERED_ONLY` 恒为 `false`。
   */
  low_balance: boolean;
}

/** 一行账本。每项恰好这 7 个键：没有 `description`、来源、操作者、成本。 */
export interface CustomerTransaction {
  id: string;
  wallet_sequence: number;
  /** 九种类型之一；后端以后加了新值时原样显示，所以类型上写成 `string`。 */
  transaction_type: string;
  /** 恰好 8 位小数、带符号的十进制字符串。 */
  amount: string;
  balance_after: string;
  /** 只有 AI 用量扣费行有（接入方上报的 `event_id`），其余行 `null`。 */
  event_id: string | null;
  /** 记账时刻，不带时区的 UTC。 */
  created_at: string;
}

export interface CustomerTransactionPage {
  items: CustomerTransaction[];
  page_size: number;
  /** 还有更早的行时 = 本页最后一行的 `wallet_sequence`；没有更多时 `null`。 */
  next_before_sequence: number | null;
}

/** 可选参数：不带（或 `null`）的就不进查询串，由后端取默认值。 */
export interface CustomerTransactionQuery {
  pageSize?: number;
  beforeSequence?: number | null;
}

export const CUSTOMER_WALLET_QUERY_KEY = ["customer", "wallet"] as const;

/** 流水各页的 key 都以它开头。 */
export const CUSTOMER_TRANSACTIONS_QUERY_KEY = ["customer", "wallet-transactions"] as const;

export function customerTransactionListQueryKey(beforeSequence: number | null, pageSize: number) {
  return [...CUSTOMER_TRANSACTIONS_QUERY_KEY, beforeSequence ?? "NEWEST", pageSize] as const;
}

export function getCustomerWallet(signal?: AbortSignal): Promise<CustomerWallet> {
  return apiGet<CustomerWallet>(CUSTOMER_WALLET_URL, signal);
}

/** 一页账本行，最新在前（顺序由后端决定，前端不重排）。 */
export function listCustomerTransactions(
  query: CustomerTransactionQuery = {},
  signal?: AbortSignal,
): Promise<CustomerTransactionPage> {
  const params = new URLSearchParams();
  if (query.pageSize !== undefined) {
    params.set("page_size", String(query.pageSize));
  }
  if (query.beforeSequence !== undefined && query.beforeSequence !== null) {
    params.set("before_sequence", String(query.beforeSequence));
  }
  const search = params.toString();
  return apiGet<CustomerTransactionPage>(
    search === "" ? CUSTOMER_WALLET_TRANSACTIONS_URL : `${CUSTOMER_WALLET_TRANSACTIONS_URL}?${search}`,
    signal,
  );
}

/**
 * 导出的日期区间：`YYYY-MM-DD` 马来西亚日历日，含两端，**两个都必填**（docs/api.md「客户导出」）。没有分页、
 * 游标与任何筛选 —— 一次导出整个区间。
 */
export interface CustomerTransactionExportQuery {
  fromDate: string;
  toDate: string;
}

/** 导出的地址（含查询串）：只有 `from_date` 与 `to_date`。 */
export function customerTransactionExportUrl(query: CustomerTransactionExportQuery): string {
  const params = new URLSearchParams({ from_date: query.fromDate, to_date: query.toDate });
  return `${CUSTOMER_WALLET_TRANSACTIONS_EXPORT_URL}?${params.toString()}`;
}

/** 后端取不到文件名时的兜底，与后端同一规则：`wallet-transactions_<from_date>_<to_date>.csv`。 */
export function customerTransactionExportFilename(query: CustomerTransactionExportQuery): string {
  return `wallet-transactions_${query.fromDate}_${query.toDate}.csv`;
}

/**
 * 把区间内记账的全部流水导出成一个 CSV 并交给浏览器下载。失败抛 `ApiError`：`EXPORT_TOO_LARGE`（超过 20 000 行）、
 * `QUERY_TOO_BROAD`（超过执行时间预算）、`EXPORT_BUSY`（已有一个导出在进行），其余照常带 `request_id`。
 */
export function exportCustomerTransactions(query: CustomerTransactionExportQuery, signal?: AbortSignal): Promise<void> {
  return downloadFile(customerTransactionExportUrl(query), customerTransactionExportFilename(query), signal);
}

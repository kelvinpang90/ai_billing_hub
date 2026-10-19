/**
 * 管理端钱包流水（`GET /api/v1/admin/customers/{customer_id}/wallet/transactions`）。
 *
 * 语义以 docs/api.md「管理端钱包流水」为准（AIH-TASK-084，设计闸门 #351 v2）。只读：不写账本、不动余额。
 *
 * 分页是**序号游标**，不是 `page` / `total`：省略 `before_sequence` 是最新一页；下一页（更早的）把响应的
 * `next_before_sequence` 原样作为 `before_sequence` 发回去，它为 `null` 时没有更早的行。带来源筛选时序号不连续，
 * 游标只能取响应给的值，不能在前端推算。
 *
 * ⚠️ 金额与余额是**字符串**（INV-10），类型刻意写成 `string`，不在这一层解析。
 */

import { apiGet } from "./client";

const CUSTOMERS_URL = "/api/v1/admin/customers";

/** 来源（`reference_type`），大小写敏感；其余值后端回 422。顺序即下拉里的顺序。 */
export const WALLET_REFERENCE_TYPES = [
  "USAGE_EVENT",
  "PAYMENT",
  "ADMIN_ADJUSTMENT",
  "REBILL",
  "SYSTEM",
] as const;

export type WalletReferenceType = (typeof WALLET_REFERENCE_TYPES)[number];

/** 一行账本。每项恰好这 12 个键（docs/api.md）。 */
export interface WalletTransaction {
  id: string;
  wallet_sequence: number;
  /** 九种类型之一；后端以后加了新值时原样显示，所以类型上写成 `string`。 */
  transaction_type: string;
  /** 恰好 8 位小数、带符号的十进制字符串。 */
  amount: string;
  balance_before: string;
  balance_after: string;
  reference_type: string;
  reference_id: string;
  /** 只有 `USAGE_EVENT` 来源有；查不到对应事件（数据缺陷）时为 `null`。 */
  usage_event_id: string | null;
  description: string | null;
  /** 系统写入的行为 `null`。 */
  created_by_email: string | null;
  /** 记账时刻，不带时区的 UTC。 */
  created_at: string;
}

export interface WalletTransactionPage {
  items: WalletTransaction[];
  page_size: number;
  /** 还有更早的行时 = 本页最后一行的 `wallet_sequence`；没有更多时 `null`。 */
  next_before_sequence: number | null;
}

/** 可选参数：不带（或 `null`）的就不进查询串，由后端取默认值。 */
export interface WalletTransactionQuery {
  pageSize?: number;
  beforeSequence?: number | null;
  referenceType?: WalletReferenceType | null;
}

/** 一个客户的所有流水查询的前缀：调账之后让它们整体过期。各页、各筛选的 key 都以它开头。 */
export function walletTransactionsQueryKey(customerId: string) {
  return ["admin", "wallet-transactions", customerId] as const;
}

export function walletTransactionListQueryKey(
  customerId: string,
  referenceType: WalletReferenceType | null,
  beforeSequence: number | null,
  pageSize: number,
) {
  return [
    ...walletTransactionsQueryKey(customerId),
    referenceType ?? "ALL",
    beforeSequence ?? "NEWEST",
    pageSize,
  ] as const;
}

/** 一页账本行，最新在前（顺序由后端决定，前端不重排）。 */
export function listWalletTransactions(
  customerId: string,
  query: WalletTransactionQuery = {},
  signal?: AbortSignal,
): Promise<WalletTransactionPage> {
  const params = new URLSearchParams();
  if (query.pageSize !== undefined) {
    params.set("page_size", String(query.pageSize));
  }
  if (query.beforeSequence !== undefined && query.beforeSequence !== null) {
    params.set("before_sequence", String(query.beforeSequence));
  }
  if (query.referenceType !== undefined && query.referenceType !== null) {
    params.set("reference_type", query.referenceType);
  }
  const search = params.toString();
  // id 来自地址栏，与 adminCustomers.ts 的 customerUrl 同一个理由：不转义的话 `a/b` 会变成另一条路径。
  const url = `${CUSTOMERS_URL}/${encodeURIComponent(customerId)}/wallet/transactions`;
  return apiGet<WalletTransactionPage>(search === "" ? url : `${url}?${search}`, signal);
}

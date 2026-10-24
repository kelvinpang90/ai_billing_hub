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
 */

import { apiGet } from "./client";

export const CUSTOMER_WALLET_URL = "/api/v1/customer/wallet";
export const CUSTOMER_WALLET_TRANSACTIONS_URL = "/api/v1/customer/wallet/transactions";

/** 钱包摘要。恰好这 6 个键（docs/api.md）：没有钱包 `version`、低余额阈值、任何内部 id。 */
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

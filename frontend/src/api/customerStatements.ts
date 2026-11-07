/**
 * 客户自己的月度对账单（`GET /api/v1/customer/statements`、`GET …/{statement_id}`）。
 *
 * 语义以 docs/api.md「月度对账单（客户）」为准（AIH-TASK-160，设计闸门 #565 v6 §2「客户接口」）。两个接口都只读，
 * 只返回本客户**已定稿**的对账单；四种 `account_status` 都照常可读。
 *
 * ⚠️ **不带任何租户或客户标识**：客户是谁只取自访问令牌。这一层也不接受这类参数。
 *
 * 分页是**页码**（`page` / `page_size`，见 docs/api.md「分页」），不是门户其他列表的游标：查询串写法与
 * `integrationAccess.ts` 的 `listCredentials` 相同，响应是 `adminCustomers.ts` 的 {@link Page}。
 *
 * ⚠️ 金额是**字符串**（INV-10），这一层不解析。`statement_id` 只来自列表的响应，**原样**放进路径：不校验形状、
 * 不改大小写 —— 不是小写规范 UUID、没有这份、属于别的客户或尚未定稿时后端回同一个 404，那才是准绳。
 */

import type { Page } from "./adminCustomers";
import type { StatementSnapshot } from "./adminStatements";
import { apiGet } from "./client";

export type {
  ModelUsage,
  PriorPeriodUsage,
  ProjectUsage,
  ProviderUsage,
  StatementSnapshot,
  UsageCounts,
} from "./adminStatements";

export const CUSTOMER_STATEMENTS_URL = "/api/v1/customer/statements";

/** 详情：没有这份、属于别的客户、尚未定稿或 id 形状不对（几种情形响应相同）。 */
export const STATEMENT_NOT_FOUND = "STATEMENT_NOT_FOUND";

/** 对账单只有 MYR：快照的 `currency` 恒为 MYR。 */
export const STATEMENT_CURRENCY = "MYR";

/** 列表项：恰好这 5 个键。时间是不带时区的 UTC，精确到秒（docs/api.md「时间」）。 */
export interface CustomerStatementSummary {
  statement_id: string;
  /** `ST-<YYYYMM>-` 加 8 个 Crockford base32 字符。 */
  statement_number: string;
  /** 账期 `YYYY-MM`（马来西亚日历月）。 */
  period: string;
  /** 快照里的期末余额，恰好 8 位小数的字符串：按 T+1 归属计算，不是某一时刻的钱包余额。 */
  closing_balance: string;
  finalized_at: string;
}

/**
 * 详情 = 列表项的 5 个键再加 `snapshot`。快照与管理端是同一份（键与含义相同）；客户看不到 `customer_id`、`cutoff_at`、
 * `ledger_high_sequence`、生成者等管理端字段。
 */
export interface CustomerStatementDetail extends CustomerStatementSummary {
  snapshot: StatementSnapshot;
}

/** 对账单相关的 key 都以它开头。 */
export const CUSTOMER_STATEMENTS_QUERY_KEY = ["customer", "statements"] as const;

export function customerStatementListQueryKey(page: number, pageSize: number) {
  return [...CUSTOMER_STATEMENTS_QUERY_KEY, "list", page, pageSize] as const;
}

export function customerStatementDetailQueryKey(statementId: string) {
  return [...CUSTOMER_STATEMENTS_QUERY_KEY, "detail", statementId] as const;
}

/** 自己已定稿的对账单分页，账期倒序（顺序由后端决定，前端不重排）。 */
export function listCustomerStatements(
  page: number,
  pageSize: number,
  signal?: AbortSignal,
): Promise<Page<CustomerStatementSummary>> {
  const query = new URLSearchParams({ page: String(page), page_size: String(pageSize) });
  return apiGet<Page<CustomerStatementSummary>>(`${CUSTOMER_STATEMENTS_URL}?${query.toString()}`, signal);
}

export function getCustomerStatement(statementId: string, signal?: AbortSignal): Promise<CustomerStatementDetail> {
  return apiGet<CustomerStatementDetail>(`${CUSTOMER_STATEMENTS_URL}/${statementId}`, signal);
}

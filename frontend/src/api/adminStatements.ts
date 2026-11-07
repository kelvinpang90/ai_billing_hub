/**
 * 管理端月度对账单（契约见 docs/api.md「月度对账单（管理端）」，AIH-TASK-159；设计闸门 #565 v6 §2）。
 *
 * 三个接口：对账单列表（按客户、账期筛）、对账单详情（带完整快照）、按客户补生成缺失的对账单。要 ADMIN，角色以
 * 后端数据库为准 —— 前端不做任何角色判断，403 原样交给页面显示。列表与详情只读；补生成没有请求体，已有的对账单
 * 不重算、不改，所以重发只会补上仍然缺的那几期。
 *
 * ⚠️ 金额是**字符串**（INV-10），这一层不解析。`statement_id` 只来自列表的响应、`customer_id` 只来自客户列表的响应，
 * 都**原样**放进路径：不校验形状、不改大小写 —— 不是小写规范 UUID 或没有这份对账单 / 这个客户时后端回 404，
 * 那才是准绳。
 */

import type { Page } from "./adminCustomers";
import { apiGet, client, toApiError, unwrapEnvelope, type ApiEnvelope } from "./client";

const STATEMENTS_URL = "/api/v1/admin/statements";
const CUSTOMERS_URL = "/api/v1/admin/customers";

/** 详情：`statement_id` 不是小写规范 UUID，或没有这份对账单（两种情形响应相同）。 */
export const STATEMENT_NOT_FOUND = "STATEMENT_NOT_FOUND";
/** 补生成：客户不是预付（内部计量客户），或没有钱包。 */
export const STATEMENT_NOT_ELIGIBLE = "STATEMENT_NOT_ELIGIBLE";

/** 对账单只有 MYR：快照的 `currency` 恒为 MYR，金额按它显示。 */
export const STATEMENT_CURRENCY = "MYR";

/**
 * 账期 `YYYY-MM`：月份 01–12、年份 2000–2999（docs/api.md：别的形状后端回 422）。页面用它在发请求之前拦下
 * 形状不对的输入。
 */
export const PERIOD_PATTERN = /^2\d{3}-(?:0[1-9]|1[0-2])$/;

/** V1 只产生 `FINALIZED`；`DRAFT` / `VOID` 只是保留的状态值。 */
export type StatementStatus = "DRAFT" | "FINALIZED" | "VOID";

/** 列表项：恰好这 8 个键。时间是不带时区的 UTC，精确到秒（docs/api.md「时间」）。 */
export interface StatementSummary {
  statement_id: string;
  /** `ST-<YYYYMM>-` 加 8 个 Crockford base32 字符，全局唯一。 */
  statement_number: string;
  customer_id: string;
  /** 快照里的公司名（生成时冻结）。 */
  company_name: string;
  period: string;
  status: StatementStatus;
  /** 快照里的期末余额，恰好 8 位小数的字符串。 */
  closing_balance: string;
  finalized_at: string;
}

/** 上期调整里的一行：在 cut-off 之后才入账的某个往期月份的用量。 */
export interface PriorPeriodUsage {
  /** 原用量月份 `YYYY-MM`。 */
  usage_period: string;
  request_count: number;
  amount: string;
}

/** 明细表共有的四个计数字段。 */
export interface UsageCounts {
  request_count: number;
  input_tokens: number;
  output_tokens: number;
  charges: string;
}

export interface ProviderUsage extends UsageCounts {
  provider_code: string;
}

export interface ModelUsage extends UsageCounts {
  provider_code: string;
  model_code: string;
}

export interface ProjectUsage extends UsageCounts {
  project_public_id: string;
  /** 生成时的项目名。 */
  project_name: string;
}

/**
 * 快照（`schema_version` 为 1）。金额都是带符号、恰好 8 位小数的字符串，扣款为负；计数与 token 数是整数。
 * 没有任何成本、汇率、加价或毛利字段。
 */
export interface StatementSnapshot {
  schema_version: number;
  period: string;
  currency: string;
  company_name: string;
  /** cut-off 时刻生效的税务政策版本的说明原文，逐字照抄。 */
  tax_notice: { zh: string; en: string };
  opening_balance: string;
  total_topup: string;
  total_ai_usage: string;
  total_adjustments: string;
  prior_period: { usage: PriorPeriodUsage[]; rebill: string; total: string };
  closing_balance: string;
  request_count: number;
  conversation_count: number;
  input_tokens: number;
  output_tokens: number;
  total_customer_charges: string;
  by_provider: ProviderUsage[];
  by_model: ModelUsage[];
  by_project: ProjectUsage[];
}

/** 详情 = 列表项的 8 个键再加 4 个。 */
export interface StatementDetail extends StatementSummary {
  /** 这一期的 T+1 cut-off 时刻（次月 2 日 00:00 马来西亚时间），不带时区的 UTC。 */
  cutoff_at: string;
  ledger_high_sequence: number;
  /** 手动补生成的管理员的邮箱；后台任务生成的为 `null`。 */
  generated_by_email: string | null;
  snapshot: StatementSnapshot;
}

/** 补生成本次新生成的一份：恰好这 2 个键。 */
export interface GeneratedStatement {
  statement_id: string;
  period: string;
}

/** 列表的筛选条件。没给的**不出现在查询串里**。 */
export interface StatementFilter {
  customer_id?: string;
  period?: string;
}

export const STATEMENTS_QUERY_KEY = ["admin", "statements"] as const;
/** 所有列表页的前缀：补生成之后（不论成败）让它们整体过期。 */
export const STATEMENT_LISTS_QUERY_KEY = [...STATEMENTS_QUERY_KEY, "list"] as const;

export function statementListQueryKey(filter: StatementFilter, page: number, pageSize: number) {
  return [...STATEMENT_LISTS_QUERY_KEY, filter, page, pageSize] as const;
}

export function statementDetailQueryKey(statementId: string) {
  return [...STATEMENTS_QUERY_KEY, "detail", statementId] as const;
}

/** 对账单分页，账期倒序（顺序由后端决定，前端不重排）。 */
export function listStatements(
  filter: StatementFilter,
  page: number,
  pageSize: number,
  signal?: AbortSignal,
): Promise<Page<StatementSummary>> {
  const query = new URLSearchParams({ page: String(page), page_size: String(pageSize) });
  if (filter.customer_id !== undefined) {
    query.set("customer_id", filter.customer_id);
  }
  if (filter.period !== undefined) {
    query.set("period", filter.period);
  }
  return apiGet<Page<StatementSummary>>(`${STATEMENTS_URL}?${query.toString()}`, signal);
}

export function getStatement(statementId: string, signal?: AbortSignal): Promise<StatementDetail> {
  return apiGet<StatementDetail>(`${STATEMENTS_URL}/${statementId}`, signal);
}

/**
 * 对这个客户补生成每一期已到期而缺失的对账单（同步执行，没有请求体）。返回本次新生成的那几份，按账期升序；
 * 什么都没缺时是空数组。某一期失败时之前已生成的照常保留、响应是 500，所以调用方不论成败都要刷新列表。
 */
export async function generateStatements(customerId: string): Promise<GeneratedStatement[]> {
  try {
    const response = await client.post<ApiEnvelope<GeneratedStatement[]>>(
      `${CUSTOMERS_URL}/${customerId}/statements/generate`,
    );
    return unwrapEnvelope<GeneratedStatement[]>(response.data);
  } catch (error) {
    throw toApiError(error);
  }
}

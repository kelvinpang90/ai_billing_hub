/**
 * 客户自己的会话（`GET /api/v1/customer/conversations`）。
 *
 * 语义以 docs/api.md「客户会话」为准（AIH-TASK-121，设计闸门 #460 v2）。只读。
 *
 * 会话的键是（项目, `conversation_id`）：同一个 `conversation_id` 出现在两个项目里是两行。只算所选马来西亚日历日区间内
 * 已计费、带 `conversation_id` 的请求；没有会话 ID 的请求不进任何一行（在用量接口看）。
 *
 * ⚠️ **不带任何租户或客户标识**：客户是谁只取自访问令牌（spec §90）。这一层也不接受这类参数。接口不按供应商、模型或
 * `conversation_id` 筛选，这一层同样不留这几个参数 —— 要看某一次会话的请求，用用量接口按 `conversation_id` 查。
 *
 * 分页是**不透明游标**：省略 `cursor` 是最新一页；更早的一页把响应的 `next_cursor` 原样作为 `cursor` 发回去，
 * 它为 `null` 时没有更早的会话。游标只原样传回，前端不解析、不拼装（后端以后可以改编码；会话游标与用量游标是两种格式）。
 *
 * ⚠️ 金额是**字符串**（INV-10），类型刻意写成 `string`，不在这一层解析。
 *
 * 会话摘要导出（`GET /api/v1/customer/conversations/export`，AIH-TASK-124）以 docs/api.md「客户导出」的
 * conversations/export 小节为准（AIH-TASK-122）：与列表同一口径，只带 `from_date`、`to_date` 与可选的 `project_id`，
 * 没有 `page_size` 与 `cursor`，一次导出整个区间。经 `downloadFile` 带令牌下载。
 */

import { apiGet } from "./client";
import { downloadFile } from "./download";

export const CUSTOMER_CONVERSATIONS_URL = "/api/v1/customer/conversations";
export const CUSTOMER_CONVERSATIONS_EXPORT_URL = "/api/v1/customer/conversations/export";

/** 一次会话在所选区间内的合计。恰好这 9 个键：没有成本、毛利、供应商 / 模型拆分与内部 id。 */
export interface CustomerConversation {
  /** 集成方上报的原值。 */
  conversation_id: string;
  /** 项目的 `public_id`。 */
  project_id: string;
  project_name: string;
  /** 区间内第一条已计费请求的发生时刻（不是会话真正的开始），不带时区的 UTC，保留微秒。 */
  first_occurred_at: string;
  /** 区间内最后一条已计费请求的发生时刻，格式同上。 */
  last_occurred_at: string;
  request_count: number;
  /** 数量形态的请求不计入；全是数量形态时为 0。 */
  input_tokens: number;
  output_tokens: number;
  /** 这些请求的计费额之和（含税），恰好 8 位小数的十进制字符串。 */
  charged_amount: string;
}

export interface CustomerConversationPage {
  items: CustomerConversation[];
  page_size: number;
  /** 还有更旧的会话时是一个不透明字符串，否则 `null`。 */
  next_cursor: string | null;
}

/**
 * 查询条件。两个日期是马来西亚日历日 `YYYY-MM-DD`，后端必填（这一层不补默认值）。其余可选：不带（`undefined`、`null`
 * 或空串）的就不进查询串。
 */
export interface CustomerConversationQuery {
  fromDate: string;
  toDate: string;
  projectId?: string | null;
  pageSize?: number;
  cursor?: string | null;
}

/** 会话各页的 key 都以它开头。 */
export const CUSTOMER_CONVERSATIONS_QUERY_KEY = ["customer", "conversations"] as const;

export function customerConversationListQueryKey(query: CustomerConversationQuery) {
  return [...CUSTOMER_CONVERSATIONS_QUERY_KEY, query] as const;
}

function present(value: string | null | undefined): value is string {
  return value !== undefined && value !== null && value !== "";
}

/** 列表的地址（含查询串）：按 `from_date`、`to_date`、`project_id`、`page_size`、`cursor` 的顺序，不带的不放。 */
export function customerConversationsUrl(query: CustomerConversationQuery): string {
  const params = new URLSearchParams();
  if (present(query.fromDate)) {
    params.set("from_date", query.fromDate);
  }
  if (present(query.toDate)) {
    params.set("to_date", query.toDate);
  }
  if (present(query.projectId)) {
    params.set("project_id", query.projectId);
  }
  if (query.pageSize !== undefined) {
    params.set("page_size", String(query.pageSize));
  }
  if (present(query.cursor)) {
    params.set("cursor", query.cursor);
  }
  const search = params.toString();
  return search === "" ? CUSTOMER_CONVERSATIONS_URL : `${CUSTOMER_CONVERSATIONS_URL}?${search}`;
}

/** 一页会话，最后活动最新的在前（顺序由后端决定，前端不重排）。 */
export function listCustomerConversations(
  query: CustomerConversationQuery,
  signal?: AbortSignal,
): Promise<CustomerConversationPage> {
  return apiGet<CustomerConversationPage>(customerConversationsUrl(query), signal);
}

/**
 * 导出的筛选：两个日期必填（马来西亚日历日 `YYYY-MM-DD`），项目可选（不带 —— `undefined`、`null` 或空串 —— 的不进
 * 查询串）。没有 `pageSize` 与 `cursor`：一次导出整个区间。
 */
export interface CustomerConversationExportQuery {
  fromDate: string;
  toDate: string;
  projectId?: string | null;
}

/** 导出的地址（含查询串）：按 `from_date`、`to_date`、`project_id` 的顺序，不带的不放。 */
export function customerConversationsExportUrl(query: CustomerConversationExportQuery): string {
  const params = new URLSearchParams();
  if (present(query.fromDate)) {
    params.set("from_date", query.fromDate);
  }
  if (present(query.toDate)) {
    params.set("to_date", query.toDate);
  }
  if (present(query.projectId)) {
    params.set("project_id", query.projectId);
  }
  const search = params.toString();
  return search === "" ? CUSTOMER_CONVERSATIONS_EXPORT_URL : `${CUSTOMER_CONVERSATIONS_EXPORT_URL}?${search}`;
}

/** 后端取不到文件名时的兜底，与后端同一规则：`conversations_<from_date>_<to_date>.csv`。 */
export function customerConversationsExportFilename(query: CustomerConversationExportQuery): string {
  return `conversations_${query.fromDate}_${query.toDate}.csv`;
}

/**
 * 把区间内全部会话导出成一个 CSV 并交给浏览器下载。失败抛 `ApiError`：`EXPORT_TOO_LARGE`（超过 20 000 个会话）、
 * `QUERY_TOO_BROAD`（超过执行时间预算）、`EXPORT_BUSY`（已有一个导出在进行），其余照常带 `request_id`。
 */
export function exportCustomerConversations(
  query: CustomerConversationExportQuery,
  signal?: AbortSignal,
): Promise<void> {
  return downloadFile(customerConversationsExportUrl(query), customerConversationsExportFilename(query), signal);
}

/**
 * 客户自己的项目与用量（`GET /api/v1/customer/projects`、`GET /api/v1/customer/usage-events`）与按月汇总
 * （`GET /api/v1/customer/usage-summary`）。
 *
 * 语义以 docs/api.md「客户用量」为准（AIH-TASK-104，设计闸门 #398 v4）；按月汇总以同一节的 usage-summary 小节为准
 * （AIH-TASK-107，设计闸门 #403 v1）；用量导出以 docs/api.md「客户导出」为准（AIH-TASK-117，设计闸门 #435 v10）；
 * 月度汇总导出以同一节的 usage-summary/export 小节为准（AIH-TASK-120，设计闸门 #457 v1）。只读。
 *
 * ⚠️ **不带任何租户或客户标识**：客户是谁只取自访问令牌（spec §90）。这一层也不接受这类参数 —— 不给调用方
 * 留下「顺手带上 customer_id」的口子。
 *
 * 分页是**不透明游标**：省略 `cursor` 是最新一页；更早的一页把响应的 `next_cursor` 原样作为 `cursor` 发回去，
 * 它为 `null` 时没有更早的行。游标只原样传回，前端不解析、不拼装（后端以后可以改编码）。
 *
 * ⚠️ 金额与数量是**字符串**（INV-10），类型刻意写成 `string`，不在这一层解析。
 */

import { apiGet } from "./client";
import { downloadFile } from "./download";

export const CUSTOMER_PROJECTS_URL = "/api/v1/customer/projects";
export const CUSTOMER_USAGE_EVENTS_URL = "/api/v1/customer/usage-events";
export const CUSTOMER_USAGE_EXPORT_URL = "/api/v1/customer/usage-events/export";
export const CUSTOMER_USAGE_SUMMARY_URL = "/api/v1/customer/usage-summary";
export const CUSTOMER_USAGE_SUMMARY_EXPORT_URL = "/api/v1/customer/usage-summary/export";

/** 查询超过后端的执行时间预算（2 秒）：请客户缩小日期区间或换筛选条件（docs/api.md「客户用量」）。 */
export const QUERY_TOO_BROAD = "QUERY_TOO_BROAD";

/** 一个项目。恰好这 2 个键：没有描述、集成状态、状态 webhook 地址、凭据。 */
export interface CustomerProject {
  id: string;
  name: string;
}

export interface CustomerProjectList {
  items: CustomerProject[];
}

/** 一条已计费的用量事件。恰好这 18 个键：没有成本、汇率、参考价、毛利、内部状态与内部 id。 */
export interface CustomerUsageEvent {
  id: string;
  event_id: string;
  request_id: string | null;
  /** AI 模块代为上报的事件为 `null`。 */
  conversation_id: string | null;
  feature: string | null;
  project_id: string;
  project_name: string;
  /** 上报的原始字符串。 */
  provider: string;
  model: string;
  /** 计量类型的 `code`；后端以后加了新类型时原样显示，所以写成 `string`。 */
  usage_type: string;
  /** token 形态四个 token 字段非空、`quantity` 为 `null`；数量形态相反。 */
  input_tokens: number | null;
  output_tokens: number | null;
  cache_creation_input_tokens: number | null;
  cache_read_input_tokens: number | null;
  /** 恰好 8 位小数的十进制字符串。 */
  quantity: string | null;
  unit: string | null;
  /** 本次请求向客户收的金额（含税），恰好 8 位小数的十进制字符串。 */
  charged_amount: string;
  /** 调用发生时刻，不带时区的 UTC，保留微秒。 */
  occurred_at: string;
}

export interface CustomerUsagePage {
  items: CustomerUsageEvent[];
  page_size: number;
  /** 还有更旧的行时是一个不透明字符串，否则 `null`。 */
  next_cursor: string | null;
}

/**
 * 可选参数：不带（`undefined`、`null` 或空串）的就不进查询串。日期是马来西亚日历日 `YYYY-MM-DD`；没有 `eventId`
 * 时两个日期都必填，那是调用方的事，这一层不补默认值。
 */
export interface CustomerUsageQuery {
  fromDate?: string | null;
  toDate?: string | null;
  projectId?: string | null;
  provider?: string | null;
  model?: string | null;
  conversationId?: string | null;
  eventId?: string | null;
  pageSize?: number;
  cursor?: string | null;
}

export const CUSTOMER_PROJECTS_QUERY_KEY = ["customer", "projects"] as const;

/** 用量各页的 key 都以它开头。 */
export const CUSTOMER_USAGE_QUERY_KEY = ["customer", "usage-events"] as const;

export function customerUsageListQueryKey(query: CustomerUsageQuery) {
  return [...CUSTOMER_USAGE_QUERY_KEY, query] as const;
}

/** 自己的全部项目，按创建先后（不分页）。 */
export function listCustomerProjects(signal?: AbortSignal): Promise<CustomerProjectList> {
  return apiGet<CustomerProjectList>(CUSTOMER_PROJECTS_URL, signal);
}

/** 查询参数名与 {@link CustomerUsageQuery} 字段的对应，按这个顺序进查询串。 */
const TEXT_PARAMS = [
  ["from_date", "fromDate"],
  ["to_date", "toDate"],
  ["project_id", "projectId"],
  ["provider", "provider"],
  ["model", "model"],
  ["conversation_id", "conversationId"],
  ["event_id", "eventId"],
] as const;

/** 按 {@link TEXT_PARAMS} 的顺序放进查询串；不带（`undefined`、`null` 或空串）的不放。 */
function textParams(query: Omit<CustomerUsageQuery, "pageSize" | "cursor">): URLSearchParams {
  const params = new URLSearchParams();
  for (const [name, field] of TEXT_PARAMS) {
    const value = query[field];
    if (value !== undefined && value !== null && value !== "") {
      params.set(name, value);
    }
  }
  return params;
}

/** 一页已计费的用量事件，最新在前（顺序由后端决定，前端不重排）。 */
export function listCustomerUsageEvents(
  query: CustomerUsageQuery = {},
  signal?: AbortSignal,
): Promise<CustomerUsagePage> {
  const params = textParams(query);
  if (query.pageSize !== undefined) {
    params.set("page_size", String(query.pageSize));
  }
  if (query.cursor !== undefined && query.cursor !== null && query.cursor !== "") {
    params.set("cursor", query.cursor);
  }
  const search = params.toString();
  return apiGet<CustomerUsagePage>(
    search === "" ? CUSTOMER_USAGE_EVENTS_URL : `${CUSTOMER_USAGE_EVENTS_URL}?${search}`,
    signal,
  );
}

/**
 * 导出的筛选：与用量页相同的参数名，**两个日期恒必填**（带 `eventId` 也要，docs/api.md「客户导出」）；没有
 * `pageSize` 与 `cursor` —— 一次导出整个区间。其余可选，不带（`undefined`、`null` 或空串）的不进查询串。
 */
export interface CustomerUsageExportQuery {
  fromDate: string;
  toDate: string;
  projectId?: string | null;
  provider?: string | null;
  model?: string | null;
  conversationId?: string | null;
  eventId?: string | null;
}

/** 导出的地址（含查询串）。 */
export function customerUsageExportUrl(query: CustomerUsageExportQuery): string {
  const search = textParams(query).toString();
  return search === "" ? CUSTOMER_USAGE_EXPORT_URL : `${CUSTOMER_USAGE_EXPORT_URL}?${search}`;
}

/** 后端取不到文件名时的兜底，与后端同一规则：`usage-events_<from_date>_<to_date>.csv`。 */
export function customerUsageExportFilename(query: CustomerUsageExportQuery): string {
  return `usage-events_${query.fromDate}_${query.toDate}.csv`;
}

/**
 * 把区间内全部已计费用量导出成一个 CSV 并交给浏览器下载。失败抛 `ApiError`：`EXPORT_TOO_LARGE`（超过 20 000 行）、
 * `QUERY_TOO_BROAD`（超过执行时间预算）、`EXPORT_BUSY`（已有一个导出在进行），其余照常带 `request_id`。
 */
export function exportCustomerUsageEvents(query: CustomerUsageExportQuery, signal?: AbortSignal): Promise<void> {
  return downloadFile(customerUsageExportUrl(query), customerUsageExportFilename(query), signal);
}

/** 按月汇总里的一天。恰好这 3 个键。 */
export interface CustomerUsageSummaryDay {
  /** 马来西亚日历日，`YYYY-MM-DD`。 */
  date: string;
  request_count: number;
  /** 当天的计费额（含税），恰好 8 位小数的十进制字符串；没有事件的日子为 `"0.00000000"`。 */
  charged_amount: string;
}

/** 一个马来西亚日历月的用量汇总。恰好这 5 个键：没有成本、毛利、token 合计、拆分与内部 id。 */
export interface CustomerUsageSummary {
  /** 实际统计的月份，`YYYY-MM`（马来西亚日历月）。 */
  month: string;
  /** 该月计费额合计（含税），恰好 8 位小数的十进制字符串；等于 `days` 各日之和。 */
  charged_amount: string;
  /** 该月已计费的请求数。 */
  request_count: number;
  /** 该月不同的非空 `conversation_id` 个数：没有对话标识的请求不计入。 */
  conversation_count: number;
  /** 该月每一天各一项（28–31 项），按日期升序。 */
  days: CustomerUsageSummaryDay[];
}

/** 汇总的 key 都以它开头。 */
export const CUSTOMER_USAGE_SUMMARY_QUERY_KEY = ["customer", "usage-summary"] as const;

export function customerUsageSummaryQueryKey(month: string | null = null) {
  return [...CUSTOMER_USAGE_SUMMARY_QUERY_KEY, month ?? "CURRENT"] as const;
}

/**
 * 某个马来西亚日历月（`YYYY-MM`）的用量汇总。不带 `month`（`undefined`、`null` 或空串）时不进查询串，由后端取
 * 请求时刻所在的月份。
 */
export function getCustomerUsageSummary(
  month: string | null = null,
  signal?: AbortSignal,
): Promise<CustomerUsageSummary> {
  if (month === null || month === "") {
    return apiGet<CustomerUsageSummary>(CUSTOMER_USAGE_SUMMARY_URL, signal);
  }
  const params = new URLSearchParams({ month });
  return apiGet<CustomerUsageSummary>(`${CUSTOMER_USAGE_SUMMARY_URL}?${params.toString()}`, signal);
}

/** 月度汇总导出的地址：只带 `month`（`YYYY-MM`，马来西亚日历月，必填）。 */
export function customerUsageSummaryExportUrl(month: string): string {
  const params = new URLSearchParams({ month });
  return `${CUSTOMER_USAGE_SUMMARY_EXPORT_URL}?${params.toString()}`;
}

/** 后端取不到文件名时的兜底，与后端同一规则：`usage-summary_<YYYY-MM>.csv`。 */
export function customerUsageSummaryExportFilename(month: string): string {
  return `usage-summary_${month}.csv`;
}

/**
 * 把某个马来西亚日历月的用量汇总（每天一行加末尾合计行）导出成一个 CSV 并交给浏览器下载。不占导出名额，所以没有
 * `EXPORT_BUSY`；行数恒有界，所以没有 `EXPORT_TOO_LARGE`。失败抛 `ApiError`：`QUERY_TOO_BROAD`（超过执行时间预算），
 * 其余照常带 `request_id`。
 */
export function exportCustomerUsageSummary(month: string, signal?: AbortSignal): Promise<void> {
  return downloadFile(customerUsageSummaryExportUrl(month), customerUsageSummaryExportFilename(month), signal);
}

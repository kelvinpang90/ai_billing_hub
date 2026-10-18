/**
 * 管理端用量分析（契约见 docs/api.md「管理端用量分析」（AIH-TASK-073））。
 *
 * 只有一个只读接口 `GET /api/v1/admin/usage-analytics`，要 ADMIN。前端不做任何角色判断，403 原样交给页面显示。
 *
 * **成本、计费额与毛利只出现在管理端接口里**（INV-7）：这里的类型只给管理端页面用。
 *
 * ⚠️ 金额、平均 token 数与毛利率是**字符串**（INV-10），这一层不解析：一旦变成 `number`，
 * `"0.10000001"` 这类值就已经不是后端给的那个数了。
 */

import { apiGet } from "./client";

const USAGE_ANALYTICS_URL = "/api/v1/admin/usage-analytics";

// --- 分组维度 ---------------------------------------------------------------------

export const USAGE_ANALYTICS_GROUP_BYS = ["provider", "model", "customer", "project", "feature"] as const;
export type UsageAnalyticsGroupBy = (typeof USAGE_ANALYTICS_GROUP_BYS)[number];

/** 时间段的最大跨度（天）。超出后端 422。 */
export const USAGE_ANALYTICS_MAX_SPAN_DAYS = 366;

// --- 对象 -----------------------------------------------------------------------
// 只统计 `PROCESSED` 事件。金额都是 MYR、8 位小数的十进制字符串。

/** 预付租户的事件：计费额、估算成本、毛利与毛利率。 */
export interface UsageAnalyticsPrepaid {
  event_count: number;
  billable_cost: string;
  estimated_provider_cost_myr: string;
  /** 计费额 − 估算成本，可为负。 */
  gross_margin: string;
  /** 毛利 ÷ 计费额（不是百分数）；计费额为 0 时为 null。 */
  gross_margin_rate: string | null;
}

/** 内部计量租户的事件：参考售价不是收入，不进毛利。 */
export interface UsageAnalyticsInternalMeteredOnly {
  event_count: number;
  estimated_provider_cost_myr: string;
  reference_customer_price: string;
}

export interface UsageAnalyticsItem {
  /**
   * 供应商 / 模型是已解析的目录代码，客户 / 项目是 public id，功能是标签。未解析的值单列为 `null` 组。
   */
  group_key: string | null;
  /** 客户与项目的名称；其余维度为 null。 */
  group_name: string | null;
  event_count: number;
  /** 带 token 数的事件个数：平均值的分母。 */
  token_event_count: number;
  input_tokens: number;
  output_tokens: number;
  cache_creation_input_tokens: number;
  cache_read_input_tokens: number;
  /** 十进制字符串；没有带 token 数的事件时为 null。 */
  average_input_tokens: string | null;
  average_output_tokens: string | null;
  prepaid: UsageAnalyticsPrepaid;
  internal_metered_only: UsageAnalyticsInternalMeteredOnly;
}

export interface UsageAnalyticsReport {
  /** 固定为 `estimated`：成本是处理时保存的估算值，不含网关手续费。 */
  cost_basis: "estimated";
  items: UsageAnalyticsItem[];
  page: number;
  page_size: number;
  total: number;
}

// --- 查询条件 ---------------------------------------------------------------------

/** 可选筛选的参数名，顺序即查询串里的顺序。语义同管理端用量事件列表。 */
const FILTER_PARAMS = ["customer_id", "project_id", "provider", "model", "feature"] as const;

/**
 * `group_by`、`occurred_from`（含）与 `occurred_to`（不含）必填；后两个是**带时区**的 RFC 3339、整秒
 * （从吉隆坡时间换算见 components/DateTimeText.tsx 的 `displayTimeToRfc3339`）。其余筛选可省略。
 */
export type UsageAnalyticsQuery = {
  group_by: UsageAnalyticsGroupBy;
  occurred_from: string;
  occurred_to: string;
} & Partial<Record<(typeof FILTER_PARAMS)[number], string>>;

// --- 查询键 ---------------------------------------------------------------------

export const USAGE_ANALYTICS_QUERY_KEY = ["admin", "usage-analytics"] as const;

export function usageAnalyticsQueryKey(query: UsageAnalyticsQuery, page: number, pageSize: number) {
  return [...USAGE_ANALYTICS_QUERY_KEY, query, page, pageSize] as const;
}

// --- 接口 -----------------------------------------------------------------------

/**
 * 一页分组，按事件数倒序、分组键升序（由后端决定，前端不重排）。
 *
 * ⚠️ 省略的、以及去掉首尾空白后为空的可选筛选**不出现在查询串里**：后端对 `provider=` 这种空值是按空串
 * 比较，结果是一张莫名其妙的空表。
 */
export function getUsageAnalytics(
  query: UsageAnalyticsQuery,
  page: number,
  pageSize: number,
  signal?: AbortSignal,
): Promise<UsageAnalyticsReport> {
  const params = new URLSearchParams({
    group_by: query.group_by,
    occurred_from: query.occurred_from,
    occurred_to: query.occurred_to,
    page: String(page),
    page_size: String(pageSize),
  });
  for (const name of FILTER_PARAMS) {
    const value = query[name]?.trim();
    if (value !== undefined && value !== "") {
      params.set(name, value);
    }
  }
  return apiGet<UsageAnalyticsReport>(`${USAGE_ANALYTICS_URL}?${params.toString()}`, signal);
}

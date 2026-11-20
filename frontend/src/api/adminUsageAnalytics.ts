/**
 * 管理端用量分析（契约见 docs/api.md「管理端用量分析」（AIH-TASK-073）与其后的导出小节；毛利扩展见设计闸门 #709
 * v2，AIH-TASK-224）。
 *
 * 两个只读接口，都要 ADMIN：`GET /api/v1/admin/usage-analytics`（分组一页加全量合计）与
 * `GET /api/v1/admin/usage-analytics/export`（同一组条件的 CSV，没有分页）。前端不做任何角色判断，403 原样交给页面显示。
 *
 * **成本、计费额、手续费与毛利只出现在管理端接口里**（INV-7）：这里的类型只给管理端页面用。
 *
 * ⚠️ 金额、平均 token 数与比率是**字符串**（INV-10），这一层不解析：一旦变成 `number`，
 * `"0.10000001"` 这类值就已经不是后端给的那个数了。
 */

import { apiGet } from "./client";
import { downloadFile, withExportFormat, type ExportFormat } from "./download";

const USAGE_ANALYTICS_URL = "/api/v1/admin/usage-analytics";
const USAGE_ANALYTICS_EXPORT_URL = "/api/v1/admin/usage-analytics/export";

/** 响应头里取不到文件名时的兜底。 */
export const USAGE_ANALYTICS_EXPORT_FILENAME = "usage-analytics.csv";

/** 同上，`format=xlsx` 时。 */
export const USAGE_ANALYTICS_EXPORT_FILENAME_XLSX = "usage-analytics.xlsx";

// --- 分组维度 ---------------------------------------------------------------------

/** `day` / `month` 按马来西亚日历归属，键是 `YYYY-MM-DD` / `YYYY-MM`，按键升序（时间顺序）。 */
export const USAGE_ANALYTICS_GROUP_BYS = ["provider", "model", "customer", "project", "feature", "day", "month"] as const;
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

/** 分组项与合计共有的计数、平均值与两块金额。 */
export interface UsageAnalyticsAggregate {
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

export interface UsageAnalyticsItem extends UsageAnalyticsAggregate {
  /**
   * 供应商 / 模型是已解析的目录代码，客户 / 项目是 public id，功能是标签，日 / 月是 `YYYY-MM-DD` / `YYYY-MM`。
   * 未解析的值单列为 `null` 组。
   */
  group_key: string | null;
  /** 客户与项目的名称；其余维度为 null。 */
  group_name: string | null;
}

/**
 * 手续费块（**现金期间**：按付款确认时刻 `paid_at` 归期，与查询同一对时刻）。只算 `PAID`、确认币种为 MYR 的支付。
 */
export interface UsageAnalyticsPayments {
  period_basis: "paid_at";
  paid_count: number;
  /** 充值到账额：现金，不是收入。 */
  topup_amount: string;
  /** 已知（非空）的网关手续费之和。 */
  gateway_fee: string;
  /** 手续费为空的笔数：这些没有被扣，`gross_profit` 因此偏高。 */
  gateway_fee_unknown_count: number;
}

/** 覆盖全部分组的合计，不随分页变化。 */
export interface UsageAnalyticsReportTotals extends UsageAnalyticsAggregate {
  /** 带了 `project_id`、`provider`、`model`、`feature` 任一参数时为 null：手续费不能归属到这些维度。 */
  payments: UsageAnalyticsPayments | null;
  /** 用量毛利 − 手续费；`payments` 为 null 时 null。 */
  gross_profit: string | null;
  /** `gross_profit` ÷ 用量收入；收入为 0 或 `payments` 为 null 时 null。 */
  gross_profit_rate: string | null;
}

export interface UsageAnalyticsReport {
  /** 固定为 `estimated`：成本是处理时保存的估算值，不是对账后成本。 */
  cost_basis: "estimated";
  /** 固定为 `occurred_at`：用量收入、估算成本与用量毛利按事件发生时刻归期。 */
  usage_period_basis: "occurred_at";
  items: UsageAnalyticsItem[];
  page: number;
  page_size: number;
  total: number;
  totals: UsageAnalyticsReportTotals;
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
  setFilters(params, query);
  return apiGet<UsageAnalyticsReport>(`${USAGE_ANALYTICS_URL}?${params.toString()}`, signal);
}

/** 只放给了值的可选筛选（去掉首尾空白后为空的不放），顺序同 {@link FILTER_PARAMS}。 */
function setFilters(params: URLSearchParams, query: UsageAnalyticsQuery): void {
  for (const name of FILTER_PARAMS) {
    const value = query[name]?.trim();
    if (value !== undefined && value !== "") {
      params.set(name, value);
    }
  }
}

/**
 * 导出的条件：分析的那一组条件加可选的文件格式（AIH-TASK-240；设计闸门 #753 v1 §2「门户与管理端页面」）。
 * `format` 缺省 `csv`。
 */
export type UsageAnalyticsExportQuery = UsageAnalyticsQuery & { format?: ExportFormat };

/** 回退文件名按格式换扩展名：`csv` 与今天逐字相同。 */
export function usageAnalyticsExportFilename(format: ExportFormat = "csv"): string {
  return format === "csv" ? USAGE_ANALYTICS_EXPORT_FILENAME : USAGE_ANALYTICS_EXPORT_FILENAME_XLSX;
}

/**
 * 导出的地址（含查询串）：参数规则同 {@link getUsageAnalytics}，但没有 `page` / `page_size`（一次导出全部分组）。
 * `csv`（缺省）时与不带格式逐字相同，`xlsx` 时末尾加 `format=xlsx`。
 */
export function usageAnalyticsExportUrl(query: UsageAnalyticsExportQuery): string {
  const params = new URLSearchParams({
    group_by: query.group_by,
    occurred_from: query.occurred_from,
    occurred_to: query.occurred_to,
  });
  setFilters(params, query);
  return withExportFormat(`${USAGE_ANALYTICS_EXPORT_URL}?${params.toString()}`, query.format ?? "csv");
}

/**
 * 把这组条件下的全部分组与合计导出成一个 CSV（或 `format: "xlsx"` 时一个 xlsx）并交给浏览器下载。失败抛
 * `ApiError`：`EXPORT_TOO_LARGE`（超过 20 000 个分组）、`EXPORT_BUSY`（已有一个导出在进行），其余照常带
 * `request_id`；错误码不分格式。
 */
export function exportUsageAnalytics(query: UsageAnalyticsExportQuery, signal?: AbortSignal): Promise<void> {
  return downloadFile(usageAnalyticsExportUrl(query), usageAnalyticsExportFilename(query.format), signal);
}

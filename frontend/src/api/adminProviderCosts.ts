/**
 * 管理端供应商成本对账（契约见 docs/api.md「管理端供应商成本对账」，设计闸门 #755 v1 §2「管理端接口」，后端为 AIH-TASK-245）。
 *
 * 收录那一节的全部 4 个接口：月份总览、预览、确认与详情。每个都要 ADMIN，角色以后端数据库为准 —— 前端不做任何角色判断，
 * 403 原样交给页面显示。路径里的 id 是记录的 `public_id`，只来自后端，原样放进路径。
 *
 * ⚠️ 金额、汇率与比率全是**字符串**（INV-10），收发都不在这一层解析：`source_total` 原样作为 JSON 字符串发出，不转 number；
 * 回来的金额恰好 8 位小数，差额率与汇率去掉末尾 0。
 *
 * ⚠️ 预览的请求体恰好七个字段，多带任何字段（含 `expected_*`）都是 422；确认在这七个之外加预览返回的两个期望值。所以两个
 * 请求体都按字段逐个拼出来，不把调用方的对象整个转发。
 */

import { apiGet, client, toApiError, unwrapEnvelope, type ApiEnvelope } from "./client";

const RECONCILIATIONS_URL = "/api/v1/admin/provider-cost-reconciliations";
/** 与后端一样先于 `{reconciliation_id}` 写出：这两段都不是记录 id。 */
const MONTHS_URL = `${RECONCILIATIONS_URL}/months`;
const PREVIEW_URL = `${RECONCILIATIONS_URL}/preview`;

// --- 错误码（docs/api.md 本节「错误」） -------------------------------------------------

export const PROVIDER_COST_RECONCILIATION_NOT_FOUND = "PROVIDER_COST_RECONCILIATION_NOT_FOUND";
/** 不带 `supersedes_id` 而该月已有记录：重拉月份总览，对当前版本更正。 */
export const RECONCILIATION_EXISTS = "RECONCILIATION_EXISTS";
/** 被取代的记录已有后继：同样重拉月份总览。 */
export const RECONCILIATION_NOT_CURRENT = "RECONCILIATION_NOT_CURRENT";
/** 仅确认：预览之后有事件被处理，期望值对不上了。重新预览后再确认。 */
export const RECONCILIATION_ESTIMATE_CHANGED = "RECONCILIATION_ESTIMATE_CHANGED";

// --- 字段规则（docs/api.md 本节「预览」；后端的 422 才是准绳） ----------------------------

/** V1 只对账这一个供应商（设计 §2「管理端页面」）：常量，不调目录接口。 */
export const PROVIDER_CODES = ["anthropic"] as const;
export type ProviderCode = (typeof PROVIDER_CODES)[number];

/** 录入币种的下拉只有这两个（设计 §2「管理端页面」）。 */
export const SOURCE_CURRENCIES = ["USD", "MYR"] as const;
export type SourceCurrency = (typeof SOURCE_CURRENCIES)[number];

/** `source_total`：不带符号，整数至多 9 位、小数至多 8 位，允许 0。 */
export const SOURCE_TOTAL_PATTERN = /^[0-9]{1,9}(\.[0-9]{1,8})?$/;
/** 来源说明与更正理由：去首尾空白后 1–255 字符。 */
export const TEXT_FIELD_MAX = 255;

export const MONTH_STATUSES = ["RECONCILED", "NOT_RECONCILED"] as const;
export type MonthStatus = (typeof MONTH_STATUSES)[number];

// --- 对象 -----------------------------------------------------------------------
// 时间都是不带时区的 UTC，没有 `Z`（docs/api.md 本节「口径」）。

/** 一条对账记录，或一次预览的结果（预览时 `reconciliation_id`、`is_current`、`created_at`、`created_by_email` 为 `null`）。 */
export interface ReconciliationView {
  reconciliation_id: string | null;
  provider_code: string;
  scope: string;
  /** `YYYY-MM`，UTC 自然月。 */
  billing_month: string;
  period_basis: string;
  period_start: string;
  period_end: string;
  source_document_reference: string;
  source_currency: string;
  source_total: string;
  /** 汇率版本的 `public_id`；MYR 录入时与下面两个一起为 `null`。 */
  fx_rate_version_id: string | null;
  fx_rate_applied: string | null;
  fx_rate_moment: string | null;
  reconciled_cost_myr: string;
  estimated_event_count: number;
  estimated_source_cost: string;
  estimated_cost_myr: string;
  source_variance: string;
  /** 估算为 0 时 `null`。 */
  source_variance_rate: string | null;
  myr_variance: string;
  myr_variance_rate: string | null;
  exceeds_threshold: boolean;
  variance_threshold: string;
  unprocessed_event_count: number;
  outbox_unsettled_count: number;
  outbox_discarded_count: number;
  coverage_method: string;
  /** 被本行取代的那一行的 `public_id`；根记录为 `null`。 */
  supersedes_id: string | null;
  reason: string | null;
  is_current: boolean | null;
  created_at: string | null;
  created_by_email: string | null;
}

/** 月份总览的一项。`NOT_RECONCILED` 时 `current` 为 `null`（总览不给估算，估算在预览里看）。 */
export interface MonthItem {
  billing_month: string;
  period_start: string;
  period_end: string;
  status: MonthStatus;
  current: ReconciliationView | null;
}

export interface MonthsOverview {
  items: MonthItem[];
}

/** 按记录的供应商、期间与币种**现在**重算的估算。 */
export interface LiveEstimate {
  estimated_event_count: number;
  estimated_source_cost: string;
  estimated_cost_myr: string;
  currency_mismatch_count: number;
  unprocessed_event_count: number;
}

export interface ReconciliationDetail {
  reconciliation: ReconciliationView;
  /** 同一供应商、范围、月份的整条更正链，最早的在前，每项带 `is_current`。 */
  chain: ReconciliationView[];
  live_estimate: LiveEstimate;
  estimate_changed_since_confirmation: boolean;
}

// --- 请求体 ---------------------------------------------------------------------

/** 预览的七个字段。`supersedes_id` 与 `reason` 要么都给（更正），要么都是 `null`（首次对账）。 */
export interface PreviewBody {
  provider_code: string;
  billing_month: string;
  source_currency: string;
  /** 用户输入的原样字符串。 */
  source_total: string;
  source_document_reference: string;
  supersedes_id: string | null;
  reason: string | null;
}

/** 确认：预览的七个字段加上**预览返回的**两个期望值。 */
export interface ConfirmBody extends PreviewBody {
  expected_event_count: number;
  expected_estimated_source_cost: string;
}

// --- 查询键 ---------------------------------------------------------------------

export const PROVIDER_COSTS_QUERY_KEY = ["admin", "provider-costs"] as const;

export function providerCostMonthsQueryKey(providerCode: string) {
  return [...PROVIDER_COSTS_QUERY_KEY, "months", providerCode] as const;
}

export function providerCostDetailQueryKey(reconciliationId: string) {
  return [...PROVIDER_COSTS_QUERY_KEY, "detail", reconciliationId] as const;
}

// --- 地址与请求体 -----------------------------------------------------------------

function previewFields(body: PreviewBody): PreviewBody {
  return {
    provider_code: body.provider_code,
    billing_month: body.billing_month,
    source_currency: body.source_currency,
    source_total: body.source_total,
    source_document_reference: body.source_document_reference,
    supersedes_id: body.supersedes_id,
    reason: body.reason,
  };
}

async function post<T>(url: string, body: unknown): Promise<T> {
  try {
    const response = await client.post<ApiEnvelope<T>>(url, body);
    return unwrapEnvelope<T>(response.data);
  } catch (error) {
    throw toApiError(error);
  }
}

// --- 接口 -----------------------------------------------------------------------

/** 从首个对账月到最近一个已结束的 UTC 月，最近的在前，不分页（顺序由后端决定，前端不重排）。 */
export function getProviderCostMonths(providerCode: string, signal?: AbortSignal): Promise<MonthsOverview> {
  const query = new URLSearchParams({ provider_code: providerCode });
  return apiGet<MonthsOverview>(`${MONTHS_URL}?${query.toString()}`, signal);
}

/** 不写库。请求体恰好七个字段，不带 `expected_*`。 */
export function previewProviderCost(body: PreviewBody): Promise<ReconciliationView> {
  return post<ReconciliationView>(PREVIEW_URL, previewFields(body));
}

/** 201：写一行不可变记录与一条审计。期望值对不上是 409 `RECONCILIATION_ESTIMATE_CHANGED`。 */
export function confirmProviderCost(body: ConfirmBody): Promise<ReconciliationView> {
  return post<ReconciliationView>(RECONCILIATIONS_URL, {
    ...previewFields(body),
    expected_event_count: body.expected_event_count,
    expected_estimated_source_cost: body.expected_estimated_source_cost,
  });
}

/** 记录、整条更正链与实时估算。只读。 */
export function getProviderCostDetail(reconciliationId: string, signal?: AbortSignal): Promise<ReconciliationDetail> {
  return apiGet<ReconciliationDetail>(`${RECONCILIATIONS_URL}/${reconciliationId}`, signal);
}

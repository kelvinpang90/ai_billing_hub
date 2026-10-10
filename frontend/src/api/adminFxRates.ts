/**
 * 管理端汇率（契约见 docs/api.md「管理端汇率」，设计闸门 #183 v3，后端为 AIH-TASK-041）。
 *
 * 收录那一节的全部 8 个接口。每个都要 ADMIN，角色以后端数据库为准 —— 前端不做任何角色判断，
 * 403 原样交给页面显示。路径里的 id 一律是 `public_id`。
 *
 * `rate` = **1 单位 `base_currency` 等于多少 MYR**：BNM 中间价、吉隆坡中午场；**从发布时刻起生效**，
 * 不从报价日起算。自动拉取只写草稿，发布只经这里。
 *
 * ⚠️ `rate` 是**字符串**（INV-10），最多 10 位小数，收发都不在这一层解析：后端只收 JSON 字符串，
 * 回来的是去掉末尾 0 的精确十进制串。
 */

import type { Page } from "./adminCustomers";
import { apiGet, client, toApiError, unwrapEnvelope, type ApiEnvelope } from "./client";

const FX_RATES_URL = "/api/v1/admin/fx-rates";
/** 与后端一样先于 `{fx_rate_id}` 写出：不是一个版本 id。 */
const FETCH_ATTEMPTS_URL = `${FX_RATES_URL}/fetch-attempts`;

// --- 错误码（docs/api.md 本节「本节专有的错误码」；EFFECTIVE_FROM_* 见 adminProviderPrices.ts） ---

export const FX_RATE_NOT_FOUND = "FX_RATE_NOT_FOUND";
export const FX_RATE_NOT_DRAFT = "FX_RATE_NOT_DRAFT";
/** 编辑 BNM 草稿：要改就丢弃后手工录入。 */
export const FX_RATE_NOT_EDITABLE = "FX_RATE_NOT_EDITABLE";
export const FX_RATE_NOT_RETIRABLE = "FX_RATE_NOT_RETIRABLE";
export const FX_RATE_FINAL = "FX_RATE_FINAL";

// --- 字段规则（docs/api.md 本节「字段规则」；后端的 422 才是准绳） ----------------------

/** `rate`：不带符号，整数最多 14 位、小数最多 10 位。另须非零。 */
export const FX_RATE_PATTERN = /^[0-9]{1,14}(\.[0-9]{1,10})?$/;
/** 报价币种永远是 MYR；`base_currency` 不能是它。 */
export const QUOTE_CURRENCY = "MYR";

export const FX_RATE_STATUSES = ["DRAFT", "PUBLISHED", "RETIRED", "DISCARDED"] as const;
export type FxRateStatus = (typeof FX_RATE_STATUSES)[number];

export type FxRateSource = "BNM" | "MANUAL";

export const FETCH_OUTCOMES = ["NEW_DRAFT", "NO_NEW_QUOTE", "NO_QUOTE_FOR_DATE", "FAILED"] as const;
export type FetchOutcome = (typeof FETCH_OUTCOMES)[number];

// --- 对象 -----------------------------------------------------------------------
// 时间都是不带时区的 UTC，精确到秒；日期是吉隆坡日期（docs/api.md 本节「版本对象」）。

export interface FxRate {
  id: string;
  base_currency: string;
  quote_currency: string;
  /** 精确的十进制字符串，去掉末尾的 0。 */
  rate: string;
  source: FxRateSource;
  source_reference: string;
  /** BNM 的报价日；手工录入为 `null`。 */
  source_quote_date: string | null;
  observed_at: string;
  status: FxRateStatus;
  /** 草稿为 `null`；已发布时 `null` =「一直以来」。 */
  effective_from: string | null;
  /** `null` = 仍生效（或尚未发布）。与 `effective_from` 相等是空区间，永不生效。 */
  effective_to: string | null;
  /** BNM 草稿为 `null`（系统写入）。 */
  created_by_email: string | null;
  approved_by_email: string | null;
  approved_at: string | null;
  created_at: string;
  updated_at: string;
}

/** 一次 BNM 拉取。只读、不可单条引用，所以没有 `id`。 */
export interface FetchAttempt {
  base_currency: string;
  source: string;
  requested_date: string;
  outcome: FetchOutcome;
  quote_date: string | null;
  error_code: string | null;
  /** `NEW_DRAFT` 时是那条草稿的 `id`。 */
  fx_rate_id: string | null;
  attempted_at: string;
}

// --- 请求体 ---------------------------------------------------------------------

/** 手工草稿。`observed_at` 是带时区的 RFC 3339、整秒（见 `displayTimeToRfc3339`）。 */
export interface CreateFxRateBody {
  base_currency: string;
  rate: string;
  observed_at: string;
  source_reference: string;
}

/** 只带要改的字段，至少一个，都不许是 `null`。币种建后不可改。 */
export interface FxRatePatch {
  rate?: string;
  observed_at?: string;
  source_reference?: string;
}

// --- 查询条件 -------------------------------------------------------------------

export interface FxRateFilter {
  base_currency?: string;
  status?: FxRateStatus;
  source?: FxRateSource;
}

export interface FetchAttemptFilter {
  base_currency?: string;
  outcome?: FetchOutcome;
}

// --- 查询键 ---------------------------------------------------------------------

export const FX_RATES_QUERY_KEY = ["admin", "fx-rates"] as const;
/** 所有版本列表页：任何写操作之后让它们整体过期。 */
export const FX_RATE_LISTS_QUERY_KEY = [...FX_RATES_QUERY_KEY, "list"] as const;
export const FETCH_ATTEMPT_LISTS_QUERY_KEY = [...FX_RATES_QUERY_KEY, "fetch-attempts"] as const;

export function fxRateListQueryKey(filter: FxRateFilter, page: number, pageSize: number) {
  return [...FX_RATE_LISTS_QUERY_KEY, filter, page, pageSize] as const;
}

export function fetchAttemptListQueryKey(filter: FetchAttemptFilter, page: number, pageSize: number) {
  return [...FETCH_ATTEMPT_LISTS_QUERY_KEY, filter, page, pageSize] as const;
}

// --- 地址 -----------------------------------------------------------------------

// id 来自地址栏或列表，照样转义：不转义的话 `a/b` 会变成另一条路径。
function rateUrl(fxRateId: string): string {
  return `${FX_RATES_URL}/${encodeURIComponent(fxRateId)}`;
}

/** 分页参数加上筛选条件。⚠️ 没给的条件**不出现在查询串里**（空值与小写后端都回 422）。 */
function listQuery(page: number, pageSize: number, extra: Record<string, string | undefined>): string {
  const query = new URLSearchParams({ page: String(page), page_size: String(pageSize) });
  for (const [name, value] of Object.entries(extra)) {
    if (value !== undefined && value !== "") {
      query.set(name, value);
    }
  }
  return query.toString();
}

async function send<T>(method: "post" | "patch", url: string, body: unknown): Promise<T> {
  try {
    const response =
      method === "post"
        ? await client.post<ApiEnvelope<T>>(url, body)
        : await client.patch<ApiEnvelope<T>>(url, body);
    return unwrapEnvelope<T>(response.data);
  } catch (error) {
    throw toApiError(error);
  }
}

// --- 接口 -----------------------------------------------------------------------

/** 版本分页，最新建的在前（顺序由后端决定，前端不重排）。 */
export function listFxRates(
  filter: FxRateFilter,
  page: number,
  pageSize: number,
  signal?: AbortSignal,
): Promise<Page<FxRate>> {
  const query = listQuery(page, pageSize, {
    base_currency: filter.base_currency,
    status: filter.status,
    source: filter.source,
  });
  return apiGet<Page<FxRate>>(`${FX_RATES_URL}?${query}`, signal);
}

/** ⚠️ **不幂等**：重发会建出两个草稿，丢弃多余的即可。防双击是调用方的责任。 */
export function createFxRate(body: CreateFxRateBody): Promise<FxRate> {
  return send<FxRate>("post", FX_RATES_URL, body);
}

/** BNM 拉取记录，`attempted_at` 倒序。 */
export function listFxFetchAttempts(
  filter: FetchAttemptFilter,
  page: number,
  pageSize: number,
  signal?: AbortSignal,
): Promise<Page<FetchAttempt>> {
  const query = listQuery(page, pageSize, {
    base_currency: filter.base_currency,
    outcome: filter.outcome,
  });
  return apiGet<Page<FetchAttempt>>(`${FETCH_ATTEMPTS_URL}?${query}`, signal);
}

export function getFxRate(fxRateId: string, signal?: AbortSignal): Promise<FxRate> {
  return apiGet<FxRate>(rateUrl(fxRateId), signal);
}

/** 只对手工来源的草稿；BNM 草稿是 409 `FX_RATE_NOT_EDITABLE`。 */
export function updateFxRate(fxRateId: string, patch: FxRatePatch): Promise<FxRate> {
  return send<FxRate>("patch", rateUrl(fxRateId), patch);
}

/** 发布。`effectiveFrom` 不给就是「不指定」，请求体是 `{}`。 */
export function publishFxRate(fxRateId: string, effectiveFrom?: string): Promise<FxRate> {
  const body = effectiveFrom === undefined ? {} : { effective_from: effectiveFrom };
  return send<FxRate>("post", `${rateUrl(fxRateId)}/publish`, body);
}

/** 退役当前版本，或撤销一个尚未开始的预约。`reason` 记在审计上。 */
export function retireFxRate(fxRateId: string, reason: string): Promise<FxRate> {
  return send<FxRate>("post", `${rateUrl(fxRateId)}/retire`, { reason });
}

/** 草稿（手工与 BNM 都可以）→ `DISCARDED`，行留着。请求体是 `{}`。 */
export function discardFxRate(fxRateId: string): Promise<FxRate> {
  return send<FxRate>("post", `${rateUrl(fxRateId)}/discard`, {});
}

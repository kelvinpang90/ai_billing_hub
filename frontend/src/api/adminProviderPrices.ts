/**
 * 管理端供应商价格（契约见 docs/api.md「管理端供应商价格」，设计闸门 #177 v4，后端为 AIH-TASK-026）。
 *
 * 收录那一节的全部 7 个接口。每个都要 ADMIN，角色以后端数据库为准 —— 前端不做任何角色判断，
 * 403 原样交给页面显示。路径里的 id 一律是 `public_id`。
 *
 * **成本价，原币种，客户不可见**：只有这一组管理端接口。
 *
 * ⚠️ `unit_quantity` 与 `rate_amount` 是**字符串**（INV-10），收发都不在这一层解析：后端只收
 * JSON 字符串（JSON 数字是 422），回来的是恰好 8 位小数的字符串。一旦变成 `number`，
 * `"0.10000001"` 这类值就已经不是用户输入的那个数了。
 */

import type { Page } from "./adminCustomers";
import { apiGet, client, toApiError, unwrapEnvelope, type ApiEnvelope } from "./client";

const PRICES_URL = "/api/v1/admin/provider-prices";

// --- 错误码（docs/api.md 本节「本节专有的错误码」） ---------------------------------

export const PRICE_VERSION_NOT_FOUND = "PRICE_VERSION_NOT_FOUND";
export const USAGE_METER_COMPONENT_NOT_FOUND = "USAGE_METER_COMPONENT_NOT_FOUND";
export const PRICE_VERSION_NOT_DRAFT = "PRICE_VERSION_NOT_DRAFT";
/** 发布时完整性不满足；`message` 列出缺的分量代码。 */
export const PRICE_VERSION_INCOMPLETE = "PRICE_VERSION_INCOMPLETE";
/** 供应商、模型或某个分量所属的计量类型已停用。 */
export const CATALOG_ITEM_RETIRED = "CATALOG_ITEM_RETIRED";
export const PRICE_VERSION_NOT_RETIRABLE = "PRICE_VERSION_NOT_RETIRABLE";
/** 对已退役 / 已丢弃的版本做任何写操作。 */
export const PRICE_VERSION_FINAL = "PRICE_VERSION_FINAL";
/** 价格与汇率共用。 */
export const EFFECTIVE_FROM_CONFLICT = "EFFECTIVE_FROM_CONFLICT";
/** 价格与汇率共用：请求的生效时刻早于服务端的下一个整秒（不许回溯）。 */
export const EFFECTIVE_FROM_IN_PAST = "EFFECTIVE_FROM_IN_PAST";

// --- 字段规则 -------------------------------------------------------------------
//
// 与后端 `app/schemas/provider_prices.py` 是同一组规则（docs/api.md 本节「字段规则」）。
// 前端校验只是提前告诉用户；后端的 422 才是准绳。

/** `unit_quantity` 与 `rate_amount`：不带符号，整数最多 12 位、小数最多 8 位。另须非零。 */
export const PRICE_DECIMAL_PATTERN = /^[0-9]{1,12}(\.[0-9]{1,8})?$/;
/** ISO 4217 大写三字母（`usd` 是 422）。 */
export const CURRENCY_PATTERN = /^[A-Z]{3}$/;
/** `source_reference` 与退役的 `reason`：去首尾空白后 1–255。 */
export const TEXT_MAX = 255;
/** 一个版本最多带多少个分量。 */
export const MAX_COMPONENTS = 64;

export const PRICE_VERSION_STATUSES = ["DRAFT", "PUBLISHED", "RETIRED", "DISCARDED"] as const;
export type PriceVersionStatus = (typeof PRICE_VERSION_STATUSES)[number];

// --- 对象 -----------------------------------------------------------------------
// 时间都是不带时区的 UTC，精确到秒（docs/api.md「时间」）。

export interface PriceComponent {
  component_code: string;
  /** 分量所属的计量类型与它的单位：`unit_quantity` 个这种单位对应一个 `rate_amount`。 */
  meter_type_code: string;
  unit: string;
  /** 恰好 8 位小数的十进制字符串。 */
  unit_quantity: string;
  /** 原币种单价，恰好 8 位小数的十进制字符串。 */
  rate_amount: string;
  /** 只作备注，计价不读它。 */
  metadata: Record<string, unknown> | null;
  created_at: string;
}

export interface PriceVersion {
  id: string;
  provider_id: string;
  provider_code: string;
  model_id: string;
  model_code: string;
  source_currency: string;
  source_type: string;
  source_reference: string;
  status: PriceVersionStatus;
  /** 草稿为 `null`；已发布时 `null` =「一直以来」。 */
  effective_from: string | null;
  /** `null` = 仍生效（或尚未发布）。与 `effective_from` 相等是空区间，永不生效。 */
  effective_to: string | null;
  /** `component_code` 升序（由后端决定，前端不重排）。 */
  components: PriceComponent[];
  created_by_email: string | null;
  approved_by_email: string | null;
  created_at: string;
  updated_at: string;
  approved_at: string | null;
}

// --- 请求体 ---------------------------------------------------------------------
// 所有请求体都拒绝多余字段（422），这里的类型也不给别的字段留位置。

export interface ComponentRateBody {
  component_code: string;
  unit_quantity: string;
  rate_amount: string;
  /** 可选。不带就不出现在请求体里。 */
  metadata?: Record<string, unknown>;
}

export interface CreatePriceBody {
  provider_id: string;
  model_id: string;
  source_currency: string;
  source_reference: string;
  components: ComponentRateBody[];
}

/** 只带要改的字段，至少一个；`components` 是整体替换。供应商与模型建后不可改。 */
export interface PricePatch {
  source_currency?: string;
  source_reference?: string;
  components?: ComponentRateBody[];
}

// --- 查询条件 -------------------------------------------------------------------

export interface PriceFilter {
  provider_id?: string;
  model_id?: string;
  status?: PriceVersionStatus;
}

// --- 查询键 ---------------------------------------------------------------------

export const PROVIDER_PRICES_QUERY_KEY = ["admin", "provider-prices"] as const;
/** 所有列表页：任何写操作之后让它们整体过期。 */
export const PROVIDER_PRICE_LISTS_QUERY_KEY = [...PROVIDER_PRICES_QUERY_KEY, "list"] as const;

export function providerPriceListQueryKey(filter: PriceFilter, page: number, pageSize: number) {
  return [...PROVIDER_PRICE_LISTS_QUERY_KEY, filter, page, pageSize] as const;
}

export function providerPriceDetailQueryKey(priceVersionId: string) {
  return [...PROVIDER_PRICES_QUERY_KEY, "detail", priceVersionId] as const;
}

// --- 地址 -----------------------------------------------------------------------

// id 来自地址栏，不能信它只含 uuid 的字符：不转义的话 `a/b` 会变成另一条路径。
function priceUrl(priceVersionId: string): string {
  return `${PRICES_URL}/${encodeURIComponent(priceVersionId)}`;
}

/** 分页参数加上筛选条件。⚠️ 没给的条件**不出现在查询串里**（`status=` 这种空值后端回 422）。 */
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

/** 按供应商 `code`、模型 `code`、`effective_from`（`null` 在前）排序，每项带分量。 */
export function listProviderPrices(
  filter: PriceFilter,
  page: number,
  pageSize: number,
  signal?: AbortSignal,
): Promise<Page<PriceVersion>> {
  const query = listQuery(page, pageSize, {
    provider_id: filter.provider_id,
    model_id: filter.model_id,
    status: filter.status,
  });
  return apiGet<Page<PriceVersion>>(`${PRICES_URL}?${query}`, signal);
}

/** ⚠️ **不幂等**：重发会建出两个草稿（草稿不影响计费，丢弃多余的即可）。防双击是调用方的责任。 */
export function createProviderPrice(body: CreatePriceBody): Promise<PriceVersion> {
  return send<PriceVersion>("post", PRICES_URL, body);
}

export function getProviderPrice(priceVersionId: string, signal?: AbortSignal): Promise<PriceVersion> {
  return apiGet<PriceVersion>(priceUrl(priceVersionId), signal);
}

export function updateProviderPrice(priceVersionId: string, patch: PricePatch): Promise<PriceVersion> {
  return send<PriceVersion>("patch", priceUrl(priceVersionId), patch);
}

/**
 * 发布。`effectiveFrom` 是带时区的 RFC 3339、整秒（见 `displayTimeToRfc3339`）；不给就是「不指定」，
 * 请求体是 `{}` —— 不发 `effective_from: null`。已发布再发布：200，什么都不写。
 */
export function publishProviderPrice(priceVersionId: string, effectiveFrom?: string): Promise<PriceVersion> {
  const body = effectiveFrom === undefined ? {} : { effective_from: effectiveFrom };
  return send<PriceVersion>("post", `${priceUrl(priceVersionId)}/publish`, body);
}

/** 退役当前版本，或撤销一个尚未开始的预约。`reason` 记在审计上。 */
export function retireProviderPrice(priceVersionId: string, reason: string): Promise<PriceVersion> {
  return send<PriceVersion>("post", `${priceUrl(priceVersionId)}/retire`, { reason });
}

/** 草稿 → `DISCARDED`，行留着。请求体是 `{}`。 */
export function discardProviderPrice(priceVersionId: string): Promise<PriceVersion> {
  return send<PriceVersion>("post", `${priceUrl(priceVersionId)}/discard`, {});
}

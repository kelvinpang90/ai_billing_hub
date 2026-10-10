/**
 * 管理端用量事件（契约见 docs/api.md「管理端用量事件：查询」（AIH-TASK-034）与「管理端用量事件：重新入队」
 * （AIH-TASK-032，设计闸门 #181 v2 §2））。
 *
 * 收录两节的全部 4 个接口：列表、详情、单个重新入队、批量重新入队。每个都要 ADMIN，角色以后端数据库为准 ——
 * 前端不做任何角色判断，403 原样交给页面显示。路径里的 id 一律是事件的 `public_id`。
 *
 * **成本、计费额与毛利只出现在这两节的管理端接口里**（INV-7）：这里的类型只给管理端页面用。
 *
 * ⚠️ 金额、汇率与数量是**字符串**（INV-10），收发都不在这一层解析：一旦变成 `number`，
 * `"0.10000001"` 这类值就已经不是后端给的那个数了。
 *
 * 另有客户详情「内部用量」面板用的 {@link recentInternalUsage}（AIH-TASK-032 之前就在这里），保持不变。
 */

import type { Page } from "./adminCustomers";
import { apiGet, client, toApiError, unwrapEnvelope, type ApiEnvelope } from "./client";

const USAGE_EVENTS_URL = "/api/v1/admin/usage-events";
/** 批量重新入队。与后端一样是固定段，不是一个事件 id。 */
const BULK_REQUEUE_URL = `${USAGE_EVENTS_URL}/requeue`;

// --- 错误码（docs/api.md 两节「本节专有的错误码」） ----------------------------------

export const USAGE_EVENT_NOT_FOUND = "USAGE_EVENT_NOT_FOUND";
/** 单个重新入队：事件不在四个错误状态之一，或账本里已有它的行。 */
export const USAGE_EVENT_NOT_REQUEUABLE = "USAGE_EVENT_NOT_REQUEUABLE";
// 批量重新入队的 `customer_id` 不存在时是 404 `CUSTOMER_NOT_FOUND`（常量在 adminCustomers.ts）；
// 列表查询里不存在的客户只是空页。

// --- 状态（spec §83 的九个取值，顺序同 app/models/usage.py 的 UsageEventStatus） -----------

export const USAGE_EVENT_STATUSES = [
  "RECEIVED",
  "PROCESSING",
  "PROCESSED",
  "PRICING_ERROR",
  "FX_RATE_ERROR",
  "MODEL_UNKNOWN",
  "IDEMPOTENCY_CONFLICT",
  "FAILED_RETRYABLE",
  "FAILED_FINAL",
] as const;
export type UsageEventStatus = (typeof USAGE_EVENT_STATUSES)[number];

/**
 * 可以重新入队的四个错误状态（批量的 `status` 只收它们）。
 *
 * 单个事件还要「账本里没有它的行」—— `LEDGER_CONFLICT` 的 `FAILED_FINAL` 就有，后端回 409。
 */
export const REQUEUABLE_STATUSES = ["MODEL_UNKNOWN", "PRICING_ERROR", "FX_RATE_ERROR", "FAILED_FINAL"] as const;
export type RequeuableStatus = (typeof REQUEUABLE_STATUSES)[number];

export function isRequeuableStatus(status: string): status is RequeuableStatus {
  return (REQUEUABLE_STATUSES as readonly string[]).includes(status);
}

/** 批量一次至多处理这么多条（按接收顺序），多的再调一次。 */
export const BULK_REQUEUE_LIMIT = 1000;

// --- 对象 -----------------------------------------------------------------------
// 时间都是不带时区的 UTC；`occurred_at` 保留上报的小数秒，其余整秒（docs/api.md「列表项」）。

export interface UsageEventSummary {
  id: string;
  /** 集成方上报的幂等键。 */
  event_id: string;
  customer_id: string;
  customer_company_name: string;
  project_id: string;
  request_id: string;
  conversation_id: string | null;
  /** 上报的原始字符串（模型未知的事件也有）。 */
  provider: string;
  model: string;
  usage_type: string;
  /** `LLM_TOKEN_FIELDS` 形态四个 token 数是整数、`quantity` 为 null；`QUANTITY` 形态相反。 */
  input_tokens: number | null;
  output_tokens: number | null;
  cache_creation_input_tokens: number | null;
  cache_read_input_tokens: number | null;
  /** 8 位小数的十进制字符串。 */
  quantity: string | null;
  unit: string;
  status: string;
  error_code: string | null;
  occurred_at: string;
  received_at: string;
  processed_at: string | null;
  /** 含税、从钱包扣的额（MYR，8 位小数）；未计费为 null。 */
  billable_cost: string | null;
  estimated_provider_cost_myr: string | null;
  billing_mode_snapshot: string | null;
  /** 仅内部计量事件有值：模拟客户售价，不是应收或收入。 */
  reference_customer_price: string | null;
}

/** 那一行 `AI_USAGE` 账本：金额 = −计费额。 */
export interface UsageLedgerEntry {
  id: string;
  amount: string;
}

/** 撞了这个 `event_id` 的冲突请求。 */
export interface UsageEventConflict {
  /** 请求方的公开 key。 */
  api_key: string;
  /** `OWNERSHIP` / `FINGERPRINT` / `BOTH`。 */
  mismatch: string;
  received_at: string;
}

export interface UsageEventDetail extends UsageEventSummary {
  schema_version: string;
  payload_shape: string;
  quantity_kind: string;
  payload_fingerprint: string;
  /** 计费失败时的异常类型名（不含载荷）。 */
  error_message: string | null;
  created_at: string;
  provider_ref_id: string | null;
  model_ref_id: string | null;
  provider_price_version_id: string | null;
  pricing_rule_id: string | null;
  /** MYR 原币时为 null。 */
  fx_rate_version_id: string | null;
  /** 去掉尾零的精确十进制字符串；MYR 原币时为 null。 */
  fx_rate_applied: string | null;
  provider_source_currency: string | null;
  provider_source_cost: string | null;
  /** 预付事件 = 计费额 − 估算成本（可为负）。内部计量及未计费事件为 null。 */
  gross_margin: string | null;
  gross_margin_basis: "estimated" | null;
  wallet_transaction: UsageLedgerEntry | null;
  attempt_count: number;
  next_attempt_at: string | null;
  /** 只有 `PROCESSING` 时非空。 */
  claim_token: string | null;
  claimed_at: string | null;
  lease_expires_at: string | null;
  conflicts: UsageEventConflict[];
}

export interface RequeueResult {
  id: string;
  status: string;
}

export interface BulkRequeueResult {
  requeued: number;
  skipped: number;
  /** 放回了的事件的 `public_id`，按内部 id 升序。 */
  ids: string[];
}

// --- 查询条件与请求体 ---------------------------------------------------------------

/** 查询参数名，顺序即查询串里的顺序。 */
const FILTER_PARAMS = [
  "customer_id",
  "project_id",
  "conversation_id",
  "request_id",
  "provider",
  "model",
  "occurred_from",
  "occurred_to",
  "status",
  "error_code",
] as const;

/**
 * 列表筛选，全部可省略、同时给的按 AND 组合。`occurred_from`（含）/ `occurred_to`（不含）是**带时区**的
 * RFC 3339、整秒（从吉隆坡时间换算见 components/DateTimeText.tsx 的 `displayTimeToRfc3339`）。
 */
export type UsageEventFilters = Partial<Record<(typeof FILTER_PARAMS)[number], string>>;

/** 批量请求体里可选的条件，顺序即请求体里的顺序。 */
const BULK_OPTIONAL_FIELDS = ["error_code", "customer_id", "provider", "model", "occurred_from", "occurred_to"] as const;

/** 批量重新入队：`status` 与 `reason` 必填，其余可省略。多余字段后端 422。 */
export type BulkRequeueBody = {
  status: RequeuableStatus;
  reason: string;
} & Partial<Record<(typeof BULK_OPTIONAL_FIELDS)[number], string>>;

// --- 查询键 ---------------------------------------------------------------------

export const USAGE_EVENTS_QUERY_KEY = ["admin", "usage-events"] as const;
/** 所有列表页：重新入队之后让它们整体过期。 */
export const USAGE_EVENT_LISTS_QUERY_KEY = [...USAGE_EVENTS_QUERY_KEY, "list"] as const;

export function usageEventListQueryKey(filters: UsageEventFilters, page: number, pageSize: number) {
  return [...USAGE_EVENT_LISTS_QUERY_KEY, filters, page, pageSize] as const;
}

export function usageEventDetailQueryKey(usageEventId: string) {
  return [...USAGE_EVENTS_QUERY_KEY, "detail", usageEventId] as const;
}

// --- 地址 -----------------------------------------------------------------------

// id 来自地址栏或列表，照样转义：不转义的话 `a/b` 会变成另一条路径。
function eventUrl(usageEventId: string): string {
  return `${USAGE_EVENTS_URL}/${encodeURIComponent(usageEventId)}`;
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

/**
 * 事件分页，按内部 id 倒序（≈ 最新接收的在前，由后端决定，前端不重排）。
 *
 * ⚠️ 省略的、以及去掉首尾空白后为空的条件**不出现在查询串里**：后端对 `request_id=` 这种空值是按空串
 * 逐字节比较，结果是一张莫名其妙的空列表。
 */
export function listUsageEvents(
  filters: UsageEventFilters,
  page: number,
  pageSize: number,
  signal?: AbortSignal,
): Promise<Page<UsageEventSummary>> {
  const query = new URLSearchParams({ page: String(page), page_size: String(pageSize) });
  for (const name of FILTER_PARAMS) {
    const value = filters[name]?.trim();
    if (value !== undefined && value !== "") {
      query.set(name, value);
    }
  }
  return apiGet<Page<UsageEventSummary>>(`${USAGE_EVENTS_URL}?${query.toString()}`, signal);
}

export function getUsageEvent(usageEventId: string, signal?: AbortSignal): Promise<UsageEventDetail> {
  return apiGet<UsageEventDetail>(eventUrl(usageEventId), signal);
}

/** 单个错误事件改回 `RECEIVED`。`reason` 写进审计。不在可重新入队的状态时 409。 */
export function requeueUsageEvent(usageEventId: string, reason: string): Promise<RequeueResult> {
  return post<RequeueResult>(`${eventUrl(usageEventId)}/requeue`, { reason });
}

/**
 * 按条件批量重新入队，至多 {@link BULK_REQUEUE_LIMIT} 条。省略的、以及为空的可选条件**不出现在请求体里**
 * （后端对空串是 422，对 `null` 也不收）。
 */
export function bulkRequeueUsageEvents(body: BulkRequeueBody): Promise<BulkRequeueResult> {
  const sent: Record<string, string> = { status: body.status };
  for (const name of BULK_OPTIONAL_FIELDS) {
    const value = body[name]?.trim();
    if (value !== undefined && value !== "") {
      sent[name] = value;
    }
  }
  sent["reason"] = body.reason;
  return post<BulkRequeueResult>(BULK_REQUEUE_URL, sent);
}

// --- 客户详情「内部用量」面板 ------------------------------------------------------------

export interface InternalUsageEvent {
  id: string;
  occurred_at: string;
  model: string;
  status: string;
  estimated_provider_cost_myr: string | null;
  reference_customer_price: string | null;
  billable_cost: string | null;
}

interface UsagePage {
  items: InternalUsageEvent[];
  total: number;
}

export function recentInternalUsage(customerId: string, signal?: AbortSignal): Promise<UsagePage> {
  const query = new URLSearchParams({ customer_id: customerId, page: "1", page_size: "20" });
  return apiGet<UsagePage>(`${USAGE_EVENTS_URL}?${query.toString()}`, signal);
}

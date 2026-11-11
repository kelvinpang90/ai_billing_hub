/**
 * 管理端支付（契约见 docs/api.md「支付（管理端）」，AIH-TASK-183；设计闸门 #585 v3 §2「管理端接口」「管理端页面」）。
 *
 * 三个接口：支付列表（按状态、客户、创建日期筛）、支付详情（带关联的回调事件）、复核结案。要 ADMIN，角色以后端数据库
 * 为准 —— 前端不做任何角色判断，403 原样交给页面显示。列表与详情只读；结案只对 `REVIEW_REQUIRED` 的支付，结案后是终态。
 * 列表页在 features/payments/PaymentsPage.tsx；详情与结案由 AIH-TASK-185 的详情页使用。
 *
 * **手续费、状态原因与人工结案信息只在这几个管理端接口里**（INV-7）：这里的类型只给管理端页面用。
 *
 * ⚠️ 金额是**字符串**（INV-10），这一层不解析。`payment_id` 只来自列表的响应或地址栏，**原样**放进路径：不校验形状、
 * 不改大小写 —— 没有这笔支付时后端回 404 `PAYMENT_NOT_FOUND`，那才是准绳。
 */

import type { Page } from "./adminCustomers";
import { apiGet, client, toApiError, unwrapEnvelope, type ApiEnvelope } from "./client";

const PAYMENTS_URL = "/api/v1/admin/payments";

// --- 错误码（docs/api.md「支付（管理端）」的「错误」） --------------------------------------

/** 详情与结案：没有这笔支付。 */
export const PAYMENT_NOT_FOUND = "PAYMENT_NOT_FOUND";
/** 结案：支付不是 `REVIEW_REQUIRED`，或已被别人结案。 */
export const PAYMENT_NOT_IN_REVIEW = "PAYMENT_NOT_IN_REVIEW";
/** 结案（只有 `CREDIT`）：开关关闭，或 Stripe 凭据读不到。 */
export const PAYMENTS_MISCONFIGURED = "PAYMENTS_MISCONFIGURED";
/** 结案（只有 `CREDIT`）：Stripe 暂时不可用。 */
export const PAYMENT_GATEWAY_UNAVAILABLE = "PAYMENT_GATEWAY_UNAVAILABLE";

// --- 状态（六种，大小写敏感；别的值后端 422） ------------------------------------------------

export const PAYMENT_STATUSES = ["PENDING", "PAID", "FAILED", "EXPIRED", "CANCELLED", "REVIEW_REQUIRED"] as const;
export type PaymentStatus = (typeof PAYMENT_STATUSES)[number];

export function isPaymentStatus(value: unknown): value is PaymentStatus {
  return typeof value === "string" && (PAYMENT_STATUSES as readonly string[]).includes(value);
}

/** 创建日期筛选的形状 `YYYY-MM-DD`（只认 ASCII 数字）。是不是真实存在的日期、年份范围由后端判（422）。 */
export const CREATED_DATE_PATTERN = /^[0-9]{4}-[0-9]{2}-[0-9]{2}$/;

// --- 对象 -----------------------------------------------------------------------
// 时间都是不带时区的 UTC，精确到秒（docs/api.md「时间」）。

/** 列表项：恰好这 14 个键。 */
export interface PaymentSummary {
  /** 支付的对外 id（`public_id`）；详情与结案用它。 */
  id: string;
  customer_id: string;
  /** 客户当前的公司名。 */
  customer_name: string;
  /** 建单时客户要付的含税毛额（`expected_amount`），恰好 8 位小数的字符串。 */
  amount: string;
  currency: string;
  gateway: string;
  /** Stripe Checkout Session id；建单时 Stripe 调用失败留下的单为 `null`。 */
  gateway_reference: string | null;
  /** Stripe 的实际手续费；拿不到时为 `null`。 */
  gateway_fee: string | null;
  status: string;
  /** 状态原因码，取值不固定（例如 `AMOUNT_MISMATCH`）；没有为 `null`。 */
  status_reason: string | null;
  /** 人工结案的方式：`CREDITED` / `CLOSED`；未结案为 `null`。 */
  resolution: string | null;
  created_at: string;
  paid_at: string | null;
  review_required_at: string | null;
}

/** 详情里的一条回调事件：恰好这 7 个键。 */
export interface PaymentEvent {
  gateway_event_id: string;
  event_type: string;
  processing_status: string;
  last_error_code: string | null;
  received_at: string;
  processed_at: string | null;
  fingerprint_conflict_at: string | null;
}

/** 详情 = 列表项的 14 个键再加 10 个。 */
export interface PaymentDetail extends PaymentSummary {
  /** Stripe 报的金额与币种；没有为 `null`（金额不为空时币种也可能为空）。 */
  confirmed_amount: string | null;
  confirmed_currency: string | null;
  /** 实际入账额（这笔支付的 `TOPUP` 账本行金额）；没有入账为 `null`。 */
  credited_amount: string | null;
  gateway_payment_intent_id: string | null;
  expires_at: string;
  last_reconciled_at: string | null;
  resolved_at: string | null;
  /** 结案管理员的邮箱；未结案为 `null`。 */
  resolved_by: string | null;
  resolution_reason: string | null;
  /** 按 `received_at` 升序，最多 100 条。 */
  events: PaymentEvent[];
}

/** 列表的筛选条件。没给的**不出现在查询串里**。日期是马来西亚时间的日历日 `YYYY-MM-DD`，含两端，原样发出。 */
export interface PaymentFilter {
  status?: PaymentStatus;
  customer_id?: string;
  created_from?: string;
  created_to?: string;
}

/** 结案动作：`CREDIT`（按实付入账）或 `CLOSE`（不入账结案）。 */
export type ResolveAction = "CREDIT" | "CLOSE";

/** 结案请求体：恰好这两个字段，别的字段后端 422。`reason` 去掉首尾空白后 1–255 字。 */
export interface ResolvePaymentBody {
  action: ResolveAction;
  reason: string;
}

// --- 查询键 ---------------------------------------------------------------------

export const PAYMENTS_QUERY_KEY = ["admin", "payments"] as const;
/** 所有列表页的前缀：结案之后让它们整体过期。 */
export const PAYMENT_LISTS_QUERY_KEY = [...PAYMENTS_QUERY_KEY, "list"] as const;

export function paymentListQueryKey(filter: PaymentFilter, page: number, pageSize: number) {
  return [...PAYMENT_LISTS_QUERY_KEY, filter, page, pageSize] as const;
}

export function paymentDetailQueryKey(paymentId: string) {
  return [...PAYMENTS_QUERY_KEY, "detail", paymentId] as const;
}

// --- 接口 -----------------------------------------------------------------------

/** 查询参数名，顺序即查询串里 `page` / `page_size` 之后的顺序。 */
const FILTER_PARAMS = ["status", "customer_id", "created_from", "created_to"] as const;

/** 支付分页，`created_at` 倒序（顺序由后端决定，前端不重排）。 */
export function listPayments(
  filter: PaymentFilter,
  page: number,
  pageSize: number,
  signal?: AbortSignal,
): Promise<Page<PaymentSummary>> {
  const query = new URLSearchParams({ page: String(page), page_size: String(pageSize) });
  for (const name of FILTER_PARAMS) {
    const value = filter[name];
    if (value !== undefined) {
      query.set(name, value);
    }
  }
  return apiGet<Page<PaymentSummary>>(`${PAYMENTS_URL}?${query.toString()}`, signal);
}

export function getPayment(paymentId: string, signal?: AbortSignal): Promise<PaymentDetail> {
  return apiGet<PaymentDetail>(`${PAYMENTS_URL}/${paymentId}`, signal);
}

/**
 * 复核结案。成功返回结案后的支付详情（与 {@link getPayment} 同样的字段）。请求体只带 `action` 与 `reason`，
 * 调用方多给的字段不会被发出去。
 *
 * ⚠️ 这里**不自动重试**：`CREDIT` 的入账金额以提交时 Stripe 的实收为准，要不要重发由管理员决定。
 */
export async function resolvePayment(paymentId: string, body: ResolvePaymentBody): Promise<PaymentDetail> {
  try {
    const response = await client.post<ApiEnvelope<PaymentDetail>>(`${PAYMENTS_URL}/${paymentId}/resolve`, {
      action: body.action,
      reason: body.reason,
    });
    return unwrapEnvelope<PaymentDetail>(response.data);
  } catch (error) {
    throw toApiError(error);
  }
}

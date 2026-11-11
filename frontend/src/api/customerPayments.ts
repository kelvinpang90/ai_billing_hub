/**
 * 客户自己的支付（契约见 docs/api.md「支付（客户）」与「客户建单」；设计闸门 #587 v3 §2「客户接口」「门户页面」）。
 *
 * 四个接口：充值选项（`GET /api/v1/customer/topup-options`）、支付列表（`GET /api/v1/customer/payments`）、一笔支付
 * （`GET …/payments/{payment_id}`）与建单（`POST /api/v1/customer/topups`，AIH-TASK-177）。前三个总是注册、只读；
 * 建单只在支付开关打开时存在。充值页在 features/portal/TopupPage.tsx；列表与详情由 AIH-TASK-193 的支付历史与付款结果页复用。
 *
 * ⚠️ **不带任何租户或客户标识**：客户是谁只取自访问令牌。这一层也不接受这类参数。
 *
 * 列表是**页码**分页（`page` / `page_size`，见 docs/api.md「分页」）：查询串写法与 `customerStatements.ts` 的
 * `listCustomerStatements` 相同，响应是 `adminCustomers.ts` 的 {@link Page}。
 *
 * ⚠️ 金额是**恰好两位小数的字符串**（INV-10），这一层不解析。`payment_id` 只来自列表、建单的响应或地址栏，**原样**放进
 * 路径：不校验形状、不改大小写 —— 不存在、属于别的客户或根本不像 uuid 时后端回同一个 404 `PAYMENT_NOT_FOUND`，那才是准绳。
 */

import type { Page } from "./adminCustomers";
import { apiGet, client, toApiError, unwrapEnvelope, type ApiEnvelope } from "./client";

export const CUSTOMER_TOPUP_OPTIONS_URL = "/api/v1/customer/topup-options";
export const CUSTOMER_PAYMENTS_URL = "/api/v1/customer/payments";
export const CUSTOMER_TOPUPS_URL = "/api/v1/customer/topups";

// --- 错误码 ---------------------------------------------------------------------

/** 详情：不存在、属于别的客户或格式不像 uuid（几种情形响应相同）。 */
export const PAYMENT_NOT_FOUND = "PAYMENT_NOT_FOUND";
/** 建单：金额低于最小恢复额（`effective_minimum`）。收到它时重新读一次充值选项。 */
export const TOPUP_BELOW_RECOVERY_MINIMUM = "TOPUP_BELOW_RECOVERY_MINIMUM";

// --- 不可充值的原因（按后端判定的顺序） ---------------------------------------------

export const TOPUP_UNAVAILABLE_REASONS = [
  "PAYMENTS_DISABLED",
  "ACCOUNT_READ_ONLY",
  "TOPUPS_NOT_OPEN",
  "TOPUP_NOT_ALLOWED",
  "RECOVERY_EXCEEDS_MAXIMUM",
] as const;
export type TopupUnavailableReason = (typeof TOPUP_UNAVAILABLE_REASONS)[number];

// --- 对象 -----------------------------------------------------------------------
// 时间都是不带时区的 UTC，精确到秒（docs/api.md「时间」）。

/** 充值选项：恰好这 8 个键。不可充值时金额字段照样返回。 */
export interface TopupOptions {
  /** 恒为 `MYR`。 */
  currency: string;
  available: boolean;
  /** `available` 为真时为 `null`，否则是 {@link TOPUP_UNAVAILABLE_REASONS} 之一。 */
  unavailable_reason: string | null;
  min_amount: string;
  /** 没设为 `null`（此时单笔上限是金额正则限住的 `999999.99`）。 */
  max_amount: string | null;
  /** 1–8 个，升序。 */
  preset_amounts: string[];
  /** 最小恢复额：只对余额 ≤ 0 的预付客户有值。 */
  recovery_minimum: string | null;
  /** 生效的最低额 `max(min_amount, recovery_minimum)`：建单的金额不能低于它。 */
  effective_minimum: string;
}

/** 客户侧状态（由支付状态与人工结案方式映射，不是支付的原始状态）。 */
export const CUSTOMER_PAYMENT_STATUSES = [
  "PENDING",
  "PAID",
  "IN_REVIEW",
  "NOT_CREDITED",
  "FAILED",
  "EXPIRED",
  "CANCELLED",
] as const;
export type CustomerPaymentStatus = (typeof CUSTOMER_PAYMENT_STATUSES)[number];

/** 列表项与详情：恰好这 8 个键。 */
export interface CustomerPayment {
  /** 支付的对外 id，即建单时得到的 `payment_id`。 */
  id: string;
  amount: string;
  currency: string;
  status: string;
  /** 实际入账额；没有入账为 `null`。人工按实付入账时可能不等于 `amount`。 */
  credited_amount: string | null;
  created_at: string;
  paid_at: string | null;
  expires_at: string;
}

/** 建单请求体：只有 `amount` 一个字段，金额是字符串（`^[0-9]{1,6}(\.[0-9]{1,2})?$`）。 */
export interface CreateTopupBody {
  amount: string;
}

/** 建单响应：恰好这 3 个键。 */
export interface CreatedTopup {
  payment_id: string;
  /** Stripe 托管付款页的地址：原样跳过去，不拼接。 */
  redirect_url: string;
  expires_at: string;
}

// --- 查询键 ---------------------------------------------------------------------

/** 客户支付相关的 key 都以它开头。 */
export const CUSTOMER_PAYMENTS_QUERY_KEY = ["customer", "payments"] as const;

export const CUSTOMER_TOPUP_OPTIONS_QUERY_KEY = [...CUSTOMER_PAYMENTS_QUERY_KEY, "topup-options"] as const;

export function customerPaymentListQueryKey(page: number, pageSize: number) {
  return [...CUSTOMER_PAYMENTS_QUERY_KEY, "list", page, pageSize] as const;
}

export function customerPaymentDetailQueryKey(paymentId: string) {
  return [...CUSTOMER_PAYMENTS_QUERY_KEY, "detail", paymentId] as const;
}

// --- 接口 -----------------------------------------------------------------------

export function getTopupOptions(signal?: AbortSignal): Promise<TopupOptions> {
  return apiGet<TopupOptions>(CUSTOMER_TOPUP_OPTIONS_URL, signal);
}

/** 自己的支付分页，`created_at` 倒序（顺序由后端决定，前端不重排）。 */
export function listCustomerPayments(
  page: number,
  pageSize: number,
  signal?: AbortSignal,
): Promise<Page<CustomerPayment>> {
  const query = new URLSearchParams({ page: String(page), page_size: String(pageSize) });
  return apiGet<Page<CustomerPayment>>(`${CUSTOMER_PAYMENTS_URL}?${query.toString()}`, signal);
}

export function getCustomerPayment(paymentId: string, signal?: AbortSignal): Promise<CustomerPayment> {
  return apiGet<CustomerPayment>(`${CUSTOMER_PAYMENTS_URL}/${paymentId}`, signal);
}

/**
 * 建单。请求体只带 `amount`，调用方多给的字段不会被发出去。
 *
 * ⚠️ 这里**不自动重试**：要不要再点由客户决定（60 秒内同金额的重试由后端复用同一笔）。
 */
export async function createTopup(body: CreateTopupBody): Promise<CreatedTopup> {
  try {
    const response = await client.post<ApiEnvelope<CreatedTopup>>(CUSTOMER_TOPUPS_URL, { amount: body.amount });
    return unwrapEnvelope<CreatedTopup>(response.data);
  } catch (error) {
    throw toApiError(error);
  }
}

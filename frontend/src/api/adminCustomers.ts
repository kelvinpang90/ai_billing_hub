/**
 * 管理端客户管理（docs/api.md「管理端客户管理」，spec §56、§124）。
 *
 * 只收已上线的四个接口：建客户、客户列表、客户详情、编辑客户。项目、调账、凭据
 * 各归各的任务。
 *
 * ⚠️ 金额是**字符串**（INV-10）：`wallet.balance` 从这里原样交给 `MoneyText`，
 * 中间不许 `parseFloat` / `Number()`。类型写成 `string` 就是为了让转换在类型上
 * 显得扎眼。
 *
 * ⚠️ 响应里只有契约列出的字段：没有 `account_status`、低余额阈值、成本或毛利。
 * 这里也**不预留**它们 —— 预留的可选字段会被当成「后端某天会给」而被渲染出去。
 */

import { ApiError, apiGet, client, toApiError, unwrapEnvelope, type ApiEnvelope } from "./client";

/** spec §108 的分页边界，与后端 `app/schemas/customers.py` 一致。超出范围后端回 422。 */
export const DEFAULT_PAGE_SIZE = 20;
export const MAX_PAGE_SIZE = 100;
export const MAX_PAGE = 10_000;

/** 本组接口专有与会分支处理的错误码。 */
export const CUSTOMER_NOT_FOUND = "CUSTOMER_NOT_FOUND";
export const ADMIN_REQUIRED = "ADMIN_REQUIRED";
export const VALIDATION_ERROR = "VALIDATION_ERROR";

/** 由余额驱动：余额 > 0 是 ACTIVE，≤ 0 是 SUSPENDED。前端只展示，从不设置。 */
export type BillingStatus = "ACTIVE" | "SUSPENDED";

export interface CustomerWallet {
  currency: string;
  /** 恰好 8 位小数的十进制字符串，例如 `"0.00000000"`。 */
  balance: string;
  version: number;
}

/** 客户列表项：客户详情去掉 `wallet`。 */
export interface CustomerSummary {
  id: string;
  company_name: string;
  contact_name: string | null;
  email: string;
  phone: string | null;
  billing_status: BillingStatus;
  status_version: number;
  /** 不带时区的 UTC，例如 `"2026-09-20T08:30:00"`。展示走 `DateTimeText`。 */
  created_at: string;
  updated_at: string;
}

export interface CustomerDetail extends CustomerSummary {
  wallet: CustomerWallet;
}

export interface Page<T> {
  items: T[];
  page: number;
  page_size: number;
  total: number;
}

export interface CreateCustomerBody {
  company_name: string;
  email: string;
  contact_name?: string | null;
  phone?: string | null;
}

/**
 * 编辑客户的请求体：**只放实际改了的字段**。
 *
 * `company_name` / `email` 不能是 `null`（后端 422）；`contact_name` / `phone`
 * 的 `null` 表示清空。
 */
export interface CustomerPatch {
  company_name?: string;
  email?: string;
  contact_name?: string | null;
  phone?: string | null;
}

const CUSTOMERS_URL = "/api/v1/admin/customers";

function customerUrl(customerId: string): string {
  return `${CUSTOMERS_URL}/${encodeURIComponent(customerId)}`;
}

/** 列表与详情分两个前缀：改完一个客户只让列表失效，不把刚写进缓存的详情也作废。 */
export const CUSTOMER_LIST_QUERY_KEY = ["admin", "customers", "list"] as const;

export function customerListQueryKey(page: number, pageSize: number) {
  return [...CUSTOMER_LIST_QUERY_KEY, page, pageSize] as const;
}

export function customerDetailQueryKey(customerId: string) {
  return ["admin", "customers", "detail", customerId] as const;
}

const FINAL_CODES: readonly string[] = [ADMIN_REQUIRED, CUSTOMER_NOT_FOUND, VALIDATION_ERROR];

/**
 * 读请求要不要重试。
 *
 * 403、404、422 是确定的答案，重试只会让用户多等一秒再看到同一句话；其余的
 * 照 App 的默认只重试一次。
 */
export function shouldRetryCustomerQuery(failureCount: number, error: Error): boolean {
  if (error instanceof ApiError && FINAL_CODES.includes(error.code)) {
    return false;
  }
  return failureCount < 1;
}

/** 最新在前（后端的顺序，前端不再排序）。 */
export function listCustomers(
  page: number,
  pageSize: number,
  signal?: AbortSignal,
): Promise<Page<CustomerSummary>> {
  const query = new URLSearchParams({ page: String(page), page_size: String(pageSize) });
  return apiGet<Page<CustomerSummary>>(`${CUSTOMERS_URL}?${query.toString()}`, signal);
}

export function getCustomer(customerId: string, signal?: AbortSignal): Promise<CustomerDetail> {
  return apiGet<CustomerDetail>(customerUrl(customerId), signal);
}

async function write<T>(send: () => Promise<{ data: ApiEnvelope<T> }>): Promise<T> {
  try {
    const response = await send();
    return unwrapEnvelope<T>(response.data);
  } catch (error) {
    throw toApiError(error);
  }
}

/**
 * ⚠️ **不幂等**：同一请求体提交两次得到两个客户。调用方必须在请求进行中挡住
 * 第二次提交。
 */
export function createCustomer(body: CreateCustomerBody): Promise<CustomerDetail> {
  return write(() => client.post<ApiEnvelope<CustomerDetail>>(CUSTOMERS_URL, body));
}

export function updateCustomer(customerId: string, patch: CustomerPatch): Promise<CustomerDetail> {
  return write(() => client.patch<ApiEnvelope<CustomerDetail>>(customerUrl(customerId), patch));
}

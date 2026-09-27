/**
 * 管理端客户管理（docs/api.md「管理端客户管理」）。
 *
 * 只有四个接口：建客户、列表、详情、编辑。字段与错误码以 docs/api.md 为准，
 * 那里改了这里跟着改。
 *
 * ⚠️ 金额是**字符串**（INV-10）。类型里写成 `string` 不是图省事：写成 `number`
 * 的话，JSON 里本来就是字符串的值会被 TS 当成数字用，一次 `+` 就成了字符串拼接，
 * 一次 `toFixed` 就成了浮点舍入。展示一律走 `components/MoneyText`。
 *
 * ⚠️ 响应里**没有**账户状态、低余额阈值、成本与毛利，这里也不预留：多一个可选
 * 字段，就会有人把它画到页面上，而它永远是 `undefined`。
 */

import { apiGet, client, toApiError, unwrapEnvelope, type ApiEnvelope } from "./client";

/** 路径里的客户不存在（docs/api.md「管理端客户管理」的专有错误码）。 */
export const CUSTOMER_NOT_FOUND = "CUSTOMER_NOT_FOUND";
/** 令牌有效，但调用者在数据库里不是 ADMIN（docs/api.md「通用错误码」）。 */
export const ADMIN_REQUIRED = "ADMIN_REQUIRED";

/** 分页的默认与上限，与后端 `app/schemas/customers.py` 一致（spec §108）。 */
export const DEFAULT_PAGE_SIZE = 20;
export const MAX_PAGE_SIZE = 100;
export const MAX_PAGE = 10_000;

export interface CustomerWallet {
  currency: string;
  /** 恰好 8 位小数的十进制字符串，如 `"0.00000000"`。 */
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
  /** `ACTIVE` 或 `SUSPENDED`，由余额驱动。 */
  billing_status: string;
  status_version: number;
  /** 不带时区的 UTC，精确到秒。 */
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

/** 部分更新：只带要改的字段。`contact_name` / `phone` 给 `null` 表示清空。 */
export interface UpdateCustomerPatch {
  company_name?: string;
  email?: string;
  contact_name?: string | null;
  phone?: string | null;
}

export const CUSTOMERS_QUERY_KEY = ["admin", "customers"] as const;
export const CUSTOMER_LIST_QUERY_KEY = [...CUSTOMERS_QUERY_KEY, "list"] as const;

export function customerListQueryKey(page: number, pageSize: number) {
  return [...CUSTOMER_LIST_QUERY_KEY, page, pageSize] as const;
}

export function customerQueryKey(customerId: string) {
  return [...CUSTOMERS_QUERY_KEY, "detail", customerId] as const;
}

const BASE = "/api/v1/admin/customers";

/** 客户 id 进路径前转义：它来自地址栏，不能假定是规规矩矩的 uuid。 */
function customerUrl(customerId: string): string {
  return `${BASE}/${encodeURIComponent(customerId)}`;
}

export function listCustomers(
  page: number,
  pageSize: number,
  signal?: AbortSignal,
): Promise<Page<CustomerSummary>> {
  const query = new URLSearchParams({ page: String(page), page_size: String(pageSize) });
  return apiGet<Page<CustomerSummary>>(`${BASE}?${query.toString()}`, signal);
}

export function getCustomer(customerId: string, signal?: AbortSignal): Promise<CustomerDetail> {
  return apiGet<CustomerDetail>(customerUrl(customerId), signal);
}

/**
 * 建客户。
 *
 * ⚠️ **不幂等**：同一请求体提交两次得到两个客户。防双击由调用方负责。
 */
export async function createCustomer(body: CreateCustomerBody): Promise<CustomerDetail> {
  try {
    const response = await client.post<ApiEnvelope<CustomerDetail>>(BASE, body);
    return unwrapEnvelope<CustomerDetail>(response.data);
  } catch (error) {
    throw toApiError(error);
  }
}

export async function updateCustomer(
  customerId: string,
  patch: UpdateCustomerPatch,
): Promise<CustomerDetail> {
  try {
    const response = await client.patch<ApiEnvelope<CustomerDetail>>(
      customerUrl(customerId),
      patch,
    );
    return unwrapEnvelope<CustomerDetail>(response.data);
  } catch (error) {
    throw toApiError(error);
  }
}

/**
 * 管理端客户管理（契约见 docs/api.md「管理端客户管理」，设计闸门 #96）。
 *
 * 只收录前端已经用到的六个接口：建客户、列表、详情、编辑，以及某个客户的建项目与
 * 项目列表（AIH-TASK-016）。每个都要 ADMIN，
 * 角色以后端数据库为准 —— 前端不做任何角色判断，403 原样交给页面显示。
 *
 * ⚠️ 金额是**字符串**（INV-10）。这里的类型刻意写成 `string`，不在这一层解析：
 * 一旦变成 `number`，`"0.10000001"` 这类值就已经不是后端给的那个数了。
 */

import { apiGet, client, toApiError, unwrapEnvelope, type ApiEnvelope } from "./client";

const CUSTOMERS_URL = "/api/v1/admin/customers";

/** 路径里的客户不存在（也包括拿别的实体的 id 来查）。 */
export const CUSTOMER_NOT_FOUND = "CUSTOMER_NOT_FOUND";
/** 令牌有效，但调用者在数据库里不是 ADMIN。 */
export const ADMIN_REQUIRED = "ADMIN_REQUIRED";

/** spec §108：`page_size` 默认 20、上限 100；`page` 上限 10000。超出范围后端回 422。 */
export const DEFAULT_PAGE_SIZE = 20;
export const MAX_PAGE_SIZE = 100;
export const MAX_PAGE = 10_000;

/** 由余额驱动：余额 > 0 为 ACTIVE，≤ 0 为 SUSPENDED。前端只显示，从不发送。 */
export type BillingStatus = "ACTIVE" | "SUSPENDED";
export type BillingMode = "PREPAID" | "INTERNAL_METERED_ONLY";

/** 客户列表项。时间是不带时区的 UTC（docs/api.md「时间」）。 */
export interface CustomerSummary {
  id: string;
  company_name: string;
  contact_name: string | null;
  email: string;
  phone: string | null;
  billing_status: BillingStatus;
  billing_mode: BillingMode;
  ai_service_enabled: boolean;
  status_version: number;
  created_at: string;
  updated_at: string;
}

export interface Wallet {
  currency: string;
  /** 恰好 8 位小数的十进制字符串，例如 `"0.00000000"`。 */
  balance: string;
  version: number;
}

/** 客户详情 = 列表项 + 钱包。 */
export interface CustomerDetail extends CustomerSummary {
  wallet: Wallet;
}

export interface Page<T> {
  items: T[];
  page: number;
  page_size: number;
  total: number;
}

/** 可选字段不带就是 `null`；后端把只含空白的值也存成 `null`。 */
export interface CreateCustomerBody {
  company_name: string;
  email: string;
  contact_name?: string;
  phone?: string;
}

/**
 * 部分更新：只带要改的字段。
 *
 * `company_name` / `email` 不能是 `null`；`contact_name` / `phone` 发 `null` 表示清空。
 * 空对象 `{}` 后端回 422，所以调用方在没有改动时根本不该调 {@link updateCustomer}。
 */
export interface CustomerPatch {
  company_name?: string;
  email?: string;
  contact_name?: string | null;
  phone?: string | null;
}

/** 项目。`description` 可为 `null`（后端把只含空白的描述也存成 `null`）。 */
export interface Project {
  id: string;
  name: string;
  description: string | null;
  created_at: string;
  updated_at: string;
}

/**
 * 建项目的请求体。项目归属只来自路径里的客户 —— 请求体里带 `customer_id` /
 * `tenant_id` 之类后端一律回 422，这里的类型也不给它们留位置。
 */
export interface CreateProjectBody {
  name: string;
  description?: string;
}

export const CUSTOMERS_QUERY_KEY = ["admin", "customers"] as const;
/** 所有列表页的前缀：建客户、改客户之后让它们整体过期。 */
export const CUSTOMER_LISTS_QUERY_KEY = [...CUSTOMERS_QUERY_KEY, "list"] as const;

export function customerListQueryKey(page: number, pageSize: number) {
  return [...CUSTOMER_LISTS_QUERY_KEY, page, pageSize] as const;
}

export function customerDetailQueryKey(customerId: string) {
  return [...CUSTOMERS_QUERY_KEY, "detail", customerId] as const;
}

/** 一个客户的所有项目列表页的前缀：建项目之后让它们整体过期。 */
export function customerProjectsQueryKey(customerId: string) {
  return [...customerDetailQueryKey(customerId), "projects"] as const;
}

export function projectListQueryKey(customerId: string, page: number, pageSize: number) {
  return [...customerProjectsQueryKey(customerId), page, pageSize] as const;
}

function customerUrl(customerId: string): string {
  // id 来自地址栏，不能信它只含 uuid 的字符：不转义的话 `a/b` 会变成另一条路径。
  return `${CUSTOMERS_URL}/${encodeURIComponent(customerId)}`;
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

/** 客户分页，最新在前（顺序由后端决定，前端不重排）。 */
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

/** ⚠️ **不幂等**：提交两次就是两个客户。防双击是调用方的责任。 */
export function createCustomer(body: CreateCustomerBody): Promise<CustomerDetail> {
  return send<CustomerDetail>("post", CUSTOMERS_URL, body);
}

export function updateCustomer(customerId: string, patch: CustomerPatch): Promise<CustomerDetail> {
  return send<CustomerDetail>("patch", customerUrl(customerId), patch);
}

/** 这个客户的项目分页，**最早在前**（顺序由后端决定，前端不重排）。 */
export function listProjects(
  customerId: string,
  page: number,
  pageSize: number,
  signal?: AbortSignal,
): Promise<Page<Project>> {
  const query = new URLSearchParams({ page: String(page), page_size: String(pageSize) });
  return apiGet<Page<Project>>(`${customerUrl(customerId)}/projects?${query.toString()}`, signal);
}

/** ⚠️ **不幂等**：提交两次就是两个项目。防双击是调用方的责任。 */
export function createProject(customerId: string, body: CreateProjectBody): Promise<Project> {
  return send<Project>("post", `${customerUrl(customerId)}/projects`, body);
}

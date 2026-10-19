/**
 * 管理端客户管理（契约见 docs/api.md「管理端客户管理」，设计闸门 #96）。
 *
 * 只收录前端已经用到的七个接口：建客户、列表、详情、编辑、改账户状态（AIH-TASK-082，契约见
 * docs/api.md「管理端账户状态」），以及某个客户的建项目与项目列表（AIH-TASK-016）；项目的集成状态与
 * 状态 webhook 地址在 `projectIntegration.ts`。每个都要 ADMIN，
 * 角色以后端数据库为准 —— 前端不做任何角色判断，403 原样交给页面显示。
 *
 * ⚠️ 金额是**字符串**（INV-10）。这里的类型刻意写成 `string`，不在这一层解析：
 * 一旦变成 `number`，`"0.10000001"` 这类值就已经不是后端给的那个数了。
 */

import { AxiosError } from "axios";

import { ApiError, apiGet, client, toApiError, unwrapEnvelope, type ApiEnvelope } from "./client";

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

/**
 * 管理员控制的账户生命周期，与 `billing_status` 互相独立（AIH-TASK-020）。现在只会出现
 * `ENABLED` / `DISABLED`；另两个值没有路径会产生，但类型按全部取值写全。
 */
export type AccountStatus = "PENDING_ACTIVATION" | "ENABLED" | "DISABLED" | "CLOSED";
/** 能作为目标的只有这两个：`PENDING_ACTIVATION` 与 `CLOSED` 作目标后端回 422。 */
export type AccountStatusTarget = "ENABLED" | "DISABLED";

/** 当前状态是 `CLOSED`（终态），任何目标都回 409。 */
export const ACCOUNT_STATUS_TRANSITION_INVALID = "ACCOUNT_STATUS_TRANSITION_INVALID";

/** 客户列表项。时间是不带时区的 UTC（docs/api.md「时间」）。 */
export interface CustomerSummary {
  id: string;
  company_name: string;
  contact_name: string | null;
  email: string;
  phone: string | null;
  billing_status: BillingStatus;
  /**
   * 后端总会带这个字段（docs/api.md「客户对象」）。类型上写成可选，与项目的 `integration_status`
   * 同一个理由：缺了的时候前端显示「未知」、不给启用 / 停用按钮，不猜默认值。
   */
  account_status?: AccountStatus;
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

/** 项目的集成状态（AIH-TASK-050）。`DISABLED` 时 `effective-status` 返回 `BLOCK_AI`，且不推送状态 webhook。 */
export type IntegrationStatus = "ENABLED" | "DISABLED";

/** 项目。`description` 可为 `null`（后端把只含空白的描述也存成 `null`）。 */
export interface Project {
  id: string;
  name: string;
  description: string | null;
  /**
   * 后端总会带这两个字段（docs/api.md「项目对象」）。类型上写成可选：缺了的时候前端显示「未知」，
   * 不猜默认值 —— 猜成 `ENABLED` 就是在替后端说话。
   */
  integration_status?: IntegrationStatus;
  /** 是否设了状态 webhook 地址。⚠️ 项目对象里没有地址本身，完整地址只在 `…/status-webhook` 里。 */
  status_webhook_configured?: boolean;
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

/**
 * 改账户状态的请求体：只有这两个字段。客户只来自路径；`status_version`、`billing_status` 之类
 * 后端一律回 422，这里的类型也不给它们留位置。`reason` 去掉首尾空白后 1–255，只写业务说明。
 */
export interface AccountStatusBody {
  account_status: AccountStatusTarget;
  reason: string;
}

/**
 * 改账户状态的错误：一个带 HTTP 状态的 {@link ApiError}。没有响应（连不上、超时）时状态是 `null`。
 * 状态要留着：404 / 409 / 422 是后端明确拒绝，5xx 与网络错误则可以原样重发（见 {@link postAccountStatus}）。
 */
export class AccountStatusError extends ApiError {
  constructor(
    code: string,
    message: string,
    requestId: string | null,
    readonly status: number | null,
  ) {
    super(code, message, requestId);
    this.name = "AccountStatusError";
  }
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

/**
 * 把客户的账户状态改成 `body.account_status`。200 时返回改后的客户详情；目标等于当前状态时后端
 * 什么都不写、返回当前详情（docs/api.md「管理端账户状态」的语义一节），所以原样重发是安全的。
 *
 * 失败一律抛 {@link AccountStatusError}（它是 `ApiError`）。
 *
 * ⚠️ 这里**不自动重试**：要不要重发由弹窗交给管理员决定。
 */
export async function postAccountStatus(
  customerId: string,
  body: AccountStatusBody,
): Promise<CustomerDetail> {
  let status: number | null = null;
  try {
    const response = await client.post<ApiEnvelope<CustomerDetail>>(
      `${customerUrl(customerId)}/account-status`,
      body,
    );
    status = response.status;
    return unwrapEnvelope<CustomerDetail>(response.data);
  } catch (error) {
    if (error instanceof AxiosError && error.response !== undefined) {
      status = error.response.status;
    }
    const apiError = toApiError(error);
    throw new AccountStatusError(apiError.code, apiError.message, apiError.requestId, status);
  }
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

/**
 * 管理端项目集成状态与状态 webhook 地址（契约见 docs/api.md「管理端项目集成状态与状态 webhook 地址」，
 * 设计闸门 #241 v6，全文 docs/design/PHASE-3-status-sync.md §2「接口」、§4）。
 *
 * 三个接口都在 `/api/v1/admin/customers/{customer_id}/projects/{project_id}` 下：
 * 改集成状态（POST `…/integration-status`）、设置或清除地址（PUT `…/status-webhook`）、读取地址
 * （GET `…/status-webhook`）。三个都幂等：目标与当前相同时 200、什么都不写，所以失败后原样再提交一次是安全的。
 *
 * ⚠️ 完整地址**只出现在 `…/status-webhook` 的响应里**（路径里可能带集成方的令牌）：项目对象只有
 * `status_webhook_configured`。这一层不打日志，地址也不放进查询键之外的任何地方。
 */

import { AxiosError } from "axios";

import { customerProjectsQueryKey, type IntegrationStatus, type Project } from "./adminCustomers";
import { ApiError, apiGet, client, toApiError, unwrapEnvelope, type ApiEnvelope } from "./client";

/** 项目不存在，或不属于路径里的客户（两者一模一样）。 */
export const PROJECT_NOT_FOUND = "PROJECT_NOT_FOUND";
/** 客户已关户：改集成状态（任何目标，含与当前相同）或设为一个地址时。清除地址不受限。什么都没写。 */
export const ACCOUNT_CLOSED = "ACCOUNT_CLOSED";
/** 请求体不合法，或地址不合规。`message` 不回显地址的任何部分。什么都没写。 */
export const VALIDATION_ERROR = "VALIDATION_ERROR";

export interface IntegrationStatusBody {
  /** 大小写敏感。 */
  integration_status: IntegrationStatus;
  /** 去掉首尾空白后 1–255。只进审计；只写业务说明，不写个人数据。 */
  reason: string;
}

export interface StatusWebhookBody {
  /** 字符串：后端规范化并校验，不合规 422；`null`：清除。字段本身必须有。 */
  url: string | null;
}

/** 地址对象：PUT 与 GET 的响应。 */
export interface StatusWebhook {
  /** 规范化后的地址（`https://<小写主机名><path>[?query]`）；没设时为 `null`。 */
  status_webhook_url: string | null;
  /** `status_webhook_url` 不为 `null`。 */
  status_webhook_configured: boolean;
}

/** 一个项目的状态 webhook 地址。放在这个客户的项目前缀下，与凭据、签名密钥并列。 */
export function statusWebhookQueryKey(customerId: string, projectId: string) {
  return [...customerProjectsQueryKey(customerId), projectId, "status-webhook"] as const;
}

/**
 * 是不是这个客户的项目列表页（`projectListQueryKey` 的形状：项目前缀 + 页码 + 每页条数）。
 *
 * 写操作之后**只**让列表页与地址查询过期：凭据与签名密钥的查询也挂在同一个前缀下，按前缀整体
 * 失效会让它们无故重读。
 */
export function isProjectListQueryKey(customerId: string, queryKey: readonly unknown[]): boolean {
  const prefix = customerProjectsQueryKey(customerId);
  return (
    queryKey.length === prefix.length + 2 &&
    prefix.every((part, index) => queryKey[index] === part) &&
    typeof queryKey[prefix.length] === "number" &&
    typeof queryKey[prefix.length + 1] === "number"
  );
}

/**
 * 这三个接口的错误：一个带 HTTP 状态的 {@link ApiError}。没有响应时状态是 `null`。
 *
 * 与凭据同一种写法（`CredentialError`）。三个接口都幂等，所以不需要「结果未知」的判定：
 * 不知道写没写上时，原样再提交一次即可。
 */
export class ProjectIntegrationError extends ApiError {
  constructor(
    code: string,
    message: string,
    requestId: string | null,
    readonly status: number | null,
  ) {
    super(code, message, requestId);
    this.name = "ProjectIntegrationError";
  }
}

function projectUrl(customerId: string, projectId: string): string {
  // 与 adminCustomers 同一个理由：不转义的话 `a/b` 会变成另一条路径。
  return (
    `/api/v1/admin/customers/${encodeURIComponent(customerId)}` +
    `/projects/${encodeURIComponent(projectId)}`
  );
}

function statusWebhookUrl(customerId: string, projectId: string): string {
  return `${projectUrl(customerId, projectId)}/status-webhook`;
}

async function send<T>(method: "post" | "put", url: string, body: object): Promise<T> {
  let status: number | null = null;
  try {
    const response =
      method === "post"
        ? await client.post<ApiEnvelope<T>>(url, body)
        : await client.put<ApiEnvelope<T>>(url, body);
    status = response.status;
    return unwrapEnvelope<T>(response.data);
  } catch (error) {
    if (error instanceof AxiosError && error.response !== undefined) {
      status = error.response.status;
    }
    // ⚠️ 不打日志：请求体与响应里有完整地址，axios 的错误对象也挂着它们。
    const apiError = toApiError(error);
    throw new ProjectIntegrationError(apiError.code, apiError.message, apiError.requestId, status);
  }
}

/** 改项目集成状态。成功（含目标与当前相同）返回项目对象。 */
export function setIntegrationStatus(
  customerId: string,
  projectId: string,
  integrationStatus: IntegrationStatus,
  reason: string,
): Promise<Project> {
  const body: IntegrationStatusBody = { integration_status: integrationStatus, reason };
  return send<Project>("post", `${projectUrl(customerId, projectId)}/integration-status`, body);
}

/** 读取地址。只读，不写审计。 */
export function getStatusWebhook(
  customerId: string,
  projectId: string,
  signal?: AbortSignal,
): Promise<StatusWebhook> {
  return apiGet<StatusWebhook>(statusWebhookUrl(customerId, projectId), signal);
}

/**
 * 设置（字符串）或清除（`null`）地址。地址原样发出：规范化与校验只在后端做，不合规 422。
 * `url` 一定带上 —— 缺这个字段后端回 422，`null` 才表示清除。
 */
export function setStatusWebhook(
  customerId: string,
  projectId: string,
  url: string | null,
): Promise<StatusWebhook> {
  const body: StatusWebhookBody = { url };
  return send<StatusWebhook>("put", statusWebhookUrl(customerId, projectId), body);
}

/**
 * 管理端出站 webhook 签名密钥（契约见 docs/api.md「管理端出站 webhook 签名密钥」，设计闸门 #135，
 * 全文 docs/design/AIH-TASK-019-webhook-signing.md）。
 *
 * 四个接口都在 `/api/v1/admin/customers/{customer_id}/projects/{project_id}/webhook-secrets` 下：
 * 签发、列表、启用一个版本、退役一个版本。每个项目任何时刻至多一个 `ACTIVE`、至多一个 `PENDING`。
 *
 * ⚠️ `secret` **只出现一次**：只在签发的 201 响应里。这一层只把它原样交给调用方，
 * 不缓存、不打日志、不放进任何 URL。调用方同样不许把它写进查询缓存或浏览器存储
 * （见 `WebhookSigningPanel`）。
 */

import { AxiosError } from "axios";

import { customerProjectsQueryKey, type Page } from "./adminCustomers";
import { ApiError, apiGet, client, toApiError, unwrapEnvelope, type ApiEnvelope } from "./client";

/** 签发时已有一个 `PENDING` 版本（先启用或退役它）。什么都没写。 */
export const WEBHOOK_SECRET_PENDING_EXISTS = "WEBHOOK_SECRET_PENDING_EXISTS";
/** 启用一个 `RETIRED` 版本。什么都没写。 */
export const WEBHOOK_SECRET_NOT_PENDING = "WEBHOOK_SECRET_NOT_PENDING";
/** 该项目没有这个 `key_version`。 */
export const WEBHOOK_SECRET_NOT_FOUND = "WEBHOOK_SECRET_NOT_FOUND";
/** 主密钥未配置：只影响签发。什么都没写。 */
export const ENCRYPTION_NOT_CONFIGURED = "ENCRYPTION_NOT_CONFIGURED";
/** 服务没有配置数据库。什么都没写。 */
export const DATABASE_NOT_CONFIGURED = "DATABASE_NOT_CONFIGURED";

/** `PENDING`（已签发、还没用它签名）/ `ACTIVE`（平台此刻用它签名）/ `RETIRED`（终态）。 */
export type WebhookSecretStatus = "PENDING" | "ACTIVE" | "RETIRED";

/** 签名版本对象。时间是不带时区的 UTC（docs/api.md「时间」）。 */
export interface WebhookSecret {
  /** 从 1 起，只增不减；出站请求头 `X-Acuven-Key-Version` 里带的就是它。 */
  key_version: number;
  status: WebhookSecretStatus;
  created_at: string;
  /** 没启用过时为 `null`（从 `PENDING` 直接退役的也没有）。 */
  activated_at: string | null;
  retired_at: string | null;
}

/**
 * 签发的版本（签发的响应）= 版本对象 + `secret`。
 *
 * ⚠️ `secret` 之后任何接口都不再返回。
 */
export interface IssuedWebhookSecret extends WebhookSecret {
  secret: string;
}

export interface RetireBody {
  /** 去掉首尾空白后 1–255。进审计；只写业务说明，不写个人数据。 */
  reason: string;
}

/** 一个项目的所有签名密钥列表页的前缀：任何写操作之后让它们整体过期。 */
export function projectWebhookSecretsQueryKey(customerId: string, projectId: string) {
  return [...customerProjectsQueryKey(customerId), projectId, "webhook-secrets"] as const;
}

export function webhookSecretListQueryKey(
  customerId: string,
  projectId: string,
  page: number,
  pageSize: number,
) {
  return [...projectWebhookSecretsQueryKey(customerId, projectId), page, pageSize] as const;
}

/**
 * 签名密钥接口的错误：一个带 HTTP 状态的 {@link ApiError}。没有响应时状态是 `null`。
 *
 * 与凭据同一个理由（`CredentialError`）：要分清「后端明确拒绝了」与「不知道写没写上」。
 */
export class WebhookSigningError extends ApiError {
  constructor(
    code: string,
    message: string,
    requestId: string | null,
    readonly status: number | null,
  ) {
    super(code, message, requestId);
    this.name = "WebhookSigningError";
  }
}

/** docs/api.md 为这四个接口列出的、什么都没写的 4xx。 */
const DEFINITE_REJECTIONS: ReadonlySet<number> = new Set([400, 401, 403, 404, 409, 422]);

/** 503 里只有这两种是明确的「什么都没做」；别的 503（代理回的之类）不知道。 */
const DEFINITE_UNAVAILABLE: ReadonlySet<string> = new Set([
  ENCRYPTION_NOT_CONFIGURED,
  DATABASE_NOT_CONFIGURED,
]);

/**
 * 这次签发失败之后，新版本到底入没入库**不知道**吗？
 *
 * 网络错误、超时、5xx（上面两种 503 除外）、非信封响应、以及上表之外的任何状态都算不知道。
 * 若其实已经入库，新版本已是 `PENDING` 而 `secret` 再也拿不回来（docs/api.md「签发」）：
 * 只能退役那个 `PENDING` 再签发一次。所以前端**不自动重试**。
 */
export function isOutcomeUnknown(error: unknown): boolean {
  if (!(error instanceof WebhookSigningError) || error.status === null) {
    return true;
  }
  if (DEFINITE_REJECTIONS.has(error.status)) {
    return false;
  }
  return !(error.status === 503 && DEFINITE_UNAVAILABLE.has(error.code));
}

function secretsUrl(customerId: string, projectId: string): string {
  // 与 adminCustomers 同一个理由：不转义的话 `a/b` 会变成另一条路径。
  return (
    `/api/v1/admin/customers/${encodeURIComponent(customerId)}` +
    `/projects/${encodeURIComponent(projectId)}/webhook-secrets`
  );
}

/** 路径里的 `key_version` 是正整数（`0`、负数、非整数后端一律 422）：不是就别发出去。 */
function isKeyVersion(value: number): boolean {
  return Number.isSafeInteger(value) && value >= 1;
}

function rejectKeyVersion(): Promise<never> {
  return Promise.reject(new RangeError("key_version must be a positive integer"));
}

function versionUrl(customerId: string, projectId: string, keyVersion: number): string {
  return `${secretsUrl(customerId, projectId)}/${String(keyVersion)}`;
}

async function post<T>(url: string, body: object): Promise<T> {
  let status: number | null = null;
  try {
    const response = await client.post<ApiEnvelope<T>>(url, body);
    status = response.status;
    return unwrapEnvelope<T>(response.data);
  } catch (error) {
    if (error instanceof AxiosError && error.response !== undefined) {
      status = error.response.status;
    }
    // ⚠️ 不打日志：成功响应里有 `secret`，失败时 axios 的错误对象也挂着请求与响应。
    const apiError = toApiError(error);
    throw new WebhookSigningError(apiError.code, apiError.message, apiError.requestId, status);
  }
}

/** 这个项目的全部签名版本，`key_version` 从小到大。没有 `secret`。 */
export function listWebhookSecrets(
  customerId: string,
  projectId: string,
  page: number,
  pageSize: number,
  signal?: AbortSignal,
): Promise<Page<WebhookSecret>> {
  const query = new URLSearchParams({ page: String(page), page_size: String(pageSize) });
  return apiGet<Page<WebhookSecret>>(
    `${secretsUrl(customerId, projectId)}?${query.toString()}`,
    signal,
  );
}

/**
 * 签发。请求体就是 `{}`：`secret` 与版本号都由服务端生成；新版本是 `PENDING`。
 *
 * ⚠️ **不幂等**：防双击是调用方的责任。
 */
export function issueWebhookSecret(
  customerId: string,
  projectId: string,
): Promise<IssuedWebhookSecret> {
  return post<IssuedWebhookSecret>(secretsUrl(customerId, projectId), {});
}

/**
 * 启用一个 `PENDING` 版本。请求体就是 `{}`。原来的 `ACTIVE`（若有）同一事务退役。
 * 返回启用后的全部版本（版本从小到大）。已是 `ACTIVE` 时幂等。
 */
export function activateWebhookSecret(
  customerId: string,
  projectId: string,
  keyVersion: number,
): Promise<WebhookSecret[]> {
  if (!isKeyVersion(keyVersion)) {
    return rejectKeyVersion();
  }
  return post<WebhookSecret[]>(`${versionUrl(customerId, projectId, keyVersion)}/activate`, {});
}

/** 退役一个版本。幂等：已经 `RETIRED` 时返回当前状态。 */
export function retireWebhookSecret(
  customerId: string,
  projectId: string,
  keyVersion: number,
  reason: string,
): Promise<WebhookSecret> {
  if (!isKeyVersion(keyVersion)) {
    return rejectKeyVersion();
  }
  const body: RetireBody = { reason };
  return post<WebhookSecret>(`${versionUrl(customerId, projectId, keyVersion)}/retire`, body);
}

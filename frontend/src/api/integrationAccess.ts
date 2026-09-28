/**
 * 管理端集成 API 凭据（契约见 docs/api.md「管理端集成 API 凭据」，设计闸门 #118，
 * 全文 docs/design/AIH-TASK-012-integration-access.md）。
 *
 * 五个接口都在 `/api/v1/admin/customers/{customer_id}/projects/{project_id}/credentials` 下：
 * 建凭据、列表、轮换、吊销一个版本、吊销整个 key。
 *
 * ⚠️ `secret` **只出现一次**：只在建凭据与轮换的 201 响应里。这一层只把它原样交给调用方，
 * 不缓存、不打日志、不放进任何 URL。调用方同样不许把它写进查询缓存或浏览器存储
 * （见 `IntegrationAccessPanel`）。
 */

import { AxiosError } from "axios";

import { customerProjectsQueryKey, type Page } from "./adminCustomers";
import { ApiError, apiGet, client, toApiError, unwrapEnvelope, type ApiEnvelope } from "./client";

/** 项目不存在，或不属于路径里的客户（两者一模一样）。 */
export const PROJECT_NOT_FOUND = "PROJECT_NOT_FOUND";
/** `api_key` / `key_version` 不存在，或属于别的项目。 */
export const CREDENTIAL_NOT_FOUND = "CREDENTIAL_NOT_FOUND";
/** 轮换时 `current_key_version` 不是当前最大版本（双击、两个管理员同时轮换）。什么都没写。 */
export const CREDENTIAL_VERSION_CONFLICT = "CREDENTIAL_VERSION_CONFLICT";
/** 轮换一个所有版本都已吊销的 `api_key`。什么都没写。 */
export const CREDENTIAL_REVOKED = "CREDENTIAL_REVOKED";
/** 主密钥未配置：只影响建凭据与轮换。什么都没写。 */
export const ENCRYPTION_NOT_CONFIGURED = "ENCRYPTION_NOT_CONFIGURED";
/** 服务没有配置数据库。什么都没写。 */
export const DATABASE_NOT_CONFIGURED = "DATABASE_NOT_CONFIGURED";

/** 只有这两种；`REVOKED` 是终态。「已过期」不另存状态，看 `verifiable`。 */
export type CredentialStatus = "ACTIVE" | "REVOKED";

/** 凭据版本对象。时间是不带时区的 UTC（docs/api.md「时间」）。 */
export interface Credential {
  /** 非机密的查找标识，`ak_` + 32 个小写十六进制字符。轮换时不变。 */
  api_key: string;
  /** 从 1 起。 */
  key_version: number;
  status: CredentialStatus;
  valid_from: string;
  /** `null` 表示没有截止。轮换时旧版本得到截止时间。 */
  valid_until: string | null;
  /** 预留，目前总是 `null`。 */
  last_used_at: string | null;
  created_at: string;
  revoked_at: string | null;
  /** 按响应那一刻算：`ACTIVE`、已到 `valid_from`、且还没到 `valid_until`。 */
  verifiable: boolean;
}

/**
 * 签发的凭据版本（建凭据与轮换的响应）= 凭据版本 + `secret`。
 *
 * ⚠️ `secret` 之后任何接口都不再返回。
 */
export interface IssuedCredential extends Credential {
  secret: string;
}

export interface RotateBody {
  /** 调用方看到的**最新**版本号。JSON 整数，不是字符串。 */
  current_key_version: number;
}

export interface RevokeBody {
  /** 去掉首尾空白后 1–255。进审计；只写业务说明，不写个人数据。 */
  reason: string;
}

/** 一个项目的所有凭据列表页的前缀：任何写操作之后让它们整体过期。 */
export function projectCredentialsQueryKey(customerId: string, projectId: string) {
  return [...customerProjectsQueryKey(customerId), projectId, "credentials"] as const;
}

export function credentialListQueryKey(
  customerId: string,
  projectId: string,
  page: number,
  pageSize: number,
) {
  return [...projectCredentialsQueryKey(customerId, projectId), page, pageSize] as const;
}

/**
 * 凭据接口的错误：一个带 HTTP 状态的 {@link ApiError}。没有响应时状态是 `null`。
 *
 * 与调账同一个理由（`AdjustmentError`）：要分清「后端明确拒绝了」与「不知道写没写上」。
 */
export class CredentialError extends ApiError {
  constructor(
    code: string,
    message: string,
    requestId: string | null,
    readonly status: number | null,
  ) {
    super(code, message, requestId);
    this.name = "CredentialError";
  }
}

/** docs/api.md 为这五个接口列出的、什么都没写的 4xx。 */
const DEFINITE_REJECTIONS: ReadonlySet<number> = new Set([400, 401, 403, 404, 409, 422]);

/** 503 里只有这两种是明确的「什么都没做」；别的 503（代理回的之类）不知道。 */
const DEFINITE_UNAVAILABLE: ReadonlySet<string> = new Set([
  ENCRYPTION_NOT_CONFIGURED,
  DATABASE_NOT_CONFIGURED,
]);

/**
 * 这次建凭据或轮换失败之后，新版本到底入没入库**不知道**吗？
 *
 * 网络错误、超时、5xx（上面两种 503 除外）、非信封响应、以及上表之外的任何状态都算不知道。
 * 若其实已经入库，`secret` 就再也拿不回来了（docs/api.md「轮换」）：只能再轮换一次，或吊销
 * 那个版本。所以前端**不自动重试** —— 自动重试的建凭据会多出一个拿不到 `secret` 的 key。
 */
export function isOutcomeUnknown(error: unknown): boolean {
  if (!(error instanceof CredentialError) || error.status === null) {
    return true;
  }
  if (DEFINITE_REJECTIONS.has(error.status)) {
    return false;
  }
  return !(error.status === 503 && DEFINITE_UNAVAILABLE.has(error.code));
}

function credentialsUrl(customerId: string, projectId: string): string {
  // 与 adminCustomers 同一个理由：不转义的话 `a/b` 会变成另一条路径。
  return (
    `/api/v1/admin/customers/${encodeURIComponent(customerId)}` +
    `/projects/${encodeURIComponent(projectId)}/credentials`
  );
}

function keyUrl(customerId: string, projectId: string, apiKey: string): string {
  return `${credentialsUrl(customerId, projectId)}/${encodeURIComponent(apiKey)}`;
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
    throw new CredentialError(apiError.code, apiError.message, apiError.requestId, status);
  }
}

/** 这个项目下每个 `api_key` 的每个版本：先按 key 的创建先后，再按版本从小到大。没有 `secret`。 */
export function listCredentials(
  customerId: string,
  projectId: string,
  page: number,
  pageSize: number,
  signal?: AbortSignal,
): Promise<Page<Credential>> {
  const query = new URLSearchParams({ page: String(page), page_size: String(pageSize) });
  return apiGet<Page<Credential>>(
    `${credentialsUrl(customerId, projectId)}?${query.toString()}`,
    signal,
  );
}

/**
 * 建凭据。请求体就是 `{}`：`api_key`、`secret`、版本号都由服务端生成。
 *
 * ⚠️ **不幂等**：再调一次得到另一个 `api_key`。防双击是调用方的责任。
 */
export function createCredential(customerId: string, projectId: string): Promise<IssuedCredential> {
  return post<IssuedCredential>(credentialsUrl(customerId, projectId), {});
}

/** 轮换：`api_key` 不变，新版本 `N+1`，响应里带它的 `secret`（只这一次）。 */
export function rotateCredential(
  customerId: string,
  projectId: string,
  apiKey: string,
  currentKeyVersion: number,
): Promise<IssuedCredential> {
  const body: RotateBody = { current_key_version: currentKeyVersion };
  return post<IssuedCredential>(`${keyUrl(customerId, projectId, apiKey)}/rotate`, body);
}

/** 吊销一个版本。幂等：已经 `REVOKED` 时返回当前状态。 */
export function revokeCredentialVersion(
  customerId: string,
  projectId: string,
  apiKey: string,
  keyVersion: number,
  reason: string,
): Promise<Credential> {
  const body: RevokeBody = { reason };
  return post<Credential>(
    `${keyUrl(customerId, projectId, apiKey)}/versions/${encodeURIComponent(String(keyVersion))}/revoke`,
    body,
  );
}

/** 吊销整个 key：返回它的全部版本（版本从小到大）。幂等。 */
export function revokeCredential(
  customerId: string,
  projectId: string,
  apiKey: string,
  reason: string,
): Promise<Credential[]> {
  const body: RevokeBody = { reason };
  return post<Credential[]>(`${keyUrl(customerId, projectId, apiKey)}/revoke`, body);
}

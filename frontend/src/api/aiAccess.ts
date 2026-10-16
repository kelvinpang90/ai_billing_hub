import { customerProjectsQueryKey } from "./adminCustomers";
import { apiGet, client, toApiError, unwrapEnvelope, type ApiEnvelope } from "./client";

export const AI_CLIENT_KEY_LIMIT = "AI_CLIENT_KEY_LIMIT";
export const AI_CLIENT_KEY_REVOKED = "AI_CLIENT_KEY_REVOKED";

export type AiClientKeyStatus = "ACTIVE" | "REVOKED";

export interface AiClientKey {
  client_id: string;
  status: AiClientKeyStatus;
  created_at: string;
  revoked_at: string | null;
}

export interface IssuedAiClientKey extends AiClientKey {
  token: string;
}

export function projectAiAccessQueryKey(customerId: string, projectId: string) {
  return [...customerProjectsQueryKey(customerId), projectId, "ai-access"] as const;
}

function accessUrl(customerId: string, projectId: string): string {
  return (
    `/api/v1/admin/customers/${encodeURIComponent(customerId)}` +
    `/projects/${encodeURIComponent(projectId)}/ai-access`
  );
}

export function listAiClientKeys(
  customerId: string,
  projectId: string,
  signal?: AbortSignal,
): Promise<AiClientKey[]> {
  return apiGet<AiClientKey[]>(accessUrl(customerId, projectId), signal);
}

async function post<T>(url: string): Promise<T> {
  try {
    const response = await client.post<ApiEnvelope<T>>(url);
    return unwrapEnvelope<T>(response.data);
  } catch (error) {
    throw toApiError(error);
  }
}

/** 签发不幂等；调用方必须在提交中禁用按钮，且不自动重试。 */
export function issueAiClientKey(customerId: string, projectId: string): Promise<IssuedAiClientKey> {
  return post<IssuedAiClientKey>(`${accessUrl(customerId, projectId)}/client-keys`);
}

export function revokeAiClientKey(
  customerId: string,
  projectId: string,
  clientId: string,
): Promise<AiClientKey> {
  return post<AiClientKey>(
    `${accessUrl(customerId, projectId)}/client-keys/${encodeURIComponent(clientId)}/revoke`,
  );
}

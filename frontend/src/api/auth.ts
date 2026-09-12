/**
 * The auth endpoints (spec §53, §54).
 *
 * 登录是**两步**：`/login` 用密码换一张 `pending_token`，`/login/totp` 用验证码
 * 换会话。ADMIN 的 2FA 是强制的，所以没注册过的管理员会先被送去 `/2fa/enrol`。
 */

import { client, toApiError, unwrapEnvelope, type ApiEnvelope } from "./client";

export const STAGE_TOTP_REQUIRED = "TOTP_REQUIRED";
export const STAGE_ENROL_2FA = "ENROL_2FA";

export interface LoginResult {
  access_token: string | null;
  token_type: string | null;
  expires_in: number | null;
  stage: string | null;
  pending_token: string | null;
  recovery_codes_remaining: number | null;
}

export interface Enrolment {
  secret: string;
  otpauth_uri: string;
}

export interface RecoveryCodes {
  recovery_codes: string[];
}

async function post<T>(url: string, body?: unknown): Promise<T> {
  try {
    const response = await client.post<ApiEnvelope<T>>(url, body);
    return unwrapEnvelope<T>(response.data);
  } catch (error) {
    throw toApiError(error);
  }
}

export function login(email: string, password: string): Promise<LoginResult> {
  return post<LoginResult>("/api/v1/auth/login", { email, password });
}

export function submitSecondFactor(pendingToken: string, code: string): Promise<LoginResult> {
  return post<LoginResult>("/api/v1/auth/login/totp", {
    pending_token: pendingToken,
    code,
  });
}

export function startEnrolment(pendingToken: string): Promise<Enrolment> {
  return post<Enrolment>("/api/v1/auth/2fa/enrol", { pending_token: pendingToken });
}

export function confirmEnrolment(pendingToken: string, code: string): Promise<RecoveryCodes> {
  return post<RecoveryCodes>("/api/v1/auth/2fa/confirm", {
    pending_token: pendingToken,
    code,
  });
}

export function logout(): Promise<unknown> {
  return post<unknown>("/api/v1/auth/logout");
}
